"""Admit one current-file + authorized-archive + public-web comparison turn.

The public-web topic is sealed from the utterance after file and archive
carriers are stripped.  File bytes, archive filenames and private paths never
enter the outbound query.  This module does not read storage, fetch web pages
or publish a result.
"""

from __future__ import annotations

import re
import unicodedata

from friday.orchestration.current_file_web_query import (
    compare_current_file_web_cues_present,
    extract_independent_public_web_query,
    utterance_names_local_file,
)

_ARCHIVE_CUES = (
    "архив",
    "archive",
    "предыдущ",
    "прошл",
    "earlier",
    "previous file",
    "stored revision",
    "сохранённ",
    "сохраненн",
)
_FILENAME_RE = re.compile(r"(?<![\w@])@?(?P<name>[0-9A-Za-zА-ЯЁа-яё_.-]{1,247}\.[0-9A-Za-z]{1,12})(?![\w.])")
_PRIVATE_PATH_RE = re.compile(
    r"(?:^|[\s«\"'`])(?:~|/|\.\./|\.\.\\|[A-Za-z]:\\)",
)
_ARCHIVE_DEICTIC_NP = re.compile(
    r"(?:(?:с|со|with|against|to|и)\s+)?"
    r"(?:эт(?:им|ом|от|ого|ому|ой|у)|данн\w*|архивн\w*|сохран[её]нн\w*|"
    r"предыдущ\w*|прошл\w*|this|that|the|previous|earlier|stored)\s+"
    r"(?:архив\w*|файл\w*|документ\w*|ревизи\w*|договор\w*|"
    r"archive|file|document|revision|contract)\w*",
    re.IGNORECASE,
)
_RAW_ID_RE = re.compile(r"\braw_[0-9a-f]{16}\b")


def _folded(message: str) -> str:
    return unicodedata.normalize("NFKC", message).casefold()


def mixed_file_archive_web_cues_present(message: object) -> bool:
    """True when the utterance names compare, public web, a local file and archive."""

    if type(message) is not str or not message.strip():
        return False
    if _PRIVATE_PATH_RE.search(message) is not None:
        return False
    folded = _folded(message)
    if not compare_current_file_web_cues_present(message):
        return False
    if not utterance_names_local_file(message):
        return False
    return any(cue in folded for cue in _ARCHIVE_CUES)


def extract_authorized_archive_filename(message: object) -> str:
    """Return one exact archive filename, or ``\"\"`` when absent/ambiguous/private."""

    if type(message) is not str or not message.strip():
        return ""
    if _PRIVATE_PATH_RE.search(message) is not None:
        return ""
    names = [match.group("name") for match in _FILENAME_RE.finditer(message)]
    unique = tuple(dict.fromkeys(names))
    if len(unique) != 1:
        return ""
    name = unique[0]
    if "/" in name or "\\" in name or name.startswith(".") or ".." in name:
        return ""
    return name


def extract_authorized_archive_raw_id(message: object) -> str:
    """Return one expert-path archive raw id, or ``\"\"``."""

    if type(message) is not str:
        return ""
    found = _RAW_ID_RE.findall(message)
    unique = tuple(dict.fromkeys(found))
    return unique[0] if len(unique) == 1 else ""


_ARCHIVE_WORD_RE = re.compile(
    r"\b(?:архив\w*|archive\w*|ревизи\w*|revision\w*|stored)\b",
    re.IGNORECASE,
)


def _strip_archive_carriers(message: str) -> str:
    text = _ARCHIVE_DEICTIC_NP.sub(" ", message)
    text = _FILENAME_RE.sub(" ", text)
    text = _RAW_ID_RE.sub(" ", text)
    text = _ARCHIVE_WORD_RE.sub(" ", text)
    return " ".join(text.split())


def extract_mixed_file_archive_public_web_query(message: object) -> str:
    """Return the independent public-web topic with file/archive carriers removed."""

    if type(message) is not str or not mixed_file_archive_web_cues_present(message):
        return ""
    stripped = _strip_archive_carriers(unicodedata.normalize("NFKC", message))
    return extract_independent_public_web_query(stripped)


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
