from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import replace
from typing import Any

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval import HybridSearcher, knowledge_chunk_units, pack_vector
from friday.retrieval.archive_search_authority import create_archive_model_batch_ledger
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus, ArchiveSearchRequest
from friday.retrieval.archive_search_dense import issue_archive_dense_query_plan
from friday.retrieval.archive_search_service import prepare_archive_search_in_transaction
from friday.retrieval.contracts import SearchLane
from friday.storage.models import KnowledgeObject, RawObject
from friday.web_surfer import WebSurfer

TENANT = "archive-dense-tenant"
OWNER = "archive-dense-owner"
OTHER = "archive-dense-other"
MODEL = "archive-dense-test-model"
SCHEME = "v2:200:20:8"
QUERY = "семантическая метеорология"


class _DeterministicEmbeddings:
    remote_enabled = True

    def __init__(self, settings: Any, *, fail: bool = False, storage: Any = None) -> None:
        self.settings = replace(
            settings,
            embeddings_model=MODEL,
            embeddings_chunk_chars=200,
            embeddings_chunk_overlap_chars=20,
            embeddings_chunk_max_per_object=8,
            embeddings_chunk_scan_multiplier=8,
            embeddings_dense_max_objects=100,
            embeddings_recall_candidates=10,
            embeddings_resident_cache=False,
        )
        self.fail = fail
        self.storage = storage

    async def embed(self, texts: list[str], **_kwargs: object) -> list[list[float]] | None:
        if self.storage is not None:
            assert self.storage.conn.in_transaction is False
        if self.fail:
            return None
        assert len(texts) == 1 and texts[0]
        return [[1.0, 0.0]]


def _seed_document(
    storage: Any,
    *,
    suffix: str,
    owner: str,
    concept_vector: list[float],
) -> tuple[str, str]:
    raw_id = f"raw_{suffix:0>16}"
    ko_id = f"ko_dense{suffix:0>8}"
    sentence = (
        "Атмосферное давление и прогноз облачности описаны в этом закрытом отчёте. "
        if concept_vector[0] > concept_vector[1]
        else "Порядок инвентаризации серверных стоек описан в этом закрытом отчёте. "
    )
    body = "\n".join(sentence for _item in range(18))
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=TENANT,
            source="upload",
            source_ref=f"telegram-file:{suffix}",
            raw_content=body,
            content_type="file",
            metadata_json={
                "filename": f"dense-{suffix}.pdf",
                "media_kind": "document",
                "mime_type": "application/pdf",
                "uploaded_by": owner,
            },
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
            received_at="2026-08-30T10:00:00+00:00",
            created_at="2026-08-30T10:00:00+00:00",
        )
    )
    item = KnowledgeObject(
        id=ko_id,
        user_id=TENANT,
        raw_object_id=raw_id,
        content=body,
        content_type="document",
        title=f"Dense {suffix}",
        summary="",
        knowledge_kind="document",
        lifecycle_stage="active",
        version=1,
        created_at="2026-08-30T10:01:00+00:00",
        updated_at="2026-08-30T10:01:00+00:00",
    )
    storage.store_knowledge_object(item)
    units = knowledge_chunk_units(
        {
            "content": body,
            "title": item.title,
            "summary": item.summary,
            "knowledge_kind": item.knowledge_kind,
        },
        max_chars=200,
        overlap_chars=20,
        max_chunks=8,
    )
    assert len(units) > 1
    chunks = []
    for index, (start, end, text) in enumerate(units):
        chunks.append(
            {
                "chunk_index": index,
                "user_id": TENANT,
                "model": MODEL,
                "dim": 2,
                "source_version": 1,
                "chunk_scheme": SCHEME,
                "start_char": start,
                "end_char": end,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
                "vector": pack_vector(concept_vector),
            }
        )
    storage.upsert_knowledge_vectors(
        [
            {
                "knowledge_object_id": ko_id,
                "user_id": TENANT,
                "model": MODEL,
                "dim": 2,
                "source_version": 1,
                "content_hash": hashlib.sha256(body.encode()).hexdigest(),
                "chunk_scheme": SCHEME,
                "vector": pack_vector(concept_vector),
            }
        ],
        {ko_id: chunks},
    )
    return raw_id, ko_id


def _actor() -> ActorContext:
    return ActorContext(
        user_id=TENANT,
        preset_key="user",
        source="archive-dense-test",
        shared_tenant=True,
        person_id=OWNER,
    )


@pytest.mark.asyncio
async def test_corpus_dense_recall_is_deterministic_principal_scoped_and_cited(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    storage.ensure_user(OTHER)
    _target_raw, target_ko = _seed_document(
        storage,
        suffix="1",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
    )
    _seed_document(storage, suffix="2", owner=OWNER, concept_vector=[0.0, 1.0])
    _foreign_raw, foreign_ko = _seed_document(
        storage,
        suffix="3",
        owner=OTHER,
        concept_vector=[1.0, 0.0],
    )
    searcher = HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    )
    first_plan = await searcher.prepare_archive_dense_query_plan(
        TENANT,
        QUERY,
        principal_id=OWNER,
    )
    second_plan = await searcher.prepare_archive_dense_query_plan(
        TENANT,
        QUERY,
        principal_id=OWNER,
    )
    assert repr(first_plan) == repr(second_plan) == "ArchiveDenseQueryPlan(private=True)"
    with pytest.raises(TypeError):
        pickle.dumps(first_plan)

    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    request = ArchiveSearchRequest.create(
        query=QUERY,
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )

    def run(plan: object, discriminator: str) -> dict[str, Any]:
        with storage.transaction() as conn:
            prepared = prepare_archive_search_in_transaction(
                conn,
                authorization=authorization,
                actor=_actor(),
                tenant_id=TENANT,
                principal_id=OWNER,
                request=request,
                snapshot_discriminator="archive-dense-snapshot",
                run_discriminator=discriminator,
                turn_ledger=create_archive_model_batch_ledger(
                    tenant_id=TENANT,
                    principal_id=OWNER,
                    turn_discriminator=f"turn-{discriminator}",
                ),
                dense_query_plan=plan,  # type: ignore[arg-type]
            )
        return json.loads(prepared.authorized_batch.model_visible_canonical_bytes)

    first = run(first_plan, "dense-first")
    second = run(second_plan, "dense-second")
    adversarial = run(
        issue_archive_dense_query_plan(
            principal_id=OWNER,
            query=QUERY,
            model_id=MODEL,
            chunk_scheme=SCHEME,
            query_vector=[1.0, 0.0],
            minimum_score=0.35,
            candidates=((foreign_ko, 0), (target_ko, 0)),
        ),
        "dense-adversarial",
    )
    assert len(first["candidates"]) == 1
    assert len(second["candidates"]) == 1
    candidate = first["candidates"][0]
    second_candidate = second["candidates"][0]
    assert candidate["title"] == second_candidate["title"] == "Dense 1"
    assert candidate["matches"] == second_candidate["matches"]
    assert [item["excerpt"] for item in candidate["passages"]] == [
        item["excerpt"] for item in second_candidate["passages"]
    ]
    assert [item["title"] for item in adversarial["candidates"]] == ["Dense 1"]
    assert candidate["matches"] == [{"channel": "dense", "rank": 1}]
    assert candidate["passages"] and candidate["passages"][0]["excerpt"]
    serialized = json.dumps(first, ensure_ascii=False)
    assert "Dense 3" not in serialized
    dense_coverage = next(item for item in first["coverage"] if item["lane"] == SearchLane.DENSE.value)
    assert dense_coverage["states"] == ["backfill_pending", "partial"]
    lexical_coverage = next(
        item for item in first["coverage"] if item["lane"] == SearchLane.LEXICAL.value
    )
    assert lexical_coverage["matched_at_least"] == 0


@pytest.mark.asyncio
async def test_dense_backend_or_stale_sidecar_fails_soft_without_changing_lexical(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    _seed_document(storage, suffix="4", owner=OWNER, concept_vector=[1.0, 0.0])
    unavailable = HybridSearcher(storage, _DeterministicEmbeddings(settings, fail=True))
    assert (
        await unavailable.prepare_archive_dense_query_plan(TENANT, QUERY, principal_id=OWNER)
        is None
    )

    request = ArchiveSearchRequest.create(
        query="Атмосферное давление",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    healthy = HybridSearcher(storage, _DeterministicEmbeddings(settings))
    tampered_plan = await healthy.prepare_archive_dense_query_plan(
        TENANT,
        request.query,
        principal_id=OWNER,
    )
    assert tampered_plan is not None
    object.__setattr__(tampered_plan, "_seal", b"x" * 32)
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator="archive-dense-fail-soft",
            run_discriminator="dense-fail-soft",
            turn_ledger=create_archive_model_batch_ledger(
                tenant_id=TENANT,
                principal_id=OWNER,
                turn_discriminator="turn-dense-fail-soft",
            ),
            dense_query_plan=tampered_plan,
        )
    payload = json.loads(prepared.authorized_batch.model_visible_canonical_bytes)
    assert payload["candidates"]
    lexical = next(item for item in payload["coverage"] if item["lane"] == "lexical")
    dense = next(item for item in payload["coverage"] if item["lane"] == "dense")
    assert lexical["matched_at_least"] == 1
    assert dense["states"] == ["unavailable"]

    stale_plan = await healthy.prepare_archive_dense_query_plan(
        TENANT,
        QUERY,
        principal_id=OWNER,
    )
    assert stale_plan is not None
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_objects SET version=version+1 WHERE user_id=?",
            (TENANT,),
        )
    stale_request = ArchiveSearchRequest.create(
        query=QUERY,
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    with storage.transaction() as conn:
        stale = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=stale_request,
            snapshot_discriminator="archive-dense-stale",
            run_discriminator="dense-stale",
            turn_ledger=create_archive_model_batch_ledger(
                tenant_id=TENANT,
                principal_id=OWNER,
                turn_discriminator="turn-dense-stale",
            ),
            dense_query_plan=stale_plan,
        )
    stale_payload = json.loads(stale.authorized_batch.model_visible_canonical_bytes)
    stale_dense = next(item for item in stale_payload["coverage"] if item["lane"] == "dense")
    assert stale_payload["candidates"] == []
    assert stale_dense["states"] == ["backfill_pending", "partial"]


@pytest.mark.asyncio
async def test_kernel_routes_archive_dense_preparation_outside_the_storage_snapshot(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    _seed_document(storage, suffix="5", owner=OWNER, concept_vector=[1.0, 0.0])
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(
        storage,
        graph,
        web,
        ingestion,
        searcher=HybridSearcher(storage, _DeterministicEmbeddings(settings, storage=storage)),
    )
    actor = _actor()
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=OWNER,
        turn_discriminator="turn-dense-kernel",
    )
    invocation = kernel.create_archive_search_invocation(actor=actor, turn_ledger=ledger)
    try:
        result = await kernel.execute(
            "archive_search",
            {
                "query": QUERY,
                "corpora": [ArchiveSearchCorpus.DOCUMENTS.value],
                "_archive_invocation": invocation,
            },
            actor=actor,
        )
    finally:
        await web.close()
    assert result.success is True
    payload = json.loads(result.archive_model_visible_bytes())
    assert [item["title"] for item in payload["candidates"]] == ["Dense 5"]
    dense = next(item for item in payload["coverage"] if item["lane"] == "dense")
    assert dense["states"] == ["backfill_pending", "partial"]
