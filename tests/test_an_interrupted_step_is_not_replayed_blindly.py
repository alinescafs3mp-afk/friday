"""Оборвавшийся шаг миссии не переигрывается вслепую.

Найдено ревью уязвимых участков 2026-08-04. Спека v3 §5 требует: шаг с побочным
эффектом, оборвавшийся на середине, — это НЕИЗВЕСТНЫЙ исход, а не повод повторить.
Аппарат для этого написан: признак `side_effect`, перевод в `uncertain`, сверка
вместо повтора.

В бою он был НЕДОСТИЖИМ. Признак ставился только для инструментов с проверкой
постусловия, а их ровно два — и шагу миссии их не выдают вовсе (`GATHER_TOOLS`
их не содержит). То есть `side_effect` не ставился НИКОГДА, и любой оборвавшийся
шаг возвращался в очередь и выполнялся заново.

При этом писать шаг может: `web_research` кладёт страницы в Raw Object и во
входящие, хотя объявлен наблюдением. Брать признак из класса риска нельзя по той
же причине — он там неверен.

Поэтому список ПЕРЕВЁРНУТ: перечислены заведомо читающие инструменты, всё
остальное считается способным оставить след. Ошибка в списке читающих стоит лишней
осторожности (шаг уйдёт на сверку вместо повтора), ошибка в обратном списке стоила
бы второго эффекта.
"""

from __future__ import annotations

import inspect

from friday.executive.service import _READ_ONLY_STEP_TOOLS, GATHER_TOOLS, ExecutiveService


def test_the_marker_no_longer_depends_on_postconditions() -> None:
    """Мутация: вернуть проверку по `POSTCONDITIONS` — тест краснеет.

    Проверяется само условие: если оно снова опирается на список инструментов с
    постусловием, признак перестаёт ставиться, потому что таких инструментов шагу
    не дают.
    """
    source = inspect.getsource(ExecutiveService._run_tool_loop)  # noqa: SLF001

    assert "call.name not in _READ_ONLY_STEP_TOOLS" in source, (
        "признак побочного эффекта снова берётся из списка, недостижимого для шага"
    )
    assert "if call.name in POSTCONDITIONS and task is not None" not in source


def test_no_postcondition_tool_is_even_offered_to_a_step() -> None:
    """Причина, по которой прежний признак не мог сработать, — здесь.

    Тест существует, чтобы это не выглядело догадкой: инструменты с проверкой
    постусловия шагу миссии не выдаются, и значит условие по ним было мёртвым.
    """
    from friday.execution_kernel import POSTCONDITIONS

    assert not (set(POSTCONDITIONS) & GATHER_TOOLS), (
        "инструмент с постусловием снова доступен шагу — условие могло бы работать"
    )


def test_the_writing_tools_are_not_called_read_only() -> None:
    """`web_research` пишет в архив и читающим не считается.

    Это ровно тот инструмент, ради которого признак и нужен: он кладёт страницы
    в Raw Object и во входящие, а объявлен наблюдением.
    """
    assert "web_research" not in _READ_ONLY_STEP_TOOLS
    assert "web_fetch" not in _READ_ONLY_STEP_TOOLS
    assert "web_search" not in _READ_ONLY_STEP_TOOLS


def test_the_read_only_list_stays_inside_what_a_step_may_call() -> None:
    """Список читающих не должен разъезжаться с тем, что шагу вообще выдают.

    Имя, оставшееся в нём после удаления инструмента, молча перестаёт защищать —
    и заметить это можно только сравнением списков.
    """
    assert _READ_ONLY_STEP_TOOLS <= GATHER_TOOLS


def test_an_interrupted_step_with_a_trace_becomes_uncertain() -> None:
    """Реакция на обрыв: неизвестность, а не повтор.

    Проверяется ветка, ради которой признак и существует, — иначе можно было бы
    поставить признак и не связать его ни с чем.
    """
    source = inspect.getsource(ExecutiveService._reclaim_stale_tasks)  # noqa: SLF001

    assert 'int(task.get("side_effect") or 0)' in source
    assert "uncertain" in source.casefold(), "оборвавшийся шаг с эффектом не уходит в неизвестность"
