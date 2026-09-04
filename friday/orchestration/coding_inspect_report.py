"""Frozen composition of the bounded Coding Mode inspection contracts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.orchestration.coding_inspect_hazards import (
    CodingInspectHazardsState,
    CodingInspectHazardsV1,
    build_coding_inspect_hazards,
)
from friday.orchestration.coding_source_inspect import (
    CodingSourceInspectState,
    CodingSourceInspectV1,
    build_coding_source_inspect,
)
from friday.orchestration.coding_source_member import CodingSourceMemberV1
from friday.orchestration.coding_source_tree import (
    CodingSourceTreeState,
    CodingSourceTreeV1,
    build_coding_source_tree,
)
from friday.orchestration.coding_toolchain_hint import (
    CodingToolchainHintState,
    CodingToolchainHintV1,
    build_coding_toolchain_hint,
)


class CodingInspectReportError(ValueError):
    """An inspection report component or identity is malformed."""


class CodingInspectReportState(StrEnum):
    """Closed composed-report outcomes."""

    EMPTY = "empty"
    INSPECTED = "inspected"
    BLOCKED = "blocked"


class CodingInspectReportReason(StrEnum):
    """Closed reasons for one composed inspection report."""

    NO_MEMBERS = "no_members"
    REPORT_COMPOSED = "report_composed"
    COMPONENT_BLOCKED = "component_blocked"
    IDENTITY_MISMATCH = "identity_mismatch"
    COMPONENT_INCONSISTENT = "component_inconsistent"
    INVALID_FACTS = "invalid_facts"


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise CodingInspectReportError(f"{field} is invalid")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise CodingInspectReportError(f"{field} is invalid")
    return value


def _state(value: object) -> CodingInspectReportState:
    if isinstance(value, CodingInspectReportState):
        return value
    if type(value) is not str:
        raise CodingInspectReportError("report must be a closed value")
    try:
        return CodingInspectReportState(value.strip().casefold())
    except ValueError as exc:
        raise CodingInspectReportError("unknown report value") from exc


def _reason(value: object) -> CodingInspectReportReason:
    if isinstance(value, CodingInspectReportReason):
        return value
    if type(value) is not str:
        raise CodingInspectReportError("reason must be a closed value")
    try:
        return CodingInspectReportReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingInspectReportError("unknown report reason") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= 4_096:
        raise CodingInspectReportError(f"{field} is outside its closed bound")
    return value


@dataclass(frozen=True, slots=True)
class CodingInspectReportV1:
    """Immutable composition of tree, inspect, hazards, and toolchain hint."""

    report_id: str
    authenticated_turn_id: str
    report: CodingInspectReportState
    tree: CodingSourceTreeV1 | None
    inspection: CodingSourceInspectV1 | None
    hazards: CodingInspectHazardsV1 | None
    toolchain_hint: CodingToolchainHintV1 | None
    member_count: int
    reason: CodingInspectReportReason

    @property
    def state(self) -> CodingInspectReportState:
        return self.report

    @property
    def closed_report(self) -> CodingInspectReportState:
        return self.report

    @property
    def decision(self) -> CodingInspectReportState:
        return self.report

    @property
    def closed_reason(self) -> CodingInspectReportReason:
        return self.reason

    @property
    def inspect(self) -> CodingSourceInspectV1 | None:
        return self.inspection

    @property
    def hint(self) -> CodingToolchainHintV1 | None:
        return self.toolchain_hint

    def __post_init__(self) -> None:
        _identifier(self.report_id, field="report_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        report = _state(self.report)
        reason = _reason(self.reason)
        object.__setattr__(self, "report", report)
        object.__setattr__(self, "reason", reason)
        member_count = _count(self.member_count, field="member_count")
        components = (self.tree, self.inspection, self.hazards, self.toolchain_hint)
        if report is CodingInspectReportState.BLOCKED:
            if member_count:
                raise CodingInspectReportError("blocked report cannot expose member count")
            return
        if any(component is None for component in components):
            raise CodingInspectReportError("non-blocked report needs all components")
        assert self.tree is not None
        assert self.inspection is not None
        assert self.hazards is not None
        assert self.toolchain_hint is not None
        if any(
            component.authenticated_turn_id != self.authenticated_turn_id
            for component in components
            if component is not None
        ):
            raise CodingInspectReportError("component identity disagrees")
        if report is CodingInspectReportState.EMPTY:
            if member_count or self.tree.member_count:
                raise CodingInspectReportError("empty report cannot expose members")
            if any(
                component.state
                not in {
                    CodingSourceTreeState.EMPTY,
                    CodingSourceInspectState.EMPTY,
                    CodingInspectHazardsState.EMPTY,
                    CodingToolchainHintState.EMPTY,
                }
                for component in components
            ):
                raise CodingInspectReportError("empty report has non-empty component")
        if report is CodingInspectReportState.INSPECTED:
            if member_count == 0 or self.tree.tree is not CodingSourceTreeState.MAPPED:
                raise CodingInspectReportError("inspected report needs a mapped tree")
            if self.inspection.member_count != member_count:
                raise CodingInspectReportError("inspection count disagrees")
            if self.hazards.member_count != member_count:
                raise CodingInspectReportError("hazard count disagrees")
            if self.toolchain_hint.member_count != member_count:
                raise CodingInspectReportError("hint count disagrees")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "report": self.report.value,
            "tree": self.tree.to_mapping() if self.tree is not None else None,
            "inspection": self.inspection.to_mapping() if self.inspection is not None else None,
            "hazards": self.hazards.to_mapping() if self.hazards is not None else None,
            "toolchain_hint": self.toolchain_hint.to_mapping() if self.toolchain_hint is not None else None,
            "member_count": self.member_count,
            "reason": self.reason.value,
        }


InspectReportState = CodingInspectReportState
InspectReportReason = CodingInspectReportReason
CodingInspectReport = CodingInspectReportV1
CodingInspectReportDecision = CodingInspectReportState


def _component_tree(
    report_id: str,
    authenticated_turn_id: str,
    value: object,
) -> CodingSourceTreeV1:
    if isinstance(value, CodingSourceTreeV1):
        if value.authenticated_turn_id != authenticated_turn_id:
            raise CodingInspectReportError("tree identity disagrees")
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        return build_coding_source_tree(report_id, authenticated_turn_id, value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return build_coding_source_tree(report_id, authenticated_turn_id, value)
    raise CodingInspectReportError("tree is invalid")


def _build_components(
    report_id: str,
    authenticated_turn_id: str,
    tree: CodingSourceTreeV1,
) -> tuple[CodingSourceInspectV1, CodingInspectHazardsV1, CodingToolchainHintV1]:
    return (
        build_coding_source_inspect(report_id, authenticated_turn_id, tree),
        build_coding_inspect_hazards(report_id, authenticated_turn_id, tree),
        build_coding_toolchain_hint(report_id, authenticated_turn_id, tree),
    )


def _inspection_component(
    report_id: str,
    authenticated_turn_id: str,
    value: object,
) -> CodingSourceInspectV1:
    if isinstance(value, CodingSourceInspectV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        raise CodingInspectReportError("inspection component is invalid")
    return CodingSourceInspectV1(
        inspect_id=value.get("inspect_id", report_id),
        authenticated_turn_id=value.get("authenticated_turn_id", authenticated_turn_id),
        inspection=value.get("inspection", value.get("state")),
        member_count=value.get("member_count"),
        file_count=value.get("file_count"),
        directory_count=value.get("directory_count"),
        executable_member_count=value.get("executable_member_count", 0),
        reason=value.get("reason"),
    )


def _hazards_component(
    report_id: str,
    authenticated_turn_id: str,
    value: object,
) -> CodingInspectHazardsV1:
    if isinstance(value, CodingInspectHazardsV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        raise CodingInspectReportError("hazards component is invalid")
    hazard_kinds = value.get("hazard_kinds", ())
    if isinstance(hazard_kinds, (str, bytes, bytearray)) or not isinstance(hazard_kinds, Sequence):
        raise CodingInspectReportError("hazard_kinds component is invalid")
    return CodingInspectHazardsV1(
        hazard_id=value.get("hazard_id", report_id),
        authenticated_turn_id=value.get("authenticated_turn_id", authenticated_turn_id),
        hazards=value.get("hazards", value.get("state")),
        member_count=value.get("member_count"),
        hazard_count=value.get("hazard_count"),
        secret_name_count=value.get("secret_name_count"),
        executable_member_count=value.get("executable_member_count"),
        nested_vcs_dir_count=value.get("nested_vcs_dir_count"),
        hazard_kinds=tuple(hazard_kinds),
        reason=value.get("reason"),
    )


def _hint_component(
    report_id: str,
    authenticated_turn_id: str,
    value: object,
) -> CodingToolchainHintV1:
    if isinstance(value, CodingToolchainHintV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        raise CodingInspectReportError("toolchain hint component is invalid")
    suffixes = value.get("detected_suffixes", value.get("suffixes", ()))
    languages = value.get("language_hints", value.get("languages", ()))
    if isinstance(suffixes, (str, bytes, bytearray)) or not isinstance(suffixes, Sequence):
        raise CodingInspectReportError("detected suffixes component is invalid")
    if isinstance(languages, (str, bytes, bytearray)) or not isinstance(languages, Sequence):
        raise CodingInspectReportError("language hints component is invalid")
    return CodingToolchainHintV1(
        hint_id=value.get("hint_id", report_id),
        authenticated_turn_id=value.get("authenticated_turn_id", authenticated_turn_id),
        hint=value.get("hint", value.get("state")),
        member_count=value.get("member_count"),
        detected_suffixes=tuple(suffixes),
        language_hints=tuple(languages),
        reason=value.get("reason"),
    )


def _result(
    report_id: str,
    authenticated_turn_id: str,
    report: CodingInspectReportState,
    reason: CodingInspectReportReason,
    *,
    tree: CodingSourceTreeV1 | None = None,
    inspection: CodingSourceInspectV1 | None = None,
    hazards: CodingInspectHazardsV1 | None = None,
    toolchain_hint: CodingToolchainHintV1 | None = None,
    members: int = 0,
) -> CodingInspectReportV1:
    return CodingInspectReportV1(
        report_id=report_id,
        authenticated_turn_id=authenticated_turn_id,
        report=report,
        tree=tree,
        inspection=inspection,
        hazards=hazards,
        toolchain_hint=toolchain_hint,
        member_count=members if report is CodingInspectReportState.INSPECTED else 0,
        reason=reason,
    )


def build_coding_inspect_report(
    report_id: str,
    authenticated_turn_id: str,
    source_tree: CodingSourceTreeV1 | Mapping[str, object] | Sequence[object] | None = None,
    inspection: CodingSourceInspectV1 | None = None,
    hazards: CodingInspectHazardsV1 | None = None,
    toolchain_hint: CodingToolchainHintV1 | None = None,
    *,
    members: Sequence[CodingSourceMemberV1 | Mapping[str, object]] | None = None,
) -> CodingInspectReportV1:
    """Compose the four bounded inspection contracts without executing code."""

    report_key = _identifier(report_id, field="report_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if source_tree is not None and members is not None:
            raise CodingInspectReportError("source_tree and members cannot both be supplied")
        tree = _component_tree(
            report_key,
            turn_key,
            members if members is not None else (source_tree if source_tree is not None else ()),
        )
        built_inspection, built_hazards, built_hint = _build_components(report_key, turn_key, tree)
    except (CodingInspectReportError, TypeError, ValueError):
        return _result(
            report_key,
            turn_key,
            CodingInspectReportState.BLOCKED,
            CodingInspectReportReason.INVALID_FACTS,
        )

    try:
        inspection = (
            _inspection_component(report_key, turn_key, inspection)
            if inspection is not None
            else built_inspection
        )
        hazards = _hazards_component(report_key, turn_key, hazards) if hazards is not None else built_hazards
        toolchain_hint = (
            _hint_component(report_key, turn_key, toolchain_hint)
            if toolchain_hint is not None
            else built_hint
        )
    except (CodingInspectReportError, TypeError, ValueError):
        return _result(
            report_key,
            turn_key,
            CodingInspectReportState.BLOCKED,
            CodingInspectReportReason.INVALID_FACTS,
        )
    components = (tree, inspection, hazards, toolchain_hint)
    if any(component.authenticated_turn_id != turn_key for component in components):
        return _result(
            report_key,
            turn_key,
            CodingInspectReportState.BLOCKED,
            CodingInspectReportReason.IDENTITY_MISMATCH,
        )
    if (
        tree.tree is CodingSourceTreeState.BLOCKED
        or inspection.inspection is CodingSourceInspectState.BLOCKED
        or hazards.hazards is CodingInspectHazardsState.BLOCKED
        or toolchain_hint.hint is CodingToolchainHintState.BLOCKED
    ):
        return _result(
            report_key,
            turn_key,
            CodingInspectReportState.BLOCKED,
            CodingInspectReportReason.COMPONENT_BLOCKED,
        )
    if tree.tree is CodingSourceTreeState.EMPTY:
        if any(
            component.state
            not in {
                CodingSourceTreeState.EMPTY,
                CodingSourceInspectState.EMPTY,
                CodingInspectHazardsState.EMPTY,
                CodingToolchainHintState.EMPTY,
            }
            for component in components
        ):
            return _result(
                report_key,
                turn_key,
                CodingInspectReportState.BLOCKED,
                CodingInspectReportReason.COMPONENT_INCONSISTENT,
            )
        return _result(
            report_key,
            turn_key,
            CodingInspectReportState.EMPTY,
            CodingInspectReportReason.NO_MEMBERS,
            tree=tree,
            inspection=inspection,
            hazards=hazards,
            toolchain_hint=toolchain_hint,
        )
    if (
        tree.tree is not CodingSourceTreeState.MAPPED
        or inspection.inspection is not CodingSourceInspectState.INSPECTED
        or hazards.hazards
        not in {
            CodingInspectHazardsState.CLEAR,
            CodingInspectHazardsState.PRESENT,
        }
        or inspection.member_count != tree.member_count
        or hazards.member_count != tree.member_count
        or toolchain_hint.member_count != tree.member_count
    ):
        return _result(
            report_key,
            turn_key,
            CodingInspectReportState.BLOCKED,
            CodingInspectReportReason.COMPONENT_INCONSISTENT,
        )
    return _result(
        report_key,
        turn_key,
        CodingInspectReportState.INSPECTED,
        CodingInspectReportReason.REPORT_COMPOSED,
        tree=tree,
        inspection=inspection,
        hazards=hazards,
        toolchain_hint=toolchain_hint,
        members=tree.member_count,
    )


compose_coding_inspect_report = build_coding_inspect_report
build_inspect_report = build_coding_inspect_report


__all__ = (
    "CodingInspectReport",
    "CodingInspectReportDecision",
    "CodingInspectReportError",
    "CodingInspectReportReason",
    "CodingInspectReportState",
    "CodingInspectReportV1",
    "InspectReportReason",
    "InspectReportState",
    "build_coding_inspect_report",
    "build_inspect_report",
    "compose_coding_inspect_report",
)
