"""Russian suffix stripping for the lexical channel.

Russian inflects: «Казань», «в Казани», «под Казанью» are one word to a reader
and three different strings to a token-matching ranker. Measured on the owner's
own corpus, that gap is not cosmetic — a document containing every word of the
query in an oblique case scored **0.0597** lexically, below the evidence floor,
and reached the answer only because it seeded the graph and its own entities
vouched for it back. The circularity was doing the work morphology should do.

This is the Snowball Russian stemmer (Porter's algorithm as published for
Russian), implemented on the standard tables. Chosen over a hand-rolled suffix
list because the failure mode of a home-made stemmer is silent over-conflation,
and this one has been read by more people than anything written here.

Deliberately narrow:

* only tokens that are ENTIRELY Cyrillic letters are stemmed. Identifiers
  (`BRK.A`, `PK-04-04`, `autovacuum_vacuum_scale_factor`) must survive verbatim —
  `identifier_coverage` drops a candidate that does not contain them literally.
* only tokens of at least FOUR characters, и ниже — нет. Порог был пять, и это
  стоило целого класса коротких, но важных слов: «акте», «акта», «цеха», «коде»,
  «года» не сводились к своей основе вовсе. Замерено на боевом корпусе — вопрос
  «что сказано в акте №77?» не находил только что принятый документ НИ НА
  КАКОЙ позиции, тогда как «акт 77» ставил его первым. После снижения порога
  тот же вопрос даёт первую позицию; на золотом наборе из 78 эталонов recall@10
  не изменился (0.7179), MRR сдвинулся на −0.0042 — критерий был объявлен до
  замера. От «дом» → «до» защищает не порог входа, а порог ВЫХОДА ниже: основа
  короче трёх букв не принимается, и слово возвращается как было.
* the FTS index is untouched. It stores what was written, and folding there is a
  schema change with its own rebuild story (see `chunk_scheme` for what that
  costs). The lexical channel computes both sides at query time, so it can fold
  today, symmetrically, with nothing stored.
"""

from __future__ import annotations

import re
from functools import lru_cache

_VOWELS = "аеиоуыэюя"
_CYRILLIC_WORD = re.compile(r"^[а-яё]+$")

# The tables are Snowball's, in the order the algorithm applies them. Each group
# is sorted longest-first so the first match is the longest one.
_PERFECTIVE_GERUND_1 = ("вшись", "вши", "в")  # only after а/я
_PERFECTIVE_GERUND_2 = ("ившись", "ывшись", "ивши", "ывши", "ив", "ыв")
_ADJECTIVE = (
    "ыми", "ими", "его", "ого", "ему", "ому", "ее", "ие", "ые", "ое", "ей", "ий",
    "ый", "ой", "ем", "им", "ым", "ом", "их", "ых", "ую", "юю", "ая", "яя", "ою", "ею",
)  # fmt: skip
_PARTICIPLE_1 = ("ющ", "нн", "вш", "ем", "щ")  # only after а/я
_PARTICIPLE_2 = ("ующ", "ивш", "ывш")
_REFLEXIVE = ("ся", "сь")
_VERB_1 = (
    "ешь", "нно", "ете", "йте", "ла", "на", "ли", "ем", "ло", "но", "ет", "ют",
    "ны", "ть", "й", "л", "н",
)  # fmt: skip
_VERB_2 = (
    "ейте", "уйте", "ила", "ыла", "ена", "ите", "или", "ыли", "ило", "ыло", "ено",
    "ует", "уют", "ены", "ить", "ыть", "ишь", "ей", "уй", "ил", "ыл", "им", "ым",
    "ен", "ят", "ит", "ыт", "ую", "ю",
)  # fmt: skip
_NOUN = (
    "иями", "ями", "ами", "иях", "ией", "иям", "ием", "ях", "ам", "ом", "ах", "ев",
    "ов", "ие", "ье", "еи", "ии", "ей", "ой", "ий", "ям", "ем", "ию", "ью", "ия",
    "ья", "а", "е", "и", "й", "о", "у", "ы", "ь", "ю", "я",
)  # fmt: skip
_SUPERLATIVE = ("ейше", "ейш")
_DERIVATIONAL = ("ость", "ост")

# Ниже этого слово не трогаем по умолчанию: окончание было бы бо́льшей частью
# слова. Лексический канал поиска просит порог 4 (см. ниже) — там фолдинг
# симметричен, а имена собственные ходят другим путём.
_MIN_STEM_INPUT = 5


def _regions(word: str) -> tuple[int, int]:
    """Snowball's RV and R2 as offsets into ``word``.

    RV: after the first vowel. R1: after the first vowel followed by a
    non-vowel. R2: the same rule applied again inside R1.
    """
    rv = len(word)
    for index, character in enumerate(word):
        if character in _VOWELS:
            rv = index + 1
            break

    def _after_vowel_consonant(start: int) -> int:
        for index in range(start, len(word) - 1):
            if word[index] in _VOWELS and word[index + 1] not in _VOWELS:
                return index + 2
        return len(word)

    r1 = _after_vowel_consonant(0)
    r2 = _after_vowel_consonant(r1)
    return rv, r2


def _strip(word: str, region: int, endings: tuple[str, ...], *, after: str = "") -> str | None:
    """Remove the longest ``endings`` match that lies inside ``region``.

    ``after`` restricts the match to endings preceded by one of those letters —
    Snowball's «group 1» rule, which is what keeps «дела» a noun and «делая» a
    gerund.
    """
    for ending in endings:
        if not word.endswith(ending):
            continue
        cut = len(word) - len(ending)
        if cut < region:
            continue
        if after:
            if cut == 0 or word[cut - 1] not in after:
                continue
            return word[: cut - 1] + word[cut - 1]  # the а/я stays with the stem
        return word[:cut]
    return None


#: Порог для лексического канала поиска. «акте», «цеха», «коде», «года» — это
#: обычные слова документов, и их падежи терять дороже, чем огрублять «поле» до
#: «пол»: запрос и документ фолдятся ОДНИМ правилом, поэтому огрубление
#: симметрично. Именам этот порог не годится — «Иван» превращается в «ива», и
#: два разных человека становятся одним, поэтому граф и разметка упоминаний
#: остаются на пороге по умолчанию.
LEXICAL_MIN_STEM_INPUT = 4


@lru_cache(maxsize=200_000)
def stem(token: str, min_input: int = _MIN_STEM_INPUT) -> str:
    """Return the Snowball stem of a Russian word, or the token unchanged.

    Anything that is not a plain Cyrillic word of at least four letters comes
    back untouched — identifiers, numbers, Latin text and very short words.

    Memoized because `lexical_vector` is the hot path: it runs once per candidate
    over the candidate's FULL body, and profiling a single search over a
    400-object pool once counted 2708 calls to it, about half the request. Word
    types are few and word tokens are many — measured on a 51 KB body, stemming
    cost 34 ms uncached against 10 ms for no stemming at all, and 11 ms cached.
    The cache is keyed on the surface form and the function is pure, so a bounded
    LRU is all it needs.
    """
    word = token.casefold().replace("ё", "е")
    if len(word) < min_input or not _CYRILLIC_WORD.match(word):
        return token
    rv, r2 = _regions(word)

    # Step 1: a perfective gerund ends the search; otherwise try reflexive, then
    # adjectival / verb / noun, in that order and only the first that matches.
    step1 = _strip(word, rv, _PERFECTIVE_GERUND_2) or _strip(word, rv, _PERFECTIVE_GERUND_1, after="ая")
    if step1 is None:
        word = _strip(word, rv, _REFLEXIVE) or word
        adjective = _strip(word, rv, _ADJECTIVE)
        if adjective is not None:
            participle = _strip(adjective, rv, _PARTICIPLE_2) or _strip(
                adjective, rv, _PARTICIPLE_1, after="ая"
            )
            step1 = participle if participle is not None else adjective
        else:
            step1 = (
                _strip(word, rv, _VERB_2) or _strip(word, rv, _VERB_1, after="ая") or _strip(word, rv, _NOUN)
            )
    word = step1 if step1 is not None else word

    # Step 2: a trailing «и» in RV.
    if word.endswith("и") and len(word) - 1 >= rv:
        word = word[:-1]

    # Step 3: a derivational suffix, but only if it reaches into R2.
    derivational = _strip(word, r2, _DERIVATIONAL)
    if derivational is not None:
        word = derivational

    # Step 4: «нн» -> «н», or a superlative, or a trailing soft sign.
    if word.endswith("нн"):
        word = word[:-1]
    elif superlative := _strip(word, rv, _SUPERLATIVE):
        word = superlative[:-1] if superlative.endswith("нн") else superlative
    elif word.endswith("ь"):
        word = word[:-1]
    # A stem shorter than three letters is not evidence, it is a prefix: «дом» ->
    # «до» would match every second word in the corpus. Keep the original then.
    return word if len(word) >= 3 else token


# Snowball strips ONE ending per pass, and for some families that stops a step
# short: «Иванова» loses its «а» and becomes «иванов», while «Иванов» itself
# loses «ов» and becomes «иван». Two spellings of one surname, two stems.
_FIXPOINT_LIMIT = 4


def stem_to_fixpoint(token: str) -> str:
    """Stem until the word stops changing — a stronger fold for NAMES only.

    Used for `entities.normalized_name`, not for the lexical channel, because the
    two make opposite trades. A graph node is a thing: «Иванов», «Иванова» and
    «Иванову» are one person, and a node per grammatical case is a node that can
    be neither found nor counted. Retrieval, by contrast, ranks — over-folding
    there quietly widens what counts as a match, and the single pass is what was
    measured on the corpus.

    Measured on 77 real entities (47 on the stand, 30 in the owner's live graph)
    before it shipped: the extra passes changed nothing at all — the same two
    collisions, both of them the intended duplicate pairs, and zero false merges.
    """
    word = token
    for _ in range(_FIXPOINT_LIMIT):
        folded = stem(word)
        if folded == word:
            return word
        word = folded
    return word
