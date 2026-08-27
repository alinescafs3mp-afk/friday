"""Dormant GPT-OSS transport for one symbolic supervisor effect intent.

The transport has no execution, permission, effect, storage, or publication
surface.  It asks the separately admitted optional scheduler for exactly one
discarded ``EFFECT_PLANNING`` result and returns only an untrusted
``EffectIntentV1``.  The caller must still pass that value through the separate
code-owned binding and gate before it can influence any later control flow.

The scheduler's admitted profile, served alias, and process-epoch probe are
snapshotted as a private lease and checked before dispatch and after the
response.  The lease is deliberately not exported and never enters the model
messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Never, Protocol

from friday import semantic_supervisor_policy
from friday.model_input_hygiene import secondary_model_messages_are_secret_free
from friday.orchestration.effect_outcome import EffectAction, EffectCapability
from friday.orchestration.supervisor_effect_intent import (
    SUPERVISOR_EFFECT_INTENT_POLICY,
    SUPERVISOR_EFFECT_INTENT_SCHEMA,
    SUPERVISOR_EFFECT_INTENT_SELECTION_POLICY,
    SUPERVISOR_EFFECT_INTENT_SELECTION_SCHEMA,
    SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
    EffectIntentActionSelection,
    EffectIntentCapabilitySelection,
    EffectIntentError,
    EffectIntentProjectionV2,
    EffectIntentReason,
    EffectIntentSelectionV2,
    EffectIntentV1,
    supervisor_effect_symbol_manifest_v2,
)
from friday.secondary_brain import (
    EffectClass,
    ModelModality,
    ModelPriority,
    ModelRequest,
    ModelWorkload,
    SecondaryAttempt,
    SecondaryResult,
)
from friday.secondary_brain.profiles import get_secondary_runtime_profile

SUPERVISOR_EFFECT_INTENT_INPUT_SCHEMA = "friday.supervisor-effect-intent-input.v1"
SUPERVISOR_EFFECT_INTENT_SELECTION_INPUT_SCHEMA = "friday.supervisor-effect-intent-selection-input.v2"

_MAX_INPUT_UTF8_BYTES = 3_328
_MAX_OUTPUT_TOKENS = semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_OUTPUT_TOKENS
_ACCEPTED_PROFILE_ADMISSION = "accepted"
_ACCEPTED_RUNTIME_MODES = frozenset({"assist", "shadow"})
_SYSTEM_PROMPT = """\
Return exactly one JSON object matching response_schema and no prose. Repeat
the code-owned capability, action, manifest digest, and proposal digest exactly
as supplied. Select only one declared reason. These symbols are advisory data:
they grant no permission or authority and must not be widened or reinterpreted.
"""
_SELECTION_SYSTEM_PROMPT = """\
Return exactly one JSON object matching response_schema and no prose. Choose
exactly one compatible capability/action pair from symbolic_manifest based only
on semantic_projection. The none/none pair means no supported effect intent.
Repeat both supplied digests exactly. Never invent arguments, paths, bodies,
identifiers, permission, confirmation, execution, or publication data.
"""


class SupervisorEffectIntentTransportFailure(StrEnum):
    INVALID_REQUEST = "invalid_request"
    DEADLINE_EXPIRED = "deadline_expired"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    RUNTIME_STALE = "runtime_stale"
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_RESPONSE = "invalid_response"


class SupervisorEffectIntentTransportError(RuntimeError):
    """A body-free closed failure from the dormant transport."""

    def __init__(self, failure: SupervisorEffectIntentTransportFailure) -> None:
        if not isinstance(failure, SupervisorEffectIntentTransportFailure):
            raise TypeError("effect intent transport failure must be closed")
        self.failure = failure
        super().__init__(failure.value)


class _EffectIntentScheduler(Protocol):
    """Only the existing accepted scheduler surface needed by this transport."""

    @property
    def served_model_alias(self) -> str: ...

    def product_attestation_identity(self) -> Mapping[str, object]: ...

    def diagnostics_status(self) -> Mapping[str, object]: ...

    async def evaluate_shadow(
        self,
        request: ModelRequest,
        *,
        validator: Callable[[SecondaryResult], bool] | None = None,
        invalidate_on_rejection: bool = True,
        pre_dispatch_validator: Callable[[], bool] | None = None,
        dispatch_observer: Callable[[], None] | None = None,
    ) -> SecondaryAttempt: ...


@dataclass(frozen=True, slots=True)
class _RuntimeLease:
    """Process-local identity of one already admitted secondary epoch."""

    runtime_object_id: int
    profile_id: str
    profile_manifest_sha256: str
    served_model_alias: str
    runtime_mode: str
    effect_shadow_requested_mode: str
    effect_shadow_policy_id: str
    effect_shadow_policy_sha256: str
    process_epoch_probe_serial: int
    inventory_probe_serial: int

    def canonical_sha256(self) -> str:
        payload = {
            "inventory_probe_serial": self.inventory_probe_serial,
            "process_epoch_probe_serial": self.process_epoch_probe_serial,
            "profile_id": self.profile_id,
            "profile_manifest_sha256": self.profile_manifest_sha256,
            "runtime_mode": self.runtime_mode,
            "served_model_alias": self.served_model_alias,
            "effect_shadow_policy_id": self.effect_shadow_policy_id,
            "effect_shadow_policy_sha256": self.effect_shadow_policy_sha256,
            "effect_shadow_requested_mode": self.effect_shadow_requested_mode,
        }
        return hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()


def _fail(failure: SupervisorEffectIntentTransportFailure) -> Never:
    raise SupervisorEffectIntentTransportError(failure)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _projection_is_current(projection: object) -> bool:
    if type(projection) is not EffectIntentProjectionV2:
        return False
    try:
        EffectIntentProjectionV2(
            semantic_text=projection.semantic_text,
            projection_digest=projection.projection_digest,
        )
    except (AttributeError, EffectIntentError, TypeError, UnicodeError, ValueError):
        return False
    return True


def _requested_intent(
    *,
    capability: EffectCapability,
    action: EffectAction,
    manifest_digest: str,
    proposal_digest: str,
) -> EffectIntentV1:
    if type(capability) is not EffectCapability or type(action) is not EffectAction:
        _fail(SupervisorEffectIntentTransportFailure.INVALID_REQUEST)
    try:
        return EffectIntentV1(
            capability=capability,
            action=action,
            manifest_digest=manifest_digest,
            proposal_digest=proposal_digest,
            reason=EffectIntentReason.EXPLICIT_USER_REQUEST,
        )
    except (EffectIntentError, TypeError, ValueError, UnicodeError):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_REQUEST)


def _response_schema(requested: EffectIntentV1) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "capability",
            "action",
            "manifest_digest",
            "proposal_digest",
            "reason",
        ],
        "properties": {
            "schema": {"type": "string", "enum": [SUPERVISOR_EFFECT_INTENT_SCHEMA]},
            "capability": {"type": "string", "enum": [requested.capability.value]},
            "action": {"type": "string", "enum": [requested.action.value]},
            "manifest_digest": {"type": "string", "enum": [requested.manifest_digest]},
            "proposal_digest": {"type": "string", "enum": [requested.proposal_digest]},
            "reason": {
                "type": "string",
                "enum": [item.value for item in EffectIntentReason],
            },
        },
    }


def supervisor_effect_intent_messages(
    *,
    capability: EffectCapability,
    action: EffectAction,
    manifest_digest: str,
    proposal_digest: str,
) -> tuple[dict[str, str], ...]:
    """Build the complete body-free model projection for one fixed request."""

    requested = _requested_intent(
        capability=capability,
        action=action,
        manifest_digest=manifest_digest,
        proposal_digest=proposal_digest,
    )
    payload = {
        "schema": SUPERVISOR_EFFECT_INTENT_INPUT_SCHEMA,
        "policy_id": SUPERVISOR_EFFECT_INTENT_POLICY,
        "requested_intent": {
            "capability": requested.capability.value,
            "action": requested.action.value,
            "manifest_digest": requested.manifest_digest,
            "proposal_digest": requested.proposal_digest,
        },
        "response_schema": _response_schema(requested),
    }
    messages = (
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _canonical_json(payload)},
    )
    try:
        size = sum(len(item["content"].encode("utf-8", errors="strict")) for item in messages)
    except UnicodeError:
        _fail(SupervisorEffectIntentTransportFailure.INVALID_REQUEST)
    if size > _MAX_INPUT_UTF8_BYTES or not secondary_model_messages_are_secret_free(messages):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_REQUEST)
    return messages


def _selection_response_schema(projection: EffectIntentProjectionV2) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "schema",
            "capability",
            "action",
            "manifest_digest",
            "projection_digest",
        ],
        "properties": {
            "schema": {
                "type": "string",
                "enum": [SUPERVISOR_EFFECT_INTENT_SELECTION_SCHEMA],
            },
            "capability": {
                "type": "string",
                "enum": [item.value for item in EffectIntentCapabilitySelection],
            },
            "action": {
                "type": "string",
                "enum": [item.value for item in EffectIntentActionSelection],
            },
            "manifest_digest": {
                "type": "string",
                "enum": [SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256],
            },
            "projection_digest": {
                "type": "string",
                "enum": [projection.projection_digest],
            },
        },
    }


def supervisor_effect_intent_selection_messages(
    *,
    projection: EffectIntentProjectionV2,
) -> tuple[dict[str, str], ...]:
    """Build the bounded projection from which the model chooses v2 symbols."""

    if not _projection_is_current(projection):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_REQUEST)
    payload = {
        "schema": SUPERVISOR_EFFECT_INTENT_SELECTION_INPUT_SCHEMA,
        "policy_id": SUPERVISOR_EFFECT_INTENT_SELECTION_POLICY,
        "symbolic_manifest": supervisor_effect_symbol_manifest_v2(),
        "manifest_digest": SUPERVISOR_EFFECT_SYMBOL_MANIFEST_SHA256,
        "semantic_projection": projection.to_payload(),
        "response_schema": _selection_response_schema(projection),
    }
    messages = (
        {"role": "system", "content": _SELECTION_SYSTEM_PROMPT},
        {"role": "user", "content": _canonical_json(payload)},
    )
    try:
        size = sum(len(item["content"].encode("utf-8", errors="strict")) for item in messages)
    except UnicodeError:
        _fail(SupervisorEffectIntentTransportFailure.INVALID_REQUEST)
    if size > _MAX_INPUT_UTF8_BYTES or not secondary_model_messages_are_secret_free(messages):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_REQUEST)
    return messages


def build_supervisor_effect_intent_selection_request(
    *,
    projection: EffectIntentProjectionV2,
    absolute_deadline_monotonic: float,
) -> ModelRequest:
    """Build one v2 symbolic-selection request with no effect surface."""

    if (
        isinstance(absolute_deadline_monotonic, bool)
        or not isinstance(absolute_deadline_monotonic, int | float)
        or not math.isfinite(float(absolute_deadline_monotonic))
        or not _projection_is_current(projection)
    ):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_REQUEST)
    return ModelRequest(
        workload=ModelWorkload.EFFECT_PLANNING,
        messages=supervisor_effect_intent_selection_messages(projection=projection),
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        absolute_deadline_monotonic=float(absolute_deadline_monotonic),
        priority=ModelPriority.BACKGROUND,
        effect_class=EffectClass.NONE,
        modality=ModelModality.TEXT,
        require_structured_output=True,
        structured_output_schema=_selection_response_schema(projection),
        require_independent_model=True,
        contains_private_text=True,
    )


def build_supervisor_effect_intent_request(
    *,
    capability: EffectCapability,
    action: EffectAction,
    manifest_digest: str,
    proposal_digest: str,
    absolute_deadline_monotonic: float,
) -> ModelRequest:
    """Build one effect-free structured request; this function never dispatches."""

    if (
        isinstance(absolute_deadline_monotonic, bool)
        or not isinstance(absolute_deadline_monotonic, int | float)
        or not math.isfinite(float(absolute_deadline_monotonic))
    ):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_REQUEST)
    requested = _requested_intent(
        capability=capability,
        action=action,
        manifest_digest=manifest_digest,
        proposal_digest=proposal_digest,
    )
    return ModelRequest(
        workload=ModelWorkload.EFFECT_PLANNING,
        messages=supervisor_effect_intent_messages(
            capability=capability,
            action=action,
            manifest_digest=manifest_digest,
            proposal_digest=proposal_digest,
        ),
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        absolute_deadline_monotonic=float(absolute_deadline_monotonic),
        priority=ModelPriority.BACKGROUND,
        effect_class=EffectClass.NONE,
        modality=ModelModality.TEXT,
        require_structured_output=True,
        structured_output_schema=_response_schema(requested),
        require_independent_model=True,
        contains_private_text=True,
    )


def parse_supervisor_effect_intent_result(
    result: SecondaryResult,
    *,
    capability: EffectCapability,
    action: EffectAction,
    manifest_digest: str,
    proposal_digest: str,
) -> EffectIntentV1:
    """Require exact raw/structured parity and the code-requested symbols."""

    requested = _requested_intent(
        capability=capability,
        action=action,
        manifest_digest=manifest_digest,
        proposal_digest=proposal_digest,
    )
    profile = get_secondary_runtime_profile(semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID)
    if type(result) is not SecondaryResult or profile is None:
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    structured_output = result.structured_output
    if (
        result.served_model_alias != profile.served_model_alias
        or result.endpoint_role != "secondary"
        or not isinstance(structured_output, Mapping)
    ):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    try:
        visible = EffectIntentV1.parse(result.visible_content)
        structured = EffectIntentV1.parse(structured_output)
    except (EffectIntentError, TypeError, ValueError, UnicodeError):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    if not hmac.compare_digest(visible.canonical_sha256(), structured.canonical_sha256()):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    if (
        visible.capability is not requested.capability
        or visible.action is not requested.action
        or not hmac.compare_digest(visible.manifest_digest, requested.manifest_digest)
        or not hmac.compare_digest(visible.proposal_digest, requested.proposal_digest)
    ):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    return visible


def parse_supervisor_effect_intent_selection_result(
    result: SecondaryResult,
    *,
    projection: EffectIntentProjectionV2,
) -> EffectIntentSelectionV2:
    """Accept only exact raw/structured parity bound to the v2 projection."""

    profile = get_secondary_runtime_profile(semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID)
    if type(result) is not SecondaryResult or not _projection_is_current(projection):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    if profile is None or result.served_model_alias != profile.served_model_alias:
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    structured_output = result.structured_output
    if result.endpoint_role != "secondary" or not isinstance(structured_output, Mapping):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    try:
        visible = EffectIntentSelectionV2.parse(result.visible_content)
        structured = EffectIntentSelectionV2.parse(structured_output)
    except (EffectIntentError, TypeError, ValueError, UnicodeError):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    if not hmac.compare_digest(visible.canonical_sha256(), structured.canonical_sha256()):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    if not hmac.compare_digest(visible.projection_digest, projection.projection_digest):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    return visible


def _runtime_lease(runtime: _EffectIntentScheduler) -> _RuntimeLease:
    profile = get_secondary_runtime_profile(semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID)
    if (
        profile is None
        or profile.manifest_sha256 != semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
    ):
        _fail(SupervisorEffectIntentTransportFailure.RUNTIME_UNAVAILABLE)
    try:
        alias = runtime.served_model_alias
        identity = runtime.product_attestation_identity()
        diagnostics = runtime.diagnostics_status()
    except Exception:
        _fail(SupervisorEffectIntentTransportFailure.RUNTIME_UNAVAILABLE)
    if not isinstance(identity, Mapping) or not isinstance(diagnostics, Mapping):
        _fail(SupervisorEffectIntentTransportFailure.RUNTIME_UNAVAILABLE)
    effect_shadow = diagnostics.get("effect_shadow")
    if not isinstance(effect_shadow, Mapping):
        _fail(SupervisorEffectIntentTransportFailure.RUNTIME_UNAVAILABLE)
    process_epoch_probe_serial = diagnostics.get("probe_success_total")
    inventory_probe_serial = diagnostics.get("model_inventory_probe_success_total")
    runtime_mode = identity.get("candidate_profile_mode")
    requested_mode = effect_shadow.get("requested_mode")
    if requested_mode != "shadow":
        _fail(SupervisorEffectIntentTransportFailure.RUNTIME_UNAVAILABLE)
    if (
        alias != profile.served_model_alias
        or identity.get("candidate_profile_id") != profile.profile_id
        or identity.get("candidate_profile_manifest_sha256") != profile.manifest_sha256
        or identity.get("candidate_profile_admission") != _ACCEPTED_PROFILE_ADMISSION
        or identity.get("served_model_alias") != profile.served_model_alias
        or identity.get("gateway_ca_certificate_sha256") != profile.gateway_ca_certificate_sha256
        or identity.get("candidate_profile_context_tokens") != profile.max_context_tokens
        or identity.get("candidate_profile_allow_private_text") is not True
        or type(runtime_mode) is not str
        or runtime_mode not in _ACCEPTED_RUNTIME_MODES
        or diagnostics.get("mode") != runtime_mode
        or diagnostics.get("state") != "healthy"
        or diagnostics.get("available") is not True
        or diagnostics.get("served_model_match") is not True
        or diagnostics.get("profile") != profile.profile_id
        or diagnostics.get("profile_admission") != _ACCEPTED_PROFILE_ADMISSION
        or diagnostics.get("profile_manifest_match") is not True
        or effect_shadow.get("workload") != ModelWorkload.EFFECT_PLANNING.value
        or effect_shadow.get("effective_mode") != "shadow"
        or effect_shadow.get("policy_id") != semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID
        or effect_shadow.get("policy_sha256")
        != semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256
        or effect_shadow.get("workload_available") is not True
        or effect_shadow.get("runtime_available") is not True
        or effect_shadow.get("closed_reason") != "admitted"
        or type(process_epoch_probe_serial) is not int
        or process_epoch_probe_serial < 1
        or type(inventory_probe_serial) is not int
        or inventory_probe_serial < 1
    ):
        _fail(SupervisorEffectIntentTransportFailure.RUNTIME_UNAVAILABLE)
    return _RuntimeLease(
        runtime_object_id=id(runtime),
        profile_id=profile.profile_id,
        profile_manifest_sha256=profile.manifest_sha256,
        served_model_alias=profile.served_model_alias,
        runtime_mode=runtime_mode,
        effect_shadow_requested_mode=requested_mode,
        effect_shadow_policy_id=semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_ID,
        effect_shadow_policy_sha256=(semantic_supervisor_policy.SUPERVISOR_EFFECT_SHADOW_POLICY_SHA256),
        process_epoch_probe_serial=process_epoch_probe_serial,
        inventory_probe_serial=inventory_probe_serial,
    )


def _lease_is_current(runtime: _EffectIntentScheduler, lease: _RuntimeLease) -> bool:
    try:
        current = _runtime_lease(runtime)
    except SupervisorEffectIntentTransportError:
        return False
    return bool(
        id(runtime) == lease.runtime_object_id
        and current.runtime_object_id == lease.runtime_object_id
        and hmac.compare_digest(current.canonical_sha256(), lease.canonical_sha256())
    )


def _deadline_is_open(deadline: float) -> bool:
    return time.monotonic() < deadline


async def describe_supervisor_effect_intent(
    runtime: _EffectIntentScheduler,
    *,
    capability: EffectCapability,
    action: EffectAction,
    manifest_digest: str,
    proposal_digest: str,
    absolute_deadline_monotonic: float,
) -> EffectIntentV1:
    """Make at most one bounded GPT-OSS call and return its untrusted intent."""

    request = build_supervisor_effect_intent_request(
        capability=capability,
        action=action,
        manifest_digest=manifest_digest,
        proposal_digest=proposal_digest,
        absolute_deadline_monotonic=absolute_deadline_monotonic,
    )
    deadline = request.absolute_deadline_monotonic
    if not _deadline_is_open(deadline):
        _fail(SupervisorEffectIntentTransportFailure.DEADLINE_EXPIRED)
    lease = _runtime_lease(runtime)
    if not _deadline_is_open(deadline):
        _fail(SupervisorEffectIntentTransportFailure.DEADLINE_EXPIRED)
    if not _lease_is_current(runtime, lease):
        _fail(SupervisorEffectIntentTransportFailure.RUNTIME_STALE)

    captured: EffectIntentV1 | None = None
    validator_rejected = False
    dispatches = 0

    def pre_dispatch() -> bool:
        return _deadline_is_open(deadline) and _lease_is_current(runtime, lease)

    def dispatched() -> None:
        nonlocal dispatches
        dispatches += 1

    def validator(result: SecondaryResult) -> bool:
        nonlocal captured, validator_rejected
        try:
            captured = parse_supervisor_effect_intent_result(
                result,
                capability=capability,
                action=action,
                manifest_digest=manifest_digest,
                proposal_digest=proposal_digest,
            )
        except SupervisorEffectIntentTransportError:
            validator_rejected = True
            return False
        return True

    try:
        async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
            attempt = await runtime.evaluate_shadow(
                request,
                validator=validator,
                invalidate_on_rejection=False,
                pre_dispatch_validator=pre_dispatch,
                dispatch_observer=dispatched,
            )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        _fail(SupervisorEffectIntentTransportFailure.DEADLINE_EXPIRED)
    except Exception:
        _fail(SupervisorEffectIntentTransportFailure.MODEL_UNAVAILABLE)

    if not _deadline_is_open(deadline):
        _fail(SupervisorEffectIntentTransportFailure.DEADLINE_EXPIRED)
    if not _lease_is_current(runtime, lease):
        _fail(SupervisorEffectIntentTransportFailure.RUNTIME_STALE)
    if validator_rejected:
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    if type(attempt) is not SecondaryAttempt or not attempt.succeeded:
        _fail(SupervisorEffectIntentTransportFailure.MODEL_UNAVAILABLE)
    result = attempt.result
    if type(result) is not SecondaryResult:
        _fail(SupervisorEffectIntentTransportFailure.MODEL_UNAVAILABLE)
    accepted = captured
    if dispatches != 1 or accepted is None:
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    parsed = parse_supervisor_effect_intent_result(
        result,
        capability=capability,
        action=action,
        manifest_digest=manifest_digest,
        proposal_digest=proposal_digest,
    )
    if not hmac.compare_digest(parsed.canonical_sha256(), accepted.canonical_sha256()):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    return parsed


async def select_supervisor_effect_intent(
    runtime: _EffectIntentScheduler,
    *,
    projection: EffectIntentProjectionV2,
    absolute_deadline_monotonic: float,
) -> EffectIntentSelectionV2:
    """Make at most one effect-free call for one untrusted symbolic choice."""

    request = build_supervisor_effect_intent_selection_request(
        projection=projection,
        absolute_deadline_monotonic=absolute_deadline_monotonic,
    )
    deadline = request.absolute_deadline_monotonic
    if not _deadline_is_open(deadline):
        _fail(SupervisorEffectIntentTransportFailure.DEADLINE_EXPIRED)
    lease = _runtime_lease(runtime)
    if not _deadline_is_open(deadline):
        _fail(SupervisorEffectIntentTransportFailure.DEADLINE_EXPIRED)
    if not _lease_is_current(runtime, lease):
        _fail(SupervisorEffectIntentTransportFailure.RUNTIME_STALE)

    captured: EffectIntentSelectionV2 | None = None
    validator_rejected = False
    dispatches = 0

    def pre_dispatch() -> bool:
        return _deadline_is_open(deadline) and _lease_is_current(runtime, lease)

    def dispatched() -> None:
        nonlocal dispatches
        dispatches += 1

    def validator(result: SecondaryResult) -> bool:
        nonlocal captured, validator_rejected
        try:
            captured = parse_supervisor_effect_intent_selection_result(
                result,
                projection=projection,
            )
        except SupervisorEffectIntentTransportError:
            validator_rejected = True
            return False
        return True

    try:
        async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
            attempt = await runtime.evaluate_shadow(
                request,
                validator=validator,
                invalidate_on_rejection=False,
                pre_dispatch_validator=pre_dispatch,
                dispatch_observer=dispatched,
            )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        _fail(SupervisorEffectIntentTransportFailure.DEADLINE_EXPIRED)
    except Exception:
        _fail(SupervisorEffectIntentTransportFailure.MODEL_UNAVAILABLE)

    if not _deadline_is_open(deadline):
        _fail(SupervisorEffectIntentTransportFailure.DEADLINE_EXPIRED)
    if not _lease_is_current(runtime, lease):
        _fail(SupervisorEffectIntentTransportFailure.RUNTIME_STALE)
    if validator_rejected:
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    if type(attempt) is not SecondaryAttempt or not attempt.succeeded:
        _fail(SupervisorEffectIntentTransportFailure.MODEL_UNAVAILABLE)
    result = attempt.result
    if type(result) is not SecondaryResult:
        _fail(SupervisorEffectIntentTransportFailure.MODEL_UNAVAILABLE)
    accepted = captured
    if dispatches != 1 or accepted is None:
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    parsed = parse_supervisor_effect_intent_selection_result(result, projection=projection)
    if not hmac.compare_digest(parsed.canonical_sha256(), accepted.canonical_sha256()):
        _fail(SupervisorEffectIntentTransportFailure.INVALID_RESPONSE)
    return parsed


__all__ = [
    "SUPERVISOR_EFFECT_INTENT_INPUT_SCHEMA",
    "SUPERVISOR_EFFECT_INTENT_SELECTION_INPUT_SCHEMA",
    "SupervisorEffectIntentTransportError",
    "SupervisorEffectIntentTransportFailure",
    "build_supervisor_effect_intent_request",
    "build_supervisor_effect_intent_selection_request",
    "describe_supervisor_effect_intent",
    "select_supervisor_effect_intent",
    "parse_supervisor_effect_intent_result",
    "parse_supervisor_effect_intent_selection_result",
    "supervisor_effect_intent_messages",
    "supervisor_effect_intent_selection_messages",
]
