"""A page has to know what it is a page OF, and the total has to answer the same question.

Every admin list used to return `count = len(items)`, which on a full page equals the
limit and is indistinguishable from «that is all there is». Adding a total is the easy
half; the hard half is that the total must be computed from the SAME filters as the
listing. `count_knowledge_objects` counts every live object of the account while the
listing beside it filters by tag, lifecycle or entity — a pager built on that pair
would have reported thousands over a set of twelve and never reached its last page.

So the property these tests hold is not «there is a total» but «the total equals what
you get by walking every page», checked with the filters on.

Second property: paging is only trustworthy if the order is stable. Several of these
lists sort by columns written to second precision — one import stamps hundreds of rows
identically — so without a unique tail a row duplicates on one boundary and vanishes
on another. The tests walk two pages and demand every row exactly once.
"""

from __future__ import annotations

import hashlib

from jericho.storage.models import (
    Entity,
    EntityType,
    KnowledgeObject,
    RawObject,
    RelationType,
    new_id,
)


def _knowledge(storage, user_id: str, title: str, *, tags: list[str] | None = None) -> str:
    content = f"Содержимое для {title}"
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(f"{title}".encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
        tags_json=tags or [],
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _walk(fetch, total: int, page: int = 5) -> list[str]:
    """Every id the pages yield, in order, walking until the total is covered."""
    seen: list[str] = []
    offset = 0
    while offset < total:
        rows = fetch(page, offset)
        if not rows:
            break
        seen.extend(str(row["id"]) for row in rows)
        offset += page
    return seen


# --- знания: счётчик обязан уважать фильтры -------------------------------


def test_the_knowledge_total_respects_the_tag_filter(storage):
    storage.ensure_user("alice")
    for index in range(12):
        _knowledge(storage, "alice", f"Общая заметка {index}")
    for index in range(3):
        _knowledge(storage, "alice", f"Помеченная {index}", tags=["смета"])

    assert storage.count_filtered_knowledge_objects("alice") == 15
    assert storage.count_filtered_knowledge_objects("alice", tag="смета") == 3, (
        "the total ignored the filter the listing applies"
    )

    page = storage.list_knowledge_objects("alice", tag="смета", limit=50)
    assert len(page) == 3


def test_the_knowledge_total_respects_the_lifecycle_filter(storage):
    storage.ensure_user("alice")
    kept = _knowledge(storage, "alice", "Живая")
    archived = _knowledge(storage, "alice", "Архивная")
    storage.update_knowledge_fields(archived, "alice", lifecycle_stage="archived")

    assert storage.count_filtered_knowledge_objects("alice", lifecycle_stage="active") == 1
    rows = storage.list_knowledge_objects("alice", lifecycle_stage="active", limit=50)
    assert [row["id"] for row in rows] == [kept]


def test_walking_the_knowledge_pages_yields_each_object_once(storage):
    storage.ensure_user("alice")
    made = {_knowledge(storage, "alice", f"Заметка {index}") for index in range(13)}
    total = storage.count_filtered_knowledge_objects("alice")
    assert total == 13

    walked = _walk(
        lambda limit, offset: storage.list_knowledge_objects("alice", limit=limit, offset=offset), total
    )
    assert len(walked) == len(set(walked)), "a row was returned on two pages"
    assert set(walked) == made, "a row fell between pages"


# --- сущности: тумбстоуны и слитые не считаются ---------------------------


def test_the_entity_total_excludes_what_the_listing_excludes(storage):
    storage.ensure_user("alice")
    live = []
    for index in range(7):
        entity = Entity(
            id=new_id("ent"),
            user_id="alice",
            name=f"Сущность {index}",
            entity_type=EntityType.CONCEPT,
        )
        storage.create_entity(entity)
        live.append(entity.id)
    storage.soft_delete_entity(live.pop(), "alice")

    assert storage.count_entities("alice") == 6, "a tombstone was counted as a live entity"
    walked = _walk(lambda limit, offset: storage.list_entities("alice", limit=limit, offset=offset), 6)
    assert sorted(walked) == sorted(live)


def test_entities_with_the_same_name_still_page_cleanly(storage):
    """Namesakes are normal; without a unique tail their order is not promised."""
    storage.ensure_user("alice")
    made = set()
    for _ in range(11):
        entity = Entity(id=new_id("ent"), user_id="alice", name="Иванов", entity_type=EntityType.PERSON)
        storage.create_entity(entity)
        made.add(entity.id)

    total = storage.count_entities("alice")
    assert total == 11
    walked = _walk(lambda limit, offset: storage.list_entities("alice", limit=limit, offset=offset), total)
    assert len(walked) == len(set(walked)), "identical names duplicated across a page boundary"
    assert set(walked) == made


# --- входящие: тот же статус, что и у выборки ------------------------------


def test_the_inbox_total_matches_the_status_being_listed(storage, settings):
    from jericho.ingestion import IngestionPipeline
    from jericho.knowledge_graph import KnowledgeGraph
    from jericho.storage.models import InboxStatus

    storage.ensure_user("alice")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    import asyncio

    for index in range(9):
        asyncio.run(
            pipeline.ingest_text(
                "alice",
                f"Договор №{index} с подрядчиком подписан 14 марта, срок работ до конца квартала.",
                source="test",
                source_ref=f"src-{index}",
                force_review=True,
            )
        )

    pending = storage.count_inbox("alice", InboxStatus.PENDING)
    assert pending == 9
    assert storage.count_inbox("alice") == 9

    walked = _walk(
        lambda limit, offset: storage.list_inbox("alice", InboxStatus.PENDING, limit=limit, offset=offset),
        pending,
    )
    assert len(walked) == len(set(walked))
    assert len(walked) == 9


# --- кандидаты в связи: соединения тоже фильтры ---------------------------


def test_the_relation_candidate_total_counts_only_what_survives_the_joins(storage):
    storage.ensure_user("alice")
    entities = []
    for index in range(4):
        entity = Entity(
            id=new_id("ent"), user_id="alice", name=f"Узел {index}", entity_type=EntityType.CONCEPT
        )
        storage.create_entity(entity)
        entities.append(entity.id)

    for index in range(6):
        storage.store_relation_candidate(
            "alice",
            entities[index % 2],
            entities[2 + index % 2],
            RelationType.RELATED_TO.value,
            confidence=0.5 + index / 100,
            evidence={"n": index},
        )

    total = storage.count_relation_candidates("alice")
    listed = storage.list_relation_candidates("alice", limit=100)
    assert total == len(listed), "the count and the listing disagree on the join"

    walked = _walk(
        lambda limit, offset: storage.list_relation_candidates("alice", limit=limit, offset=offset),
        total,
        page=2,
    )
    assert len(walked) == len(set(walked))
    assert len(walked) == total


# --- то же самое, но через HTTP: маршрут обязан звать правильный счётчик ---


def test_the_knowledge_route_returns_a_total_that_respects_the_filter(settings):
    """The storage layer being right does not mean the route calls the right function.

    Exactly the mistake available here: `count_knowledge_objects` sits next to
    `count_filtered_knowledge_objects` and counts every live object of the account.
    Wiring the first one compiles, passes every storage test, and makes the pager
    claim thousands over a filtered set of twelve.
    """
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        storage = app.state.storage
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        user_id = client.get("/api/admin/users", headers=owner).json()["items"][0]["id"]
        for index in range(12):
            _knowledge(storage, user_id, f"Общая {index}")
        for index in range(3):
            _knowledge(storage, user_id, f"Помеченная {index}", tags=["смета"])

        everything = client.get(f"/api/admin/knowledge?user_id={user_id}&limit=5", headers=owner).json()
        assert everything["total"] == 15
        assert everything["count"] == 5, "the page itself is not paged"

        filtered = client.get(
            f"/api/admin/knowledge?user_id={user_id}&tag=%D1%81%D0%BC%D0%B5%D1%82%D0%B0&limit=5",
            headers=owner,
        ).json()
        assert filtered["total"] == 3, (
            f"the route reported {filtered['total']} for a filtered set of 3 — wrong counter"
        )


def test_every_paged_listing_orders_by_something_unique():
    """SQLite is free to reorder equal keys between two queries, and these lists sort
    on columns written to second precision — one import stamps hundreds identically.
    That freedom cannot be provoked deterministically in a test, so the property is
    pinned in the source instead: every paged ORDER BY ends in a unique column.

    Knowledge is the deliberate exception. `tests/test_storage_surface.py` pins its
    query plan to `idx_knowledge_user_importance` with no TEMP B-TREE, and a third
    sort column is not in that index — adding one would trade a measured 90 ms at
    10k objects for a boundary case. Fixing it properly needs a new index, which is
    its own decision.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "jericho" / "storage"
    paged = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"ORDER BY ([^\"']*?)LIMIT \? OFFSET \?", text, re.S):
            paged.append((path.name, " ".join(match.group(1).split())))

    assert paged, "no paged queries found — has the pattern changed?"
    unstable = [
        (name, clause)
        for name, clause in paged
        if not re.search(r"\b(id|rowid)\b\s*(ASC|DESC)?\s*$", clause.strip().rstrip(","))
        and "importance DESC" not in clause
    ]
    assert not unstable, f"paged queries without a unique sort tail: {unstable}"
