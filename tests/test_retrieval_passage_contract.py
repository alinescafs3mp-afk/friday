from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    LifecycleRef,
    LifecycleState,
    MessageRole,
    MessageWindowLocator,
    PassageRef,
    RepresentationKind,
    ResolvedSource,
    RetrievalContractError,
    RevalidationTarget,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TextSpanLocator,
)

_KEY = b"p" * 32
_RAW_ID = "raw_0123456789abcdef"
_INBOX_ID = "inbox_0123456789abcdef"
_KO_ID = "ko_0123456789abcdef"
_CONVERSATION_ID = "conv_0123456789abcdef"


def _document_snapshot(version: str = "3") -> tuple[ResolvedSource, SourceRevision]:
    source_ref = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        "tenant-main",
        "person-42",
        CanonicalObjectKind.RAW_OBJECT,
        _RAW_ID,
    )
    raw = SourceRepresentation(RepresentationKind.RAW_OBJECT, _RAW_ID)
    inbox = SourceRepresentation(RepresentationKind.INBOX_ITEM, _INBOX_ID)
    knowledge = SourceRepresentation(RepresentationKind.KNOWLEDGE_OBJECT, _KO_ID)
    knowledge_revision = SourceRevision(knowledge, RevisionKind.KNOWLEDGE_VERSION, version)
    return (
        ResolvedSource.create(
            source_ref=source_ref,
            representations=[raw, inbox, knowledge],
            lifecycle=[
                LifecycleRef(raw, LifecycleState.ACTIVE),
                LifecycleRef(inbox, LifecycleState.CLASSIFIED),
                LifecycleRef(knowledge, LifecycleState.ACTIVE),
            ],
            revisions=[
                SourceRevision(raw, RevisionKind.RAW_CONTENT_SHA256, "a" * 64),
                knowledge_revision,
            ],
            revalidation_targets=[
                RevalidationTarget(raw, AuthorityScope.TENANT_PRINCIPAL),
                RevalidationTarget(inbox, AuthorityScope.TENANT_PRINCIPAL),
                RevalidationTarget(knowledge, AuthorityScope.TENANT_PRINCIPAL),
            ],
        ),
        knowledge_revision,
    )


def _embedding(
    compatibility: EmbeddingCompatibility = EmbeddingCompatibility.CURRENT,
    *,
    source_version: int = 3,
    content_hash: str = "c" * 64,
) -> EmbeddingIdentity:
    return EmbeddingIdentity.indexed(
        compatibility,
        model_id="multilingual-e5-large",
        dimensions=1_024,
        source_version=source_version,
        chunk_scheme="ko-char-v1",
        chunk_content_sha256=content_hash,
    )


def _passage(
    compatibility: EmbeddingCompatibility = EmbeddingCompatibility.CURRENT,
    *,
    content_hash: str = "c" * 64,
) -> PassageRef:
    snapshot, revision = _document_snapshot()
    return PassageRef.from_resolved_source(
        snapshot,
        source_revision=revision,
        locator=TextSpanLocator(chunk_index=2, start_char=40, end_char=92),
        passage_index_version="ko-char-v1",
        embedding=_embedding(compatibility, content_hash=content_hash),
    )


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value))
    return set()


def test_knowledge_passage_round_trip_matches_schema38_chunk_coordinates() -> None:
    passage = _passage()

    assert PassageRef.parse_private(passage.to_private_json()) == passage
    assert passage.locator == TextSpanLocator(chunk_index=2, start_char=40, end_char=92)
    assert passage.source_revision.value == "3"
    assert passage.embedding.source_version == 3
    assert passage.embedding.chunk_scheme == passage.passage_index_version
    assert passage.embedding.compatibility is EmbeddingCompatibility.CURRENT
    assert not hasattr(passage, "dense_vector_citable")
    assert not (_all_keys(passage.to_private_payload()) & {"body", "excerpt", "filename", "path"})


def test_passage_identity_excludes_chunk_attestation_and_embedding_state() -> None:
    current = _passage(EmbeddingCompatibility.CURRENT, content_hash="c" * 64)
    stale = _passage(EmbeddingCompatibility.STALE, content_hash="d" * 64)

    assert current.passage_digest(_KEY) == stale.passage_digest(_KEY)
    assert current.embedding.compatibility is EmbeddingCompatibility.CURRENT
    assert stale.embedding.compatibility is EmbeddingCompatibility.STALE
    assert not hasattr(current, "dense_vector_citable")
    assert "c" * 64 not in current.passage_digest(_KEY)


def test_indexed_passage_requires_exact_ko_version_and_chunk_scheme() -> None:
    snapshot, revision = _document_snapshot()
    locator = TextSpanLocator(0, 0, 12)

    with pytest.raises(RetrievalContractError, match="source_version"):
        PassageRef.from_resolved_source(
            snapshot,
            source_revision=revision,
            locator=locator,
            passage_index_version="ko-char-v1",
            embedding=_embedding(source_version=2),
        )
    wrong_scheme = EmbeddingIdentity.indexed(
        EmbeddingCompatibility.CURRENT,
        model_id="model",
        dimensions=3,
        source_version=3,
        chunk_scheme="other-v1",
        chunk_content_sha256="e" * 64,
    )
    with pytest.raises(RetrievalContractError, match="chunk_scheme"):
        PassageRef.from_resolved_source(
            snapshot,
            source_revision=revision,
            locator=locator,
            passage_index_version="ko-char-v1",
            embedding=wrong_scheme,
        )


def test_revision_membership_is_not_misnamed_as_authority() -> None:
    passage = _passage()
    current, _revision = _document_snapshot("3")
    changed, _revision = _document_snapshot("4")

    assert passage.revision_matches(current)
    assert not passage.revision_matches(changed)
    assert not hasattr(passage, "authorize")
    assert not hasattr(passage, "revalidates_against")


def test_message_window_is_a_passage_anchored_to_conversation() -> None:
    source_ref = SourceRef(
        SourceKind.CONVERSATION,
        AuthorityScope.PRINCIPAL,
        None,
        "person-42",
        CanonicalObjectKind.CONVERSATION,
        _CONVERSATION_ID,
    )
    conversation = SourceRepresentation(RepresentationKind.CONVERSATION, _CONVERSATION_ID)
    revision = SourceRevision(conversation, RevisionKind.MESSAGE_LEDGER_SHA256, "f" * 64)
    snapshot = ResolvedSource.create(
        source_ref=source_ref,
        representations=[conversation],
        lifecycle=[LifecycleRef(conversation, LifecycleState.ACTIVE)],
        revisions=[revision],
        revalidation_targets=[RevalidationTarget(conversation, AuthorityScope.PRINCIPAL)],
    )
    locator = MessageWindowLocator.create(
        first_message_id="msg_0123456789abcdef",
        last_message_id="msg_fedcba9876543210",
        start_at=datetime(2026, 8, 23, 9, 0, tzinfo=timezone(timedelta(hours=3))),
        end_at=datetime(2026, 8, 23, 9, 5, tzinfo=timezone(timedelta(hours=3))),
        context_before=2,
        context_after=3,
        matched_role=MessageRole.USER,
    )
    passage = PassageRef.from_resolved_source(
        snapshot,
        source_revision=revision,
        locator=locator,
        passage_index_version="message-window-v1",
        embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )

    assert locator.start_at == datetime(2026, 8, 23, 6, 0, tzinfo=UTC).isoformat()
    assert PassageRef.parse_private(passage.to_private_json()) == passage
    assert passage.source_ref.source_kind is SourceKind.CONVERSATION


def test_message_window_rejects_naive_time_and_wrong_source() -> None:
    with pytest.raises(RetrievalContractError, match="offset"):
        MessageWindowLocator.create(
            first_message_id="msg_0123456789abcdef",
            last_message_id="msg_fedcba9876543210",
            start_at=datetime(2026, 8, 23, 9, 0),
            end_at=datetime(2026, 8, 23, 9, 5),
        )

    snapshot, revision = _document_snapshot()
    window = MessageWindowLocator.create(
        first_message_id="msg_0123456789abcdef",
        last_message_id="msg_fedcba9876543210",
        start_at=datetime(2026, 8, 23, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 8, 23, 9, 5, tzinfo=UTC),
    )
    with pytest.raises(RetrievalContractError, match="conversation revision"):
        PassageRef(
            snapshot.source_ref,
            revision,
            window,
            "message-window-v1",
            EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
        )


def test_passage_json_is_closed() -> None:
    passage = _passage()
    payload = passage.to_private_payload()
    payload["excerpt"] = "secret"
    with pytest.raises(RetrievalContractError, match="closed contract"):
        PassageRef.parse_private(json.dumps(payload, sort_keys=True, separators=(",", ":")))
