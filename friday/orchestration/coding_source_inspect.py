"""Pure static inspection summary for an observed coding source tree."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.orchestration.coding_source_member import CodingSourceFileKind, CodingSourceMemberV1
from friday.orchestration.coding_source_tree import (
    CodingSourceTreeState,
    CodingSourceTreeV1,
    build_coding_source_tree,
)


class CodingSourceInspectError(ValueError):
    """A static-inspection fact is malformed."""


class CodingSourceInspectState(StrEnum):
    """Closed static-inspection outcomes."""

    EMPTY = "empty"
    INSPECTED = "inspected"
    BLOCKED = "blocked"


class CodingSourceInspectReason(StrEnum):
    """Closed reasons for one static-inspection summary."""

    NO_MEMBERS = "no_members"
    INSPECTION_COMPLETE = "inspection_complete"
    TREE_BLOCKED = "tree_blocked"
    INVALID_FACTS = "invalid_facts"


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise CodingSourceInspectError(f"{field} is invalid")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise CodingSourceInspectError(f"{field} is invalid")
    return value


def _state(value: object) -> CodingSourceInspectState:
    if isinstance(value, CodingSourceInspectState):
        return value
    if type(value) is not str:
        raise CodingSourceInspectError("inspection must be a closed value")
    try:
        return CodingSourceInspectState(value.strip().casefold())
    except ValueError as exc:
        raise CodingSourceInspectError("unknown inspection value") from exc


def _reason(value: object) -> CodingSourceInspectReason:
    if isinstance(value, CodingSourceInspectReason):
        return value
    if type(value) is not str:
        raise CodingSourceInspectError("reason must be a closed value")
    try:
        return CodingSourceInspectReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingSourceInspectError("unknown inspection reason") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= 4_096:
        raise CodingSourceInspectError(f"{field} is outside its closed bound")
    return value


@dataclass(frozen=True, slots=True)
class CodingSourceInspectV1:
    """Immutable summary of metadata inspection; no command was executed."""

    inspect_id: str
    authenticated_turn_id: str
    inspection: CodingSourceInspectState
    member_count: int
    file_count: int
    directory_count: int
    executable_member_count: int
    reason: CodingSourceInspectReason

    @property
    def state(self) -> CodingSourceInspectState:
        return self.inspection

    @property
    def closed_inspection(self) -> CodingSourceInspectState:
        return self.inspection

    @property
    def decision(self) -> CodingSourceInspectState:
        return self.inspection

    @property
    def closed_reason(self) -> CodingSourceInspectReason:
        return self.reason

    @property
    def execution_attempted(self) -> bool:
        return False

    @property
    def rebuild_attempted(self) -> bool:
        return False

    @property
    def executed(self) -> bool:
        return False

    @property
    def rebuilt(self) -> bool:
        return False

    def __post_init__(self) -> None:
        _identifier(self.inspect_id, field="inspect_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        inspection = _state(self.inspection)
        reason = _reason(self.reason)
        object.__setattr__(self, "inspection", inspection)
        object.__setattr__(self, "reason", reason)
        member_count = _count(self.member_count, field="member_count")
        file_count = _count(self.file_count, field="file_count")
        directory_count = _count(self.directory_count, field="directory_count")
        executable_count = _count(self.executable_member_count, field="executable_member_count")
        if file_count + directory_count != member_count:
            raise CodingSourceInspectError("file and directory counts disagree")
        if executable_count > file_count:
            raise CodingSourceInspectError("executable count exceeds file count")
        if inspection in {
            CodingSourceInspectState.EMPTY,
            CodingSourceInspectState.BLOCKED,
        } and any((member_count, file_count, directory_count, executable_count)):
            raise CodingSourceInspectError("empty and blocked inspections expose no counts")
        if inspection is CodingSourceInspectState.INSPECTED and member_count == 0:
            raise CodingSourceInspectError("inspected result needs members")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "inspect_id": self.inspect_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "inspection": self.inspection.value,
            "member_count": self.member_count,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "executable_member_count": self.executable_member_count,
            "reason": self.reason.value,
        }


SourceInspectState = CodingSourceInspectState
SourceInspectReason = CodingSourceInspectReason
CodingSourceInspect = CodingSourceInspectV1
CodingSourceInspectDecision = CodingSourceInspectState


def _tree(
    inspect_id: str,
    authenticated_turn_id: str,
    value: object,
) -> CodingSourceTreeV1:
    if isinstance(value, CodingSourceTreeV1):
        if value.authenticated_turn_id != authenticated_turn_id:
            raise CodingSourceInspectError("tree turn identity disagrees")
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if "members" in value or "source_members" in value or "member_facts" in value:
            return build_coding_source_tree(inspect_id, authenticated_turn_id, value)
        raise CodingSourceInspectError("tree mapping has no members")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return build_coding_source_tree(inspect_id, authenticated_turn_id, value)
    raise CodingSourceInspectError("tree must be a source tree or member sequence")


def _result(
    inspect_id: str,
    authenticated_turn_id: str,
    inspection: CodingSourceInspectState,
    reason: CodingSourceInspectReason,
    *,
    members: int = 0,
    files: int = 0,
    directories: int = 0,
    executable: int = 0,
) -> CodingSourceInspectV1:
    return CodingSourceInspectV1(
        inspect_id=inspect_id,
        authenticated_turn_id=authenticated_turn_id,
        inspection=inspection,
        member_count=members if inspection is CodingSourceInspectState.INSPECTED else 0,
        file_count=files if inspection is CodingSourceInspectState.INSPECTED else 0,
        directory_count=directories if inspection is CodingSourceInspectState.INSPECTED else 0,
        executable_member_count=executable if inspection is CodingSourceInspectState.INSPECTED else 0,
        reason=reason,
    )


def build_coding_source_inspect(
    inspect_id: str,
    authenticated_turn_id: str,
    source_tree: CodingSourceTreeV1 | Mapping[str, object] | Sequence[object] | None = None,
    *,
    members: Sequence[CodingSourceMemberV1 | Mapping[str, object]] | None = None,
) -> CodingSourceInspectV1:
    """Inspect member metadata only; executable bits are recorded, never run."""

    inspect_key = _identifier(inspect_id, field="inspect_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if source_tree is not None and members is not None:
            raise CodingSourceInspectError("source_tree and members cannot both be supplied")
        tree = _tree(
            inspect_key,
            turn_key,
            members if members is not None else (source_tree if source_tree is not None else ()),
        )
    except CodingSourceInspectError:
        return _result(
            inspect_key,
            turn_key,
            CodingSourceInspectState.BLOCKED,
            CodingSourceInspectReason.INVALID_FACTS,
        )
    except (TypeError, ValueError):
        return _result(
            inspect_key,
            turn_key,
            CodingSourceInspectState.BLOCKED,
            CodingSourceInspectReason.INVALID_FACTS,
        )
    if tree.tree is CodingSourceTreeState.BLOCKED:
        return _result(
            inspect_key,
            turn_key,
            CodingSourceInspectState.BLOCKED,
            CodingSourceInspectReason.TREE_BLOCKED,
        )
    if tree.tree is CodingSourceTreeState.EMPTY:
        return _result(
            inspect_key,
            turn_key,
            CodingSourceInspectState.EMPTY,
            CodingSourceInspectReason.NO_MEMBERS,
        )
    files = sum(member.file_kind is CodingSourceFileKind.REGULAR_FILE for member in tree.members)
    directories = sum(member.file_kind is CodingSourceFileKind.DIRECTORY for member in tree.members)
    executable = sum(member.executable for member in tree.members)
    return _result(
        inspect_key,
        turn_key,
        CodingSourceInspectState.INSPECTED,
        CodingSourceInspectReason.INSPECTION_COMPLETE,
        members=tree.member_count,
        files=files,
        directories=directories,
        executable=executable,
    )


inspect_coding_source = build_coding_source_inspect
build_source_inspect = build_coding_source_inspect


__all__ = (
    "CodingSourceInspect",
    "CodingSourceInspectDecision",
    "CodingSourceInspectError",
    "CodingSourceInspectReason",
    "CodingSourceInspectState",
    "CodingSourceInspectV1",
    "SourceInspectReason",
    "SourceInspectState",
    "build_coding_source_inspect",
    "build_source_inspect",
    "inspect_coding_source",
)
