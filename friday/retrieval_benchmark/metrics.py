"""Deterministic integer metrics and comparison for recall benchmark v1."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from typing import Final, cast

from friday.retrieval.archive_search_contract import ArchiveSearchCorpus
from friday.retrieval.contracts import AbsenceDecision, SearchCorpus, SearchLane
from friday.retrieval_benchmark._canonical import RecallContractError, canonical_json
from friday.retrieval_benchmark.contracts import (
    MAX_CASES,
    METRIC_NAMES,
    MetricStatusV1,
    MetricValueV1,
    RecallAlternativeV1,
    RecallBreakdownV1,
    RecallCandidateV1,
    RecallCaseResultV1,
    RecallCaseV1,
    RecallCoverageAggregateV1,
    RecallCoveragePlanAggregateV1,
    RecallEvidenceSourceV1,
    RecallMatchedFactV1,
    RecallMetricAggregateV1,
    RecallObservationV1,
    RecallOutcomeV1,
    RecallReportV1,
    _coverage_target_score,
    _ranked_ndcg_ppm,
    case_manifest_sha256,
    coverage_absence_oracle,
    observation_manifest_sha256,
)

_PPM: Final = 1_000_000
_RECIPROCAL_PPM: Final = tuple(_PPM // rank for rank in range(1, 11))
_HIGHER_IS_BETTER: Final = frozenset(
    {
        "candidate_recall_at_50",
        "candidate_recall_at_100",
        "mrr_at_10",
        "ndcg_at_10",
        "date_role_accuracy",
        "catalog_coverage",
        "passage_coverage",
        "embedding_coverage",
    }
)
_LOWER_IS_BETTER: Final = frozenset({"false_absence_rate"})
_SEARCH_CORPUS_BY_ARCHIVE_CORPUS: Final = {
    ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
    ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
    ArchiveSearchCorpus.MESSAGES: SearchCorpus.CONVERSATION,
    ArchiveSearchCorpus.OBSIDIAN: SearchCorpus.OBSIDIAN,
    ArchiveSearchCorpus.GENERATED: SearchCorpus.GENERATED_ARTIFACTS,
    ArchiveSearchCorpus.WEB: SearchCorpus.WEB_CAPTURES,
    ArchiveSearchCorpus.EXTERNAL: SearchCorpus.EXTERNAL,
}


@dataclass(frozen=True, slots=True)
class _MatchedAlternative:
    alternative: RecallAlternativeV1
    candidate: RecallCandidateV1


def _candidate_matches(
    candidate: RecallCandidateV1,
    alternative: RecallAlternativeV1,
    *,
    corpus: ArchiveSearchCorpus,
) -> bool:
    return bool(
        candidate.corpus is corpus
        and candidate.source_identity == alternative.source_identity
        and alternative.locator_kind in candidate.locator_kinds
        and set(candidate.passage_window_identities) & set(alternative.passage_window_identities)
    )


def _matched_alternatives(
    case: RecallCaseV1,
    observation: RecallObservationV1,
    *,
    limit: int,
) -> tuple[_MatchedAlternative, ...]:
    credited: set[str] = set()
    matches: list[_MatchedAlternative] = []
    alternatives = {item.source_identity: item for item in case.alternatives}
    for candidate in observation.candidates:
        if candidate.rank > limit:
            continue
        alternative = alternatives.get(candidate.source_identity)
        if (
            alternative is None
            or alternative.source_identity in credited
            or not _candidate_matches(candidate, alternative, corpus=case.expected_corpus)
        ):
            continue
        credited.add(alternative.source_identity)
        matches.append(_MatchedAlternative(alternative, candidate))
    return tuple(matches)


def _metric_ratio(numerator: int, denominator: int) -> MetricValueV1:
    return MetricValueV1.ratio(numerator, denominator) if denominator else MetricValueV1.unavailable()


def _coverage_metric(
    pairs: tuple[tuple[RecallCaseV1, RecallObservationV1], ...],
    lanes: frozenset[SearchLane],
    *,
    corpus: ArchiveSearchCorpus | None = None,
) -> MetricValueV1:
    target_scores: list[int] = []
    search_corpus = _SEARCH_CORPUS_BY_ARCHIVE_CORPUS[corpus] if corpus is not None else None
    for _case, observation in pairs:
        for coverage in observation.coverage:
            if coverage.lane not in lanes or (
                search_corpus is not None and coverage.corpus is not search_corpus
            ):
                continue
            score = _coverage_target_score(coverage)
            if score is None:
                return MetricValueV1.unavailable()
            target_scores.append(score)
    if not target_scores:
        return MetricValueV1.unavailable()
    return MetricValueV1.ratio(sum(target_scores), len(target_scores) * _PPM)


def _ndcg_ppm(case: RecallCaseV1, observation: RecallObservationV1) -> int:
    if not case.alternatives:
        return 0
    matched = _matched_alternatives(case, observation, limit=10)
    return _ranked_ndcg_ppm(
        tuple(sorted((item.relevance_grade for item in case.alternatives), reverse=True)),
        tuple(
            RecallMatchedFactV1(
                rank=item.candidate.rank,
                relevance_grade=item.alternative.relevance_grade,
                temporal_correct=(
                    None
                    if item.alternative.temporal_role is None
                    else item.candidate.temporal_roles == (item.alternative.temporal_role,)
                ),
            )
            for item in matched
        ),
    )


def _score_metrics(
    pairs: tuple[tuple[RecallCaseV1, RecallObservationV1], ...],
    *,
    coverage_corpus: ArchiveSearchCorpus | None = None,
) -> tuple[tuple[str, MetricValueV1], ...]:
    alternatives_total = sum(len(case.alternatives) for case, _observation in pairs)
    recall_50 = sum(len(_matched_alternatives(case, observation, limit=50)) for case, observation in pairs)
    recall_100 = sum(len(_matched_alternatives(case, observation, limit=100)) for case, observation in pairs)
    hit_pairs = tuple((case, observation) for case, observation in pairs if not case.expected_no_hit)

    reciprocal_sum = 0
    ndcg_sum = 0
    false_absence = 0
    dated_matched = 0
    dated_correct = 0
    for case, observation in hit_pairs:
        matched_10 = _matched_alternatives(case, observation, limit=10)
        if matched_10:
            reciprocal_sum += _RECIPROCAL_PPM[matched_10[0].candidate.rank - 1]
        ndcg_sum += _ndcg_ppm(case, observation)
        if (
            not observation.candidates
            and observation.absence_decision is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
        ):
            false_absence += 1
        for match in _matched_alternatives(case, observation, limit=100):
            if match.alternative.temporal_role is None:
                continue
            dated_matched += 1
            if match.candidate.temporal_roles == (match.alternative.temporal_role,):
                dated_correct += 1

    hit_count = len(hit_pairs)
    catalog = _coverage_metric(
        pairs,
        frozenset({SearchLane.CATALOG}),
        corpus=coverage_corpus,
    )
    passages = _coverage_metric(
        pairs,
        frozenset({SearchLane.LEXICAL, SearchLane.MESSAGE_HISTORY}),
        corpus=coverage_corpus,
    )
    embedding = _coverage_metric(
        pairs,
        frozenset({SearchLane.DENSE}),
        corpus=coverage_corpus,
    )
    values = {
        "candidate_recall_at_50": _metric_ratio(recall_50, alternatives_total),
        "candidate_recall_at_100": _metric_ratio(recall_100, alternatives_total),
        "mrr_at_10": (
            MetricValueV1.ratio(reciprocal_sum, hit_count * _PPM)
            if hit_count
            else MetricValueV1.unavailable()
        ),
        "ndcg_at_10": (
            MetricValueV1.ratio(ndcg_sum, hit_count * _PPM) if hit_count else MetricValueV1.unavailable()
        ),
        "false_absence_rate": _metric_ratio(false_absence, hit_count),
        "date_role_accuracy": _metric_ratio(dated_correct, dated_matched),
        "catalog_coverage": catalog,
        "passage_coverage": passages,
        "embedding_coverage": embedding,
        "grounded_answer_accuracy": MetricValueV1.not_measured(),
    }
    return tuple((name, values[name]) for name in METRIC_NAMES)


def _validate_pairs(
    cases: Iterable[RecallCaseV1],
    observations: Iterable[RecallObservationV1],
) -> tuple[tuple[RecallCaseV1, RecallObservationV1], ...]:
    try:
        bounded_cases = tuple(islice(iter(cases), MAX_CASES + 1))
        bounded_observations = tuple(islice(iter(observations), MAX_CASES + 1))
    except Exception as exc:
        raise RecallContractError("scoring manifests must be bounded iterables") from exc
    if len(bounded_cases) > MAX_CASES or len(bounded_observations) > MAX_CASES:
        raise RecallContractError("scoring manifests exceed the closed case bound")
    if not bounded_cases or any(type(item) is not RecallCaseV1 for item in bounded_cases):
        raise RecallContractError("scoring requires typed recall cases")
    if any(type(item) is not RecallObservationV1 for item in bounded_observations):
        raise RecallContractError("scoring requires typed recall observations")
    case_values = tuple(sorted(bounded_cases, key=lambda item: item.opaque_case_id))
    observation_values = tuple(sorted(bounded_observations, key=lambda item: item.case_id))
    if len({item.opaque_case_id for item in case_values}) != len(case_values):
        raise RecallContractError("scoring cases contain duplicate IDs")
    if len({item.privacy_key_hex for item in case_values}) != len(case_values):
        raise RecallContractError("scoring cases must use distinct privacy keys")
    if len({item.case_id for item in observation_values}) != len(observation_values):
        raise RecallContractError("scoring observations contain duplicate IDs")
    if tuple(item.opaque_case_id for item in case_values) != tuple(
        item.case_id for item in observation_values
    ):
        raise RecallContractError("case and observation manifests do not align")
    pairs = tuple(zip(case_values, observation_values, strict=True))
    source = case_values[0].evidence_source
    release = observation_values[0].release_sha256
    for case, observation in pairs:
        observation.validate_case_binding(case)
        if observation.evidence_source is not case.evidence_source or case.evidence_source is not source:
            raise RecallContractError("evidence sources cannot be mixed")
        if observation.release_sha256 != release:
            raise RecallContractError("observation releases cannot be mixed")
        if (
            case.evidence_source is RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL
            and not observation.attests_shipped_projection()
        ):
            raise RecallContractError("serialized fixtures cannot claim shipped ephemeral evidence")
    return pairs


def _case_result(case: RecallCaseV1, observation: RecallObservationV1) -> RecallCaseResultV1:
    matches = _matched_alternatives(case, observation, limit=100)
    matched_facts = tuple(
        RecallMatchedFactV1(
            rank=item.candidate.rank,
            relevance_grade=item.alternative.relevance_grade,
            temporal_correct=(
                None
                if item.alternative.temporal_role is None
                else item.candidate.temporal_roles == (item.alternative.temporal_role,)
            ),
        )
        for item in matches
    )
    if matches:
        outcome = RecallOutcomeV1.HIT
        first_rank: int | None = matches[0].candidate.rank
    elif case.expected_no_hit and observation.candidates:
        outcome = RecallOutcomeV1.FALSE_POSITIVE
        first_rank = None
    elif case.expected_no_hit and (
        observation.absence_decision is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
    ):
        outcome = RecallOutcomeV1.EXPECTED_NO_HIT
        first_rank = None
    elif case.expected_no_hit:
        outcome = RecallOutcomeV1.UNCERTAIN_NO_HIT
        first_rank = None
    else:
        outcome = RecallOutcomeV1.MISS
        first_rank = None
    return RecallCaseResultV1(
        case_id=observation.case_id,
        case_sha256=case.canonical_sha256,
        observation_sha256=observation.observation_sha256,
        taxonomy=case.taxonomy,
        corpus=case.expected_corpus,
        expected_no_hit=case.expected_no_hit,
        candidate_count=len(observation.candidates),
        absence_decision=observation.absence_decision,
        coverage_authorizes_absence=(
            coverage_absence_oracle(observation.coverage, candidate_count=0)
            is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
        ),
        outcome=outcome,
        first_relevant_rank=first_rank,
        expected_relevance_grades=tuple(
            sorted((item.relevance_grade for item in case.alternatives), reverse=True)
        ),
        expected_temporal_grade_counts=cast(
            tuple[int, int, int],
            tuple(
                sum(
                    item.relevance_grade == grade and item.temporal_role is not None
                    for item in case.alternatives
                )
                for grade in (1, 2, 3)
            ),
        ),
        matched_facts=matched_facts,
        coverage_target_scores=(
            (
                "catalog_coverage",
                tuple(
                    sorted(
                        (
                            (item.corpus, _coverage_target_score(item))
                            for item in observation.coverage
                            if item.lane is SearchLane.CATALOG
                        ),
                        key=lambda score: (
                            score[0].value,
                            -1 if score[1] is None else score[1],
                        ),
                    )
                ),
            ),
            (
                "passage_coverage",
                tuple(
                    sorted(
                        (
                            (item.corpus, _coverage_target_score(item))
                            for item in observation.coverage
                            if item.lane in {SearchLane.LEXICAL, SearchLane.MESSAGE_HISTORY}
                        ),
                        key=lambda score: (
                            score[0].value,
                            -1 if score[1] is None else score[1],
                        ),
                    )
                ),
            ),
            (
                "embedding_coverage",
                tuple(
                    sorted(
                        (
                            (item.corpus, _coverage_target_score(item))
                            for item in observation.coverage
                            if item.lane is SearchLane.DENSE
                        ),
                        key=lambda score: (
                            score[0].value,
                            -1 if score[1] is None else score[1],
                        ),
                    )
                ),
            ),
        ),
        metrics=_score_metrics(((case, observation),)),
    )


def score_recall(
    cases: Iterable[RecallCaseV1],
    observations: Iterable[RecallObservationV1],
) -> RecallReportV1:
    """Score an exact manifest; input order cannot affect report bytes."""

    pairs = _validate_pairs(cases, observations)
    case_results = tuple(_case_result(case, observation) for case, observation in pairs)
    taxonomy_groups: dict[str, list[RecallCaseResultV1]] = defaultdict(list)
    corpus_groups: dict[str, list[RecallCaseResultV1]] = defaultdict(list)
    for result in case_results:
        taxonomy_groups[result.taxonomy.value].append(result)
        corpus_groups[result.corpus.value].append(result)
    per_taxonomy = tuple(
        RecallBreakdownV1.create(label=label, cases=values)
        for label, values in sorted(taxonomy_groups.items())
    )
    per_corpus = tuple(
        RecallBreakdownV1.create(
            label=label,
            cases=values,
            coverage_corpus=ArchiveSearchCorpus(label),
        )
        for label, values in sorted(corpus_groups.items())
    )
    case_values = tuple(case for case, _observation in pairs)
    observation_values = tuple(observation for _case, observation in pairs)
    return RecallReportV1.create(
        evidence_source=case_values[0].evidence_source,
        release_sha256=observation_values[0].release_sha256,
        case_manifest_sha256=case_manifest_sha256(case_values),
        observation_manifest_sha256=observation_manifest_sha256(observation_values),
        metrics=_score_metrics(pairs),
        per_taxonomy=per_taxonomy,
        per_corpus=per_corpus,
        cases=case_results,
    )


def score_recall_case_results(
    cases: Iterable[RecallCaseV1],
    observations: Iterable[RecallObservationV1],
) -> tuple[RecallCaseResultV1, ...]:
    """Return body-free in-memory diagnostics that are excluded from reports."""

    return tuple(
        _case_result(case, observation) for case, observation in _validate_pairs(cases, observations)
    )


def _metric_expectation_signature(
    facts: RecallMetricAggregateV1,
) -> tuple[
    int,
    int,
    tuple[
        tuple[
            tuple[
                str,
                str,
                tuple[int, int, int],
                tuple[int, int, int],
                tuple[int, int, int],
            ],
            int,
        ],
        ...,
    ],
]:
    profiles: Counter[
        tuple[
            str,
            str,
            tuple[int, int, int],
            tuple[int, int, int],
            tuple[int, int, int],
        ]
    ] = Counter()
    for bucket in facts.ndcg_buckets:
        profiles[
            (
                bucket.coverage.taxonomy.value,
                bucket.coverage.expected_corpus.value,
                bucket.coverage.target_counts,
                bucket.expected_grade_counts,
                bucket.expected_temporal_grade_counts,
            )
        ] += bucket.case_count
    return facts.expected_hit_case_count, facts.qrel_count, tuple(sorted(profiles.items()))


def _coverage_target_signature(
    facts: tuple[tuple[str, RecallCoverageAggregateV1], ...],
) -> tuple[tuple[str, int], ...]:
    return tuple((name, item.target_count) for name, item in facts)


def _coverage_plan_expectation_signature(
    facts: tuple[RecallCoveragePlanAggregateV1, ...],
) -> tuple[tuple[tuple[str, str, tuple[int, int, int]], int], ...]:
    plans: Counter[tuple[str, str, tuple[int, int, int]]] = Counter()
    for item in facts:
        plans[
            (
                item.coverage.taxonomy.value,
                item.coverage.expected_corpus.value,
                item.target_counts,
            )
        ] += item.case_count
    return tuple(sorted(plans.items()))


def compare_reports(baseline: RecallReportV1, candidate: RecallReportV1) -> dict[str, object]:
    """Return a canonical regression verdict without claiming a release threshold."""

    if type(baseline) is not RecallReportV1 or type(candidate) is not RecallReportV1:
        raise RecallContractError("comparison requires typed recall reports")
    if (
        baseline.case_manifest_sha256 != candidate.case_manifest_sha256
        or baseline.evidence_source is not candidate.evidence_source
    ):
        raise RecallContractError("comparison manifests or evidence sources differ")
    baseline_bindings = tuple((item.case_id, item.case_sha256) for item in baseline.cases)
    candidate_bindings = tuple((item.case_id, item.case_sha256) for item in candidate.cases)
    if baseline_bindings != candidate_bindings:
        raise RecallContractError("comparison opaque case bindings differ")
    if (
        _metric_expectation_signature(baseline.metric_facts)
        != _metric_expectation_signature(candidate.metric_facts)
        or _coverage_plan_expectation_signature(baseline.coverage_plan_facts)
        != _coverage_plan_expectation_signature(candidate.coverage_plan_facts)
        or _coverage_target_signature(baseline.coverage_facts)
        != _coverage_target_signature(candidate.coverage_facts)
        or _coverage_target_signature(baseline.off_expected_coverage_facts)
        != _coverage_target_signature(candidate.off_expected_coverage_facts)
    ):
        raise RecallContractError("comparison case expectations or coverage plans differ")
    regressions: list[dict[str, object]] = []

    def compare_metric_sets(
        scope: str,
        label: str,
        before_values: tuple[tuple[str, MetricValueV1], ...],
        after_values: tuple[tuple[str, MetricValueV1], ...],
    ) -> None:
        before_metrics = dict(before_values)
        after_metrics = dict(after_values)
        for name in METRIC_NAMES:
            before = before_metrics[name]
            after = after_metrics[name]
            regressed = False
            if before.status is MetricStatusV1.AVAILABLE:
                if after.status is not MetricStatusV1.AVAILABLE:
                    regressed = True
                else:
                    assert before.numerator is not None and before.denominator is not None
                    assert after.numerator is not None and after.denominator is not None
                    regressed = (
                        name in _HIGHER_IS_BETTER
                        and after.numerator * before.denominator < before.numerator * after.denominator
                    ) or (
                        name in _LOWER_IS_BETTER
                        and after.numerator * before.denominator > before.numerator * after.denominator
                    )
            if regressed:
                regressions.append(
                    {
                        "baseline_denominator": before.denominator,
                        "baseline_numerator": before.numerator,
                        "baseline_ppm": before.value_ppm,
                        "baseline_status": before.status.value,
                        "candidate_denominator": after.denominator,
                        "candidate_numerator": after.numerator,
                        "candidate_ppm": after.value_ppm,
                        "candidate_status": after.status.value,
                        "label": label,
                        "metric": name,
                        "scope": scope,
                    }
                )

    compare_metric_sets("aggregate", "all", baseline.metrics, candidate.metrics)
    for scope, before_groups, after_groups in (
        ("taxonomy", baseline.per_taxonomy, candidate.per_taxonomy),
        ("corpus", baseline.per_corpus, candidate.per_corpus),
    ):
        before_by_label = {item.label: item for item in before_groups}
        after_by_label = {item.label: item for item in after_groups}
        if tuple((item.label, item.case_count) for item in before_groups) != tuple(
            (item.label, item.case_count) for item in after_groups
        ):
            raise RecallContractError("comparison breakdown labels or counts differ")
        for label in sorted(before_by_label):
            before = before_by_label[label]
            after = after_by_label[label]
            if _metric_expectation_signature(before.metric_facts) != _metric_expectation_signature(
                after.metric_facts
            ) or _coverage_target_signature(before.coverage_facts) != _coverage_target_signature(
                after.coverage_facts
            ):
                raise RecallContractError("comparison breakdown expectations or coverage plans differ")
            compare_metric_sets(
                scope,
                label,
                before.metrics,
                after.metrics,
            )
    return {
        "baseline_report_sha256": baseline.report_sha256,
        "candidate_report_sha256": candidate.report_sha256,
        "regression": bool(regressions),
        "regressions": regressions,
        "release_threshold": "not_assessed",
        "schema": "friday.retrieval-recall-comparison.body-free.v1",
    }


def compare_reports_json(baseline: RecallReportV1, candidate: RecallReportV1) -> str:
    return canonical_json(compare_reports(baseline, candidate))


__all__ = [
    "compare_reports",
    "compare_reports_json",
    "score_recall",
    "score_recall_case_results",
]
