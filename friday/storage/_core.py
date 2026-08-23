"""Storage methods for connection lifecycle, schema creation and migration.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

import re
import secrets
import stat
import unicodedata
import zlib
from collections.abc import Callable
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from friday.audit_privacy import (
    decode_audit_privacy_key,
    sanitize_audit_action,
    sanitize_audit_actor,
    sanitize_audit_created_at,
    sanitize_audit_id,
    sanitize_audit_ip,
    sanitize_audit_payload,
    sanitize_audit_request_id,
    sanitize_audit_target,
)
from friday.interaction_control_plane.failure_schema import (
    INTERACTION_FAILURE_SCHEMA,
    INTERACTION_FAILURE_SCHEMA_VERSION,
    validate_interaction_failure_schema,
)
from friday.private_fs import prepare_private_sqlite, restrict_sqlite_files
from friday.storage._base import (
    CORE_INDEX_SCHEMA,
    CORE_TABLE_SCHEMA,
    DATA_SOURCES_SCHEMA,
    FTS_SCHEMA,
    FTS_VOCAB_SCHEMA,
    LOGGER,
    MISSION_TASKS_SCHEMA,
    SCHEMA_VERSION,
    Any,
    FridaySettings,
    Iterator,
    StorageClosedError,
    StorageShared,
    UnsupportedSchemaVersionError,
    _snapshot,
    audit_generated_id_exists,
    contextmanager,
    deleted_account_tombstone_key,
    hashlib,
    json,
    new_id,
    normalize_entity_name,
    sqlite3,
    suppress,
    threading,
    time,
    utc_now,
)
from friday.storage._obsidian import (
    OBSIDIAN_SCHEMA,
    upgrade_obsidian_schema_35_to_36,
    validate_obsidian_schema,
)
from friday.storage._privacy import (
    PRIVATE_DERIVATIVE_CACHE_REBUILD_SQL,
    PRIVATE_MATERIAL_CACHE_REBUILD_SQL,
    PRIVATE_MATERIAL_PERSISTENT_SCHEMA,
    PRIVATE_MATERIAL_RUNTIME_SCHEMA,
)
from friday.storage.models import RelationHistorySnapshotError, normalize_known_at

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DMY_DATE_RE = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})$")
# Tests and clock adapters replace ``datetime`` to control wall time. Timestamp
# parsing must remain the real stdlib implementation under that substitution.
_TIMESTAMP_DATETIME = datetime
_GUARDED_TRANSACTION_CONTEXT: ContextVar[
    tuple[
        object,
        Callable[[], None],
        Callable[[], None] | None,
        Callable[[], None] | None,
    ]
    | None
] = ContextVar(
    "friday_guarded_transaction",
    default=None,
)

_OLDEST_MIGRATABLE_DATABASE_SCHEMA = 13
_REQUIRED_SCHEMA_META_COLUMNS = frozenset({"key", "value", "updated_at"})
_REQUIRED_USERS_COLUMNS = frozenset(
    {
        "id",
        "source",
        "external_id",
        "display_name",
        "username",
        "preset_key",
        "status",
        "metadata_json",
        "created_at",
        "updated_at",
        "last_seen_at",
    }
)


def _required_database_fingerprint(path: Path) -> tuple[int, int]:
    """Prove that the authoritative database path is one nonempty regular file."""

    try:
        metadata = path.lstat()
    except OSError as exc:
        raise sqlite3.OperationalError("required Friday database is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
        raise sqlite3.OperationalError("required Friday database is unavailable or empty")
    return int(metadata.st_dev), int(metadata.st_ino)


def _required_database_has_friday_schema(conn: sqlite3.Connection) -> bool:
    """Recognize every supported Friday schema without writing to the image."""

    meta_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(schema_meta)")}
    user_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(users)")}
    if not (meta_columns >= _REQUIRED_SCHEMA_META_COLUMNS and user_columns >= _REQUIRED_USERS_COLUMNS):
        return False
    try:
        row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        version = int(str(row[0]).strip()) if row is not None else -1
    except (IndexError, TypeError, ValueError, sqlite3.DatabaseError):
        return False
    # The migration fixtures cover every released schema from 13 onward.  A
    # lower/missing marker has no audited upgrade path; a newer marker belongs
    # to code this process does not understand.  Reject both before WAL/DDL.
    return _OLDEST_MIGRATABLE_DATABASE_SCHEMA <= version <= SCHEMA_VERSION


def _unicode_casefold(value: Any) -> str | None:
    """Canonical Unicode casefold used by privacy-sensitive SQL comparisons."""

    if value is None:
        return None
    # Canonically equivalent text can arrive precomposed (NFC) from the UI and
    # decomposed (NFD) from files/bridges.  Plain ``casefold`` preserves that
    # distinction, which let a copied private name evade every SQL dependency
    # scan.  Normalise both before and after folding so the UDF has one stable
    # representation even for folds which themselves introduce combining code
    # points.
    return unicodedata.normalize(
        "NFC",
        unicodedata.normalize("NFC", str(value)).casefold(),
    )


def _private_identity_match(value: Any, identity: Any) -> int:
    """Match one canonical identity as a token, never as an alnum prefix.

    The old substring comparison made ``дело 1`` quarantine ``дело 10``.  A
    copied name/alias still matches case-insensitively and across NFC/NFD, but an
    adjacent Unicode letter, number or underscore keeps a longer token distinct.
    Entity ids continue to use their deliberately conservative substring checks.
    """

    folded_value = _unicode_casefold(value) or ""
    folded_identity = _unicode_casefold(identity) or ""
    if not folded_identity:
        return 0

    def continues_token(character: str) -> bool:
        return character == "_" or character.isalnum()

    offset = 0
    while True:
        start = folded_value.find(folded_identity, offset)
        if start < 0:
            return 0
        end = start + len(folded_identity)
        left_ok = (
            not continues_token(folded_identity[0])
            or start == 0
            or not continues_token(folded_value[start - 1])
        )
        right_ok = (
            not continues_token(folded_identity[-1])
            or end == len(folded_value)
            or not continues_token(folded_value[end])
        )
        if left_ok and right_ok:
            return 1
        offset = start + 1


_RELATION_HISTORY_TRIGGER_TABLES_V31 = {
    # Full current-projection capture.
    "relations_revision_ai": "relations",
    "relations_revision_au": "relations",
    "relations_revision_bd": "relations",
    # Relation ID and tenant identity cannot move between lineages.
    "relations_revision_identity_immutable": "relations",
    # Existing evidence and its completeness promise are append-only.
    "relation_revisions_append_only_update": "relation_revisions",
    "relation_revisions_append_only_delete": "relation_revisions",
    "relation_revisions_append_only_replace": "relation_revisions",
    "relation_history_floor_immutable_update": "schema_meta",
    "relation_history_floor_immutable_delete": "schema_meta",
    "relation_history_floor_immutable_insert": "schema_meta",
}
_RELATION_HISTORY_TRIGGER_TABLES = {
    **_RELATION_HISTORY_TRIGGER_TABLES_V31,
    # Schema 32: SQLite REPLACE conflict-deletes bypass DELETE capture when
    # recursive_triggers is disabled (the default), and the observed boundary
    # is an append-only promise in its own right.
    "relations_revision_insert_guard": "relations",
    "relations_revision_update_conflict_guard": "relations",
    "relation_revision_context_monotonic_update": "relation_revision_context",
    "relation_revision_context_immutable_delete": "relation_revision_context",
    "relation_revision_context_singleton_insert": "relation_revision_context",
}
_RELATION_HISTORY_OWNED_TABLES = {
    "relation_revision_context",
    "relation_revisions",
}
_RELATION_HISTORY_SCHEMA_V31_TABLES = _RELATION_HISTORY_OWNED_TABLES | {"relations"}
# Exact sqlite_master.sql digests produced by the deployed schema-31 build.
# They are intentionally code constants rather than a permissive shape check:
# only that known predecessor may cross the 31→32 boundary. A missing/altered
# capture guard still fails before any idempotent DDL can conceal lost history.
_RELATION_HISTORY_SCHEMA_V31_SHA256 = {
    "relations": "0996aff8ddf8910ebd8c25142601091d8b877b1bb9e7f99be095381dd70485e7",
    "relation_revision_context": "80a1c5834ee4fa98420a56ff4d5cbda4a50cb85cc2fcb4162ed35d9cad0eeb1d",
    "relation_revisions": "6812d27ef68f5d8a2a7f27a56b695468daf0c409088a1f2bbd8eaae9049e0b81",
    "relations_revision_ai": "d91db1e4fe288ce8d46f3eeca4d9f104d2b38438011ab31c93de4c1d703cc416",
    "relations_revision_au": "5cc9a16fc99f986f1041997c556676e0d7b3d0f7890698082bea3f15d421a3e3",
    "relations_revision_bd": "7da0a75a53cd2a59446d59ef4a81d2bf3d8d99cc5392b57d7a71d937806630f6",
    "relations_revision_identity_immutable": "16a9ed76dfb2ddc4492c8df37686a2deb13da000933add19cb8653f5842cee26",
    "relation_revisions_append_only_update": "dc612388aacf54f8c75bea9e4014db805ea9af4712bb83f614e2e33dac24ccd8",
    "relation_revisions_append_only_delete": "2dcb37864e178eabb7bf0cc61630fc5ae0d5d8dc171f1e8f1ea2f10e4251cf3c",
    "relation_revisions_append_only_replace": "4502b0011b936143863c1938f38e281b79bc8a6a648e4b9993482cde5f40c6e8",
    "relation_history_floor_immutable_update": "d2674de317c99dcf40045d4a0a9b6e09b5caef7292aee8b080e0eefcedc21d13",
    "relation_history_floor_immutable_delete": "2be6ca1d13d394e9f01ef5c6f224f22d597ec638dda98b81307b50b5b0ed0545",
    "relation_history_floor_immutable_insert": "80c9c54a0300f5070812fb2d5574d3921646f369e686d986c92223d0daf5fa3c",
}
# `relations` predates the history layer. Historical schema 13–26 databases
# reached its current columns through ALTER TABLE, while schema 27+ created the
# canonical table directly; sqlite_master therefore has two legitimate byte
# shapes. Every synthetic historical fixture reaches exactly one of these two.
_RELATION_PROJECTION_SCHEMA_SHA256 = frozenset(
    {
        "0996aff8ddf8910ebd8c25142601091d8b877b1bb9e7f99be095381dd70485e7",
        "ae3c02812bc65f1e2db6dce5d814840e353ddd2f42e73c43eb2a27794c8bb37e",
    }
)
# Every UNIQUE index on a table protected by relation-history guards affects
# SQLite's conflict set. An unrecognised index can make INSERT OR REPLACE evict a
# different authoritative row while recursive DELETE triggers are disabled.
_RELATION_HISTORY_UNIQUE_INDEX_TABLES = frozenset(_RELATION_HISTORY_TRIGGER_TABLES.values())
_PRIVATE_MATERIAL_CACHE_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_cache_ai",
        "privacy_material_cache_au",
        "privacy_material_cache_ad",
    }
)
_PRIVATE_MATERIAL_CACHE_STATE_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_cache_state_bi",
        "privacy_material_cache_state_bu",
    }
)
_PRIVATE_MATERIAL_WORK_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_work_ai",
        "privacy_material_work_au",
        "privacy_material_work_ad",
    }
)
_PRIVATE_MATERIAL_DERIVATIVE_CACHE_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_derivative_cache_ai",
        "privacy_material_derivative_cache_au",
        "privacy_material_derivative_cache_ad",
    }
)
_PRIVATE_MATERIAL_DERIVATIVE_WORK_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_derivative_work_ai",
        "privacy_material_derivative_work_au",
        "privacy_material_derivative_work_ad",
    }
)
_PRIVATE_MATERIAL_DERIVATIVE_STATE_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_derivative_state_bi",
        "privacy_material_derivative_state_bu",
    }
)
_PRIVATE_MATERIAL_WRITER_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_entities_ai",
        "privacy_material_entities_ai_restore",
        "privacy_material_entities_au",
        "privacy_material_entities_au_restore",
        "privacy_material_entities_ad",
        "privacy_material_owners_ai",
        "privacy_material_owners_au",
        "privacy_material_owners_ad",
        "privacy_material_time_ai",
        "privacy_material_time_au",
        "privacy_material_time_ad",
        "privacy_material_versions_ai",
        "privacy_material_versions_ai_restore",
        "privacy_material_versions_au",
        "privacy_material_versions_au_restore",
        "privacy_material_versions_ad",
    }
)
_PRIVATE_MATERIAL_LEGACY_WRITER_TRIGGER_NAMES = frozenset(
    name for name in _PRIVATE_MATERIAL_WRITER_TRIGGER_NAMES if not name.endswith("_restore")
)
_PRIVATE_MATERIAL_INVALIDATOR_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_entities_bi_invalidate",
        "privacy_material_entities_bu_invalidate",
        "privacy_material_entities_bd_invalidate",
        "privacy_material_owners_bi_invalidate",
        "privacy_material_owners_bu_invalidate",
        "privacy_material_owners_bd_invalidate",
        "privacy_material_time_bi_invalidate",
        "privacy_material_time_bu_invalidate",
        "privacy_material_time_bd_invalidate",
        "privacy_material_versions_bi_invalidate",
        "privacy_material_versions_bu_invalidate",
        "privacy_material_versions_bd_invalidate",
    }
)
_PRIVATE_MATERIAL_DERIVATIVE_INVALIDATOR_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_raw_bi_invalidate",
        "privacy_material_raw_bu_invalidate",
        "privacy_material_raw_bd_invalidate",
        "privacy_material_knowledge_bi_invalidate",
        "privacy_material_knowledge_bu_invalidate",
        "privacy_material_knowledge_bd_invalidate",
        "privacy_material_links_bi_invalidate",
        "privacy_material_links_bu_invalidate",
        "privacy_material_links_bd_invalidate",
        "privacy_material_inbox_bi_invalidate",
        "privacy_material_inbox_bu_invalidate",
        "privacy_material_inbox_bd_invalidate",
    }
)
_PRIVATE_MATERIAL_DERIVATIVE_REFRESH_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_derivative_refresh_ai",
        "privacy_material_raw_ai_refresh",
        "privacy_material_raw_au_refresh",
        "privacy_material_raw_ad_refresh",
        "privacy_material_knowledge_ai_refresh",
        "privacy_material_knowledge_au_refresh",
        "privacy_material_knowledge_ad_refresh",
        "privacy_material_links_ai_refresh",
        "privacy_material_links_au_refresh",
        "privacy_material_links_ad_refresh",
        "privacy_material_inbox_ai_refresh",
        "privacy_material_inbox_au_refresh",
        "privacy_material_inbox_ad_refresh",
    }
)
_PRIVATE_MATERIAL_LEGACY_CACHE_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_cache_bi",
        "privacy_material_cache_bu",
        "privacy_material_cache_bd",
    }
)
_PRIVATE_MATERIAL_ENTITY_GUARD_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_entities_private_bu",
        "privacy_material_entities_private_bd",
        "privacy_material_versions_private_bu",
        "privacy_material_versions_private_bd",
    }
)
_PRIVATE_MATERIAL_LEGACY_ENTITY_GUARD_TRIGGER_NAMES = frozenset(
    {
        "privacy_material_owners_private_bu",
        "privacy_material_owners_private_bd",
        "privacy_material_time_private_bu",
        "privacy_material_time_private_bd",
    }
)

_AUDIT_PRIVACY_MARKER_KEY = "audit_payload_privacy"
_AUDIT_PRIVACY_MARKER_VALUE = "v3"
_AUDIT_PRIVACY_PENDING_VALUE = "pending_wal_truncate:v3"
_AUDIT_PRIVACY_V1_VALUES = frozenset({"v1", "v2", "pending_wal_truncate", "pending_wal_truncate:v2"})
_AUDIT_PRIVACY_HMAC_KEY = "audit_privacy_hmac_key"
_PRIVATE_MATERIAL_RULE_MARKER_KEY = "private_material_rule_digest"
_IDEMPOTENCY_PRIVACY_MARKER_KEY = "idempotency_response_privacy"
_IDEMPOTENCY_PRIVACY_MARKER_VALUE = "v1"
_IDEMPOTENCY_PRIVACY_PENDING_VALUE = "pending_wal_truncate:v1"
_AUDIT_APPEND_ONLY_TRIGGERS = frozenset(
    {
        "audit_log_no_delete",
        "audit_log_no_update",
        "audit_log_privacy_pending_no_insert",
    }
)

_PRIVATE_MIGRATION_SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024
_PACKED_KNOWLEDGE_SNAPSHOT_MAGIC = b"zKOV1"
_PRIVATE_IDENTITY_INPUT_MAX_BYTES = 1_048_576
_PRIVATE_IDENTITY_EXPANDED_MAX_BYTES = 8 * 1_048_576
_PRIVATE_IDENTITY_MAX_NODES = 200_000
_PRIVATE_IDENTITY_QUOTED_FRAGMENT_RE = re.compile(r'"((?:\\.|[^"\\])*)"|\'((?:\\.|[^\'\\])*)\'')
_PRIVATE_IDENTITY_FRAGMENT_SPLIT_RE = re.compile(r"[{}\[\],:\s\"']+")


def _private_identity_tokens_json(name_value: Any, aliases_value: Any) -> str:
    """Expand bounded current/history identity into scalar JSON tokens.

    Old imports contain aliases nested in objects and, occasionally, JSON encoded
    a second time inside a string.  SQLite's one-pass ``json_tree`` sees only the
    wrapper in the latter case.  Decode recursively under hard byte/node budgets;
    exceeding either raises and therefore fails the privacy query closed instead
    of returning an incomplete token set.
    """

    def text_value(value: Any) -> str:
        result = value.decode("utf-8") if isinstance(value, bytes) else str(value or "")
        if len(result.encode("utf-8")) > _PRIVATE_IDENTITY_INPUT_MAX_BYTES:
            raise ValueError("private identity input exceeds inspection budget")
        return result

    tokens: set[str] = set()
    pending: list[Any] = []
    expanded_bytes = 0
    visited = 0

    def queue_malformed_fragments(value: str) -> None:
        fragments: set[str] = set()
        for match in _PRIVATE_IDENTITY_QUOTED_FRAGMENT_RE.finditer(value):
            fragment = match.group(1) if match.group(1) is not None else match.group(2)
            if fragment:
                fragments.add(fragment)
        fragments.update(
            fragment for fragment in _PRIVATE_IDENTITY_FRAGMENT_SPLIT_RE.split(value) if fragment
        )
        for fragment in fragments:
            if fragment == value:
                continue
            try:
                decoded = json.loads(f'"{fragment}"')
            except (TypeError, ValueError, RecursionError):
                decoded = fragment
            if isinstance(decoded, str) and decoded and decoded != value:
                pending.append(decoded)

    def add_text(value: str) -> None:
        nonlocal expanded_bytes
        encoded_size = len(value.encode("utf-8"))
        if encoded_size > _PRIVATE_IDENTITY_INPUT_MAX_BYTES:
            raise ValueError("private identity token exceeds inspection budget")
        if value and value not in tokens:
            expanded_bytes += encoded_size
            if expanded_bytes > _PRIVATE_IDENTITY_EXPANDED_MAX_BYTES:
                raise ValueError("private identity expansion exceeds inspection budget")
            tokens.add(value)
        candidate = value.lstrip()
        if not candidate.startswith(("{", "[", '"')):
            return
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, RecursionError):
            queue_malformed_fragments(value)
            return
        if isinstance(decoded, (dict, list, str)) and decoded != value:
            pending.append(decoded)

    name = text_value(name_value)
    add_text(name)
    aliases = text_value(aliases_value)
    try:
        decoded_aliases = json.loads(aliases)
    except (TypeError, ValueError, RecursionError):
        add_text(aliases)
    else:
        pending.append(decoded_aliases)

    while pending:
        visited += 1
        if visited > _PRIVATE_IDENTITY_MAX_NODES:
            raise ValueError("private identity expansion exceeds node budget")
        item = pending.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                add_text(str(key))
                pending.append(value)
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            add_text(item)

    return json.dumps(sorted(tokens), ensure_ascii=False, separators=(",", ":"))


def _bounded_knowledge_version_snapshot(value: Any) -> dict[str, Any] | None:
    """Inspect legacy KOV provenance without accepting a decompression bomb."""

    try:
        if isinstance(value, bytes):
            if len(value) > _PRIVATE_MIGRATION_SNAPSHOT_MAX_BYTES:
                return None
            if value.startswith(_PACKED_KNOWLEDGE_SNAPSHOT_MAGIC):
                decompressor = zlib.decompressobj()
                raw = decompressor.decompress(
                    value[len(_PACKED_KNOWLEDGE_SNAPSHOT_MAGIC) :],
                    _PRIVATE_MIGRATION_SNAPSHOT_MAX_BYTES + 1,
                )
                if (
                    len(raw) > _PRIVATE_MIGRATION_SNAPSHOT_MAX_BYTES
                    or decompressor.unconsumed_tail
                    or not decompressor.eof
                ):
                    return None
                text = raw.decode("utf-8")
            else:
                text = value.decode("utf-8")
        else:
            text = str(value or "")
    except (UnicodeError, ValueError, OSError, zlib.error):
        return None
    if not text or len(text) > _PRIVATE_MIGRATION_SNAPSHOT_MAX_BYTES:
        return None
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _snapshot_private_token_matches(
    snapshot: dict[str, Any],
    token_owners: dict[str, set[str]],
    entity_id_tokens: set[str],
) -> set[str] | None:
    """Return candidate entity ids copied anywhere into a bounded snapshot.

    ``None`` means the nested shape exceeded the inspection budget and every
    candidate in that tenant must remain quarantined.
    """

    matches: set[str] = set()
    pending: list[Any] = [snapshot]
    visited = 0
    while pending:
        visited += 1
        if visited > 1_000_000:
            return None
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            for token, entity_ids in token_owners.items():
                if token and (
                    (token in entity_id_tokens and token in value)
                    or (token not in entity_id_tokens and _private_identity_match(value, token))
                ):
                    matches.update(entity_ids)
            candidate = value.lstrip()
            if candidate.startswith(("{", "[", '"')):
                try:
                    decoded = json.loads(value)
                except (TypeError, ValueError, RecursionError):
                    return None
                if isinstance(decoded, (dict, list, str)) and decoded != value:
                    pending.append(decoded)
    return matches


def _merge_transfer_reminder_owner(value: Any) -> tuple[bool, str]:
    """Find reminder provenance in one bounded merge transfer, JSON-spacing agnostic."""

    try:
        if isinstance(value, bytes):
            raw = value
            text = value.decode("utf-8")
        else:
            text = str(value or "")
            raw = text.encode("utf-8")
    except UnicodeError:
        return (isinstance(value, bytes) and b"reminder:" in value), ""
    if len(raw) > 1_048_576:
        return ("reminder:" in text), ""
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return ("reminder:" in text), ""
    if not isinstance(decoded, dict):
        return ("reminder:" in text), ""

    owners: set[str] = set()
    ambiguous = False
    pending: list[Any] = [decoded]
    visited = 0
    while pending:
        visited += 1
        if visited > 100_000:
            return ("reminder:" in text), ""
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and "reminder:" in item:
            if item.startswith("reminder:") and item.count("reminder:") == 1:
                owner = item[len("reminder:") :]
                if owner:
                    owners.add(owner)
                else:
                    ambiguous = True
            else:
                ambiguous = True
    if not owners and not ambiguous:
        return False, ""
    return True, next(iter(owners)) if len(owners) == 1 and not ambiguous else ""


def _private_material_table_columns(
    conn: sqlite3.Connection,
    table: str,
) -> list[tuple[str, str, int, int]]:
    """Return the fixed shape fields needed for owned derivative tables."""

    return [
        (str(row["name"]), str(row["type"]).upper(), int(row["notnull"]), int(row["pk"]))
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()  # nosec B608
    ]


def _validate_private_material_cache_tables(
    conn: sqlite3.Connection,
    *,
    allow_legacy_foreign_keys: bool = False,
) -> None:
    """Reject counterfeit owned tables which ``IF NOT EXISTS`` would conceal."""

    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "private_entity_material_cache" in tables:
        if _private_material_table_columns(conn, "private_entity_material_cache") != [
            ("entity_id", "TEXT", 0, 1)
        ]:
            raise sqlite3.DatabaseError("Private material cache table shape is invalid")
        foreign_keys = conn.execute('PRAGMA foreign_key_list("private_entity_material_cache")').fetchall()
        legacy_foreign_key = len(foreign_keys) == 1 and (
            str(foreign_keys[0]["table"]),
            str(foreign_keys[0]["from"]),
            str(foreign_keys[0]["to"]),
            str(foreign_keys[0]["on_delete"]).upper(),
        ) == ("entities", "entity_id", "id", "CASCADE")
        if foreign_keys and not (allow_legacy_foreign_keys and legacy_foreign_key):
            raise sqlite3.DatabaseError("Private material cache foreign key is invalid")
    if "private_entity_material_work" in tables:
        if _private_material_table_columns(conn, "private_entity_material_work") != [
            ("entity_id", "TEXT", 0, 1)
        ]:
            raise sqlite3.DatabaseError("Private material work table shape is invalid")
        work_foreign_keys = conn.execute('PRAGMA foreign_key_list("private_entity_material_work")').fetchall()
        legacy_work_foreign_key = len(work_foreign_keys) == 1 and (
            str(work_foreign_keys[0]["table"]),
            str(work_foreign_keys[0]["from"]),
            str(work_foreign_keys[0]["to"]),
            str(work_foreign_keys[0]["on_delete"]).upper(),
        ) == ("entities", "entity_id", "id", "CASCADE")
        if work_foreign_keys and not (allow_legacy_foreign_keys and legacy_work_foreign_key):
            raise sqlite3.DatabaseError("Private material work foreign key is invalid")
    if "private_entity_material_cache_state" in tables:
        state_columns = _private_material_table_columns(conn, "private_entity_material_cache_state")
        canonical_state = [
            ("singleton", "INTEGER", 0, 1),
            ("valid", "INTEGER", 1, 0),
            ("prior_valid", "INTEGER", 1, 0),
        ]
        legacy_state = canonical_state[:2]
        if state_columns != canonical_state and not (
            allow_legacy_foreign_keys and state_columns == legacy_state
        ):
            raise sqlite3.DatabaseError("Private material cache state table shape is invalid")
    derivative_shape = [
        ("material_kind", "TEXT", 1, 1),
        ("object_id", "TEXT", 1, 2),
        ("user_id", "TEXT", 1, 3),
    ]
    for table in (
        "private_entity_material_derivative_cache",
        "private_entity_material_derivative_work",
    ):
        if table not in tables:
            continue
        if _private_material_table_columns(conn, table) != derivative_shape:
            raise sqlite3.DatabaseError(f"Private derivative table shape is invalid: {table}")
        if conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():  # nosec B608
            raise sqlite3.DatabaseError(f"Private derivative table foreign key is invalid: {table}")
    if "private_entity_material_derivative_state" in tables and _private_material_table_columns(
        conn,
        "private_entity_material_derivative_state",
    ) != [
        ("singleton", "INTEGER", 0, 1),
        ("valid", "INTEGER", 1, 0),
        ("prior_valid", "INTEGER", 1, 0),
    ]:
        raise sqlite3.DatabaseError("Private derivative state table shape is invalid")


def _private_material_unexpected_triggers(
    conn: sqlite3.Connection,
    *,
    allow_legacy: bool,
) -> list[str]:
    allowed = (
        _PRIVATE_MATERIAL_CACHE_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_CACHE_STATE_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_WORK_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_DERIVATIVE_CACHE_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_DERIVATIVE_WORK_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_DERIVATIVE_STATE_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_INVALIDATOR_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_DERIVATIVE_INVALIDATOR_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_ENTITY_GUARD_TRIGGER_NAMES
    )
    if allow_legacy:
        allowed |= (
            _PRIVATE_MATERIAL_LEGACY_CACHE_TRIGGER_NAMES
            | _PRIVATE_MATERIAL_LEGACY_WRITER_TRIGGER_NAMES
            | _PRIVATE_MATERIAL_LEGACY_ENTITY_GUARD_TRIGGER_NAMES
        )
    return [
        str(row["name"])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='trigger' AND name LIKE 'privacy_material_%'
                 ORDER BY name"""
        ).fetchall()
        if str(row["name"]) not in allowed
    ]


def _validate_private_material_cache_pre_schema(conn: sqlite3.Connection) -> None:
    """Validate persistent cache artifacts before canonical DDL can hide them."""

    _validate_private_material_cache_tables(conn, allow_legacy_foreign_keys=True)
    if unexpected := _private_material_unexpected_triggers(conn, allow_legacy=True):
        raise sqlite3.DatabaseError(
            "Private material cache has unexpected persistent triggers: " + ", ".join(unexpected)
        )


def _drop_private_material_runtime_triggers(conn: sqlite3.Connection) -> None:
    """Remove only validated, code-owned guards before the legacy owner move."""

    trigger_names = (
        _PRIVATE_MATERIAL_CACHE_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_CACHE_STATE_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_WORK_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_DERIVATIVE_CACHE_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_DERIVATIVE_WORK_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_DERIVATIVE_STATE_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_LEGACY_WRITER_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_INVALIDATOR_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_DERIVATIVE_INVALIDATOR_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_ENTITY_GUARD_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_LEGACY_ENTITY_GUARD_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_LEGACY_CACHE_TRIGGER_NAMES
    )
    for trigger_name in sorted(trigger_names):
        conn.execute(
            f'DROP TRIGGER IF EXISTS main."{trigger_name}"'  # nosec B608 - fixed allowlist
        )


def _install_private_material_authorizer(conn: sqlite3.Connection) -> None:
    """Deny direct mutation of the privacy authority after startup.

    SQLite identifies the trigger which caused an authorised nested write.  That
    gives the staging/cache design the distinction SQL tables alone cannot make:
    runtime graph triggers may atomically rebuild it, while a caller cannot edit
    cache, work and validity rows in concert and forge a self-consistent leak.
    """

    cache_tables = {
        "private_entity_material_cache",
        "private_entity_material_work",
    }
    state_table = "private_entity_material_cache_state"
    allowed_state_sources = (
        _PRIVATE_MATERIAL_WRITER_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_INVALIDATOR_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_CACHE_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_WORK_TRIGGER_NAMES
    )
    derivative_tables = {
        "private_entity_material_derivative_cache",
        "private_entity_material_derivative_work",
    }
    derivative_state_table = "private_entity_material_derivative_state"
    derivative_writer_sources = (
        _PRIVATE_MATERIAL_WRITER_TRIGGER_NAMES | _PRIVATE_MATERIAL_DERIVATIVE_REFRESH_TRIGGER_NAMES
    )
    derivative_state_sources = (
        derivative_writer_sources
        | _PRIVATE_MATERIAL_DERIVATIVE_INVALIDATOR_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_DERIVATIVE_CACHE_TRIGGER_NAMES
        | _PRIVATE_MATERIAL_DERIVATIVE_WORK_TRIGGER_NAMES
    )
    ddl_actions = {
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_DROP_VIEW,
    }
    dml_actions = {sqlite3.SQLITE_DELETE, sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE}

    def authorize(
        action: int,
        first: str | None,
        second: str | None,
        _database: str | None,
        source: str | None,
    ) -> int:
        first_name = str(first or "")
        second_name = str(second or "")
        source_name = str(source or "")
        if action in dml_actions and first_name in cache_tables:
            return (
                sqlite3.SQLITE_OK
                if source_name in _PRIVATE_MATERIAL_WRITER_TRIGGER_NAMES
                else sqlite3.SQLITE_DENY
            )
        if action in dml_actions and first_name == state_table:
            return sqlite3.SQLITE_OK if source_name in allowed_state_sources else sqlite3.SQLITE_DENY
        if action in dml_actions and first_name in derivative_tables:
            return sqlite3.SQLITE_OK if source_name in derivative_writer_sources else sqlite3.SQLITE_DENY
        if action in dml_actions and first_name == derivative_state_table:
            return sqlite3.SQLITE_OK if source_name in derivative_state_sources else sqlite3.SQLITE_DENY
        if action in ddl_actions:
            protected = (first_name, second_name)
            if any(
                name.startswith(("private_entity_material_", "privacy_material_"))
                or name
                in {
                    "public_raw_material",
                    "public_knowledge_dependencies",
                    "public_inbox_dependencies",
                }
                for name in protected
            ):
                return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and first_name.casefold() == "writable_schema":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)


def _validate_private_material_cache(
    conn: sqlite3.Connection,
    *,
    fresh_entity_rebuild_from_live: bool = False,
    fresh_derivative_rebuild_from_live: bool = False,
) -> None:
    """Fail startup unless the privacy authority has one exact source.

    A caller which has just rebuilt one authority tier under the same write
    transaction already materialized its complete live fixed point into that work
    table.  In that narrow context compare the published table with its staging
    result instead of expanding the corresponding live view twice more.  Any tier
    which was not freshly rebuilt retains the independent live-source validation.
    """

    _validate_private_material_cache_tables(conn)
    if unexpected := _private_material_unexpected_triggers(conn, allow_legacy=False):
        raise sqlite3.DatabaseError(
            "Private material cache has unexpected persistent triggers: " + ", ".join(unexpected)
        )
    cache_triggers = {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='trigger' AND tbl_name='private_entity_material_cache'"""
        ).fetchall()
    }
    state_triggers = {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='trigger' AND tbl_name='private_entity_material_cache_state'"""
        ).fetchall()
    }
    if cache_triggers != _PRIVATE_MATERIAL_CACHE_TRIGGER_NAMES:
        raise sqlite3.DatabaseError("Private material cache guards are incomplete")
    if state_triggers != _PRIVATE_MATERIAL_CACHE_STATE_TRIGGER_NAMES:
        raise sqlite3.DatabaseError("Private material cache state guards are incomplete")
    work_triggers = {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='trigger' AND tbl_name='private_entity_material_work'"""
        ).fetchall()
    }
    if work_triggers != _PRIVATE_MATERIAL_WORK_TRIGGER_NAMES:
        raise sqlite3.DatabaseError("Private material work guards are incomplete")
    derivative_cache_triggers = {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='trigger'
                   AND tbl_name='private_entity_material_derivative_cache'"""
        ).fetchall()
    }
    if derivative_cache_triggers != _PRIVATE_MATERIAL_DERIVATIVE_CACHE_TRIGGER_NAMES:
        raise sqlite3.DatabaseError("Private derivative cache guards are incomplete")
    derivative_work_triggers = {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='trigger'
                   AND tbl_name='private_entity_material_derivative_work'"""
        ).fetchall()
    }
    if derivative_work_triggers != _PRIVATE_MATERIAL_DERIVATIVE_WORK_TRIGGER_NAMES:
        raise sqlite3.DatabaseError("Private derivative work guards are incomplete")
    derivative_state_triggers = {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='trigger'
                   AND tbl_name='private_entity_material_derivative_state'"""
        ).fetchall()
    }
    if derivative_state_triggers != _PRIVATE_MATERIAL_DERIVATIVE_STATE_TRIGGER_NAMES:
        raise sqlite3.DatabaseError("Private derivative state guards are incomplete")
    invalidators = {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='trigger' AND name LIKE 'privacy_material_%_invalidate'"""
        ).fetchall()
    }
    if invalidators != (
        _PRIVATE_MATERIAL_INVALIDATOR_TRIGGER_NAMES | _PRIVATE_MATERIAL_DERIVATIVE_INVALIDATOR_TRIGGER_NAMES
    ):
        raise sqlite3.DatabaseError("Private material persistent invalidators are incomplete")
    entity_guards = {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='trigger' AND name LIKE 'privacy_material_%_private_b%'"""
        ).fetchall()
    }
    if entity_guards != _PRIVATE_MATERIAL_ENTITY_GUARD_TRIGGER_NAMES:
        raise sqlite3.DatabaseError("Private material immutable guards are incomplete")
    runtime_triggers = {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_temp_master
                 WHERE type='trigger' AND name LIKE 'privacy_material_%'"""
        ).fetchall()
    }
    if runtime_triggers != (
        _PRIVATE_MATERIAL_WRITER_TRIGGER_NAMES | _PRIVATE_MATERIAL_DERIVATIVE_REFRESH_TRIGGER_NAMES
    ):
        raise sqlite3.DatabaseError("Private material runtime rebuild guards are incomplete")
    runtime_views = {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_temp_master
                 WHERE type='view' AND name IN (
                    'private_entity_material_states',
                    'private_entity_identity_tokens',
                    'private_entity_material_live',
                    'private_entity_material_cached_closure',
                    'private_entity_material_closure',
                    'private_entity_material_derivative_live',
                    'public_raw_material',
                    'public_knowledge_dependencies',
                    'public_inbox_dependencies'
                 )"""
        ).fetchall()
    }
    if runtime_views != {
        "private_entity_material_states",
        "private_entity_identity_tokens",
        "private_entity_material_live",
        "private_entity_material_cached_closure",
        "private_entity_material_closure",
        "private_entity_material_derivative_live",
        "public_raw_material",
        "public_knowledge_dependencies",
        "public_inbox_dependencies",
    }:
        raise sqlite3.DatabaseError("Private material runtime views are incomplete")
    refresh_tables = {
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_temp_master
                 WHERE type='table'
                   AND name IN (
                       'private_entity_material_derivative_refresh',
                       'private_entity_material_derivative_decision'
                   )"""
        ).fetchall()
    }
    if refresh_tables != {
        "private_entity_material_derivative_refresh",
        "private_entity_material_derivative_decision",
    }:
        raise sqlite3.DatabaseError("Private derivative refresh control is incomplete")
    udf_schema = conn.execute(
        """SELECT name FROM sqlite_master
             WHERE sql IS NOT NULL
               AND (
                    name LIKE 'privacy_material_%'
                    OR name LIKE 'private_entity_material_%'
                    OR name IN (
                        'public_raw_material',
                        'public_knowledge_dependencies',
                        'public_inbox_dependencies'
                    )
               )
               AND instr(lower(sql), 'jericho_')>0
             LIMIT 1"""
    ).fetchone()
    if udf_schema is not None:
        raise sqlite3.DatabaseError("Private material persistent schema depends on runtime UDFs")
    state = conn.execute("SELECT singleton, valid FROM private_entity_material_cache_state").fetchall()
    if len(state) != 1 or tuple(state[0]) != (1, 1):
        raise sqlite3.DatabaseError("Private material cache is not in a valid state")
    derivative_state = conn.execute(
        "SELECT singleton, valid FROM private_entity_material_derivative_state"
    ).fetchall()
    if len(derivative_state) != 1 or tuple(derivative_state[0]) != (1, 1):
        raise sqlite3.DatabaseError("Private derivative cache is not in a valid state")
    duplicate = conn.execute(
        """SELECT COUNT(*)<>COUNT(DISTINCT entity_id)
             FROM private_entity_material_cache"""
    ).fetchone()
    if duplicate is None or bool(duplicate[0]):
        raise sqlite3.DatabaseError("Private material cache contains duplicate ids")
    work_duplicate = conn.execute(
        """SELECT COUNT(*)<>COUNT(DISTINCT entity_id)
             FROM private_entity_material_work"""
    ).fetchone()
    if work_duplicate is None or bool(work_duplicate[0]):
        raise sqlite3.DatabaseError("Private material work table contains duplicate ids")
    for derivative_table in (
        "private_entity_material_derivative_cache",
        "private_entity_material_derivative_work",
    ):
        derivative_duplicate = conn.execute(
            f"""SELECT EXISTS (
                    SELECT 1 FROM {derivative_table}
                     GROUP BY material_kind, object_id, user_id
                    HAVING COUNT(*)>1
                )"""  # nosec B608 - fixed allowlist
        ).fetchone()
        if derivative_duplicate is None or bool(derivative_duplicate[0]):
            raise sqlite3.DatabaseError("Private derivative authority contains duplicate ids")

    if fresh_entity_rebuild_from_live:
        mismatch = conn.execute(
            """SELECT 1 FROM (
                   SELECT work.entity_id
                     FROM private_entity_material_work work
                    WHERE NOT EXISTS (
                        SELECT 1 FROM private_entity_material_cache cached
                         WHERE cached.entity_id=work.entity_id
                    )
                   UNION ALL
                   SELECT cached.entity_id
                     FROM private_entity_material_cache cached
                    WHERE NOT EXISTS (
                        SELECT 1 FROM private_entity_material_work work
                         WHERE work.entity_id=cached.entity_id
                    )
               ) cache_difference
               LIMIT 1"""
        ).fetchone()
    else:
        mismatch = conn.execute(
            """SELECT 1 FROM (
                   SELECT live.id AS entity_id
                     FROM private_entity_material_live live
                    WHERE NOT EXISTS (
                        SELECT 1 FROM private_entity_material_cache cached
                         WHERE cached.entity_id=live.id
                    )
                   UNION ALL
                   SELECT cached.entity_id
                     FROM private_entity_material_cache cached
                    WHERE NOT EXISTS (
                        SELECT 1 FROM private_entity_material_live live
                         WHERE live.id=cached.entity_id
                    )
               ) cache_difference
               LIMIT 1"""
        ).fetchone()
    if mismatch is not None:
        raise sqlite3.DatabaseError("Private material cache rebuild did not match the live privacy closure")
    if fresh_derivative_rebuild_from_live:
        derivative_mismatch = conn.execute(
            """SELECT 1 FROM (
                   SELECT work.material_kind, work.object_id, work.user_id
                     FROM private_entity_material_derivative_work work
                    WHERE NOT EXISTS (
                        SELECT 1 FROM private_entity_material_derivative_cache cached
                         WHERE cached.material_kind=work.material_kind
                           AND cached.object_id=work.object_id
                           AND cached.user_id=work.user_id
                    )
                   UNION ALL
                   SELECT cached.material_kind, cached.object_id, cached.user_id
                     FROM private_entity_material_derivative_cache cached
                    WHERE NOT EXISTS (
                        SELECT 1 FROM private_entity_material_derivative_work work
                         WHERE work.material_kind=cached.material_kind
                           AND work.object_id=cached.object_id
                           AND work.user_id=cached.user_id
                    )
               ) derivative_difference
               LIMIT 1"""
        ).fetchone()
    else:
        derivative_mismatch = conn.execute(
            """SELECT 1 FROM (
                   SELECT live.material_kind, live.object_id, live.user_id
                     FROM private_entity_material_derivative_live live
                    WHERE NOT EXISTS (
                        SELECT 1 FROM private_entity_material_derivative_cache cached
                         WHERE cached.material_kind=live.material_kind
                           AND cached.object_id=live.object_id
                           AND cached.user_id=live.user_id
                    )
                   UNION ALL
                   SELECT cached.material_kind, cached.object_id, cached.user_id
                     FROM private_entity_material_derivative_cache cached
                    WHERE NOT EXISTS (
                        SELECT 1 FROM private_entity_material_derivative_live live
                         WHERE live.material_kind=cached.material_kind
                           AND live.object_id=cached.object_id
                           AND live.user_id=cached.user_id
                    )
               ) derivative_difference
               LIMIT 1"""
        ).fetchone()
    if derivative_mismatch is not None:
        raise sqlite3.DatabaseError("Private derivative cache rebuild did not match live dependencies")


def _invalidate_private_material_on_rule_change(conn: sqlite3.Connection) -> None:
    """Пересчитать кэш приватности, когда изменилось само ПРАВИЛО, а не схема.

    Долговечный кэш — это ответ, посчитанный прежней редакцией правил. Форма
    таблиц при правке правила не меняется, поэтому номер схемы её не замечает, а
    открытие с валидным состоянием не пересобирает и не сверяет кэш: старый ответ
    живёт дальше. Замерено на копии живого архива: без этой отметки послабление
    §76 не вернуло бы владельцу ни одной из 108 запертых записей — код новый,
    кэш прежний.

    Отметка — отпечаток текста самих правил, а не число, которое надо не забыть
    поднять. Любая будущая правка приватного SQL инвалидирует кэш сама.
    """

    digest = hashlib.sha256(
        b"".join(
            statement.encode("utf-8")
            for statement in (
                PRIVATE_MATERIAL_PERSISTENT_SCHEMA,
                PRIVATE_MATERIAL_RUNTIME_SCHEMA,
                PRIVATE_MATERIAL_CACHE_REBUILD_SQL,
                PRIVATE_DERIVATIVE_CACHE_REBUILD_SQL,
            )
        )
    ).hexdigest()
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key=?",
        (_PRIVATE_MATERIAL_RULE_MARKER_KEY,),
    ).fetchone()
    if row is not None and str(row[0]) == digest:
        return
    # Инвалидируется ВЕРХНЯЯ пара: она пересобирает и производную следом.
    conn.execute("UPDATE main.private_entity_material_cache_state SET valid=0 WHERE singleton=1")
    conn.execute("UPDATE main.private_entity_material_derivative_state SET valid=0 WHERE singleton=1")
    conn.execute(
        """INSERT INTO schema_meta(key, value, updated_at) VALUES(?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (_PRIVATE_MATERIAL_RULE_MARKER_KEY, digest, utc_now()),
    )


def _refresh_private_derivative_authority(conn: sqlite3.Connection) -> bool:
    """Publish an exact derivative allowlist when MAIN says it is dirty."""

    state = conn.execute(
        "SELECT valid FROM main.private_entity_material_derivative_state WHERE singleton=1"
    ).fetchone()
    if state is None:
        raise sqlite3.DatabaseError("Private derivative authority state is missing")
    if int(state[0]) == 1:
        return False
    conn.execute("INSERT INTO temp.private_entity_material_derivative_refresh(requested) VALUES(1)")
    healed = conn.execute(
        "SELECT valid FROM main.private_entity_material_derivative_state WHERE singleton=1"
    ).fetchone()
    if healed is None or int(healed[0]) != 1:
        raise sqlite3.DatabaseError("Private derivative authority rebuild did not publish")
    return True


def _invalidate_legacy_idempotency_responses(conn: sqlite3.Connection) -> bool:
    """Securely drop pre-privacy HTTP response copies, once.

    Complete idempotency rows are a retry cache, not authoritative user data.
    Their response bodies predate reminder isolation and cannot be attributed
    retroactively, so parsing them would turn a convenience cache into another
    disclosure oracle.  Pending leases contain only ``{}`` and remain intact.
    """

    marker = conn.execute(
        "SELECT value FROM schema_meta WHERE key=?",
        (_IDEMPOTENCY_PRIVACY_MARKER_KEY,),
    ).fetchone()
    marker_value = str(marker[0]) if marker is not None else ""
    if marker_value == _IDEMPOTENCY_PRIVACY_MARKER_VALUE:
        return False
    if marker_value and marker_value != _IDEMPOTENCY_PRIVACY_PENDING_VALUE:
        raise RuntimeError("Unknown idempotency privacy migration marker")

    conn.execute("PRAGMA secure_delete=ON")
    secure_delete = conn.execute("PRAGMA secure_delete").fetchone()
    if secure_delete is None or int(secure_delete[0]) != 1:
        raise RuntimeError("SQLite secure_delete is unavailable for idempotency privacy migration")
    conn.execute("DELETE FROM request_idempotency WHERE state='complete'")
    conn.execute(
        """INSERT INTO schema_meta(key, value, updated_at) VALUES(?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (
            _IDEMPOTENCY_PRIVACY_MARKER_KEY,
            _IDEMPOTENCY_PRIVACY_PENDING_VALUE,
            utc_now(),
        ),
    )
    return True


def _migrate_private_reminder_entities(conn: sqlite3.Connection) -> dict[str, int]:
    """Move only unambiguous legacy reminders from a shared tenant to their person.

    The reminder text never leaves SQLite and is never selected here.  Rows with
    graph/content dependencies stay in place: every generic graph reader hides
    them, while rewriting their ownership would make those dependencies lie.
    """

    # A short-lived WIP build installed this trigger.  It erased the very
    # convenience pointer needed to recognise a dependent KO/Inbox as private.
    # Retire it before marker backfill can fire it on a reopened database.
    conn.execute("DROP TRIGGER IF EXISTS private_entity_owner_clear_public_pointers")

    now = utc_now()
    conn.execute(
        """INSERT OR IGNORE INTO private_entity_owners(
               entity_id, person_id, privacy_kind, created_at)
           SELECT t.entity_id, substr(t.source, length('reminder:') + 1), 'reminder', ?
             FROM entity_time t
            WHERE t.source LIKE 'reminder:%'""",
        (now,),
    )
    # Pre-isolation merges may have deleted the source time row after folding its
    # private name into the target aliases.  Parse bounded JSON rather than
    # matching one serializer's whitespace: ``"source" : "reminder:bob"`` is
    # the same provenance as the compact form.  Ambiguous reminder-bearing
    # transfers still receive an owner marker with an empty person, which hides
    # both endpoints from every generic reader without guessing ownership.
    merge_histories = conn.execute(
        """SELECT source_entity_id, target_entity_id, transfer_json
             FROM entity_merge_history"""
    ).fetchall()
    for history in merge_histories:
        is_private, person_id = _merge_transfer_reminder_owner(history["transfer_json"])
        if not is_private:
            continue
        for entity_id in (history["source_entity_id"], history["target_entity_id"]):
            conn.execute(
                """INSERT OR IGNORE INTO private_entity_owners(
                       entity_id, person_id, privacy_kind, created_at)
                   SELECT id, ?, 'reminder', ? FROM entities WHERE id=?""",
                (person_id, now, str(entity_id)),
            )
    conn.execute(
        """WITH RECURSIVE private_chain(entity_id, person_id) AS (
               SELECT entity_id, person_id FROM private_entity_owners
               UNION
               SELECT e.merged_into_id, private_chain.person_id
                 FROM private_chain
                 JOIN entities e ON e.id=private_chain.entity_id
                WHERE e.merged_into_id IS NOT NULL AND e.merged_into_id<>''
           )
           INSERT OR IGNORE INTO private_entity_owners(
               entity_id, person_id, privacy_kind, created_at)
           SELECT e.id, private_chain.person_id, 'reminder', ?
             FROM private_chain JOIN entities e ON e.id=private_chain.entity_id""",
        (now,),
    )

    candidates = conn.execute(
        """SELECT e.id AS entity_id, e.user_id AS old_user_id, e.name AS entity_name,
                  substr(t.source, length('reminder:') + 1) AS person_id
             FROM entity_time t
             JOIN entities e ON e.id=t.entity_id AND e.user_id=t.user_id
            WHERE t.source LIKE 'reminder:%'
              AND length(t.source)>length('reminder:')
              AND e.entity_type='event' AND e.deleted_at IS NULL
              AND e.canonical=1 AND e.merged_into_id IS NULL
            ORDER BY e.id"""
    ).fetchall()
    report = {"matched": len(candidates), "migrated": 0, "ambiguous": 0}
    candidate_user_ids = {str(row["old_user_id"]) for row in candidates}
    candidate_tokens_by_user: dict[str, dict[str, set[str]]] = {}
    for candidate in candidates:
        candidate_user = str(candidate["old_user_id"])
        candidate_id = str(candidate["entity_id"])
        tokens = candidate_tokens_by_user.setdefault(candidate_user, {})
        for token in (candidate_id, str(candidate["entity_name"] or "")):
            if token:
                tokens.setdefault(token, set()).add(candidate_id)
    historical_knowledge_entity_refs: set[tuple[str, str]] = set()
    unreadable_knowledge_history_users: set[str] = set()
    if candidate_user_ids:
        user_placeholders = ",".join("?" for _ in candidate_user_ids)
        version_rows = conn.execute(
            f"""SELECT user_id, snapshot_json FROM knowledge_object_versions
                 WHERE user_id IN ({user_placeholders})""",  # nosec B608 - generated placeholders only
            tuple(sorted(candidate_user_ids)),
        ).fetchall()
        for version_row in version_rows:
            version_user = str(version_row["user_id"] or "")
            snapshot = _bounded_knowledge_version_snapshot(version_row["snapshot_json"])
            if snapshot is None or str(snapshot.get("user_id") or "") != version_user:
                unreadable_knowledge_history_users.add(version_user)
                continue
            entity_ref = snapshot.get("entity_id")
            if entity_ref not in (None, "") and not isinstance(entity_ref, str):
                unreadable_knowledge_history_users.add(version_user)
                continue
            matches = _snapshot_private_token_matches(
                snapshot,
                candidate_tokens_by_user.get(version_user, {}),
                {
                    str(candidate["entity_id"])
                    for candidate in candidates
                    if str(candidate["old_user_id"]) == version_user
                },
            )
            if matches is None:
                unreadable_knowledge_history_users.add(version_user)
                continue
            historical_knowledge_entity_refs.update((version_user, entity_id) for entity_id in matches)

    dependency_queries = (
        "SELECT 1 FROM knowledge_objects WHERE entity_id=? LIMIT 1",
        "SELECT 1 FROM inbox WHERE suggested_entity_id=? LIMIT 1",
        "SELECT 1 FROM knowledge_entity_links WHERE entity_id=? LIMIT 1",
        "SELECT 1 FROM relations WHERE source_entity_id=? OR target_entity_id=? LIMIT 1",
        "SELECT 1 FROM relation_revisions WHERE source_entity_id=? OR target_entity_id=? LIMIT 1",
        "SELECT 1 FROM relation_candidates WHERE source_entity_id=? OR target_entity_id=? LIMIT 1",
        "SELECT 1 FROM entity_resolution_candidates WHERE entity_a_id=? OR entity_b_id=? LIMIT 1",
        "SELECT 1 FROM entity_merge_history WHERE source_entity_id=? OR target_entity_id=? LIMIT 1",
        "SELECT 1 FROM entities WHERE merged_into_id=? LIMIT 1",
        "SELECT 1 FROM feedback WHERE target_id=? LIMIT 1",
        "SELECT 1 FROM feedback_state WHERE target_id=? LIMIT 1",
        # Suggestions/notes are full model-produced copies.  A pre-isolation
        # Inbox row may carry only the reminder id/name inside JSON while its
        # typed suggested_entity_id is empty.  Moving the entity would make that
        # private reference foreign and invisible to ordinary tenant joins.
        """SELECT 1 FROM entities private_entity
             WHERE private_entity.id=? AND EXISTS (
               SELECT 1 FROM inbox i WHERE i.user_id=private_entity.user_id AND (
                 instr(COALESCE(i.suggested_tags_json,''), private_entity.id)>0
                 OR instr(COALESCE(i.suggestions_json,''), private_entity.id)>0
                 OR instr(COALESCE(i.classification_notes,''), private_entity.id)>0
                 OR (
                   private_entity.name<>'' AND (
                     jericho_private_identity_match(
                         COALESCE(i.suggested_tags_json,''), private_entity.name)=1
                     OR jericho_private_identity_match(
                         COALESCE(i.suggestions_json,''), private_entity.name)=1
                     OR jericho_private_identity_match(
                         COALESCE(i.classification_notes,''), private_entity.name)=1
                   )
                 )
               )
             ) LIMIT 1""",
    )
    for candidate in candidates:
        entity_id = str(candidate["entity_id"])
        old_user_id = str(candidate["old_user_id"])
        person_id = str(candidate["person_id"])
        if person_id == old_user_id:
            continue
        if (
            not person_id
            or conn.execute("SELECT 1 FROM users WHERE id=? LIMIT 1", (person_id,)).fetchone() is None
        ):
            report["ambiguous"] += 1
            continue

        ambiguous = (
            old_user_id in unreadable_knowledge_history_users
            or (
                old_user_id,
                entity_id,
            )
            in historical_knowledge_entity_refs
        )
        for query in dependency_queries:
            parameter_count = query.count("?")
            if conn.execute(query, (entity_id,) * parameter_count).fetchone() is not None:
                ambiguous = True
                break
        if ambiguous:
            report["ambiguous"] += 1
            continue

        version_state = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(CASE
                            WHEN user_id=? AND json_valid(snapshot_json)
                            THEN CASE
                                   WHEN json_type(snapshot_json)='object'
                                    AND json_extract(snapshot_json,'$.id')=?
                                    AND json_extract(snapshot_json,'$.user_id')=?
                                   THEN 0 ELSE 1 END
                            ELSE 1 END) AS invalid
                 FROM entity_versions WHERE entity_id=?""",
            (old_user_id, entity_id, old_user_id, entity_id),
        ).fetchone()
        if version_state is None or int(version_state["invalid"] or 0):
            report["ambiguous"] += 1
            continue

        version_total = int(version_state["total"] or 0)
        updated_versions = conn.execute(
            """UPDATE entity_versions
                  SET user_id=?, snapshot_json=json_set(snapshot_json,'$.user_id',?)
                WHERE entity_id=? AND user_id=?""",
            (person_id, person_id, entity_id, old_user_id),
        ).rowcount
        updated_time = conn.execute(
            """UPDATE entity_time SET user_id=?
                WHERE entity_id=? AND user_id=? AND source=?""",
            (person_id, entity_id, old_user_id, f"reminder:{person_id}"),
        ).rowcount
        updated_entity = conn.execute(
            """UPDATE entities SET user_id=?
                WHERE id=? AND user_id=? AND entity_type='event'
                  AND deleted_at IS NULL AND canonical=1 AND merged_into_id IS NULL""",
            (person_id, entity_id, old_user_id),
        ).rowcount
        if updated_versions != version_total or updated_time != 1 or updated_entity != 1:
            raise sqlite3.IntegrityError("Private reminder migration lost its atomic precondition")
        report["migrated"] += 1

    return report


@lru_cache(maxsize=1)
def _canonical_relation_history_schema_sql() -> tuple[dict[str, str], dict[str, str]]:
    """Build owned-table and trigger contracts with this process's SQLite parser.

    ``sqlite_master.sql`` omits ``IF NOT EXISTS`` and normalises some DDL syntax.
    Compiling the synthetic empty core schema avoids a hand-maintained second
    copy of the security-critical DDL while still comparing the installed
    database with the exact definitions shipped by this build.
    """

    canonical = sqlite3.connect(":memory:")
    try:
        canonical.executescript(CORE_TABLE_SCHEMA)
        canonical.executescript(CORE_INDEX_SCHEMA)
        table_definitions = {
            str(row[0]): str(row[1] or "")
            for row in canonical.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
            if str(row[0]) in _RELATION_HISTORY_OWNED_TABLES
        }
        trigger_definitions = {
            str(row[0]): str(row[1] or "")
            for row in canonical.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
            if str(row[0]) in _RELATION_HISTORY_TRIGGER_TABLES
        }
    finally:
        canonical.close()
    missing = sorted(
        (_RELATION_HISTORY_OWNED_TABLES - table_definitions.keys())
        | (set(_RELATION_HISTORY_TRIGGER_TABLES) - trigger_definitions.keys())
    )
    if missing:
        raise RuntimeError(f"Core schema omits required relation-history DDL: {', '.join(missing)}")
    return table_definitions, trigger_definitions


def _relation_history_unique_index_contract(
    conn: sqlite3.Connection,
) -> dict[str, tuple[Any, ...]]:
    """Describe only conflict-producing indexes on guarded authority tables."""

    contract: dict[str, tuple[Any, ...]] = {}
    for table in sorted(_RELATION_HISTORY_UNIQUE_INDEX_TABLES):
        indexes = conn.execute(
            """SELECT name, origin, partial
                 FROM pragma_index_list(?)
                WHERE "unique"=1
                ORDER BY name""",
            (table,),
        ).fetchall()
        for index in indexes:
            name = str(index[0])
            definition = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (name,),
            ).fetchone()
            columns = tuple(
                tuple(row)
                for row in conn.execute(
                    """SELECT seqno, cid, name, desc, coll, key
                         FROM pragma_index_xinfo(?)
                        ORDER BY seqno""",
                    (name,),
                ).fetchall()
            )
            contract[name] = (
                table,
                str(index[1]),
                int(index[2]),
                str((definition[0] if definition else "") or ""),
                columns,
            )
    return contract


@lru_cache(maxsize=1)
def _canonical_relation_history_unique_index_contract() -> dict[str, tuple[Any, ...]]:
    canonical = sqlite3.connect(":memory:")
    try:
        canonical.executescript(CORE_TABLE_SCHEMA)
        canonical.executescript(CORE_INDEX_SCHEMA)
        contract = _relation_history_unique_index_contract(canonical)
    finally:
        canonical.close()
    if "uq_active_relation" not in contract:
        raise RuntimeError("Core schema omits the active relation uniqueness contract")
    return contract


def _upgrade_relation_history_31_to_32(conn: sqlite3.Connection) -> None:
    """Replace only the exact deployed v31 guards, preserving all evidence.

    The caller has already validated the complete v31 schema and lineage under
    ``BEGIN IMMEDIATE``. DDL remains in that same transaction: rollback restores
    the old table and triggers byte-for-byte if any v32 proof fails.
    """

    allowed_context_references = {
        "relations_revision_ai",
        "relations_revision_au",
        "relations_revision_bd",
    }
    unexpected_references = [
        str(row[0])
        for row in conn.execute(
            """SELECT name FROM sqlite_master
                 WHERE sql IS NOT NULL
                   AND name<>'relation_revision_context'
                   AND instr(lower(sql), 'relation_revision_context')>0"""
        ).fetchall()
        if str(row[0]) not in allowed_context_references
    ]
    if unexpected_references:
        raise UnsupportedSchemaVersionError(
            "Schema 31 relation context has unexpected dependencies; restore a verified backup"
        )

    observed_at: str | None = None
    for row in conn.execute(
        """SELECT value AS boundary FROM schema_meta
             WHERE key='relation_history_complete_from'
           UNION ALL
           SELECT recorded_at FROM relation_revisions
           UNION ALL
           SELECT created_at FROM entity_versions
           UNION ALL
           SELECT created_at FROM entity_merge_history
           UNION ALL
           SELECT undone_at FROM entity_merge_history WHERE undone_at IS NOT NULL"""
    ):
        raw_boundary = str(row[0] or "")
        try:
            boundary = normalize_known_at(raw_boundary, reject_future=False)
        except ValueError as exc:
            raise UnsupportedSchemaVersionError(
                "Schema 31 temporal authority is unreadable; restore a verified backup"
            ) from exc
        if observed_at is None or boundary > observed_at:
            observed_at = boundary
    if observed_at is None:
        raise UnsupportedSchemaVersionError(
            "Schema 31 relation history has no completeness boundary; restore a verified backup"
        )

    for trigger in sorted(_RELATION_HISTORY_TRIGGER_TABLES_V31):
        conn.execute(f'DROP TRIGGER "{trigger}"')  # nosec B608 - fixed allowlist
    conn.execute("DROP TABLE relation_revision_context")

    canonical_tables, canonical_triggers = _canonical_relation_history_schema_sql()
    conn.execute(canonical_tables["relation_revision_context"])
    conn.execute(
        """INSERT INTO relation_revision_context(
               singleton, batch_id, recorded_at, observed_at
           ) VALUES(1, '', '', ?)""",
        (observed_at,),
    )
    for trigger in sorted(_RELATION_HISTORY_TRIGGER_TABLES):
        conn.execute(canonical_triggers[trigger])


@lru_cache(maxsize=1)
def _canonical_audit_trigger_sql() -> dict[str, str]:
    """Compile the append-only and pending-migration guards from shipped DDL."""

    canonical = sqlite3.connect(":memory:")
    try:
        canonical.executescript(CORE_TABLE_SCHEMA)
        definitions = {
            str(row[0]): str(row[1] or "")
            for row in canonical.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
            if str(row[0]) in _AUDIT_APPEND_ONLY_TRIGGERS
        }
    finally:
        canonical.close()
    missing = _AUDIT_APPEND_ONLY_TRIGGERS - definitions.keys()
    if missing:
        raise RuntimeError(f"Core schema omits required audit DDL: {', '.join(sorted(missing))}")
    return definitions


def _validate_audit_append_only_guards(conn: sqlite3.Connection) -> None:
    canonical = _canonical_audit_trigger_sql()
    installed = {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name IN (?, ?, ?)",
            tuple(sorted(_AUDIT_APPEND_ONLY_TRIGGERS)),
        ).fetchall()
    }
    if installed != canonical:
        raise RuntimeError("Audit append-only guards are missing or altered")


def _legacy_audit_payload(
    raw: Any,
    *,
    key: bytes,
    user_exists: Any,
    id_exists: Any,
) -> dict[str, Any] | None:
    """Parse an old audit JSON cell without ever returning its original text."""

    if raw is None:
        return None
    text = str(raw)
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {"private_chars": len(text), "private_fields_count": 1}
    if isinstance(parsed, dict):
        return sanitize_audit_payload(
            parsed,
            key=key,
            user_exists=user_exists,
            id_exists=id_exists,
        )
    if isinstance(parsed, str):
        return sanitize_audit_payload(
            {"content": parsed},
            key=key,
            user_exists=user_exists,
            id_exists=id_exists,
        )
    if isinstance(parsed, list):
        return {"private_items_count": len(parsed)}
    return {"private_fields_count": 1}


def _sanitize_legacy_audit_log(conn: sqlite3.Connection) -> bool:
    """One-time local redaction of rows written before the complete sink guard.

    Audit is normally append-only.  Privacy is the one legitimate reason to
    rewrite historical rows: marker v2 still accepted unproven IPs/generated-ID
    shapes and plain content digests.  V3 replaces those with provenance-checked
    evidence and keyed references.
    The trigger removal, complete row projection, trigger recreation and pending
    marker share the surrounding schema transaction, so a crash exposes either
    the old guarded table or the fully redacted guarded table, never an unguarded
    midpoint.
    """

    marker = conn.execute(
        "SELECT value FROM schema_meta WHERE key=?", (_AUDIT_PRIVACY_MARKER_KEY,)
    ).fetchone()
    marker_value = str(marker[0]) if marker is not None else ""
    key_row = conn.execute("SELECT value FROM schema_meta WHERE key=?", (_AUDIT_PRIVACY_HMAC_KEY,)).fetchone()
    if marker_value in {_AUDIT_PRIVACY_MARKER_VALUE, _AUDIT_PRIVACY_PENDING_VALUE}:
        decode_audit_privacy_key(key_row[0] if key_row is not None else None)
        _validate_audit_append_only_guards(conn)
        return marker_value == _AUDIT_PRIVACY_PENDING_VALUE
    if marker_value and marker_value not in _AUDIT_PRIVACY_V1_VALUES | {_AUDIT_PRIVACY_PENDING_VALUE}:
        raise RuntimeError("Unknown audit privacy migration marker")

    if key_row is None:
        key_text = secrets.token_hex(32)
        conn.execute(
            "INSERT INTO schema_meta(key, value, updated_at) VALUES(?, ?, ?)",
            (_AUDIT_PRIVACY_HMAC_KEY, key_text, utc_now()),
        )
        privacy_key = decode_audit_privacy_key(key_text)
    else:
        privacy_key = decode_audit_privacy_key(key_row[0])

    def user_exists(candidate: str) -> bool:
        return conn.execute("SELECT 1 FROM users WHERE id=? LIMIT 1", (candidate,)).fetchone() is not None

    def id_exists(candidate: str, prefixes: frozenset[str]) -> bool:
        return audit_generated_id_exists(conn.execute, candidate, prefixes)

    # UPDATE alone is only a logical deletion: SQLite may leave the old record in
    # freed cell bytes.  secure_delete makes the rewrite overwrite those bytes;
    # the pre-rewrite WAL is truncated after this transaction commits below.
    conn.execute("PRAGMA secure_delete=ON")
    secure_delete = conn.execute("PRAGMA secure_delete").fetchone()
    if secure_delete is None or int(secure_delete[0]) != 1:
        raise RuntimeError("SQLite secure_delete is unavailable for audit privacy migration")

    trigger_sql = _canonical_audit_trigger_sql()
    for trigger in sorted(_AUDIT_APPEND_ONLY_TRIGGERS):
        conn.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')  # nosec B608 - fixed allowlist

    rows = conn.execute("SELECT * FROM audit_log").fetchall()
    for row in rows:
        safe_before = _legacy_audit_payload(
            row["before_json"],
            key=privacy_key,
            user_exists=user_exists,
            id_exists=id_exists,
        )
        safe_after = _legacy_audit_payload(
            row["after_json"],
            key=privacy_key,
            user_exists=user_exists,
            id_exists=id_exists,
        )
        safe_target_type, safe_target_id = sanitize_audit_target(
            row["target_type"],
            row["target_id"],
            key=privacy_key,
            user_exists=user_exists,
            id_exists=id_exists,
        )
        safe_id = sanitize_audit_id(row["id"], key=privacy_key, id_exists=id_exists)
        conn.execute(
            """UPDATE audit_log
                  SET id=?, user_id=?, action=?, target_type=?, target_id=?,
                      before_json=?, after_json=?, ip_address=?, request_id=?, created_at=?
                WHERE id=?""",
            (
                safe_id,
                sanitize_audit_actor(row["user_id"], user_exists=user_exists),
                sanitize_audit_action(row["action"]),
                safe_target_type,
                safe_target_id,
                (
                    json.dumps(safe_before, ensure_ascii=False, sort_keys=True)
                    if safe_before is not None
                    else None
                ),
                (
                    json.dumps(safe_after, ensure_ascii=False, sort_keys=True)
                    if safe_after is not None
                    else None
                ),
                sanitize_audit_ip(row["ip_address"], key=privacy_key),
                sanitize_audit_request_id(row["request_id"], key=privacy_key),
                sanitize_audit_created_at(
                    row["created_at"],
                    fallback="1970-01-01T00:00:00+00:00",
                ),
                row["id"],
            ),
        )

    for trigger in sorted(_AUDIT_APPEND_ONLY_TRIGGERS):
        conn.execute(trigger_sql[trigger])
    _validate_audit_append_only_guards(conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value, updated_at) VALUES(?, ?, ?)",
        (_AUDIT_PRIVACY_MARKER_KEY, _AUDIT_PRIVACY_PENDING_VALUE, utc_now()),
    )
    return True


def _relation_history_artifacts_present(conn: sqlite3.Connection, *, schema_meta_preexisting: bool) -> bool:
    """Recognise schema-31 authority before any idempotent DDL can rewrite it."""

    for kind, name in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')"
    ).fetchall():
        if str(kind) == "table" and str(name) in {
            "relation_revisions",
            "relation_revision_context",
        }:
            return True
        if str(kind) == "trigger" and str(name) in _RELATION_HISTORY_TRIGGER_TABLES:
            return True
    return bool(
        schema_meta_preexisting
        and conn.execute("SELECT 1 FROM schema_meta WHERE key='relation_history_complete_from'").fetchone()
    )


def _relation_history_lineage_problems(conn: sqlite3.Connection) -> list[str]:
    """Audit both sides and the state machine of relation history.

    Diagnostics intentionally identify only the broken invariant.  Relation IDs
    and tenant IDs are private archive data and must not escape into startup logs
    or exception telemetry.
    """

    snapshot_match = """history.user_id IS current.user_id
                     AND history.source_entity_id IS current.source_entity_id
                     AND history.target_entity_id IS current.target_entity_id
                     AND history.relation_type IS current.relation_type
                     AND history.weight IS current.weight
                     AND history.metadata_json IS current.metadata_json
                     AND history.created_at IS current.created_at
                     AND history.deleted_at IS current.deleted_at
                     AND history.valid_from IS current.valid_from
                     AND history.valid_to IS current.valid_to
                     AND history.invalidated_at IS current.invalidated_at
                     AND history.superseded_by IS current.superseded_by"""
    latest = """WITH latest_keys AS (
                     SELECT relation_id, MAX(revision) AS revision
                     FROM relation_revisions
                     GROUP BY relation_id
                 ), latest_history AS (
                     SELECT history.*
                     FROM relation_revisions AS history
                     JOIN latest_keys AS key
                       ON key.relation_id=history.relation_id
                      AND key.revision=history.revision
                 )"""
    problems: list[str] = []

    current_mismatch = conn.execute(
        f"""{latest}
            SELECT 1
            FROM relations AS current
            LEFT JOIN latest_history AS history ON history.relation_id=current.id
            WHERE history.relation_id IS NULL
               OR history.present<>1
               OR NOT ({snapshot_match})
            LIMIT 1"""  # nosec B608 - fixed internal invariant fragment
    ).fetchone()
    if current_mismatch is not None:
        problems.append("current relation projection has incomplete lineage")

    present_mismatch = conn.execute(
        f"""{latest}
            SELECT 1
            FROM latest_history AS history
            LEFT JOIN relations AS current ON current.id=history.relation_id
            WHERE history.present=1
              AND (current.id IS NULL OR NOT ({snapshot_match}))
            LIMIT 1"""  # nosec B608 - fixed internal invariant fragment
    ).fetchone()
    if present_mismatch is not None:
        problems.append("latest present relation history is absent from current projection")

    tombstone_with_current = conn.execute(
        f"""{latest}
            SELECT 1
            FROM latest_history AS history
            JOIN relations AS current ON current.id=history.relation_id
            WHERE history.present=0
            LIMIT 1"""  # nosec B608 - fixed internal invariant fragment
    ).fetchone()
    if tombstone_with_current is not None:
        problems.append("latest relation tombstone still has a current projection")

    audit = conn.execute(
        """WITH ordered AS (
               SELECT relation_id, revision, event_seq, present, operation, user_id,
                      ROW_NUMBER() OVER (
                          PARTITION BY relation_id ORDER BY revision
                      ) AS expected_revision,
                      LAG(event_seq) OVER (
                          PARTITION BY relation_id ORDER BY revision
                      ) AS previous_event_seq,
                      LAG(present) OVER (
                          PARTITION BY relation_id ORDER BY revision
                      ) AS previous_present,
                      LAG(user_id) OVER (
                          PARTITION BY relation_id ORDER BY revision
                      ) AS previous_user_id
               FROM relation_revisions
           )
           SELECT
               MAX(CASE
                   WHEN revision<>expected_revision
                     OR (previous_event_seq IS NOT NULL AND event_seq<=previous_event_seq)
                   THEN 1 ELSE 0 END) AS broken_sequence,
               MAX(CASE
                   WHEN previous_user_id IS NOT NULL AND user_id IS NOT previous_user_id
                   THEN 1 ELSE 0 END) AS broken_owner,
               MAX(CASE
                   WHEN expected_revision=1
                     AND (present<>1 OR operation NOT IN ('insert', 'migration_baseline'))
                   THEN 1
                   WHEN expected_revision>1 AND operation='migration_baseline'
                   THEN 1
                   WHEN expected_revision>1 AND operation='insert'
                     AND (present<>1 OR previous_present<>0)
                   THEN 1
                   WHEN expected_revision>1 AND operation='update'
                     AND (present<>1 OR previous_present<>1)
                   THEN 1
                   WHEN expected_revision>1 AND operation='delete'
                     AND (present<>0 OR previous_present<>1)
                   THEN 1
                   ELSE 0 END) AS broken_state
           FROM ordered"""
    ).fetchone()
    if audit is not None:
        if bool(audit["broken_sequence"]):
            problems.append("relation revision sequence has gaps or reordered events")
        if bool(audit["broken_owner"]):
            problems.append("relation history owner continuity is broken")
        if bool(audit["broken_state"]):
            problems.append("relation revision presence sequence is inconsistent")

    timestamp_invalid = False
    for row in conn.execute("SELECT DISTINCT recorded_at FROM relation_revisions").fetchall():
        raw_timestamp = str(row[0] or "")
        try:
            canonical_timestamp = normalize_known_at(raw_timestamp, reject_future=False)
        except ValueError:
            timestamp_invalid = True
            break
        if raw_timestamp != canonical_timestamp:
            timestamp_invalid = True
            break
    if timestamp_invalid:
        problems.append("relation history contains a non-canonical recorded_at")
    else:
        decreasing_timestamp = conn.execute(
            """WITH ordered AS (
                   SELECT recorded_at,
                          LAG(recorded_at) OVER (ORDER BY event_seq) AS previous_recorded_at
                     FROM relation_revisions
               )
               SELECT 1 FROM ordered
                WHERE previous_recorded_at IS NOT NULL
                  AND recorded_at < previous_recorded_at
                LIMIT 1"""
        ).fetchone()
        if decreasing_timestamp is not None:
            problems.append("relation history recorded_at decreases across event order")
        reused_boundary = conn.execute(
            """WITH ordered AS (
                   SELECT recorded_at, batch_id,
                          LAG(recorded_at) OVER (ORDER BY event_seq) AS previous_recorded_at,
                          LAG(batch_id) OVER (ORDER BY event_seq) AS previous_batch_id
                     FROM relation_revisions
               )
               SELECT 1 FROM ordered
                WHERE recorded_at=previous_recorded_at
                  AND batch_id IS NOT previous_batch_id
                LIMIT 1"""
        ).fetchone()
        if reused_boundary is not None:
            problems.append("relation history reuses a transaction boundary across batches")

    inconsistent_batch = conn.execute(
        """SELECT 1 FROM relation_revisions
           GROUP BY batch_id
          HAVING batch_id='' OR COUNT(DISTINCT recorded_at)<>1
           LIMIT 1"""
    ).fetchone()
    if inconsistent_batch is not None:
        problems.append("relation history batch has inconsistent transaction time")
    return problems


def iso_date(value: Any) -> str | None:
    """Привести дату документа к `ГГГГ-ММ-ДД` или вернуть None.

    Строки берутся из бумаги как есть, поэтому здесь три работы сразу: узнать форму,
    отбросить не-даты и отбросить невозможные даты.

    Замерено на архиве владельца (3180 значений у 630 объектов): 2537 в форме
    дд.мм.гггг, 345 уже в ISO, 223 — это ВРЕМЯ («1:25»), остальное мусор, включая
    «00.00.0000». Время и мусор обязаны отсеиваться здесь, а не попадать в диапазон
    случайно: иначе фильтр «за март 2023» вернёт документы, в которых нет ни одной
    мартовской даты, и человек перестанет ему верить.

    Год ограничен снизу: «01.01.0001» — это не дата документа, а артефакт разбора.
    """
    if value is None:
        return None
    text = str(value).strip()
    match = _ISO_DATE_RE.match(text)
    if match:
        year, month, day = (int(part) for part in match.groups())
    else:
        match = _DMY_DATE_RE.match(text)
        if not match:
            return None
        day, month, year = (int(part) for part in match.groups())
    if not (1900 <= year <= 2200 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    try:
        date(year, month, day)
    except ValueError:
        # 31 февраля и подобное: строка выглядит датой, датой не являясь.
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


@contextmanager
def guarded_storage_transaction(
    storage: Any,
    *,
    before_commit: Callable[[], None],
    lock_timeout_sec: float,
    after_commit: Callable[[], None] | None = None,
    after_rollback: Callable[[], None] | None = None,
) -> Iterator[sqlite3.Connection]:
    """Own one bounded outer commit without changing FridayStorage's surface.

    V12 publication needs a deadline-aware Python/SQLite writer admission and a
    final callback immediately before the durable commit.  Keeping this as a
    module function preserves the long-standing ``FridayStorage.transaction()``
    signature used by legacy callers and its audited public method inventory.
    """

    if _GUARDED_TRANSACTION_CONTEXT.get() is not None:
        raise RuntimeError("guarded transaction context is already active")
    timeout = max(0.0, float(lock_timeout_sec))
    lock_started = time.monotonic()
    acquired = storage._write_lock.acquire(timeout=timeout)  # noqa: SLF001
    if not acquired:
        raise TimeoutError("storage writer lock deadline expired")
    conn: sqlite3.Connection | None = None
    old_busy_timeout: int | None = None
    token = None
    try:
        conn = storage.conn
        if conn.in_transaction:
            raise RuntimeError("guarded transaction requires the outer commit boundary")
        old_busy_row = conn.execute("PRAGMA busy_timeout").fetchone()
        old_busy_timeout = int(old_busy_row[0]) if old_busy_row is not None else 10_000
        remaining = max(0.0, timeout - (time.monotonic() - lock_started))
        guarded_busy_timeout = max(0, min(old_busy_timeout, int(remaining * 1000)))
        conn.execute(f"PRAGMA busy_timeout={guarded_busy_timeout}")  # nosec B608 - bounded int
        token = _GUARDED_TRANSACTION_CONTEXT.set((storage, before_commit, after_commit, after_rollback))
        try:
            with storage.transaction() as guarded_conn:
                yield guarded_conn
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).casefold():
                raise TimeoutError("storage SQLite writer deadline expired") from exc
            raise
    finally:
        if token is not None:
            _GUARDED_TRANSACTION_CONTEXT.reset(token)
        if conn is not None and old_busy_timeout is not None:
            with suppress(sqlite3.Error):
                conn.execute(f"PRAGMA busy_timeout={old_busy_timeout}")  # nosec B608 - prior int
        storage._write_lock.release()  # noqa: SLF001


class CoreMixin(StorageShared):
    def __init__(self, settings: FridaySettings) -> None:
        self.settings = settings
        self._db_path = settings.database_path
        # Connection-per-thread: no two threads ever share one sqlite3.Connection,
        # so cursors are never stepped concurrently on a single connection (the
        # half-written-read hazard). WAL then gives many concurrent readers plus a
        # single writer across the separate connections. ``threading.local`` holds
        # each thread's connection; the registry lets close() shut every thread's
        # connection down (e.g. before a restore swaps the database file), and the
        # generation counter invalidates the per-thread caches after close() so the
        # next use transparently reopens.
        self._local = threading.local()
        self._registry_lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._generation = 0
        # Writers serialise through this process-wide lock, held for the whole
        # transaction() block. Reads never take it, so the WAL many-reader win is
        # preserved; writers, however, keep the single-writer invariant in Python
        # (no two BEGIN IMMEDIATE ever contend at the SQLite level, so a writer can
        # never surface "database is locked" from internal contention). close()
        # also takes it, so a shutdown cannot close a connection out from under an
        # in-flight write transaction on a background-worker thread — restoring the
        # drain-before-close guarantee the old shared RLock provided. Reentrant so
        # nested transaction() on one thread does not self-deadlock.
        self._write_lock = threading.RLock()
        # Schema creation/migration runs exactly once, on the first connection,
        # behind this lock; every other connection only opens and configures itself.
        self._init_lock = threading.Lock()
        self._schema_ready = False
        self._fts_available = True
        # Set by close(final=True). Distinct from a plain close(), which must stay
        # reopenable — restore_backup swaps the database file and then carries on.
        self._shut_down = False

    @property
    def conn(self) -> sqlite3.Connection:
        local = self._local
        cached = getattr(local, "conn", None)
        if cached is not None and getattr(local, "generation", None) == self._generation:
            return cached
        if self._shut_down:
            # A thread that outlived shutdown asking for a connection is not a
            # request to reopen the database; it is work that should have finished.
            # Silently handing it a fresh connection is how writes escaped past the
            # released process lease.
            raise StorageClosedError("Storage is shut down; this connection will not be reopened")
        connection = self._open()
        with self._registry_lock:
            self._connections.append(connection)
            local.conn = connection
            local.generation = self._generation
        return connection

    @staticmethod
    def _is_sqlite_busy(exc: sqlite3.OperationalError) -> bool:
        message = str(exc).lower()
        return "database is locked" in message or "database is busy" in message

    def _open(self) -> sqlite3.Connection:
        """Open and migrate SQLite, tolerating concurrent first-start workers.

        SQLite's connection timeout does not consistently cover
        ``PRAGMA journal_mode=WAL`` or schema initialization.  A second process
        starting at the same instant can therefore receive ``database is
        locked`` before normal busy handling applies.  Retrying the complete,
        idempotent initialization avoids exposing a half-configured connection.
        """

        deadline = time.monotonic() + 15.0
        delay = 0.025
        while True:
            try:
                return self._open_once()
            except sqlite3.OperationalError as exc:
                if not self._is_sqlite_busy(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 1.8, 0.5)

    def _open_once(self) -> sqlite3.Connection:
        must_exist = bool(self.settings.database_must_exist)
        required_fingerprint: tuple[int, int] | None = None
        if must_exist:
            # load_settings() validates the selected image, but validation and
            # first use are separated by startup work.  Plain sqlite3.connect()
            # carries O_CREAT semantics and would silently replace a database
            # removed in that window with an empty one.  URI ``mode=rw`` is the
            # SQLite-level no-create guarantee at the actual open boundary.  It
            # does not, however, reject an existing zero-byte image: SQLite is
            # willing to initialise that file.  Revalidate size/inode here and
            # again after connecting, before any PRAGMA or migration may write.
            required_fingerprint = _required_database_fingerprint(self._db_path)
            restrict_sqlite_files(self._db_path)
            database_target = f"{self._db_path.resolve(strict=False).as_uri()}?mode=rw"
        else:
            prepare_private_sqlite(self._db_path)
            database_target = str(self._db_path)
        # check_same_thread=False so close() can shut every thread's connection
        # down from the shutdown/restore thread; cross-thread *use* is prevented
        # structurally (the conn property only ever hands back the caller thread's
        # own connection via threading.local), not by the sqlite thread check.
        conn = sqlite3.connect(
            database_target,
            check_same_thread=False,
            timeout=10.0,
            uri=must_exist,
        )
        conn.row_factory = sqlite3.Row
        try:
            if must_exist:
                if _required_database_fingerprint(self._db_path) != required_fingerprint:
                    raise sqlite3.OperationalError("required Friday database changed during open")
                if not _required_database_has_friday_schema(conn):
                    raise sqlite3.OperationalError(
                        "required Friday database has no recognizable Friday schema"
                    )
            # Per-connection configuration — applied to EVERY thread's connection.
            # busy_timeout must precede WAL negotiation; sqlite3's connect timeout
            # alone does not reliably protect that PRAGMA on every platform. WAL is
            # persistent in the database header (idempotent to re-issue), but
            # foreign_keys and synchronous are per-connection and non-persistent, so
            # every connection must set them or FK enforcement silently disappears.
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=WAL")
            # Existing sidecars may predate the owner-only policy.  A new WAL/SHM
            # inherits the pre-secured main database mode; this second pass also
            # tightens legacy sidecars before normal application reads begin.
            restrict_sqlite_files(self._db_path)
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            # SQLite's lower()/NOCASE only fold ASCII; tags and entity names are
            # frequently Cyrillic, so Unicode-correct case-insensitive queries
            # (browse-by-tag) go through Python's casefold instead. User functions
            # are registered per connection.
            conn.create_function(
                "jericho_casefold",
                1,
                _unicode_casefold,
                deterministic=True,
            )
            conn.create_function(
                "jericho_private_identity_tokens",
                2,
                _private_identity_tokens_json,
                deterministic=True,
            )
            conn.create_function(
                "jericho_private_identity_match",
                2,
                _private_identity_match,
                deterministic=True,
            )
            # Даты из документов извлечены и лежат в метаданных СЫРЫМИ строками — так,
            # как они написаны в бумаге. Замерено на архиве владельца: 3180 значений у
            # 630 объектов, из них 2537 в форме дд.мм.гггг, 345 в ISO, 223 — вообще
            # время («1:25»), остальное мусор вроде «00.00.0000». Фильтровать по такому
            # можно только приведя к одной форме, а приводить надо В SQL: иначе условие
            # не построить, и работа остаётся лежать в JSON-блобе без применения.
            conn.create_function(
                "jericho_iso_date",
                1,
                iso_date,
                deterministic=True,
            )
            # Schema creation/migration/FTS is applied exactly once, by the first
            # connection; later connections open against the already-migrated file.
            self._ensure_schema(conn)
            # UDF-backed views/rebuild triggers are connection-local by design:
            # persistent SQLite schema must remain reparsable by offline tools.
            # Every thread owns a distinct connection and therefore installs its
            # own TEMP privacy runtime before any application query can run.
            self._execute_statements(conn, PRIVATE_MATERIAL_RUNTIME_SCHEMA)
            # A raw/out-of-process writer owns only persistent invalidators.  A
            # later connection must heal under a database write lock *after* its
            # Unicode TEMP runtime exists and before any reader can observe it.
            # Recheck after BEGIN IMMEDIATE: another opener may have repaired the
            # same global state while this connection waited for the lock.
            conn.execute("BEGIN IMMEDIATE")
            _invalidate_private_material_on_rule_change(conn)
            material_rebuilt = False
            derivative_rebuilt = False
            material_state = conn.execute(
                "SELECT valid FROM main.private_entity_material_cache_state WHERE singleton=1"
            ).fetchone()
            derivative_state = conn.execute(
                "SELECT valid FROM main.private_entity_material_derivative_state WHERE singleton=1"
            ).fetchone()
            if material_state is None or derivative_state is None:
                raise sqlite3.DatabaseError("Private material authority state is missing")
            if int(material_state[0]) != 1:
                self._execute_statements(conn, PRIVATE_MATERIAL_CACHE_REBUILD_SQL)
                material_rebuilt = True
                derivative_rebuilt = True
            elif int(derivative_state[0]) != 1:
                self._execute_statements(conn, PRIVATE_DERIVATIVE_CACHE_REBUILD_SQL)
                derivative_rebuilt = True
            if material_rebuilt or derivative_rebuilt:
                _validate_private_material_cache(
                    conn,
                    fresh_entity_rebuild_from_live=material_rebuilt,
                    fresh_derivative_rebuild_from_live=derivative_rebuilt,
                )
            conn.commit()
            _install_private_material_authorizer(conn)
            # The core migration transiently lowers busy_timeout (a PRAGMA embedded
            # in CORE_SCHEMA); restore the uniform value on the migrating connection.
            conn.execute("PRAGMA busy_timeout=10000")
            return conn
        except BaseException:
            conn.close()
            raise

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create/upgrade the schema exactly once, behind a barrier.

        The first connection runs the (idempotent) migration; no connection may
        run application queries until it has committed, so the double-checked
        ``_init_lock`` both serialises the single migration and makes later
        openers wait for it. A busy failure propagates so ``_open``'s retry loop
        re-runs the still-idempotent migration on a fresh connection.
        """

        if self._schema_ready:
            return
        with self._init_lock:
            if self._schema_ready:
                return
            self._migrate_schema(conn)
            self._schema_ready = True

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        # Create/recognize tables first, then add legacy columns, and only then
        # create indexes that reference those columns. Keep this core migration in
        # one explicit transaction: sqlite3's executescript() commits an existing
        # transaction implicitly, which otherwise leaves half-applied DDL/backfills
        # after a migration failure.
        # The legacy reconstruction is an UPGRADE step, and it ran on every
        # process's first connection instead. That is how a corrected backfill keeps
        # re-firing on data that is already right — and it re-scanned every entity
        # row on every start for nothing. The marker is only written after the whole
        # migration transaction commits, so version == SCHEMA_VERSION already proves
        # the backfill ran; an absent or lower marker still runs it.
        try:
            conn.execute("BEGIN IMMEDIATE")
            # Read every migration decision only AFTER acquiring the cross-process
            # write lock. Another worker may have completed schema 30 -> 31 while
            # this connection was waiting; a stale pre-lock marker would mistake
            # its legitimate new artifacts for corruption.
            schema_meta_preexisting = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
                ).fetchone()
                is not None
            )
            previous_schema_version: str | None = None
            parsed_version: int | None = None
            # Separate from the schema marker ON PURPOSE. The core migration
            # commits first, then the FTS phase runs and commits second. This
            # marker is written only after that phase commits, so a crash between
            # them reopens as "index not built by this version" and heals.
            fts_build_marker: str | None = None
            if schema_meta_preexisting:
                row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
                previous_schema_version = str(row[0]).strip() if row else None
                marker_row = conn.execute("SELECT value FROM schema_meta WHERE key='fts_build'").fetchone()
                fts_build_marker = str(marker_row[0]).strip() if marker_row else None
                # Fail closed before executing any DDL. A newer or malformed
                # marker must never become a best-effort downgrade.
                if previous_schema_version:
                    try:
                        parsed_version = int(previous_schema_version)
                    except ValueError as exc:
                        raise UnsupportedSchemaVersionError("Invalid database schema version marker") from exc
                    if parsed_version < 0 or parsed_version > SCHEMA_VERSION:
                        raise UnsupportedSchemaVersionError(
                            f"Database schema version {parsed_version} is not supported by "
                            f"this Friday build (maximum {SCHEMA_VERSION})"
                        )

            # Probe under the same lock and before this connection creates FTS
            # DDL. Asking afterwards always answers "yes" and can skip the rebuild
            # of an external-content index that started empty.
            fts_preexisting = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
                ).fetchone()
                is not None
            )
            raw_fts_preexisting = (
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_fts'").fetchone()
                is not None
            )
            messages_fts_preexisting = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
                ).fetchone()
                is not None
            )
            already_current = previous_schema_version is not None and (
                previous_schema_version.strip() == str(SCHEMA_VERSION)
            )
            # A missing/stale marker must not turn an already-authoritative
            # schema-31 database back into a legacy migration candidate. Doing
            # so would recreate schema_meta, move its immutable completeness
            # floor and let IF NOT EXISTS conceal counterfeit capture triggers.
            if (parsed_version is None or parsed_version < 31) and _relation_history_artifacts_present(
                conn,
                schema_meta_preexisting=schema_meta_preexisting,
            ):
                raise UnsupportedSchemaVersionError(
                    "Database schema marker predates installed relation-history artifacts; "
                    "restore a verified backup"
                )
            # A schema-31 marker plus its immutable floor is a promise that the
            # whole capture/protection mechanism and its evidence still exist.
            # Validate it under the migration write lock and BEFORE idempotent DDL
            # can conceal what was missing or race a concurrent relation write.
            if parsed_version is not None and parsed_version >= 31:
                self._validate_relation_history_schema(conn, parsed_version)
                if parsed_version == 31:
                    _upgrade_relation_history_31_to_32(conn)
                    self._validate_relation_history_schema(conn, SCHEMA_VERSION)
            if parsed_version is not None and parsed_version >= 34:
                self._validate_file_source_alias_schema(conn)
            if parsed_version == 35:
                upgrade_obsidian_schema_35_to_36(conn)
            elif parsed_version is not None and parsed_version >= 36:
                # Obsidian's sub-schema remains v36 while the core database
                # advances independently. Never pass an unrelated core marker
                # into the exact Obsidian DDL validator.
                validate_obsidian_schema(conn)
            if parsed_version is not None and parsed_version >= INTERACTION_FAILURE_SCHEMA_VERSION:
                validate_interaction_failure_schema(conn)
            elif parsed_version is not None and parsed_version < INTERACTION_FAILURE_SCHEMA_VERSION:
                # A previous interrupted schema-37 attempt may have committed no
                # marker. Accept only its exact ownership shape before retrying
                # idempotent DDL; never let IF NOT EXISTS conceal a weaker table.
                validate_interaction_failure_schema(conn, required=False)
            self._execute_statements(conn, CORE_TABLE_SCHEMA)
            self._execute_statements(conn, OBSIDIAN_SCHEMA)
            self._execute_statements(conn, INTERACTION_FAILURE_SCHEMA)
            if not already_current:
                self._migrate_legacy_schema(conn)
                self._retire_outdated_indexes(conn)
            _validate_private_material_cache_pre_schema(conn)
            # Current-schema databases can still contain a pre-owner reminder
            # imported by an older build.  Its tenant move updates entity history
            # and identity; persisted privacy guards from the previous run would
            # correctly reject that at runtime but must not brick this audited
            # startup migration.  Validate the allowlist first, remove only our
            # known triggers, migrate under the schema write lock, then recreate
            # and exactly rebuild every guard/cache below before commit.
            _drop_private_material_runtime_triggers(conn)
            _migrate_private_reminder_entities(conn)
            # One dynamic fixed-point authority for direct reminder entities and
            # every bounded entity that copies their identity.  Public readers
            # reference this view instead of embedding an O(E²) recursive CTE in
            # each nested dependency predicate.
            self._execute_statements(conn, PRIVATE_MATERIAL_PERSISTENT_SCHEMA)
            self._execute_statements(conn, PRIVATE_MATERIAL_RUNTIME_SCHEMA)
            self._execute_statements(conn, PRIVATE_MATERIAL_CACHE_REBUILD_SQL)
            validate_obsidian_schema(conn)
            validate_interaction_failure_schema(conn)
            _validate_private_material_cache(
                conn,
                fresh_entity_rebuild_from_live=True,
                fresh_derivative_rebuild_from_live=True,
            )
            # Pre-release audit tables can predate ``request_id``.  The legacy
            # migration above adds that column; sanitising before it therefore
            # fails startup exactly on the databases this path exists to heal.
            # Keep the scrub in the same uncommitted migration transaction, but
            # only after the table has its current shape.
            audit_privacy_pending = _sanitize_legacy_audit_log(conn)
            idempotency_privacy_pending = _invalidate_legacy_idempotency_responses(conn)
            self._execute_statements(conn, CORE_INDEX_SCHEMA)
            self._validate_file_source_alias_schema(conn)
            if not already_current:
                # The marker is a durable proof, not an optimistic target. Run
                # the current contract again after every legacy/privacy step so
                # no later migration can damage history between the dedicated
                # 31→32 upgrade and publication of schema_version=32.
                self._validate_relation_history_schema(conn, SCHEMA_VERSION)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value, updated_at) VALUES('schema_version', ?, ?)",
                (str(SCHEMA_VERSION), utc_now()),
            )
            conn.commit()
            if audit_privacy_pending or idempotency_privacy_pending:
                # A committed pre-redaction/cache-invalidation WAL frame can
                # still be a byte-for-byte copy of personal content.  TRUNCATE
                # must succeed before either durable completion marker is
                # written; a busy reader therefore stops startup and leaves its
                # pending marker for a safe retry.
                checkpoint = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or int(checkpoint[0]) != 0:
                    raise sqlite3.OperationalError("Privacy migration WAL checkpoint is busy")
                conn.execute("BEGIN IMMEDIATE")
                if audit_privacy_pending:
                    conn.execute(
                        "UPDATE schema_meta SET value=?, updated_at=? WHERE key=?",
                        (_AUDIT_PRIVACY_MARKER_VALUE, utc_now(), _AUDIT_PRIVACY_MARKER_KEY),
                    )
                if idempotency_privacy_pending:
                    conn.execute(
                        "UPDATE schema_meta SET value=?, updated_at=? WHERE key=?",
                        (
                            _IDEMPOTENCY_PRIVACY_MARKER_VALUE,
                            utc_now(),
                            _IDEMPOTENCY_PRIVACY_MARKER_KEY,
                        ),
                    )
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

        # FTS is an optional, self-healing derivative index. Build it only after
        # the authoritative core schema/data transaction has committed, so an
        # unavailable FTS5 module can never roll back or partially expose personal
        # knowledge migration.
        # True whenever this build has not recorded a finished FTS build. Covers
        # the crash window above and every database written before the marker
        # existed. `integrity-check` cannot stand in for it: measured on SQLite,
        # an external-content index that is entirely EMPTY passes integrity-check
        # and matches nothing — the check verifies what the index claims against
        # itself, not against the content table it shadows.
        fts_unverified = fts_build_marker != str(SCHEMA_VERSION)
        try:
            conn.executescript(FTS_SCHEMA)
            knowledge_count = conn.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0]
            if knowledge_count and (not fts_preexisting or fts_unverified):
                # An external-content FTS table created after rows already exist
                # starts with an empty index. Rebuild before update triggers can
                # attempt to delete missing index entries.
                conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
            elif fts_preexisting and previous_schema_version != str(SCHEMA_VERSION):
                try:
                    conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('integrity-check')")
                except sqlite3.DatabaseError:
                    LOGGER.warning("Rebuilding an inconsistent knowledge FTS index")
                    conn.execute("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
            # Same contract for the source index: an external-content FTS table
            # created over existing rows starts EMPTY, and the update triggers would
            # then try to delete index entries that were never written.
            raw_count = conn.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0]
            if raw_count and (not raw_fts_preexisting or fts_unverified):
                conn.execute("INSERT INTO raw_fts(raw_fts) VALUES('rebuild')")
            # Same contract for chat history: messages predate messages_fts on any
            # schema < 20, so an empty external-content index must rebuild once.
            messages_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            if messages_count and (not messages_fts_preexisting or fts_unverified):
                conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            elif messages_fts_preexisting and previous_schema_version != str(SCHEMA_VERSION):
                try:
                    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")
                except sqlite3.DatabaseError:
                    LOGGER.warning("Rebuilding an inconsistent messages FTS index")
                    conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            # The vocabulary view, in its own try: losing spelling repair on an
            # SQLite without `fts5vocab` is a degradation, losing full-text search
            # with it would be an outage.
            try:
                conn.executescript(FTS_VOCAB_SCHEMA)
            except sqlite3.OperationalError as exc:
                LOGGER.info(
                    "fts5vocab is unavailable; spelling repair disabled (%s)",
                    type(exc).__name__,
                )
            # Last, and inside the same commit as the rebuilds it certifies.
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value, updated_at) VALUES('fts_build', ?, ?)",
                (str(SCHEMA_VERSION), utc_now()),
            )
        except sqlite3.OperationalError as exc:
            if self._is_sqlite_busy(exc):
                raise
            self._fts_available = False
            LOGGER.warning("SQLite FTS5 is unavailable; using LIKE search (%s)", type(exc).__name__)
        conn.commit()

    @staticmethod
    def _validate_relation_history_schema(conn: sqlite3.Connection, schema_version: int) -> None:
        """Reject a broken authoritative relation-history contract before DDL.

        ``CREATE ... IF NOT EXISTS`` is safe for derivative indexes, but not for
        capture or immutability triggers: recreating one cannot recover revisions
        lost while it was absent. A current schema marker therefore turns every
        item below into a required invariant, never an automatic repair target.
        """

        required_tables = {
            "relations",
            "relation_revisions",
            "relation_revision_context",
        }
        installed_table_sql = {
            str(row[0]): str(row[1] or "")
            for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
        }
        installed_tables = set(installed_table_sql)
        missing_tables = sorted(required_tables - installed_tables)

        installed_triggers = {
            str(row[0]): (str(row[1]), str(row[2] or ""))
            for row in conn.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        trigger_tables = (
            _RELATION_HISTORY_TRIGGER_TABLES_V31 if schema_version == 31 else _RELATION_HISTORY_TRIGGER_TABLES
        )
        missing_triggers = sorted(
            name
            for name, table in trigger_tables.items()
            if name not in installed_triggers or installed_triggers[name][0] != table
        )
        protected_trigger_tables = set(_RELATION_HISTORY_TRIGGER_TABLES.values())
        unexpected_triggers = sorted(
            name
            for name, (table, sql) in installed_triggers.items()
            if name not in trigger_tables
            and (
                table in protected_trigger_tables
                or "relation_history_complete_from" in sql.casefold()
                or re.search(
                    r"\b(?:relations|relation_revisions|relation_revision_context)\b",
                    sql,
                    flags=re.IGNORECASE,
                )
                is not None
            )
        )
        canonical_unique_indexes = _canonical_relation_history_unique_index_contract()
        installed_unique_indexes = _relation_history_unique_index_contract(conn)
        missing_unique_indexes = sorted(canonical_unique_indexes.keys() - installed_unique_indexes.keys())
        altered_unique_indexes = sorted(
            name
            for name in canonical_unique_indexes.keys() & installed_unique_indexes.keys()
            if installed_unique_indexes[name] != canonical_unique_indexes[name]
        )
        unexpected_unique_index_count = len(installed_unique_indexes.keys() - canonical_unique_indexes.keys())
        if schema_version == 31:
            altered_tables = sorted(
                name
                for name in _RELATION_HISTORY_SCHEMA_V31_TABLES
                if name in installed_table_sql
                and hashlib.sha256(installed_table_sql[name].encode("utf-8")).hexdigest()
                != _RELATION_HISTORY_SCHEMA_V31_SHA256[name]
            )
            altered_triggers = sorted(
                name
                for name, table in trigger_tables.items()
                if name in installed_triggers
                and installed_triggers[name][0] == table
                and hashlib.sha256(installed_triggers[name][1].encode("utf-8")).hexdigest()
                != _RELATION_HISTORY_SCHEMA_V31_SHA256[name]
            )
        else:
            canonical_tables, canonical_triggers = _canonical_relation_history_schema_sql()
            altered_table_names = {
                name
                for name in _RELATION_HISTORY_OWNED_TABLES
                if name in installed_table_sql and installed_table_sql[name] != canonical_tables[name]
            }
            if (
                "relations" in installed_table_sql
                and hashlib.sha256(installed_table_sql["relations"].encode("utf-8")).hexdigest()
                not in _RELATION_PROJECTION_SCHEMA_SHA256
            ):
                altered_table_names.add("relations")
            altered_tables = sorted(altered_table_names)
            altered_triggers = sorted(
                name
                for name, table in trigger_tables.items()
                if name in installed_triggers
                and installed_triggers[name][0] == table
                and installed_triggers[name][1] != canonical_triggers[name]
            )

        problems: list[str] = []
        if missing_tables:
            problems.append(f"missing tables: {', '.join(missing_tables)}")
        if altered_tables:
            problems.append(f"altered tables: {', '.join(altered_tables)}")
        if missing_triggers:
            problems.append(f"missing triggers: {', '.join(missing_triggers)}")
        if unexpected_triggers:
            # Names outside the shipped allowlist are attacker-controlled schema
            # text. Do not copy them into startup errors or telemetry.
            problems.append(f"unexpected triggers: {len(unexpected_triggers)}")
        if altered_triggers:
            problems.append(f"altered triggers: {', '.join(altered_triggers)}")
        if missing_unique_indexes:
            problems.append(f"missing unique indexes: {', '.join(missing_unique_indexes)}")
        if altered_unique_indexes:
            problems.append(f"altered unique indexes: {', '.join(altered_unique_indexes)}")
        if unexpected_unique_index_count:
            # Unknown index names and expressions are attacker-controlled schema
            # text. The count proves the conflict surface changed without
            # copying either value into startup errors or telemetry.
            problems.append(f"unexpected unique indexes: {unexpected_unique_index_count}")

        # Do not query a counterfeit shape. Missing constraints are safe to read,
        # but a same-name table may also omit/retype columns and turn diagnostics
        # into an unrelated sqlite OperationalError.
        if problems:
            detail = "; ".join(problems)
            raise UnsupportedSchemaVersionError(
                f"Schema {schema_version} relation history is incomplete ({detail}); "
                "restore a verified backup"
            )

        floor_row = conn.execute(
            "SELECT value, updated_at FROM schema_meta WHERE key='relation_history_complete_from'"
        ).fetchone()
        floor = str(floor_row[0] if floor_row else "")
        canonical_floor = ""
        if not floor.strip():
            problems.append("missing relation_history_complete_from floor")
        else:
            try:
                canonical_floor = normalize_known_at(floor, reject_future=False)
            except ValueError:
                problems.append("invalid relation_history_complete_from floor")
            else:
                if floor != canonical_floor:
                    problems.append("non-canonical relation_history_complete_from floor")
                elif str(floor_row[1] or "") != floor:
                    problems.append("relation_history_complete_from floor provenance is inconsistent")

        observed_at = ""
        if "relation_revision_context" in installed_tables:
            context_columns = (
                "singleton, batch_id, recorded_at"
                if schema_version == 31
                else "singleton, batch_id, recorded_at, observed_at"
            )
            context_rows = conn.execute(
                f"SELECT {context_columns} FROM relation_revision_context"  # nosec B608 - fixed by schema version
            ).fetchall()
            if len(context_rows) != 1 or tuple(context_rows[0][:3]) != (1, "", ""):
                problems.append("missing or dirty relation revision context singleton")
            elif schema_version >= 32:
                raw_observed_at = str(context_rows[0]["observed_at"] or "")
                try:
                    observed_at = normalize_known_at(raw_observed_at, reject_future=False)
                except ValueError:
                    problems.append("invalid relation history observed boundary")
                else:
                    if raw_observed_at != observed_at:
                        problems.append("non-canonical relation history observed boundary")
                    elif canonical_floor and observed_at < canonical_floor:
                        problems.append("relation history observed boundary precedes its floor")

        if {"relations", "relation_revisions"}.issubset(installed_tables):
            if canonical_floor:
                baseline_mismatch = conn.execute(
                    """SELECT 1 FROM relation_revisions
                       WHERE (operation='migration_baseline')
                             IS NOT (history_quality='migration_baseline')
                          OR (operation='migration_baseline' AND (
                                 recorded_at IS NOT ?
                              OR batch_id IS NOT 'migration:v31'
                              OR revision<>1
                              OR present<>1
                          ))
                       LIMIT 1""",
                    (canonical_floor,),
                ).fetchone()
                if baseline_mismatch is not None:
                    problems.append("migration baseline does not match relation history floor")
                violates_floor = conn.execute(
                    """SELECT 1 FROM relation_revisions
                        WHERE recorded_at < ?
                           OR (recorded_at = ? AND operation IS NOT 'migration_baseline')
                        LIMIT 1""",
                    (canonical_floor, canonical_floor),
                ).fetchone()
                if violates_floor is not None:
                    problems.append("relation history violates its completeness floor")
                if observed_at:
                    later_than_observed = conn.execute(
                        "SELECT 1 FROM relation_revisions WHERE recorded_at>? LIMIT 1",
                        (observed_at,),
                    ).fetchone()
                    if later_than_observed is not None:
                        problems.append("relation history exceeds its observed boundary")
            problems.extend(_relation_history_lineage_problems(conn))

        if problems:
            detail = "; ".join(problems)
            raise UnsupportedSchemaVersionError(
                f"Schema {schema_version} relation history is incomplete ({detail}); "
                "restore a verified backup"
            )

    @staticmethod
    def _execute_statements(conn: sqlite3.Connection, script: str) -> None:
        """Execute a plain SQL script without sqlite3's implicit commit.

        ``sqlite3.complete_statement`` handles multiline indexes, comments and
        trigger bodies (it only reports completion at the trigger's final
        ``END;``) while preserving the caller's explicit transaction.
        """

        pending = ""
        for line in script.splitlines(keepends=True):
            pending += line
            if not sqlite3.complete_statement(pending):
                continue
            statement = pending.strip()
            pending = ""
            if statement:
                conn.execute(statement)
        if pending.strip():
            raise sqlite3.OperationalError("Incomplete SQL statement in core schema")

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    @staticmethod
    def _validate_file_source_alias_schema(conn: sqlite3.Connection) -> None:
        """Fail closed on a schema-34 alias column/guard that was weakened."""

        columns = {str(row[1]): row for row in conn.execute("PRAGMA table_info(file_source_aliases)")}
        supplied = columns.get("supplied_filename")
        if (
            supplied is None
            or str(supplied[2]).upper() != "TEXT"
            or int(supplied[3]) != 1
            or str(supplied[4] or "") != "''"
        ):
            raise UnsupportedSchemaVersionError(
                "Schema 34 file source alias filename column is missing or weakened"
            )
        invalid = conn.execute(
            """SELECT 1 FROM file_source_aliases a
                WHERE length(a.supplied_filename)>260
                   OR instr(a.supplied_filename,'/')<>0
                   OR instr(a.supplied_filename,'\\')<>0
                   OR instr(a.supplied_filename,char(0))<>0
                   OR instr(a.supplied_filename,char(10))<>0
                   OR instr(a.supplied_filename,char(13))<>0
                   OR (substr(a.source_ref,1,20)='friday-message-name:'
                       AND a.supplied_filename='')
                   OR (a.supplied_filename<>''
                       AND substr(a.source_ref,1,14)<>'telegram-file:'
                       AND NOT (length(a.source_ref)=40
                                AND substr(a.source_ref,1,24)='friday-message-name:msg_'
                                AND substr(a.source_ref,25,16) NOT GLOB '*[^0-9a-f]*'))
                   OR (a.supplied_filename<>''
                       AND length(a.source_ref)=40
                       AND substr(a.source_ref,1,24)='friday-message-name:msg_'
                       AND (NOT EXISTS (
                           SELECT 1 FROM messages m
                           JOIN conversations c
                             ON c.id=m.conversation_id AND c.user_id=m.user_id
                           WHERE m.id=substr(a.source_ref,21,20)
                             AND m.user_id=a.user_id AND m.role='user'
                             AND m.content='Загружен документ: ' || a.supplied_filename
                             AND json_valid(m.metadata_json)
                             AND json_type(m.metadata_json,'$.synthetic_document_notice')='true'
                             AND json_array_length(
                                     m.metadata_json,
                                     '$.conversation_attachment_raw_ids'
                                 )=1
                             AND json_array_length(
                                     m.metadata_json,
                                     '$.conversation_uploaded_raw_ids'
                                 )=1
                             AND json_extract(
                                     m.metadata_json,
                                     '$.conversation_attachment_raw_ids[0]'
                                 )=a.raw_object_id
                             AND json_extract(
                                     m.metadata_json,
                                     '$.conversation_uploaded_raw_ids[0]'
                                 )=a.raw_object_id
                       ) OR NOT EXISTS (
                           SELECT 1 FROM raw_objects r
                           JOIN users exact_alias_uploader
                             ON exact_alias_uploader.id=a.uploaded_by
                            AND exact_alias_uploader.status='active'
                           WHERE r.id=a.raw_object_id AND r.user_id=a.user_id
                             AND r.source='upload' AND r.content_type='file'
                             AND r.deleted_at IS NULL
                             AND CASE
                               WHEN length(CAST(COALESCE(r.metadata_json,'') AS BLOB)) <= 131072
                                AND typeof(r.metadata_json)='text'
                                AND json_valid(r.metadata_json)
                               THEN CASE
                                 WHEN json_type(r.metadata_json)='object'
                                  AND NOT EXISTS (
                                        SELECT 1 FROM json_tree(r.metadata_json)
                                                       uploader_json_member
                                         WHERE uploader_json_member.key IS NOT NULL
                                         GROUP BY uploader_json_member.parent,
                                                  CAST(uploader_json_member.key AS TEXT)
                                        HAVING COUNT(*) > 1
                                      )
                                  AND json_type(r.metadata_json,'$.uploaded_by')='text'
                                 THEN json_extract(r.metadata_json,'$.uploaded_by')=a.uploaded_by
                                 ELSE 0
                               END
                               ELSE 0
                             END
                       )))
                LIMIT 1"""
        ).fetchone()
        if invalid is not None:
            raise UnsupportedSchemaVersionError(
                "Schema 34 file source alias filename data violates its invariant"
            )
        required: dict[str, tuple[str, ...]] = {
            "file_source_alias_filename_insert_guard": (
                "before insert on file_source_aliases",
                "length(new.supplied_filename) > 260",
                "instr(new.supplied_filename, '/')",
                "instr(new.supplied_filename, '\\')",
                "char(0)",
                "char(10)",
                "char(13)",
                "telegram-file:",
                "friday-message-name:msg_",
                "length(new.source_ref) = 40",
                "exact_alias_uploader.id=new.uploaded_by",
                "exact_alias_uploader.status='active'",
                "m.id=substr(new.source_ref,21,20)",
                "m.user_id=new.user_id and m.role='user'",
                "m.content='загружен документ: ' || new.supplied_filename",
                "synthetic_document_notice",
                "conversation_attachment_raw_ids",
                "conversation_uploaded_raw_ids",
                "r.id=new.raw_object_id and r.user_id=new.user_id",
                "r.source='upload' and r.content_type='file'",
                "json_extract(r.metadata_json,'$.uploaded_by')=new.uploaded_by",
            ),
            "file_source_alias_filename_update_guard": (
                "before update of supplied_filename on file_source_aliases",
                "old.supplied_filename <> '' and new.supplied_filename <> old.supplied_filename",
                "length(new.supplied_filename) > 260",
                "instr(new.supplied_filename, '/')",
                "instr(new.supplied_filename, '\\')",
                "char(0)",
                "char(10)",
                "char(13)",
                "telegram-file:",
                "friday-message-name:msg_",
                "length(new.source_ref) = 40",
                "exact_alias_uploader.id=new.uploaded_by",
                "exact_alias_uploader.status='active'",
                "m.id=substr(new.source_ref,21,20)",
                "m.user_id=new.user_id and m.role='user'",
                "m.content='загружен документ: ' || new.supplied_filename",
                "synthetic_document_notice",
                "conversation_attachment_raw_ids",
                "conversation_uploaded_raw_ids",
                "r.id=new.raw_object_id and r.user_id=new.user_id",
                "r.source='upload' and r.content_type='file'",
                "json_extract(r.metadata_json,'$.uploaded_by')=new.uploaded_by",
            ),
            "file_source_alias_identity_update_guard": (
                "before update of user_id, uploaded_by, source_ref, raw_object_id, created_at",
                "new.user_id is not old.user_id",
                "new.uploaded_by is not old.uploaded_by",
                "new.source_ref is not old.source_ref",
                "new.raw_object_id is not old.raw_object_id",
                "new.created_at is not old.created_at",
            ),
        }
        rows = conn.execute(
            """SELECT name, sql FROM sqlite_master
                WHERE type='trigger' AND name IN (?, ?, ?)""",
            tuple(required),
        ).fetchall()
        definitions = {str(row["name"]): " ".join(str(row["sql"] or "").casefold().split()) for row in rows}
        if any(
            name not in definitions
            or any(fragment.casefold() not in definitions[name] for fragment in fragments)
            for name, fragments in required.items()
        ):
            raise UnsupportedSchemaVersionError(
                "Schema 34 file source alias filename guards are missing or weakened"
            )

    @staticmethod
    def _widen_mission_task_states(conn: sqlite3.Connection, table_names: set[str]) -> None:
        """Разрешить шагам миссии состояния восстановления.

        `uncertain` («неизвестно, случился ли побочный эффект») и `compensated`
        появились в схеме 24 вместе с чекпойнтами. SQLite не умеет менять CHECK
        на месте, а `ALTER TABLE ADD COLUMN` его не трогает — поэтому база,
        созданная прежней версией, молча отвергала бы новое состояние ровно в тот
        момент, когда оно нужнее всего: при разборе сбоя.

        Таблица пересоздаётся со строками: шаги миссии — рабочие записи, терять
        их нельзя. Идёт только если старое ограничение действительно узкое.
        """
        if "mission_tasks" not in table_names:
            return
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='mission_tasks'"
        ).fetchone()
        definition = str((row[0] if row else "") or "")
        if "uncertain" in definition:
            return
        columns = [item[1] for item in conn.execute("PRAGMA table_info(mission_tasks)")]
        shared = ", ".join(columns)
        conn.execute("ALTER TABLE mission_tasks RENAME TO mission_tasks_pre24")
        # `executescript` неявно коммитит открытую транзакцию — на середине
        # миграции это означает половину применённой схемы при любой ошибке
        # дальше. Здесь ровно одно выражение, поэтому обычный execute.
        conn.execute(MISSION_TASKS_SCHEMA.strip().rstrip(";"))
        conn.execute(f"INSERT INTO mission_tasks({shared}) SELECT {shared} FROM mission_tasks_pre24")  # nosec B608
        conn.execute("DROP TABLE mission_tasks_pre24")

    @staticmethod
    def _rescope_data_sources_to_tenant(conn: sqlite3.Connection, table_names: set[str]) -> None:
        """Сделать ключом источника ПАРУ «владелец + имя» (схема 29).

        В схеме 28 первичным ключом было одно имя. Читается источник всегда
        парой `name + user_id`, а писался по имени — поэтому второй человек,
        объявив источник с уже занятым именем, делал UPDATE ЧУЖОЙ строки:
        владелец в ней оставался прежний, а `dsn_env` становился новый. Чужой
        источник начинал читать базу соседа, и заметно это стало бы нескоро.

        Таблица пересоздаётся со строками: объявленные источники — рабочие
        записи. Идёт только если ключ действительно старый.
        """

        if "data_sources" not in table_names:
            return
        # У составного ключа `pk` — это НОМЕР столбца в ключе, а не «да/нет»,
        # поэтому порядок берётся из него, а не из порядка объявления столбцов.
        parts = sorted(
            (item[5], item[1]) for item in conn.execute("PRAGMA table_info(data_sources)") if item[5]
        )
        if [name for _, name in parts] == ["user_id", "name"]:
            return
        columns = [item[1] for item in conn.execute("PRAGMA table_info(data_sources)")]
        shared = ", ".join(columns)
        conn.execute("ALTER TABLE data_sources RENAME TO data_sources_pre29")
        conn.execute(DATA_SOURCES_SCHEMA.strip().rstrip(";"))
        conn.execute(f"INSERT INTO data_sources({shared}) SELECT {shared} FROM data_sources_pre29")  # nosec B608
        conn.execute("DROP TABLE data_sources_pre29")

    def _retire_outdated_indexes(self, conn: sqlite3.Connection) -> None:
        """Снести индексы, чьё ОПРЕДЕЛЕНИЕ изменилось.

        `CREATE ... IF NOT EXISTS` смотрит только на имя: индекс с прежним
        набором столбцов остаётся жить, и правка молча не доезжает до базы, где
        она нужнее всего — на живой. Ровно тот же класс, что «новый столбец без
        нового номера схемы»: тесты создают базу с нуля и разницы не видят.

        Проверяется САМО определение из `sqlite_master`, а не номер версии:
        совпало с новым — трогать нечего, разошлось — пересоздаём.
        """
        wanted = {
            # Схема 26: дедуп оповещений считается по адресату (chat_id), потому
            # что у одного человека бывает несколько учёток и один чат.
            "uq_outbound_dedup": "chat_id, dedup_key",
            # Схема 30: законченная relation — исторический интервал, а не
            # действующая строка. Старый partial index запрещал человеку снова
            # вступить в ту же организацию после завершённого периода.
            "uq_active_relation": "WHERE deleted_at IS NULL AND valid_to IS NULL",
        }
        for name, columns in wanted.items():
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
            if row is None or not row[0]:
                continue
            if columns.replace(" ", "") in str(row[0]).replace(" ", ""):
                continue
            LOGGER.info("schema: пересоздаю индекс %s — определение изменилось", name)
            conn.execute(f"DROP INDEX IF EXISTS {name}")  # nosec B608 - имя из словаря выше
        # Уже лежащие дубли — часть той же работы, а не отдельная.
        #
        # Снести старый индекс мало: новый УНИКАЛЬНЫЙ не создастся, пока в
        # таблице есть строки, которые он запрещает. Замерено на живой базе
        # владельца — приложение не запустилось вовсе:
        #   sqlite3.IntegrityError: UNIQUE constraint failed:
        #   outbound_notifications.chat_id, outbound_notifications.dedup_key
        # То есть правка, придуманная против дублей, споткнулась ровно о те
        # дубли, ради которых написана.
        #
        # Оставляется САМАЯ РАННЯЯ строка каждой пары: она либо уже отправлена
        # (и человек её видел), либо ждёт очереди первой. Удаляются копии,
        # которые всё равно не должны были появиться.
        removed = conn.execute(
            """DELETE FROM outbound_notifications
               WHERE dedup_key <> '' AND rowid NOT IN (
                   SELECT MIN(rowid) FROM outbound_notifications
                   WHERE dedup_key <> '' GROUP BY chat_id, dedup_key
               )"""
        ).rowcount
        if removed:
            LOGGER.info("schema: убрано %d повторных оповещений в один чат", removed)

    def _migrate_legacy_schema(self, conn: sqlite3.Connection) -> None:
        """Upgrade pre-release databases without discarding personal data.

        Early Friday builds shipped before the users table, provenance hashes,
        normalized entity names and JSON snapshots existed. SQLite cannot add
        foreign keys or table constraints in place, so the migration adds the
        columns needed by current code and backfills equivalent records. New
        installations take the same idempotent path with no data to transform.
        """
        table_names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        additions: dict[str, dict[str, str]] = {
            "raw_objects": {
                "content_hash": "TEXT NOT NULL DEFAULT ''",
                "version": "INTEGER NOT NULL DEFAULT 1",
                "deleted_at": "TEXT",
            },
            "knowledge_object_versions": {
                "user_id": "TEXT NOT NULL DEFAULT ''",
                "snapshot_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "knowledge_objects": {
                "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
                "knowledge_kind": "TEXT NOT NULL DEFAULT 'note'",
                "quality_score": "REAL NOT NULL DEFAULT 0.5",
                "promotion_score": "REAL NOT NULL DEFAULT 0.5",
            },
            "inbox": {
                "knowledge_object_id": "TEXT",
                "suggestions_json": "TEXT NOT NULL DEFAULT '{}'",
                "suggested_action": "TEXT NOT NULL DEFAULT 'review'",
                "promotion_score": "REAL NOT NULL DEFAULT 0.0",
                "quality_score": "REAL NOT NULL DEFAULT 0.0",
            },
            "entities": {"normalized_name": "TEXT NOT NULL DEFAULT ''"},
            # Бюджеты, срок и следы восстановления миссий (спека v3 §5, схема 24).
            "missions": {
                "budget_seconds": "INTEGER NOT NULL DEFAULT 0",
                "budget_tool_calls": "INTEGER NOT NULL DEFAULT 0",
                "budget_retries": "INTEGER NOT NULL DEFAULT 0",
                "spent_seconds": "INTEGER NOT NULL DEFAULT 0",
                "spent_tool_calls": "INTEGER NOT NULL DEFAULT 0",
                "spent_retries": "INTEGER NOT NULL DEFAULT 0",
                "deadline_at": "TEXT",
            },
            "mission_tasks": {
                "attempts": "INTEGER NOT NULL DEFAULT 0",
                "side_effect": "INTEGER NOT NULL DEFAULT 0",
                "checkpoint_json": "TEXT NOT NULL DEFAULT '{}'",
                "compensation": "TEXT NOT NULL DEFAULT ''",
            },
            "entity_merge_history": {
                "transfer_json": "TEXT NOT NULL DEFAULT '{}'",
                "undone_at": "TEXT",
                "undone_by": "TEXT",
            },
            "entity_resolution_candidates": {
                "pair_key": "TEXT NOT NULL DEFAULT ''",
                "evidence_json": "TEXT NOT NULL DEFAULT '{}'",
            },
            "audit_log": {"request_id": "TEXT NOT NULL DEFAULT ''"},
            "request_idempotency": {
                "request_hash": "TEXT NOT NULL DEFAULT ''",
                "state": "TEXT NOT NULL DEFAULT 'complete'",
                "lease_token": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            },
            # Автор слежения. Пустая строка у старых строк означает «автор
            # неизвестен»: до этой правки его негде было взять. Такие слежения
            # видит только владелец архива — отдавать их участникам по догадке
            # значило бы ровно ту утечку, ради которой столбец и заводится.
            "monitors": {"created_by": "TEXT NOT NULL DEFAULT ''"},
            "conversations": {"mode": "TEXT NOT NULL DEFAULT 'dialogue'"},
            "channel_sessions": {"mode": "TEXT NOT NULL DEFAULT 'dialogue'"},
            # NULL expires_at = a non-expiring token (all legacy tokens stay valid).
            "api_tokens": {"expires_at": "TEXT"},
            # Fingerprint of the chunking config an object's chunk rows were built
            # with. '' means "not chunked" -- exactly what every pre-0.41 row already
            # stores, so turning chunking off re-indexes nothing.
            "knowledge_embeddings": {"chunk_scheme": "TEXT NOT NULL DEFAULT ''"},
            # Schema 34: preserve the basename attached to a deduplicated
            # transport identity without rewriting the canonical Raw metadata.
            "file_source_aliases": {
                "supplied_filename": "TEXT NOT NULL DEFAULT ''",
            },
            # Время связи (схема 27). Пустой `valid_from` у прежних строк — это
            # «начало неизвестно», и оно НЕ подменяется датой записи: `created_at`
            # говорит, когда мы узнали, а не когда стало правдой. Выдать одно за
            # другое значило бы задним числом объявить, что человек служит в части
            # с того дня, когда его рапорт попал в архив.
            "relations": {
                "valid_from": "TEXT NOT NULL DEFAULT ''",
                "valid_to": "TEXT",
                "invalidated_at": "TEXT",
                "superseded_by": "TEXT",
            },
        }
        for table, columns in additions.items():
            if table not in table_names:
                continue
            existing = self._table_columns(conn, table)
            for column, declaration in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

        self._widen_mission_task_states(conn, table_names)
        self._rescope_data_sources_to_tenant(conn, table_names)

        now = utc_now()

        # Схема 31 начинает честную transaction-time историю только с момента
        # миграции. Притвориться, что нынешние endpoints/valid_to существовали в
        # старом `created_at`, нельзя: прежние merge и invalidation уже необратимо
        # переписали current projection. Поэтому один immutable floor и baseline
        # получают ОДИН настоящий микросекундный timestamp внутри той же schema
        # transaction. Повтор после rollback либо reopen ничего не дублирует.
        relation_history_floor = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        stored_floor = conn.execute(
            "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
        ).fetchone()
        if stored_floor is None:
            conn.execute(
                """INSERT INTO schema_meta(key, value, updated_at)
                   VALUES('relation_history_complete_from', ?, ?)""",
                (relation_history_floor, relation_history_floor),
            )
            stored_floor = conn.execute(
                "SELECT value FROM schema_meta WHERE key='relation_history_complete_from'"
            ).fetchone()
        if stored_floor is None:
            raise sqlite3.IntegrityError("relation history completeness floor was not stored")
        relation_history_floor = str(stored_floor[0])

        # Legacy identity history was written with the then-current wall clock.
        # A machine-clock correction can leave its newest value after this
        # migration's wall time, so seed the durable logical clock from every
        # historical authority rather than moving it backwards to the floor.
        relation_history_observed_at = relation_history_floor
        for query in (
            "SELECT DISTINCT created_at FROM entity_versions",
            "SELECT DISTINCT created_at FROM entity_merge_history",
            "SELECT DISTINCT undone_at FROM entity_merge_history WHERE undone_at IS NOT NULL",
        ):
            for authority_row in conn.execute(query):
                raw_authority = str(authority_row[0] or "")
                if not raw_authority:
                    continue
                try:
                    authority = normalize_known_at(raw_authority, reject_future=False)
                except ValueError as exc:
                    raise sqlite3.IntegrityError(
                        "legacy entity transaction history has an unreadable timestamp"
                    ) from exc
                if authority > relation_history_observed_at:
                    relation_history_observed_at = authority
        context_row = conn.execute(
            "SELECT singleton FROM relation_revision_context WHERE singleton=1"
        ).fetchone()
        if context_row is None:
            conn.execute(
                """INSERT INTO relation_revision_context(
                       singleton, batch_id, recorded_at, observed_at
                   ) VALUES(1, '', '', ?)""",
                (relation_history_observed_at,),
            )
        conn.execute(
            """INSERT INTO relation_revisions(
                   relation_id, revision, present, operation, recorded_at, batch_id,
                   history_quality, user_id, source_entity_id, target_entity_id,
                   relation_type, weight, metadata_json, created_at, deleted_at,
                   valid_from, valid_to, invalidated_at, superseded_by
               )
               SELECT r.id, 1, 1, 'migration_baseline', ?, 'migration:v31',
                      'migration_baseline', r.user_id, r.source_entity_id,
                      r.target_entity_id, r.relation_type, r.weight, r.metadata_json,
                      r.created_at, r.deleted_at, r.valid_from, r.valid_to,
                      r.invalidated_at, r.superseded_by
               FROM relations AS r
               WHERE NOT EXISTS (
                   SELECT 1 FROM relation_revisions AS history
                   WHERE history.relation_id=r.id
               )
               ORDER BY r.id""",
            (relation_history_floor,),
        )

        # Register every tenant already referenced by legacy data. This makes
        # old knowledge immediately visible to workers and the Admin UI.
        tenant_tables = (
            "raw_objects",
            "knowledge_objects",
            "inbox",
            "entities",
            "relations",
            "feedback",
            "conversations",
            "messages",
            "audit_log",
        )
        tenant_ids: set[str] = set()
        for table in tenant_tables:
            if table not in table_names or "user_id" not in self._table_columns(conn, table):
                continue
            # ``table`` comes from the fixed legacy tenant-table allowlist above.
            rows = conn.execute(
                f"SELECT DISTINCT user_id FROM {table} WHERE user_id IS NOT NULL AND user_id<>''"  # nosec B608
            ).fetchall()
            tenant_ids.update(str(row[0]) for row in rows if row[0])
        for user_id in sorted(tenant_ids):
            # Audit history is intentionally retained after a hard deletion.  It
            # must not become a provisioning source on the next schema upgrade.
            # The opaque tombstone is the durable authority for that distinction.
            try:
                deletion_key = deleted_account_tombstone_key(user_id)
            except ValueError:
                deletion_key = ""
            if (
                deletion_key
                and conn.execute("SELECT 1 FROM runtime_kv WHERE key=?", (deletion_key,)).fetchone()
            ):
                continue
            conn.execute(
                """INSERT OR IGNORE INTO users(
                       id, source, external_id, display_name, username, preset_key, status,
                       metadata_json, created_at, updated_at, last_seen_at
                   ) VALUES(?, 'legacy', '', '', '', 'user', 'active', ?, ?, ?, ?)""",
                (
                    user_id,
                    json.dumps({"migrated_from_pre_release": True}, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )

        if "raw_objects" in table_names:
            rows = conn.execute(
                "SELECT id, raw_content FROM raw_objects WHERE content_hash='' OR content_hash IS NULL"
            ).fetchall()
            for row in rows:
                digest = hashlib.sha256(
                    str(row["raw_content"] or "").encode("utf-8", errors="replace")
                ).hexdigest()
                conn.execute(
                    "UPDATE raw_objects SET content_hash=? WHERE id=?",
                    (digest, row["id"]),
                )

        if "knowledge_objects" in table_names:
            # Existing records predate explicit promotion/quality scoring. Keep
            # them searchable, but mark the scores as migrated estimates so the
            # safe cleanup scanner can review them without destructive guesses.
            conn.execute(
                """UPDATE knowledge_objects
                   SET metadata_json=CASE
                         WHEN metadata_json IS NULL OR metadata_json='' THEN '{}'
                         ELSE metadata_json
                       END,
                       knowledge_kind=CASE
                         WHEN knowledge_kind IS NULL OR knowledge_kind='' THEN 'note'
                         ELSE knowledge_kind
                       END,
                       quality_score=CASE
                         WHEN quality_score IS NULL THEN 0.5
                         ELSE MIN(1.0, MAX(0.0, quality_score))
                       END,
                       promotion_score=CASE
                         WHEN promotion_score IS NULL THEN 0.5
                         ELSE MIN(1.0, MAX(0.0, promotion_score))
                       END"""
            )

        if "inbox" in table_names:
            conn.execute(
                """UPDATE inbox
                   SET suggestions_json=CASE
                         WHEN suggestions_json IS NULL OR suggestions_json='' THEN '{}'
                         ELSE suggestions_json
                       END,
                       suggested_action=CASE
                         WHEN suggested_action IS NULL OR suggested_action='' THEN 'review'
                         ELSE suggested_action
                       END,
                       promotion_score=MIN(1.0, MAX(0.0, COALESCE(promotion_score, 0.0))),
                       quality_score=MIN(1.0, MAX(0.0, COALESCE(quality_score, 0.0)))"""
            )

        if "request_idempotency" in table_names:
            conn.execute(
                """UPDATE request_idempotency
                   SET request_hash=COALESCE(request_hash, ''),
                       state=CASE WHEN state='pending' THEN 'pending' ELSE 'complete' END,
                       lease_token=COALESCE(lease_token, ''),
                       response_json=COALESCE(NULLIF(response_json, ''), '{}'),
                       updated_at=CASE
                         WHEN updated_at IS NULL OR updated_at='' THEN created_at
                         ELSE updated_at
                       END"""
            )

        if "conversations" in table_names:
            conn.execute(
                """UPDATE conversations SET mode=CASE
                       WHEN mode IN ('dialogue', 'knowledge_work', 'research') THEN mode
                       ELSE 'dialogue'
                   END"""
            )

        if "channel_sessions" in table_names:
            conn.execute(
                """UPDATE channel_sessions SET mode=CASE
                       WHEN mode IN ('dialogue', 'knowledge_work', 'research') THEN mode
                       ELSE 'dialogue'
                   END"""
            )

        # Schema v8 keeps the append-only feedback history and reconstructs a
        # single current signal per target/type.  Later changes update this
        # projection without deleting the audit trail.
        if {"feedback", "feedback_state"} <= table_names:
            rows = conn.execute(
                """SELECT f.* FROM feedback f
                   JOIN (
                     SELECT user_id, target_type, target_id, feedback_type, MAX(created_at) AS newest
                     FROM feedback
                     GROUP BY user_id, target_type, target_id, feedback_type
                   ) latest
                     ON latest.user_id=f.user_id
                    AND latest.target_type=f.target_type
                    AND latest.target_id=f.target_id
                    AND latest.feedback_type=f.feedback_type
                    AND latest.newest=f.created_at
                   ORDER BY f.id DESC"""
            ).fetchall()
            for row in rows:
                conn.execute(
                    """INSERT OR REPLACE INTO feedback_state(
                           user_id, target_type, target_id, feedback_type, score,
                           comment, context_json, feedback_id, updated_at
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["user_id"],
                        row["target_type"],
                        row["target_id"],
                        row["feedback_type"],
                        row["score"],
                        row["comment"],
                        row["context_json"],
                        row["id"],
                        row["created_at"],
                    ),
                )

        if {"knowledge_objects", "knowledge_usage"} <= table_names:
            conn.execute(
                """INSERT OR IGNORE INTO knowledge_usage(
                       user_id, knowledge_object_id, updated_at
                   )
                   SELECT user_id, id, COALESCE(NULLIF(updated_at, ''), created_at)
                   FROM knowledge_objects"""
            )

        if "entities" in table_names:
            # Recompute all values because schema v5 preserves punctuation in compact identifiers.
            # This prevents distinct symbols such as ``ABC.A`` and ``ABC/B`` from collapsing.
            #
            # v18 recomputes them again: `normalize_entity_name` now folds Russian
            # inflection and `ё`, so «CIDR-ПОДПИСКА» and «CIDR-ПОДПИСКУ» resolve to
            # one node instead of accumulating one node per grammatical case. The
            # rows are NOT merged here — folding changes what a lookup finds and
            # makes the pair visible as a duplicate candidate, while merging two
            # existing nodes stays the owner's decision.
            rows = conn.execute("SELECT id, name FROM entities").fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE entities SET normalized_name=? WHERE id=?",
                    (normalize_entity_name(row["name"]), row["id"]),
                )

        if "entity_resolution_candidates" in table_names:
            rows = conn.execute(
                "SELECT id, entity_a_id, entity_b_id FROM entity_resolution_candidates "
                "WHERE pair_key='' OR pair_key IS NULL"
            ).fetchall()
            for row in rows:
                pair_key = "|".join(sorted((row["entity_a_id"], row["entity_b_id"])))
                conn.execute(
                    "UPDATE entity_resolution_candidates SET pair_key=? WHERE id=?",
                    (pair_key, row["id"]),
                )

        # Convert old per-field versions to provenance-rich JSON snapshots.
        if "knowledge_object_versions" in table_names:
            version_columns = self._table_columns(conn, "knowledge_object_versions")
            rows = conn.execute(
                """SELECT v.*, k.user_id AS owner_user_id,
                          k.raw_object_id AS owner_raw_object_id,
                          k.entity_id AS owner_entity_id,
                          k.superseded_by_id AS owner_superseded_by_id
                   FROM knowledge_object_versions v
                   JOIN knowledge_objects k ON k.id=v.knowledge_object_id
                   WHERE v.user_id='' OR v.user_id IS NULL
                      OR v.snapshot_json='{}' OR v.snapshot_json IS NULL"""
            ).fetchall()
            for row in rows:
                values = dict(row)
                owner = str(values.pop("owner_user_id") or "")
                raw_object_id = str(values.pop("owner_raw_object_id") or "")
                entity_id = values.pop("owner_entity_id")
                superseded_by_id = values.pop("owner_superseded_by_id")
                snapshot: dict[str, Any] = {
                    key: values.get(key)
                    for key in (
                        "knowledge_object_id",
                        "version",
                        "content",
                        "title",
                        "summary",
                        "tags_json",
                        "importance",
                        "created_at",
                    )
                    if key in version_columns
                }
                # Current readers authenticate every historical body against the
                # live object's immutable identity and structural dependencies.
                # Old per-field history did not carry those keys, so add the
                # owner-side values while converting it instead of producing a
                # snapshot that is immediately (and correctly) rejected as
                # unauthenticated history.
                snapshot.update(
                    {
                        "id": str(values.get("knowledge_object_id") or ""),
                        "user_id": owner,
                        "raw_object_id": raw_object_id,
                        "entity_id": entity_id,
                        "superseded_by_id": superseded_by_id,
                    }
                )
                conn.execute(
                    "UPDATE knowledge_object_versions SET user_id=?, snapshot_json=? WHERE id=?",
                    (owner, _snapshot(snapshot), row["id"]),
                )

        # Ensure each current object/entity has at least one baseline version.
        if "knowledge_objects" in table_names:
            for row in conn.execute("SELECT * FROM knowledge_objects").fetchall():
                existing = conn.execute(
                    """SELECT 1 FROM knowledge_object_versions
                       WHERE knowledge_object_id=? AND version=? LIMIT 1""",
                    (row["id"], int(row["version"] or 1)),
                ).fetchone()
                if not existing:
                    conn.execute(
                        """INSERT INTO knowledge_object_versions
                           (id, user_id, knowledge_object_id, version, snapshot_json, created_at)
                           VALUES(?, ?, ?, ?, ?, ?)""",
                        (
                            new_id("kov"),
                            row["user_id"],
                            row["id"],
                            int(row["version"] or 1),
                            _snapshot(dict(row)),
                            now,
                        ),
                    )

        if "entities" in table_names:
            for row in conn.execute("SELECT * FROM entities").fetchall():
                conn.execute(
                    """INSERT OR IGNORE INTO entity_versions
                       (id, user_id, entity_id, version, snapshot_json, created_at)
                       VALUES(?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("entv"),
                        row["user_id"],
                        row["id"],
                        int(row["version"] or 1),
                        _snapshot(dict(row)),
                        now,
                    ),
                )

        # Reconstruct links represented by legacy convenience columns.
        #
        # `k.deleted_at IS NULL` is load-bearing, not defensive. DATA_LIFECYCLE §3
        # makes IGNORED a verdict: the attached Knowledge Object is soft-deleted and
        # the Inbox link cleared, so the material leaves retrieval. Matching on
        # raw_object_id alone re-pointed that Inbox row straight back at the object
        # the human had just rejected — the reconstruction quietly overruled a
        # review decision it knows nothing about.
        if "inbox" in table_names and "knowledge_objects" in table_names:
            conn.execute(
                """UPDATE inbox
                   SET knowledge_object_id=(
                       SELECT k.id FROM knowledge_objects k
                       WHERE k.user_id=inbox.user_id AND k.raw_object_id=inbox.raw_object_id
                         AND k.deleted_at IS NULL
                       ORDER BY k.version DESC LIMIT 1
                   )
                   WHERE (knowledge_object_id IS NULL OR knowledge_object_id='')
                     AND reviewed_at IS NULL"""
            )

        if {"knowledge_objects", "entities", "knowledge_entity_links"} <= table_names:
            rows = conn.execute(
                """SELECT k.user_id, k.id AS knowledge_object_id, k.entity_id
                   FROM knowledge_objects k
                   JOIN entities e ON e.id=k.entity_id AND e.user_id=k.user_id
                   WHERE k.entity_id IS NOT NULL AND k.entity_id<>''"""
            ).fetchall()
            for row in rows:
                conn.execute(
                    """INSERT OR IGNORE INTO knowledge_entity_links(
                           id, user_id, knowledge_object_id, entity_id, status, confidence,
                           evidence_json, created_at, reviewed_at, reviewed_by
                       ) VALUES(?, ?, ?, ?, 'accepted', 1.0, ?, ?, ?, 'migration')""",
                    (
                        new_id("kel"),
                        row["user_id"],
                        row["knowledge_object_id"],
                        row["entity_id"],
                        json.dumps({"source": "legacy_entity_id"}, sort_keys=True),
                        now,
                        now,
                    ),
                )

    def close(self, *, final: bool = False) -> None:
        """Close every thread's connection. ``final`` makes the closure permanent.

        ``final=False`` keeps the reopen-after-close contract that ``restore_backup``
        depends on. ``final=True`` is process shutdown: any later access raises
        ``StorageClosedError`` instead of quietly opening a new connection behind the
        released ``backend.lock``.
        """
        if final:
            self._shut_down = True
        # Shut down every thread's connection (not just the caller's), commit any
        # pending work, then invalidate the per-thread caches by bumping the
        # generation and re-arming the one-time schema init. This makes close()
        # release all WAL locks before a restore swaps the database file, and lets
        # the next use on any thread transparently reopen (and re-migrate) — the
        # reopen-after-close contract restore_backup() depends on.
        #
        # The write lock is taken first (same order as transaction()) so a shutdown
        # drains any in-flight write transaction — e.g. a background worker whose
        # asyncio task was cancelled but whose to_thread() DB call is still running
        # — before its connection is closed, instead of committing a half-written
        # transaction or closing the connection mid-statement.
        with self._write_lock:
            with self._registry_lock:
                connections = list(self._connections)
                self._connections.clear()
                self._generation += 1
                self._schema_ready = False
            for connection in connections:
                with suppress(sqlite3.Error):
                    # Roll back, never commit. A connection still inside a
                    # transaction at shutdown is holding an *unfinished* unit of
                    # work — committing it here is how an aborted write became
                    # durable. Nothing legitimate depends on this commit: every DML
                    # statement in the storage layer runs inside transaction(),
                    # which commits on its own successful exit.
                    if connection.in_transaction:
                        connection.rollback()
                with suppress(sqlite3.Error):
                    connection.close()

    def execute(self, sql: str, params: tuple | dict | None = None) -> sqlite3.Cursor:
        # No lock: this thread's own connection, and the caller fetches the cursor
        # on this same thread, so no cursor is ever stepped across threads.
        return self.conn.execute(sql, params or ())

    def commit(self) -> None:
        conn = self.conn
        _refresh_private_derivative_authority(conn)
        conn.commit()

    def optimize(self) -> None:
        # PRAGMA optimize runs ANALYZE, which writes sqlite_stat*, so it is a writer
        # and must take the write lock like transaction() — otherwise it contends
        # with a concurrent BEGIN IMMEDIATE at the SQLite level (risking "database is
        # locked") and close() cannot drain it before shutting the connection down.
        with self._write_lock:
            self.conn.execute("PRAGMA optimize")

    def _observe_relation_history_boundary(self, boundary: str) -> None:
        """Persist one historical-read promise before publishing its snapshot.

        A cutoff can legally sit after the latest event but before wall time. If
        CLOCK_REALTIME later moves backwards, strict-after-event timestamps alone
        would let a new commit appear inside that already returned cutoff.  The
        singleton observed boundary is therefore a durable logical clock shared
        by readers, managed writers and direct-relation fallback triggers.
        """

        try:
            canonical_boundary = normalize_known_at(boundary, reject_future=False)
        except ValueError as exc:
            raise RelationHistorySnapshotError("relation history observed boundary is unreadable") from exc
        if boundary != canonical_boundary:
            raise RelationHistorySnapshotError("relation history observed boundary is non-canonical")

        conn = self.conn
        fast_row = conn.execute(
            """SELECT batch_id, recorded_at, observed_at
                 FROM relation_revision_context WHERE singleton=1"""
        ).fetchone()
        if fast_row is None:
            raise RelationHistorySnapshotError("relation history observed boundary is missing")
        raw_observed = str(fast_row["observed_at"] or "")
        try:
            current_observed = normalize_known_at(raw_observed, reject_future=False)
        except ValueError as exc:
            raise RelationHistorySnapshotError("relation history observed boundary is unreadable") from exc
        if raw_observed != current_observed:
            raise RelationHistorySnapshotError("relation history observed boundary is non-canonical")
        if conn.in_transaction:
            # The value visible on this connection may itself be an uncommitted
            # managed-clock advance. Returning before the caller's outer COMMIT
            # would publish a promise that an eventual ROLLBACK can erase.
            raise RelationHistorySnapshotError(
                "relation history boundary cannot be observed inside an active transaction"
            )
        if current_observed >= canonical_boundary:
            return

        with self._write_lock:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                context = conn.execute(
                    """SELECT batch_id, recorded_at, observed_at
                         FROM relation_revision_context WHERE singleton=1"""
                ).fetchone()
                if context is None:
                    raise RelationHistorySnapshotError("relation history observed boundary is missing")
                if str(context["batch_id"] or "") or str(context["recorded_at"] or ""):
                    raise RelationHistorySnapshotError(
                        "relation history revision context is unexpectedly active"
                    )
                raw_observed = str(context["observed_at"] or "")
                try:
                    current_observed = normalize_known_at(raw_observed, reject_future=False)
                except ValueError as exc:
                    raise RelationHistorySnapshotError(
                        "relation history observed boundary is unreadable"
                    ) from exc
                if raw_observed != current_observed:
                    raise RelationHistorySnapshotError("relation history observed boundary is non-canonical")
                if canonical_boundary > current_observed:
                    cursor = conn.execute(
                        """UPDATE relation_revision_context SET observed_at=?
                             WHERE singleton=1 AND observed_at<?""",
                        (canonical_boundary, canonical_boundary),
                    )
                    if cursor.rowcount != 1:
                        raise RelationHistorySnapshotError(
                            "relation history observed boundary was not persisted"
                        )
                conn.commit()
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        guarded_context = _GUARDED_TRANSACTION_CONTEXT.get()
        before_commit = (
            guarded_context[1] if guarded_context is not None and guarded_context[0] is self else None
        )
        after_commit = (
            guarded_context[2] if guarded_context is not None and guarded_context[0] is self else None
        )
        after_rollback = (
            guarded_context[3] if guarded_context is not None and guarded_context[0] is self else None
        )
        # Serialise writers in Python (single-writer invariant), so two threads'
        # BEGIN IMMEDIATE never contend at the SQLite level and close() can drain
        # the in-flight writer before shutting connections down. Reads never take
        # this lock, so concurrent readers still run in parallel over WAL. The lock
        # is reentrant, so a nested transaction() on the same thread — detected via
        # this thread's own connection's in_transaction — does not self-deadlock.
        with self._write_lock:
            conn = self.conn
            nested = conn.in_transaction
            if before_commit is not None and nested:
                # A guarded publication must own the durable outer boundary.
                # Running it as a savepoint could return a message id while an
                # unrelated caller still owns (and may roll back) the commit.
                raise RuntimeError("guarded transaction requires the outer commit boundary")
            if not nested:
                conn.execute("BEGIN IMMEDIATE")
            context = conn.execute(
                """SELECT recorded_at, observed_at
                     FROM relation_revision_context WHERE singleton=1"""
            ).fetchone()
            # Обычно nested=True означает второй FridayStorage.transaction() и
            # контекст уже принадлежит внешнему блоку. Но legacy/test code иногда
            # сначала делает прямой execute(), открывая неявную sqlite transaction,
            # и лишь затем входит сюда. Такой блок тоже обязан получить точный
            # relation batch; отличаем его по ПУСТОМУ context, а не по одному лишь
            # sqlite `in_transaction`.
            owns_context = not nested or context is None or not str(context["recorded_at"] or "")
            # `nested=True` has two different meanings. A non-empty relation
            # context means a real nested Friday transaction: it deliberately
            # shares the outer batch and lets the outer block own rollback. An
            # EMPTY context means a caller first opened an implicit sqlite
            # transaction through execute(). We do not own that transaction, but
            # we must still be able to roll back precisely the managed unit on an
            # exception; otherwise its relation row and revisions can be committed
            # later by an unrelated storage.commit().
            # Every nested managed unit gets its own rollback boundary.  A real
            # nested Friday transaction still shares the outer relation context,
            # but an exception caught by the outer block must remove only the
            # inner current/history writes instead of poisoning the outer unit.
            savepoint = new_id("friday_managed") if nested else ""
            if savepoint:
                conn.execute(f"SAVEPOINT {savepoint}")  # nosec B608 - generated [a-z0-9_] identifier
            try:
                if owns_context:
                    # Все relation mutations одного outer unit становятся видны одним
                    # transaction-time срезом. event_seq разрешает несколько версий
                    # одной строки внутри batch, не разрывая merge/unmerge посередине.
                    wall_timestamp = (
                        datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
                    )
                    authority = conn.execute(
                        """SELECT
                               (SELECT recorded_at FROM relation_revisions
                                 ORDER BY event_seq DESC LIMIT 1) AS latest_relation_recorded_at,
                               (SELECT created_at FROM entity_versions
                                 ORDER BY created_at DESC, rowid DESC
                                 LIMIT 1) AS latest_entity_version_at,
                               (SELECT created_at FROM entity_merge_history
                                 ORDER BY created_at DESC, rowid DESC
                                 LIMIT 1) AS latest_merge_created_at,
                               (SELECT undone_at FROM entity_merge_history
                                 WHERE undone_at IS NOT NULL
                                 ORDER BY undone_at DESC, rowid DESC
                                 LIMIT 1) AS latest_merge_undone_at,
                               (SELECT value FROM schema_meta
                                 WHERE key='relation_history_complete_from') AS history_floor,
                               (SELECT observed_at FROM relation_revision_context
                                 WHERE singleton=1) AS observed_at"""
                    ).fetchone()
                    authority_boundaries: list[str] = []
                    for raw_boundary, require_canonical in (
                        (
                            authority["latest_relation_recorded_at"] if authority else None,
                            True,
                        ),
                        (authority["latest_entity_version_at"] if authority else None, False),
                        (authority["latest_merge_created_at"] if authority else None, False),
                        (authority["latest_merge_undone_at"] if authority else None, False),
                        (authority["history_floor"] if authority else None, True),
                        (authority["observed_at"] if authority else None, True),
                    ):
                        if not raw_boundary:
                            continue
                        raw_text = str(raw_boundary)
                        try:
                            canonical = normalize_known_at(raw_text, reject_future=False)
                        except ValueError as exc:
                            raise RuntimeError("relation transaction-time authority is unreadable") from exc
                        if require_canonical and raw_text != canonical:
                            raise RuntimeError("relation transaction-time authority is non-canonical")
                        authority_boundaries.append(canonical)
                    # CLOCK_REALTIME may move backwards after NTP/manual correction
                    # or a power recovery. A timestamp-only public cutoff cannot
                    # distinguish two committed batches at the same instant, so a
                    # later graph/history transaction must be STRICTLY after every
                    # prior relation or identity event. Events inside this one outer
                    # transaction still share the resulting timestamp and batch.
                    history_tail = max(authority_boundaries)
                    if wall_timestamp > history_tail:
                        recorded_at = wall_timestamp
                    else:
                        recorded_at = (
                            (
                                _TIMESTAMP_DATETIME.fromisoformat(history_tail.replace("Z", "+00:00"))
                                + timedelta(microseconds=1)
                            )
                            .isoformat(timespec="microseconds")
                            .replace("+00:00", "Z")
                        )
                    cursor = conn.execute(
                        """UPDATE relation_revision_context
                           SET batch_id=?, recorded_at=?, observed_at=?
                           WHERE singleton=1""",
                        (new_id("relation_batch"), recorded_at, recorded_at),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("relation revision transaction context is missing")
                yield conn
                if not nested:
                    # Persistent BEFORE invalidators are global and rollback-safe.
                    # Common ingest triggers update one authority row immediately;
                    # any conservative/batched dirty path is rebuilt exactly once
                    # at the outer publication boundary.
                    _refresh_private_derivative_authority(conn)
                if owns_context:
                    # Очистка входит в ТОТ ЖЕ commit. При rollback возвращается
                    # прежний пустой context вместе с current row и revisions.
                    conn.execute(
                        """UPDATE relation_revision_context
                           SET batch_id='', recorded_at='' WHERE singleton=1"""
                    )
                if savepoint:
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")  # nosec B608 - generated identifier
                if not nested:
                    # Selected orchestration routes may own an absolute
                    # publication deadline.  Run their code-owned guard after
                    # every refresh/context mutation but before the single
                    # durable commit; an exception follows the ordinary full
                    # rollback path below.  Existing callers remain unchanged.
                    if before_commit is not None:
                        before_commit()
                    conn.commit()
                    if after_commit is not None:
                        after_commit()
            except BaseException:
                # BaseException, not Exception: KeyboardInterrupt, SystemExit and
                # asyncio.CancelledError all unwind through here, and all three are
                # ways a *real* transaction gets abandoned — Ctrl-C during `jericho
                # import`, a cancelled worker tick. Catching only Exception left the
                # BEGIN IMMEDIATE open on this connection, and close() then committed
                # it: an interrupted unit of work became a durable partial write.
                if not nested:
                    if conn.in_transaction:
                        conn.rollback()
                        if after_rollback is not None:
                            after_rollback()
                    elif after_commit is not None:
                        # A signal may land after sqlite committed but before the
                        # next Python bytecode. Preserve the durable fence rather
                        # than releasing a request that already published rows.
                        after_commit()
                elif savepoint:
                    # Keep the caller's prelude and outer implicit transaction,
                    # but remove every current/history/context write made by this
                    # managed unit. RELEASE closes the savepoint after ROLLBACK TO;
                    # neither statement commits the caller's outer transaction.
                    with suppress(sqlite3.Error):
                        conn.execute(
                            f"ROLLBACK TO SAVEPOINT {savepoint}"  # nosec B608 - generated identifier
                        )
                        conn.execute(
                            f"RELEASE SAVEPOINT {savepoint}"  # nosec B608 - generated identifier
                        )
                raise
