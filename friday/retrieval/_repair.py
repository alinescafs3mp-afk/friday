"""Understanding a question that was typed badly.

Three failures look identical to a token matcher and are completely different
things to the person typing:

* the layout was never switched — «uhfabr jngecrjd» is «график отпусков»;
* a finger slipped — «график дужурств»;
* the phone was in a pocket — «asdkjhqwe zxcmn».

The first two are questions the archive can answer and currently answers with
silence. The third is not a question at all, and the honest response is an empty
answer — which only works if the first two stop being mistaken for it.

The rule this module follows is that a repair must EARN its way in: a variant is
only used when the corpus recognises its words and the original's words are
unknown. Nothing is guessed from shape alone, because the cost of guessing wrong
is answering a different question than the one asked — worse than answering none.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from friday.retrieval._keyboard import switched

# A term shorter than this is not worth correcting: at three characters, every
# other word of the corpus is within one edit.
_MIN_CORRECTABLE = 5
# Edits allowed, by term length. Two edits on a short word is a different word.
_MAX_EDITS_SHORT = 1
_MAX_EDITS_LONG = 2
_LONG_TERM = 8
# How many corpus terms one unknown term may be compared against.
_CANDIDATE_LIMIT = 400


@dataclass(frozen=True)
class Repair:
    """A rewritten query and the reason, for the trace and for the user."""

    query: str
    kind: str  # "keyboard_layout" | "spelling"
    detail: str = ""


def _edit_distance(left: str, right: str, ceiling: int) -> int:
    """Levenshtein distance, abandoned as soon as it exceeds ``ceiling``.

    The ceiling is what keeps this cheap: the answer only matters when it is
    small, and quitting early turns the worst case from |a|x|b| into a band.
    """
    if abs(len(left) - len(right)) > ceiling:
        return ceiling + 1
    previous = list(range(len(right) + 1))
    for i, left_character in enumerate(left, start=1):
        current = [i]
        best = i
        for j, right_character in enumerate(right, start=1):
            cost = 0 if left_character == right_character else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        if best > ceiling:
            return ceiling + 1
        previous = current
    return previous[-1]


def _allowed_edits(term: str) -> int:
    return _MAX_EDITS_LONG if len(term) >= _LONG_TERM else _MAX_EDITS_SHORT


def _prefixes(term: str) -> list[str]:
    """Where in the vocabulary to look for what this term was meant to be.

    Its own first two letters catch every typo from the third character on, and
    the swapped pair catches the commonest first-letters mistake — «акзань» for
    «казань». Both are range scans on an indexed column.
    """
    if len(term) < 2:
        return [term]
    prefixes = [term[:2]]
    swapped = term[1] + term[0]
    if swapped != term[:2]:
        prefixes.append(swapped)
    return prefixes


def correct_terms(
    terms: Sequence[str],
    vocabulary: Callable[[Sequence[str]], Sequence[str]],
) -> dict[str, str]:
    """Map each unknown term to the corpus word it was probably meant to be.

    A term is replaced only when exactly one corpus word is closest to it; a tie
    means the archive itself cannot tell which was meant, and picking one would
    be inventing an answer.
    """
    corrections: dict[str, str] = {}
    for term in terms:
        if len(term) < _MIN_CORRECTABLE:
            continue
        ceiling = _allowed_edits(term)
        best_distance = ceiling + 1
        best: list[str] = []
        for candidate in vocabulary(_prefixes(term))[:_CANDIDATE_LIMIT]:
            if candidate == term:
                best, best_distance = [term], 0
                break
            distance = _edit_distance(term, candidate, ceiling)
            if distance > ceiling:
                continue
            if distance < best_distance:
                best_distance, best = distance, [candidate]
            elif distance == best_distance and candidate not in best:
                best.append(candidate)
        if best_distance == 0 or len(best) != 1:
            continue
        corrections[term] = best[0]
    return corrections


# A repaired reading must contain at least one word this length that the corpus
# actually uses. Below it, noise collides with real vocabulary too easily.
_MIN_MEANINGFUL_TERM = 4


def repair_query(
    query: str,
    *,
    substantive_terms: Callable[[str], list[str]],
    is_answerable: Callable[[str], bool],
    is_words: Callable[[Sequence[str]], bool],
    vocabulary: Callable[[Sequence[str]], Sequence[str]],
) -> Repair | None:
    """Return a better reading of ``query``, or None to leave it alone.

    Two tests, and a candidate must pass both. ``is_words`` asks whether the
    reading is made of words this corpus uses — that is what separates «график
    дежурств» from «[;op[;op rrrrr», which was accepted once because its
    two-letter fragment prefix-matched a log file. ``is_answerable`` then asks
    whether it finds anything for THIS user, which is also what keeps a
    correction borrowed from another tenant's vocabulary out of the results.
    """
    original_terms = substantive_terms(query)
    if not original_terms or is_answerable(query):
        return None  # nothing to repair, or nothing wrong

    def _accept(text: str) -> bool:
        return is_words(substantive_terms(text)) and is_answerable(text)

    # 1. The layout. Exact and reversible, so it is tried first — and accepted on
    #    the same evidence as everything else.
    flipped = switched(query)
    if flipped != query and _accept(flipped):
        return Repair(flipped, "keyboard_layout", detail=flipped)

    # 2. Spelling, against the corpus's own words. Applied per term, and only
    #    the terms that needed it change.
    corrections = correct_terms(original_terms, vocabulary)
    if corrections:
        repaired = query
        for wrong, right in corrections.items():
            repaired = repaired.replace(wrong, right)
        if repaired != query and _accept(repaired):
            detail = ", ".join(f"{wrong} → {right}" for wrong, right in corrections.items())
            return Repair(repaired, "spelling", detail=detail)

    # 3. A layout flip whose words also need correcting: «uhfabr le;ehcnd».
    if flipped != query:
        flipped_terms = substantive_terms(flipped)
        flipped_corrections = correct_terms(flipped_terms, vocabulary)
        if flipped_corrections:
            repaired = flipped
            for wrong, right in flipped_corrections.items():
                repaired = repaired.replace(wrong, right)
            if repaired != flipped and _accept(repaired):
                return Repair(repaired, "keyboard_layout", detail=repaired)

    return None  # not a question this archive can read: say so with an empty answer
