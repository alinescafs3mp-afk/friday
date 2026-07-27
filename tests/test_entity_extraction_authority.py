"""Which extraction methods may write to the graph without a human looking.

`_link_entities` used to decide this with `method.startswith("explicit_")`. A method
named `explicit_identifier_syntax` — capitals joined by punctuation, nothing declared
by anyone — inherited that authority from its own name. Measured on the only real
document in this installation: 26 of 28 auto-accepted graph links came from it, and
about three quarters of those were not things (`CIDR-ПОДПИСКА` — a Russian word
shouted in a heading, present twice in two grammatical cases — `README-EN`,
`SET_DEFAULT_BROWSER`, `ТОП-100`, `V2-`).

A prefix test hands authority to whatever a future author names their pattern. These
tests hold the line where it belongs: on a list someone had to edit deliberately.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import jericho.ingestion._base as base
from jericho.ingestion import IngestionPipeline
from jericho.ingestion._base import (
    DECLARED_ENTITY_METHODS,
    EVIDENCE_ONLY_ENTITY_METHODS,
    _extract_entities,
)
from jericho.knowledge_graph import KnowledgeGraph
from jericho.storage.models import InboxStatus, KnowledgeObject, RawObject, new_id


def _methods_produced_anywhere_in_ingestion() -> set[str]:
    """Every entity-provenance method literal in the ingestion package.

    Walked rather than listed: a battery of sample texts only covers the patterns
    someone remembered to write a sample for, and the point is to catch the method
    nobody thought about. The walk covers the whole package, not just the regex
    extractor — entities are also proposed by the local model, by the agent, and by
    vision/transcript advice, and each of those is a `"method": "..."` literal.
    """
    package = Path(inspect.getfile(base)).parent
    found: set[str] = set()
    for source in sorted(package.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # add(name, entity_type, confidence, method, **evidence)
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "add"
                and len(node.args) >= 4
                and isinstance(node.args[3], ast.Constant)
            ):
                found.add(str(node.args[3].value))
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "method"
                        and isinstance(value, ast.Constant)
                    ):
                        found.add(str(value.value))
    return found


def test_every_extraction_method_is_classified():
    emitted = _methods_produced_anywhere_in_ingestion()
    assert emitted, "the AST walk found no methods — it stopped matching the code"
    unclassified = emitted - DECLARED_ENTITY_METHODS - EVIDENCE_ONLY_ENTITY_METHODS
    assert not unclassified, (
        f"new extraction method(s) {sorted(unclassified)} are neither declared nor "
        "evidence-only; decide deliberately which one they are"
    )


def test_the_two_authority_sets_do_not_overlap():
    assert not DECLARED_ENTITY_METHODS & EVIDENCE_ONLY_ENTITY_METHODS


def test_shape_matched_capitals_are_evidence_and_not_a_declaration():
    """`ТОП-100` and `CIDR-ПОДПИСКА` must not outrank a sentence that says what a thing is."""
    assert "identifier_syntax" in EVIDENCE_ONLY_ENTITY_METHODS
    assert "identifier_syntax" not in DECLARED_ENTITY_METHODS
    entities = {item["name"]: item for item in _extract_entities("Список ТОП-100 и CIDR-ПОДПИСКА.")}
    assert set(entities) == {"ТОП-100", "CIDR-ПОДПИСКА"}
    for item in entities.values():
        assert item["method"] == "identifier_syntax"
        # Below the 0.88 bar `_link_entities` uses to create a node without review.
        assert item["confidence"] < 0.88


def test_a_declared_identifier_still_outranks_the_bar():
    """The word «код» is a declaration by the author, and it keeps its authority."""
    entities = {item["name"]: item for item in _extract_entities("Договор, код KX-771.")}
    assert entities["KX-771"]["method"] == "explicit_identifier"
    assert entities["KX-771"]["method"] in DECLARED_ENTITY_METHODS
    assert entities["KX-771"]["confidence"] >= 0.88


@pytest.mark.asyncio
async def test_shouted_capitals_do_not_become_accepted_graph_nodes(settings, storage):
    """End to end: the two rules must disagree about the same document.

    On the live installation this document shape produced 28 accepted links, 26 of
    them from capitalisation alone. The graph is what every later answer is built
    on, so a node that arrives without anyone declaring it is a fact nobody stated.
    """
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(settings, storage, graph)
    text = (
        "Инструкция по настройке. Раздел CIDR-ПОДПИСКА и SET_DEFAULT_BROWSER, "
        "смотрите README-EN и ТОП-100 серверов. "
        "Проект Атлас отвечает за маршрутизацию, и это важно помнить при работе "
        "с подписками, потому что маршрут зависит от выбранного списка."
    )
    await pipeline.ingest_text("alice", text, source_ref="doc:1")
    # Linking happens on promotion, so the reviewer has to say yes first — and that
    # is the point: they approved the DOCUMENT, not each capitalised fragment in it.
    inbox_id = str(pipeline.list_inbox("alice")[0]["id"])
    pipeline.classify_inbox_item("alice", inbox_id, InboxStatus.CLASSIFIED, promote=True, reviewed_by="alice")

    by_id = {str(item["id"]): str(item["name"]) for item in storage.list_entities("alice", limit=200)}
    accepted = {
        by_id.get(str(link["entity_id"]), "")
        for link in storage.list_knowledge_entity_links("alice", status="accepted", limit=200)
    }
    entity_names = set(by_id.values())
    shouted = {"CIDR-ПОДПИСКА", "SET_DEFAULT_BROWSER", "README-EN", "ТОП-100"}
    assert not shouted & entity_names, f"shape alone created graph nodes: {shouted & entity_names}"
    assert not shouted & accepted
    # The declared one survives — this is a filter, not an off switch.
    assert "Атлас" in entity_names


@pytest.mark.asyncio
async def test_naming_a_method_explicit_no_longer_grants_it_authority(settings, storage):
    """The defect was that a NAME conferred the right to write to the graph.

    This is the mechanism test, and it is deliberately redundant with the confidence
    calibration above: lowering `identifier_syntax` to 0.75 already keeps today's
    shape matches out of the graph, so reverting the gate alone leaves the end-to-end
    test green. That redundancy is the point — the gate is what protects the method
    somebody adds next year, whatever they decide to call it.
    """
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(settings, storage, graph)
    storage.ensure_user("alice")
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref=new_id("source"),
        raw_content="тело",
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(id=new_id("ko"), user_id="alice", raw_object_id=raw.id, content="тело", title="тело")
    storage.store_knowledge_object(ko)

    links, _ = pipeline._link_entities(  # noqa: SLF001
        "alice",
        ko.id,
        raw.id,
        [
            {
                "name": "Придуманное",
                "entity_type": "other",
                "confidence": 0.95,
                "method": "explicit_brand_new_marker",
            },
            {
                "name": "Атлас",
                "entity_type": "project",
                "confidence": 0.93,
                "method": "explicit_project_marker",
            },
        ],
    )
    by_name = {str(storage.get_entity(str(link["entity_id"]), "alice")["name"]): link for link in links}
    # High confidence and an `explicit_` name, but nobody put it on the list.
    assert by_name["Придуманное"]["status"] == "suggested"
    assert by_name["Атлас"]["status"] == "accepted"
