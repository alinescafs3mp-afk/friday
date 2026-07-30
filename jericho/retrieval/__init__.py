"""Hybrid personal retrieval with lexical, FTS, optional embeddings, graph, and feedback signals."""

from __future__ import annotations

import array
import heapq
import json
import logging
import math
import re
import time
from bisect import bisect_right
from collections import Counter, OrderedDict
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import httpx

from jericho.config import JerichoSettings
from jericho.morphology import stem
from jericho.retrieval._repair import _MIN_MEANINGFUL_TERM, Repair, repair_query
from jericho.retrieval._rerank_backend import CONFIDENT_MIN_DEFAULT

try:  # optional acceleration (jericho[vectors]); pure-Python fallback below
    import numpy as _np
except ImportError:  # pragma: no cover - exercised only when numpy is absent
    _np = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from jericho.storage import JerichoStorage

LOGGER = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[0-9a-zA-Zа-яёА-ЯЁ][0-9a-zA-Zа-яёА-ЯЁ._+#-]*", re.UNICODE)
# Separators that carry meaning INSIDE a token but are punctuation at the end of one.
# `+` and `#` are excluded on purpose: C++ and C# end in them legitimately.
_TRAILING_PUNCTUATION = ".-"


def tokens_of(text: str, *, fold_yo: bool = True) -> list[str]:
    """Tokenize the way every part of retrieval must agree to tokenize.

    ``_TOKEN_RE`` deliberately lets ``. _ + # -`` continue a token so that ``file.txt``,
    ``BRK.A`` and ``scale_factor`` survive as single units. The cost is that a token
    ending a sentence swallows the full stop, and then the same identifier written in a
    query and in a document is two different strings.

    That is not cosmetic. ``_identifier_coverage`` drops any candidate whose identifiers
    do not all appear in its text, so a document mentioning
    ``autovacuum_vacuum_scale_factor.`` was unreachable by a query for
    ``autovacuum_vacuum_scale_factor`` — FTS returned the hit and the blend discarded it
    as ``identifier_mismatch``.

    ``ё`` folds to ``е`` (see ``_YO_FOLD``). Callers that must see the letter as
    typed — the FTS query builder, which has to search an index that stored the text
    verbatim — pass ``fold_yo=False``.
    """

    tokens = [token.rstrip(_TRAILING_PUNCTUATION) for token in _TOKEN_RE.findall(text or "")]
    return [token.translate(_YO_FOLD) for token in tokens] if fold_yo else tokens


# `ё` and `е` are the same Russian letter as far as a reader is concerned, and the
# language is written both ways in the same breath: phone keyboards produce `е`,
# careful writers and most published text use `ё`, and one person mixes both within
# a note. Measured on the owner's own document — the question «что выбрать для
# чёрных списков» against a document saying «Для Черных Списков»: lexical similarity
# 0.482 with matching letters, **0.275** without, and FTS `MATCH 'чёрных'` returned
# nothing at all where `MATCH 'черных'` returned the document. Neither `unicode61
# remove_diacritics 2` nor NFKC folds it: U+0451 is its own letter, not `е` plus a
# combining mark.
#
# The cost is a handful of pairs distinguished only by this letter (все/всё,
# небо/нёбо). For recall over a personal archive that is the right trade, and it is
# the same one every Russian search engine makes.
_YO_FOLD = str.maketrans({"ё": "е", "Ё": "Е"})


_RELATIONAL_QUERY_RE = re.compile(
    r"\b(?:связан\w*|завис\w*|участву\w*|работа\w*\s+над|относ\w*\s+к|"
    r"част\w*\s+(?:проекта|системы)|через\s+что|между\s+\w+\s+и\s+\w+|"
    r"related\s+to|depends?\s+on|works?\s+on|part\s+of|connected\s+to|between)\b",
    re.IGNORECASE,
)
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "the",
    "to",
    "what",
    "with",
    "а",
    "без",
    "бы",
    "в",
    "во",
    "где",
    "да",
    "для",
    "до",
    "же",
    "за",
    "и",
    "из",
    "или",
    "как",
    "к",
    "ко",
    "ли",
    "мне",
    "мой",
    "моя",
    "мы",
    "на",
    "над",
    "не",
    "но",
    "о",
    "об",
    "он",
    "она",
    "они",
    "от",
    "по",
    "под",
    "про",
    "с",
    "со",
    "так",
    "там",
    "то",
    "у",
    "что",
    "это",
    "я",
}


def lexical_vector(text: str) -> dict[str, float]:
    """L2-normalized word and character-trigram vector with identifier preservation.

    The word feature is the STEM (see `_morphology`), so «Казань» and «в Казани»
    are the same feature — Russian inflects, and a ranker that matches strings
    counted them as different words. Both sides of every comparison run through
    this one function, so the folding is symmetric by construction.

    Trigrams stay on the surface form. They are the part that already tolerated
    morphology approximately, and keeping them unstemmed preserves the signal a
    stemmer cannot express — «список»/«списка» differ by a fleeting vowel that no
    suffix rule reaches, and their trigrams still overlap almost completely.
    """
    tokens = [token.casefold() for token in tokens_of(text)]
    tokens = [token for token in tokens if token not in _STOPWORDS]
    weights: dict[str, float] = {}
    for token in tokens:
        word = stem(token)
        weights[f"w:{word}"] = weights.get(f"w:{word}", 0.0) + 1.5
        padded = f"#{token}#"
        for index in range(max(0, len(padded) - 2)):
            key = f"t:{padded[index : index + 3]}"
            weights[key] = weights.get(key, 0.0) + 0.35
    norm = math.sqrt(sum(value * value for value in weights.values()))
    return {key: value / norm for key, value in weights.items()} if norm else {}


def sparse_cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


# How far a snippet edge may walk to reach a word boundary. Long enough for any
# real word or product name, short enough that a base64 blob or a URL cannot drag
# the edge across the document.
_SNIPPET_BOUNDARY_SCAN = 48


def _is_word_char(character: str) -> bool:
    return character.isalnum() or character in "_-"


def _word_start(text: str, index: int) -> int:
    """Move an offset back to the start of the word it landed inside."""
    limit = max(0, index - _SNIPPET_BOUNDARY_SCAN)
    while index > limit and _is_word_char(text[index - 1]) and _is_word_char(text[index]):
        index -= 1
    return index


def _word_end(text: str, index: int, *, floor: int) -> int:
    """Move an offset back to the end of the last whole word before it."""
    limit = max(floor, index - _SNIPPET_BOUNDARY_SCAN)
    while (
        index > limit and index < len(text) and _is_word_char(text[index - 1]) and _is_word_char(text[index])
    ):
        index -= 1
    return index


# Extracted tables arrive as one record per line with cells joined by " | "
# (docx tables, xlsx and csv/tsv in `jericho.documents` all render this shape).
_RECORD_CELL_SEPARATOR = " | "
# A query token found on this many distinct lines marks those lines as records
# even without cell separators: a per-row field label repeats once per record.
_RECORD_TOKEN_MIN_LINES = 4


def _search_form(token: str) -> str:
    """What is actually searched in the body for a query token.

    «личный номер Иванова» names the row's owner in genitive while the table
    stores «Иванов»: the surface form never matches as a substring. The Snowball
    stem strips the case ending and stays a PREFIX of every inflected form, so
    one substring search finds them all. Identifiers, Latin and short words come
    back from `stem` unchanged, and a stem under 3 characters would match half
    the alphabet, so it is not used as a search term.
    """
    folded = token.casefold().replace("ё", "е")
    stemmed = stem(folded)
    if stemmed != folded and folded.startswith(stemmed) and len(stemmed) >= 3:
        return stemmed
    return folded


def _record_line_segments(body: str, occurrences: list[tuple[int, str]]) -> list[tuple[int, int, bool]]:
    """Split ``body`` so that a snippet window cannot span two table records.

    A line is a record when it carries the extractors' cell separator alongside
    a neighbouring line that does too, or a query token that repeats across
    ``_RECORD_TOKEN_MIN_LINES`` distinct lines — a per-row field label. Record
    lines become their own segments; everything between them merges, so prose
    keeps its cross-paragraph windows. Returns ascending ``(lo, hi, is_header)``
    spans covering the body: a header is the digit-free first line of a bar-table
    block — column names, not content, so it must not win the window for itself
    (a two-word label in the header would outscore the queried person's row);
    `best_snippet` glues it back under the winning row instead.
    """
    line_spans: list[tuple[int, int]] = []
    cursor = 0
    for line in body.split("\n"):
        line_spans.append((cursor, cursor + len(line)))
        cursor += len(line) + 1
    if len(line_spans) < 3:
        return [(0, len(body), False)]
    has_bar = [_RECORD_CELL_SEPARATOR in body[lo:hi] for lo, hi in line_spans]
    record = [
        bar and ((index > 0 and has_bar[index - 1]) or (index + 1 < len(has_bar) and has_bar[index + 1]))
        for index, bar in enumerate(has_bar)
    ]
    starts = [lo for lo, _ in line_spans]
    lines_of_form: dict[str, set[int]] = {}
    for position, form in occurrences:
        lines_of_form.setdefault(form, set()).add(bisect_right(starts, position) - 1)
    for indices in lines_of_form.values():
        if len(indices) >= _RECORD_TOKEN_MIN_LINES:
            for index in indices:
                record[index] = True
    if not any(record):
        return [(0, len(body), False)]
    is_header = [False] * len(line_spans)
    index = 0
    while index < len(has_bar):
        if not has_bar[index]:
            index += 1
            continue
        block_start = index
        while index < len(has_bar) and has_bar[index]:
            index += 1
        first_lo, first_hi = line_spans[block_start]
        if index - block_start >= 2 and not any(ch.isdigit() for ch in body[first_lo:first_hi]):
            is_header[block_start] = True
    segments: list[tuple[int, int, bool]] = []
    cursor = 0
    for index, (lo, hi) in enumerate(line_spans):
        if not record[index]:
            continue
        if lo > cursor:
            segments.append((cursor, lo, False))
        segments.append((lo, hi, is_header[index]))
        cursor = hi
    if cursor < len(body):
        segments.append((cursor, len(body), False))
    return segments


def _select_window(
    occurrences: list[tuple[int, str]],
    weights: dict[str, float],
    segments: list[tuple[int, int, bool]],
    max_chars: int,
) -> tuple[int, int, int]:
    """Position and segment of the best window: ``(best_pos, seg_lo, seg_hi)``.

    A window's score is the sum of the rarity weights of the DISTINCT terms
    inside it, and the window never leaves its segment. Header segments cannot
    anchor a window (first pass skips them; the second pass only runs when
    nothing matched outside headers). Two pointers over the sorted occurrences,
    one segment at a time — the obvious rescan-per-candidate version is
    quadratic in the number of matches, which grows with DOCUMENT SIZE, not
    query length: measured at 0.31 s for 40 KB, 4.5 s for 162 KB and **115.7 s
    for 812 KB**, all synchronous on the event loop, so one large document made
    the whole backend unresponsive for two minutes.
    """
    best_score = -1.0
    # Placeholder only: the fallback pass visits every occurrence, and any real
    # candidate scores above -1, so the placeholder never survives the loop.
    best = (occurrences[0][0], 0, occurrences[0][0] + max_chars)
    total = len(occurrences)
    for skip_headers in (True, False):
        occ_index = 0
        for seg_lo, seg_hi, is_header in segments:
            while occ_index < total and occurrences[occ_index][0] < seg_lo:
                occ_index += 1
            left = occ_index
            if left >= total or occurrences[left][0] >= seg_hi:
                continue
            bound = left
            while bound < total and occurrences[bound][0] < seg_hi:
                bound += 1
            occ_index = bound
            if skip_headers and is_header:
                continue
            counts: Counter[str] = Counter()
            score = 0.0
            right = left
            for anchor in range(left, bound):
                pos = occurrences[anchor][0]
                if anchor > left:
                    # The window held [anchor-1, right); drop the element leaving it.
                    leaving = occurrences[anchor - 1][1]
                    counts[leaving] -= 1
                    if not counts[leaving]:
                        del counts[leaving]
                        score -= weights[leaving]
                while right < bound and occurrences[right][0] < pos + max_chars:
                    entering = occurrences[right][1]
                    if not counts[entering]:
                        score += weights[entering]
                    counts[entering] += 1
                    right += 1
                # Strictly-greater keeps the EARLIEST of equal windows, as before;
                # the epsilon keeps float drift from reordering true ties.
                if score > best_score + 1e-12:
                    best_score = score
                    best = (pos, seg_lo, seg_hi)
        if best_score >= 0.0:
            break
    return best


def best_snippet(query: str, text: str, *, max_chars: int = 520) -> str:
    """Return the passage of ``text`` most relevant to ``query`` (query-aware
    excerpting) so a reader — the LLM or the answer verifier — sees the region
    that actually matched, not the document head. Pure lexical scoring over the
    shared tokenizer (no model, no embedding); falls back to the head when the
    text is short or nothing matches.

    Terms are weighted by rarity (1/occurrences) and windows stop at table-record
    boundaries. Measured on the owner's live archive before that: a 520-char
    window over a 58-row personnel table carried 9 rows — the column label
    matched in every row, the queried surname in one, their contribution was
    equal, and the answer came back with ANOTHER PERSON's number while grounding
    and citation checks honestly passed (the number really was in the cited
    document). Rarity makes the one-off surname outweigh the everywhere-label;
    the segment boundary keeps neighbouring records out of the excerpt.
    """
    body = (text or "").strip()
    if len(body) <= max_chars:
        return body
    # form → the token as the user typed it, for the missing-term note.
    forms: dict[str, str] = {}
    for token in tokens_of(query):
        folded = token.casefold()
        if len(token) > 1 and folded not in _STOPWORDS:
            forms.setdefault(_search_form(folded), token)
    if not forms:
        return body[:max_chars].rstrip() + "…"
    lowered = body.casefold().replace("ё", "е")
    occurrences: list[tuple[int, str]] = []
    first_pos: dict[str, int] = {}
    for form in forms:
        start = 0
        while True:
            found = lowered.find(form, start)
            if found < 0:
                break
            occurrences.append((found, form))
            first_pos.setdefault(form, found)
            start = found + len(form)
    if not occurrences:
        return body[:max_chars].rstrip() + "…"
    occurrences.sort()
    doc_counts = Counter(form for _, form in occurrences)
    weights = {form: 1.0 / count for form, count in doc_counts.items()}
    segments = _record_line_segments(body, occurrences)
    best_pos, seg_lo, seg_hi = _select_window(occurrences, weights, segments, max_chars)

    # A bar-table row shows values without their column names. The block's first
    # line, when it is provably a header (no digits — data rows carry numbers and
    # dates), is prepended so the model can NAME the field it reads instead of
    # guessing among cells. A digit-bearing first line is somebody's record, and
    # gluing it back in would recreate the defect this function exists to fix.
    header_lo, header_hi = -1, -1
    if (seg_lo, seg_hi) != (0, len(body)) and _RECORD_CELL_SEPARATOR in body[seg_lo:seg_hi]:
        first_lo = seg_lo
        cursor = seg_lo
        while cursor > 0:
            previous_hi = cursor - 1  # the newline before the current line
            previous_lo = body.rfind("\n", 0, previous_hi) + 1
            if _RECORD_CELL_SEPARATOR not in body[previous_lo:previous_hi]:
                break
            first_lo = previous_lo
            cursor = previous_lo
        line_end = body.find("\n", first_lo)
        header = body[first_lo:line_end].strip() if first_lo != seg_lo and line_end >= 0 else ""
        if header and not any(character.isdigit() for character in header) and len(header) <= max_chars // 3:
            header_lo, header_hi = first_lo, line_end
    header_text = body[header_lo:header_hi].strip() if header_lo >= 0 else ""
    budget = max_chars - (len(header_text) + 1 if header_text else 0)

    # Snap both edges to word boundaries. Cutting at an arbitrary offset decapitates
    # whichever word straddles it, and the leading "…" makes the remainder look like
    # a whole word rather than a fragment. Measured on the owner's own document: the
    # question "какое приложение не решило проблему авторизации localhost" produced
    # an excerpt beginning "…dify ❌ Проблема авторизации localhost-порта не решена"
    # — the answer was present and the name of the application, Hiddify, was gone.
    # Names, products and identifiers are exactly the words an excerpt exists to
    # carry, and they sit at the edges as often as anywhere else.
    start = _word_start(body, max(seg_lo, best_pos - 64))
    end = _word_end(body, min(seg_hi, start + budget), floor=best_pos + 1)
    snippet = body[start:end].strip()
    if header_text:
        between = body[header_hi:start]
        joiner = "\n" if not between.strip() else "\n…\n"
        prefix = "…" if header_lo > 0 else ""
        result = f"{prefix}{header_text}{joiner}{snippet}"
    else:
        result = f"{'…' if start > 0 else ''}{snippet}"
    if end < len(body):
        result += "…"

    # The rarest term the document DOES contain is the query's most specific
    # anchor — usually the person or thing asked about. When constraints keep it
    # out of the window, silence would present the excerpt as the best the
    # document has; the live defect survived every check exactly that way.
    rarest = min(doc_counts, key=lambda form: (doc_counts[form], first_pos[form], form))
    covered = any(
        (start <= position < end) or (header_lo >= 0 and header_lo <= position < header_hi)
        for position, form in occurrences
        if form == rarest
    )
    if not covered:
        # 24-char cap keeps the whole note inside every caller's excerpt budget.
        display = forms[rarest][:24]
        result += f"\n[слово «{display}» из запроса есть в документе, но в эту выдержку не попало]"
    return result


def dense_cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    return dot / (norm_left * norm_right) if norm_left and norm_right else 0.0


def reciprocal_rank_fusion(rankings: list[list[str]], *, k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, document_id in enumerate(ranking):
            scores[document_id] = scores.get(document_id, 0.0) + 1.0 / (k + position + 1)
    return scores


def knowledge_search_text(item: dict[str, Any]) -> str:
    """Canonical text used both for indexing and query-time embedding of an object."""
    return " ".join(
        str(item.get(field, "")) for field in ("title", "summary", "content", "tags_json", "knowledge_kind")
    )


# A boundary is only worth honouring past this fraction of the window; nearer the
# start it would trade a whole chunk for a stub.
# Mirrors the indexer's own ceilings (`_DOC_VECTOR_MAX_CHARS` / `_EMBED_REQUEST_MAX_CHARS`
# in jericho/workers): one candidate never exceeds what an embeddings service accepts,
# and one request stays inside the measured ~2800 chars/s throughput.
#
# «Отражают потолки индексатора» — так тут было написано, и это перестало быть правдой,
# когда пределы сервиса замерили: у индексатора 10 000 на вход и 30 000 на запрос, а
# здесь стояло вдвое больше. То есть путь, по которому идёт ЖИВОЙ запрос человека
# (фолбэк, когда индекса ещё нет), гарантированно упирался в оба отказа по длине —
# и по входу, и по запросу — а лечатся они делением и укорачиванием, каждое из которых
# стоит лишнего обращения к сервису. Числа приведены к замеренным.
_POOL_TEXT_MAX_CHARS = 10_000
_POOL_REQUEST_MAX_CHARS = 30_000
# A raw cosine below this is noise, not evidence — the floor for the
# `insufficient_evidence` gate. MEASURED against the production model
# (qwen3-embedding-0.6b, dim 1024) at the operating point that matters, a short
# query against a document body: 56 query x unrelated-document pairs scored
# min 0.1032 / p50 0.2361 / p90 0.3255 / max 0.3878, while 8 query x own-document
# pairs scored min 0.4188 / p50 0.5197 / max 0.6196.
#
# The previous constant, 0.16, sat *below the median of the noise*: it admitted
# 48 of those 56 unrelated documents — 85.7% — as dense evidence. 0.35 clears
# noise p90 while leaving 0.07 of headroom under the weakest genuine match, which
# matters because this gate REMOVES results: erring low costs precision, erring
# high costs recall, and only one of those is recoverable by reading further down
# the list. Configurable because the number belongs to the model, not to Jericho.
#
# 0.40, re-measured on a REAL corpus. The 0.35 above came from a synthetic stand
# where unrelated pairs stopped at p90 = 0.3255. On 77 documents of the owner's own
# working archive — long, formal, homogeneous Russian office text — unrelated pairs
# reach p90 = 0.427 and max 0.568, because two formal Russian documents resemble each
# other no matter what they are about. 770 unrelated pairs (10 questions on subjects
# absent from the archive) against 122 matching pairs (questions built from a
# document's own words):
#
#     threshold   unrelated passing   matching passing
#       0.35            28.8%               97.5%
#       0.40            13.9%               83.6%
#       0.45             5.3%               69.7%
#
# 0.40 halves the false evidence and keeps five sixths of the real. Higher costs more
# than it buys: dense recall exists precisely for the match that shares no words, and
# this gate is one leg of a conjunction — a document with lexical or FTS evidence is
# admitted regardless of it.
#
# The classes overlap (matching from 0.314, unrelated to 0.568), so no threshold
# separates them and none is claimed to. And the number belongs to the CORPUS as much
# as to the model: a homogeneous archive raises the floor. Re-measure rather than
# inherit — the stand is described in ARCHITECTURE §7.
#
# 0.35, перемерено 2026-07-29 на корпусе из 1537 документов и — впервые — на ЧЕСТНОМ
# индексе. Прежние 0.40 выведены на 77 документах, чьи вектора считались по обрезкам
# текста: отказ сервиса по длине лечился укорачиванием, и вектор описывал начало
# документа. У формальных русских документов начала похожи (шапки, реквизиты,
# преамбулы) независимо от содержания — поэтому чужие пары и набирали так много.
#
# Та же методика, чтобы числа были сравнимы: чужие пары — вопросы о том, чего в
# архиве НЕТ (отсутствие каждой темы проверено поиском по телам: 3 темы из 12
# отброшены, потому что нашлись), против случайных документов. 540 чужих пар.
#
#     чужие пары: p50 0.2304, p90 0.2981, p99 0.3487, max 0.3747
#     (было на обрезанном индексе: p50 0.308, p90 0.427, max 0.568)
#
#     порог   чужих проходит
#      0.30        9.3%
#      0.35        0.9%   <- отсюда
#      0.38        0.0%
#      0.40        0.0%
#
# Шум упал почти на полторы десятых, и 0.40 перестал что-либо покупать: чужие пары
# и так кончаются на 0.3747. Он при этом продолжает СТОИТЬ — на 60 своих парах через
# 0.40 проходит 53.3% против 66.7% через 0.35. Своя сторона с прежней таблицей
# несравнима (там вопросы строились из слов самого документа, здесь их писала модель
# как человеческие), поэтому 66.7% не надо сопоставлять с 83.6%; сравнима чужая.
#
# 0.35 выбран по тому же правилу, что и прежние значения, — «выше шума»: он стоит над
# p99 чужих пар. Ниже брать нельзя: 0.30 впускает 9.3% чужих, а на корпусе в полторы
# тысячи документов это полторы сотни документов, готовых стать «доказательством» для
# бессмысленного вопроса, — та самая беда, которую этот проект уже мерил и лечил.
_DENSE_EVIDENCE_MIN_DEFAULT = 0.35
_CHUNK_BOUNDARY_FLOOR = 0.5
# Below this a lexical score is not weak evidence, it is none: the same number the
# `insufficient_evidence` gate uses to say a document shares no vocabulary with the
# query. Named because it is now consulted twice, and the two uses must not drift.
_LEXICAL_EVIDENCE_MIN = 0.075
# How many documents' lexical vectors stay cached between requests.
_VECTOR_CACHE_MAX = 2048
_SENTENCE_END_RE = re.compile(r"[.!?…][»\"')\]]?\s")


def _chunk_boundary(text: str, start: int, end: int, max_chars: int) -> int:
    """The last natural boundary inside ``[start, end)``, else ``end``.

    Searched backwards from the window edge in descending order of how much meaning
    the break preserves: paragraph, sentence, line, word. A hard cut is the last
    resort — a base64 blob with no whitespace at all must still terminate.
    """
    floor = start + int(max_chars * _CHUNK_BOUNDARY_FLOOR)
    window = text[start:end]
    paragraph = window.rfind("\n\n")
    if paragraph >= 0 and start + paragraph + 2 > floor:
        return start + paragraph + 2
    sentence = None
    for candidate in _SENTENCE_END_RE.finditer(window):
        if start + candidate.end() > floor:
            sentence = candidate
    if sentence is not None:
        return start + sentence.end()
    for separator in ("\n", " "):
        found = window.rfind(separator)
        if found >= 0 and start + found + 1 > floor:
            return start + found + 1
    # Below the floor a word break still beats splitting mid-word.
    found = window.rfind(" ")
    return start + found + 1 if found > 0 else end


def _advance(text: str, position: int, limit: int) -> int:
    """Nudge an overlap start forward off the middle of a word."""
    if position <= 0 or position >= len(text) or text[position - 1].isspace():
        return position
    found = text.find(" ", position)
    return found + 1 if 0 <= found < limit else position


def chunk_spans(text: str, *, max_chars: int, overlap_chars: int, max_chunks: int) -> list[tuple[int, int]]:
    """Split ``text`` into overlapping ``[start, end)`` spans on natural boundaries.

    Spans are character offsets into ``text`` itself (not into a normalised copy), so
    the winning passage can be quoted back verbatim. Overlap means a fact shorter than
    ``overlap_chars`` always lands whole inside at least one span.
    """
    body = text or ""
    if max_chars <= 0 or not body:
        return []
    if len(body) <= max_chars:
        return [(0, len(body))]
    bounded_overlap = max(0, min(int(overlap_chars), max_chars // 2))
    limit = max(1, int(max_chunks))

    def _cut(window: int) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        start = 0
        while start < len(body):
            end = min(start + window, len(body))
            if end < len(body):
                end = _chunk_boundary(body, start, end, window)
            spans.append((start, end))
            if end >= len(body):
                break
            # ``start + 1`` guarantees forward progress whatever the overlap is.
            start = _advance(body, max(start + 1, end - bounded_overlap), end)
        return spans

    spans = _cut(max_chars)
    window = max_chars
    if len(spans) > limit:
        # Widen until it fits, instead of one pass followed by ``[:limit]``.
        #
        # The single pass was capped at ``max_chars * 4`` and sized by
        # ``ceil(len/limit)``, which ignores that ``_chunk_boundary`` snaps a span
        # back by up to half a window — so both bounds under-shoot, and whatever did
        # not fit was cut away in silence. Measured with the shipped defaults
        # (1200 / 200 / 63): a 490 KB document indexed **59% of itself**, and the
        # missing 41% was reachable only through the whole-object vector, which is
        # itself capped. Nothing downstream could tell a truncated document from a
        # short one.
        #
        # Growing geometrically and STOPPING there was the next defect: the jump
        # lands wherever ×1.5 happens to land, and everything between the last
        # window that did not fit and the one that did was never examined. Measured
        # on a real 342-document archive: of the 22 documents that needed widening,
        # passages came out 1.40× (median) to 1.84× larger than the smallest window
        # that already fitted, wasting a median 31% of the passage budget — the
        # 757 031-char object got 43 passages of 18 326 chars where 63 of ~12 588
        # fit. Precisely the documents chunking exists for were split the worst.
        #
        # So: estimate first (right whenever boundary snapping is mild), doubling
        # to find SOME window that fits, then a binary search back down to the
        # smallest fitting one. Each probe is one O(len) cut and the search adds
        # ~log2(len/max_chars) of them — a dozen linear passes on a megabyte body.
        floor = window  # the widest window known NOT to fit
        while len(spans) > limit and window < len(body):
            estimate = math.ceil(len(body) / limit) + bounded_overlap
            floor = window
            window = min(len(body), estimate if estimate > window else window * 2)
            spans = _cut(window)
        # Span count is not strictly monotonic in the window (snapping makes it
        # step), so the search only ever moves to a window it has SEEN fit; the
        # result is the smallest fitting window on the probed path, and always fits.
        while floor + 1 < window:
            middle = (floor + window) // 2
            candidate = _cut(middle)
            if len(candidate) > limit:
                floor = middle
            else:
                window, spans = middle, candidate
    if len(spans) > limit:
        spans = spans[:limit]
        # Unreachable while the loop above can widen, but if it ever is reached the
        # tail is content, not slack: carry the last span to the end of the body.
        spans[-1] = (spans[-1][0], len(body))
    # A trailing stub scores erratically high on a short match; fold it backwards.
    stub = max(120, max_chars // 6)
    if len(spans) > 1 and spans[-1][1] - spans[-1][0] < stub:
        spans[-2] = (spans[-2][0], spans[-1][1])
        spans.pop()
    return spans


def knowledge_chunk_units(
    item: dict[str, Any], *, max_chars: int, overlap_chars: int, max_chunks: int
) -> list[tuple[int, int, str]]:
    """``(start, end, text_to_embed)`` per passage, or ``[]`` when no chunking applies.

    Returning ``[]`` for a short object is the load-bearing case: it keeps such an
    object represented by exactly one whole-object vector computed from byte-identical
    text, so enabling chunking changes nothing for the bulk of a personal corpus.
    Each passage is prefixed with the object's header — a paragraph stripped of what
    document it belongs to loses most of its topical signal.
    """
    if max_chars <= 0 or len(knowledge_search_text(item)) <= max_chars:
        return []
    content = str(item.get("content") or "")
    spans = chunk_spans(content, max_chars=max_chars, overlap_chars=overlap_chars, max_chunks=max_chunks)
    if len(spans) <= 1:
        # One passage is what the whole-object vector already represents.
        return []
    header = " ".join(
        part
        for part in (
            str(item.get("title") or ""),
            str(item.get("summary") or ""),
            str(item.get("knowledge_kind") or ""),
        )
        if part.strip()
    )[: max(0, max_chars // 4)]
    return [(start, end, f"{header}\n\n{content[start:end]}") for start, end in spans]


def chunk_scheme(settings: JerichoSettings) -> str:
    """Fingerprint of the chunking configuration a stored row was built with.

    ``''`` means "not chunked" — exactly what every pre-0.41 row already stores, so
    turning chunking off re-indexes nothing that was never chunked.

    The ALGORITHM is part of the fingerprint, not just the numbers: ``v2`` is the
    binary-searched minimal window (documents over the passage budget get ~limit
    fine passages instead of ~2/3 of the budget in coarse ones), and a ``v1`` row
    for such a document describes spans a fresh index would no longer produce.
    The bump is cheap where nothing changed: an unwidened document re-chunks to
    byte-identical texts, and the indexer reuses their vectors by content hash
    without asking the embeddings service for anything.
    """
    if settings.embeddings_chunk_chars <= 0:
        return ""
    return (
        f"v2:{settings.embeddings_chunk_chars}"
        f":{settings.embeddings_chunk_overlap_chars}"
        f":{settings.embeddings_chunk_max_per_object}"
    )


def pack_vector(vector: list[float]) -> bytes:
    """Pack an embedding as little-agnostic float32 bytes for BLOB storage."""
    return array.array("f", (float(value) for value in vector)).tobytes()


def unpack_vector(blob: bytes) -> array.array:
    """Inverse of :func:`pack_vector` — returns a float32 array of the stored vector."""
    values = array.array("f")
    values.frombytes(blob)
    return values


def _dense_scores_python(
    query_vector: list[float], stored: list[tuple[str, bytes]], query_dim: int
) -> list[tuple[float, str]]:
    """Cosine-score every stored vector against the query in pure Python (fallback)."""
    query_norm = math.sqrt(sum(value * value for value in query_vector))
    if query_norm == 0.0:
        return []
    scored: list[tuple[float, str]] = []
    for document_id, blob in stored:
        vector = unpack_vector(blob)
        if len(vector) != query_dim:
            continue
        dot = 0.0
        norm = 0.0
        for query_value, value in zip(query_vector, vector, strict=False):
            dot += query_value * value
            norm += value * value
        if norm <= 0.0:
            continue
        scored.append((dot / (query_norm * math.sqrt(norm)), document_id))
    return scored


def _dense_scores_numpy(
    query_vector: list[float], stored: list[tuple[str, bytes]], query_dim: int
) -> list[tuple[float, str]]:
    """Vectorised cosine over all stored vectors: the per-element Python loop
    collapses into one matmul (BLAS), turning ~N*dim scalar ops into a matrix op."""
    expected_bytes = query_dim * 4  # pack_vector stores float32 (4 bytes/value)
    ids: list[str] = []
    buffers: list[bytes] = []
    for document_id, blob in stored:
        if len(blob) == expected_bytes:  # skip dimension-mismatched vectors, as the loop did
            ids.append(document_id)
            buffers.append(blob)
    if not ids:
        return []
    # float32 (the stored precision) keeps this zero-copy and ~15x over the loop;
    # cosine values differ from the float64 loop only ~1e-7, well below any ranking
    # threshold. einsum computes per-row squared norms without an N*dim temporary.
    matrix = _np.frombuffer(b"".join(buffers), dtype=_np.float32).reshape(len(ids), query_dim)
    query = _np.asarray(query_vector, dtype=_np.float32)
    query_norm = math.sqrt(float(query @ query))
    if query_norm == 0.0:
        return []
    norms = _np.sqrt(_np.einsum("ij,ij->i", matrix, matrix))
    dots = matrix @ query
    valid = norms > 0.0  # drop zero-norm vectors, matching the Python path
    scores = dots[valid] / (norms[valid] * query_norm)
    valid_ids = [ids[index] for index in range(len(ids)) if valid[index]]
    return list(zip((float(score) for score in scores), valid_ids, strict=True))


_CHUNK_CORROBORATION_K = 3

# Signals whose weight can be zeroed WITHOUT changing anything else, so switching one
# off and re-measuring the gold set answers "does this weight earn its place?".
# Веса каналов в итоговом слиянии. Вынесены из выражения, чтобы их можно было
# ЗАМЕРИТЬ: подобрать вес, не имея возможности его изменить, нельзя, а правка
# исходника на каждую пробу означает, что каждый раз меряется другая программа.
# Значения — ровно те, что стояли в выражении; менять их разрешено только замером
# с отложенной половиной вопросов.
#
# ОТРИЦАТЕЛЬНЫЙ РЕЗУЛЬТАТ 2026-07-30, чтобы не перебирали второй раз. Покоординатный
# спуск по всем четырём (каждый на ×0.5/×0.75/×1.5/×2, метрика — «есть ли среди
# пятёрки отвечающий по существу», судья с базой 12.2%): НИ ОДИН вес не сдвинул
# метрику больше чем на один вопрос из сорока при пороге шума в три.
#
# Что при этом видно и что верно:
#   * подъём веса плотного канала монотонно поднимает точность (43.0 -> 46.5%) и
#     ровно настолько же роняет охват — размен, а не выигрыш;
#   * вес графа не влияет НИКАК ни при каком значении: на 110 сущностях и 196 связях
#     канал даёт ноль всем документам (AUC 0.500 при разделении «отвечает/нет»);
#   * итоговое слияние различает отвечающих ХУЖЕ своего лучшего канала: AUC 0.640
#     против 0.687 у плотного в одиночку. Формула разбавляет сильный сигнал слабыми,
#     и это настоящий дефект — но перевесами он не лечится, разница слишком мала.
#
# Вывод прохода: качество упорядочивания упирается в СИГНАЛ, а не в способ его
# смешивания. Все ручки (веса, потолок отбора, порог доказательств, переранжирование
# чат-моделью) замерены и исчерпаны; нужен различитель другого рода.
_CHANNEL_WEIGHTS: dict[str, float] = {
    "lexical": 0.19,
    "field": 0.17,
    "embedding": 0.17,
    "graph": 0.16,
}

ABLATABLE_SIGNALS: tuple[str, ...] = (
    "feedback",
    "usage",
    "kind_alignment",
    "fts_bonus",
    "noise_penalty",
)
# Signals that also decide candidate ADMISSION, feed the RRF fusion, or act as an
# exclusion gate. Zeroing their weight is NOT the same as removing them, so an
# ablation number for these would be confidently wrong — they are reported as
# not measured, with the reason, instead.
ENTANGLED_SIGNALS: dict[str, str] = {
    "lexical": "feeds RRF and the insufficient_evidence gate",
    "field": "feeds the insufficient_evidence gate",
    "embedding": "decides candidate admission, feeds RRF and the evidence gate",
    "graph": "expands the candidate pool and feeds the evidence gate",
    "rrf": "a fusion of the other rankings, not a weight of its own",
    "identifier_coverage": "drives the identifier_mismatch exclusion, not just a penalty",
    "importance": "also a multiplicative lifecycle/quality input",
    "quality": "also a multiplicative quality_factor input",
    "promotion": "also a multiplicative quality_factor input",
}


def _trim_to_whole_objects(rows: list[tuple[str, bytes]], budget: int) -> list[tuple[str, bytes]]:
    """Cut a chunk-row scan on an OBJECT boundary at or below ``budget``.

    Rows arrive grouped by object (the query orders by object then chunk index). An
    object is either scanned completely or not at all, so ``scanned == indexed`` for
    every object that contributes a score — otherwise a half-scanned document both
    hides its answering passage and escapes the corroboration discount.
    """
    if budget <= 0 or len(rows) <= budget:
        return rows
    kept = 0
    current = rows[0][0].rpartition("#")[0]
    for index, (key, _) in enumerate((*rows, ("\x00#0", b""))):
        document_id = key.rpartition("#")[0]
        if document_id == current:
            continue
        # ``current`` ended at ``index``. Take it whole if it fits — or if it is the
        # first one, since dropping every row would be worse than a single overrun.
        if index <= budget or kept == 0:
            kept = index
        else:
            break
        current = document_id
    return rows[:kept]


def aggregate_chunk_scores(
    scored: Sequence[tuple[float, str]], *, blend: float
) -> tuple[dict[str, float], dict[str, tuple[int, int]]]:
    """Collapse per-chunk cosines into one score per Knowledge Object.

    ``(1 - blend) * best + blend * mean(top-3)``. Max-over-passages is what makes a
    single relevant paragraph of a long import recallable at all; the top-k mean is a
    corroboration term, so one lucky fragment does not outrank a document that is
    genuinely about the query — the expected maximum of N samples grows with N, and
    against a fixed evidence gate that bias would convert directly into false rescues.
    The result is a convex combination of cosines, so it stays on exactly the scale
    the 0.17 blend weight and the 0.16 evidence gate were calibrated for.

    ОТРИЦАТЕЛЬНЫЙ РЕЗУЛЬТАТ, 2026-07-29, чтобы это не «чинили» второй раз. Опасение
    выше — что максимум по пассажам систематически поднимает длинные документы —
    выглядит правдоподобно и на настоящем корпусе (1537 документов, 40 вопросов от
    модели, глубина 50) НЕ подтверждается:

        корреляция «число пассажей ↔ плотный скор»      +0.048
        корреляция «длина ↔ плотный скор» внутри запроса +0.024
        корреляция «длина ↔ итоговый скор»               +0.077
        корреляция «длина ↔ лексический скор»            −0.196

    Поводом к проверке было наблюдение: медиана длины выданного 6886 знаков против
    3065 по корпусу, вдвое с лишним. Но документы, ВООБЩЕ содержащие слова запроса,
    имеют медиану 6270 — длинный документ просто чаще содержит любое заданное слово.
    Поиск добавляет поверх этого 10%, а не в два раза.

    И польза от длины не зависит: доля документов, признанных отвечающими (судья с
    известной базой 12.2% на случайной паре), по полосам длины 37.5% / 33.3% / 28.8%
    / 29.9% — разброс внутри шума. Нормировать скор на длину значило бы лечить
    здоровое и ударить ровно по тем документам, ради которых пассажи и вводились.

    Returns ``(score_by_object, {object: (best_chunk_index, chunks_scored)})``.
    """
    weight = max(0.0, min(1.0, float(blend)))
    by_object: dict[str, list[tuple[float, int]]] = {}
    for score, key in scored:
        document_id, _, raw_index = key.rpartition("#")
        if not document_id:
            # Not a chunk key; ignore rather than mis-attribute it to an object.
            continue
        try:
            index = int(raw_index)
        except ValueError:
            continue
        by_object.setdefault(document_id, []).append((score, index))
    aggregated: dict[str, float] = {}
    provenance: dict[str, tuple[int, int]] = {}
    for document_id, entries in by_object.items():
        entries.sort(key=lambda pair: pair[0], reverse=True)
        best, best_index = entries[0]
        top = [score for score, _ in entries[:_CHUNK_CORROBORATION_K]]
        aggregated[document_id] = (1.0 - weight) * best + weight * (sum(top) / len(top))
        provenance[document_id] = (best_index, len(entries))
    return aggregated, provenance


def dense_scores(
    query_vector: list[float], stored: list[tuple[str, bytes]], query_dim: int
) -> list[tuple[float, str]]:
    """Cosine-score persisted vectors against the query, using numpy when available."""
    if _np is not None:
        return _dense_scores_numpy(query_vector, stored, query_dim)
    return _dense_scores_python(query_vector, stored, query_dim)


def _input_too_long(body: str) -> bool:
    """Отвергнут ли запрос именно за длину входа.

    По тексту, а не по коду: 400 отвечают и на неизвестную модель, и на кривой
    запрос, и укорачивать текст в этих случаях значило бы лечить чужую болезнь.
    Формулировки сервиса: «an input exceeds the 8192-token limit»,
    «input_too_many_tokens», «exceeds the 32768-character limit», а для предела на
    весь запрос — «input exceeds the 16384-token request limit» с кодом
    `request_too_many_tokens` и «the 262144-character request limit» с кодом
    `request_too_large`. Последний не подходил ни под один прежний признак:
    «character request limit» — это не «character limit». Такой отказ уходил в общий
    `except`, и пачка терялась целиком без внятной записи в журнале.
    """
    text = (body or "").casefold()
    return "input" in text and (
        "token limit" in text
        or "too_many_tokens" in text
        or "character limit" in text
        or "request_too_large" in text
        or "character request limit" in text
        # «16384-token request limit» — это не «token limit»: между словами затесалось
        # `request`. На живом сервисе выручает поле `code`, но полагаться на него одно
        # значит зависеть от того, что сервис его пришлёт. Признак должен узнавать и
        # саму формулировку.
        or "token request limit" in text
    )


def _retry_after_seconds(value: str | None) -> float | None:
    """`Retry-After` в секундах, если сервис его прислал.

    Только числовая форма: HTTP допускает и дату, но она требует разбора часового
    пояса и рассинхронизации часов, а цена ошибки — либо мгновенный повтор, либо
    часовая пауза. Непонятное значение лучше проигнорировать и взять свою выдержку.
    """
    if not value:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


class EmbeddingBackend:
    # Пауза после отказа перегруженного сервиса: удваивается, пока отказы идут,
    # и сбрасывается первым успехом. Потолок — чтобы после длинной аварии
    # индексация вернулась за минуту, а не через час.
    _COOLDOWN_START_SEC = 5.0
    _COOLDOWN_MAX_SEC = 300.0
    # Коды, означающие «слишком много» или «я не справляюсь». 502/503/504 — это
    # обычно прокси перед сервисом: сам сервис жив, а очередь перед ним переполнена.
    _OVERLOAD_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(self, settings: JerichoSettings) -> None:
        self.settings = settings
        # Монотонные часы: системное время может прыгнуть, и пауза станет вечной.
        self._cooldown_until = 0.0
        self._cooldown_sec = 0.0

    @property
    def cooling_down(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def cooldown_remaining(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    def _enter_cooldown(self, retry_after: float | None = None) -> None:
        if retry_after and retry_after > 0:
            wait = min(float(retry_after), self._COOLDOWN_MAX_SEC)
        else:
            previous = self._cooldown_sec or self._COOLDOWN_START_SEC / 2
            wait = min(previous * 2, self._COOLDOWN_MAX_SEC)
        self._cooldown_sec = wait
        self._cooldown_until = time.monotonic() + wait
        LOGGER.warning(
            "embeddings backend is overloaded; backing off for %.0fs before the next request", wait
        )

    def _clear_cooldown(self) -> None:
        self._cooldown_sec = 0.0
        self._cooldown_until = 0.0

    @property
    def remote_enabled(self) -> bool:
        return bool(
            self.settings.embeddings_enabled
            and self.settings.embeddings_base_url
            and self.settings.embeddings_model
        )

    async def embed(
        self,
        texts: list[str],
        *,
        budget_sec: float | None = None,
        _shortened: bool = False,
        _deadline: float | None = None,
        _nested: bool = False,
    ) -> list[list[float]] | None:
        if not self.remote_enabled or not texts:
            return None
        if self.cooling_down:
            # Отказ, не ожидание: подождать здесь значило бы держать вызывающего
            # (в том числе поисковый запрос человека) ради фоновой индексации.
            # Пусть каждый сам решит, что делать; воркер просто вернётся позже.
            return None
        # Бюджет — СРОК на всю операцию, а не терпение на каждый запрос. Разница
        # появилась вместе с делением слишком большого запроса: половины считались
        # отдельными вызовами и каждая получала полный бюджет заново, так что пачка,
        # поделённая до одиночных входов, имела право занять время, умноженное на
        # размер дерева деления. Живой запрос человека, у которого бюджет 4 секунды
        # ровно затем, чтобы он не ждал, мог держать его минутами.
        if _deadline is None and budget_sec is not None:
            _deadline = time.monotonic() + float(budget_sec)
        if _deadline is not None:
            remaining = _deadline - time.monotonic()
            if remaining <= 0:
                LOGGER.warning("embeddings budget of %.1fs is spent; giving up", budget_sec or 0.0)
                return None
        try:
            # The operator's configured patience, exactly as the LLM client uses it.
            # This used to be `min(llm_timeout_sec, 60.0)` — a hard ceiling that
            # overrode the setting, and the only place in the codebase that did.
            #
            # Measured on the installed service: **768 characters/second**, not the
            # 2800 the request-size cap was calibrated against. A full-size request
            # (`_EMBED_REQUEST_MAX_CHARS`, 30000 since the token limits were measured)
            # therefore takes **39 seconds** against that 60-second ceiling — and it
            # was 51.6 at the old 40000, eight seconds of margin that one busy moment
            # ate. The whole batch then fails, and the objects in it
            # keep no vector.
            #
            # Not hypothetical: the owner's log holds **83 failed embedding requests
            # between 00:00 and 03:00** on one night, indexing a single large
            # document, before it finally got through. Their `llm_timeout_sec` was
            # 240 the entire time.
            # Фоновой индексации нужен щедрый таймаут: она может ждать сколько угодно,
            # её никто не читает. Живому запросу человека — нет: он сидит и смотрит.
            # На этой установке эмбеддинги считаются на процессоре (в видеопамять не
            # помещаются вместе с LLM), и одна короткая строка занимает ~70 секунд.
            # Без отдельного бюджета человек ждал 40 секунд, чтобы получить в конце
            # «модель недоступна» — при том что LLM отвечала за две.
            patience = (
                max(1.0, _deadline - time.monotonic())
                if _deadline is not None
                else self.settings.llm_timeout_sec
            )
            timeout = httpx.Timeout(patience, connect=10.0)
            key = self.settings.embeddings_api_key
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, headers=headers) as client:
                response = await client.post(
                    f"{self.settings.embeddings_base_url}/embeddings",
                    json={"model": self.settings.embeddings_model, "input": texts},
                )
                if response.status_code == 400 and _input_too_long(response.text):
                    # У сервиса ДВА предела: на один вход (8192 токена) и на весь
                    # запрос (16384 токена). Лечение у них разное, и раньше было одно.
                    #
                    # Слишком большой ЗАПРОС лечится делением пополам: тексты целы,
                    # платим лишним обращением. Прежний код вместо этого укорачивал
                    # вдвое КАЖДЫЙ текст пачки — и объекты получали вектор по половине
                    # своего содержимого, молча и без следа в базе. На корпусе
                    # владельца это случилось 206 раз за один досчёт.
                    #
                    # Укорачивание остаётся, но только там, где выбора нет: один вход,
                    # который сам по себе длиннее предела. Такой бывает только вектор
                    # документа — огрублённый сигнал по началу текста, — а пассажи
                    # короче предела на порядок.
                    if len(texts) > 1:
                        LOGGER.warning(
                            "embeddings request too large for %d inputs; splitting in two", len(texts)
                        )
                        middle = len(texts) // 2
                        # Половины наследуют СРОК, а не бюджет: иначе каждая начинала
                        # отсчёт заново. И помечаются вложенными — успех половины не
                        # должен обнулять лестницу отступления, пока вся операция ещё
                        # может закончиться отказом.
                        first = await self.embed(texts[:middle], _deadline=_deadline, _nested=True)
                        if first is None:
                            return None
                        second = await self.embed(texts[middle:], _deadline=_deadline, _nested=True)
                        if second is None:
                            return None
                        if not _nested:
                            self._clear_cooldown()
                        return first + second
                    if not _shortened:
                        # Сообщение сервиса — В ЖУРНАЛ. Пределов у него несколько, и
                        # запись «слишком длинно» не говорит, какой именно сработал:
                        # разбираться приходится пробами вместо чтения. Тело ответа
                        # короткое и содержит только числа предела.
                        LOGGER.warning(
                            "embeddings input too long (%d chars, service said: %s); retrying at half length",
                            len(texts[0]),
                            " ".join(response.text.split())[:200],
                        )
                        return await self.embed(
                            [text[: max(1, len(text) // 2)] for text in texts],
                            _deadline=_deadline,
                            _shortened=True,
                            _nested=_nested,
                        )
                    return None
                if response.status_code in self._OVERLOAD_STATUSES:
                    # Отличать «перегружен» от «сломан» обязательно: раньше сюда
                    # приходило `raise_for_status()` и всё падало в общий `except`,
                    # после которого воркер повторял ТОТ ЖЕ объём через секунду.
                    # Замерено на этой установке: nginx перед сервисом эмбеддингов
                    # начал отдавать 502 через полтора часа непрерывной индексации.
                    self._enter_cooldown(_retry_after_seconds(response.headers.get("retry-after")))
                    return None
                response.raise_for_status()
                payload = response.json()
        except httpx.TimeoutException:
            # Таймаут при живом соединении — тоже сигнал перегрузки, а не поломки:
            # сервис принял запрос и не успел. Повторять сразу — добивать очередь.
            self._enter_cooldown()
            LOGGER.warning("embeddings backend timed out; treating as overload")
            return None
        except Exception:
            # Chunking multiplies how often this path runs (more inputs, bigger
            # requests), so the failure must stop being completely silent.
            LOGGER.warning("embeddings backend request failed", exc_info=True)
            return None
        # Лестницу отступления обнуляет только УСПЕХ ЦЕЛОЙ операции. Половина внутри
        # деления этого не делает: одна операция стала давать оба исхода сразу, и
        # успешная половина сбрасывала рост паузы ровно у тех запросов, которые чаще
        # всего и перегружают сервис, — у слишком больших.
        if not _nested:
            self._clear_cooldown()
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list) or len(items) != len(texts):
            return None
        output: list[list[float]] = []
        for item in items:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list):
                return None
            output.append([float(value) for value in vector])
        return output


async def semantic_similarity_order(
    backend: EmbeddingBackend,
    query: str,
    documents: list[str],
) -> list[int]:
    if not documents:
        return []
    vectors = await backend.embed([query, *documents])
    if vectors and len(vectors) == len(documents) + 1:
        scores = [dense_cosine(vectors[0], vector) for vector in vectors[1:]]
    else:
        query_vector = lexical_vector(query)
        scores = [sparse_cosine(query_vector, lexical_vector(document)) for document in documents]
    return sorted(range(len(documents)), key=lambda index: scores[index], reverse=True)


class HybridSearcher:
    """Rank personal knowledge using text, graph, quality, lifecycle, and feedback."""

    def __init__(
        self,
        storage: JerichoStorage,
        embeddings: EmbeddingBackend | None = None,
        *,
        graph_max_depth: int = 2,
        chunk_recall: bool = True,
        record_usage: bool = True,
        ablate: Sequence[str] | None = None,
        pool_max: int = 400,
        dense_evidence_min: float = _DENSE_EVIDENCE_MIN_DEFAULT,
        channel_weights: Mapping[str, float] | None = None,
        reranker: Any = None,
        rerank_top: int = 0,
        rerank_confident_min: float | None = None,
    ) -> None:
        self.storage = storage
        self.embeddings = embeddings
        self._graph_max_depth = max(1, int(graph_max_depth))
        # Ceiling on the fuzzy recall pool. Above it the lexical channel sees only
        # the most important/recent slice of the corpus, and the answer has to say so.
        self._pool_max = max(1, int(pool_max))
        # Cosine below which a dense score is not evidence. Model-dependent, so it is
        # configurable and its default is measured rather than chosen — see
        # `JERICHO_RETRIEVAL_DENSE_EVIDENCE_MIN` in docs/ARCHITECTURE.md §7.
        self._dense_evidence_min = float(dense_evidence_min)
        # Off only for the A/B harness, which must measure the same corpus twice.
        self._chunk_recall = bool(chunk_recall)
        # Off for advisory harnesses: they must not write the counter that
        # ``usage_signal`` reads back into the very blend they are measuring.
        self._record_usage = bool(record_usage)
        # Names from ABLATABLE_SIGNALS whose weight is forced to zero for this
        # instance. Measurement only: a name can silence a weight, never raise it.
        self._ablate = frozenset(ablate or ())
        # Веса четырёх каналов, участвующих в слиянии. Шов ТОЛЬКО ДЛЯ ЗАМЕРА, как и
        # `ablate` рядом: наружу не выведен, из настроек не читается, по умолчанию —
        # ровно те константы, что стояли в выражении. Нужен потому, что подобрать
        # веса, не имея возможности их менять, нельзя, а править исходник на каждую
        # пробу значит мерить каждый раз другую программу.
        #
        # Эти четыре в `ABLATABLE_SIGNALS` не входят и не могут: обнуление любого из
        # них меняет не только вес, но и отбор кандидатов и гейт доказательств (см.
        # `ENTANGLED_SIGNALS`). Здесь вес именно ИЗМЕНЯЕТСЯ, а не выключается, поэтому
        # шов законен: отбор и гейт остаются прежними.
        # Переранжирование отдельной моделью. Выключено, пока `rerank_top` не задан.
        # Замеры — в `_rerank_backend.py`: внутри пула прежний порядок различал
        # отвечающие документы на уровне монетки (AUC 0.512), cross-encoder даёт 0.754.
        # Про чат-модель в этой роли и почему она не подошла — в `_rerank.py`.
        self._reranker = reranker
        self._rerank_top = max(0, int(rerank_top))
        self._confident_min = (
            CONFIDENT_MIN_DEFAULT if rerank_confident_min is None else float(rerank_confident_min)
        )
        self._channel_weights = dict(_CHANNEL_WEIGHTS)
        for name, value in (channel_weights or {}).items():
            if name not in _CHANNEL_WEIGHTS:
                raise ValueError(f"unknown channel weight: {name}")
            self._channel_weights[name] = max(0.0, float(value))
        # Lexical vectors ACROSS requests, keyed by what makes one stale.
        #
        # `lexical_vector` runs once per candidate over that candidate's full body
        # and dominates a search: profiled on the 342-document stand it accounted
        # for 69 s of the 85 s spent in `search`, and one call on a 205 KB body
        # costs 48 ms. The per-request cache below it only ever helped the second
        # of the two `_lexical_rank` passes; the same document was rebuilt from
        # scratch on the next question, and on the one after that.
        #
        # The key is (id, version, updated_at): version moves on an edit, and
        # updated_at moves on anything that touches the row at all, so a stale
        # vector cannot outlive its text. Bounded by entry count — this is a
        # personal corpus, and the alternative is a cache that grows with it.
        self._vector_cache: OrderedDict[tuple[str, str, str], dict[str, float]] = OrderedDict()
        self._field_vector_cache: OrderedDict[tuple[str, str, str], tuple[dict[str, float], ...]] = (
            OrderedDict()
        )

    def _repair_query(self, user_id: str, clean_query: str) -> Repair | None:
        """Ask whether the question was typed the way it was meant.

        Called only when the query as typed found nothing, so the common path
        costs nothing at all. A candidate reading has to answer that same
        question — the index is the arbiter, not a heuristic about how the text
        looks.
        """

        def _substantive(text: str) -> list[str]:
            return [
                token.casefold()
                for token in tokens_of(text)
                if len(token) > 1 and token.casefold() not in _STOPWORDS
            ]

        # The caller has just established this, so it is not asked again.
        answered: dict[str, bool] = {clean_query: False}

        def _answerable(text: str) -> bool:
            if text not in answered:
                answered[text] = bool(self.storage.search_knowledge(user_id, text, limit=1))
            return answered[text]

        def _is_words(terms: Sequence[str]) -> bool:
            """Is this reading made of words the corpus uses, not collisions?"""
            long_enough = [term for term in terms if len(term) >= _MIN_MEANINGFUL_TERM]
            if not long_enough:
                return False
            return bool(self.storage.known_vocabulary(long_enough))

        try:
            return repair_query(
                clean_query,
                substantive_terms=_substantive,
                is_answerable=_answerable,
                is_words=_is_words,
                vocabulary=lambda prefixes: self.storage.vocabulary_terms(prefixes),
            )
        except Exception:
            # A repair is a courtesy. If anything about it fails — an old database
            # without the vocabulary view, say — the original question still gets
            # searched for.
            LOGGER.debug("Query repair failed for %r", clean_query, exc_info=True)
            return None

    async def search(
        self,
        user_id: str,
        query: str,
        *,
        limit: int = 20,
        include_entities: bool = True,
        kg: Any = None,
        explain: bool = False,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        # Ширина отбора кандидатов считается от ГЛУБИНЫ ОТБОРА, а не от размера
        # страницы. Раньше и то и другое росло от `limit`, и пул для переранжирования
        # усыхал вместе со страницей: Telegram просит восемь — FTS приносил сорок
        # кандидатов, админка просит двадцать — сотню, и это давали РАЗНЫЕ выдачи на
        # один вопрос. Замерено: при `limit=8` порог оставлял пустыми 12 вопросов из
        # 32, при `limit=20` — 10, разница целиком в составе пула. Собирать надо на ту
        # глубину, на которой работает переранжировщик; урезание до страницы — шаг
        # ПОСЛЕ, и он уже есть.
        depth = max(limit, self._rerank_top) if self._reranker is not None else limit
        clean_query = " ".join((query or "").split()).strip()
        if not clean_query:
            return {"query": query, "results": [], "count": 0, "entity_matches": []}

        fts_candidates = self.storage.search_knowledge(user_id, clean_query, limit=depth * 5)
        # Only when the question as typed matched nothing at all. A stuck keyboard
        # layout and a slipped finger produce strings the archive cannot match,
        # and both are ordinary human typing rather than nonsense — without this
        # they get the same empty answer as a pocket-dialled query, which is the
        # one case where an empty answer is right. Gated on the search that was
        # going to happen anyway, so a question that works pays nothing for this.
        repair = None
        if not fts_candidates:
            repair = self._repair_query(user_id, clean_query)
            if repair is not None:
                clean_query = repair.query
                fts_candidates = self.storage.search_knowledge(user_id, clean_query, limit=depth * 5)
        # Fuzzy matching needs a bounded recall pool even when FTS finds no exact token.
        pool_limit = min(self._pool_max, max(depth * 10, 100))
        recent_pool = self.storage.list_knowledge_objects(user_id, limit=pool_limit)
        candidate_map = {item["id"]: item for item in [*fts_candidates, *recent_pool]}
        candidates = list(candidate_map.values())

        fts_ranking = [item["id"] for item in fts_candidates]
        # A SECOND ranking, over the content-bearing words only, used by nothing but
        # the exclusion gate below.
        #
        # `fts_ranking` is a recall pool and it is right to be generous: FTS keeps
        # stopwords and searches every term as a prefix, deliberately (ARCHITECTURE
        # §«стоп-слова»). But `_exclusion_reason` then treated membership in that pool
        # as EVIDENCE, and `"по"*` matches 314 of 342 real documents, `"на"*` 290,
        # `"за"*` 259. Measured on the owner's corpus: 26.7% of answered results
        # contained not one content-bearing word of the question, and in 42 of 232
        # queries that filler filled the answer completely and pushed the right
        # document out.
        #
        # Removing the exemption outright is worse, not better — measured too:
        # recall@10 54.2% -> 50.8%, because `lex < 0.075` is unreachable for a long
        # document even when it contains every word of the query (39% of this corpus
        # scores under 0.075 against its own full title). The exemption has to stay
        # and be earned. Same prefix semantics, same tokenizer, one extra indexed
        # query: 26.7% -> 4.9% empty results, recall@10 52.5% -> 63.3%, 13 documents
        # recovered and none lost.
        substantive = [
            token for token in tokens_of(clean_query) if len(token) > 1 and token.casefold() not in _STOPWORDS
        ]
        if substantive and len(substantive) < len(tokens_of(clean_query)):
            evidence_ranking = {
                str(item["id"])
                for item in self.storage.search_knowledge(user_id, " ".join(substantive), limit=depth * 5)
            }
        else:
            # Nothing to strip, or nothing left after stripping: a question made of
            # stopwords has no better evidence than the pool it already produced.
            evidence_ranking = set(fts_ranking)
        # Shared by both `_lexical_rank` passes and by the snippet pass below, so a
        # candidate's body is tokenized once per request rather than once per use.
        lexical_cache: dict[str, dict[str, float]] = {}
        lexical_ranking, lexical_scores, shares_word = self._lexical_rank(
            candidates, clean_query, cache=lexical_cache
        )
        rankings = [fts_ranking, lexical_ranking]
        embedding_scores: dict[str, float] = {}
        dense_meta: dict[str, Any] = {}
        chunk_provenance: dict[str, tuple[int, int]] = {}
        chunk_carried: set[str] = set()
        if self.embeddings and self.embeddings.remote_enabled:
            embedding_scores = await self._dense_recall(user_id, clean_query, candidate_map, meta=dense_meta)
            chunk_provenance = dict(dense_meta.get("chunk_provenance") or {})
            chunk_carried = set(dense_meta.get("chunk_carried") or set())
            if embedding_scores:
                candidates = list(candidate_map.values())
                rankings.append(
                    [
                        document_id
                        for document_id, _ in sorted(
                            embedding_scores.items(), key=lambda pair: pair[1], reverse=True
                        )
                    ]
                )

        # The graph is a retrieval signal, not a decorative side channel.  Seed it with the best
        # lexical/FTS matches and bring linked knowledge into the pool.  Explicitly relational
        # questions may use two conservative hops; ordinary lookup stays at one to limit noise.
        graph_context: dict[str, Any] = {
            "nodes": [],
            "relations": [],
            "knowledge_candidates": [],
        }
        graph_scores: dict[str, float] = {}
        # Documents vouched for by an entity the QUERY named, as opposed to one
        # reached through another document. Only the first is evidence by itself
        # — see the gate below.
        graph_query_matched: set[str] = set()
        graph_evidence: dict[str, list[dict[str, Any]]] = {}
        graph_depth = self._graph_max_depth if _RELATIONAL_QUERY_RE.search(clean_query) else 1
        graph_evidence_threshold = 0.12 if graph_depth >= 2 else 0.20
        if kg:
            # A seed has to be a real match. `_lexical_rank` returns EVERY candidate
            # with no floor, so the top eight exist even for a question the archive
            # cannot answer — and a graph score of 0.677 clears `insufficient_evidence`
            # on its own, so those eight turned "I have nothing on this" into ten
            # unrelated personal documents. An empty answer is the only way the
            # system can say it does not know something.
            #
            # This filter was written, measured and REVERTED once, because it broke
            # the case the graph exists for: «Казань» against a document saying «в
            # Казани» scored 0.0597 lexically — under the floor — and reached the
            # answer only because it seeded the graph and its own entities vouched
            # for it back. The circularity was standing in for morphology. Now that
            # `lexical_vector` folds Russian inflection (see `_morphology`), that
            # same pair scores far above the floor on its own merits, and the seeds
            # can be honest. Order matters: applying this filter BEFORE stemming
            # would have made retrieval worse, not more honest.
            seed_ids = [
                document_id
                for document_id in dict.fromkeys([*fts_ranking[:8], *lexical_ranking[:8]])
                if document_id in fts_ranking or lexical_scores.get(document_id, 0.0) >= _LEXICAL_EVIDENCE_MIN
            ]
            try:
                graph_context = kg.context_for_query(
                    user_id,
                    clean_query,
                    seed_knowledge_ids=seed_ids,
                    entity_limit=8,
                    depth=graph_depth,
                    knowledge_limit=max(depth * 6, 50),
                )
                for candidate in graph_context.get("knowledge_candidates", []):
                    document_id = str(candidate.get("knowledge_object_id") or "")
                    if not document_id:
                        continue
                    item = candidate_map.get(document_id) or self.storage.get_knowledge_object(
                        document_id,
                        user_id,
                    )
                    if not item or item.get("deleted_at"):
                        continue
                    candidate_map[document_id] = item
                    if candidate.get("query_matched"):
                        graph_query_matched.add(document_id)
                    graph_scores[document_id] = max(
                        graph_scores.get(document_id, 0.0),
                        float(candidate.get("score", 0.0)),
                    )
                    graph_evidence[document_id] = list(candidate.get("evidence") or [])
            except Exception:
                # Search must remain available even if graph enrichment encounters bad legacy data.
                graph_context = {"nodes": [], "relations": [], "knowledge_candidates": []}

        # Re-rank after graph expansion so newly discovered records receive lexical evidence too.
        candidates = list(candidate_map.values())
        lexical_ranking, lexical_scores, shares_word = self._lexical_rank(
            candidates, clean_query, cache=lexical_cache
        )
        rankings[1] = lexical_ranking
        if graph_scores:
            rankings.append(
                [
                    document_id
                    for document_id, _ in sorted(graph_scores.items(), key=lambda item: item[1], reverse=True)
                ]
            )

        # A match in a concise, curated field is more trustworthy than the same token buried in a
        # long body. Entity names are loaded in one batch so this remains cheap on a local SQLite KB.
        entity_links = self._entity_links_by_document(user_id, list(candidate_map))
        field_components: dict[str, dict[str, float]] = {}
        field_scores: dict[str, float] = {}
        identifier_coverage: dict[str, float] = {}
        query_identifiers = self._query_identifiers(clean_query)
        query_lexical_vector = lexical_vector(clean_query)
        for document_id, item in candidate_map.items():
            names = [str(entity.get("name") or "") for entity in entity_links.get(document_id, [])]
            fields = self._field_scores(item, clean_query, names, query_vector=query_lexical_vector)
            field_components[document_id] = fields
            field_scores[document_id] = min(
                1.0,
                fields["title"] * 0.32
                + fields["summary"] * 0.14
                + fields["tags"] * 0.20
                + fields["entities"] * 0.24
                + fields["exact_phrase"] * 0.18,
            )
            identifier_coverage[document_id] = self._identifier_coverage(item, query_identifiers, names)
        # The curated-field signal is deliberately NOT a fourth RRF ranking. It used
        # to be one, admitted by an absolute `score >= 0.035` — a threshold sitting
        # inside the signal's working range (median per-query maximum 0.05), which
        # made the most trusted signal behave as a binary trigger: two documents at
        # 0.0349 / 0.0351 diverged by a whole RRF membership step (~0.022, about 7
        # positions at the median adjacent gap), and in 8 of 20 measured queries the
        # channel silently vanished from the fusion altogether. Measured on the real
        # 342-document stand, two disjoint 120-document query samples (title-token
        # and body-token queries, embeddings and graph off):
        #
        #     recall@10        base 187/198 and 178/186
        #     without the RRF  187/198 and 178/186   (MRR ±0.006, noise)
        #     threshold-free   181/198 and 177/186   (strictly worse: trigram dust
        #                       from unrelated titles buys whole RRF memberships)
        #
        # The channel's information survives in two graduated places: `field * 0.17`
        # in the blend and `fld >= 0.12` as evidence in the exclusion gates. FTS
        # already carries exact title/tag tokens into the fusion, which is why
        # removing the redundant ranking costs nothing — and a cliff nothing pays
        # for should not decide seven positions.
        rrf = reciprocal_rank_fusion([ranking for ranking in rankings if ranking], k=45)
        feedback = self._feedback_scores(user_id, list(candidate_map))
        usage = self.storage.get_knowledge_usage(user_id, list(candidate_map))
        # Ablation seam: an advisory harness zeroes ONE weight and re-measures the gold
        # set, which is what turns a hand-tuned constant into a measured one. Lifted
        # out of the loop so the blend expression below stays a single readable line
        # per signal, and so the default path is the literal constant it always was.
        off = self._ablate
        w_feedback = 0.0 if "feedback" in off else 0.05
        w_usage = 0.0 if "usage" in off else 0.028
        w_kind = 0.0 if "kind_alignment" in off else 0.035
        w_fts_bonus = 0.0 if "fts_bonus" in off else 0.018
        w_noise = 0.0 if "noise_penalty" in off else 1.0
        final_scores: dict[str, float] = {}
        components: dict[str, dict[str, float]] = {}
        for document_id, item in candidate_map.items():
            lifecycle_factor = {
                "active": 1.0,
                "archived": 0.72,
                "deprecated": 0.36,
            }.get(str(item.get("lifecycle_stage", "active")), 0.25)
            importance = self._bounded(item.get("importance"), 0.5)
            quality = self._bounded(item.get("quality_score"), 0.5)
            promotion = self._bounded(item.get("promotion_score"), 0.5)
            lexical = max(0.0, lexical_scores.get(document_id, 0.0))
            embedding = max(0.0, embedding_scores.get(document_id, 0.0))
            graph = max(0.0, graph_scores.get(document_id, 0.0))
            field = max(0.0, field_scores.get(document_id, 0.0))
            identifiers = identifier_coverage.get(document_id, 1.0)
            user_feedback = max(-1.0, min(1.0, feedback.get(document_id, 0.0)))
            usage_row = usage.get(document_id, {})
            retrieval_count = max(0, int(usage_row.get("retrieval_count") or 0))
            answer_count = max(0, int(usage_row.get("answer_count") or 0))
            positive_count = max(0, int(usage_row.get("positive_feedback_count") or 0))
            negative_count = max(0, int(usage_row.get("negative_feedback_count") or 0))
            # Usage is a weak tie-breaker, not popularity lock-in.  Logarithmic
            # saturation and a low cap prevent old frequently seen notes from
            # drowning out a precise new fact.
            usage_signal = min(
                1.0,
                math.log1p(retrieval_count) * 0.08
                + math.log1p(answer_count) * 0.18
                + max(-0.4, min(0.4, (positive_count - negative_count) * 0.08)),
            )
            kind_alignment = self._kind_alignment(clean_query, str(item.get("knowledge_kind", "note")))
            noise_penalty = self._noise_penalty(item)
            # Identifier coverage is NOT a weight. A candidate missing any query
            # identifier is dropped outright by `identifier_mismatch` in
            # `_exclusion_reason` below, and that gate fires for exactly the
            # candidates a penalty here would touch — both read the same coverage
            # map over the same keys. A `-0.22` term lived here for a while and
            # could never change a single ranking: whoever tunes strictness must
            # tune the gate, so the knob that does nothing is gone.
            fts_bonus = w_fts_bonus if document_id in fts_ranking else 0.0
            base = (
                rrf.get(document_id, 0.0)
                + lexical * self._channel_weights["lexical"]
                + field * self._channel_weights["field"]
                + embedding * self._channel_weights["embedding"]
                + graph * self._channel_weights["graph"]
                + importance * 0.035
                + quality * 0.045
                + promotion * 0.03
                + user_feedback * w_feedback
                + usage_signal * w_usage
                + kind_alignment * w_kind
                + fts_bonus
                - noise_penalty * w_noise
            )
            quality_factor = 0.42 + quality * 0.38 + promotion * 0.20
            final_scores[document_id] = max(0.0, base * lifecycle_factor * quality_factor)
            components[document_id] = {
                "rrf": round(rrf.get(document_id, 0.0), 6),
                "lexical": round(lexical, 6),
                "field": round(field, 6),
                "title_match": round(field_components[document_id]["title"], 6),
                "summary_match": round(field_components[document_id]["summary"], 6),
                "tag_match": round(field_components[document_id]["tags"], 6),
                "entity_match": round(field_components[document_id]["entities"], 6),
                "exact_phrase": round(field_components[document_id]["exact_phrase"], 6),
                "identifier_coverage": round(identifiers, 6),
                "embedding": round(embedding, 6),
                # The passage the excerpt is taken from; -1 when the object has no
                # passage vectors at all.
                "embedding_chunk": chunk_provenance.get(document_id, (-1, 0))[0],
                "embedding_chunks": chunk_provenance.get(document_id, (-1, 0))[1],
                "graph": round(graph, 6),
                "importance": round(importance, 6),
                "quality": round(quality, 6),
                "promotion": round(promotion, 6),
                "feedback": round(user_feedback, 6),
                "usage": round(usage_signal, 6),
                "kind_alignment": round(kind_alignment, 6),
                # The APPLIED bonus, not the weight: an admin replaying the blend
                # from this map must get the published score back. This term and
                # `quality_factor` used to be missing, and with a median gap of
                # ~0.003 between adjacent ranks an invisible 0.018 was large
                # enough to explain almost any neighbouring pair — the trace
                # claimed to show the ranker's arithmetic and did not add up.
                "fts_bonus": round(fts_bonus, 6),
                "noise_penalty": round(noise_penalty, 6),
                "lifecycle_factor": round(lifecycle_factor, 6),
                "quality_factor": round(quality_factor, 6),
            }

        def _exclusion_reason(
            document_id: str,
            item: dict[str, Any],
            lex: float,
            fld: float,
            emb: float,
            grp: float,
        ) -> str | None:
            """Why a scored candidate does not make the result set — centralised so
            the explain-trace reports the exact same reasons the ranker applies."""
            # Exact identifiers are discrete evidence, not fuzzy language: a query
            # mentioning BRK.A must not be satisfied by a nearby BRK.B record.
            if query_identifiers and identifier_coverage.get(document_id, 0.0) < 1.0:
                return "identifier_mismatch"
            # Recent-pool records need real evidence; graph expansion or a strong
            # curated-field match can satisfy it without repeating body terms.
            # The graph counts as evidence only when the QUERY named the entity.
            # «Этот документ про то, о чём вы спросили» is a claim about the
            # document; «этот документ делит сущность с чем-то, что совпало» is a
            # claim about its neighbour, and the score is the same number either
            # way — a seeded entity vouches at a flat 0.677, which clears any
            # threshold on its own. Measured on the real corpus after seeds were
            # filtered honestly: ten invented-word questions still returned 20
            # documents, every one of them carried by the graph alone, and every
            # one reached through a neighbour rather than through the question.
            graph_evidence_value = grp if document_id in graph_query_matched else 0.0
            # A lexical score counts only when the document actually contains one
            # of the query's words — see `_lexical_rank`. Trigram similarity is
            # useful for ORDERING and useless as proof: nonsense words reach 0.086
            # against ordinary Russian text on letter overlap alone.
            lexical_evidence = lex if document_id in shares_word else 0.0
            if (
                document_id not in evidence_ranking
                and lexical_evidence < _LEXICAL_EVIDENCE_MIN
                and fld < 0.12
                and emb < self._dense_evidence_min
                and graph_evidence_value < graph_evidence_threshold
            ):
                return "insufficient_evidence"
            # A deprecated record needs STRONGER evidence than a live one to earn a
            # slot — but every kind of evidence must count. This gate once looked
            # only at FTS membership, `lex` and `grp`, so a deprecated note recalled
            # by meaning (cosine 0.9) or by an exact title/tag match was gone for
            # good: two lines above, `insufficient_evidence` had just accepted
            # `emb >= dense_min` / `fld >= 0.12` as proof, and this gate threw the
            # object away without looking at either. `lex >= 0.25` alone could not
            # save it — measured across 11 286 query x candidate pairs on a real
            # corpus, p99 lexical is 0.153; even a query built from a document's own
            # title clears 0.25 in 8.6% of cases. DATA_LIFECYCLE promises a
            # deprecated record «остаётся в поиске» with a demoted rank, and the
            # demotion already happens in the blend (`lifecycle_factor` 0.36).
            #
            # FTS membership is `evidence_ranking`, not `fts_ranking`, for the same
            # measured reason as `insufficient_evidence`: `"по"*` matches 314 of 342
            # documents, so a stopword-prefix hit is presence, not proof.
            if (
                item.get("lifecycle_stage") == "deprecated"
                and document_id not in evidence_ranking
                and lex < 0.25
                and fld < 0.12
                and emb < self._dense_evidence_min
                and grp < 0.45
            ):
                return "deprecated_weak"
            return None

        ordered = sorted(candidate_map, key=lambda document_id: final_scores[document_id], reverse=True)
        # При включённом переранжировании собирается ГЛУБЖЕ запрошенного: смысл шага в
        # том, чтобы поднять отвечающий документ с двенадцатого места на второе, а для
        # этого его надо сначала собрать. Замерено, ради чего: точность выдачи плоская
        # по глубине (35.9% в пятёрке против 35.2% в двадцатке), то есть отвечающие
        # разбросаны по списку.
        collect = max(limit, self._rerank_top) if self._reranker is not None else limit
        results: list[dict[str, Any]] = []
        for document_id in ordered:
            if len(results) >= collect:
                break
            item = self.storage.get_knowledge_object(document_id, user_id)
            if not item or item.get("deleted_at"):
                continue
            lexical_score = lexical_scores.get(document_id, 0.0)
            graph_score = graph_scores.get(document_id, 0.0)
            embedding_score = embedding_scores.get(document_id, 0.0)
            field_score = field_scores.get(document_id, 0.0)
            if _exclusion_reason(document_id, item, lexical_score, field_score, embedding_score, graph_score):
                continue
            item["_score"] = round(final_scores[document_id], 6)
            item["_lexical_score"] = round(lexical_score, 6)
            item["_field_score"] = round(field_score, 6)
            item["_field_matches"] = field_components[document_id]
            item["_embedding_score"] = round(embedding_score, 6)
            if document_id in chunk_provenance:
                item["_embedding_chunk"] = chunk_provenance[document_id][0]
            item["_graph_score"] = round(graph_score, 6)
            item["_feedback_score"] = round(feedback.get(document_id, 0.0), 6)
            item["_score_components"] = components[document_id]
            if graph_evidence.get(document_id):
                item["_graph_evidence"] = graph_evidence[document_id]
            entities = entity_links.get(document_id, [])
            if entities:
                item["_entities"] = entities
            results.append(item)

        reranked_count = 0
        rerank_cut: set[str] = set()
        rerank_scores: dict[str, float] = {}
        # Условие «кандидатов больше запрошенного» здесь стояло, пока шаг только
        # ПЕРЕСТАВЛЯЛ: переставлять пятёрку внутри пятёрки бессмысленно. С порогом у
        # шага появилась вторая работа — отсев, — и нужен он как раз тогда, когда
        # нашлось мало: три правдоподобных документа, ни один из которых не о том,
        # выглядят убедительнее двадцати.
        if self._reranker is not None and self._rerank_top > 0 and len(results) >= 2:
            reordered = await self._reranker(clean_query, results[: self._rerank_top])
            if reordered is not None and len(reordered) == len(results[: self._rerank_top]):
                # Порядок меняется, состав — нет: хвост за `rerank_top` остаётся на
                # месте. Проверка длины не формальность: потеря объекта выглядела бы
                # как «поиск ничего не нашёл», а не как сбой модели.
                results = reordered + results[self._rerank_top :]
                reranked_count = len(reordered)
                if self._confident_min > 0.0:
                    # Отсев по откалиброванному скору. Замер размена — в
                    # `_rerank_backend.py`: доля отвечающих среди показанного растёт
                    # 43.5% → 78.6%, и на 6 безответных вопросах из 7 система начинает
                    # верно молчать, ценой 4 вопросов из 25, у которых ответ был.
                    # Размен принят владельцем осознанно.
                    #
                    # Режется ДО обрезки по limit, а не после: иначе отсев съедал бы
                    # места в странице, и человек получал бы два документа там, где
                    # прошедших порог восемь.
                    #
                    # Документы БЕЗ скора (хвост за `rerank_top`) не режутся: про них
                    # ничего не измерено, и молчаливый отказ по отсутствию оценки — это
                    # приписывание модели решения, которого она не принимала.
                    kept: list[dict[str, Any]] = []
                    for item in results:
                        score = item.get("_rerank_score")
                        if score is None:
                            kept.append(item)
                            continue
                        rerank_scores[str(item["id"])] = float(score)
                        if float(score) < self._confident_min:
                            rerank_cut.add(str(item["id"]))
                            continue
                        kept.append(item)
                    results = kept
        results = results[:limit]

        # Resolve the winning passage's offsets so the answer can quote what actually
        # matched. Without this, a document recalled semantically at section 7 would
        # still be shown to the LLM through a LEXICALLY chosen window near its head —
        # precisely the case passage-level recall creates more of.
        if chunk_provenance and self.embeddings is not None:
            wanted = [
                (str(item["id"]), int(item["_embedding_chunk"]))
                for item in results
                if item.get("_embedding_chunk") is not None
                and int(item["_embedding_chunk"]) >= 0
                # Only when dense recall is the REASON the object is here. A hit that
                # also matched lexically must keep excerpting over the whole body, or
                # the excerpt could drop the very phrase the user searched for.
                and item["id"] not in fts_ranking
            ]
            if wanted:
                with suppress(Exception):
                    spans = self.storage.get_chunk_spans(
                        user_id, self.embeddings.settings.embeddings_model, wanted
                    )
                    for item in results:
                        key = (str(item["id"]), int(item.get("_embedding_chunk", -1)))
                        if key in spans:
                            item["_embedding_chunk_span"] = list(spans[key])

        # Ranking remains available if a read-only/locked deployment cannot
        # persist this optional, best-effort usage signal. An advisory harness turns
        # the write off entirely: ``usage_signal`` reads this counter back into the
        # blend, so measuring the corpus would otherwise change it.
        if self._record_usage:
            with suppress(Exception):
                top_score = float(results[0].get("_score", 0.0) or 0.0) if results else 0.0
                usage_floor = max(0.015, top_score * 0.32)
                retrieved_ids = [
                    str(item["id"])
                    for item in results[:5]
                    if item.get("id") and float(item.get("_score", 0.0) or 0.0) >= usage_floor
                ]
                self.storage.record_knowledge_usage(
                    user_id,
                    retrieved_ids,
                    retrieved=True,
                )

        if include_entities and kg:
            entity_matches = list(graph_context.get("nodes", []))[:5]
            if not entity_matches:
                entity_matches = kg.search_entities(user_id, clean_query, limit=5)
        else:
            entity_matches = []
        strategy: dict[str, Any] = {
            "fts": True,
            "lexical": True,
            "embeddings": bool(embedding_scores),
            "feedback": True,
            "graph": bool(kg),
        }
        if reranked_count:
            # Видно в explain-трейсе: порядок выдачи решала не только формула, и
            # человек, разбирающий «почему это первым», должен об этом знать.
            strategy["reranked"] = reranked_count
            if rerank_cut:
                # Сколько кандидатов отсеяно как не отвечающие. Число нужно ровно там,
                # где выдача опустела: «ничего не нашлось» и «нашлось двадцать, но ни
                # одно не о том» — разные ответы, и до этого поля они выглядели
                # одинаково. Пустой результат на пустом архиве уже был отделён от
                # пустого на большом (`lexical_pool_scanned`), это тот же долг.
                strategy["rerank_dropped"] = len(rerank_cut)
        if repair is not None:
            # Said out loud, always: the answer is about a different string than
            # the one the user typed, and a reader who is not told that has no way
            # to notice a repair that guessed wrong.
            strategy["query_repaired"] = repair.kind
            strategy["query_as_typed"] = query
            if repair.detail:
                strategy["query_repair_detail"] = repair.detail
        if chunk_provenance:
            # At least one object was carried by a passage rather than its whole-object
            # vector — the visible signature of passage-level recall doing work.
            strategy["embeddings_chunked"] = True
        if dense_meta.get("dense_chunks_capped"):
            strategy["embeddings_chunks_capped"] = True
        if dense_meta.get("dense_capped"):
            # Dense recall scored only the newest N vectors — latency degrades
            # visibly (in the explain-trace) rather than silently on a big corpus.
            strategy["embeddings_capped"] = True
        if len(recent_pool) >= pool_limit:
            # The pool came back full, so the corpus may be larger than what the
            # lexical channel actually looked at. The count is only paid here, on a
            # saturated pool: an empty result over 8000 objects and an empty result
            # over 40 mean opposite things, and until now they printed the same.
            corpus_size = self.storage.count_knowledge_objects(user_id)
            if corpus_size > pool_limit:
                strategy["lexical_pool_capped"] = True
                strategy["lexical_pool_scanned"] = pool_limit
                strategy["corpus_size"] = corpus_size
        response: dict[str, Any] = {
            "query": clean_query,
            "results": results,
            "count": len(results),
            "entity_matches": entity_matches,
            "strategy": strategy,
        }
        if explain:
            # Every candidate was already scored; expose the ranked set (returned,
            # below-limit, and discarded-with-reason) plus each one's signal
            # breakdown so an admin can see WHY the ranking came out this way.
            returned_ids = {item["id"] for item in results}
            trace: list[dict[str, Any]] = []
            rank = 0
            trace_cap = max(limit * 3, 30)
            for document_id in ordered:
                item_meta = candidate_map.get(document_id) or {}
                reason = (
                    "deleted"
                    if item_meta.get("deleted_at")
                    else _exclusion_reason(
                        document_id,
                        item_meta,
                        lexical_scores.get(document_id, 0.0),
                        field_scores.get(document_id, 0.0),
                        embedding_scores.get(document_id, 0.0),
                        graph_scores.get(document_id, 0.0),
                    )
                )
                # Отсев переранжировщиком — такая же причина, как три остальные, и
                # называется вслух по той же причине: без неё документ, снятый ЗА
                # порог, лежал бы в трейсе как «не поместился в страницу», и вопрос
                # «он же точно про это, почему его нет» снова остался бы без ответа.
                if not reason and document_id in rerank_cut:
                    reason = "rerank_below_threshold"
                if reason:
                    status: str = "discarded"
                    entry_rank: int | None = None
                elif document_id in returned_ids:
                    status, entry_rank = "returned", rank
                    rank += 1
                else:
                    status, entry_rank = "below_limit", None
                    rank += 1
                trace.append(
                    {
                        "id": document_id,
                        "title": str(item_meta.get("title") or "Без названия")[:160],
                        "knowledge_kind": item_meta.get("knowledge_kind"),
                        "lifecycle_stage": item_meta.get("lifecycle_stage"),
                        "score": round(final_scores.get(document_id, 0.0), 6),
                        "status": status,
                        "rank": entry_rank,
                        "reason": reason,
                        # WHICH signal carried the retrieval — a different question
                        # from which passage is quoted, and it used to be answered by
                        # silently discarding the second one. Outside `components`
                        # because that map is numeric by contract.
                        "embedding_source": (
                            "chunk"
                            if document_id in chunk_carried
                            else ("document" if document_id in chunk_provenance else "none")
                        ),
                        # Скор переранжировщика — рядом с причиной отсева, иначе
                        # «rerank_below_threshold» это ярлык без числа, и порог не с
                        # чем сверить.
                        "rerank_score": rerank_scores.get(document_id),
                        "components": components.get(document_id, {}),
                    }
                )
                if len(trace) >= trace_cap:
                    break
            response["trace"] = trace
        return response

    @staticmethod
    def _bounded(value: Any, default: float) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    @staticmethod
    def _json_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return [value] if value.strip() else []
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        return []

    @staticmethod
    def _is_prose_not_an_identifier(token: str) -> bool:
        """Reject the two shapes that look like codes and are ordinary language.

        `identifier_mismatch` is the FIRST gate and it has no escape hatch: a token
        it accepts must appear verbatim in a document or that document is dropped.
        So anything it accepts by mistake empties the answer.

        Measured on 342 real documents: adding « и т.д.» — three of the commonest
        characters in written Russian — to a working query made `т.д` a required
        identifier and took the correct document from 15 of 15 answers to **0 of
        15**. Adding «примерно 12.5 процента» returned **nothing at all**, from any
        query, because no document contains the literal `12.5`.

        Two rules, both about shape rather than a list of words:

        * every dot-separated part is a single character — `т.д`, `т.п`, `и.о`,
          `e.g`, `i.e`. A real code has at least one part worth naming: `BRK.A`
          keeps its `BRK`. (A guard for the single-part case was written and then
          removed: mutation showed nothing reached it, because a lone character
          carries no separator and never gets this far.)
        * no letters at all — `12.5`, `9.00`, `4.2`. A number is a quantity, and
          requiring it verbatim turns "about 12.5 percent" into a search for a
          string.
        """
        parts = [part for part in token.replace("/", ".").replace("#", ".").split(".") if part]
        if parts and all(len(part) == 1 for part in parts):
            return True
        return not any(character.isalpha() for character in token)

    @staticmethod
    def _query_identifiers(query: str) -> set[str]:
        identifiers: set[str] = set()
        for token in tokens_of(query):
            if HybridSearcher._is_prose_not_an_identifier(token):
                continue
            has_discrete_separator = any(character in token for character in "._/#")
            has_hyphenated_code = "-" in token and any(character.isdigit() for character in token)
            has_alphanumeric_code = (
                any(character.isdigit() for character in token)
                and any(character.isalpha() for character in token)
                and token.upper() == token
            )
            if has_discrete_separator or has_hyphenated_code or has_alphanumeric_code:
                identifiers.add(token.casefold())
        return identifiers

    def _identifier_coverage(
        self,
        item: dict[str, Any],
        identifiers: set[str],
        entity_names: list[str],
    ) -> float:
        if not identifiers:
            return 1.0
        text = self._search_text(item) + " " + " ".join(entity_names)
        # Necessary condition first, at C speed. `tokens_of` only splits the text,
        # strips trailing `.-` and folds ё — all of which leave every token a
        # substring of the text folded the same way, so an identifier absent as a
        # substring cannot be present as a token. Tokenizing every candidate body on
        # every query was the single largest cost in a search: profiled on the
        # 342-document stand, `_identifier_coverage` was 86% of the time inside
        # `search` and 92% of everything the graph added to it, because a candidate
        # pool of ~150 documents means ~150 full-body tokenizations per question.
        folded = text.translate(_YO_FOLD).casefold()
        if not any(identifier in folded for identifier in identifiers):
            return 0.0
        tokens = {token.casefold() for token in tokens_of(text)}
        return len(identifiers & tokens) / len(identifiers)

    def _entity_links_by_document(
        self, user_id: str, document_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        output: dict[str, list[dict[str, Any]]] = {}
        unique_ids = list(dict.fromkeys(document_ids))
        for start in range(0, len(unique_ids), 350):
            chunk = unique_ids[start : start + 350]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            # The only interpolated fragment is a bounded sequence of ``?`` placeholders.
            query = f"""SELECT l.knowledge_object_id, l.entity_id, l.confidence,
                           e.name, e.entity_type
                    FROM knowledge_entity_links l
                    JOIN entities e ON e.id=l.entity_id AND e.user_id=l.user_id
                    WHERE l.user_id=? AND l.status='accepted' AND e.deleted_at IS NULL
                      AND l.knowledge_object_id IN ({placeholders})
                    ORDER BY l.confidence DESC, e.name COLLATE NOCASE"""  # nosec B608
            rows = self.storage.execute(query, (user_id, *chunk)).fetchall()
            for row in rows:
                output.setdefault(str(row["knowledge_object_id"]), []).append(
                    {
                        "id": row["entity_id"],
                        "name": row["name"],
                        "type": row["entity_type"],
                        "confidence": float(row["confidence"] or 0.0),
                    }
                )
        return output

    def _noise_penalty(self, item: dict[str, Any]) -> float:
        metadata = self._json_dict(item.get("metadata_json"))
        assessment = metadata.get("promotion_assessment")
        if not isinstance(assessment, dict):
            assessment = {}
        category = str(assessment.get("category", ""))
        action = str(assessment.get("action", ""))
        penalty = 0.0
        if category in {"question", "greeting", "command"}:
            penalty += 0.12
        if action == "transient":
            penalty += 0.12
        if self._bounded(item.get("quality_score"), 0.5) < 0.28:
            penalty += 0.08
        if self._bounded(item.get("promotion_score"), 0.5) < 0.28:
            penalty += 0.07
        content = str(item.get("content") or "").strip()
        if len(content.split()) < 5 and str(item.get("content_type")) != "file":
            penalty += 0.045
        return min(0.28, penalty)

    @staticmethod
    def _kind_alignment(query: str, knowledge_kind: str) -> float:
        patterns = {
            "task": r"\b(?:задач|сделать|дедлайн|todo|task|deadline)\w*",
            "decision": r"\b(?:решени|решили|decision|decided)\w*",
            "preference": r"\b(?:предпочит|нрав|любим|preference|prefer|favou?r)\w*",
            "event": r"\b(?:встреч|событи|конференц|meeting|event)\w*",
            "project": r"\b(?:проект|репозитор|project|repository)\w*",
            "procedure": r"\b(?:инструкц|процедур|настро|runbook|procedure)\w*",
            "contact": r"\b(?:контакт|телефон|почт|contact|email)\w*",
            "document": r"\b(?:файл|документ|отч[её]т|file|document|report)\w*",
        }
        pattern = patterns.get(knowledge_kind)
        return 1.0 if pattern and re.search(pattern, query, re.I) else 0.0

    @staticmethod
    def _search_text(item: dict[str, Any]) -> str:
        return knowledge_search_text(item)

    def _cached_vector(self, item: dict[str, Any]) -> dict[str, float]:
        """The candidate's lexical vector, reused until its row changes."""
        key = (
            str(item.get("id") or ""),
            str(item.get("version") or ""),
            str(item.get("updated_at") or ""),
        )
        cached = self._vector_cache.get(key)
        if cached is not None:
            self._vector_cache.move_to_end(key)
            return cached
        vector = lexical_vector(self._search_text(item))
        self._vector_cache[key] = vector
        while len(self._vector_cache) > _VECTOR_CACHE_MAX:
            self._vector_cache.popitem(last=False)
        return vector

    def _cached_field_vectors(self, item: dict[str, Any]) -> tuple[dict[str, float], ...]:
        """Title, summary and tag vectors — row-derived, so they outlive one query.

        `_field_scores` runs once per candidate, and three of its four vectors depend
        only on the candidate's own row. On a ~150-document pool that was ~450 rebuilds
        of the same short vectors per question, and `lexical_vector` was the largest
        remaining cost in a search once `_identifier_coverage` stopped tokenizing
        every body. The entities vector is deliberately NOT cached here: its input
        comes from the link table, which changes without touching this row.
        """
        key = (
            str(item.get("id") or ""),
            str(item.get("version") or ""),
            str(item.get("updated_at") or ""),
        )
        cached = self._field_vector_cache.get(key)
        if cached is not None:
            self._field_vector_cache.move_to_end(key)
            return cached
        vectors = (
            lexical_vector(str(item.get("title") or "")),
            lexical_vector(str(item.get("summary") or "")),
            lexical_vector(" ".join(self._json_list(item.get("tags_json")))),
        )
        self._field_vector_cache[key] = vectors
        while len(self._field_vector_cache) > _VECTOR_CACHE_MAX:
            self._field_vector_cache.popitem(last=False)
        return vectors

    def _lexical_rank(
        self,
        candidates: list[dict[str, Any]],
        query: str,
        *,
        cache: dict[str, dict[str, float]] | None = None,
    ) -> tuple[list[str], dict[str, float], set[str]]:
        query_vector = lexical_vector(query)
        # One vector per candidate per request. `_lexical_rank` runs twice — once
        # before dense recall widens the pool and once after — and each run rebuilt
        # the token and trigram vector of every candidate's FULL body from scratch.
        # Profiling one search over a 400-candidate pool counted 2708 calls to
        # `lexical_vector`, about half the request's total time.
        vectors = cache if cache is not None else {}
        # Which query WORDS exist at all, separately from how similar the texts
        # look. `lexical_vector` mixes word features with character trigrams, and
        # trigrams are what let a document score without sharing a single word:
        # measured on the real corpus, invented Russian-shaped words
        # («переквантовать сизиморбность») reach 0.081-0.086 against ordinary
        # documents — over the 0.075 evidence floor — purely on letter overlap.
        # Trigrams earn their place in RANKING, where approximate is useful; in a
        # gate that decides whether there is any evidence at all they are noise
        # wearing the shape of a score.
        query_words = {key for key in query_vector if key.startswith("w:")}
        scored: list[tuple[str, float]] = []
        shares_word: set[str] = set()
        for item in candidates:
            document_id = str(item["id"])
            vector = vectors.get(document_id)
            if vector is None:
                vector = self._cached_vector(item)
                vectors[document_id] = vector
            scored.append((document_id, sparse_cosine(query_vector, vector)))
            if query_words and not query_words.isdisjoint(vector):
                shares_word.add(document_id)
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return [document_id for document_id, _ in scored], dict(scored), shares_word

    async def _dense_recall(
        self,
        user_id: str,
        query: str,
        candidate_map: dict[str, dict[str, Any]],
        *,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, float]:
        """Corpus-wide dense recall over persisted vectors.

        The query is embedded once, cosine-scored against every stored vector for
        the active model, and the strongest matches are UNIONED into the candidate
        pool — so a semantically relevant object that shares no lexical tokens and is
        not recent still becomes retrievable. When no vectors are indexed yet (the
        indexer has not run since embeddings were enabled), this degrades to
        re-ranking the existing pool by embedding the candidate texts directly.
        """
        assert self.embeddings is not None
        # Плотный канал — улучшение, а не условие ответа: лексика и граф уже дали
        # результат, и ждать ради добавки дольше, чем человек готов ждать весь ответ,
        # значит менять полезное на бесполезное.
        query_vectors = await self.embeddings.embed(
            [query], budget_sec=self.embeddings.settings.retrieval_dense_query_budget_sec
        )
        if not query_vectors:
            return {}
        query_vector = query_vectors[0]
        query_dim = len(query_vector)
        if query_dim == 0:
            return {}
        settings = self.embeddings.settings
        model = settings.embeddings_model
        max_objects = int(settings.embeddings_dense_max_objects)
        stored = self.storage.get_user_embeddings(user_id, model, query_dim, limit=(max_objects or None))
        if meta is not None:
            # Deliberately still counted in OBJECTS: the cap, its explain-trace flag
            # and the operator-facing wording all keep the meaning they had.
            meta["dense_scanned"] = len(stored)
            meta["dense_capped"] = bool(max_objects) and len(stored) >= max_objects
        doc_scores = {
            document_id: score for score, document_id in dense_scores(query_vector, stored, query_dim)
        }

        chunk_scores: dict[str, float] = {}
        provenance: dict[str, tuple[int, int]] = {}
        chunk_rows: list[tuple[str, bytes]] = []
        if settings.embeddings_chunk_chars > 0 and self._chunk_recall:
            # Floored at one object's worth of chunks: the fuse exists to bound a
            # heavily split corpus, never to scan a single document only halfway.
            row_cap = max(
                max_objects * max(1, int(settings.embeddings_chunk_scan_multiplier)),
                int(settings.embeddings_chunk_max_per_object) if max_objects else 0,
            )
            # Over-fetch by one object's worth, then drop the trailing PARTIAL object:
            # a plain LIMIT cuts at an arbitrary chunk index, which both hides the
            # passage that answers the query and — because the corroboration term
            # averages over however many chunks were scanned — hands the truncated
            # document an undiscounted score it did not earn.
            over_fetch = int(settings.embeddings_chunk_max_per_object)
            fetched = self.storage.get_user_chunk_embeddings(
                user_id,
                model,
                query_dim,
                object_limit=(max_objects or None),
                row_limit=(row_cap + over_fetch if row_cap else None),
            )
            chunk_rows = _trim_to_whole_objects(fetched, row_cap) if row_cap else fetched
            if meta is not None:
                meta["dense_chunks_scanned"] = len(chunk_rows)
                meta["dense_chunks_capped"] = bool(row_cap) and len(chunk_rows) < len(fetched)
            chunk_scores, provenance = aggregate_chunk_scores(
                dense_scores(query_vector, chunk_rows, query_dim),
                blend=float(settings.embeddings_chunk_blend),
            )

        if not stored and not chunk_rows:
            # Nothing indexed yet: degrade to re-ranking the pool, exactly as before.
            return await self._dense_recall_pool(query_vector, candidate_map)

        # The whole-object vector is the FLOOR: passage scores can only raise an
        # object, never lower it, so chunking cannot regress any existing result.
        # Built in a DETERMINISTIC order (doc rows first, in DB order) — a set union
        # would iterate in per-process string-hash order and leak PYTHONHASHSEED into
        # tie-breaking, which must stay identical to pre-0.41 when chunking is off.
        combined: dict[str, float] = {}
        for document_id in (*doc_scores, *chunk_scores):
            if document_id not in combined:
                combined[document_id] = max(
                    doc_scores.get(document_id, -1.0), chunk_scores.get(document_id, -1.0)
                )
        if not combined:
            return {}

        # Promote from BOTH rankings. Selecting on the combined score alone would let
        # chunk-boosted long documents evict objects the whole-object vector had
        # already earned (max-over-passages is systematically higher than the document
        # average) — and an object outside candidate_map loses its score entirely, so
        # the floor would protect the value while the selection silently dropped it.
        # Ties break on (score, id), matching the pre-0.41 nlargest over (score, id).
        top_k = max(1, int(settings.embeddings_recall_candidates))

        def _ranked(scores: dict[str, float]) -> list[str]:
            return [
                document_id
                for document_id, _ in heapq.nlargest(
                    top_k, scores.items(), key=lambda pair: (pair[1], pair[0])
                )
            ]

        promoted = list(dict.fromkeys(_ranked(doc_scores) + _ranked(combined)))
        for document_id in promoted:
            if document_id in candidate_map:
                continue
            item = self.storage.get_knowledge_object(document_id, user_id)
            if item and not item.get("deleted_at"):
                candidate_map[document_id] = item
        if meta is not None:
            # Per-call, never on the instance: HybridSearcher is shared and search()
            # is async, so instance state would race between concurrent queries.
            # Recorded for every object that HAS a best passage, without asking
            # whether the whole-object vector outscored it.
            #
            # The comparison used to gate this, and it decided what the reader sees.
            # Two things were wrong with it. `chunk_scores` is the corroborated
            # aggregate — algebraically at most `best` and as low as 0.8333 * best —
            # so a passage could genuinely beat the document vector and still lose its
            # provenance. And `doc_scores` comes from a vector built on the FIRST
            # 20 000 characters (`_DOC_VECTOR_MAX_CHARS`): on this archive 44 of 342
            # objects are longer than that, the vector covering a median of 35% of the
            # text and as little as 2.6% of the longest.
            #
            # What followed was deterministic: no provenance, no `_embedding_chunk`,
            # no span lookup, `_matched_region` hands back the entire body, and
            # `best_snippet` over a body with none of the query's words returns its
            # first 520 characters. The model and the verifier were shown the header
            # of a document that had matched somewhere in the middle.
            #
            # The best passage is the best passage whichever vector ranked higher, so
            # the excerpt uses it unconditionally. Which signal actually carried the
            # retrieval is a separate question, and it is answered separately —
            # `embedding_source` in the trace below.
            meta["chunk_provenance"] = {
                document_id: provenance[document_id] for document_id in combined if document_id in provenance
            }
            meta["chunk_carried"] = {
                document_id
                for document_id in combined
                if document_id in provenance
                and chunk_scores.get(document_id, -1.0) >= doc_scores.get(document_id, -1.0)
            }
        return {document_id: score for document_id, score in combined.items() if document_id in candidate_map}

    async def _dense_recall_pool(
        self,
        query_vector: list[float],
        candidate_map: dict[str, dict[str, Any]],
    ) -> dict[str, float]:
        """Fallback: embed and score the current pool when no vectors are indexed.

        Bounded twice, because neither bound the indexer applies reached here. Every
        candidate's FULL body went into a single request — title, summary, content,
        tags, kind, untruncated, for up to `pool_max` objects — so the fallback that
        exists to keep search working before the index is built was the one request
        most likely to time out or be rejected outright. A 400-object pool of
        ordinary articles is several megabytes in one POST.
        """
        assert self.embeddings is not None
        candidates = list(candidate_map.values())
        if not candidates:
            return {}
        texts = [self._search_text(item)[:_POOL_TEXT_MAX_CHARS] for item in candidates]
        vectors: list[list[float]] = []
        start = 0
        while start < len(texts):
            end, volume = start, 0
            while end < len(texts) and (end == start or volume + len(texts[end]) <= _POOL_REQUEST_MAX_CHARS):
                volume += len(texts[end])
                end += 1
            returned = await self.embeddings.embed(texts[start:end])
            if not returned or len(returned) != end - start:
                # All-or-nothing, like the indexer: a partially embedded pool would
                # score some candidates densely and others not at all, which reads as
                # a ranking decision rather than a failed request.
                return {}
            vectors.extend(returned)
            start = end
        if len(vectors) != len(candidates):
            return {}
        return {
            item["id"]: dense_cosine(query_vector, vectors[index]) for index, item in enumerate(candidates)
        }

    def _field_scores(
        self,
        item: dict[str, Any],
        query: str,
        entity_names: list[str],
        *,
        query_vector: dict[str, float] | None = None,
    ) -> dict[str, float]:
        # The QUERY vector does not depend on the candidate, and this runs once per
        # candidate: on a 400-object pool that was 400 rebuilds of the same thing.
        vector = lexical_vector(query) if query_vector is None else query_vector
        query_folded = query.casefold()
        title_vector, summary_vector, tags_vector = self._cached_field_vectors(item)
        entities = " ".join(entity_names)
        exact_phrase = (
            1.0 if len(query_folded) >= 3 and query_folded in self._search_text(item).casefold() else 0.0
        )
        return {
            "title": sparse_cosine(vector, title_vector),
            "summary": sparse_cosine(vector, summary_vector),
            "tags": sparse_cosine(vector, tags_vector),
            "entities": sparse_cosine(vector, lexical_vector(entities)),
            "exact_phrase": exact_phrase,
        }

    def _feedback_scores(self, user_id: str, document_ids: list[str]) -> dict[str, float]:
        if not document_ids:
            return {}
        output: dict[str, float] = {}
        for start in range(0, len(document_ids), 400):
            chunk = document_ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            # The only interpolated fragment is a bounded sequence of ``?`` placeholders.
            query = f"""SELECT target_id, AVG(score) AS score FROM feedback_state
                    WHERE user_id=? AND target_id IN ({placeholders})
                      AND feedback_type IN ('search_quality', 'answer_usefulness')
                    GROUP BY target_id"""  # nosec B608
            rows = self.storage.execute(query, (user_id, *chunk)).fetchall()
            output.update({row["target_id"]: float(row["score"] or 0.0) for row in rows})
        return output
