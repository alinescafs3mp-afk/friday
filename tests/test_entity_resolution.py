from __future__ import annotations

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import EntityResolutionCandidate, EntityType, KnowledgeObject, RawObject, new_id


def _knowledge(storage, user_id: str):
    raw = RawObject(new_id("raw"), user_id, "test", new_id("ref"), "Alpha context", "text")
    storage.store_raw_object(raw)
    ko = KnowledgeObject(new_id("ko"), user_id, raw.id, content="Alpha context", title="Alpha")
    storage.store_knowledge_object(ko)
    return ko


def test_rejected_candidate_stays_rejected(storage):
    graph = KnowledgeGraph(storage)
    graph.create_entity("alice", "Project Alpha", EntityType.PROJECT, aliases=["Alpha"])
    graph.create_entity("alice", "Alpha Project", EntityType.PROJECT)
    candidates = graph.resolver.detect_duplicates("alice", min_confidence=0.25)
    assert candidates
    candidate = candidates[0]
    assert graph.resolver.reject_resolution(candidate.id, "alice", resolved_by="owner") is True
    assert graph.resolver.detect_duplicates("alice", min_confidence=0.25) == []
    stored = storage.get_resolution_candidate(candidate.id, "alice")
    assert stored and stored["status"] == "rejected"


def test_merge_preserves_links_relations_aliases_and_history(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Alpha Project", EntityType.PROJECT, aliases=["A Project"])
    target = graph.create_entity("alice", "Project Alpha", EntityType.PROJECT, aliases=["Alpha"])
    person = graph.create_entity("alice", "Ivan Petrov", EntityType.PERSON)
    ko = _knowledge(storage, "alice")
    graph.link_knowledge_to_entity(ko.id, source["id"], "alice")
    graph.create_relation("alice", source["id"], person["id"])
    candidate = EntityResolutionCandidate(
        id=new_id("er"),
        user_id="alice",
        entity_a_id=source["id"],
        entity_b_id=target["id"],
        confidence=0.9,
        resolution_method="test",
    )
    storage.store_resolution_candidate(candidate)

    merged = graph.resolver.accept_resolution(
        candidate.id,
        "alice",
        target_entity_id=target["id"],
        resolved_by="owner",
    )
    assert merged["target_entity_id"] == target["id"]
    old = storage.get_entity(source["id"], "alice")
    current = storage.get_entity(target["id"], "alice")
    assert old and old["merged_into_id"] == target["id"] and old["canonical"] == 0
    assert current and "Alpha Project" in current["aliases_json"]
    links = storage.list_knowledge_entity_links("alice", entity_id=target["id"])
    assert any(link["knowledge_object_id"] == ko.id for link in links)
    relations = storage.get_entity_relations(target["id"], "alice")
    assert any(rel["target_entity_id"] == person["id"] for rel in relations)
    history = storage.list_merge_history("alice")
    assert history and history[0]["source_entity_id"] == source["id"]


def test_compact_identifiers_are_exact_match_only(storage):
    graph = KnowledgeGraph(storage)
    graph.create_entity("alice", "BRK.A", EntityType.OTHER)
    graph.create_entity("alice", "BRK.B", EntityType.OTHER)
    graph.create_entity("alice", "BRNQ26", EntityType.OTHER)
    graph.create_entity("alice", "BRNQ27", EntityType.OTHER)

    assert graph.resolver.detect_duplicates("alice", min_confidence=0.0) == []
    assert storage.find_entity_by_name("alice", "BRK.A")["name"] == "BRK.A"
    assert storage.find_entity_by_name("alice", "BRK.B")["name"] == "BRK.B"


def test_accepted_relation_candidate_is_not_reopened_by_rediscovery(storage):
    graph = KnowledgeGraph(storage)
    source = graph.create_entity("alice", "Orion", EntityType.PROJECT)
    target = graph.create_entity("alice", "PostgreSQL", EntityType.CONCEPT)
    candidate = storage.store_relation_candidate(
        "alice",
        source["id"],
        target["id"],
        "uses",
        confidence=0.7,
        evidence={"source": "first-pass"},
    )
    accepted = graph.review_relation_candidate(
        "alice",
        candidate["id"],
        "accepted",
        reviewed_by="alice",
    )
    assert accepted and accepted["status"] == "accepted"

    rediscovered = storage.store_relation_candidate(
        "alice",
        source["id"],
        target["id"],
        "uses",
        confidence=0.79,
        evidence={"source": "later-pass"},
    )

    assert rediscovered["id"] == candidate["id"]
    assert rediscovered["status"] == "accepted"
    assert len(storage.get_entity_relations(source["id"], "alice")) == 1


def test_pending_resolutions_are_enriched_for_review(storage):
    graph = KnowledgeGraph(storage)
    left = graph.create_entity("alice", "Ivan Petrov", EntityType.PERSON)
    right = graph.create_entity("alice", "Ivan Petroff", EntityType.PERSON)
    assert left["id"] != right["id"]
    ko = _knowledge(storage, "alice")
    graph.link_knowledge_to_entity(ko.id, left["id"], "alice")
    candidate = EntityResolutionCandidate(
        id=new_id("er"),
        user_id="alice",
        entity_a_id=left["id"],
        entity_b_id=right["id"],
        confidence=0.96,
        resolution_method="test",
    )
    storage.store_resolution_candidate(candidate)

    pending = graph.resolver.get_pending_resolutions("alice")
    assert len(pending) == 1
    item = pending[0]
    assert item["id"] == candidate.id
    assert item["entity_a"]["name"] == "Ivan Petrov"
    assert item["entity_b"]["name"] == "Ivan Petroff"
    assert item["entity_a"]["knowledge_count"] == 1
    assert item["recommendation"] == "strong_merge_candidate"


# --- duplicate detection at corpus scale ----------------------------------


def _seed_entities(storage, names: list[str], *, user_id: str = "owner") -> None:
    from friday.storage.models import new_id, utc_now

    storage.ensure_user(user_id)
    now = utc_now()
    with storage.transaction() as conn:
        for name in names:
            conn.execute(
                "INSERT INTO entities(id,user_id,name,normalized_name,entity_type,description,"
                "aliases_json,metadata_json,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id("ent"),
                    user_id,
                    name,
                    name.strip().casefold(),
                    "project",
                    "",
                    "[]",
                    "{}",
                    1,
                    now,
                    now,
                ),
            )


def _exhaustive(storage, *, min_confidence: float) -> list[tuple[str, str, float, str]]:
    """The pre-blocking all-pairs scan, kept as an oracle.

    Setting the short-name threshold beyond any real name puts every entity in the
    one exhaustive bucket, which is exactly the original algorithm.
    """
    import friday.storage._knowledge as module

    saved = module._SHORT_NAME_CHARS
    module._SHORT_NAME_CHARS = 10_000
    try:
        return [
            (c.entity_a_id, c.entity_b_id, round(c.confidence, 9), c.resolution_method)
            for c in storage.find_duplicate_candidates("owner", min_confidence=min_confidence)
        ]
    finally:
        module._SHORT_NAME_CHARS = saved


def test_blocking_proposes_exactly_what_the_exhaustive_scan_proposed(storage):
    """Blocking is an optimisation. If it changes the proposals it is a bug.

    The scan was quadratic in entity count with three-plus SequenceMatcher calls per
    surviving pair, and it runs from an agent tool call and two HTTP routes as well
    as a worker: measured at 2000 entities, **94 seconds** on the event loop.

    Candidate pairs now come from blocking keys — a shared alias, a shared word, a
    matching acronym, a shared character bigram — chosen so that any pair capable of
    reaching the threshold shares at least one. Trigrams were tried first and lost
    2% of the proposals («Орион» vs «Орион2 1», «ООО 24» vs «ОСОО 40»), which is why
    the keys are bigrams and short names keep an exhaustive bucket as well.
    """
    names = [
        "Проект Орион",
        "Орион Проект",
        "Орион",
        "Орин",
        "Орион2",
        "Атлас Инфраструктура",
        "Инфраструктура Атлас",
        "Атлас",
        "Атлаc",
        "Общество С Ограниченной Ответственностью",
        "ОСОО",
        "ООО",
        "BRK.A",
        "BRK.B",
        "v1.2.3",
        "v1.2.4",
        "Ким",
        "Ли",
        "Ын",
        "Ан",
        "Сервис Уведомлений",
        "Сервис Уведомленй",
        "Служба Уведомлений",
        "Kubernetes",
        "Kubernets",
        "K8s",
    ]
    _seed_entities(
        storage,
        [
            f"{name} {index // len(names)}" if index >= len(names) else name
            for index, name in enumerate(names * 8)
        ],
    )

    for threshold in (0.5, 0.55, 0.6):
        blocked = [
            (c.entity_a_id, c.entity_b_id, round(c.confidence, 9), c.resolution_method)
            for c in storage.find_duplicate_candidates("owner", min_confidence=threshold)
        ]
        oracle = _exhaustive(storage, min_confidence=threshold)
        assert sorted(blocked) == sorted(oracle), (
            f"threshold {threshold}: blocking changed the proposal set "
            f"(missing {len(set(map(lambda t: t[:2], oracle)) - set(map(lambda t: t[:2], blocked)))})"
        )
    assert oracle, "the fixture produced no duplicates, so this proves nothing"


def test_the_pair_ceiling_is_announced_rather_than_silent(storage, caplog):
    """A short list must not be mistaken for "nothing left to merge".

    The scan is bounded so an HTTP route gets an answer instead of an eventual one,
    and the bound drops the flimsiest evidence first — but a reviewer reading a
    truncated list has no way to know it was truncated unless it is said.
    """
    import logging

    import friday.storage._knowledge as module

    _seed_entities(storage, [f"Сервис Уведомлений {index}" for index in range(120)])
    saved = module._MAX_DUPLICATE_PAIRS
    module._MAX_DUPLICATE_PAIRS = 50
    try:
        with caplog.at_level(logging.WARNING, logger="friday.storage"):
            storage.find_duplicate_candidates("owner", min_confidence=0.55)
    finally:
        module._MAX_DUPLICATE_PAIRS = saved
    assert any("PARTIAL" in record.message for record in caplog.records), caplog.text
