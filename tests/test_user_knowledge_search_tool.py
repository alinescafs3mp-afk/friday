"""Спросить по существу о том, что написал другой человек, не сливая корпуса.

`user_activity` отвечает про объём и темы — «что прислал, когда, сколько».
На вопрос «а что он писал про сроки поставки» она ответить не может, а обычный
поиск ограничен своим арендатором by design. Между ними была дыра: старший,
которому владелец разрешил видеть содержимое, читал чужое только глазами.

Изоляция при этом не снимается — арендатор остаётся ПАРАМЕТРОМ поиска. Меняется
одно: кто вправе назвать чужой арендатор этим параметром. Отсюда три правила,
которые здесь и закреплены: гейт на ВЕРХНЕМ праве (метаданного уровня у этого
инструмента не бывает — любой результат есть содержимое), запись в аудит на
ЦЕЛЕВОЙ аккаунт вместе с самим вопросом, и честный отказ вместо пустоты.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage.models import KnowledgeObject, RawObject, new_id
from friday.web_surfer import WebSurfer


def _knowledge(storage, user_id: str, text: str, title: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="telegram",
        source_ref=new_id("src"),
        raw_content=text,
        content_type="text",
        content_hash=hashlib.sha256(f"{user_id}{text}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        content_type="text",
        title=title,
        summary=text[:120],
    )
    storage.store_knowledge_object(knowledge)
    return knowledge.id


@pytest.fixture
async def kernel(settings, storage):
    storage.ensure_user("boss", preset_key="admin")
    storage.update_user("boss", preset_key="admin", display_name="Босс")
    storage.ensure_user("usr_ivan", preset_key="user")
    storage.update_user("usr_ivan", preset_key="user", display_name="Иван")
    storage.ensure_user("usr_anna", preset_key="user")
    storage.update_user("usr_anna", preset_key="user", display_name="Анна")
    _knowledge(storage, "usr_ivan", "Срыв сроков поставки оборудования на склад в июле.", "Поставка")
    _knowledge(storage, "usr_ivan", "Рутинная заметка про обед и погоду.", "Обед")
    _knowledge(storage, "usr_anna", "Совсем другая тема: отпуск и билеты.", "Отпуск")

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
async def test_an_admin_gets_the_text_of_what_that_person_wrote(kernel):
    runtime, auth, _ = kernel
    boss = auth.actor_for_user("boss", source="test")

    result = await runtime.execute(
        "user_knowledge_search", {"person": "Ивану", "query": "срыв сроков поставки"}, actor=boss
    )

    assert result.success is True, result.error
    assert result.data["resolved"]["user_id"] == "usr_ivan"
    titles = [item["title"] for item in result.data["results"]]
    assert "Поставка" in titles
    excerpt = next(item["excerpt"] for item in result.data["results"] if item["title"] == "Поставка")
    assert "срок" in excerpt.casefold(), "выдержка не показывает написанное"


@pytest.mark.asyncio
async def test_the_isolation_holds_the_other_way(kernel):
    """Корпус Анны не должен просачиваться в ответ про Ивана."""
    runtime, auth, _ = kernel
    boss = auth.actor_for_user("boss", source="test")

    result = await runtime.execute(
        "user_knowledge_search", {"person": "Иван", "query": "отпуск билеты"}, actor=boss
    )

    assert result.success is True
    assert all(item["title"] != "Отпуск" for item in result.data["results"])


@pytest.mark.asyncio
async def test_an_ordinary_account_cannot_read_anyone(kernel):
    runtime, auth, _ = kernel
    ivan = auth.actor_for_user("usr_ivan", source="test")

    result = await runtime.execute("user_knowledge_search", {"person": "Анна", "query": "отпуск"}, actor=ivan)

    assert result.success is False
    assert "denied" in result.error.casefold() or "not allowed" in result.error.casefold()


@pytest.mark.asyncio
async def test_the_tool_is_invisible_without_the_capability(kernel):
    """Гейт — не только на вызове: обычный аккаунт не должен даже видеть инструмент."""
    runtime, auth, _ = kernel
    ivan = auth.actor_for_user("usr_ivan", source="test")
    boss = auth.actor_for_user("boss", source="test")

    assert "user_knowledge_search" not in set(runtime.get_tool_names(ivan))
    assert "user_knowledge_search" in set(runtime.get_tool_names(boss))


@pytest.mark.asyncio
async def test_reading_someone_is_recorded_against_them_with_the_question(kernel):
    runtime, auth, storage = kernel
    boss = auth.actor_for_user("boss", source="test")

    await runtime.execute(
        "user_knowledge_search", {"person": "Иван", "query": "срыв сроков поставки"}, actor=boss
    )

    rows = [
        row
        for row in storage.list_audit_log(limit=50)
        if str(row.get("action")) == "tool.user_knowledge_search"
    ]
    assert rows, "чтение чужого корпуса не записано"
    entry = rows[0]
    assert entry["target_id"] == "usr_ivan", "запись не на том, кого читали"
    after = entry.get("after") if isinstance(entry.get("after"), dict) else json.loads(entry["after_json"])
    assert "поставк" in str(after.get("query")).casefold(), "в журнале нет самого вопроса"


@pytest.mark.asyncio
async def test_an_ambiguous_name_answers_with_candidates_and_leaves_a_trace(kernel):
    runtime, auth, storage = kernel
    storage.ensure_user("usr_ivan2", preset_key="user")
    storage.update_user("usr_ivan2", preset_key="user", display_name="Иван")
    boss = auth.actor_for_user("boss", source="test")

    result = await runtime.execute(
        "user_knowledge_search", {"person": "Иван", "query": "поставка"}, actor=boss
    )

    assert result.success is True
    assert result.data["resolved"] is None and result.data["reason"] == "ambiguous"
    assert len(result.data["candidates"]) == 2
    assert any(
        str(row.get("action")) == "tool.user_knowledge_search.unresolved"
        for row in storage.list_audit_log(limit=50)
    ), "перебор неоднозначных имён не оставил следа"


@pytest.mark.asyncio
async def test_storage_fallback_clamps_limit_like_the_searcher_path(kernel, monkeypatch):
    """Without a HybridSearcher the bare FTS path used to honour limit=999.

    Schema says maximum 20, but execute() does not enforce schema bounds —
    the handler itself must clamp both branches the same way.
    """
    runtime, auth, storage = kernel
    boss = auth.actor_for_user("boss", source="test")
    assert runtime.searcher is None, "precondition: fixture leaves searcher unbound"

    seen: list[int] = []
    real = storage.search_knowledge

    def _spy(user_id, query, *, limit=20):
        seen.append(int(limit))
        return real(user_id, query, limit=limit)

    monkeypatch.setattr(storage, "search_knowledge", _spy)

    result = await runtime.execute(
        "user_knowledge_search",
        {"person": "Иван", "query": "поставка", "limit": 999},
        actor=boss,
    )

    assert result.success is True, result.error
    assert seen == [20], f"storage got unbounded limit: {seen}"
