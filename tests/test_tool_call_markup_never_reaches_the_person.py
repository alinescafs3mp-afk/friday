"""Разметка вызова инструмента не должна доходить до человека.

Модель обязана просить инструмент отдельным полем протокола, но иногда пишет его в
ОТВЕТ как текст. Замерено на живом экземпляре: вопрос «сколько всего знаний в базе?
посчитай точно» вернул пользователю буквально

    <tool_call>
    {"name":"kg_stats"}
    </tool_call>

и больше ничего. Снаружи это неотличимо от поломки.

Исполнять такой «вызов» здесь намеренно НЕ делается: это означало бы принимать
команды из недоверенного текста тем же путём, каким приходят данные. Разметка
снимается; если под ней ничего не было, вызывающий скажет своё честное «не
удалось сформировать ответ» — сбой, названный сбоем, лучше разметки на экране.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime.llm import _strip_tool_call_markup


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('<tool_call>\n{"name":"kg_stats"}\n</tool_call>', ""),
        ('Вот ответ. <tool_call>{"name":"x"}</tool_call> И продолжение.', "Вот ответ.  И продолжение."),
        # Незакрытый блок — обрыв генерации на середине вызова; хвост тоже не текст.
        ('<tool_call>{"name":"x"}', ""),
        ("<TOOL_CALL>{}</TOOL_CALL>", ""),
        ("Обычный ответ без разметки", "Обычный ответ без разметки"),
        # Слово в прозе — не разметка, и трогать его нельзя.
        ("Я могу сделать tool_call, если нужно", "Я могу сделать tool_call, если нужно"),
    ],
)
def test_markup_is_removed_and_prose_is_kept(raw, expected):
    assert _strip_tool_call_markup(raw) == expected


def test_the_sanitiser_runs_where_the_answer_is_formed():
    """Мутация: убрать вызов из ветки «answer» — тест краснеет.

    Проверяется не помощник, а то, что его ЗОВУТ, и ИМЕННО ТАМ. Первая редакция
    ставила очистку в `_strip_thinking` — до разбора протокола — и ломала рабочий
    механизм: `tool_protocol` умеет распознать текстовый вызов и исполнить его, а
    вырезанный блок превращался в пустоту, пустота — в «нарушение протокола», и
    человек получал «не удалось безопасно завершить вызов инструмента» там, где
    раньше получал ответ.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._agentic_loop)  # noqa: SLF001
    start = source.index('elif turn.kind == "answer":')
    end = source.index('if turn.kind == "protocol_error" or not calls:', start)
    answer_branch = source[start:end]
    assert "clean_answer = _strip_tool_call_markup(turn.text)" in answer_branch, (
        "финальный ответ отдаётся человеку без очистки служебных маркеров"
    )
    assert "accepted_answer = context.deferred_web_file_body or clean_answer" in answer_branch
    assert '"content": accepted_answer' in answer_branch, (
        "очищенный ответ вычисляется, но человеку отдаётся исходная строка"
    )


def test_the_sanitiser_also_runs_after_the_final_synthesis():
    """Мутация: убрать очистку из ветки финального синтеза — тест краснеет.

    Второй выход к человеку, и он обошёлся без очистки. Замерено на живом
    экземпляре 2026-08-01: вопрос «какая погода завтра в Москве?» вернул в чат

        Похоже, Яндекс.Погода не отдала текст напрямую. Попробую другой источник.
        <tool_call> {"name": "web_fetch", "arguments": {...}} </tool_call>

    — то есть ровно то, что прошлый тест запрещает, но другим путём: синтез
    после инструментов возвращает свой текст сразу, минуя разобранную ветку.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._agentic_loop)  # noqa: SLF001
    start = source.index("final_turn = classify_tool_turn")
    end = source.index('LOGGER.warning("Final synthesis returned bare tool-call markup")', start)
    synthesis = source[start:end]
    assert "_strip_tool_call_markup(final_turn.text)" in synthesis, (
        "итог синтеза после инструментов уходит человеку без очистки"
    )
    assert "accepted_final = context.deferred_web_file_body or clean" in synthesis
    assert '"content": accepted_final' in synthesis, (
        "очистка вычисляется, но человеку отдаётся неочищенный текст"
    )


def test_the_protocol_parser_still_sees_raw_markup():
    """А до разбора протокола текст не трогается.

    Разметка в сыром ответе — законный способ вызвать инструмент для рантаймов без
    нативного tool-calling, и `classify_tool_turn` на это рассчитывает.
    """
    from friday.agent_runtime.llm import LLMRouter

    raw = '<tool_call>\n{"name":"kg_stats"}\n</tool_call>'
    assert LLMRouter._strip_thinking(raw) == raw, (  # noqa: SLF001
        "разметка снята слишком рано — разбор протокола её больше не увидит"
    )


def test_a_broken_protocol_falls_back_to_an_answer_without_tools():
    """Мутация: убрать `_answer_without_tools` из финала — тест краснеет.

    Замерено на боевой переписке: 22 ответа из 381 (5.8%) были отказами
    «не удалось обработать запрос» / «не удалось безопасно завершить вызов
    инструмента». К последнему шагу история полна сломанных вызовов и ремонтных
    указаний, и модель, глядя на них, повторяет ту же ошибку. Обычный ответ по
    уже собранному архиву лучше отказа.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._agentic_loop)  # noqa: SLF001
    assert "_answer_without_tools(" in source, "после сломанного протокола нет попытки ответить"
    tail = source[source.index("_answer_without_tools(") :]
    assert "_TOOL_PROTOCOL_FAILURE" in tail, (
        "отказ должен оставаться последним вариантом, а не исчезнуть совсем"
    )


def test_the_salvage_pass_offers_no_tools():
    """Инструменты на этой ступени не предлагаются: именно они и сломались."""
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime._answer_without_tools)  # noqa: SLF001
    assert "tool_enabled=False" in source
    assert "tools=[]" in source
