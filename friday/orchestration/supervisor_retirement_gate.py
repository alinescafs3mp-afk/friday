"""Evidence gate for retiring one journey-specific semantic heuristic.

P6 is intentionally a release-time decision, not a runtime routing feature.
This module cannot edit code or configuration.  It only evaluates a body-free
receipt against the exact conditions that must be true before a reviewer may
remove one semantic guess.  Deterministic, authority, lifecycle and publication
guards are structurally ineligible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from friday.orchestration.supervisor_contracts import TaskClass, canonical_sha256

SUPERVISOR_RETIREMENT_EVIDENCE_SCHEMA = "friday.supervisor-retirement-evidence.v1"
SUPERVISOR_RETIREMENT_GATE_VERSION = "semantic-supervisor-retirement-gate-v1"

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")


class RetirementSurfaceClass(StrEnum):
    SEMANTIC_HEURISTIC = "semantic_heuristic"
    DETERMINISTIC_INVARIANT = "deterministic_invariant"
    AUTHORITY_GUARD = "authority_guard"
    LIFECYCLE_OR_STATE = "lifecycle_or_state"
    PUBLICATION_GUARD = "publication_guard"
    LEGACY_MIXED = "legacy_mixed"


class RetirementEvidenceAuthority(StrEnum):
    PRODUCTION_JOINED = "production_joined"
    ISOLATED_ENDPOINT = "isolated_endpoint"
    SYNTHETIC_OFFLINE = "synthetic_offline"


class RetirementGateReason(StrEnum):
    ADMITTED = "admitted"
    SURFACE_IS_NOT_SEMANTIC = "surface_is_not_semantic"
    JOURNEY_IS_NOT_PROMOTABLE = "journey_is_not_promotable"
    JOURNEY_MISMATCH = "journey_mismatch"
    REPLACEMENT_IDENTITY_MISMATCH = "replacement_identity_mismatch"
    PRODUCTION_EVIDENCE_REQUIRED = "production_evidence_required"
    SHADOW_NOT_ACCEPTED = "shadow_not_accepted"
    CANARY_NOT_ACCEPTED = "canary_not_accepted"
    PRODUCTION_PROMOTION_NOT_ACCEPTED = "production_promotion_not_accepted"
    FALLBACK_NOT_PROVEN = "fallback_not_proven"
    TRACE_COVERAGE_INCOMPLETE = "trace_coverage_incomplete"
    HIDDEN_OWNER_OBSERVED = "hidden_owner_observed"
    DUPLICATE_OPERATION_OBSERVED = "duplicate_operation_observed"
    FALSE_COMPLETION_REGRESSION = "false_completion_regression"
    USER_VISIBLE_REGRESSION = "user_visible_regression"
    ROLLBACK_NOT_PROVEN = "rollback_not_proven"
    DOCUMENTATION_NOT_UPDATED = "documentation_not_updated"
    STATUS_REGISTRY_NOT_UPDATED = "status_registry_not_updated"


def _digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _safe_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _count(value: object, *, label: str, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class HeuristicRetirementCandidate:
    """Repository-owned description of exactly one removable semantic branch."""

    candidate_id: str
    journey: TaskClass
    surface_class: RetirementSurfaceClass
    replacement_policy_sha256: str
    replacement_manifest_sha256: str
    replacement_adapter_registry_sha256: str
    rollback_source_sha256: str
    documentation_updated: bool
    status_registry_updated: bool

    def __post_init__(self) -> None:
        _safe_id(self.candidate_id, label="candidate_id")
        if not isinstance(self.journey, TaskClass):
            raise ValueError("journey must be a typed task class")
        if not isinstance(self.surface_class, RetirementSurfaceClass):
            raise ValueError("surface_class is invalid")
        for label, value in (
            ("replacement_policy_sha256", self.replacement_policy_sha256),
            ("replacement_manifest_sha256", self.replacement_manifest_sha256),
            ("replacement_adapter_registry_sha256", self.replacement_adapter_registry_sha256),
            ("rollback_source_sha256", self.rollback_source_sha256),
        ):
            _digest(value, label=label)
        if not isinstance(self.documentation_updated, bool) or not isinstance(
            self.status_registry_updated, bool
        ):
            raise ValueError("candidate release-state fields must be boolean")


@dataclass(frozen=True, slots=True)
class HeuristicRetirementEvidence:
    """Body-free, accepted release evidence for one exact replacement journey."""

    evidence_id: str
    authority: RetirementEvidenceAuthority
    journey: TaskClass
    replacement_policy_sha256: str
    replacement_manifest_sha256: str
    replacement_adapter_registry_sha256: str
    tested_rollback_source_sha256: str
    shadow_accepted: bool
    canary_accepted: bool
    production_promotion_accepted: bool
    primary_fallback_proven: bool
    rollback_proven: bool
    observation_count: int
    joined_trace_count: int
    hidden_owner_count: int
    duplicate_capability_count: int
    duplicate_effect_count: int
    duplicate_publication_count: int
    false_completion_regression_count: int
    user_visible_regression_count: int

    def __post_init__(self) -> None:
        _safe_id(self.evidence_id, label="evidence_id")
        if not isinstance(self.authority, RetirementEvidenceAuthority):
            raise ValueError("evidence authority is invalid")
        if not isinstance(self.journey, TaskClass):
            raise ValueError("evidence journey must be a typed task class")
        for label, value in (
            ("replacement_policy_sha256", self.replacement_policy_sha256),
            ("replacement_manifest_sha256", self.replacement_manifest_sha256),
            ("replacement_adapter_registry_sha256", self.replacement_adapter_registry_sha256),
            ("tested_rollback_source_sha256", self.tested_rollback_source_sha256),
        ):
            _digest(value, label=label)
        for label, value in (
            ("shadow_accepted", self.shadow_accepted),
            ("canary_accepted", self.canary_accepted),
            ("production_promotion_accepted", self.production_promotion_accepted),
            ("primary_fallback_proven", self.primary_fallback_proven),
            ("rollback_proven", self.rollback_proven),
        ):
            if not isinstance(value, bool):
                raise ValueError(f"{label} must be boolean")
        _count(self.observation_count, label="observation_count", positive=True)
        _count(self.joined_trace_count, label="joined_trace_count")
        for label, value in (
            ("hidden_owner_count", self.hidden_owner_count),
            ("duplicate_capability_count", self.duplicate_capability_count),
            ("duplicate_effect_count", self.duplicate_effect_count),
            ("duplicate_publication_count", self.duplicate_publication_count),
            ("false_completion_regression_count", self.false_completion_regression_count),
            ("user_visible_regression_count", self.user_visible_regression_count),
        ):
            _count(value, label=label)
        if self.joined_trace_count > self.observation_count:
            raise ValueError("joined_trace_count exceeds the evidence window")

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_RETIREMENT_EVIDENCE_SCHEMA,
            "evidence_id": self.evidence_id,
            "authority": self.authority.value,
            "journey": self.journey.value,
            "replacement_policy_sha256": self.replacement_policy_sha256,
            "replacement_manifest_sha256": self.replacement_manifest_sha256,
            "replacement_adapter_registry_sha256": self.replacement_adapter_registry_sha256,
            "tested_rollback_source_sha256": self.tested_rollback_source_sha256,
            "shadow_accepted": self.shadow_accepted,
            "canary_accepted": self.canary_accepted,
            "production_promotion_accepted": self.production_promotion_accepted,
            "primary_fallback_proven": self.primary_fallback_proven,
            "rollback_proven": self.rollback_proven,
            "observation_count": self.observation_count,
            "joined_trace_count": self.joined_trace_count,
            "hidden_owner_count": self.hidden_owner_count,
            "duplicate_capability_count": self.duplicate_capability_count,
            "duplicate_effect_count": self.duplicate_effect_count,
            "duplicate_publication_count": self.duplicate_publication_count,
            "false_completion_regression_count": self.false_completion_regression_count,
            "user_visible_regression_count": self.user_visible_regression_count,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


@dataclass(frozen=True, slots=True)
class HeuristicRetirementDecision:
    admitted: bool
    reason: RetirementGateReason
    candidate_id: str
    evidence_sha256: str | None = None


def _reject(
    candidate: HeuristicRetirementCandidate,
    reason: RetirementGateReason,
) -> HeuristicRetirementDecision:
    return HeuristicRetirementDecision(
        admitted=False,
        reason=reason,
        candidate_id=candidate.candidate_id,
    )


def evaluate_heuristic_retirement(
    candidate: HeuristicRetirementCandidate,
    evidence: HeuristicRetirementEvidence,
) -> HeuristicRetirementDecision:
    """Admit a reviewer-visible P6 candidate; never mutate or delete a surface."""

    if not isinstance(candidate, HeuristicRetirementCandidate) or not isinstance(
        evidence, HeuristicRetirementEvidence
    ):
        raise TypeError("retirement gate requires typed candidate and evidence")
    if candidate.surface_class is not RetirementSurfaceClass.SEMANTIC_HEURISTIC:
        return _reject(candidate, RetirementGateReason.SURFACE_IS_NOT_SEMANTIC)
    if candidate.journey is not TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB:
        return _reject(candidate, RetirementGateReason.JOURNEY_IS_NOT_PROMOTABLE)
    if evidence.journey is not candidate.journey:
        return _reject(candidate, RetirementGateReason.JOURNEY_MISMATCH)
    if (
        evidence.replacement_policy_sha256 != candidate.replacement_policy_sha256
        or evidence.replacement_manifest_sha256 != candidate.replacement_manifest_sha256
        or evidence.replacement_adapter_registry_sha256 != candidate.replacement_adapter_registry_sha256
        or evidence.tested_rollback_source_sha256 != candidate.rollback_source_sha256
    ):
        return _reject(candidate, RetirementGateReason.REPLACEMENT_IDENTITY_MISMATCH)
    if evidence.authority is not RetirementEvidenceAuthority.PRODUCTION_JOINED:
        return _reject(candidate, RetirementGateReason.PRODUCTION_EVIDENCE_REQUIRED)
    checks = (
        (evidence.shadow_accepted, RetirementGateReason.SHADOW_NOT_ACCEPTED),
        (evidence.canary_accepted, RetirementGateReason.CANARY_NOT_ACCEPTED),
        (
            evidence.production_promotion_accepted,
            RetirementGateReason.PRODUCTION_PROMOTION_NOT_ACCEPTED,
        ),
        (evidence.primary_fallback_proven, RetirementGateReason.FALLBACK_NOT_PROVEN),
        (
            evidence.joined_trace_count == evidence.observation_count,
            RetirementGateReason.TRACE_COVERAGE_INCOMPLETE,
        ),
        (evidence.hidden_owner_count == 0, RetirementGateReason.HIDDEN_OWNER_OBSERVED),
        (
            evidence.duplicate_capability_count == 0
            and evidence.duplicate_effect_count == 0
            and evidence.duplicate_publication_count == 0,
            RetirementGateReason.DUPLICATE_OPERATION_OBSERVED,
        ),
        (
            evidence.false_completion_regression_count == 0,
            RetirementGateReason.FALSE_COMPLETION_REGRESSION,
        ),
        (
            evidence.user_visible_regression_count == 0,
            RetirementGateReason.USER_VISIBLE_REGRESSION,
        ),
        (evidence.rollback_proven, RetirementGateReason.ROLLBACK_NOT_PROVEN),
        (candidate.documentation_updated, RetirementGateReason.DOCUMENTATION_NOT_UPDATED),
        (candidate.status_registry_updated, RetirementGateReason.STATUS_REGISTRY_NOT_UPDATED),
    )
    for accepted, reason in checks:
        if not accepted:
            return _reject(candidate, reason)
    return HeuristicRetirementDecision(
        admitted=True,
        reason=RetirementGateReason.ADMITTED,
        candidate_id=candidate.candidate_id,
        evidence_sha256=evidence.canonical_sha256(),
    )


__all__ = [
    "HeuristicRetirementCandidate",
    "HeuristicRetirementDecision",
    "HeuristicRetirementEvidence",
    "RetirementEvidenceAuthority",
    "RetirementGateReason",
    "RetirementSurfaceClass",
    "SUPERVISOR_RETIREMENT_EVIDENCE_SCHEMA",
    "SUPERVISOR_RETIREMENT_GATE_VERSION",
    "evaluate_heuristic_retirement",
]
