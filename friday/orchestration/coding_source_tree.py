"""Immutable, body-free source-tree mapping for Coding Mode inspection."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.orchestration.coding_source_member import (
    CodingSourceLinkKind,
    CodingSourceMemberError,
    CodingSourceMemberV1,
    build_coding_source_member,
)

MAX_SOURCE_MEMBER_COUNT = 4_096


class CodingSourceTreeError(ValueError):
    """A source-tree fact is malformed or unsafe to map."""


class CodingSourceTreeState(StrEnum):
    """Closed source-tree mapping outcomes."""

    EMPTY = "empty"
    MAPPED = "mapped"
    BLOCKED = "blocked"


class CodingSourceTreeReason(StrEnum):
    """Closed reasons for one source-tree mapping."""

    NO_MEMBERS = "no_members"
    ALL_MEMBERS_MAPPED = "all_members_mapped"
    PATH_TRAVERSAL = "path_traversal"
    ABSOLUTE_PATH = "absolute_path"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    CASEFOLD_COLLISION = "casefold_collision"
    MEMBER_COUNT_LIMIT = "member_count_limit"
    INVALID_FACTS = "invalid_facts"

    CASE_FOLD_COLLISION = CASEFOLD_COLLISION
    HARD_LINK = HARDLINK


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise CodingSourceTreeError(f"{field} is invalid")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise CodingSourceTreeError(f"{field} is invalid")
    return value


def _state(value: object) -> CodingSourceTreeState:
    if isinstance(value, CodingSourceTreeState):
        return value
    if type(value) is not str:
        raise CodingSourceTreeError("tree must be a closed value")
    try:
        return CodingSourceTreeState(value.strip().casefold())
    except ValueError as exc:
        raise CodingSourceTreeError("unknown tree value") from exc


def _reason(value: object) -> CodingSourceTreeReason:
    if isinstance(value, CodingSourceTreeReason):
        return value
    if type(value) is not str:
        raise CodingSourceTreeError("reason must be a closed value")
    try:
        return CodingSourceTreeReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingSourceTreeError("unknown tree reason") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SOURCE_MEMBER_COUNT:
        raise CodingSourceTreeError(f"{field} is outside its closed bound")
    return value


def _canonical_path(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _member_values(value: object) -> tuple[CodingSourceMemberV1, ...]:
    if isinstance(value, Mapping):
        allowed = {"members", "source_members", "member_facts"}
        if set(value) - allowed:
            raise CodingSourceTreeError("tree contains unknown fields")
        value = value.get("members", value.get("source_members", value.get("member_facts", ())))
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CodingSourceTreeError("members must be a sequence")
    if len(value) > MAX_SOURCE_MEMBER_COUNT:
        raise CodingSourceTreeError("members exceed the closed bound")
    result: list[CodingSourceMemberV1] = []
    for item in value:
        try:
            result.append(build_coding_source_member(item))
        except CodingSourceMemberError as exc:
            raise CodingSourceTreeError(str(exc)) from exc
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CodingSourceTreeV1:
    """Immutable source-member map; it contains metadata, never file bodies."""

    tree_id: str
    authenticated_turn_id: str
    tree: CodingSourceTreeState
    members: tuple[CodingSourceMemberV1, ...]
    member_count: int
    reason: CodingSourceTreeReason

    @property
    def state(self) -> CodingSourceTreeState:
        return self.tree

    @property
    def closed_tree(self) -> CodingSourceTreeState:
        return self.tree

    @property
    def decision(self) -> CodingSourceTreeState:
        return self.tree

    @property
    def closed_reason(self) -> CodingSourceTreeReason:
        return self.reason

    @property
    def source_members(self) -> tuple[CodingSourceMemberV1, ...]:
        return self.members

    def __post_init__(self) -> None:
        _identifier(self.tree_id, field="tree_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        tree = _state(self.tree)
        reason = _reason(self.reason)
        if type(self.members) is not tuple:
            raise CodingSourceTreeError("members must be immutable")
        if any(not isinstance(member, CodingSourceMemberV1) for member in self.members):
            raise CodingSourceTreeError("members contain invalid facts")
        _count(self.member_count, field="member_count")
        if self.member_count != len(self.members):
            raise CodingSourceTreeError("member_count disagrees with members")
        folded = tuple(_canonical_path(member.relative_path) for member in self.members)
        if len(set(folded)) != len(folded):
            raise CodingSourceTreeError("members contain a casefold collision")
        object.__setattr__(self, "tree", tree)
        object.__setattr__(self, "reason", reason)
        if any(member.link_kind is not CodingSourceLinkKind.NONE for member in self.members):
            raise CodingSourceTreeError("mapped tree cannot contain links")
        if tree is CodingSourceTreeState.BLOCKED and self.members:
            raise CodingSourceTreeError("blocked tree cannot expose members")
        if tree is CodingSourceTreeState.EMPTY and self.members:
            raise CodingSourceTreeError("empty tree cannot expose members")
        if tree is CodingSourceTreeState.MAPPED and not self.members:
            raise CodingSourceTreeError("mapped tree needs members")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "tree_id": self.tree_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "tree": self.tree.value,
            "members": [member.to_mapping() for member in self.members],
            "member_count": self.member_count,
            "reason": self.reason.value,
        }


SourceTreeState = CodingSourceTreeState
SourceTreeReason = CodingSourceTreeReason
SourceTree = CodingSourceTreeV1
CodingSourceTree = CodingSourceTreeV1
CodingSourceTreeDecision = CodingSourceTreeState


def _result(
    tree_id: str,
    authenticated_turn_id: str,
    tree: CodingSourceTreeState,
    reason: CodingSourceTreeReason,
    *,
    members: tuple[CodingSourceMemberV1, ...] = (),
) -> CodingSourceTreeV1:
    return CodingSourceTreeV1(
        tree_id=tree_id,
        authenticated_turn_id=authenticated_turn_id,
        tree=tree,
        members=members if tree is CodingSourceTreeState.MAPPED else (),
        member_count=len(members) if tree is CodingSourceTreeState.MAPPED else 0,
        reason=reason,
    )


def build_coding_source_tree(
    tree_id: str,
    authenticated_turn_id: str,
    members: Sequence[CodingSourceMemberV1 | Mapping[str, object]] | Mapping[str, object] | None = None,
    *,
    source_members: Sequence[CodingSourceMemberV1 | Mapping[str, object]] | None = None,
) -> CodingSourceTreeV1:
    """Map observed members and reject unsafe path/link metadata."""

    tree_key = _identifier(tree_id, field="tree_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if members is not None and source_members is not None:
            raise CodingSourceTreeError("members and source_members cannot both be supplied")
        member_values = _member_values(
            source_members if source_members is not None else (members if members is not None else ())
        )
    except CodingSourceTreeError as exc:
        message = str(exc).casefold()
        if "absolute" in message:
            reason = CodingSourceTreeReason.ABSOLUTE_PATH
        elif "travers" in message or "separator" in message:
            reason = CodingSourceTreeReason.PATH_TRAVERSAL
        elif "member" in message and "bound" in message:
            reason = CodingSourceTreeReason.MEMBER_COUNT_LIMIT
        else:
            reason = CodingSourceTreeReason.INVALID_FACTS
        return _result(tree_key, turn_key, CodingSourceTreeState.BLOCKED, reason)
    except (TypeError, ValueError):
        return _result(
            tree_key,
            turn_key,
            CodingSourceTreeState.BLOCKED,
            CodingSourceTreeReason.INVALID_FACTS,
        )

    if not member_values:
        return _result(
            tree_key,
            turn_key,
            CodingSourceTreeState.EMPTY,
            CodingSourceTreeReason.NO_MEMBERS,
        )
    folded: set[str] = set()
    for member in member_values:
        if member.link_kind is CodingSourceLinkKind.SYMLINK:
            return _result(tree_key, turn_key, CodingSourceTreeState.BLOCKED, CodingSourceTreeReason.SYMLINK)
        if member.link_kind is CodingSourceLinkKind.HARDLINK:
            return _result(tree_key, turn_key, CodingSourceTreeState.BLOCKED, CodingSourceTreeReason.HARDLINK)
        canonical = _canonical_path(member.relative_path)
        if canonical in folded:
            return _result(
                tree_key,
                turn_key,
                CodingSourceTreeState.BLOCKED,
                CodingSourceTreeReason.CASEFOLD_COLLISION,
            )
        folded.add(canonical)
    return _result(
        tree_key,
        turn_key,
        CodingSourceTreeState.MAPPED,
        CodingSourceTreeReason.ALL_MEMBERS_MAPPED,
        members=member_values,
    )


map_coding_source_tree = build_coding_source_tree
build_source_tree = build_coding_source_tree


__all__ = (
    "CodingSourceTree",
    "CodingSourceTreeDecision",
    "CodingSourceTreeError",
    "CodingSourceTreeReason",
    "CodingSourceTreeState",
    "CodingSourceTreeV1",
    "MAX_SOURCE_MEMBER_COUNT",
    "SourceTree",
    "SourceTreeReason",
    "SourceTreeState",
    "build_coding_source_tree",
    "build_source_tree",
    "map_coding_source_tree",
)
