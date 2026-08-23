from __future__ import annotations

import json

import pytest

import friday.retrieval.contracts as retrieval_contracts
from friday.retrieval.contracts import (
    AbsenceDecision,
    AuthorityScope,
    CoverageState,
    RetrievalContractError,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
    aggregate_absence_decision,
)


def _binding(
    targets: tuple[tuple[SearchCorpus, SearchLane], ...],
    *,
    request: str = '{"query":"quarterly report"}',
    tenant_id: str = "tenant-main",
    principal_id: str = "person-42",
    snapshot: str = "snapshot-17",
    run: str = "run-0123456789abcdef",
    key: bytes = b"k" * 32,
) -> SearchExecutionBinding:
    return SearchExecutionBinding.create(
        normalized_private_request_json=request,
        authority_scope=AuthorityScope.TENANT_PRINCIPAL,
        tenant_id=tenant_id,
        principal_id=principal_id,
        requested_targets=targets,
        snapshot_discriminator=snapshot,
        run_discriminator=run,
        privacy_key=key,
    )


def _coverage(
    *,
    corpus: SearchCorpus = SearchCorpus.RAW_DOCUMENTS,
    lane: SearchLane = SearchLane.LEXICAL,
    states: tuple[CoverageState, ...] = (CoverageState.COMPLETE,),
    eligible: int | None = 12,
    examined: int = 12,
    matched: int = 0,
    returned: int = 0,
    authority_rechecked: bool = True,
    snapshot_current: bool = True,
    limit: int | None = None,
    cursor: bool = False,
    binding: SearchExecutionBinding | None = None,
) -> SearchCoverage:
    execution_binding = binding or _binding(((corpus, lane),))
    return SearchCoverage.create(
        corpus=corpus,
        lane=lane,
        execution_binding=execution_binding,
        states=states,
        eligible_authorized=eligible,
        examined=examined,
        matched_at_least=matched,
        returned=returned,
        authority_rechecked=authority_rechecked,
        snapshot_current=snapshot_current,
        limit=limit,
        next_cursor_available=cursor,
    )


def test_complete_absence_requires_full_exact_authorized_corpus() -> None:
    coverage = _coverage()
    assert coverage.absence_decision() is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
    assert SearchCoverage.parse(coverage.to_json()) == coverage

    with pytest.raises(RetrievalContractError, match="eligible==examined"):
        _coverage(eligible=20, examined=12)
    with pytest.raises(RetrievalContractError, match="eligible==examined"):
        _coverage(eligible=None, examined=12)


@pytest.mark.parametrize(
    ("authority_rechecked", "snapshot_current"),
    [(False, True), (True, False), (False, False)],
)
def test_complete_projection_without_fresh_attestations_cannot_prove_absence(
    authority_rechecked: bool, snapshot_current: bool
) -> None:
    coverage = _coverage(
        authority_rechecked=authority_rechecked,
        snapshot_current=snapshot_current,
    )
    assert coverage.absence_decision() is AbsenceDecision.NOT_ESTABLISHED


def test_simultaneous_partial_conditions_are_preserved_and_fail_closed() -> None:
    coverage = _coverage(
        lane=SearchLane.DENSE,
        states=(
            CoverageState.PARTIAL,
            CoverageState.CAPPED,
            CoverageState.STALE,
            CoverageState.EMBEDDING_INCOMPATIBLE,
        ),
        eligible=100,
        examined=20,
        limit=20,
        cursor=True,
    )

    assert tuple(item.value for item in coverage.states) == tuple(
        sorted(item.value for item in coverage.states)
    )
    assert coverage.absence_decision() is AbsenceDecision.NOT_ESTABLISHED
    assert SearchCoverage.parse(coverage.to_json()) == coverage


def test_partial_lane_preserves_simultaneous_unavailable_subset() -> None:
    coverage = _coverage(
        states=(CoverageState.PARTIAL, CoverageState.UNAVAILABLE),
        eligible=30,
        examined=12,
        matched=1,
        returned=1,
    )

    assert CoverageState.UNAVAILABLE in coverage.states
    assert coverage.absence_decision() is AbsenceDecision.EVIDENCE_FOUND

    with pytest.raises(RetrievalContractError, match="explicitly partial"):
        _coverage(
            states=(CoverageState.CAPPED, CoverageState.UNAVAILABLE),
            eligible=None,
            examined=0,
            limit=10,
        )


def test_permission_filter_is_an_absence_blocker_and_has_no_filtered_count() -> None:
    coverage = _coverage(
        states=(CoverageState.PARTIAL, CoverageState.PERMISSION_FILTERED),
        eligible=9,
        examined=9,
    )
    payload = coverage.to_payload()

    assert coverage.absence_decision() is AbsenceDecision.NOT_ESTABLISHED
    assert not ({"filtered_count", "unauthorized_count", "query", "cursor", "digest"} & payload.keys())
    with pytest.raises(RetrievalContractError, match="exclusive"):
        _coverage(states=(CoverageState.COMPLETE, CoverageState.PERMISSION_FILTERED))


def test_cursor_requires_explicit_cap_and_limit() -> None:
    with pytest.raises(RetrievalContractError, match="continuation"):
        _coverage(
            states=(CoverageState.PARTIAL, CoverageState.STALE),
            eligible=20,
            examined=10,
            cursor=True,
        )
    with pytest.raises(RetrievalContractError, match="applied limit"):
        _coverage(
            states=(CoverageState.PARTIAL, CoverageState.CAPPED),
            eligible=20,
            examined=10,
            cursor=True,
        )


def test_federated_absence_requires_every_requested_corpus_lane_pair() -> None:
    requested = (
        (SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL),
        (SearchCorpus.KNOWLEDGE, SearchLane.LEXICAL),
    )
    binding = _binding(requested)
    raw_lexical = _coverage(
        corpus=SearchCorpus.RAW_DOCUMENTS,
        lane=SearchLane.LEXICAL,
        binding=binding,
    )
    knowledge_lexical = _coverage(
        corpus=SearchCorpus.KNOWLEDGE,
        lane=SearchLane.LEXICAL,
        binding=binding,
    )

    assert (
        aggregate_absence_decision(
            [raw_lexical, knowledge_lexical],
            requested_targets=requested,
        )
        is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
    )
    assert (
        aggregate_absence_decision([raw_lexical], requested_targets=requested)
        is AbsenceDecision.NOT_ESTABLISHED
    )


def test_valid_found_evidence_wins_but_stale_dense_matches_do_not() -> None:
    found = _coverage(matched=2, returned=2)
    stale = _coverage(
        corpus=SearchCorpus.KNOWLEDGE,
        lane=SearchLane.DENSE,
        states=(CoverageState.PARTIAL, CoverageState.STALE),
        eligible=12,
        examined=12,
        matched=2,
        returned=2,
    )
    assert found.absence_decision() is AbsenceDecision.EVIDENCE_FOUND
    assert stale.absence_decision() is AbsenceDecision.NOT_ESTABLISHED


def test_aggregate_rejects_mixed_execution_batches() -> None:
    requested = (
        (SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL),
        (SearchCorpus.KNOWLEDGE, SearchLane.LEXICAL),
    )
    first = _binding(requested, run="run-one")
    second = _binding(requested, run="run-two")
    coverages = [
        _coverage(
            corpus=SearchCorpus.RAW_DOCUMENTS,
            lane=SearchLane.LEXICAL,
            binding=first,
        ),
        _coverage(
            corpus=SearchCorpus.KNOWLEDGE,
            lane=SearchLane.LEXICAL,
            binding=second,
        ),
    ]
    assert (
        aggregate_absence_decision(coverages, requested_targets=requested) is AbsenceDecision.NOT_ESTABLISHED
    )


def test_execution_binding_is_opaque_and_binds_every_private_discriminator() -> None:
    targets = ((SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL),)
    baseline = _binding(targets)
    variants = (
        _binding(targets, request='{"query":"other"}'),
        _binding(targets, tenant_id="tenant-other"),
        _binding(targets, principal_id="person-other"),
        _binding(targets, snapshot="snapshot-18"),
        _binding(targets, run="run-other"),
        _binding(((SearchCorpus.RAW_DOCUMENTS, SearchLane.DENSE),)),
    )
    assert all(item.opaque_handle != baseline.opaque_handle for item in variants)
    serialized = json.dumps(baseline.to_payload(), sort_keys=True)
    assert "quarterly report" not in serialized
    assert "tenant-main" not in serialized
    assert "person-42" not in serialized
    assert "snapshot-17" not in serialized
    assert "run-0123456789abcdef" not in serialized


def test_coverage_rejects_unrelated_execution_target() -> None:
    binding = _binding(((SearchCorpus.KNOWLEDGE, SearchLane.DENSE),))
    with pytest.raises(RetrievalContractError, match="absent from"):
        _coverage(binding=binding)


def test_contract_does_not_claim_dense_passage_citability() -> None:
    assert not hasattr(retrieval_contracts, "dense_passage_is_citable")


def test_coverage_json_rejects_open_state_and_process_token() -> None:
    payload = _coverage().to_payload()
    payload["cursor_token"] = "process-private"
    with pytest.raises(RetrievalContractError, match="closed contract"):
        SearchCoverage.parse(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    payload = _coverage().to_payload()
    payload["states"] = ["complete", "unknown"]
    with pytest.raises(RetrievalContractError, match="closed enum"):
        SearchCoverage.parse(json.dumps(payload, sort_keys=True, separators=(",", ":")))
