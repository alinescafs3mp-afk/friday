"""A question typed badly is still a question.

Three inputs look identical to a token matcher and are three different things:

* «uhfabr jngecrjd» — the layout was never switched; a real question;
* «график дужурста» — a finger slipped; a real question;
* «asdkjhqwe zxcmn» — the phone was in a pocket; not a question.

The first two used to get the same silence as the third. The third must keep
getting it, which is the harder half: a repair loose enough to rescue mashing
would answer questions nobody asked.

Every repair here has to EARN its way in — a rewritten query is used only when
the archive answers it and the original finds nothing.
"""

from __future__ import annotations

import hashlib

import pytest

from friday.retrieval import HybridSearcher
from friday.retrieval._keyboard import switched
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _make_ko(storage, user_id: str, title: str, content: str) -> str:
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


@pytest.fixture
def corpus(storage):
    storage.ensure_user("alice")
    return {
        "duty": _make_ko(
            storage,
            "alice",
            "График дежурств",
            "График дежурств караула на месяц. Ответственный за смену докладывает дежурному.",
        ),
        "vpn": _make_ko(
            storage,
            "alice",
            "Конфигурация подписки",
            "Конфигурация подписки и чёрные списки адресов для маршрутизации.",
        ),
    }


@pytest.mark.asyncio
async def test_a_query_typed_in_the_wrong_layout_finds_its_document(storage, corpus):
    typed = "uhfabr jngecrjd"  # «график отпусков» with the layout stuck on English
    assert switched(typed) == "график отпусков", "the premise"

    searcher = HybridSearcher(storage, None, record_usage=False)
    result = await searcher.search("alice", typed, limit=5)
    assert corpus["duty"] in {item["id"] for item in result["results"]}
    # And the answer says the question was re-read, so nobody is silently
    # answered about a different string than the one they typed.
    assert result["strategy"]["query_repaired"] == "keyboard_layout"
    assert result["strategy"]["query_as_typed"] == typed


@pytest.mark.asyncio
async def test_russian_letters_from_an_english_question(storage, corpus):
    """The other direction: English typed with the layout stuck on Russian."""
    _make_ko(storage, "alice", "Backup policy", "The backup policy covers offsite copies.")
    typed = switched("backup policy")  # «憬...» — whatever the keys produce
    searcher = HybridSearcher(storage, None, record_usage=False)
    result = await searcher.search("alice", typed, limit=5)
    assert result["count"] >= 1
    assert result["strategy"]["query_repaired"] == "keyboard_layout"


@pytest.mark.asyncio
async def test_a_typo_is_corrected_against_the_corpus_own_words(storage, corpus):
    searcher = HybridSearcher(storage, None, record_usage=False)
    result = await searcher.search("alice", "конфигурацоя подпискт", limit=5)
    assert corpus["vpn"] in {item["id"] for item in result["results"]}
    assert result["strategy"]["query_repaired"] == "spelling"
    assert "→" in result["strategy"]["query_repair_detail"]


@pytest.mark.asyncio
async def test_keyboard_mashing_still_answers_nothing(storage, corpus):
    """The case where silence is the correct answer, and must stay correct."""
    searcher = HybridSearcher(storage, None, record_usage=False)
    for mashed in ("asdkjhqwe zxcmn", "фывапролдж ячсмить", "хжщзхжщз ккккк", "12345 67890"):
        result = await searcher.search("alice", mashed, limit=5)
        assert result["count"] == 0, f"{mashed!r} answered with {result['count']} result(s)"
        assert "query_repaired" not in result["strategy"]


@pytest.mark.asyncio
async def test_noise_that_collides_with_a_prefix_is_not_a_repair(storage, corpus):
    """The measured false positive, kept as a test.

    «хжщзхжщз ккккк» read on the other layout is «[;op[;op rrrrr». Its fragment
    `op` prefix-matched a log file on the real corpus — FTS searches by prefix —
    and "the variant finds something" was the whole acceptance rule, so noise
    was answered with two documents. A reading now also has to be made of words
    the corpus actually uses.
    """
    _make_ko(storage, "alice", "log.txt", "openvpn started, operator connected, opening tunnel")
    searcher = HybridSearcher(storage, None, record_usage=False)
    result = await searcher.search("alice", "хжщзхжщз ккккк", limit=5)
    assert result["count"] == 0, result["strategy"]


@pytest.mark.asyncio
async def test_a_question_that_works_is_never_rewritten(storage, corpus):
    """No repair on the happy path — and no extra queries paid for it."""
    searcher = HybridSearcher(storage, None, record_usage=False)
    result = await searcher.search("alice", "график отпусков", limit=5)
    assert corpus["duty"] in {item["id"] for item in result["results"]}
    assert "query_repaired" not in result["strategy"]


@pytest.mark.asyncio
async def test_a_word_this_archive_does_not_know_is_left_alone(storage, corpus):
    """Repair must not invent a topic: an unrelated real word stays unanswered."""
    searcher = HybridSearcher(storage, None, record_usage=False)
    result = await searcher.search("alice", "гидропоника", limit=5)
    assert result["count"] == 0
    assert "query_repaired" not in result["strategy"]
