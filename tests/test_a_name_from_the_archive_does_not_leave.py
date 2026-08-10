"""Explicit web searches are not blocked by names already present in the archive."""

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
async def test_every_case_of_an_archived_surname_reaches_explicit_web_search(
    kernel_and_wire,
    question,
):
    kernel, went_out = kernel_and_wire

    result = await kernel.execute("web_search", {"query": question}, actor=_actor())

    assert went_out == [question]
    assert result.data.get("refused") is not True


@pytest.mark.asyncio
async def test_the_research_road_is_open_too(kernel_and_wire):
    kernel, went_out = kernel_and_wire

    await kernel.execute("web_research", {"query": "что пишут про Хасанова"}, actor=_actor())

    assert went_out == ["что пишут про Хасанова"]


@pytest.mark.asyncio
@pytest.mark.parametrize("guard_mode", ["missing", "broken"])
async def test_archive_name_lookup_is_not_a_web_search_dependency(kernel_and_wire, guard_mode):

    kernel, went_out = kernel_and_wire
    original_storage = kernel.storage
    assert original_storage is not None

    class _StorageProxy:
        def __getattribute__(self, name):  # noqa: ANN001
            if name == "people_whose_name_starts_with":
                mode = object.__getattribute__(self, "mode")
                if mode == "missing":
                    raise AttributeError(name)

                def _broken(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
                    raise RuntimeError("synthetic private database failure")

                return _broken
            return object.__getattribute__(self, name)

        def __init__(self, delegate, mode):  # noqa: ANN001
            self.delegate = delegate
            self.mode = mode

        def __getattr__(self, name):  # noqa: ANN001
            if name == "people_whose_name_starts_with" and self.mode == "missing":
                raise AttributeError(name)
            return getattr(self.delegate, name)

    kernel.storage = _StorageProxy(original_storage, guard_mode)  # type: ignore[assignment]

    search = await kernel._web_search(  # noqa: SLF001
        actor=_actor(), query="проверь приватную справку"
    )
    research = await kernel._web_research(  # noqa: SLF001
        actor=_actor(), query="найди приватную справку"
    )

    assert search.get("refused") is not True
    assert research.get("refused") is not True
    assert went_out == ["проверь приватную справку", "найди приватную справку"]


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
