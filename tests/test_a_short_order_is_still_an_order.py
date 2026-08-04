"""Короткое поручение остаётся поручением, а не превращается в переспрос.

Найдено разведкой по коду и подтверждено замером 2026-08-03.

`terse_request` ставился по одной ДЛИНЕ: не болтовня, не веб, ≤2 слов, ≤24
знаков. «Собери отчёт» — это два слова и тринадцать знаков, то есть система
получала указание «переспроси, что именно нужно — найти, напомнить, посчитать,
оформить». Человек, который печатает коротко или диктует голосом, систематически
получал беседу вместо дела: на живой переписке 99 коротких реплик из 817, и 89
из них закончились уточняющим вопросом.

Рядом лежала вторая половина той же беды: у арбитра намерения не было вида
«действие», а вид «другое» описан в промпте как «разговор, просьба сделать
что-то в системе» — беседа и поручение склеены в одну категорию, которая не
запускает ничего. Замерено: `make_file` вызван 66 раз, из них решением модели
12 (18%); остальное сделали принудительные пути, проложенные заранее под
конкретные виды. Класс поручений, у которого своего пути нет, не исполнялся.

Снимается переспрос по ПОНИМАНИЮ, а не по списку глаголов: вердикт арбитра к
этому месту уже готов. «Ромашка» остаётся переспросом, «озвучь это» — нет.

Замер до/после на 21 просьбе и 9 контрольных ререликах:
    было  16/17 действий, 6/7 тишины
    стало 20/21 действий, 8/9 тишины   (все четыре коротких поручения ожили,
                                        одинокие слова остались переспросом)
"""

from __future__ import annotations

import inspect

import pytest

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel


def test_the_arbiter_knows_the_action_kind() -> None:
    """Вида, которого нет в промпте, модель не вернёт."""
    source = inspect.getsource(AgentRuntime._web_query_by_arbiter)
    assert "|действие|" in source, "вид «действие» пропал из перечня"
    assert "просят СДЕЛАТЬ что-то в системе" in source, "вид не объяснён модели"


def test_an_order_clears_the_follow_up_question() -> None:
    """Мутация: убрать снятие `terse_request` — короткое поручение снова переспрос."""
    source = inspect.getsource(AgentRuntime._prepare_context)
    at = source.index("context.outward_verdict = await arbiter")
    tail = source[at:]
    assert "context.terse_request = False" in tail, "переспрос больше не снимается"
    at_clear = tail.index("context.terse_request = False")
    guard = tail[max(0, at_clear - 600) : at_clear]
    # Правило с тех пор расширено: переспрос снимается для ЛЮБОГО понятого вида,
    # а не только для трёх перечисленных. Владелец 2026-08-03: «некоторые будут
    # её использовать как тупой поисковик» — «курс доллара» и «цена 5090» тоже
    # получали встречный вопрос. Поручение по-прежнему покрыто: «действие» — это
    # понятый вид, а не «другое».
    assert 'startswith("друг")' in guard, "переспрос снова шире, чем «не понял»"


def test_the_clearing_happens_after_the_verdict_not_before() -> None:
    """Порядок здесь и есть смысл: до вердикта понимания ещё нет.

    `terse_request` ставится по длине выше по методу; снять его можно только
    после того, как арбитр сказал, поручение это или одинокое слово.
    """
    source = inspect.getsource(AgentRuntime._prepare_context)
    assert source.index("context.terse_request = (") < source.index(
        "context.terse_request = False"
    ), "снятие переспроса уехало выше вердикта — понимания там ещё нет"


def test_a_lonely_word_still_gets_a_question() -> None:
    """Обратная сторона: одинокое слово-тема переспросом остаётся.

    Замерено на живой переписке: на слово из пяти букв приходило десять
    документов и ответ на килобайт, причём счёт совпадения у такой реплики ВЫШЕ,
    чем у настоящего вопроса (0.83 против 0.26). Здесь переспросить — правильно,
    а действие было бы гаданием.
    """
    source = inspect.getsource(AgentRuntime._prepare_context)
    at = source.index("context.terse_request = (")
    condition = source[at : source.index("\n        )", at)]
    # Правило по длине никуда не делось — снимается только поверх него.
    assert "<= 2" in condition and "<= 24" in condition


@pytest.mark.parametrize(
    "tool",
    ["remind", "memory_save", "entity_create", "speak", "make_file", "collect_files"],
)
def test_the_action_kind_offers_the_tools_that_change_things(tool: str) -> None:
    """Поручению нужны инструменты, меняющие мир, — а они простаивали.

    Замерено: из десяти мутирующих инструментов шесть не срабатывали НИ РАЗУ за
    всё время жизни системы.
    """
    assert tool in ExecutionKernel._RELEVANT_TOOLS["действие"]


def test_the_action_kind_does_not_offer_the_web() -> None:
    """«Напомни завтра» — не повод идти в интернет.

    Лишний веб-вызов стоит человеку секунд и уводит ответ от его же архива.
    """
    assert not {"web_search", "web_research", "web_fetch"} & ExecutionKernel._RELEVANT_TOOLS[
        "действие"
    ]


def test_every_named_tool_exists() -> None:
    """Опечатка в списке молча лишила бы инструмент подробного описания."""
    known = set(ExecutionKernel._RELEVANT_TOOLS["архив"]) | {
        "remind",
        "memory_save",
        "entity_create",
        "entity_link",
        "relation_end",
        "speak",
        "make_file",
        "collect_files",
        "mission_propose",
    }
    assert ExecutionKernel._RELEVANT_TOOLS["действие"] <= known
