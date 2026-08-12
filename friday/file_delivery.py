"""Privacy-safe reads of stored upload bytes.

The database row is the authorization authority; ``stored_path`` is only a
location.  Callers must therefore acquire the current row and consume the file
under one write-blocking SQLite transaction.  Returning a path for later
streaming creates a quarantine race: the row can become private after the check
while a ``FileResponse`` still reads and sends the old path.

Registration is three-state:

* ``legacy_unregistered`` — no modern disk-registration fields at all
* ``registered_valid`` — path/size/hash/content_hash agree and the bytes match
* ``registered_invalid`` — modern fields are present but incomplete or inconsistent

``registered_invalid`` must never be silently treated as legacy, and must never
authorize a read of the cached ``raw_content`` body as if it were verified disk
bytes.  That decision belongs to higher layers; this module only refuses the
disk path.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from friday.storage._privacy import _not_private_raw_dependency

_READ_CHUNK_BYTES = 1024 * 1024
_MAX_STORED_PATH_CHARS = 16_384
_MAX_DOWNLOAD_FILENAME_CHARS = 255
_MIME_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")

LEGACY_UNREGISTERED = "legacy_unregistered"
REGISTERED_VALID = "registered_valid"
REGISTERED_INVALID = "registered_invalid"

_MODERN_REGISTRATION_KEYS = frozenset({"stored_path", "sha256", "size_bytes"})


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


@dataclass(frozen=True, slots=True)
class FileRegistrationVerdict:
    """Closed classification of one Raw file registration.

    ``reason`` is a stable machine code without paths, names, or content.
    """

    state: str
    reason: str


def attachment_content_disposition(filename: str) -> str:
    """Build a bounded header without trusting stored control characters."""

    safe = _safe_filename(filename)
    ascii_name = "".join(char if 32 <= ord(char) < 127 else "_" for char in safe)
    ascii_name = ascii_name.replace('"', "_").replace("\\", "_") or "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(safe, safe='')}"


def classify_file_registration(
    metadata: Mapping[str, Any] | None,
    *,
    content_hash: str | None,
) -> FileRegistrationVerdict:
    """Classify registration fields without touching the filesystem.

    Disk agreement is a separate step (``verify_registered_file_bytes`` /
    ``read_authorized_file``).  A row that only has modern fields is already
    either valid-shape or invalid — never legacy.
    """

    meta = metadata if isinstance(metadata, Mapping) else {}
    modern_present = any(key in meta for key in _MODERN_REGISTRATION_KEYS)
    if not modern_present:
        return FileRegistrationVerdict(LEGACY_UNREGISTERED, "no_disk_registration_fields")

    stored_path = meta.get("stored_path")
    if not isinstance(stored_path, str) or not stored_path or len(stored_path) > _MAX_STORED_PATH_CHARS:
        return FileRegistrationVerdict(REGISTERED_INVALID, "stored_path_missing_or_unbounded")
    if "\x00" in stored_path or stored_path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", stored_path):
        # Absolute host paths are historical machine coupling.  They are not a
        # modern relative registration even when they still happen to resolve.
        return FileRegistrationVerdict(REGISTERED_INVALID, "stored_path_not_relative")
    if "\\" in stored_path or ".." in Path(stored_path).parts:
        return FileRegistrationVerdict(REGISTERED_INVALID, "stored_path_unsafe")

    digest = meta.get("sha256")
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        return FileRegistrationVerdict(REGISTERED_INVALID, "sha256_missing_or_malformed")
    digest = digest.casefold()

    raw_hash = str(content_hash or "").strip().casefold()
    if not raw_hash or not _HEX64.fullmatch(raw_hash):
        return FileRegistrationVerdict(REGISTERED_INVALID, "content_hash_missing_or_malformed")
    if not hmac.compare_digest(raw_hash, digest):
        return FileRegistrationVerdict(REGISTERED_INVALID, "content_hash_sha256_mismatch")

    if "size_bytes" not in meta:
        return FileRegistrationVerdict(REGISTERED_INVALID, "size_bytes_missing")
    size_bytes = meta.get("size_bytes")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        return FileRegistrationVerdict(REGISTERED_INVALID, "size_bytes_invalid")

    return FileRegistrationVerdict(REGISTERED_VALID, "registration_fields_consistent")


def verify_registered_file_bytes(
    root: Path,
    metadata: Mapping[str, Any] | None,
    *,
    content_hash: str | None,
    max_bytes: int | None = None,
) -> FileRegistrationVerdict:
    """Classify and prove disk bytes for one registration without authorization.

    Callers that already authorized a Raw row use ``read_authorized_file``.
    This helper is for audit and ingestion repair checks: it never prints
    paths or content, only a closed verdict.
    """

    verdict = classify_file_registration(metadata, content_hash=content_hash)
    if verdict.state != REGISTERED_VALID:
        return verdict
    meta = metadata if isinstance(metadata, Mapping) else {}
    digest = str(meta.get("sha256") or "").casefold()
    stored_path = str(meta.get("stored_path") or "")
    try:
        content = _read_regular_file(
            root,
            stored_path,
            max_bytes=max_bytes,
            expected_sha256=digest,
        )
    except _FileTooLarge:
        return FileRegistrationVerdict(REGISTERED_INVALID, "file_too_large")
    except (OSError, ValueError):
        return FileRegistrationVerdict(REGISTERED_INVALID, "disk_bytes_unreadable_or_mismatched")
    size_bytes = meta.get("size_bytes")
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or size_bytes != len(content)
    ):
        return FileRegistrationVerdict(REGISTERED_INVALID, "size_bytes_mismatch")
    return FileRegistrationVerdict(REGISTERED_VALID, "disk_bytes_verified")


def read_authorized_file(
    storage: Any,
    root: Path,
    raw_id: str,
    user_id: str,
    *,
    person_id: str | None = None,
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
            person_id=person_id,
            include_deleted=include_deleted,
            max_bytes=max_bytes,
        )


def read_authorized_file_in_transaction(
    conn: Any,
    root: Path,
    raw_id: str,
    user_id: str,
    *,
    person_id: str | None = None,
    include_deleted: bool = False,
    max_bytes: int | None = None,
) -> AuthorizedFileBytes:
    """Transaction-scoped implementation used to assemble an atomic archive."""

    deleted_clause = "" if include_deleted else " AND r.deleted_at IS NULL"
    person_clause = (
        " AND json_valid(r.metadata_json) AND COALESCE(json_extract(r.metadata_json, '$.uploaded_by'), '')=?"
        if person_id is not None
        else ""
    )
    parameters: tuple[str, ...] = (
        (str(raw_id), str(user_id), str(person_id)) if person_id is not None else (str(raw_id), str(user_id))
    )
    row = conn.execute(
        f"""SELECT r.id, r.user_id, r.source_ref, r.metadata_json, r.content_hash
              FROM raw_objects r
             WHERE r.id=? AND r.user_id=? AND r.content_type='file'
               {deleted_clause}{person_clause}
               AND {_not_private_raw_dependency("r")}""",  # nosec B608 - fixed SQL fragments
        parameters,
    ).fetchone()
    if row is None:
        raise FileRecordUnavailable

    stored = _read_authorized_row(row, root, max_bytes=max_bytes)

    # Same IMMEDIATE writer lock already serializes quarantine, but re-check the
    # authorization predicate after the bytes leave the descriptor so a row that
    # was mutated mid-read (or a corrupted SELECT projection) cannot ship.
    still = conn.execute(
        f"""SELECT r.id FROM raw_objects r
             WHERE r.id=? AND r.user_id=? AND r.content_type='file'
               {deleted_clause}{person_clause}
               AND {_not_private_raw_dependency("r")}""",  # nosec B608 - fixed SQL fragments
        parameters,
    ).fetchone()
    if still is None:
        raise FileRecordUnavailable
    return stored


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
    try:
        content_hash = str(row["content_hash"] or "")
    except (KeyError, IndexError, TypeError):
        content_hash = ""
    verdict = classify_file_registration(metadata, content_hash=content_hash)
    if verdict.state == LEGACY_UNREGISTERED:
        # Legacy rows have no durable disk registration.  They must not be
        # "repaired" into a verified disk read by inventing a path here.
        raise AuthorizedFileReadError(filename, "файл не зарегистрирован на диске")
    if verdict.state == REGISTERED_INVALID:
        # Fail closed.  Do not fall through to a path-only open, and do not
        # expose which field failed — higher layers may use classify_* for that.
        raise AuthorizedFileReadError(filename, "регистрация файла повреждена")

    stored_path = str(metadata.get("stored_path") or "")
    expected_digest = str(metadata.get("sha256") or "").casefold()

    try:
        content = _read_regular_file(
            root,
            stored_path,
            max_bytes=max_bytes,
            expected_sha256=expected_digest,
        )
    except _FileTooLarge as exc:
        raise AuthorizedFileReadError(filename, "не поместился по размеру") from exc
    except (OSError, ValueError) as exc:
        # Paths, errno strings and file contents never cross the public boundary.
        raise AuthorizedFileReadError(filename, "файл не прочитался") from exc

    size_bytes = metadata.get("size_bytes")
    # Modern valid registration always carries a non-negative integer size; the
    # classifier already rejected missing/invalid values before this open.
    if (
        isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or size_bytes != len(content)
    ):
        raise AuthorizedFileReadError(filename, "файл не прошёл проверку размера")

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

    if not expected_sha256 or not _HEX64.fullmatch(expected_sha256):
        raise ValueError("expected digest is required")

    base_root = root.resolve(strict=True)
    given = Path(candidate)
    # Relative registration only.  Absolute candidates are rejected even when
    # they resolve under the root — modern writes always store a relative path.
    if given.is_absolute() or ".." in given.parts:
        raise ValueError("stored file path is not a safe relative registration")
    resolved = (base_root / given).resolve(strict=True)
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
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            if max_bytes is not None and total > max(0, int(max_bytes)):
                raise _FileTooLarge
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError("stored file changed while being read")
        if not hmac.compare_digest(digest.hexdigest(), expected_sha256.casefold()):
            raise ValueError("stored file digest mismatch")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


__all__ = [
    "AuthorizedFileBytes",
    "AuthorizedFileReadError",
    "FileRecordUnavailable",
    "FileRegistrationVerdict",
    "LEGACY_UNREGISTERED",
    "REGISTERED_INVALID",
    "REGISTERED_VALID",
    "attachment_content_disposition",
    "classify_file_registration",
    "read_authorized_file",
    "read_authorized_file_in_transaction",
    "read_authorized_generated_file",
    "verify_registered_file_bytes",
]
