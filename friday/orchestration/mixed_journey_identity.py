"""Closed identity facts for a mixed-organ journey.

The identity is deliberately small: one operation id and one authenticated
turn id.  Owner claims are counted but never exposed as raw values.  This
module is a read-only admission contract; it does not bind a live owner.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

MIXED_JOURNEY_IDENTITY_SCHEMA = "friday.mixed-journey-identity.v1"
MAX_ID_CHARS = 128
MAX_OWNER_CLAIMS = 8
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class MixedJourneyIdentityError(ValueError):
    """A mixed-journey identity fact is malformed."""


class MixedJourneyIdentityState(StrEnum):
    EMPTY = "empty"
    BOUND = "bound"
    BLOCKED = "blocked"


class MixedJourneyIdentityReason(StrEnum):
    NO_FACTS = "no_facts"
    BOUND = "bound"
    INVALID_FACTS = "invalid_facts"
    MULTIPLE_EFFECT_OWNERS = "multiple_effect_owners"
    MULTIPLE_PUBLISHERS = "multiple_publishers"


@dataclass(frozen=True, slots=True)
class MixedJourneyIdentityFactsV1:
    operation_id: str | None = None
    authenticated_turn_id: str | None = None
    effect_owners: tuple[object, ...] = ()
    publishers: tuple[object, ...] = ()


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyIdentityError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _claims(value: object, *, field: str) -> int:
    if value is None:
        return 0
    if type(value) is int:
        if not 0 <= value <= MAX_OWNER_CLAIMS:
            _fail(field, "count")
        return cast(int, value)
    if isinstance(value, (str, bytes, bytearray)):
        values: tuple[object, ...] = (value,)
    elif isinstance(value, Sequence):
        values = tuple(value)
    else:
        _fail(field, "sequence")
    if len(values) > MAX_OWNER_CLAIMS:
        _fail(field, "count")
    for item in values:
        if type(item) is not str or not item or item != item.strip():
            _fail(field)
        if any(unicodedata.category(char).startswith("C") for char in item):
            _fail(field)
        if "://" in item or "/" in item or "\\" in item:
            _fail(field, "private")
    return len(set(cast(str, item) for item in values))


@dataclass(frozen=True, slots=True)
class MixedJourneyIdentityV1:
    identity_id: str
    authenticated_turn_id: str
    state: MixedJourneyIdentityState
    operation_id: str | None
    effect_owner_count: int
    publisher_count: int
    reason: MixedJourneyIdentityReason

    def __post_init__(self) -> None:
        _identifier(self.identity_id, field="identity_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        try:
            state = MixedJourneyIdentityState(self.state)
            reason = MixedJourneyIdentityReason(self.reason)
        except (TypeError, ValueError) as exc:
            raise MixedJourneyIdentityError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        if self.operation_id is not None:
            _identifier(self.operation_id, field="operation_id")
        for value, field in (
            (self.effect_owner_count, "effect_owner_count"),
            (self.publisher_count, "publisher_count"),
        ):
            if type(value) is not int or not 0 <= value <= MAX_OWNER_CLAIMS:
                _fail(field, "count")
        if state is MixedJourneyIdentityState.BOUND:
            if self.operation_id is None or self.effect_owner_count > 1 or self.publisher_count > 1:
                _fail("bound", "facts")
        elif self.operation_id is not None or self.effect_owner_count or self.publisher_count:
            _fail("blocked", "leak")

    @property
    def identity_state(self) -> MixedJourneyIdentityState:
        return self.state

    @property
    def journey_id(self) -> str:
        return self.identity_id

    @property
    def turn_id(self) -> str:
        return self.authenticated_turn_id

    @property
    def owner_count(self) -> int:
        return self.effect_owner_count

    @property
    def publisher_claim_count(self) -> int:
        return self.publisher_count

    @property
    def decision(self) -> MixedJourneyIdentityState:
        return self.state

    @property
    def bound(self) -> bool:
        return self.state is MixedJourneyIdentityState.BOUND

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_IDENTITY_SCHEMA,
            "identity_id": self.identity_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "state": self.state.value,
            "operation_id": self.operation_id,
            "effect_owner_count": self.effect_owner_count,
            "publisher_count": self.publisher_count,
            "reason": self.reason.value,
        }


MixedJourneyIdentity = MixedJourneyIdentityV1
MixedJourneyIdentityFacts = MixedJourneyIdentityFactsV1
IdentityState = MixedJourneyIdentityState
IdentityReason = MixedJourneyIdentityReason


def _empty(identity_id: str, turn_id: str, reason: MixedJourneyIdentityReason) -> MixedJourneyIdentityV1:
    return MixedJourneyIdentityV1(identity_id, turn_id, MixedJourneyIdentityState.EMPTY, None, 0, 0, reason)


def _blocked(identity_id: str, turn_id: str, reason: MixedJourneyIdentityReason) -> MixedJourneyIdentityV1:
    return MixedJourneyIdentityV1(identity_id, turn_id, MixedJourneyIdentityState.BLOCKED, None, 0, 0, reason)


def _mapping_facts(raw: Mapping[str, Any]) -> MixedJourneyIdentityFactsV1:
    allowed = {
        "schema",
        "identity_id",
        "journey_id",
        "operation_id",
        "authenticated_turn_id",
        "turn_id",
        "effect_owners",
        "effect_owner_count",
        "publishers",
        "publisher_count",
        "state",
        "reason",
    }
    if set(raw) - allowed:
        _fail("facts", "unknown")
    owners = raw.get("effect_owners", raw.get("effect_owner_count"))
    publishers = raw.get("publishers", raw.get("publisher_count"))
    return MixedJourneyIdentityFactsV1(
        operation_id=cast(str | None, raw.get("operation_id")),
        authenticated_turn_id=cast(str | None, raw.get("authenticated_turn_id", raw.get("turn_id"))),
        effect_owners=cast(Any, owners) if type(owners) is int else tuple(owners or ()),
        publishers=cast(Any, publishers) if type(publishers) is int else tuple(publishers or ()),
    )


def _admit(
    key: str,
    turn: str,
    operation_id: object,
    effect_owners: object,
    publishers: object,
) -> MixedJourneyIdentityV1:
    if operation_id is None and effect_owners in (None, ()) and publishers in (None, ()):
        return _empty(key, turn, MixedJourneyIdentityReason.NO_FACTS)
    try:
        op = _identifier(operation_id, field="operation_id")
        owner_count = _claims(effect_owners, field="effect_owners")
        publisher_count = _claims(publishers, field="publishers")
    except (TypeError, ValueError):
        return _blocked(key, turn, MixedJourneyIdentityReason.INVALID_FACTS)
    if owner_count > 1:
        return _blocked(key, turn, MixedJourneyIdentityReason.MULTIPLE_EFFECT_OWNERS)
    if publisher_count > 1:
        return _blocked(key, turn, MixedJourneyIdentityReason.MULTIPLE_PUBLISHERS)
    return MixedJourneyIdentityV1(
        key,
        turn,
        MixedJourneyIdentityState.BOUND,
        op,
        owner_count,
        publisher_count,
        MixedJourneyIdentityReason.BOUND,
    )


def build_mixed_journey_identity(
    identity_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    operation_id: str | None = None,
    *,
    facts: MixedJourneyIdentityFactsV1 | Mapping[str, Any] | None = None,
    effect_owners: object = None,
    publishers: object = None,
) -> MixedJourneyIdentityV1:
    """Admit one immutable mixed-journey identity from supplied facts."""

    if isinstance(identity_id, Mapping):
        raw = identity_id
        try:
            if raw.get("schema", MIXED_JOURNEY_IDENTITY_SCHEMA) != MIXED_JOURNEY_IDENTITY_SCHEMA:
                _fail("schema")
            key = _identifier(
                raw.get("identity_id", raw.get("journey_id", raw.get("operation_id"))), field="identity_id"
            )
            turn = _identifier(
                raw.get("authenticated_turn_id", raw.get("turn_id")), field="authenticated_turn_id"
            )
            if "state" in raw:
                state = MixedJourneyIdentityState(raw["state"])
                reason = MixedJourneyIdentityReason(raw.get("reason", "invalid_facts"))
                if state is not MixedJourneyIdentityState.BOUND:
                    return (
                        _blocked(key, turn, reason)
                        if state is MixedJourneyIdentityState.BLOCKED
                        else _empty(key, turn, reason)
                    )
            parsed = _mapping_facts(raw)
            return _admit(key, turn, parsed.operation_id, parsed.effect_owners, parsed.publishers)
        except (TypeError, ValueError, MixedJourneyIdentityError):
            key = cast(str, raw.get("identity_id", raw.get("journey_id", "journey")))
            turn = cast(str, raw.get("authenticated_turn_id", raw.get("turn_id", "turn")))
            try:
                return _blocked(
                    _identifier(key, field="identity_id"),
                    _identifier(turn, field="authenticated_turn_id"),
                    MixedJourneyIdentityReason.INVALID_FACTS,
                )
            except MixedJourneyIdentityError:
                return _blocked("journey", "turn", MixedJourneyIdentityReason.INVALID_FACTS)
    else:
        key = _identifier(identity_id, field="identity_id")
        turn = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    if facts is not None:
        try:
            if any(value is not None for value in (operation_id, effect_owners, publishers)):
                _fail("facts", "duplicate")
            if isinstance(facts, MixedJourneyIdentityFactsV1):
                operation_id = facts.operation_id
                turn = _identifier(facts.authenticated_turn_id or turn, field="authenticated_turn_id")
                effect_owners, publishers = facts.effect_owners, facts.publishers
            elif isinstance(facts, Mapping):
                if facts.get("schema", MIXED_JOURNEY_IDENTITY_SCHEMA) != MIXED_JOURNEY_IDENTITY_SCHEMA:
                    _fail("schema")
                parsed = _mapping_facts(facts)
                operation_id = parsed.operation_id
                turn = _identifier(parsed.authenticated_turn_id or turn, field="authenticated_turn_id")
                effect_owners, publishers = parsed.effect_owners, parsed.publishers
            else:
                _fail("facts", "type")
        except (TypeError, ValueError, MixedJourneyIdentityError):
            return _blocked(key, turn, MixedJourneyIdentityReason.INVALID_FACTS)
    return _admit(key, turn, operation_id, effect_owners, publishers)


def validate_mixed_journey_identity(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyIdentityV1)
            else build_mixed_journey_identity(cast(Mapping[str, Any], value))
        )
        return (
            isinstance(result, MixedJourneyIdentityV1)
            and result.state is not MixedJourneyIdentityState.BLOCKED
        )
    except (TypeError, ValueError):
        return False


build_journey_identity = build_mixed_journey_identity
validate_journey_identity = validate_mixed_journey_identity

__all__ = [
    "MIXED_JOURNEY_IDENTITY_SCHEMA",
    "IdentityReason",
    "IdentityState",
    "MixedJourneyIdentity",
    "MixedJourneyIdentityError",
    "MixedJourneyIdentityFacts",
    "MixedJourneyIdentityFactsV1",
    "MixedJourneyIdentityReason",
    "MixedJourneyIdentityState",
    "MixedJourneyIdentityV1",
    "build_journey_identity",
    "build_mixed_journey_identity",
    "validate_journey_identity",
    "validate_mixed_journey_identity",
]
