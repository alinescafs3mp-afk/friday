"""Сказанное в чате остаётся навсегда — и это обеспечивает база, а не код.

Требование владельца 2026-08-01: «вся информация из чата должна быть неудаляема
на уровне базы, то есть попала в чат один раз и всё».

Ключевое слово — «на уровне базы». Проверка в приложении защищает ровно те пути,
о которых знает автор проверки: новый маршрут, забытый скрипт обслуживания или
`sqlite3` из консоли обходят её молча. Триггер отменяет транзакцию независимо от
того, кто и откуда пришёл, поэтому тесты ниже бьют по хранилищу НАПРЯМУЮ, минуя
всякий прикладной код.
"""

from __future__ import annotations

import sqlite3

import pytest


def _say(storage, user_id: str = "alice") -> tuple[str, str]:
    storage.ensure_user(user_id)
    conversation = storage.create_conversation(user_id, "Разговор")
    message = storage.store_message(conversation["id"], user_id, "user", "Это сказано навсегда")
    return conversation["id"], str(message["id"] if isinstance(message, dict) else message)


def test_a_message_cannot_be_deleted_even_by_raw_sql(storage):
    """Мутация: убрать триггер `messages_are_never_deleted` — тест краснеет."""
    conversation_id, _ = _say(storage)

    with pytest.raises(sqlite3.IntegrityError) as failure, storage.transaction() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
    assert "неудаляем" in str(failure.value)

    remaining = storage.execute(
        "SELECT COUNT(*) c FROM messages WHERE conversation_id=?", (conversation_id,)
    ).fetchone()["c"]
    assert remaining == 1, "сообщение всё-таки исчезло"


def test_the_text_of_a_message_cannot_be_rewritten(storage):
    """Правка содержимого — то же стирание, только тише."""
    conversation_id, _ = _say(storage)

    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute(
            "UPDATE messages SET content='другое' WHERE conversation_id=?", (conversation_id,)
        )

    kept = storage.execute(
        "SELECT content FROM messages WHERE conversation_id=?", (conversation_id,)
    ).fetchone()["content"]
    assert kept == "Это сказано навсегда"


def test_service_columns_of_a_message_can_still_change(storage):
    """Запрет — на сказанное, а не на служебные пометки рядом с ним.

    Иначе неудаляемость превратилась бы в неработоспособность: разметка,
    счётчики и ссылки на разговор обязаны обновляться.
    """
    conversation_id, _ = _say(storage)
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE messages SET metadata_json='{\"seen\": true}' WHERE conversation_id=?",
            (conversation_id,),
        )
    row = storage.execute(
        "SELECT metadata_json FROM messages WHERE conversation_id=?", (conversation_id,)
    ).fetchone()
    assert "seen" in str(row["metadata_json"])


def test_a_conversation_cannot_be_deleted(storage):
    """Контейнер тоже остаётся: иначе история цела, но недоступна."""
    conversation_id, _ = _say(storage)
    with pytest.raises(sqlite3.IntegrityError), storage.transaction() as conn:
        conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))


def test_deleting_a_conversation_archives_it_and_keeps_every_word(storage):
    """То, что раньше стирало историю, теперь убирает разговор из списка."""
    conversation_id, _ = _say(storage)
    storage.store_message(conversation_id, "alice", "assistant", "И это тоже")

    report = storage.delete_conversation(conversation_id, "alice")

    assert report["existed"] is True
    assert report["archived"] is True
    assert report["messages_kept"] == 2, "сообщения не сохранены"
    assert storage.get_conversation(conversation_id, "alice"), "разговор исчез из базы"
    kept = storage.execute(
        "SELECT COUNT(*) c FROM messages WHERE conversation_id=?", (conversation_id,)
    ).fetchone()["c"]
    assert kept == 2


def test_the_archived_conversation_drops_out_of_the_default_list(storage):
    """Убрать из списка — обязано работать, иначе человеку некуда деться."""
    conversation_id, _ = _say(storage)
    storage.delete_conversation(conversation_id, "alice")

    visible = [item["id"] for item in storage.list_conversations("alice")]
    assert conversation_id not in visible

    archived = storage.get_conversation(conversation_id, "alice")
    assert archived and archived.get("is_archived")


def test_the_history_is_still_searchable_after_removal(storage):
    """Сохранённое, но ненаходимое — почти то же, что стёртое."""
    conversation_id, _ = _say(storage)
    storage.delete_conversation(conversation_id, "alice")

    found = storage.search_messages("alice", "навсегда")
    assert any(conversation_id == str(item.get("conversation_id")) for item in found), (
        "сообщение убранного разговора перестало находиться поиском по переписке"
    )
