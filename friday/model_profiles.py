"""Code-owned model capability profiles for the V12 orchestration boundary.

Runtime configuration describes how a model is launched.  It is not authority
to let that model own a V12 turn.  This module keeps that second decision small
and process-local: a versioned profile states the maximum capabilities Friday is
willing to attest, and :class:`V12ModelGate` issues a least-privilege lease only
after a matching live attestation has been installed by trusted runtime code.

There is deliberately no environment or file loader here.  A setting can select
``shadow`` and a probe may measure the endpoint, but neither a setting nor a JSON
file can proclaim the endpoint safe for canary execution.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

V12_MODEL_PROFILE_SCHEMA = "friday.v12-model-profile.v1"
V12_MODEL_ATTESTATION_SCHEMA = "friday.v12-model-attestation.v1"
V12_MODEL_LEASE_SCHEMA = "friday.v12-model-lease.v1"
V12_TURN_PLAN_SCHEMA = "friday.turn-plan.v1"

_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _version_sha256(label: str, payload: Mapping[str, Any]) -> str:
    return _canonical_sha256({"label": label, "payload": dict(payload)})


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _valid_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class ModelCapability(StrEnum):
    """Capabilities that a live probe may grant; absence always means denied."""

    TURN_PLAN_V1 = "turn_plan_v1"
    RU_PLANNING = "ru_planning"
    PREPARED_EVIDENCE_2 = "prepared_evidence_2"
    CONTEXT_8K = "context_8k"
    REMOTE_CANCELLATION = "remote_cancellation"
    # Declared for future profiles, but intentionally absent from the current
    # profile's allowlist until their own live probes exist.
    RAW_VISION = "raw_vision"
    NATIVE_TOOL_CALLS = "native_tool_calls"


class ModelEffect(StrEnum):
    """Effect vocabulary kept independent of the orchestration package.

    ``model_profiles`` is a leaf policy module.  Importing
    ``friday.orchestration.contracts`` here would execute that package's
    ``__init__`` and create a cycle as soon as the router imports this gate.
    """

    READ = "read"
    WRITE = "write"
    HIGH = "high"


class ModelGateStatus(StrEnum):
    SHADOW_CANDIDATE = "shadow_candidate"
    CANARY_READY = "canary_ready"
    REVOKED = "revoked"


class ModelGateReason(StrEnum):
    AWAITING_LIVE_ATTESTATION = "awaiting_live_attestation"
    LIVE_ATTESTATION_CLEAR = "live_attestation_clear"
    ATTESTATION_REJECTED = "attestation_rejected"
    EPOCH_INVALID = "epoch_invalid"
    EPOCH_CHANGED = "epoch_changed"
    EXPLICIT_REVOCATION = "explicit_revocation"


@dataclass(frozen=True, slots=True)
class ModelRequirements:
    """Maximum model authority needed by one code-owned route handler."""

    capabilities: frozenset[ModelCapability]
    required_context_tokens: int = 0
    prepared_evidence_items: int = 0
    max_tool_steps: int = 0
    effect: ModelEffect = ModelEffect.READ
    verifier_required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.capabilities, frozenset) or any(
            not isinstance(item, ModelCapability) for item in self.capabilities
        ):
            raise ValueError("model capabilities must be an immutable capability set")
        if not _valid_nonnegative_int(self.required_context_tokens):
            raise ValueError("required context tokens must be a non-negative integer")
        if not _valid_nonnegative_int(self.prepared_evidence_items):
            raise ValueError("prepared evidence items must be a non-negative integer")
        if not _valid_nonnegative_int(self.max_tool_steps):
            raise ValueError("max tool steps must be a non-negative integer")
        if not isinstance(self.effect, ModelEffect):
            raise ValueError("model requirement effect must be a ModelEffect")
        if not isinstance(self.verifier_required, bool):
            raise ValueError("verifier_required must be boolean")

    def canonical_sha256(self) -> str:
        return _canonical_sha256(
            {
                "capabilities": sorted(item.value for item in self.capabilities),
                "effect": self.effect.value,
                "max_tool_steps": self.max_tool_steps,
                "prepared_evidence_items": self.prepared_evidence_items,
                "required_context_tokens": self.required_context_tokens,
                "verifier_required": self.verifier_required,
            }
        )


@dataclass(frozen=True, slots=True)
class V12ModelProfileSpec:
    """Immutable upper bound for one model/runtime pairing."""

    profile_id: str
    runtime_profile_name: str
    served_model_alias: str
    planner_contract_sha256: str
    probe_suite_sha256: str
    allowed_capabilities: frozenset[ModelCapability]
    required_capabilities: frozenset[ModelCapability]
    minimum_context_tokens: int
    max_context_tokens: int
    max_prepared_evidence_items: int
    max_tool_steps: int
    allowed_effects: frozenset[ModelEffect]
    verifier_required: bool

    def __post_init__(self) -> None:
        for label, value in (
            ("profile id", self.profile_id),
            ("runtime profile name", self.runtime_profile_name),
            ("served model alias", self.served_model_alias),
        ):
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ValueError(f"invalid V12 model {label}")
        if not _valid_sha256(self.planner_contract_sha256) or not _valid_sha256(self.probe_suite_sha256):
            raise ValueError("V12 model profile hashes must be lowercase SHA-256 values")
        if not isinstance(self.allowed_capabilities, frozenset) or any(
            not isinstance(item, ModelCapability) for item in self.allowed_capabilities
        ):
            raise ValueError("allowed capabilities must be an immutable capability set")
        if not isinstance(self.required_capabilities, frozenset) or any(
            not isinstance(item, ModelCapability) for item in self.required_capabilities
        ):
            raise ValueError("required capabilities must be an immutable capability set")
        if not self.required_capabilities <= self.allowed_capabilities:
            raise ValueError("required capabilities must be a subset of allowed capabilities")
        if not _valid_positive_int(self.minimum_context_tokens):
            raise ValueError("minimum context tokens must be a positive integer")
        if (
            not _valid_positive_int(self.max_context_tokens)
            or self.max_context_tokens < self.minimum_context_tokens
        ):
            raise ValueError("max context tokens must cover the minimum context tier")
        if not _valid_positive_int(self.max_prepared_evidence_items):
            raise ValueError("max prepared evidence items must be a positive integer")
        if not _valid_nonnegative_int(self.max_tool_steps):
            raise ValueError("max tool steps must be a non-negative integer")
        if not isinstance(self.allowed_effects, frozenset) or any(
            not isinstance(item, ModelEffect) for item in self.allowed_effects
        ):
            raise ValueError("allowed effects must be an immutable effect set")
        if not self.allowed_effects:
            raise ValueError("a V12 model profile must declare at least one effect class")
        if not isinstance(self.verifier_required, bool):
            raise ValueError("verifier_required must be boolean")


@dataclass(frozen=True, slots=True)
class V12LiveAttestation:
    """Sanitized, process-bound result produced by the future live probe.

    The endpoint binding and vLLM process epoch are intentionally hidden from
    repr/public status.  Raw prompts, responses, URLs and credentials do not
    belong in this object at all.
    """

    profile_id: str
    planner_contract_sha256: str
    probe_suite_sha256: str
    endpoint_binding_sha256: str = field(repr=False)
    process_epoch_sha256: str = field(repr=False)
    capabilities: frozenset[ModelCapability]
    verified_context_tokens: int
    max_prepared_evidence_items: int
    max_tool_steps: int
    allowed_effects: frozenset[ModelEffect]
    verifier_required: bool

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or _SAFE_ID.fullmatch(self.profile_id) is None:
            raise ValueError("invalid attested profile id")
        for value in (
            self.planner_contract_sha256,
            self.probe_suite_sha256,
            self.endpoint_binding_sha256,
            self.process_epoch_sha256,
        ):
            if not _valid_sha256(value):
                raise ValueError("attestation hashes must be lowercase SHA-256 values")
        if not isinstance(self.capabilities, frozenset) or any(
            not isinstance(item, ModelCapability) for item in self.capabilities
        ):
            raise ValueError("attested capabilities must be an immutable capability set")
        if not _valid_positive_int(self.verified_context_tokens):
            raise ValueError("verified context tokens must be a positive integer")
        if not _valid_positive_int(self.max_prepared_evidence_items):
            raise ValueError("attested prepared evidence limit must be a positive integer")
        if not _valid_nonnegative_int(self.max_tool_steps):
            raise ValueError("attested max tool steps must be a non-negative integer")
        if not isinstance(self.allowed_effects, frozenset) or any(
            not isinstance(item, ModelEffect) for item in self.allowed_effects
        ):
            raise ValueError("attested effects must be an immutable effect set")
        if not isinstance(self.verifier_required, bool):
            raise ValueError("attested verifier_required must be boolean")

    def canonical_sha256(self) -> str:
        return _canonical_sha256(
            {
                "allowed_effects": sorted(item.value for item in self.allowed_effects),
                "capabilities": sorted(item.value for item in self.capabilities),
                "endpoint_binding_sha256": self.endpoint_binding_sha256,
                "max_prepared_evidence_items": self.max_prepared_evidence_items,
                "max_tool_steps": self.max_tool_steps,
                "planner_contract_sha256": self.planner_contract_sha256,
                "probe_suite_sha256": self.probe_suite_sha256,
                "process_epoch_sha256": self.process_epoch_sha256,
                "profile_id": self.profile_id,
                "schema": V12_MODEL_ATTESTATION_SCHEMA,
                "verified_context_tokens": self.verified_context_tokens,
                "verifier_required": self.verifier_required,
            }
        )

    def public_sha256(self) -> str:
        """Hash capability claims without the private endpoint binding/epoch."""

        return _canonical_sha256(
            {
                "allowed_effects": sorted(item.value for item in self.allowed_effects),
                "capabilities": sorted(item.value for item in self.capabilities),
                "max_prepared_evidence_items": self.max_prepared_evidence_items,
                "max_tool_steps": self.max_tool_steps,
                "planner_contract_sha256": self.planner_contract_sha256,
                "probe_suite_sha256": self.probe_suite_sha256,
                "profile_id": self.profile_id,
                "schema": V12_MODEL_ATTESTATION_SCHEMA,
                "verified_context_tokens": self.verified_context_tokens,
                "verifier_required": self.verifier_required,
            }
        )


@dataclass(frozen=True, slots=True)
class ModelProfileLease:
    """Least-privilege snapshot issued for one already-checked route admission."""

    profile_id: str
    attestation_sha256: str = field(repr=False)
    requirements_sha256: str
    capabilities: frozenset[ModelCapability]
    required_context_tokens: int
    prepared_evidence_items: int
    max_tool_steps: int
    effect: ModelEffect
    verifier_required: bool
    process_epoch_sha256: str = field(repr=False, compare=False)
    _gate_authority: object = field(repr=False, compare=False)
    _gate_generation: int = field(repr=False, compare=False)
    schema: str = V12_MODEL_LEASE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or _SAFE_ID.fullmatch(self.profile_id) is None:
            raise ValueError("invalid leased profile id")
        if not _valid_sha256(self.attestation_sha256) or not _valid_sha256(self.requirements_sha256):
            raise ValueError("lease hashes must be lowercase SHA-256 values")
        if not isinstance(self.capabilities, frozenset) or any(
            not isinstance(item, ModelCapability) for item in self.capabilities
        ):
            raise ValueError("leased capabilities must be an immutable capability set")
        if not _valid_nonnegative_int(self.required_context_tokens):
            raise ValueError("leased context tokens must be a non-negative integer")
        if not _valid_nonnegative_int(self.prepared_evidence_items):
            raise ValueError("leased prepared evidence items must be a non-negative integer")
        if not _valid_nonnegative_int(self.max_tool_steps):
            raise ValueError("leased max tool steps must be a non-negative integer")
        if not isinstance(self.effect, ModelEffect) or not isinstance(self.verifier_required, bool):
            raise ValueError("lease authority fields have invalid types")
        if not _valid_sha256(self.process_epoch_sha256):
            raise ValueError("lease process epoch must be a lowercase SHA-256 value")
        if self.schema != V12_MODEL_LEASE_SCHEMA:
            raise ValueError("invalid model lease schema")
        if not _valid_positive_int(self._gate_generation):
            raise ValueError("invalid model lease generation")


class V12ModelGate:
    """Process-local, fail-closed admission gate for canary/V12 handlers."""

    def __init__(self, spec: V12ModelProfileSpec, *, endpoint_binding_sha256: str) -> None:
        if not isinstance(spec, V12ModelProfileSpec):
            raise TypeError("V12ModelGate requires a code-owned model profile")
        registered = V12_MODEL_PROFILES.get((spec.runtime_profile_name, spec.served_model_alias))
        if registered is not spec:
            raise ValueError("V12ModelGate profile is not the registered code-owned object")
        if not _valid_sha256(endpoint_binding_sha256):
            raise ValueError("endpoint binding must be a lowercase SHA-256 value")
        self._spec = spec
        self._endpoint_binding_sha256 = endpoint_binding_sha256
        self._attestation: V12LiveAttestation | None = None
        self._authority = object()
        self._generation = 0
        self._status = ModelGateStatus.SHADOW_CANDIDATE
        self._reason = ModelGateReason.AWAITING_LIVE_ATTESTATION
        self._lock = threading.Lock()

    @property
    def profile(self) -> V12ModelProfileSpec:
        return self._spec

    def shadow_allowed(self) -> bool:
        """Shadow remains available because it owns no publication or effect."""

        return True

    def _reject_locked(self, reason: ModelGateReason) -> None:
        self._generation += 1
        self._attestation = None
        self._status = ModelGateStatus.REVOKED
        self._reason = reason

    def install_live(self, attestation: object) -> bool:
        """Install one typed live result; mappings/files are never accepted.

        Any rejected replacement also clears a prior grant.  Retaining an old
        capability set after a failed re-attestation would make the gate fail
        open precisely when the endpoint changed.
        """

        with self._lock:
            if not isinstance(attestation, V12LiveAttestation):
                self._reject_locked(ModelGateReason.ATTESTATION_REJECTED)
                return False
            valid = bool(
                attestation.profile_id == self._spec.profile_id
                and attestation.planner_contract_sha256 == self._spec.planner_contract_sha256
                and attestation.probe_suite_sha256 == self._spec.probe_suite_sha256
                and attestation.endpoint_binding_sha256 == self._endpoint_binding_sha256
                and self._spec.required_capabilities <= attestation.capabilities
                and attestation.capabilities <= self._spec.allowed_capabilities
                and self._spec.minimum_context_tokens
                <= attestation.verified_context_tokens
                <= self._spec.max_context_tokens
                and 0 < attestation.max_prepared_evidence_items <= self._spec.max_prepared_evidence_items
                and attestation.max_prepared_evidence_items == self._spec.max_prepared_evidence_items
                and attestation.max_tool_steps <= self._spec.max_tool_steps
                and attestation.allowed_effects == self._spec.allowed_effects
                and (not self._spec.verifier_required or attestation.verifier_required)
            )
            if not valid:
                self._reject_locked(ModelGateReason.ATTESTATION_REJECTED)
                return False
            self._generation += 1
            self._attestation = attestation
            self._status = ModelGateStatus.CANARY_READY
            self._reason = ModelGateReason.LIVE_ATTESTATION_CLEAR
            return True

    def revoke(self, reason: ModelGateReason | object = ModelGateReason.EXPLICIT_REVOCATION) -> None:
        """Revoke future leases without copying arbitrary caller text to status."""

        safe_reason = reason if isinstance(reason, ModelGateReason) else ModelGateReason.EXPLICIT_REVOCATION
        with self._lock:
            self._reject_locked(safe_reason)

    def lease(
        self,
        requirements: ModelRequirements,
        *,
        process_epoch_sha256: str,
    ) -> ModelProfileLease | None:
        """Return an exact-subset lease, or ``None`` for every uncertain state."""

        if not isinstance(requirements, ModelRequirements):
            return None
        with self._lock:
            attestation = self._attestation
            if self._status is not ModelGateStatus.CANARY_READY or attestation is None:
                return None
            if not _valid_sha256(process_epoch_sha256):
                self._reject_locked(ModelGateReason.EPOCH_INVALID)
                return None
            if process_epoch_sha256 != attestation.process_epoch_sha256:
                self._reject_locked(ModelGateReason.EPOCH_CHANGED)
                return None
            permitted = bool(
                requirements.capabilities <= attestation.capabilities
                and requirements.required_context_tokens <= attestation.verified_context_tokens
                and requirements.prepared_evidence_items <= attestation.max_prepared_evidence_items
                and requirements.max_tool_steps <= attestation.max_tool_steps
                and requirements.effect in attestation.allowed_effects
                and (not attestation.verifier_required or requirements.verifier_required)
            )
            if not permitted:
                return None
            return ModelProfileLease(
                profile_id=self._spec.profile_id,
                attestation_sha256=attestation.canonical_sha256(),
                requirements_sha256=requirements.canonical_sha256(),
                capabilities=requirements.capabilities,
                required_context_tokens=requirements.required_context_tokens,
                prepared_evidence_items=requirements.prepared_evidence_items,
                max_tool_steps=requirements.max_tool_steps,
                effect=requirements.effect,
                verifier_required=requirements.verifier_required,
                process_epoch_sha256=attestation.process_epoch_sha256,
                _gate_authority=self._authority,
                _gate_generation=self._generation,
            )

    def validate_lease(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        process_epoch_sha256: str,
    ) -> bool:
        """Revalidate a lease against the live generation and endpoint epoch."""

        if type(lease) is not ModelProfileLease or not isinstance(requirements, ModelRequirements):
            return False
        with self._lock:
            attestation = self._attestation
            if self._status is not ModelGateStatus.CANARY_READY or attestation is None:
                return False
            if not _valid_sha256(process_epoch_sha256):
                self._reject_locked(ModelGateReason.EPOCH_INVALID)
                return False
            if process_epoch_sha256 != attestation.process_epoch_sha256:
                self._reject_locked(ModelGateReason.EPOCH_CHANGED)
                return False
            return bool(
                lease._gate_authority is self._authority
                and lease._gate_generation == self._generation
                and lease.profile_id == self._spec.profile_id
                and lease.attestation_sha256 == attestation.canonical_sha256()
                and lease.requirements_sha256 == requirements.canonical_sha256()
                and lease.capabilities == requirements.capabilities
                and lease.required_context_tokens == requirements.required_context_tokens
                and lease.prepared_evidence_items == requirements.prepared_evidence_items
                and lease.max_tool_steps == requirements.max_tool_steps
                and lease.effect is requirements.effect
                and lease.verifier_required is requirements.verifier_required
                and lease.process_epoch_sha256 == attestation.process_epoch_sha256
            )

    def public_status(self) -> dict[str, object]:
        """Return bounded capability facts without URL, epoch, prompt or response."""

        with self._lock:
            attestation = self._attestation
            return {
                "schema": V12_MODEL_PROFILE_SCHEMA,
                "profile_id": self._spec.profile_id,
                "status": self._status.value,
                "reason_code": self._reason.value,
                "planner_contract_sha256": self._spec.planner_contract_sha256,
                "probe_suite_sha256": self._spec.probe_suite_sha256,
                "attestation_sha256": (attestation.public_sha256() if attestation is not None else ""),
                "capabilities": (
                    sorted(item.value for item in attestation.capabilities) if attestation is not None else []
                ),
                "verified_context_tokens": (
                    attestation.verified_context_tokens if attestation is not None else 0
                ),
                "max_prepared_evidence_items": (
                    attestation.max_prepared_evidence_items if attestation is not None else 0
                ),
                "max_tool_steps": attestation.max_tool_steps if attestation is not None else 0,
                "allowed_effects": (
                    sorted(item.value for item in attestation.allowed_effects)
                    if attestation is not None
                    else []
                ),
                "verifier_required": self._spec.verifier_required,
            }


_QWEN36_27B_PLANNER_CONTRACT_SHA256 = _version_sha256(
    "friday.v12-planner-contract.v1",
    {
        "initial_canary_tool_steps": 0,
        "one_publication": True,
        "schema": V12_TURN_PLAN_SCHEMA,
        "source_routes_require_citations": True,
    },
)
# Exact v2 manifest digest produced by ``friday.model_probe``.  The manifest
# binds fixed prompts, validators, verifier cases, deadlines and cancellation
# semantics—not merely a list of case names.  Keeping the digest here avoids a
# model_profiles -> orchestration/model_probe import cycle; the probe refuses a
# profile if its independently recomputed manifest differs by one byte.
_QWEN36_27B_PROBE_SUITE_SHA256 = "1d7648d97977449e7c38463708d42022a3d9b1db425f07bc5df7a55c4a8889c8"

QWEN36_27B_V12_PROFILE = V12ModelProfileSpec(
    profile_id="qwen36-27b-nvfp4-nvidia:dispatcher:v12.1",
    runtime_profile_name="qwen36-27b-nvfp4-nvidia",
    served_model_alias="dispatcher",
    planner_contract_sha256=_QWEN36_27B_PLANNER_CONTRACT_SHA256,
    probe_suite_sha256=_QWEN36_27B_PROBE_SUITE_SHA256,
    allowed_capabilities=frozenset(
        {
            ModelCapability.TURN_PLAN_V1,
            ModelCapability.RU_PLANNING,
            ModelCapability.PREPARED_EVIDENCE_2,
            ModelCapability.CONTEXT_8K,
            ModelCapability.REMOTE_CANCELLATION,
        }
    ),
    required_capabilities=frozenset(
        {
            ModelCapability.TURN_PLAN_V1,
            ModelCapability.RU_PLANNING,
            ModelCapability.PREPARED_EVIDENCE_2,
            ModelCapability.CONTEXT_8K,
            ModelCapability.REMOTE_CANCELLATION,
        }
    ),
    minimum_context_tokens=8192,
    max_context_tokens=8192,
    max_prepared_evidence_items=2,
    max_tool_steps=0,
    allowed_effects=frozenset({ModelEffect.READ}),
    verifier_required=True,
)

V12_MODEL_PROFILES: Mapping[tuple[str, str], V12ModelProfileSpec] = MappingProxyType(
    {
        (
            QWEN36_27B_V12_PROFILE.runtime_profile_name,
            QWEN36_27B_V12_PROFILE.served_model_alias,
        ): QWEN36_27B_V12_PROFILE
    }
)


def v12_model_profile_for(
    runtime_profile_name: object,
    served_model_alias: object,
) -> V12ModelProfileSpec | None:
    """Resolve only an exact code-owned pair; unknown values stay unsupported."""

    if not isinstance(runtime_profile_name, str) or not isinstance(served_model_alias, str):
        return None
    return V12_MODEL_PROFILES.get((runtime_profile_name, served_model_alias))


__all__ = [
    "ModelCapability",
    "ModelEffect",
    "ModelGateReason",
    "ModelGateStatus",
    "ModelProfileLease",
    "ModelRequirements",
    "QWEN36_27B_V12_PROFILE",
    "V12LiveAttestation",
    "V12ModelGate",
    "V12ModelProfileSpec",
    "V12_MODEL_ATTESTATION_SCHEMA",
    "V12_MODEL_LEASE_SCHEMA",
    "V12_MODEL_PROFILES",
    "V12_MODEL_PROFILE_SCHEMA",
    "V12_TURN_PLAN_SCHEMA",
    "v12_model_profile_for",
]
