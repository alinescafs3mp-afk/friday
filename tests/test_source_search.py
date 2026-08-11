"""Source-text search, and the verdict it must not overturn.

`raw_objects` holds the original ingested characters; the Knowledge Object holds a
normalised, often summarised version. Measured on the owner's database, **93% of
ingested characters** lived only in the former and no index covered them — an exact
phrase from a PDF was unfindable once review had condensed it.

The complication is the reason this file exists. On that same database the Inbox
breakdown is 65 ignored / 1 classified: nearly all of that unreachable text is
material the owner EXPLICITLY REJECTED. DATA_LIFECYCLE §3 makes "игнорировать" a
verdict, and this project has already shipped three separate paths that resurrected
rejected material. Making raw text searchable without honouring the verdict would
repeat that at the largest scale yet.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from friday.execution_kernel import ExecutionKernel, _source_anchor_context_projection
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval import HybridSearcher, pack_vector
from friday.server import create_app
from friday.storage.models import (
    Entity,
    EntityType,
    InboxItem,
    InboxStatus,
    KnowledgeObject,
    RawObject,
    new_id,
)

PHRASE = "autovacuum_vacuum_scale_factor"


def _ingest(storage, user_id: str, text: str, *, status: InboxStatus | None) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
    )
    storage.store_raw_object(raw)
    if status is not None:
        storage.store_inbox_item(
            InboxItem(id=new_id("inbox"), user_id=user_id, raw_object_id=raw.id, status=status)
        )
    return raw.id


def test_source_text_is_searchable_and_the_verdict_is_obeyed(storage):
    storage.ensure_user("owner")
    pending = _ingest(storage, "owner", f"черновик {PHRASE} на проверке", status=InboxStatus.PENDING)
    classified = _ingest(storage, "owner", f"принято {PHRASE} в работу", status=InboxStatus.CLASSIFIED)
    archived = _ingest(storage, "owner", f"убрано из inbox {PHRASE}", status=InboxStatus.ARCHIVED)
    rejected = _ingest(storage, "owner", f"отвергнуто {PHRASE} совсем", status=InboxStatus.IGNORED)
    orphan = _ingest(storage, "owner", f"без inbox-строки {PHRASE}", status=None)

    found = {item["id"] for item in storage.search_raw_objects("owner", PHRASE, limit=50)}

    # Awaiting a decision, approved, and Inbox-tidied material is reachable.
    assert pending in found
    assert classified in found
    assert archived in found
    assert orphan in found
    # The verdict stands.
    assert rejected not in found, "search resurrected material the reviewer rejected"


@pytest.mark.asyncio
async def test_explicit_agent_source_search_reads_pending_owned_file_but_not_rejected(
    settings,
    storage,
):
    owner = "source-tool-owner"
    neighbour = "source-tool-neighbour"
    storage.ensure_user(owner, preset_key="owner")
    storage.ensure_user(neighbour, preset_key="owner")
    target = "Иванов — ведущий инженер по эксплуатации"
    kept = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-kept",
        raw_content=("служебное вступление\n" * 80) + target,
        content_type="file",
        metadata_json={"filename": "штатное расписание.docx"},
    )
    rejected = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-rejected",
        raw_content=f"отклонённая копия: {target}",
        content_type="file",
        metadata_json={"filename": "отклонено.docx"},
    )
    foreign = RawObject(
        id=new_id("raw"),
        user_id=neighbour,
        source="upload",
        source_ref="opaque-foreign",
        raw_content=f"чужой материал: {target}",
        content_type="file",
        metadata_json={"filename": "чужое.docx"},
    )
    for raw in (kept, rejected, foreign):
        storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=kept.id, status=InboxStatus.PENDING)
    )
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=owner,
            raw_object_id=rejected.id,
            status=InboxStatus.IGNORED,
        )
    )
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=neighbour,
            raw_object_id=foreign.id,
            status=InboxStatus.PENDING,
        )
    )

    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    result = await kernel.execute(
        "source_search",
        {"query": "Иванов должность", "limit": 20},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True
    assert result.data["shown"] == 1
    assert result.data["coverage"] == {
        "complete": True,
        "limit": 20,
        "candidates_scanned": 1,
        "candidate_cap": 100,
        "focus_conjunctive": False,
        "focus_match_found": False,
        "focus_fallback_contextual": False,
        "ignored_excluded": True,
    }
    [item] = result.data["results"]
    assert item["raw_object_id"] == kept.id
    assert item["title"] == "штатное расписание.docx"
    assert item["review_status"] == "pending"
    assert item["promoted"] is False
    assert target in item["excerpt"]
    assert rejected.id not in str(result.data)
    assert foreign.id not in str(result.data)


@pytest.mark.asyncio
async def test_source_search_adopts_dense_reranked_passage_from_canonical_raw(settings, storage):
    owner = "source-semantic-owner"
    storage.ensure_user(owner, preset_key="owner")
    source_text = (
        ("Служебное описание общего порядка работы подразделения.\n" * 80)
        + "Основной пункт управления размещён в здании на площади Победы.\n"
        + ("Дополнительный порядок связи приведён в отдельной ведомости.\n" * 80)
    )
    raw = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="semantic-source",
        raw_content=source_text,
        content_type="file",
        metadata_json={"filename": "дислокация.docx", "uploaded_by": owner},
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=owner,
        raw_object_id=raw.id,
        content=source_text,
        content_type="file",
        title="Дислокация подразделения",
    )
    storage.store_knowledge_object(knowledge)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=owner,
            raw_object_id=raw.id,
            knowledge_object_id=knowledge.id,
            status=InboxStatus.CLASSIFIED,
        )
    )
    # Raw FTS is intentionally OR-based.  This lone literal decoy matches one
    # generic query word, but it must not suppress the differently worded dense
    # target merely because the lexical page happens to contain exactly one row.
    decoy = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="semantic-literal-decoy",
        raw_content="Оперативный справочник по хозяйственному обеспечению.",
        content_type="file",
        metadata_json={"filename": "справочник.docx", "uploaded_by": owner},
    )
    storage.store_raw_object(decoy)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id=owner,
            raw_object_id=decoy.id,
            status=InboxStatus.PENDING,
        )
    )
    passage_lo = source_text.index("Основной пункт")
    passage_hi = passage_lo + len("Основной пункт управления размещён в здании на площади Победы.")
    tuned = dataclasses.replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9/v1",
        embeddings_model="source-semantic-test",
        embeddings_resident_cache=False,
        embeddings_chunk_chars=1200,
        embeddings_dense_max_objects=100,
        embeddings_recall_candidates=20,
    )
    storage.upsert_knowledge_vectors(
        [
            {
                "knowledge_object_id": knowledge.id,
                "user_id": owner,
                "model": tuned.embeddings_model,
                "dim": 2,
                "source_version": knowledge.version,
                "content_hash": "whole",
                "chunk_scheme": "source-semantic-v1",
                "vector": pack_vector([1.0, 0.0]),
            }
        ],
        {
            knowledge.id: [
                {
                    "chunk_index": 0,
                    "user_id": owner,
                    "model": tuned.embeddings_model,
                    "dim": 2,
                    "source_version": knowledge.version,
                    "chunk_scheme": "source-semantic-v1",
                    "start_char": passage_lo,
                    "end_char": passage_hi,
                    "content_hash": "passage",
                    "vector": pack_vector([1.0, 0.0]),
                }
            ]
        },
    )

    class SemanticEmbeddings:
        def __init__(self):
            self.settings = tuned
            self.remote_enabled = True
            self.calls = []

        async def embed(self, texts, *, budget_sec=None):
            self.calls.append((list(texts), budget_sec))
            return [[1.0, 0.0] for _text in texts]

    embeddings = SemanticEmbeddings()
    rerank_calls = []

    async def reranker(query, items):
        rerank_calls.append((query, [str(item["id"]) for item in items]))
        return [{**item, "_rerank_score": 0.97} for item in items]

    searcher = HybridSearcher(
        storage,
        embeddings,  # type: ignore[arg-type]
        reranker=reranker,
        rerank_top=10,
        rerank_confident_min=0.1,
    )
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, tuned)
    kernel.bind_services(storage, None, None, None, searcher=searcher)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": "где находится оперативный центр", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True, result.error
    assert embeddings.calls and embeddings.calls[0][0] == ["где находится оперативный центр"]
    assert rerank_calls == [("где находится оперативный центр", [knowledge.id])]
    item = result.data["results"][0]
    assert item["raw_object_id"] == raw.id
    assert item["retrieval_match_kind"] == "semantic"
    assert "пункт управления" in item["excerpt"]
    assert [row["raw_object_id"] for row in result.data["results"]] == [raw.id, decoy.id]
    assert result.data["coverage"]["semantic_recall"] is True
    assert result.data["coverage"]["semantic_reranked"] is True
    assert result.data["coverage"]["complete"] is False


@pytest.mark.asyncio
async def test_focused_semantic_query_keeps_anchor_and_rejects_another_section(
    settings,
    storage,
    monkeypatch,
):
    owner = "source-focused-semantic-owner"
    storage.ensure_user(owner, preset_key="owner")
    fixtures = []
    for label, content in (
        ("target", "Подразделение РЭБ | командир взвода | капитан Орлов"),
        (
            "other",
            "Радиоэлектронное противодействие | начальник группы | капитан Соколов",
        ),
    ):
        raw = RawObject(
            id=new_id("raw"),
            user_id=owner,
            source="upload",
            source_ref=f"focused-{label}",
            raw_content=content,
            content_type="file",
            metadata_json={"filename": f"{label}.xlsx", "uploaded_by": owner},
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id=owner,
            raw_object_id=raw.id,
            content=content,
            content_type="file",
            title=label,
        )
        storage.store_knowledge_object(knowledge)
        storage.store_inbox_item(
            InboxItem(
                id=new_id("inbox"),
                user_id=owner,
                raw_object_id=raw.id,
                knowledge_object_id=knowledge.id,
                status=InboxStatus.CLASSIFIED,
            )
        )
        fixtures.append((label, raw, knowledge))

    class FocusSearcher:
        async def search(self, user_id, query, **kwargs):
            assert user_id == owner
            assert query == "РЭБ командир взвода"
            assert kwargs["record_usage"] is False
            # Put a dense-only related section first.  It contains neither the
            # literal anchor nor the requested focus; semantic similarity alone
            # must not turn it into focused evidence.
            ordered = [fixtures[1], fixtures[0]]
            return {
                "results": [
                    {
                        **storage.get_knowledge_object(knowledge.id, owner),
                        "_embedding_score": 0.98 - index * 0.01,
                        "_rerank_score": 0.97 - index * 0.01,
                    }
                    for index, (_label, _raw, knowledge) in enumerate(ordered)
                ],
                "matched_at_least": 2,
                "strategy": {"embeddings": True, "reranked": 2},
            }

    # Exercise the semantic-only envelope directly.  The target proves its anchor
    # and focus from canonical Raw bytes; the related row proves neither.
    monkeypatch.setattr(storage, "search_raw_objects", lambda *_args, **_kwargs: [])
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None, searcher=FocusSearcher())  # type: ignore[arg-type]
    result = await kernel.execute(
        "source_search",
        {"query": "РЭБ", "focus": "командир взвода", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True, result.error
    assert [item["raw_object_id"] for item in result.data["results"]] == [fixtures[0][1].id]
    assert fixtures[1][1].id not in str(result.data)
    [item] = result.data["results"]
    assert item["retrieval_match_kind"] == "semantic"
    assert item["focus_match_kind"] == "full"
    assert item["focus_terms_matched"] == item["focus_terms_total"] == 2
    assert item["anchor_context_terms"] > 0
    assert result.data["coverage"]["focus_match_found"] is True
    assert result.data["coverage"]["complete"] is False


@pytest.mark.asyncio
async def test_semantic_source_candidates_are_reauthorized_by_uploader_and_verdict(settings, storage):
    tenant = "source-shared-tenant"
    person = "source-shared-person"
    foreign_person = "source-shared-foreign"
    for user_id in (tenant, person, foreign_person):
        storage.ensure_user(user_id, preset_key="owner")

    fixtures = []
    for label, uploaded_by, status in (
        ("owned", person, InboxStatus.CLASSIFIED),
        ("foreign", foreign_person, InboxStatus.CLASSIFIED),
        ("ignored", person, InboxStatus.IGNORED),
        ("rerank-only", person, InboxStatus.CLASSIFIED),
    ):
        source_text = f"{label}: закрытый смысловой фрагмент о резервном узле управления"
        raw = RawObject(
            id=new_id("raw"),
            user_id=tenant,
            source="upload",
            source_ref=f"semantic-{label}",
            raw_content=source_text,
            content_type="file",
            metadata_json={"filename": f"{label}.docx", "uploaded_by": uploaded_by},
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id=tenant,
            raw_object_id=raw.id,
            content=source_text,
            content_type="file",
            title=label,
        )
        storage.store_knowledge_object(knowledge)
        storage.store_inbox_item(
            InboxItem(
                id=new_id("inbox"),
                user_id=tenant,
                raw_object_id=raw.id,
                knowledge_object_id=knowledge.id,
                status=status,
            )
        )
        fixtures.append((label, raw, knowledge))

    class HostileSearcher:
        async def search(self, user_id, query, **kwargs):
            assert user_id == tenant
            assert kwargs["uploaded_by"] == person
            results = []
            for index, (label, _raw, knowledge) in enumerate(fixtures):
                item = {
                    **storage.get_knowledge_object(knowledge.id, tenant),
                    "_rerank_score": 0.98 - index * 0.01,
                }
                if label != "rerank-only":
                    item["_embedding_score"] = 0.99 - index * 0.01
                results.append(item)
            return {
                "results": results,
                "matched_at_least": 4,
                "strategy": {"embeddings": True, "reranked": 4},
            }

    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None, searcher=HostileSearcher())  # type: ignore[arg-type]
    actor = ActorContext(
        user_id=tenant,
        person_id=person,
        preset_key="owner",
        source="test",
        shared_tenant=True,
    )

    result = await kernel.execute(
        "source_search",
        {"query": "аварийная командная площадка", "limit": 10},
        actor=actor,
    )

    assert result.success is True, result.error
    assert [item["raw_object_id"] for item in result.data["results"]] == [fixtures[0][1].id]
    assert fixtures[1][1].id not in str(result.data)
    assert fixtures[2][1].id not in str(result.data)
    assert fixtures[3][1].id not in str(result.data)
    assert result.data["coverage"]["uploader_scoped"] is True
    assert result.data["coverage"]["ignored_excluded"] is True


@pytest.mark.asyncio
async def test_one_literal_source_hit_does_not_pay_for_or_yield_to_semantic_fallback(settings, storage):
    owner = "source-literal-first-owner"
    storage.ensure_user(owner, preset_key="owner")
    raw_id = _ingest(
        storage,
        owner,
        "UNIQUE-LITERAL-SOURCE-MARKER точный ответ",
        status=InboxStatus.PENDING,
    )

    class ForbiddenSemanticSearcher:
        async def search(self, *_args, **_kwargs):
            raise AssertionError("an unambiguous literal source lookup invoked semantic fallback")

    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(
        storage,
        None,
        None,
        None,
        searcher=ForbiddenSemanticSearcher(),
    )  # type: ignore[arg-type]
    result = await kernel.execute(
        "source_search",
        {"query": "UNIQUE-LITERAL-SOURCE-MARKER", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True, result.error
    assert [item["raw_object_id"] for item in result.data["results"]] == [raw_id]
    assert "semantic_recall" not in result.data["coverage"]


@pytest.mark.asyncio
async def test_multiple_literal_source_hits_invoke_semantic_fallback(settings, storage):
    owner = "source-literal-ambiguous-owner"
    storage.ensure_user(owner, preset_key="owner")
    first = _ingest(
        storage,
        owner,
        "AMBIGUOUS-LITERAL-MARKER первая версия ответа",
        status=InboxStatus.PENDING,
    )
    second = _ingest(
        storage,
        owner,
        "AMBIGUOUS-LITERAL-MARKER вторая версия ответа",
        status=InboxStatus.PENDING,
    )

    class ObservedSemanticSearcher:
        def __init__(self):
            self.calls = []

        async def search(self, user_id, query, **kwargs):
            self.calls.append((user_id, query, kwargs))
            return {
                "results": [],
                "matched_at_least": 0,
                "strategy": {"embeddings": True, "reranked": False},
            }

    searcher = ObservedSemanticSearcher()
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None, searcher=searcher)  # type: ignore[arg-type]
    result = await kernel.execute(
        "source_search",
        {"query": "AMBIGUOUS-LITERAL-MARKER", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True, result.error
    assert {item["raw_object_id"] for item in result.data["results"]} == {first, second}
    assert len(searcher.calls) == 1
    assert searcher.calls[0][0:2] == (owner, "AMBIGUOUS-LITERAL-MARKER")
    assert searcher.calls[0][2]["record_usage"] is False
    assert result.data["coverage"]["semantic_recall"] is False


def test_searchable_source_reads_scope_before_limit_and_reject_ignored(storage):
    tenant = "source-storage-scope"
    person = "source-storage-person"
    foreign = "source-storage-foreign"
    for user_id in (tenant, person, foreign):
        storage.ensure_user(user_id)

    own_id = _ingest(storage, tenant, "OWN-SCOPE-MARKER", status=InboxStatus.PENDING)
    ignored_id = _ingest(storage, tenant, "OWN-SCOPE-MARKER ignored", status=InboxStatus.IGNORED)
    foreign_id = _ingest(storage, tenant, "OWN-SCOPE-MARKER foreign", status=InboxStatus.PENDING)
    for raw_id, uploader in ((own_id, person), (ignored_id, person), (foreign_id, foreign)):
        metadata = {"filename": f"{raw_id}.txt", "uploaded_by": uploader}
        storage.execute(
            "UPDATE raw_objects SET content_type='file', metadata_json=? WHERE id=?",
            (json.dumps(metadata), raw_id),
        )
    storage.execute(
        "UPDATE raw_objects SET received_at='2020-01-01T00:00:00+00:00' WHERE id=?",
        (own_id,),
    )
    storage.commit()

    fts = storage.search_raw_objects(
        tenant,
        "OWN-SCOPE-MARKER",
        limit=1,
        include_content=True,
        uploaded_by=person,
    )
    assert [row["id"] for row in fts] == [own_id]
    adopted = storage.get_searchable_file_sources(
        tenant,
        [foreign_id, ignored_id, own_id],
        uploaded_by=person,
        include_content=True,
    )
    assert [row["id"] for row in adopted] == [own_id]
    assert adopted[0]["_raw_content"] == "OWN-SCOPE-MARKER"


@pytest.mark.asyncio
async def test_source_search_projects_closed_evidence_authority(settings, storage):
    owner = "source-authority-owner"
    storage.ensure_user(owner, preset_key="owner")
    marker = "SOURCE-AUTHORITY-MARKER"
    cases = {
        "native.txt": (
            {"text_extraction_success": True},
            {"verification_eligible": True, "basis": "extracted_text"},
        ),
        "review-scan.jpg": (
            {"text_extraction_success": True, "vision_review_required": True},
            {"verification_eligible": False, "basis": "advisory_visual"},
        ),
        "advisory-visual.png": (
            {"vision_used": True, "advisory_only": True},
            {"verification_eligible": False, "basis": "advisory_visual"},
        ),
        "transcript.txt": (
            {"text_extraction_success": True, "transcription": {"model": "local"}},
            {"verification_eligible": False, "basis": "advisory_transcript"},
        ),
    }
    expected_by_id = {}
    for filename, (metadata, authority) in cases.items():
        raw = RawObject(
            id=new_id("raw"),
            user_id=owner,
            source="upload",
            source_ref=new_id("src"),
            raw_content=f"{marker} {filename}",
            content_type="file",
            metadata_json={"filename": filename, **metadata},
        )
        storage.store_raw_object(raw)
        storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=raw.id))
        expected_by_id[raw.id] = authority

    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    result = await kernel.execute(
        "source_search",
        {"query": marker, "limit": 20},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True, result.error
    assert {
        row["raw_object_id"]: row["evidence_authority"] for row in result.data["results"]
    } == expected_by_id
    assert "vision_review_required" not in str(result.data)
    assert "transcription" not in str(result.data)


@pytest.mark.asyncio
async def test_source_search_requires_knowledge_read(settings, storage):
    storage.ensure_user("source-guest", preset_key="guest")
    authorization = AuthorizationService(storage)
    authorization.deny_permission("source-guest", "knowledge.read")
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": PHRASE},
        actor=authorization.actor_for_user("source-guest", source="test"),
    )

    assert result.success is False


def test_source_search_is_detailed_for_file_work_and_withheld_from_small_talk(settings, storage):
    storage.ensure_user("source-routing-owner", preset_key="owner")
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    actor = authorization.actor_for_user("source-routing-owner", source="test")

    file_tools = {
        str((item.get("function") or {}).get("name") or "")
        for item in kernel.get_tool_definitions(actor, topic="файл")
    }
    household_tools = {
        str((item.get("function") or {}).get("name") or "")
        for item in kernel.get_tool_definitions(actor, topic="быт")
    }

    assert "source_search" in file_tools
    assert "source_search" not in household_tools


@pytest.mark.asyncio
async def test_source_search_page_reaches_the_model_without_tail_truncation(settings, storage):
    owner = "source-page-owner"
    storage.ensure_user(owner, preset_key="owner")
    for index in range(20):
        raw = RawObject(
            id=new_id("raw"),
            user_id=owner,
            source="upload",
            source_ref=f"source-{index}",
            raw_content=(f"PAGE-SOURCE-{index:02d} {PHRASE} " + "длинное окружение " * 120),
            content_type="file",
            metadata_json={"filename": f"Материал {index:02d}.docx"},
        )
        storage.store_raw_object(raw)
        storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=raw.id))
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": PHRASE, "limit": 20},
        actor=authorization.actor_for_user(owner, source="test"),
    )
    rendered = result.to_llm_message()

    assert result.success is True
    assert result.data["shown"] == 20
    assert result.data["coverage"]["complete"] is False
    assert len(rendered) < 12_000
    assert "Материал 00.docx" in rendered
    assert "Материал 19.docx" in rendered
    assert result.truncated is False


@pytest.mark.asyncio
async def test_source_search_uses_a_separate_focus_without_broadening_retrieval(settings, storage):
    owner = "source-focus-owner"
    storage.ensure_user(owner, preset_key="owner")
    target = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-focus-target",
        raw_content=("Иванов\n" * 1_000) + "Иванов — ведущий инженер по эксплуатации\n",
        content_type="file",
        metadata_json={"filename": "synthetic-focus-target.docx"},
    )
    predicate_noise = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-focus-noise",
        raw_content="Должность: посторонний предикат без искомой фамилии",
        content_type="file",
        metadata_json={"filename": "synthetic-focus-noise.docx"},
    )
    anchor_noise = [
        RawObject(
            id=new_id("raw"),
            user_id=owner,
            source="upload",
            source_ref=f"opaque-anchor-noise-{index}",
            raw_content="Иванов\n" * 200,
            content_type="file",
            metadata_json={"filename": f"synthetic-anchor-noise-{index:02d}.docx"},
        )
        for index in range(30)
    ]
    for raw in (target, predicate_noise, *anchor_noise):
        storage.store_raw_object(raw)
        storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=raw.id))
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": "Иванов", "focus": "Иванов должность", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True
    assert result.data["query"] == "Иванов"
    assert result.data["focus"] == "Иванов должность"
    assert result.data["shown"] == 1
    assert result.data["coverage"]["focus_match_found"] is False
    assert result.data["coverage"]["focus_fallback_contextual"] is True
    [item] = result.data["results"]
    assert item["raw_object_id"] == target.id
    assert "Иванов — ведущий инженер по эксплуатации" in item["excerpt"]
    assert item["focus_match_kind"] == "anchor_context"
    assert predicate_noise.id not in str(result.data)
    assert all(noise.id not in str(result.data) for noise in anchor_noise)


@pytest.mark.asyncio
async def test_source_search_binds_a_single_cell_section_heading_to_its_first_record(
    settings,
    storage,
):
    owner = "source-table-section-owner"
    storage.ensure_user(owner, preset_key="owner")
    target = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-orion-section",
        raw_content=(
            "ORION platoon |  |  | \n"
            "ALPHA person | Commander platoon | Senior | 41\n"
            "BRAVO person | Operator | Junior | 42"
        ),
        content_type="file",
        metadata_json={"filename": "synthetic-orion-staff.xlsx"},
    )
    storage.store_raw_object(target)
    storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=target.id))
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {
            "query": "ORION",
            "focus": "ORION commander platoon",
            "limit": 10,
        },
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True
    assert result.data["shown"] == 1
    [item] = result.data["results"]
    assert item["raw_object_id"] == target.id
    assert item["focus_match_kind"] == "full"
    assert "ORION platoon" in item["excerpt"]
    assert "ALPHA person" in item["excerpt"]
    assert "Commander platoon" in item["excerpt"]
    assert "BRAVO person" not in item["excerpt"]


@pytest.mark.asyncio
async def test_source_search_never_cross_joins_a_far_predicate_in_the_same_document(settings, storage):
    owner = "source-same-window-owner"
    storage.ensure_user(owner, preset_key="owner")
    source = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-same-window",
        raw_content=("Иванов\n" * 1_000)
        + ("нейтральный раздел без кадровых сведений\n" * 100)
        + "Петров\nДолжность: генеральный директор\n",
        content_type="file",
        metadata_json={"filename": "synthetic-same-window.docx"},
    )
    storage.store_raw_object(source)
    storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=source.id))
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": "Иванов", "focus": "Иванов должность", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True
    assert result.data["shown"] == 0
    assert result.data["results"] == []
    assert "Петров" not in str(result.data)
    assert "генеральный директор" not in str(result.data)
    assert result.data["coverage"]["focus_match_found"] is False


@pytest.mark.asyncio
async def test_source_search_context_boilerplate_cannot_page_out_an_implicit_value(settings, storage):
    owner = "source-context-rank-owner"
    storage.ensure_user(owner, preset_key="owner")
    target = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="opaque-context-target",
        raw_content="Иванов — ведущий инженер по эксплуатации",
        content_type="file",
        metadata_json={"filename": "target.docx"},
    )
    storage.store_raw_object(target)
    storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=target.id))
    for index in range(30):
        noise = RawObject(
            id=new_id("raw"),
            user_id=owner,
            source="upload",
            source_ref=f"opaque-context-noise-{index}",
            raw_content=(
                "Список сотрудников организации: Иванов. "
                "Дополнительные сведения об обязанностях отсутствуют полностью."
            ),
            content_type="file",
            metadata_json={"filename": f"noise-{index:02d}.docx"},
        )
        storage.store_raw_object(noise)
        storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=noise.id))
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": "Иванов", "focus": "Иванов должность", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True
    assert any(item["raw_object_id"] == target.id for item in result.data["results"])
    assert "ведущий инженер по эксплуатации" in str(result.data)


@pytest.mark.asyncio
async def test_source_search_focus_first_reaches_target_beyond_anchor_candidate_cap(settings, storage):
    owner = "source-focus-cap-owner"
    storage.ensure_user(owner, preset_key="owner")
    target = RawObject(
        id=new_id("raw"),
        user_id=owner,
        source="upload",
        source_ref="focus-cap-target",
        raw_content="Иванов\nДолжность: ведущий инженер",
        content_type="file",
        metadata_json={"filename": "focused-target.docx", "text_extraction_success": True},
    )
    storage.store_raw_object(target)
    storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=target.id))
    storage.execute(
        "UPDATE raw_objects SET received_at=? WHERE id=?",
        ("2020-01-01T00:00:00+00:00", target.id),
    )
    for index in range(100):
        noise = RawObject(
            id=new_id("raw"),
            user_id=owner,
            source="upload",
            source_ref=f"focus-cap-noise-{index:03d}",
            raw_content="Иванов",
            content_type="file",
            metadata_json={"filename": f"anchor-only-{index:03d}.txt"},
        )
        storage.store_raw_object(noise)
        storage.store_inbox_item(InboxItem(id=new_id("inbox"), user_id=owner, raw_object_id=noise.id))
    storage.commit()

    anchor_page = storage.search_raw_objects(owner, "Иванов", limit=100, include_content=True)
    assert len(anchor_page) == 100
    assert target.id not in {row["id"] for row in anchor_page}, "fixture did not place target at 101+"

    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]
    result = await kernel.execute(
        "source_search",
        {"query": "Иванов", "focus": "Иванов должность", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )

    assert result.success is True, result.error
    assert result.data["shown"] == 1
    assert result.data["results"][0]["raw_object_id"] == target.id
    assert result.data["results"][0]["focus_match_kind"] == "full"
    assert result.data["coverage"]["focus_match_found"] is True
    assert result.data["coverage"]["complete"] is False


@pytest.mark.asyncio
async def test_source_search_maximum_metadata_page_remains_valid_untruncated_json(
    settings,
    storage,
    monkeypatch,
):
    owner = "source-envelope-owner"
    storage.ensure_user(owner, preset_key="owner")
    rows = [
        {
            "id": f"raw-{index:02d}-" + ("r" * 70),
            "content_type": "application/synthetic-" + ("x" * 58),
            "received_at": "2026-08-10T00:00:00.000000+00:00-extra",
            "inbox_status": "pending-review-state-" + ("s" * 19),
            "knowledge_object_id": None,
            "_raw_metadata": {"filename": f"Материал-{index:02d}-" + ("т" * 248)},
            "_raw_content": ("Иванов " + ("контекст " * 42) + f"Должность: ведущий инженер {index:02d}"),
        }
        for index in range(10)
    ]

    calls = []

    def fake_search_raw_objects(user_id, query, *, limit, include_content):
        assert user_id == owner
        assert query in {"Иванов", "Иванов должность"}
        assert limit == 100
        assert include_content is True
        calls.append(query)
        return rows

    monkeypatch.setattr(storage, "search_raw_objects", fake_search_raw_objects)
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, None, None, None)  # type: ignore[arg-type]

    result = await kernel.execute(
        "source_search",
        {"query": "Иванов", "focus": "Иванов должность", "limit": 10},
        actor=authorization.actor_for_user(owner, source="test"),
    )
    rendered = result.to_llm_message()

    assert result.success is True
    assert result.truncated is False
    assert len(rendered.removeprefix("Результат source_search:\n")) < 12_000
    parsed = json.loads(rendered.removeprefix("Результат source_search:\n"))
    assert parsed["shown"] == 10
    assert len(parsed["results"]) == 10
    assert "Должность: ведущий инженер 09" in parsed["results"][-1]["excerpt"]
    assert parsed["results"][-1]["focus_match_kind"] == "full"
    assert calls == ["Иванов должность", "Иванов"]


@pytest.mark.parametrize(
    ("source_name", "query", "expected"),
    [
        ("Иванов", "иванов", True),
        ("Иванова", "иванов", True),
        ("Иванову", "иванов", True),
        ("Ивановым", "иванов", True),
        ("Ивановский", "иванов", False),
        ("Иванович", "иванов", False),
        ("Петровский", "петровск", True),
        ("Петровского", "петровск", True),
        ("Петровскому", "петровск", True),
        ("Петровским", "петровск", True),
    ],
)
def test_source_anchor_uses_closed_surname_forms(source_name, query, expected):
    excerpt, matched, context = _source_anchor_context_projection(
        query,
        f"{query} должност",
        f"{source_name}\nДолжность: ведущий инженер",
        max_chars=600,
    )

    assert bool(excerpt) is expected
    if expected:
        assert source_name in excerpt
        assert matched == 2
        assert context >= 2


@pytest.mark.parametrize(
    ("focus", "text"),
    [
        ("иванов рол", "Иванов\nПароль: PRIVATE-VALUE"),
        ("иванов рол", "Иванов\nКонтроль: PRIVATE-VALUE"),
        ("иванов позици", "Иванов\nПозиционирование продукта"),
        ("иванов должност", "Иванов\nДолжностная инструкция"),
    ],
)
def test_source_focus_does_not_match_unrelated_token_substrings(focus, text):
    excerpt, matched, _context = _source_anchor_context_projection(
        "иванов",
        focus,
        text,
        max_chars=600,
    )

    assert "Иванов" in excerpt
    assert matched == 1


def test_source_projection_preserves_original_offsets_and_table_record_boundaries():
    unicode_text = (
        ("ﬁ" * 500) + ("before " * 30) + "\nИванов\nДолжность: ведущий инженер\n" + ("after " * 1_000)
    )
    excerpt, matched, context = _source_anchor_context_projection(
        "иванов",
        "иванов должност",
        unicode_text,
        max_chars=600,
    )
    assert "Иванов\nДолжность: ведущий инженер" in excerpt
    assert matched == 2
    assert context >= 2

    table = "\n".join(
        ["Фамилия | Примечание | Должность"]
        + [f"Петров-{index:02d} | заметка | генеральный директор" for index in range(20)]
        + ["Иванов | " + ("длинное примечание " * 80) + " | Должность: ведущий инженер"]
        + [f"Сидоров-{index:02d} | заметка | начальник отдела" for index in range(20)]
    )
    excerpt, matched, context = _source_anchor_context_projection(
        "иванов",
        "иванов должност",
        table,
        max_chars=480,
    )
    assert excerpt == "Фамилия | Должность\nИванов | Должность: ведущий инженер"
    assert matched == 2
    assert context >= 2
    assert "Петров" not in excerpt
    assert "Сидоров" not in excerpt

    for implicit_table in (
        "Иванов | ведущий инженер",
        "ФИО | Штатная единица\nИванов | ведущий инженер",
    ):
        excerpt, matched, context = _source_anchor_context_projection(
            "иванов",
            "иванов должност",
            implicit_table,
            max_chars=480,
        )
        assert "Иванов" in excerpt
        assert "ведущий инженер" in excerpt
        assert matched == 1
        assert context >= 2


def test_source_projection_accepts_a_safe_preceding_field_but_not_a_neighbour_record():
    excerpt, matched, context = _source_anchor_context_projection(
        "иванов",
        "иванов должност",
        "Должность: ведущий инженер\nИванов",
        max_chars=600,
    )
    assert excerpt == "Должность: ведущий инженер\nИванов"
    assert matched == 2
    assert context >= 2

    hostile, hostile_matched, hostile_context = _source_anchor_context_projection(
        "иванов",
        "иванов должност",
        "Петров\nДолжность: генеральный директор\nИванов",
        max_chars=600,
    )
    assert hostile == "Иванов"
    assert hostile_matched == 1
    assert hostile_context == 0


def test_source_projection_rejects_a_field_label_without_a_value():
    excerpt, matched, context = _source_anchor_context_projection(
        "иванов",
        "иванов должност",
        "Иванов\nДолжность:",
        max_chars=600,
    )
    assert excerpt == "Иванов\nДолжность:"
    assert matched == 2
    assert context == 0


def test_a_soft_deleted_source_is_not_reachable(storage):
    storage.ensure_user("owner")
    raw_id = _ingest(storage, "owner", f"будет удалено {PHRASE}", status=InboxStatus.PENDING)
    assert any(item["id"] == raw_id for item in storage.search_raw_objects("owner", PHRASE))

    # No public soft-delete for a Raw Object (purge removes it outright), so mark
    # it the way the column is meant to be used and check the query honours it.
    with storage.transaction() as conn:
        conn.execute("UPDATE raw_objects SET deleted_at=? WHERE id=?", ("2026-07-27T00:00:00Z", raw_id))
    assert not any(item["id"] == raw_id for item in storage.search_raw_objects("owner", PHRASE))


def test_source_search_is_tenant_scoped(storage):
    storage.ensure_user("alice")
    storage.ensure_user("bob")
    _ingest(storage, "alice", f"личное {PHRASE} алисы", status=InboxStatus.PENDING)
    assert storage.search_raw_objects("alice", PHRASE)
    assert storage.search_raw_objects("bob", PHRASE) == []


def test_a_foreign_inbox_row_cannot_hide_or_relabel_an_owned_source(storage):
    """Every correlated child row must prove the same tenant as its Raw parent."""

    storage.ensure_user("owner")
    storage.ensure_user("foreign")
    raw_id = _ingest(storage, "owner", f"tenant correlation {PHRASE}", status=None)
    foreign_rows = [
        InboxItem(
            id=new_id("inbox"),
            user_id="foreign",
            raw_object_id=raw_id,
            status=status,
            created_at=created_at,
        ).to_row()
        for status, created_at in (
            (InboxStatus.IGNORED, "2026-08-08T00:00:00+00:00"),
            (InboxStatus.CLASSIFIED, "2026-08-08T00:00:01+00:00"),
        )
    ]
    with storage.transaction() as conn:
        conn.executemany(
            """INSERT INTO inbox(id, user_id, raw_object_id, knowledge_object_id, status,
                   suggested_entity_id, suggested_tags_json, suggestions_json, suggested_action,
                   promotion_score, quality_score, classification_notes, created_at,
                   reviewed_at, reviewed_by)
               VALUES(:id, :user_id, :raw_object_id, :knowledge_object_id, :status,
                   :suggested_entity_id, :suggested_tags_json, :suggestions_json, :suggested_action,
                   :promotion_score, :quality_score, :classification_notes, :created_at,
                   :reviewed_at, :reviewed_by)""",
            foreign_rows,
        )

    found = storage.search_raw_objects("owner", PHRASE)
    owned = next(item for item in found if item["id"] == raw_id)
    assert owned["inbox_status"] is None
    for row in foreign_rows:
        assert storage.get_inbox_item(row["id"], "foreign") is None
    assert storage.get_inbox_by_raw(raw_id, "foreign") is None
    assert storage.find_inbox_by_raw(raw_id, "foreign") is None
    assert storage.count_inbox("foreign") == 0
    assert storage.list_inbox("foreign") == []
    assert storage.list_inbox_detailed("foreign") == []
    assert storage.group_pending_inbox("foreign")["items_total"] == 0


def test_raw_replay_keys_cannot_reopen_a_quarantined_source(storage):
    """Source-ref/hash/text-hash replay readers share the full raw dependency guard."""

    user_id = "alice"
    sentinel = "PRIVATE RAW REPLAY SENTINEL"
    content_hash = "a" * 64
    text_hash = "b" * 64
    storage.ensure_user(user_id)
    raw = RawObject(
        id="raw-private-replay",
        user_id=user_id,
        source="agent_tool",
        source_ref="private-replay-ref",
        raw_content=sentinel,
        content_type="file",
        content_hash=content_hash,
        metadata_json={
            "text_sha256": text_hash,
            "candidate_type": "memory_save",
            "requested_by": "alice",
        },
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id="inbox-private-replay",
            user_id=user_id,
            raw_object_id=raw.id,
            status=InboxStatus.PENDING,
        )
    )
    knowledge = KnowledgeObject(
        id="ko-private-replay",
        user_id=user_id,
        raw_object_id=raw.id,
        content=sentinel,
        content_type="text",
        title=sentinel,
    )
    storage.store_knowledge_object(knowledge)
    hidden = Entity(
        id="ent-private-replay",
        user_id=user_id,
        name=sentinel,
        entity_type=EntityType.EVENT,
    )
    storage.create_entity(hidden)
    storage.link_knowledge_entity(user_id, knowledge.id, hidden.id, status="accepted")
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (hidden.id, "bob", "2026-08-05T00:00:00Z"),
        )

    assert storage.get_raw_object(raw.id, user_id) is None
    assert storage.find_raw_by_source_ref(user_id, raw.source, raw.source_ref) is None
    assert storage.find_file_by_content_hash(user_id, content_hash) is None
    assert storage.find_file_by_extracted_text(user_id, text_hash) is None
    assert (
        storage.find_fresh_agent_candidate(
            user_id,
            raw.source,
            "memory_save",
            content_hash,
            requested_by="alice",
            since="2000-01-01T00:00:00Z",
        )
        is None
    )


def test_the_index_is_only_ever_read_through_filtered_storage_helpers():
    """Every FTS reader owns the full lifecycle/privacy predicate before LIMIT.

    `raw_fts` holds terms derived from EVERY raw object, rejected ones included — a
    deliberate choice, so that returning an ignored item to pending makes it
    reachable again without an index rebuild. The price is that a second query
    against `raw_fts` without the verdict filter would expose rejected material, so
    there must not be one.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).parent.parent / "friday"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "raw_fts" not in source:
            continue
        # The schema declares it; storage/_intake.py is the one reader.
        if path.name in {"_base.py", "_core.py", "_intake.py"}:
            continue
        offenders.append(str(path.relative_to(root)))
    assert not offenders, f"raw_fts is queried outside the filtered helper: {offenders}"

    intake = (root / "storage" / "_intake.py").read_text(encoding="utf-8")
    tree = ast.parse(intake)
    readers = {
        node.name: ast.get_source_segment(intake, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and "raw_fts" in (ast.get_source_segment(intake, node) or "")
    }
    assert set(readers) == {
        "search_raw_objects",
        "search_raw_objects_in_set",
        "search_owned_file_content",
    }
    for name, source in readers.items():
        assert "_not_private_raw_dependency" in source, name
        assert "status='ignored'" in source, name


def test_source_text_only_reaches_prompts_through_the_explicit_filtered_tool():
    """Body-free file selection may use FTS; only source_search projects text.

    Pending uploads must be searchable when the person asks about an uploaded
    source, but they must not silently enter HybridSearcher or every prompt.  The
    execution kernel remains the only bridge that projects excerpts. Runtime may
    ask the two verdict-aware helpers for opaque ids to resolve an explicitly
    referenced file, but those helpers return no source body.
    """
    from pathlib import Path

    root = Path(__file__).parent.parent / "friday"
    retrieval = (root / "retrieval" / "__init__.py").read_text(encoding="utf-8")
    assert "search_raw_objects" not in retrieval
    runtime = (root / "agent_runtime" / "__init__.py").read_text(encoding="utf-8")
    assert "self.storage.search_raw_objects(" not in runtime
    assert "search_owned_file_content" in runtime
    assert "search_raw_objects_in_set" in runtime
    kernel = (root / "execution_kernel" / "__init__.py").read_text(encoding="utf-8")
    assert kernel.count("storage.search_raw_objects") == 1
    assert '"source_search"' in kernel


def test_source_search_over_http_excludes_rejected_material(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        from friday.permissions import LEGACY_OWNER_USER_ID

        storage.ensure_user(LEGACY_OWNER_USER_ID)
        kept = _ingest(storage, LEGACY_OWNER_USER_ID, f"оставлено {PHRASE}", status=InboxStatus.PENDING)
        _ingest(storage, LEGACY_OWNER_USER_ID, f"отклонено {PHRASE}", status=InboxStatus.IGNORED)

        owner = {"Authorization": f"Bearer {settings.api_token}"}
        response = client.get("/api/knowledge/sources", params={"q": PHRASE}, headers=owner)
        assert response.status_code == 200
        body = response.json()
        assert [item["id"] for item in body["items"]] == [kept]
        assert body["excludes"] == "ignored"
        assert "_raw_content" not in str(body)
        assert "_raw_metadata" not in str(body)

        # Unauthenticated callers get nothing.
        assert client.get("/api/knowledge/sources", params={"q": PHRASE}).status_code == 401


def test_one_rejection_hides_the_source_even_among_several_inbox_rows(storage):
    """A Raw Object can carry SEVERAL Inbox rows, and a join let it through.

    `ingest_text` returns the existing raw object on an idempotent replay while
    still creating a review row, so `raw_object_id` is not unique in `inbox`. The
    first version of this query joined on the row and admitted the object whenever
    any single row was not the rejection — reproduced, and it returned rejected
    text. The test is `NOT EXISTS ... status='ignored'`: any rejection hides it.
    """
    storage.ensure_user("owner")
    raw = RawObject(
        id=new_id("raw"),
        user_id="owner",
        source="upload",
        source_ref=new_id("src"),
        raw_content=f"две строки inbox {PHRASE}",
        content_type="text",
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(id=new_id("inbox"), user_id="owner", raw_object_id=raw.id, status=InboxStatus.IGNORED)
    )
    storage.store_inbox_item(
        InboxItem(id=new_id("inbox"), user_id="owner", raw_object_id=raw.id, status=InboxStatus.PENDING)
    )
    assert (
        storage.execute("SELECT COUNT(*) AS c FROM inbox WHERE raw_object_id=?", (raw.id,)).fetchone()["c"]
        == 2
    )

    assert storage.search_raw_objects("owner", PHRASE) == []


def test_the_index_is_rebuilt_over_rows_that_predate_it(settings, tmp_path, simulate_legacy_schema):
    """An external-content FTS table created over existing rows starts EMPTY.

    The rebuild is guarded on "did this table already exist", and probing that
    AFTER running the DDL always answers yes — so the guard skipped the rebuild and
    left an index that reports rows and matches nothing. Caught only by searching a
    copy of the owner's real database, where every query returned zero.
    """
    from dataclasses import replace

    from friday.storage import FridayStorage

    database = tmp_path / "predates.sqlite3"
    first = FridayStorage(replace(settings, database_path=database))
    try:
        first.ensure_user("owner")
        _ingest(first, "owner", f"записано до индекса {PHRASE}", status=InboxStatus.PENDING)
        # Drop the index and its triggers: the state a schema-16 database is in.
        with first.transaction() as conn:
            conn.execute("DROP TABLE IF EXISTS raw_fts")
            for name in ("raw_objects_ai", "raw_objects_ad", "raw_objects_au"):
                conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    finally:
        first.close(final=True)

    with sqlite3.connect(database) as legacy:
        simulate_legacy_schema(legacy, 16)

    migrated = FridayStorage(replace(settings, database_path=database))
    try:
        assert migrated.search_raw_objects("owner", PHRASE), "the index was not rebuilt over existing rows"
    finally:
        migrated.close(final=True)
