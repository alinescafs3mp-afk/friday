"""Candidates for inverted entity-name lookup in free text.

Shared by mention matching, inbox suggestions and the mention-backfill worker so
all three generate the same multi-word phrases. Without multi-word n-grams a
name like «Ядрица Омега Ультра» would only be found by walking the whole graph.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping

# Contiguous token spans from text become lookup keys. Entity names are multi-word
# ("Ядрица Омега Ультра", "Проект Альфа") and the old path found them by walking
# every graph node; the inverted path must still generate those phrases or the
# match is silently lost. 12 tokens covers long titles without exploding on a
# page of prose (n tokens → ≤ 12n candidates before de-dup).
_PHRASE_TOKEN_RE = re.compile(r"(?u)[\w.+#/-]+")
_MAX_MENTION_PHRASE_TOKENS = 12
# A canonical entity card is at most 240 characters.  The densest accepted
# multi-token material is therefore 120 one-character tokens separated by one
# space.  Backfill opts into this ceiling so literal discovery covers the whole
# stored card contract without making every synchronous mention lookup generate
# ten times as many n-grams.
MAX_EXACT_MENTION_PHRASE_TOKENS = 120
MAX_EXACT_MENTION_PHRASE_CHARS = 240
# Entity search cards bound canonical names to 240 characters and alias JSON to
# 8 KiB.  A single lookup key larger than the latter cannot equal material the
# bounded reader is allowed to expose.  Keeping the same ceiling here also turns
# a malicious megabyte-long "word" into a sequence of cooperative skips rather
# than one uninterruptible regular-expression match.
_MAX_LOOKUP_PHRASE_CHARS = 8_192
_PHRASE_SCAN_CHARS = 8_192


def _cursor_integer(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise ValueError("cursor integer required")


def _is_phrase_character(character: str) -> bool:
    return character.isalnum() or character == "_" or character in ".+#/-"


def _next_phrase_token(
    text: str,
    offset: int,
    *,
    scan_end: int,
    max_chars: int = _MAX_LOOKUP_PHRASE_CHARS,
) -> tuple[tuple[int, int, str] | None, int, bool]:
    """Read at most one bounded token.

    The returned integer is always a safe forward cursor.  ``too_long`` means
    that the token cannot equal a bounded entity name/alias and may therefore be
    skipped without retaining any of its private characters in durable state.
    """

    size = len(text)
    position = max(0, min(int(offset), size))
    ceiling = max(position, min(int(scan_end), size))
    while position < ceiling and not _is_phrase_character(text[position]):
        position += 1
    if position >= ceiling:
        return None, position, False

    start = position
    token_ceiling = min(size, start + max_chars + 1)
    while position < token_ceiling and _is_phrase_character(text[position]):
        position += 1
    if position - start > max_chars:
        # The caller resumes *after* this bounded prefix in skip mode.  No
        # matchable token is lost: public aliases are at most 8 KiB and canonical
        # names are shorter still.
        return None, position, position < size and _is_phrase_character(text[position])

    value = text[start:position].rstrip(".,;:!?…")
    if not value:
        return None, position, False
    end = start + len(value)
    return (start, end, value), position, False


def _skip_oversized_token(text: str, offset: int) -> tuple[int, bool]:
    """Advance a bounded distance through an already-known oversized token."""

    size = len(text)
    position = max(0, min(int(offset), size))
    ceiling = min(size, position + _PHRASE_SCAN_CHARS)
    while position < ceiling and _is_phrase_character(text[position]):
        position += 1
    return position, position < size and _is_phrase_character(text[position])


def iter_mention_phrase_candidates(
    text: str,
    *,
    max_tokens: int = _MAX_MENTION_PHRASE_TOKENS,
) -> Iterator[str]:
    """Yield de-duplicated contiguous n-grams in their canonical lookup order.

    Spans stop at non-whitespace gaps so ``Иванов, Пётр`` yields two names, not
    one. Identifiers with dots/dashes stay a single token (``PK-04-04``, ``GPT-4``).
    """
    if not text:
        return
    # The class includes `.` so identifiers like BRK.A stay one token, but a
    # sentence-final period must not stick to the last word — otherwise
    # «Ультра.» never equals the stored name «Ультра».
    tokens: list[tuple[int, int, str]] = []
    for match in _PHRASE_TOKEN_RE.finditer(text):
        value = match.group(0).rstrip(".,;:!?…")
        if len(value) < 2:
            continue
        # start stays; end shrinks when trailing punctuation was stripped
        end = match.start() + len(value)
        # Safety: only accept when the prefix of the match is the cleaned token
        if text[match.start() : end] != value and not match.group(0).startswith(value):
            continue
        tokens.append((match.start(), end, value))
    if not tokens:
        return
    ceiling = max(1, min(int(max_tokens), MAX_EXACT_MENTION_PHRASE_TOKENS))
    seen: set[str] = set()
    count = len(tokens)
    for start_index in range(count):
        for length in range(1, ceiling + 1):
            end_index = start_index + length - 1
            if end_index >= count:
                break
            gap_ok = True
            for mid in range(start_index, end_index):
                _s, mid_end, _ = tokens[mid]
                next_start, _e, _ = tokens[mid + 1]
                gap = text[mid_end:next_start]
                if gap and not gap.isspace():
                    gap_ok = False
                    break
            if not gap_ok:
                break
            start, _, _ = tokens[start_index]
            _, end, _ = tokens[end_index]
            phrase = text[start:end]
            # Rebuild from cleaned tokens so a stripped trailing period does not
            # re-enter via the slice when only the last token lost punctuation.
            if length == 1:
                phrase = tokens[start_index][2]
            else:
                parts = [tokens[start_index][2]]
                for mid in range(start_index, end_index):
                    _s, mid_end, _ = tokens[mid]
                    next_start, _e, next_val = tokens[mid + 1]
                    parts.append(text[mid_end:next_start])
                    parts.append(next_val)
                phrase = "".join(parts)
            if len(phrase) < 2:
                continue
            key = phrase.casefold()
            if key in seen:
                continue
            seen.add(key)
            yield phrase


def mention_phrase_candidates(
    text: str,
    *,
    max_tokens: int = _MAX_MENTION_PHRASE_TOKENS,
) -> list[str]:
    """Contiguous token n-grams from ``text`` used as entity-name lookup keys."""
    return list(iter_mention_phrase_candidates(text, max_tokens=max_tokens))


def mention_phrase_candidate_page(
    text: str,
    *,
    cursor: Mapping[str, object] | None,
    limit: int = 64,
    max_tokens: int = _MAX_MENTION_PHRASE_TOKENS,
    max_chars: int = _MAX_LOOKUP_PHRASE_CHARS,
) -> tuple[list[str], dict[str, int], bool, bool]:
    """Return one cooperatively generated page and its opaque numeric cursor.

    A phrase-count offset is not a resumable cursor: reaching offset 80 000 by
    replaying the first 79 999 candidates on every tick is quadratic and can
    livelock forever under a wall-clock budget.  This cursor names the source
    character and the next n-gram length, so every invocation starts at new work.

    The state contains no phrase, token or span — only positions in the current
    document.  Repeated phrases may occur in different pages; lookup/linking is
    idempotent and this small amount of duplicate work buys a privacy-safe cursor
    without persisting a set derived from document text.
    """

    body = text or ""
    ceiling = max(1, min(int(max_tokens), MAX_EXACT_MENTION_PHRASE_TOKENS))
    phrase_char_ceiling = max(3, min(int(max_chars), _MAX_LOOKUP_PHRASE_CHARS))
    bounded = max(1, min(int(limit), 800))
    raw = dict(cursor or {})
    try:
        position = _cursor_integer(raw.get("char", 0))
        length = _cursor_integer(raw.get("length", 1))
        skipping = _cursor_integer(raw.get("skip", 0))
    except (TypeError, ValueError, AttributeError):
        return [], {"char": 0, "length": 1, "skip": 0}, False, False
    if not 0 <= position <= len(body) or not 1 <= length <= ceiling or skipping not in {0, 1}:
        return [], {"char": 0, "length": 1, "skip": 0}, False, False

    state = {"char": position, "length": length, "skip": skipping}
    page: list[str] = []
    seen: set[str] = set()
    scan_left = _PHRASE_SCAN_CHARS

    while len(page) < bounded and state["char"] < len(body) and scan_left > 0:
        if state["skip"]:
            before = state["char"]
            state["char"], still_skipping = _skip_oversized_token(body, before)
            state["skip"] = int(still_skipping)
            state["length"] = 1
            scan_left -= max(1, state["char"] - before)
            continue

        search_start = state["char"]
        search_end = min(len(body), search_start + scan_left)
        first, advanced, too_long = _next_phrase_token(
            body,
            search_start,
            scan_end=search_end,
            max_chars=phrase_char_ceiling,
        )
        scan_left -= max(1, advanced - search_start)
        if too_long:
            state.update(char=advanced, length=1, skip=1)
            continue
        if first is None:
            state.update(char=advanced, length=1, skip=0)
            continue

        first_start, first_end, first_value = first
        # A cursor in punctuation/whitespace canonically snaps to the token.  Once
        # there, `length` identifies the exact n-gram that comes next.
        state["char"] = first_start
        tokens = [(first_start, first_end, first_value)]
        next_search = advanced
        while len(tokens) < ceiling:
            if next_search - first_start >= phrase_char_ceiling:
                break
            following, following_advanced, following_too_long = _next_phrase_token(
                body,
                next_search,
                scan_end=min(
                    len(body),
                    first_start + phrase_char_ceiling,
                ),
                max_chars=phrase_char_ceiling,
            )
            if following_too_long or following is None:
                break
            if following[1] - first_start > phrase_char_ceiling:
                break
            gap = body[tokens[-1][1] : following[0]]
            if gap and not gap.isspace():
                break
            tokens.append(following)
            next_search = following_advanced

        available = len(tokens)
        if state["length"] <= available:
            if state["length"] == 1:
                phrase = first_value
            else:
                parts = [first_value]
                for index in range(state["length"] - 1):
                    parts.append(body[tokens[index][1] : tokens[index + 1][0]])
                    parts.append(tokens[index + 1][2])
                phrase = "".join(parts)
            state["length"] += 1
            if state["length"] > available:
                # All matchable n-grams for this start token are done.  The next
                # token is a stable source cursor; if none is currently visible,
                # advancing to the end of this bounded scan is still progress.
                next_char = tokens[1][0] if len(tokens) > 1 else max(first_end, advanced)
                state.update(char=next_char, length=1, skip=0)
            key = phrase.casefold()
            if key not in seen:
                seen.add(key)
                page.append(phrase)
            continue

        # A forged/stale length cannot declare a document complete.  Restart the
        # current token; storage will also discard the state when document version
        # changes.
        state["length"] = 1

    has_more = state["char"] < len(body)
    return page, state, has_more, True
