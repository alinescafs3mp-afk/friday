#!/usr/bin/env python
"""Repair historical deduplicated-upload filenames from durable turn evidence.

Preview is read-only. Apply requires an exact private claim, a verified backup,
and an unchanged plan. The schema migration itself deliberately leaves legacy
aliases empty. A recovered basename is inserted under a new code-owned identity
derived from the exact immutable synthetic message id; timestamps and Telegram
carrier aliases are never guessed, updated, or cross-correlated.

The message row owns the shared tenant, while the immutable Raw metadata owns
the authenticated uploader identity.  A repair may therefore cover another
active uploader in the same tenant only when the exact singleton message, Raw
tenant and duplicate-key-free ``uploaded_by`` provenance all agree. Public
output contains counts and digests only. Filenames, message ids, Raw ids, and
derived source references are used in the private plan digest but are never
printed or written to the public receipt.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from friday.config import load_settings  # noqa: E402
from friday.diagnostics.runtime_lease import (  # noqa: E402
    ProcessLease,
    RuntimeLeaseError,
    process_owns_lease,
)
from friday.storage import SCHEMA_VERSION, FridayStorage  # noqa: E402
from friday.storage._privacy import (  # noqa: E402
    _exact_uploader_raw_dependency,
    _not_audio_document,
    _not_private_raw_dependency,
)
from tools.backfill_telegram_file_aliases import (  # noqa: E402
    ContractError,
    _canonical_json,
    _connect,
    _ensure_private_directory,
    _safe_hex64,
    _safe_identity,
    _sha256,
    _tag,
    _unique_object,
    _write_private_json,
)

PLAN_SCHEMA = "friday.file-alias-filenames-plan.v1"
CLAIM_SCHEMA = "friday.file-alias-filenames-claim.v1"
REPORT_SCHEMA = "friday.file-alias-filenames-report.v1"
EXTERNAL_BACKUP_SCHEMA = "friday.file-alias-filenames-external-backup.v1"
CLAIM_SCOPE = "repair_historical_file_alias_filenames"
NOTICE_PREFIX = "Загружен документ: "
MAX_NOTICE_ROWS = 10_000
MAX_CANDIDATES = 2_048
MAX_METADATA_BYTES = 65_536
MAX_MANIFEST_BYTES = 64 * 1024
MESSAGE_NAME_ALIAS_PREFIX = "friday-message-name:"
_MESSAGE_ID_RE = re.compile(r"^msg_[0-9a-f]{16}$")
_RAW_ID_RE = re.compile(r"^raw_[0-9a-f]{16}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_NOT_IGNORED_INBOX = """NOT EXISTS (
    SELECT 1 FROM inbox i
     WHERE i.raw_object_id=r.id AND i.user_id=r.user_id AND i.status='ignored'
)"""


def _metadata_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_METADATA_BYTES:
        return None
    try:
        parsed = json.loads(value, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _closed_filename_from_notice(content: Any) -> str:
    text = str(content or "")
    if not text.startswith(NOTICE_PREFIX):
        return ""
    filename = text.removeprefix(NOTICE_PREFIX)
    if (
        not filename
        or filename != filename.strip()
        or len(filename) > 260
        or any(char in filename for char in ("/", "\\", "\x00", "\r", "\n"))
    ):
        return ""
    return filename


def _singleton_raw_id(metadata: Mapping[str, Any]) -> str:
    attached = metadata.get("conversation_attachment_raw_ids")
    uploaded = metadata.get("conversation_uploaded_raw_ids")
    if (
        metadata.get("synthetic_document_notice") is not True
        or not isinstance(attached, list)
        or not isinstance(uploaded, list)
        or len(attached) != 1
        or attached != uploaded
        or not isinstance(attached[0], str)
    ):
        return ""
    raw_id = attached[0]
    return raw_id if _RAW_ID_RE.fullmatch(raw_id) else ""


def _utc_timestamp(value: Any) -> datetime | None:
    text = str(value or "")
    if not text or len(text) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _message_name_source_ref(message_id: str) -> str:
    if not _MESSAGE_ID_RE.fullmatch(str(message_id or "")):
        return ""
    return f"{MESSAGE_NAME_ALIAS_PREFIX}{message_id}"


def _required_schema(conn: sqlite3.Connection) -> None:
    required = {
        "conversations",
        "file_source_aliases",
        "inbox",
        "messages",
        "raw_objects",
        "schema_meta",
        "users",
    }
    present = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if str(row[0]) in required
    }
    if present != required:
        raise ContractError("database does not have the required Friday tables")
    schema = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if schema is None or str(schema[0]) != str(SCHEMA_VERSION):
        raise ContractError("database must already be at the current Friday schema")
    try:
        FridayStorage._validate_file_source_alias_schema(conn)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 - normalize private schema detail
        raise ContractError("schema-34 alias filename invariant is not intact") from exc


def _lease_is_exactly_held(lease: ProcessLease) -> bool:
    try:
        lexical = os.stat(lease.path, follow_symlinks=False)
    except OSError:
        return False
    return bool(
        lease.acquired
        and process_owns_lease(lease.path, protocol=lease.protocol)
        and lease.held_file_identity == (int(lexical.st_dev), int(lexical.st_ino))
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        lexical = os.stat(path, follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError("database identity is unavailable") from exc
    if (
        resolved != Path(os.path.abspath(path))
        or not path.is_file()
        or path.is_symlink()
        or lexical.st_nlink != 1
        or lexical.st_uid != os.geteuid()
        or lexical.st_mode & 0o077
    ):
        raise ContractError("database identity is not private and regular")
    return (
        int(lexical.st_dev),
        int(lexical.st_ino),
        int(lexical.st_size),
        int(lexical.st_mtime_ns),
        int(lexical.st_ctime_ns),
    )


@contextmanager
def _release_operation_lock(state_dir: Path):
    """Coordinate standalone apply with immutable activate/recover operations."""

    try:
        root = state_dir.resolve(strict=True)
        root_status = os.stat(state_dir, follow_symlinks=False)
    except OSError as exc:
        raise ContractError("release operation lock directory is unavailable") from exc
    if (
        root != Path(os.path.abspath(state_dir))
        or state_dir.is_symlink()
        or not stat.S_ISDIR(root_status.st_mode)
        or root_status.st_uid != os.geteuid()
        or stat.S_IMODE(root_status.st_mode) & 0o077
    ):
        raise ContractError("release operation lock directory is unsafe")
    path = root / "immutable-release-operator.v1.lock"
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        status = os.fstat(descriptor)
        lexical = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) & 0o077
            or (status.st_dev, status.st_ino) != (lexical.st_dev, lexical.st_ino)
        ):
            raise ContractError("release operation lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ContractError("another immutable release operation is in progress") from exc
        yield
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _verify_external_backup(receipt: ExternalBackupReceipt, database: Path) -> dict[str, str]:
    if type(receipt) is not ExternalBackupReceipt or receipt.schema != EXTERNAL_BACKUP_SCHEMA:
        raise ContractError("external backup receipt schema is invalid")
    manifest_path = receipt.manifest_path
    try:
        manifest_status = os.stat(manifest_path, follow_symlinks=False)
        parent_status = os.stat(manifest_path.parent, follow_symlinks=False)
    except OSError as exc:
        raise ContractError("external backup manifest is unavailable") from exc
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_status.st_nlink != 1
        or manifest_status.st_uid != os.geteuid()
        or manifest_status.st_mode & 0o077
        or manifest_path.parent.is_symlink()
        or not manifest_path.parent.is_dir()
        or parent_status.st_uid != os.geteuid()
        or parent_status.st_mode & 0o077
    ):
        raise ContractError("external backup manifest is not private and regular")
    raw = manifest_path.read_bytes()
    if not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise ContractError("external backup manifest size is invalid")
    if _sha256(raw) != _safe_hex64(receipt.manifest_sha256, label="backup manifest SHA-256"):
        raise ContractError("external backup manifest digest changed")
    try:
        manifest = json.loads(raw.decode("ascii"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError("external backup manifest is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"database_schema", "files", "schema"}
        or manifest.get("schema") != "friday.immutable-cutover-exact-backup.v1"
        or type(manifest.get("database_schema")) is not int
        or not isinstance(manifest.get("files"), list)
    ):
        raise ContractError("external backup manifest shape is invalid")
    allowed_names = {
        "database.sqlite3",
        "database.sqlite3-wal",
        "database.sqlite3-shm",
        "inbox.sqlite3",
        "inbox.sqlite3-wal",
        "inbox.sqlite3-shm",
    }
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_item in manifest["files"]:
        if not isinstance(raw_item, dict) or set(raw_item) != {"name", "sha256", "size"}:
            raise ContractError("external backup file entry is invalid")
        name = raw_item.get("name")
        size = raw_item.get("size")
        digest = _safe_hex64(str(raw_item.get("sha256") or ""), label="backup file SHA-256")
        if (
            not isinstance(name, str)
            or name not in allowed_names
            or name in seen
            or type(size) is not int
            or size < 0
        ):
            raise ContractError("external backup file identity is invalid")
        seen.add(name)
        file_path = manifest_path.parent / name
        try:
            file_status = os.stat(file_path, follow_symlinks=False)
        except OSError as exc:
            raise ContractError("external backup file is unavailable") from exc
        if (
            file_path.is_symlink()
            or not file_path.is_file()
            or file_status.st_nlink != 1
            or file_status.st_uid != os.geteuid()
            or file_status.st_mode & 0o077
            or file_status.st_size != size
            or _file_sha256(file_path) != digest
        ):
            raise ContractError("external backup file changed")
        seen.add(name)
        entries.append({"name": name, "sha256": digest, "size": size})
    if "database.sqlite3" not in seen or "inbox.sqlite3" not in seen:
        raise ContractError("external backup main file is missing")
    scratch = Path(tempfile.mkdtemp(prefix=".alias-backup-verify-", dir=manifest_path.parent))
    os.chmod(scratch, 0o700)
    try:
        for item in entries:
            if str(item["name"]).endswith("-shm"):
                continue
            destination = scratch / str(item["name"])
            shutil.copyfile(manifest_path.parent / str(item["name"]), destination)
            os.chmod(destination, 0o600)
        for name, require_schema in (("database.sqlite3", True), ("inbox.sqlite3", False)):
            backup_connection: sqlite3.Connection | None = None
            try:
                backup_connection = sqlite3.connect(
                    f"file:{scratch / name}?mode=ro",
                    uri=True,
                    isolation_level=None,
                )
                integrity = str(backup_connection.execute("PRAGMA integrity_check").fetchone()[0])
                foreign_keys = backup_connection.execute("PRAGMA foreign_key_check").fetchall()
                if integrity != "ok" or foreign_keys:
                    raise ContractError("external backup SQLite integrity failed")
                if require_schema:
                    schema = backup_connection.execute(
                        "SELECT value FROM schema_meta WHERE key='schema_version'"
                    ).fetchone()
                    if schema is None or int(schema[0]) != int(manifest["database_schema"]):
                        raise ContractError("external backup schema changed")
            except (sqlite3.Error, TypeError, ValueError) as exc:
                raise ContractError("external backup SQLite verification failed") from exc
            finally:
                if backup_connection is not None:
                    backup_connection.close()
    finally:
        for child in scratch.iterdir():
            child.unlink(missing_ok=True)
        scratch.rmdir()
    database_basis = [item for item in entries if str(item["name"]).startswith("database")]
    inbox_basis = [item for item in entries if str(item["name"]).startswith("inbox")]
    if _sha256(_canonical_json(database_basis)) != _safe_hex64(
        receipt.database_files_sha256,
        label="database backup receipt SHA-256",
    ) or _sha256(_canonical_json(inbox_basis)) != _safe_hex64(
        receipt.inbox_files_sha256,
        label="inbox backup receipt SHA-256",
    ):
        raise ContractError("external backup component receipt changed")
    if _file_identity(database) != receipt.live_database_identity or _file_sha256(database) != _safe_hex64(
        receipt.live_database_sha256, label="live database SHA-256"
    ):
        raise ContractError("live database changed after migration attestation")
    return {
        "manifest_sha256": receipt.manifest_sha256,
        "database_files_sha256": receipt.database_files_sha256,
        "inbox_files_sha256": receipt.inbox_files_sha256,
        "live_database_sha256": receipt.live_database_sha256,
    }


@contextmanager
def _quiesced_writer_leases(state_dir: Path):
    """Exclude both code-owned SQLite writer processes for backup + apply."""

    root = state_dir.resolve(strict=True)
    if state_dir.is_symlink() or not root.is_dir():
        raise ContractError("configured state directory is not a safe directory")
    leases = (
        ProcessLease(root / "backend.lock", protocol="friday.backend.v1"),
        ProcessLease(root / "telegram-inbox.sqlite3.lock", protocol="friday.telegram-bridge.v1"),
    )
    acquired: list[ProcessLease] = []
    try:
        for lease in leases:
            try:
                lease.acquire()
            except (OSError, RuntimeLeaseError) as exc:
                raise ContractError("backend/bridge writers are not quiesced") from exc
            acquired.append(lease)
            if not _lease_is_exactly_held(lease):
                raise ContractError("writer process lease identity is not exact")
        yield {
            "leases": leases,
            "protocols_sha256": _sha256(_canonical_json(sorted(lease.protocol for lease in leases))),
        }
    finally:
        for lease in reversed(acquired):
            lease.release()


def _active_owner(conn: sqlite3.Connection, owner_id: str) -> bool:
    rows = conn.execute(
        "SELECT preset_key,status FROM users WHERE id=? LIMIT 2",
        (owner_id,),
    ).fetchall()
    return bool(
        len(rows) == 1
        and str(rows[0]["preset_key"] or "") == "owner"
        and str(rows[0]["status"] or "") == "active"
    )


def _active_uploader(conn: sqlite3.Connection, uploader_id: str) -> bool:
    rows = conn.execute("SELECT status FROM users WHERE id=? LIMIT 2", (uploader_id,)).fetchall()
    return bool(len(rows) == 1 and str(rows[0]["status"] or "") == "active")


@dataclass(frozen=True)
class _Notice:
    message_id: str
    raw_id: str
    filename: str
    created_at: str
    timestamp: datetime
    metadata_sha256: str


@dataclass(frozen=True)
class FilenameCandidate:
    tenant_id: str
    uploader_id: str
    message_id: str
    raw_id: str
    source_ref: str
    filename: str
    created_at: str
    notice_metadata_sha256: str
    raw_content_hash: str
    raw_metadata_sha256: str

    def private_basis(self) -> dict[str, str]:
        return {
            "created_at": self.created_at,
            "filename": self.filename,
            "message_id": self.message_id,
            "raw_content_hash": self.raw_content_hash,
            "raw_id": self.raw_id,
            "raw_metadata_sha256": self.raw_metadata_sha256,
            "source_ref": self.source_ref,
            "notice_metadata_sha256": self.notice_metadata_sha256,
            "tenant_id": self.tenant_id,
            "uploader_id": self.uploader_id,
        }


@dataclass(frozen=True)
class Plan:
    tenant_id: str
    owner_id: str
    uploader_id: str
    candidates: tuple[FilenameCandidate, ...]
    counts: dict[str, int]
    plan_sha256: str

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
            "counts": dict(self.counts),
        }


@dataclass(frozen=True)
class ExternalBackupReceipt:
    """Private cutover evidence passed by the lease-owning release operator."""

    schema: str
    manifest_path: Path
    manifest_sha256: str
    database_files_sha256: str
    inbox_files_sha256: str
    live_database_identity: tuple[int, int, int, int, int]
    live_database_sha256: str


def _notice_rows(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
) -> list[_Notice]:
    count = int(
        conn.execute(
            """SELECT COUNT(*) FROM messages
                WHERE user_id=? AND role='user'
                  AND substr(content,1,?)=?""",
            (tenant_id, len(NOTICE_PREFIX), NOTICE_PREFIX),
        ).fetchone()[0]
    )
    if count > MAX_NOTICE_ROWS:
        raise ContractError("synthetic notice scan exceeds its fixed bound")
    rows = conn.execute(
        """SELECT m.id,m.content,m.metadata_json,m.created_at
             FROM messages m
             JOIN conversations c ON c.id=m.conversation_id AND c.user_id=m.user_id
            WHERE m.user_id=? AND m.role='user'
              AND substr(m.content,1,?)=?
            ORDER BY m.created_at,m.id""",
        (tenant_id, len(NOTICE_PREFIX), NOTICE_PREFIX),
    ).fetchall()
    notices: list[_Notice] = []
    for row in rows:
        if not _MESSAGE_ID_RE.fullmatch(str(row["id"] or "")):
            continue
        filename = _closed_filename_from_notice(row["content"])
        raw_metadata = str(row["metadata_json"] or "")
        metadata = _metadata_object(raw_metadata)
        raw_id = _singleton_raw_id(metadata or {})
        timestamp = _utc_timestamp(row["created_at"])
        if not filename or not raw_id or timestamp is None:
            continue
        notices.append(
            _Notice(
                message_id=str(row["id"]),
                raw_id=raw_id,
                filename=filename,
                created_at=str(row["created_at"]),
                timestamp=timestamp,
                metadata_sha256=_sha256(raw_metadata.encode("utf-8")),
            )
        )
    return notices


def _authorized_raw(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    uploader_id: str,
    raw_id: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        f"""SELECT r.id,r.content_hash,r.metadata_json
              FROM raw_objects r
             WHERE r.id=? AND r.user_id=? AND r.source='upload'
               AND r.content_type='file' AND r.deleted_at IS NULL
               AND {_not_audio_document("r")}
               AND {_not_private_raw_dependency("r")}
               AND {_exact_uploader_raw_dependency("r")}
               AND {_NOT_IGNORED_INBOX}
             LIMIT 1""",  # nosec B608 - fixed code-owned predicates
        (raw_id, tenant_id, uploader_id),
    ).fetchone()
    if (
        row is None
        or not _RAW_ID_RE.fullmatch(str(row["id"] or ""))
        or not _HEX64_RE.fullmatch(str(row["content_hash"] or ""))
    ):
        return None
    return row


def build_plan(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
    uploader_id: str,
) -> Plan:
    tenant = _safe_identity(tenant_id, label="tenant id")
    owner = _safe_identity(owner_id, label="owner id")
    uploader = _safe_identity(uploader_id, label="uploader id")
    _required_schema(conn)
    if not _active_owner(conn, owner):
        raise ContractError("owner id is not one unique active owner account")
    if not _active_uploader(conn, uploader):
        raise ContractError("uploader id is not one unique active authenticated user")

    notices = _notice_rows(conn, tenant_id=tenant)
    counts: Counter[str] = Counter(
        {
            "closed_notices": len(notices),
            "planned_updates": 0,
            "already_repaired": 0,
            "refused_raw_authority": 0,
            "refused_existing_conflict": 0,
            "refused_candidate_collision": 0,
        }
    )
    provisional: list[FilenameCandidate] = []
    for notice in notices:
        raw = _authorized_raw(
            conn,
            tenant_id=tenant,
            uploader_id=uploader,
            raw_id=notice.raw_id,
        )
        if raw is None:
            counts["refused_raw_authority"] += 1
            continue
        source_ref = _message_name_source_ref(notice.message_id)
        existing = conn.execute(
            """SELECT raw_object_id,supplied_filename,created_at
                 FROM file_source_aliases
                WHERE user_id=? AND uploaded_by=? AND source_ref=?
                LIMIT 2""",
            (tenant, uploader, source_ref),
        ).fetchall()
        if existing:
            if (
                len(existing) == 1
                and str(existing[0]["raw_object_id"] or "") == notice.raw_id
                and str(existing[0]["supplied_filename"] or "") == notice.filename
                and str(existing[0]["created_at"] or "") == notice.created_at
            ):
                counts["already_repaired"] += 1
            else:
                counts["refused_existing_conflict"] += 1
            continue
        raw_metadata = str(raw["metadata_json"] or "")
        provisional.append(
            FilenameCandidate(
                tenant_id=tenant,
                uploader_id=uploader,
                message_id=notice.message_id,
                raw_id=notice.raw_id,
                source_ref=source_ref,
                filename=notice.filename,
                created_at=notice.created_at,
                notice_metadata_sha256=notice.metadata_sha256,
                raw_content_hash=str(raw["content_hash"] or ""),
                raw_metadata_sha256=_sha256(raw_metadata.encode("utf-8")),
            )
        )

    carrier_counts = Counter(item.source_ref for item in provisional)
    candidates = [item for item in provisional if carrier_counts[item.source_ref] == 1]
    counts["refused_candidate_collision"] += len(provisional) - len(candidates)
    candidates.sort(key=lambda item: (item.created_at, item.message_id, item.source_ref))
    if len(candidates) > MAX_CANDIDATES:
        raise ContractError("candidate count exceeds its fixed apply bound")
    counts["planned_updates"] = len(candidates)
    private_basis = {
        "schema": PLAN_SCHEMA,
        "claim_scope": CLAIM_SCOPE,
        "tenant_id": tenant,
        "owner_id": owner,
        "uploader_id": uploader,
        "source_ref_prefix": MESSAGE_NAME_ALIAS_PREFIX,
        "candidates": [item.private_basis() for item in candidates],
    }
    return Plan(
        tenant_id=tenant,
        owner_id=owner,
        uploader_id=uploader,
        candidates=tuple(candidates),
        counts=dict(counts),
        plan_sha256=_sha256(_canonical_json(private_basis)),
    )


def _load_claim(path: Path) -> dict[str, Any]:
    try:
        lexical = os.stat(path, follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ContractError("claim manifest must be a private regular non-symlink file") from exc
    if (
        resolved != Path(os.path.abspath(path))
        or path.is_symlink()
        or not path.is_file()
        or lexical.st_nlink != 1
        or lexical.st_uid != os.geteuid()
        or lexical.st_mode & 0o077
    ):
        raise ContractError("claim manifest must be a private regular non-symlink file")
    data = path.read_bytes()
    if not data or len(data) > MAX_MANIFEST_BYTES:
        raise ContractError("claim manifest size is invalid")
    try:
        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError("claim manifest is invalid JSON") from exc
    expected = {
        "approved",
        "candidate_count",
        "claim_scope",
        "owner_id",
        "plan_sha256",
        "schema",
        "tenant_id",
        "uploader_id",
    }
    if not isinstance(parsed, dict) or set(parsed) != expected:
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
    manifest_sha = _safe_hex64(str(claim.get("plan_sha256") or ""), label="claim plan SHA-256")
    if (
        int(claim["candidate_count"]) != expected_count
        or expected_count != plan.candidate_count
        or manifest_sha != expected_sha256
        or expected_sha256 != plan.plan_sha256
    ):
        raise ContractError("claim manifest/count/checksum does not match the current plan")
    if expected_count <= 0:
        raise ContractError("refusing an empty apply plan")


def _apply_locked_plan(
    storage: FridayStorage,
    initial: Plan,
    *,
    expected_count: int,
    leases_held: Callable[[], bool],
) -> Plan:
    if not leases_held():
        raise ContractError("writer lease identity changed before apply")
    with storage.transaction() as conn:
        if not leases_held():
            raise ContractError("writer lease identity changed inside apply")
        current = build_plan(
            conn,
            tenant_id=initial.tenant_id,
            owner_id=initial.owner_id,
            uploader_id=initial.uploader_id,
        )
        if current.plan_sha256 != initial.plan_sha256 or current.candidate_count != expected_count:
            raise ContractError("in-transaction plan drifted; refusing apply")
        for item in current.candidates:
            conn.execute(
                """INSERT INTO file_source_aliases(
                       user_id,uploaded_by,source_ref,raw_object_id,
                       supplied_filename,created_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    item.tenant_id,
                    item.uploader_id,
                    item.source_ref,
                    item.raw_id,
                    item.filename,
                    item.created_at,
                ),
            )
            verified = conn.execute(
                """SELECT a.supplied_filename,a.created_at,r.content_hash,r.metadata_json
                     FROM file_source_aliases a
                     JOIN raw_objects r ON r.id=a.raw_object_id AND r.user_id=a.user_id
                    WHERE a.user_id=? AND a.uploaded_by=? AND a.source_ref=?
                      AND a.raw_object_id=?""",
                (item.tenant_id, item.uploader_id, item.source_ref, item.raw_id),
            ).fetchone()
            if (
                verified is None
                or str(verified["supplied_filename"] or "") != item.filename
                or str(verified["created_at"] or "") != item.created_at
                or str(verified["content_hash"] or "") != item.raw_content_hash
                or _sha256(str(verified["metadata_json"] or "").encode("utf-8")) != item.raw_metadata_sha256
            ):
                raise ContractError("post-insert alias or immutable Raw verification failed")
            notice = conn.execute(
                """SELECT m.content,m.metadata_json,m.created_at
                     FROM messages m
                     JOIN conversations c
                       ON c.id=m.conversation_id AND c.user_id=m.user_id
                    WHERE m.id=? AND m.user_id=? AND m.role='user'
                    LIMIT 2""",
                (item.message_id, item.tenant_id),
            ).fetchall()
            if len(notice) != 1:
                raise ContractError("exact synthetic message identity changed during apply")
            notice_metadata = str(notice[0]["metadata_json"] or "")
            if (
                _closed_filename_from_notice(notice[0]["content"]) != item.filename
                or _singleton_raw_id(_metadata_object(notice_metadata) or {}) != item.raw_id
                or str(notice[0]["created_at"] or "") != item.created_at
                or _sha256(notice_metadata.encode("utf-8")) != item.notice_metadata_sha256
                or _message_name_source_ref(item.message_id) != item.source_ref
            ):
                raise ContractError("exact synthetic message evidence changed during apply")
            public_matches = storage.find_owned_files_by_filename(
                item.tenant_id,
                item.uploader_id,
                item.filename,
            )
            if not any(
                str(match.get("id") or "") == item.raw_id
                and str(match.get("filename") or "") == item.filename
                for match in public_matches
            ):
                raise ContractError("public exact filename lookup did not expose the repaired alias")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        if integrity != "ok" or foreign_keys:
            raise ContractError("post-apply integrity/foreign-key check failed")
        remaining = build_plan(
            conn,
            tenant_id=initial.tenant_id,
            owner_id=initial.owner_id,
            uploader_id=initial.uploader_id,
        )
        if remaining.candidate_count != 0:
            raise ContractError("planned alias filenames remain after apply")
    return current


def apply_plan(
    database: Path,
    *,
    tenant_id: str,
    owner_id: str,
    uploader_id: str,
    claim_manifest: Path,
    expected_count: int,
    expected_plan_sha256: str,
    backup_dir: Path,
) -> tuple[Plan, dict[str, Any]]:
    expected_sha = _safe_hex64(expected_plan_sha256, label="expected plan SHA-256")
    if type(expected_count) is not int or expected_count <= 0:
        raise ContractError("expected count must be a positive integer")
    loaded_settings = load_settings()
    if loaded_settings.database_path.resolve(strict=True) != database.resolve(strict=True):
        raise ContractError("database is not the configured backend database for this lease namespace")
    with _release_operation_lock(loaded_settings.state_dir):
        claim = _load_claim(claim_manifest)
        preflight = _connect(database, read_only=True)
        try:
            initial = build_plan(
                preflight,
                tenant_id=tenant_id,
                owner_id=owner_id,
                uploader_id=uploader_id,
            )
        finally:
            preflight.close()
        _validate_claim(claim, initial, expected_count=expected_count, expected_sha256=expected_sha)
        _ensure_private_directory(backup_dir)
        settings = replace(
            loaded_settings,
            database_path=database.resolve(strict=True),
            database_must_exist=True,
            backups_dir=backup_dir.resolve(strict=True),
        )
        backup: Mapping[str, Any]
        current: Plan
        with _quiesced_writer_leases(loaded_settings.state_dir) as lease_evidence:
            storage = FridayStorage(settings)
            try:
                locked_plan = build_plan(
                    storage.conn,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    uploader_id=uploader_id,
                )
                if (
                    locked_plan.plan_sha256 != initial.plan_sha256
                    or locked_plan.candidate_count != expected_count
                ):
                    raise ContractError("plan drifted before the verified backup; refusing apply")
                try:
                    backup = storage.create_backup(
                        label=f"pre-alias-filename-repair-{initial.plan_sha256[:12]}"
                    )
                except Exception as exc:  # noqa: BLE001 - public failure is class-only
                    raise ContractError(f"verified backup failed: {type(exc).__name__}") from exc

                current = _apply_locked_plan(
                    storage,
                    initial,
                    expected_count=expected_count,
                    leases_held=lambda: all(
                        _lease_is_exactly_held(lease) for lease in lease_evidence["leases"]
                    ),
                )
            finally:
                storage.close(final=True)

        return current, {
            "applied_count": current.candidate_count,
            "applied_plan_sha256": current.plan_sha256,
            "backup_manifest_sha256": _sha256(Path(str(backup["manifest_path"])).read_bytes()),
            "backup_database_sha256": str(backup["sha256"]),
            "writer_quiescence_sha256": str(lease_evidence["protocols_sha256"]),
        }


def apply_plan_under_held_leases(
    database: Path,
    *,
    claim_manifest: Path,
    expected_count: int,
    expected_plan_sha256: str,
    backend_lease: ProcessLease,
    bridge_lease: ProcessLease,
    verified_backup_receipt: ExternalBackupReceipt,
) -> tuple[Plan, dict[str, Any]]:
    """Apply one exact claim while the release operator retains both writer leases."""

    expected_sha = _safe_hex64(expected_plan_sha256, label="expected plan SHA-256")
    if type(expected_count) is not int or expected_count <= 0:
        raise ContractError("expected count must be a positive integer")
    claim = _load_claim(claim_manifest)
    tenant_id = _safe_identity(str(claim.get("tenant_id") or ""), label="tenant id")
    owner_id = _safe_identity(str(claim.get("owner_id") or ""), label="owner id")
    uploader_id = _safe_identity(str(claim.get("uploader_id") or ""), label="uploader id")
    loaded_settings = load_settings()
    configured_database = loaded_settings.database_path.resolve(strict=True)
    if configured_database != database.resolve(strict=True):
        raise ContractError("database is not the exact configured backend database")
    expected_leases = (
        (
            backend_lease,
            loaded_settings.state_dir.resolve(strict=True) / "backend.lock",
            "friday.backend.v1",
        ),
        (
            bridge_lease,
            loaded_settings.state_dir.resolve(strict=True) / "telegram-inbox.sqlite3.lock",
            "friday.telegram-bridge.v1",
        ),
    )

    def leases_held() -> bool:
        return all(
            lease.path.resolve(strict=False) == expected_path
            and lease.protocol == expected_protocol
            and _lease_is_exactly_held(lease)
            for lease, expected_path, expected_protocol in expected_leases
        )

    if not leases_held():
        raise ContractError("release operator does not hold both exact writer leases")
    backup_evidence = _verify_external_backup(verified_backup_receipt, configured_database)
    settings = replace(
        loaded_settings,
        database_path=configured_database,
        database_must_exist=True,
    )
    storage = FridayStorage(settings)
    try:
        initial = build_plan(
            storage.conn,
            tenant_id=tenant_id,
            owner_id=owner_id,
            uploader_id=uploader_id,
        )
        _validate_claim(claim, initial, expected_count=expected_count, expected_sha256=expected_sha)
        current = _apply_locked_plan(
            storage,
            initial,
            expected_count=expected_count,
            leases_held=leases_held,
        )
    finally:
        storage.close(final=True)
    evidence = {
        "applied_count": current.candidate_count,
        "applied_plan_sha256": current.plan_sha256,
        "backup_manifest_sha256": backup_evidence["manifest_sha256"],
        "backup_database_sha256": backup_evidence["database_files_sha256"],
        "backup_inbox_sha256": backup_evidence["inbox_files_sha256"],
        "pre_apply_database_sha256": backup_evidence["live_database_sha256"],
        "writer_quiescence_sha256": _sha256(
            _canonical_json(sorted(expected_protocol for _lease, _path, expected_protocol in expected_leases))
        ),
    }
    return current, evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--uploader-id", required=True)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--claim-manifest", type=Path, default=None)
    parser.add_argument("--expect-count", type=int, default=None)
    parser.add_argument("--expect-plan-sha256", default=None)
    parser.add_argument("--backup-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if args.apply:
            if (
                args.claim_manifest is None
                or args.expect_count is None
                or not args.expect_plan_sha256
                or args.backup_dir is None
                or args.report is None
            ):
                raise ContractError(
                    "apply requires claim, expected count/digest, backup directory, and private report"
                )
            plan, evidence = apply_plan(
                args.database,
                tenant_id=args.tenant_id,
                owner_id=args.owner_id,
                uploader_id=args.uploader_id,
                claim_manifest=args.claim_manifest,
                expected_count=args.expect_count,
                expected_plan_sha256=args.expect_plan_sha256,
                backup_dir=args.backup_dir,
            )
            report = plan.public_summary(mode="apply")
            report["applied"] = True
            report["evidence"] = evidence
        else:
            conn = _connect(args.database, read_only=True)
            try:
                plan = build_plan(
                    conn,
                    tenant_id=args.tenant_id,
                    owner_id=args.owner_id,
                    uploader_id=args.uploader_id,
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
        error = {
            "schema": REPORT_SCHEMA,
            "mode": "error",
            "applied": False,
            "error": "ContractError",
            "message": str(exc),
        }
        sys.stdout.write(json.dumps(error, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
