"""Pure prompt-to-project request normalization for Coding Mode.

The contract consumes already-supplied title and goal facts.  It does not
call a model, open files, or create a project.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

CODING_PROMPT_NORMALIZATION_SCHEMA = "friday.coding-prompt-normalization.v1"
MAX_PROMPT_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_TITLE_CHARS = 80
MAX_GOAL_CHARS = 500

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_LANGUAGE_HINT_RE = re.compile(r"[a-z][a-z0-9+]{0,15}\Z")
_UNBOUNDED = frozenset(
    {
        "everything",
        "anything",
        "all",
        "universal",
        "any-language",
        "any_language",
        "fullstack",
        "full-stack",
        "всё",
        "все",
        "что угодно",
    }
)
_PATH_RE = re.compile(r"(?:^|[\s«\"'(])(?:~(?:/|$)|(?:/home|/etc|/var|/usr|/tmp|/root|/opt)/|[A-Za-z]:\\)")
_TRAVERSAL_RE = re.compile(r"(?:^|[\s/\\])\.\.(?:[/\\]|$)")
_URL_RE = re.compile(r"://")
_SECRETISH_RE = re.compile(r"(?i)(?:api[_-]?key|password|secret|token|bearer|authorization)\s*[:=]")


class CodingPromptNormalizationError(ValueError):
    """A prompt identity, fact, or result is malformed."""


class CodingPromptNormalizationState(StrEnum):
    EMPTY = "empty"
    NORMALIZED = "normalized"
    BLOCKED = "blocked"


class CodingPromptNormalizationReason(StrEnum):
    NO_FACTS = "no_facts"
    NORMALIZED = "normalized"
    MISSING_GOAL = "missing_goal"
    UNBOUNDED_GOAL = "unbounded_goal"
    UNSAFE_TEXT = "unsafe_text"
    INVALID_FACTS = "invalid_facts"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingPromptNormalizationError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> CodingPromptNormalizationState:
    try:
        return CodingPromptNormalizationState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingPromptNormalizationError("prompt_closed") from exc


def _reason(value: object) -> CodingPromptNormalizationReason:
    try:
        return CodingPromptNormalizationReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingPromptNormalizationError("reason_closed") from exc


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _safe_text(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or not value or value != value.strip():
        _fail(field, "text")
    text = cast(str, value)
    if len(text) > maximum or _contains_control(text):
        _fail(field, "text")
    if _PATH_RE.search(text) is not None or _TRAVERSAL_RE.search(text) is not None:
        _fail(field, "path")
    if _URL_RE.search(text) is not None:
        _fail(field, "url")
    if _SECRETISH_RE.search(text) is not None:
        _fail(field, "secret")
    return text


def _language_hint(value: object) -> str | None:
    if value is None or value == "":
        return None
    if type(value) is not str:
        _fail("language_hint", "token")
    hint = value.strip().casefold()
    if _LANGUAGE_HINT_RE.fullmatch(hint) is None:
        _fail("language_hint", "token")
    return hint


def _unbounded(goal: str) -> bool:
    folded = unicodedata.normalize("NFC", goal).casefold()
    tokens = set(re.findall(r"[0-9a-zа-яё+-]+", folded, flags=re.IGNORECASE))
    if tokens & _UNBOUNDED:
        return True
    return any(phrase in folded for phrase in _UNBOUNDED if " " in phrase)


@dataclass(frozen=True, slots=True)
class CodingPromptFactsV1:
    title: str | None = None
    goal: str | None = None
    language_hint: str | None = None


@dataclass(frozen=True, slots=True)
class CodingPromptNormalizationV1:
    prompt_id: str
    authenticated_turn_id: str
    prompt: CodingPromptNormalizationState
    title: str | None
    goal: str | None
    language_hint: str | None
    reason: CodingPromptNormalizationReason

    def __post_init__(self) -> None:
        _identifier(self.prompt_id, field="prompt_id", maximum=MAX_PROMPT_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        state = _state(self.prompt)
        reason = _reason(self.reason)
        object.__setattr__(self, "prompt", state)
        object.__setattr__(self, "reason", reason)
        if state is CodingPromptNormalizationState.NORMALIZED:
            object.__setattr__(self, "title", _safe_text(self.title, field="title", maximum=MAX_TITLE_CHARS))
            object.__setattr__(self, "goal", _safe_text(self.goal, field="goal", maximum=MAX_GOAL_CHARS))
            object.__setattr__(self, "language_hint", _language_hint(self.language_hint))
        elif self.title is not None or self.goal is not None or self.language_hint is not None:
            _fail("blocked_or_empty_prompt", "exposed")

    @property
    def state(self) -> CodingPromptNormalizationState:
        return self.prompt

    @property
    def closed_reason(self) -> CodingPromptNormalizationReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_PROMPT_NORMALIZATION_SCHEMA,
            "prompt_id": self.prompt_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "prompt": self.prompt.value,
            "title": self.title,
            "goal": self.goal,
            "language_hint": self.language_hint,
            "reason": self.reason.value,
        }


def _result(
    prompt_id: str,
    authenticated_turn_id: str,
    state: CodingPromptNormalizationState,
    reason: CodingPromptNormalizationReason,
    *,
    title: str | None = None,
    goal: str | None = None,
    language_hint: str | None = None,
) -> CodingPromptNormalizationV1:
    if state is not CodingPromptNormalizationState.NORMALIZED:
        title = None
        goal = None
        language_hint = None
    return CodingPromptNormalizationV1(
        prompt_id=prompt_id,
        authenticated_turn_id=authenticated_turn_id,
        prompt=state,
        title=title,
        goal=goal,
        language_hint=language_hint,
        reason=reason,
    )


def _facts(value: object) -> CodingPromptFactsV1:
    if value is None:
        return CodingPromptFactsV1()
    if isinstance(value, CodingPromptFactsV1):
        return value
    if not isinstance(value, Mapping):
        _fail("facts", "type")
    allowed = {"title", "goal", "language_hint", "language", "hint"}
    if set(value) - allowed:
        _fail("facts", "unknown_fields")
    return CodingPromptFactsV1(
        title=cast(str | None, value.get("title")),
        goal=cast(str | None, value.get("goal")),
        language_hint=cast(str | None, value.get("language_hint", value.get("language", value.get("hint")))),
    )


def build_coding_prompt_normalization(
    prompt_id: str,
    authenticated_turn_id: str,
    facts: CodingPromptFactsV1 | Mapping[str, object] | None = None,
    *,
    title: object = None,
    goal: object = None,
    language_hint: object = None,
) -> CodingPromptNormalizationV1:
    """Normalize a bounded prompt-to-project request from supplied facts."""

    identity = _identifier(prompt_id, field="prompt_id", maximum=MAX_PROMPT_ID_CHARS)
    turn = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    explicit = title is not None or goal is not None or language_hint is not None
    try:
        if explicit:
            if facts is not None:
                _fail("facts", "conflict")
            parsed = CodingPromptFactsV1(
                title=cast(str | None, title),
                goal=cast(str | None, goal),
                language_hint=cast(str | None, language_hint),
            )
        else:
            parsed = _facts(facts)
    except CodingPromptNormalizationError:
        return _result(
            identity,
            turn,
            CodingPromptNormalizationState.BLOCKED,
            CodingPromptNormalizationReason.INVALID_FACTS,
        )
    if parsed.title is None and parsed.goal is None and parsed.language_hint is None:
        return _result(
            identity,
            turn,
            CodingPromptNormalizationState.EMPTY,
            CodingPromptNormalizationReason.NO_FACTS,
        )
    if parsed.goal is None or (type(parsed.goal) is str and not parsed.goal.strip()):
        return _result(
            identity,
            turn,
            CodingPromptNormalizationState.BLOCKED,
            CodingPromptNormalizationReason.MISSING_GOAL,
        )
    try:
        goal_text = _safe_text(parsed.goal, field="goal", maximum=MAX_GOAL_CHARS)
        title_text = _safe_text(
            parsed.title if parsed.title is not None else goal_text[:MAX_TITLE_CHARS],
            field="title",
            maximum=MAX_TITLE_CHARS,
        )
        hint = _language_hint(parsed.language_hint)
    except CodingPromptNormalizationError as exc:
        if str(exc).endswith(("_path", "_url", "_secret", "_text")):
            return _result(
                identity,
                turn,
                CodingPromptNormalizationState.BLOCKED,
                CodingPromptNormalizationReason.UNSAFE_TEXT,
            )
        return _result(
            identity,
            turn,
            CodingPromptNormalizationState.BLOCKED,
            CodingPromptNormalizationReason.INVALID_FACTS,
        )
    if _unbounded(goal_text) or _unbounded(title_text):
        return _result(
            identity,
            turn,
            CodingPromptNormalizationState.BLOCKED,
            CodingPromptNormalizationReason.UNBOUNDED_GOAL,
        )
    return _result(
        identity,
        turn,
        CodingPromptNormalizationState.NORMALIZED,
        CodingPromptNormalizationReason.NORMALIZED,
        title=title_text,
        goal=goal_text,
        language_hint=hint,
    )


normalize_coding_prompt = build_coding_prompt_normalization

__all__ = [
    "CODING_PROMPT_NORMALIZATION_SCHEMA",
    "CodingPromptFactsV1",
    "CodingPromptNormalizationError",
    "CodingPromptNormalizationReason",
    "CodingPromptNormalizationState",
    "CodingPromptNormalizationV1",
    "build_coding_prompt_normalization",
    "normalize_coding_prompt",
]
