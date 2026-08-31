from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pytest

import friday.retrieval.archive_search_service as service_module
import friday.storage._archive_search_messages as message_storage_module
from friday.organs.obsidian import OBSIDIAN_READ
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval.archive_search_authority import (
    ArchiveSearchPublicationDenialReason,
    ArchiveSearchPublicationDenied,
    attest_archive_search_before_publication,
    canonical_archive_search_targets,
    create_archive_model_batch_ledger,
)
from friday.retrieval.archive_search_contract import (
    ArchiveContextWindow,
    ArchiveEvidenceAuthority,
    ArchiveMatchChannel,
    ArchiveMatchRank,
    ArchiveReviewState,
    ArchiveSearchCandidate,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
)
from friday.retrieval.archive_search_federation import federate_archive_search
from friday.retrieval.archive_search_service import (
    ArchiveSearchServiceError,
    prepare_archive_search_in_transaction,
    reauthorize_archive_search_candidate,
    reauthorize_archive_search_coverage,
    refresh_archive_search_reauthorization_in_transaction,
)
from friday.retrieval.catalog_contract import (
    CatalogIndexLane,
    CatalogIndexState,
    CatalogIndexStatus,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    CoverageState,
    LifecycleRef,
    LifecycleState,
    RepresentationKind,
    ResolvedSource,
    RevalidationTarget,
    RevisionKind,
    SearchCorpus,
    SearchCoverage,
    SearchLane,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
)
from friday.storage.models import InboxItem, InboxStatus, RawObject

TENANT = "archive-service-tenant"
PRINCIPAL = "archive-service-principal"
SNAPSHOT = "archive-service-snapshot"
_TURNS = itertools.count(1)


class _AlienReviewState(StrEnum):
    CONFIRMED = "confirmed"


@dataclass(frozen=True)
class _AlienActorContext(ActorContext):
    pass


def _actor(*, principal: str = PRINCIPAL) -> ActorContext:
    return ActorContext(
        user_id=TENANT,
        preset_key="user",
        source="archive-service-test",
        shared_tenant=True,
        person_id=principal,
    )


def _authorization(storage: Any) -> AuthorizationService:
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    authorization.register_capability(OBSIDIAN_READ)
    return authorization


def _seed_authority(storage: Any, *, principal: str = PRINCIPAL) -> AuthorizationService:
    storage.ensure_user(TENANT)
    storage.ensure_user(principal)
    return _authorization(storage)


def _ledger(*, principal: str = PRINCIPAL):
    return create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=principal,
        turn_discriminator=f"archive-service-turn-{next(_TURNS)}",
    )


def _message_boundary(storage: Any) -> tuple[str, str]:
    conversation = storage.create_conversation(PRINCIPAL, "Archive request boundary")
    message = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "current archive request",
    )
    return conversation["id"], message["id"]


def _converge_conversation_passages(storage: Any, principal: str) -> None:
    cursor: str | None = None
    for _attempt in range(32):
        report = storage.backfill_conversation_passages(
            principal,
            resume_at_conversation_id=cursor,
            limit=256,
        )
        if report["has_more"] is False:
            return
        cursor = report["next_resume_conversation_id"]
        assert isinstance(cursor, str)
    raise AssertionError("conversation-passage setup did not converge")


def _payload(prepared: Any) -> dict[str, Any]:
    return json.loads(prepared.authorized_batch.model_visible_canonical_bytes)


def _coverage(payload: dict[str, Any], lane: SearchLane) -> dict[str, Any]:
    return next(item for item in payload["coverage"] if item["lane"] == lane.value)


def test_facade_requires_one_caller_owned_transaction_and_closes_unsupported_plan(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    actor = _actor()
    conn = storage.conn
    request = ArchiveSearchRequest.create(
        query="private artifact",
        corpora=(ArchiveSearchCorpus.GENERATED,),
        limit=2,
    )
    ledger = _ledger()

    with pytest.raises(ArchiveSearchServiceError):
        prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=actor,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="outside-transaction",
            turn_ledger=ledger,
        )

    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=actor,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="inside-transaction",
            turn_ledger=ledger,
        )
        assert conn.in_transaction
    payload = _payload(prepared)
    assert payload["candidates"] == []
    assert payload["absence"] == "not_established"
    assert len(payload["coverage"]) == 5
    assert all(
        item["states"] == [CoverageState.UNAVAILABLE.value]
        and item["authority_rechecked"] is True
        and item["snapshot_current"] is True
        for item in payload["coverage"]
    )


def test_document_storage_failure_is_not_false_absence(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    monkeypatch.setattr(
        service_module,
        "search_archive_document_lane",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError()),
    )
    request = ArchiveSearchRequest.create(
        query="private document",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="document-storage-failure",
            turn_ledger=_ledger(),
        )
    payload = _payload(prepared)
    for lane in (SearchLane.CATALOG, SearchLane.LEXICAL):
        item = _coverage(payload, lane)
        assert item["states"] == [
            CoverageState.PARTIAL.value,
            CoverageState.UNAVAILABLE.value,
        ]
        assert item["authority_rechecked"] is False
        assert item["snapshot_current"] is False
    assert payload["absence"] == "not_established"


def test_document_lanes_are_federated_from_the_authoritative_store(storage: Any) -> None:
    authorization = _seed_authority(storage)
    body = "Needle private archive body"
    storage.store_raw_object(
        RawObject(
            id="raw_00000000000000a1",
            user_id=TENANT,
            source="upload",
            source_ref="telegram-file:archive-service",
            raw_content=body,
            content_type="file",
            metadata_json={
                "filename": "Friday Architecture.md",
                "media_kind": "document",
                "mime_type": "text/markdown",
                "uploaded_by": PRINCIPAL,
            },
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
            received_at="2026-08-23T10:00:00+00:00",
            created_at="2026-08-23T10:00:00+00:00",
        )
    )
    storage.store_inbox_item(
        InboxItem(
            id="inbox_00000000000000a1",
            user_id=TENANT,
            raw_object_id="raw_00000000000000a1",
            knowledge_object_id=None,
            status=InboxStatus.CLASSIFIED,
            created_at="2026-08-23T10:01:00+00:00",
            reviewed_at="2026-08-23T10:02:00+00:00",
            reviewed_by=PRINCIPAL,
        )
    )
    request = ArchiveSearchRequest.create(
        query="Needle",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        filename_hints=("Friday Architecture.md",),
        limit=5,
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="document-live",
            turn_ledger=_ledger(),
        )
        assert conn.in_transaction
    payload = _payload(prepared)
    assert len(payload["candidates"]) == 1
    assert _coverage(payload, SearchLane.CATALOG)["states"] == [
        "backfill_pending",
        "partial",
    ]
    assert _coverage(payload, SearchLane.LEXICAL)["states"] == ["partial", "unavailable"]
    assert _coverage(payload, SearchLane.DENSE)["states"] == ["unavailable"]


def test_malformed_document_mime_forces_public_not_established(storage: Any) -> None:
    authorization = _seed_authority(storage)
    body = "Closed extraction-ready document without a format query token."
    raw_id = "raw_00000000000000a2"
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=TENANT,
            source="upload",
            source_ref="telegram-file:archive-malformed-mime",
            raw_content=body,
            content_type="file",
            metadata_json={
                "extraction_error": "",
                "extraction_success": True,
                "filename": "extensionless-document",
                "media_kind": "document",
                "mime": "application/pdf",
                "mime_type": "text/plain",
                "text_extraction_success": True,
                "uploaded_by": PRINCIPAL,
            },
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
            received_at="2026-08-23T10:03:00+00:00",
            created_at="2026-08-23T10:03:00+00:00",
        )
    )
    storage.store_inbox_item(
        InboxItem(
            id="inbox_00000000000000a2",
            user_id=TENANT,
            raw_object_id=raw_id,
            knowledge_object_id=None,
            status=InboxStatus.CLASSIFIED,
            created_at="2026-08-23T10:04:00+00:00",
            reviewed_at="2026-08-23T10:05:00+00:00",
            reviewed_by=PRINCIPAL,
        )
    )
    backfill = storage.backfill_document_catalog(
        TENANT,
        after_raw_object_id=None,
        limit=8,
        include_document_passages=True,
    )
    catalog = storage.document_catalog_coverage(TENANT)
    projection = storage.execute(
        "SELECT projection_status FROM document_passage_projections WHERE raw_object_id=?",
        (raw_id,),
    ).fetchone()
    assert backfill["has_more"] is False
    assert catalog["coverage_complete"] is catalog["enrichment_complete"] is True
    assert projection is not None and projection["projection_status"] == "current"

    request = ArchiveSearchRequest.create(
        query="application/pdf",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="malformed-document-mime",
            turn_ledger=_ledger(),
        )
    payload = _payload(prepared)

    assert payload["candidates"] == []
    assert payload["continuation"] is None
    assert payload["absence"] == "not_established"
    assert payload["exhaustive"] is False
    for lane in (SearchLane.CATALOG, SearchLane.LEXICAL):
        public_coverage = _coverage(payload, lane)
        assert public_coverage["states"] == [
            CoverageState.BACKFILL_PENDING.value,
            CoverageState.PARTIAL.value,
        ]
        assert public_coverage["eligible_authorized"] is None
        assert public_coverage["examined"] == 1
        assert public_coverage["matched_at_least"] == public_coverage["returned"] == 0
        assert public_coverage["authority_rechecked"] is True
        assert public_coverage["snapshot_current"] is True
        assert public_coverage["next_cursor_available"] is False


def test_message_history_uses_authorized_context_and_leaves_other_lanes_unavailable(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    conversation = storage.create_conversation(PRINCIPAL, "Friday project")
    storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "Needle private conversation",
    )
    storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "assistant",
        "Adjacent context",
    )
    boundary = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "current archive request",
    )
    request = ArchiveSearchRequest.create(
        query="Needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        limit=5,
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="messages-live",
            turn_ledger=_ledger(),
            current_conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
        )
    payload = _payload(prepared)
    assert len(payload["candidates"]) == 1
    assert payload["absence"] == "evidence_found"
    assert _coverage(payload, SearchLane.MESSAGE_HISTORY)["states"] == ["complete"]
    assert _coverage(payload, SearchLane.LEXICAL)["states"] == [
        CoverageState.BACKFILL_PENDING.value,
        CoverageState.CAPPED.value,
        CoverageState.PARTIAL.value,
    ]
    assert payload["candidates"][0]["match_channels"] == [ArchiveMatchChannel.MESSAGE_HISTORY.value]
    assert _coverage(payload, SearchLane.DENSE)["states"] == ["unavailable"]


def test_complete_message_history_is_the_authoritative_zero_hit_conversation_lane(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    boundary = _message_boundary(storage)
    request = ArchiveSearchRequest.create(
        query="zero-hit-conversation-canary-absent",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="messages-authoritative-zero-hit",
            turn_ledger=_ledger(),
            current_conversation_id=boundary[0],
            boundary_user_message_id=boundary[1],
        )

    payload = _payload(prepared)
    history = _coverage(payload, SearchLane.MESSAGE_HISTORY)
    lexical = _coverage(payload, SearchLane.LEXICAL)
    assert payload["candidates"] == []
    assert history["states"] == [CoverageState.COMPLETE.value]
    assert history["matched_at_least"] == 0
    assert history["authority_rechecked"] is True
    assert history["snapshot_current"] is True
    assert CoverageState.PARTIAL.value in lexical["states"]
    assert payload["absence"] == "authorized_absence_confirmed"


def test_conversation_lexical_lane_reuses_exact_context_when_legacy_fts_is_unavailable(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    conversation = storage.create_conversation(PRINCIPAL, "Conversation passage source")
    storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "passage-lane-needle private body",
    )
    storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "assistant",
        "Adjacent lexical context",
    )
    boundary = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "current archive request",
    )
    report = storage.backfill_conversation_passages(PRINCIPAL, limit=8)
    assert report["has_more"] is False
    assert report["anchors_written"] == 3
    with storage.transaction() as conn:
        conn.execute("DROP TABLE messages_fts")

    request = ArchiveSearchRequest.create(
        query="passage-lane-needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        context=ArchiveContextWindow(before=0, after=1),
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="conversation-lexical-live",
            turn_ledger=_ledger(),
            current_conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
        )

    payload = _payload(prepared)
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["match_channels"] == [ArchiveMatchChannel.LEXICAL.value]
    assert "Adjacent lexical context" in payload["candidates"][0]["passages"][0]["excerpt"]
    assert _coverage(payload, SearchLane.LEXICAL)["states"] == [
        CoverageState.BACKFILL_PENDING.value,
        CoverageState.CAPPED.value,
        CoverageState.PARTIAL.value,
    ]
    assert _coverage(payload, SearchLane.MESSAGE_HISTORY)["states"] == [
        CoverageState.PARTIAL.value,
        CoverageState.UNAVAILABLE.value,
    ]
    assert _coverage(payload, SearchLane.DENSE)["states"] == [CoverageState.UNAVAILABLE.value]


def test_conversation_lexical_and_legacy_lanes_merge_one_exact_replay_source(storage: Any) -> None:
    authorization = _seed_authority(storage)
    conversation = storage.create_conversation(PRINCIPAL, "Merged conversation source")
    storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "merged-passage-needle private body",
    )
    boundary = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "current archive request",
    )
    assert storage.backfill_conversation_passages(PRINCIPAL, limit=8)["has_more"] is False
    request = ArchiveSearchRequest.create(
        query="merged-passage-needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
    )
    ledger = _ledger()
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="conversation-lexical-merge",
            turn_ledger=ledger,
            current_conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
        )
    payload = _payload(prepared)
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["match_channels"] == [
        ArchiveMatchChannel.LEXICAL.value,
        ArchiveMatchChannel.MESSAGE_HISTORY.value,
    ]
    assert _coverage(payload, SearchLane.LEXICAL)["states"] == [
        CoverageState.BACKFILL_PENDING.value,
        CoverageState.CAPPED.value,
        CoverageState.PARTIAL.value,
    ]
    assert _coverage(payload, SearchLane.MESSAGE_HISTORY)["states"] == [CoverageState.COMPLETE.value]

    ledger.admit_model_tool_bytes(
        prepared.run_binding,
        prepared.authorized_batch,
        prepared.authorized_batch.model_visible_canonical_bytes,
    )
    ledger.freeze_for_publication()
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )
    attestation = attest_archive_search_before_publication(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=ledger,
        answer="One exact merged source",
        candidate_reauthorizer=reauthorize_archive_search_candidate,
        coverage_reauthorizer=reauthorize_archive_search_coverage,
        authority_context=context,
    )
    assert attestation.attests_answer("One exact merged source")


def test_tampered_passage_fts_token_never_becomes_canonical_message_evidence(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    conversation = storage.create_conversation(PRINCIPAL, "Tampered passage token")
    source = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "canonical-body-token private body",
    )
    boundary = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "current archive request",
    )
    assert storage.backfill_conversation_passages(PRINCIPAL, limit=8)["has_more"] is False
    with storage.transaction() as conn:
        row = conn.execute(
            "SELECT passage_rowid FROM conversation_passages WHERE anchor_message_id=?",
            (source["id"],),
        ).fetchone()
        assert row is not None
        passage_rowid = int(row[0])
        conn.execute(
            "INSERT INTO conversation_passages_fts(conversation_passages_fts,rowid,content) "
            "VALUES('delete',?,?)",
            (passage_rowid, "canonical-body-token private body"),
        )
        conn.execute(
            "INSERT INTO conversation_passages_fts(rowid,content) VALUES(?,?)",
            (passage_rowid, "forged-passage-token"),
        )

    forged_request = ArchiveSearchRequest.create(
        query="forged-passage-token",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
    )
    with storage.transaction() as conn:
        forged = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=forged_request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="conversation-lexical-forged-token",
            turn_ledger=_ledger(),
            current_conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
        )
    forged_payload = _payload(forged)
    assert forged_payload["candidates"] == []
    assert (
        CoverageState.BACKFILL_PENDING.value
        in _coverage(
            forged_payload,
            SearchLane.LEXICAL,
        )["states"]
    )
    assert _coverage(forged_payload, SearchLane.MESSAGE_HISTORY)["states"] == [CoverageState.COMPLETE.value]

    canonical_request = ArchiveSearchRequest.create(
        query="canonical-body-token",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
    )
    with storage.transaction() as conn:
        canonical = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=canonical_request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="conversation-lexical-legacy-fallback",
            turn_ledger=_ledger(),
            current_conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
        )
    canonical_payload = _payload(canonical)
    assert len(canonical_payload["candidates"]) == 1
    assert canonical_payload["candidates"][0]["match_channels"] == [ArchiveMatchChannel.MESSAGE_HISTORY.value]
    assert _coverage(canonical_payload, SearchLane.MESSAGE_HISTORY)["states"] == [
        CoverageState.COMPLETE.value
    ]
    assert (
        CoverageState.PARTIAL.value
        in _coverage(
            canonical_payload,
            SearchLane.LEXICAL,
        )["states"]
    )


def test_missing_conversation_passage_fts_degrades_to_complete_legacy_fallback(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    conversation = storage.create_conversation(PRINCIPAL, "Missing passage derivative")
    storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "missing-passage-fts-needle private body",
    )
    boundary = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "current archive request",
    )
    assert storage.backfill_conversation_passages(PRINCIPAL, limit=8)["has_more"] is False
    with storage.transaction() as conn:
        conn.execute("DROP TABLE conversation_passages_fts")

    request = ArchiveSearchRequest.create(
        query="missing-passage-fts-needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="missing-conversation-passage-fts",
            turn_ledger=_ledger(),
            current_conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
        )

    payload = _payload(prepared)
    assert len(payload["candidates"]) == 1
    assert payload["candidates"][0]["match_channels"] == [ArchiveMatchChannel.MESSAGE_HISTORY.value]
    assert _coverage(payload, SearchLane.LEXICAL)["states"] == [
        CoverageState.BACKFILL_PENDING.value,
        CoverageState.PARTIAL.value,
        CoverageState.STALE.value,
    ]
    assert _coverage(payload, SearchLane.MESSAGE_HISTORY)["states"] == [CoverageState.COMPLETE.value]


def test_conversation_lexical_request_is_shape_only_and_rechecks_bounded_selected_sources(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    selected_owner_conversations: set[str] = set()
    for index in range(5):
        conversation = storage.create_conversation(PRINCIPAL, f"Selected lexical source {index}")
        selected_owner_conversations.add(str(conversation["id"]))
        storage.store_message(
            conversation["id"],
            PRINCIPAL,
            "user",
            f"bounded-reader-needle owner body {index}",
        )
    boundary_conversation = storage.create_conversation(PRINCIPAL, "Bounded reader boundary")
    boundary = storage.store_message(
        boundary_conversation["id"],
        PRINCIPAL,
        "user",
        "current archive request",
    )
    foreign = "archive-service-foreign-lexical"
    storage.ensure_user(foreign)
    for index in range(5):
        conversation = storage.create_conversation(foreign, f"Foreign lexical source {index}")
        storage.store_message(
            conversation["id"],
            foreign,
            "user",
            f"bounded-reader-needle foreign body {index}",
        )
    assert storage.backfill_conversation_passages(PRINCIPAL, limit=32)["has_more"] is False
    assert storage.backfill_conversation_passages(foreign, limit=32)["has_more"] is False

    validation_modes: list[tuple[bool, bool]] = []
    real_validate = message_storage_module.validate_conversation_passage_fts_schema

    def recording_validate(  # noqa: ANN001, ANN202
        conn,
        *,
        required=True,
        validate_data=True,
        _register_functions=True,
    ):
        validation_modes.append((bool(validate_data), bool(_register_functions)))
        assert validate_data is False
        assert _register_functions is False
        return real_validate(
            conn,
            required=required,
            validate_data=validate_data,
            _register_functions=_register_functions,
        )

    reader_conversations: list[str] = []
    real_reader = service_module.select_authorized_conversation_passage_projection_in_transaction

    def recording_reader(conn, **kwargs):  # noqa: ANN001, ANN202
        reader_conversations.append(str(kwargs["conversation_id"]))
        return real_reader(conn, **kwargs)

    monkeypatch.setattr(message_storage_module, "_CONVERSATION_LEXICAL_POOL_CAP", 2)
    monkeypatch.setattr(
        message_storage_module,
        "validate_conversation_passage_fts_schema",
        recording_validate,
    )
    monkeypatch.setattr(
        service_module,
        "select_authorized_conversation_passage_projection_in_transaction",
        recording_reader,
    )
    request = ArchiveSearchRequest.create(
        query="bounded-reader-needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="conversation-lexical-bounded-selected-sources",
            turn_ledger=_ledger(),
            current_conversation_id=str(boundary_conversation["id"]),
            boundary_user_message_id=str(boundary["id"]),
        )

    payload = _payload(prepared)
    assert validation_modes and set(validation_modes) == {(False, False)}
    assert reader_conversations
    assert len(reader_conversations) <= 4 * len(validation_modes)
    assert all(reader_conversations.count(item) <= len(validation_modes) for item in reader_conversations)
    assert set(reader_conversations) <= selected_owner_conversations
    assert _coverage(payload, SearchLane.LEXICAL)["states"] == [
        CoverageState.BACKFILL_PENDING.value,
        CoverageState.CAPPED.value,
        CoverageState.PARTIAL.value,
    ]
    assert _coverage(payload, SearchLane.MESSAGE_HISTORY)["states"] == [CoverageState.COMPLETE.value]


def test_conversation_lexical_refills_one_capped_foreign_window_by_rowid(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_authority(storage)
    foreign = "archive-service-refill-foreign"
    storage.ensure_user(foreign)
    foreign_source = storage.create_conversation(foreign, "Earlier foreign refill rows")
    for index in range(3):
        storage.store_message(
            foreign_source["id"],
            foreign,
            "user",
            f"refillwindowneedle foreign posting {index}",
        )
    _converge_conversation_passages(storage, foreign)

    owner_source = storage.create_conversation(PRINCIPAL, "Owner refill target")
    owner_target = storage.store_message(
        owner_source["id"],
        PRINCIPAL,
        "user",
        "refillwindowneedle exact authorized owner target",
    )
    boundary = _message_boundary(storage)
    _converge_conversation_passages(storage, PRINCIPAL)
    monkeypatch.setattr(message_storage_module, "_CONVERSATION_LEXICAL_POOL_CAP", 2)

    with storage.transaction() as conn:
        page = message_storage_module._materialize_authorized_archive_message_page_in_transaction(  # noqa: SLF001
            conn,
            principal_id=PRINCIPAL,
            query="refillwindowneedle",
            selection_lane=SearchLane.LEXICAL,
            conversation_id=boundary[0],
            boundary_user_message_id=boundary[1],
            limit=1,
        )

    assert page is not None and page.has_more is True
    assert tuple(hit.message.message_id for hit in page.hits) == (owner_target["id"],)
    assert {hit.message.principal_id for hit in page.hits} == {PRINCIPAL}


def test_conversation_lexical_skips_refill_when_first_window_fills_limit(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_authority(storage)
    early_source = storage.create_conversation(PRINCIPAL, "Early sufficient owner source")
    early_target = storage.store_message(
        early_source["id"],
        PRINCIPAL,
        "user",
        "sufficientwindowneedle early authorized target",
    )
    _converge_conversation_passages(storage, PRINCIPAL)

    foreign = "archive-service-sufficient-foreign"
    storage.ensure_user(foreign)
    foreign_source = storage.create_conversation(foreign, "Foreign sentinel rows")
    for index in range(2):
        storage.store_message(
            foreign_source["id"],
            foreign,
            "user",
            f"sufficientwindowneedle foreign posting {index}",
        )
    _converge_conversation_passages(storage, foreign)

    later_source = storage.create_conversation(PRINCIPAL, "Later refill-only owner source")
    later_target = storage.store_message(
        later_source["id"],
        PRINCIPAL,
        "user",
        "sufficientwindowneedle newer authorized target",
    )
    boundary = _message_boundary(storage)
    _converge_conversation_passages(storage, PRINCIPAL)
    monkeypatch.setattr(message_storage_module, "_CONVERSATION_LEXICAL_POOL_CAP", 2)

    with storage.transaction() as conn:
        page = message_storage_module._materialize_authorized_archive_message_page_in_transaction(  # noqa: SLF001
            conn,
            principal_id=PRINCIPAL,
            query="sufficientwindowneedle",
            selection_lane=SearchLane.LEXICAL,
            conversation_id=boundary[0],
            boundary_user_message_id=boundary[1],
            limit=1,
        )

    assert page is not None and page.has_more is True
    assert tuple(hit.message.message_id for hit in page.hits) == (early_target["id"],)
    assert later_target["id"] not in {hit.message.message_id for hit in page.hits}


def test_conversation_lexical_stops_after_one_refill_and_history_stays_complete(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    foreign = "archive-service-overcap-foreign"
    storage.ensure_user(foreign)
    foreign_source = storage.create_conversation(foreign, "Two full foreign windows")
    for index in range(4):
        storage.store_message(
            foreign_source["id"],
            foreign,
            "user",
            f"overcapwindowneedle foreign posting {index}",
        )
    _converge_conversation_passages(storage, foreign)

    owner_source = storage.create_conversation(PRINCIPAL, "Owner beyond one refill")
    owner_target = storage.store_message(
        owner_source["id"],
        PRINCIPAL,
        "user",
        "overcapwindowneedle exact owner target beyond the hard cap",
    )
    boundary = _message_boundary(storage)
    _converge_conversation_passages(storage, PRINCIPAL)
    monkeypatch.setattr(message_storage_module, "_CONVERSATION_LEXICAL_POOL_CAP", 2)

    with storage.transaction() as conn:
        lexical = message_storage_module._materialize_authorized_archive_message_page_in_transaction(  # noqa: SLF001
            conn,
            principal_id=PRINCIPAL,
            query="overcapwindowneedle",
            selection_lane=SearchLane.LEXICAL,
            conversation_id=boundary[0],
            boundary_user_message_id=boundary[1],
            limit=1,
        )
        history = message_storage_module._materialize_authorized_archive_message_page_in_transaction(  # noqa: SLF001
            conn,
            principal_id=PRINCIPAL,
            query="overcapwindowneedle",
            selection_lane=SearchLane.MESSAGE_HISTORY,
            conversation_id=boundary[0],
            boundary_user_message_id=boundary[1],
            limit=1,
        )
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=ArchiveSearchRequest.create(
                query="overcapwindowneedle",
                corpora=(ArchiveSearchCorpus.MESSAGES,),
                limit=1,
            ),
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="conversation-lexical-overcap-history",
            turn_ledger=_ledger(),
            current_conversation_id=boundary[0],
            boundary_user_message_id=boundary[1],
        )

    assert lexical is not None and lexical.hits == () and lexical.has_more is True
    assert history is not None
    assert tuple(hit.message.message_id for hit in history.hits) == (owner_target["id"],)
    payload = _payload(prepared)
    assert payload["candidates"][0]["match_channels"] == [ArchiveMatchChannel.MESSAGE_HISTORY.value]
    lexical_coverage = _coverage(payload, SearchLane.LEXICAL)
    assert lexical_coverage["states"] == [
        CoverageState.BACKFILL_PENDING.value,
        CoverageState.CAPPED.value,
        CoverageState.PARTIAL.value,
    ]
    assert lexical_coverage["eligible_authorized"] is None
    assert lexical_coverage["next_cursor_available"] is False
    assert _coverage(payload, SearchLane.MESSAGE_HISTORY)["states"] == [CoverageState.COMPLETE.value]


def test_conversation_lexical_vm_work_is_flat_after_the_global_fts_pool_is_saturated(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_authority(storage)
    foreign = "archive-service-foreign-saturation"
    storage.ensure_user(foreign)
    owner_source = storage.create_conversation(PRINCIPAL, "Bounded lexical owner source")
    owner_hit = storage.store_message(
        owner_source["id"],
        PRINCIPAL,
        "user",
        "flatpoolneedle exact owner hit",
    )
    boundary_conversation = storage.create_conversation(PRINCIPAL, "Bounded lexical boundary")
    small_boundary = storage.store_message(
        boundary_conversation["id"],
        PRINCIPAL,
        "user",
        "small corpus boundary",
    )
    foreign_source = storage.create_conversation(foreign, "Foreign saturated postings")
    # Fill both bounded windows plus the one final sentinel before comparing
    # work against a much larger foreign corpus.
    for index in range(33):
        storage.store_message(
            foreign_source["id"],
            foreign,
            "user",
            f"flatpoolneedle initial foreign posting {index}",
        )

    _converge_conversation_passages(storage, PRINCIPAL)
    _converge_conversation_passages(storage, foreign)
    monkeypatch.setattr(message_storage_module, "_CONVERSATION_LEXICAL_POOL_CAP", 16)

    def optimize_fts() -> None:
        with storage.transaction() as conn:
            conn.execute(
                "INSERT INTO conversation_passages_fts(conversation_passages_fts) VALUES('optimize')"
            )

    def measured(boundary_id: str) -> tuple[Any, int, dict[str, int], tuple[str, ...]]:
        instruction_blocks = 0
        statement = "unknown"
        per_statement: dict[str, int] = {}
        main_sql: list[str] = []

        def trace(sql: str) -> None:
            nonlocal statement
            normalized = " ".join(sql.split())
            if "first_pool_with_sentinel" in sql and not main_sql:
                main_sql.append(sql)
            statement = (
                "main"
                if "first_pool_with_sentinel" in sql
                else "ledger"
                if "selected_owned AS MATERIALIZED" in sql
                else "schema"
                if "sqlite_master" in sql or "table_info" in sql or "foreign_key_list" in sql
                else f"preflight:{normalized[:120]}"
            )

        def progress() -> int:
            nonlocal instruction_blocks
            instruction_blocks += 1
            per_statement[statement] = per_statement.get(statement, 0) + 1
            return 0

        with storage.transaction() as conn:
            conn.set_trace_callback(trace)
            conn.set_progress_handler(progress, 100)
            try:
                page = message_storage_module._materialize_authorized_archive_message_page_in_transaction(  # noqa: SLF001
                    conn,
                    principal_id=PRINCIPAL,
                    query="flatpoolneedle",
                    selection_lane=SearchLane.LEXICAL,
                    scope=message_storage_module.ArchiveMessageScope.ALL,
                    conversation_id=str(boundary_conversation["id"]),
                    boundary_user_message_id=boundary_id,
                    limit=20,
                )
            finally:
                conn.set_progress_handler(None, 0)
                conn.set_trace_callback(None)
            plan = tuple(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + main_sql[0]).fetchall())
        return page, instruction_blocks, per_statement, plan

    def owner_output(page: Any) -> tuple[object, ...]:
        return (
            page.returned,
            page.total,
            page.examined,
            tuple(
                (
                    hit.match_rank,
                    hit.source_rank,
                    hit.lexical_score,
                    hit.message.message_id,
                    tuple((context.relative_position, context.row.message_id) for context in hit.context),
                )
                for hit in page.hits
            ),
        )

    optimize_fts()
    small_page, small_blocks, small_statements, small_plan = measured(str(small_boundary["id"]))
    assert small_page is not None and len(small_page.hits) == 1
    assert small_page.hits[0].message.message_id == owner_hit["id"]

    for index in range(512):
        storage.store_message(
            foreign_source["id"],
            foreign,
            "user",
            f"flatpoolneedle added foreign posting {index}",
        )
    _converge_conversation_passages(storage, foreign)
    optimize_fts()
    foreign_page, foreign_blocks, foreign_statements, foreign_plan = measured(str(small_boundary["id"]))
    assert foreign_page is not None and len(foreign_page.hits) == 1
    assert foreign_page.hits[0].message.message_id == owner_hit["id"]
    assert owner_output(foreign_page) == owner_output(small_page)
    owner_unrelated = storage.create_conversation(PRINCIPAL, "Owner nonmatching corpus")
    for _index in range(512):
        storage.store_message(
            owner_unrelated["id"],
            PRINCIPAL,
            "user",
            "owner nonmatching passage",
        )
    large_boundary = storage.store_message(
        boundary_conversation["id"],
        PRINCIPAL,
        "user",
        "large corpus boundary",
    )
    _converge_conversation_passages(storage, PRINCIPAL)

    optimize_fts()
    large_page, large_blocks, large_statements, large_plan = measured(str(large_boundary["id"]))
    assert large_page is not None and len(large_page.hits) == 1
    assert large_page.hits[0].message.message_id == owner_hit["id"]
    assert foreign_blocks <= small_blocks + max(25, small_blocks // 5), (
        small_blocks,
        foreign_blocks,
        large_blocks,
        small_statements,
        foreign_statements,
        large_statements,
        small_plan,
        foreign_plan,
        large_plan,
    )
    assert large_blocks <= foreign_blocks + max(25, foreign_blocks // 5), (
        small_blocks,
        foreign_blocks,
        large_blocks,
        small_statements,
        foreign_statements,
        large_statements,
        small_plan,
        foreign_plan,
        large_plan,
    )


def test_message_continuation_is_bound_to_the_original_accepted_turn(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    for index in range(2):
        conversation = storage.create_conversation(PRINCIPAL, f"Archive source {index}")
        storage.store_message(
            conversation["id"],
            PRINCIPAL,
            "user",
            f"boundary-continuation-needle source {index}",
        )
    boundary_conversation = storage.create_conversation(PRINCIPAL, "Current turn")
    boundary = storage.store_message(
        boundary_conversation["id"],
        PRINCIPAL,
        "user",
        "current archive request",
    )
    request = ArchiveSearchRequest.create(
        query="boundary-continuation-needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        limit=1,
    )
    ledger = _ledger()
    with storage.transaction() as conn:
        first = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="message-boundary-first",
            turn_ledger=ledger,
            current_conversation_id=boundary_conversation["id"],
            boundary_user_message_id=boundary["id"],
        )
    token = _payload(first)["continuation"]
    assert isinstance(token, str)
    resumed = ArchiveSearchRequest.create(
        query="boundary-continuation-needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        limit=1,
        continuation=token,
    )
    later_boundary = storage.store_message(
        boundary_conversation["id"],
        PRINCIPAL,
        "user",
        "later archive request",
    )
    with storage.transaction() as conn, pytest.raises(ArchiveSearchServiceError):
        prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=resumed,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="message-boundary-drift",
            turn_ledger=ledger,
            current_conversation_id=boundary_conversation["id"],
            boundary_user_message_id=later_boundary["id"],
        )


@pytest.mark.parametrize("mutation", ("update", "delete"))
def test_message_publication_refresh_rejects_pre_boundary_drift(
    storage: Any,
    mutation: str,
) -> None:
    authorization = _seed_authority(storage)
    conversation = storage.create_conversation(PRINCIPAL, "Mutable source")
    source = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "refresh-boundary-needle private body",
    )
    boundary = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "current archive request",
    )
    request = ArchiveSearchRequest.create(
        query="refresh-boundary-needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
    )
    ledger = _ledger()
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator=f"message-refresh-{mutation}",
            turn_ledger=ledger,
            current_conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
        )
    body = prepared.authorized_batch.model_visible_canonical_bytes
    ledger.admit_model_tool_bytes(prepared.run_binding, prepared.authorized_batch, body)
    ledger.freeze_for_publication()
    with storage.transaction() as conn:
        if mutation == "update":
            conn.execute("DROP TRIGGER messages_are_never_rewritten")
            conn.execute(
                "UPDATE messages SET content='changed after model admission' WHERE id=?",
                (source["id"],),
            )
        else:
            conn.execute("DROP TRIGGER messages_are_never_deleted")
            conn.execute("DELETE FROM messages WHERE id=?", (source["id"],))
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )
    with pytest.raises(ArchiveSearchPublicationDenied):
        attest_archive_search_before_publication(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer="Stale answer",
            candidate_reauthorizer=reauthorize_archive_search_candidate,
            coverage_reauthorizer=reauthorize_archive_search_coverage,
            authority_context=context,
        )


def test_message_publication_refresh_rejects_boundary_identity_drift_with_stable_candidates(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    conversation = storage.create_conversation(PRINCIPAL, "Mutable accepted boundary")
    storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "stable-boundary-candidate private body",
    )
    boundary = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "current archive request",
    )
    request = ArchiveSearchRequest.create(
        query="stable-boundary-candidate",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
    )
    ledger = _ledger()
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="message-boundary-identity-refresh",
            turn_ledger=ledger,
            current_conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
        )
    original_candidate = prepared.authorized_batch._page.results[0].candidate.to_private_payload()
    original_boundary_identity = prepared._recipe.accepted_boundary_identity_sha256
    assert original_boundary_identity is not None

    with storage.transaction() as conn:
        conn.execute("DROP TRIGGER messages_are_never_rewritten")
        conn.execute(
            "UPDATE messages SET content='changed accepted boundary' WHERE id=?",
            (boundary["id"],),
        )

    controls = service_module.archive_message_storage_controls(request)
    with storage.transaction() as conn:
        fresh_page = service_module.select_authorized_archive_message_page_in_transaction(
            conn,
            principal_id=PRINCIPAL,
            query=request.query,
            selection_lane=SearchLane.MESSAGE_HISTORY,
            scope=controls["scope"],
            conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
            roles=controls["roles"],
            lifecycle_states=controls["lifecycle_states"],
            since=controls["since"],
            until=controls["until"],
            limit=service_module._materialized_lane_limit(request),
            context_before=controls["context_before"],
            context_after=controls["context_after"],
        )
        assert fresh_page is not None
        assert fresh_page.boundary_identity_sha256 != original_boundary_identity
        fresh_projection = service_module.project_archive_message_page(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            page=fresh_page,
            index_state=CatalogIndexState(
                CatalogIndexLane.LEXICAL,
                CatalogIndexStatus.CURRENT,
                None,
            ),
            execution_binding=prepared.run_binding.execution_binding,
            snapshot_discriminator=prepared._recipe.snapshot_discriminator,
            selection_lane=SearchLane.MESSAGE_HISTORY,
            current_conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
        )
    assert fresh_projection.candidates[0].to_private_payload() == original_candidate

    with storage.transaction() as conn, pytest.raises(ArchiveSearchServiceError):
        refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )


def test_boundary_drift_cannot_hide_behind_two_unchanged_unavailable_message_lanes(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    conversation = storage.create_conversation(PRINCIPAL, "Unavailable message lanes")
    boundary = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "current archive request",
    )
    with storage.transaction() as conn:
        conn.execute("UPDATE schema_meta SET value='stale' WHERE key='fts_build'")
        conn.execute("UPDATE schema_meta SET value='stale' WHERE key='conversation_passage_fts_build'")
    request = ArchiveSearchRequest.create(
        query="no materialized candidate",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="message-boundary-unavailable-refresh",
            turn_ledger=_ledger(),
            current_conversation_id=conversation["id"],
            boundary_user_message_id=boundary["id"],
        )
    payload = _payload(prepared)
    assert payload["candidates"] == []
    assert CoverageState.UNAVAILABLE.value in _coverage(payload, SearchLane.MESSAGE_HISTORY)["states"]
    assert CoverageState.BACKFILL_PENDING.value in _coverage(payload, SearchLane.LEXICAL)["states"]

    with storage.transaction() as conn:
        conn.execute("DROP TRIGGER messages_are_never_rewritten")
        conn.execute(
            "UPDATE messages SET content='changed unavailable boundary' WHERE id=?",
            (boundary["id"],),
        )
    with storage.transaction() as conn, pytest.raises(ArchiveSearchServiceError):
        refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )


def test_obsidian_lanes_verify_exact_bytes_and_merge_one_stable_source(storage: Any) -> None:
    authorization = _seed_authority(storage)
    storage.create_obsidian_bundle(
        PRINCIPAL,
        config_root="/private/config/archive-service",
        database_root="/private/data/archive-service",
        api_endpoint="unix:///private/run/archive-service.sock",
        api_key_ref="secret:obsidian:archive-service",
        server_path="/private/vaults/archive-service",
        folder_id="friday-archive-service",
        setup_token_hash=hashlib.sha256(b"archive-service-token").hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )
    vault = storage.update_obsidian_vault(PRINCIPAL, state="ready")
    body = "Project Phoenix is the private release plan."
    revision = hashlib.sha256(body.encode()).hexdigest()
    binding = storage.upsert_obsidian_note_binding(
        PRINCIPAL,
        vault_id=str(vault["id"]),
        integration_id="archive-service-note",
        current_path="Projects/Phoenix.md",
        current_revision=revision,
        origin="user",
    )
    storage.upsert_obsidian_note_index(
        PRINCIPAL,
        binding_id=str(binding["id"]),
        revision=revision,
        metadata={"aliases": ["Project Phoenix"]},
        metadata_coverage="complete",
        body_text=body,
        body_coverage="complete",
        source_size_bytes=len(body.encode()),
        title="Phoenix",
    )
    reads: list[tuple[str, str, str]] = []

    def exact_reader(vault_id: str, path: str, expected_sha256: str, /) -> bytes:
        reads.append((vault_id, path, expected_sha256))
        assert expected_sha256 == revision
        return body.encode()

    request = ArchiveSearchRequest.create(
        query="Phoenix",
        corpora=(ArchiveSearchCorpus.OBSIDIAN,),
        title_hints=("Phoenix",),
        filename_hints=("Phoenix.md",),
        limit=5,
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="obsidian-live",
            turn_ledger=_ledger(),
            exact_file_reader=exact_reader,
        )
    payload = _payload(prepared)
    assert len(payload["candidates"]) == 1
    assert len(reads) == 2
    assert {item[1] for item in reads} == {"Projects/Phoenix.md"}
    assert _coverage(payload, SearchLane.LEXICAL)["states"] == ["complete"]
    assert _coverage(payload, SearchLane.DENSE)["states"] == ["unavailable"]


@pytest.mark.parametrize(
    ("corpus", "capability"),
    (
        (ArchiveSearchCorpus.DOCUMENTS, "knowledge.read"),
        (ArchiveSearchCorpus.KNOWLEDGE, "knowledge.read"),
        (ArchiveSearchCorpus.MESSAGES, "conversations.read"),
        (ArchiveSearchCorpus.OBSIDIAN, "obsidian.read"),
        (ArchiveSearchCorpus.GENERATED, "knowledge.read"),
        (ArchiveSearchCorpus.WEB, "knowledge.read"),
    ),
)
def test_initial_explicit_corpus_denial_is_honest_and_reads_no_lane(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    corpus: ArchiveSearchCorpus,
    capability: str,
) -> None:
    authorization = _seed_authority(storage)
    storage.set_permission_override(PRINCIPAL, capability, "deny")
    lane_calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        lane_calls.append("called")
        raise AssertionError("a denied corpus reached its storage lane")

    monkeypatch.setattr(service_module, "_collect_document_target", forbidden)
    monkeypatch.setattr(service_module, "_collect_message_target", forbidden)
    monkeypatch.setattr(service_module, "_collect_obsidian_target", forbidden)
    request = ArchiveSearchRequest.create(query="private denied", corpora=(corpus,))
    boundary = _message_boundary(storage) if corpus is ArchiveSearchCorpus.MESSAGES else (None, None)
    if corpus is ArchiveSearchCorpus.MESSAGES:
        monkeypatch.setattr(
            service_module,
            "_accepted_archive_message_boundary_identity_in_transaction",
            forbidden,
        )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator=f"initial-deny-{corpus.value}",
            turn_ledger=_ledger(),
            current_conversation_id=boundary[0],
            boundary_user_message_id=boundary[1],
        )
    payload = _payload(prepared)
    assert lane_calls == []
    assert payload["candidates"] == []
    assert payload["absence"] == "not_established"
    assert all(
        set(item["states"]) == {CoverageState.PARTIAL.value, CoverageState.PERMISSION_FILTERED.value}
        and item["authority_rechecked"] is True
        and item["snapshot_current"] is True
        for item in payload["coverage"]
    )


def test_external_archive_corpus_is_unavailable_and_never_reaches_a_lane(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    lane_calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        lane_calls.append("called")
        raise AssertionError("external archive search attempted an execution lane")

    monkeypatch.setattr(service_module, "_collect_document_target", forbidden)
    monkeypatch.setattr(service_module, "_collect_message_target", forbidden)
    monkeypatch.setattr(service_module, "_collect_obsidian_target", forbidden)
    request = ArchiveSearchRequest.create(
        query="private external",
        corpora=(ArchiveSearchCorpus.EXTERNAL,),
    )
    assert request.permits_outbound is False
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="external-never-outbound",
            turn_ledger=_ledger(),
        )
    payload = _payload(prepared)
    assert lane_calls == []
    assert payload["candidates"] == []
    assert all(item["states"] == [CoverageState.UNAVAILABLE.value] for item in payload["coverage"])
    assert all(
        item.capability is None and item.allowed is False for item in prepared._recipe.target_authority
    )


def test_global_search_capability_remains_the_kernel_boundary(storage: Any) -> None:
    authorization = _seed_authority(storage)
    storage.set_permission_override(PRINCIPAL, "search.use", "deny")
    request = ArchiveSearchRequest.create(
        query="kernel already admitted this search",
        corpora=(ArchiveSearchCorpus.GENERATED,),
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="kernel-search-boundary",
            turn_ledger=_ledger(),
        )
    assert all(
        CoverageState.PERMISSION_FILTERED.value not in item["states"]
        for item in _payload(prepared)["coverage"]
    )


def test_same_value_foreign_actor_and_unbound_principal_fail_closed(storage: Any) -> None:
    authorization = _seed_authority(storage)
    canonical = _actor()
    alien = _AlienActorContext(
        user_id=canonical.user_id,
        preset_key=canonical.preset_key,
        source=canonical.source,
        identity_id=canonical.identity_id,
        session_id=canonical.session_id,
        shared_tenant=canonical.shared_tenant,
        person_id=canonical.person_id,
    )
    request = ArchiveSearchRequest.create(
        query="private actor",
        corpora=(ArchiveSearchCorpus.GENERATED,),
    )
    for actor in (
        alien,
        ActorContext(
            user_id=TENANT,
            preset_key="owner",
            source="archive-service-test",
            person_id=PRINCIPAL,
        ),
    ):
        with storage.transaction() as conn, pytest.raises(ArchiveSearchServiceError):
            prepare_archive_search_in_transaction(
                conn,
                authorization=authorization,
                actor=actor,
                tenant_id=TENANT,
                principal_id=PRINCIPAL,
                request=request,
                snapshot_discriminator=SNAPSHOT,
                run_discriminator=f"actor-spoof-{type(actor).__name__}",
                turn_ledger=_ledger(),
            )


@pytest.mark.parametrize(
    ("corpus", "capability"),
    (
        (ArchiveSearchCorpus.GENERATED, "knowledge.read"),
        (ArchiveSearchCorpus.MESSAGES, "conversations.read"),
        (ArchiveSearchCorpus.OBSIDIAN, "obsidian.read"),
    ),
)
def test_between_phase_explicit_deny_blocks_publication(
    storage: Any,
    corpus: ArchiveSearchCorpus,
    capability: str,
) -> None:
    authorization = _seed_authority(storage)
    request = ArchiveSearchRequest.create(query="private late deny", corpora=(corpus,))
    ledger = _ledger()
    boundary = _message_boundary(storage) if corpus is ArchiveSearchCorpus.MESSAGES else (None, None)
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator=f"late-deny-{corpus.value}",
            turn_ledger=ledger,
            current_conversation_id=boundary[0],
            boundary_user_message_id=boundary[1],
        )
    ledger.admit_model_tool_bytes(
        prepared.run_binding,
        prepared.authorized_batch,
        prepared.authorized_batch.model_visible_canonical_bytes,
    )
    ledger.freeze_for_publication()
    storage.set_permission_override(PRINCIPAL, capability, "deny")
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )
    with pytest.raises(ArchiveSearchPublicationDenied) as denied:
        attest_archive_search_before_publication(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer="This must not publish",
            candidate_reauthorizer=reauthorize_archive_search_candidate,
            coverage_reauthorizer=reauthorize_archive_search_coverage,
            authority_context=context,
        )
    assert denied.value.reason is ArchiveSearchPublicationDenialReason.AUTHORITY_CHANGED


def test_between_phase_explicit_allow_revocation_blocks_publication(storage: Any) -> None:
    authorization = _seed_authority(storage)
    authorization.create_custom_preset(
        "archive_denied",
        "Archive denied",
        set(),
        created_by=PRINCIPAL,
    )
    authorization.set_user_preset(PRINCIPAL, "archive_denied")
    storage.set_permission_override(PRINCIPAL, "knowledge.read", "allow")
    request = ArchiveSearchRequest.create(
        query="private late revoke",
        corpora=(ArchiveSearchCorpus.GENERATED,),
    )
    ledger = _ledger()
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="late-explicit-allow-revoke",
            turn_ledger=ledger,
        )
    ledger.admit_model_tool_bytes(
        prepared.run_binding,
        prepared.authorized_batch,
        prepared.authorized_batch.model_visible_canonical_bytes,
    )
    ledger.freeze_for_publication()
    storage.set_permission_override(PRINCIPAL, "knowledge.read", None)
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )
    with pytest.raises(ArchiveSearchPublicationDenied) as denied:
        attest_archive_search_before_publication(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer="This must not publish",
            candidate_reauthorizer=reauthorize_archive_search_candidate,
            coverage_reauthorizer=reauthorize_archive_search_coverage,
            authority_context=context,
        )
    assert denied.value.reason is ArchiveSearchPublicationDenialReason.AUTHORITY_CHANGED


def _synthetic_candidate(index: int, rank: int) -> ArchiveSearchCandidate:
    raw_id = f"raw_{index:016x}"
    source_ref = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        TENANT,
        PRINCIPAL,
        CanonicalObjectKind.RAW_OBJECT,
        raw_id,
    )
    representation = SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id)
    knowledge = SourceRepresentation(
        RepresentationKind.KNOWLEDGE_OBJECT,
        f"ko_{index:016x}",
    )
    resolved = ResolvedSource.create(
        source_ref=source_ref,
        representations=(representation, knowledge),
        lifecycle=(
            LifecycleRef(representation, LifecycleState.ACTIVE),
            LifecycleRef(knowledge, LifecycleState.ACTIVE),
        ),
        revisions=(
            SourceRevision(
                representation,
                RevisionKind.RAW_CONTENT_SHA256,
                f"{index:x}" * 64,
            ),
            SourceRevision(knowledge, RevisionKind.KNOWLEDGE_VERSION, "1"),
        ),
        revalidation_targets=(
            RevalidationTarget(representation, AuthorityScope.TENANT_PRINCIPAL),
            RevalidationTarget(knowledge, AuthorityScope.TENANT_PRINCIPAL),
        ),
    )
    return ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        resolved_source=resolved,
        title=f"Document {index}",
        review_state=ArchiveReviewState.CONFIRMED,
        evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
        lifecycle_state=LifecycleState.ACTIVE,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.CATALOG, rank),),
    )


def _synthetic_federation(_conn: sqlite3.Connection, **values: Any):
    recipe = values["recipe"]
    run = values["run"]
    binding = run.execution_binding
    targets = canonical_archive_search_targets(recipe.request)
    candidates = tuple(_synthetic_candidate(index, index) for index in range(1, 4))
    by_target: dict[tuple[SearchCorpus, SearchLane], tuple[ArchiveSearchCandidate, ...]] = {
        target: () for target in targets
    }
    by_target[(SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG)] = candidates
    coverage: list[SearchCoverage] = []
    for target in targets:
        if target == (SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG):
            coverage.append(
                SearchCoverage.create(
                    corpus=target[0],
                    lane=target[1],
                    execution_binding=binding,
                    states=(CoverageState.COMPLETE,),
                    eligible_authorized=3,
                    examined=3,
                    matched_at_least=3,
                    returned=3,
                    authority_rechecked=True,
                    snapshot_current=True,
                )
            )
        else:
            coverage.append(
                SearchCoverage.create(
                    corpus=target[0],
                    lane=target[1],
                    execution_binding=binding,
                    states=(CoverageState.UNAVAILABLE,),
                    eligible_authorized=None,
                    examined=0,
                    matched_at_least=0,
                    returned=0,
                    authority_rechecked=True,
                    snapshot_current=True,
                )
            )
    return federate_archive_search(
        request=recipe.request,
        execution_binding=binding,
        coverage=tuple(coverage),
        candidates_by_target=by_target,
    )


def test_sealed_corpus_authority_rejects_a_federation_bypass(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    storage.set_permission_override(PRINCIPAL, "knowledge.read", "deny")
    monkeypatch.setattr(
        service_module,
        "_collect_federated_in_transaction",
        _synthetic_federation,
    )
    request = ArchiveSearchRequest.create(
        query="private bypass",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=1,
    )
    with storage.transaction() as conn, pytest.raises(ArchiveSearchServiceError):
        prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="federation-bypass",
            turn_ledger=_ledger(),
        )


def test_continuation_cannot_widen_a_previously_denied_corpus(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    storage.set_permission_override(PRINCIPAL, "obsidian.read", "deny")
    obsidian_calls: list[tuple[SearchCorpus, SearchLane]] = []

    def synthetic_document_lane(
        _conn: sqlite3.Connection,
        **values: Any,
    ) -> tuple[tuple[ArchiveSearchCandidate, ...], SearchCoverage]:
        target = values["target"]
        binding = values["run"].execution_binding
        candidates = (
            tuple(_synthetic_candidate(index, index) for index in range(1, 4))
            if target == (SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG)
            else ()
        )
        return candidates, SearchCoverage.create(
            corpus=target[0],
            lane=target[1],
            execution_binding=binding,
            states=((CoverageState.COMPLETE,) if candidates else (CoverageState.UNAVAILABLE,)),
            eligible_authorized=3 if candidates else None,
            examined=3 if candidates else 0,
            matched_at_least=3 if candidates else 0,
            returned=3 if candidates else 0,
            authority_rechecked=True,
            snapshot_current=True,
        )

    def observed_obsidian_lane(
        _conn: sqlite3.Connection,
        **values: Any,
    ) -> tuple[tuple[ArchiveSearchCandidate, ...], SearchCoverage]:
        target = values["target"]
        obsidian_calls.append(target)
        return (), service_module._unsupported(
            target,
            values["run"].execution_binding,
        )

    monkeypatch.setattr(
        service_module,
        "_collect_document_target",
        synthetic_document_lane,
    )
    monkeypatch.setattr(
        service_module,
        "_collect_obsidian_target",
        observed_obsidian_lane,
    )
    ledger = _ledger()
    request = ArchiveSearchRequest.create(
        query="private continuation",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.OBSIDIAN),
        limit=1,
    )
    with storage.transaction() as conn:
        first = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="authority-continuation-first",
            turn_ledger=ledger,
        )
    first_payload = _payload(first)
    assert isinstance(first_payload["continuation"], str)
    assert obsidian_calls == []

    storage.set_permission_override(PRINCIPAL, "obsidian.read", None)
    resumed = ArchiveSearchRequest.create(
        query="private continuation",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.OBSIDIAN),
        limit=1,
        continuation=first_payload["continuation"],
    )
    with storage.transaction() as conn:
        second = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=resumed,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="authority-continuation-second",
            turn_ledger=ledger,
        )
    assert obsidian_calls == []
    assert all(
        item.allowed is False
        for item in second._recipe.target_authority
        if item.corpus is SearchCorpus.OBSIDIAN
    )
    assert all(
        item["corpus"] == ArchiveSearchCorpus.DOCUMENTS.value for item in _payload(second)["candidates"]
    )


def test_deterministic_continuation_reauthorizes_against_fresh_full_universe(
    monkeypatch: pytest.MonkeyPatch,
    storage: Any,
) -> None:
    monkeypatch.setattr(
        service_module,
        "_collect_federated_in_transaction",
        _synthetic_federation,
    )
    authorization = _seed_authority(storage)
    ledger = _ledger()
    initial_request = ArchiveSearchRequest.create(
        query="private documents",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=1,
    )
    with storage.transaction() as conn:
        first = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=initial_request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="continuation-first",
            turn_ledger=ledger,
        )
    first_payload = _payload(first)
    assert len(first_payload["candidates"]) == 1
    assert isinstance(first_payload["continuation"], str)

    resumed_request = ArchiveSearchRequest.create(
        query="private documents",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=1,
        continuation=first_payload["continuation"],
    )
    with storage.transaction() as conn:
        second = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=resumed_request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="continuation-second",
            turn_ledger=ledger,
        )
    second_payload = _payload(second)
    assert len(second_payload["candidates"]) == 1
    assert second_payload["continuation"] is not None
    assert first_payload["candidates"][0] != second_payload["candidates"][0]
    assert first_payload["candidates"][0]["label"] == "A1"
    assert second_payload["candidates"][0]["label"] == "A21"
    for prepared in (first, second):
        ledger.admit_model_tool_bytes(
            prepared.run_binding,
            prepared.authorized_batch,
            prepared.authorized_batch.model_visible_canonical_bytes,
        )
    ledger.freeze_for_publication()
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(first, second),
        )
    attestation = attest_archive_search_before_publication(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=ledger,
        answer="Two exact archive pages",
        candidate_reauthorizer=reauthorize_archive_search_candidate,
        coverage_reauthorizer=reauthorize_archive_search_coverage,
        authority_context=context,
    )
    assert attestation.attests_answer("Two exact archive pages")


def test_same_json_foreign_nested_type_invalidates_candidate_and_prepared_carrier(
    monkeypatch: pytest.MonkeyPatch,
    storage: Any,
) -> None:
    canonical = _synthetic_candidate(1, 1)
    alien = _synthetic_candidate(1, 1)
    object.__setattr__(alien, "review_state", _AlienReviewState.CONFIRMED)
    assert alien.to_private_json() == canonical.to_private_json()
    assert not service_module._same_candidate(alien, canonical)

    monkeypatch.setattr(
        service_module,
        "_collect_federated_in_transaction",
        _synthetic_federation,
    )
    authorization = _seed_authority(storage)
    request = ArchiveSearchRequest.create(
        query="private documents",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=1,
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="same-json-spoof",
            turn_ledger=_ledger(),
        )
    run = prepared.run_binding
    batch = prepared.authorized_batch
    carried = batch._page.results[0].candidate
    object.__setattr__(carried, "review_state", _AlienReviewState.CONFIRMED)
    with storage.transaction() as conn, pytest.raises(ArchiveSearchServiceError):
        refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )
    assert run.execution_binding.is_live_private_request_binding


def test_publication_refresh_attests_exact_batch_and_rejects_wrong_actor(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    request = ArchiveSearchRequest.create(
        query="private artifact",
        corpora=(ArchiveSearchCorpus.GENERATED,),
    )
    ledger = _ledger()
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="publication",
            turn_ledger=ledger,
        )
    body = prepared.authorized_batch.model_visible_canonical_bytes
    ledger.admit_model_tool_bytes(
        prepared.run_binding,
        prepared.authorized_batch,
        body,
    )
    ledger.freeze_for_publication()
    with storage.transaction() as conn, pytest.raises(ArchiveSearchServiceError):
        refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id="wrong-principal",
            prepared_searches=(prepared,),
        )
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )
    attestation = attest_archive_search_before_publication(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=ledger,
        answer="Bounded answer",
        candidate_reauthorizer=reauthorize_archive_search_candidate,
        coverage_reauthorizer=reauthorize_archive_search_coverage,
        authority_context=context,
    )
    assert attestation.attests_answer("Bounded answer")
    with pytest.raises(ArchiveSearchPublicationDenied):
        attest_archive_search_before_publication(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer="Replay",
            candidate_reauthorizer=reauthorize_archive_search_candidate,
            coverage_reauthorizer=reauthorize_archive_search_coverage,
            authority_context=context,
        )


def test_publication_refresh_reproduces_honestly_degraded_storage_coverage(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    monkeypatch.setattr(
        service_module,
        "search_archive_document_lane",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError()),
    )
    request = ArchiveSearchRequest.create(
        query="missing storage",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
    )
    ledger = _ledger()
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="degraded-publication",
            turn_ledger=ledger,
        )
    body = prepared.authorized_batch.model_visible_canonical_bytes
    ledger.admit_model_tool_bytes(prepared.run_binding, prepared.authorized_batch, body)
    ledger.freeze_for_publication()
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )
    attestation = attest_archive_search_before_publication(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=ledger,
        answer="Coverage is unavailable",
        candidate_reauthorizer=reauthorize_archive_search_candidate,
        coverage_reauthorizer=reauthorize_archive_search_coverage,
        authority_context=context,
    )
    assert attestation.attests_answer("Coverage is unavailable")
