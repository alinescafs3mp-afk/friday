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

from jericho.knowledge_graph import KnowledgeGraph
from jericho.retrieval import HybridSearcher
from jericho.storage.models import Entity, KnowledgeObject, RawObject, new_id


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
        result = await searcher.search("alice", question, limit=10, kg=kg)
        assert result["count"] == 0, (
            f"{question!r} answered with {result['count']} unrelated document(s): "
            f"{[item.get('title') for item in result['results']]}"
        )


@pytest.mark.asyncio
async def test_an_inflected_question_still_reaches_its_document(storage):
    """The case the unfiltered seeding existed to serve, now served honestly."""
    ids = _seed_corpus(storage)
    searcher = HybridSearcher(storage, None, record_usage=False)
    kg = KnowledgeGraph(storage)
    result = await searcher.search("alice", "Казань", limit=10, kg=kg)
    assert ids["kazan"] in {item["id"] for item in result["results"]}
