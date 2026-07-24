"""Temporal semantics for event entities: dates, ranges, and timeline queries.

EVENT entities previously carried no time, RelationType.OCCURRED_AT was dead code,
and there were no timeline queries. These tests pin date parsing/normalization,
setting an event's occurrence time, the ordered/bounded timeline, best-effort
auto-extraction on ingestion, and the HTTP surface.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import (
    EVENT_TIME_RELATION,
    KnowledgeGraph,
    normalize_event_date,
    parse_event_date,
)
from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app
from jericho.storage.models import EntityType

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


def test_timeline_is_ordered_bounded_and_event_only(storage):
    graph = KnowledgeGraph(storage)
    launch = graph.create_entity("alice", "Launch", EntityType.EVENT)
    review = graph.create_entity("alice", "Review", EntityType.EVENT)
    person = graph.create_entity("alice", "Ivan", EntityType.PERSON)
    graph.set_event_time("alice", review["id"], "2024-09-01")
    graph.set_event_time("alice", launch["id"], "2024-06-12")
    # A non-event with a time row must never surface on the timeline.
    storage.set_entity_time(person["id"], "alice", "2024-07-01")

    full = graph.timeline("alice")
    assert [item["name"] for item in full] == ["Launch", "Review"]
    assert all(item["relation"] == "occurred_at" for item in full)

    windowed = graph.timeline("alice", start="2024-08-01", end="2024-12-31")
    assert [item["name"] for item in windowed] == ["Review"]


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
