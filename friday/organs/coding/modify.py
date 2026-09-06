"""Coding edit admission and bounded, explicit immutable-revision transformation.

The observer remains read-only. The preparer transforms in-memory source bytes;
the authenticated turn alone writes a new workspace through the shared writer.
Neither function executes source code or changes the selected parent revision.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from friday.orchestration.coding_archive_extract_admission import MAX_ARCHIVE_NESTING_DEPTH
from friday.orchestration.coding_implementation_plan import MAX_STEPS, build_coding_implementation_plan
from friday.orchestration.coding_inspect_report import (
    CodingInspectReportState,
    CodingInspectReportV1,
    build_coding_inspect_report,
)
from friday.orchestration.coding_mode_snapshot import (
    MAX_SNAPSHOT_MEMBERS,
    CodingModeSnapshotState,
    build_coding_mode_snapshot,
)
from friday.orchestration.coding_project_identity import build_coding_project_identity
from friday.orchestration.coding_project_isolation_admission import (
    build_coding_project_isolation_admission,
)
from friday.orchestration.coding_upload_modification_admission import (
    CodingUploadModificationAdmissionState,
    CodingUploadModificationAdmissionV1,
    build_coding_upload_modification_admission,
)
from friday.orchestration.operation_result_carrier import MAX_OPERATION_RESULT_ARCHIVE_BYTES
from friday.organs.coding.result_archive import is_exportable_source_path
from friday.organs.coding.revision import CodingSourceRevision
from friday.organs.coding.workspace_io import source_revision_sha256

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
    """Read-only admission by default; the turn may report an applied copy edit."""

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
    target_paths: tuple[str, ...] | None = None,
) -> CodingUploadModificationObserveV1:
    """Admit mapped-tree edits.  Never write, extract, or execute."""

    del workspace
    if creating:
        return _empty(turn_id, CodingUploadModificationObserveReason.NOT_MODIFY)
    if not members:
        return _empty(turn_id, CodingUploadModificationObserveReason.NO_UPLOAD)
    if not modify_requested(message, has_members=True):
        return _empty(turn_id, CodingUploadModificationObserveReason.NOT_MODIFY)
    targets = target_paths if target_paths is not None else _targets(message, members)
    if not targets:
        return _empty(turn_id, CodingUploadModificationObserveReason.NO_UPLOAD)
    seen: dict[str, int] = {}
    steps = tuple(
        {
            "step_id": f"edit_{index}" if target_paths is not None else _step_id(path, seen),
            "action": "edit",
            "target_path": path,
        }
        for index, path in enumerate(targets)
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


MAX_REVISION_EDIT_BYTES = 1024 * 1024


def _unique_edit_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields: dict[str, object] = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("duplicate edit field")
        fields[key] = value
    return fields


def prepare_revision_edit(
    source: CodingSourceRevision,
    payload: str,
) -> tuple[CodingSourceRevision, tuple[str, ...]]:
    """Prepare an all-or-nothing candidate, not a write or execute authorization.

    Input is an explicit bounded JSON object: replace/add map relative names to
    complete UTF-8 text; delete names existing members. The selected whole-source
    digest is the optimistic precondition. No fuzzy matches, partial application,
    shell, guessed files, generated patches or implicit latest revision are used.
    """

    if type(payload) is not str or len(payload) > MAX_REVISION_EDIT_BYTES:
        raise ValueError("edit payload exceeds its bound")
    if len(payload.encode("utf-8")) > MAX_REVISION_EDIT_BYTES:
        raise ValueError("edit payload exceeds its bound")
    fields = json.loads(payload, object_pairs_hook=_unique_edit_fields)
    if type(fields) is not dict or not fields or set(fields) - {"replace", "add", "delete"}:
        raise ValueError("invalid edit fields")
    replacements, additions, deletions = (
        fields.get("replace", {}),
        fields.get("add", {}),
        fields.get("delete", []),
    )
    if type(replacements) is not dict or type(additions) is not dict or type(deletions) is not list:
        raise ValueError("invalid edit operations")
    count = len(replacements) + len(additions) + len(deletions)
    if not 0 < count <= MAX_STEPS:
        raise ValueError("edit operation count exceeds its bound")
    targets = (*replacements, *additions, *deletions)
    if any(type(path) is not str for path in targets) or len(set(targets)) != count:
        raise ValueError("duplicate or invalid edit path")
    target_snapshot = build_coding_mode_snapshot(
        "edit.targets",
        "edit.targets",
        {path: "0" * 64 for path in targets},
    )
    if target_snapshot.snapshot is not CodingModeSnapshotState.SNAPSHOT or any(
        not is_exportable_source_path(path) or path.count("/") > MAX_ARCHIVE_NESTING_DEPTH for path in targets
    ):
        raise ValueError("edit path cannot be published")
    if not 0 < len(source.members) <= MAX_SNAPSHOT_MEMBERS:
        raise ValueError("invalid parent source inventory")
    before = dict(source.members)
    if len(before) != len(source.members) or any(type(body) is not bytes for body in before.values()):
        raise ValueError("invalid parent source members")
    digests = {path: hashlib.sha256(body).hexdigest() for path, body in before.items()}
    if source_revision_sha256(digests) != source.revision_sha256:
        raise ValueError("parent source digest mismatch")
    if any(path not in before for path in (*replacements, *deletions)) or any(
        path in before for path in additions
    ):
        raise ValueError("edit precondition mismatch")
    candidate = dict(before)
    for path in deletions:
        del candidate[path]
    for path, body in (*replacements.items(), *additions.items()):
        if type(body) is not str:
            raise ValueError("replacement must be complete UTF-8 text")
        candidate[path] = body.encode("utf-8")
    if candidate == before:
        raise ValueError("edit has no source changes")
    return prepare_source_revision(source.project_id, candidate), tuple(targets)


def prepare_source_revision(project_id: str, candidate: dict[str, bytes]) -> CodingSourceRevision:
    """Validate complete in-memory sources identically for creation and revision edits.

    This is validation, not filesystem, model or execution authority. Reuse the
    existing snapshot, export and inspection contracts for every member.
    """

    if type(candidate) is not dict or not 0 < len(candidate) <= MAX_SNAPSHOT_MEMBERS:
        raise ValueError("invalid candidate source inventory")
    if any(type(path) is not str or type(body) is not bytes for path, body in candidate.items()):
        raise ValueError("invalid candidate source members")
    if any(
        not is_exportable_source_path(path) or path.count("/") > MAX_ARCHIVE_NESTING_DEPTH
        for path in candidate
    ):
        raise ValueError("candidate path cannot be published")
    if sum(map(len, candidate.values())) > MAX_OPERATION_RESULT_ARCHIVE_BYTES:
        raise ValueError("candidate source bytes exceed the export bound")
    digests = {path: hashlib.sha256(body).hexdigest() for path, body in candidate.items()}
    snapshot = build_coding_mode_snapshot("edit.candidate", "edit.candidate", digests)
    if snapshot.snapshot is not CodingModeSnapshotState.SNAPSHOT:
        raise ValueError("candidate source inventory cannot be published")
    # A file cannot also be an ancestor directory, including case-fold aliases.
    folded = {path.casefold() for path in candidate}
    if any(
        "/".join(path.split("/")[:i]).casefold() in folded
        for path in candidate
        for i in range(1, len(path.split("/")))
    ):
        raise ValueError("candidate file and directory collide")
    inspected = build_coding_inspect_report(
        "edit.inspect",
        "edit.inspect",
        members=[
            {
                "relative_path": path,
                "size": len(body),
                "file_kind": "regular_file",
                "executable": False,
                "link_kind": "none",
            }
            for path, body in candidate.items()
        ],
    )
    if (
        inspected.report is not CodingInspectReportState.INSPECTED
        or inspected.hazards is None
        or inspected.hazards.hazards.value != "clear"
    ):
        raise ValueError("candidate source inspection blocked")
    return CodingSourceRevision(project_id, source_revision_sha256(digests), tuple(sorted(candidate.items())))
