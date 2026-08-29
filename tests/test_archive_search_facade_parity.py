from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import replace
from typing import Any

import pytest

from friday.document_catalog.passage_projection import DocumentPassageProjection
from friday.document_catalog.passage_schema import document_passage_set_sha256
from friday.execution_kernel import ExecutionKernel
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval.archive_search_authority import (
    abandon_empty_archive_model_batch_ledger,
    attest_archive_search_before_publication,
    consume_archive_model_batch_ledger_fail_closed,
    create_archive_model_batch_ledger,
)
from friday.retrieval.archive_search_contract import (
    ArchiveEvidenceAuthority,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
)
from friday.retrieval.archive_search_document_locator import (
    DOCUMENT_STORED_PASSAGE_INDEX_VERSION,
    LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION,
)
from friday.retrieval.archive_search_service import (
    prepare_archive_search_in_transaction,
    reauthorize_archive_search_candidate,
    reauthorize_archive_search_coverage,
    refresh_archive_search_reauthorization_in_transaction,
)
from friday.retrieval.contracts import CoverageState, SearchCorpus, SearchLane
from friday.storage.models import InboxItem, InboxStatus, KnowledgeObject, RawObject

_SNAPSHOT = "s4-r3-facade-parity-snapshot"
_TURNS = itertools.count(1)


def _opaque_id(prefix: str, number: int) -> str:
    return f"{prefix}_{number:016x}"


def _actor(tenant: str, principal: str, *, shared: bool) -> ActorContext:
    return ActorContext(
        user_id=tenant,
        preset_key="user",
        source="archive-facade-parity-test",
        shared_tenant=shared,
        person_id=principal,
    )


def _seed_document(
    storage: Any,
    number: int,
    *,
    tenant: str,
    owner: str,
    body: str,
    filename: str,
    source_ref: str,
    inbox_status: InboxStatus | None = None,
    promote: bool = False,
    received_at: str | None = None,
) -> tuple[str, str | None]:
    storage.ensure_user(tenant, preset_key="owner")
    storage.ensure_user(owner, preset_key="user")
    raw_id = _opaque_id("raw", number)
    at = received_at or f"2026-08-28T10:{number % 60:02d}:00+00:00"
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=tenant,
            source="upload",
            source_ref=source_ref,
            raw_content=body,
            content_type="file",
            metadata_json={
                "filename": filename,
                "media_kind": "document",
                "mime_type": "text/plain",
                "uploaded_by": owner,
                "extraction_success": True,
                "text_extraction_success": True,
            },
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
            received_at=at,
            created_at=at,
        )
    )
    knowledge_id: str | None = None
    if promote:
        knowledge_id = _opaque_id("ko", number)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=knowledge_id,
                user_id=tenant,
                raw_object_id=raw_id,
                content=body,
                content_type="document",
                title=filename,
                summary=body,
                created_at=at,
                updated_at=at,
            )
        )
    if inbox_status is not None:
        storage.store_inbox_item(
            InboxItem(
                id=_opaque_id("inbox", number),
                user_id=tenant,
                raw_object_id=raw_id,
                knowledge_object_id=knowledge_id,
                status=inbox_status,
                created_at=at,
                reviewed_at=None if inbox_status is InboxStatus.PENDING else at,
                reviewed_by=None if inbox_status is InboxStatus.PENDING else owner,
            )
        )
    return raw_id, knowledge_id


def _new_ledger(tenant: str, principal: str):
    return create_archive_model_batch_ledger(
        tenant_id=tenant,
        principal_id=principal,
        turn_discriminator=f"archive-facade-parity-turn-{next(_TURNS)}",
    )


def _prepare(
    storage: Any,
    authorization: AuthorizationService,
    actor: ActorContext,
    request: ArchiveSearchRequest,
    *,
    ledger: Any | None = None,
    current_conversation_id: str | None = None,
    boundary_user_message_id: str | None = None,
):
    selected_ledger = ledger or _new_ledger(actor.user_id, actor.own_id)
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=actor,
            tenant_id=actor.user_id,
            principal_id=actor.own_id,
            request=request,
            snapshot_discriminator=_SNAPSHOT,
            run_discriminator=f"archive-facade-parity-run-{next(_TURNS)}",
            turn_ledger=selected_ledger,
            current_conversation_id=current_conversation_id,
            boundary_user_message_id=boundary_user_message_id,
        )
    return prepared, selected_ledger


def _private_candidates(prepared: Any) -> tuple[Any, ...]:
    return tuple(
        result.candidate
        for result in prepared.authorized_batch._page.results  # noqa: SLF001 - parity oracle
    )


def _source_handle(source_id: str) -> str:
    return hashlib.sha256(f"archive-facade-parity\0{source_id}".encode()).hexdigest()


def _normalized_body_free_artifact(
    prepared: Any,
    *,
    legacy_source_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    candidates = _private_candidates(prepared)
    return {
        "schema": "friday.test.archive-facade-parity.body-free.v1",
        "archive": [
            {
                "corpus": candidate.corpus.value,
                "evidence_authority": candidate.evidence_authority.value,
                "matches": [
                    {"channel": match.channel.value, "rank": match.rank} for match in candidate.matches
                ],
                "passage_index_versions": [
                    passage.passage_ref.passage_index_version for passage in candidate.passages
                ],
                "source_handle": _source_handle(candidate.resolved_source.source_ref.canonical_object_id),
            }
            for candidate in candidates
        ],
        "coverage": [
            {
                "corpus": coverage.corpus.value,
                "lane": coverage.lane.value,
                "next_cursor_available": coverage.next_cursor_available,
                "returned": coverage.returned,
                "states": [state.value for state in coverage.states],
            }
            for coverage in prepared.authorized_batch._page.coverage  # noqa: SLF001
        ],
        "legacy_source_handles": [_source_handle(source_id) for source_id in legacy_source_ids],
    }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _project_current_document_passages(storage: Any, raw_id: str) -> None:
    with storage.transaction() as conn:
        source = conn.execute(
            "SELECT version,content_hash,raw_content FROM raw_objects WHERE id=?",
            (raw_id,),
        ).fetchone()
        assert source is not None
        projection = DocumentPassageProjection.from_complete_text(
            raw_object_id=raw_id,
            source_version=int(source["version"]),
            source_content_sha256=str(source["content_hash"]),
            extracted_text=str(source["raw_content"]),
        )
        passage_rows = tuple(
            (
                passage.chunk_index,
                passage.start_char,
                passage.end_char,
                passage.content_sha256,
            )
            for passage in projection.passages
        )
        conn.execute(
            """UPDATE document_passage_projections
                  SET source_version=?,source_content_sha256=?,
                      extracted_text_sha256=?,source_char_count=?,
                      passage_set_sha256=?,passage_index_revision=?,
                      projection_status='current',incomplete_reason=NULL,
                      passage_count=?,projected_at='2026-08-29T12:00:00Z'
                WHERE raw_object_id=?""",
            (
                projection.source_version,
                projection.source_content_sha256,
                projection.extracted_text_sha256,
                projection.source_char_count,
                document_passage_set_sha256(passage_rows),
                projection.passage_index_revision,
                len(projection.passages),
                projection.raw_object_id,
            ),
        )
        conn.executemany(
            """INSERT INTO document_passages(
                   raw_object_id,chunk_index,start_char,end_char,content_sha256
               ) VALUES(?,?,?,?,?)""",
            (
                (
                    raw_id,
                    passage.chunk_index,
                    passage.start_char,
                    passage.end_char,
                    passage.content_sha256,
                )
                for passage in projection.passages
            ),
        )


@pytest.mark.asyncio
async def test_literal_promoted_knowledge_matches_memory_fallback_body_free(storage: Any) -> None:
    owner = "facade-parity-knowledge-owner"
    foreign = "facade-parity-knowledge-foreign"
    query = "literalpromotedmembership"
    privacy = "PRIVATE-KNOWLEDGE-BODY-AND-PATH-SENTINEL"
    target_raw, target_knowledge = _seed_document(
        storage,
        101,
        tenant=owner,
        owner=owner,
        body=f"{query} {privacy}",
        filename=f"{privacy}.txt",
        source_ref=f"/private/{privacy}/target.txt",
        inbox_status=InboxStatus.CLASSIFIED,
        promote=True,
    )
    foreign_raw, foreign_knowledge = _seed_document(
        storage,
        102,
        tenant=foreign,
        owner=foreign,
        body=f"{query} FOREIGN-KNOWLEDGE-DECOY",
        filename="foreign-decoy.txt",
        source_ref="/private/foreign-decoy.txt",
        inbox_status=InboxStatus.CLASSIFIED,
        promote=True,
    )
    assert target_knowledge is not None and foreign_knowledge is not None

    authorization = AuthorizationService(storage)
    actor = authorization.actor_for_user(owner, source="archive-facade-parity-test")
    kernel = ExecutionKernel(authorization)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    legacy = await kernel._memory_search(actor=actor, query=query, limit=20)  # noqa: SLF001

    request = ArchiveSearchRequest.create(
        query=query,
        corpora=(ArchiveSearchCorpus.KNOWLEDGE,),
        limit=20,
    )
    prepared, ledger = _prepare(storage, authorization, actor, request)
    candidates = _private_candidates(prepared)
    legacy_knowledge_ids = tuple(item["id"] for item in legacy["results"])
    normalized_legacy_ids = tuple(
        {target_knowledge: target_raw, foreign_knowledge: foreign_raw}[knowledge_id]
        for knowledge_id in legacy_knowledge_ids
    )

    assert legacy_knowledge_ids == (target_knowledge,)
    assert (
        tuple(candidate.resolved_source.source_ref.canonical_object_id for candidate in candidates)
        == normalized_legacy_ids
        == (target_raw,)
    )
    artifact_json = _canonical_json(
        _normalized_body_free_artifact(
            prepared,
            legacy_source_ids=normalized_legacy_ids,
        )
    )
    for private in (
        privacy,
        query,
        owner,
        foreign,
        target_raw,
        target_knowledge,
        foreign_raw,
        foreign_knowledge,
    ):
        assert private not in artifact_json
    assert "FOREIGN-KNOWLEDGE-DECOY" not in json.dumps(legacy, ensure_ascii=False)
    abandon_empty_archive_model_batch_ledger(ledger)


@pytest.mark.asyncio
async def test_pending_source_matches_source_search_but_cannot_be_publication_evidence(
    storage: Any,
) -> None:
    tenant = "facade-parity-shared-tenant"
    principal = "facade-parity-source-owner"
    other_principal = "facade-parity-source-neighbour"
    foreign_tenant = "facade-parity-source-foreign-tenant"
    query = "pendingmembershipneedle"
    privacy = "PRIVATE-PENDING-BODY-AND-PATH-SENTINEL"
    target_raw, _ = _seed_document(
        storage,
        201,
        tenant=tenant,
        owner=principal,
        body=f"{query} {privacy}",
        filename=f"{privacy}.txt",
        source_ref=f"/private/{privacy}/target.txt",
        inbox_status=InboxStatus.PENDING,
    )
    same_tenant_decoy, _ = _seed_document(
        storage,
        202,
        tenant=tenant,
        owner=other_principal,
        body=f"{query} CROSS-PRINCIPAL-PRIVATE-DECOY",
        filename="cross-principal.txt",
        source_ref="/private/cross-principal.txt",
        inbox_status=InboxStatus.PENDING,
    )
    cross_tenant_decoy, _ = _seed_document(
        storage,
        203,
        tenant=foreign_tenant,
        owner=principal,
        body=f"{query} CROSS-TENANT-PRIVATE-DECOY",
        filename="cross-tenant.txt",
        source_ref="/private/cross-tenant.txt",
        inbox_status=InboxStatus.PENDING,
    )

    authorization = AuthorizationService(storage, shared_tenant=tenant)
    actor = _actor(tenant, principal, shared=True)
    kernel = ExecutionKernel(authorization)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    legacy = await kernel._source_search(actor=actor, query=query, limit=20)  # noqa: SLF001

    request = ArchiveSearchRequest.create(
        query=query,
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=20,
    )
    prepared, ledger = _prepare(storage, authorization, actor, request)
    payload = prepared.authorized_batch.public_tool_result_payload
    candidates = _private_candidates(prepared)

    assert [item["raw_object_id"] for item in legacy["results"]] == [target_raw]
    assert len(candidates) == len(payload["candidates"]) == 1
    candidate = candidates[0]
    public_candidate = payload["candidates"][0]
    assert candidate.resolved_source.source_ref.canonical_object_id == target_raw
    assert candidate.evidence_authority is ArchiveEvidenceAuthority.NONCANONICAL
    assert public_candidate["review_state"] == "pending"
    assert public_candidate["evidence_authority"] == "noncanonical"
    assert public_candidate["navigation_only"] is False
    assert public_candidate["passages"]

    leaked_views = json.dumps(
        {"archive": payload, "legacy": legacy},
        ensure_ascii=False,
        default=dict,
    )
    for private in (
        "CROSS-PRINCIPAL-PRIVATE-DECOY",
        "CROSS-TENANT-PRIVATE-DECOY",
        same_tenant_decoy,
        cross_tenant_decoy,
        other_principal,
        foreign_tenant,
    ):
        assert private not in leaked_views

    ledger.admit_model_tool_bytes(
        prepared.run_binding,
        prepared.authorized_batch,
        prepared.authorized_batch.model_visible_canonical_bytes,
    )
    ledger.freeze_for_publication()
    with storage.transaction() as conn:
        authority_context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=actor,
            tenant_id=tenant,
            principal_id=principal,
            prepared_searches=(prepared,),
        )
    attestation = attest_archive_search_before_publication(
        tenant_id=tenant,
        principal_id=principal,
        ledger=ledger,
        answer="Pending discovery is visible but noncanonical [A1.1].",
        candidate_reauthorizer=reauthorize_archive_search_candidate,
        coverage_reauthorizer=reauthorize_archive_search_coverage,
        authority_context=authority_context,
    )
    assert attestation.candidate_count == 1
    assert attestation.used_citation_labels == ("A1.1",)
    assert attestation.selected_evidence is None
    assert attestation.candidate_projection.candidate_count == 0
    assert attestation.candidate_projection.candidates == ()

    artifact_json = _canonical_json(
        _normalized_body_free_artifact(
            prepared,
            legacy_source_ids=(target_raw,),
        )
    )
    for private in (privacy, query, tenant, principal, target_raw):
        assert private not in artifact_json


def test_mixed_v1_v2_document_locators_preserve_federated_membership_and_rank(
    storage: Any,
) -> None:
    tenant = "facade-parity-locator-tenant"
    principal = "facade-parity-locator-owner"
    query = "mixedlocatorneedle"
    privacy = "PRIVATE-MIXED-LOCATOR-BODY-AND-PATH-SENTINEL"
    first_raw, _ = _seed_document(
        storage,
        301,
        tenant=tenant,
        owner=principal,
        body=f"{query} first {privacy}",
        filename=f"first-{privacy}.txt",
        source_ref=f"/private/{privacy}/first.txt",
        inbox_status=InboxStatus.CLASSIFIED,
        received_at="2026-08-28T10:01:00+00:00",
    )
    second_raw, _ = _seed_document(
        storage,
        302,
        tenant=tenant,
        owner=principal,
        body=f"{query} second {privacy}",
        filename=f"second-{privacy}.txt",
        source_ref=f"/private/{privacy}/second.txt",
        inbox_status=InboxStatus.CLASSIFIED,
        received_at="2026-08-28T10:02:00+00:00",
    )
    authorization = AuthorizationService(storage, shared_tenant=tenant)
    actor = _actor(tenant, principal, shared=True)
    request = ArchiveSearchRequest.create(
        query=query,
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=20,
    )

    before, before_ledger = _prepare(storage, authorization, actor, request)
    before_candidates = _private_candidates(before)
    before_signature = tuple(
        (
            candidate.resolved_source.source_ref.canonical_object_id,
            tuple((match.channel, match.rank) for match in candidate.matches),
        )
        for candidate in before_candidates
    )
    assert {item[0] for item in before_signature} == {first_raw, second_raw}
    assert {
        passage.passage_ref.passage_index_version
        for candidate in before_candidates
        for passage in candidate.passages
    } == {LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION}

    _project_current_document_passages(storage, first_raw)
    after, after_ledger = _prepare(storage, authorization, actor, request)
    after_candidates = _private_candidates(after)
    after_signature = tuple(
        (
            candidate.resolved_source.source_ref.canonical_object_id,
            tuple((match.channel, match.rank) for match in candidate.matches),
        )
        for candidate in after_candidates
    )
    assert after_signature == before_signature
    locator_versions = {
        candidate.resolved_source.source_ref.canonical_object_id: tuple(
            passage.passage_ref.passage_index_version for passage in candidate.passages
        )
        for candidate in after_candidates
    }
    assert locator_versions == {
        first_raw: (DOCUMENT_STORED_PASSAGE_INDEX_VERSION,),
        second_raw: (LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION,),
    }
    lexical_coverage = next(
        coverage
        for coverage in after.authorized_batch._page.coverage  # noqa: SLF001
        if coverage.corpus is SearchCorpus.RAW_DOCUMENTS and coverage.lane is SearchLane.LEXICAL
    )
    assert lexical_coverage.states == (
        CoverageState.BACKFILL_PENDING,
        CoverageState.PARTIAL,
    )
    assert lexical_coverage.next_cursor_available is False

    artifact_json = _canonical_json(_normalized_body_free_artifact(after))
    for private in (privacy, query, tenant, principal, first_raw, second_raw):
        assert private not in artifact_json
    abandon_empty_archive_model_batch_ledger(before_ledger)
    abandon_empty_archive_model_batch_ledger(after_ledger)


def _insert_ranked_message(
    conn: Any,
    *,
    principal: str,
    number: int,
    content: str,
    created_at: str,
) -> tuple[str, str]:
    conversation_id = _opaque_id("conv", number)
    message_id = _opaque_id("msg", number)
    conn.execute(
        """INSERT INTO conversations(
               id,user_id,title,last_message,unread_count,is_pinned,is_archived,
               mode,created_at,updated_at
           ) VALUES(?,?,?,'',0,0,0,'dialogue',?,?)""",
        (conversation_id, principal, f"rank-{number:02d}", created_at, created_at),
    )
    conn.execute(
        """INSERT INTO messages(
               id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
           ) VALUES(?,?,?,'assistant',?,'{}',NULL,?)""",
        (message_id, conversation_id, principal, content, created_at),
    )
    return conversation_id, message_id


def test_rank_twenty_one_message_is_reachable_by_real_facade_continuation(
    storage: Any,
) -> None:
    principal = "facade-parity-message-owner"
    query = "ranktwentyone parityneedle"
    target_marker = "RANK-21-TARGET-PRIVATE-SENTINEL"
    boundary_marker = "ACCEPTED-BOUNDARY-PRIVATE-SENTINEL"
    post_boundary_marker = "POST-BOUNDARY-PRIVATE-SENTINEL"
    storage.ensure_user(principal, preset_key="user")
    with storage.transaction() as conn:
        source_conversations = []
        for number in range(1, 22):
            conversation_id, _message_id = _insert_ranked_message(
                conn,
                principal=principal,
                number=number,
                content=(f"{query} {target_marker}" if number == 1 else f"{query} newer-decoy-{number:02d}"),
                created_at=f"2026-08-{number:02d}T10:00:00+00:00",
            )
            source_conversations.append(conversation_id)
        boundary_conversation, _ = _insert_ranked_message(
            conn,
            principal=principal,
            number=0xF001,
            content="unrelated assistant row",
            created_at="2026-08-29T09:59:00+00:00",
        )
        boundary_message = _opaque_id("msg", 0xF002)
        conn.execute(
            """INSERT INTO messages(
                   id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
               ) VALUES(?,?,?,'user',?,'{}',NULL,?)""",
            (
                boundary_message,
                boundary_conversation,
                principal,
                boundary_marker,
                "2026-08-29T10:00:00+00:00",
            ),
        )
        conn.execute(
            """INSERT INTO messages(
                   id,conversation_id,user_id,role,content,metadata_json,reply_to,created_at
               ) VALUES(?,?,?,'assistant',?,'{}',NULL,?)""",
            (
                _opaque_id("msg", 0xF003),
                source_conversations[0],
                principal,
                f"{query} {post_boundary_marker}",
                "2026-08-30T10:00:00+00:00",
            ),
        )

    authorization = AuthorizationService(storage)
    actor = authorization.actor_for_user(principal, source="archive-facade-parity-test")
    request = ArchiveSearchRequest.create(
        query=query,
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        limit=20,
    )
    ledger = _new_ledger(principal, principal)
    first, _ = _prepare(
        storage,
        authorization,
        actor,
        request,
        ledger=ledger,
        current_conversation_id=boundary_conversation,
        boundary_user_message_id=boundary_message,
    )
    first_payload = first.authorized_batch.public_tool_result_payload
    first_sources = {
        candidate.resolved_source.source_ref.canonical_object_id for candidate in _private_candidates(first)
    }
    assert len(source_conversations) == 21
    assert first_payload["candidates"]
    assert source_conversations[0] not in first_sources
    assert isinstance(first_payload["continuation"], str)
    first_coverage = next(
        item
        for item in first_payload["coverage"]
        if item["corpus"] == SearchCorpus.CONVERSATION.value
        and item["lane"] == SearchLane.MESSAGE_HISTORY.value
    )
    assert {CoverageState.CAPPED.value, CoverageState.PARTIAL.value} <= set(first_coverage["states"])
    assert first_coverage["next_cursor_available"] is True

    pages = [first]
    continuation = first_payload["continuation"]
    while continuation is not None:
        page, _ = _prepare(
            storage,
            authorization,
            actor,
            replace(request, continuation=continuation),
            ledger=ledger,
            current_conversation_id=boundary_conversation,
            boundary_user_message_id=boundary_message,
        )
        pages.append(page)
        continuation = page.authorized_batch.public_tool_result_payload["continuation"]

    assert len(pages) >= 2
    all_candidates = tuple(candidate for page in pages for candidate in _private_candidates(page))
    assert {candidate.resolved_source.source_ref.canonical_object_id for candidate in all_candidates} == set(
        source_conversations
    )
    target = next(
        candidate
        for candidate in all_candidates
        if candidate.resolved_source.source_ref.canonical_object_id == source_conversations[0]
    )
    assert target_marker in target.passages[0].excerpt
    assert first.attests_origin(request, _SNAPSHOT)
    assert all(page.attests_origin(request, _SNAPSHOT) for page in pages)
    model_bytes = b"".join(page.authorized_batch.model_visible_canonical_bytes for page in pages).decode(
        "ascii"
    )
    assert boundary_marker not in model_bytes
    assert post_boundary_marker not in model_bytes

    for prepared in pages:
        ledger.admit_model_tool_bytes(
            prepared.run_binding,
            prepared.authorized_batch,
            prepared.authorized_batch.model_visible_canonical_bytes,
        )
    ledger.freeze_for_publication()
    consume_archive_model_batch_ledger_fail_closed(ledger)
