"""R8B keeps legacy dialogue recall while establishing a closed internal lane."""

from __future__ import annotations

from collections import Counter

import pytest

from friday.execution_kernel import (
    INTERNAL_SEARCH_ADAPTER_TOOLS,
    ExecutionKernel,
)
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.retrieval_benchmark.parity import run_parity_ephemeral


def _bound_kernel(settings, storage):  # noqa: ANN001, ANN202
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(
        storage,
        graph,
        object(),
        IngestionPipeline(settings, storage, graph),
    )
    return kernel, authorization


def test_internal_scope_is_closed_after_dialogue_catalog_cutover(settings, storage) -> None:
    storage.ensure_user("r8b-owner", preset_key="owner")
    kernel, authorization = _bound_kernel(settings, storage)
    actor = authorization.actor_for_user("r8b-owner", source="r8b-test")

    dialogue = set(kernel.get_tool_names(actor))
    internal = set(kernel.get_tool_names(actor, execution_scope="internal"))

    assert "archive_search" in dialogue
    assert dialogue.isdisjoint(INTERNAL_SEARCH_ADAPTER_TOOLS)
    assert internal == INTERNAL_SEARCH_ADAPTER_TOOLS
    assert "archive_search" not in internal
    assert all(kernel.internal_search_adapter_available(name, actor) for name in internal)
    assert kernel.internal_search_adapter_available("archive_search", actor) is False


@pytest.mark.asyncio
async def test_internal_execute_uses_fresh_transactional_account_authority(settings, storage) -> None:
    storage.ensure_user("r8b-stale-principal", preset_key="guest")
    kernel, authorization = _bound_kernel(settings, storage)
    authorization.create_custom_preset(
        "r8b_no_retrieval",
        "R8B no retrieval",
        set(),
        created_by="r8b-stale-principal",
    )
    authorization.set_user_preset("r8b-stale-principal", "r8b_no_retrieval")
    stale_actor = authorization.actor_for_user("r8b-stale-principal", source="r8b-test")
    assert kernel.internal_search_adapter_available("source_search", stale_actor) is False

    # The stale no-retrieval actor lacks knowledge.read, while the current durable user
    # preset grants it.  Internal execution must use the latter snapshot.
    storage.update_user("r8b-stale-principal", preset_key="user")
    assert kernel.internal_search_adapter_available("source_search", stale_actor) is True
    granted = await kernel.execute(
        "source_search",
        {"query": "synthetic-absent-r8b-canary"},
        actor=stale_actor,
        execution_scope="internal",
    )
    assert granted.success is True
    assert granted.handler_entered is True

    # Conversely, the stale actor still carries a valid preset after the account
    # is disabled.  Status from the fresh transaction must stop dispatch.
    storage.update_user("r8b-stale-principal", status="disabled")
    assert kernel.internal_search_adapter_available("memory_search", stale_actor) is False
    denied = await kernel.execute(
        "memory_search",
        {"query": "synthetic-absent-r8b-canary"},
        actor=stale_actor,
        execution_scope="internal",
    )
    assert denied.success is False
    assert denied.error == "Authorization denied"
    assert denied.handler_entered is False


@pytest.mark.asyncio
async def test_internal_adapter_contract_and_message_capability_drift_fail_closed(settings, storage) -> None:
    storage.ensure_user("r8b-contract-owner", preset_key="owner")
    kernel, authorization = _bound_kernel(settings, storage)
    actor = authorization.actor_for_user("r8b-contract-owner", source="r8b-test")

    source_tool = kernel.get_tool("source_search")
    assert source_tool is not None
    source_tool.security_id = "weather.read"
    assert kernel.internal_search_adapter_available("source_search", actor) is False
    drifted = await kernel.execute(
        "source_search",
        {"query": "synthetic-absent-r8b-canary"},
        actor=actor,
        execution_scope="internal",
    )
    assert drifted.success is False
    assert drifted.handler_entered is False

    authorization.deny_permission("r8b-contract-owner", "conversations.read")
    assert kernel.internal_search_adapter_available("message_search", actor) is False
    denied = await kernel.execute(
        "message_search",
        {"query": "synthetic-absent-r8b-canary"},
        actor=actor,
        execution_scope="internal",
    )
    assert denied.success is False
    assert denied.error == "Authorization denied"
    assert denied.handler_entered is False


def test_parity_calls_every_legacy_adapter_through_internal_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []
    original_execute = ExecutionKernel.execute

    async def recording_execute(  # noqa: ANN202
        self,
        name,
        arguments,
        *,
        actor=None,
        execution_scope="dialogue",
    ):
        if name in INTERNAL_SEARCH_ADAPTER_TOOLS:
            calls.append((name, execution_scope))
        return await original_execute(
            self,
            name,
            arguments,
            actor=actor,
            execution_scope=execution_scope,
        )

    monkeypatch.setattr(ExecutionKernel, "execute", recording_execute)

    report = run_parity_ephemeral()

    assert len(report.cases) == 7
    assert Counter(calls) == Counter(
        {
            ("memory_search", "internal"): 1,
            ("message_search", "internal"): 3,
            ("source_search", "internal"): 3,
        }
    )
