"""Read-only composition of a mixed journey and its shared situation view."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.agent_situation_projection import (
    AgentSituationAudience,
    AgentSituationProjectionV1,
    build_agent_situation_projection,
)
from friday.orchestration.mixed_journey_coverage import (
    MixedJourneyCoverageFactsV1,
    MixedJourneyCoverageState,
    MixedJourneyCoverageV1,
    build_mixed_journey_coverage,
)
from friday.orchestration.mixed_journey_identity import (
    MixedJourneyIdentityFactsV1,
    MixedJourneyIdentityV1,
    build_mixed_journey_identity,
)
from friday.orchestration.mixed_journey_organs import (
    MixedJourneyOrgansFactsV1,
    MixedJourneyOrgansState,
    MixedJourneyOrgansV1,
    build_mixed_journey_organs,
)
from friday.orchestration.mixed_journey_restart import (
    MixedJourneyRestartFactsV1,
    MixedJourneyRestartV1,
    build_mixed_journey_restart,
)
from friday.orchestration.mixed_journey_revoke import (
    MixedJourneyRevokeFactsV1,
    MixedJourneyRevokeState,
    MixedJourneyRevokeV1,
    build_mixed_journey_revoke,
)
from friday.orchestration.shared_operation_view import (
    SHARED_OPERATION_VIEW_SCHEMA,
    SharedOperationPendingWorkOwner,
    SharedOperationViewState,
    SharedOperationViewV1,
    build_shared_operation_view,
)

MIXED_JOURNEY_VIEW_SCHEMA = "friday.mixed-journey-view.v1"
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class MixedJourneyViewError(ValueError):
    """A mixed-journey composition is malformed."""


class MixedJourneyViewState(StrEnum):
    EMPTY = "empty"
    PROJECTED = "projected"
    BLOCKED = "blocked"


class MixedJourneyViewReason(StrEnum):
    NO_FACTS = "no_facts"
    PROJECTED = "projected"
    INVALID_FACTS = "invalid_facts"
    COMPONENT_BLOCKED = "component_blocked"
    COMPONENT_MISSING = "component_missing"
    SHARED_VIEW_BLOCKED = "shared_view_blocked"
    IDENTITY_MISMATCH = "identity_mismatch"
    REVOKED_BEFORE_PUBLISH = "revoked_before_publish"
    MULTIPLE_PUBLISHERS = "multiple_publishers"
    SECONDARY_OWNERSHIP = "secondary_ownership"


@dataclass(frozen=True, slots=True)
class MixedJourneyViewFactsV1:
    shared_operation_view: SharedOperationViewV1 | Mapping[str, Any] | None = None
    identity: MixedJourneyIdentityV1 | MixedJourneyIdentityFactsV1 | Mapping[str, Any] | None = None
    organs: MixedJourneyOrgansV1 | MixedJourneyOrgansFactsV1 | Mapping[str, Any] | None = None
    coverage: MixedJourneyCoverageV1 | MixedJourneyCoverageFactsV1 | Mapping[str, Any] | None = None
    revoke: MixedJourneyRevokeV1 | MixedJourneyRevokeFactsV1 | Mapping[str, Any] | None = None
    restart: MixedJourneyRestartV1 | MixedJourneyRestartFactsV1 | Mapping[str, Any] | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyViewError(f"{field}_{detail}")


def _id(value: object, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


@dataclass(frozen=True, slots=True)
class MixedJourneyViewV1:
    view_id: str
    authenticated_turn_id: str
    state: MixedJourneyViewState
    identity: MixedJourneyIdentityV1
    organs: MixedJourneyOrgansV1
    coverage: MixedJourneyCoverageV1
    revoke: MixedJourneyRevokeV1
    restart: MixedJourneyRestartV1
    shared_operation_view: SharedOperationViewV1
    primary_situation: AgentSituationProjectionV1
    secondary_situation: AgentSituationProjectionV1
    publication_claimed: bool
    publisher_count: int
    reason: MixedJourneyViewReason

    def __post_init__(self) -> None:
        _id(self.view_id, "view_id")
        _id(self.authenticated_turn_id, "authenticated_turn_id")
        try:
            state = MixedJourneyViewState(self.state)
            reason = MixedJourneyViewReason(self.reason)
        except (TypeError, ValueError) as exc:
            raise MixedJourneyViewError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        for component in (self.identity, self.organs, self.coverage, self.revoke, self.restart):
            if not isinstance(
                component,
                (
                    MixedJourneyIdentityV1,
                    MixedJourneyOrgansV1,
                    MixedJourneyCoverageV1,
                    MixedJourneyRevokeV1,
                    MixedJourneyRestartV1,
                ),
            ):
                _fail("component", "type")
            component.__post_init__()
        if not isinstance(self.shared_operation_view, SharedOperationViewV1):
            _fail("shared_operation_view", "type")
        self.shared_operation_view.__post_init__()
        if not isinstance(self.primary_situation, AgentSituationProjectionV1) or not isinstance(
            self.secondary_situation, AgentSituationProjectionV1
        ):
            _fail("situation", "type")
        if (
            type(self.publication_claimed) is not bool
            or type(self.publisher_count) is not int
            or not 0 <= self.publisher_count <= 8
        ):
            _fail("publication", "facts")
        if state is MixedJourneyViewState.PROJECTED:
            if any(
                component.state.value in {"blocked", "empty"}
                for component in (self.identity, self.organs, self.coverage, self.revoke, self.restart)
            ):
                _fail("projected", "components")
            if self.shared_operation_view.view is not SharedOperationViewState.PROJECTED:
                _fail("projected", "shared_view")
            if self.revoke.state is MixedJourneyRevokeState.REVOKED and self.publication_claimed:
                _fail("projected", "revoked")
            if self.publisher_count > 1:
                _fail("projected", "publishers")
        elif (
            self.publication_claimed
            or self.publisher_count
            or self.shared_operation_view.view is SharedOperationViewState.PROJECTED
        ):
            _fail("non_projected", "leak")

    @property
    def view_state(self) -> MixedJourneyViewState:
        return self.state

    @property
    def decision(self) -> MixedJourneyViewState:
        return self.state

    @property
    def shared_view(self) -> SharedOperationViewV1:
        return self.shared_operation_view

    @property
    def primary(self) -> AgentSituationProjectionV1:
        return self.primary_situation

    @property
    def secondary(self) -> AgentSituationProjectionV1:
        return self.secondary_situation

    @property
    def operation_id(self) -> str | None:
        return self.shared_operation_view.operation_id

    @property
    def pending_work_owner(self) -> SharedOperationPendingWorkOwner | None:
        return self.shared_operation_view.pending_work_owner

    @property
    def publication_admitted(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_VIEW_SCHEMA,
            "view_id": self.view_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "state": self.state.value,
            "identity": self.identity.to_mapping(),
            "organs": self.organs.to_mapping(),
            "coverage": self.coverage.to_mapping(),
            "revoke": self.revoke.to_mapping(),
            "restart": self.restart.to_mapping(),
            "shared_operation_view": self.shared_operation_view.to_mapping(),
            "primary_situation": self.primary_situation.to_mapping(),
            "secondary_situation": self.secondary_situation.to_mapping(),
            "publication_claimed": self.publication_claimed,
            "publisher_count": self.publisher_count,
            "reason": self.reason.value,
        }


MixedJourneyView = MixedJourneyViewV1
MixedJourneyViewFacts = MixedJourneyViewFactsV1
ViewState = MixedJourneyViewState
ViewReason = MixedJourneyViewReason


def _empty(key: str, turn: str, reason: MixedJourneyViewReason) -> MixedJourneyViewV1:
    identity = build_mixed_journey_identity(key, turn)
    organs = build_mixed_journey_organs(key, turn)
    coverage = build_mixed_journey_coverage(key, turn)
    revoke = build_mixed_journey_revoke(key, turn)
    restart = build_mixed_journey_restart(key, turn)
    shared = build_shared_operation_view(key, turn)
    primary = build_agent_situation_projection(key, turn, audience=AgentSituationAudience.PRIMARY)
    secondary = build_agent_situation_projection(key, turn, audience=AgentSituationAudience.SECONDARY)
    return MixedJourneyViewV1(
        key,
        turn,
        MixedJourneyViewState.EMPTY,
        identity,
        organs,
        coverage,
        revoke,
        restart,
        shared,
        primary,
        secondary,
        False,
        0,
        reason,
    )


def _blocked(key: str, turn: str, reason: MixedJourneyViewReason) -> MixedJourneyViewV1:
    identity = build_mixed_journey_identity(key, turn)
    organs = build_mixed_journey_organs(key, turn)
    coverage = build_mixed_journey_coverage(key, turn)
    revoke = build_mixed_journey_revoke(key, turn)
    restart = build_mixed_journey_restart(key, turn)
    shared = build_shared_operation_view(key, turn)
    primary = build_agent_situation_projection(key, turn, audience=AgentSituationAudience.PRIMARY)
    secondary = build_agent_situation_projection(key, turn, audience=AgentSituationAudience.SECONDARY)
    return MixedJourneyViewV1(
        key,
        turn,
        MixedJourneyViewState.BLOCKED,
        identity,
        organs,
        coverage,
        revoke,
        restart,
        shared,
        primary,
        secondary,
        False,
        0,
        reason,
    )


def _shared(value: object, *, key: str, turn: str) -> SharedOperationViewV1:
    if isinstance(value, SharedOperationViewV1):
        value.__post_init__()
        if value.view is not SharedOperationViewState.PROJECTED:
            _fail("shared_view", "state")
        return value
    if isinstance(value, Mapping):
        raw = dict(value)
        if raw.get("schema") == SHARED_OPERATION_VIEW_SCHEMA:
            raw.setdefault("view_id", key)
            raw.setdefault("authenticated_turn_id", turn)
            result = build_shared_operation_view(raw)
        else:
            result = build_shared_operation_view(key, turn, facts=raw)
        if result.view is not SharedOperationViewState.PROJECTED:
            _fail("shared_view", "state")
        return result
    _fail("shared_view", "type")


def _component(value: object, *, key: str, turn: str, kind: str) -> object:
    builder: Any
    cls: type[Any]
    facts_cls: type[Any]
    if kind == "identity":
        builder = build_mixed_journey_identity
        cls = MixedJourneyIdentityV1
        facts_cls = MixedJourneyIdentityFactsV1
    elif kind == "organs":
        builder = build_mixed_journey_organs
        cls = MixedJourneyOrgansV1
        facts_cls = MixedJourneyOrgansFactsV1
    elif kind == "coverage":
        builder = build_mixed_journey_coverage
        cls = MixedJourneyCoverageV1
        facts_cls = MixedJourneyCoverageFactsV1
    elif kind == "revoke":
        builder = build_mixed_journey_revoke
        cls = MixedJourneyRevokeV1
        facts_cls = MixedJourneyRevokeFactsV1
    else:
        builder = build_mixed_journey_restart
        cls = MixedJourneyRestartV1
        facts_cls = MixedJourneyRestartFactsV1
    if isinstance(value, cls):
        value.__post_init__()
        if value.journey_id != key or value.authenticated_turn_id != turn:
            _fail(kind, "identity")
        return value
    if isinstance(value, facts_cls):
        return builder(key, turn, facts=value)
    if isinstance(value, Mapping):
        return builder(value) if value.get("schema") else builder(key, turn, facts=value)
    _fail(kind, "type")


def build_mixed_journey_view(
    view_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    shared_operation_view: SharedOperationViewV1 | Mapping[str, Any] | None = None,
    *,
    facts: MixedJourneyViewFactsV1 | Mapping[str, Any] | None = None,
    identity: object = None,
    organs: object = None,
    coverage: object = None,
    revoke: object = None,
    restart: object = None,
) -> MixedJourneyViewV1:
    """Compose one mixed journey from already-supplied, immutable facts."""

    if isinstance(view_id, Mapping):
        raw = view_id
        key = cast(str, raw.get("view_id", raw.get("journey_id", "view")))
        turn = cast(str, raw.get("authenticated_turn_id", raw.get("turn_id", "turn")))
        try:
            key, turn = _id(key, "view_id"), _id(turn, "authenticated_turn_id")
            if raw.get("schema", MIXED_JOURNEY_VIEW_SCHEMA) != MIXED_JOURNEY_VIEW_SCHEMA:
                _fail("schema")
            allowed = {
                "schema",
                "view_id",
                "journey_id",
                "authenticated_turn_id",
                "turn_id",
                "state",
                "reason",
                "identity",
                "organs",
                "coverage",
                "revoke",
                "restart",
                "shared_operation_view",
                "shared_view",
                "primary_situation",
                "secondary_situation",
                "publication_claimed",
                "publisher_count",
            }
            if set(raw) - allowed:
                _fail("facts", "unknown")
            if raw.get("state") in {"empty", "blocked"}:
                state = MixedJourneyViewState(raw["state"])
                reason = MixedJourneyViewReason(raw.get("reason", "invalid_facts"))
                return (
                    _blocked(key, turn, reason)
                    if state is MixedJourneyViewState.BLOCKED
                    else _empty(key, turn, reason)
                )
            shared_operation_view = raw.get("shared_operation_view", raw.get("shared_view"))
            identity, organs, coverage, revoke, restart = (
                raw.get(name) for name in ("identity", "organs", "coverage", "revoke", "restart")
            )
        except (TypeError, ValueError, MixedJourneyViewError):
            try:
                return _blocked(
                    _id(key, "view_id"),
                    _id(turn, "authenticated_turn_id"),
                    MixedJourneyViewReason.INVALID_FACTS,
                )
            except MixedJourneyViewError:
                return _blocked("view", "turn", MixedJourneyViewReason.INVALID_FACTS)
    else:
        key = _id(view_id, "view_id")
        turn = _id(authenticated_turn_id, "authenticated_turn_id")
    if facts is not None:
        try:
            if isinstance(facts, MixedJourneyViewFactsV1):
                if shared_operation_view is not None and facts.shared_operation_view is not None:
                    _fail("facts", "duplicate")
                if any(value is not None for value in (identity, organs, coverage, revoke, restart)):
                    _fail("facts", "duplicate")
                if shared_operation_view is None:
                    shared_operation_view = facts.shared_operation_view
                identity, organs, coverage, revoke, restart = (
                    facts.identity,
                    facts.organs,
                    facts.coverage,
                    facts.revoke,
                    facts.restart,
                )
            elif isinstance(facts, Mapping):
                allowed = {
                    "schema",
                    "shared_operation_view",
                    "shared_view",
                    "identity",
                    "organs",
                    "coverage",
                    "revoke",
                    "restart",
                    "primary_situation",
                    "secondary_situation",
                    "publication_claimed",
                    "publisher_count",
                    "state",
                    "reason",
                }
                if set(facts) - allowed:
                    _fail("facts", "unknown")
                supplied_shared = facts.get("shared_operation_view", facts.get("shared_view"))
                if shared_operation_view is not None and supplied_shared is not None:
                    _fail("facts", "duplicate")
                if any(value is not None for value in (identity, organs, coverage, revoke, restart)):
                    _fail("facts", "duplicate")
                if shared_operation_view is None:
                    shared_operation_view = supplied_shared
                identity, organs, coverage, revoke, restart = (
                    facts.get(name) for name in ("identity", "organs", "coverage", "revoke", "restart")
                )
            else:
                _fail("facts", "type")
        except (TypeError, ValueError, MixedJourneyViewError):
            return _blocked(key, turn, MixedJourneyViewReason.INVALID_FACTS)
    if all(value is None for value in (shared_operation_view, identity, organs, coverage, revoke, restart)):
        return _empty(key, turn, MixedJourneyViewReason.NO_FACTS)
    try:
        shared = _shared(shared_operation_view, key=key, turn=turn)
        identity_value = cast(
            MixedJourneyIdentityV1, _component(identity, key=key, turn=turn, kind="identity")
        )
        organs_value = cast(MixedJourneyOrgansV1, _component(organs, key=key, turn=turn, kind="organs"))
        coverage_value = cast(
            MixedJourneyCoverageV1, _component(coverage, key=key, turn=turn, kind="coverage")
        )
        revoke_value = cast(MixedJourneyRevokeV1, _component(revoke, key=key, turn=turn, kind="revoke"))
        restart_value = cast(MixedJourneyRestartV1, _component(restart, key=key, turn=turn, kind="restart"))
    except (TypeError, ValueError, MixedJourneyViewError):
        return _blocked(
            key,
            turn,
            MixedJourneyViewReason.COMPONENT_MISSING
            if any(
                value is None
                for value in (shared_operation_view, identity, organs, coverage, revoke, restart)
            )
            else MixedJourneyViewReason.INVALID_FACTS,
        )
    components = (identity_value, organs_value, coverage_value, revoke_value, restart_value)
    if any(component.state.value == "blocked" for component in components):
        return _blocked(key, turn, MixedJourneyViewReason.COMPONENT_BLOCKED)
    if any(component.state.value == "empty" for component in components):
        return _blocked(key, turn, MixedJourneyViewReason.COMPONENT_MISSING)
    if (
        identity_value.operation_id != shared.operation_id
        or identity_value.authenticated_turn_id != shared.authenticated_turn_id
    ):
        return _blocked(key, turn, MixedJourneyViewReason.IDENTITY_MISMATCH)
    publishers = shared.publisher_count
    if identity_value.publisher_count > publishers:
        publishers = identity_value.publisher_count
    if publishers > 1:
        return _blocked(key, turn, MixedJourneyViewReason.MULTIPLE_PUBLISHERS)
    if revoke_value.state is MixedJourneyRevokeState.REVOKED and revoke_value.publication_claimed:
        return _blocked(key, turn, MixedJourneyViewReason.REVOKED_BEFORE_PUBLISH)
    if organs_value.state is not MixedJourneyOrgansState.PRESENT or coverage_value.state not in {
        MixedJourneyCoverageState.PARTIAL,
        MixedJourneyCoverageState.COMPLETE,
    }:
        return _blocked(key, turn, MixedJourneyViewReason.COMPONENT_MISSING)
    if (
        shared.secondary.secondary.value == "absent"
        and shared.pending_work_owner is not SharedOperationPendingWorkOwner.PRIMARY
    ):
        return _blocked(key, turn, MixedJourneyViewReason.SECONDARY_OWNERSHIP)
    primary = build_agent_situation_projection(key, turn, shared, AgentSituationAudience.PRIMARY)
    secondary = build_agent_situation_projection(key, turn, shared, AgentSituationAudience.SECONDARY)
    if primary.state.value == "blocked" or secondary.state.value == "blocked":
        return _blocked(key, turn, MixedJourneyViewReason.INVALID_FACTS)
    return MixedJourneyViewV1(
        key,
        turn,
        MixedJourneyViewState.PROJECTED,
        identity_value,
        organs_value,
        coverage_value,
        revoke_value,
        restart_value,
        shared,
        primary,
        secondary,
        revoke_value.publication_claimed,
        publishers,
        MixedJourneyViewReason.PROJECTED,
    )


def validate_mixed_journey_view(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyViewV1)
            else build_mixed_journey_view(cast(Mapping[str, Any], value))
        )
        return isinstance(result, MixedJourneyViewV1) and result.state is not MixedJourneyViewState.BLOCKED
    except (TypeError, ValueError):
        return False


build_journey_view = build_mixed_journey_view
validate_journey_view = validate_mixed_journey_view

__all__ = [
    "MIXED_JOURNEY_VIEW_SCHEMA",
    "MixedJourneyView",
    "MixedJourneyViewError",
    "MixedJourneyViewFacts",
    "MixedJourneyViewFactsV1",
    "MixedJourneyViewReason",
    "MixedJourneyViewState",
    "MixedJourneyViewV1",
    "ViewReason",
    "ViewState",
    "build_journey_view",
    "build_mixed_journey_view",
    "validate_journey_view",
    "validate_mixed_journey_view",
]
