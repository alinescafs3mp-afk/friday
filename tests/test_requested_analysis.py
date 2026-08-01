"""«Запрошенный анализ» — ответ о человеке, а не выдача строк, из которых его надо сложить.

Лента отвечает на вопрос о событиях, сводка — о количествах. Оба молча
предполагают, что смотрящий сам сложит картину; на аккаунте с девятьюстами
поступлениями это неправда.

Свойства, за которыми тут следят:

1. Сравнение двух окон меряет окна ОДНОЙ длины. Месяц против трёх месяцев
   истории показал бы обвал активности, который целиком арифметический.
2. «Что изменилось» без начала окна — это отказ, а не пустой результат: пустой
   разрез читается как «ничего не изменилось».
3. Ни одно число не выводится из длины показанного списка. Тем показывается
   `top`, а `topics_total` говорит, сколько их всего.
4. Уровень без содержимого не получает список тем: перечень тегов — это сжатый
   пересказ написанного, а не факт о деятельности.
"""

from __future__ import annotations

import hashlib

import pytest

from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id

NOW_START = "2026-07-15T00:00:00+00:00"
NOW_END = "2026-07-30T00:00:00+00:00"


def _arrival(storage, user_id: str, *, at: str, tags: list[str], content: str = "тело материала") -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(f"{at}{content}{tags}".encode()).hexdigest(),
        metadata_json={"knowledge_kind": "document", "size_bytes": len(content)},
    )
    storage.store_raw_object(raw)
    storage.execute("UPDATE raw_objects SET received_at=?, created_at=? WHERE id=?", (at, at, raw.id))
    item = InboxItem(
        id=new_id("inb"),
        user_id=user_id,
        raw_object_id=raw.id,
        status=InboxStatus.PENDING,
        suggested_tags_json=tags,
    )
    storage.store_inbox_item(item)
    storage.commit()
    return raw.id


@pytest.fixture
def worked(storage):
    """Прошлое окно — про смету. Текущее — про переезд. Одна тема сквозная."""
    storage.ensure_user("alice")
    for index in range(4):
        _arrival(storage, "alice", at=f"2026-07-0{index + 1}T09:00:00+00:00", tags=["смета", "склад"])
    for index in range(6):
        _arrival(storage, "alice", at=f"2026-07-2{index}T14:00:00+00:00", tags=["переезд", "склад"])
    return storage


def test_the_comparison_measures_two_windows_of_the_same_length(worked):
    change = worked.user_activity_analysis("alice", since=NOW_START, until=NOW_END, analyses=["change"])[
        "change"
    ]

    assert change["available"] is True
    assert change["previous_since"] == "2026-06-30T00:00:00+00:00", (
        "предыдущее окно не равно текущему по длине — сравнение мерило бы разное"
    )
    assert change["previous_until"] == NOW_START
    assert change["arrivals_now"] == 6
    assert change["arrivals_before"] == 4


def test_a_topic_that_only_appeared_now_is_named_as_new(worked):
    change = worked.user_activity_analysis("alice", since=NOW_START, until=NOW_END, analyses=["change"])[
        "change"
    ]

    assert "переезд" in change["new_topics"]
    assert "смета" in change["dropped_topics"]
    assert "склад" not in change["new_topics"], "сквозная тема названа новой"
    assert "склад" not in change["dropped_topics"]


def test_a_comparison_without_a_window_refuses_instead_of_returning_nothing(worked):
    change = worked.user_activity_analysis("alice", analyses=["change"])["change"]
    assert change["available"] is False
    assert change["reason"] == "since_required"


def test_the_topic_list_says_how_many_it_left_out(worked):
    """Число тем не должно выводиться из длины показанного списка."""
    analysis = worked.user_activity_analysis("alice", analyses=["topics"], top=1)
    assert len(analysis["topics"]) == 1
    assert analysis["topics_total"] == 3, (
        f"тем всего три (смета/склад/переезд), а отчёт говорит {analysis['topics_total']}"
    )


def test_the_topics_are_the_ones_actually_present(worked):
    analysis = worked.user_activity_analysis("alice", analyses=["topics"], top=10)
    counts = {row["topic"]: row["count"] for row in analysis["topics"]}
    assert counts == {"склад": 10, "переезд": 6, "смета": 4}
    assert analysis["kinds"] == [{"kind": "document", "count": 10}]


def test_volume_counts_days_the_person_actually_worked(worked):
    volume = worked.user_activity_analysis("alice", analyses=["volume"])["volume"]
    assert volume["arrivals"] == 10
    assert volume["active_days"] == 10, "десять поступлений в десять разных дней"
    assert volume["chars"] == 10 * len("тело материала")


def test_rhythm_reports_the_hours_and_weekdays_worked(worked):
    analysis = worked.user_activity_analysis("alice", analyses=["rhythm"])
    hours = {row["hour"]: row["count"] for row in analysis["by_hour"]}
    assert hours == {"09": 4, "14": 6}
    assert sum(row["count"] for row in analysis["by_weekday"]) == 10
    assert all(0 <= row["weekday"] <= 6 for row in analysis["by_weekday"])


def test_an_unknown_analysis_is_refused_by_name(worked):
    with pytest.raises(ValueError, match="Unknown analysis"):
        worked.user_activity_analysis("alice", analyses=["предсказание"])


# --- граница с уровнем без содержимого -------------------------------------


def test_the_metadata_tier_gets_the_rhythm_but_not_the_subject(worked):
    full = worked.user_activity_analysis("alice", since=NOW_START, until=NOW_END)
    redacted = worked.user_activity_analysis("alice", since=NOW_START, until=NOW_END, include_content=False)

    assert full["topics"] and full["kinds"]
    assert redacted["topics"] == [] and redacted["kinds"] == []
    assert redacted["topics_redacted"] is True

    # Ритм и объём — про деятельность, а не про её предмет: остаются.
    assert redacted["by_hour"] == full["by_hour"]
    assert redacted["volume"] == full["volume"]

    # И то же самое внутри сравнения окон, где список тем легко проглядеть.
    assert redacted["change"]["arrivals_now"] == full["change"]["arrivals_now"]
    assert redacted["change"]["new_topics"] == []
    blob = str(redacted)
    for subject in ("переезд", "смета", "склад"):
        assert subject not in blob, f"анализ без содержимого назвал тему {subject!r}"


def test_both_analysis_shapes_have_the_same_keys(worked):
    full = set(worked.user_activity_analysis("alice", since=NOW_START))
    redacted = set(worked.user_activity_analysis("alice", since=NOW_START, include_content=False))
    assert full == redacted


def test_the_route_carries_the_analysis_and_records_it(settings):
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        storage.ensure_user("subject", preset_key="user")
        _arrival(storage, "subject", at="2026-07-20T09:00:00+00:00", tags=["склад"])

        response = client.get(
            "/api/admin/users/subject/activity",
            params={"analysis": ["topics", "volume"], "since": NOW_START},
            headers=owner,
        )
        assert response.status_code == 200, response.text
        analysis = response.json()["analysis"]
        assert analysis["analyses"] == ["topics", "volume"]
        assert analysis["topics"] == [{"topic": "склад", "count": 1}]
        assert "change" not in analysis, "разрез, который не просили, не должен считаться"

        bad = client.get(
            "/api/admin/users/subject/activity",
            params={"analysis": ["гороскоп"]},
            headers=owner,
        )
        assert bad.status_code == 400
        assert "Unknown analysis" in bad.text
