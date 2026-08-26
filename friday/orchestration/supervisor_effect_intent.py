"""Dormant, non-authorizing supervisor effect intent for one proven contour.

The model may name an Obsidian note create/append and bind that suggestion to
the current manifest and proposal.  It cannot supply effect arguments or any
authority-bearing field.  A process-owned binding records the real tool,
permission, effect, identity, and lifecycle facts.  The pure gate below only
returns an advisory value: it never exposes an execution handle and can never
authorize execution or publication.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from friday.orchestration.effect_outcome import EffectAction, EffectCapability
from friday.orchestration.supervisor_contracts import CapabilityEffectClass

SUPERVISOR_EFFECT_INTENT_SCHEMA = "friday.supervisor-effect-intent.v1"
SUPERVISOR_EFFECT_INTENT_POLICY = "semantic-supervisor-effect-intent-v1"

_MAX_MODEL_JSON_BYTES = 2_048
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_PROCESS_AUTHORITY = object()
_PROCESS_SEAL_KEY = secrets.token_bytes(32)
_OBSIDIAN_SECURITY_ID = "obsidian.write"
_OBSIDIAN_TOOL_RISK = "mutate"
_CONTOUR_TO_TOOL = {
    (EffectCapability.OBSIDIAN_NOTE_MUTATION, EffectAction.CREATE): "obsidian_create_note",
    (EffectCapability.OBSIDIAN_NOTE_MUTATION, EffectAction.APPEND): "obsidian_append_note",
}


class EffectIntentError(ValueError):
    """A value is outside the closed supervisor effect-intent contract."""


class EffectIntentReason(StrEnum):
    """Advisory model reasons; neither value conveys authority."""

    EXPLICIT_USER_REQUEST = "explicit_user_request"
    DECLARED_PLAN_EFFECT = "declared_plan_effect"


class EffectLifecycle(StrEnum):
    """Fresh effect state observed by the code-owned gate caller."""

    NOT_STARTED = "not_started"
    STARTED = "started"
    UNCERTAIN = "uncertain"


class EffectIntentGateReason(StrEnum):
    ADVISORY_BOUND = "advisory_bound"
    INVALID_BINDING = "invalid_binding"
    SYMBOLIC_DRIFT = "symbolic_drift"
    TOOL_CONTRACT_DRIFT = "tool_contract_drift"
    SECURITY_CONTRACT_DRIFT = "security_contract_drift"
    EFFECT_CONTRACT_DRIFT = "effect_contract_drift"
    MANIFEST_DRIFT = "manifest_drift"
    PROPOSAL_DRIFT = "proposal_drift"
    ACTOR_DRIFT = "actor_drift"
    CONVERSATION_DRIFT = "conversation_drift"
    REQUEST_DRIFT = "request_drift"
    SOURCE_REVISION_DRIFT = "source_revision_drift"
    AUTHORIZATION_DRIFT = "authorization_drift"
    PERMISSION_DENIED = "permission_denied"
    SOURCE_NOT_AUTHORIZED = "source_not_authorized"
    IDEMPOTENCY_DRIFT = "idempotency_drift"
    REGISTRY_DRIFT = "registry_drift"
    POLICY_DRIFT = "policy_drift"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_DRIFT = "confirmation_drift"
    EFFECT_ALREADY_STARTED = "effect_already_started"
    OUTCOME_UNCERTAIN = "outcome_uncertain"


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise EffectIntentError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EffectIntentError("effect intent contains a duplicate object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise EffectIntentError(f"effect intent contains unsupported JSON constant {value}")


def _canonical_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class EffectIntentV1:
    """The complete model-visible contract: symbols, bindings, and a reason."""

    capability: EffectCapability
    action: EffectAction
    manifest_digest: str
    proposal_digest: str
    reason: EffectIntentReason

    def __post_init__(self) -> None:
        if self.capability is not EffectCapability.OBSIDIAN_NOTE_MUTATION:
            raise EffectIntentError("effect capability is unavailable")
        if self.action not in {EffectAction.CREATE, EffectAction.APPEND}:
            raise EffectIntentError("effect action is unavailable")
        _require_digest(self.manifest_digest, label="manifest_digest")
        _require_digest(self.proposal_digest, label="proposal_digest")
        if not isinstance(self.reason, EffectIntentReason):
            raise EffectIntentError("effect intent reason is unavailable")

    def to_payload(self) -> dict[str, str]:
        return {
            "schema": SUPERVISOR_EFFECT_INTENT_SCHEMA,
            "capability": self.capability.value,
            "action": self.action.value,
            "manifest_digest": self.manifest_digest,
            "proposal_digest": self.proposal_digest,
            "reason": self.reason.value,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_payload())

    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("ascii")).hexdigest()

    @classmethod
    def parse(cls, value: str | Mapping[str, object]) -> EffectIntentV1:
        if isinstance(value, str):
            try:
                encoded = value.encode("utf-8", errors="strict")
            except UnicodeEncodeError as exc:
                raise EffectIntentError("effect intent JSON must be valid UTF-8") from exc
            if len(encoded) > _MAX_MODEL_JSON_BYTES:
                raise EffectIntentError("effect intent JSON is too large")
            try:
                decoded = json.loads(
                    value,
                    object_pairs_hook=_closed_object,
                    parse_constant=_reject_constant,
                )
            except json.JSONDecodeError as exc:
                raise EffectIntentError("effect intent must be one JSON object") from exc
        else:
            decoded = value
        if not isinstance(decoded, Mapping):
            raise EffectIntentError("effect intent must be one JSON object")
        expected = {
            "schema",
            "capability",
            "action",
            "manifest_digest",
            "proposal_digest",
            "reason",
        }
        if any(type(key) is not str for key in decoded) or set(decoded) != expected:
            raise EffectIntentError("effect intent keys do not match the closed contract")
        if decoded["schema"] != SUPERVISOR_EFFECT_INTENT_SCHEMA:
            raise EffectIntentError("effect intent schema is not supported")
        try:
            capability = EffectCapability(decoded["capability"])
            action = EffectAction(decoded["action"])
            reason = EffectIntentReason(decoded["reason"])
        except (TypeError, ValueError) as exc:
            raise EffectIntentError("effect capability, action, or reason is unavailable") from exc
        return cls(
            capability=capability,
            action=action,
            manifest_digest=_require_digest(decoded["manifest_digest"], label="manifest_digest"),
            proposal_digest=_require_digest(decoded["proposal_digest"], label="proposal_digest"),
            reason=reason,
        )


def _binding_payload(
    *,
    capability: EffectCapability,
    action: EffectAction,
    tool_name: str,
    security_id: str,
    effect_class: CapabilityEffectClass,
    tool_risk: str,
    actor_binding_digest: str,
    conversation_binding_digest: str,
    request_digest: str,
    source_revision_digest: str,
    authorization_basis_digest: str,
    idempotency_key_digest: str,
    registry_digest: str,
    policy_digest: str,
    manifest_digest: str,
    proposal_digest: str,
    confirmation_digest: str,
    lifecycle: EffectLifecycle,
) -> dict[str, str]:
    return {
        "capability": capability.value,
        "action": action.value,
        "tool_name": tool_name,
        "security_id": security_id,
        "effect_class": effect_class.value,
        "tool_risk": tool_risk,
        "actor_binding_digest": actor_binding_digest,
        "conversation_binding_digest": conversation_binding_digest,
        "request_digest": request_digest,
        "source_revision_digest": source_revision_digest,
        "authorization_basis_digest": authorization_basis_digest,
        "idempotency_key_digest": idempotency_key_digest,
        "registry_digest": registry_digest,
        "policy_digest": policy_digest,
        "manifest_digest": manifest_digest,
        "proposal_digest": proposal_digest,
        "confirmation_digest": confirmation_digest,
        "lifecycle": lifecycle.value,
    }


def _binding_seal(payload: Mapping[str, object]) -> str:
    return hmac.new(
        _PROCESS_SEAL_KEY,
        _canonical_json(payload).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedEffectBinding:
    """Code-owned, process-sealed facts for the exact prospective effect."""

    capability: EffectCapability
    action: EffectAction
    tool_name: str
    security_id: str
    effect_class: CapabilityEffectClass
    tool_risk: str
    actor_binding_digest: str
    conversation_binding_digest: str
    request_digest: str
    source_revision_digest: str
    authorization_basis_digest: str
    idempotency_key_digest: str
    registry_digest: str
    policy_digest: str
    manifest_digest: str
    proposal_digest: str
    confirmation_digest: str
    lifecycle: EffectLifecycle
    _seal: str = field(repr=False, compare=False)
    _process_authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        payload = self._payload()
        expected_tool = _CONTOUR_TO_TOOL.get((self.capability, self.action))
        if (
            self._process_authority is not _PROCESS_AUTHORITY
            or expected_tool is None
            or self.tool_name != expected_tool
            or self.security_id != _OBSIDIAN_SECURITY_ID
            or self.effect_class is not CapabilityEffectClass.WRITE
            or self.tool_risk != _OBSIDIAN_TOOL_RISK
            or self.lifecycle is not EffectLifecycle.NOT_STARTED
            or type(self._seal) is not str
            or not hmac.compare_digest(self._seal, _binding_seal(payload))
        ):
            raise EffectIntentError("prepared effect binding is not process-owned")
        for label in (
            "actor_binding_digest",
            "conversation_binding_digest",
            "request_digest",
            "source_revision_digest",
            "authorization_basis_digest",
            "idempotency_key_digest",
            "registry_digest",
            "policy_digest",
            "manifest_digest",
            "proposal_digest",
            "confirmation_digest",
        ):
            _require_digest(getattr(self, label), label=label)

    def _payload(self) -> dict[str, str]:
        return _binding_payload(
            capability=self.capability,
            action=self.action,
            tool_name=self.tool_name,
            security_id=self.security_id,
            effect_class=self.effect_class,
            tool_risk=self.tool_risk,
            actor_binding_digest=self.actor_binding_digest,
            conversation_binding_digest=self.conversation_binding_digest,
            request_digest=self.request_digest,
            source_revision_digest=self.source_revision_digest,
            authorization_basis_digest=self.authorization_basis_digest,
            idempotency_key_digest=self.idempotency_key_digest,
            registry_digest=self.registry_digest,
            policy_digest=self.policy_digest,
            manifest_digest=self.manifest_digest,
            proposal_digest=self.proposal_digest,
            confirmation_digest=self.confirmation_digest,
            lifecycle=self.lifecycle,
        )

    def canonical_sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self._payload()).encode("ascii")).hexdigest()


def prepare_obsidian_effect_binding(
    *,
    capability: EffectCapability,
    action: EffectAction,
    resolved_tool_name: str,
    resolved_security_id: str,
    resolved_effect_class: CapabilityEffectClass,
    resolved_tool_risk: str,
    actor_binding_digest: str,
    conversation_binding_digest: str,
    request_digest: str,
    source_revision_digest: str,
    authorization_basis_digest: str,
    idempotency_key_digest: str,
    registry_digest: str,
    policy_digest: str,
    manifest_digest: str,
    proposal_digest: str,
    confirmation_digest: str,
) -> PreparedEffectBinding:
    """Seal a registry-resolved create/append contour; no effect is started."""

    expected_tool = _CONTOUR_TO_TOOL.get((capability, action))
    if (
        expected_tool is None
        or resolved_tool_name != expected_tool
        or resolved_security_id != _OBSIDIAN_SECURITY_ID
        or resolved_effect_class is not CapabilityEffectClass.WRITE
        or resolved_tool_risk != _OBSIDIAN_TOOL_RISK
    ):
        raise EffectIntentError("resolved effect contract is unavailable or has drifted")
    values = {
        "actor_binding_digest": actor_binding_digest,
        "conversation_binding_digest": conversation_binding_digest,
        "request_digest": request_digest,
        "source_revision_digest": source_revision_digest,
        "authorization_basis_digest": authorization_basis_digest,
        "idempotency_key_digest": idempotency_key_digest,
        "registry_digest": registry_digest,
        "policy_digest": policy_digest,
        "manifest_digest": manifest_digest,
        "proposal_digest": proposal_digest,
        "confirmation_digest": confirmation_digest,
    }
    for label, value in values.items():
        _require_digest(value, label=label)
    payload = _binding_payload(
        capability=capability,
        action=action,
        tool_name=resolved_tool_name,
        security_id=resolved_security_id,
        effect_class=resolved_effect_class,
        tool_risk=resolved_tool_risk,
        lifecycle=EffectLifecycle.NOT_STARTED,
        **values,
    )
    return PreparedEffectBinding(
        capability=capability,
        action=action,
        tool_name=resolved_tool_name,
        security_id=resolved_security_id,
        effect_class=resolved_effect_class,
        tool_risk=resolved_tool_risk,
        lifecycle=EffectLifecycle.NOT_STARTED,
        _seal=_binding_seal(payload),
        _process_authority=_PROCESS_AUTHORITY,
        **values,
    )


@dataclass(frozen=True, slots=True)
class FreshEffectGateState:
    """Caller-observed, body-free facts rechecked immediately before admission."""

    resolved_tool_name: str
    resolved_security_id: str
    resolved_effect_class: CapabilityEffectClass
    resolved_tool_risk: str
    actor_binding_digest: str
    conversation_binding_digest: str
    request_digest: str
    source_revision_digest: str
    authorization_basis_digest: str
    idempotency_key_digest: str
    registry_digest: str
    policy_digest: str
    manifest_digest: str
    proposal_digest: str
    permission_allowed: bool
    source_authorized: bool
    confirmation_present: bool
    confirmation_digest: str | None
    lifecycle: EffectLifecycle

    def __post_init__(self) -> None:
        for label in (
            "actor_binding_digest",
            "conversation_binding_digest",
            "request_digest",
            "source_revision_digest",
            "authorization_basis_digest",
            "idempotency_key_digest",
            "registry_digest",
            "policy_digest",
            "manifest_digest",
            "proposal_digest",
        ):
            _require_digest(getattr(self, label), label=label)
        if not isinstance(self.resolved_effect_class, CapabilityEffectClass):
            raise EffectIntentError("resolved effect class is invalid")
        if any(
            not isinstance(value, bool)
            for value in (
                self.permission_allowed,
                self.source_authorized,
                self.confirmation_present,
            )
        ):
            raise EffectIntentError("fresh permission, source, and confirmation facts must be booleans")
        if self.confirmation_digest is not None:
            _require_digest(self.confirmation_digest, label="confirmation_digest")
        if not isinstance(self.lifecycle, EffectLifecycle):
            raise EffectIntentError("fresh effect lifecycle is invalid")


@dataclass(frozen=True, slots=True)
class BoundAdvisoryEffectIntent:
    """Body-free advisory result.  This value is intentionally not a token."""

    intent_digest: str
    binding_digest: str
    capability: EffectCapability
    action: EffectAction
    reason: EffectIntentReason
    execution_authorized: bool = field(default=False, init=False)
    publication_authorized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        _require_digest(self.intent_digest, label="intent_digest")
        _require_digest(self.binding_digest, label="binding_digest")


@dataclass(frozen=True, slots=True)
class EffectIntentGateDecision:
    bound: bool
    reason: EffectIntentGateReason
    advisory: BoundAdvisoryEffectIntent | None = None
    execution_authorized: bool = field(default=False, init=False)
    publication_authorized: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.bound, bool) or not isinstance(self.reason, EffectIntentGateReason):
            raise EffectIntentError("effect intent decision is invalid")
        if self.bound is not (self.advisory is not None):
            raise EffectIntentError("bound effect decision and advisory must agree")

    @property
    def reason_code(self) -> str:
        return self.reason.value


def _reject(reason: EffectIntentGateReason) -> EffectIntentGateDecision:
    return EffectIntentGateDecision(bound=False, reason=reason)


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def _binding_is_current(binding: PreparedEffectBinding) -> bool:
    if type(binding) is not PreparedEffectBinding or binding._process_authority is not _PROCESS_AUTHORITY:
        return False
    try:
        expected = _binding_seal(binding._payload())
    except (AttributeError, EffectIntentError, TypeError, ValueError):
        return False
    return type(binding._seal) is str and hmac.compare_digest(binding._seal, expected)


def gate_supervisor_effect_intent(
    intent: EffectIntentV1,
    binding: PreparedEffectBinding,
    current: FreshEffectGateState,
) -> EffectIntentGateDecision:
    """Purely bind an advisory intent after exact freshness checks.

    No tool, kernel, callable, arguments, path, note body, outcome, or
    publication surface is accepted by this API.
    """

    if type(intent) is not EffectIntentV1 or type(current) is not FreshEffectGateState:
        raise TypeError("effect intent gate requires typed, body-free contracts")
    if not _binding_is_current(binding):
        return _reject(EffectIntentGateReason.INVALID_BINDING)
    if intent.capability is not binding.capability or intent.action is not binding.action:
        return _reject(EffectIntentGateReason.SYMBOLIC_DRIFT)
    expected_tool = _CONTOUR_TO_TOOL.get((intent.capability, intent.action))
    if expected_tool is None or current.resolved_tool_name != expected_tool:
        return _reject(EffectIntentGateReason.TOOL_CONTRACT_DRIFT)
    if current.resolved_security_id != _OBSIDIAN_SECURITY_ID:
        return _reject(EffectIntentGateReason.SECURITY_CONTRACT_DRIFT)
    if (
        current.resolved_effect_class is not CapabilityEffectClass.WRITE
        or current.resolved_tool_risk != _OBSIDIAN_TOOL_RISK
    ):
        return _reject(EffectIntentGateReason.EFFECT_CONTRACT_DRIFT)
    if not (
        _same(intent.manifest_digest, binding.manifest_digest)
        and _same(current.manifest_digest, binding.manifest_digest)
    ):
        return _reject(EffectIntentGateReason.MANIFEST_DRIFT)
    if not (
        _same(intent.proposal_digest, binding.proposal_digest)
        and _same(current.proposal_digest, binding.proposal_digest)
    ):
        return _reject(EffectIntentGateReason.PROPOSAL_DRIFT)
    for left, right, reason in (
        (current.actor_binding_digest, binding.actor_binding_digest, EffectIntentGateReason.ACTOR_DRIFT),
        (
            current.conversation_binding_digest,
            binding.conversation_binding_digest,
            EffectIntentGateReason.CONVERSATION_DRIFT,
        ),
        (current.request_digest, binding.request_digest, EffectIntentGateReason.REQUEST_DRIFT),
        (
            current.source_revision_digest,
            binding.source_revision_digest,
            EffectIntentGateReason.SOURCE_REVISION_DRIFT,
        ),
        (
            current.authorization_basis_digest,
            binding.authorization_basis_digest,
            EffectIntentGateReason.AUTHORIZATION_DRIFT,
        ),
        (
            current.idempotency_key_digest,
            binding.idempotency_key_digest,
            EffectIntentGateReason.IDEMPOTENCY_DRIFT,
        ),
        (current.registry_digest, binding.registry_digest, EffectIntentGateReason.REGISTRY_DRIFT),
        (current.policy_digest, binding.policy_digest, EffectIntentGateReason.POLICY_DRIFT),
    ):
        if not _same(left, right):
            return _reject(reason)
    if not current.permission_allowed:
        return _reject(EffectIntentGateReason.PERMISSION_DENIED)
    if not current.source_authorized:
        return _reject(EffectIntentGateReason.SOURCE_NOT_AUTHORIZED)
    if not current.confirmation_present or current.confirmation_digest is None:
        return _reject(EffectIntentGateReason.CONFIRMATION_REQUIRED)
    if not _same(current.confirmation_digest, binding.confirmation_digest):
        return _reject(EffectIntentGateReason.CONFIRMATION_DRIFT)
    if current.lifecycle is EffectLifecycle.UNCERTAIN:
        return _reject(EffectIntentGateReason.OUTCOME_UNCERTAIN)
    if current.lifecycle is not EffectLifecycle.NOT_STARTED:
        return _reject(EffectIntentGateReason.EFFECT_ALREADY_STARTED)
    advisory = BoundAdvisoryEffectIntent(
        intent_digest=intent.canonical_sha256(),
        binding_digest=binding.canonical_sha256(),
        capability=intent.capability,
        action=intent.action,
        reason=intent.reason,
    )
    return EffectIntentGateDecision(
        bound=True,
        reason=EffectIntentGateReason.ADVISORY_BOUND,
        advisory=advisory,
    )


__all__ = [
    "BoundAdvisoryEffectIntent",
    "EffectIntentError",
    "EffectIntentGateDecision",
    "EffectIntentGateReason",
    "EffectIntentReason",
    "EffectIntentV1",
    "EffectLifecycle",
    "FreshEffectGateState",
    "PreparedEffectBinding",
    "SUPERVISOR_EFFECT_INTENT_POLICY",
    "SUPERVISOR_EFFECT_INTENT_SCHEMA",
    "gate_supervisor_effect_intent",
    "prepare_obsidian_effect_binding",
]
