"""The graph channel must be able to RANK, not just to vote.

A document's graph score was `max` over the entities that vouch for it, and the
root score of a seeded entity is a constant (`0.72 * link_confidence`). Measured
on a 342-document corpus, that made 83% of candidate scores collapse onto one of
two values, and a document sharing SIXTEEN entities with the query scored exactly
the same as one sharing a single hub entity. A channel that returns a constant
cannot order the results it admits.

Each additional shared entity is independent corroboration, so the rest fold in
noisy-or fashion on top of the best one, damped — entities linked to one document
co-occur rather than testify independently, and the same score also feeds the
`insufficient_evidence` gate, where inflation would readmit noise.
"""

from __future__ import annotations

import hashlib

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import Entity, KnowledgeObject, RawObject, new_id


def _make_ko(storage, user_id: str, content: str, *, title: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
    )
    storage.store_knowledge_object(ko)
    return ko.id


def _entity(storage, user_id: str, name: str) -> str:
    entity = Entity(id=new_id("ent"), user_id=user_id, name=name, entity_type="thing")
    storage.create_entity(entity)
    return entity.id


def _link(storage, user_id: str, knowledge_id: str, entity_id: str, confidence: float = 0.97) -> None:
    storage.link_knowledge_entity(
        user_id,
        knowledge_id,
        entity_id,
        status="accepted",
        confidence=confidence,
    )


@pytest.fixture
def graph_corpus(storage):
    """One query entity plus three others; documents differ ONLY in how many share."""
    storage.ensure_user("alice")
    query_entity = _entity(storage, "alice", "Атлас")
    others = [_entity(storage, "alice", name) for name in ("Москва", "Продакшн", "Дежурство")]

    many = _make_ko(storage, "alice", "Документ про многое.", title="Много связей")
    one = _make_ko(storage, "alice", "Документ про одно.", title="Одна связь")
    _link(storage, "alice", many, query_entity)
    for entity_id in others:
        _link(storage, "alice", many, entity_id)
    _link(storage, "alice", one, query_entity)
    return {"query_entity": query_entity, "many": many, "one": one, "others": others}


def test_more_shared_entities_outrank_one(storage, graph_corpus):
    kg = KnowledgeGraph(storage)
    context = kg.context_for_query("alice", "Атлас Москва Продакшн Дежурство", knowledge_limit=10)
    scores = {
        str(item["knowledge_object_id"]): float(item["score"]) for item in context["knowledge_candidates"]
    }
    assert graph_corpus["many"] in scores and graph_corpus["one"] in scores
    # The defect: these were EQUAL, so the channel could not order them.
    assert scores[graph_corpus["many"]] > scores[graph_corpus["one"]], (
        f"four shared entities scored {scores[graph_corpus['many']]}, "
        f"one shared entity {scores[graph_corpus['one']]}"
    )


def test_corroboration_saturates_below_one(storage, graph_corpus):
    """Corroboration must not let a pile of weak links out-argue a strong match."""
    kg = KnowledgeGraph(storage)
    context = kg.context_for_query("alice", "Атлас Москва Продакшн Дежурство", knowledge_limit=10)
    scores = [float(item["score"]) for item in context["knowledge_candidates"]]
    assert scores and max(scores) < 1.0


def test_a_lower_ranked_seed_vouches_for_less(storage, graph_corpus):
    """Seeds arrive in relevance order and that order has to mean something.

    Every seeded entity used to get a flat `0.72 * confidence`, and since most
    graph-scored documents in production are reached through seeds rather than
    through a query match, the channel handed a near-constant to a whole cluster.
    Measured on a 342-document corpus, 93% of returned graph scores were tied
    with another result of the same query; decaying by seed position took
    recall@10 from 131/198 to 149/198 and cut answers to nonsense queries by 11%.
    """
    # Two documents with DISJOINT entities, so each is reached only through its
    # own — otherwise both inherit the same entity's score and the seed order is
    # invisible, which is how the first version of this test passed on the old code.
    alpha_entity = _entity(storage, "alice", "Альфа")
    beta_entity = _entity(storage, "alice", "Бета")
    alpha = _make_ko(storage, "alice", "Первый документ.", title="Альфа-документ")
    beta = _make_ko(storage, "alice", "Второй документ.", title="Бета-документ")
    _link(storage, "alice", alpha, alpha_entity)
    _link(storage, "alice", beta, beta_entity)

    kg = KnowledgeGraph(storage)
    # Nothing matches the query, so both are reached ONLY as seeds — in the order
    # retrieval passes them, which is its own ranking.
    context = kg.context_for_query(
        "alice",
        "несуществующее слово",
        seed_knowledge_ids=[alpha, beta],
        knowledge_limit=20,
    )
    scores = {
        str(item["knowledge_object_id"]): float(item["score"]) for item in context["knowledge_candidates"]
    }
    assert scores[alpha] > scores[beta], (
        f"first seed {scores[alpha]}, last seed {scores[beta]} — seed order carries no weight"
    )


def test_a_single_link_scores_exactly_as_before(storage, graph_corpus):
    """The one-entity case is the old formula, unchanged: entity_score * confidence.

    Pins that corroboration only ever ADDS to documents that earned it, so the
    calibration of everything downstream (the 0.20 evidence threshold, the 0.16
    blend weight) still applies to the ordinary single-entity hit.
    """
    kg = KnowledgeGraph(storage)
    context = kg.context_for_query("alice", "Атлас", knowledge_limit=10)
    scores = {
        str(item["knowledge_object_id"]): float(item["score"]) for item in context["knowledge_candidates"]
    }
    # Only «Атлас» matches the query, so both documents are reached through it
    # alone and neither can corroborate: exact-mention confidence 0.99 x link 0.97.
    assert scores[graph_corpus["one"]] == pytest.approx(0.99 * 0.97, abs=1e-6)
    assert scores[graph_corpus["many"]] == pytest.approx(scores[graph_corpus["one"]], abs=1e-6)
