"""Pure name-and-metadata hazard scan for an observed coding source tree."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.orchestration.coding_source_member import CodingSourceMemberV1
from friday.orchestration.coding_source_tree import (
    CodingSourceTreeState,
    CodingSourceTreeV1,
    build_coding_source_tree,
)


class CodingInspectHazardsError(ValueError):
    """Hazard input facts are malformed."""


class CodingInspectHazardsState(StrEnum):
    """Closed static-inspection hazard outcomes."""

    EMPTY = "empty"
    CLEAR = "clear"
    PRESENT = "present"
    BLOCKED = "blocked"


class CodingInspectHazardKind(StrEnum):
    """Closed hazard categories derived without reading file bodies."""

    SECRET_LOOKING_NAME = "secret_looking_name"
    EXECUTABLE_BIT = "executable_bit"
    NESTED_VCS_DIRECTORY = "nested_vcs_directory"


class CodingInspectHazardsReason(StrEnum):
    """Closed reasons for one hazard scan."""

    NO_MEMBERS = "no_members"
    NO_HAZARDS = "no_hazards"
    HAZARDS_PRESENT = "hazards_present"
    TREE_BLOCKED = "tree_blocked"
    INVALID_FACTS = "invalid_facts"


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise CodingInspectHazardsError(f"{field} is invalid")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise CodingInspectHazardsError(f"{field} is invalid")
    return value


def _state(value: object) -> CodingInspectHazardsState:
    if isinstance(value, CodingInspectHazardsState):
        return value
    if type(value) is not str:
        raise CodingInspectHazardsError("hazards must be a closed value")
    try:
        return CodingInspectHazardsState(value.strip().casefold())
    except ValueError as exc:
        raise CodingInspectHazardsError("unknown hazards value") from exc


def _kind(value: object) -> CodingInspectHazardKind:
    if isinstance(value, CodingInspectHazardKind):
        return value
    if type(value) is not str:
        raise CodingInspectHazardsError("hazard kind must be closed")
    try:
        return CodingInspectHazardKind(value.strip().casefold())
    except ValueError as exc:
        raise CodingInspectHazardsError("unknown hazard kind") from exc


def _reason(value: object) -> CodingInspectHazardsReason:
    if isinstance(value, CodingInspectHazardsReason):
        return value
    if type(value) is not str:
        raise CodingInspectHazardsError("reason must be a closed value")
    try:
        return CodingInspectHazardsReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingInspectHazardsError("unknown hazards reason") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= 4_096:
        raise CodingInspectHazardsError(f"{field} is outside its closed bound")
    return value


@dataclass(frozen=True, slots=True)
class CodingInspectHazardsV1:
    """Immutable body-free hazards found in source member metadata."""

    hazard_id: str
    authenticated_turn_id: str
    hazards: CodingInspectHazardsState
    member_count: int
    hazard_count: int
    secret_name_count: int
    executable_member_count: int
    nested_vcs_dir_count: int
    hazard_kinds: tuple[CodingInspectHazardKind, ...]
    reason: CodingInspectHazardsReason

    @property
    def state(self) -> CodingInspectHazardsState:
        return self.hazards

    @property
    def closed_hazards(self) -> CodingInspectHazardsState:
        return self.hazards

    @property
    def decision(self) -> CodingInspectHazardsState:
        return self.hazards

    @property
    def closed_reason(self) -> CodingInspectHazardsReason:
        return self.reason

    def __post_init__(self) -> None:
        _identifier(self.hazard_id, field="hazard_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        hazards = _state(self.hazards)
        reason = _reason(self.reason)
        object.__setattr__(self, "hazards", hazards)
        object.__setattr__(self, "reason", reason)
        member_count = _count(self.member_count, field="member_count")
        hazard_count = _count(self.hazard_count, field="hazard_count")
        secret_count = _count(self.secret_name_count, field="secret_name_count")
        executable_count = _count(self.executable_member_count, field="executable_member_count")
        vcs_count = _count(self.nested_vcs_dir_count, field="nested_vcs_dir_count")
        if type(self.hazard_kinds) is not tuple:
            raise CodingInspectHazardsError("hazard_kinds must be immutable")
        kinds = tuple(_kind(item) for item in self.hazard_kinds)
        if len(set(kinds)) != len(kinds):
            raise CodingInspectHazardsError("hazard_kinds must be unique")
        object.__setattr__(self, "hazard_kinds", kinds)
        if hazard_count != secret_count + executable_count + vcs_count:
            raise CodingInspectHazardsError("hazard counts disagree")
        if any(count > member_count for count in (secret_count, executable_count, vcs_count)):
            raise CodingInspectHazardsError("hazard count exceeds member count")
        if hazards in {
            CodingInspectHazardsState.EMPTY,
            CodingInspectHazardsState.BLOCKED,
        } and any((member_count, hazard_count, secret_count, executable_count, vcs_count, kinds)):
            raise CodingInspectHazardsError("empty and blocked hazards expose no facts")
        if hazards is CodingInspectHazardsState.CLEAR and (member_count == 0 or hazard_count != 0 or kinds):
            raise CodingInspectHazardsError("clear hazards need members and no hazards")
        if hazards is CodingInspectHazardsState.PRESENT and (
            member_count == 0 or hazard_count == 0 or not kinds
        ):
            raise CodingInspectHazardsError("present hazards need hazard facts")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "hazard_id": self.hazard_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "hazards": self.hazards.value,
            "member_count": self.member_count,
            "hazard_count": self.hazard_count,
            "secret_name_count": self.secret_name_count,
            "executable_member_count": self.executable_member_count,
            "nested_vcs_dir_count": self.nested_vcs_dir_count,
            "hazard_kinds": [kind.value for kind in self.hazard_kinds],
            "reason": self.reason.value,
        }


InspectHazardsState = CodingInspectHazardsState
InspectHazardKind = CodingInspectHazardKind
InspectHazardsReason = CodingInspectHazardsReason
CodingInspectHazards = CodingInspectHazardsV1
CodingInspectHazardsDecision = CodingInspectHazardsState


_SECRET_NAME_RE = re.compile(
    r"(?i)(?:^|[._-])(?:env|secret|secrets|credential|credentials|password|passwd|"
    r"token|apikey|api_key|private_key|id_rsa|ssh_key)(?:$|[._-])"
)
_VCS_DIRECTORY_NAMES = frozenset({".git", ".hg", ".svn", ".bzr"})


def _tree(
    hazard_id: str,
    authenticated_turn_id: str,
    value: object,
) -> CodingSourceTreeV1:
    if isinstance(value, CodingSourceTreeV1):
        if value.authenticated_turn_id != authenticated_turn_id:
            raise CodingInspectHazardsError("tree turn identity disagrees")
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if "members" in value or "source_members" in value or "member_facts" in value:
            return build_coding_source_tree(hazard_id, authenticated_turn_id, value)
        raise CodingInspectHazardsError("tree mapping has no members")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return build_coding_source_tree(hazard_id, authenticated_turn_id, value)
    raise CodingInspectHazardsError("tree must be a source tree or member sequence")


def _result(
    hazard_id: str,
    authenticated_turn_id: str,
    hazards: CodingInspectHazardsState,
    reason: CodingInspectHazardsReason,
    *,
    members: int = 0,
    hazard_count: int = 0,
    secret: int = 0,
    executable: int = 0,
    vcs: int = 0,
    kinds: tuple[CodingInspectHazardKind, ...] = (),
) -> CodingInspectHazardsV1:
    if hazards in {
        CodingInspectHazardsState.EMPTY,
        CodingInspectHazardsState.BLOCKED,
    }:
        members = hazard_count = secret = executable = vcs = 0
        kinds = ()
    return CodingInspectHazardsV1(
        hazard_id=hazard_id,
        authenticated_turn_id=authenticated_turn_id,
        hazards=hazards,
        member_count=members,
        hazard_count=hazard_count,
        secret_name_count=secret,
        executable_member_count=executable,
        nested_vcs_dir_count=vcs,
        hazard_kinds=kinds,
        reason=reason,
    )


def build_coding_inspect_hazards(
    hazard_id: str,
    authenticated_turn_id: str,
    source_tree: CodingSourceTreeV1 | Mapping[str, object] | Sequence[object] | None = None,
    *,
    members: Sequence[CodingSourceMemberV1 | Mapping[str, object]] | None = None,
) -> CodingInspectHazardsV1:
    """Scan names and permission metadata without reading file bodies."""

    hazard_key = _identifier(hazard_id, field="hazard_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if source_tree is not None and members is not None:
            raise CodingInspectHazardsError("source_tree and members cannot both be supplied")
        tree = _tree(
            hazard_key,
            turn_key,
            members if members is not None else (source_tree if source_tree is not None else ()),
        )
    except CodingInspectHazardsError:
        return _result(
            hazard_key,
            turn_key,
            CodingInspectHazardsState.BLOCKED,
            CodingInspectHazardsReason.INVALID_FACTS,
        )
    except (TypeError, ValueError):
        return _result(
            hazard_key,
            turn_key,
            CodingInspectHazardsState.BLOCKED,
            CodingInspectHazardsReason.INVALID_FACTS,
        )
    if tree.tree is CodingSourceTreeState.BLOCKED:
        return _result(
            hazard_key,
            turn_key,
            CodingInspectHazardsState.BLOCKED,
            CodingInspectHazardsReason.TREE_BLOCKED,
        )
    if tree.tree is CodingSourceTreeState.EMPTY:
        return _result(
            hazard_key,
            turn_key,
            CodingInspectHazardsState.EMPTY,
            CodingInspectHazardsReason.NO_MEMBERS,
        )

    secret_count = 0
    executable_count = 0
    vcs_count = 0
    kinds: list[CodingInspectHazardKind] = []
    for member in tree.members:
        components = member.relative_path.split("/")
        names = [component for component in components if component]
        basename = names[-1]
        if _SECRET_NAME_RE.search(basename):
            secret_count += 1
            if CodingInspectHazardKind.SECRET_LOOKING_NAME not in kinds:
                kinds.append(CodingInspectHazardKind.SECRET_LOOKING_NAME)
        if member.executable:
            executable_count += 1
            if CodingInspectHazardKind.EXECUTABLE_BIT not in kinds:
                kinds.append(CodingInspectHazardKind.EXECUTABLE_BIT)
        if any(component.casefold() in _VCS_DIRECTORY_NAMES for component in names):
            vcs_count += 1
            if CodingInspectHazardKind.NESTED_VCS_DIRECTORY not in kinds:
                kinds.append(CodingInspectHazardKind.NESTED_VCS_DIRECTORY)
    hazard_count = secret_count + executable_count + vcs_count
    if hazard_count == 0:
        return _result(
            hazard_key,
            turn_key,
            CodingInspectHazardsState.CLEAR,
            CodingInspectHazardsReason.NO_HAZARDS,
            members=tree.member_count,
        )
    return _result(
        hazard_key,
        turn_key,
        CodingInspectHazardsState.PRESENT,
        CodingInspectHazardsReason.HAZARDS_PRESENT,
        members=tree.member_count,
        hazard_count=hazard_count,
        secret=secret_count,
        executable=executable_count,
        vcs=vcs_count,
        kinds=tuple(kinds),
    )


scan_coding_inspect_hazards = build_coding_inspect_hazards
build_inspect_hazards = build_coding_inspect_hazards


__all__ = (
    "CodingInspectHazardKind",
    "CodingInspectHazards",
    "CodingInspectHazardsDecision",
    "CodingInspectHazardsError",
    "CodingInspectHazardsReason",
    "CodingInspectHazardsState",
    "CodingInspectHazardsV1",
    "InspectHazardKind",
    "InspectHazardsReason",
    "InspectHazardsState",
    "build_coding_inspect_hazards",
    "build_inspect_hazards",
    "scan_coding_inspect_hazards",
)
