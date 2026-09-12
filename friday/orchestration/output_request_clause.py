"""Output syntax shared by file and comparison presentation.

Callers mask quoted content and prove the complete imperative prefix. Format
tokens need an output-object or carrier relation; their presence is not one.
"""

from __future__ import annotations

import re

_CLAUSE_BOUNDARY = re.compile(r"(?<=[.!?;])\s+|\n\s*\n")
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
_USER_TRANSITION = re.compile(r"(?:а\s+теперь\b|моя\s+просьба\s*:|мой\s+запрос\s*:)", re.IGNORECASE)
_LINE_SEPARATOR = re.compile(r"\r\n?|[\v\f\x1c-\x1e\x85\u2028\u2029]")
_INDENTED_CODE = re.compile(r"(?m)^(?: {4}| {0,3}\t)[^\n]*(?:\n(?: {4}| {0,3}\t)[^\n]*)*")
_STRIKETHROUGH = re.compile(r"~~[^~]+~~")

FILE_OUTPUT_FORMAT_TOKEN = (
    r"(?:\.?docx?|word|ворд\w*|\.?xlsx?|excel|эксел\w*|эксель\w*|"
    r"\.?pdf|пдф\w*|\.?png|картинк\w*|изображени\w*)"
)
FILE_OUTPUT_ACTION = (
    r"(?:сдела\w*|созда\w*|собер\w*|собра\w*|сформир\w*|подготов\w*|"
    r"оформ\w*|состав\w*|выгруз\w*|экспорт\w*|конверт\w*|преобраз\w*|"
    r"перевед\w*|генерир\w*|сгенерир\w*|пришл\w*|отправ\w*|сохран\w*)"
)
_OUTPUT_MODIFIER = (
    r"(?:мне|нам|пожалуйста|просто|один|одну|одно|одним|нов\w*|готов\w*|"
    r"красив\w*|кратк\w*|отдельн\w*|итогов\w*|полн\w*|обычн\w*|приложенн\w*)"
)
FILE_OUTPUT_TARGET_PATTERNS = (
    re.compile(
        rf"\b{FILE_OUTPUT_ACTION}\b[^.!?\n]{{0,100}}?\b(?:в|как)\s+"
        rf"(?:(?:красив\w*|готов\w*)\s+)?(?:(?:виде|формат|файл)\w*\s+)?"
        rf"(?P<format>{FILE_OUTPUT_FORMAT_TOKEN})(?!\w)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{FILE_OUTPUT_ACTION}\b\s+(?:{_OUTPUT_MODIFIER}\s+){{0,4}}"
        rf"(?P<format>{FILE_OUTPUT_FORMAT_TOKEN})(?!\w)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:файл|документ|таблицу|отч[её]т|справку)\s+"
        rf"(?P<format>{FILE_OUTPUT_FORMAT_TOKEN})(?!\w)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<!\w)(?P<format>{FILE_OUTPUT_FORMAT_TOKEN})(?!\w)[-\s]+файлом\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{FILE_OUTPUT_ACTION}\b\s+(?:{_OUTPUT_MODIFIER}\s+){{0,4}}"
        rf"не\s+{FILE_OUTPUT_FORMAT_TOKEN}\s*,?\s*а\s+"
        rf"(?P<format>{FILE_OUTPUT_FORMAT_TOKEN})(?!\w)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b{FILE_OUTPUT_ACTION}\b\s+(?:{_OUTPUT_MODIFIER}\s+){{0,4}}"
        r"(?:по\s+(?:нему|ней|ним)\s+)?"
        r"[^\s,;:!?/\\<>]+\.(?P<format>docx?|xlsx?|pdf|png)(?!\w)",
        re.IGNORECASE,
    ),
)
_DIRECT_ARTIFACT = re.compile(
    rf"\b{FILE_OUTPUT_ACTION}\b"
    r"(?:\s+в\s+(?:mcp\s+)?outbox)?"
    r"(?:\s+(?!(?:по|о|об|про|для|с|на|в)\b)\w+){0,3}\s+"
    r"(?:отчёт|отчет|справку|документ|таблицу|файл|картинку|изображение)\b|"
    r"\b(?:пришли|отправь|скинь)\s+файл\w*",
    re.IGNORECASE,
)
_GENERIC_OUTPUT_CARRIER = (
    r"\b(?:в\s+(?:документ|файл|отч[её]т|справку|таблицу|табличку)\b|"
    r"(?:в\s+виде|как)\s+(?:документа?|файла?|отч[её]та?|справк[ауи]|"
    r"таблиц[ауы]|табличк[ауи])\b|"
    r"(?:документом|файлом|отч[её]том|справкой|таблицей|табличкой)\b)"
)
_FORWARD_GENERIC_CARRIER = re.compile(
    rf"\b{FILE_OUTPUT_ACTION}\b[^,.;:!?\n]{{0,100}}?{_GENERIC_OUTPUT_CARRIER}",
    re.IGNORECASE,
)
_PREPOSED_OUTPUT_PREFIX = re.compile(
    r"\s*(?:(?:а|и|ну)\s+)?(?:пожалуйста[,\s]+)?"
    r"(?:(?:можешь|можете|можно|прошу|хочу)\s+)?"
    r"(?:(?:мне|нам|пожалуйста)\s+){0,2}"
    r"(?:(?:(?:этот|эту|данный|данную|полученный|полученную)\s+)?"
    r"(?:его|е[её]|это|ответ|результат|сводку|список|документ|файл|данные)\s+)?"
    rf"(?:(?P<generic>{_GENERIC_OUTPUT_CARRIER})|"
    rf"(?:в\s+(?:(?:виде|формате)\s+)?|как\s+)?(?P<format>{FILE_OUTPUT_FORMAT_TOKEN}))\s+",
    re.IGNORECASE,
)
_PREPOSED_OUTPUT_ACTION = re.compile(
    r"оформи(?:ть|те)?|сдела(?:ть|йте|й)|созда(?:ть|йте|й)|"
    r"сформир(?:овать|уй(?:те)?)|подготов(?:ить|ь(?:те)?)|"
    r"соб(?:рать|ери(?:те)?)|состав(?:ить|ь(?:те)?)|"
    r"сохрани(?:ть|те)?|экспортир(?:овать|уй(?:те)?)|"
    r"преобраз(?:овать|уй(?:те)?)|выгрузи(?:ть|те)?|"
    r"пришл(?:и|ите)|прислать|отправ(?:ь(?:те)?|ить)|скин(?:ь(?:те)?|уть)|"
    r"выда(?:й(?:те)?|ть)|(?:с)?генерир(?:овать|уй(?:те)?)|"
    r"конвертир(?:овать|уй(?:те)?)|переве(?:ди(?:те)?|сти)",
    re.IGNORECASE,
)


def file_output_action_is_command(action: str) -> bool:
    """Admit an imperative/infinitive, not a report of an earlier action."""

    return _PREPOSED_OUTPUT_ACTION.fullmatch(action) is not None


def preposed_file_output_target(prefix: str, action: str, *, table_is_carrier: bool = True) -> str:
    """Prove a complete request lead whose output target precedes its action."""

    if not file_output_action_is_command(action):
        return ""
    match = _PREPOSED_OUTPUT_PREFIX.fullmatch(prefix)
    if match is None:
        return ""
    if token := match.group("format"):
        return token
    if re.search(r"\bтабли[цч]", match.group("generic"), re.IGNORECASE):
        return "xlsx" if table_is_carrier else ""
    return "docx"


def file_output_object_requested(clause: str, *, table_is_carrier: bool = True) -> bool:
    """Recognize an output object in an independently admitted action clause."""

    if any(pattern.search(clause) for pattern in FILE_OUTPUT_TARGET_PATTERNS):
        return True
    # Some code-owned replies already render a table in the conversation.
    # Their caller can withhold only that implicit carrier; explicit file
    # objects/formats and a separately requested artifact remain authoritative.
    for pattern in (_DIRECT_ARTIFACT, _FORWARD_GENERIC_CARRIER):
        for match in pattern.finditer(clause):
            if table_is_carrier or not re.search(r"\bтабли[цч]\w*\b$", match.group(), re.IGNORECASE):
                return True
    return False


def mask_output_request_markup(surface: str) -> str:
    """Normalize physical lines before masking their non-command markup."""

    surface = _LINE_SEPARATOR.sub("\n", surface)
    for pattern in (_INDENTED_CODE, _STRIKETHROUGH):
        surface = pattern.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), surface)
    return surface


# These punctuation marks belong to one technical atom, not a prose
# introducer. Other colons, including a reporter before such an atom, remain.
_TECHNICAL_COLON_ATOM = re.compile(
    r"\b(?:[a-z0-9][a-z0-9_-]*:){2,}[a-z0-9][a-z0-9_-]*\b|"
    r"\b[a-z][a-z0-9+.-]*://[^\s]+|\b[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?\b",
    re.IGNORECASE | re.ASCII,
)


def _has_prose_colon(text: str) -> bool:
    return ":" in _TECHNICAL_COLON_ATOM.sub("", text)


def output_request_clause_prefix(surface: str, action_start: int) -> str:
    """Keep the introducer's speaker across sentences and numbered items."""

    prefix = surface[:action_start]
    start = 0
    block = False
    for boundary in _CLAUSE_BOUNDARY.finditer(prefix):
        preceding = boundary.start() - 1
        while preceding >= 0 and prefix[preceding].isspace():
            preceding -= 1
        # An empty paragraph after an introducer has no new speaker/content.
        if preceding >= 0 and prefix[preceding] == ":":
            block = True
            continue
        block = block or _has_prose_colon(prefix[start : boundary.start()])
        explicit_transition = _USER_TRANSITION.match(prefix[boundary.end() :]) is not None
        if block and not _PARAGRAPH_BREAK.search(boundary.group()) and not explicit_transition:
            continue
        block = False
        start = boundary.end()
    return prefix[start:]
