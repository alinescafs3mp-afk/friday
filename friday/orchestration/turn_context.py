"""One authenticated authority spine for an admitted user turn.

Phase-B integration map
-----------------------
* The durable ingress boundary supplies its stable opaque token, the existing
  audit-privacy namespace key, the exact authenticated actor, the already
  selected conversation target/mode, and the real request-effect binding.
* It constructs ``TurnInput`` exactly once through ``TurnInput.from_chat()``,
  binds the existing ``TurnPolicyDecision``, exact process-owned source tokens,
  pending admission, deadline and limits, then creates one context.
* Router, legacy/V12, AgentContext, tools, Engineer, advisory models and final
  publication carry that same object.  A fallback reads its policy; it does not
  rebuild the object or reclassify the raw message.
* A newly targeted conversation remains a typed ``NEW`` admission until Phase B
  provides one code-owned resolution adapter.  This module neither creates a
  conversation nor simulates persistence.

The canonical context is deliberately body-free.  Exact model and policy
content is bound with deployment-local keyed digests; raw text, source bodies,
IDs, paths and public-response bodies never enter canonical JSON.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum, StrEnum
from typing import Any

from friday.file_evidence import CurrentTurnFileReferenceToken, current_turn_file_reference_of
from friday.model_input_hygiene import secondary_model_messages_are_secret_free
from friday.orchestration.contracts import AttachmentDescriptor, RouterMode, TurnInput
from friday.orchestration.supervisor_actor_binding import supervisor_canary_actor_binding_sha256
from friday.pending_durable_turn import PendingDurableAdmissionState, PendingDurableTurnAdmission
from friday.permissions import ActorContext
from friday.source_identity import (
    AuthorizedFileSnapshotToken,
    authorized_file_snapshot_token_is_process_owned,
)
from friday.turn_intent_policy import (
    AttachmentDisposition,
    CapabilityProjection,
    ImageGenerationProjection,
    IntegrationProjection,
    LocationSource,
    TurnIntent,
    TurnPolicyDecision,
    WeatherHorizon,
    WebDisposition,
)

AUTHENTICATED_TURN_CONTEXT_SCHEMA = "friday.authenticated-turn-context.v1"
AUTHENTICATED_INGRESS_AUTHORITY_SCHEMA = "friday.authenticated-ingress-authority.v1"
CONVERSATION_ADMISSION_SCHEMA = "friday.conversation-admission.v1"
TURN_IDENTITY_SCHEMA = "friday.turn-identity.v1"
AUTHORIZED_SOURCE_IDENTITY_SCHEMA = "friday.authorized-source-identity.v1"
TURN_POLICY_SCHEMA = "friday.authenticated-turn-policy.v1"
INHERITED_TURN_BUDGET_SCHEMA = "friday.inherited-turn-budget.v1"
PENDING_WORK_BINDING_SCHEMA = "friday.pending-work-admission-binding.v1"
EFFECT_FENCE_SCHEMA = "friday.effect-fence.v1"
ADVISORY_TURN_PROJECTION_SCHEMA = "friday.advisory-turn-projection.v1"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TURN_ID_RE = re.compile(r"turn_[0-9a-f]{64}\Z")
_CONVERSATION_ID_RE = re.compile(r"conv_[0-9a-f]{16}\Z")
_MAX_OPAQUE_ID_BYTES = 512
_MAX_CANONICAL_CONTEXT_BYTES = 32_768
_MAX_PRIVATE_BINDING_BYTES = 131_072
_MAX_ADVISORY_UTF8_BYTES = 5_500
_MAX_AUTHORIZED_SOURCES = 17
_MAX_TURN_HORIZON_NS = 3_600_000_000_000
_MAX_MONOTONIC_NS = (1 << 63) - 1
_MODEL_MEDIA_TYPES = frozenset({"archive", "audio", "binary", "image", "office", "pdf", "text", "video"})
_SEAL_MARKER = object()


class TurnContextError(ValueError):
    """The turn context is malformed, untrusted, stale or relationally invalid."""


class IngressKind(StrEnum):
    TELEGRAM = "telegram"
    SIGNED_HTTP = "signed_http"


class ConversationScopeKind(StrEnum):
    NEW = "new"
    EXISTING = "existing"


class TurnMode(StrEnum):
    DIALOGUE = "dialogue"
    KNOWLEDGE_WORK = "knowledge_work"
    RESEARCH = "research"
    ENGINEER = "engineer"


class AuthorizedSourceKind(StrEnum):
    ACCEPTED_INGRESS = "accepted_ingress"
    CURRENT_ATTACHMENT = "current_attachment"
    REGISTERED_FILE = "registered_file"


class PendingOwnerKind(StrEnum):
    UNCERTAIN_FAIL_CLOSED = "uncertain_fail_closed"
    LEGACY_PENDING_RUNTIME = "legacy_pending_runtime"
    WORK_ITEM = "work_item"
    WORK_GRAPH = "work_graph"


class EffectOwner(StrEnum):
    PRIMARY = "primary"


class FinalPublisher(StrEnum):
    PRIMARY = "primary"


def _canonical_bytes(value: object, *, maximum: int | None = None) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TurnContextError("turn context value is not canonical JSON") from exc
    if maximum is not None and len(encoded) > maximum:
        raise TurnContextError("turn context value exceeds its closed byte limit")
    return encoded


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _keyed_binding(namespace_key: bytes, domain: bytes, value: object) -> str:
    encoded = _canonical_bytes(value, maximum=_MAX_PRIVATE_BINDING_BYTES)
    return hmac.new(namespace_key, domain + encoded, hashlib.sha256).hexdigest()


def _digest(value: object, *, label: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise TurnContextError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _bounded_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise TurnContextError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def _opaque_id(value: object, *, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise TurnContextError(f"{label} is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise TurnContextError(f"{label} is invalid") from exc
    if len(encoded) > _MAX_OPAQUE_ID_BYTES or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise TurnContextError(f"{label} is invalid")
    return value


def _optional_text(value: object, *, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    if type(value) is not str or len(value) > maximum:
        raise TurnContextError(f"{label} is invalid")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise TurnContextError(f"{label} is invalid") from exc
    return value


def _reject_json_constant(_value: str) -> Any:
    raise TurnContextError("canonical turn context JSON is invalid")


def _reject_json_float(_value: str) -> Any:
    raise TurnContextError("canonical turn context JSON is invalid")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TurnContextError("canonical turn context JSON is invalid")
        result[key] = value
    return result


def _visible_strings(value: object) -> tuple[str, ...]:
    if type(value) is str:
        return (value,)
    if type(value) is list:
        return tuple(text for item in value for text in _visible_strings(item))
    if type(value) is dict:
        return tuple(text for item in value.values() for text in _visible_strings(item))
    return ()


@dataclass(frozen=True, slots=True, repr=False)
class _Seal:
    kind: str
    namespace_fingerprint: str
    payload_sha256: str
    component_ids: tuple[int, ...] = ()
    private_binding_sha256: str | None = None
    marker: object = field(default=_SEAL_MARKER, init=False, compare=False)


def _validate_seal(
    seal: object,
    *,
    kind: str,
    payload: object,
    component_ids: tuple[int, ...] = (),
) -> _Seal:
    if (
        type(seal) is not _Seal
        or seal.marker is not _SEAL_MARKER
        or seal.kind != kind
        or seal.payload_sha256 != _sha256(payload)
        or seal.component_ids != component_ids
        or _DIGEST_RE.fullmatch(seal.namespace_fingerprint) is None
    ):
        raise TurnContextError(f"{kind} was not issued by the authenticated seam")
    if seal.private_binding_sha256 is not None:
        _digest(seal.private_binding_sha256, label=f"{kind} private binding")
    return seal


@dataclass(frozen=True, slots=True, repr=False)
class ConversationAdmission:
    kind: ConversationScopeKind
    conversation_id: str | None = field(repr=False)
    binding_sha256: str
    _seal: _Seal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.kind) is not ConversationScopeKind:
            raise TurnContextError("conversation admission kind must be closed")
        if self.kind is ConversationScopeKind.NEW:
            if self.conversation_id is not None:
                raise TurnContextError("new conversation admission cannot carry a conversation id")
        elif (
            type(self.conversation_id) is not str
            or _CONVERSATION_ID_RE.fullmatch(self.conversation_id) is None
        ):
            raise TurnContextError("existing conversation admission requires a code-owned id")
        _digest(self.binding_sha256, label="conversation admission binding")
        _validate_seal(
            self._seal,
            kind="conversation admission",
            payload=self.payload(),
            component_ids=(id(self.conversation_id),),
        )

    def payload(self) -> dict[str, str]:
        return {
            "schema": CONVERSATION_ADMISSION_SCHEMA,
            "kind": self.kind.value,
            "binding_sha256": self.binding_sha256,
        }


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedIngressAuthority:
    """Exact private actor/ingress state plus only opaque public bindings."""

    ingress_kind: IngressKind
    interaction_mode: TurnMode
    actor: ActorContext = field(repr=False)
    conversation: ConversationAdmission = field(repr=False)
    ingress_issued_token: str = field(repr=False)
    source_id: str = field(repr=False)
    update_id: str = field(repr=False)
    request_effect_binding_sha256: str | None
    issuer_fingerprint_sha256: str
    accepted_ingress_binding_sha256: str
    actor_binding_sha256: str
    tenant_binding_sha256: str
    person_binding_sha256: str
    source_binding_sha256: str
    update_binding_sha256: str
    _seal: _Seal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.ingress_kind) is not IngressKind or type(self.interaction_mode) is not TurnMode:
            raise TurnContextError("ingress kind or turn mode must be closed")
        if type(self.actor) is not ActorContext or type(self.conversation) is not ConversationAdmission:
            raise TurnContextError("authenticated actor or conversation admission is invalid")
        _opaque_id(self.ingress_issued_token, label="ingress-issued token")
        _opaque_id(self.source_id, label="ingress source identity")
        _opaque_id(self.update_id, label="ingress update identity")
        for label, value in (
            ("request effect binding", self.request_effect_binding_sha256),
            ("issuer fingerprint", self.issuer_fingerprint_sha256),
            ("accepted ingress binding", self.accepted_ingress_binding_sha256),
            ("actor binding", self.actor_binding_sha256),
            ("tenant binding", self.tenant_binding_sha256),
            ("person binding", self.person_binding_sha256),
            ("source binding", self.source_binding_sha256),
            ("update binding", self.update_binding_sha256),
        ):
            _digest(value, label=label, optional=label == "request effect binding")
        _validate_seal(
            self._seal,
            kind="ingress authority",
            payload=self.payload(),
            component_ids=(id(self.actor), id(self.conversation)),
        )

    @property
    def tenant_id(self) -> str:
        return self.actor.user_id

    @property
    def person_id(self) -> str:
        return self.actor.own_id

    @property
    def conversation_id(self) -> str | None:
        return self.conversation.conversation_id

    def payload(self) -> dict[str, object]:
        return {
            "schema": AUTHENTICATED_INGRESS_AUTHORITY_SCHEMA,
            "ingress_kind": self.ingress_kind.value,
            "interaction_mode": self.interaction_mode.value,
            "conversation": self.conversation.payload(),
            "request_effect_binding_sha256": self.request_effect_binding_sha256,
            "issuer_fingerprint_sha256": self.issuer_fingerprint_sha256,
            "accepted_ingress_binding_sha256": self.accepted_ingress_binding_sha256,
            "actor_binding_sha256": self.actor_binding_sha256,
            "tenant_binding_sha256": self.tenant_binding_sha256,
            "person_binding_sha256": self.person_binding_sha256,
            "source_binding_sha256": self.source_binding_sha256,
            "update_binding_sha256": self.update_binding_sha256,
        }

    def canonical_sha256(self) -> str:
        return _sha256(self.payload())


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    turn_id: str
    authority_sha256: str

    def __post_init__(self) -> None:
        if type(self.turn_id) is not str or _TURN_ID_RE.fullmatch(self.turn_id) is None:
            raise TurnContextError("turn_id is invalid")
        _digest(self.authority_sha256, label="turn authority")

    @classmethod
    def from_authority(cls, authority: AuthenticatedIngressAuthority) -> TurnIdentity:
        if type(authority) is not AuthenticatedIngressAuthority:
            raise TurnContextError("ingress authority is invalid")
        authority_sha256 = authority.canonical_sha256()
        turn_digest = _sha256({"schema": TURN_IDENTITY_SCHEMA, "authority_sha256": authority_sha256})
        return cls(f"turn_{turn_digest}", authority_sha256)

    def payload(self) -> dict[str, str]:
        return {
            "schema": TURN_IDENTITY_SCHEMA,
            "turn_id": self.turn_id,
            "authority_sha256": self.authority_sha256,
        }


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizedSourceIdentity:
    """Turn-scoped identity retaining one exact process-owned source carrier."""

    kind: AuthorizedSourceKind
    ordinal: int | None
    turn_authority_sha256: str
    identity_sha256: str
    private_carrier: object = field(repr=False, compare=False)
    _seal: _Seal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.kind) is not AuthorizedSourceKind:
            raise TurnContextError("authorized source kind must be closed")
        if self.kind is AuthorizedSourceKind.ACCEPTED_INGRESS:
            if self.ordinal is not None:
                raise TurnContextError("accepted ingress source cannot carry an attachment ordinal")
        elif type(self.ordinal) is not int or not 1 <= self.ordinal <= 16:
            raise TurnContextError("attachment source ordinal is invalid")
        _digest(self.turn_authority_sha256, label="source turn authority")
        _digest(self.identity_sha256, label="authorized source identity")
        _validate_seal(
            self._seal,
            kind="authorized source",
            payload=self.payload(),
            component_ids=(id(self.private_carrier),),
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": AUTHORIZED_SOURCE_IDENTITY_SCHEMA,
            "kind": self.kind.value,
            "ordinal": self.ordinal,
            "turn_authority_sha256": self.turn_authority_sha256,
            "identity_sha256": self.identity_sha256,
        }


def _policy_value(value: object) -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {item.name: _policy_value(getattr(value, item.name)) for item in fields(value)}
    if type(value) is tuple:
        return [_policy_value(item) for item in value]
    raise TurnContextError("turn policy decision contains an invalid value")


def _turn_policy_decision_payload(decision: TurnPolicyDecision) -> dict[str, object]:
    if type(decision) is not TurnPolicyDecision:
        raise TurnContextError("turn policy must carry the exact TurnPolicyDecision")
    if (
        type(decision.intent) is not TurnIntent
        or type(decision.web) is not WebDisposition
        or type(decision.attachments) is not AttachmentDisposition
        or (decision.location_source is not None and type(decision.location_source) is not LocationSource)
        or (decision.weather_horizon is not None and type(decision.weather_horizon) is not WeatherHorizon)
        or (
            decision.capability_projection is not None
            and type(decision.capability_projection) is not CapabilityProjection
        )
        or (
            decision.image_generation_projection is not None
            and type(decision.image_generation_projection) is not ImageGenerationProjection
        )
        or (
            decision.integration_projection is not None
            and type(decision.integration_projection) is not IntegrationProjection
        )
        or type(decision.local_diagnostics_allowed) is not bool
    ):
        raise TurnContextError("turn policy decision is not closed")
    _optional_text(decision.location, label="turn policy location", maximum=256)
    _optional_text(decision.public_response, label="turn policy public response", maximum=4_096)
    _optional_text(decision.required_capability, label="turn policy capability", maximum=128)
    payload = {item.name: _policy_value(getattr(decision, item.name)) for item in fields(decision)}
    _canonical_bytes(payload, maximum=16_384)
    return payload


@dataclass(frozen=True, slots=True, repr=False)
class TurnPolicy:
    """Existing code-owned decision plus the one allowed strategy/fallback."""

    router_mode: RouterMode
    fallback_router_mode: RouterMode | None
    decision: TurnPolicyDecision = field(repr=False)
    decision_binding_sha256: str
    _seal: _Seal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.router_mode) is not RouterMode:
            raise TurnContextError("turn router mode must be closed")
        if self.router_mode is RouterMode.LEGACY:
            if self.fallback_router_mode is not None:
                raise TurnContextError("legacy strategy cannot install another fallback")
        elif self.fallback_router_mode is not RouterMode.LEGACY:
            raise TurnContextError("non-legacy strategy must fail only to legacy")
        _turn_policy_decision_payload(self.decision)
        _digest(self.decision_binding_sha256, label="turn policy decision binding")
        _validate_seal(
            self._seal,
            kind="turn policy",
            payload=self.payload(),
            component_ids=(id(self.decision),),
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": TURN_POLICY_SCHEMA,
            "router_mode": self.router_mode.value,
            "fallback_router_mode": (
                self.fallback_router_mode.value if self.fallback_router_mode is not None else None
            ),
            "decision_binding_sha256": self.decision_binding_sha256,
            "raw_message_reclassification": False,
        }


@dataclass(frozen=True, slots=True)
class TurnSafetyDeadline:
    monotonic_ns: int

    def __post_init__(self) -> None:
        _bounded_int(
            self.monotonic_ns,
            label="turn safety deadline",
            minimum=1,
            maximum=_MAX_MONOTONIC_NS,
        )

    def child(self, requested_monotonic_ns: int) -> TurnSafetyDeadline:
        requested = _bounded_int(
            requested_monotonic_ns,
            label="child safety deadline",
            minimum=1,
            maximum=_MAX_MONOTONIC_NS,
        )
        return TurnSafetyDeadline(min(self.monotonic_ns, requested))


@dataclass(frozen=True, slots=True)
class ModelAntiLoopBudget:
    max_model_calls: int
    max_model_retries: int

    def __post_init__(self) -> None:
        calls = _bounded_int(self.max_model_calls, label="model call limit", minimum=1, maximum=64)
        retries = _bounded_int(self.max_model_retries, label="model retry limit", minimum=0, maximum=16)
        if retries >= calls:
            raise TurnContextError("model retry limit must be smaller than the model call limit")


@dataclass(frozen=True, slots=True)
class TurnResourceBudget:
    max_tool_calls: int
    max_advisory_calls: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        _bounded_int(self.max_tool_calls, label="tool call limit", minimum=0, maximum=64)
        _bounded_int(self.max_advisory_calls, label="advisory call limit", minimum=0, maximum=16)
        _bounded_int(self.max_output_tokens, label="output token limit", minimum=1, maximum=1_000_000)


@dataclass(frozen=True, slots=True)
class InheritedTurnBudget:
    """Immutable ceilings; Phase B shares one consumption ledger beneath them."""

    safety_deadline: TurnSafetyDeadline
    model_anti_loop: ModelAntiLoopBudget
    resources: TurnResourceBudget

    def __post_init__(self) -> None:
        if type(self.safety_deadline) is not TurnSafetyDeadline:
            raise TurnContextError("turn safety deadline has an invalid type")
        if type(self.model_anti_loop) is not ModelAntiLoopBudget:
            raise TurnContextError("model anti-loop budget has an invalid type")
        if type(self.resources) is not TurnResourceBudget:
            raise TurnContextError("turn resource budget has an invalid type")

    def derive_child(
        self,
        *,
        safety_deadline_monotonic_ns: int,
        max_model_calls: int,
        max_model_retries: int,
        max_tool_calls: int,
        max_advisory_calls: int,
        max_output_tokens: int,
    ) -> InheritedTurnBudget:
        deadline = self.safety_deadline.child(safety_deadline_monotonic_ns)
        calls = min(
            self.model_anti_loop.max_model_calls,
            _bounded_int(max_model_calls, label="child model call limit", minimum=1, maximum=64),
        )
        retries = min(
            self.model_anti_loop.max_model_retries,
            _bounded_int(max_model_retries, label="child model retry limit", minimum=0, maximum=16),
            calls - 1,
        )
        return InheritedTurnBudget(
            deadline,
            ModelAntiLoopBudget(calls, retries),
            TurnResourceBudget(
                min(
                    self.resources.max_tool_calls,
                    _bounded_int(max_tool_calls, label="child tool call limit", minimum=0, maximum=64),
                ),
                min(
                    self.resources.max_advisory_calls,
                    _bounded_int(
                        max_advisory_calls,
                        label="child advisory call limit",
                        minimum=0,
                        maximum=16,
                    ),
                ),
                min(
                    self.resources.max_output_tokens,
                    _bounded_int(
                        max_output_tokens,
                        label="child output token limit",
                        minimum=1,
                        maximum=1_000_000,
                    ),
                ),
            ),
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": INHERITED_TURN_BUDGET_SCHEMA,
            "safety_deadline": {"monotonic_ns": self.safety_deadline.monotonic_ns},
            "model_anti_loop": {
                "max_model_calls": self.model_anti_loop.max_model_calls,
                "max_model_retries": self.model_anti_loop.max_model_retries,
            },
            "resources": {
                "max_tool_calls": self.resources.max_tool_calls,
                "max_advisory_calls": self.resources.max_advisory_calls,
                "max_output_tokens": self.resources.max_output_tokens,
            },
        }


@dataclass(frozen=True, slots=True, repr=False)
class PendingWorkAdmission:
    """Turn-scoped wrapper over the existing pending admission contract."""

    admission: PendingDurableTurnAdmission = field(repr=False)
    owner_kind: PendingOwnerKind
    turn_authority_sha256: str
    scope_binding_sha256: str
    owner_binding_sha256: str | None
    _seal: _Seal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.admission) is not PendingDurableTurnAdmission:
            raise TurnContextError("pending admission must wrap the exact existing contract")
        if type(self.owner_kind) is not PendingOwnerKind:
            raise TurnContextError("pending owner kind must be closed")
        _digest(self.turn_authority_sha256, label="pending turn authority")
        _digest(self.scope_binding_sha256, label="pending scope binding")
        _digest(self.owner_binding_sha256, label="pending owner binding", optional=True)
        if self.owner_kind in {PendingOwnerKind.WORK_ITEM, PendingOwnerKind.WORK_GRAPH}:
            if self.owner_binding_sha256 is None:
                raise TurnContextError("typed pending owner requires an opaque binding")
        elif self.owner_binding_sha256 is not None:
            raise TurnContextError("unbound pending state cannot carry an owner binding")
        _validate_seal(
            self._seal,
            kind="pending work admission",
            payload=self.payload(),
            component_ids=(id(self.admission),),
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": PENDING_WORK_BINDING_SCHEMA,
            "state": self.admission.state.value,
            "owner_kind": self.owner_kind.value,
            "turn_authority_sha256": self.turn_authority_sha256,
            "scope_binding_sha256": self.scope_binding_sha256,
            "owner_binding_sha256": self.owner_binding_sha256,
            "revision": self.admission.revision,
        }


@dataclass(frozen=True, slots=True, repr=False)
class EffectFence:
    turn_id: str
    context_authority_sha256: str
    request_effect_binding_sha256: str | None
    effect_owner: EffectOwner
    final_publisher: FinalPublisher
    binding_sha256: str
    _seal: _Seal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.turn_id) is not str or _TURN_ID_RE.fullmatch(self.turn_id) is None:
            raise TurnContextError("effect fence turn_id is invalid")
        _digest(self.context_authority_sha256, label="effect context authority")
        _digest(self.request_effect_binding_sha256, label="request effect binding", optional=True)
        if type(self.effect_owner) is not EffectOwner or type(self.final_publisher) is not FinalPublisher:
            raise TurnContextError("effect and publication owners must be primary")
        _digest(self.binding_sha256, label="effect fence binding")
        _validate_seal(self._seal, kind="effect fence", payload=self.payload())

    def payload(self) -> dict[str, object]:
        return {
            "schema": EFFECT_FENCE_SCHEMA,
            "turn_id": self.turn_id,
            "context_authority_sha256": self.context_authority_sha256,
            "request_effect_binding_sha256": self.request_effect_binding_sha256,
            "effect_owner": self.effect_owner.value,
            "final_publisher": self.final_publisher.value,
            "binding_sha256": self.binding_sha256,
        }


def _source_sort_key(source: AuthorizedSourceIdentity) -> tuple[int, int, str, str]:
    return (
        0 if source.kind is AuthorizedSourceKind.ACCEPTED_INGRESS else 1,
        source.ordinal or 0,
        source.kind.value,
        source.identity_sha256,
    )


def _validate_model_input(model_input: TurnInput) -> None:
    if type(model_input) is not TurnInput:
        raise TurnContextError("model_input must be the exact TurnInput contract")
    for name, maximum in (("message", 16_000), ("reply_quote", 1_000), ("conversation_mode", 80)):
        value = getattr(model_input, name)
        if type(value) is not str or len(value) > maximum:
            raise TurnContextError(f"TurnInput {name} is invalid")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise TurnContextError(f"TurnInput {name} is invalid") from exc
    for name in (
        "message_truncated",
        "reply_quote_truncated",
        "conversation_present",
        "enable_tools",
        "attachments_truncated",
        "synthetic_document_notice",
        "quoted_attachment_reference",
        "reply_assistant_reference",
        "actor_is_owner",
        "shared_archive",
    ):
        if type(getattr(model_input, name)) is not bool:
            raise TurnContextError(f"TurnInput {name} must be a boolean")
    if type(model_input.attachments) is not tuple or len(model_input.attachments) > 16:
        raise TurnContextError("TurnInput attachments are invalid")
    previous = 0
    for item in model_input.attachments:
        if type(item) is not AttachmentDescriptor:
            raise TurnContextError("TurnInput attachment descriptor is invalid")
        if (
            type(item.ordinal) is not int
            or not previous < item.ordinal <= 16
            or item.name != f"attachment-{item.ordinal}"
            or item.media_type not in _MODEL_MEDIA_TYPES
            or type(item.extracted_text_available) is not bool
            or (
                item.size_bytes is not None
                and (type(item.size_bytes) is not int or not 0 <= item.size_bytes <= 1_000_000_000)
            )
        ):
            raise TurnContextError("TurnInput attachment descriptor is invalid")
        previous = item.ordinal
    _canonical_bytes(model_input.model_payload(), maximum=_MAX_PRIVATE_BINDING_BYTES)


def _validate_pending_contract(admission: PendingDurableTurnAdmission) -> None:
    if type(admission) is not PendingDurableTurnAdmission:
        raise TurnContextError("pending work admission has an invalid type")
    try:
        replayed = PendingDurableTurnAdmission(
            state=admission.state,
            person_id=admission.person_id,
            conversation_id=admission.conversation_id,
            work_item_id=admission.work_item_id,
            work_graph_id=admission.work_graph_id,
            revision=admission.revision,
        )
    except (TypeError, ValueError) as exc:
        raise TurnContextError("pending work admission is invalid") from exc
    if replayed != admission:
        raise TurnContextError("pending work admission is invalid")


def _context_authority_payload(
    *,
    identity: TurnIdentity,
    authority: AuthenticatedIngressAuthority,
    model_input_binding_sha256: str,
    authorized_sources: tuple[AuthorizedSourceIdentity, ...],
    turn_policy: TurnPolicy,
    inherited_budget: InheritedTurnBudget,
    pending_work_admission: PendingWorkAdmission | None,
) -> dict[str, object]:
    return {
        "schema": "friday.authenticated-turn-context-authority.v1",
        "identity": identity.payload(),
        "authority": authority.payload(),
        "model_input_binding_sha256": model_input_binding_sha256,
        "authorized_sources": [item.payload() for item in authorized_sources],
        "turn_policy": turn_policy.payload(),
        "inherited_budget": inherited_budget.payload(),
        "pending_work_admission": (
            pending_work_admission.payload() if pending_work_admission is not None else None
        ),
    }


def _context_payload(
    *,
    identity: TurnIdentity,
    authority: AuthenticatedIngressAuthority,
    model_input_binding_sha256: str,
    authorized_sources: tuple[AuthorizedSourceIdentity, ...],
    turn_policy: TurnPolicy,
    inherited_budget: InheritedTurnBudget,
    pending_work_admission: PendingWorkAdmission | None,
    context_authority_sha256: str,
    effect_fence: EffectFence,
) -> dict[str, object]:
    payload = _context_authority_payload(
        identity=identity,
        authority=authority,
        model_input_binding_sha256=model_input_binding_sha256,
        authorized_sources=authorized_sources,
        turn_policy=turn_policy,
        inherited_budget=inherited_budget,
        pending_work_admission=pending_work_admission,
    )
    return {
        "schema": AUTHENTICATED_TURN_CONTEXT_SCHEMA,
        **{key: value for key, value in payload.items() if key != "schema"},
        "context_authority_sha256": context_authority_sha256,
        "effect_fence": effect_fence.payload(),
    }


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedTurnContext:
    """Immutable authenticated context carried unchanged through the turn."""

    identity: TurnIdentity
    authority: AuthenticatedIngressAuthority
    model_input: TurnInput = field(repr=False)
    model_input_binding_sha256: str
    authorized_sources: tuple[AuthorizedSourceIdentity, ...]
    turn_policy: TurnPolicy
    inherited_budget: InheritedTurnBudget
    pending_work_admission: PendingWorkAdmission | None = field(repr=False)
    context_authority_sha256: str
    effect_fence: EffectFence
    _seal: _Seal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        components = (
            self.identity,
            self.authority,
            self.model_input,
            self.authorized_sources,
            self.turn_policy,
            self.inherited_budget,
            self.pending_work_admission,
            self.effect_fence,
        )
        _validate_seal(
            self._seal,
            kind="authenticated turn context",
            payload=self.canonical_payload(),
            component_ids=tuple(id(item) for item in components),
        )
        _validate_context_structure(self)

    @property
    def turn_id(self) -> str:
        return self.identity.turn_id

    def model_payload(self) -> dict[str, Any]:
        """Delegate byte/behavior semantics to the one existing projection."""

        return self.model_input.model_payload()

    def advisory_projection(self) -> dict[str, object]:
        """Return bounded advisory-only input or fail closed on secrets/size."""

        model_payload = self.model_input.model_payload()
        model_payload.pop("enable_tools", None)
        model_payload.pop("authority", None)
        projection: dict[str, object] = {
            "schema": ADVISORY_TURN_PROJECTION_SCHEMA,
            "advisory_only": True,
            "model_input": model_payload,
        }
        visible = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        visible_messages = tuple(
            {"role": "user", "content": value} for value in (*_visible_strings(projection), visible)
        )
        if len(
            visible.encode("utf-8")
        ) > _MAX_ADVISORY_UTF8_BYTES or not secondary_model_messages_are_secret_free(visible_messages):
            raise TurnContextError("advisory turn projection is not safe for a secondary model")
        return projection

    def canonical_payload(self) -> dict[str, object]:
        return _context_payload(
            identity=self.identity,
            authority=self.authority,
            model_input_binding_sha256=self.model_input_binding_sha256,
            authorized_sources=self.authorized_sources,
            turn_policy=self.turn_policy,
            inherited_budget=self.inherited_budget,
            pending_work_admission=self.pending_work_admission,
            context_authority_sha256=self.context_authority_sha256,
            effect_fence=self.effect_fence,
        )

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.canonical_payload(), maximum=_MAX_CANONICAL_CONTEXT_BYTES)

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def verify_canonical_bytes(self, raw: bytes) -> None:
        if type(raw) is not bytes:
            raise TypeError("canonical turn context must be bytes")
        if not 0 < len(raw) <= _MAX_CANONICAL_CONTEXT_BYTES:
            raise TurnContextError("canonical turn context JSON is invalid")
        try:
            decoded = json.loads(
                raw.decode("ascii", errors="strict"),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
                parse_float=_reject_json_float,
            )
        except TurnContextError:
            raise
        except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
            raise TurnContextError("canonical turn context JSON is invalid") from exc
        if type(decoded) is not dict or decoded != self.canonical_payload() or raw != self.canonical_bytes():
            raise TurnContextError("canonical turn context JSON is invalid")


def _validate_source_set(context: AuthenticatedTurnContext) -> None:
    sources = context.authorized_sources
    if (
        type(sources) is not tuple
        or not 1 <= len(sources) <= _MAX_AUTHORIZED_SOURCES
        or any(type(item) is not AuthorizedSourceIdentity for item in sources)
    ):
        raise TurnContextError("authorized sources must be a bounded exact tuple")
    keys = tuple(_source_sort_key(item) for item in sources)
    if tuple(sorted(keys)) != keys or len(set(keys)) != len(keys):
        raise TurnContextError("authorized sources must be sorted and unique")
    authority_sha256 = context.authority.canonical_sha256()
    if any(item.turn_authority_sha256 != authority_sha256 for item in sources):
        raise TurnContextError("authorized source belongs to another turn authority")
    ingress = tuple(item for item in sources if item.kind is AuthorizedSourceKind.ACCEPTED_INGRESS)
    if len(ingress) != 1:
        raise TurnContextError("authorized sources require exactly one accepted ingress")
    input_ordinals = tuple(item.ordinal for item in context.model_input.attachments)
    source_ordinals = tuple(
        sorted(
            item.ordinal
            for item in sources
            if item.kind is not AuthorizedSourceKind.ACCEPTED_INGRESS and item.ordinal is not None
        )
    )
    if source_ordinals != input_ordinals:
        raise TurnContextError("authorized attachment identities differ from TurnInput attachments")


def _validate_context_structure(context: AuthenticatedTurnContext) -> None:
    if (
        type(context.identity) is not TurnIdentity
        or type(context.authority) is not AuthenticatedIngressAuthority
    ):
        raise TurnContextError("turn identity or ingress authority has an invalid type")
    if context.identity != TurnIdentity.from_authority(context.authority):
        raise TurnContextError("turn identity is not bound to ingress authority")
    _validate_model_input(context.model_input)
    _digest(context.model_input_binding_sha256, label="model input binding")
    if context.model_input.conversation_present is not (
        context.authority.conversation.kind is ConversationScopeKind.EXISTING
    ):
        raise TurnContextError("TurnInput conversation scope differs from ingress authority")
    if context.model_input.conversation_mode != context.authority.interaction_mode.value:
        raise TurnContextError("TurnInput mode differs from ingress authority")
    if (
        context.model_input.actor_is_owner is not context.authority.actor.is_owner
        or context.model_input.shared_archive is not context.authority.actor.shared_tenant
    ):
        raise TurnContextError("TurnInput authority projection differs from authenticated actor")
    _validate_source_set(context)
    if (
        type(context.turn_policy) is not TurnPolicy
        or type(context.inherited_budget) is not InheritedTurnBudget
    ):
        raise TurnContextError("turn policy or inherited budget has an invalid type")
    if context.pending_work_admission is not None:
        if type(context.pending_work_admission) is not PendingWorkAdmission:
            raise TurnContextError("pending work admission binding is invalid")
        if context.pending_work_admission.turn_authority_sha256 != context.authority.canonical_sha256():
            raise TurnContextError("pending work admission belongs to another turn authority")
        if context.turn_policy.router_mode is not RouterMode.LEGACY:
            raise TurnContextError("pending work ownership requires the legacy continuation owner")
    expected_context_authority = _sha256(
        _context_authority_payload(
            identity=context.identity,
            authority=context.authority,
            model_input_binding_sha256=context.model_input_binding_sha256,
            authorized_sources=context.authorized_sources,
            turn_policy=context.turn_policy,
            inherited_budget=context.inherited_budget,
            pending_work_admission=context.pending_work_admission,
        )
    )
    if context.context_authority_sha256 != expected_context_authority:
        raise TurnContextError("authenticated context authority binding is stale")
    if (
        type(context.effect_fence) is not EffectFence
        or context.effect_fence.turn_id != context.turn_id
        or context.effect_fence.context_authority_sha256 != context.context_authority_sha256
        or context.effect_fence.request_effect_binding_sha256
        != context.authority.request_effect_binding_sha256
    ):
        raise TurnContextError("effect fence is not bound to the full turn authority")


@dataclass(frozen=True, slots=True, repr=False)
class TurnContextIssuer:
    """Trusted pure issuer backed by the deployment's durable privacy key."""

    _namespace_key: bytes = field(repr=False, compare=False)
    _namespace_fingerprint: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self._namespace_key) is not bytes or len(self._namespace_key) != hashlib.sha256().digest_size:
            raise TurnContextError("turn context namespace key must be exactly 32 bytes")
        object.__setattr__(
            self,
            "_namespace_fingerprint",
            _keyed_binding(self._namespace_key, b"friday/turn-context/namespace/v1\0", "active"),
        )

    def issue_ingress_authority(
        self,
        *,
        ingress_kind: IngressKind,
        ingress_issued_token: str,
        actor: ActorContext,
        conversation_id: str | None,
        interaction_mode: TurnMode,
        source_id: str,
        update_id: str,
        request_effect_binding_sha256: str | None,
    ) -> AuthenticatedIngressAuthority:
        """Bind one supplied durable token; this function never invents it."""

        if type(ingress_kind) is not IngressKind or type(interaction_mode) is not TurnMode:
            raise TurnContextError("ingress kind or interaction mode must be closed")
        if type(actor) is not ActorContext:
            raise TurnContextError("actor must be the exact authenticated ActorContext")
        token = _opaque_id(ingress_issued_token, label="ingress-issued token")
        source = _opaque_id(source_id, label="ingress source identity")
        update = _opaque_id(update_id, label="ingress update identity")
        request_binding = _digest(
            request_effect_binding_sha256,
            label="request effect binding",
            optional=True,
        )
        try:
            actor_binding = supervisor_canary_actor_binding_sha256(
                actor,
                namespace_key=self._namespace_key,
            )
        except (TypeError, ValueError) as exc:
            raise TurnContextError("authenticated actor projection is invalid") from exc
        tenant_id = _opaque_id(actor.user_id, label="tenant identity")
        person_id = _opaque_id(actor.own_id, label="person identity")
        accepted_binding = _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/accepted-ingress/v1\0",
            token,
        )
        conversation = self._issue_conversation_admission(
            accepted_ingress_binding_sha256=accepted_binding,
            person_id=person_id,
            conversation_id=conversation_id,
        )
        values = {
            "issuer_fingerprint_sha256": self._namespace_fingerprint,
            "accepted_ingress_binding_sha256": accepted_binding,
            "actor_binding_sha256": actor_binding,
            "tenant_binding_sha256": _keyed_binding(
                self._namespace_key,
                b"friday/turn-context/tenant/v1\0",
                tenant_id,
            ),
            "person_binding_sha256": _keyed_binding(
                self._namespace_key,
                b"friday/turn-context/person/v1\0",
                person_id,
            ),
            "source_binding_sha256": _keyed_binding(
                self._namespace_key,
                b"friday/turn-context/ingress-source/v1\0",
                source,
            ),
            "update_binding_sha256": _keyed_binding(
                self._namespace_key,
                b"friday/turn-context/ingress-update/v1\0",
                update,
            ),
        }
        payload = {
            "schema": AUTHENTICATED_INGRESS_AUTHORITY_SCHEMA,
            "ingress_kind": ingress_kind.value,
            "interaction_mode": interaction_mode.value,
            "conversation": conversation.payload(),
            "request_effect_binding_sha256": request_binding,
            **values,
        }
        return AuthenticatedIngressAuthority(
            ingress_kind=ingress_kind,
            interaction_mode=interaction_mode,
            actor=actor,
            conversation=conversation,
            ingress_issued_token=token,
            source_id=source,
            update_id=update,
            request_effect_binding_sha256=request_binding,
            _seal=self._seal(
                kind="ingress authority",
                payload=payload,
                component_ids=(id(actor), id(conversation)),
            ),
            **values,
        )

    def issue_turn_policy(
        self,
        *,
        router_mode: RouterMode,
        fallback_router_mode: RouterMode | None,
        decision: TurnPolicyDecision,
    ) -> TurnPolicy:
        private_payload = _turn_policy_decision_payload(decision)
        decision_binding = _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/turn-policy-decision/v1\0",
            private_payload,
        )
        payload = {
            "schema": TURN_POLICY_SCHEMA,
            "router_mode": router_mode.value if type(router_mode) is RouterMode else router_mode,
            "fallback_router_mode": (
                fallback_router_mode.value
                if type(fallback_router_mode) is RouterMode
                else fallback_router_mode
            ),
            "decision_binding_sha256": decision_binding,
            "raw_message_reclassification": False,
        }
        return TurnPolicy(
            router_mode=router_mode,
            fallback_router_mode=fallback_router_mode,
            decision=decision,
            decision_binding_sha256=decision_binding,
            _seal=self._seal(
                kind="turn policy",
                payload=payload,
                component_ids=(id(decision),),
            ),
        )

    def accepted_ingress_source(
        self,
        authority: AuthenticatedIngressAuthority,
    ) -> AuthorizedSourceIdentity:
        self._require_authority(authority)
        private_payload = {
            "kind": AuthorizedSourceKind.ACCEPTED_INGRESS.value,
            "turn_authority_sha256": authority.canonical_sha256(),
            "accepted_ingress_binding_sha256": authority.accepted_ingress_binding_sha256,
        }
        return self._issue_source(
            authority=authority,
            kind=AuthorizedSourceKind.ACCEPTED_INGRESS,
            ordinal=None,
            private_carrier=authority,
            private_payload=private_payload,
        )

    def current_attachment_source(
        self,
        *,
        authority: AuthenticatedIngressAuthority,
        ordinal: int,
        carrier: object,
    ) -> AuthorizedSourceIdentity:
        self._require_authority(authority)
        ordinal = _bounded_int(ordinal, label="attachment source ordinal", minimum=1, maximum=16)
        token = current_turn_file_reference_of(carrier)
        if type(token) is not CurrentTurnFileReferenceToken:
            raise TurnContextError("current attachment source lacks a process-owned token")
        private_payload = {
            "kind": AuthorizedSourceKind.CURRENT_ATTACHMENT.value,
            "turn_authority_sha256": authority.canonical_sha256(),
            "ordinal": ordinal,
            "raw_id": token.raw_id,
            "source_identity_sha256": token.source_identity_sha256,
            "content_sha256": token.content_sha256,
            "reinspect_current_upload": token.reinspect_current_upload,
        }
        return self._issue_source(
            authority=authority,
            kind=AuthorizedSourceKind.CURRENT_ATTACHMENT,
            ordinal=ordinal,
            private_carrier=token,
            private_payload=private_payload,
        )

    def registered_file_source(
        self,
        *,
        authority: AuthenticatedIngressAuthority,
        ordinal: int,
        token: AuthorizedFileSnapshotToken,
    ) -> AuthorizedSourceIdentity:
        self._require_authority(authority)
        ordinal = _bounded_int(ordinal, label="attachment source ordinal", minimum=1, maximum=16)
        if not authorized_file_snapshot_token_is_process_owned(token):
            raise TurnContextError("registered file source lacks a process-owned token")
        private_payload = {
            "kind": AuthorizedSourceKind.REGISTERED_FILE.value,
            "turn_authority_sha256": authority.canonical_sha256(),
            "ordinal": ordinal,
            "raw_id": token.source.raw_id,
            "source_identity_sha256": token.source.identity_sha256,
            "content_sha256": token.content_sha256,
        }
        return self._issue_source(
            authority=authority,
            kind=AuthorizedSourceKind.REGISTERED_FILE,
            ordinal=ordinal,
            private_carrier=token,
            private_payload=private_payload,
        )

    def bind_pending_work(
        self,
        *,
        authority: AuthenticatedIngressAuthority,
        admission: PendingDurableTurnAdmission,
    ) -> PendingWorkAdmission:
        self._require_authority(authority)
        _validate_pending_contract(admission)
        if (
            authority.conversation.kind is not ConversationScopeKind.EXISTING
            or admission.person_id != authority.person_id
            or admission.conversation_id != authority.conversation_id
        ):
            raise TurnContextError("pending work admission belongs to another turn scope")
        if admission.state is PendingDurableAdmissionState.UNCERTAIN:
            owner_kind = PendingOwnerKind.UNCERTAIN_FAIL_CLOSED
        elif admission.work_item_id is not None:
            owner_kind = PendingOwnerKind.WORK_ITEM
        elif admission.work_graph_id is not None:
            owner_kind = PendingOwnerKind.WORK_GRAPH
        else:
            owner_kind = PendingOwnerKind.LEGACY_PENDING_RUNTIME
        authority_sha256 = authority.canonical_sha256()
        scope_binding = _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/pending-scope/v1\0",
            {
                "turn_authority_sha256": authority_sha256,
                "person_id": admission.person_id,
                "conversation_id": admission.conversation_id,
            },
        )
        owner_binding = None
        if admission.is_bound:
            owner_binding = _keyed_binding(
                self._namespace_key,
                b"friday/turn-context/pending-owner/v1\0",
                {
                    "turn_authority_sha256": authority_sha256,
                    "scope_binding_sha256": scope_binding,
                    "owner_kind": owner_kind.value,
                    "identity": admission.binding_id,
                    "revision": admission.revision,
                },
            )
        payload = {
            "schema": PENDING_WORK_BINDING_SCHEMA,
            "state": admission.state.value,
            "owner_kind": owner_kind.value,
            "turn_authority_sha256": authority_sha256,
            "scope_binding_sha256": scope_binding,
            "owner_binding_sha256": owner_binding,
            "revision": admission.revision,
        }
        return PendingWorkAdmission(
            admission=admission,
            owner_kind=owner_kind,
            turn_authority_sha256=authority_sha256,
            scope_binding_sha256=scope_binding,
            owner_binding_sha256=owner_binding,
            _seal=self._seal(
                kind="pending work admission",
                payload=payload,
                component_ids=(id(admission),),
            ),
        )

    def authenticate_turn(
        self,
        *,
        authority: AuthenticatedIngressAuthority,
        model_input: TurnInput,
        authorized_sources: tuple[AuthorizedSourceIdentity, ...],
        turn_policy: TurnPolicy,
        inherited_budget: InheritedTurnBudget,
        pending_work_admission: PendingWorkAdmission | None,
        now_monotonic_ns: int,
    ) -> AuthenticatedTurnContext:
        self._require_authority(authority)
        self._require_policy(turn_policy)
        _validate_model_input(model_input)
        if type(authorized_sources) is not tuple:
            raise TurnContextError("authorized sources must be an exact tuple")
        for source in authorized_sources:
            self._require_source(authority, source)
        if pending_work_admission is not None:
            self._require_pending(authority, pending_work_admission)
        now = _bounded_int(
            now_monotonic_ns,
            label="current monotonic instant",
            minimum=1,
            maximum=_MAX_MONOTONIC_NS,
        )
        if type(inherited_budget) is not InheritedTurnBudget:
            raise TurnContextError("inherited turn budget has an invalid type")
        remaining = inherited_budget.safety_deadline.monotonic_ns - now
        if not 0 < remaining <= _MAX_TURN_HORIZON_NS:
            raise TurnContextError("turn safety deadline is expired or exceeds the admitted horizon")
        model_binding = self._model_input_binding(model_input)
        identity = TurnIdentity.from_authority(authority)
        context_authority = _sha256(
            _context_authority_payload(
                identity=identity,
                authority=authority,
                model_input_binding_sha256=model_binding,
                authorized_sources=authorized_sources,
                turn_policy=turn_policy,
                inherited_budget=inherited_budget,
                pending_work_admission=pending_work_admission,
            )
        )
        effect_fence = self._issue_effect_fence(
            identity=identity,
            context_authority_sha256=context_authority,
            request_effect_binding_sha256=authority.request_effect_binding_sha256,
        )
        payload = _context_payload(
            identity=identity,
            authority=authority,
            model_input_binding_sha256=model_binding,
            authorized_sources=authorized_sources,
            turn_policy=turn_policy,
            inherited_budget=inherited_budget,
            pending_work_admission=pending_work_admission,
            context_authority_sha256=context_authority,
            effect_fence=effect_fence,
        )
        components = (
            identity,
            authority,
            model_input,
            authorized_sources,
            turn_policy,
            inherited_budget,
            pending_work_admission,
            effect_fence,
        )
        context = AuthenticatedTurnContext(
            identity=identity,
            authority=authority,
            model_input=model_input,
            model_input_binding_sha256=model_binding,
            authorized_sources=authorized_sources,
            turn_policy=turn_policy,
            inherited_budget=inherited_budget,
            pending_work_admission=pending_work_admission,
            context_authority_sha256=context_authority,
            effect_fence=effect_fence,
            _seal=self._seal(
                kind="authenticated turn context",
                payload=payload,
                component_ids=tuple(id(item) for item in components),
            ),
        )
        return self.require_context(context)

    def require_context(self, context: AuthenticatedTurnContext) -> AuthenticatedTurnContext:
        """Reject exact-looking contexts minted by any other namespace/key."""

        if (
            type(context) is not AuthenticatedTurnContext
            or context._seal.namespace_fingerprint != self._namespace_fingerprint
        ):
            raise TurnContextError("authenticated turn context belongs to another issuer")
        _validate_context_structure(context)
        self._require_authority(context.authority)
        self._require_policy(context.turn_policy)
        if not hmac.compare_digest(
            self._model_input_binding(context.model_input), context.model_input_binding_sha256
        ):
            raise TurnContextError("model input binding is stale")
        for source in context.authorized_sources:
            self._require_source(context.authority, source)
        if context.pending_work_admission is not None:
            self._require_pending(context.authority, context.pending_work_admission)
        expected_effect_fence = self._issue_effect_fence(
            identity=context.identity,
            context_authority_sha256=context.context_authority_sha256,
            request_effect_binding_sha256=context.authority.request_effect_binding_sha256,
        )
        if expected_effect_fence.payload() != context.effect_fence.payload():
            raise TurnContextError("effect fence binding is stale")
        return context

    def _issue_conversation_admission(
        self,
        *,
        accepted_ingress_binding_sha256: str,
        person_id: str,
        conversation_id: str | None,
    ) -> ConversationAdmission:
        if conversation_id is None:
            kind = ConversationScopeKind.NEW
            canonical_id = None
        else:
            if type(conversation_id) is not str or _CONVERSATION_ID_RE.fullmatch(conversation_id) is None:
                raise TurnContextError("conversation identity is invalid")
            kind = ConversationScopeKind.EXISTING
            canonical_id = conversation_id
        binding = _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/conversation-admission/v1\0",
            {
                "kind": kind.value,
                "conversation_id": canonical_id,
                "person_id": person_id,
                "accepted_ingress_binding_sha256": accepted_ingress_binding_sha256,
            },
        )
        payload = {
            "schema": CONVERSATION_ADMISSION_SCHEMA,
            "kind": kind.value,
            "binding_sha256": binding,
        }
        return ConversationAdmission(
            kind=kind,
            conversation_id=canonical_id,
            binding_sha256=binding,
            _seal=self._seal(
                kind="conversation admission",
                payload=payload,
                component_ids=(id(canonical_id),),
            ),
        )

    def _issue_source(
        self,
        *,
        authority: AuthenticatedIngressAuthority,
        kind: AuthorizedSourceKind,
        ordinal: int | None,
        private_carrier: object,
        private_payload: Mapping[str, object],
    ) -> AuthorizedSourceIdentity:
        identity = _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/authorized-source/v1\0",
            private_payload,
        )
        payload = {
            "schema": AUTHORIZED_SOURCE_IDENTITY_SCHEMA,
            "kind": kind.value,
            "ordinal": ordinal,
            "turn_authority_sha256": authority.canonical_sha256(),
            "identity_sha256": identity,
        }
        return AuthorizedSourceIdentity(
            kind=kind,
            ordinal=ordinal,
            turn_authority_sha256=authority.canonical_sha256(),
            identity_sha256=identity,
            private_carrier=private_carrier,
            _seal=self._seal(
                kind="authorized source",
                payload=payload,
                component_ids=(id(private_carrier),),
                private_binding_sha256=_sha256(private_payload),
            ),
        )

    def _issue_effect_fence(
        self,
        *,
        identity: TurnIdentity,
        context_authority_sha256: str,
        request_effect_binding_sha256: str | None,
    ) -> EffectFence:
        material = {
            "schema": "friday.effect-fence-binding.v1",
            "turn_id": identity.turn_id,
            "context_authority_sha256": context_authority_sha256,
            "request_effect_binding_sha256": request_effect_binding_sha256,
            "effect_owner": EffectOwner.PRIMARY.value,
            "final_publisher": FinalPublisher.PRIMARY.value,
        }
        binding = _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/effect-fence/v1\0",
            material,
        )
        payload = {
            "schema": EFFECT_FENCE_SCHEMA,
            **{key: value for key, value in material.items() if key != "schema"},
            "binding_sha256": binding,
        }
        return EffectFence(
            turn_id=identity.turn_id,
            context_authority_sha256=context_authority_sha256,
            request_effect_binding_sha256=request_effect_binding_sha256,
            effect_owner=EffectOwner.PRIMARY,
            final_publisher=FinalPublisher.PRIMARY,
            binding_sha256=binding,
            _seal=self._seal(kind="effect fence", payload=payload),
        )

    def _model_input_binding(self, model_input: TurnInput) -> str:
        _validate_model_input(model_input)
        return _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/model-input/v1\0",
            model_input.model_payload(),
        )

    def _require_authority(self, authority: AuthenticatedIngressAuthority) -> None:
        if (
            type(authority) is not AuthenticatedIngressAuthority
            or authority.issuer_fingerprint_sha256 != self._namespace_fingerprint
            or authority._seal.namespace_fingerprint != self._namespace_fingerprint
        ):
            raise TurnContextError("ingress authority belongs to another issuer")
        expected = self.issue_ingress_authority(
            ingress_kind=authority.ingress_kind,
            ingress_issued_token=authority.ingress_issued_token,
            actor=authority.actor,
            conversation_id=authority.conversation_id,
            interaction_mode=authority.interaction_mode,
            source_id=authority.source_id,
            update_id=authority.update_id,
            request_effect_binding_sha256=authority.request_effect_binding_sha256,
        )
        if expected.payload() != authority.payload():
            raise TurnContextError("ingress authority binding is stale")

    def _require_policy(self, policy: TurnPolicy) -> None:
        if (
            type(policy) is not TurnPolicy
            or policy._seal.namespace_fingerprint != self._namespace_fingerprint
        ):
            raise TurnContextError("turn policy belongs to another issuer")
        expected = self.issue_turn_policy(
            router_mode=policy.router_mode,
            fallback_router_mode=policy.fallback_router_mode,
            decision=policy.decision,
        )
        if expected.payload() != policy.payload():
            raise TurnContextError("turn policy binding is stale")

    def _require_source(
        self,
        authority: AuthenticatedIngressAuthority,
        source: AuthorizedSourceIdentity,
    ) -> None:
        if type(source) is not AuthorizedSourceIdentity or source._seal.namespace_fingerprint != (
            self._namespace_fingerprint
        ):
            raise TurnContextError("authorized source belongs to another issuer")
        if source.kind is AuthorizedSourceKind.ACCEPTED_INGRESS:
            if source.private_carrier is not authority:
                raise TurnContextError("accepted ingress source carrier is stale")
            expected = self.accepted_ingress_source(authority)
        elif source.kind is AuthorizedSourceKind.CURRENT_ATTACHMENT:
            token = source.private_carrier
            if type(token) is not CurrentTurnFileReferenceToken:
                raise TurnContextError("current attachment source carrier is stale")
            private_payload = {
                "kind": AuthorizedSourceKind.CURRENT_ATTACHMENT.value,
                "turn_authority_sha256": authority.canonical_sha256(),
                "ordinal": source.ordinal,
                "raw_id": token.raw_id,
                "source_identity_sha256": token.source_identity_sha256,
                "content_sha256": token.content_sha256,
                "reinspect_current_upload": token.reinspect_current_upload,
            }
            expected = self._issue_source(
                authority=authority,
                kind=source.kind,
                ordinal=source.ordinal,
                private_carrier=token,
                private_payload=private_payload,
            )
        else:
            token = source.private_carrier
            if type(
                token
            ) is not AuthorizedFileSnapshotToken or not authorized_file_snapshot_token_is_process_owned(
                token
            ):
                raise TurnContextError("registered file source carrier is stale")
            expected = self.registered_file_source(
                authority=authority,
                ordinal=source.ordinal or 0,
                token=token,
            )
        if expected.payload() != source.payload():
            raise TurnContextError("authorized source binding is stale")

    def _require_pending(
        self,
        authority: AuthenticatedIngressAuthority,
        pending: PendingWorkAdmission,
    ) -> None:
        if (
            type(pending) is not PendingWorkAdmission
            or pending._seal.namespace_fingerprint != self._namespace_fingerprint
        ):
            raise TurnContextError("pending work admission belongs to another issuer")
        expected = self.bind_pending_work(authority=authority, admission=pending.admission)
        if expected.payload() != pending.payload():
            raise TurnContextError("pending work admission binding is stale")

    def _seal(
        self,
        *,
        kind: str,
        payload: object,
        component_ids: tuple[int, ...] = (),
        private_binding_sha256: str | None = None,
    ) -> _Seal:
        return _Seal(
            kind=kind,
            namespace_fingerprint=self._namespace_fingerprint,
            payload_sha256=_sha256(payload),
            component_ids=component_ids,
            private_binding_sha256=private_binding_sha256,
        )


__all__ = [
    "ADVISORY_TURN_PROJECTION_SCHEMA",
    "AUTHENTICATED_INGRESS_AUTHORITY_SCHEMA",
    "AUTHENTICATED_TURN_CONTEXT_SCHEMA",
    "AUTHORIZED_SOURCE_IDENTITY_SCHEMA",
    "CONVERSATION_ADMISSION_SCHEMA",
    "EFFECT_FENCE_SCHEMA",
    "INHERITED_TURN_BUDGET_SCHEMA",
    "PENDING_WORK_BINDING_SCHEMA",
    "TURN_IDENTITY_SCHEMA",
    "TURN_POLICY_SCHEMA",
    "AuthenticatedIngressAuthority",
    "AuthenticatedTurnContext",
    "AuthorizedSourceIdentity",
    "AuthorizedSourceKind",
    "ConversationAdmission",
    "ConversationScopeKind",
    "EffectFence",
    "EffectOwner",
    "FinalPublisher",
    "IngressKind",
    "InheritedTurnBudget",
    "ModelAntiLoopBudget",
    "PendingOwnerKind",
    "PendingWorkAdmission",
    "TurnContextError",
    "TurnContextIssuer",
    "TurnIdentity",
    "TurnMode",
    "TurnPolicy",
    "TurnResourceBudget",
    "TurnSafetyDeadline",
]
