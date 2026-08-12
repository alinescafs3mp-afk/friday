#!/usr/bin/env python
"""Dry-run-first backfill of historical Telegram file aliases.

Default mode is read-only. Apply needs an approved claim manifest, exact plan
count, exact plan SHA-256, a verified backup directory, and stopped writers.
Evidence is only immutable durable transport identity already on the Raw
(source_ref) or existing alias rows — never filename/latest/fuzzy/model.

Stdout and public reports contain only counts, classes, and short tags.
Private plan basis (full raw/uploader/ref/hash) is never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
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
from friday.storage import SCHEMA_VERSION  # noqa: E402
from friday.storage._intake import _telegram_file_source_ref_kind  # noqa: E402
from friday.storage._privacy import (  # noqa: E402
    _exact_uploader_raw_dependency,
    _not_audio_document,
    _not_private_raw_dependency,
)

PLAN_SCHEMA = "friday.telegram-file-aliases-plan.v2"
CLAIM_SCHEMA = "friday.telegram-file-aliases-claim.v2"
REPORT_SCHEMA = "friday.telegram-file-aliases-report.v2"
CLAIM_SCOPE = "recoverable_telegram_file_aliases"
AUDIT_ACTION = "cli.telegram_file_aliases.backfill"
MAX_MANIFEST_BYTES = 64 * 1024
HEX64 = frozenset("0123456789abcdef")

# Same gate production binder/resolver uses on Raw (NOT EXISTS inbox ignored).
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
    required = {"raw_objects", "users", "schema_meta", "file_source_aliases", "audit_log", "inbox"}
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?,?,?)",
            tuple(sorted(required)),
        )
    }
    if present != required:
        raise ContractError("database does not have the required Friday tables")
    schema = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if schema is None or str(schema[0]) != str(SCHEMA_VERSION):
        raise ContractError("database must already be at the current Friday schema")


def _owner_is_active(conn: sqlite3.Connection, owner_id: str) -> bool:
    rows = conn.execute(
        "SELECT preset_key, status FROM users WHERE id=? LIMIT 2",
        (owner_id,),
    ).fetchall()
    return bool(
        len(rows) == 1
        and str(rows[0]["preset_key"] or "") == "owner"
        and str(rows[0]["status"] or "") == "active"
    )


def _uploader_is_active(conn: sqlite3.Connection, uploader_id: str) -> bool:
    """Exact target uploader must be one active authenticated user row."""

    rows = conn.execute(
        "SELECT status FROM users WHERE id=? LIMIT 2",
        (uploader_id,),
    ).fetchall()
    return bool(len(rows) == 1 and str(rows[0]["status"] or "") == "active")


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


def _extract_telegram_file_ref(source_ref: str) -> str | None:
    """Return a closed telegram-file identity embedded in a Raw source_ref."""

    text = str(source_ref or "").strip()
    if not text:
        return None
    match = re.search(r"(telegram-file:[^\s]+)\Z", text)
    if match is None:
        match = re.search(r"(telegram-file:[A-Za-z0-9!#$&^_.+\-/=]+)", text)
    if match is None:
        return None
    candidate = match.group(1)
    if _telegram_file_source_ref_kind(candidate) != "file":
        return None
    return candidate


def _metadata_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or len(value) > 1_048_576:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


@dataclass(frozen=True)
class AliasCandidate:
    raw_id: str
    tenant_id: str
    uploader_id: str
    source_ref: str
    kind: str
    evidence_class: str
    content_hash: str
    registration_class: str

    def public_projection(self) -> dict[str, str]:
        """Bounded short tags/classes only — never full ids/hashes/paths."""

        return {
            "evidence_class": self.evidence_class,
            "kind": self.kind,
            "raw_tag": _tag(self.raw_id),
            "registration_class": self.registration_class,
            "source_ref_tag": _tag(self.source_ref),
            "uploader_tag": _tag(self.uploader_id),
        }

    def private_basis_entry(self) -> dict[str, str]:
        """Full identities for plan_sha256 only — never printed."""

        return {
            "content_hash": self.content_hash,
            "evidence_class": self.evidence_class,
            "kind": self.kind,
            "raw_id": self.raw_id,
            "registration_class": self.registration_class,
            "source_ref": self.source_ref,
            "uploader_id": self.uploader_id,
        }


@dataclass(frozen=True)
class Plan:
    tenant_id: str
    owner_id: str
    uploader_id: str
    candidates: tuple[AliasCandidate, ...]
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
            "uploader_tag": _tag(self.uploader_id),
            "candidate_count": self.candidate_count,
            "plan_sha256": self.plan_sha256,
            "disk_verified": self.disk_verified,
            "counts": dict(self.counts),
            "sample_candidate_tags": [item.public_projection() for item in self.candidates[:8]],
        }


def _authorized_file_rows(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    uploader_id: str,
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            f"""SELECT r.id, r.user_id, r.source_ref, r.content_hash, r.metadata_json, r.deleted_at
                  FROM raw_objects r
                 WHERE r.user_id=? AND r.source='upload' AND r.content_type='file'
                   AND r.deleted_at IS NULL
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND {_exact_uploader_raw_dependency("r")}
                   AND {_not_ignored_inbox("r")}
                 ORDER BY r.id""",  # nosec B608
            (tenant_id, uploader_id),
        ).fetchall()
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
        # Closed class only — no path/hash leakage.
        return "refused_registration_disk", "disk_unreadable_or_mismatched"
    if reason == "size_bytes_mismatch":
        return "refused_registration_disk", "size_mismatch"
    return "refused_registration_disk", "disk_invalid"


def build_plan(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
    uploader_id: str,
    files_root: Path,
) -> Plan:
    tenant = _safe_identity(tenant_id, label="tenant id")
    owner = _safe_identity(owner_id, label="owner id")
    uploader = _safe_identity(uploader_id, label="uploader id")
    root = _resolve_files_root(files_root)
    _required_tables(conn)
    if not _owner_is_active(conn, owner):
        raise ContractError("owner id is not one unique active owner account")
    if not _uploader_is_active(conn, uploader):
        raise ContractError("uploader id is not one unique active authenticated user")

    counts: dict[str, int] = {
        "rows_scanned": 0,
        "recoverable_file_refs": 0,
        "already_bound": 0,
        "planned_inserts": 0,
        "conflicts": 0,
        "unmapped": 0,
        "refused_no_uploader": 0,
        "refused_uploader_mismatch": 0,
        "refused_gates": 0,
        "refused_registration_legacy": 0,
        "refused_registration_invalid": 0,
        "refused_registration_disk": 0,
        "existing_message_aliases": 0,
        "existing_unique_aliases": 0,
        "existing_file_aliases": 0,
    }
    candidates: list[AliasCandidate] = []

    rows = _authorized_file_rows(conn, tenant_id=tenant, uploader_id=uploader)
    for row in rows:
        counts["rows_scanned"] += 1
        raw_id = str(row["id"] or "")
        source_ref = str(row["source_ref"] or "")
        content_hash = str(row["content_hash"] or "")
        metadata = _metadata_object(row["metadata_json"])
        if metadata is None or "uploaded_by" not in metadata:
            counts["refused_no_uploader"] += 1
            continue
        if metadata.get("uploaded_by") != uploader:
            counts["refused_uploader_mismatch"] += 1
            continue

        refusal_key, reg_class = _registration_gate(
            metadata,
            content_hash=content_hash,
            files_root=root,
        )
        if refusal_key is not None:
            counts[refusal_key] += 1
            continue

        aliases = conn.execute(
            """SELECT source_ref, raw_object_id FROM file_source_aliases
                WHERE user_id=? AND uploaded_by=? AND raw_object_id=?""",
            (tenant, uploader, raw_id),
        ).fetchall()
        existing_refs = {str(a["source_ref"]) for a in aliases}
        for ref in existing_refs:
            kind = _telegram_file_source_ref_kind(ref)
            if kind == "message":
                counts["existing_message_aliases"] += 1
            elif kind == "unique":
                counts["existing_unique_aliases"] += 1
            elif kind == "file":
                counts["existing_file_aliases"] += 1

        file_ref = _extract_telegram_file_ref(source_ref)
        if file_ref is None:
            counts["unmapped"] += 1
            continue

        counts["recoverable_file_refs"] += 1
        bound = conn.execute(
            """SELECT raw_object_id FROM file_source_aliases
                WHERE user_id=? AND uploaded_by=? AND source_ref=?""",
            (tenant, uploader, file_ref),
        ).fetchone()
        if bound is not None and str(bound["raw_object_id"]) != raw_id:
            counts["conflicts"] += 1
            continue
        if file_ref in existing_refs:
            counts["already_bound"] += 1
            continue

        candidates.append(
            AliasCandidate(
                raw_id=raw_id,
                tenant_id=tenant,
                uploader_id=uploader,
                source_ref=file_ref,
                kind="file",
                evidence_class="raw_source_ref",
                content_hash=content_hash,
                registration_class=reg_class,
            )
        )

    candidates.sort(key=lambda item: (item.raw_id, item.kind, item.source_ref))
    counts["planned_inserts"] = len(candidates)
    # Private canonical basis — full identities + gates. Never print this object.
    private_basis = {
        "schema": PLAN_SCHEMA,
        "claim_scope": CLAIM_SCOPE,
        "tenant_id": tenant,
        "owner_id": owner,
        "uploader_id": uploader,
        "gates": {
            "disk_verified": True,
            "exact_uploader": True,
            "modern_valid_registration": True,
            "not_audio_document": True,
            "not_ignored_inbox": True,
            "not_private_raw": True,
            "source": "upload",
            "content_type": "file",
        },
        "candidates": [item.private_basis_entry() for item in candidates],
    }
    return Plan(
        tenant_id=tenant,
        owner_id=owner,
        uploader_id=uploader,
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
        "uploader_id",
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
        claim.get("tenant_id") != plan.tenant_id
        or claim.get("owner_id") != plan.owner_id
        or claim.get("uploader_id") != plan.uploader_id
    ):
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


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def apply_plan(
    database: Path,
    *,
    tenant_id: str,
    owner_id: str,
    uploader_id: str,
    files_root: Path,
    claim_manifest: Path,
    expected_count: int,
    expected_plan_sha256: str,
    backup_dir: Path,
) -> tuple[Plan, dict[str, Any]]:
    expected_sha = _safe_hex64(expected_plan_sha256, label="expected plan SHA-256")
    if type(expected_count) is not int or expected_count <= 0:
        raise ContractError("expected count must be a positive integer")
    claim = _load_claim(claim_manifest)
    preflight = _connect(database, read_only=True)
    try:
        initial = build_plan(
            preflight,
            tenant_id=tenant_id,
            owner_id=owner_id,
            uploader_id=uploader_id,
            files_root=files_root,
        )
    finally:
        preflight.close()
    _validate_claim(claim, initial, expected_count=expected_count, expected_sha256=expected_sha)
    _ensure_private_directory(backup_dir)

    from dataclasses import replace

    from friday.config import load_settings
    from friday.storage import FridayStorage

    settings = replace(
        load_settings(),
        database_path=database.resolve(strict=True),
        database_must_exist=True,
        backups_dir=backup_dir.resolve(strict=True),
    )
    storage = FridayStorage(settings)
    try:
        backup_result = storage.create_backup(label=f"pre-telegram-alias-backfill-{initial.plan_sha256[:12]}")
    except Exception as exc:  # noqa: BLE001
        raise ContractError(f"verified backup failed: {type(exc).__name__}") from exc

    audit_request_id = f"tg-alias-{initial.plan_sha256[:16]}"
    with storage.transaction() as conn:
        plan = build_plan(
            conn,
            tenant_id=tenant_id,
            owner_id=owner_id,
            uploader_id=uploader_id,
            files_root=files_root,
        )
        if plan.plan_sha256 != initial.plan_sha256 or plan.candidate_count != expected_count:
            raise ContractError("in-transaction plan drifted; refusing apply")
        for item in plan.candidates:
            if _telegram_file_source_ref_kind(item.source_ref) != "file":
                raise ContractError("apply only inserts closed telegram-file identities")
            ok = conn.execute(
                f"""SELECT r.id FROM raw_objects r
                     WHERE r.id=? AND r.user_id=? AND r.source='upload'
                       AND r.content_type='file' AND r.deleted_at IS NULL
                       AND r.content_hash=?
                       AND {_not_audio_document("r")}
                       AND {_not_private_raw_dependency("r")}
                       AND {_exact_uploader_raw_dependency("r")}
                       AND {_not_ignored_inbox("r")}
                     LIMIT 1""",  # nosec B608
                (item.raw_id, plan.tenant_id, item.content_hash, plan.uploader_id),
            ).fetchone()
            if ok is None:
                raise ContractError("candidate Raw failed in-transaction authorization")
            # Re-check modern-valid + disk under CAS (files root unchanged).
            meta_row = conn.execute(
                "SELECT metadata_json FROM raw_objects WHERE id=? AND user_id=?",
                (item.raw_id, plan.tenant_id),
            ).fetchone()
            metadata = _metadata_object(meta_row["metadata_json"] if meta_row else None)
            refusal, _reg = _registration_gate(
                metadata,
                content_hash=item.content_hash,
                files_root=_resolve_files_root(files_root),
            )
            if refusal is not None:
                raise ContractError("candidate registration failed in-transaction verification")
            existing = conn.execute(
                """SELECT raw_object_id FROM file_source_aliases
                    WHERE user_id=? AND uploaded_by=? AND source_ref=?""",
                (plan.tenant_id, plan.uploader_id, item.source_ref),
            ).fetchone()
            if existing is not None:
                if str(existing["raw_object_id"]) != item.raw_id:
                    raise ContractError("alias conflict under CAS")
                continue
            conn.execute(
                """INSERT INTO file_source_aliases(
                       user_id, uploaded_by, source_ref, raw_object_id, created_at
                   ) VALUES(?, ?, ?, ?, ?)""",
                (plan.tenant_id, plan.uploader_id, item.source_ref, item.raw_id, _utc_now()),
            )
            after_json = json.dumps(
                {
                    "kind": item.kind,
                    "evidence_class": item.evidence_class,
                    "registration_class": item.registration_class,
                    "raw_tag": _tag(item.raw_id),
                    "source_ref_tag": _tag(item.source_ref),
                    "uploader_tag": _tag(item.uploader_id),
                    "plan_sha256": plan.plan_sha256,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            conn.execute(
                """INSERT INTO audit_log(
                       id, user_id, action, target_type, target_id,
                       before_json, after_json, ip_address, request_id, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"audit_{_sha256(f'{audit_request_id}:{item.raw_id}:{item.source_ref}'.encode())[:24]}",
                    plan.owner_id,
                    AUDIT_ACTION,
                    "raw_object",
                    item.raw_id,
                    "{}",
                    after_json,
                    "",
                    audit_request_id,
                    _utc_now(),
                ),
            )
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE request_id=? AND action=?",
            (audit_request_id, AUDIT_ACTION),
        ).fetchone()[0]
        if int(audit_count) != plan.candidate_count:
            raise ContractError("audit row count does not match the applied row count")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise ContractError("post-apply integrity/foreign-key check failed")
        remaining = build_plan(
            conn,
            tenant_id=tenant_id,
            owner_id=owner_id,
            uploader_id=uploader_id,
            files_root=files_root,
        )
        if remaining.candidate_count != 0:
            raise ContractError("planned aliases remain after apply")

    return plan, {
        "backup": {
            "label_tag": _tag(str(backup_result.get("label") or "")),
            "ok": True,
        },
        "audit_request_tag": _tag(audit_request_id),
        "applied": plan.candidate_count,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--files-root", required=True, type=Path)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument(
        "--owner-id",
        required=True,
        help="Active operator-owner who approves the claim (must be preset owner)",
    )
    parser.add_argument(
        "--uploader-id",
        required=True,
        help="Exact target uploader scope (owner or other active tenant user, e.g. JBL)",
    )
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--claim-manifest", type=Path, default=None)
    parser.add_argument("--expect-count", type=int, default=None)
    parser.add_argument("--expect-plan-sha256", default=None)
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        if args.apply:
            if not args.claim_manifest or args.expect_count is None or not args.expect_plan_sha256:
                raise ContractError("apply requires claim manifest, expect-count, expect-plan-sha256")
            if args.backup_dir is None:
                raise ContractError("apply requires --backup-dir")
            plan, extra = apply_plan(
                args.database,
                tenant_id=args.tenant_id,
                owner_id=args.owner_id,
                uploader_id=args.uploader_id,
                files_root=args.files_root,
                claim_manifest=args.claim_manifest,
                expected_count=int(args.expect_count),
                expected_plan_sha256=str(args.expect_plan_sha256),
                backup_dir=args.backup_dir,
            )
            report = plan.public_summary(mode="apply")
            report["applied"] = True
            report["apply"] = extra
        else:
            conn = _connect(args.database, read_only=True)
            try:
                plan = build_plan(
                    conn,
                    tenant_id=args.tenant_id,
                    owner_id=args.owner_id,
                    uploader_id=args.uploader_id,
                    files_root=args.files_root,
                )
            finally:
                conn.close()
            report = plan.public_summary(mode="preview")
            report["applied"] = False
        report["report_sha256"] = _sha256(_canonical_json(report))
        if args.report is not None:
            _write_private_json(args.report, report)
        sys.stdout.write(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except ContractError as exc:
        payload = {
            "schema": REPORT_SCHEMA,
            "mode": "error",
            "applied": False,
            "error": "ContractError",
            "message": str(exc),
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
