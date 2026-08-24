from __future__ import annotations

import copy
import itertools
import pickle
from dataclasses import replace
from typing import cast

import pytest

import friday.retrieval.archive_search_federation as federation_module
from friday.retrieval.archive_search_authority import (
    ArchiveSearchAuthorityPhase,
    ArchiveSearchCandidateReauthorization,
    ArchiveSearchCoverageReauthorization,
    ArchiveSearchRunBinding,
    authorize_archive_search_before_model,
    authorize_archive_search_resumed_before_model,
    canonical_archive_search_targets,
    create_archive_model_batch_ledger,
    create_archive_search_run_binding,
    issue_archive_search_continuation,
    redeem_archive_search_continuation,
)
from friday.retrieval.archive_search_contract import (
    ArchiveEvidenceAuthority,
    ArchiveMatchChannel,
    ArchiveMatchRank,
    ArchiveReviewState,
    ArchiveSearchCandidate,
    ArchiveSearchCorpus,
    ArchiveSearchPage,
    ArchiveSearchPassage,
    ArchiveSearchRequest,
    ArchiveSearchWarning,
)
from friday.retrieval.archive_search_federation import (
    ArchiveSearchFederationError,
    federate_archive_search,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    CoverageState,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    LifecycleRef,
    LifecycleState,
    PassageRef,
    RepresentationKind,
    ResolvedSource,
    RevalidationTarget,
    RevisionKind,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TextSpanLocator,
)

TENANT = "tenant-federation"
PRINCIPAL = "principal-federation"
SNAPSHOT = "snapshot-federation"
_RUNS = itertools.count(1)


def _allow_candidate(
    _phase: ArchiveSearchAuthorityPhase,
    _run: ArchiveSearchRunBinding,
    candidate: ArchiveSearchCandidate,
    _context: object,
) -> ArchiveSearchCandidateReauthorization:
    return ArchiveSearchCandidateReauthorization.authorized(candidate)


def _allow_coverage(
    _phase: ArchiveSearchAuthorityPhase,
    _run: ArchiveSearchRunBinding,
    coverage: SearchCoverage,
    _context: object,
) -> ArchiveSearchCoverageReauthorization:
    return ArchiveSearchCoverageReauthorization.authorized(coverage)


def _request(*, limit: int = 10) -> ArchiveSearchRequest:
    return ArchiveSearchRequest.create(
        query="QNAP Nextcloud",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=limit,
    )


def _binding(request: ArchiveSearchRequest, *, run: str | None = None) -> SearchExecutionBinding:
    return SearchExecutionBinding.create(
        normalized_private_request_json=request.to_identity_json(),
        authority_scope=AuthorityScope.TENANT_PRINCIPAL,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        requested_targets=canonical_archive_search_targets(request),
        snapshot_discriminator=SNAPSHOT,
        run_discriminator=run or f"run-{next(_RUNS)}",
        privacy_key=b"f" * 32,
    )


def _resolved(index: int, *, revision_digit: str | None = None) -> tuple[ResolvedSource, SourceRevision]:
    raw_id = f"raw_{index:016x}"
    source = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        TENANT,
        PRINCIPAL,
        CanonicalObjectKind.RAW_OBJECT,
        raw_id,
    )
    raw = SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id)
    knowledge = SourceRepresentation(RepresentationKind.KNOWLEDGE_OBJECT, f"ko_{index:016x}")
    revision = SourceRevision(
        raw,
        RevisionKind.RAW_CONTENT_SHA256,
        (revision_digit or f"{index % 16:x}") * 64,
    )
    resolved = ResolvedSource.create(
        source_ref=source,
        representations=(raw, knowledge),
        lifecycle=(
            LifecycleRef(raw, LifecycleState.ACTIVE),
            LifecycleRef(knowledge, LifecycleState.ACTIVE),
        ),
        revisions=(
            revision,
            SourceRevision(knowledge, RevisionKind.KNOWLEDGE_VERSION, "1"),
        ),
        revalidation_targets=(
            RevalidationTarget(raw, AuthorityScope.TENANT_PRINCIPAL),
            RevalidationTarget(knowledge, AuthorityScope.TENANT_PRINCIPAL),
        ),
    )
    return resolved, revision


def _candidate(
    index: int,
    channel: ArchiveMatchChannel,
    rank: int,
    *,
    navigation: bool = False,
    revision_digit: str | None = None,
    corpus: ArchiveSearchCorpus = ArchiveSearchCorpus.DOCUMENTS,
) -> ArchiveSearchCandidate:
    resolved, revision = _resolved(index, revision_digit=revision_digit)
    passages: tuple[ArchiveSearchPassage, ...] = ()
    if not navigation:
        passages = (
            ArchiveSearchPassage(
                PassageRef.from_resolved_source(
                    resolved,
                    source_revision=revision,
                    locator=TextSpanLocator(chunk_index=0, start_char=0, end_char=12),
                    passage_index_version="archive-federation-test-v1",
                    embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
                ),
                f"Excerpt {index}",
            ),
        )
    return ArchiveSearchCandidate.create(
        corpus=corpus,
        resolved_source=resolved,
        title=f"Document {index}",
        filename=f"document-{index}.md",
        review_state=ArchiveReviewState.CONFIRMED,
        evidence_authority=(
            ArchiveEvidenceAuthority.NAVIGATION_ONLY if navigation else ArchiveEvidenceAuthority.CANONICAL
        ),
        lifecycle_state=LifecycleState.ACTIVE,
        matches=(ArchiveMatchRank(channel, rank),),
        passages=passages,
    )


def _principal_navigation_candidate(
    corpus: ArchiveSearchCorpus,
    index: int,
    channel: ArchiveMatchChannel,
    rank: int,
) -> ArchiveSearchCandidate:
    if corpus is ArchiveSearchCorpus.MESSAGES:
        object_id = f"conv_{index:016x}"
        source_kind = SourceKind.CONVERSATION
        object_kind = CanonicalObjectKind.CONVERSATION
        representation_kind = RepresentationKind.CONVERSATION
        revision_kind = RevisionKind.MESSAGE_LEDGER_SHA256
    else:
        assert corpus is ArchiveSearchCorpus.OBSIDIAN
        object_id = f"obsbind_{index:016x}"
        source_kind = SourceKind.OBSIDIAN_NOTE
        object_kind = CanonicalObjectKind.OBSIDIAN_BINDING
        representation_kind = RepresentationKind.OBSIDIAN_BINDING
        revision_kind = RevisionKind.OBSIDIAN_REVISION_SHA256
    source = SourceRef(
        source_kind,
        AuthorityScope.PRINCIPAL,
        None,
        PRINCIPAL,
        object_kind,
        object_id,
    )
    representation = SourceRepresentation(representation_kind, object_id)
    resolved = ResolvedSource.create(
        source_ref=source,
        representations=(representation,),
        lifecycle=(LifecycleRef(representation, LifecycleState.ACTIVE),),
        revisions=(SourceRevision(representation, revision_kind, f"{index % 16:x}" * 64),),
        revalidation_targets=(RevalidationTarget(representation, AuthorityScope.PRINCIPAL),),
    )
    return ArchiveSearchCandidate.create(
        corpus=corpus,
        resolved_source=resolved,
        title=f"Private source {index}",
        review_state=ArchiveReviewState.NOT_APPLICABLE,
        evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
        lifecycle_state=LifecycleState.ACTIVE,
        matches=(ArchiveMatchRank(channel, rank),),
    )


def _lanes(
    request: ArchiveSearchRequest,
    **values: tuple[ArchiveSearchCandidate, ...],
) -> dict[tuple[SearchCorpus, SearchLane], tuple[ArchiveSearchCandidate, ...]]:
    selected = {SearchLane(name): candidates for name, candidates in values.items()}
    return {target: selected.get(target[1], ()) for target in canonical_archive_search_targets(request)}


def _coverage(
    binding: SearchExecutionBinding,
    lanes: dict[tuple[SearchCorpus, SearchLane], tuple[ArchiveSearchCandidate, ...]],
    *,
    states: dict[SearchLane, tuple[CoverageState, ...]] | None = None,
) -> tuple[SearchCoverage, ...]:
    result: list[SearchCoverage] = []
    for target in binding.requested_targets:
        count = len(lanes[target])
        target_states = (states or {}).get(target[1], (CoverageState.COMPLETE,))
        capped = CoverageState.CAPPED in target_states
        result.append(
            SearchCoverage.create(
                corpus=target[0],
                lane=target[1],
                execution_binding=binding,
                states=target_states,
                eligible_authorized=count,
                examined=count,
                matched_at_least=count,
                returned=count,
                authority_rechecked=True,
                snapshot_current=True,
                limit=count or 1 if capped else None,
                next_cursor_available=False,
            )
        )
    return tuple(result)


def _federate(
    request: ArchiveSearchRequest,
    binding: SearchExecutionBinding,
    lanes: dict[tuple[SearchCorpus, SearchLane], tuple[ArchiveSearchCandidate, ...]],
):
    return federate_archive_search(
        request=request,
        execution_binding=binding,
        coverage=_coverage(binding, lanes),
        candidates_by_target=lanes,
    )


def test_merges_exact_source_across_channels_and_factual_beats_navigation() -> None:
    request = _request()
    binding = _binding(request)
    lanes = _lanes(
        request,
        exact_identity=(_candidate(1, ArchiveMatchChannel.EXACT_IDENTITY, 1, navigation=True),),
        lexical=(_candidate(1, ArchiveMatchChannel.LEXICAL, 1),),
    )

    result = _federate(request, binding, lanes)

    assert len(result.candidates) == 1
    merged = result.candidates[0]
    assert merged.evidence_authority is ArchiveEvidenceAuthority.CANONICAL
    assert merged.match_channels == (
        ArchiveMatchChannel.EXACT_IDENTITY,
        ArchiveMatchChannel.LEXICAL,
    )
    assert len(merged.passages) == 1
    assert result.tail_candidates == ()
    assert not any(item.next_cursor_available for item in result.coverage)


def test_same_corpus_merge_tolerates_deterministic_display_metadata() -> None:
    request = _request()
    binding = _binding(request)
    navigation = replace(
        _candidate(1, ArchiveMatchChannel.CATALOG, 1, navigation=True),
        title="Zeta",
        filename=None,
    )
    factual = replace(
        _candidate(1, ArchiveMatchChannel.LEXICAL, 1),
        title="Alpha",
        filename="source.md",
    )
    lanes = _lanes(request, catalog=(navigation,), lexical=(factual,))

    result = _federate(request, binding, lanes)

    assert len(result.candidates) == 1
    assert result.candidates[0].title == "Alpha"
    assert result.candidates[0].filename == "source.md"


def test_cross_corpus_source_is_globally_deduplicated_without_false_coverage() -> None:
    request = ArchiveSearchRequest.create(
        query="same stable source",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE),
    )
    binding = _binding(request)
    lanes = _lanes(request)
    lanes[(SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL)] = (_candidate(1, ArchiveMatchChannel.LEXICAL, 1),)
    lanes[(SearchCorpus.KNOWLEDGE, SearchLane.LEXICAL)] = (
        _candidate(
            1,
            ArchiveMatchChannel.LEXICAL,
            1,
            corpus=ArchiveSearchCorpus.KNOWLEDGE,
        ),
    )

    result = _federate(request, binding, lanes)
    coverage = {(item.corpus, item.lane): item for item in result.coverage}

    assert len(result.candidates) == 1
    assert result.candidates[0].corpus is ArchiveSearchCorpus.KNOWLEDGE
    suppressed = coverage[(SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL)]
    assert suppressed.returned == 0
    assert set(suppressed.states) == {CoverageState.CAPPED, CoverageState.PARTIAL}
    assert not suppressed.next_cursor_available
    assert set(result.warnings) == {
        ArchiveSearchWarning.CONTINUATION_UNAVAILABLE,
        ArchiveSearchWarning.LANE_CAPPED,
    }


def test_suppressed_cross_corpus_target_never_borrows_an_unrelated_cursor() -> None:
    request = ArchiveSearchRequest.create(
        query="same stable source",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE),
        limit=1,
    )
    binding = _binding(request)
    lanes = _lanes(request)
    lanes[(SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL)] = (_candidate(1, ArchiveMatchChannel.LEXICAL, 1),)
    lanes[(SearchCorpus.KNOWLEDGE, SearchLane.LEXICAL)] = (
        _candidate(
            1,
            ArchiveMatchChannel.LEXICAL,
            1,
            corpus=ArchiveSearchCorpus.KNOWLEDGE,
        ),
        _candidate(
            2,
            ArchiveMatchChannel.LEXICAL,
            2,
            corpus=ArchiveSearchCorpus.KNOWLEDGE,
        ),
    )

    result = _federate(request, binding, lanes)
    coverage = {(item.corpus, item.lane): item for item in result.coverage}

    assert result.continuation_available
    assert coverage[(SearchCorpus.KNOWLEDGE, SearchLane.LEXICAL)].next_cursor_available
    assert not coverage[(SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL)].next_cursor_available


def test_cross_corpus_suppression_keeps_the_real_tail_issuable() -> None:
    request = ArchiveSearchRequest.create(
        query="same stable source",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE),
        limit=1,
    )
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"federation-turn-{next(_RUNS)}",
    )
    run = create_archive_search_run_binding(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        request=request,
        requested_targets=canonical_archive_search_targets(request),
        snapshot_discriminator=SNAPSHOT,
        run_discriminator=f"authority-run-{next(_RUNS)}",
        turn_ledger=ledger,
    )
    lanes = _lanes(request)
    lanes[(SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL)] = (_candidate(1, ArchiveMatchChannel.LEXICAL, 1),)
    lanes[(SearchCorpus.KNOWLEDGE, SearchLane.LEXICAL)] = (
        _candidate(
            1,
            ArchiveMatchChannel.LEXICAL,
            1,
            corpus=ArchiveSearchCorpus.KNOWLEDGE,
        ),
        _candidate(
            2,
            ArchiveMatchChannel.LEXICAL,
            2,
            corpus=ArchiveSearchCorpus.KNOWLEDGE,
        ),
    )
    result = federate_archive_search(
        request=request,
        execution_binding=run.execution_binding,
        coverage=_coverage(run.execution_binding, lanes),
        candidates_by_target=lanes,
    )

    issued = issue_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=run,
        tail_candidates=result.tail_candidates,
        terminal_coverage=result.terminal_coverage,
        warnings=result.warnings,
    )

    first = authorize_archive_search_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=run,
        candidates=result.candidates,
        coverage=result.coverage,
        warnings=result.warnings,
        continuation=issued,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=None,
    )
    token = first.public_tool_result_payload["continuation"]
    assert type(token) is str
    resumed_request = ArchiveSearchRequest.create(
        query="same stable source",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE),
        limit=1,
        continuation=token,
    )
    resumed_run = create_archive_search_run_binding(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        request=resumed_request,
        requested_targets=canonical_archive_search_targets(resumed_request),
        snapshot_discriminator=SNAPSHOT,
        run_discriminator=f"authority-run-{next(_RUNS)}",
        turn_ledger=ledger,
    )
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed_run,
    )
    resumed = authorize_archive_search_resumed_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed_run,
        redemption=redemption,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=None,
    )
    resumed_payload = resumed.public_tool_result_payload
    resumed_coverage = {
        (item["corpus"], item["lane"]): item
        for item in cast(list[dict[str, object]], resumed_payload["coverage"])
    }
    suppressed = resumed_coverage[("raw_documents", "lexical")]
    assert suppressed["returned"] == 0
    assert suppressed["states"] == ["capped", "partial"]
    assert suppressed["next_cursor_available"] is False
    assert "continuation_unavailable" in cast(list[str], resumed_payload["warnings"])


def test_cross_corpus_suppression_composes_with_same_lane_internal_overfetch() -> None:
    request = ArchiveSearchRequest.create(
        query="same stable source",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE),
        limit=1,
    )
    binding = _binding(request)
    lanes = _lanes(request)
    lanes[(SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL)] = (
        _candidate(1, ArchiveMatchChannel.LEXICAL, 1),
        _candidate(2, ArchiveMatchChannel.LEXICAL, 2),
        _candidate(3, ArchiveMatchChannel.LEXICAL, 3),
    )
    lanes[(SearchCorpus.KNOWLEDGE, SearchLane.LEXICAL)] = (
        _candidate(
            1,
            ArchiveMatchChannel.LEXICAL,
            1,
            corpus=ArchiveSearchCorpus.KNOWLEDGE,
        ),
    )

    result = _federate(request, binding, lanes)
    terminal = {(item.corpus, item.lane): item for item in result.terminal_coverage}
    raw = terminal[(SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL)]

    assert len(result.candidates) == 1
    assert len(result.tail_candidates) == 2
    assert raw.returned == raw.limit == 2
    assert set(raw.states) == {CoverageState.CAPPED, CoverageState.PARTIAL}


def test_global_order_is_deterministic_and_uses_only_integer_rank_keys() -> None:
    request = _request()
    binding = _binding(request)
    lanes = _lanes(
        request,
        lexical=(
            _candidate(1, ArchiveMatchChannel.LEXICAL, 1),
            _candidate(3, ArchiveMatchChannel.LEXICAL, 2),
        ),
        exact_identity=(_candidate(2, ArchiveMatchChannel.EXACT_IDENTITY, 1),),
    )
    reverse_inserted = dict(reversed(tuple(lanes.items())))

    first = _federate(request, binding, lanes)
    second = _federate(request, binding, reverse_inserted)

    expected = tuple(item.resolved_source.source_ref for item in first.candidates)
    assert expected == tuple(item.resolved_source.source_ref for item in second.candidates)
    assert [item.title for item in first.candidates] == ["Document 2", "Document 1", "Document 3"]


def test_real_tail_marks_only_affected_targets_and_terminal_is_cursor_free() -> None:
    request = _request(limit=1)
    binding = _binding(request)
    lanes = _lanes(
        request,
        exact_identity=(_candidate(1, ArchiveMatchChannel.EXACT_IDENTITY, 1),),
        lexical=(_candidate(2, ArchiveMatchChannel.LEXICAL, 1),),
    )

    result = _federate(request, binding, lanes)
    page = {(item.corpus, item.lane): item for item in result.coverage}

    assert len(result.candidates) == len(result.tail_candidates) == 1
    assert page[(SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL)].next_cursor_available
    assert set(page[(SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL)].states) == {
        CoverageState.CAPPED,
        CoverageState.PARTIAL,
    }
    assert page[(SearchCorpus.RAW_DOCUMENTS, SearchLane.EXACT_IDENTITY)].states == (CoverageState.COMPLETE,)
    assert not any(item.next_cursor_available for item in result.terminal_coverage)
    assert result.warnings == (ArchiveSearchWarning.LANE_CAPPED,)


def test_single_lane_can_materialize_a_real_tail_beyond_public_limit() -> None:
    request = _request(limit=1)
    binding = _binding(request)
    lanes = _lanes(
        request,
        lexical=(
            _candidate(1, ArchiveMatchChannel.LEXICAL, 1),
            _candidate(2, ArchiveMatchChannel.LEXICAL, 2),
        ),
    )

    result = _federate(request, binding, lanes)

    assert len(result.candidates) == len(result.tail_candidates) == 1
    assert result.continuation_available


def test_byte_aware_first_page_stays_within_real_public_envelope() -> None:
    request = _request(limit=10)
    binding = _binding(request)
    candidates: list[ArchiveSearchCandidate] = []
    for index in range(1, 11):
        candidate = _candidate(index, ArchiveMatchChannel.LEXICAL, index)
        passage = candidate.passages[0]
        candidates.append(
            replace(
                candidate,
                passages=(ArchiveSearchPassage(passage.passage_ref, "x" * 720),),
            )
        )
    lanes = _lanes(request, lexical=tuple(candidates))

    result = _federate(request, binding, lanes)
    public = ArchiveSearchPage.create(
        request=request,
        candidates=result.candidates,
        coverage=result.coverage,
        warnings=result.warnings,
        continuation="A" * 43,
    ).to_public_json(b"p" * 32)

    assert len(result.candidates) < request.limit
    assert result.tail_candidates
    assert len(public.encode("ascii")) <= 7_900


def test_false_complete_fullness_is_rejected() -> None:
    request = _request()
    binding = _binding(request)
    lanes = _lanes(request, lexical=(_candidate(1, ArchiveMatchChannel.LEXICAL, 1),))
    dishonest = tuple(
        replace(item, eligible_authorized=2, examined=2, matched_at_least=2)
        if item.lane is SearchLane.LEXICAL
        else item
        for item in _coverage(binding, lanes)
    )

    with pytest.raises(ArchiveSearchFederationError):
        federate_archive_search(
            request=request,
            execution_binding=binding,
            coverage=dishonest,
            candidates_by_target=lanes,
        )


def test_duplicate_source_drift_fails_closed() -> None:
    request = _request()
    binding = _binding(request)
    lanes = _lanes(
        request,
        exact_identity=(_candidate(1, ArchiveMatchChannel.EXACT_IDENTITY, 1),),
        lexical=(_candidate(1, ArchiveMatchChannel.LEXICAL, 1, revision_digit="f"),),
    )

    with pytest.raises(ArchiveSearchFederationError, match="federation failed"):
        _federate(request, binding, lanes)


def test_cross_binding_and_request_fail_closed() -> None:
    request = _request()
    first = _binding(request)
    second = _binding(request)
    lanes = _lanes(request, lexical=(_candidate(1, ArchiveMatchChannel.LEXICAL, 1),))

    with pytest.raises(ArchiveSearchFederationError):
        federate_archive_search(
            request=request,
            execution_binding=second,
            coverage=_coverage(first, lanes),
            candidates_by_target=lanes,
        )
    other_request = ArchiveSearchRequest.create(
        query="different private query",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
    )
    with pytest.raises(ArchiveSearchFederationError):
        federate_archive_search(
            request=other_request,
            execution_binding=first,
            coverage=_coverage(first, lanes),
            candidates_by_target=lanes,
        )


@pytest.mark.parametrize("failure", ["missing_target", "wrong_rank", "wrong_corpus", "fake_returned"])
def test_lane_target_rank_and_count_invariants_fail_closed(failure: str) -> None:
    request = _request()
    binding = _binding(request)
    candidate = _candidate(1, ArchiveMatchChannel.LEXICAL, 1)
    lanes = _lanes(request, lexical=(candidate,))
    coverage = _coverage(binding, lanes)
    if failure == "missing_target":
        del lanes[next(iter(lanes))]
    elif failure == "wrong_rank":
        lanes[(SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL)] = (
            replace(candidate, matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 2),)),
        )
    elif failure == "wrong_corpus":
        lanes[(SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG)] = (candidate,)
    else:
        coverage = tuple(
            replace(item, returned=0) if item.lane is SearchLane.LEXICAL else item for item in coverage
        )

    with pytest.raises(ArchiveSearchFederationError):
        federate_archive_search(
            request=request,
            execution_binding=binding,
            coverage=coverage,
            candidates_by_target=lanes,
        )


def test_merged_only_head_does_not_invent_a_tail() -> None:
    request = _request(limit=1)
    binding = _binding(request)
    lanes = _lanes(
        request,
        exact_identity=(_candidate(1, ArchiveMatchChannel.EXACT_IDENTITY, 1),),
        lexical=(_candidate(1, ArchiveMatchChannel.LEXICAL, 1),),
    )

    result = _federate(request, binding, lanes)

    assert len(result.candidates) == 1
    assert result.tail_candidates == ()
    assert not result.continuation_available
    assert not any(item.next_cursor_available for item in result.coverage)


def test_oversized_tail_is_not_truncated_into_a_false_continuation(monkeypatch) -> None:
    monkeypatch.setattr(federation_module, "ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL", 1)
    request = _request(limit=1)
    binding = _binding(request)
    lanes = _lanes(
        request,
        exact_identity=(_candidate(1, ArchiveMatchChannel.EXACT_IDENTITY, 1),),
        lexical=(_candidate(2, ArchiveMatchChannel.LEXICAL, 1),),
        catalog=(_candidate(3, ArchiveMatchChannel.CATALOG, 1),),
    )

    result = _federate(request, binding, lanes)

    assert result.tail_candidates == ()
    assert not any(item.next_cursor_available for item in result.coverage)
    assert set(result.warnings) == {
        ArchiveSearchWarning.CONTINUATION_UNAVAILABLE,
        ArchiveSearchWarning.LANE_CAPPED,
    }
    lexical = next(item for item in result.coverage if item.lane is SearchLane.LEXICAL)
    assert set(lexical.states) == {CoverageState.CAPPED, CoverageState.PARTIAL}


def test_real_authority_bound_rejects_an_actual_oversized_tail() -> None:
    request = ArchiveSearchRequest.create(
        query="large private federation",
        corpora=(
            ArchiveSearchCorpus.DOCUMENTS,
            ArchiveSearchCorpus.KNOWLEDGE,
            ArchiveSearchCorpus.MESSAGES,
            ArchiveSearchCorpus.OBSIDIAN,
        ),
        limit=20,
    )
    binding = _binding(request)
    lanes: dict[
        tuple[SearchCorpus, SearchLane],
        tuple[ArchiveSearchCandidate, ...],
    ] = {target: () for target in canonical_archive_search_targets(request)}
    sequence = itertools.count(10_000)
    for target in tuple(lanes):
        channel = ArchiveMatchChannel(target[1].value)
        candidates: list[ArchiveSearchCandidate] = []
        for rank in range(1, request.limit + 1):
            index = next(sequence)
            if target[0] is SearchCorpus.RAW_DOCUMENTS:
                candidate = _candidate(index, channel, rank, navigation=True)
            elif target[0] is SearchCorpus.KNOWLEDGE:
                candidate = _candidate(
                    index,
                    channel,
                    rank,
                    navigation=True,
                    corpus=ArchiveSearchCorpus.KNOWLEDGE,
                )
            elif target[0] is SearchCorpus.CONVERSATION:
                candidate = _principal_navigation_candidate(
                    ArchiveSearchCorpus.MESSAGES,
                    index,
                    channel,
                    rank,
                )
            else:
                candidate = _principal_navigation_candidate(
                    ArchiveSearchCorpus.OBSIDIAN,
                    index,
                    channel,
                    rank,
                )
            candidates.append(candidate)
        lanes[target] = tuple(candidates)

    result = _federate(request, binding, lanes)

    assert len(lanes) * request.limit - request.limit > 256
    assert 0 < len(result.candidates) <= request.limit
    assert result.tail_candidates == ()
    assert not result.continuation_available
    assert not any(item.next_cursor_available for item in result.coverage)
    assert ArchiveSearchWarning.CONTINUATION_UNAVAILABLE in result.warnings


def test_preserves_existing_degradation_and_derives_closed_warnings() -> None:
    request = _request()
    binding = _binding(request)
    lanes = _lanes(request)
    states: dict[SearchLane, tuple[CoverageState, ...]] = {
        SearchLane.DENSE: (
            CoverageState.EMBEDDING_INCOMPATIBLE,
            CoverageState.PARTIAL,
        ),
        SearchLane.LEXICAL: (
            CoverageState.BACKFILL_PENDING,
            CoverageState.PARTIAL,
        ),
    }
    coverage = _coverage(binding, lanes, states=states)

    result = federate_archive_search(
        request=request,
        execution_binding=binding,
        coverage=coverage,
        candidates_by_target=lanes,
    )

    assert set(result.warnings) == {
        ArchiveSearchWarning.BACKFILL_PENDING,
        ArchiveSearchWarning.LANE_UNAVAILABLE,
    }


def test_terminal_coverage_can_issue_authority_continuation() -> None:
    request = _request(limit=1)
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"federation-turn-{next(_RUNS)}",
    )
    run = create_archive_search_run_binding(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        request=request,
        requested_targets=canonical_archive_search_targets(request),
        snapshot_discriminator=SNAPSHOT,
        run_discriminator=f"authority-run-{next(_RUNS)}",
        turn_ledger=ledger,
    )
    lanes = _lanes(
        request,
        exact_identity=(_candidate(1, ArchiveMatchChannel.EXACT_IDENTITY, 1),),
        lexical=(_candidate(2, ArchiveMatchChannel.LEXICAL, 1),),
    )
    result = federate_archive_search(
        request=request,
        execution_binding=run.execution_binding,
        coverage=_coverage(run.execution_binding, lanes),
        candidates_by_target=lanes,
    )

    issued = issue_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=run,
        tail_candidates=result.tail_candidates,
        terminal_coverage=result.terminal_coverage,
        warnings=result.warnings,
    )

    assert issued is not None


def test_result_is_private_sealed_and_detects_nested_tampering() -> None:
    request = _request()
    binding = _binding(request)
    lanes = _lanes(request, lexical=(_candidate(1, ArchiveMatchChannel.LEXICAL, 1),))
    result = _federate(request, binding, lanes)

    with pytest.raises(TypeError):
        copy.copy(result)
    with pytest.raises(TypeError):
        copy.deepcopy(result)
    with pytest.raises(TypeError):
        pickle.dumps(result)
    object.__setattr__(result.candidates[0], "title", "tampered")
    with pytest.raises(ArchiveSearchFederationError):
        _ = result.coverage


def test_seal_rejects_duck_typed_nested_values_with_identical_payloads() -> None:
    request = _request()
    binding = _binding(request)
    lanes = _lanes(request, lexical=(_candidate(1, ArchiveMatchChannel.LEXICAL, 1),))
    result = _federate(request, binding, lanes)

    class ForgedMatch:
        channel = ArchiveMatchChannel.DENSE
        rank = 1

        @staticmethod
        def to_private_payload() -> dict[str, object]:
            return {"channel": "lexical", "rank": 1}

    object.__setattr__(result._head[0], "matches", (ForgedMatch(),))

    with pytest.raises(ArchiveSearchFederationError):
        _ = result.candidates


def test_seal_rejects_noncanonical_nested_collection_types() -> None:
    request = _request()
    binding = _binding(request)
    lanes = _lanes(request, lexical=(_candidate(1, ArchiveMatchChannel.LEXICAL, 1),))
    result = _federate(request, binding, lanes)
    object.__setattr__(result._coverage[0], "states", list(result._coverage[0].states))

    with pytest.raises(ArchiveSearchFederationError):
        _ = result.coverage
