"""The first V12 route: current-turn, complete, registered file evidence."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from friday.evidence_bundle import EvidenceBundle
from friday.execution_kernel import (
    confirm_staged_request_effect,
    rollback_staged_request_effect,
    stage_request_effect_possible_in_transaction,
)
from friday.file_evidence import (
    CurrentTurnFileReferenceToken,
    current_turn_file_reference_token_authorizes_tenant,
)
from friday.file_evidence_reader import (
    FileEvidenceUnavailable,
    PreparedFileEvidence,
    prepare_current_turn_file_evidence,
    prepared_file_evidence_is_process_owned,
    reauthorize_prepared_file_evidence_in_transaction,
)
from friday.interaction_control_plane import (
    CapabilityClass,
    CountAccounting,
    IntentClass,
    PlaybookClass,
)
from friday.interaction_control_plane.runtime_trace import (
    attach_trace_to_metadata,
    build_committed_direct_trace,
    load_trace_namespace_key,
)
from friday.model_input_hygiene import (
    model_messages_are_secret_free,
    model_visible_text_is_secret_free,
)
from friday.model_profiles import (
    ModelCapability,
    ModelEffect,
    ModelProfileLease,
    ModelRequirements,
)
from friday.orchestration.capability_outcome import (
    CapabilityOutcome,
    CapabilityOutcomeError,
    CapabilityOutcomeStatus,
    attach_accepted_capability_outcome_receipt,
    load_accepted_capability_outcome_receipt,
    require_complete_read_only_publication,
)
from friday.orchestration.contracts import RouteClass, ToolEffect, TurnInput, TurnPlan
from friday.orchestration.file_read_contract import (
    V12_FILE_SYNTHESIS_SYSTEM,
    V12_FILE_VERIFIER_SCHEMA,
    V12_FILE_VERIFIER_SYSTEM,
    build_file_synthesis_messages,
    build_file_verifier_messages,
    require_file_verifier_clear,
    validate_file_synthesis_answer,
)
from friday.orchestration.router import (
    ReadOnlyAttachmentReference,
    ReadOnlyRoutePreparation,
    ReadOnlyRouteRequest,
    ReadOnlyRouteResult,
)
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    AuthorizedSourceKind,
)
from friday.orchestration.turn_context_call_scope import require_current_authenticated_chat_call_scope
from friday.orchestration.turn_context_runtime import current_primary_authenticated_turn_context
from friday.permissions import AuthorizationService
from friday.storage import normalize_conversation_mode
from friday.storage._conversations import (
    create_conversation_in_transaction,
    store_message_in_transaction,
)
from friday.storage._core import guarded_storage_transaction

_PROCESS_AUTHORITY = object()
_MAX_CANARY_FILES = 2
_MAX_ANSWER_JSON_UTF8_BYTES = 2_048
_SYNTHESIS_MAX_TOKENS = 512
_PREPARATION_BUDGET_SEC = 4.5
_PUBLICATION_RESERVE_SEC = 2.0
_MAX_ATTESTED_INPUT_UTF8_BYTES = 5_500
_BASE_CONTEXT_TOKENS = 8_192
_MAX_MEASURED_CONTEXT_TOKENS = 40_960
_CONTEXT_TOKEN_TIERS = (8_192, 16_384, 24_576, 32_768, 40_960)
_MAX_TRACE_LATENCY_MS = 86_400_000
_TWO_CALL_READ_MODEL_CALLS = 2
_UNSET_TURN_CONTEXT = object()
_PREPARED_TURN_BINDING_KEY = secrets.token_bytes(32)
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_RAW_ID_RE = re.compile(r"raw_[0-9a-f]{16}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_INTERACTION_MODES = frozenset({"dialogue", "knowledge_work", "research", "engineer", "coding"})
_PREPARED_FILE_ROUTES = frozenset({RouteClass.FILE_READ, RouteClass.ARCHIVE_READ})
LOGGER = logging.getLogger(__name__)


class V12FileReadError(RuntimeError):
    """A selected V12 file turn could not be safely published."""


class _V12ModelLeaseUnavailable(V12FileReadError):
    """The exact measured lease was lost before a bounded model call."""


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedFileContext:
    evidence: PreparedFileEvidence = field(repr=False)
    conversation_id: str | None
    interaction_mode: str


@dataclass(frozen=True, slots=True, repr=False)
class _PreparedFileTurn:
    evidence: PreparedFileEvidence = field(repr=False)
    turn_plan: TurnPlan = field(repr=False, compare=False)
    turn_plan_sha256: str
    conversation_id: str | None
    interaction_mode: str
    model_lease: ModelProfileLease = field(repr=False, compare=False)
    model_requirements: ModelRequirements = field(repr=False)
    authenticated_turn_context: AuthenticatedTurnContext | None = field(
        repr=False,
        compare=False,
    )
    _process_authority: object = field(repr=False, compare=False)
    _binding_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self._process_authority is not _PROCESS_AUTHORITY
            or not prepared_file_evidence_is_process_owned(self.evidence)
            or type(self.turn_plan) is not TurnPlan
            or self.turn_plan.route not in _PREPARED_FILE_ROUTES
            or type(self.turn_plan_sha256) is not str
            or _SHA256_RE.fullmatch(self.turn_plan_sha256) is None
            or not hmac.compare_digest(
                self.turn_plan_sha256,
                self.turn_plan.canonical_sha256(),
            )
            or type(self.model_lease) is not ModelProfileLease
            or type(self.model_requirements) is not ModelRequirements
            or self.model_requirements.prepared_evidence_items != len(self.evidence.bundle.parts)
            or (
                self.conversation_id is not None
                and (
                    type(self.conversation_id) is not str
                    or _CONVERSATION_ID_RE.fullmatch(self.conversation_id) is None
                )
            )
            or type(self.interaction_mode) is not str
            or self.interaction_mode not in _INTERACTION_MODES
            or (
                self.authenticated_turn_context is not None
                and type(self.authenticated_turn_context) is not AuthenticatedTurnContext
            )
            or not _model_lease_matches_requirements(self.model_lease, self.model_requirements)
            or not _prepared_file_turn_matches_context(self)
            or type(self._binding_sha256) is not str
            or _SHA256_RE.fullmatch(self._binding_sha256) is None
        ):
            raise ValueError("prepared V12 file turn is not process-owned")
        binding = _prepared_file_turn_binding_sha256(self)
        if binding is None or not hmac.compare_digest(self._binding_sha256, binding):
            raise ValueError("prepared V12 file turn is not process-owned")


def _model_lease_matches_requirements(
    lease: object,
    requirements: ModelRequirements,
) -> bool:
    try:
        expected_requirements = _file_requirements(
            requirements.prepared_evidence_items,
            requirements.required_context_tokens,
        )
    except (AttributeError, TypeError, ValueError):
        return False
    if not isinstance(lease, ModelProfileLease) or type(lease) is not ModelProfileLease:
        return False
    return bool(
        type(requirements) is ModelRequirements
        and requirements is expected_requirements
        and type(lease.requirements_sha256) is str
        and _SHA256_RE.fullmatch(lease.requirements_sha256) is not None
        and hmac.compare_digest(lease.requirements_sha256, requirements.canonical_sha256())
        and lease.capabilities == requirements.capabilities
        and lease.required_context_tokens == requirements.required_context_tokens
        and lease.prepared_evidence_items == requirements.prepared_evidence_items
        and lease.max_tool_steps == requirements.max_tool_steps
        and lease.max_tool_rounds == requirements.max_tool_rounds
        and lease.max_tool_calls == requirements.max_tool_calls
        and lease.effect is requirements.effect
        and lease.verifier_required is requirements.verifier_required
    )


def _prepared_file_turn_matches_context(prepared: _PreparedFileTurn) -> bool:
    context = prepared.authenticated_turn_context
    if context is None:
        return True
    conversation_id = context.authority.conversation_id
    current_sources = tuple(
        source
        for source in context.authorized_sources
        if source.kind is AuthorizedSourceKind.CURRENT_ATTACHMENT
    )
    if (
        type(conversation_id) is not str
        or _CONVERSATION_ID_RE.fullmatch(conversation_id) is None
        or prepared.conversation_id != conversation_id
        or prepared.interaction_mode != context.authority.interaction_mode.value
        or prepared.evidence.tenant_id != context.authority.tenant_id
        or prepared.evidence.person_id != context.authority.person_id
        or len(current_sources) != len(prepared.evidence.snapshot_tokens)
    ):
        return False
    return all(
        type(source.private_carrier) is CurrentTurnFileReferenceToken
        and source.private_carrier.raw_id == token.source.raw_id
        and source.private_carrier.source_identity_sha256 == token.source.identity_sha256
        and source.private_carrier.content_sha256 == token.content_sha256
        for source, token in zip(
            current_sources,
            prepared.evidence.snapshot_tokens,
            strict=True,
        )
    )


def _prepared_file_turn_process_seal(
    *,
    evidence: PreparedFileEvidence,
    turn_plan: TurnPlan,
    turn_plan_sha256: str,
    conversation_id: str | None,
    interaction_mode: str,
    lease: ModelProfileLease,
    requirements: ModelRequirements,
    context: AuthenticatedTurnContext | None,
) -> str | None:
    try:
        parts = (
            str(id(evidence)),
            evidence.identity_sha256,
            str(id(turn_plan)),
            turn_plan_sha256,
            turn_plan.canonical_sha256(),
            conversation_id or "<new-conversation>",
            interaction_mode,
            str(id(lease)),
            lease.schema,
            lease.profile_id,
            lease.attestation_sha256,
            lease.requirements_sha256,
            lease.process_epoch_sha256,
            str(lease._gate_generation),
            str(id(lease._gate_authority)),
            str(id(requirements)),
            requirements.canonical_sha256(),
            str(id(context)) if context is not None else "<legacy-context>",
            context.canonical_sha256() if context is not None else "<legacy-context>",
        )
        digest = hmac.new(
            _PREPARED_TURN_BINDING_KEY,
            b"friday/prepared-v12-file-turn/v1\0",
            hashlib.sha256,
        )
        for part in parts:
            if type(part) is not str:
                return None
            encoded = part.encode("utf-8", errors="strict")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        return digest.hexdigest()
    except (AttributeError, TypeError, UnicodeEncodeError, ValueError):
        return None


def _prepared_file_turn_binding_sha256(prepared: object) -> str | None:
    if type(prepared) is not _PreparedFileTurn:
        return None
    try:
        lease = prepared.model_lease
        requirements = prepared.model_requirements
        context = prepared.authenticated_turn_context
        if (
            prepared._process_authority is not _PROCESS_AUTHORITY
            or not prepared_file_evidence_is_process_owned(prepared.evidence)
            or not _model_lease_matches_requirements(lease, requirements)
            or not _prepared_file_turn_matches_context(prepared)
        ):
            return None
        return _prepared_file_turn_process_seal(
            evidence=prepared.evidence,
            turn_plan=prepared.turn_plan,
            turn_plan_sha256=prepared.turn_plan_sha256,
            conversation_id=prepared.conversation_id,
            interaction_mode=prepared.interaction_mode,
            lease=lease,
            requirements=requirements,
            context=context,
        )
    except (AttributeError, TypeError, UnicodeEncodeError, ValueError):
        return None


def _prepared_file_turn_is_bound(prepared: object) -> bool:
    if type(prepared) is not _PreparedFileTurn:
        return False
    expected = _prepared_file_turn_binding_sha256(prepared)
    return bool(
        expected is not None
        and type(prepared._binding_sha256) is str
        and _SHA256_RE.fullmatch(prepared._binding_sha256) is not None
        and hmac.compare_digest(prepared._binding_sha256, expected)
    )


def _require_prepared_file_turn_bound(prepared: object) -> _PreparedFileTurn:
    if not _prepared_file_turn_is_bound(prepared):
        raise V12FileReadError("file preparation authority drifted")
    return cast(_PreparedFileTurn, prepared)


class _AttestedFileModel(Protocol):
    def available_context_tokens(self) -> int: ...

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None: ...

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool: ...

    def lease_is_process_current(
        self,
        lease: object,
        requirements: ModelRequirements,
    ) -> bool: ...

    async def complete(
        self,
        lease: object,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None,
        priority: Literal["foreground", "background"],
        absolute_deadline: float,
        temperature: float | None = 0.0,
    ) -> dict[str, Any]: ...


_FILE_REQUIREMENTS_BY_CONTEXT_AND_EVIDENCE = tuple(
    tuple(
        ModelRequirements(
            capabilities=frozenset(
                {
                    ModelCapability.PREPARED_EVIDENCE_2,
                    ModelCapability.CONTEXT_8K,
                    ModelCapability.REMOTE_CANCELLATION,
                }
            ),
            required_context_tokens=context_tokens,
            prepared_evidence_items=evidence_items,
            max_tool_steps=0,
            max_tool_rounds=0,
            max_tool_calls=0,
            effect=ModelEffect.READ,
            verifier_required=True,
        )
        for evidence_items in range(1, _MAX_CANARY_FILES + 1)
    )
    for context_tokens in _CONTEXT_TOKEN_TIERS
)


def _file_requirements(
    evidence_items: int,
    required_context_tokens: int = _BASE_CONTEXT_TOKENS,
) -> ModelRequirements:
    """Return one exact process-wide lease projection for evidence and context tier."""

    if type(evidence_items) is not int or not 1 <= evidence_items <= _MAX_CANARY_FILES:
        raise ValueError("file model evidence count is outside the closed lease projection")
    if type(required_context_tokens) is not int or required_context_tokens not in _CONTEXT_TOKEN_TIERS:
        raise ValueError("file model context is outside the closed measured tiers")
    context_index = _CONTEXT_TOKEN_TIERS.index(required_context_tokens)
    return _FILE_REQUIREMENTS_BY_CONTEXT_AND_EVIDENCE[context_index][evidence_items - 1]


def _attested_input_max_bytes(required_context_tokens: int) -> int:
    if type(required_context_tokens) is not int or required_context_tokens not in _CONTEXT_TOKEN_TIERS:
        return 0
    return (_MAX_ATTESTED_INPUT_UTF8_BYTES * required_context_tokens) // _BASE_CONTEXT_TOKENS


def _model_available_context_tokens(model: object) -> int:
    """Read only the code-owned current capacity; legacy test doubles stay at baseline."""

    method = getattr(model, "available_context_tokens", None)
    if not callable(method):
        return _BASE_CONTEXT_TOKENS
    try:
        value = method()
    except Exception:
        return 0
    if type(value) is not int or value < _BASE_CONTEXT_TOKENS:
        return 0
    return min(value, _MAX_MEASURED_CONTEXT_TOKENS)


def _model_available_context_tier(model: object) -> int:
    available = _model_available_context_tokens(model)
    return max(
        (tier for tier in _CONTEXT_TOKEN_TIERS if tier <= available),
        default=0,
    )


def _file_requirements_for_input_bytes(
    model: object,
    evidence_items: int,
    *required_input_bytes: int,
    available_context_tokens: int | None = None,
) -> ModelRequirements | None:
    """Choose the least measured context tier that fits every bounded model call."""

    if not required_input_bytes or any(type(value) is not int or value < 0 for value in required_input_bytes):
        return None
    available = (
        _model_available_context_tier(model) if available_context_tokens is None else available_context_tokens
    )
    if type(available) is not int or available not in _CONTEXT_TOKEN_TIERS:
        return None
    required = max(required_input_bytes)
    for context_tokens in _CONTEXT_TOKEN_TIERS:
        if context_tokens > available:
            break
        if required <= _attested_input_max_bytes(context_tokens):
            return _file_requirements(evidence_items, context_tokens)
    return None


def _two_call_read_model_output_limits(
    context: AuthenticatedTurnContext | None,
    *,
    synthesis_max_tokens: int,
    verifier_max_tokens: int,
) -> tuple[int, int]:
    """Intersect fixed model-call bounds with the authenticated parent budget."""

    if (
        type(synthesis_max_tokens) is not int
        or type(verifier_max_tokens) is not int
        or synthesis_max_tokens <= 0
        or verifier_max_tokens <= 0
    ):
        raise ValueError("read-model output limits must be positive integers")
    if context is None:
        return synthesis_max_tokens, verifier_max_tokens
    parent = context.inherited_budget
    child = parent.derive_child(
        safety_deadline_monotonic_ns=parent.safety_deadline.monotonic_ns,
        max_model_calls=_TWO_CALL_READ_MODEL_CALLS,
        max_model_retries=0,
        max_tool_calls=0,
        max_tool_rounds=0,
        max_advisory_calls=0,
        max_output_tokens=max(synthesis_max_tokens, verifier_max_tokens),
    )
    if child.model_anti_loop.max_model_calls < _TWO_CALL_READ_MODEL_CALLS:
        raise V12FileReadError("authenticated turn has no file model-call budget")
    if child.resources.max_tool_calls != 0 or child.resources.max_tool_rounds != 0:
        raise V12FileReadError("file model journey gained tool authority")
    return (
        min(synthesis_max_tokens, child.resources.max_output_tokens),
        min(verifier_max_tokens, child.resources.max_output_tokens),
    )


async def _lease_is_current_before_deadline(
    model: _AttestedFileModel,
    lease: object,
    requirements: ModelRequirements,
    *,
    absolute_deadline: float,
) -> bool:
    """Physically bound one exact lease recheck to the inherited deadline."""

    deadline = _validated_future_deadline(
        absolute_deadline,
        stage="before model lease check",
    )
    remaining = deadline - time.monotonic()
    if not _model_lease_matches_requirements(lease, requirements):
        return False
    current = await asyncio.wait_for(
        model.lease_is_current(
            lease,
            requirements,
            absolute_deadline=deadline,
        ),
        timeout=remaining,
    )
    return type(current) is bool and current


def _lease_is_process_current(
    model: _AttestedFileModel,
    lease: object,
    requirements: ModelRequirements,
) -> bool:
    """Recheck one exact lease against the non-I/O process gate."""

    if type(lease) is not ModelProfileLease or not _model_lease_matches_requirements(lease, requirements):
        return False
    try:
        current = model.lease_is_process_current(lease, requirements)
    except Exception:
        return False
    return current is True and _model_lease_matches_requirements(lease, requirements)


def _messages_fit_attested_context(
    messages: list[dict[str, str]],
    required_context_tokens: int = _BASE_CONTEXT_TOKENS,
) -> bool:
    return len(
        json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ) <= _attested_input_max_bytes(required_context_tokens)


def _validated_future_deadline(
    deadline: object,
    *,
    stage: str,
    reserve: float = 0.0,
) -> float:
    if type(deadline) not in (int, float):
        raise TypeError(f"V12 publication deadline is invalid {stage}")
    value = float(cast("int | float", deadline))
    if not math.isfinite(value):
        raise ValueError(f"V12 publication deadline is invalid {stage}")
    if value - time.monotonic() <= reserve:
        raise TimeoutError(f"V12 publication deadline expired {stage}")
    return value


def _require_deadline(deadline: float, *, stage: str, reserve: float = 0.0) -> None:
    _validated_future_deadline(deadline, stage=stage, reserve=reserve)


def _authenticated_file_references_match(
    context: AuthenticatedTurnContext,
    references: tuple[ReadOnlyAttachmentReference, ...],
) -> bool:
    sources = tuple(
        source
        for source in context.authorized_sources
        if source.kind is not AuthorizedSourceKind.ACCEPTED_INGRESS
    )
    if len(sources) != len(references):
        return False
    for source, reference in zip(sources, references, strict=True):
        descriptor = source.model_descriptor
        token = source.private_carrier
        if (
            source.kind is not AuthorizedSourceKind.CURRENT_ATTACHMENT
            or type(reference) is not ReadOnlyAttachmentReference
            or type(reference.ordinal) is not int
            or not 1 <= reference.ordinal <= _MAX_CANARY_FILES
            or type(reference.raw_object_id) is not str
            or _RAW_ID_RE.fullmatch(reference.raw_object_id) is None
            or type(reference.source_identity_sha256) is not str
            or _SHA256_RE.fullmatch(reference.source_identity_sha256) is None
            or type(reference.name) is not str
            or type(reference.media_type) is not str
            or type(token) is not CurrentTurnFileReferenceToken
            or not current_turn_file_reference_token_authorizes_tenant(
                token,
                tenant_id=context.authority.tenant_id,
            )
            or descriptor is None
            or source.ordinal != reference.ordinal
            or descriptor.ordinal != reference.ordinal
            or type(descriptor.name) is not str
            or descriptor.name != reference.name
            or type(descriptor.media_type) is not str
            or descriptor.media_type != reference.media_type
            or not hmac.compare_digest(token.raw_id, reference.raw_object_id)
            or not hmac.compare_digest(
                token.source_identity_sha256,
                reference.source_identity_sha256,
            )
        ):
            return False
    return True


def _require_v12_turn_context(
    request: ReadOnlyRouteRequest,
    turn: TurnInput,
    *,
    expected: AuthenticatedTurnContext | None | object = _UNSET_TURN_CONTEXT,
) -> AuthenticatedTurnContext | None:
    """Revalidate the exact ambient primary context without weakening legacy calls."""

    context = current_primary_authenticated_turn_context()
    if expected is not _UNSET_TURN_CONTEXT and context is not expected:
        raise V12FileReadError("authenticated file-turn context identity drifted")
    if context is not None and (
        context.model_input is not turn
        or context.authority.actor is not request.actor
        or type(request.user_id) is not str
        or context.authority.tenant_id != request.user_id
        or type(request.conversation_id) is not str
        or _CONVERSATION_ID_RE.fullmatch(request.conversation_id) is None
        or type(context.authority.conversation_id) is not str
        or not hmac.compare_digest(
            context.authority.conversation_id,
            request.conversation_id,
        )
        or request.conversation_mode != turn.conversation_mode
        or request.synthetic_document_notice is not turn.synthetic_document_notice
        or request.reply_to != turn.reply_quote
        or request.quoted_attachment_reference is not turn.quoted_attachment_reference
        or request.reply_assistant_reference is not turn.reply_assistant_reference
        or request.replay_source_message_id is not None
        or request.reply_assistant_message_id is not None
        or not _authenticated_file_references_match(context, request.attachments)
    ):
        raise V12FileReadError("authenticated file-turn inputs drifted")
    if context is not None:
        require_current_authenticated_chat_call_scope(context)
    return context


def _within_parent_deadline(
    deadline: float,
    context: AuthenticatedTurnContext | None,
) -> float:
    candidate = _validated_future_deadline(
        deadline,
        stage="before inherited parent clamp",
    )
    if context is None:
        return candidate
    parent = _validated_future_deadline(
        math.nextafter(
            context.inherited_budget.safety_deadline.monotonic_ns / 1_000_000_000,
            -math.inf,
        ),
        stage="at inherited parent clamp",
    )
    return _validated_future_deadline(
        min(candidate, parent),
        stage="after inherited parent clamp",
    )


async def _call_model_once(
    model: _AttestedFileModel,
    lease: ModelProfileLease,
    requirements: ModelRequirements,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    deadline: float,
    priority: Literal["foreground", "background"],
    on_dispatch: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if not model_messages_are_secret_free(messages):
        raise V12FileReadError("model payload requires a secret projection")
    if not _messages_fit_attested_context(messages, requirements.required_context_tokens):
        raise V12FileReadError("model payload exceeds the attested context tier")
    try:
        deadline = _validated_future_deadline(deadline, stage="before model call")
    except TimeoutError:
        raise TimeoutError("V12 file route has no model budget") from None
    remaining = deadline - time.monotonic()
    if remaining <= _PUBLICATION_RESERVE_SEC:
        raise TimeoutError("V12 file route has no model budget")
    model_deadline = deadline - _PUBLICATION_RESERVE_SEC
    if not await _lease_is_current_before_deadline(
        model,
        lease,
        requirements,
        absolute_deadline=model_deadline,
    ):
        raise _V12ModelLeaseUnavailable("file model authority changed before model call")
    call_remaining = model_deadline - time.monotonic()
    if call_remaining <= 0:
        raise TimeoutError("V12 file route has no model budget")

    async def dispatch() -> dict[str, Any]:
        if on_dispatch is not None:
            on_dispatch()
        return await model.complete(
            lease,
            requirements,
            messages,
            max_tokens=max_tokens,
            priority=priority,
            absolute_deadline=model_deadline,
            temperature=0.0,
        )

    response = await asyncio.wait_for(
        dispatch(),
        timeout=call_remaining,
    )
    if not isinstance(response, dict):
        raise V12FileReadError("model returned a non-object response")
    if response.get("finish_reason") != "stop" or response.get("tool_calls") not in (None, []):
        raise V12FileReadError("model response was incomplete or effectful")
    content = response.get("content")
    if not isinstance(content, str):
        raise V12FileReadError("model response has no text")
    return response


class V12FileReadHandler:
    """Read, synthesize, verify and atomically publish one current-file turn."""

    route = RouteClass.FILE_READ
    effect = ToolEffect.READ

    def __init__(
        self,
        *,
        storage: Any,
        authorization: AuthorizationService,
        settings: Any,
        model: _AttestedFileModel,
    ) -> None:
        self._storage = storage
        self._authorization = authorization
        self._settings = settings
        self._model = model

    def _prepare_sync(
        self,
        request: ReadOnlyRouteRequest,
        absolute_deadline: float,
    ) -> _PreparedFileContext | None:
        if request.user_id != request.actor.user_id or not 1 <= len(request.attachments) <= _MAX_CANARY_FILES:
            return None
        try:
            evidence = prepare_current_turn_file_evidence(
                self._storage,
                self._authorization,
                self._settings.files_dir,
                request.actor,
                request.attachments,
                max_bytes=self._settings.max_upload_bytes,
                absolute_deadline=absolute_deadline,
            )
        except (FileEvidenceUnavailable, TimeoutError):
            return None

        conversation_id = request.conversation_id
        if conversation_id is not None:
            conversation = self._storage.get_conversation(conversation_id, request.actor.own_id)
            if not isinstance(conversation, dict):
                return None
            interaction_mode = normalize_conversation_mode(str(conversation.get("mode") or "dialogue"))
        else:
            interaction_mode = normalize_conversation_mode(request.conversation_mode or "dialogue")
        return _PreparedFileContext(
            evidence=evidence,
            conversation_id=conversation_id,
            interaction_mode=interaction_mode,
        )

    async def _prepare_context(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        absolute_deadline: float,
    ) -> _PreparedFileContext | None:
        """Strategy seam for another read-only route over the same evidence plane."""

        del turn, plan
        return await asyncio.to_thread(self._prepare_sync, request, absolute_deadline)

    async def prepare(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
    ) -> ReadOnlyRoutePreparation | None:
        if type(plan) is not TurnPlan or plan.route is not self.route:
            return None
        plan_sha256 = plan.canonical_sha256()
        authenticated_context = _require_v12_turn_context(request, turn)
        preparation_deadline = time.monotonic() + _PREPARATION_BUDGET_SEC
        if request.turn_deadline is not None:
            request_deadline = _validated_future_deadline(
                request.turn_deadline,
                stage="before preparation clamp",
            )
            preparation_deadline = min(preparation_deadline, request_deadline)
        preparation_deadline = _within_parent_deadline(
            preparation_deadline,
            authenticated_context,
        )
        if authenticated_context is not None:
            _require_deadline(preparation_deadline, stage="before preparation")
        _two_call_read_model_output_limits(
            authenticated_context,
            synthesis_max_tokens=_SYNTHESIS_MAX_TOKENS,
            verifier_max_tokens=256,
        )
        prepared = await self._prepare_context(
            request,
            turn,
            plan,
            preparation_deadline,
        )
        if not hmac.compare_digest(plan_sha256, plan.canonical_sha256()):
            raise V12FileReadError("turn plan changed during preparation")
        _require_v12_turn_context(
            request,
            turn,
            expected=authenticated_context,
        )
        if prepared is None:
            return None
        synthesis_messages = self._synthesis_messages(turn, plan, prepared.evidence.bundle)
        verifier_messages = self._verifier_messages(turn, prepared.evidence.bundle, "")
        empty_answer_bytes = len(json.dumps("", ensure_ascii=False).encode("utf-8"))
        reserved_verifier_bytes = (
            len(json.dumps(verifier_messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            # The answer is JSON-encoded once into the verifier's user
            # content, then that content is encoded again as a chat message.
            # Quotes/backslashes can therefore expand twice.  Reserve the
            # exact closed answer budget at the worst two-byte expansion so
            # every admitted answer can reach the verifier without truncation.
            + 2 * (_MAX_ANSWER_JSON_UTF8_BYTES - empty_answer_bytes)
        )
        synthesis_input_bytes = len(
            json.dumps(synthesis_messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        requirements = _file_requirements_for_input_bytes(
            self._model,
            len(prepared.evidence.bundle.parts),
            synthesis_input_bytes,
            reserved_verifier_bytes,
        )
        if (
            requirements is None
            or not model_messages_are_secret_free(synthesis_messages)
            or not _messages_fit_attested_context(
                synthesis_messages,
                requirements.required_context_tokens,
            )
            or not model_messages_are_secret_free(verifier_messages)
            or reserved_verifier_bytes > _attested_input_max_bytes(requirements.required_context_tokens)
        ):
            return None
        lease_remaining = preparation_deadline - time.monotonic()
        if lease_remaining <= 0:
            raise TimeoutError("V12 publication deadline expired before lease acquisition")
        lease = await asyncio.wait_for(
            self._model.acquire_lease(
                requirements,
                absolute_deadline=preparation_deadline,
            ),
            timeout=lease_remaining,
        )
        if not hmac.compare_digest(plan_sha256, plan.canonical_sha256()):
            raise V12FileReadError("turn plan changed during preparation")
        _require_v12_turn_context(
            request,
            turn,
            expected=authenticated_context,
        )
        if type(lease) is not ModelProfileLease:
            return None
        binding_sha256 = _prepared_file_turn_process_seal(
            evidence=prepared.evidence,
            turn_plan=plan,
            turn_plan_sha256=plan_sha256,
            conversation_id=prepared.conversation_id,
            interaction_mode=prepared.interaction_mode,
            lease=lease,
            requirements=requirements,
            context=authenticated_context,
        )
        if binding_sha256 is None:
            raise V12FileReadError("file preparation authority cannot be sealed")
        attested = _PreparedFileTurn(
            evidence=prepared.evidence,
            turn_plan=plan,
            turn_plan_sha256=plan_sha256,
            conversation_id=prepared.conversation_id,
            interaction_mode=prepared.interaction_mode,
            model_lease=lease,
            model_requirements=requirements,
            authenticated_turn_context=authenticated_context,
            _process_authority=_PROCESS_AUTHORITY,
            _binding_sha256=binding_sha256,
        )
        return ReadOnlyRoutePreparation(
            route=self.route,
            plan_sha256=plan_sha256,
            evidence_identity_sha256=attested.evidence.identity_sha256,
            private_payload=attested,
        )

    def _prepared_matches(
        self,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> _PreparedFileTurn | None:
        prepared = preparation.private_payload
        if (
            type(prepared) is not _PreparedFileTurn
            or not _prepared_file_turn_is_bound(prepared)
            or not prepared_file_evidence_is_process_owned(prepared.evidence)
            or type(prepared.model_lease) is not ModelProfileLease
            or prepared.turn_plan is not plan
            or not hmac.compare_digest(prepared.turn_plan_sha256, plan.canonical_sha256())
            or plan.route is not self.route
            or preparation.route is not self.route
            or preparation.plan_sha256 != prepared.turn_plan_sha256
            or preparation.evidence_identity_sha256 != prepared.evidence.identity_sha256
        ):
            return None
        return prepared

    async def preparation_is_current(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> bool:
        prepared = self._prepared_matches(plan, preparation)
        if prepared is None:
            return False
        authenticated_context = _require_v12_turn_context(
            request,
            turn,
            expected=prepared.authenticated_turn_context,
        )
        deadline = (
            request.turn_deadline
            if request.turn_deadline is not None
            else time.monotonic() + _PREPARATION_BUDGET_SEC
        )
        deadline = _within_parent_deadline(deadline, authenticated_context)
        if authenticated_context is not None:
            _require_deadline(deadline, stage="before preparation authority check")
        current = await _lease_is_current_before_deadline(
            self._model,
            prepared.model_lease,
            prepared.model_requirements,
            absolute_deadline=deadline,
        )
        _require_prepared_file_turn_bound(prepared)
        _require_v12_turn_context(
            request,
            turn,
            expected=authenticated_context,
        )
        return current

    @staticmethod
    def _synthesis_messages(
        turn: TurnInput,
        plan: TurnPlan,
        bundle: EvidenceBundle,
    ) -> list[dict[str, str]]:
        return build_file_synthesis_messages(turn, plan, bundle)

    async def _synthesize(
        self,
        turn: TurnInput,
        plan: TurnPlan,
        bundle: EvidenceBundle,
        lease: ModelProfileLease,
        requirements: ModelRequirements,
        *,
        deadline: float,
        max_tokens: int = _SYNTHESIS_MAX_TOKENS,
    ) -> str:
        response = await _call_model_once(
            self._model,
            lease,
            requirements,
            self._synthesis_messages(turn, plan, bundle),
            max_tokens=max_tokens,
            deadline=deadline,
            priority="foreground",
        )
        try:
            return validate_file_synthesis_answer(
                response["content"],
                bundle.citation_labels,
            )
        except ValueError:
            raise V12FileReadError("synthesis returned unsafe text") from None

    @staticmethod
    def _verifier_messages(
        turn: TurnInput,
        bundle: EvidenceBundle,
        answer: str,
    ) -> list[dict[str, str]]:
        return build_file_verifier_messages(turn, bundle, answer)

    async def _verify(
        self,
        turn: TurnInput,
        bundle: EvidenceBundle,
        answer: str,
        lease: ModelProfileLease,
        requirements: ModelRequirements,
        *,
        deadline: float,
        max_tokens: int = 256,
    ) -> None:
        response = await _call_model_once(
            self._model,
            lease,
            requirements,
            self._verifier_messages(turn, bundle, answer),
            max_tokens=max_tokens,
            deadline=deadline,
            priority="foreground",
        )
        try:
            require_file_verifier_clear(response["content"], bundle.citation_labels)
        except ValueError:
            raise V12FileReadError("verifier rejected the answer") from None

    def _completion_outcome(
        self,
        plan: TurnPlan,
        evidence: PreparedFileEvidence,
    ) -> CapabilityOutcome:
        """Build the only outcome currently publishable by the narrow canary."""

        return CapabilityOutcome(
            route=self.route,
            status=CapabilityOutcomeStatus.COMPLETE,
            plan_sha256=plan.canonical_sha256(),
            evidence_identity_sha256=evidence.identity_sha256,
            citation_labels=evidence.bundle.citation_labels,
            authority_rechecked=True,
            verified=True,
        )

    def _publish_sync(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        prepared: _PreparedFileTurn,
        answer: str,
        *,
        deadline: float,
        trace_started_at: float,
        trace_planner_model_calls: int,
    ) -> tuple[str, str, str, CapabilityOutcome]:
        _require_prepared_file_turn_bound(prepared)
        authenticated_context = _require_v12_turn_context(
            request,
            turn,
            expected=prepared.authenticated_turn_context,
        )
        _require_deadline(
            deadline,
            stage="before effect ownership",
            reserve=_PUBLICATION_RESERVE_SEC,
        )
        if not model_visible_text_is_secret_free(answer):
            raise V12FileReadError("publication output requires a secret projection")
        evidence = prepared.evidence
        plan_sha256 = prepared.turn_plan_sha256

        def before_commit() -> None:
            _require_prepared_file_turn_bound(prepared)
            _require_v12_turn_context(
                request,
                turn,
                expected=authenticated_context,
            )
            _require_deadline(deadline, stage="before transaction commit")
            if not _lease_is_process_current(
                self._model,
                prepared.model_lease,
                prepared.model_requirements,
            ):
                raise V12FileReadError("file model authority changed before transaction commit")
            if not model_visible_text_is_secret_free(answer):
                raise V12FileReadError("publication output requires a secret projection")

        try:
            with guarded_storage_transaction(
                self._storage,
                before_commit=before_commit,
                lock_timeout_sec=max(
                    0.0,
                    deadline - time.monotonic() - _PUBLICATION_RESERVE_SEC,
                ),
                after_commit=confirm_staged_request_effect,
                after_rollback=rollback_staged_request_effect,
            ) as conn:
                _require_prepared_file_turn_bound(prepared)
                if not reauthorize_prepared_file_evidence_in_transaction(
                    conn,
                    self._authorization,
                    self._settings.files_dir,
                    request.actor,
                    evidence,
                    max_bytes=self._settings.max_upload_bytes,
                    storage=self._storage,
                ):
                    raise V12FileReadError("file authority changed before publication")
                _require_deadline(deadline, stage="during final reauthorization")
                _require_prepared_file_turn_bound(prepared)
                _require_v12_turn_context(
                    request,
                    turn,
                    expected=authenticated_context,
                )
                if not model_visible_text_is_secret_free(answer):
                    raise V12FileReadError("publication output requires a secret projection")
                _require_prepared_file_turn_bound(prepared)
                conversation_id = prepared.conversation_id
                interaction_mode = prepared.interaction_mode
                if conversation_id is None:
                    if authenticated_context is not None:
                        raise V12FileReadError("authenticated file turn cannot create a conversation")
                    conversation = create_conversation_in_transaction(
                        conn,
                        request.actor.own_id,
                        title=turn.message[:80],
                        mode=interaction_mode,
                    )
                    conversation_id = str(conversation.get("id") or "")
                else:
                    conversation_row = conn.execute(
                        "SELECT id, mode FROM conversations WHERE id=? AND user_id=?",
                        (conversation_id, request.actor.own_id),
                    ).fetchone()
                    if conversation_row is None:
                        raise V12FileReadError("conversation authority changed before publication")
                    current_interaction_mode = normalize_conversation_mode(
                        str(conversation_row["mode"] or "dialogue")
                    )
                    if current_interaction_mode != prepared.interaction_mode:
                        raise V12FileReadError("conversation mode changed before publication")
                    interaction_mode = current_interaction_mode
                if (
                    authenticated_context is not None
                    and interaction_mode != authenticated_context.authority.interaction_mode.value
                ):
                    raise V12FileReadError("authenticated conversation mode changed before publication")
                _require_v12_turn_context(
                    request,
                    turn,
                    expected=authenticated_context,
                )
                if not _lease_is_process_current(
                    self._model,
                    prepared.model_lease,
                    prepared.model_requirements,
                ):
                    raise V12FileReadError("file model authority changed before publication")
                _require_deadline(deadline, stage="after final process lease recheck")
                _require_prepared_file_turn_bound(prepared)
                _require_v12_turn_context(
                    request,
                    turn,
                    expected=authenticated_context,
                )
                expected_effect_binding = (
                    authenticated_context.effect_fence.request_effect_binding_sha256
                    if authenticated_context is not None
                    else None
                )
                if not stage_request_effect_possible_in_transaction(
                    conn,
                    expected_request_binding_sha256=expected_effect_binding,
                ):
                    raise V12FileReadError("request effect fence could not be committed")

                # Both source and conversation authority are now current in the
                # same transaction that will own the two durable message rows.
                outcome = self._completion_outcome(plan, evidence)
                try:
                    require_complete_read_only_publication(
                        outcome,
                        expected_route=self.route,
                        expected_plan_sha256=plan_sha256,
                        expected_evidence_identity_sha256=evidence.identity_sha256,
                        expected_citation_labels=evidence.bundle.citation_labels,
                        answer=answer,
                        authority_rechecked=True,
                        verification_passed=True,
                    )
                except CapabilityOutcomeError:
                    raise V12FileReadError("completion gate rejected publication") from None

                route_mode = f"v12_{self.route.value}"
                user_metadata = {
                    "answer_mode": f"{route_mode}_request",
                    "private_context_lineage": True,
                    "v12_plan_sha256": plan_sha256,
                }
                if evidence.historical_selection is None:
                    user_metadata["conversation_uploaded_raw_ids"] = list(evidence.raw_ids)
                else:
                    user_metadata["conversation_attachment_raw_ids"] = list(evidence.raw_ids)
                    user_metadata["conversation_attachment_uploaders"] = {
                        raw_id: evidence.person_id for raw_id in evidence.raw_ids
                    }
                _require_deadline(deadline, stage="before durable messages")
                _require_prepared_file_turn_bound(prepared)
                _require_v12_turn_context(
                    request,
                    turn,
                    expected=authenticated_context,
                )
                user_message = store_message_in_transaction(
                    conn,
                    conversation_id,
                    request.actor.own_id,
                    "user",
                    turn.message,
                    metadata=user_metadata,
                )
                user_message_id = str(user_message.get("id") or "")
                if not re.fullmatch(r"msg_[0-9a-f]{16}", user_message_id):
                    raise V12FileReadError("user publication has no durable identity")
                assistant_metadata = {
                    "answer_mode": route_mode,
                    "attachment_context_used": True,
                    "attachment_context_expected_count": len(evidence.raw_ids),
                    "attachment_context_readable_count": len(evidence.raw_ids),
                    "attachment_coverage_complete": True,
                    "attachment_verification_complete": True,
                    "citation_check": {
                        "status": "verified",
                        "checked": len(evidence.bundle.citation_labels),
                    },
                    "conversation_attachment_raw_ids": list(evidence.raw_ids),
                    "conversation_attachment_uploaders": {
                        raw_id: evidence.person_id for raw_id in evidence.raw_ids
                    },
                    "evidence_identity_sha256": evidence.identity_sha256,
                    "interaction_mode": interaction_mode,
                    "knowledge_citations": {},
                    "private_context_lineage": True,
                    "tools_used": [],
                    "v12_plan_sha256": plan_sha256,
                    "verification": {"status": "verified", "score": 1.0, "issues": []},
                    "verification_status": "verified",
                    "verified": True,
                }
                try:
                    attach_accepted_capability_outcome_receipt(assistant_metadata, outcome)
                except CapabilityOutcomeError:
                    raise V12FileReadError(
                        "accepted capability outcome receipt rejected publication"
                    ) from None
                try:
                    trace = build_committed_direct_trace(
                        namespace_key=load_trace_namespace_key(conn),
                        turn_identifier=user_message_id,
                        conversation_identifier=conversation_id,
                        intent=IntentClass.DOCUMENT_WORK,
                        playbook=PlaybookClass.DIRECT,
                        capabilities=(
                            *((CapabilityClass.MODEL_PLANNING,) if trace_planner_model_calls else ()),
                            CapabilityClass.DOCUMENT_RETRIEVAL,
                            CapabilityClass.MODEL_SYNTHESIS,
                            CapabilityClass.VERIFICATION,
                        ),
                        latency_ms=min(
                            _MAX_TRACE_LATENCY_MS,
                            max(0, int((time.monotonic() - trace_started_at) * 1_000)),
                        ),
                        model_calls=2 + trace_planner_model_calls,
                        model_call_accounting=(
                            CountAccounting.LOWER_BOUND
                            if trace_planner_model_calls
                            else CountAccounting.COMPLETE
                        ),
                        capability_calls=1,
                        capability_call_accounting=CountAccounting.COMPLETE,
                        authority_rechecked=True,
                    )
                    attach_trace_to_metadata(assistant_metadata, trace)
                except Exception as exc:  # noqa: BLE001 - shadow tracing cannot abort publication
                    LOGGER.warning("interaction-trace omitted (%s)", type(exc).__name__)
                try:
                    load_accepted_capability_outcome_receipt(
                        assistant_metadata,
                        expected_outcome=outcome,
                    )
                except CapabilityOutcomeError:
                    raise V12FileReadError(
                        "accepted capability outcome receipt rejected publication"
                    ) from None
                assistant = store_message_in_transaction(
                    conn,
                    conversation_id,
                    request.actor.own_id,
                    "assistant",
                    answer,
                    metadata=assistant_metadata,
                )
                message_id = str(assistant.get("id") or "")
                if not re.fullmatch(r"msg_[0-9a-f]{16}", message_id):
                    raise V12FileReadError("assistant publication has no durable identity")
                try:
                    load_accepted_capability_outcome_receipt(
                        assistant.get("metadata_json"),
                        expected_outcome=outcome,
                    )
                except CapabilityOutcomeError:
                    raise V12FileReadError(
                        "accepted capability outcome receipt was not stored durably"
                    ) from None
                _require_deadline(deadline, stage="before transaction commit")
                _require_prepared_file_turn_bound(prepared)
                _require_v12_turn_context(
                    request,
                    turn,
                    expected=authenticated_context,
                )
                publication = (conversation_id, message_id, interaction_mode, outcome)
        except BaseException:
            raise
        confirm_staged_request_effect()
        return publication

    async def handle(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> ReadOnlyRouteResult:
        prepared = self._prepared_matches(plan, preparation)
        if prepared is None:
            raise V12FileReadError("file preparation authority is invalid")
        authenticated_context = _require_v12_turn_context(
            request,
            turn,
            expected=prepared.authenticated_turn_context,
        )
        handler_started_at = time.monotonic()
        raw_orchestration_started_at = request.orchestration_started_at
        try:
            trace_start_candidate = (
                float(raw_orchestration_started_at)
                if isinstance(raw_orchestration_started_at, (int, float))
                and not isinstance(raw_orchestration_started_at, bool)
                else math.nan
            )
        except (TypeError, OverflowError, ValueError):
            trace_start_candidate = math.nan
        router_trace_scope = bool(
            math.isfinite(trace_start_candidate)
            and 0.0 <= trace_start_candidate <= handler_started_at
            and isinstance(request.planner_model_calls_lower_bound, int)
            and not isinstance(request.planner_model_calls_lower_bound, bool)
            and 0 < request.planner_model_calls_lower_bound <= 1_022
        )
        trace_started_at = trace_start_candidate if router_trace_scope else handler_started_at
        trace_planner_model_calls = request.planner_model_calls_lower_bound if router_trace_scope else 0
        deadline = request.turn_deadline if request.turn_deadline is not None else time.monotonic() + 60.0
        deadline = _within_parent_deadline(deadline, authenticated_context)
        if authenticated_context is not None:
            _require_deadline(deadline, stage="before handler execution")
        synthesis_max_tokens, verifier_max_tokens = _two_call_read_model_output_limits(
            authenticated_context,
            synthesis_max_tokens=_SYNTHESIS_MAX_TOKENS,
            verifier_max_tokens=256,
        )
        _require_prepared_file_turn_bound(prepared)
        _require_v12_turn_context(
            request,
            turn,
            expected=authenticated_context,
        )
        answer = await self._synthesize(
            turn,
            plan,
            prepared.evidence.bundle,
            prepared.model_lease,
            prepared.model_requirements,
            deadline=deadline,
            max_tokens=synthesis_max_tokens,
        )
        _require_prepared_file_turn_bound(prepared)
        _require_v12_turn_context(
            request,
            turn,
            expected=authenticated_context,
        )
        await self._verify(
            turn,
            prepared.evidence.bundle,
            answer,
            prepared.model_lease,
            prepared.model_requirements,
            deadline=deadline,
            max_tokens=verifier_max_tokens,
        )
        _require_prepared_file_turn_bound(prepared)
        _require_v12_turn_context(
            request,
            turn,
            expected=authenticated_context,
        )
        if not await _lease_is_current_before_deadline(
            self._model,
            prepared.model_lease,
            prepared.model_requirements,
            absolute_deadline=deadline,
        ):
            raise V12FileReadError("file model authority changed before publication")
        _require_deadline(deadline, stage="after final remote lease recheck")
        _require_prepared_file_turn_bound(prepared)
        _require_v12_turn_context(
            request,
            turn,
            expected=authenticated_context,
        )
        # Remote epoch freshness is sampled immediately before publication;
        # the synchronous transaction then rechecks the same lease against the
        # process gate after source/conversation reauthorization. It never
        # yields while owning SQLite state or after crossing the effect fence.
        conversation_id, message_id, interaction_mode, outcome = self._publish_sync(
            request,
            turn,
            plan,
            prepared,
            answer,
            deadline=deadline,
            trace_started_at=trace_started_at,
            trace_planner_model_calls=trace_planner_model_calls,
        )
        _require_prepared_file_turn_bound(prepared)
        return ReadOnlyRouteResult(
            message=answer,
            conversation_id=conversation_id,
            message_id=message_id,
            evidence_identity_sha256=prepared.evidence.identity_sha256,
            citation_labels=prepared.evidence.bundle.citation_labels,
            verified=True,
            outcome=outcome,
            message_format="markdown",
            interaction_mode=interaction_mode,
        )


__all__ = [
    "V12FileReadError",
    "V12FileReadHandler",
    "V12_FILE_SYNTHESIS_SYSTEM",
    "V12_FILE_VERIFIER_SCHEMA",
    "V12_FILE_VERIFIER_SYSTEM",
]
