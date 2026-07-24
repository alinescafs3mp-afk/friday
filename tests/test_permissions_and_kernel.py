from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from jericho.execution_kernel import ExecutionKernel
from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import AuthorizationService
from jericho.web_surfer import WebSurfer


def test_default_deny_presets_and_persistent_overrides(storage):
    storage.ensure_user("guest-user", preset_key="guest")
    storage.ensure_user("normal-user", preset_key="user")
    auth = AuthorizationService(storage)
    guest = auth.actor_for_user("guest-user", source="test")
    normal = auth.actor_for_user("normal-user", source="test")
    assert auth.authorize(guest, "knowledge.create").allowed is False
    assert auth.authorize(normal, "knowledge.create").allowed is True
    assert auth.authorize(normal, "unknown.capability").allowed is False

    auth.deny_permission("normal-user", "knowledge.create")
    assert auth.authorize(normal, "knowledge.create").allowed is False
    auth.grant_permission("guest-user", "knowledge.create")
    assert auth.authorize(guest, "knowledge.create").allowed is True

    preset = auth.create_custom_preset(
        "reader_only",
        "Reader only",
        {"knowledge.read", "search.use"},
        created_by="normal-user",
    )
    assert preset["preset_key"] == "reader_only"
    auth.set_user_preset("guest-user", "reader_only")
    custom_actor = auth.actor_for_user("guest-user", source="test")
    assert auth.authorize(custom_actor, "search.use").allowed is True
    assert auth.authorize(custom_actor, "knowledge.create").allowed is True  # explicit allow wins


@pytest.mark.asyncio
async def test_kernel_enforces_actor_and_never_captures_one_owner(settings, storage):
    storage.ensure_user("alice", preset_key="user")
    storage.ensure_user("bob", preset_key="user")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(settings, storage, graph)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, web, ingestion)
    alice = auth.actor_for_user("alice", source="test")
    bob = auth.actor_for_user("bob", source="test")
    try:
        saved = await kernel.execute(
            "memory_save",
            {"title": "Alpha", "content": "Project Alpha launch plan", "tags": ["alpha"]},
            actor=alice,
        )
        assert saved.success is True
        # Agent tools may prepare durable knowledge, but they cannot bypass the
        # same moderate-classification review boundary as Telegram/Admin flows.
        assert storage.count_knowledge_objects("alice") == 0
        pending = storage.list_inbox("alice", status="pending")
        assert len(pending) == 1
        assert saved.data["queued_for_review"] is True
        assert saved.data["promoted"] is False
        assert storage.count_knowledge_objects("bob") == 0

        bob_search = await kernel.execute("memory_search", {"query": "Alpha"}, actor=bob)
        assert bob_search.success is True
        assert bob_search.data["count"] == 0

        code = await kernel.execute("code_run", {"code": "print(1)"}, actor=alice)
        assert code.success is False
        assert "denied" in code.error.casefold() or "disabled" in code.error.casefold()
        audit = storage.list_audit_log("alice")
        assert any(entry["target_id"] == "memory_save" for entry in audit)
    finally:
        await web.close()


@pytest.mark.asyncio
async def test_timed_out_code_execution_kills_child(settings, storage, tmp_path):
    storage.ensure_user("operator", preset_key="owner")
    executable_settings = replace(
        settings,
        code_execution_enabled=True,
        code_execution_timeout_sec=1,
    )
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    ingestion = IngestionPipeline(executable_settings, storage, graph)
    web = WebSurfer(executable_settings)
    kernel = ExecutionKernel(auth, executable_settings)
    kernel.bind_services(storage, graph, web, ingestion)
    actor = auth.actor_for_user("operator", source="test")
    marker = tmp_path / "child-survived.txt"
    child_code = (
        "import pathlib, time; "
        "time.sleep(2); "
        f"pathlib.Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    )
    code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-I', '-S', '-B', '-c', {child_code!r}])\n"
        "time.sleep(20)\n"
    )
    try:
        result = await kernel.execute("code_run", {"code": code}, actor=actor)
        assert result.success is False
        assert "timed out" in result.error.casefold()
        await asyncio.sleep(1.5)
        assert not marker.exists()
    finally:
        await web.close()
