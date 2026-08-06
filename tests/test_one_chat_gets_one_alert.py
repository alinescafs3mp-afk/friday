"""Одно и то же оповещение приходит в чат ОДИН раз.

Найдено владельцем в живом Telegram 2026-08-04 («было оповещение от Пятницы,
правда два подряд») и подтверждено по базе — задвоены ВСЕ записи сторожа:

    2026-08-04T10:49:33 ×2  «Модель принимает соединения, но не отвечает»
    2026-08-04T05:04:02 ×2  «Проверьте конфигурацию» (TLS)
    2026-08-04T05:04:02 ×2  «Секрет лежит в постороннем файле»

Совпадение времени до секунды означает не два прогона, а двойную постановку в
очередь внутри одного. Причина видна в самих строках:

    964e5f17-a4bf-…            | чат 467035772 | sentinel:llm_not_generating:2026-08-04
    telegram:telegram:467035772 | чат 467035772 | sentinel:llm_not_generating:2026-08-04

У владельца ДВЕ учётки — через API и через бота, — а чат один. Ключ уникальности
стоял по `user_id`, поэтому обе строки проходили, и в один чат приходило два
одинаковых сообщения. Граница была проведена не по тому признаку: получает
сообщение чат, а не строка в базе.

Цена здесь выше обычной. Этим каналом система говорит о лежащей модели и об
утёкшем секрете; удвоение превращает такое сообщение в шум, а шум перестают
читать — и следующее, настоящее, пройдёт мимо глаз.
"""

from __future__ import annotations

import sqlite3

import pytest


def test_two_accounts_one_chat_get_one_message(storage):
    """Мутация: вернуть уникальность по `user_id` — тест краснеет."""
    storage.ensure_user("owner-api")
    storage.ensure_user("telegram:telegram:467035772")
    storage.commit()

    first = storage.enqueue_notification(
        "owner-api",
        "467035772",
        "🚨 Модель не отвечает",
        kind="sentinel",
        dedup_key="sentinel:llm:2026-08-04",
    )
    second = storage.enqueue_notification(
        "telegram:telegram:467035772",
        "467035772",
        "🚨 Модель не отвечает",
        kind="sentinel",
        dedup_key="sentinel:llm:2026-08-04",
    )

    assert first is True, "первое оповещение не поставилось в очередь"
    assert second is False, "в один чат ушло два одинаковых сообщения"
    queued = [row for row in storage.list_pending_notifications(limit=20) if row["chat_id"] == "467035772"]
    assert len(queued) == 1


def test_different_chats_still_both_get_it(storage):
    """Ошибка в другую сторону: два РАЗНЫХ человека должны узнать оба."""
    storage.ensure_user("person-a")
    storage.ensure_user("person-b")
    storage.commit()

    assert storage.enqueue_notification("person-a", "111", "текст", dedup_key="alert:2026-08-04")
    assert storage.enqueue_notification("person-b", "222", "текст", dedup_key="alert:2026-08-04")

    chats = {row["chat_id"] for row in storage.list_pending_notifications(limit=20)}
    assert {"111", "222"} <= chats


def test_a_different_day_is_a_different_alert(storage):
    """Ключ несёт сутки: завтра то же самое — это новое сообщение."""
    storage.ensure_user("person-a")
    storage.commit()

    assert storage.enqueue_notification("person-a", "111", "текст", dedup_key="alert:2026-08-04")
    assert storage.enqueue_notification("person-a", "111", "текст", dedup_key="alert:2026-08-05")


def test_an_empty_key_never_deduplicates(storage):
    """Без ключа дедупа нет вовсе — иначе разные сообщения глотали бы друг друга."""
    storage.ensure_user("person-a")
    storage.commit()

    assert storage.enqueue_notification("person-a", "111", "первое")
    assert storage.enqueue_notification("person-a", "111", "второе")


def test_the_index_is_rebuilt_on_an_old_database(settings, tmp_path, simulate_legacy_schema):
    """Правка обязана доехать до ЖИВОЙ базы, а не только до созданной с нуля.

    `CREATE ... IF NOT EXISTS` смотрит только на имя: индекс с прежним набором
    столбцов пережил бы обновление, и дубли остались бы ровно там, где их видит
    владелец. Тесты этого не замечают — они всегда начинают с пустой базы.

    Мутация: убрать `_retire_outdated_indexes` — тест краснеет.
    """
    from friday.storage import init_storage

    # Настоящая база сегодняшней схемы — а потом ей возвращают ПРЕЖНИЙ индекс и
    # прежнюю отметку версии. Так выглядит живая база, которую обновляют: сборка
    # таблиц руками этого не показала бы, потому что миграция и не запустилась бы.
    first = init_storage(settings)
    first.ensure_user("owner-api")
    first.ensure_user("owner-telegram")
    with first.transaction() as conn:
        conn.execute("DROP INDEX IF EXISTS uq_outbound_dedup")
        conn.execute(
            "CREATE UNIQUE INDEX uq_outbound_dedup "
            "ON outbound_notifications(user_id, dedup_key) WHERE dedup_key <> ''"
        )
        # ДУБЛИ, которые эта база уже накопила: две учётки, один чат, один ключ.
        # Без них тест зелен при неработающей миграции — ровно так и вышло:
        # правка прошла набор и уронила живой экземпляр, потому что новый
        # уникальный индекс не создаётся поверх строк, которые он запрещает.
        for index, person in enumerate(("owner-api", "owner-telegram")):
            conn.execute(
                "INSERT INTO outbound_notifications(id, user_id, chat_id, kind, dedup_key, body,"
                " status, attempts, created_at) VALUES(?,?,?,?,?,?,'sent',0,?)",
                (
                    f"notif-{index}",
                    person,
                    "467035772",
                    "sentinel",
                    "sentinel:llm:2026-08-04",
                    "🚨 Модель не отвечает",
                    f"2026-08-04T10:49:3{index}+00:00",
                ),
            )
    first.close()

    with sqlite3.connect(settings.database_path) as legacy:
        simulate_legacy_schema(legacy, 25)

    storage = init_storage(settings)
    try:
        definition = storage.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_outbound_dedup'"
        ).fetchone()
        assert definition, "индекс исчез вовсе"
        assert "chat_id" in str(definition[0]), "на старой базе остался индекс по учётке"
        # Дубль убран, а первая строка — та, что человек уже видел, — осталась.
        rows = storage.execute(
            "SELECT id FROM outbound_notifications WHERE dedup_key='sentinel:llm:2026-08-04'"
        ).fetchall()
        assert len(rows) == 1, f"дубли пережили миграцию: {[dict(row) for row in rows]}"
        assert str(rows[0]["id"]) == "notif-0", "оставлена не самая ранняя строка"
    finally:
        storage.close()


@pytest.mark.parametrize("kind", ["sentinel", "reminder"])
def test_every_kind_shares_the_rule(storage, kind: str):
    """Правило одно для всех видов: дубль в одном чате не нужен никому."""
    storage.ensure_user("person-a")
    storage.ensure_user("person-b")
    storage.commit()

    assert storage.enqueue_notification("person-a", "555", "текст", kind=kind, dedup_key=f"{kind}:x")
    assert not storage.enqueue_notification("person-b", "555", "текст", kind=kind, dedup_key=f"{kind}:x")
