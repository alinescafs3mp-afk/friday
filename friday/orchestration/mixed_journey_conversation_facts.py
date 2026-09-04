"""Read-only conversation identity facts for a mixed journey.

The adapter admits only an opaque conversation id, an authenticated turn id,
and, when present, an explicit immutable revision selector.  It never retains
message bodies or reads conversation history.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

MIXED_JOURNEY_CONVERSATION_FACTS_SCHEMA = "friday.mixed-journey-conversation-facts.v1"
MAX_CONVERSATION_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_RECENCY_SELECTOR_CHARS = 20
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SELECTOR_RE = re.compile(r"revision:[1-9][0-9]{0,9}\Z")


class MixedJourneyConversationFactsError(ValueError):
    """A conversation fact or serialized result is malformed."""


class MixedJourneyConversationFactsState(StrEnum):
    EMPTY = "empty"
    PRESENT = "present"
    BLOCKED = "blocked"


class MixedJourneyConversationFactsReason(StrEnum):
    NO_FACTS = "no_facts"
    PRESENT = "present"
    INVALID_FACTS = "invalid_facts"
    INVALID_CONVERSATION_ID = "invalid_conversation_id"
    INVALID_TURN_ID = "invalid_turn_id"
    UNSAFE_RECENCY = "unsafe_recency"
    PRIVATE_FACT = "private_fact"


@dataclass(frozen=True, slots=True)
class MixedJourneyConversationFactsInputV1:
    """Facts supplied by a conversation observer without message content."""

    conversation_id: str | None = None
    authenticated_turn_id: str | None = None
    recency_selector: str | None = None


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise MixedJourneyConversationFactsError(f"{field}_{detail}")


def _id(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _selector(value: object) -> str:
    if type(value) is not str or len(value) > MAX_RECENCY_SELECTOR_CHARS:
        _fail("recency_selector", "unsafe")
    selector = cast(str, value).strip().casefold()
    if _SELECTOR_RE.fullmatch(selector) is None:
        _fail("recency_selector", "unsafe")
    return selector


@dataclass(frozen=True, slots=True)
class MixedJourneyConversationFactsV1:
    """One immutable, body-free conversation-organ result."""

    conversation_id: str | None
    authenticated_turn_id: str | None
    state: MixedJourneyConversationFactsState
    recency_selector: str | None
    summary_digest: str | None
    reason: MixedJourneyConversationFactsReason

    def __post_init__(self) -> None:
        if self.conversation_id is not None:
            _id(self.conversation_id, field="conversation_id")
        if self.authenticated_turn_id is not None:
            _id(self.authenticated_turn_id, field="authenticated_turn_id")
        try:
            state = MixedJourneyConversationFactsState(self.state)
            reason = MixedJourneyConversationFactsReason(self.reason)
        except (TypeError, ValueError) as exc:
            raise MixedJourneyConversationFactsError("state_closed") from exc
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "reason", reason)
        if state is MixedJourneyConversationFactsState.PRESENT:
            if self.conversation_id is None or self.authenticated_turn_id is None:
                _fail("present", "identity")
            if self.recency_selector is not None:
                _selector(self.recency_selector)
            if (
                type(self.summary_digest) is not str
                or re.fullmatch(r"[0-9a-f]{64}\Z", self.summary_digest) is None
            ):
                _fail("summary_digest", "hex")
        elif self.recency_selector is not None or self.summary_digest is not None:
            _fail("non_present", "leak")
        if state is MixedJourneyConversationFactsState.BLOCKED and (
            self.conversation_id is not None or self.authenticated_turn_id is not None
        ):
            _fail("blocked", "body_leak")

    @property
    def fact_state(self) -> MixedJourneyConversationFactsState:
        return self.state

    @property
    def conversation_state(self) -> MixedJourneyConversationFactsState:
        return self.state

    @property
    def decision(self) -> MixedJourneyConversationFactsState:
        return self.state

    @property
    def selector(self) -> str | None:
        return self.recency_selector

    @property
    def digest(self) -> str | None:
        return self.summary_digest

    @property
    def closed_reason(self) -> MixedJourneyConversationFactsReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": MIXED_JOURNEY_CONVERSATION_FACTS_SCHEMA,
            "conversation_id": self.conversation_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "state": self.state.value,
            "recency_selector": self.recency_selector,
            "summary_digest": self.summary_digest,
            "reason": self.reason.value,
        }


ConversationFactsInput = MixedJourneyConversationFactsInputV1
ConversationFactsState = MixedJourneyConversationFactsState
ConversationFactsReason = MixedJourneyConversationFactsReason
MixedJourneyConversationFacts = MixedJourneyConversationFactsV1


def _blocked(reason: MixedJourneyConversationFactsReason) -> MixedJourneyConversationFactsV1:
    return MixedJourneyConversationFactsV1(
        None, None, MixedJourneyConversationFactsState.BLOCKED, None, None, reason
    )


def _digest(conversation_id: str, turn_id: str, selector: str | None) -> str:
    value = "|".join((conversation_id, turn_id, selector or "none"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _known(raw: Mapping[str, object]) -> None:
    allowed = {
        "schema",
        "conversation_id",
        "id",
        "authenticated_turn_id",
        "turn_id",
        "state",
        "reason",
        "recency_selector",
        "selector",
        "revision",
        "summary_digest",
        "digest",
    }
    if set(raw) - allowed:
        _fail("facts", "unknown")
    if raw.get("schema", MIXED_JOURNEY_CONVERSATION_FACTS_SCHEMA) != MIXED_JOURNEY_CONVERSATION_FACTS_SCHEMA:
        _fail("schema")


def build_mixed_journey_conversation_facts(
    conversation_id: str | Mapping[str, object] | None = None,
    authenticated_turn_id: str | None = None,
    recency_selector: object = None,
    *,
    facts: MixedJourneyConversationFactsInputV1 | Mapping[str, object] | None = None,
) -> MixedJourneyConversationFactsV1:
    """Validate conversation identity facts and fail closed on body hazards."""

    if facts is not None:
        if conversation_id is not None or authenticated_turn_id is not None or recency_selector is not None:
            return _blocked(MixedJourneyConversationFactsReason.INVALID_FACTS)
        if isinstance(facts, MixedJourneyConversationFactsInputV1):
            conversation_id, authenticated_turn_id, recency_selector = (
                facts.conversation_id,
                facts.authenticated_turn_id,
                facts.recency_selector,
            )
        elif isinstance(facts, Mapping):
            conversation_id = facts
        else:
            return _blocked(MixedJourneyConversationFactsReason.INVALID_FACTS)
    if isinstance(conversation_id, Mapping):
        raw = conversation_id
        try:
            _known(raw)
            state = raw.get("state")
            if state in {"empty", "blocked"}:
                selected = MixedJourneyConversationFactsState(state)
                return MixedJourneyConversationFactsV1(
                    None,
                    None,
                    selected,
                    None,
                    None,
                    MixedJourneyConversationFactsReason(raw.get("reason", "no_facts")),
                )
            conversation_id = cast(str | None, raw.get("conversation_id", raw.get("id")))
            authenticated_turn_id = cast(str | None, raw.get("authenticated_turn_id", raw.get("turn_id")))
            recency_selector = raw.get("recency_selector", raw.get("selector", raw.get("revision")))
        except (TypeError, ValueError, MixedJourneyConversationFactsError):
            return _blocked(MixedJourneyConversationFactsReason.INVALID_FACTS)
    if conversation_id is None and authenticated_turn_id is None:
        return MixedJourneyConversationFactsV1(
            None,
            None,
            MixedJourneyConversationFactsState.EMPTY,
            None,
            None,
            MixedJourneyConversationFactsReason.NO_FACTS,
        )
    try:
        conversation_key = _id(conversation_id, field="conversation_id")
    except MixedJourneyConversationFactsError as exc:
        return _blocked(
            MixedJourneyConversationFactsReason.PRIVATE_FACT
            if "private" in str(exc)
            else MixedJourneyConversationFactsReason.INVALID_CONVERSATION_ID
        )
    try:
        turn_key = _id(authenticated_turn_id, field="authenticated_turn_id")
    except MixedJourneyConversationFactsError:
        return _blocked(MixedJourneyConversationFactsReason.INVALID_TURN_ID)
    try:
        selector = None if recency_selector is None else _selector(recency_selector)
    except MixedJourneyConversationFactsError:
        return _blocked(MixedJourneyConversationFactsReason.UNSAFE_RECENCY)
    return MixedJourneyConversationFactsV1(
        conversation_key,
        turn_key,
        MixedJourneyConversationFactsState.PRESENT,
        selector,
        _digest(conversation_key, turn_key, selector),
        MixedJourneyConversationFactsReason.PRESENT,
    )


def validate_mixed_journey_conversation_facts(value: object) -> bool:
    try:
        result = (
            value
            if isinstance(value, MixedJourneyConversationFactsV1)
            else build_mixed_journey_conversation_facts(cast(Mapping[str, object], value))
        )
        return (
            isinstance(result, MixedJourneyConversationFactsV1)
            and result.state is not MixedJourneyConversationFactsState.BLOCKED
        )
    except (TypeError, ValueError):
        return False


build_conversation_facts = build_mixed_journey_conversation_facts
validate_conversation_facts = validate_mixed_journey_conversation_facts

__all__ = [
    "MIXED_JOURNEY_CONVERSATION_FACTS_SCHEMA",
    "ConversationFactsInput",
    "ConversationFactsReason",
    "ConversationFactsState",
    "MixedJourneyConversationFacts",
    "MixedJourneyConversationFactsError",
    "MixedJourneyConversationFactsInputV1",
    "MixedJourneyConversationFactsReason",
    "MixedJourneyConversationFactsState",
    "MixedJourneyConversationFactsV1",
    "build_conversation_facts",
    "build_mixed_journey_conversation_facts",
    "validate_conversation_facts",
    "validate_mixed_journey_conversation_facts",
]
