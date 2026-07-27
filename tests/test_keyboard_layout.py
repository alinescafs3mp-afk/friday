"""Typing with the layout stuck is not a typo, it is the same keys.

«ghbdtn» and «привет» share no letters, so no edit-distance or trigram measure
brings them near each other — but they are one keystroke sequence. The mapping
is by key position, which makes it exact and reversible; these tests pin that it
stays exact, because a table with one shifted character corrupts every query
that passes through it and looks like nothing in particular.
"""

from __future__ import annotations

import pytest

from jericho.retrieval._keyboard import switched, to_english, to_russian


@pytest.mark.parametrize(
    ("typed", "meant"),
    [
        ("ghbdtn", "привет"),
        ("uhfabr lt;ehcnd", "график дежурств"),
        ("rjyabuehfwbz", "конфигурация"),
        ("Ghbrfp j lt;ehcndt", "Приказ о дежурстве"),
        (",tp ,evfub", "без бумаги"),
        ("dtljvjcnm", "ведомость"),
    ],
)
def test_english_keys_read_as_russian(typed, meant):
    assert to_russian(typed) == meant
    assert switched(typed) == meant


@pytest.mark.parametrize(("typed", "meant"), [("руддщ", "hello"), ("сфдд ьу", "call me")])
def test_russian_keys_read_as_english(typed, meant):
    assert to_english(typed) == meant
    assert switched(typed) == meant


def test_the_transformation_is_reversible():
    for text in ("график дежурств караула", "backup policy review", "Приказ №12"):
        assert to_english(to_russian(text)) == text or to_russian(to_english(text)) == text


def test_digits_and_unmapped_characters_survive():
    assert to_russian("ghbrfp 12/2026") == "приказ 12.2026"
    assert switched("2026") == "2026"


def test_the_table_pairs_every_key():
    """A shifted character here would corrupt every query it touches."""
    from jericho.retrieval import _keyboard

    for english_row, russian_row in _keyboard._ROWS:
        assert len(english_row) == len(russian_row), (english_row, russian_row)
    assert len(set(_keyboard._EN)) == len(_keyboard._EN), "a key mapped twice"
    assert len(set(_keyboard._RU)) == len(_keyboard._RU), "a key mapped twice"
