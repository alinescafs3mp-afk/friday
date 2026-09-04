"""Revoke-before-publish admission facts for a mixed journey."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

MIXED_JOURNEY_REVOKE_SCHEMA = "friday.mixed-journey-revoke.v1"
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class MixedJourneyRevokeError(ValueError):
    """A revoke or publication fact is malformed."""


class MixedJourneyRevokeState(StrEnum):
    EMPTY = "empty"
    HELD = "held"
    REVOKED = "revoked"
    BLOCKED = "blocked"


class MixedJourneyRevokeReason(StrEnum):
    NO_FACTS = "no_facts"
    HELD = "held"
    REVOKED = "revoked"
    INVALID_FACTS = "invalid_facts"


@dataclass(frozen=True, slots=True)
class MixedJourneyRevokeFactsV1:
    revoked: bool | None = None
    publication_claimed: bool | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyRevokeError(f"{field}_{detail}")


def _id(value: object, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        _fail(field, "boolean")
    return cast(bool, value)


@dataclass(frozen=True, slots=True)
class MixedJourneyRevokeV1:
    journey_id: str
    authenticated_turn_id: str
    state: MixedJourneyRevokeState
    revoked: bool
    publication_claimed: bool
    publication_admitted: bool
    reason: MixedJourneyRevokeReason

    def __post_init__(self) -> None:
        _id(self.journey_id, "journey_id")
        _id(self.authenticated_turn_id, "authenticated_turn_id")
        try:
            state = MixedJourneyRevokeState(self.state)
            reason = MixedJourneyRevokeReason(self.reason)
        except (TypeError, ValueError) as exc:
            raise MixedJourneyRevokeError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        if type(self.revoked) is not bool or type(self.publication_claimed) is not bool:
            _fail("facts", "boolean")
        if type(self.publication_admitted) is not bool:
            _fail("publication_admitted", "boolean")
        if self.publication_admitted:
            _fail("publication_admitted", "authority")
        if state is MixedJourneyRevokeState.EMPTY:
            if self.revoked or self.publication_claimed:
                _fail("empty", "leak")
        elif state is MixedJourneyRevokeState.BLOCKED:
            if self.revoked or self.publication_claimed:
                _fail("blocked", "leak")
        elif state is MixedJourneyRevokeState.REVOKED:
            if not self.revoked or self.publication_admitted:
                _fail("revoked", "facts")
        elif state is MixedJourneyRevokeState.HELD and self.revoked:
            _fail("held", "facts")

    @property
    def revoke_state(self) -> MixedJourneyRevokeState:
        return self.state

    @property
    def decision(self) -> MixedJourneyRevokeState:
        return self.state

    @property
    def can_publish(self) -> bool:
        return False

    @property
    def publication_allowed(self) -> bool:
        return False

    @property
    def held(self) -> bool:
        return self.state is MixedJourneyRevokeState.HELD

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_REVOKE_SCHEMA,
            "journey_id": self.journey_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "state": self.state.value,
            "revoked": self.revoked,
            "publication_claimed": self.publication_claimed,
            "publication_admitted": False,
            "reason": self.reason.value,
        }


MixedJourneyRevoke = MixedJourneyRevokeV1
MixedJourneyRevokeFacts = MixedJourneyRevokeFactsV1
RevokeState = MixedJourneyRevokeState
RevokeReason = MixedJourneyRevokeReason


def _empty(key: str, turn: str, reason: MixedJourneyRevokeReason) -> MixedJourneyRevokeV1:
    return MixedJourneyRevokeV1(key, turn, MixedJourneyRevokeState.EMPTY, False, False, False, reason)


def _blocked(key: str, turn: str, reason: MixedJourneyRevokeReason) -> MixedJourneyRevokeV1:
    return MixedJourneyRevokeV1(key, turn, MixedJourneyRevokeState.BLOCKED, False, False, False, reason)


def build_mixed_journey_revoke(
    journey_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    *,
    facts: MixedJourneyRevokeFactsV1 | Mapping[str, Any] | None = None,
    revoked: bool | None = None,
    publication_claimed: bool | None = None,
) -> MixedJourneyRevokeV1:
    """Admit a held or revoked state; no state admits publication."""

    if isinstance(journey_id, Mapping):
        raw = journey_id
        key = cast(str, raw.get("journey_id", "journey"))
        turn = cast(str, raw.get("authenticated_turn_id", raw.get("turn_id", "turn")))
        try:
            key, turn = _id(key, "journey_id"), _id(turn, "authenticated_turn_id")
            if raw.get("schema", MIXED_JOURNEY_REVOKE_SCHEMA) != MIXED_JOURNEY_REVOKE_SCHEMA:
                _fail("schema")
            if raw.get("state") in {"empty", "blocked"}:
                state = MixedJourneyRevokeState(raw["state"])
                reason = MixedJourneyRevokeReason(raw.get("reason", "invalid_facts"))
                return (
                    _blocked(key, turn, reason)
                    if state is MixedJourneyRevokeState.BLOCKED
                    else _empty(key, turn, reason)
                )
            revoked = raw.get("revoked")
            publication_claimed = raw.get("publication_claimed")
            journey_id, authenticated_turn_id = key, turn
        except (TypeError, ValueError, MixedJourneyRevokeError):
            try:
                return _blocked(
                    _id(key, "journey_id"),
                    _id(turn, "authenticated_turn_id"),
                    MixedJourneyRevokeReason.INVALID_FACTS,
                )
            except MixedJourneyRevokeError:
                return _blocked("journey", "turn", MixedJourneyRevokeReason.INVALID_FACTS)
    key = _id(journey_id, "journey_id")
    turn = _id(authenticated_turn_id, "authenticated_turn_id")
    if facts is not None:
        try:
            if revoked is not None or publication_claimed is not None:
                _fail("facts", "duplicate")
            if isinstance(facts, MixedJourneyRevokeFactsV1):
                revoked, publication_claimed = facts.revoked, facts.publication_claimed
            elif isinstance(facts, Mapping):
                allowed = {
                    "schema",
                    "revoked",
                    "publication_claimed",
                    "revoke_requested",
                    "publish_requested",
                }
                if facts.get("schema", MIXED_JOURNEY_REVOKE_SCHEMA) != MIXED_JOURNEY_REVOKE_SCHEMA:
                    _fail("schema")
                if set(facts) - allowed:
                    _fail("facts", "unknown")
                revoked = facts.get("revoked", facts.get("revoke_requested"))
                publication_claimed = facts.get("publication_claimed", facts.get("publish_requested"))
            else:
                _fail("facts", "type")
        except (TypeError, ValueError, MixedJourneyRevokeError):
            return _blocked(key, turn, MixedJourneyRevokeReason.INVALID_FACTS)
    if revoked is None and publication_claimed is None:
        return _empty(key, turn, MixedJourneyRevokeReason.NO_FACTS)
    try:
        revoke_value = _bool(False if revoked is None else revoked, "revoked")
        claimed_value = _bool(
            False if publication_claimed is None else publication_claimed, "publication_claimed"
        )
    except MixedJourneyRevokeError:
        return _blocked(key, turn, MixedJourneyRevokeReason.INVALID_FACTS)
    if revoke_value:
        return MixedJourneyRevokeV1(
            key,
            turn,
            MixedJourneyRevokeState.REVOKED,
            True,
            claimed_value,
            False,
            MixedJourneyRevokeReason.REVOKED,
        )
    return MixedJourneyRevokeV1(
        key, turn, MixedJourneyRevokeState.HELD, False, claimed_value, False, MixedJourneyRevokeReason.HELD
    )


def validate_mixed_journey_revoke(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyRevokeV1)
            else build_mixed_journey_revoke(cast(Mapping[str, Any], value))
        )
        return (
            isinstance(result, MixedJourneyRevokeV1) and result.state is not MixedJourneyRevokeState.BLOCKED
        )
    except (TypeError, ValueError):
        return False


build_journey_revoke = build_mixed_journey_revoke
validate_journey_revoke = validate_mixed_journey_revoke

__all__ = [
    "MIXED_JOURNEY_REVOKE_SCHEMA",
    "MixedJourneyRevoke",
    "MixedJourneyRevokeError",
    "MixedJourneyRevokeFacts",
    "MixedJourneyRevokeFactsV1",
    "MixedJourneyRevokeReason",
    "MixedJourneyRevokeState",
    "MixedJourneyRevokeV1",
    "RevokeReason",
    "RevokeState",
    "build_journey_revoke",
    "build_mixed_journey_revoke",
    "validate_journey_revoke",
    "validate_mixed_journey_revoke",
]
