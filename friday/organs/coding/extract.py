"""Extract an already-supplied archive only after landed admission.

Bytes must already be in memory.  This observer never reads a host path, never
follows links, and never executes uploaded project code.
"""

from __future__ import annotations

import base64
import binascii
import io
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from friday.private_fs import ensure_private_directory, prepare_private_file, restrict_private_file

_ZIP_LOCAL = b"PK\x03\x04"
_ZIP_EMPTY = b"PK\x05\x06"
_S_IFMT = 0o170000
_S_IFLNK = 0o120000
_S_IFBLK = 0o060000
_S_IFCHR = 0o020000
_S_IFIFO = 0o010000
_S_IFSOCK = 0o140000


class CodingArchiveExtractObserveState(StrEnum):
    EMPTY = "empty"
    EXTRACTED = "extracted"
    BLOCKED = "blocked"


class CodingArchiveExtractObserveReason(StrEnum):
    NO_ARCHIVE = "no_archive"
    EXTRACTED = "extracted"
    INVALID_ARCHIVE = "invalid_archive"
    ADMISSION_NOT_GRANTED = "admission_not_granted"
    PLAN_NOT_GRANTED = "plan_not_granted"
    ISOLATION_NOT_GRANTED = "isolation_not_granted"
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True, slots=True)
class CodingArchiveExtractObserveV1:
    """Closed extract observation.  Untrusted execute is never attempted."""

    state: CodingArchiveExtractObserveState
    reason: CodingArchiveExtractObserveReason
    extracted_count: int
    untrusted_execute: bool = False


def _unix_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def _member_path(name: str) -> str | None:
    text = name.replace("\\", "/").strip()
    if not text:
        return None
    if text.startswith("/") or (len(text) >= 2 and text[0].isalpha() and text[1] == ":"):
        return text.rstrip("/") or text
    parts = tuple(part for part in text.split("/") if part)
    if not parts:
        return None
    return "/".join(parts)


def _member_from_zip(info: zipfile.ZipInfo):
    from friday.orchestration.coding_archive_extract_admission import (
        CodingArchiveExtractAdmissionError,
        CodingArchiveFileKind,
        CodingArchiveLinkKind,
        CodingArchiveMemberV1,
    )

    path = _member_path(info.filename)
    if path is None:
        return None
    mode = _unix_mode(info) & _S_IFMT
    link = CodingArchiveLinkKind.SYMLINK if mode == _S_IFLNK else CodingArchiveLinkKind.NONE
    if mode in {_S_IFBLK, _S_IFCHR, _S_IFIFO, _S_IFSOCK}:
        kind = CodingArchiveFileKind.DEVICE
    elif info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
        kind = CodingArchiveFileKind.DIRECTORY
    else:
        kind = CodingArchiveFileKind.REGULAR_FILE
    try:
        return CodingArchiveMemberV1(
            path=path,
            compressed_size=int(info.compress_size),
            uncompressed_size=int(info.file_size),
            link_kind=link,
            file_kind=kind,
        )
    except (CodingArchiveExtractAdmissionError, TypeError, ValueError):
        return None


def archive_bytes_from_attachment(item: object) -> bytes | None:
    """Return in-memory archive bytes.  Host paths are never opened."""

    if not isinstance(item, Mapping):
        return None
    raw = item.get("content")
    if raw is None:
        raw = item.get("bytes")
    if type(raw) is bytes:
        return raw if raw else None
    encoded = item.get("content_b64")
    if type(encoded) is not str or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None
    return decoded or None


def _zip_payload(raw: bytes) -> zipfile.ZipFile | None:
    if not (raw.startswith(_ZIP_LOCAL) or raw.startswith(_ZIP_EMPTY)):
        return None
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw), mode="r")
        archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return None
    return archive


def _blocked(reason: CodingArchiveExtractObserveReason) -> CodingArchiveExtractObserveV1:
    return CodingArchiveExtractObserveV1(CodingArchiveExtractObserveState.BLOCKED, reason, 0, False)


def _empty() -> CodingArchiveExtractObserveV1:
    return CodingArchiveExtractObserveV1(
        CodingArchiveExtractObserveState.EMPTY,
        CodingArchiveExtractObserveReason.NO_ARCHIVE,
        0,
        False,
    )


def _safe_destination(workspace: Path, relative: str) -> Path | None:
    try:
        root = workspace.resolve()
        dest = (workspace / relative).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        dest.relative_to(root)
    except ValueError:
        return None
    return dest


def observe_coding_archive_extract(
    *,
    extract_id: str,
    authenticated_turn_id: str,
    workspace: Path,
    raw: bytes | None,
) -> CodingArchiveExtractObserveV1:
    """Catalog, admit, plan, isolate, then extract.  Write nothing if blocked."""

    if raw is None or not raw:
        return _empty()
    if not (raw.startswith(_ZIP_LOCAL) or raw.startswith(_ZIP_EMPTY)):
        return _empty()
    from friday.orchestration.coding_archive_extract_admission import (
        CodingArchiveExtractAdmissionError,
        CodingArchiveExtractAdmissionState,
        CodingArchiveFileKind,
        CodingArchiveMemberV1,
        build_coding_archive_extract_admission,
    )
    from friday.orchestration.coding_archive_extract_plan import (
        CodingArchiveExtractPlanError,
        CodingArchiveExtractPlanState,
        build_coding_archive_extract_plan,
    )
    from friday.orchestration.coding_archive_member_catalog import (
        CodingArchiveMemberCatalogError,
        CodingArchiveMemberCatalogState,
        build_coding_archive_member_catalog,
    )
    from friday.orchestration.coding_project_isolation_admission import (
        CodingProjectIsolationAdmissionError,
        CodingProjectIsolationAdmissionState,
        build_coding_project_isolation_admission,
    )

    archive = _zip_payload(raw)
    if archive is None:
        return _blocked(CodingArchiveExtractObserveReason.INVALID_ARCHIVE)
    contract_errors = (
        CodingArchiveExtractAdmissionError,
        CodingArchiveExtractPlanError,
        CodingArchiveMemberCatalogError,
        CodingProjectIsolationAdmissionError,
    )
    try:
        try:
            infos = tuple(archive.infolist())
            members: list[CodingArchiveMemberV1] = []
            by_path: dict[str, zipfile.ZipInfo] = {}
            for info in infos:
                member = _member_from_zip(info)
                if member is None:
                    return _blocked(CodingArchiveExtractObserveReason.INVALID_ARCHIVE)
                if member.path in by_path:
                    return _blocked(CodingArchiveExtractObserveReason.INVALID_ARCHIVE)
                members.append(member)
                by_path[member.path] = info
            catalog = build_coding_archive_member_catalog(
                extract_id + "-cat",
                authenticated_turn_id,
                members,
            )
            admission = build_coding_archive_extract_admission(
                extract_id + "-adm",
                authenticated_turn_id,
                members,
            )
            if catalog.catalog is not CodingArchiveMemberCatalogState.CATALOGUED:
                if catalog.catalog is CodingArchiveMemberCatalogState.EMPTY:
                    return _empty()
                return _blocked(CodingArchiveExtractObserveReason.ADMISSION_NOT_GRANTED)
            if admission.admission is not CodingArchiveExtractAdmissionState.ADMITTED:
                if admission.admission is CodingArchiveExtractAdmissionState.EMPTY:
                    return _empty()
                return _blocked(CodingArchiveExtractObserveReason.ADMISSION_NOT_GRANTED)
            plan = build_coding_archive_extract_plan(
                extract_id + "-plan",
                authenticated_turn_id,
                catalog,
                admission,
            )
            if plan.plan is not CodingArchiveExtractPlanState.PLANNED:
                return _blocked(CodingArchiveExtractObserveReason.PLAN_NOT_GRANTED)
            try:
                ensure_private_directory(workspace)
                root_path = workspace.resolve()
            except (OSError, ValueError):
                return _blocked(CodingArchiveExtractObserveReason.WRITE_FAILED)
            root = str(root_path)
            pending: list[tuple[Path, bytes | None]] = []
            for index, destination in enumerate(plan.destination_paths):
                isolation = build_coding_project_isolation_admission(
                    extract_id + "-i" + str(index),
                    authenticated_turn_id,
                    project_root=root,
                    destination=destination,
                )
                if isolation.admission is not CodingProjectIsolationAdmissionState.ADMITTED:
                    return _blocked(CodingArchiveExtractObserveReason.ISOLATION_NOT_GRANTED)
                zip_info = by_path.get(destination)
                dest = _safe_destination(root_path, destination)
                if zip_info is None or dest is None:
                    return _blocked(CodingArchiveExtractObserveReason.ISOLATION_NOT_GRANTED)
                member = _member_from_zip(zip_info)
                if member is None:
                    return _blocked(CodingArchiveExtractObserveReason.INVALID_ARCHIVE)
                if member.file_kind is CodingArchiveFileKind.DIRECTORY:
                    pending.append((dest, None))
                    continue
                try:
                    with archive.open(zip_info, "r") as handle:
                        payload = handle.read()
                except (OSError, RuntimeError, ValueError, zipfile.BadZipFile):
                    return _blocked(CodingArchiveExtractObserveReason.WRITE_FAILED)
                if len(payload) != member.uncompressed_size:
                    return _blocked(CodingArchiveExtractObserveReason.INVALID_ARCHIVE)
                pending.append((dest, payload))
            try:
                for dest, body in pending:
                    if body is None:
                        ensure_private_directory(dest)
                        continue
                    ensure_private_directory(dest.parent)
                    prepare_private_file(dest)
                    dest.write_bytes(body)
                    restrict_private_file(dest)
            except (OSError, ValueError):
                return _blocked(CodingArchiveExtractObserveReason.WRITE_FAILED)
            return CodingArchiveExtractObserveV1(
                CodingArchiveExtractObserveState.EXTRACTED,
                CodingArchiveExtractObserveReason.EXTRACTED,
                len(pending),
                False,
            )
        except contract_errors:
            return _blocked(CodingArchiveExtractObserveReason.INVALID_ARCHIVE)
        except (OSError, TypeError, ValueError, RuntimeError, zipfile.BadZipFile):
            return _blocked(CodingArchiveExtractObserveReason.INVALID_ARCHIVE)
    finally:
        archive.close()


def first_archive_bytes(attachments: Sequence[object] | None) -> bytes | None:
    """Return the first in-memory zip payload.  Filenames are not opened."""

    for item in attachments or ():
        raw = archive_bytes_from_attachment(item)
        if raw is None:
            continue
        if raw.startswith(_ZIP_LOCAL) or raw.startswith(_ZIP_EMPTY):
            return raw
    return None
