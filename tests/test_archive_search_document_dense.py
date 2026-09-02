from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import replace
from typing import Any

import pytest

import friday.retrieval.archive_search_dense as dense_plan_module
import friday.retrieval.archive_search_service as service_module
import friday.storage._archive_search_documents as archive_document_storage
from friday.document_catalog import (
    document_passage_set_sha256,
    register_document_passage_connection_functions,
)
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval import HybridSearcher, knowledge_chunk_units, pack_vector
from friday.retrieval.archive_evidence_replay import (
    ArchiveEvidenceReplayCoverageGrade,
    ArchiveEvidenceReplayStatus,
    replay_archive_evidence_in_transaction,
)
from friday.retrieval.archive_evidence_snapshot import archive_selected_evidence_snapshot_sha256
from friday.retrieval.archive_search_authority import create_archive_model_batch_ledger
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus, ArchiveSearchRequest
from friday.retrieval.archive_search_document_locator import (
    DOCUMENT_STORED_PASSAGE_INDEX_VERSION,
    LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION,
)
from friday.retrieval.archive_search_service import prepare_archive_search_in_transaction
from friday.retrieval.contracts import (
    AuthorityScope,
    CoverageState,
    EmbeddingCompatibility,
    RevisionKind,
    SearchCorpus,
    SearchExecutionBinding,
    SearchLane,
    TextSpanLocator,
)
from friday.storage._archive_search_documents import search_archive_document_lane
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


@pytest.mark.asyncio
async def test_archive_dense_plan_preparation_never_loads_knowledge_bodies(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    _seed_document(
        storage,
        suffix="9310",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
        body_override="dense plan body-free proof " * 4_000,
    )

    def forbidden_body_load(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("archive dense plan preparation loaded a KO body")

    monkeypatch.setattr(type(storage), "get_knowledge_object", forbidden_body_load)
    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(
        TENANT,
        QUERY,
        principal_id=OWNER,
    )
    projection = dense_plan_module.project_archive_dense_query_plan(
        plan,
        principal_id=OWNER,
        query=QUERY,
    )
    assert projection is not None
    assert projection.candidates


@pytest.mark.asyncio
async def test_archive_dense_plan_filters_hostile_vector_blobs_before_projection(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    _seed_document(
        storage,
        suffix="9311",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
    )
    _bad_raw, bad_knowledge = _seed_document(
        storage,
        suffix="9312",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_embeddings SET vector=zeroblob(4000000) WHERE knowledge_object_id=?",
            (bad_knowledge,),
        )
        conn.execute(
            "UPDATE knowledge_chunk_embeddings SET vector=zeroblob(4000000) WHERE knowledge_object_id=?",
            (bad_knowledge,),
        )

    document_rows = storage.get_user_embeddings(
        TENANT,
        MODEL,
        2,
        limit=100,
        uploaded_by=OWNER,
    )
    chunk_rows = storage.get_user_chunk_embeddings(
        TENANT,
        MODEL,
        2,
        object_limit=100,
        row_limit=1_000,
        uploaded_by=OWNER,
    )
    assert all(row_id != bad_knowledge for row_id, _vector in document_rows)
    assert all(not row_id.startswith(f"{bad_knowledge}#") for row_id, _vector in chunk_rows)

    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(TENANT, QUERY, principal_id=OWNER)
    projection = dense_plan_module.project_archive_dense_query_plan(
        plan,
        principal_id=OWNER,
        query=QUERY,
    )
    assert projection is not None
    assert projection.candidates
    assert all(item.knowledge_object_id != bad_knowledge for item in projection.candidates)


def test_shared_vector_readers_keep_bounded_legacy_ids_and_timestamps(storage: Any) -> None:
    """Archive validation must not narrow the shared legacy vector API."""
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    _raw_id, canonical_id = _seed_document(
        storage,
        suffix="9313",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
    )
    legacy_id = "ko-old"
    legacy_timestamp = "2026-08-30 10:01:00"
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO knowledge_objects(
                   id,user_id,raw_object_id,entity_id,content,content_type,title,summary,
                   tags_json,metadata_json,knowledge_kind,importance,quality_score,
                   promotion_score,lifecycle_stage,version,superseded_by_id,created_at,
                   updated_at,deleted_at
               )
               SELECT ?,user_id,raw_object_id,entity_id,content,content_type,title,summary,
                      tags_json,metadata_json,knowledge_kind,importance,quality_score,
                      promotion_score,lifecycle_stage,version,superseded_by_id,?,?,deleted_at
                 FROM knowledge_objects WHERE id=?""",
            (legacy_id, legacy_timestamp, legacy_timestamp, canonical_id),
        )
        conn.execute(
            """INSERT INTO knowledge_embeddings(
                   knowledge_object_id,user_id,model,dim,source_version,content_hash,
                   chunk_scheme,vector,updated_at
               )
               SELECT ?,user_id,model,dim,source_version,content_hash,chunk_scheme,
                      vector,?
                 FROM knowledge_embeddings WHERE knowledge_object_id=?""",
            (legacy_id, legacy_timestamp, canonical_id),
        )
        conn.execute(
            """INSERT INTO knowledge_chunk_embeddings(
                   knowledge_object_id,chunk_index,user_id,model,dim,source_version,
                   chunk_scheme,start_char,end_char,content_hash,vector,updated_at
               )
               SELECT ?,chunk_index,user_id,model,dim,source_version,chunk_scheme,
                      start_char,end_char,content_hash,vector,?
                 FROM knowledge_chunk_embeddings WHERE knowledge_object_id=?""",
            (legacy_id, legacy_timestamp, canonical_id),
        )

    document_ids = {
        row_id
        for row_id, _vector in storage.get_user_embeddings(
            TENANT,
            MODEL,
            2,
            limit=100,
            uploaded_by=OWNER,
        )
    }
    chunk_ids = {
        row_id
        for row_id, _vector in storage.get_user_chunk_embeddings(
            TENANT,
            MODEL,
            2,
            object_limit=100,
            row_limit=1_000,
            uploaded_by=OWNER,
        )
    }
    assert legacy_id in document_ids
    assert any(row_id.startswith(f"{legacy_id}#") for row_id in chunk_ids)


@pytest.mark.asyncio
async def test_archive_dense_materialization_reads_only_bounded_winning_spans(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    knowledge_ids: list[str] = []
    for ordinal in range(12):
        _raw_id, knowledge_id = _seed_document(
            storage,
            suffix=f"94{ordinal:02d}",
            owner=OWNER,
            concept_vector=[1.0, 0.0],
            body_override=(f"bounded dense source {ordinal:02d}\n" + "large inert source payload. " * 5_000),
        )
        knowledge_ids.append(knowledge_id)
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_objects SET title=?, summary=? WHERE id=?",
            ("H" * 1_500_000, "S" * 1_500_000, knowledge_ids[-1]),
        )
    request = ArchiveSearchRequest.create(
        query=QUERY,
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=20,
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

    per_source = 20_000
    aggregate = 42_000
    monkeypatch.setattr(archive_document_storage, "_DENSE_CHUNK_BODY_MAX_BYTES", per_source)
    monkeypatch.setattr(archive_document_storage, "_DENSE_CHUNK_BODY_BUDGET_BYTES", aggregate)
    original_reader = archive_document_storage._read_dense_chunk_body  # noqa: SLF001
    material_sizes: list[int] = []

    def monitored_reader(*args: object, **kwargs: object):
        selected = original_reader(*args, **kwargs)
        if selected is not None:
            body, material_bytes, _title, _header, _full_focus_source, _text_digest = selected
            assert len(body.encode("utf-8")) <= material_bytes
            material_sizes.append(material_bytes)
        return selected

    monkeypatch.setattr(archive_document_storage, "_read_dense_chunk_body", monitored_reader)
    statements: list[str] = []
    snapshot = "dense-bounded-spans"
    binding = _dense_binding(request, snapshot)
    storage.conn.set_trace_callback(statements.append)
    try:
        with storage.transaction() as conn:
            page = search_archive_document_lane(
                conn,
                tenant_id=TENANT,
                owner_id=OWNER,
                request=request,
                corpus=ArchiveSearchCorpus.DOCUMENTS,
                lane=SearchLane.DENSE,
                execution_binding=binding,
                snapshot_discriminator=snapshot,
                snapshot_current=True,
                dense_query_plan=plan,
            )
    finally:
        storage.conn.set_trace_callback(None)

    assert material_sizes
    assert sum(material_sizes) <= aggregate
    assert all(item <= per_source for item in material_sizes)
    coverage = page.to_coverage(
        execution_binding=binding,
        tenant_id=TENANT,
        owner_id=OWNER,
        request=request,
        snapshot_discriminator=snapshot,
    )
    assert CoverageState.CAPPED in coverage.states
    assert CoverageState.UNAVAILABLE in coverage.states
    lead = next(item for item in statements if "dense_candidates AS MATERIALIZED" in item)
    assert "SELECT s.*" not in lead
    assert "s.knowledge_title" not in lead
    reads = [item for item in statements if "AS dense_chunk_body" in item]
    assert reads
    assert all("k.content IS r.raw_content" not in item for item in reads)
    assert all("substr(k.content" in item and "substr(r.raw_content" in item for item in reads)
    assert all("k.title AS knowledge_title" not in item for item in reads)
    assert all("length(CAST(COALESCE(k.title" not in item for item in reads)
    assert all("substr(COALESCE(k.title" in item for item in reads)


def _seed_document(
    storage: Any,
    *,
    suffix: str,
    owner: str,
    concept_vector: list[float],
    with_chunks: bool = True,
    body_override: str | None = None,
    passage_ready: bool = False,
) -> tuple[str, str]:
    raw_id = f"raw_{suffix:0>16}"
    ko_id = f"ko_dense{suffix:0>8}"
    sentence = (
        "Атмосферное давление и прогноз облачности описаны в этом закрытом отчёте. "
        if concept_vector[0] > concept_vector[1]
        else "Порядок инвентаризации серверных стоек описан в этом закрытом отчёте. "
    )
    body = "\n".join(sentence for _item in range(18)) if body_override is None else body_override
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
                **(
                    {
                        "extraction_success": True,
                        "text_extraction_success": True,
                    }
                    if passage_ready
                    else {}
                ),
            },
            content_hash=hashlib.sha256(f"source-bytes-{suffix}".encode()).hexdigest(),
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


def _force_dense_winning_span(
    storage: Any,
    knowledge_id: str,
    body: str,
    start: int,
    end: int,
) -> None:
    row = storage.conn.execute(
        "SELECT title,summary,knowledge_kind FROM knowledge_objects WHERE id=?",
        (knowledge_id,),
    ).fetchone()
    assert row is not None
    header = " ".join(str(row[key] or "") for key in ("title", "summary", "knowledge_kind") if row[key])[:50]
    embedded = f"{header}\n\n{body[start:end]}"
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_chunk_embeddings SET vector=? WHERE knowledge_object_id=?",
            (pack_vector([0.0, 1.0]), knowledge_id),
        )
        conn.execute(
            """UPDATE knowledge_chunk_embeddings
                  SET start_char=?, end_char=?, content_hash=?, vector=?
                WHERE knowledge_object_id=? AND chunk_index=0""",
            (
                start,
                end,
                hashlib.sha256(embedded.encode()).hexdigest(),
                pack_vector([1.0, 0.0]),
                knowledge_id,
            ),
        )


def _actor() -> ActorContext:
    return ActorContext(
        user_id=TENANT,
        preset_key="user",
        source="archive-dense-test",
        shared_tenant=True,
        person_id=OWNER,
    )


def _dense_binding(request: ArchiveSearchRequest, snapshot: str) -> SearchExecutionBinding:
    return SearchExecutionBinding.create(
        normalized_private_request_json=request.to_identity_json(),
        authority_scope=AuthorityScope.TENANT_PRINCIPAL,
        tenant_id=TENANT,
        principal_id=OWNER,
        requested_targets=((SearchCorpus.RAW_DOCUMENTS, SearchLane.DENSE),),
        snapshot_discriminator=snapshot,
        run_discriminator=f"{snapshot}-run",
        privacy_key=b"d" * 32,
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
async def test_dense_plan_identity_binds_query_and_focus_as_one_exact_recall_input(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    _seed_document(
        storage,
        suffix="9300",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
    )
    request = ArchiveSearchRequest.create(
        query="Иванов",
        focus="должность",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
    )
    changed_focus = ArchiveSearchRequest.create(
        query=request.query,
        focus="роль",
        corpora=request.corpora,
    )
    searcher = HybridSearcher(storage, _DeterministicEmbeddings(settings))
    plan = await searcher.prepare_archive_dense_query_plan(
        TENANT,
        request.dense_query,
        principal_id=OWNER,
    )
    changed_plan = await searcher.prepare_archive_dense_query_plan(
        TENANT,
        changed_focus.dense_query,
        principal_id=OWNER,
    )
    assert plan is not None
    assert changed_plan is not None

    projection = dense_plan_module.project_archive_dense_query_plan(
        plan,
        principal_id=OWNER,
        query=request.dense_query,
    )
    changed_projection = dense_plan_module.project_archive_dense_query_plan(
        changed_plan,
        principal_id=OWNER,
        query=changed_focus.dense_query,
    )
    assert projection is not None
    assert changed_projection is not None
    assert projection.identity_sha256 != changed_projection.identity_sha256
    assert (
        dense_plan_module.project_archive_dense_query_plan(
            plan,
            principal_id=OWNER,
            query=request.query,
        )
        is None
    )
    assert (
        dense_plan_module.project_archive_dense_query_plan(
            plan,
            principal_id=OWNER,
            query=changed_focus.dense_query,
        )
        is None
    )


@pytest.mark.asyncio
async def test_focused_dense_passage_is_one_exact_anchor_bound_body_slice(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    expected = "Иванов\nДолжность: ведущий инженер"
    body = (
        f"Кадровая ведомость подразделения.\n\n{expected}\n\n" + "Техническое примечание к ведомости. " * 24
    )
    raw_id, _knowledge_id = _seed_document(
        storage,
        suffix="9301",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
        body_override=body,
    )
    request = ArchiveSearchRequest.create(
        query="Иванов",
        focus="должность",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(
        TENANT,
        request.dense_query,
        principal_id=OWNER,
    )
    assert plan is not None

    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=AuthorizationService(storage, shared_tenant=TENANT),
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator="focused-dense-exact-snapshot",
            run_discriminator="focused-dense-exact",
            turn_ledger=create_archive_model_batch_ledger(
                tenant_id=TENANT,
                principal_id=OWNER,
                turn_discriminator="turn-focused-dense-exact",
            ),
            dense_query_plan=plan,
        )

    selected = [
        result.candidate
        for result in prepared.authorized_batch._page.results  # noqa: SLF001
        if result.candidate.resolved_source.source_ref.canonical_object_id == raw_id
    ]
    assert len(selected) == 1
    assert ArchiveSearchCorpus.DOCUMENTS is selected[0].corpus
    assert any(match.channel.value == "dense" for match in selected[0].matches)
    assert len(selected[0].passages) == 1
    [passage] = selected[0].passages
    locator = passage.passage_ref.locator
    assert type(locator) is TextSpanLocator
    assert passage.excerpt == expected
    assert body[locator.start_char : locator.end_char] == passage.excerpt
    assert locator.start_char == body.index(expected)
    assert locator.end_char == locator.start_char + len(expected)
    assert passage.passage_ref.source_revision.kind is RevisionKind.RAW_CONTENT_SHA256
    assert passage.passage_ref.passage_index_version == LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
    assert passage.passage_ref.embedding.compatibility is EmbeddingCompatibility.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_focused_dense_projects_inside_the_authenticated_winning_chunk(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    first = "Иванов\nДолжность: генеральный директор по эксплуатации"
    second = "Иванов\nДолжность: инженер"
    body = f"{first}\n\n{'нейтральный контекст. ' * 24}\n\n{second}"
    raw_id, knowledge_id = _seed_document(
        storage,
        suffix="9303",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
        body_override=body,
    )
    second_start = body.rindex(second)
    chunks = tuple(
        storage.conn.execute(
            """SELECT chunk_index,start_char,end_char
                 FROM knowledge_chunk_embeddings
                WHERE knowledge_object_id=? ORDER BY chunk_index""",
            (knowledge_id,),
        ).fetchall()
    )
    winning_chunk = next(
        row
        for row in chunks
        if int(row["start_char"]) <= second_start and second_start + len(second) <= int(row["end_char"])
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE knowledge_chunk_embeddings SET vector=? WHERE knowledge_object_id=?",
            (pack_vector([0.0, 1.0]), knowledge_id),
        )
        conn.execute(
            """UPDATE knowledge_chunk_embeddings SET vector=?
                  WHERE knowledge_object_id=? AND chunk_index=?""",
            (pack_vector([1.0, 0.0]), knowledge_id, int(winning_chunk["chunk_index"])),
        )
    request = ArchiveSearchRequest.create(
        query="Иванов",
        focus="должность",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(
        TENANT,
        request.dense_query,
        principal_id=OWNER,
    )
    assert plan is not None

    snapshot = "focused-dense-second-record"
    with storage.transaction() as conn:
        page = search_archive_document_lane(
            conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.DENSE,
            execution_binding=_dense_binding(request, snapshot),
            snapshot_discriminator=snapshot,
            snapshot_current=True,
            dense_query_plan=plan,
        )

    assert len(page.candidates) == 1
    candidate = page.candidates[0]
    assert candidate.resolved_source.source_ref.canonical_object_id == raw_id
    assert len(candidate.passages) == 1
    passage = candidate.passages[0]
    locator = passage.passage_ref.locator
    assert type(locator) is TextSpanLocator
    assert passage.excerpt == second
    assert (locator.start_char, locator.end_char) == (second_start, second_start + len(second))
    assert body[locator.start_char : locator.end_char] == second


@pytest.mark.parametrize(
    ("suffix", "body", "selected"),
    (
        (
            "9360",
            "Foreign record\nSmith\nRole: engineer\n\n" + "neutral appendix. " * 30,
            "Smith\nRole: engineer",
        ),
        (
            "9361",
            "Prelude\n\nSmith\nRole: engineer\nNext record\n\n" + "neutral appendix. " * 30,
            "Smith\nRole: engineer",
        ),
        (
            "9362",
            "XXSmith\nRole: engineer\n\n" + "neutral appendix. " * 30,
            "Smith\nRole: engineer",
        ),
        (
            "9363",
            "Smith\nRole: engineerX\n\n" + "neutral appendix. " * 30,
            "Smith\nRole: engineer",
        ),
    ),
)
@pytest.mark.asyncio
async def test_focused_dense_rejects_false_chunk_boundaries(
    suffix: str,
    body: str,
    selected: str,
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    _raw_id, knowledge_id = _seed_document(
        storage,
        suffix=suffix,
        owner=OWNER,
        concept_vector=[0.0, 1.0],
        body_override=body,
    )
    start = body.index(selected)
    _force_dense_winning_span(storage, knowledge_id, body, start, start + len(selected))
    request = ArchiveSearchRequest.create(
        query="Smith",
        focus="role",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(TENANT, request.dense_query, principal_id=OWNER)
    assert plan is not None

    snapshot = f"focused-dense-boundary-{suffix}"
    with storage.transaction() as conn:
        page = search_archive_document_lane(
            conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.DENSE,
            execution_binding=_dense_binding(request, snapshot),
            snapshot_discriminator=snapshot,
            snapshot_current=True,
            dense_query_plan=plan,
        )

    assert page.candidates == ()
    assert page.matched == 0


@pytest.mark.asyncio
async def test_focused_dense_refuses_an_oversized_raw_source_before_projection(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    body = (
        "Smith\nRole: engineer\n\n" + "x" * (archive_document_storage._DENSE_CHUNK_BODY_MAX_BYTES + 64)  # noqa: SLF001
    )
    _seed_document(
        storage,
        suffix="9364",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
        body_override=body,
    )
    request = ArchiveSearchRequest.create(
        query="Smith",
        focus="role",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(TENANT, request.dense_query, principal_id=OWNER)
    assert plan is not None

    snapshot = "focused-dense-oversized-source"
    binding = _dense_binding(request, snapshot)
    with storage.transaction() as conn:
        page = search_archive_document_lane(
            conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.DENSE,
            execution_binding=binding,
            snapshot_discriminator=snapshot,
            snapshot_current=True,
            dense_query_plan=plan,
        )

    assert page.candidates == ()
    coverage = page.to_coverage(
        execution_binding=binding,
        tenant_id=TENANT,
        owner_id=OWNER,
        request=request,
        snapshot_discriminator=snapshot,
    )
    assert CoverageState.CAPPED in coverage.states
    assert CoverageState.UNAVAILABLE in coverage.states


@pytest.mark.asyncio
async def test_focused_dense_failed_full_source_reads_consume_the_attempt_budget(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    for ordinal in range(8):
        _seed_document(
            storage,
            suffix=f"937{ordinal}",
            owner=OWNER,
            concept_vector=[1.0, 0.0],
            body_override=(f"Smith {ordinal}\nRole: engineer\n\n" + "oversized dense source\n" * 40),
        )
    monkeypatch.setattr(archive_document_storage, "_DENSE_CHUNK_BODY_MAX_BYTES", 512)
    monkeypatch.setattr(archive_document_storage, "_DENSE_CHUNK_BODY_BUDGET_BYTES", 2_048)
    projector_calls = 0

    def forbidden_projector(*_args: object, **_kwargs: object) -> None:
        nonlocal projector_calls
        projector_calls += 1
        raise AssertionError("oversized dense source reached the exact projector")

    monkeypatch.setattr(
        archive_document_storage,
        "_project_focused_dense_source",
        forbidden_projector,
    )
    request = ArchiveSearchRequest.create(
        query="Smith",
        focus="role",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=10,
    )
    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(TENANT, request.dense_query, principal_id=OWNER)
    assert plan is not None
    snapshot = "focused-dense-failed-read-budget"
    binding = _dense_binding(request, snapshot)
    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    try:
        with storage.transaction() as conn:
            page = search_archive_document_lane(
                conn,
                tenant_id=TENANT,
                owner_id=OWNER,
                request=request,
                corpus=ArchiveSearchCorpus.DOCUMENTS,
                lane=SearchLane.DENSE,
                execution_binding=binding,
                snapshot_discriminator=snapshot,
                snapshot_current=True,
                dense_query_plan=plan,
            )
    finally:
        storage.conn.set_trace_callback(None)

    body_queries = [item for item in statements if "AS dense_chunk_body" in item]
    assert projector_calls == 0
    assert len(body_queries) == 4
    assert all("length(CAST(r.raw_content AS BLOB)) BETWEEN 1 AND 512" in item for item in body_queries)
    assert page.candidates == ()
    coverage = page.to_coverage(
        execution_binding=binding,
        tenant_id=TENANT,
        owner_id=OWNER,
        request=request,
        snapshot_discriminator=snapshot,
    )
    assert CoverageState.CAPPED in coverage.states
    assert CoverageState.UNAVAILABLE in coverage.states


@pytest.mark.parametrize("stored_sidecar", (False, True))
@pytest.mark.asyncio
async def test_focused_dense_selection_replays_as_exact_raw_evidence(
    stored_sidecar: bool,
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    expected = "Артемьев\nДолжность: ведущий инженер"
    body = expected + "\n\n" + "Техническое приложение к анкете. " * 24
    raw_id, _knowledge_id = _seed_document(
        storage,
        suffix="9304" if stored_sidecar else "9305",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
        body_override=body,
        passage_ready=stored_sidecar,
    )
    if stored_sidecar:
        backfill = storage.backfill_document_catalog(
            TENANT,
            after_raw_object_id=None,
            limit=10,
            include_document_passages=True,
        )
        assert backfill["passage_changed"] == 1
    conversation = storage.create_conversation(OWNER, "focused dense replay")
    boundary = storage.store_message(
        conversation["id"],
        OWNER,
        "user",
        "accepted focused source request",
    )
    request = ArchiveSearchRequest.create(
        query="Артемьева",
        focus="должность",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(
        TENANT,
        request.dense_query,
        principal_id=OWNER,
    )
    assert plan is not None
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    snapshot = f"focused-dense-replay-{stored_sidecar}"

    with storage.transaction() as conn:
        page = search_archive_document_lane(
            conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.DENSE,
            execution_binding=_dense_binding(request, snapshot),
            snapshot_discriminator=snapshot,
            snapshot_current=True,
            dense_query_plan=plan,
        )
        assert len(page.candidates) == 1
        candidate = page.candidates[0]
        assert candidate.resolved_source.source_ref.canonical_object_id == raw_id
        assert len(candidate.passages) == 1
        passage = candidate.passages[0]
        passage_ref = passage.passage_ref
        assert passage.excerpt == expected
        assert passage_ref.source_revision.kind is RevisionKind.RAW_CONTENT_SHA256
        assert passage_ref.embedding.compatibility is EmbeddingCompatibility.NOT_APPLICABLE
        assert passage_ref.passage_index_version == (
            DOCUMENT_STORED_PASSAGE_INDEX_VERSION if stored_sidecar else LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
        )
        selected_snapshot = archive_selected_evidence_snapshot_sha256(
            candidate.resolved_source,
            (passage_ref,),
            (passage.excerpt,),
        )
        exact = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=OWNER,
            origin_boundary_user_message_id=boundary["id"],
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=(passage_ref,),
            expected_source_snapshot_sha256=selected_snapshot,
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.PARTIAL,
        )
        assert exact.status is ArchiveEvidenceReplayStatus.EXACT
        assert exact.excerpts[0].text == expected

        locator = passage_ref.locator
        assert type(locator) is TextSpanLocator
        shifted = replace(
            passage_ref,
            locator=TextSpanLocator(
                locator.chunk_index,
                locator.start_char + 1,
                locator.end_char,
            ),
        )
        locator_drift = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=OWNER,
            origin_boundary_user_message_id=boundary["id"],
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=(shifted,),
            expected_source_snapshot_sha256=selected_snapshot,
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.PARTIAL,
        )
        assert locator_drift.status is ArchiveEvidenceReplayStatus.DRIFTED

        conn.execute("UPDATE raw_objects SET content_hash=? WHERE id=?", ("f" * 64, raw_id))
        revision_drift = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=OWNER,
            origin_boundary_user_message_id=boundary["id"],
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=(passage_ref,),
            expected_source_snapshot_sha256=selected_snapshot,
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.PARTIAL,
        )
        assert revision_drift.status is ArchiveEvidenceReplayStatus.DRIFTED


@pytest.mark.asyncio
async def test_focused_dense_noncanonical_passage_topology_stays_legacy(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    expected = "Сидоров\nДолжность: ведущий инженер"
    body = expected + "\n\n" + "Большое техническое приложение. " * 180
    raw_id, _knowledge_id = _seed_document(
        storage,
        suffix="9306",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
        body_override=body,
        passage_ready=True,
    )
    backfill = storage.backfill_document_catalog(
        TENANT,
        after_raw_object_id=None,
        limit=10,
        include_document_passages=True,
    )
    assert backfill["passage_changed"] == 1
    stored_rows = storage.conn.execute(
        """SELECT chunk_index,start_char,end_char,content_sha256
             FROM document_passages WHERE raw_object_id=? ORDER BY chunk_index""",
        (raw_id,),
    ).fetchall()
    assert len(stored_rows) >= 2
    first = stored_rows[0]
    second = stored_rows[1]
    replacement_end = max(int(first["start_char"]) + 1, int(second["start_char"]))
    assert replacement_end < int(first["end_char"])
    mutated_rows = [
        (
            int(row["chunk_index"]),
            int(row["start_char"]),
            replacement_end if index == 0 else int(row["end_char"]),
            (
                hashlib.sha256(body[int(row["start_char"]) : replacement_end].encode()).hexdigest()
                if index == 0
                else str(row["content_sha256"])
            ),
        )
        for index, row in enumerate(stored_rows)
    ]
    mutated_set_digest = document_passage_set_sha256(tuple(mutated_rows))
    with storage.transaction() as conn:
        conn.create_function(
            "friday_document_passage_projection_valid",
            14,
            lambda *_args: 1,
            deterministic=True,
        )
        conn.create_function(
            "friday_document_passage_span_valid",
            6,
            lambda *_args: 1,
            deterministic=True,
        )
        try:
            conn.execute(
                """UPDATE document_passages SET end_char=?,content_sha256=?
                     WHERE raw_object_id=? AND chunk_index=0""",
                (replacement_end, mutated_rows[0][3], raw_id),
            )
            conn.execute(
                """UPDATE document_passage_projections SET passage_set_sha256=?
                     WHERE raw_object_id=?""",
                (mutated_set_digest, raw_id),
            )
        finally:
            register_document_passage_connection_functions(conn)

    request = ArchiveSearchRequest.create(
        query="Сидорова",
        focus="должность",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(TENANT, request.dense_query, principal_id=OWNER)
    assert plan is not None
    snapshot = "focused-dense-noncanonical-sidecar"
    with storage.transaction() as conn:
        page = search_archive_document_lane(
            conn,
            tenant_id=TENANT,
            owner_id=OWNER,
            request=request,
            corpus=ArchiveSearchCorpus.DOCUMENTS,
            lane=SearchLane.DENSE,
            execution_binding=_dense_binding(request, snapshot),
            snapshot_discriminator=snapshot,
            snapshot_current=True,
            dense_query_plan=plan,
        )

    assert len(page.candidates) == 1
    passage = page.candidates[0].passages[0]
    assert passage.excerpt == expected
    assert passage.passage_ref.passage_index_version == LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION


@pytest.mark.asyncio
async def test_focused_dense_rejects_a_passage_with_a_remote_foreign_predicate(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(OWNER)
    body = (
        "Иванов\n\n"
        + "нейтральный раздел без кадровых сведений\n" * 30
        + "Петров\nДолжность: генеральный директор\n"
    )
    raw_id, _knowledge_id = _seed_document(
        storage,
        suffix="9302",
        owner=OWNER,
        concept_vector=[1.0, 0.0],
        body_override=body,
    )
    request = ArchiveSearchRequest.create(
        query="Иванов",
        focus="должность",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=5,
    )
    plan = await HybridSearcher(
        storage,
        _DeterministicEmbeddings(settings),
        dense_evidence_min=0.35,
    ).prepare_archive_dense_query_plan(
        TENANT,
        request.dense_query,
        principal_id=OWNER,
    )
    projection = dense_plan_module.project_archive_dense_query_plan(
        plan,
        principal_id=OWNER,
        query=request.dense_query,
    )
    assert projection is not None
    assert projection.candidates

    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=AuthorizationService(storage, shared_tenant=TENANT),
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator="focused-dense-remote-snapshot",
            run_discriminator="focused-dense-remote",
            turn_ledger=create_archive_model_batch_ledger(
                tenant_id=TENANT,
                principal_id=OWNER,
                turn_discriminator="turn-focused-dense-remote",
            ),
            dense_query_plan=plan,
        )

    payload = json.loads(prepared.authorized_batch.model_visible_canonical_bytes)
    dense = next(item for item in payload["coverage"] if item["lane"] == "dense")
    assert dense["matched_at_least"] == 0
    assert all(
        passage.passage_ref.embedding.compatibility is not EmbeddingCompatibility.CURRENT
        for result in prepared.authorized_batch._page.results  # noqa: SLF001
        if result.candidate.resolved_source.source_ref.canonical_object_id == raw_id
        for passage in result.candidate.passages
    )


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
