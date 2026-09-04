"""Pure bounded implementation plan for prompt-to-small-project.

The planner consumes already-normalized step facts.  It does not execute,
scaffold files, or talk to a coding worker.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

CODING_IMPLEMENTATION_PLAN_SCHEMA = "friday.coding-implementation-plan.v1"
MAX_PLAN_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_STEPS = 16
MAX_STEP_ID_CHARS = 64
MAX_TARGET_PATH_CHARS = 256

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_STEP_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


class CodingImplementationPlanError(ValueError):
    """A plan identity, step, or result is malformed."""


class CodingImplementationPlanState(StrEnum):
    EMPTY = "empty"
    PLANNED = "planned"
    BLOCKED = "blocked"


class CodingImplementationPlanReason(StrEnum):
    NO_STEPS = "no_steps"
    ALL_STEPS_PLANNED = "all_steps_planned"
    STEP_LIMIT = "step_limit"
    UNSAFE_TARGET = "unsafe_target"
    EXECUTE_FORBIDDEN = "execute_forbidden"
    INVALID_FACTS = "invalid_facts"


class CodingPlanAction(StrEnum):
    CREATE = "create"
    EDIT = "edit"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise CodingImplementationPlanError(f"{field}_{detail}")


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> CodingImplementationPlanState:
    try:
        return CodingImplementationPlanState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingImplementationPlanError("plan_closed") from exc


def _reason(value: object) -> CodingImplementationPlanReason:
    try:
        return CodingImplementationPlanReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingImplementationPlanError("reason_closed") from exc


def _action(value: object) -> CodingPlanAction:
    try:
        return CodingPlanAction(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingImplementationPlanError("action_closed") from exc


def _target_path(value: object) -> str:
    if type(value) is not str or not value or len(value) > MAX_TARGET_PATH_CHARS:
        _fail("target_path", "path")
    path = cast(str, value)
    if path != path.strip() or any(unicodedata.category(character).startswith("C") for character in path):
        _fail("target_path", "path")
    if path.startswith(("/", "\\")) or _DRIVE_RE.match(path) is not None:
        _fail("target_path", "absolute")
    parts = tuple(part for part in re.split(r"[/\\]", path) if part)
    if not parts or any(part in {".", ".."} for part in parts):
        _fail("target_path", "traversal")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class CodingImplementationStepV1:
    step_id: str
    action: CodingPlanAction
    target_path: str

    def __post_init__(self) -> None:
        if type(self.step_id) is not str or _STEP_ID_RE.fullmatch(self.step_id) is None:
            _fail("step_id", "id")
        object.__setattr__(self, "action", _action(self.action))
        object.__setattr__(self, "target_path", _target_path(self.target_path))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action": self.action.value,
            "target_path": self.target_path,
        }


@dataclass(frozen=True, slots=True)
class CodingImplementationPlanV1:
    plan_id: str
    authenticated_turn_id: str
    plan: CodingImplementationPlanState
    steps: tuple[CodingImplementationStepV1, ...]
    reason: CodingImplementationPlanReason

    def __post_init__(self) -> None:
        _identifier(self.plan_id, field="plan_id", maximum=MAX_PLAN_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        state = _state(self.plan)
        reason = _reason(self.reason)
        object.__setattr__(self, "plan", state)
        object.__setattr__(self, "reason", reason)
        if not isinstance(self.steps, tuple):
            _fail("steps", "tuple")
        if len(self.steps) > MAX_STEPS:
            _fail("steps", "count")
        if state is CodingImplementationPlanState.PLANNED:
            if not self.steps:
                _fail("steps", "missing")
            seen: set[str] = set()
            for step in self.steps:
                if not isinstance(step, CodingImplementationStepV1):
                    _fail("steps", "type")
                step.__post_init__()
                if step.step_id in seen:
                    _fail("step_id", "duplicate")
                seen.add(step.step_id)
        elif self.steps:
            _fail("blocked_or_empty_plan", "exposed")

    @property
    def state(self) -> CodingImplementationPlanState:
        return self.plan

    @property
    def closed_reason(self) -> CodingImplementationPlanReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_IMPLEMENTATION_PLAN_SCHEMA,
            "plan_id": self.plan_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "plan": self.plan.value,
            "steps": [step.to_mapping() for step in self.steps],
            "reason": self.reason.value,
        }


_EXECUTE_ACTIONS = frozenset({"execute", "build", "test", "run", "install", "spawn"})


def _step(value: object) -> CodingImplementationStepV1:
    if isinstance(value, CodingImplementationStepV1):
        value.__post_init__()
        return value
    if not isinstance(value, Mapping):
        _fail("step", "type")
    allowed = {"step_id", "action", "target_path", "path"}
    if set(value) - allowed:
        _fail("step", "unknown_fields")
    action = value.get("action")
    if type(action) is str and action.strip().casefold() in _EXECUTE_ACTIONS:
        _fail("action", "execute")
    return CodingImplementationStepV1(
        step_id=cast(str, value.get("step_id")),
        action=cast(CodingPlanAction, action),
        target_path=cast(str, value.get("target_path", value.get("path"))),
    )


def _result(
    plan_id: str,
    authenticated_turn_id: str,
    state: CodingImplementationPlanState,
    reason: CodingImplementationPlanReason,
    steps: tuple[CodingImplementationStepV1, ...] = (),
) -> CodingImplementationPlanV1:
    if state is not CodingImplementationPlanState.PLANNED:
        steps = ()
    return CodingImplementationPlanV1(
        plan_id=plan_id,
        authenticated_turn_id=authenticated_turn_id,
        plan=state,
        steps=steps,
        reason=reason,
    )


def build_coding_implementation_plan(
    plan_id: str,
    authenticated_turn_id: str,
    steps: Sequence[object] | None = None,
) -> CodingImplementationPlanV1:
    """Plan create/edit steps only; execute/build/test fail closed."""

    identity = _identifier(plan_id, field="plan_id", maximum=MAX_PLAN_ID_CHARS)
    turn = _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    if steps is None:
        return _result(
            identity,
            turn,
            CodingImplementationPlanState.EMPTY,
            CodingImplementationPlanReason.NO_STEPS,
        )
    if isinstance(steps, (str, bytes, bytearray)) or not isinstance(steps, Sequence):
        return _result(
            identity,
            turn,
            CodingImplementationPlanState.BLOCKED,
            CodingImplementationPlanReason.INVALID_FACTS,
        )
    if len(steps) > MAX_STEPS:
        return _result(
            identity,
            turn,
            CodingImplementationPlanState.BLOCKED,
            CodingImplementationPlanReason.STEP_LIMIT,
        )
    if not steps:
        return _result(
            identity,
            turn,
            CodingImplementationPlanState.EMPTY,
            CodingImplementationPlanReason.NO_STEPS,
        )
    planned: list[CodingImplementationStepV1] = []
    try:
        for item in steps:
            planned.append(_step(item))
    except CodingImplementationPlanError as exc:
        code = str(exc)
        if code == "action_execute":
            reason = CodingImplementationPlanReason.EXECUTE_FORBIDDEN
        elif code.endswith(("_absolute", "_traversal", "_path")):
            reason = CodingImplementationPlanReason.UNSAFE_TARGET
        else:
            reason = CodingImplementationPlanReason.INVALID_FACTS
        return _result(identity, turn, CodingImplementationPlanState.BLOCKED, reason)
    return _result(
        identity,
        turn,
        CodingImplementationPlanState.PLANNED,
        CodingImplementationPlanReason.ALL_STEPS_PLANNED,
        tuple(planned),
    )


plan_coding_implementation = build_coding_implementation_plan

__all__ = [
    "CODING_IMPLEMENTATION_PLAN_SCHEMA",
    "CodingImplementationPlanError",
    "CodingImplementationPlanReason",
    "CodingImplementationPlanState",
    "CodingImplementationPlanV1",
    "CodingImplementationStepV1",
    "CodingPlanAction",
    "build_coding_implementation_plan",
    "plan_coding_implementation",
]
