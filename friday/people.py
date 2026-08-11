"""Finding the person someone is talking about, by the name they used.

An account is identified in the database by a string like
`telegram:telegram:100000001`. Nobody says that. They say «Иван», «у Ивана»,
«иван» typed in the wrong layout, or «Иавн» with a finger off by one — and the
question is which account that is.

This is the same problem the query repair solves for search, so it uses the same
pieces: ё folding, the Russian stemmer for oblique cases, the ЙЦУКЕН↔QWERTY table
for the wrong-layout classic, and a bounded Levenshtein for typos. What it adds is
that being WRONG here is worse than finding nothing: naming the wrong person means
showing one account's private material to someone asking about another. So every
match carries how it was made, and a tie is reported as a tie rather than resolved
by ordering.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from friday.morphology import stem
from friday.retrieval._keyboard import switched
from friday.retrieval._repair import _edit_distance

_YO_FOLD = str.maketrans({"ё": "е", "Ё": "Е"})
_NAME_TOKEN_RE = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ]+")

# A name shorter than this is not typo-corrected: at three characters almost
# everything is within one edit of everything else, and a wrong person is worse
# than no person.
_MIN_FUZZY_NAME = 4


@dataclass(frozen=True)
class PersonMatch:
    """One account that could be who was meant, and why it could be."""

    user_id: str
    display_name: str
    username: str
    confidence: float
    method: str
    matched_on: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "username": self.username,
            "confidence": round(self.confidence, 4),
            "method": self.method,
            "matched_on": self.matched_on,
        }


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).translate(_YO_FOLD).casefold().strip()


def _name_fold(text: str) -> str:
    """Human-name comparison form, without transport punctuation.

    Telegram handles are commonly pasted as ``@name`` while account rows store
    ``name``.  Display names also drift between ``Анна-Мария``, ``Анна Мария``
    and punctuation copied from a contact card.  Those separators are not
    identity-bearing, whereas punctuation inside a stable id can be.  Stable ids
    therefore stay on the exact-only channel below; this form is used only for
    display names and usernames.
    """

    return " ".join(_NAME_TOKEN_RE.findall(_fold(text).lstrip("@")))


# Sound, not key position. `switched` handles «набрал не в той раскладке» — the same
# physical keys — and that is a different problem from the one that actually turned
# up on the live instance: the account's Telegram display name is «Ivan» in Latin
# and the owner writes «Иван» in Cyrillic. No layout table maps between those; a
# transliteration does. Applied to BOTH sides, so it normalises whichever way round
# the pair happens to be.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch",
    "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}  # fmt: skip


def _translit(text: str) -> str:
    return "".join(_TRANSLIT.get(character, character) for character in text)


def _names_of(row: dict[str, Any]) -> list[tuple[str, str]]:
    """Every string that could name this account, with where it came from."""
    out: list[tuple[str, str]] = []
    for field in ("display_name", "username", "external_id", "id"):
        value = str(row.get(field) or "").strip()
        if value:
            out.append((value, field))
    # `telegram:telegram:100000001` — the trailing segment is the id a person may
    # actually paste, and the leading ones are plumbing.
    identifier = str(row.get("id") or "")
    if ":" in identifier:
        tail = identifier.rsplit(":", 1)[-1]
        if tail:
            out.append((tail, "id"))
    return out


def _stems(text: str) -> tuple[str, ...]:
    return tuple(stem(token) for token in _NAME_TOKEN_RE.findall(_name_fold(text)) if token)


def _is_transposition(left: str, right: str) -> bool:
    """Are these the same string with one adjacent pair swapped?

    Plain Levenshtein calls «Иавн» two edits away from «Иван» — a delete and an
    insert — so a one-edit budget misses the single most common way a name is
    mistyped. Damerau counts it as one. Checked here rather than in the shared
    `_edit_distance`, which is measured into the search behaviour and not mine to
    change from this side.
    """
    if len(left) != len(right) or left == right:
        return False
    differing = [index for index, (a, b) in enumerate(zip(left, right, strict=True)) if a != b]
    if len(differing) != 2:
        return False
    first, second = differing
    return second == first + 1 and left[first] == right[second] and left[second] == right[first]


def _ladder(folded_query: str, folded_name: str) -> tuple[float, str] | None:
    """The comparison itself, over two already-normalised strings."""
    if folded_query == folded_name:
        return (1.0, "exact")

    query_stems = _stems(folded_query)
    name_stems = _stems(folded_name)
    if query_stems and query_stems == name_stems:
        # «Ивану» against «Иван»: the same name in an oblique case.
        return (0.93, "inflected")
    if query_stems and name_stems and set(query_stems) <= set(name_stems):
        # «Иван» against «Иван Сафин»: a first name naming a full one.
        return (0.86, "partial_name")
    if (
        query_stems
        and name_stems
        and all(
            any(len(left) >= 3 and len(right) >= len(left) and right.startswith(left) for right in name_stems)
            for left in query_stems
        )
    ):
        # A contact is often named by an unfinished token: ``Алекс Кор``.
        # Prefixes are deliberately weaker than complete-name containment and a
        # same-tier neighbour remains an ambiguity in ``unambiguous``.
        return (0.82, "partial_name")
    if (
        query_stems
        and name_stems
        and all(
            any(
                len(left) >= _MIN_FUZZY_NAME
                and len(right) >= _MIN_FUZZY_NAME
                and _edit_distance(left, right, 1) <= 1
                for right in name_stems
            )
            for left in query_stems
        )
    ):
        # A fleeting vowel is not a typo and the stemmer cannot reach it: «Павел»
        # stems to «павел» while «Павла» stems to «павл». The same gap the retrieval
        # comments describe for «список»/«списка». One edit at the stem covers it.
        return (0.78, "inflected_stem")

    if len(folded_query) >= _MIN_FUZZY_NAME and len(folded_name) >= _MIN_FUZZY_NAME:
        if _is_transposition(folded_query, folded_name):
            return (0.72 - 0.08, "typo")
        budget = 1 if len(folded_query) < 8 else 2
        distance = _edit_distance(folded_query, folded_name, budget)
        if distance <= budget:
            return (0.72 - 0.08 * distance, "typo")
    return None


def _score_one(query: str, name: str, source: str) -> tuple[float, str] | None:
    """How well one query string matches one name, or None for no match at all."""
    if source in {"id", "external_id"}:
        return (1.0, "exact") if _fold(query) == _fold(name) and _fold(name) else None
    folded_query = _name_fold(query)
    folded_name = _name_fold(name)
    if not folded_query or not folded_name:
        return None

    direct = _ladder(folded_query, folded_name)
    if direct is not None:
        return direct

    def short_account_typo() -> tuple[float, str] | None:
        # Three-letter account nicknames are common in the live archive.  A
        # blanket four-character typo floor made ``GBL`` unable to resolve the
        # unique ``JBL`` account.  Keep this exception deliberately narrow:
        # one uppercase ASCII token, exactly one substitution, display/handle
        # fields only.  It is the weakest tier and still requires a unique
        # winner, so two nearby acronyms fail closed.
        compact_query = folded_query.replace(" ", "")
        compact_name = folded_name.replace(" ", "")
        visible_query = str(query or "").strip().lstrip("@")
        if (
            3 <= len(compact_query) <= 5
            and len(compact_query) == len(compact_name)
            and compact_query.isascii()
            and compact_query.isalpha()
            and compact_name.isascii()
            and compact_name.isalpha()
            and visible_query.isupper()
            and _edit_distance(compact_query, compact_name, 1) == 1
        ):
            return (0.55, "short_typo")
        return None

    # Same name, different alphabet. Only worth trying when the two sides are not
    # already in the same one — transliterating Latin leaves it unchanged, so this
    # costs nothing when it cannot help.
    romanised_query = _translit(folded_query)
    romanised_name = _translit(folded_name)
    if (romanised_query, romanised_name) == (folded_query, folded_name):
        return short_account_typo()
    # Case endings come off BEFORE the alphabet changes: the stemmer is Russian, so
    # «ивану» romanised first is «ivanu», which it cannot touch. Stemmed first it
    # is «иван» → «ivan», an exact hit on the Latin display name rather than a
    # lucky one-edit near miss.
    stemmed_query = _translit(" ".join(_stems(folded_query)))
    stemmed_name = _translit(" ".join(_stems(folded_name)))
    crossed = _ladder(stemmed_query, stemmed_name) or _ladder(romanised_query, romanised_name)
    if crossed is None:
        return short_account_typo()
    confidence, method = crossed
    return (confidence * 0.95, f"translit+{method}")


def resolve_person(rows: list[dict[str, Any]], query: str) -> list[PersonMatch]:
    """Rank the accounts that `query` could be naming, strongest first.

    Returns every plausible match rather than a winner. A caller that acts on this
    is about to show one person's material to another, so «two people are called
    that» has to reach them as a question, not be silently decided by sort order.
    """
    text = str(query or "").strip()
    if not text:
        return []

    # Stable ids and exact usernames win before any fuzzy name work.  Besides
    # being faster, this prevents a near display-name candidate from turning an
    # explicitly pasted ``@username`` or account id into an ambiguity.
    exact: dict[str, PersonMatch] = {}
    exact_query = _fold(text)
    exact_handle = _name_fold(text)
    for row in rows:
        user_id = str(row.get("id") or "")
        if not user_id:
            continue
        matched_on = ""
        if exact_query in {_fold(user_id), _fold(str(row.get("external_id") or ""))}:
            matched_on = user_id if exact_query == _fold(user_id) else str(row.get("external_id") or "")
        elif exact_handle and exact_handle == _name_fold(str(row.get("username") or "")):
            matched_on = str(row.get("username") or "")
        if matched_on:
            exact[user_id] = PersonMatch(
                user_id=user_id,
                display_name=str(row.get("display_name") or ""),
                username=str(row.get("username") or ""),
                confidence=1.0,
                method="exact",
                matched_on=matched_on,
            )
    if exact:
        return sorted(exact.values(), key=lambda item: item.user_id)

    attempts: list[tuple[str, str]] = [(text, "")]
    flipped = switched(text)
    if flipped and _fold(flipped) != _fold(text):
        # The classic: the name typed without switching the keyboard layout.
        attempts.append((flipped, "layout"))

    best: dict[str, PersonMatch] = {}
    for attempt, qualifier in attempts:
        for row in rows:
            user_id = str(row.get("id") or "")
            if not user_id:
                continue
            for name, source in _names_of(row):
                scored = _score_one(attempt, name, source)
                if scored is None:
                    continue
                confidence, method = scored
                if qualifier:
                    # A layout flip is a guess on top of a guess.
                    confidence *= 0.9
                    method = f"{qualifier}+{method}"
                if source in {"id", "external_id"} and method != "exact":
                    # Fuzzy-matching an identifier is how you reach the wrong account:
                    # identifiers differ by single characters ON PURPOSE.
                    continue
                current = best.get(user_id)
                if current is None or confidence > current.confidence:
                    best[user_id] = PersonMatch(
                        user_id=user_id,
                        display_name=str(row.get("display_name") or ""),
                        username=str(row.get("username") or ""),
                        confidence=confidence,
                        method=method,
                        matched_on=name,
                    )

    return sorted(best.values(), key=lambda item: (-item.confidence, item.user_id))


# How a match was made, strongest first. Comparing these is what decides a winner —
# not the score. The difference between «this is the name, inflected» and «this is
# one edit from the stem of the name» is categorical, and measured over forty real
# Russian given names a numeric margin could not express it: at 0.1 «Павлу» resolved
# confidently to «Павла» (both stem to «павл» while «Павел» keeps its fleeting
# vowel), and at 0.2 six of eight ordinary case forms became «which of them?».
_TIERS = ("exact", "inflected", "partial_name", "inflected_stem", "typo", "short_typo")

# Within one tier the scores are near-identical by construction, so this only has to
# separate a match from a genuinely equal one.
_SAME_TIER_MARGIN = 0.05


def _tier(match: PersonMatch) -> int:
    for part in match.method.split("+"):
        if part in _TIERS:
            return _TIERS.index(part)
    return len(_TIERS)


def unambiguous(matches: list[PersonMatch], *, margin: float = _SAME_TIER_MARGIN) -> PersonMatch | None:
    """The one clear winner, or None when the answer is «which of them?».

    A stronger KIND of match wins outright: «Александр» must not become a question
    merely because «Александра» is one edit away. Two matches of the same kind are a
    real tie, and a tie has to reach the caller as one — they are about to show one
    person's private material, and guessing shows it to somebody who asked about
    somebody else.
    """
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    best, runner_up = matches[0], matches[1]
    if _tier(best) < _tier(runner_up):
        return best
    if best.confidence - runner_up.confidence >= margin:
        return best
    return None
