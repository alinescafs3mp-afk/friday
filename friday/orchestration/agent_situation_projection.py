"""Bounded primary/secondary projections of a shared operation view.

Both audiences receive only safe, already-projected facts.  The secondary
audience is intentionally smaller than the primary audience and remains
advisory: this module grants no tools, effects, publication, or authority.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.operation_progress import (
    OperationMode,
    OperationStep,
    ResultDeliveryState,
    build_operation_progress,
)
from friday.orchestration.shared_operation_view import (
    SHARED_OPERATION_VIEW_SCHEMA,
    SharedOperationPendingWorkOwner,
    SharedOperationViewState,
    SharedOperationViewV1,
    build_shared_operation_view,
)

AGENT_SITUATION_PROJECTION_SCHEMA = "friday.agent-situation-projection.v1"
MAX_PROJECTION_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_CAPABILITY_STATES = frozenset({"empty", "available", "unavailable", "blocked"})
_SECONDARY_STATES = frozenset({"empty", "present", "absent", "blocked"})


class AgentSituationProjectionError(ValueError):
    """An audience, source view, or situation projection is malformed."""


class AgentSituationProjectionState(StrEnum):
    EMPTY = "empty"
    PROJECTED = "projected"
    BLOCKED = "blocked"


class AgentSituationProjectionReason(StrEnum):
    NO_FACTS = "no_facts"
    PROJECTED = "projected"
    SOURCE_BLOCKED = "source_blocked"
    SOURCE_INVALID = "source_invalid"
    AUDIENCE_INVALID = "audience_invalid"
    INVALID_FACTS = "invalid_facts"


class AgentSituationAudience(StrEnum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


@dataclass(frozen=True, slots=True)
class AgentSituationProjectionFactsV1:
    """One source view and the intended safe audience."""

    view: SharedOperationViewV1 | Mapping[str, Any] | None = None
    audience: AgentSituationAudience | str = AgentSituationAudience.PRIMARY


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise AgentSituationProjectionError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _audience(value: object) -> AgentSituationAudience:
    if isinstance(value, AgentSituationAudience):
        return value
    try:
        return AgentSituationAudience(cast(str, value).strip().casefold())
    except (TypeError, ValueError) as exc:
        raise AgentSituationProjectionError("audience_invalid") from exc


def _state(value: object) -> AgentSituationProjectionState:
    try:
        return AgentSituationProjectionState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise AgentSituationProjectionError("situation_closed") from exc


def _reason(value: object) -> AgentSituationProjectionReason:
    try:
        return AgentSituationProjectionReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise AgentSituationProjectionError("reason_closed") from exc


def _safe_status(value: object, *, allowed: frozenset[str], field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or value not in allowed:
        _fail(field)
    return cast(str, value)


def _safe_digest(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail(field)
    return cast(str, value)


def _safe_owner(value: object) -> SharedOperationPendingWorkOwner | None:
    if value is None:
        return None
    if isinstance(value, SharedOperationPendingWorkOwner):
        return value
    try:
        return SharedOperationPendingWorkOwner(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise AgentSituationProjectionError("pending_work_owner_invalid") from exc


def _serialized_plan(
    value: object,
    *,
    operation_id: str,
    turn_id: str,
    mode: OperationMode,
    terminal: bool,
    active_step_id: str | None,
) -> tuple[OperationStep, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("ordered_plan", "sequence")
    delivery = ResultDeliveryState.CONFIRMED if terminal else ResultDeliveryState.IN_FLIGHT
    try:
        projection = build_operation_progress(
            {
                "operation_id": operation_id,
                "authenticated_turn_id": turn_id,
                "revision": 1,
                "terminal": terminal,
                "mode": mode.value,
                "title": "situation",
                "ordered_steps": value,
                "active_step_id": active_step_id,
                "elapsed_sec": 0,
                "hard_deadline_remaining_sec": None,
                "result_delivery_state": delivery.value,
                "plan_generation": 1,
            }
        )
    except (TypeError, ValueError) as exc:
        raise AgentSituationProjectionError("ordered_plan_invalid") from exc
    return projection.ordered_steps


@dataclass(frozen=True, slots=True)
class AgentSituationProjectionV1:
    """A secret-free situation projection for one model audience."""

    projection_id: str
    authenticated_turn_id: str
    audience: AgentSituationAudience
    situation: AgentSituationProjectionState
    operation_id: str | None
    mode: OperationMode | None
    binding_digest: str | None
    ordered_plan: tuple[OperationStep, ...]
    active_step_id: str | None
    pending_work_owner: SharedOperationPendingWorkOwner | None
    deadline_remaining_sec: int | None
    capability_availability: str | None
    secondary_availability: str | None
    artifact_class: str | None
    artifact_count: int
    artifact_digest: str | None
    terminal_evidence_class: str | None
    terminal: bool | None
    reason: AgentSituationProjectionReason

    def __post_init__(self) -> None:
        _identifier(self.projection_id, field="projection_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        audience = _audience(self.audience)
        state = _state(self.situation)
        reason = _reason(self.reason)
        object.__setattr__(self, "audience", audience)
        object.__setattr__(self, "situation", state)
        object.__setattr__(self, "reason", reason)
        if self.operation_id is not None:
            _identifier(self.operation_id, field="operation_id")
        if self.mode is not None and not isinstance(self.mode, OperationMode):
            try:
                object.__setattr__(self, "mode", OperationMode(cast(str, self.mode)))
            except (TypeError, ValueError) as exc:
                raise AgentSituationProjectionError("mode_invalid") from exc
        pending_owner = _safe_owner(self.pending_work_owner)
        object.__setattr__(self, "pending_work_owner", pending_owner)
        _safe_digest(self.binding_digest, field="binding_digest")
        _safe_status(
            self.capability_availability,
            allowed=_CAPABILITY_STATES,
            field="capability_availability",
        )
        _safe_status(
            self.secondary_availability,
            allowed=_SECONDARY_STATES,
            field="secondary_availability",
        )
        if self.deadline_remaining_sec is not None and (
            type(self.deadline_remaining_sec) is not int or not 0 <= self.deadline_remaining_sec <= 604800
        ):
            _fail("deadline_remaining_sec")
        if self.terminal is not None and type(self.terminal) is not bool:
            _fail("terminal")
        if type(self.artifact_count) is not int or self.artifact_count < 0 or self.artifact_count > 1_000_000:
            _fail("artifact_count")
        if self.artifact_class is not None and (
            type(self.artifact_class) is not str
            or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", self.artifact_class)
        ):
            _fail("artifact_class")
        _safe_digest(self.artifact_digest, field="artifact_digest")
        if self.terminal_evidence_class is not None and (
            type(self.terminal_evidence_class) is not str
            or not re.fullmatch(r"[a-z][a-z0-9_.:-]{0,63}", self.terminal_evidence_class)
        ):
            _fail("terminal_evidence_class")
        if type(self.ordered_plan) is not tuple or any(
            not isinstance(step, OperationStep) for step in self.ordered_plan
        ):
            _fail("ordered_plan", "immutable")
        if audience is AgentSituationAudience.SECONDARY and (
            self.binding_digest is not None
            or self.ordered_plan
            or self.secondary_availability is not None
            or self.artifact_class is not None
            or self.artifact_count
            or self.artifact_digest is not None
            or self.terminal_evidence_class is not None
        ):
            _fail("secondary", "too_broad")
        if state is not AgentSituationProjectionState.PROJECTED and (
            self.operation_id is not None
            or self.mode is not None
            or self.binding_digest is not None
            or self.ordered_plan
            or self.active_step_id is not None
            or self.pending_work_owner is not None
            or self.deadline_remaining_sec is not None
            or self.capability_availability is not None
            or self.secondary_availability is not None
            or self.artifact_class is not None
            or self.artifact_count
            or self.artifact_digest is not None
            or self.terminal_evidence_class is not None
            or self.terminal is not None
        ):
            _fail("non_projected", "exposes_facts")

    @property
    def state(self) -> AgentSituationProjectionState:
        return self.situation

    @property
    def situation_state(self) -> AgentSituationProjectionState:
        return self.situation

    @property
    def projection(self) -> AgentSituationProjectionState:
        return self.situation

    @property
    def closed_situation(self) -> AgentSituationProjectionState:
        return self.situation

    @property
    def decision(self) -> AgentSituationProjectionState:
        return self.situation

    @property
    def closed_reason(self) -> AgentSituationProjectionReason:
        return self.reason

    @property
    def is_primary(self) -> bool:
        return self.audience is AgentSituationAudience.PRIMARY

    @property
    def is_secondary(self) -> bool:
        return self.audience is AgentSituationAudience.SECONDARY

    @property
    def ordered_steps(self) -> tuple[OperationStep, ...]:
        return self.ordered_plan

    @property
    def deadline(self) -> int | None:
        return self.deadline_remaining_sec

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": AGENT_SITUATION_PROJECTION_SCHEMA,
            "projection_id": self.projection_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "audience": self.audience.value,
            "situation": self.situation.value,
            "operation_id": self.operation_id,
            "mode": self.mode.value if self.mode is not None else None,
            "binding_digest": self.binding_digest,
            "ordered_plan": [step.to_mapping() for step in self.ordered_plan],
            "active_step_id": self.active_step_id,
            "pending_work_owner": self.pending_work_owner.value
            if self.pending_work_owner is not None
            else None,
            "deadline_remaining_sec": self.deadline_remaining_sec,
            "capability_availability": self.capability_availability,
            "secondary_availability": self.secondary_availability,
            "artifact_class": self.artifact_class,
            "artifact_count": self.artifact_count,
            "artifact_digest": self.artifact_digest,
            "terminal_evidence_class": self.terminal_evidence_class,
            "terminal": self.terminal,
            "reason": self.reason.value,
        }


SituationState = AgentSituationProjectionState
SituationReason = AgentSituationProjectionReason
SituationAudience = AgentSituationAudience
AgentSituationProjection = AgentSituationProjectionV1
AgentSituationFacts = AgentSituationProjectionFactsV1


def _empty_result(
    projection_id: str,
    turn_id: str,
    audience: AgentSituationAudience,
    state: AgentSituationProjectionState,
    reason: AgentSituationProjectionReason,
) -> AgentSituationProjectionV1:
    return AgentSituationProjectionV1(
        projection_id,
        turn_id,
        audience,
        state,
        None,
        None,
        None,
        (),
        None,
        None,
        None,
        None,
        None,
        None,
        0,
        None,
        None,
        None,
        reason,
    )


def _view(value: object, *, view_id: str, turn_id: str) -> SharedOperationViewV1:
    if isinstance(value, SharedOperationViewV1):
        value.__post_init__()
        return value
    if isinstance(value, Mapping):
        raw = dict(value)
        if raw.get("schema") == SHARED_OPERATION_VIEW_SCHEMA:
            raw.setdefault("view_id", view_id)
            raw.setdefault("authenticated_turn_id", turn_id)
            return build_shared_operation_view(raw)
        return build_shared_operation_view(
            view_id,
            turn_id,
            facts=raw,
        )
    _fail("view", "invalid")


def build_agent_situation_projection(
    projection_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    view: SharedOperationViewV1 | Mapping[str, Any] | None = None,
    audience: AgentSituationAudience | str = AgentSituationAudience.PRIMARY,
    *,
    facts: AgentSituationProjectionFactsV1 | Mapping[str, Any] | None = None,
) -> AgentSituationProjectionV1:
    """Project one shared view for the primary or advisory secondary audience."""

    if isinstance(projection_id, Mapping):
        raw = projection_id
        try:
            if raw.get("schema", AGENT_SITUATION_PROJECTION_SCHEMA) != AGENT_SITUATION_PROJECTION_SCHEMA:
                _fail("schema")
            projection_key = _identifier(raw.get("projection_id"), field="projection_id")
            turn_key = _identifier(raw.get("authenticated_turn_id"), field="authenticated_turn_id")
            audience = _audience(raw.get("audience"))
            if "situation" in raw or "state" in raw:
                state = _state(raw.get("situation", raw.get("state")))
                reason = _reason(raw.get("reason"))
                if state is not AgentSituationProjectionState.PROJECTED:
                    return _empty_result(projection_key, turn_key, audience, state, reason)
                return AgentSituationProjectionV1(
                    projection_id=projection_key,
                    authenticated_turn_id=turn_key,
                    audience=audience,
                    situation=state,
                    operation_id=cast(str | None, raw.get("operation_id")),
                    mode=cast(OperationMode | None, raw.get("mode")),
                    binding_digest=cast(str | None, raw.get("binding_digest")),
                    ordered_plan=_serialized_plan(
                        raw.get("ordered_plan", raw.get("ordered_steps", [])),
                        operation_id=cast(str, raw.get("operation_id")),
                        turn_id=turn_key,
                        mode=OperationMode(cast(str, raw.get("mode"))),
                        terminal=cast(bool, raw.get("terminal")),
                        active_step_id=cast(str | None, raw.get("active_step_id")),
                    )
                    if audience is AgentSituationAudience.PRIMARY
                    else (),
                    active_step_id=cast(str | None, raw.get("active_step_id")),
                    pending_work_owner=cast(
                        SharedOperationPendingWorkOwner | None, raw.get("pending_work_owner")
                    ),
                    deadline_remaining_sec=cast(int | None, raw.get("deadline_remaining_sec")),
                    capability_availability=cast(str | None, raw.get("capability_availability")),
                    secondary_availability=cast(str | None, raw.get("secondary_availability")),
                    artifact_class=cast(str | None, raw.get("artifact_class")),
                    artifact_count=cast(int, raw.get("artifact_count", 0)),
                    artifact_digest=cast(str | None, raw.get("artifact_digest")),
                    terminal_evidence_class=cast(str | None, raw.get("terminal_evidence_class")),
                    terminal=cast(bool | None, raw.get("terminal")),
                    reason=reason,
                )
            view = raw.get("view")
            audience = _audience(raw.get("audience", audience))
            projection_id = projection_key
            authenticated_turn_id = turn_key
        except (TypeError, ValueError):
            projection_id = cast(str, raw.get("projection_id", "projection"))
            authenticated_turn_id = cast(str, raw.get("authenticated_turn_id", "turn"))
            projection_key = _identifier(projection_id, field="projection_id")
            turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
            return _empty_result(
                projection_key,
                turn_key,
                AgentSituationAudience.PRIMARY,
                AgentSituationProjectionState.BLOCKED,
                AgentSituationProjectionReason.INVALID_FACTS,
            )

    projection_key = _identifier(projection_id, field="projection_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    audience_value = _audience(audience)
    if facts is not None:
        try:
            if view is not None:
                _fail("facts", "duplicate_arguments")
            if isinstance(facts, AgentSituationProjectionFactsV1):
                view = facts.view
                audience_value = _audience(facts.audience)
            elif isinstance(facts, Mapping):
                view = facts.get("view", facts.get("source_view"))
                audience_value = _audience(facts.get("audience", audience_value))
            else:
                _fail("facts", "type")
        except AgentSituationProjectionError:
            return _empty_result(
                projection_key,
                turn_key,
                audience_value,
                AgentSituationProjectionState.BLOCKED,
                AgentSituationProjectionReason.INVALID_FACTS,
            )
    if view is None:
        return _empty_result(
            projection_key,
            turn_key,
            audience_value,
            AgentSituationProjectionState.EMPTY,
            AgentSituationProjectionReason.NO_FACTS,
        )
    try:
        source = _view(view, view_id=projection_key, turn_id=turn_key)
    except (TypeError, ValueError):
        return _empty_result(
            projection_key,
            turn_key,
            audience_value,
            AgentSituationProjectionState.BLOCKED,
            AgentSituationProjectionReason.SOURCE_INVALID,
        )
    if source.view is SharedOperationViewState.BLOCKED:
        return _empty_result(
            projection_key,
            turn_key,
            audience_value,
            AgentSituationProjectionState.BLOCKED,
            AgentSituationProjectionReason.SOURCE_BLOCKED,
        )
    if source.view is SharedOperationViewState.EMPTY:
        return _empty_result(
            projection_key,
            turn_key,
            audience_value,
            AgentSituationProjectionState.EMPTY,
            AgentSituationProjectionReason.NO_FACTS,
        )
    if source.authenticated_turn_id != turn_key:
        return _empty_result(
            projection_key,
            turn_key,
            audience_value,
            AgentSituationProjectionState.BLOCKED,
            AgentSituationProjectionReason.SOURCE_INVALID,
        )
    if audience_value is AgentSituationAudience.SECONDARY:
        return AgentSituationProjectionV1(
            projection_id=projection_key,
            authenticated_turn_id=turn_key,
            audience=audience_value,
            situation=AgentSituationProjectionState.PROJECTED,
            operation_id=source.operation_id,
            mode=source.mode,
            binding_digest=None,
            ordered_plan=(),
            active_step_id=source.active_step_id,
            pending_work_owner=source.pending_work_owner,
            deadline_remaining_sec=source.inherited_deadline_remaining_sec,
            capability_availability=source.capability.capability.value,
            secondary_availability=None,
            artifact_class=None,
            artifact_count=0,
            artifact_digest=None,
            terminal_evidence_class=None,
            terminal=source.terminal,
            reason=AgentSituationProjectionReason.PROJECTED,
        )
    return AgentSituationProjectionV1(
        projection_id=projection_key,
        authenticated_turn_id=turn_key,
        audience=audience_value,
        situation=AgentSituationProjectionState.PROJECTED,
        operation_id=source.operation_id,
        mode=source.mode,
        binding_digest=source.binding_digest,
        ordered_plan=source.ordered_plan,
        active_step_id=source.active_step_id,
        pending_work_owner=source.pending_work_owner,
        deadline_remaining_sec=source.inherited_deadline_remaining_sec,
        capability_availability=source.capability.capability.value,
        secondary_availability=source.secondary.secondary.value,
        artifact_class=source.artifacts.artifact_class,
        artifact_count=source.artifacts.artifact_count,
        artifact_digest=source.artifacts.artifact_digest,
        terminal_evidence_class=source.terminal_evidence_class,
        terminal=source.terminal,
        reason=AgentSituationProjectionReason.PROJECTED,
    )


def validate_agent_situation_projection(value: object) -> bool:
    try:
        if isinstance(value, AgentSituationProjectionV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping) or value.get("schema") != AGENT_SITUATION_PROJECTION_SCHEMA:
            return False
        build_agent_situation_projection(value)
        return True
    except (TypeError, ValueError):
        return False


build_situation_projection = build_agent_situation_projection
validate_situation_projection = validate_agent_situation_projection


__all__ = [
    "AGENT_SITUATION_PROJECTION_SCHEMA",
    "AgentSituationAudience",
    "AgentSituationFacts",
    "AgentSituationProjection",
    "AgentSituationProjectionError",
    "AgentSituationProjectionFactsV1",
    "AgentSituationProjectionReason",
    "AgentSituationProjectionState",
    "AgentSituationProjectionV1",
    "SituationAudience",
    "SituationReason",
    "SituationState",
    "build_agent_situation_projection",
    "build_situation_projection",
    "validate_agent_situation_projection",
    "validate_situation_projection",
]
