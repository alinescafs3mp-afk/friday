"""Acceptance contract for the passive R8H composite archive seam."""

from __future__ import annotations

import copy
import hashlib
import importlib
import inspect
import json
import pickle
import sqlite3
import time
from dataclasses import dataclass, replace
from typing import Any

import pytest

from friday.execution_kernel import ToolResult
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
from friday.retrieval import HybridSearcher
from friday.retrieval.archive_search_authority import create_archive_model_batch_ledger
from friday.retrieval.archive_search_contract import (
    ARCHIVE_SEARCH_REQUEST_IDENTITY_SCHEMA_V3,
    ARCHIVE_SEARCH_REQUEST_SCHEMA,
    ARCHIVE_SEARCH_REQUEST_SCHEMA_V3,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
)
from friday.retrieval.archive_search_service import (
    MAX_ARCHIVE_EXACT_CHAIN_PAGES,
    ArchiveSearchServiceError,
    PreparedArchiveSearch,
    PreparedArchiveSearchComposite,
    compose_prepared_archive_searches,
    prepare_archive_search_in_transaction,
)
from friday.retrieval.contracts import RetrievalContractError
from friday.retrieval.memory_exact_contract import (
    MemoryExactContinuation,
    MemoryExactPage,
    MemoryExactRequest,
)
from friday.retrieval.memory_exact_internal import MemoryExactInternalAdapter
from friday.retrieval.message_exact_contract import (
    MessageExactContinuation,
    MessageExactPage,
    MessageExactRequest,
)
from friday.retrieval.message_exact_internal import MessageExactInternalAdapter
from friday.storage.models import KnowledgeObject, RawObject, new_id
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

TENANT = "r8h-shared-tenant"
PRINCIPAL = "r8h-principal"
QUERY = "R8HCOMPOSITE exact evidence"
STORED_AT = "2026-08-30T09:00:00+00:00"


@dataclass(frozen=True, slots=True)
class _CompositeFixture:
    actor: ActorContext
    authorization: AuthorizationService
    boundary_id: str
    context: AuthenticatedTurnContext
    conversation_id: str
    memory_adapter: MemoryExactInternalAdapter
    memory_pages: tuple[MemoryExactPage, ...]
    memory_request: MemoryExactRequest | None
    message_adapter: MessageExactInternalAdapter
    message_pages: tuple[MessageExactPage, ...]
    message_request: MessageExactRequest | None
    prepared: PreparedArchiveSearch
    request: ArchiveSearchRequest


def _turn(
    actor: ActorContext,
    conversation_id: str,
    *,
    label: str,
) -> tuple[TurnContextIssuer, AuthenticatedTurnContext]:
    now = time.monotonic_ns()
    issuer = TurnContextIssuer(
        hashlib.sha256(f"r8h:{label}".encode("ascii")).digest(),
        _monotonic_ns=lambda: now,
    )
    authority = issuer.issue_ingress_authority(
        ingress_kind=IngressKind.SIGNED_HTTP,
        ingress_issued_token=f"r8h-ingress-{label}",
        actor=actor,
        conversation_id=conversation_id,
        interaction_mode=TurnMode.DIALOGUE,
        source_id=f"r8h-source-{label}",
        update_id=f"r8h-update-{label}",
        request_effect_binding_sha256=hashlib.sha256(label.encode("ascii")).hexdigest(),
    )
    model_input = TurnInput.from_chat(
        message="compose exact private archive evidence",
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
        source_ref=f"r8h-{label}-{index}",
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
            title=f"R8H {label} {index}",
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


def _message_chain(
    storage: Any,
    adapter: MessageExactInternalAdapter,
    context: AuthenticatedTurnContext,
    request: MessageExactRequest,
    *,
    maximum: int,
) -> tuple[MessageExactPage, ...]:
    pages: list[MessageExactPage] = []
    current = request
    for _index in range(maximum):
        with storage.transaction() as conn:
            page = adapter.prepare_in_transaction(conn, context=context, request=current)
        pages.append(page)
        if page.next_continuation is None:
            return tuple(pages)
        current = replace(request, continuation=page.next_continuation)
    raise AssertionError("exact-message fixture did not reach its terminal page")


async def _memory_chain(
    adapter: MemoryExactInternalAdapter,
    context: AuthenticatedTurnContext,
    request: MemoryExactRequest,
    *,
    maximum: int,
) -> tuple[MemoryExactPage, ...]:
    pages: list[MemoryExactPage] = []
    current = request
    for _index in range(maximum):
        page = await adapter.prepare(context=context, request=current)
        pages.append(page)
        if page.next_continuation is None:
            return tuple(pages)
        current = replace(request, continuation=page.next_continuation)
    raise AssertionError("exact-memory fixture did not reach its terminal page")


async def _fixture(
    storage: Any,
    *,
    label: str,
    include_message: bool,
    include_memory: bool,
    message_rows: int = 3,
    message_page_size: int = 1,
    memory_rows: int = 3,
    memory_page_size: int = 1,
) -> _CompositeFixture:
    storage.ensure_user(TENANT, preset_key="owner")
    storage.ensure_user(PRINCIPAL, preset_key="owner")
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    actor = authorization.actor_for_user(PRINCIPAL, source=f"r8h-{label}")
    conversation = storage.create_conversation(PRINCIPAL, f"R8H {label}")
    conversation_id = str(conversation["id"])
    for index in range(message_rows if include_message else 0):
        storage.store_message(
            conversation_id,
            PRINCIPAL,
            "assistant" if index % 2 else "user",
            f"{QUERY} message {label} {index}",
        )
    boundary = storage.store_message(
        conversation_id,
        PRINCIPAL,
        "user",
        f"R8H accepted boundary {label}",
    )
    boundary_id = str(boundary["id"])
    issuer, context = _turn(actor, conversation_id, label=label)
    message_adapter = MessageExactInternalAdapter(authorization, issuer)
    searcher = HybridSearcher(storage, record_usage=False)
    memory_adapter = MemoryExactInternalAdapter(
        authorization,
        issuer,
        storage,
        searcher,
        KnowledgeGraph(storage),
    )

    message_request = (
        MessageExactRequest.create(
            conversation_id=conversation_id,
            accepted_boundary_user_message_id=boundary_id,
            page_size=message_page_size,
        )
        if include_message
        else None
    )
    if include_memory:
        for index in range(memory_rows):
            _seed_knowledge(storage, label=label, index=index)
        memory_request = MemoryExactRequest.create(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            active_turn_id=context.turn_id,
            query=QUERY,
            page_size=memory_page_size,
            snapshot_limit=max(memory_rows, memory_page_size),
        )
    else:
        memory_request = None

    message_pages = (
        _message_chain(
            storage,
            message_adapter,
            context,
            message_request,
            maximum=message_rows + 1,
        )
        if message_request is not None
        else ()
    )
    memory_pages = (
        await _memory_chain(
            memory_adapter,
            context,
            memory_request,
            maximum=memory_rows + 1,
        )
        if memory_request is not None
        else ()
    )
    corpora = tuple(
        corpus
        for corpus, included in (
            (ArchiveSearchCorpus.KNOWLEDGE, include_memory),
            (ArchiveSearchCorpus.MESSAGES, include_message),
        )
        if included
    )
    request = ArchiveSearchRequest.create(
        query=QUERY,
        corpora=corpora,
        message_exact_request=message_request,
        memory_exact_request=memory_request,
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=actor,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=f"r8h-snapshot-{label}",
            run_discriminator=f"r8h-run-{label}",
            turn_ledger=create_archive_model_batch_ledger(
                tenant_id=TENANT,
                principal_id=PRINCIPAL,
                turn_discriminator=f"r8h-turn-{label}",
            ),
            current_conversation_id=conversation_id if include_message else None,
            boundary_user_message_id=boundary_id if include_message else None,
        )
    return _CompositeFixture(
        actor=actor,
        authorization=authorization,
        boundary_id=boundary_id,
        context=context,
        conversation_id=conversation_id,
        memory_adapter=memory_adapter,
        memory_pages=memory_pages,
        memory_request=memory_request,
        message_adapter=message_adapter,
        message_pages=message_pages,
        message_request=message_request,
        prepared=prepared,
        request=request,
    )


_V1_PRIVATE = (
    '{"context":{"after":0,"before":0},"continuation":null,'
    '"conversation_scope":"all","corpora":["documents"],"entity_hints":[],'
    '"filename_hints":[],"lifecycle_constraints":[],"limit":10,'
    '"query":"legacy needle","review_scope":"discoverable","roles":[],'
    '"schema":"friday.archive-search-request.private.v1",'
    '"temporal_constraints":[],"title_hints":[]}'
)
_V1_IDENTITY = (
    '{"context":{"after":0,"before":0},"conversation_scope":"all",'
    '"corpora":["documents"],"entity_hints":[],"filename_hints":[],'
    '"lifecycle_constraints":[],"limit":10,"query":"legacy needle",'
    '"review_scope":"discoverable","roles":[],'
    '"schema":"friday.archive-search-request-identity.private.v1",'
    '"temporal_constraints":[],"title_hints":[]}'
)
_V2_PRIVATE = (
    '{"context":{"after":0,"before":0},"continuation":null,'
    '"conversation_scope":"all","corpora":["documents"],"entity_hints":[],'
    '"filename_hints":[],"focus":"exact focus","lifecycle_constraints":[],'
    '"limit":10,"query":"legacy needle","review_scope":"discoverable",'
    '"roles":[],"schema":"friday.archive-search-request.private.v2",'
    '"temporal_constraints":[],"title_hints":[]}'
)
_V2_IDENTITY = (
    '{"context":{"after":0,"before":0},"conversation_scope":"all",'
    '"corpora":["documents"],"entity_hints":[],"filename_hints":[],'
    '"focus":"exact focus","lifecycle_constraints":[],"limit":10,'
    '"query":"legacy needle","review_scope":"discoverable","roles":[],'
    '"schema":"friday.archive-search-request-identity.private.v2",'
    '"temporal_constraints":[],"title_hints":[]}'
)


@pytest.mark.parametrize(
    ("focus", "private_json", "identity_json", "identity_sha256"),
    (
        (
            "",
            _V1_PRIVATE,
            _V1_IDENTITY,
            "30dbf0b8931ce7f974e02855c7231de390a5ef84f3e1619d82fc568046bf0511",
        ),
        (
            "exact focus",
            _V2_PRIVATE,
            _V2_IDENTITY,
            "c6628e4acd4f8b48ae3cc64047cee81da7874ce38af1018d0c21d7c24051fabf",
        ),
    ),
)
def test_legacy_v1_v2_request_bytes_and_identity_digests_are_frozen(
    focus: str,
    private_json: str,
    identity_json: str,
    identity_sha256: str,
) -> None:
    request = ArchiveSearchRequest.create(
        query="legacy needle",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        focus=focus,
    )
    domain = f"friday/archive-search-request-identity/v{2 if focus else 1}\0".encode()
    assert request.to_private_json() == private_json
    assert request.to_identity_json() == identity_json
    assert request.identity_digest_material() == domain + identity_json.encode("ascii")
    assert hashlib.sha256(request.identity_digest_material()).hexdigest() == identity_sha256


def _typed_v3_request() -> ArchiveSearchRequest:
    message = MessageExactRequest.create(
        conversation_id="conv_0123456789abcdef",
        accepted_boundary_user_message_id="msg_0123456789abcdef",
        since="2026-08-01T00:00:00+00:00",
        until="2026-09-01T00:00:00+00:00",
        page_size=7,
    )
    memory = MemoryExactRequest.create(
        tenant_id="r8h-tenant",
        principal_id="r8h-principal",
        active_turn_id=f"turn_{'a' * 64}",
        query="R8H exact query",
        since="2026-08-01",
        until="2026-08-31",
        as_of="2026-08-31",
        known_at="2026-08-31T23:59:59+00:00",
        page_size=3,
        snapshot_limit=9,
    )
    return ArchiveSearchRequest.create(
        query="R8H exact query",
        corpora=(ArchiveSearchCorpus.KNOWLEDGE, ArchiveSearchCorpus.MESSAGES),
        message_exact_request=message,
        memory_exact_request=memory,
    )


def test_v3_roundtrip_binds_exact_windows_and_temporal_graph_inputs() -> None:
    request = _typed_v3_request()
    payload = request.to_private_payload()
    identity = request.to_identity_payload()
    assert payload["schema"] == ARCHIVE_SEARCH_REQUEST_SCHEMA_V3
    assert identity["schema"] == ARCHIVE_SEARCH_REQUEST_IDENTITY_SCHEMA_V3
    assert payload["message_exact_request"]["since"] == "2026-08-01T00:00:00+00:00"
    assert payload["message_exact_request"]["until"] == "2026-09-01T00:00:00+00:00"
    assert payload["memory_exact_request"]["as_of"] == "2026-08-31"
    assert payload["memory_exact_request"]["known_at"] == "2026-08-31T23:59:59.000000Z"
    assert "include_graph" not in request.to_private_json()
    assert ArchiveSearchRequest.parse_private(request.to_private_json()) == request


def test_v3_is_closed_and_model_payload_cannot_inject_exact_authority() -> None:
    request = _typed_v3_request()
    payload = request.to_private_payload()
    with pytest.raises(RetrievalContractError, match="closed contract"):
        ArchiveSearchRequest.from_private_payload({**payload, "include_graph": True})
    without_memory = {key: value for key, value in payload.items() if key != "memory_exact_request"}
    with pytest.raises(RetrievalContractError, match="closed contract"):
        ArchiveSearchRequest.from_private_payload(without_memory)
    with pytest.raises(RetrievalContractError, match="requires an exact selection"):
        ArchiveSearchRequest.from_private_payload(
            {**payload, "message_exact_request": None, "memory_exact_request": None}
        )
    for injected in (
        {"message_exact_request": payload["message_exact_request"]},
        {"memory_exact_request": payload["memory_exact_request"]},
        {"accepted_boundary_user_message_id": "msg_0123456789abcdef"},
        {"include_graph": True},
    ):
        with pytest.raises(RetrievalContractError, match="keys"):
            ArchiveSearchRequest.from_model_payload(
                {"query": "R8H exact query", "corpora": ["messages"], **injected}
            )


def test_v3_outer_and_child_cursors_are_independent_transport_identity() -> None:
    request = _typed_v3_request()
    assert request.message_exact_request is not None
    assert request.memory_exact_request is not None
    variants = (
        replace(request, continuation="outer_page_two"),
        replace(
            request,
            message_exact_request=replace(
                request.message_exact_request,
                continuation=MessageExactContinuation.create("m" * 32),
            ),
        ),
        replace(
            request,
            memory_exact_request=replace(
                request.memory_exact_request,
                continuation=MemoryExactContinuation.create("n" * 32),
            ),
        ),
    )
    assert all(item.to_private_json() != request.to_private_json() for item in variants)
    assert all(ArchiveSearchRequest.parse_private(item.to_private_json()) == item for item in variants)
    assert all(item.to_identity_json() == request.to_identity_json() for item in variants)
    assert all(item.identity_digest_material() == request.identity_digest_material() for item in variants)
    assert "continuation" not in request.to_identity_json()


async def test_message_only_composite_accepts_one_real_terminal_page(storage: Any) -> None:
    fixture = await _fixture(
        storage,
        label="single-message",
        include_message=True,
        include_memory=False,
        message_rows=1,
        message_page_size=2,
    )
    composite = compose_prepared_archive_searches(
        fixture.prepared,
        message_exact_pages=fixture.message_pages,
    )
    assert type(composite) is PreparedArchiveSearchComposite
    assert composite.request == fixture.request
    assert composite.prepared_search is fixture.prepared
    assert composite.message_exact_pages == fixture.message_pages
    assert composite.memory_exact_pages == ()


async def test_memory_only_composite_accepts_one_real_terminal_page(storage: Any) -> None:
    fixture = await _fixture(
        storage,
        label="single-memory",
        include_message=False,
        include_memory=True,
        memory_rows=1,
        memory_page_size=2,
    )
    composite = compose_prepared_archive_searches(
        fixture.prepared,
        memory_exact_pages=fixture.memory_pages,
    )
    assert composite.request.memory_exact_request == fixture.memory_request
    assert composite.memory_exact_pages == fixture.memory_pages
    assert composite.message_exact_pages == ()


async def test_dual_composite_accepts_real_ordered_terminal_chains(storage: Any) -> None:
    fixture = await _fixture(
        storage,
        label="dual-terminal",
        include_message=True,
        include_memory=True,
    )
    composite = compose_prepared_archive_searches(
        fixture.prepared,
        message_exact_pages=fixture.message_pages,
        memory_exact_pages=fixture.memory_pages,
    )
    assert len(composite.message_exact_pages) == 3
    assert len(composite.memory_exact_pages) == 3
    assert composite.message_exact_pages[-1].next_continuation is None
    assert composite.memory_exact_pages[-1].next_continuation is None


async def test_composite_accepts_bounded_partial_exact_prefixes(storage: Any) -> None:
    fixture = await _fixture(
        storage,
        label="dual-partial",
        include_message=True,
        include_memory=True,
    )
    composite = compose_prepared_archive_searches(
        fixture.prepared,
        message_exact_pages=fixture.message_pages[:1],
        memory_exact_pages=fixture.memory_pages[:1],
    )
    assert composite.message_exact_pages[-1].next_continuation is not None
    assert composite.memory_exact_pages[-1].next_continuation is not None


async def test_message_chain_rejects_cursor_and_offset_gap_replay_terminal_append_and_duplicates(
    storage: Any,
) -> None:
    fixture = await _fixture(
        storage,
        label="message-order",
        include_message=True,
        include_memory=False,
    )
    first, second, terminal = fixture.message_pages
    with storage.transaction() as conn:
        replayed_second = fixture.message_adapter.prepare_in_transaction(
            conn,
            context=fixture.context,
            request=second.request,
        )
    assert replayed_second._is_process_owned()
    assert replayed_second.selection_handle == second.selection_handle
    assert terminal.offset != first.offset + len(first.rows)
    invalid_chains = (
        (first, terminal),
        (first, second, replayed_second),
        (first, second, terminal, first),
        (first, first),
    )
    for pages in invalid_chains:
        with pytest.raises(ArchiveSearchServiceError):
            compose_prepared_archive_searches(
                fixture.prepared,
                message_exact_pages=pages,
            )


async def test_memory_chain_rejects_cursor_and_offset_gap_replay_terminal_append_and_duplicates(
    storage: Any,
) -> None:
    fixture = await _fixture(
        storage,
        label="memory-order",
        include_message=False,
        include_memory=True,
    )
    first, second, terminal = fixture.memory_pages
    replayed_second = await fixture.memory_adapter.prepare(
        context=fixture.context,
        request=second.request,
    )
    assert replayed_second._is_process_owned()
    assert replayed_second.selection_handle == second.selection_handle
    assert terminal.offset != first.offset + len(first.candidates)
    invalid_chains = (
        (first, terminal),
        (first, second, replayed_second),
        (first, second, terminal, first),
        (first, first),
    )
    for pages in invalid_chains:
        with pytest.raises(ArchiveSearchServiceError):
            compose_prepared_archive_searches(
                fixture.prepared,
                memory_exact_pages=pages,
            )


async def test_composite_rejects_authority_drift_after_valid_page_continuity(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _fixture(
        storage,
        label="authority-drift",
        include_message=True,
        include_memory=False,
    )
    first, second, _terminal = fixture.message_pages
    assert first.next_continuation is not None
    assert second.offset == first.offset + len(first.rows)
    assert second.request.continuation is not None
    assert second.request.continuation.token == first.next_continuation.token
    assert {item.message_id for item in second.rows}.isdisjoint(item.message_id for item in first.rows)
    object.__setattr__(second, "authority_handle", "f" * 64)
    monkeypatch.setattr(MessageExactPage, "_is_process_owned", lambda _self: True)
    with pytest.raises(ArchiveSearchServiceError):
        compose_prepared_archive_searches(
            fixture.prepared,
            message_exact_pages=(first, second),
        )


async def test_composite_rejects_cross_scope_real_exact_pages(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await _fixture(
        storage,
        label="scope-first",
        include_message=True,
        include_memory=True,
    )
    second = await _fixture(
        storage,
        label="scope-second",
        include_message=True,
        include_memory=True,
    )
    # Isolate the outer scope/boundary check from the independently tested
    # page-chain request check.
    monkeypatch.setattr(
        "friday.retrieval.archive_search_service._message_exact_chain_is_valid",
        lambda *_args: True,
    )
    with pytest.raises(ArchiveSearchServiceError):
        compose_prepared_archive_searches(
            first.prepared,
            message_exact_pages=second.message_pages,
            memory_exact_pages=first.memory_pages,
        )
    monkeypatch.undo()
    with pytest.raises(ArchiveSearchServiceError):
        compose_prepared_archive_searches(
            first.prepared,
            message_exact_pages=first.message_pages,
            memory_exact_pages=second.memory_pages,
        )


async def test_immutable_boundary_preserves_the_original_valid_composite(storage: Any) -> None:
    fixture = await _fixture(
        storage,
        label="immutable-boundary",
        include_message=True,
        include_memory=False,
        message_rows=1,
        message_page_size=2,
    )
    with (
        pytest.raises(
            sqlite3.IntegrityError,
            match=r"текст сообщения чата неизменяем",
        ),
        storage.transaction() as conn,
    ):
        conn.execute(
            "UPDATE messages SET content=? WHERE id=?",
            ("mutated accepted boundary", fixture.boundary_id),
        )
    with storage.transaction() as conn:
        boundary = conn.execute(
            "SELECT content FROM messages WHERE id=?",
            (fixture.boundary_id,),
        ).fetchone()
    assert boundary is not None
    assert boundary["content"] == "R8H accepted boundary immutable-boundary"
    composite = compose_prepared_archive_searches(
        fixture.prepared,
        message_exact_pages=fixture.message_pages,
    )
    assert composite.message_exact_pages == fixture.message_pages


async def test_composite_rejects_more_than_32_real_exact_pages(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = await _fixture(
        storage,
        label="bounded-prefix",
        include_message=True,
        include_memory=False,
        message_rows=MAX_ARCHIVE_EXACT_CHAIN_PAGES + 1,
        message_page_size=1,
    )
    assert len(fixture.message_pages) == MAX_ARCHIVE_EXACT_CHAIN_PAGES + 1
    assert all(page._is_process_owned() for page in fixture.message_pages)
    material_calls = 0

    def observe_material(_self: PreparedArchiveSearchComposite) -> dict[str, object]:
        nonlocal material_calls
        material_calls += 1
        return {}

    monkeypatch.setattr(PreparedArchiveSearchComposite, "_material", observe_material)
    with pytest.raises(ArchiveSearchServiceError):
        compose_prepared_archive_searches(
            fixture.prepared,
            message_exact_pages=fixture.message_pages,
        )
    assert material_calls == 0


async def test_composite_seal_request_prepared_and_page_tuple_tamper_fail_closed(
    storage: Any,
) -> None:
    fixture = await _fixture(
        storage,
        label="carrier-tamper",
        include_message=True,
        include_memory=True,
    )

    def fresh() -> PreparedArchiveSearchComposite:
        return compose_prepared_archive_searches(
            fixture.prepared,
            message_exact_pages=fixture.message_pages,
            memory_exact_pages=fixture.memory_pages,
        )

    mutations: tuple[tuple[str, object], ...] = (
        ("_seal", b"x" * 32),
        ("_request", replace(fixture.request, continuation="r8h-tampered-outer-cursor")),
        ("_prepared_search", object()),
        ("_message_exact_pages", (*fixture.message_pages, fixture.message_pages[0])),
    )
    for field, value in mutations:
        composite = fresh()
        object.__setattr__(composite, field, value)
        with pytest.raises(ArchiveSearchServiceError):
            _ = composite.request


async def test_composite_repr_copy_deepcopy_and_pickle_are_private(storage: Any) -> None:
    fixture = await _fixture(
        storage,
        label="private-carrier",
        include_message=True,
        include_memory=True,
        message_rows=1,
        message_page_size=2,
        memory_rows=1,
        memory_page_size=2,
    )
    composite = compose_prepared_archive_searches(
        fixture.prepared,
        message_exact_pages=fixture.message_pages,
        memory_exact_pages=fixture.memory_pages,
    )
    assert QUERY not in repr(composite)
    assert fixture.boundary_id not in repr(composite)
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError, match="process-private"):
            operation(composite)


async def test_tool_result_serializers_omit_composite_and_private_fields(storage: Any) -> None:
    fixture = await _fixture(
        storage,
        label="tool-result",
        include_message=True,
        include_memory=True,
        message_rows=3,
        message_page_size=1,
        memory_rows=3,
        memory_page_size=1,
    )
    composite = compose_prepared_archive_searches(
        fixture.prepared,
        message_exact_pages=fixture.message_pages,
        memory_exact_pages=fixture.memory_pages,
    )
    visible = fixture.prepared.authorized_batch.model_visible_canonical_bytes.decode("ascii")
    assert fixture.message_pages[0].next_continuation is not None
    assert fixture.memory_pages[0].next_continuation is not None
    boundary_body = fixture.message_pages[0].boundary.content
    assert boundary_body == "R8H accepted boundary tool-result"
    assert boundary_body not in visible
    result = ToolResult(
        "archive_search",
        True,
        data=visible,
        prepared_archive_search=fixture.prepared,
        prepared_archive_search_composite=composite,
    )
    public = json.dumps(result.to_dict(), ensure_ascii=True, sort_keys=True)
    model = result.to_llm_message()
    for rendered in (public, model):
        assert "prepared_archive_search_composite" not in rendered
        assert "message_exact_request" not in rendered
        assert fixture.boundary_id not in rendered
        assert fixture.context.turn_id not in rendered
        assert fixture.message_pages[0].next_continuation.token not in rendered
        assert fixture.memory_pages[0].next_continuation.token not in rendered
        assert boundary_body not in rendered


async def test_tool_result_requires_one_matching_authoritative_prepared_search(storage: Any) -> None:
    first = await _fixture(
        storage,
        label="tool-authority-first",
        include_message=True,
        include_memory=False,
        message_rows=1,
        message_page_size=2,
    )
    second = await _fixture(
        storage,
        label="tool-authority-second",
        include_message=True,
        include_memory=False,
        message_rows=1,
        message_page_size=2,
    )
    first_composite = compose_prepared_archive_searches(
        first.prepared,
        message_exact_pages=first.message_pages,
    )
    second_composite = compose_prepared_archive_searches(
        second.prepared,
        message_exact_pages=second.message_pages,
    )
    visible = first.prepared.authorized_batch.model_visible_canonical_bytes.decode("ascii")
    invalid = (
        ToolResult(
            "archive_search",
            True,
            data=visible,
            prepared_archive_search=first.prepared,
            prepared_archive_search_composite=second_composite,
        ),
        ToolResult(
            "archive_search",
            True,
            data=visible,
            prepared_archive_search_composite=first_composite,
        ),
    )
    for result in invalid:
        with pytest.raises(ValueError, match="archive search result is unavailable"):
            result.archive_model_visible_bytes()
        assert result.to_dict() == {
            "tool": "archive_search",
            "success": False,
            "error": "Archive search result failed private validation",
        }
        assert result.to_llm_message() == (
            "Ошибка инструмента archive_search: результат не прошёл приватную проверку"
        )


def test_r8h_import_surface_is_passive_and_backward_compatible() -> None:
    contract = importlib.import_module("friday.retrieval.archive_search_contract")
    service = importlib.import_module("friday.retrieval.archive_search_service")
    kernel = importlib.import_module("friday.execution_kernel")
    assert ARCHIVE_SEARCH_REQUEST_SCHEMA == "friday.archive-search-request.private.v2"
    assert contract.ARCHIVE_SEARCH_REQUEST_SCHEMA_V3 == ("friday.archive-search-request.private.v3")
    assert service.MAX_ARCHIVE_EXACT_CHAIN_PAGES == 32
    assert service.PreparedArchiveSearchComposite is PreparedArchiveSearchComposite
    assert service.compose_prepared_archive_searches is compose_prepared_archive_searches
    assert tuple(kernel.ToolResult.__dataclass_fields__)[-1] == ("prepared_archive_search_composite")
    archive_parameters = inspect.signature(kernel.ExecutionKernel._archive_search).parameters
    assert "message_exact_request" not in archive_parameters
    assert "memory_exact_request" not in archive_parameters
    assert "prepared_archive_search_composite" not in archive_parameters
    assert not hasattr(kernel, "compose_prepared_archive_searches")
