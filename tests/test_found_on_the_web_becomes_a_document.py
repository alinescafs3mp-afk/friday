"""Найденное в интернете попадает в общий конвейер, а не живёт один ход.

Требование владельца 2026-08-01: результаты поиска Пятницы должны считаться
полноправным участником конвейера — связываться с людьми, тегами и сущностями,
обрабатываться как документы.

До этого страница показывалась модели и забывалась: на завтрашний вопрос про то
же самое всё искалось заново, а в архиве не оставалось ни строки о том, что
Пятница вообще куда-то ходила.

Путь намеренно тот же, что у `POST /api/ingest/url`: Raw Object плюс Inbox, а не
запись в знания молча. Иначе каждый гуглинг дописывал бы в личный архив
содержимое чужих сайтов без ведома человека.
"""

from __future__ import annotations

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService


class _Surfer:
    """Веб-слой, который «нашёл» две страницы: одну содержательную, одну пустую."""

    def __init__(self, sources: list[dict] | None = None) -> None:
        self.sources = (
            sources
            if sources is not None
            else [
                {
                    "url": "https://cbr.ru/hd_base/KeyRate/",
                    "title": "Ключевая ставка Банка России",
                    "text": "Ключевая ставка Банка России составляет 14,00% годовых на 31.07.2026. " * 6,
                },
                {"url": "https://example.org/empty", "title": "Пусто", "text": ""},
            ]
        )
        self.asked: list[str] = []

    async def research(self, query: str, *, max_sources: int = 3) -> dict:
        self.asked.append(query)
        sources: list[dict] = []
        for raw in self.sources:
            item = dict(raw)
            text = str(item.get("text") or "")
            item.update(
                {
                    "text_length": len(text),
                    "status_code": 200,
                    "error": "",
                    "truncated": False,
                }
            )
            sources.append(item)
        return {
            "query": query,
            "sources": sources,
            "summary": "ok",
            # The fake already represents the provider's selected work set;
            # it did not silently drop the unused caller capacity.
            "requested_sources": len(sources),
            "completed_sources": len(sources),
            "timed_out_sources": 0,
            "failed_sources": 0,
            "search_timed_out": False,
        }


def _kernel(settings, storage, surfer) -> ExecutionKernel:
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(AuthorizationService(storage), settings)
    kernel.bind_services(storage, graph, surfer, IngestionPipeline(settings, storage, graph))
    return kernel


@pytest.mark.anyio
async def test_a_found_page_lands_in_the_inbox_as_a_document(settings, storage):
    """Мутация: убрать вызов `_capture_web_sources` — тест краснеет."""
    storage.ensure_user("alice", preset_key="admin")
    surfer = _Surfer()
    kernel = _kernel(settings, storage, surfer)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute("web_research", {"query": "ключевая ставка"}, actor=actor)

    assert result.success, result.error
    captured = result.data.get("captured") or []
    assert len(captured) == 1, f"в конвейер ушло {len(captured)} страниц вместо одной"
    assert captured[0]["url"] == "https://cbr.ru/hd_base/KeyRate/"
    assert captured[0]["raw_object_id"], "страница не стала Raw Object"

    inbox = storage.list_inbox("alice")
    assert inbox, "найденная страница не дошла до Inbox"


@pytest.mark.anyio
async def test_the_page_keeps_where_it_came_from(settings, storage):
    """Провенанс: адрес страницы и запрос, ради которого её нашли."""
    storage.ensure_user("alice", preset_key="admin")
    kernel = _kernel(settings, storage, _Surfer())
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    await kernel.execute("web_research", {"query": "ключевая ставка ЦБ"}, actor=actor)

    raw = storage.execute(
        "SELECT source, source_ref, metadata_json FROM raw_objects "
        "WHERE user_id='alice' ORDER BY created_at DESC"
    ).fetchone()
    assert raw["source"] == "web"
    # Ключ несёт адрес И отпечаток содержимого: страница живая, и адрес в
    # одиночку конфликтовал сам с собой при втором чтении. Адрес по-прежнему
    # читается — и он же лежит чистым в метаданных.
    assert raw["source_ref"].startswith("https://cbr.ru/hd_base/KeyRate/#")

    # По метаданным должно быть видно не только откуда страница, но и ЗАЧЕМ её
    # взяли: без запроса непонятно, почему чужой сайт лежит в личном архиве.
    metadata = str(raw["metadata_json"] or "")
    assert "web_research" in metadata, metadata
    assert "ключевая ставка ЦБ" in metadata, metadata
    assert "cbr.ru" in metadata, metadata


@pytest.mark.anyio
async def test_an_empty_page_is_not_work_for_a_human(settings, storage):
    """Страница без текста в Inbox — работа на ровном месте."""
    storage.ensure_user("alice", preset_key="admin")
    surfer = _Surfer([{"url": "https://example.org/x", "title": "Пусто", "text": "коротко"}])
    kernel = _kernel(settings, storage, surfer)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute("web_research", {"query": "что угодно"}, actor=actor)

    assert not (result.data.get("captured") or []), "пустая страница ушла в Inbox"
    assert storage.list_inbox("alice") == []


@pytest.mark.anyio
async def test_searching_is_allowed_without_the_right_to_write(settings, storage):
    """Искать и запоминать — разные разрешения, и это не формальность.

    Мутация: убрать проверку `knowledge.create` — тест краснеет, потому что
    такой человек начнёт молча наполнять архив.

    Пресета, где `web.research` есть, а `knowledge.create` нет, в наборе по
    умолчанию не существует (первая редакция теста брала `guest` — у него нет и
    самого поиска, поэтому до записи дело не доходило и мутация не ловилась).
    Здесь запрет задан явным override — ровно так его и задаст администратор.
    """
    storage.ensure_user("reader", preset_key="moderator")
    storage.set_permission_override("reader", "knowledge.create", "deny")
    kernel = _kernel(settings, storage, _Surfer())
    actor = ActorContext(user_id="reader", preset_key="moderator", source="test")

    result = await kernel.execute("web_research", {"query": "ключевая ставка"}, actor=actor)

    assert result.success, f"поиск запретили вместе с записью: {result.error}"
    assert result.data.get("sources"), "выдача не дошла до того, кому искать можно"
    assert not (result.data.get("captured") or []), "права на запись нет, а страница всё равно сохранена"
    assert storage.list_inbox("reader") == []


@pytest.mark.anyio
async def test_the_answer_still_carries_the_sources(settings, storage):
    """Сохранение — добавка, а не замена: модель по-прежнему видит выдачу."""
    storage.ensure_user("alice", preset_key="admin")
    kernel = _kernel(settings, storage, _Surfer())
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute("web_research", {"query": "ставка"}, actor=actor)

    assert result.data.get("sources"), "выдача пропала из результата инструмента"
    rendered = result.to_llm_message()
    assert "cbr.ru" in rendered


@pytest.mark.anyio
async def test_collision_topic_is_filtered_before_any_page_is_captured(settings, storage):
    """An airport collision may reach the provider, but never Raw/Inbox."""

    storage.ensure_user("alice", preset_key="admin")
    airport_text = (
        "Sheremetyevo SVO is the largest airport in Russia; this page contains terminal "
        "departures, airline desks, baggage rules, and a live flight schedule. "
    ) * 4
    relevant_text = (
        "Ukraine war reporting describes a confirmed dated military development and "
        "the public statements surrounding it. "
    ) * 4
    surfer = _Surfer(
        [
            {
                "url": "https://www.svo.aero/en/timetable/departures/",
                "title": "SVO airport departures",
                "text": airport_text,
            },
            {
                "url": "https://foreign.example.org/ukraine-update",
                "title": "Ukraine war update",
                "text": relevant_text,
            },
        ]
    )
    kernel = _kernel(settings, storage, surfer)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute(
        "web_research",
        {
            "query": "Russia Ukraine war latest news",
            "max_sources": 2,
            "topic_class": "russia_ukraine_war_news",
        },
        actor=actor,
    )

    assert result.success, result.error
    assert [item["url"] for item in result.data["sources"]] == ["https://foreign.example.org/ukraine-update"]
    assert result.data["completed_sources"] == 1
    assert result.data["failed_sources"] == 1
    assert result.data["topic_filtered_sources"] == 1
    assert [item["url"] for item in result.data.get("captured") or []] == [
        "https://foreign.example.org/ukraine-update"
    ]
    durable = "\n".join(
        str(row["source_ref"] or "") + "\n" + str(row["raw_content"] or "")
        for row in storage.execute(
            "SELECT source_ref, raw_content FROM raw_objects WHERE user_id='alice'"
        ).fetchall()
    )
    assert "svo.aero" not in durable.casefold()
    assert "airport" not in durable.casefold()
    assert "foreign.example.org/ukraine-update" in durable


@pytest.mark.anyio
async def test_collision_only_report_fails_without_capturing_an_airport(settings, storage):
    storage.ensure_user("alice", preset_key="admin")
    airport_text = (
        "SVO airport departure board for terminals, airlines, baggage and scheduled flights. "
    ) * 5
    surfer = _Surfer(
        [
            {
                "url": "https://www.svo.aero/en/timetable/departures/",
                "title": "SVO airport departures",
                "text": airport_text,
            }
        ]
    )
    kernel = _kernel(settings, storage, surfer)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    result = await kernel.execute(
        "web_research",
        {
            "query": "Russia Ukraine war latest news",
            "topic_class": "russia_ukraine_war_news",
        },
        actor=actor,
    )

    assert result.success, result.error
    assert result.data["search_failed"] is True
    assert result.data["error"] == "topic_mismatch"
    assert result.data["sources"] == []
    assert not (result.data.get("captured") or [])
    assert storage.list_inbox("alice") == []
    assert storage.execute("SELECT COUNT(*) AS c FROM raw_objects WHERE user_id='alice'").fetchone()["c"] == 0


@pytest.mark.anyio
async def test_unknown_research_topic_class_is_rejected_before_outbound_work(settings, storage):
    storage.ensure_user("alice", preset_key="admin")
    surfer = _Surfer()
    kernel = _kernel(settings, storage, surfer)
    actor = ActorContext(user_id="alice", preset_key="admin", source="test")

    report = await kernel._web_research(  # noqa: SLF001
        actor=actor,
        query="public news",
        topic_class="model_invented_topic",
    )

    assert report["outbound_attempted"] is False
    assert report["error"] == "invalid_topic_class"
    assert surfer.asked == []
    assert storage.list_inbox("alice") == []


@pytest.mark.anyio
async def test_a_page_that_changed_between_two_readings_is_saved_anyway(settings, storage):
    """Мутация: вернуть `source_ref=url` — тест краснеет.

    Замерено на живом экземпляре: пять срывов за сутки со стеком в журнале.
    Страница живая — курс ЦБ, прогноз погоды, лента новостей меняются между
    двумя чтениями, — и адрес, взятый ключом в одиночку, конфликтовал сам с
    собой: `source_ref is already bound to different text content`. Страница при
    этом терялась молча для человека и шумно для журнала.
    """
    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    kernel = _kernel(settings, storage, _Surfer())
    actor = auth.actor_for_user("alice", source="test")

    async def _capture(text: str) -> list[dict]:
        return await kernel._capture_web_sources(  # noqa: SLF001
            actor,
            "ключевая ставка",
            {
                "sources": [
                    {
                        "url": "https://cbr.ru/",
                        "title": "Ключевая ставка",
                        "text": text,
                        "text_length": len(text),
                        "status_code": 200,
                        "error": "",
                        "truncated": False,
                    }
                ],
                "requested_sources": 1,
                "completed_sources": 1,
                "timed_out_sources": 0,
                "failed_sources": 0,
                "search_timed_out": False,
            },
        )

    first = await _capture("Ключевая ставка составляет 14,00% годовых. " * 12)
    assert len(first) == 1, "первая версия страницы не сохранилась"

    second = await _capture("Ключевая ставка составляет 13,50% годовых. " * 12)
    assert len(second) == 1, "обновившаяся страница потеряна"

    # Неизменная страница по-прежнему не задваивается.
    third = await _capture("Ключевая ставка составляет 13,50% годовых. " * 12)
    assert len(third) <= 1
    rows = storage.execute(
        "SELECT COUNT(*) AS c FROM raw_objects WHERE source='web' AND deleted_at IS NULL"
    ).fetchone()["c"]
    assert rows == 2, f"ожидалось две версии страницы, в архиве {rows}"
