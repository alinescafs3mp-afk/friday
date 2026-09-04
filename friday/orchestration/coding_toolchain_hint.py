"""Pure filename-suffix hints for an observed coding source tree."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from friday.orchestration.coding_source_member import (
    CodingSourceFileKind,
    CodingSourceMemberV1,
)
from friday.orchestration.coding_source_tree import (
    CodingSourceTreeState,
    CodingSourceTreeV1,
    build_coding_source_tree,
)


class CodingToolchainHintError(ValueError):
    """Toolchain-hint input facts are malformed."""


class CodingToolchainHintState(StrEnum):
    """Closed filename-only toolchain-hint outcomes."""

    EMPTY = "empty"
    HINTED = "hinted"
    BLOCKED = "blocked"


class CodingToolchainHintReason(StrEnum):
    """Closed reasons for one filename-only hint."""

    NO_MEMBERS = "no_members"
    NO_KNOWN_SUFFIXES = "no_known_suffixes"
    SUFFIX_HINTS = "suffix_hints"
    TREE_BLOCKED = "tree_blocked"
    INVALID_FACTS = "invalid_facts"


_SUFFIX_HINTS = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".c": "c",
    ".h": "c-header",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp-header",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
}


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or not value or len(value) > 128:
        raise CodingToolchainHintError(f"{field} is invalid")
    if value != value.strip() or any(ord(character) < 32 for character in value):
        raise CodingToolchainHintError(f"{field} is invalid")
    return value


def _state(value: object) -> CodingToolchainHintState:
    if isinstance(value, CodingToolchainHintState):
        return value
    if type(value) is not str:
        raise CodingToolchainHintError("hint must be a closed value")
    try:
        return CodingToolchainHintState(value.strip().casefold())
    except ValueError as exc:
        raise CodingToolchainHintError("unknown hint value") from exc


def _reason(value: object) -> CodingToolchainHintReason:
    if isinstance(value, CodingToolchainHintReason):
        return value
    if type(value) is not str:
        raise CodingToolchainHintError("reason must be a closed value")
    try:
        return CodingToolchainHintReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingToolchainHintError("unknown hint reason") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= 4_096:
        raise CodingToolchainHintError(f"{field} is outside its closed bound")
    return value


def _text_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CodingToolchainHintError(f"{field} must be an immutable sequence")
    result: list[str] = []
    for item in value:
        if type(item) is not str or not item or item != item.strip():
            raise CodingToolchainHintError(f"{field} contains invalid text")
        result.append(item)
    if len(set(result)) != len(result):
        raise CodingToolchainHintError(f"{field} contains duplicates")
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CodingToolchainHintV1:
    """Immutable suffix hints; no installed toolchain is claimed."""

    hint_id: str
    authenticated_turn_id: str
    hint: CodingToolchainHintState
    member_count: int
    detected_suffixes: tuple[str, ...]
    language_hints: tuple[str, ...]
    reason: CodingToolchainHintReason

    @property
    def state(self) -> CodingToolchainHintState:
        return self.hint

    @property
    def closed_hint(self) -> CodingToolchainHintState:
        return self.hint

    @property
    def decision(self) -> CodingToolchainHintState:
        return self.hint

    @property
    def closed_reason(self) -> CodingToolchainHintReason:
        return self.reason

    @property
    def suffixes(self) -> tuple[str, ...]:
        return self.detected_suffixes

    @property
    def languages(self) -> tuple[str, ...]:
        return self.language_hints

    @property
    def installed(self) -> bool:
        return False

    def __post_init__(self) -> None:
        _identifier(self.hint_id, field="hint_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        hint = _state(self.hint)
        reason = _reason(self.reason)
        object.__setattr__(self, "hint", hint)
        object.__setattr__(self, "reason", reason)
        _count(self.member_count, field="member_count")
        suffixes = _text_tuple(self.detected_suffixes, field="detected_suffixes")
        languages = _text_tuple(self.language_hints, field="language_hints")
        object.__setattr__(self, "detected_suffixes", suffixes)
        object.__setattr__(self, "language_hints", languages)
        if hint is CodingToolchainHintState.BLOCKED and (self.member_count or suffixes or languages):
            raise CodingToolchainHintError("blocked hint cannot expose facts")
        if hint is CodingToolchainHintState.HINTED and (
            self.member_count == 0 or not suffixes or not languages
        ):
            raise CodingToolchainHintError("hinted result needs suffix hints")
        if hint is CodingToolchainHintState.EMPTY and (suffixes or languages):
            raise CodingToolchainHintError("empty hint cannot expose suffix hints")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "hint_id": self.hint_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "hint": self.hint.value,
            "member_count": self.member_count,
            "detected_suffixes": list(self.detected_suffixes),
            "language_hints": list(self.language_hints),
            "reason": self.reason.value,
        }


ToolchainHintState = CodingToolchainHintState
ToolchainHintReason = CodingToolchainHintReason
CodingToolchainHint = CodingToolchainHintV1
CodingToolchainHintDecision = CodingToolchainHintState


def _tree(
    hint_id: str,
    authenticated_turn_id: str,
    value: object,
) -> CodingSourceTreeV1:
    if isinstance(value, CodingSourceTreeV1):
        if value.authenticated_turn_id != authenticated_turn_id:
            raise CodingToolchainHintError("tree turn identity disagrees")
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        if "members" in value or "source_members" in value or "member_facts" in value:
            return build_coding_source_tree(hint_id, authenticated_turn_id, value)
        raise CodingToolchainHintError("tree mapping has no members")
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return build_coding_source_tree(hint_id, authenticated_turn_id, value)
    raise CodingToolchainHintError("tree must be a source tree or member sequence")


def _result(
    hint_id: str,
    authenticated_turn_id: str,
    hint: CodingToolchainHintState,
    reason: CodingToolchainHintReason,
    *,
    members: int = 0,
    suffixes: tuple[str, ...] = (),
    languages: tuple[str, ...] = (),
) -> CodingToolchainHintV1:
    if hint is CodingToolchainHintState.BLOCKED:
        members = 0
        suffixes = languages = ()
    return CodingToolchainHintV1(
        hint_id=hint_id,
        authenticated_turn_id=authenticated_turn_id,
        hint=hint,
        member_count=members,
        detected_suffixes=suffixes,
        language_hints=languages,
        reason=reason,
    )


def build_coding_toolchain_hint(
    hint_id: str,
    authenticated_turn_id: str,
    source_tree: CodingSourceTreeV1 | Mapping[str, object] | Sequence[object] | None = None,
    *,
    members: Sequence[CodingSourceMemberV1 | Mapping[str, object]] | None = None,
) -> CodingToolchainHintV1:
    """Derive neutral filename suffix hints without probing or claiming tools."""

    hint_key = _identifier(hint_id, field="hint_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if source_tree is not None and members is not None:
            raise CodingToolchainHintError("source_tree and members cannot both be supplied")
        tree = _tree(
            hint_key,
            turn_key,
            members if members is not None else (source_tree if source_tree is not None else ()),
        )
    except CodingToolchainHintError:
        return _result(
            hint_key,
            turn_key,
            CodingToolchainHintState.BLOCKED,
            CodingToolchainHintReason.INVALID_FACTS,
        )
    except (TypeError, ValueError):
        return _result(
            hint_key,
            turn_key,
            CodingToolchainHintState.BLOCKED,
            CodingToolchainHintReason.INVALID_FACTS,
        )
    if tree.tree is CodingSourceTreeState.BLOCKED:
        return _result(
            hint_key,
            turn_key,
            CodingToolchainHintState.BLOCKED,
            CodingToolchainHintReason.TREE_BLOCKED,
        )
    if tree.tree is CodingSourceTreeState.EMPTY:
        return _result(
            hint_key,
            turn_key,
            CodingToolchainHintState.EMPTY,
            CodingToolchainHintReason.NO_MEMBERS,
        )

    suffixes: list[str] = []
    languages: list[str] = []
    for member in tree.members:
        if member.file_kind is not CodingSourceFileKind.REGULAR_FILE:
            continue
        basename = member.relative_path.rsplit("/", 1)[-1]
        suffix = "." + basename.rsplit(".", 1)[-1].casefold() if "." in basename else ""
        language = _SUFFIX_HINTS.get(suffix)
        if language is not None:
            if suffix not in suffixes:
                suffixes.append(suffix)
            if language not in languages:
                languages.append(language)
    if not suffixes:
        return _result(
            hint_key,
            turn_key,
            CodingToolchainHintState.EMPTY,
            CodingToolchainHintReason.NO_KNOWN_SUFFIXES,
            members=tree.member_count,
        )
    return _result(
        hint_key,
        turn_key,
        CodingToolchainHintState.HINTED,
        CodingToolchainHintReason.SUFFIX_HINTS,
        members=tree.member_count,
        suffixes=tuple(suffixes),
        languages=tuple(languages),
    )


hint_coding_toolchain = build_coding_toolchain_hint
build_toolchain_hint = build_coding_toolchain_hint


__all__ = (
    "CodingToolchainHint",
    "CodingToolchainHintDecision",
    "CodingToolchainHintError",
    "CodingToolchainHintReason",
    "CodingToolchainHintState",
    "CodingToolchainHintV1",
    "ToolchainHintReason",
    "ToolchainHintState",
    "build_coding_toolchain_hint",
    "build_toolchain_hint",
    "hint_coding_toolchain",
)
