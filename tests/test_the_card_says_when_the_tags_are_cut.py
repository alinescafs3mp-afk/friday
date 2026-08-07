"""Карточка объекта не выдаёт пятнадцать тегов за полный набор.

Находка глобального аудита, найденная прибором, а не чтением наугад: обход
дерева разбора нашёл ключи, которые ПИШУТСЯ и не читаются никем. Среди них —
`tags_matched_at_least` и `tags_truncated`: хранилище честно считает, сколько
тегов подошло и был ли список обрезан (`storage/_knowledge.py`), профиль честно
проносит это наружу, а карточка в чате не читала ни того, ни другого — и резала
показанное ЕЩЁ РАЗ, на пятнадцати.

Два обреза, ноль слов человеку. Тот же класс «молчаливый обрез», который за
смену ловится седьмой раз, и та же схема: механизм есть, потребителя нет.

Проба идёт через настоящую `_send_entity_profile` с ответом той формы, которую
даёт маршрут: собранный руками словарь — ровно то, из-за чего прежние пробы
оставались зелёными при мёртвой ветке.
"""

from __future__ import annotations

from typing import Any

import pytest

from friday.telegram_bridge import TelegramBridge


async def _card(monkeypatch, profile: dict[str, Any]) -> str:
    bridge = TelegramBridge.__new__(TelegramBridge)
    sent: list[str] = []

    async def _fake_send(_self, _telegram, _chat_id, text, *, reply_markup=None, **_kwargs):
        sent.append(text)

    async def _fake_backend_json(*_args, **_kwargs):
        return {
            "entity": {"id": "ent_1", "name": "Иванов"},
            "profile": profile,
            "profile_provenance": {"source_count": 40},
            "relations": [],
            "knowledge_objects": [],
            "knowledge_objects_total": 0,
            "pending_relations_count": 0,
            "event_time": None,
            "edits": {"versions": 0, "last_edited_at": None, "restorable_version": None},
        }

    monkeypatch.setattr(TelegramBridge, "_send_message", _fake_send, raising=False)
    monkeypatch.setattr(TelegramBridge, "_backend_json", _fake_backend_json, raising=False)
    await bridge._send_entity_profile(None, None, 1, "42", {"id": 42}, "Иванов")
    assert sent, "карточка не отправлена"
    return sent[0]


@pytest.mark.asyncio
async def test_a_cut_tag_list_says_so(monkeypatch):
    """Обрез на стороне ХРАНИЛИЩА: тегов больше, чем оно вернуло."""
    text = await _card(
        monkeypatch,
        {
            "tags": [f"тег{index}" for index in range(12)],
            "tags_matched_at_least": 40,
            "tags_truncated": True,
            "document_date_range": None,
            "documents_without_own_date": 0,
        },
    )
    assert "показаны 12 из 40" in text, text


@pytest.mark.asyncio
async def test_the_card_own_ceiling_is_named_too(monkeypatch):
    """Второй обрез — свой, на пятнадцати. Он тоже должен быть слышен."""
    text = await _card(
        monkeypatch,
        {
            "tags": [f"тег{index}" for index in range(30)],
            "tags_matched_at_least": 30,
            "tags_truncated": False,
            "document_date_range": None,
            "documents_without_own_date": 0,
        },
    )
    assert "показаны 15 из 30" in text, text


@pytest.mark.asyncio
async def test_a_complete_tag_list_says_nothing_extra(monkeypatch):
    """Оговорка только там, где есть о чём говорить."""
    text = await _card(
        monkeypatch,
        {
            "tags": ["приказ", "смета"],
            "tags_matched_at_least": 2,
            "tags_truncated": False,
            "document_date_range": None,
            "documents_without_own_date": 0,
        },
    )
    assert "#приказ" in text
    assert "показаны" not in text, text
