"""Инструмент, который кладёт страницы в архив, — не наблюдатель.

`web_research` объявлял себя `observe`, а внутри звал `_capture_web_sources`:
найденные страницы ложатся в Raw Object и во входящие человека. Тот же разрыв был
у `resolve_duplicates` — «предложить дубликаты» наполняет очередь слияний строками,
которые человеку потом разбирать.

Класс риска — это обещание «после вызова в архиве ничего не изменится», и обещание
было ложным. Цена лжи не в ярлыке, а в двух механизмах, которые ярлык включает:

1. Запись «вызов начат» ДО эффекта. Обрыв посреди работы (отмена задачи, остановка
   процесса) оставлял страницы в базе и НОЛЬ записей о вызове — со стороны это
   выглядит как «инструмент не звали».

2. Честный исход по таймауту. Наблюдателю говорят «не вышло» — читать нечего
   дважды. Пишущему так говорить нельзя: эффект мог случиться ровно в момент, когда
   его перестали ждать, и «не вышло» — это приглашение повторить. Для `web_research`
   это не редкий случай, а штатный: он ходит в сеть, где таймаут — обычное дело.

Подтверждения человеком перенос НЕ добавил и не должен был: заявка требуется только
для `high`, и поиск, спрашивающий разрешения, никому не нужен. Проверяется это здесь
же — иначе «починка» тихо превратила бы каждый поиск в вопрос.
"""

from __future__ import annotations

import asyncio

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.web_surfer import WebSurfer

WRITES_TO_THE_ARCHIVE = ["web_research", "resolve_duplicates"]


def _kernel(settings, storage):
    auth = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(auth, settings)
    kernel.bind_services(storage, graph, WebSurfer(settings), IngestionPipeline(settings, storage, graph))
    return kernel, auth


@pytest.mark.parametrize("tool_name", WRITES_TO_THE_ARCHIVE)
def test_a_tool_that_leaves_rows_behind_is_not_declared_a_reader(settings, storage, tool_name):
    """Ярлык сверяется с тем, что инструмент делает, а не с тем, как называется."""
    kernel, _auth = _kernel(settings, storage)
    spec = kernel._tools[tool_name]  # noqa: SLF001
    assert spec.risk == "mutate", (
        f"{tool_name} объявлен {spec.risk!r}, а он оставляет записи в архиве: "
        "у обрыва не будет следа, а таймаут скажет «не вышло» и позовёт повторить"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", WRITES_TO_THE_ARCHIVE)
async def test_an_interrupted_search_says_the_outcome_is_unknown(settings, storage, tool_name):
    """Оборванный вызов не выдаёт себя за «ничего не вышло».

    Мутация: вернуть `changes_data = tool.risk == "high"` в `execute` — оба
    инструмента снова получат отказ без слова «неизвестно», и тест краснеет.
    """
    storage.ensure_user("alice", preset_key="owner")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    async def _never_returns(**_kwargs):
        await asyncio.sleep(60)

    kernel._tools[tool_name].handler = _never_returns  # noqa: SLF001
    import friday.execution_kernel as kernel_module

    original = kernel_module.asyncio.timeout

    def _instant_timeout(_seconds):
        return original(0.05)

    kernel_module.asyncio.timeout = _instant_timeout
    try:
        result = await kernel.execute(tool_name, {"query": "погода в Москве"}, actor=actor)
    finally:
        kernel_module.asyncio.timeout = original

    assert result.success is False
    assert "НЕИЗВЕСТНО" in (result.error or ""), (
        f"{tool_name} после обрыва сообщает {result.error!r} — это читается как "
        "«эффекта нет», а страницы уже могли лечь во входящие"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", WRITES_TO_THE_ARCHIVE)
async def test_the_start_of_the_call_is_recorded_before_the_effect(settings, storage, tool_name):
    """След вызова появляется до эффекта, а не после успешного возврата.

    Обработчик подменён на такой, который читает журнал ИЗНУТРИ вызова: если запись
    о начале делается после возврата, изнутри её видно не будет.
    """
    storage.ensure_user("alice", preset_key="owner")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    seen_from_inside: list[dict] = []

    async def _looks_at_the_journal(**_kwargs):
        seen_from_inside.extend(
            row
            for row in storage.list_audit_log("alice", limit=50)
            if row["target_id"] == tool_name
        )
        return {"ok": True}

    kernel._tools[tool_name].handler = _looks_at_the_journal  # noqa: SLF001
    result = await kernel.execute(tool_name, {"query": "погода в Москве"}, actor=actor)

    assert result.success is True
    assert seen_from_inside, (
        f"во время работы {tool_name} в журнале нет ни одной записи о вызове — "
        "обрыв на середине не оставит следа вовсе"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", WRITES_TO_THE_ARCHIVE)
async def test_a_search_still_does_not_ask_permission(settings, storage, tool_name):
    """Перенос в `mutate` не превратил поиск в вопрос к человеку.

    Ошибка в эту сторону так же дорога, как и в обратную: заявка на каждый поиск
    сделала бы Пятницу неработоспособной, и «починку» пришлось бы откатывать целиком.
    """
    storage.ensure_user("alice", preset_key="owner")
    kernel, auth = _kernel(settings, storage)
    actor = auth.actor_for_user("alice", source="test")

    async def _harmless(**_kwargs):
        return {"ok": True}

    kernel._tools[tool_name].handler = _harmless  # noqa: SLF001
    result = await kernel.execute(tool_name, {"query": "погода в Москве"}, actor=actor)

    assert result.success is True
    assert "approval_id" not in (result.data or {}), (
        f"{tool_name} стал требовать подтверждения — заявка положена только `high`"
    )
