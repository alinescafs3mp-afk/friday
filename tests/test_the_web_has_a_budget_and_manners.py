"""Выход в интернет ограничен по числу и вежлив по темпу.

Способность `web.search` есть у пресета `user`, участников одиннадцать. До этой
правки потолка не было вовсе: один зациклившийся research тратил платный ключ и
портил репутацию адреса, и заметить это можно было только по счёту.

Размер потолка взят ЗАМЕРОМ на живом архиве, а не из головы: пик — 135 вызовов
веб-инструментов на человека за сутки, медиана по человеко-дням 76. Потолок 400 —
тройной запас над настоящим пиком, потому что защита нужна не от работающего
человека, а от цикла.

Ворота стоят на ВСЕХ трёх дорогах наружу. `_what_must_not_leave` зовут только две
из трёх, и квота на том же месте охраняла бы через раз — этот класс на проекте
уже находили.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Web:
    """Сеть, которая считает, сколько раз к ней обратились."""

    def __init__(self) -> None:
        self.searches = 0
        self.fetches = 0
        self.researches = 0

    async def search(self, query, **kwargs):  # noqa: ANN001, ANN003, ARG002
        self.searches += 1
        return []

    async def fetch(self, url, **kwargs):  # noqa: ANN001, ANN003, ARG002
        self.fetches += 1
        from friday.web_surfer import FetchResult

        return FetchResult(url=url, title="", text="страница", text_length=8)

    async def research(self, query, **kwargs):  # noqa: ANN001, ANN003, ARG002
        self.researches += 1
        return {"query": query, "sources": []}


def _kernel(settings, storage, **overrides):
    tight = replace(settings, **overrides)
    storage.ensure_user("alice", source="test", external_id="alice")
    graph = KnowledgeGraph(storage)
    built = ExecutionKernel(AuthorizationService(storage), tight)
    built.bind_services(storage, graph, _Web(), IngestionPipeline(tight, storage, graph))
    return built


@pytest.fixture
def kernel(settings, storage):
    return _kernel(settings, storage, web_daily_quota=3)


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test", person_id="")


@pytest.mark.anyio
async def test_the_quota_stops_the_third_call_and_says_why(kernel) -> None:
    actor = _actor()
    for _ in range(3):
        answer = await kernel._web_search(actor=actor, query="погода")  # noqa: SLF001
        assert not answer.get("quota_exhausted"), "потолок сработал раньше времени"

    stopped = await kernel._web_search(actor=actor, query="погода")  # noqa: SLF001

    assert stopped.get("quota_exhausted") is True
    # Отказ обязан НАЗЫВАТЬ причину: молчаливую пустую выдачу модель пересказала
    # бы человеку как факт об интернете.
    assert "лимит" in stopped["error"] and "3" in stopped["error"]
    assert "не ходили" in stopped["note"]


@pytest.mark.anyio
async def test_every_road_outward_is_counted(kernel) -> None:
    """Ворота на одной дороге не охраняют ничего: считаются все три инструмента."""

    actor = _actor()
    assert not (await kernel._web_search(actor=actor, query="а")).get("quota_exhausted")  # noqa: SLF001
    assert not (await kernel._web_fetch(actor=actor, url="https://ok.example/1")).get(  # noqa: SLF001
        "quota_exhausted"
    )
    assert not (await kernel._web_research(actor=actor, query="в")).get("quota_exhausted")  # noqa: SLF001

    for call in (
        kernel._web_search(actor=actor, query="г"),  # noqa: SLF001
        kernel._web_fetch(actor=actor, url="https://ok.example/2"),  # noqa: SLF001
        kernel._web_research(actor=actor, query="д"),  # noqa: SLF001
    ):
        assert (await call).get("quota_exhausted") is True


@pytest.mark.anyio
async def test_the_refused_call_does_not_reach_the_network(kernel) -> None:
    """Отказ должен экономить ключ, а не сообщать о перерасходе задним числом."""

    actor = _actor()
    for _ in range(3):
        await kernel._web_search(actor=actor, query="а")  # noqa: SLF001
    before = kernel.web_surfer.searches

    await kernel._web_search(actor=actor, query="б")  # noqa: SLF001

    assert kernel.web_surfer.searches == before, "запрос ушёл наружу вопреки исчерпанной квоте"


@pytest.mark.anyio
async def test_the_neighbour_has_their_own_budget(kernel, storage) -> None:
    """Квота на ЧЕЛОВЕКА: один шумный участник не отрезает интернет остальным."""

    storage.ensure_user("bob", source="test", external_id="bob")
    mine = _actor()
    theirs = ActorContext(user_id="bob", preset_key="owner", source="test", person_id="")
    for _ in range(4):
        await kernel._web_search(actor=mine, query="а")  # noqa: SLF001

    answer = await kernel._web_search(actor=theirs, query="а")  # noqa: SLF001

    assert not answer.get("quota_exhausted")


@pytest.mark.anyio
async def test_a_zero_quota_means_no_limit(settings, storage) -> None:
    """Ноль — «не ограничивать»: иначе выключение потолка отрезало бы интернет."""

    kernel = _kernel(settings, storage, web_daily_quota=0)
    for _ in range(12):
        answer = await kernel._web_search(actor=_actor(), query="а")  # noqa: SLF001
        assert not answer.get("quota_exhausted")


def test_the_counter_is_atomic(storage) -> None:
    """Читать-прибавить-записать здесь нельзя: ходы разговора идут параллельно."""

    storage.ensure_user("alice", source="test", external_id="alice")
    values = [storage.bump_daily_counter("web", "alice", "2026-08-05") for _ in range(5)]

    assert values == [1, 2, 3, 4, 5]
    assert storage.daily_counter("web", "alice", "2026-08-05") == 5
    # Соседние сутки — отдельный счёт, вчерашний расход сегодня не мешает.
    assert storage.daily_counter("web", "alice", "2026-08-06") == 0


def test_old_counters_can_be_swept(storage) -> None:
    storage.ensure_user("alice", source="test", external_id="alice")
    for day in ("2026-07-01", "2026-08-04", "2026-08-05"):
        storage.bump_daily_counter("web", "alice", day)

    swept = storage.sweep_daily_counters("web", keep_days="2026-08-05")

    assert swept == 2
    assert storage.daily_counter("web", "alice", "2026-08-05") == 1
    assert storage.daily_counter("web", "alice", "2026-07-01") == 0


@pytest.mark.anyio
async def test_two_pages_of_one_site_are_not_taken_at_once(settings) -> None:
    """Пауза между обращениями к ОДНОМУ сайту, но не между разными."""

    import asyncio

    from friday.web_surfer import WebSurfer

    surfer = WebSurfer(replace(settings, web_host_pause_sec=0.2))
    loop = asyncio.get_running_loop()

    start = loop.time()
    await surfer._be_polite_to("ria.example")  # noqa: SLF001
    await surfer._be_polite_to("ria.example")  # noqa: SLF001
    same_host = loop.time() - start

    start = loop.time()
    await asyncio.gather(
        surfer._be_polite_to("a.example"),  # noqa: SLF001
        surfer._be_polite_to("b.example"),  # noqa: SLF001
        surfer._be_polite_to("c.example"),  # noqa: SLF001
    )
    other_hosts = loop.time() - start

    assert same_host >= 0.19, "второе обращение к тому же сайту пошло без паузы"
    assert other_hosts < 0.1, "пауза для одного сайта задержала соседние"


@pytest.mark.anyio
async def test_no_pause_means_no_pause(settings) -> None:
    import asyncio

    from friday.web_surfer import WebSurfer

    surfer = WebSurfer(replace(settings, web_host_pause_sec=0.0))
    loop = asyncio.get_running_loop()
    start = loop.time()
    await surfer._be_polite_to("ria.example")  # noqa: SLF001
    await surfer._be_polite_to("ria.example")  # noqa: SLF001

    assert loop.time() - start < 0.05
