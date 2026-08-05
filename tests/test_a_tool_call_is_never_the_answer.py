"""Вызов инструмента, записанный как код, — не ответ человеку.

Найдено недельным прогоном 2026-08-02. На вопрос «Стоит ли брать 5090 под
локальные модели?» человек получил ответом ровно строку:

    memory_search.search(query="Стоит ли брать 5090 под локальные модели?")

Распознавались только JSON-конверт и `<tool_call>`; питоноподобная запись — ни то,
ни другое, и она уходила как готовый ответ. Это третья форма одной и той же
болезни: служебное содержимое доходит до человека вместо ответа.

Обратная сторона так же важна. Объяснение, В КОТОРОМ упомянут вызов, — законный
ответ, и терять его нельзя: ровно эту цену платил прежний детектор конвертов,
забраковывавший любой рассказ про JSON вместе с потраченными на него ходами.
"""

from __future__ import annotations

import pytest

from friday.agent_runtime.tool_protocol import classify_tool_turn, looks_like_a_code_style_call


@pytest.mark.parametrize(
    "text",
    [
        'memory_search.search(query="Стоит ли брать 5090 под локальные модели?")',
        'web_research(query="курс евро", max_sources=3)',
        'what_happened(period="вчера")',
    ],
)
def test_a_bare_code_style_call_is_not_an_answer(text: str) -> None:
    """Мутация: убрать проверку из `classify_tool_turn` — тест краснеет."""
    assert looks_like_a_code_style_call(text)
    assert classify_tool_turn(text).kind == "protocol_error", f"ушло человеку как ответ: {text}"


@pytest.mark.parametrize(
    "text",
    [
        'Чтобы найти это, я зову memory_search.search(query="поверка") и смотрю выдачу.',
        "Цена RTX 5090 — около 210 000 рублей.",
        "Функция print(x) выводит значение на экран.",
        "В конфиге пишут timeout(seconds=30), это стандартная запись.",
    ],
)
def test_a_real_answer_survives(text: str) -> None:
    """Упоминание вызова внутри объяснения — обычный ответ."""
    assert classify_tool_turn(text).kind == "answer", f"потеряли настоящий ответ: {text}"


def test_a_multiline_reply_is_not_a_call() -> None:
    """Рассказ в несколько строк — не одно выражение, чем бы он ни начинался."""
    text = 'memory_search.search(query="x")\n\nЯ нашла три документа, вот они.'
    assert classify_tool_turn(text).kind == "answer"


def test_empty_input_is_not_a_call() -> None:
    assert looks_like_a_code_style_call("") is False
    assert looks_like_a_code_style_call("   ") is False
