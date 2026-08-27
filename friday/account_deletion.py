"""Fail-closed permanent account deletion for the administrative console.

An account is wider than the row in ``users``: identities and tokens can still
authenticate, sessions can still select an old conversation, FTS mirrors contain
the text, and Telegram can auto-provision the same derived id on its next update.
This module owns the single coordinated deletion plan and its preflight.

Some material deliberately cannot be guessed away.  Append-only relation history,
files/vault projections and rows authored inside another (shared) tenant block the
operation.  The preflight reports those categories without returning content; the
delete path recomputes the same report under ``BEGIN IMMEDIATE`` before changing a
row.  A blocked or stale request therefore changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import zlib
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from friday.document_catalog.worker_state import (
    DOCUMENT_CATALOG_WORKER_STATE_KEY,
    document_catalog_worker_entry_fingerprint,
    load_document_catalog_worker_namespace_key,
    remove_document_catalog_worker_entry,
)
from friday.memory import MemoryVaultDeletionHandle, VaultProjectionBoundaryError
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage._base import (
    account_deletion_eligibility_key,
    account_external_identity_history_key,
    deleted_account_tombstone_key,
    deleted_identity_tombstone_key,
    known_runtime_key_owners,
    validate_user_id,
)
from friday.storage.models import AuditEntry, new_id, utc_now

if TYPE_CHECKING:
    from friday.storage import FridayStorage


class AccountDeletionBlocked(ValueError):
    """The current account shape cannot be erased without an unsafe policy guess."""

    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__("Account deletion preflight is blocked")
        self.report = report


class AccountDeletionConflict(ValueError):
    """The account changed after the administrator reviewed the preflight."""


@dataclass(frozen=True)
class _Scope:
    key: str
    table: str
    predicate: str


# Every row which is owned directly by an account, in foreign-key-safe deletion
# order.  Tables without an explicit user column use a bounded subquery into the
# target's entity set.  ``relation_revisions`` is counted separately and blocks:
# its append-only trigger correctly refuses ordinary DELETE.
_DELETE_SCOPES: tuple[_Scope, ...] = (
    _Scope(
        "work_item_compare_current_file_web_restart_rebind_steps",
        "work_item_compare_current_file_web_restart_rebind_steps",
        "graph_id IN (SELECT id FROM work_item_compare_current_file_web_graphs WHERE user_id=?)",
    ),
    _Scope(
        "work_item_compare_current_file_web_restart_rebinds",
        "work_item_compare_current_file_web_restart_rebinds",
        "graph_id IN (SELECT id FROM work_item_compare_current_file_web_graphs WHERE user_id=?)",
    ),
    _Scope(
        "work_item_compare_current_file_web_steps",
        "work_item_compare_current_file_web_steps",
        "graph_id IN (SELECT id FROM work_item_compare_current_file_web_graphs WHERE user_id=?)",
    ),
    _Scope(
        "work_item_compare_current_file_web_graphs",
        "work_item_compare_current_file_web_graphs",
        "user_id=?",
    ),
    _Scope(
        "work_item_compare_outcomes",
        "work_item_compare_outcomes",
        "work_item_id IN (SELECT id FROM work_items WHERE user_id=?)",
    ),
    _Scope(
        "work_item_compare_document_evidence",
        "work_item_compare_document_evidence",
        "work_item_id IN (SELECT id FROM work_items WHERE user_id=?)",
    ),
    _Scope(
        "work_item_compare_document_questions",
        "work_item_compare_document_questions",
        "work_item_id IN (SELECT id FROM work_items WHERE user_id=?)",
    ),
    _Scope(
        "work_item_archive_candidate_questions",
        "work_item_archive_candidate_questions",
        "work_item_id IN (SELECT id FROM work_items WHERE user_id=?)",
    ),
    _Scope(
        "work_item_archive_candidate_set_items",
        "work_item_archive_candidate_set_items",
        "work_item_id IN (SELECT id FROM work_items WHERE user_id=?)",
    ),
    _Scope(
        "work_item_archive_candidate_sets",
        "work_item_archive_candidate_sets",
        "work_item_id IN (SELECT id FROM work_items WHERE user_id=?)",
    ),
    _Scope(
        "work_item_selected_evidence",
        "work_item_selected_evidence",
        "work_item_id IN (SELECT id FROM work_items WHERE user_id=?)",
    ),
    _Scope("work_items", "work_items", "user_id=?"),
    _Scope("interaction_failure_traces", "interaction_failure_traces", "user_id=?"),
    _Scope("obsidian_active_frames", "obsidian_active_frames", "user_id=?"),
    _Scope("obsidian_candidate_set_items", "obsidian_candidate_set_items", "user_id=?"),
    _Scope("obsidian_candidate_sets", "obsidian_candidate_sets", "user_id=?"),
    _Scope("obsidian_note_links", "obsidian_note_links", "user_id=?"),
    _Scope("obsidian_note_index", "obsidian_note_index", "user_id=?"),
    _Scope("obsidian_note_bindings", "obsidian_note_bindings", "user_id=?"),
    _Scope("obsidian_conflicts", "obsidian_conflicts", "user_id=?"),
    _Scope("obsidian_operations", "obsidian_operations", "user_id=?"),
    _Scope("obsidian_pairing_candidates", "obsidian_pairing_candidates", "user_id=?"),
    _Scope("obsidian_onboarding_sessions", "obsidian_onboarding_sessions", "user_id=?"),
    _Scope("obsidian_vaults", "obsidian_vaults", "user_id=?"),
    _Scope("obsidian_android_devices", "obsidian_android_devices", "user_id=?"),
    _Scope("obsidian_sync_profiles", "obsidian_sync_profiles", "user_id=?"),
    _Scope(
        "private_entity_owners",
        "private_entity_owners",
        "entity_id IN (SELECT id FROM entities WHERE user_id=?)",
    ),
    _Scope("feedback_state", "feedback_state", "user_id=?"),
    _Scope("knowledge_chunk_embeddings", "knowledge_chunk_embeddings", "user_id=?"),
    _Scope("knowledge_embeddings", "knowledge_embeddings", "user_id=?"),
    _Scope("knowledge_usage", "knowledge_usage", "user_id=?"),
    _Scope("knowledge_entity_links", "knowledge_entity_links", "user_id=?"),
    _Scope("relation_candidates", "relation_candidates", "user_id=?"),
    _Scope("knowledge_conflicts", "knowledge_conflicts", "user_id=?"),
    _Scope("entity_resolution_candidates", "entity_resolution_candidates", "user_id=?"),
    _Scope("entity_time", "entity_time", "user_id=?"),
    _Scope("entity_versions", "entity_versions", "user_id=?"),
    _Scope("entity_merge_history", "entity_merge_history", "user_id=?"),
    _Scope("inbox", "inbox", "user_id=?"),
    _Scope("knowledge_object_versions", "knowledge_object_versions", "user_id=?"),
    _Scope("mission_tasks", "mission_tasks", "user_id=?"),
    _Scope("channel_sessions", "channel_sessions", "user_id=?"),
    _Scope("request_idempotency", "request_idempotency", "user_id=?"),
    _Scope("action_approvals", "action_approvals", "user_id=?"),
    _Scope("day_compacts", "day_compacts", "principal=?"),
    _Scope("eval_cases", "eval_cases", "user_id=?"),
    _Scope("outbound_notifications", "outbound_notifications", "user_id=?"),
    _Scope("monitors", "monitors", "user_id=?"),
    _Scope("data_sources", "data_sources", "user_id=?"),
    _Scope("api_tokens", "api_tokens", "user_id=?"),
    _Scope("feedback", "feedback", "user_id=?"),
    _Scope("missions", "missions", "user_id=?"),
    _Scope("knowledge_objects", "knowledge_objects", "user_id=?"),
    # A transport alias is a disposable authority pointer, not the shared Raw
    # Object itself.  Both account axes are ON DELETE CASCADE in schema 33, but
    # delete it explicitly before Raw/users so preflight and result accounting
    # include the exact row once even when both columns name the target.
    _Scope(
        "file_source_aliases",
        "file_source_aliases",
        "user_id=? OR uploaded_by=?",
    ),
    # DocumentCatalog deliberately carries no duplicate owner.  Its ownership is
    # derived through the authoritative Raw Object and it must be counted/deleted
    # before that Raw disappears.
    _Scope(
        "document_catalog",
        "document_catalog",
        "raw_object_id IN (SELECT id FROM raw_objects WHERE user_id=?)",
    ),
    _Scope("raw_objects", "raw_objects", "user_id=?"),
    _Scope("entities", "entities", "user_id=?"),
    _Scope("user_identities", "user_identities", "user_id=?"),
    _Scope("user_permission_overrides", "user_permission_overrides", "user_id=?"),
)

_CANDIDATE_CASCADE_DELETE_KEYS = frozenset(
    {
        "work_item_compare_current_file_web_restart_rebind_steps",
        "work_item_compare_current_file_web_restart_rebinds",
        "work_item_compare_current_file_web_steps",
        "work_item_compare_outcomes",
        "work_item_compare_document_evidence",
        "work_item_compare_document_questions",
        "work_item_archive_candidate_questions",
        "work_item_archive_candidate_set_items",
        "work_item_archive_candidate_sets",
    }
)

_BLOCKING_GRAPH_SCOPES: tuple[_Scope, ...] = (
    _Scope("relations", "relations", "user_id=?"),
    _Scope("relation_revisions", "relation_revisions", "user_id=?"),
)

_BLOCKING_CHAT_SCOPES: tuple[_Scope, ...] = (
    _Scope("messages", "messages", "user_id=?"),
    _Scope("conversations", "conversations", "user_id=?"),
)

# Schema 43 host-action events are append-only and their parent plan identity is
# immutable.  Treat the complete lifecycle as retained history until a dedicated
# erasure policy exists; silently deleting only the user row would either violate
# that contract or leave an FK dependency discovered too late in the delete path.
_BLOCKING_HOST_ACTION_SCOPES: tuple[_Scope, ...] = (
    _Scope(
        "host_action_events",
        "host_action_events",
        "job_id IN (SELECT id FROM host_action_jobs WHERE user_id=? OR actor_own_id=?)",
    ),
    _Scope(
        "host_action_jobs",
        "host_action_jobs",
        "user_id=? OR actor_own_id=?",
    ),
)

# Schema audit: a future table carrying one of these ownership columns must be
# classified explicitly before deletion can proceed.  Otherwise a new feature can
# silently turn a once-complete cascade into an orphan-producing one.
_KNOWN_USER_SCOPES = frozenset(
    {
        (scope.table, "user_id")
        for scope in (
            *_DELETE_SCOPES,
            *_BLOCKING_GRAPH_SCOPES,
            *_BLOCKING_CHAT_SCOPES,
            *_BLOCKING_HOST_ACTION_SCOPES,
        )
        if scope.predicate == "user_id=?"
    }
    | {
        ("day_compacts", "principal"),
        ("audit_log", "user_id"),
        # These are disposable privacy derivatives.  SQLite's authorizer
        # intentionally refuses direct writes; transaction publication rebuilds
        # them from the surviving base rows after the account cascade.
        ("private_entity_material_derivative_cache", "user_id"),
        ("private_entity_material_derivative_work", "user_id"),
        # Schema 33's alias row is owned through either account axis.  The
        # compound delete scope above is authoritative for both columns.
        ("file_source_aliases", "user_id"),
        ("file_source_aliases", "uploaded_by"),
        # Both user FKs are one immutable schema-43 host-action identity.  The
        # compound blocking scope above owns both axes.
        ("host_action_jobs", "user_id"),
        ("host_action_jobs", "actor_own_id"),
    }
)

_KNOWN_ACTOR_REFERENCE_SCOPES = frozenset(
    {
        ("custom_presets", "created_by"),
        ("user_identities", "linked_by"),
        ("inbox", "reviewed_by"),
        ("knowledge_entity_links", "reviewed_by"),
        ("entity_resolution_candidates", "resolved_by"),
        ("entity_merge_history", "merged_by"),
        ("entity_merge_history", "undone_by"),
        ("relation_candidates", "reviewed_by"),
        ("knowledge_conflicts", "reviewed_by"),
        ("missions", "created_by"),
        ("api_tokens", "created_by"),
        ("monitors", "created_by"),
        ("data_sources", "created_by"),
        ("action_approvals", "requested_by"),
        ("action_approvals", "decided_by"),
        # Also an ownership axis in _DELETE_SCOPES; name it here because the
        # schema auditor classifies every ``*_by`` column as an actor reference.
        ("file_source_aliases", "uploaded_by"),
        # These end in ``_by`` but point to another relation, not an actor.
        ("relations", "superseded_by"),
        ("relation_revisions", "superseded_by"),
    }
)

_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_component(user_id: str) -> str:
    original = (user_id or "user").strip()
    slug = _SAFE_COMPONENT_RE.sub("-", original).strip(" .-")[:48] or "user"
    digest = hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{slug}--{digest}"


def _account_files_directory(storage: FridayStorage, user_id: str) -> Path:
    return Path(storage.settings.files_dir) / _safe_component(user_id)


def _obsidian_account_directory(storage: FridayStorage, user_id: str) -> Path:
    from friday.organs.obsidian.syncthing import owner_filesystem_key

    return storage.settings.obsidian_effective_root / "users" / owner_filesystem_key(user_id)


def _path_state(path: Path) -> str:
    """Classify without reading file bodies or following a user-controlled link."""

    try:
        if path.is_symlink():
            return "unsafe"
        if not path.exists():
            return "absent"
        if not path.is_dir():
            return "unsafe"
        return "material" if next(path.iterdir(), None) is not None else "empty"
    except OSError:
        return "unreadable"


def _vault_account_state(storage: FridayStorage, user_id: str) -> str:
    try:
        return MemoryVaultDeletionHandle(storage.settings.memory_vault_dir).account_state(user_id)
    except VaultProjectionBoundaryError:
        return "unsafe"


def _account_export_artifacts(storage: FridayStorage, user_id: str) -> tuple[int, str]:
    """Count code-owned export names without opening their personal payloads."""

    root = Path(storage.settings.exports_dir)
    try:
        if root.is_symlink():
            return 0, "unsafe"
        if not root.exists():
            return 0, "absent"
        if not root.is_dir():
            return 0, "unsafe"
        slug = _SAFE_COMPONENT_RE.sub("-", user_id.strip()).strip(".-")[:80] or "user"
        identity_hash = hashlib.sha256(user_id.encode("utf-8", errors="replace")).hexdigest()[:12]
        prefix = f"jericho-export-{slug}--{identity_hash}-"
        count = sum(
            1
            for candidate in root.iterdir()
            if (
                (candidate.name.startswith(prefix) and candidate.name.endswith(".json"))
                or (candidate.name.startswith(f".{prefix}") and candidate.name.endswith(".tmp"))
            )
        )
        return count, "material" if count else "clear"
    except OSError:
        return 0, "unreadable"


def _params(scope: _Scope, user_id: str) -> tuple[str, ...]:
    return tuple(user_id for _ in range(scope.predicate.count("?")))


def _count(conn: sqlite3.Connection, scope: _Scope, user_id: str) -> int:
    # Table, predicate and column names come only from the closed constants above.
    row = conn.execute(
        f'SELECT COUNT(*) AS count FROM "{scope.table}" WHERE {scope.predicate}',  # nosec B608
        _params(scope, user_id),
    ).fetchone()
    return int(row["count"] if row else 0)


def _runtime_key_inventory(conn: sqlite3.Connection, user_id: str) -> tuple[list[str], list[str]]:
    """Return exact owned keys and opaque hashes of ambiguous future formats."""

    owned: list[str] = []
    ambiguous_hashes: list[str] = []
    for row in conn.execute("SELECT key FROM runtime_kv ORDER BY key"):
        key = str(row["key"])
        if key == DOCUMENT_CATALOG_WORKER_STATE_KEY:
            # Account ownership is hash-keyed inside this one shared row and is
            # inventoried independently below; substring matching its global key
            # would both miss ownership and falsely claim accounts named "catalog".
            continue
        owners = known_runtime_key_owners(key)
        if owners == {user_id}:
            owned.append(key)
        elif user_id in owners or (not owners and user_id in key):
            ambiguous_hashes.append(hashlib.sha256(key.encode("utf-8")).hexdigest())
    return owned, ambiguous_hashes


def _document_catalog_worker_runtime_inventory(
    conn: sqlite3.Connection,
    user_id: str,
) -> tuple[str | None, bool]:
    """Inventory one hash-owned cursor inside the worker's shared state row."""

    row = conn.execute(
        "SELECT value FROM runtime_kv WHERE key=?",
        (DOCUMENT_CATALOG_WORKER_STATE_KEY,),
    ).fetchone()
    if row is None:
        return None, True
    return document_catalog_worker_entry_fingerprint(
        row["value"],
        user_id,
        namespace_key=load_document_catalog_worker_namespace_key(conn),
    )


def _identity_tombstone_plan(
    conn: sqlite3.Connection, user: dict[str, Any]
) -> tuple[dict[str, tuple[str, str]], int]:
    """Canonical tombstones for the target, plus cross-account collisions."""

    user_id = str(user["id"])
    rows = conn.execute(
        "SELECT source,external_id FROM user_identities WHERE user_id=? ORDER BY source,external_id",
        (user_id,),
    ).fetchall()
    pairs = {(str(item["source"]), str(item["external_id"])) for item in rows}
    source = str(user.get("source") or "").strip()
    external_id = str(user.get("external_id") or "").strip()
    if source and external_id:
        pairs.add((source, external_id))
    planned = {
        deleted_identity_tombstone_key(source, external_id): (source, external_id)
        for source, external_id in pairs
    }

    collisions = 0
    other_rows = conn.execute(
        """SELECT source,external_id FROM user_identities WHERE user_id<>?
           UNION ALL
           SELECT source,external_id FROM users
            WHERE id<>? AND source<>'' AND external_id<>''""",
        (user_id, user_id),
    ).fetchall()
    for item in other_rows:
        try:
            key = deleted_identity_tombstone_key(item["source"], item["external_id"])
        except ValueError:
            continue
        if key in planned:
            collisions += 1
    return planned, collisions


def _mark_account_deletion_history_clean(storage: FridayStorage, user_id: str) -> bool:
    """Mark a newly admin-created local account as having no external queue history.

    This is deliberately not a general migration helper.  The admin-create route
    calls it only when it has just inserted a brand-new account.  The conditional
    write cannot race a Telegram link: whichever transaction commits last leaves
    the irreversible external-history marker authoritative.
    """

    user_id = validate_user_id(user_id)
    eligibility_key = account_deletion_eligibility_key(user_id)
    history_key = account_external_identity_history_key(user_id)
    with storage.transaction() as conn:
        row = conn.execute(
            "SELECT source,external_id,metadata_json FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if row is None:
            return False
        metadata: dict[str, Any] = {}
        try:
            parsed = json.loads(str(row["metadata_json"] or "{}"))
            if isinstance(parsed, dict):
                metadata = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        has_telegram_identity = conn.execute(
            "SELECT 1 FROM user_identities WHERE user_id=? AND lower(trim(source))='telegram'",
            (user_id,),
        ).fetchone()
        trusted_local_shape = (
            str(row["source"] or "").strip().casefold() in {"admin", "local"}
            and not user_id.casefold().startswith("telegram:")
            and not str(row["external_id"] or "").strip()
            and not str(metadata.get("chat_id") or "").strip()
            and has_telegram_identity is None
            and conn.execute("SELECT 1 FROM runtime_kv WHERE key=?", (history_key,)).fetchone() is None
        )
        if not trusted_local_shape:
            conn.execute("DELETE FROM runtime_kv WHERE key=?", (eligibility_key,))
            return False
        conn.execute(
            """INSERT INTO runtime_kv(key,value,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO NOTHING""",
            (eligibility_key, "{}", utc_now()),
        )
        return True


def _unknown_user_scopes(conn: sqlite3.Connection) -> list[str]:
    unknown: set[str] = set()
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for row in tables:
        table = str(row["name"])
        quoted = table.replace('"', '""')
        columns = {str(item["name"]) for item in conn.execute(f'PRAGMA table_info("{quoted}")')}  # nosec B608
        for column in sorted(columns):
            scope = (table, column)
            if (
                column in {"user_id", "principal", "account_id", "owner_id"}
                and scope not in (_KNOWN_USER_SCOPES | _KNOWN_ACTOR_REFERENCE_SCOPES)
            ) or (column.endswith("_by") and scope not in _KNOWN_ACTOR_REFERENCE_SCOPES):
                unknown.add(f"{table}.{column}")
        for foreign_key in conn.execute(f'PRAGMA foreign_key_list("{quoted}")'):  # nosec B608
            if str(foreign_key["table"]) != "users" or str(foreign_key["to"]) != "id":
                continue
            column = str(foreign_key["from"])
            scope = (table, column)
            if scope not in (_KNOWN_USER_SCOPES | _KNOWN_ACTOR_REFERENCE_SCOPES):
                unknown.add(f"{table}.{column}")
    return sorted(unknown)


def _supervisor_link_ids(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """Resolve hierarchy edges with the exact coercion used by supervisor_of."""

    result: list[str] = []
    for row in conn.execute("SELECT id,metadata_json FROM users WHERE id<>? ORDER BY id", (user_id,)):
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("supervisor_id") or "").strip() == user_id:
            result.append(str(row["id"]))
    return result


def _supervisor_links(conn: sqlite3.Connection, user_id: str) -> int:
    return len(_supervisor_link_ids(conn, user_id))


def _shared_owned_counts(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """Rows owned by this person but physically stored below another tenant.

    Shared-archive policy makes those rows ambiguous: erasing them changes the
    common corpus, retaining them violates the promise to erase the person.  The
    only safe automatic choice is to block and show the category to an owner.
    """

    queries = {
        "shared_uploads": """SELECT COUNT(*) AS count FROM raw_objects
             WHERE user_id<>? AND json_valid(metadata_json)
               AND (COALESCE(json_extract(metadata_json,'$.uploaded_by'),'') IN (?,?)
                    OR COALESCE(json_extract(metadata_json,'$.requested_by'),'') IN (?,?))""",
        "shared_missions": (
            "SELECT COUNT(*) AS count FROM missions WHERE user_id<>? AND created_by IN (?,?)"
        ),
        "shared_monitors": (
            "SELECT COUNT(*) AS count FROM monitors WHERE user_id<>? AND created_by IN (?,?)"
        ),
        "shared_approvals": (
            "SELECT COUNT(*) AS count FROM action_approvals WHERE user_id<>? AND requested_by IN (?,?)"
        ),
        "shared_data_sources": (
            "SELECT COUNT(*) AS count FROM data_sources WHERE user_id<>? AND created_by IN (?,?)"
        ),
        "shared_private_entities": (
            "SELECT COUNT(*) AS count FROM private_entity_owners owner "
            "JOIN entities entity ON entity.id=owner.entity_id "
            "WHERE entity.user_id<>? AND owner.person_id=?"
        ),
        "shared_legacy_reminders": (
            "SELECT COUNT(*) AS count FROM entity_time timed "
            "JOIN entities entity ON entity.id=timed.entity_id "
            "WHERE entity.user_id<>? AND timed.source=?"
        ),
        "global_presets": "SELECT COUNT(*) AS count FROM custom_presets WHERE created_by IN (?,?)",
    }
    result: dict[str, int] = {}
    actor_ids = (user_id, f"agent:{user_id}")
    for key, sql in queries.items():
        params: tuple[str, ...]
        if key == "shared_private_entities":
            params = (user_id, user_id)
        elif key == "shared_legacy_reminders":
            params = (user_id, f"reminder:{user_id}")
        elif key in {"global_presets"}:
            params = actor_ids
        elif sql.count("?") == 3:
            params = (user_id, *actor_ids)
        elif sql.count("?") == 5:
            params = (user_id, *actor_ids, *actor_ids)
        else:
            params = tuple(user_id for _ in range(sql.count("?")))
        row = conn.execute(sql, params).fetchone()
        result[key] = int(row["count"] if row else 0)
    return result


def _cross_account_reference_counts(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """Attribution in somebody else's row cannot be erased or silently reassigned."""

    queries = {
        "identity_links_created": (
            "SELECT COUNT(*) AS count FROM user_identities WHERE user_id<>:user_id "
            "AND linked_by IN (:user_id,:agent_id)"
        ),
        "inbox_reviews": (
            "SELECT COUNT(*) AS count FROM inbox WHERE user_id<>:user_id "
            "AND reviewed_by IN (:user_id,:agent_id)"
        ),
        "knowledge_link_reviews": (
            "SELECT COUNT(*) AS count FROM knowledge_entity_links WHERE user_id<>:user_id "
            "AND reviewed_by IN (:user_id,:agent_id)"
        ),
        "entity_resolutions": (
            "SELECT COUNT(*) AS count FROM entity_resolution_candidates WHERE user_id<>:user_id "
            "AND resolved_by IN (:user_id,:agent_id)"
        ),
        "entity_merges": (
            "SELECT COUNT(*) AS count FROM entity_merge_history WHERE user_id<>:user_id "
            "AND (merged_by IN (:user_id,:agent_id) OR undone_by IN (:user_id,:agent_id))"
        ),
        "relation_reviews": (
            "SELECT COUNT(*) AS count FROM relation_candidates WHERE user_id<>:user_id "
            "AND reviewed_by IN (:user_id,:agent_id)"
        ),
        "conflict_reviews": (
            "SELECT COUNT(*) AS count FROM knowledge_conflicts WHERE user_id<>:user_id "
            "AND reviewed_by IN (:user_id,:agent_id)"
        ),
        "tokens_created": (
            "SELECT COUNT(*) AS count FROM api_tokens WHERE user_id<>:user_id "
            "AND created_by IN (:user_id,:agent_id)"
        ),
        "approval_decisions": (
            "SELECT COUNT(*) AS count FROM action_approvals WHERE user_id<>:user_id "
            "AND decided_by IN (:user_id,:agent_id)"
        ),
    }
    params = {"user_id": user_id, "agent_id": f"agent:{user_id}"}
    result: dict[str, int] = {}
    for key, sql in queries.items():
        row = conn.execute(sql, params).fetchone()
        result[key] = int(row["count"] if row else 0)
    return result


_ACCOUNT_ROW_SCOPES = {
    scope.table: scope
    for scope in (
        *_DELETE_SCOPES,
        *_BLOCKING_GRAPH_SCOPES,
        *_BLOCKING_CHAT_SCOPES,
        *_BLOCKING_HOST_ACTION_SCOPES,
    )
}
_ACCOUNT_ROW_SCOPES["users"] = _Scope("users", "users", "id=?")


def _incoming_foreign_object_counts(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """Incoming FKs from rows outside the target cascade, discovered from schema."""

    result: dict[str, int] = {}
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for table_row in tables:
        table = str(table_row["name"])
        reference_scope = _ACCOUNT_ROW_SCOPES.get(table)
        quoted_table = table.replace('"', '""')
        for foreign_key in conn.execute(f'PRAGMA foreign_key_list("{quoted_table}")'):  # nosec B608
            if int(foreign_key["seq"]) != 0:
                continue
            target_table = str(foreign_key["table"])
            if target_table == "users":
                continue
            target_scope = _ACCOUNT_ROW_SCOPES.get(target_table)
            if target_scope is None:
                continue
            source_column = str(foreign_key["from"])
            target_column = str(foreign_key["to"] or "id")
            safe_source = source_column.replace('"', '""')
            safe_target = target_column.replace('"', '""')
            safe_target_table = target_table.replace('"', '""')
            outside_cascade = f" AND NOT ({reference_scope.predicate})" if reference_scope is not None else ""
            sql = (
                f'SELECT COUNT(*) AS count FROM "{quoted_table}" '
                f'WHERE "{safe_source}" IN (SELECT "{safe_target}" FROM "{safe_target_table}" '
                f"WHERE {target_scope.predicate}){outside_cascade}"
            )
            params = _params(target_scope, user_id)
            if reference_scope is not None:
                params = (*params, *_params(reference_scope, user_id))
            row = conn.execute(sql, params).fetchone()  # nosec B608 - schema identifiers quoted
            count = int(row["count"] if row else 0)
            if count:
                result[f"{table}.{source_column}->{target_table}.{target_column}"] = count
    return result


def _non_fk_object_reference_counts(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """Closed inventory of id-like columns which deliberately have no FK."""

    queries = {
        "knowledge_entity": """SELECT COUNT(*) AS count FROM knowledge_objects
            WHERE user_id<>? AND entity_id IN (SELECT id FROM entities WHERE user_id=?)""",
        "knowledge_superseded": """SELECT COUNT(*) AS count FROM knowledge_objects
            WHERE user_id<>? AND superseded_by_id IN
                  (SELECT id FROM knowledge_objects WHERE user_id=?)""",
        "inbox_suggested_entity": """SELECT COUNT(*) AS count FROM inbox
            WHERE user_id<>? AND suggested_entity_id IN (SELECT id FROM entities WHERE user_id=?)""",
        "entity_merged_into": """SELECT COUNT(*) AS count FROM entities
            WHERE user_id<>? AND merged_into_id IN (SELECT id FROM entities WHERE user_id=?)""",
        "merge_history_entities": """SELECT COUNT(*) AS count FROM entity_merge_history
            WHERE user_id<>? AND (source_entity_id IN (SELECT id FROM entities WHERE user_id=?)
               OR target_entity_id IN (SELECT id FROM entities WHERE user_id=?))""",
        "mission_task_inbox": """SELECT COUNT(*) AS count FROM mission_tasks
            WHERE user_id<>? AND inbox_id IN (SELECT id FROM inbox WHERE user_id=?)""",
        "approval_conversation": """SELECT COUNT(*) AS count FROM action_approvals
            WHERE user_id<>? AND conversation_id IN
                  (SELECT id FROM conversations WHERE user_id=?)""",
        "approval_mission": """SELECT COUNT(*) AS count FROM action_approvals
            WHERE user_id<>? AND mission_id IN (SELECT id FROM missions WHERE user_id=?)""",
        "message_reply": """SELECT COUNT(*) AS count FROM messages
            WHERE user_id<>? AND reply_to IN (SELECT id FROM messages WHERE user_id=?)""",
        "relation_superseded": """SELECT COUNT(*) AS count FROM relations
            WHERE user_id<>? AND superseded_by IN (SELECT id FROM relations WHERE user_id=?)""",
        "relation_revision_endpoints": """SELECT COUNT(*) AS count FROM relation_revisions
            WHERE user_id<>? AND (source_entity_id IN (SELECT id FROM entities WHERE user_id=?)
               OR target_entity_id IN (SELECT id FROM entities WHERE user_id=?))""",
        "feedback_targets": """SELECT COUNT(*) AS count FROM feedback
            WHERE user_id<>? AND target_id IN (
                SELECT id FROM raw_objects WHERE user_id=?
                UNION SELECT id FROM knowledge_objects WHERE user_id=?
                UNION SELECT id FROM inbox WHERE user_id=?
                UNION SELECT id FROM entities WHERE user_id=?
                UNION SELECT id FROM relations WHERE user_id=?
                UNION SELECT id FROM conversations WHERE user_id=?
                UNION SELECT id FROM messages WHERE user_id=?
                UNION SELECT id FROM missions WHERE user_id=?
                UNION SELECT id FROM relation_candidates WHERE user_id=?
                UNION SELECT id FROM entity_resolution_candidates WHERE user_id=?)""",
        "feedback_state_targets": """SELECT COUNT(*) AS count FROM feedback_state
            WHERE user_id<>? AND target_id IN (
                SELECT id FROM raw_objects WHERE user_id=?
                UNION SELECT id FROM knowledge_objects WHERE user_id=?
                UNION SELECT id FROM inbox WHERE user_id=?
                UNION SELECT id FROM entities WHERE user_id=?
                UNION SELECT id FROM relations WHERE user_id=?
                UNION SELECT id FROM conversations WHERE user_id=?
                UNION SELECT id FROM messages WHERE user_id=?
                UNION SELECT id FROM missions WHERE user_id=?
                UNION SELECT id FROM relation_candidates WHERE user_id=?
                UNION SELECT id FROM entity_resolution_candidates WHERE user_id=?)""",
    }
    result: dict[str, int] = {}
    for key, sql in queries.items():
        row = conn.execute(sql, tuple(user_id for _ in range(sql.count("?")))).fetchone()
        count = int(row["count"] if row else 0)
        if count:
            result[key] = count

    target_knowledge_ids = {
        str(row["id"]) for row in conn.execute("SELECT id FROM knowledge_objects WHERE user_id=?", (user_id,))
    }
    eval_case_count = 0
    for row in conn.execute("SELECT expected_ids_json FROM eval_cases WHERE user_id<>?", (user_id,)):
        try:
            expected_ids = json.loads(str(row["expected_ids_json"] or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            eval_case_count += 1
            continue
        if (
            not isinstance(expected_ids, list)
            or len(expected_ids) > 10_000
            or any(not isinstance(item, str) for item in expected_ids)
        ):
            eval_case_count += 1
            continue
        if target_knowledge_ids.intersection(expected_ids):
            eval_case_count += 1
    if eval_case_count:
        result["eval_case_expected_ids"] = eval_case_count
    return result


_CROSS_ACCOUNT_JSON_SCOPES = (
    ("work_items", "active_frame_json", "user_id"),
    ("users", "metadata_json", "id"),
    ("raw_objects", "metadata_json", "user_id"),
    ("knowledge_objects", "metadata_json", "user_id"),
    ("inbox", "suggestions_json", "user_id"),
    ("entities", "metadata_json", "user_id"),
    ("knowledge_entity_links", "evidence_json", "user_id"),
    ("relations", "metadata_json", "user_id"),
    ("relation_revisions", "metadata_json", "user_id"),
    ("entity_resolution_candidates", "evidence_json", "user_id"),
    ("feedback", "context_json", "user_id"),
    ("relation_candidates", "evidence_json", "user_id"),
    ("knowledge_conflicts", "evidence_json", "user_id"),
    ("missions", "metadata_json", "user_id"),
    ("action_approvals", "payload_json", "user_id"),
    ("action_approvals", "result_json", "user_id"),
    ("mission_tasks", "checkpoint_json", "user_id"),
    ("mission_tasks", "depends_on_json", "user_id"),
    ("mission_tasks", "tools_used_json", "user_id"),
)

_WORK_ITEM_EVIDENCE_JSON_SCOPES = (
    (
        "work_item_compare_document_evidence",
        ("source_ref_json",),
    ),
    (
        "work_item_selected_evidence",
        ("source_ref_json", "passage_refs_json"),
    ),
    (
        "work_item_archive_candidate_set_items",
        ("source_ref_json", "passage_refs_json"),
    ),
)

# Object ids are also persisted inside executable approval/checkpoint arguments.
# These columns have no declarative FK, so the generic incoming-FK inventory
# cannot protect them.  Reusing the full provenance registry is intentionally
# conservative: an exact generated id in another tenant's JSON is a dependency,
# even when a newer writer no longer reads that particular field.
_STRUCTURAL_JSON_REFERENCE_SCOPES = _CROSS_ACCOUNT_JSON_SCOPES

_MERGE_HISTORY_JSON_COLUMNS = (
    "source_snapshot_json",
    "target_before_json",
    "target_after_json",
    "transfer_json",
)
_SNAPSHOT_MAGIC = b"zKOV1"
_MAX_PROVENANCE_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_PROVENANCE_JSON_CHARS = _MAX_PROVENANCE_SNAPSHOT_BYTES // 4


def _snapshot_text_bounded(value: Any) -> str:
    """Decode legacy/current version snapshots without an unbounded zlib inflate."""

    if isinstance(value, bytes):
        if value.startswith(_SNAPSHOT_MAGIC):
            decompressor = zlib.decompressobj()
            raw = decompressor.decompress(
                value[len(_SNAPSHOT_MAGIC) :],
                _MAX_PROVENANCE_SNAPSHOT_BYTES + 1,
            )
            if (
                len(raw) > _MAX_PROVENANCE_SNAPSHOT_BYTES
                or decompressor.unconsumed_tail
                or not decompressor.eof
                or decompressor.unused_data
            ):
                raise ValueError("version snapshot exceeds the bounded provenance scan")
            return raw.decode("utf-8", errors="strict")
        if len(value) > _MAX_PROVENANCE_SNAPSHOT_BYTES:
            raise ValueError("version snapshot exceeds the bounded provenance scan")
        return value.decode("utf-8", errors="strict")
    text = str(value or "")
    if len(text.encode("utf-8", errors="strict")) > _MAX_PROVENANCE_SNAPSHOT_BYTES:
        raise ValueError("version snapshot exceeds the bounded provenance scan")
    return text


def _json_has_exact_person_reference(
    raw: str,
    references: frozenset[str],
    *,
    ignored_root_values: Mapping[str, frozenset[str]] | None = None,
) -> bool:
    """Inspect JSON plus bounded JSON-encoded strings for exact person ids.

    Merge snapshots deliberately serialize fields such as ``metadata_json`` as
    strings inside an outer JSON document.  SQLite's ``json_tree`` stops at that
    string, so a second, bounded decode is required.  A document that exceeds the
    traversal budget is treated as referenced: account erasure must fail closed
    rather than pronounce an uninspected history row safe.
    """

    # A Python Unicode code point occupies at most four UTF-8 bytes.  The
    # conservative character cap therefore bounds parsing without first making
    # another potentially huge encoded copy merely to measure it.
    if len(raw) > _MAX_PROVENANCE_JSON_CHARS:
        return True
    try:
        root: Any = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return True
    if ignored_root_values and isinstance(root, dict):
        root = dict(root)
        for key, allowed_values in ignored_root_values.items():
            value = root.get(key)
            if not isinstance(value, (dict, list)) and str(value or "").strip() in allowed_values:
                root.pop(key, None)
    if not isinstance(root, (dict, list)):
        return True
    stack: list[tuple[Any, int, bool]] = [(root, 0, False)]
    visited = 0
    while stack:
        value, encoded_depth, decode_string = stack.pop()
        visited += 1
        if visited > 10_000:
            return True
        if isinstance(value, dict):
            if any(str(key) in references for key in value):
                return True
            stack.extend(
                (item, encoded_depth, str(key).casefold().endswith("_json")) for key, item in value.items()
            )
        elif isinstance(value, list):
            stack.extend((item, encoded_depth, decode_string) for item in value)
        elif isinstance(value, str):
            if value in references:
                return True
            candidate = value.strip()
            if not decode_string or not candidate.startswith(("{", "[")):
                continue
            if encoded_depth >= 4:
                return True
            try:
                decoded = json.loads(candidate)
            except (TypeError, ValueError, json.JSONDecodeError):
                return True
            stack.append((decoded, encoded_depth + 1, False))
    return False


def _cross_account_structural_json_reference_counts(
    conn: sqlite3.Connection,
    user_id: str,
) -> dict[str, int]:
    """Executable/provenance JSON in another tenant referencing target rows.

    Approval payloads and mission checkpoints intentionally carry polymorphic
    object ids without FKs.  Build the reference set only from the closed account
    cascade and inspect the closed JSON registry with the same bounded recursive
    decoder used for actor provenance.  Malformed/opaque rows fail closed.
    """

    references: set[str] = set()
    for table, scope in sorted(_ACCOUNT_ROW_SCOPES.items()):
        if table == "users":
            continue
        quoted_table = table.replace('"', '""')
        columns = {
            str(row["name"])
            for row in conn.execute(f'PRAGMA table_info("{quoted_table}")')  # nosec B608
        }
        if "id" not in columns:
            continue
        rows = conn.execute(  # nosec B608 - table and predicate are closed constants
            f'SELECT id FROM "{quoted_table}" WHERE {scope.predicate}',
            _params(scope, user_id),
        ).fetchall()
        references.update(str(row["id"]) for row in rows if row["id"] not in (None, ""))
    if not references:
        return {}

    exact_references = frozenset(references)
    result: dict[str, int] = {}
    for table, column, owner_column in _STRUCTURAL_JSON_REFERENCE_SCOPES:
        rows = conn.execute(
            f'''SELECT item."{column}" AS payload FROM "{table}" item
                  WHERE item."{owner_column}"<>?''',  # nosec B608 - closed registry
            (user_id,),
        ).fetchall()
        count = sum(
            1
            for row in rows
            if row["payload"] not in (None, "")
            and _json_has_exact_person_reference(str(row["payload"] or ""), exact_references)
        )
        if count:
            result[f"structural_json:{table}.{column}"] = count

    for table, evidence_columns in _WORK_ITEM_EVIDENCE_JSON_SCOPES:
        for column in evidence_columns:
            rows = conn.execute(
                f'''SELECT evidence."{column}" AS payload
                      FROM "{table}" evidence
                      JOIN work_items work ON work.id=evidence.work_item_id
                     WHERE work.user_id<>?''',  # nosec B608 - closed registry
                (user_id,),
            ).fetchall()
            count = sum(
                1
                for row in rows
                if row["payload"] not in (None, "")
                and _json_has_exact_person_reference(
                    str(row["payload"] or ""),
                    exact_references,
                )
            )
            if count:
                result[f"structural_json:{table}.{column}"] = count

    for table in ("entity_versions", "knowledge_object_versions"):
        rows = conn.execute(
            f'''SELECT snapshot_json FROM "{table}" WHERE user_id<>?''',  # nosec B608
            (user_id,),
        ).fetchall()
        count = 0
        for row in rows:
            try:
                snapshot = _snapshot_text_bounded(row["snapshot_json"])
            except (OSError, UnicodeError, ValueError, zlib.error):
                count += 1
                continue
            if _json_has_exact_person_reference(snapshot, exact_references):
                count += 1
        if count:
            result[f"structural_json:{table}.snapshot_json"] = count

    for column in _MERGE_HISTORY_JSON_COLUMNS:
        rows = conn.execute(
            f'''SELECT "{column}" AS payload FROM entity_merge_history
                 WHERE user_id<>?''',  # nosec B608 - fixed registry above
            (user_id,),
        ).fetchall()
        count = sum(
            1 for row in rows if _json_has_exact_person_reference(str(row["payload"] or ""), exact_references)
        )
        if count:
            result[f"structural_json:entity_merge_history.{column}"] = count

    runtime_count = 0
    for row in conn.execute("SELECT payload FROM runtime_events WHERE json_valid(payload)"):
        if _json_has_exact_person_reference(str(row["payload"] or ""), exact_references):
            runtime_count += 1
    if runtime_count:
        result["structural_json:runtime_events.payload"] = runtime_count
    return result


def _cross_account_json_reference_counts(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """Exact person-id values embedded in another tenant's JSON provenance."""

    actor_id = f"agent:{user_id}"
    references = frozenset((user_id, actor_id))
    result: dict[str, int] = {}
    for table, column, owner_column in _CROSS_ACCOUNT_JSON_SCOPES:
        # Identifiers come only from the fixed registry above.  Python decoding
        # is deliberate: SQLite json_tree skips both malformed legacy rows and
        # JSON serialized inside a JSON string.  Either shape must fail closed.
        rows = conn.execute(
            f'''SELECT item."{column}" AS payload FROM "{table}" item
                  WHERE item."{owner_column}"<>?''',  # nosec B608
            (user_id,),
        ).fetchall()
        ignored_root_values = {"supervisor_id": frozenset((user_id,))} if table == "users" else None
        count = sum(
            1
            for row in rows
            if row["payload"] not in (None, "")
            and _json_has_exact_person_reference(
                str(row["payload"] or ""),
                references,
                ignored_root_values=ignored_root_values,
            )
        )
        if count:
            result[f"{table}.{column}"] = count

    for table, evidence_columns in _WORK_ITEM_EVIDENCE_JSON_SCOPES:
        for column in evidence_columns:
            rows = conn.execute(
                f'''SELECT evidence."{column}" AS payload
                      FROM "{table}" evidence
                      JOIN work_items work ON work.id=evidence.work_item_id
                     WHERE work.user_id<>?''',  # nosec B608 - closed registry
                (user_id,),
            ).fetchall()
            count = sum(
                1
                for row in rows
                if row["payload"] not in (None, "")
                and _json_has_exact_person_reference(str(row["payload"] or ""), references)
            )
            if count:
                result[f"{table}.{column}"] = count

    for table in ("entity_versions", "knowledge_object_versions"):
        rows = conn.execute(
            f'''SELECT snapshot_json FROM "{table}" WHERE user_id<>?''',  # nosec B608
            (user_id,),
        ).fetchall()
        count = 0
        for row in rows:
            try:
                snapshot = _snapshot_text_bounded(row["snapshot_json"])
            except (OSError, UnicodeError, ValueError, zlib.error):
                count += 1
                continue
            if _json_has_exact_person_reference(snapshot, references):
                count += 1
        if count:
            result[f"{table}.snapshot_json"] = count

    for column in _MERGE_HISTORY_JSON_COLUMNS:
        rows = conn.execute(
            f'''SELECT "{column}" AS payload FROM entity_merge_history
                 WHERE user_id<>?''',  # nosec B608 - fixed column registry above
            (user_id,),
        ).fetchall()
        count = sum(
            1 for row in rows if _json_has_exact_person_reference(str(row["payload"] or ""), references)
        )
        if count:
            result[f"entity_merge_history.{column}"] = count
    return result


def _runtime_event_reference_count(conn: sqlite3.Connection, user_id: str, created_at: str) -> int:
    row = conn.execute(
        """SELECT COUNT(*) AS count FROM runtime_events event
           WHERE (json_valid(event.payload)
                  AND EXISTS (SELECT 1 FROM json_tree(event.payload) node
                       WHERE node.type='text' AND node.atom IN (?,?)))
              OR (event.event_type='graph.entities_pruned'
                  AND event.created_at>=?
                  AND CASE
                        WHEN NOT json_valid(event.payload) THEN 1
                        WHEN json_type(event.payload,'$.user_id')='text' THEN 0
                        WHEN json_type(event.payload,'$.user_ids')='array'
                         AND json_array_length(event.payload,'$.user_ids')>0
                         AND NOT EXISTS (
                             SELECT 1 FROM json_each(event.payload,'$.user_ids') owner
                              WHERE owner.type<>'text'
                         ) THEN 0
                        ELSE 1
                      END=1)
              OR (event.created_at>=? AND NOT json_valid(event.payload))""",
        (user_id, f"agent:{user_id}", created_at, created_at),
    ).fetchone()
    # Older versions retained document-derived entity names without recording
    # which account supplied them.  Such an event predating this account cannot
    # describe it; an unscoped event during its lifetime remains a blocker.
    return int(row["count"] if row else 0)


def _cross_account_chat_derivative_count(conn: sqlite3.Connection, user_id: str) -> int:
    """Count durable proof that another account received target-derived material.

    The assistant message contains a synthesis, so it cannot be matched back to
    the source account reliably.  The oversight tools do, however, append an
    audit row naming that exact source before returning the material.  Treat that
    durable marker as an irreversible cross-account derivative and block instead
    of claiming that the target's data has been completely erased.
    """

    row = conn.execute(
        """SELECT COUNT(*) AS count FROM audit_log
            WHERE user_id<>? AND target_type='user' AND target_id=?
              AND action IN ('tool.user_activity','tool.user_knowledge_search')""",
        (user_id, user_id),
    ).fetchone()
    return int(row["count"] if row else 0)


_BLOCKER_MESSAGES = {
    "account_active": "Сначала отключите учётную запись и завершите её активные запросы.",
    "relation_history": "У аккаунта есть append-only история графа; её нельзя стирать обычным каскадом.",
    "chat_history": "У аккаунта есть неизменяемая история чата; действующая политика запрещает её стирать.",
    "host_action_history": "У аккаунта есть неизменяемая история Host Action; для неё ещё не определён отдельный безопасный каскад удаления.",
    "private_quarantine": "Часть графа находится в неизменяемом приватном карантине.",
    "active_operations": "У аккаунта есть выполняющиеся операции; сначала завершите или отмените их.",
    "stored_files": "У аккаунта есть сохранённые файлы; требуется согласованное файловое удаление.",
    "file_directory": "Каталог файлов не пуст, небезопасен или недоступен.",
    "vault_directory": "Проекция Memory Vault не пуста, небезопасна или недоступна.",
    "obsidian_directory": "Профиль или vault Obsidian не пуст; сначала остановите синхронизацию и удалите его согласованно.",
    "export_artifacts": "Ранее созданный JSON-экспорт аккаунта сохранён вне базы; сначала удалите его вручную.",
    "export_directory": "Каталог экспортов небезопасен или недоступен для проверки.",
    "shared_owned_data": "Часть данных создана внутри другого (общего) арендатора; нужна явная политика архива.",
    "cross_account_references": "ID аккаунта записан автором или ревьюером в данных другого арендатора; чужую историю нельзя менять автоматически.",
    "unknown_user_scope": "Схема содержит новый пользовательский контур, которого ещё нет в каскаде удаления.",
    "unknown_runtime_scope": "Найден пользовательский runtime-ключ неизвестного формата; автоматическое удаление небезопасно.",
    "identity_collision": "Способ входа совпадает с идентичностью другого аккаунта после канонизации.",
    "external_identity_state": "Для Telegram-идентичности есть внешний контур очереди; требуется отдельное согласованное удаление.",
    "unsupported_legacy_id": "Исторический ID аккаунта не соответствует текущему безопасному формату.",
    "quiescence_unavailable": "Для удаления перезапустите backend с отключёнными фоновыми воркерами; активные запросы аккаунта будут остановлены и дожаты автоматически.",
    "external_history_unverified": "Аккаунт создан до появления журнала внешнего контура; отсутствие старой Telegram-очереди доказать нельзя.",
    "runtime_event_history": "Операционный журнал содержит ссылку на аккаунт и не входит в транзакционный каскад.",
    "cross_account_chat_derivatives": "Другой аккаунт получал данные пользователя через инструменты надзора; их пересказ в чужой истории нельзя надёжно выделить и стереть.",
    "cross_account_object_references": "Объекты другого арендатора ссылаются на данные аккаунта; удаление создало бы битые ссылки.",
    "cross_account_json_references": "ID аккаунта сохранён в JSON-происхождении другого арендатора или его версиях.",
}


def _preflight(
    conn: sqlite3.Connection,
    storage: FridayStorage,
    user_id: str,
    *,
    quiescence_available: bool,
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise LookupError("Пользователь не найден")
    user = dict(row)

    counts: dict[str, int] = {scope.key: _count(conn, scope, user_id) for scope in _DELETE_SCOPES}
    graph_counts = {scope.key: _count(conn, scope, user_id) for scope in _BLOCKING_GRAPH_SCOPES}
    counts.update(graph_counts)
    chat_counts = {scope.key: _count(conn, scope, user_id) for scope in _BLOCKING_CHAT_SCOPES}
    counts.update(chat_counts)
    host_action_counts = {scope.key: _count(conn, scope, user_id) for scope in _BLOCKING_HOST_ACTION_SCOPES}
    counts.update(host_action_counts)
    private_row = conn.execute(
        """SELECT COUNT(*) AS count FROM private_entity_material_cache
             WHERE entity_id IN (SELECT id FROM entities WHERE user_id=?)""",
        (user_id,),
    ).fetchone()
    private_quarantine = int(private_row["count"] if private_row else 0)
    if private_quarantine:
        counts["private_quarantine"] = private_quarantine
    active_row = conn.execute(
        """SELECT
             (SELECT COUNT(*) FROM missions WHERE user_id=? AND status='running')
           + (SELECT COUNT(*) FROM mission_tasks
               WHERE user_id=? AND status IN ('running','uncertain'))
           + (SELECT COUNT(*) FROM action_approvals
               WHERE user_id=? AND status IN ('claimed','uncertain'))
           + (SELECT COUNT(*) FROM request_idempotency
               WHERE user_id=? AND state='pending') AS count""",
        (user_id, user_id, user_id, user_id),
    ).fetchone()
    active_operations = int(active_row["count"] if active_row else 0)
    if active_operations:
        counts["active_operations"] = active_operations
    counts["users"] = 1

    runtime_keys, ambiguous_runtime_hashes = _runtime_key_inventory(conn, user_id)
    counts["runtime_kv"] = len(runtime_keys)
    catalog_worker_fingerprint, catalog_worker_state_supported = _document_catalog_worker_runtime_inventory(
        conn, user_id
    )
    if catalog_worker_fingerprint is not None:
        counts["document_catalog_worker_state"] = 1

    raw_files_row = conn.execute(
        "SELECT COUNT(*) AS count FROM raw_objects WHERE user_id=? AND content_type='file'",
        (user_id,),
    ).fetchone()
    raw_file_records = int(raw_files_row["count"] if raw_files_row else 0)
    shared_owned = _shared_owned_counts(conn, user_id)
    cross_account_references = _cross_account_reference_counts(conn, user_id)
    incoming_foreign_objects = _incoming_foreign_object_counts(conn, user_id)
    non_fk_object_references = _non_fk_object_reference_counts(conn, user_id)
    non_fk_object_references.update(_cross_account_structural_json_reference_counts(conn, user_id))
    cross_account_json_references = _cross_account_json_reference_counts(conn, user_id)
    runtime_event_references = _runtime_event_reference_count(
        conn,
        user_id,
        str(user.get("created_at") or ""),
    )
    cross_account_chat_derivatives = _cross_account_chat_derivative_count(conn, user_id)
    if runtime_event_references:
        counts["runtime_events_retained"] = runtime_event_references
    unknown_scopes = _unknown_user_scopes(conn)
    supervisor_links = _supervisor_links(conn, user_id)
    identity_tombstones, identity_collisions = _identity_tombstone_plan(conn, user)
    eligibility_key = ""
    external_history_key = ""
    account_id_supported = True
    try:
        eligibility_key = account_deletion_eligibility_key(user_id)
        external_history_key = account_external_identity_history_key(user_id)
    except ValueError:
        account_id_supported = False
    deletion_history_clean = bool(
        eligibility_key
        and conn.execute("SELECT 1 FROM runtime_kv WHERE key=?", (eligibility_key,)).fetchone()
    )
    external_history_recorded = bool(
        external_history_key
        and conn.execute("SELECT 1 FROM runtime_kv WHERE key=?", (external_history_key,)).fetchone()
    )
    if deletion_history_clean:
        counts["deletion_eligibility"] = 1
    metadata: dict[str, Any] = {}
    try:
        parsed_metadata = json.loads(str(user.get("metadata_json") or "{}"))
        if isinstance(parsed_metadata, dict):
            metadata = parsed_metadata
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    external_identity_state = sum(
        1 for source, _external_id in identity_tombstones.values() if source.strip().casefold() == "telegram"
    )
    chat_id = metadata.get("chat_id")
    if chat_id is not None and str(chat_id) and not external_identity_state:
        external_identity_state = 1
    if external_history_recorded:
        external_identity_state += 1

    files_dir = _account_files_directory(storage, user_id)
    export_artifacts, export_state = _account_export_artifacts(storage, user_id)
    path_states = {
        "files": _path_state(files_dir),
        "vault": _vault_account_state(storage, user_id),
        "obsidian": _path_state(_obsidian_account_directory(storage, user_id)),
        "exports": export_state,
    }
    if export_artifacts:
        counts["exports_retained"] = export_artifacts

    blockers: list[dict[str, Any]] = []

    def block(code: str, count: int = 1) -> None:
        blockers.append({"code": code, "count": int(count), "message": _BLOCKER_MESSAGES[code]})

    if str(user.get("status") or "") != "disabled":
        block("account_active")
    if not quiescence_available:
        block("quiescence_unavailable")
    if not account_id_supported:
        block("unsupported_legacy_id")
    elif not deletion_history_clean:
        block("external_history_unverified")
    graph_total = graph_counts["relations"] + graph_counts["relation_revisions"]
    if graph_total:
        block("relation_history", graph_total)
    chat_total = chat_counts["messages"] + chat_counts["conversations"]
    if chat_total:
        block("chat_history", chat_total)
    host_action_total = host_action_counts["host_action_jobs"] + host_action_counts["host_action_events"]
    if host_action_total:
        block("host_action_history", host_action_total)
    if private_quarantine:
        block("private_quarantine", private_quarantine)
    if active_operations:
        block("active_operations", active_operations)
    if raw_file_records:
        block("stored_files", raw_file_records)
    if path_states["files"] not in {"absent", "empty"}:
        block("file_directory")
    if path_states["vault"] not in {"absent", "empty"}:
        block("vault_directory")
    if path_states["obsidian"] not in {"absent", "empty"}:
        block("obsidian_directory")
    if export_artifacts:
        block("export_artifacts", export_artifacts)
    if export_state in {"unsafe", "unreadable"}:
        block("export_directory")
    shared_total = sum(shared_owned.values())
    if shared_total:
        block("shared_owned_data", shared_total)
    cross_reference_total = sum(cross_account_references.values())
    if cross_reference_total:
        block("cross_account_references", cross_reference_total)
    cross_object_total = sum(incoming_foreign_objects.values()) + sum(non_fk_object_references.values())
    if cross_object_total:
        block("cross_account_object_references", cross_object_total)
    cross_json_total = sum(cross_account_json_references.values())
    if cross_json_total:
        block("cross_account_json_references", cross_json_total)
    if runtime_event_references:
        block("runtime_event_history", runtime_event_references)
    if cross_account_chat_derivatives:
        block("cross_account_chat_derivatives", cross_account_chat_derivatives)
    unknown_runtime_count = len(ambiguous_runtime_hashes) + int(not catalog_worker_state_supported)
    if unknown_runtime_count:
        block("unknown_runtime_scope", unknown_runtime_count)
    if identity_collisions:
        block("identity_collision", identity_collisions)
    if external_identity_state:
        block("external_identity_state", external_identity_state)
    if unknown_scopes:
        block("unknown_user_scope", len(unknown_scopes))

    retained_audit_row = conn.execute(
        """SELECT COUNT(*) AS count FROM audit_log entry
             WHERE user_id=? OR target_id=?
                OR (json_valid(before_json) AND EXISTS (
                    SELECT 1 FROM json_tree(before_json) node
                     WHERE node.type='text' AND node.atom IN (?,?)))
                OR (json_valid(after_json) AND EXISTS (
                    SELECT 1 FROM json_tree(after_json) node
                     WHERE node.type='text' AND node.atom IN (?,?)))""",
        (
            user_id,
            user_id,
            user_id,
            f"agent:{user_id}",
            user_id,
            f"agent:{user_id}",
        ),
    ).fetchone()
    retained = {"audit_log": int(retained_audit_row["count"] if retained_audit_row else 0)}
    nonzero_counts = {key: value for key, value in counts.items() if value}
    deletion_keys = {scope.key for scope in _DELETE_SCOPES} | {
        "deletion_eligibility",
        "document_catalog_worker_state",
        "runtime_kv",
        "users",
    }
    planned_delete_rows = sum(value for key, value in counts.items() if key in deletion_keys)
    basis = {
        "user": {
            "id": user_id,
            "status": str(user.get("status") or ""),
            "preset_key": str(user.get("preset_key") or ""),
            "source": str(user.get("source") or ""),
            "updated_at": str(user.get("updated_at") or ""),
        },
        "counts": nonzero_counts,
        "blockers": [{"code": item["code"], "count": item["count"]} for item in blockers],
        "retained": retained,
        "shared_owned": shared_owned,
        "cross_account_references": cross_account_references,
        "incoming_foreign_objects": incoming_foreign_objects,
        "non_fk_object_references": non_fk_object_references,
        "cross_account_json_references": cross_account_json_references,
        "runtime_event_references": runtime_event_references,
        "cross_account_chat_derivatives": cross_account_chat_derivatives,
        "unknown_scopes": unknown_scopes,
        "supervisor_links": supervisor_links,
        "path_states": path_states,
        "runtime_key_hashes": [hashlib.sha256(key.encode("utf-8")).hexdigest() for key in runtime_keys],
        "ambiguous_runtime_hashes": ambiguous_runtime_hashes,
        "document_catalog_worker_fingerprint": catalog_worker_fingerprint,
        "document_catalog_worker_state_supported": catalog_worker_state_supported,
        # Keys are opaque hashes.  Including them makes an identity swap with the
        # same row count invalidate the reviewed plan without exposing login ids.
        "identity_tombstone_keys": sorted(identity_tombstones),
        "identity_collisions": identity_collisions,
        "external_identity_state": external_identity_state,
        "external_history_recorded": external_history_recorded,
        "deletion_history_clean": deletion_history_clean,
        "quiescence_available": quiescence_available,
    }
    fingerprint = hashlib.sha256(
        json.dumps(basis, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "user_id": user_id,
        "ready": not blockers,
        "fingerprint": fingerprint,
        "counts": nonzero_counts,
        "planned_delete_rows": planned_delete_rows,
        "affected_other_rows": {"supervisor_links_removed": supervisor_links},
        "identity_tombstones_planned": len(identity_tombstones),
        "retained": retained,
        "blockers": blockers,
        "shared_owned": {key: value for key, value in shared_owned.items() if value},
        "cross_account_references": {key: value for key, value in cross_account_references.items() if value},
        "cross_account_object_references": {
            "foreign_keys": incoming_foreign_objects,
            "non_fk": non_fk_object_references,
        },
        "cross_account_json_references": cross_account_json_references,
        "runtime_event_references": runtime_event_references,
        "cross_account_chat_derivatives": cross_account_chat_derivatives,
        "unknown_scopes": unknown_scopes,
        "document_catalog_worker_fingerprint": catalog_worker_fingerprint,
    }


def preflight_account_deletion(
    storage: FridayStorage,
    user_id: str,
    *,
    quiescence_available: bool = False,
) -> dict[str, Any]:
    """Return a content-free, race-detecting deletion plan."""

    return _preflight(
        storage.conn,
        storage,
        user_id,
        quiescence_available=quiescence_available,
    )


def _delete_scope(conn: sqlite3.Connection, scope: _Scope, user_id: str) -> int:
    cursor = conn.execute(
        f'DELETE FROM "{scope.table}" WHERE {scope.predicate}',  # nosec B608
        _params(scope, user_id),
    )
    return max(0, int(cursor.rowcount))


def _protected_account_reason(user: dict[str, Any], actor_user_id: str) -> str:
    user_id = str(user.get("id") or "")
    if user_id == actor_user_id:
        return "Нельзя удалить учётную запись текущего администратора"
    if user_id == LEGACY_OWNER_USER_ID or str(user.get("preset_key") or "") == "owner":
        return "Учётную запись владельца нельзя удалить"
    metadata: dict[str, Any] = {}
    try:
        parsed = json.loads(str(user.get("metadata_json") or "{}"))
        if isinstance(parsed, dict):
            metadata = parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    if str(user.get("source") or "").casefold() == "system" or metadata.get("system_account") is True:
        return "Системную учётную запись нельзя удалить"
    return ""


def delete_account(
    storage: FridayStorage,
    user_id: str,
    *,
    expected_fingerprint: str,
    actor_user_id: str,
    ip_address: str = "",
    request_id: str = "",
    authorization_check: Callable[[], None] | None = None,
    quiescence_verified: bool = False,
) -> dict[str, Any]:
    """Atomically erase one preflight-approved account and append its audit row."""

    deleted: dict[str, int] = {}
    supervisor_links_removed = 0
    identity_tombstones_created = 0
    audit_id = new_id("audit")
    with storage.transaction() as conn:
        report = _preflight(
            conn,
            storage,
            user_id,
            quiescence_available=quiescence_verified,
        )
        if not report["ready"]:
            raise AccountDeletionBlocked(report)
        if not expected_fingerprint or report["fingerprint"] != expected_fingerprint:
            raise AccountDeletionConflict("Учётная запись изменилась; повторите предварительную проверку")

        user_row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if user_row is None:
            raise AccountDeletionConflict("Учётная запись исчезла во время удаления")
        user = dict(user_row)
        protected_reason = _protected_account_reason(user, actor_user_id)
        if protected_reason:
            raise AccountDeletionConflict(protected_reason)
        actor_row = conn.execute("SELECT status FROM users WHERE id=?", (actor_user_id,)).fetchone()
        if actor_row is None or str(actor_row["status"]) != "active":
            raise AccountDeletionConflict("Полномочия администратора изменились; войдите заново")
        if authorization_check is not None:
            authorization_check()
        identity_tombstones, identity_collisions = _identity_tombstone_plan(conn, user)
        if identity_collisions:
            raise AccountDeletionConflict("Способ входа теперь пересекается с другим аккаунтом")
        runtime_keys, ambiguous_runtime_hashes = _runtime_key_inventory(conn, user_id)
        if ambiguous_runtime_hashes:
            raise AccountDeletionConflict("Появился runtime-ключ неизвестного формата")

        # Candidate sidecars are deletion-immutable while their parent exists.
        # Snapshot global counts so the parent Work Item cascade remains exactly
        # accounted without opening a direct sidecar-delete bypass.
        candidate_cascade_scopes = tuple(
            scope for scope in _DELETE_SCOPES if scope.key in _CANDIDATE_CASCADE_DELETE_KEYS
        )
        candidate_cascade_counts = {
            scope.key: _count(conn, scope, user_id) for scope in candidate_cascade_scopes
        }
        candidate_global_counts = {
            scope.key: int(conn.execute(f'SELECT COUNT(*) FROM "{scope.table}"').fetchone()[0])
            for scope in candidate_cascade_scopes
        }

        # Remove every dependent row before its parent.  The scope list is also the
        # preflight count authority, so the result can prove exact accounting.
        for scope in _DELETE_SCOPES:
            if scope.key in _CANDIDATE_CASCADE_DELETE_KEYS:
                continue
            count = _delete_scope(conn, scope, user_id)
            if count:
                deleted[scope.key] = count
        for scope in candidate_cascade_scopes:
            expected = candidate_cascade_counts[scope.key]
            remaining = int(conn.execute(f'SELECT COUNT(*) FROM "{scope.table}"').fetchone()[0])
            if candidate_global_counts[scope.key] - remaining != expected:
                raise AccountDeletionConflict("Work Item candidate cascade changed during deletion")
            if expected:
                deleted[scope.key] = expected

        if runtime_keys:
            placeholders = ",".join("?" for _ in runtime_keys)
            runtime_cursor = conn.execute(
                f"DELETE FROM runtime_kv WHERE key IN ({placeholders})",  # nosec B608
                tuple(runtime_keys),
            )
            if runtime_cursor.rowcount != len(runtime_keys):
                raise AccountDeletionConflict("Runtime-состояние изменилось во время удаления")
            deleted["runtime_kv"] = int(runtime_cursor.rowcount)

        if int(report["counts"].get("document_catalog_worker_state") or 0):
            catalog_state_row = conn.execute(
                "SELECT value FROM runtime_kv WHERE key=?",
                (DOCUMENT_CATALOG_WORKER_STATE_KEY,),
            ).fetchone()
            if catalog_state_row is None:
                raise AccountDeletionConflict("DocumentCatalog runtime-состояние исчезло")
            catalog_namespace_key = load_document_catalog_worker_namespace_key(conn)
            current_catalog_fingerprint, current_catalog_supported = (
                document_catalog_worker_entry_fingerprint(
                    catalog_state_row["value"],
                    user_id,
                    namespace_key=catalog_namespace_key,
                )
            )
            if not current_catalog_supported or current_catalog_fingerprint != report.get(
                "document_catalog_worker_fingerprint"
            ):
                raise AccountDeletionConflict("DocumentCatalog runtime-состояние изменилось")
            try:
                catalog_state_payload, catalog_state_removed = remove_document_catalog_worker_entry(
                    catalog_state_row["value"],
                    user_id,
                    namespace_key=catalog_namespace_key,
                )
            except ValueError as exc:
                raise AccountDeletionConflict("DocumentCatalog runtime-состояние изменило формат") from exc
            if not catalog_state_removed:
                raise AccountDeletionConflict("DocumentCatalog runtime-состояние изменилось")
            catalog_state_cursor = conn.execute(
                "UPDATE runtime_kv SET value=?,updated_at=? WHERE key=? AND value=?",
                (
                    catalog_state_payload,
                    utc_now(),
                    DOCUMENT_CATALOG_WORKER_STATE_KEY,
                    catalog_state_row["value"],
                ),
            )
            if catalog_state_cursor.rowcount != 1:
                raise AccountDeletionConflict("DocumentCatalog runtime-состояние изменилось")
            deleted["document_catalog_worker_state"] = 1

        eligibility_cursor = conn.execute(
            "DELETE FROM runtime_kv WHERE key=?",
            (account_deletion_eligibility_key(user_id),),
        )
        if eligibility_cursor.rowcount != 1:
            raise AccountDeletionConflict("Метка проверенного контура удаления изменилась")
        deleted["deletion_eligibility"] = 1

        # A deleted supervisor must not remain as an invisible hierarchy edge on
        # somebody else's account.  Only the exact code-owned JSON key is removed.
        supervisor_link_ids = _supervisor_link_ids(conn, user_id)
        if supervisor_link_ids:
            placeholders = ",".join("?" for _ in supervisor_link_ids)
            supervisor_cursor = conn.execute(
                f"""UPDATE users
                       SET metadata_json=json_remove(metadata_json,'$.supervisor_id'), updated_at=?
                     WHERE id IN ({placeholders})""",  # nosec B608 - bound ids, generated placeholders
                (utc_now(), *supervisor_link_ids),
            )
            supervisor_links_removed = max(0, int(supervisor_cursor.rowcount))

        tombstone_key = deleted_account_tombstone_key(user_id)
        tombstone_cursor = conn.execute(
            "INSERT INTO runtime_kv(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
            (tombstone_key, json.dumps({"audit_id": audit_id}, sort_keys=True), utc_now()),
        )
        if tombstone_cursor.rowcount != 1:
            raise AccountDeletionConflict("Метка удалённой учётной записи уже существует")
        for identity_key in sorted(identity_tombstones):
            identity_cursor = conn.execute(
                "INSERT INTO runtime_kv(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO NOTHING",
                (
                    identity_key,
                    json.dumps({"audit_id": audit_id}, sort_keys=True),
                    utc_now(),
                ),
            )
            if identity_cursor.rowcount != 1:
                raise AccountDeletionConflict("Метка удалённого способа входа уже существует")
            identity_tombstones_created += 1

        # The audit insert is inside the same outer transaction and happens while
        # the target user still exists, so privacy sanitisation recognises the id.
        total_before_user = sum(deleted.values())
        storage.log_audit(
            AuditEntry(
                id=audit_id,
                user_id=actor_user_id,
                action="admin.user.delete",
                target_type="user",
                target_id=user_id,
                before_json={"status": "disabled"},
                after_json={
                    "status": "deleted",
                    "deleted": total_before_user + 1,
                    "items": len(deleted) + 1,
                },
                ip_address=ip_address,
                request_id=request_id,
            )
        )

        user_cursor = conn.execute("DELETE FROM users WHERE id=? AND status='disabled'", (user_id,))
        if user_cursor.rowcount != 1:
            raise AccountDeletionConflict("Учётная запись изменилась во время удаления")
        deleted["users"] = 1

    # Preflight permits only absent or empty per-account directories.  Removing an
    # empty shell after commit is idempotent and cannot destroy material.
    empty_directories_removed = 0
    files_directory = _account_files_directory(storage, user_id)
    with suppress(OSError):
        files_directory.rmdir()
        empty_directories_removed += 1
    with suppress(VaultProjectionBoundaryError):
        if MemoryVaultDeletionHandle(storage.settings.memory_vault_dir).remove_empty_account(user_id):
            empty_directories_removed += 1
    with suppress(OSError):
        _obsidian_account_directory(storage, user_id).rmdir()
        empty_directories_removed += 1
    retained = dict(report["retained"])
    retained["audit_log"] = int(retained.get("audit_log") or 0) + 1
    return {
        "status": "deleted",
        "user_id": user_id,
        "deleted": deleted,
        "deleted_rows": sum(deleted.values()),
        "supervisor_links_removed": supervisor_links_removed,
        "retained": retained,
        "audit_id": audit_id,
        "tombstoned": True,
        "identity_tombstones_created": identity_tombstones_created,
        "empty_directories_removed": empty_directories_removed,
    }


__all__ = [
    "AccountDeletionBlocked",
    "AccountDeletionConflict",
    "delete_account",
    "preflight_account_deletion",
]
