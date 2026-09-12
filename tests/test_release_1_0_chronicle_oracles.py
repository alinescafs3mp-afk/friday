"""Chronicle HTTP observes corpus history, personal events and local dates."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import EntityType
from tests.test_organs_profile_chronicle import _seed_knowledge
from tests.test_release_1_0_conversation_oracles import _body, _equal
from tests.test_release_1_0_profile_oracles import _A, _B, _GUEST, _state
from tests.test_release_1_0_profile_oracles import profile_http as profile_http

_NOW = datetime(2035, 9, 8, 0, 30, tzinfo=ZoneInfo("Europe/Moscow"))
_PATH = "/api/chronicle"


@pytest.mark.parametrize(
    ("now", "zone", "cutoff"),
    [
        ("2035-09-08T00:30:00", "America/Los_Angeles", "2035-09-07T07:30:00+00:00"),
        ("2024-03-10T12:30:00", "America/New_York", "2024-03-09T17:30:00+00:00"),
        ("2024-11-03T12:30:00", "America/New_York", "2024-11-02T16:30:00+00:00"),
    ],
    ids=["negative-offset", "spring-calendar-day", "autumn-calendar-day"],
)
def test_chronicle_recent_window_preserves_local_calendar_days_across_offsets(
    chronicle_http, monkeypatch, now, zone, cutoff
):
    from datetime import timedelta

    import friday.organs.chronicle as organ

    ctx = chronicle_http
    local = datetime.fromisoformat(now).replace(tzinfo=ZoneInfo(zone))
    monkeypatch.setattr(organ, "local_now", lambda settings: local)
    instant = datetime.fromisoformat(cutoff)
    inside = _knowledge(ctx, "Inside cutoff", (instant + timedelta(seconds=1)).isoformat())
    boundary = _knowledge(ctx, "Exact cutoff", cutoff)
    _knowledge(ctx, "Outside cutoff", (instant - timedelta(seconds=1)).isoformat())
    _assert_chronicle(
        ctx,
        {"window": {"days": 1, "recent_knowledge": [inside, boundary], "events": []}, "on_this_day": []},
        days=1,
    )


@pytest.fixture
def chronicle_http(profile_http, monkeypatch):
    import friday.organs.chronicle as organ

    def local_clock(settings):
        assert settings is profile_http["app"].state.settings
        return _NOW

    monkeypatch.setattr(organ, "local_now", local_clock)
    yield profile_http


def _chronicle_state(ctx):
    result = _state(ctx)
    for table in ("entity_time", "private_entity_owners", "outbound_notifications"):
        result[table] = [
            dict(r) for r in ctx["storage"].execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        ]
    return result


def _knowledge(ctx, title, timestamp, *, person=None):
    _seed_knowledge(ctx["storage"], person or ctx["corpus"], title, [], created_at=timestamp)
    return {"title": title, "kind": "note", "created_at": timestamp}


def _event(ctx, name, occurred_at, *, person=None, private=False):
    person = person or ctx["corpus"]
    graph = KnowledgeGraph(ctx["storage"])
    event = graph.create_entity(person, name, EntityType.EVENT, deduplicate=False)
    # Official storage accepts both dates and timestamps; timeline projection
    # explicitly supports both. No invalid SQL-only fixture is substituted.
    ctx["storage"].set_entity_time(
        event["id"],
        person,
        occurred_at,
        precision="minute" if "T" in occurred_at else "day",
        source=f"reminder:{person}" if private else "user",
    )
    return {"name": name, "occurred_at": occurred_at}


def _assert_chronicle(ctx, expected, *, person=_A, days=None):
    before = _chronicle_state(ctx)
    params = {"user_id": _B, "person_id": _B}
    if days is not None:
        params["days"] = days
    body = _body(ctx["client"].get(_PATH, headers=ctx[person], params=params))
    _equal(body, expected, "chronicle_exact_person_window")
    _equal(_chronicle_state(ctx), before, "chronicle_readonly")


def _seed_window(ctx):
    newer = _knowledge(ctx, "Новая запись", "2035-09-07T12:00:00+00:00")
    older = _knowledge(ctx, "Ранняя запись", "2035-09-02T12:00:00+00:00")
    _knowledge(ctx, "За пределом окна", "2035-08-30T12:00:00+00:00")
    _knowledge(ctx, "FOREIGN_CORPUS", "2035-09-07T13:00:00+00:00", person=_B)
    shared = _event(ctx, "Общее событие", "2035-09-01")
    own = _event(ctx, "Личное событие A", "2035-09-08", person=_A, private=True)
    other = _event(ctx, "Личное событие B", "2035-09-08", person=_B, private=True)
    _event(ctx, "Будущее событие", "2035-09-09")
    _event(ctx, "Старое событие", "2035-08-31")
    return newer, older, shared, own, other


def test_chronicle_preserves_shared_corpus_and_each_persons_private_calendar(chronicle_http):
    ctx = chronicle_http
    newer, older, shared, own, other = _seed_window(ctx)
    for person, private in ((_A, own), (_B, other)):
        _assert_chronicle(
            ctx,
            {
                "window": {"days": 7, "recent_knowledge": [newer, older], "events": [shared, private]},
                "on_this_day": [],
            },
            person=person,
        )
    _assert_chronicle(
        ctx, {"window": {"days": 1, "recent_knowledge": [newer], "events": [own]}, "on_this_day": []}, days=1
    )


def test_chronicle_recent_window_compares_utc_records_to_the_same_instant(chronicle_http):
    ctx = chronicle_http
    # Local Sept1 00:30 is Aug31 21:30UTC. Both boundary and +one second
    # belong in the seven-day window; -one second does not.
    included = _knowledge(ctx, "Внутри окна", "2035-08-31T21:30:01+00:00")
    boundary = _knowledge(ctx, "На границе", "2035-08-31T21:30:00+00:00")
    _knowledge(ctx, "Снаружи окна", "2035-08-31T21:29:59+00:00")
    _assert_chronicle(
        ctx,
        {"window": {"days": 7, "recent_knowledge": [included, boundary], "events": []}, "on_this_day": []},
    )


def test_chronicle_includes_a_timed_event_earlier_today_in_the_persons_calendar(chronicle_http):
    ctx = chronicle_http
    own = _event(ctx, "Сегодня в 00:10", "2035-09-08T00:10:00", person=_A, private=True)
    _assert_chronicle(
        ctx, {"window": {"days": 7, "recent_knowledge": [], "events": [own]}, "on_this_day": []}
    )


def test_chronicle_anniversaries_use_local_day_exact_order_and_five_item_limit(chronicle_http):
    ctx = chronicle_http
    memories = []
    labels = ("год назад", "2 года назад", "3 года назад", "4 года назад", "5 лет назад", "6 лет назад")
    for delta, label in enumerate(labels, 1):
        row = _knowledge(ctx, f"Память {delta}", f"{2035 - delta}-09-07T22:30:00+00:00")
        memories.append({"title": row["title"], "created_at": row["created_at"], "ago": label})
    _knowledge(ctx, "Вчера по Москве", "2034-09-07T20:59:59+00:00")
    _knowledge(ctx, "FOREIGN_ANNIVERSARY", "2034-09-07T22:31:00+00:00", person=_B)
    today = _knowledge(ctx, "Сегодняшняя запись", "2035-09-07T21:10:00+00:00")
    _assert_chronicle(
        ctx, {"window": {"days": 7, "recent_knowledge": [today], "events": []}, "on_this_day": memories[:5]}
    )


def test_chronicle_empty_history_is_an_exact_empty_result(chronicle_http):
    _assert_chronicle(
        chronicle_http,
        {"window": {"days": 365, "recent_knowledge": [], "events": []}, "on_this_day": []},
        days=365,
    )


def test_chronicle_fifty_item_limits_count_visible_rows_and_preserve_order(chronicle_http):
    ctx = chronicle_http
    knowledge, events = [], []
    for index in range(51):
        knowledge.append(_knowledge(ctx, f"Запись {index:02d}", f"2035-09-07T12:00:{index:02d}+00:00"))
        events.append(_event(ctx, f"Событие {index:02d}", "2035-09-07"))
    # This sorts ahead of visible events. Filtering after applying LIMIT would
    # consume one of the person's fifty slots and fail the exact response.
    _event(ctx, "A_FOREIGN_PRIVATE", "2035-09-07", person=_B, private=True)
    _assert_chronicle(
        ctx,
        {
            "window": {"days": 7, "recent_knowledge": list(reversed(knowledge))[:50], "events": events[:50]},
            "on_this_day": [],
        },
    )


def _assert_refusal(ctx, *, person, days, status):
    before = _chronicle_state(ctx)
    response = ctx["client"].get(_PATH, headers={} if person is None else ctx[person], params={"days": days})
    _equal(response.status_code, status, "chronicle_refusal_status")
    _equal(_chronicle_state(ctx), before, "chronicle_refusal_no_effect")
    assert "FOREIGN_CORPUS" not in response.text and "Личное событие" not in response.text


def test_chronicle_rejects_anonymous_denied_guest_and_invalid_windows_without_effects(chronicle_http):
    ctx = chronicle_http
    _seed_window(ctx)
    ctx["storage"].set_permission_override(_B, "chronicle.read", "deny")
    for person, days, status in (
        (None, 7, 401),
        (_B, 7, 403),
        (_GUEST, 7, 403),
        (_A, 0, 422),
        (_A, 366, 422),
        (_A, "1.5", 422),
        (_A, "bad", 422),
    ):
        _assert_refusal(ctx, person=person, days=days, status=status)


@pytest.mark.parametrize(
    "fault",
    ["foreign_event", "missing_record", "wrong_order", "read_write", "refusal_write", "wrong_anniversary"],
)
def test_chronicle_oracles_reject_corrupted_actual_http_and_persisted_effects(
    chronicle_http, monkeypatch, fault
):
    ctx = chronicle_http
    newer, older, shared, own, other = _seed_window(ctx)
    expected = {
        "window": {"days": 7, "recent_knowledge": [newer, older], "events": [shared, own]},
        "on_this_day": [],
    }
    original = ctx["client"].request

    def corrupted(method, url, **kwargs):
        response = original(method, url, **kwargs)
        if method == "GET" and url == _PATH:
            if fault in {"read_write", "refusal_write"}:
                _event(ctx, "UNEXPECTED_EVENT_WRITE", "2035-09-07")
            else:
                body = response.json()
                if fault == "foreign_event":
                    body["window"]["events"].append(other)
                elif fault == "missing_record":
                    body["window"]["recent_knowledge"].pop()
                elif fault == "wrong_order":
                    body["window"]["recent_knowledge"].reverse()
                elif fault == "wrong_anniversary":
                    body["on_this_day"] = [
                        {"title": "FOREIGN_ANNIVERSARY", "created_at": "2034-09-08", "ago": "год назад"}
                    ]
                response = httpx.Response(response.status_code, json=body)
        return response

    monkeypatch.setattr(ctx["client"], "request", corrupted)
    code = {"read_write": "chronicle_readonly", "refusal_write": "chronicle_refusal_no_effect"}.get(
        fault, "chronicle_exact_person_window"
    )
    with pytest.raises(AssertionError, match=code):
        if fault == "refusal_write":
            _assert_refusal(ctx, person=None, days=7, status=401)
        else:
            _assert_chronicle(ctx, expected)
