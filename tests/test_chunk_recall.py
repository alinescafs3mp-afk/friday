"""Passage-level dense recall — chunking long Knowledge Objects for embeddings.

A whole imported article is stored as ONE Knowledge Object and, until 0.41.0, as
ONE averaged vector. A forty-section import that mentions the user's dog in a
single section produces a vector dominated by the other thirty-nine sections, so
the cosine against "что любит мой питомец" lands below the evidence gate and the
object is discarded — even though one passage answers the query exactly.

The first test PINS that defect (it passes on 0.40.0). The second is the contract
that fixes it: chunk vectors let the matching passage carry the object, while the
whole-object vector stays the floor, so chunking can only ever add recall.
"""

from __future__ import annotations

import dataclasses
import hashlib
import heapq
import math
import random
import re
import sqlite3
import string
from dataclasses import replace

import pytest

from jericho import retrieval as retrieval_module
from jericho.retrieval import (
    HybridSearcher,
    aggregate_chunk_scores,
    chunk_scheme,
    chunk_spans,
    dense_cosine,
    knowledge_chunk_units,
    knowledge_search_text,
    pack_vector,
)
from jericho.storage import SCHEMA_VERSION, JerichoStorage
from jericho.storage.models import KnowledgeObject, RawObject, new_id
from jericho.workers import WorkersManager

# A composite "semantic" embedding: unlike a one-hot toy vector it reproduces
# DILUTION, which is the whole point — a topic that owns 1/40th of a document
# survives in the document vector only as 1/40th of its length.
#
# The SCORING vocabulary deliberately differs from the vocabulary the sections are
# WRITTEN from: "питом" scores on the pet axis but never appears in the article, so
# the query stays lexically disjoint while still being semantically about the dog.
_TOPICS: dict[str, tuple[str, ...]] = {
    "pet": ("пёс", "рекс", "корм", "собак", "миск", "питом"),
    "tax": ("налог", "декларац", "вычет"),
    "code": ("сервер", "деплой", "миграц"),
    "trip": ("рейс", "отель", "виза"),
}
_SECTION_WORDS: dict[str, tuple[str, ...]] = {
    "pet": ("пёс", "рекс", "корм", "собак", "миск"),
    "tax": _TOPICS["tax"],
    "code": _TOPICS["code"],
    "trip": _TOPICS["trip"],
}


def _topic_vector(text: str) -> list[float]:
    lowered = text.lower()
    counts = [float(sum(lowered.count(word) for word in words)) for words in _TOPICS.values()]
    norm = math.sqrt(sum(value * value for value in counts))
    return [value / norm for value in counts] if norm else [0.0] * len(_TOPICS)


class _FakeTopicEmbeddings:
    """Stands in for EmbeddingBackend; records every batch it was asked to embed."""

    def __init__(self, settings):
        self.settings = settings
        self.remote_enabled = True
        self.calls: list[list[str]] = []

    async def embed(self, texts):
        batch = list(texts)
        self.calls.append(batch)
        return [_topic_vector(text) for text in batch]


def _section(words: tuple[str, ...], index: int, *, chars: int = 1100) -> str:
    """A ~``chars``-long paragraph built only from one topic's vocabulary."""
    parts: list[str] = [f"Раздел {index}."]
    position = 0
    while sum(len(part) + 1 for part in parts) < chars:
        parts.append(f"{words[position % len(words)]}ами")
        position += 1
    return " ".join(parts)


def _long_import(pet_section: int = 7, sections: int = 40) -> str:
    """A long import whose ONLY pet passage is section ``pet_section``."""
    filler = ("tax", "code", "trip")
    body: list[str] = []
    for index in range(sections):
        topic = "pet" if index == pet_section else filler[index % len(filler)]
        body.append(_section(_SECTION_WORDS[topic], index))
    return "\n\n".join(body)


def _make_ko(storage, user_id: str, content: str, *, title: str, summary: str) -> dict:
    """Like the other suites' helper, but with a SHORT summary — a summary equal to
    the body would smuggle the whole document into every chunk's header."""
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
        summary=summary,
    )
    storage.store_knowledge_object(ko)
    return storage.get_knowledge_object(ko.id, user_id) or {}


def _embeddings_settings(settings, **overrides):
    base = {
        "embeddings_enabled": True,
        "embeddings_base_url": "http://127.0.0.1:9999/v1",
        "embeddings_model": "test-embed",
        "embeddings_recall_candidates": 40,
        "embeddings_index_batch": 64,
    }
    return dataclasses.replace(settings, **{**base, **overrides})


# The query shares NO surface token with the article: "питомец" and "любит" never
# appear in it, so FTS, field and graph all score zero and embeddings are the only
# channel that can recall it.
QUERY = "что любит мой питомец"


@pytest.mark.asyncio
async def test_long_object_passage_is_missed_by_the_averaged_vector(storage, settings):
    """Pins the pre-0.41 defect: one averaged vector loses a single relevant passage.

    Also the regression test for the off switch — ``JERICHO_EMBEDDINGS_CHUNK_CHARS=0``
    must reproduce 0.40.0 exactly, including this weakness.
    """
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=0)
    fake = _FakeTopicEmbeddings(tuned)
    article = _make_ko(
        storage,
        "alice",
        _long_import(),
        title="Импортированная статья",
        summary="Длинный импорт из блога",
    )

    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)
    await manager._embeddings_index_all()  # noqa: SLF001
    assert storage.count_knowledge_embeddings("alice") == 1
    assert storage.count_knowledge_chunk_embeddings("alice") == 0

    # The query IS about the pet axis — the signal exists in the document, it is just
    # drowned: the cosine of the averaged vector falls below the 0.16 evidence gate in
    # _exclusion_reason, while the pet section on its own would score ~1.
    query_vector = _topic_vector(QUERY)
    assert query_vector[0] == pytest.approx(1.0)
    whole = _topic_vector(knowledge_search_text(article))
    assert 0.0 < dense_cosine(query_vector, whole) < 0.16
    assert dense_cosine(query_vector, _topic_vector(_section(_SECTION_WORDS["pet"], 7))) > 0.99

    result = await HybridSearcher(storage, fake).search("alice", QUERY, limit=5, explain=True)
    assert article["id"] not in {hit["id"] for hit in result["results"]}
    discarded = {row["id"]: row for row in result["trace"]}
    assert discarded[article["id"]]["reason"] == "insufficient_evidence"


@pytest.mark.asyncio
async def test_long_object_passage_is_recalled_with_chunking(storage, settings):
    """The contract: the matching passage carries the object into the results."""
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    article = _make_ko(
        storage,
        "alice",
        _long_import(),
        title="Импортированная статья",
        summary="Длинный импорт из блога",
    )

    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)
    await manager._embeddings_index_all()  # noqa: SLF001
    # The whole-object vector still exists — chunking adds rows, it never replaces them.
    assert storage.count_knowledge_embeddings("alice") == 1
    assert storage.count_knowledge_chunk_embeddings("alice") > 1

    result = await HybridSearcher(storage, fake).search("alice", QUERY, limit=5)
    hit = next(item for item in result["results"] if item["id"] == article["id"])
    # Far above both the 0.16 evidence gate and the ~0.04 the averaged vector scores.
    # It is not ~1.0 by design: the corroboration term discounts a lone strong passage.
    assert hit["_embedding_score"] > 0.5
    assert hit["_score_components"]["embedding_chunk"] >= 0
    assert result["strategy"]["embeddings_chunked"] is True
    # Chunk keys are an internal detail: they must never escape as result ids.
    assert all("#" not in item["id"] for item in result["results"])


# --- the chunker itself ---------------------------------------------------


def test_chunk_spans_never_split_mid_word_and_overlap():
    text = "\n\n".join(_section(_SECTION_WORDS["tax"], index) for index in range(40))
    spans = chunk_spans(text, max_chars=1200, overlap_chars=200, max_chunks=64)

    assert len(spans) > 1
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(end > start for start, end in spans)
    # Every character is covered by at least one span — no passage is unsearchable.
    covered = bytearray(len(text))
    for start, end in spans:
        covered[start:end] = b"\x01" * (end - start)
    assert 0 not in covered
    # Consecutive spans overlap, so a fact on a boundary lands whole in one of them.
    assert all(spans[index][0] < spans[index - 1][1] for index in range(1, len(spans)))
    # No span starts in the middle of a word.
    assert not [start for start, _ in spans if start > 0 and not (text[start - 1].isspace())]


def test_chunk_spans_terminate_on_a_blob_with_no_whitespace():
    # A base64 attachment has no natural boundary anywhere: the chunker must still
    # terminate, respect the cap, and cover the whole blob.
    blob = "".join(random.choices(string.ascii_letters, k=100_000))  # noqa: S311 - not crypto
    spans = chunk_spans(blob, max_chars=1200, overlap_chars=200, max_chunks=64)
    assert 0 < len(spans) <= 64
    assert spans[0][0] == 0 and spans[-1][1] == len(blob)


def test_a_very_long_document_is_covered_to_its_last_character():
    """The tail of a long document was cut away, and nothing said so.

    The widening was a single pass — capped at ``max_chars * 4`` and sized by
    ``ceil(len/limit)`` — followed by ``[:limit]``. Both bounds under-shoot,
    because ``_chunk_boundary`` snaps a span back by up to half a window, so
    whatever did not fit was dropped in silence. With the shipped defaults
    (1200 / 200 / 63) a 490 KB body indexed **59% of itself**; the missing 41%
    was reachable only through the whole-object vector, which is itself capped,
    and no signal distinguished a truncated document from a short one.

    Coarse passages are the accepted price: the whole-object vector stays the
    floor, so chunking can only ever add recall.
    """
    import random as _random

    _random.seed(3)
    words = ["текст", "предложение", "данные", "система", "важно", "заметка"]
    for size in (100_000, 500_000, 2_000_000):
        body = " ".join(_random.choice(words) for _ in range(size // 8))[:size]
        spans = chunk_spans(body, max_chars=1200, overlap_chars=200, max_chunks=63)
        assert len(spans) <= 63
        assert spans[0][0] == 0
        assert spans[-1][1] == len(body), (
            f"{size} chars: indexing stopped at {spans[-1][1]} — "
            f"{100 * (1 - spans[-1][1] / len(body)):.1f}% of the document is unsearchable"
        )
        covered = bytearray(len(body))
        for start, end in spans:
            covered[start:end] = b"\x01" * (end - start)
        assert 0 not in covered

    # Passage granularity where it already fitted must not get coarser: a 100 KB
    # body used ~63 spans of ~1.8 K before and has to keep doing so, or this
    # "fix" would quietly degrade recall on the documents that already worked.
    body = " ".join(_random.choice(words) for _ in range(100_000 // 8))[:100_000]
    spans = chunk_spans(body, max_chars=1200, overlap_chars=200, max_chunks=63)
    assert len(spans) >= 60
    assert sum(end - start for start, end in spans) // len(spans) < 2_500


@pytest.mark.asyncio
async def test_chunking_is_inert_for_short_objects(storage, settings):
    """The load-bearing case: a short note is embedded byte-identically to 0.40.0."""
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    note = _make_ko(storage, "alice", "Мой пёс Рекс любит мяч", title="Пёс", summary="Про пса")
    assert knowledge_chunk_units(note, max_chars=1200, overlap_chars=200, max_chunks=64) == []

    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)
    await manager._embeddings_index_all()  # noqa: SLF001
    assert storage.count_knowledge_chunk_embeddings("alice") == 0
    assert fake.calls == [[knowledge_search_text(note)]]


def test_chunk_settings_default_and_off_switch(settings):
    assert settings.embeddings_chunk_chars == 1200
    assert settings.embeddings_chunk_overlap_chars == 200
    assert settings.embeddings_chunk_max_per_object == 64
    assert settings.embeddings_chunk_blend == pytest.approx(0.25)
    assert settings.embeddings_chunk_scan_multiplier == 4
    assert settings.embeddings_max_inputs_per_request == 64
    # '' is exactly what every pre-0.41 row stores, so switching off re-indexes nothing.
    assert chunk_scheme(dataclasses.replace(settings, embeddings_chunk_chars=0)) == ""
    assert chunk_scheme(settings).startswith("v1:")
    # The tuning knobs stay internal; only the on/off state is exposed.
    exposed = settings.public_dict()["embeddings"]
    assert exposed["chunk_chars"] == 1200
    assert "chunk_blend" not in exposed


# --- storage contract -----------------------------------------------------


def test_migration_creates_the_chunk_table_on_a_live_database(settings, tmp_path):
    database = tmp_path / "legacy-chunks.sqlite3"
    seed = JerichoStorage(replace(settings, database_path=database))
    note = _make_ko(seed, "alice", "Мой пёс Рекс", title="Пёс", summary="Про пса")
    seed.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": note["id"],
                "user_id": "alice",
                "model": "test-embed",
                "dim": 4,
                "source_version": note["version"],
                "content_hash": "h",
                "vector": pack_vector([1.0, 0.0, 0.0, 0.0]),
            }
        ]
    )
    seed.close()

    # Simulate a schema-15 database: no chunk table, no chunk_scheme column.
    raw = sqlite3.connect(database)
    raw.execute("DROP TABLE knowledge_chunk_embeddings")
    raw.execute("ALTER TABLE knowledge_embeddings DROP COLUMN chunk_scheme")
    raw.execute("UPDATE schema_meta SET value='15' WHERE key='schema_version'")
    raw.commit()
    raw.close()

    migrated = JerichoStorage(replace(settings, database_path=database))
    try:
        tables = {
            row[0] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert "knowledge_chunk_embeddings" in tables
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(knowledge_embeddings)").fetchall()}
        assert "chunk_scheme" in columns
        version = int(
            migrated.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
        )
        assert version == SCHEMA_VERSION == 17
        # The pre-existing vector survived and reads as "never chunked".
        row = migrated.execute(
            "SELECT chunk_scheme FROM knowledge_embeddings WHERE knowledge_object_id=?", (note["id"],)
        ).fetchone()
        assert row["chunk_scheme"] == ""
        assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        migrated.close()


@pytest.mark.asyncio
async def test_staleness_flips_only_for_chunk_eligible_objects(storage, settings):
    """Enabling chunking must not rewrite the corpus of short notes."""
    plain = _embeddings_settings(settings, embeddings_chunk_chars=0)
    fake = _FakeTopicEmbeddings(plain)
    short = _make_ko(storage, "alice", "Мой пёс Рекс", title="Пёс", summary="Коротко")
    long_one = _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт из блога")
    await WorkersManager(plain, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001
    assert storage.count_knowledge_embeddings("alice") == 2

    chunked = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    stale = storage.list_knowledge_missing_embedding(
        "test-embed", chunk_scheme=chunk_scheme(chunked), chunk_threshold=1200
    )
    assert [row["id"] for row in stale] == [long_one["id"]]
    assert short["id"] not in {row["id"] for row in stale}


@pytest.mark.asyncio
async def test_chunk_rows_are_replaced_not_accumulated_when_an_object_shrinks(storage, settings):
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    article = _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт")
    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)
    await manager._embeddings_index_all()  # noqa: SLF001
    assert storage.count_knowledge_chunk_embeddings("alice") > 1

    storage.update_knowledge_fields(article["id"], "alice", content="Теперь совсем короткая заметка")
    await manager._embeddings_index_all()  # noqa: SLF001
    # Delete-then-insert: the object shrank below the threshold and left no orphans.
    assert storage.count_knowledge_chunk_embeddings("alice") == 0
    assert storage.count_knowledge_embeddings("alice") == 1
    assert storage.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
async def test_chunk_vectors_exclude_soft_deleted_objects(storage, settings):
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    article = _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт")
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001
    assert storage.get_user_chunk_embeddings("alice", "test-embed", 4)

    storage.soft_delete_knowledge_object(article["id"], "alice")
    # Without this filter, deleted knowledge would resurrect through its passages.
    assert storage.get_user_chunk_embeddings("alice", "test-embed", 4) == []


# --- indexer --------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_never_exceeds_max_inputs_and_keeps_objects_whole(storage, settings):
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200, embeddings_max_inputs_per_request=16)
    fake = _FakeTopicEmbeddings(tuned)
    ids = {
        _make_ko(storage, "alice", _long_import(sections=12), title=f"Импорт {n}", summary="Длинный")["id"]
        for n in range(3)
    }
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001

    assert len(fake.calls) > 1  # the batch really was split into several requests
    # Every object's inputs stay inside ONE request, so a failed request loses that
    # object and never scrambles vectors across objects.
    owner = re.compile(r"Импорт \\d")
    for call in fake.calls:
        assert len({match.group(0) for text in call if (match := owner.search(text))}) <= 1
    assert storage.count_chunked_knowledge_objects("alice") == len(ids)


@pytest.mark.asyncio
async def test_lifecycle_only_change_costs_no_embedding_call(storage, settings):
    """content_hash reuse: a version bump that did not change the text is free."""
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    article = _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт")
    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)
    await manager._embeddings_index_all()  # noqa: SLF001
    chunk_rows = storage.count_knowledge_chunk_embeddings("alice")
    fake.calls.clear()

    storage.update_knowledge_fields(article["id"], "alice", lifecycle_stage="archived")
    await manager._embeddings_index_all()  # noqa: SLF001
    assert fake.calls == []  # every vector came from the reuse cache
    assert storage.count_knowledge_chunk_embeddings("alice") == chunk_rows
    assert (
        storage.list_knowledge_missing_embedding(
            "test-embed", chunk_scheme=chunk_scheme(tuned), chunk_threshold=1200
        )
        == []
    )


# --- aggregation and the read path ----------------------------------------


def test_aggregation_prefers_corroborated_document_over_one_lucky_fragment():
    lucky = [(0.90, "ko_a#0"), (0.05, "ko_a#1"), (0.05, "ko_a#2")]
    about_it = [(0.88, "ko_b#0"), (0.85, "ko_b#1"), (0.80, "ko_b#2")]
    scores, provenance = aggregate_chunk_scores(lucky + about_it, blend=0.25)
    assert scores["ko_b"] > scores["ko_a"]
    assert provenance["ko_a"] == (0, 3)

    # blend=0 degenerates to pure max-over-passages, where the lucky fragment wins.
    pure_max, _ = aggregate_chunk_scores(lucky + about_it, blend=0.0)
    assert pure_max["ko_a"] > pure_max["ko_b"]
    assert pure_max["ko_a"] == pytest.approx(0.90)


def test_aggregation_ignores_keys_that_are_not_chunk_keys():
    scores, provenance = aggregate_chunk_scores([(0.9, "ko_plain"), (0.5, "ko_a#1")], blend=0.25)
    assert set(scores) == {"ko_a"} and set(provenance) == {"ko_a"}


@pytest.mark.asyncio
async def test_chunk_scan_cap_is_object_granular_and_reported(storage, settings):
    """A capped scan never covers a document only halfway."""
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200, embeddings_dense_max_objects=1)
    fake = _FakeTopicEmbeddings(tuned)
    for index in range(3):
        _make_ko(storage, "alice", _long_import(sections=12), title=f"Импорт {index}", summary="Длинный")
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001

    searcher = HybridSearcher(storage, fake)
    meta: dict = {}
    await searcher._dense_recall("alice", QUERY, {}, meta=meta)  # noqa: SLF001
    assert meta["dense_scanned"] == 1  # objects, exactly as before chunking
    assert meta["dense_capped"] is True
    # The chunk scan covers that ONE object completely — not a fraction of each.
    newest = storage.get_user_embeddings("alice", "test-embed", 4, limit=1)[0][0]
    expected = storage.execute(
        "SELECT COUNT(*) AS n FROM knowledge_chunk_embeddings WHERE knowledge_object_id=?", (newest,)
    ).fetchone()["n"]
    assert meta["dense_chunks_scanned"] == expected

    result = await searcher.search("alice", QUERY, limit=5)
    assert result["strategy"].get("embeddings_capped") is True


@pytest.mark.asyncio
async def test_numpy_and_python_agree_on_chunk_recall(storage, settings, monkeypatch):
    """The optional numpy extra must not rank differently from the pure-Python path."""
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт")
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001

    searcher = HybridSearcher(storage, fake)
    with_numpy = await searcher._dense_recall("alice", QUERY, {})  # noqa: SLF001
    monkeypatch.setattr(retrieval_module, "_np", None)
    without_numpy = await searcher._dense_recall("alice", QUERY, {})  # noqa: SLF001

    assert set(with_numpy) == set(without_numpy)
    for key, value in with_numpy.items():
        assert value == pytest.approx(without_numpy[key], abs=1e-4)


@pytest.mark.asyncio
async def test_explain_trace_names_the_winning_chunk(storage, settings):
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    article = _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт")
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001

    result = await HybridSearcher(storage, fake).search("alice", QUERY, limit=5, explain=True)
    row = next(entry for entry in result["trace"] if entry["id"] == article["id"])
    assert row["components"]["embedding_chunk"] >= 0
    assert row["components"]["embedding_chunks"] == storage.count_knowledge_chunk_embeddings("alice")


@pytest.mark.asyncio
async def test_matched_passage_grounds_the_answer_context(storage, settings):
    """Retrieval improved is worthless if the model still reads the wrong window."""
    from jericho.agent_runtime import _matched_region

    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    article = _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт")
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001

    result = await HybridSearcher(storage, fake).search("alice", QUERY, limit=5)
    hit = next(item for item in result["results"] if item["id"] == article["id"])
    span = hit["_embedding_chunk_span"]
    assert isinstance(span, list) and span[1] > span[0]
    region = _matched_region(hit)
    # The grounding region is the matched passage, and it is about the dog — the head
    # of the document (which is what a lexical window would pick) is not.
    assert len(region) < len(article["content"])
    assert any(word in region.lower() for word in _SECTION_WORDS["pet"])


@pytest.mark.asyncio
async def test_diagnostics_reports_chunk_coverage(storage, settings):
    from jericho.diagnostics import collect_diagnostics

    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт")
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001

    report = collect_diagnostics(tuned, storage=storage)
    coverage = report["embeddings_index"]
    assert coverage["available"] is True
    assert coverage["indexed_objects"] == 1
    assert coverage["chunked_objects"] == 1
    assert coverage["chunk_rows"] > 1


@pytest.mark.asyncio
async def test_eval_ab_reports_the_recall_gain(storage, settings):
    """The acceptance criterion runs on the operator's own gold set."""
    from jericho.eval import compare_chunk_recall

    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    article = _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт")
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001
    storage.add_eval_case("alice", QUERY, [article["id"]])

    report = await compare_chunk_recall(storage, fake, tuned, "alice")
    assert report["baseline"]["recall_at_k"] < report["chunked"]["recall_at_k"]
    assert report["delta"]["recall_at_k"] > 0
    assert report["delta"]["precision_at_k"] >= 0
    assert report["per_case"][0]["delta"] >= 0  # no case regressed

    off = dataclasses.replace(tuned, embeddings_chunk_chars=0)
    assert (await compare_chunk_recall(storage, fake, off, "alice"))["reason"] == "chunking disabled"


# --- regressions found by adversarial review ------------------------------


class _CappedEmbeddings(_FakeTopicEmbeddings):
    """An endpoint that enforces its client batch limit, like TEI or vLLM do."""

    def __init__(self, settings, *, cap: int):
        super().__init__(settings)
        self._cap = cap

    async def embed(self, texts):
        batch = list(texts)
        self.calls.append(batch)
        if len(batch) > self._cap:
            return None
        return [_topic_vector(text) for text in batch]


@pytest.mark.asyncio
async def test_one_object_never_exceeds_a_single_request_at_stock_defaults(storage, settings):
    """Stock defaults used to emit 65 inputs against a 64-input cap, so a maximally
    split import was rejected forever and ended up with NO vector at all — strictly
    less recall than before chunking existed."""
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    assert tuned.embeddings_chunk_max_per_object == tuned.embeddings_max_inputs_per_request
    fake = _CappedEmbeddings(tuned, cap=tuned.embeddings_max_inputs_per_request)
    # 200 sections: far more than the 64-chunk ceiling, so the object is maximally split.
    _make_ko(storage, "alice", _long_import(sections=200), title="Импорт", summary="Очень длинный")

    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001

    assert fake.calls, "the indexer never called the backend"
    assert max(len(call) for call in fake.calls) <= tuned.embeddings_max_inputs_per_request
    # And it actually got indexed — doc vector AND passages.
    assert storage.count_knowledge_embeddings("alice") == 1
    assert storage.count_knowledge_chunk_embeddings("alice") > 1


@pytest.mark.asyncio
async def test_chunking_never_evicts_a_doc_only_object_from_recall(storage, settings):
    """The floor protects an object's SCORE; it must also protect its SELECTION.

    Chunk scores are systematically higher than document averages, so selecting
    candidates on the combined score alone let chunked imports crowd a short, strongly
    matching note out of the fixed recall budget — and an object outside candidate_map
    loses its dense score entirely.
    """
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200, embeddings_recall_candidates=4)
    fake = _FakeTopicEmbeddings(tuned)
    # A partly-relevant note: cosine 0.6 against the query — comfortably the best
    # WHOLE-OBJECT score in the corpus, but below what max-over-passages hands the
    # long imports (~0.87), which is exactly how the eviction used to happen.
    note = _make_ko(
        storage,
        "alice",
        " ".join(["пёс"] * 3 + ["налог"] * 4),
        title="Заметка",
        summary="Смешанная",
    )
    for index in range(8):
        _make_ko(storage, "alice", _long_import(sections=12), title=f"Импорт {index}", summary="Длинный")
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001

    plain = await HybridSearcher(storage, fake, chunk_recall=False)._dense_recall(  # noqa: SLF001
        "alice", QUERY, {}
    )
    assert note["id"] in plain, "fixture is wrong: the note must be recalled without chunking"

    chunked = await HybridSearcher(storage, fake)._dense_recall("alice", QUERY, {})  # noqa: SLF001
    # Passage recall must ADD candidates, never evict the ones the whole-object
    # vector already earned.
    assert set(plain) <= set(chunked), "chunking dropped an object dense recall used to return"
    assert note["id"] in chunked


def test_row_fuse_cuts_only_on_object_boundaries():
    from jericho.retrieval import _trim_to_whole_objects

    def rows(spec):
        return [(f"{name}#{index}", b"") for name, count in spec for index in range(count)]

    def objects(trimmed):
        counted: dict[str, int] = {}
        for key, _ in trimmed:
            counted[key.rpartition("#")[0]] = counted.get(key.rpartition("#")[0], 0) + 1
        return counted

    # A plain LIMIT would keep 3 of b's 5 chunks; the trim drops b entirely.
    assert objects(_trim_to_whole_objects(rows([("a", 3), ("b", 5)]), 6)) == {"a": 3}
    assert objects(_trim_to_whole_objects(rows([("a", 3), ("b", 3)]), 6)) == {"a": 3, "b": 3}
    assert objects(_trim_to_whole_objects(rows([("a", 2), ("b", 2), ("c", 9)]), 5)) == {"a": 2, "b": 2}
    # Never return nothing: a first object over budget is kept whole rather than halved.
    assert objects(_trim_to_whole_objects(rows([("a", 10), ("b", 2)]), 6)) == {"a": 10}
    assert _trim_to_whole_objects([], 6) == []


@pytest.mark.asyncio
async def test_truncated_scan_never_reports_a_partially_scanned_object(storage, settings):
    tuned = _embeddings_settings(
        settings,
        embeddings_chunk_chars=1200,
        embeddings_dense_max_objects=3,
        embeddings_chunk_scan_multiplier=1,
        embeddings_chunk_max_per_object=4,
    )
    fake = _FakeTopicEmbeddings(tuned)
    for index in range(3):
        _make_ko(storage, "alice", _long_import(sections=12), title=f"Импорт {index}", summary="Длинный")
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001

    meta: dict = {}
    await HybridSearcher(storage, fake)._dense_recall("alice", QUERY, {}, meta=meta)  # noqa: SLF001
    # Whatever was scanned is a whole number of complete objects.
    per_object = {
        row["knowledge_object_id"]: row["n"]
        for row in storage.execute(
            "SELECT knowledge_object_id, COUNT(*) AS n FROM knowledge_chunk_embeddings GROUP BY 1"
        ).fetchall()
    }
    scanned = meta["dense_chunks_scanned"]
    assert scanned in {sum(sorted(per_object.values())[:n]) for n in range(len(per_object) + 1)} or any(
        scanned == count for count in per_object.values()
    )


@pytest.mark.asyncio
async def test_dimension_change_recovers_instead_of_deadlocking(storage, settings):
    """A model that answers in a new dimension under the same name must converge.

    Reused vectors carry the OLD dimension while freshly embedded ones carry the new
    one; refusing to mix them is right, but doing so without dropping the stale rows
    meant the reuse cache resurrected them on every tick and the object was stuck
    forever — worse than pre-0.41, which simply overwrote the row.
    """
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    article = _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт")
    manager = WorkersManager(tuned, storage, None, None, embeddings=fake)
    await manager._embeddings_index_all()  # noqa: SLF001
    assert {row["dim"] for row in storage.execute("SELECT DISTINCT dim FROM knowledge_embeddings")} == {4}

    # The model behind the same name now answers in 5 dimensions.
    async def wider(texts):
        fake.calls.append(list(texts))
        return [[*_topic_vector(text), 0.5] for text in texts]

    fake.embed = wider
    # Appending a section leaves the earlier passages byte-identical, so they come
    # back from the reuse cache (4 dims) while the document text is embedded afresh
    # (5 dims) — exactly the mix that used to wedge the object.
    storage.update_knowledge_fields(
        article["id"], "alice", content=_long_import() + "\n\n" + _section(_SECTION_WORDS["trip"], 99)
    )
    await manager._embeddings_index_all()  # noqa: SLF001
    await manager._embeddings_index_all()  # noqa: SLF001

    dims = {row["dim"] for row in storage.execute("SELECT DISTINCT dim FROM knowledge_embeddings")}
    assert dims == {5}, f"the index never converged on the new dimension: {dims}"
    chunk_dims = {
        row["dim"] for row in storage.execute("SELECT DISTINCT dim FROM knowledge_chunk_embeddings")
    }
    assert chunk_dims == {5}


@pytest.mark.asyncio
async def test_lexically_matched_hit_keeps_the_whole_body_for_excerpting(storage, settings):
    """A hit that also matched lexically must not be narrowed to the dense passage —
    the excerpt could otherwise drop the very phrase the user searched for."""
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    article = _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт")
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001

    # "налогами" is a literal term of the document, so FTS matches it.
    result = await HybridSearcher(storage, fake).search("alice", "налогами", limit=5)
    hit = next((item for item in result["results"] if item["id"] == article["id"]), None)
    assert hit is not None
    assert "_embedding_chunk_span" not in hit


@pytest.mark.asyncio
async def test_stale_span_is_dropped_after_an_edit(storage, settings):
    """Between an edit and the next index tick the stored offsets describe the OLD
    revision; slicing today's content at them would quote an arbitrary window."""
    tuned = _embeddings_settings(settings, embeddings_chunk_chars=1200)
    fake = _FakeTopicEmbeddings(tuned)
    article = _make_ko(storage, "alice", _long_import(), title="Импорт", summary="Длинный импорт")
    await WorkersManager(tuned, storage, None, None, embeddings=fake)._embeddings_index_all()  # noqa: SLF001
    fresh = await HybridSearcher(storage, fake).search("alice", QUERY, limit=5)
    assert "_embedding_chunk_span" in next(item for item in fresh["results"] if item["id"] == article["id"])

    storage.update_knowledge_fields(article["id"], "alice", content=_long_import(pet_section=3))
    stale = await HybridSearcher(storage, fake).search("alice", QUERY, limit=5)
    hit = next((item for item in stale["results"] if item["id"] == article["id"]), None)
    if hit is not None:
        assert "_embedding_chunk_span" not in hit


def test_candidate_ties_break_deterministically_by_id():
    """Selection order must not depend on PYTHONHASHSEED: equal scores break on id,
    exactly as the pre-0.41 nlargest over (score, id) tuples did."""
    scores = {f"ko_{index}": 0.5 for index in range(10)}
    ordered = [key for key, _ in heapq.nlargest(3, scores.items(), key=lambda p: (p[1], p[0]))]
    assert ordered == sorted(scores, reverse=True)[:3]


@pytest.mark.asyncio
async def test_the_pool_fallback_bounds_what_it_sends(storage, settings):
    """Before the index exists, the fallback embedded the whole pool in one request.

    Every candidate's FULL search text — title, summary, content, tags, kind,
    untruncated — for up to `pool_max` objects, in a single POST. The path that
    keeps search working before the indexer has run was therefore the request most
    likely to time out or be refused outright: a 400-object pool of ordinary
    articles is several megabytes.
    """
    from jericho.retrieval import _POOL_REQUEST_MAX_CHARS, _POOL_TEXT_MAX_CHARS, HybridSearcher

    class _RecordingEmbeddings:
        def __init__(self, settings) -> None:
            self.settings = settings
            self.remote_enabled = True
            self.requests: list[list[str]] = []

        async def embed(self, texts):
            self.requests.append(list(texts))
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    storage.ensure_user("alice")
    for index in range(40):
        _make_ko(
            storage,
            "alice",
            "проект " + "очень длинное тело документа " * 3000,
            title=f"K{index}",
            summary="проект",
        )

    fake = _RecordingEmbeddings(_embeddings_settings(settings, embeddings_chunk_chars=1200))
    searcher = HybridSearcher(storage, fake)
    await searcher.search("alice", "проект", limit=5)

    assert fake.requests, "the fallback never ran"
    # The query embedding is one request; the pool is split into bounded ones.
    pool_requests = [batch for batch in fake.requests if len(batch) > 1 or len(batch[0]) > 200]
    assert pool_requests, "the pool was not embedded"
    for batch in pool_requests:
        assert sum(len(text) for text in batch) <= max(_POOL_REQUEST_MAX_CHARS, _POOL_TEXT_MAX_CHARS)
        assert all(len(text) <= _POOL_TEXT_MAX_CHARS for text in batch)
