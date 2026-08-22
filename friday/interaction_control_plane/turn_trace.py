"""Closed v1 structural trace for one interaction turn.

The trace is deliberately less expressive than an application log.  It can
describe where a turn went, what broad capabilities ran, and whether the work
was completed or published.  It cannot carry user prose, queries, document
metadata, filesystem paths, or raw account and conversation identifiers.

Raw identifiers enter only :func:`derive_trace_identifier`.  That function
uses a deployment-local key and domain-separated HMAC-SHA-256 so low-entropy
identifiers cannot be recovered from a deidentified event by dictionary
matching.  The key and the source identifiers are never retained by the
contract.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

TURN_TRACE_SCHEMA = "friday.interaction-turn-trace.v1"

_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER_CONTEXT = b"friday.interaction-turn-trace.identifier.v1\x00"
_MAX_IDENTIFIER_BYTES = 4_096
_MIN_NAMESPACE_KEY_BYTES = 32
_MAX_NAMESPACE_KEY_BYTES = 4_096
_MAX_SERIALIZED_BYTES = 16_384
_MAX_STEPS = 32
_MAX_ATTEMPTS = 16
_MAX_LATENCY_MS = 86_400_000
_MAX_CALLS = 1_024
_MAX_TOKENS = 100_000_000
EnumT = TypeVar("EnumT", bound=StrEnum)


class TurnTraceError(ValueError):
    """A value is outside the closed Turn Trace v1 contract."""


class TraceIdentifierDomain(StrEnum):
    """Non-interchangeable domains for opaque identifiers in a trace."""

    TURN = "turn"
    CONVERSATION = "conversation"
    WORK_ITEM = "work_item"
    STEP = "step"


class WorkRelation(StrEnum):
    DIRECT = "direct"
    NEW = "new"
    CONTINUED = "continued"


class IntentClass(StrEnum):
    SMALL_TALK = "small_talk"
    ORDINARY_DIALOGUE = "ordinary_dialogue"
    DOCUMENT_WORK = "document_work"
    MESSAGE_RECALL = "message_recall"
    WEB_RESEARCH = "web_research"
    ENTITY_LOOKUP = "entity_lookup"
    PERSONAL_ORGANIZATION = "personal_organization"
    EFFECT = "effect"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class ContinuationKind(StrEnum):
    NONE = "none"
    SAME_GOAL = "same_goal"
    CANDIDATE_SELECTION = "candidate_selection"
    CONSTRAINT_UPDATE = "constraint_update"
    REFERENCE = "reference"
    RESUME = "resume"
    CORRECTION = "correction"
    UNKNOWN = "unknown"


class PlaybookClass(StrEnum):
    NONE = "none"
    DIRECT = "direct"
    LOCATE_AND_EXPLAIN_DOCUMENT = "locate_and_explain_document"
    RECALL_CONVERSATION = "recall_conversation"
    COMPARE_INTERNAL_AND_EXTERNAL_SOURCES = "compare_internal_and_external_sources"
    OTHER = "other"


class CapabilityClass(StrEnum):
    """Coarse classes only; tool, provider, path, and query names are excluded."""

    CONVERSATION = "conversation"
    MODEL_PLANNING = "model_planning"
    DOCUMENT_INGESTION = "document_ingestion"
    DOCUMENT_RETRIEVAL = "document_retrieval"
    MESSAGE_RETRIEVAL = "message_retrieval"
    WEB_RESEARCH = "web_research"
    ENTITY_LOOKUP = "entity_lookup"
    FILE_GENERATION = "file_generation"
    PERSONAL_ORGANIZATION = "personal_organization"
    OBSIDIAN = "obsidian"
    VERIFICATION = "verification"
    MODEL_SYNTHESIS = "model_synthesis"
    PUBLICATION = "publication"
    OTHER_READ = "other_read"
    OTHER_EFFECT = "other_effect"


class OutcomeStatus(StrEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    EMPTY = "empty"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


class CompletionDecision(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    INCOMPLETE = "incomplete"
    WAITING_FOR_INPUT = "waiting_for_input"
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class PublicationStatus(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    SUPPRESSED = "suppressed"
    PUBLISHED = "published"
    FAILED = "failed"
    DENIED = "denied"


class FailureStage(StrEnum):
    NONE = "none"
    INTENT = "intent"
    CONTINUATION = "continuation"
    REFERENCE = "reference"
    PLANNING = "planning"
    CANDIDATE_GENERATION = "candidate_generation"
    CAPABILITY = "capability"
    STATE_LOSS = "state_loss"
    COMPLETION = "completion"
    SYNTHESIS_CONTRADICTION = "synthesis_contradiction"
    PUBLICATION = "publication"


class FailureReason(StrEnum):
    NONE = "none"
    INVALID_INPUT = "invalid_input"
    AMBIGUOUS_REFERENCE = "ambiguous_reference"
    AUTHORITY_DENIED = "authority_denied"
    CHANNEL_DENIED = "channel_denied"
    SOURCE_UNAVAILABLE = "source_unavailable"
    PROVIDER_FAILURE = "provider_failure"
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"
    STALE_STATE = "stale_state"
    STATE_CONFLICT = "state_conflict"
    COMPLETION_UNSATISFIED = "completion_unsatisfied"
    VERIFICATION_REJECTED = "verification_rejected"
    PUBLICATION_REJECTED = "publication_rejected"
    INVALID_CONTRACT = "invalid_contract"
    INTERNAL_ERROR = "internal_error"
    UNKNOWN = "unknown"


class TokenAccounting(StrEnum):
    UNAVAILABLE = "unavailable"
    ESTIMATED = "estimated"
    PROVIDER_REPORTED = "provider_reported"


class CountAccounting(StrEnum):
    UNAVAILABLE = "unavailable"
    LOWER_BOUND = "lower_bound"
    COMPLETE = "complete"


def _contains_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _closed_keys(value: Mapping[object, object], expected: frozenset[str], *, label: str) -> None:
    if any(not isinstance(key, str) for key in value) or frozenset(value) != expected:
        # Never echo an unknown key: it may itself contain private input.
        raise TurnTraceError(f"{label} keys do not match the closed contract")


def _enum_value(enum_type: type[EnumT], value: object, *, label: str) -> EnumT:
    if not isinstance(value, str) or len(value) > 64 or _contains_control(value):
        raise TurnTraceError(f"{label} must be a closed enum value")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise TurnTraceError(f"{label} must be a closed enum value") from exc


def _digest_value(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise TurnTraceError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _bounded_int(value: object, *, label: str, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise TurnTraceError(f"{label} must be an integer between 0 and {maximum}")
    return value


def _boolean(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise TurnTraceError(f"{label} must be a boolean")
    return value


def _mapping(value: object, *, label: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise TurnTraceError(f"{label} must be an object")
    return value


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TurnTraceError("turn trace contains a duplicate object key")
        result[key] = value
    return result


def _reject_json_constant(_constant: str) -> Any:
    raise TurnTraceError("turn trace contains a non-finite number")


def derive_trace_identifier(
    *,
    domain: TraceIdentifierDomain,
    raw_identifier: str | bytes,
    namespace_key: bytes,
) -> str:
    """Return a stable, domain-separated opaque identifier.

    ``namespace_key`` should be the deployment's existing audit privacy HMAC
    key.  Requiring a key avoids reversible hashes of short numeric IDs while
    retaining stable linkage inside one deployment.
    """

    if not isinstance(domain, TraceIdentifierDomain):
        raise TurnTraceError("identifier domain must be a TraceIdentifierDomain")
    if not isinstance(namespace_key, bytes) or not (
        _MIN_NAMESPACE_KEY_BYTES <= len(namespace_key) <= _MAX_NAMESPACE_KEY_BYTES
    ):
        raise TurnTraceError("identifier namespace key must contain 32 to 4096 bytes")
    if isinstance(raw_identifier, str):
        if not raw_identifier or _contains_control(raw_identifier):
            raise TurnTraceError("raw identifier must be non-empty and control-free")
        try:
            raw = raw_identifier.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise TurnTraceError("raw identifier must be valid UTF-8") from exc
    elif isinstance(raw_identifier, bytes):
        raw = raw_identifier
    else:
        raise TurnTraceError("raw identifier must be text or bytes")
    if not raw or len(raw) > _MAX_IDENTIFIER_BYTES:
        raise TurnTraceError(f"raw identifier must contain 1 to {_MAX_IDENTIFIER_BYTES} bytes")
    message = _IDENTIFIER_CONTEXT + domain.value.encode("ascii") + b"\x00" + raw
    return hmac.new(namespace_key, message, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityStepTrace:
    step_digest: str
    capability: CapabilityClass
    outcome: OutcomeStatus
    attempts: int
    required: bool

    def __post_init__(self) -> None:
        _digest_value(self.step_digest, label="step_digest")
        if not isinstance(self.capability, CapabilityClass):
            raise TurnTraceError("capability must be a CapabilityClass")
        if not isinstance(self.outcome, OutcomeStatus):
            raise TurnTraceError("outcome must be an OutcomeStatus")
        _bounded_int(self.attempts, label="attempts", maximum=_MAX_ATTEMPTS)
        _boolean(self.required, label="required")

    @classmethod
    def parse(cls, value: object) -> CapabilityStepTrace:
        item = _mapping(value, label="step")
        _closed_keys(
            item,
            frozenset({"step_digest", "capability", "outcome", "attempts", "required"}),
            label="step",
        )
        return cls(
            step_digest=_digest_value(item["step_digest"], label="step_digest"),
            capability=_enum_value(CapabilityClass, item["capability"], label="capability"),
            outcome=_enum_value(OutcomeStatus, item["outcome"], label="outcome"),
            attempts=_bounded_int(item["attempts"], label="attempts", maximum=_MAX_ATTEMPTS),
            required=_boolean(item["required"], label="required"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "step_digest": self.step_digest,
            "capability": self.capability.value,
            "outcome": self.outcome.value,
            "attempts": self.attempts,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class TraceBudget:
    latency_ms: int
    model_calls: int
    model_call_accounting: CountAccounting
    capability_calls: int
    capability_call_accounting: CountAccounting
    input_tokens: int
    output_tokens: int
    token_accounting: TokenAccounting

    def __post_init__(self) -> None:
        _bounded_int(self.latency_ms, label="latency_ms", maximum=_MAX_LATENCY_MS)
        _bounded_int(self.model_calls, label="model_calls", maximum=_MAX_CALLS)
        _bounded_int(self.capability_calls, label="capability_calls", maximum=_MAX_CALLS)
        if not isinstance(self.model_call_accounting, CountAccounting):
            raise TurnTraceError("model_call_accounting must be a CountAccounting")
        if not isinstance(self.capability_call_accounting, CountAccounting):
            raise TurnTraceError("capability_call_accounting must be a CountAccounting")
        if self.model_call_accounting is CountAccounting.UNAVAILABLE and self.model_calls:
            raise TurnTraceError("unobserved model call count must be zero")
        if self.capability_call_accounting is CountAccounting.UNAVAILABLE and self.capability_calls:
            raise TurnTraceError("unobserved capability call count must be zero")
        _bounded_int(self.input_tokens, label="input_tokens", maximum=_MAX_TOKENS)
        _bounded_int(self.output_tokens, label="output_tokens", maximum=_MAX_TOKENS)
        if not isinstance(self.token_accounting, TokenAccounting):
            raise TurnTraceError("token_accounting must be a TokenAccounting")
        if self.token_accounting is TokenAccounting.UNAVAILABLE and (self.input_tokens or self.output_tokens):
            raise TurnTraceError("unobserved token counts must be zero")

    @classmethod
    def parse(cls, value: object) -> TraceBudget:
        item = _mapping(value, label="budget")
        _closed_keys(
            item,
            frozenset(
                {
                    "latency_ms",
                    "model_calls",
                    "model_call_accounting",
                    "capability_calls",
                    "capability_call_accounting",
                    "input_tokens",
                    "output_tokens",
                    "token_accounting",
                }
            ),
            label="budget",
        )
        return cls(
            latency_ms=_bounded_int(item["latency_ms"], label="latency_ms", maximum=_MAX_LATENCY_MS),
            model_calls=_bounded_int(item["model_calls"], label="model_calls", maximum=_MAX_CALLS),
            model_call_accounting=_enum_value(
                CountAccounting, item["model_call_accounting"], label="model_call_accounting"
            ),
            capability_calls=_bounded_int(
                item["capability_calls"], label="capability_calls", maximum=_MAX_CALLS
            ),
            capability_call_accounting=_enum_value(
                CountAccounting,
                item["capability_call_accounting"],
                label="capability_call_accounting",
            ),
            input_tokens=_bounded_int(item["input_tokens"], label="input_tokens", maximum=_MAX_TOKENS),
            output_tokens=_bounded_int(item["output_tokens"], label="output_tokens", maximum=_MAX_TOKENS),
            token_accounting=_enum_value(TokenAccounting, item["token_accounting"], label="token_accounting"),
        )

    def to_payload(self) -> dict[str, int | str]:
        return {
            "latency_ms": self.latency_ms,
            "model_calls": self.model_calls,
            "model_call_accounting": self.model_call_accounting.value,
            "capability_calls": self.capability_calls,
            "capability_call_accounting": self.capability_call_accounting.value,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "token_accounting": self.token_accounting.value,
        }


@dataclass(frozen=True, slots=True)
class TurnTrace:
    """Immutable final structural state for one turn."""

    turn_digest: str
    conversation_digest: str
    work_item_digest: str | None
    work_relation: WorkRelation
    intent: IntentClass
    continuation: ContinuationKind
    playbook: PlaybookClass
    steps: tuple[CapabilityStepTrace, ...]
    completion: CompletionDecision
    publication: PublicationStatus
    failure_stage: FailureStage
    failure_reason: FailureReason
    ambiguity_present: bool
    partial_coverage: bool
    state_restored: bool
    authority_rechecked: bool
    budget: TraceBudget

    def __post_init__(self) -> None:
        _digest_value(self.turn_digest, label="turn_digest")
        _digest_value(self.conversation_digest, label="conversation_digest")
        if self.work_item_digest is not None:
            _digest_value(self.work_item_digest, label="work_item_digest")
        for label, enum_value, enum_type in (
            ("work_relation", self.work_relation, WorkRelation),
            ("intent", self.intent, IntentClass),
            ("continuation", self.continuation, ContinuationKind),
            ("playbook", self.playbook, PlaybookClass),
            ("completion", self.completion, CompletionDecision),
            ("publication", self.publication, PublicationStatus),
            ("failure_stage", self.failure_stage, FailureStage),
            ("failure_reason", self.failure_reason, FailureReason),
        ):
            if not isinstance(enum_value, enum_type):
                raise TurnTraceError(f"{label} has an invalid enum type")
        if not isinstance(self.steps, tuple) or len(self.steps) > _MAX_STEPS:
            raise TurnTraceError(f"steps must be an immutable tuple of at most {_MAX_STEPS} items")
        if any(not isinstance(step, CapabilityStepTrace) for step in self.steps):
            raise TurnTraceError("steps must contain CapabilityStepTrace values")
        step_digests = {step.step_digest for step in self.steps}
        if len(step_digests) != len(self.steps):
            raise TurnTraceError("step digests must be unique within a turn")
        for label, boolean_value in (
            ("ambiguity_present", self.ambiguity_present),
            ("partial_coverage", self.partial_coverage),
            ("state_restored", self.state_restored),
            ("authority_rechecked", self.authority_rechecked),
        ):
            _boolean(boolean_value, label=label)
        if not isinstance(self.budget, TraceBudget):
            raise TurnTraceError("budget must be a TraceBudget")
        self._validate_relationships()

    def _validate_relationships(self) -> None:
        if self.work_relation is WorkRelation.DIRECT:
            if self.work_item_digest is not None:
                raise TurnTraceError("direct work must not carry a work item digest")
        elif self.work_item_digest is None:
            raise TurnTraceError("new and continued work require a work item digest")
        if self.work_relation is WorkRelation.NEW and self.continuation is not ContinuationKind.NONE:
            raise TurnTraceError("new work cannot declare continuation")
        if self.work_relation is WorkRelation.CONTINUED and self.continuation is ContinuationKind.NONE:
            raise TurnTraceError("continued work must declare a continuation kind")
        has_stage = self.failure_stage is not FailureStage.NONE
        has_reason = self.failure_reason is not FailureReason.NONE
        if has_stage != has_reason:
            raise TurnTraceError("failure stage and reason must either both be none or both be set")

    @classmethod
    def parse(cls, value: str | Mapping[str, object]) -> TurnTrace:
        if isinstance(value, str):
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise TurnTraceError("turn trace JSON must be valid UTF-8") from exc
            if len(encoded) > _MAX_SERIALIZED_BYTES:
                raise TurnTraceError(f"turn trace exceeds {_MAX_SERIALIZED_BYTES} serialized bytes")
            try:
                decoded = json.loads(
                    value,
                    parse_constant=_reject_json_constant,
                    object_pairs_hook=_closed_json_object,
                )
            except json.JSONDecodeError as exc:
                raise TurnTraceError("turn trace must be one JSON object without surrounding text") from exc
        else:
            decoded = value
        item = _mapping(decoded, label="turn trace")
        _closed_keys(
            item,
            frozenset(
                {
                    "schema",
                    "turn_digest",
                    "conversation_digest",
                    "work_item_digest",
                    "work_relation",
                    "intent",
                    "continuation",
                    "playbook",
                    "steps",
                    "completion",
                    "publication",
                    "failure_stage",
                    "failure_reason",
                    "ambiguity_present",
                    "partial_coverage",
                    "state_restored",
                    "authority_rechecked",
                    "budget",
                }
            ),
            label="turn trace",
        )
        if item["schema"] != TURN_TRACE_SCHEMA:
            raise TurnTraceError(f"turn trace schema must be {TURN_TRACE_SCHEMA}")
        raw_steps = item["steps"]
        if not isinstance(raw_steps, list) or len(raw_steps) > _MAX_STEPS:
            raise TurnTraceError(f"steps must contain at most {_MAX_STEPS} items")
        work_item = item["work_item_digest"]
        if work_item is not None:
            work_item = _digest_value(work_item, label="work_item_digest")
        trace = cls(
            turn_digest=_digest_value(item["turn_digest"], label="turn_digest"),
            conversation_digest=_digest_value(item["conversation_digest"], label="conversation_digest"),
            work_item_digest=work_item,
            work_relation=_enum_value(WorkRelation, item["work_relation"], label="work_relation"),
            intent=_enum_value(IntentClass, item["intent"], label="intent"),
            continuation=_enum_value(ContinuationKind, item["continuation"], label="continuation"),
            playbook=_enum_value(PlaybookClass, item["playbook"], label="playbook"),
            steps=tuple(CapabilityStepTrace.parse(step) for step in raw_steps),
            completion=_enum_value(CompletionDecision, item["completion"], label="completion"),
            publication=_enum_value(PublicationStatus, item["publication"], label="publication"),
            failure_stage=_enum_value(FailureStage, item["failure_stage"], label="failure_stage"),
            failure_reason=_enum_value(FailureReason, item["failure_reason"], label="failure_reason"),
            ambiguity_present=_boolean(item["ambiguity_present"], label="ambiguity_present"),
            partial_coverage=_boolean(item["partial_coverage"], label="partial_coverage"),
            state_restored=_boolean(item["state_restored"], label="state_restored"),
            authority_rechecked=_boolean(item["authority_rechecked"], label="authority_rechecked"),
            budget=TraceBudget.parse(item["budget"]),
        )
        if len(trace.to_json().encode("utf-8")) > _MAX_SERIALIZED_BYTES:
            raise TurnTraceError(f"turn trace exceeds {_MAX_SERIALIZED_BYTES} serialized bytes")
        return trace

    def to_payload(self) -> dict[str, object]:
        """Return the only supported assistant-metadata/event projection."""

        return {
            "schema": TURN_TRACE_SCHEMA,
            "turn_digest": self.turn_digest,
            "conversation_digest": self.conversation_digest,
            "work_item_digest": self.work_item_digest,
            "work_relation": self.work_relation.value,
            "intent": self.intent.value,
            "continuation": self.continuation.value,
            "playbook": self.playbook.value,
            "steps": [step.to_payload() for step in self.steps],
            "completion": self.completion.value,
            "publication": self.publication.value,
            "failure_stage": self.failure_stage.value,
            "failure_reason": self.failure_reason.value,
            "ambiguity_present": self.ambiguity_present,
            "partial_coverage": self.partial_coverage,
            "state_restored": self.state_restored,
            "authority_rechecked": self.authority_rechecked,
            "budget": self.budget.to_payload(),
        }

    def to_json(self) -> str:
        """Serialize deterministically without accepting caller extensions."""

        encoded = json.dumps(
            self.to_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > _MAX_SERIALIZED_BYTES:  # pragma: no cover - bounded fields
            raise TurnTraceError(f"turn trace exceeds {_MAX_SERIALIZED_BYTES} serialized bytes")
        return encoded


__all__ = [
    "TURN_TRACE_SCHEMA",
    "CapabilityClass",
    "CapabilityStepTrace",
    "CompletionDecision",
    "ContinuationKind",
    "CountAccounting",
    "FailureReason",
    "FailureStage",
    "IntentClass",
    "OutcomeStatus",
    "PlaybookClass",
    "PublicationStatus",
    "TraceBudget",
    "TraceIdentifierDomain",
    "TokenAccounting",
    "TurnTrace",
    "TurnTraceError",
    "WorkRelation",
    "derive_trace_identifier",
]
