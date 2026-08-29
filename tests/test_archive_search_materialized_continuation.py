from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval.archive_search_authority import (
    ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL,
    create_archive_model_batch_ledger,
)
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus, ArchiveSearchRequest
from friday.retrieval.archive_search_service import (
    _materialized_lane_limit,
    prepare_archive_search_in_transaction,
)
from friday.retrieval.contracts import SearchLane
from friday.storage.models import InboxItem, InboxStatus, RawObject

TENANT = "archive-materialized-tenant"
PRINCIPAL = "archive-materialized-principal"
SNAPSHOT = "archive-materialized-snapshot"


def _actor() -> ActorContext:
    return ActorContext(
        user_id=TENANT,
        preset_key="user",
        source="archive-materialized-test",
        shared_tenant=True,
        person_id=PRINCIPAL,
    )


def _seed_documents(storage: Any, count: int) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(PRINCIPAL)
    for ordinal in range(1, count + 1):
        raw_id = f"raw_{ordinal:016x}"
        body = f"materialized-cursor-needle evidence {ordinal:03d}"
        at = f"2026-07-{ordinal:02d}T10:00:00+00:00"
        storage.store_raw_object(
            RawObject(
                id=raw_id,
                user_id=TENANT,
                source="upload",
                source_ref=f"opaque:{ordinal:03d}",
                raw_content=body,
                content_type="file",
                metadata_json={
                    "filename": f"record-{ordinal:03d}.txt",
                    "media_kind": "document",
                    "mime_type": "text/plain",
                    "uploaded_by": PRINCIPAL,
                },
                content_hash=hashlib.sha256(body.encode()).hexdigest(),
                received_at=at,
                created_at=at,
            )
        )
        storage.store_inbox_item(
            InboxItem(
                id=f"inbox_{ordinal:016x}",
                user_id=TENANT,
                raw_object_id=raw_id,
                status=InboxStatus.CLASSIFIED,
                created_at=at,
                reviewed_at=at,
                reviewed_by=PRINCIPAL,
            )
        )


def _coverage(payload: dict[str, Any], lane: SearchLane) -> dict[str, Any]:
    return next(item for item in payload["coverage"] if item["lane"] == lane.value)


def test_extended_lane_materialization_is_fair_shared_under_the_global_tail_budget() -> None:
    request = ArchiveSearchRequest.create(
        query="bounded mixed corpus materialization",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.MESSAGES),
        limit=20,
    )

    lane_limit = _materialized_lane_limit(request)

    assert lane_limit == 85
    assert lane_limit * 3 <= 1 + ARCHIVE_AUTHORITY_MAX_CONTINUATION_TAIL

    documents_only = ArchiveSearchRequest.create(
        query="bounded document materialization",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=20,
    )
    messages_only = ArchiveSearchRequest.create(
        query="bounded message materialization",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        limit=20,
    )
    unsupported_targets_do_not_dilute = ArchiveSearchRequest.create(
        query="bounded supported materialization",
        corpora=(
            ArchiveSearchCorpus.DOCUMENTS,
            ArchiveSearchCorpus.GENERATED,
            ArchiveSearchCorpus.WEB,
            ArchiveSearchCorpus.EXTERNAL,
        ),
        limit=20,
    )
    fixed_obsidian_budget_is_reserved = ArchiveSearchRequest.create(
        query="bounded fixed and extended materialization",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.OBSIDIAN),
        limit=20,
    )
    assert _materialized_lane_limit(documents_only) == 100
    assert _materialized_lane_limit(messages_only) == 100
    assert _materialized_lane_limit(unsupported_targets_do_not_dilute) == 100
    assert _materialized_lane_limit(fixed_obsidian_budget_is_reserved) == 88


def test_document_rank_twenty_one_is_reachable_by_a_truthful_materialized_cursor(storage: Any) -> None:
    _seed_documents(storage, 21)
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    request = ArchiveSearchRequest.create(
        query="materialized-cursor-needle",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=20,
    )
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator="archive-materialized-continuation",
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
            run_discriminator="archive-materialized-first",
            turn_ledger=ledger,
        )
    first_payload = first.authorized_batch.public_tool_result_payload
    assert first_payload["candidates"]
    first_continuation = first_payload["continuation"]
    assert isinstance(first_continuation, str)
    first_lexical = _coverage(first_payload, SearchLane.LEXICAL)
    assert {"capped", "partial"} <= set(first_lexical["states"])
    assert first_lexical["next_cursor_available"] is True
    first_warnings = first_payload["warnings"]
    assert isinstance(first_warnings, list)
    assert "continuation_unavailable" not in first_warnings

    pages = [first]
    continuation: str | None = first_continuation
    while continuation is not None:
        with storage.transaction() as conn:
            page = prepare_archive_search_in_transaction(
                conn,
                authorization=authorization,
                actor=_actor(),
                tenant_id=TENANT,
                principal_id=PRINCIPAL,
                request=replace(request, continuation=continuation),
                snapshot_discriminator=SNAPSHOT,
                run_discriminator=f"archive-materialized-page-{len(pages) + 1}",
                turn_ledger=ledger,
            )
        pages.append(page)
        next_continuation = page.authorized_batch.public_tool_result_payload["continuation"]
        assert next_continuation is None or isinstance(next_continuation, str)
        continuation = next_continuation

    last_payload = pages[-1].authorized_batch.public_tool_result_payload
    assert len(pages) >= 2
    assert _coverage(last_payload, SearchLane.LEXICAL)["next_cursor_available"] is False
    source_ids = {
        result.candidate.resolved_source.source_ref.canonical_object_id
        for page in pages
        for result in page.authorized_batch._page.results  # noqa: SLF001 - exact parity oracle
    }
    assert source_ids == {f"raw_{ordinal:016x}" for ordinal in range(1, 22)}


def test_mixed_document_and_message_tail_stays_reachable_under_one_global_budget(
    storage: Any,
) -> None:
    _seed_documents(storage, 11)
    conversation_ids: list[str] = []
    for ordinal in range(1, 12):
        conversation = storage.create_conversation(PRINCIPAL, f"mixed source {ordinal:03d}")
        conversation_ids.append(conversation["id"])
        storage.store_message(
            conversation["id"],
            PRINCIPAL,
            "assistant",
            f"materialized-cursor-needle message evidence {ordinal:03d}",
        )
    boundary_conversation = storage.create_conversation(PRINCIPAL, "accepted mixed boundary")
    boundary = storage.store_message(
        boundary_conversation["id"],
        PRINCIPAL,
        "user",
        "current mixed archive request",
    )
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    request = ArchiveSearchRequest.create(
        query="materialized-cursor-needle",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.MESSAGES),
        limit=20,
    )
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator="archive-mixed-materialized-continuation",
    )
    pages = []
    continuation: str | None = None
    while not pages or continuation is not None:
        page_request = request if continuation is None else replace(request, continuation=continuation)
        with storage.transaction() as conn:
            page = prepare_archive_search_in_transaction(
                conn,
                authorization=authorization,
                actor=_actor(),
                tenant_id=TENANT,
                principal_id=PRINCIPAL,
                request=page_request,
                snapshot_discriminator=SNAPSHOT,
                run_discriminator=f"archive-mixed-materialized-page-{len(pages) + 1}",
                turn_ledger=ledger,
                current_conversation_id=boundary_conversation["id"],
                boundary_user_message_id=boundary["id"],
            )
        pages.append(page)
        next_continuation = page.authorized_batch.public_tool_result_payload["continuation"]
        assert next_continuation is None or isinstance(next_continuation, str)
        continuation = next_continuation

    assert len(pages) >= 2
    first_warnings = pages[0].authorized_batch.public_tool_result_payload["warnings"]
    assert isinstance(first_warnings, list)
    assert "continuation_unavailable" not in first_warnings
    source_ids = {
        result.candidate.resolved_source.source_ref.canonical_object_id
        for page in pages
        for result in page.authorized_batch._page.results  # noqa: SLF001 - exact parity oracle
    }
    assert source_ids == {
        *(f"raw_{ordinal:016x}" for ordinal in range(1, 12)),
        *conversation_ids,
    }
