"""Сводка о поведении системы должна доходить до Telegram, а не только до браузера.

Орган-компактор считает сводки с 3 августа, и прочитать их можно было двумя
дорогами: HTTP и вкладка панели. То есть наблюдение за системой существовало для
того, кто откроет браузер, — а владелец переписывается в чате.

Второе свойство здесь важнее удобства: сводка обязана не врать про свои же
пределы. Три дня — это три дня, а не «всё»; пять происшествий из семи — это пять
из семи. Молчаливый обрез на этом проекте находили четырежды за сутки.
"""

from __future__ import annotations

import pytest

from friday.telegram_bridge._views import ViewsMixin


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Bridge(ViewsMixin):
    """Только то, что нужно экрану сводок."""

    def __init__(self, answer: dict) -> None:
        self._answer = answer
        self.asked: list[str] = []
        self.sent: list[str] = []

    async def _backend_json(self, backend, method, path, payload, user, chat):  # noqa: ANN001, ARG002
        self.asked.append(path)
        return self._answer

    async def _send_message(self, telegram, chat_id, text, reply_markup=None, **kwargs):  # noqa: ANN001, ARG002
        self.sent.append(text)


def _day(date: str, turns: int | None, *incidents: tuple[str, str, int]) -> dict:
    counters = {} if turns is None else {"total_turns": turns}
    return {
        "local_date": date,
        "counters": counters,
        "incidents": [{"code": code, "text": text, "count": count} for code, text, count in incidents],
    }


async def _run(answer: dict) -> _Bridge:
    bridge = _Bridge(answer)
    await bridge._send_compacts(None, None, 42, "tg:1", {"id": 1})  # noqa: SLF001
    return bridge


@pytest.mark.anyio
async def test_the_command_asks_the_organ_and_shows_the_days() -> None:
    """Мутация: убрать ветку `/compact` — команда не дойдёт сюда вовсе."""

    bridge = await _run(
        {
            "items": [
                _day("2026-08-04", 747, ("model_silent", "Модель не ответила.", 3)),
                _day("2026-08-03", 120),
            ],
            "total": 2,
        }
    )

    assert bridge.asked == ["/api/compacts?limit=3"], "команда пошла не к органу сводок"
    said = bridge.sent[0]
    assert "2026-08-04" in said and "2026-08-03" in said
    assert "Модель не ответила." in said, "происшествие названо кодом, а не словами"
    assert "×3" in said, "трижды за сутки и однажды — разные новости"
    assert "Происшествий не отмечено." in said, "тихий день выглядит как отсутствующий"


@pytest.mark.anyio
async def test_a_page_of_days_is_labelled_as_a_page() -> None:
    """Три последних дня из тридцати — это не «сводки за всё время»."""

    bridge = await _run({"items": [_day("2026-08-04", 10)], "total": 30})

    assert "из 30" in bridge.sent[0], "страница выдаётся за весь список"


@pytest.mark.anyio
async def test_the_incident_list_says_when_it_is_cut() -> None:
    bridge = await _run(
        {
            "items": [
                _day(
                    "2026-08-04",
                    500,
                    *[(f"code_{index}", f"Происшествие {index}", 1) for index in range(7)],
                )
            ],
            "total": 1,
        }
    )

    said = bridge.sent[0]
    assert "Происшествие 0" in said and "Происшествие 4" in said
    assert "Происшествие 5" not in said, "показано больше, чем обещано"
    assert "и ещё 2" in said, "обрез списка происшествий не назван"


@pytest.mark.anyio
async def test_a_missing_counter_is_a_dash_and_not_a_zero() -> None:
    """«Признака не было» и «ходов не было» — разные ответы.

    Ноль здесь врал бы в ту сторону, где его примут за факт: сводку читает
    человек и делает по ней выводы. Ровно на этом обжёгся сам компактор —
    сутки на 747 ходов вернули `model_answers: 0`.
    """

    bridge = await _run({"items": [_day("2026-08-04", None)], "total": 1})

    said = bridge.sent[0]
    assert "ходов: —" in said
    assert "ходов: 0" not in said


@pytest.mark.anyio
async def test_an_empty_history_explains_itself() -> None:
    bridge = await _run({"items": [], "total": 0})

    assert "Сводок пока нет" in bridge.sent[0]
    assert "раз в сутки" in bridge.sent[0], "человеку не сказано, когда они появятся"


def test_the_command_is_declared_to_telegram() -> None:
    """Необъявленная команда не появится в меню бота — её никто не найдёт."""

    from friday.telegram_bridge import BOT_COMMANDS

    declared = {name for name, _ in BOT_COMMANDS}
    assert "compact" in declared


def test_rebuilding_a_day_asks_for_its_own_right() -> None:
    """Читать и ПЕРЕСОБИРАТЬ — разные права: у сборки другая цена.

    `compact.read` роздан пресету `user`, участников одиннадцать. Сборка
    перечитывает все ходы суток и пишет в базу — проект с самого начала объявлял
    для неё отдельное право, и оно не было заведено.
    """

    import inspect

    from friday.organs.compactor import RUN_CAPABILITY, CompactorOrgan

    assert RUN_CAPABILITY.security_id == "compact.run"
    assert RUN_CAPABILITY.default_presets == ("admin",)
    assert RUN_CAPABILITY in CompactorOrgan().capabilities(), "право объявлено, но не зарегистрировано"

    router_source = inspect.getsource(CompactorOrgan.router)
    build = router_source.split("async def run_compact", 1)
    assert len(build) == 2, "маршрут пересборки исчез — тест смотрит не туда"
    assert '_require(request, "compact.run")' in build[1], "сборку по-прежнему пускает право чтения"
