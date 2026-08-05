"""Вежливая вопросительная просьба о документе всё-таки даёт документ.

Замерено 2026-08-03, три прогона подряд без файла: «можешь оформить отчёт по
июльским документам файлом?» — арбитр говорит «файл» ТРИ РАЗА ИЗ ТРЁХ, то есть
намерение распознано верно, а файла нет.

Ломалось дальше по пути. Вежливая вопросительная форма провоцирует встречный
вопрос («что именно включить?»), а `_file_for_a_request_that_wanted_one`
отказывался собирать документ, если ответ модели оказался вопросом. Два
предохранителя, каждый разумный по отдельности, замыкались в петлю: форма
просьбы порождала вопрос, а вопрос отменял просьбу.

Отказ по встречному вопросу сохраняется там, где просьба о файле НЕ распознана —
он защищает от сборки документа на пустом месте. Снимается он только при явном
вердикте «файл», и содержимое при этом берётся из ОСНОВАНИЙ, а не из текста
ответа: если оснований нет, отказ всё равно случится, но по своей причине.

Второй случай того же замера — просьба о СУЖДЕНИИ уходила в поисковик:
«Как думаешь, стоит ли переходить на новую версию?» давала вердикт «интернет»
два раза из трёх и двадцать секунд ожидания ради выдачи, в которой ответа на
этот вопрос нет и быть не может.

Замер после обеих правок: действие 20/21, тишина 9/9 (было 16/17 и 6/7).
"""

from __future__ import annotations

import inspect

import pytest

from friday.agent_runtime import AgentRuntime


def test_a_clear_file_request_survives_a_counter_question() -> None:
    """Мутация: убрать проверку вердикта — вежливая просьба снова без файла."""
    source = inspect.getsource(AgentRuntime._file_for_a_request_that_wanted_one)
    at = source.index("_answer_is_a_question(answer)")
    condition = source[at - 200 : at + 120]
    assert "asked_plainly" in condition, "встречный вопрос снова отменяет ясную просьбу"
    assert 'startswith(\n            "файл"\n        )' in source or '"файл"' in source


def test_the_refusal_still_protects_an_unclear_request() -> None:
    """Обратная сторона: без вердикта «файл» отказ по встречному вопросу остаётся.

    Иначе любой уточняющий диалог заканчивался бы документом, собранным из
    вопроса, — а это выдумка в файле, который человек унесёт с собой.
    """
    source = inspect.getsource(AgentRuntime._file_for_a_request_that_wanted_one)
    assert "and not asked_plainly" in source, "защита от сборки на пустом месте снята целиком"


def test_no_grounds_still_means_no_file() -> None:
    """Снятие отказа не отменяет главного предохранителя.

    Замерено раньше: без оснований второй заход давал «15 420 записей», «500 ГБ»,
    «10 миллионов уникальных записей» — при 1533 документах в архиве.
    """
    source = inspect.getsource(AgentRuntime._file_for_a_request_that_wanted_one)
    assert "No content and no grounds" in source
    assert "return None" in source[source.index("No content and no grounds") :][:400]


@pytest.mark.parametrize("word", ["как думаешь", "стоит ли", "посоветуй", "твоё мнение", "что лучше выбрать"])
def test_the_arbiter_is_told_that_advice_is_not_a_search(word: str) -> None:
    """Просьба о суждении — не поиск факта: в сети нет ответа про ЭТОГО человека."""
    source = inspect.getsource(AgentRuntime._web_query_by_arbiter)
    assert word in source, f"«{word}» не названо арбитру как просьба о суждении"


def test_the_advice_branch_answers_from_its_own_head() -> None:
    """Вид для суждения — «знание»: отвечай сама и честно пометь, что это мнение."""
    source = inspect.getsource(AgentRuntime._web_query_by_arbiter)
    at = source.index("Сюда же — просьба о СУЖДЕНИИ")
    block = source[at : at + 700]
    assert "отвечай сама" in block
    assert "твоё мнение, а не найденный факт" in block
    # И объяснение ПОЧЕМУ, а не голый запрет: запрет без причины модель обходит.
    assert "нет ответа на вопрос, что лучше для ЭТОГО человека" in block.replace(
        '"\n                            "', ""
    )
