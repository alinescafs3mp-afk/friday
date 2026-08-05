"""Собранный файл переживает отказ модели.

Найдено ревью уязвимых участков 2026-08-04, измерение «поведение при отказах».

Архив собирается структурной предвыборкой ДО обращения к модели: человек просит
«собери документы за 26 и 28 число и скажи, что в них», система откладывает файл,
и только потом идёт к модели за словами. Дальше самый длинный за ход вызов
(полный контекст плюс инструменты) упирается в таймаут молчащей модели — и ветка
отказа возвращала всё, кроме собранного файла. Из шести возвратов агентского цикла
`file_clips` не нёс ровно этот один.

Почему это дороже обычного отказа: работа СДЕЛАНА, пересобрать её нельзя (следующий
такой же запрос пойдёт по архиву заново), а человек уже прочитал первую строку
ответа, где сказано, что файл готов.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime


class _DeadLLM:
    """Модель, которая принимает запрос и не отвечает — как при обрыве туннеля."""

    enabled = True
    total_budget_sec = 1.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ANN003, ARG002
        raise TimeoutError("read timeout")


def _runtime(settings, storage):
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.settings = settings
    runtime.storage = storage
    runtime.llm = _DeadLLM()
    return runtime


@pytest.mark.asyncio
async def test_the_archive_is_returned_even_when_the_model_is_silent(settings, storage) -> None:
    """Мутация: убрать `file_clips` из ветки отказа — файл снова пропадает."""
    made = {"kind": "document", "filename": "Документы.zip", "size_bytes": 1024}

    runtime = _runtime(settings, storage)
    context = AgentContext(conversation_id="c", user_id="alice", search_query="")
    context.kb_size = 10

    async def _collected(
        context, actor, messages, tools_used, tool_evidence, file_clips, tools, *, message=""
    ):  # noqa: ANN001, ANN003, ARG001
        """Предвыборка успела собрать файл до того, как модель замолчала."""
        file_clips.append(made)

    runtime._prefetch_the_archive_if_asked = _collected  # type: ignore[attr-defined]

    async def _nothing(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        return False

    for name in (
        "_prefetch_the_web_if_asked",
        "_prefetch_person_activity",
        "_prefetch_the_timeline_if_asked",
        "_prefetch_archive_numbers",
        "_prefetch_a_reminder_if_asked",
    ):
        setattr(runtime, name, _nothing)

    # Прямой прогон цикла: подменять весь ход незачем, дефект жил в одном возврате.
    response = await AgentRuntime._agentic_loop(  # noqa: SLF001
        runtime, context, "собери документы за 26 и 28 число", None, [], None
    )

    assert response["llm_failed"] is True
    assert response.get("file_clips") == [made], "собранный файл потерян на отказе модели"


def test_every_exit_of_the_loop_carries_the_files() -> None:
    """Все выходы цикла несут собранное — а не пять из шести.

    Проверяется именно ПОЛНОТА: дефект был не в логике, а в одном забытом ключе,
    и такой промах виден только сравнением выходов между собой.
    """
    import ast
    import inspect
    import textwrap

    # Разбор синтаксиса, а не регулярка: первая редакция считала выходы поиском по
    # тексту, ловила один вместо шести и краснела на верном коде.
    tree = ast.parse(textwrap.dedent(inspect.getsource(AgentRuntime._agentic_loop)))  # noqa: SLF001
    exits = [
        node for node in ast.walk(tree) if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    ]

    assert len(exits) >= 4, f"выходов найдено {len(exits)} — проверь, не переписан ли цикл"
    without = [
        node.lineno
        for node in exits
        if "file_clips" not in {key.value for key in node.value.keys if isinstance(key, ast.Constant)}
    ]
    assert not without, f"выходы цикла в строках {without} теряют собранный файл"
