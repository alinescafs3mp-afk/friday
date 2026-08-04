"""Сводка не выдаёт «признака не было» за «случаев не было».

Найдено на ПЕРВОМ настоящем прогоне, 2026-08-04. Сутки владельца за 3 августа:
`total_turns: 747`, а `model_answers: 0`. То есть сводка утверждала, что за
семьсот сорок семь ходов модель не сказала ни слова.

Правда была другая: структурную метку хода начали писать вечером тех же суток, и
у ходов её просто нет. Ноль здесь не осторожная оценка — это ложь в ту сторону,
где её примут за факт, потому что сводку читает человек и делает по ней выводы о
том, что чинить.

Приём тот же, что с остатком реплики: величина трёхзначна. «Столько-то»,
«ноль» и «не измерялось» — три разных ответа, и третий нельзя записывать вторым.
Счётчики, живущие на метке, при её полном отсутствии не пишутся вовсе; рядом
едет `measured_turns`, потому что три случая из семисот сорока семи и три из трёх
— разные сутки, а по одному числу их не различить.

Счётчик `total_turns` и происшествия этим не затронуты: они считаются по самим
ходам и честны всегда.
"""

from __future__ import annotations

from friday.organs.compactor import counters_of_a_day

_BEHAVIOUR = ("structural_answers", "model_answers", "corrections_accepted", "refusals")


def _turn(**structural: object) -> dict[str, object]:
    return {"structural": dict(structural)} if structural else {}


def test_a_day_without_the_mark_reports_nothing_measured() -> None:
    """Мутация: вернуть нули вместо отсутствия — тест краснеет."""
    counters = counters_of_a_day([_turn() for _ in range(747)])

    assert counters["total_turns"] == 747, "число ходов считается по самим ходам и обязано быть верным"
    assert counters["measured_turns"] == 0
    for name in _BEHAVIOUR:
        assert name not in counters, f"{name} выдаёт «признака не было» за «случаев не было»"


def test_a_measured_day_still_counts() -> None:
    """Обратная сторона: правка, гасящая счётчики совсем, ломает весь смысл сводки."""
    rows = [
        _turn(model_spoke=True),
        _turn(model_spoke=True, correction_learned=True),
        _turn(answer_present=True, model_spoke=False),
        _turn(rule_refused=True),
    ]

    counters = counters_of_a_day(rows)

    assert counters["measured_turns"] == 4
    assert counters["model_answers"] == 2
    assert counters["corrections_accepted"] == 1
    assert counters["structural_answers"] == 1
    assert counters["refusals"] == 1


def test_a_partly_measured_day_says_by_how_much() -> None:
    """Три случая из семисот и три из трёх — разные сутки.

    Без `measured_turns` человек читает «модель ответила 3 раза» и заключает, что
    она молчала весь день, хотя измерены были три хода из ста.
    """
    rows = [_turn() for _ in range(97)] + [_turn(model_spoke=True) for _ in range(3)]

    counters = counters_of_a_day(rows)

    assert counters["total_turns"] == 100
    assert counters["measured_turns"] == 3
    assert counters["model_answers"] == 3


def test_an_ignored_order_is_counted_without_the_mark() -> None:
    """Поручение без вызова инструмента видно по самому ходу, а не по метке.

    Это половина доказательства того, что правка узкая: она гасит ровно те
    счётчики, которые без метки посчитать нечем, и не трогает остальные.
    """
    rows = [{"structural": {"verdict_kind": "действие"}, "tools_used": []} for _ in range(2)]

    counters = counters_of_a_day(rows)

    assert counters["ignored_orders"] == 2
    assert counters["measured_turns"] == 2


def test_the_panel_draws_a_dash_where_nothing_was_measured() -> None:
    """Прочерк рисуется на той же стороне, что и данные.

    Сводка может честно не прислать счётчик, а панель — привести его к нулю
    (`Number(v||0)`) и показать ровно то, чего мы избегали. Проверяется поэтому
    сама отрисовка.
    """
    from pathlib import Path

    source = Path("friday/admin_ui/static/app.js").read_text(encoding="utf-8")
    body = source[source.index("function compactBody") :][:2600]

    assert "Number(v||0)" not in body, "панель снова приводит непосчитанное к нулю"
    assert "'—'" in body, "прочерка нет — непосчитанное опять выглядит как ноль"
    assert "measured_turns" in body, "панель не говорит, по скольким ходам посчитано"
