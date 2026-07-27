from __future__ import annotations

import pytest

from jericho.ingestion import IngestionPipeline, _extract_entities
from jericho.knowledge_graph import KnowledgeGraph
from jericho.retrieval import HybridSearcher, best_snippet


def test_entity_extraction_uses_boundaries_and_explicit_markers():
    entities = _extract_entities(
        "Project Alpha is led by Ivan Petrov and Maria Sidorova at company Google. "
        "The conference called DevFest takes place in San Francisco."
    )
    by_name = {item["name"]: item for item in entities}
    assert "Alpha" in by_name
    assert "Ivan Petrov" in by_name
    assert "Maria Sidorova" in by_name
    assert "Google" in by_name
    assert "DevFest" in by_name
    assert "San Francisco" in by_name
    assert "Alpha is" not in by_name
    assert "cal" not in by_name


@pytest.mark.asyncio
async def test_vertical_ingestion_graph_retrieval_and_idempotency(settings, storage):
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(settings, storage, graph)
    text = (
        "Project Alpha is an important knowledge management project. "
        "The company Google supports the conference called DevFest in San Francisco."
    )
    first = await pipeline.ingest_text(
        "alice",
        text,
        source="telegram",
        source_ref="message-42",
        force_knowledge=True,
    )
    assert first["promoted"] is True
    ko = first["knowledge_object"]
    assert ko["raw_object_id"] == first["raw_object_id"]
    assert first["graph_links"]
    assert storage.get_inbox_item(first["inbox_id"], "alice") is not None
    assert graph.get_stats("alice")["knowledge_object_count"] == 1

    replay = await pipeline.ingest_text(
        "alice",
        text,
        source="telegram",
        source_ref="message-42",
        force_knowledge=True,
    )
    assert replay["idempotent_replay"] is True
    assert replay["raw_object_id"] == first["raw_object_id"]
    assert graph.get_stats("alice")["knowledge_object_count"] == 1

    searcher = HybridSearcher(storage)
    result = await searcher.search("alice", "DevFest knowledge", kg=graph)
    assert result["count"] >= 1
    assert result["results"][0]["id"] == ko["id"]
    assert result["strategy"]["feedback"] is True
    assert await searcher.search("bob", "DevFest", kg=graph) == {
        "query": "DevFest",
        "results": [],
        "count": 0,
        "entity_matches": [],
        "strategy": {
            "fts": True,
            "lexical": True,
            "embeddings": False,
            "feedback": True,
            "graph": True,
        },
    }


def test_best_snippet_returns_query_matched_passage_not_head():
    head = "нейтральный вводный текст без ключевых слов. " * 12  # ~530 chars, no query terms
    fact = "IP сервера Atlas равен 10.0.0.7 в дата-центре Москвы."
    text = head + fact + " " + ("прочий хвост. " * 40)

    snippet = best_snippet("IP сервера Atlas", text, max_chars=200)

    # The matched passage is surfaced, not the (irrelevant) document head.
    assert "Atlas" in snippet and "10.0.0.7" in snippet
    assert snippet.startswith("…")  # a middle window, not the head
    assert len(snippet) <= 202  # max_chars + the two ellipses


def test_best_snippet_short_text_and_no_match_fallbacks():
    assert best_snippet("что угодно", "короткий текст") == "короткий текст"
    long_unmatched = "ааааа " * 200
    fallback = best_snippet("zzz", long_unmatched, max_chars=100)
    assert fallback.startswith("а") and fallback.endswith("…") and len(fallback) <= 101


def _quadratic_best_snippet(query: str, text: str, max_chars: int) -> str:
    """The original implementation, kept as an oracle for the linear rewrite."""
    from jericho.retrieval import _STOPWORDS, tokens_of

    body = (text or "").strip()
    if len(body) <= max_chars:
        return body
    query_tokens = {
        token.casefold()
        for token in tokens_of(query)
        if len(token) > 1 and token.casefold() not in _STOPWORDS
    }
    if not query_tokens:
        return body[:max_chars].rstrip() + "…"
    lowered = body.casefold()
    occurrences: list[tuple[int, str]] = []
    for token in query_tokens:
        start = 0
        while True:
            found = lowered.find(token, start)
            if found < 0:
                break
            occurrences.append((found, token))
            start = found + len(token)
    if not occurrences:
        return body[:max_chars].rstrip() + "…"
    occurrences.sort()
    best_pos, best_distinct = occurrences[0][0], -1
    for pos, _ in occurrences:
        covered = {tok for (position, tok) in occurrences if pos <= position < pos + max_chars}
        if len(covered) > best_distinct:
            best_distinct = len(covered)
            best_pos = pos
    start = max(0, best_pos - 64)
    snippet = body[start : start + max_chars].strip()
    return f"{'…' if start > 0 else ''}{snippet}{'…' if start + max_chars < len(body) else ''}"


def test_best_snippet_picks_the_same_window_as_the_original():
    """The linear rewrite must be an optimisation, not a behaviour change."""
    import random

    words = ["сервис", "api", "данные", "запрос", "система", "модуль", "конфиг", "узел", "x"]
    query = "api сервис данные запрос конфиг"
    for trial in range(120):
        random.seed(trial)
        body = " ".join(random.choice(words) for _ in range(random.randint(1, 400)))
        max_chars = random.choice([40, 120, 520, 600])
        assert best_snippet(query, body, max_chars=max_chars) == _quadratic_best_snippet(
            query, body, max_chars
        ), f"diverged on trial {trial}"


def test_best_snippet_is_linear_in_the_number_of_matches():
    """A big document must not freeze the backend.

    The window search rescanned every occurrence for every candidate start, so the
    cost grew with DOCUMENT SIZE rather than query length — and it runs
    synchronously on the event loop from `_build_initial_messages`. Measured on
    this machine before the rewrite: 0.23 s at 38 KB, 3.8 s at 149 KB, **90 s at
    750 KB**. One imported article therefore made the whole backend unresponsive
    for a minute and a half. After: 0.07 s at 750 KB.

    The bound below is deliberately loose (the old code needed ~40 s here) so this
    fails on a return to quadratic, not on a slow machine.
    """
    import random
    import time

    random.seed(11)
    words = ["сервис", "api", "данные", "запрос", "конфиг"]
    body = " ".join(random.choice(words) for _ in range(60_000))  # ~400 KB, dense matches
    started = time.perf_counter()
    snippet = best_snippet("api сервис данные запрос конфиг", body, max_chars=600)
    elapsed = time.perf_counter() - started
    assert len(snippet) <= 602
    assert elapsed < 2.0, f"best_snippet took {elapsed:.1f}s on {len(body)} chars — quadratic again?"


@pytest.mark.asyncio
async def test_a_capped_recall_pool_says_so(storage):
    """Zero results over 8000 objects and zero over 40 must not print the same.

    The fuzzy pool is bounded at 400 by default, so on a real corpus the lexical
    channel only ever sees the most important/recent slice — and the response said
    nothing about it. `strategy` already reports `embeddings_capped` for exactly
    this reason; the lexical side now does too, and the count is paid only when
    the pool comes back full.
    """
    from jericho.retrieval import HybridSearcher
    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("owner")
    for index in range(40):
        raw = RawObject(
            id=new_id("raw"),
            user_id="owner",
            source="test",
            source_ref=new_id("src"),
            raw_content=f"заметка про проект номер {index}",
            content_type="text",
        )
        storage.store_raw_object(raw)
        storage.store_knowledge_object(
            KnowledgeObject(
                id=new_id("ko"),
                user_id="owner",
                raw_object_id=raw.id,
                content=raw.raw_content,
                title=f"Заметка {index}",
                summary=raw.raw_content,
            )
        )

    roomy = await HybridSearcher(storage, pool_max=400).search("owner", "проект", limit=5)
    assert "lexical_pool_capped" not in roomy["strategy"]

    tight = await HybridSearcher(storage, pool_max=10).search("owner", "проект", limit=5)
    assert tight["strategy"]["lexical_pool_capped"] is True
    assert tight["strategy"]["lexical_pool_scanned"] == 10
    assert tight["strategy"]["corpus_size"] == 40
