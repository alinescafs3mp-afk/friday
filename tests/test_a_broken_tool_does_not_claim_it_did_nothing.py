"""Тип исключения не доказывает, что эффекта не было.

Механизм честного исхода строился вокруг таймаута: он наступает ПОСЛЕ начала
работы, значит эффект мог случиться, и человеку говорится «НЕИЗВЕСТНО, успел ли».
Остальные исключения при этом считались обычным отказом — по ТИПУ: `TypeError` и
`ValueError` объявлялись разбором аргументов, прочие — сбоем.

Оба утверждения ложны, когда исключение прилетело из середины обработчика. Тот
мог записать в базу и упасть следующей строкой; «Invalid tool arguments» в этом
случае ложно вдвойне — оно называет причиной аргументы. Модель читает такой текст
как «не сделано» и предлагает повторить, а повтор делает эффект вторым.

Указано внешним разбором (Сол, 2026-08-04) и подтверждено: различает не тип
исключения, а РИСК инструмента. У наблюдающего эффекта нет вовсе, и там прежний
короткий отказ остаётся — если неизвестно всё, слово теряет смысл и его перестают
читать.

Слово «НЕИЗВЕСТНО» намеренно оставлено таймауту: там неизвестен сам исход, здесь
известен сбой и неизвестны его последствия. Это разные сообщения человеку.
"""

from __future__ import annotations

import pytest

from friday.permissions import ActorContext, AuthorizationService


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


@pytest.fixture
def kernel(settings, storage):
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.knowledge_graph import KnowledgeGraph
    from friday.web_surfer import WebSurfer

    storage.ensure_user("alice", preset_key="owner")
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    instance = ExecutionKernel(auth, settings)
    instance.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    return instance


def _break(kernel, tool_name: str, exception: Exception) -> None:
    """Сломать обработчик так, будто он упал ПОСЛЕ записи в базу."""
    tool = kernel._tools[tool_name]  # noqa: SLF001

    async def _fails(**_kwargs):
        raise exception

    tool.handler = _fails


@pytest.mark.asyncio
async def test_a_mutating_tool_says_the_work_had_started(kernel, storage):
    """Мутация: вернуть разделение по типу исключения — тест краснеет."""
    _break(kernel, "memory_save", ValueError("что-то не так на середине"))

    result = await kernel.execute("memory_save", {"content": "текст", "title": "т"}, actor=_actor())

    assert result.success is False
    assert "НАЧАВ" in result.error, f"сказано, что действие не начиналось: {result.error!r}"
    assert "Invalid tool arguments" not in result.error, "причиной названы аргументы"
    assert "прежде чем повторять" in result.error


@pytest.mark.asyncio
async def test_the_trail_says_the_failure_came_after_the_start(kernel, storage):
    """Оборванная пара «начал / нет конца» должна быть видна в журнале."""
    _break(kernel, "memory_save", RuntimeError("упал"))

    await kernel.execute("memory_save", {"content": "текст", "title": "т"}, actor=_actor())

    trail = " ".join(str(row) for row in storage.list_audit_log(limit=20))
    assert "started" in trail, "не записано, что работа началась"
    assert "failed_after_start" in trail, f"сбой после начала не отличим от отказа: {trail!r}"


@pytest.mark.asyncio
async def test_a_wrong_field_name_is_not_called_started_work(kernel, storage):
    """Ошибка в ИМЕНИ поля случается до первой строки обработчика.

    Найдено собственным тестом сразу после правки выше: она объявляла «работа
    НАЧАЛАСЬ» на любом `TypeError` у мутирующего инструмента, а модель ошибается
    именем поля регулярно. Предупреждение, приходящее не по делу, обесценивает
    те, что по делу, — а здесь ещё и подталкивает искать несделанное действие.

    Различает не тип исключения, а то, дошло ли дело до вызова: аргументы теперь
    сверяются с сигнатурой ДО записи «начал».

    Мутация: убрать сверку сигнатуры — тест краснеет.
    """
    result = await kernel.execute(
        "remind", {"текст": "позвонить", "when": "2027-03-01"}, actor=_actor()
    )

    assert result.success is False
    assert "НАЧАВ" not in result.error, f"неверное поле названо начатой работой: {result.error!r}"
    assert "Invalid tool arguments" in result.error
    trail = " ".join(str(row) for row in storage.list_audit_log(limit=10))
    assert "started" not in trail, "запись о начале появилась там, где вызова не было"


@pytest.mark.asyncio
async def test_an_observing_tool_keeps_the_short_refusal(kernel):
    """Ошибка в другую сторону: у чтения эффекта нет, и пугать им нечем.

    Если «возможно, что-то произошло» пишется на каждый неудавшийся поиск,
    предупреждение обесценивается и человек перестаёт его замечать.
    """
    _break(kernel, "memory_search", ValueError("плохой запрос"))

    result = await kernel.execute("memory_search", {"query": "х"}, actor=_actor())

    assert result.success is False
    assert "НАЧАВ" not in result.error
    assert "Invalid tool arguments" in result.error


@pytest.mark.asyncio
async def test_a_timeout_still_says_the_outcome_is_unknown(kernel):
    """Контроль: у таймаута своё слово, и правка его не забрала."""
    _break(kernel, "memory_save", TimeoutError())

    result = await kernel.execute("memory_save", {"content": "т", "title": "т"}, actor=_actor())

    assert "НЕИЗВЕСТНО" in result.error, f"таймаут потерял своё слово: {result.error!r}"


@pytest.mark.asyncio
async def test_a_reminder_is_written_whole_or_not_at_all(kernel, storage, monkeypatch):
    """Две записи одного напоминания — одна транзакция.

    Врозь они давали настоящее половинчатое состояние: событие в графе есть,
    времени у него нет, не напомнит никто, а человеку сказано «поставила».

    Мутация: убрать `with storage.transaction()` — тест краснеет.
    """
    _, graph, _, _ = kernel._require_services()  # noqa: SLF001
    before = len(storage.list_entities("alice", limit=200))

    def _fails(*args, **kwargs):
        raise ValueError("дата не разобрана")

    monkeypatch.setattr(graph, "set_event_time", _fails)

    result = await kernel.execute(
        "remind", {"what": "позвонить в банк", "when": "2027-03-01"}, actor=_actor()
    )

    assert result.success is False
    after = storage.list_entities("alice", limit=200)
    assert len(after) == before, f"осталось событие без времени: {[e['name'] for e in after]}"
