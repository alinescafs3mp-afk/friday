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

    from friday import agent_runtime

    source = inspect.getsource(agent_runtime)
    answer_branch = source[source.index('elif turn.kind == "answer":') :][:600]
    assert "_strip_tool_call_markup(turn.text)" in answer_branch, (
        "финальный ответ отдаётся человеку без очистки служебных маркеров"
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
