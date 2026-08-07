"""Изменилось ПРАВИЛО приватности — долговечный кэш обязан пересчитаться.

Кэш приватного материала — это ответ, посчитанный прежней редакцией правил. Форма
таблиц при правке правила не меняется, поэтому номер схемы её не замечает, а
открытие базы с валидным состоянием кэш не пересобирает и не сверяет: старый ответ
живёт дальше. Замерено на копии живого архива — без этой отметки послабление §76
не вернуло бы владельцу ни одной из 108 запертых записей: код новый, кэш прежний.

Отметка — отпечаток текста самих правил, а не число, которое надо не забыть
поднять: любая будущая правка приватного SQL инвалидирует кэш сама.
"""

from __future__ import annotations

import sqlite3

from friday.storage import init_storage
from friday.storage._core import _PRIVATE_MATERIAL_RULE_MARKER_KEY


def _opened(settings) -> None:
    """Открыть базу так, чтобы схема действительно создалась."""

    storage = init_storage(settings)
    try:
        storage.ensure_user("rule-digest-probe")
    finally:
        storage.close()


def _marker(path) -> tuple[str, str]:
    connection = sqlite3.connect(str(path))
    try:
        row = connection.execute(
            "SELECT value, updated_at FROM schema_meta WHERE key=?",
            (_PRIVATE_MATERIAL_RULE_MARKER_KEY,),
        ).fetchone()
    finally:
        connection.close()
    return (str(row[0]), str(row[1])) if row else ("", "")


def _set_marker(path, value: str) -> None:
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "UPDATE schema_meta SET value=?, updated_at='2000-01-01T00:00:00+00:00' WHERE key=?",
            (value, _PRIVATE_MATERIAL_RULE_MARKER_KEY),
        )
        connection.commit()
    finally:
        connection.close()


def test_the_rule_digest_is_written_on_open(settings) -> None:
    _opened(settings)

    digest, _ = _marker(settings.database_path)

    assert digest, "отпечаток правила не записан вовсе"


def test_a_foreign_digest_forces_a_recompute_on_the_next_open(settings) -> None:
    """Проверяется ПОДКЛЮЧЕНИЕ: путь открытия обязан звать пересчёт."""

    _opened(settings)
    own, _ = _marker(settings.database_path)
    _set_marker(settings.database_path, "правило прежней редакции")

    _opened(settings)

    assert _marker(settings.database_path)[0] == own, (
        "база открылась с ответом, посчитанным по другому правилу"
    )


def test_an_unchanged_rule_is_not_rewritten_on_every_open(settings) -> None:
    """Иначе каждое открытие обесценивало бы весь кэш и пересобирало его заново."""

    _opened(settings)
    _, stamp = _marker(settings.database_path)

    _opened(settings)

    assert _marker(settings.database_path)[1] == stamp, (
        "неизменное правило всё равно переписало отметку — значит, и кэш"
    )
