"""`ё` and `е` are one letter to a reader, and Russian is written both ways.

Found by asking the production searcher a question about the owner's own document.
The question said «чёрных списков», the document says «Черных Списков», and:

* `MATCH 'чёрных'` returned nothing where `MATCH 'черных'` returned the document;
* lexical similarity between question and answering sentence fell 0.482 -> 0.275.

Neither `unicode61 remove_diacritics 2` nor NFKC folds it — U+0451 is a letter in
its own right, not `е` with a combining mark — so nothing in the stack did it for
us. Phone keyboards produce `е`, careful writers and most published text use `ё`,
and one person mixes both inside a single note.
"""

from __future__ import annotations

import pytest

from friday.retrieval import lexical_vector, sparse_cosine, tokens_of
from friday.storage._knowledge import _fts_terms
from friday.storage.models import KnowledgeObject, RawObject, new_id


def _store(storage, user_id: str, title: str, text: str) -> str:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="text",
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=text,
        title=title,
        summary=text[:120],
    )
    storage.store_knowledge_object(ko)
    return ko.id


@pytest.mark.parametrize(
    "left,right",
    [
        ("чёрных списков", "черных списков"),
        ("подъём ёлки", "подъем елки"),
        ("Пётр", "Петр"),
    ],
)
def test_the_two_spellings_tokenize_the_same(left, right):
    assert tokens_of(left) == tokens_of(right)


def test_lexical_similarity_no_longer_depends_on_the_spelling():
    document = "Для Черных Списков (ЧС) выбирайте BLACK_VLESS_RUS_mobile.txt для телефона."
    with_yo = sparse_cosine(lexical_vector("для чёрных списков на телефон"), lexical_vector(document))
    without = sparse_cosine(lexical_vector("для черных списков на телефон"), lexical_vector(document))
    assert with_yo == pytest.approx(without)
    assert with_yo > 0.3


def test_the_fts_query_carries_both_spellings():
    """The index stored the text as written, so only the query passes through us.

    Folding the query alone would fix «пользователь напечатал ё, документ с е» and
    silently keep the mirror case broken. Terms are OR-ed, so the second spelling
    costs one alternative.
    """
    terms = _fts_terms("что выбрать для чёрных списков")
    assert "чёрных" in terms and "черных" in terms
    # A word without the letter is not duplicated.
    assert terms.count("списков") == 1


def test_a_variant_never_costs_a_distinct_word_its_budget_slot():
    query = "ёлка " + " ".join(f"слово{index}" for index in range(30))
    terms = _fts_terms(query)
    assert "ёлка" in terms and "елка" in terms
    # Every distinct word that made the budget is still one word, not one slot lost
    # to its own second spelling.
    assert len({term.replace("ё", "е") for term in terms}) >= 12


def test_a_document_written_without_yo_is_found_by_a_query_with_it(storage):
    storage.ensure_user("alice")
    target = _store(storage, "alice", "elka.md", "Купили елку на Новый год, поставили в гостиной у окна.")
    _store(storage, "alice", "other.md", "Ремонт балкона перенесли на весну, подрядчик занят.")

    found = [item["id"] for item in storage.search_knowledge("alice", "ёлку", limit=10)]
    assert target in found


def test_the_mirror_direction_is_covered_by_the_spelling_insensitive_legs():
    """FTS cannot reach a `ё` document from an `е` query, and does not have to.

    The index holds the text as written, so reaching it from the query side would
    mean guessing where the `ё` goes — `кластер` becomes `кластёр` on every Russian
    query. FTS is one recall leg: the lexical score folds BOTH sides through
    `tokens_of`, and embeddings never saw the letter. This pins the leg that does
    the work, so a regression in folding is caught even though FTS misses.
    """
    document = "Купили ёлку на Новый год, поставили в гостиной у окна."
    unrelated = "Ремонт балкона перенесли на весну, подрядчик занят."
    query = "купили елку"
    assert sparse_cosine(lexical_vector(query), lexical_vector(document)) > 0.3
    assert sparse_cosine(lexical_vector(query), lexical_vector(document)) > sparse_cosine(
        lexical_vector(query), lexical_vector(unrelated)
    )


def test_a_long_word_is_not_expanded_into_a_thicket():
    """A word with many `е` would cost 2^k alternatives to chase a spelling nobody writes."""
    from friday.storage._knowledge import _yo_spellings

    assert _yo_spellings("предложение") == ["предложение"]
    assert len(_yo_spellings("перенес")) <= 4
