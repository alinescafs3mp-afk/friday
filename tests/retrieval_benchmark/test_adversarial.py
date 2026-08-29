from __future__ import annotations

import json
from dataclasses import replace

import pytest

from friday.retrieval.archive_search_authority import canonical_archive_search_targets
from friday.retrieval.contracts import CoverageState, TemporalRole
from friday.retrieval_benchmark._canonical import parse_canonical_json
from friday.retrieval_benchmark.contracts import (
    MetricValueV1,
    RecallCandidateV1,
    RecallCaseV1,
    RecallContractError,
    RecallCoverageAggregateV1,
    RecallEvidenceSourceV1,
    RecallObservationV1,
    RecallOutcomeV1,
    case_manifest_sha256,
)
from friday.retrieval_benchmark.harness import cases_jsonl
from friday.retrieval_benchmark.metrics import score_recall, score_recall_case_results
from friday.retrieval_benchmark.synthetic import synthetic_cases
from tests.retrieval_benchmark.conftest import candidate_for, observation_for


def test_no_hit_and_alternatives_are_mutually_exclusive(recall_case: RecallCaseV1) -> None:
    with pytest.raises(RecallContractError):
        replace(recall_case, expected_no_hit=True)
    with pytest.raises(RecallContractError):
        replace(recall_case, alternatives=())


def test_candidate_rank_and_identity_bounds(recall_case: RecallCaseV1) -> None:
    candidate = candidate_for(recall_case)
    with pytest.raises(RecallContractError):
        replace(candidate, rank=101)
    with pytest.raises(RecallContractError):
        replace(candidate, source_identity="a" * 65)


def test_duplicate_temporal_roles_are_rejected(recall_case: RecallCaseV1) -> None:
    candidate = candidate_for(recall_case)
    with pytest.raises(RecallContractError):
        replace(
            candidate,
            temporal_roles=(TemporalRole.RECEIVED_AT, TemporalRole.RECEIVED_AT),
        )


def test_observation_create_rejects_missing_canonical_coverage_target(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(recall_case, complete=False)
    assert len(observation.coverage) == len(canonical_archive_search_targets(recall_case.request))
    with pytest.raises(RecallContractError):
        RecallObservationV1.create(
            case=recall_case,
            release_sha256=observation.release_sha256,
            candidates=(),
            coverage=observation.coverage[:-1],
        )


def test_scoring_rejects_release_forgery_across_manifest(recall_case: RecallCaseV1) -> None:
    second_case = replace(
        recall_case,
        case_id="case.release-forgery",
        privacy_key_hex="e" * 64,
    )
    first = observation_for(recall_case, complete=False, release_sha256="a" * 64)
    second = observation_for(second_case, complete=False, release_sha256="b" * 64)
    with pytest.raises(RecallContractError):
        score_recall((recall_case, second_case), (first, second))


def test_scoring_rejects_evidence_source_forgery(recall_case: RecallCaseV1) -> None:
    observation = observation_for(recall_case, complete=False)
    forged_case = replace(recall_case, evidence_source=RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL)
    with pytest.raises(RecallContractError):
        score_recall((forged_case,), (observation,))


def test_public_factory_cannot_mint_synthetic_shipped_evidence(recall_case: RecallCaseV1) -> None:
    owner_observation = observation_for(recall_case, complete=False)
    synthetic_case = replace(
        recall_case,
        evidence_source=RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL,
    )
    with pytest.raises(RecallContractError):
        RecallObservationV1.create(
            case=synthetic_case,
            release_sha256="f" * 64,
            candidates=(),
            coverage=owner_observation.coverage,
        )


def test_shipped_factory_requires_bound_phase2_attestation_and_pages(
    recall_case: RecallCaseV1,
) -> None:
    with pytest.raises(RecallContractError):
        RecallObservationV1.from_archive_attestation(
            case=recall_case,
            release_sha256="f" * 64,
            attestation=(),  # type: ignore[arg-type]
            prepared_searches=(),
        )


def test_candidate_cannot_claim_temporal_roles_outside_request(recall_case: RecallCaseV1) -> None:
    candidate = candidate_for(
        recall_case,
        temporal_roles=tuple(TemporalRole),
    )
    with pytest.raises(RecallContractError):
        observation_for(recall_case, candidates=(candidate,), complete=True)


def test_semantically_valid_noncanonical_observation_order_is_rejected(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    payload = observation.to_payload()
    noncanonical = json.dumps(payload, sort_keys=False, separators=(",", ":"))
    assert noncanonical != observation.to_json()
    with pytest.raises(RecallContractError):
        RecallObservationV1.parse(noncanonical)


def test_manifest_rejects_duplicate_case_even_if_object_is_identical(
    recall_case: RecallCaseV1,
) -> None:
    with pytest.raises(RecallContractError):
        case_manifest_sha256((recall_case, recall_case))


def test_case_writer_rejects_reused_privacy_key(recall_case: RecallCaseV1) -> None:
    second = replace(recall_case, case_id="case.reused-privacy-key")
    with pytest.raises(RecallContractError):
        cases_jsonl((recall_case, second))


def test_serialized_candidate_passage_collision_is_rejected(recall_case: RecallCaseV1) -> None:
    candidate = candidate_for(recall_case)
    payload = candidate.to_payload()
    payload["passage_window_identities"] = [
        candidate.passage_window_identities[0],
        candidate.passage_window_identities[0],
    ]
    with pytest.raises(RecallContractError):
        RecallCandidateV1.from_payload(payload)


def test_coverage_bool_cannot_impersonate_count(recall_case: RecallCaseV1) -> None:
    observation = observation_for(recall_case, complete=False)
    payload = observation.to_payload()
    coverage = payload["coverage"]
    assert isinstance(coverage, list)
    assert isinstance(coverage[0], dict)
    coverage[0]["examined"] = False
    payload_without_digest = dict(payload)
    payload_without_digest["observation_sha256"] = observation.observation_sha256
    with pytest.raises(RecallContractError):
        RecallObservationV1.from_payload(payload_without_digest)


def test_complete_state_cannot_be_combined_with_uncertainty(recall_case: RecallCaseV1) -> None:
    observation = observation_for(recall_case, complete=True)
    coverage = observation.coverage[0]
    with pytest.raises(RecallContractError):
        replace(
            coverage,
            states=(CoverageState.COMPLETE, CoverageState.STALE),
        )


def test_per_case_rank_and_observation_facts_cannot_drift_from_metrics(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    result = score_recall_case_results((recall_case,), (observation,))[0]
    with pytest.raises(RecallContractError):
        replace(result, first_relevant_rank=10)
    with pytest.raises(RecallContractError):
        replace(
            result,
            candidate_count=0,
            absence_decision=observation_for(recall_case, complete=True).absence_decision,
        )


def test_per_case_recall_and_date_denominators_are_closed(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    result = score_recall_case_results((recall_case,), (observation,))[0]
    metrics = dict(result.metrics)

    mismatched_denominator = tuple(
        (
            name,
            MetricValueV1.ratio(1, 2) if name == "candidate_recall_at_100" else metric,
        )
        for name, metric in result.metrics
    )
    with pytest.raises(RecallContractError):
        replace(result, metrics=mismatched_denominator)

    too_many_qrels = tuple(
        (name, MetricValueV1.ratio(2, 2) if name.startswith("candidate_recall") else metric)
        for name, metric in result.metrics
    )
    with pytest.raises(RecallContractError):
        replace(result, metrics=too_many_qrels)

    invented_date_denominator = tuple(
        (name, MetricValueV1.ratio(0, 2) if name == "date_role_accuracy" else metrics[name])
        for name, _metric in result.metrics
    )
    with pytest.raises(RecallContractError):
        replace(result, metrics=invented_date_denominator)


def test_per_case_ndcg_is_bound_to_exact_rank_and_grade(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case, rank=10),),
        complete=True,
    )
    result = score_recall_case_results((recall_case,), (observation,))[0]
    assert dict(result.metrics)["ndcg_at_10"].value_ppm == 289_065
    forged = tuple(
        (name, MetricValueV1.ratio(1_000_000, 1_000_000) if name == "ndcg_at_10" else metric)
        for name, metric in result.metrics
    )
    with pytest.raises(RecallContractError):
        replace(result, metrics=forged)


def test_per_case_coverage_scores_reject_bool_and_nondeterministic_order(
    recall_case: RecallCaseV1,
) -> None:
    result = score_recall_case_results(
        (recall_case,),
        (observation_for(recall_case, complete=True),),
    )[0]
    with pytest.raises(RecallContractError):
        replace(
            result,
            coverage_target_scores=(
                (
                    "catalog_coverage",
                    ((result.coverage_target_scores[0][1][0][0], False),),
                ),  # type: ignore[arg-type]
                *result.coverage_target_scores[1:],
            ),
        )
    with pytest.raises(RecallContractError):
        replace(
            result,
            coverage_target_scores=(
                (
                    "catalog_coverage",
                    (
                        (result.coverage_target_scores[0][1][0][0], 1_000_000),
                        (result.coverage_target_scores[0][1][0][0], 0),
                    ),
                ),
                *result.coverage_target_scores[1:],
            ),
        )


def test_malformed_nested_case_result_values_fail_as_contract_errors(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(recall_case, complete=True)
    result = score_recall_case_results((recall_case,), (observation,))[0]
    with pytest.raises(RecallContractError):
        replace(
            result,
            coverage_target_scores=(
                ("catalog_coverage",),  # type: ignore[arg-type]
                *result.coverage_target_scores[1:],
            ),
        )
    with pytest.raises(RecallContractError):
        replace(
            result,
            metrics=(
                ("candidate_recall_at_50",),  # type: ignore[arg-type]
                *result.metrics[1:],
            ),
        )


def test_malformed_report_case_binding_fails_as_contract_error(
    recall_case: RecallCaseV1,
) -> None:
    report = score_recall(
        (recall_case,),
        (observation_for(recall_case, complete=True),),
    )
    with pytest.raises(RecallContractError):
        replace(report, cases=(object(),))  # type: ignore[arg-type]


def test_malformed_coverage_aggregate_facts_fail_as_contract_errors(
    recall_case: RecallCaseV1,
) -> None:
    with pytest.raises(RecallContractError):
        RecallCoverageAggregateV1(True, 0, 0)  # type: ignore[arg-type]
    report = score_recall(
        (recall_case,),
        (observation_for(recall_case, complete=False),),
    )
    with pytest.raises(RecallContractError):
        replace(
            report,
            coverage_facts=(
                ("catalog_coverage",),  # type: ignore[arg-type]
                *report.coverage_facts[1:],
            ),
        )
    malformed = object.__new__(RecallCoverageAggregateV1)
    with pytest.raises(RecallContractError, match="malformed"):
        replace(
            report,
            coverage_facts=((report.coverage_facts[0][0], malformed), *report.coverage_facts[1:]),
        )


def test_negative_case_cannot_forge_hit_or_date_metric() -> None:
    base = synthetic_cases()[0]
    owner_case = replace(
        base,
        evidence_source=RecallEvidenceSourceV1.OWNER_PRIVATE_JSONL,
        alternatives=(),
        expected_no_hit=True,
    )
    result = score_recall_case_results(
        (owner_case,),
        (observation_for(owner_case, complete=False),),
    )[0]
    with pytest.raises(RecallContractError):
        replace(result, outcome=RecallOutcomeV1.HIT, first_relevant_rank=1)
    forged_metrics = tuple(
        (name, MetricValueV1.ratio(0, 1) if name == "date_role_accuracy" else metric)
        for name, metric in result.metrics
    )
    with pytest.raises(RecallContractError):
        replace(result, metrics=forged_metrics)


@pytest.mark.parametrize("depth", (40, 2_000))
def test_deep_json_is_closed_as_contract_error_instead_of_recursion_failure(
    depth: int,
) -> None:
    deeply_nested = "[" * depth + "0" + "]" * depth
    with pytest.raises(RecallContractError):
        parse_canonical_json(deeply_nested, label="adversarial nesting")
