"""Privacy-safe reads of stored upload bytes.

The database row is the authorization authority; ``stored_path`` is only a
location.  Callers must therefore acquire the current row and consume the file
under one write-blocking SQLite transaction.  Returning a path for later
streaming creates a quarantine race: the row can become private after the check
while a ``FileResponse`` still reads and sends the old path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from friday.storage._privacy import _not_private_raw_dependency

_READ_CHUNK_BYTES = 1024 * 1024
_MAX_STORED_PATH_CHARS = 16_384
_MAX_DOWNLOAD_FILENAME_CHARS = 255
_MIME_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")


class FileRecordUnavailable(Exception):
    """The current database state does not authorize this stored file."""


class AuthorizedFileReadError(Exception):
    """An authorized row exists, but its bytes cannot safely be consumed."""

    def __init__(self, filename: str, reason: str) -> None:
        super().__init__(reason)
        self.filename = filename
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AuthorizedFileBytes:
    raw_id: str
    filename: str
    mime_type: str
    content: bytes


def attachment_content_disposition(filename: str) -> str:
    """Build a bounded header without trusting stored control characters."""

    safe = _safe_filename(filename)
    ascii_name = "".join(char if 32 <= ord(char) < 127 else "_" for char in safe)
    ascii_name = ascii_name.replace('"', "_").replace("\\", "_") or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe, safe='')}"


def read_authorized_file(
    storage: Any,
    root: Path,
    raw_id: str,
    user_id: str,
    *,
    include_deleted: bool = False,
    max_bytes: int | None = None,
) -> AuthorizedFileBytes:
    """Revalidate and read one file at a single privacy linearization point.

    ``transaction()`` uses ``BEGIN IMMEDIATE``.  A concurrent quarantine writer
    either commits before this query (and the row is refused) or waits until all
    bytes have been consumed.  No filesystem path survives the transaction for
    a response object to read later.
    """

    with storage.transaction() as conn:
        return read_authorized_file_in_transaction(
            conn,
            root,
            raw_id,
            user_id,
            include_deleted=include_deleted,
            max_bytes=max_bytes,
        )


def read_authorized_file_in_transaction(
    conn: Any,
    root: Path,
    raw_id: str,
    user_id: str,
    *,
    include_deleted: bool = False,
    max_bytes: int | None = None,
) -> AuthorizedFileBytes:
    """Transaction-scoped implementation used to assemble an atomic archive."""

    deleted_clause = "" if include_deleted else " AND r.deleted_at IS NULL"
    row = conn.execute(
        f"""SELECT r.id, r.user_id, r.source_ref, r.metadata_json
              FROM raw_objects r
             WHERE r.id=? AND r.user_id=? AND r.content_type='file'
               {deleted_clause}
               AND {_not_private_raw_dependency("r")}""",  # nosec B608 - fixed SQL fragments
        (str(raw_id), str(user_id)),
    ).fetchone()
    if row is None:
        raise FileRecordUnavailable

    return _read_authorized_row(row, root, max_bytes=max_bytes)


def read_authorized_generated_file(
    storage: Any,
    root: Path,
    raw_id: str,
    tenant_id: str,
    person_id: str,
    *,
    max_bytes: int | None = None,
) -> AuthorizedFileBytes:
    """Read one generated output owned by the exact authenticated person.

    A shared archive intentionally gives several people the same ``tenant_id``.
    Generated outputs belong to the conversation participant, so tenant scope by
    itself is insufficient here; both durable ownership markers must match.
    """

    with storage.transaction() as conn:
        row = conn.execute(
            f"""SELECT r.id, r.user_id, r.source_ref, r.metadata_json, r.content_hash
                  FROM raw_objects r
                 WHERE r.id=? AND r.user_id=? AND r.content_type='generated_file'
                   AND r.deleted_at IS NULL
                   AND json_valid(r.metadata_json)
                   AND json_extract(r.metadata_json, '$.generated_artifact')=1
                   AND json_extract(r.metadata_json, '$.generated_for')=?
                   AND json_extract(r.metadata_json, '$.generated_tenant')=?
                   AND {_not_private_raw_dependency("r")}""",  # nosec B608 - fixed SQL fragments
            (str(raw_id), str(person_id), str(person_id), str(tenant_id)),
        ).fetchone()
        if row is None:
            raise FileRecordUnavailable
        metadata = _metadata_object(row["metadata_json"])
        expected_digest = str(metadata.get("sha256") or "")
        if not expected_digest or not hmac.compare_digest(str(row["content_hash"] or ""), expected_digest):
            raise FileRecordUnavailable
        stored = _read_authorized_row(row, root, max_bytes=max_bytes)
        expected_size = metadata.get("size_bytes")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size < 0
            or len(stored.content) != expected_size
        ):
            raise AuthorizedFileReadError(stored.filename, "файл не прошёл проверку размера")
        return stored


def _read_authorized_row(
    row: Any,
    root: Path,
    *,
    max_bytes: int | None,
) -> AuthorizedFileBytes:
    metadata = _metadata_object(row["metadata_json"])
    filename = _safe_filename(metadata.get("filename") or row["source_ref"] or "download")
    stored_path = metadata.get("stored_path")
    if not isinstance(stored_path, str) or not stored_path or len(stored_path) > _MAX_STORED_PATH_CHARS:
        raise AuthorizedFileReadError(filename, "файл не прочитался")
    expected_digest = metadata.get("sha256")
    if not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_digest):
        expected_digest = ""

    try:
        content = _read_regular_file(
            root,
            stored_path,
            max_bytes=max_bytes,
            expected_sha256=expected_digest.casefold(),
        )
    except _FileTooLarge as exc:
        raise AuthorizedFileReadError(filename, "не поместился по размеру") from exc
    except (OSError, ValueError) as exc:
        # Paths, errno strings and file contents never cross the public boundary.
        raise AuthorizedFileReadError(filename, "файл не прочитался") from exc

    mime_type = metadata.get("mime_type")
    if not isinstance(mime_type, str) or not _MIME_TYPE.fullmatch(mime_type):
        mime_type = "application/octet-stream"
    return AuthorizedFileBytes(
        raw_id=str(row["id"]),
        filename=filename,
        mime_type=mime_type,
        content=content,
    )


class _FileTooLarge(ValueError):
    pass


def _metadata_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or len(value) > 1_048_576:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_filename(value: Any) -> str:
    text = str(value or "download").replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(char for char in text if char >= " " and char != "\x7f").strip(" .")
    return text[:_MAX_DOWNLOAD_FILENAME_CHARS] or "download"


def _read_regular_file(
    root: Path,
    candidate: str,
    *,
    max_bytes: int | None,
    expected_sha256: str,
) -> bytes:
    """Open one immutable in-root file descriptor, verify it, then consume it."""

    base_root = root.resolve(strict=True)
    given = Path(candidate)
    resolved = (
        (base_root / given).resolve(strict=True) if not given.is_absolute() else given.resolve(strict=True)
    )
    if resolved == base_root or not resolved.is_relative_to(base_root):
        raise ValueError("stored file is outside its root")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("stored object is not a regular file")
        if max_bytes is not None and before.st_size > max(0, int(max_bytes)):
            raise _FileTooLarge

        # Validate the object actually opened, not merely the pathname checked
        # before ``open``.  This closes symlink/rename swaps between resolution
        # and descriptor acquisition on Linux, where Friday is deployed.
        fd_link = Path(f"/proc/self/fd/{descriptor}")
        if fd_link.exists():
            opened = Path(os.path.realpath(fd_link))
            if opened == base_root or not opened.is_relative_to(base_root):
                raise ValueError("opened file is outside its root")

        chunks: list[bytes] = []
        digest = hashlib.sha256() if expected_sha256 else None
        total = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max(0, int(max_bytes)):
                raise _FileTooLarge
            chunks.append(chunk)
            if digest is not None:
                digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError("stored file changed while being read")
        if digest is not None and not hmac.compare_digest(digest.hexdigest(), expected_sha256):
            raise ValueError("stored file digest mismatch")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


__all__ = [
    "AuthorizedFileBytes",
    "AuthorizedFileReadError",
    "FileRecordUnavailable",
    "attachment_content_disposition",
    "read_authorized_file",
    "read_authorized_file_in_transaction",
    "read_authorized_generated_file",
]
