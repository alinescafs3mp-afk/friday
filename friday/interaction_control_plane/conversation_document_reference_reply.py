"""Closed scalar filename reply for a pending comparison question.

The returned name is only a lookup term.  It carries no file authority and is
never a filesystem path; the runtime must still resolve exactly one owned Raw
source under fresh authorization.
"""

from __future__ import annotations

import re
import unicodedata

_MAX_SURFACE_CHARS = 280
_MAX_SURFACE_UTF8_BYTES = 768
_MAX_FILENAME_CHARS = 260

_QUOTED = (
    ("«", "»"),
    ("‹", "›"),
    ("“", "”"),
    ("„", "“"),
    ('"', '"'),
    ("'", "'"),
)
_PREFIX_RE = re.compile(
    r"^(?:(?:это|вот|выбираю|укажу)\s+)?"
    r"(?:(?:с\s+)?(?:файл(?:ом)?|документ(?:ом)?|вложени(?:е|ем))\s+|"
    r"(?:(?:the|this|that)\s+)?(?:file|document|attachment)\s+)?",
    re.IGNORECASE,
)
_FORBIDDEN_RE = re.compile(
    r"(?:https?://|ftp://|file://)|"
    r"\b(?:и\s+(?:затем|потом)|затем|потом|then|and\s+then|"
    r"созда\w*|измен\w*|удал\w*|отправ\w*|сохран\w*|найд\w*|поищ\w*|"
    r"проверь\w*|интернет\w*|сайт\w*|"
    r"create\w*|edit\w*|delete\w*|remove\w*|send\w*|save\w*|search\w*|"
    r"find\w*|browse\w*|internet|website|ignore\w*|system\s+prompt|"
    r"игнорир\w*|системн\w*\s+промпт\w*)\b",
    re.IGNORECASE,
)
_BARE_FILENAME_RE = re.compile(
    r"(?=.{1,260}\Z)(?![. ])(?!.*[. ]\Z)(?!.*\.\.)"
    r"[^\x00-\x1f\x7f/\\<>:*?\"'`|]+\.[^\W_][\w+.-]{0,15}\Z",
    re.UNICODE,
)
_QUOTED_NAME_RE = re.compile(r"(?=.{1,260}\Z)(?![. ])(?!.*[. ]\Z)[^\x00-\x1f\x7f/\\]+\Z")
_MULTIPLE_REFERENCE_RE = re.compile(
    r"\b(?:(?:два|две|три|четыре|несколько)\s+(?:файл\w*|документ\w*|вложени\w*)|"
    r"(?:two|three|four|several|multiple)\s+(?:files?|documents?|attachments?))\b|"
    r"\S+\.[^\W_][\w+.-]{0,15}\s+(?:и|and)\s+\S+\.[^\W_][\w+.-]{0,15}",
    re.IGNORECASE,
)


def _canonical_surface(message: object) -> str | None:
    if type(message) is not str or not message or len(message) > _MAX_SURFACE_CHARS:
        return None
    if any(unicodedata.category(character).startswith("C") for character in message):
        return None
    try:
        if len(message.encode("utf-8", errors="strict")) > _MAX_SURFACE_UTF8_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    surface = " ".join(unicodedata.normalize("NFKC", message).split()).strip()
    if surface[-1:] in {".", "!", "?"} and not _BARE_FILENAME_RE.fullmatch(surface):
        surface = surface[:-1].rstrip()
    if (
        not surface
        or len(surface) > _MAX_SURFACE_CHARS
        or _FORBIDDEN_RE.search(surface)
        or _MULTIPLE_REFERENCE_RE.search(surface)
        or "/" in surface
        or "\\" in surface
        or "`" in surface
        or any(unicodedata.category(character).startswith("C") for character in surface)
    ):
        return None
    return surface


def _unwrap_one_quote_pair(value: str) -> str | None:
    matching = tuple((opening, closing) for opening, closing in _QUOTED if value.startswith(opening))
    if not matching:
        if any(opening in value or closing in value for opening, closing in _QUOTED):
            return None
        return value if _BARE_FILENAME_RE.fullmatch(value) else None
    if len(matching) != 1:
        return None
    opening, closing = matching[0]
    if not value.endswith(closing) or len(value) <= len(opening) + len(closing):
        return None
    inner = value[len(opening) : -len(closing)].strip()
    if any(mark in inner for pair in _QUOTED for mark in pair):
        return None
    return inner if _QUOTED_NAME_RE.fullmatch(inner) else None


def parse_conversation_document_reference_reply(message: object) -> str | None:
    """Return one bounded exact-name lookup term, or ``None``."""

    surface = _canonical_surface(message)
    if surface is None:
        return None
    candidate = _PREFIX_RE.sub("", surface, count=1).strip()
    if not candidate or len(candidate) > _MAX_FILENAME_CHARS:
        return None
    return _unwrap_one_quote_pair(candidate)


__all__ = ["parse_conversation_document_reference_reply"]
