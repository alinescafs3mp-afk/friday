"""Writing a conflict must read back the conflict that was written.

``store_knowledge_conflict`` answered "what did I just write?" by listing up to 5000
conflicts and scanning them in Python. Two things were wrong with that, and only one of
them was performance.

The row is unique on ``(user_id, pair_key, conflict_type)``, but the scan matched on
``pair_key`` alone against a list ordered by confidence. With two conflict types about
the same pair it returned whichever was more confident — not the one just stored. The
caller then reported a contradiction it had not detected.

The cost was the other half: O(n) work on every write, growing, while conflict detection
runs once per promoted object. It got slower exactly as the knowledge base filled up.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from jericho.storage.models import KnowledgeObject, RawObject, new_id


def _knowledge(storage, title: str) -> str:
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="upload",
            source_ref=f"sha256:{new_id('x')}",
            raw_content=title,
            content_type="text/plain",
            content_hash=new_id("h") * 2,
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    return storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            entity_id=None,
            title=title,
            summary=title,
            content=title,
            knowledge_kind="note",
            importance=0.5,
            created_at=datetime.now(UTC).isoformat(),
        )
    ).id


def test_two_conflict_types_about_one_pair_stay_distinct(storage):
    """The defect, stated as the caller sees it.

    Both conflicts concern the same two objects. The second is stored with the LOWER
    confidence, so a lookup by pair alone — ordered by confidence — hands back the
    first one, and the caller believes it detected a contradiction rather than a
    duplicate.
    """
    storage.ensure_user("alice", source="upload")
    left, right = _knowledge(storage, "Встреча в марте"), _knowledge(storage, "Встреча в мае")

    contradiction = storage.store_knowledge_conflict(
        "alice", left, right, conflict_type="potential_contradiction", confidence=0.9
    )
    duplicate = storage.store_knowledge_conflict(
        "alice", left, right, conflict_type="near_duplicate", confidence=0.4
    )

    assert contradiction["conflict_type"] == "potential_contradiction"
    assert duplicate["conflict_type"] == "near_duplicate", (
        "storing a second conflict type returned the other conflict about the same pair"
    )
    assert contradiction["id"] != duplicate["id"]
    assert duplicate["confidence"] == 0.4


def test_the_written_row_comes_back_with_the_listing_shape(storage):
    """Callers must not be able to tell a freshly written conflict from a listed one."""
    storage.ensure_user("alice", source="upload")
    left, right = _knowledge(storage, "Проект Альфа"), _knowledge(storage, "Проект Бета")

    written = storage.store_knowledge_conflict("alice", left, right, confidence=0.7)
    listed = storage.list_knowledge_conflicts("alice", status=None)[0]

    assert set(written) == set(listed)
    assert written["id"] == listed["id"]
    assert written["knowledge_a_title"] and written["knowledge_b_title"]


def test_upsert_still_raises_confidence_and_reads_back_the_merged_row(storage):
    storage.ensure_user("alice", source="upload")
    left, right = _knowledge(storage, "Первый"), _knowledge(storage, "Второй")

    storage.store_knowledge_conflict("alice", left, right, confidence=0.5)
    merged = storage.store_knowledge_conflict("alice", left, right, confidence=0.8)

    assert merged["confidence"] == 0.8
    assert len(storage.list_knowledge_conflicts("alice", status=None)) == 1


def test_an_unknown_pair_reads_back_as_nothing(storage):
    storage.ensure_user("alice", source="upload")
    assert storage.get_knowledge_conflict_by_pair("alice", "no:such:pair", "x") == {}


def test_writing_a_conflict_does_not_get_slower_as_conflicts_accumulate(storage):
    """The scan was O(n) per write, so the thousandth conflict cost far more than the first.

    Timing is a blunt instrument, so the bar is deliberately loose: this fails on a
    return to list-and-scan, not on a slow machine.
    """
    storage.ensure_user("alice", source="upload")
    objects = [_knowledge(storage, f"Объект {index}") for index in range(60)]

    def write_batch(pairs) -> float:
        started = time.monotonic()
        for left, right in pairs:
            storage.store_knowledge_conflict("alice", left, right, confidence=0.5)
        return time.monotonic() - started

    first = [(objects[0], objects[index]) for index in range(1, 30)]
    early = write_batch(first)
    later = write_batch([(objects[1], objects[index]) for index in range(2, 31)])

    assert len(storage.list_knowledge_conflicts("alice", status=None, limit=5000)) == 58
    # With the scan, the second batch reads a table twice as long on every write.
    assert later < early * 3 + 0.05, f"write cost grew with table size: {early:.3f}s -> {later:.3f}s"
