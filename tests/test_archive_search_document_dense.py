from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import replace
from typing import Any

import pytest

import friday.retrieval.archive_search_dense as dense_plan_module
import friday.retrieval.archive_search_service as service_module
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval import HybridSearcher, knowledge_chunk_units, pack_vector
from friday.retrieval.archive_search_authority import create_archive_model_batch_ledger
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus, ArchiveSearchRequest
from friday.retrieval.archive_search_service import prepare_archive_search_in_transaction
from friday.retrieval.contracts import EmbeddingCompatibility, RevisionKind, SearchLane
from friday.storage.models import InboxItem, InboxStatus, KnowledgeObject, RawObject
from friday.web_surfer import WebSurfer

TENANT = "archive-dense-tenant"
OWNER = "archive-dense-owner"
OTHER = "archive-dense-other"
MODEL = "archive-dense-test-model"
SCHEME = "v2:200:20:8"
QUERY = "семантическая метеорология"
TAIL_QUERY = "densecapneedle"


class _DeterministicEmbeddings:
    remote_enabled = True

    def __init__(
        self,
        settings: Any,
        *,
        fail: bool = False,
        storage: Any = None,
        query_vector: list[float] | None = None,
    ) -> None:
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
        self.query_vector = [1.0, 0.0] if query_vector is None else query_vector

    async def embed(self, texts: list[str], **_kwargs: object) -> list[list[float]] | None:
        if self.storage is not None:
            assert self.storage.conn.in_transaction is False
        if self.fail:
            return None
        assert len(texts) == 1 and texts[0]
        return [self.query_vector]


def _seed_document(
    storage: Any,
    *,
    suffix: str,
    owner: str,
    concept_vector: list[float],
    with_chunks: bool = True,
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
        {ko_id: chunks} if with_chunks else {},
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


def _seed_tail_documents(
    storage: Any,
    count: int,
    *,
    needle: str = TAIL_QUERY,
) -> set[str]:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    raw_ids: set[str] = set()
    for ordinal in range(1, count + 1):
        identity = 0xD000000000000000 + ordinal
        raw_id = f"raw_{identity:016x}"
        raw_ids.add(raw_id)
        body = f"{needle} bounded lexical evidence {ordinal:03d}"
        at = f"2026-08-29T10:{ordinal // 60:02d}:{ordinal % 60:02d}+00:00"
        storage.store_raw_object(
            RawObject(
                id=raw_id,
                user_id=TENANT,
                source="upload",
                source_ref=f"tail:{ordinal:03d}",
                raw_content=body,
                content_type="file",
                metadata_json={
                    "filename": f"tail-{ordinal:03d}.txt",
                    "media_kind": "document",
                    "mime_type": "text/plain",
                    "uploaded_by": OWNER,
                },
                content_hash=hashlib.sha256(body.encode()).hexdigest(),
                received_at=at,
                created_at=at,
            )
        )
        storage.store_inbox_item(
            InboxItem(
                id=f"inbox_{identity:016x}",
                user_id=TENANT,
                raw_object_id=raw_id,
                status=InboxStatus.CLASSIFIED,
                created_at=at,
                reviewed_at=at,
                reviewed_by=OWNER,
            )
        )
    return raw_ids


def _seed_tail_messages(storage: Any, count: int) -> tuple[set[str], str, str]:
    conversation_ids: set[str] = set()
    for ordinal in range(1, count + 1):
        conversation = storage.create_conversation(OWNER, f"tail conversation {ordinal:03d}")
        conversation_ids.add(conversation["id"])
        storage.store_message(
            conversation["id"],
            OWNER,
            "assistant",
            f"{TAIL_QUERY} bounded message evidence {ordinal:03d}",
        )
    boundary_conversation = storage.create_conversation(OWNER, "accepted dense tail boundary")
    boundary = storage.store_message(
        boundary_conversation["id"],
        OWNER,
        "user",
        "current archive request",
    )
    return conversation_ids, boundary_conversation["id"], boundary["id"]


def _install_federation_capture(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    captured: list[Any] = []
    original = service_module._collect_federated_in_transaction

    def capture(*args: object, **kwargs: object) -> Any:
        value = original(*args, **kwargs)
        captured.append(value)
        return value

    monkeypatch.setattr(service_module, "_collect_federated_in_transaction", capture)
    return captured


def _stable_materialization(
    storage: Any,
    authorization: AuthorizationService,
    request: ArchiveSearchRequest,
    *,
    captured: list[Any],
    discriminator: str,
    dense_query_plan: object | None,
    current_conversation_id: str | None = None,
    boundary_user_message_id: str | None = None,
) -> dict[str, object]:
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=OWNER,
        turn_discriminator=f"turn-{discriminator}",
    )
    before = len(captured)
    with storage.transaction() as conn:
        prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=f"snapshot-{discriminator}",
            run_discriminator=discriminator,
            turn_ledger=ledger,
            current_conversation_id=current_conversation_id,
            boundary_user_message_id=boundary_user_message_id,
            dense_query_plan=dense_query_plan,  # type: ignore[arg-type]
        )
    fresh = captured[before:]
    assert len(fresh) == 2
    assert service_module._same_federation(fresh[0], fresh[1])

    def coverage_payload(item: Any) -> dict[str, object]:
        return {key: value for key, value in item.to_payload().items() if key != "execution_binding"}

    federation = fresh[0]
    return {
        "candidates": [
            item.to_private_payload() for item in (*federation.candidates, *federation.tail_candidates)
        ],
        "coverage": [
            coverage_payload(item) for item in federation.coverage if item.lane is not SearchLane.DENSE
        ],
        "terminal_coverage": [
            coverage_payload(item)
            for item in federation.terminal_coverage
            if item.lane is not SearchLane.DENSE
        ],
        "warnings": [item.value for item in federation.warnings],
    }


@pytest.mark.asyncio
async def test_corpus_dense_recall_is_deterministic_principal_scoped_and_cited(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    storage.ensure_user(OTHER)
    _seed_document(
        storage,
        suffix="1",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
    )
    _seed_document(storage, suffix="2", owner=OWNER, concept_vector=[0.0, 1.0])
    _seed_document(
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
    assert not hasattr(dense_plan_module, "issue_archive_dense_query_plan")
    assert "issue_archive_dense_query_plan" not in dense_plan_module.__all__
    with pytest.raises(TypeError):
        pickle.dumps(first_plan)

    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    request = ArchiveSearchRequest.create(
        query=QUERY,
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    prepared_values: list[Any] = []

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
        prepared_values.append(prepared)
        return json.loads(prepared.authorized_batch.model_visible_canonical_bytes)

    first = run(first_plan, "dense-first")
    second = run(second_plan, "dense-second")
    assert len(first["candidates"]) == 1
    assert len(second["candidates"]) == 1
    candidate = first["candidates"][0]
    second_candidate = second["candidates"][0]
    assert candidate["title"] == second_candidate["title"] == "Dense 1"
    assert candidate["matches"] == second_candidate["matches"]
    assert [item["excerpt"] for item in candidate["passages"]] == [
        item["excerpt"] for item in second_candidate["passages"]
    ]
    assert candidate["matches"] == [{"channel": "dense", "rank": 1}]
    assert candidate["passages"] and candidate["passages"][0]["excerpt"]
    private_passage = (
        prepared_values[0]
        .authorized_batch._page.results[0]
        .candidate.passages[  # noqa: SLF001
            0
        ]
        .passage_ref
    )
    assert private_passage.source_revision.kind is RevisionKind.KNOWLEDGE_VERSION
    assert private_passage.source_revision.value == "1"
    assert private_passage.passage_index_version == SCHEME
    assert private_passage.embedding.compatibility is EmbeddingCompatibility.CURRENT
    assert private_passage.embedding.model_id == MODEL
    assert private_passage.embedding.dimensions == 2
    assert private_passage.embedding.source_version == 1
    assert private_passage.embedding.chunk_scheme == SCHEME
    assert private_passage.embedding.chunk_content_sha256 is not None
    serialized = json.dumps(first, ensure_ascii=False)
    assert "Dense 3" not in serialized
    dense_coverage = next(item for item in first["coverage"] if item["lane"] == SearchLane.DENSE.value)
    assert dense_coverage["states"] == ["backfill_pending", "partial"]
    lexical_coverage = next(item for item in first["coverage"] if item["lane"] == SearchLane.LEXICAL.value)
    assert lexical_coverage["matched_at_least"] == 0


@pytest.mark.asyncio
async def test_revalidated_dense_rank_one_is_not_buried_behind_the_lexical_tail(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    _seed_document(
        storage,
        suffix="9201",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
    )
    _seed_tail_documents(storage, 40, needle=QUERY)
    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(
        TENANT,
        QUERY,
        principal_id=OWNER,
    )
    assert plan is not None
    captured = _install_federation_capture(monkeypatch)
    materialized = _stable_materialization(
        storage,
        AuthorizationService(storage, shared_tenant=TENANT),
        ArchiveSearchRequest.create(
            query=QUERY,
            corpora=(ArchiveSearchCorpus.DOCUMENTS,),
            limit=20,
        ),
        captured=captured,
        discriminator="dense-ranked-with-lexical-tail",
        dense_query_plan=plan,
    )
    candidates = materialized["candidates"]
    target_rank = next(
        rank
        for rank, candidate in enumerate(candidates, 1)  # type: ignore[arg-type]
        if candidate["title"] == "Dense 9201"
    )
    target = candidates[target_rank - 1]  # type: ignore[index]
    assert target_rank <= 2
    assert {item["channel"] for item in target["matches"]} >= {"dense"}


@pytest.mark.asyncio
async def test_dense_backend_or_stale_sidecar_fails_soft_without_changing_lexical(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    request = ArchiveSearchRequest.create(
        query="Атмосферное давление",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    _seed_document(
        storage,
        suffix="4000",
        owner=OWNER,
        concept_vector=[0.0, 1.0],
        with_chunks=False,
    )
    empty_plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
    ).prepare_archive_dense_query_plan(
        TENANT,
        request.query,
        principal_id=OWNER,
    )
    assert empty_plan is not None
    _raw_id, knowledge_id = _seed_document(
        storage,
        suffix="4",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
    )
    unavailable = HybridSearcher(storage, _DeterministicEmbeddings(settings, fail=True))
    failed_backend = await unavailable.prepare_archive_dense_query_plan(
        TENANT,
        "Атмосферное давление",
        principal_id=OWNER,
    )
    assert failed_backend is None

    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    healthy = HybridSearcher(storage, _DeterministicEmbeddings(settings))
    current_plan = await healthy.prepare_archive_dense_query_plan(
        TENANT,
        request.query,
        principal_id=OWNER,
    )
    assert current_plan is not None
    tampered_plan = await healthy.prepare_archive_dense_query_plan(
        TENANT,
        request.query,
        principal_id=OWNER,
    )
    assert tampered_plan is not None
    object.__setattr__(tampered_plan, "_seal", b"x" * 32)
    below_floor_plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings, query_vector=[0.0, 1.0]),
        dense_evidence_min=0.9,
    ).prepare_archive_dense_query_plan(
        TENANT,
        request.query,
        principal_id=OWNER,
    )
    assert below_floor_plan is not None

    def run(plan: object | None, discriminator: str) -> dict[str, object]:
        with storage.transaction() as conn:
            prepared = prepare_archive_search_in_transaction(
                conn,
                authorization=authorization,
                actor=_actor(),
                tenant_id=TENANT,
                principal_id=OWNER,
                request=request,
                snapshot_discriminator=f"archive-dense-{discriminator}",
                run_discriminator=f"dense-{discriminator}",
                turn_ledger=create_archive_model_batch_ledger(
                    tenant_id=TENANT,
                    principal_id=OWNER,
                    turn_discriminator=f"turn-dense-{discriminator}",
                ),
                dense_query_plan=plan,  # type: ignore[arg-type]
            )
        payload = json.loads(prepared.authorized_batch.model_visible_canonical_bytes)
        candidates = payload["candidates"]
        for candidate in candidates:
            candidate.pop("source_handle")
            for passage in candidate["passages"]:
                passage.pop("passage_handle")
        return {
            "candidates": candidates,
            "coverage": [item for item in payload["coverage"] if item["lane"] != "dense"],
        }

    baseline = run(None, "baseline")
    assert baseline["candidates"]
    for label, plan in (
        ("backend-failure", failed_backend),
        ("empty", empty_plan),
        ("below-floor", below_floor_plan),
        ("tampered", tampered_plan),
    ):
        assert run(plan, label) == baseline

    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_objects SET version=version+1 WHERE id=? AND user_id=?",
            (knowledge_id, TENANT),
        )
    assert run(current_plan, "stale") == baseline


@pytest.mark.asyncio
async def test_empty_dense_plan_preserves_full_document_tail(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_sources = _seed_tail_documents(storage, 86)
    _seed_document(
        storage,
        suffix="8600",
        owner=OWNER,
        concept_vector=[0.0, 1.0],
        with_chunks=False,
    )
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    captured = _install_federation_capture(monkeypatch)
    request = ArchiveSearchRequest.create(
        query=TAIL_QUERY,
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=20,
    )
    baseline = _stable_materialization(
        storage,
        authorization,
        request,
        captured=captured,
        discriminator="tail-documents-baseline",
        dense_query_plan=None,
    )
    baseline_sources = {
        candidate["resolved_source"]["source_ref"]["canonical_object_id"]
        for candidate in baseline["candidates"]  # type: ignore[union-attr]
    }
    assert baseline_sources == expected_sources

    empty = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
    ).prepare_archive_dense_query_plan(
        TENANT,
        TAIL_QUERY,
        principal_id=OWNER,
    )
    assert empty is not None
    assert (
        _stable_materialization(
            storage,
            authorization,
            request,
            captured=captured,
            discriminator="tail-documents-empty",
            dense_query_plan=empty,
        )
        == baseline
    )


@pytest.mark.asyncio
async def test_empty_dense_plan_preserves_mixed_document_and_message_tail(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_documents = _seed_tail_documents(storage, 52)
    expected_messages, conversation_id, boundary_id = _seed_tail_messages(storage, 52)
    _seed_document(
        storage,
        suffix="5200",
        owner=OWNER,
        concept_vector=[0.0, 1.0],
        with_chunks=False,
    )
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    captured = _install_federation_capture(monkeypatch)
    request = ArchiveSearchRequest.create(
        query=TAIL_QUERY,
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.MESSAGES),
        limit=20,
    )
    baseline = _stable_materialization(
        storage,
        authorization,
        request,
        captured=captured,
        discriminator="tail-mixed-baseline",
        dense_query_plan=None,
        current_conversation_id=conversation_id,
        boundary_user_message_id=boundary_id,
    )
    baseline_sources = {
        candidate["resolved_source"]["source_ref"]["canonical_object_id"]
        for candidate in baseline["candidates"]  # type: ignore[union-attr]
    }
    assert baseline_sources == expected_documents | expected_messages
    empty = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
    ).prepare_archive_dense_query_plan(
        TENANT,
        TAIL_QUERY,
        principal_id=OWNER,
    )
    assert empty is not None
    assert (
        _stable_materialization(
            storage,
            authorization,
            request,
            captured=captured,
            discriminator="tail-mixed-empty",
            dense_query_plan=empty,
            current_conversation_id=conversation_id,
            boundary_user_message_id=boundary_id,
        )
        == baseline
    )


@pytest.mark.parametrize("drift", ("stale", "tampered"))
@pytest.mark.asyncio
async def test_knowledge_dense_revalidates_current_and_rejects_stale_or_tampered(
    drift: str,
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    _raw_id, knowledge_id = _seed_document(
        storage,
        suffix="9101",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
    )
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    request = ArchiveSearchRequest.create(
        query=QUERY,
        corpora=(ArchiveSearchCorpus.KNOWLEDGE,),
        limit=5,
    )
    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(
        TENANT,
        QUERY,
        principal_id=OWNER,
    )
    assert plan is not None

    def run(discriminator: str) -> tuple[dict[str, Any], Any]:
        with storage.transaction() as conn:
            prepared = prepare_archive_search_in_transaction(
                conn,
                authorization=authorization,
                actor=_actor(),
                tenant_id=TENANT,
                principal_id=OWNER,
                request=request,
                snapshot_discriminator=f"knowledge-{discriminator}",
                run_discriminator=f"knowledge-{discriminator}",
                turn_ledger=create_archive_model_batch_ledger(
                    tenant_id=TENANT,
                    principal_id=OWNER,
                    turn_discriminator=f"turn-knowledge-{discriminator}",
                ),
                dense_query_plan=plan,
            )
        return json.loads(prepared.authorized_batch.model_visible_canonical_bytes), prepared

    current, prepared = run(f"current-{drift}")
    assert [item["title"] for item in current["candidates"]] == ["Dense 9101"]
    current_dense = next(item for item in current["coverage"] if item["lane"] == "dense")
    assert current_dense["eligible_authorized"] == 1
    assert current_dense["examined"] == 1
    assert current_dense["matched_at_least"] == 1
    candidate = prepared.authorized_batch._page.results[0].candidate  # noqa: SLF001
    assert candidate.passages[0].passage_ref.source_revision.kind is RevisionKind.KNOWLEDGE_VERSION
    assert candidate.passages[0].passage_ref.source_revision.value == "1"
    assert candidate.passages[0].passage_ref.passage_index_version == SCHEME
    assert candidate.passages[0].passage_ref.embedding.compatibility is EmbeddingCompatibility.CURRENT
    assert candidate.passages[0].passage_ref.embedding.model_id == MODEL

    with storage.transaction() as conn:
        if drift == "stale":
            conn.execute(
                "UPDATE knowledge_objects SET version=version+1 WHERE id=? AND user_id=?",
                (knowledge_id, TENANT),
            )
        else:
            conn.execute(
                "UPDATE knowledge_chunk_embeddings SET content_hash=? "
                "WHERE knowledge_object_id=? AND chunk_index=0 AND user_id=?",
                ("f" * 64, knowledge_id, TENANT),
            )
    rejected, _prepared = run(drift)
    assert rejected["candidates"] == []
    rejected_dense = next(item for item in rejected["coverage"] if item["lane"] == "dense")
    assert rejected_dense["eligible_authorized"] == 1
    assert rejected_dense["examined"] == 0
    assert rejected_dense["matched_at_least"] == 0
    assert rejected_dense["states"] == ["backfill_pending", "partial"]


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
