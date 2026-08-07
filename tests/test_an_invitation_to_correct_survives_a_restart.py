"""Приглашение «ответьте новым текстом» переживает перезапуск моста.

Правка записи из чата (0.178.0) устроена через ответ на конкретное сообщение:
мост шлёт приглашение и запоминает, о какой записи речь. Запоминал он это в
словаре процесса — и перезапуск разрывал связь МОЛЧА. Человек отвечал репликой
на приглашение, ответ не узнавался как правка и уходил к модели обычным
вопросом: запись оставалась неисправленной, а человек получал ответ на текст,
который он писал не в качестве вопроса.

Окно не редкое: ждать ответа приглашение может сколько угодно, а мост между тем
перезапускается при каждом релизе. Аудит Grok назвал это как L3 («in-memory
album captions / edit targets — lose on restart»), и из двух названных там
словарей durable нужен именно этот: подпись альбома живёт секунды, все части
альбома приходят одной пачкой опроса.
"""

from __future__ import annotations

from friday.telegram_bridge import TelegramBridge, TelegramConfig
from friday.telegram_bridge._base import _EDIT_TARGET_MEMORY

DOCUMENT_ID = "ko_0000000000000042"


def _bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


def test_the_invitation_outlives_the_process(tmp_path):
    bridge = _bridge(tmp_path)
    try:
        bridge._inbox.remember_edit_prompt(777, DOCUMENT_ID)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    restarted = _bridge(tmp_path)
    try:
        found = restarted._inbox.take_edit_prompt(777)  # noqa: SLF001
    finally:
        restarted._inbox.close()  # noqa: SLF001

    assert found == DOCUMENT_ID, "после перезапуска ответ человека было бы некуда адресовать"


def test_an_invitation_is_used_once(tmp_path):
    """Забрать, а не прочитать: второй ответ на то же сообщение не правит снова."""
    bridge = _bridge(tmp_path)
    try:
        bridge._inbox.remember_edit_prompt(778, DOCUMENT_ID)  # noqa: SLF001
        first = bridge._inbox.take_edit_prompt(778)  # noqa: SLF001
        second = bridge._inbox.take_edit_prompt(778)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert first == DOCUMENT_ID
    assert second == "", "приглашение сработало второй раз"


def test_the_memory_stays_bounded(tmp_path):
    """Потолок тот же, что был у словаря: очередь живёт вместе с мостом годами."""
    bridge = _bridge(tmp_path)
    try:
        for index in range(_EDIT_TARGET_MEMORY + 5):
            bridge._inbox.remember_edit_prompt(1000 + index, f"ko_{index:016d}")  # noqa: SLF001
        rows = bridge._inbox._conn.execute("SELECT COUNT(*) AS n FROM edit_prompts").fetchone()  # noqa: SLF001
        oldest = bridge._inbox.take_edit_prompt(1000)  # noqa: SLF001
        newest = bridge._inbox.take_edit_prompt(1000 + _EDIT_TARGET_MEMORY + 4)  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert int(rows["n"]) == _EDIT_TARGET_MEMORY, f"потолок приглашений не держится: {rows['n']}"
    assert oldest == "", "вытеснено должно быть самое старое"
    assert newest, "вытеснено самое свежее вместо самого старого"


def test_an_unknown_reply_is_not_an_edit(tmp_path):
    """Ответ на любое другое сообщение остаётся обычным вопросом."""
    bridge = _bridge(tmp_path)
    try:
        assert bridge._inbox.take_edit_prompt(999) == ""  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001
