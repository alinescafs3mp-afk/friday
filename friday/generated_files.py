"""Durable, person-owned artifacts produced by ``make_file``.

The execution tool returns an inline base64 attachment so an existing client can
download it immediately.  Inline JSON is not a durable file store, though: a page
refresh only has conversation messages, and Telegram may retry delivery after the
backend response has already been cached.  This module freezes the exact bytes in
the ordinary private file root, records an opaque Raw Object handle, and attaches a
content-free descriptor to the assistant message that produced it.

Generated artifacts deliberately use their own ``content_type``.  They are outputs,
not uploads, so document selection and "which files did I send?" must never count
them as source material.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from friday.private_fs import ensure_private_directory, restrict_private_file
from friday.raw_metadata import bounded_raw_file_metadata
from friday.storage.models import RawObject, new_id

_PUBLIC_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}")
_MIME_TYPE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_GENERATED_FILES_PER_RESPONSE = 16
_MAX_FILENAME_CHARS = 180
_MESSAGE_METADATA_MAX_BYTES = 1024 * 1024
_PROTECTED_RESPONSE_FIELDS = frozenset({"id", "raw_object_id", "download_url", "sha256", "size_bytes"})


class GeneratedFilePersistenceError(RuntimeError):
    """A response claimed to contain a file which could not be frozen safely."""


class GeneratedFilesPersistenceRollbackGuard:
    """Track new blobs until their enclosing database commit is durable.

    ``persist_generated_response_files`` normally owns its outer transaction.
    Some publication paths deliberately call it inside a larger transaction so
    the assistant message, Raw handles and accepted outcome commit together.
    This process-private guard lets that outer boundary compensate newly-created
    blobs while it still owns the SQLite writer lock.
    """

    __slots__ = ("_created_paths", "_files_root", "_inserted_raw_ids")

    def __init__(self, files_root: Path) -> None:
        self._files_root = Path(files_root).resolve()
        self._created_paths: set[str] = set()
        self._inserted_raw_ids: set[str] = set()

    def _sets_for(self, files_root: Path) -> tuple[set[str], set[str]]:
        if Path(files_root).resolve() != self._files_root:
            raise GeneratedFilePersistenceError("rollback guard file root mismatch")
        return self._created_paths, self._inserted_raw_ids

    def after_commit(self) -> None:
        """Disarm the guard after the Raw handles are durable."""

        self._created_paths.clear()
        self._inserted_raw_ids.clear()

    def after_rollback(self, conn: Any) -> None:
        """Remove only blobs which remain unreferenced after the rollback."""

        _discard_unreferenced_paths(
            conn,
            self._files_root,
            self._created_paths,
            # The rows created by this unit have already rolled back.  Query
            # every remaining row so a pre-existing shared blob is retained.
            inserted_raw_ids=set(),
        )
        self._created_paths.clear()
        self._inserted_raw_ids.clear()


@contextmanager
def generated_files_publication_transaction(
    storage: Any,
    rollback_guard: GeneratedFilesPersistenceRollbackGuard,
) -> Iterator[Any]:
    """Keep the writer lock through outer rollback and blob compensation."""

    if type(rollback_guard) is not GeneratedFilesPersistenceRollbackGuard:
        raise GeneratedFilePersistenceError("generated-file rollback guard is invalid")
    connection = None
    # ``FridayStorage.transaction`` already takes this reentrant lock. Taking
    # one outer hold preserves its normal nested-savepoint behavior while
    # preventing another writer from claiming a just-rolled-back blob before
    # compensation has checked all remaining Raw references.
    with storage._write_lock:  # noqa: SLF001 - atomic FS/SQLite publication boundary
        try:
            with storage.transaction() as connection:
                yield connection
        except BaseException:
            if connection is not None:
                rollback_guard.after_rollback(connection)
            raise
        else:
            rollback_guard.after_commit()


@dataclass(frozen=True, slots=True)
class GeneratedFilesPersistenceAttestation:
    """Process-private identity of one already-committed generated-file batch."""

    message_id: str
    descriptors: tuple[tuple[str, str, str, str, int], ...]


def generated_files_persistence_attestation(
    response: Mapping[str, Any],
) -> GeneratedFilesPersistenceAttestation | None:
    """Build a typed attestation from exact persisted response descriptors."""

    message_id = str(response.get("message_id") or "").strip()
    values = response.get("files")
    if not _PUBLIC_MESSAGE_ID.fullmatch(message_id) or not isinstance(values, list) or not values:
        return None
    descriptors: list[tuple[str, str, str, str, int]] = []
    for value in values:
        if not isinstance(value, Mapping):
            return None
        raw_id = value.get("id")
        filename = value.get("filename")
        mime_type = value.get("mime_type")
        digest = value.get("sha256")
        size = value.get("size_bytes")
        encoded = value.get("content_base64")
        if (
            not isinstance(raw_id, str)
            or re.fullmatch(r"raw_[0-9a-f]{16}", raw_id) is None
            or not isinstance(filename, str)
            or filename != _safe_filename(filename)
            or not isinstance(mime_type, str)
            or mime_type != _safe_mime_type(mime_type)
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(encoded, str)
        ):
            return None
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, binascii.Error):
            return None
        if len(payload) != size or not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), digest):
            return None
        descriptors.append((raw_id, filename, mime_type, digest, size))
    return GeneratedFilesPersistenceAttestation(message_id, tuple(descriptors))


def validate_generated_files_persistence_attestation(
    storage: Any,
    response: Mapping[str, Any],
    attestation: object,
    *,
    tenant_id: str,
    person_id: str,
) -> bool:
    """Revalidate a process-private skip marker against Raw and message state."""

    if type(attestation) is not GeneratedFilesPersistenceAttestation:
        return False
    expected = generated_files_persistence_attestation(response)
    if expected is None or expected != attestation:
        return False
    with storage.transaction() as conn:
        row = conn.execute(
            """SELECT metadata_json FROM messages
                 WHERE id=? AND user_id IN (?, ?) AND role='assistant'""",
            (attestation.message_id, str(person_id), str(tenant_id)),
        ).fetchone()
        if row is None:
            return False
        raw_metadata = row["metadata_json"]
        if (
            not isinstance(raw_metadata, str)
            or len(raw_metadata.encode("utf-8")) > _MESSAGE_METADATA_MAX_BYTES
        ):
            return False
        try:
            metadata = json.loads(raw_metadata or "{}")
        except (TypeError, ValueError, RecursionError):
            return False
        durable_values = metadata.get("generated_files") if isinstance(metadata, Mapping) else None
        if not isinstance(durable_values, list) or len(durable_values) != len(attestation.descriptors):
            return False
        for durable, identity in zip(durable_values, attestation.descriptors, strict=True):
            raw_id, filename, mime_type, digest, size = identity
            descriptor = generated_file_descriptor(
                storage,
                raw_id,
                tenant_id=str(tenant_id),
                person_id=str(person_id),
            )
            if (
                descriptor is None
                or not isinstance(durable, Mapping)
                or descriptor != dict(durable)
                or descriptor.get("filename") != filename
                or descriptor.get("mime_type") != mime_type
                or descriptor.get("sha256") != digest
                or descriptor.get("size_bytes") != size
            ):
                return False
    return True


def persist_generated_response_files(
    storage: Any,
    files_root: Path,
    response: Mapping[str, Any],
    *,
    tenant_id: str,
    person_id: str,
    max_bytes: int,
    rollback_guard: GeneratedFilesPersistenceRollbackGuard | None = None,
) -> dict[str, Any]:
    """Freeze inline response files and return the backwards-compatible response.

    The returned items still contain ``content_base64``.  In addition they carry
    an opaque id, exact digest/size and an authenticated relative download URL.
    Message history stores only the descriptor, never another copy of the bytes.
    """

    public = dict(response)
    value = response.get("files")
    if not isinstance(value, list):
        return public

    items = [dict(item) for item in value if isinstance(item, Mapping)]
    if not items:
        public["files"] = []
        return public
    if len(items) > _MAX_GENERATED_FILES_PER_RESPONSE:
        raise GeneratedFilePersistenceError("too many generated files in one response")

    message_id = str(response.get("message_id") or "").strip()
    # Real AgentRuntime replies always name the stored assistant message.  Test
    # doubles and old cached responses may not; keep their inline compatibility,
    # but never bless caller-shaped handles as durable resources.
    if not _PUBLIC_MESSAGE_ID.fullmatch(message_id):
        public["files"] = [_without_claimed_handle(item) for item in items]
        return public

    # Validate and account for the whole batch before writing even its first
    # byte.  A malformed or cumulatively oversized second item must not leave
    # the first one as an invisible Raw Object/blob.
    byte_limit = max(0, int(max_bytes))
    batch_bytes = 0
    for item in items:
        payload, _filename, _mime_type, _digest = _validated_item(item, max_bytes=byte_limit)
        batch_bytes += len(payload)
        if batch_bytes > byte_limit:
            raise GeneratedFilePersistenceError(
                "generated attachment batch exceeds the configured file limit"
            )

    persisted_items: list[dict[str, Any]] = []
    history_descriptors: list[dict[str, Any]] = []
    if rollback_guard is None:
        created_paths: set[str] = set()
        inserted_raw_ids: set[str] = set()
    elif type(rollback_guard) is GeneratedFilesPersistenceRollbackGuard:
        created_paths, inserted_raw_ids = rollback_guard._sets_for(Path(files_root))
    else:
        raise GeneratedFilePersistenceError("generated-file rollback guard is invalid")
    # ``store_raw_object`` and descriptor attachment use nested managed
    # transactions, so this outer unit rolls every Raw row back together.
    with storage.transaction() as conn:
        try:
            for index, item in enumerate(items):
                persisted, descriptor = _persist_one(
                    storage,
                    Path(files_root),
                    item,
                    tenant_id=str(tenant_id),
                    person_id=str(person_id),
                    message_id=message_id,
                    index=index,
                    max_bytes=byte_limit,
                    created_paths=created_paths,
                    inserted_raw_ids=inserted_raw_ids,
                )
                persisted_items.append(persisted)
                history_descriptors.append(descriptor)

            if not _attach_descriptors_to_message(
                storage,
                message_id=message_id,
                tenant_id=str(tenant_id),
                person_id=str(person_id),
                descriptors=history_descriptors,
            ):
                raise GeneratedFilePersistenceError("assistant message disappeared before artifact commit")
        except BaseException:
            # Compensate while the same SQLite writer lock is still held.  A
            # post-rollback cleanup can race another backend: it may observe no
            # committed row and unlink a blob just before that backend commits
            # its handle.  Existing rows are retained; rows inserted by this
            # unit are excluded because the surrounding transaction will roll
            # them back immediately after cleanup.
            _discard_unreferenced_paths(
                conn,
                Path(files_root),
                created_paths,
                inserted_raw_ids=inserted_raw_ids,
            )
            raise

    public["files"] = persisted_items
    return public


def generated_file_descriptor(
    storage: Any,
    raw_id: Any,
    *,
    tenant_id: str,
    person_id: str,
) -> dict[str, Any] | None:
    """Re-authorize one durable descriptor without reading or exposing its bytes."""

    if not isinstance(raw_id, str) or not re.fullmatch(r"raw_[0-9a-f]{16}", raw_id):
        return None
    try:
        row = storage.get_raw_object(raw_id, str(person_id))
    except Exception:  # noqa: BLE001 - HTTP projection must fail closed
        return None
    if not isinstance(row, Mapping):
        return None
    if row.get("deleted_at") is not None:
        return None
    if row.get("content_type") != "generated_file" or row.get("source") != "generated":
        return None
    metadata = bounded_raw_file_metadata(row.get("metadata_json"))
    if metadata.get("generated_artifact") is not True:
        return None
    if metadata.get("generated_for") != str(person_id):
        return None
    if metadata.get("generated_tenant") != str(tenant_id):
        return None
    descriptor = _descriptor_from_metadata(raw_id, metadata)
    if descriptor is None:
        return None
    if not hmac.compare_digest(str(row.get("content_hash") or ""), descriptor["sha256"]):
        return None
    return descriptor


def _persist_one(
    storage: Any,
    files_root: Path,
    item: dict[str, Any],
    *,
    tenant_id: str,
    person_id: str,
    message_id: str,
    index: int,
    max_bytes: int,
    created_paths: set[str],
    inserted_raw_ids: set[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload, filename, mime_type, digest = _validated_item(item, max_bytes=max_bytes)
    stored_path = _store_content_addressed(
        files_root,
        person_id=person_id,
        digest=digest,
        content=payload,
        created_paths=created_paths,
    )
    source_ref = f"generated:{person_id}:{message_id}:{index}"
    candidate_id = new_id("raw")
    # Register the candidate before SQLite is entered.  If cancellation lands
    # immediately after the INSERT, rollback cleanup must still be able to
    # ignore that soon-to-be-rolled-back row when deciding whether the new blob
    # has another durable reference.
    inserted_raw_ids.add(candidate_id)
    raw = storage.store_raw_object(
        RawObject(
            id=candidate_id,
            user_id=person_id,
            source="generated",
            source_ref=source_ref,
            raw_content="",
            content_type="generated_file",
            content_hash=digest,
            metadata_json={
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": len(payload),
                "sha256": digest,
                "stored_path": stored_path,
                "generated_artifact": True,
                "generated_for": person_id,
                "generated_tenant": tenant_id,
                "generated_from_message_id": message_id,
            },
        )
    )
    if raw.id != candidate_id:
        inserted_raw_ids.discard(candidate_id)
    metadata = bounded_raw_file_metadata(raw.metadata_json)
    descriptor = _descriptor_from_metadata(raw.id, metadata)
    if (
        descriptor is None
        or raw.user_id != person_id
        or raw.content_type != "generated_file"
        or raw.source != "generated"
        or raw.source_ref != source_ref
        or metadata.get("generated_for") != person_id
        or not hmac.compare_digest(raw.content_hash, digest)
    ):
        raise GeneratedFilePersistenceError("generated source reference is bound to another artifact")

    public_item = _without_claimed_handle(item)
    public_item.update(descriptor)
    # Canonical base64 makes the advertised digest refer to the exact inline
    # bytes even if the renderer supplied a non-canonical but valid spelling.
    public_item["content_base64"] = base64.b64encode(payload).decode("ascii")
    public_item["kind"] = "document"
    return public_item, descriptor


def _validated_item(
    item: Mapping[str, Any],
    *,
    max_bytes: int,
) -> tuple[bytes, str, str, str]:
    encoded = item.get("content_base64")
    if not isinstance(encoded, str) or not encoded:
        raise GeneratedFilePersistenceError("generated attachment has no bytes")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise GeneratedFilePersistenceError("generated attachment is not valid base64") from exc
    if len(payload) > max_bytes:
        raise GeneratedFilePersistenceError("generated attachment exceeds the configured file limit")
    filename = _safe_filename(item.get("filename"))
    mime_type = _safe_mime_type(item.get("mime_type"))
    return payload, filename, mime_type, hashlib.sha256(payload).hexdigest()


def _descriptor_from_metadata(raw_id: str, metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    filename = metadata.get("filename")
    mime_type = metadata.get("mime_type")
    digest = metadata.get("sha256")
    size = metadata.get("size_bytes")
    if not isinstance(filename, str) or filename != _safe_filename(filename):
        return None
    if not isinstance(mime_type, str) or mime_type != _safe_mime_type(mime_type):
        return None
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        return None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        return None
    return {
        "id": raw_id,
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": size,
        "sha256": digest,
        "download_url": f"/api/files/{raw_id}",
    }


def _attach_descriptors_to_message(
    storage: Any,
    *,
    message_id: str,
    tenant_id: str,
    person_id: str,
    descriptors: list[dict[str, Any]],
) -> bool:
    """Merge artifact handles into the exact assistant message in one write lock."""

    with storage.transaction() as conn:
        row = conn.execute(
            """SELECT metadata_json FROM messages
                 WHERE id=? AND user_id IN (?, ?) AND role='assistant'""",
            (message_id, person_id, tenant_id),
        ).fetchone()
        if row is None:
            return False
        raw_metadata = row["metadata_json"]
        if (
            not isinstance(raw_metadata, str)
            or len(raw_metadata.encode("utf-8")) > _MESSAGE_METADATA_MAX_BYTES
        ):
            raise GeneratedFilePersistenceError("assistant message metadata is not bounded")
        try:
            metadata = json.loads(raw_metadata or "{}")
        except (TypeError, ValueError, RecursionError) as exc:
            raise GeneratedFilePersistenceError("assistant message metadata is invalid") from exc
        if not isinstance(metadata, dict):
            raise GeneratedFilePersistenceError("assistant message metadata is not an object")
        metadata["generated_files"] = descriptors
        encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MESSAGE_METADATA_MAX_BYTES:
            raise GeneratedFilePersistenceError("assistant message metadata exceeds its limit")
        cursor = conn.execute(
            """UPDATE messages SET metadata_json=?
                 WHERE id=? AND user_id IN (?, ?) AND role='assistant'""",
            (encoded, message_id, person_id, tenant_id),
        )
        return cursor.rowcount == 1


def _store_content_addressed(
    root: Path,
    *,
    person_id: str,
    digest: str,
    content: bytes,
    created_paths: set[str],
) -> str:
    root = ensure_private_directory(Path(root))
    person_dir = ensure_private_directory(root / _safe_component(person_id))
    target_dir = ensure_private_directory(person_dir / "generated" / digest[:2])
    target = target_dir / f"{digest}.blob"
    if target.is_symlink():
        raise GeneratedFilePersistenceError("generated artifact target is a symlink")
    if target.is_file():
        if not hmac.compare_digest(_file_sha256(target), digest):
            raise GeneratedFilePersistenceError("generated artifact path contains different bytes")
        restrict_private_file(target)
        return str(target.relative_to(root))

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=target_dir)
    temporary = Path(temporary_name)
    installed = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        restrict_private_file(temporary)
        os.replace(temporary, target)
        installed = True
        restrict_private_file(target)
        _fsync_directory(target_dir)
        # Keep registration inside the same BaseException-protected block as
        # installation.  A signal before this line removes the just-installed
        # target; a signal after it is compensated by the caller's guard.
        relative = str(target.relative_to(root))
        created_paths.add(relative)
    except BaseException:
        temporary.unlink(missing_ok=True)
        if installed:
            target.unlink(missing_ok=True)
        _prune_empty_parents(target_dir, stop=root)
        raise
    return relative


def _discard_unreferenced_paths(
    conn: Any,
    root: Path,
    relative_paths: set[str],
    *,
    inserted_raw_ids: set[str],
) -> None:
    """Compensate filesystem writes before the enclosing DB unit rolls back."""

    if not relative_paths:
        return
    resolved_root = root.resolve()
    excluded_clause = ""
    excluded_parameters: tuple[str, ...] = ()
    if inserted_raw_ids:
        placeholders = ",".join("?" for _ in inserted_raw_ids)
        excluded_clause = f" AND id NOT IN ({placeholders})"  # nosec B608 - placeholders only
        excluded_parameters = tuple(sorted(inserted_raw_ids))
    for relative in sorted(relative_paths):
        try:
            referenced = conn.execute(
                f"""SELECT 1 FROM raw_objects
                     WHERE json_valid(metadata_json)
                       AND json_extract(metadata_json,'$.stored_path')=?
                       {excluded_clause} LIMIT 1""",  # nosec B608 - fixed clause, generated placeholders
                (relative, *excluded_parameters),
            ).fetchone()
            candidate = (resolved_root / relative).resolve()
            if referenced is None and candidate.is_relative_to(resolved_root) and not candidate.is_symlink():
                candidate.unlink(missing_ok=True)
                _prune_empty_parents(candidate.parent, stop=resolved_root)
        except OSError:
            # The DB unit is about to roll back, so an undeletable
            # content-addressed blob is unavailable, never an authorization
            # handle.  A later maintenance pass may remove it; do not hide the
            # original failure.
            continue


def _prune_empty_parents(start: Path, *, stop: Path) -> None:
    """Remove only empty directories created below the private file root."""

    boundary = stop.resolve()
    current = start.resolve()
    while current != boundary and current.is_relative_to(boundary):
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_component(value: str) -> str:
    original = (value or "user").strip()
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", original).strip(" .-")[:48] or "user"
    digest = hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{slug}--{digest}"


def _safe_filename(value: Any) -> str:
    name = Path(str(value or "report.bin").replace("\\", "/")).name
    name = unicodedata.normalize("NFKC", name).replace("\x00", "")
    name = "".join(char for char in name if char >= " " and char != "\x7f")
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return "report.bin"
    suffix = Path(name).suffix
    if len(suffix) > 17 or not re.fullmatch(r"\.[\w-]{1,16}", suffix, flags=re.UNICODE):
        suffix = ""
    stem = name[: -len(suffix)] if suffix else name
    stem = stem[: max(1, _MAX_FILENAME_CHARS - len(suffix))].rstrip(" .") or "report"
    return f"{stem}{suffix}"


def _safe_mime_type(value: Any) -> str:
    mime = str(value or "application/octet-stream").split(";", 1)[0].strip().casefold()
    return mime if _MIME_TYPE.fullmatch(mime) else "application/octet-stream"


def _without_claimed_handle(item: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in _PROTECTED_RESPONSE_FIELDS}


__all__ = [
    "GeneratedFilePersistenceError",
    "GeneratedFilesPersistenceRollbackGuard",
    "generated_files_publication_transaction",
    "generated_file_descriptor",
    "persist_generated_response_files",
]
