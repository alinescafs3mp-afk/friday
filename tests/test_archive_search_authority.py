from __future__ import annotations

import copy
import hashlib
import itertools
import json
import pickle
import re
import threading
from dataclasses import replace
from typing import cast

import pytest

import friday.retrieval.archive_search_authority as authority_module
from friday.orchestration.archive_recall_outcome import (
    ArchiveRecallStatus,
    archive_recall_outcome_from_attestation,
)
from friday.retrieval.archive_evidence_snapshot import (
    archive_selected_evidence_snapshot_sha256,
)
from friday.retrieval.archive_search_authority import (
    ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES,
    ARCHIVE_AUTHORITY_MAX_MODEL_BYTES,
    ARCHIVE_SEARCH_ACCEPTED_CANDIDATE_PROJECTION_SCHEMA,
    ARCHIVE_SEARCH_CANDIDATE_PROJECTION_ENTRY_SCHEMA,
    ArchiveModelBatchLedger,
    ArchiveSearchAcceptedCandidateProjection,
    ArchiveSearchAuthorityError,
    ArchiveSearchAuthorityPhase,
    ArchiveSearchCandidateReauthorization,
    ArchiveSearchCoverageGrade,
    ArchiveSearchCoverageReauthorization,
    ArchiveSearchPublicationDenialReason,
    ArchiveSearchPublicationDenied,
    ArchiveSearchReauthorizationStatus,
    ArchiveSearchRunBinding,
    ArchiveSearchSelectedEvidence,
    AuthorizedArchiveBatch,
    IssuedArchiveContinuation,
    RedeemedArchiveContinuation,
    abandon_empty_archive_model_batch_ledger,
    attest_archive_search_before_publication,
    authorize_archive_search_before_model,
    authorize_archive_search_resumed_before_model,
    canonical_archive_search_targets,
    create_archive_model_batch_ledger,
    create_archive_search_run_binding,
    issue_archive_search_continuation,
    preview_archive_search_candidate_projection_labels,
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

TENANT = "tenant-private-authority"
PRINCIPAL = "principal-private-authority"
QUERY = "private QNAP Nextcloud material"
RAW_ONE = "raw_1111111111111111"
RAW_TWO = "raw_2222222222222222"
TARGETS = (
    (SearchCorpus.RAW_DOCUMENTS, SearchLane.APPROXIMATE_IDENTITY),
    (SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG),
    (SearchCorpus.RAW_DOCUMENTS, SearchLane.DENSE),
    (SearchCorpus.RAW_DOCUMENTS, SearchLane.EXACT_IDENTITY),
    (SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL),
)
CONTEXT = object()
_TURN_SEQUENCE = itertools.count(1)


def _request(*, continuation: str | None = None, limit: int = 20) -> ArchiveSearchRequest:
    return ArchiveSearchRequest.create(
        query=QUERY,
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=limit,
        continuation=continuation,
    )


def _run(
    *,
    targets: tuple[tuple[SearchCorpus, SearchLane], ...] = TARGETS,
    principal_id: str = PRINCIPAL,
    run: str = "run-private-1",
    request: ArchiveSearchRequest | None = None,
    ledger: ArchiveModelBatchLedger | None = None,
) -> ArchiveSearchRunBinding:
    selected_ledger = (
        create_archive_model_batch_ledger(
            tenant_id=TENANT,
            principal_id=principal_id,
            turn_discriminator=f"test-turn-{next(_TURN_SEQUENCE)}",
        )
        if ledger is None
        else ledger
    )
    return create_archive_search_run_binding(
        tenant_id=TENANT,
        principal_id=principal_id,
        request=_request() if request is None else request,
        requested_targets=targets,
        snapshot_discriminator="snapshot-private-1",
        run_discriminator=run,
        turn_ledger=selected_ledger,
    )


def _candidate(
    raw_id: str,
    channel: ArchiveMatchChannel,
    *,
    title: str,
    principal_id: str = PRINCIPAL,
) -> ArchiveSearchCandidate:
    source = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        TENANT,
        principal_id,
        CanonicalObjectKind.RAW_OBJECT,
        raw_id,
    )
    representation = SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id)
    knowledge = SourceRepresentation(RepresentationKind.KNOWLEDGE_OBJECT, raw_id.replace("raw_", "ko_"))
    revision = SourceRevision(
        representation,
        RevisionKind.RAW_CONTENT_SHA256,
        ("1" if raw_id == RAW_ONE else "2") * 64,
    )
    knowledge_revision = SourceRevision(
        knowledge,
        RevisionKind.KNOWLEDGE_VERSION,
        "1",
    )
    resolved = ResolvedSource.create(
        source_ref=source,
        representations=(representation, knowledge),
        lifecycle=(
            LifecycleRef(representation, LifecycleState.ACTIVE),
            LifecycleRef(knowledge, LifecycleState.ACTIVE),
        ),
        revisions=(revision, knowledge_revision),
        revalidation_targets=(
            RevalidationTarget(representation, AuthorityScope.TENANT_PRINCIPAL),
            RevalidationTarget(knowledge, AuthorityScope.TENANT_PRINCIPAL),
        ),
    )
    passage = ArchiveSearchPassage(
        PassageRef.from_resolved_source(
            resolved,
            source_revision=revision,
            locator=TextSpanLocator(chunk_index=0, start_char=0, end_char=24),
            passage_index_version="archive-authority-v1",
            embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
        ),
        f"Model-visible excerpt {title}",
    )
    return ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        resolved_source=resolved,
        review_state=ArchiveReviewState.CONFIRMED,
        evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
        lifecycle_state=LifecycleState.ACTIVE,
        matches=(ArchiveMatchRank(channel, 1),),
        title=title,
        filename=f"{title.casefold()}.md",
        passages=(passage,),
    )


def _candidates() -> tuple[ArchiveSearchCandidate, ...]:
    return (
        _candidate(RAW_ONE, ArchiveMatchChannel.LEXICAL, title="One"),
        _candidate(RAW_TWO, ArchiveMatchChannel.DENSE, title="Two"),
    )


def _large_candidates(count: int, *, excerpt_chars: int = 1_800) -> tuple[ArchiveSearchCandidate, ...]:
    result: list[ArchiveSearchCandidate] = []
    for index in range(1, count + 1):
        candidate = _candidate(
            f"raw_{index:016d}",
            ArchiveMatchChannel.LEXICAL,
            title=f"Large {index}",
        )
        passage = replace(candidate.passages[0], excerpt=str(index) * excerpt_chars)
        result.append(
            replace(
                candidate,
                matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, index),),
                passages=(passage,),
            )
        )
    return tuple(result)


def _oversized_candidate() -> ArchiveSearchCandidate:
    candidate = _candidate(
        "raw_0000000000000001",
        ArchiveMatchChannel.LEXICAL,
        title="Cannot fit",
    )
    original = candidate.passages[0]
    passages = tuple(
        ArchiveSearchPassage(
            replace(
                original.passage_ref,
                locator=TextSpanLocator(
                    chunk_index=index,
                    start_char=index * 30,
                    end_char=index * 30 + 24,
                ),
            ),
            "X" * 1_900,
        )
        for index in range(4)
    )
    return replace(candidate, passages=passages)


def _coverage(
    run: ArchiveSearchRunBinding,
    *,
    zero: bool = False,
) -> tuple[SearchCoverage, ...]:
    result: list[SearchCoverage] = []
    for corpus, lane in run.execution_binding.requested_targets:
        matched = 0 if zero or lane not in {SearchLane.DENSE, SearchLane.LEXICAL} else 1
        result.append(
            SearchCoverage.create(
                corpus=corpus,
                lane=lane,
                execution_binding=run.execution_binding,
                states=(CoverageState.COMPLETE,),
                eligible_authorized=matched,
                examined=matched,
                matched_at_least=matched,
                returned=matched,
                authority_rechecked=True,
                snapshot_current=True,
            )
        )
    return tuple(result)


def _continuing_coverage(
    run: ArchiveSearchRunBinding,
    terminal: tuple[SearchCoverage, ...],
    tail: tuple[ArchiveSearchCandidate, ...],
) -> tuple[SearchCoverage, ...]:
    continued_targets = {
        (SearchCorpus.RAW_DOCUMENTS, match.channel.search_lane)
        for candidate in tail
        for match in candidate.matches
    }
    return tuple(
        SearchCoverage.create(
            corpus=item.corpus,
            lane=item.lane,
            execution_binding=run.execution_binding,
            states=(CoverageState.PARTIAL, CoverageState.CAPPED)
            if (item.corpus, item.lane) in continued_targets
            else item.states,
            eligible_authorized=item.eligible_authorized,
            examined=item.examined,
            matched_at_least=item.matched_at_least,
            returned=0,
            authority_rechecked=item.authority_rechecked,
            snapshot_current=item.snapshot_current,
            limit=20 if (item.corpus, item.lane) in continued_targets else item.limit,
            next_cursor_available=(item.corpus, item.lane) in continued_targets,
        )
        for item in terminal
    )


def _terminal_coverage_for_tail(
    run: ArchiveSearchRunBinding,
    tail: tuple[ArchiveSearchCandidate, ...],
) -> tuple[SearchCoverage, ...]:
    counts: dict[tuple[SearchCorpus, SearchLane], int] = {}
    for candidate in tail:
        for match in candidate.matches:
            target = SearchCorpus.RAW_DOCUMENTS, match.channel.search_lane
            counts[target] = counts.get(target, 0) + 1
    return tuple(
        SearchCoverage.create(
            corpus=corpus,
            lane=lane,
            execution_binding=run.execution_binding,
            states=(CoverageState.COMPLETE,),
            eligible_authorized=counts.get((corpus, lane), 0),
            examined=counts.get((corpus, lane), 0),
            matched_at_least=counts.get((corpus, lane), 0),
            returned=counts.get((corpus, lane), 0),
            authority_rechecked=True,
            snapshot_current=True,
        )
        for corpus, lane in run.execution_binding.requested_targets
    )


def _issue_public_continuation(
    run: ArchiveSearchRunBinding,
    *,
    tail: tuple[ArchiveSearchCandidate, ...] | None = None,
) -> tuple[str, IssuedArchiveContinuation]:
    selected_tail = _candidates() if tail is None else tail
    terminal = _terminal_coverage_for_tail(run, selected_tail)
    issued = issue_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=run,
        tail_candidates=selected_tail,
        terminal_coverage=terminal,
    )
    batch = authorize_archive_search_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=run,
        candidates=(),
        coverage=_continuing_coverage(run, terminal, selected_tail),
        continuation=issued,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )
    token = batch.public_tool_result_payload["continuation"]
    assert type(token) is str
    return token, issued


def _allow_candidate(
    _phase: ArchiveSearchAuthorityPhase,
    _run_binding: ArchiveSearchRunBinding,
    candidate: ArchiveSearchCandidate,
    _context: object,
) -> ArchiveSearchCandidateReauthorization:
    return ArchiveSearchCandidateReauthorization.authorized(candidate)


def _allow_coverage(
    _phase: ArchiveSearchAuthorityPhase,
    _run_binding: ArchiveSearchRunBinding,
    coverage: SearchCoverage,
    _context: object,
) -> ArchiveSearchCoverageReauthorization:
    return ArchiveSearchCoverageReauthorization.authorized(coverage)


def _batch(
    run: ArchiveSearchRunBinding,
    *,
    candidates: tuple[ArchiveSearchCandidate, ...] | None = None,
    coverage: tuple[SearchCoverage, ...] | None = None,
    candidate_reauthorizer=_allow_candidate,
    coverage_reauthorizer=_allow_coverage,
) -> AuthorizedArchiveBatch:
    return authorize_archive_search_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=run,
        candidates=_candidates() if candidates is None else candidates,
        coverage=_coverage(run) if coverage is None else coverage,
        candidate_reauthorizer=candidate_reauthorizer,
        coverage_reauthorizer=coverage_reauthorizer,
        authority_context=CONTEXT,
    )


def _attest(
    run: ArchiveSearchRunBinding,
    batch: AuthorizedArchiveBatch,
    *,
    visible: bytes | None = None,
    answer: str = "Bound final answer",
    candidate_reauthorizer=_allow_candidate,
    coverage_reauthorizer=_allow_coverage,
    ledger: ArchiveModelBatchLedger | None = None,
):
    selected_ledger = run.turn_ledger if ledger is None else ledger
    if ledger is None:
        selected_ledger.admit_model_tool_bytes(
            run,
            batch,
            batch.model_visible_canonical_bytes if visible is None else visible,
        )
        selected_ledger.freeze_for_publication()
    return attest_archive_search_before_publication(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=selected_ledger,
        answer=answer,
        candidate_reauthorizer=candidate_reauthorizer,
        coverage_reauthorizer=coverage_reauthorizer,
        authority_context=CONTEXT,
    )


def test_run_factory_owns_exact_binding_and_actor_or_binding_mismatch_fails_closed() -> None:
    run = _run()

    assert canonical_archive_search_targets(_request()) == TARGETS
    assert run.execution_binding.authority_scope is AuthorityScope.TENANT_PRINCIPAL
    assert run.execution_binding.attests_private_request(_request().to_identity_json())
    rendered = repr(run) + json.dumps(run, default=str)
    assert QUERY not in rendered and TENANT not in rendered and PRINCIPAL not in rendered
    assert not hasattr(run, "to_payload") and not hasattr(run, "to_json")

    with pytest.raises(ArchiveSearchAuthorityError) as actor_error:
        authorize_archive_search_before_model(
            tenant_id=TENANT,
            principal_id="another-principal",
            run_binding=run,
            candidates=_candidates(),
            coverage=_coverage(run),
            candidate_reauthorizer=_allow_candidate,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )
    assert actor_error.value.__cause__ is None

    other_run = _run(run="run-private-2")
    with pytest.raises(ArchiveSearchAuthorityError):
        _batch(run, coverage=_coverage(other_run))

    with pytest.raises(ArchiveSearchAuthorityError) as underdeclared:
        _run(targets=((SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL),))
    assert underdeclared.value.__cause__ is None


def test_run_seal_binds_privacy_key_and_full_request_including_continuation() -> None:
    changed_key = _run()
    object.__setattr__(changed_key, "_privacy_key", b"x" * 32)
    with pytest.raises(ArchiveSearchAuthorityError) as key_error:
        _batch(changed_key)
    assert key_error.value.__cause__ is None

    changed_request = _run(request=_request(continuation="continuation-A"))
    object.__setattr__(changed_request._request, "continuation", "continuation-B")
    with pytest.raises(ArchiveSearchAuthorityError) as request_error:
        _batch(changed_request)
    assert request_error.value.__cause__ is None


def test_actor_and_answer_require_exact_str_without_virtual_encode() -> None:
    encode_calls: list[str] = []

    class HostileText(str):
        def encode(self, *_args: object, **_kwargs: object) -> bytes:
            encode_calls.append("called")
            raise RuntimeError(f"private encode body: {QUERY}")

    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"hostile-turn-{next(_TURN_SEQUENCE)}",
    )
    with pytest.raises(ArchiveSearchAuthorityError) as actor_error:
        create_archive_search_run_binding(
            tenant_id=cast(str, HostileText(TENANT)),
            principal_id=PRINCIPAL,
            request=_request(),
            requested_targets=TARGETS,
            snapshot_discriminator="snapshot-private-1",
            run_discriminator="run-private-1",
            turn_ledger=ledger,
        )
    assert actor_error.value.__cause__ is None

    run = _run()
    batch = _batch(run)
    with pytest.raises(ArchiveSearchPublicationDenied) as answer_error:
        _attest(run, batch, answer=cast(str, HostileText("private answer")))
    assert answer_error.value.reason is ArchiveSearchPublicationDenialReason.ANSWER_INVALID
    assert answer_error.value.__cause__ is None
    assert encode_calls == []
    assert QUERY not in (str(actor_error.value) + str(answer_error.value))


def test_both_reauthorization_phases_run_and_attestation_binds_exact_answer() -> None:
    run = _run()
    calls: list[tuple[str, ArchiveSearchAuthorityPhase]] = []

    def candidate_reauth(
        phase: ArchiveSearchAuthorityPhase,
        _run_binding: ArchiveSearchRunBinding,
        candidate: ArchiveSearchCandidate,
        context: object,
    ) -> ArchiveSearchCandidateReauthorization:
        assert context is CONTEXT
        calls.append(("candidate", phase))
        return ArchiveSearchCandidateReauthorization.authorized(candidate)

    def coverage_reauth(
        phase: ArchiveSearchAuthorityPhase,
        _run_binding: ArchiveSearchRunBinding,
        coverage: SearchCoverage,
        context: object,
    ) -> ArchiveSearchCoverageReauthorization:
        assert context is CONTEXT
        calls.append(("coverage", phase))
        return ArchiveSearchCoverageReauthorization.authorized(coverage)

    batch = _batch(
        run,
        candidate_reauthorizer=candidate_reauth,
        coverage_reauthorizer=coverage_reauth,
    )
    payload = batch.public_tool_result_payload
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    assert canonical == batch.model_visible_canonical_bytes
    public_text = canonical.decode("ascii")
    assert QUERY not in public_text and TENANT not in public_text and PRINCIPAL not in public_text
    assert RAW_ONE not in public_text and RAW_TWO not in public_text

    answer = "Exact archive answer"
    attestation = _attest(
        run,
        batch,
        answer=answer,
        candidate_reauthorizer=candidate_reauth,
        coverage_reauthorizer=coverage_reauth,
    )
    assert attestation.answer_sha256 == hashlib.sha256(answer.encode()).hexdigest()
    assert attestation.attests_answer(answer)
    assert not attestation.attests_answer("changed answer")
    assert calls.count(("candidate", ArchiveSearchAuthorityPhase.BEFORE_MODEL)) == 2
    assert calls.count(("candidate", ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION)) == 2
    assert calls.count(("coverage", ArchiveSearchAuthorityPhase.BEFORE_MODEL)) == 5
    assert calls.count(("coverage", ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION)) == 5


def test_before_model_filters_denied_source_and_degrades_only_its_match_target() -> None:
    run = _run()

    def candidate_reauth(
        _phase: ArchiveSearchAuthorityPhase,
        _run_binding: ArchiveSearchRunBinding,
        candidate: ArchiveSearchCandidate,
        _context: object,
    ) -> ArchiveSearchCandidateReauthorization:
        if candidate.title == "One":
            return ArchiveSearchCandidateReauthorization.rejected(ArchiveSearchReauthorizationStatus.DENIED)
        return ArchiveSearchCandidateReauthorization.authorized(candidate)

    batch = _batch(run, candidate_reauthorizer=candidate_reauth)
    payload = batch.public_tool_result_payload
    candidates = cast(list[dict[str, object]], payload["candidates"])
    coverage = {item["lane"]: item for item in cast(list[dict[str, object]], payload["coverage"])}

    assert [item["title"] for item in candidates] == ["Two"]
    assert coverage["dense"]["states"] == ["complete"]
    assert set(cast(list[str], coverage["lexical"]["states"])) == {
        "partial",
        "permission_filtered",
    }
    assert coverage["lexical"]["returned"] == 0
    assert payload["exhaustive"] is False
    assert "permission_filtered" in cast(list[str], payload["warnings"])


def test_source_scope_mismatch_is_filtered_even_if_callback_claims_authorized() -> None:
    run = _run()
    foreign = _candidate(
        RAW_ONE,
        ArchiveMatchChannel.LEXICAL,
        title="Foreign",
        principal_id="foreign-principal",
    )
    batch = _batch(
        run,
        candidates=(foreign,),
        coverage=_coverage(run, zero=True),
    )
    payload = batch.public_tool_result_payload

    assert payload["candidates"] == []
    assert payload["absence"] == "not_established"
    projected_coverage = {item["lane"]: item for item in cast(list[dict[str, object]], payload["coverage"])}
    assert projected_coverage["lexical"]["states"] == ["partial", "permission_filtered"]


def test_resumed_page_rebinds_frozen_coverage_then_uses_same_authority_gate() -> None:
    original = _run(run="original-run")
    token, _issued = _issue_public_continuation(original)
    resumed = _run(
        run="resumed-run",
        request=_request(continuation=token),
    )
    seen_bindings: list[SearchExecutionBinding] = []
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
    )

    def coverage_reauth(
        phase: ArchiveSearchAuthorityPhase,
        run_binding: ArchiveSearchRunBinding,
        coverage: SearchCoverage,
        _context: object,
    ) -> ArchiveSearchCoverageReauthorization:
        assert phase is ArchiveSearchAuthorityPhase.BEFORE_MODEL
        assert coverage.execution_binding is run_binding.execution_binding
        seen_bindings.append(coverage.execution_binding)
        return ArchiveSearchCoverageReauthorization.authorized(coverage)

    batch = authorize_archive_search_resumed_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
        redemption=redemption,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=coverage_reauth,
        authority_context=CONTEXT,
    )

    assert len(seen_bindings) == 5
    assert all(binding is resumed.execution_binding for binding in seen_bindings)
    assert batch.public_tool_result_payload["continuation"] is None
    duplicate_run = _run(
        run="duplicate-resumed-run",
        request=_request(continuation=token),
    )
    with pytest.raises(ArchiveSearchAuthorityError) as duplicate:
        redeem_archive_search_continuation(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=duplicate_run,
        )
    assert duplicate.value.__cause__ is None

    with pytest.raises(ArchiveSearchAuthorityError):
        authorize_archive_search_resumed_before_model(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=resumed,
            redemption=redemption,
            candidate_reauthorizer=_allow_candidate,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )


def test_continuation_registry_rejects_invented_tokens_and_consumes_before_actor_checks() -> None:
    assert not hasattr(authority_module, "create_redeemed_archive_continuation")
    invented = _run(
        run="invented-redemption-run",
        request=_request(continuation="invented-token"),
    )
    with pytest.raises(ArchiveSearchAuthorityError) as invented_error:
        redeem_archive_search_continuation(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=invented,
        )
    assert invented_error.value.__cause__ is None

    origin_without_tail = _run(run="empty-tail-origin")
    with pytest.raises(ArchiveSearchAuthorityError):
        issue_archive_search_continuation(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=origin_without_tail,
            tail_candidates=(),
            terminal_coverage=_coverage(origin_without_tail),
        )

    original = _run(run="original-redemption-run")
    token, issued = _issue_public_continuation(original)
    assert token not in repr(issued) and QUERY not in repr(issued)
    with pytest.raises(ArchiveSearchAuthorityError):
        IssuedArchiveContinuation()
    with pytest.raises(TypeError, match="process-private"):
        copy.copy(issued)
    with pytest.raises(TypeError, match="process-private"):
        copy.deepcopy(issued)
    with pytest.raises(TypeError, match="process-private"):
        pickle.dumps(issued)
    with pytest.raises(ArchiveSearchAuthorityError):
        issue_archive_search_continuation(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=original,
            tail_candidates=_candidates(),
            terminal_coverage=_terminal_coverage_for_tail(original, _candidates()),
        )
    with pytest.raises(ArchiveSearchAuthorityError):
        authorize_archive_search_before_model(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=original,
            candidates=(),
            coverage=_continuing_coverage(original, _coverage(original), _candidates()),
            continuation=issued,
            candidate_reauthorizer=_allow_candidate,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )

    wrong_actor_run = _run(
        principal_id="wrong-principal",
        run="wrong-actor-redemption-run",
        request=_request(continuation=token),
    )
    with pytest.raises(ArchiveSearchAuthorityError) as wrong_actor:
        redeem_archive_search_continuation(
            tenant_id=TENANT,
            principal_id="wrong-principal",
            run_binding=wrong_actor_run,
        )
    assert wrong_actor.value.__cause__ is None

    correct_run = _run(
        run="correct-after-actor-failure",
        request=_request(continuation=token),
    )
    with pytest.raises(ArchiveSearchAuthorityError):
        redeem_archive_search_continuation(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=correct_run,
        )


def test_continuation_record_tamper_is_consumed_and_fails_closed() -> None:
    origin = _run(run="tampered-record-origin")
    token, _issued = _issue_public_continuation(origin)
    token_handle = authority_module._continuation_token_handle(token)
    record = authority_module._CONTINUATION_REGISTRY._records[token_handle]
    object.__setattr__(record, "seal", "f" * 64)
    resumed = _run(
        run="tampered-record-redemption",
        request=_request(continuation=token),
    )
    with pytest.raises(ArchiveSearchAuthorityError):
        redeem_archive_search_continuation(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=resumed,
        )
    with pytest.raises(ArchiveSearchAuthorityError):
        redeem_archive_search_continuation(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=resumed,
        )


def test_continuation_redemption_is_process_private_and_request_exact() -> None:
    original = _run(run="private-redemption-origin")
    token, _issued = _issue_public_continuation(original)
    resumed = _run(run="private-redemption-run", request=_request(continuation=token))
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
    )
    rendered = repr(redemption) + json.dumps(redemption, default=str)
    assert token not in rendered and QUERY not in rendered
    with pytest.raises(ArchiveSearchAuthorityError):
        RedeemedArchiveContinuation()
    with pytest.raises(TypeError, match="process-private"):
        copy.copy(redemption)
    with pytest.raises(TypeError, match="process-private"):
        copy.deepcopy(redemption)
    with pytest.raises(TypeError, match="process-private"):
        pickle.dumps(redemption)

    object.__setattr__(resumed._request, "continuation", "changed-token")
    with pytest.raises(ArchiveSearchAuthorityError) as rebound_token:
        authorize_archive_search_resumed_before_model(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=resumed,
            redemption=redemption,
            candidate_reauthorizer=_allow_candidate,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )
    assert rebound_token.value.__cause__ is None


def test_resumed_authority_mints_exact_child_without_caller_token() -> None:
    tail = tuple(
        replace(
            _candidate(f"raw_{index:016d}", ArchiveMatchChannel.LEXICAL, title=f"T{index}"),
            matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, index),),
        )
        for index in range(1, 4)
    )
    origin = _run(run="child-origin", request=_request(limit=2))
    token, _issued = _issue_public_continuation(origin, tail=tail)
    resumed = _run(
        run="child-page-one",
        request=_request(limit=2, continuation=token),
    )
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
    )
    first = authorize_archive_search_resumed_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
        redemption=redemption,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )
    payload = first.public_tool_result_payload
    child = payload["continuation"]
    assert type(child) is str and child != token
    assert len(cast(list[object], payload["candidates"])) == 2

    final_run = _run(
        run="child-page-two",
        request=_request(limit=2, continuation=child),
    )
    final_redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=final_run,
    )
    final = authorize_archive_search_resumed_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=final_run,
        redemption=final_redemption,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )
    assert final.public_tool_result_payload["continuation"] is None
    assert len(cast(list[object], final.public_tool_result_payload["candidates"])) == 1


def test_resumed_authority_uses_largest_public_byte_prefix_without_losing_suffix() -> None:
    tail = _large_candidates(3)
    origin = _run(run="byte-prefix-origin", request=_request(limit=3))
    token, _issued = _issue_public_continuation(origin, tail=tail)
    resumed = _run(
        run="byte-prefix-first",
        request=_request(limit=3, continuation=token),
    )
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
    )

    first = authorize_archive_search_resumed_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
        redemption=redemption,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )
    first_payload = first.public_tool_result_payload
    first_candidates = cast(list[dict[str, object]], first_payload["candidates"])
    child = first_payload["continuation"]

    assert [item["title"] for item in first_candidates] == ["Large 1", "Large 2"]
    assert type(child) is str and child != token
    assert len(child) == 43 and child.isascii()
    assert len(first.model_visible_canonical_bytes) <= 7_900

    final_run = _run(
        run="byte-prefix-final",
        request=_request(limit=3, continuation=child),
    )
    final_redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=final_run,
    )
    final = authorize_archive_search_resumed_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=final_run,
        redemption=final_redemption,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )
    final_payload = final.public_tool_result_payload
    final_candidates = cast(list[dict[str, object]], final_payload["candidates"])

    assert [item["title"] for item in final_candidates] == ["Large 3"]
    assert final_payload["continuation"] is None


def test_resumed_probe_differs_from_inbound_and_child_mint_follows_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = authority_module._ArchiveContinuationRegistry()
    monkeypatch.setattr(authority_module, "_CONTINUATION_REGISTRY", registry)
    tokens = iter(("A" * 43, "A" * 43, "C" * 43))
    monkeypatch.setattr(authority_module.secrets, "token_urlsafe", lambda _size: next(tokens))
    tail = _large_candidates(4)
    origin = _run(run="probe-origin", request=_request(limit=3))
    token, _issued = _issue_public_continuation(origin, tail=tail)
    assert token == "A" * 43

    resumed = _run(
        run="probe-resumed",
        request=_request(limit=3, continuation=token),
    )
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
    )
    events: list[tuple[str, str | None]] = []
    original_projection = ArchiveSearchPage.to_public_json
    original_mint_child = authority_module._ArchiveContinuationRegistry.mint_selected_child

    def observed_projection(page: ArchiveSearchPage, privacy_key: bytes) -> str:
        events.append(("project", page.continuation))
        return original_projection(page, privacy_key)

    def observed_mint_child(
        selected_registry: authority_module._ArchiveContinuationRegistry,
        *,
        run: ArchiveSearchRunBinding,
        selection: authority_module._SelectedArchiveContinuation,
    ) -> authority_module._ArchiveContinuationRecord:
        events.append(("mint", None))
        return original_mint_child(
            selected_registry,
            run=run,
            selection=selection,
        )

    monkeypatch.setattr(ArchiveSearchPage, "to_public_json", observed_projection)
    monkeypatch.setattr(
        authority_module._ArchiveContinuationRegistry,
        "mint_selected_child",
        observed_mint_child,
    )
    batch = authorize_archive_search_resumed_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
        redemption=redemption,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )

    child = batch.public_tool_result_payload["continuation"]
    mint_index = events.index(("mint", None))
    assert events[0] == ("project", "B" * 43)
    assert all(value != token for event, value in events[:mint_index] if event == "project")
    assert mint_index > 0
    assert child == "C" * 43


def test_resumed_callback_cannot_reenter_or_mint_before_outer_selection_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = authority_module._ArchiveContinuationRegistry()
    monkeypatch.setattr(authority_module, "_CONTINUATION_REGISTRY", registry)
    tail = tuple(
        replace(
            _candidate(f"raw_{index:016d}", ArchiveMatchChannel.LEXICAL, title=f"R{index}"),
            matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, index),),
        )
        for index in range(1, 4)
    )
    origin = _run(run="reentrant-origin", request=_request(limit=1))
    token, _issued = _issue_public_continuation(origin, tail=tail)
    resumed = _run(
        run="reentrant-resumed",
        request=_request(limit=1, continuation=token),
    )
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
    )
    nested_attempted = False

    def reentrant_candidate(
        _phase: ArchiveSearchAuthorityPhase,
        _run_binding: ArchiveSearchRunBinding,
        candidate: ArchiveSearchCandidate,
        _context: object,
    ) -> ArchiveSearchCandidateReauthorization:
        nonlocal nested_attempted
        if not nested_attempted:
            nested_attempted = True
            with pytest.raises(TypeError):
                authority_module._finish_redeemed_continuation(  # type: ignore[call-arg]
                    resumed,
                    redemption,
                    head_count=1,
                )
            with pytest.raises((AttributeError, ArchiveSearchAuthorityError)):
                authority_module._finish_redeemed_continuation(
                    resumed,
                    redemption,
                    cast(
                        authority_module._ArchiveContinuationSelectionLease,
                        redemption._selection_handle,
                    ),
                    head_count=1,
                )
            with pytest.raises(ArchiveSearchAuthorityError):
                authority_module._finish_redeemed_continuation(
                    resumed,
                    redemption,
                    cast(
                        authority_module._ArchiveContinuationSelectionLease,
                        redemption,
                    ),
                    head_count=1,
                )
            authority_module._abort_redeemed_continuation(
                resumed,
                redemption,
                cast(
                    authority_module._ArchiveContinuationSelectionLease,
                    redemption,
                ),
            )
            with pytest.raises(ArchiveSearchAuthorityError):
                authority_module._new_selected_continuation(
                    run=resumed,
                    redemption=redemption,
                    lease=cast(
                        authority_module._ArchiveContinuationSelectionLease,
                        redemption._selection_handle,
                    ),
                    candidates=tail[1:],
                )
            with pytest.raises(ArchiveSearchAuthorityError):
                authorize_archive_search_resumed_before_model(
                    tenant_id=TENANT,
                    principal_id=PRINCIPAL,
                    run_binding=resumed,
                    redemption=redemption,
                    candidate_reauthorizer=_allow_candidate,
                    coverage_reauthorizer=_allow_coverage,
                    authority_context=CONTEXT,
                )
            assert not registry._records
            assert not hasattr(registry, "mint_child")
        return ArchiveSearchCandidateReauthorization.authorized(candidate)

    batch = authorize_archive_search_resumed_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
        redemption=redemption,
        candidate_reauthorizer=reentrant_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )

    assert nested_attempted is True
    assert type(batch.public_tool_result_payload["continuation"]) is str
    assert len(registry._records) == 1
    with pytest.raises(ArchiveSearchAuthorityError):
        authorize_archive_search_resumed_before_model(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=resumed,
            redemption=redemption,
            candidate_reauthorizer=_allow_candidate,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )
    assert len(registry._records) == 1


def test_concurrent_resumed_authorization_has_one_winner_and_one_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = authority_module._ArchiveContinuationRegistry()
    monkeypatch.setattr(authority_module, "_CONTINUATION_REGISTRY", registry)
    tail = tuple(
        replace(
            _candidate(f"raw_{index:016d}", ArchiveMatchChannel.LEXICAL, title=f"C{index}"),
            matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, index),),
        )
        for index in range(1, 4)
    )
    origin = _run(run="concurrent-origin", request=_request(limit=1))
    token, _issued = _issue_public_continuation(origin, tail=tail)
    resumed = _run(
        run="concurrent-resumed",
        request=_request(limit=1, continuation=token),
    )
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
    )
    entered = threading.Event()
    release = threading.Event()
    batches: list[AuthorizedArchiveBatch] = []
    failures: list[Exception] = []

    def paused_candidate(
        _phase: ArchiveSearchAuthorityPhase,
        _run_binding: ArchiveSearchRunBinding,
        candidate: ArchiveSearchCandidate,
        _context: object,
    ) -> ArchiveSearchCandidateReauthorization:
        entered.set()
        assert release.wait(timeout=2)
        return ArchiveSearchCandidateReauthorization.authorized(candidate)

    def winner() -> None:
        try:
            batches.append(
                authorize_archive_search_resumed_before_model(
                    tenant_id=TENANT,
                    principal_id=PRINCIPAL,
                    run_binding=resumed,
                    redemption=redemption,
                    candidate_reauthorizer=paused_candidate,
                    coverage_reauthorizer=_allow_coverage,
                    authority_context=CONTEXT,
                )
            )
        except Exception as exc:  # pragma: no cover - assertion below owns detail
            failures.append(exc)

    thread = threading.Thread(target=winner)
    thread.start()
    assert entered.wait(timeout=2)
    with pytest.raises(ArchiveSearchAuthorityError):
        authorize_archive_search_resumed_before_model(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=resumed,
            redemption=redemption,
            candidate_reauthorizer=_allow_candidate,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )
    assert not registry._records
    release.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert not failures
    assert len(batches) == 1
    assert type(batches[0].public_tool_result_payload["continuation"]) is str
    assert len(registry._records) == 1


def test_resumed_page_that_cannot_fit_one_candidate_consumes_and_leaves_no_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = authority_module._ArchiveContinuationRegistry()
    monkeypatch.setattr(authority_module, "_CONTINUATION_REGISTRY", registry)
    tail = (_oversized_candidate(), _large_candidates(2, excerpt_chars=100)[1])
    origin = _run(run="unfit-origin", request=_request(limit=2))
    token, _issued = _issue_public_continuation(origin, tail=tail)
    resumed = _run(
        run="unfit-resumed",
        request=_request(limit=2, continuation=token),
    )
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
    )

    with pytest.raises(ArchiveSearchAuthorityError):
        authorize_archive_search_resumed_before_model(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=resumed,
            redemption=redemption,
            candidate_reauthorizer=_allow_candidate,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )

    assert not registry._records
    with pytest.raises(ArchiveSearchAuthorityError):
        authorize_archive_search_resumed_before_model(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=resumed,
            redemption=redemption,
            candidate_reauthorizer=_allow_candidate,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )


def test_resumed_final_projection_failure_revokes_minted_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = authority_module._ArchiveContinuationRegistry()
    monkeypatch.setattr(authority_module, "_CONTINUATION_REGISTRY", registry)
    tail = _candidates()
    origin = _run(run="final-projection-origin", request=_request(limit=1))
    token, _issued = _issue_public_continuation(origin, tail=tail)
    resumed = _run(
        run="final-projection-resumed",
        request=_request(limit=1, continuation=token),
    )
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
    )
    original_projection = ArchiveSearchPage.to_public_json
    resumed_projections = 0

    def fail_after_selection(page: ArchiveSearchPage, privacy_key: bytes) -> str:
        nonlocal resumed_projections
        if page.request is resumed._request:
            resumed_projections += 1
            if resumed_projections == 2:
                raise RuntimeError("final projection failed")
        return original_projection(page, privacy_key)

    monkeypatch.setattr(ArchiveSearchPage, "to_public_json", fail_after_selection)
    with pytest.raises(ArchiveSearchAuthorityError):
        authorize_archive_search_resumed_before_model(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=resumed,
            redemption=redemption,
            candidate_reauthorizer=_allow_candidate,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )

    assert resumed_projections == 2
    assert not registry._records
    with pytest.raises(ArchiveSearchAuthorityError):
        authorize_archive_search_resumed_before_model(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=resumed,
            redemption=redemption,
            candidate_reauthorizer=_allow_candidate,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )


def test_backend_capped_lane_without_cursor_warns_beside_other_lane_child() -> None:
    tail = _candidates()
    origin = _run(run="backend-capped-origin", request=_request(limit=1))
    terminal = tuple(
        SearchCoverage.create(
            corpus=item.corpus,
            lane=item.lane,
            execution_binding=item.execution_binding,
            states=(CoverageState.PARTIAL, CoverageState.CAPPED),
            eligible_authorized=item.eligible_authorized,
            examined=item.examined,
            matched_at_least=item.matched_at_least,
            returned=item.returned,
            authority_rechecked=item.authority_rechecked,
            snapshot_current=item.snapshot_current,
            limit=1,
            next_cursor_available=False,
        )
        if item.lane is SearchLane.LEXICAL
        else item
        for item in _terminal_coverage_for_tail(origin, tail)
    )
    issued = issue_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=origin,
        tail_candidates=tail,
        terminal_coverage=terminal,
        warnings=(ArchiveSearchWarning.BACKFILL_PENDING,),
    )
    initial = authorize_archive_search_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=origin,
        candidates=(),
        coverage=_continuing_coverage(origin, terminal, tail),
        warnings=(ArchiveSearchWarning.BACKFILL_PENDING,),
        continuation=issued,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )
    token = cast(str, initial.public_tool_result_payload["continuation"])
    resumed = _run(
        run="backend-capped-first",
        request=_request(limit=1, continuation=token),
    )
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
    )
    first = authorize_archive_search_resumed_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
        redemption=redemption,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )
    first_payload = first.public_tool_result_payload
    first_coverage = {item["lane"]: item for item in cast(list[dict[str, object]], first_payload["coverage"])}
    child = cast(str, first_payload["continuation"])

    assert first_payload["warnings"] == ["backfill_pending", "continuation_unavailable"]
    assert first_coverage["lexical"]["next_cursor_available"] is False
    assert first_coverage["dense"]["next_cursor_available"] is True

    final_run = _run(
        run="backend-capped-final",
        request=_request(limit=1, continuation=child),
    )
    final_redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=final_run,
    )
    final = authorize_archive_search_resumed_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=final_run,
        redemption=final_redemption,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )

    assert final.public_tool_result_payload["continuation"] is None
    assert final.public_tool_result_payload["warnings"] == [
        "backfill_pending",
        "continuation_unavailable",
    ]


def test_continuation_expiry_and_registry_restart_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    registry = authority_module._ArchiveContinuationRegistry(
        clock=lambda: clock[0],
        ttl_seconds=1.0,
    )
    monkeypatch.setattr(authority_module, "_CONTINUATION_REGISTRY", registry)
    expired_origin = _run(run="expiry-origin")
    expired_token, _issued = _issue_public_continuation(expired_origin)
    clock[0] = 102.0
    expired_run = _run(
        run="expiry-redemption",
        request=_request(continuation=expired_token),
    )
    with pytest.raises(ArchiveSearchAuthorityError):
        redeem_archive_search_continuation(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=expired_run,
        )

    clock[0] = 200.0
    restart_origin = _run(run="restart-origin")
    restart_token, _issued = _issue_public_continuation(restart_origin)
    monkeypatch.setattr(
        authority_module,
        "_CONTINUATION_REGISTRY",
        authority_module._ArchiveContinuationRegistry(clock=lambda: clock[0]),
    )
    restart_run = _run(
        run="restart-redemption",
        request=_request(continuation=restart_token),
    )
    with pytest.raises(ArchiveSearchAuthorityError):
        redeem_archive_search_continuation(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            run_binding=restart_run,
        )


def test_candidate_revoke_or_snapshot_change_between_phases_denies_old_answer() -> None:
    run = _run()
    revoked = False
    phases: list[ArchiveSearchAuthorityPhase] = []

    def candidate_reauth(
        phase: ArchiveSearchAuthorityPhase,
        _run_binding: ArchiveSearchRunBinding,
        candidate: ArchiveSearchCandidate,
        _context: object,
    ) -> ArchiveSearchCandidateReauthorization:
        phases.append(phase)
        if revoked and candidate.title == "One":
            return ArchiveSearchCandidateReauthorization.rejected(ArchiveSearchReauthorizationStatus.DRIFTED)
        return ArchiveSearchCandidateReauthorization.authorized(candidate)

    batch = _batch(run, candidate_reauthorizer=candidate_reauth)
    revoked = True
    with pytest.raises(ArchiveSearchPublicationDenied) as denied:
        _attest(run, batch, candidate_reauthorizer=candidate_reauth)

    assert denied.value.reason is ArchiveSearchPublicationDenialReason.AUTHORITY_CHANGED
    assert denied.value.__cause__ is None
    denial_text = str(denied.value)
    assert QUERY not in denial_text and RAW_ONE not in denial_text
    assert ArchiveSearchAuthorityPhase.BEFORE_MODEL in phases
    assert ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION in phases


def test_coverage_change_between_phases_denies_even_when_callback_says_authorized() -> None:
    run = _run()
    drift = False

    def coverage_reauth(
        _phase: ArchiveSearchAuthorityPhase,
        _run_binding: ArchiveSearchRunBinding,
        coverage: SearchCoverage,
        _context: object,
    ) -> ArchiveSearchCoverageReauthorization:
        current = coverage
        if drift and coverage.lane is SearchLane.LEXICAL:
            current = replace(
                coverage,
                eligible_authorized=2,
                examined=2,
                matched_at_least=2,
            )
        return ArchiveSearchCoverageReauthorization.authorized(current)

    batch = _batch(run, coverage_reauthorizer=coverage_reauth)
    drift = True
    with pytest.raises(ArchiveSearchPublicationDenied) as denied:
        _attest(run, batch, coverage_reauthorizer=coverage_reauth)
    assert denied.value.reason is ArchiveSearchPublicationDenialReason.AUTHORITY_CHANGED


def test_phase_two_nested_mutation_is_body_free_and_still_checks_every_carrier() -> None:
    run = _run()
    candidate_calls = 0
    coverage_calls = 0

    class ExplodingPrivateValue:
        def __getattribute__(self, _name: str) -> object:
            raise RuntimeError(f"private nested body: {QUERY} {RAW_ONE}")

    def candidate_reauth(
        phase: ArchiveSearchAuthorityPhase,
        _run_binding: ArchiveSearchRunBinding,
        candidate: ArchiveSearchCandidate,
        _context: object,
    ) -> ArchiveSearchCandidateReauthorization:
        nonlocal candidate_calls
        if phase is ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION:
            candidate_calls += 1
            object.__setattr__(candidate, "resolved_source", ExplodingPrivateValue())
        return ArchiveSearchCandidateReauthorization.authorized(candidate)

    def coverage_reauth(
        phase: ArchiveSearchAuthorityPhase,
        _run_binding: ArchiveSearchRunBinding,
        coverage: SearchCoverage,
        _context: object,
    ) -> ArchiveSearchCoverageReauthorization:
        nonlocal coverage_calls
        if phase is ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION:
            coverage_calls += 1
            object.__setattr__(coverage, "execution_binding", ExplodingPrivateValue())
        return ArchiveSearchCoverageReauthorization.authorized(coverage)

    batch = _batch(run)
    with pytest.raises(ArchiveSearchPublicationDenied) as denied:
        _attest(
            run,
            batch,
            candidate_reauthorizer=candidate_reauth,
            coverage_reauthorizer=coverage_reauth,
        )

    assert denied.value.reason is ArchiveSearchPublicationDenialReason.AUTHORITY_CHANGED
    assert denied.value.__cause__ is None
    assert candidate_calls == 2
    assert coverage_calls == 5
    assert QUERY not in (str(denied.value) + repr(denied.value))


def test_zero_hit_coverage_is_reauthorized_and_late_lane_revoke_denies_absence() -> None:
    run = _run()
    revoked = False
    phases: list[ArchiveSearchAuthorityPhase] = []

    def coverage_reauth(
        phase: ArchiveSearchAuthorityPhase,
        _run_binding: ArchiveSearchRunBinding,
        coverage: SearchCoverage,
        _context: object,
    ) -> ArchiveSearchCoverageReauthorization:
        phases.append(phase)
        if revoked:
            return ArchiveSearchCoverageReauthorization.rejected(ArchiveSearchReauthorizationStatus.DENIED)
        return ArchiveSearchCoverageReauthorization.authorized(coverage)

    batch = _batch(
        run,
        candidates=(),
        coverage=_coverage(run, zero=True),
        coverage_reauthorizer=coverage_reauth,
    )
    assert batch.public_tool_result_payload["absence"] == "authorized_absence_confirmed"
    revoked = True
    with pytest.raises(ArchiveSearchPublicationDenied) as denied:
        _attest(run, batch, coverage_reauthorizer=coverage_reauth)
    assert denied.value.reason is ArchiveSearchPublicationDenialReason.AUTHORITY_CHANGED
    assert phases.count(ArchiveSearchAuthorityPhase.BEFORE_MODEL) == 5
    assert phases.count(ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION) == 5


def test_model_batch_ledger_is_exact_bounded_frozen_and_one_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turn_id = f"ledger-turn-{next(_TURN_SEQUENCE)}"
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=turn_id,
    )
    assert (
        create_archive_model_batch_ledger(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            turn_discriminator=turn_id,
        )
        is ledger
    )
    run = _run(ledger=ledger)
    batch = _batch(run)

    with pytest.raises(ArchiveSearchAuthorityError) as changed_bytes:
        ledger.admit_model_tool_bytes(run, batch, batch.model_visible_canonical_bytes + b" ")
    assert changed_bytes.value.__cause__ is None

    ledger.admit_model_tool_bytes(run, batch, batch.model_visible_canonical_bytes)
    with pytest.raises(ArchiveSearchAuthorityError):
        ledger.admit_model_tool_bytes(run, batch, batch.model_visible_canonical_bytes)
    ledger.freeze_for_publication()
    with pytest.raises(ArchiveSearchAuthorityError):
        ledger.admit_model_tool_bytes(run, _batch(run), batch.model_visible_canonical_bytes)

    attestation = _attest(run, batch, ledger=ledger)
    assert attestation.attests_answer("Bound final answer")
    with pytest.raises(ArchiveSearchPublicationDenied) as replay:
        _attest(run, batch, ledger=ledger)
    assert replay.value.reason is ArchiveSearchPublicationDenialReason.LEDGER_UNAVAILABLE
    assert replay.value.__cause__ is None

    rendered = repr(ledger) + json.dumps(ledger, default=str)
    assert QUERY not in rendered and RAW_ONE not in rendered
    with pytest.raises(ArchiveSearchAuthorityError):
        ArchiveModelBatchLedger()
    with pytest.raises(TypeError, match="process-private"):
        copy.copy(ledger)
    with pytest.raises(TypeError, match="process-private"):
        copy.deepcopy(ledger)
    with pytest.raises(TypeError, match="process-private"):
        pickle.dumps(ledger)

    with pytest.raises(ArchiveSearchAuthorityError):
        create_archive_model_batch_ledger(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            turn_discriminator=turn_id,
        )
    assert all(candidate is not ledger for candidate in authority_module._TURN_LEDGER_REGISTRY.values())
    with pytest.raises(ArchiveSearchAuthorityError):
        _run(ledger=ledger, run="cannot-restart-consumed-ledger")

    bounded = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"bounded-turn-{next(_TURN_SEQUENCE)}",
    )
    monkeypatch.setattr(authority_module, "ARCHIVE_AUTHORITY_MAX_MODEL_BYTES", 10_000_000)
    accepted: list[tuple[ArchiveSearchRunBinding, AuthorizedArchiveBatch]] = []
    for index in range(ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES):
        bounded_run = _run(ledger=bounded, run=f"bounded-run-{index}")
        current = _batch(bounded_run)
        bounded.admit_model_tool_bytes(
            bounded_run,
            current,
            current.model_visible_canonical_bytes,
        )
        accepted.append((bounded_run, current))
    duplicate_run, _accepted_batch = accepted[0]
    overflow = _batch(duplicate_run)
    with pytest.raises(ArchiveSearchAuthorityError):
        bounded.admit_model_tool_bytes(
            duplicate_run,
            overflow,
            overflow.model_visible_canonical_bytes,
        )


def test_model_batch_ledger_enforces_aggregate_byte_cap() -> None:
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"aggregate-turn-{next(_TURN_SEQUENCE)}",
    )
    accepted_bytes = 0
    rejected_size = 0
    for index in range(ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES):
        run = _run(ledger=ledger, run=f"aggregate-run-{index}")
        batch = _batch(run)
        body = batch.model_visible_canonical_bytes
        try:
            ledger.admit_model_tool_bytes(run, batch, body)
        except ArchiveSearchAuthorityError:
            rejected_size = len(body)
            break
        accepted_bytes += len(body)

    assert rejected_size > 0
    assert accepted_bytes <= ARCHIVE_AUTHORITY_MAX_MODEL_BYTES
    assert accepted_bytes + rejected_size > ARCHIVE_AUTHORITY_MAX_MODEL_BYTES


def test_turn_ledger_attests_every_run_and_one_late_revoke_denies_all() -> None:
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"multi-run-turn-{next(_TURN_SEQUENCE)}",
    )
    first_run = _run(ledger=ledger, run="multi-run-one")
    second_run = _run(ledger=ledger, run="multi-run-two")
    first_batch = _batch(first_run)
    second_batch = _batch(second_run)
    ledger.admit_model_tool_bytes(
        first_run,
        first_batch,
        first_batch.model_visible_canonical_bytes,
    )
    ledger.admit_model_tool_bytes(
        second_run,
        second_batch,
        second_batch.model_visible_canonical_bytes,
    )
    ledger.freeze_for_publication()
    seen_runs: list[ArchiveSearchRunBinding] = []

    def late_reauth(
        phase: ArchiveSearchAuthorityPhase,
        run_binding: ArchiveSearchRunBinding,
        candidate: ArchiveSearchCandidate,
        _context: object,
    ) -> ArchiveSearchCandidateReauthorization:
        if phase is ArchiveSearchAuthorityPhase.BEFORE_PUBLICATION:
            seen_runs.append(run_binding)
            if run_binding is second_run and candidate.title == "One":
                return ArchiveSearchCandidateReauthorization.rejected(
                    ArchiveSearchReauthorizationStatus.DENIED
                )
        return ArchiveSearchCandidateReauthorization.authorized(candidate)

    with pytest.raises(ArchiveSearchPublicationDenied) as denied:
        attest_archive_search_before_publication(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer="must not publish",
            candidate_reauthorizer=late_reauth,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )
    assert denied.value.reason is ArchiveSearchPublicationDenialReason.AUTHORITY_CHANGED
    assert seen_runs.count(first_run) == 2
    assert seen_runs.count(second_run) == 2


def test_turn_ledger_assigns_disjoint_monotonic_public_labels_to_three_runs() -> None:
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"label-ordinal-turn-{next(_TURN_SEQUENCE)}",
    )
    batches: list[tuple[ArchiveSearchRunBinding, AuthorizedArchiveBatch]] = []
    for index in range(1, 4):
        run = _run(ledger=ledger, run=f"label-ordinal-run-{index}")
        candidate = _candidate(
            f"raw_{index + 2:016d}",
            ArchiveMatchChannel.LEXICAL,
            title=f"Page {index}",
        )
        batch = _batch(
            run,
            candidates=(candidate,),
            coverage=_coverage(run),
        )
        payload = batch.public_tool_result_payload
        public_ordinal = (index - 1) * 20 + 1
        assert payload["candidates"][0]["label"] == f"A{public_ordinal}"  # type: ignore[index]
        assert payload["candidates"][0]["passages"][0]["label"] == (  # type: ignore[index]
            f"A{public_ordinal}.1"
        )
        assert "model_page_index" not in payload
        batches.append((run, batch))

    bodies = [batch.model_visible_canonical_bytes for _run_binding, batch in batches]
    assert len(set(bodies)) == 3
    for run, batch in batches:
        ledger.admit_model_tool_bytes(run, batch, batch.model_visible_canonical_bytes)
    ledger.freeze_for_publication()
    attestation = attest_archive_search_before_publication(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=ledger,
        answer="Page one [A1.1], page two [A21.1], page three [A41.1].",
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )
    assert attestation.attests_answer("Page one [A1.1], page two [A21.1], page three [A41.1].")
    projection = attestation.candidate_projection
    assert tuple(item.ordinal for item in projection.candidates) == (1, 2, 3)
    assert tuple(item.public_citation_label for item in projection.candidates) == (
        "A1",
        "A21",
        "A41",
    )
    assert tuple(item.source_ref.canonical_object_id for item in projection.candidates) == tuple(
        f"raw_{index:016d}" for index in range(3, 6)
    )


def test_failed_run_ordinal_is_never_reused_by_a_later_page() -> None:
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"label-gap-turn-{next(_TURN_SEQUENCE)}",
    )
    first_run = _run(ledger=ledger, run="label-gap-first")
    _failed_run = _run(ledger=ledger, run="label-gap-failed")
    third_run = _run(ledger=ledger, run="label-gap-third")
    first = _batch(first_run, candidates=(_candidates()[0],), coverage=_coverage(first_run))
    third = _batch(third_run, candidates=(_candidates()[1],), coverage=_coverage(third_run))

    assert first.public_tool_result_payload["candidates"][0]["label"] == "A1"  # type: ignore[index]
    assert third.public_tool_result_payload["candidates"][0]["label"] == "A41"  # type: ignore[index]
    abandon_empty_archive_model_batch_ledger(ledger)


def test_same_run_page_ordinal_can_be_admitted_only_once_under_lock() -> None:
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"label-race-turn-{next(_TURN_SEQUENCE)}",
    )
    run = _run(ledger=ledger, run="label-race-first")
    batches = (_batch(run), _batch(run))
    barrier = threading.Barrier(3)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def admit(batch: AuthorizedArchiveBatch) -> None:
        barrier.wait()
        try:
            ledger.admit_model_tool_bytes(run, batch, batch.model_visible_canonical_bytes)
        except ArchiveSearchAuthorityError:
            outcome = "rejected"
        else:
            outcome = "admitted"
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=admit, args=(batch,)) for batch in batches]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(outcomes) == ["admitted", "rejected"]
    second_run = _run(ledger=ledger, run="label-race-second")
    second = _batch(second_run)
    assert second.public_tool_result_payload["candidates"][0]["label"] == "A21"  # type: ignore[index]
    ledger.admit_model_tool_bytes(second_run, second, second.model_visible_canonical_bytes)
    ledger.freeze_for_publication()
    attestation = attest_archive_search_before_publication(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=ledger,
        answer="Unique page labels [A1.1] [A21.1].",
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )
    assert attestation.attests_answer("Unique page labels [A1.1] [A21.1].")


def test_turn_ledger_cannot_omit_an_admitted_batch_or_move_it_to_an_alternate() -> None:
    turn_id = f"no-omission-turn-{next(_TURN_SEQUENCE)}"
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=turn_id,
    )
    first_run = _run(ledger=ledger, run="no-omission-run-first")
    second_run = _run(ledger=ledger, run="no-omission-run-second")
    first = _batch(first_run)
    second = _batch(second_run)
    ledger.admit_model_tool_bytes(first_run, first, first.model_visible_canonical_bytes)
    ledger.admit_model_tool_bytes(second_run, second, second.model_visible_canonical_bytes)
    ledger.freeze_for_publication()
    assert (
        create_archive_model_batch_ledger(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            turn_discriminator=turn_id,
        )
        is ledger
    )

    original_entries = ledger._entries
    object.__setattr__(ledger, "_entries", original_entries[:1])
    with pytest.raises(ArchiveSearchPublicationDenied) as omitted:
        attest_archive_search_before_publication(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer="must not publish",
            candidate_reauthorizer=_allow_candidate,
            coverage_reauthorizer=_allow_coverage,
            authority_context=CONTEXT,
        )
    assert omitted.value.reason is ArchiveSearchPublicationDenialReason.LEDGER_UNAVAILABLE


def test_carriers_are_nontransferable_tamper_evident_and_public_copy_is_detached() -> None:
    run = _run()
    batch = _batch(run)
    attestation = _attest(run, batch)

    for value in (run, batch, attestation):
        rendered = repr(value) + json.dumps(value, default=str)
        assert QUERY not in rendered and TENANT not in rendered and RAW_ONE not in rendered
        assert not hasattr(value, "to_payload") and not hasattr(value, "to_json")
        with pytest.raises(TypeError):
            json.dumps(value)
        with pytest.raises(TypeError, match="process-private"):
            copy.copy(value)
        with pytest.raises(TypeError, match="process-private"):
            copy.deepcopy(value)
        with pytest.raises(TypeError, match="process-private"):
            pickle.dumps(value)

    public_copy = batch.public_tool_result_payload
    public_copy["schema"] = "tampered"
    assert batch.public_tool_result_payload["schema"] != "tampered"
    with pytest.raises(ArchiveSearchAuthorityError) as bytes_denied:
        _attest(run, batch, visible=batch.model_visible_canonical_bytes + b" ")
    assert bytes_denied.value.__cause__ is None

    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"tamper-turn-{next(_TURN_SEQUENCE)}",
    )
    tamper_run = _run(ledger=ledger, run="tamper-run")
    tamper_batch = _batch(tamper_run)
    ledger.admit_model_tool_bytes(
        tamper_run,
        tamper_batch,
        tamper_batch.model_visible_canonical_bytes,
    )
    ledger.freeze_for_publication()
    object.__setattr__(tamper_batch, "_seal", "f" * 64)
    with pytest.raises(ArchiveSearchPublicationDenied) as carrier_denied:
        _attest(tamper_run, tamper_batch, ledger=ledger)
    assert carrier_denied.value.reason is ArchiveSearchPublicationDenialReason.LEDGER_UNAVAILABLE
    assert carrier_denied.value.__cause__ is None


def test_projector_exception_is_body_free_and_has_no_private_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run()

    def explode(_page: ArchiveSearchPage, _privacy_key: bytes) -> str:
        raise RuntimeError(f"{QUERY} {RAW_ONE} {TENANT}")

    monkeypatch.setattr(ArchiveSearchPage, "to_public_json", explode)
    with pytest.raises(ArchiveSearchAuthorityError) as error:
        _batch(run)

    assert error.value.__cause__ is None
    rendered = str(error.value) + repr(error.value)
    assert QUERY not in rendered and RAW_ONE not in rendered and TENANT not in rendered


def test_empty_open_ledger_can_only_be_abandoned_once_and_is_replay_closed() -> None:
    turn = f"abandoned-empty-turn-{next(_TURN_SEQUENCE)}"
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=turn,
    )
    _run(ledger=ledger, run="abandoned-empty-run")

    abandon_empty_archive_model_batch_ledger(ledger)

    with pytest.raises(ArchiveSearchAuthorityError):
        abandon_empty_archive_model_batch_ledger(ledger)
    with pytest.raises(ArchiveSearchAuthorityError):
        create_archive_model_batch_ledger(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            turn_discriminator=turn,
        )


def test_consumed_turn_filter_rotates_after_replay_retention() -> None:
    now = [0.0]
    replay_filter = authority_module._RotatingTurnReplayFilter(
        filter_bytes=8,
        retain_seconds=6.0,
        bucket_count=3,
        clock=lambda: now[0],
    )
    handle = "a" * 64

    now[0] = 2.99
    replay_filter.add(handle)
    now[0] = 8.98
    assert replay_filter.contains(handle) is True
    now[0] = 9.01
    assert replay_filter.contains(handle) is False

    for epoch in range(32):
        replay_filter.add(f"{epoch:064x}")
        now[0] += 3.01
    now[0] += 10.0
    assert replay_filter.contains("f" * 64) is False


def test_phase_two_attestation_seals_body_free_single_selected_evidence() -> None:
    run = _run()
    batch = _batch(run)
    answer = "The first exact fact is supported here [A1.1]."

    attestation = _attest(run, batch, answer=answer)

    assert attestation.attests_answer(answer)
    assert attestation.coverage_grade is ArchiveSearchCoverageGrade.COMPLETE
    assert attestation.candidate_count == 2
    assert attestation.used_citation_count == 1
    assert attestation.used_citation_labels == ("A1.1",)
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", value)
        for value in (
            attestation.plan_sha256,
            attestation.evidence_sha256,
            attestation.coverage_sha256,
            attestation.answer_sha256,
        )
    )
    selected = attestation.selected_evidence
    assert type(selected) is ArchiveSearchSelectedEvidence
    assert selected.corpus is ArchiveSearchCorpus.DOCUMENTS
    assert selected.source_ref == _candidates()[0].resolved_source.source_ref
    assert selected.passage_refs == (_candidates()[0].passages[0].passage_ref,)
    assert selected.resolved_snapshot_sha256 == archive_selected_evidence_snapshot_sha256(
        _candidates()[0].resolved_source,
        selected.passage_refs,
        (_candidates()[0].passages[0].excerpt,),
    )
    assert selected == ArchiveSearchSelectedEvidence.from_private_payload(selected.to_private_payload())
    body_free = selected.to_private_json() + repr(selected)
    assert QUERY not in body_free
    assert "Model-visible excerpt" not in body_free
    assert "One" not in body_free
    with pytest.raises(ArchiveSearchAuthorityError):
        replace(selected, corpus=selected.corpus.value)
    with pytest.raises(ArchiveSearchAuthorityError):
        replace(selected, corpus=ArchiveSearchCorpus.MESSAGES)
    with pytest.raises(ArchiveSearchAuthorityError):
        replace(selected, resolved_snapshot_sha256=True)
    oversized_payload = selected.to_private_payload()
    raw_passages = cast(list[object], oversized_payload["passage_refs"])
    oversized_payload["passage_refs"] = raw_passages * 9
    with pytest.raises(ArchiveSearchAuthorityError, match="selected passages"):
        ArchiveSearchSelectedEvidence.from_private_payload(oversized_payload)

    outcome = archive_recall_outcome_from_attestation(attestation)
    assert outcome.status is ArchiveRecallStatus.COMPLETE
    assert outcome.selected_evidence == selected
    assert outcome.publication_attested is True
    assert outcome.semantic_verified is False


def test_phase_two_attestation_emits_body_free_ordered_candidate_projection() -> None:
    candidates = _candidates()
    run = _run(run=f"candidate-projection-{next(_TURN_SEQUENCE)}")
    answer = "The first exact fact is here [A1.1], and the second is here [A2.1]."
    attestation = _attest(run, _batch(run, candidates=candidates), answer=answer)

    projection = attestation.candidate_projection
    assert type(projection) is ArchiveSearchAcceptedCandidateProjection
    assert projection.candidate_count == attestation.candidate_count == 2
    assert projection.coverage_grade is attestation.coverage_grade
    assert projection.coverage_sha256 == attestation.coverage_sha256
    assert projection.evidence_sha256 == attestation.evidence_sha256
    assert (
        projection.canonical_sha256
        == hashlib.sha256(projection.to_private_json().encode("ascii")).hexdigest()
    )
    assert tuple(item.ordinal for item in projection.candidates) == (1, 2)
    assert tuple(item.public_citation_label for item in projection.candidates) == ("A1", "A2")
    assert tuple(item.source_ref for item in projection.candidates) == tuple(
        item.resolved_source.source_ref for item in candidates
    )
    assert tuple(item.passage_refs for item in projection.candidates) == tuple(
        tuple(passage.passage_ref for passage in item.passages) for item in candidates
    )
    assert tuple(item.resolved_snapshot_sha256 for item in projection.candidates) == tuple(
        archive_selected_evidence_snapshot_sha256(
            item.resolved_source,
            tuple(passage.passage_ref for passage in item.passages),
            tuple(passage.excerpt for passage in item.passages),
        )
        for item in candidates
    )

    payload = projection.to_private_payload()
    assert frozenset(payload) == frozenset(
        {
            "candidate_count",
            "candidates",
            "coverage_grade",
            "coverage_sha256",
            "evidence_sha256",
            "schema",
        }
    )
    assert payload["schema"] == ARCHIVE_SEARCH_ACCEPTED_CANDIDATE_PROJECTION_SCHEMA
    raw_candidates = cast(list[dict[str, object]], payload["candidates"])
    assert all(
        frozenset(item)
        == frozenset(
            {
                "corpus",
                "ordinal",
                "passage_refs",
                "public_citation_label",
                "resolved_snapshot_sha256",
                "schema",
                "source_ref",
            }
        )
        and item["schema"] == ARCHIVE_SEARCH_CANDIDATE_PROJECTION_ENTRY_SCHEMA
        for item in raw_candidates
    )
    rendered = (
        projection.to_private_json()
        + repr(projection)
        + "".join(repr(item) for item in projection.candidates)
    )
    for forbidden in (
        QUERY,
        "Model-visible excerpt",
        "Bound final answer",
        answer,
        '"title"',
        '"filename"',
        '"excerpt"',
        "candidate_handle",
        "continuation",
        "model_visible",
    ):
        assert forbidden not in rendered

    payload["coverage_grade"] = "tampered"
    raw_candidates[0]["ordinal"] = 99
    assert projection.coverage_grade is ArchiveSearchCoverageGrade.COMPLETE
    assert projection.candidates[0].ordinal == 1


def test_candidate_projection_uses_first_base_citation_order_and_exact_passage_union() -> None:
    first, second = _candidates()
    first_passage = first.passages[0]
    second_passage = ArchiveSearchPassage(
        replace(
            first_passage.passage_ref,
            locator=TextSpanLocator(chunk_index=1, start_char=24, end_char=48),
        ),
        "Second private projection excerpt",
    )
    multi_passage = replace(first, passages=(first_passage, second_passage))
    candidates = (multi_passage, second)
    answer = "Second source first [A2.1], then one exact passage [A1.2], repeated [A1.2]."
    run = _run(run=f"candidate-citation-order-{next(_TURN_SEQUENCE)}")
    attestation = _attest(run, _batch(run, candidates=candidates), answer=answer)

    projection = attestation.candidate_projection
    assert attestation.candidate_count == projection.candidate_count == 2
    assert tuple(item.public_citation_label for item in projection.candidates) == ("A2", "A1")
    assert tuple(item.ordinal for item in projection.candidates) == (1, 2)
    assert projection.candidates[0].source_ref == second.resolved_source.source_ref
    assert projection.candidates[0].passage_refs == (second.passages[0].passage_ref,)
    assert projection.candidates[1].source_ref == multi_passage.resolved_source.source_ref
    assert projection.candidates[1].passage_refs == (second_passage.passage_ref,)
    assert projection.candidates[1].resolved_snapshot_sha256 == (
        archive_selected_evidence_snapshot_sha256(
            multi_passage.resolved_source,
            (second_passage.passage_ref,),
            (second_passage.excerpt,),
        )
    )
    assert second_passage.excerpt not in projection.to_private_json()

    base_run = _run(run=f"candidate-base-citation-{next(_TURN_SEQUENCE)}")
    base_projection = _attest(
        base_run,
        _batch(base_run, candidates=(multi_passage,)),
        answer="The whole candidate is cited [A1].",
    ).candidate_projection
    assert base_projection.candidates[0].passage_refs == tuple(
        item.passage_ref for item in multi_passage.passages
    )


def test_candidate_projection_omits_uncited_search_candidates_without_changing_outcome_count() -> None:
    candidates = _candidates()
    run = _run(run=f"candidate-omitted-{next(_TURN_SEQUENCE)}")
    answer = "Only the second search candidate is cited [A2.1]."
    attestation = _attest(run, _batch(run, candidates=candidates), answer=answer)

    projection = attestation.candidate_projection
    assert attestation.candidate_count == 2
    assert projection.candidate_count == 1
    assert projection.candidates[0].ordinal == 1
    assert projection.candidates[0].public_citation_label == "A2"
    assert projection.candidates[0].source_ref == candidates[1].resolved_source.source_ref


def test_candidate_projection_excludes_navigation_and_unsupported_factual_shapes() -> None:
    first, second = _candidates()
    navigation = replace(
        first,
        evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
        passages=(),
    )
    knowledge_revision = next(
        item
        for item in second.resolved_source.revisions
        if item.representation.kind is RepresentationKind.KNOWLEDGE_OBJECT
    )
    incompatible_passage = replace(
        second.passages[0],
        passage_ref=replace(
            second.passages[0].passage_ref,
            source_revision=knowledge_revision,
        ),
    )
    unsupported = replace(second, passages=(incompatible_passage,))
    run = _run(run=f"candidate-exclusions-{next(_TURN_SEQUENCE)}")
    answer = "Navigation [A1] and an unsupported factual shape [A2.1]."
    attestation = _attest(
        run,
        _batch(run, candidates=(navigation, unsupported)),
        answer=answer,
    )

    assert attestation.attests_answer(answer)
    assert attestation.candidate_count == 2
    assert attestation.candidate_projection.candidate_count == 0
    assert attestation.candidate_projection.candidates == ()


@pytest.mark.parametrize(
    "answer",
    (
        "A known citation plus a malformed one [A1.1] [A1.9].",
        "A known citation plus an unknown one [A1.1] [A3.1].",
    ),
)
def test_malformed_or_unknown_citation_closes_candidate_projection(answer: str) -> None:
    run = _run(run=f"candidate-closed-{next(_TURN_SEQUENCE)}")
    attestation = _attest(run, _batch(run), answer=answer)

    projection = attestation.candidate_projection
    assert attestation.attests_answer(answer)
    assert not attestation.attests_answer(answer + " changed")
    assert attestation.candidate_count == 2
    assert projection.candidate_count == 0
    assert projection.candidates == ()
    rendered = projection.to_private_json() + repr(projection)
    assert answer not in rendered
    assert QUERY not in rendered
    assert "Model-visible excerpt" not in rendered


def test_candidate_projection_label_preview_is_frozen_non_consuming_and_exact() -> None:
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"candidate-preview-{next(_TURN_SEQUENCE)}",
    )
    run = _run(ledger=ledger, run=f"candidate-preview-run-{next(_TURN_SEQUENCE)}")
    batch = _batch(run)
    answer = "Second first [A2.1], then first [A1.1]."
    ledger.admit_model_tool_bytes(run, batch, batch.model_visible_canonical_bytes)

    assert (
        preview_archive_search_candidate_projection_labels(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer=answer,
        )
        == ()
    )
    ledger.freeze_for_publication()
    assert (
        preview_archive_search_candidate_projection_labels(
            tenant_id=TENANT,
            principal_id="wrong-principal",
            ledger=ledger,
            answer=answer,
        )
        == ()
    )
    assert (
        preview_archive_search_candidate_projection_labels(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer=answer + " Unknown [A3.1].",
        )
        == ()
    )
    labels = preview_archive_search_candidate_projection_labels(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=ledger,
        answer=answer,
    )
    assert labels == ("A2", "A1")
    final_answer = answer + "\n\n1 — A2\n2 — A1"
    assert (
        preview_archive_search_candidate_projection_labels(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer=final_answer,
        )
        == labels
    )

    attestation = _attest(
        run,
        batch,
        ledger=ledger,
        answer=final_answer,
    )
    assert tuple(item.public_citation_label for item in attestation.candidate_projection.candidates) == labels
    assert attestation.attests_answer(final_answer)
    assert (
        preview_archive_search_candidate_projection_labels(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer=final_answer,
        )
        == ()
    )


def test_duplicate_source_citations_merge_under_first_visible_label_or_close() -> None:
    original = _candidates()[0]
    first_passage = original.passages[0]
    second_passage = ArchiveSearchPassage(
        replace(
            first_passage.passage_ref,
            locator=TextSpanLocator(chunk_index=1, start_char=24, end_char=48),
        ),
        "Second private duplicate-source excerpt",
    )
    first_candidate = replace(original, passages=(first_passage,))
    second_candidate = replace(original, passages=(second_passage,))
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"duplicate-source-{next(_TURN_SEQUENCE)}",
    )
    first_run = _run(ledger=ledger, run=f"duplicate-source-first-{next(_TURN_SEQUENCE)}")
    second_run = _run(ledger=ledger, run=f"duplicate-source-second-{next(_TURN_SEQUENCE)}")
    first_batch = _batch(first_run, candidates=(first_candidate,))
    second_batch = _batch(second_run, candidates=(second_candidate,))
    ledger.admit_model_tool_bytes(first_run, first_batch, first_batch.model_visible_canonical_bytes)
    ledger.admit_model_tool_bytes(second_run, second_batch, second_batch.model_visible_canonical_bytes)
    ledger.freeze_for_publication()
    answer = "The later label occurs first [A21.1], then the earlier label [A1.1]."

    assert preview_archive_search_candidate_projection_labels(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=ledger,
        answer=answer,
    ) == ("A21",)
    attestation = _attest(
        first_run,
        first_batch,
        ledger=ledger,
        answer=answer,
    )
    projection = attestation.candidate_projection
    assert attestation.candidate_count == 2
    assert projection.candidate_count == 1
    assert projection.candidates[0].public_citation_label == "A21"
    assert projection.candidates[0].source_ref == original.resolved_source.source_ref
    assert projection.candidates[0].passage_refs == (
        first_passage.passage_ref,
        second_passage.passage_ref,
    )

    conflict_ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"duplicate-source-conflict-{next(_TURN_SEQUENCE)}",
    )
    conflict_first_run = _run(
        ledger=conflict_ledger,
        run=f"duplicate-conflict-first-{next(_TURN_SEQUENCE)}",
    )
    conflict_second_run = _run(
        ledger=conflict_ledger,
        run=f"duplicate-conflict-second-{next(_TURN_SEQUENCE)}",
    )
    conflict_first_batch = _batch(conflict_first_run, candidates=(first_candidate,))
    conflicting = replace(
        first_candidate,
        passages=(replace(first_passage, excerpt="Conflicting private body"),),
    )
    conflict_second_batch = _batch(conflict_second_run, candidates=(conflicting,))
    conflict_ledger.admit_model_tool_bytes(
        conflict_first_run,
        conflict_first_batch,
        conflict_first_batch.model_visible_canonical_bytes,
    )
    conflict_ledger.admit_model_tool_bytes(
        conflict_second_run,
        conflict_second_batch,
        conflict_second_batch.model_visible_canonical_bytes,
    )
    conflict_ledger.freeze_for_publication()
    conflict_answer = "Conflicting snapshots [A1.1] [A21.1]."
    assert (
        preview_archive_search_candidate_projection_labels(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=conflict_ledger,
            answer=conflict_answer,
        )
        == ()
    )
    conflict_attestation = _attest(
        conflict_first_run,
        conflict_first_batch,
        ledger=conflict_ledger,
        answer=conflict_answer,
    )
    assert conflict_attestation.attests_answer(conflict_answer)
    assert conflict_attestation.candidate_projection.candidates == ()


def test_candidate_projection_snapshot_changes_with_unstored_body_bytes() -> None:
    original = _candidates()[0]
    changed = replace(
        original,
        passages=(replace(original.passages[0], excerpt="Different private exact body"),),
    )
    projections: list[ArchiveSearchAcceptedCandidateProjection] = []
    for index, candidate in enumerate((original, changed), 1):
        run = _run(run=f"candidate-snapshot-{index}-{next(_TURN_SEQUENCE)}")
        projections.append(
            _attest(
                run,
                _batch(run, candidates=(candidate,)),
                answer="One accepted source [A1.1].",
            ).candidate_projection
        )

    first, second = (item.candidates[0] for item in projections)
    assert first.source_ref == second.source_ref
    assert first.passage_refs == second.passage_refs
    assert first.resolved_snapshot_sha256 != second.resolved_snapshot_sha256
    rendered = "".join(item.to_private_json() for item in projections)
    assert original.passages[0].excerpt not in rendered
    assert changed.passages[0].excerpt not in rendered

    navigation = replace(
        original,
        evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
        passages=(),
    )
    navigation_run = _run(run=f"candidate-navigation-{next(_TURN_SEQUENCE)}")
    navigation_projection = _attest(
        navigation_run,
        _batch(navigation_run, candidates=(navigation,)),
        answer="One navigation result [A1].",
    ).candidate_projection
    assert navigation_projection.candidate_count == 0
    assert navigation_projection.candidates == ()


def test_candidate_projection_is_phase_two_only_and_tamper_evident() -> None:
    with pytest.raises(ArchiveSearchAuthorityError, match="phase-2"):
        ArchiveSearchAcceptedCandidateProjection()

    run = _run(run=f"candidate-projection-carrier-{next(_TURN_SEQUENCE)}")
    batch = _batch(run)
    assert not hasattr(batch, "candidate_projection")
    attestation = _attest(run, batch, answer="Accepted answer [A1.1].")
    projection = attestation.candidate_projection
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError, match="process-private"):
            operation(projection)

    for index, mutation in enumerate(("order", "coverage", "citation", "source", "snapshot"), 1):
        current_run = _run(run=f"candidate-projection-tamper-{index}-{next(_TURN_SEQUENCE)}")
        current_answer = "Accepted answer [A1.1] and [A2.1]."
        current_attestation = _attest(
            current_run,
            _batch(current_run),
            answer=current_answer,
        )
        current_projection = current_attestation.candidate_projection
        if mutation == "order":
            object.__setattr__(
                current_projection,
                "_candidates",
                tuple(reversed(current_projection.candidates)),
            )
        elif mutation == "coverage":
            object.__setattr__(current_projection, "_coverage_sha256", "f" * 64)
        elif mutation == "citation":
            entry = current_projection.candidates[0]
            object.__setattr__(entry, "public_citation_label", "A3")
        elif mutation == "source":
            entry = current_projection.candidates[0]
            object.__setattr__(entry, "source_ref", _candidates()[1].resolved_source.source_ref)
        else:
            entry = current_projection.candidates[0]
            object.__setattr__(entry, "resolved_snapshot_sha256", "f" * 64)
        assert current_attestation.attests_answer(current_answer) is False
        with pytest.raises(ArchiveSearchAuthorityError, match="projection is unavailable"):
            _ = current_projection.candidates


def test_phase_two_selection_requires_all_citations_to_name_one_candidate() -> None:
    cases = (
        ("No citation was used.", ()),
        ("Two candidates [A1.1] and [A2.1].", ("A1.1", "A2.1")),
        ("One real and one invented [A1.1] [A999.1].", ("A1.1",)),
        ("One real and one malformed group [A1.1] [A2.1, A2].", ("A1.1",)),
        ("One real and one zero label [A1.1] [A0].", ("A1.1",)),
        ("One real and one leading-zero label [A1.1] [A01.1].", ("A1.1",)),
        ("One real and one bare label [A1.1] [A].", ("A1.1",)),
    )
    for answer, labels in cases:
        run = _run(run=f"selection-case-{next(_TURN_SEQUENCE)}")
        attestation = _attest(run, _batch(run), answer=answer)
        assert attestation.used_citation_labels == labels
        assert attestation.selected_evidence is None


def test_noncanonical_factual_candidate_never_becomes_selected_evidence() -> None:
    canonical = _candidates()[0]
    inbox = SourceRepresentation(RepresentationKind.INBOX_ITEM, "inbox_3333333333333333")
    resolved = ResolvedSource.create(
        source_ref=canonical.resolved_source.source_ref,
        representations=(*canonical.resolved_source.representations, inbox),
        lifecycle=(
            *canonical.resolved_source.lifecycle,
            LifecycleRef(inbox, LifecycleState.PENDING),
        ),
        revisions=canonical.resolved_source.revisions,
        revalidation_targets=(
            *canonical.resolved_source.revalidation_targets,
            RevalidationTarget(inbox, AuthorityScope.TENANT_PRINCIPAL),
        ),
    )
    noncanonical = replace(
        canonical,
        resolved_source=resolved,
        review_state=ArchiveReviewState.PENDING,
        evidence_authority=ArchiveEvidenceAuthority.NONCANONICAL,
        lifecycle_state=LifecycleState.PENDING,
    )
    run = _run()
    attestation = _attest(
        run,
        _batch(run, candidates=(noncanonical,), coverage=_coverage(run)),
        answer="Pending material was cited [A1.1].",
    )

    assert attestation.candidate_count == 1
    assert attestation.used_citation_labels == ("A1.1",)
    assert attestation.selected_evidence is None


def test_incompatible_selected_shape_does_not_deny_valid_archive_publication() -> None:
    canonical = _candidates()[0]
    knowledge_revision = next(
        item
        for item in canonical.resolved_source.revisions
        if item.representation.kind is RepresentationKind.KNOWLEDGE_OBJECT
    )
    incompatible_passage = replace(
        canonical.passages[0],
        passage_ref=replace(
            canonical.passages[0].passage_ref,
            source_revision=knowledge_revision,
        ),
    )
    incompatible = replace(canonical, passages=(incompatible_passage,))
    run = _run()
    answer = "The contract-valid archive passage was cited [A1.1]."

    attestation = _attest(
        run,
        _batch(run, candidates=(incompatible,)),
        answer=answer,
    )

    assert attestation.attests_answer(answer)
    assert attestation.used_citation_labels == ("A1.1",)
    assert attestation.selected_evidence is None


def test_phase_two_projection_distinguishes_partial_and_complete_empty_coverage() -> None:
    partial_run = _run()
    partial_coverage = list(_coverage(partial_run))
    partial_coverage[0] = replace(
        partial_coverage[0],
        states=(CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL),
    )
    partial = _attest(
        partial_run,
        _batch(partial_run, coverage=tuple(partial_coverage)),
        answer="A bounded result [A1.1].",
    )
    assert partial.coverage_grade is ArchiveSearchCoverageGrade.PARTIAL
    assert archive_recall_outcome_from_attestation(partial).status is ArchiveRecallStatus.PARTIAL

    empty_run = _run()
    empty = _attest(
        empty_run,
        _batch(empty_run, candidates=(), coverage=_coverage(empty_run, zero=True)),
        answer="No authorized matches were present.",
    )
    assert empty.coverage_grade is ArchiveSearchCoverageGrade.COMPLETE
    assert empty.candidate_count == 0
    assert empty.selected_evidence is None
    assert archive_recall_outcome_from_attestation(empty).status is ArchiveRecallStatus.EMPTY

    incomplete_run = _run()
    incomplete_coverage = list(_coverage(incomplete_run, zero=True))
    incomplete_coverage[0] = replace(
        incomplete_coverage[0],
        states=(CoverageState.BACKFILL_PENDING, CoverageState.PARTIAL),
    )
    incomplete = _attest(
        incomplete_run,
        _batch(incomplete_run, candidates=(), coverage=tuple(incomplete_coverage)),
        answer="Coverage was incomplete, so absence was not established.",
    )
    assert incomplete.coverage_grade is ArchiveSearchCoverageGrade.PARTIAL
    assert incomplete.candidate_count == 0
    assert archive_recall_outcome_from_attestation(incomplete).status is ArchiveRecallStatus.INCOMPLETE_EMPTY


def test_ordinary_model_gate_rejects_an_unredeemed_inbound_continuation() -> None:
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"unredeemed-turn-{next(_TURN_SEQUENCE)}",
    )
    origin = _run(run="unredeemed-origin", request=_request(limit=1), ledger=ledger)
    token, _issued = _issue_public_continuation(origin)
    forged = _run(
        run="unredeemed-forged",
        request=_request(limit=1, continuation=token),
        ledger=ledger,
    )

    with pytest.raises(ArchiveSearchAuthorityError, match="model admission failed"):
        _batch(forged)


@pytest.mark.parametrize("warning", tuple(ArchiveSearchWarning))
def test_single_page_warning_cannot_claim_complete_coverage(
    warning: ArchiveSearchWarning,
) -> None:
    run = _run(run=f"warning-grade-{warning.value}-{next(_TURN_SEQUENCE)}")
    batch = authorize_archive_search_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=run,
        candidates=_candidates(),
        coverage=_coverage(run),
        warnings=(warning,),
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )

    attestation = _attest(run, batch, answer="A bounded result [A1.1].")

    assert batch.public_tool_result_payload["exhaustive"] is True
    assert attestation.coverage_grade is ArchiveSearchCoverageGrade.PARTIAL


def test_redeemed_exhaustive_chain_is_complete_despite_pagination_warning() -> None:
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"complete-chain-{next(_TURN_SEQUENCE)}",
    )
    origin = _run(run="complete-chain-origin", request=_request(limit=20), ledger=ledger)
    tail = _candidates()
    terminal = _terminal_coverage_for_tail(origin, tail)
    issued = issue_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=origin,
        tail_candidates=tail,
        terminal_coverage=terminal,
        warnings=(ArchiveSearchWarning.LANE_CAPPED,),
    )
    initial = authorize_archive_search_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=origin,
        candidates=(),
        coverage=_continuing_coverage(origin, terminal, tail),
        warnings=(ArchiveSearchWarning.LANE_CAPPED,),
        continuation=issued,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )
    token = cast(str, initial.public_tool_result_payload["continuation"])
    ledger.admit_model_tool_bytes(origin, initial, initial.model_visible_canonical_bytes)

    resumed = _run(
        run="complete-chain-resumed",
        request=_request(limit=20, continuation=token),
        ledger=ledger,
    )
    redemption = redeem_archive_search_continuation(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
    )
    final = authorize_archive_search_resumed_before_model(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        run_binding=resumed,
        redemption=redemption,
        candidate_reauthorizer=_allow_candidate,
        coverage_reauthorizer=_allow_coverage,
        authority_context=CONTEXT,
    )
    assert final.public_tool_result_payload["warnings"] == ["lane_capped"]
    assert final.public_tool_result_payload["continuation"] is None
    ledger.admit_model_tool_bytes(resumed, final, final.model_visible_canonical_bytes)
    ledger.freeze_for_publication()

    attestation = _attest(
        resumed,
        final,
        answer="The exact fact is cited [A21.1].",
        ledger=ledger,
    )
    assert attestation.coverage_grade is ArchiveSearchCoverageGrade.COMPLETE
    assert archive_recall_outcome_from_attestation(attestation).status is ArchiveRecallStatus.COMPLETE


def test_phase_two_projection_fields_are_inside_the_attestation_seal() -> None:
    run = _run()
    answer = "Exact fact [A1.1]."
    attestation = _attest(run, _batch(run), answer=answer)
    object.__setattr__(attestation, "_candidate_count", attestation.candidate_count + 1)

    assert attestation.attests_answer(answer) is False
    with pytest.raises(ArchiveSearchAuthorityError, match="attestation is unavailable"):
        _ = attestation.selected_evidence
