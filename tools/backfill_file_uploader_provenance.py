#!/usr/bin/env python
"""Audit and explicitly claim legacy CLI imports for one archive owner.

The default mode is read-only.  Applying a plan deliberately needs three
independent operator assertions: an approved claim manifest, the exact row
count, and the exact SHA-256 printed by the preview.  The candidate set is
recomputed after ``BEGIN IMMEDIATE`` and every row is updated with compare-and-
swap semantics.  Legacy Telegram evidence is reported, never mutated here.

No filename, import path, Telegram identifier, file body, or file digest is
printed.  A private report contains the same content-free summary as stdout.
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

from friday.raw_metadata import RAW_FILE_METADATA_MAX_BYTES, bounded_raw_file_metadata  # noqa: E402
from friday.storage import SCHEMA_VERSION  # noqa: E402

PLAN_SCHEMA = "friday.file-uploader-provenance-plan.v1"
CLAIM_SCHEMA = "friday.file-uploader-owner-claim.v1"
REPORT_SCHEMA = "friday.file-uploader-provenance-report.v1"
CLAIM_SCOPE = "all_unattributed_cli_imports"
AUDIT_ACTION = "cli.file_uploader.backfill"
MAX_MANIFEST_BYTES = 64 * 1024
MAX_IMPORT_PATH_BYTES = 4096
HEX64 = frozenset("0123456789abcdef")


class ContractError(RuntimeError):
    """The immutable operator or database contract was not satisfied."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_identity(value: str, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 200 or any(ord(char) < 32 for char in text):
        raise ContractError(f"invalid {label}")
    return text


def _safe_hex64(value: str, *, label: str) -> str:
    text = str(value or "").strip().casefold()
    if len(text) != 64 or any(char not in HEX64 for char in text):
        raise ContractError(f"invalid {label}")
    return text


@dataclass(frozen=True)
class ImportCandidate:
    raw_id: str
    tenant_id: str
    metadata_json: str
    metadata_sha256: str
    content_hash: str
    source: str
    source_ref_sha256: str
    received_at: str

    def checksum_projection(self) -> dict[str, str]:
        return {
            "content_hash": self.content_hash,
            "metadata_sha256": self.metadata_sha256,
            "raw_id": self.raw_id,
            "received_at": self.received_at,
            "source": self.source,
            "source_ref_sha256": self.source_ref_sha256,
            "tenant_id": self.tenant_id,
        }


@dataclass(frozen=True)
class Plan:
    tenant_id: str
    owner_id: str
    candidates: tuple[ImportCandidate, ...]
    plan_sha256: str
    explicit_uploader_rows: int
    non_import_unattributed_rows: int
    unreadable_metadata_rows: int
    telegram_exact_rows: int
    telegram_ambiguous_rows: int
    telegram_unmapped_rows: int

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    def public_summary(self, *, mode: str) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "mode": mode,
            "tenant_tag": _sha256(self.tenant_id.encode("utf-8"))[:16],
            "owner_tag": _sha256(self.owner_id.encode("utf-8"))[:16],
            "claim_scope": CLAIM_SCOPE,
            "candidate_count": self.candidate_count,
            "plan_sha256": self.plan_sha256,
            "blocked": {
                "explicit_uploader": self.explicit_uploader_rows,
                "non_import_unattributed": self.non_import_unattributed_rows,
                "unreadable_metadata": self.unreadable_metadata_rows,
            },
            "legacy_telegram_report_only": {
                "exact": self.telegram_exact_rows,
                "ambiguous": self.telegram_ambiguous_rows,
                "unmapped": self.telegram_unmapped_rows,
                "mutation_enabled": False,
            },
        }


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
    required = {"raw_objects", "users", "channel_sessions", "audit_log", "schema_meta"}
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?,?)",
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


def _is_import_candidate(metadata: Mapping[str, Any]) -> bool:
    if "uploaded_by" in metadata:
        return False
    import_path = metadata.get("import_source_path")
    if not isinstance(import_path, str) or not import_path.strip():
        return False
    try:
        return len(import_path.encode("utf-8")) <= MAX_IMPORT_PATH_BYTES
    except UnicodeError:
        return False


def _legacy_telegram_status(
    conn: sqlite3.Connection,
    metadata: Mapping[str, Any],
) -> str | None:
    if "uploaded_by" in metadata or "import_source_path" in metadata:
        return None
    channel = str(metadata.get("channel") or "").strip().casefold()
    chat_value = metadata.get("chat_id")
    chat_id = str(chat_value).strip() if isinstance(chat_value, (str, int)) else ""
    if channel not in {"telegram", "telegram-bridge", "api-token"}:
        return None
    if not chat_id or not chat_id.isascii() or not chat_id.isdecimal() or int(chat_id) <= 0:
        return "unmapped"
    users = conn.execute(
        "SELECT id FROM users WHERE status='active' AND external_id=? LIMIT 3",
        (chat_id,),
    ).fetchall()
    sessions = conn.execute(
        "SELECT user_id FROM channel_sessions WHERE channel='telegram' AND channel_id=? LIMIT 3",
        (chat_id,),
    ).fetchall()
    if not users or not sessions:
        return "unmapped"
    user_ids = {str(row["id"]) for row in users}
    session_users = [str(row["user_id"]) for row in sessions]
    if len(users) == 1 and len(sessions) == 1 and session_users[0] in user_ids:
        return "exact"
    return "ambiguous"


def build_plan(conn: sqlite3.Connection, *, tenant_id: str, owner_id: str) -> Plan:
    tenant = _safe_identity(tenant_id, label="tenant id")
    owner = _safe_identity(owner_id, label="owner id")
    _required_tables(conn)
    if not _owner_is_active(conn, owner):
        raise ContractError("owner id is not one unique active owner account")
    rows = conn.execute(
        """SELECT id, user_id, source, source_ref, content_hash, received_at, metadata_json
             FROM raw_objects
            WHERE user_id=? AND deleted_at IS NULL AND content_type='file'
            ORDER BY id""",
        (tenant,),
    ).fetchall()
    candidates: list[ImportCandidate] = []
    explicit = non_import = unreadable = 0
    telegram = {"exact": 0, "ambiguous": 0, "unmapped": 0}
    for row in rows:
        metadata_json = row["metadata_json"]
        metadata = _strict_metadata(metadata_json)
        if metadata is None:
            unreadable += 1
            continue
        telegram_status = _legacy_telegram_status(conn, metadata)
        if telegram_status is not None:
            telegram[telegram_status] += 1
        if "uploaded_by" in metadata:
            explicit += 1
            continue
        if not _is_import_candidate(metadata):
            non_import += 1
            continue
        raw_id = str(row["id"] or "")
        if not raw_id:
            raise ContractError("candidate has no Raw id")
        candidates.append(
            ImportCandidate(
                raw_id=raw_id,
                tenant_id=str(row["user_id"] or ""),
                metadata_json=str(metadata_json),
                metadata_sha256=_sha256(str(metadata_json).encode("utf-8")),
                content_hash=str(row["content_hash"] or ""),
                source=str(row["source"] or ""),
                source_ref_sha256=_sha256(str(row["source_ref"] or "").encode("utf-8")),
                received_at=str(row["received_at"] or ""),
            )
        )
    candidates.sort(key=lambda item: item.raw_id)
    basis = {
        "schema": PLAN_SCHEMA,
        "claim_scope": CLAIM_SCOPE,
        "owner_id": owner,
        "tenant_id": tenant,
        "candidates": [item.checksum_projection() for item in candidates],
    }
    return Plan(
        tenant_id=tenant,
        owner_id=owner,
        candidates=tuple(candidates),
        plan_sha256=_sha256(_canonical_json(basis)),
        explicit_uploader_rows=explicit,
        non_import_unattributed_rows=non_import,
        unreadable_metadata_rows=unreadable,
        telegram_exact_rows=telegram["exact"],
        telegram_ambiguous_rows=telegram["ambiguous"],
        telegram_unmapped_rows=telegram["unmapped"],
    )


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
            stream.write(_canonical_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _updated_metadata(candidate: ImportCandidate, owner_id: str) -> str:
    metadata = _strict_metadata(candidate.metadata_json)
    if metadata is None or not _is_import_candidate(metadata):
        raise ContractError("candidate stopped satisfying the import-only contract")
    updated = dict(metadata)
    updated["uploaded_by"] = owner_id
    encoded = json.dumps(updated, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > RAW_FILE_METADATA_MAX_BYTES:
        raise ContractError("updated metadata exceeds the bounded Raw envelope")
    return encoded


def _postcheck_transaction(
    conn: sqlite3.Connection,
    plan: Plan,
    *,
    audit_request_id: str,
) -> None:
    for candidate in plan.candidates:
        row = conn.execute(
            """SELECT source, source_ref, content_hash, received_at, metadata_json
                 FROM raw_objects WHERE id=? AND user_id=? AND deleted_at IS NULL""",
            (candidate.raw_id, plan.tenant_id),
        ).fetchone()
        if row is None:
            raise ContractError("updated Raw disappeared before commit")
        metadata = _strict_metadata(row["metadata_json"])
        if metadata is None or metadata.get("uploaded_by") != plan.owner_id:
            raise ContractError("uploader postcondition failed")
        without_uploader = dict(metadata)
        without_uploader.pop("uploaded_by", None)
        original = _strict_metadata(candidate.metadata_json)
        if without_uploader != original:
            raise ContractError("backfill changed metadata other than uploaded_by")
        if (
            str(row["source"] or "") != candidate.source
            or _sha256(str(row["source_ref"] or "").encode("utf-8")) != candidate.source_ref_sha256
            or str(row["content_hash"] or "") != candidate.content_hash
            or str(row["received_at"] or "") != candidate.received_at
        ):
            raise ContractError("backfill changed immutable Raw provenance")
    audit_count = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE request_id=? AND action=?",
        (audit_request_id, AUDIT_ACTION),
    ).fetchone()[0]
    if int(audit_count) != plan.candidate_count:
        raise ContractError("audit row count does not match the applied row count")
    if build_plan(conn, tenant_id=plan.tenant_id, owner_id=plan.owner_id).candidate_count != 0:
        raise ContractError("the in-transaction import scope was not fully attributed")
    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if integrity != "ok" or foreign_keys:
        raise ContractError("post-apply integrity/foreign-key check failed")


def apply_plan(
    database: Path,
    *,
    tenant_id: str,
    owner_id: str,
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
        initial_plan = build_plan(preflight, tenant_id=tenant_id, owner_id=owner_id)
    finally:
        preflight.close()
    _validate_claim(
        claim,
        initial_plan,
        expected_count=expected_count,
        expected_sha256=expected_sha,
    )
    _ensure_private_directory(backup_dir)
    # FridayStorage is load-bearing here: its transaction establishes
    # BEGIN IMMEDIATE and atomically republishes the privacy derivative cache
    # invalidated by Raw metadata updates. A bare sqlite3 connection would leave
    # every protected read fail-closed until the next backend restart.
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
    try:
        # Backup precedes the writer transaction. If any other writer changes the
        # logical candidate set in between, the in-transaction checksum recheck
        # refuses all mutations; the verified backup merely remains as evidence.
        made = storage.create_backup(label=f"pre-uploader-backfill-{initial_plan.plan_sha256[:12]}")
        verified = storage.verify_backup(str(made.get("database") or ""))
        if verified.get("ok") is not True:
            raise ContractError("pre-apply backup verification failed")
        backup_result = {
            "database": str(made.get("database") or ""),
            "manifest": Path(str(made.get("manifest_path") or "")).name,
            "verified": True,
        }
        with storage.transaction() as conn:
            plan = build_plan(conn, tenant_id=tenant_id, owner_id=owner_id)
            _validate_claim(claim, plan, expected_count=expected_count, expected_sha256=expected_sha)
            request_id = f"uploader-backfill-{plan.plan_sha256[:24]}"
            now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            for candidate in plan.candidates:
                new_metadata = _updated_metadata(candidate, plan.owner_id)
                cursor = conn.execute(
                    """UPDATE raw_objects SET metadata_json=?
                        WHERE id=? AND user_id=? AND metadata_json=?
                          AND deleted_at IS NULL AND content_type='file'""",
                    (new_metadata, candidate.raw_id, plan.tenant_id, candidate.metadata_json),
                )
                if cursor.rowcount != 1:
                    raise ContractError("candidate compare-and-swap failed")
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
                        '{"applied":true,"operation":"uploader_provenance_backfill",'
                        '"provenance":"explicit_owner_claim","status":"accepted"}',
                        "",
                        request_id,
                        now,
                    ),
                )
            _postcheck_transaction(conn, plan, audit_request_id=request_id)
        committed = True
    except BaseException:
        raise
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
    return plan, backup_result


def audit_plan(database: Path, *, tenant_id: str, owner_id: str) -> Plan:
    conn = _connect(database, read_only=True)
    try:
        return build_plan(conn, tenant_id=tenant_id, owner_id=owner_id)
    finally:
        conn.close()


def _write_report(path: Path | None, report: Mapping[str, Any]) -> None:
    if path is not None:
        _write_private_json(path, report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--claim-manifest", type=Path)
    parser.add_argument("--expect-count", type=int)
    parser.add_argument("--expect-plan-sha256")
    parser.add_argument("--backup-dir", type=Path)
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
            plan, backup = apply_plan(
                args.database,
                tenant_id=args.tenant_id,
                owner_id=args.owner_id,
                claim_manifest=args.claim_manifest,
                expected_count=args.expect_count,
                expected_plan_sha256=args.expect_plan_sha256,
                backup_dir=args.backup_dir,
            )
            report = {**plan.public_summary(mode="applied"), "backup": backup, "applied": True}
        else:
            if any(
                value is not None
                for value in (
                    args.claim_manifest,
                    args.expect_count,
                    args.expect_plan_sha256,
                    args.backup_dir,
                )
            ):
                raise ContractError("apply-only arguments require --apply")
            plan = audit_plan(args.database, tenant_id=args.tenant_id, owner_id=args.owner_id)
            report = {**plan.public_summary(mode="dry_run"), "applied": False}
        _write_report(args.report, report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except (ContractError, OSError, sqlite3.Error) as exc:
        print(
            json.dumps(
                {
                    "schema": REPORT_SCHEMA,
                    "mode": "error",
                    "error": type(exc).__name__,
                    "applied": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
