from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday import semantic_supervisor_policy
from friday.orchestration.supervisor_assist_activation import (
    parse_assist_promotion_live_evidence,
)
from friday.orchestration.supervisor_assist_promotion import (
    SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
    AssistPromotionEvidenceAuthority,
    AssistPromotionOutcomeEvidence,
    AssistPromotionQualityBasis,
    AssistPromotionReadinessEvidence,
)
from friday.orchestration.supervisor_contracts import (
    SupervisorMode,
    canonical_sha256,
)
from friday.orchestration.supervisor_production_baseline import (
    SUPERVISOR_PRODUCT_WINDOW_SCHEMA,
    SUPERVISOR_PRODUCTION_BASELINE_KIND,
    SUPERVISOR_PRODUCTION_BASELINE_SCHEMA,
)
from friday.orchestration.supervisor_promotion_evidence_producer import (
    SupervisorPromotionEvidenceProducerError,
    SupervisorPromotionOperatorAttestation,
    build_supervisor_assist_promotion_evidence,
    build_supervisor_canary_promotion_evidence,
    build_supervisor_latency_budget_document,
    build_supervisor_promotion_bundle_payload,
    canonical_json_file_bytes,
    load_accepted_supervisor_production_baseline,
    load_accepted_supervisor_promotion_bundle,
    load_canonical_supervisor_latency_budget,
)
from friday.orchestration.supervisor_representative_window_attestation import (
    REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA,
    REPRESENTATIVE_WINDOW_AUTHORITY,
    REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA,
    representative_window_sha256,
)
from tools import build_semantic_supervisor_promotion_evidence as cli
from tools import immutable_release_operator as operator

SOURCE = "b" * 64
REGISTRY = "c" * 64
PRECURSOR = "d" * 64
FAILURE_DIGEST = "e" * 64
FAILURE_CLASS = "capability:source_unavailable"


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


def _report() -> dict[str, Any]:
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
        observations=0,
        complete=0,
        failure_counts={},
        latency_total=0,
        latency_max=0,
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
            "promoted_product_events": 20,
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
                    "promotion_evidence_sha256": PRECURSOR,
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
                    "observation_count": 0,
                    "joined_trace_count": 0,
                    "promotion_evidence_count": 0,
                    "promotion_evidence_sha256": None,
                    "promoted": canary_metrics,
                    "call_rate_observation_count": 0,
                    "supervisor_invocation_count": 0,
                    "unnecessary_supervisor_invocation_count": 0,
                    "user_visible_observation_count": 0,
                    "user_visible_regression_count": 0,
                    "product_window_sha256": "6" * 64,
                },
            },
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def _baseline_raw(report: dict[str, Any] | None = None) -> bytes:
    return canonical_json_file_bytes(report or _report())


def _resign(report: dict[str, Any]) -> bytes:
    report.pop("report_sha256", None)
    report["report_sha256"] = canonical_sha256(report)
    return _baseline_raw(report)


def _accepted_budget(mode: SupervisorMode) -> tuple[bytes, Any]:
    document = build_supervisor_latency_budget_document(
        target_mode=mode,
        source_revision_sha256=SOURCE,
        maximum_user_visible_latency_ms=2_500,
    )
    raw = canonical_json_file_bytes(document.payload())
    return raw, load_canonical_supervisor_latency_budget(
        raw,
        expected_file_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _accepted_baseline(report: dict[str, Any] | None = None) -> Any:
    raw = _baseline_raw(report)
    return load_accepted_supervisor_production_baseline(
        raw,
        expected_file_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _attestation(
    mode: SupervisorMode,
    *,
    baseline: Any,
    budget: Any,
    basis: AssistPromotionQualityBasis | None = None,
    precursor: str | None = None,
    **changes: object,
) -> SupervisorPromotionOperatorAttestation:
    values: dict[str, object] = {
        "target_mode": mode,
        "baseline_file_sha256": baseline.file_sha256,
        "baseline_report_sha256": baseline.report_sha256,
        "latency_budget_file_sha256": budget.document_sha256,
        "source_revision_sha256": SOURCE,
        "registry_binding_sha256": REGISTRY,
        "representative_window_attested": True,
        "primary_fallback_proven": True,
        "laptop_unavailable_fallback_proven": True,
        "final_authority_recheck_proven": True,
        "primary_publication_owner_proven": True,
        "zero_hidden_owners_attested": True,
        "zero_duplicate_capabilities_attested": True,
        "zero_duplicate_effects_attested": True,
        "zero_duplicate_publications_attested": True,
        "zero_false_completion_regressions_attested": True,
        "precursor_assist_promotion_evidence_sha256": precursor,
        "quality_basis": basis,
    }
    values.update(changes)
    return SupervisorPromotionOperatorAttestation(**values)  # type: ignore[arg-type]


def _representative_window_issue(
    mode: SupervisorMode,
    *,
    baseline: Any,
    budget: Any,
    precursor: str | None = None,
) -> dict[str, Any]:
    lookup_token = "7" * 64
    observed_mode = SupervisorMode.SHADOW if mode is SupervisorMode.ASSIST else SupervisorMode.ASSIST
    representative_window = (
        baseline.shadow_readiness.readiness_witness_sha256
        if mode is SupervisorMode.ASSIST
        else baseline.assist_execution.product_window_sha256
    )
    joined = (
        baseline.shadow_readiness.joined_trace_count
        if mode is SupervisorMode.ASSIST
        else baseline.assist_execution.joined_trace_count
    )
    server: dict[str, Any] = {
        "schema": REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA,
        "attestation_id": "sswindow_" + "6" * 32,
        "authority": REPRESENTATIVE_WINDOW_AUTHORITY,
        "target_mode": mode.value,
        "observed_mode": observed_mode.value,
        "baseline_file_sha256": baseline.file_sha256,
        "baseline_report_sha256": baseline.report_sha256,
        "latency_budget_file_sha256": budget.document_sha256,
        "latency_budget_document_sha256": budget.document_sha256,
        "latency_budget_target_mode": mode.value,
        "latency_budget_source_revision_sha256": SOURCE,
        "maximum_user_visible_latency_ms": 2_500,
        "precursor_assist_promotion_evidence_sha256": precursor,
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
        "joined_trace_count": joined,
        "representative_window_sha256": representative_window,
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


def _write_representative_window_issue(
    path: Path,
    mode: SupervisorMode,
    *,
    baseline: Any,
    budget: Any,
    precursor: str | None = None,
) -> None:
    path.write_bytes(
        canonical_json_file_bytes(
            _representative_window_issue(
                mode,
                baseline=baseline,
                budget=budget,
                precursor=precursor,
            )
        )
    )
    path.chmod(0o600)


def _assist_bundle() -> tuple[bytes, bytes, Any]:
    baseline_raw = _baseline_raw()
    baseline = _accepted_baseline()
    budget_raw, budget = _accepted_budget(SupervisorMode.ASSIST)
    attestation = _attestation(
        SupervisorMode.ASSIST,
        baseline=baseline,
        budget=budget,
    )
    evidence = build_supervisor_assist_promotion_evidence(
        evidence_id="producer_bundle_assist_window",
        baseline=baseline,
        budget=budget,
        attestation=attestation,
        documented_failure_class_id=FAILURE_CLASS,
        documented_failure_class_sha256=FAILURE_DIGEST,
    )
    bundle_raw = canonical_json_file_bytes(
        build_supervisor_promotion_bundle_payload(
            baseline_raw=baseline_raw,
            budget=budget,
            attestation=attestation,
            representative_window_issue=_representative_window_issue(
                SupervisorMode.ASSIST,
                baseline=baseline,
                budget=budget,
            ),
            evidence=evidence,
        )
    )
    return bundle_raw, budget_raw, evidence


def test_assist_producer_uses_real_identities_and_is_activation_parseable() -> None:
    baseline = _accepted_baseline()
    budget_raw, budget = _accepted_budget(SupervisorMode.ASSIST)
    attestation = _attestation(SupervisorMode.ASSIST, baseline=baseline, budget=budget)

    evidence = build_supervisor_assist_promotion_evidence(
        evidence_id="production_assist_window_1",
        baseline=baseline,
        budget=budget,
        attestation=attestation,
        documented_failure_class_id=FAILURE_CLASS,
        documented_failure_class_sha256=FAILURE_DIGEST,
    )

    assert evidence.authority is AssistPromotionEvidenceAuthority.PRODUCTION_JOINED
    assert evidence.observed_mode is SupervisorMode.SHADOW
    assert evidence.promotion_policy_sha256 == SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256
    assert evidence.observed_policy_sha256 == semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_SHA256
    assert evidence.target_policy_sha256 == (
        semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256
    )
    assert evidence.baseline_file_sha256 == baseline.file_sha256
    assert evidence.baseline_report_sha256 == baseline.report_sha256
    assert evidence.operator_attestation_sha256 == attestation.canonical_sha256()
    assert evidence.precursor_assist_promotion_evidence_sha256 is None
    assert isinstance(evidence.product_evidence, AssistPromotionReadinessEvidence)
    assert evidence.product_evidence.baseline_failure_class_count == 5
    assert evidence.product_evidence.latency_budget_sha256 == hashlib.sha256(budget_raw).hexdigest()
    raw = canonical_json_file_bytes(evidence.payload())
    loaded = parse_assist_promotion_live_evidence(raw, hashlib.sha256(raw).hexdigest())
    assert loaded.evidence == evidence


def test_canary_producer_requires_precursor_and_explicit_quality_basis() -> None:
    baseline = _accepted_baseline()
    _raw, budget = _accepted_budget(SupervisorMode.CANARY)
    attestation = _attestation(
        SupervisorMode.CANARY,
        baseline=baseline,
        budget=budget,
        basis=AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT,
        precursor=PRECURSOR,
    )

    evidence = build_supervisor_canary_promotion_evidence(
        evidence_id="production_canary_window_1",
        baseline=baseline,
        budget=budget,
        attestation=attestation,
    )

    assert evidence.observed_mode is SupervisorMode.ASSIST
    assert evidence.baseline_file_sha256 == baseline.file_sha256
    assert evidence.baseline_report_sha256 == baseline.report_sha256
    assert evidence.operator_attestation_sha256 == attestation.canonical_sha256()
    assert evidence.precursor_assist_promotion_evidence_sha256 == PRECURSOR
    assert isinstance(evidence.product_evidence, AssistPromotionOutcomeEvidence)
    assert evidence.product_evidence.promoted_complete_count == 12
    assert evidence.product_evidence.quality_basis is (
        AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT
    )

    wrong = _attestation(
        SupervisorMode.CANARY,
        baseline=baseline,
        budget=budget,
        basis=AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT,
        precursor="f" * 64,
    )
    with pytest.raises(SupervisorPromotionEvidenceProducerError, match="precursor"):
        build_supervisor_canary_promotion_evidence(
            evidence_id="production_canary_window_2",
            baseline=baseline,
            budget=budget,
            attestation=wrong,
        )


def test_canary_failure_removal_is_measured_not_asserted() -> None:
    baseline = _accepted_baseline()
    _raw, budget = _accepted_budget(SupervisorMode.CANARY)
    attestation = _attestation(
        SupervisorMode.CANARY,
        baseline=baseline,
        budget=budget,
        basis=AssistPromotionQualityBasis.DOCUMENTED_FAILURE_CLASS_REMOVAL,
        precursor=PRECURSOR,
    )

    evidence = build_supervisor_canary_promotion_evidence(
        evidence_id="production_canary_failure_removal",
        baseline=baseline,
        budget=budget,
        attestation=attestation,
        documented_failure_class_id=FAILURE_CLASS,
        documented_failure_class_sha256=FAILURE_DIGEST,
    )

    product = evidence.product_evidence
    assert isinstance(product, AssistPromotionOutcomeEvidence)
    assert product.baseline_failure_class_count == 5
    assert product.promoted_failure_class_count == 0


@pytest.mark.parametrize(
    "mutation",
    (
        "candidate_authority",
        "not_production_joined",
        "duplicate_rows",
        "wrong_stage",
        "bad_self_digest",
        "noncanonical_file",
    ),
)
def test_baseline_loader_rejects_untrusted_or_ambiguous_inputs(mutation: str) -> None:
    report = _report()
    if mutation == "candidate_authority":
        report["evidence"]["promotion_authority"] = True
        raw = _resign(report)
    elif mutation == "not_production_joined":
        report["product_windows"]["shadow_readiness"]["production_joined"] = False
        raw = _resign(report)
    elif mutation == "duplicate_rows":
        report["sample"]["duplicate_promoted_product_events"] = 1
        raw = _resign(report)
    elif mutation == "wrong_stage":
        report["product_windows"]["promoted_execution"]["assist"]["promoted"]["stage"] = "canary"
        raw = _resign(report)
    elif mutation == "bad_self_digest":
        report["report_sha256"] = "0" * 64
        raw = _baseline_raw(report)
    else:
        raw = json.dumps(report, indent=2, sort_keys=True).encode("utf-8") + b"\n"

    with pytest.raises(SupervisorPromotionEvidenceProducerError):
        load_accepted_supervisor_production_baseline(
            raw,
            expected_file_sha256=hashlib.sha256(raw).hexdigest(),
        )


def test_operator_attestation_must_explicitly_affirm_every_invariant() -> None:
    baseline = _accepted_baseline()
    _raw, budget = _accepted_budget(SupervisorMode.ASSIST)
    attestation = _attestation(
        SupervisorMode.ASSIST,
        baseline=baseline,
        budget=budget,
        zero_duplicate_effects_attested=False,
    )

    with pytest.raises(SupervisorPromotionEvidenceProducerError, match="explicitly affirm"):
        build_supervisor_assist_promotion_evidence(
            evidence_id="unattested_window",
            baseline=baseline,
            budget=budget,
            attestation=attestation,
            documented_failure_class_id=FAILURE_CLASS,
            documented_failure_class_sha256=FAILURE_DIGEST,
        )


def test_accepted_input_tokens_cannot_be_rebound_with_dataclass_replace() -> None:
    baseline = _accepted_baseline()
    _raw, budget = _accepted_budget(SupervisorMode.ASSIST)

    with pytest.raises(SupervisorPromotionEvidenceProducerError, match="not accepted"):
        replace(baseline, report_sha256="f" * 64)
    with pytest.raises(SupervisorPromotionEvidenceProducerError, match="not accepted"):
        replace(budget, document_sha256="f" * 64)


@pytest.mark.parametrize("mutation", ("latency", "regression", "unnecessary_call"))
def test_assist_producer_refuses_product_windows_that_fail_the_exact_gate(mutation: str) -> None:
    report = _report()
    shadow = report["product_windows"]["shadow_readiness"]
    if mutation == "latency":
        shadow["baseline"]["latency_max_ms"] = 3_000
        shadow["baseline"]["latency_total_ms"] = 60_000
    elif mutation == "regression":
        shadow["user_visible_regression_count"] = 1
    else:
        shadow["unnecessary_supervisor_invocation_count"] = 1
    raw = _resign(report)
    baseline = load_accepted_supervisor_production_baseline(
        raw,
        expected_file_sha256=hashlib.sha256(raw).hexdigest(),
    )
    _budget_raw, budget = _accepted_budget(SupervisorMode.ASSIST)

    with pytest.raises(SupervisorPromotionEvidenceProducerError, match="promotion budget"):
        build_supervisor_assist_promotion_evidence(
            evidence_id="failed_product_gate",
            baseline=baseline,
            budget=budget,
            attestation=_attestation(SupervisorMode.ASSIST, baseline=baseline, budget=budget),
            documented_failure_class_id=FAILURE_CLASS,
            documented_failure_class_sha256=FAILURE_DIGEST,
        )


def test_canary_completion_basis_requires_measured_rate_improvement() -> None:
    report = _report()
    promoted = report["product_windows"]["promoted_execution"]["assist"]["promoted"]
    promoted["complete_count"] = 8
    promoted["completion_counts"] = {"complete": 8, "failed": 12}
    raw = _resign(report)
    baseline = load_accepted_supervisor_production_baseline(
        raw,
        expected_file_sha256=hashlib.sha256(raw).hexdigest(),
    )
    _budget_raw, budget = _accepted_budget(SupervisorMode.CANARY)
    attestation = _attestation(
        SupervisorMode.CANARY,
        baseline=baseline,
        budget=budget,
        basis=AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT,
        precursor=PRECURSOR,
    )

    with pytest.raises(SupervisorPromotionEvidenceProducerError, match="improvement"):
        build_supervisor_canary_promotion_evidence(
            evidence_id="no_measured_improvement",
            baseline=baseline,
            budget=budget,
            attestation=attestation,
        )


def _attestation_flags() -> list[str]:
    return [
        "--attest-primary-fallback",
        "--attest-laptop-unavailable-fallback",
        "--attest-final-authority-recheck",
        "--attest-primary-publication-owner",
        "--attest-zero-hidden-owners",
        "--attest-zero-duplicate-capabilities",
        "--attest-zero-duplicate-effects",
        "--attest-zero-duplicate-publications",
        "--attest-zero-false-completion-regressions",
    ]


def _evidence_cli_args(
    *,
    baseline: Path,
    baseline_sha: str,
    budget: Path,
    budget_sha: str,
    representative_window_issue: Path,
    output: Path,
) -> list[str]:
    return [
        "promotion-evidence",
        "--target-mode",
        "assist",
        "--baseline",
        str(baseline),
        "--baseline-sha256",
        baseline_sha,
        "--latency-budget",
        str(budget),
        "--latency-budget-sha256",
        budget_sha,
        "--representative-window-issue-response",
        str(representative_window_issue),
        "--attested-source-revision-sha256",
        SOURCE,
        "--attested-registry-binding-sha256",
        REGISTRY,
        "--evidence-id",
        "cli_production_assist_window",
        "--documented-failure-class-id",
        FAILURE_CLASS,
        "--documented-failure-class-sha256",
        FAILURE_DIGEST,
        "--output",
        str(output),
    ]


def test_cli_writes_private_nonreplaceable_artifacts_and_body_free_receipts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline_raw = _baseline_raw()
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_bytes(baseline_raw)
    budget_path = tmp_path / "budget.json"
    assert (
        cli.main(
            [
                "latency-budget",
                "--target-mode",
                "assist",
                "--source-revision-sha256",
                SOURCE,
                "--maximum-user-visible-latency-ms",
                "2500",
                "--output",
                str(budget_path),
            ]
        )
        == 0
    )
    budget_receipt = json.loads(capsys.readouterr().out)
    budget_raw = budget_path.read_bytes()
    assert stat.S_IMODE(budget_path.stat().st_mode) == 0o600
    assert budget_receipt["output_file_sha256"] == hashlib.sha256(budget_raw).hexdigest()
    assert budget_receipt["promotion_authority_granted"] is False
    assert budget_receipt["activation_performed"] is False

    evidence_path = tmp_path / "evidence.json"
    baseline = _accepted_baseline()
    _budget_raw, accepted_budget = _accepted_budget(SupervisorMode.ASSIST)
    issue_path = tmp_path / "representative-window-issue.json"
    _write_representative_window_issue(
        issue_path,
        SupervisorMode.ASSIST,
        baseline=baseline,
        budget=accepted_budget,
    )
    arguments = _evidence_cli_args(
        baseline=baseline_path,
        baseline_sha=hashlib.sha256(baseline_raw).hexdigest(),
        budget=budget_path,
        budget_sha=hashlib.sha256(budget_raw).hexdigest(),
        representative_window_issue=issue_path,
        output=evidence_path,
    )
    assert cli.main([*arguments, *_attestation_flags()]) == 0
    receipt_text = capsys.readouterr().out
    receipt = json.loads(receipt_text)
    evidence_raw = evidence_path.read_bytes()
    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    assert receipt["output_file_sha256"] == hashlib.sha256(evidence_raw).hexdigest()
    assert receipt["operator_attestation_sha256"]
    assert receipt["body_free"] is True
    assert str(tmp_path) not in receipt_text
    assert "cli_production_assist_window" not in receipt_text

    original = evidence_raw
    with pytest.raises(SystemExit):
        cli.main([*arguments, *_attestation_flags()])
    assert evidence_path.read_bytes() == original
    assert not tuple(tmp_path.glob(".*.tmp.*"))

    victim = tmp_path / "victim"
    victim.write_text("unchanged", encoding="utf-8")
    symlink = tmp_path / "linked-budget.json"
    symlink.symlink_to(victim)
    with pytest.raises(SystemExit):
        cli.main(
            [
                "latency-budget",
                "--target-mode",
                "assist",
                "--source-revision-sha256",
                SOURCE,
                "--maximum-user-visible-latency-ms",
                "2500",
                "--output",
                str(symlink),
            ]
        )
    assert victim.read_text(encoding="utf-8") == "unchanged"


def test_cli_requires_each_operator_attestation_before_reading_or_writing(tmp_path: Path) -> None:
    output = tmp_path / "must-not-exist.json"
    arguments = _evidence_cli_args(
        baseline=tmp_path / "missing-baseline",
        baseline_sha="1" * 64,
        budget=tmp_path / "missing-budget",
        budget_sha="2" * 64,
        representative_window_issue=tmp_path / "missing-window-issue",
        output=output,
    )
    with pytest.raises(SystemExit) as caught:
        cli.main([*arguments, *_attestation_flags()[:-1]])
    assert caught.value.code == 2
    assert not output.exists()


def test_cli_builds_precursor_bound_canary_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline_raw = _baseline_raw()
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_bytes(baseline_raw)
    budget_raw, budget = _accepted_budget(SupervisorMode.CANARY)
    budget_path = tmp_path / "canary-budget.json"
    budget_path.write_bytes(budget_raw)
    issue_path = tmp_path / "canary-representative-window-issue.json"
    _write_representative_window_issue(
        issue_path,
        SupervisorMode.CANARY,
        baseline=_accepted_baseline(),
        budget=budget,
        precursor=PRECURSOR,
    )
    output = tmp_path / "canary-evidence.json"
    arguments = [
        "promotion-evidence",
        "--target-mode",
        "canary",
        "--baseline",
        str(baseline_path),
        "--baseline-sha256",
        hashlib.sha256(baseline_raw).hexdigest(),
        "--latency-budget",
        str(budget_path),
        "--latency-budget-sha256",
        hashlib.sha256(budget_raw).hexdigest(),
        "--representative-window-issue-response",
        str(issue_path),
        "--attested-source-revision-sha256",
        SOURCE,
        "--attested-registry-binding-sha256",
        REGISTRY,
        "--evidence-id",
        "cli_production_canary_window",
        "--quality-basis",
        AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT.value,
        "--precursor-assist-promotion-evidence-sha256",
        PRECURSOR,
        "--output",
        str(output),
        *_attestation_flags(),
    ]

    assert cli.main(arguments) == 0
    receipt = json.loads(capsys.readouterr().out)
    payload = json.loads(output.read_text(encoding="utf-8"))
    evidence = payload["promotion_evidence"]
    assert receipt["precursor_assist_promotion_evidence_sha256"] == PRECURSOR
    assert payload["schema"] == "friday.semantic-supervisor-promotion-bundle.v1"
    assert evidence["observed_mode"] == "assist"
    assert evidence["baseline_file_sha256"] == receipt["baseline_file_sha256"]
    assert evidence["baseline_report_sha256"] == receipt["baseline_report_sha256"]
    assert evidence["operator_attestation_sha256"] == receipt["operator_attestation_sha256"]
    assert evidence["precursor_assist_promotion_evidence_sha256"] == PRECURSOR
    assert evidence["product_evidence"]["quality_basis"] == "completion_rate_improvement"


def test_immutable_operator_accepts_exact_producer_output(tmp_path: Path) -> None:
    baseline_raw = _baseline_raw()
    baseline = _accepted_baseline()
    budget_raw, budget = _accepted_budget(SupervisorMode.ASSIST)
    attestation = _attestation(SupervisorMode.ASSIST, baseline=baseline, budget=budget)
    evidence = build_supervisor_assist_promotion_evidence(
        evidence_id="operator_integration_assist_window",
        baseline=baseline,
        budget=budget,
        attestation=attestation,
        documented_failure_class_id=FAILURE_CLASS,
        documented_failure_class_sha256=FAILURE_DIGEST,
    )
    evidence_raw = canonical_json_file_bytes(
        build_supervisor_promotion_bundle_payload(
            baseline_raw=baseline_raw,
            budget=budget,
            attestation=attestation,
            representative_window_issue=_representative_window_issue(
                SupervisorMode.ASSIST,
                baseline=baseline,
                budget=budget,
            ),
            evidence=evidence,
        )
    )
    budget_path = tmp_path / "budget.json"
    evidence_path = tmp_path / "evidence.json"
    budget_path.write_bytes(budget_raw)
    evidence_path.write_bytes(evidence_raw)
    budget_path.chmod(0o600)
    evidence_path.chmod(0o600)
    values = {
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_FILE": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_SHA256": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_MODE": "off",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS": "1",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS": "6",
        "FRIDAY_SEMANTIC_SUPERVISOR_MODE": "assist",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED": "1",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE": str(evidence_path),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256": hashlib.sha256(evidence_raw).hexdigest(),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE": str(budget_path),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256": hashlib.sha256(budget_raw).hexdigest(),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256": REGISTRY,
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256": SOURCE,
        "FRIDAY_SEMANTIC_SUPERVISOR_TASKS": "compare_current_file_with_current_web",
        "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC": "12",
    }

    operator._validate_semantic_supervisor_promoted_values(  # noqa: SLF001
        values,
        mode="assist",
        invalid_code="producer_integration_invalid",
    )


def test_bundle_loader_rebuilds_every_claim_and_rejects_standalone_or_tampered_members() -> None:
    bundle_raw, budget_raw, evidence = _assist_bundle()
    accepted = load_accepted_supervisor_promotion_bundle(
        bundle_raw,
        expected_file_sha256=hashlib.sha256(bundle_raw).hexdigest(),
        budget_raw=budget_raw,
        expected_budget_file_sha256=hashlib.sha256(budget_raw).hexdigest(),
    )
    assert accepted.evidence == evidence

    standalone = canonical_json_file_bytes(evidence.payload())
    with pytest.raises(SupervisorPromotionEvidenceProducerError):
        load_accepted_supervisor_promotion_bundle(
            standalone,
            expected_file_sha256=hashlib.sha256(standalone).hexdigest(),
            budget_raw=budget_raw,
            expected_budget_file_sha256=hashlib.sha256(budget_raw).hexdigest(),
        )

    original = json.loads(bundle_raw)
    variants: list[dict[str, Any]] = []
    missing_receipt = json.loads(json.dumps(original))
    missing_receipt.pop("producer_receipt")
    variants.append(missing_receipt)
    invented_metric = json.loads(json.dumps(original))
    invented_metric["promotion_evidence"]["product_evidence"]["latency_total_ms"] += 1
    variants.append(invented_metric)
    swapped_baseline = json.loads(json.dumps(original))
    swapped_baseline["baseline"]["product_windows"]["shadow_readiness"]["baseline"]["latency_total_ms"] += 1
    variants.append(swapped_baseline)
    forged_receipt = json.loads(json.dumps(original))
    forged_receipt["producer_receipt"]["promotion_evidence_file_sha256"] = "f" * 64
    variants.append(forged_receipt)

    for variant in variants:
        tampered = canonical_json_file_bytes(variant)
        with pytest.raises(SupervisorPromotionEvidenceProducerError):
            load_accepted_supervisor_promotion_bundle(
                tampered,
                expected_file_sha256=hashlib.sha256(tampered).hexdigest(),
                budget_raw=budget_raw,
                expected_budget_file_sha256=hashlib.sha256(budget_raw).hexdigest(),
            )

    canary_budget_raw, _canary_budget = _accepted_budget(SupervisorMode.CANARY)
    with pytest.raises(SupervisorPromotionEvidenceProducerError):
        load_accepted_supervisor_promotion_bundle(
            bundle_raw,
            expected_file_sha256=hashlib.sha256(bundle_raw).hexdigest(),
            budget_raw=canary_budget_raw,
            expected_budget_file_sha256=hashlib.sha256(canary_budget_raw).hexdigest(),
        )


def test_producer_canary_output_binds_exact_predecessor_for_operator(tmp_path: Path) -> None:
    assist_baseline = _accepted_baseline()
    _assist_budget_raw, assist_budget = _accepted_budget(SupervisorMode.ASSIST)
    assist_evidence = build_supervisor_assist_promotion_evidence(
        evidence_id="chain_assist_window",
        baseline=assist_baseline,
        budget=assist_budget,
        attestation=_attestation(
            SupervisorMode.ASSIST,
            baseline=assist_baseline,
            budget=assist_budget,
        ),
        documented_failure_class_id=FAILURE_CLASS,
        documented_failure_class_sha256=FAILURE_DIGEST,
    )
    precursor_sha256 = assist_evidence.canonical_sha256()

    canary_report = _report()
    canary_report["product_windows"]["promoted_execution"]["assist"]["promotion_evidence_sha256"] = (
        precursor_sha256
    )
    canary_baseline_raw = _resign(canary_report)
    canary_baseline = load_accepted_supervisor_production_baseline(
        canary_baseline_raw,
        expected_file_sha256=hashlib.sha256(canary_baseline_raw).hexdigest(),
    )
    canary_budget_raw, canary_budget = _accepted_budget(SupervisorMode.CANARY)
    canary_attestation = _attestation(
        SupervisorMode.CANARY,
        baseline=canary_baseline,
        budget=canary_budget,
        basis=AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT,
        precursor=precursor_sha256,
    )
    canary_evidence = build_supervisor_canary_promotion_evidence(
        evidence_id="chain_canary_window",
        baseline=canary_baseline,
        budget=canary_budget,
        attestation=canary_attestation,
    )
    canary_raw = canonical_json_file_bytes(
        build_supervisor_promotion_bundle_payload(
            baseline_raw=canary_baseline_raw,
            budget=canary_budget,
            attestation=canary_attestation,
            representative_window_issue=_representative_window_issue(
                SupervisorMode.CANARY,
                baseline=canary_baseline,
                budget=canary_budget,
                precursor=precursor_sha256,
            ),
            evidence=canary_evidence,
        )
    )
    canary_path = tmp_path / "canary-evidence.json"
    budget_path = tmp_path / "canary-budget.json"
    canary_path.write_bytes(canary_raw)
    budget_path.write_bytes(canary_budget_raw)
    canary_path.chmod(0o600)
    budget_path.chmod(0o600)
    values = {
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_FILE": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_EVIDENCE_SHA256": "",
        "FRIDAY_SEMANTIC_SUPERVISOR_EFFECT_MODE": "off",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS": "1",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS": "6",
        "FRIDAY_SEMANTIC_SUPERVISOR_MODE": "canary",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS": "d" * 64,
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED": "1",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE": str(canary_path),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256": hashlib.sha256(canary_raw).hexdigest(),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE": str(budget_path),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256": hashlib.sha256(
            canary_budget_raw
        ).hexdigest(),
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256": REGISTRY,
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256": SOURCE,
        "FRIDAY_SEMANTIC_SUPERVISOR_TASKS": "compare_current_file_with_current_web",
        "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC": "12",
    }

    assert (
        operator._validate_semantic_supervisor_promoted_values(  # noqa: SLF001
            values,
            mode="canary",
            invalid_code="producer_chain_invalid",
            expected_precursor_assist_evidence_sha256=precursor_sha256,
        )
        == canary_evidence.canonical_sha256()
    )
    with pytest.raises(operator.ReleaseFailure):
        operator._validate_semantic_supervisor_promoted_values(  # noqa: SLF001
            values,
            mode="canary",
            invalid_code="producer_chain_invalid",
            expected_precursor_assist_evidence_sha256="f" * 64,
        )


def test_budget_mode_and_source_are_not_inferred_from_evidence_target() -> None:
    baseline = _accepted_baseline()
    _raw, budget = _accepted_budget(SupervisorMode.ASSIST)
    with pytest.raises(SupervisorPromotionEvidenceProducerError, match="binding"):
        build_supervisor_assist_promotion_evidence(
            evidence_id="wrong_source",
            baseline=baseline,
            budget=budget,
            attestation=_attestation(
                SupervisorMode.ASSIST,
                baseline=baseline,
                budget=budget,
                source_revision_sha256="f" * 64,
            ),
            documented_failure_class_id=FAILURE_CLASS,
            documented_failure_class_sha256=FAILURE_DIGEST,
        )
