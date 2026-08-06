"""Temporal semantics for event entities: dates, ranges, and timeline queries.

EVENT entities previously carried no time, RelationType.OCCURRED_AT was dead code,
and there were no timeline queries. These tests pin date parsing/normalization,
setting an event's occurrence time, the ordered/bounded timeline, best-effort
auto-extraction on ingestion, and the HTTP surface.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import (
    EVENT_TIME_RELATION,
    KnowledgeGraph,
    normalize_event_date,
    parse_event_date,
)
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app
from friday.storage._graph import _bounded_visible_timeline_event_rows, _count_visible_timeline_events
from friday.storage.models import Entity, EntityType, Relation, RelationType, utc_now

# --- pure date helpers ----------------------------------------------------


def test_parse_event_date_extracts_absolute_dates_only():
    assert parse_event_date("Встреча 2024-03-15 в офисе") == ("2024-03-15", "day")
    assert parse_event_date("дедлайн 05.11.2025") == ("2025-11-05", "day")
    assert parse_event_date("релиз 2024-06") == ("2024-06-01", "month")
    # Relative and invalid dates are ignored.
    assert parse_event_date("встреча завтра") is None
    assert parse_event_date("версия 99.99.9999") is None


def test_normalize_event_date_by_precision_and_rejects_garbage():
    assert normalize_event_date("2024") == ("2024-01-01", "year")
    assert normalize_event_date("2024-03") == ("2024-03-01", "month")
    assert normalize_event_date("2024-06-12") == ("2024-06-12", "day")
    with pytest.raises(ValueError):
        normalize_event_date("2024-13-40")
    with pytest.raises(ValueError):
        normalize_event_date("not-a-date")


# --- KG set/get/timeline --------------------------------------------------


def test_set_event_time_validates_type_dates_and_range(storage):
    graph = KnowledgeGraph(storage)
    person = graph.create_entity("alice", "Ivan", EntityType.PERSON)
    event = graph.create_entity("alice", "Launch", EntityType.EVENT)

    with pytest.raises(ValueError, match="event entities"):
        graph.set_event_time("alice", person["id"], "2024-06-12")
    with pytest.raises(ValueError):
        graph.set_event_time("alice", event["id"], "not-a-date")
    with pytest.raises(ValueError, match="occurred_end"):
        graph.set_event_time("alice", event["id"], "2024-06-12", occurred_end="2024-06-01")

    record = graph.set_event_time("alice", event["id"], "2024-06-12", occurred_end="2024-06-13")
    assert record["occurred_at"] == "2024-06-12"
    assert record["occurred_end"] == "2024-06-13"
    assert record["relation"] == EVENT_TIME_RELATION == "occurred_at"
    assert graph.get_event_time("alice", event["id"])["occurred_at"] == "2024-06-12"


def test_timeline_keeps_event_rows_ordered_bounded_and_compatible(storage):
    graph = KnowledgeGraph(storage)
    launch = graph.create_entity("alice", "Launch", EntityType.EVENT)
    review = graph.create_entity("alice", "Review", EntityType.EVENT)
    person = graph.create_entity("alice", "Ivan", EntityType.PERSON)
    graph.set_event_time("alice", review["id"], "2024-09-01")
    graph.set_event_time("alice", launch["id"], "2024-06-12")
    # A non-event with a time row must never surface on the timeline.
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, occurred_end, precision, source, updated_at)
               VALUES(?, ?, ?, NULL, 'day', 'corrupt-test-row', ?)""",
            (person["id"], "alice", "2024-07-01", utc_now()),
        )

    full = graph.timeline("alice")
    assert [item["name"] for item in full] == ["Launch", "Review"]
    assert all(item["relation"] == "occurred_at" for item in full)

    windowed = graph.timeline("alice", start="2024-08-01", end="2024-12-31")
    assert [item["name"] for item in windowed] == ["Review"]


def test_post_migration_source_only_reminder_rows_fail_closed_without_an_owner_marker(storage):
    storage.ensure_user("shared")
    storage.ensure_user("alice")
    event = Entity(
        id="ent-source-only-reminder",
        user_id="shared",
        name="PRIVATE SOURCE ONLY REMINDER SENTINEL",
        entity_type=EntityType.EVENT,
    )
    storage.create_entity(event)
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, occurred_end, precision, source, updated_at)
               VALUES(?, ?, '2026-08-05', NULL, 'day', 'reminder:alice', ?)""",
            (event.id, "shared", utc_now()),
        )

    assert _bounded_visible_timeline_event_rows(storage, "shared", "alice") == []
    assert _count_visible_timeline_events(storage, "shared", "alice") == 0
    assert storage.list_events_in_range("shared") == []
    assert storage.count_events_in_range("shared") == 0


def test_person_timeline_closes_foreign_private_carriers_and_keeps_exact_own_reminder(storage):
    for user_id in ("shared", "alice", "bob"):
        storage.ensure_user(user_id)

    bob_alias = "BOB-PRIVATE-TIMELINE-ALIAS-7f21"
    bob_private = Entity(
        id="ent-bob-private-timeline",
        user_id="bob",
        name="Bob private timeline authority",
        entity_type=EntityType.EVENT,
        aliases_json=[bob_alias],
    )
    direct_carrier = Entity(
        id="ent-shared-direct-private-carrier",
        user_id="shared",
        name="Shared direct timeline carrier",
        entity_type=EntityType.EVENT,
        description=f"Copied foreign identity: {bob_alias}",
    )
    transitive_carrier = Entity(
        id="ent-shared-transitive-private-carrier",
        user_id="shared",
        name="Shared transitive timeline carrier",
        entity_type=EntityType.EVENT,
        description=f"Copied carrier identity: {direct_carrier.name}",
    )
    public_event = Entity(
        id="ent-shared-public-timeline",
        user_id="shared",
        name="Shared public timeline event",
        entity_type=EntityType.EVENT,
    )
    alice_private = Entity(
        id="ent-alice-private-timeline",
        user_id="alice",
        name="Alice exact private timeline reminder",
        entity_type=EntityType.EVENT,
    )
    alice_legacy_private = Entity(
        id="ent-shared-alice-private-timeline",
        user_id="shared",
        name="Alice exact legacy private timeline reminder",
        entity_type=EntityType.EVENT,
    )
    for entity in (
        bob_private,
        direct_carrier,
        transitive_carrier,
        public_event,
        alice_private,
        alice_legacy_private,
    ):
        storage.create_entity(entity)

    with storage.transaction() as conn:
        for entity, occurred_at, source in (
            (bob_private, "2026-08-01", "reminder:bob"),
            (direct_carrier, "2026-08-02", "document:test"),
            (transitive_carrier, "2026-08-03", "document:test"),
            (public_event, "2026-08-04", "document:test"),
            (alice_private, "2026-08-05", "reminder:alice"),
            (alice_legacy_private, "2026-08-06", "reminder:alice"),
        ):
            conn.execute(
                """INSERT INTO entity_time(
                       entity_id, user_id, occurred_at, occurred_end, precision, source, updated_at)
                   VALUES(?, ?, ?, NULL, 'day', ?, ?)""",
                (entity.id, entity.user_id, occurred_at, source, utc_now()),
            )
        conn.executemany(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (
                (bob_private.id, "bob", utc_now()),
                (alice_private.id, "alice", utc_now()),
                (alice_legacy_private.id, "alice", utc_now()),
            ),
        )

    visible = _bounded_visible_timeline_event_rows(storage, "shared", "alice")
    assert [row["entity_id"] for row in visible] == [
        public_event.id,
        alice_private.id,
        alice_legacy_private.id,
    ]
    assert _count_visible_timeline_events(storage, "shared", "alice") == 3

    assert [row["entity_id"] for row in storage.list_events_in_range("shared")] == [public_event.id]
    assert storage.count_events_in_range("shared") == 1
    assert [row["entity_id"] for row in storage.list_events_in_range("alice")] == [alice_private.id]
    assert storage.count_events_in_range("alice") == 1


def test_timeline_page_unifies_events_and_relation_changes_under_one_limit(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Альфа", EntityType.PERSON)
    target = graph.create_entity("alice", "Бета", EntityType.ORGANIZATION)
    event = graph.create_entity("alice", "Совещание", EntityType.EVENT)
    graph.set_event_time("alice", event["id"], "2024-03-02")
    relation = graph.create_relation(
        "alice",
        source["id"],
        target["id"],
        RelationType.MEMBER_OF,
        metadata={"private": "RAW RELATION METADATA"},
        valid_from="2024-03-01",
    )
    graph.invalidate_relation(
        "alice",
        relation.id,
        valid_to="2024-03-03",
        reason="RAW INVALIDATION REASON",
    )

    page = graph.timeline_page(
        "alice",
        start="2024/3",
        end="2024-03-03",
        limit=2,
    )

    assert page["start"] == "2024-03-01"
    assert page["end"] == "2024-03-03"
    assert page["count"] == 2
    assert page["total"] == 3
    assert page["truncated"] is True
    assert [(item["kind"], item["at"], item["boundary"]) for item in page["items"]] == [
        ("relation", "2024-03-01", "confirmed"),
        ("event", "2024-03-02", "occurred_at"),
    ]
    confirmed = page["items"][0]
    assert confirmed == {
        "kind": "relation",
        "at": "2024-03-01",
        "boundary": "confirmed",
        "relation_id": relation.id,
        "relation_type": RelationType.MEMBER_OF.value,
        "source": {"id": source["id"], "name": "Альфа"},
        "target": {"id": target["id"], "name": "Бета"},
        "valid_from": "2024-03-01",
        "valid_to": "2024-03-03",
        "created_at": relation.created_at,
        "invalidated_at": confirmed["invalidated_at"],
        "superseded_by": None,
    }
    assert confirmed["invalidated_at"]
    assert "RAW RELATION METADATA" not in json.dumps(page, ensure_ascii=False)
    assert "RAW INVALIDATION REASON" not in json.dumps(page, ensure_ascii=False)

    whole = graph.timeline_page("alice", start="2024-03-01", end="2024-03-03", limit=10)
    assert whole["count"] == whole["total"] == 3
    assert whole["truncated"] is False
    assert whole["items"][1]["name"] == "Совещание"
    assert whole["items"][1]["relation"] == EVENT_TIME_RELATION
    assert whole["items"][2]["boundary"] == "ended"


def test_relation_timeline_is_stable_tenant_scoped_and_keeps_only_real_boundaries(storage):
    graph = KnowledgeGraph(storage)
    alice_source = graph.create_entity("alice", "Источник", EntityType.PERSON)
    alice_target = graph.create_entity("alice", "Цель", EntityType.ORGANIZATION)
    bob_source = graph.create_entity("bob", "Чужой источник", EntityType.PERSON)
    bob_target = graph.create_entity("bob", "Чужая цель", EntityType.ORGANIZATION)

    unknown_start = graph.create_relation(
        "alice",
        alice_source["id"],
        alice_target["id"],
        RelationType.WORKS_ON,
    )
    graph.invalidate_relation("alice", unknown_start.id, valid_to="2024-04-04")
    deleted = graph.create_relation(
        "alice",
        alice_source["id"],
        alice_target["id"],
        RelationType.MANAGES,
        valid_from="2024-04-01",
    )
    with storage.transaction() as conn:
        conn.execute("UPDATE relations SET deleted_at=? WHERE id=?", (utc_now(), deleted.id))
    graph.create_relation(
        "bob",
        bob_source["id"],
        bob_target["id"],
        RelationType.MANAGES,
        valid_from="2024-04-01",
    )

    first = graph.timeline_page("alice", start="2024-04", end="2024-04-30", limit=10)
    second = graph.timeline_page("alice", start="2024-04", end="2024-04-30", limit=10)

    assert first == second
    assert first["total"] == 1
    assert [(item["relation_id"], item["boundary"]) for item in first["items"]] == [
        (unknown_start.id, "ended")
    ]
    assert all(item["source"]["name"] != "Чужой источник" for item in first["items"])


def test_superseded_relation_id_is_hidden_if_the_replacement_becomes_private(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Публичный источник", EntityType.PERSON)
    old_target = graph.create_entity("alice", "Публичная старая цель", EntityType.ORGANIZATION)
    private_target = graph.create_entity(
        "alice",
        "PRIVATE REPLACEMENT TARGET SENTINEL",
        EntityType.EVENT,
    )
    original = graph.create_relation(
        "alice",
        source["id"],
        old_target["id"],
        RelationType.MANAGES,
        valid_from="2024-01-01",
    )
    replacement = Relation(
        id="rel-private-replacement-sentinel",
        user_id="alice",
        source_entity_id=source["id"],
        target_entity_id=private_target["id"],
        relation_type=RelationType.MANAGES,
        valid_from="2024-02-01",
    )
    storage.create_relation(replacement)
    graph.invalidate_relation(
        "alice",
        original.id,
        valid_to="2024-02-01",
        superseded_by=replacement.id,
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, ?, 'reminder', ?)""",
            (private_target["id"], "person-alice", utc_now()),
        )

    relations = graph.get_entity_relations(source["id"], "alice", as_of="2024-01-15")
    assert [item["id"] for item in relations] == [original.id]
    assert relations[0]["superseded_by"] is None
    page = graph.timeline_page("alice", start="2024-01-01", end="2024-02-01", limit=20)
    encoded = json.dumps(page, ensure_ascii=False)
    assert replacement.id not in encoded
    assert "PRIVATE REPLACEMENT TARGET SENTINEL" not in encoded


def test_timeline_ties_have_one_explicit_stable_cross_kind_order(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Альфа", EntityType.PERSON)
    target = graph.create_entity("alice", "Бета", EntityType.ORGANIZATION)
    event = graph.create_entity("alice", "Событие", EntityType.EVENT)
    graph.set_event_time("alice", event["id"], "2024-05-05")
    confirmed = graph.create_relation(
        "alice",
        source["id"],
        target["id"],
        RelationType.MANAGES,
        valid_from="2024-05-05",
    )
    ended = graph.create_relation(
        "alice",
        source["id"],
        target["id"],
        RelationType.WORKS_ON,
        valid_from="2024-01-01",
    )
    graph.invalidate_relation("alice", ended.id, valid_to="2024-05-05")

    page = graph.timeline_page("alice", start="2024-05-05", end="2024-05-05", limit=10)

    assert [
        (item["kind"], item["boundary"], item.get("entity_id") or item.get("relation_id"))
        for item in page["items"]
    ] == [
        ("event", "occurred_at", event["id"]),
        ("relation", "confirmed", confirmed.id),
        ("relation", "ended", ended.id),
    ]


def test_timeline_rejects_a_reversed_window_before_reading_storage(storage, monkeypatch):
    graph = KnowledgeGraph(storage)

    def must_not_read(*_args, **_kwargs):
        raise AssertionError("storage was read before the range was rejected")

    monkeypatch.setattr(storage, "list_events_in_range", must_not_read)
    monkeypatch.setattr(storage, "count_events_in_range", must_not_read)

    with pytest.raises(ValueError, match="end"):
        graph.timeline_page("alice", start="2025", end="2024")
    with pytest.raises(ValueError, match="Invalid date"):
        graph.timeline_page("alice", start="not-a-date")


# --- ingestion auto-extraction --------------------------------------------


@pytest.mark.asyncio
async def test_ingestion_stamps_a_single_event_with_its_date(settings, storage):
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(settings, storage, graph)

    result = await pipeline.ingest_text(
        "alice",
        "Запомни: конференция «DevConf» пройдёт 2024-06-12 в Москве.",
        source_ref="event:devconf",
    )
    assert result["promoted"] is True
    event_links = [
        link for link in result["graph_links"] if link.get("entity_type") == EntityType.EVENT.value
    ]
    assert event_links, result["graph_links"]
    recorded = graph.get_event_time("alice", event_links[0]["entity_id"])
    assert recorded is not None
    assert recorded["occurred_at"] == "2024-06-12"
    assert recorded["source"] == "ingestion"


# --- HTTP surface ---------------------------------------------------------


def test_timeline_and_set_time_over_http(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        event = app.state.kg.create_entity(LEGACY_OWNER_USER_ID, "Product Launch", EntityType.EVENT)
        person = app.state.kg.create_entity(LEGACY_OWNER_USER_ID, "Ivan", EntityType.PERSON)

        ok = client.post(
            f"/api/kg/entities/{event['id']}/time", json={"occurred_at": "2024-06-12"}, headers=owner
        )
        assert ok.status_code == 200
        assert ok.json()["event_time"]["occurred_at"] == "2024-06-12"

        # Non-event and invalid dates are rejected.
        assert (
            client.post(
                f"/api/kg/entities/{person['id']}/time",
                json={"occurred_at": "2024-06-12"},
                headers=owner,
            ).status_code
            == 400
        )
        assert (
            client.post(
                f"/api/kg/entities/{event['id']}/time",
                json={"occurred_at": "nope"},
                headers=owner,
            ).status_code
            == 400
        )

        timeline = client.get("/api/kg/timeline", headers=owner)
        assert timeline.status_code == 200
        names = [item["name"] for item in timeline.json()["items"]]
        assert names == ["Product Launch"]


def test_unified_timeline_page_is_exposed_over_http(settings):
    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        graph = app.state.kg
        source = graph.create_entity(LEGACY_OWNER_USER_ID, "Иван", EntityType.PERSON)
        target = graph.create_entity(LEGACY_OWNER_USER_ID, "Проект", EntityType.PROJECT)
        event = graph.create_entity(LEGACY_OWNER_USER_ID, "Приёмка", EntityType.EVENT)
        graph.set_event_time(LEGACY_OWNER_USER_ID, event["id"], "2024-03-02")
        relation = graph.create_relation(
            LEGACY_OWNER_USER_ID,
            source["id"],
            target["id"],
            RelationType.WORKS_ON,
            valid_from="2024-03-01",
        )
        graph.invalidate_relation(LEGACY_OWNER_USER_ID, relation.id, valid_to="2024-03-03")

        response = client.get(
            "/api/kg/timeline?start=2024.3&end=2024-03-03&limit=2",
            headers=owner,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["start"] == "2024-03-01"
        assert body["end"] == "2024-03-03"
        assert body["count"] == 2
        assert body["total"] == 3
        assert body["truncated"] is True
        assert [item["kind"] for item in body["items"]] == ["relation", "event"]

        reversed_window = client.get(
            "/api/kg/timeline?start=2025&end=2024",
            headers=owner,
        )
        assert reversed_window.status_code == 400
