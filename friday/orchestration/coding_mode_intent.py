"""Pure request-intent classification for Coding Mode.

The classifier consumes request facts supplied by an upstream boundary.  It
does not read a prompt source, inspect an upload, resolve a revision, or
start a worker.  Ambiguous requests and floating revision selectors fail
closed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from friday.orchestration.coding_project_identity import build_coding_project_identity

CODING_MODE_INTENT_SCHEMA = "friday.coding-mode-intent.v1"
MAX_INTENT_ID_CHARS = 128
MAX_AUTHENTICATED_TURN_ID_CHARS = 128
MAX_PROMPT_CHARS = 16_384

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MISSING = object()


class CodingModeIntentError(ValueError):
    """An intent identity, request fact, or closed result is malformed."""


class CodingModeIntentState(StrEnum):
    EMPTY = "empty"
    PROMPT = "prompt"
    UPLOAD = "upload"
    INSPECT = "inspect"
    CONTINUE = "continue"
    BLOCKED = "blocked"


class CodingModeIntentReason(StrEnum):
    NO_FACTS = "no_facts"
    PROMPT_RECEIVED = "prompt_received"
    UPLOAD_RECEIVED = "upload_received"
    INSPECT_RECEIVED = "inspect_received"
    CONTINUE_RECEIVED = "continue_received"
    MULTIPLE_INTENTS = "multiple_intents"
    RECENCY_REVISION_SELECTOR = "recency_revision_selector"
    MISSING_PROJECT_ID = "missing_project_id"
    MISSING_REVISION_SELECTOR = "missing_revision_selector"
    INVALID_PROMPT = "invalid_prompt"
    INVALID_UPLOAD = "invalid_upload"
    INVALID_INSPECT = "invalid_inspect"
    INVALID_CONTINUE = "invalid_continue"
    INVALID_FACTS = "invalid_facts"


@dataclass(frozen=True, slots=True)
class CodingModeIntentFactsV1:
    """Already-supplied request facts, with no request body retention."""

    prompt: object | None = None
    upload: object | None = None
    inspect: object | None = None
    continue_request: object | None = None
    project_id: object | None = None
    revision_selector: object | None = None


@dataclass(frozen=True, slots=True)
class CodingModeIntentV1:
    """Immutable one-of Coding Mode request intent."""

    intent_id: str
    authenticated_turn_id: str
    intent: CodingModeIntentState
    prompt: str | None
    project_id: str | None
    revision_selector: str | None
    reason: CodingModeIntentReason

    def __post_init__(self) -> None:
        _identifier(self.intent_id, "intent_id", MAX_INTENT_ID_CHARS)
        _identifier(self.authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
        state = _state(self.intent)
        reason = _reason(self.reason)
        object.__setattr__(self, "intent", state)
        object.__setattr__(self, "reason", reason)
        if state is CodingModeIntentState.PROMPT:
            _prompt(self.prompt)
            if self.project_id is not None or self.revision_selector is not None:
                raise CodingModeIntentError("prompt_intent_exposes_project_facts")
        elif state is CodingModeIntentState.CONTINUE:
            if self.prompt is not None or self.project_id is None or self.revision_selector is None:
                raise CodingModeIntentError("continue_intent_facts_invalid")
            _identifier(self.project_id, "project_id", 128)
            _exact_revision(self.revision_selector)
        elif self.prompt is not None or self.project_id is not None or self.revision_selector is not None:
            raise CodingModeIntentError("non_payload_intent_exposes_facts")

    @property
    def state(self) -> CodingModeIntentState:
        return self.intent

    @property
    def decision(self) -> CodingModeIntentState:
        return self.intent

    @property
    def mode(self) -> CodingModeIntentState:
        return self.intent

    @property
    def prompt_body(self) -> str | None:
        return self.prompt

    @property
    def continuation(self) -> bool:
        return self.intent is CodingModeIntentState.CONTINUE

    @property
    def closed_reason(self) -> CodingModeIntentReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": CODING_MODE_INTENT_SCHEMA,
            "intent_id": self.intent_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "intent": self.intent.value,
            "prompt": self.prompt,
            "project_id": self.project_id,
            "revision_selector": self.revision_selector,
            "reason": self.reason.value,
        }


CodingModeIntentStateV1 = CodingModeIntentState
CodingModeIntent = CodingModeIntentV1
CodingModeIntentFacts = CodingModeIntentFactsV1


def _identifier(value: object, field: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum or _ID_RE.fullmatch(value) is None:
        raise CodingModeIntentError(f"{field}_id_invalid")
    return cast(str, value)


def _state(value: object) -> CodingModeIntentState:
    try:
        return value if isinstance(value, CodingModeIntentState) else CodingModeIntentState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingModeIntentError("intent_closed") from exc


def _reason(value: object) -> CodingModeIntentReason:
    try:
        return value if isinstance(value, CodingModeIntentReason) else CodingModeIntentReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise CodingModeIntentError("reason_closed") from exc


def _prompt(value: object) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > MAX_PROMPT_CHARS
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
    ):
        raise CodingModeIntentError("prompt_invalid")
    return cast(str, value)


def _exact_revision(value: object) -> str:
    if type(value) is not str:
        raise CodingModeIntentError("revision_selector_invalid")
    if value.strip().casefold() in {"", "latest", "head", "newest", "current"}:
        raise CodingModeIntentError("recency_revision_selector")
    return _identifier(value, "revision_selector", 128)


def _mapping_facts(value: Mapping[str, object]) -> CodingModeIntentFactsV1:
    allowed = {
        "schema",
        "prompt",
        "prompt_body",
        "upload",
        "upload_request",
        "inspect",
        "inspect_request",
        "continue",
        "continue_request",
        "continuation",
        "project_id",
        "project",
        "revision_selector",
        "revision",
        "revision_id",
    }
    if set(value) - allowed:
        raise CodingModeIntentError("intent_facts_unknown_fields")
    if value.get("schema", CODING_MODE_INTENT_SCHEMA) != CODING_MODE_INTENT_SCHEMA:
        raise CodingModeIntentError("intent_schema_invalid")
    project = value.get("project_id", value.get("project"))
    revision = value.get("revision_selector", value.get("revision", value.get("revision_id")))
    continuation = value.get("continue_request", value.get("continuation", value.get("continue")))
    return CodingModeIntentFactsV1(
        prompt=value.get("prompt", value.get("prompt_body")),
        upload=value.get("upload", value.get("upload_request")),
        inspect=value.get("inspect", value.get("inspect_request")),
        continue_request=continuation,
        project_id=project,
        revision_selector=revision,
    )


def _facts(value: object) -> CodingModeIntentFactsV1:
    if value is None:
        return CodingModeIntentFactsV1()
    if isinstance(value, CodingModeIntentFactsV1):
        return value
    if isinstance(value, Mapping):
        return _mapping_facts(value)
    raise CodingModeIntentError("intent_facts_invalid")


def _result(
    intent_id: str,
    turn: str,
    state: CodingModeIntentState,
    reason: CodingModeIntentReason,
    *,
    prompt: str | None = None,
    project_id: str | None = None,
    revision_selector: str | None = None,
) -> CodingModeIntentV1:
    if state is not CodingModeIntentState.PROMPT:
        prompt = None
    if state is not CodingModeIntentState.CONTINUE:
        project_id = None
        revision_selector = None
    return CodingModeIntentV1(intent_id, turn, state, prompt, project_id, revision_selector, reason)


def build_coding_mode_intent(
    intent_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    facts: CodingModeIntentFactsV1 | Mapping[str, object] | None = None,
    *,
    prompt: object = _MISSING,
    upload: object = _MISSING,
    inspect: object = _MISSING,
    continue_request: object = _MISSING,
    continuation: object = _MISSING,
    project_id: object = _MISSING,
    revision_selector: object = _MISSING,
) -> CodingModeIntentV1:
    """Classify exactly one already-supplied Coding Mode request intent."""

    if isinstance(intent_id, Mapping):
        raw = intent_id
        allowed_output = {
            "schema",
            "intent_id",
            "authenticated_turn_id",
            "intent",
            "state",
            "prompt",
            "project_id",
            "revision_selector",
            "reason",
        }
        if set(raw) - allowed_output:
            raise CodingModeIntentError("intent_mapping_unknown_fields")
        if {"intent", "state", "reason"}.intersection(raw):
            required = {
                "schema",
                "intent_id",
                "authenticated_turn_id",
                "intent",
                "prompt",
                "project_id",
                "revision_selector",
                "reason",
            }
            if set(raw) != required or raw.get("schema") != CODING_MODE_INTENT_SCHEMA:
                raise CodingModeIntentError("intent_mapping_serialized_invalid")
            return CodingModeIntentV1(
                cast(str, raw.get("intent_id")),
                cast(str, raw.get("authenticated_turn_id")),
                cast(CodingModeIntentState, raw.get("intent", raw.get("state"))),
                cast(str | None, raw.get("prompt")),
                cast(str | None, raw.get("project_id")),
                cast(str | None, raw.get("revision_selector")),
                cast(CodingModeIntentReason, raw.get("reason")),
            )
        if facts is not None or any(value is not _MISSING for value in (prompt, upload, inspect, continue_request, continuation, project_id, revision_selector)):
            raise CodingModeIntentError("intent_mapping_and_explicit_facts_mixed")
        intent_id = cast(str, raw.get("intent_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        facts = raw
    intent_key = _identifier(intent_id, "intent_id", MAX_INTENT_ID_CHARS)
    turn_key = _identifier(authenticated_turn_id, "authenticated_turn_id", MAX_AUTHENTICATED_TURN_ID_CHARS)
    try:
        if any(value is not _MISSING for value in (prompt, upload, inspect, continue_request, continuation, project_id, revision_selector)):
            if facts is not None:
                raise CodingModeIntentError("facts_and_explicit_intent_mixed")
            continuation_value = continuation if continuation is not _MISSING else continue_request
            fact_values = CodingModeIntentFactsV1(
                None if prompt is _MISSING else prompt,
                None if upload is _MISSING else upload,
                None if inspect is _MISSING else inspect,
                None if continuation_value is _MISSING else continuation_value,
                None if project_id is _MISSING else project_id,
                None if revision_selector is _MISSING else revision_selector,
            )
        else:
            fact_values = _facts(facts)
    except (TypeError, ValueError):
        return _result(intent_key, turn_key, CodingModeIntentState.BLOCKED, CodingModeIntentReason.INVALID_FACTS)

    prompt_present = fact_values.prompt is not None
    upload_present = fact_values.upload is not None
    inspect_present = fact_values.inspect is not None
    continue_present = (
        fact_values.continue_request is not None
        or fact_values.project_id is not None
        or fact_values.revision_selector is not None
    )
    present_count = sum((prompt_present, upload_present, inspect_present, continue_present))
    if present_count == 0:
        return _result(intent_key, turn_key, CodingModeIntentState.EMPTY, CodingModeIntentReason.NO_FACTS)
    if present_count > 1:
        return _result(intent_key, turn_key, CodingModeIntentState.BLOCKED, CodingModeIntentReason.MULTIPLE_INTENTS)
    if prompt_present:
        try:
            prompt_value = _prompt(fact_values.prompt)
        except (TypeError, ValueError):
            return _result(intent_key, turn_key, CodingModeIntentState.BLOCKED, CodingModeIntentReason.INVALID_PROMPT)
        return _result(
            intent_key,
            turn_key,
            CodingModeIntentState.PROMPT,
            CodingModeIntentReason.PROMPT_RECEIVED,
            prompt=prompt_value,
        )
    if upload_present:
        if fact_values.upload is False or not isinstance(fact_values.upload, (bool, Mapping, list, tuple, str)):
            return _result(intent_key, turn_key, CodingModeIntentState.BLOCKED, CodingModeIntentReason.INVALID_UPLOAD)
        return _result(intent_key, turn_key, CodingModeIntentState.UPLOAD, CodingModeIntentReason.UPLOAD_RECEIVED)
    if inspect_present:
        if fact_values.inspect is not True and not isinstance(fact_values.inspect, Mapping):
            return _result(intent_key, turn_key, CodingModeIntentState.BLOCKED, CodingModeIntentReason.INVALID_INSPECT)
        return _result(intent_key, turn_key, CodingModeIntentState.INSPECT, CodingModeIntentReason.INSPECT_RECEIVED)
    try:
        identity = build_coding_project_identity(
            f"{intent_key}:identity",
            turn_key,
            project_id=fact_values.project_id,
            revision_selector=fact_values.revision_selector,
        )
    except (TypeError, ValueError):
        return _result(intent_key, turn_key, CodingModeIntentState.BLOCKED, CodingModeIntentReason.INVALID_CONTINUE)
    if identity.identity.value == "blocked":
        reason = (
            CodingModeIntentReason.RECENCY_REVISION_SELECTOR
            if fact_values.revision_selector is not None
            and isinstance(fact_values.revision_selector, str)
            and fact_values.revision_selector.strip().casefold() in {"", "latest", "head", "newest", "current"}
            else CodingModeIntentReason.INVALID_CONTINUE
        )
        if identity.closed_reason.value == "missing_project_id":
            reason = CodingModeIntentReason.MISSING_PROJECT_ID
        elif identity.closed_reason.value == "missing_revision_selector":
            reason = CodingModeIntentReason.MISSING_REVISION_SELECTOR
        return _result(intent_key, turn_key, CodingModeIntentState.BLOCKED, reason)
    return _result(
        intent_key,
        turn_key,
        CodingModeIntentState.CONTINUE,
        CodingModeIntentReason.CONTINUE_RECEIVED,
        project_id=identity.project_id,
        revision_selector=identity.revision_selector,
    )


build_mode_intent = build_coding_mode_intent


__all__ = [
    "CODING_MODE_INTENT_SCHEMA",
    "MAX_AUTHENTICATED_TURN_ID_CHARS",
    "MAX_INTENT_ID_CHARS",
    "MAX_PROMPT_CHARS",
    "CodingModeIntent",
    "CodingModeIntentError",
    "CodingModeIntentFacts",
    "CodingModeIntentFactsV1",
    "CodingModeIntentReason",
    "CodingModeIntentState",
    "CodingModeIntentStateV1",
    "CodingModeIntentV1",
    "build_coding_mode_intent",
    "build_mode_intent",
]
