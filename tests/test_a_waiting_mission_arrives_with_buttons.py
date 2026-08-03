"""Уведомление о миссии приходит с кнопками, а не с просьбой набрать команду.

Заявка на подтверждение так и приходит — и рядом с её кодом стоит объяснение
почему: «уведомление, которое лишь СООБЩАЕТ о необходимости решить, заставляет
человека идти за ним в другую команду, и половина смысла проактивности теряется».

Миссия до 2026-08-03 не приходила вовсе (висела `proposed` неделю). Когда стала
приходить — приходила голым текстом «Открыть: /missions», то есть ровно тем, что
в соседнем комментарии названо потерей половины смысла.

Кнопки те же, что в списке миссий, и обработчик у них общий: второй путь к тому
же действию однажды рассинхронизируется с первым.
"""

from __future__ import annotations

import pytest

from friday.telegram_bridge._transport import TransportMixin


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Bridge(TransportMixin):
    """Только то, что нужно доставке: остальное у моста здесь не участвует."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items
        self.sent: list[tuple[int, str, dict | None]] = []
        self._warned_no_signer = False

    # `_signer_chat` — не подменяется, а обслуживается: он ходит в
    # `config.allowed_chat_ids`, и подменить его значило бы проверять не тот путь.
    class _Config:
        allowed_chat_ids = (42,)

    config = _Config()

    def _may_message_chat(self, chat_id: int) -> bool:
        return True

    async def _backend_json(self, backend, method, path, payload, user, chat):  # noqa: ANN001, ARG002
        if "notifications/pending" in path:
            return {"items": self._items}
        return {}

    async def _send_message(self, telegram, chat_id, text, reply_markup=None, **kwargs):  # noqa: ANN001, ARG002
        self.sent.append((chat_id, text, reply_markup))

    async def _ack_outbound(self, backend, signer_chat, sent, failed):  # noqa: ANN001, ARG002
        return None


def _buttons(markup: dict | None) -> list[str]:
    if not markup:
        return []
    return [
        str(button.get("callback_data") or "")
        for row in markup.get("inline_keyboard", [])
        for button in row
    ]


@pytest.mark.anyio
async def test_a_mission_notice_carries_the_decision_buttons() -> None:
    """Мутация: убрать ветку `mission` — придёт голый текст, тест краснеет."""
    bridge = _Bridge(
        [
            {
                "id": "n1",
                "chat_id": "42",
                "body": "Миссия ждёт запуска: Сводка по входящим",
                "kind": "mission",
                "dedup_key": "mission:msn_95dcbd2dd38e4d3c",
            }
        ]
    )

    await bridge._drain_outbound(object(), object())

    assert bridge.sent, "уведомление не доставлено"
    _, text, markup = bridge.sent[0]
    assert "Сводка по входящим" in text
    assert _buttons(markup) == [
        "mission:start:msn_95dcbd2dd38e4d3c",
        "mission:stop:msn_95dcbd2dd38e4d3c",
    ], f"кнопок решения нет: {markup}"


@pytest.mark.anyio
async def test_the_approval_buttons_did_not_change() -> None:
    """Соседняя ветка не должна пострадать от появления новой."""
    bridge = _Bridge(
        [
            {
                "id": "n2",
                "chat_id": "42",
                "body": "Нужно ваше решение: объединить А и Б",
                "kind": "approval",
                "dedup_key": "approval:apr_123456",
            }
        ]
    )

    await bridge._drain_outbound(object(), object())

    assert _buttons(bridge.sent[0][2]) == ["apr:yes:apr_123456", "apr:no:apr_123456"]


@pytest.mark.anyio
async def test_an_ordinary_notice_stays_without_buttons() -> None:
    """Напоминание — не решение: кнопки там были бы обещанием, которого нет."""
    bridge = _Bridge(
        [{"id": "n3", "chat_id": "42", "body": "Напоминание: позвонить", "kind": "reminder", "dedup_key": ""}]
    )

    await bridge._drain_outbound(object(), object())

    assert bridge.sent[0][2] is None


@pytest.mark.anyio
async def test_a_forged_identifier_gets_no_buttons() -> None:
    """Ключ приходит из базы, но проверяется всё равно.

    В `callback_data` Telegram отдаёт 64 байта и никакой типизации: подставленная
    строка ушла бы обработчику как есть.
    """
    bridge = _Bridge(
        [
            {
                "id": "n4",
                "chat_id": "42",
                "body": "Миссия ждёт запуска",
                "kind": "mission",
                "dedup_key": "mission:../../etc/passwd",
            }
        ]
    )

    await bridge._drain_outbound(object(), object())

    assert bridge.sent[0][2] is None, "кривой идентификатор уехал в кнопку"
