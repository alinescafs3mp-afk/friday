from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

import pytest

from friday.retrieval.archive_search_authority import canonical_archive_search_targets
from friday.retrieval.contracts import AbsenceDecision, CoverageState, TemporalRole
from friday.retrieval_benchmark.contracts import (
    RecallCandidateV1,
    RecallCaseV1,
    RecallCoverageV1,
    RecallEvidenceSourceV1,
    RecallObservationV1,
)
from friday.retrieval_benchmark.synthetic import synthetic_cases

RELEASE_SHA256 = "f" * 64


@pytest.fixture
def recall_case() -> RecallCaseV1:
    return replace(
        synthetic_cases()[0],
        evidence_source=RecallEvidenceSourceV1.OWNER_PRIVATE_JSONL,
    )


def candidate_for(
    case: RecallCaseV1,
    *,
    rank: int = 1,
    source_identity: str | None = None,
    passage_identity: str | None = None,
    temporal_roles: Iterable[TemporalRole] = (),
) -> RecallCandidateV1:
    alternative = case.alternatives[0]
    return RecallCandidateV1(
        rank=rank,
        corpus=case.expected_corpus,
        source_identity=source_identity or alternative.source_identity,
        passage_window_identities=(passage_identity or alternative.passage_window_identities[0],),
        locator_kinds=(alternative.locator_kind,),
        temporal_roles=tuple(sorted(temporal_roles, key=lambda item: item.value)),
    )


def observation_for(
    case: RecallCaseV1,
    *,
    candidates: Iterable[RecallCandidateV1] = (),
    complete: bool,
    release_sha256: str = RELEASE_SHA256,
) -> RecallObservationV1:
    candidate_values = tuple(candidates)
    coverage: list[RecallCoverageV1] = []
    for corpus, lane in canonical_archive_search_targets(case.request):
        if complete:
            count = 1 if candidate_values else 0
            coverage.append(
                RecallCoverageV1(
                    corpus=corpus,
                    lane=lane,
                    states=(CoverageState.COMPLETE,),
                    eligible_authorized=count,
                    examined=count,
                    matched_at_least=count,
                    returned=count,
                    limit=None,
                    next_cursor_available=False,
                    authority_rechecked=True,
                    snapshot_current=True,
                )
            )
        else:
            coverage.append(
                RecallCoverageV1(
                    corpus=corpus,
                    lane=lane,
                    states=(CoverageState.PARTIAL, CoverageState.UNAVAILABLE),
                    eligible_authorized=None,
                    examined=0,
                    matched_at_least=0,
                    returned=0,
                    limit=None,
                    next_cursor_available=False,
                    authority_rechecked=False,
                    snapshot_current=False,
                )
            )
    result = RecallObservationV1.create(
        case=case,
        release_sha256=release_sha256,
        candidates=candidate_values,
        coverage=coverage,
    )
    expected = (
        AbsenceDecision.EVIDENCE_FOUND
        if candidate_values
        else (AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED if complete else AbsenceDecision.NOT_ESTABLISHED)
    )
    assert result.absence_decision is expected
    return result
