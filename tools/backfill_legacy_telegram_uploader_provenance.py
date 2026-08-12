#!/usr/bin/env python
"""Dry-run-first backfill of exact legacy Telegram uploader provenance.

Default mode is read-only. Only Raws whose bounded metadata already carries a
Telegram chat identity that maps 1:1 to one active user and one matching
channel_session receive ``uploaded_by=<mapped user id>``.

No owner-wide claim, no filename/path/content/fuzzy heuristics, no aliases.
Stdout and public reports contain only counts, classes, and short tags.
Private plan basis (full raw/uploader/hash identities) is never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from friday.file_delivery import (  # noqa: E402
    LEGACY_UNREGISTERED,
    REGISTERED_VALID,
    classify_file_registration,
    verify_registered_file_bytes,
)
from friday.permissions import LEGACY_OWNER_USER_ID  # noqa: E402
from friday.raw_metadata import RAW_FILE_METADATA_MAX_BYTES, bounded_raw_file_metadata  # noqa: E402
from friday.storage import SCHEMA_VERSION  # noqa: E402
from friday.storage._privacy import (  # noqa: E402
    _not_audio_document,
    _not_private_raw_dependency,
)

PLAN_SCHEMA = "friday.legacy-telegram-uploader-provenance-plan.v3"
CLAIM_SCHEMA = "friday.legacy-telegram-uploader-provenance-claim.v2"
REPORT_SCHEMA = "friday.legacy-telegram-uploader-provenance-report.v1"
CLAIM_SCOPE = "exact_legacy_telegram_uploader_mappings"
AUDIT_ACTION = "cli.legacy_telegram_uploader.backfill"
MAX_MANIFEST_BYTES = 64 * 1024
HEX64 = frozenset("0123456789abcdef")
TELEGRAM_CHANNELS = frozenset({"telegram", "telegram-bridge", "api-token"})
# Closed mapping evidence classes — never mixed.
EVIDENCE_IDENTITY_CURRENT = "identity_current"
EVIDENCE_LEGACY_EXTERNAL_CURRENT = "legacy_external_current"

_NOT_IGNORED_INBOX = """NOT EXISTS (
                     SELECT 1 FROM inbox i
                      WHERE i.raw_object_id={alias}.id AND i.user_id={alias}.user_id
                        AND i.status='ignored'
                   )"""


class ContractError(RuntimeError):
    """Operator or database contract was not satisfied."""


def _not_ignored_inbox(alias: str = "r") -> str:
    return _NOT_IGNORED_INBOX.format(alias=alias)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _tag(value: str) -> str:
    return _sha256(str(value).encode("utf-8"))[:16]


def _safe_identity(value: str, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 200 or any(ord(ch) < 32 for ch in text):
        raise ContractError(f"invalid {label}")
    return text


def _safe_hex64(value: str, *, label: str) -> str:
    text = str(value or "").strip().casefold()
    if len(text) != 64 or any(ch not in HEX64 for ch in text):
        raise ContractError(f"invalid {label}")
    return text


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON key")
        result[key] = value
    return result


def _connect(database: Path, *, read_only: bool) -> sqlite3.Connection:
    path = database.resolve(strict=True)
    if database.is_symlink() or not path.is_file():
        raise ContractError("database must be a regular non-symlink file")
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _required_tables(conn: sqlite3.Connection) -> None:
    required = {
        "raw_objects",
        "users",
        "channel_sessions",
        "conversations",
        "user_identities",
        "audit_log",
        "schema_meta",
        "inbox",
    }
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?,?,?,?,?)",
            tuple(sorted(required)),
        )
    }
    if present != required:
        raise ContractError("database does not have the required Friday tables")
    schema = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if schema is None or str(schema[0]) != str(SCHEMA_VERSION):
        raise ContractError("database must already be at the current Friday schema")


def _assert_canonical_owner_archive(conn: sqlite3.Connection, *, tenant_id: str, owner_id: str) -> None:
    """Shared owner archive only: tenant/owner must be the canonical archive owner.

    The archive owner is the exact ``LEGACY_OWNER_USER_ID`` string, not “whichever
    active ``preset_key=owner`` row happens to be unique in this database”:

    - ``tenant_id == owner_id == LEGACY_OWNER_USER_ID``;
    - that exact users row exists once, ``status='active'``, ``preset_key='owner'``.

    Other active owner-preset accounts are allowed and ignored by this gate.
    """

    if tenant_id != owner_id:
        raise ContractError("tenant id must equal owner id for the shared owner archive")
    if tenant_id != LEGACY_OWNER_USER_ID or owner_id != LEGACY_OWNER_USER_ID:
        raise ContractError("tenant/owner must be the canonical archive owner")
    rows = conn.execute(
        "SELECT id, preset_key, status FROM users WHERE id=? LIMIT 2",
        (LEGACY_OWNER_USER_ID,),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError("canonical archive owner row is missing or not unique")
    row = rows[0]
    found_id = str(row["id"] if hasattr(row, "keys") else row[0])
    preset = str(row["preset_key"] if hasattr(row, "keys") else row[1] or "")
    status = str(row["status"] if hasattr(row, "keys") else row[2] or "")
    if found_id != LEGACY_OWNER_USER_ID:
        raise ContractError("canonical archive owner row is missing or not unique")
    if preset != "owner":
        raise ContractError("canonical archive owner must have preset_key=owner")
    if status != "active":
        raise ContractError("canonical archive owner must be active")


def _resolve_files_root(files_root: Path | None) -> Path:
    if files_root is None:
        raise ContractError(
            "files root is required for modern-valid disk verification "
            "(pass --files-root; refuse to invent a local open/hash path)"
        )
    path = files_root.resolve(strict=True)
    if files_root.is_symlink() or not path.is_dir():
        raise ContractError("files root must be a regular non-symlink directory")
    return path


def _strict_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        if len(value.encode("utf-8")) > RAW_FILE_METADATA_MAX_BYTES:
            return None
    except UnicodeError:
        return None
    parsed = bounded_raw_file_metadata(value)
    if not parsed or not isinstance(parsed, dict):
        return None
    return parsed


def _normalize_chat_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or not text.isascii() or not text.isdecimal() or int(text) <= 0:
            return None
        return text
    return None


@dataclass(frozen=True)
class MappingHit:
    """Closed exact mapping with private session/conversation evidence."""

    mapped_user_id: str
    evidence_class: str
    chat_id: str
    conversation_id: str
    session_user_id: str
    session_channel_id: str
    session_updated_at: str


def _current_telegram_sessions(conn: sqlite3.Connection, chat_id: str) -> list[sqlite3.Row]:
    """Owned, unarchived Telegram sessions for chat_id (JOIN conversations)."""

    return list(
        conn.execute(
            """SELECT s.user_id AS user_id,
                      s.channel_id AS channel_id,
                      s.conversation_id AS conversation_id,
                      s.updated_at AS updated_at
                 FROM channel_sessions s
                 JOIN conversations c
                   ON c.id = s.conversation_id AND c.user_id = s.user_id
                WHERE s.channel='telegram' AND s.channel_id=?
                  AND COALESCE(c.is_archived, 0)=0
                LIMIT 3""",
            (chat_id,),
        ).fetchall()
    )


def _active_user_row(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    rows = conn.execute(
        "SELECT id, source, external_id, status FROM users WHERE id=? LIMIT 2",
        (user_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    if str(rows[0]["status"] or "") != "active":
        return None
    return rows[0]


def _resolve_exact_telegram_uploader(
    conn: sqlite3.Connection,
    metadata: Mapping[str, Any],
) -> tuple[MappingHit | None, str]:
    """Return (MappingHit, inventory_class).

    Inventory classes: exact | ambiguous | unmapped | not_telegram | has_uploader | cli_import

    Evidence classes (on exact hits only, never mixed):
    - identity_current: user_identities(source=telegram) + one owned unarchived session
    - legacy_external_current: closed fallback only when no identity row exists
    """

    if "uploaded_by" in metadata:
        return None, "has_uploader"
    if isinstance(metadata.get("import_source_path"), str) and str(metadata["import_source_path"]).strip():
        return None, "cli_import"

    channel = str(metadata.get("channel") or "").strip().casefold()
    if channel not in TELEGRAM_CHANNELS:
        return None, "not_telegram"

    chat_id = _normalize_chat_id(metadata.get("chat_id"))
    if chat_id is None:
        return None, "unmapped"

    identities = conn.execute(
        """SELECT user_id FROM user_identities
            WHERE source='telegram' AND external_id=?
            LIMIT 3""",
        (chat_id,),
    ).fetchall()
    sessions = _current_telegram_sessions(conn, chat_id)

    if len(identities) > 1:
        return None, "ambiguous"
    if len(sessions) > 1:
        return None, "ambiguous"

    if len(identities) == 1:
        # Primary: authenticated identity link. No legacy fallback when identity exists.
        mapped = str(identities[0]["user_id"] or "")
        if not mapped or _active_user_row(conn, mapped) is None:
            return None, "unmapped"
        if len(sessions) != 1:
            return None, "unmapped"
        session = sessions[0]
        if str(session["user_id"] or "") != mapped:
            return None, "ambiguous"
        return (
            MappingHit(
                mapped_user_id=mapped,
                evidence_class=EVIDENCE_IDENTITY_CURRENT,
                chat_id=chat_id,
                conversation_id=str(session["conversation_id"] or ""),
                session_user_id=str(session["user_id"] or ""),
                session_channel_id=str(session["channel_id"] or ""),
                session_updated_at=str(session["updated_at"] or ""),
            ),
            "exact",
        )

    # No identity row: closed legacy class only — exact users.source/external_id + session.
    if len(sessions) != 1:
        return None, "unmapped"
    session = sessions[0]
    session_user = str(session["user_id"] or "")
    if not session_user:
        return None, "unmapped"
    user_row = _active_user_row(conn, session_user)
    if user_row is None:
        return None, "unmapped"
    if str(user_row["source"] or "").casefold() != "telegram":
        return None, "unmapped"
    if str(user_row["external_id"] or "").strip() != chat_id:
        return None, "unmapped"
    # Refuse if another active user also claims the same external_id.
    peers = conn.execute(
        """SELECT id FROM users
            WHERE status='active' AND source='telegram' AND external_id=?
            LIMIT 3""",
        (chat_id,),
    ).fetchall()
    if len(peers) != 1 or str(peers[0]["id"] or "") != session_user:
        return None, "ambiguous"
    return (
        MappingHit(
            mapped_user_id=session_user,
            evidence_class=EVIDENCE_LEGACY_EXTERNAL_CURRENT,
            chat_id=chat_id,
            conversation_id=str(session["conversation_id"] or ""),
            session_user_id=str(session["user_id"] or ""),
            session_channel_id=str(session["channel_id"] or ""),
            session_updated_at=str(session["updated_at"] or ""),
        ),
        "exact",
    )


def _registration_gate(
    metadata: Mapping[str, Any] | None,
    *,
    content_hash: str,
    files_root: Path,
) -> tuple[str | None, str]:
    """Return (None, class) if modern-valid + disk OK, else (refusal_count_key, class)."""

    verdict = classify_file_registration(metadata, content_hash=content_hash)
    if verdict.state == LEGACY_UNREGISTERED:
        return "refused_registration_legacy", "legacy"
    if verdict.state != REGISTERED_VALID:
        return "refused_registration_invalid", "invalid"

    disk = verify_registered_file_bytes(
        files_root,
        metadata,
        content_hash=content_hash,
    )
    if disk.state == REGISTERED_VALID:
        return None, "modern_valid_disk"
    reason = disk.reason
    if reason == "disk_bytes_unreadable_or_mismatched":
        return "refused_registration_disk", "disk_unreadable_or_mismatched"
    if reason == "size_bytes_mismatch":
        return "refused_registration_disk", "size_mismatch"
    return "refused_registration_disk", "disk_invalid"


@dataclass(frozen=True)
class ProvenanceCandidate:
    raw_id: str
    tenant_id: str
    mapped_uploader_id: str
    metadata_json: str
    metadata_sha256: str
    content_hash: str
    received_at: str
    source: str
    registration_class: str
    mapping_evidence_class: str
    chat_id: str
    conversation_id: str
    session_user_id: str
    session_channel_id: str
    session_updated_at: str
    audio_carrier: bool

    def public_projection(self) -> dict[str, str]:
        return {
            "mapped_uploader_tag": _tag(self.mapped_uploader_id),
            "mapping_evidence_class": self.mapping_evidence_class,
            "raw_tag": _tag(self.raw_id),
            "registration_class": self.registration_class,
        }

    def private_basis_entry(self) -> dict[str, Any]:
        return {
            "audio_carrier": self.audio_carrier,
            "chat_id": self.chat_id,
            "content_hash": self.content_hash,
            "conversation_id": self.conversation_id,
            "conversation_id_sha256": _sha256(self.conversation_id.encode("utf-8")),
            "mapped_uploader_id": self.mapped_uploader_id,
            "mapping_evidence_class": self.mapping_evidence_class,
            "metadata_sha256": self.metadata_sha256,
            "raw_id": self.raw_id,
            "received_at": self.received_at,
            "registration_class": self.registration_class,
            "session_channel_id": self.session_channel_id,
            "session_updated_at": self.session_updated_at,
            "session_user_id": self.session_user_id,
            "session_identity_sha256": _sha256(
                f"{self.session_user_id}|{self.session_channel_id}|{self.conversation_id}|{self.session_updated_at}".encode()
            ),
            "source": self.source,
        }


@dataclass(frozen=True)
class Plan:
    tenant_id: str
    owner_id: str
    candidates: tuple[ProvenanceCandidate, ...]
    plan_sha256: str
    counts: dict[str, int]
    disk_verified: bool

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    def public_summary(self, *, mode: str) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "mode": mode,
            "claim_scope": CLAIM_SCOPE,
            "tenant_tag": _tag(self.tenant_id),
            "owner_tag": _tag(self.owner_id),
            "candidate_count": self.candidate_count,
            "plan_sha256": self.plan_sha256,
            "disk_verified": self.disk_verified,
            "counts": dict(self.counts),
            "sample_candidate_tags": [item.public_projection() for item in self.candidates[:8]],
        }


def _candidate_scan_rows(conn: sqlite3.Connection, *, tenant_id: str) -> list[sqlite3.Row]:
    """Live upload files: public + non-ignored.

    Audio/voice carriers are inventoried (``nonaudio`` flag) so the offline tool
    can attribute them; they stay out of runtime document readers.
    """

    return list(
        conn.execute(
            f"""SELECT r.id, r.user_id, r.source, r.source_ref, r.content_hash,
                      r.received_at, r.metadata_json, r.deleted_at,
                      CASE WHEN {_not_audio_document("r")} THEN 1 ELSE 0 END AS nonaudio
                  FROM raw_objects r
                 WHERE r.user_id=? AND r.source='upload' AND r.content_type='file'
                   AND r.deleted_at IS NULL
                   AND {_not_private_raw_dependency("r")}
                   AND {_not_ignored_inbox("r")}
                 ORDER BY r.id""",  # nosec B608
            (tenant_id,),
        ).fetchall()
    )


def build_plan(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
    files_root: Path,
) -> Plan:
    tenant = _safe_identity(tenant_id, label="tenant id")
    owner = _safe_identity(owner_id, label="owner id")
    root = _resolve_files_root(files_root)
    _required_tables(conn)
    _assert_canonical_owner_archive(conn, tenant_id=tenant, owner_id=owner)

    counts: dict[str, int] = {
        "rows_scanned": 0,
        "planned_inserts": 0,
        "telegram_no_uploader": 0,
        "exact": 0,
        "exact_identity_current": 0,
        "exact_legacy_external_current": 0,
        "ambiguous": 0,
        "unmapped": 0,
        "not_telegram": 0,
        "cli_import": 0,
        "existing_uploader": 0,
        "explicit_null_uploader": 0,
        "unreadable_metadata": 0,
        "refused_audio": 0,
        "planned_audio": 0,
        "refused_registration_legacy": 0,
        "refused_registration_invalid": 0,
        "refused_registration_disk": 0,
        "refused_mapped_inactive": 0,
    }
    candidates: list[ProvenanceCandidate] = []

    rows = _candidate_scan_rows(conn, tenant_id=tenant)
    for row in rows:
        counts["rows_scanned"] += 1
        metadata_json = row["metadata_json"]
        metadata = _strict_metadata(metadata_json)
        if metadata is None:
            counts["unreadable_metadata"] += 1
            continue

        if "uploaded_by" in metadata:
            if metadata.get("uploaded_by") is None:
                counts["explicit_null_uploader"] += 1
            else:
                counts["existing_uploader"] += 1
            continue

        hit, mapping_class = _resolve_exact_telegram_uploader(conn, metadata)
        if mapping_class == "cli_import":
            counts["cli_import"] += 1
            continue
        if mapping_class == "not_telegram":
            counts["not_telegram"] += 1
            continue
        if mapping_class == "unmapped":
            counts["telegram_no_uploader"] += 1
            counts["unmapped"] += 1
            continue
        if mapping_class == "ambiguous":
            counts["telegram_no_uploader"] += 1
            counts["ambiguous"] += 1
            continue
        if mapping_class != "exact" or hit is None:
            counts["telegram_no_uploader"] += 1
            counts["unmapped"] += 1
            continue
        if not hit.conversation_id or not hit.session_user_id:
            counts["telegram_no_uploader"] += 1
            counts["unmapped"] += 1
            continue

        # Exact Telegram mapping inventory (before registration gates).
        counts["telegram_no_uploader"] += 1
        counts["exact"] += 1
        if hit.evidence_class == EVIDENCE_IDENTITY_CURRENT:
            counts["exact_identity_current"] += 1
        elif hit.evidence_class == EVIDENCE_LEGACY_EXTERNAL_CURRENT:
            counts["exact_legacy_external_current"] += 1
        else:
            counts["unmapped"] += 1
            continue

        audio_carrier = int(row["nonaudio"] or 0) != 1
        content_hash = str(row["content_hash"] or "")
        refusal_key, reg_class = _registration_gate(
            metadata,
            content_hash=content_hash,
            files_root=root,
        )
        if refusal_key is not None:
            counts[refusal_key] += 1
            continue

        raw_id = str(row["id"] or "")
        if not raw_id:
            raise ContractError("candidate has no Raw id")
        if audio_carrier:
            counts["planned_audio"] += 1
        candidates.append(
            ProvenanceCandidate(
                raw_id=raw_id,
                tenant_id=tenant,
                mapped_uploader_id=hit.mapped_user_id,
                metadata_json=str(metadata_json),
                metadata_sha256=_sha256(str(metadata_json).encode("utf-8")),
                content_hash=content_hash,
                received_at=str(row["received_at"] or ""),
                source=str(row["source"] or ""),
                registration_class=reg_class,
                mapping_evidence_class=hit.evidence_class,
                chat_id=hit.chat_id,
                conversation_id=hit.conversation_id,
                session_user_id=hit.session_user_id,
                session_channel_id=hit.session_channel_id,
                session_updated_at=hit.session_updated_at,
                audio_carrier=audio_carrier,
            )
        )

    candidates.sort(key=lambda item: (item.raw_id, item.mapped_uploader_id, item.mapping_evidence_class))
    counts["planned_inserts"] = len(candidates)
    private_basis = {
        "schema": PLAN_SCHEMA,
        "claim_scope": CLAIM_SCOPE,
        "tenant_id": tenant,
        "owner_id": owner,
        "planned_audio": counts["planned_audio"],
        "gates": {
            "canonical_owner": True,
            "disk_verified": True,
            "exact_telegram_mapping": True,
            "mapping_evidence_closed": True,
            "offline_audio_attribution": True,
            "owned_unarchived_session": True,
            "peer_owner_presets_ignored": True,
            "shared_owner_archive": True,
            "tenant_equals_owner": True,
            "modern_valid_registration": True,
            "not_ignored_inbox": True,
            "not_private_raw": True,
            "source": "upload",
            "content_type": "file",
            "uploaded_by_absent": True,
        },
        "candidates": [item.private_basis_entry() for item in candidates],
    }
    return Plan(
        tenant_id=tenant,
        owner_id=owner,
        candidates=tuple(candidates),
        plan_sha256=_sha256(_canonical_json(private_basis)),
        counts=counts,
        disk_verified=True,
    )


def _ensure_private_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
            raise ContractError("private output directory has unsafe permissions")
        return
    path.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(path, 0o700)


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    _ensure_private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(_canonical_json(dict(value)) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _load_claim(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ContractError("claim manifest must be a regular non-symlink file")
    if path.stat().st_mode & 0o077:
        raise ContractError("claim manifest must not be group/world accessible")
    data = path.read_bytes()
    if not data or len(data) > MAX_MANIFEST_BYTES:
        raise ContractError("claim manifest size is invalid")
    try:
        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError("claim manifest is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ContractError("claim manifest root must be an object")
    expected_keys = {
        "approved",
        "candidate_count",
        "claim_scope",
        "owner_id",
        "plan_sha256",
        "schema",
        "tenant_id",
    }
    if set(parsed) != expected_keys:
        raise ContractError("claim manifest has an unexpected shape")
    return parsed


def _validate_claim(
    claim: Mapping[str, Any],
    plan: Plan,
    *,
    expected_count: int,
    expected_sha256: str,
) -> None:
    if claim.get("approved") is not True:
        raise ContractError("claim manifest is not explicitly approved")
    if claim.get("schema") != CLAIM_SCHEMA or claim.get("claim_scope") != CLAIM_SCOPE:
        raise ContractError("claim manifest schema or scope is invalid")
    if (
        claim.get("tenant_id") != LEGACY_OWNER_USER_ID
        or claim.get("owner_id") != LEGACY_OWNER_USER_ID
        or plan.tenant_id != LEGACY_OWNER_USER_ID
        or plan.owner_id != LEGACY_OWNER_USER_ID
    ):
        raise ContractError("claim manifest is not the canonical archive owner")
    if claim.get("tenant_id") != plan.tenant_id or claim.get("owner_id") != plan.owner_id:
        raise ContractError("claim manifest identity does not match")
    if type(claim.get("candidate_count")) is not int:
        raise ContractError("claim manifest candidate_count is invalid")
    manifest_sha = _safe_hex64(str(claim.get("plan_sha256") or ""), label="manifest plan SHA-256")
    if (
        int(claim["candidate_count"]) != expected_count
        or expected_count != plan.candidate_count
        or manifest_sha != expected_sha256
        or expected_sha256 != plan.plan_sha256
    ):
        raise ContractError("claim manifest/count/checksum does not match the current plan")
    if expected_count <= 0:
        raise ContractError("refusing an empty apply plan")


def _updated_metadata(candidate: ProvenanceCandidate) -> str:
    metadata = _strict_metadata(candidate.metadata_json)
    if metadata is None or "uploaded_by" in metadata:
        raise ContractError("candidate stopped satisfying the absent-uploader contract")
    if isinstance(metadata.get("import_source_path"), str) and str(metadata["import_source_path"]).strip():
        raise ContractError("candidate became a CLI import; refusing")
    updated = dict(metadata)
    updated["uploaded_by"] = candidate.mapped_uploader_id
    encoded = json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > RAW_FILE_METADATA_MAX_BYTES:
        raise ContractError("updated metadata exceeds the bounded Raw envelope")
    # Round-trip through the same bounded parser used at read time.
    if _strict_metadata(encoded) is None:
        raise ContractError("updated metadata failed bounded validation")
    return encoded


def _postcheck_transaction(
    conn: sqlite3.Connection,
    plan: Plan,
    *,
    audit_request_id: str,
    files_root: Path,
) -> None:
    for candidate in plan.candidates:
        row = conn.execute(
            f"""SELECT r.source, r.content_hash, r.received_at, r.metadata_json
                  FROM raw_objects r
                 WHERE r.id=? AND r.user_id=? AND r.deleted_at IS NULL
                   AND r.source='upload' AND r.content_type='file'
                   AND {_not_private_raw_dependency("r")}
                   AND {_not_ignored_inbox("r")}""",  # nosec B608
            (candidate.raw_id, plan.tenant_id),
        ).fetchone()
        if row is None:
            raise ContractError("updated Raw disappeared or lost authorization before commit")
        metadata = _strict_metadata(row["metadata_json"])
        if metadata is None or metadata.get("uploaded_by") != candidate.mapped_uploader_id:
            raise ContractError("uploader postcondition failed")
        without_uploader = dict(metadata)
        without_uploader.pop("uploaded_by", None)
        original = _strict_metadata(candidate.metadata_json)
        if without_uploader != original:
            raise ContractError("backfill changed metadata other than uploaded_by")
        if (
            str(row["source"] or "") != candidate.source
            or str(row["content_hash"] or "") != candidate.content_hash
            or str(row["received_at"] or "") != candidate.received_at
        ):
            raise ContractError("backfill changed immutable Raw provenance")
        refusal, _reg = _registration_gate(
            metadata,
            content_hash=candidate.content_hash,
            files_root=files_root,
        )
        if refusal is not None:
            raise ContractError("registration postcondition failed after write")

    audit_count = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE request_id=? AND action=?",
        (audit_request_id, AUDIT_ACTION),
    ).fetchone()[0]
    if int(audit_count) != plan.candidate_count:
        raise ContractError("audit row count does not match the applied row count")

    remaining = build_plan(
        conn,
        tenant_id=plan.tenant_id,
        owner_id=plan.owner_id,
        files_root=files_root,
    )
    if remaining.candidate_count != 0:
        raise ContractError("the in-transaction exact scope was not fully attributed")

    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_keys:
        raise ContractError("post-apply integrity/foreign-key check failed")


def apply_plan(
    database: Path,
    *,
    tenant_id: str,
    owner_id: str,
    files_root: Path,
    claim_manifest: Path,
    expected_count: int,
    expected_plan_sha256: str,
    backup_dir: Path,
    writers_stopped_acknowledged: bool,
) -> tuple[Plan, dict[str, Any]]:
    if not writers_stopped_acknowledged:
        raise ContractError("apply requires explicit stopped-writers acknowledgement")
    expected_sha = _safe_hex64(expected_plan_sha256, label="expected plan SHA-256")
    if type(expected_count) is not int or expected_count <= 0:
        raise ContractError("expected count must be a positive integer")
    claim = _load_claim(claim_manifest)
    root = _resolve_files_root(files_root)

    preflight = _connect(database, read_only=True)
    try:
        initial_plan = build_plan(
            preflight,
            tenant_id=tenant_id,
            owner_id=owner_id,
            files_root=root,
        )
    finally:
        preflight.close()
    _validate_claim(
        claim,
        initial_plan,
        expected_count=expected_count,
        expected_sha256=expected_sha,
    )
    _ensure_private_directory(backup_dir)

    from friday.config import load_settings
    from friday.storage import FridayStorage

    settings = replace(
        load_settings(),
        database_path=database.resolve(strict=True),
        database_must_exist=True,
        backups_dir=backup_dir.resolve(strict=True),
    )
    storage = FridayStorage(settings)
    backup_result: dict[str, Any] = {}
    committed = False
    request_id = ""
    plan = initial_plan
    try:
        made = storage.create_backup(label=f"pre-legacy-tg-uploader-{initial_plan.plan_sha256[:12]}")
        verified = storage.verify_backup(str(made.get("database") or ""))
        if verified.get("ok") is not True:
            raise ContractError("pre-apply backup verification failed")
        backup_result = {
            "database": str(made.get("database") or ""),
            "manifest": Path(str(made.get("manifest_path") or "")).name,
            "verified": True,
            "label_tag": _tag(str(made.get("label") or "")),
        }
        with storage.transaction() as conn:
            plan = build_plan(
                conn,
                tenant_id=tenant_id,
                owner_id=owner_id,
                files_root=root,
            )
            _validate_claim(
                claim,
                plan,
                expected_count=expected_count,
                expected_sha256=expected_sha,
            )
            if plan.plan_sha256 != initial_plan.plan_sha256:
                raise ContractError("in-transaction plan drifted; refusing apply")
            request_id = f"legacy-tg-uploader-{plan.plan_sha256[:24]}"
            now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            for candidate in plan.candidates:
                # Re-resolve mapping under CAS; refuse drift of mapped identity.
                meta_now = _strict_metadata(candidate.metadata_json)
                if meta_now is None:
                    raise ContractError("candidate metadata unreadable under CAS")
                hit_now, mapping_class = _resolve_exact_telegram_uploader(conn, meta_now)
                if (
                    mapping_class != "exact"
                    or hit_now is None
                    or hit_now.mapped_user_id != candidate.mapped_uploader_id
                    or hit_now.evidence_class != candidate.mapping_evidence_class
                    or hit_now.conversation_id != candidate.conversation_id
                    or hit_now.session_user_id != candidate.session_user_id
                    or hit_now.session_channel_id != candidate.session_channel_id
                    or hit_now.chat_id != candidate.chat_id
                ):
                    raise ContractError("candidate mapping drifted under CAS")
                if (
                    plan.tenant_id != plan.owner_id
                    or plan.tenant_id != LEGACY_OWNER_USER_ID
                    or plan.owner_id != LEGACY_OWNER_USER_ID
                ):
                    raise ContractError("tenant/owner archive contract drifted under CAS")
                refusal, _reg = _registration_gate(
                    meta_now,
                    content_hash=candidate.content_hash,
                    files_root=root,
                )
                if refusal is not None:
                    raise ContractError("candidate registration failed under CAS")

                new_metadata = _updated_metadata(candidate)
                cursor = conn.execute(
                    f"""UPDATE raw_objects SET metadata_json=?
                        WHERE id=? AND user_id=? AND metadata_json=?
                          AND deleted_at IS NULL AND content_type='file'
                          AND source='upload'
                          AND content_hash=?
                          AND {_not_private_raw_dependency("raw_objects")}
                          AND {_not_ignored_inbox("raw_objects")}""",  # nosec B608
                    (
                        new_metadata,
                        candidate.raw_id,
                        plan.tenant_id,
                        candidate.metadata_json,
                        candidate.content_hash,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ContractError("candidate compare-and-swap failed")
                after_json = json.dumps(
                    {
                        "mapped_uploader_tag": _tag(candidate.mapped_uploader_id),
                        "mapping_evidence_class": candidate.mapping_evidence_class,
                        "operation": "legacy_telegram_uploader_provenance_backfill",
                        "plan_sha256": plan.plan_sha256,
                        "raw_tag": _tag(candidate.raw_id),
                        "registration_class": candidate.registration_class,
                        "status": "accepted",
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                conn.execute(
                    """INSERT INTO audit_log(
                           id,user_id,action,target_type,target_id,before_json,after_json,
                           ip_address,request_id,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        f"audit_{uuid.uuid4().hex}",
                        plan.owner_id,
                        AUDIT_ACTION,
                        "raw_object",
                        candidate.raw_id,
                        '{"provenance":"unknown","status":"pending"}',
                        after_json,
                        "",
                        request_id,
                        now,
                    ),
                )
            _postcheck_transaction(
                conn,
                plan,
                audit_request_id=request_id,
                files_root=root,
            )
        committed = True
    finally:
        storage.close(final=True)
    if not committed:
        raise ContractError("apply did not commit")

    check = _connect(database, read_only=True)
    try:
        integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = check.execute("PRAGMA foreign_key_check").fetchall()
        privacy_state = check.execute(
            "SELECT valid FROM private_entity_material_derivative_state WHERE singleton=1"
        ).fetchone()
    finally:
        check.close()
    if integrity != "ok" or foreign_keys or privacy_state is None or int(privacy_state[0]) != 1:
        raise ContractError("post-commit diagnostics failed")
    return plan, {
        "backup": backup_result,
        "audit_request_tag": _tag(request_id),
        "applied": plan.candidate_count,
    }


def audit_plan(database: Path, *, tenant_id: str, owner_id: str, files_root: Path) -> Plan:
    conn = _connect(database, read_only=True)
    try:
        return build_plan(
            conn,
            tenant_id=tenant_id,
            owner_id=owner_id,
            files_root=files_root,
        )
    finally:
        conn.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--files-root", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument(
        "--owner-id",
        required=True,
        help="Canonical archive owner (LEGACY_OWNER_USER_ID); peer owner presets are ignored",
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--claim-manifest", type=Path)
    parser.add_argument("--expect-count", type=int)
    parser.add_argument("--expect-plan-sha256")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument(
        "--i-confirm-writers-stopped",
        action="store_true",
        help="Required for apply: operator acknowledges Friday writers are stopped",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.apply:
            if (
                args.claim_manifest is None
                or args.expect_count is None
                or args.expect_plan_sha256 is None
                or args.backup_dir is None
            ):
                raise ContractError(
                    "apply requires --claim-manifest, --expect-count, --expect-plan-sha256 and --backup-dir"
                )
            plan, extra = apply_plan(
                args.database,
                tenant_id=args.tenant_id,
                owner_id=args.owner_id,
                files_root=args.files_root,
                claim_manifest=args.claim_manifest,
                expected_count=int(args.expect_count),
                expected_plan_sha256=str(args.expect_plan_sha256),
                backup_dir=args.backup_dir,
                writers_stopped_acknowledged=bool(args.i_confirm_writers_stopped),
            )
            report = {**plan.public_summary(mode="applied"), "apply": extra, "applied": True}
        else:
            if (
                any(
                    value is not None
                    for value in (
                        args.claim_manifest,
                        args.expect_count,
                        args.expect_plan_sha256,
                        args.backup_dir,
                    )
                )
                or args.i_confirm_writers_stopped
            ):
                raise ContractError("apply-only arguments require --apply")
            plan = audit_plan(
                args.database,
                tenant_id=args.tenant_id,
                owner_id=args.owner_id,
                files_root=args.files_root,
            )
            report = {**plan.public_summary(mode="dry_run"), "applied": False}
        report["report_sha256"] = _sha256(_canonical_json(report))
        if args.report is not None:
            _write_private_json(args.report, report)
        sys.stdout.write(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except (ContractError, OSError, sqlite3.Error) as exc:
        payload = {
            "schema": REPORT_SCHEMA,
            "mode": "error",
            "error": type(exc).__name__,
            "applied": False,
            "message": str(exc) if isinstance(exc, ContractError) else type(exc).__name__,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
