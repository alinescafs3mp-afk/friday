"""Pure, record-bounded projection for focused source retrieval.

The legacy ``source_search`` path grew careful rules for binding a requested
field to the same person/record as the query anchor.  This module carries those
rules without importing storage or either runtime so archive retrieval can use
the same proof before it mints an exact text-span locator.

Every returned excerpt is an exact slice of the supplied body.  In particular,
the projector never synthesizes a non-contiguous ``header + distant row``
excerpt: a table header is included only when it is directly adjacent to the
matching record.  That closed-span rule is intentionally stricter than the
legacy presentation helper, which may project selected cells from a distant
header and row after candidate authorization.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from collections import Counter
from dataclasses import FrozenInstanceError, dataclass
from enum import StrEnum
from typing import Final, NoReturn, SupportsIndex

_TRAILING_TOKEN_PUNCTUATION: Final = ".-"
MAX_SOURCE_FOCUS_ANCHOR_TERMS: Final = 24
MAX_SOURCE_FOCUS_BODY_CHARS: Final = 1_048_576
MAX_SOURCE_FOCUS_LINES: Final = 4_096
MAX_SOURCE_FOCUS_TOKENS: Final = 32_768
_SIMPLE_SURNAME: Final = re.compile(r"[а-я]{4,}(?:ов|ев|ин|ын)$")
_ADJECTIVE_SURNAME: Final = re.compile(r"[а-я]{4,}(?:ск|цк)(?:ий)?$")
_SIMPLE_SURNAME_ENDINGS: Final = frozenset({"", "а", "у", "е", "ы", "и", "ым", "ом", "ой"})
_ADJECTIVE_SURNAME_ENDINGS: Final = frozenset(
    {"ий", "ого", "ому", "им", "ом", "ая", "ой", "ую", "ие", "их", "ими"}
)
_CLOSED_FOCUS_FORMS: Final[dict[str, frozenset[str]]] = {
    "анкет": frozenset(
        {
            "анкета",
            "анкеты",
            "анкете",
            "анкету",
            "анкетой",
            "анкет",
            "анкетам",
            "анкетах",
        }
    ),
    "должност": frozenset(
        {
            "должность",
            "должности",
            "должностью",
            "должностей",
            "должностям",
            "должностях",
        }
    ),
    "позици": frozenset(
        {
            "позиция",
            "позиции",
            "позицию",
            "позицией",
            "позиций",
            "позициям",
            "позициях",
        }
    ),
    "рол": frozenset({"роль", "роли", "ролью", "ролей", "ролям", "ролями", "ролях"}),
    "код": frozenset(
        {
            "код",
            "кода",
            "коду",
            "кодом",
            "коде",
            "коды",
            "кодов",
            "кодам",
            "кодах",
        }
    ),
    "значени": frozenset(
        {
            "значение",
            "значения",
            "значению",
            "значением",
            "значений",
            "значениям",
            "значениях",
        }
    ),
    "строк": frozenset(
        {
            "строка",
            "строки",
            "строку",
            "строкой",
            "строк",
            "строкам",
            "строках",
        }
    ),
    "узл": frozenset(
        {
            "узел",
            "узла",
            "узлу",
            "узлом",
            "узле",
            "узлы",
            "узлов",
            "узлам",
            "узлах",
        }
    ),
}
_FOCUS_FORM_TO_FAMILY: Final = {
    form: family for family, forms in _CLOSED_FOCUS_FORMS.items() for form in (*forms, family)
}
_TABLE_HEADER_SUBJECTS: Final = frozenset(
    {
        "фамилия",
        "фио",
        "имя",
        "сотрудник",
        "работник",
        "person",
        "employee",
        "name",
        "surname",
    }
)
_ENGLISH_FOCUS_FORMS: Final = frozenset(
    {"endpoint", "node", "position", "role", "code", "value", "line", "title", "surname"}
)


class SourceFocusMatchKind(StrEnum):
    """Closed strength of one exact anchor-bound source passage."""

    FULL = "full"
    ANCHOR_CONTEXT = "anchor_context"


class SourceFocusProjection:
    """An immutable, non-serializable exact slice plus its focus proof."""

    __slots__ = (
        "context_count",
        "end",
        "excerpt",
        "focus_match_kind",
        "matched_focus_count",
        "start",
    )

    start: int
    end: int
    excerpt: str
    matched_focus_count: int
    context_count: int
    focus_match_kind: SourceFocusMatchKind

    def __init__(
        self,
        start: int,
        end: int,
        excerpt: str,
        matched_focus_count: int,
        context_count: int,
        focus_match_kind: SourceFocusMatchKind,
    ) -> None:
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
        ):
            raise ValueError("source-focus projection requires a non-empty absolute span")
        if not isinstance(excerpt, str) or not excerpt:
            raise ValueError("source-focus projection requires an excerpt")
        if end - start != len(excerpt):
            raise ValueError("source-focus projection span must match its exact excerpt length")
        if (
            isinstance(matched_focus_count, bool)
            or not isinstance(matched_focus_count, int)
            or matched_focus_count < 0
            or isinstance(context_count, bool)
            or not isinstance(context_count, int)
            or context_count < 0
        ):
            raise ValueError("source-focus projection counters must be non-negative integers")
        if type(focus_match_kind) is not SourceFocusMatchKind:
            raise ValueError("source-focus projection requires a closed match kind")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "excerpt", excerpt)
        object.__setattr__(self, "matched_focus_count", matched_focus_count)
        object.__setattr__(self, "context_count", context_count)
        object.__setattr__(self, "focus_match_kind", focus_match_kind)

    def __setattr__(self, name: str, value: object) -> None:
        del value
        raise FrozenInstanceError(f"cannot assign to field {name!r}")

    def __repr__(self) -> str:
        return "SourceFocusProjection(private_excerpt=True)"

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("source-focus projection is process-private")

    @property
    def focus_terms_matched(self) -> int:
        """Legacy-compatible spelling used by the source-search carrier."""

        return self.matched_focus_count

    @property
    def anchor_context_terms(self) -> int:
        """Legacy-compatible spelling used by the source-search carrier."""

        return self.context_count


@dataclass(frozen=True, slots=True)
class _Line:
    start: int
    end: int
    text: str
    tokens: tuple[tuple[int, int, str, str], ...]


@dataclass(frozen=True, slots=True)
class _ScoredPassage:
    start: int
    end: int
    excerpt: str
    matched_focus_count: int
    context_vocabulary: frozenset[str]


def _normalized_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("ё", "е").strip()
    return normalized if any(character.isalnum() for character in normalized) else ""


def _semantic_token_spans(value: str) -> tuple[tuple[int, int, str, str], ...]:
    """Keep substantive one-character terms while omitting linguistic stopwords."""

    from friday.retrieval import _STOPWORDS

    return tuple(
        item
        for item in _token_spans(value, minimum_length=1)
        if not (
            len(item[2]) == 1
            and item[2] in _STOPWORDS
            and not (
                (canonical_original := unicodedata.normalize("NFKC", item[3])).isascii()
                and canonical_original.isupper()
            )
            and not any(character.isdigit() for character in item[3])
        )
    )


def _terms(value: str, *, limit: int) -> tuple[str, ...] | None:
    """Return every unique proof term, or fail closed above the proof budget."""

    unique = list(
        dict.fromkeys(
            normalized for _start, _end, normalized, _original in _semantic_token_spans(value) if normalized
        )
    )
    return tuple(unique) if len(unique) <= limit else None


def _token_character(character: str, *, started: bool) -> bool:
    normalized = unicodedata.normalize("NFKC", character)
    if any(item.isalnum() for item in normalized):
        return True
    return bool(started and (unicodedata.category(character).startswith("M") or character in "._+#-"))


def _token_spans(
    value: str,
    *,
    offset: int = 0,
    maximum: int | None = None,
    minimum_length: int = 2,
) -> tuple[tuple[int, int, str, str], ...]:
    """Return NFKC-aware all-script tokens with exact original offsets."""

    tokens: list[tuple[int, int, str, str]] = []
    start: int | None = None
    for index in range(len(value) + 1):
        character = value[index] if index < len(value) else ""
        if character and _token_character(character, started=start is not None):
            if start is None:
                start = index
            continue
        if start is None:
            continue
        original = value[start:index].rstrip(_TRAILING_TOKEN_PUNCTUATION)
        normalized = _normalized_token(original)
        if len(original) >= minimum_length and normalized:
            tokens.append((offset + start, offset + start + len(original), normalized, original))
            if maximum is not None and len(tokens) >= maximum:
                return tuple(tokens)
        start = None
    return tuple(tokens)


def _unicode_value_tokens(value: str) -> tuple[tuple[str, str], ...]:
    """Tokenize every Unicode letter/number for the adjacent-value guard."""

    return tuple(
        (normalized, original) for _start, _end, normalized, original in _token_spans(value, minimum_length=1)
    )


def source_focus_fts_tokens(value: str) -> tuple[str, ...]:
    """Return bounded original spellings for the source-focus FTS seam."""

    from friday.retrieval import _STOPWORDS

    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _start, _end, normalized, original in _semantic_token_spans(value):
        if normalized not in seen:
            seen.add(normalized)
            unique.append((normalized, original))
    if len(unique) > MAX_SOURCE_FOCUS_ANCHOR_TERMS:
        chosen = [item for item in unique if item[0] not in _STOPWORDS][:MAX_SOURCE_FOCUS_ANCHOR_TERMS]
        if len(chosen) < MAX_SOURCE_FOCUS_ANCHOR_TERMS:
            taken = {item[0] for item in chosen}
            chosen += [item for item in unique if item[0] not in taken][
                : MAX_SOURCE_FOCUS_ANCHOR_TERMS - len(chosen)
            ]
    else:
        chosen = unique
    return tuple(original for _normalized, original in chosen)


def _anchor_matches_token(term: str, token: str) -> bool:
    """Closed token-aware match for a literal/id or Russian surname case."""

    anchor = _normalized_token(term)
    candidate = _normalized_token(token)
    if not anchor or not candidate:
        return False
    if candidate == anchor:
        return True
    anchor_surnames = _simple_surname_bases(anchor)
    candidate_surnames = _simple_surname_bases(candidate)
    anchor_adjectives = _adjective_surname_stems(anchor)
    candidate_adjectives = _adjective_surname_stems(candidate)
    if anchor_surnames or candidate_surnames or anchor_adjectives or candidate_adjectives:
        return bool(anchor_surnames & candidate_surnames or anchor_adjectives & candidate_adjectives)
    anchor_family = _FOCUS_FORM_TO_FAMILY.get(anchor)
    candidate_family = _FOCUS_FORM_TO_FAMILY.get(candidate)
    if anchor_family is not None or candidate_family is not None:
        return anchor_family is not None and anchor_family == candidate_family
    anchor_english = next(
        (base for base in _ENGLISH_FOCUS_FORMS if anchor in {base, f"{base}s"}),
        None,
    )
    candidate_english = next(
        (base for base in _ENGLISH_FOCUS_FORMS if candidate in {base, f"{base}s"}),
        None,
    )
    return anchor_english is not None and anchor_english == candidate_english


def _simple_surname_bases(value: str) -> frozenset[str]:
    return frozenset(
        base
        for ending in _SIMPLE_SURNAME_ENDINGS
        if (not ending or value.endswith(ending))
        and (base := value[: len(value) - len(ending)] if ending else value)
        and _SIMPLE_SURNAME.fullmatch(base)
    )


def _adjective_surname_stems(value: str) -> frozenset[str]:
    return frozenset(
        stem
        for ending in _ADJECTIVE_SURNAME_ENDINGS
        if (not ending or value.endswith(ending))
        and (stem := value[: len(value) - len(ending)] if ending else value)
        and _ADJECTIVE_SURNAME.fullmatch(stem)
    )


def _focus_matches_token(term: str, token: str, *, query_terms: tuple[str, ...]) -> bool:
    normalized = _normalized_token(term)
    candidate = _normalized_token(token)
    if normalized in query_terms:
        return _anchor_matches_token(normalized, candidate)
    family = _FOCUS_FORM_TO_FAMILY.get(normalized)
    closed = _CLOSED_FOCUS_FORMS.get(family) if family is not None else None
    if closed is not None:
        return candidate in closed
    if normalized in _ENGLISH_FOCUS_FORMS:
        return candidate in {normalized, f"{normalized}s"}
    return candidate == normalized


def _substantive_context_token(token: str, original: str) -> bool:
    return bool(
        len(token) >= 3
        or (not original.isascii() and any(character.isalnum() for character in original))
        or any(character.isdigit() for character in token)
        or (len(token) <= 2 and original.isascii() and original.isalpha() and original.isupper())
    )


def _closed_field_value_line(
    line: _Line,
    *,
    non_anchor_focus: tuple[str, ...],
    query_terms: tuple[str, ...],
) -> bool:
    """Prove one exact ``Field: arbitrary value`` line."""

    if line.text.count(":") != 1:
        return False
    colon = line.text.index(":")
    label = line.text[:colon]
    label_tokens = _token_spans(label, minimum_length=1)
    if not label_tokens or label_tokens[0][0] != 0 or len(label_tokens) > len(non_anchor_focus):
        return False

    cursor = 0
    for index, (start, end, _token, _original) in enumerate(label_tokens):
        gap = line.text[cursor:start]
        if (index == 0 and gap) or (index > 0 and (not gap or not gap.isspace())):
            return False
        cursor = end
    trailing_label_gap = line.text[cursor:colon]
    if trailing_label_gap and not trailing_label_gap.isspace():
        return False

    # Each label token must be justified by a different caller-owned focus
    # term.  This admits closed multi-word labels (``job title`` / ``номер
    # телефона``), while an arbitrary prefix or a second field fails closed.
    term_matches = tuple(
        tuple(
            index
            for index, term in enumerate(non_anchor_focus)
            if _focus_matches_token(term, token, query_terms=query_terms)
        )
        for _start, _end, token, _original in label_tokens
    )
    if any(not matches for matches in term_matches):
        return False
    assigned: dict[int, int] = {}

    def bind_label_token(token_index: int, visited: set[int]) -> bool:
        for term_index in term_matches[token_index]:
            if term_index in visited:
                continue
            visited.add(term_index)
            previous = assigned.get(term_index)
            if previous is None or bind_label_token(previous, visited):
                assigned[term_index] = token_index
                return True
        return False

    if not all(bind_label_token(index, set()) for index in range(len(label_tokens))):
        return False

    value_tokens = _unicode_value_tokens(line.text[colon + 1 :])
    if not value_tokens:
        return False
    line_tokens = _unicode_value_tokens(line.text)
    return not any(
        _anchor_matches_token(term, token) for token, _original in line_tokens for term in query_terms
    )


def _closed_two_line_record(
    lines: tuple[_Line, ...],
    *,
    anchor_index: int,
    field_index: int,
    non_anchor_focus: tuple[str, ...],
    query_terms: tuple[str, ...],
) -> bool:
    """Prove a sole anchor + explicit field pair in one blank-delimited block."""

    if abs(anchor_index - field_index) != 1:
        return False
    block_start = min(anchor_index, field_index)
    block_end = max(anchor_index, field_index)
    if (block_start > 0 and lines[block_start - 1].text) or (
        block_end + 1 < len(lines) and lines[block_end + 1].text
    ):
        return False
    anchor_line = lines[anchor_index]
    anchor_tokens = _unicode_value_tokens(anchor_line.text)
    if len(anchor_tokens) != len(query_terms):
        return False
    term_matches = tuple(
        tuple(term_index for term_index, term in enumerate(query_terms) if _anchor_matches_token(term, token))
        for token, _original in anchor_tokens
    )
    if any(not matches for matches in term_matches):
        return False
    assigned: dict[int, int] = {}

    def bind_anchor_token(token_index: int, visited: set[int]) -> bool:
        for term_index in term_matches[token_index]:
            if term_index in visited:
                continue
            visited.add(term_index)
            previous = assigned.get(term_index)
            if previous is None or bind_anchor_token(previous, visited):
                assigned[term_index] = token_index
                return True
        return False

    if not all(bind_anchor_token(index, set()) for index in range(len(anchor_tokens))):
        return False
    if any(
        _focus_matches_token(term, token, query_terms=query_terms)
        for token, _original in anchor_tokens
        for term in non_anchor_focus
    ):
        return False
    return _closed_field_value_line(
        lines[field_index],
        non_anchor_focus=non_anchor_focus,
        query_terms=query_terms,
    )


def _table_header_candidate(line: str) -> bool:
    candidate = str(line or "").strip()
    if (
        not candidate
        or " | " not in candidate
        or ":" in candidate
        or any(char.isdigit() for char in candidate)
    ):
        return False
    first_cell = candidate.split(" | ", 1)[0]
    first_tokens = {token for _start, _end, token, _original in _token_spans(first_cell)}
    return bool(first_tokens & _TABLE_HEADER_SUBJECTS)


def _sparse_section_heading(line: str) -> bool:
    # Empty spreadsheet cells render as `` |  | ``.  Split the delimiter,
    # rather than its surrounding padding, exactly as the legacy projector does.
    cells = [cell.strip() for cell in re.split(r"\s*\|\s*", line)]
    return len(cells) >= 3 and sum(bool(cell) for cell in cells) == 1


def _trimmed_span(body: str, start: int, end: int) -> tuple[int, int]:
    while start < end and body[start].isspace():
        start += 1
    while end > start and body[end - 1].isspace():
        end -= 1
    return start, end


def _line_rows(body: str) -> tuple[_Line, ...] | None:
    if len(body) > MAX_SOURCE_FOCUS_BODY_CHARS:
        return None
    line_breaks = 0
    for character in body:
        if character in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029":
            line_breaks += 1
            if line_breaks >= MAX_SOURCE_FOCUS_LINES:
                return None
    rows: list[_Line] = []
    token_count = 0
    cursor = 0
    for raw_line in body.splitlines(keepends=True) or [body]:
        content_end = cursor + len(raw_line.rstrip("\r\n"))
        start, end = _trimmed_span(body, cursor, content_end)
        text = body[start:end]
        tokens = _token_spans(
            text,
            offset=start,
            maximum=MAX_SOURCE_FOCUS_TOKENS - token_count + 1,
            minimum_length=1,
        )
        token_count += len(tokens)
        if token_count > MAX_SOURCE_FOCUS_TOKENS:
            return None
        rows.append(_Line(start=start, end=end, text=text, tokens=tokens))
        cursor += len(raw_line)
    return tuple(rows)


def _window_around_anchor(line: _Line, anchor_start: int, *, max_chars: int) -> tuple[int, int]:
    relative = anchor_start - line.start
    start = max(0, relative - max(24, max_chars // 4))
    end = min(len(line.text), start + max_chars)
    start = max(0, end - max_chars)
    absolute_start = line.start + start
    absolute_end = line.start + end
    for token_start, token_end, _normalized, _original in line.tokens:
        if token_start < absolute_start < token_end:
            start = token_end - line.start
        if token_start < absolute_end < token_end:
            end = token_start - line.start
        if token_start >= absolute_end:
            break
    return _trimmed_span(line.text, start, end)


def project_source_focus(
    body: str,
    query: str,
    focus: str,
    *,
    max_chars: int,
) -> SourceFocusProjection | None:
    """Select one exact anchor-bound source passage.

    ``query`` owns admission: a focus predicate can rank an occurrence but can
    never admit a passage without the query anchor.  Normal extracted lines and
    table rows are closed records.  A requested field may join only the directly
    adjacent line only when it forms an exact two-line blank-delimited record;
    a sparse spreadsheet section may bind exactly its first following data row.
    A richer focus also requires a substantive value/context token, so
    ``Иванов / Должность:`` and an ambiguous multi-line neighbour fail closed.
    """

    if not isinstance(body, str) or not isinstance(query, str) or not isinstance(focus, str):
        raise TypeError("source-focus body, query and focus must be text")
    if isinstance(max_chars, bool) or not isinstance(max_chars, int):
        raise TypeError("source-focus max_chars must be an integer")
    if max_chars <= 0:
        raise ValueError("source-focus max_chars must be positive")
    if not body:
        return None

    query_terms = _terms(query, limit=MAX_SOURCE_FOCUS_ANCHOR_TERMS)
    if not query_terms:
        return None
    focus_terms = _terms(focus or query, limit=MAX_SOURCE_FOCUS_ANCHOR_TERMS)
    if not focus_terms:
        return None
    non_anchor_focus = tuple(term for term in focus_terms if term not in query_terms)
    lines = _line_rows(body)
    if not lines:
        return None

    vocabulary_counts: Counter[str] = Counter(
        token for line in lines for _start, _end, token, _original in line.tokens if token
    )
    best: tuple[int, int, float, int, int, _ScoredPassage] | None = None

    def passage_tokens(excerpt: str) -> tuple[tuple[str, str], ...]:
        return _unicode_value_tokens(excerpt)

    def score(excerpt: str) -> tuple[int, frozenset[str]]:
        tokens = passage_tokens(excerpt)
        matched = sum(
            any(_focus_matches_token(term, token, query_terms=query_terms) for token, _original in tokens)
            for term in focus_terms
        )
        context = frozenset(
            token
            for token, original in tokens
            if _substantive_context_token(token, original)
            and not any(_anchor_matches_token(term, token) for term in query_terms)
            and token not in _TABLE_HEADER_SUBJECTS
            and not any(
                _focus_matches_token(term, token, query_terms=query_terms) for term in non_anchor_focus
            )
        )
        return matched, context

    def consider(start: int, end: int, *, position: int) -> None:
        nonlocal best
        start, end = _trimmed_span(body, start, end)
        if start >= end or end - start > max_chars:
            return
        excerpt = body[start:end]
        if "\r" in excerpt or any(
            unicodedata.category(character).startswith("C") and character != "\n" for character in excerpt
        ):
            return
        tokens = passage_tokens(excerpt)
        if not all(
            any(_anchor_matches_token(term, token) for token, _original in tokens) for term in query_terms
        ):
            return
        matched, context = score(excerpt)
        full_focus = bool(focus_terms) and matched == len(focus_terms)
        rarity = sum(1.0 / max(1, vocabulary_counts[token]) for token in context)
        passage = _ScoredPassage(start, end, excerpt, matched, context)
        candidate = (int(full_focus), matched, rarity, len(context), -position, passage)
        if best is None or candidate[:5] > best[:5]:
            best = candidate

    active_table_header: int | None = None
    previous_was_table = False
    for line_index, line in enumerate(lines):
        is_table = " | " in line.text
        if not is_table:
            active_table_header = None
            previous_was_table = False
            continue
        if not previous_was_table:
            active_table_header = line_index if _table_header_candidate(line.text) else None
        previous_was_table = True

        anchor_positions = [
            start
            for start, _end, token, _original in line.tokens
            if any(_anchor_matches_token(term, token) for term in query_terms)
        ]
        if not anchor_positions:
            continue

        if _sparse_section_heading(line.text):
            # A section label may scope exactly its first following record, but
            # never a group and never anything beyond another section/boundary.
            for candidate in lines[line_index + 1 : line_index + 13]:
                if " | " not in candidate.text or _sparse_section_heading(candidate.text):
                    break
                consider(line.start, candidate.end, position=line.start)
                break

        # The row itself is always the primary exact record.  Reusing a header
        # from much earlier would require a non-contiguous locator and could
        # expose neighbouring people, so only the directly adjacent header is
        # eligible for an exact combined passage.
        if line.end - line.start <= max_chars:
            consider(line.start, line.end, position=line.start)
        else:
            for anchor_start in _selected_anchor_positions(line, anchor_positions, focus_terms, query_terms):
                rel_start, rel_end = _window_around_anchor(line, anchor_start, max_chars=max_chars)
                consider(line.start + rel_start, line.start + rel_end, position=anchor_start)
        if active_table_header == line_index - 1:
            header = lines[active_table_header]
            consider(header.start, line.end, position=line.start)

    for line_index, line in enumerate(lines):
        if " | " in line.text:
            continue
        anchor_positions = [
            start
            for start, _end, token, _original in line.tokens
            if any(_anchor_matches_token(term, token) for term in query_terms)
        ]
        if not anchor_positions:
            continue

        passage_start = line.start
        passage_end = line.end
        if line_index > 0:
            previous = lines[line_index - 1]
            if previous.text and _closed_two_line_record(
                lines,
                anchor_index=line_index,
                field_index=line_index - 1,
                non_anchor_focus=non_anchor_focus,
                query_terms=query_terms,
            ):
                passage_start = previous.start
        if line_index + 1 < len(lines):
            following = lines[line_index + 1]
            if following.text and _closed_two_line_record(
                lines,
                anchor_index=line_index,
                field_index=line_index + 1,
                non_anchor_focus=non_anchor_focus,
                query_terms=query_terms,
            ):
                passage_end = following.end

        if passage_end - passage_start <= max_chars:
            consider(passage_start, passage_end, position=line.start)
        elif line.end - line.start <= max_chars:
            consider(line.start, line.end, position=line.start)
        else:
            for anchor_start in _selected_anchor_positions(line, anchor_positions, focus_terms, query_terms):
                rel_start, rel_end = _window_around_anchor(line, anchor_start, max_chars=max_chars)
                consider(line.start + rel_start, line.start + rel_end, position=anchor_start)

    if best is None:
        return None
    passage = best[5]
    # A richer focus without a substantive value is not evidence.  This is the
    # projector-level version of source_search's eligibility gate and closes
    # predicate-only / far-section joins before a locator is minted.
    if non_anchor_focus and not passage.context_vocabulary:
        return None
    kind = (
        SourceFocusMatchKind.FULL
        if passage.matched_focus_count == len(focus_terms)
        else SourceFocusMatchKind.ANCHOR_CONTEXT
    )
    return SourceFocusProjection(
        start=passage.start,
        end=passage.end,
        excerpt=passage.excerpt,
        matched_focus_count=passage.matched_focus_count,
        context_count=len(passage.context_vocabulary),
        focus_match_kind=kind,
    )


def _selected_anchor_positions(
    line: _Line,
    anchor_positions: list[int],
    focus_terms: tuple[str, ...],
    query_terms: tuple[str, ...],
) -> tuple[int, ...]:
    """Bound long-line work independently of repeated-anchor cardinality."""

    selected = {anchor_positions[0], anchor_positions[-1]}
    if len(anchor_positions) > 2:
        stride = max(1, len(anchor_positions) // 30)
        selected.update(anchor_positions[::stride][:32])

    non_anchor_focus = tuple(term for term in focus_terms if term not in query_terms)
    first_focus: dict[str, int] = {}
    last_focus: dict[str, int] = {}
    for start, _end, token, _original in line.tokens:
        for term in non_anchor_focus:
            if _focus_matches_token(term, token, query_terms=query_terms):
                first_focus.setdefault(term, start)
                last_focus[term] = start

    # At most two occurrences per bounded focus term affect candidate windows.
    # Locate their nearest anchors in O(log A), rather than scanning every
    # anchor for every repeated focus token on a large extracted line.
    for start in {*first_focus.values(), *last_focus.values()}:
        index = bisect_left(anchor_positions, start)
        neighbours = anchor_positions[max(0, index - 1) : min(len(anchor_positions), index + 1)]
        if neighbours:
            selected.add(min(neighbours, key=lambda value: (abs(value - start), value)))
    return tuple(sorted(selected))


__all__ = [
    "MAX_SOURCE_FOCUS_ANCHOR_TERMS",
    "MAX_SOURCE_FOCUS_BODY_CHARS",
    "MAX_SOURCE_FOCUS_LINES",
    "MAX_SOURCE_FOCUS_TOKENS",
    "SourceFocusMatchKind",
    "SourceFocusProjection",
    "project_source_focus",
    "source_focus_fts_tokens",
]
