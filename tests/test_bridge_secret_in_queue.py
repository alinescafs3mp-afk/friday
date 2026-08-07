"""Проверка здоровья не должна печатать credential, который она проверяет.

Адрес Telegram API СОДЕРЖИТ токен бота — `.../bot<token>/sendMessage`, — а httpx
цитирует адрес внутри текста каждого `HTTPStatusError`. Эта строка попадала на
строку очереди как есть (`mark_failure`/`mark_dead_letter`), а диагностика потом
показывала последнюю ошибку через `jericho doctor`, `jericho status --json` и
`GET /api/admin/diagnostics`. То есть единственная команда, которую оператор
запускает, когда что-то сломалось, — и вставляет в отчёт об ошибке — печатала
токен бота.

Защищают ДВА конца, и оба проверяются здесь:

* пишущий — мост кладёт на строку только ИМЯ КЛАССА исключения, не его текст;
* читающий — диагностика чистит то, что прочитала, на случай строки, записанной
  прежней сборкой.

До 0.185.0 рядом жил третий, никем не вызываемый: `TelegramBridge._redact`
присваивался в конструкторе и не использовался ни разу. Проба у него была — вот
эта, — и она проверяла ФУНКЦИЮ, а не её подключение, поэтому годы молчала о том,
что подключения нет. Теперь проверяется путь: настоящее исключение с токеном в
тексте прогоняется через настоящую обработку неудачи, и токен ищется в том, что
реально легло в базу.
"""

from __future__ import annotations

import httpx
import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig

TOKEN = "123456789:AAHqwertyuiopASDFGHJKLzxcvbnm-Test01"


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token=TOKEN,
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def _telegram_error() -> httpx.HTTPStatusError:
    """Ровно та форма, которую строит сам httpx: адрес с токеном внутри текста."""
    request = httpx.Request("POST", f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    response = httpx.Response(400, request=request, json={"description": "Bad Request"})
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as raised:
        return raised
    raise AssertionError("raise_for_status не бросил на 400")


def test_the_premise_holds_the_raw_text_really_leaks_the_token() -> None:
    """Опора всей пробы. Без неё «токена нет в базе» верно и без единой защиты."""
    error = _telegram_error()
    assert TOKEN in f"{type(error).__name__}: {error}"


@pytest.mark.asyncio
async def test_a_failing_send_does_not_carry_the_token_into_the_queue(tmp_path):
    """Настоящий путь неудачи: обновление, ход по нему, запись исхода в очередь."""
    bridge = _bridge(tmp_path)
    error = _telegram_error()

    async def _explode(*_args, **_kwargs):
        raise error

    update = {
        "update_id": 4242,
        "message": {
            "message_id": 1,
            "chat": {"id": 5001},
            "from": {"id": 5001, "first_name": "Владелец"},
            "text": "привет",
        },
    }
    try:
        bridge._inbox.store(update)  # noqa: SLF001
        row = next(row for row in bridge._inbox.pending() if int(row["update_id"]) == 4242)  # noqa: SLF001
        bridge._process_update = _explode  # noqa: SLF001
        await bridge._run_update(object(), object(), row)  # noqa: SLF001
        stored = bridge._inbox._conn.execute(  # noqa: SLF001
            "SELECT last_error FROM updates WHERE update_id=4242"
        ).fetchone()
        last_error = str(stored["last_error"])
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert TOKEN not in last_error, f"токен бота лёг в очередь: {last_error!r}"
    assert "api.telegram.org" not in last_error, f"адрес с токеном лёг в очередь: {last_error!r}"
    # Всё ещё пригодно для разбора: неудача названа.
    assert "HTTPStatusError" in last_error, last_error


def test_diagnostics_redacts_what_it_reads_back(tmp_path):
    """Второй конец: строка, записанная прежней сборкой, не должна утечь при чтении."""
    import sqlite3

    from friday.diagnostics import _bridge_queue_status

    database = tmp_path / "telegram.sqlite3"
    bridge = _bridge(tmp_path)
    # Opening is explicit in this fixture.  The production constructor must stay
    # side-effect free so a second bridge cannot map the live WAL before losing
    # the process lease; calling an inbox operation is what opens/migrates it.
    assert bridge._inbox.get_offset() == 0  # noqa: SLF001
    bridge._inbox.close()  # noqa: SLF001 - schema is durable; write a legacy row directly

    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO updates(update_id, payload_json, status, attempts, last_error, failed_at,"
        " created_at) VALUES(1, '{}', 'dead_letter', 5, ?, 1, 1)",
        (f"HTTPStatusError: 400 for url https://api.telegram.org/bot{TOKEN}/sendMessage",),
    )
    connection.commit()
    connection.close()

    status = _bridge_queue_status(database)
    assert status["dead_letter"] == 1
    assert TOKEN not in status["last_dead_letter_error"], status["last_dead_letter_error"]
