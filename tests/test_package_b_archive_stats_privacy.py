"""Exact archive aggregates keep page, tenant, and quarantine boundaries."""

from __future__ import annotations

import threading

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import (
    Entity,
    EntityType,
    KnowledgeObject,
    RawObject,
    Relation,
    RelationType,
    new_id,
)


def _raw(storage, user_id: str, label: str, *, file: bool = False) -> RawObject:
    storage.ensure_user(user_id)
    return storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id=user_id,
            source="synthetic",
            source_ref=new_id("ref"),
            raw_content=label,
            content_type="file" if file else "text",
        )
    )


def _knowledge(storage, raw: RawObject, label: str, *, entity_id: str | None = None) -> None:
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id=raw.user_id,
            raw_object_id=raw.id,
            entity_id=entity_id,
            content=label,
            title=label,
        )
    )


def _quarantine_for_another_person(storage, entity: Entity) -> None:
    with storage.transaction() as connection:
        connection.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, ?, '2026-08-10T09:00:00Z', 'day',
                      'reminder:synthetic-other-person', '2026-08-08T00:00:00Z')""",
            (entity.id, entity.user_id),
        )
        connection.execute(
            """INSERT INTO private_entity_owners(
                   entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'synthetic-other-person', 'reminder', '2026-08-08T00:00:00Z')""",
            (entity.id,),
        )


def test_raw_and_file_totals_are_exact_beyond_a_typical_page_and_tenant_isolated(storage) -> None:
    for index in range(137):
        _raw(storage, "tenant-a", f"file-a-{index}", file=True)
    for index in range(3):
        _raw(storage, "tenant-b", f"file-b-{index}", file=True)
    _raw(storage, "tenant-a", "plain note")
    storage.commit()

    graph = KnowledgeGraph(storage)
    first = graph.get_stats("tenant-a")
    second = graph.get_stats("tenant-b")

    assert first["raw_object_count"] == 138
    assert first["file_count"] == 137
    assert second["raw_object_count"] == 3
    assert second["file_count"] == 3


def test_every_published_aggregate_excludes_a_quarantined_dependency(storage) -> None:
    storage.ensure_user("tenant-a")
    public_entity = storage.create_entity(
        Entity(new_id("ent"), "tenant-a", "Synthetic public node", EntityType.PROJECT)
    )
    private_entity = storage.create_entity(
        Entity(new_id("ent"), "tenant-a", "SYNTHETIC PRIVATE NODE 8d0c", EntityType.EVENT)
    )
    public_raw = _raw(storage, "tenant-a", "public note")
    _raw(storage, "tenant-a", "public file", file=True)
    private_file = _raw(storage, "tenant-a", "private source", file=True)
    _knowledge(storage, public_raw, "public knowledge")
    _knowledge(storage, private_file, "private knowledge", entity_id=private_entity.id)
    storage.create_relation(
        Relation(
            new_id("rel"),
            "tenant-a",
            private_entity.id,
            public_entity.id,
            RelationType.RELATED_TO,
        )
    )
    _quarantine_for_another_person(storage, private_entity)
    storage.commit()

    stats = KnowledgeGraph(storage).get_stats("tenant-a")

    assert stats["knowledge_object_count"] == 1
    assert stats["raw_object_count"] == 2
    assert stats["file_count"] == 1
    assert stats["entity_count"] == 1
    assert stats["relation_count"] == 0


def test_all_published_stats_come_from_one_wal_snapshot(storage, monkeypatch) -> None:
    user_id = "tenant-snapshot"
    storage.ensure_user(user_id)
    storage.commit()
    graph = KnowledgeGraph(storage)
    original_count_entities = storage.count_entities
    writer_errors: list[BaseException] = []
    wrote = False

    def commit_after_the_first_counter(*args, **kwargs):
        nonlocal wrote
        result = original_count_entities(*args, **kwargs)
        if wrote:
            return result
        wrote = True

        def writer() -> None:
            try:
                raw = _raw(storage, user_id, "concurrent synthetic material")
                _knowledge(storage, raw, "concurrent synthetic knowledge")
                storage.commit()
            except BaseException as exc:  # pragma: no cover - surfaced below
                writer_errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        return result

    monkeypatch.setattr(storage, "count_entities", commit_after_the_first_counter)

    during_commit = graph.get_stats(user_id)
    after_commit = graph.get_stats(user_id)

    assert writer_errors == []
    assert during_commit["knowledge_object_count"] == 0
    assert during_commit["raw_object_count"] == 0
    assert after_commit["knowledge_object_count"] == 1
    assert after_commit["raw_object_count"] == 1
    assert storage.conn.in_transaction is False
