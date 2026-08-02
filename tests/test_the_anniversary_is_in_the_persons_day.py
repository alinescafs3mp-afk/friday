"""Годовщина считается по суткам ЧЕЛОВЕКА, а не по UTC.

Найдено при разборе теста, покрасневшего после полуночи по Москве. Хроника «в этот
день» брала месяц и день из `local_now` (у человека уже 3 августа), а сравнивала с
`created_at`, который лежит в базе в UTC (там ещё 2-е). Две разные шкалы.

Замерено на живом архиве: в окно 21:00–24:00 UTC при МСК попадают 2 записи из
1533 — 0.1%. Редко, но годовщина такой записи показалась бы не в свой день, и
понять причину по одному сообщению в чате невозможно.

Третий случай этого класса: тихие часы по Гринвичу давали шесть перевёрнутых часов
из двадцати четырёх, «вчера» в напоминаниях считалось не тем днём.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta, timezone

from friday.organs.chronicle import build_on_this_day
from friday.storage.models import KnowledgeObject, RawObject, new_id

MSK = timezone(timedelta(hours=3))


def _seed(storage, title: str, created_at: str) -> None:
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref=new_id("src"),
        raw_content=title,
        content_type="text",
        content_hash=hashlib.sha256(new_id("h").encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        content=title,
        content_type="text",
        title=title,
        summary=title,
        tags_json=["память"],
    )
    storage.store_knowledge_object(ko)
    with storage.transaction() as conn:
        conn.execute("UPDATE knowledge_objects SET created_at=? WHERE id=?", (created_at, ko.id))


def test_a_late_evening_record_keeps_the_persons_date(storage) -> None:
    """Мутация: убрать сдвиг — тест краснеет.

    Запись сделана 3 августа в 00:30 по Москве. В UTC это 2 августа 21:30. Для
    человека годовщина наступает ТРЕТЬЕГО, и именно в этот день он ждёт её от
    Пятницы.
    """
    storage.ensure_user("alice")
    _seed(storage, "Поздний вечер", datetime(2025, 8, 2, 21, 30, tzinfo=UTC).isoformat())

    now = datetime(2026, 8, 3, 10, 0, tzinfo=MSK)
    titles = [item["title"] for item in build_on_this_day(storage, "alice", now)]

    assert "Поздний вечер" in titles, "годовщина не нашлась в свой день у человека"


def test_it_does_not_show_up_a_day_early(storage) -> None:
    """И не показывается ВТОРОГО — иначе мы просто сдвинули ошибку на день."""
    storage.ensure_user("alice")
    _seed(storage, "Поздний вечер", datetime(2025, 8, 2, 21, 30, tzinfo=UTC).isoformat())

    now = datetime(2026, 8, 2, 10, 0, tzinfo=MSK)
    titles = [item["title"] for item in build_on_this_day(storage, "alice", now)]

    assert "Поздний вечер" not in titles


def test_a_daytime_record_is_unaffected(storage) -> None:
    """Записи вне пограничного окна ведут себя как раньше."""
    storage.ensure_user("alice")
    _seed(storage, "Полдень", datetime(2025, 8, 3, 12, 0, tzinfo=UTC).isoformat())

    now = datetime(2026, 8, 3, 10, 0, tzinfo=MSK)
    titles = [item["title"] for item in build_on_this_day(storage, "alice", now)]

    assert "Полдень" in titles


def test_a_naive_moment_does_not_break_the_call(storage) -> None:
    """Время без пояса — не повод упасть: смещение просто нулевое."""
    storage.ensure_user("alice")
    _seed(storage, "Без пояса", datetime(2025, 8, 3, 12, 0, tzinfo=UTC).isoformat())

    titles = [
        item["title"] for item in build_on_this_day(storage, "alice", datetime(2026, 8, 3, 10, 0))
    ]
    assert "Без пояса" in titles
