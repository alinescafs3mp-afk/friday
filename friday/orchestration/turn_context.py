"""Authenticated, body-free authority spine for one admitted user turn.

The durable ingress layer supplies an opaque, restart-stable token and the
deployment's existing privacy namespace key.  This module never generates an
ingress token and never persists one.  It derives one deterministic turn
identity, wraps the existing :class:`TurnInput`, and keeps model content out of
the canonical authority digest.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from friday.orchestration.contracts import AttachmentDescriptor, RouterMode, TurnInput
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext

AUTHENTICATED_TURN_CONTEXT_SCHEMA = "friday.authenticated-turn-context.v1"
AUTHENTICATED_INGRESS_AUTHORITY_SCHEMA = "friday.authenticated-ingress-authority.v1"
TURN_IDENTITY_SCHEMA = "friday.turn-identity.v1"
AUTHORIZED_SOURCE_IDENTITY_SCHEMA = "friday.authorized-source-identity.v1"
TURN_POLICY_SCHEMA = "friday.turn-policy.v1"
INHERITED_TURN_BUDGET_SCHEMA = "friday.inherited-turn-budget.v1"
EFFECT_FENCE_SCHEMA = "friday.effect-fence.v1"
ADVISORY_TURN_PROJECTION_SCHEMA = "friday.advisory-turn-projection.v1"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TURN_ID_RE = re.compile(r"turn_[0-9a-f]{64}\Z")
_MAX_OPAQUE_ID_BYTES = 512
_MAX_AUTHORIZED_SOURCES = 32
_MAX_CANONICAL_CONTEXT_BYTES = 32_768
_MAX_UNIX_MILLISECONDS = 253_402_300_799_999
_MODEL_MEDIA_TYPES = frozenset({"archive", "audio", "binary", "image", "office", "pdf", "text", "video"})
_ISSUED_MARKER = object()
_CONTEXT_MARKER = object()


class TurnContextError(ValueError):
    """An authenticated turn component is malformed or relationally invalid."""


class IngressKind(StrEnum):
    TELEGRAM = "telegram"
    SIGNED_HTTP = "signed_http"


class ConversationScopeKind(StrEnum):
    NEW = "new"
    EXISTING = "existing"


class AuthorizedSourceKind(StrEnum):
    ACCEPTED_INGRESS = "accepted_ingress"
    CURRENT_ATTACHMENT = "current_attachment"
    CONVERSATION_SNAPSHOT = "conversation_snapshot"
    ARCHIVE_SNAPSHOT = "archive_snapshot"
    REGISTERED_FILE = "registered_file"
    REMOTE_EVIDENCE = "remote_evidence"


class EffectOwner(StrEnum):
    PRIMARY = "primary"


class FinalPublisher(StrEnum):
    PRIMARY = "primary"


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise TurnContextError("turn context value is not canonical JSON") from exc


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _digest(value: object, *, label: str) -> str:
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
    if len(encoded) > _MAX_OPAQUE_ID_BYTES or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise TurnContextError(f"{label} is invalid")
    return value


def _optional_opaque_id(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _opaque_id(value, label=label)


def _keyed_binding(namespace_key: bytes, domain: bytes, value: object) -> str:
    return hmac.new(namespace_key, domain + _canonical_bytes(value), hashlib.sha256).hexdigest()


class _IssuedSeal:
    __slots__ = ("marker", "namespace_fingerprint", "payload_sha256", "model_authority_sha256")

    def __init__(
        self,
        *,
        namespace_fingerprint: str,
        payload_sha256: str,
        model_authority_sha256: str | None = None,
    ) -> None:
        self.marker = _ISSUED_MARKER
        self.namespace_fingerprint = namespace_fingerprint
        self.payload_sha256 = payload_sha256
        self.model_authority_sha256 = model_authority_sha256


@dataclass(frozen=True, slots=True)
class AuthenticatedIngressAuthority:
    """Opaque bindings issued only after authenticated durable admission."""

    ingress_kind: IngressKind
    conversation_scope: ConversationScopeKind
    accepted_ingress_binding_sha256: str
    actor_binding_sha256: str
    tenant_binding_sha256: str
    person_binding_sha256: str
    conversation_binding_sha256: str
    source_binding_sha256: str
    update_binding_sha256: str
    _seal: _IssuedSeal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.ingress_kind) is not IngressKind:
            raise TurnContextError("ingress kind must be closed")
        if type(self.conversation_scope) is not ConversationScopeKind:
            raise TurnContextError("conversation scope must be closed")
        for label, value in (
            ("accepted ingress binding", self.accepted_ingress_binding_sha256),
            ("actor binding", self.actor_binding_sha256),
            ("tenant binding", self.tenant_binding_sha256),
            ("person binding", self.person_binding_sha256),
            ("conversation binding", self.conversation_binding_sha256),
            ("source binding", self.source_binding_sha256),
            ("update binding", self.update_binding_sha256),
        ):
            _digest(value, label=label)
        if (
            type(self._seal) is not _IssuedSeal
            or self._seal.marker is not _ISSUED_MARKER
            or self._seal.payload_sha256 != _sha256(self.payload())
            or self._seal.model_authority_sha256 is None
        ):
            raise TurnContextError("ingress authority was not issued by the authenticated seam")

    def payload(self) -> dict[str, str]:
        return {
            "schema": AUTHENTICATED_INGRESS_AUTHORITY_SCHEMA,
            "ingress_kind": self.ingress_kind.value,
            "conversation_scope": self.conversation_scope.value,
            "accepted_ingress_binding_sha256": self.accepted_ingress_binding_sha256,
            "actor_binding_sha256": self.actor_binding_sha256,
            "tenant_binding_sha256": self.tenant_binding_sha256,
            "person_binding_sha256": self.person_binding_sha256,
            "conversation_binding_sha256": self.conversation_binding_sha256,
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
        turn_digest = _sha256(
            {
                "schema": TURN_IDENTITY_SCHEMA,
                "authority_sha256": authority_sha256,
            }
        )
        return cls(turn_id=f"turn_{turn_digest}", authority_sha256=authority_sha256)

    def payload(self) -> dict[str, str]:
        return {
            "schema": TURN_IDENTITY_SCHEMA,
            "turn_id": self.turn_id,
            "authority_sha256": self.authority_sha256,
        }


@dataclass(frozen=True, slots=True)
class AuthorizedSourceIdentity:
    """A typed, body-free identity; the source reference remains private."""

    kind: AuthorizedSourceKind
    identity_sha256: str
    _seal: _IssuedSeal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.kind) is not AuthorizedSourceKind:
            raise TurnContextError("authorized source kind must be closed")
        _digest(self.identity_sha256, label="authorized source identity")
        if (
            type(self._seal) is not _IssuedSeal
            or self._seal.marker is not _ISSUED_MARKER
            or self._seal.payload_sha256 != _sha256(self.payload())
            or self._seal.model_authority_sha256 is not None
        ):
            raise TurnContextError("authorized source identity was not issued by code")

    def payload(self) -> dict[str, str]:
        return {
            "schema": AUTHORIZED_SOURCE_IDENTITY_SCHEMA,
            "kind": self.kind.value,
            "identity_sha256": self.identity_sha256,
        }


@dataclass(frozen=True, slots=True)
class TurnPolicy:
    router_mode: RouterMode
    fallback_router_mode: RouterMode | None

    def __post_init__(self) -> None:
        if type(self.router_mode) is not RouterMode:
            raise TurnContextError("turn router mode must be closed")
        if self.router_mode is RouterMode.LEGACY:
            if self.fallback_router_mode is not None:
                raise TurnContextError("legacy strategy cannot install another fallback")
        elif self.fallback_router_mode is not RouterMode.LEGACY:
            raise TurnContextError("non-legacy strategy must fail only to legacy")

    def payload(self) -> dict[str, object]:
        return {
            "schema": TURN_POLICY_SCHEMA,
            "router_mode": self.router_mode.value,
            "fallback_router_mode": (
                self.fallback_router_mode.value if self.fallback_router_mode is not None else None
            ),
            "raw_message_reclassification": False,
        }


@dataclass(frozen=True, slots=True)
class TurnSafetyDeadline:
    unix_ms: int

    def __post_init__(self) -> None:
        _bounded_int(
            self.unix_ms,
            label="turn safety deadline",
            minimum=1,
            maximum=_MAX_UNIX_MILLISECONDS,
        )

    def child(self, requested_unix_ms: int) -> TurnSafetyDeadline:
        requested = _bounded_int(
            requested_unix_ms,
            label="child safety deadline",
            minimum=1,
            maximum=_MAX_UNIX_MILLISECONDS,
        )
        return TurnSafetyDeadline(min(self.unix_ms, requested))


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
        safety_deadline_unix_ms: int,
        max_model_calls: int,
        max_model_retries: int,
        max_tool_calls: int,
        max_advisory_calls: int,
        max_output_tokens: int,
    ) -> InheritedTurnBudget:
        deadline = self.safety_deadline.child(safety_deadline_unix_ms)
        model_calls = min(
            self.model_anti_loop.max_model_calls,
            _bounded_int(max_model_calls, label="child model call limit", minimum=1, maximum=64),
        )
        model_retries = min(
            self.model_anti_loop.max_model_retries,
            _bounded_int(max_model_retries, label="child model retry limit", minimum=0, maximum=16),
            model_calls - 1,
        )
        resources = TurnResourceBudget(
            max_tool_calls=min(
                self.resources.max_tool_calls,
                _bounded_int(max_tool_calls, label="child tool call limit", minimum=0, maximum=64),
            ),
            max_advisory_calls=min(
                self.resources.max_advisory_calls,
                _bounded_int(max_advisory_calls, label="child advisory call limit", minimum=0, maximum=16),
            ),
            max_output_tokens=min(
                self.resources.max_output_tokens,
                _bounded_int(
                    max_output_tokens,
                    label="child output token limit",
                    minimum=1,
                    maximum=1_000_000,
                ),
            ),
        )
        return InheritedTurnBudget(
            safety_deadline=deadline,
            model_anti_loop=ModelAntiLoopBudget(model_calls, model_retries),
            resources=resources,
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": INHERITED_TURN_BUDGET_SCHEMA,
            "safety_deadline": {"unix_ms": self.safety_deadline.unix_ms},
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


@dataclass(frozen=True, slots=True)
class EffectFence:
    turn_id: str
    effect_owner: EffectOwner
    final_publisher: FinalPublisher
    binding_sha256: str

    def __post_init__(self) -> None:
        if type(self.turn_id) is not str or _TURN_ID_RE.fullmatch(self.turn_id) is None:
            raise TurnContextError("effect fence turn_id is invalid")
        if type(self.effect_owner) is not EffectOwner:
            raise TurnContextError("effect owner must be primary")
        if type(self.final_publisher) is not FinalPublisher:
            raise TurnContextError("final publisher must be primary")
        _digest(self.binding_sha256, label="effect fence binding")

    @classmethod
    def for_identity(cls, identity: TurnIdentity) -> EffectFence:
        if type(identity) is not TurnIdentity:
            raise TurnContextError("turn identity is invalid")
        binding = _sha256(
            {
                "schema": EFFECT_FENCE_SCHEMA,
                "turn_id": identity.turn_id,
                "effect_owner": EffectOwner.PRIMARY.value,
                "final_publisher": FinalPublisher.PRIMARY.value,
            }
        )
        return cls(
            turn_id=identity.turn_id,
            effect_owner=EffectOwner.PRIMARY,
            final_publisher=FinalPublisher.PRIMARY,
            binding_sha256=binding,
        )

    def payload(self) -> dict[str, str]:
        return {
            "schema": EFFECT_FENCE_SCHEMA,
            "turn_id": self.turn_id,
            "effect_owner": self.effect_owner.value,
            "final_publisher": self.final_publisher.value,
            "binding_sha256": self.binding_sha256,
        }


class _ContextSeal:
    __slots__ = (
        "marker",
        "namespace_fingerprint",
        "component_ids",
        "pending_owner_binding_sha256",
        "payload_sha256",
    )

    def __init__(
        self,
        *,
        namespace_fingerprint: str,
        component_ids: tuple[int, ...],
        pending_owner_binding_sha256: str | None,
        payload_sha256: str,
    ) -> None:
        self.marker = _CONTEXT_MARKER
        self.namespace_fingerprint = namespace_fingerprint
        self.component_ids = component_ids
        self.pending_owner_binding_sha256 = pending_owner_binding_sha256
        self.payload_sha256 = payload_sha256


def _pending_payload(
    admission: PendingDurableTurnAdmission | None,
    *,
    owner_binding_sha256: str | None,
) -> dict[str, object] | None:
    if admission is None:
        return None
    if admission.work_item_id is not None:
        owner_kind = "work_item"
    elif admission.work_graph_id is not None:
        owner_kind = "work_graph"
    else:
        owner_kind = "unbound"
    return {
        "state": admission.state.value,
        "owner_kind": owner_kind,
        "owner_binding_sha256": owner_binding_sha256,
        "revision": admission.revision,
    }


def _context_payload(
    *,
    identity: TurnIdentity,
    authority: AuthenticatedIngressAuthority,
    authorized_sources: tuple[AuthorizedSourceIdentity, ...],
    turn_policy: TurnPolicy,
    inherited_budget: InheritedTurnBudget,
    effect_fence: EffectFence,
    pending_work_admission: PendingDurableTurnAdmission | None,
    pending_owner_binding_sha256: str | None,
) -> dict[str, object]:
    return {
        "schema": AUTHENTICATED_TURN_CONTEXT_SCHEMA,
        "identity": identity.payload(),
        "authority": authority.payload(),
        "model_input_schema": "friday.turn-input.v1",
        "authorized_sources": [item.payload() for item in authorized_sources],
        "turn_policy": turn_policy.payload(),
        "inherited_budget": inherited_budget.payload(),
        "effect_fence": effect_fence.payload(),
        "pending_work_admission": _pending_payload(
            pending_work_admission,
            owner_binding_sha256=pending_owner_binding_sha256,
        ),
    }


@dataclass(frozen=True, slots=True, repr=False)
class AuthenticatedTurnContext:
    """One immutable authority spine carried unchanged through the whole turn."""

    identity: TurnIdentity
    authority: AuthenticatedIngressAuthority
    model_input: TurnInput = field(repr=False)
    authorized_sources: tuple[AuthorizedSourceIdentity, ...]
    turn_policy: TurnPolicy
    inherited_budget: InheritedTurnBudget
    effect_fence: EffectFence
    pending_work_admission: PendingDurableTurnAdmission | None = field(repr=False)
    _seal: _ContextSeal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        components = (
            self.identity,
            self.authority,
            self.model_input,
            self.authorized_sources,
            self.turn_policy,
            self.inherited_budget,
            self.effect_fence,
            self.pending_work_admission,
        )
        if (
            type(self._seal) is not _ContextSeal
            or self._seal.marker is not _CONTEXT_MARKER
            or self._seal.component_ids != tuple(id(item) for item in components)
        ):
            raise TurnContextError("authenticated turn context must be created by its issuer")
        _validate_context_components(
            identity=self.identity,
            authority=self.authority,
            model_input=self.model_input,
            authorized_sources=self.authorized_sources,
            turn_policy=self.turn_policy,
            inherited_budget=self.inherited_budget,
            effect_fence=self.effect_fence,
            pending_work_admission=self.pending_work_admission,
        )
        if self._seal.payload_sha256 != _sha256(self.canonical_payload()):
            raise TurnContextError("authenticated turn context binding is stale")

    @property
    def turn_id(self) -> str:
        return self.identity.turn_id

    def model_payload(self) -> dict[str, Any]:
        """Return the existing primary projection without adding context fields."""

        return self.model_input.model_payload()

    def advisory_projection(self) -> dict[str, object]:
        """Return bounded model data with no tools, authority, effects or publisher."""

        model_payload = self.model_input.model_payload()
        model_payload.pop("enable_tools", None)
        model_payload.pop("authority", None)
        return {
            "schema": ADVISORY_TURN_PROJECTION_SCHEMA,
            "advisory_only": True,
            "model_input": model_payload,
        }

    def canonical_payload(self) -> dict[str, object]:
        """Return the body-free security spine used for the canonical digest."""

        return _context_payload(
            identity=self.identity,
            authority=self.authority,
            authorized_sources=self.authorized_sources,
            turn_policy=self.turn_policy,
            inherited_budget=self.inherited_budget,
            effect_fence=self.effect_fence,
            pending_work_admission=self.pending_work_admission,
            pending_owner_binding_sha256=self._seal.pending_owner_binding_sha256,
        )

    def canonical_bytes(self) -> bytes:
        encoded = _canonical_bytes(self.canonical_payload())
        if len(encoded) > _MAX_CANONICAL_CONTEXT_BYTES:  # pragma: no cover - closed limits above
            raise TurnContextError("canonical turn context exceeds its size limit")
        return encoded

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _validate_model_input(model_input: TurnInput) -> None:
    if type(model_input) is not TurnInput:
        raise TurnContextError("model_input must be the exact TurnInput contract")
    text_fields = (
        (model_input.message, "message", 16_000, True),
        (model_input.reply_quote, "reply quote", 1_000, True),
        (model_input.conversation_mode, "conversation mode", 40, False),
    )
    for value, label, maximum, allow_empty in text_fields:
        if type(value) is not str or len(value) > maximum or (not allow_empty and not value):
            raise TurnContextError(f"TurnInput {label} is invalid")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise TurnContextError(f"TurnInput {label} is invalid") from exc
    if model_input.conversation_mode != model_input.conversation_mode.casefold():
        raise TurnContextError("TurnInput conversation mode is not canonical")
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
    previous_ordinal = 0
    for attachment in model_input.attachments:
        if type(attachment) is not AttachmentDescriptor:
            raise TurnContextError("TurnInput attachment descriptor is invalid")
        if (
            type(attachment.ordinal) is not int
            or not previous_ordinal < attachment.ordinal <= 16
            or attachment.name != f"attachment-{attachment.ordinal}"
            or attachment.media_type not in _MODEL_MEDIA_TYPES
            or type(attachment.extracted_text_available) is not bool
            or (
                attachment.size_bytes is not None
                and (
                    type(attachment.size_bytes) is not int
                    or not 0 <= attachment.size_bytes <= 1_000_000_000
                )
            )
        ):
            raise TurnContextError("TurnInput attachment descriptor is invalid")
        previous_ordinal = attachment.ordinal
    try:
        _canonical_bytes(model_input.model_payload())
    except TurnContextError as exc:
        raise TurnContextError("TurnInput model payload is invalid") from exc


def _validate_source_set(
    authority: AuthenticatedIngressAuthority,
    sources: tuple[AuthorizedSourceIdentity, ...],
) -> None:
    if (
        type(sources) is not tuple
        or not 1 <= len(sources) <= _MAX_AUTHORIZED_SOURCES
        or any(type(item) is not AuthorizedSourceIdentity for item in sources)
    ):
        raise TurnContextError("authorized sources must be a bounded exact tuple")
    sort_keys = tuple((item.kind.value, item.identity_sha256) for item in sources)
    if tuple(sorted(sort_keys)) != sort_keys or len(set(sort_keys)) != len(sort_keys):
        raise TurnContextError("authorized sources must be sorted and unique")
    expected_ingress_identity = _sha256(
        {
            "schema": "friday.accepted-ingress-source-identity.v1",
            "accepted_ingress_binding_sha256": authority.accepted_ingress_binding_sha256,
        }
    )
    ingress_sources = tuple(item for item in sources if item.kind is AuthorizedSourceKind.ACCEPTED_INGRESS)
    if len(ingress_sources) != 1 or ingress_sources[0].identity_sha256 != expected_ingress_identity:
        raise TurnContextError("authorized sources must contain the exact accepted ingress")


def _revalidate_pending(admission: PendingDurableTurnAdmission | None) -> None:
    if admission is None:
        return
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


def _validate_context_components(
    *,
    identity: TurnIdentity,
    authority: AuthenticatedIngressAuthority,
    model_input: TurnInput,
    authorized_sources: tuple[AuthorizedSourceIdentity, ...],
    turn_policy: TurnPolicy,
    inherited_budget: InheritedTurnBudget,
    effect_fence: EffectFence,
    pending_work_admission: PendingDurableTurnAdmission | None,
) -> None:
    if type(identity) is not TurnIdentity or type(authority) is not AuthenticatedIngressAuthority:
        raise TurnContextError("turn identity or ingress authority has an invalid type")
    if identity != TurnIdentity.from_authority(authority):
        raise TurnContextError("turn identity is not bound to ingress authority")
    _validate_model_input(model_input)
    if model_input.conversation_present is not (authority.conversation_scope is ConversationScopeKind.EXISTING):
        raise TurnContextError("TurnInput conversation scope differs from ingress authority")
    _validate_source_set(authority, authorized_sources)
    if type(turn_policy) is not TurnPolicy or type(inherited_budget) is not InheritedTurnBudget:
        raise TurnContextError("turn policy or inherited budget has an invalid type")
    if type(effect_fence) is not EffectFence or effect_fence != EffectFence.for_identity(identity):
        raise TurnContextError("effect fence is not bound to the turn identity")
    _revalidate_pending(pending_work_admission)


@dataclass(frozen=True, slots=True, repr=False)
class TurnContextIssuer:
    """Pure Phase-A issuance seam; callers supply the durable token and key."""

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
        source_id: str,
        update_id: str,
    ) -> AuthenticatedIngressAuthority:
        """Bind, but never invent, one stable token from accepted durable ingress."""

        if type(ingress_kind) is not IngressKind:
            raise TurnContextError("ingress kind must be closed")
        if type(actor) is not ActorContext:
            raise TurnContextError("actor must be the exact authenticated ActorContext")
        token = _opaque_id(ingress_issued_token, label="ingress-issued token")
        tenant_id = _opaque_id(actor.user_id, label="tenant identity")
        person_id = _opaque_id(actor.own_id, label="person identity")
        principal_id = _optional_opaque_id(actor.identity_id, label="actor principal identity") or person_id
        actor_source = _opaque_id(actor.source, label="actor source")
        conversation = _optional_opaque_id(conversation_id, label="conversation identity")
        source = _opaque_id(source_id, label="ingress source identity")
        update = _opaque_id(update_id, label="ingress update identity")
        if type(actor.shared_tenant) is not bool:
            raise TurnContextError("actor shared-tenant authority is invalid")
        actor_payload = {
            "principal_id": principal_id,
            "tenant_id": tenant_id,
            "person_id": person_id,
            "source": actor_source,
        }
        model_authority_payload = {
            "actor_is_owner": actor.is_owner,
            "shared_archive": actor.shared_tenant,
        }
        values: dict[str, object] = {
            "accepted_ingress_binding_sha256": _keyed_binding(
                self._namespace_key,
                b"friday/turn-context/accepted-ingress/v1\0",
                token,
            ),
            "actor_binding_sha256": _keyed_binding(
                self._namespace_key,
                b"friday/turn-context/actor/v1\0",
                actor_payload,
            ),
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
            "conversation_binding_sha256": _keyed_binding(
                self._namespace_key,
                b"friday/turn-context/conversation/v1\0",
                {"present": conversation is not None, "identity": conversation},
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
            "conversation_scope": (
                ConversationScopeKind.EXISTING.value if conversation is not None else ConversationScopeKind.NEW.value
            ),
            **values,
        }
        seal = _IssuedSeal(
            namespace_fingerprint=self._namespace_fingerprint,
            payload_sha256=_sha256(payload),
            model_authority_sha256=_keyed_binding(
                self._namespace_key,
                b"friday/turn-context/model-authority/v1\0",
                model_authority_payload,
            ),
        )
        return AuthenticatedIngressAuthority(
            ingress_kind=ingress_kind,
            conversation_scope=(
                ConversationScopeKind.EXISTING if conversation is not None else ConversationScopeKind.NEW
            ),
            _seal=seal,
            **values,  # type: ignore[arg-type]
        )

    def accepted_ingress_source(
        self,
        authority: AuthenticatedIngressAuthority,
    ) -> AuthorizedSourceIdentity:
        self._require_authority(authority)
        identity_sha256 = _sha256(
            {
                "schema": "friday.accepted-ingress-source-identity.v1",
                "accepted_ingress_binding_sha256": authority.accepted_ingress_binding_sha256,
            }
        )
        return self._seal_source(AuthorizedSourceKind.ACCEPTED_INGRESS, identity_sha256)

    def issue_authorized_source(
        self,
        *,
        kind: AuthorizedSourceKind,
        code_owned_reference: str,
    ) -> AuthorizedSourceIdentity:
        if type(kind) is not AuthorizedSourceKind or kind is AuthorizedSourceKind.ACCEPTED_INGRESS:
            raise TurnContextError("authorized source kind is invalid for a referenced source")
        reference = _opaque_id(code_owned_reference, label="code-owned source reference")
        identity_sha256 = _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/authorized-source/v1\0",
            {"kind": kind.value, "reference": reference},
        )
        return self._seal_source(kind, identity_sha256)

    def authenticate_turn(
        self,
        *,
        authority: AuthenticatedIngressAuthority,
        model_input: TurnInput,
        authorized_sources: tuple[AuthorizedSourceIdentity, ...],
        turn_policy: TurnPolicy,
        inherited_budget: InheritedTurnBudget,
        pending_work_admission: PendingDurableTurnAdmission | None,
    ) -> AuthenticatedTurnContext:
        self._require_authority(authority)
        _validate_model_input(model_input)
        expected_model_authority = _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/model-authority/v1\0",
            {
                "actor_is_owner": model_input.actor_is_owner,
                "shared_archive": model_input.shared_archive,
            },
        )
        if not hmac.compare_digest(
            authority._seal.model_authority_sha256 or "",
            expected_model_authority,
        ):
            raise TurnContextError("TurnInput authority projection differs from authenticated actor")
        self._require_sources(authorized_sources)
        _validate_source_set(authority, authorized_sources)
        self._validate_pending_scope(authority, pending_work_admission)
        identity = TurnIdentity.from_authority(authority)
        effect_fence = EffectFence.for_identity(identity)
        pending_owner_binding = self._pending_owner_binding(pending_work_admission)
        components = (
            identity,
            authority,
            model_input,
            authorized_sources,
            turn_policy,
            inherited_budget,
            effect_fence,
            pending_work_admission,
        )
        payload = _context_payload(
            identity=identity,
            authority=authority,
            authorized_sources=authorized_sources,
            turn_policy=turn_policy,
            inherited_budget=inherited_budget,
            effect_fence=effect_fence,
            pending_work_admission=pending_work_admission,
            pending_owner_binding_sha256=pending_owner_binding,
        )
        seal = _ContextSeal(
            namespace_fingerprint=self._namespace_fingerprint,
            component_ids=tuple(id(item) for item in components),
            pending_owner_binding_sha256=pending_owner_binding,
            payload_sha256=_sha256(payload),
        )
        return AuthenticatedTurnContext(
            identity=identity,
            authority=authority,
            model_input=model_input,
            authorized_sources=authorized_sources,
            turn_policy=turn_policy,
            inherited_budget=inherited_budget,
            effect_fence=effect_fence,
            pending_work_admission=pending_work_admission,
            _seal=seal,
        )

    def _require_authority(self, authority: AuthenticatedIngressAuthority) -> None:
        if (
            type(authority) is not AuthenticatedIngressAuthority
            or authority._seal.namespace_fingerprint != self._namespace_fingerprint
        ):
            raise TurnContextError("ingress authority belongs to another issuer")

    def _seal_source(
        self,
        kind: AuthorizedSourceKind,
        identity_sha256: str,
    ) -> AuthorizedSourceIdentity:
        payload = {
            "schema": AUTHORIZED_SOURCE_IDENTITY_SCHEMA,
            "kind": kind.value,
            "identity_sha256": identity_sha256,
        }
        return AuthorizedSourceIdentity(
            kind=kind,
            identity_sha256=identity_sha256,
            _seal=_IssuedSeal(
                namespace_fingerprint=self._namespace_fingerprint,
                payload_sha256=_sha256(payload),
            ),
        )

    def _require_sources(self, sources: tuple[AuthorizedSourceIdentity, ...]) -> None:
        if type(sources) is not tuple:
            raise TurnContextError("authorized sources must be an exact tuple")
        for source in sources:
            if (
                type(source) is not AuthorizedSourceIdentity
                or source._seal.namespace_fingerprint != self._namespace_fingerprint
            ):
                raise TurnContextError("authorized source belongs to another issuer")

    def _validate_pending_scope(
        self,
        authority: AuthenticatedIngressAuthority,
        admission: PendingDurableTurnAdmission | None,
    ) -> None:
        _revalidate_pending(admission)
        if admission is None:
            return
        person_binding = _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/person/v1\0",
            _opaque_id(admission.person_id, label="pending person identity"),
        )
        conversation_binding = _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/conversation/v1\0",
            {
                "present": True,
                "identity": _opaque_id(admission.conversation_id, label="pending conversation identity"),
            },
        )
        if (
            authority.conversation_scope is not ConversationScopeKind.EXISTING
            or not hmac.compare_digest(person_binding, authority.person_binding_sha256)
            or not hmac.compare_digest(conversation_binding, authority.conversation_binding_sha256)
        ):
            raise TurnContextError("pending work admission belongs to another turn scope")

    def _pending_owner_binding(
        self,
        admission: PendingDurableTurnAdmission | None,
    ) -> str | None:
        if admission is None or not admission.is_bound:
            return None
        identifier = admission.binding_id
        if identifier is None:  # pragma: no cover - guarded by is_bound
            return None
        return _keyed_binding(
            self._namespace_key,
            b"friday/turn-context/pending-owner/v1\0",
            {
                "kind": "work_item" if admission.work_item_id is not None else "work_graph",
                "identity": identifier,
                "revision": admission.revision,
            },
        )


__all__ = [
    "ADVISORY_TURN_PROJECTION_SCHEMA",
    "AUTHENTICATED_INGRESS_AUTHORITY_SCHEMA",
    "AUTHENTICATED_TURN_CONTEXT_SCHEMA",
    "AUTHORIZED_SOURCE_IDENTITY_SCHEMA",
    "EFFECT_FENCE_SCHEMA",
    "INHERITED_TURN_BUDGET_SCHEMA",
    "TURN_IDENTITY_SCHEMA",
    "TURN_POLICY_SCHEMA",
    "AuthenticatedIngressAuthority",
    "AuthenticatedTurnContext",
    "AuthorizedSourceIdentity",
    "AuthorizedSourceKind",
    "ConversationScopeKind",
    "EffectFence",
    "EffectOwner",
    "FinalPublisher",
    "IngressKind",
    "InheritedTurnBudget",
    "ModelAntiLoopBudget",
    "TurnContextError",
    "TurnContextIssuer",
    "TurnIdentity",
    "TurnPolicy",
    "TurnResourceBudget",
    "TurnSafetyDeadline",
]
