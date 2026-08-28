"""Мост ждёт ровно столько, сколько ядру разрешено работать.

Дефект: два числа считались по РАЗНЫМ формулам от одной настройки, и никто их не
связал. На боевых умолчаниях (`llm_timeout_sec=240`, в `.env.local` не
переопределён) выходило так:

  * бюджет одного обращения к модели с повторами — 360 с;
  * бюджет хода агента — 720 с, причём начатый вызов не прерывался, поэтому
    настоящий потолок доходил до 1080 с;
  * мост ждал `llm_timeout_sec + 30` = 270 с.

То есть мост бросал запрос через четыре с половиной минуты, а ядро в режиме
`research` имело право работать двенадцать и продолжало считать. Доведённая до
конца работа выбрасывалась, а обновление уходило на повтор — тот же дорогой ход
считался заново.

"""

from __future__ import annotations

from dataclasses import replace

import pytest


@pytest.mark.parametrize("timeout_sec", [1.0, 5.0, 30.0, 240.0, 600.0, 3600.0])
def test_the_bridge_always_waits_longer_than_the_core_may_work(settings, timeout_sec):
    """Мутация: сделать таймаут моста равным потолку хода — краснеет.

    Проверяются КРАЙНИЕ значения настройки, а не только умолчание: формулы
    расходились именно потому, что их сверяли на одном числе."""

    tuned = replace(settings, llm_timeout_sec=timeout_sec)

    assert tuned.bridge_backend_timeout_sec > tuned.agent_turn_budget_sec, (
        f"при llm_timeout_sec={timeout_sec} мост ждёт "
        f"{tuned.bridge_backend_timeout_sec:.0f} с, а ход имеет право работать "
        f"{tuned.agent_turn_budget_sec:.0f} с — мост снова сдастся раньше ядра"
    )


def test_the_bridge_takes_its_timeout_from_the_single_source(settings, monkeypatch):
    """Проводочная проба: смотрит, с чем СОБРАЛИ мост.

    Мутация: вернуть мосту собственную формулу (`llm_timeout_sec + 30`) —
    краснеет. Значение здесь не переписывается вручную: проба сравнивает
    переданное с тем же свойством настроек, поэтому она переживёт изменение
    самой формулы и не переживёт её раздвоение."""
    import friday.config
    import friday.telegram_bridge
    from friday.cli import _run_telegram_bridge

    seen: list = []

    class _Bridge:
        def __init__(self, config):
            seen.append(config)

        async def run(self):
            return None

    tuned = replace(settings, llm_timeout_sec=120.0)
    monkeypatch.setattr(friday.config, "load_settings", lambda: tuned)
    monkeypatch.setattr(friday.config, "ensure_runtime_dirs", lambda _settings: None)
    monkeypatch.setattr(friday.config, "validate_settings", lambda _s, production=False: [])
    monkeypatch.setattr(friday.telegram_bridge, "TelegramBridge", _Bridge)
    monkeypatch.setenv("FRIDAY_TELEGRAM_BOT_TOKEN", "1:" + "T" * 34)

    _run_telegram_bridge()

    assert seen, "мост не собрали — проба проверяет не то"
    assert seen[0].backend_timeout_sec == tuned.bridge_backend_timeout_sec, (
        "мост снова считает таймаут своей формулой"
    )


def test_one_call_budget_has_exactly_one_definition(settings):
    """Мутация: вернуть формулу в `LLMRouter.total_budget_sec` — краснеет.

    Три копии одной формулы уже разошлись однажды; роутер обязан брать число из
    настроек, а не считать своё."""
    from friday.agent_runtime.llm import LLMRouter

    tuned = replace(settings, llm_timeout_sec=77.0)
    router = LLMRouter(tuned)

    assert router.total_budget_sec == tuned.llm_call_budget_sec
    assert tuned.agent_turn_budget_sec == tuned.llm_call_budget_sec * 2


def test_the_loop_refuses_a_call_that_cannot_finish_in_time(settings):
    """Потолок хода стал НАСТОЯЩИМ.

    Прежняя редакция не начинала новый круг ПОСЛЕ дедлайна, но уже начатый вызов
    был волен идти ещё один полный бюджет: объявленные 720 с превращались в 1080.
    Теперь круг не начинается, если вызов заведомо не успеет.

    Мутация: вернуть проверку `time.monotonic() >= loop_deadline` — краснеет.
    """
    import inspect

    from friday.agent_runtime import AgentRuntime

    source = inspect.getsource(AgentRuntime)
    assert "time.monotonic() + self.llm.total_budget_sec > loop_deadline" in source, (
        "цикл снова проверяет только «прошёл ли дедлайн», а не «успеет ли вызов»"
    )


def test_the_core_ceiling_and_the_bridge_wait_agree_on_the_production_router(settings):
    """Согласие двух чисел проверяется на БОЕВОМ роутере, а не на настройках.

    Цикл берёт бюджет у роутера — он бывает подменён двойником, и следовать надо
    тому, кто действительно ходит к модели. Мост берёт своё число у настроек.
    Значит настоящая гарантия здесь одна: на боевом роутере эти два пути обязаны
    сойтись. Ровно в этом месте они однажды и разъехались — 720 против 270.

    Мутация: изменить любую из двух формул по отдельности — краснеет."""
    from friday.agent_runtime.llm import LLMRouter

    for timeout_sec in (1.0, 30.0, 240.0, 3600.0):
        tuned = replace(settings, llm_timeout_sec=timeout_sec)
        loop_budget = LLMRouter(tuned).total_budget_sec * 2
        assert loop_budget == tuned.agent_turn_budget_sec, (
            f"при llm_timeout_sec={timeout_sec} цикл считает потолок {loop_budget:.0f} с, "
            f"а мост исходит из {tuned.agent_turn_budget_sec:.0f} с"
        )
        assert tuned.bridge_backend_timeout_sec > loop_budget
