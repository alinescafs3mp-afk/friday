"""Upload-modification observer for Coding Mode.

Compose inspect, isolation, identity and an edit plan, then admit.  Never apply
edits, never rewrite uploaded files, and never execute the uploaded program.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from friday.orchestration.coding_implementation_plan import build_coding_implementation_plan
from friday.orchestration.coding_inspect_report import CodingInspectReportV1
from friday.orchestration.coding_project_identity import build_coding_project_identity
from friday.orchestration.coding_project_isolation_admission import (
    build_coding_project_isolation_admission,
)
from friday.orchestration.coding_upload_modification_admission import (
    CodingUploadModificationAdmissionState,
    CodingUploadModificationAdmissionV1,
    build_coding_upload_modification_admission,
)

_MODIFY_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:edit|modify|change|patch|update|rewrite)\b"
    r"|измени|поправ|отредактир|правк"
    r")"
)


class CodingUploadModificationObserveState(StrEnum):
    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingUploadModificationObserveReason(StrEnum):
    NO_UPLOAD = "no_upload"
    NOT_MODIFY = "not_modify"
    ADMITTED = "admitted"
    ADMISSION_NOT_GRANTED = "admission_not_granted"


@dataclass(frozen=True, slots=True)
class CodingUploadModificationObserveV1:
    """Closed modification observation.  Apply and untrusted execute stay empty."""

    state: CodingUploadModificationObserveState
    reason: CodingUploadModificationObserveReason
    admission: CodingUploadModificationAdmissionV1
    applied: bool = False
    untrusted_execute: bool = False


def modify_requested(message: str, *, has_members: bool) -> bool:
    """True only for an edit request against already-supplied members."""

    if not has_members or not (message or "").strip():
        return False
    return _MODIFY_RE.search(message) is not None


def _empty(turn_id: str, reason: CodingUploadModificationObserveReason) -> CodingUploadModificationObserveV1:
    return CodingUploadModificationObserveV1(
        CodingUploadModificationObserveState.EMPTY,
        reason,
        build_coding_upload_modification_admission(f"{turn_id}-modify", turn_id),
        False,
        False,
    )


def _targets(message: str, members: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    paths: list[str] = []
    for item in members:
        raw = item.get("relative_path")
        if type(raw) is not str or not raw.strip():
            continue
        path = raw.replace("\\", "/").strip()
        if path.startswith("/") or ".." in path.split("/"):
            continue
        paths.append(path)
        if len(paths) >= 16:
            break
    if not paths:
        return ()
    lowered = (message or "").casefold()
    named = tuple(
        path for path in paths if path.casefold() in lowered or path.rsplit("/", 1)[-1].casefold() in lowered
    )
    return named or tuple(paths)


def _step_id(path: str, seen: dict[str, int]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", path.rsplit(".", 1)[0].casefold()).strip("_") or "file"
    if base[0].isdigit():
        base = "f_" + base
    seen[base] = seen.get(base, 0) + 1
    if seen[base] > 1:
        return f"{base}_{seen[base]}"
    return base


def observe_coding_upload_modification(
    *,
    turn_id: str,
    project_id: str,
    revision_selector: str,
    message: str,
    workspace: Path,
    inspect_report: CodingInspectReportV1,
    members: Sequence[Mapping[str, object]],
    creating: bool,
) -> CodingUploadModificationObserveV1:
    """Admit mapped-tree edits.  Never write, extract, or execute."""

    del workspace
    if creating:
        return _empty(turn_id, CodingUploadModificationObserveReason.NOT_MODIFY)
    if not members:
        return _empty(turn_id, CodingUploadModificationObserveReason.NO_UPLOAD)
    if not modify_requested(message, has_members=True):
        return _empty(turn_id, CodingUploadModificationObserveReason.NOT_MODIFY)
    targets = _targets(message, members)
    if not targets:
        return _empty(turn_id, CodingUploadModificationObserveReason.NO_UPLOAD)
    seen: dict[str, int] = {}
    steps = tuple(
        {
            "step_id": _step_id(path, seen),
            "action": "edit",
            "target_path": path,
        }
        for path in targets
    )
    identity = build_coding_project_identity(
        f"{turn_id}-ident",
        turn_id,
        project_id=project_id,
        revision_selector=revision_selector,
    )
    isolation = build_coding_project_isolation_admission(
        f"{turn_id}-iso",
        turn_id,
        project_root="/coding/project",
        destination=targets[0],
    )
    plan = build_coding_implementation_plan(f"{turn_id}-plan", turn_id, list(steps))
    admission = build_coding_upload_modification_admission(
        f"{turn_id}-modify",
        turn_id,
        identity=identity,
        inspect_report=inspect_report,
        isolation=isolation,
        plan=plan,
    )
    if admission.admission is CodingUploadModificationAdmissionState.ADMITTED:
        return CodingUploadModificationObserveV1(
            CodingUploadModificationObserveState.ADMITTED,
            CodingUploadModificationObserveReason.ADMITTED,
            admission,
            False,
            False,
        )
    if admission.admission is CodingUploadModificationAdmissionState.EMPTY:
        return CodingUploadModificationObserveV1(
            CodingUploadModificationObserveState.EMPTY,
            CodingUploadModificationObserveReason.NO_UPLOAD,
            admission,
            False,
            False,
        )
    return CodingUploadModificationObserveV1(
        CodingUploadModificationObserveState.BLOCKED,
        CodingUploadModificationObserveReason.ADMISSION_NOT_GRANTED,
        admission,
        False,
        False,
    )
