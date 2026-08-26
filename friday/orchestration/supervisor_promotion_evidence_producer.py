"""Pure, fail-closed producer for promoted-supervisor evidence artifacts.

The production baseline is intentionally only a body-free candidate.  This
module accepts it only by exact file digest and self-digest, validates the
closed v2 aggregate, and combines it with an explicit operator attestation.
It never enables promotion, edits configuration, or manufactures authority
from synthetic/isolated observations.
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
from typing import Any, cast

from friday import semantic_supervisor_policy
from friday.orchestration.supervisor_assist_promotion import (
    SUPERVISOR_ASSIST_MAX_UNNECESSARY_CALL_RATE_BPS,
    SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS,
    SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS,
    SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS,
    SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
    AssistPromotionEvidenceAuthority,
    AssistPromotionLiveEvidence,
    AssistPromotionOutcomeEvidence,
    AssistPromotionQualityBasis,
    AssistPromotionReadinessEvidence,
)
from friday.orchestration.supervisor_contracts import (
    SupervisorMode,
    TaskClass,
    canonical_dumps,
    canonical_sha256,
)
from friday.orchestration.supervisor_production_baseline import (
    SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
    SUPERVISOR_PRODUCTION_BASELINE_KIND,
    SUPERVISOR_PRODUCTION_BASELINE_SCHEMA,
)
from friday.orchestration.supervisor_promoted_product_event import (
    PromotedProductEventError,
    SupervisorLatencyBudgetDocument,
    load_accepted_supervisor_latency_budget,
)
from friday.orchestration.supervisor_representative_window_attestation import (
    REPRESENTATIVE_WINDOW_ATTESTATION_KEYS,
    REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA,
    REPRESENTATIVE_WINDOW_AUTHORITY,
    REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA,
    representative_window_canonical,
    representative_window_sha256,
)

SUPERVISOR_PROMOTION_OPERATOR_ATTESTATION_SCHEMA = (
    "friday.semantic-supervisor-promotion-operator-attestation.v1"
)
SUPERVISOR_PROMOTION_ARTIFACT_RECEIPT_SCHEMA = "friday.semantic-supervisor-promotion-artifact-receipt.v1"
SUPERVISOR_PROMOTION_BUNDLE_SCHEMA = "friday.semantic-supervisor-promotion-bundle.v1"
SUPERVISOR_PROMOTION_BUNDLE_RECEIPT_SCHEMA = "friday.semantic-supervisor-promotion-bundle-receipt.v1"

_MAX_BASELINE_BYTES = 1_048_576
_MAX_BUNDLE_BYTES = 2_097_152
_MAX_COUNT_MAP_ITEMS = 256
_MAX_SAMPLE_ROWS = 100_000
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_METRIC_KEY_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}\Z")
_PROCESS_ACCEPTANCE_AUTHORITY = object()
_PROCESS_ACCEPTANCE_KEY = secrets.token_bytes(32)

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "evidence",
        "sample",
        "primary_baseline",
        "supervisor_join",
        "product_windows",
        "report_sha256",
    }
)
_EVIDENCE_KEYS = frozenset(
    {
        "kind",
        "body_free",
        "production_acceptance",
        "acceptance_authority",
        "representative_window_attested",
        "promotion_authority",
    }
)
_SAMPLE_KEYS = frozenset(
    {
        "limit",
        "turn_traces",
        "joined_supervisor_events",
        "promoted_product_events",
        "malformed_turn_traces",
        "malformed_joined_events",
        "malformed_promoted_product_events",
        "duplicate_turn_trace_digests",
        "duplicate_shadow_product_events",
        "duplicate_promoted_product_events",
        "unmatched_shadow_product_events",
        "unmatched_promoted_product_events",
    }
)
_ANOMALY_SAMPLE_KEYS = frozenset(
    {
        "malformed_turn_traces",
        "malformed_joined_events",
        "malformed_promoted_product_events",
        "duplicate_turn_trace_digests",
        "duplicate_shadow_product_events",
        "duplicate_promoted_product_events",
        "unmatched_shadow_product_events",
        "unmatched_promoted_product_events",
    }
)
_PRIMARY_BASELINE_KEYS = frozenset(
    {
        "intent_counts",
        "playbook_counts",
        "completion_counts",
        "publication_counts",
        "failure_counts",
        "authority_rechecked_count",
        "partial_coverage_count",
        "state_restored_count",
    }
)
_SUPERVISOR_JOIN_KEYS = frozenset(
    {
        "task_counts",
        "skip_counts",
        "parse_counts",
        "policy_reason_counts",
        "planner_latency_bucket_counts",
        "actual_completion_counts",
        "actual_publication_counts",
        "actual_capability_outcome_counts",
        "invoked_count",
        "admitted_count",
        "final_authority_rechecked_count",
        "state_restored_count",
        "retry_occurred_count",
    }
)
_METRIC_KEYS = frozenset(
    {
        "schema",
        "stage",
        "observation_count",
        "completion_counts",
        "complete_count",
        "failure_class_counts",
        "latency_observation_count",
        "latency_total_ms",
        "latency_max_ms",
        "window_sha256",
    }
)
_SHADOW_KEYS = frozenset(
    {
        "schema",
        "mode",
        "production_joined",
        "actual_promoted_execution",
        "quality_claim",
        "observation_count",
        "joined_trace_count",
        "baseline",
        "readiness_observation_count",
        "call_rate_observation_count",
        "supervisor_invocation_count",
        "unnecessary_supervisor_invocation_count",
        "user_visible_observation_count",
        "user_visible_regression_count",
        "readiness_witness_sha256",
    }
)
_PROMOTED_KEYS = frozenset(
    {
        "schema",
        "mode",
        "production_joined",
        "actual_promoted_execution",
        "observation_count",
        "joined_trace_count",
        "promotion_evidence_count",
        "promotion_evidence_sha256",
        "promoted",
        "call_rate_observation_count",
        "supervisor_invocation_count",
        "unnecessary_supervisor_invocation_count",
        "user_visible_observation_count",
        "user_visible_regression_count",
        "product_window_sha256",
    }
)
_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "target_mode",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "representative_window_attested",
        "primary_fallback_proven",
        "laptop_unavailable_fallback_proven",
        "final_authority_recheck_proven",
        "primary_publication_owner_proven",
        "zero_hidden_owners_attested",
        "zero_duplicate_capabilities_attested",
        "zero_duplicate_effects_attested",
        "zero_duplicate_publications_attested",
        "zero_false_completion_regressions_attested",
        "precursor_assist_promotion_evidence_sha256",
        "quality_basis",
    }
)
_BUNDLE_KEYS = frozenset(
    {
        "schema",
        "body_free",
        "baseline",
        "operator_attestation",
        "promotion_evidence",
        "representative_window_issue",
        "producer_receipt",
        "producer_receipt_sha256",
    }
)
_REPRESENTATIVE_WINDOW_ISSUE_KEYS = frozenset(
    {
        "schema",
        "status",
        "server_attestation",
        "server_attestation_sha256",
        "attestation_lookup_token",
        "lookup_token_sha256",
        "state_version",
    }
)


class SupervisorPromotionEvidenceProducerError(ValueError):
    """An input cannot prove the exact body-free evidence contract."""


class SupervisorPromotionArtifactKind(StrEnum):
    LATENCY_BUDGET = "latency_budget"
    PROMOTION_EVIDENCE = "promotion_evidence"


def _digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise SupervisorPromotionEvidenceProducerError(f"{label} must be a lowercase SHA-256")
    return value


def _count(value: object, *, label: str, maximum: int = _MAX_SAMPLE_ROWS) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise SupervisorPromotionEvidenceProducerError(f"{label} is outside its count bound")
    return value


def _exact_dict(value: object, expected: frozenset[str], *, label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise SupervisorPromotionEvidenceProducerError(f"{label} keys do not match")
    return cast(dict[str, Any], value)


def _count_map(value: object, *, label: str) -> tuple[tuple[str, int], ...]:
    if type(value) is not dict or len(value) > _MAX_COUNT_MAP_ITEMS:
        raise SupervisorPromotionEvidenceProducerError(f"{label} is not a bounded count map")
    result: list[tuple[str, int]] = []
    for key, raw_count in cast(dict[object, object], value).items():
        if type(key) is not str or _SAFE_METRIC_KEY_RE.fullmatch(key) is None:
            raise SupervisorPromotionEvidenceProducerError(f"{label} has an invalid key")
        result.append((key, _count(raw_count, label=label)))
    return tuple(sorted(result))


def _sum_counts(values: tuple[tuple[str, int], ...]) -> int:
    return sum(count for _key, count in values)


def canonical_json_file_bytes(payload: Mapping[str, object]) -> bytes:
    """Return the one producer-owned canonical JSON-file representation."""

    if not isinstance(payload, Mapping):
        raise TypeError("canonical artifact payload must be a mapping")
    return (canonical_dumps(dict(payload)) + "\n").encode("utf-8")


def _process_acceptance_seal(*, kind: str, fields: tuple[object, ...]) -> str:
    return hmac.new(
        _PROCESS_ACCEPTANCE_KEY,
        repr((kind, fields)).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class SupervisorProductMetricWindow:
    stage: SupervisorMode
    observation_count: int
    complete_count: int
    failure_class_counts: tuple[tuple[str, int], ...]
    latency_observation_count: int
    latency_total_ms: int
    latency_max_ms: int
    window_sha256: str

    def failure_count(self, failure_class_id: str) -> int:
        return dict(self.failure_class_counts).get(failure_class_id, 0)


@dataclass(frozen=True, slots=True)
class SupervisorShadowReadinessWindow:
    observation_count: int
    joined_trace_count: int
    baseline: SupervisorProductMetricWindow
    readiness_observation_count: int
    call_rate_observation_count: int
    supervisor_invocation_count: int
    unnecessary_supervisor_invocation_count: int
    user_visible_observation_count: int
    user_visible_regression_count: int
    readiness_witness_sha256: str


@dataclass(frozen=True, slots=True)
class SupervisorPromotedExecutionWindow:
    mode: SupervisorMode
    observation_count: int
    joined_trace_count: int
    promotion_evidence_count: int
    promotion_evidence_sha256: str | None
    promoted: SupervisorProductMetricWindow
    call_rate_observation_count: int
    supervisor_invocation_count: int
    unnecessary_supervisor_invocation_count: int
    user_visible_observation_count: int
    user_visible_regression_count: int
    product_window_sha256: str


@dataclass(frozen=True, slots=True)
class AcceptedSupervisorProductionBaseline:
    """Exact-hash, self-digested v2 aggregate; never an authority by itself."""

    file_sha256: str
    report_sha256: str
    shadow_readiness: SupervisorShadowReadinessWindow
    assist_execution: SupervisorPromotedExecutionWindow
    canary_execution: SupervisorPromotedExecutionWindow
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        expected = _process_acceptance_seal(
            kind="production-baseline",
            fields=(
                self.file_sha256,
                self.report_sha256,
                self.shadow_readiness,
                self.assist_execution,
                self.canary_execution,
            ),
        )
        if (
            self._process_authority is not _PROCESS_ACCEPTANCE_AUTHORITY
            or type(self._process_seal_sha256) is not str
            or not hmac.compare_digest(self._process_seal_sha256, expected)
        ):
            raise SupervisorPromotionEvidenceProducerError("baseline was not accepted by this process")


@dataclass(frozen=True, slots=True)
class AcceptedCanonicalSupervisorLatencyBudget:
    """Existing typed budget plus producer-canonical file acceptance."""

    document: SupervisorLatencyBudgetDocument
    document_sha256: str
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        expected = _process_acceptance_seal(
            kind="latency-budget",
            fields=(self.document, self.document_sha256),
        )
        if (
            type(self.document) is not SupervisorLatencyBudgetDocument
            or self._process_authority is not _PROCESS_ACCEPTANCE_AUTHORITY
            or type(self._process_seal_sha256) is not str
            or not hmac.compare_digest(self._process_seal_sha256, expected)
        ):
            raise SupervisorPromotionEvidenceProducerError("budget was not accepted by this process")
        _digest(self.document_sha256, label="canonical budget file digest")


@dataclass(frozen=True, slots=True)
class _AcceptedRepresentativeWindowIssue:
    """Structurally closed server issue envelope retained for one-shot consume."""

    target_mode: SupervisorMode
    observed_mode: SupervisorMode
    server_attestation_sha256: str
    lookup_token_sha256: str
    observer_runner_sha256: str
    representative_window_sha256: str
    precursor_assist_promotion_evidence_sha256: str | None
    canonical_raw: bytes = field(repr=False, compare=False)
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.target_mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}:
            raise SupervisorPromotionEvidenceProducerError("representative-window issue mode is invalid")
        expected_observed = (
            SupervisorMode.SHADOW if self.target_mode is SupervisorMode.ASSIST else SupervisorMode.ASSIST
        )
        if self.observed_mode is not expected_observed:
            raise SupervisorPromotionEvidenceProducerError(
                "representative-window observed mode does not match"
            )
        for label, value in (
            ("server_attestation_sha256", self.server_attestation_sha256),
            ("lookup_token_sha256", self.lookup_token_sha256),
            ("observer_runner_sha256", self.observer_runner_sha256),
            ("representative_window_sha256", self.representative_window_sha256),
        ):
            _digest(value, label=label)
        if self.target_mode is SupervisorMode.ASSIST:
            if self.precursor_assist_promotion_evidence_sha256 is not None:
                raise SupervisorPromotionEvidenceProducerError(
                    "assist representative-window issue carries a precursor"
                )
        else:
            _digest(
                self.precursor_assist_promotion_evidence_sha256,
                label="representative-window precursor",
            )
        expected = _process_acceptance_seal(
            kind="representative-window-issue",
            fields=(
                self.target_mode,
                self.observed_mode,
                self.server_attestation_sha256,
                self.lookup_token_sha256,
                self.observer_runner_sha256,
                self.representative_window_sha256,
                self.precursor_assist_promotion_evidence_sha256,
                self.canonical_raw,
            ),
        )
        if (
            type(self.canonical_raw) is not bytes
            or self._process_authority is not _PROCESS_ACCEPTANCE_AUTHORITY
            or type(self._process_seal_sha256) is not str
            or not hmac.compare_digest(self._process_seal_sha256, expected)
        ):
            raise SupervisorPromotionEvidenceProducerError(
                "representative-window issue was not accepted by this process"
            )

    def payload(self) -> dict[str, Any]:
        return _exact_dict(
            _decode_closed_json(self.canonical_raw),
            _REPRESENTATIVE_WINDOW_ISSUE_KEYS,
            label="representative-window issue",
        )


@dataclass(frozen=True, slots=True)
class SupervisorPromotionOperatorAttestation:
    """Explicit operator claims kept separate from measured baseline facts."""

    target_mode: SupervisorMode
    baseline_file_sha256: str
    baseline_report_sha256: str
    latency_budget_file_sha256: str
    source_revision_sha256: str
    registry_binding_sha256: str
    representative_window_attested: bool
    primary_fallback_proven: bool
    laptop_unavailable_fallback_proven: bool
    final_authority_recheck_proven: bool
    primary_publication_owner_proven: bool
    zero_hidden_owners_attested: bool
    zero_duplicate_capabilities_attested: bool
    zero_duplicate_effects_attested: bool
    zero_duplicate_publications_attested: bool
    zero_false_completion_regressions_attested: bool
    precursor_assist_promotion_evidence_sha256: str | None = None
    quality_basis: AssistPromotionQualityBasis | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target_mode, SupervisorMode) or self.target_mode not in {
            SupervisorMode.ASSIST,
            SupervisorMode.CANARY,
        }:
            raise SupervisorPromotionEvidenceProducerError("attestation target mode is not promoted")
        for label, value in (
            ("baseline_file_sha256", self.baseline_file_sha256),
            ("baseline_report_sha256", self.baseline_report_sha256),
            ("latency_budget_file_sha256", self.latency_budget_file_sha256),
            ("source_revision_sha256", self.source_revision_sha256),
            ("registry_binding_sha256", self.registry_binding_sha256),
        ):
            _digest(value, label=label)
        for label in (
            "representative_window_attested",
            "primary_fallback_proven",
            "laptop_unavailable_fallback_proven",
            "final_authority_recheck_proven",
            "primary_publication_owner_proven",
            "zero_hidden_owners_attested",
            "zero_duplicate_capabilities_attested",
            "zero_duplicate_effects_attested",
            "zero_duplicate_publications_attested",
            "zero_false_completion_regressions_attested",
        ):
            if type(getattr(self, label)) is not bool:
                raise SupervisorPromotionEvidenceProducerError(f"{label} must be boolean")
        if self.target_mode is SupervisorMode.ASSIST:
            if self.precursor_assist_promotion_evidence_sha256 is not None or self.quality_basis is not None:
                raise SupervisorPromotionEvidenceProducerError(
                    "assist attestation must not claim a precursor or outcome basis"
                )
        else:
            _digest(
                self.precursor_assist_promotion_evidence_sha256,
                label="precursor_assist_promotion_evidence_sha256",
            )
            if not isinstance(self.quality_basis, AssistPromotionQualityBasis):
                raise SupervisorPromotionEvidenceProducerError("canary quality basis is required")

    def all_invariants_affirmed(self) -> bool:
        return all(
            (
                self.representative_window_attested,
                self.primary_fallback_proven,
                self.laptop_unavailable_fallback_proven,
                self.final_authority_recheck_proven,
                self.primary_publication_owner_proven,
                self.zero_hidden_owners_attested,
                self.zero_duplicate_capabilities_attested,
                self.zero_duplicate_effects_attested,
                self.zero_duplicate_publications_attested,
                self.zero_false_completion_regressions_attested,
            )
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_PROMOTION_OPERATOR_ATTESTATION_SCHEMA,
            "target_mode": self.target_mode.value,
            "baseline_file_sha256": self.baseline_file_sha256,
            "baseline_report_sha256": self.baseline_report_sha256,
            "latency_budget_file_sha256": self.latency_budget_file_sha256,
            "source_revision_sha256": self.source_revision_sha256,
            "registry_binding_sha256": self.registry_binding_sha256,
            "representative_window_attested": self.representative_window_attested,
            "primary_fallback_proven": self.primary_fallback_proven,
            "laptop_unavailable_fallback_proven": self.laptop_unavailable_fallback_proven,
            "final_authority_recheck_proven": self.final_authority_recheck_proven,
            "primary_publication_owner_proven": self.primary_publication_owner_proven,
            "zero_hidden_owners_attested": self.zero_hidden_owners_attested,
            "zero_duplicate_capabilities_attested": self.zero_duplicate_capabilities_attested,
            "zero_duplicate_effects_attested": self.zero_duplicate_effects_attested,
            "zero_duplicate_publications_attested": self.zero_duplicate_publications_attested,
            "zero_false_completion_regressions_attested": (self.zero_false_completion_regressions_attested),
            "precursor_assist_promotion_evidence_sha256": (self.precursor_assist_promotion_evidence_sha256),
            "quality_basis": self.quality_basis.value if self.quality_basis is not None else None,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


@dataclass(frozen=True, slots=True)
class SupervisorPromotionArtifactReceipt:
    """Body-free CLI receipt.  It explicitly records that nothing was enabled."""

    artifact_kind: SupervisorPromotionArtifactKind
    target_mode: SupervisorMode
    output_file_sha256: str
    canonical_payload_sha256: str
    source_revision_sha256: str
    baseline_file_sha256: str | None = None
    baseline_report_sha256: str | None = None
    latency_budget_file_sha256: str | None = None
    operator_attestation_sha256: str | None = None
    precursor_assist_promotion_evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_kind, SupervisorPromotionArtifactKind):
            raise SupervisorPromotionEvidenceProducerError("receipt artifact kind is not typed")
        if not isinstance(self.target_mode, SupervisorMode) or self.target_mode not in {
            SupervisorMode.ASSIST,
            SupervisorMode.CANARY,
        }:
            raise SupervisorPromotionEvidenceProducerError("receipt target mode is not promoted")
        for label, value in (
            ("output_file_sha256", self.output_file_sha256),
            ("canonical_payload_sha256", self.canonical_payload_sha256),
            ("source_revision_sha256", self.source_revision_sha256),
        ):
            _digest(value, label=label)
        optionals = (
            self.baseline_file_sha256,
            self.baseline_report_sha256,
            self.latency_budget_file_sha256,
            self.operator_attestation_sha256,
            self.precursor_assist_promotion_evidence_sha256,
        )
        for optional in optionals:
            if optional is not None:
                _digest(optional, label="receipt optional digest")
        if self.artifact_kind is SupervisorPromotionArtifactKind.LATENCY_BUDGET:
            if any(value is not None for value in optionals):
                raise SupervisorPromotionEvidenceProducerError("budget receipt carries evidence inputs")
        elif any(
            value is None
            for value in (
                self.baseline_file_sha256,
                self.baseline_report_sha256,
                self.latency_budget_file_sha256,
                self.operator_attestation_sha256,
            )
        ):
            raise SupervisorPromotionEvidenceProducerError("evidence receipt is incomplete")

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_PROMOTION_ARTIFACT_RECEIPT_SCHEMA,
            "artifact_kind": self.artifact_kind.value,
            "target_mode": self.target_mode.value,
            "output_file_sha256": self.output_file_sha256,
            "canonical_payload_sha256": self.canonical_payload_sha256,
            "source_revision_sha256": self.source_revision_sha256,
            "baseline_file_sha256": self.baseline_file_sha256,
            "baseline_report_sha256": self.baseline_report_sha256,
            "latency_budget_file_sha256": self.latency_budget_file_sha256,
            "operator_attestation_sha256": self.operator_attestation_sha256,
            "precursor_assist_promotion_evidence_sha256": (self.precursor_assist_promotion_evidence_sha256),
            "body_free": True,
            "promotion_authority_granted": False,
            "activation_performed": False,
        }


@dataclass(frozen=True, slots=True)
class SupervisorPromotionBundleReceipt:
    """Canonical component identities embedded in one atomic bundle."""

    target_mode: SupervisorMode
    source_revision_sha256: str
    registry_binding_sha256: str
    baseline_file_sha256: str
    baseline_report_sha256: str
    latency_budget_file_sha256: str
    operator_attestation_sha256: str
    representative_window_server_attestation_sha256: str
    representative_window_lookup_token_sha256: str
    representative_window_sha256: str
    representative_window_observer_runner_sha256: str
    promotion_evidence_file_sha256: str
    promotion_evidence_canonical_sha256: str
    precursor_assist_promotion_evidence_sha256: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.target_mode, SupervisorMode) or self.target_mode not in {
            SupervisorMode.ASSIST,
            SupervisorMode.CANARY,
        }:
            raise SupervisorPromotionEvidenceProducerError("bundle receipt mode is not promoted")
        for label, value in (
            ("source_revision_sha256", self.source_revision_sha256),
            ("registry_binding_sha256", self.registry_binding_sha256),
            ("baseline_file_sha256", self.baseline_file_sha256),
            ("baseline_report_sha256", self.baseline_report_sha256),
            ("latency_budget_file_sha256", self.latency_budget_file_sha256),
            ("operator_attestation_sha256", self.operator_attestation_sha256),
            (
                "representative_window_server_attestation_sha256",
                self.representative_window_server_attestation_sha256,
            ),
            (
                "representative_window_lookup_token_sha256",
                self.representative_window_lookup_token_sha256,
            ),
            ("representative_window_sha256", self.representative_window_sha256),
            (
                "representative_window_observer_runner_sha256",
                self.representative_window_observer_runner_sha256,
            ),
            ("promotion_evidence_file_sha256", self.promotion_evidence_file_sha256),
            (
                "promotion_evidence_canonical_sha256",
                self.promotion_evidence_canonical_sha256,
            ),
        ):
            _digest(value, label=label)
        if self.target_mode is SupervisorMode.ASSIST:
            if self.precursor_assist_promotion_evidence_sha256 is not None:
                raise SupervisorPromotionEvidenceProducerError("assist bundle receipt carries a precursor")
        else:
            _digest(
                self.precursor_assist_promotion_evidence_sha256,
                label="precursor_assist_promotion_evidence_sha256",
            )

    def payload(self) -> dict[str, object]:
        return {
            "schema": SUPERVISOR_PROMOTION_BUNDLE_RECEIPT_SCHEMA,
            "target_mode": self.target_mode.value,
            "source_revision_sha256": self.source_revision_sha256,
            "registry_binding_sha256": self.registry_binding_sha256,
            "baseline_file_sha256": self.baseline_file_sha256,
            "baseline_report_sha256": self.baseline_report_sha256,
            "latency_budget_file_sha256": self.latency_budget_file_sha256,
            "operator_attestation_sha256": self.operator_attestation_sha256,
            "representative_window_server_attestation_sha256": (
                self.representative_window_server_attestation_sha256
            ),
            "representative_window_lookup_token_sha256": (self.representative_window_lookup_token_sha256),
            "representative_window_sha256": self.representative_window_sha256,
            "representative_window_observer_runner_sha256": (
                self.representative_window_observer_runner_sha256
            ),
            "promotion_evidence_file_sha256": self.promotion_evidence_file_sha256,
            "promotion_evidence_canonical_sha256": (self.promotion_evidence_canonical_sha256),
            "precursor_assist_promotion_evidence_sha256": (self.precursor_assist_promotion_evidence_sha256),
            "body_free": True,
            "promotion_authority_granted": False,
            "activation_performed": False,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


@dataclass(frozen=True, slots=True)
class AcceptedSupervisorPromotionBundle:
    """A bundle independently rebuilt from its baseline and attestation."""

    evidence: AssistPromotionLiveEvidence
    bundle_file_sha256: str
    baseline_file_sha256: str
    baseline_report_sha256: str
    operator_attestation_sha256: str
    producer_receipt_sha256: str
    representative_window_issue_raw: bytes = field(repr=False, compare=False)
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("bundle_file_sha256", self.bundle_file_sha256),
            ("baseline_file_sha256", self.baseline_file_sha256),
            ("baseline_report_sha256", self.baseline_report_sha256),
            ("operator_attestation_sha256", self.operator_attestation_sha256),
            ("producer_receipt_sha256", self.producer_receipt_sha256),
        ):
            _digest(value, label=label)
        expected = _process_acceptance_seal(
            kind="promotion-bundle",
            fields=(
                self.evidence,
                self.bundle_file_sha256,
                self.baseline_file_sha256,
                self.baseline_report_sha256,
                self.operator_attestation_sha256,
                self.producer_receipt_sha256,
                self.representative_window_issue_raw,
            ),
        )
        if (
            self._process_authority is not _PROCESS_ACCEPTANCE_AUTHORITY
            or type(self._process_seal_sha256) is not str
            or not hmac.compare_digest(self._process_seal_sha256, expected)
        ):
            raise SupervisorPromotionEvidenceProducerError(
                "promotion bundle was not accepted by this process"
            )

    def representative_window_issue_payload(self) -> dict[str, Any]:
        """Return a fresh closed mapping for trusted one-shot consume/verification."""

        return _exact_dict(
            _decode_closed_json(self.representative_window_issue_raw),
            _REPRESENTATIVE_WINDOW_ISSUE_KEYS,
            label="representative-window issue",
        )


def _parse_metric_window(value: object, *, expected_stage: SupervisorMode) -> SupervisorProductMetricWindow:
    item = _exact_dict(value, _METRIC_KEYS, label=f"{expected_stage.value} metric window")
    if item["schema"] != SUPERVISOR_PRODUCT_WINDOW_SCHEMA or item["stage"] != expected_stage.value:
        raise SupervisorPromotionEvidenceProducerError("metric window identity does not match")
    observations = _count(item["observation_count"], label="metric observations")
    completion_counts = _count_map(item["completion_counts"], label="completion counts")
    failure_counts = _count_map(item["failure_class_counts"], label="failure counts")
    complete = _count(item["complete_count"], label="complete count")
    latency_observations = _count(item["latency_observation_count"], label="latency observations")
    latency_total = _count(
        item["latency_total_ms"], label="latency total", maximum=86_400_000 * max(1, observations)
    )
    latency_max = _count(item["latency_max_ms"], label="latency maximum", maximum=86_400_000)
    if (
        _sum_counts(completion_counts) != observations
        or _sum_counts(failure_counts) != observations
        or dict(completion_counts).get("complete", 0) != complete
        or latency_observations != observations
        or (observations == 0 and (latency_total != 0 or latency_max != 0))
        or (observations > 0 and not latency_max <= latency_total <= latency_max * observations)
    ):
        raise SupervisorPromotionEvidenceProducerError("metric aggregates are inconsistent")
    return SupervisorProductMetricWindow(
        stage=expected_stage,
        observation_count=observations,
        complete_count=complete,
        failure_class_counts=failure_counts,
        latency_observation_count=latency_observations,
        latency_total_ms=latency_total,
        latency_max_ms=latency_max,
        window_sha256=_digest(item["window_sha256"], label="metric window digest"),
    )


def _parse_shadow_window(value: object) -> SupervisorShadowReadinessWindow:
    item = _exact_dict(value, _SHADOW_KEYS, label="shadow readiness window")
    if (
        item["schema"] != SUPERVISOR_PRODUCT_WINDOW_SCHEMA
        or item["mode"] != SupervisorMode.SHADOW.value
        or item["production_joined"] is not True
        or item["actual_promoted_execution"] is not False
        or item["quality_claim"] != "documented_baseline_failure_only"
    ):
        raise SupervisorPromotionEvidenceProducerError("shadow readiness identity is invalid")
    observations = _count(item["observation_count"], label="shadow observations")
    joined = _count(item["joined_trace_count"], label="shadow joined traces")
    readiness = _count(item["readiness_observation_count"], label="readiness observations")
    calls = _count(item["call_rate_observation_count"], label="shadow call-rate observations")
    invoked = _count(item["supervisor_invocation_count"], label="shadow invocations")
    unnecessary = _count(item["unnecessary_supervisor_invocation_count"], label="shadow unnecessary calls")
    visible = _count(item["user_visible_observation_count"], label="shadow visible observations")
    regressions = _count(item["user_visible_regression_count"], label="shadow regressions")
    baseline = _parse_metric_window(item["baseline"], expected_stage=SupervisorMode.SHADOW)
    if (
        joined != observations
        or calls != observations
        or readiness != baseline.observation_count
        or visible != readiness
        or readiness > observations
        or invoked > calls
        or unnecessary > invoked
        or regressions > visible
    ):
        raise SupervisorPromotionEvidenceProducerError("shadow readiness aggregates are inconsistent")
    return SupervisorShadowReadinessWindow(
        observation_count=observations,
        joined_trace_count=joined,
        baseline=baseline,
        readiness_observation_count=readiness,
        call_rate_observation_count=calls,
        supervisor_invocation_count=invoked,
        unnecessary_supervisor_invocation_count=unnecessary,
        user_visible_observation_count=visible,
        user_visible_regression_count=regressions,
        readiness_witness_sha256=_digest(item["readiness_witness_sha256"], label="readiness witness digest"),
    )


def _parse_promoted_window(
    value: object, *, expected_mode: SupervisorMode
) -> SupervisorPromotedExecutionWindow:
    item = _exact_dict(value, _PROMOTED_KEYS, label=f"{expected_mode.value} promoted window")
    if (
        expected_mode not in {SupervisorMode.ASSIST, SupervisorMode.CANARY}
        or item["schema"] != SUPERVISOR_PRODUCT_WINDOW_SCHEMA
        or item["mode"] != expected_mode.value
        or item["production_joined"] is not True
        or item["actual_promoted_execution"] is not True
    ):
        raise SupervisorPromotionEvidenceProducerError("promoted window identity is invalid")
    observations = _count(item["observation_count"], label="promoted outer observations")
    joined = _count(item["joined_trace_count"], label="promoted joined traces")
    evidence_count = _count(item["promotion_evidence_count"], label="promotion evidence count")
    evidence_digest_raw = item["promotion_evidence_sha256"]
    if evidence_count == 1:
        evidence_digest = _digest(evidence_digest_raw, label="promotion evidence digest")
    elif evidence_digest_raw is None:
        evidence_digest = None
    else:
        raise SupervisorPromotionEvidenceProducerError("promotion evidence aggregate is inconsistent")
    calls = _count(item["call_rate_observation_count"], label="promoted call-rate observations")
    invoked = _count(item["supervisor_invocation_count"], label="promoted invocations")
    unnecessary = _count(item["unnecessary_supervisor_invocation_count"], label="promoted unnecessary calls")
    visible = _count(item["user_visible_observation_count"], label="promoted visible observations")
    regressions = _count(item["user_visible_regression_count"], label="promoted regressions")
    promoted = _parse_metric_window(item["promoted"], expected_stage=expected_mode)
    if (
        joined != observations
        or calls != observations
        or promoted.observation_count > observations
        or invoked > calls
        or unnecessary > invoked
        or visible > promoted.observation_count
        or regressions > visible
    ):
        raise SupervisorPromotionEvidenceProducerError("promoted aggregates are inconsistent")
    return SupervisorPromotedExecutionWindow(
        mode=expected_mode,
        observation_count=observations,
        joined_trace_count=joined,
        promotion_evidence_count=evidence_count,
        promotion_evidence_sha256=evidence_digest,
        promoted=promoted,
        call_rate_observation_count=calls,
        supervisor_invocation_count=invoked,
        unnecessary_supervisor_invocation_count=unnecessary,
        user_visible_observation_count=visible,
        user_visible_regression_count=regressions,
        product_window_sha256=_digest(item["product_window_sha256"], label="product window digest"),
    )


def _validate_aggregate_sections(report: dict[str, Any], *, traces: int, joins: int) -> None:
    primary = _exact_dict(report["primary_baseline"], _PRIMARY_BASELINE_KEYS, label="primary baseline")
    for key in (
        "intent_counts",
        "playbook_counts",
        "completion_counts",
        "publication_counts",
        "failure_counts",
    ):
        if _sum_counts(_count_map(primary[key], label=key)) != traces:
            raise SupervisorPromotionEvidenceProducerError("primary aggregate does not match sample")
    for key in ("authority_rechecked_count", "partial_coverage_count", "state_restored_count"):
        if _count(primary[key], label=key) > traces:
            raise SupervisorPromotionEvidenceProducerError("primary scalar exceeds sample")

    supervisor = _exact_dict(report["supervisor_join"], _SUPERVISOR_JOIN_KEYS, label="supervisor join")
    for key in (
        "task_counts",
        "skip_counts",
        "parse_counts",
        "policy_reason_counts",
        "planner_latency_bucket_counts",
        "actual_completion_counts",
        "actual_publication_counts",
    ):
        if _sum_counts(_count_map(supervisor[key], label=key)) != joins:
            raise SupervisorPromotionEvidenceProducerError("supervisor aggregate does not match sample")
    _count_map(supervisor["actual_capability_outcome_counts"], label="capability outcomes")
    for key in (
        "invoked_count",
        "admitted_count",
        "final_authority_rechecked_count",
        "state_restored_count",
        "retry_occurred_count",
    ):
        if _count(supervisor[key], label=key) > joins:
            raise SupervisorPromotionEvidenceProducerError("supervisor scalar exceeds sample")


def _decode_closed_json(raw: bytes) -> dict[str, Any]:
    def closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SupervisorPromotionEvidenceProducerError("baseline contains duplicate JSON keys")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise SupervisorPromotionEvidenceProducerError("baseline contains a non-finite number")

    try:
        decoded = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=closed_pairs,
            parse_constant=reject_constant,
        )
    except SupervisorPromotionEvidenceProducerError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise SupervisorPromotionEvidenceProducerError("baseline JSON is malformed") from exc
    if type(decoded) is not dict:
        raise SupervisorPromotionEvidenceProducerError("baseline must be one JSON object")
    return cast(dict[str, Any], decoded)


def load_accepted_supervisor_production_baseline(
    raw: bytes,
    *,
    expected_file_sha256: str,
) -> AcceptedSupervisorProductionBaseline:
    """Load one exact canonical baseline and retain only typed aggregates."""

    if type(raw) is not bytes:
        raise TypeError("baseline loader requires bytes")
    expected = _digest(expected_file_sha256, label="expected baseline file digest")
    if not 0 < len(raw) <= _MAX_BASELINE_BYTES or not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected
    ):
        raise SupervisorPromotionEvidenceProducerError("baseline file digest does not match")
    report = _exact_dict(_decode_closed_json(raw), _TOP_LEVEL_KEYS, label="production baseline")
    if raw != canonical_json_file_bytes(report):
        raise SupervisorPromotionEvidenceProducerError("baseline file is not canonical JSON")
    if report["schema"] != SUPERVISOR_PRODUCTION_BASELINE_SCHEMA:
        raise SupervisorPromotionEvidenceProducerError("baseline schema is not accepted")
    report_digest = _digest(report["report_sha256"], label="baseline report digest")
    unsigned = dict(report)
    unsigned.pop("report_sha256")
    if not hmac.compare_digest(canonical_sha256(unsigned), report_digest):
        raise SupervisorPromotionEvidenceProducerError("baseline self-digest does not match")

    evidence = _exact_dict(report["evidence"], _EVIDENCE_KEYS, label="baseline evidence")
    if evidence != {
        "kind": SUPERVISOR_PRODUCTION_BASELINE_KIND,
        "body_free": True,
        "production_acceptance": False,
        "acceptance_authority": "operator_review_required",
        "representative_window_attested": False,
        "promotion_authority": False,
    }:
        raise SupervisorPromotionEvidenceProducerError("baseline candidate authority is invalid")

    sample = _exact_dict(report["sample"], _SAMPLE_KEYS, label="baseline sample")
    limit = _count(sample["limit"], label="sample limit")
    if limit < 1:
        raise SupervisorPromotionEvidenceProducerError("sample limit must be positive")
    traces = _count(sample["turn_traces"], label="sample turn traces")
    joins = _count(sample["joined_supervisor_events"], label="sample joined events")
    promoted_rows = _count(sample["promoted_product_events"], label="sample promoted events")
    if traces > limit or joins > limit or promoted_rows > limit:
        raise SupervisorPromotionEvidenceProducerError("sample row count exceeds limit")
    if any(_count(sample[key], label=key) != 0 for key in _ANOMALY_SAMPLE_KEYS):
        raise SupervisorPromotionEvidenceProducerError("baseline contains malformed or ambiguous rows")
    _validate_aggregate_sections(report, traces=traces, joins=joins)

    windows = _exact_dict(
        report["product_windows"],
        frozenset({"shadow_readiness", "promoted_execution"}),
        label="product windows",
    )
    shadow = _parse_shadow_window(windows["shadow_readiness"])
    promoted = _exact_dict(
        windows["promoted_execution"],
        frozenset({SupervisorMode.ASSIST.value, SupervisorMode.CANARY.value}),
        label="promoted execution windows",
    )
    assist = _parse_promoted_window(
        promoted[SupervisorMode.ASSIST.value], expected_mode=SupervisorMode.ASSIST
    )
    canary = _parse_promoted_window(
        promoted[SupervisorMode.CANARY.value], expected_mode=SupervisorMode.CANARY
    )
    if (
        shadow.observation_count != joins
        or assist.observation_count + canary.observation_count != promoted_rows
    ):
        raise SupervisorPromotionEvidenceProducerError("product windows do not cover the exact sample")
    seal_fields = (expected, report_digest, shadow, assist, canary)
    return AcceptedSupervisorProductionBaseline(
        file_sha256=expected,
        report_sha256=report_digest,
        shadow_readiness=shadow,
        assist_execution=assist,
        canary_execution=canary,
        _process_authority=_PROCESS_ACCEPTANCE_AUTHORITY,
        _process_seal_sha256=_process_acceptance_seal(
            kind="production-baseline",
            fields=seal_fields,
        ),
    )


def build_supervisor_latency_budget_document(
    *,
    target_mode: SupervisorMode,
    source_revision_sha256: str,
    maximum_user_visible_latency_ms: int,
) -> SupervisorLatencyBudgetDocument:
    """Build the existing closed v1 budget; this does not accept or install it."""

    return SupervisorLatencyBudgetDocument(
        target_mode=target_mode,
        source_revision_sha256=source_revision_sha256,
        maximum_user_visible_latency_ms=maximum_user_visible_latency_ms,
    )


def load_canonical_supervisor_latency_budget(
    raw: bytes,
    *,
    expected_file_sha256: str,
) -> AcceptedCanonicalSupervisorLatencyBudget:
    """Load an exact-hash v1 budget and require producer-canonical bytes."""

    try:
        accepted = load_accepted_supervisor_latency_budget(
            raw,
            expected_document_sha256=expected_file_sha256,
        )
    except (TypeError, PromotedProductEventError) as exc:
        raise SupervisorPromotionEvidenceProducerError("latency budget is not accepted") from exc
    if raw != canonical_json_file_bytes(accepted.document.payload()):
        raise SupervisorPromotionEvidenceProducerError("latency budget file is not canonical JSON")
    seal_fields = (accepted.document, accepted.document_sha256)
    return AcceptedCanonicalSupervisorLatencyBudget(
        document=accepted.document,
        document_sha256=accepted.document_sha256,
        _process_authority=_PROCESS_ACCEPTANCE_AUTHORITY,
        _process_seal_sha256=_process_acceptance_seal(
            kind="latency-budget",
            fields=seal_fields,
        ),
    )


def _assert_attested_inputs(
    baseline: AcceptedSupervisorProductionBaseline,
    budget: AcceptedCanonicalSupervisorLatencyBudget,
    attestation: SupervisorPromotionOperatorAttestation,
) -> None:
    if (
        type(baseline) is not AcceptedSupervisorProductionBaseline
        or baseline._process_authority is not _PROCESS_ACCEPTANCE_AUTHORITY
    ):
        raise TypeError("promotion evidence requires an accepted production baseline")
    if (
        type(budget) is not AcceptedCanonicalSupervisorLatencyBudget
        or budget._process_authority is not _PROCESS_ACCEPTANCE_AUTHORITY
    ):
        raise TypeError("promotion evidence requires an accepted latency budget")
    if not isinstance(attestation, SupervisorPromotionOperatorAttestation):
        raise TypeError("promotion evidence requires a typed operator attestation")
    if (
        not hmac.compare_digest(attestation.baseline_file_sha256, baseline.file_sha256)
        or not hmac.compare_digest(attestation.baseline_report_sha256, baseline.report_sha256)
        or not hmac.compare_digest(attestation.latency_budget_file_sha256, budget.document_sha256)
        or budget.document.target_mode is not attestation.target_mode
        or not hmac.compare_digest(
            budget.document.source_revision_sha256,
            attestation.source_revision_sha256,
        )
    ):
        raise SupervisorPromotionEvidenceProducerError("operator attestation input binding does not match")
    if not attestation.all_invariants_affirmed():
        raise SupervisorPromotionEvidenceProducerError("operator did not explicitly affirm every invariant")


def _live_evidence_common(
    *,
    evidence_id: str,
    attestation: SupervisorPromotionOperatorAttestation,
    observation_count: int,
    joined_trace_count: int,
    product_evidence: AssistPromotionReadinessEvidence | AssistPromotionOutcomeEvidence,
) -> AssistPromotionLiveEvidence:
    observed_mode = (
        SupervisorMode.SHADOW if attestation.target_mode is SupervisorMode.ASSIST else SupervisorMode.ASSIST
    )
    observed_policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(observed_mode)
    target_policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(
        attestation.target_mode
    )
    return AssistPromotionLiveEvidence(
        evidence_id=evidence_id,
        authority=AssistPromotionEvidenceAuthority.PRODUCTION_JOINED,
        observed_mode=observed_mode,
        task_class=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        source_revision_sha256=attestation.source_revision_sha256,
        promotion_policy_sha256=SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
        observed_policy_id=observed_policy.policy_id,
        observed_policy_sha256=observed_policy.policy_sha256,
        target_policy_id=target_policy.policy_id,
        target_policy_sha256=target_policy.policy_sha256,
        runtime_profile_id=semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        runtime_profile_manifest_sha256=(
            semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        ),
        registry_binding_sha256=attestation.registry_binding_sha256,
        baseline_file_sha256=attestation.baseline_file_sha256,
        baseline_report_sha256=attestation.baseline_report_sha256,
        operator_attestation_sha256=attestation.canonical_sha256(),
        precursor_assist_promotion_evidence_sha256=(attestation.precursor_assist_promotion_evidence_sha256),
        max_steps=SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS,
        max_review_rounds=SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS,
        observation_count=observation_count,
        joined_trace_count=joined_trace_count,
        representative_window_attested=attestation.representative_window_attested,
        primary_fallback_proven=attestation.primary_fallback_proven,
        laptop_unavailable_fallback_proven=attestation.laptop_unavailable_fallback_proven,
        final_authority_recheck_proven=attestation.final_authority_recheck_proven,
        primary_publication_owner_proven=attestation.primary_publication_owner_proven,
        hidden_owner_count=0,
        duplicate_capability_count=0,
        duplicate_effect_count=0,
        duplicate_publication_count=0,
        false_completion_regression_count=0,
        product_evidence=product_evidence,
    )


def _assert_product_gate_metrics(
    *,
    observations: int,
    baseline_observations: int,
    promoted_observations: int,
    call_rate_observations: int,
    unnecessary_invocations: int,
    user_visible_observations: int,
    user_visible_regressions: int,
    latency_observations: int,
    latency_total_ms: int,
    latency_max_ms: int,
    latency_budget_ms: int,
) -> None:
    minimum = SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
    if (
        observations < minimum
        or baseline_observations < minimum
        or promoted_observations < minimum
        or promoted_observations > observations
        or call_rate_observations != observations
        or user_visible_observations != promoted_observations
        or latency_observations != promoted_observations
    ):
        raise SupervisorPromotionEvidenceProducerError("product evidence window is not promotion-complete")
    if (
        unnecessary_invocations * 10_000
        > SUPERVISOR_ASSIST_MAX_UNNECESSARY_CALL_RATE_BPS * call_rate_observations
        or user_visible_regressions != 0
        or latency_max_ms > latency_budget_ms
        or latency_total_ms > latency_budget_ms * latency_observations
    ):
        raise SupervisorPromotionEvidenceProducerError("product evidence exceeds a promotion budget")


def build_supervisor_assist_promotion_evidence(
    *,
    evidence_id: str,
    baseline: AcceptedSupervisorProductionBaseline,
    budget: AcceptedCanonicalSupervisorLatencyBudget,
    attestation: SupervisorPromotionOperatorAttestation,
    documented_failure_class_id: str,
    documented_failure_class_sha256: str,
) -> AssistPromotionLiveEvidence:
    """Build readiness v2 from the exact shadow product window."""

    _assert_attested_inputs(baseline, budget, attestation)
    if attestation.target_mode is not SupervisorMode.ASSIST:
        raise SupervisorPromotionEvidenceProducerError("assist producer requires assist attestation")
    shadow = baseline.shadow_readiness
    failure_count = shadow.baseline.failure_count(documented_failure_class_id)
    if failure_count < 1:
        raise SupervisorPromotionEvidenceProducerError("documented failure is absent from baseline")
    product = AssistPromotionReadinessEvidence(
        baseline_window_sha256=shadow.baseline.window_sha256,
        baseline_observation_count=shadow.baseline.observation_count,
        baseline_complete_count=shadow.baseline.complete_count,
        documented_failure_class_id=documented_failure_class_id,
        documented_failure_class_sha256=documented_failure_class_sha256,
        baseline_failure_class_count=failure_count,
        readiness_witness_sha256=shadow.readiness_witness_sha256,
        readiness_observation_count=shadow.readiness_observation_count,
        latency_budget_target_mode=budget.document.target_mode,
        latency_budget_source_revision_sha256=budget.document.source_revision_sha256,
        latency_budget_ms=budget.document.maximum_user_visible_latency_ms,
        latency_budget_sha256=budget.document_sha256,
        latency_total_ms=shadow.baseline.latency_total_ms,
        latency_max_ms=shadow.baseline.latency_max_ms,
        call_rate_observation_count=shadow.call_rate_observation_count,
        supervisor_invocation_count=shadow.supervisor_invocation_count,
        unnecessary_supervisor_invocation_count=shadow.unnecessary_supervisor_invocation_count,
        user_visible_observation_count=shadow.user_visible_observation_count,
        user_visible_regression_count=shadow.user_visible_regression_count,
    )
    _assert_product_gate_metrics(
        observations=shadow.observation_count,
        baseline_observations=shadow.baseline.observation_count,
        promoted_observations=shadow.readiness_observation_count,
        call_rate_observations=shadow.call_rate_observation_count,
        unnecessary_invocations=shadow.unnecessary_supervisor_invocation_count,
        user_visible_observations=shadow.user_visible_observation_count,
        user_visible_regressions=shadow.user_visible_regression_count,
        latency_observations=shadow.readiness_observation_count,
        latency_total_ms=shadow.baseline.latency_total_ms,
        latency_max_ms=shadow.baseline.latency_max_ms,
        latency_budget_ms=budget.document.maximum_user_visible_latency_ms,
    )
    return _live_evidence_common(
        evidence_id=evidence_id,
        attestation=attestation,
        observation_count=shadow.observation_count,
        joined_trace_count=shadow.joined_trace_count,
        product_evidence=product,
    )


def build_supervisor_canary_promotion_evidence(
    *,
    evidence_id: str,
    baseline: AcceptedSupervisorProductionBaseline,
    budget: AcceptedCanonicalSupervisorLatencyBudget,
    attestation: SupervisorPromotionOperatorAttestation,
    documented_failure_class_id: str | None = None,
    documented_failure_class_sha256: str | None = None,
) -> AssistPromotionLiveEvidence:
    """Build outcome v2 from one exact precursor-bound assist window."""

    _assert_attested_inputs(baseline, budget, attestation)
    if attestation.target_mode is not SupervisorMode.CANARY:
        raise SupervisorPromotionEvidenceProducerError("canary producer requires canary attestation")
    assist = baseline.assist_execution
    precursor = attestation.precursor_assist_promotion_evidence_sha256
    if (
        precursor is None
        or assist.promotion_evidence_count != 1
        or assist.promotion_evidence_sha256 is None
        or not hmac.compare_digest(assist.promotion_evidence_sha256, precursor)
    ):
        raise SupervisorPromotionEvidenceProducerError("assist window is not bound to one exact precursor")
    basis = attestation.quality_basis
    if basis is AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT:
        if documented_failure_class_id is not None or documented_failure_class_sha256 is not None:
            raise SupervisorPromotionEvidenceProducerError(
                "completion improvement cannot claim a failure class"
            )
        failure_id = "none"
        failure_sha = None
        baseline_failure_count = 0
        promoted_failure_count = 0
    elif basis is AssistPromotionQualityBasis.DOCUMENTED_FAILURE_CLASS_REMOVAL:
        if documented_failure_class_id is None or documented_failure_class_sha256 is None:
            raise SupervisorPromotionEvidenceProducerError("failure removal requires exact failure identity")
        failure_id = documented_failure_class_id
        failure_sha = documented_failure_class_sha256
        baseline_failure_count = baseline.shadow_readiness.baseline.failure_count(failure_id)
        promoted_failure_count = assist.promoted.failure_count(failure_id)
        if baseline_failure_count < 1 or promoted_failure_count != 0:
            raise SupervisorPromotionEvidenceProducerError("failure removal is not measured by the windows")
    else:  # pragma: no cover - typed attestation closes this branch
        raise SupervisorPromotionEvidenceProducerError("canary quality basis is invalid")
    product = AssistPromotionOutcomeEvidence(
        quality_basis=basis,
        baseline_window_sha256=baseline.shadow_readiness.baseline.window_sha256,
        promoted_window_sha256=assist.promoted.window_sha256,
        baseline_observation_count=baseline.shadow_readiness.baseline.observation_count,
        baseline_complete_count=baseline.shadow_readiness.baseline.complete_count,
        promoted_observation_count=assist.promoted.observation_count,
        promoted_complete_count=assist.promoted.complete_count,
        documented_failure_class_id=failure_id,
        documented_failure_class_sha256=failure_sha,
        baseline_failure_class_count=baseline_failure_count,
        promoted_failure_class_count=promoted_failure_count,
        latency_budget_target_mode=budget.document.target_mode,
        latency_budget_source_revision_sha256=budget.document.source_revision_sha256,
        latency_budget_ms=budget.document.maximum_user_visible_latency_ms,
        latency_budget_sha256=budget.document_sha256,
        latency_observation_count=assist.promoted.latency_observation_count,
        latency_total_ms=assist.promoted.latency_total_ms,
        latency_max_ms=assist.promoted.latency_max_ms,
        call_rate_observation_count=assist.call_rate_observation_count,
        supervisor_invocation_count=assist.supervisor_invocation_count,
        unnecessary_supervisor_invocation_count=assist.unnecessary_supervisor_invocation_count,
        user_visible_observation_count=assist.user_visible_observation_count,
        user_visible_regression_count=assist.user_visible_regression_count,
    )
    _assert_product_gate_metrics(
        observations=assist.observation_count,
        baseline_observations=baseline.shadow_readiness.baseline.observation_count,
        promoted_observations=assist.promoted.observation_count,
        call_rate_observations=assist.call_rate_observation_count,
        unnecessary_invocations=assist.unnecessary_supervisor_invocation_count,
        user_visible_observations=assist.user_visible_observation_count,
        user_visible_regressions=assist.user_visible_regression_count,
        latency_observations=assist.promoted.latency_observation_count,
        latency_total_ms=assist.promoted.latency_total_ms,
        latency_max_ms=assist.promoted.latency_max_ms,
        latency_budget_ms=budget.document.maximum_user_visible_latency_ms,
    )
    if (
        basis is AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT
        and product.promoted_complete_count * product.baseline_observation_count
        <= product.baseline_complete_count * product.promoted_observation_count
    ):
        raise SupervisorPromotionEvidenceProducerError("completion rate improvement is not measured")
    return _live_evidence_common(
        evidence_id=evidence_id,
        attestation=attestation,
        observation_count=assist.observation_count,
        joined_trace_count=assist.joined_trace_count,
        product_evidence=product,
    )


def _operator_attestation_from_payload(value: object) -> SupervisorPromotionOperatorAttestation:
    item = _exact_dict(value, _ATTESTATION_KEYS, label="operator attestation")
    if item["schema"] != SUPERVISOR_PROMOTION_OPERATOR_ATTESTATION_SCHEMA:
        raise SupervisorPromotionEvidenceProducerError("operator attestation schema is invalid")
    try:
        mode = SupervisorMode(item["target_mode"])
        quality_raw = item["quality_basis"]
        quality = None if quality_raw is None else AssistPromotionQualityBasis(quality_raw)
        return SupervisorPromotionOperatorAttestation(
            target_mode=mode,
            baseline_file_sha256=item["baseline_file_sha256"],
            baseline_report_sha256=item["baseline_report_sha256"],
            latency_budget_file_sha256=item["latency_budget_file_sha256"],
            source_revision_sha256=item["source_revision_sha256"],
            registry_binding_sha256=item["registry_binding_sha256"],
            representative_window_attested=item["representative_window_attested"],
            primary_fallback_proven=item["primary_fallback_proven"],
            laptop_unavailable_fallback_proven=item["laptop_unavailable_fallback_proven"],
            final_authority_recheck_proven=item["final_authority_recheck_proven"],
            primary_publication_owner_proven=item["primary_publication_owner_proven"],
            zero_hidden_owners_attested=item["zero_hidden_owners_attested"],
            zero_duplicate_capabilities_attested=item["zero_duplicate_capabilities_attested"],
            zero_duplicate_effects_attested=item["zero_duplicate_effects_attested"],
            zero_duplicate_publications_attested=item["zero_duplicate_publications_attested"],
            zero_false_completion_regressions_attested=(item["zero_false_completion_regressions_attested"]),
            precursor_assist_promotion_evidence_sha256=(item["precursor_assist_promotion_evidence_sha256"]),
            quality_basis=quality,
        )
    except (TypeError, ValueError) as exc:
        raise SupervisorPromotionEvidenceProducerError("operator attestation payload is invalid") from exc


def _accepted_representative_window_issue(
    value: object,
    *,
    baseline: AcceptedSupervisorProductionBaseline,
    budget: AcceptedCanonicalSupervisorLatencyBudget,
    attestation: SupervisorPromotionOperatorAttestation,
) -> _AcceptedRepresentativeWindowIssue:
    item = _exact_dict(
        value,
        _REPRESENTATIVE_WINDOW_ISSUE_KEYS,
        label="representative-window issue",
    )
    server = _exact_dict(
        item["server_attestation"],
        REPRESENTATIVE_WINDOW_ATTESTATION_KEYS,
        label="representative-window server attestation",
    )
    try:
        target_mode = SupervisorMode(server["target_mode"])
        observed_mode = SupervisorMode(server["observed_mode"])
    except (TypeError, ValueError) as exc:
        raise SupervisorPromotionEvidenceProducerError("representative-window modes are invalid") from exc
    expected_observed = (
        SupervisorMode.SHADOW if attestation.target_mode is SupervisorMode.ASSIST else SupervisorMode.ASSIST
    )
    expected_window = (
        baseline.shadow_readiness.readiness_witness_sha256
        if attestation.target_mode is SupervisorMode.ASSIST
        else baseline.assist_execution.product_window_sha256
    )
    expected_joined = (
        baseline.shadow_readiness.joined_trace_count
        if attestation.target_mode is SupervisorMode.ASSIST
        else baseline.assist_execution.joined_trace_count
    )
    token = item["attestation_lookup_token"]
    token_sha256 = item["lookup_token_sha256"]
    server_sha256 = item["server_attestation_sha256"]
    digest_names = (
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "latency_budget_document_sha256",
        "latency_budget_source_revision_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "primary_process_epoch_sha256",
        "observed_release_metadata_sha256",
        "observed_release_tree_sha256",
        "observed_registry_binding_sha256",
        "supervisor_policy_sha256",
        "runtime_profile_manifest_sha256",
        "observer_runner_sha256",
        "representative_window_sha256",
        "lookup_token_sha256",
        "signature",
    )
    precursor = server["precursor_assist_promotion_evidence_sha256"]
    if (
        item["schema"] != REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA
        or item["status"] != "unused"
        or item["state_version"] != 1
        or server["schema"] != REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA
        or server["authority"] != REPRESENTATIVE_WINDOW_AUTHORITY
        or server["state_version"] != 1
        or target_mode is not attestation.target_mode
        or observed_mode is not expected_observed
        or server["requested_mode"] != SupervisorMode.ASSIST.value
        or server["baseline_file_sha256"] != baseline.file_sha256
        or server["baseline_report_sha256"] != baseline.report_sha256
        or server["latency_budget_file_sha256"] != budget.document_sha256
        or server["latency_budget_document_sha256"] != budget.document_sha256
        or server["latency_budget_target_mode"] != budget.document.target_mode.value
        or server["latency_budget_source_revision_sha256"] != budget.document.source_revision_sha256
        or server["maximum_user_visible_latency_ms"] != budget.document.maximum_user_visible_latency_ms
        or server["source_revision_sha256"] != attestation.source_revision_sha256
        or server["registry_binding_sha256"] != attestation.registry_binding_sha256
        or server["observed_registry_binding_sha256"] != attestation.registry_binding_sha256
        or server["supervisor_policy_id"] != semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID
        or server["supervisor_policy_sha256"]
        != semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256
        or server["runtime_profile_id"] != semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID
        or server["runtime_profile_manifest_sha256"]
        != semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        or server["representative_window_sha256"] != expected_window
        or server["joined_trace_count"] != expected_joined
        or server["server_recomputed"] is not True
        or server["representative_window_attested"] is not True
        or server["synthetic_authority"] is not False
        or (precursor is None) != (attestation.precursor_assist_promotion_evidence_sha256 is None)
        or precursor != attestation.precursor_assist_promotion_evidence_sha256
        or type(token) is not str
        or _DIGEST_RE.fullmatch(token) is None
        or set(token) == {"0"}
        or not _digest(token_sha256, label="representative-window lookup token digest")
        or not hmac.compare_digest(hashlib.sha256(token.encode("ascii")).hexdigest(), token_sha256)
        or server["lookup_token_sha256"] != token_sha256
        or not _digest(
            server_sha256,
            label="representative-window server attestation digest",
        )
        or not hmac.compare_digest(representative_window_sha256(server), server_sha256)
        or any(not _digest(server[name], label=name) for name in digest_names)
        or type(server["primary_pid"]) is not int
        or server["primary_pid"] <= 0
        or re.fullmatch(r"[0-9a-f]{40}", str(server["observed_release_commit"])) is None
        or type(server["primary_backend_version"]) is not str
        or not server["primary_backend_version"]
        or type(server["sample_limit"]) is not int
        or type(server["turn_trace_count"]) is not int
        or type(server["joined_trace_count"]) is not int
        or not 0 < server["joined_trace_count"] <= server["turn_trace_count"] < server["sample_limit"]
        or type(server["issued_at"]) is not int
        or type(server["expires_at"]) is not int
        or not server["issued_at"] < server["expires_at"]
    ):
        raise SupervisorPromotionEvidenceProducerError(
            "representative-window issue does not bind the promotion inputs"
        )
    canonical_raw = representative_window_canonical(item)
    fields = (
        target_mode,
        observed_mode,
        server_sha256,
        token_sha256,
        server["observer_runner_sha256"],
        server["representative_window_sha256"],
        precursor,
        canonical_raw,
    )
    return _AcceptedRepresentativeWindowIssue(
        target_mode=target_mode,
        observed_mode=observed_mode,
        server_attestation_sha256=server_sha256,
        lookup_token_sha256=token_sha256,
        observer_runner_sha256=server["observer_runner_sha256"],
        representative_window_sha256=server["representative_window_sha256"],
        precursor_assist_promotion_evidence_sha256=precursor,
        canonical_raw=canonical_raw,
        _process_authority=_PROCESS_ACCEPTANCE_AUTHORITY,
        _process_seal_sha256=_process_acceptance_seal(
            kind="representative-window-issue",
            fields=fields,
        ),
    )


def _rebuild_bundle_evidence(
    *,
    evidence_payload: object,
    baseline: AcceptedSupervisorProductionBaseline,
    budget: AcceptedCanonicalSupervisorLatencyBudget,
    attestation: SupervisorPromotionOperatorAttestation,
) -> AssistPromotionLiveEvidence:
    if type(evidence_payload) is not dict:
        raise SupervisorPromotionEvidenceProducerError("bundle evidence is not an object")
    item = cast(dict[str, Any], evidence_payload)
    evidence_id = item.get("evidence_id")
    product = item.get("product_evidence")
    if type(evidence_id) is not str or type(product) is not dict:
        raise SupervisorPromotionEvidenceProducerError("bundle evidence identity is invalid")
    product_item = cast(dict[str, Any], product)
    if attestation.target_mode is SupervisorMode.ASSIST:
        failure_id = product_item.get("documented_failure_class_id")
        failure_sha256 = product_item.get("documented_failure_class_sha256")
        if type(failure_id) is not str or type(failure_sha256) is not str:
            raise SupervisorPromotionEvidenceProducerError("assist bundle has no documented failure identity")
        expected = build_supervisor_assist_promotion_evidence(
            evidence_id=evidence_id,
            baseline=baseline,
            budget=budget,
            attestation=attestation,
            documented_failure_class_id=failure_id,
            documented_failure_class_sha256=failure_sha256,
        )
    else:
        failure_id_raw = product_item.get("documented_failure_class_id")
        failure_sha256_raw = product_item.get("documented_failure_class_sha256")
        failure_id = failure_id_raw if type(failure_id_raw) is str and failure_id_raw != "none" else None
        failure_sha256 = failure_sha256_raw if type(failure_sha256_raw) is str else None
        expected = build_supervisor_canary_promotion_evidence(
            evidence_id=evidence_id,
            baseline=baseline,
            budget=budget,
            attestation=attestation,
            documented_failure_class_id=failure_id,
            documented_failure_class_sha256=failure_sha256,
        )
    if item != expected.payload():
        raise SupervisorPromotionEvidenceProducerError(
            "bundle evidence was not derived from the accepted inputs"
        )
    return expected


def _bundle_receipt(
    *,
    baseline: AcceptedSupervisorProductionBaseline,
    budget: AcceptedCanonicalSupervisorLatencyBudget,
    attestation: SupervisorPromotionOperatorAttestation,
    representative_window_issue: _AcceptedRepresentativeWindowIssue,
    evidence: AssistPromotionLiveEvidence,
) -> SupervisorPromotionBundleReceipt:
    evidence_file = canonical_json_file_bytes(evidence.payload())
    return SupervisorPromotionBundleReceipt(
        target_mode=attestation.target_mode,
        source_revision_sha256=attestation.source_revision_sha256,
        registry_binding_sha256=attestation.registry_binding_sha256,
        baseline_file_sha256=baseline.file_sha256,
        baseline_report_sha256=baseline.report_sha256,
        latency_budget_file_sha256=budget.document_sha256,
        operator_attestation_sha256=attestation.canonical_sha256(),
        representative_window_server_attestation_sha256=(
            representative_window_issue.server_attestation_sha256
        ),
        representative_window_lookup_token_sha256=(representative_window_issue.lookup_token_sha256),
        representative_window_sha256=(representative_window_issue.representative_window_sha256),
        representative_window_observer_runner_sha256=(representative_window_issue.observer_runner_sha256),
        promotion_evidence_file_sha256=hashlib.sha256(evidence_file).hexdigest(),
        promotion_evidence_canonical_sha256=evidence.canonical_sha256(),
        precursor_assist_promotion_evidence_sha256=(attestation.precursor_assist_promotion_evidence_sha256),
    )


def build_supervisor_promotion_bundle_payload(
    *,
    baseline_raw: bytes,
    budget: AcceptedCanonicalSupervisorLatencyBudget,
    attestation: SupervisorPromotionOperatorAttestation,
    representative_window_issue: Mapping[str, object],
    evidence: AssistPromotionLiveEvidence,
) -> dict[str, object]:
    """Build one canonical, body-free bundle from independently accepted inputs."""

    if type(baseline_raw) is not bytes:
        raise TypeError("bundle baseline must be bytes")
    baseline_sha256 = hashlib.sha256(baseline_raw).hexdigest()
    baseline = load_accepted_supervisor_production_baseline(
        baseline_raw,
        expected_file_sha256=baseline_sha256,
    )
    _assert_attested_inputs(baseline, budget, attestation)
    accepted_issue = _accepted_representative_window_issue(
        dict(representative_window_issue),
        baseline=baseline,
        budget=budget,
        attestation=attestation,
    )
    rebuilt = _rebuild_bundle_evidence(
        evidence_payload=evidence.payload(),
        baseline=baseline,
        budget=budget,
        attestation=attestation,
    )
    receipt = _bundle_receipt(
        baseline=baseline,
        budget=budget,
        attestation=attestation,
        representative_window_issue=accepted_issue,
        evidence=rebuilt,
    )
    baseline_payload = _decode_closed_json(baseline_raw)
    payload: dict[str, object] = {
        "schema": SUPERVISOR_PROMOTION_BUNDLE_SCHEMA,
        "body_free": True,
        "baseline": baseline_payload,
        "operator_attestation": attestation.payload(),
        "promotion_evidence": rebuilt.payload(),
        "representative_window_issue": accepted_issue.payload(),
        "producer_receipt": receipt.payload(),
        "producer_receipt_sha256": receipt.canonical_sha256(),
    }
    if len(canonical_json_file_bytes(payload)) > _MAX_BUNDLE_BYTES:
        raise SupervisorPromotionEvidenceProducerError("promotion bundle exceeds its byte bound")
    return payload


def load_accepted_supervisor_promotion_bundle(
    raw: bytes,
    *,
    expected_file_sha256: str,
    budget_raw: bytes,
    expected_budget_file_sha256: str,
) -> AcceptedSupervisorPromotionBundle:
    """Rebuild every bundle claim from its full baseline, budget and attestation."""

    if type(raw) is not bytes or type(budget_raw) is not bytes:
        raise TypeError("promotion bundle loader requires bytes")
    bundle_sha256 = _digest(expected_file_sha256, label="expected bundle file digest")
    if not 0 < len(raw) <= _MAX_BUNDLE_BYTES or not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), bundle_sha256
    ):
        raise SupervisorPromotionEvidenceProducerError("promotion bundle digest does not match")
    item = _exact_dict(_decode_closed_json(raw), _BUNDLE_KEYS, label="promotion bundle")
    if (
        item["schema"] != SUPERVISOR_PROMOTION_BUNDLE_SCHEMA
        or item["body_free"] is not True
        or raw != canonical_json_file_bytes(item)
    ):
        raise SupervisorPromotionEvidenceProducerError("promotion bundle is not canonical")
    baseline_payload = _exact_dict(
        item["baseline"],
        _TOP_LEVEL_KEYS,
        label="bundle production baseline",
    )
    baseline_raw = canonical_json_file_bytes(baseline_payload)
    baseline = load_accepted_supervisor_production_baseline(
        baseline_raw,
        expected_file_sha256=hashlib.sha256(baseline_raw).hexdigest(),
    )
    budget = load_canonical_supervisor_latency_budget(
        budget_raw,
        expected_file_sha256=expected_budget_file_sha256,
    )
    attestation = _operator_attestation_from_payload(item["operator_attestation"])
    _assert_attested_inputs(baseline, budget, attestation)
    accepted_issue = _accepted_representative_window_issue(
        item["representative_window_issue"],
        baseline=baseline,
        budget=budget,
        attestation=attestation,
    )
    evidence = _rebuild_bundle_evidence(
        evidence_payload=item["promotion_evidence"],
        baseline=baseline,
        budget=budget,
        attestation=attestation,
    )
    receipt = _bundle_receipt(
        baseline=baseline,
        budget=budget,
        attestation=attestation,
        representative_window_issue=accepted_issue,
        evidence=evidence,
    )
    if (
        item["producer_receipt"] != receipt.payload()
        or item["producer_receipt_sha256"] != receipt.canonical_sha256()
    ):
        raise SupervisorPromotionEvidenceProducerError("promotion bundle receipt does not match")
    fields = (
        evidence,
        bundle_sha256,
        baseline.file_sha256,
        baseline.report_sha256,
        attestation.canonical_sha256(),
        receipt.canonical_sha256(),
        accepted_issue.canonical_raw,
    )
    return AcceptedSupervisorPromotionBundle(
        evidence=evidence,
        bundle_file_sha256=bundle_sha256,
        baseline_file_sha256=baseline.file_sha256,
        baseline_report_sha256=baseline.report_sha256,
        operator_attestation_sha256=attestation.canonical_sha256(),
        producer_receipt_sha256=receipt.canonical_sha256(),
        representative_window_issue_raw=accepted_issue.canonical_raw,
        _process_authority=_PROCESS_ACCEPTANCE_AUTHORITY,
        _process_seal_sha256=_process_acceptance_seal(
            kind="promotion-bundle",
            fields=fields,
        ),
    )


__all__ = [
    "AcceptedSupervisorPromotionBundle",
    "AcceptedCanonicalSupervisorLatencyBudget",
    "AcceptedSupervisorProductionBaseline",
    "SUPERVISOR_PROMOTION_ARTIFACT_RECEIPT_SCHEMA",
    "SUPERVISOR_PROMOTION_BUNDLE_RECEIPT_SCHEMA",
    "SUPERVISOR_PROMOTION_BUNDLE_SCHEMA",
    "SUPERVISOR_PROMOTION_OPERATOR_ATTESTATION_SCHEMA",
    "SupervisorPromotionArtifactKind",
    "SupervisorPromotionArtifactReceipt",
    "SupervisorPromotionBundleReceipt",
    "SupervisorPromotionEvidenceProducerError",
    "SupervisorPromotionOperatorAttestation",
    "build_supervisor_assist_promotion_evidence",
    "build_supervisor_canary_promotion_evidence",
    "build_supervisor_latency_budget_document",
    "build_supervisor_promotion_bundle_payload",
    "canonical_json_file_bytes",
    "load_accepted_supervisor_production_baseline",
    "load_canonical_supervisor_latency_budget",
    "load_accepted_supervisor_promotion_bundle",
]
