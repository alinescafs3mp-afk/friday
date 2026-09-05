from __future__ import annotations

from pathlib import Path

from friday.orchestration.coding_inspect_report import (
    CodingInspectReportState,
    build_coding_inspect_report,
)
from friday.orchestration.coding_upload_modification_admission import (
    CodingUploadModificationAdmissionState,
)
from friday.organs.coding.modify import (
    CodingUploadModificationObserveReason,
    CodingUploadModificationObserveState,
    modify_requested,
    observe_coding_upload_modification,
)

TURN = "coding-turn-1"
PROJECT = "coding-p-1"
REVISION = "a" * 64
MEMBERS = (
    {
        "relative_path": "main.py",
        "size": 12,
        "file_kind": "regular_file",
        "executable": False,
        "link_kind": "none",
    },
)


def _report(members=MEMBERS):
    return build_coding_inspect_report("report-1", TURN, members=members)


def test_modify_requested_needs_members_and_edit_language() -> None:
    assert modify_requested("измени main.py", has_members=True) is True
    assert modify_requested("edit main.py", has_members=True) is True
    assert modify_requested("измени main.py", has_members=False) is False
    assert modify_requested("осмотри main.py", has_members=True) is False


def test_inspect_only_stays_empty(tmp_path: Path) -> None:
    result = observe_coding_upload_modification(
        turn_id=TURN,
        project_id=PROJECT,
        revision_selector=REVISION,
        message="осмотри main.py",
        workspace=tmp_path,
        inspect_report=_report(),
        members=MEMBERS,
        creating=False,
    )
    assert result.state is CodingUploadModificationObserveState.EMPTY
    assert result.reason is CodingUploadModificationObserveReason.NOT_MODIFY
    assert result.applied is False
    assert result.untrusted_execute is False
    assert result.admission.admission is CodingUploadModificationAdmissionState.EMPTY
    assert list(tmp_path.iterdir()) == []


def test_create_turn_does_not_observe_modify(tmp_path: Path) -> None:
    result = observe_coding_upload_modification(
        turn_id=TURN,
        project_id=PROJECT,
        revision_selector=REVISION,
        message="создай проект и измени main.py",
        workspace=tmp_path,
        inspect_report=_report(),
        members=MEMBERS,
        creating=True,
    )
    assert result.state is CodingUploadModificationObserveState.EMPTY
    assert result.applied is False


def test_mapped_tree_edit_is_admitted_and_not_applied(tmp_path: Path) -> None:
    marker = tmp_path / "main.py"
    marker.write_text("print(1)\n", encoding="utf-8")
    report = _report()
    assert report.report is CodingInspectReportState.INSPECTED
    result = observe_coding_upload_modification(
        turn_id=TURN,
        project_id=PROJECT,
        revision_selector=REVISION,
        message="измени main.py: добавь docstring",
        workspace=tmp_path,
        inspect_report=report,
        members=MEMBERS,
        creating=False,
    )
    assert result.state is CodingUploadModificationObserveState.ADMITTED
    assert result.reason is CodingUploadModificationObserveReason.ADMITTED
    assert result.applied is False
    assert result.untrusted_execute is False
    assert result.admission.admission is CodingUploadModificationAdmissionState.ADMITTED
    assert marker.read_text(encoding="utf-8") == "print(1)\n"


def test_secret_member_blocks_without_writing(tmp_path: Path) -> None:
    members = (
        {
            "relative_path": ".env",
            "size": 4,
            "file_kind": "regular_file",
            "executable": False,
            "link_kind": "none",
        },
    )
    marker = tmp_path / ".env"
    marker.write_text("x=1\n", encoding="utf-8")
    result = observe_coding_upload_modification(
        turn_id=TURN,
        project_id=PROJECT,
        revision_selector=REVISION,
        message="измени .env",
        workspace=tmp_path,
        inspect_report=_report(members),
        members=members,
        creating=False,
    )
    assert result.state is CodingUploadModificationObserveState.BLOCKED
    assert result.applied is False
    assert marker.read_text(encoding="utf-8") == "x=1\n"


def test_empty_members_stay_empty(tmp_path: Path) -> None:
    result = observe_coding_upload_modification(
        turn_id=TURN,
        project_id=PROJECT,
        revision_selector=REVISION,
        message="измени main.py",
        workspace=tmp_path,
        inspect_report=build_coding_inspect_report("report-1", TURN),
        members=(),
        creating=False,
    )
    assert result.state is CodingUploadModificationObserveState.EMPTY
    assert result.reason is CodingUploadModificationObserveReason.NO_UPLOAD
    assert result.applied is False
