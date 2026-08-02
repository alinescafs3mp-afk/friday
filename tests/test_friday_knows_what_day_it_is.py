"""Пятница обязана знать текущий год, дату и время — во ВСЕХ местах, где решает.

Требование владельца 2026-08-02: «Пятница должна прекрасно понимать текущий год,
дату и время».

Дата подставлялась только в основной системный промпт. Два других места, где
модель принимает решения о фактах, жили в году своего обучения:

- Арбитр намерения. Замерено на недельном прогоне: на «какие новые дроны
  применяются?» он составил запрос «new drones used in Ukraine war 2024» — и
  человек получил позапрошлогоднюю сводку под видом свежей.
- Судья обоснованности. Ответ «официальный курс ЦБ на 1 августа 2026» без даты
  выглядит для него выдумкой о будущем, и он бракует правильное.

Тест держит все три места сразу: пропажа даты из любого — регрессия.
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime

from friday.agent_runtime import AgentRuntime


def _runtime(settings) -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.settings = settings
    return runtime


def test_the_date_line_names_a_real_moment(settings) -> None:
    line = _runtime(settings)._today_line()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})", line)
    assert match, f"в строке нет даты и часа: {line!r}"
    stamp = datetime(*(int(part) for part in match.groups()))  # type: ignore[arg-type]
    # Не «похоже на дату», а ИМЕННО сегодняшняя: год из обучения модели тоже
    # похож на дату.
    assert abs((stamp - datetime.now()).total_seconds()) < 36 * 3600, (
        f"названный момент не сегодняшний: {stamp}"
    )


def test_the_weekday_matches_the_date(settings) -> None:
    """День недели считается, а не берётся откуда-то ещё."""
    line = _runtime(settings)._today_line()
    names = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", line)
    assert match
    expected = names[datetime(*(int(part) for part in match.groups())).weekday()]  # type: ignore[arg-type]
    assert expected in line, f"день недели не сходится с датой: ждали {expected}, строка {line!r}"


def test_the_main_prompt_carries_the_date() -> None:
    assert "_today_line()" in inspect.getsource(AgentRuntime._build_initial_messages)


def test_the_intent_arbiter_carries_the_date() -> None:
    """Мутация: убрать строку из промпта арбитра — тест краснеет."""
    source = inspect.getsource(AgentRuntime._web_query_by_arbiter)
    assert "_today_line()" in source, "арбитр снова не знает, какой сейчас год"
    assert "ТЕКУЩИЙ" in source, "не сказано брать текущий год, а не запомненный"


def test_the_grounding_judge_carries_the_date() -> None:
    source = inspect.getsource(AgentRuntime._verify_response)
    assert "_today_line()" in source, "судья снова живёт в году своего обучения"


def test_a_broken_timezone_name_does_not_break_the_turn(settings) -> None:
    """Кривой пояс в настройках не должен ронять ход — дата всё равно нужна."""
    from dataclasses import replace

    line = _runtime(replace(settings, local_timezone="Нет/Такого"))._today_line()
    assert re.search(r"\d{4}-\d{2}-\d{2}", line), line
