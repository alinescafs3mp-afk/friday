"""Четыре числа в одной панели обязаны отвечать на один и тот же вопрос.

Экран активности показывает «Поступлений / Знаний / В Inbox / Сообщений» рядом,
и над ними кнопки периода. `arrivals` считался по окну, а остальные три — за всё
время. Нажатие «7 дней» двигало одну карточку из четырёх, и три оставшихся
читались как «столько он сделал за неделю», хотя отвечали «за всё время».

Отличить это по самому числу нельзя: у нового аккаунта «за неделю» и «за всё
время» совпадают, и расхождение проявляется ровно тогда, когда надзор становится
осмысленным — на аккаунте с историей.

Второе свойство здесь — гистограмма по дням не должна молча обрываться, стоя
рядом с точным `arrivals`.

Третье — режим без содержимого не должен протекать.
"""

from __future__ import annotations

import hashlib

import pytest

from jericho.storage.models import KnowledgeObject, RawObject, new_id

OLD = "2026-01-10T09:00:00+00:00"
NEW = "2026-07-20T09:00:00+00:00"
WINDOW = "2026-07-01T00:00:00+00:00"


def _arrival(storage, user_id: str, *, at: str, content: str, filename: str = "") -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload" if filename else "telegram",
        source_ref=f"/home/{user_id}/секретный-путь/{new_id('f')}/{filename or 'note'}",
        raw_content=content,
        content_type="file" if filename else "text",
        content_hash=hashlib.sha256(f"{at}{content}".encode()).hexdigest(),
        metadata_json={"filename": filename, "size_bytes": len(content)} if filename else {},
    )
    storage.store_raw_object(raw)
    storage.execute("UPDATE raw_objects SET received_at=?, created_at=? WHERE id=?", (at, at, raw.id))
    storage.commit()
    return raw.id


def _knowledge(storage, user_id: str, raw_id: str, *, at: str, title: str) -> str:
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw_id,
        content="тело",
        content_type="text",
        title=title,
    )
    storage.store_knowledge_object(ko)
    storage.execute("UPDATE knowledge_objects SET created_at=? WHERE id=?", (at, ko.id))
    storage.commit()
    return ko.id


@pytest.fixture
def история(storage):
    """Аккаунт с прошлым: половина материала до окна, половина внутри."""
    storage.ensure_user("alice")
    for index, at in ((0, OLD), (1, OLD), (2, NEW)):
        raw = _arrival(storage, "alice", at=at, content=f"Заметка {index}")
        _knowledge(storage, "alice", raw, at=at, title=f"Заголовок {index}")
    return storage


def test_every_card_in_the_panel_counts_the_same_window(история):
    everything = история.user_activity_summary("alice")
    windowed = история.user_activity_summary("alice", since=WINDOW)

    assert everything["arrivals"] == 3 and everything["knowledge_objects"] == 3

    assert windowed["arrivals"] == 1
    assert windowed["knowledge_objects"] == 1, (
        f"«Знаний» показало {windowed['knowledge_objects']} за окно, в котором одно поступление — "
        "это число за всё время, стоящее рядом с оконным"
    )


def test_the_inbox_card_counts_the_same_window(storage):
    storage.ensure_user("alice")
    from jericho.storage.models import InboxItem, InboxStatus

    for at in (OLD, NEW):
        raw = _arrival(storage, "alice", at=at, content=f"Требует решения {at}")
        item = InboxItem(id=new_id("inb"), user_id="alice", raw_object_id=raw, status=InboxStatus.PENDING)
        storage.store_inbox_item(item)
        storage.execute("UPDATE inbox SET created_at=? WHERE id=?", (at, item.id))
    storage.commit()

    assert storage.user_activity_summary("alice")["pending_inbox"] == 2
    assert storage.user_activity_summary("alice", since=WINDOW)["pending_inbox"] == 1


def test_a_clipped_day_histogram_says_so(история, monkeypatch):
    """Обрезка обязана быть видна, а не выводиться из длины списка."""
    import jericho.storage._oversight as oversight

    monkeypatch.setattr(oversight, "_DAY_BUCKETS", 1)
    summary = история.user_activity_summary("alice")

    assert len(summary["by_day"]) == 1
    assert summary["by_day_days"] == 2, "истинное число дней потерялось вместе с обрезкой"
    assert summary["by_day_truncated"] is True
    assert sum(day["count"] for day in summary["by_day"]) < summary["arrivals"]


def test_a_complete_day_histogram_does_not_claim_truncation(история):
    summary = история.user_activity_summary("alice")
    assert summary["by_day_truncated"] is False
    assert sum(day["count"] for day in summary["by_day"]) == summary["arrivals"], (
        "бары и итог разошлись, хотя обрезки не было"
    )


# --- режим без содержимого --------------------------------------------------


def test_the_metadata_view_carries_nothing_the_person_wrote(storage):
    storage.ensure_user("alice")
    _arrival(storage, "alice", at=NEW, content="Зарплата Иванова 145000 рублей", filename="ведомость.xlsx")

    full = storage.user_activity("alice")[0]
    assert "145000" in full["preview"] and full["filename"] == "ведомость.xlsx"

    redacted = storage.user_activity("alice", include_content=False)[0]
    blob = " ".join(str(value) for value in redacted.values())
    for secret in ("145000", "Иванова", "ведомость", "секретный-путь"):
        assert secret not in blob, f"режим без содержимого протёк: в ответе осталось {secret!r}"


def test_the_metadata_view_still_answers_who_when_and_how_much(storage):
    storage.ensure_user("alice")
    _arrival(storage, "alice", at=NEW, content="Зарплата Иванова 145000 рублей", filename="ведомость.xlsx")

    redacted = storage.user_activity("alice", include_content=False)[0]
    assert redacted["at"].startswith("2026-07-20")
    assert redacted["activity"] == "upload"
    assert redacted["source"] == "upload"
    assert redacted["content_chars"] == len("Зарплата Иванова 145000 рублей")
    assert redacted["redacted"] is True


def test_both_views_have_the_same_keys(storage):
    """Одинаковая форма — чтобы забытая проверка флага дала пустую ячейку, а не тело."""
    storage.ensure_user("alice")
    _arrival(storage, "alice", at=NEW, content="Что-то", filename="файл.pdf")

    full = set(storage.user_activity("alice")[0])
    redacted = set(storage.user_activity("alice", include_content=False)[0])
    assert full - redacted == set(), f"в полном виде есть поля, которых нет в урезанном: {full - redacted}"
