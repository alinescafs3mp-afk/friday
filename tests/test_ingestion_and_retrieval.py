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


def _label_per_row_table() -> str:
    """The live defect's shape: a field label repeated in EVERY row, the queried
    surname in exactly one, deep inside. Synthetic — no owner data."""
    rows = [f"Человек{i:02d} П.С. | личный номер АА-1{i:04d} | часть {i}" for i in range(50)]
    rows[40] = "Тарасов П.С. | личный номер АА-77777 | часть 40"
    return "\n".join(rows)


def test_best_snippet_rare_surname_beats_field_label_repeated_per_row():
    """Measured on the owner's live archive: the label «личный номер» matched in
    all 58 rows, the surname in one, their contribution was equal, the window
    drifted into the densest label cluster and the answer carried ANOTHER
    PERSON's number — with grounding and citation checks honestly green."""
    snippet = best_snippet("личный номер Тарасов", _label_per_row_table(), max_chars=520)

    assert "Тарасов" in snippet and "АА-77777" in snippet
    # Neighbouring records are NOT glued into the excerpt…
    assert "АА-10039" not in snippet and "АА-10041" not in snippet
    # …and the head of the table (the old winning cluster) is not shown either.
    assert "АА-10000" not in snippet
    # The rarest term is inside, so no missing-term note.
    assert "\n[" not in snippet


def test_best_snippet_declined_query_surname_finds_the_nominative_row():
    """«личный номер Тарасова» must find the row that stores «Тарасов»: the stem
    strips the case ending and is searched as a prefix substring."""
    snippet = best_snippet("личный номер Тарасова", _label_per_row_table(), max_chars=520)

    assert "Тарасов" in snippet and "АА-77777" in snippet


def test_best_snippet_prepends_a_provable_header_and_shows_only_the_right_row():
    """In a header-style table the label lives ONLY in the header. The header must
    not win the window for itself (two label words would outscore the one-word
    surname row); it is glued back under the winning row so the model can NAME
    the column it reads — but only a provably digit-free first line qualifies."""
    header = "Фамилия | Личный номер | Подразделение"
    rows = [header] + [f"Человек{i:02d} | АА-1{i:04d} | часть {i}" for i in range(30)]
    rows[20] = "Тарасов | АА-77777 | часть 20"
    snippet = best_snippet("личный номер Тарасова", "\n".join(rows), max_chars=520)

    assert "Тарасов" in snippet and "АА-77777" in snippet
    assert "Фамилия" in snippet  # the header is shown…
    assert "АА-10000" not in snippet  # …but no foreign rows,
    assert "АА-10018" not in snippet and "АА-10020" not in snippet  # not even the neighbours
    assert "\n[" not in snippet


def test_best_snippet_does_not_glue_a_digit_bearing_first_row_as_a_header():
    """A first table line with digits is somebody's record, not a header — gluing
    it back in would recreate the very defect this code exists to fix."""
    snippet = best_snippet("личный номер Тарасов", _label_per_row_table(), max_chars=520)

    assert "АА-10000" not in snippet  # first row of the block stays out


def test_best_snippet_notes_the_rarest_term_it_could_not_include():
    """When the window that wins cannot contain the rarest matched term, the
    excerpt must say so instead of silently presenting itself as complete."""
    filler = "наполнитель прочего текста ради расстояния между кусками. " * 6
    body = (
        "дельта упомянута здесь один раз. "
        + filler
        + "альфа бета гамма стоят рядом. и снова альфа бета гамма стоят рядом."
    )
    snippet = best_snippet("альфа бета гамма дельта", body, max_chars=120)

    core, _, note = snippet.rpartition("\n")
    assert note.startswith("[слово «дельта»") and note.endswith("не попало]")
    assert "дельта" not in core  # the note is truthful: the term is not in the window


def _naive_select_window(
    occurrences: list[tuple[int, str]],
    weights: dict[str, float],
    segments: list[tuple[int, int, bool]],
    max_chars: int,
) -> tuple[int, int, int]:
    """Quadratic oracle for the linear two-pointer window search.

    Same semantics, written the obvious way: for every candidate anchor, rescan
    every occurrence inside the window. The linear rewrite exists because this
    is O(matches²); the oracle exists to pin that the rewrite chooses the SAME
    window — including the header-skip pass and the earliest-of-equals rule.
    """
    best_score = -1.0
    best = (occurrences[0][0], 0, occurrences[0][0] + max_chars)
    for skip_headers in (True, False):
        for seg_lo, seg_hi, is_header in segments:
            if skip_headers and is_header:
                continue
            for pos, _form in occurrences:
                if not (seg_lo <= pos < seg_hi):
                    continue
                inside = {form for p, form in occurrences if pos <= p < min(pos + max_chars, seg_hi)}
                score = sum(weights[form] for form in inside)
                if score > best_score + 1e-12:
                    best_score = score
                    best = (pos, seg_lo, seg_hi)
        if best_score >= 0.0:
            break
    return best


def test_best_snippet_window_choice_matches_quadratic_oracle():
    """The linear window search must equal the obvious quadratic one — on prose
    (single segment) and on bar-tables (per-row segments, header skipped)."""
    import random
    from collections import Counter

    from jericho.retrieval import (
        _STOPWORDS,
        _record_line_segments,
        _search_form,
        _select_window,
        tokens_of,
    )

    words = ["сервис", "api", "данные", "запрос", "система", "модуль", "конфиг", "узел", "x"]
    query = "api сервис данные запрос конфиг"
    forms: dict[str, str] = {}
    for token in tokens_of(query):
        folded = token.casefold()
        if len(token) > 1 and folded not in _STOPWORDS:
            forms.setdefault(_search_form(folded), token)
    for trial in range(150):
        random.seed(trial)
        if trial % 3:
            body = " ".join(random.choice(words) for _ in range(random.randint(1, 400)))
        else:
            rows = [" | ".join(random.choice(words) for _ in range(3)) for _ in range(random.randint(3, 40))]
            body = "\n".join(rows)
        max_chars = random.choice([40, 120, 520, 600])
        lowered = body.casefold().replace("ё", "е")
        occurrences: list[tuple[int, str]] = []
        for form in forms:
            start = 0
            while (found := lowered.find(form, start)) >= 0:
                occurrences.append((found, form))
                start = found + len(form)
        if not occurrences:
            continue
        occurrences.sort()
        counts = Counter(form for _, form in occurrences)
        weights = {form: 1.0 / count for form, count in counts.items()}
        segments = _record_line_segments(body, occurrences)
        assert _select_window(occurrences, weights, segments, max_chars) == _naive_select_window(
            occurrences, weights, segments, max_chars
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
