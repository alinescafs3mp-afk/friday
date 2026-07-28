"""A caller's metadata may travel with a Raw Object; it may not impersonate the pipeline.

`ingest_text` spread the caller's dict LAST into `metadata_json`, so anything reaching
`POST /api/ingest/text` could replace `promotion_assessment` — the provenance block
`_replay_text_source` reads back to decide whether an ingestion is still in flight.

Two ways that bites, both reproduced before the fix:
  * a non-dict value made every later ingest of the same `source_ref` raise
    AttributeError out of an unguarded `.get(...).get(...)`, i.e. HTTP 500;
  * a forged `{"action": "promote"}` on content the pipeline judged transient left no
    Knowledge Object and no Inbox item, so the replay read "in progress" forever and
    that `source_ref` could never be ingested again.
"""

from __future__ import annotations

import json

import pytest

from jericho.ingestion import IngestionPipeline
from jericho.knowledge_graph import KnowledgeGraph


@pytest.fixture
def pipeline(settings, storage):
    return IngestionPipeline(settings, storage, KnowledgeGraph(storage))


def _raw_metadata(storage, raw_object_id: str) -> dict:
    row = storage.execute("SELECT metadata_json FROM raw_objects WHERE id=?", (raw_object_id,)).fetchone()
    return json.loads(row["metadata_json"])


@pytest.mark.asyncio
async def test_the_pipelines_own_provenance_survives_hostile_metadata(pipeline, storage):
    storage.ensure_user("alice")
    result = await pipeline.ingest_text(
        "alice",
        "Договор с подрядчиком подписан 14 марта, срок работ — до конца квартала.",
        source="api",
        source_ref="hostile-1",
        metadata={"promotion_assessment": "not-a-dict", "classification": "forged"},
    )
    stored = _raw_metadata(storage, result["raw_object_id"])
    assert isinstance(stored["promotion_assessment"], dict), "the caller overwrote provenance"
    assert stored["promotion_assessment"].get("action") in {"promote", "review", "transient"}
    assert stored["classification"] != "forged"


@pytest.mark.asyncio
async def test_replaying_that_source_ref_still_works(pipeline, storage):
    storage.ensure_user("alice")
    text = "Договор с подрядчиком подписан 14 марта, срок работ — до конца квартала."
    first = await pipeline.ingest_text(
        "alice", text, source="api", source_ref="hostile-2", metadata={"promotion_assessment": "not-a-dict"}
    )
    second = await pipeline.ingest_text("alice", text, source="api", source_ref="hostile-2")
    assert second["raw_object_id"] == first["raw_object_id"]
    assert second["idempotent_replay"] is True


@pytest.mark.asyncio
async def test_a_forged_promote_cannot_wedge_a_source_ref(pipeline, storage):
    """Transient content leaves no artifact; a forged 'promote' made that look in-flight."""
    storage.ensure_user("alice")
    text = "ок"
    first = await pipeline.ingest_text(
        "alice",
        text,
        source="api",
        source_ref="hostile-3",
        metadata={"promotion_assessment": {"action": "promote"}},
    )
    assert first["action"] == "transient"
    for _ in range(3):
        replay = await pipeline.ingest_text("alice", text, source="api", source_ref="hostile-3")
        assert replay["raw_object_id"] == first["raw_object_id"]


@pytest.mark.asyncio
async def test_a_row_written_before_the_fix_still_replays(pipeline, storage):
    """The second layer, for rows already on disk.

    Ordering the spread correctly stops new bad rows; it does nothing for the ones a
    live instance may already hold. A reader that assumes the block is a dict turns
    one of those into an unhandled AttributeError on every retry of that source_ref.
    """
    storage.ensure_user("alice")
    text = "Смета по объекту на Ленина, 14 — согласована в понедельник."
    first = await pipeline.ingest_text("alice", text, source="api", source_ref="legacy-1")
    storage.execute(
        "UPDATE raw_objects SET metadata_json=? WHERE id=?",
        (json.dumps({"promotion_assessment": "not-a-dict"}), first["raw_object_id"]),
    )
    storage.commit()

    replay = await pipeline.ingest_text("alice", text, source="api", source_ref="legacy-1")
    assert replay["raw_object_id"] == first["raw_object_id"]


@pytest.mark.asyncio
async def test_ordinary_caller_metadata_is_still_kept(pipeline, storage):
    """The point is provenance, not refusing the caller's own keys."""
    storage.ensure_user("alice")
    result = await pipeline.ingest_text(
        "alice",
        "Заметка о поставке оборудования на склад в четверг.",
        source="api",
        source_ref="ordinary-1",
        metadata={"telegram_message_id": 4242, "channel": "notes"},
    )
    stored = _raw_metadata(storage, result["raw_object_id"])
    assert stored["telegram_message_id"] == 4242
    assert stored["channel"] == "notes"
