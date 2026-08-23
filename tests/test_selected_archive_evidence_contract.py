from __future__ import annotations

import json
from dataclasses import replace

import pytest

from friday.interaction_control_plane.selected_archive_evidence import (
    SelectedArchiveCorpus,
    SelectedArchiveCoverageGrade,
    SelectedArchiveEvidence,
    SelectedArchiveEvidenceError,
    canonical_passage_refs_json,
    parse_canonical_passage_refs,
)
from friday.retrieval.archive_search_message_adapter import MESSAGE_PASSAGE_INDEX_VERSION
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    MessageWindowLocator,
    PassageRef,
    RepresentationKind,
    RevisionKind,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TextSpanLocator,
)
from friday.storage._archive_search_documents import PASSAGE_INDEX_VERSION


def _document_evidence(*, corpus: SelectedArchiveCorpus) -> SelectedArchiveEvidence:
    raw_id = "raw_0123456789abcdef"
    source = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        "tenant-main",
        "person-main",
        CanonicalObjectKind.RAW_OBJECT,
        raw_id,
    )
    if corpus is SelectedArchiveCorpus.DOCUMENTS:
        representation = SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id)
        revision = SourceRevision(representation, RevisionKind.RAW_CONTENT_SHA256, "a" * 64)
        embedding = EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE)
        index_version = PASSAGE_INDEX_VERSION
    else:
        representation = SourceRepresentation(
            RepresentationKind.KNOWLEDGE_OBJECT,
            "ko_0123456789abcdef",
        )
        revision = SourceRevision(representation, RevisionKind.KNOWLEDGE_VERSION, "3")
        embedding = EmbeddingIdentity.indexed(
            EmbeddingCompatibility.CURRENT,
            model_id="model-v1",
            dimensions=8,
            source_version=3,
            chunk_scheme=PASSAGE_INDEX_VERSION,
            chunk_content_sha256="b" * 64,
        )
        index_version = PASSAGE_INDEX_VERSION
    passage = PassageRef(
        source,
        revision,
        TextSpanLocator(chunk_index=0, start_char=4, end_char=28),
        index_version,
        embedding,
    )
    return SelectedArchiveEvidence(
        work_item_id="work_0123456789abcdef",
        corpus=corpus,
        source_ref=source,
        passage_refs=(passage,),
        source_snapshot_sha256="c" * 64,
        coverage_sha256="d" * 64,
        coverage_grade=SelectedArchiveCoverageGrade.COMPLETE,
        origin_boundary_user_message_id="msg_0123456789abcdef",
    )


def _message_evidence() -> SelectedArchiveEvidence:
    conversation_id = "conv_0123456789abcdef"
    source = SourceRef(
        SourceKind.CONVERSATION,
        AuthorityScope.PRINCIPAL,
        None,
        "person-main",
        CanonicalObjectKind.CONVERSATION,
        conversation_id,
    )
    representation = SourceRepresentation(RepresentationKind.CONVERSATION, conversation_id)
    revision = SourceRevision(representation, RevisionKind.MESSAGE_LEDGER_SHA256, "e" * 64)
    passage = PassageRef(
        source,
        revision,
        MessageWindowLocator(
            first_message_id="msg_1111111111111111",
            last_message_id="msg_2222222222222222",
            start_at="2026-08-20T08:00:00+00:00",
            end_at="2026-08-20T09:00:00+00:00",
            context_before=1,
            context_after=1,
        ),
        MESSAGE_PASSAGE_INDEX_VERSION,
        EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
    )
    return SelectedArchiveEvidence(
        work_item_id="work_fedcba9876543210",
        corpus=SelectedArchiveCorpus.MESSAGES,
        source_ref=source,
        passage_refs=(passage,),
        source_snapshot_sha256="f" * 64,
        coverage_sha256="0" * 64,
        coverage_grade=SelectedArchiveCoverageGrade.PARTIAL,
        origin_boundary_user_message_id="msg_fedcba9876543210",
    )


@pytest.mark.parametrize(
    "evidence",
    [
        _document_evidence(corpus=SelectedArchiveCorpus.DOCUMENTS),
        _document_evidence(corpus=SelectedArchiveCorpus.KNOWLEDGE),
        _message_evidence(),
    ],
)
def test_selected_evidence_storage_round_trip_is_canonical_and_body_free(
    evidence: SelectedArchiveEvidence,
) -> None:
    stored = evidence.to_storage_payload()

    assert SelectedArchiveEvidence.from_storage_row(stored) == evidence
    assert parse_canonical_passage_refs(stored["passage_refs_json"]) == evidence.passage_refs
    assert stored["passage_refs_json"] == canonical_passage_refs_json(evidence.passage_refs)
    encoded = json.dumps(evidence.to_payload(), sort_keys=True)
    assert all(word not in encoded for word in ("excerpt", "filename", "query", "body", "model_prose"))


def test_selected_evidence_rejects_corpus_source_and_revision_mismatch() -> None:
    document = _document_evidence(corpus=SelectedArchiveCorpus.DOCUMENTS)
    message = _message_evidence()

    with pytest.raises(SelectedArchiveEvidenceError, match="corpus"):
        replace(document, corpus=SelectedArchiveCorpus.MESSAGES)
    with pytest.raises(SelectedArchiveEvidenceError, match="corpus"):
        replace(document, corpus=SelectedArchiveCorpus.KNOWLEDGE)
    with pytest.raises(SelectedArchiveEvidenceError, match="selected source"):
        replace(document, passage_refs=message.passage_refs)


def test_knowledge_evidence_accepts_each_promoted_document_source_kind() -> None:
    knowledge = _document_evidence(corpus=SelectedArchiveCorpus.KNOWLEDGE)

    for source_kind in (SourceKind.WEB_CAPTURE, SourceKind.GENERATED_ARTIFACT):
        source = replace(knowledge.source_ref, source_kind=source_kind)
        passage = replace(knowledge.passage_refs[0], source_ref=source)
        selected = replace(knowledge, source_ref=source, passage_refs=(passage,))

        assert selected.source_ref.source_kind is source_kind
        assert SelectedArchiveEvidence.from_storage_row(selected.to_storage_payload()) == selected


def test_selected_evidence_rejects_empty_duplicate_unsorted_and_oversized_passages() -> None:
    evidence = _document_evidence(corpus=SelectedArchiveCorpus.DOCUMENTS)
    passage = evidence.passage_refs[0]
    later = replace(
        passage,
        locator=TextSpanLocator(chunk_index=1, start_char=30, end_char=50),
    )
    ordered = tuple(sorted((passage, later), key=lambda item: item.to_private_json()))

    with pytest.raises(SelectedArchiveEvidenceError, match="one to eight"):
        replace(evidence, passage_refs=())
    with pytest.raises(SelectedArchiveEvidenceError, match="unique"):
        replace(evidence, passage_refs=(passage, passage))
    with pytest.raises(SelectedArchiveEvidenceError, match="ordered"):
        replace(evidence, passage_refs=tuple(reversed(ordered)))
    with pytest.raises(SelectedArchiveEvidenceError, match="one to eight"):
        replace(evidence, passage_refs=tuple(passage for _item in range(9)))


def test_selected_evidence_rejects_unreplayable_authority_and_index_version() -> None:
    evidence = _document_evidence(corpus=SelectedArchiveCorpus.DOCUMENTS)
    tenant_source = replace(
        evidence.source_ref,
        authority_scope=AuthorityScope.TENANT,
        principal_id=None,
    )
    tenant_passage = replace(evidence.passage_refs[0], source_ref=tenant_source)

    with pytest.raises(SelectedArchiveEvidenceError, match="replay matrix"):
        replace(
            evidence,
            source_ref=tenant_source,
            passage_refs=(tenant_passage,),
        )
    with pytest.raises(SelectedArchiveEvidenceError, match="replay matrix"):
        replace(
            evidence,
            passage_refs=(replace(evidence.passage_refs[0], passage_index_version="raw-char-v1"),),
        )


def test_selected_evidence_requires_one_revision_and_unique_locators() -> None:
    evidence = _document_evidence(corpus=SelectedArchiveCorpus.DOCUMENTS)
    passage = evidence.passage_refs[0]
    second_revision = SourceRevision(
        passage.source_revision.representation,
        RevisionKind.RAW_CONTENT_SHA256,
        "e" * 64,
    )
    later_revision = replace(
        passage,
        source_revision=second_revision,
        locator=TextSpanLocator(chunk_index=1, start_char=30, end_char=50),
    )
    mixed = tuple(sorted((passage, later_revision), key=lambda item: item.to_private_json()))

    with pytest.raises(SelectedArchiveEvidenceError, match="one exact source revision"):
        replace(evidence, passage_refs=mixed)

    same_locator = replace(
        passage,
        embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.MISSING),
    )
    duplicate_locators = tuple(sorted((passage, same_locator), key=lambda item: item.to_private_json()))
    with pytest.raises(SelectedArchiveEvidenceError, match="locators must be unique"):
        replace(evidence, passage_refs=duplicate_locators)


def test_passage_array_parser_rejects_noncanonical_or_extra_identity_data() -> None:
    evidence = _message_evidence()
    canonical = canonical_passage_refs_json(evidence.passage_refs)
    decoded = json.loads(canonical)
    decoded[0]["excerpt"] = "copied body"

    with pytest.raises(SelectedArchiveEvidenceError, match="canonical"):
        parse_canonical_passage_refs(" " + canonical)
    with pytest.raises(SelectedArchiveEvidenceError, match="typed identity"):
        parse_canonical_passage_refs(json.dumps(decoded, sort_keys=True, separators=(",", ":")))
    with pytest.raises(SelectedArchiveEvidenceError, match="semantically canonical"):
        parse_canonical_passage_refs(json.dumps(json.loads(canonical), indent=2))


def test_storage_row_requires_exact_keys_and_lowercase_digests() -> None:
    evidence = _message_evidence()
    stored: dict[str, object] = evidence.to_storage_payload()
    stored["body"] = "must not persist"
    with pytest.raises(SelectedArchiveEvidenceError, match="keys"):
        SelectedArchiveEvidence.from_storage_row(stored)

    stored = evidence.to_storage_payload()
    stored["coverage_sha256"] = "A" * 64
    with pytest.raises(SelectedArchiveEvidenceError, match="lowercase"):
        SelectedArchiveEvidence.from_storage_row(stored)
