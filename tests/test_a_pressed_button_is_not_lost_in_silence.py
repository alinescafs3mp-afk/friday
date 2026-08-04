"""Нажатая кнопка, не дошедшая до системы, не пропадает молча.

Мост подписан на три вида обновлений (`allowed_updates`), а чат обновления искался
ровно в одном — `update["message"]["chat"]`. У нажатой кнопки чат лежит на этаж
глубже, в `callback_query.message`, и поиск возвращал `None`. Воспроизведено: один
и тот же чат находится у текста и не находится у кнопки.

Единственный потребитель этого поиска — уведомление о неудаче, поэтому цена ровно
такая: человек нажал «Подтвердить», попытки исчерпались, и он не узнал ничего.

Молчание после нажатия — худшее из возможных толкований: пропавший ВОПРОС человек
задаст ещё раз, а пропавшее ПОДТВЕРЖДЕНИЕ он считает исполненным и больше к нему не
возвращается. Это та же асимметрия, что у «неизвестного исхода»: у подтверждения
дороже потерять сигнал.

Класс ошибки — не «забыли callback_query», а «подписка и разбор живут в разных
местах». Поэтому ниже сверяются оба списка: новый вид в подписке не должен снова
оказаться невидимым.
"""

from __future__ import annotations

import pytest

from friday.telegram_bridge._base import ALLOWED_UPDATE_KINDS
from friday.telegram_bridge._transport import TransportMixin

CHAT_ID = 46703577


def _text_update() -> dict:
    return {"message": {"message_id": 5, "chat": {"id": CHAT_ID, "type": "private"}, "text": "привет"}}


def _edited_update() -> dict:
    return {"edited_message": {"message_id": 5, "chat": {"id": CHAT_ID, "type": "private"}, "text": "не так"}}


def _button_update() -> dict:
    return {
        "callback_query": {
            "id": "1",
            "data": "approve:xyz",
            "from": {"id": CHAT_ID},
            "message": {"message_id": 5, "chat": {"id": CHAT_ID, "type": "private"}},
        }
    }


@pytest.mark.parametrize(
    ("name", "update"),
    [("текст", _text_update()), ("правка", _edited_update()), ("кнопка", _button_update())],
)
def test_every_kind_of_update_has_an_address(name, update):
    """Мутация: вернуть чтение только `update["message"]` — «кнопка» краснеет."""
    assert TransportMixin._update_chat_id(update) == CHAT_ID, (  # noqa: SLF001
        f"у обновления вида «{name}» не нашлось чата — уведомление о неудаче не уйдёт никуда"
    )


def test_the_subscription_and_the_reader_cannot_drift_apart():
    """Оба списка видов — один список.

    Ошибка была не в том, что забыли `callback_query`, а в том, что подписка на
    виды и их разбор жили порознь. Здесь это сверяется: каждый вид, который мост
    просит у Telegram, должен быть таким, у которого функция находит адресата.
    """
    carriers = {
        "message": _text_update(),
        "edited_message": _edited_update(),
        "callback_query": _button_update(),
    }
    unread = [kind for kind in ALLOWED_UPDATE_KINDS if kind not in carriers]
    assert not unread, (
        f"мост подписан на {unread}, а разбирать их этот тест не умеет — "
        "значит и уведомление о неудаче для них не проверено"
    )
    for kind in ALLOWED_UPDATE_KINDS:
        assert TransportMixin._update_chat_id(carriers[kind]) == CHAT_ID, (  # noqa: SLF001
            f"вид {kind} запрашивается у Telegram, но адресата у него не находят"
        )


def test_a_broken_button_is_told_about_as_a_decision(settings):
    """Человеку говорят про РЕШЕНИЕ, а не про «сообщение».

    После нажатия «Подтвердить» фраза «не удалось обработать это сообщение» не
    отвечает на его вопрос — подтвердилось действие или нет.
    """
    import asyncio

    sent: list[tuple[int, str]] = []

    class _Stand(TransportMixin):
        def __init__(self):
            self.config = type("cfg", (), {"allowed_chat_ids": {CHAT_ID}})()
            self._inbox = type("inbox", (), {"is_registered_chat": staticmethod(lambda _c: False)})()

        async def _send_message(self, _client, chat_id, text, **_kwargs):
            sent.append((chat_id, text))

    stand = _Stand()
    asyncio.run(stand._notify_dead_letter(None, _button_update(), permanent=False))  # noqa: SLF001
    assert sent, "после нажатия кнопки человек не получил ничего"
    _chat, text = sent[0]
    assert "решение" in text.casefold(), f"человеку не сказано про судьбу решения: {text!r}"

    sent.clear()
    asyncio.run(stand._notify_dead_letter(None, _text_update(), permanent=False))  # noqa: SLF001
    assert "сообщение" in sent[0][1].casefold(), "текстовому сообщению досталась формулировка про кнопку"
