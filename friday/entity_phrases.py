"""Candidates for inverted entity-name lookup in free text.

Shared by mention matching, inbox suggestions and the mention-backfill worker so
all three generate the same multi-word phrases. Without multi-word n-grams a
name like «Ядрица Омега Ультра» would only be found by walking the whole graph.
"""

from __future__ import annotations

import re

# Contiguous token spans from text become lookup keys. Entity names are multi-word
# ("Ядрица Омега Ультра", "Проект Альфа") and the old path found them by walking
# every graph node; the inverted path must still generate those phrases or the
# match is silently lost. 12 tokens covers long titles without exploding on a
# page of prose (n tokens → ≤ 12n candidates before de-dup).
_PHRASE_TOKEN_RE = re.compile(r"(?u)[\w.+#/-]+")
_MAX_MENTION_PHRASE_TOKENS = 12


def mention_phrase_candidates(
    text: str,
    *,
    max_tokens: int = _MAX_MENTION_PHRASE_TOKENS,
) -> list[str]:
    """Contiguous token n-grams from ``text`` used as entity-name lookup keys.

    Spans stop at non-whitespace gaps so ``Иванов, Пётр`` yields two names, not
    one. Identifiers with dots/dashes stay a single token (``PK-04-04``, ``GPT-4``).
    """
    if not text:
        return []
    # The class includes `.` so identifiers like BRK.A stay one token, but a
    # sentence-final period must not stick to the last word — otherwise
    # «Ультра.» never equals the stored name «Ультра».
    raw_matches = list(_PHRASE_TOKEN_RE.finditer(text))
    tokens: list[tuple[int, int, str]] = []
    for match in raw_matches:
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
        return []
    ceiling = max(1, min(int(max_tokens), 32))
    phrases: list[str] = []
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
            phrases.append(phrase)
    return phrases
