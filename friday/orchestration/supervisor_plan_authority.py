"""Private source and authority admission for supervisor execution plans.

The supervisor sees neither these source identities nor an authority handle.
Policy Kernel asks one code-owned attestor for a fresh decision only after the
proposal, manifest, registry and budget checks have succeeded.  The promoted
adapter still repeats source and permission checks at use and publication.
"""

from __future__ import annotations

import hmac
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from friday.orchestration.supervisor_contracts import canonical_sha256

PLAN_SOURCE_BINDING_SCHEMA = "friday.supervisor-plan-source-binding.private.v1"
PLAN_AUTHORITY_BOUNDARY_SCHEMA = "friday.supervisor-plan-authority-boundary.private.v1"
PLAN_AUTHORITY_ATTESTATION_SCHEMA = "friday.supervisor-plan-authority-attestation.private.v1"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY_SEAL = object()
_MAX_FRESHNESS_NS = 1_000_000_000


class PlanSourceKind(StrEnum):
    CURRENT_RAW_OBJECT = "current_raw_object"
    SHADOW_PROJECTION = "shadow_projection"


class PlanAuthorityScope(StrEnum):
    SHADOW_ONLY = "shadow_only"
    ASSIST_EXECUTION = "assist_execution"


class PlanAuthorityReason(StrEnum):
    ADMITTED = "admitted"
    DENIED = "denied"
    SOURCE_DRIFT = "source_drift"
    STALE = "stale"
    INVALID_BOUNDARY = "invalid_boundary"


def _digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _raw_reference_binding(raw_object_id: str) -> str:
    if not isinstance(raw_object_id, str) or not raw_object_id:
        raise ValueError("raw object reference is unavailable")
    return canonical_sha256(
        {
            "schema": "friday.supervisor-raw-object-reference.private.v1",
            "raw_object_id": raw_object_id,
        }
    )


@dataclass(frozen=True, slots=True)
class PlanSourceBinding:
    """Body-free exact source triple retained only on the private plan side."""

    kind: PlanSourceKind
    reference_binding_sha256: str
    source_identity_sha256: str
    content_identity_sha256: str

    def __post_init__(self) -> None:
        if type(self.kind) is not PlanSourceKind:
            raise ValueError("plan source kind must be closed")
        _digest(self.reference_binding_sha256, label="source reference binding")
        _digest(self.source_identity_sha256, label="source identity")
        _digest(self.content_identity_sha256, label="content identity")

    @classmethod
    def current_raw_object(
        cls,
        *,
        raw_object_id: str,
        source_identity_sha256: str,
        content_sha256: str,
    ) -> PlanSourceBinding:
        return cls(
            kind=PlanSourceKind.CURRENT_RAW_OBJECT,
            reference_binding_sha256=_raw_reference_binding(raw_object_id),
            source_identity_sha256=source_identity_sha256,
            content_identity_sha256=content_sha256,
        )

    @classmethod
    def shadow_projection(
        cls,
        *,
        projection_sha256: str,
        manifest_sha256: str,
        turn_sha256: str,
    ) -> PlanSourceBinding:
        return cls(
            kind=PlanSourceKind.SHADOW_PROJECTION,
            reference_binding_sha256=projection_sha256,
            source_identity_sha256=manifest_sha256,
            content_identity_sha256=turn_sha256,
        )

    def payload(self) -> dict[str, str]:
        return {
            "schema": PLAN_SOURCE_BINDING_SCHEMA,
            "kind": self.kind.value,
            "reference_binding_sha256": self.reference_binding_sha256,
            "source_identity_sha256": self.source_identity_sha256,
            "content_identity_sha256": self.content_identity_sha256,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


def source_bindings_sha256(bindings: tuple[PlanSourceBinding, ...]) -> str:
    if (
        type(bindings) is not tuple
        or not bindings
        or len(bindings) > 4
        or any(type(item) is not PlanSourceBinding for item in bindings)
        or len({item.canonical_sha256() for item in bindings}) != len(bindings)
    ):
        raise ValueError("plan source bindings must be a small unique tuple")
    return canonical_sha256(
        {
            "schema": "friday.supervisor-plan-source-bindings.private.v1",
            "bindings": [item.payload() for item in bindings],
        }
    )


def _validate_security_ids(values: tuple[str, ...]) -> None:
    if (
        type(values) is not tuple
        or tuple(sorted(values)) != values
        or len(set(values)) != len(values)
        or any(
            not isinstance(value, str) or not value or len(value) > 256 or value != value.strip()
            for value in values
        )
    ):
        raise ValueError("required security ids must be a closed sorted tuple")


def durable_authority_binding_sha256(
    *,
    scope: PlanAuthorityScope,
    actor_binding_sha256: str,
    conversation_binding_sha256: str,
    proposal_sha256: str,
    manifest_sha256: str,
    policy_sha256: str,
    source_bindings_sha256: str,
    capability_bindings_sha256: str,
    budget_sha256: str,
    required_security_ids: tuple[str, ...],
) -> str:
    """Stable authority identity with no process-local clock coordinate."""

    if type(scope) is not PlanAuthorityScope:
        raise ValueError("plan authority scope must be closed")
    for label, value in (
        ("actor binding", actor_binding_sha256),
        ("conversation binding", conversation_binding_sha256),
        ("proposal", proposal_sha256),
        ("manifest", manifest_sha256),
        ("policy", policy_sha256),
        ("source bindings", source_bindings_sha256),
        ("capability bindings", capability_bindings_sha256),
        ("budget", budget_sha256),
    ):
        _digest(value, label=label)
    _validate_security_ids(required_security_ids)
    return canonical_sha256(
        {
            "schema": "friday.supervisor-plan-authority-binding.private.v1",
            "scope": scope.value,
            "actor_binding_sha256": actor_binding_sha256,
            "conversation_binding_sha256": conversation_binding_sha256,
            "proposal_sha256": proposal_sha256,
            "manifest_sha256": manifest_sha256,
            "policy_sha256": policy_sha256,
            "source_bindings_sha256": source_bindings_sha256,
            "capability_bindings_sha256": capability_bindings_sha256,
            "budget_sha256": budget_sha256,
            "required_security_ids": list(required_security_ids),
        }
    )


@dataclass(frozen=True, slots=True)
class PlanAuthorityBoundary:
    scope: PlanAuthorityScope
    actor_binding_sha256: str
    conversation_binding_sha256: str
    proposal_sha256: str
    manifest_sha256: str
    policy_sha256: str
    source_bindings_sha256: str
    capability_bindings_sha256: str
    budget_sha256: str
    required_security_ids: tuple[str, ...]
    turn_deadline_monotonic_ns: int

    def __post_init__(self) -> None:
        if type(self.scope) is not PlanAuthorityScope:
            raise ValueError("plan authority scope must be closed")
        for label, value in (
            ("actor binding", self.actor_binding_sha256),
            ("conversation binding", self.conversation_binding_sha256),
            ("proposal", self.proposal_sha256),
            ("manifest", self.manifest_sha256),
            ("policy", self.policy_sha256),
            ("source bindings", self.source_bindings_sha256),
            ("capability bindings", self.capability_bindings_sha256),
            ("budget", self.budget_sha256),
        ):
            _digest(value, label=label)
        _validate_security_ids(self.required_security_ids)
        if type(self.turn_deadline_monotonic_ns) is not int or self.turn_deadline_monotonic_ns <= 0:
            raise ValueError("turn deadline must be a positive monotonic instant")

    def payload(self) -> dict[str, Any]:
        return {
            "schema": PLAN_AUTHORITY_BOUNDARY_SCHEMA,
            "scope": self.scope.value,
            "actor_binding_sha256": self.actor_binding_sha256,
            "conversation_binding_sha256": self.conversation_binding_sha256,
            "proposal_sha256": self.proposal_sha256,
            "manifest_sha256": self.manifest_sha256,
            "policy_sha256": self.policy_sha256,
            "source_bindings_sha256": self.source_bindings_sha256,
            "capability_bindings_sha256": self.capability_bindings_sha256,
            "budget_sha256": self.budget_sha256,
            "required_security_ids": list(self.required_security_ids),
            "turn_deadline_monotonic_ns": self.turn_deadline_monotonic_ns,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())

    def durable_binding_sha256(self) -> str:
        """Stable plan identity; the process-local monotonic deadline is excluded."""

        return durable_authority_binding_sha256(
            scope=self.scope,
            actor_binding_sha256=self.actor_binding_sha256,
            conversation_binding_sha256=self.conversation_binding_sha256,
            proposal_sha256=self.proposal_sha256,
            manifest_sha256=self.manifest_sha256,
            policy_sha256=self.policy_sha256,
            source_bindings_sha256=self.source_bindings_sha256,
            capability_bindings_sha256=self.capability_bindings_sha256,
            budget_sha256=self.budget_sha256,
            required_security_ids=self.required_security_ids,
        )


@dataclass(frozen=True, slots=True)
class PlanAuthorityAttestation:
    boundary_sha256: str
    checked_at_monotonic_ns: int
    expires_at_monotonic_ns: int
    witness_sha256: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _digest(self.boundary_sha256, label="authority boundary")
        _digest(self.witness_sha256, label="authority witness")
        if (
            self._seal is not _AUTHORITY_SEAL
            or type(self.checked_at_monotonic_ns) is not int
            or type(self.expires_at_monotonic_ns) is not int
            or self.checked_at_monotonic_ns <= 0
            or not self.checked_at_monotonic_ns
            < self.expires_at_monotonic_ns
            <= self.checked_at_monotonic_ns + _MAX_FRESHNESS_NS
        ):
            raise ValueError("plan authority attestation is invalid")

    def payload(self) -> dict[str, Any]:
        return {
            "schema": PLAN_AUTHORITY_ATTESTATION_SCHEMA,
            "boundary_sha256": self.boundary_sha256,
            "checked_at_monotonic_ns": self.checked_at_monotonic_ns,
            "expires_at_monotonic_ns": self.expires_at_monotonic_ns,
            "witness_sha256": self.witness_sha256,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())

    def is_fresh_for(self, boundary: PlanAuthorityBoundary, *, now_ns: int) -> bool:
        return bool(
            type(boundary) is PlanAuthorityBoundary
            and type(now_ns) is int
            and self._seal is _AUTHORITY_SEAL
            and hmac.compare_digest(self.boundary_sha256, boundary.canonical_sha256())
            and self.checked_at_monotonic_ns <= now_ns < self.expires_at_monotonic_ns
            and now_ns < boundary.turn_deadline_monotonic_ns
        )


@dataclass(frozen=True, slots=True)
class PlanAuthorityDecision:
    reason: PlanAuthorityReason
    attestation: PlanAuthorityAttestation | None = None

    def __post_init__(self) -> None:
        if type(self.reason) is not PlanAuthorityReason:
            raise ValueError("plan authority decision reason must be closed")
        if (self.reason is PlanAuthorityReason.ADMITTED) != (
            type(self.attestation) is PlanAuthorityAttestation
        ):
            raise ValueError("plan authority decision and attestation disagree")

    @classmethod
    def rejected(cls, reason: PlanAuthorityReason) -> PlanAuthorityDecision:
        if reason is PlanAuthorityReason.ADMITTED:
            raise ValueError("admitted authority needs an attestation")
        return cls(reason=reason)


PlanAuthorityAttestor = Callable[[PlanAuthorityBoundary], PlanAuthorityDecision]


def attest_plan_authority(
    boundary: PlanAuthorityBoundary,
    *,
    witness_sha256: str,
    now_ns: int | None = None,
    freshness_ns: int = 250_000_000,
) -> PlanAuthorityDecision:
    """Mint a short-lived process-private witness after a trusted check passed."""

    if type(boundary) is not PlanAuthorityBoundary:
        return PlanAuthorityDecision.rejected(PlanAuthorityReason.INVALID_BOUNDARY)
    _digest(witness_sha256, label="authority witness")
    checked = time.monotonic_ns() if now_ns is None else now_ns
    if (
        type(checked) is not int
        or type(freshness_ns) is not int
        or checked <= 0
        or not 1 <= freshness_ns <= _MAX_FRESHNESS_NS
        or checked >= boundary.turn_deadline_monotonic_ns
    ):
        return PlanAuthorityDecision.rejected(PlanAuthorityReason.STALE)
    return PlanAuthorityDecision(
        reason=PlanAuthorityReason.ADMITTED,
        attestation=PlanAuthorityAttestation(
            boundary_sha256=boundary.canonical_sha256(),
            checked_at_monotonic_ns=checked,
            expires_at_monotonic_ns=min(
                checked + freshness_ns,
                boundary.turn_deadline_monotonic_ns,
            ),
            witness_sha256=witness_sha256,
            _seal=_AUTHORITY_SEAL,
        ),
    )


def current_raw_source_matches(
    binding: PlanSourceBinding,
    *,
    raw_object_id: str,
    source_identity_sha256: str,
    content_sha256: str,
) -> bool:
    try:
        expected = PlanSourceBinding.current_raw_object(
            raw_object_id=raw_object_id,
            source_identity_sha256=source_identity_sha256,
            content_sha256=content_sha256,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(binding.canonical_sha256(), expected.canonical_sha256())


def authority_witness_sha256(boundary: PlanAuthorityBoundary, *state_digests: str) -> str:
    """Create a body-free witness over the exact boundary and checked state."""

    for value in state_digests:
        _digest(value, label="authority state")
    return canonical_sha256(
        {
            "schema": "friday.supervisor-plan-authority-witness.private.v1",
            "boundary_sha256": boundary.canonical_sha256(),
            "state_digests": list(state_digests),
        }
    )


__all__ = [
    "PlanAuthorityAttestation",
    "PlanAuthorityAttestor",
    "PlanAuthorityBoundary",
    "PlanAuthorityDecision",
    "PlanAuthorityReason",
    "PlanAuthorityScope",
    "PlanSourceBinding",
    "PlanSourceKind",
    "attest_plan_authority",
    "authority_witness_sha256",
    "current_raw_source_matches",
    "durable_authority_binding_sha256",
    "source_bindings_sha256",
]
