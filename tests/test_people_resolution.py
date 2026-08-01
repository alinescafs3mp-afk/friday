"""Naming a person the way people actually name them.

An account is `telegram:telegram:100000001` in the database and «Иван» in a
sentence. Oversight is asked for in words — «что Иван загружал на прошлой
неделе» — so the words have to reach the account.

Getting this wrong is worse than getting nothing: the answer is one person's
private material, so naming the wrong account shows it to someone who asked about
somebody else. Hence two rules the tests below hold to. Identifiers are never
fuzzy-matched — they differ by single characters on purpose. And a tie stays a
tie: `unambiguous` returns None rather than letting sort order decide.
"""

from __future__ import annotations

import pytest

from friday.people import resolve_person, unambiguous

PEOPLE = [
    {
        "id": "telegram:telegram:100000001",
        "display_name": "Иван",
        "username": "ivan",
        "external_id": "100000001",
    },
    {"id": "usr_anna", "display_name": "Анна Королёва", "username": "anna_k", "external_id": ""},
    {"id": "usr_pavel", "display_name": "Павел", "username": "pavel", "external_id": ""},
]


def _top(query: str):
    matches = resolve_person(PEOPLE, query)
    return matches[0] if matches else None


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Иван", "telegram:telegram:100000001"),
        ("иван", "telegram:telegram:100000001"),
        ("ivan", "telegram:telegram:100000001"),
        ("Ивану", "telegram:telegram:100000001"),  # dative
        ("Ивана", "telegram:telegram:100000001"),  # genitive
        ("Иавн", "telegram:telegram:100000001"),  # typo
        ("bdfy", "telegram:telegram:100000001"),  # «иван» on a QWERTY layout
        ("100000001", "telegram:telegram:100000001"),  # the raw external id
        ("Анна", "usr_anna"),
        ("Королева", "usr_anna"),  # ё folded away
        ("Королёва", "usr_anna"),
        ("Павел", "usr_pavel"),
    ],
)
def test_a_person_is_found_by_the_name_people_use(query, expected):
    match = _top(query)
    assert match is not None, f"{query!r} matched nobody"
    assert match.user_id == expected, f"{query!r} -> {match.user_id} ({match.method})"


def test_an_identifier_is_never_fuzzy_matched():
    """One digit off is a different account, not a typo to be helpful about."""
    matches = resolve_person(PEOPLE, "467035773")
    assert not [m for m in matches if m.matched_on.endswith("100000001")], (
        "a near-miss on an identifier resolved to an account"
    )


def test_an_unknown_name_matches_nobody():
    assert resolve_person(PEOPLE, "Бенедикт") == []
    assert resolve_person(PEOPLE, "") == []


def test_a_tie_is_reported_rather_than_decided():
    twins = [
        {"id": "usr_a", "display_name": "Саша", "username": "sasha_a", "external_id": ""},
        {"id": "usr_b", "display_name": "Саша", "username": "sasha_b", "external_id": ""},
    ]
    matches = resolve_person(twins, "Саша")
    assert len(matches) == 2
    assert unambiguous(matches) is None, "one of two identical names was picked silently"


def test_a_clear_winner_is_returned():
    matches = resolve_person(PEOPLE, "Иван")
    winner = unambiguous(matches)
    assert winner is not None
    assert winner.user_id == "telegram:telegram:100000001"


KNOWN_METHODS = {"exact", "inflected", "inflected_stem", "partial_name", "typo"}


def test_every_match_says_how_it_was_made():
    """The route is part of the answer: a caller weighing an exact hit against a
    guessed one needs to be told which it got. Which route wins for a given spelling
    is not pinned — several can reach the same person, and the strongest should."""
    for query in ("Иван", "Ивану", "Иавн", "bdfy", "Королева"):
        match = _top(query)
        assert match is not None, f"{query!r} matched nobody"
        parts = set(match.method.split("+"))
        assert parts <= KNOWN_METHODS | {"layout", "translit"}, match.method
        assert parts & KNOWN_METHODS, f"{query!r} reported no route: {match.method}"

    assert _top("Иван").method == "exact", "an exact spelling must not be reported as a guess"


# The account that actually exists on the live instance is named «Ivan» in Latin
# while the owner writes «Иван». That is not a keyboard layout — the same keys give
# «bdfy» — it is a transliteration, and it needs the case ending off first: «ивану»
# romanised as-is is «ivanu», which the Russian stemmer can no longer touch.
LATIN_NAMED = [
    {"id": "telegram:telegram:100000001", "display_name": "Ivan", "username": "", "external_id": "100000001"}
]

CYRILLIC_ONLY = [{"id": "usr_pavel", "display_name": "Павел Ильин", "username": "", "external_id": ""}]


@pytest.mark.parametrize("query", ["Иван", "иван", "Ивану", "Ивана", "Иваном", "Иване"])
def test_a_latin_display_name_is_reachable_in_cyrillic(query):
    matches = resolve_person(LATIN_NAMED, query)
    assert matches, f"{query!r} did not reach the Latin-named account"
    assert matches[0].user_id == "telegram:telegram:100000001"
    assert "translit" in matches[0].method


@pytest.mark.parametrize("query", ["Павел", "Павлу", "Павла", "Ильин", "Ильина"])
def test_a_cyrillic_name_is_reachable_in_any_case(query):
    matches = resolve_person(CYRILLIC_ONLY, query)
    assert matches, f"{query!r} did not reach the account"
    assert matches[0].user_id == "usr_pavel"


def test_transliteration_does_not_invent_people():
    assert resolve_person(LATIN_NAMED, "Бенедикт") == []
    assert resolve_person(CYRILLIC_ONLY, "Светлана") == []


def test_a_full_name_is_reachable_by_its_first_part():
    match = _top("Анна")
    assert match is not None and match.user_id == "usr_anna"
    assert match.confidence < 1.0, "a partial name should not claim to be exact"


# --- what this cannot do, stated rather than hidden ----------------------


def test_a_stem_collision_between_two_real_names_is_a_known_limit():
    """«Павлу» is the dative of «Павел» AND one edit from the nominative «Павла».

    The stemmer maps «павлу» and «павла» to the same «павл» while «Павел» keeps its
    fleeting vowel, so a directory holding both names resolves this to the wrong one.
    Fixing it needs a real morphological dictionary, not a bigger edit budget. The
    safety net is elsewhere and is what actually matters: the caller never acts
    silently — it reports which account it picked and how, so a wrong pick is visible
    in the answer rather than buried in it.
    """
    both = [
        {"id": "usr_pavel", "display_name": "Павел", "username": "", "external_id": ""},
        {"id": "usr_pavla", "display_name": "Павла", "username": "", "external_id": ""},
    ]
    match = unambiguous(resolve_person(both, "Павлу"))
    assert match is not None
    assert match.user_id == "usr_pavla"  # documented, not endorsed
    assert match.method != "exact", "a guess must never be reported as an exact match"


def test_measured_over_a_directory_of_similar_names():
    """Forty real Russian given names, many one edit apart. No wrong exact answers."""
    names = [
        "Иван",
        "Роман",
        "Рената",
        "Раиса",
        "Марина",
        "Дина",
        "Инна",
        "Нина",
        "Анна",
        "Павел",
        "Савелий",
        "Пётр",
        "Олег",
        "Олеся",
        "Ольга",
        "Игорь",
        "Егор",
        "Мария",
        "Дарья",
        "Дария",
        "Марья",
        "Наталья",
        "Наталия",
        "Виктор",
        "Виталий",
        "Валерий",
        "Валерия",
        "Сергей",
        "Андрей",
        "Алексей",
        "Александр",
        "Александра",
        "Максим",
        "Максимилиан",
        "Денис",
        "Данис",
        "Данила",
    ]
    rows = [
        {"id": f"usr_{index}", "display_name": name, "username": "", "external_id": ""}
        for index, name in enumerate(names)
    ]
    for index, name in enumerate(names):
        match = unambiguous(resolve_person(rows, name))
        assert match is not None, f"{name!r} became a question despite being spelled exactly"
        assert match.user_id == f"usr_{index}", f"{name!r} resolved to {match.display_name}"
