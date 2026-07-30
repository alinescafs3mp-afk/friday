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
    """The original implementation, kept as an oracle for the linear rewrite.

    The edges use the production boundary snapping: this oracle exists to pin WHICH
    WINDOW is chosen, and word-boundary trimming is a separate, deliberate change
    (see `test_a_snippet_never_starts_or_ends_mid_word`). Reimplementing the snapping
    here would make the oracle a copy of the code it checks; calling it keeps the
    comparison about the window search, which is what the rewrite touched.
    """
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
    from jericho.retrieval import _word_end, _word_start

    start = _word_start(body, max(0, best_pos - 64))
    end = _word_end(body, min(len(body), start + max_chars), floor=best_pos + 1)
    snippet = body[start:end].strip()
    return f"{'…' if start > 0 else ''}{snippet}{'…' if end < len(body) else ''}"


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


@pytest.mark.asyncio
async def test_a_weak_dense_score_alone_is_not_evidence(storage):
    """The `insufficient_evidence` floor is a property of the MODEL, not a constant.

    Measured against the production model (qwen3-embedding-0.6b) at the operating
    point that matters — a short query against a document body — 56 query ×
    unrelated-document pairs scored min 0.1032 / p50 0.2361 / p90 0.3255 / max
    0.3878, while 8 query × own-document pairs scored min 0.4188 / p50 0.5197.

    The shipped constant was 0.16, *below the median of the noise*: it admitted 48
    of those 56 unrelated documents — 85.7% — as dense evidence. The default is now
    0.35, which clears noise p90 with headroom under the weakest genuine match, and
    it is configurable because the next model will land somewhere else entirely.

    `tools/retrieval_bench.py` against the live model reads 0.8333 both before and
    after, category for category, so the tightening costs no recall on the gold set.
    """
    from jericho.retrieval import HybridSearcher
    from jericho.storage.models import KnowledgeObject, RawObject, new_id

    class _FixedCosine:
        """Returns vectors whose cosine with the query is exactly `similarity`."""

        remote_enabled = True

        def __init__(self, settings, similarity: float) -> None:
            self.settings = settings
            self._similarity = similarity

        async def embed(self, texts, *, budget_sec=None):
            import math

            # Keyed on the TEXT, not its position: the query is embedded in its own
            # call, so an index-based fake gives the first document cosine 1.0.
            angle = math.acos(max(-1.0, min(1.0, self._similarity)))
            document = [math.cos(angle), math.sin(angle)]
            return [[1.0, 0.0] if "отчётность" in text else document for text in texts]

    storage.ensure_user("owner")
    raw = RawObject(
        id=new_id("raw"),
        user_id="owner",
        source="test",
        source_ref=new_id("src"),
        raw_content="Совершенно посторонний документ о ремонте велосипеда",
        content_type="text",
    )
    storage.store_raw_object(raw)
    storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="owner",
            raw_object_id=raw.id,
            content=raw.raw_content,
            title="Велосипед",
            summary=raw.raw_content,
        )
    )

    # A query sharing no tokens with the document: lexical, field and graph are all
    # at zero, so the dense term is the only thing that can carry it.
    query = "квартальная отчётность"
    from dataclasses import replace as _replace

    settings_stub = _replace(storage.settings, embeddings_enabled=True)

    lenient = HybridSearcher(storage, _FixedCosine(settings_stub, 0.25), dense_evidence_min=0.16)
    strict = HybridSearcher(storage, _FixedCosine(settings_stub, 0.25), dense_evidence_min=0.35)

    kept = await lenient.search("owner", query, limit=5, explain=True)
    dropped = await strict.search("owner", query, limit=5, explain=True)

    reasons = {item["reason"] for item in dropped.get("trace", [])}
    assert kept["count"] == 1, "the premise: a 0.25 cosine used to be enough on its own"
    assert dropped["count"] == 0
    assert "insufficient_evidence" in reasons


def test_the_dense_evidence_floor_is_configurable(settings):
    """Число принадлежит модели И КОРПУСУ, поэтому оно настраивается.

    Значение поднято 0.35 → 0.40 после замера на настоящем архиве: 0.35 калибровался
    на синтетике, где чужие пары кончались на p90 = 0.3255, а на однородных русских
    служебных документах чужие доходят до p90 = 0.427. Замер на 770 чужих и 122 своих
    парах: 0.35 пропускает 28.8% чужих, 0.40 — 13.9% при сохранении 83.6% своих.

    Тест держит не само число, а то, что настройка и константа не разъезжаются:
    разъехавшись, они дадут одно поведение в коде и другое в документации.
    """
    from jericho.retrieval import _DENSE_EVIDENCE_MIN_DEFAULT

    assert settings.retrieval_dense_evidence_min == _DENSE_EVIDENCE_MIN_DEFAULT
    # Порог должен оставаться выше медианы шума, измеренной на настоящем корпусе.
    assert _DENSE_EVIDENCE_MIN_DEFAULT > 0.308


def test_a_snippet_never_starts_or_ends_mid_word():
    """A decapitated name is worse than no excerpt: it reads as a whole word.

    Found on the owner's own document. The question "какое приложение не решило
    проблему авторизации localhost" produced an excerpt beginning

        …dify ❌ - последнее обновление было 5 марта. Проблема авторизации
        localhost-порта не решена.

    The answer was right there and the name of the application — Hiddify — had been
    cut off by the window edge, which lands 64 characters before the matched token
    with no regard for what sits at that offset. Names, products and identifiers are
    exactly what an excerpt exists to carry, and they fall on the edges as often as
    anywhere else.
    """
    body = (
        "Проверка клиентов на проблему авторизации локального порта. "
        + "Наполнитель, чтобы окно не начиналось с начала документа. " * 4
        + "Пункт 7. Hiddify — последнее обновление было 5 марта, проблема "
        "авторизации localhost-порта не решена. "
        + "Дальше следует ещё текст про другие клиенты и настройки прокси. "
        * 6
    )
    snippet = best_snippet("проблема авторизации localhost", body, max_chars=200)

    assert "Hiddify" in snippet, snippet
    stripped = snippet.strip("…").strip()
    # Neither edge may sit inside a word.
    assert body.find(stripped) >= 0
    start = body.find(stripped)
    assert start == 0 or not (body[start - 1].isalnum() and body[start].isalnum())
    end = start + len(stripped)
    assert end == len(body) or not (body[end - 1].isalnum() and body[end].isalnum())


def test_boundary_snapping_cannot_be_dragged_across_the_document():
    """A URL or a base64 blob is one long "word"; the edge must give up, not walk.

    Without a scan limit the left edge walks to the start of the blob, and the whole
    window then sits inside it — bounded in LENGTH, and no longer containing the
    match it was built around. Length alone does not catch that, which is why the
    assertion is about the matched terms.
    """
    blob = "A" * 5000
    body = f"начало документа {blob} запрос конфиг " + "хвост " * 200
    snippet = best_snippet("запрос конфиг", body, max_chars=300)
    assert len(snippet) <= 300 + 2
    assert "запрос" in snippet and "конфиг" in snippet, snippet


def test_the_confident_threshold_is_configurable(settings):
    """Порог «похоже на ответ» принадлежит МОДЕЛИ: у другой шкала будет другой.

    Он ничего не отсекает — уходит числом в `strategy.rerank_confident`, чтобы система
    могла сказать «нашла пять, отвечает похоже что один». Замер размена (порог почти
    удваивает долю отвечающих, но стоит 16% вопросов, у которых ответ был) записан в
    `_rerank_backend.py`.

    Тест держит не число, а то, что настройка и константа не разъезжаются: импорт из
    config в retrieval невозможен, там цикл, поэтому значение записано дважды.
    """
    from jericho.retrieval._rerank_backend import CONFIDENT_MIN_DEFAULT

    assert settings.rerank_confident_min == CONFIDENT_MIN_DEFAULT
