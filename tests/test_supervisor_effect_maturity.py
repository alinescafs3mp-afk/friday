from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass, replace
from typing import Any

import pytest

from friday import semantic_supervisor_policy
from friday.orchestration.capability_binding import expected_effect_capability_snapshot
from friday.orchestration.supervisor_assist_promotion import (
    SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS,
    AssistPromotionQualityBasis,
)
from friday.orchestration.supervisor_contracts import SupervisorMode, canonical_sha256
from friday.orchestration.supervisor_effect_maturity import (
    SUPERVISOR_READ_ONLY_MATURITY_ARTIFACT_SCHEMA,
    SUPERVISOR_READ_ONLY_MATURITY_POLICY_SHA256,
    AcceptedReadOnlyMaturityWitness,
    SupervisorEffectMaturityError,
    accepted_read_only_maturity_witness_is_current,
    build_read_only_maturity_artifact,
    load_accepted_read_only_maturity_witness,
)
from friday.orchestration.supervisor_production_baseline import (
    SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
    SUPERVISOR_PRODUCTION_BASELINE_KIND,
    SUPERVISOR_PRODUCTION_BASELINE_SCHEMA,
)
from friday.orchestration.supervisor_promotion_evidence_producer import (
    SupervisorPromotionOperatorAttestation,
    build_supervisor_canary_promotion_evidence,
    build_supervisor_latency_budget_document,
    build_supervisor_promotion_bundle_payload,
    canonical_json_file_bytes,
    load_accepted_supervisor_production_baseline,
    load_canonical_supervisor_latency_budget,
)
from friday.orchestration.supervisor_representative_window_attestation import (
    REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA,
    REPRESENTATIVE_WINDOW_AUTHORITY,
    REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA,
    representative_window_sha256,
)
from tools import build_semantic_supervisor_promotion_evidence as maturity_cli

SOURCE = "b" * 64
REGISTRY = "c" * 64
EFFECT_REGISTRY = "e" * 64
ASSIST_PRECURSOR = "d" * 64
FAILURE_CLASS = "capability:source_unavailable"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _metrics(
    *,
    stage: str,
    observations: int,
    complete: int,
    failure_counts: dict[str, int],
    latency_total: int,
    latency_max: int,
    window: str,
) -> dict[str, object]:
    completion_counts = {} if observations == 0 else {"complete": complete, "failed": observations - complete}
    return {
        "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
        "stage": stage,
        "observation_count": observations,
        "completion_counts": completion_counts,
        "complete_count": complete,
        "failure_class_counts": failure_counts,
        "latency_observation_count": observations,
        "latency_total_ms": latency_total,
        "latency_max_ms": latency_max,
        "window_sha256": window * 64,
    }


def _report(
    *,
    canary_observations: int = 0,
    canary_evidence_sha256: str | None = None,
) -> dict[str, Any]:
    shadow_metrics = _metrics(
        stage="shadow",
        observations=20,
        complete=8,
        failure_counts={FAILURE_CLASS: 5, "none:none": 15},
        latency_total=20_000,
        latency_max=1_000,
        window="1",
    )
    assist_metrics = _metrics(
        stage="assist",
        observations=20,
        complete=12,
        failure_counts={"none:none": 20},
        latency_total=18_000,
        latency_max=900,
        window="2",
    )
    canary_metrics = _metrics(
        stage="canary",
        observations=canary_observations,
        complete=canary_observations,
        failure_counts=({} if canary_observations == 0 else {"none:none": canary_observations}),
        latency_total=canary_observations * 800,
        latency_max=800 if canary_observations else 0,
        window="3",
    )
    report: dict[str, Any] = {
        "schema": SUPERVISOR_PRODUCTION_BASELINE_SCHEMA,
        "evidence": {
            "kind": SUPERVISOR_PRODUCTION_BASELINE_KIND,
            "body_free": True,
            "production_acceptance": False,
            "acceptance_authority": "operator_review_required",
            "representative_window_attested": False,
            "promotion_authority": False,
        },
        "sample": {
            "limit": 100,
            "turn_traces": 40,
            "joined_supervisor_events": 20,
            "promoted_product_events": 20 + canary_observations,
            "malformed_turn_traces": 0,
            "malformed_joined_events": 0,
            "malformed_promoted_product_events": 0,
            "duplicate_turn_trace_digests": 0,
            "duplicate_shadow_product_events": 0,
            "duplicate_promoted_product_events": 0,
            "unmatched_shadow_product_events": 0,
            "unmatched_promoted_product_events": 0,
        },
        "primary_baseline": {
            "intent_counts": {"dialogue": 40},
            "playbook_counts": {"dialogue": 40},
            "completion_counts": {"complete": 20, "failed": 20},
            "publication_counts": {"assistant_committed": 40},
            "failure_counts": {"none:none": 40},
            "authority_rechecked_count": 40,
            "partial_coverage_count": 0,
            "state_restored_count": 0,
        },
        "supervisor_join": {
            "task_counts": {"compare_current_file_with_current_web": 20},
            "skip_counts": {"none": 20},
            "parse_counts": {"parsed": 20},
            "policy_reason_counts": {"admitted": 20},
            "planner_latency_bucket_counts": {"250_999ms": 20},
            "actual_completion_counts": {"complete": 20},
            "actual_publication_counts": {"assistant_committed": 20},
            "actual_capability_outcome_counts": {},
            "invoked_count": 20,
            "admitted_count": 20,
            "final_authority_rechecked_count": 20,
            "state_restored_count": 0,
            "retry_occurred_count": 0,
        },
        "product_windows": {
            "shadow_readiness": {
                "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
                "mode": "shadow",
                "production_joined": True,
                "actual_promoted_execution": False,
                "quality_claim": "documented_baseline_failure_only",
                "observation_count": 20,
                "joined_trace_count": 20,
                "baseline": shadow_metrics,
                "readiness_observation_count": 20,
                "call_rate_observation_count": 20,
                "supervisor_invocation_count": 20,
                "unnecessary_supervisor_invocation_count": 0,
                "user_visible_observation_count": 20,
                "user_visible_regression_count": 0,
                "readiness_witness_sha256": "4" * 64,
            },
            "promoted_execution": {
                "assist": {
                    "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
                    "mode": "assist",
                    "production_joined": True,
                    "actual_promoted_execution": True,
                    "observation_count": 20,
                    "joined_trace_count": 20,
                    "promotion_evidence_count": 1,
                    "promotion_evidence_sha256": ASSIST_PRECURSOR,
                    "promoted": assist_metrics,
                    "call_rate_observation_count": 20,
                    "supervisor_invocation_count": 20,
                    "unnecessary_supervisor_invocation_count": 0,
                    "user_visible_observation_count": 20,
                    "user_visible_regression_count": 0,
                    "product_window_sha256": "5" * 64,
                },
                "canary": {
                    "schema": SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
                    "mode": "canary",
                    "production_joined": True,
                    "actual_promoted_execution": True,
                    "observation_count": canary_observations,
                    "joined_trace_count": canary_observations,
                    "promotion_evidence_count": (1 if canary_evidence_sha256 is not None else 0),
                    "promotion_evidence_sha256": canary_evidence_sha256,
                    "promoted": canary_metrics,
                    "call_rate_observation_count": canary_observations,
                    "supervisor_invocation_count": canary_observations,
                    "unnecessary_supervisor_invocation_count": 0,
                    "user_visible_observation_count": canary_observations,
                    "user_visible_regression_count": 0,
                    "product_window_sha256": "6" * 64,
                },
            },
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def _baseline_raw(report: dict[str, Any]) -> bytes:
    return canonical_json_file_bytes(report)


def _budget() -> tuple[bytes, Any]:
    document = build_supervisor_latency_budget_document(
        target_mode=SupervisorMode.CANARY,
        source_revision_sha256=SOURCE,
        maximum_user_visible_latency_ms=2_500,
    )
    raw = canonical_json_file_bytes(document.payload())
    accepted = load_canonical_supervisor_latency_budget(
        raw,
        expected_file_sha256=_sha256(raw),
    )
    return raw, accepted


def _attestation(baseline: Any, budget: Any) -> SupervisorPromotionOperatorAttestation:
    return SupervisorPromotionOperatorAttestation(
        target_mode=SupervisorMode.CANARY,
        baseline_file_sha256=baseline.file_sha256,
        baseline_report_sha256=baseline.report_sha256,
        latency_budget_file_sha256=budget.document_sha256,
        source_revision_sha256=SOURCE,
        registry_binding_sha256=REGISTRY,
        representative_window_attested=True,
        primary_fallback_proven=True,
        laptop_unavailable_fallback_proven=True,
        final_authority_recheck_proven=True,
        primary_publication_owner_proven=True,
        zero_hidden_owners_attested=True,
        zero_duplicate_capabilities_attested=True,
        zero_duplicate_effects_attested=True,
        zero_duplicate_publications_attested=True,
        zero_false_completion_regressions_attested=True,
        precursor_assist_promotion_evidence_sha256=ASSIST_PRECURSOR,
        quality_basis=AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT,
    )


def _representative_window_issue(baseline: Any, budget: Any) -> dict[str, Any]:
    lookup_token = "7" * 64
    server: dict[str, Any] = {
        "schema": REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA,
        "attestation_id": "sswindow_" + "6" * 32,
        "authority": REPRESENTATIVE_WINDOW_AUTHORITY,
        "target_mode": SupervisorMode.CANARY.value,
        "observed_mode": SupervisorMode.ASSIST.value,
        "baseline_file_sha256": baseline.file_sha256,
        "baseline_report_sha256": baseline.report_sha256,
        "latency_budget_file_sha256": budget.document_sha256,
        "latency_budget_document_sha256": budget.document_sha256,
        "latency_budget_target_mode": SupervisorMode.CANARY.value,
        "latency_budget_source_revision_sha256": SOURCE,
        "maximum_user_visible_latency_ms": 2_500,
        "precursor_assist_promotion_evidence_sha256": ASSIST_PRECURSOR,
        "source_revision_sha256": SOURCE,
        "registry_binding_sha256": REGISTRY,
        "primary_pid": 100,
        "primary_process_epoch_sha256": "5" * 64,
        "primary_backend_version": "test",
        "observed_release_commit": "4" * 40,
        "observed_release_metadata_sha256": "3" * 64,
        "observed_release_tree_sha256": "2" * 64,
        "observed_registry_binding_sha256": REGISTRY,
        "requested_mode": SupervisorMode.ASSIST.value,
        "supervisor_policy_id": semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID,
        "supervisor_policy_sha256": (semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256),
        "runtime_profile_id": semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": (
            semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        ),
        "observer_runner_sha256": "1" * 64,
        "sample_limit": 100,
        "turn_trace_count": 40,
        "joined_trace_count": baseline.assist_execution.joined_trace_count,
        "representative_window_sha256": baseline.assist_execution.product_window_sha256,
        "server_recomputed": True,
        "representative_window_attested": True,
        "synthetic_authority": False,
        "lookup_token_sha256": hashlib.sha256(lookup_token.encode("ascii")).hexdigest(),
        "state_version": 1,
        "issued_at": 1_000,
        "expires_at": 1_500,
        "signature": "9" * 64,
    }
    return {
        "schema": REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA,
        "status": "unused",
        "server_attestation": server,
        "server_attestation_sha256": representative_window_sha256(server),
        "attestation_lookup_token": lookup_token,
        "lookup_token_sha256": server["lookup_token_sha256"],
        "state_version": 1,
    }


@dataclass(frozen=True, slots=True)
class _Inputs:
    mature_baseline_raw: bytes
    bundle_raw: bytes
    budget_raw: bytes
    canary_evidence_sha256: str


@pytest.fixture
def maturity_inputs() -> _Inputs:
    precursor_raw = _baseline_raw(_report())
    precursor = load_accepted_supervisor_production_baseline(
        precursor_raw,
        expected_file_sha256=_sha256(precursor_raw),
    )
    budget_raw, budget = _budget()
    attestation = _attestation(precursor, budget)
    evidence = build_supervisor_canary_promotion_evidence(
        evidence_id="p5_canary_promotion_window",
        baseline=precursor,
        budget=budget,
        attestation=attestation,
    )
    bundle_raw = canonical_json_file_bytes(
        build_supervisor_promotion_bundle_payload(
            baseline_raw=precursor_raw,
            budget=budget,
            attestation=attestation,
            representative_window_issue=_representative_window_issue(precursor, budget),
            evidence=evidence,
        )
    )
    mature_raw = _baseline_raw(
        _report(
            canary_observations=SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS,
            canary_evidence_sha256=evidence.canonical_sha256(),
        )
    )
    return _Inputs(
        mature_baseline_raw=mature_raw,
        bundle_raw=bundle_raw,
        budget_raw=budget_raw,
        canary_evidence_sha256=evidence.canonical_sha256(),
    )


def _build_artifact(inputs: _Inputs) -> bytes:
    return build_read_only_maturity_artifact(
        production_baseline_raw=inputs.mature_baseline_raw,
        expected_production_baseline_file_sha256=_sha256(inputs.mature_baseline_raw),
        canary_promotion_bundle_raw=inputs.bundle_raw,
        expected_canary_promotion_bundle_file_sha256=_sha256(inputs.bundle_raw),
        canary_budget_raw=inputs.budget_raw,
        expected_canary_budget_file_sha256=_sha256(inputs.budget_raw),
        expected_source_revision_sha256=SOURCE,
        expected_registry_binding_sha256=REGISTRY,
        expected_effect_registry_binding_sha256=EFFECT_REGISTRY,
    )


def _load_artifact(raw: bytes) -> AcceptedReadOnlyMaturityWitness:
    return load_accepted_read_only_maturity_witness(
        raw,
        expected_file_sha256=_sha256(raw),
        expected_source_revision_sha256=SOURCE,
        expected_registry_binding_sha256=REGISTRY,
        expected_effect_registry_binding_sha256=EFFECT_REGISTRY,
    )


def _resign_artifact(payload: dict[str, Any]) -> bytes:
    payload.pop("artifact_payload_sha256", None)
    payload["artifact_payload_sha256"] = canonical_sha256(payload)
    return canonical_json_file_bytes(payload)


def test_canonical_artifact_round_trip_returns_process_sealed_witness(
    maturity_inputs: _Inputs,
) -> None:
    raw = _build_artifact(maturity_inputs)
    assert raw == _build_artifact(maturity_inputs)
    assert raw == canonical_json_file_bytes(json.loads(raw))

    artifact = json.loads(raw)
    assert artifact["schema"] == SUPERVISOR_READ_ONLY_MATURITY_ARTIFACT_SCHEMA
    assert artifact["canary_promotion_bundle"]["promotion_evidence"]["registry_binding_sha256"] == REGISTRY
    assert artifact["maturity"]["registry_binding_sha256"] == REGISTRY
    assert artifact["maturity"]["effect_registry_binding_sha256"] == EFFECT_REGISTRY
    assert artifact["runtime_authority_granted"] is False
    assert artifact["activation_performed"] is False
    assert artifact["write_effect_authorized"] is False

    witness = _load_artifact(raw)
    assert type(witness) is AcceptedReadOnlyMaturityWitness
    assert witness.artifact_file_sha256 == _sha256(raw)
    assert witness.canary_promotion_evidence_sha256 == maturity_inputs.canary_evidence_sha256
    assert witness.registry_binding_sha256 == REGISTRY
    assert witness.effect_registry_binding_sha256 == EFFECT_REGISTRY
    assert witness.observation_count == witness.joined_trace_count == 20
    assert witness.primary_fallback_proven is True
    assert witness.laptop_unavailable_fallback_proven is True
    assert witness.primary_publication_owner_proven is True
    assert witness.hidden_owner_count == 0
    assert witness.duplicate_effect_count == witness.duplicate_publication_count == 0
    assert artifact["maturity"]["maturity_policy_sha256"] == (SUPERVISOR_READ_ONLY_MATURITY_POLICY_SHA256)
    assert witness.payload()["runtime_authority_granted"] is False
    assert accepted_read_only_maturity_witness_is_current(witness) is True
    assert accepted_read_only_maturity_witness_is_current(object()) is False
    with pytest.raises(SupervisorEffectMaturityError):
        load_accepted_read_only_maturity_witness(
            raw,
            expected_file_sha256=_sha256(raw),
            expected_source_revision_sha256=SOURCE,
            expected_registry_binding_sha256=REGISTRY,
            expected_effect_registry_binding_sha256=REGISTRY,
        )
    with pytest.raises(SupervisorEffectMaturityError):
        load_accepted_read_only_maturity_witness(
            raw,
            expected_file_sha256=_sha256(raw),
            expected_source_revision_sha256=SOURCE,
            expected_registry_binding_sha256="f" * 64,
            expected_effect_registry_binding_sha256=EFFECT_REGISTRY,
        )

    with pytest.raises(SupervisorEffectMaturityError):
        replace(witness, artifact_file_sha256="0" * 64)

    object.__setattr__(witness, "duplicate_effect_count", 1)
    assert accepted_read_only_maturity_witness_is_current(witness) is False


@pytest.mark.parametrize(
    ("observations", "evidence_binding"),
    ((0, "missing"), (19, "exact"), (20, "stale")),
)
def test_builder_rejects_missing_immature_or_stale_canary_window(
    maturity_inputs: _Inputs,
    observations: int,
    evidence_binding: str,
) -> None:
    evidence_sha256 = {
        "missing": None,
        "exact": maturity_inputs.canary_evidence_sha256,
        "stale": "f" * 64,
    }[evidence_binding]
    baseline_raw = _baseline_raw(
        _report(
            canary_observations=observations,
            canary_evidence_sha256=evidence_sha256,
        )
    )
    with pytest.raises(SupervisorEffectMaturityError):
        build_read_only_maturity_artifact(
            production_baseline_raw=baseline_raw,
            expected_production_baseline_file_sha256=_sha256(baseline_raw),
            canary_promotion_bundle_raw=maturity_inputs.bundle_raw,
            expected_canary_promotion_bundle_file_sha256=_sha256(maturity_inputs.bundle_raw),
            canary_budget_raw=maturity_inputs.budget_raw,
            expected_canary_budget_file_sha256=_sha256(maturity_inputs.budget_raw),
            expected_source_revision_sha256=SOURCE,
            expected_registry_binding_sha256=REGISTRY,
            expected_effect_registry_binding_sha256=EFFECT_REGISTRY,
        )


@pytest.mark.parametrize(
    ("complete_count", "failure_counts"),
    (
        (19, {"none:none": 20}),
        (20, {FAILURE_CLASS: 1, "none:none": 19}),
    ),
)
def test_builder_requires_every_canary_observation_complete_and_failure_free(
    maturity_inputs: _Inputs,
    complete_count: int,
    failure_counts: dict[str, int],
) -> None:
    observations = SUPERVISOR_ASSIST_PROMOTION_MIN_PRODUCT_OBSERVATIONS
    report = _report(
        canary_observations=observations,
        canary_evidence_sha256=maturity_inputs.canary_evidence_sha256,
    )
    promoted = report["product_windows"]["promoted_execution"]["canary"]["promoted"]
    promoted["complete_count"] = complete_count
    promoted["completion_counts"] = {
        "complete": complete_count,
        "failed": observations - complete_count,
    }
    promoted["failure_class_counts"] = failure_counts
    report.pop("report_sha256")
    report["report_sha256"] = canonical_sha256(report)
    baseline_raw = _baseline_raw(report)

    with pytest.raises(SupervisorEffectMaturityError, match="not mature"):
        build_read_only_maturity_artifact(
            production_baseline_raw=baseline_raw,
            expected_production_baseline_file_sha256=_sha256(baseline_raw),
            canary_promotion_bundle_raw=maturity_inputs.bundle_raw,
            expected_canary_promotion_bundle_file_sha256=_sha256(maturity_inputs.bundle_raw),
            canary_budget_raw=maturity_inputs.budget_raw,
            expected_canary_budget_file_sha256=_sha256(maturity_inputs.budget_raw),
            expected_source_revision_sha256=SOURCE,
            expected_registry_binding_sha256=REGISTRY,
            expected_effect_registry_binding_sha256=EFFECT_REGISTRY,
        )


@pytest.mark.parametrize(
    "field",
    (
        "primary_fallback_proven",
        "laptop_unavailable_fallback_proven",
        "primary_publication_owner_proven",
        "hidden_owner_count",
        "duplicate_effect_count",
        "duplicate_publication_count",
    ),
)
def test_loader_rejects_forged_owner_fallback_or_duplicate_claim(
    maturity_inputs: _Inputs,
    field: str,
) -> None:
    payload = json.loads(_build_artifact(maturity_inputs))
    evidence = payload["canary_promotion_bundle"]["promotion_evidence"]
    evidence[field] = 1 if field.endswith("_count") else False
    embedded_bundle = canonical_json_file_bytes(payload["canary_promotion_bundle"])
    payload["maturity"]["canary_promotion_bundle_file_sha256"] = _sha256(embedded_bundle)
    forged = _resign_artifact(payload)

    with pytest.raises(SupervisorEffectMaturityError):
        _load_artifact(forged)


@pytest.mark.parametrize(
    "case",
    ("wrong_hash", "noncanonical", "missing_component", "forged_facts", "stale_source"),
)
def test_loader_fails_closed_for_malformed_forged_stale_or_incomplete_artifact(
    maturity_inputs: _Inputs,
    case: str,
) -> None:
    raw = _build_artifact(maturity_inputs)
    payload = json.loads(raw)
    if case == "wrong_hash":
        with pytest.raises(SupervisorEffectMaturityError):
            load_accepted_read_only_maturity_witness(
                raw,
                expected_file_sha256="0" * 64,
                expected_source_revision_sha256=SOURCE,
                expected_registry_binding_sha256=REGISTRY,
                expected_effect_registry_binding_sha256=EFFECT_REGISTRY,
            )
        return
    if case == "noncanonical":
        candidate = json.dumps(payload, indent=2, sort_keys=True).encode()
    elif case == "missing_component":
        payload.pop("canary_latency_budget")
        candidate = _resign_artifact(payload)
    elif case == "forged_facts":
        payload["maturity"]["hidden_owner_count"] = 1
        candidate = _resign_artifact(payload)
    else:
        with pytest.raises(SupervisorEffectMaturityError):
            load_accepted_read_only_maturity_witness(
                raw,
                expected_file_sha256=_sha256(raw),
                expected_source_revision_sha256="f" * 64,
                expected_registry_binding_sha256=REGISTRY,
                expected_effect_registry_binding_sha256=EFFECT_REGISTRY,
            )
        return
    with pytest.raises(SupervisorEffectMaturityError):
        _load_artifact(candidate)


def test_loader_rejects_malformed_json_and_builder_requires_exact_component_hashes(
    maturity_inputs: _Inputs,
) -> None:
    malformed = b'{"schema":'
    with pytest.raises(SupervisorEffectMaturityError):
        _load_artifact(malformed)
    with pytest.raises(SupervisorEffectMaturityError):
        build_read_only_maturity_artifact(
            production_baseline_raw=maturity_inputs.mature_baseline_raw,
            expected_production_baseline_file_sha256="0" * 64,
            canary_promotion_bundle_raw=maturity_inputs.bundle_raw,
            expected_canary_promotion_bundle_file_sha256=_sha256(maturity_inputs.bundle_raw),
            canary_budget_raw=maturity_inputs.budget_raw,
            expected_canary_budget_file_sha256=_sha256(maturity_inputs.budget_raw),
            expected_source_revision_sha256=SOURCE,
            expected_registry_binding_sha256=REGISTRY,
            expected_effect_registry_binding_sha256=EFFECT_REGISTRY,
        )


def test_cli_builds_private_nonreplaceable_effect_maturity_artifact(
    maturity_inputs: _Inputs,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = tmp_path / "production-baseline.json"
    bundle = tmp_path / "canary-promotion-bundle.json"
    budget = tmp_path / "canary-latency-budget.json"
    output = tmp_path / "effect-maturity.json"
    baseline.write_bytes(maturity_inputs.mature_baseline_raw)
    bundle.write_bytes(maturity_inputs.bundle_raw)
    budget.write_bytes(maturity_inputs.budget_raw)
    expected_effect_registry = expected_effect_capability_snapshot().digest_hex()
    arguments = [
        "effect-maturity",
        "--production-baseline",
        str(baseline),
        "--production-baseline-sha256",
        _sha256(maturity_inputs.mature_baseline_raw),
        "--canary-promotion-bundle",
        str(bundle),
        "--canary-promotion-bundle-sha256",
        _sha256(maturity_inputs.bundle_raw),
        "--canary-latency-budget",
        str(budget),
        "--canary-latency-budget-sha256",
        _sha256(maturity_inputs.budget_raw),
        "--source-revision-sha256",
        SOURCE,
        "--registry-binding-sha256",
        REGISTRY,
        "--effect-registry-binding-sha256",
        expected_effect_registry,
        "--output",
        str(output),
    ]

    assert maturity_cli.main(arguments) == 0
    receipt = json.loads(capsys.readouterr().out)
    raw = output.read_bytes()
    witness = load_accepted_read_only_maturity_witness(
        raw,
        expected_file_sha256=_sha256(raw),
        expected_source_revision_sha256=SOURCE,
        expected_registry_binding_sha256=REGISTRY,
        expected_effect_registry_binding_sha256=expected_effect_registry,
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert receipt == {
        "schema": "friday.semantic-supervisor-effect-maturity-artifact-receipt.v1",
        "output_file_sha256": _sha256(raw),
        "maturity_facts_sha256": witness.maturity_facts_sha256,
        "source_revision_sha256": SOURCE,
        "registry_binding_sha256": REGISTRY,
        "effect_registry_binding_sha256": expected_effect_registry,
        "body_free": True,
        "runtime_authority_granted": False,
        "activation_performed": False,
        "write_effect_authorized": False,
    }
    with pytest.raises(SystemExit):
        maturity_cli.main(arguments)
    assert output.read_bytes() == raw
