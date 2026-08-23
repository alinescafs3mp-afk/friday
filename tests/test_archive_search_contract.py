from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from friday.execution_kernel import ToolResult
from friday.retrieval.archive_search_contract import (
    ArchiveContextWindow,
    ArchiveEvidenceAuthority,
    ArchiveLifecycleConstraint,
    ArchiveMatchChannel,
    ArchiveMatchRank,
    ArchiveReviewState,
    ArchiveSearchCandidate,
    ArchiveSearchCorpus,
    ArchiveSearchPage,
    ArchiveSearchPassage,
    ArchiveSearchRequest,
    ArchiveSearchWarning,
    ArchiveTemporalConstraint,
    ConversationScope,
    ReviewScope,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    CoverageState,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    LifecycleRef,
    LifecycleState,
    MessageRole,
    PassageRef,
    RepresentationKind,
    ResolvedSource,
    RetrievalContractError,
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
    TemporalFact,
    TemporalOrigin,
    TemporalPrecision,
    TemporalRole,
    TemporalValueKind,
    TextSpanLocator,
)

_KEY = b"a" * 32
_OTHER_KEY = b"b" * 32
_RAW_ID = "raw_0123456789abcdef"
_OTHER_RAW_ID = "raw_fedcba9876543210"
_KO_ID = "ko_0123456789abcdef"
_OTHER_KO_ID = "ko_fedcba9876543210"


def _snapshot(
    raw_id: str = _RAW_ID,
    ko_id: str = _KO_ID,
    source_kind: SourceKind = SourceKind.DOCUMENT,
    inbox_state: LifecycleState | None = None,
    raw_state: LifecycleState = LifecycleState.ACTIVE,
    knowledge_state: LifecycleState = LifecycleState.ACTIVE,
) -> tuple[ResolvedSource, SourceRevision, SourceRevision]:
    source_ref = SourceRef(
        source_kind,
        AuthorityScope.TENANT_PRINCIPAL,
        "tenant-private",
        "owner-private",
        CanonicalObjectKind.RAW_OBJECT,
        raw_id,
    )
    raw = SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id)
    knowledge = SourceRepresentation(RepresentationKind.KNOWLEDGE_OBJECT, ko_id)
    raw_revision = SourceRevision(raw, RevisionKind.RAW_CONTENT_SHA256, "d" * 64)
    knowledge_revision = SourceRevision(knowledge, RevisionKind.KNOWLEDGE_VERSION, "1")
    representations = [raw, knowledge]
    lifecycle = [
        LifecycleRef(raw, raw_state),
        LifecycleRef(knowledge, knowledge_state),
    ]
    targets = [
        RevalidationTarget(raw, AuthorityScope.TENANT_PRINCIPAL),
        RevalidationTarget(knowledge, AuthorityScope.TENANT_PRINCIPAL),
    ]
    if inbox_state is not None:
        inbox = SourceRepresentation(RepresentationKind.INBOX_ITEM, raw_id.replace("raw_", "inbox_"))
        representations.append(inbox)
        lifecycle.append(LifecycleRef(inbox, inbox_state))
        targets.append(RevalidationTarget(inbox, AuthorityScope.TENANT_PRINCIPAL))
    return (
        ResolvedSource.create(
            source_ref=source_ref,
            representations=representations,
            lifecycle=lifecycle,
            revisions=(raw_revision, knowledge_revision),
            revalidation_targets=targets,
        ),
        raw_revision,
        knowledge_revision,
    )


def _single_representation_snapshot(
    source_kind: SourceKind,
    lifecycle_state: LifecycleState,
) -> tuple[ResolvedSource, SourceRevision]:
    if source_kind is SourceKind.OBSIDIAN_NOTE:
        scope = AuthorityScope.PRINCIPAL
        tenant_id = None
        principal_id = "owner-private"
        object_kind = CanonicalObjectKind.OBSIDIAN_BINDING
        representation_kind = RepresentationKind.OBSIDIAN_BINDING
        object_id = "obsbind_0123456789abcdef"
        revision_kind = RevisionKind.OBSIDIAN_REVISION_SHA256
        revision_value = "e" * 64
    elif source_kind is SourceKind.EXTERNAL_REGISTERED_SOURCE:
        scope = AuthorityScope.TENANT
        tenant_id = "tenant-private"
        principal_id = None
        object_kind = CanonicalObjectKind.EXTERNAL_SOURCE
        representation_kind = RepresentationKind.EXTERNAL_SOURCE
        object_id = "registered-private-source"
        revision_kind = RevisionKind.EXTERNAL_REVISION
        revision_value = "revision-private"
    else:  # pragma: no cover - fixture is intentionally closed
        raise AssertionError("unsupported single-representation fixture")
    source_ref = SourceRef(
        source_kind,
        scope,
        tenant_id,
        principal_id,
        object_kind,
        object_id,
    )
    representation = SourceRepresentation(representation_kind, object_id)
    revision = SourceRevision(representation, revision_kind, revision_value)
    return (
        ResolvedSource.create(
            source_ref=source_ref,
            representations=(representation,),
            lifecycle=(LifecycleRef(representation, lifecycle_state),),
            revisions=(revision,),
            revalidation_targets=(RevalidationTarget(representation, scope),),
        ),
        revision,
    )


def _passage(
    snapshot: ResolvedSource,
    revision: SourceRevision,
    *,
    excerpt: str = "Exact factual excerpt",
) -> ArchiveSearchPassage:
    passage_ref = PassageRef.from_resolved_source(
        snapshot,
        source_revision=revision,
        locator=TextSpanLocator(chunk_index=0, start_char=10, end_char=31),
        passage_index_version="archive-char-v1",
        embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )
    return ArchiveSearchPassage(passage_ref, excerpt)


def _request(**overrides: object) -> ArchiveSearchRequest:
    payload: dict[str, object] = {
        "query": "  private   quarterly report  ",
        "corpora": ["knowledge", "documents"],
    }
    payload.update(overrides)
    return ArchiveSearchRequest.parse(payload)


def _coverage(
    request: ArchiveSearchRequest,
    *,
    states: tuple[CoverageState, ...] = (CoverageState.COMPLETE,),
    cursor: bool = False,
    matched_at_least: int = 1,
    returned: int = 1,
) -> tuple[SearchCoverage, ...]:
    targets = (
        (SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL),
        (SearchCorpus.KNOWLEDGE, SearchLane.LEXICAL),
    )
    binding = SearchExecutionBinding.create(
        normalized_private_request_json=request.to_identity_json(),
        authority_scope=AuthorityScope.TENANT_PRINCIPAL,
        tenant_id="tenant-private",
        principal_id="owner-private",
        requested_targets=targets,
        snapshot_discriminator="snapshot-private",
        run_discriminator="run-private",
        privacy_key=_KEY,
    )
    return tuple(
        SearchCoverage.create(
            corpus=corpus,
            lane=lane,
            execution_binding=binding,
            states=states,
            eligible_authorized=10 if cursor else max(1, matched_at_least),
            examined=5 if cursor else max(1, matched_at_least),
            matched_at_least=matched_at_least,
            returned=returned,
            authority_rechecked=True,
            snapshot_current=True,
            limit=1 if cursor else None,
            next_cursor_available=cursor,
        )
        for corpus, lane in targets
    )


def _factual_candidate() -> ArchiveSearchCandidate:
    snapshot, _raw_revision, knowledge_revision = _snapshot()
    temporal = TemporalFact.for_instant(
        role=TemporalRole.RECEIVED_AT,
        value=datetime(2026, 8, 23, 8, 22, tzinfo=UTC),
        origin=TemporalOrigin.STORAGE_COLUMN,
        source_revision=knowledge_revision,
    )
    return ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        resolved_source=snapshot,
        review_state=ArchiveReviewState.CONFIRMED,
        evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
        lifecycle_state=LifecycleState.ACTIVE,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
        title="Quarterly report",
        filename="report.md",
        temporal_facts=(temporal,),
        passages=(_passage(snapshot, knowledge_revision),),
    )


def test_model_request_is_closed_normalized_sorted_and_private_round_trips() -> None:
    request = ArchiveSearchRequest.parse(
        {
            "query": "  secret   query  ",
            "corpora": ["web", "documents", "external"],
            "title_hints": ["Zulu", "Alpha"],
            "filename_hints": ["z.md", "a.md"],
            "temporal_constraints": [
                {
                    "corpus": "documents",
                    "role": "received_at",
                    "value_kind": "instant",
                    "precision": "instant",
                    "start": "2026-08-01T00:00:00+00:00",
                    "end": "2026-09-01T00:00:00+00:00",
                }
            ],
            "review_scope": "confirmed_only",
            "limit": 20,
            "continuation": "opaque_token-17",
        }
    )

    assert request.query == "secret query"
    assert request.corpora == (
        ArchiveSearchCorpus.DOCUMENTS,
        ArchiveSearchCorpus.EXTERNAL,
        ArchiveSearchCorpus.WEB,
    )
    assert request.title_hints == ("Alpha", "Zulu")
    assert request.filename_hints == ("a.md", "z.md")
    assert request.review_scope is ReviewScope.CONFIRMED_ONLY
    assert request.permits_outbound is False
    assert ArchiveSearchRequest.parse(request.to_private_json()) == request
    resumed = ArchiveSearchRequest.parse(
        {
            **{
                key: value
                for key, value in request.to_private_payload().items()
                if key not in {"schema", "continuation"}
            },
            "continuation": "different_page_token",
        }
    )
    assert resumed.to_private_json() != request.to_private_json()
    assert resumed.to_identity_json() == request.to_identity_json()
    assert resumed.identity_digest_material() == request.identity_digest_material()
    assert "continuation" not in request.to_identity_payload()
    assert "secret query" not in repr(request)
    assert "opaque_token-17" not in repr(request)


@pytest.mark.parametrize(
    "extra",
    [
        {"raw_object_id": _RAW_ID},
        {"owner_id": "owner-private"},
        {"knowledge_object_id": _KO_ID},
        {"conversation_id": "conv_0123456789abcdef"},
        {"binding_id": "obsbind_0123456789abcdef"},
        {"provider": "internet"},
        {"outbound": True},
    ],
)
def test_request_rejects_private_ids_outbound_controls_and_every_extra_key(
    extra: dict[str, object],
) -> None:
    with pytest.raises(RetrievalContractError, match="keys"):
        ArchiveSearchRequest.parse({"query": "needle", "corpora": ["documents"], **extra})


def test_query_hint_limit_and_canonical_json_boundaries() -> None:
    assert len(ArchiveSearchRequest.parse({"query": "я" * 1_000, "corpora": ["documents"]}).query) == 1_000
    with pytest.raises(RetrievalContractError, match="1000"):
        ArchiveSearchRequest.parse({"query": "я" * 1_001, "corpora": ["documents"]})

    hints = [f"{index:02d}-" + "x" * 257 for index in range(8)]
    assert len(_request(title_hints=hints).title_hints) == 8
    for invalid in (hints + ["ninth"], ["same", "same"], ["x" * 261]):
        with pytest.raises(RetrievalContractError):
            _request(filename_hints=invalid)

    canonical = _request().to_private_json()
    with pytest.raises(RetrievalContractError, match="canonical"):
        ArchiveSearchRequest.parse_private(json.dumps(json.loads(canonical)))
    with pytest.raises(RetrievalContractError, match="duplicate"):
        ArchiveSearchRequest.parse_private(canonical[:-1] + ',"query":"second"}')

    unicode_request = ArchiveSearchRequest.parse(
        {"query": "😀" * 1_000, "corpora": ["documents", "knowledge"]}
    )
    assert all(
        item.execution_binding.attests_private_request(unicode_request.to_identity_json())
        for item in _coverage(unicode_request)
    )

    oversized_hints = [f"{index:02d}" + "😀" * 258 for index in range(8)]
    with pytest.raises(RetrievalContractError, match="closed byte limit"):
        _request(
            title_hints=oversized_hints,
            filename_hints=oversized_hints,
            entity_hints=oversized_hints,
        )


def test_entity_hints_and_lifecycle_constraints_are_closed_canonical_and_private() -> None:
    request = _request(
        entity_hints=["Zulu Entity", "Alpha Entity"],
        lifecycle_constraints=[
            {
                "corpus": "documents",
                "states": ["pending", "active"],
            }
        ],
    )

    assert request.entity_hints == ("Alpha Entity", "Zulu Entity")
    assert request.lifecycle_constraints == (
        ArchiveLifecycleConstraint.create(
            ArchiveSearchCorpus.DOCUMENTS,
            (LifecycleState.ACTIVE, LifecycleState.PENDING),
        ),
    )
    assert ArchiveSearchRequest.parse_private(request.to_private_json()) == request

    valid_hints = [f"entity-{index}-" + "x" * 251 for index in range(8)]
    assert len(_request(entity_hints=valid_hints).entity_hints) == 8
    for invalid in (valid_hints + ["ninth"], ["same", "same"], ["x" * 261]):
        with pytest.raises(RetrievalContractError):
            _request(entity_hints=invalid)

    with pytest.raises(RetrievalContractError, match="not canonical"):
        _request(lifecycle_constraints=[{"corpus": "documents", "states": ["tombstoned"]}])
    with pytest.raises(RetrievalContractError, match="was not requested"):
        ArchiveSearchRequest.parse(
            {
                "query": "needle",
                "corpora": ["documents"],
                "lifecycle_constraints": [{"corpus": "knowledge", "states": ["active"]}],
            }
        )
    with pytest.raises(RetrievalContractError, match="unique exact identities"):
        _request(
            lifecycle_constraints=[
                {"corpus": "documents", "states": ["active"]},
                {"corpus": "documents", "states": ["pending"]},
            ]
        )


def test_temporal_constraints_are_exact_role_typed_and_half_open() -> None:
    received = ArchiveTemporalConstraint(
        ArchiveSearchCorpus.DOCUMENTS,
        TemporalRole.RECEIVED_AT,
        TemporalValueKind.INSTANT,
        TemporalPrecision.INSTANT,
        "2026-08-23T08:22:00+00:00",
        "2026-08-23T09:22:00+00:00",
    )
    request = ArchiveSearchRequest.create(
        query="needle",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        temporal_constraints=(received,),
    )
    assert request.temporal_constraints[0].role is TemporalRole.RECEIVED_AT

    base = {
        "corpus": "documents",
        "role": "received_at",
        "value_kind": "instant",
        "precision": "instant",
        "start": "2026-08-01T00:00:00+00:00",
        "end": "2026-09-01T00:00:00+00:00",
    }
    for invalid in (
        {**base, "role": "uploaded"},
        {**base, "start": "2026-09-01T00:00:00+00:00"},
        {**base, "value_kind": "date_interval", "precision": "day", "start": "2026-08-01"},
        {**base, "start": "2026-08-01T03:00:00+03:00", "end": "2026-09-01T03:00:00+03:00"},
        {**base, "corpus": "messages"},
        {**base, "corpus": "obsidian"},
        {**base, "role": "conversation_time"},
        {**base, "role": "knowledge_projection_created_at"},
        {**base, "role": "legacy_unclassified_document_date"},
    ):
        with pytest.raises(RetrievalContractError):
            ArchiveSearchRequest.parse(
                {
                    "query": "needle",
                    "corpora": ["documents", "messages", "obsidian"],
                    "temporal_constraints": [invalid],
                }
            )

    with pytest.raises(RetrievalContractError, match="was not requested"):
        ArchiveSearchRequest.create(
            query="needle",
            corpora=(ArchiveSearchCorpus.KNOWLEDGE,),
            temporal_constraints=(received,),
        )


def test_message_scope_roles_and_context_are_closed_without_conversation_ids() -> None:
    request = ArchiveSearchRequest.parse(
        {
            "query": "what did I say",
            "corpora": ["messages"],
            "conversation_scope": "current",
            "roles": ["user", "assistant"],
            "context": {"before": 3, "after": 2},
        }
    )
    assert request.conversation_scope is ConversationScope.CURRENT
    assert request.roles == (MessageRole.ASSISTANT, MessageRole.USER)
    assert request.context == ArchiveContextWindow(before=3, after=2)
    assert "conversation_id" not in request.to_private_payload()

    with pytest.raises(RetrievalContractError, match="only user and assistant"):
        ArchiveSearchRequest.parse({"query": "needle", "corpora": ["messages"], "roles": ["system"]})

    with pytest.raises(RetrievalContractError, match="messages corpus"):
        ArchiveSearchRequest.parse(
            {"query": "needle", "corpora": ["documents"], "conversation_scope": "current"}
        )
    with pytest.raises(RetrievalContractError, match="range"):
        ArchiveSearchRequest.parse(
            {"query": "needle", "corpora": ["messages"], "context": {"before": 4, "after": 0}}
        )


def test_navigation_and_factual_candidates_enforce_exact_passage_identity() -> None:
    snapshot, _raw_revision, revision = _snapshot(inbox_state=LifecycleState.PENDING)
    passage = _passage(snapshot, revision)

    navigation = ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        resolved_source=snapshot,
        review_state=ArchiveReviewState.PENDING,
        evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
        lifecycle_state=LifecycleState.PENDING,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.EXACT_IDENTITY, 1),),
        filename="report.md",
    )
    assert navigation.passages == ()
    with pytest.raises(RetrievalContractError, match="zero passages"):
        ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            resolved_source=snapshot,
            review_state=ArchiveReviewState.PENDING,
            evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
            lifecycle_state=LifecycleState.PENDING,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.EXACT_IDENTITY, 1),),
            passages=(passage,),
        )
    confirmed_snapshot, _confirmed_raw, _confirmed_revision = _snapshot()
    with pytest.raises(RetrievalContractError, match="exact passages"):
        ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            resolved_source=confirmed_snapshot,
            review_state=ArchiveReviewState.CONFIRMED,
            evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
            lifecycle_state=LifecycleState.ACTIVE,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
        )

    other, _other_raw, other_revision = _snapshot(_OTHER_RAW_ID, _OTHER_KO_ID)
    with pytest.raises(RetrievalContractError, match="exact source snapshot"):
        ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            resolved_source=confirmed_snapshot,
            review_state=ArchiveReviewState.CONFIRMED,
            evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
            lifecycle_state=LifecycleState.ACTIVE,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
            passages=(_passage(other, other_revision),),
        )


def test_candidate_lifecycle_requires_real_relevant_and_current_representations() -> None:
    snapshot, _raw_revision, _knowledge_revision = _snapshot()
    with pytest.raises(RetrievalContractError, match="review and lifecycle"):
        ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            resolved_source=snapshot,
            review_state=ArchiveReviewState.PENDING,
            evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
            lifecycle_state=LifecycleState.ACTIVE,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.CATALOG, 1),),
        )
    with pytest.raises(RetrievalContractError, match="absent from its relevant"):
        ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.KNOWLEDGE,
            resolved_source=snapshot,
            review_state=ArchiveReviewState.ARCHIVED,
            evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
            lifecycle_state=LifecycleState.DEPRECATED,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.CATALOG, 1),),
        )

    deleted, _deleted_raw, deleted_knowledge = _snapshot(knowledge_state=LifecycleState.DELETED)
    with pytest.raises(RetrievalContractError, match="review and lifecycle"):
        ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.KNOWLEDGE,
            resolved_source=deleted,
            review_state=ArchiveReviewState.CONFIRMED,
            evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
            lifecycle_state=LifecycleState.DELETED,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
            passages=(_passage(deleted, deleted_knowledge),),
        )

    ignored, ignored_raw, _ignored_knowledge = _snapshot(inbox_state=LifecycleState.IGNORED)
    with pytest.raises(RetrievalContractError, match="ignored source lifecycle"):
        ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            resolved_source=ignored,
            review_state=ArchiveReviewState.CONFIRMED,
            evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
            lifecycle_state=LifecycleState.ACTIVE,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
            passages=(_passage(ignored, ignored_raw),),
        )

    pending_source, _pending_raw, _pending_knowledge = _snapshot(inbox_state=LifecycleState.PENDING)
    with pytest.raises(RetrievalContractError, match="relevant source snapshot"):
        ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            resolved_source=pending_source,
            review_state=ArchiveReviewState.PENDING,
            evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
            lifecycle_state=LifecycleState.ACTIVE,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.CATALOG, 1),),
        )

    ignored_web, ignored_web_raw, _ignored_web_knowledge = _snapshot(
        source_kind=SourceKind.WEB_CAPTURE,
        inbox_state=LifecycleState.IGNORED,
    )
    with pytest.raises(RetrievalContractError, match="not archive-discoverable"):
        ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.WEB,
            resolved_source=ignored_web,
            review_state=ArchiveReviewState.NOT_APPLICABLE,
            evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
            lifecycle_state=LifecycleState.IGNORED,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
            passages=(_passage(ignored_web, ignored_web_raw),),
        )

    reviewed_source, reviewed_raw, _reviewed_knowledge = _snapshot()
    raw_representation = reviewed_raw.representation
    raw_only = ResolvedSource.create(
        source_ref=reviewed_source.source_ref,
        representations=(raw_representation,),
        lifecycle=(LifecycleRef(raw_representation, LifecycleState.ACTIVE),),
        revisions=(reviewed_raw,),
        revalidation_targets=(RevalidationTarget(raw_representation, AuthorityScope.TENANT_PRINCIPAL),),
    )
    with pytest.raises(RetrievalContractError, match="authoritative review lifecycle"):
        ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            resolved_source=raw_only,
            review_state=ArchiveReviewState.CONFIRMED,
            evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
            lifecycle_state=LifecycleState.ACTIVE,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
            passages=(_passage(raw_only, reviewed_raw),),
        )

    pending_web, pending_web_raw, _pending_web_knowledge = _snapshot(
        source_kind=SourceKind.WEB_CAPTURE,
        inbox_state=LifecycleState.PENDING,
    )
    with pytest.raises(RetrievalContractError, match="pending source"):
        ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.WEB,
            resolved_source=pending_web,
            review_state=ArchiveReviewState.NOT_APPLICABLE,
            evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
            lifecycle_state=LifecycleState.PENDING,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
            passages=(_passage(pending_web, pending_web_raw),),
        )

    for corpus, source_kind, state in (
        (ArchiveSearchCorpus.OBSIDIAN, SourceKind.OBSIDIAN_NOTE, LifecycleState.TOMBSTONED),
        (
            ArchiveSearchCorpus.EXTERNAL,
            SourceKind.EXTERNAL_REGISTERED_SOURCE,
            LifecycleState.UNAVAILABLE,
        ),
    ):
        unavailable, revision = _single_representation_snapshot(source_kind, state)
        with pytest.raises(RetrievalContractError, match="invalid source lifecycle"):
            ArchiveSearchCandidate.create(
                corpus=corpus,
                resolved_source=unavailable,
                review_state=ArchiveReviewState.NOT_APPLICABLE,
                evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
                lifecycle_state=state,
                matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
                passages=(_passage(unavailable, revision),),
            )


def test_candidate_private_projector_round_trips_exact_authority_and_rejects_open_shape() -> None:
    candidate = _factual_candidate()
    private_json = candidate.to_private_json()
    restored = ArchiveSearchCandidate.parse_private(private_json)

    assert restored == candidate
    assert restored.review_state is ArchiveReviewState.CONFIRMED
    assert restored.evidence_authority is ArchiveEvidenceAuthority.CANONICAL
    assert restored.matches == (ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),)
    assert restored.passages[0].excerpt == "Exact factual excerpt"
    opened = candidate.to_private_payload()
    opened["raw_object_id"] = _RAW_ID
    with pytest.raises(RetrievalContractError, match="keys"):
        ArchiveSearchCandidate.from_private_payload(opened)
    with pytest.raises(RetrievalContractError, match="unique"):
        ArchiveSearchCandidate.create(
            corpus=candidate.corpus,
            resolved_source=candidate.resolved_source,
            review_state=candidate.review_state,
            evidence_authority=candidate.evidence_authority,
            lifecycle_state=candidate.lifecycle_state,
            matches=(
                ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),
                ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 2),
            ),
            passages=candidate.passages,
        )
    with pytest.raises(RetrievalContractError, match="canonical"):
        ArchiveSearchCandidate.parse_private(json.dumps(json.loads(private_json)))


def test_knowledge_candidates_accept_every_closed_promoted_source_kind() -> None:
    for source_kind, raw_id, ko_id in (
        (SourceKind.WEB_CAPTURE, _RAW_ID, _KO_ID),
        (SourceKind.GENERATED_ARTIFACT, _OTHER_RAW_ID, _OTHER_KO_ID),
    ):
        snapshot, _raw_revision, revision = _snapshot(raw_id, ko_id, source_kind)
        candidate = ArchiveSearchCandidate.create(
            corpus=ArchiveSearchCorpus.KNOWLEDGE,
            resolved_source=snapshot,
            review_state=ArchiveReviewState.CONFIRMED,
            evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
            lifecycle_state=LifecycleState.ACTIVE,
            matches=(ArchiveMatchRank(ArchiveMatchChannel.DENSE, 2),),
            passages=(_passage(snapshot, revision),),
        )
        assert candidate.resolved_source.source_ref.source_kind is source_kind


def test_page_requires_live_exact_private_request_attestation() -> None:
    original = _request()
    coverage = _coverage(original)
    candidate = _factual_candidate()

    different_query = _request(query="different private query")
    with pytest.raises(RetrievalContractError, match="does not attest"):
        ArchiveSearchPage.create(
            request=different_query,
            candidates=(candidate,),
            coverage=coverage,
        )

    restored_coverage = tuple(SearchCoverage.parse(item.to_json()) for item in coverage)
    assert restored_coverage == coverage
    with pytest.raises(RetrievalContractError, match="does not attest"):
        ArchiveSearchPage.create(
            request=original,
            candidates=(candidate,),
            coverage=restored_coverage,
        )
    with pytest.raises(RetrievalContractError, match="does not attest"):
        ArchiveSearchPage.create(
            request=original,
            candidates=(candidate,),
            coverage=(coverage[0], restored_coverage[1]),
        )

    resumed = _request(continuation="inbound_page_two")
    assert resumed.to_identity_json() == original.to_identity_json()
    assert (
        ArchiveSearchPage.create(
            request=resumed,
            candidates=(candidate,),
            coverage=coverage,
        ).request
        is resumed
    )


def test_page_enforces_requested_lifecycle_and_attested_unique_lane_ranks() -> None:
    lifecycle_request = _request(lifecycle_constraints=[{"corpus": "documents", "states": ["active"]}])
    snapshot, _raw_revision, _knowledge_revision = _snapshot(inbox_state=LifecycleState.PENDING)
    pending = ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        resolved_source=snapshot,
        review_state=ArchiveReviewState.PENDING,
        evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
        lifecycle_state=LifecycleState.PENDING,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
    )
    with pytest.raises(RetrievalContractError, match="outside the requested constraint"):
        ArchiveSearchPage.create(
            request=lifecycle_request,
            candidates=(pending,),
            coverage=_coverage(lifecycle_request),
        )

    request = _request()
    first = _factual_candidate()
    first_source = first.resolved_source
    over_ranked = ArchiveSearchCandidate.create(
        corpus=first.corpus,
        resolved_source=first_source,
        review_state=first.review_state,
        evidence_authority=first.evidence_authority,
        lifecycle_state=first.lifecycle_state,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 2),),
        passages=first.passages,
    )
    with pytest.raises(RetrievalContractError, match="rank is not attested"):
        ArchiveSearchPage.create(
            request=request,
            candidates=(over_ranked,),
            coverage=_coverage(request),
        )

    wrong_lane = ArchiveSearchCandidate.create(
        corpus=first.corpus,
        resolved_source=first_source,
        review_state=first.review_state,
        evidence_authority=first.evidence_authority,
        lifecycle_state=first.lifecycle_state,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.DENSE, 1),),
        passages=first.passages,
    )
    with pytest.raises(RetrievalContractError, match="rank is not attested"):
        ArchiveSearchPage.create(
            request=request,
            candidates=(wrong_lane,),
            coverage=_coverage(request),
        )

    second_source, _second_raw, second_revision = _snapshot(_OTHER_RAW_ID, _OTHER_KO_ID)
    second = ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        resolved_source=second_source,
        review_state=ArchiveReviewState.CONFIRMED,
        evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
        lifecycle_state=LifecycleState.ACTIVE,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
        passages=(_passage(second_source, second_revision),),
    )
    with pytest.raises(RetrievalContractError, match="rank is not attested"):
        ArchiveSearchPage.create(
            request=request,
            candidates=(first, second),
            coverage=_coverage(request, matched_at_least=2, returned=2),
        )


def test_confirmed_only_page_rejects_pending_or_noncanonical_evidence() -> None:
    request = _request(review_scope="confirmed_only")
    snapshot, raw_revision, _revision = _snapshot(inbox_state=LifecycleState.PENDING)
    pending = ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        resolved_source=snapshot,
        review_state=ArchiveReviewState.PENDING,
        evidence_authority=ArchiveEvidenceAuthority.NONCANONICAL,
        lifecycle_state=LifecycleState.PENDING,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
        passages=(_passage(snapshot, raw_revision),),
    )
    with pytest.raises(RetrievalContractError, match="confirmed-only"):
        ArchiveSearchPage.create(
            request=request,
            candidates=(pending,),
            coverage=_coverage(request),
        )

    archived_source, _raw, archived_revision = _snapshot(knowledge_state=LifecycleState.ARCHIVED)
    archived = ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        resolved_source=archived_source,
        review_state=ArchiveReviewState.ARCHIVED,
        evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
        lifecycle_state=LifecycleState.ARCHIVED,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
        passages=(_passage(archived_source, archived_revision),),
    )
    with pytest.raises(RetrievalContractError, match="confirmed-only"):
        ArchiveSearchPage.create(
            request=request,
            candidates=(archived,),
            coverage=_coverage(request),
        )


def test_public_projection_has_only_opaque_handles_and_safe_bounded_evidence() -> None:
    request = _request(continuation="private_inbound_cursor")
    candidate = _factual_candidate()
    page = ArchiveSearchPage.create(
        request=request,
        candidates=(candidate,),
        coverage=_coverage(request),
    )
    payload = page.to_public_payload(_KEY)
    serialized = page.to_public_json(_KEY)

    assert payload["absence"] == "evidence_found"
    assert payload["exhaustive"] is True
    assert payload["continuation"] is None
    assert payload["candidates"][0]["label"] == "A1"  # type: ignore[index]
    assert payload["candidates"][0]["corpus"] == "knowledge"  # type: ignore[index]
    assert payload["candidates"][0]["source_kind"] == "document"  # type: ignore[index]
    assert payload["candidates"][0]["review_state"] == "confirmed"  # type: ignore[index]
    assert payload["candidates"][0]["evidence_authority"] == "canonical"  # type: ignore[index]
    assert payload["candidates"][0]["lifecycle_state"] == "active"  # type: ignore[index]
    assert payload["candidates"][0]["match_channels"] == ["lexical"]  # type: ignore[index]
    assert payload["candidates"][0]["matches"] == [  # type: ignore[index]
        {"channel": "lexical", "rank": 1}
    ]
    assert payload["candidates"][0]["passages"][0]["label"] == "A1.1"  # type: ignore[index]
    assert payload["candidates"][0]["temporal_facts"] == [  # type: ignore[index]
        {
            "end": None,
            "origin": "storage_column",
            "precision": "instant",
            "role": "received_at",
            "start": "2026-08-23T08:22:00+00:00",
            "value_kind": "instant",
        }
    ]
    for private in (
        request.query,
        "private_inbound_cursor",
        _RAW_ID,
        _KO_ID,
        "tenant-private",
        "owner-private",
        "snapshot-private",
        "run-private",
        "d" * 64,
    ):
        assert private not in serialized
    assert "source_handle" in serialized and "passage_handle" in serialized
    assert page.to_public_json(_OTHER_KEY) != serialized
    assert _RAW_ID not in repr(candidate) and "owner-private" not in repr(candidate)
    assert "execution_binding" in payload
    projected_coverage = payload["coverage"]
    assert isinstance(projected_coverage, list)
    assert all(isinstance(item, dict) and "execution_binding" not in item for item in projected_coverage)

    default_encoded = json.dumps(payload, ensure_ascii=False)
    pretty_encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    assert len(default_encoded) <= 7_900
    assert len(pretty_encoded) <= 11_900
    tool_result = ToolResult("archive_search", True, data=payload)
    assert json.loads(tool_result.to_dict()["result"]) == payload
    assert tool_result.truncated is False
    assert pretty_encoded in tool_result.to_llm_message()
    assert tool_result.truncated is False


def test_public_projection_boundary_rejects_subclass_method_injection() -> None:
    candidate = _factual_candidate()
    source = candidate.resolved_source

    class ForgedResolvedSource(ResolvedSource):
        def logical_digest(self, privacy_key: bytes) -> str:
            return "private-source-injection"

    forged_source = ForgedResolvedSource(
        source.source_ref,
        source.representations,
        source.lifecycle,
        source.revisions,
        source.revalidation_targets,
    )
    with pytest.raises(RetrievalContractError, match="typed corpus and source"):
        ArchiveSearchCandidate.create(
            corpus=candidate.corpus,
            resolved_source=forged_source,
            review_state=candidate.review_state,
            evidence_authority=candidate.evidence_authority,
            lifecycle_state=candidate.lifecycle_state,
            matches=candidate.matches,
            temporal_facts=candidate.temporal_facts,
            passages=candidate.passages,
        )

    passage = candidate.passages[0]
    passage_ref = passage.passage_ref

    class ForgedPassageRef(PassageRef):
        def passage_digest(self, privacy_key: bytes) -> str:
            return "private-passage-injection"

    forged_passage_ref = ForgedPassageRef(
        passage_ref.source_ref,
        passage_ref.source_revision,
        passage_ref.locator,
        passage_ref.passage_index_version,
        passage_ref.embedding,
    )
    with pytest.raises(RetrievalContractError, match="exact PassageRef"):
        ArchiveSearchPassage(forged_passage_ref, passage.excerpt)

    class ForgedRequest(ArchiveSearchRequest):
        pass

    request = _request()
    forged_request = object.__new__(ForgedRequest)
    with pytest.raises(RetrievalContractError, match="canonical private request"):
        ArchiveSearchPage.create(
            request=forged_request,
            candidates=(candidate,),
            coverage=_coverage(request),
        )

    class StatefulTuple(tuple[object, ...]):
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield from super().__iter__()
            yield {"private_query": "must-not-reach-public-projection"}

    with pytest.raises(RetrievalContractError, match="evidence must use exact tuples"):
        ArchiveSearchCandidate(
            corpus=candidate.corpus,
            resolved_source=candidate.resolved_source,
            title=candidate.title,
            filename=candidate.filename,
            review_state=candidate.review_state,
            evidence_authority=candidate.evidence_authority,
            lifecycle_state=candidate.lifecycle_state,
            matches=StatefulTuple(candidate.matches),  # type: ignore[arg-type]
            temporal_facts=candidate.temporal_facts,
            passages=candidate.passages,
        )

    coverage = _coverage(request)
    with pytest.raises(RetrievalContractError, match="page collections must use exact tuples"):
        ArchiveSearchPage(
            request=request,
            results=(),
            coverage=StatefulTuple(coverage),  # type: ignore[arg-type]
            warnings=(),
            continuation=None,
        )


def test_public_projection_fails_closed_before_tool_result_json_can_be_truncated() -> None:
    request = _request(limit=1)
    snapshot, _raw_revision, revision = _snapshot()
    passages = []
    for index in range(8):
        passage_ref = PassageRef.from_resolved_source(
            snapshot,
            source_revision=revision,
            locator=TextSpanLocator(index, index * 100, index * 100 + 90),
            passage_index_version="archive-char-v1",
            embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
        )
        passages.append(ArchiveSearchPassage(passage_ref, f"{index}-" + "x" * 1_998))
    candidate = ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.KNOWLEDGE,
        resolved_source=snapshot,
        review_state=ArchiveReviewState.CONFIRMED,
        evidence_authority=ArchiveEvidenceAuthority.CANONICAL,
        lifecycle_state=LifecycleState.ACTIVE,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.LEXICAL, 1),),
        passages=passages,
    )
    page = ArchiveSearchPage.create(
        request=request,
        candidates=(candidate,),
        coverage=_coverage(request),
    )

    with pytest.raises(RetrievalContractError, match="real ToolResult envelope"):
        page.to_public_payload(_KEY)


def test_public_projection_rejects_mixed_coverage_and_exposes_honest_continuation() -> None:
    request = _request(limit=1)
    candidate = _factual_candidate()
    coverage = _coverage(
        request,
        states=(CoverageState.PARTIAL, CoverageState.CAPPED),
        cursor=True,
    )
    page = ArchiveSearchPage.create(
        request=request,
        candidates=(candidate,),
        coverage=coverage,
        warnings=(ArchiveSearchWarning.LANE_CAPPED,),
        continuation="next_page_token",
    )
    public = page.to_public_payload(_KEY)

    assert public["absence"] == "evidence_found"
    assert public["exhaustive"] is False
    assert public["continuation"] == "next_page_token"
    assert public["warnings"] == ["lane_capped"]

    with pytest.raises(RetrievalContractError, match="continuation"):
        ArchiveSearchPage.create(
            request=request,
            candidates=(candidate,),
            coverage=coverage,
            continuation=None,
        )
    with pytest.raises(RetrievalContractError, match="every bound target"):
        ArchiveSearchPage.create(
            request=request,
            candidates=(candidate,),
            coverage=coverage[:1],
            continuation="next_page_token",
        )

    resumed_request = _request(limit=1, continuation="same_page_token")
    resumed_coverage = _coverage(
        resumed_request,
        states=(CoverageState.PARTIAL, CoverageState.CAPPED),
        cursor=True,
    )
    with pytest.raises(RetrievalContractError, match="must differ"):
        ArchiveSearchPage.create(
            request=resumed_request,
            candidates=(candidate,),
            coverage=resumed_coverage,
            continuation="same_page_token",
        )
