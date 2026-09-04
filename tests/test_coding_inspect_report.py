from __future__ import annotations

import pytest

from friday.orchestration.coding_inspect_hazards import (
    CodingInspectHazardsReason,
    CodingInspectHazardsState,
    CodingInspectHazardsV1,
    build_coding_inspect_hazards,
)
from friday.orchestration.coding_inspect_report import (
    CodingInspectReportError,
    CodingInspectReportReason,
    CodingInspectReportState,
    CodingInspectReportV1,
    build_coding_inspect_report,
)
from friday.orchestration.coding_source_inspect import build_coding_source_inspect
from friday.orchestration.coding_source_member import CodingSourceMemberV1
from friday.orchestration.coding_source_tree import build_coding_source_tree
from friday.orchestration.coding_toolchain_hint import build_coding_toolchain_hint


def member(path: str, *, executable: bool = False) -> CodingSourceMemberV1:
    return CodingSourceMemberV1(path, 1, "file", executable, "none")


def test_empty_components_compose_to_empty_report() -> None:
    result = build_coding_inspect_report("report-1", "turn-1", members=())
    assert result.report is CodingInspectReportState.EMPTY
    assert result.member_count == 0
    assert result.tree is not None and result.tree.member_count == 0
    assert result.reason is CodingInspectReportReason.NO_MEMBERS


def test_report_composes_tree_inspect_hazards_and_hint() -> None:
    result = build_coding_inspect_report("report-1", "turn-1", members=(member("main.py"),))
    assert result.report is CodingInspectReportState.INSPECTED
    assert result.member_count == 1
    assert result.tree is not None and result.tree.tree.value == "mapped"
    assert result.inspection is not None and result.inspection.member_count == 1
    assert result.hazards is not None and result.hazards.hazards.value == "clear"
    assert result.toolchain_hint is not None and result.toolchain_hint.hint.value == "hinted"
    assert result.reason is CodingInspectReportReason.REPORT_COMPOSED
    with pytest.raises(AttributeError):
        result.member_count = 2  # type: ignore[misc]


def test_report_composes_prebuilt_components() -> None:
    tree = build_coding_source_tree("tree-1", "turn-1", (member("main.py"),))
    inspection = build_coding_source_inspect("inspect-1", "turn-1", tree)
    hazards = build_coding_inspect_hazards("hazard-1", "turn-1", tree)
    hint = build_coding_toolchain_hint("hint-1", "turn-1", tree)
    result = build_coding_inspect_report("report-1", "turn-1", tree, inspection, hazards, hint)
    assert result.report is CodingInspectReportState.INSPECTED
    assert result.tree is tree
    assert result.inspection is inspection
    assert result.hazards is hazards
    assert result.toolchain_hint is hint


def test_report_accepts_serialized_component_mappings() -> None:
    original = build_coding_inspect_report("report-1", "turn-1", members=(member("main.py"),))
    assert original.tree is not None
    assert original.inspection is not None
    assert original.hazards is not None
    assert original.toolchain_hint is not None
    result = build_coding_inspect_report(
        "report-2",
        "turn-1",
        original.tree.to_mapping(),
        original.inspection.to_mapping(),
        original.hazards.to_mapping(),
        original.toolchain_hint.to_mapping(),
    )
    assert result.report is CodingInspectReportState.INSPECTED


def test_any_blocked_component_blocks_report_without_member_count() -> None:
    tree = build_coding_source_tree("tree-1", "turn-1", (member("main.py"),))
    blocked_hazards = CodingInspectHazardsV1(
        "hazard-1",
        "turn-1",
        CodingInspectHazardsState.BLOCKED,
        0,
        0,
        0,
        0,
        0,
        (),
        CodingInspectHazardsReason.TREE_BLOCKED,
    )
    result = build_coding_inspect_report("report-1", "turn-1", tree, hazards=blocked_hazards)
    assert result.report is CodingInspectReportState.BLOCKED
    assert result.member_count == 0
    assert result.reason is CodingInspectReportReason.COMPONENT_BLOCKED


def test_invalid_members_and_identity_mismatch_block_report() -> None:
    invalid = build_coding_inspect_report("report-1", "turn-1", members=({"path": "../escape"},))
    tree = build_coding_source_tree("tree-1", "turn-1", (member("main.py"),))
    mismatch = build_coding_inspect_report(
        "report-2",
        "turn-1",
        tree,
        build_coding_source_inspect("inspect-2", "turn-2", tree),
    )
    assert invalid.report is CodingInspectReportState.BLOCKED
    assert invalid.reason is CodingInspectReportReason.COMPONENT_BLOCKED
    assert mismatch.report is CodingInspectReportState.BLOCKED
    assert mismatch.reason is CodingInspectReportReason.IDENTITY_MISMATCH


def test_direct_invalid_report_fields_raise() -> None:
    with pytest.raises(CodingInspectReportError):
        CodingInspectReportV1(
            "report-1",
            "turn-1",
            CodingInspectReportState.INSPECTED,
            None,
            None,
            None,
            None,
            0,
            CodingInspectReportReason.REPORT_COMPOSED,
        )
