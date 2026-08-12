#!/usr/bin/env python
"""Read-only audit of SQLite file registration against the files root.

Default mode never mutates the database or the filesystem.  Output is only
counts and opaque Raw-id tags — no filenames, absolute paths, bodies, or
content digests.  There is no apply/fix mode.

States counted per Raw row:

* valid — modern relative registration, content_hash/sha256/size agree, disk OK
* legacy — no modern disk-registration fields
* invalid — modern fields present but incomplete or inconsistent (metadata only)
* missing — registered path does not resolve to a regular in-root file
* hash_mismatch — file opens but SHA-256 disagrees with registration
* unsafe_path — absolute, traversal, symlink, or outside files root
* alias_conflict — alias points at absent/foreign/deleted/private/ignored/non-file Raw
* provenance_unknown — file row with no explicit uploaded_by (not guessed)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from friday.file_delivery import (  # noqa: E402
    LEGACY_UNREGISTERED,
    REGISTERED_INVALID,
    REGISTERED_VALID,
    classify_file_registration,
    verify_registered_file_bytes,
)
from friday.storage import SCHEMA_VERSION  # noqa: E402
from friday.storage._privacy import (  # noqa: E402
    _exact_uploader_raw_dependency,
    _not_audio_document,
    _not_private_raw_dependency,
)

REPORT_SCHEMA = "friday.file-registry-audit.v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ContractError(RuntimeError):
    """Operator or database contract was not satisfied."""


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


def _tag(raw_id: str) -> str:
    return _sha256(raw_id.encode("utf-8"))[:16]


def _connect(database: Path) -> sqlite3.Connection:
    path = database.resolve(strict=True)
    if database.is_symlink() or not path.is_file():
        raise ContractError("database must be a regular non-symlink file")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _required_tables(conn: sqlite3.Connection) -> None:
    required = {"raw_objects", "users", "schema_meta"}
    present = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)",
            tuple(sorted(required)),
        )
    }
    if present != required:
        raise ContractError("database does not have the required Friday tables")
    schema = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if schema is None or str(schema[0]) != str(SCHEMA_VERSION):
        raise ContractError("database must already be at the current Friday schema")


def _metadata_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or len(value) > 1_048_576:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _disk_detail(
    files_root: Path,
    metadata: dict[str, Any],
    *,
    content_hash: str,
) -> str:
    """Return a closed disk-level state for a metadata-valid registration."""

    verdict = verify_registered_file_bytes(
        files_root,
        metadata,
        content_hash=content_hash,
    )
    if verdict.state == REGISTERED_VALID:
        return "valid"
    reason = verdict.reason
    if reason in {"stored_path_not_relative", "stored_path_unsafe", "stored_path_missing_or_unbounded"}:
        return "unsafe_path"
    if reason == "disk_bytes_unreadable_or_mismatched":
        # Distinguish missing path from digest mismatch without printing paths.
        stored = str(metadata.get("stored_path") or "")
        try:
            base = files_root.resolve(strict=True)
            candidate = (base / stored).resolve(strict=False)
            if not candidate.is_relative_to(base):
                return "unsafe_path"
            if candidate.is_symlink():
                return "unsafe_path"
            if not candidate.is_file():
                return "missing"
            # File exists: classify as hash mismatch vs other open failure.
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(candidate, flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    return "unsafe_path"
                digest = hashlib.sha256()
                while True:
                    chunk = os.read(fd, 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                expected = str(metadata.get("sha256") or "").casefold()
                if expected and not hmac.compare_digest(digest.hexdigest(), expected):
                    return "hash_mismatch"
                return "invalid"
            finally:
                os.close(fd)
        except (OSError, ValueError):
            return "missing"
    return "invalid"


def audit_registry(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    files_root: Path,
    uploader: str | None = None,
) -> dict[str, Any]:
    tenant = _safe_identity(tenant_id, label="tenant id")
    exact_uploader: str | None = None
    if uploader is not None:
        exact_uploader = _safe_identity(uploader, label="uploader id")
    _required_tables(conn)
    root = files_root.resolve(strict=True)
    if files_root.is_symlink() or not root.is_dir():
        raise ContractError("files root must be a regular directory")

    counts: Counter[str] = Counter()
    sample_tags: dict[str, list[str]] = {
        "valid": [],
        "legacy": [],
        "invalid": [],
        "missing": [],
        "hash_mismatch": [],
        "unsafe_path": [],
        "alias_conflict": [],
        "provenance_unknown": [],
    }

    def _note(state: str, raw_id: str) -> None:
        counts[state] += 1
        bucket = sample_tags.setdefault(state, [])
        if len(bucket) < 8:
            bucket.append(_tag(raw_id))

    rows = conn.execute(
        """SELECT id, content_hash, metadata_json, deleted_at
             FROM raw_objects
            WHERE user_id=? AND content_type='file'
            ORDER BY id""",
        (tenant,),
    ).fetchall()

    for row in rows:
        raw_id = str(row["id"] or "")
        if not raw_id:
            continue
        if row["deleted_at"] is not None:
            counts["deleted_skipped"] += 1
            continue
        metadata = _metadata_object(row["metadata_json"])
        if metadata is None:
            _note("invalid", raw_id)
            continue
        if exact_uploader is not None and metadata.get("uploaded_by") != exact_uploader:
            counts["uploader_filtered"] += 1
            continue
        if "uploaded_by" not in metadata:
            _note("provenance_unknown", raw_id)

        content_hash = str(row["content_hash"] or "")
        classification = classify_file_registration(metadata, content_hash=content_hash)
        if classification.state == LEGACY_UNREGISTERED:
            _note("legacy", raw_id)
            continue
        if classification.state == REGISTERED_INVALID:
            if classification.reason in {
                "stored_path_not_relative",
                "stored_path_unsafe",
                "stored_path_missing_or_unbounded",
            }:
                _note("unsafe_path", raw_id)
            else:
                _note("invalid", raw_id)
            continue
        # Metadata-valid: prove disk.
        disk_state = _disk_detail(root, metadata, content_hash=content_hash)
        _note(disk_state, raw_id)

    # Alias conflicts: same authorization surface as bind/resolve (tenant,
    # uploader, file kind, privacy, ignored, soft-delete) — not a second semantic.
    alias_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_source_aliases'"
    ).fetchone()
    if alias_table is not None:
        alias_sql = """
            SELECT a.source_ref, a.uploaded_by, a.raw_object_id
              FROM file_source_aliases a
             WHERE a.user_id=?
        """
        alias_params: list[Any] = [tenant]
        if exact_uploader is not None:
            alias_sql += " AND a.uploaded_by=?"
            alias_params.append(exact_uploader)
        authorized_alias_sql = f"""
            SELECT 1 FROM raw_objects r
             WHERE r.id=? AND r.user_id=? AND r.content_type='file'
               AND r.source='upload'
               AND r.deleted_at IS NULL
               AND {_not_audio_document("r")}
               AND {_not_private_raw_dependency("r")}
               AND NOT EXISTS (
                     SELECT 1 FROM inbox i
                      WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                        AND i.status='ignored'
                   )
               AND {_exact_uploader_raw_dependency("r")}
             LIMIT 1
        """  # nosec B608 - fixed privacy/uploader predicates only
        for alias in conn.execute(alias_sql, tuple(alias_params)):
            raw_id = str(alias["raw_object_id"] or "")
            uploader = str(alias["uploaded_by"] or "")
            ok = None
            if raw_id and uploader:
                ok = conn.execute(
                    authorized_alias_sql,
                    (raw_id, tenant, uploader),
                ).fetchone()
            if ok is None:
                _note("alias_conflict", raw_id or f"alias:{_tag(str(alias['source_ref'] or ''))}")

    report = {
        "schema": REPORT_SCHEMA,
        "mode": "read_only",
        "tenant_tag": _sha256(tenant.encode("utf-8"))[:16],
        "uploader_tag": (
            _sha256(exact_uploader.encode("utf-8"))[:16] if exact_uploader is not None else None
        ),
        "files_root_present": True,
        "counts": {
            "valid": int(counts.get("valid", 0)),
            "legacy": int(counts.get("legacy", 0)),
            "invalid": int(counts.get("invalid", 0)),
            "missing": int(counts.get("missing", 0)),
            "hash_mismatch": int(counts.get("hash_mismatch", 0)),
            "unsafe_path": int(counts.get("unsafe_path", 0)),
            "alias_conflict": int(counts.get("alias_conflict", 0)),
            "provenance_unknown": int(counts.get("provenance_unknown", 0)),
            "deleted_skipped": int(counts.get("deleted_skipped", 0)),
            "uploader_filtered": int(counts.get("uploader_filtered", 0)),
            "rows_scanned": int(len(rows)),
        },
        "sample_raw_tags": {key: sample_tags.get(key, []) for key in sample_tags},
    }
    report["report_sha256"] = _sha256(_canonical_json(report))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="Path to Friday SQLite file")
    parser.add_argument("--files-root", required=True, type=Path, help="Configured files directory")
    parser.add_argument("--tenant", required=True, help="Exact tenant user_id")
    parser.add_argument("--uploader", default=None, help="Optional exact uploaded_by filter")
    args = parser.parse_args(argv)

    try:
        conn = _connect(args.database)
        try:
            report = audit_registry(
                conn,
                tenant_id=args.tenant,
                files_root=args.files_root,
                uploader=args.uploader,
            )
        finally:
            conn.close()
    except ContractError as exc:
        payload = {
            "schema": REPORT_SCHEMA,
            "mode": "read_only",
            "error": "contract",
            "message": str(exc),
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
        return 2

    sys.stdout.write(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
