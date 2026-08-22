from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pytest

from friday.organs import ServiceContext
from friday.organs.obsidian import ObsidianOrgan
from friday.organs.obsidian.tools import build_obsidian_tools
from friday.permissions import ActorContext


@dataclass
class FakeRuntime:
    calls: list[tuple[str, str, tuple[object, ...], dict[str, object]]] = field(default_factory=list)

    def _record(self, method: str, owner_id: str, *args: object, **kwargs: object) -> None:
        self.calls.append((method, owner_id, args, kwargs))

    async def vaults(self, owner_id: str) -> list[dict[str, Any]]:
        self._record("vaults", owner_id)
        return [{"id": "vault-1"}]

    async def list_notes(self, owner_id: str) -> list[dict[str, Any]]:
        self._record("list_notes", owner_id)
        return [{"path": "note.md"}]

    async def search_notes(
        self,
        owner_id: str,
        query: str,
        limit: int = 20,
        *,
        context_key: str | None = None,
    ) -> list[dict[str, Any]]:
        self._record("search_notes", owner_id, query, limit=limit, context_key=context_key)
        return [{"path": "match.md"}]

    async def search_index_coverage(self, _owner_id: str) -> dict[str, Any]:
        return {
            "state": "partial",
            "known_notes": 2,
            "indexed_notes": 1,
            "complete_notes": 1,
            "semantic_lane": "local_approximate",
        }

    async def read_note(self, owner_id: str, path: str) -> dict[str, Any]:
        self._record("read_note", owner_id, path)
        return {"path": path, "content": "body"}

    async def create_note(
        self,
        owner_id: str,
        operation_id: str,
        path: str,
        content: str = "",
        properties: dict[str, object] | None = None,
        work_item_id: str | None = None,
        context_key: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "create_note",
            owner_id,
            operation_id,
            path,
            content=content,
            properties=properties,
            work_item_id=work_item_id,
            context_key=context_key,
        )
        return {"operation_id": operation_id, "status": "scan_pending"}

    async def append_note(
        self,
        owner_id: str,
        operation_id: str,
        path: str,
        text: str,
        expected_revision: str | None = None,
        work_item_id: str | None = None,
        context_key: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "append_note",
            owner_id,
            operation_id,
            path,
            text,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
            context_key=context_key,
        )
        return {"operation_id": operation_id, "status": "scan_pending"}

    async def set_properties(
        self,
        owner_id: str,
        operation_id: str,
        path: str,
        properties: dict[str, object],
        expected_revision: str | None = None,
        work_item_id: str | None = None,
        context_key: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "set_properties",
            owner_id,
            operation_id,
            path,
            properties,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
            context_key=context_key,
        )
        return {"operation_id": operation_id, "status": "scan_pending"}

    async def daily_note(
        self,
        owner_id: str,
        operation_id: str,
        day: date | None = None,
        content: str = "",
        expected_revision: str | None = None,
        work_item_id: str | None = None,
        context_key: str | None = None,
    ) -> dict[str, Any]:
        self._record(
            "daily_note",
            owner_id,
            operation_id,
            day=day,
            content=content,
            expected_revision=expected_revision,
            work_item_id=work_item_id,
            context_key=context_key,
        )
        return {"operation_id": operation_id, "status": "scan_pending"}


def _context(runtime: object | None) -> ServiceContext:
    return ServiceContext(
        settings=None,  # type: ignore[arg-type]
        storage=None,
        kg=None,
        ingestion=None,
        obsidian=runtime,
    )


def test_organ_registers_no_tools_without_the_optional_runtime() -> None:
    context = _context(None)

    assert build_obsidian_tools(context) == ()
    assert ObsidianOrgan().tools(context) == ()


def test_tool_metadata_is_closed_and_declares_read_write_risk() -> None:
    tools = {tool.name: tool for tool in ObsidianOrgan().tools(_context(FakeRuntime()))}

    assert set(tools) == {
        "obsidian_list_vaults",
        "obsidian_list_notes",
        "obsidian_search_notes",
        "obsidian_read_note",
        "obsidian_create_note",
        "obsidian_append_note",
        "obsidian_set_properties",
        "obsidian_daily_note",
        "obsidian_workflow_read",
        "obsidian_workflow_write",
    }
    assert {name for name, tool in tools.items() if tool.risk == "observe"} == {
        "obsidian_list_vaults",
        "obsidian_list_notes",
        "obsidian_search_notes",
        "obsidian_read_note",
        "obsidian_workflow_read",
    }
    assert {name for name, tool in tools.items() if tool.risk == "mutate"} == {
        "obsidian_create_note",
        "obsidian_append_note",
        "obsidian_set_properties",
        "obsidian_daily_note",
        "obsidian_workflow_write",
    }
    assert all(
        tool.security_id == ("obsidian.read" if tool.risk == "observe" else "obsidian.write")
        for tool in tools.values()
    )
    assert all(tool.parameters["additionalProperties"] is False for tool in tools.values())
    schemas = json.dumps({name: tool.parameters for name, tool in tools.items()}, sort_keys=True)
    assert "owner_id" not in schemas
    assert "user_id" not in schemas
    workflow_actions = tools["obsidian_workflow_write"].parameters["properties"]["action"]["enum"]
    assert "accept_conflict_merge" in workflow_actions
    workflow_read = tools["obsidian_workflow_read"].parameters["properties"]
    assert "summarize_today_notes" in workflow_read["action"]["enum"]
    assert workflow_read["day"] == {"type": "string", "format": "date"}


@pytest.mark.asyncio
async def test_every_handler_uses_actor_own_id_and_forwards_closed_arguments() -> None:
    runtime = FakeRuntime()
    tools = {tool.name: tool for tool in build_obsidian_tools(_context(runtime))}
    actor = ActorContext(
        user_id="shared-archive",
        preset_key="owner",
        source="test",
        shared_tenant=True,
        person_id="person-alice",
    )
    revision = "a" * 64
    properties: dict[str, object] = {
        "status": "review",
        "due": {"type": "date", "value": "2026-08-22"},
    }

    await tools["obsidian_list_vaults"].handler(actor=actor)  # type: ignore[misc]
    await tools["obsidian_list_notes"].handler(actor=actor)  # type: ignore[misc]
    search_result = await tools["obsidian_search_notes"].handler(  # type: ignore[misc]
        actor=actor, query="Friday", limit=7
    )
    await tools["obsidian_read_note"].handler(actor=actor, path="Notes/Friday.md")  # type: ignore[misc]
    await tools["obsidian_create_note"].handler(  # type: ignore[misc]
        actor=actor,
        operation_id="create-1",
        path="Notes/New.md",
        content="created",
        properties=properties,
        work_item_id="work-1",
    )
    await tools["obsidian_append_note"].handler(  # type: ignore[misc]
        actor=actor,
        operation_id="append-1",
        path="Notes/New.md",
        text="appended",
        expected_revision=revision,
        work_item_id="work-2",
    )
    await tools["obsidian_set_properties"].handler(  # type: ignore[misc]
        actor=actor,
        operation_id="properties-1",
        path="Notes/New.md",
        properties=properties,
        expected_revision=revision,
        work_item_id="work-3",
    )
    await tools["obsidian_daily_note"].handler(  # type: ignore[misc]
        actor=actor,
        operation_id="daily-1",
        day="2026-08-21",
        content="day",
        expected_revision=revision,
        work_item_id="work-4",
    )

    assert all(call[1] == "person-alice" for call in runtime.calls)
    assert [call[0] for call in runtime.calls] == [
        "vaults",
        "list_notes",
        "search_notes",
        "read_note",
        "create_note",
        "append_note",
        "set_properties",
        "daily_note",
    ]
    assert runtime.calls[2][2:] == (
        ("Friday",),
        {"limit": 7, "context_key": "person-alice"},
    )
    assert search_result["coverage"]["state"] == "partial"
    assert runtime.calls[4][2:] == (
        ("create-1", "Notes/New.md"),
        {
            "content": "created",
            "properties": properties,
            "work_item_id": "work-1",
            "context_key": "person-alice",
        },
    )
    assert runtime.calls[6][2][2] == properties
    assert runtime.calls[7][3]["day"] == date(2026, 8, 21)


@pytest.mark.asyncio
async def test_daily_note_rejects_a_non_iso_day_before_runtime_dispatch() -> None:
    runtime = FakeRuntime()
    tool = next(
        item for item in build_obsidian_tools(_context(runtime)) if item.name == "obsidian_daily_note"
    )

    with pytest.raises(ValueError):
        await tool.handler(actor=ActorContext("alice", "user", "test"), operation_id="daily", day="today")  # type: ignore[misc]

    assert runtime.calls == []
