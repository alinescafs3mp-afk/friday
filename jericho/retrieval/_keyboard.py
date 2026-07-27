"""Typing Russian on the English layout, and the other way round.

«ghbdtn» is «привет» typed without switching the keyboard, and «руддщ» is
«hello» typed with it switched. Both are ordinary human typing, both currently
find nothing at all, and neither looks like a typo to any fuzzy matcher — the
letters are not close to the intended ones, they are the same KEYS.

The mapping is by key position on the standard ЙЦУКЕН / QWERTY pair, so the
transformation is exact and reversible; there is nothing to guess. What does
need judging is WHETHER to apply it, and that decision is left to the caller:
this module reports what the text would be, `looks_mistyped` says whether the
shape is suspicious, and only the searcher — which can see whether a variant
actually finds anything — decides.
"""

from __future__ import annotations

# The standard layouts, row by row so the pairing can be checked by eye rather
# than by counting characters. Punctuation keys are included because a mistyped
# query keeps its separators: «привет,» arrives as «ghbdtn?».
_ROWS: tuple[tuple[str, str], ...] = (
    ("qwertyuiop[]", "йцукенгшщзхъ"),
    ("asdfghjkl;'", "фывапролджэ"),
    ("zxcvbnm,./", "ячсмитьбю."),
    ("`", "ё"),
    ("QWERTYUIOP{}", "ЙЦУКЕНГШЩЗХЪ"),
    ('ASDFGHJKL:"', "ФЫВАПРОЛДЖЭ"),
    ("ZXCVBNM<>?", "ЯЧСМИТЬБЮ,"),
    ("~", "Ё"),
)
for _english_row, _russian_row in _ROWS:  # a typo in a table is a silent defect
    assert len(_english_row) == len(_russian_row), (_english_row, _russian_row)

_EN = "".join(row for row, _ in _ROWS)
_RU = "".join(row for _, row in _ROWS)
_EN_TO_RU = str.maketrans(_EN, _RU)
_RU_TO_EN = str.maketrans(_RU, _EN)

_CYRILLIC = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ")
_LATIN = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")

# Letter pairs that Russian does not produce. A wrong-layout Latin string is
# random from English's point of view, so it collects clusters English never
# writes; the reverse is true for Cyrillic. Used only as a hint — see
# `looks_mistyped`.
_IMPOSSIBLE_LATIN = ("hb", "dt", "ghb", "ntk", "ktp", " jn", "ehk", "jhc", "ybr", "vjy")
_IMPOSSIBLE_CYRILLIC = ("руд", "цшер", "еру", "фయ", "ыву", "црд", "мшу")


def to_russian(text: str) -> str:
    """Read ``text`` as if the keys had been pressed on the Russian layout."""
    return text.translate(_EN_TO_RU)


def to_english(text: str) -> str:
    """Read ``text`` as if the keys had been pressed on the English layout."""
    return text.translate(_RU_TO_EN)


def switched(text: str) -> str:
    """The same keystrokes read on the other layout, whichever that is.

    Mixed text (a Russian phrase with one Latin word) flips whichever script
    dominates, because that is what a stuck layout produces: the whole phrase.
    """
    cyrillic = sum(1 for character in text if character in _CYRILLIC)
    latin = sum(1 for character in text if character in _LATIN)
    if latin >= cyrillic:
        return to_russian(text)
    return to_english(text)


def looks_mistyped(text: str) -> bool:
    """A cheap shape check: is it worth ASKING the index about the other layout?

    Deliberately permissive — a false positive costs one extra indexed query and
    is discarded when it finds nothing, while a false negative leaves the user
    with an empty answer to a question they did type. Only the obviously
    unnecessary case is refused: text with no letters to flip.
    """
    stripped = text.strip()
    if not stripped:
        return False
    return any(character in _CYRILLIC or character in _LATIN for character in stripped)
