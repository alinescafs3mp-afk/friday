"""Pure P2/P4 promotion admission for the one bounded supervisor journey.

This is deliberately not runtime wiring.  It can attest that a source candidate
and independently accepted production evidence are eligible for an ``assist``
or narrow ``canary`` activation.  It cannot execute a plan, own a user turn,
publish an answer, write product state, or turn a P1 shadow observation into
authority.

The accepted P1 product policy remains the exact discarded-shadow policy.  A
distinct P4 product policy admits one review round for assist/canary while the
scheduler remains non-owning shadow.  A separate operator gate, bound to one
body-free evidence digest, is still required for promotion.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from friday import semantic_supervisor_policy
from friday.orchestration.capability_binding import CapabilityBindingSnapshot
from friday.orchestration.supervisor_contracts import (
    FILE_CURRENT_READ_ID,
    WEB_SEARCH_CURRENT_ID,
    CapabilityEffectClass,
    SupervisorMode,
    TaskClass,
    canonical_sha256,
)

SUPERVISOR_ASSIST_PROMOTION_SCHEMA = "friday.supervisor-assist-promotion.v5"
SUPERVISOR_ASSIST_READINESS_EVIDENCE_SCHEMA = "friday.supervisor-assist-readiness-evidence.v2"
SUPERVISOR_ASSIST_OUTCOME_EVIDENCE_SCHEMA = "friday.supervisor-assist-outcome-evidence.v2"
SUPERVISOR_ASSIST_PROMOTION_GATE_ID = "semantic-supervisor-current-file-web-promotion-v2"
SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS = 6
SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS = 1
SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS = 20
SUPERVISOR_ASSIST_MAX_UNNECESSARY_CALL_RATE_BPS = 0

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_ID_RE = re.compile(r"[a-z][a-z0-9_.-]{0,95}\Z")
_SAFE_FAILURE_CLASS_RE = re.compile(r"[a-z][a-z0-9_.:-]{0,127}\Z")
_MAX_LATENCY_MS = 86_400_000
_P1_POLICY_ID = semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_ID
_P1_POLICY_SHA256 = semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_SHA256
_ASSIST_POLICY_ID = semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID
_ASSIST_POLICY_SHA256 = semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256
_P1_PROFILE_ID = semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID
_P1_PROFILE_MANIFEST_SHA256 = semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
_P1_WORKLOAD = semantic_supervisor_policy.SUPERVISOR_WORKLOAD

_FILE_BINDING = (
    "files.read",
    "file_read",
    "friday.orchestration.file_read.V12FileReadHandler",
)
_WEB_BINDING = (
    "web.compare.transient",
    "friday.orchestration.transient_web_comparison.TransientWebComparisonAdapter.research",
    "transient_web_comparison",
)

_EXPECTED_PROMOTION_POLICY = MappingProxyType(
    {
        "schema": SUPERVISOR_ASSIST_PROMOTION_SCHEMA,
        "gate_id": SUPERVISOR_ASSIST_PROMOTION_GATE_ID,
        "shadow_policy_id": _P1_POLICY_ID,
        "shadow_policy_sha256": _P1_POLICY_SHA256,
        "target_policy_id": _ASSIST_POLICY_ID,
        "target_policy_sha256": _ASSIST_POLICY_SHA256,
        "runtime_profile_id": _P1_PROFILE_ID,
        "runtime_profile_manifest_sha256": _P1_PROFILE_MANIFEST_SHA256,
        "scheduler_workload": _P1_WORKLOAD,
        "scheduler_effective_mode": SupervisorMode.SHADOW.value,
        "task_class": TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB.value,
        "promotion_modes": (SupervisorMode.ASSIST.value, SupervisorMode.CANARY.value),
        "max_steps": SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS,
        "max_review_rounds": SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS,
        "readiness_evidence_schema": SUPERVISOR_ASSIST_READINESS_EVIDENCE_SCHEMA,
        "outcome_evidence_schema": SUPERVISOR_ASSIST_OUTCOME_EVIDENCE_SCHEMA,
        "minimum_product_observations": SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS,
        "max_unnecessary_call_rate_bps": SUPERVISOR_ASSIST_MAX_UNNECESSARY_CALL_RATE_BPS,
        "user_visible_regression_budget": 0,
        "canary_requires_exact_actor_allowlist": True,
        "operator_gate_required": True,
        "live_production_joined_evidence_required": True,
        "execution_authorized": False,
        "publication_authorized": False,
        "storage_write_authorized": False,
    }
)
SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256 = canonical_sha256(dict(_EXPECTED_PROMOTION_POLICY))


class AssistPromotionEvidenceAuthority(StrEnum):
    """Provenance classes are not interchangeable promotion authority."""

    SOURCE_READY = "source_ready"
    SYNTHETIC_OFFLINE = "synthetic_offline"
    ISOLATED_LIVE_PROTOCOL = "isolated_live_protocol"
    PRODUCTION_JOINED = "production_joined"


class AssistPromotionReadiness(StrEnum):
    CLOSED = "closed"
    SOURCE_READY = "source_ready"
    LIVE_EVIDENCE_READY = "live_evidence_ready"


class AssistPromotionQualityBasis(StrEnum):
    """The two product claims that may justify one bounded promotion."""

    COMPLETION_RATE_IMPROVEMENT = "completion_rate_improvement"
    DOCUMENTED_FAILURE_CLASS_REMOVAL = "documented_failure_class_removal"


class AssistPromotionReason(StrEnum):
    ADMITTED = "admitted"
    MALFORMED = "malformed"
    MODE_OFF = "mode_off"
    SHADOW_NEVER_OWNS = "shadow_never_owns"
    MODE_NOT_ADMITTED = "mode_not_admitted"
    TASK_NOT_ADMITTED = "task_not_admitted"
    BOUNDS_DRIFT = "bounds_drift"
    P1_POLICY_IDENTITY_DRIFT = "p1_policy_identity_drift"
    ASSIST_POLICY_IDENTITY_DRIFT = "assist_policy_identity_drift"
    RUNTIME_PROFILE_IDENTITY_DRIFT = "runtime_profile_identity_drift"
    SCHEDULER_IDENTITY_DRIFT = "scheduler_identity_drift"
    SCHEDULER_WORKLOAD_UNAVAILABLE = "scheduler_workload_unavailable"
    REGISTRY_BINDING_DRIFT = "registry_binding_drift"
    LAPTOP_RUNTIME_UNAVAILABLE = "laptop_runtime_unavailable"
    LIVE_EVIDENCE_REQUIRED = "live_evidence_required"
    PRODUCTION_JOINED_EVIDENCE_REQUIRED = "production_joined_evidence_required"
    EVIDENCE_STAGE_DRIFT = "evidence_stage_drift"
    EVIDENCE_IDENTITY_DRIFT = "evidence_identity_drift"
    EVIDENCE_WINDOW_INCOMPLETE = "evidence_window_incomplete"
    EVIDENCE_INVARIANT_FAILED = "evidence_invariant_failed"
    PRODUCT_QUALITY_NOT_PROVEN = "product_quality_not_proven"
    PRODUCT_LATENCY_BUDGET_EXCEEDED = "product_latency_budget_exceeded"
    UNNECESSARY_SUPERVISOR_CALL_RATE_EXCEEDED = "unnecessary_supervisor_call_rate_exceeded"
    USER_VISIBLE_REGRESSION_OBSERVED = "user_visible_regression_observed"
    OPERATOR_GATE_CLOSED = "operator_gate_closed"
    OPERATOR_GATE_DRIFT = "operator_gate_drift"
    OPERATOR_EVIDENCE_NOT_BOUND = "operator_evidence_not_bound"
    ASSIST_ALLOWLIST_NOT_ADMITTED = "assist_allowlist_not_admitted"
    CANARY_ALLOWLIST_REQUIRED = "canary_allowlist_required"
    CANARY_ACTOR_NOT_ALLOWLISTED = "canary_actor_not_allowlisted"


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_optional_digest(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _require_digest(value, label=label)


def _require_safe_id(value: object, *, label: str) -> str:
    if type(value) is not str or _SAFE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _require_bool(value: object, *, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be boolean")
    return value


def _require_count(value: object, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _require_positive_count(value: object, *, label: str) -> int:
    count = _require_count(value, label=label)
    if count < 1:
        raise ValueError(f"{label} must be a positive integer")
    return count


@dataclass(frozen=True, slots=True)
class AssistPromotionReadinessEvidence:
    """Shadow-stage evidence that justifies a bounded assist experiment.

    This receipt proves a real baseline failure and a separately digested
    counterfactual/readiness battery.  It deliberately has no promoted outcome
    fields and cannot claim that the failure class has already disappeared.
    """

    baseline_window_sha256: str
    baseline_observation_count: int
    baseline_complete_count: int
    documented_failure_class_id: str
    documented_failure_class_sha256: str
    baseline_failure_class_count: int
    readiness_witness_sha256: str
    readiness_observation_count: int
    latency_budget_target_mode: SupervisorMode
    latency_budget_source_revision_sha256: str
    latency_budget_ms: int
    latency_budget_sha256: str
    latency_total_ms: int
    latency_max_ms: int
    call_rate_observation_count: int
    supervisor_invocation_count: int
    unnecessary_supervisor_invocation_count: int
    user_visible_observation_count: int
    user_visible_regression_count: int

    def __post_init__(self) -> None:
        for label, value in (
            ("baseline_window_sha256", self.baseline_window_sha256),
            ("documented_failure_class_sha256", self.documented_failure_class_sha256),
            ("readiness_witness_sha256", self.readiness_witness_sha256),
            (
                "latency_budget_source_revision_sha256",
                self.latency_budget_source_revision_sha256,
            ),
            ("latency_budget_sha256", self.latency_budget_sha256),
        ):
            _require_digest(value, label=label)
        if self.latency_budget_target_mode is not SupervisorMode.ASSIST:
            raise ValueError("readiness latency budget target mode must be assist")
        if (
            type(self.documented_failure_class_id) is not str
            or self.documented_failure_class_id == "none"
            or _SAFE_FAILURE_CLASS_RE.fullmatch(self.documented_failure_class_id) is None
        ):
            raise ValueError("documented_failure_class_id is invalid")
        for label in (
            "baseline_observation_count",
            "baseline_complete_count",
            "baseline_failure_class_count",
            "readiness_observation_count",
            "latency_total_ms",
            "latency_max_ms",
            "call_rate_observation_count",
            "supervisor_invocation_count",
            "unnecessary_supervisor_invocation_count",
            "user_visible_observation_count",
            "user_visible_regression_count",
        ):
            _require_count(getattr(self, label), label=label)
        _require_positive_count(self.baseline_observation_count, label="baseline_observation_count")
        _require_positive_count(self.readiness_observation_count, label="readiness_observation_count")
        _require_positive_count(self.latency_budget_ms, label="latency_budget_ms")
        if self.latency_budget_ms > _MAX_LATENCY_MS or self.latency_max_ms > _MAX_LATENCY_MS:
            raise ValueError("readiness latency exceeds the trace contract")
        if self.baseline_complete_count > self.baseline_observation_count:
            raise ValueError("baseline_complete_count exceeds its observation window")
        if self.baseline_failure_class_count > self.baseline_observation_count:
            raise ValueError("baseline failure count exceeds its observation window")
        if self.supervisor_invocation_count > self.call_rate_observation_count:
            raise ValueError("supervisor invocation count exceeds its observation window")
        if self.unnecessary_supervisor_invocation_count > self.supervisor_invocation_count:
            raise ValueError("unnecessary invocation count exceeds all invocations")
        if self.user_visible_regression_count > self.user_visible_observation_count:
            raise ValueError("user-visible regression count exceeds its observation window")
        if (
            not self.latency_max_ms
            <= self.latency_total_ms
            <= (self.latency_max_ms * self.readiness_observation_count)
        ):
            raise ValueError("readiness latency aggregate is inconsistent")

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_ASSIST_READINESS_EVIDENCE_SCHEMA,
            "baseline_window_sha256": self.baseline_window_sha256,
            "baseline_observation_count": self.baseline_observation_count,
            "baseline_complete_count": self.baseline_complete_count,
            "documented_failure_class_id": self.documented_failure_class_id,
            "documented_failure_class_sha256": self.documented_failure_class_sha256,
            "baseline_failure_class_count": self.baseline_failure_class_count,
            "readiness_witness_sha256": self.readiness_witness_sha256,
            "readiness_observation_count": self.readiness_observation_count,
            "latency_budget_target_mode": self.latency_budget_target_mode.value,
            "latency_budget_source_revision_sha256": (self.latency_budget_source_revision_sha256),
            "latency_budget_ms": self.latency_budget_ms,
            "latency_budget_sha256": self.latency_budget_sha256,
            "latency_total_ms": self.latency_total_ms,
            "latency_max_ms": self.latency_max_ms,
            "call_rate_observation_count": self.call_rate_observation_count,
            "supervisor_invocation_count": self.supervisor_invocation_count,
            "unnecessary_supervisor_invocation_count": (self.unnecessary_supervisor_invocation_count),
            "user_visible_observation_count": self.user_visible_observation_count,
            "user_visible_regression_count": self.user_visible_regression_count,
        }


@dataclass(frozen=True, slots=True)
class AssistPromotionOutcomeEvidence:
    """Actual assist observations required before a canary promotion."""

    quality_basis: AssistPromotionQualityBasis
    baseline_window_sha256: str
    promoted_window_sha256: str
    baseline_observation_count: int
    baseline_complete_count: int
    promoted_observation_count: int
    promoted_complete_count: int
    documented_failure_class_id: str
    documented_failure_class_sha256: str | None
    baseline_failure_class_count: int
    promoted_failure_class_count: int
    latency_budget_target_mode: SupervisorMode
    latency_budget_source_revision_sha256: str
    latency_budget_ms: int
    latency_budget_sha256: str
    latency_observation_count: int
    latency_total_ms: int
    latency_max_ms: int
    call_rate_observation_count: int
    supervisor_invocation_count: int
    unnecessary_supervisor_invocation_count: int
    user_visible_observation_count: int
    user_visible_regression_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.quality_basis, AssistPromotionQualityBasis):
            raise ValueError("quality_basis must be typed")
        for label, value in (
            ("baseline_window_sha256", self.baseline_window_sha256),
            ("promoted_window_sha256", self.promoted_window_sha256),
            (
                "latency_budget_source_revision_sha256",
                self.latency_budget_source_revision_sha256,
            ),
            ("latency_budget_sha256", self.latency_budget_sha256),
        ):
            _require_digest(value, label=label)
        if self.latency_budget_target_mode is not SupervisorMode.CANARY:
            raise ValueError("outcome latency budget target mode must be canary")
        if hmac.compare_digest(self.baseline_window_sha256, self.promoted_window_sha256):
            raise ValueError("product evidence windows must be distinct")
        for label in (
            "baseline_observation_count",
            "baseline_complete_count",
            "promoted_observation_count",
            "promoted_complete_count",
            "baseline_failure_class_count",
            "promoted_failure_class_count",
            "latency_observation_count",
            "latency_total_ms",
            "latency_max_ms",
            "call_rate_observation_count",
            "supervisor_invocation_count",
            "unnecessary_supervisor_invocation_count",
            "user_visible_observation_count",
            "user_visible_regression_count",
        ):
            _require_count(getattr(self, label), label=label)
        _require_positive_count(self.baseline_observation_count, label="baseline_observation_count")
        _require_positive_count(self.promoted_observation_count, label="promoted_observation_count")
        _require_positive_count(self.latency_budget_ms, label="latency_budget_ms")
        if self.latency_budget_ms > _MAX_LATENCY_MS:
            raise ValueError("latency_budget_ms exceeds the trace contract")
        if self.latency_max_ms > _MAX_LATENCY_MS:
            raise ValueError("latency_max_ms exceeds the trace contract")
        if self.baseline_complete_count > self.baseline_observation_count:
            raise ValueError("baseline_complete_count exceeds its observation window")
        if self.promoted_complete_count > self.promoted_observation_count:
            raise ValueError("promoted_complete_count exceeds its observation window")
        if self.baseline_failure_class_count > self.baseline_observation_count:
            raise ValueError("baseline failure count exceeds its observation window")
        if self.promoted_failure_class_count > self.promoted_observation_count:
            raise ValueError("promoted failure count exceeds its observation window")
        if self.supervisor_invocation_count > self.call_rate_observation_count:
            raise ValueError("supervisor invocation count exceeds its observation window")
        if self.unnecessary_supervisor_invocation_count > self.supervisor_invocation_count:
            raise ValueError("unnecessary invocation count exceeds all invocations")
        if self.user_visible_regression_count > self.user_visible_observation_count:
            raise ValueError("user-visible regression count exceeds its observation window")
        if self.latency_observation_count:
            if (
                not self.latency_max_ms
                <= self.latency_total_ms
                <= (self.latency_max_ms * self.latency_observation_count)
            ):
                raise ValueError("latency aggregate is inconsistent")
        elif self.latency_total_ms or self.latency_max_ms:
            raise ValueError("empty latency window must have zero aggregate")
        if (
            type(self.documented_failure_class_id) is not str
            or _SAFE_FAILURE_CLASS_RE.fullmatch(self.documented_failure_class_id) is None
        ):
            raise ValueError("documented_failure_class_id is invalid")
        if self.documented_failure_class_sha256 is not None:
            _require_digest(
                self.documented_failure_class_sha256,
                label="documented_failure_class_sha256",
            )
        if self.quality_basis is AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT:
            if (
                self.documented_failure_class_id != "none"
                or self.documented_failure_class_sha256 is not None
                or self.baseline_failure_class_count
                or self.promoted_failure_class_count
            ):
                raise ValueError("completion improvement must not carry a failure-class claim")
        elif self.documented_failure_class_id == "none" or self.documented_failure_class_sha256 is None:
            raise ValueError("failure-class removal requires an exact documented identity")

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_ASSIST_OUTCOME_EVIDENCE_SCHEMA,
            "quality_basis": self.quality_basis.value,
            "baseline_window_sha256": self.baseline_window_sha256,
            "promoted_window_sha256": self.promoted_window_sha256,
            "baseline_observation_count": self.baseline_observation_count,
            "baseline_complete_count": self.baseline_complete_count,
            "promoted_observation_count": self.promoted_observation_count,
            "promoted_complete_count": self.promoted_complete_count,
            "documented_failure_class_id": self.documented_failure_class_id,
            "documented_failure_class_sha256": self.documented_failure_class_sha256,
            "baseline_failure_class_count": self.baseline_failure_class_count,
            "promoted_failure_class_count": self.promoted_failure_class_count,
            "latency_budget_target_mode": self.latency_budget_target_mode.value,
            "latency_budget_source_revision_sha256": (self.latency_budget_source_revision_sha256),
            "latency_budget_ms": self.latency_budget_ms,
            "latency_budget_sha256": self.latency_budget_sha256,
            "latency_observation_count": self.latency_observation_count,
            "latency_total_ms": self.latency_total_ms,
            "latency_max_ms": self.latency_max_ms,
            "call_rate_observation_count": self.call_rate_observation_count,
            "supervisor_invocation_count": self.supervisor_invocation_count,
            "unnecessary_supervisor_invocation_count": (self.unnecessary_supervisor_invocation_count),
            "user_visible_observation_count": self.user_visible_observation_count,
            "user_visible_regression_count": self.user_visible_regression_count,
        }


@dataclass(frozen=True, slots=True)
class SupervisorSchedulerAdmissionSnapshot:
    """Body-free projection of the selected target scheduler admission."""

    workload: str
    requested_mode: str
    effective_mode: str
    policy_id: str
    policy_sha256: str
    runtime_profile_id: str
    runtime_profile_manifest_sha256: str
    profile_admission: str
    closed_reason: str
    workload_available: bool
    runtime_available: bool

    def __post_init__(self) -> None:
        for label, value in (
            ("workload", self.workload),
            ("requested_mode", self.requested_mode),
            ("effective_mode", self.effective_mode),
            ("policy_id", self.policy_id),
            ("profile_admission", self.profile_admission),
            ("closed_reason", self.closed_reason),
        ):
            _require_safe_id(value, label=label)
        _require_digest(self.policy_sha256, label="policy_sha256")
        _require_safe_id(self.runtime_profile_id, label="runtime_profile_id")
        _require_digest(
            self.runtime_profile_manifest_sha256,
            label="runtime_profile_manifest_sha256",
        )
        _require_bool(self.workload_available, label="workload_available")
        _require_bool(self.runtime_available, label="runtime_available")


@dataclass(frozen=True, slots=True)
class AssistPromotionCandidate:
    """Exact source/runtime candidate for one promotion decision.

    ``expected_registry_binding_sha256`` is the release-pinned identity;
    ``binding_snapshot`` is the freshly resolved process-private registry view.
    The evaluator only compares and inspects them.
    """

    requested_mode: SupervisorMode
    task_class: TaskClass
    source_revision_sha256: str
    expected_registry_binding_sha256: str
    binding_snapshot: CapabilityBindingSnapshot
    scheduler: SupervisorSchedulerAdmissionSnapshot
    max_steps: int
    max_review_rounds: int
    latency_budget_sha256: str
    latency_budget_ms: int
    actor_binding_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.requested_mode, SupervisorMode):
            raise ValueError("requested_mode must be typed")
        if not isinstance(self.task_class, TaskClass):
            raise ValueError("task_class must be typed")
        _require_digest(self.source_revision_sha256, label="source_revision_sha256")
        _require_digest(
            self.expected_registry_binding_sha256,
            label="expected_registry_binding_sha256",
        )
        if not isinstance(self.binding_snapshot, CapabilityBindingSnapshot):
            raise ValueError("binding_snapshot must be a code-owned snapshot")
        if not isinstance(self.scheduler, SupervisorSchedulerAdmissionSnapshot):
            raise ValueError("scheduler must be a typed admission snapshot")
        if type(self.max_steps) is not int or type(self.max_review_rounds) is not int:
            raise ValueError("promotion bounds must be integers")
        _require_digest(self.latency_budget_sha256, label="latency_budget_sha256")
        _require_positive_count(self.latency_budget_ms, label="latency_budget_ms")
        if self.latency_budget_ms > _MAX_LATENCY_MS:
            raise ValueError("latency_budget_ms exceeds the trace contract")
        _require_optional_digest(self.actor_binding_sha256, label="actor_binding_sha256")


@dataclass(frozen=True, slots=True)
class AssistPromotionLiveEvidence:
    """Body-free live facts; this object cannot accept itself.

    The optional precursor is the predecessor assist evidence's canonical
    payload digest, not its formatting-sensitive file digest.
    """

    evidence_id: str
    authority: AssistPromotionEvidenceAuthority
    observed_mode: SupervisorMode
    task_class: TaskClass
    source_revision_sha256: str
    promotion_policy_sha256: str
    observed_policy_id: str
    observed_policy_sha256: str
    target_policy_id: str
    target_policy_sha256: str
    runtime_profile_id: str
    runtime_profile_manifest_sha256: str
    registry_binding_sha256: str
    baseline_file_sha256: str
    baseline_report_sha256: str
    operator_attestation_sha256: str
    precursor_assist_promotion_evidence_sha256: str | None
    max_steps: int
    max_review_rounds: int
    observation_count: int
    joined_trace_count: int
    representative_window_attested: bool
    primary_fallback_proven: bool
    laptop_unavailable_fallback_proven: bool
    final_authority_recheck_proven: bool
    primary_publication_owner_proven: bool
    hidden_owner_count: int
    duplicate_capability_count: int
    duplicate_effect_count: int
    duplicate_publication_count: int
    false_completion_regression_count: int
    product_evidence: AssistPromotionReadinessEvidence | AssistPromotionOutcomeEvidence

    def __post_init__(self) -> None:
        _require_safe_id(self.evidence_id, label="evidence_id")
        if not isinstance(self.authority, AssistPromotionEvidenceAuthority):
            raise ValueError("evidence authority must be typed")
        if not isinstance(self.observed_mode, SupervisorMode):
            raise ValueError("observed_mode must be typed")
        if not isinstance(self.task_class, TaskClass):
            raise ValueError("evidence task_class must be typed")
        for label, value in (
            ("source_revision_sha256", self.source_revision_sha256),
            ("promotion_policy_sha256", self.promotion_policy_sha256),
            ("observed_policy_sha256", self.observed_policy_sha256),
            ("target_policy_sha256", self.target_policy_sha256),
            ("runtime_profile_manifest_sha256", self.runtime_profile_manifest_sha256),
            ("registry_binding_sha256", self.registry_binding_sha256),
            ("baseline_file_sha256", self.baseline_file_sha256),
            ("baseline_report_sha256", self.baseline_report_sha256),
            ("operator_attestation_sha256", self.operator_attestation_sha256),
        ):
            _require_digest(value, label=label)
        if self.observed_mode is SupervisorMode.SHADOW:
            if self.precursor_assist_promotion_evidence_sha256 is not None:
                raise ValueError("shadow readiness evidence cannot carry an assist precursor")
        elif self.observed_mode is SupervisorMode.ASSIST:
            _require_digest(
                self.precursor_assist_promotion_evidence_sha256,
                label="precursor_assist_promotion_evidence_sha256",
            )
        else:
            raise ValueError("evidence observed_mode must be shadow or assist")
        _require_safe_id(self.observed_policy_id, label="observed_policy_id")
        _require_safe_id(self.target_policy_id, label="target_policy_id")
        _require_safe_id(self.runtime_profile_id, label="runtime_profile_id")
        if type(self.max_steps) is not int or type(self.max_review_rounds) is not int:
            raise ValueError("evidence bounds must be integers")
        for label in (
            "observation_count",
            "joined_trace_count",
            "hidden_owner_count",
            "duplicate_capability_count",
            "duplicate_effect_count",
            "duplicate_publication_count",
            "false_completion_regression_count",
        ):
            _require_count(getattr(self, label), label=label)
        for label in (
            "representative_window_attested",
            "primary_fallback_proven",
            "laptop_unavailable_fallback_proven",
            "final_authority_recheck_proven",
            "primary_publication_owner_proven",
        ):
            _require_bool(getattr(self, label), label=label)
        if self.joined_trace_count > self.observation_count:
            raise ValueError("joined_trace_count exceeds observation_count")
        if not isinstance(
            self.product_evidence,
            (AssistPromotionReadinessEvidence, AssistPromotionOutcomeEvidence),
        ):
            raise ValueError("product_evidence must be typed")

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_ASSIST_PROMOTION_SCHEMA,
            "evidence_id": self.evidence_id,
            "authority": self.authority.value,
            "observed_mode": self.observed_mode.value,
            "task_class": self.task_class.value,
            "source_revision_sha256": self.source_revision_sha256,
            "promotion_policy_sha256": self.promotion_policy_sha256,
            "observed_policy_id": self.observed_policy_id,
            "observed_policy_sha256": self.observed_policy_sha256,
            "target_policy_id": self.target_policy_id,
            "target_policy_sha256": self.target_policy_sha256,
            "runtime_profile_id": self.runtime_profile_id,
            "runtime_profile_manifest_sha256": self.runtime_profile_manifest_sha256,
            "registry_binding_sha256": self.registry_binding_sha256,
            "baseline_file_sha256": self.baseline_file_sha256,
            "baseline_report_sha256": self.baseline_report_sha256,
            "operator_attestation_sha256": self.operator_attestation_sha256,
            "precursor_assist_promotion_evidence_sha256": (self.precursor_assist_promotion_evidence_sha256),
            "max_steps": self.max_steps,
            "max_review_rounds": self.max_review_rounds,
            "observation_count": self.observation_count,
            "joined_trace_count": self.joined_trace_count,
            "representative_window_attested": self.representative_window_attested,
            "primary_fallback_proven": self.primary_fallback_proven,
            "laptop_unavailable_fallback_proven": self.laptop_unavailable_fallback_proven,
            "final_authority_recheck_proven": self.final_authority_recheck_proven,
            "primary_publication_owner_proven": self.primary_publication_owner_proven,
            "hidden_owner_count": self.hidden_owner_count,
            "duplicate_capability_count": self.duplicate_capability_count,
            "duplicate_effect_count": self.duplicate_effect_count,
            "duplicate_publication_count": self.duplicate_publication_count,
            "false_completion_regression_count": self.false_completion_regression_count,
            "product_evidence": self.product_evidence.payload(),
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


@dataclass(frozen=True, slots=True)
class AssistPromotionOperatorGate:
    """Independent, evidence-bound operator switch; closed by default."""

    enabled: bool = False
    gate_id: str = SUPERVISOR_ASSIST_PROMOTION_GATE_ID
    promotion_policy_sha256: str = SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256
    target_mode: SupervisorMode = SupervisorMode.OFF
    task_class: TaskClass = TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB
    source_revision_sha256: str | None = None
    registry_binding_sha256: str | None = None
    accepted_evidence_sha256: str | None = None
    canary_actor_bindings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_bool(self.enabled, label="enabled")
        _require_safe_id(self.gate_id, label="gate_id")
        _require_digest(self.promotion_policy_sha256, label="promotion_policy_sha256")
        if not isinstance(self.target_mode, SupervisorMode):
            raise ValueError("target_mode must be typed")
        if not isinstance(self.task_class, TaskClass):
            raise ValueError("gate task_class must be typed")
        _require_optional_digest(self.source_revision_sha256, label="source_revision_sha256")
        _require_optional_digest(
            self.registry_binding_sha256,
            label="registry_binding_sha256",
        )
        _require_optional_digest(
            self.accepted_evidence_sha256,
            label="accepted_evidence_sha256",
        )
        if type(self.canary_actor_bindings) is not tuple or len(self.canary_actor_bindings) > 32:
            raise ValueError("canary actor allowlist must be a bounded tuple")
        for actor in self.canary_actor_bindings:
            _require_digest(actor, label="canary actor binding")
        if len(set(self.canary_actor_bindings)) != len(self.canary_actor_bindings):
            raise ValueError("canary actor allowlist contains duplicates")


@dataclass(frozen=True, slots=True)
class AssistPromotionDecision:
    """Non-executing result of the promotion gate."""

    promotion_admitted: bool
    readiness: AssistPromotionReadiness
    reason: AssistPromotionReason
    requested_mode: SupervisorMode
    admitted_mode: SupervisorMode
    source_ready: bool
    live_evidence_ready: bool
    operator_gate_bound: bool
    evidence_sha256: str | None
    execution_authorized: bool = False
    publication_authorized: bool = False
    storage_write_authorized: bool = False

    @property
    def reason_code(self) -> str:
        return self.reason.value


def _decision(
    candidate: AssistPromotionCandidate,
    reason: AssistPromotionReason,
    *,
    source_ready: bool,
    live_evidence_ready: bool = False,
    operator_gate_bound: bool = False,
    evidence_sha256: str | None = None,
    admitted: bool = False,
) -> AssistPromotionDecision:
    readiness = AssistPromotionReadiness.CLOSED
    if source_ready:
        readiness = (
            AssistPromotionReadiness.LIVE_EVIDENCE_READY
            if live_evidence_ready
            else AssistPromotionReadiness.SOURCE_READY
        )
    return AssistPromotionDecision(
        promotion_admitted=admitted,
        readiness=readiness,
        reason=reason,
        requested_mode=candidate.requested_mode,
        admitted_mode=candidate.requested_mode if admitted else SupervisorMode.OFF,
        source_ready=source_ready,
        live_evidence_ready=live_evidence_ready,
        operator_gate_bound=operator_gate_bound,
        evidence_sha256=evidence_sha256,
    )


def _scheduler_source_reason(
    candidate: AssistPromotionCandidate,
) -> AssistPromotionReason | None:
    scheduler = candidate.scheduler
    if not semantic_supervisor_policy.supervisor_product_policy_is_well_formed():
        return AssistPromotionReason.P1_POLICY_IDENTITY_DRIFT
    if not semantic_supervisor_policy.supervisor_assist_product_policy_is_well_formed():
        return AssistPromotionReason.ASSIST_POLICY_IDENTITY_DRIFT
    if scheduler.policy_id != _ASSIST_POLICY_ID or not hmac.compare_digest(
        scheduler.policy_sha256,
        _ASSIST_POLICY_SHA256,
    ):
        return AssistPromotionReason.ASSIST_POLICY_IDENTITY_DRIFT
    if scheduler.runtime_profile_id != _P1_PROFILE_ID or not hmac.compare_digest(
        scheduler.runtime_profile_manifest_sha256,
        _P1_PROFILE_MANIFEST_SHA256,
    ):
        return AssistPromotionReason.RUNTIME_PROFILE_IDENTITY_DRIFT
    if (
        scheduler.workload != _P1_WORKLOAD
        or scheduler.requested_mode != candidate.requested_mode.value
        or scheduler.effective_mode != SupervisorMode.SHADOW.value
        or scheduler.profile_admission != "accepted"
        or scheduler.closed_reason != "admitted"
    ):
        return AssistPromotionReason.SCHEDULER_IDENTITY_DRIFT
    if not scheduler.workload_available:
        return AssistPromotionReason.SCHEDULER_WORKLOAD_UNAVAILABLE
    return None


def _registry_is_exact(candidate: AssistPromotionCandidate) -> bool:
    snapshot = candidate.binding_snapshot
    if not hmac.compare_digest(
        snapshot.digest_hex(),
        candidate.expected_registry_binding_sha256,
    ):
        return False
    for capability_id, expected in (
        (FILE_CURRENT_READ_ID, _FILE_BINDING),
        (WEB_SEARCH_CURRENT_ID, _WEB_BINDING),
    ):
        binding = snapshot.binding_for(capability_id)
        if binding is None or not binding.available:
            return False
        if binding.effect_class is not CapabilityEffectClass.READ:
            return False
        if (binding.security_id, binding.tool_id, binding.adapter_id) != expected:
            return False
    return True


def _source_reason(candidate: AssistPromotionCandidate) -> AssistPromotionReason | None:
    if candidate.requested_mode is SupervisorMode.OFF:
        return AssistPromotionReason.MODE_OFF
    if candidate.requested_mode is SupervisorMode.SHADOW:
        return AssistPromotionReason.SHADOW_NEVER_OWNS
    if candidate.requested_mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}:
        return AssistPromotionReason.MODE_NOT_ADMITTED
    if candidate.task_class is not TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB:
        return AssistPromotionReason.TASK_NOT_ADMITTED
    if (
        candidate.max_steps != SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS
        or candidate.max_review_rounds != SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS
    ):
        return AssistPromotionReason.BOUNDS_DRIFT
    scheduler_reason = _scheduler_source_reason(candidate)
    if scheduler_reason is not None:
        return scheduler_reason
    if not _registry_is_exact(candidate):
        return AssistPromotionReason.REGISTRY_BINDING_DRIFT
    return None


def _live_evidence_reason(
    candidate: AssistPromotionCandidate,
    evidence: AssistPromotionLiveEvidence | None,
) -> AssistPromotionReason | None:
    if evidence is None:
        return AssistPromotionReason.LIVE_EVIDENCE_REQUIRED
    if evidence.authority is not AssistPromotionEvidenceAuthority.PRODUCTION_JOINED:
        return AssistPromotionReason.PRODUCTION_JOINED_EVIDENCE_REQUIRED
    expected_stage = {
        SupervisorMode.ASSIST: SupervisorMode.SHADOW,
        SupervisorMode.CANARY: SupervisorMode.ASSIST,
    }[candidate.requested_mode]
    expected_observed_policy_id, expected_observed_policy_sha256 = (
        (_P1_POLICY_ID, _P1_POLICY_SHA256)
        if expected_stage is SupervisorMode.SHADOW
        else (_ASSIST_POLICY_ID, _ASSIST_POLICY_SHA256)
    )
    if evidence.observed_mode is not expected_stage:
        return AssistPromotionReason.EVIDENCE_STAGE_DRIFT
    if (
        evidence.task_class is not candidate.task_class
        or not hmac.compare_digest(
            evidence.source_revision_sha256,
            candidate.source_revision_sha256,
        )
        or not hmac.compare_digest(
            evidence.promotion_policy_sha256,
            SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
        )
        or evidence.observed_policy_id != expected_observed_policy_id
        or not hmac.compare_digest(
            evidence.observed_policy_sha256,
            expected_observed_policy_sha256,
        )
        or evidence.target_policy_id != _ASSIST_POLICY_ID
        or not hmac.compare_digest(
            evidence.target_policy_sha256,
            _ASSIST_POLICY_SHA256,
        )
        or evidence.runtime_profile_id != _P1_PROFILE_ID
        or not hmac.compare_digest(
            evidence.runtime_profile_manifest_sha256,
            _P1_PROFILE_MANIFEST_SHA256,
        )
        or not hmac.compare_digest(
            evidence.registry_binding_sha256,
            candidate.expected_registry_binding_sha256,
        )
        or evidence.max_steps != candidate.max_steps
        or evidence.max_review_rounds != candidate.max_review_rounds
    ):
        return AssistPromotionReason.EVIDENCE_IDENTITY_DRIFT
    product = evidence.product_evidence
    if candidate.requested_mode is SupervisorMode.ASSIST:
        if not isinstance(product, AssistPromotionReadinessEvidence):
            return AssistPromotionReason.EVIDENCE_STAGE_DRIFT
        if (
            evidence.observation_count < SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
            or evidence.joined_trace_count != evidence.observation_count
            or product.call_rate_observation_count != evidence.observation_count
            or product.baseline_observation_count < SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
            or product.readiness_observation_count < SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
            or product.readiness_observation_count > evidence.observation_count
            or product.user_visible_observation_count != product.readiness_observation_count
        ):
            return AssistPromotionReason.EVIDENCE_WINDOW_INCOMPLETE
    else:
        if not isinstance(product, AssistPromotionOutcomeEvidence):
            return AssistPromotionReason.EVIDENCE_STAGE_DRIFT
        if (
            evidence.observation_count < SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
            or evidence.joined_trace_count != evidence.observation_count
            or product.call_rate_observation_count != evidence.observation_count
            or product.baseline_observation_count < SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
            or product.promoted_observation_count < SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
            or product.promoted_observation_count > evidence.observation_count
            or product.latency_observation_count != product.promoted_observation_count
            or product.user_visible_observation_count != product.promoted_observation_count
        ):
            return AssistPromotionReason.EVIDENCE_WINDOW_INCOMPLETE
    if (
        product.latency_budget_target_mode is not candidate.requested_mode
        or not hmac.compare_digest(
            product.latency_budget_source_revision_sha256,
            candidate.source_revision_sha256,
        )
        or not hmac.compare_digest(
            product.latency_budget_sha256,
            candidate.latency_budget_sha256,
        )
        or product.latency_budget_ms != candidate.latency_budget_ms
    ):
        return AssistPromotionReason.EVIDENCE_IDENTITY_DRIFT
    if not all(
        (
            evidence.representative_window_attested,
            evidence.primary_fallback_proven,
            evidence.laptop_unavailable_fallback_proven,
            evidence.final_authority_recheck_proven,
            evidence.primary_publication_owner_proven,
        )
    ) or any(
        (
            evidence.hidden_owner_count,
            evidence.duplicate_capability_count,
            evidence.duplicate_effect_count,
            evidence.duplicate_publication_count,
            evidence.false_completion_regression_count,
        )
    ):
        return AssistPromotionReason.EVIDENCE_INVARIANT_FAILED
    if isinstance(product, AssistPromotionReadinessEvidence):
        if product.baseline_failure_class_count < 1:
            return AssistPromotionReason.PRODUCT_QUALITY_NOT_PROVEN
        latency_observation_count = product.readiness_observation_count
    else:
        if product.quality_basis is AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT:
            measurable_improvement = (
                product.promoted_complete_count * product.baseline_observation_count
                > product.baseline_complete_count * product.promoted_observation_count
            )
            if not measurable_improvement:
                return AssistPromotionReason.PRODUCT_QUALITY_NOT_PROVEN
        elif product.baseline_failure_class_count < 1 or product.promoted_failure_class_count != 0:
            return AssistPromotionReason.PRODUCT_QUALITY_NOT_PROVEN
        latency_observation_count = product.latency_observation_count
    if (
        product.latency_max_ms > product.latency_budget_ms
        or product.latency_total_ms > product.latency_budget_ms * latency_observation_count
    ):
        return AssistPromotionReason.PRODUCT_LATENCY_BUDGET_EXCEEDED
    if (
        product.unnecessary_supervisor_invocation_count * 10_000
        > SUPERVISOR_ASSIST_MAX_UNNECESSARY_CALL_RATE_BPS * product.call_rate_observation_count
    ):
        return AssistPromotionReason.UNNECESSARY_SUPERVISOR_CALL_RATE_EXCEEDED
    if product.user_visible_regression_count:
        return AssistPromotionReason.USER_VISIBLE_REGRESSION_OBSERVED
    return None


def _gate_reason(
    candidate: AssistPromotionCandidate,
    evidence: AssistPromotionLiveEvidence,
    gate: AssistPromotionOperatorGate,
) -> tuple[AssistPromotionReason | None, bool]:
    if not gate.enabled:
        return AssistPromotionReason.OPERATOR_GATE_CLOSED, False
    if (
        gate.gate_id != SUPERVISOR_ASSIST_PROMOTION_GATE_ID
        or not hmac.compare_digest(
            gate.promotion_policy_sha256,
            SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
        )
        or gate.target_mode is not candidate.requested_mode
        or gate.task_class is not candidate.task_class
        or gate.source_revision_sha256 is None
        or not hmac.compare_digest(
            gate.source_revision_sha256,
            candidate.source_revision_sha256,
        )
        or gate.registry_binding_sha256 is None
        or not hmac.compare_digest(
            gate.registry_binding_sha256,
            candidate.expected_registry_binding_sha256,
        )
    ):
        return AssistPromotionReason.OPERATOR_GATE_DRIFT, False
    evidence_sha256 = evidence.canonical_sha256()
    if gate.accepted_evidence_sha256 is None or not hmac.compare_digest(
        gate.accepted_evidence_sha256,
        evidence_sha256,
    ):
        return AssistPromotionReason.OPERATOR_EVIDENCE_NOT_BOUND, False
    if candidate.requested_mode is SupervisorMode.ASSIST:
        if gate.canary_actor_bindings:
            return AssistPromotionReason.ASSIST_ALLOWLIST_NOT_ADMITTED, False
    else:
        if not gate.canary_actor_bindings:
            return AssistPromotionReason.CANARY_ALLOWLIST_REQUIRED, False
        actor = candidate.actor_binding_sha256
        if actor is None or not any(
            hmac.compare_digest(actor, allowed) for allowed in gate.canary_actor_bindings
        ):
            return AssistPromotionReason.CANARY_ACTOR_NOT_ALLOWLISTED, False
    return None, True


def admit_supervisor_assist_promotion(
    candidate: AssistPromotionCandidate,
    evidence: AssistPromotionLiveEvidence | None,
    operator_gate: AssistPromotionOperatorGate,
) -> AssistPromotionDecision:
    """Evaluate promotion only; never execute, publish, persist, or own a turn."""

    if (
        not isinstance(candidate, AssistPromotionCandidate)
        or not isinstance(
            operator_gate,
            AssistPromotionOperatorGate,
        )
        or (evidence is not None and not isinstance(evidence, AssistPromotionLiveEvidence))
    ):
        if isinstance(candidate, AssistPromotionCandidate):
            return _decision(
                candidate,
                AssistPromotionReason.MALFORMED,
                source_ready=False,
            )
        raise TypeError("assist promotion requires a typed candidate")

    source_reason = _source_reason(candidate)
    if source_reason is not None:
        return _decision(candidate, source_reason, source_ready=False)

    live_reason = _live_evidence_reason(candidate, evidence)
    live_ready = live_reason is None
    evidence_sha256 = evidence.canonical_sha256() if live_ready and evidence is not None else None

    if not candidate.scheduler.runtime_available:
        return _decision(
            candidate,
            AssistPromotionReason.LAPTOP_RUNTIME_UNAVAILABLE,
            source_ready=True,
            live_evidence_ready=live_ready,
            evidence_sha256=evidence_sha256,
        )
    if not operator_gate.enabled:
        return _decision(
            candidate,
            AssistPromotionReason.OPERATOR_GATE_CLOSED,
            source_ready=True,
            live_evidence_ready=live_ready,
            evidence_sha256=evidence_sha256,
        )
    if live_reason is not None or evidence is None:
        return _decision(
            candidate,
            live_reason or AssistPromotionReason.LIVE_EVIDENCE_REQUIRED,
            source_ready=True,
        )

    gate_reason, gate_bound = _gate_reason(candidate, evidence, operator_gate)
    if gate_reason is not None:
        return _decision(
            candidate,
            gate_reason,
            source_ready=True,
            live_evidence_ready=True,
            evidence_sha256=evidence_sha256,
        )
    return _decision(
        candidate,
        AssistPromotionReason.ADMITTED,
        source_ready=True,
        live_evidence_ready=True,
        operator_gate_bound=gate_bound,
        evidence_sha256=evidence_sha256,
        admitted=True,
    )


__all__ = [
    "AssistPromotionCandidate",
    "AssistPromotionDecision",
    "AssistPromotionEvidenceAuthority",
    "AssistPromotionLiveEvidence",
    "AssistPromotionOperatorGate",
    "AssistPromotionOutcomeEvidence",
    "AssistPromotionQualityBasis",
    "AssistPromotionReadinessEvidence",
    "AssistPromotionReadiness",
    "AssistPromotionReason",
    "SUPERVISOR_ASSIST_PROMOTION_GATE_ID",
    "SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS",
    "SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS",
    "SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS",
    "SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256",
    "SUPERVISOR_ASSIST_PROMOTION_SCHEMA",
    "SUPERVISOR_ASSIST_OUTCOME_EVIDENCE_SCHEMA",
    "SUPERVISOR_ASSIST_READINESS_EVIDENCE_SCHEMA",
    "SUPERVISOR_ASSIST_MAX_UNNECESSARY_CALL_RATE_BPS",
    "SupervisorSchedulerAdmissionSnapshot",
    "admit_supervisor_assist_promotion",
]
