"""Pure closed network policy for an isolated Coding worker.

The policy describes what a future worker may be given.  It does not create a
socket, resolve a host, contact a dependency, or inspect a network namespace.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

CODING_WORKER_NETWORK_SCHEMA = "friday.coding-worker-network.v1"
MAX_NETWORK_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_NETWORK_STEPS = 8
CODING_WORKER_NETWORK_ALLOWLIST = frozenset({"dependency", "research"})
NETWORK_ALLOWLIST = CODING_WORKER_NETWORK_ALLOWLIST

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MISSING = object()


class CodingWorkerNetworkError(ValueError):
    """A network-policy fact or directly constructed result is malformed."""


class CodingWorkerNetworkState(StrEnum):
    """Closed outcomes for the worker network policy."""

    EMPTY = "empty"
    DISABLED = "disabled"
    BOUNDED = "bounded"
    BLOCKED = "blocked"


class CodingWorkerNetworkReason(StrEnum):
    """Non-sensitive reason for one network-policy outcome."""

    NO_FACTS = "no_facts"
    NETWORK_DISABLED = "network_disabled"
    ALLOWLIST_BOUNDED = "allowlist_bounded"
    MISSING_POLICY = "missing_policy"
    HOST_NETWORK = "host_network"
    UNBOUNDED = "unbounded"
    EMPTY_ALLOWLIST = "empty_allowlist"
    UNKNOWN_ALLOWLIST_STEP = "unknown_allowlist_step"
    TOO_MANY_STEPS = "too_many_steps"
    CONFLICTING_FACTS = "conflicting_facts"
    INVALID_FACTS = "invalid_facts"


def _identifier(value: object, *, field: str, maximum: int) -> str:
    if type(value) is not str or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingWorkerNetworkError(f"{field} must be a bounded opaque identifier")
    return cast(str, value)


def _state(value: object) -> CodingWorkerNetworkState:
    if isinstance(value, CodingWorkerNetworkState):
        return value
    if type(value) is not str:
        raise CodingWorkerNetworkError("network must be a closed value")
    try:
        return CodingWorkerNetworkState(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerNetworkError("network must be a closed value") from exc


def _reason(value: object) -> CodingWorkerNetworkReason:
    if isinstance(value, CodingWorkerNetworkReason):
        return value
    if type(value) is not str:
        raise CodingWorkerNetworkError("network reason must be a closed value")
    try:
        return CodingWorkerNetworkReason(value.strip().casefold())
    except ValueError as exc:
        raise CodingWorkerNetworkError("network reason must be a closed value") from exc


def _strict_bool(value: object, *, field: str) -> bool:
    if type(value) is not bool:
        raise CodingWorkerNetworkError(f"{field} must be boolean")
    return cast(bool, value)


def _mode(value: object) -> CodingWorkerNetworkState:
    if isinstance(value, CodingWorkerNetworkState):
        return value
    if type(value) is not str:
        raise CodingWorkerNetworkError("network policy must be a closed value")
    aliases = {"disabled": CodingWorkerNetworkState.DISABLED, "bounded": CodingWorkerNetworkState.BOUNDED}
    try:
        return aliases[value.strip().casefold()]
    except KeyError as exc:
        raise CodingWorkerNetworkError("network policy must be disabled or bounded") from exc


def _steps(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise CodingWorkerNetworkError("dependency_or_research_steps must be a sequence")
    if isinstance(value, (set, frozenset)):
        value = tuple(sorted(value, key=repr))
    if not isinstance(value, Sequence):
        raise CodingWorkerNetworkError("dependency_or_research_steps must be a sequence")
    if not value or len(value) > MAX_NETWORK_STEPS:
        raise CodingWorkerNetworkError("dependency_or_research_steps has invalid size")
    normalized: list[str] = []
    for step in value:
        if type(step) is not str:
            raise CodingWorkerNetworkError("network allowlist step must be text")
        canonical = step.strip().casefold()
        if canonical not in CODING_WORKER_NETWORK_ALLOWLIST:
            raise CodingWorkerNetworkError("network allowlist step is not closed")
        if canonical in normalized:
            raise CodingWorkerNetworkError("network allowlist cannot contain duplicates")
        normalized.append(canonical)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class CodingWorkerNetworkFactsV1:
    """Caller-supplied network policy facts."""

    policy: str | CodingWorkerNetworkState | None = None
    dependency_or_research_steps: tuple[str, ...] | None = None
    host_network: bool | None = None
    unbounded: bool | None = None


@dataclass(frozen=True, slots=True)
class CodingWorkerNetworkPolicyV1:
    """Immutable network policy; no implicit host-network capability exists."""

    network_id: str
    authenticated_turn_id: str
    network: CodingWorkerNetworkState
    dependency_or_research_steps: tuple[str, ...]
    reason: CodingWorkerNetworkReason

    def __post_init__(self) -> None:
        _identifier(self.network_id, field="network_id", maximum=MAX_NETWORK_ID_CHARS)
        _identifier(
            self.authenticated_turn_id,
            field="authenticated_turn_id",
            maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
        )
        network = _state(self.network)
        reason = _reason(self.reason)
        object.__setattr__(self, "network", network)
        object.__setattr__(self, "reason", reason)
        if network is CodingWorkerNetworkState.BOUNDED:
            object.__setattr__(
                self, "dependency_or_research_steps", _steps(self.dependency_or_research_steps)
            )
        elif self.dependency_or_research_steps:
            raise CodingWorkerNetworkError("non-bounded network cannot expose an allowlist")
        else:
            object.__setattr__(self, "dependency_or_research_steps", ())
        if network is CodingWorkerNetworkState.EMPTY and reason is not CodingWorkerNetworkReason.NO_FACTS:
            raise CodingWorkerNetworkError("empty network needs no_facts reason")

    @property
    def state(self) -> CodingWorkerNetworkState:
        return self.network

    @property
    def policy(self) -> CodingWorkerNetworkState:
        return self.network

    @property
    def decision(self) -> CodingWorkerNetworkState:
        return self.network

    @property
    def closed_policy(self) -> CodingWorkerNetworkState:
        return self.network

    @property
    def closed_reason(self) -> CodingWorkerNetworkReason:
        return self.reason

    @property
    def allowlist(self) -> tuple[str, ...]:
        return self.dependency_or_research_steps

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_WORKER_NETWORK_SCHEMA,
            "network_id": self.network_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "network": self.network.value,
            "dependency_or_research_steps": list(self.dependency_or_research_steps),
            "reason": self.reason.value,
        }


WorkerNetworkState = CodingWorkerNetworkState
WorkerNetworkReason = CodingWorkerNetworkReason
CodingWorkerNetwork = CodingWorkerNetworkPolicyV1
CodingWorkerNetworkDecision = CodingWorkerNetworkState
CodingWorkerNetworkPolicy = CodingWorkerNetworkPolicyV1
CodingWorkerNetworkPolicyState = CodingWorkerNetworkState
CodingWorkerNetworkPolicyReason = CodingWorkerNetworkReason
CodingWorkerNetworkPolicyFactsV1 = CodingWorkerNetworkFactsV1
CODING_WORKER_NETWORK_POLICY_SCHEMA = CODING_WORKER_NETWORK_SCHEMA


def _mapping_facts(value: Mapping[str, object]) -> tuple[object, object, object, object]:
    allowed = {
        "schema",
        "network_id",
        "authenticated_turn_id",
        "network",
        "state",
        "policy",
        "mode",
        "network_policy",
        "reason",
        "dependency_or_research_steps",
        "steps",
        "allowlist",
        "host_network",
        "host_network_enabled",
        "unbounded",
        "unbounded_network",
    }
    if set(value) - allowed:
        raise CodingWorkerNetworkError("network facts contain unknown fields")
    if value.get("schema", CODING_WORKER_NETWORK_SCHEMA) != CODING_WORKER_NETWORK_SCHEMA:
        raise CodingWorkerNetworkError("network schema is invalid")
    policy = value.get("policy", value.get("mode", value.get("network_policy", _MISSING)))
    if policy is _MISSING and "network" in value:
        policy = value["network"]
    steps = value.get(
        "dependency_or_research_steps",
        value.get("steps", value.get("allowlist", _MISSING)),
    )
    host = value.get("host_network", value.get("host_network_enabled", _MISSING))
    unbounded = value.get("unbounded", value.get("unbounded_network", _MISSING))
    return policy, steps, host, unbounded


def _facts(value: object) -> tuple[object, object, object, object]:
    if value is None:
        return _MISSING, _MISSING, _MISSING, _MISSING
    if isinstance(value, CodingWorkerNetworkFactsV1):
        return (
            value.policy,
            value.dependency_or_research_steps,
            value.host_network,
            value.unbounded,
        )
    if isinstance(value, Mapping):
        return _mapping_facts(value)
    raise CodingWorkerNetworkError("network facts must be a mapping or facts object")


def _result(
    network_id: str,
    authenticated_turn_id: str,
    network: CodingWorkerNetworkState,
    reason: CodingWorkerNetworkReason,
    *,
    steps: tuple[str, ...] = (),
) -> CodingWorkerNetworkPolicyV1:
    if network is not CodingWorkerNetworkState.BOUNDED:
        steps = ()
    return CodingWorkerNetworkPolicyV1(
        network_id=network_id,
        authenticated_turn_id=authenticated_turn_id,
        network=network,
        dependency_or_research_steps=steps,
        reason=reason,
    )


def build_coding_worker_network(
    network_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: CodingWorkerNetworkFactsV1 | Mapping[str, object] | None = None,
    *,
    policy: object = _MISSING,
    dependency_or_research_steps: object = _MISSING,
    host_network: object = _MISSING,
    unbounded: object = _MISSING,
) -> CodingWorkerNetworkPolicyV1:
    """Build a disabled or explicitly bounded policy from supplied facts."""

    if isinstance(network_id, Mapping):
        raw = network_id
        network_id = raw.get("network_id", "network:worker")
        authenticated_turn_id = raw.get("authenticated_turn_id", authenticated_turn_id)
        if facts is not None or any(
            value is not _MISSING for value in (policy, dependency_or_research_steps, host_network, unbounded)
        ):
            raise CodingWorkerNetworkError("network mapping and explicit facts cannot be mixed")
        facts = raw
    _identifier(network_id, field="network_id", maximum=MAX_NETWORK_ID_CHARS)
    _identifier(
        authenticated_turn_id,
        field="authenticated_turn_id",
        maximum=MAX_AUTHENTICATED_TURN_ID_CHARS,
    )
    try:
        explicit = (policy, dependency_or_research_steps, host_network, unbounded)
        raw_facts = explicit if any(value is not _MISSING for value in explicit) else _facts(facts)
        if any(value is not _MISSING for value in explicit) and facts is not None:
            raise CodingWorkerNetworkError("facts and explicit network facts cannot both be supplied")
    except CodingWorkerNetworkError:
        return _result(
            cast(str, network_id),
            cast(str, authenticated_turn_id),
            CodingWorkerNetworkState.BLOCKED,
            CodingWorkerNetworkReason.INVALID_FACTS,
        )
    if all(value is _MISSING or value is None for value in raw_facts):
        return _result(
            cast(str, network_id),
            cast(str, authenticated_turn_id),
            CodingWorkerNetworkState.EMPTY,
            CodingWorkerNetworkReason.NO_FACTS,
        )
    policy_fact, steps_fact, host_fact, unbounded_fact = raw_facts
    if host_fact is not _MISSING and type(host_fact) is not bool:
        return _result(
            cast(str, network_id),
            cast(str, authenticated_turn_id),
            CodingWorkerNetworkState.BLOCKED,
            CodingWorkerNetworkReason.INVALID_FACTS,
        )
    if unbounded_fact is not _MISSING and type(unbounded_fact) is not bool:
        return _result(
            cast(str, network_id),
            cast(str, authenticated_turn_id),
            CodingWorkerNetworkState.BLOCKED,
            CodingWorkerNetworkReason.INVALID_FACTS,
        )
    if host_fact is True:
        return _result(
            cast(str, network_id),
            cast(str, authenticated_turn_id),
            CodingWorkerNetworkState.BLOCKED,
            CodingWorkerNetworkReason.HOST_NETWORK,
        )
    if unbounded_fact is True:
        return _result(
            cast(str, network_id),
            cast(str, authenticated_turn_id),
            CodingWorkerNetworkState.BLOCKED,
            CodingWorkerNetworkReason.UNBOUNDED,
        )
    if policy_fact is _MISSING or policy_fact is None:
        return _result(
            cast(str, network_id),
            cast(str, authenticated_turn_id),
            CodingWorkerNetworkState.BLOCKED,
            CodingWorkerNetworkReason.MISSING_POLICY,
        )
    try:
        selected = _mode(policy_fact)
    except CodingWorkerNetworkError:
        return _result(
            cast(str, network_id),
            cast(str, authenticated_turn_id),
            CodingWorkerNetworkState.BLOCKED,
            CodingWorkerNetworkReason.INVALID_FACTS,
        )
    if selected is CodingWorkerNetworkState.DISABLED:
        if steps_fact not in (_MISSING, None, (), [], ()):
            return _result(
                cast(str, network_id),
                cast(str, authenticated_turn_id),
                CodingWorkerNetworkState.BLOCKED,
                CodingWorkerNetworkReason.CONFLICTING_FACTS,
            )
        return _result(
            cast(str, network_id),
            cast(str, authenticated_turn_id),
            CodingWorkerNetworkState.DISABLED,
            CodingWorkerNetworkReason.NETWORK_DISABLED,
        )
    if steps_fact is _MISSING or steps_fact is None:
        return _result(
            cast(str, network_id),
            cast(str, authenticated_turn_id),
            CodingWorkerNetworkState.BLOCKED,
            CodingWorkerNetworkReason.EMPTY_ALLOWLIST,
        )
    try:
        bounded_steps = _steps(steps_fact)
    except CodingWorkerNetworkError as exc:
        reason = CodingWorkerNetworkReason.INVALID_FACTS
        message = str(exc)
        if "closed" in message:
            reason = CodingWorkerNetworkReason.UNKNOWN_ALLOWLIST_STEP
        elif "size" in message:
            reason = CodingWorkerNetworkReason.TOO_MANY_STEPS
        elif "sequence" in message:
            reason = CodingWorkerNetworkReason.INVALID_FACTS
        return _result(
            cast(str, network_id), cast(str, authenticated_turn_id), CodingWorkerNetworkState.BLOCKED, reason
        )
    return _result(
        cast(str, network_id),
        cast(str, authenticated_turn_id),
        CodingWorkerNetworkState.BOUNDED,
        CodingWorkerNetworkReason.ALLOWLIST_BOUNDED,
        steps=bounded_steps,
    )


build_coding_worker_network_policy = build_coding_worker_network
build_coding_worker_network_admission = build_coding_worker_network


def default_coding_worker_network_policy(
    network_id: str,
    authenticated_turn_id: str,
) -> CodingWorkerNetworkPolicyV1:
    """Return the explicit safe default: networking is disabled."""

    return build_coding_worker_network(network_id, authenticated_turn_id, {"policy": "disabled"})


default_network_policy = default_coding_worker_network_policy


def validate_coding_worker_network(value: Mapping[str, object]) -> bool:
    """Return whether a mapping is a valid serialized network result."""

    try:
        if value.get("schema") != CODING_WORKER_NETWORK_SCHEMA:
            return False
        network_id = cast(str, value.get("network_id"))
        turn = cast(str, value.get("authenticated_turn_id"))
        state_value = value.get("network", value.get("policy", value.get("state")))
        if state_value in {
            CodingWorkerNetworkState.EMPTY.value,
            CodingWorkerNetworkState.BLOCKED.value,
        }:
            state = CodingWorkerNetworkState(state_value)
            reason = CodingWorkerNetworkReason(cast(str, value.get("reason")))
            result = CodingWorkerNetworkPolicyV1(network_id, turn, state, (), reason)
        else:
            result = build_coding_worker_network(network_id, turn, value)
        return result.to_mapping() == dict(value)
    except (CodingWorkerNetworkError, TypeError, ValueError):
        return False
