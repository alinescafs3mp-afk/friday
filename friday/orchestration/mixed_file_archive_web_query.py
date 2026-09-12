"""Admit one current-file + authorized-archive + public-web comparison turn.

The public-web topic is sealed from the utterance after file and archive
carriers are stripped.  File bytes, archive filenames and private paths never
enter the outbound query.  This module does not read storage, fetch web pages
or publish a result.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from friday.orchestration.current_file_web_query import (
    PRIVATE_RAW_REFERENCE_RE,
    compare_current_file_web_cues_present,
    extract_independent_public_web_query,
    utterance_names_local_file,
)

_ARCHIVE_REFERENCE_PATTERN = (
    r"\b(?:архив(?:ом|а|е|у)?|archive(?:\s+(?:revision|file|document|copy|snapshot|version))?|"
    r"(?:архивн\w*|предыдущ\w*|прошл\w*|сохран[её]нн\w*)\s+"
    r"(?:ревизи\w*|верси\w*|файл\w*|документ\w*|договор\w*|копи\w*|слеп\w*|сним\w*)|"
    r"(?:previous|earlier|stored|saved|archived)\s+(?:revision|file|document|contract|copy|snapshot|version))\b"
)
# Privacy recognition must be wider than the admitted selector grammar.
# Unknown archive wording stays in the closed mixed path, never generic web.
_ARCHIVE_CUE_RE = re.compile(
    r"\b(?:архив\w*|archiv(?:e|ed|al))\b|" + _ARCHIVE_REFERENCE_PATTERN,
    re.IGNORECASE,
)
_ARCHIVE_PREFIX_RE = re.compile(
    _ARCHIVE_REFERENCE_PATTERN + r"(?:\s*:\s*|\s+)",
    re.IGNORECASE,
)
_FILENAME_RE = re.compile(r"(?<![\w@])@?(?P<name>[0-9A-Za-zА-ЯЁа-яё_.-]{1,247}\.[0-9A-Za-z]{1,12})(?![\w.])")
_EXACT_FILENAME_RE = re.compile(r"[\w ()-][\w .()-]*\.[A-Za-z0-9]{1,12}\Z")
_RAW_ID_RE = re.compile(r"raw_[0-9a-f]{16}\Z")
_SELECTOR_END_RE = re.compile(r"(?:\s*(?:$|[,;.!?]|\b(?:и|а|and|with|against|с|со)\b))", re.IGNORECASE)
_QUOTE_PAIRS = {'"': '"', "'": "'", "`": "`", "«": "»", "“": "”"}
_PRIVATE_PATH_RE = re.compile(
    r"(?:^|[\s«\"'`])(?:~|/|\.\./|\.\.\\|[A-Za-z]:\\)",
)


@dataclass(frozen=True, slots=True)
class _ArchiveSelector:
    filename: str
    raw_id: str
    remainder: str


def _archive_selector(message: object) -> _ArchiveSelector | None:
    """Parse one complete archive reference; never salvage a filename suffix."""

    if type(message) is not str or not 1 <= len(message) <= 1_200:
        return None
    if _PRIVATE_PATH_RE.search(message) is not None:
        return None
    prefix = _ARCHIVE_PREFIX_RE.search(message)
    if prefix is None or prefix.end() == len(message):
        return None
    start = prefix.end()
    quoted = message[start] in _QUOTE_PAIRS
    filename_marker = False
    if quoted:
        end = message.find(_QUOTE_PAIRS[message[start]], start + 1)
        if end < 0:
            return None
        value = message[start + 1 : end]
        end += 1
    else:
        token = re.match(r"[^\s,;]+", message[start:])
        if token is None:
            return None
        value = token[0]
        end = start + len(value)
        # One sentence terminator may follow an otherwise complete selector.
        # Keep it in the remainder to preserve the public clause boundary;
        # never search inside a malformed token for a valid filename suffix.
        if value.endswith((".", "!", "?")):
            value = value[:-1]
            end -= 1
        filename_marker = value.startswith("@")
        if filename_marker:
            value = value[1:]
    if _SELECTOR_END_RE.match(message[end:]) is None:
        return None
    raw_id = value if not quoted and not filename_marker and _RAW_ID_RE.fullmatch(value) is not None else ""
    filename = ""
    if not raw_id:
        if (
            not 1 <= len(value) <= 260
            or value != value.strip()
            or value.startswith(".")
            or ".." in value
            or _EXACT_FILENAME_RE.fullmatch(value) is None
        ):
            return None
        filename = value
    remainder = message[: prefix.start()] + " " + message[end:]
    normalized = unicodedata.normalize("NFKC", remainder)
    if (
        _ARCHIVE_PREFIX_RE.search(normalized) is not None
        or _FILENAME_RE.search(normalized) is not None
        or PRIVATE_RAW_REFERENCE_RE.search(normalized) is not None
    ):
        return None
    return _ArchiveSelector(filename, raw_id, remainder)


def mixed_file_archive_web_cues_present(message: object) -> bool:
    """True when the utterance names compare, public web, a local file and archive."""

    if type(message) is not str or not message.strip():
        return False
    if not compare_current_file_web_cues_present(message):
        return False
    if not utterance_names_local_file(message):
        return False
    # Recognize malformed references too, so the outbound adapter cannot
    # bypass mixed validation by falling back to generic file/web extraction.
    return _ARCHIVE_CUE_RE.search(message) is not None


def extract_authorized_archive_filename(message: object) -> str:
    """Return one exact archive filename, or ``\"\"`` when absent/ambiguous/private."""

    selector = _archive_selector(message)
    return selector.filename if selector is not None else ""


def extract_authorized_archive_raw_id(message: object) -> str:
    """Return one expert-path archive raw id, or ``\"\"``."""

    selector = _archive_selector(message)
    return selector.raw_id if selector is not None else ""


def extract_mixed_file_archive_public_web_query(message: object) -> str:
    """Return the independent public-web topic with file/archive carriers removed."""

    if type(message) is not str or not mixed_file_archive_web_cues_present(message):
        return ""
    selector = _archive_selector(message)
    return extract_independent_public_web_query(selector.remainder) if selector is not None else ""


def mixed_file_archive_web_turn_is_admitted(
    message: object,
    *,
    attachments: object = None,
) -> bool:
    """True when a current attachment, archive selector and sealed web topic coexist."""

    if not mixed_file_archive_web_cues_present(message):
        return False
    if not extract_mixed_file_archive_public_web_query(message):
        return False
    if not extract_authorized_archive_filename(message) and not extract_authorized_archive_raw_id(message):
        return False
    if not isinstance(attachments, list) or not attachments:
        return False
    return any(isinstance(item, dict) for item in attachments)


__all__ = [
    "extract_authorized_archive_filename",
    "extract_authorized_archive_raw_id",
    "extract_mixed_file_archive_public_web_query",
    "mixed_file_archive_web_cues_present",
    "mixed_file_archive_web_turn_is_admitted",
]
