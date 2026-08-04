"""Фамилия сотрудника не уходит в чужой поисковик — на ЛЮБОЙ дороге и в любом падеже.

Две находки одного замера (2026-08-04, изолированный стенд).

ПЕРВАЯ: ворота стояли на одной дороге. Проверка «есть ли в вопросе имя человека
из графа» жила в `agent_runtime._prefetch_the_web_if_asked` — то есть на дороге
предвыборки. Модель зовёт `web_search` и `web_research` НАПРЯМУЮ, и этой дорогой
запрос уходил целиком. Замерено: «что известно про Хасанова Рустама Маратовича?»
ушло в поисковик со всеми тремя словами.

ВТОРАЯ, тяжелее: сама проверка узнавала одну форму из шести. Она звала
`search_entities`, а тот находит только точное совпадение:

    'Хасанов'    → найден
    'Хасанова'   → НЕ найден
    'Хасанову'   → НЕ найден
    'Хасановым'  → НЕ найден
    'хасан'      → НЕ найден
    'Маратовича' → НЕ найден

Спрашивают же как раз в косвенном падеже — «про Хасанова». То есть ворота, ради
которых писался отдельный механизм, срабатывали в меньшинстве случаев.

Теперь сравнивается ОСНОВА (первые пять букв) префиксным поиском по индексу
`(user_id, entity_type, normalized_name)`, и проверка стоит в самом инструменте —
в единственном месте, через которое наружу идёт всё.

Ошибка в сторону «лишний отказ» здесь дешевле: человек увидит его сразу и
переспросит, а ушедшую фамилию не вернуть — в журнале остаётся только хеш
запроса, и владелец никогда не узнает, что именно ушло.
"""

from __future__ import annotations

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService
from friday.storage.models import Entity, EntityType, new_id
from friday.web_surfer import WebSurfer


@pytest.fixture
def kernel_and_wire(settings, storage):
    """Ядро с человеком в графе и перехватом всего, что уходит наружу."""
    storage.ensure_user("alice", preset_key="owner")
    storage.create_entity(
        Entity(
            id=new_id("ent"),
            user_id="alice",
            name="Хасанов Рустам Маратович",
            entity_type=EntityType.PERSON.value,
        )
    )
    storage.commit()

    graph = KnowledgeGraph(storage)
    web = WebSurfer(settings)
    went_out: list[str] = []

    async def _capture(query: str, *, max_results: int = 5):
        went_out.append(query)
        return []

    async def _capture_research(query: str, *, max_sources: int = 3):
        went_out.append(query)
        return {"query": query, "sources": []}

    web.search = _capture  # type: ignore[method-assign]
    web.research = _capture_research  # type: ignore[method-assign]

    instance = ExecutionKernel(AuthorizationService(storage), settings)
    instance.bind_services(storage, graph, web, IngestionPipeline(settings, storage, graph))
    return instance, went_out


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "что известно про Хасанова Рустама Маратовича?",
        "Хасанов Рустам — кто это?",
        "погугли Хасанову характеристику",
        "новости про Хасановым",
        "найди Хасанов Рустам Маратович линкедин",
    ],
)
@pytest.mark.asyncio
async def test_no_case_of_the_surname_leaves(kernel_and_wire, question):
    """Мутация: сравнивать слово целиком вместо основы — тест краснеет."""
    kernel, went_out = kernel_and_wire

    result = await kernel.execute("web_search", {"query": question}, actor=_actor())

    assert went_out == [], f"фамилия ушла в поисковик: {went_out}"
    assert result.data.get("refused") is True
    assert "архива" in str(result.data.get("reason") or "")


@pytest.mark.asyncio
async def test_the_research_road_is_closed_too(kernel_and_wire):
    """Обе дороги наружу, а не одна: ворота на одной не охраняют ничего."""
    kernel, went_out = kernel_and_wire

    await kernel.execute(
        "web_research", {"query": "что пишут про Хасанова"}, actor=_actor()
    )

    assert went_out == [], f"вторая дорога осталась открытой: {went_out}"


@pytest.mark.asyncio
async def test_an_ordinary_question_still_goes_out(kernel_and_wire):
    """Ошибка в другую сторону: обычный вопрос обязан искаться.

    Слишком широкая проверка молча отключила бы поиск целиком, и это заметили бы
    не сразу — ответы просто стали бы хуже.
    """
    kernel, went_out = kernel_and_wire

    await kernel.execute("web_search", {"query": "курс евро на сегодня"}, actor=_actor())

    assert went_out == ["курс евро на сегодня"]


@pytest.mark.asyncio
async def test_a_long_message_is_cut_before_it_leaves(kernel_and_wire):
    """Наружу уходит запрос, а не пересказ обстоятельств.

    Замерено: прямой вызов отправлял реплику целиком — 371 знак разговорного
    текста. Поисковику столько не нужно, а цена утечки растёт с каждым знаком.
    """
    from friday.execution_kernel import _MAX_OUTBOUND_QUERY_CHARS

    kernel, went_out = kernel_and_wire
    long_message = "цены на одноплатные компьютеры " * 20

    await kernel.execute("web_search", {"query": long_message}, actor=_actor())

    assert went_out, "запрос не ушёл вовсе"
    assert len(went_out[0]) == _MAX_OUTBOUND_QUERY_CHARS
    assert len(long_message) > _MAX_OUTBOUND_QUERY_CHARS, "стенд собран неверно"


def test_the_stems_are_matched_by_prefix(storage):
    """Отдельно — сам поиск по основе, чтобы падение было читаемым."""
    storage.ensure_user("alice")
    storage.create_entity(
        Entity(
            id=new_id("ent"),
            user_id="alice",
            name="Хасанов Рустам Маратович",
            entity_type=EntityType.PERSON.value,
        )
    )
    storage.commit()

    assert storage.people_whose_name_starts_with("alice", ["хасан"])
    assert storage.people_whose_name_starts_with("alice", ["Хасан"])
    assert not storage.people_whose_name_starts_with("alice", ["курс"])
    # Короткие обрывки не годятся: «нов» совпало бы с половиной фамилий.
    assert not storage.people_whose_name_starts_with("alice", ["ха"])
