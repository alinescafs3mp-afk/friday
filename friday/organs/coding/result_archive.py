"""Pack one final Coding Mode source carrier from an inventoried workspace."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from friday.orchestration.coding_archive_extract_admission import (
    MAX_ARCHIVE_MEMBER_COUNT,
    MAX_ARCHIVE_NESTING_DEPTH,
)
from friday.orchestration.coding_mode_carrier import (
    CodingModeCarrierState,
    CodingModeCarrierV1,
    build_coding_mode_carrier,
)
from friday.orchestration.coding_result_archive_manifest import (
    CodingResultArchiveManifestState,
    build_coding_result_archive_manifest,
)
from friday.orchestration.coding_result_archive_pack_admission import (
    CodingResultArchivePackAdmissionState,
    build_coding_result_archive_pack_admission,
)
from friday.orchestration.coding_result_archive_plan import (
    CODING_RESULT_ARCHIVE_FILENAME,
    CodingResultArchivePlanState,
    build_coding_result_archive_plan,
)
from friday.orchestration.coding_result_publication_admission import (
    CodingResultPublicationAdmissionState,
    build_coding_result_publication_admission,
)
from friday.orchestration.coding_result_restart_admission import (
    build_coding_result_restart_admission,
)
from friday.orchestration.coding_result_rollback_admission import (
    build_coding_result_rollback_admission,
)
from friday.orchestration.coding_result_uncertainty import (
    CodingResultUncertaintyState,
    build_coding_result_uncertainty,
)
from friday.orchestration.engineer_result_carrier import is_internal_result_file
from friday.orchestration.operation_result_carrier import (
    MAX_OPERATION_RESULT_ARCHIVE_BYTES,
    MAX_OPERATION_RESULT_FILES,
    pack_operation_result_archive,
)
from friday.organs.coding.workspace_io import entry_stamp as _stamp
from friday.organs.coding.workspace_io import open_directory as _directory
from friday.organs.coding.workspace_io import open_relative_directory as _relative_directory
from friday.organs.coding.workspace_io import source_revision_sha256

MAX_RESULT_FILES = MAX_OPERATION_RESULT_FILES
MAX_RESULT_INPUT_BYTES = MAX_OPERATION_RESULT_ARCHIVE_BYTES
_GENERATED_COMPONENTS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "node_modules",
        ".ds_store",
        ".coverage",
        "htmlcov",
    }
)


class CodingResultArchiveObserveState(StrEnum):
    EMPTY = "empty"
    FILE = "file"
    ARCHIVE = "archive"
    BLOCKED = "blocked"


class CodingResultArchiveObserveReason(StrEnum):
    NO_FILES = "no_files"
    FILE_PACKED = "file_packed"
    ARCHIVE_PACKED = "archive_packed"
    PLAN_NOT_GRANTED = "plan_not_granted"
    PACK_NOT_GRANTED = "pack_not_granted"
    PUBLICATION_NOT_GRANTED = "publication_not_granted"
    WRITE_FAILED = "write_failed"


@dataclass(frozen=True, slots=True)
class CodingResultArchiveObserveV1:
    """Closed one-final-carrier observation.  Does not execute project code."""

    state: CodingResultArchiveObserveState
    reason: CodingResultArchiveObserveReason
    files: tuple[dict[str, Any], ...]
    carrier: CodingModeCarrierV1
    untrusted_execute: bool = False
    restart_state: str = "empty"
    rollback_state: str = "empty"
    source_digests: tuple[tuple[str, str], ...] = ()
    source_revision: str | None = None


def _empty(turn_id: str) -> CodingResultArchiveObserveV1:
    return CodingResultArchiveObserveV1(
        CodingResultArchiveObserveState.EMPTY,
        CodingResultArchiveObserveReason.NO_FILES,
        (),
        build_coding_mode_carrier(f"{turn_id}-carrier", turn_id),
        False,
    )


def _blocked(turn_id: str, reason: CodingResultArchiveObserveReason) -> CodingResultArchiveObserveV1:
    return CodingResultArchiveObserveV1(
        CodingResultArchiveObserveState.BLOCKED,
        reason,
        (),
        build_coding_mode_carrier(f"{turn_id}-carrier", turn_id),
        False,
    )


def _attachment(filename: str, payload: bytes, mime_type: str) -> dict[str, Any]:
    return {
        "filename": filename,
        "mime_type": mime_type,
        "content_base64": base64.standard_b64encode(payload).decode("ascii"),
        "size": len(payload),
    }


def _mime(name: str) -> str:
    lowered = name.casefold()
    if lowered.endswith(".py"):
        return "text/x-python"
    if lowered.endswith(".md"):
        return "text/markdown"
    if lowered.endswith(".js"):
        return "text/javascript"
    if lowered.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"


def _secret(relative: str) -> bool:
    parts = tuple(part.casefold() for part in relative.split("/"))
    return any(
        part == ".env"
        or part.startswith(".env.")
        or part in {"id_rsa", "id_ed25519", "credentials", "credential"}
        or part.startswith("secret")
        for part in parts
    )


def is_exportable_source_path(relative: str) -> bool:
    """Share exact output exclusions with revision edits; never drop requested edits."""

    parts = relative.split("/")
    return (
        not _secret(relative)
        and not any(part.casefold() in _GENERATED_COMPONENTS for part in parts)
        and not relative.casefold().endswith((".pyc", ".pyo"))
        and not is_internal_result_file(relative)
        and not any(
            is_internal_result_file("/".join(parts[:index]) + "/source.py") for index in range(1, len(parts))
        )
    )


def _read_bounded(descriptor: int, budget: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, budget - total + 1))
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > budget:
            raise ValueError("source/export byte budget exceeded")
        chunks.append(chunk)


def _snapshot(workspace: Path) -> dict[str, bytes]:
    """Read one bounded, no-alias byte snapshot and detect concurrent drift.

    Keep file descriptors until all reads finish. Recheck both inode metadata
    and directory names before releasing the snapshot; manifests and carriers
    subsequently consume these same immutable bytes, never reopen source paths.
    """

    members: dict[str, bytes] = {}
    records: list[tuple[tuple[str, ...], int, tuple[int, ...]]] = []
    directories: list[tuple[tuple[str, ...], tuple[int, ...]]] = []
    visited = 0
    total = 0
    with _directory(workspace) as root, ExitStack() as handles:
        root_identity = _stamp(os.fstat(root))

        def walk(descriptor: int, parts: tuple[str, ...]) -> None:
            nonlocal visited, total
            if len(parts) > MAX_ARCHIVE_NESTING_DEPTH:
                raise ValueError("source directory depth exceeded")
            directories.append((parts, _stamp(os.fstat(descriptor))))
            with os.scandir(descriptor) as iterator:
                entries = []
                for entry in iterator:
                    visited += 1
                    if visited > MAX_ARCHIVE_MEMBER_COUNT:
                        raise ValueError("source inventory budget exceeded")
                    entries.append(entry)
                for entry in sorted(entries, key=lambda item: item.name):
                    path = (*parts, entry.name)
                    relative = "/".join(path)
                    if _secret(relative) or entry.name.casefold() in _GENERATED_COMPONENTS:
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        if is_internal_result_file(relative + "/source.py"):
                            continue
                        with _relative_directory(descriptor, (entry.name,)) as child:
                            if _stamp(os.fstat(child)) != _stamp(info):
                                raise ValueError("source directory changed")
                            walk(child, path)
                        continue
                    if not is_exportable_source_path(relative):
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("source is not a unique regular file")
                    if (
                        len(members) >= MAX_RESULT_FILES
                        or not 0 <= info.st_size <= MAX_RESULT_INPUT_BYTES - total
                    ):
                        raise ValueError("source file budget exceeded")
                    file_descriptor = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                        dir_fd=descriptor,
                    )
                    handles.callback(os.close, file_descriptor)
                    expected = _stamp(info)
                    if _stamp(os.fstat(file_descriptor)) != expected:
                        raise ValueError("source file changed before open")
                    body = _read_bounded(file_descriptor, MAX_RESULT_INPUT_BYTES - total)
                    if len(body) != info.st_size or _stamp(os.fstat(file_descriptor)) != expected:
                        raise ValueError("source changed while reading")
                    total += len(body)
                    members[relative] = body
                    records.append((path, file_descriptor, expected))

        walk(root, ())
        for path, descriptor, expected in records:
            with _relative_directory(root, path[:-1]) as parent:
                current_entry = os.stat(path[-1], dir_fd=parent, follow_symlinks=False)
            if _stamp(os.fstat(descriptor)) != expected or _stamp(current_entry) != expected:
                raise ValueError("source snapshot changed")
        for parts, expected in directories:
            with _relative_directory(root, parts) as current:
                if _stamp(os.fstat(current)) != expected:
                    raise ValueError("source inventory changed")
        with _directory(workspace) as current:
            if _stamp(os.fstat(current)) != root_identity:
                raise ValueError("source root changed")
    return members


def _existing_export_matches(directory: int, name: str, payload: bytes) -> bool:
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory
        )
    except FileNotFoundError:
        return False
    try:
        expected = os.fstat(descriptor)
        if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1 or expected.st_size != len(payload):
            raise ValueError("export destination already contains another object")
        if _read_bounded(descriptor, len(payload)) != payload:
            raise ValueError("export destination already contains another revision")
        if _stamp(os.fstat(descriptor)) != _stamp(expected) or _stamp(
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        ) != _stamp(expected):
            raise ValueError("existing export changed")
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.fsync(directory)
        return True
    finally:
        os.close(descriptor)


def _publish_export(export_path: Path, name: str, payload: bytes) -> None:
    """Install a complete private artifact atomically, without overwriting a revision.

    Repeated publication of identical bytes is idempotent. A conflicting object
    requires a new operation/revision, not a destructive rewrite of its export.
    """

    with _directory(export_path, create=True) as directory:
        if _existing_export_matches(directory, name, payload):
            return
        temporary = ".friday-export-" + secrets.token_hex(16)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        try:
            expected = os.fstat(descriptor)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short export write")
                view = view[written:]
            os.fsync(descriptor)
            with _directory(export_path) as current:
                if (os.fstat(current).st_dev, os.fstat(current).st_ino) != (
                    os.fstat(directory).st_dev,
                    os.fstat(directory).st_ino,
                ):
                    raise ValueError("export directory changed")
            try:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                if not _existing_export_matches(directory, name, payload):
                    raise ValueError("export publication raced another writer") from None
            else:
                installed = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if (installed.st_dev, installed.st_ino, installed.st_size) != (
                    expected.st_dev,
                    expected.st_ino,
                    len(payload),
                ):
                    raise ValueError("export publication changed")
        finally:
            os.close(descriptor)
            os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)


def observe_coding_result_archive(
    *,
    turn_id: str,
    workspace: Path,
    export_path: Path,
    ready: bool,
    expected_revision: str | None = None,
) -> CodingResultArchiveObserveV1:
    """Plan, admit, and pack one FILE or ARCHIVE carrier.  Never execute sources.

    Restart and rollback are observed from landed facts only.  Without a prior
    packed archive and exact previous revision they stay EMPTY; this path never
    restarts or rolls back a workspace.
    """

    restart = build_coding_result_restart_admission(f"{turn_id}-restart", turn_id)
    rollback = build_coding_result_rollback_admission(f"{turn_id}-rollback", turn_id)
    if ready is not True:
        return _empty(turn_id)
    try:
        workspace = Path(workspace).absolute()
        export_path = Path(export_path).absolute()
        if workspace == export_path or workspace in export_path.parents or export_path in workspace.parents:
            raise ValueError("source and export directories must be disjoint")
        snapshot = _snapshot(workspace)
    except (OSError, TypeError, ValueError):
        return _blocked(turn_id, CodingResultArchiveObserveReason.WRITE_FAILED)
    plan = build_coding_result_archive_plan(f"{turn_id}-rplan", turn_id, files=list(snapshot))
    if plan.plan is CodingResultArchivePlanState.EMPTY:
        return _empty(turn_id)
    if plan.plan not in {CodingResultArchivePlanState.FILE, CodingResultArchivePlanState.ARCHIVE}:
        return _blocked(turn_id, CodingResultArchiveObserveReason.PLAN_NOT_GRANTED)
    members = {relative: snapshot[relative] for relative in plan.files}
    digest_map = {name: hashlib.sha256(body).hexdigest() for name, body in members.items()}
    revision = source_revision_sha256(digest_map)
    if expected_revision is not None and revision != expected_revision:
        return _blocked(turn_id, CodingResultArchiveObserveReason.PACK_NOT_GRANTED)
    pack = None
    if plan.plan is CodingResultArchivePlanState.ARCHIVE:
        manifest = build_coding_result_archive_manifest(f"{turn_id}-manifest", turn_id, digest_map)
        if manifest.manifest is not CodingResultArchiveManifestState.LISTED:
            return _blocked(turn_id, CodingResultArchiveObserveReason.PACK_NOT_GRANTED)
        pack = build_coding_result_archive_pack_admission(f"{turn_id}-pack", turn_id, plan, manifest)
        if pack.admission is not CodingResultArchivePackAdmissionState.ADMITTED:
            return _blocked(turn_id, CodingResultArchiveObserveReason.PACK_NOT_GRANTED)
    uncertainty = build_coding_result_uncertainty(
        f"{turn_id}-unc",
        turn_id,
        plan,
        pack,
        restart,
        rollback,
    )
    if uncertainty.uncertainty is not CodingResultUncertaintyState.KNOWN:
        return _blocked(turn_id, CodingResultArchiveObserveReason.PUBLICATION_NOT_GRANTED)
    publication = build_coding_result_publication_admission(
        f"{turn_id}-pub",
        turn_id,
        plan,
        pack,
        uncertainty=uncertainty,
    )
    if publication.admission is not CodingResultPublicationAdmissionState.ADMITTED:
        return _blocked(turn_id, CodingResultArchiveObserveReason.PUBLICATION_NOT_GRANTED)
    carrier = build_coding_mode_carrier(f"{turn_id}-carrier", turn_id, publication)
    if carrier.carrier not in {CodingModeCarrierState.FILE, CodingModeCarrierState.ARCHIVE}:
        return _blocked(turn_id, CodingResultArchiveObserveReason.PUBLICATION_NOT_GRANTED)
    try:
        if plan.plan is CodingResultArchivePlanState.FILE:
            relative = plan.files[0]
            payload = members[relative]
            if not payload:
                return _blocked(turn_id, CodingResultArchiveObserveReason.PACK_NOT_GRANTED)
            filename = Path(relative).name
            mime_type = _mime(relative)
            state = CodingResultArchiveObserveState.FILE
            reason = CodingResultArchiveObserveReason.FILE_PACKED
        else:
            payload = pack_operation_result_archive(tuple(members.items()))
            filename = CODING_RESULT_ARCHIVE_FILENAME
            mime_type = "application/zip"
            state = CodingResultArchiveObserveState.ARCHIVE
            reason = CodingResultArchiveObserveReason.ARCHIVE_PACKED
        _publish_export(export_path, filename, payload)
        files = (_attachment(filename, payload, mime_type),)
    except (OSError, ValueError):
        return _blocked(turn_id, CodingResultArchiveObserveReason.WRITE_FAILED)
    return CodingResultArchiveObserveV1(
        state,
        reason,
        files,
        carrier,
        False,
        restart.admission.value,
        rollback.admission.value,
        tuple(sorted(digest_map.items())),
        revision,
    )
