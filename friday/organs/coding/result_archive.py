"""Pack one final Coding Mode source carrier from an inventoried workspace."""

from __future__ import annotations

import base64
import hashlib
import io
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

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
from friday.private_fs import ensure_private_directory, prepare_private_file, restrict_private_file

MAX_RESULT_FILES = 32
_SECRET_NAME_RE = (
    "env",
    "id_rsa",
    "id_ed25519",
    "credentials",
    "credential",
    "secret",
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


def _inventory(workspace: Path) -> tuple[str, ...] | None:
    if not workspace.is_dir():
        return ()
    try:
        root = workspace.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    paths: list[str] = []
    for item in sorted(root.rglob("*")):
        if item.is_symlink() or not item.is_file():
            continue
        try:
            relative = item.resolve().relative_to(root).as_posix()
        except ValueError:
            return None
        if not relative or relative.startswith("/") or ".." in relative.split("/"):
            return None
        if _secret(relative):
            continue
        paths.append(relative)
        if len(paths) > MAX_RESULT_FILES:
            return None
    return tuple(paths)


def observe_coding_result_archive(
    *,
    turn_id: str,
    workspace: Path,
    export_path: Path,
    ready: bool,
) -> CodingResultArchiveObserveV1:
    """Plan, admit, and pack one FILE or ARCHIVE carrier.  Never execute sources.

    Restart and rollback are observed from landed facts only.  Without a prior
    packed archive and exact previous revision they stay EMPTY; this path never
    restarts or rolls back a workspace.
    """

    restart = build_coding_result_restart_admission(f"{turn_id}-restart", turn_id)
    rollback = build_coding_result_rollback_admission(f"{turn_id}-rollback", turn_id)
    if not ready:
        return _empty(turn_id)
    inventory = _inventory(workspace)
    if inventory is None:
        return _blocked(turn_id, CodingResultArchiveObserveReason.WRITE_FAILED)
    plan = build_coding_result_archive_plan(f"{turn_id}-rplan", turn_id, files=list(inventory))
    if plan.plan is CodingResultArchivePlanState.EMPTY:
        return _empty(turn_id)
    if plan.plan not in {CodingResultArchivePlanState.FILE, CodingResultArchivePlanState.ARCHIVE}:
        return _blocked(turn_id, CodingResultArchiveObserveReason.PLAN_NOT_GRANTED)
    members: dict[str, bytes] = {}
    try:
        root = workspace.resolve()
        for relative in plan.files:
            payload = (root / relative).read_bytes()
            members[relative] = payload
    except (OSError, ValueError):
        return _blocked(turn_id, CodingResultArchiveObserveReason.WRITE_FAILED)
    pack = None
    if plan.plan is CodingResultArchivePlanState.ARCHIVE:
        digest_map = {name: hashlib.sha256(body).hexdigest() for name, body in members.items()}
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
    try:
        ensure_private_directory(export_path)
        if plan.plan is CodingResultArchivePlanState.FILE:
            relative = plan.files[0]
            payload = members[relative]
            dest = export_path / Path(relative).name
            prepare_private_file(dest)
            dest.write_bytes(payload)
            restrict_private_file(dest)
            files = (_attachment(Path(relative).name, payload, _mime(relative)),)
            state = CodingResultArchiveObserveState.FILE
            reason = CodingResultArchiveObserveReason.FILE_PACKED
        else:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for relative in plan.files:
                    archive.writestr(relative, members[relative])
            payload = buffer.getvalue()
            dest = export_path / CODING_RESULT_ARCHIVE_FILENAME
            prepare_private_file(dest)
            dest.write_bytes(payload)
            restrict_private_file(dest)
            files = (_attachment(CODING_RESULT_ARCHIVE_FILENAME, payload, "application/zip"),)
            state = CodingResultArchiveObserveState.ARCHIVE
            reason = CodingResultArchiveObserveReason.ARCHIVE_PACKED
    except (OSError, ValueError):
        return _blocked(turn_id, CodingResultArchiveObserveReason.WRITE_FAILED)
    if carrier.carrier not in {CodingModeCarrierState.FILE, CodingModeCarrierState.ARCHIVE}:
        return _blocked(turn_id, CodingResultArchiveObserveReason.PUBLICATION_NOT_GRANTED)
    return CodingResultArchiveObserveV1(
        state,
        reason,
        files,
        carrier,
        False,
        restart.admission.value,
        rollback.admission.value,
    )
