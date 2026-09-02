"""Seeded parity evidence for the authenticated exact-memory internal lane.

The released ``HybridSearcher`` remains the ranking oracle.  These tests compare
that oracle with the new authenticated adapter while also proving that the
adapter replaces provider rows with transaction-local exact storage revisions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import pickle
import time
from dataclasses import replace
from typing import Any

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.orchestration.contracts import RouterMode, TurnInput
from friday.orchestration.turn_context import (
    AuthenticatedTurnContext,
    IngressKind,
    InheritedTurnBudget,
    ModelAntiLoopBudget,
    TurnContextIssuer,
    TurnMode,
    TurnResourceBudget,
    TurnSafetyDeadline,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval import HybridSearcher, best_snippet, is_relational_query
from friday.retrieval.memory_exact_contract import (
    MEMORY_EXACT_MAX_EXCERPT_CHARS,
    MEMORY_EXACT_MAX_GRAPH_NODES,
    MEMORY_EXACT_MAX_GRAPH_PATH_EDGES,
    MEMORY_EXACT_MAX_GRAPH_PATHS,
    MEMORY_EXACT_MAX_GRAPH_RELATIONS,
    MemoryExactContentCoverage,
    MemoryExactGraphCoverage,
    MemoryExactLifecycleStage,
    MemoryExactPublicationStatus,
    MemoryExactRequest,
    MemoryExactRowCoverage,
)
from friday.retrieval.memory_exact_internal import (
    MEMORY_EXACT_ADAPTER_BINDING,
    MemoryExactAdapterBinding,
    MemoryExactInternalAdapter,
    MemoryExactInternalError,
)
from friday.storage import FridayStorage
from friday.storage.models import (
    Entity,
    EntityType,
    KnowledgeObject,
    RawObject,
    Relation,
    RelationType,
    new_id,
)
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

TENANT = "memory-exact-shared-tenant"
PRINCIPAL = "memory-exact-principal"
BASE_TIME = "2026-08-31T09:00:00+00:00"


def _turn(
    actor: ActorContext,
    *,
    label: str,
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext]:
    now = time.monotonic_ns()
    conversation_id = f"conv_{hashlib.sha256(f'memory-exact:{label}'.encode('ascii')).hexdigest()[:16]}"
    issuer = TurnContextIssuer(
        hashlib.sha256(f"memory-exact:{label}".encode("ascii")).digest(),
        _monotonic_ns=lambda: now,
    )
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"memory-exact-ingress-{label}",
        actor=actor,
        conversation_id=conversation_id,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=f"memory-exact-source-{label}",
        update_id=f"memory-exact-update-{label}",
        request_effect_binding_sha256=hashlib.sha256(label.encode("ascii")).hexdigest(),
    )
    model_input = TurnInput.from_chat(
        message="retrieve authenticated archival memory",
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
) -> tuple[
    AuthorizationService,
    ActorContext,
    TurnContextIssuer,
    AuthenticatedTurnContext,
    HybridSearcher,
    MemoryExactInternalAdapter,
]:
    storage.ensure_user(TENANT, preset_key="owner")
    storage.ensure_user(principal, preset_key="owner")
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    actor = authorization.actor_for_user(principal, source="memory-exact-test")
    issuer, context = _turn(actor, label=label)
    searcher = HybridSearcher(storage, record_usage=False)
    adapter = MemoryExactInternalAdapter(
        authorization,
        issuer,
        storage,
        searcher,
        KnowledgeGraph(storage),
    )
    return authorization, actor, issuer, context, searcher, adapter


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


def _seed_knowledge(
    storage: FridayStorage,
    *,
    suffix: str,
    body: str,
    title: str,
    document_date: str | None = None,
    lifecycle_stage: str = "active",
) -> tuple[RawObject, KnowledgeObject]:
    storage.ensure_user(TENANT, preset_key="owner")
    raw = RawObject(
        id=new_id("raw"),
        user_id=TENANT,
        source="test",
        source_ref=f"memory-exact-ref-{suffix}",
        raw_content=body,
        content_type="text",
        metadata_json={"source_label": suffix},
        content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        received_at=BASE_TIME,
        created_at=BASE_TIME,
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=TENANT,
        raw_object_id=raw.id,
        content=body,
        content_type="text",
        title=title,
        summary="",
        metadata_json={} if document_date is None else {"document_date": document_date},
        knowledge_kind="document",
        lifecycle_stage=lifecycle_stage,
        importance=0.7,
        quality_score=0.8,
        promotion_score=0.8,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    storage.store_knowledge_object(knowledge)
    return raw, knowledge


async def _legacy(
    searcher: HybridSearcher,
    graph: KnowledgeGraph,
    request: MemoryExactRequest,
) -> dict[str, Any]:
    return await searcher.search(
        request.tenant_id,
        request.query,
        limit=request.snapshot_limit,
        include_entities=True,
        kg=graph,
        graph_expansion=bool(request.as_of or request.known_at or is_relational_query(request.query)),
        explain=False,
        since=request.since or None,
        until=request.until or None,
        as_of=request.as_of,
        known_at=request.known_at,
        record_usage=False,
    )


def _legacy_ids(payload: dict[str, Any]) -> list[str]:
    return [str(item["id"]) for item in payload["results"]]


def _legacy_graph_semantics(payload: dict[str, Any]) -> dict[str, object]:
    """Reduce one released graph payload to its ordered model-visible meaning."""

    nodes_by_id: dict[str, tuple[str, str]] = {}
    node_sources = [*payload["nodes"], *payload["roots"]]
    for path in payload["paths"]:
        node_sources.extend(path.get("entities", ()))
    for node in node_sources:
        nodes_by_id[str(node["id"])] = (
            str(node["name"]),
            str(node["entity_type"])[:80],
        )

    def node(identity: object) -> tuple[str, str]:
        return nodes_by_id[str(identity)]

    return {
        "nodes": tuple(node(item["id"]) for item in payload["nodes"]),
        "roots": tuple(node(item["id"]) for item in payload["roots"]),
        "relations": tuple(
            (
                node(item["source_entity_id"]),
                node(item["target_entity_id"]),
                str(item["relation_type"])[:80],
                str(item.get("valid_from") or "") or None,
                item.get("valid_to"),
                bool(item["implicit"]),
            )
            for item in payload["relations"]
        ),
        "paths": tuple(
            tuple(
                (
                    node(edge["from"]),
                    node(edge["to"]),
                    node(edge["source"]),
                    node(edge["target"]),
                    str(edge["type"])[:80],
                    str(edge["direction"]),
                    str(edge.get("valid_from") or "") or None,
                    edge.get("valid_to"),
                    bool(edge["implicit"]),
                )
                for edge in path["edges"]
            )
            for path in payload["paths"]
        ),
    }


def _projected_graph_semantics(payload: dict[str, Any]) -> dict[str, object]:
    """Reduce the ID-free projection to the same ordered semantic shape."""

    nodes_by_alias = {str(item["alias"]): (str(item["name"]), str(item["type"])) for item in payload["nodes"]}

    def node(alias: object) -> tuple[str, str]:
        return nodes_by_alias[str(alias)]

    def assertion_endpoints(edge: dict[str, Any]) -> tuple[tuple[str, str], tuple[str, str]]:
        traversal = (node(edge["from"]), node(edge["to"]))
        return traversal if edge["direction"] == "forward" else (traversal[1], traversal[0])

    return {
        "nodes": tuple(node(item["alias"]) for item in payload["nodes"]),
        "roots": tuple(node(alias) for alias in payload["roots"]),
        "relations": tuple(
            (
                node(item["source"]),
                node(item["target"]),
                str(item["relation"]),
                item["valid_from"],
                item["valid_to"],
                bool(item["implicit"]),
            )
            for item in payload["relations"]
        ),
        "paths": tuple(
            tuple(
                (
                    node(edge["from"]),
                    node(edge["to"]),
                    *assertion_endpoints(edge),
                    str(edge["relation"]),
                    str(edge["direction"]),
                    edge["valid_from"],
                    edge["valid_to"],
                    bool(edge["implicit"]),
                )
                for edge in path["edges"]
            )
            for path in payload["paths"]
        ),
    }


def test_adapter_binding_is_closed_read_only_and_not_model_visible() -> None:
    payload = MEMORY_EXACT_ADAPTER_BINDING.payload()
    assert payload["capability_id"] == "archive.search"
    assert payload["security_ids"] == ["search.use", "knowledge.read"]
    assert payload["effect_class"] == "read"
    assert payload["model_visible"] is False
    assert len(MEMORY_EXACT_ADAPTER_BINDING.canonical_sha256()) == 64

    tampered = MemoryExactAdapterBinding()
    object.__setattr__(tampered, "model_visible", True)
    with pytest.raises(MemoryExactInternalError, match="not closed"):
        tampered.payload()


def test_request_is_canonical_immutable_and_query_free_in_durable_identity(storage: Any) -> None:
    _authorization, actor, _issuer, context, _searcher, _adapter = _stack(
        storage,
        label="request-contract",
    )
    query = "PRIVATE-QUERY-CANARY   exact\n memory"
    request = _request(actor, context, query)
    assert request.query == "PRIVATE-QUERY-CANARY exact memory"
    assert MemoryExactRequest.parse_private(request.to_private_json()) == request
    assert request.query_sha256 in request.to_identity_json()
    assert request.query not in request.to_identity_json()
    assert request.query not in repr(request)
    assert actor.user_id not in repr(request)
    assert actor.own_id not in repr(request)
    assert context.turn_id not in repr(request)

    same = _request(actor, context, "PRIVATE-QUERY-CANARY exact memory")
    changed = _request(actor, context, "PRIVATE-QUERY-CANARY other memory")
    assert same.identity_sha256() == request.identity_sha256()
    assert changed.identity_sha256() != request.identity_sha256()
    with pytest.raises((AttributeError, TypeError)):
        request.query = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "supplied", "expected"),
    (
        ("since", "2023-03", "2023-03-01"),
        ("until", "2023-03", "2023-03-31"),
        ("since", "2024", "2024-01-01"),
        ("until", "2024", "2024-12-31"),
        ("until", "2024-02", "2024-02-29"),
        ("since", "01.03.2023", "2023-03-01"),
        ("until", "31/03/2023", "2023-03-31"),
        ("as_of", "29.02.2024", "2024-02-29"),
        ("as_of", "2024-02-29", "2024-02-29"),
    ),
    ids=(
        "since-month-first-day",
        "until-month-last-day",
        "since-year-first-day",
        "until-year-last-day",
        "until-leap-month-last-day",
        "since-dmy",
        "until-dmy",
        "as-of-dmy",
        "as-of-iso",
    ),
)
def test_temporal_inputs_match_legacy_edge_specific_normalization(
    field: str,
    supplied: str,
    expected: str,
) -> None:
    request = MemoryExactRequest.create(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        active_turn_id=f"turn_{'c' * 64}",
        query="R8ETEMPORALINPUT",
        **{field: supplied},
    )
    assert getattr(request, field) == expected


async def test_seeded_rank_order_and_projection_match_released_legacy_search(storage: Any) -> None:
    _first_raw, first = _seed_knowledge(
        storage,
        suffix="rank-first",
        title="Needle primary",
        body="R8EORDERNEEDLE appears twice. R8EORDERNEEDLE exact primary evidence.",
    )
    _second_raw, second = _seed_knowledge(
        storage,
        suffix="rank-second",
        title="Needle secondary",
        body="R8EORDERNEEDLE secondary evidence.",
    )
    _seed_knowledge(
        storage,
        suffix="rank-noise",
        title="Unrelated",
        body="This row must not enter the exact ranked selection.",
    )
    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="rank-parity",
    )
    request = _request(actor, context, "R8EORDERNEEDLE", page_size=2, snapshot_limit=2)
    legacy = await _legacy(searcher, KnowledgeGraph(storage), request)
    page = await adapter.prepare(context=context, request=request)

    expected = _legacy_ids(legacy)
    assert set(expected) == {first.id, second.id}
    assert [candidate.knowledge_id for candidate in page.candidates] == expected
    assert page.matched_rows == int(legacy["matched_at_least"])
    assert all(len(candidate.revision_sha256) == 64 for candidate in page.candidates)
    assert all(len(candidate.source_handle) == 64 for candidate in page.candidates)
    assert [candidate.excerpt for candidate in page.candidates] == [
        best_snippet(request.query, str(item["content"]), max_chars=600) for item in legacy["results"]
    ]

    projection = adapter.project_for_model(context=context, page=page)
    assert [row.title for row in projection.rows] == [str(item["title"]) for item in legacy["results"]]
    assert projection.row_coverage is MemoryExactRowCoverage.PARTIAL
    assert projection.content_coverage is MemoryExactContentCoverage.COMPLETE
    model_json = projection.to_model_json()
    for private in (
        first.id,
        second.id,
        first.raw_object_id,
        second.raw_object_id,
        actor.user_id,
        actor.own_id,
        context.turn_id,
    ):
        assert private not in model_json


async def test_long_body_keeps_exact_bounded_legacy_excerpt_and_reports_truncation(
    storage: Any,
) -> None:
    body = "R8ELONGBODYNEEDLE " + "x " * 800 + "PRIVATE-END-CANARY"
    _raw, knowledge = _seed_knowledge(
        storage,
        suffix="long-body",
        title="Long bounded memory",
        body=body,
    )
    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="long-body",
    )
    request = _request(actor, context, "R8ELONGBODYNEEDLE", page_size=1, snapshot_limit=1)
    legacy = await _legacy(searcher, KnowledgeGraph(storage), request)
    page = await adapter.prepare(context=context, request=request)

    assert _legacy_ids(legacy) == [knowledge.id]
    assert [candidate.knowledge_id for candidate in page.candidates] == [knowledge.id]
    candidate = page.candidates[0]
    assert (
        candidate.excerpt
        == best_snippet(
            request.query,
            body,
            max_chars=MEMORY_EXACT_MAX_EXCERPT_CHARS,
        )[:MEMORY_EXACT_MAX_EXCERPT_CHARS]
    )
    assert len(candidate.excerpt) == MEMORY_EXACT_MAX_EXCERPT_CHARS
    assert candidate.excerpt_truncated is True
    assert candidate.content_chars == len(body)
    assert candidate.body_sha256 == hashlib.sha256(body.encode("utf-8")).hexdigest()

    projection = adapter.project_for_model(context=context, page=page)
    assert projection.content_coverage is MemoryExactContentCoverage.TRUNCATED
    assert projection.truncated_rows == 1
    assert projection.rows[0].excerpt == candidate.excerpt
    assert projection.rows[0].excerpt_truncated is True
    assert "PRIVATE-END-CANARY" not in projection.to_model_json()
    assert body not in projection.to_model_json()


@pytest.mark.parametrize(
    ("boundary", "value", "expected_title"),
    (
        ("since", "2024-01-01", "Window late"),
        ("until", "2022-12-31", "Window early"),
    ),
    ids=("since-only", "until-only"),
)
async def test_one_sided_date_windows_match_legacy_inclusive_prefilter(
    storage: Any,
    boundary: str,
    value: str,
    expected_title: str,
) -> None:
    _seed_knowledge(
        storage,
        suffix="window-early",
        title="Window early",
        body="R8EWINDOWNEEDLE early evidence",
        document_date="2022-05-03",
    )
    _seed_knowledge(
        storage,
        suffix="window-late",
        title="Window late",
        body="R8EWINDOWNEEDLE late evidence",
        document_date="2024-06-07",
    )
    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label=f"window-{boundary}",
    )
    request = _request(
        actor,
        context,
        "R8EWINDOWNEEDLE",
        page_size=2,
        snapshot_limit=2,
        **{boundary: value},
    )
    legacy = await _legacy(searcher, KnowledgeGraph(storage), request)
    page = await adapter.prepare(context=context, request=request)

    assert [candidate.knowledge_id for candidate in page.candidates] == _legacy_ids(legacy)
    assert [candidate.title for candidate in page.candidates] == [expected_title]
    assert page.temporal_status.as_of is None
    assert page.temporal_status.known_at is None
    assert page.date_window_status.requested is True
    assert page.date_window_status.applied is True
    assert page.date_window_status.empty is False
    assert adapter.project_for_model(
        context=context,
        page=page,
    ).to_model_payload()["date_window"] == {
        "applied": True,
        "empty": False,
        "requested": True,
        "since": request.since,
        "until": request.until,
    }


async def test_lifecycle_subset_revalidates_provider_rows_without_reordering(storage: Any) -> None:
    _raw_active, active = _seed_knowledge(
        storage,
        suffix="lifecycle-active",
        title="Lifecycle active",
        body="R8ELIFECYCLENEEDLE active evidence",
        lifecycle_stage="active",
    )
    _seed_knowledge(
        storage,
        suffix="lifecycle-archived",
        title="Lifecycle archived",
        body="R8ELIFECYCLENEEDLE archived evidence",
        lifecycle_stage="archived",
    )
    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="lifecycle",
    )
    request = _request(
        actor,
        context,
        "R8ELIFECYCLENEEDLE",
        lifecycle_stages=(MemoryExactLifecycleStage.ACTIVE,),
        page_size=2,
        snapshot_limit=2,
    )
    legacy = await _legacy(searcher, KnowledgeGraph(storage), request)
    expected = [str(item["id"]) for item in legacy["results"] if item["lifecycle_stage"] == "active"]
    page = await adapter.prepare(context=context, request=request)

    assert expected == [active.id]
    assert [candidate.knowledge_id for candidate in page.candidates] == expected
    assert page.snapshot_rows == 1
    assert page.matched_rows == 1


async def test_as_of_graph_context_is_bounded_id_free_and_source_bound(storage: Any) -> None:
    _raw, knowledge = _seed_knowledge(
        storage,
        suffix="valid-time-graph",
        title="Opaque valid-time witness",
        body="R8EGRAPHNEEDLE carries source-bound valid-time evidence.",
    )
    alpha = Entity(new_id("ent"), TENANT, "AlphaR8E", EntityType.PERSON)
    beta = Entity(new_id("ent"), TENANT, "BetaR8E", EntityType.ORGANIZATION)
    gamma = Entity(new_id("ent"), TENANT, "GammaR8E", EntityType.PROJECT)
    for entity in (alpha, beta, gamma):
        storage.create_entity(entity)
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
        RelationType.MEMBER_OF,
        weight=1.0,
        valid_from="2020-01-01",
        valid_to="2025-01-01",
        metadata_json={"evidence": {"knowledge_object_id": knowledge.id}},
    )
    storage.create_relation(relation)
    parent_relation = Relation(
        new_id("rel"),
        TENANT,
        beta.id,
        gamma.id,
        RelationType.PART_OF,
        weight=1.0,
        valid_from="2019-01-01",
        valid_to="2026-01-01",
        metadata_json={"evidence": {"knowledge_object_id": knowledge.id}},
    )
    storage.create_relation(parent_relation)

    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="valid-time-graph",
    )
    request = _request(
        actor,
        context,
        "как связан GammaR8E",
        as_of="2022-01-01",
        page_size=2,
        snapshot_limit=2,
    )
    legacy = await _legacy(searcher, KnowledgeGraph(storage), request)
    page = await adapter.prepare(context=context, request=request)
    projection = adapter.project_for_model(context=context, page=page)
    graph = projection.graph_projection

    assert [candidate.knowledge_id for candidate in page.candidates] == _legacy_ids(legacy)
    assert page.temporal_status.as_of == "2022-01-01"
    assert graph.expanded is bool(legacy["graph_context"]["expanded"])
    assert len(graph.nodes) <= MEMORY_EXACT_MAX_GRAPH_NODES
    assert len(graph.relations) <= MEMORY_EXACT_MAX_GRAPH_RELATIONS
    assert len(graph.paths) <= MEMORY_EXACT_MAX_GRAPH_PATHS
    assert all(len(path.edges) <= MEMORY_EXACT_MAX_GRAPH_PATH_EDGES for path in graph.paths)
    assert len(page.graph_source_set_sha256) == 64
    graph_payload = graph.to_model_payload()
    assert graph_payload["query"] == legacy["graph_context"]["query"]
    assert _projected_graph_semantics(graph_payload) == _legacy_graph_semantics(legacy["graph_context"])
    assert {
        (
            item["relation"],
            item["valid_from"],
            item["valid_to"],
        )
        for item in graph_payload["relations"]
    } == {
        ("member_of", "2020-01-01", "2025-01-01"),
        ("part_of", "2019-01-01", "2026-01-01"),
    }
    assert [item["relation"] for item in graph_payload["relations"]] == [
        "member_of",
        "part_of",
    ]
    assert graph_payload["paths"]
    assert [[edge["relation"] for edge in path["edges"]] for path in graph_payload["paths"]] == [
        ["part_of"],
        ["part_of", "member_of"],
    ]
    assert {edge["direction"] for path in graph_payload["paths"] for edge in path["edges"]} == {"reverse"}
    assert all(item["implicit"] is False for item in graph_payload["relations"])
    assert all(item["evidence_basis"] == "relation_row_only" for item in graph_payload["relations"])
    assert all(path["grounded"] is False for path in graph_payload["paths"])
    model_json = projection.to_model_json()
    for private in (
        alpha.id,
        beta.id,
        gamma.id,
        relation.id,
        parent_relation.id,
        knowledge.id,
        knowledge.raw_object_id,
    ):
        assert private not in model_json
    assert {item["alias"] for item in graph_payload["nodes"]} == {
        f"n{index}" for index in range(1, len(graph.nodes) + 1)
    }


async def test_current_implicit_graph_keeps_cooccurrence_and_local_grounding(
    storage: Any,
) -> None:
    _raw, knowledge = _seed_knowledge(
        storage,
        suffix="implicit-grounding",
        title="Opaque witness",
        body="archival omega witness.",
    )
    alpha = Entity(new_id("ent"), TENANT, "ImplicitGroundAlpha", EntityType.PERSON)
    beta = Entity(
        new_id("ent"),
        TENANT,
        "OmegaZetaNode",
        EntityType.ORGANIZATION,
    )
    storage.create_entity(alpha)
    storage.create_entity(beta)
    for entity in (alpha, beta):
        storage.link_knowledge_entity(
            user_id=TENANT,
            knowledge_object_id=knowledge.id,
            entity_id=entity.id,
            status="accepted",
        )
    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="implicit-grounding",
    )
    request = _request(
        actor,
        context,
        "как связан ImplicitGroundAlpha",
        page_size=5,
        snapshot_limit=5,
    )
    legacy = await _legacy(searcher, KnowledgeGraph(storage), request)
    page = await adapter.prepare(
        context=context,
        request=request,
    )
    projection = adapter.project_for_model(context=context, page=page)
    payload = projection.graph_projection.to_model_payload()
    implicit_relations = [item for item in payload["relations"] if item["implicit"]]
    implicit_edges = [edge for path in payload["paths"] for edge in path["edges"] if edge["implicit"]]

    assert knowledge.id in {candidate.knowledge_id for candidate in page.candidates}
    assert any(edge["implicit"] for path in legacy["graph_context"]["paths"] for edge in path["edges"])
    assert implicit_relations
    assert implicit_edges
    assert all(item["evidence_basis"] == "accepted_links" for item in implicit_relations)
    assert all(item["evidence_result_ordinal"] is not None for item in implicit_edges)
    assert any(path["grounded"] is True for path in payload["paths"])
    encoded = projection.to_model_json()
    for private in (knowledge.id, knowledge.raw_object_id, alpha.id, beta.id):
        assert private not in encoded


async def test_adapter_propagates_real_partial_and_unknown_graph_coverage(
    storage: Any,
) -> None:
    _raw, knowledge = _seed_knowledge(
        storage,
        suffix="graph-coverage",
        title="Graph coverage witness",
        body="Archival witness shared by a deliberately saturated entity cluster.",
    )
    entities = [
        Entity(
            new_id("ent"),
            TENANT,
            "CoverageHubR8E" if index == 0 else f"CoverageSatellite{index:02d}R8E",
            EntityType.CONCEPT,
        )
        for index in range(MEMORY_EXACT_MAX_GRAPH_NODES)
    ]
    for entity in entities:
        storage.create_entity(entity)
        storage.link_knowledge_entity(
            TENANT,
            knowledge.id,
            entity.id,
            status="accepted",
        )

    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="graph-coverage",
    )
    request = _request(
        actor,
        context,
        "как связан CoverageHubR8E",
        page_size=5,
        snapshot_limit=5,
    )
    legacy = await _legacy(searcher, KnowledgeGraph(storage), request)
    legacy_graph = legacy["graph_context"]

    assert len(legacy_graph["nodes"]) == MEMORY_EXACT_MAX_GRAPH_NODES
    assert len(legacy_graph["relations"]) == MEMORY_EXACT_MAX_GRAPH_RELATIONS
    assert len(legacy_graph["paths"]) > MEMORY_EXACT_MAX_GRAPH_PATHS
    assert legacy_graph["paths_matched_at_least"] > len(legacy_graph["paths"])
    assert legacy_graph["paths_truncated"] is True

    page = await adapter.prepare(context=context, request=request)
    assert knowledge.id in {candidate.knowledge_id for candidate in page.candidates}
    graph = page.graph_projection
    assert graph.nodes_coverage is MemoryExactGraphCoverage.UNKNOWN
    assert graph.relations_coverage is MemoryExactGraphCoverage.UNKNOWN
    assert graph.paths_coverage is MemoryExactGraphCoverage.PARTIAL

    payload = adapter.project_for_model(
        context=context,
        page=page,
    ).graph_projection.to_model_payload()
    assert payload["nodes_shown"] == payload["nodes_matched_at_least"]
    assert payload["nodes_truncated"] is None
    assert payload["relations_shown"] == payload["relations_matched_at_least"]
    assert payload["relations_truncated"] is None
    assert payload["paths_shown"] == MEMORY_EXACT_MAX_GRAPH_PATHS
    assert payload["paths_matched_at_least"] == legacy_graph["paths_matched_at_least"]
    assert payload["paths_truncated"] is True


async def test_reviewed_relation_without_knowledge_anchor_stays_ungrounded(
    storage: Any,
) -> None:
    _raw, knowledge = _seed_knowledge(
        storage,
        suffix="reviewed-without-anchor",
        title="Reviewed relation without a document anchor",
        body="R8EREVIEWNOANCHOR links ReviewAlpha with ReviewBeta.",
    )
    alpha = Entity(new_id("ent"), TENANT, "ReviewAlpha", EntityType.PROJECT)
    beta = Entity(new_id("ent"), TENANT, "ReviewBeta", EntityType.CONCEPT)
    storage.create_entity(alpha)
    storage.create_entity(beta)
    storage.link_knowledge_entity(
        TENANT,
        knowledge.id,
        alpha.id,
        status="accepted",
    )
    candidate = storage.store_relation_candidate(
        TENANT,
        source_entity_id=alpha.id,
        target_entity_id=beta.id,
        relation_type="member_of",
        confidence=0.9,
        evidence={"span": "reviewed-no-knowledge-anchor"},
    )
    reviewer = "R8E-PRIVATE-REVIEWER"
    storage.review_relation_candidate(
        TENANT,
        str(candidate["id"]),
        "accepted",
        reviewed_by=reviewer,
    )
    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="reviewed-without-anchor",
    )
    request = _request(actor, context, "как связан ReviewAlpha")
    legacy = await _legacy(searcher, KnowledgeGraph(storage), request)
    page = await adapter.prepare(context=context, request=request)
    projection = adapter.project_for_model(context=context, page=page)

    legacy_relation = next(
        item for item in legacy["graph_context"]["relations"] if item["relation_type"] == "member_of"
    )
    assert legacy_relation["implicit"] is False
    legacy_edge = next(
        edge
        for path in legacy["graph_context"]["paths"]
        for edge in path["edges"]
        if edge["type"] == "member_of"
    )
    assert legacy_edge["provenance"]["reviewed"] is True
    assert legacy_edge["provenance"]["origin"] == "review"
    assert "knowledge_object_id" not in legacy_edge["provenance"]

    payload = projection.graph_projection.to_model_payload()
    relation = next(item for item in payload["relations"] if item["relation"] == "member_of")
    edge = next(
        item for path in payload["paths"] for item in path["edges"] if item["relation"] == "member_of"
    )
    path = next(item for item in payload["paths"] if edge in item["edges"])
    assert relation["implicit"] is False
    assert relation["evidence_basis"] == "reviewed_relation"
    assert relation["evidence_result_ordinal"] is None
    assert edge["evidence_basis"] == "reviewed_relation"
    assert edge["evidence_result_ordinal"] is None
    assert path["grounded"] is False
    encoded = projection.to_model_json()
    for private in (
        alpha.id,
        beta.id,
        str(candidate["id"]),
        reviewer,
        knowledge.id,
        knowledge.raw_object_id,
    ):
        assert private not in encoded


async def test_explicit_legacy_merged_endpoint_is_exactly_canonicalized(
    storage: Any,
) -> None:
    _raw, knowledge = _seed_knowledge(
        storage,
        suffix="explicit-legacy-merge",
        title="Explicit relation with a legacy merged endpoint",
        body="R8EEXPLICITMERGE links MergeRoot with MergeLive.",
    )
    custom_entity_type = f"custom-{'x' * 93}"
    custom_relation_type = "😀" * 80
    root = Entity(new_id("ent"), TENANT, "MergeRoot", custom_entity_type)
    obsolete = Entity(new_id("ent"), TENANT, "MergeObsolete", EntityType.CONCEPT)
    live = Entity(new_id("ent"), TENANT, "MergeLive", EntityType.CONCEPT)
    for entity in (root, obsolete, live):
        storage.create_entity(entity)
    storage.link_knowledge_entity(TENANT, knowledge.id, root.id, status="accepted")
    relation = Relation(
        new_id("rel"),
        TENANT,
        root.id,
        obsolete.id,
        custom_relation_type,
        metadata_json={"origin": "manual"},
    )
    storage.create_relation(relation)
    with storage.transaction() as connection:
        connection.execute(
            """UPDATE entities
               SET canonical=0, merged_into_id=?,
                   deleted_at='2024-01-01T00:00:00Z'
               WHERE id=? AND user_id=?""",
            (live.id, obsolete.id, TENANT),
        )

    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="explicit-legacy-merge",
    )
    request = _request(actor, context, "как связан MergeRoot")
    legacy = await _legacy(searcher, KnowledgeGraph(storage), request)
    page = await adapter.prepare(context=context, request=request)
    projection = adapter.project_for_model(context=context, page=page)

    legacy_relation = next(item for item in legacy["graph_context"]["relations"] if item["id"] == relation.id)
    assert legacy_relation["source_entity_id"] == root.id
    assert legacy_relation["target_entity_id"] == live.id
    assert obsolete.id not in json.dumps(legacy["graph_context"], sort_keys=True)

    payload = projection.graph_projection.to_model_payload()
    aliases = {item["alias"]: item["name"] for item in payload["nodes"]}
    node_types = {item["alias"]: item["type"] for item in payload["nodes"]}
    exact_relation = next(item for item in payload["relations"] if item["relation"] == custom_relation_type)
    assert aliases[exact_relation["source"]] == "MergeRoot"
    assert aliases[exact_relation["target"]] == "MergeLive"
    assert node_types[exact_relation["source"]] == custom_entity_type[:80]
    assert exact_relation["implicit"] is False
    assert exact_relation["evidence_basis"] == "relation_row_only"
    assert any(
        aliases[edge["from"]] == "MergeRoot" and aliases[edge["to"]] == "MergeLive"
        for path in payload["paths"]
        for edge in path["edges"]
        if edge["relation"] == custom_relation_type
    )
    encoded = projection.to_model_json()
    assert "MergeObsolete" not in encoded
    for private in (
        root.id,
        obsolete.id,
        live.id,
        relation.id,
        knowledge.id,
        knowledge.raw_object_id,
    ):
        assert private not in encoded


async def test_implicit_legacy_merged_link_is_exactly_deduplicated_and_grounded(
    storage: Any,
) -> None:
    _raw, knowledge = _seed_knowledge(
        storage,
        suffix="implicit-legacy-merge",
        title="Implicit relation with a legacy merged link",
        body="R8EIMPLICITMERGE shared archival evidence.",
    )
    obsolete = Entity(
        new_id("ent"),
        TENANT,
        "ImplicitObsolete",
        EntityType.PROJECT,
    )
    live = Entity(new_id("ent"), TENANT, "ImplicitLive", EntityType.PROJECT)
    beta = Entity(new_id("ent"), TENANT, "AnchorZeta", EntityType.CONCEPT)
    for entity in (obsolete, live, beta):
        storage.create_entity(entity)
    obsolete_link = storage.link_knowledge_entity(
        TENANT,
        knowledge.id,
        obsolete.id,
        status="accepted",
    )
    beta_link = storage.link_knowledge_entity(
        TENANT,
        knowledge.id,
        beta.id,
        status="accepted",
    )
    with storage.transaction() as connection:
        connection.execute(
            """UPDATE entities
               SET canonical=0, merged_into_id=?,
                   deleted_at='2024-01-01T00:00:00Z'
               WHERE id=? AND user_id=?""",
            (live.id, obsolete.id, TENANT),
        )

    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="implicit-legacy-merge",
    )
    request = _request(actor, context, "как связан AnchorZeta")
    legacy = await _legacy(searcher, KnowledgeGraph(storage), request)
    page = await adapter.prepare(context=context, request=request)
    projection = adapter.project_for_model(context=context, page=page)

    legacy_implicit = [item for item in legacy["graph_context"]["relations"] if item["implicit"] is True]
    assert len(legacy_implicit) == 1
    assert {
        legacy_implicit[0]["source_entity_id"],
        legacy_implicit[0]["target_entity_id"],
    } == {live.id, beta.id}
    assert obsolete.id not in json.dumps(legacy["graph_context"], sort_keys=True)

    payload = projection.graph_projection.to_model_payload()
    exact_implicit = [item for item in payload["relations"] if item["implicit"]]
    assert len(exact_implicit) == 1
    relation = exact_implicit[0]
    assert relation["evidence_basis"] == "accepted_links"
    assert relation["evidence_result_ordinal"] is not None
    matching_paths = [path for path in payload["paths"] if any(edge["implicit"] for edge in path["edges"])]
    assert matching_paths
    assert any(path["grounded"] is True for path in matching_paths)
    assert all(
        edge["evidence_basis"] == "accepted_links"
        and edge["evidence_result_ordinal"] == relation["evidence_result_ordinal"]
        for path in matching_paths
        for edge in path["edges"]
        if edge["implicit"]
    )
    encoded = projection.to_model_json()
    assert "ImplicitObsolete" not in encoded
    for private in (
        obsolete.id,
        live.id,
        beta.id,
        str(obsolete_link["id"]),
        str(beta_link["id"]),
        knowledge.id,
        knowledge.raw_object_id,
    ):
        assert private not in encoded


async def test_known_at_relation_history_matches_legacy_snapshot(storage: Any) -> None:
    _raw, knowledge = _seed_knowledge(
        storage,
        suffix="known-at-graph",
        title="Historical Alpha Beta relation",
        body="R8EHISTORYNEEDLE links HistoricalAlphaR8E and HistoricalBetaR8E.",
    )
    alpha = Entity(new_id("ent"), TENANT, "HistoricalAlphaR8E", EntityType.PERSON)
    beta = Entity(new_id("ent"), TENANT, "HistoricalBetaR8E", EntityType.ORGANIZATION)
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
    created = str(
        storage.execute(
            "SELECT recorded_at FROM relation_revisions WHERE relation_id=? ORDER BY event_seq",
            (relation.id,),
        ).fetchall()[-1]["recorded_at"]
    )
    storage.invalidate_relation(
        TENANT,
        relation.id,
        valid_to="2023-01-01",
        reason="historical move",
    )

    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="known-at-graph",
    )
    request = _request(
        actor,
        context,
        "как связан HistoricalAlphaR8E с HistoricalBetaR8E",
        as_of="2024-01-01",
        known_at=created,
        page_size=2,
        snapshot_limit=2,
    )
    legacy = await _legacy(searcher, KnowledgeGraph(storage), request)
    page = await adapter.prepare(context=context, request=request)

    assert [candidate.knowledge_id for candidate in page.candidates] == _legacy_ids(legacy)
    assert page.temporal_status.known_at == legacy["known_at"]
    assert page.temporal_status.known_at_floor == legacy["known_at_floor"]
    assert page.temporal_status.temporal_basis.value == "bitemporal"
    assert len(page.graph_projection.relations) == len(legacy["graph_context"]["relations"])
    assert len(page.graph_projection.paths) == len(legacy["graph_context"]["paths"])
    graph_payload = page.graph_projection.to_model_payload()
    assert _projected_graph_semantics(graph_payload) == _legacy_graph_semantics(legacy["graph_context"])
    assert {
        (
            item["relation"],
            item["valid_from"],
            item["valid_to"],
        )
        for item in graph_payload["relations"]
    } == {("related_to", "2020-01-01", None)}


async def test_signed_continuation_is_deterministic_and_pages_one_snapshot(storage: Any) -> None:
    seeded = [
        _seed_knowledge(
            storage,
            suffix=f"cursor-{index}",
            title=f"Cursor {index}",
            body=f"R8ECURSORNEEDLE evidence row {index}",
        )[1]
        for index in range(3)
    ]
    _authorization, actor, _issuer, context, _searcher, adapter = _stack(
        storage,
        label="cursor",
    )
    first_request = _request(
        actor,
        context,
        "R8ECURSORNEEDLE",
        page_size=1,
        snapshot_limit=3,
    )
    first = await adapter.prepare(context=context, request=first_request)
    replay = await adapter.prepare(context=context, request=first_request)
    assert first.next_continuation is not None
    assert replay.next_continuation == first.next_continuation
    assert first.snapshot_handle == replay.snapshot_handle

    second_request = _request(
        actor,
        context,
        "R8ECURSORNEEDLE",
        page_size=1,
        snapshot_limit=3,
        continuation=first.next_continuation,
    )
    assert second_request.identity_sha256() == first_request.identity_sha256()
    second = await adapter.prepare(context=context, request=second_request)
    assert second.offset == 1
    assert second.snapshot_handle == first.snapshot_handle
    assert second.candidates[0].knowledge_id != first.candidates[0].knowledge_id
    assert {first.candidates[0].knowledge_id, second.candidates[0].knowledge_id} <= {
        item.id for item in seeded
    }


async def test_matched_lower_bound_does_not_create_phantom_cursor_pages(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(37):
        _seed_knowledge(
            storage,
            suffix=f"matched-bound-{index}",
            title=f"Matched bound {index}",
            body=f"R8EMATCHEDBOUND exact evidence {index}",
        )
    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="matched-bound",
    )
    provider = await searcher.search(
        actor.user_id,
        "R8EMATCHEDBOUND",
        limit=3,
        include_entities=True,
        kg=KnowledgeGraph(storage),
        graph_expansion=False,
        record_usage=False,
    )
    assert len(provider["results"]) == 3
    provider["matched_at_least"] = 37

    async def bounded_provider(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return provider

    monkeypatch.setattr(HybridSearcher, "search", bounded_provider)
    first_request = _request(
        actor,
        context,
        "R8EMATCHEDBOUND",
        page_size=2,
        snapshot_limit=3,
    )
    first = await adapter.prepare(context=context, request=first_request)
    assert first.snapshot_rows == 3
    assert first.matched_rows == 37
    assert first.next_continuation is not None
    first_projection = adapter.project_for_model(
        context=context,
        page=first,
    ).to_model_payload()
    assert first_projection["row_coverage"] == "partial"
    assert first_projection["snapshot_truncated"] is True

    final_request = _request(
        actor,
        context,
        "R8EMATCHEDBOUND",
        page_size=2,
        snapshot_limit=3,
        continuation=first.next_continuation,
    )
    final = await adapter.prepare(context=context, request=final_request)
    final_projection = adapter.project_for_model(
        context=context,
        page=final,
    ).to_model_payload()
    assert final.offset == 2
    assert len(final.candidates) == 1
    assert final.next_continuation is None
    assert final.matched_rows == 37
    assert final_projection["row_coverage"] == "partial"
    assert final_projection["snapshot_exhausted"] is True
    assert final_projection["snapshot_rows"] == 3
    assert final_projection["snapshot_truncated"] is True
    assert final_projection["eligible_corpus_rows"] == 37
    assert "total_rows" not in final_projection


async def test_unapplied_large_corpus_date_window_preserves_provider_ranking(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raw, outside = _seed_knowledge(
        storage,
        suffix="unapplied-window",
        title="Outside unapplied window",
        body="R8EUNAPPLIEDWINDOW exact evidence",
        document_date="2020-01-01",
    )
    _authorization, actor, _issuer, context, searcher, adapter = _stack(
        storage,
        label="unapplied-window",
    )
    provider = await searcher.search(
        actor.user_id,
        "R8EUNAPPLIEDWINDOW",
        limit=5,
        include_entities=True,
        kg=KnowledgeGraph(storage),
        graph_expansion=False,
        record_usage=False,
    )
    assert _legacy_ids(provider) == [outside.id]
    provider["strategy"] = {
        **provider["strategy"],
        "date_window": True,
        "date_since": "2025-01-01",
        "date_window_applied": False,
    }

    async def large_corpus_provider(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return provider

    monkeypatch.setattr(HybridSearcher, "search", large_corpus_provider)
    request = _request(
        actor,
        context,
        "R8EUNAPPLIEDWINDOW",
        since="2025-01-01",
        page_size=5,
        snapshot_limit=5,
    )
    page = await adapter.prepare(context=context, request=request)
    assert [candidate.knowledge_id for candidate in page.candidates] == [outside.id]
    assert page.total_rows >= 1
    assert page.date_window_status.applied is False
    assert page.date_window_status.empty is False
    assert adapter.project_for_model(
        context=context,
        page=page,
    ).to_model_payload()["date_window"] == {
        "applied": False,
        "empty": False,
        "requested": True,
        "since": "2025-01-01",
        "until": None,
    }


async def test_exact_empty_date_window_is_distinct_from_zero_query_matches(
    storage: Any,
) -> None:
    _seed_knowledge(
        storage,
        suffix="empty-window",
        title="Outside exact empty window",
        body="R8EEMPTYWINDOW exact evidence",
        document_date="2020-01-01",
    )
    _authorization, actor, _issuer, context, _searcher, adapter = _stack(
        storage,
        label="empty-window",
    )
    request = _request(
        actor,
        context,
        "R8EEMPTYWINDOW",
        since="2025-01-01",
        page_size=5,
        snapshot_limit=5,
    )
    page = await adapter.prepare(context=context, request=request)
    projection = adapter.project_for_model(context=context, page=page).to_model_payload()

    assert page.candidates == ()
    assert page.total_rows == page.snapshot_rows == page.matched_rows == 0
    assert projection["date_window"] == {
        "applied": True,
        "empty": True,
        "requested": True,
        "since": "2025-01-01",
        "until": None,
    }

    zero_request = _request(
        actor,
        context,
        "R8EREALZEROMATCHCANARY",
        page_size=5,
        snapshot_limit=5,
    )
    zero_page = await adapter.prepare(context=context, request=zero_request)
    zero_projection = adapter.project_for_model(
        context=context,
        page=zero_page,
    ).to_model_payload()
    assert zero_page.candidates == ()
    assert zero_page.total_rows == 1
    assert zero_page.snapshot_rows == zero_page.matched_rows == 0
    assert zero_projection["row_coverage"] == "partial"
    assert zero_projection["matched_at_least"] == 0
    assert zero_projection["date_window"] == {
        "applied": False,
        "empty": False,
        "requested": False,
        "since": None,
        "until": None,
    }


async def test_signed_continuation_survives_storage_restart(settings: Any, tmp_path: Any) -> None:
    database = tmp_path / "memory-exact-restart.sqlite3"
    configured = replace(settings, database_path=database, database_must_exist=False)
    first_storage = FridayStorage(configured)
    try:
        for index in range(2):
            _seed_knowledge(
                first_storage,
                suffix=f"restart-{index}",
                title=f"Restart {index}",
                body=f"R8ERESTARTNEEDLE evidence {index}",
            )
        _authorization, actor, issuer, context, _searcher, adapter = _stack(
            first_storage,
            label="restart",
        )
        request = _request(
            actor,
            context,
            "R8ERESTARTNEEDLE",
            page_size=1,
            snapshot_limit=2,
        )
        first_page = await adapter.prepare(context=context, request=request)
        assert first_page.next_continuation is not None
        continuation = first_page.next_continuation
        first_id = first_page.candidates[0].knowledge_id
    finally:
        first_storage.close(final=True)

    reopened = FridayStorage(replace(configured, database_must_exist=True))
    try:
        reopened_authorization = AuthorizationService(reopened, shared_tenant=TENANT)
        reopened_searcher = HybridSearcher(reopened, record_usage=False)
        adapter = MemoryExactInternalAdapter(
            reopened_authorization,
            issuer,
            reopened,
            reopened_searcher,
            KnowledgeGraph(reopened),
        )
        resumed = _request(
            actor,
            context,
            "R8ERESTARTNEEDLE",
            page_size=1,
            snapshot_limit=2,
            continuation=continuation,
        )
        second_page = await adapter.prepare(context=context, request=resumed)
        assert second_page.offset == 1
        assert second_page.candidates[0].knowledge_id != first_id
    finally:
        reopened.close(final=True)


async def test_fresh_read_and_one_shot_publication_authority(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_knowledge(
        storage,
        suffix="publication",
        title="Publication",
        body="R8EPUBLICATIONNEEDLE exact evidence",
    )
    provider_transaction_states: list[bool] = []
    original_search = HybridSearcher.search

    async def observed_provider_search(
        provider: HybridSearcher,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        provider_transaction_states.append(storage.conn.in_transaction)
        return await original_search(provider, *args, **kwargs)

    monkeypatch.setattr(HybridSearcher, "search", observed_provider_search)
    _authorization, actor, _issuer, context, _searcher, adapter = _stack(
        storage,
        label="publication",
    )
    request = _request(actor, context, "R8EPUBLICATIONNEEDLE")
    authority_before = str(
        storage.execute("SELECT observed_at FROM relation_revision_context WHERE singleton=1").fetchone()[
            "observed_at"
        ]
    )
    page = await adapter.prepare(context=context, request=request)
    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    refresh_call = len(provider_transaction_states)
    refresh = await adapter.refresh_publication_authority(
        context=context,
        page=page,
        decision=decision,
    )
    authority_after = str(
        storage.execute("SELECT observed_at FROM relation_revision_context WHERE singleton=1").fetchone()[
            "observed_at"
        ]
    )

    assert authority_after == authority_before
    assert provider_transaction_states
    assert all(state is False for state in provider_transaction_states)
    assert provider_transaction_states[refresh_call:] == [False]
    assert decision.status is MemoryExactPublicationStatus.AUTHORIZED
    assert refresh.status is MemoryExactPublicationStatus.AUTHORIZED
    with pytest.raises(TypeError, match="process-private"):
        copy.copy(refresh)
    with pytest.raises(TypeError, match="process-private"):
        copy.deepcopy(refresh)
    with pytest.raises(TypeError, match="process-private"):
        pickle.dumps(refresh)
    assert decision.to_public_payload() == {
        "authorized": True,
        "one_shot": True,
        "schema": "friday.memory-exact-publication-decision.v1",
        "status": "authorized",
    }
    assert decision.authorizes(page) is False
    provider_calls_before_transaction = len(provider_transaction_states)
    publication_key = f"test.memory-exact-publication.{context.turn_id[-16:]}"
    with storage.transaction() as conn:
        assert conn is storage.conn
        assert conn.in_transaction is True
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
        assert conn.in_transaction is True
        inserted = conn.execute(
            """INSERT INTO schema_meta(key,value,updated_at)
               SELECT ?,?,?
                WHERE NOT EXISTS (SELECT 1 FROM schema_meta WHERE key=?)""",
            (publication_key, "published", BASE_TIME, publication_key),
        )
        assert inserted.rowcount == 1
        assert conn.in_transaction is True
        assert (
            str(
                conn.execute(
                    "SELECT value FROM schema_meta WHERE key=?",
                    (publication_key,),
                ).fetchone()["value"]
            )
            == "published"
        )
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
    assert len(provider_transaction_states) == provider_calls_before_transaction
    assert (
        str(
            storage.execute(
                "SELECT value FROM schema_meta WHERE key=?",
                (publication_key,),
            ).fetchone()["value"]
        )
        == "published"
    )
    assert decision.to_public_payload()["authorized"] is False
    assert "R8EPUBLICATIONNEEDLE" not in repr(decision)
    assert "R8EPUBLICATIONNEEDLE" not in repr(refresh)
    assert json.dumps(decision.to_public_payload(), sort_keys=True).count("authority") == 0
