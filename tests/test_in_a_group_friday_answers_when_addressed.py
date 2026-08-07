"""В группе Пятница отвечает, когда обращаются к ней, а не всем подряд.

Прежде в разрешённой группе к модели уходило КАЖДОЕ сообщение: разговор двух
людей о своём становился и вопросом, и материалом для архива, и счётом за
модель. Понятия «ко мне обратились» у моста не было вовсе — отсюда и запись в
`OPEN.md` §9 «упоминание бота в группе не читается».

Обращением считаются три вещи и только они:

* упоминание по `@имени` (имя мост узнаёт у самого Telegram через `getMe`);
* ответ на сообщение самой Пятницы;
* команда.

Упоминание УБИРАЕТСЯ из текста: «@friday_bot что по смете» — это вопрос «что по
смете», а не вопрос про бота. Оставленное упоминание попадало бы и в
классификатор, и в поиск, и в сохранённую запись.

Личная переписка не меняется ничем: там обращение и есть само сообщение.
"""

from __future__ import annotations

from typing import Any

from friday.telegram_bridge import TelegramBridge, TelegramConfig


def _bridge(tmp_path, *, username: str = "friday_bot") -> TelegramBridge:
    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001, -700],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )
    bridge._bot_username = username  # noqa: SLF001 - обычно приходит из getMe
    return bridge


def _group(text: str, **extra: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    chat = {"id": -700, "type": "supergroup"}
    message: dict[str, Any] = {"message_id": 5, "chat": chat, "text": text, **extra}
    return message, chat


def test_a_mention_is_an_address_and_is_cut_out(tmp_path):
    bridge = _bridge(tmp_path)
    message, chat = _group("@friday_bot что по смете за март?")
    try:
        addressed, text = bridge._group_address(message, chat, message["text"])  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert addressed is True
    assert text == "что по смете за март?", text


def test_a_mention_at_the_end_works_too(tmp_path):
    """Обратиться могут и в конце: «что по смете, @friday_bot»."""
    bridge = _bridge(tmp_path)
    message, chat = _group("что по смете, @Friday_Bot")
    try:
        addressed, text = bridge._group_address(message, chat, message["text"])  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert addressed is True
    assert "@" not in text, text
    assert text == "что по смете,", text


def test_someone_elses_conversation_is_left_alone(tmp_path):
    """Главное свойство: молчать, когда говорят не с тобой.

    И не отвечать, и не записывать: чужой разговор — не материал для архива.
    """
    bridge = _bridge(tmp_path)
    message, chat = _group("Петь, ты смету видел?")
    try:
        addressed, text = bridge._group_address(message, chat, message["text"])  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert addressed is False
    assert text == "Петь, ты смету видел?"


def test_a_reply_to_friday_is_an_address(tmp_path):
    """Продолжение разговора не требует повторного упоминания в каждой реплике."""
    bridge = _bridge(tmp_path)
    message, chat = _group(
        "а по второму пункту?",
        reply_to_message={"message_id": 4, "from": {"id": 42, "is_bot": True}},
    )
    try:
        addressed, _ = bridge._group_address(message, chat, message["text"])  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert addressed is True


def test_a_reply_to_another_person_is_not(tmp_path):
    """Вторая половина того же: ответ ЧЕЛОВЕКУ — не обращение к боту."""
    bridge = _bridge(tmp_path)
    message, chat = _group(
        "да, видел",
        reply_to_message={"message_id": 4, "from": {"id": 77, "is_bot": False}},
    )
    try:
        addressed, _ = bridge._group_address(message, chat, message["text"])  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert addressed is False


def test_a_command_is_always_an_address(tmp_path):
    bridge = _bridge(tmp_path)
    message, chat = _group("/status")
    try:
        addressed, text = bridge._group_address(message, chat, message["text"])  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert addressed is True
    assert text == "/status"


def test_an_update_without_a_chat_kind_behaves_as_before(tmp_path):
    """Требование обращения включается ТОЛЬКО там, где Telegram сказал «группа».

    Цена ошибки несимметрична: лишний ответ — шум, лишнее молчание — сломанный
    главный интерфейс продукта. Поэтому умолчание здесь открытое, и это решение,
    а не недосмотр.
    """
    bridge = _bridge(tmp_path)
    chat = {"id": 5001}
    message = {"message_id": 5, "chat": chat, "text": "что по смете?"}
    try:
        addressed, text = bridge._group_address(message, chat, message["text"])  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert addressed is True
    assert text == "что по смете?"


def test_a_private_chat_is_unchanged(tmp_path):
    """В личке обращение — само сообщение; ничего вырезать и решать не нужно."""
    bridge = _bridge(tmp_path)
    chat = {"id": 5001, "type": "private"}
    message = {"message_id": 5, "chat": chat, "text": "что по смете?"}
    try:
        addressed, text = bridge._group_address(message, chat, message["text"])  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert addressed is True
    assert text == "что по смете?"


def test_without_a_known_name_a_mention_is_not_recognised(tmp_path):
    """Ограничение названо, а не спрятано.

    Если `getMe` не ответил, имени нет — и упоминание распознать нечем. Остаются
    команды и ответы. Тихо отвечать всем подряд было бы хуже, чем отвечать реже.
    """
    bridge = _bridge(tmp_path, username="")
    message, chat = _group("@friday_bot что по смете?")
    try:
        addressed, _ = bridge._group_address(message, chat, message["text"])  # noqa: SLF001
    finally:
        bridge._inbox.close()  # noqa: SLF001

    assert addressed is False
