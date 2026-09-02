"""Hostile evidence for the authenticated exact-memory internal lane."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import pickle
import sqlite3
import time
from dataclasses import replace
from typing import Any

import pytest

import friday.retrieval.memory_exact_internal as memory_exact_internal
from friday.knowledge_graph import KnowledgeGraph
from friday.orchestration.contracts import RouterMode, TurnInput
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    TurnContextError,
    TurnContextIssuer,
    TurnMode,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval import EmbeddingBackend, HybridSearcher, best_snippet, pack_vector
from friday.retrieval.memory_exact_contract import (
    MEMORY_EXACT_MAX_GRAPH_NODES,
    MemoryExactContinuation,
    MemoryExactContractError,
    MemoryExactDateWindowStatus,
    MemoryExactGraphCoverage,
    MemoryExactGraphDirection,
    MemoryExactGraphEdgeProjection,
    MemoryExactGraphEvidenceBasis,
    MemoryExactGraphNodeProjection,
    MemoryExactGraphPathProjection,
    MemoryExactGraphProjection,
    MemoryExactGraphRelationProjection,
    MemoryExactLifecycleStage,
    MemoryExactPublicationStatus,
    MemoryExactRequest,
    _create_memory_exact_candidate,
)
from friday.retrieval.memory_exact_internal import (
    MEMORY_EXACT_SECURITY_IDS,
    MemoryExactInternalAdapter,
    MemoryExactInternalError,
    MemoryExactReadDenied,
)
from friday.storage import FridayStorage
from friday.storage._core import read_only_storage_snapshot
from friday.storage._memory_exact_internal import MemoryExactStorageDrift, MemoryExactStorageError
from friday.storage.models import (
    Entity,
    EntityType,
    FeedbackItem,
    FeedbackType,
    KnowledgeObject,
    RawObject,
    Relation,
    RelationType,
    new_id,
)
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

TENANT = "memory-exact-adversarial-tenant"
FOREIGN_TENANT = "memory-exact-adversarial-foreign-tenant"
PRINCIPAL = "memory-exact-adversarial-principal"
OTHER_PRINCIPAL = "memory-exact-adversarial-other-principal"
BASE_TIME = "2026-09-01T08:00:00+00:00"
VALID_TURN = f"turn_{'a' * 64}"
_PROVIDER_SOURCE_SQL_MARKERS = (
    "entity_merge_history",
    "entities",
    "entity_versions",
    "feedback_state",
    "inbox",
    "knowledge_chunk_embeddings",
    "knowledge_embeddings",
    "knowledge_entity_links",
    "knowledge_fts",
    "knowledge_objects",
    "private_entity_material_cache",
    "private_entity_material_cache_state",
    "private_entity_material_closure",
    "private_entity_material_derivative_cache",
    "private_entity_material_derivative_state",
    "raw_objects",
    "relation_candidates",
    "relation_revision_context",
    "relation_revisions",
    "relations",
    "schema_meta",
    "from users tenant",
)


def _turn(
    actor: ActorContext,
    *,
    label: str,
    clock_ns: list[int] | None = None,
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext]:
    now = time.monotonic_ns()
    clock = [now] if clock_ns is None else clock_ns
    clock[:] = [now]
    conversation_id = (
        "conv_" + hashlib.sha256(f"memory-exact-adversarial:{label}".encode("ascii")).hexdigest()[:16]
    )
    issuer = TurnContextIssuer(
        hashlib.sha256(f"memory-exact-adversarial:{label}".encode("ascii")).digest(),
        _monotonic_ns=lambda: clock[0],
    )
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"memory-exact-adversarial-ingress-{label}",
        actor=actor,
        conversation_id=conversation_id,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=f"memory-exact-adversarial-source-{label}",
        update_id=f"memory-exact-adversarial-update-{label}",
        request_effect_binding_sha256=hashlib.sha256(label.encode("ascii")).hexdigest(),
    )
    model_input = TurnInput.from_chat(
        message="hostile authenticated archive recall",
        actor=actor,
        conversation_id=conversation_id,
        attachments=(),
        enable_tools=True,
        synthetic_document_notice=False,
        mode=TurnMode.DIALOGUE.value,
        reply_to=None,
        quoted_attachment_reference=False,
        reply_assistant_reference=False,
    )
    policy = issuer.issue_turn_policy(
        router_mode=RouterMode.LEGACY,
        fallback_router_mode=None,
        decision=TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH),
    )
    context = issuer.authenticate_turn(
        authority=authority,
        model_input=model_input,
        authorized_sources=(issuer.accepted_ingress_source(authority),),
        turn_policy=policy,
        inherited_budget=InheritedTurnBudget(
            TurnSafetyDeadline(now + 60_000_000_000),
            ModelAntiLoopBudget(4, 1),
            TurnResourceBudget(4, 2, 2, 32_768),
        ),
        pending_work_admission=None,
    )
    return issuer, context


def _stack(
    storage: FridayStorage,
    *,
    label: str,
    principal: str = PRINCIPAL,
    tenant: str = TENANT,
    clock_ns: list[int] | None = None,
    embeddings: EmbeddingBackend | None = None,
) -> tuple[
    AuthorizationService,
    ActorContext,
    AuthenticatedTurnContext,
    HybridSearcher,
    MemoryExactInternalAdapter,
]:
    storage.ensure_user(tenant, preset_key="owner")
    storage.ensure_user(principal, preset_key="owner")
    authorization = AuthorizationService(storage, shared_tenant=tenant)
    actor = authorization.actor_for_user(principal, source="memory-exact-adversarial-test")
    issuer, context = _turn(actor, label=label, clock_ns=clock_ns)
    searcher = HybridSearcher(storage, embeddings, record_usage=False)
    adapter = MemoryExactInternalAdapter(
        authorization,
        issuer,
        storage,
        searcher,
        KnowledgeGraph(storage),
    )
    return authorization, actor, context, searcher, adapter


def _dense_stack(
    storage: FridayStorage,
    monkeypatch: pytest.MonkeyPatch,
    *,
    label: str,
) -> tuple[
    AuthorizationService,
    ActorContext,
    AuthenticatedTurnContext,
    HybridSearcher,
    MemoryExactInternalAdapter,
]:
    tuned = replace(
        storage.settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9/v1",
        embeddings_model="memory-exact-read-set",
        embeddings_dense_max_objects=10,
        embeddings_chunk_chars=1_200,
        embeddings_resident_cache=False,
    )
    storage.settings = tuned

    async def fixed_embed(
        _backend: EmbeddingBackend,
        texts: list[str],
        **_kwargs: object,
    ) -> list[list[float]]:
        return [[1.0, 0.0] for _text in texts]

    monkeypatch.setattr(EmbeddingBackend, "embed", fixed_embed)
    return _stack(
        storage,
        label=label,
        embeddings=EmbeddingBackend(tuned),
    )


def _request(
    actor: ActorContext,
    context: AuthenticatedTurnContext,
    query: str,
    **overrides: Any,
) -> MemoryExactRequest:
    values: dict[str, Any] = {
        "tenant_id": actor.user_id,
        "principal_id": actor.own_id,
        "active_turn_id": context.turn_id,
        "query": query,
        "page_size": 10,
        "snapshot_limit": 10,
    }
    values.update(overrides)
    return MemoryExactRequest.create(**values)


async def _refresh_and_consume(
    storage: FridayStorage,
    adapter: MemoryExactInternalAdapter,
    *,
    context: AuthenticatedTurnContext,
    page: Any,
    decision: Any,
) -> bool:
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    with storage.transaction() as conn:
        return adapter.consume_publication_authority_in_transaction(
            conn,
            context=context,
            page=page,
            decision=decision,
            refresh=refresh,
        )


def _seed(
    storage: FridayStorage,
    *,
    suffix: str,
    query: str,
    tenant: str = TENANT,
    body: str | None = None,
    knowledge_id: str | None = None,
) -> tuple[RawObject, KnowledgeObject]:
    storage.ensure_user(tenant, preset_key="owner")
    content = body if body is not None else f"{query} exact hostile evidence {suffix}"
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source="test",
        source_ref=f"memory-exact-hostile-ref-{suffix}",
        raw_content=content,
        content_type="text",
        metadata_json={"source_label": suffix},
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        received_at=BASE_TIME,
        created_at=BASE_TIME,
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=knowledge_id or new_id("ko"),
        user_id=tenant,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=f"Hostile {suffix}",
        knowledge_kind="document",
        importance=0.8,
        quality_score=0.8,
        promotion_score=0.8,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    storage.store_knowledge_object(knowledge)
    return raw, knowledge


def _seed_graph_cards(storage: FridayStorage, *, count: int, label: str) -> list[Entity]:
    entities = [
        Entity(
            new_id("ent"),
            TENANT,
            f"{label} Graph Card {index:03d}",
            EntityType.CONCEPT,
        )
        for index in range(count)
    ]
    for entity in entities:
        storage.create_entity(entity)
    return entities


def _seed_known_at_graph(
    storage: FridayStorage,
    *,
    suffix: str,
) -> tuple[KnowledgeObject, Entity, Entity, Relation, str, str]:
    query = "как связан HistoricalAlphaR8E с HistoricalBetaR8E"
    _raw, knowledge = _seed(
        storage,
        suffix=suffix,
        query="R8EACTIVEHISTORY",
        body=f"R8EACTIVEHISTORY: {query}",
    )
    alpha = Entity(new_id("ent"), TENANT, "HistoricalAlphaR8E", EntityType.PERSON)
    beta = Entity(
        new_id("ent"),
        TENANT,
        "HistoricalBetaR8E",
        EntityType.ORGANIZATION,
    )
    for entity in (alpha, beta):
        storage.create_entity(entity)
        storage.link_knowledge_entity(
            TENANT,
            knowledge.id,
            entity.id,
            status="accepted",
            evidence={"basis": "known-at-active-transaction"},
            reviewed_by=PRINCIPAL,
        )
    relation = Relation(
        new_id("rel"),
        TENANT,
        alpha.id,
        beta.id,
        RelationType.RELATED_TO,
        weight=0.8,
        valid_from="2020-01-01",
        metadata_json={"evidence": {"knowledge_object_id": knowledge.id}},
    )
    storage.create_relation(relation)
    revision = storage.execute(
        """SELECT recorded_at FROM relation_revisions
             WHERE relation_id=? ORDER BY event_seq DESC LIMIT 1""",
        (relation.id,),
    ).fetchone()
    assert revision is not None
    known_at = str(revision[0])
    storage.invalidate_relation(
        TENANT,
        relation.id,
        valid_to="2023-01-01",
        reason="known-at active transaction fixture",
    )
    return knowledge, alpha, beta, relation, query, known_at


def _seed_merged_known_at_graph(
    storage: FridayStorage,
    *,
    suffix: str,
) -> tuple[Entity, Entity, Entity, str, str]:
    query = "как связан MergeBoundAlphaR8E с MergeBoundBetaR8E"
    _raw, knowledge = _seed(
        storage,
        suffix=suffix,
        query="R8EMERGEBOUNDHISTORY",
        body=f"R8EMERGEBOUNDHISTORY: {query}",
    )
    alpha = Entity(new_id("ent"), TENANT, "MergeBoundAlphaR8E", EntityType.PERSON)
    beta = Entity(
        new_id("ent"),
        TENANT,
        "MergeBoundBetaR8E",
        EntityType.ORGANIZATION,
    )
    merged = Entity(
        new_id("ent"),
        TENANT,
        "MergeBoundLegacyBetaR8E",
        EntityType.ORGANIZATION,
    )
    for entity in (alpha, beta, merged):
        storage.create_entity(entity)
    for entity in (alpha, beta):
        storage.link_knowledge_entity(
            TENANT,
            knowledge.id,
            entity.id,
            status="accepted",
            evidence={"basis": "merged-known-at-bound"},
            reviewed_by=PRINCIPAL,
        )
    relation = Relation(
        new_id("rel"),
        TENANT,
        alpha.id,
        merged.id,
        RelationType.RELATED_TO,
        weight=0.8,
        valid_from="2020-01-01",
        metadata_json={"evidence": {"knowledge_object_id": knowledge.id}},
    )
    storage.create_relation(relation)
    storage.merge_entities(TENANT, merged.id, beta.id, merged_by=TENANT)
    revision = storage.execute(
        """SELECT recorded_at FROM relation_revisions
             WHERE relation_id=? ORDER BY event_seq DESC LIMIT 1""",
        (relation.id,),
    ).fetchone()
    assert revision is not None
    return alpha, beta, merged, query, str(revision[0])


def test_observer_open_failure_exposes_only_the_fixed_public_error(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(TENANT, preset_key="owner")
    storage.ensure_user(PRINCIPAL, preset_key="owner")
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    issuer, _context = _turn(
        authorization.actor_for_user(
            PRINCIPAL,
            source="memory-exact-adversarial-test",
        ),
        label="observer-open-failure",
    )
    private_body = f"{storage.settings.database_path}:observer-private-body"

    def fail_observer_open(_source: sqlite3.Connection) -> sqlite3.Connection:
        raise memory_exact_internal._ProviderSnapshotInvalid(private_body)  # noqa: SLF001

    monkeypatch.setattr(
        memory_exact_internal,
        "_open_main_database_observer",
        fail_observer_open,
    )
    with pytest.raises(MemoryExactInternalError) as captured:
        MemoryExactInternalAdapter(
            authorization,
            issuer,
            storage,
            HybridSearcher(storage, record_usage=False),
            KnowledgeGraph(storage),
        )
    assert str(captured.value) == "memory-exact provider connection is unavailable"
    assert private_body not in str(captured.value)
    assert str(storage.settings.database_path) not in str(captured.value)


@pytest.mark.parametrize(
    "overrides",
    (
        {"query": " \n\t "},
        {"query": "x" * 701},
        {"active_turn_id": "turn_not_code_owned"},
        {"page_size": 0},
        {"page_size": 2, "snapshot_limit": 1},
        {"snapshot_limit": 51},
        {"since": "2025-01-02", "until": "2025-01-01"},
        {"known_at": "2026-09-01T08:00:00"},
        {"known_at": "2999-01-01T00:00:00Z"},
        {
            "lifecycle_stages": (
                MemoryExactLifecycleStage.ACTIVE,
                MemoryExactLifecycleStage.ACTIVE,
            )
        },
        {"lifecycle_stages": ()},
    ),
    ids=(
        "empty-query",
        "oversized-query",
        "malformed-turn",
        "zero-page",
        "page-over-snapshot",
        "oversized-snapshot",
        "reversed-window",
        "naive-known-at",
        "future-known-at",
        "duplicate-lifecycle",
        "empty-lifecycle",
    ),
)
def test_closed_request_rejects_malformed_and_oversized_selection(overrides: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "tenant_id": TENANT,
        "principal_id": PRINCIPAL,
        "active_turn_id": VALID_TURN,
        "query": "R8ECLOSEDQUERY",
        "page_size": 10,
        "snapshot_limit": 10,
    }
    values.update(overrides)
    with pytest.raises(MemoryExactContractError):
        MemoryExactRequest.create(**values)


def test_continuation_contract_matches_the_signed_base64url_alphabet() -> None:
    with pytest.raises(MemoryExactContractError):
        MemoryExactContinuation.create(f"{'A' * 32}.foreign-segment")


@pytest.mark.parametrize(
    "partial",
    ("2024", "2024-02", "02.2024"),
    ids=("year", "year-month", "month-year"),
)
def test_as_of_rejects_partial_dates_instead_of_inventing_a_day(partial: str) -> None:
    with pytest.raises(MemoryExactContractError):
        MemoryExactRequest.create(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            active_turn_id=VALID_TURN,
            query="R8EASOFPARTIAL",
            as_of=partial,
        )


@pytest.mark.parametrize(
    "corruption",
    ("duplicate", "nonfinite", "extra", "noncanonical", "wrong-schema"),
)
def test_private_request_parser_rejects_ambiguous_or_open_json(corruption: str) -> None:
    request = MemoryExactRequest.create(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        active_turn_id=VALID_TURN,
        query="R8ECANONICALQUERY",
    )
    payload = request.to_private_payload()
    canonical = request.to_private_json()
    if corruption == "duplicate":
        malformed = canonical[:-1] + ',"query":"R8ECANONICALQUERY"}'
    elif corruption == "nonfinite":
        malformed = canonical.replace('"page_size":10', '"page_size":NaN')
    elif corruption == "extra":
        payload["private_path"] = "/home/owner/secret"
        malformed = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    elif corruption == "noncanonical":
        malformed = f" {canonical}"
    else:
        payload["schema"] = "friday.memory-exact-request.private.v999"
        malformed = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    with pytest.raises(MemoryExactContractError):
        MemoryExactRequest.parse_private(malformed)


def test_process_private_candidate_discards_body_and_redacts_identity() -> None:
    query = "R8EPRIVATEQUERY"
    tail = "FULL-BODY-TAIL-CANARY"
    body = f"{query} " + ("x" * 900) + tail
    request = MemoryExactRequest.create(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        active_turn_id=VALID_TURN,
        query=query,
    )
    candidate = _create_memory_exact_candidate(
        request=request,
        knowledge_id="ko_private_candidate",
        raw_object_id="raw_private_candidate",
        source_handle="1" * 64,
        knowledge_revision_sha256="2" * 64,
        raw_revision_sha256="3" * 64,
        title="Private candidate",
        knowledge_kind="document",
        lifecycle_stage=MemoryExactLifecycleStage.ACTIVE,
        updated_at="2026-09-01T08:00:00+00:00",
        body=body,
    )

    assert not hasattr(candidate, "body")
    assert tail not in candidate.excerpt
    assert candidate.excerpt == best_snippet(query, body, max_chars=600)[:600]
    assert len(candidate.excerpt) <= 600
    assert candidate.content_chars == len(body)
    assert candidate.body_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()
    redacted = repr(candidate)
    for private in (query, tail, TENANT, PRINCIPAL, candidate.knowledge_id, candidate.raw_object_id):
        assert private not in redacted
    for private in (query, tail, candidate.knowledge_id, candidate.raw_object_id):
        assert private not in request.to_identity_json()
    with pytest.raises(TypeError):
        copy.copy(candidate)
    with pytest.raises(TypeError):
        pickle.dumps(candidate)


def test_bounded_graph_projection_reports_honest_truncation_without_raw_ids() -> None:
    nodes = (
        MemoryExactGraphNodeProjection(1, "Alpha", "person"),
        MemoryExactGraphNodeProjection(2, "Beta", "organization"),
    )
    relation = MemoryExactGraphRelationProjection(1, 1, 2, "member_of")
    edge = MemoryExactGraphEdgeProjection(
        1,
        1,
        2,
        "member_of",
        MemoryExactGraphDirection.FORWARD,
    )
    graph = MemoryExactGraphProjection(
        effective_query="graph query",
        nodes=nodes,
        relations=(relation,),
        paths=(MemoryExactGraphPathProjection(1, (edge,)),),
        nodes_matched_at_least=7,
        relations_matched_at_least=4,
        paths_matched_at_least=3,
        nodes_coverage=MemoryExactGraphCoverage.PARTIAL,
        relations_coverage=MemoryExactGraphCoverage.PARTIAL,
        paths_coverage=MemoryExactGraphCoverage.PARTIAL,
        expanded=True,
    )
    payload = graph.to_model_payload()

    assert payload["nodes_truncated"] is True
    assert payload["relations_truncated"] is True
    assert payload["paths_truncated"] is True
    assert payload["nodes_shown"] == 2
    assert payload["nodes_matched_at_least"] == 7
    assert payload["relations"][0]["evidence_basis"] == "relation_row_only"
    assert payload["relations"][0]["implicit"] is False
    assert payload["paths"][0]["grounded"] is False
    encoded = graph.to_model_json()
    assert '"alias":"n1"' in encoded
    assert "entity-database-id" not in encoded
    assert "relation-database-id" not in encoded


def test_graph_evidence_authority_is_closed_id_free_and_conservatively_grounded() -> None:
    reviewed = MemoryExactGraphEdgeProjection(
        1,
        1,
        2,
        "member_of",
        MemoryExactGraphDirection.FORWARD,
        evidence_basis=MemoryExactGraphEvidenceBasis.REVIEWED_RELATION,
        evidence_result_ordinal=1,
    )
    accepted_links = MemoryExactGraphEdgeProjection(
        2,
        2,
        3,
        "co_occurs_in",
        MemoryExactGraphDirection.FORWARD,
        implicit=True,
        evidence_basis=MemoryExactGraphEvidenceBasis.ACCEPTED_LINKS,
        evidence_result_ordinal=2,
    )
    path = MemoryExactGraphPathProjection(1, (reviewed, accepted_links))

    assert path.grounded is True
    payload = path.to_model_payload()
    assert payload["grounded"] is True
    assert payload["edges"][0]["evidence_basis"] == "reviewed_relation"
    assert payload["edges"][1]["implicit"] is True
    assert "knowledge" not in json.dumps(payload, sort_keys=True)

    with pytest.raises(MemoryExactContractError, match="contradicts"):
        MemoryExactGraphRelationProjection(
            1,
            1,
            2,
            "co_occurs_in",
            implicit=True,
        )
    with pytest.raises(MemoryExactContractError, match="unreviewed"):
        MemoryExactGraphEdgeProjection(
            1,
            1,
            2,
            "related_to",
            MemoryExactGraphDirection.FORWARD,
            evidence_result_ordinal=1,
        )


def test_date_window_status_rejects_invented_or_ungrounded_emptiness() -> None:
    with pytest.raises(MemoryExactContractError, match="invented"):
        MemoryExactDateWindowStatus(None, None, applied=True, empty=False)
    with pytest.raises(MemoryExactContractError, match="must have been applied"):
        MemoryExactDateWindowStatus("2025-01-01", None, applied=False, empty=True)


def test_saturated_graph_without_upstream_counts_reports_unknown_coverage() -> None:
    nodes = tuple(
        MemoryExactGraphNodeProjection(index, f"Node {index}", "other")
        for index in range(1, MEMORY_EXACT_MAX_GRAPH_NODES + 1)
    )
    graph = MemoryExactGraphProjection(
        effective_query="graph query",
        nodes=nodes,
        nodes_matched_at_least=len(nodes),
        nodes_coverage=MemoryExactGraphCoverage.UNKNOWN,
        expanded=True,
    )

    payload = graph.to_model_payload()
    assert payload["nodes_coverage"] == "unknown"
    assert payload["nodes_truncated"] is None


def test_graph_projection_rejects_understated_missing_or_oversized_sources() -> None:
    nodes = (
        MemoryExactGraphNodeProjection(1, "Alpha", "person"),
        MemoryExactGraphNodeProjection(2, "Beta", "organization"),
    )
    relation = MemoryExactGraphRelationProjection(1, 1, 2, "member_of")
    with pytest.raises(MemoryExactContractError, match="understates"):
        MemoryExactGraphProjection(
            effective_query="graph query",
            nodes=nodes,
            relations=(relation,),
            nodes_matched_at_least=2,
            relations_matched_at_least=0,
        )
    with pytest.raises(MemoryExactContractError, match="absent node"):
        MemoryExactGraphProjection(
            effective_query="graph query",
            nodes=(nodes[0],),
            relations=(relation,),
            nodes_matched_at_least=1,
            relations_matched_at_least=1,
        )
    oversized = tuple(
        MemoryExactGraphNodeProjection(index, f"Node {index}", "other")
        for index in range(1, MEMORY_EXACT_MAX_GRAPH_NODES + 1)
    ) + (MemoryExactGraphNodeProjection(1, "Duplicate overflow", "other"),)
    with pytest.raises(MemoryExactContractError, match="exceed"):
        MemoryExactGraphProjection(
            effective_query="graph query",
            nodes=oversized,
            nodes_matched_at_least=len(oversized),
        )


async def test_cross_tenant_or_principal_context_is_refused_before_provider(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization, actor, context, searcher, adapter = _stack(
        storage,
        label="cross-scope",
    )
    called = False

    async def forbidden_provider(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        raise AssertionError("provider ran before authenticated scope refusal")

    monkeypatch.setattr(HybridSearcher, "search", forbidden_provider)
    for request in (
        _request(actor, context, "R8ECROSSSCOPE", tenant_id=FOREIGN_TENANT),
        _request(actor, context, "R8ECROSSSCOPE", principal_id=OTHER_PRINCIPAL),
        _request(actor, context, "R8ECROSSSCOPE", active_turn_id=f"turn_{'b' * 64}"),
    ):
        with pytest.raises(MemoryExactInternalError, match="authenticated turn"):
            await adapter.prepare(context=context, request=request)
    assert called is False


@pytest.mark.parametrize("security_id", ("search.use", "knowledge.read"))
async def test_read_authority_is_fresh_and_precedes_provider(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    security_id: str,
) -> None:
    _seed(storage, suffix=f"read-deny-{security_id}", query="R8EREADDENY")
    authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"read-deny-{security_id}",
    )
    authorization.deny_permission(actor.own_id, security_id)
    called = False

    async def forbidden_provider(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        raise AssertionError("provider ran after transactional denial")

    monkeypatch.setattr(HybridSearcher, "search", forbidden_provider)
    with pytest.raises(MemoryExactReadDenied, match="authorization denied"):
        await adapter.prepare(
            context=context,
            request=_request(actor, context, "R8EREADDENY"),
        )
    assert called is False


@pytest.mark.parametrize("security_id", ("search.use", "knowledge.read"))
async def test_projection_reauthorizes_after_prepare(
    storage: Any,
    security_id: str,
) -> None:
    _seed(storage, suffix=f"projection-revoke-{security_id}", query="R8EPROJECTIONREVOKE")
    authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"projection-revoke-{security_id}",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8EPROJECTIONREVOKE"),
    )
    authorization.deny_permission(actor.own_id, security_id)
    with pytest.raises(MemoryExactReadDenied, match="projection authorization denied"):
        adapter.project_for_model(context=context, page=page)


@pytest.mark.parametrize(
    "denied_security_id",
    (None, "search.use", "knowledge.read"),
    ids=("authorized", "search-denied", "knowledge-denied"),
)
async def test_provider_time_revocation_precedes_every_exact_source_read(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    denied_security_id: str | None,
) -> None:
    label = denied_security_id or "authorized"
    _seed(storage, suffix=f"provider-revoke-{label}", query="R8EPROVIDERREVOKE")
    authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"provider-revoke-{label}",
    )
    events: list[tuple[str, str]] = []
    after_provider_return = False
    released_search = HybridSearcher.search

    def trace_sql(statement: str) -> None:
        if not after_provider_return:
            return
        normalized = " ".join(statement.casefold().split())
        if normalized.startswith(("begin", "savepoint")):
            events.append(("begin", ""))
        elif normalized.startswith(("commit", "end", "rollback", "release")):
            events.append(("end", ""))
        elif "from user_permission_overrides" in normalized:
            for security_id in MEMORY_EXACT_SECURITY_IDS:
                if security_id in normalized:
                    events.append(("authorization", security_id))
                    break
        elif normalized.startswith(("select", "with")):
            markers = ",".join(
                candidate
                for candidate in _PROVIDER_SOURCE_SQL_MARKERS
                if candidate in normalized
            )
            if markers:
                events.append(("source", markers))

    async def revoke_before_return(
        provider: HybridSearcher,
        *args: object,
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal after_provider_return
        result = await released_search(provider, *args, **kwargs)
        # Force the valid-local-ID closure path: the ranked identity must be
        # loaded only after the post-provider authorization pair, not merely
        # re-used from the provider's earlier materialization.
        envelope = provider.storage._envelope  # noqa: SLF001
        with envelope._lock:  # noqa: SLF001
            envelope._revisions.clear()  # noqa: SLF001
        if denied_security_id is not None:
            authorization.deny_permission(actor.own_id, denied_security_id)
        after_provider_return = True
        return result

    monkeypatch.setattr(HybridSearcher, "search", revoke_before_return)
    storage.conn.set_trace_callback(trace_sql)
    try:
        if denied_security_id is None:
            page = await adapter.prepare(
                context=context,
                request=_request(actor, context, "R8EPROVIDERREVOKE"),
            )
            assert page.candidates
        else:
            with pytest.raises(MemoryExactReadDenied, match="authorization changed"):
                await adapter.prepare(
                    context=context,
                    request=_request(actor, context, "R8EPROVIDERREVOKE"),
                )
    finally:
        storage.conn.set_trace_callback(None)

    transaction_events: list[list[tuple[str, str]]] = []
    active: list[tuple[str, str]] | None = None
    for event in events:
        kind, _value = event
        if kind == "begin":
            assert active is None
            active = []
        elif kind == "end":
            assert active is not None
            transaction_events.append(active)
            active = None
        else:
            assert active is not None
            active.append(event)
    assert active is None

    source_queries = 0
    for transaction in transaction_events:
        authorizations: list[str] = []
        for kind, value in transaction:
            if kind == "authorization":
                authorizations.append(value)
            elif kind == "source":
                source_queries += 1
                assert authorizations == list(MEMORY_EXACT_SECURITY_IDS)
    if denied_security_id is None:
        assert source_queries > 0
        first_source_markers = {
            marker
            for kind, value in transaction_events[0]
            if kind == "source"
            for marker in value.split(",")
        }
        assert {"knowledge_objects", "raw_objects"} <= first_source_markers
    else:
        assert source_queries == 0
        expected = list(MEMORY_EXACT_SECURITY_IDS)
        stop = expected.index(denied_security_id) + 1
        assert [value for kind, value in events if kind == "authorization"] == expected[:stop]


@pytest.mark.parametrize("edge", ("prepare", "projection", "publication"))
async def test_inherited_turn_deadline_closes_every_model_and_publication_edge(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    edge: str,
) -> None:
    _seed(storage, suffix=f"deadline-{edge}", query="R8EDEADLINE")
    clock_ns: list[int] = []
    _authorization, actor, context, searcher, adapter = _stack(
        storage,
        label=f"deadline-{edge}",
        clock_ns=clock_ns,
    )
    request = _request(actor, context, "R8EDEADLINE")
    page = None
    if edge in {"projection", "publication"}:
        page = await adapter.prepare(context=context, request=request)
    if edge == "projection":
        assert page is not None
        clock_ns[0] += 61_000_000_000
        with pytest.raises(TurnContextError, match="deadline"):
            adapter.project_for_model(context=context, page=page)
        return
    released_search = searcher.search

    async def crosses_deadline(
        _provider: HybridSearcher,
        *args: object,
        **kwargs: Any,
    ) -> dict[str, Any]:
        result = await released_search(*args, **kwargs)
        clock_ns[0] += 61_000_000_000
        return result

    monkeypatch.setattr(HybridSearcher, "search", crosses_deadline)
    if edge == "prepare":
        with pytest.raises(TurnContextError, match="deadline"):
            await adapter.prepare(context=context, request=request)
    else:
        assert page is not None
        decision = await adapter.reauthorize_for_publication(context=context, page=page)
        assert decision.status is MemoryExactPublicationStatus.UNAVAILABLE
        assert decision.authorizes(page) is False


@pytest.mark.parametrize("security_id", ("search.use", "knowledge.read"))
async def test_revocation_denies_late_publication_without_consuming_page(
    storage: Any,
    security_id: str,
) -> None:
    _seed(storage, suffix=f"late-deny-{security_id}", query="R8ELATEDENY")
    authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"late-deny-{security_id}",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8ELATEDENY"),
    )
    authorization.deny_permission(actor.own_id, security_id)
    decision = await adapter.reauthorize_for_publication(context=context, page=page)

    assert decision.status is MemoryExactPublicationStatus.DENIED
    assert decision.authorizes(page) is False
    assert decision.to_public_payload()["authorized"] is False


async def test_publication_receipt_cannot_outlive_deadline_or_later_revocation(
    storage: Any,
) -> None:
    _seed(storage, suffix="receipt-live-edge", query="R8ERECEIPTLIVE")
    clock_ns: list[int] = []
    authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="receipt-live-edge",
        clock_ns=clock_ns,
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8ERECEIPTLIVE"),
    )
    expired = await adapter.reauthorize_for_publication(context=context, page=page)
    assert expired.status is MemoryExactPublicationStatus.AUTHORIZED
    expired_refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=expired,
    )
    clock_ns[0] += 61_000_000_000
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=expired,
                refresh=expired_refresh,
            )
            is False
        )
    assert expired.authorizes(page) is False
    assert expired.to_public_payload()["authorized"] is False

    _issuer, live_context = _turn(actor, label="receipt-live-edge-revoked")
    live_request = _request(actor, live_context, "R8ERECEIPTLIVE")
    live_adapter = MemoryExactInternalAdapter(
        authorization,
        _issuer,
        storage,
        HybridSearcher(storage, record_usage=False),
        KnowledgeGraph(storage),
    )
    live_page = await live_adapter.prepare(context=live_context, request=live_request)
    revoked = await live_adapter.reauthorize_for_publication(
        context=live_context,
        page=live_page,
    )
    authorization.deny_permission(actor.own_id, "knowledge.read")
    assert (
        await _refresh_and_consume(
            storage,
            live_adapter,
            context=live_context,
            page=live_page,
            decision=revoked,
        )
        is False
    )
    authorization.grant_permission(actor.own_id, "knowledge.read")
    assert (
        await _refresh_and_consume(
            storage,
            live_adapter,
            context=live_context,
            page=live_page,
            decision=revoked,
        )
        is False
    )


async def test_publication_claim_is_single_consumer_and_provider_failure_burns_it(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(storage, suffix="receipt-single", query="R8ERECEIPTSINGLE")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="receipt-single",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8ERECEIPTSINGLE"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    outcomes = await asyncio.gather(
        _refresh_and_consume(
            storage,
            adapter,
            context=context,
            page=page,
            decision=decision,
        ),
        _refresh_and_consume(
            storage,
            adapter,
            context=context,
            page=page,
            decision=decision,
        ),
    )
    assert sorted(outcomes) == [False, True]

    released_search = HybridSearcher.search
    failed = await adapter.reauthorize_for_publication(context=context, page=page)

    async def unavailable(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("private provider failure")

    monkeypatch.setattr(HybridSearcher, "search", unavailable)
    assert (
        await _refresh_and_consume(
            storage,
            adapter,
            context=context,
            page=page,
            decision=failed,
        )
        is False
    )
    assert failed.to_public_payload()["authorized"] is False
    assert failed.authorizes(page) is False

    monkeypatch.setattr(HybridSearcher, "search", released_search)
    cancelled = await adapter.reauthorize_for_publication(context=context, page=page)
    started = asyncio.Event()

    async def blocked(*_args: object, **_kwargs: object) -> dict[str, Any]:
        started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    monkeypatch.setattr(HybridSearcher, "search", blocked)
    task = asyncio.create_task(
        adapter.refresh_publication_authority(
            context=context,
            page=page,
            decision=cancelled,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.to_public_payload()["authorized"] is True
    monkeypatch.setattr(HybridSearcher, "search", released_search)
    assert (
        await _refresh_and_consume(
            storage,
            adapter,
            context=context,
            page=page,
            decision=cancelled,
        )
        is True
    )


async def test_publication_refresh_refuses_an_active_transaction_before_provider(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(storage, suffix="refresh-transaction", query="R8EREFRESHTRANSACTION")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="refresh-transaction",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8EREFRESHTRANSACTION"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    called = False

    async def forbidden_provider(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        raise AssertionError("provider ran inside the publication transaction")

    monkeypatch.setattr(HybridSearcher, "search", forbidden_provider)
    with (
        storage.transaction(),
        pytest.raises(
            MemoryExactInternalError,
            match="no active publication transaction",
        ),
    ):
        await adapter.refresh_publication_authority(
            context=context,
            page=page,
            decision=decision,
        )
    assert called is False


async def test_wrong_page_does_not_burn_receipt_and_denied_receipt_never_upgrades(
    storage: Any,
) -> None:
    _seed(storage, suffix="receipt-page-a", query="R8ERECEIPTPAGEA")
    _seed(storage, suffix="receipt-page-b", query="R8ERECEIPTPAGEB")
    authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="receipt-page-binding",
    )
    page_a = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8ERECEIPTPAGEA"),
    )
    page_b = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8ERECEIPTPAGEB"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page_a)
    assert (
        await _refresh_and_consume(
            storage,
            adapter,
            context=context,
            page=page_b,
            decision=decision,
        )
        is False
    )
    assert decision.to_public_payload()["authorized"] is True
    assert (
        await _refresh_and_consume(
            storage,
            adapter,
            context=context,
            page=page_a,
            decision=decision,
        )
        is True
    )

    authorization.deny_permission(actor.own_id, "knowledge.read")
    denied = await adapter.reauthorize_for_publication(context=context, page=page_a)
    assert denied.status is MemoryExactPublicationStatus.DENIED
    authorization.grant_permission(actor.own_id, "knowledge.read")
    assert (
        await _refresh_and_consume(
            storage,
            adapter,
            context=context,
            page=page_a,
            decision=denied,
        )
        is False
    )
    assert denied.to_public_payload()["authorized"] is False


@pytest.mark.parametrize("foreign_scope", ("turn", "person"))
async def test_wrong_context_burns_an_exact_page_receipt(
    storage: Any,
    foreign_scope: str,
) -> None:
    _seed(storage, suffix=f"receipt-wrong-{foreign_scope}", query="R8EWRONGCONTEXT")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"receipt-wrong-{foreign_scope}",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8EWRONGCONTEXT"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    if foreign_scope == "turn":
        _foreign_issuer, foreign_context = _turn(actor, label="receipt-foreign-turn")
    else:
        storage.ensure_user(OTHER_PRINCIPAL, preset_key="owner")
        other_actor = AuthorizationService(storage, shared_tenant=TENANT).actor_for_user(
            OTHER_PRINCIPAL,
            source="memory-exact-wrong-person",
        )
        _foreign_issuer, foreign_context = _turn(other_actor, label="receipt-foreign-person")
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=foreign_context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.to_public_payload()["authorized"] is False
    assert (
        await _refresh_and_consume(
            storage,
            adapter,
            context=context,
            page=page,
            decision=decision,
        )
        is False
    )


async def test_publication_transaction_rollback_keeps_receipt_burned(storage: Any) -> None:
    _seed(storage, suffix="receipt-rollback", query="R8ERECEIPTROLLBACK")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="receipt-rollback",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8ERECEIPTROLLBACK"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    before = storage.execute("SELECT display_name FROM users WHERE id=?", (TENANT,)).fetchone()[0]
    with pytest.raises(RuntimeError, match="publication CAS failed"), storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is True
        )
        conn.execute("UPDATE users SET display_name=? WHERE id=?", ("should-roll-back", TENANT))
        raise RuntimeError("publication CAS failed")
    after = storage.execute("SELECT display_name FROM users WHERE id=?", (TENANT,)).fetchone()[0]
    assert after == before
    assert decision.to_public_payload()["authorized"] is False


async def test_provider_order_change_after_authorized_decision_is_burned(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(2):
        _seed(storage, suffix=f"provider-order-{index}", query="R8EPROVIDERORDER")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="provider-order",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(
            actor,
            context,
            "R8EPROVIDERORDER",
            page_size=2,
            snapshot_limit=2,
        ),
    )
    assert len(page.candidates) == 2
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    released_search = HybridSearcher.search

    async def reordered(
        provider: HybridSearcher,
        *args: object,
        **kwargs: Any,
    ) -> dict[str, Any]:
        result = await released_search(provider, *args, **kwargs)
        result["results"] = list(reversed(result["results"]))
        return result

    monkeypatch.setattr(HybridSearcher, "search", reordered)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.DRIFTED
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.to_public_payload()["authorized"] is False


async def test_feedback_order_change_after_authorized_refresh_is_burned(
    storage: Any,
    request: pytest.FixtureRequest,
) -> None:
    knowledge_ids: list[str] = []
    for index in range(2):
        _raw, knowledge = _seed(
            storage,
            suffix=f"feedback-order-{index}",
            query="R8EFEEDBACKORDER",
            body="R8EFEEDBACKORDER identical ranking evidence",
        )
        knowledge_ids.append(knowledge.id)
    storage.execute(
        "UPDATE knowledge_objects SET title=? WHERE id IN (?,?)",
        ("Identical feedback-order title", *knowledge_ids),
    )
    storage.commit()
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="feedback-order",
    )
    writer = FridayStorage(storage.settings)
    request.addfinalizer(writer.close)
    assert writer.get_user(TENANT) is not None
    turn_request = _request(
        actor,
        context,
        "R8EFEEDBACKORDER",
        page_size=2,
        snapshot_limit=2,
    )
    page = await adapter.prepare(context=context, request=turn_request)
    before = tuple(candidate.knowledge_id for candidate in page.candidates)
    assert len(before) == 2
    assert set(before) == set(knowledge_ids)
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is True
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is True

    for target_id, score in zip(before, (-1.0, 1.0), strict=True):
        writer.store_feedback(
            FeedbackItem(
                id=new_id("feedback"),
                user_id=TENANT,
                target_type="knowledge_object",
                target_id=target_id,
                feedback_type=FeedbackType.SEARCH_QUALITY,
                score=score,
            )
        )
    changed = await adapter.prepare(context=context, request=turn_request)
    assert tuple(candidate.knowledge_id for candidate in changed.candidates) == tuple(
        reversed(before)
    )
    assert decision.authorized is True

    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is False
    assert decision.to_public_payload()["authorized"] is False
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )


async def test_provider_dependency_ledger_reuses_one_source_thread_epoch(
    storage: Any,
) -> None:
    _seed(storage, suffix="dependency-thread", query="R8EDEPENDENCYTHREAD")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="dependency-thread",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8EDEPENDENCYTHREAD"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    source_conn = storage.conn
    observer = adapter._dependency_observer  # noqa: SLF001
    observer_lock = adapter._dependency_observer_lock  # noqa: SLF001
    observer_generation = adapter._dependency_observer_generation  # noqa: SLF001
    assert type(observer) is sqlite3.Connection
    assert observer is not source_conn
    assert observer_generation == storage._generation  # noqa: SLF001
    connection_count = len(storage._connections)  # noqa: SLF001
    observer_ids: set[int] = set()
    observer_lock_ids: set[int] = set()

    for _index in range(3):
        refresh = await adapter.refresh_publication_authority(
            context=context,
            page=page,
            decision=decision,
        )
        assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
        ledger = refresh._dependency_ledger  # noqa: SLF001
        assert ledger is not None
        assert ledger._connection is source_conn  # noqa: SLF001
        assert ledger._observer is observer  # noqa: SLF001
        assert ledger._observer_lock is observer_lock  # noqa: SLF001
        assert ledger._storage_generation == observer_generation  # noqa: SLF001
        observer_ids.add(id(ledger._observer))  # noqa: SLF001
        observer_lock_ids.add(id(ledger._observer_lock))  # noqa: SLF001
        assert len(storage._connections) == connection_count  # noqa: SLF001

    assert observer_ids == {id(observer)}
    assert observer_lock_ids == {id(observer_lock)}
    with storage.transaction() as conn:
        assert conn is source_conn
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is True
        )


@pytest.mark.parametrize("mutation", ("feedback", "embedding", "accepted-link"))
async def test_committed_provider_dependency_change_after_refresh_is_burned(
    storage: Any,
    mutation: str,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix=f"committed-dependency-{mutation}",
        query="R8ECOMMITTEDDEPENDENCY",
    )
    linked_entity = None
    if mutation == "accepted-link":
        linked_entity = Entity(
            new_id("ent"),
            TENANT,
            "CommittedDependencyEntityR8E",
            EntityType.ORGANIZATION,
        )
        storage.create_entity(linked_entity)
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"committed-dependency-{mutation}",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8ECOMMITTEDDEPENDENCY"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert decision.authorized is True
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED

    if mutation == "feedback":
        storage.store_feedback(
            FeedbackItem(
                id=new_id("feedback"),
                user_id=TENANT,
                target_type="knowledge_object",
                target_id=knowledge.id,
                feedback_type=FeedbackType.SEARCH_QUALITY,
                score=0.25,
            )
        )
    elif mutation == "embedding":
        storage.upsert_knowledge_embeddings(
            [
                {
                    "knowledge_object_id": knowledge.id,
                    "user_id": TENANT,
                    "model": "memory-exact-adversarial",
                    "dim": 2,
                    "source_version": knowledge.version,
                    "content_hash": hashlib.sha256(
                        knowledge.content.encode("utf-8")
                    ).hexdigest(),
                    "vector": b"\x00\x00\x00\x00\x00\x00\x00\x00",
                }
            ]
        )
    else:
        assert linked_entity is not None
        storage.link_knowledge_entity(
            TENANT,
            knowledge.id,
            linked_entity.id,
            status="accepted",
            evidence={"basis": "committed-dependency"},
            reviewed_by=PRINCIPAL,
        )

    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is False


@pytest.mark.parametrize("mutation", ("feedback-dml", "embedding-update"))
async def test_uncommitted_provider_dependency_change_burns_through_rollback(
    storage: Any,
    mutation: str,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix=f"uncommitted-dependency-{mutation}",
        query="R8EUNCOMMITTEDDEPENDENCY",
    )
    baseline_vector = b"\x00\x00\x00\x00\x00\x00\x00\x00"
    if mutation == "feedback-dml":
        storage.store_feedback(
            FeedbackItem(
                id=new_id("feedback"),
                user_id=TENANT,
                target_type="knowledge_object",
                target_id=knowledge.id,
                feedback_type=FeedbackType.SEARCH_QUALITY,
                score=0.25,
            )
        )
    else:
        storage.upsert_knowledge_embeddings(
            [
                {
                    "knowledge_object_id": knowledge.id,
                    "user_id": TENANT,
                    "model": "memory-exact-adversarial",
                    "dim": 2,
                    "source_version": knowledge.version,
                    "content_hash": hashlib.sha256(
                        knowledge.content.encode("utf-8")
                    ).hexdigest(),
                    "vector": baseline_vector,
                }
            ]
        )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"uncommitted-dependency-{mutation}",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8EUNCOMMITTEDDEPENDENCY"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert decision.authorized is True
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED

    with pytest.raises(RuntimeError, match="roll back hostile dependency"):
        with storage.transaction() as conn:
            if mutation == "feedback-dml":
                cursor = conn.execute(
                    """UPDATE feedback_state SET score=?,comment=?
                         WHERE user_id=? AND target_type=? AND target_id=?
                           AND feedback_type=?""",
                    (
                        0.75,
                        "uncommitted",
                        TENANT,
                        "knowledge_object",
                        knowledge.id,
                        FeedbackType.SEARCH_QUALITY.value,
                    ),
                )
            else:
                cursor = conn.execute(
                    "UPDATE knowledge_embeddings SET vector=? WHERE knowledge_object_id=?",
                    (b"\x01\x00\x00\x00\x01\x00\x00\x00", knowledge.id),
                )
            assert cursor.rowcount == 1
            assert (
                adapter.consume_publication_authority_in_transaction(
                    conn,
                    context=context,
                    page=page,
                    decision=decision,
                    refresh=refresh,
                )
                is False
            )
            raise RuntimeError("roll back hostile dependency")

    if mutation == "feedback-dml":
        restored = storage.execute(
            """SELECT score,comment FROM feedback_state
                 WHERE user_id=? AND target_type=? AND target_id=?
                   AND feedback_type=?""",
            (
                TENANT,
                "knowledge_object",
                knowledge.id,
                FeedbackType.SEARCH_QUALITY.value,
            ),
        ).fetchone()
        assert restored is not None
        assert restored[0] == pytest.approx(0.25)
        assert restored[1] == ""
    else:
        restored = storage.execute(
            "SELECT vector FROM knowledge_embeddings WHERE knowledge_object_id=?",
            (knowledge.id,),
        ).fetchone()
        assert restored is not None
        assert bytes(restored[0]) == baseline_vector
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is False


@pytest.mark.parametrize(
    ("entity_count", "bounded"),
    ((400, True), (401, False)),
    ids=("bounded-legacy-parity", "saturated-current-unknown"),
)
async def test_graph_read_set_cap_preserves_bounded_or_omits_saturated_current(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    entity_count: int,
    bounded: bool,
) -> None:
    _seed(storage, suffix=f"graph-read-cap-{entity_count}", query="R8EGRAPHCAP")
    _seed_graph_cards(storage, count=entity_count, label="R8EGraphCap")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"graph-read-cap-{entity_count}",
    )
    released_search_entities = KnowledgeGraph.search_entities
    released_context_for_query = KnowledgeGraph.context_for_query
    graph_rankings = 0
    graph_context_calls = 0

    def search_entities_spy(
        graph: KnowledgeGraph,
        *args: object,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        nonlocal graph_rankings
        graph_rankings += 1
        return released_search_entities(graph, *args, **kwargs)

    def context_for_query_spy(
        graph: KnowledgeGraph,
        *args: object,
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal graph_context_calls
        graph_context_calls += 1
        return released_context_for_query(graph, *args, **kwargs)

    monkeypatch.setattr(KnowledgeGraph, "search_entities", search_entities_spy)
    monkeypatch.setattr(KnowledgeGraph, "context_for_query", context_for_query_spy)
    traced_sql: list[str] = []
    page: Any = None
    storage.conn.set_trace_callback(traced_sql.append)
    try:
        page = await adapter.prepare(
            context=context,
            request=_request(actor, context, "R8EGRAPHCAP"),
        )
    finally:
        storage.conn.set_trace_callback(None)
    cap_reads = [
        " ".join(statement.casefold().split())
        for statement in traced_sql
        if "from entities e" in " ".join(statement.casefold().split())
        and "order by e.id limit 401" in " ".join(statement.casefold().split())
    ]
    assert cap_reads
    assert all("e.id>''" in statement for statement in cap_reads)
    card_projection_reads = [
        " ".join(statement.casefold().split())
        for statement in traced_sql
        if "cards as materialized" in " ".join(statement.casefold().split())
        and "select * from cards order by id" in " ".join(statement.casefold().split())
    ]
    assert page is not None
    if bounded:
        assert page.candidates
        assert graph_rankings == 1
        assert len(card_projection_reads) == 1
    else:
        assert graph_rankings == 0
        assert card_projection_reads == []
        graph_projection = page.graph_projection
        assert graph_projection.expanded is False
        assert graph_projection.nodes == ()
        assert graph_projection.relations == ()
        assert graph_projection.paths == ()
        assert graph_projection.nodes_coverage is MemoryExactGraphCoverage.UNKNOWN
    assert graph_context_calls == 0

    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    ledger = refresh._dependency_ledger  # noqa: SLF001
    assert ledger is not None
    read_set = ledger._read_set  # noqa: SLF001
    assert read_set._graph_saturated is (not bounded)  # noqa: SLF001
    assert type(read_set._graph_candidate_cards_sha256) is str  # noqa: SLF001
    assert len(read_set._graph_candidate_cards_sha256) == 64  # noqa: SLF001
    witness_kinds = tuple(witness._kind for witness in read_set._witnesses)  # noqa: SLF001
    assert witness_kinds.count("graph_candidate_cards") == 1
    if bounded:
        assert witness_kinds.count("graph_search_entities") == 1
        assert graph_rankings == 3
    else:
        assert "graph_search_entities" not in witness_kinds
        assert "graph_context_for_query" not in witness_kinds
        assert graph_rankings == 0
    assert graph_context_calls == 0


async def test_saturated_temporal_graph_is_unavailable_before_graph_ranking(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(storage, suffix="graph-temporal-saturated", query="R8EGRAPHTEMPORALCAP")
    _seed_graph_cards(storage, count=401, label="R8EGraphTemporalCap")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="graph-temporal-saturated",
    )
    graph_calls = 0

    def forbidden_graph(*_args: object, **_kwargs: object) -> object:
        nonlocal graph_calls
        graph_calls += 1
        raise AssertionError("temporal saturated request reached KnowledgeGraph")

    monkeypatch.setattr(KnowledgeGraph, "context_for_query", forbidden_graph)
    monkeypatch.setattr(KnowledgeGraph, "search_entities", forbidden_graph)
    with pytest.raises(MemoryExactInternalError, match="provider snapshot is invalid"):
        await adapter.prepare(
            context=context,
            request=_request(
                actor,
                context,
                "как связан R8EGraphTemporalCap",
                as_of="2022-01-01",
            ),
        )
    assert graph_calls == 0


async def test_graph_saturation_transition_after_refresh_burns_receipt(
    storage: Any,
) -> None:
    _seed(storage, suffix="graph-saturation-transition", query="R8EGRAPHSATURATION")
    entities = _seed_graph_cards(
        storage,
        count=401,
        label="R8EGraphSaturation",
    )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="graph-saturation-transition",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8EGRAPHSATURATION"),
    )
    assert page.graph_projection.nodes_coverage is MemoryExactGraphCoverage.UNKNOWN
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    ledger = refresh._dependency_ledger  # noqa: SLF001
    assert ledger is not None
    assert ledger._read_set._graph_saturated is True  # noqa: SLF001

    storage.execute(
        "UPDATE entities SET deleted_at=? WHERE id=?",
        ("2026-09-02T08:00:00+00:00", entities[-1].id),
    )
    storage.commit()
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is False


async def test_graph_operation_failure_then_success_after_refresh_burns_receipt(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "R8EGraphFailureTransition"
    _seed(storage, suffix="graph-failure-transition", query=query)
    _seed_graph_cards(storage, count=1, label=query)
    released_search_entities = KnowledgeGraph.search_entities
    fail_graph = True
    graph_calls = 0

    def unstable_search_entities(
        graph: KnowledgeGraph,
        *args: object,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        nonlocal graph_calls
        graph_calls += 1
        if fail_graph:
            raise RuntimeError("injected current graph failure")
        return released_search_entities(graph, *args, **kwargs)

    monkeypatch.setattr(KnowledgeGraph, "search_entities", unstable_search_entities)
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="graph-failure-transition",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, query),
    )
    assert page.graph_projection.expanded is False
    assert page.graph_projection.nodes == ()
    assert page.graph_projection.nodes_coverage is MemoryExactGraphCoverage.UNKNOWN
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    ledger = refresh._dependency_ledger  # noqa: SLF001
    assert ledger is not None
    read_set = ledger._read_set  # noqa: SLF001
    assert read_set._graph_saturated is False  # noqa: SLF001
    witness_kinds = tuple(
        witness._kind  # noqa: SLF001
        for witness in read_set._witnesses  # noqa: SLF001
    )
    assert witness_kinds.count("graph_search_entities") == 1
    assert read_set._graph_suppressed_at == witness_kinds.index(  # noqa: SLF001
        "graph_search_entities"
    )
    assert graph_calls >= 2

    fail_graph = False
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is False


def test_provider_select_view_rejects_write_cte_and_restores_authorizer(
    storage: Any,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix="select-view-write-cte",
        query="R8EWRITECTE",
    )
    baseline = knowledge.content
    import friday.storage._memory_exact_internal as memory_storage

    with read_only_storage_snapshot(storage) as conn:
        view = memory_storage._MemoryExactProviderSelectView(conn)  # noqa: SLF001
        with pytest.raises((MemoryExactStorageError, sqlite3.DatabaseError)):
            view.execute(
                """WITH target(id) AS (VALUES (?))
                   UPDATE knowledge_objects SET content=?
                    WHERE id IN (SELECT id FROM target)""",
                (knowledge.id, "R8EWRITECTE forged body"),
            )
        unchanged = conn.execute(
            "SELECT content FROM knowledge_objects WHERE id=?",
            (knowledge.id,),
        ).fetchone()
        assert unchanged is not None
        assert unchanged[0] == baseline

        # The temporary SELECT-only authorizer must be gone, while Friday's
        # connection-owned private-material authorizer must still be installed.
        allowed = conn.execute(
            """UPDATE relation_revision_context SET observed_at=observed_at
                 WHERE singleton=1"""
        )
        assert allowed.rowcount == 1
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(
                """UPDATE private_entity_material_cache_state SET valid=valid
                     WHERE singleton=1"""
            )

    durable = storage.execute(
        "SELECT content FROM knowledge_objects WHERE id=?",
        (knowledge.id,),
    ).fetchone()
    assert durable is not None
    assert durable[0] == baseline


@pytest.mark.parametrize("write_vector", ("callback-update", "writable-blobopen"))
def test_whole_graph_operation_holds_read_only_lease_through_materialization(
    storage: Any,
    write_vector: str,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix=f"graph-read-only-lease-{write_vector}",
        query="R8EGraphReadOnlyLease",
    )
    entities = _seed_graph_cards(
        storage,
        count=2,
        label="R8EGraphReadOnlyLease",
    )
    candidate_ids = tuple(sorted(entity.id for entity in entities))
    baseline = knowledge.content
    import friday.storage._memory_exact_internal as memory_storage

    attempted = False
    denied = False
    with storage.transaction() as conn:

        def hostile_reservation(_size: int) -> None:
            nonlocal attempted, denied
            if attempted:
                return
            attempted = True
            try:
                if write_vector == "callback-update":
                    conn.execute(
                        """UPDATE relation_revision_context
                              SET observed_at=observed_at WHERE singleton=1"""
                    )
                else:
                    with conn.blobopen(
                        "relation_revision_context",
                        "observed_at",
                        1,
                        readonly=False,
                    ) as blob:
                        first = blob.read(1)
                        assert first
                        blob.seek(0)
                        blob.write(first)
            except sqlite3.DatabaseError:
                denied = True

        released, proof_sha256 = (
            memory_storage._replay_memory_exact_provider_graph_operation_in_transaction(  # noqa: SLF001
                conn,
                storage=storage,
                allow_active_managed_context=True,
                kind="graph_search_entities",
                arguments=(TENANT, "R8EGraphReadOnlyLease", 10, None),
                candidate_entity_ids=candidate_ids,
                reserve_bytes=hostile_reservation,
            )
        )
        assert type(released) is list
        assert type(proof_sha256) is str
        assert len(proof_sha256) == 64
        assert attempted is True
        assert denied is True

        query_only_after = conn.execute("PRAGMA query_only").fetchone()
        assert query_only_after is not None
        assert query_only_after[0] == 0
        allowed = conn.execute(
            """UPDATE relation_revision_context
                  SET observed_at=observed_at WHERE singleton=1"""
        )
        assert allowed.rowcount == 1
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            conn.execute(
                """UPDATE private_entity_material_cache_state SET valid=valid
                     WHERE singleton=1"""
            )

    durable = storage.execute(
        "SELECT content FROM knowledge_objects WHERE id=?",
        (knowledge.id,),
    ).fetchone()
    assert durable is not None
    assert durable[0] == baseline


def test_temp_schema_cap_plus_one_refuses_before_row_materialization(
    storage: Any,
) -> None:
    for index in range(513):
        storage.conn.execute(  # nosec B608 - bounded code-owned test identifier
            f"CREATE TEMP TABLE r8e_temp_cap_{index}(value INTEGER)"
        )

    materialized_names: set[str] = set()

    def tracking_text_factory(value: bytes) -> str:
        decoded = value.decode("utf-8", errors="strict")
        if decoded.startswith("r8e_temp_cap_"):
            materialized_names.add(decoded)
        return decoded

    storage.conn.text_factory = tracking_text_factory
    try:
        with pytest.raises(memory_exact_internal._ProviderSnapshotInvalid):  # noqa: SLF001
            memory_exact_internal._temp_schema_sha256(storage.conn)  # noqa: SLF001
    finally:
        storage.conn.text_factory = str
    assert materialized_names == set()


def test_oversized_temp_ddl_refuses_before_sql_body_materialization(
    storage: Any,
) -> None:
    ddl_marker = "R8E_OVERSIZED_TEMP_DDL"
    storage.conn.execute(  # nosec B608 - fixed test DDL plus bounded filler
        f"CREATE TEMP VIEW r8e_oversized_temp_ddl AS SELECT 1 /*{ddl_marker}"
        + ("x" * 1_048_577)
        + "*/"
    )
    materialized_body = False

    def tracking_text_factory(value: bytes) -> str:
        nonlocal materialized_body
        if len(value) > 1_048_576 or ddl_marker.encode("ascii") in value:
            materialized_body = True
        return value.decode("utf-8", errors="strict")

    storage.conn.text_factory = tracking_text_factory
    try:
        with pytest.raises(memory_exact_internal._ProviderSnapshotInvalid):  # noqa: SLF001
            memory_exact_internal._temp_schema_sha256(storage.conn)  # noqa: SLF001
    finally:
        storage.conn.text_factory = str
    assert materialized_body is False


def test_function_registry_cap_plus_one_is_bounded(storage: Any) -> None:
    baseline = storage.conn.execute("PRAGMA function_list").fetchall()
    missing = 2_049 - len(baseline)
    assert missing > 0

    def zero() -> int:
        return 0

    for index in range(missing):
        storage.conn.create_function(f"r8e_function_cap_{index}", 0, zero)
    with pytest.raises(memory_exact_internal._ProviderSnapshotInvalid):  # noqa: SLF001
        memory_exact_internal._function_schema_sha256(storage.conn)  # noqa: SLF001


async def test_oversized_consumed_graph_link_field_refuses_before_materialization(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "как связан OversizedLinkAlphaR8E с OversizedLinkBetaR8E"
    _raw, knowledge = _seed(
        storage,
        suffix="oversized-graph-link-field",
        query="R8EOVERSIZEDGRAPHLINK",
        body=f"R8EOVERSIZEDGRAPHLINK: {query}",
    )
    alpha = Entity(new_id("ent"), TENANT, "OversizedLinkAlphaR8E", EntityType.PERSON)
    beta = Entity(
        new_id("ent"),
        TENANT,
        "OversizedLinkBetaR8E",
        EntityType.ORGANIZATION,
    )
    links: list[dict[str, Any]] = []
    for entity in (alpha, beta):
        storage.create_entity(entity)
        links.append(
            storage.link_knowledge_entity(
                TENANT,
                knowledge.id,
                entity.id,
                status="accepted",
                evidence={"basis": "oversized-graph-link-field"},
                reviewed_by=PRINCIPAL,
            )
        )
    storage.create_relation(
        Relation(
            new_id("rel"),
            TENANT,
            alpha.id,
            beta.id,
            RelationType.RELATED_TO,
            valid_from="2020-01-01",
            metadata_json={"evidence": {"knowledge_object_id": knowledge.id}},
        )
    )

    import friday.storage._memory_exact_internal as memory_storage

    oversized_bytes = memory_storage.MEMORY_EXACT_MAX_SNAPSHOT_UTF8_BYTES + 1
    storage.execute(
        """UPDATE knowledge_entity_links
              SET created_at=CAST(zeroblob(?) AS TEXT) WHERE id=?""",
        (oversized_bytes, links[0]["id"]),
    )
    storage.commit()
    stored_size = storage.execute(
        "SELECT length(CAST(created_at AS BLOB)) FROM knowledge_entity_links WHERE id=?",
        (links[0]["id"],),
    ).fetchone()
    assert stored_size is not None
    assert stored_size[0] == oversized_bytes

    materialized = False
    released_links = (
        memory_storage._MemoryExactProviderGraphSelectView.list_knowledge_entity_links  # noqa: SLF001
    )

    def links_spy(view: Any, *args: object, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal materialized
        rows = released_links(view, *args, **kwargs)
        if any(
            type(row.get("created_at")) in (str, bytes)
            and len(row["created_at"]) >= oversized_bytes
            for row in rows
        ):
            materialized = True
        return rows

    monkeypatch.setattr(
        memory_storage._MemoryExactProviderGraphSelectView,  # noqa: SLF001
        "list_knowledge_entity_links",
        links_spy,
    )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="oversized-graph-link-field",
    )
    traced_sql: list[str] = []
    storage.conn.set_trace_callback(traced_sql.append)
    try:
        with pytest.raises(MemoryExactInternalError):
            await adapter.prepare(
                context=context,
                request=_request(actor, context, query, as_of="2024-01-01"),
            )
    finally:
        storage.conn.set_trace_callback(None)
    assert materialized is False
    link_preflights = [
        " ".join(statement.casefold().split())
        for statement in traced_sql
        if "from knowledge_entity_links" in " ".join(statement.casefold().split())
        and "length(cast(link.created_at as blob))"
        in " ".join(statement.casefold().split())
    ]
    assert link_preflights


@pytest.mark.parametrize(
    "transaction_mode",
    ("ordinary-stable", "direct-active-refused", "ordinary-drift"),
)
async def test_active_transaction_known_at_replay_is_bound_and_drift_sensitive(
    storage: Any,
    transaction_mode: str,
) -> None:
    _knowledge, _alpha, _beta, _relation, query, known_at = _seed_known_at_graph(
        storage,
        suffix=f"active-known-at-{transaction_mode}",
    )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"active-known-at-{transaction_mode}",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(
            actor,
            context,
            query,
            as_of="2024-01-01",
            known_at=known_at,
        ),
    )
    assert page.graph_projection.relations
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    ledger = refresh._dependency_ledger  # noqa: SLF001
    assert ledger is not None

    def consume(conn: sqlite3.Connection) -> bool:
        active = conn.execute(
            """SELECT batch_id,recorded_at,observed_at
                 FROM relation_revision_context WHERE singleton=1"""
        ).fetchone()
        assert active is not None
        assert str(active[0])
        assert active[1] == active[2]
        return adapter.consume_publication_authority_in_transaction(
            conn,
            context=context,
            page=page,
            decision=decision,
            refresh=refresh,
        )

    candidate_ids = next(
        witness._arguments[2]  # noqa: SLF001
        for witness in ledger._read_set._witnesses  # noqa: SLF001
        if witness._kind == "relation_history_status"  # noqa: SLF001
    )
    if transaction_mode == "ordinary-stable":
        with storage.transaction() as conn:
            assert consume(conn) is True
    elif transaction_mode == "direct-active-refused":
        import friday.storage._memory_exact_internal as memory_storage

        with storage.transaction() as conn:
            with pytest.raises(
                MemoryExactStorageError,
                match="relation history transaction is not authorized",
            ):
                memory_storage._memory_exact_provider_relation_history_status_in_transaction(  # noqa: SLF001
                    conn,
                    tenant_id=TENANT,
                    known_at=known_at,
                    candidate_entity_ids=candidate_ids,
                    reserve_bytes=lambda _size: None,
                    allow_active_managed_context=False,
                )
            assert consume(conn) is True
    else:
        idle_context = storage.execute(
            """SELECT batch_id,recorded_at,observed_at
                 FROM relation_revision_context WHERE singleton=1"""
        ).fetchone()
        assert idle_context is not None
        with pytest.raises(RuntimeError, match="roll back active history drift"):
            with storage.transaction() as conn:
                expected_total_changes = ledger._total_changes + 1  # noqa: SLF001
                assert conn.total_changes == expected_total_changes
                active = conn.execute(
                    """SELECT recorded_at,observed_at
                         FROM relation_revision_context WHERE singleton=1"""
                ).fetchone()
                assert active is not None
                assert active[0] == active[1]
                baseline = str(active[1]).encode("utf-8")
                digit_offset = max(
                    index
                    for index, value in enumerate(baseline)
                    if 48 <= value <= 57
                )
                replacement = b"0" if baseline[digit_offset] != ord("0") else b"1"
                with conn.blobopen(
                    "relation_revision_context",
                    "observed_at",
                    1,
                    readonly=False,
                ) as blob:
                    blob.seek(digit_offset)
                    assert blob.read(1) == baseline[digit_offset : digit_offset + 1]
                    blob.seek(digit_offset)
                    blob.write(replacement)
                assert conn.total_changes == expected_total_changes
                assert consume(conn) is False
                assert conn.total_changes == expected_total_changes
                raise RuntimeError("roll back active history drift")
        restored_context = storage.execute(
            """SELECT batch_id,recorded_at,observed_at
                 FROM relation_revision_context WHERE singleton=1"""
        ).fetchone()
        assert restored_context is not None
        assert tuple(restored_context) == tuple(idle_context)

    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is False


@pytest.mark.parametrize(
    "endpoint_case",
    ("bounded", "overflow", "incomplete", "additional-drift"),
)
async def test_historical_additional_endpoint_scope_is_bounded_and_witnessed(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    endpoint_case: str,
) -> None:
    alpha, beta, merged, query, known_at = _seed_merged_known_at_graph(
        storage,
        suffix=f"historical-additional-endpoint-{endpoint_case}",
    )
    import friday.storage._memory_exact_internal as memory_storage

    assert memory_storage.MEMORY_EXACT_MAX_GRAPH_ENTITY_SOURCE_ROWS == 512
    if endpoint_case == "overflow":
        monkeypatch.setattr(
            memory_storage,
            "MEMORY_EXACT_MAX_GRAPH_ENTITY_SOURCE_ROWS",
            2,
        )
    elif endpoint_case == "incomplete":
        storage.execute(
            "DELETE FROM entity_versions WHERE entity_id=?",
            (merged.id,),
        )
        storage.commit()

    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"historical-additional-endpoint-{endpoint_case}",
    )
    request = _request(
        actor,
        context,
        query,
        as_of="2024-01-01",
        known_at=known_at,
    )
    if endpoint_case in {"overflow", "incomplete"}:
        with pytest.raises(MemoryExactInternalError):
            await adapter.prepare(context=context, request=request)
        return

    page = await adapter.prepare(context=context, request=request)
    assert page.graph_projection.relations
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    ledger = refresh._dependency_ledger  # noqa: SLF001
    assert ledger is not None
    history_witnesses = [
        witness
        for witness in ledger._read_set._witnesses  # noqa: SLF001
        if witness._kind == "relation_history_status"  # noqa: SLF001
    ]
    assert len(history_witnesses) == 1
    assert history_witnesses[0]._arguments == (  # noqa: SLF001
        TENANT,
        known_at,
        tuple(sorted((alpha.id, beta.id))),
    )

    if endpoint_case == "bounded":
        with storage.transaction() as conn:
            assert (
                adapter.consume_publication_authority_in_transaction(
                    conn,
                    context=context,
                    page=page,
                    decision=decision,
                    refresh=refresh,
                )
                is True
            )
    else:
        version_row = storage.execute(
            """SELECT rowid,snapshot_json FROM entity_versions
                 WHERE entity_id=? ORDER BY version DESC LIMIT 1""",
            (merged.id,),
        ).fetchone()
        assert version_row is not None
        rowid = int(version_row[0])
        baseline = str(version_row[1]).encode("utf-8")
        assert baseline.startswith(b"{")
        with pytest.raises(RuntimeError, match="roll back additional endpoint drift"):
            with storage.transaction() as conn:
                expected_total_changes = ledger._total_changes + 1  # noqa: SLF001
                assert conn.total_changes == expected_total_changes
                with conn.blobopen(
                    "entity_versions",
                    "snapshot_json",
                    rowid,
                    readonly=False,
                ) as blob:
                    assert blob.read(1) == b"{"
                    blob.seek(0)
                    blob.write(b"[")
                assert conn.total_changes == expected_total_changes
                assert (
                    adapter.consume_publication_authority_in_transaction(
                        conn,
                        context=context,
                        page=page,
                        decision=decision,
                        refresh=refresh,
                    )
                    is False
                )
                assert conn.total_changes == expected_total_changes
                raise RuntimeError("roll back additional endpoint drift")
        restored = storage.execute(
            "SELECT snapshot_json FROM entity_versions WHERE rowid=?",
            (rowid,),
        ).fetchone()
        assert restored is not None
        assert str(restored[0]).encode("utf-8") == baseline
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is False


async def test_historical_incident_cap_counts_only_latest_public_relations(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = "как связан IncidentCapAlphaR8E с IncidentCapBetaR8E"
    _seed(
        storage,
        suffix="historical-incident-public-cap",
        query="R8EHISTORICALINCIDENTCAP",
        body=f"R8EHISTORICALINCIDENTCAP: {query}",
    )
    alpha = Entity(new_id("ent"), TENANT, "IncidentCapAlphaR8E", EntityType.PERSON)
    beta = Entity(
        new_id("ent"),
        TENANT,
        "IncidentCapBetaR8E",
        EntityType.ORGANIZATION,
    )
    deleted_targets = [
        Entity(
            new_id("ent"),
            TENANT,
            f"DeletedIncidentTargetR8E{index}",
            EntityType.CONCEPT,
        )
        for index in range(3)
    ]
    private_target = Entity(
        new_id("ent"),
        TENANT,
        "PrivateIncidentTargetR8E",
        EntityType.CONCEPT,
    )
    for entity in (alpha, beta, *deleted_targets, private_target):
        storage.create_entity(entity)

    visible = Relation(
        new_id("rel"),
        TENANT,
        alpha.id,
        beta.id,
        RelationType.RELATED_TO,
        valid_from="2020-01-01",
    )
    storage.create_relation(visible)
    deleted_relations: list[Relation] = []
    for target in deleted_targets:
        relation = Relation(
            new_id("rel"),
            TENANT,
            alpha.id,
            target.id,
            RelationType.MEMBER_OF,
            valid_from="2020-01-01",
        )
        storage.create_relation(relation)
        deleted_relations.append(relation)
    private_relation = Relation(
        new_id("rel"),
        TENANT,
        alpha.id,
        private_target.id,
        RelationType.DEPENDS_ON,
        valid_from="2020-01-01",
    )
    storage.create_relation(private_relation)
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners
               (entity_id,person_id,privacy_kind,created_at)
               VALUES(?,?,?,?)""",
            (private_target.id, PRINCIPAL, "reminder", BASE_TIME),
        )
        conn.executemany(
            "DELETE FROM relations WHERE id=? AND user_id=?",
            ((relation.id, TENANT) for relation in deleted_relations),
        )
    boundary_row = storage.execute(
        """SELECT MAX(recorded_at) FROM relation_revisions
             WHERE relation_id IN (?,?,?)""",
        tuple(relation.id for relation in deleted_relations),
    ).fetchone()
    assert boundary_row is not None
    known_at = str(boundary_row[0])

    import friday.storage._memory_exact_internal as memory_storage

    assert memory_storage._MEMORY_EXACT_MAX_HISTORICAL_RELATION_IDS == 802  # noqa: SLF001
    monkeypatch.setattr(
        memory_storage,
        "_MEMORY_EXACT_MAX_HISTORICAL_RELATION_IDS",
        2,
    )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="historical-incident-public-cap",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(
            actor,
            context,
            query,
            as_of="2024-01-01",
            known_at=known_at,
        ),
    )
    assert tuple(
        relation.relation_type for relation in page.graph_projection.relations
    ) == (RelationType.RELATED_TO.value,)

    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is True
        )


async def test_large_unrelated_history_stays_outside_bounded_known_at_ledger(
    storage: Any,
) -> None:
    storage.ensure_user(TENANT, preset_key="owner")
    unrelated = Entity(
        new_id("ent"),
        TENANT,
        "UnrelatedHistoricalNoiseR8E",
        EntityType.CONCEPT,
    )
    storage.create_entity(unrelated)
    assert storage.soft_delete_entity(unrelated.id, TENANT) is True
    unrelated_tail = storage.execute(
        """SELECT snapshot_json,created_at,version FROM entity_versions
             WHERE entity_id=? ORDER BY version DESC LIMIT 1""",
        (unrelated.id,),
    ).fetchone()
    assert unrelated_tail is not None
    stable_snapshot = str(unrelated_tail[0])
    stable_created_at = str(unrelated_tail[1])
    first_version = int(unrelated_tail[2]) + 1
    history_rows = 600
    with storage.transaction() as conn:
        conn.executemany(
            """INSERT INTO entity_versions
               (id,user_id,entity_id,version,snapshot_json,created_at)
               VALUES(?,?,?,?,?,?)""",
            (
                (
                    new_id("entv"),
                    TENANT,
                    unrelated.id,
                    first_version + index,
                    "{" if index == history_rows - 1 else stable_snapshot,
                    stable_created_at,
                )
                for index in range(history_rows)
            ),
        )

    _knowledge, alpha, beta, _relation, query, known_at = _seed_known_at_graph(
        storage,
        suffix="large-unrelated-history",
    )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="large-unrelated-history",
    )
    traced_sql: list[str] = []
    storage.conn.set_trace_callback(traced_sql.append)
    try:
        page = await adapter.prepare(
            context=context,
            request=_request(
                actor,
                context,
                query,
                as_of="2024-01-01",
                known_at=known_at,
            ),
        )
    finally:
        storage.conn.set_trace_callback(None)
    assert page.graph_projection.relations
    history_reads = [
        " ".join(statement.casefold().split())
        for statement in traced_sql
        if "entity_versions" in " ".join(statement.casefold().split())
    ]
    assert history_reads
    assert all(unrelated.id.casefold() not in statement for statement in history_reads)
    assert all(
        alpha.id.casefold() in statement or beta.id.casefold() in statement
        for statement in history_reads
    )
    normalized_sql = [" ".join(statement.casefold().split()) for statement in traced_sql]
    assert not any("coalesce(max(rr.event_seq)" in statement for statement in normalized_sql)
    historical_rankings = [
        statement
        for statement in normalized_sql
        if "row_number() over" in statement and "relation_revisions" in statement
    ]
    assert historical_rankings
    assert all(" values " in statement for statement in historical_rankings)

    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    ledger = refresh._dependency_ledger  # noqa: SLF001
    assert ledger is not None
    witnesses = ledger._read_set._witnesses  # noqa: SLF001
    assert len(witnesses) <= memory_exact_internal._PROVIDER_READ_SET_MAX_OPERATIONS  # noqa: SLF001
    history_witnesses = [
        witness
        for witness in witnesses
        if witness._kind == "relation_history_status"  # noqa: SLF001
    ]
    assert len(history_witnesses) == 1
    assert history_witnesses[0]._arguments == (  # noqa: SLF001
        TENANT,
        known_at,
        tuple(sorted((alpha.id, beta.id))),
    )
    graph_witnesses = [
        witness
        for witness in witnesses
        if witness._kind == "graph_context_for_query"  # noqa: SLF001
    ]
    assert len(graph_witnesses) == 1
    assert len(graph_witnesses[0]._result_sha256) == 64  # noqa: SLF001

    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is True
        )


def test_provider_read_set_fixed_operation_cap_refuses_cap_plus_one() -> None:
    operation_cap = memory_exact_internal._PROVIDER_READ_SET_MAX_OPERATIONS  # noqa: SLF001
    assert operation_cap == 1_024
    read_set = memory_exact_internal._ProviderReadSet()  # noqa: SLF001
    for index in range(operation_cap):
        assert read_set.observe(
            "known_vocabulary",
            ((f"r8e-read-set-{index}",),),
            set(),
        ) == set()
    with pytest.raises(memory_exact_internal._ProviderResourceExceeded):  # noqa: SLF001
        read_set.observe(
            "known_vocabulary",
            (("r8e-read-set-cap-plus-one",),),
            set(),
        )
    with pytest.raises(memory_exact_internal._ProviderResourceExceeded):  # noqa: SLF001
        read_set.require_collecting()
    with pytest.raises(memory_exact_internal._ProviderResourceExceeded):  # noqa: SLF001
        read_set.finalize()


async def test_graph_search_blobopen_promotes_unreturned_card_and_burns_receipt(
    storage: Any,
) -> None:
    query = "R8EGraphCard"
    _seed(storage, suffix="graph-search-blob", query=query)
    _seed_graph_cards(storage, count=5, label=query)
    target = Entity(
        new_id("ent"),
        TENANT,
        "X" * len(query),
        EntityType.CONCEPT,
    )
    storage.create_entity(target)
    graph = KnowledgeGraph(storage)
    initial_matches = graph.search_entities(TENANT, query, limit=5)
    assert len(initial_matches) == 5
    assert target.id not in {str(item["id"]) for item in initial_matches}

    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="graph-search-blob",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, query),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    ledger = refresh._dependency_ledger  # noqa: SLF001
    assert ledger is not None
    assert "graph_search_entities" in {
        witness._kind  # noqa: SLF001
        for witness in ledger._read_set._witnesses  # noqa: SLF001
    }
    target_row = storage.execute(
        "SELECT rowid,name FROM entities WHERE id=?",
        (target.id,),
    ).fetchone()
    assert target_row is not None
    rowid = int(target_row[0])
    baseline = str(target_row[1]).encode("utf-8")
    replacement = query.encode("utf-8")
    assert len(replacement) == len(baseline)

    with pytest.raises(RuntimeError, match="roll back graph search blob"):
        with storage.transaction() as conn:
            expected_total_changes = ledger._total_changes + 1  # noqa: SLF001
            assert conn.total_changes == expected_total_changes
            with conn.blobopen("entities", "name", rowid, readonly=False) as blob:
                assert blob.read() == baseline
                blob.seek(0)
                blob.write(replacement)
            assert conn.total_changes == expected_total_changes
            changed_matches = KnowledgeGraph(storage).search_entities(
                TENANT,
                query,
                limit=5,
            )
            assert target.id in {str(item["id"]) for item in changed_matches}
            assert conn.total_changes == expected_total_changes
            assert (
                adapter.consume_publication_authority_in_transaction(
                    conn,
                    context=context,
                    page=page,
                    decision=decision,
                    refresh=refresh,
                )
                is False
            )
            assert conn.total_changes == expected_total_changes
            raise RuntimeError("roll back graph search blob")

    restored = storage.execute(
        "SELECT name FROM entities WHERE rowid=?",
        (rowid,),
    ).fetchone()
    assert restored is not None
    assert str(restored[0]).encode("utf-8") == baseline
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is False
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )


async def test_graph_context_blobopen_dependency_is_burned_through_rollback(
    storage: Any,
) -> None:
    query = "как связан BlobAlphaR8E с BlobBetaR8E"
    _raw, knowledge = _seed(
        storage,
        suffix="graph-context-blob",
        query="R8EGRAPHCONTEXTBLOB",
        body=f"R8EGRAPHCONTEXTBLOB: {query}",
    )
    alpha = Entity(new_id("ent"), TENANT, "BlobAlphaR8E", EntityType.PERSON)
    beta = Entity(new_id("ent"), TENANT, "BlobBetaR8E", EntityType.ORGANIZATION)
    for entity in (alpha, beta):
        storage.create_entity(entity)
        storage.link_knowledge_entity(
            TENANT,
            knowledge.id,
            entity.id,
            status="accepted",
            evidence={"basis": "graph-context-blob"},
            reviewed_by=PRINCIPAL,
        )
    relation = Relation(
        new_id("rel"),
        TENANT,
        alpha.id,
        beta.id,
        RelationType.RELATED_TO,
        metadata_json={"evidence": {"knowledge_object_id": knowledge.id}},
        valid_from="2020-01-01",
    )
    storage.create_relation(relation)
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="graph-context-blob",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, query),
    )
    assert page.graph_projection.relations
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    ledger = refresh._dependency_ledger  # noqa: SLF001
    assert ledger is not None
    context_witnesses = [
        witness
        for witness in ledger._read_set._witnesses  # noqa: SLF001
        if witness._kind == "graph_context_for_query"  # noqa: SLF001
    ]
    assert len(context_witnesses) == 1
    relation_row = storage.execute(
        "SELECT rowid,valid_from FROM relations WHERE id=?",
        (relation.id,),
    ).fetchone()
    assert relation_row is not None
    rowid = int(relation_row[0])
    baseline = str(relation_row[1]).encode("utf-8")
    replacement = b"2021-01-01"
    assert baseline == b"2020-01-01"
    assert len(replacement) == len(baseline)

    with pytest.raises(RuntimeError, match="roll back graph context blob"):
        with storage.transaction() as conn:
            expected_total_changes = ledger._total_changes + 1  # noqa: SLF001
            assert conn.total_changes == expected_total_changes
            with conn.blobopen("relations", "valid_from", rowid, readonly=False) as blob:
                assert blob.read() == baseline
                blob.seek(0)
                blob.write(replacement)
            assert conn.total_changes == expected_total_changes

            arguments = context_witnesses[0]._arguments  # noqa: SLF001
            seeds = None if arguments[5] is None else list(arguments[5])
            changed_context = KnowledgeGraph(storage).context_for_query(
                arguments[0],
                arguments[1],
                depth=arguments[2],
                entity_limit=arguments[3],
                knowledge_limit=arguments[4],
                seed_knowledge_ids=seeds,
                as_of=arguments[6],
                known_at=arguments[7],
            )
            assert any(
                item.get("valid_from") == replacement.decode("ascii")
                for item in changed_context["relations"]
            )
            assert conn.total_changes == expected_total_changes
            assert (
                adapter.consume_publication_authority_in_transaction(
                    conn,
                    context=context,
                    page=page,
                    decision=decision,
                    refresh=refresh,
                )
                is False
            )
            assert conn.total_changes == expected_total_changes
            raise RuntimeError("roll back graph context blob")

    restored = storage.execute(
        "SELECT valid_from FROM relations WHERE rowid=?",
        (rowid,),
    ).fetchone()
    assert restored is not None
    assert str(restored[0]).encode("utf-8") == baseline
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is False
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )


@pytest.mark.parametrize(
    ("dependency", "table", "column", "witness_kind"),
    (
        (
            "whole-embedding",
            "knowledge_embeddings",
            "vector",
            "get_user_embeddings",
        ),
        (
            "chunk-embedding",
            "knowledge_chunk_embeddings",
            "vector",
            "get_user_chunk_embeddings",
        ),
        (
            "knowledge-text",
            "knowledge_objects",
            "content",
            "provider_rows",
        ),
    ),
    ids=("whole-embedding", "chunk-embedding", "knowledge-text"),
)
async def test_writable_blobopen_rank_dependency_is_burned_through_rollback(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    dependency: str,
    table: str,
    column: str,
    witness_kind: str,
) -> None:
    raw, knowledge = _seed(
        storage,
        suffix=f"blob-read-set-{dependency}",
        query="R8EBLOBREADSET",
    )
    vector = pack_vector([1.0, 0.0])
    content_sha256 = hashlib.sha256(knowledge.content.encode("utf-8")).hexdigest()
    storage.upsert_knowledge_vectors(
        [
            {
                "knowledge_object_id": knowledge.id,
                "user_id": TENANT,
                "model": "memory-exact-read-set",
                "dim": 2,
                "source_version": knowledge.version,
                "content_hash": content_sha256,
                "chunk_scheme": "memory-exact-read-set-v1",
                "vector": vector,
            }
        ],
        {
            knowledge.id: [
                {
                    "chunk_index": 0,
                    "user_id": TENANT,
                    "model": "memory-exact-read-set",
                    "dim": 2,
                    "source_version": knowledge.version,
                    "chunk_scheme": "memory-exact-read-set-v1",
                    "start_char": 0,
                    "end_char": len(knowledge.content),
                    "content_hash": content_sha256,
                    "vector": vector,
                }
            ]
        },
    )
    _authorization, actor, context, _searcher, adapter = _dense_stack(
        storage,
        monkeypatch,
        label=f"blob-read-set-{dependency}",
    )
    turn_request = _request(actor, context, "R8EBLOBREADSET")
    page = await adapter.prepare(context=context, request=turn_request)
    assert [candidate.knowledge_id for candidate in page.candidates] == [knowledge.id]
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert decision.authorized is True
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    ledger = refresh._dependency_ledger  # noqa: SLF001
    assert ledger is not None
    read_set = ledger._read_set  # noqa: SLF001
    witnessed_kinds = tuple(witness._kind for witness in read_set._witnesses)  # noqa: SLF001
    assert witness_kind in witnessed_kinds

    if dependency == "chunk-embedding":
        target = storage.execute(
            f"SELECT rowid,{column} FROM {table} "  # nosec B608 - fixed parametrization
            "WHERE knowledge_object_id=? AND chunk_index=0",
            (knowledge.id,),
        ).fetchone()
    elif dependency == "knowledge-text":
        target = storage.execute(
            f"SELECT rowid,{column} FROM {table} WHERE id=?",  # nosec B608
            (knowledge.id,),
        ).fetchone()
    else:
        target = storage.execute(
            f"SELECT rowid,{column} FROM {table} WHERE knowledge_object_id=?",  # nosec B608
            (knowledge.id,),
        ).fetchone()
    assert target is not None
    rowid = int(target[0])
    baseline = (
        target[1].encode("utf-8") if isinstance(target[1], str) else bytes(target[1])
    )
    assert baseline
    changed = bytes((baseline[0] ^ 1,)) + baseline[1:]

    with pytest.raises(RuntimeError, match="roll back blob dependency"):
        with storage.transaction() as conn:
            # Friday's publication transaction owns exactly one local context
            # update. Incremental BLOB I/O is deliberately absent from
            # Connection.total_changes, so only the bounded read-set can close
            # this same-connection mutation gap.
            expected_total_changes = ledger._total_changes + 1  # noqa: SLF001
            assert conn.total_changes == expected_total_changes
            with conn.blobopen(table, column, rowid, readonly=False) as blob:
                assert blob.read() == baseline
                blob.seek(0)
                blob.write(changed)
                blob.seek(0)
                assert blob.read() == changed
            assert conn.total_changes == expected_total_changes
            assert (
                adapter.consume_publication_authority_in_transaction(
                    conn,
                    context=context,
                    page=page,
                    decision=decision,
                    refresh=refresh,
                )
                is False
            )
            assert conn.total_changes == expected_total_changes
            raise RuntimeError("roll back blob dependency")

    restored = storage.execute(
        f"SELECT {column} FROM {table} WHERE rowid=?",  # nosec B608 - fixed parametrization
        (rowid,),
    ).fetchone()
    assert restored is not None
    restored_bytes = (
        restored[0].encode("utf-8")
        if isinstance(restored[0], str)
        else bytes(restored[0])
    )
    assert restored_bytes == baseline
    assert raw.raw_content == knowledge.content
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is False
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )


async def test_publication_replay_is_bounded_to_exact_witnessed_operations(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(storage, suffix="bounded-read-set-replay", query="R8EBOUNDEDREPLAY")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="bounded-read-set-replay",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8EBOUNDEDREPLAY"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    ledger = refresh._dependency_ledger  # noqa: SLF001
    assert ledger is not None
    witnesses = ledger._read_set._witnesses  # noqa: SLF001
    assert witnesses
    assert len(witnesses) <= memory_exact_internal._PROVIDER_READ_SET_MAX_OPERATIONS  # noqa: SLF001
    expected_replays = [
        (witness._kind, witness._arguments)  # noqa: SLF001
        for witness in witnesses
        if witness._kind in memory_exact_internal._PROVIDER_STORAGE_READ_KINDS  # noqa: SLF001
    ]
    assert expected_replays
    expected_graph_replays = [
        witness._arguments  # noqa: SLF001
        for witness in witnesses
        if witness._kind == "graph_search_entities"  # noqa: SLF001
    ]
    assert expected_graph_replays

    import friday.storage._memory_exact_internal as memory_storage

    released_replay = memory_storage._replay_memory_exact_provider_read_in_transaction
    replayed: list[tuple[str, tuple[object, ...]]] = []
    released_entity_search = KnowledgeGraph.search_entities
    graph_replayed: list[tuple[object, ...]] = []

    def replay_spy(
        conn: sqlite3.Connection,
        *,
        allow_active_managed_context: bool,
        kind: str,
        arguments: tuple[object, ...],
        reserve_bytes: Any,
    ) -> object:
        replayed.append((kind, arguments))
        return released_replay(
            conn,
            allow_active_managed_context=allow_active_managed_context,
            kind=kind,
            arguments=arguments,
            reserve_bytes=reserve_bytes,
        )

    def graph_replay_spy(
        graph: KnowledgeGraph,
        user_id: str,
        query: str,
        *,
        limit: int = 10,
        entity_type: Any = None,
    ) -> list[dict[str, Any]]:
        graph_replayed.append((user_id, query, limit, entity_type))
        return released_entity_search(
            graph,
            user_id,
            query,
            limit=limit,
            entity_type=entity_type,
        )

    async def forbidden_provider(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("HybridSearcher ran during synchronous publication replay")

    monkeypatch.setattr(
        memory_storage,
        "_replay_memory_exact_provider_read_in_transaction",
        replay_spy,
    )
    monkeypatch.setattr(KnowledgeGraph, "search_entities", graph_replay_spy)
    monkeypatch.setattr(HybridSearcher, "search", forbidden_provider)
    traced_sql: list[str] = []
    with storage.transaction() as conn:
        conn.set_trace_callback(traced_sql.append)
        try:
            assert (
                adapter.consume_publication_authority_in_transaction(
                    conn,
                    context=context,
                    page=page,
                    decision=decision,
                    refresh=refresh,
                )
                is True
            )
        finally:
            conn.set_trace_callback(None)

    # One synchronous storage/graph replay per unique finalized witness, in the
    # exact sealed order. No HybridSearcher/model/network callback can widen it.
    assert replayed == expected_replays
    assert graph_replayed == expected_graph_replays
    assert len(replayed) == len(set(replayed))
    assert all(type(arguments) is tuple for _kind, arguments in replayed)

    graph_cap_reads = [
        " ".join(statement.casefold().split())
        for statement in traced_sql
        if "from entities e" in " ".join(statement.casefold().split())
        and "order by e.id limit 401" in " ".join(statement.casefold().split())
    ]
    assert len(graph_cap_reads) == len(expected_graph_replays)
    assert all("e.id>''" in statement for statement in graph_cap_reads)

    dependency_reads: list[str] = []
    for statement in traced_sql:
        normalized = " ".join(statement.casefold().split())
        if not normalized.startswith(("select", "with")):
            continue
        if any(marker in normalized for marker in _PROVIDER_SOURCE_SQL_MARKERS):
            dependency_reads.append(normalized)
    assert dependency_reads
    # A former corpus fingerprint selected dependency tables without an exact
    # key/range. Every remaining source read is fenced by a witnessed WHERE or
    # a code-owned VALUES keyset; there is no whole dependency-table sweep.
    assert all(
        " where " in statement or " values " in statement
        for statement in dependency_reads
    )


async def test_fts_capability_change_after_authorized_refresh_is_burned(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(storage, suffix="fts-capability", query="R8EFTSCAPABILITY")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="fts-capability",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8EFTSCAPABILITY"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert decision.authorized is True
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    live_fts_available = storage._fts_available  # noqa: SLF001
    monkeypatch.setattr(storage, "_fts_available", not live_fts_available)

    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.authorized is False


async def test_storage_generation_change_after_authorized_refresh_is_burned(
    storage: Any,
) -> None:
    _seed(storage, suffix="storage-generation", query="R8ESTORAGEGENERATION")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="storage-generation",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8ESTORAGEGENERATION"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert decision.authorized is True
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    origin_conn = storage.conn
    origin_generation = storage._generation  # noqa: SLF001
    storage.close()
    assert storage._generation == origin_generation + 1  # noqa: SLF001

    with storage.transaction() as conn:
        assert conn is not origin_conn
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.authorized is False


async def test_storage_generation_change_during_awaited_provider_is_refused(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(storage, suffix="provider-generation", query="R8EPROVIDERGENERATION")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="provider-generation",
    )
    released_search = HybridSearcher.search
    origin_generation = storage._generation  # noqa: SLF001

    async def close_after_ranking(
        provider: HybridSearcher,
        *args: object,
        **kwargs: Any,
    ) -> dict[str, Any]:
        result = await released_search(provider, *args, **kwargs)
        storage.close()
        return result

    monkeypatch.setattr(HybridSearcher, "search", close_after_ranking)
    with pytest.raises(MemoryExactInternalError):
        await adapter.prepare(
            context=context,
            request=_request(actor, context, "R8EPROVIDERGENERATION"),
        )
    assert storage._generation == origin_generation + 1  # noqa: SLF001


@pytest.mark.parametrize("damage", ("unreadable", "missing"))
async def test_provider_dependency_ledger_damage_is_burned(
    storage: Any,
    damage: str,
) -> None:
    _seed(storage, suffix=f"dependency-{damage}", query="R8EDEPENDENCYLEDGER")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"dependency-{damage}",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8EDEPENDENCYLEDGER"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert decision.authorized is True
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    if damage == "unreadable":
        ledger = refresh._dependency_ledger  # noqa: SLF001
        assert ledger is not None
        assert "R8EDEPENDENCYLEDGER" not in repr(ledger)
        assert TENANT not in repr(ledger)
        assert str(storage.settings.database_path) not in repr(ledger)
        assert ledger._observer is adapter._dependency_observer  # noqa: SLF001
        ledger._observer.close()  # noqa: SLF001
    else:
        object.__setattr__(refresh, "_dependency_ledger", None)

    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert decision.authorized is False
    assert decision.to_public_payload()["authorized"] is False


@pytest.mark.parametrize("mutation", ("link-status", "link-review", "merge"))
async def test_link_review_or_merge_change_after_authorized_refresh_is_burned(
    storage: Any,
    mutation: str,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix=f"late-topology-{mutation}",
        query="R8ELATETOPOLOGY",
        body="R8ELATETOPOLOGY links LateAlphaR8E with LateBetaR8E.",
    )
    alpha = Entity(new_id("ent"), TENANT, "LateAlphaR8E", EntityType.PERSON)
    beta = Entity(new_id("ent"), TENANT, "LateBetaR8E", EntityType.ORGANIZATION)
    gamma = Entity(new_id("ent"), TENANT, "LateGammaR8E", EntityType.ORGANIZATION)
    for entity in (alpha, beta, gamma):
        storage.create_entity(entity)
    alpha_link = storage.link_knowledge_entity(
        TENANT,
        knowledge.id,
        alpha.id,
        status="accepted",
        evidence={"basis": "late-topology"},
        reviewed_by=PRINCIPAL,
    )
    storage.link_knowledge_entity(
        TENANT,
        knowledge.id,
        beta.id,
        status="accepted",
        evidence={"basis": "late-topology"},
        reviewed_by=PRINCIPAL,
    )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"late-topology-{mutation}",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "как связан LateAlphaR8E с LateBetaR8E"),
    )
    assert page.graph_projection.relations
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    if mutation == "link-status":
        storage.execute(
            "UPDATE knowledge_entity_links SET status='rejected' WHERE id=?",
            (alpha_link["id"],),
        )
        storage.commit()
    elif mutation == "link-review":
        storage.execute(
            """UPDATE knowledge_entity_links
                  SET reviewed_by=?,reviewed_at=?,evidence_json=? WHERE id=?""",
            (
                OTHER_PRINCIPAL,
                "2026-09-01T11:00:00+00:00",
                json.dumps({"basis": "changed-review"}),
                alpha_link["id"],
            ),
        )
        storage.commit()
    else:
        storage.merge_entities(TENANT, alpha.id, gamma.id, merged_by=PRINCIPAL)
    changed = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert changed.status is not MemoryExactPublicationStatus.AUTHORIZED
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.to_public_payload()["authorized"] is False
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )


@pytest.mark.parametrize("source", ("knowledge", "raw", "deleted"))
async def test_exact_selected_revision_or_source_drift_refuses_publication(
    storage: Any,
    source: str,
) -> None:
    raw, knowledge = _seed(
        storage,
        suffix=f"drift-{source}",
        query="R8EDRIFTNEEDLE",
    )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"drift-{source}",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8EDRIFTNEEDLE"),
    )
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    if source == "knowledge":
        storage.execute(
            "UPDATE knowledge_objects SET title=?,version=version+1,updated_at=? WHERE id=?",
            ("Changed title", "2026-09-01T09:00:00+00:00", knowledge.id),
        )
    elif source == "raw":
        storage.execute(
            "UPDATE raw_objects SET content_hash=?,version=version+1 WHERE id=?",
            ("f" * 64, raw.id),
        )
    else:
        storage.execute(
            "UPDATE knowledge_objects SET deleted_at=? WHERE id=?",
            ("2026-09-01T09:00:00+00:00", knowledge.id),
        )
    storage.commit()

    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.DRIFTED
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.to_public_payload()["authorized"] is False


@pytest.mark.parametrize("graph_source", ("entity", "relation"))
async def test_exact_graph_source_drift_refuses_publication(
    storage: Any,
    graph_source: str,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix=f"graph-drift-{graph_source}",
        query="R8EGRAPHDRIFT",
        body="R8EGRAPHDRIFT says DriftAlphaR8E is connected with DriftBetaR8E.",
    )
    alpha = Entity(new_id("ent"), TENANT, "DriftAlphaR8E", EntityType.PERSON)
    beta = Entity(new_id("ent"), TENANT, "DriftBetaR8E", EntityType.ORGANIZATION)
    storage.create_entity(alpha)
    storage.create_entity(beta)
    for entity in (alpha, beta):
        storage.link_knowledge_entity(
            user_id=TENANT,
            knowledge_object_id=knowledge.id,
            entity_id=entity.id,
            status="accepted",
        )
    relation = Relation(
        new_id("rel"),
        TENANT,
        alpha.id,
        beta.id,
        RelationType.RELATED_TO,
        weight=0.8,
        valid_from="2020-01-01",
        metadata_json={"evidence": {"knowledge_object_id": knowledge.id}},
    )
    storage.create_relation(relation)
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"graph-drift-{graph_source}",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(
            actor,
            context,
            "как связан DriftAlphaR8E с DriftBetaR8E",
            as_of="2022-01-01",
        ),
    )
    assert page.graph_projection.relations
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    if graph_source == "entity":
        storage.execute(
            "UPDATE entities SET name=?,version=version+1,updated_at=? WHERE id=?",
            ("DriftAlphaChangedR8E", "2026-09-01T09:00:00+00:00", alpha.id),
        )
    else:
        storage.execute(
            "UPDATE relations SET weight=? WHERE id=?",
            (0.4, relation.id),
        )
    storage.commit()

    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    assert refresh.status is MemoryExactPublicationStatus.DRIFTED
    with storage.transaction() as conn:
        assert (
            adapter.consume_publication_authority_in_transaction(
                conn,
                context=context,
                page=page,
                decision=decision,
                refresh=refresh,
            )
            is False
        )
    assert decision.to_public_payload()["authorized"] is False


async def test_foreign_provider_candidate_is_never_projected(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(storage, suffix="local-provider", query="R8EPROVIDERTENANT")
    _foreign_raw, foreign = _seed(
        storage,
        suffix="foreign-provider",
        query="R8EPROVIDERTENANT",
        tenant=FOREIGN_TENANT,
    )
    _authorization, actor, context, searcher, adapter = _stack(
        storage,
        label="foreign-provider",
    )
    genuine = await searcher.search(
        actor.user_id,
        "R8EPROVIDERTENANT",
        limit=10,
        include_entities=True,
        kg=KnowledgeGraph(storage),
        graph_expansion=False,
        record_usage=False,
    )
    hostile = copy.deepcopy(genuine)
    hostile["results"] = [{"id": foreign.id}]
    hostile["count"] = 1
    hostile["matched_at_least"] = 1

    async def forged(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return hostile

    monkeypatch.setattr(HybridSearcher, "search", forged)
    request = _request(actor, context, "R8EPROVIDERTENANT")
    with pytest.raises((MemoryExactStorageError, MemoryExactInternalError)):
        await adapter.prepare(context=context, request=request)


async def test_provider_cannot_claim_an_exactly_nonempty_date_window_is_empty(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix="forged-empty-window",
        query="R8EFORGEDWINDOWEMPTY",
    )
    storage.execute(
        "UPDATE knowledge_objects SET metadata_json=? WHERE id=?",
        (json.dumps({"document_date": "2025-05-01"}), knowledge.id),
    )
    storage.commit()
    _authorization, actor, context, searcher, adapter = _stack(
        storage,
        label="forged-empty-window",
    )
    genuine = await searcher.search(
        actor.user_id,
        "R8EFORGEDWINDOWEMPTY",
        limit=10,
        include_entities=True,
        kg=KnowledgeGraph(storage),
        graph_expansion=False,
        since="2025-01-01",
        record_usage=False,
    )
    legacy_ids = [str(item["id"]) for item in genuine["results"]]
    assert legacy_ids
    assert knowledge.id in legacy_ids
    hostile = copy.deepcopy(genuine)
    hostile["results"] = []
    hostile["count"] = 0
    hostile["matched_at_least"] = 0
    hostile["strategy"] = {"date_window": True, "date_window_empty": True}

    async def forged(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return hostile

    monkeypatch.setattr(HybridSearcher, "search", forged)
    with pytest.raises(MemoryExactStorageError, match="coverage is inconsistent"):
        await adapter.prepare(
            context=context,
            request=_request(
                actor,
                context,
                "R8EFORGEDWINDOWEMPTY",
                since="2025-01-01",
            ),
        )


async def test_provider_oom_fails_closed_without_private_exception_text(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="provider-resource",
    )
    private = "PRIVATE-OOM-QUERY-AND-BODY-CANARY"

    async def exhausted(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise MemoryError(private)

    monkeypatch.setattr(HybridSearcher, "search", exhausted)
    with pytest.raises(MemoryExactInternalError) as captured:
        await adapter.prepare(
            context=context,
            request=_request(actor, context, private),
        )
    assert private not in str(captured.value)


async def test_provider_internal_error_is_replaced_with_a_fixed_body_free_error(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="provider-private-internal-error",
    )
    private = "PRIVATE-PROVIDER-INTERNAL-QUERY-AND-BODY-CANARY"

    async def failed(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise MemoryExactInternalError(private)

    monkeypatch.setattr(HybridSearcher, "search", failed)
    with pytest.raises(MemoryExactInternalError) as captured:
        await adapter.prepare(
            context=context,
            request=_request(actor, context, "R8EPRIVATEPROVIDERERROR"),
        )
    assert str(captured.value) == "memory-exact provider is unavailable"
    assert private not in repr(captured.value)


async def test_provider_pool_has_a_real_aggregate_envelope_before_full_row_reads(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = "R8EREALAGGREGATE " + ("x" * 950_000)
    for index in range(9):
        _seed(
            storage,
            suffix=f"real-aggregate-{index}",
            query="R8EREALAGGREGATE",
            body=f"{body}{index}",
        )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="real-aggregate",
    )

    def forbidden_legacy_materialization(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("legacy full-row materialization escaped the bounded facade")

    monkeypatch.setattr(FridayStorage, "search_knowledge", forbidden_legacy_materialization)
    monkeypatch.setattr(FridayStorage, "list_knowledge_objects", forbidden_legacy_materialization)
    with pytest.raises(MemoryExactInternalError, match="aggregate byte bound"):
        await adapter.prepare(
            context=context,
            request=_request(actor, context, "R8EREALAGGREGATE"),
        )


@pytest.mark.parametrize(
    ("field", "forged_value"),
    (
        ("content", "PRIVATE-FORGED-PROVIDER-BODY"),
        ("title", "PRIVATE-FORGED-PROVIDER-TITLE"),
        ("lifecycle_stage", "deprecated"),
    ),
)
async def test_valid_local_provider_row_cannot_forge_ranked_material(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    forged_value: str,
) -> None:
    _seed(storage, suffix=f"provider-forge-{field}", query="R8ELOCALFORGE")
    _authorization, actor, context, searcher, adapter = _stack(
        storage,
        label=f"provider-forge-{field}",
    )
    genuine = await searcher.search(
        actor.user_id,
        "R8ELOCALFORGE",
        limit=10,
        include_entities=True,
        kg=KnowledgeGraph(storage),
        graph_expansion=False,
        record_usage=False,
    )
    assert genuine["results"]
    hostile = copy.deepcopy(genuine)
    hostile["results"][0][field] = forged_value

    async def forged(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return hostile

    monkeypatch.setattr(HybridSearcher, "search", forged)
    with pytest.raises(MemoryExactInternalError) as captured:
        await adapter.prepare(
            context=context,
            request=_request(actor, context, "R8ELOCALFORGE"),
        )
    assert forged_value not in str(captured.value)


@pytest.mark.parametrize(
    ("assignment", "value"),
    (
        ("content=?", "R8EPROVIDERTOCTOU changed body"),
        ("title=?", "Changed provider-time title"),
        ("lifecycle_stage=?", "archived"),
    ),
)
async def test_provider_to_storage_revision_race_fails_closed(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    assignment: str,
    value: str,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix=f"provider-toctou-{assignment}",
        query="R8EPROVIDERTOCTOU",
    )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"provider-toctou-{assignment}",
    )
    released_search = HybridSearcher.search

    async def mutate_after_ranking(
        provider: HybridSearcher,
        *args: object,
        **kwargs: Any,
    ) -> dict[str, Any]:
        result = await released_search(provider, *args, **kwargs)
        storage.execute(
            f"""UPDATE knowledge_objects
                   SET {assignment},version=version+1,updated_at=? WHERE id=?""",  # nosec B608
            (value, "2026-09-01T10:00:00+00:00", knowledge.id),
        )
        storage.commit()
        return result

    monkeypatch.setattr(HybridSearcher, "search", mutate_after_ranking)
    with pytest.raises(MemoryExactStorageDrift, match="changed after ranking"):
        await adapter.prepare(
            context=context,
            request=_request(actor, context, "R8EPROVIDERTOCTOU"),
        )


async def test_oversized_date_metadata_makes_exact_window_unavailable(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix="date-metadata-bound",
        query="R8EDATEMETABOUND",
    )
    storage.execute(
        "UPDATE knowledge_objects SET metadata_json=? WHERE id=?",
        (json.dumps({"document_date": "2020-01-01"}), knowledge.id),
    )
    storage.commit()
    _authorization, actor, context, searcher, adapter = _stack(
        storage,
        label="date-metadata-bound",
    )
    empty = await searcher.search(
        actor.user_id,
        "R8EDATEMETABOUND",
        limit=10,
        include_entities=True,
        kg=KnowledgeGraph(storage),
        graph_expansion=False,
        since="2025-01-01",
        record_usage=False,
    )
    assert empty["results"] == []
    assert empty["strategy"]["date_window_empty"] is True
    oversized_metadata = json.dumps({"document_date": "2025-05-01", "padding": "x" * 512})
    storage.execute(
        "UPDATE knowledge_objects SET metadata_json=? WHERE id=?",
        (oversized_metadata, knowledge.id),
    )
    storage.commit()

    async def stale_empty(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return empty

    def forbidden_source_scan(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("source bodies were read before date metadata refusal")

    import friday.storage._memory_exact_internal as memory_storage

    # Keep the row inside the independent privacy envelope while proving that
    # this lane refuses metadata above its own classification envelope.
    monkeypatch.setattr(memory_storage, "MEMORY_EXACT_MAX_METADATA_UTF8_BYTES", 256)
    monkeypatch.setattr(HybridSearcher, "search", stale_empty)
    monkeypatch.setattr(memory_storage, "_scan_material", forbidden_source_scan)
    with pytest.raises(MemoryExactStorageError, match="classification bound"):
        await adapter.prepare(
            context=context,
            request=_request(
                actor,
                context,
                "R8EDATEMETABOUND",
                since="2025-01-01",
            ),
        )


@pytest.mark.parametrize(
    "metadata_json",
    (
        json.dumps({"document_date": "2025-05-01", "padding": "x" * 512}),
        '{"document_date":"2025-05-01","document_date":"2025-05-02"}',
    ),
    ids=("oversized", "duplicate-date-key"),
)
async def test_live_date_provider_refuses_metadata_before_legacy_sql_or_material_fetch(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    metadata_json: str,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix="live-date-preflight",
        query="R8ELIVEDATEPREFLIGHT",
    )
    storage.execute(
        "UPDATE knowledge_objects SET metadata_json=? WHERE id=?",
        (metadata_json, knowledge.id),
    )
    storage.commit()
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"live-date-preflight-{hashlib.sha256(metadata_json.encode()).hexdigest()[:8]}",
    )
    calls: list[str] = []

    def forbidden_legacy_date_sql(*_args: object, **_kwargs: object) -> object:
        calls.append("legacy-date-sql")
        raise AssertionError("legacy date SQL ran before provider metadata refusal")

    def forbidden_material_fetch(*_args: object, **_kwargs: object) -> object:
        calls.append("material-fetch")
        raise AssertionError("provider material was fetched before date metadata refusal")

    import friday.storage._memory_exact_internal as memory_storage

    # Keep the row privacy-eligible: this test exercises the exact-provider
    # classification envelope, not the wider generic privacy envelope.
    monkeypatch.setattr(memory_storage, "MEMORY_EXACT_MAX_METADATA_UTF8_BYTES", 256)
    monkeypatch.setattr(FridayStorage, "knowledge_ids_in_window", forbidden_legacy_date_sql)
    monkeypatch.setattr(
        memory_storage,
        "_load_memory_exact_provider_rows_in_transaction",
        forbidden_material_fetch,
    )
    with pytest.raises(MemoryExactInternalError, match="provider is unavailable"):
        await adapter.prepare(
            context=context,
            request=_request(
                actor,
                context,
                "R8ELIVEDATEPREFLIGHT",
                since="2025-01-01",
            ),
        )
    assert calls == []


@pytest.mark.parametrize(
    ("assignment", "field", "dynamic_type"),
    (
        ("version=CAST(zeroblob(?) AS TEXT)", "version", "text"),
        ("quality_score=zeroblob(?)", "quality_score", "blob"),
    ),
    ids=("text-version", "blob-score"),
)
async def test_huge_dynamic_provider_fields_fail_preflight_before_materialization(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    assignment: str,
    field: str,
    dynamic_type: str,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix=f"dynamic-{dynamic_type}",
        query="R8EDYNAMICPREFLIGHT",
    )
    storage.execute("PRAGMA ignore_check_constraints=ON")
    storage.execute(
        f"UPDATE knowledge_objects SET {assignment} WHERE id=?",  # nosec B608
        (5 * 1024 * 1024, knowledge.id),
    )
    storage.commit()
    storage.execute("PRAGMA ignore_check_constraints=OFF")
    stored_type = storage.execute(
        f"SELECT typeof({field}) FROM knowledge_objects WHERE id=?",  # nosec B608
        (knowledge.id,),
    ).fetchone()[0]
    assert stored_type == dynamic_type
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label=f"dynamic-preflight-{dynamic_type}",
    )
    materialized = False

    def forbidden_materialization(*_args: object, **_kwargs: object) -> object:
        nonlocal materialized
        materialized = True
        raise AssertionError("a dynamically oversized provider row was materialized")

    import friday.storage._memory_exact_internal as memory_storage

    monkeypatch.setattr(memory_storage, "_stored_material", forbidden_materialization)
    with pytest.raises(MemoryExactInternalError, match="provider is unavailable"):
        await adapter.prepare(
            context=context,
            request=_request(actor, context, "R8EDYNAMICPREFLIGHT"),
        )
    assert materialized is False


async def test_oversized_provider_identity_is_refused_before_material_fetch(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw, _knowledge = _seed(
        storage,
        suffix="oversized-provider-identity",
        query="R8EOVERSIZEDPROVIDERIDENTITY",
        knowledge_id="k" * 241,
    )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="oversized-provider-identity",
    )
    materialized = False

    def forbidden_materialization(*_args: object, **_kwargs: object) -> object:
        nonlocal materialized
        materialized = True
        raise AssertionError("an oversized provider identity reached material fetch")

    import friday.storage._memory_exact_internal as memory_storage

    monkeypatch.setattr(
        memory_storage,
        "_load_memory_exact_provider_rows_in_transaction",
        forbidden_materialization,
    )
    with pytest.raises(MemoryExactInternalError, match="provider is unavailable"):
        await adapter.prepare(
            context=context,
            request=_request(actor, context, "R8EOVERSIZEDPROVIDERIDENTITY"),
        )
    assert materialized is False


async def test_provider_snapshot_oversize_fails_before_any_source_read(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _authorization, actor, context, searcher, adapter = _stack(
        storage,
        label="provider-oversize",
    )
    provider = await searcher.search(
        actor.user_id,
        "R8EPROVIDEROVERSIZE",
        limit=10,
        include_entities=True,
        kg=KnowledgeGraph(storage),
        graph_expansion=False,
        record_usage=False,
    )
    provider["results"] = [{"id": f"ko_provider_oversize_{index}"} for index in range(11)]
    provider["count"] = 11
    provider["matched_at_least"] = 11

    async def oversized(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return provider

    monkeypatch.setattr(HybridSearcher, "search", oversized)
    with pytest.raises(MemoryExactInternalError, match="snapshot is invalid"):
        await adapter.prepare(
            context=context,
            request=_request(
                actor,
                context,
                "R8EPROVIDEROVERSIZE",
                page_size=10,
                snapshot_limit=10,
            ),
        )


async def test_signed_cursor_rejects_tamper_and_cross_request_replay(storage: Any) -> None:
    for index in range(3):
        _seed(storage, suffix=f"cursor-{index}", query="R8EHOSTILECURSOR")
    authorization, actor, context, searcher, adapter = _stack(
        storage,
        label="hostile-cursor",
    )
    initial = _request(
        actor,
        context,
        "R8EHOSTILECURSOR",
        page_size=1,
        snapshot_limit=3,
    )
    first = await adapter.prepare(context=context, request=initial)
    assert first.next_continuation is not None
    token = first.next_continuation.token
    replacement = "A" if token[0] != "A" else "B"
    tampered = MemoryExactContinuation.create(replacement + token[1:])
    raw_envelope = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("ascii")
    envelope = json.loads(raw_envelope)
    payload_text = json.dumps(
        envelope["payload"],
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )

    def opaque(text: str) -> MemoryExactContinuation:
        encoded = base64.urlsafe_b64encode(text.encode("ascii")).rstrip(b"=").decode("ascii")
        return MemoryExactContinuation.create(encoded)

    duplicate = opaque(
        f'{{"payload":{payload_text},"signature":"{envelope["signature"]}",'
        f'"signature":"{envelope["signature"]}"}}'
    )
    nonfinite = opaque(f'{{"payload":NaN,"signature":"{envelope["signature"]}"}}')
    noncanonical = opaque(f'{{ "payload": {payload_text}, "signature": "{envelope["signature"]}" }}')
    hostile_requests = (
        _request(
            actor,
            context,
            "R8EHOSTILECURSOR",
            page_size=1,
            snapshot_limit=3,
            continuation=tampered,
        ),
        _request(
            actor,
            context,
            "R8EHOSTILECURSOR",
            page_size=1,
            snapshot_limit=3,
            continuation=duplicate,
        ),
        _request(
            actor,
            context,
            "R8EHOSTILECURSOR",
            page_size=1,
            snapshot_limit=3,
            continuation=nonfinite,
        ),
        _request(
            actor,
            context,
            "R8EHOSTILECURSOR",
            page_size=1,
            snapshot_limit=3,
            continuation=noncanonical,
        ),
        _request(
            actor,
            context,
            "R8EHOSTILECURSOR-CHANGED",
            page_size=1,
            snapshot_limit=3,
            continuation=first.next_continuation,
        ),
        _request(
            actor,
            context,
            "R8EHOSTILECURSOR",
            page_size=2,
            snapshot_limit=3,
            continuation=first.next_continuation,
        ),
    )
    for request in hostile_requests:
        with pytest.raises(MemoryExactStorageError):
            await adapter.prepare(context=context, request=request)

    changed_issuer, changed_context = _turn(actor, label="hostile-cursor")
    assert changed_context.turn_id == context.turn_id
    assert changed_context.context_authority_sha256 != context.context_authority_sha256
    changed_adapter = MemoryExactInternalAdapter(
        authorization,
        changed_issuer,
        storage,
        searcher,
        KnowledgeGraph(storage),
    )
    changed_authority_request = _request(
        actor,
        changed_context,
        "R8EHOSTILECURSOR",
        page_size=1,
        snapshot_limit=3,
        continuation=first.next_continuation,
    )
    with pytest.raises(MemoryExactStorageError):
        await changed_adapter.prepare(
            context=changed_context,
            request=changed_authority_request,
        )

    foreign_turn_issuer, foreign_turn_context = _turn(actor, label="hostile-cursor-foreign-turn")
    foreign_turn_adapter = MemoryExactInternalAdapter(
        authorization,
        foreign_turn_issuer,
        storage,
        HybridSearcher(storage, record_usage=False),
        KnowledgeGraph(storage),
    )
    with pytest.raises(MemoryExactStorageError):
        await foreign_turn_adapter.prepare(
            context=foreign_turn_context,
            request=_request(
                actor,
                foreign_turn_context,
                "R8EHOSTILECURSOR",
                page_size=1,
                snapshot_limit=3,
                continuation=first.next_continuation,
            ),
        )

    (
        other_authorization,
        other_actor,
        other_context,
        _other_searcher,
        other_adapter,
    ) = _stack(storage, label="hostile-cursor-other-person", principal=OTHER_PRINCIPAL)
    del other_authorization
    with pytest.raises(MemoryExactStorageError):
        await other_adapter.prepare(
            context=other_context,
            request=_request(
                other_actor,
                other_context,
                "R8EHOSTILECURSOR",
                page_size=1,
                snapshot_limit=3,
                continuation=first.next_continuation,
            ),
        )

    (
        foreign_authorization,
        foreign_actor,
        foreign_context,
        _foreign_searcher,
        foreign_adapter,
    ) = _stack(storage, label="hostile-cursor-other-tenant", tenant=FOREIGN_TENANT)
    del foreign_authorization
    with pytest.raises(MemoryExactStorageError):
        await foreign_adapter.prepare(
            context=foreign_context,
            request=_request(
                foreign_actor,
                foreign_context,
                "R8EHOSTILECURSOR",
                page_size=1,
                snapshot_limit=3,
                continuation=first.next_continuation,
            ),
        )

    storage.execute("UPDATE users SET preset_key='user' WHERE id=?", (actor.own_id,))
    storage.commit()
    try:
        with pytest.raises(MemoryExactStorageError):
            await adapter.prepare(
                context=context,
                request=_request(
                    actor,
                    context,
                    "R8EHOSTILECURSOR",
                    page_size=1,
                    snapshot_limit=3,
                    continuation=first.next_continuation,
                ),
            )
    finally:
        storage.execute("UPDATE users SET preset_key='owner' WHERE id=?", (actor.own_id,))
        storage.commit()


async def test_cursor_envelope_contains_no_query_body_or_raw_identity(storage: Any) -> None:
    raw, knowledge = _seed(
        storage,
        suffix="cursor-private",
        query="R8ECURSORPRIVATE",
        body="R8ECURSORPRIVATE " + ("secret-body " * 100),
    )
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="cursor-private",
    )
    request = _request(
        actor,
        context,
        "R8ECURSORPRIVATE",
        page_size=1,
        snapshot_limit=2,
    )
    _seed(storage, suffix="cursor-private-second", query="R8ECURSORPRIVATE")
    page = await adapter.prepare(context=context, request=request)
    assert page.next_continuation is not None
    decoded = base64.urlsafe_b64decode(
        page.next_continuation.token + "=" * (-len(page.next_continuation.token) % 4)
    ).decode("ascii")
    for private in (
        request.query,
        raw.id,
        knowledge.id,
        raw.raw_content,
        actor.user_id,
        actor.own_id,
    ):
        assert private not in decoded
        assert private not in repr(page)
        assert private not in repr(page.next_continuation)


async def test_temporal_provider_cannot_reintroduce_present_day_implicit_edges(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw, knowledge = _seed(
        storage,
        suffix="temporal-implicit",
        query="R8EIMPLICITGRAPH",
        body="R8EIMPLICITGRAPH mentions ImplicitAlphaR8E and ImplicitBetaR8E.",
    )
    alpha = Entity(new_id("ent"), TENANT, "ImplicitAlphaR8E", EntityType.PERSON)
    beta = Entity(new_id("ent"), TENANT, "ImplicitBetaR8E", EntityType.ORGANIZATION)
    storage.create_entity(alpha)
    storage.create_entity(beta)
    for entity in (alpha, beta):
        storage.link_knowledge_entity(
            user_id=TENANT,
            knowledge_object_id=knowledge.id,
            entity_id=entity.id,
            status="accepted",
        )
    _authorization, actor, context, searcher, adapter = _stack(
        storage,
        label="temporal-implicit",
    )
    current = await searcher.search(
        actor.user_id,
        "как связан ImplicitAlphaR8E с ImplicitBetaR8E",
        limit=5,
        include_entities=True,
        kg=KnowledgeGraph(storage),
        graph_expansion=True,
        record_usage=False,
    )
    implicit_relations = [
        item for item in current["graph_context"]["relations"] if item.get("implicit") is True
    ]
    assert implicit_relations, "seed must expose one present-day cooccurrence edge"
    hostile = copy.deepcopy(current)
    hostile["as_of"] = "2022-01-01"
    hostile["graph_context"]["as_of"] = "2022-01-01"

    async def forged(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return hostile

    monkeypatch.setattr(HybridSearcher, "search", forged)
    request = _request(
        actor,
        context,
        "как связан ImplicitAlphaR8E с ImplicitBetaR8E",
        as_of="2022-01-01",
        page_size=5,
        snapshot_limit=5,
    )
    with pytest.raises(MemoryExactInternalError, match="snapshot is invalid"):
        await adapter.prepare(context=context, request=request)


async def test_tampered_process_private_page_fails_projection_and_publication(storage: Any) -> None:
    _seed(storage, suffix="carrier-tamper", query="R8ECARRIERTAMPER")
    _authorization, actor, context, _searcher, adapter = _stack(
        storage,
        label="carrier-tamper",
    )
    page = await adapter.prepare(
        context=context,
        request=_request(actor, context, "R8ECARRIERTAMPER"),
    )
    object.__setattr__(page.candidates[0], "title", "tampered")
    with pytest.raises(MemoryExactInternalError, match="private page"):
        adapter.project_for_model(context=context, page=page)
    with pytest.raises(MemoryExactInternalError, match="private page"):
        await adapter.reauthorize_for_publication(context=context, page=page)
