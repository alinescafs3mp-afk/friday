"""S4-R8H exact archive dispatch: window, as_of/known_at and graph intents."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import pytest

from friday.execution_kernel import (
    ExecutionKernel,
    ToolResult,
    bind_authenticated_request_effect_authority,
    track_request_effects,
)
from friday.ingestion import IngestionPipeline
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
from friday.orchestration.turn_context_runtime import bind_authenticated_turn_context
from friday.permissions import AuthorizationService
from friday.retrieval import HybridSearcher
from friday.retrieval.archive_search_authority import create_archive_model_batch_ledger
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus, ArchiveSearchRequest
from friday.retrieval.archive_search_exact import (
    ARCHIVE_SEARCH_COMPOSITE_PUBLIC_SCHEMA,
    ARCHIVE_SEARCH_EXACT_MODEL_PAYLOAD_SCHEMA,
    ArchiveExactDispatchError,
    derive_archive_exact_requests,
    parse_archive_exact_intent,
    prepare_archive_exact_lanes,
)
from friday.retrieval.archive_search_service import (
    compose_prepared_archive_searches,
    prepare_archive_search_in_transaction,
)
from friday.retrieval.memory_exact_internal import MemoryExactInternalAdapter
from friday.retrieval.message_exact_internal import MessageExactInternalAdapter
from friday.storage.models import KnowledgeObject, RawObject, new_id
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision
from friday.web_surfer import WebSurfer

TENANT = "r8h-dispatch-tenant"
PRINCIPAL = "r8h-dispatch-principal"
QUERY = "R8HDISPATCH exact evidence"
STORED_AT = "2026-08-30T09:00:00+00:00"


def _turn(
    actor: Any,
    conversation_id: str,
    *,
    label: str,
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext]:
    now = time.monotonic_ns()
    issuer = TurnContextIssuer(
        hashlib.sha256(f"r8h-dispatch:{label}".encode("ascii")).digest(),
        _monotonic_ns=lambda: now,
    )
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"r8h-dispatch-ingress-{label}",
        actor=actor,
        conversation_id=conversation_id,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=f"r8h-dispatch-source-{label}",
        update_id=f"r8h-dispatch-update-{label}",
        request_effect_binding_sha256=hashlib.sha256(label.encode("ascii")).hexdigest(),
    )
    model_input = TurnInput.from_chat(
        message="dispatch exact archive evidence",
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
            TurnSafetyDeadline(now + 300_000_000_000),
            ModelAntiLoopBudget(4, 1),
            TurnResourceBudget(4, 2, 2, 32_768),
        ),
        pending_work_admission=None,
    )
    return issuer, context


def _seed_knowledge(storage: Any, *, label: str, index: int) -> None:
    body = f"{QUERY} row {label} {index}"
    raw = RawObject(
        id=new_id("raw"),
        user_id=TENANT,
        source="test",
        source_ref=f"r8h-dispatch-{label}-{index}",
        raw_content=body,
        content_type="text",
        metadata_json={"r8h": label},
        content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        received_at=STORED_AT,
        created_at=STORED_AT,
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id=TENANT,
            raw_object_id=raw.id,
            content=body,
            content_type="text",
            title=f"R8H dispatch {label} {index}",
            summary="",
            metadata_json={},
            knowledge_kind="document",
            lifecycle_stage="active",
            importance=0.7,
            quality_score=0.8,
            promotion_score=0.8,
            created_at=STORED_AT,
            updated_at=STORED_AT,
        )
    )


def _stack(storage: Any, settings: Any, *, label: str) -> dict[str, Any]:
    storage.ensure_user(TENANT, preset_key="owner")
    storage.ensure_user(PRINCIPAL, preset_key="owner")
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    actor = authorization.actor_for_user(PRINCIPAL, source=f"r8h-dispatch-{label}")
    conversation = storage.create_conversation(PRINCIPAL, f"R8H dispatch {label}")
    conversation_id = str(conversation["id"])
    storage.store_message(conversation_id, PRINCIPAL, "assistant", f"{QUERY} earlier")
    boundary = storage.store_message(
        conversation_id,
        PRINCIPAL,
        "user",
        f"R8H dispatch boundary {label}",
    )
    boundary_id = str(boundary["id"])
    issuer, context = _turn(actor, conversation_id, label=label)
    for index in range(3):
        _seed_knowledge(storage, label=label, index=index)
    graph = KnowledgeGraph(storage)
    searcher = HybridSearcher(storage, record_usage=False)
    kernel = ExecutionKernel(authorization, settings)
    web = WebSurfer(settings)
    ingestion = IngestionPipeline(settings, storage, graph)
    kernel.bind_services(
        storage,
        graph,
        web,
        ingestion,
        searcher=searcher,
    )
    message_adapter = MessageExactInternalAdapter(authorization, issuer)
    memory_adapter = MemoryExactInternalAdapter(
        authorization,
        issuer,
        storage,
        searcher,
        graph,
    )
    kernel.bind_archive_exact_adapters(
        message_exact_adapter=message_adapter,
        memory_exact_adapter=memory_adapter,
    )
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator=f"r8h-dispatch-{label}",
    )
    invocation = kernel.create_archive_search_invocation(
        actor=actor,
        turn_ledger=ledger,
        current_conversation_id=conversation_id,
        boundary_user_message_id=boundary_id,
    )
    return {
        "actor": actor,
        "authorization": authorization,
        "boundary_id": boundary_id,
        "context": context,
        "conversation_id": conversation_id,
        "invocation": invocation,
        "issuer": issuer,
        "kernel": kernel,
        "web": web,
    }


def test_parse_archive_exact_intent_is_closed() -> None:
    idle = parse_archive_exact_intent()
    assert idle.active is False
    window = parse_archive_exact_intent(exact_window=True)
    assert window.requests_message_window is True
    assert window.requests_memory_exact is False
    graph = parse_archive_exact_intent(include_graph=True, as_of="2026-08-31")
    assert graph.requests_memory_exact is True
    assert graph.as_of == "2026-08-31"
    with pytest.raises(ArchiveExactDispatchError, match="full_content"):
        parse_archive_exact_intent(content_mode="full_content")
    with pytest.raises(ArchiveExactDispatchError, match="booleans"):
        parse_archive_exact_intent(exact_window="yes")  # type: ignore[arg-type]


def test_derived_memory_request_binds_as_of_and_known_at() -> None:
    request = ArchiveSearchRequest.create(
        query=QUERY,
        corpora=(ArchiveSearchCorpus.KNOWLEDGE,),
    )
    intent = parse_archive_exact_intent(
        as_of="2026-08-31",
        known_at="2026-08-31T23:59:59+00:00",
        include_graph=True,
    )
    message, memory = derive_archive_exact_requests(
        request,
        intent,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        active_turn_id="turn_" + "a" * 64,
        conversation_id=None,
        boundary_user_message_id=None,
    )
    assert message is None
    assert memory is not None
    assert memory.as_of == "2026-08-31"
    assert memory.known_at == "2026-08-31T23:59:59.000000Z"
    assert memory.query == QUERY


def test_model_payload_still_cannot_inject_exact_authority() -> None:
    with pytest.raises(Exception, match="keys"):
        ArchiveSearchRequest.from_model_payload(
            {
                "query": QUERY,
                "corpora": ["messages"],
                "as_of": "2026-08-31",
            }
        )
    with pytest.raises(Exception, match="keys"):
        ArchiveSearchRequest.from_model_payload(
            {
                "query": QUERY,
                "corpora": ["messages"],
                "exact_window": True,
            }
        )


def test_legacy_catalogue_still_offers_dialogue_adapters(settings: Any) -> None:
    kernel = ExecutionKernel(settings=settings)
    for name in ("archive_search", "memory_search", "message_search", "source_search"):
        assert kernel.get_tool(name) is not None
    archive = kernel.get_tool("archive_search")
    assert archive is not None
    properties = archive.parameters["properties"]
    assert "as_of" in properties
    assert "known_at" in properties
    assert "exact_window" in properties
    assert "include_graph" in properties
    assert "message_exact_request" not in properties
    assert "memory_exact_request" not in properties


@pytest.mark.asyncio
async def test_inactive_intent_keeps_released_archive_path(
    settings: Any,
    storage: Any,
) -> None:
    storage.ensure_user(TENANT, preset_key="owner")
    storage.ensure_user(PRINCIPAL, preset_key="owner")
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    actor = authorization.actor_for_user(PRINCIPAL, source="r8h-dispatch-idle")
    kernel = ExecutionKernel(authorization, settings)
    web = WebSurfer(settings)
    graph = KnowledgeGraph(storage)
    kernel.bind_services(
        storage,
        graph,
        web,
        IngestionPipeline(settings, storage, graph),
        searcher=HybridSearcher(storage, record_usage=False),
    )
    ledger = create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        turn_discriminator="r8h-dispatch-idle",
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
    assert result.prepared_archive_search is not None
    assert result.prepared_archive_search_composite is None
    assert result.archive_exact_model_payload is None
    payload = json.loads(result.archive_model_visible_bytes())
    assert payload["schema"].startswith("friday.archive-search-page.public.")


@pytest.mark.asyncio
async def test_exact_window_without_authenticated_turn_fails_closed(
    settings: Any,
    storage: Any,
) -> None:
    stack = _stack(storage, settings, label="no-turn")
    try:
        result = await stack["kernel"].execute(
            "archive_search",
            {
                "query": QUERY,
                "corpora": [ArchiveSearchCorpus.MESSAGES.value],
                "exact_window": True,
                "_archive_invocation": stack["invocation"],
            },
            actor=stack["actor"],
        )
    finally:
        await stack["web"].close()
    assert result.success is False
    assert result.prepared_archive_search is None
    assert result.prepared_archive_search_composite is None
    assert result.error.startswith("Invalid tool arguments")


@pytest.mark.asyncio
async def test_exact_window_dispatches_message_lane_through_archive_search(
    settings: Any,
    storage: Any,
) -> None:
    stack = _stack(storage, settings, label="window")
    try:
        with (
            track_request_effects(
                lambda: True,
                request_binding_sha256=stack["context"].effect_fence.request_effect_binding_sha256,
            ) as effects,
            bind_authenticated_turn_context(stack["issuer"], stack["context"]),
            bind_authenticated_request_effect_authority(effects),
        ):
            result = await stack["kernel"].execute(
                "archive_search",
                {
                    "query": QUERY,
                    "corpora": [ArchiveSearchCorpus.MESSAGES.value],
                    "exact_window": True,
                    "_archive_invocation": stack["invocation"],
                },
                actor=stack["actor"],
            )
    finally:
        await stack["web"].close()
    assert result.success is True
    assert type(result) is ToolResult
    assert result.prepared_archive_search is not None
    assert result.prepared_archive_search_composite is not None
    assert result.archive_exact_model_payload is not None
    assert result.archive_exact_model_payload["schema"] == ARCHIVE_SEARCH_EXACT_MODEL_PAYLOAD_SCHEMA
    envelope = json.loads(result.to_llm_message())
    assert envelope["schema"] == ARCHIVE_SEARCH_COMPOSITE_PUBLIC_SCHEMA
    archive_page = json.loads(result.archive_model_visible_bytes())
    assert envelope["archive"] == archive_page
    window = envelope["message_window"]
    assert window["schema"] == "friday.archive-search-exact-message-pages.model.v1"
    texts = [row["text"] for page in window["pages"] for row in page["results"]]
    assert texts
    assert any(QUERY in text or "dispatch boundary" in text or "earlier" in text for text in texts)
    rendered = json.dumps(result.to_dict(), ensure_ascii=True)
    assert stack["boundary_id"] not in rendered
    assert stack["context"].turn_id not in rendered
    assert "message_exact_request" not in rendered
    assert result.prepared_archive_search_composite.message_exact_pages
    assert not result.prepared_archive_search_composite.memory_exact_pages


@pytest.mark.asyncio
async def test_as_of_and_graph_dispatch_memory_lane_through_archive_search(
    storage: Any,
) -> None:
    storage.ensure_user(TENANT, preset_key="owner")
    storage.ensure_user(PRINCIPAL, preset_key="owner")
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    actor = authorization.actor_for_user(PRINCIPAL, source="r8h-dispatch-temporal")
    conversation = storage.create_conversation(PRINCIPAL, "R8H dispatch temporal")
    conversation_id = str(conversation["id"])
    issuer, context = _turn(actor, conversation_id, label="temporal")
    for index in range(3):
        _seed_knowledge(storage, label="temporal", index=index)
    searcher = HybridSearcher(storage, record_usage=False)
    memory_adapter = MemoryExactInternalAdapter(
        authorization,
        issuer,
        storage,
        searcher,
        KnowledgeGraph(storage),
    )
    assert actor.user_id == TENANT
    assert actor.own_id == PRINCIPAL
    lanes = await prepare_archive_exact_lanes(
        request=ArchiveSearchRequest.create(
            query=QUERY,
            corpora=(ArchiveSearchCorpus.KNOWLEDGE,),
        ),
        intent=parse_archive_exact_intent(include_graph=True),
        storage=storage,
        turn_context=context,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        conversation_id=None,
        boundary_user_message_id=None,
        message_adapter=None,
        memory_adapter=memory_adapter,
    )
    assert lanes.memory_pages
    assert lanes.model_payload is not None
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=actor,
            tenant_id=actor.user_id,
            principal_id=actor.own_id,
            request=lanes.execution_request,
            snapshot_discriminator="r8h-dispatch-temporal-snapshot",
            run_discriminator="r8h-dispatch-temporal-run",
            turn_ledger=create_archive_model_batch_ledger(
                tenant_id=actor.user_id,
                principal_id=actor.own_id,
                turn_discriminator="r8h-dispatch-temporal",
            ),
        )
    composite = compose_prepared_archive_searches(
        prepared,
        memory_exact_pages=lanes.memory_pages,
    )
    visible = prepared.authorized_batch.model_visible_canonical_bytes.decode("ascii")
    result = ToolResult(
        "archive_search",
        True,
        data=visible,
        prepared_archive_search=prepared,
        archive_exact_model_payload=lanes.model_payload,
        prepared_archive_search_composite=composite,
    )
    envelope = json.loads(result.to_llm_message())
    assert envelope["schema"] == ARCHIVE_SEARCH_COMPOSITE_PUBLIC_SCHEMA
    memory = envelope["memory"]
    assert memory["schema"] == "friday.archive-search-exact-memory-pages.model.v1"
    first = memory["pages"][0]
    assert "graph_context" in first
    titles = [row["title"] for page in memory["pages"] for row in page["results"]]
    assert any(title.startswith("R8H dispatch temporal") for title in titles)
    assert composite.memory_exact_pages
    assert not composite.message_exact_pages
    assert context.turn_id not in result.to_llm_message()


@pytest.mark.asyncio
async def test_kernel_include_graph_uses_the_same_dispatch_owner(
    settings: Any,
    storage: Any,
) -> None:
    stack = _stack(storage, settings, label="kernel-graph")
    try:
        with (
            track_request_effects(
                lambda: True,
                request_binding_sha256=stack["context"].effect_fence.request_effect_binding_sha256,
            ) as effects,
            bind_authenticated_turn_context(stack["issuer"], stack["context"]),
            bind_authenticated_request_effect_authority(effects),
        ):
            result = await stack["kernel"].execute(
                "archive_search",
                {
                    "query": QUERY,
                    "corpora": [ArchiveSearchCorpus.KNOWLEDGE.value],
                    "include_graph": True,
                    "_archive_invocation": stack["invocation"],
                },
                actor=stack["actor"],
            )
    finally:
        await stack["web"].close()
    assert result.success is True
    envelope = json.loads(result.to_llm_message())
    assert envelope["schema"] == ARCHIVE_SEARCH_COMPOSITE_PUBLIC_SCHEMA
    assert envelope["memory"]["pages"][0]["graph_context"]
    assert result.prepared_archive_search_composite is not None
    assert result.prepared_archive_search_composite.memory_exact_pages


@pytest.mark.asyncio
async def test_malformed_as_of_does_not_become_a_current_snapshot(
    settings: Any,
    storage: Any,
) -> None:
    stack = _stack(storage, settings, label="bad-as-of")
    try:
        with (
            track_request_effects(
                lambda: True,
                request_binding_sha256=stack["context"].effect_fence.request_effect_binding_sha256,
            ) as effects,
            bind_authenticated_turn_context(stack["issuer"], stack["context"]),
            bind_authenticated_request_effect_authority(effects),
        ):
            result = await stack["kernel"].execute(
                "archive_search",
                {
                    "query": QUERY,
                    "corpora": [ArchiveSearchCorpus.KNOWLEDGE.value],
                    "as_of": "not-a-date",
                    "_archive_invocation": stack["invocation"],
                },
                actor=stack["actor"],
            )
    finally:
        await stack["web"].close()
    assert result.success is False
    assert result.prepared_archive_search is None
    assert result.error.startswith("Invalid tool arguments")
