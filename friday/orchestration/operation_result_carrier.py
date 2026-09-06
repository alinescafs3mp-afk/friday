"""Universal final-carrier policy for every interactive operation.

One user message yields one editable status and one final result: text, one
ordinary file, or one deterministic archive.  Path validation and the closed
TEXT/FILE/ARCHIVE choice reuse the Engineer policy so chat, files, archive,
web and later Coding cannot invent a second carrier family.  Archive packing
consumes already-supplied bytes only; it never opens a path.
"""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from friday.orchestration.engineer_result_carrier import (
    EngineerResultCarrierKind,
    EngineerResultCarrierPlan,
    EngineerResultFile,
    EngineerResultPolicyError,
    is_internal_result_file,
    select_engineer_result_carrier,
    select_user_result_files,
    validate_engineer_result_carrier,
)

OPERATION_RESULT_CARRIER_SCHEMA = "friday.operation-result-carrier.v1"
MAX_OPERATION_RESULT_FILES = 32
MAX_OPERATION_RESULT_ARCHIVE_BYTES = 36 * 1024 * 1024
OPERATION_RESULT_ARCHIVE_FILENAME = "friday-result.zip"
OPERATION_RESULT_ARCHIVE_MIME_TYPE = "application/zip"

OperationResultCarrierKind = EngineerResultCarrierKind
OperationResultCarrierPlan = EngineerResultCarrierPlan
OperationResultFile = EngineerResultFile
OperationResultCarrierError = EngineerResultPolicyError


@dataclass(frozen=True, slots=True)
class OperationResultDocument:
    """One already-decoded user document the transport may send."""

    artifact_id: str
    filename: str
    mime_type: str
    payload: bytes

    def to_mapping(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": len(self.payload),
        }


def select_operation_result_carrier(
    files: Iterable[EngineerResultFile | str | Mapping[str, Any]]
    | EngineerResultFile
    | str
    | Mapping[str, Any]
    | None,
    *,
    requested: EngineerResultCarrierKind | str | None = None,
    archive_requested: bool = False,
    include_internal: bool = False,
) -> EngineerResultCarrierPlan:
    """Select text, one file, or one archive from already-inventoried paths."""

    selected = select_user_result_files(files, include_internal=include_internal)
    if len(selected) > MAX_OPERATION_RESULT_FILES:
        raise EngineerResultPolicyError("result_file_count_limit")
    return select_engineer_result_carrier(
        selected,
        requested=requested,
        archive_requested=archive_requested,
        include_internal=True,
    )


plan_operation_result_carrier = select_operation_result_carrier
choose_operation_result_carrier = select_operation_result_carrier
validate_operation_result_carrier = validate_engineer_result_carrier


def pack_operation_result_archive(
    members: Sequence[tuple[str, bytes]],
    *,
    max_archive_bytes: int = MAX_OPERATION_RESULT_ARCHIVE_BYTES,
) -> bytes:
    """Pack already-supplied user files into one ZIP_STORED archive."""

    if (
        isinstance(max_archive_bytes, bool)
        or not isinstance(max_archive_bytes, int)
        or max_archive_bytes <= 0
    ):
        raise EngineerResultPolicyError("result_archive_limit_invalid")
    archive_limit = min(max_archive_bytes, MAX_OPERATION_RESULT_ARCHIVE_BYTES)
    if isinstance(members, (str, bytes, bytearray)) or not isinstance(members, Sequence):
        raise EngineerResultPolicyError("result_files_invalid")
    if len(members) > MAX_OPERATION_RESULT_FILES:
        raise EngineerResultPolicyError("result_file_count_limit")
    checked: list[tuple[str, bytes]] = []
    for member in members:
        if (
            not isinstance(member, (tuple, list))
            or len(member) != 2
            or not isinstance(member[0], str)
            or type(member[1]) is not bytes
        ):
            raise EngineerResultPolicyError("result_file_invalid")
        checked.append((member[0], member[1]))
    selected = select_user_result_files(
        tuple({"relative_path": path, "size_bytes": len(payload)} for path, payload in checked)
    )
    plan = select_operation_result_carrier(selected, include_internal=True)
    if plan.carrier is not EngineerResultCarrierKind.ARCHIVE:
        raise EngineerResultPolicyError("result_archive_requires_multiple_files")
    by_path = dict(checked)
    # This seekable ZIP_STORED writer emits 30-byte local headers, 46-byte
    # central headers and one 22-byte end record; no extras, comments or Zip64.
    # Bound allocation before writing, including both copies of each UTF-8 name.
    try:
        predicted_size = 22 + sum(
            76 + 2 * len(item.relative_path.encode("utf-8")) + len(by_path[item.relative_path])
            for item in plan.files
        )
    except UnicodeError as exc:
        raise EngineerResultPolicyError("result_path_invalid") from exc
    if predicted_size > archive_limit:
        raise EngineerResultPolicyError("result_archive_size_limit")
    buffer = io.BytesIO()
    try:
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=False,
            strict_timestamps=True,
        ) as archive:
            archive.comment = b""
            for item in plan.files:
                payload = by_path[item.relative_path]
                # Empty package markers (for example __init__.py) are real
                # source members, not an empty final archive.
                info = zipfile.ZipInfo(filename=item.relative_path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.extra = b""
                info.comment = b""
                archive.writestr(info, payload)
    except EngineerResultPolicyError:
        raise
    except (OSError, OverflowError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise EngineerResultPolicyError("result_archive_write_failed") from exc
    payload = buffer.getvalue()
    if len(payload) != predicted_size or len(payload) > archive_limit:
        raise EngineerResultPolicyError("result_archive_size_limit")
    return payload


def plan_generated_file_documents(
    files: object,
) -> tuple[EngineerResultCarrierPlan, tuple[OperationResultDocument, ...]]:
    """Admit chat/tool `files` into one TEXT, FILE or ARCHIVE document list."""

    if files is None:
        raw_items: Sequence[object] = ()
    elif isinstance(files, Mapping):
        raw_items = (files,)
    elif isinstance(files, Sequence) and not isinstance(files, (str, bytes, bytearray)):
        raw_items = files
    else:
        raise EngineerResultPolicyError("result_files_invalid")
    if len(raw_items) > MAX_OPERATION_RESULT_FILES:
        raise EngineerResultPolicyError("result_file_count_limit")
    decoded: list[tuple[str, str, str, bytes]] = []
    remaining = MAX_OPERATION_RESULT_ARCHIVE_BYTES
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        encoded = item.get("content_base64")
        payload = item.get("payload")
        if not isinstance(payload, bytes) and not (isinstance(encoded, str) and encoded):
            continue
        filename = item.get("filename") or item.get("relative_path") or item.get("path")
        if not isinstance(filename, str) or not filename.strip():
            continue
        filename = filename.strip()
        # Filter by the same policy before decoding, and bind final selection
        # to this path. Position in the unfiltered input is not authority.
        internal = item.get("internal", False)
        if not isinstance(internal, bool):
            raise EngineerResultPolicyError("result_internal_flag_invalid")
        if internal or is_internal_result_file(filename):
            continue
        if isinstance(payload, bytes):
            body = payload
        else:
            assert isinstance(encoded, str)
            if len(encoded) > 4 * ((remaining + 2) // 3):
                raise EngineerResultPolicyError("result_archive_size_limit")
            try:
                body = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                continue
        if not body:
            continue
        if len(body) > remaining:
            raise EngineerResultPolicyError("result_archive_size_limit")
        remaining -= len(body)
        mime_type = item.get("mime_type")
        if not isinstance(mime_type, str) or not mime_type.strip():
            mime_type = "application/octet-stream"
        artifact_id = item.get("id") or item.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id.strip():
            artifact_id = filename
        decoded.append((artifact_id.strip(), filename, mime_type.strip(), body))
    plan = select_operation_result_carrier(
        tuple(EngineerResultFile(name, mime, len(body)) for _, name, mime, body in decoded)
    )
    if plan.carrier is EngineerResultCarrierKind.TEXT:
        return plan, ()
    if plan.carrier is EngineerResultCarrierKind.FILE:
        selected_path = plan.files[0].relative_path
        artifact_id, filename, mime_type, body = next(row for row in decoded if row[1] == selected_path)
        return plan, (OperationResultDocument(artifact_id, filename, mime_type, body),)
    packed = pack_operation_result_archive(tuple((name, body) for _, name, _, body in decoded))
    digest = hashlib.sha256(packed).hexdigest()
    return plan, (
        OperationResultDocument(
            artifact_id=f"archive:{digest}",
            filename=OPERATION_RESULT_ARCHIVE_FILENAME,
            mime_type=OPERATION_RESULT_ARCHIVE_MIME_TYPE,
            payload=packed,
        ),
    )


__all__ = [
    "MAX_OPERATION_RESULT_ARCHIVE_BYTES",
    "MAX_OPERATION_RESULT_FILES",
    "OPERATION_RESULT_ARCHIVE_FILENAME",
    "OPERATION_RESULT_ARCHIVE_MIME_TYPE",
    "OPERATION_RESULT_CARRIER_SCHEMA",
    "OperationResultCarrierError",
    "OperationResultCarrierKind",
    "OperationResultCarrierPlan",
    "OperationResultDocument",
    "OperationResultFile",
    "choose_operation_result_carrier",
    "pack_operation_result_archive",
    "plan_generated_file_documents",
    "plan_operation_result_carrier",
    "select_operation_result_carrier",
    "select_user_result_files",
    "validate_operation_result_carrier",
]
