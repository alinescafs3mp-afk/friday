"""Период, который модель не сумела записать, — это отказ, а не «искать по всему».

Границы приходят строкой ОТ МОДЕЛИ и уходили прямо в SQL как операнды сравнения
строк. Проверено на трёх документах за март 2023: окно «01.01.2025..31.01.2025»
возвращало все три, потому что посимвольно `'2023-03-10' >= '01.01.2025'` истинно.
То есть фильтр молча снимался, а мартовские документы выдавались как январские —
и человек об этом не узнавал.

Форма дд.мм.гггг здесь не экзотика: в архиве владельца так записаны 2537 значений
дат из 3180, и модель перепишет её из документа. Зеркальный отказ той же природы:
«2023-03» давало ноль там, где документов три, и модель по описанию инструмента
обязана была сказать «в архиве есть, но не в этом периоде» — утверждение ложное.

HTTP-маршруты эту форму проверяют шаблоном, и на это есть отдельный тест. Путь
инструмента — то есть Telegram, главный вход владельца, — проверки не имел.
"""

from __future__ import annotations

import pytest

from jericho.execution_kernel import ExecutionKernel, _window_bound
from jericho.permissions import ActorContext
from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _dated(storage, user_id: str, title: str, document_date: str) -> None:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=title,
        raw_content="поверка весов на складе",
        content_type="text/plain",
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id=user_id,
            raw_object_id=raw.id,
            content="поверка весов на складе",
            title=title,
            metadata_json={"document_date": document_date},
        )
    )


def test_a_russian_date_is_understood_not_ignored():
    """Именно она снимала фильтр: сравнение строк считало её меньше любой ISO-даты."""
    assert _window_bound("01.01.2025", edge="since") == ("2025-01-01", None)
    assert _window_bound("31.01.2025", edge="until") == ("2025-01-31", None)


def test_a_partial_date_grows_to_its_own_edge():
    """«с 2023-03 по 2023-03» — это весь март, а не нулевой день.

    Иначе месячная точность, которую модель выбирает чаще всего, даёт пустоту при
    полном архиве — а инструмент велит ей сказать «в этот период ничего нет».
    """
    assert _window_bound("2023-03", edge="since") == ("2023-03-01", None)
    assert _window_bound("2023-03", edge="until") == ("2023-03-31", None)
    assert _window_bound("2023", edge="since") == ("2023-01-01", None)
    assert _window_bound("2023", edge="until") == ("2023-12-31", None)
    # Февраль високосного года считается календарём, а не «тридцатым числом».
    assert _window_bound("2024-02", edge="until") == ("2024-02-29", None)


def test_what_cannot_be_understood_is_named_not_dropped():
    assert _window_bound("позавчера", edge="since") == (None, "позавчера")
    assert _window_bound("2023-13-40", edge="since") == (None, "2023-13-40")
    # Пустая граница — это законное «без границы», а не ошибка.
    assert _window_bound("", edge="since") == (None, None)
    assert _window_bound(None, edge="until") == (None, None)


@pytest.mark.asyncio
async def test_the_tool_refuses_instead_of_searching_the_whole_archive(settings, storage):
    """Сквозная проверка на том самом сценарии, что давал ложный ответ."""
    storage.ensure_user("alice")
    for day in ("10", "11", "12"):
        _dated(storage, "alice", f"акт 2023-03-{day}", f"2023-03-{day}")
    kernel = ExecutionKernel(settings=settings)
    kernel.storage = storage
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    understood = await kernel._memory_search(  # noqa: SLF001
        actor=actor, query="поверка весов", since="01.01.2025", until="31.01.2025"
    )
    assert understood["count"] == 0, "документы марта 2023 выданы как январские 2025 — фильтр снялся молча"

    whole_march = await kernel._memory_search(  # noqa: SLF001
        actor=actor, query="поверка весов", since="2023-03", until="2023-03"
    )
    assert whole_march["count"] == 3, "месячная точность дала пустоту при трёх документах"

    refused = await kernel._memory_search(  # noqa: SLF001
        actor=actor, query="поверка весов", since="позавчера"
    )
    assert refused["count"] == 0
    assert refused["empty_because"] == "date_window_unparsed"
    # Отказ обязан назвать причину: иначе модель повторит ту же запись.
    assert "позавчера" in refused["detail"]
