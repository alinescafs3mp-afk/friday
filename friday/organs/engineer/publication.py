"""Exact multi-output publication seam for Engineer generated files."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from friday.generated_files import (
    GeneratedFilesPersistenceAttestation,
    GeneratedFilesPersistenceRollbackGuard,
    generated_files_persistence_attestation,
    persist_generated_response_files,
)

MAX_EXACT_GENERATED_FILES = 16
_AUTHORITY = object()
_MIME_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")
_RAW_ID = re.compile(r"raw_[0-9a-f]{16}")


class ExactGeneratedFilePublicationError(ValueError):
    """Content-free failure at the exact generated-file batch boundary."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "generated_batch_invalid")
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class ExpectedGeneratedFile:
    filename: str
    mime_type: str
    content_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ExactGeneratedFileBatch:
    files: tuple[ExpectedGeneratedFile, ...]
    total_bytes: int
    _authority: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class ExactGeneratedFilesPublication:
    response: dict[str, Any]
    attestation: GeneratedFilesPersistenceAttestation


def _filename_is_exact_safe(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 180:
        return False
    if unicodedata.normalize("NFKC", value) != value or value != value.strip(" ."):
        return False
    if value in {".", ".."} or "/" in value or "\\" in value:
        return False
    return all(char >= " " and char != "\x7f" for char in value)


def _item_identity(value: Mapping[str, Any], *, max_bytes: int) -> ExpectedGeneratedFile:
    if set(value) != {"kind", "filename", "mime_type", "content_base64"}:
        raise ExactGeneratedFilePublicationError("generated_item_shape_invalid")
    filename = value.get("filename")
    mime_type = value.get("mime_type")
    encoded = value.get("content_base64")
    if (
        value.get("kind") != "document"
        or not _filename_is_exact_safe(filename)
        or not isinstance(mime_type, str)
        or _MIME_TYPE.fullmatch(mime_type) is None
        or not isinstance(encoded, str)
        or not encoded
    ):
        raise ExactGeneratedFilePublicationError("generated_item_invalid")
    filename = cast(str, filename)
    mime_type = cast(str, mime_type)
    encoded = cast(str, encoded)
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ExactGeneratedFilePublicationError("generated_item_invalid") from exc
    if (
        not payload
        or len(payload) > max(0, int(max_bytes))
        or not hmac.compare_digest(base64.b64encode(payload).decode("ascii"), encoded)
    ):
        raise ExactGeneratedFilePublicationError("generated_item_size_invalid")
    return ExpectedGeneratedFile(
        filename=filename,
        mime_type=mime_type,
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )


def exact_generated_file_batch(
    values: Sequence[Mapping[str, Any]],
    *,
    max_bytes: int,
) -> ExactGeneratedFileBatch:
    """Freeze exact file order and identity before assistant publication."""

    items = tuple(values)
    if not 1 <= len(items) <= MAX_EXACT_GENERATED_FILES:
        raise ExactGeneratedFilePublicationError("generated_batch_count_invalid")
    identities = tuple(_item_identity(item, max_bytes=max_bytes) for item in items)
    total = sum(item.size_bytes for item in identities)
    if total > max(0, int(max_bytes)):
        raise ExactGeneratedFilePublicationError("generated_batch_size_invalid")
    names = [item.filename.casefold() for item in identities]
    if len(names) != len(set(names)):
        raise ExactGeneratedFilePublicationError("generated_batch_filename_collision")
    return ExactGeneratedFileBatch(identities, total, _AUTHORITY)


def _response_matches_batch(response: Mapping[str, Any], batch: ExactGeneratedFileBatch) -> bool:
    values = response.get("files")
    if not isinstance(values, list) or len(values) != len(batch.files):
        return False
    try:
        observed = tuple(
            _item_identity(value, max_bytes=batch.total_bytes)
            for value in values
            if isinstance(value, Mapping)
        )
    except ExactGeneratedFilePublicationError:
        return False
    return len(observed) == len(batch.files) and observed == batch.files


def persist_exact_generated_file_batch(
    storage: Any,
    files_root: Path,
    response: Mapping[str, Any],
    batch: ExactGeneratedFileBatch,
    *,
    tenant_id: str,
    person_id: str,
    max_bytes: int,
    rollback_guard: GeneratedFilesPersistenceRollbackGuard,
) -> ExactGeneratedFilesPublication:
    """Persist and attest every expected item, preserving exact order and bytes.

    The caller owns ``generated_files_publication_transaction`` so the assistant
    row, Raw handles and filesystem compensation share one transaction.
    """

    if (
        type(batch) is not ExactGeneratedFileBatch
        or batch._authority is not _AUTHORITY
        or type(rollback_guard) is not GeneratedFilesPersistenceRollbackGuard
        or batch.total_bytes > max(0, int(max_bytes))
        or not _response_matches_batch(response, batch)
    ):
        raise ExactGeneratedFilePublicationError("generated_batch_changed")
    persisted = persist_generated_response_files(
        storage,
        Path(files_root),
        response,
        tenant_id=str(tenant_id),
        person_id=str(person_id),
        max_bytes=max_bytes,
        rollback_guard=rollback_guard,
    )
    values = persisted.get("files")
    if not isinstance(values, list) or len(values) != len(batch.files):
        raise ExactGeneratedFilePublicationError("generated_batch_persistence_invalid")
    seen_ids: set[str] = set()
    for expected, value in zip(batch.files, values, strict=True):
        if not isinstance(value, Mapping):
            raise ExactGeneratedFilePublicationError("generated_batch_persistence_invalid")
        raw_id = value.get("id")
        encoded = value.get("content_base64")
        if not isinstance(encoded, str):
            raise ExactGeneratedFilePublicationError("generated_batch_persistence_invalid")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ExactGeneratedFilePublicationError("generated_batch_persistence_invalid") from exc
        if (
            not isinstance(raw_id, str)
            or _RAW_ID.fullmatch(raw_id) is None
            or raw_id in seen_ids
            or value.get("filename") != expected.filename
            or value.get("mime_type") != expected.mime_type
            or value.get("size_bytes") != expected.size_bytes
            or not hmac.compare_digest(str(value.get("sha256") or ""), expected.content_sha256)
            or len(content) != expected.size_bytes
            or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), expected.content_sha256)
        ):
            raise ExactGeneratedFilePublicationError("generated_batch_persistence_invalid")
        seen_ids.add(raw_id)
    attestation = generated_files_persistence_attestation(persisted)
    if attestation is None:
        raise ExactGeneratedFilePublicationError("generated_batch_attestation_unavailable")
    return ExactGeneratedFilesPublication(dict(persisted), attestation)


__all__ = [
    "ExactGeneratedFileBatch",
    "ExactGeneratedFilePublicationError",
    "ExactGeneratedFilesPublication",
    "ExpectedGeneratedFile",
    "MAX_EXACT_GENERATED_FILES",
    "exact_generated_file_batch",
    "persist_exact_generated_file_batch",
]
