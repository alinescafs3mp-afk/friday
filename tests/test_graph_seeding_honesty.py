"""A question the archive cannot answer must get an empty answer.

The graph used to be seeded from the top eight lexical hits with no floor at
all, and `_lexical_rank` scores every candidate — so eight seeds existed for any
question, including one about a subject the archive has never heard of. A graph
score of 0.677 clears `insufficient_evidence` on its own, so those seeds turned
"I have nothing on this" into ten of the owner's unrelated personal documents.
An empty result is the only way this system can say it does not know something.

The filter was written, measured and REVERTED once, because it broke the case
the graph exists for: «Казань» against «в Казани» scored 0.0593 lexically, under
the floor, and reached the answer only by seeding the graph and being vouched
for by its own entities. Morphology (see `test_morphology`) lifted that pair to
0.3317, and only then could the seeds be filtered honestly. The order was the
whole point, so both halves are pinned here.
"""

from __future__ import annotations

import hashlib

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.retrieval import HybridSearcher
from friday.storage.models import Entity, KnowledgeObject, RawObject, new_id


def _seed_corpus(storage) -> dict[str, str]:
    storage.ensure_user("alice")
    ids: dict[str, str] = {}
    documents = {
        "kazan": ("Поездка", "Поездка в Казани прошла хорошо, встретились с филиалом."),
        "duty": ("График дежурств", "График дежурств караула на месяц, ответственный по смене."),
        "vpn": ("Конфигурация", "Конфигурация подписки и чёрные списки адресов."),
    }
    for key, (title, content) in documents.items():
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("source"),
            raw_content=content,
            content_type="text",
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        ko = KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            content=content,
            content_type="text",
            title=title,
        )
        storage.store_knowledge_object(ko)
        ids[key] = ko.id

    # Entities and accepted links, so the graph has something to expand through —
    # without them the seeding question could not arise at all.
    for name, key in (("Казань", "kazan"), ("Караул", "duty"), ("Подписка", "vpn")):
        entity = Entity(id=new_id("ent"), user_id="alice", name=name, entity_type="thing")
        storage.create_entity(entity)
        storage.link_knowledge_entity("alice", ids[key], entity.id, status="accepted", confidence=0.97)
    return ids


@pytest.mark.asyncio
async def test_a_subject_the_archive_never_heard_of_returns_nothing(storage):
    _seed_corpus(storage)
    searcher = HybridSearcher(storage, None, record_usage=False)
    kg = KnowledgeGraph(storage)
    for question in (
        "гидропоника питательный раствор",
        "квинтэссаж парогубница",
        "sourdough hydration schedule",
    ):
        result = await searcher.search("alice", question, limit=10, kg=kg, graph_expansion=True)
        assert result["count"] == 0, (
            f"{question!r} answered with {result['count']} unrelated document(s): "
            f"{[item.get('title') for item in result['results']]}"
        )


@pytest.mark.asyncio
async def test_letter_similarity_is_not_evidence(storage):
    """Invented words that merely LOOK Russian must not clear the evidence gate.

    `lexical_vector` mixes word features with character trigrams, and trigrams
    are what let a document score without sharing a single word: measured on the
    real corpus, «переквантовать сизиморбность» reached 0.081–0.086 against
    ordinary documents — above the 0.075 floor — purely on letter overlap. Those
    documents then passed the gate and the graph's flat 0.677 lifted them into
    the answer. Trigrams earn their place in ranking, where approximate is
    useful; in a gate that decides whether there is any evidence at all they are
    noise wearing the shape of a score.
    """
    _seed_corpus(storage)
    searcher = HybridSearcher(storage, None, record_usage=False)
    kg = KnowledgeGraph(storage)
    for invented in ("переквантовать сизиморбность", "конфигурационность дежурственный"):
        result = await searcher.search("alice", invented, limit=10, kg=kg, graph_expansion=True, explain=True)
        assert result["count"] == 0, (
            f"{invented!r} returned {result['count']}: "
            f"{[(i.get('title'), i.get('_lexical_score')) for i in result['results']]}"
        )


@pytest.mark.asyncio
async def test_a_word_shared_with_the_document_still_counts(storage):
    """The other half: a real shared word must keep working as evidence."""
    ids = _seed_corpus(storage)
    searcher = HybridSearcher(storage, None, record_usage=False)
    kg = KnowledgeGraph(storage)
    result = await searcher.search("alice", "конфигурация подписки", limit=10, kg=kg, graph_expansion=True)
    assert ids["vpn"] in {item["id"] for item in result["results"]}


@pytest.mark.asyncio
async def test_an_asserted_relation_still_carries_a_neighbour(storage):
    """Grounding travels along relations the OWNER asserted, and must keep doing so.

    «Альфа зависит от Беты» is the owner's own claim, so a question about Alpha
    legitimately reaches Beta's document — even though the question never named
    Beta. Only co-occurrence («these two appeared in one document») and seeded
    entities stop grounding. The distinction is the whole point: without it this
    filter would have cut the graph channel's real work along with its noise.
    """
    from friday.storage.models import RelationType

    ids = _seed_corpus(storage)
    kg = KnowledgeGraph(storage)
    entities = {entity["name"]: entity["id"] for entity in kg.list_entities("alice", limit=50)}
    kg.create_relation("alice", entities["Казань"], entities["Караул"], RelationType.DEPENDS_ON, weight=1.0)

    searcher = HybridSearcher(storage, None, record_usage=False)
    result = await searcher.search("alice", "Казань", limit=10, kg=kg, graph_expansion=True)
    found = {item["id"] for item in result["results"]}
    assert ids["kazan"] in found
    assert ids["duty"] in found, "an asserted relation stopped carrying its neighbour"


@pytest.mark.asyncio
async def test_an_inflected_question_still_reaches_its_document(storage):
    """The case the unfiltered seeding existed to serve, now served honestly."""
    ids = _seed_corpus(storage)
    searcher = HybridSearcher(storage, None, record_usage=False)
    kg = KnowledgeGraph(storage)
    result = await searcher.search("alice", "Казань", limit=10, kg=kg, graph_expansion=True)
    assert ids["kazan"] in {item["id"] for item in result["results"]}
