"""Advisory secondary-availability facts for a shared operation.

The secondary is an optional observer.  This contract deliberately contains
no tool owner, effect owner, publisher, or authority token, and its absence
never changes primary ownership.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

SHARED_OPERATION_SECONDARY_SCHEMA = "friday.shared-operation-secondary.v1"
MAX_SECONDARY_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


class SharedOperationSecondaryError(ValueError):
    """A secondary-availability fact or result is malformed."""


class SharedOperationSecondaryState(StrEnum):
    EMPTY = "empty"
    PRESENT = "present"
    ABSENT = "absent"
    BLOCKED = "blocked"


class SharedOperationSecondaryReason(StrEnum):
    NO_FACTS = "no_facts"
    PRESENT = "present"
    ABSENT = "absent"
    AVAILABILITY_INVALID = "availability_invalid"
    SECONDARY_DIGEST_INVALID = "secondary_digest_invalid"
    OWNERSHIP_CLAIM = "ownership_claim"
    INVALID_FACTS = "invalid_facts"


@dataclass(frozen=True, slots=True)
class SharedOperationSecondaryFactsV1:
    """Caller-supplied presence and optional opaque secondary digest."""

    present: bool | None = None
    secondary_digest: str | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise SharedOperationSecondaryError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail("secondary_digest")
    return cast(str, value)


def _state(value: object) -> SharedOperationSecondaryState:
    try:
        return SharedOperationSecondaryState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise SharedOperationSecondaryError("secondary_closed") from exc


def _reason(value: object) -> SharedOperationSecondaryReason:
    try:
        return SharedOperationSecondaryReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise SharedOperationSecondaryError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class SharedOperationSecondaryV1:
    """Immutable advisory secondary state."""

    secondary_id: str
    authenticated_turn_id: str
    secondary: SharedOperationSecondaryState
    present: bool | None
    secondary_digest: str | None
    reason: SharedOperationSecondaryReason

    def __post_init__(self) -> None:
        _identifier(self.secondary_id, field="secondary_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        state = _state(self.secondary)
        reason = _reason(self.reason)
        object.__setattr__(self, "secondary", state)
        object.__setattr__(self, "reason", reason)
        if state is SharedOperationSecondaryState.PRESENT:
            if self.present is not True:
                _fail("present", "mismatch")
            if self.secondary_digest is not None:
                _digest(self.secondary_digest)
        elif state is SharedOperationSecondaryState.ABSENT:
            if self.present is not False or self.secondary_digest is not None:
                _fail("absent", "exposes_facts")
        elif self.present is not None or self.secondary_digest is not None:
            _fail("non_admitted", "exposes_facts")

    @property
    def state(self) -> SharedOperationSecondaryState:
        return self.secondary

    @property
    def secondary_state(self) -> SharedOperationSecondaryState:
        return self.secondary

    @property
    def availability(self) -> SharedOperationSecondaryState:
        return self.secondary

    @property
    def closed_secondary(self) -> SharedOperationSecondaryState:
        return self.secondary

    @property
    def decision(self) -> SharedOperationSecondaryState:
        return self.secondary

    @property
    def closed_reason(self) -> SharedOperationSecondaryReason:
        return self.reason

    @property
    def is_present(self) -> bool:
        return self.secondary is SharedOperationSecondaryState.PRESENT

    @property
    def available(self) -> bool | None:
        return self.present

    @property
    def can_own_tools(self) -> bool:
        return False

    @property
    def can_own_effects(self) -> bool:
        return False

    @property
    def can_publish(self) -> bool:
        return False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": SHARED_OPERATION_SECONDARY_SCHEMA,
            "secondary_id": self.secondary_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "secondary": self.secondary.value,
            "present": self.present,
            "secondary_digest": self.secondary_digest,
            "reason": self.reason.value,
        }


SecondaryState = SharedOperationSecondaryState
SecondaryReason = SharedOperationSecondaryReason
SharedOperationSecondary = SharedOperationSecondaryV1
SharedOperationSecondaryFacts = SharedOperationSecondaryFactsV1


def _presence(value: object) -> bool:
    if type(value) is bool:
        return cast(bool, value)
    if type(value) is str:
        value = value.strip().casefold()
        if value in {"present", "available", "true"}:
            return True
        if value in {"absent", "unavailable", "false"}:
            return False
    _fail("availability")


def _facts(value: object) -> tuple[object, object]:
    if isinstance(value, SharedOperationSecondaryFactsV1):
        return value.present, value.secondary_digest
    if not isinstance(value, Mapping):
        _fail("facts", "type")
    allowed = {
        "present",
        "secondary_present",
        "available",
        "availability",
        "secondary_digest",
        "digest",
    }
    extras = set(value) - allowed
    if extras:
        ownership_words = ("owner", "tool", "effect", "publish", "authority", "token")
        if any(any(word in str(key).casefold() for word in ownership_words) for key in extras):
            _fail("ownership", "claim")
        _fail("facts", "unknown_fields")
    presence = value.get(
        "present",
        value.get("secondary_present", value.get("available", value.get("availability"))),
    )
    digest = value.get("secondary_digest", value.get("digest"))
    return presence, digest


def _known_mapping_keys(raw: Mapping[str, Any]) -> None:
    known = {
        "schema",
        "secondary_id",
        "id",
        "authenticated_turn_id",
        "facts",
        "present",
        "secondary_present",
        "available",
        "availability",
        "secondary_digest",
        "digest",
        "secondary",
        "state",
        "reason",
    }
    if set(raw) - known:
        _fail("secondary", "unknown_fields")


def _result(
    secondary_id: str,
    turn_id: str,
    state: SharedOperationSecondaryState,
    reason: SharedOperationSecondaryReason,
    *,
    digest: str | None = None,
) -> SharedOperationSecondaryV1:
    if state is SharedOperationSecondaryState.PRESENT:
        return SharedOperationSecondaryV1(secondary_id, turn_id, state, True, digest, reason)
    if state is SharedOperationSecondaryState.ABSENT:
        return SharedOperationSecondaryV1(secondary_id, turn_id, state, False, None, reason)
    return SharedOperationSecondaryV1(secondary_id, turn_id, state, None, None, reason)


def build_shared_operation_secondary(
    secondary_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: SharedOperationSecondaryFactsV1 | Mapping[str, object] | None = None,
    *,
    present: object = None,
    secondary_digest: object = None,
) -> SharedOperationSecondaryV1:
    """Build advisory secondary state without assigning any ownership."""

    if isinstance(secondary_id, Mapping):
        raw = secondary_id
        try:
            _known_mapping_keys(raw)
            if raw.get("schema", SHARED_OPERATION_SECONDARY_SCHEMA) != SHARED_OPERATION_SECONDARY_SCHEMA:
                _fail("schema")
            if "reason" in raw or "state" in raw:
                if "facts" in raw:
                    _fail("secondary", "duplicate_representations")
                return SharedOperationSecondaryV1(
                    secondary_id=cast(str, raw.get("secondary_id", raw.get("id"))),
                    authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                    secondary=cast(SharedOperationSecondaryState, raw.get("secondary", raw.get("state"))),
                    present=cast(bool | None, raw.get("present")),
                    secondary_digest=cast(str | None, raw.get("secondary_digest")),
                    reason=cast(SharedOperationSecondaryReason, raw.get("reason")),
                )
            secondary_id = cast(str, raw.get("secondary_id", raw.get("id")))
            authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
            if "facts" in raw:
                facts = raw["facts"]
            else:
                facts = dict(raw)
                for key in ("schema", "secondary_id", "id", "authenticated_turn_id"):
                    facts.pop(key, None)
        except (TypeError, ValueError):
            secondary_id = cast(str, raw.get("secondary_id", raw.get("id", "secondary")))
            authenticated_turn_id = cast(str, raw.get("authenticated_turn_id", "turn"))
            secondary_key = _identifier(secondary_id, field="secondary_id")
            turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
            return _result(
                secondary_key,
                turn_key,
                SharedOperationSecondaryState.BLOCKED,
                SharedOperationSecondaryReason.INVALID_FACTS,
            )

    secondary_key = _identifier(secondary_id, field="secondary_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if facts is not None and (present is not None or secondary_digest is not None):
            _fail("facts", "duplicate_arguments")
        if facts is not None:
            presence_fact, digest_fact = _facts(facts)
        else:
            presence_fact, digest_fact = present, secondary_digest
        if presence_fact is None and digest_fact is None:
            return _result(
                secondary_key,
                turn_key,
                SharedOperationSecondaryState.EMPTY,
                SharedOperationSecondaryReason.NO_FACTS,
            )
        if presence_fact is None:
            _fail("availability")
        presence = _presence(presence_fact)
        digest = None if digest_fact is None else _digest(digest_fact)
        if not presence and digest is not None:
            _fail("absent", "digest")
    except SharedOperationSecondaryError as exc:
        code = str(exc)
        reason = (
            SharedOperationSecondaryReason.OWNERSHIP_CLAIM
            if "ownership" in code
            else SharedOperationSecondaryReason.SECONDARY_DIGEST_INVALID
            if "digest" in code
            else SharedOperationSecondaryReason.AVAILABILITY_INVALID
            if "availability" in code
            else SharedOperationSecondaryReason.INVALID_FACTS
        )
        return _result(secondary_key, turn_key, SharedOperationSecondaryState.BLOCKED, reason)
    if presence:
        return _result(
            secondary_key,
            turn_key,
            SharedOperationSecondaryState.PRESENT,
            SharedOperationSecondaryReason.PRESENT,
            digest=digest,
        )
    return _result(
        secondary_key, turn_key, SharedOperationSecondaryState.ABSENT, SharedOperationSecondaryReason.ABSENT
    )


def validate_shared_operation_secondary(value: object) -> bool:
    try:
        if isinstance(value, SharedOperationSecondaryV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping) or value.get("schema") != SHARED_OPERATION_SECONDARY_SCHEMA:
            return False
        required = {
            "schema",
            "secondary_id",
            "authenticated_turn_id",
            "secondary",
            "present",
            "secondary_digest",
            "reason",
        }
        if set(value) != required:
            return False
        SharedOperationSecondaryV1(
            secondary_id=cast(str, value.get("secondary_id")),
            authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
            secondary=cast(SharedOperationSecondaryState, value.get("secondary")),
            present=cast(bool | None, value.get("present")),
            secondary_digest=cast(str | None, value.get("secondary_digest")),
            reason=cast(SharedOperationSecondaryReason, value.get("reason")),
        )
        return True
    except (TypeError, ValueError):
        return False


build_operation_secondary = build_shared_operation_secondary
validate_operation_secondary = validate_shared_operation_secondary


__all__ = [
    "SHARED_OPERATION_SECONDARY_SCHEMA",
    "SecondaryReason",
    "SecondaryState",
    "SharedOperationSecondary",
    "SharedOperationSecondaryError",
    "SharedOperationSecondaryFacts",
    "SharedOperationSecondaryFactsV1",
    "SharedOperationSecondaryReason",
    "SharedOperationSecondaryState",
    "SharedOperationSecondaryV1",
    "build_operation_secondary",
    "build_shared_operation_secondary",
    "validate_operation_secondary",
    "validate_shared_operation_secondary",
]
