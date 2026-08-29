from __future__ import annotations

from dataclasses import replace

import pytest

from friday.retrieval.archive_search_contract import ArchiveSearchCorpus
from friday.retrieval.contracts import (
    AbsenceDecision,
    CoverageState,
    SearchCorpus,
    SearchLane,
    TemporalRole,
)
from friday.retrieval_benchmark._canonical import canonical_json, digest_payload
from friday.retrieval_benchmark.contracts import (
    MAX_CASES,
    MetricStatusV1,
    MetricValueV1,
    RecallAlternativeV1,
    RecallCaseV1,
    RecallContractError,
    RecallCoverageConfigurationV1,
    RecallCoveragePlanAggregateV1,
    RecallCoverageV1,
    RecallEvidenceSourceV1,
    RecallMetricAggregateV1,
    RecallNdcgAggregateBucketV1,
    RecallObservationV1,
    RecallReportV1,
    RecallTaxonomyV1,
    coverage_absence_oracle,
)
from friday.retrieval_benchmark.metrics import (
    compare_reports,
    score_recall,
    score_recall_case_results,
)
from friday.retrieval_benchmark.synthetic import SYNTHETIC_PRINCIPAL, SYNTHETIC_TENANT, synthetic_cases
from tests.retrieval_benchmark.conftest import candidate_for, observation_for


def _metric(report, name: str):  # type: ignore[no-untyped-def]
    return dict(report.metrics)[name]


def _parse_resealed_report(payload: dict[str, object]) -> RecallReportV1:
    unsigned = dict(payload)
    unsigned.pop("report_sha256")
    payload["report_sha256"] = digest_payload(
        b"friday/retrieval-recall-report/v1",
        unsigned,
    )
    return RecallReportV1.parse(canonical_json(payload))


def _full_document_coverage(
    *,
    absence_oracle_ready: bool = True,
) -> RecallCoverageConfigurationV1:
    return RecallCoverageConfigurationV1(
        taxonomy=RecallTaxonomyV1.APPROXIMATE_CONTENT,
        expected_corpus=ArchiveSearchCorpus.DOCUMENTS,
        absence_oracle_ready=absence_oracle_ready,
        target_counts=(1, 1, 1),
        unknown_counts=(0, 0, 0),
        score_sums_ppm=(1_000_000, 1_000_000, 1_000_000),
        expected_unknown_counts=(0, 0, 0),
        expected_score_sums_ppm=(1_000_000, 1_000_000, 1_000_000),
    )


def test_perfect_golden_vector_scores_one(recall_case: RecallCaseV1) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    report = score_recall((recall_case,), (observation,))
    for name in (
        "candidate_recall_at_50",
        "candidate_recall_at_100",
        "mrr_at_10",
        "ndcg_at_10",
    ):
        assert _metric(report, name).value_ppm == 1_000_000


def test_graded_alternatives_reward_the_better_order(recall_case: RecallCaseV1) -> None:
    original = recall_case.alternatives[0]
    second = RecallAlternativeV1(
        "a" * 64,
        ("b" * 64,),
        original.locator_kind,
        1,
    )
    case = replace(
        recall_case, alternatives=tuple(sorted((original, second), key=lambda item: item.source_identity))
    )
    candidates = (
        candidate_for(case, rank=1, source_identity=second.source_identity, passage_identity="b" * 64),
        candidate_for(
            case,
            rank=2,
            source_identity=original.source_identity,
            passage_identity=original.passage_window_identities[0],
        ),
    )
    observation = observation_for(case, candidates=candidates, complete=True)
    suboptimal = _metric(score_recall((case,), (observation,)), "ndcg_at_10").value_ppm
    ideal = observation_for(
        case,
        candidates=(
            candidate_for(
                case,
                rank=1,
                source_identity=original.source_identity,
                passage_identity=original.passage_window_identities[0],
            ),
            candidate_for(
                case,
                rank=2,
                source_identity=second.source_identity,
                passage_identity="b" * 64,
            ),
        ),
        complete=True,
    )
    assert suboptimal is not None and 0 < suboptimal < 1_000_000
    assert _metric(score_recall((case,), (ideal,)), "ndcg_at_10").value_ppm == 1_000_000


def test_equal_grade_tie_has_literal_deterministic_golden(recall_case: RecallCaseV1) -> None:
    original = recall_case.alternatives[0]
    tied = RecallAlternativeV1(
        "a" * 64,
        ("b" * 64,),
        original.locator_kind,
        original.relevance_grade,
    )
    case = replace(
        recall_case,
        alternatives=tuple(sorted((original, tied), key=lambda item: item.source_identity)),
    )

    def score(*, original_first: bool):  # type: ignore[no-untyped-def]
        ordered = (original, tied) if original_first else (tied, original)
        observation = observation_for(
            case,
            candidates=tuple(
                candidate_for(
                    case,
                    rank=rank,
                    source_identity=alternative.source_identity,
                    passage_identity=alternative.passage_window_identities[0],
                )
                for rank, alternative in enumerate(ordered, 1)
            ),
            complete=True,
        )
        report = score_recall((case,), (observation,))
        return {
            name: (_metric(report, name).numerator, _metric(report, name).denominator)
            for name in ("candidate_recall_at_100", "mrr_at_10", "ndcg_at_10")
        }

    literal_golden = {
        "candidate_recall_at_100": (2, 2),
        "mrr_at_10": (1_000_000, 1_000_000),
        "ndcg_at_10": (1_000_000, 1_000_000),
    }
    assert score(original_first=True) == literal_golden
    assert score(original_first=False) == literal_golden


def test_duplicate_candidate_cannot_double_credit(recall_case: RecallCaseV1) -> None:
    first = candidate_for(recall_case, rank=1)
    duplicate = candidate_for(recall_case, rank=2)
    observation = observation_for(
        recall_case,
        candidates=(first, duplicate),
        complete=True,
    )
    report = score_recall((recall_case,), (observation,))
    assert (
        _metric(report, "candidate_recall_at_50").numerator,
        _metric(report, "candidate_recall_at_100").numerator,
        _metric(report, "ndcg_at_10").value_ppm,
    ) == (1, 1, 1_000_000)


def test_rank_75_distinguishes_recall_50_from_recall_100(recall_case: RecallCaseV1) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case, rank=75),),
        complete=True,
    )
    report = score_recall((recall_case,), (observation,))
    assert _metric(report, "candidate_recall_at_50").value_ppm == 0
    assert _metric(report, "candidate_recall_at_100").value_ppm == 1_000_000


def test_empty_complete_search_exposes_false_absence(recall_case: RecallCaseV1) -> None:
    observation = observation_for(recall_case, complete=True)
    report = score_recall((recall_case,), (observation,))
    assert observation.absence_decision is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
    assert _metric(report, "false_absence_rate").value_ppm == 1_000_000
    payload = report.to_payload()
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    plans = payload["coverage_plan_facts"]
    assert isinstance(per_taxonomy, list) and isinstance(per_taxonomy[0], dict)
    assert isinstance(per_corpus, list) and isinstance(per_corpus[0], dict)
    assert isinstance(plans, list) and isinstance(plans[0], dict)
    for scope in (payload, per_taxonomy[0], per_corpus[0]):
        facts = scope["metric_facts"]
        metrics = scope["metrics"]
        assert isinstance(facts, dict) and isinstance(metrics, dict)
        buckets = facts["ndcg_buckets"]
        assert isinstance(buckets, list) and isinstance(buckets[0], dict)
        buckets[0]["absence_decision"] = AbsenceDecision.NOT_ESTABLISHED.value
        facts["false_absence_count"] = 0
        metrics["false_absence_rate"] = MetricValueV1.ratio(0, 1).to_payload()
    plans[0]["false_absence_count"] = 0
    with pytest.raises(RecallContractError, match="uncertain absence"):
        _parse_resealed_report(payload)


def test_empty_incomplete_search_is_not_not_found(recall_case: RecallCaseV1) -> None:
    observation = observation_for(recall_case, complete=False)
    report = score_recall((recall_case,), (observation,))
    assert observation.absence_decision is AbsenceDecision.NOT_ESTABLISHED
    assert _metric(report, "false_absence_rate").value_ppm == 0


@pytest.mark.parametrize(
    "state",
    (
        CoverageState.STALE,
        CoverageState.UNAVAILABLE,
        CoverageState.PERMISSION_FILTERED,
        CoverageState.BACKFILL_PENDING,
        CoverageState.EMBEDDING_INCOMPATIBLE,
        CoverageState.CAPPED,
    ),
)
def test_uncertain_coverage_states_never_confirm_absence(state: CoverageState) -> None:
    lane = SearchLane.DENSE if state is CoverageState.EMBEDDING_INCOMPATIBLE else SearchLane.LEXICAL
    states = tuple(sorted({CoverageState.PARTIAL, state}, key=lambda item: item.value))
    coverage = RecallCoverageV1(
        SearchCorpus.RAW_DOCUMENTS,
        lane,
        states,
        None,
        0,
        0,
        0,
        1 if state is CoverageState.CAPPED else None,
        state is CoverageState.CAPPED,
        True,
        True,
    )
    assert coverage_absence_oracle((coverage,), candidate_count=0) is AbsenceDecision.NOT_ESTABLISHED


@pytest.mark.parametrize("case_index", (2, 3))
def test_date_role_accuracy_uses_received_and_uploaded_roles(case_index: int) -> None:
    case = replace(
        synthetic_cases()[case_index],
        evidence_source=RecallEvidenceSourceV1.OWNER_PRIVATE_JSONL,
    )
    role = case.alternatives[0].temporal_role
    assert role is not None
    correct = observation_for(
        case,
        candidates=(candidate_for(case, temporal_roles=(role,)),),
        complete=True,
    )
    missing = observation_for(case, candidates=(candidate_for(case),), complete=True)
    assert _metric(score_recall((case,), (correct,)), "date_role_accuracy").value_ppm == 1_000_000
    assert _metric(score_recall((case,), (missing,)), "date_role_accuracy").value_ppm == 0

    confused_role = TemporalRole.UPLOADED_AT if role is TemporalRole.RECEIVED_AT else TemporalRole.RECEIVED_AT
    confused = candidate_for(case, temporal_roles=(confused_role,))
    with pytest.raises(RecallContractError):
        observation_for(case, candidates=(confused,), complete=True)


def test_unsupported_metrics_are_typed(recall_case: RecallCaseV1) -> None:
    report = score_recall((recall_case,), (observation_for(recall_case, complete=False),))
    assert _metric(report, "embedding_coverage").status is MetricStatusV1.UNAVAILABLE
    assert _metric(report, "grounded_answer_accuracy").status is MetricStatusV1.NOT_MEASURED


def test_known_partial_coverage_scores_examined_over_eligible(
    recall_case: RecallCaseV1,
) -> None:
    complete = observation_for(recall_case, complete=True)
    coverage = tuple(
        replace(
            item,
            states=(CoverageState.CAPPED, CoverageState.PARTIAL),
            eligible_authorized=4,
            examined=1,
            matched_at_least=0,
            returned=0,
            limit=1,
            next_cursor_available=True,
        )
        if item.lane is SearchLane.CATALOG
        else item
        for item in complete.coverage
    )
    partial = RecallObservationV1.create(
        case=recall_case,
        release_sha256=complete.release_sha256,
        candidates=(),
        coverage=coverage,
    )

    metric = _metric(score_recall((recall_case,), (partial,)), "catalog_coverage")
    assert metric.status is MetricStatusV1.AVAILABLE
    assert metric.value_ppm == 250_000


def test_unknown_coverage_denominator_stays_unavailable(
    recall_case: RecallCaseV1,
) -> None:
    report = score_recall(
        (recall_case,),
        (observation_for(recall_case, complete=False),),
    )
    assert _metric(report, "catalog_coverage").status is MetricStatusV1.UNAVAILABLE


def test_mixed_request_per_corpus_coverage_uses_only_that_corpus_lanes() -> None:
    case = replace(
        next(
            item
            for item in synthetic_cases()
            if item.expected_corpus is ArchiveSearchCorpus.DOCUMENTS
            and ArchiveSearchCorpus.MESSAGES in item.request.corpora
        ),
        evidence_source=RecallEvidenceSourceV1.OWNER_PRIVATE_JSONL,
    )
    complete = observation_for(case, complete=True)
    passage_lanes = {SearchLane.LEXICAL, SearchLane.MESSAGE_HISTORY}
    coverage = []
    for item in complete.coverage:
        if item.lane not in passage_lanes:
            coverage.append(item)
        elif item.corpus is SearchCorpus.RAW_DOCUMENTS:
            coverage.append(
                replace(
                    item,
                    eligible_authorized=4,
                    examined=4,
                    matched_at_least=0,
                    returned=0,
                )
            )
        else:
            coverage.append(
                replace(
                    item,
                    states=(CoverageState.CAPPED, CoverageState.PARTIAL),
                    eligible_authorized=4,
                    examined=1,
                    matched_at_least=1,
                    returned=0,
                    limit=1,
                    next_cursor_available=True,
                )
            )
    observation = RecallObservationV1.create(
        case=case,
        release_sha256=complete.release_sha256,
        candidates=(),
        coverage=coverage,
    )

    report = score_recall((case,), (observation,))
    documents = next(item for item in report.per_corpus if item.label == "documents")
    assert _metric(report, "passage_coverage").value_ppm == 500_000
    assert dict(documents.metrics)["passage_coverage"].value_ppm == 1_000_000


def test_report_is_byte_identical_under_input_permutation() -> None:
    cases = tuple(
        replace(case, evidence_source=RecallEvidenceSourceV1.OWNER_PRIVATE_JSONL)
        for case in synthetic_cases()[:4]
    )
    observations = tuple(
        observation_for(case, candidates=(candidate_for(case),), complete=True) for case in cases
    )
    first = score_recall(cases, observations)
    second = score_recall(reversed(cases), reversed(observations))
    assert first.to_json() == second.to_json()
    assert first.report_sha256 == second.report_sha256


def test_compare_detects_regression_without_release_claim(recall_case: RecallCaseV1) -> None:
    baseline = score_recall(
        (recall_case,),
        (
            observation_for(
                recall_case,
                candidates=(candidate_for(recall_case),),
                complete=True,
            ),
        ),
    )
    candidate = score_recall((recall_case,), (observation_for(recall_case, complete=False),))
    comparison = compare_reports(baseline, candidate)
    assert comparison["regression"] is True
    assert comparison["release_threshold"] == "not_assessed"
    regressions = comparison["regressions"]
    assert isinstance(regressions, list)
    assert any(
        item["baseline_status"] == "available" and item["candidate_status"] == "unavailable"
        for item in regressions
    )
    assert all(item["scope"] != "case" for item in regressions)


def test_compare_rejects_different_manifest() -> None:
    first, second = (
        replace(case, evidence_source=RecallEvidenceSourceV1.OWNER_PRIVATE_JSONL)
        for case in synthetic_cases()[:2]
    )
    baseline = score_recall((first,), (observation_for(first, complete=False),))
    candidate = score_recall((second,), (observation_for(second, complete=False),))
    with pytest.raises(RecallContractError):
        compare_reports(baseline, candidate)


def test_compare_rejects_case_expectation_drift_hidden_by_equal_metrics(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    baseline = score_recall((recall_case,), (observation,))
    payload = baseline.to_payload()
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    assert isinstance(per_taxonomy, list) and isinstance(per_taxonomy[0], dict)
    assert isinstance(per_corpus, list) and isinstance(per_corpus[0], dict)
    for scope in (payload, per_taxonomy[0], per_corpus[0]):
        facts = scope["metric_facts"]
        metrics = scope["metrics"]
        assert isinstance(facts, dict) and isinstance(metrics, dict)
        buckets = facts["ndcg_buckets"]
        assert isinstance(buckets, list) and len(buckets) == 1
        bucket = buckets[0]
        assert isinstance(bucket, dict)
        facts["qrel_count"] = 2
        facts["recalled_at_50_count"] = 2
        facts["recalled_at_100_count"] = 2
        bucket["expected_grade_counts"] = [0, 0, 2]
        bucket["top_10_relevance_grades"] = [3, 3, 0, 0, 0, 0, 0, 0, 0, 0]
        metrics["candidate_recall_at_50"] = MetricValueV1.ratio(2, 2).to_payload()
        metrics["candidate_recall_at_100"] = MetricValueV1.ratio(2, 2).to_payload()
    payload_without_digest = dict(payload)
    payload_without_digest.pop("report_sha256")
    payload["report_sha256"] = digest_payload(
        b"friday/retrieval-recall-report/v1",
        payload_without_digest,
    )
    candidate = RecallReportV1.parse(canonical_json(payload))
    assert tuple(item.value_ppm for _name, item in candidate.metrics) == tuple(
        item.value_ppm for _name, item in baseline.metrics
    )
    with pytest.raises(RecallContractError, match="expectations"):
        compare_reports(baseline, candidate)


def test_compare_detects_sub_ppm_regression_exactly(recall_case: RecallCaseV1) -> None:
    case = replace(
        recall_case,
        request=replace(
            recall_case.request,
            corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE),
        ),
    )
    candidate = candidate_for(case)
    template = observation_for(case, candidates=(candidate,), complete=True)

    def report_with_examined(examined: int) -> RecallReportV1:
        coverage = tuple(
            replace(
                item,
                states=(CoverageState.PARTIAL, CoverageState.STALE),
                eligible_authorized=1_000_000,
                examined=examined,
            )
            if item.corpus is SearchCorpus.RAW_DOCUMENTS and item.lane is SearchLane.CATALOG
            else item
            for item in template.coverage
        )
        observation = RecallObservationV1.create(
            case=case,
            release_sha256=template.release_sha256,
            candidates=(candidate,),
            coverage=coverage,
        )
        return score_recall((case,), (observation,))

    baseline = report_with_examined(999_999)
    regressed = report_with_examined(999_998)
    before = dict(baseline.metrics)["catalog_coverage"]
    after = dict(regressed.metrics)["catalog_coverage"]
    assert before.value_ppm == after.value_ppm == 999_999
    comparison = compare_reports(baseline, regressed)
    assert comparison["regression"] is True
    regressions = comparison["regressions"]
    assert isinstance(regressions, list)
    row = next(item for item in regressions if item["metric"] == "catalog_coverage")
    assert frozenset(row) == {
        "baseline_denominator",
        "baseline_numerator",
        "baseline_ppm",
        "baseline_status",
        "candidate_denominator",
        "candidate_numerator",
        "candidate_ppm",
        "candidate_status",
        "label",
        "metric",
        "scope",
    }
    assert row["baseline_ppm"] == row["candidate_ppm"] == 999_999
    assert row["baseline_numerator"] == 1_999_999
    assert row["candidate_numerator"] == 1_999_998
    assert row["baseline_denominator"] == row["candidate_denominator"] == 2_000_000


def test_report_contains_no_private_request_or_identity_text(recall_case: RecallCaseV1) -> None:
    report = score_recall(
        (recall_case,),
        (observation_for(recall_case, candidates=(candidate_for(recall_case),), complete=True),),
    )
    serialized = report.to_json()
    for forbidden in (
        recall_case.request.query,
        SYNTHETIC_TENANT,
        SYNTHETIC_PRINCIPAL,
        "filename",
        "excerpt",
        "prompt",
        "tool_args",
    ):
        assert forbidden not in serialized


def test_report_case_entries_contain_only_opaque_bindings(recall_case: RecallCaseV1) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    report = score_recall((recall_case,), (observation,))
    payload = report.to_payload()
    cases = payload["cases"]
    assert isinstance(cases, list)
    assert len(cases) == 1
    assert isinstance(cases[0], dict)
    assert frozenset(cases[0]) == {
        "case_id",
        "case_sha256",
        "observation_sha256",
    }
    assert cases[0] == {
        "case_id": observation.case_id,
        "case_sha256": recall_case.canonical_sha256,
        "observation_sha256": observation.observation_sha256,
    }
    metric_facts = payload["metric_facts"]
    assert isinstance(metric_facts, dict)
    assert frozenset(metric_facts) == {
        "dated_correct_count",
        "dated_match_count",
        "expected_hit_case_count",
        "false_absence_count",
        "first_relevant_rank_counts",
        "ndcg_buckets",
        "ndcg_sum_ppm",
        "qrel_count",
        "recalled_at_100_count",
        "recalled_at_50_count",
    }
    ndcg_buckets = metric_facts["ndcg_buckets"]
    assert isinstance(ndcg_buckets, list) and ndcg_buckets
    assert all(
        isinstance(item, dict)
        and frozenset(item)
        == {
            "absence_decision",
            "case_count",
            "coverage",
            "expected_grade_counts",
            "expected_temporal_grade_counts",
            "rank_11_50_match_counts",
            "rank_51_100_match_counts",
            "top_10_relevance_grades",
            "top_10_temporal_correct",
        }
        for item in ndcg_buckets
    )
    coverage_keys = {
        "absence_oracle_ready",
        "expected_corpus",
        "expected_score_sums_ppm",
        "expected_unknown_counts",
        "score_sums_ppm",
        "target_counts",
        "taxonomy",
        "unknown_counts",
    }
    assert all(
        isinstance(item, dict)
        and isinstance(item["coverage"], dict)
        and frozenset(item["coverage"]) == coverage_keys
        for item in ndcg_buckets
    )
    coverage_plans = payload["coverage_plan_facts"]
    assert isinstance(coverage_plans, list) and coverage_plans
    assert all(
        isinstance(item, dict)
        and frozenset(item) == {"case_count", "coverage", "false_absence_count"}
        and isinstance(item["coverage"], dict)
        and frozenset(item["coverage"]) == coverage_keys
        and all(
            isinstance(item["coverage"][name], list)
            and len(item["coverage"][name]) == 3
            and all(type(count) is int for count in item["coverage"][name])
            for name in (
                "expected_score_sums_ppm",
                "expected_unknown_counts",
                "score_sums_ppm",
                "target_counts",
                "unknown_counts",
            )
        )
        for item in coverage_plans
    )
    serialized = report.to_json()
    for forbidden_key in (
        '"candidate_count":',
        '"coverage_target_scores":',
        '"expected_no_hit":',
        '"expected_relevance_grades":',
        '"first_relevant_rank":',
        '"matched_facts":',
        '"outcome":',
    ):
        assert forbidden_key not in serialized


def test_semantic_case_label_and_private_key_are_pseudonymized_from_report(
    recall_case: RecallCaseV1,
) -> None:
    private_case = replace(recall_case, case_id="alice-private-diagnosis")
    observation = observation_for(private_case, complete=False)
    report = score_recall((private_case,), (observation,))
    serialized = observation.to_json() + report.to_json()
    assert "alice-private-diagnosis" not in serialized
    assert private_case.privacy_key_hex not in serialized
    assert observation.case_id == private_case.opaque_case_id


def test_report_rejects_aggregate_metrics_that_contradict_per_case_facts(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    report = score_recall((recall_case,), (observation,))
    forged_metrics = tuple(
        (
            name,
            replace(metric, numerator=0, value_ppm=0) if name == "candidate_recall_at_100" else metric,
        )
        for name, metric in report.metrics
    )
    with pytest.raises(RecallContractError):
        RecallReportV1.create(
            evidence_source=report.evidence_source,
            release_sha256=report.release_sha256,
            case_manifest_sha256=report.case_manifest_sha256,
            observation_manifest_sha256=report.observation_manifest_sha256,
            metrics=forged_metrics,
            per_taxonomy=report.per_taxonomy,
            per_corpus=report.per_corpus,
            cases=score_recall_case_results((recall_case,), (observation,)),
        )


def test_report_factory_rejects_forged_per_corpus_metric(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    report = score_recall((recall_case,), (observation,))
    breakdown = report.per_corpus[0]
    forged_facts = replace(
        breakdown.metric_facts,
        recalled_at_50_count=0,
        recalled_at_100_count=0,
        first_relevant_rank_counts=(0,) * 10,
        ndcg_sum_ppm=0,
        ndcg_buckets=tuple(
            replace(item, top_10_relevance_grades=(0,) * 10) for item in breakdown.metric_facts.ndcg_buckets
        ),
        dated_match_count=0,
        dated_correct_count=0,
    )
    forged_breakdown = replace(
        breakdown,
        metric_facts=forged_facts,
        metrics=forged_facts.metrics(breakdown.coverage_facts),
    )
    with pytest.raises(RecallContractError):
        RecallReportV1.create(
            evidence_source=report.evidence_source,
            release_sha256=report.release_sha256,
            case_manifest_sha256=report.case_manifest_sha256,
            observation_manifest_sha256=report.observation_manifest_sha256,
            metrics=report.metrics,
            per_taxonomy=report.per_taxonomy,
            per_corpus=(forged_breakdown,),
            cases=score_recall_case_results((recall_case,), (observation,)),
        )


def test_report_parser_rejects_aggregate_metric_that_contradicts_breakdowns(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    report = score_recall((recall_case,), (observation,))
    payload = report.to_payload()
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    metrics["candidate_recall_at_100"] = MetricValueV1.ratio(0, 1).to_payload()
    payload_without_digest = dict(payload)
    payload_without_digest.pop("report_sha256")
    payload["report_sha256"] = digest_payload(
        b"friday/retrieval-recall-report/v1",
        payload_without_digest,
    )
    with pytest.raises(RecallContractError):
        RecallReportV1.parse(canonical_json(payload))


def test_report_parser_rejects_corpus_coverage_that_contradicts_total(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(recall_case, complete=False)
    report = score_recall((recall_case,), (observation,))
    payload = report.to_payload()
    per_corpus = payload["per_corpus"]
    assert isinstance(per_corpus, list) and isinstance(per_corpus[0], dict)
    coverage_facts = per_corpus[0]["coverage_facts"]
    metrics = per_corpus[0]["metrics"]
    assert isinstance(coverage_facts, dict) and isinstance(metrics, dict)
    coverage_facts["catalog_coverage"] = {
        "score_sum_ppm": 1_000_000,
        "target_count": 1,
        "unknown_count": 0,
    }
    metrics["catalog_coverage"] = MetricValueV1.ratio(1_000_000, 1_000_000).to_payload()
    payload_without_digest = dict(payload)
    payload_without_digest.pop("report_sha256")
    payload["report_sha256"] = digest_payload(
        b"friday/retrieval-recall-report/v1",
        payload_without_digest,
    )
    with pytest.raises(RecallContractError):
        RecallReportV1.parse(canonical_json(payload))


def test_report_parser_rejects_impossible_embedding_only_residual_plan(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    payload = score_recall((recall_case,), (observation,)).to_payload()
    top_facts = payload["coverage_facts"]
    top_metrics = payload["metrics"]
    residual = payload["off_expected_coverage_facts"]
    per_taxonomy = payload["per_taxonomy"]
    assert isinstance(top_facts, dict) and isinstance(top_metrics, dict)
    assert isinstance(residual, dict)
    assert isinstance(per_taxonomy, list) and isinstance(per_taxonomy[0], dict)
    taxonomy_facts = per_taxonomy[0]["coverage_facts"]
    taxonomy_metrics = per_taxonomy[0]["metrics"]
    assert isinstance(taxonomy_facts, dict) and isinstance(taxonomy_metrics, dict)
    forged_facts = {
        "score_sum_ppm": 2_000_000,
        "target_count": 2,
        "unknown_count": 0,
    }
    forged_metric = MetricValueV1.ratio(2_000_000, 2_000_000).to_payload()
    top_facts["embedding_coverage"] = forged_facts
    top_metrics["embedding_coverage"] = forged_metric
    taxonomy_facts["embedding_coverage"] = forged_facts
    taxonomy_metrics["embedding_coverage"] = forged_metric
    residual["embedding_coverage"] = {
        "score_sum_ppm": 1_000_000,
        "target_count": 1,
        "unknown_count": 0,
    }
    payload_without_digest = dict(payload)
    payload_without_digest.pop("report_sha256")
    payload["report_sha256"] = digest_payload(
        b"friday/retrieval-recall-report/v1",
        payload_without_digest,
    )
    with pytest.raises(RecallContractError):
        RecallReportV1.parse(canonical_json(payload))


def test_report_parser_rejects_impossible_expected_hit_denominators(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    payload = score_recall((recall_case,), (observation,)).to_payload()
    forged_mrr = MetricValueV1.ratio(0, 2_000_000).to_payload()
    top_metrics = payload["metrics"]
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    assert isinstance(top_metrics, dict)
    assert isinstance(per_taxonomy, list) and isinstance(per_taxonomy[0], dict)
    assert isinstance(per_corpus, list) and isinstance(per_corpus[0], dict)
    top_metrics["mrr_at_10"] = forged_mrr
    for breakdown in (per_taxonomy[0], per_corpus[0]):
        metrics = breakdown["metrics"]
        assert isinstance(metrics, dict)
        metrics["mrr_at_10"] = forged_mrr
    payload_without_digest = dict(payload)
    payload_without_digest.pop("report_sha256")
    payload["report_sha256"] = digest_payload(
        b"friday/retrieval-recall-report/v1",
        payload_without_digest,
    )
    with pytest.raises(RecallContractError):
        RecallReportV1.parse(canonical_json(payload))


@pytest.mark.parametrize("forged_ndcg", (9_088, 500_000))
def test_report_parser_rejects_ndcg_that_contradicts_exact_aggregate_buckets(
    recall_case: RecallCaseV1,
    forged_ndcg: int,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    payload = score_recall((recall_case,), (observation,)).to_payload()
    metric = MetricValueV1.ratio(forged_ndcg, 1_000_000).to_payload()
    top_facts = payload["metric_facts"]
    top_metrics = payload["metrics"]
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    assert isinstance(top_facts, dict) and isinstance(top_metrics, dict)
    assert isinstance(per_taxonomy, list) and isinstance(per_taxonomy[0], dict)
    assert isinstance(per_corpus, list) and isinstance(per_corpus[0], dict)
    top_facts["ndcg_sum_ppm"] = forged_ndcg
    top_metrics["ndcg_at_10"] = metric
    for breakdown in (per_taxonomy[0], per_corpus[0]):
        facts = breakdown["metric_facts"]
        metrics = breakdown["metrics"]
        assert isinstance(facts, dict) and isinstance(metrics, dict)
        facts["ndcg_sum_ppm"] = forged_ndcg
        metrics["ndcg_at_10"] = metric
    payload_without_digest = dict(payload)
    payload_without_digest.pop("report_sha256")
    payload["report_sha256"] = digest_payload(
        b"friday/retrieval-recall-report/v1",
        payload_without_digest,
    )
    with pytest.raises(RecallContractError):
        RecallReportV1.parse(canonical_json(payload))


def test_metric_aggregate_rejects_public_case_bound_overflow() -> None:
    with pytest.raises(RecallContractError):
        RecallMetricAggregateV1(
            expected_hit_case_count=MAX_CASES + 1,
            qrel_count=MAX_CASES + 1,
            recalled_at_50_count=0,
            recalled_at_100_count=0,
            first_relevant_rank_counts=(0,) * 10,
            ndcg_sum_ppm=0,
            ndcg_buckets=(),
            false_absence_count=0,
            dated_match_count=0,
            dated_correct_count=0,
        )


def test_metric_aggregate_rejects_top_10_matches_above_recall_count() -> None:
    bucket = RecallNdcgAggregateBucketV1(
        expected_grade_counts=(0, 0, 2),
        expected_temporal_grade_counts=(0, 0, 0),
        top_10_relevance_grades=(3, 3, 0, 0, 0, 0, 0, 0, 0, 0),
        top_10_temporal_correct=(None,) * 10,
        rank_11_50_match_counts=(0,) * 9,
        rank_51_100_match_counts=(0,) * 9,
        absence_decision=AbsenceDecision.EVIDENCE_FOUND,
        coverage=_full_document_coverage(),
        case_count=1,
    )
    with pytest.raises(RecallContractError):
        RecallMetricAggregateV1(
            expected_hit_case_count=1,
            qrel_count=2,
            recalled_at_50_count=1,
            recalled_at_100_count=1,
            first_relevant_rank_counts=(1, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            ndcg_sum_ppm=bucket.ndcg_ppm,
            ndcg_buckets=(bucket,),
            false_absence_count=0,
            dated_match_count=0,
            dated_correct_count=0,
        )


def test_metric_aggregate_rejects_false_absence_above_profile_capacity() -> None:
    buckets = tuple(
        RecallNdcgAggregateBucketV1(
            expected_grade_counts=(qrel_count, 0, 0),
            expected_temporal_grade_counts=(0, 0, 0),
            top_10_relevance_grades=(0,) * 10,
            top_10_temporal_correct=(None,) * 10,
            rank_11_50_match_counts=(0,) * 9,
            rank_51_100_match_counts=(0,) * 9,
            absence_decision=(
                AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
                if qrel_count == 10
                else AbsenceDecision.NOT_ESTABLISHED
            ),
            coverage=_full_document_coverage(
                absence_oracle_ready=qrel_count == 10,
            ),
            case_count=1,
        )
        for qrel_count in (10, 20)
    )
    with pytest.raises(RecallContractError):
        RecallMetricAggregateV1(
            expected_hit_case_count=2,
            qrel_count=30,
            recalled_at_50_count=29,
            recalled_at_100_count=29,
            first_relevant_rank_counts=(0,) * 10,
            ndcg_sum_ppm=0,
            ndcg_buckets=buckets,
            false_absence_count=1,
            dated_match_count=0,
            dated_correct_count=0,
        )


@pytest.mark.parametrize(
    ("temporal_correct", "absence_decision"),
    (
        (True, AbsenceDecision.EVIDENCE_FOUND),
        (None, AbsenceDecision.NOT_ESTABLISHED),
    ),
)
def test_ndcg_bucket_rejects_untyped_temporal_or_non_evidence_match(
    temporal_correct: bool | None,
    absence_decision: AbsenceDecision,
) -> None:
    with pytest.raises(RecallContractError):
        RecallNdcgAggregateBucketV1(
            expected_grade_counts=(0, 0, 1),
            expected_temporal_grade_counts=(0, 0, 0),
            top_10_relevance_grades=(3, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            top_10_temporal_correct=(temporal_correct,) + (None,) * 9,
            rank_11_50_match_counts=(0,) * 9,
            rank_51_100_match_counts=(0,) * 9,
            absence_decision=absence_decision,
            coverage=_full_document_coverage(
                absence_oracle_ready=absence_decision is not AbsenceDecision.NOT_ESTABLISHED,
            ),
            case_count=1,
        )


def test_metric_aggregate_normalizes_malformed_nested_bucket_error() -> None:
    malformed = object.__new__(RecallNdcgAggregateBucketV1)
    with pytest.raises(RecallContractError, match="malformed"):
        RecallMetricAggregateV1(
            expected_hit_case_count=1,
            qrel_count=1,
            recalled_at_50_count=0,
            recalled_at_100_count=0,
            first_relevant_rank_counts=(0,) * 10,
            ndcg_sum_ppm=0,
            ndcg_buckets=(malformed,),
            false_absence_count=0,
            dated_match_count=0,
            dated_correct_count=0,
        )


def test_coverage_configuration_rejects_ready_but_unknown_lane() -> None:
    with pytest.raises(RecallContractError, match="readiness"):
        RecallCoverageConfigurationV1(
            taxonomy=RecallTaxonomyV1.APPROXIMATE_CONTENT,
            expected_corpus=ArchiveSearchCorpus.DOCUMENTS,
            absence_oracle_ready=True,
            target_counts=(1, 1, 1),
            unknown_counts=(1, 0, 0),
            score_sums_ppm=(0, 1_000_000, 1_000_000),
            expected_unknown_counts=(1, 0, 0),
            expected_score_sums_ppm=(0, 1_000_000, 1_000_000),
        )


def test_aggregate_buckets_normalize_malformed_coverage_error() -> None:
    malformed = object.__new__(RecallCoverageConfigurationV1)
    with pytest.raises(RecallContractError, match="malformed"):
        RecallCoveragePlanAggregateV1(
            coverage=malformed,
            case_count=1,
            false_absence_count=0,
        )
    with pytest.raises(RecallContractError, match="malformed"):
        RecallNdcgAggregateBucketV1(
            expected_grade_counts=(0, 0, 1),
            expected_temporal_grade_counts=(0, 0, 0),
            top_10_relevance_grades=(0,) * 10,
            top_10_temporal_correct=(None,) * 10,
            rank_11_50_match_counts=(0,) * 9,
            rank_51_100_match_counts=(0,) * 9,
            absence_decision=AbsenceDecision.NOT_ESTABLISHED,
            coverage=malformed,
            case_count=1,
        )


def test_report_parser_rejects_coverage_count_above_per_case_bound(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(recall_case, complete=True)
    payload = score_recall((recall_case,), (observation,)).to_payload()
    forged_facts = {
        "score_sum_ppm": 100_000_000,
        "target_count": 100,
        "unknown_count": 0,
    }
    forged_metric = MetricValueV1.ratio(100_000_000, 100_000_000).to_payload()
    coverage_facts = payload["coverage_facts"]
    metrics = payload["metrics"]
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    assert isinstance(coverage_facts, dict) and isinstance(metrics, dict)
    assert isinstance(per_taxonomy, list) and isinstance(per_taxonomy[0], dict)
    assert isinstance(per_corpus, list) and isinstance(per_corpus[0], dict)
    coverage_facts["catalog_coverage"] = forged_facts
    metrics["catalog_coverage"] = forged_metric
    for breakdown in (per_taxonomy[0], per_corpus[0]):
        breakdown_facts = breakdown["coverage_facts"]
        breakdown_metrics = breakdown["metrics"]
        assert isinstance(breakdown_facts, dict) and isinstance(breakdown_metrics, dict)
        breakdown_facts["catalog_coverage"] = forged_facts
        breakdown_metrics["catalog_coverage"] = forged_metric
    payload_without_digest = dict(payload)
    payload_without_digest.pop("report_sha256")
    payload["report_sha256"] = digest_payload(
        b"friday/retrieval-recall-report/v1",
        payload_without_digest,
    )
    with pytest.raises(RecallContractError):
        RecallReportV1.parse(canonical_json(payload))


def test_breakdown_rejects_seven_non_message_catalog_targets_for_one_case(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    breakdown = score_recall((recall_case,), (observation,)).per_taxonomy[0]
    forged_coverage = tuple(
        (
            name,
            replace(
                facts,
                target_count=7,
                unknown_count=0,
                score_sum_ppm=7_000_000,
            ),
        )
        for name, facts in breakdown.coverage_facts
    )
    with pytest.raises(RecallContractError):
        replace(
            breakdown,
            coverage_facts=forged_coverage,
            metrics=breakdown.metric_facts.metrics(forged_coverage),
        )


@pytest.mark.parametrize(
    "forgery",
    (
        "ranked_without_recall_50",
        "false_absence_with_recall",
        "ranked_positivity_drift",
        "zero_coverage_targets",
    ),
)
def test_report_parser_rejects_cross_metric_impossibilities(
    recall_case: RecallCaseV1,
    forgery: str,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    payload = score_recall((recall_case,), (observation,)).to_payload()
    top_metrics = payload["metrics"]
    top_coverage = payload["coverage_facts"]
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    assert isinstance(top_metrics, dict) and isinstance(top_coverage, dict)
    assert isinstance(per_taxonomy, list) and isinstance(per_taxonomy[0], dict)
    assert isinstance(per_corpus, list) and isinstance(per_corpus[0], dict)
    breakdowns = (per_taxonomy[0], per_corpus[0])
    if forgery == "zero_coverage_targets":
        zero_facts = {"score_sum_ppm": 0, "target_count": 0, "unknown_count": 0}
        for name in ("catalog_coverage", "passage_coverage", "embedding_coverage"):
            top_coverage[name] = zero_facts
            top_metrics[name] = MetricValueV1.unavailable().to_payload()
            for breakdown in breakdowns:
                facts = breakdown["coverage_facts"]
                metrics = breakdown["metrics"]
                assert isinstance(facts, dict) and isinstance(metrics, dict)
                facts[name] = zero_facts
                metrics[name] = MetricValueV1.unavailable().to_payload()
    else:
        metric_name, forged_metric = {
            "ranked_without_recall_50": (
                "candidate_recall_at_50",
                MetricValueV1.ratio(0, 1),
            ),
            "false_absence_with_recall": (
                "false_absence_rate",
                MetricValueV1.ratio(1, 1),
            ),
            "ranked_positivity_drift": (
                "ndcg_at_10",
                MetricValueV1.ratio(0, 1_000_000),
            ),
        }[forgery]
        top_metrics[metric_name] = forged_metric.to_payload()
        for breakdown in breakdowns:
            metrics = breakdown["metrics"]
            assert isinstance(metrics, dict)
            metrics[metric_name] = forged_metric.to_payload()
    payload_without_digest = dict(payload)
    payload_without_digest.pop("report_sha256")
    payload["report_sha256"] = digest_payload(
        b"friday/retrieval-recall-report/v1",
        payload_without_digest,
    )
    with pytest.raises(RecallContractError):
        RecallReportV1.parse(canonical_json(payload))


@pytest.mark.parametrize(
    ("metric_name", "forged_metric"),
    (
        ("false_absence_rate", MetricValueV1.ratio(1, 2)),
        ("candidate_recall_at_50", MetricValueV1.ratio(1, 2)),
    ),
)
def test_report_parser_rejects_impossible_multi_case_gain_capacity(
    metric_name: str,
    forged_metric: MetricValueV1,
) -> None:
    cases = tuple(
        replace(case, evidence_source=RecallEvidenceSourceV1.OWNER_PRIVATE_JSONL)
        for case in synthetic_cases()[:2]
    )
    observations = tuple(
        observation_for(case, candidates=(candidate_for(case),), complete=True) for case in cases
    )
    payload = score_recall(cases, observations).to_payload()
    top_metrics = payload["metrics"]
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    assert isinstance(top_metrics, dict)
    assert isinstance(per_taxonomy, list) and len(per_taxonomy) == 1
    assert isinstance(per_corpus, list) and len(per_corpus) == 1
    top_metrics[metric_name] = forged_metric.to_payload()
    for breakdown in (per_taxonomy[0], per_corpus[0]):
        assert isinstance(breakdown, dict) and isinstance(breakdown["metrics"], dict)
        breakdown["metrics"][metric_name] = forged_metric.to_payload()
    payload_without_digest = dict(payload)
    payload_without_digest.pop("report_sha256")
    payload["report_sha256"] = digest_payload(
        b"friday/retrieval-recall-report/v1",
        payload_without_digest,
    )
    with pytest.raises(RecallContractError):
        RecallReportV1.parse(canonical_json(payload))


def test_report_parser_rejects_temporal_match_without_temporal_qrel(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    payload = score_recall((recall_case,), (observation,)).to_payload()
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    assert isinstance(per_taxonomy, list) and isinstance(per_taxonomy[0], dict)
    assert isinstance(per_corpus, list) and isinstance(per_corpus[0], dict)
    for scope in (payload, per_taxonomy[0], per_corpus[0]):
        facts = scope["metric_facts"]
        metrics = scope["metrics"]
        assert isinstance(facts, dict) and isinstance(metrics, dict)
        buckets = facts["ndcg_buckets"]
        assert isinstance(buckets, list) and isinstance(buckets[0], dict)
        ranked_temporal = buckets[0]["top_10_temporal_correct"]
        assert isinstance(ranked_temporal, list)
        ranked_temporal[0] = True
        facts["dated_match_count"] = 1
        facts["dated_correct_count"] = 1
        metrics["date_role_accuracy"] = MetricValueV1.ratio(1, 1).to_payload()
    with pytest.raises(RecallContractError):
        _parse_resealed_report(payload)


@pytest.mark.parametrize("off_expected_mode", ("unknown", "partial"))
def test_report_parser_rejects_false_absence_with_unfinished_off_expected_lane(
    recall_case: RecallCaseV1,
    off_expected_mode: str,
) -> None:
    case = replace(
        recall_case,
        request=replace(
            recall_case.request,
            corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE),
        ),
    )
    complete = observation_for(case, complete=True)
    coverage = tuple(
        (
            replace(
                item,
                states=(CoverageState.PARTIAL, CoverageState.UNAVAILABLE),
                eligible_authorized=None,
                examined=0,
                authority_rechecked=False,
                snapshot_current=False,
            )
            if off_expected_mode == "unknown"
            else replace(
                item,
                states=(CoverageState.CAPPED, CoverageState.PARTIAL),
                eligible_authorized=4,
                examined=1,
                limit=1,
                next_cursor_available=True,
            )
        )
        if item.corpus is SearchCorpus.KNOWLEDGE and item.lane is SearchLane.CATALOG
        else item
        for item in complete.coverage
    )
    observation = RecallObservationV1.create(
        case=case,
        release_sha256=complete.release_sha256,
        candidates=(),
        coverage=coverage,
    )
    assert observation.absence_decision is AbsenceDecision.NOT_ESTABLISHED
    payload = score_recall((case,), (observation,)).to_payload()
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    coverage_plans = payload["coverage_plan_facts"]
    assert isinstance(per_taxonomy, list) and isinstance(per_taxonomy[0], dict)
    assert isinstance(per_corpus, list) and isinstance(per_corpus[0], dict)
    assert isinstance(coverage_plans, list) and isinstance(coverage_plans[0], dict)
    for scope in (payload, per_taxonomy[0], per_corpus[0]):
        facts = scope["metric_facts"]
        metrics = scope["metrics"]
        assert isinstance(facts, dict) and isinstance(metrics, dict)
        buckets = facts["ndcg_buckets"]
        assert isinstance(buckets, list) and isinstance(buckets[0], dict)
        buckets[0]["absence_decision"] = AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED.value
        facts["false_absence_count"] = 1
        metrics["false_absence_rate"] = MetricValueV1.ratio(1, 1).to_payload()
    coverage_plans[0]["false_absence_count"] = 1
    with pytest.raises(RecallContractError, match="full known coverage"):
        _parse_resealed_report(payload)


def test_compare_rejects_temporal_expectation_drift_with_equal_relevance_metrics(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    baseline = score_recall((recall_case,), (observation,))
    payload = baseline.to_payload()
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    assert isinstance(per_taxonomy, list) and isinstance(per_taxonomy[0], dict)
    assert isinstance(per_corpus, list) and isinstance(per_corpus[0], dict)
    for scope in (payload, per_taxonomy[0], per_corpus[0]):
        facts = scope["metric_facts"]
        metrics = scope["metrics"]
        assert isinstance(facts, dict) and isinstance(metrics, dict)
        buckets = facts["ndcg_buckets"]
        assert isinstance(buckets, list) and isinstance(buckets[0], dict)
        buckets[0]["expected_temporal_grade_counts"] = [0, 0, 1]
        ranked_temporal = buckets[0]["top_10_temporal_correct"]
        assert isinstance(ranked_temporal, list)
        ranked_temporal[0] = True
        facts["dated_match_count"] = 1
        facts["dated_correct_count"] = 1
        metrics["date_role_accuracy"] = MetricValueV1.ratio(1, 1).to_payload()
    candidate = _parse_resealed_report(payload)
    for name in ("candidate_recall_at_50", "candidate_recall_at_100", "mrr_at_10", "ndcg_at_10"):
        assert dict(candidate.metrics)[name] == dict(baseline.metrics)[name]
    with pytest.raises(RecallContractError, match="expectations"):
        compare_reports(baseline, candidate)


def test_compare_rejects_coverage_plan_drift_with_equal_target_totals() -> None:
    stock = synthetic_cases()
    first, second = (
        replace(case, evidence_source=RecallEvidenceSourceV1.OWNER_PRIVATE_JSONL) for case in stock[:2]
    )
    second = replace(
        second,
        alternatives=(replace(second.alternatives[0], relevance_grade=2),),
    )
    second = replace(
        second,
        request=replace(
            second.request,
            corpora=(
                ArchiveSearchCorpus.DOCUMENTS,
                ArchiveSearchCorpus.KNOWLEDGE,
                ArchiveSearchCorpus.OBSIDIAN,
            ),
        ),
    )
    cases = (first, second)
    observations = tuple(
        observation_for(case, candidates=(candidate_for(case),), complete=True) for case in cases
    )
    baseline = score_recall(cases, observations)
    assert tuple(item.target_counts for item in baseline.coverage_plan_facts) == (
        (1, 1, 1),
        (3, 3, 3),
    )
    payload = baseline.to_payload()
    replacement_coverage = {
        "absence_oracle_ready": True,
        "expected_corpus": ArchiveSearchCorpus.DOCUMENTS.value,
        "expected_score_sums_ppm": [1_000_000, 1_000_000, 1_000_000],
        "expected_unknown_counts": [0, 0, 0],
        "score_sums_ppm": [2_000_000, 2_000_000, 2_000_000],
        "target_counts": [2, 2, 2],
        "taxonomy": RecallTaxonomyV1.APPROXIMATE_CONTENT.value,
        "unknown_counts": [0, 0, 0],
    }
    payload["coverage_plan_facts"] = [
        {
            "case_count": 2,
            "coverage": replacement_coverage,
            "false_absence_count": 0,
        }
    ]
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    assert isinstance(per_taxonomy, list) and len(per_taxonomy) == 1
    assert isinstance(per_corpus, list) and len(per_corpus) == 1
    for scope in (payload, *per_taxonomy, *per_corpus):
        assert isinstance(scope, dict)
        facts = scope["metric_facts"]
        assert isinstance(facts, dict)
        buckets = facts["ndcg_buckets"]
        assert isinstance(buckets, list)
        for bucket in buckets:
            assert isinstance(bucket, dict)
            bucket["coverage"] = replacement_coverage
    candidate = _parse_resealed_report(payload)
    assert candidate.coverage_facts == baseline.coverage_facts
    with pytest.raises(RecallContractError, match="coverage plans"):
        compare_reports(baseline, candidate)


def test_compare_rejects_qrel_profile_reallocated_between_coverage_plans() -> None:
    stock = synthetic_cases()
    first, second = (
        replace(case, evidence_source=RecallEvidenceSourceV1.OWNER_PRIVATE_JSONL) for case in stock[:2]
    )
    second = replace(
        second,
        alternatives=(replace(second.alternatives[0], relevance_grade=2),),
        request=replace(
            second.request,
            corpora=(
                ArchiveSearchCorpus.DOCUMENTS,
                ArchiveSearchCorpus.KNOWLEDGE,
                ArchiveSearchCorpus.OBSIDIAN,
            ),
        ),
    )
    cases = (first, second)
    observations = tuple(
        observation_for(case, candidates=(candidate_for(case),), complete=True) for case in cases
    )
    baseline = score_recall(cases, observations)
    payload = baseline.to_payload()
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    assert isinstance(per_taxonomy, list) and len(per_taxonomy) == 1
    assert isinstance(per_corpus, list) and len(per_corpus) == 1
    for scope in (payload, per_taxonomy[0], per_corpus[0]):
        assert isinstance(scope, dict)
        facts = scope["metric_facts"]
        assert isinstance(facts, dict)
        buckets = facts["ndcg_buckets"]
        assert isinstance(buckets, list) and len(buckets) == 2
        first_coverage = buckets[0]["coverage"]
        second_coverage = buckets[1]["coverage"]
        buckets[0]["coverage"], buckets[1]["coverage"] = second_coverage, first_coverage

    candidate = _parse_resealed_report(payload)
    assert candidate.coverage_plan_facts == baseline.coverage_plan_facts
    assert candidate.metrics == baseline.metrics
    with pytest.raises(RecallContractError, match="expectations"):
        compare_reports(baseline, candidate)


def test_report_parser_rejects_message_only_plans_above_message_cases() -> None:
    document_cases = tuple(
        replace(case, evidence_source=RecallEvidenceSourceV1.OWNER_PRIVATE_JSONL)
        for case in synthetic_cases()[:2]
    )
    message_case = replace(
        next(case for case in synthetic_cases() if case.expected_corpus is ArchiveSearchCorpus.MESSAGES),
        evidence_source=RecallEvidenceSourceV1.OWNER_PRIVATE_JSONL,
    )
    mixed_document = replace(
        document_cases[0],
        request=replace(
            document_cases[0].request,
            corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.MESSAGES),
        ),
    )
    cases = (mixed_document, document_cases[1], message_case)
    observations = tuple(
        observation_for(case, candidates=(candidate_for(case),), complete=True) for case in cases
    )
    payload = score_recall(cases, observations).to_payload()
    payload["coverage_plan_facts"] = [
        {
            "case_count": 2,
            "coverage": {
                "absence_oracle_ready": True,
                "expected_corpus": ArchiveSearchCorpus.MESSAGES.value,
                "expected_score_sums_ppm": [0, 2_000_000, 1_000_000],
                "expected_unknown_counts": [0, 0, 0],
                "score_sums_ppm": [0, 2_000_000, 1_000_000],
                "target_counts": [0, 2, 1],
                "taxonomy": message_case.taxonomy.value,
                "unknown_counts": [0, 0, 0],
            },
            "false_absence_count": 0,
        },
        {
            "case_count": 1,
            "coverage": {
                "absence_oracle_ready": True,
                "expected_corpus": ArchiveSearchCorpus.DOCUMENTS.value,
                "expected_score_sums_ppm": [1_000_000, 1_000_000, 1_000_000],
                "expected_unknown_counts": [0, 0, 0],
                "score_sums_ppm": [2_000_000, 2_000_000, 2_000_000],
                "target_counts": [2, 2, 2],
                "taxonomy": document_cases[0].taxonomy.value,
                "unknown_counts": [0, 0, 0],
            },
            "false_absence_count": 0,
        },
    ]
    with pytest.raises(RecallContractError, match="coverage"):
        _parse_resealed_report(payload)


def test_known_empty_lane_scores_coverage_without_confirming_absence(
    recall_case: RecallCaseV1,
) -> None:
    observation = observation_for(recall_case, complete=True)
    coverage = tuple(
        replace(
            item,
            states=(CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL),
        )
        if item.lane is SearchLane.CATALOG
        else item
        for item in observation.coverage
    )
    uncertain = RecallObservationV1.create(
        case=recall_case,
        release_sha256=observation.release_sha256,
        candidates=(),
        coverage=coverage,
    )
    assert uncertain.absence_decision is AbsenceDecision.NOT_ESTABLISHED
    report = score_recall((recall_case,), (uncertain,))
    assert _metric(report, "catalog_coverage").value_ppm == 1_000_000
    assert report.coverage_plan_facts[0].coverage.absence_oracle_ready is False
    ready_payload = report.to_payload()
    ready_taxonomy = ready_payload["per_taxonomy"]
    ready_corpus = ready_payload["per_corpus"]
    ready_plans = ready_payload["coverage_plan_facts"]
    assert isinstance(ready_taxonomy, list) and isinstance(ready_taxonomy[0], dict)
    assert isinstance(ready_corpus, list) and isinstance(ready_corpus[0], dict)
    assert isinstance(ready_plans, list) and isinstance(ready_plans[0], dict)
    for scope in (ready_payload, ready_taxonomy[0], ready_corpus[0]):
        facts = scope["metric_facts"]
        assert isinstance(facts, dict)
        buckets = facts["ndcg_buckets"]
        assert isinstance(buckets, list) and isinstance(buckets[0], dict)
        bucket_coverage = buckets[0]["coverage"]
        assert isinstance(bucket_coverage, dict)
        bucket_coverage["absence_oracle_ready"] = True
    plan_coverage = ready_plans[0]["coverage"]
    assert isinstance(plan_coverage, dict)
    plan_coverage["absence_oracle_ready"] = True
    with pytest.raises(RecallContractError, match="uncertain absence"):
        _parse_resealed_report(ready_payload)

    payload = report.to_payload()
    per_taxonomy = payload["per_taxonomy"]
    per_corpus = payload["per_corpus"]
    plans = payload["coverage_plan_facts"]
    assert isinstance(per_taxonomy, list) and isinstance(per_taxonomy[0], dict)
    assert isinstance(per_corpus, list) and isinstance(per_corpus[0], dict)
    assert isinstance(plans, list) and isinstance(plans[0], dict)
    for scope in (payload, per_taxonomy[0], per_corpus[0]):
        facts = scope["metric_facts"]
        metrics = scope["metrics"]
        assert isinstance(facts, dict) and isinstance(metrics, dict)
        buckets = facts["ndcg_buckets"]
        assert isinstance(buckets, list) and isinstance(buckets[0], dict)
        buckets[0]["absence_decision"] = AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED.value
        facts["false_absence_count"] = 1
        metrics["false_absence_rate"] = MetricValueV1.ratio(1, 1).to_payload()
    plans[0]["false_absence_count"] = 1
    with pytest.raises(RecallContractError, match="oracle-ready"):
        _parse_resealed_report(payload)
