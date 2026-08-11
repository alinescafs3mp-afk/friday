"""Ephemeral archive-password parsing and bounded transport variants.

Passwords are credentials, not natural-language text.  This module therefore
never strips, case-folds, logs, hashes, or persists a supplied value.  The only
alternatives it produces are a small closed set for representation artefacts:
matching presentation quotes and the two standard Unicode normalization forms.
"""

from __future__ import annotations

import re
import unicodedata

_MAX_ARCHIVE_PASSWORD_BYTES = 4096
_MAX_ARCHIVE_PASSWORD_CANDIDATES = 6
_ARCHIVE_PASSWORD_DIRECTIVE_RE = re.compile(
    r"^[ \t]*(?:пароль|password)[ \t]*(?::|=|[—-])[ \t]*(.*)$",
    re.IGNORECASE,
)
_ARCHIVE_PASSWORD_INLINE_RE = re.compile(
    r"^(?P<prefix>.*?)(?:[,;][ \t]*)?\b(?:пароль|password)[ \t]*"
    r"(?::|=|[—-])[ \t]*(?P<secret>.+)$",
    re.IGNORECASE,
)
_PRESENTATION_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "«": "»",
    "“": "”",
    "‘": "’",
    "„": "“",
}


def bounded_archive_password(value: str | None) -> str | None:
    """Return one unchanged password if it is safe for the request-local pipe."""

    if not isinstance(value, str) or not value:
        return None
    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) > _MAX_ARCHIVE_PASSWORD_BYTES or "\x00" in value or "\n" in value or "\r" in value:
        return None
    return value


def _unwrap_presentation_quotes(value: str) -> str | None:
    if len(value) < 2 or _PRESENTATION_QUOTE_PAIRS.get(value[0]) != value[-1]:
        return None
    # Quotes are presentation syntax only at the outermost boundary.  Inner
    # whitespace is password data and must remain byte-for-byte intact.
    return value[1:-1]


def _directive_password(value: str) -> str:
    """Interpret an optional quote wrapper, never whitespace inside it."""

    if not value:
        return value
    closing = _PRESENTATION_QUOTE_PAIRS.get(value[0])
    if closing is None:
        return value
    # Whitespace following a closing presentation quote belongs to directive
    # syntax.  The same characters *inside* the quotes remain credential data.
    end = len(value) - 1
    while end > 0 and value[end] in " \t":
        end -= 1
    if value[end] != closing:
        return value
    return value[1:end]


def archive_password_candidates(value: str | None) -> tuple[str, ...]:
    """Return exact-first, bounded representation variants for extraction.

    This is deliberately not a password guesser.  At most six candidates can
    arise: the exact value, an optional matching-quote wrapper removed, and
    NFC/NFD spellings of those values.  Callers must reuse one extraction
    deadline and resource budget across every candidate.
    """

    exact = bounded_archive_password(value)
    if exact is None:
        return ()
    bases = [exact]
    unwrapped = _unwrap_presentation_quotes(exact)
    if unwrapped is not None:
        bases.append(unwrapped)

    candidates: list[str] = []
    for candidate in bases:
        safe = bounded_archive_password(candidate)
        if safe is not None and safe not in candidates:
            candidates.append(safe)
    for base in bases:
        for candidate in (unicodedata.normalize("NFC", base), unicodedata.normalize("NFD", base)):
            safe = bounded_archive_password(candidate)
            if safe is None or safe in candidates:
                continue
            candidates.append(safe)
            if len(candidates) >= _MAX_ARCHIVE_PASSWORD_CANDIDATES:
                return tuple(candidates)
    return tuple(candidates)


def strip_archive_password_directives(text: str) -> tuple[str, str | None]:
    """Remove one password directive while preserving its credential payload.

    Unquoted syntax consumes only whitespace belonging to the delimiter after
    ``пароль:``/``password:``; trailing characters are never trimmed.  Matching
    outer quotes are syntax and are removed while their inner whitespace is
    preserved.  A password made entirely of spaces therefore uses quoted form
    or the standalone pending-challenge message.
    """

    secret: str | None = None
    kept: list[str] = []
    for line in str(text or "").splitlines():
        match = _ARCHIVE_PASSWORD_DIRECTIVE_RE.fullmatch(line)
        if match is None:
            kept.append(line)
            continue
        candidate = match.group(1)
        if secret is None and candidate:
            secret = _directive_password(candidate)
    if secret is None and kept:
        match = _ARCHIVE_PASSWORD_INLINE_RE.fullmatch(kept[-1])
        if match is not None and match.group("secret"):
            candidate = match.group("secret")
            secret = _directive_password(candidate)
            prefix = match.group("prefix").rstrip(" ,;\t")
            if prefix:
                kept[-1] = prefix
            else:
                kept.pop()
    return "\n".join(kept).strip(), secret


__all__ = [
    "archive_password_candidates",
    "bounded_archive_password",
    "strip_archive_password_directives",
]
