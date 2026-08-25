"""Closed syntax admission for comparing selected messages with one document.

This module is intentionally pure and schema-independent.  It recognizes only
one bounded RU/EN follow-up shape: an explicit comparison of the retained
message evidence with one document or file.  Authority, evidence type and file
resolution remain runtime concerns; a successful parse grants no capability by
itself.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

_MAX_SURFACE_LENGTH = 256
_MAX_SURFACE_UTF8_BYTES = 768

_QUOTE_OPENERS = {
    '"': '"',
    "'": "'",
    "«": "»",
    "‹": "›",
    "“": "”",
    "„": "“",
}
_QUOTE_CLOSERS = frozenset(_QUOTE_OPENERS.values())
_QUOTE_CHARACTERS = frozenset(_QUOTE_OPENERS) | _QUOTE_CLOSERS

_CODE_MARKUP_RE = re.compile(
    r"`|~~~|\{\{|\}\}|</?(?:code|pre|script|tool|system|developer)\b",
    re.IGNORECASE,
)
_CONTROL_META_RE = re.compile(
    r"\b(?:"
    r"ignore(?:\s+(?:all|any|the|these|those|prior|previous))?\s+instructions?|"
    r"forget\s+(?:all|the|prior|previous)\s+instructions?|"
    r"system\s+(?:prompt|message|instructions?|metadata)|"
    r"developer\s+(?:message|instructions?)|hidden\s+instructions?|"
    r"(?:your|my|the|these|those|model|runtime)\s+instructions?|"
    r"internal\s+(?:prompt|instructions?|metadata)|chain\s+of\s+thought|"
    r"jailbreak|override\s+(?:the\s+)?instructions?|bypass\s+(?:the\s+)?rules?|"
    r"игнорир\w*|забуд\w*|системн\w*\s+(?:промпт\w*|сообщени\w*|инструкц\w*|метаданн\w*)|"
    r"разработчик\w*\s+(?:сообщени\w*|инструкц\w*)|скрыт\w*\s+инструкц\w*|"
    r"(?:тво\w*|мо\w*|эти\w*|модел\w*|рантайм\w*)\s+инструкц\w*|"
    r"внутренн\w*\s+(?:промпт\w*|инструкц\w*|метаданн\w*)|"
    r"цепочк\w*\s+мысл\w*|обойди\w*\s+(?:правил\w*|ограничени\w*)"
    r")\b",
    re.IGNORECASE,
)
_EFFECT_OR_SECOND_ACTION_RE = re.compile(
    r"\b(?:"
    r"creat\w*|writ\w*|edit\w*|chang\w*|modif\w*|updat\w*|delet\w*|remov\w*|"
    r"renam\w*|mov\w*|sav\w*|send\w*|sent|email\w*|publish\w*|post\w*|"
    r"uploads?|uploading|attach(?:es|ing)?|"
    r"download\w*|print\w*|execut\w*|run\w*|ran|open\w*|clos\w*|call\w*|"
    r"schedul\w*|remind\w*|translat\w*|rewrit\w*|summari[sz]\w*|analy[sz]\w*|"
    r"explain\w*|render\w*|format\w*|export\w*|install\w*|connect\w*|sync\w*|"
    r"synchroniz\w*|add\w*|append\w*|"
    r"созда\w*|добав\w*|допис\w*|запис\w*|измен\w*|редакт\w*|удал\w*|"
    r"переимен\w*|перемест\w*|сохран\w*|отправ\w*|пришл\w*|перешл\w*|"
    r"опублик\w*|загрузи(?:ть|те)?|прикрепи(?:ть|те)?|скач\w*|распечат\w*|"
    r"выполн\w*|запуст\w*|"
    r"позвон\w*|напомн\w*|заплан\w*|перевед\w*|перевод\w*|перепиш\w*|"
    r"суммир\w*|объясн\w*|оформ\w*|верн\w*|вывед\w*|синхрон\w*|обнов\w*|"
    r"установ\w*|подключ\w*"
    r")\b",
    re.IGNORECASE,
)
_WEB_OR_SEARCH_RE = re.compile(
    r"\b(?:"
    r"web|internet|online|website|site|url|browser|google|wikipedia|news|"
    r"search\w*|brows\w*|find|finding|lookup|look\s+up|verify\w*|check\w*|"
    r"external\s+sources?|other\s+sources?|archive|cloud|"
    r"интернет\w*|онлайн|веб|сайт\w*|браузер\w*|гугл\w*|википед\w*|новост\w*|"
    r"поиск\w*|ищи\w*|искать|найди|найдите|найти|поищи\w*|проверь\w*|сверь\w*|"
    r"внешн\w*\s+источник\w*|друг\w*\s+источник\w*|архив\w*|облак\w*"
    r")\b",
    re.IGNORECASE,
)
_OUTPUT_MODE_RE = re.compile(
    r"\b(?:"
    r"json|xml|yaml|csv|markdown|html|table|tabular|bullet\w*|list|brief|briefly|"
    r"short|concise|detail\w*|thoroughly|sentence\w*|paragraph\w*|citation\w*|"
    r"quote\w*|footnote\w*|"
    r"layout|style|language|english|russian|german|polish|french|spanish|only|"
    r"without|summary|"
    r"таблиц\w*|списк\w*|маркирован\w*|формат\w*|кратк\w*|коротк\w*|подробн\w*|"
    r"тезис\w*|"
    r"предложени\w*|абзац\w*|цитат\w*|сноск\w*|ссылк\w*|стил\w*|язык\w*|"
    r"английск\w*|русск\w*|немецк\w*|польск\w*|французск\w*|испанск\w*|"
    r"только|без|резюме"
    r")\b",
    re.IGNORECASE,
)
_MULTIPLE_DOCUMENT_RE = re.compile(
    r"\b(?:"
    r"documents|files|attachments|another\s+(?:document|file|attachment)|"
    r"(?:two|three|four|several|multiple)\s+(?:documents?|files?|attachments?)|"
    r"документы|документов|документами|файлы|файлов|файлами|вложений|вложениями|"
    r"(?:два|две|три|четыре|несколько)\s+(?:документ\w*|файл\w*|вложени\w*)|"
    r"(?:друг\w*|ещ[ёе]\s+один)\s+(?:документ\w*|файл\w*|вложени\w*)"
    r")\b",
    re.IGNORECASE,
)
_DOCUMENT_NOUN_RE = re.compile(
    r"(?<!\w)(?:document|file|attachment|документ(?:а|у|ом|е)?|файл(?:а|у|ом|е)?|"
    r"вложени(?:е|я|ю|ем))(?!\w)",
    re.IGNORECASE,
)

_QUOTED_TITLE = (
    r"(?:"
    r"«[^«»]{1,192}»|‹[^‹›]{1,192}›|"
    r'"[^"]{1,192}"|\'[^\']{1,192}\'|'
    r"“[^“”]{1,192}”|„[^„“]{1,192}“"
    r")"
)
_SAFE_TITLE_TOKEN = r"[^\W_][\w.-]{0,79}"
_FILENAME_TOKEN = r"[^\W_][\w-]{0,63}\.[^\W_][\w.-]{0,15}"
_RU_TITLE = (
    rf"(?:\s+(?:{_QUOTED_TITLE}|{_FILENAME_TOKEN}|"
    rf"(?:под\s+названием|с\s+названием)\s+(?:{_QUOTED_TITLE}|{_SAFE_TITLE_TOKEN})))?"
)
_EN_TITLE = (
    rf"(?:\s+(?:{_QUOTED_TITLE}|{_FILENAME_TOKEN}|"
    rf"(?:named|called)\s+(?:{_QUOTED_TITLE}|{_SAFE_TITLE_TOKEN})))?"
)

_RU_MESSAGE_REFERENCE = (
    r"(?:"
    r"(?:ранее\s+)?выбранн(?:ые|ых|ыми|ым|ое|ого|ому|ом)\s+"
    r"(?:сообщени(?:я|й|ями|е|ем)|переписк(?:а|у|ой|е)|фрагмент(?:ы|ов|ами))|"
    r"найденн(?:ые|ых|ыми|ым|ое|ого|ому|ом)\s+"
    r"(?:сообщени(?:я|й|ями|е|ем)|переписк(?:а|у|ой|е)|фрагмент(?:ы|ов|ами))|"
    r"эт(?:и|их|ими|им|о|ого)\s+(?:сообщени(?:я|й|ями|е|ем)|фрагмент(?:ы|ов|ами))|"
    r"(?:эту|этой)\s+переписк(?:у|ой)|их|ними"
    r")"
)
_EN_MESSAGE_REFERENCE = (
    r"(?:"
    r"(?:the\s+)?(?:previously\s+)?selected\s+"
    r"(?:messages?|conversation|chat|message\s+evidence|excerpts?)|"
    r"(?:the\s+)?found\s+(?:messages?|conversation|excerpts?)|"
    r"(?:these|those)\s+(?:selected\s+)?(?:messages?|excerpts?)|"
    r"the\s+selected\s+(?:evidence|result)|them"
    r")"
)

_RU_DOCUMENT_REFERENCE = (
    rf"(?:(?:этим|этот|этого|этому|этом|одним|выбранным|выбранный|выбранного|"
    rf"прикрепл[её]нным|прикрепл[её]нный|приложенным|приложенный|"
    rf"загруженным|загруженный|последним|последний|"
    rf"моим|мой|предоставленным|предоставленный)\s+)*(?:pdf[- ]?)?"
    rf"(?:документ(?:а|у|ом|е)?|файл(?:а|у|ом|е)?|вложени(?:е|я|ю|ем)){_RU_TITLE}"
)
_EN_DOCUMENT_REFERENCE = (
    rf"(?:(?:this|that|the|a|an|one|my|selected|attached|uploaded|provided|latest|last|"
    rf"pdf|text|word)\s+)*(?:document|file|attachment){_EN_TITLE}"
)


def _pair_patterns(
    message_reference: str,
    document_reference: str,
    *,
    language: str,
) -> tuple[re.Pattern[str], ...]:
    if language == "ru":
        imperative = r"(?:пожалуйста,\s*)?(?:сравни|сравните|сопоставь|сопоставьте)(?:,\s*пожалуйста,?)?"
        infinitive = (
            r"(?:(?:можешь|можете|не\s+мог\s+бы|не\s+могла\s+бы|не\s+могли\s+бы)"
            r"(?:\s+пожалуйста)?\s+|можно\s+ли\s+)(?:сравнить|сопоставить)"
        )
        request = rf"(?:теперь,?\s+)?(?:{imperative}|{infinitive})"
        return tuple(
            re.compile(pattern)
            for pattern in (
                rf"{request}\s+{message_reference}\s+(?:с|со)\s+{document_reference}(?:,?\s+пожалуйста)?",
                rf"{request}\s+{document_reference}\s+(?:с|со)\s+{message_reference}(?:,?\s+пожалуйста)?",
                rf"{request}\s+{message_reference}\s+и\s+{document_reference}(?:,?\s+пожалуйста)?",
                rf"{request}\s+{document_reference}\s+и\s+{message_reference}(?:,?\s+пожалуйста)?",
                rf"чем\s+{message_reference}\s+отлича(?:ется|ются)\s+от\s+{document_reference}",
                rf"чем\s+{document_reference}\s+отлича(?:ется|ются)\s+от\s+{message_reference}",
                rf"(?:в\s+ч[её]м|какие)\s+(?:различия|сходства|отличия)\s+между\s+"
                rf"{message_reference}\s+и\s+{document_reference}",
                rf"(?:в\s+ч[её]м|какие)\s+(?:различия|сходства|отличия)\s+между\s+"
                rf"{document_reference}\s+и\s+{message_reference}",
            )
        )

    request = (
        r"(?:now,?\s+)?(?:(?:please\s+)?(?:compare(?:\s+and\s+contrast)?|contrast)|"
        r"(?:can|could|would|will)\s+you(?:\s+please)?\s+"
        r"(?:compare(?:\s+and\s+contrast)?|contrast))"
    )
    return tuple(
        re.compile(pattern)
        for pattern in (
            rf"{request}\s+{message_reference}\s+(?:with|to|against)\s+{document_reference}(?:,?\s+please)?",
            rf"{request}\s+{document_reference}\s+(?:with|to|against)\s+{message_reference}(?:,?\s+please)?",
            rf"{request}\s+{message_reference}\s+and\s+{document_reference}(?:,?\s+please)?",
            rf"{request}\s+{document_reference}\s+and\s+{message_reference}(?:,?\s+please)?",
            rf"how\s+(?:do|does)\s+{message_reference}\s+compare\s+(?:with|to)\s+{document_reference}",
            rf"how\s+(?:do|does)\s+{document_reference}\s+compare\s+(?:with|to)\s+{message_reference}",
            rf"how\s+(?:do|does)\s+{message_reference}\s+differ\s+from\s+{document_reference}",
            rf"how\s+(?:do|does)\s+{document_reference}\s+differ\s+from\s+{message_reference}",
            rf"what\s+are\s+(?:the\s+)?(?:differences|similarities)\s+between\s+"
            rf"{message_reference}\s+and\s+{document_reference}",
            rf"what\s+are\s+(?:the\s+)?(?:differences|similarities)\s+between\s+"
            rf"{document_reference}\s+and\s+{message_reference}",
        )
    )


_ADMITTED_PATTERNS = (
    *_pair_patterns(_RU_MESSAGE_REFERENCE, _RU_DOCUMENT_REFERENCE, language="ru"),
    *_pair_patterns(_EN_MESSAGE_REFERENCE, _EN_DOCUMENT_REFERENCE, language="en"),
)


class ConversationDocumentComparisonFollowupKind(StrEnum):
    """The only continuation syntax admitted by this parser."""

    COMPARE = "compare_conversation_with_document"


def _is_word_apostrophe(surface: str, index: int) -> bool:
    return (
        surface[index] == "'"
        and index > 0
        and index + 1 < len(surface)
        and surface[index - 1].isalnum()
        and surface[index + 1].isalnum()
    )


def _quotes_are_closed(surface: str) -> bool:
    expected: str | None = None
    content_start = 0
    closed_pairs = 0
    for index, character in enumerate(surface):
        if _is_word_apostrophe(surface, index):
            continue
        if expected is not None:
            if character == expected:
                if not surface[content_start:index].strip():
                    return False
                expected = None
                closed_pairs += 1
            elif character in _QUOTE_CHARACTERS:
                return False
            continue
        closing_only = character in _QUOTE_CLOSERS and character not in _QUOTE_OPENERS
        if closing_only:
            return False
        closer = _QUOTE_OPENERS.get(character)
        if closer is not None:
            expected = closer
            content_start = index + 1
    return expected is None and closed_pairs <= 1


def _canonical_surface(message: object) -> str | None:
    if type(message) is not str or not message or len(message) > _MAX_SURFACE_LENGTH:
        return None
    if any(unicodedata.category(character).startswith("C") for character in message):
        return None
    try:
        encoded = message.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    if len(encoded) > _MAX_SURFACE_UTF8_BYTES:
        return None
    surface = " ".join(unicodedata.normalize("NFKC", message).split())
    if (
        not surface
        or len(surface) > _MAX_SURFACE_LENGTH
        or any(unicodedata.category(character).startswith("C") for character in surface)
    ):
        return None
    try:
        if len(surface.encode("utf-8", errors="strict")) > _MAX_SURFACE_UTF8_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    return surface


def parse_conversation_document_comparison_followup(
    message: object,
) -> ConversationDocumentComparisonFollowupKind | None:
    """Parse one explicit, read-only selected-messages/document comparison."""

    surface = _canonical_surface(message)
    if surface is None or _CODE_MARKUP_RE.search(surface) or not _quotes_are_closed(surface):
        return None
    if surface[-1:] in {".", "?", "!"}:
        surface = surface[:-1].rstrip()
    if not surface or any(character in surface for character in "?!;"):
        return None
    surface = surface.casefold()
    if (
        len(surface) > _MAX_SURFACE_LENGTH
        or len(surface.encode("utf-8")) > _MAX_SURFACE_UTF8_BYTES
        or _CONTROL_META_RE.search(surface)
        or _EFFECT_OR_SECOND_ACTION_RE.search(surface)
        or _WEB_OR_SEARCH_RE.search(surface)
        or _OUTPUT_MODE_RE.search(surface)
        or _MULTIPLE_DOCUMENT_RE.search(surface)
        or len(tuple(_DOCUMENT_NOUN_RE.finditer(surface))) != 1
    ):
        return None
    if not any(pattern.fullmatch(surface) is not None for pattern in _ADMITTED_PATTERNS):
        return None
    return ConversationDocumentComparisonFollowupKind.COMPARE


def is_conversation_document_comparison_followup_syntax(message: object) -> bool:
    """Return whether ``message`` is inside the closed comparison grammar."""

    return (
        parse_conversation_document_comparison_followup(message)
        is ConversationDocumentComparisonFollowupKind.COMPARE
    )


__all__ = [
    "ConversationDocumentComparisonFollowupKind",
    "is_conversation_document_comparison_followup_syntax",
    "parse_conversation_document_comparison_followup",
]
