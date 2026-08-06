"""Обрыв связи сохраняет сигнал, но не содержимое exception.

Замерено на живом журнале моста: 6.43 МБ файла, из них 6.39 МБ (99.5%) —
сцепленные трейсбеки httpx по ~5 КБ, 1368 у опроса и 70 у отправки. Причина одна:
Имя цикла и класс ошибки остаются; traceback и `str(error)` запрещены:
httpx часто включает в них полный URL Telegram с token или текст ответа.

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


def test_bridge_failures_keep_type_but_never_message_or_traceback(tmp_path, caplog):
    bridge = _bridge(tmp_path)
    sentinel = "SYNTHETIC_PRIVATE_BRIDGE_EXCEPTION_" + "x" * 5_000
    error = httpx.ConnectError(sentinel)

    with caplog.at_level(logging.WARNING, logger="friday.telegram_bridge"):
        bridge._log_loop_failure("poll", error)  # noqa: SLF001
        first = list(caplog.records)
        assert len(first) == 1
        assert first[0].levelno == logging.ERROR
        assert first[0].exc_info is None
        assert "poll" in first[0].getMessage()
        assert "ConnectError" in first[0].getMessage()
        assert sentinel not in first[0].getMessage()

        # Эпизод продолжается: мост уже знает, что цикл сломан.
        bridge._loop_failing["poll"] = True  # noqa: SLF001
        caplog.clear()
        for _ in range(20):
            bridge._log_loop_failure("poll", error)  # noqa: SLF001

    assert len(caplog.records) == 20
    assert all(record.exc_info is None for record in caplog.records)
    assert all(sentinel not in record.getMessage() for record in caplog.records)
    assert "ConnectError" in caplog.records[0].getMessage(), "имя исключения должно остаться"


def test_a_new_episode_gets_a_fresh_error_signal_without_traceback(tmp_path, caplog):
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
    assert caplog.records[0].levelno == logging.ERROR
    assert caplog.records[0].exc_info is None
    assert "ReadTimeout" in caplog.records[0].getMessage()
    assert "новый обрыв" not in caplog.records[0].getMessage()


def test_the_two_loops_are_counted_apart(tmp_path, caplog):
    bridge = _bridge(tmp_path)
    with caplog.at_level(logging.WARNING, logger="friday.telegram_bridge"):
        bridge._log_loop_failure("poll", httpx.ConnectError("опрос"))  # noqa: SLF001
        bridge._loop_failing["poll"] = True  # noqa: SLF001
        caplog.clear()
        bridge._log_loop_failure("outbound", httpx.ConnectError("отправка"))  # noqa: SLF001

    assert caplog.records[0].levelno == logging.ERROR
    assert caplog.records[0].exc_info is None
    assert "outbound" in caplog.records[0].getMessage()
