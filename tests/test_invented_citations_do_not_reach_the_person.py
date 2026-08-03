"""Ссылка, которую нельзя открыть, — не ссылка, а мусор в тексте.

Найдено недельным прогоном 2026-08-02. Отвечая по веб-выдаче, модель ставила
`[K_source]` — по образцу настоящих `[K1]`, но ни на что не указывающую:

    «По свежей сводке за 2 августа 2026 года [K_source]: …»

Настоящие метки номерные и разбираются `CITATION_MARKER_RE`; выдуманная под неё
не подпадала и потому доходила до человека как есть. Вред двойной: служебный
мусор в тексте и ложный вид обоснованности — выглядит как ссылка на источник,
а открыть нечего.
"""

from __future__ import annotations

import inspect

import pytest

from friday.agent_runtime import AgentRuntime, _strip_invented_citations


@pytest.mark.parametrize(
    "text",
    [
        "По свежей сводке [K_source]: цена выросла.",
        "Данные [K источник] подтверждают вывод.",
        "Итог [KB] такой.",
        "Смотри [K-src] дальше.",
    ],
)
def test_an_invented_marker_is_removed(text: str) -> None:
    """Мутация: убрать вызов из `chat` — тест на подключение краснеет."""
    cleaned = _strip_invented_citations(text)
    assert "[K" not in cleaned and "[k" not in cleaned, cleaned


def test_real_citations_are_untouched() -> None:
    text = "Смотри [K1] и [K2] — там всё есть."
    assert _strip_invented_citations(text) == text


def test_a_mixed_answer_keeps_only_the_real_ones() -> None:
    assert _strip_invented_citations("Смешанно: [K1] верно, [K_source] нет.") == (
        "Смешанно: [K1] верно, нет."
    )


def test_the_space_before_punctuation_goes_with_the_marker() -> None:
    """Иначе на месте метки остаётся висящий пробел перед точкой."""
    assert _strip_invented_citations("Вывод [K_source].") == "Вывод."


def test_text_without_markers_is_returned_as_is() -> None:
    text = "Обычный ответ без всяких меток."
    assert _strip_invented_citations(text) is text or _strip_invented_citations(text) == text


def test_the_cleanup_is_wired_into_the_turn() -> None:
    """Проверяется подключённое: чистка стоит в боевом ходе, а не рядом."""
    source = inspect.getsource(AgentRuntime.chat)
    # Именно факт вызова, а не его точная форма: список поданных источников
    # добавился вторым аргументом 2026-08-03, и тест, закреплявший число
    # аргументов, покраснел на правке, ничего при этом не защищая.
    assert "_strip_invented_citations(content" in source, "чистка не подключена к ответу"
    # Порядок важен: судья видит ответ как есть, человек — уже без мусора.
    assert source.index("_verify_response") < source.index("_strip_invented_citations(content")
