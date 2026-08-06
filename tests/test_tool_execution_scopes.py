"""A mission's tool list is not authority; the kernel enforces its scope too.

The model may name a function that was not included in its tool definitions.
Permissions alone do not stop that in a mission because the step deliberately
runs as its owner. Execution scope is therefore trusted caller context and is
checked both while selecting definitions and immediately before dispatch.
"""

from __future__ import annotations

import json

import pytest

from friday.execution_kernel import (
    EXECUTION_SCOPES,
    MISSION_EXECUTION_TOOLS,
    ExecutionKernel,
    ToolSpec,
)
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.web_surfer import WebSurfer


@pytest.mark.asyncio
async def test_a_dialogue_tool_named_directly_by_a_mission_has_no_effect(settings, storage):
    """Mutation: remove the dispatch-time scope check and Inbox gains one row."""

    storage.ensure_user("alice", preset_key="owner")
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, graph, web, ingestion)
    actor = authorization.actor_for_user("alice", source="mission-test")

    try:
        visible = {
            item["function"]["name"] for item in kernel.get_tool_definitions(actor, execution_scope="mission")
        }
        assert visible == MISSION_EXECUTION_TOOLS
        assert "memory_save" not in visible

        denied = await kernel.execute(
            "memory_save",
            {"content": "synthetic mission payload"},
            actor=actor,
            execution_scope="mission",
        )

        assert denied.success is False
        assert "scope" in denied.error.casefold()
        assert storage.list_inbox("alice", status="pending") == []
        audit = [
            row
            for row in storage.list_audit_log("alice", limit=10)
            if row["action"] == "tool.invoke" and row["target_id"] == "memory_save"
        ]
        after = json.loads(str(audit[0]["after_json"]))
        assert after["reason"] == "execution_scope_denied"
        assert after["execution_scope"] == "mission"

        allowed = await kernel.execute(
            "memory_search",
            {"query": "synthetic absent fact"},
            actor=actor,
            execution_scope="mission",
        )
        assert allowed.success is True
    finally:
        await web.close()


def test_execution_scope_declarations_are_closed_and_fail_closed(settings, storage):
    kernel = ExecutionKernel(AuthorizationService(storage), settings)

    assert {"dialogue", "mission"} == EXECUTION_SCOPES
    by_scope = {
        name: tool.allowed_execution_scopes
        for name, tool in kernel._tools.items()  # noqa: SLF001 - registry invariant
    }
    assert {name for name, scopes in by_scope.items() if "mission" in scopes} == MISSION_EXECUTION_TOOLS
    assert all(scopes and scopes <= EXECUTION_SCOPES for scopes in by_scope.values())

    with pytest.raises(ValueError, match="execution scope"):
        ToolSpec(
            name="synthetic_bad_scope",
            description="",
            parameters={"type": "object", "properties": {}},
            security_id="knowledge.read",
            risk="observe",
            allowed_execution_scopes=frozenset({"tool_argument"}),
        )
