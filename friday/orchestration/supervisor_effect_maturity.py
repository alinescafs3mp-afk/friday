"""Pure P5 evidence contract for one mature read-only supervisor journey.

The artifact produced here is body-free and self-contained, but inert.  It does
not enable a mode, admit an effect, change configuration, or grant publication
authority.  Acceptance is possible only by reloading the embedded production
baseline, CANARY promotion bundle, and latency budget through their existing
exact-hash loaders and then sealing the derived facts to this process.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, cast

from friday import semantic_supervisor_policy
from friday.orchestration.supervisor_assist_promotion import (
    SUPERVISOR_ASSIST_MAX_UNNECESSARY_CALL_RATE_BPS,
    SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS,
    AssistPromotionEvidenceAuthority,
    AssistPromotionOutcomeEvidence,
)
from friday.orchestration.supervisor_contracts import (
    SupervisorMode,
    TaskClass,
    canonical_sha256,
)
from friday.orchestration.supervisor_promotion_evidence_producer import (
    AcceptedCanonicalSupervisorLatencyBudget,
    AcceptedSupervisorProductionBaseline,
    AcceptedSupervisorPromotionBundle,
    SupervisorPromotionEvidenceProducerError,
    canonical_json_file_bytes,
    load_accepted_supervisor_production_baseline,
    load_accepted_supervisor_promotion_bundle,
    load_canonical_supervisor_latency_budget,
)

SUPERVISOR_READ_ONLY_MATURITY_ARTIFACT_SCHEMA = "friday.semantic-supervisor-read-only-maturity-artifact.v1"
SUPERVISOR_READ_ONLY_MATURITY_FACTS_SCHEMA = "friday.semantic-supervisor-read-only-maturity-facts.v1"
SUPERVISOR_READ_ONLY_MATURITY_WITNESS_SCHEMA = (
    "friday.semantic-supervisor-accepted-read-only-maturity-witness.v1"
)
SUPERVISOR_READ_ONLY_MATURITY_POLICY_ID = "semantic-supervisor-read-only-maturity-v1"

_MAX_ARTIFACT_BYTES = 4_194_304
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROCESS_AUTHORITY = object()
_PROCESS_KEY = secrets.token_bytes(32)
_ACCEPTANCE_SCOPE = "read_only_effect_maturity"
_PUBLICATION_OWNER = "primary"

_MATURITY_POLICY = {
    "schema": "friday.semantic-supervisor-read-only-maturity-policy.v1",
    "policy_id": SUPERVISOR_READ_ONLY_MATURITY_POLICY_ID,
    "task_class": TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB.value,
    "mature_mode": SupervisorMode.CANARY.value,
    "minimum_product_observations": SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS,
    "maximum_unnecessary_call_rate_bps": SUPERVISOR_ASSIST_MAX_UNNECESSARY_CALL_RATE_BPS,
    "joined_trace_coverage": "exact",
    "promotion_evidence_count": 1,
    "publication_owner": _PUBLICATION_OWNER,
    "zero_hidden_owners": True,
    "zero_duplicate_capabilities": True,
    "zero_duplicate_effects": True,
    "zero_duplicate_publications": True,
    "zero_false_completion_regressions": True,
    "zero_user_visible_regressions": True,
    "primary_fallback_required": True,
    "laptop_unavailable_fallback_required": True,
    "current_read_registry_binding_required": True,
    "current_effect_registry_binding_required": True,
    "activation_authority": False,
    "write_effect_authority": False,
}
SUPERVISOR_READ_ONLY_MATURITY_POLICY_SHA256 = canonical_sha256(_MATURITY_POLICY)

_ARTIFACT_KEYS = frozenset(
    {
        "schema",
        "body_free",
        "acceptance_scope",
        "maturity_accepted",
        "runtime_authority_granted",
        "activation_performed",
        "write_effect_authorized",
        "production_baseline",
        "canary_promotion_bundle",
        "canary_latency_budget",
        "maturity",
        "artifact_payload_sha256",
    }
)
_MATURITY_KEYS = frozenset(
    {
        "schema",
        "body_free",
        "authority",
        "task_class",
        "mature_mode",
        "maturity_policy_id",
        "maturity_policy_sha256",
        "production_baseline_file_sha256",
        "production_baseline_report_sha256",
        "canary_promotion_bundle_file_sha256",
        "canary_promotion_evidence_sha256",
        "canary_budget_file_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "effect_registry_binding_sha256",
        "canary_product_window_sha256",
        "canary_metric_window_sha256",
        "minimum_observation_count",
        "observation_count",
        "joined_trace_count",
        "promoted_observation_count",
        "promotion_evidence_count",
        "supervisor_invocation_count",
        "unnecessary_supervisor_invocation_count",
        "user_visible_observation_count",
        "maximum_user_visible_latency_ms",
        "latency_observation_count",
        "latency_total_ms",
        "latency_max_ms",
        "primary_fallback_proven",
        "laptop_unavailable_fallback_proven",
        "publication_owner",
        "primary_publication_owner_proven",
        "hidden_owner_count",
        "duplicate_capability_count",
        "duplicate_effect_count",
        "duplicate_publication_count",
        "false_completion_regression_count",
        "user_visible_regression_count",
    }
)


class SupervisorEffectMaturityError(ValueError):
    """The supplied evidence cannot prove the closed P5 maturity contract."""


def _digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise SupervisorEffectMaturityError(f"{label} must be a lowercase SHA-256")
    return value


def _count(value: object, *, label: str, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum:
        raise SupervisorEffectMaturityError(f"{label} must be an integer >= {minimum}")
    return value


def _exact_dict(value: object, expected: frozenset[str], *, label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise SupervisorEffectMaturityError(f"{label} keys do not match")
    return cast(dict[str, Any], value)


def _object(value: object, *, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise SupervisorEffectMaturityError(f"{label} must be one JSON object")
    return cast(dict[str, object], value)


def _reject_constant(value: str) -> None:
    raise SupervisorEffectMaturityError(f"non-finite JSON constant is forbidden: {value}")


def _closed_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise SupervisorEffectMaturityError("duplicate JSON key is forbidden")
        result[key] = value
    return result


def _decode_closed_json(raw: bytes, *, label: str) -> dict[str, Any]:
    if type(raw) is not bytes:
        raise TypeError(f"{label} must be bytes")
    try:
        decoded = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_closed_object,
            parse_constant=_reject_constant,
        )
    except SupervisorEffectMaturityError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise SupervisorEffectMaturityError(f"{label} JSON is malformed") from exc
    if type(decoded) is not dict:
        raise SupervisorEffectMaturityError(f"{label} must be one JSON object")
    return cast(dict[str, Any], decoded)


def _process_seal(fields: tuple[object, ...]) -> str:
    return hmac.new(_PROCESS_KEY, repr(fields).encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class AcceptedReadOnlyMaturityWitness:
    """Process-sealed acceptance of one exact, inert P5 artifact."""

    artifact_file_sha256: str
    artifact_payload_sha256: str
    maturity_facts_sha256: str
    production_baseline_file_sha256: str
    production_baseline_report_sha256: str
    canary_promotion_bundle_file_sha256: str
    canary_promotion_evidence_sha256: str
    canary_budget_file_sha256: str
    source_revision_sha256: str
    registry_binding_sha256: str
    effect_registry_binding_sha256: str
    canary_product_window_sha256: str
    canary_metric_window_sha256: str
    observation_count: int
    joined_trace_count: int
    maximum_user_visible_latency_ms: int
    primary_fallback_proven: bool
    laptop_unavailable_fallback_proven: bool
    primary_publication_owner_proven: bool
    hidden_owner_count: int
    duplicate_capability_count: int
    duplicate_effect_count: int
    duplicate_publication_count: int
    false_completion_regression_count: int
    user_visible_regression_count: int
    _process_authority: object = field(repr=False, compare=False)
    _process_seal_sha256: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        values = (
            self.artifact_file_sha256,
            self.artifact_payload_sha256,
            self.maturity_facts_sha256,
            self.production_baseline_file_sha256,
            self.production_baseline_report_sha256,
            self.canary_promotion_bundle_file_sha256,
            self.canary_promotion_evidence_sha256,
            self.canary_budget_file_sha256,
            self.source_revision_sha256,
            self.registry_binding_sha256,
            self.effect_registry_binding_sha256,
            self.canary_product_window_sha256,
            self.canary_metric_window_sha256,
            self.observation_count,
            self.joined_trace_count,
            self.maximum_user_visible_latency_ms,
            self.primary_fallback_proven,
            self.laptop_unavailable_fallback_proven,
            self.primary_publication_owner_proven,
            self.hidden_owner_count,
            self.duplicate_capability_count,
            self.duplicate_effect_count,
            self.duplicate_publication_count,
            self.false_completion_regression_count,
            self.user_visible_regression_count,
        )
        for label, value in (
            ("artifact_file_sha256", self.artifact_file_sha256),
            ("artifact_payload_sha256", self.artifact_payload_sha256),
            ("maturity_facts_sha256", self.maturity_facts_sha256),
            ("production_baseline_file_sha256", self.production_baseline_file_sha256),
            ("production_baseline_report_sha256", self.production_baseline_report_sha256),
            (
                "canary_promotion_bundle_file_sha256",
                self.canary_promotion_bundle_file_sha256,
            ),
            ("canary_promotion_evidence_sha256", self.canary_promotion_evidence_sha256),
            ("canary_budget_file_sha256", self.canary_budget_file_sha256),
            ("source_revision_sha256", self.source_revision_sha256),
            ("registry_binding_sha256", self.registry_binding_sha256),
            ("effect_registry_binding_sha256", self.effect_registry_binding_sha256),
            ("canary_product_window_sha256", self.canary_product_window_sha256),
            ("canary_metric_window_sha256", self.canary_metric_window_sha256),
        ):
            _digest(value, label=label)
        _count(self.observation_count, label="observation_count", positive=True)
        _count(self.joined_trace_count, label="joined_trace_count", positive=True)
        _count(
            self.maximum_user_visible_latency_ms,
            label="maximum_user_visible_latency_ms",
            positive=True,
        )
        for label, count in (
            ("hidden_owner_count", self.hidden_owner_count),
            ("duplicate_capability_count", self.duplicate_capability_count),
            ("duplicate_effect_count", self.duplicate_effect_count),
            ("duplicate_publication_count", self.duplicate_publication_count),
            (
                "false_completion_regression_count",
                self.false_completion_regression_count,
            ),
            ("user_visible_regression_count", self.user_visible_regression_count),
        ):
            if type(count) is not int or count != 0:
                raise SupervisorEffectMaturityError(f"{label} must be exactly zero")
        _digest(self._process_seal_sha256, label="process seal")
        if (
            self.observation_count < SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
            or self.joined_trace_count != self.observation_count
            or self.primary_fallback_proven is not True
            or self.laptop_unavailable_fallback_proven is not True
            or self.primary_publication_owner_proven is not True
            or self._process_authority is not _PROCESS_AUTHORITY
            or not hmac.compare_digest(self._process_seal_sha256, _process_seal(values))
        ):
            raise SupervisorEffectMaturityError("maturity witness was not accepted by this process")

    def payload(self) -> dict[str, object]:
        """Return only body-free accepted identities and aggregate facts."""

        return {
            "schema": SUPERVISOR_READ_ONLY_MATURITY_WITNESS_SCHEMA,
            "artifact_file_sha256": self.artifact_file_sha256,
            "artifact_payload_sha256": self.artifact_payload_sha256,
            "maturity_facts_sha256": self.maturity_facts_sha256,
            "production_baseline_file_sha256": self.production_baseline_file_sha256,
            "production_baseline_report_sha256": self.production_baseline_report_sha256,
            "canary_promotion_bundle_file_sha256": (self.canary_promotion_bundle_file_sha256),
            "canary_promotion_evidence_sha256": self.canary_promotion_evidence_sha256,
            "canary_budget_file_sha256": self.canary_budget_file_sha256,
            "source_revision_sha256": self.source_revision_sha256,
            "registry_binding_sha256": self.registry_binding_sha256,
            "effect_registry_binding_sha256": self.effect_registry_binding_sha256,
            "canary_product_window_sha256": self.canary_product_window_sha256,
            "canary_metric_window_sha256": self.canary_metric_window_sha256,
            "observation_count": self.observation_count,
            "joined_trace_count": self.joined_trace_count,
            "maximum_user_visible_latency_ms": self.maximum_user_visible_latency_ms,
            "primary_fallback_proven": self.primary_fallback_proven,
            "laptop_unavailable_fallback_proven": self.laptop_unavailable_fallback_proven,
            "publication_owner": _PUBLICATION_OWNER,
            "primary_publication_owner_proven": self.primary_publication_owner_proven,
            "hidden_owner_count": self.hidden_owner_count,
            "duplicate_capability_count": self.duplicate_capability_count,
            "duplicate_effect_count": self.duplicate_effect_count,
            "duplicate_publication_count": self.duplicate_publication_count,
            "false_completion_regression_count": self.false_completion_regression_count,
            "user_visible_regression_count": self.user_visible_regression_count,
            "body_free": True,
            "acceptance_scope": _ACCEPTANCE_SCOPE,
            "runtime_authority_granted": False,
            "activation_performed": False,
            "write_effect_authorized": False,
        }

    def canonical_sha256(self) -> str:
        return canonical_sha256(self.payload())


def accepted_read_only_maturity_witness_is_current(value: object) -> bool:
    """Revalidate the exact witness invariants and process HMAC after construction."""

    if type(value) is not AcceptedReadOnlyMaturityWitness:
        return False
    try:
        AcceptedReadOnlyMaturityWitness.__post_init__(value)
    except (AttributeError, TypeError, SupervisorEffectMaturityError):
        return False
    return True


def _load_accepted_inputs(
    *,
    production_baseline_raw: bytes,
    expected_production_baseline_file_sha256: str,
    canary_promotion_bundle_raw: bytes,
    expected_canary_promotion_bundle_file_sha256: str,
    canary_budget_raw: bytes,
    expected_canary_budget_file_sha256: str,
) -> tuple[
    AcceptedSupervisorProductionBaseline,
    AcceptedSupervisorPromotionBundle,
    AcceptedCanonicalSupervisorLatencyBudget,
]:
    for label, raw in (
        ("production baseline", production_baseline_raw),
        ("CANARY promotion bundle", canary_promotion_bundle_raw),
        ("CANARY latency budget", canary_budget_raw),
    ):
        if type(raw) is not bytes:
            raise TypeError(f"{label} must be bytes")
    try:
        baseline = load_accepted_supervisor_production_baseline(
            production_baseline_raw,
            expected_file_sha256=expected_production_baseline_file_sha256,
        )
        budget = load_canonical_supervisor_latency_budget(
            canary_budget_raw,
            expected_file_sha256=expected_canary_budget_file_sha256,
        )
        bundle = load_accepted_supervisor_promotion_bundle(
            canary_promotion_bundle_raw,
            expected_file_sha256=expected_canary_promotion_bundle_file_sha256,
            budget_raw=canary_budget_raw,
            expected_budget_file_sha256=expected_canary_budget_file_sha256,
        )
    except (TypeError, SupervisorPromotionEvidenceProducerError) as exc:
        raise SupervisorEffectMaturityError("maturity inputs were not accepted") from exc
    return baseline, bundle, budget


def _derive_maturity_facts(
    *,
    production_baseline_raw: bytes,
    expected_production_baseline_file_sha256: str,
    canary_promotion_bundle_raw: bytes,
    expected_canary_promotion_bundle_file_sha256: str,
    canary_budget_raw: bytes,
    expected_canary_budget_file_sha256: str,
    expected_source_revision_sha256: str,
    expected_registry_binding_sha256: str,
    expected_effect_registry_binding_sha256: str,
) -> dict[str, object]:
    expected_source = _digest(expected_source_revision_sha256, label="expected source revision")
    expected_registry = _digest(
        expected_registry_binding_sha256,
        label="expected read registry binding",
    )
    expected_effect_registry = _digest(
        expected_effect_registry_binding_sha256,
        label="expected effect registry binding",
    )
    baseline, bundle, budget = _load_accepted_inputs(
        production_baseline_raw=production_baseline_raw,
        expected_production_baseline_file_sha256=(expected_production_baseline_file_sha256),
        canary_promotion_bundle_raw=canary_promotion_bundle_raw,
        expected_canary_promotion_bundle_file_sha256=(expected_canary_promotion_bundle_file_sha256),
        canary_budget_raw=canary_budget_raw,
        expected_canary_budget_file_sha256=expected_canary_budget_file_sha256,
    )
    evidence = bundle.evidence
    product = evidence.product_evidence
    expected_observed_policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(
        SupervisorMode.ASSIST
    )
    expected_target_policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(
        SupervisorMode.CANARY
    )
    if (
        budget.document.target_mode is not SupervisorMode.CANARY
        or not hmac.compare_digest(budget.document.source_revision_sha256, expected_source)
        or evidence.authority is not AssistPromotionEvidenceAuthority.PRODUCTION_JOINED
        or evidence.observed_mode is not SupervisorMode.ASSIST
        or evidence.task_class is not TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB
        or type(product) is not AssistPromotionOutcomeEvidence
        or evidence.observed_policy_id != expected_observed_policy.policy_id
        or not hmac.compare_digest(
            evidence.observed_policy_sha256,
            expected_observed_policy.policy_sha256,
        )
        or evidence.target_policy_id != expected_target_policy.policy_id
        or not hmac.compare_digest(
            evidence.target_policy_sha256,
            expected_target_policy.policy_sha256,
        )
        or not hmac.compare_digest(evidence.source_revision_sha256, expected_source)
        or not hmac.compare_digest(evidence.registry_binding_sha256, expected_registry)
        or not hmac.compare_digest(
            product.latency_budget_source_revision_sha256,
            expected_source,
        )
        or not hmac.compare_digest(
            product.latency_budget_sha256,
            budget.document_sha256,
        )
        or product.latency_budget_target_mode is not SupervisorMode.CANARY
        or product.latency_budget_ms != budget.document.maximum_user_visible_latency_ms
    ):
        raise SupervisorEffectMaturityError("CANARY promotion evidence is stale or mismatched")

    canary = baseline.canary_execution
    promoted = canary.promoted
    evidence_sha256 = evidence.canonical_sha256()
    minimum = SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
    latency_budget_ms = budget.document.maximum_user_visible_latency_ms
    if (
        canary.mode is not SupervisorMode.CANARY
        or canary.observation_count < minimum
        or canary.joined_trace_count != canary.observation_count
        or canary.promotion_evidence_count != 1
        or canary.promotion_evidence_sha256 is None
        or not hmac.compare_digest(canary.promotion_evidence_sha256, evidence_sha256)
        or promoted.observation_count != canary.observation_count
        or promoted.complete_count != canary.observation_count
        or promoted.failure_class_counts != (("none:none", canary.observation_count),)
        or promoted.latency_observation_count != canary.observation_count
        or canary.user_visible_observation_count != canary.observation_count
        or canary.supervisor_invocation_count != canary.observation_count
        or canary.user_visible_regression_count != 0
        or promoted.latency_max_ms > latency_budget_ms
        or promoted.latency_total_ms > latency_budget_ms * canary.observation_count
        or canary.unnecessary_supervisor_invocation_count * 10_000
        > canary.supervisor_invocation_count * SUPERVISOR_ASSIST_MAX_UNNECESSARY_CALL_RATE_BPS
    ):
        raise SupervisorEffectMaturityError(
            "CANARY execution window is not mature, fully joined, or evidence-bound"
        )
    if (
        not evidence.representative_window_attested
        or not evidence.primary_fallback_proven
        or not evidence.laptop_unavailable_fallback_proven
        or not evidence.final_authority_recheck_proven
        or not evidence.primary_publication_owner_proven
        or any(
            (
                evidence.hidden_owner_count,
                evidence.duplicate_capability_count,
                evidence.duplicate_effect_count,
                evidence.duplicate_publication_count,
                evidence.false_completion_regression_count,
            )
        )
    ):
        raise SupervisorEffectMaturityError("CANARY promotion invariants do not prove one read-only owner")

    return {
        "schema": SUPERVISOR_READ_ONLY_MATURITY_FACTS_SCHEMA,
        "body_free": True,
        "authority": AssistPromotionEvidenceAuthority.PRODUCTION_JOINED.value,
        "task_class": TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB.value,
        "mature_mode": SupervisorMode.CANARY.value,
        "maturity_policy_id": SUPERVISOR_READ_ONLY_MATURITY_POLICY_ID,
        "maturity_policy_sha256": SUPERVISOR_READ_ONLY_MATURITY_POLICY_SHA256,
        "production_baseline_file_sha256": baseline.file_sha256,
        "production_baseline_report_sha256": baseline.report_sha256,
        "canary_promotion_bundle_file_sha256": bundle.bundle_file_sha256,
        "canary_promotion_evidence_sha256": evidence_sha256,
        "canary_budget_file_sha256": budget.document_sha256,
        "source_revision_sha256": expected_source,
        "registry_binding_sha256": expected_registry,
        "effect_registry_binding_sha256": expected_effect_registry,
        "canary_product_window_sha256": canary.product_window_sha256,
        "canary_metric_window_sha256": promoted.window_sha256,
        "minimum_observation_count": minimum,
        "observation_count": canary.observation_count,
        "joined_trace_count": canary.joined_trace_count,
        "promoted_observation_count": promoted.observation_count,
        "promotion_evidence_count": canary.promotion_evidence_count,
        "supervisor_invocation_count": canary.supervisor_invocation_count,
        "unnecessary_supervisor_invocation_count": (canary.unnecessary_supervisor_invocation_count),
        "user_visible_observation_count": canary.user_visible_observation_count,
        "maximum_user_visible_latency_ms": latency_budget_ms,
        "latency_observation_count": promoted.latency_observation_count,
        "latency_total_ms": promoted.latency_total_ms,
        "latency_max_ms": promoted.latency_max_ms,
        "primary_fallback_proven": evidence.primary_fallback_proven,
        "laptop_unavailable_fallback_proven": (evidence.laptop_unavailable_fallback_proven),
        "publication_owner": _PUBLICATION_OWNER,
        "primary_publication_owner_proven": evidence.primary_publication_owner_proven,
        "hidden_owner_count": evidence.hidden_owner_count,
        "duplicate_capability_count": evidence.duplicate_capability_count,
        "duplicate_effect_count": evidence.duplicate_effect_count,
        "duplicate_publication_count": evidence.duplicate_publication_count,
        "false_completion_regression_count": evidence.false_completion_regression_count,
        "user_visible_regression_count": canary.user_visible_regression_count,
    }


def build_read_only_maturity_artifact(
    *,
    production_baseline_raw: bytes,
    expected_production_baseline_file_sha256: str,
    canary_promotion_bundle_raw: bytes,
    expected_canary_promotion_bundle_file_sha256: str,
    canary_budget_raw: bytes,
    expected_canary_budget_file_sha256: str,
    expected_source_revision_sha256: str,
    expected_registry_binding_sha256: str,
    expected_effect_registry_binding_sha256: str,
) -> bytes:
    """Build canonical, self-contained P5 evidence without granting authority."""

    maturity = _derive_maturity_facts(
        production_baseline_raw=production_baseline_raw,
        expected_production_baseline_file_sha256=(expected_production_baseline_file_sha256),
        canary_promotion_bundle_raw=canary_promotion_bundle_raw,
        expected_canary_promotion_bundle_file_sha256=(expected_canary_promotion_bundle_file_sha256),
        canary_budget_raw=canary_budget_raw,
        expected_canary_budget_file_sha256=expected_canary_budget_file_sha256,
        expected_source_revision_sha256=expected_source_revision_sha256,
        expected_registry_binding_sha256=expected_registry_binding_sha256,
        expected_effect_registry_binding_sha256=(expected_effect_registry_binding_sha256),
    )
    payload: dict[str, object] = {
        "schema": SUPERVISOR_READ_ONLY_MATURITY_ARTIFACT_SCHEMA,
        "body_free": True,
        "acceptance_scope": _ACCEPTANCE_SCOPE,
        "maturity_accepted": True,
        "runtime_authority_granted": False,
        "activation_performed": False,
        "write_effect_authorized": False,
        "production_baseline": _decode_closed_json(
            production_baseline_raw,
            label="production baseline",
        ),
        "canary_promotion_bundle": _decode_closed_json(
            canary_promotion_bundle_raw,
            label="CANARY promotion bundle",
        ),
        "canary_latency_budget": _decode_closed_json(
            canary_budget_raw,
            label="CANARY latency budget",
        ),
        "maturity": maturity,
    }
    payload["artifact_payload_sha256"] = canonical_sha256(payload)
    raw = canonical_json_file_bytes(payload)
    if len(raw) > _MAX_ARTIFACT_BYTES:
        raise SupervisorEffectMaturityError("maturity artifact exceeds its byte bound")
    return raw


def load_accepted_read_only_maturity_witness(
    raw: bytes,
    *,
    expected_file_sha256: str,
    expected_source_revision_sha256: str,
    expected_registry_binding_sha256: str,
    expected_effect_registry_binding_sha256: str,
) -> AcceptedReadOnlyMaturityWitness:
    """Reload every embedded input and return one process-sealed P5 witness."""

    if type(raw) is not bytes:
        raise TypeError("maturity artifact loader requires bytes")
    expected_file = _digest(expected_file_sha256, label="expected maturity artifact digest")
    if not 0 < len(raw) <= _MAX_ARTIFACT_BYTES or not hmac.compare_digest(
        hashlib.sha256(raw).hexdigest(), expected_file
    ):
        raise SupervisorEffectMaturityError("maturity artifact digest does not match")
    artifact = _exact_dict(
        _decode_closed_json(raw, label="maturity artifact"),
        _ARTIFACT_KEYS,
        label="maturity artifact",
    )
    if (
        artifact["schema"] != SUPERVISOR_READ_ONLY_MATURITY_ARTIFACT_SCHEMA
        or artifact["body_free"] is not True
        or artifact["acceptance_scope"] != _ACCEPTANCE_SCOPE
        or artifact["maturity_accepted"] is not True
        or artifact["runtime_authority_granted"] is not False
        or artifact["activation_performed"] is not False
        or artifact["write_effect_authorized"] is not False
        or raw != canonical_json_file_bytes(artifact)
    ):
        raise SupervisorEffectMaturityError("maturity artifact identity is invalid")
    payload_sha256 = _digest(
        artifact["artifact_payload_sha256"],
        label="maturity artifact self-digest",
    )
    unsigned = dict(artifact)
    unsigned.pop("artifact_payload_sha256")
    if not hmac.compare_digest(canonical_sha256(unsigned), payload_sha256):
        raise SupervisorEffectMaturityError("maturity artifact self-digest does not match")
    maturity = _exact_dict(artifact["maturity"], _MATURITY_KEYS, label="maturity facts")
    baseline_raw = canonical_json_file_bytes(
        _object(
            artifact["production_baseline"],
            label="embedded production baseline",
        )
    )
    bundle_raw = canonical_json_file_bytes(
        _object(
            artifact["canary_promotion_bundle"],
            label="embedded CANARY promotion bundle",
        )
    )
    budget_raw = canonical_json_file_bytes(
        _object(
            artifact["canary_latency_budget"],
            label="embedded CANARY latency budget",
        )
    )
    rebuilt = _derive_maturity_facts(
        production_baseline_raw=baseline_raw,
        expected_production_baseline_file_sha256=_digest(
            maturity["production_baseline_file_sha256"],
            label="embedded production baseline digest",
        ),
        canary_promotion_bundle_raw=bundle_raw,
        expected_canary_promotion_bundle_file_sha256=_digest(
            maturity["canary_promotion_bundle_file_sha256"],
            label="embedded CANARY promotion bundle digest",
        ),
        canary_budget_raw=budget_raw,
        expected_canary_budget_file_sha256=_digest(
            maturity["canary_budget_file_sha256"],
            label="embedded CANARY budget digest",
        ),
        expected_source_revision_sha256=expected_source_revision_sha256,
        expected_registry_binding_sha256=expected_registry_binding_sha256,
        expected_effect_registry_binding_sha256=(expected_effect_registry_binding_sha256),
    )
    if maturity != rebuilt:
        raise SupervisorEffectMaturityError("maturity facts were not derived from the inputs")
    facts_sha256 = canonical_sha256(rebuilt)
    values = (
        expected_file,
        payload_sha256,
        facts_sha256,
        rebuilt["production_baseline_file_sha256"],
        rebuilt["production_baseline_report_sha256"],
        rebuilt["canary_promotion_bundle_file_sha256"],
        rebuilt["canary_promotion_evidence_sha256"],
        rebuilt["canary_budget_file_sha256"],
        rebuilt["source_revision_sha256"],
        rebuilt["registry_binding_sha256"],
        rebuilt["effect_registry_binding_sha256"],
        rebuilt["canary_product_window_sha256"],
        rebuilt["canary_metric_window_sha256"],
        rebuilt["observation_count"],
        rebuilt["joined_trace_count"],
        rebuilt["maximum_user_visible_latency_ms"],
        rebuilt["primary_fallback_proven"],
        rebuilt["laptop_unavailable_fallback_proven"],
        rebuilt["primary_publication_owner_proven"],
        rebuilt["hidden_owner_count"],
        rebuilt["duplicate_capability_count"],
        rebuilt["duplicate_effect_count"],
        rebuilt["duplicate_publication_count"],
        rebuilt["false_completion_regression_count"],
        rebuilt["user_visible_regression_count"],
    )
    return AcceptedReadOnlyMaturityWitness(
        artifact_file_sha256=expected_file,
        artifact_payload_sha256=payload_sha256,
        maturity_facts_sha256=facts_sha256,
        production_baseline_file_sha256=cast(
            str,
            rebuilt["production_baseline_file_sha256"],
        ),
        production_baseline_report_sha256=cast(
            str,
            rebuilt["production_baseline_report_sha256"],
        ),
        canary_promotion_bundle_file_sha256=cast(
            str,
            rebuilt["canary_promotion_bundle_file_sha256"],
        ),
        canary_promotion_evidence_sha256=cast(
            str,
            rebuilt["canary_promotion_evidence_sha256"],
        ),
        canary_budget_file_sha256=cast(str, rebuilt["canary_budget_file_sha256"]),
        source_revision_sha256=cast(str, rebuilt["source_revision_sha256"]),
        registry_binding_sha256=cast(str, rebuilt["registry_binding_sha256"]),
        effect_registry_binding_sha256=cast(
            str,
            rebuilt["effect_registry_binding_sha256"],
        ),
        canary_product_window_sha256=cast(
            str,
            rebuilt["canary_product_window_sha256"],
        ),
        canary_metric_window_sha256=cast(
            str,
            rebuilt["canary_metric_window_sha256"],
        ),
        observation_count=cast(int, rebuilt["observation_count"]),
        joined_trace_count=cast(int, rebuilt["joined_trace_count"]),
        maximum_user_visible_latency_ms=cast(
            int,
            rebuilt["maximum_user_visible_latency_ms"],
        ),
        primary_fallback_proven=cast(bool, rebuilt["primary_fallback_proven"]),
        laptop_unavailable_fallback_proven=cast(
            bool,
            rebuilt["laptop_unavailable_fallback_proven"],
        ),
        primary_publication_owner_proven=cast(
            bool,
            rebuilt["primary_publication_owner_proven"],
        ),
        hidden_owner_count=cast(int, rebuilt["hidden_owner_count"]),
        duplicate_capability_count=cast(int, rebuilt["duplicate_capability_count"]),
        duplicate_effect_count=cast(int, rebuilt["duplicate_effect_count"]),
        duplicate_publication_count=cast(int, rebuilt["duplicate_publication_count"]),
        false_completion_regression_count=cast(
            int,
            rebuilt["false_completion_regression_count"],
        ),
        user_visible_regression_count=cast(
            int,
            rebuilt["user_visible_regression_count"],
        ),
        _process_authority=_PROCESS_AUTHORITY,
        _process_seal_sha256=_process_seal(values),
    )


__all__ = [
    "AcceptedReadOnlyMaturityWitness",
    "SUPERVISOR_READ_ONLY_MATURITY_ARTIFACT_SCHEMA",
    "SUPERVISOR_READ_ONLY_MATURITY_FACTS_SCHEMA",
    "SUPERVISOR_READ_ONLY_MATURITY_POLICY_ID",
    "SUPERVISOR_READ_ONLY_MATURITY_POLICY_SHA256",
    "SUPERVISOR_READ_ONLY_MATURITY_WITNESS_SCHEMA",
    "SupervisorEffectMaturityError",
    "accepted_read_only_maturity_witness_is_current",
    "build_read_only_maturity_artifact",
    "load_accepted_read_only_maturity_witness",
]
