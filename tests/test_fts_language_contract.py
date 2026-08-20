"""The primary FTS indexes and Russian morphology are two distinct contracts.

``porter`` in SQLite is an English tokenizer.  Friday does not use it: source
text stays in unicode61 external-content indexes, while Russian Snowball roots
are produced at query time and searched as prefixes.  A character-trigram
index may be useful as a separate fuzzy lane one day, but replacing the word
indexes with it would silently change ranking and storage size.
"""

from __future__ import annotations

from friday.storage._knowledge import _fts_terms


def test_primary_fts_indexes_are_unicode_words_not_english_porter_or_trigrams(storage) -> None:
    rows = storage.execute(
        """SELECT name, sql FROM sqlite_master
             WHERE type='table' AND name IN ('knowledge_fts','raw_fts','messages_fts')
             ORDER BY name"""
    ).fetchall()

    assert [str(row["name"]) for row in rows] == ["knowledge_fts", "messages_fts", "raw_fts"]
    for row in rows:
        definition = " ".join(str(row["sql"] or "").casefold().split())
        assert "unicode61 remove_diacritics 2" in definition
        assert "porter" not in definition
        assert "trigram" not in definition


def test_russian_snowball_roots_are_wired_into_fts_query_terms() -> None:
    assert _fts_terms("сообщениями документами штатка") == [
        "сообщен*",
        "документ*",
        "штатк*",
    ]
    assert _fts_terms("сообщения сообщение сообщений") == ["сообщен*"]
