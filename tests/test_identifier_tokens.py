"""An identifier written at the end of a sentence must still be the same identifier.

The retrieval tokenizer lets ``. _ + # -`` continue a token so that ``file.txt``,
``BRK.A`` and ``scale_factor`` survive as units. That is right, but it also meant a
token ending a sentence kept the full stop — and since ``_identifier_coverage`` drops
any candidate whose query identifiers are not all present as whole tokens, a document
was unreachable by the exact identifier it contained.

Found by the retrieval bench, then reduced to two documents: FTS returned the hit and
``HybridSearcher`` returned nothing, reporting ``identifier_mismatch``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from friday.knowledge_graph import KnowledgeGraph
from friday.retrieval import HybridSearcher, tokens_of
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _store(storage, title: str, content: str) -> str:
    raw = storage.store_raw_object(
        RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="upload",
            source_ref=f"sha256:{new_id('x')}",
            raw_content=content,
            content_type="text/plain",
            content_hash=new_id("h") * 2,
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    stored = storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            entity_id=None,
            title=title,
            summary=content[:120],
            content=content,
            knowledge_kind="note",
            importance=0.5,
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    return stored.id


@pytest.mark.parametrize(
    "text,expected",
    [
        ("конец предложения.", ["конец", "предложения"]),
        # `ё` folds to `е`: the same word is written both ways in Russian, and the
        # index/query pair must agree. See `_YO_FOLD`.
        ("подъём autovacuum_vacuum_scale_factor.", ["подъем", "autovacuum_vacuum_scale_factor"]),
        ("подъём", tokens_of("подъем")),
        ("версия 1.2.3.", ["версия", "1.2.3"]),
        ("тире- и точка.", ["тире", "и", "точка"]),
        # Inside a token these characters are meaning, not punctuation.
        ("файл.txt и BRK.A", ["файл.txt", "и", "BRK.A"]),
        # C++ and C# genuinely end in those characters, so they are never stripped.
        ("C++ и C#", ["C++", "и", "C#"]),
    ],
)
def test_trailing_punctuation_is_not_part_of_a_token(text, expected):
    assert tokens_of(text) == expected


def test_an_identifier_ending_a_sentence_is_still_found(settings, storage):
    """The defect, end to end: FTS had the hit and the blend discarded it."""
    storage.ensure_user("alice", source="upload")
    target = _store(
        storage,
        "pg-vacuum.md",
        "Помог VACUUM FULL и подъём autovacuum_vacuum_scale_factor. Партиционировать по месяцам.",
    )
    _store(storage, "kazan.md", "Попробовать эчпочмак и дойти до Свияжска.")

    query = "autovacuum_vacuum_scale_factor"
    assert storage.search_knowledge("alice", query, limit=10), "precondition: FTS must match"

    searcher = HybridSearcher(storage, record_usage=False)
    results = asyncio.run(searcher.search("alice", query, limit=10, kg=KnowledgeGraph(storage)))["results"]

    assert [hit["id"] for hit in results] == [target]


def test_a_query_identifier_must_still_match_a_whole_token(settings, storage):
    """The discrimination the rule exists for is preserved.

    ``scale_factor`` is a substring of ``autovacuum_vacuum_scale_factor``, not the same
    identifier — the same reason a query for ``BRK.A`` must not be satisfied by
    ``XBRK.A``. Stripping trailing punctuation must not quietly turn identifier
    matching into substring matching.
    """
    storage.ensure_user("alice", source="upload")
    _store(storage, "pg-vacuum.md", "Подъём autovacuum_vacuum_scale_factor. Готово.")

    searcher = HybridSearcher(storage, record_usage=False)
    results = asyncio.run(searcher.search("alice", "scale_factor", limit=10, kg=KnowledgeGraph(storage)))[
        "results"
    ]

    assert results == []


def test_a_long_question_does_not_lose_the_term_that_identifies_the_answer(storage):
    """FTS terms were the first twelve tokens *in text order*, before any filtering.

    A Russian question front-loads «как», «почему», «пожалуйста», «именно» — words
    every document contains — and the identifier that names the answer comes last.
    A 14-token question therefore spent its entire budget on filler and the
    identifier never reached the index, so the one document that contained it was
    unreachable by its own exact term.

    Stopwords are dropped only when the query is over budget. Doing it always cost
    real recall: `tools/retrieval_bench.py` fell 0.583 → 0.458 (paraphrase
    0.50→0.17, synonym 0.40→0.20), because for a paraphrase the common words are
    the only lexical bridge there is.
    """
    import re

    from friday.storage._knowledge import _FTS_TERM_BUDGET, _fts_terms
    from friday.storage.models import KnowledgeObject, RawObject, new_id

    storage.ensure_user("owner")

    def add(text: str, title: str, importance: float) -> str:
        raw = RawObject(
            id=new_id("raw"),
            user_id="owner",
            source="test",
            source_ref=new_id("src"),
            raw_content=text,
            content_type="text",
        )
        storage.store_raw_object(raw)
        ko = KnowledgeObject(
            id=new_id("ko"),
            user_id="owner",
            raw_object_id=raw.id,
            content=text,
            title=title,
            summary=text[:120],
            importance=importance,
        )
        storage.store_knowledge_object(ko)
        return ko.id

    target = add(
        "Настройка PostgreSQL: параметр autovacuum_vacuum_scale_factor управляет тем, "
        "как часто запускается автоочистка на большой таблице.",
        "PostgreSQL autovacuum",
        0.1,  # deliberately low: it must win on the term, not on importance
    )
    for index in range(60):
        add(
            f"Планёрка {index}. Обсудили как и почему в этом квартале у нас на проекте "
            f"что-то надо делать и какие для этого есть задачи по этому самому поводу.",
            f"Планёрка {index}",
            0.9,
        )

    question = (
        "подскажи пожалуйста как именно в нашей базе на сервере правильно настроить "
        "тот самый параметр autovacuum_vacuum_scale_factor"
    )
    previous = re.findall(r"[\w#+.-]{2,}", question, flags=re.UNICODE)[:12]
    assert "autovacuum_vacuum_scale_factor" not in previous  # the defect, stated

    chosen = _fts_terms(question)
    assert "autovacuum_vacuum_scale_factor" in chosen
    # The budget counts WORDS. `чёрных` and `черных` are one word in two spellings
    # (see `_yo_spellings`), added after the budget so a spelling never costs a
    # distinct word its slot.
    from friday.retrieval import _YO_FOLD

    assert len({term.translate(_YO_FOLD) for term in chosen}) <= _FTS_TERM_BUDGET

    results = storage.search_knowledge("owner", question, limit=10)
    assert results, "the identifier still does not reach the index"
    assert results[0]["id"] == target

    # A query within budget keeps every token, stopwords included — that is what
    # the bench measures, and it must not change.
    short = "как чинить кластер"
    assert _fts_terms(short) == ["как", "чинить", "кластер"]


def test_a_comparison_of_two_identifiers_is_not_answered_with_silence(settings, storage):
    """«Чем X отличается от Y» отвечается документом про одну из сторон.

    Правило «покрыть ВСЕ идентификаторы запроса» верно для одного идентификатора и
    гарантирует пустоту для сравнительного вопроса: документа сразу про обе стороны
    может не быть вовсе. Замерено на боевом корпусе — «как АК-12 отличается от
    АК-74М» возвращало НОЛЬ результатов из тридцати кандидатов, включая профильный
    «prezent АК 12_отличительные особенности v4.pdf», который шёл первым по
    релевантности (embedding 0.66) и был отброшен с coverage 0.5.

    Пустая выдача здесь хуже частичной: это утверждение «в архиве ничего нет», и
    оно неверно.

    Мутация: вернуть порог 1.0 для нескольких идентификаторов — тест краснеет.
    """
    storage.ensure_user("alice", source="upload")
    about_first = _store(
        storage,
        "ak-12.md",
        "АК-12 — отличительные особенности: новый приклад, планка Пикатинни, отдача.",
    )
    _store(storage, "kazan.md", "Попробовать эчпочмак и дойти до Свияжска.")

    searcher = HybridSearcher(storage, record_usage=False)
    results = asyncio.run(
        searcher.search("alice", "как АК-12 отличается от АК-74М", limit=10, kg=KnowledgeGraph(storage))
    )["results"]

    assert results, "сравнительный вопрос получил пустую выдачу"
    assert about_first in [hit["id"] for hit in results], (
        "документ про одну из сравниваемых сторон не показан"
    )


def test_a_single_identifier_still_demands_a_full_match(settings, storage):
    """Защита, ради которой правило существует, цела.

    Послабление касается запросов с несколькими идентификаторами, но защита здесь
    держится не на ветвлении по их числу: у ОДНОГО идентификатора покрытие бинарно
    (он либо есть в документе целым токеном, либо нет), поэтому запрос про BRK.A не
    удовлетворяется записью про BRK.B при любом пороге выше нуля. Проверено
    мутацией: применение послабления к одиночному идентификатору этот тест НЕ
    роняет — и это факт о механике, а не пробел в проверке.

    Тест остаётся регрессией на саму защиту: BRK.A и BRK.B — разные бумаги.
    """
    storage.ensure_user("alice", source="upload")
    _store(storage, "brk-b.md", "Отчёт по BRK.B за третий квартал: выкуп акций продолжен.")

    searcher = HybridSearcher(storage, record_usage=False)
    results = asyncio.run(searcher.search("alice", "BRK.A", limit=10, kg=KnowledgeGraph(storage)))["results"]

    assert results == [], "запрос про BRK.A удовлетворён записью про BRK.B"
