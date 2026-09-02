"""Hostile evidence for the authenticated exact-memory internal lane."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import pickle
import time
from typing import Any

import pytest

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
from friday.retrieval import HybridSearcher, best_snippet
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
    MemoryExactInternalAdapter,
    MemoryExactInternalError,
    MemoryExactReadDenied,
)
from friday.storage import FridayStorage
from friday.storage._memory_exact_internal import MemoryExactStorageError
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

TENANT = "memory-exact-adversarial-tenant"
FOREIGN_TENANT = "memory-exact-adversarial-foreign-tenant"
PRINCIPAL = "memory-exact-adversarial-principal"
OTHER_PRINCIPAL = "memory-exact-adversarial-other-principal"
BASE_TIME = "2026-09-01T08:00:00+00:00"
VALID_TURN = f"turn_{'a' * 64}"


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
    searcher = HybridSearcher(storage, record_usage=False)
    adapter = MemoryExactInternalAdapter(
        authorization,
        issuer,
        storage,
        searcher,
        KnowledgeGraph(storage),
    )
    return authorization, actor, context, searcher, adapter


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


def _seed(
    storage: FridayStorage,
    *,
    suffix: str,
    query: str,
    tenant: str = TENANT,
    body: str | None = None,
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
        id=new_id("ko"),
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
    clock_ns[0] += 61_000_000_000
    assert (
        await adapter.consume_publication_authority(
            context=context,
            page=page,
            decision=expired,
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
        await live_adapter.consume_publication_authority(
            context=live_context,
            page=live_page,
            decision=revoked,
        )
        is False
    )
    authorization.grant_permission(actor.own_id, "knowledge.read")
    assert (
        await live_adapter.consume_publication_authority(
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
        adapter.consume_publication_authority(
            context=context,
            page=page,
            decision=decision,
        ),
        adapter.consume_publication_authority(
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
        await adapter.consume_publication_authority(
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
        adapter.consume_publication_authority(
            context=context,
            page=page,
            decision=cancelled,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.to_public_payload()["authorized"] is False
    assert (
        await adapter.consume_publication_authority(
            context=context,
            page=page,
            decision=cancelled,
        )
        is False
    )


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
        await adapter.consume_publication_authority(
            context=context,
            page=page_b,
            decision=decision,
        )
        is False
    )
    assert decision.to_public_payload()["authorized"] is True
    assert (
        await adapter.consume_publication_authority(
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
        await adapter.consume_publication_authority(
            context=context,
            page=page_a,
            decision=denied,
        )
        is False
    )
    assert denied.to_public_payload()["authorized"] is False


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

    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    assert decision.status is MemoryExactPublicationStatus.DRIFTED
    assert decision.authorizes(page) is False


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

    decision = await adapter.reauthorize_for_publication(context=context, page=page)
    assert decision.status is MemoryExactPublicationStatus.DRIFTED
    assert decision.authorizes(page) is False


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
    try:
        page = await adapter.prepare(context=context, request=request)
    except MemoryExactStorageError:
        return
    assert page.candidates == ()
    assert (
        foreign.id
        not in adapter.project_for_model(
            context=context,
            page=page,
        ).to_model_json()
    )


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
