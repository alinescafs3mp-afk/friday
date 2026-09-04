"""Pure composition gate for the five isolated Coding-worker contracts.

Admission is a summary, not an executor.  It accepts already-produced
contracts or their body-free mappings and never spawns a process, opens a
socket, imports Docker, or reads a path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from friday.orchestration.coding_worker_identity import (
    CodingWorkerIdentityReason,
    CodingWorkerIdentityState,
    CodingWorkerIdentityV1,
    build_coding_worker_identity,
)
from friday.orchestration.coding_worker_isolation import (
    CodingWorkerIsolationAdmissionV1,
    CodingWorkerIsolationReason,
    CodingWorkerIsolationState,
    build_coding_worker_isolation,
)
from friday.orchestration.coding_worker_limits import (
    CodingWorkerLimitsReason,
    CodingWorkerLimitsState,
    CodingWorkerLimitsV1,
    build_coding_worker_limits,
)
from friday.orchestration.coding_worker_network import (
    CodingWorkerNetworkPolicyV1,
    CodingWorkerNetworkReason,
    CodingWorkerNetworkState,
    build_coding_worker_network,
)
from friday.orchestration.coding_worker_workspace import (
    CodingWorkerWorkspaceReason,
    CodingWorkerWorkspaceState,
    CodingWorkerWorkspaceV1,
    build_coding_worker_workspace,
)

CODING_WORKER_ADMISSION_SCHEMA = "friday.coding-worker-admission.v1"
MAX_ADMISSION_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MISSING = object()


class CodingWorkerAdmissionError(ValueError):
    """Admission input or directly constructed result is malformed."""


class CodingWorkerAdmissionState(StrEnum):
    """Closed outcomes for composed worker admission."""

    EMPTY = "empty"
    ADMITTED = "admitted"
    BLOCKED = "blocked"


class CodingWorkerAdmissionReason(StrEnum):
    """Non-sensitive reason for one composed admission outcome."""

    NO_FACTS = "no_facts"
    ADMITTED = "admitted"
    MISSING_COMPONENT = "missing_component"
    COMPONENT_EMPTY = "component_empty"
    IDENTITY_BLOCKED = "identity_blocked"
    ISOLATION_BLOCKED = "isolation_blocked"
    NETWORK_BLOCKED = "network_blocked"
    WORKSPACE_BLOCKED = "workspace_blocked"
    LIMITS_BLOCKED = "limits_blocked"
    IDENTITY_NOT_IDENTIFIED = "identity_not_identified"
    ISOLATION_NOT_ADMITTED = "isolation_not_admitted"
    NETWORK_NOT_BOUNDED = "network_not_bounded"
    WORKSPACE_NOT_BOUND = "workspace_not_bound"
    LIMITS_NOT_BOUNDED = "limits_not_bounded"
    AUTHENTICATED_TURN_MISMATCH = "authenticated_turn_mismatch"
    OPERATION_MISMATCH = "operation_mismatch"
    INVALID_FACTS = "invalid_facts"


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingWorkerAdmissionError(f"{field} must be a bounded opaque identifier")
    return cast(str, value)


def _state(value: object) -> CodingWorkerAdmissionState:
    if isinstance(value, CodingWorkerAdmissionState):
        return value
    if type(value) is not str:
        raise CodingWorkerAdmissionError("admission must be a closed value")
    try:
        return CodingWorkerAdmissionState(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerAdmissionError("admission must be a closed value") from exc


def _reason(value: object) -> CodingWorkerAdmissionReason:
    if isinstance(value, CodingWorkerAdmissionReason):
        return value
    if type(value) is not str:
        raise CodingWorkerAdmissionError("admission reason must be a closed value")
    try:
        return CodingWorkerAdmissionReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerAdmissionError("admission reason must be a closed value") from exc


@dataclass(frozen=True, slots=True)
class CodingWorkerAdmissionFactsV1:
    """Five already-supplied contracts or fact mappings to compose."""

    identity: object | None = None
    isolation: object | None = None
    network: object | None = None
    workspace: object | None = None
    limits: object | None = None


@dataclass(frozen=True, slots=True)
class CodingWorkerAdmissionV1:
    """Immutable summary of an isolated worker's complete admission."""

    admission_id: str
    authenticated_turn_id: str
    admission: CodingWorkerAdmissionState
    identity: CodingWorkerIdentityV1 | None
    isolation: CodingWorkerIsolationAdmissionV1 | None
    network: CodingWorkerNetworkPolicyV1 | None
    workspace: CodingWorkerWorkspaceV1 | None
    limits: CodingWorkerLimitsV1 | None
    reason: CodingWorkerAdmissionReason

    def __post_init__(self) -> None:
        _identifier(self.admission_id, field="admission_id", maximum=MAX_ADMISSION_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        admission = _state(self.admission)
        reason = _reason(self.reason)
        object.__setattr__(self, "admission", admission)
        object.__setattr__(self, "reason", reason)
        components = (self.identity, self.isolation, self.network, self.workspace, self.limits)
        if admission is not CodingWorkerAdmissionState.ADMITTED:
            if any(component is not None for component in components):
                raise CodingWorkerAdmissionError("blocked or empty admission cannot expose components")
            return
        if any(component is None for component in components):
            raise CodingWorkerAdmissionError("admitted admission needs five components")
        assert self.identity is not None
        assert self.isolation is not None
        assert self.network is not None
        assert self.workspace is not None
        assert self.limits is not None
        if self.identity.identity is not CodingWorkerIdentityState.IDENTIFIED:
            raise CodingWorkerAdmissionError("admitted admission needs identified identity")
        if self.isolation.isolation is not CodingWorkerIsolationState.ADMITTED:
            raise CodingWorkerAdmissionError("admitted admission needs admitted isolation")
        if self.network.network not in {
            CodingWorkerNetworkState.DISABLED,
            CodingWorkerNetworkState.BOUNDED,
        }:
            raise CodingWorkerAdmissionError("admitted admission needs disabled or bounded network")
        if self.workspace.workspace is not CodingWorkerWorkspaceState.BOUND:
            raise CodingWorkerAdmissionError("admitted admission needs bound workspace")
        if self.limits.limits is not CodingWorkerLimitsState.BOUNDED:
            raise CodingWorkerAdmissionError("admitted admission needs bounded limits")

    @property
    def state(self) -> CodingWorkerAdmissionState:
        return self.admission

    @property
    def decision(self) -> CodingWorkerAdmissionState:
        return self.admission

    @property
    def closed_admission(self) -> CodingWorkerAdmissionState:
        return self.admission

    @property
    def closed_reason(self) -> CodingWorkerAdmissionReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_WORKER_ADMISSION_SCHEMA,
            "admission_id": self.admission_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "admission": self.admission.value,
            "identity": self.identity.to_mapping() if self.identity is not None else None,
            "isolation": self.isolation.to_mapping() if self.isolation is not None else None,
            "network": self.network.to_mapping() if self.network is not None else None,
            "workspace": self.workspace.to_mapping() if self.workspace is not None else None,
            "limits": self.limits.to_mapping() if self.limits is not None else None,
            "reason": self.reason.value,
        }


WorkerAdmissionState = CodingWorkerAdmissionState
WorkerAdmissionReason = CodingWorkerAdmissionReason
CodingWorkerAdmissionDecision = CodingWorkerAdmissionState
CodingWorkerAdmissionContract = CodingWorkerAdmissionV1
CodingWorkerAdmissionFacts = CodingWorkerAdmissionFactsV1


def _mapping_facts(value: Mapping[str, object]) -> tuple[object, object, object, object, object]:
    allowed = {
        "schema",
        "admission_id",
        "authenticated_turn_id",
        "admission",
        "state",
        "reason",
        "identity",
        "worker_identity",
        "isolation",
        "worker_isolation",
        "network",
        "network_policy",
        "workspace",
        "workspace_binding",
        "limits",
        "resource_limits",
    }
    if set(value) - allowed:
        raise CodingWorkerAdmissionError("admission facts contain unknown fields")
    if value.get("schema", CODING_WORKER_ADMISSION_SCHEMA) != CODING_WORKER_ADMISSION_SCHEMA:
        raise CodingWorkerAdmissionError("admission schema is invalid")
    return (
        value.get("identity", value.get("worker_identity", _MISSING)),
        value.get("isolation", value.get("worker_isolation", _MISSING)),
        value.get("network", value.get("network_policy", _MISSING)),
        value.get("workspace", value.get("workspace_binding", _MISSING)),
        value.get("limits", value.get("resource_limits", _MISSING)),
    )


def _facts(value: object) -> tuple[object, object, object, object, object]:
    if value is None:
        return (_MISSING, _MISSING, _MISSING, _MISSING, _MISSING)
    if isinstance(value, CodingWorkerAdmissionFactsV1):
        return value.identity, value.isolation, value.network, value.workspace, value.limits
    if isinstance(value, Mapping):
        return _mapping_facts(value)
    raise CodingWorkerAdmissionError("admission facts must be a mapping or facts object")


def _mapping_state(value: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if type(candidate) is str:
            return candidate.strip().casefold()
    return None


def _coerce_identity(value: object, admission_id: str, turn: str) -> CodingWorkerIdentityV1:
    if isinstance(value, CodingWorkerIdentityV1):
        return value
    if isinstance(value, Mapping):
        state = _mapping_state(value, "identity", "state")
        if state in {"empty", "blocked"} and not any(
            value.get(name) is not None
            for name in ("worker_id", "operation_id", "project_id", "revision_selector")
        ):
            selected = CodingWorkerIdentityState(state)
            reason_value = value.get("reason", CodingWorkerIdentityReason.INVALID_FACTS.value)
            try:
                selected_reason = CodingWorkerIdentityReason(str(reason_value).casefold())
            except ValueError:
                selected_reason = CodingWorkerIdentityReason.INVALID_FACTS
            return CodingWorkerIdentityV1(
                f"{admission_id}:identity",
                turn,
                selected,
                None,
                None,
                None,
                None,
                selected_reason,
            )
        return build_coding_worker_identity(f"{admission_id}:identity", turn, value)
    raise CodingWorkerAdmissionError("identity component has invalid type")


def _coerce_isolation(value: object, admission_id: str, turn: str) -> CodingWorkerIsolationAdmissionV1:
    if isinstance(value, CodingWorkerIsolationAdmissionV1):
        return value
    if isinstance(value, Mapping):
        state = _mapping_state(value, "isolation", "admission", "state")
        if state in {"empty", "blocked"} and not any(
            value.get(name) is not None
            for name in (
                "host_secrets_visible",
                "docker_socket_present",
                "production_database_reachable",
                "owner_ssh_keys_visible",
            )
        ):
            selected = CodingWorkerIsolationState(state)
            reason_value = value.get("reason", CodingWorkerIsolationReason.INVALID_FACTS.value)
            try:
                selected_reason = CodingWorkerIsolationReason(str(reason_value).casefold())
            except ValueError:
                selected_reason = CodingWorkerIsolationReason.INVALID_FACTS
            return CodingWorkerIsolationAdmissionV1(
                f"{admission_id}:isolation", turn, selected, None, None, None, None, selected_reason
            )
        return build_coding_worker_isolation(f"{admission_id}:isolation", turn, value)
    raise CodingWorkerAdmissionError("isolation component has invalid type")


def _coerce_network(value: object, admission_id: str, turn: str) -> CodingWorkerNetworkPolicyV1:
    if isinstance(value, CodingWorkerNetworkPolicyV1):
        return value
    if isinstance(value, Mapping):
        state = _mapping_state(value, "network", "policy", "state")
        if state in {"empty", "blocked"} and not any(
            value.get(name) not in (None, (), [])
            for name in ("dependency_or_research_steps", "steps", "allowlist")
        ):
            selected = CodingWorkerNetworkState(state)
            reason_value = value.get("reason", CodingWorkerNetworkReason.INVALID_FACTS.value)
            try:
                selected_reason = CodingWorkerNetworkReason(str(reason_value).casefold())
            except ValueError:
                selected_reason = CodingWorkerNetworkReason.INVALID_FACTS
            return CodingWorkerNetworkPolicyV1(f"{admission_id}:network", turn, selected, (), selected_reason)
        return build_coding_worker_network(f"{admission_id}:network", turn, value)
    raise CodingWorkerAdmissionError("network component has invalid type")


def _coerce_workspace(value: object, admission_id: str, turn: str) -> CodingWorkerWorkspaceV1:
    if isinstance(value, CodingWorkerWorkspaceV1):
        return value
    if isinstance(value, Mapping):
        state = _mapping_state(value, "workspace", "binding", "state")
        if state in {"empty", "blocked"} and not any(
            value.get(name) is not None
            for name in (
                "operation_id",
                "project_root",
                "workspace_path",
                "input_snapshot_sha256",
                "export_path",
            )
        ):
            selected = CodingWorkerWorkspaceState(state)
            reason_value = value.get("reason", CodingWorkerWorkspaceReason.INVALID_FACTS.value)
            try:
                selected_reason = CodingWorkerWorkspaceReason(str(reason_value).casefold())
            except ValueError:
                selected_reason = CodingWorkerWorkspaceReason.INVALID_FACTS
            return CodingWorkerWorkspaceV1(
                f"{admission_id}:workspace", turn, selected, None, None, None, None, None, selected_reason
            )
        return build_coding_worker_workspace(f"{admission_id}:workspace", turn, value)
    raise CodingWorkerAdmissionError("workspace component has invalid type")


def _coerce_limits(value: object, admission_id: str, turn: str) -> CodingWorkerLimitsV1:
    if isinstance(value, CodingWorkerLimitsV1):
        return value
    if isinstance(value, Mapping):
        state = _mapping_state(value, "limits", "state")
        if state in {"empty", "blocked"} and not any(
            value.get(name) is not None
            for name in (
                "wall_clock_sec",
                "wall_clock_seconds",
                "memory_bytes",
                "memory",
                "cpu_sec",
                "cpu_seconds",
            )
        ):
            selected = CodingWorkerLimitsState(state)
            reason_value = value.get("reason", CodingWorkerLimitsReason.INVALID_FACTS.value)
            try:
                selected_reason = CodingWorkerLimitsReason(str(reason_value).casefold())
            except ValueError:
                selected_reason = CodingWorkerLimitsReason.INVALID_FACTS
            return CodingWorkerLimitsV1(
                f"{admission_id}:limits", turn, selected, None, None, None, selected_reason
            )
        return build_coding_worker_limits(f"{admission_id}:limits", turn, value)
    raise CodingWorkerAdmissionError("limits component has invalid type")


def _result(
    admission_id: str,
    turn: str,
    admission: CodingWorkerAdmissionState,
    reason: CodingWorkerAdmissionReason,
    *,
    components: tuple[
        CodingWorkerIdentityV1 | None,
        CodingWorkerIsolationAdmissionV1 | None,
        CodingWorkerNetworkPolicyV1 | None,
        CodingWorkerWorkspaceV1 | None,
        CodingWorkerLimitsV1 | None,
    ] = (None, None, None, None, None),
) -> CodingWorkerAdmissionV1:
    if admission is not CodingWorkerAdmissionState.ADMITTED:
        components = (None, None, None, None, None)
    return CodingWorkerAdmissionV1(admission_id, turn, admission, *components, reason)


def build_coding_worker_admission(
    admission_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: CodingWorkerAdmissionFactsV1 | Mapping[str, object] | None = None,
    *,
    identity: object = _MISSING,
    isolation: object = _MISSING,
    network: object = _MISSING,
    workspace: object = _MISSING,
    limits: object = _MISSING,
) -> CodingWorkerAdmissionV1:
    """Compose the five closed contracts into one admission decision."""

    if isinstance(admission_id, Mapping):
        raw = admission_id
        admission_id = raw.get("admission_id", "admission:worker")
        authenticated_turn_id = raw.get("authenticated_turn_id", authenticated_turn_id)
        if facts is not None or any(
            value is not _MISSING for value in (identity, isolation, network, workspace, limits)
        ):
            raise CodingWorkerAdmissionError("admission mapping and explicit facts cannot be mixed")
        facts = raw
    _identifier(admission_id, field="admission_id", maximum=MAX_ADMISSION_ID_CHARS)
    _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    try:
        explicit = (identity, isolation, network, workspace, limits)
        raw_components = explicit if any(value is not _MISSING for value in explicit) else _facts(facts)
        if any(value is not _MISSING for value in explicit) and facts is not None:
            raise CodingWorkerAdmissionError("facts and explicit components cannot both be supplied")
    except CodingWorkerAdmissionError:
        return _result(
            cast(str, admission_id),
            cast(str, authenticated_turn_id),
            CodingWorkerAdmissionState.BLOCKED,
            CodingWorkerAdmissionReason.INVALID_FACTS,
        )
    if all(value is _MISSING or value is None for value in raw_components):
        return _result(
            cast(str, admission_id),
            cast(str, authenticated_turn_id),
            CodingWorkerAdmissionState.EMPTY,
            CodingWorkerAdmissionReason.NO_FACTS,
        )
    if any(value is _MISSING or value is None for value in raw_components):
        return _result(
            cast(str, admission_id),
            cast(str, authenticated_turn_id),
            CodingWorkerAdmissionState.BLOCKED,
            CodingWorkerAdmissionReason.MISSING_COMPONENT,
        )
    try:
        identity_result = _coerce_identity(
            raw_components[0], cast(str, admission_id), cast(str, authenticated_turn_id)
        )
        isolation_result = _coerce_isolation(
            raw_components[1], cast(str, admission_id), cast(str, authenticated_turn_id)
        )
        network_result = _coerce_network(
            raw_components[2], cast(str, admission_id), cast(str, authenticated_turn_id)
        )
        workspace_result = _coerce_workspace(
            raw_components[3], cast(str, admission_id), cast(str, authenticated_turn_id)
        )
        limits_result = _coerce_limits(
            raw_components[4], cast(str, admission_id), cast(str, authenticated_turn_id)
        )
    except (CodingWorkerAdmissionError, TypeError, ValueError):
        return _result(
            cast(str, admission_id),
            cast(str, authenticated_turn_id),
            CodingWorkerAdmissionState.BLOCKED,
            CodingWorkerAdmissionReason.INVALID_FACTS,
        )
    components = (identity_result, isolation_result, network_result, workspace_result, limits_result)
    states = (
        identity_result.identity,
        isolation_result.isolation,
        network_result.network,
        workspace_result.workspace,
        limits_result.limits,
    )
    if any(state is CodingWorkerIdentityState.BLOCKED for state in states[:1]):
        reason = CodingWorkerAdmissionReason.IDENTITY_BLOCKED
    elif any(state is CodingWorkerIsolationState.BLOCKED for state in states[1:2]):
        reason = CodingWorkerAdmissionReason.ISOLATION_BLOCKED
    elif any(state is CodingWorkerNetworkState.BLOCKED for state in states[2:3]):
        reason = CodingWorkerAdmissionReason.NETWORK_BLOCKED
    elif any(state is CodingWorkerWorkspaceState.BLOCKED for state in states[3:4]):
        reason = CodingWorkerAdmissionReason.WORKSPACE_BLOCKED
    elif any(state is CodingWorkerLimitsState.BLOCKED for state in states[4:5]):
        reason = CodingWorkerAdmissionReason.LIMITS_BLOCKED
    elif any(state.value == "empty" for state in states):
        reason = CodingWorkerAdmissionReason.COMPONENT_EMPTY
    elif identity_result.identity is not CodingWorkerIdentityState.IDENTIFIED:
        reason = CodingWorkerAdmissionReason.IDENTITY_NOT_IDENTIFIED
    elif isolation_result.isolation is not CodingWorkerIsolationState.ADMITTED:
        reason = CodingWorkerAdmissionReason.ISOLATION_NOT_ADMITTED
    elif network_result.network not in {
        CodingWorkerNetworkState.DISABLED,
        CodingWorkerNetworkState.BOUNDED,
    }:
        reason = CodingWorkerAdmissionReason.NETWORK_NOT_BOUNDED
    elif workspace_result.workspace is not CodingWorkerWorkspaceState.BOUND:
        reason = CodingWorkerAdmissionReason.WORKSPACE_NOT_BOUND
    elif limits_result.limits is not CodingWorkerLimitsState.BOUNDED:
        reason = CodingWorkerAdmissionReason.LIMITS_NOT_BOUNDED
    elif any(component.authenticated_turn_id != authenticated_turn_id for component in components):
        reason = CodingWorkerAdmissionReason.AUTHENTICATED_TURN_MISMATCH
    elif identity_result.operation_id != workspace_result.operation_id:
        reason = CodingWorkerAdmissionReason.OPERATION_MISMATCH
    else:
        return _result(
            cast(str, admission_id),
            cast(str, authenticated_turn_id),
            CodingWorkerAdmissionState.ADMITTED,
            CodingWorkerAdmissionReason.ADMITTED,
            components=components,
        )
    return _result(
        cast(str, admission_id),
        cast(str, authenticated_turn_id),
        CodingWorkerAdmissionState.BLOCKED,
        reason,
    )


build_coding_worker_admission_contract = build_coding_worker_admission


def validate_coding_worker_admission(value: Mapping[str, object]) -> bool:
    """Return whether a mapping is a valid serialized admission result."""

    try:
        if value.get("schema") != CODING_WORKER_ADMISSION_SCHEMA:
            return False
        admission_id = cast(str, value.get("admission_id"))
        turn = cast(str, value.get("authenticated_turn_id"))
        state_value = value.get("admission", value.get("state"))
        if state_value in {
            CodingWorkerAdmissionState.EMPTY.value,
            CodingWorkerAdmissionState.BLOCKED.value,
        }:
            state = CodingWorkerAdmissionState(state_value)
            reason = CodingWorkerAdmissionReason(cast(str, value.get("reason")))
            result = CodingWorkerAdmissionV1(admission_id, turn, state, None, None, None, None, None, reason)
        else:
            result = build_coding_worker_admission(admission_id, turn, value)
        return result.to_mapping() == dict(value)
    except (CodingWorkerAdmissionError, TypeError, ValueError):
        return False
