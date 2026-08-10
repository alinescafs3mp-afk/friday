"""Asking in words who did what — and the three rules that makes necessary.

`user_activity` is the only tool that reads across accounts, so:

* cross-account reads are gated on `admin.activity.read`; the sole narrow
  exception is an exact documents-only read of the authenticated account under
  its ordinary `files.read` capability;
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

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage.models import RawObject, new_id
from friday.web_surfer import WebSurfer


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
    assert result.data["человек"] == "Иван"  # форма ответа человеческая: см. `_person_answer_for_llm`
    assert result.data["присылал файлов"] == 2
    assert len(result.data["файлы"]) == 2


@pytest.mark.asyncio
async def test_the_answer_says_which_account_it_picked_and_how(kernel):
    runtime, auth, _ = kernel
    boss = auth.actor_for_user("boss", source="test")
    result = await runtime.execute("user_activity", {"person": "Иавну"}, actor=boss)

    assert result.success is True
    assert result.data["человек"] == "Иван"
    # Неточное совпадение имени обязано себя объявить: иначе ответ про другого
    # человека выглядит так же уверенно, как про нужного.
    assert result.data.get("опознан приблизительно"), result.data


@pytest.mark.asyncio
async def test_an_ordinary_account_cannot_look_at_anyone(kernel):
    runtime, auth, _ = kernel
    ivan = auth.actor_for_user("usr_ivan", source="test")
    result = await runtime.execute("user_activity", {"person": "Анна"}, actor=ivan)

    assert result.success is False
    assert "denied" in result.error.casefold() or "not allowed" in result.error.casefold()


@pytest.mark.asyncio
async def test_an_ordinary_account_can_inventory_only_its_own_documents(kernel):
    runtime, auth, storage = kernel
    raw = RawObject(
        id=new_id("raw"),
        user_id="usr_ivan",
        source="upload",
        source_ref=new_id("src"),
        raw_content="OWN-DOCUMENT-CONTENT",
        content_type="file",
        content_hash=hashlib.sha256(b"own-document").hexdigest(),
        metadata_json={"filename": "own-document.pdf"},
    )
    storage.store_raw_object(raw)
    storage.commit()
    ivan = auth.actor_for_user("usr_ivan", source="test")

    own_documents = await runtime.execute(
        "user_activity",
        {"person": ivan.own_id, "documents_only": True},
        actor=ivan,
    )
    own_general_activity = await runtime.execute(
        "user_activity",
        {"person": ivan.own_id},
        actor=ivan,
    )
    someone_else = await runtime.execute(
        "user_activity",
        {"person": "usr_anna", "documents_only": True},
        actor=ivan,
    )

    assert own_documents.success is True, own_documents.error
    assert own_documents.data["документов с подтверждённым автором"] == 1
    assert own_documents.data["документы"][0]["что"] == "own-document.pdf"
    assert own_general_activity.success is False
    assert someone_else.success is False


@pytest.mark.asyncio
async def test_shared_tenant_account_inventories_its_own_documents_not_the_tenants(
    settings,
    storage,
):
    tenant = "shared-tenant"
    ivan_id = "shared-ivan"
    anna_id = "shared-anna"
    storage.ensure_user(tenant, preset_key="owner")
    storage.ensure_user(ivan_id, preset_key="user", display_name="Иван")
    storage.ensure_user(anna_id, preset_key="user", display_name="Анна")
    # An unrelated alias may equal the authenticated stable id.  Self binding
    # comes from authentication, not fuzzy directory ambiguity.
    storage.ensure_user("alias-collision", preset_key="user", display_name=ivan_id)

    def document(filename: str, *, uploaded_by: str) -> None:
        storage.store_raw_object(
            RawObject(
                id=new_id("raw"),
                user_id=tenant,
                source="upload",
                source_ref=new_id("src"),
                raw_content=filename,
                content_type="file",
                content_hash=hashlib.sha256(filename.encode()).hexdigest(),
                metadata_json={"filename": filename, "uploaded_by": uploaded_by},
            )
        )

    document("ivan-only.pdf", uploaded_by=ivan_id)
    document("anna-private.pdf", uploaded_by=anna_id)
    storage.commit()

    auth = AuthorizationService(storage, shared_tenant=tenant)
    graph = KnowledgeGraph(storage)
    runtime = ExecutionKernel(auth, settings)
    runtime.bind_services(storage, graph, object(), IngestionPipeline(settings, storage, graph))
    ivan = auth.actor_for_user(ivan_id, source="test")

    own_documents = await runtime.execute(
        "user_activity",
        {"person": ivan.own_id, "documents_only": True},
        actor=ivan,
    )
    someone_else = await runtime.execute(
        "user_activity",
        {"person": anna_id, "documents_only": True},
        actor=ivan,
    )

    assert ivan.user_id == tenant and ivan.own_id == ivan_id
    assert own_documents.success is True, own_documents.error
    assert own_documents.data["документов с подтверждённым автором"] == 1
    assert [row["что"] for row in own_documents.data["документы"]] == ["ivan-only.pdf"]
    assert someone_else.success is False


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
    assert result.data["присылал файлов"] == 1
    assert len(result.data["файлы"]) == 1


@pytest.mark.asyncio
async def test_it_never_returns_a_second_accounts_rows(kernel):
    """Anna wrote on the same day; asking about Ivan must not surface her."""
    runtime, auth, _ = kernel
    boss = auth.actor_for_user("boss", source="test")
    result = await runtime.execute("user_activity", {"person": "Иван"}, actor=boss)

    previews = " ".join(str(item.get("что") or "") for item in result.data["файлы"])
    assert "Чужая" not in previews


@pytest.mark.asyncio
async def test_denying_the_wide_capability_takes_away_the_content(kernel):
    """Явный запрет обязан отнимать ровно то, что назван отнимать.

    Раньше запрет `admin.all_data.read` закрывал инструмент целиком, потому что
    это и был его гейт. Теперь гейт — нижняя способность, и запрет старшей
    оставляет администратора с метаданными. Так и должно быть: способности здесь
    сравниваются по точному идентификатору, и запрет одной никогда не значил
    запрет другой. Но проверять надо не «инструмент доступен», а «написанного в
    ответе больше нет» — иначе запрет превратился бы в косметику.
    """
    runtime, auth, storage = kernel
    auth.deny_permission("boss", "admin.all_data.read")
    boss = auth.actor_for_user("boss", source="test")
    result = await runtime.execute("user_activity", {"person": "Иван"}, actor=boss)

    assert result.success is True, result.error
    assert result.data["доступ"] == "без содержания"
    body = " ".join(str(value) for item in result.data["файлы"] for value in item.values())
    assert "склад" not in body and "Смета" not in body


@pytest.mark.asyncio
async def test_denying_both_levels_closes_the_tool(kernel):
    runtime, auth, storage = kernel
    auth.deny_permission("boss", "admin.all_data.read")
    auth.deny_permission("boss", "admin.activity.read")
    boss = auth.actor_for_user("boss", source="test")
    result = await runtime.execute("user_activity", {"person": "Иван"}, actor=boss)
    assert result.success is False


def test_a_settings_only_check_that_the_tool_is_declared(settings, storage):
    """The capability on the spec is the gate; a typo there would open it to everyone.

    Deliberately the LOWER of the two levels. Gating on `admin.all_data.read` would
    shut the metadata tier out of the agent entirely, so the disclosure decision
    moved into the handler — which is why this pin is worth keeping: reading it as
    «the gate is weaker now» without noticing where the content check went is
    exactly the mistake that would open every body to the narrow level.
    """
    auth = AuthorizationService(storage)
    kernel = ExecutionKernel(auth, replace(settings))
    spec = kernel._tools["user_activity"]  # noqa: SLF001
    assert spec.security_id == "admin.activity.read"


@pytest.mark.asyncio
async def test_the_metadata_tier_reaches_the_tool_but_not_the_text(kernel):
    """Инструмент — та поверхность, где различие теряется легче всего.

    Гейт стоит на НИЖНЕЙ способности, иначе наблюдатель не смог бы позвать
    инструмент вовсе. Значит редактирование обязано жить внутри обработчика: без
    него один гейт молча выдал бы наблюдателю всё написанное — через агента,
    в свободном тексте, где потом не разберёшь, откуда это взялось.
    """
    runtime, auth, storage = kernel
    storage.ensure_user("watcher", preset_key="user")
    auth.grant_permission("watcher", "admin.activity.read")
    watcher = auth.actor_for_user("watcher", source="test")

    result = await runtime.execute("user_activity", {"person": "Иван"}, actor=watcher)

    assert result.success is True, result.error
    assert result.data["доступ"] == "без содержания"
    assert result.data["присылал файлов"] == 2, "объём активности наблюдателю виден"
    body = " ".join(str(value) for item in result.data["файлы"] for value in item.values())
    for secret in ("склад", "Смета", "ремонт"):
        assert secret not in body, f"инструмент отдал наблюдателю {secret!r}"


@pytest.mark.asyncio
async def test_the_tool_lists_itself_for_the_metadata_tier(kernel):
    """Если инструмента нет в списке, модель его не позовёт, и уровень мёртв."""
    runtime, auth, storage = kernel
    storage.ensure_user("watcher2", preset_key="user")
    auth.grant_permission("watcher2", "admin.activity.read")
    watcher = auth.actor_for_user("watcher2", source="test")

    assert "user_activity" in runtime.get_tool_names(watcher)
    assert "user_activity" not in runtime.get_tool_names(auth.actor_for_user("usr_ivan", source="test"))
