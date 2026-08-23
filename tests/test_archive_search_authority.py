from __future__ import annotations

import copy
import hashlib
import itertools
import json
import pickle
import threading
from dataclasses import replace
from typing import cast

import pytest

import friday.retrieval.archive_search_authority as authority_module
from friday.retrieval.archive_search_authority import (
    ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES,
    ARCHIVE_AUTHORITY_MAX_MODEL_BYTES,
    ArchiveModelBatchLedger,
    ArchiveSearchAuthorityError,
    ArchiveSearchAuthorityPhase,
    ArchiveSearchCandidateReauthorization,
    ArchiveSearchCoverageReauthorization,
    ArchiveSearchPublicationDenialReason,
    ArchiveSearchPublicationDenied,
    ArchiveSearchReauthorizationStatus,
    ArchiveSearchRunBinding,
    AuthorizedArchiveBatch,
    IssuedArchiveContinuation,
    RedeemedArchiveContinuation,
    attest_archive_search_before_publication,
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
    first_coverage = {
        item["lane"]: item for item in cast(list[dict[str, object]], first_payload["coverage"])
    }
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

    assert (
        create_archive_model_batch_ledger(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            turn_discriminator=turn_id,
        )
        is ledger
    )
    with pytest.raises(ArchiveSearchAuthorityError):
        _run(ledger=ledger, run="cannot-restart-consumed-ledger")

    bounded = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"bounded-turn-{next(_TURN_SEQUENCE)}",
    )
    bounded_run = _run(ledger=bounded, run="bounded-run")
    monkeypatch.setattr(authority_module, "ARCHIVE_AUTHORITY_MAX_MODEL_BYTES", 10_000_000)
    for _index in range(ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES):
        current = _batch(bounded_run)
        bounded.admit_model_tool_bytes(
            bounded_run,
            current,
            current.model_visible_canonical_bytes,
        )
    overflow = _batch(bounded_run)
    with pytest.raises(ArchiveSearchAuthorityError):
        bounded.admit_model_tool_bytes(
            bounded_run,
            overflow,
            overflow.model_visible_canonical_bytes,
        )


def test_model_batch_ledger_enforces_aggregate_byte_cap() -> None:
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"aggregate-turn-{next(_TURN_SEQUENCE)}",
    )
    run = _run(ledger=ledger, run="aggregate-run")
    accepted_bytes = 0
    rejected_size = 0
    for _index in range(ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES):
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


def test_turn_ledger_cannot_omit_an_admitted_batch_or_move_it_to_an_alternate() -> None:
    turn_id = f"no-omission-turn-{next(_TURN_SEQUENCE)}"
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=turn_id,
    )
    run = _run(ledger=ledger, run="no-omission-run")
    first = _batch(run)
    second = _batch(run)
    ledger.admit_model_tool_bytes(run, first, first.model_visible_canonical_bytes)
    ledger.admit_model_tool_bytes(run, second, second.model_visible_canonical_bytes)
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
