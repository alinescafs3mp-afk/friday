"""Смещение опроса доезжает на диск само, а не за компанию.

`set_offset` выполнял `INSERT ... ON CONFLICT` и не звал `commit`: запись висела
в открытой транзакции до ближайшего `store()` — то есть до СЛЕДУЮЩЕГО
обновления. В тихом чате или при остановке моста она не доезжала на диск вовсе.

Повторной обработки это не давало: `store` вставляет через `INSERT OR IGNORE`, а
отвеченная строка удаляется целиком. Но состояние на диске расходилось с
состоянием в памяти, а расхождение, которое ничего не ломает сегодня, ломает
завтра — например, когда рядом появится второй читатель этой же таблицы.
"""

from __future__ import annotations

from friday.telegram_bridge._queue import _UpdateInbox


def test_the_offset_is_durable_without_a_later_write(tmp_path):
    """Ни одного `store` после — и всё равно на диске."""
    path = str(tmp_path / "telegram.sqlite3")
    inbox = _UpdateInbox(path)
    try:
        inbox.set_offset(4242)
    finally:
        inbox.close()

    reopened = _UpdateInbox(path)
    try:
        assert reopened.get_offset() == 4242, "смещение не доехало на диск"
    finally:
        reopened.close()


def test_the_offset_never_goes_negative(tmp_path):
    """Прежнее свойство цело: отрицательное смещение Telegram не понимает."""
    path = str(tmp_path / "telegram.sqlite3")
    inbox = _UpdateInbox(path)
    try:
        inbox.set_offset(-5)
        assert inbox.get_offset() == 0
    finally:
        inbox.close()
