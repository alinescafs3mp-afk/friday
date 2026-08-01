"""Обрыв связи пишется один раз со стеком, а не на каждой попытке.

Замерено на живом журнале моста: 6.43 МБ файла, из них 6.39 МБ (99.5%) —
сцепленные трейсбеки httpx по ~5 КБ, 1368 у опроса и 70 у отправки. Причина одна:
`LOGGER.exception` стоял внутри цикла, у которого уже есть экспоненциальный откат,
то есть печатался на КАЖДОЙ попытке переподключения.

Под этим шумом похоронено то, что стоит знать: переходы «сломалось/починилось»
пишутся в `runtime_events` отдельно — 300 падений опроса против 299 восстановлений,
суммарно 6 ч 13 мин недоступности за четверо суток, самый длинный обрыв 49 мин 27 с.
Ротация журналов работает, но крутила почти исключительно этот шум.
"""

from __future__ import annotations

import logging

import httpx

from friday.telegram_bridge import TelegramBridge, TelegramConfig


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:abc",
            inbox_db_path=str(tmp_path / "inbox.sqlite3"),
            bridge_secret="S" * 48,
            allowed_chat_ids=[42],
        )
    )


def test_the_first_failure_carries_a_traceback_and_the_rest_do_not(tmp_path, caplog):
    bridge = _bridge(tmp_path)
    error = httpx.ConnectError("сеть недоступна")

    with caplog.at_level(logging.WARNING, logger="friday.telegram_bridge"):
        bridge._log_loop_failure("poll", error)  # noqa: SLF001
        first = list(caplog.records)
        assert len(first) == 1
        assert first[0].exc_info is not None, "первая неудача обязана нести стек"

        # Эпизод продолжается: мост уже знает, что цикл сломан.
        bridge._loop_failing["poll"] = True  # noqa: SLF001
        caplog.clear()
        for _ in range(20):
            bridge._log_loop_failure("poll", error)  # noqa: SLF001

    assert len(caplog.records) == 20
    assert all(record.exc_info is None for record in caplog.records), (
        "повторные попытки печатают стек — ровно то, что раздуло журнал до 99.5% шума"
    )
    assert "ConnectError" in caplog.records[0].getMessage(), "имя исключения должно остаться"


def test_a_new_episode_gets_its_traceback_again(tmp_path, caplog):
    """Иначе вторая, другая поломка осталась бы без объяснения."""
    bridge = _bridge(tmp_path)
    with caplog.at_level(logging.WARNING, logger="friday.telegram_bridge"):
        bridge._log_loop_failure("poll", httpx.ConnectError("первый обрыв"))  # noqa: SLF001
        bridge._loop_failing["poll"] = True  # noqa: SLF001
        bridge._log_loop_failure("poll", httpx.ConnectError("тот же обрыв"))  # noqa: SLF001
        # Цикл починился и сломался снова — уже по другой причине.
        bridge._loop_failing["poll"] = False  # noqa: SLF001
        caplog.clear()
        bridge._log_loop_failure("poll", httpx.ReadTimeout("новый обрыв"))  # noqa: SLF001

    assert len(caplog.records) == 1
    assert caplog.records[0].exc_info is not None


def test_the_two_loops_are_counted_apart(tmp_path, caplog):
    """Сломанная отправка не должна лишить опрос его стека, и наоборот."""
    bridge = _bridge(tmp_path)
    with caplog.at_level(logging.WARNING, logger="friday.telegram_bridge"):
        bridge._log_loop_failure("poll", httpx.ConnectError("опрос"))  # noqa: SLF001
        bridge._loop_failing["poll"] = True  # noqa: SLF001
        caplog.clear()
        bridge._log_loop_failure("outbound", httpx.ConnectError("отправка"))  # noqa: SLF001

    assert caplog.records[0].exc_info is not None
