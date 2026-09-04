"""Read-only availability facts for one shared-operation capability.

Availability is intentionally not authority.  The contract records whether an
already-supplied capability can be observed; it never resolves tools or grants
permission to execute one.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

SHARED_OPERATION_CAPABILITY_SCHEMA = "friday.shared-operation-capability.v1"
MAX_CAPABILITY_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class SharedOperationCapabilityError(ValueError):
    """A capability fact or serialized availability is malformed."""


class SharedOperationCapabilityState(StrEnum):
    EMPTY = "empty"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


class SharedOperationCapabilityReason(StrEnum):
    NO_FACTS = "no_facts"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MISSING_AVAILABILITY = "missing_availability"
    CAPABILITY_ID_INVALID = "capability_id_invalid"
    AVAILABILITY_INVALID = "availability_invalid"
    AUTHORITY_CLAIM = "authority_claim"
    INVALID_FACTS = "invalid_facts"


@dataclass(frozen=True, slots=True)
class SharedOperationCapabilityFactsV1:
    """Caller-supplied capability identity and availability only."""

    capability_id: str | None = None
    available: bool | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise SharedOperationCapabilityError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _safe_id(value: object, *, field: str) -> str:
    result = _identifier(value, field=field)
    if any(unicodedata.category(char).startswith("C") for char in result):
        _fail(field, "control")
    return result


def _state(value: object) -> SharedOperationCapabilityState:
    try:
        return SharedOperationCapabilityState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise SharedOperationCapabilityError("capability_closed") from exc


def _reason(value: object) -> SharedOperationCapabilityReason:
    try:
        return SharedOperationCapabilityReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise SharedOperationCapabilityError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class SharedOperationCapabilityV1:
    """Immutable availability projection with no execution authority."""

    capability_id: str
    authenticated_turn_id: str
    capability: SharedOperationCapabilityState
    available: bool | None
    reason: SharedOperationCapabilityReason

    def __post_init__(self) -> None:
        _identifier(self.capability_id, field="capability_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        state = _state(self.capability)
        reason = _reason(self.reason)
        object.__setattr__(self, "capability", state)
        object.__setattr__(self, "reason", reason)
        if state is SharedOperationCapabilityState.AVAILABLE:
            if self.available is not True:
                _fail("available", "mismatch")
        elif state is SharedOperationCapabilityState.UNAVAILABLE:
            if self.available is not False:
                _fail("available", "mismatch")
        elif self.available is not None:
            _fail("non_available", "exposed")

    @property
    def state(self) -> SharedOperationCapabilityState:
        return self.capability

    @property
    def availability(self) -> SharedOperationCapabilityState:
        return self.capability

    @property
    def closed_capability(self) -> SharedOperationCapabilityState:
        return self.capability

    @property
    def decision(self) -> SharedOperationCapabilityState:
        return self.capability

    @property
    def closed_reason(self) -> SharedOperationCapabilityReason:
        return self.reason

    @property
    def is_available(self) -> bool:
        return self.capability is SharedOperationCapabilityState.AVAILABLE

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": SHARED_OPERATION_CAPABILITY_SCHEMA,
            "capability_id": self.capability_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "capability": self.capability.value,
            "available": self.available,
            "reason": self.reason.value,
        }


CapabilityState = SharedOperationCapabilityState
CapabilityReason = SharedOperationCapabilityReason
SharedOperationCapability = SharedOperationCapabilityV1
SharedOperationCapabilityFacts = SharedOperationCapabilityFactsV1


def _availability(value: object) -> bool:
    if type(value) is bool:
        return cast(bool, value)
    if type(value) is str:
        normalised = value.strip().casefold()
        if normalised in {"available", "present", "ready", "true"}:
            return True
        if normalised in {"unavailable", "absent", "missing", "false"}:
            return False
    _fail("availability")


def _facts(value: object) -> tuple[object, object]:
    if isinstance(value, SharedOperationCapabilityFactsV1):
        return value.capability_id, value.available
    if not isinstance(value, Mapping):
        _fail("facts", "type")
    allowed = {
        "capability_id",
        "capability",
        "id",
        "available",
        "availability",
        "present",
        "enabled",
    }
    extras = set(value) - allowed
    if extras:
        if any(
            any(
                word in str(key).casefold()
                for word in ("authority", "execute", "effect", "token", "permission")
            )
            for key in extras
        ):
            _fail("authority", "claim")
        _fail("facts", "unknown_fields")
    capability_id = value.get("capability_id", value.get("capability", value.get("id")))
    availability = value.get(
        "available",
        value.get("availability", value.get("present", value.get("enabled"))),
    )
    return capability_id, availability


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "capability_id",
        "capability",
        "id",
        "authenticated_turn_id",
        "facts",
        "available",
        "availability",
        "present",
        "enabled",
        "state",
        "reason",
    }
    if set(raw) - known:
        _fail("capability", "unknown_fields")


def _result(
    capability_id: str,
    turn_id: str,
    state: SharedOperationCapabilityState,
    reason: SharedOperationCapabilityReason,
    available: bool | None = None,
) -> SharedOperationCapabilityV1:
    if state is SharedOperationCapabilityState.AVAILABLE:
        available = True
    elif state is SharedOperationCapabilityState.UNAVAILABLE:
        available = False
    else:
        available = None
    return SharedOperationCapabilityV1(
        capability_id=capability_id,
        authenticated_turn_id=turn_id,
        capability=state,
        available=available,
        reason=reason,
    )


def build_shared_operation_capability(
    capability_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: SharedOperationCapabilityFactsV1 | Mapping[str, object] | None = None,
    *,
    available: object = None,
    availability: object = None,
) -> SharedOperationCapabilityV1:
    """Build an availability result from already-supplied facts."""

    if isinstance(capability_id, Mapping):
        raw = capability_id
        try:
            _known_mapping_keys(raw)
            if raw.get("schema", SHARED_OPERATION_CAPABILITY_SCHEMA) != SHARED_OPERATION_CAPABILITY_SCHEMA:
                _fail("schema")
            if "state" in raw or "reason" in raw:
                if "facts" in raw:
                    _fail("capability", "duplicate_representations")
                return SharedOperationCapabilityV1(
                    capability_id=cast(str, raw.get("capability_id", raw.get("id", raw.get("capability")))),
                    authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                    capability=cast(SharedOperationCapabilityState, raw.get("state", raw.get("capability"))),
                    available=cast(bool | None, raw.get("available")),
                    reason=cast(SharedOperationCapabilityReason, raw.get("reason")),
                )
            capability_id = cast(str, raw.get("capability_id", raw.get("id", raw.get("capability"))))
            authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
            if "facts" in raw:
                facts = raw["facts"]
            else:
                facts = dict(raw)
                for key in ("schema", "capability_id", "id", "authenticated_turn_id"):
                    facts.pop(key, None)
        except (TypeError, ValueError):
            capability_id = cast(str, raw.get("capability_id", raw.get("id", "capability")))
            authenticated_turn_id = cast(str, raw.get("authenticated_turn_id", "turn"))
            capability_key = _identifier(capability_id, field="capability_id")
            turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
            return _result(
                capability_key,
                turn_key,
                SharedOperationCapabilityState.BLOCKED,
                SharedOperationCapabilityReason.INVALID_FACTS,
            )

    capability_key = _identifier(capability_id, field="capability_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if facts is not None and (available is not None or availability is not None):
            _fail("facts", "duplicate_arguments")
        if facts is not None:
            capability_fact, availability_fact = _facts(facts)
        else:
            if available is None and availability is None:
                return _result(
                    capability_key,
                    turn_key,
                    SharedOperationCapabilityState.EMPTY,
                    SharedOperationCapabilityReason.NO_FACTS,
                )
            capability_fact, availability_fact = (
                capability_key,
                (available if available is not None else availability),
            )
        if capability_fact is None and availability_fact is None:
            return _result(
                capability_key,
                turn_key,
                SharedOperationCapabilityState.EMPTY,
                SharedOperationCapabilityReason.NO_FACTS,
            )
        if capability_fact is None:
            _fail("capability_id")
        if availability_fact is None:
            return _result(
                capability_key,
                turn_key,
                SharedOperationCapabilityState.BLOCKED,
                SharedOperationCapabilityReason.MISSING_AVAILABILITY,
            )
        capability_value = _safe_id(capability_fact, field="capability_id")
        available_value = _availability(availability_fact)
    except SharedOperationCapabilityError as exc:
        code = str(exc)
        reason = (
            SharedOperationCapabilityReason.AUTHORITY_CLAIM
            if "authority" in code
            else SharedOperationCapabilityReason.CAPABILITY_ID_INVALID
            if "capability_id" in code
            else SharedOperationCapabilityReason.AVAILABILITY_INVALID
            if "availability" in code
            else SharedOperationCapabilityReason.INVALID_FACTS
        )
        return _result(
            capability_key,
            turn_key,
            SharedOperationCapabilityState.BLOCKED,
            reason,
        )
    return _result(
        capability_value,
        turn_key,
        SharedOperationCapabilityState.AVAILABLE
        if available_value
        else SharedOperationCapabilityState.UNAVAILABLE,
        SharedOperationCapabilityReason.AVAILABLE
        if available_value
        else SharedOperationCapabilityReason.UNAVAILABLE,
    )


def validate_shared_operation_capability(value: object) -> bool:
    try:
        if isinstance(value, SharedOperationCapabilityV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping) or value.get("schema") != SHARED_OPERATION_CAPABILITY_SCHEMA:
            return False
        required = {
            "schema",
            "capability_id",
            "authenticated_turn_id",
            "capability",
            "available",
            "reason",
        }
        if set(value) != required:
            return False
        SharedOperationCapabilityV1(
            capability_id=cast(str, value.get("capability_id")),
            authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
            capability=cast(SharedOperationCapabilityState, value.get("capability")),
            available=cast(bool | None, value.get("available")),
            reason=cast(SharedOperationCapabilityReason, value.get("reason")),
        )
        return True
    except (TypeError, ValueError):
        return False


build_operation_capability = build_shared_operation_capability
validate_operation_capability = validate_shared_operation_capability


__all__ = [
    "SHARED_OPERATION_CAPABILITY_SCHEMA",
    "CapabilityReason",
    "CapabilityState",
    "SharedOperationCapability",
    "SharedOperationCapabilityError",
    "SharedOperationCapabilityFacts",
    "SharedOperationCapabilityFactsV1",
    "SharedOperationCapabilityReason",
    "SharedOperationCapabilityState",
    "SharedOperationCapabilityV1",
    "build_operation_capability",
    "build_shared_operation_capability",
    "validate_operation_capability",
    "validate_shared_operation_capability",
]
