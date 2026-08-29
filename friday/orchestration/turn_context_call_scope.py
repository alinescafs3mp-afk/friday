"""Exact raw-call validation for an authenticated turn.

The ingress-issued :class:`AuthenticatedTurnContext` is authoritative.  This
module proves that compatibility arguments still describe that same call; it
never derives a replacement model input or policy from them.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, cast

from friday.file_evidence import current_turn_file_reference_for_tenant
from friday.orchestration.contracts import AttachmentDescriptor, RouterMode, TurnInput
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    AuthorizedSourceIdentity,
    AuthorizedSourceKind,
    IngressKind,
    TurnContextError,
    TurnMode,
)
from friday.orchestration.turn_context_runtime import (
    bind_or_get_authenticated_chat_call_scope,
    current_authenticated_chat_call_scope,
    current_primary_authenticated_turn_context,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.turn_intent_policy import TurnPolicyDecision

UNSPECIFIED_CHAT_ADJUNCT = object()
_MAX_PROJECTION_NODES = 200_000
_MAX_PROJECTION_DEPTH = 64
_CALL_SCOPE_BINDING_KEY = secrets.token_bytes(32)
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


class _AuthenticatedCallAdjunctSeal:
    """Late-bind exact service adjuncts without trusting an outer omission."""

    __slots__ = ("_hybrid", "_ingestion", "_ingestion_sha256", "_kg", "_lock")

    def __init__(self) -> None:
        self._kg: object = UNSPECIFIED_CHAT_ADJUNCT
        self._hybrid: object = UNSPECIFIED_CHAT_ADJUNCT
        self._ingestion: object = UNSPECIFIED_CHAT_ADJUNCT
        self._ingestion_sha256 = ""
        self._lock = Lock()

    def bind_or_validate(
        self,
        *,
        kg: object,
        hybrid_searcher: object,
        ingestion_result: object,
    ) -> None:
        with self._lock:
            if self._ingestion is not UNSPECIFIED_CHAT_ADJUNCT:
                current_sha256 = _process_local_projection_sha256(
                    self._ingestion,
                    label="ingestion result",
                )
                if current_sha256 != self._ingestion_sha256:
                    raise TurnContextError("authenticated turn chat call scope drifted")
            if kg is not UNSPECIFIED_CHAT_ADJUNCT:
                if self._kg is UNSPECIFIED_CHAT_ADJUNCT:
                    self._kg = kg
                elif self._kg is not kg:
                    raise TurnContextError("authenticated turn chat call scope drifted")
            if hybrid_searcher is not UNSPECIFIED_CHAT_ADJUNCT:
                if self._hybrid is UNSPECIFIED_CHAT_ADJUNCT:
                    self._hybrid = hybrid_searcher
                elif self._hybrid is not hybrid_searcher:
                    raise TurnContextError("authenticated turn chat call scope drifted")
            if ingestion_result is not UNSPECIFIED_CHAT_ADJUNCT:
                if ingestion_result is not None and type(ingestion_result) is not dict:
                    raise TurnContextError("authenticated turn ingestion result is invalid")
                ingestion_sha256 = _process_local_projection_sha256(
                    ingestion_result,
                    label="ingestion result",
                )
                if self._ingestion is UNSPECIFIED_CHAT_ADJUNCT:
                    self._ingestion = ingestion_result
                    self._ingestion_sha256 = ingestion_sha256
                elif self._ingestion is not ingestion_result or self._ingestion_sha256 != ingestion_sha256:
                    raise TurnContextError("authenticated turn chat call scope drifted")

    @property
    def knowledge_graph(self) -> Any:
        with self._lock:
            return None if self._kg is UNSPECIFIED_CHAT_ADJUNCT else self._kg

    @property
    def hybrid_searcher(self) -> Any:
        with self._lock:
            return None if self._hybrid is UNSPECIFIED_CHAT_ADJUNCT else self._hybrid

    @property
    def ingestion_result(self) -> dict[str, Any] | None:
        with self._lock:
            if self._ingestion is UNSPECIFIED_CHAT_ADJUNCT or self._ingestion is None:
                return None
            return cast(dict[str, Any], self._ingestion)

    @property
    def ingestion_result_sha256(self) -> str:
        with self._lock:
            return self._ingestion_sha256

    def exact_forwarding_kwargs(self) -> dict[str, Any]:
        """Return only adjuncts that an exact wrapper actually supplied."""

        with self._lock:
            if self._ingestion is not UNSPECIFIED_CHAT_ADJUNCT:
                current_sha256 = _process_local_projection_sha256(
                    self._ingestion,
                    label="ingestion result",
                )
                if current_sha256 != self._ingestion_sha256:
                    raise TurnContextError("authenticated turn chat call scope drifted")
            result: dict[str, Any] = {}
            if self._kg is not UNSPECIFIED_CHAT_ADJUNCT:
                result["kg"] = self._kg
            if self._hybrid is not UNSPECIFIED_CHAT_ADJUNCT:
                result["hybrid_searcher"] = self._hybrid
            if self._ingestion is not UNSPECIFIED_CHAT_ADJUNCT:
                result["ingestion_result"] = self._ingestion
            return result


@dataclass(frozen=True, slots=True)
class AuthenticatedChatCallScope:
    """Validated process-local projection; never store it past the primary call."""

    model_input: TurnInput
    attachment_carriers: tuple[Mapping[str, Any], ...]
    attachment_carrier_sha256: tuple[str, ...]
    attachment_sources: tuple[AuthorizedSourceIdentity, ...]
    _adjuncts: _AuthenticatedCallAdjunctSeal
    deadline_monotonic: float
    deadline_monotonic_ns: int
    router_mode: RouterMode
    actor_binding_sha256: str
    conversation_binding_sha256: str
    pending_work_bound: bool
    _binding_sha256: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        binding = _chat_call_scope_binding_sha256(self)
        if binding is None:
            raise TurnContextError("authenticated turn chat call scope is invalid")
        object.__setattr__(self, "_binding_sha256", binding)

    @property
    def knowledge_graph(self) -> Any:
        return self._adjuncts.knowledge_graph

    @property
    def hybrid_searcher(self) -> Any:
        return self._adjuncts.hybrid_searcher

    @property
    def ingestion_result(self) -> dict[str, Any] | None:
        return self._adjuncts.ingestion_result

    @property
    def ingestion_result_sha256(self) -> str:
        return self._adjuncts.ingestion_result_sha256

    def exact_service_kwargs(self) -> dict[str, Any]:
        """Project bound service adjuncts without turning omission into ``None``."""

        return self._adjuncts.exact_forwarding_kwargs()


def _chat_call_scope_binding_sha256(value: object) -> str | None:
    """Bind every immutable raw-call field and exact carrier identity."""

    if type(value) is not AuthenticatedChatCallScope:
        return None
    if (
        type(value.model_input) is not TurnInput
        or type(value.attachment_carriers) is not tuple
        or any(not isinstance(item, Mapping) for item in value.attachment_carriers)
        or type(value.attachment_carrier_sha256) is not tuple
        or len(value.attachment_carriers) != len(value.attachment_carrier_sha256)
        or any(
            type(item) is not str or _DIGEST_RE.fullmatch(item) is None
            for item in value.attachment_carrier_sha256
        )
        or type(value.attachment_sources) is not tuple
        or len(value.attachment_carriers) != len(value.attachment_sources)
        or any(type(item) is not AuthorizedSourceIdentity for item in value.attachment_sources)
        or type(value._adjuncts) is not _AuthenticatedCallAdjunctSeal
        or type(value.deadline_monotonic) is not float
        or not math.isfinite(value.deadline_monotonic)
        or type(value.deadline_monotonic_ns) is not int
        or value.deadline_monotonic_ns <= 0
        or type(value.router_mode) is not RouterMode
        or type(value.actor_binding_sha256) is not str
        or _DIGEST_RE.fullmatch(value.actor_binding_sha256) is None
        or type(value.conversation_binding_sha256) is not str
        or _DIGEST_RE.fullmatch(value.conversation_binding_sha256) is None
        or type(value.pending_work_bound) is not bool
    ):
        return None
    parts = (
        str(id(value)),
        str(id(value.model_input)),
        *(str(id(item)) for item in value.attachment_carriers),
        *value.attachment_carrier_sha256,
        *(str(id(item)) for item in value.attachment_sources),
        *(item.identity_sha256 for item in value.attachment_sources),
        str(id(value._adjuncts)),
        value.deadline_monotonic.hex(),
        str(value.deadline_monotonic_ns),
        value.router_mode.value,
        value.actor_binding_sha256,
        value.conversation_binding_sha256,
        "1" if value.pending_work_bound else "0",
    )
    digest = hmac.new(_CALL_SCOPE_BINDING_KEY, b"friday/authenticated-chat-call-scope/v1\0", hashlib.sha256)
    try:
        for part in parts:
            if type(part) is not str:
                return None
            encoded = part.encode("utf-8", errors="strict")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    except (AttributeError, UnicodeEncodeError):
        return None
    return digest.hexdigest()


def _require_chat_call_scope_binding(value: object) -> AuthenticatedChatCallScope:
    if type(value) is not AuthenticatedChatCallScope:
        raise TurnContextError("authenticated turn chat call scope identity is invalid")
    expected = _chat_call_scope_binding_sha256(value)
    if (
        expected is None
        or type(value._binding_sha256) is not str
        or _DIGEST_RE.fullmatch(value._binding_sha256) is None
        or not hmac.compare_digest(value._binding_sha256, expected)
    ):
        raise TurnContextError("authenticated turn chat call scope drifted")
    return value


def _process_local_projection_sha256(value: object, *, label: str) -> str:
    """Hash one exact transient JSON-like shape without retaining its body."""

    digest = hashlib.sha256()
    active: set[int] = set()
    nodes = 0

    def add(raw: bytes) -> None:
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)

    def walk(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_PROJECTION_NODES or depth > _MAX_PROJECTION_DEPTH:
            raise TurnContextError(f"authenticated turn {label} projection is too large")
        if item is None:
            add(b"none")
            return
        if type(item) is bool:
            add(b"bool:true" if item else b"bool:false")
            return
        if type(item) is int:
            add(b"int")
            add(str(item).encode("ascii"))
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise TurnContextError(f"authenticated turn {label} contains a non-finite number")
            add(b"float")
            add(item.hex().encode("ascii"))
            return
        if type(item) is str:
            try:
                encoded = item.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise TurnContextError(f"authenticated turn {label} contains invalid UTF-8 text") from exc
            add(b"str")
            add(encoded)
            return
        if type(item) is bytes:
            add(b"bytes")
            add(item)
            return
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise TurnContextError(f"authenticated turn {label} contains a cycle")
            try:
                keys = tuple(item.keys())
            except Exception as exc:
                raise TurnContextError(f"authenticated turn {label} mapping is not readable") from exc
            if any(type(key) is not str for key in keys):
                raise TurnContextError(f"authenticated turn {label} keys must be strings")
            active.add(identity)
            try:
                add(b"mapping")
                add(str(len(keys)).encode("ascii"))
                for key in sorted(keys):
                    walk(key, depth + 1)
                    try:
                        child = item[key]
                    except Exception as exc:
                        raise TurnContextError(
                            f"authenticated turn {label} mapping changed while projected"
                        ) from exc
                    walk(child, depth + 1)
                if tuple(item.keys()) != keys:
                    raise TurnContextError(f"authenticated turn {label} mapping changed while projected")
            finally:
                active.remove(identity)
            return
        if type(item) in {list, tuple}:
            sequence = cast(list[object] | tuple[object, ...], item)
            identity = id(item)
            if identity in active:
                raise TurnContextError(f"authenticated turn {label} contains a cycle")
            active.add(identity)
            try:
                add(b"list" if type(item) is list else b"tuple")
                add(str(len(sequence)).encode("ascii"))
                for child in sequence:
                    walk(child, depth + 1)
            finally:
                active.remove(identity)
            return
        # Process-owned carrier markers and attestation objects are not model
        # bodies.  Bind their exact in-process identity while all strings,
        # bytes, mappings, and sequences (the model-visible surface) remain
        # content-hashed above.
        add(b"opaque-process-object")
        add(f"{type(item).__module__}.{type(item).__qualname__}".encode())
        add(str(id(item)).encode("ascii"))

    walk(value, 0)
    return digest.hexdigest()


def _normalized_text(value: str | None) -> str:
    return str(value or "").strip().encode("utf-8", errors="replace").decode("utf-8")


def _attachment_scope(
    context: AuthenticatedTurnContext,
    attachments: list[dict[str, Any]] | None,
) -> tuple[
    tuple[Mapping[str, Any], ...],
    tuple[str, ...],
    tuple[AuthorizedSourceIdentity, ...],
]:
    if attachments is None:
        carriers: tuple[Mapping[str, Any], ...] = ()
    elif type(attachments) is list and all(isinstance(item, Mapping) for item in attachments):
        carriers = tuple(attachments)
    else:
        raise TurnContextError("authenticated turn attachment carriers are invalid")

    turn = context.model_input
    if turn.attachments_truncated or len(carriers) != len(turn.attachments):
        raise TurnContextError("authenticated turn attachment cardinality drifted")
    sources_by_ordinal = {
        source.ordinal: source
        for source in context.authorized_sources
        if source.kind is not AuthorizedSourceKind.ACCEPTED_INGRESS
    }
    if set(sources_by_ordinal) != set(range(1, len(turn.attachments) + 1)):
        raise TurnContextError("authenticated turn attachment source set drifted")

    ordered_sources: list[AuthorizedSourceIdentity] = []
    carrier_sha256: list[str] = []
    for ordinal, (carrier, descriptor) in enumerate(
        zip(carriers, turn.attachments, strict=True),
        start=1,
    ):
        try:
            projected = AttachmentDescriptor.from_raw(carrier, ordinal=ordinal)
        except Exception as exc:
            raise TurnContextError("authenticated turn attachment descriptor is invalid") from exc
        source = sources_by_ordinal[ordinal]
        if projected != descriptor or source.model_descriptor is not descriptor:
            raise TurnContextError("authenticated turn attachment descriptor drifted")
        # Chat attachments are current-ingress carriers.  Registered archive or
        # reply sources need a future typed call carrier and are closed here.
        if (
            source.kind is not AuthorizedSourceKind.CURRENT_ATTACHMENT
            or current_turn_file_reference_for_tenant(
                carrier,
                tenant_id=context.authority.tenant_id,
            )
            is not source.private_carrier
        ):
            raise TurnContextError("authenticated turn attachment carrier drifted")
        carrier_sha256.append(_process_local_projection_sha256(carrier, label=f"attachment {ordinal}"))
        ordered_sources.append(source)
    return carriers, tuple(carrier_sha256), tuple(ordered_sources)


def require_authenticated_chat_call_scope(
    context: AuthenticatedTurnContext,
    *,
    user_id: str,
    message: str,
    actor: ActorContext,
    conversation_id: str | None,
    attachments: list[dict[str, Any]] | None,
    enable_tools: bool,
    synthetic_document_notice: bool,
    replay_source_message_id: str | None,
    mode: str | None,
    answer_with_voice: bool,
    reply_to: str | None,
    quoted_attachment_reference: bool,
    reply_assistant_reference: bool,
    reply_assistant_message_id: str | None,
    turn_policy: TurnPolicyDecision | None,
    telegram_update_id: str | None,
    turn_deadline: float | None,
    pending_durable_admission: PendingDurableTurnAdmission | None,
    kg: Any = UNSPECIFIED_CHAT_ADJUNCT,
    hybrid_searcher: Any = UNSPECIFIED_CHAT_ADJUNCT,
    ingestion_result: dict[str, Any] | None | object = UNSPECIFIED_CHAT_ADJUNCT,
    runtime_router_mode: RouterMode | None = None,
) -> AuthenticatedChatCallScope:
    """Require every authority-relevant compatibility argument to be exact."""

    if type(context) is not AuthenticatedTurnContext:
        raise TurnContextError("authenticated chat call has an invalid context")
    turn = context.model_input
    authority = context.authority
    authority_conversation_id = authority.conversation_id
    if (
        authority.actor is not actor
        or type(user_id) is not str
        or authority.tenant_id != user_id
        or type(authority_conversation_id) is not str
        or _CONVERSATION_ID_RE.fullmatch(authority_conversation_id) is None
        or type(conversation_id) is not str
        or _CONVERSATION_ID_RE.fullmatch(conversation_id) is None
        or not hmac.compare_digest(authority_conversation_id, conversation_id)
    ):
        raise TurnContextError("authenticated turn actor or conversation scope drifted")

    carriers, carrier_sha256, attachment_sources = _attachment_scope(context, attachments)
    if type(message) is not str or turn.message_truncated:
        raise TurnContextError("authenticated turn message is not exact")
    expected_message = (
        ("Загружены документы." if len(carriers) > 1 else "Загружен документ.")
        if synthetic_document_notice and carriers
        else _normalized_text(message)
    )
    if turn.message != expected_message:
        raise TurnContextError("authenticated turn message drifted")

    if type(enable_tools) is not bool or turn.enable_tools is not enable_tools:
        raise TurnContextError("authenticated turn tool authority drifted")
    if (
        type(synthetic_document_notice) is not bool
        or turn.synthetic_document_notice is not synthetic_document_notice
        or type(quoted_attachment_reference) is not bool
        or turn.quoted_attachment_reference is not quoted_attachment_reference
        or type(reply_assistant_reference) is not bool
        or turn.reply_assistant_reference is not reply_assistant_reference
    ):
        raise TurnContextError("authenticated turn surface flags drifted")
    if reply_to is not None or turn.reply_quote_truncated or turn.reply_quote:
        raise TurnContextError("authenticated turn reply scope drifted")
    if replay_source_message_id is not None or reply_assistant_message_id is not None:
        raise TurnContextError("authenticated turn carries an unbound replay or reply identity")
    if type(answer_with_voice) is not bool or answer_with_voice:
        raise TurnContextError("authenticated turn carries unbound voice delivery")

    if mode is not None:
        raise TurnContextError("authenticated turn carries an explicit mode override")
    raw_mode = TurnMode.DIALOGUE.value
    if len(raw_mode) > 40 or raw_mode != turn.conversation_mode:
        raise TurnContextError("authenticated turn interaction mode drifted")
    expected_policy = context.turn_policy.decision if context.turn_policy.decision.handled else None
    if turn_policy is not expected_policy:
        raise TurnContextError("authenticated turn policy carrier drifted")
    expected_pending = (
        context.pending_work_admission.admission if context.pending_work_admission is not None else None
    )
    if pending_durable_admission is not expected_pending:
        raise TurnContextError("authenticated turn pending-work carrier drifted")

    if authority.ingress_kind is IngressKind.TELEGRAM:
        if type(telegram_update_id) is not str or telegram_update_id != authority.update_id:
            raise TurnContextError("authenticated turn Telegram update identity drifted")
    elif telegram_update_id is not None:
        raise TurnContextError("signed HTTP turn carries a Telegram update identity")

    if (
        not isinstance(turn_deadline, (int, float))
        or isinstance(turn_deadline, bool)
        or not math.isfinite(float(turn_deadline))
        or int(float(turn_deadline) * 1_000_000_000) != context.inherited_budget.safety_deadline.monotonic_ns
    ):
        raise TurnContextError("authenticated turn deadline drifted")
    sealed_router_mode = context.turn_policy.router_mode
    if (
        runtime_router_mode is not None
        and sealed_router_mode is not RouterMode.LEGACY
        and runtime_router_mode is not sealed_router_mode
    ):
        raise TurnContextError("authenticated turn router mode drifted")

    existing = current_authenticated_chat_call_scope(context)
    if existing is not None:
        _require_chat_call_scope_binding(existing)
    candidate = AuthenticatedChatCallScope(
        model_input=turn,
        attachment_carriers=carriers,
        attachment_carrier_sha256=carrier_sha256,
        attachment_sources=attachment_sources,
        _adjuncts=_AuthenticatedCallAdjunctSeal(),
        deadline_monotonic=float(turn_deadline),
        deadline_monotonic_ns=context.inherited_budget.safety_deadline.monotonic_ns,
        router_mode=sealed_router_mode,
        actor_binding_sha256=authority.actor_binding_sha256,
        conversation_binding_sha256=authority.conversation.binding_sha256,
        pending_work_bound=context.pending_work_admission is not None,
    )
    sealed = bind_or_get_authenticated_chat_call_scope(context, candidate)
    sealed = _require_chat_call_scope_binding(sealed)
    if (
        sealed.model_input is not candidate.model_input
        or len(sealed.attachment_carriers) != len(candidate.attachment_carriers)
        or any(
            left is not right
            for left, right in zip(
                sealed.attachment_carriers,
                candidate.attachment_carriers,
                strict=True,
            )
        )
        or sealed.attachment_carrier_sha256 != candidate.attachment_carrier_sha256
        or len(sealed.attachment_sources) != len(candidate.attachment_sources)
        or any(
            left is not right
            for left, right in zip(
                sealed.attachment_sources,
                candidate.attachment_sources,
                strict=True,
            )
        )
        or sealed.deadline_monotonic != candidate.deadline_monotonic
        or sealed.deadline_monotonic_ns != candidate.deadline_monotonic_ns
        or sealed.router_mode is not candidate.router_mode
        or sealed.actor_binding_sha256 != candidate.actor_binding_sha256
        or sealed.conversation_binding_sha256 != candidate.conversation_binding_sha256
        or sealed.pending_work_bound is not candidate.pending_work_bound
    ):
        raise TurnContextError("authenticated turn chat call scope drifted")
    sealed._adjuncts.bind_or_validate(  # noqa: SLF001 - same-module private seal
        kg=kg,
        hybrid_searcher=hybrid_searcher,
        ingestion_result=ingestion_result,
    )
    _require_chat_call_scope_binding(sealed)
    return sealed


def require_current_authenticated_chat_call_scope(
    context: AuthenticatedTurnContext,
) -> AuthenticatedChatCallScope:
    """Revalidate one already-sealed raw call before or after an awaited seam."""

    admitted = current_primary_authenticated_turn_context(context)
    if admitted is not context:
        raise TurnContextError("authenticated turn chat call scope has no primary authority")
    scope = current_authenticated_chat_call_scope(context)
    if scope is None:
        raise TurnContextError("authenticated turn chat call scope is unavailable")
    scope = _require_chat_call_scope_binding(scope)
    attachment_list = cast(list[dict[str, Any]], list(scope.attachment_carriers))
    carriers, carrier_sha256, sources = _attachment_scope(context, attachment_list)
    if (
        scope.model_input is not context.model_input
        or len(carriers) != len(scope.attachment_carriers)
        or any(
            current is not sealed for current, sealed in zip(carriers, scope.attachment_carriers, strict=True)
        )
        or carrier_sha256 != scope.attachment_carrier_sha256
        or len(sources) != len(scope.attachment_sources)
        or any(
            current is not sealed for current, sealed in zip(sources, scope.attachment_sources, strict=True)
        )
        or scope.deadline_monotonic_ns != context.inherited_budget.safety_deadline.monotonic_ns
        or int(scope.deadline_monotonic * 1_000_000_000) != scope.deadline_monotonic_ns
        or scope.router_mode is not context.turn_policy.router_mode
        or scope.actor_binding_sha256 != context.authority.actor_binding_sha256
        or scope.conversation_binding_sha256 != context.authority.conversation.binding_sha256
        or scope.pending_work_bound is not (context.pending_work_admission is not None)
    ):
        raise TurnContextError("authenticated turn chat call scope drifted")
    scope.exact_service_kwargs()
    _require_chat_call_scope_binding(scope)
    if current_primary_authenticated_turn_context(context) is not context:
        raise TurnContextError("authenticated turn chat call scope has no primary authority")
    return scope


__all__ = [
    "AuthenticatedChatCallScope",
    "UNSPECIFIED_CHAT_ADJUNCT",
    "require_authenticated_chat_call_scope",
    "require_current_authenticated_chat_call_scope",
]
