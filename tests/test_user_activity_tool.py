"""Asking in words who did what — and the three rules that makes necessary.

`user_activity` is the only tool that reads across accounts, so:

* it is gated on `admin.all_data.read` like every other tool is gated, which means
  an ordinary account cannot call it at all;
* the read is written to the audit log against the ACCOUNT it was about, not merely
  the tool's name — «кто-то смотрел активность» is not a record of anything;
* the account it resolved to comes back in the answer. `resolve_person` tolerates
  case endings, wrong layouts and typos, and a tolerant match that lands on the
  wrong person has to be visible in the reply rather than buried under a
  confident-sounding summary.

A name that matches two accounts is returned as two candidates. Guessing there is
exactly how one person's material reaches somebody who asked about another.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from jericho.execution_kernel import ExecutionKernel
from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph
from jericho.permissions import AuthorizationService
from jericho.storage.models import RawObject, new_id
from jericho.web_surfer import WebSurfer


def _arrival(storage, user_id: str, content: str, at: str) -> None:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="telegram",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    storage.execute("UPDATE raw_objects SET received_at=? WHERE id=?", (at, raw.id))
    storage.commit()


@pytest.fixture
async def kernel(settings, storage):
    storage.ensure_user("boss", preset_key="admin")
    storage.update_user("boss", preset_key="admin", display_name="Босс")
    storage.ensure_user("usr_ivan", preset_key="user")
    storage.update_user("usr_ivan", preset_key="user", display_name="Иван")
    storage.ensure_user("usr_anna", preset_key="user")
    storage.update_user("usr_anna", preset_key="user", display_name="Анна")
    _arrival(storage, "usr_ivan", "Заметка про склад", "2026-07-01T09:00:00+00:00")
    _arrival(storage, "usr_ivan", "Смета на ремонт", "2026-07-05T09:00:00+00:00")
    _arrival(storage, "usr_anna", "Чужая заметка", "2026-07-05T10:00:00+00:00")

    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    web = WebSurfer(settings)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, web, IngestionPipeline(settings, storage, graph))
    try:
        yield kernel, auth, storage
    finally:
        await web.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["Иван", "иван", "Ивану", "Иавн"])
async def test_an_admin_can_ask_by_the_name_they_use(kernel, spelling):
    runtime, auth, _ = kernel
    boss = auth.actor_for_user("boss", source="test")
    result = await runtime.execute("user_activity", {"person": spelling}, actor=boss)

    assert result.success is True, result.error
    assert result.data["resolved"]["user_id"] == "usr_ivan"
    assert result.data["summary"]["arrivals"] == 2
    assert len(result.data["items"]) == 2


@pytest.mark.asyncio
async def test_the_answer_says_which_account_it_picked_and_how(kernel):
    runtime, auth, _ = kernel
    boss = auth.actor_for_user("boss", source="test")
    result = await runtime.execute("user_activity", {"person": "Иавну"}, actor=boss)

    assert result.success is True
    resolved = result.data["resolved"]
    assert resolved["display_name"] == "Иван"
    assert resolved["method"] and resolved["method"] != "exact", "a tolerant match reported itself as exact"


@pytest.mark.asyncio
async def test_an_ordinary_account_cannot_look_at_anyone(kernel):
    runtime, auth, _ = kernel
    ivan = auth.actor_for_user("usr_ivan", source="test")
    result = await runtime.execute("user_activity", {"person": "Анна"}, actor=ivan)

    assert result.success is False
    assert "denied" in result.error.casefold() or "not allowed" in result.error.casefold()


@pytest.mark.asyncio
async def test_reading_someone_is_recorded_against_that_someone(kernel):
    runtime, auth, storage = kernel
    boss = auth.actor_for_user("boss", source="test")
    await runtime.execute("user_activity", {"person": "Иван"}, actor=boss)

    entries = [e for e in storage.list_audit_log("boss", limit=100) if e["action"] == "tool.user_activity"]
    assert entries, "an admin read another account's activity and only 'a tool ran' was recorded"
    assert entries[0]["target_id"] == "usr_ivan"


@pytest.mark.asyncio
async def test_two_people_of_the_same_name_come_back_as_a_question(kernel):
    runtime, auth, storage = kernel
    storage.ensure_user("usr_ivan2", preset_key="user")
    storage.update_user("usr_ivan2", preset_key="user", display_name="Иван")
    boss = auth.actor_for_user("boss", source="test")

    result = await runtime.execute("user_activity", {"person": "Иван"}, actor=boss)
    assert result.success is True
    assert result.data["resolved"] is None
    assert result.data["reason"] == "ambiguous"
    assert len(result.data["candidates"]) == 2


@pytest.mark.asyncio
async def test_an_unknown_name_returns_nothing_rather_than_somebody(kernel):
    runtime, auth, _ = kernel
    boss = auth.actor_for_user("boss", source="test")
    result = await runtime.execute("user_activity", {"person": "Бенедикт"}, actor=boss)

    assert result.success is True
    assert result.data["resolved"] is None
    assert result.data["reason"] == "not_found"
    assert result.data["candidates"] == []


@pytest.mark.asyncio
async def test_a_window_narrows_what_comes_back(kernel):
    runtime, auth, _ = kernel
    boss = auth.actor_for_user("boss", source="test")
    result = await runtime.execute(
        "user_activity",
        {"person": "Иван", "since": "2026-07-03T00:00:00+00:00"},
        actor=boss,
    )
    assert result.success is True
    assert result.data["summary"]["arrivals"] == 1
    assert len(result.data["items"]) == 1


@pytest.mark.asyncio
async def test_it_never_returns_a_second_accounts_rows(kernel):
    """Anna wrote on the same day; asking about Ivan must not surface her."""
    runtime, auth, _ = kernel
    boss = auth.actor_for_user("boss", source="test")
    result = await runtime.execute("user_activity", {"person": "Иван"}, actor=boss)

    previews = " ".join(str(item.get("preview") or "") for item in result.data["items"])
    assert "Чужая" not in previews


@pytest.mark.asyncio
async def test_the_admin_can_still_be_denied_by_an_explicit_override(kernel):
    runtime, auth, storage = kernel
    auth.deny_permission("boss", "admin.all_data.read")
    boss = auth.actor_for_user("boss", source="test")
    result = await runtime.execute("user_activity", {"person": "Иван"}, actor=boss)
    assert result.success is False


def test_a_settings_only_check_that_the_tool_is_declared(settings, storage):
    """The capability on the spec is the gate; a typo there would open it to everyone."""
    auth = AuthorizationService(storage)
    kernel = ExecutionKernel(auth, replace(settings))
    spec = kernel._tools["user_activity"]  # noqa: SLF001
    assert spec.security_id == "admin.all_data.read"
