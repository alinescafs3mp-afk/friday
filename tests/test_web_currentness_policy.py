from __future__ import annotations

import hashlib

import pytest

from friday.orchestration.web_currentness_policy import (
    CurrentnessPolicyError,
    SealedPublicQueryIntent,
    WebCurrentnessDecision,
    WebCurrentnessRequest,
    classify_web_currentness,
    seal_public_query,
    seal_public_query_intent,
)
from friday.web_research_contract import MAX_OUTBOUND_WEB_QUERY_CHARS


@pytest.mark.parametrize(
    "question",
    (
        "Найди последние официальные сведения о Python 3.14",
        "Проверь текущую цену продукта",
        "Сравни расписание поездов на сегодня",
        "What is the current office holder?",
        "What is the latest package version?",
        "Is the service available now?",
        "Which law applies this year?",
        "What is in the referenced external paper?",
        "What does https://public.example.test describe?",
    ),
)
def test_current_or_explicit_research_is_required(question: str) -> None:
    assert classify_web_currentness(question) is WebCurrentnessDecision.SEARCH_REQUIRED


@pytest.mark.parametrize(
    "facts",
    (
        WebCurrentnessRequest(unfamiliar_material_term=True),
        WebCurrentnessRequest(insufficient_material_evidence=True),
        WebCurrentnessRequest(coding_current_docs=True),
        WebCurrentnessRequest(engineer_current_advisories=True),
        WebCurrentnessRequest(local_conflict_resolvable=True),
        {"missing_external_reference": True},
    ),
)
def test_knowledge_gap_facts_require_research(facts: object) -> None:
    assert classify_web_currentness(facts) is WebCurrentnessDecision.SEARCH_REQUIRED


@pytest.mark.parametrize(
    "question",
    (
        "What is a binary tree?",
        "Explain the definition of a mutex.",
        "Как определить среднее арифметическое?",
        "What does polymorphism mean?",
        "What is an API?",
        "What is a product?",
    ),
)
def test_timeless_definition_does_not_require_search(question: str) -> None:
    assert classify_web_currentness(question) is WebCurrentnessDecision.SEARCH_NOT_REQUIRED


@pytest.mark.parametrize(
    "facts",
    (
        WebCurrentnessRequest(question="What is the current status of report.pdf?"),
        WebCurrentnessRequest(question="Проверь актуальную версию /home/user/private.txt"),
        WebCurrentnessRequest(question="Compare the latest result for job_abc123"),
        WebCurrentnessRequest(question="What is the current answer in this attached document?"),
        {"question": "Найди новости по этому файлу"},
    ),
)
def test_private_carrier_blocks_without_public_concepts(facts: object) -> None:
    assert classify_web_currentness(facts) is WebCurrentnessDecision.SEARCH_BLOCKED_PRIVATE


def test_private_context_can_be_classified_required_when_public_topic_is_separately_supplied() -> None:
    request = WebCurrentnessRequest(
        question="Проверь текущие docs в этом локальном файле",
        public_concepts=("Python 3.14 official documentation",),
    )
    assert classify_web_currentness(request) is WebCurrentnessDecision.SEARCH_REQUIRED


def test_private_marker_without_research_trigger_stays_local() -> None:
    assert (
        classify_web_currentness("Explain this attached document")
        is WebCurrentnessDecision.SEARCH_NOT_REQUIRED
    )


def test_temporal_slash_notation_is_not_mistaken_for_a_private_path() -> None:
    assert classify_web_currentness("What happened in the news on 2026/09/04?") is (
        WebCurrentnessDecision.SEARCH_REQUIRED
    )


def test_mapping_aliases_and_non_boolean_values_are_closed() -> None:
    assert classify_web_currentness({"freshness_sensitive": True}) is WebCurrentnessDecision.SEARCH_REQUIRED
    with pytest.raises(CurrentnessPolicyError):
        classify_web_currentness({"currentness_sensitive": 1})
    with pytest.raises(CurrentnessPolicyError):
        classify_web_currentness({"not_a_signal": True})


def test_sealer_uses_public_concepts_and_contract_bound() -> None:
    intent = seal_public_query_intent(("Python 3.14", "official documentation"))
    assert isinstance(intent, SealedPublicQueryIntent)
    assert intent.query == "Python 3.14 official documentation"
    assert intent.concepts == ("Python 3.14", "official documentation")
    assert intent.query_sha256 == hashlib.sha256(intent.query.encode()).hexdigest()
    assert seal_public_query(("Python", "documentation")) == "Python documentation"
    assert len(intent.query) <= MAX_OUTBOUND_WEB_QUERY_CHARS


def test_sealer_bounds_long_public_concepts_without_file_or_network_access() -> None:
    intent = seal_public_query_intent(("public concept " + "x" * 300,))
    assert len(intent.query) == MAX_OUTBOUND_WEB_QUERY_CHARS
    assert intent.query == ("public concept " + "x" * 300)[:MAX_OUTBOUND_WEB_QUERY_CHARS]


@pytest.mark.parametrize(
    "concepts",
    (
        (),
        ("",),
        ("this attached file",),
        ("report.pdf",),
        ("/home/user/report",),
        ("private_id_abc123",),
        ("https://example.test/private",),
        ("секретный документ здесь",),
    ),
)
def test_sealer_rejects_private_or_contextual_concepts(concepts: tuple[str, ...]) -> None:
    with pytest.raises(CurrentnessPolicyError):
        seal_public_query_intent(concepts)


def test_sealer_rejects_non_text_concepts_and_control_carriers() -> None:
    with pytest.raises(CurrentnessPolicyError):
        seal_public_query_intent(("Python", 3))
    with pytest.raises(CurrentnessPolicyError):
        seal_public_query_intent(("Python\nprivate",))
