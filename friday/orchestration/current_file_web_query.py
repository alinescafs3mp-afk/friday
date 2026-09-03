"""Closed public-web topic extraction for current-file comparison turns.

The admitted product journey is: read one current-turn file locally, search the
public web for an independent topic named in the same utterance, then compare
locally.  File bytes, filenames and file deictics are never part of the outbound
query.  Ambiguous or file-as-query speech fails closed so the ordinary
private-source web refusal remains in force.
"""

from __future__ import annotations

import re
import unicodedata

from friday.orchestration.supervisor_contracts import SupervisorContractError, parse_query_intent

# Keep cue tables aligned with friday.orchestration.semantic_supervisor.
_WEB_CUES = ("интернет", "web", "публичн", "актуальн", "нынешн", "в сети", "current public")
_COMPARE_CUES = ("сравни", "отлич", "разниц", "versus", " vs ", "сопостав", "compare")
_MAX_QUERY_WORDS = 14
_MAX_MESSAGE_CHARS = 1_200

_COMPARE_VERB = re.compile(
    r"\b(?:сравни\w*|сопостав\w*|отлич\w*|разниц\w*|versus|compare)\b|\bvs\b",
    re.IGNORECASE,
)
_WEB_TRANSPORT = re.compile(
    r"\b(?:найд\w*|найти|поищ\w*|поиш\w*|ищи|искать|поиск\w*|посмотр\w*|смотри|"
    r"глян\w*|провер\w*|узна\w*|search|google|find)\b|"
    r"\bв\s+(?:интернет\w*|инете|сети|вебе|гугле|яндексе|google|web)\b|"
    r"\b(?:интернет(?:е|а|у|ом)?|web)\b",
    re.IGNORECASE,
)
_FILE_DEICTIC_NP = re.compile(
    r"(?:(?:с|со|со\s+стороны|по|из|в|во|про|об|о|к|ко|with|against|to)\s+)?"
    r"(?:эт(?:им|ом|от|ого|ому|ой|у|и|их|ая|ое|а)|данн\w*|текущ\w*|"
    r"присланн\w*|загруженн\w*|прикрепл\w*|приложенн\w*|this|that|the)\s+"
    r"(?:файл\w*|документ\w*|договор\w*|контракт\w*|вложен\w*|текст\w*|"
    r"заметк\w*|file|document|attachment|contract|note)\w*",
    re.IGNORECASE,
)
_FILE_AS_QUERY = re.compile(
    r"(?:"
    r"что\s+(?:в|внутри|написано\s+в)|"
    r"содержим\w*\s+(?:файл|документ|вложен)|"
    r"текст\w*\s+(?:файл|документ|вложен)|"
    r"то,?\s+что\s+(?:в|внутри)\s+(?:файл|документ|вложен)|"
    r"what(?:'s|\s+is)\s+in\s+(?:the\s+)?(?:file|document|attachment)"
    r")",
    re.IGNORECASE,
)
_FILE_CARRIER_LEFTOVER = re.compile(
    r"\b(?:файл\w*|документ\w*|договор\w*|контракт\w*|вложен\w*|"
    r"file|document|attachment|contract)\w*\b",
    re.IGNORECASE,
)
_LOCAL_READ_LEFTOVER = re.compile(
    r"\b(?:обобщ\w*|суммир\w*|перескаж\w*|прочит\w*|раскрой\w*|"
    r"summar\w*|read)\w*\b",
    re.IGNORECASE,
)
_COMPARE_PRONOUN_JOIN = re.compile(
    r"\b(?:их|его|её|ее|them|it)\s+(?:с|со|with|against)\b",
    re.IGNORECASE,
)
_DANGLING_PRONOUN = re.compile(r"\b(?:их|его|её|ее|them|it)\b", re.IGNORECASE)
_LEADING_JOIN = re.compile(
    r"^(?:с|со|with|against|to|и|а|and|then|on)\s+",
    re.IGNORECASE,
)
_TRAILING_PREP = re.compile(
    r"\s+(?:on|in|at|with|с|со|в|во)(?:\s+the)?\s*$",
    re.IGNORECASE,
)
_CLAUSE_SPLIT = re.compile(
    r"\s+(?:и|а\s+затем|затем|потом|and(?:\s+then)?)\s+|[;]|[.!?]\s+",
    re.IGNORECASE,
)
_VAGUE_TOPIC = re.compile(
    r"^(?:"
    r"(?:это|этот|эта|эти|там|тут|его|её|ее|их)|"
    r"(?:данн\w*|информац\w*|сведени\w*|подробност\w*)|"
    r"(?:что[- ]?нибудь|вс[её]|подробнее|дальше)|"
    r"(?:this|that|it|them|there)|(?:more\s+)?(?:data|information|details?)"
    r")\W*$",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"```|~~~")


def _folded(message: str) -> str:
    return unicodedata.normalize("NFKC", message).casefold()


def _has_any(message: str, needles: tuple[str, ...]) -> bool:
    folded = _folded(message)
    return any(needle in folded for needle in needles)


def compare_current_file_web_cues_present(message: str) -> bool:
    """True when the utterance names both comparison and public-web transport."""

    if type(message) is not str or not message.strip():
        return False
    return _has_any(message, _COMPARE_CUES) and _has_any(message, _WEB_CUES)


def _strip_clause(clause: str) -> str:
    text = _COMPARE_VERB.sub(" ", clause)
    text = _FILE_DEICTIC_NP.sub(" ", text)
    text = _COMPARE_PRONOUN_JOIN.sub(" ", text)
    text = _DANGLING_PRONOUN.sub(" ", text)
    text = _WEB_TRANSPORT.sub(" ", text)
    text = _TRAILING_PREP.sub("", text)
    text = _LEADING_JOIN.sub("", " ".join(text.split()).strip(" ,.:;—–-"))
    text = _TRAILING_PREP.sub("", text)
    return " ".join(text.split()).strip(" ,.:;—–-")


def extract_compare_current_file_public_web_query(message: object) -> str:
    """Return the independent public-web topic, or ``""`` when extraction is unsafe."""

    if type(message) is not str:
        return ""
    raw = unicodedata.normalize("NFKC", message)
    if (
        not raw.strip()
        or len(raw) > _MAX_MESSAGE_CHARS
        or "\x00" in raw
        or _CODE_FENCE.search(raw) is not None
        or not compare_current_file_web_cues_present(raw)
        or _FILE_AS_QUERY.search(raw) is not None
    ):
        return ""
    visible = " ".join(raw.split())
    remnants: list[str] = []
    clauses = [visible] if _CLAUSE_SPLIT.search(visible) is None else _CLAUSE_SPLIT.split(visible)
    if not clauses:
        clauses = [visible]
    for clause in clauses:
        remnant = _strip_clause(clause)
        if remnant:
            remnants.append(remnant)
    topic = " ".join(remnants).strip()
    topic = " ".join(topic.split())
    if (
        not topic
        or len(topic.split()) < 2
        or len(topic.split()) > _MAX_QUERY_WORDS
        or _FILE_DEICTIC_NP.search(topic) is not None
        or _FILE_AS_QUERY.search(topic) is not None
        or _FILE_CARRIER_LEFTOVER.search(topic) is not None
        or _LOCAL_READ_LEFTOVER.search(topic) is not None
        or _COMPARE_VERB.search(topic) is not None
        or _WEB_TRANSPORT.search(topic) is not None
        or _VAGUE_TOPIC.fullmatch(topic) is not None
    ):
        return ""
    try:
        return parse_query_intent(topic, label="compare-current-file public web query")
    except SupervisorContractError:
        return ""
