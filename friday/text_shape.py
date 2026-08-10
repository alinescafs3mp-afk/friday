"""Small fail-closed repairs for explicit text-composition contracts.

The model remains responsible for prose.  This module only fixes a bounded set
of structurally unambiguous slips after generation: duplicate required
literals, unrequested simple emphasis, exact-list overflow, a quoted
explanation line, and a named literal left outside explicitly requested angle
brackets.  Ambiguous requests and answers are returned byte-for-byte unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_MAX_REQUEST_CHARS = 4_000
_MAX_ANSWER_CHARS = 16_000

TEXT_SHAPE_UNOWNED: Literal["unowned"] = "unowned"
TEXT_SHAPE_VALID: Literal["valid"] = "valid"
TEXT_SHAPE_INVALID: Literal["invalid"] = "invalid"
StructuredListRegenerationReason = Literal[
    "accepted",
    "type",
    "arity",
    "item",
    "foreign_id",
    "render",
]


@dataclass(frozen=True)
class ExplicitTextShapeContract:
    """One code-proven contract eligible for a bounded shape regeneration."""

    kind: Literal["list", "single_sentence"]
    literal: str
    control: str
    count: int | None = None
    word_list: bool = False
    list_style: Literal["bullet", "numbered"] | None = None


_DIRECT_COMPOSITION = re.compile(
    r"^\s*(?:"
    r"ответь|сформируй|составь|дай|напиши|верни|сделай|оформи|подготовь|"
    r"включи|выдели|помести|отправь|создай|вставь|покажи|подчеркни|"
    r"передай|экранируй|"
    r"финальн\w*\s+доставк\w*\s+должн\w*|"
    r"answer|return|write|format|compose|create"
    r")\b",
    re.IGNORECASE,
)
_NAMED_LITERAL = re.compile(
    r"\b(?:маркер|идентификатор|токен|marker|identifier|token)\b"
    r"(?:\s+(?:со\s+значением|значением|долж\w*\s+быть|равн\w*|value))?"
    r"\s*(?:[:=—–-]\s*)?"
    r"(?P<literal>"
    r"`[^`\n]{1,160}`|«[^»\n]{1,160}»|“[^”\n]{1,160}”|"
    r"\"[^\"\n]{1,160}\"|'[^'\n]{1,160}'|[^\s,;.!?()]{1,160}"
    r")",
    re.IGNORECASE,
)
_PLAIN_LIST_TOKEN_PATTERN = r"[A-Za-zА-Яа-яЁё0-9](?:[A-Za-zА-Яа-яЁё0-9_.:-]{0,158}[A-Za-zА-Яа-яЁё0-9])?"
_PLAIN_LIST_TOKEN = re.compile(rf"^{_PLAIN_LIST_TOKEN_PATTERN}$")
_LITERAL_SUFFIX_LABEL_PATTERN = r"(?:маркер\w*|идентификатор\w*|токен\w*|marker|identifier|token)"
_CONTROL_SUFFIX_RU_LABEL_PATTERN = r"(?:контрол\w*|проверк\w*)"
_CONTROL_SUFFIX_EN_LABEL_PATTERN = r"(?:control|check)"
_LIST_SUFFIX_LABEL_PATTERN = (
    rf"(?:{_LITERAL_SUFFIX_LABEL_PATTERN}|"
    rf"{_CONTROL_SUFFIX_RU_LABEL_PATTERN}|{_CONTROL_SUFFIX_EN_LABEL_PATTERN})"
)
_LIST_SUFFIX_LABEL = re.compile(
    rf"^[ \t]*{_LIST_SUFFIX_LABEL_PATTERN}\b[ \t]*(?=[:=—–]|[ \t])",
    re.IGNORECASE,
)
_LIST_SUFFIX_LITERAL_METADATA = re.compile(
    rf"^[ \t]*{_LITERAL_SUFFIX_LABEL_PATTERN}[ \t]*(?:[:=—–][ \t]*|[ \t]+)"
    rf"(?P<token>{_PLAIN_LIST_TOKEN_PATTERN})[ \t]*$",
    re.IGNORECASE,
)
_LIST_SUFFIX_CONTROL_METADATA = re.compile(
    rf"^[ \t]*(?:"
    rf"{_CONTROL_SUFFIX_RU_LABEL_PATTERN}[ \t]*(?:[:=—–][ \t]*|[ \t]+)"
    rf"(?P<control_ru>{_PLAIN_LIST_TOKEN_PATTERN})|"
    rf"{_CONTROL_SUFFIX_EN_LABEL_PATTERN}[ \t]*[:=—–][ \t]*"
    rf"(?P<control_en>{_PLAIN_LIST_TOKEN_PATTERN})"
    rf")[ \t]*$",
    re.IGNORECASE,
)
_LIST_SUFFIX_PAIRED_METADATA = re.compile(
    rf"^[ \t]*{_LITERAL_SUFFIX_LABEL_PATTERN}"
    rf"[ \t]*[:=—–][ \t]*(?P<token>{_PLAIN_LIST_TOKEN_PATTERN})[ \t]*[,;][ \t]*(?:"
    rf"{_CONTROL_SUFFIX_RU_LABEL_PATTERN}[ \t]*(?:[:=—–][ \t]*|[ \t]+)"
    rf"(?P<control_ru>{_PLAIN_LIST_TOKEN_PATTERN})|"
    rf"{_CONTROL_SUFFIX_EN_LABEL_PATTERN}[ \t]*[:=—–][ \t]*"
    rf"(?P<control_en>{_PLAIN_LIST_TOKEN_PATTERN})"
    rf")[ \t]*\.?[ \t]*$",
    re.IGNORECASE,
)
_INLINE_WORD_LIST_EXTRA_PATTERN = r"[A-Za-zА-Яа-яЁё0-9](?:[A-Za-zА-Яа-яЁё0-9-]{0,62}[A-Za-zА-Яа-яЁё0-9])?"
_INLINE_WORD_LIST_LITERAL_LABEL_PATTERN = r"(?:маркер|метка|идентификатор|токен|marker|identifier|token)"
_INLINE_WORD_LIST_CONTROL_RU_LABEL_PATTERN = r"(?:контроль|проверка)"
_INLINE_WORD_LIST_MARKER_METADATA = re.compile(
    rf"^(?:(?P<extra>{_INLINE_WORD_LIST_EXTRA_PATTERN})[ \t]+[—–-][ \t]+)?"
    rf"{_INLINE_WORD_LIST_LITERAL_LABEL_PATTERN}\b[ \t]*(?:[:=—–][ \t]*|[ \t]+)"
    rf"(?:\[(?P<bracket_literal>{_PLAIN_LIST_TOKEN_PATTERN})\]|"
    rf"(?P<plain_literal>{_PLAIN_LIST_TOKEN_PATTERN}))"
    rf"(?:[,;][ \t]*|\.[ \t]+)(?:"
    rf"{_INLINE_WORD_LIST_CONTROL_RU_LABEL_PATTERN}\b[ \t]*(?:[:=—–][ \t]*|[ \t]+)"
    rf"(?P<control_ru>{_PLAIN_LIST_TOKEN_PATTERN})|"
    rf"{_CONTROL_SUFFIX_EN_LABEL_PATTERN}\b[ \t]*:[ \t]*"
    rf"(?P<control_en>{_PLAIN_LIST_TOKEN_PATTERN})"
    rf")[ \t]*\.?[ \t]*$",
    re.IGNORECASE,
)
_TRAILING_CONTROL_METADATA = re.compile(
    rf"(?:^|[.!?;]\s*)[ \t]*(?:"
    rf"(?:контроль|проверка)(?:[ \t]+(?:идентификатор|токен|маркер))?"
    rf"[ \t]*(?:[:=—–][ \t]*|[ \t]+)(?P<ru>{_PLAIN_LIST_TOKEN_PATTERN})|"
    rf"(?:control|check)(?:[ \t]+(?:id|identifier|token|marker))?"
    rf"[ \t]*[:=—–][ \t]*(?P<en>{_PLAIN_LIST_TOKEN_PATTERN})"
    rf")[.!?]?[ \t]*$",
    re.IGNORECASE,
)
_LIST_CUE = re.compile(r"\b(?:спис\w*|пункт\w*|элемент\w*|list|items?)\b", re.IGNORECASE)
_WORD_CUE = re.compile(r"\b(?:слов\w*|токен\w*|words?|tokens?)\b", re.IGNORECASE)
_QUOTE_CUE = re.compile(r"\b(?:цитат\w*|blockquote|quote)\b", re.IGNORECASE)
_EXPLANATION_CUE = re.compile(
    r"\b(?:пояснен\w*|поясн\w*|комментар\w*|объяснен\w*|explanation|comment)\b",
    re.IGNORECASE,
)
_QUOTE_EXPLANATION_NONAUTHORITY_CUE = re.compile(
    r"\b(?:необязател\w*|опциональн\w*|мож\w*|"
    r"(?:при|по)\s+желани\w*|вместо|отмен\w*|лучше|"
    r"optional|may|can|could|feel\s+free|instead|cancel\w*|rather)\b",
    re.IGNORECASE,
)
_QUOTE_EXPLANATION_META_CUE = re.compile(
    r"\b(?:термин\w*|упомян\w*|обсуд\w*|металингвист\w*|"
    r"terms?|mention\w*|discuss\w*|metalinguistic)\b",
    re.IGNORECASE,
)
_QUOTE_EXPLANATION_RU_CONTRACT = re.compile(
    r"(?:верни(?:те)?|сформируй(?:те)?|составь(?:те)?|напиши(?:те)?|"
    r"дай(?:те)?|подготовь(?:те)?|создай(?:те)?|оформи(?:те)?)[ \t]+"
    r"(?:(?:один|одна|одно|одну|одной|1)[ \t]+)?"
    r"(?:[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*[ \t]+){0,2}"
    r"(?:цитат\w*|блок-цитат\w*)[ \t]+и[ \t]+"
    r"(?:один|одна|одно|одну|одной|1)[ \t]+(?:"
    r"(?:отдельн\w*[ \t]+)?"
    r"строк\w*[ \t]+(?:пояснен\w*|комментар\w*|объяснен\w*)|"
    r"(?:пояснен\w*|комментар\w*|объяснен\w*)[ \t]+"
    r"(?:отдельн\w*[ \t]+)?строк\w*"
    r")[ \t]*\.[ \t]+(?:(?:пожалуйста|please)[ \t]*,?[ \t]+)?",
    re.IGNORECASE,
)
_QUOTE_EXPLANATION_EN_CONTRACT = re.compile(
    r"(?:return|write|compose|create|provide|format)[ \t]+"
    r"(?:(?:one|a[ \t]+single|single)[ \t]+)?"
    r"(?:[A-Za-z]+(?:-[A-Za-z]+)*[ \t]+){0,2}(?:quote|blockquote)[ \t]+and[ \t]+"
    r"(?:one|a(?:[ \t]+single)?|single)[ \t]+(?:"
    r"(?:separate[ \t]+)?explanation[ \t]+line|"
    r"(?:separate[ \t]+)?line[ \t]+of[ \t]+explanation"
    r")[ \t]*\.[ \t]+(?:(?:пожалуйста|please)[ \t]*,?[ \t]+)?",
    re.IGNORECASE,
)
_ANGLE_CUE = re.compile(
    r"(?:\bуглов\w*\s+скоб\w*\b|"
    r"\bсимвол\w*\b.{0,40}\bменьше\b.{0,40}\bбольше\b|"
    r"\bangle\s+brackets?\b|\bless[- ]than\b.{0,40}\bgreater[- ]than\b)",
    re.IGNORECASE | re.DOTALL,
)
_ONE_SENTENCE_CUE = re.compile(
    r"\b(?:одно|одну|одна|one)\b.{0,32}\b(?:предложен\w*|фраз\w*|строк\w*|sentence|phrase|line)\b",
    re.IGNORECASE,
)
_SINGLE_SENTENCE_CONTRACT = re.compile(
    r"\b(?:"
    r"(?:одно|единственное)\s+(?:[\w-]+\s+){0,3}предложение|"
    r"(?:одним|единственным)\s+(?:[\w-]+\s+){0,3}предложением|"
    r"(?:one|(?:a\s+)?single)\s+(?:[\w-]+\s+){0,3}sentence"
    r")\b",
    re.IGNORECASE,
)
_NON_SINGLE_SENTENCE_CONTRACT = re.compile(
    r"\b(?:"
    r"(?:2|3|4|5|6|7|8|9|10|два|две|двух|двумя|три|тр[её]х|четыре|пять|"
    r"шесть|семь|восемь|девять|десять|несколько|пара)"
    r"\s+(?:[\w-]+\s+){0,3}предложен\w*|"
    r"(?:two|three|four|five|six|seven|eight|nine|ten|multiple|several)"
    r"\s+(?:[\w-]+\s+){0,3}sentences?"
    r")\b",
    re.IGNORECASE,
)
_POST_SINGLE_SENTENCE_NEGATION = re.compile(
    r"^\s*[,;:—–-]?\s*(?:не\s+(?:надо|нужн\w*|следует|пиши|делай|треб\w*)|"
    r"отмен\w*|вместо\b|rather\s+than|not\s+(?:needed|required|wanted))",
    re.IGNORECASE,
)
_NEGATED_SINGLE_SENTENCE_PREFIX = re.compile(
    r"(?:\bне|\bбез|\bnot|\bwithout)(?:[\s,]+[\w-]+){0,6}\s*$",
    re.IGNORECASE,
)
_POST_SINGLE_SENTENCE_REVISION = re.compile(
    r"^.{0,64}\b(?:а|но|лучше|вместо|but|actually|rather|instead)\b.{0,40}"
    r"\b(?:2|3|4|5|6|7|8|9|10|два|две|три|четыре|пять|несколько|"
    r"two|three|four|five|multiple|several)\b",
    re.IGNORECASE,
)
_METALINGUISTIC_SENTENCE_MENTION = re.compile(
    r"\b(?:разбор|значени|определени|обсуждени|анализ)\w*\s+"
    r"(?:термин|фраз|выражени|словосочетани)\w*.{0,32}$",
    re.IGNORECASE,
)
_FACTUAL_SOURCE_CUE = re.compile(
    r"\b(?:файл\w*|вложен\w*|документ\w*|таблиц\w*|архив\w*|данн\w*|"
    r"факт\w*|источник\w*|запис\w*|file|attachment|document|table|archive|data|facts?|source)\b",
    re.IGNORECASE,
)
_RELATIONAL_SOURCE_CUE = re.compile(
    r"(?:"
    r"\b(?:из|по|согласно|на\s+основе|на\s+основании)\s+"
    r"(?:(?:эт\w*|данн\w*|мо\w*|наш\w*|пользовательск\w*|переданн\w*|"
    r"предоставленн\w*|присланн\w*|привед[её]нн\w*|вставленн\w*|"
    r"процитированн\w*|исходн\w*|следующ\w*)\s+){0,3}"
    r"(?:сообщени\w*|чат\w*|переписк\w*|корреспонденц\w*|письм\w*|"
    r"e[ -]?mail\w*|текст\w*|цитат\w*|реплик\w*|формулировк\w*)\b|"
    r"\b(?:from|according\s+to|based\s+on)\s+"
    r"(?:(?:this|that|the|my|our|user(?:'s)?|user[- ]supplied|supplied|provided|"
    r"given|pasted|quoted|original|following|above|below)\s+){0,3}"
    r"(?:messages?|chats?|conversations?|correspondence|e[- ]?mails?|texts?|"
    r"quotes?|citations?|wording|repl(?:y|ies))\b|"
    r"\b(?:переданн\w*|предоставленн\w*|присланн\w*|привед[её]нн\w*|"
    r"вставленн\w*|процитированн\w*|пользовательск\w*)\s+"
    r"(?:сообщени\w*|чат\w*|переписк\w*|корреспонденц\w*|письм\w*|"
    r"e[ -]?mail\w*|текст\w*|цитат\w*|реплик\w*|формулировк\w*)\b|"
    r"\b(?:user[- ]supplied|user[- ]provided|supplied|provided|given|pasted|quoted)\s+"
    r"(?:messages?|chats?|conversations?|correspondence|e[- ]?mails?|texts?|"
    r"quotes?|citations?|wording|repl(?:y|ies))\b|"
    r"\b(?:из|по|на\s+основе|from|based\s+on)\s*[:=—–-]?\s*[«“\"'`]"
    r")",
    re.IGNORECASE,
)
_NEGATED_SHAPE = re.compile(
    r"\b(?:не|без|not|without)\s+(?:спис\w*|пункт\w*|цитат\w*|углов\w*|"
    r"list|items?|quote|angle)\b",
    re.IGNORECASE,
)
_REPEAT_LITERAL_CUE = re.compile(
    r"\b(?:повтор\w*|дважды|два\s+раза|несколько\s+раз|repeat\w*|twice|two\s+times)\b",
    re.IGNORECASE,
)
_BOLD_REQUEST_CUE = re.compile(r"\b(?:жирн\w*|полужирн\w*|bold|strong)\b", re.IGNORECASE)
_ITALIC_REQUEST_CUE = re.compile(r"\b(?:курсив\w*|italic)\b", re.IGNORECASE)
_GENERIC_EMPHASIS_REQUEST_CUE = re.compile(
    r"\b(?:выдел\w*|подчерк\w*|emphasis|emphasize\w*|markdown|маркдаун\w*|форматирован\w*)\b",
    re.IGNORECASE,
)
_NEGATION_BEFORE_CUE = re.compile(
    r"(?:\bне|\bбез|\bnot|\bwithout)(?:\s+[\w-]+){0,2}\s*$",
    re.IGNORECASE,
)
_MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]*\]\([^\n)]*\)")
_SIMPLE_EMPHASIS = re.compile(
    r"(?P<bold_star>\*\*(?P<bold_star_text>[^\s*\n](?:[^*\n]*[^\s*\n])?)\*\*)|"
    r"(?P<bold_under>(?<!_)__(?!_)(?P<bold_under_text>[^\s_\n](?:[^_\n]*[^\s_\n])?)__(?!_))|"
    r"(?P<italic_star>(?<!\*)\*(?![\s*])(?P<italic_star_text>[^\s*\n](?:[^*\n]*[^\s*\n])?)\*(?!\*))|"
    r"(?P<italic_under>(?<![\w_])_(?![\s_])(?P<italic_under_text>[^\s_\n](?:[^_\n]*[^\s_\n])?)_(?![\w_]))"
)
_LIST_LINE = re.compile(r"^(?P<indent>[ \t]*)(?P<prefix>[-*+•]|\d{1,2}[.)])(?P<space>[ \t]+)(?P<value>\S.*)$")
_BULLET_COLON_LINE = re.compile(r"^(?P<indent>[ \t]*)(?P<prefix>[-*+•]):(?P<space>[ \t]+)(?P<value>\S.*)$")
_BULLET_LIST_CONTRACT = re.compile(
    r"\b(?:маркирован\w*\s+спис\w*|bullet(?:ed)?\s+list)\b",
    re.IGNORECASE,
)
_NUMBERED_LIST_CONTRACT = re.compile(
    r"\b(?:нумерован\w*\s+спис\w*|numbered\s+list)\b",
    re.IGNORECASE,
)
_AMPERSAND_LINE_CONTRACT = re.compile(
    r"(?:\bкоротк\w*\s+строк\w*\b[^.!?\n]{0,40}\b(?:с\s+)?амперсанд\w*\b|"
    r"\bshort\s+lines?\b[^.!?\n]{0,40}\b(?:with\s+(?:an?\s+)?)?ampersand\b)",
    re.IGNORECASE,
)
_STANDALONE_CONJUNCTION = re.compile(r"(?P<left>[ \t]+)(?P<carrier>и|and)(?P<right>[ \t]+)", re.IGNORECASE)
_QUOTED_ATOM = re.compile(
    r"(?P<open>«|“|\"|')(?P<target>[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{0,63})(?P<close>»|”|\"|')"
)
_ANSWER_QUOTED_FRAGMENT = re.compile(
    r"(?:«[^»\n]{1,256}»|“[^”\n]{1,256}”|\"[^\"\n]{1,256}\"|'[^'\n]{1,256}')"
)
_MARKER_LITERAL_REFUSAL_CUE = re.compile(
    r"\b(?:"
    r"не|без|никогда|запрещ\w*|исключ\w*|пропуст\w*|удал\w*|убер\w*|"
    r"игнор\w*|отброс\w*|not|without|never|ignore\w*|omit\w*|exclude\w*|"
    r"skip\w*|remove\w*|delete\w*|drop\w*|forbid\w*|don['’]t"
    r")\b",
    re.IGNORECASE,
)
_NAMED_LITERAL_INCLUDE_CUE = re.compile(
    r"\b(?:включи(?:те)?|добав(?:ь|ьте)|помест(?:и|ите)|встав(?:ь|ьте)|"
    r"include|add|append|insert|place)\b",
    re.IGNORECASE,
)
_ANSWER_LITERAL_LABEL = re.compile(
    r"\b(?:маркер\w*|метк\w*|идентификатор\w*|токен\w*|marker|label|identifier|token)\b",
    re.IGNORECASE,
)
_AMPERSAND_REFUSAL_CUE = re.compile(
    rf"(?:{_MARKER_LITERAL_REFUSAL_CUE.pattern}|\bavoid\w*\b)",
    re.IGNORECASE,
)
_HTML_TAG_NAMES = frozenset({"a", "b", "blockquote", "code", "em", "i", "pre", "s", "strong", "u"})
_DELIMITED_ATOM = re.compile(r"(?<![\w-])[A-Za-zА-Яа-яЁё0-9]+(?:[-_.:]+[A-Za-zА-Яа-яЁё0-9]+)+(?![\w-])")
_TERMINAL_SENTENCE_BOUNDARY = re.compile(r"[.!?…]+(?=\s|$)")
_COMMON_ABBREVIATION_PREFIX = re.compile(
    r"(?:\b(?:т\.\s*е|т\.\s*к|т\.\s*д|т\.\s*п|и\.\s*т\.\s*д|"
    r"и\.\s*т\.\s*п|e\.g|i\.e|etc|mr|mrs|ms|dr|prof|vs|ул|стр|рис|г|д|им|см|мин|сек)|"
    r"(?:\b[A-Za-zА-Яа-яЁё]\.){1,4}[A-Za-zА-Яа-яЁё])$",
    re.IGNORECASE,
)
# A lone letter before a dot may be an initial or alphabetic enumerator.  A
# lone digit at the end of ordinary prose (``... меньше 2.``) is not: numeric
# enumerators are already guarded by the bounded patterns below.  Treating the
# digit as an atom prevented an otherwise safe one-sentence repair.
_LONE_ATOM_PREFIX = re.compile(r"(?:^|\s)[A-Za-zА-Яа-яЁё]$")
_NUMERIC_ENUMERATOR_PREFIX = re.compile(r"(?:^|[:;]\s)\d{1,3}$")
_INLINE_NUMERIC_ENUMERATOR_PREFIX = re.compile(
    r"\b(?:вариант\w*|пункт\w*|шаг\w*|этап\w*|раздел\w*|пример\w*|опци\w*|"
    r"options?|items?|steps?|cases?|examples?)\s+\d{1,3}$",
    re.IGNORECASE,
)
_ROMAN_ENUMERATOR_PREFIX = re.compile(r"(?:^|\s)[IVXLCDM]{1,8}$")
_PAREN_ENUMERATOR_PREFIX = re.compile(r"(?:^|\s)\((?:\d{1,3}|[IVXLCDM]{1,8})\)$")
_SHORT_MEASURE_PREFIX = re.compile(r"\b\d+(?:[.,]\d+)?\s+[A-Za-zА-Яа-яЁё]{1,4}$")

_NUMBER_WORDS = {
    "один": 1,
    "одна": 1,
    "одно": 1,
    "одну": 1,
    "one": 1,
    "два": 2,
    "две": 2,
    "двух": 2,
    "two": 2,
    "три": 3,
    "трех": 3,
    "трёх": 3,
    "three": 3,
    "четыре": 4,
    "четырех": 4,
    "четырёх": 4,
    "four": 4,
    "пять": 5,
    "пяти": 5,
    "five": 5,
    "шесть": 6,
    "шести": 6,
    "six": 6,
    "семь": 7,
    "семи": 7,
    "seven": 7,
    "восемь": 8,
    "восьми": 8,
    "eight": 8,
    "девять": 9,
    "девяти": 9,
    "nine": 9,
    "десять": 10,
    "десяти": 10,
    "ten": 10,
}
_NUMBER_TOKEN = re.compile(
    r"(?<![\w-])(?:[1-9]|10)(?![\w-])|\b(?:" + "|".join(_NUMBER_WORDS) + r")\b",
    re.IGNORECASE,
)
_MISSING_LITERAL_RU_COUNT = (
    r"(?:[2-9]|10|два|две|двух|три|трех|трёх|четыре|четырех|четырёх|"
    r"пять|пяти|шесть|шести|семь|семи|восемь|восьми|девять|девяти|десять|десяти)"
)
_MISSING_LITERAL_EN_COUNT = r"(?:[2-9]|10|two|three|four|five|six|seven|eight|nine|ten)"
_MISSING_LITERAL_RU_WORD = r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*"
_MISSING_LITERAL_EN_WORD = r"[A-Za-z]+(?:-[A-Za-z]+)*"
_MISSING_LITERAL_ANY_WORD = rf"(?:{_MISSING_LITERAL_RU_WORD}|{_MISSING_LITERAL_EN_WORD})"
_MISSING_LITERAL_RU_LIST_VERB = (
    r"(?:ответь(?:те)?|дай(?:те)?|сформируй(?:те)?|составь(?:те)?|напиши(?:те)?|верни(?:те)?|"
    r"сделай(?:те)?|оформи(?:те)?|подготовь(?:те)?|создай(?:те)?)"
)
_MISSING_LITERAL_EN_LIST_VERB = r"(?:return|write|format|compose|create|prepare|provide)"
_MISSING_LITERAL_RU_ITEM = (
    r"(?:пункт(?:а|ов|ы)?|элемент(?:а|ов|ы)?|"
    r"слов(?:а|о|у)?|токен(?:а|ов|ы)?)"
)
_MISSING_LITERAL_EN_ITEM = r"(?:items?|words?|tokens?)"
_MISSING_LITERAL_UNSAFE_CONTRACT_CUE = re.compile(
    r"\b(?:не|без|если|когда|пока|услови\w*|только\s+при|чтобы|хотя|"
    r"поскольку|иначе|либо|или|игнор\w*|пропуст\w*|удал\w*|"
    r"not|without|if|when|while|unless|provided|assuming|depending|where|"
    r"because|although|whether|otherwise|or|ignore\w*|omit\w*|skip\w*|remove\w*)\b",
    re.IGNORECASE,
)
_REGENERABLE_SHAPE_UNSAFE_CUE = re.compile(
    r"\b(?:если|когда|пока|при\s+услови\w*|только\s+при|"
    r"вместо|отмен\w*|необязател\w*|опционал\w*|"
    r"if|when|while|unless|provided|assuming|depending|where\s+appropriate|"
    r"instead|rather|cancel\w*|optional)\b",
    re.IGNORECASE,
)
_REGENERABLE_ANSWER_REFUSAL_CUE = re.compile(
    r"\b(?:отказ\w*|отказыва\w*|не\s+(?:могу|буду|стану|добав\w*|включ\w*|пиш\w*)|"
    r"невозмож\w*|cannot|can't|won't|refus\w*|do\s+not\s+(?:add|include|write))\b",
    re.IGNORECASE,
)
_REGENERABLE_EFFECT_CUE = re.compile(
    r"\b(?:напоминан\w*|таймер\w*|будильник\w*|голосов\w*|аудио\w*|"
    r"интернет\w*|веб-поиск\w*|reminders?|timers?|alarms?|voice|audio|"
    r"internet|web[ -]?search)\b|"
    r"\b(?:поставь(?:те)?|создай(?:те)?|добавь(?:те)?)[ \t]+"
    r"(?:напоминан\w*|таймер\w*|будильник\w*)\b|"
    r"\b(?:отправь(?:те)?|удали(?:те)?|сохрани(?:те)?|"
    r"запусти(?:те)?[ \t]+(?:поиск|веб-поиск))\b|"
    r"\b(?:set|create|add)[ \t]+(?:a[ \t]+)?(?:reminder|timer|alarm)\b|"
    r"\b(?:send|delete|remove|save|search)[ \t]+(?:the[ \t]+)?\w+",
    re.IGNORECASE,
)
_REGENERABLE_REQUEST_UNSAFE_CUE = re.compile(
    r"\b(?:перепиш\w*|исправ\w*|редакт\w*|преобраз\w*|"
    r"отказ\w*|отказыва\w*|обсуд\w*|упомян\w*|термин\w*|"
    r"rewrite\w*|revise\w*|edit\w*|transform\w*|refus\w*|"
    r"discuss\w*|mention\w*|terms?)\b",
    re.IGNORECASE,
)
_MISSING_LITERAL_LIST_CONTRACT_PATTERNS = (
    re.compile(
        rf"{_MISSING_LITERAL_RU_LIST_VERB}[ \t]+(?P<count>{_MISSING_LITERAL_RU_COUNT})"
        rf"[ \t]+(?:{_MISSING_LITERAL_RU_WORD}[ \t]+){{0,2}}{_MISSING_LITERAL_RU_ITEM}"
        rf"(?:[ \t]+для[ \t]+{_MISSING_LITERAL_ANY_WORD}"
        rf"(?:[ \t]+{_MISSING_LITERAL_ANY_WORD}){{0,3}})?[ \t]*\.[ \t]+"
        rf"(?:(?:пожалуйста|please)[ \t]*,?[ \t]+)?",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_MISSING_LITERAL_RU_LIST_VERB}[ \t]+"
        rf"(?:{_MISSING_LITERAL_RU_WORD}[ \t]+){{0,2}}спис(?:ок|ком)[ \t]+из[ \t]+"
        rf"(?P<count>{_MISSING_LITERAL_RU_COUNT})[ \t]+"
        rf"(?:{_MISSING_LITERAL_RU_WORD}[ \t]+){{0,2}}{_MISSING_LITERAL_RU_ITEM}"
        rf"(?:[ \t]+для[ \t]+{_MISSING_LITERAL_ANY_WORD}"
        rf"(?:[ \t]+{_MISSING_LITERAL_ANY_WORD}){{0,3}})?[ \t]*\.[ \t]+"
        rf"(?:(?:пожалуйста|please)[ \t]*,?[ \t]+)?",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_MISSING_LITERAL_EN_LIST_VERB}[ \t]+(?P<count>{_MISSING_LITERAL_EN_COUNT})"
        rf"[ \t]+(?:{_MISSING_LITERAL_EN_WORD}[ \t]+){{0,2}}{_MISSING_LITERAL_EN_ITEM}"
        rf"(?:[ \t]+(?:for|to)[ \t]+{_MISSING_LITERAL_EN_WORD}"
        rf"(?:[ \t]+{_MISSING_LITERAL_EN_WORD}){{0,3}})?[ \t]*\.[ \t]+"
        rf"(?:(?:пожалуйста|please)[ \t]*,?[ \t]+)?",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_MISSING_LITERAL_EN_LIST_VERB}[ \t]+(?:(?:a|the)[ \t]+)?"
        rf"(?:{_MISSING_LITERAL_EN_WORD}[ \t]+){{0,2}}list[ \t]+of[ \t]+"
        rf"(?P<count>{_MISSING_LITERAL_EN_COUNT})[ \t]+"
        rf"(?:{_MISSING_LITERAL_EN_WORD}[ \t]+){{0,2}}{_MISSING_LITERAL_EN_ITEM}"
        rf"(?:[ \t]+(?:for|to)[ \t]+{_MISSING_LITERAL_EN_WORD}"
        rf"(?:[ \t]+{_MISSING_LITERAL_EN_WORD}){{0,3}})?[ \t]*\.[ \t]+"
        rf"(?:(?:пожалуйста|please)[ \t]*,?[ \t]+)?",
        re.IGNORECASE,
    ),
)
_REGENERABLE_RU_SENTENCE_PREFIX = re.compile(
    rf"(?:ответь|сформируй|составь|дай|напиши|верни|сделай|оформи|подготовь|"
    rf"создай|вставь|покажи)(?:те)?[ \t]+"
    rf"(?:(?:ответ|текст|фраз\w*|реплик\w*)[ \t]+)?(?:"
    rf"(?:одно|единственное)[ \t]+(?:{_MISSING_LITERAL_RU_WORD}[ \t]+){{0,2}}предложение|"
    rf"(?:одним|единственным)[ \t]+(?:{_MISSING_LITERAL_RU_WORD}[ \t]+){{0,2}}предложением"
    rf")"
    rf"(?:[ \t]+для[ \t]+{_MISSING_LITERAL_RU_WORD}"
    rf"(?:[ \t]+{_MISSING_LITERAL_RU_WORD}){{0,3}})?"
    rf"(?:[ \t]*,[ \t]*(?:(?:строго|только)[ \t]+)?"
    rf"(?:в[ \t]+)?одн\w*[ \t]+(?:строк\w*|абзац\w*)"
    rf"[ \t]*,?[ \t]*без[ \t]+(?:перенос\w*|разбиени\w*)|"
    rf"[ \t]*,[ \t]*{_MISSING_LITERAL_RU_WORD}"
    rf"(?:[ \t]+{_MISSING_LITERAL_RU_WORD}){{0,3}}"
    rf"[ \t]*,?[ \t]*без[ \t]+(?:перенос\w*|разбиени\w*))?"
    rf"[ \t]*\.[ \t]+",
    re.IGNORECASE,
)
_REGENERABLE_EN_SENTENCE_PREFIX = re.compile(
    rf"(?:answer|return|write|format|compose|create|provide)[ \t]+"
    rf"(?:(?:an?|the)[ \t]+)?(?:(?:answer|response|text|reply)[ \t]+)?(?:"
    rf"one[ \t]+(?:{_MISSING_LITERAL_EN_WORD}[ \t]+){{0,2}}sentence|"
    rf"(?:a[ \t]+)?single[ \t]+(?:{_MISSING_LITERAL_EN_WORD}[ \t]+){{0,2}}sentence"
    rf")"
    rf"(?:[ \t]+(?:for|to)[ \t]+{_MISSING_LITERAL_EN_WORD}"
    rf"(?:[ \t]+{_MISSING_LITERAL_EN_WORD}){{0,3}})?"
    rf"(?:[ \t]*,[ \t]*(?:strictly[ \t]+)?(?:in[ \t]+)?one[ \t]+line"
    rf"[ \t]*,?[ \t]*without[ \t]+(?:line[ \t]+)?breaks?|"
    rf"[ \t]*,[ \t]*{_MISSING_LITERAL_EN_WORD}"
    rf"(?:[ \t]+{_MISSING_LITERAL_EN_WORD}){{0,3}}"
    rf"[ \t]*,?[ \t]*without[ \t]+(?:line[ \t]+)?breaks?)?"
    rf"[ \t]*\.[ \t]+",
    re.IGNORECASE,
)


def _inside_reported_quote(text: str, position: int) -> bool:
    for opening, closing in (("«", "»"), ("“", "”"), ('"', '"'), ("'", "'")):
        left = text.rfind(opening, 0, position)
        if left < 0:
            continue
        right = text.find(closing, left + len(opening))
        if left < position < right:
            return True
    return False


def _unwrap_literal(raw: str) -> tuple[str, bool]:
    pairs = {"`": "`", "«": "»", "“": "”", '"': '"', "'": "'"}
    closing = pairs.get(raw[:1])
    quoted = bool(closing and raw.endswith(closing) and len(raw) > 2)
    return (raw[1:-1] if quoted else raw), quoted


def _single_named_literal(request: str) -> str | None:
    candidates: set[str] = set()
    for match in _NAMED_LITERAL.finditer(request):
        if _inside_reported_quote(request, match.start()):
            continue
        literal, quoted = _unwrap_literal(match.group("literal"))
        literal = literal.strip()
        if (
            not literal
            or len(literal) > 160
            or any(character.isspace() for character in literal)
            or any(character in literal for character in "<>&/\\")
            or not re.search(r"[A-Za-zА-Яа-яЁё0-9]", literal)
            or (not quoted and not re.search(r"[0-9_.:-]", literal))
            or request.count(literal) != 1
        ):
            continue
        candidates.add(literal)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _instruction_before_literal(request: str) -> str:
    match = _NAMED_LITERAL.search(request)
    return request[: match.start()] if match is not None else request


def _requested_list_size(request: str) -> int | None:
    request = _instruction_before_literal(request)
    sizes: set[int] = set()
    for cue in _LIST_CUE.finditer(request):
        window = request[max(0, cue.start() - 72) : min(len(request), cue.end() + 72)]
        for match in _NUMBER_TOKEN.finditer(window):
            token = match.group(0).casefold()
            sizes.add(int(token) if token.isdigit() else _NUMBER_WORDS[token])
    return next(iter(sizes)) if len(sizes) == 1 else None


def _is_word_list(request: str) -> bool:
    request = _instruction_before_literal(request)
    list_matches = list(_LIST_CUE.finditer(request))
    word_matches = list(_WORD_CUE.finditer(request))
    for list_match in list_matches:
        for word_match in word_matches:
            left = min(list_match.end(), word_match.end())
            right = max(list_match.start(), word_match.start())
            if right - left <= 72 and not re.search(r"[.!?\n]", request[left:right]):
                return True
    return False


def _enumerates_word_values(request: str) -> bool:
    """Conservatively detect a user-owned value set that must not lose an item."""

    before_literal = _NAMED_LITERAL.split(request, maxsplit=1)[0]
    if re.search(r":\s*[^:;\n]+(?:,|;)\s*[^:;\n]+", before_literal):
        return True
    quoted_values = re.findall(r"(?:«[^»\n]+»|“[^”\n]+”|\"[^\"\n]+\"|'[^'\n]+')", before_literal)
    return len(quoted_values) >= 2


def _literal_occurrences(text: str, literal: str) -> list[re.Match[str]]:
    return list(re.finditer(rf"(?<![\w-]){re.escape(literal)}(?![\w-])", text))


def _has_source_authority(request: str) -> bool:
    """Return whether list/prose bytes come from user-owned source material."""

    return bool(_FACTUAL_SOURCE_CUE.search(request) or _RELATIONAL_SOURCE_CUE.search(request))


def _trailing_control_metadata_token(request: str) -> str | None:
    """Return one exact trailing, noun-labelled control token.

    Russian noun forms are unambiguous without punctuation.  English ``check``
    may be an imperative, so its metadata form requires an explicit separator.
    The token must occur exactly once in the request; a repeated or quoted
    control is not authority to discard a second suffix line.
    """

    match = _TRAILING_CONTROL_METADATA.search(request)
    if match is None or _inside_reported_quote(request, match.start()):
        return None
    token = str(match.group("ru") or match.group("en") or "")
    if not token or len(_literal_occurrences(request, token)) != 1:
        return None
    return token


def strip_parser_control_metadata(request: str, answer: str) -> str:
    """Remove a parser-only control atom from a model draft.

    A closed composition request uses one trailing ``Control``/``Контроль``
    atom to make the request itself unambiguous.  That atom is correlation
    metadata, not requested answer content.  If a model echoes it, remove only
    the exact parser-proven atom (and its optional noun label) before any
    repair, regeneration, or fallback decision.  Ordinary words such as
    ``control`` are never touched.
    """

    if not isinstance(request, str) or not isinstance(answer, str) or not answer:
        return answer
    contract = regenerable_text_shape_contract(request)
    if contract is not None:
        control = contract.control
    else:
        quote_contract = _closed_quote_explanation_contract(request)
        if quote_contract is None:
            return answer
        _literal, control = quote_contract
    control_label = (
        r"(?:(?:контроль|проверка)(?:[ \t]+(?:идентификатор|токен|маркер))?|"
        r"(?:control|check)(?:[ \t]+(?:id|identifier|token|marker))?)"
    )
    labelled_control_pattern = re.compile(
        rf"(?<![\w-]){control_label}"
        rf"(?:[ \t]*[:=—–-][ \t]*|[ \t]+)"
        rf"{re.escape(control)}(?![\w-])[ \t]*(?P<punct>[.!?])?",
        re.IGNORECASE,
    )
    bare_control_pattern = re.compile(
        rf"(?<![\w-]){re.escape(control)}(?![\w-])",
        re.IGNORECASE,
    )
    if labelled_control_pattern.search(answer) is None and bare_control_pattern.search(answer) is None:
        return answer

    def strip_line(body: str) -> tuple[str, bool, bool]:
        labelled = list(labelled_control_pattern.finditer(body))
        labelled_spans = [(match.start(), match.end()) for match in labelled]
        bare = [
            match
            for match in bare_control_pattern.finditer(body)
            if not any(start <= match.start() and match.end() <= end for start, end in labelled_spans)
        ]
        edits: list[tuple[int, int, str]] = []
        for match in labelled:
            left = body[: match.start()].rstrip(" \t")
            punctuation = str(match.group("punct") or "")
            replacement = "" if left.endswith((".", "!", "?")) else punctuation
            edits.append((match.start(), match.end(), replacement))
        edits.extend((match.start(), match.end(), "") for match in bare)
        if not edits:
            return body, False, False
        expanded: list[tuple[int, int, str]] = []
        for start, end, replacement in edits:
            while start > 0 and body[start - 1] in " \t":
                start -= 1
            if start == 0:
                while end < len(body) and body[end] in " \t":
                    end += 1
            expanded.append((start, end, replacement))
        candidate = body
        for start, end, replacement in sorted(expanded, reverse=True):
            candidate = f"{candidate[:start]}{replacement}{candidate[end:]}"
        metadata_only = bool(
            re.fullmatch(
                r"[ \t]*(?:(?:[-*+•]|\d{1,2}[.)])[ \t]*)?[.,;:!?—–-]*[ \t]*",
                candidate,
            )
        )
        return ("" if metadata_only else candidate), True, metadata_only

    parts = answer.splitlines(keepends=True)
    rendered: list[str] = []
    for index, part in enumerate(parts):
        ending_match = re.search(r"(?:\r\n|\n|\r)$", part)
        ending = ending_match.group(0) if ending_match is not None else ""
        body = part[: -len(ending)] if ending else part
        stripped, matched, metadata_only = strip_line(body)
        if matched and metadata_only:
            if index == len(parts) - 1 and rendered:
                rendered[-1] = re.sub(r"(?:\r\n|\n|\r)$", "", rendered[-1])
            continue
        rendered.append(f"{stripped}{ending}")
    return "".join(rendered)


def _has_positive_cue(request: str, pattern: re.Pattern[str]) -> bool:
    for match in pattern.finditer(request):
        prefix = request[max(0, match.start() - 48) : match.start()]
        if not _NEGATION_BEFORE_CUE.search(prefix):
            return True
    return False


def _requests_literal_repetition(request: str) -> bool:
    return _has_positive_cue(request, _REPEAT_LITERAL_CUE)


def _request_refuses_named_literal(request: str, literal: str) -> bool:
    matches = [
        match
        for match in _NAMED_LITERAL.finditer(request)
        if _unwrap_literal(str(match.group("literal")))[0].strip() == literal
        and not _inside_reported_quote(request, match.start())
    ]
    if len(matches) != 1:
        return True
    match = matches[0]
    window = request[max(0, match.start() - 160) : min(len(request), match.end() + 160)]
    return _MARKER_LITERAL_REFUSAL_CUE.search(window) is not None


def _closed_missing_literal_contract_count(prefix: str) -> int | None:
    """Parse one byte-zero, period-terminated list-composition clause."""

    if _MISSING_LITERAL_UNSAFE_CONTRACT_CUE.search(prefix):
        return None
    matches = [pattern.fullmatch(prefix) for pattern in _MISSING_LITERAL_LIST_CONTRACT_PATTERNS]
    matched = [match for match in matches if match is not None]
    if len(matched) != 1:
        return None
    token = str(matched[0].group("count")).casefold()
    count = int(token) if token.isdigit() else _NUMBER_WORDS.get(token)
    return count if count is not None and 2 <= count <= 10 else None


def _request_explicitly_includes_named_literal(
    request: str,
    literal: str,
    control: str,
    expected: int,
) -> bool:
    """Require a closed list and literal contract with trailing parser metadata."""

    matches = [
        match
        for match in _NAMED_LITERAL.finditer(request)
        if _unwrap_literal(str(match.group("literal")))[0].strip() == literal
        and not _inside_reported_quote(request, match.start())
    ]
    if len(matches) != 1:
        return False
    literal_match = matches[0]
    prefix = request[: literal_match.start()]
    cues = [
        cue
        for cue in _NAMED_LITERAL_INCLUDE_CUE.finditer(prefix)
        if not _inside_reported_quote(request, cue.start())
    ]
    if len(cues) != 1:
        return False
    cue = cues[0]
    before_cue = prefix[: cue.start()]
    between = prefix[cue.end() :]
    control_match = _TRAILING_CONTROL_METADATA.search(request)
    if control_match is None or _inside_reported_quote(request, control_match.start()):
        return False
    observed_control = str(control_match.group("ru") or control_match.group("en") or "")
    between_literal_and_control = request[literal_match.end() : control_match.start()]
    control_boundary = request[control_match.start() : control_match.start() + 1]
    control_clause = control_match.group(0)
    return bool(
        observed_control == control
        and _closed_missing_literal_contract_count(before_cue) == expected
        and re.fullmatch(r"[ \t]+(?:the[ \t]+)?", between, re.IGNORECASE)
        and re.fullmatch(r"[ \t]*", between_literal_and_control)
        and control_boundary == "."
        and "\n" not in control_clause
        and "\r" not in control_clause
        and request.rstrip(" \t").endswith(".")
    )


def _closed_single_sentence_regeneration_prefix(prefix: str) -> bool:
    """Recognise one direct, declarative sentence-composition clause."""

    if (
        not prefix
        or len(prefix) > 320
        or "\n" in prefix
        or "\r" in prefix
        or prefix.count(".") != 1
        or not re.fullmatch(r"[^.!?;]{1,300}\.[ \t]+", prefix)
        or _REGENERABLE_SHAPE_UNSAFE_CUE.search(prefix)
    ):
        return False
    return bool(
        _REGENERABLE_RU_SENTENCE_PREFIX.fullmatch(prefix) or _REGENERABLE_EN_SENTENCE_PREFIX.fullmatch(prefix)
    )


def regenerable_text_shape_contract(request: str) -> ExplicitTextShapeContract | None:
    """Parse only whole, unconditional contracts safe for one model retry.

    The parser deliberately owns fewer requests than the deterministic repair
    surface.  A retry can rewrite prose, so its authority must come from the
    complete request rather than from nearby cue co-occurrence.
    """

    if (
        not isinstance(request, str)
        or not request
        or len(request) > _MAX_REQUEST_CHARS
        or "\n" in request
        or "\r" in request
        or _DIRECT_COMPOSITION.match(request) is None
        or _has_source_authority(request)
        or _enumerates_word_values(request)
        or _requests_literal_repetition(request)
        or _NEGATED_SHAPE.search(request)
        or _QUOTE_CUE.search(request)
        or _EXPLANATION_CUE.search(request)
        or _REGENERABLE_SHAPE_UNSAFE_CUE.search(request)
        or _REGENERABLE_EFFECT_CUE.search(request)
        or _REGENERABLE_REQUEST_UNSAFE_CUE.search(request)
        or any(character in request for character in "`<>[]{}")
    ):
        return None
    literal = _single_named_literal(request)
    control = _trailing_control_metadata_token(request)
    if (
        literal is None
        or control is None
        or literal.casefold() == control.casefold()
        or control.casefold() in literal.casefold()
        or not _safe_regeneration_identifier(literal)
        or not _safe_regeneration_identifier(control)
        or len(_literal_occurrences(request, literal)) != 1
        or len(_literal_occurrences(request, control)) != 1
    ):
        return None
    named_matches = [
        match
        for match in _NAMED_LITERAL.finditer(request)
        if _unwrap_literal(str(match.group("literal")))[0].strip() == literal
        and not _inside_reported_quote(request, match.start())
    ]
    if len(named_matches) != 1:
        return None
    named = named_matches[0]
    cues = [
        cue
        for cue in _NAMED_LITERAL_INCLUDE_CUE.finditer(request[: named.start()])
        if not _inside_reported_quote(request, cue.start())
    ]
    control_match = _TRAILING_CONTROL_METADATA.search(request)
    if len(cues) != 1 or control_match is None or _inside_reported_quote(request, control_match.start()):
        return None
    cue = cues[0]
    observed_control = str(control_match.group("ru") or control_match.group("en") or "")
    if not (
        observed_control == control
        and re.fullmatch(r"[ \t]+(?:the[ \t]+)?", request[cue.end() : named.start()], re.IGNORECASE)
        and control_match.start() == named.end()
        and request[control_match.start() : control_match.start() + 1] == "."
        and request.rstrip(" \t").endswith(".")
    ):
        return None
    prefix = request[: cue.start()]
    expected = _closed_missing_literal_contract_count(prefix)
    if expected is not None:
        bullet_style = _BULLET_LIST_CONTRACT.search(request) is not None
        numbered_style = _NUMBERED_LIST_CONTRACT.search(request) is not None
        if bullet_style and numbered_style:
            return None
        return ExplicitTextShapeContract(
            kind="list",
            literal=literal,
            control=control,
            count=expected,
            word_list=_is_word_list(request),
            list_style="numbered" if numbered_style else "bullet",
        )
    if _closed_single_sentence_regeneration_prefix(prefix):
        return ExplicitTextShapeContract(
            kind="single_sentence",
            literal=literal,
            control=control,
        )
    return None


def _safe_regeneration_identifier(value: str) -> bool:
    """Require an unmistakable synthetic/control atom, not an ordinary number."""

    return bool(
        3 <= len(value) <= 96
        and _PLAIN_LIST_TOKEN.fullmatch(value)
        and re.search(r"[A-Za-zА-Яа-яЁё]", value)
        and re.search(r"\d", value)
        and re.search(r"[-_.:]", value)
    )


def owns_regenerable_text_shape(request: str) -> bool:
    """Whether ``request`` is a complete contract eligible for regeneration."""

    return regenerable_text_shape_contract(request) is not None


def owns_closed_text_shape(request: str) -> bool:
    """Whether current text alone proves a side-effect-free shape contract."""

    return bool(
        regenerable_text_shape_contract(request) is not None
        or _closed_quote_explanation_contract(request) is not None
    )


def _exact_answer_identifiers(contract: ExplicitTextShapeContract, answer: str) -> bool:
    literal_exact = bool(
        len(_literal_occurrences(answer, contract.literal)) == 1
        and answer.casefold().count(contract.literal.casefold()) == 1
    )
    control_absent = bool(
        not _literal_occurrences(answer, contract.control)
        and contract.control.casefold() not in answer.casefold()
    )
    # ``Include marker ...`` is the user's output instruction.  The trailing
    # noun-labelled ``Control ...`` token closes the parser contract, but does
    # not itself ask to be echoed.  Treating it as answer content made natural
    # dense-model outputs fail and could regenerate a correct answer merely to
    # expose correlation metadata.
    return bool(literal_exact and control_absent)


def _answer_refuses_exact_literal(answer: str, literal: str) -> bool:
    """Reject a refusal/negative claim bound to the requested literal itself."""

    marker = re.escape(literal)
    if (
        re.search(
            rf"\b(?:без|не|without|not)\b\s+{marker}\b",
            answer,
            re.IGNORECASE,
        )
        or re.search(
            rf"\b(?:без|не|никогда|without|not|never)\b"
            rf"[^.!?\n]{{0,20}}\b(?:маркер|метк|идентификатор|marker|label|identifier)\w*"
            rf"[^.!?\n]{{0,20}}{marker}\b",
            answer,
            re.IGNORECASE,
        )
        or re.search(
            rf"\b(?:не|никогда|not|never)\b\s+"
            rf"(?:включ|добав|использ|вывед|покаж|верн|include|add|use|output|show|return)\w*"
            rf"[^.!?\n]{{0,24}}{marker}\b",
            answer,
            re.IGNORECASE,
        )
    ):
        return True

    # Dense models naturally use contrastive phrases such as “is not invalid”
    # or “correct rather than wrong”.  A nearby negative adjective is a refusal
    # only when it is asserted, not when that adjective is itself negated.
    for judgment in re.finditer(
        rf"{marker}(?P<prefix>[^.!?\n]{{0,40}}?)"
        rf"\b(?:неверн|ложн|ошибочн|wrong|false|invalid)\w*\b",
        answer,
        re.IGNORECASE,
    ):
        prefix = str(judgment.group("prefix"))
        judgment_is_negated = bool(
            re.search(
                r"(?:"
                r"\bnot(?:[ \t]+(?:actually|really|necessarily|considered|deemed|at[ \t]+all))?"
                r"|\b(?:isn['’]t|aren['’]t|wasn['’]t|weren['’]t)"
                r"|\bне(?:[ \t]+(?:является|считается|обязательно|"
                r"был(?:а|о|и)?[ \t]+признан(?:а|о|ы)?))?"
                r"|\brather[ \t]+than"
                r"|\bneither"
                r")[ \t]*$",
                prefix,
                re.IGNORECASE,
            )
            or (
                re.search(r"\bnor[ \t]*$", prefix, re.IGNORECASE) is not None
                and re.search(r"\bneither\b", prefix, re.IGNORECASE) is not None
            )
        )
        if judgment_is_negated:
            continue
        return True
    return False


def _exact_regenerable_list_is_valid(contract: ExplicitTextShapeContract, answer: str) -> bool:
    expected = contract.count
    if expected is None or "\r" in answer or "\n".join(answer.splitlines()) != answer:
        return False
    lines = answer.splitlines()
    if len(lines) != expected or any(not line.strip() for line in lines):
        return False
    raw_matches = [_LIST_LINE.fullmatch(line) for line in lines]
    if any(match is None for match in raw_matches):
        return False
    matches = [match for match in raw_matches if match is not None]
    if len({(match.group("indent"), match.group("space")) for match in matches}) != 1:
        return False
    prefixes = [match.group("prefix") for match in matches]
    numbered = [prefix[0].isdigit() for prefix in prefixes]
    if any(numbered) and not all(numbered):
        return False
    if all(numbered):
        if len({prefix[-1] for prefix in prefixes}) != 1 or [prefix[:-1] for prefix in prefixes] != [
            str(index) for index in range(1, expected + 1)
        ]:
            return False
    elif len(set(prefixes)) != 1:
        return False
    values = [str(match.group("value")) for match in matches]
    if contract.word_list and any(_PLAIN_LIST_TOKEN.fullmatch(value) is None for value in values):
        return False
    if contract.word_list and len({value.casefold() for value in values}) != len(values):
        return False
    if any(re.search(r"[A-Za-zА-Яа-яЁё0-9]", value) is None for value in values):
        return False
    if _REGENERABLE_ANSWER_REFUSAL_CUE.search(answer):
        return False
    if not contract.word_list:
        for value in values:
            without_identifiers = re.sub(
                rf"(?<![\w-]){re.escape(contract.literal)}(?![\w-])",
                " ",
                value,
            )
            if re.search(r"[A-Za-zА-Яа-яЁё]{2,}", without_identifiers) is None:
                return False
    literal_rows = sum(bool(_literal_occurrences(value, contract.literal)) for value in values)
    control_rows = sum(bool(_literal_occurrences(value, contract.control)) for value in values)
    if literal_rows != 1 or control_rows != 0:
        return False
    if contract.word_list and any(
        _safe_regeneration_identifier(value) and value != contract.literal for value in values
    ):
        return False
    return not bool(
        any(character in answer for character in '`<>~[]«»“”"')
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
        or _ANSWER_QUOTED_FRAGMENT.search(answer)
        or re.search(r"https?://|\bwww\.", answer, re.IGNORECASE)
        or re.search(r"(?m)^\s*(?:#{1,6}|>)\s+", answer)
    )


def _exact_regenerable_sentence_is_valid(contract: ExplicitTextShapeContract, answer: str) -> bool:
    if (
        "\n" in answer
        or "\r" in answer
        or answer.strip() != answer
        or len(_TERMINAL_SENTENCE_BOUNDARY.findall(answer)) > 1
        or _answer_refuses_exact_literal(answer, contract.literal)
        or _REGENERABLE_ANSWER_REFUSAL_CUE.search(answer)
        or any(character in answer for character in '`<>~[]«»“”"')
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
        or _ANSWER_QUOTED_FRAGMENT.search(answer)
        or re.search(r"https?://|\bwww\.", answer, re.IGNORECASE)
        or re.match(r"\s*(?:#{1,6}\s|>\s|[-+*•]\s|\d{1,2}[.)]\s|\|)", answer)
    ):
        return False
    without_identifiers = re.sub(
        rf"(?<![\w-]){re.escape(contract.literal)}(?![\w-])",
        " ",
        answer,
    )
    return re.search(r"[A-Za-zА-Яа-яЁё]{2,}", without_identifiers) is not None


def explicit_text_shape_status(
    request: str,
    answer: str,
) -> Literal["unowned", "valid", "invalid"]:
    """Return the closed regeneration status for one request/answer pair."""

    contract = regenerable_text_shape_contract(request)
    if contract is None:
        return TEXT_SHAPE_UNOWNED
    if (
        not isinstance(answer, str)
        or not answer
        or len(answer) > _MAX_ANSWER_CHARS
        or not _exact_answer_identifiers(contract, answer)
    ):
        return TEXT_SHAPE_INVALID
    valid = (
        _exact_regenerable_list_is_valid(contract, answer)
        if contract.kind == "list"
        else _exact_regenerable_sentence_is_valid(contract, answer)
    )
    return TEXT_SHAPE_VALID if valid else TEXT_SHAPE_INVALID


def _single_emphasis_target(request: str) -> str | None:
    """Extract one explicit, locally bound quoted word to emphasize."""

    cue_pattern = re.compile(
        r"\b(?:выдел\w*|подчерк\w*|жирн\w*|полужирн\w*|курсив\w*|"
        r"emphasis|emphasize\w*|bold|italic)\b",
        re.IGNORECASE,
    )
    pairs = {"«": "»", "“": "”", '"': '"', "'": "'"}
    candidates: set[str] = set()
    for quoted in _QUOTED_ATOM.finditer(request):
        if pairs.get(str(quoted.group("open"))) != str(quoted.group("close")):
            continue
        if _inside_reported_quote(request, quoted.start()):
            continue
        window_start = max(0, quoted.start() - 96)
        prefix = request[window_start : quoted.start()]
        cues = list(cue_pattern.finditer(prefix))
        if not cues:
            continue
        cue = cues[-1]
        between = prefix[cue.end() :]
        if re.search(r"[.!?\n]", between):
            continue
        cue_position = window_start + cue.start()
        if _inside_reported_quote(request, cue_position):
            continue
        before_cue = request[max(0, cue_position - 48) : cue_position]
        if _NEGATION_BEFORE_CUE.search(before_cue):
            continue
        candidates.add(str(quoted.group("target")))
    return next(iter(candidates)) if len(candidates) == 1 else None


def _dedupe_named_literal(request: str, answer: str, literal: str) -> str:
    occurrences = _literal_occurrences(answer, literal)
    if len(occurrences) < 2 or _requests_literal_repetition(request):
        return answer

    sentinel = "\x00"
    if sentinel in answer:
        return answer
    seen = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal seen
        seen += 1
        return match.group(0) if seen == 1 else sentinel

    result = re.sub(rf"(?<![\w-]){re.escape(literal)}(?![\w-])", replace, answer)
    while sentinel in result:
        position = result.index(sentinel)
        line_start = result.rfind("\n", 0, position) + 1
        line_end = result.find("\n", position + 1)
        line_end = len(result) if line_end < 0 else line_end
        if not result[line_start:position].strip() and not result[position + 1 : line_end].strip():
            if line_end < len(result):
                result = f"{result[:line_start]}{result[line_end + 1 :]}"
            elif line_start:
                result = result[: line_start - 1]
            else:
                result = ""
            continue
        left = position
        while left > 0 and result[left - 1] in " \t":
            left -= 1
        right = position + 1
        while right < len(result) and result[right] in " \t":
            right += 1
        previous = result[left - 1] if left else "\n"
        following = result[right] if right < len(result) else "\n"
        joiner = "" if following in ",.;:!?…" or previous in "\n([{<«“\"'" or following == "\n" else " "
        result = f"{result[:left]}{joiner}{result[right:]}"
    return result


def _parsed_exact_list(request: str, answer: str) -> tuple[int, list[re.Match[str]]] | None:
    expected = _requested_list_size(request)
    lines = answer.splitlines()
    if expected is None or len(lines) != expected or any(not line.strip() for line in lines):
        return None
    parsed = [_LIST_LINE.fullmatch(line) for line in lines]
    if any(match is None for match in parsed):
        return None
    matches = [match for match in parsed if match is not None]
    numbered = [match.group("prefix")[0].isdigit() for match in matches]
    if any(numbered) and not all(numbered):
        return None
    if all(numbered):
        values = [int(match.group("prefix")[:-1]) for match in matches]
        if values != list(range(1, expected + 1)):
            return None
    return expected, matches


def _structured_regeneration_forbidden_identifiers(
    request: str,
    source_text: str,
    contract: ExplicitTextShapeContract,
) -> frozenset[str]:
    def identifiers(text: str) -> set[str]:
        return {
            match.group(0).casefold()
            for match in _DELIMITED_ATOM.finditer(text)
            if _safe_regeneration_identifier(match.group(0))
        }

    request_identifiers = identifiers(request)
    forbidden_identifiers = identifiers(source_text) - request_identifiers
    forbidden_identifiers.update((contract.literal.casefold(), contract.control.casefold()))
    return frozenset(forbidden_identifiers)


def render_structured_list_regeneration_result(
    request: str,
    contract: ExplicitTextShapeContract,
    items: object,
    *,
    source_text: str = "",
) -> tuple[str, StructuredListRegenerationReason]:
    """Validate semantic items and return a code-owned list plus a safe reason."""

    count = contract.count
    semantic_count = count - 1 if contract.word_list and count is not None else count
    if contract.kind != "list" or count is None or semantic_count is None:
        return "", "render"
    if type(items) is not list or any(type(item) is not str for item in items):
        return "", "type"
    if len(items) != semantic_count:
        return "", "arity"
    forbidden_identifiers = _structured_regeneration_forbidden_identifiers(
        request,
        source_text,
        contract,
    )
    values: list[str] = []
    for item in items:
        if not item or item.strip() != item:
            return "", "item"
        try:
            item.encode("utf-8")
        except UnicodeEncodeError:
            return "", "item"
        if (
            contract.literal.casefold() in item.casefold()
            or contract.control.casefold() in item.casefold()
            or any(
                match.group(0).casefold() in forbidden_identifiers for match in _DELIMITED_ATOM.finditer(item)
            )
        ):
            return "", "foreign_id"
        if (
            len(item) > 1_000
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F or character in "\u0085\u2028\u2029"
                for character in item
            )
            or re.match(r"\s*(?:[-+*\u2022]\s|\d{1,2}[.)]\s|#{1,6}\s|>\s)", item)
            or any(character in item for character in "{}")
        ):
            return "", "item"
        if contract.word_list and _PLAIN_LIST_TOKEN.fullmatch(item) is None:
            return "", "item"
        values.append(item)
    if contract.word_list:
        values.insert(0, contract.literal)
    else:
        values[0] = f"{values[0]} {contract.literal}"
    if len({value.casefold() for value in values}) != len(values):
        return "", "item"
    if contract.list_style not in {"bullet", "numbered"}:
        return "", "render"
    rendered = "\n".join(
        f"{index}. {value}" if contract.list_style == "numbered" else f"- {value}"
        for index, value in enumerate(values, start=1)
    )
    if explicit_text_shape_status(request, rendered) != TEXT_SHAPE_VALID:
        return "", "render"
    return (
        (rendered, "accepted")
        if repair_explicit_text_shape(request, rendered) == rendered
        else ("", "render")
    )


def render_structured_list_regeneration(
    request: str,
    contract: ExplicitTextShapeContract,
    items: object,
    *,
    source_text: str = "",
) -> str:
    """Render strict semantic JSON items into a code-owned list scaffold."""

    return render_structured_list_regeneration_result(
        request,
        contract,
        items,
        source_text=source_text,
    )[0]


def _plain_homogeneous_word_list(
    lines: list[str],
    expected: int,
) -> list[re.Match[str]] | None:
    """Parse exactly N one-token rows with one byte-visible list layout."""

    if not (2 <= expected <= 10) or len(lines) != expected:
        return None
    parsed = [_LIST_LINE.fullmatch(line) for line in lines]
    if any(match is None for match in parsed):
        return None
    matches = [match for match in parsed if match is not None]
    if len({(match.group("indent"), match.group("space")) for match in matches}) != 1:
        return None
    prefixes = [match.group("prefix") for match in matches]
    numbered = [prefix[0].isdigit() for prefix in prefixes]
    if any(numbered) and not all(numbered):
        return None
    if all(numbered):
        closing = {prefix[-1] for prefix in prefixes}
        if len(closing) != 1 or [prefix[:-1] for prefix in prefixes] != [
            str(index) for index in range(1, expected + 1)
        ]:
            return None
    elif len(set(prefixes)) != 1:
        return None
    values = [match.group("value") for match in matches]
    if any(_PLAIN_LIST_TOKEN.fullmatch(value) is None for value in values):
        return None
    if len({value.casefold() for value in values}) != expected:
        return None
    return matches


def _repair_word_list_suffix_block(
    request: str,
    answer: str,
    literal: str,
) -> tuple[bool, str]:
    """Close one exact word list followed only by bounded metadata.

    ``owned`` distinguishes a malformed instance of this narrow shape from an
    unrelated answer.  Once the suffix is recognisably metadata, every failed
    guard returns the original bytes and prevents broader literal dedupe from
    accidentally turning the malformed block into an authorised repair.
    """

    expected = _requested_list_size(request)
    if expected is None or not (2 <= expected <= 10) or not _is_word_list(request):
        return False, answer
    raw_lines = answer.splitlines()
    if len(raw_lines) <= expected:
        return False, answer
    list_lines = raw_lines[:expected]
    if any(_LIST_LINE.fullmatch(line) is None for line in list_lines):
        return False, answer
    raw_suffix = raw_lines[expected:]
    nonblank_suffix = [line for line in raw_suffix if line.strip()]
    if not nonblank_suffix or not any(_LIST_SUFFIX_LABEL.match(line) for line in nonblank_suffix):
        return False, answer
    suffix = raw_suffix[1:] if raw_suffix and not raw_suffix[0].strip() else raw_suffix
    if not (1 <= len(suffix) <= 2) or any(not line.strip() for line in suffix):
        return True, answer
    literal_suffix_matches = [_LIST_SUFFIX_LITERAL_METADATA.fullmatch(line) for line in suffix]
    control_suffix_matches = [_LIST_SUFFIX_CONTROL_METADATA.fullmatch(line) for line in suffix]
    paired_suffix = _LIST_SUFFIX_PAIRED_METADATA.fullmatch(suffix[0]) if len(suffix) == 1 else None
    if (
        any(
            literal_match is None and control_match is None
            for literal_match, control_match in zip(
                literal_suffix_matches,
                control_suffix_matches,
                strict=True,
            )
        )
        and paired_suffix is None
    ):
        return True, answer

    # From here this is the closed suffix-block shape.  Invalid instances are
    # owned and returned unchanged rather than falling into broader repairs.
    if (
        _enumerates_word_values(request)
        or _has_source_authority(request)
        or _EXPLANATION_CUE.search(request)
        or _requests_literal_repetition(request)
        or _PLAIN_LIST_TOKEN.fullmatch(literal) is None
        or len("\n".join(suffix).encode("utf-8")) > 256
        or "\r" in answer
        or "`" in answer
        or "<" in answer
        or ">" in answer
        or "~" in answer
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
    ):
        return True, answer
    matches = _plain_homogeneous_word_list(list_lines, expected)
    if matches is None or len(_literal_occurrences(answer, literal)) != 1:
        return True, answer
    if paired_suffix is not None:
        required_token = str(paired_suffix.group("token"))
        control_token = str(paired_suffix.group("control_ru") or paired_suffix.group("control_en") or "")
    elif len(suffix) == 1:
        required_match = literal_suffix_matches[0]
        if required_match is None:
            return True, answer
        required_token = str(required_match.group("token"))
        control_token = None
    else:
        required_matches = [match for match in literal_suffix_matches if match is not None]
        control_matches = [match for match in control_suffix_matches if match is not None]
        if len(required_matches) != 1 or len(control_matches) != 1:
            return True, answer
        required_token = str(required_matches[0].group("token"))
        control_token = str(
            control_matches[0].group("control_ru") or control_matches[0].group("control_en") or ""
        )
    if required_token != literal:
        return True, answer
    if control_token is not None:
        control = _trailing_control_metadata_token(request)
        if (
            control_token.casefold() == literal.casefold()
            or control is None
            or control_token != control
            or len(_literal_occurrences(answer, control_token)) != 1
        ):
            return True, answer

    last = matches[-1]
    result_lines = [
        *list_lines[:-1],
        f"{last.group('indent')}{last.group('prefix')}{last.group('space')}{literal}",
    ]
    result = "\n".join(result_lines)
    validated = _plain_homogeneous_word_list(result_lines, expected)
    closed_contract = regenerable_text_shape_contract(request)
    if (
        validated is None
        or result_lines[:-1] != list_lines[:-1]
        or validated[-1].group("value") != literal
        or len(_literal_occurrences(result, literal)) != 1
        or (closed_contract is not None and explicit_text_shape_status(request, result) != TEXT_SHAPE_VALID)
    ):
        return True, answer
    # Exact N lines have no suffix and therefore cannot be owned a second time.
    replay_owned, replay = _repair_word_list_suffix_block(request, result, literal)
    if replay_owned or replay != result:
        return True, answer
    return True, result


def _repair_word_list_values(
    request: str,
    answer: str,
    literal: str,
) -> tuple[bool, str]:
    """Own one exact-N word list and remove one closed metadata row.

    Recognition deliberately happens before generic literal/Markdown cleanup.
    Once an exact-N list for a closed word-list request contains the requested
    marker, malformed variants are owned and byte-preserved.  This keeps a bad
    case, duplicate, subtoken, layout, or metadata value from becoming
    repairable after a later cleanup pass.
    """

    contract = regenerable_text_shape_contract(request)
    expected = contract.count if contract is not None else None
    if (
        contract is None
        or contract.kind != "list"
        or not contract.word_list
        or contract.literal != literal
        or expected is None
        or not (2 <= expected <= 10)
        or request.casefold().count(contract.literal.casefold()) != 1
        or request.casefold().count(contract.control.casefold()) != 1
    ):
        return False, answer
    lines = answer.splitlines()
    if (
        len(lines) != expected
        or "\r" in answer
        or "\n".join(lines) != answer
        or any(not line.strip() for line in lines)
    ):
        return False, answer
    raw_matches = [_LIST_LINE.fullmatch(line) for line in lines]
    if any(match is None for match in raw_matches):
        return False, answer
    matches = [match for match in raw_matches if match is not None]
    # The synthetic marker contains both a separator and a digit, so a
    # case-insensitive substring is a sufficiently narrow family recognizer.
    # Exact boundary/case checks below decide whether mutation is authorised.
    if literal.casefold() not in answer.casefold():
        return False, answer

    # An already valid marker+control word list is a fixed point
    # owned by this branch.  Returning here also prevents later generic repairs
    # from changing its byte layout.
    if explicit_text_shape_status(request, answer) == TEXT_SHAPE_VALID:
        return True, answer

    layouts = {(match.group("indent"), match.group("space")) for match in matches}
    prefixes = [str(match.group("prefix")) for match in matches]
    numbered = [prefix[0].isdigit() for prefix in prefixes]
    if len(layouts) != 1 or (any(numbered) and not all(numbered)):
        return True, answer
    if all(numbered):
        if len({prefix[-1] for prefix in prefixes}) != 1 or [prefix[:-1] for prefix in prefixes] != [
            str(index) for index in range(1, expected + 1)
        ]:
            return True, answer
    elif len(set(prefixes)) != 1:
        return True, answer

    marker_rows = [
        index
        for index, match in enumerate(matches)
        if len(_literal_occurrences(str(match.group("value")), literal)) == 1
    ]
    if (
        len(marker_rows) != 1
        or len(_literal_occurrences(answer, literal)) != 1
        or answer.casefold().count(literal.casefold()) != 1
        or len(_literal_occurrences(answer, contract.control)) != 1
        or answer.casefold().count(contract.control.casefold()) != 1
    ):
        return True, answer
    marker_row = marker_rows[0]
    marker_value = str(matches[marker_row].group("value"))
    metadata = _INLINE_WORD_LIST_MARKER_METADATA.fullmatch(marker_value)
    if metadata is None:
        return True, answer
    observed_literal = str(metadata.group("bracket_literal") or metadata.group("plain_literal") or "")
    observed_control = str(metadata.group("control_ru") or metadata.group("control_en") or "")
    extra = str(metadata.group("extra") or "")
    if (
        observed_literal != literal
        or observed_control != contract.control
        or observed_control.casefold() == literal.casefold()
        or len(marker_value.encode("utf-8")) > 320
        or (
            extra
            and (
                _MARKER_LITERAL_REFUSAL_CUE.search(extra)
                or _MISSING_LITERAL_UNSAFE_CONTRACT_CUE.search(extra)
                or _REGENERABLE_ANSWER_REFUSAL_CUE.search(extra)
                or _ANSWER_LITERAL_LABEL.fullmatch(extra)
                or re.fullmatch(
                    rf"(?:{_CONTROL_SUFFIX_RU_LABEL_PATTERN}|{_CONTROL_SUFFIX_EN_LABEL_PATTERN})",
                    extra,
                    re.IGNORECASE,
                )
            )
        )
    ):
        return True, answer

    other_values = [str(match.group("value")) for index, match in enumerate(matches) if index != marker_row]
    folded_other_values = {value.casefold() for value in other_values}
    if (
        len(other_values) != expected - 1
        or any(_PLAIN_LIST_TOKEN.fullmatch(value) is None for value in other_values)
        or len(folded_other_values) != len(other_values)
        or literal.casefold() in folded_other_values
        or contract.control.casefold() in folded_other_values
        or any(
            literal.casefold() in value.casefold() or contract.control.casefold() in value.casefold()
            for value in other_values
        )
    ):
        return True, answer

    marker_match = matches[marker_row]
    row_prefix = f"{marker_match.group('indent')}{marker_match.group('prefix')}{marker_match.group('space')}"
    result_lines = list(lines)
    result_lines[marker_row] = f"{row_prefix}{literal}"
    result = "\n".join(result_lines)
    replay_owned, replay = _repair_word_list_values(request, result, literal)
    if (
        any(line != result_lines[index] for index, line in enumerate(lines) if index != marker_row)
        or lines[marker_row] != f"{row_prefix}{marker_value}"
        or result_lines[marker_row] != f"{row_prefix}{literal}"
        or explicit_text_shape_status(request, result) != TEXT_SHAPE_VALID
        or not replay_owned
        or replay != result
    ):
        return True, answer
    return True, result


def _repair_terminal_literal_list_item_overflow(
    request: str,
    answer: str,
    literal: str,
) -> tuple[bool, str]:
    """Merge one terminal marker-only item into an exact generated list.

    ``owned`` closes the narrow N+1 shape before broader cleanup can erase an
    ambiguity guard.  Only the final list prefix is discarded; every value and
    every earlier row remains byte-visible in the repaired N-item list.
    """

    expected = _requested_list_size(request)
    lines = answer.splitlines()
    if (
        expected is None
        or not (2 <= expected <= 10)
        or len(lines) != expected + 1
        or "\n".join(lines) != answer
    ):
        return False, answer
    parsed = [_LIST_LINE.fullmatch(line) for line in lines]
    if any(match is None for match in parsed):
        return False, answer
    matches = [match for match in parsed if match is not None]
    overflow_value = matches[-1].group("value")
    overflow_literals = _literal_occurrences(overflow_value, literal)
    if len(overflow_literals) != 1:
        return False, answer

    # From here the answer is recognisably the terminal-literal overflow shape.
    # A failed guard owns it and returns the original bytes, preventing literal
    # dedupe or Markdown cleanup from turning an ambiguous instance into one we
    # would have accepted if observed directly.
    literal_metadata = _LIST_SUFFIX_LITERAL_METADATA.fullmatch(overflow_value)
    metadata_token = str(literal_metadata.group("token")) if literal_metadata is not None else ""
    overflow_prefix = overflow_value[: overflow_literals[0].start()]
    unsafe_answer_markup = bool(
        any(character in answer for character in "`<>~[]«»“”\"'")
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
        or _ANSWER_QUOTED_FRAGMENT.search(answer)
    )
    if (
        _is_word_list(request)
        or _enumerates_word_values(request)
        or _has_source_authority(request)
        or _EXPLANATION_CUE.search(request)
        or _requests_literal_repetition(request)
        or _request_refuses_named_literal(request, literal)
        or _MARKER_LITERAL_REFUSAL_CUE.search(overflow_prefix)
        or len(_literal_occurrences(answer, literal)) != 1
        or not (overflow_value == literal or metadata_token == literal)
        or unsafe_answer_markup
    ):
        return True, answer

    layouts = {(match.group("indent"), match.group("space")) for match in matches}
    prefixes = [match.group("prefix") for match in matches]
    numbered = [prefix[0].isdigit() for prefix in prefixes]
    if len(layouts) != 1 or (any(numbered) and not all(numbered)):
        return True, answer
    if all(numbered):
        if len({prefix[-1] for prefix in prefixes}) != 1 or [prefix[:-1] for prefix in prefixes] != [
            str(index) for index in range(1, expected + 2)
        ]:
            return True, answer
    elif len(set(prefixes)) != 1:
        return True, answer

    destination = matches[expected - 1]
    result_lines = lines[:expected]
    merged_value = f"{destination.group('value')} {overflow_value}"
    result_lines[-1] = (
        f"{destination.group('indent')}{destination.group('prefix')}"
        f"{destination.group('space')}{merged_value}"
    )
    result = "\n".join(result_lines)
    validated = _parsed_exact_list(request, result)
    if validated is None:
        return True, answer
    _validated_expected, validated_matches = validated
    replay_owned, replay = _repair_terminal_literal_list_item_overflow(request, result, literal)
    if (
        result_lines[:-1] != lines[: expected - 1]
        or validated_matches[-1].group("value") != merged_value
        or len(_literal_occurrences(result, literal)) != 1
        or replay_owned
        or replay != result
    ):
        return True, answer
    return True, result


def _append_missing_literal_to_control_anchored_list(
    request: str,
    answer: str,
    literal: str,
) -> tuple[bool, str]:
    """Append one omitted literal without replacing any generated bytes."""

    expected = _requested_list_size(request)
    control = _trailing_control_metadata_token(request)
    lines = answer.splitlines()
    if (
        expected is None
        or not (2 <= expected <= 10)
        or control is None
        or control.casefold() == literal.casefold()
        or len(lines) != expected
        or "\n".join(lines) != answer
        or _literal_occurrences(answer, literal)
    ):
        return False, answer
    parsed = _parsed_exact_list(request, answer)
    if parsed is None:
        return False, answer
    _parsed_expected, matches = parsed
    literal_components = [part.casefold() for part in re.split(r"[-_.:]+", literal) if part]
    if len(literal_components) < 3:
        return True, answer
    control_occurrences = _literal_occurrences(answer, control)
    final_value = matches[-1].group("value")
    russian_control_tail = re.compile(
        rf"(?<![\w-]){_CONTROL_SUFFIX_RU_LABEL_PATTERN}\b[ \t]*"
        rf"(?:[:=—–-][ \t]*|[ \t]+){re.escape(control)}[.!?]?[ \t]*$",
        re.IGNORECASE,
    )
    english_control_tail = re.compile(
        rf"(?<![\w-]){_CONTROL_SUFFIX_EN_LABEL_PATTERN}\b[ \t]*"
        rf"[:=—–-][ \t]*{re.escape(control)}[.!?]?[ \t]*$",
        re.IGNORECASE,
    )
    if (
        len(control_occurrences) != 1
        or len(_literal_occurrences(final_value, control)) != 1
        or not (russian_control_tail.search(final_value) or english_control_tail.search(final_value))
    ):
        return True, answer

    # The exact-N list with one final, request-bound control anchor is now
    # owned.  Any ambiguity keeps every byte unchanged and blocks cleanup from
    # making a later invocation appear safe.
    layouts = {(match.group("indent"), match.group("space")) for match in matches}
    prefixes = [match.group("prefix") for match in matches]
    numbered = [prefix[0].isdigit() for prefix in prefixes]
    if all(numbered):
        homogeneous_prefixes = len({prefix[-1] for prefix in prefixes}) == 1
    else:
        homogeneous_prefixes = not any(numbered) and len(set(prefixes)) == 1
    unsafe_markup = bool(
        any(character in answer for character in "`<>~[]«»“”\"'")
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
        or _ANSWER_QUOTED_FRAGMENT.search(answer)
        or re.search(r"https?://|\bwww\.", answer, re.IGNORECASE)
    )
    marker_like_alias = False
    for text in (request, answer):
        for token_match in _DELIMITED_ATOM.finditer(text):
            token = token_match.group(0)
            if token in {literal, control}:
                continue
            components = [part.casefold() for part in re.split(r"[-_.:]+", token) if part]
            shared_components = len(set(components).intersection(literal_components))
            if (
                len(literal_components) >= 3
                and len(components) >= 2
                and (
                    (components[0] == literal_components[0] and components[-1] == literal_components[-1])
                    or (
                        (components[0] == literal_components[0] or components[-1] == literal_components[-1])
                        and shared_components >= max(2, min(len(components), len(literal_components)) - 1)
                    )
                )
            ):
                marker_like_alias = True
                break
        if marker_like_alias:
            break
    values = [match.group("value") for match in matches]
    generic_russian_control_tail = re.compile(
        rf"(?<![\w-]){_CONTROL_SUFFIX_RU_LABEL_PATTERN}\b[ \t]*"
        rf"(?:[:=—–-][ \t]*|[ \t]+)(?P<token>{_PLAIN_LIST_TOKEN_PATTERN})[.!?]?[ \t]*$",
        re.IGNORECASE,
    )
    generic_english_control_tail = re.compile(
        rf"(?<![\w-]){_CONTROL_SUFFIX_EN_LABEL_PATTERN}\b[ \t]*"
        rf"[:=—–-][ \t]*(?P<token>{_PLAIN_LIST_TOKEN_PATTERN})[.!?]?[ \t]*$",
        re.IGNORECASE,
    )
    observed_control_tokens = [
        str(match.group("token"))
        for value in values
        for pattern in (generic_russian_control_tail, generic_english_control_tail)
        if (match := pattern.search(value)) is not None
    ]
    if (
        _is_word_list(request)
        or _enumerates_word_values(request)
        or _has_source_authority(request)
        or _QUOTE_CUE.search(request)
        or _EXPLANATION_CUE.search(request)
        or _requests_literal_repetition(request)
        or not _request_explicitly_includes_named_literal(request, literal, control, expected)
        or _request_refuses_named_literal(request, literal)
        or _MARKER_LITERAL_REFUSAL_CUE.search(answer)
        or _REPEAT_LITERAL_CUE.search(answer)
        or _EXPLANATION_CUE.search(answer)
        or _ANSWER_LITERAL_LABEL.search(answer)
        or literal.casefold() in answer.casefold()
        or answer.casefold().count(control.casefold()) != 1
        or any(token != control and _DELIMITED_ATOM.fullmatch(token) for token in observed_control_tokens)
        or marker_like_alias
        or unsafe_markup
        or len(layouts) != 1
        or not homogeneous_prefixes
        or any(line != line.rstrip(" \t") for line in lines)
        or any(re.search(r"[A-Za-zА-Яа-яЁё0-9]", value) is None for value in values)
        or len({value.casefold() for value in values}) != expected
    ):
        return True, answer

    result = f"{answer} {literal}"
    validated = _parsed_exact_list(request, result)
    replay_owned, replay = _append_missing_literal_to_control_anchored_list(request, result, literal)
    if (
        validated is None
        or not result.startswith(answer)
        or result[: len(answer)] != answer
        or result[len(answer) :] != f" {literal}"
        or len(_literal_occurrences(result, literal)) != 1
        or len(_literal_occurrences(result, control)) != 1
        or replay_owned
        or replay != result
    ):
        return True, answer
    return True, result


def _repair_bullet_colon_prefix(
    request: str,
    answer: str,
    literal: str,
) -> tuple[bool, str]:
    """Delete one colon accidentally placed inside an exact bullet prefix."""

    bullet_contracts = [
        match
        for match in _BULLET_LIST_CONTRACT.finditer(request)
        if not _inside_reported_quote(request, match.start())
    ]
    numbered_contracts = [
        match
        for match in _NUMBERED_LIST_CONTRACT.finditer(request)
        if not _inside_reported_quote(request, match.start())
    ]
    expected = _requested_list_size(request)
    lines = answer.splitlines()
    if (
        len(bullet_contracts) != 1
        or numbered_contracts
        or expected != 2
        or len(lines) != expected
        or "\n".join(lines) != answer
    ):
        return False, answer
    valid_rows = [(index, _LIST_LINE.fullmatch(line)) for index, line in enumerate(lines)]
    malformed_rows = [(index, _BULLET_COLON_LINE.fullmatch(line)) for index, line in enumerate(lines)]
    valid = [(index, match) for index, match in valid_rows if match is not None]
    malformed = [(index, match) for index, match in malformed_rows if match is not None]
    if len(valid) != 1 or len(malformed) != 1:
        return False, answer
    valid_index, valid_match = valid[0]
    malformed_index, malformed_match = malformed[0]
    if valid_index == malformed_index or valid_match.group("prefix")[0].isdigit():
        return False, answer

    # The complete two-row shape is now owned.  Failed safety or layout guards
    # retain every original byte and prevent a later cleanup from widening it.
    values = [valid_match.group("value"), malformed_match.group("value")]
    unsafe_markup = bool(
        any(character in answer for character in "`<>~[]«»“”\"'")
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
        or _ANSWER_QUOTED_FRAGMENT.search(answer)
        or re.search(r"https?://", answer, re.IGNORECASE)
    )
    if (
        _is_word_list(request)
        or _enumerates_word_values(request)
        or _has_source_authority(request)
        or _EXPLANATION_CUE.search(request)
        or _requests_literal_repetition(request)
        or _request_refuses_named_literal(request, literal)
        or len(_literal_occurrences(answer, literal)) != 1
        or any(re.search(r"[A-Za-zА-Яа-яЁё0-9]", value) is None for value in values)
        or unsafe_markup
        or (
            valid_match.group("indent"),
            valid_match.group("prefix"),
            valid_match.group("space"),
        )
        != (
            malformed_match.group("indent"),
            malformed_match.group("prefix"),
            malformed_match.group("space"),
        )
    ):
        return True, answer

    result_lines = list(lines)
    result_lines[malformed_index] = (
        f"{malformed_match.group('indent')}{malformed_match.group('prefix')}"
        f"{malformed_match.group('space')}{malformed_match.group('value')}"
    )
    result = "\n".join(result_lines)
    replay_owned, replay = _repair_bullet_colon_prefix(request, result, literal)
    if (
        _parsed_exact_list(request, result) is None
        or result_lines[valid_index] != lines[valid_index]
        or malformed_match.group("value") not in result_lines[malformed_index]
        or len(_literal_occurrences(result, literal)) != 1
        or replay_owned
        or replay != result
    ):
        return True, answer
    return True, result


def _repair_missing_ampersand_carrier(
    request: str,
    answer: str,
    literal: str,
) -> tuple[bool, str]:
    """Replace one standalone conjunction for one explicit ampersand contract."""

    contracts = [
        match
        for match in _AMPERSAND_LINE_CONTRACT.finditer(request)
        if not _inside_reported_quote(request, match.start())
    ]
    if (
        len(contracts) != 1
        or len(re.findall(r"\b(?:амперсанд\w*|ampersand)\b", request, re.IGNORECASE)) != 1
        or "&" in request
        or "\\" in request
        or "\n" in answer
        or "\r" in answer
        or "&" in answer
        or not _literal_occurrences(answer, literal)
    ):
        return False, answer
    carriers = list(_STANDALONE_CONJUNCTION.finditer(answer))

    # A direct one-line ampersand contract with no ampersand is owned before
    # safety checks so ambiguous carriers cannot fall through to other repairs.
    contract_window = request[max(0, contracts[0].start() - 96) : min(len(request), contracts[0].end() + 128)]
    unsafe_answer = bool(
        any(character in answer for character in "`~[]«»“”\"'")
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
        or _ANSWER_QUOTED_FRAGMENT.search(answer)
        or re.search(r"https?://|&(?:amp|lt|gt|quot|#\d+|#x[0-9a-f]+);", answer, re.IGNORECASE)
        or re.search(r"<\s*/?\s*[A-Za-zА-Яа-яЁё!]", answer)
    )
    literal_text_request = bool(
        re.search(
            r"\b(?:слов\w*|текстом|буквальн\w*|spell\w*|word|literal(?:ly)?)\b"
            r"[^.!?\n]{0,40}\b(?:амперсанд\w*|ampersand)\b",
            contract_window,
            re.IGNORECASE,
        )
    )
    if (
        len(carriers) != 1
        or len(_literal_occurrences(answer, literal)) != 1
        or _AMPERSAND_REFUSAL_CUE.search(contract_window)
        or _request_refuses_named_literal(request, literal)
        or _has_source_authority(request)
        or _QUOTE_CUE.search(request)
        or _EXPLANATION_CUE.search(request)
        or _requests_literal_repetition(request)
        or literal_text_request
        or unsafe_answer
    ):
        return True, answer

    carrier = carriers[0]
    result = f"{answer[: carrier.start('carrier')]}&{answer[carrier.end('carrier') :]}"
    replay_owned, replay = _repair_missing_ampersand_carrier(request, result, literal)
    if (
        result.count("&") != 1
        or len(result.splitlines()) != 1
        or result[: carrier.start("carrier")] != answer[: carrier.start("carrier")]
        or result[carrier.start("carrier") + 1 :] != answer[carrier.end("carrier") :]
        or len(_literal_occurrences(result, literal)) != 1
        or replay_owned
        or replay != result
    ):
        return True, answer
    return True, result


def _repair_blockquoted_exact_list(request: str, answer: str, literal: str) -> str:
    """Remove one unrequested blockquote wrapper around an otherwise exact list."""

    expected = _requested_list_size(request)
    if (
        expected is None
        or _QUOTE_CUE.search(request)
        or _enumerates_word_values(request)
        or len(_literal_occurrences(answer, literal)) != 1
        or "`" in answer
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
    ):
        return answer
    raw_lines = answer.splitlines()
    quoted = [re.fullmatch(r"[ \t]*>[ \t]+(?P<inner>\S.*)", line) for line in raw_lines]
    if not raw_lines or any(match is None for match in quoted):
        return answer
    inner_lines = [match.group("inner") for match in quoted if match is not None]
    if any(line.lstrip().startswith(">") for line in inner_lines):
        return answer
    list_rows: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(inner_lines):
        match = _LIST_LINE.fullmatch(line)
        if match is not None:
            list_rows.append((index, match))
    if len(list_rows) != expected:
        return answer
    list_matches = [match for _index, match in list_rows]
    numbered = [match.group("prefix")[0].isdigit() for match in list_matches]
    if any(numbered) and not all(numbered):
        return answer
    if all(numbered):
        values = [int(match.group("prefix")[:-1]) for match in list_matches]
        if values != list(range(1, expected + 1)):
            return answer
    first_list_index = list_rows[0][0]
    last_list_index = list_rows[-1][0]
    if [index for index, _match in list_rows] != list(range(first_list_index, last_list_index + 1)):
        return answer
    prefix = inner_lines[:first_list_index]
    suffix = inner_lines[last_list_index + 1 :]
    if (
        len(prefix) > 1
        or len(suffix) > 1
        or sum(len(line) for line in [*prefix, *suffix]) > 512
        or any("<" in line or ">" in line or re.match(r"\s*(?:#{1,6}\s|\|)", line) for line in inner_lines)
    ):
        return answer
    rendered = [
        f"{match.group('indent')}{match.group('prefix')}{match.group('space')}{match.group('value')}"
        for match in list_matches
    ]
    if prefix:
        first = list_matches[0]
        rendered[0] = (
            f"{first.group('indent')}{first.group('prefix')}{first.group('space')}"
            f"{' '.join(prefix)} {first.group('value').lstrip()}"
        )
    if suffix:
        last = list_matches[-1]
        rendered[-1] = (
            f"{last.group('indent')}{last.group('prefix')}{last.group('space')}"
            f"{last.group('value').rstrip()} {' '.join(suffix)}"
        )
    result = "\n".join(rendered)
    return result if len(_literal_occurrences(result, literal)) == 1 else answer


def _repair_list_overflow(request: str, answer: str, literal: str) -> str:
    expected = _requested_list_size(request)
    if expected is None or len(_literal_occurrences(answer, literal)) != 1:
        return answer
    lines = answer.splitlines()
    if len(lines) == expected + 2 and not lines[-2].strip() and lines[-1].strip() == literal:
        lines.pop(-2)
    overflow = lines[-1].strip() if lines else ""
    if (
        len(lines) != expected + 1
        or not overflow
        or _LIST_LINE.fullmatch(lines[-1]) is not None
        or len(_literal_occurrences(overflow, literal)) != 1
    ):
        return answer
    parsed = [_LIST_LINE.fullmatch(line) for line in lines[:-1]]
    if any(match is None for match in parsed):
        return answer
    matches = [match for match in parsed if match is not None]
    numbered = [match.group("prefix")[0].isdigit() for match in matches]
    if any(numbered) and not all(numbered):
        return answer
    if all(numbered):
        values = [int(match.group("prefix")[:-1]) for match in matches]
        if values != list(range(1, expected + 1)):
            return answer
    word_list = _is_word_list(request)
    if word_list:
        if _enumerates_word_values(request) or any(
            len(match.group("value").split()) != 1 for match in matches
        ):
            return answer
        replacement = literal
    else:
        replacement = f"{matches[-1].group('value').rstrip()} {overflow}"
    last = matches[-1]
    lines[-2] = f"{last.group('indent')}{last.group('prefix')}{last.group('space')}{replacement}"
    return "\n".join(lines[:-1])


def _repair_quoted_explanation(request: str, answer: str, literal: str) -> str:
    if not (_QUOTE_CUE.search(request) and _EXPLANATION_CUE.search(request)):
        return answer
    quote_counts = _requested_count_near(request, _QUOTE_CUE)
    if quote_counts and quote_counts != {1}:
        return answer
    if answer.count(literal) != 1:
        return answer
    lines = answer.splitlines()
    if len(lines) != 2 or not all(re.match(r"^[ \t]*>[ \t]+\S", line) for line in lines):
        return answer
    lines[1] = re.sub(r"^([ \t]*)>[ \t]+", r"\1", lines[1], count=1)
    return "\n".join(lines)


def _quote_explanation_relation_is_exact(request: str, literal: str) -> bool:
    named_matches = [
        match
        for match in _NAMED_LITERAL.finditer(request)
        if _unwrap_literal(str(match.group("literal")))[0].strip() == literal
        and not _inside_reported_quote(request, match.start())
    ]
    if len(named_matches) != 1:
        return False
    named = named_matches[0]
    instruction = request[: named.start()]
    if (
        len(list(_QUOTE_CUE.finditer(instruction))) != 1
        or len(list(_EXPLANATION_CUE.finditer(instruction))) != 1
        or _requested_count_near(instruction, _QUOTE_CUE) != {1}
        or _requested_count_near(instruction, _EXPLANATION_CUE) != {1}
        or _QUOTE_EXPLANATION_META_CUE.search(instruction)
    ):
        return False
    include_cues = [
        cue
        for cue in _NAMED_LITERAL_INCLUDE_CUE.finditer(instruction)
        if not _inside_reported_quote(request, cue.start())
    ]
    if len(include_cues) != 1:
        return False
    cue = include_cues[0]
    contract = instruction[: cue.start()]
    between = instruction[cue.end() :]
    suffix = request[named.end() :]
    control_match = _TRAILING_CONTROL_METADATA.search(request)
    control_suffix_exact = bool(
        control_match is not None
        and control_match.start() == named.end()
        and _trailing_control_metadata_token(request) is not None
        and "\n" not in control_match.group(0)
        and "\r" not in control_match.group(0)
        and request.rstrip(" \t").endswith(".")
    )
    return bool(
        (
            _QUOTE_EXPLANATION_RU_CONTRACT.fullmatch(contract)
            or _QUOTE_EXPLANATION_EN_CONTRACT.fullmatch(contract)
        )
        and re.fullmatch(r"[ \t]+(?:the[ \t]+)?", between, re.IGNORECASE)
        and (re.fullmatch(r"\.[ \t]*", suffix) or control_suffix_exact)
    )


def _closed_quote_explanation_contract(request: str) -> tuple[str, str] | None:
    """Return the two request-bound identifiers for a closed quote contract."""

    if not isinstance(request, str) or not request or len(request) > _MAX_REQUEST_CHARS:
        return None
    literal = _single_named_literal(request)
    control = _trailing_control_metadata_token(request)
    if (
        literal is None
        or control is None
        or control.casefold() in literal.casefold()
        or not _safe_regeneration_identifier(literal)
        or not _safe_regeneration_identifier(control)
        or len(_literal_occurrences(request, literal)) != 1
        or len(_literal_occurrences(request, control)) != 1
        or not _quote_explanation_relation_is_exact(request, literal)
        or _has_source_authority(request)
        or _requests_literal_repetition(request)
        or _request_refuses_named_literal(request, literal)
        or _MISSING_LITERAL_UNSAFE_CONTRACT_CUE.search(request)
        or _QUOTE_EXPLANATION_NONAUTHORITY_CUE.search(request)
    ):
        return None
    return literal, control


def _repair_collapsed_quote_explanation_line(
    request: str,
    answer: str,
    literal: str,
) -> tuple[bool, str]:
    """Split one initial inline quote from its blockquoted explanation tail."""

    lines = answer.splitlines()
    line_match = (
        re.fullmatch(r"(?P<indent>[ ]{0,3})>[ \t]+(?P<body>\S.*)", lines[0])
        if len(lines) == 1 and "\n".join(lines) == answer
        else None
    )
    body = str(line_match.group("body")) if line_match is not None else ""
    body_start = line_match.start("body") if line_match is not None else 0
    fragments = [match for match in _ANSWER_QUOTED_FRAGMENT.finditer(body) if match.start() == 0]
    boundaries: list[tuple[re.Match[str], re.Match[str]]] = []
    for fragment in fragments:
        separator = re.match(r"[ \t]+(?=\S)", body[fragment.end() :])
        if separator is not None:
            boundaries.append((fragment, separator))
    recognizable = bool(
        _QUOTE_CUE.search(request)
        and _EXPLANATION_CUE.search(request)
        and line_match is not None
        and boundaries
    )
    if not recognizable:
        return False, answer

    quote_contract = _closed_quote_explanation_contract(request)
    control = quote_contract[1] if quote_contract is not None else None
    if (
        quote_contract is None
        or quote_contract[0] != literal
        or control is None
        or len(fragments) != 1
        or len(boundaries) != 1
        or len(_literal_occurrences(answer, literal)) != 1
        or len(_literal_occurrences(answer, control)) != 0
        or answer.casefold().count(literal.casefold()) != 1
        or control.casefold() in answer.casefold()
    ):
        return True, answer
    fragment, separator = boundaries[0]
    tail_start = fragment.end() + separator.end()
    quote_value = body[fragment.start() : fragment.end()]
    tail = body[tail_start:]
    outside_fragment = body[fragment.end() :]
    without_identifiers = re.sub(
        rf"(?<![\w-]){re.escape(literal)}(?![\w-])",
        " ",
        tail,
    )
    unsafe_markup = bool(
        any(character in outside_fragment for character in "`<>~[]«»“”\"'")
        or _MARKDOWN_LINK.search(body)
        or _SIMPLE_EMPHASIS.search(body)
        or _has_unresolved_emphasis_marker(body)
        or re.search(r"https?://|\bwww\.", body, re.IGNORECASE)
        or re.search(r"(?m)^\s*(?:[-*+•]|\d{1,2}[.)]|#{1,6})\s+", body)
    )
    if (
        unsafe_markup
        or _literal_occurrences(quote_value, literal)
        or _literal_occurrences(quote_value, control)
        or len(_literal_occurrences(tail, literal)) != 1
        or len(_literal_occurrences(tail, control)) != 0
        or re.search(r"[A-Za-zА-Яа-яЁё]{3,}", quote_value) is None
        or re.search(r"[A-Za-zА-Яа-яЁё]{3,}", without_identifiers) is None
    ):
        return True, answer

    separator_start = body_start + fragment.end()
    separator_end = separator_start + separator.end()
    result = f"{answer[:separator_start]}\n{answer[separator_end:]}"
    result_lines = result.splitlines()
    replay_owned, replay = _repair_collapsed_quote_explanation_line(request, result, literal)
    if (
        len(result_lines) != 2
        or result_lines[0] != answer[:separator_start]
        or result_lines[1] != answer[separator_end:]
        or re.fullmatch(r"[ ]{0,3}>[ \t]+\S.*", result_lines[0]) is None
        or re.match(r"^[ \t]*>", result_lines[1]) is not None
        or len(_literal_occurrences(result, literal)) != 1
        or len(_literal_occurrences(result, control)) != 0
        or replay_owned
        or replay != result
    ):
        return True, answer
    return True, result


def exact_quote_explanation_shape_owned(request: str, answer: str) -> bool:
    """Recognise an already-correct closed quote plus plain explanation."""

    if (
        not isinstance(request, str)
        or not isinstance(answer, str)
        or len(answer.splitlines()) != 2
        or "\n".join(answer.splitlines()) != answer
    ):
        return False
    literal = _single_named_literal(request)
    if literal is None:
        return False
    lines = answer.splitlines()
    collapsed = f"{lines[0]} {lines[1]}"
    owned, candidate = _repair_collapsed_quote_explanation_line(request, collapsed, literal)
    replay_owned, replay = _repair_collapsed_quote_explanation_line(request, answer, literal)
    return bool(
        owned and candidate == answer and candidate != collapsed and not replay_owned and replay == answer
    )


def repair_collapsed_quote_explanation_shape(
    request: str,
    answer: str,
) -> tuple[bool, str]:
    """Return the closed quote/explanation ownership decision and candidate.

    Runtime transport needs to distinguish this exact two-line repair from the
    broader deterministic postprocessor.  In particular, a repaired quote must
    not acquire an out-of-band banner or warning and silently become three
    lines.  ``owned`` is tri-state-like: a recognizable but ambiguous shape is
    owned with the original bytes as its candidate, blocking later cleanup.
    """

    if (
        not isinstance(request, str)
        or not isinstance(answer, str)
        or not request
        or not answer
        or len(request) > _MAX_REQUEST_CHARS
        or len(answer) > _MAX_ANSWER_CHARS
        or _DIRECT_COMPOSITION.search(request) is None
        or _NEGATED_SHAPE.search(request)
    ):
        return False, answer
    answer = strip_parser_control_metadata(request, answer)
    if not answer:
        return True, answer
    literal = _single_named_literal(request)
    if literal is None:
        return False, answer
    if exact_quote_explanation_shape_owned(request, answer):
        return True, answer
    return _repair_collapsed_quote_explanation_line(request, answer, literal)


def _repair_empty_quote_separator_before_explanation(
    request: str,
    answer: str,
    literal: str,
) -> tuple[bool, str]:
    """Own one quote-gap-quote family and close only its exact safe form.

    Ownership is deliberately established before generic literal and emphasis
    cleanup.  Once the recognizable family is present, an ambiguous request,
    layout, literal, or markup must preserve the original bytes instead of
    becoming repairable after another cleanup pass.
    """

    lines = answer.splitlines()
    quote_like = re.compile(r"^[ \t]{0,3}>")
    separator_like = [
        index
        for index, line in enumerate(lines)
        if not line.strip() or re.fullmatch(r"[ \t]{0,3}>[ \t]*", line)
    ]
    recognizable = bool(
        _QUOTE_CUE.search(request)
        and _EXPLANATION_CUE.search(request)
        and any(
            any(quote_like.match(line) for line in lines[:index])
            and any(quote_like.match(line) for line in lines[index + 1 :])
            for index in separator_like
        )
    )
    if not recognizable:
        return False, answer

    if (
        not _quote_explanation_relation_is_exact(request, literal)
        or _has_source_authority(request)
        or _requests_literal_repetition(request)
        or _request_refuses_named_literal(request, literal)
        or _MISSING_LITERAL_UNSAFE_CONTRACT_CUE.search(request)
        or _QUOTE_EXPLANATION_NONAUTHORITY_CUE.search(request)
        or len(_literal_occurrences(answer, literal)) != 1
        or answer.casefold().count(literal.casefold()) != 1
    ):
        return True, answer
    if len(lines) != 3 or "\n".join(lines) != answer:
        return True, answer
    quote_line = re.compile(r"(?P<indent>[ ]{0,3})>[ \t]+(?P<value>\S(?:.*\S)?)")
    empty_quote_line = re.compile(r"(?P<indent>[ ]{0,3})>[ \t]*")
    first = quote_line.fullmatch(lines[0])
    separator = empty_quote_line.fullmatch(lines[1])
    explanation = quote_line.fullmatch(lines[2])
    if (
        first is None
        or separator is None
        or explanation is None
        or len({first.group("indent"), separator.group("indent"), explanation.group("indent")}) != 1
    ):
        return True, answer
    first_value = str(first.group("value"))
    explanation_value = str(explanation.group("value"))
    content = f"{first_value}\n{explanation_value}"
    unsafe_markup = bool(
        any(character in content for character in "`<>~[]«»“”\"'\\")
        or _MARKDOWN_LINK.search(content)
        or _SIMPLE_EMPHASIS.search(content)
        or _has_unresolved_emphasis_marker(content)
        or re.search(r"https?://|\bwww\.", content, re.IGNORECASE)
        or re.search(r"(?m)^\s*(?:[-*+•]|\d{1,2}[.)]|#{1,6})\s+", content)
    )
    without_literal = re.sub(
        rf"(?<![\w-]){re.escape(literal)}(?![\w-])",
        " ",
        explanation_value,
    )
    if (
        unsafe_markup
        or re.search(r"[A-Za-zА-Яа-яЁё]{3,}", first_value) is None
        or re.search(r"[A-Za-zА-Яа-яЁё]{3,}", without_literal) is None
    ):
        return True, answer

    result = f"{lines[0]}\n{explanation_value}"
    result_lines = result.splitlines()
    replay_owned, replay = _repair_empty_quote_separator_before_explanation(request, result, literal)
    if (
        len(result_lines) != 2
        or result_lines[0] != lines[0]
        or result_lines[1] != explanation_value
        or quote_line.fullmatch(result_lines[0]) is None
        or re.match(r"^[ \t]*>", result_lines[1]) is not None
        or len(_literal_occurrences(result, literal)) != 1
        or replay_owned
        or replay != result
    ):
        return True, answer
    return True, result


def _requested_count_near(request: str, cue_pattern: re.Pattern[str]) -> set[int]:
    request = _instruction_before_literal(request)
    sizes: set[int] = set()
    for cue in cue_pattern.finditer(request):
        window = request[max(0, cue.start() - 40) : min(len(request), cue.end() + 40)]
        for match in _NUMBER_TOKEN.finditer(window):
            token = match.group(0).casefold()
            sizes.add(int(token) if token.isdigit() else _NUMBER_WORDS[token])
    return sizes


def _repair_angle_literal(request: str, answer: str, literal: str) -> str:
    angle_cues = [
        match
        for match in _ANGLE_CUE.finditer(request)
        if not _inside_reported_quote(request, match.start())
        and not _NEGATION_BEFORE_CUE.search(request[max(0, match.start() - 48) : match.start()])
    ]
    if not angle_cues or not _ONE_SENTENCE_CUE.search(request):
        return answer
    explicit_targets = {
        str(match.group("target"))
        for match in _QUOTED_ATOM.finditer(request)
        if not _inside_reported_quote(request, match.start())
        and any(abs(match.start() - cue.start()) <= 120 for cue in angle_cues)
    }
    explicit_targets.update(
        match.group(1)
        for match in re.finditer(
            r"\b(?:слово|литерал|word|literal)\s+([A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{0,63})\b",
            request,
            re.IGNORECASE,
        )
        if not _inside_reported_quote(request, match.start())
        and any(abs(match.start() - cue.start()) <= 120 for cue in angle_cues)
    )
    if any(target.casefold() != literal.casefold() for target in explicit_targets):
        return answer
    if (
        "\n" in answer
        or "\r" in answer
        or "<" in answer
        or ">" in answer
        or "`" in answer
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
        or len(_literal_occurrences(answer, literal)) != 1
        or literal.casefold() in _HTML_TAG_NAMES
    ):
        return answer
    occurrence = _literal_occurrences(answer, literal)[0]
    if _inside_reported_quote(answer, occurrence.start()):
        return answer
    previous = answer[occurrence.start() - 1 : occurrence.start()]
    following = answer[occurrence.end() : occurrence.end() + 1]
    if (previous and previous in "/\\") or (following and following in "/\\"):
        return answer
    return f"{answer[: occurrence.start()]}<{occurrence.group(0)}>{answer[occurrence.end() :]}"


def _repair_single_sentence_punctuation(
    request: str,
    answer: str,
    literal: str | None,
) -> str:
    """Join plainly separated clauses when exactly one sentence was requested."""

    sentence_contracts = [
        match
        for match in _SINGLE_SENTENCE_CONTRACT.finditer(request)
        if not _inside_reported_quote(request, match.start())
        and not _NEGATION_BEFORE_CUE.search(request[max(0, match.start() - 48) : match.start()])
        and not _NEGATED_SINGLE_SENTENCE_PREFIX.search(request[max(0, match.start() - 96) : match.start()])
        and not _METALINGUISTIC_SENTENCE_MENTION.search(request[: match.start()])
        and not _POST_SINGLE_SENTENCE_NEGATION.search(request[match.end() : match.end() + 64])
        and not _POST_SINGLE_SENTENCE_REVISION.search(request[match.end() : match.end() + 112])
    ]
    if len(sentence_contracts) != 1 or any(
        not _inside_reported_quote(request, match.start())
        for match in _NON_SINGLE_SENTENCE_CONTRACT.finditer(request)
    ):
        return answer
    if (
        "\n" in answer
        or "\r" in answer
        or "`" in answer
        or any(character in answer for character in '«»“”"')
        or "~" in answer
        or _MARKDOWN_LINK.search(answer)
        or not _has_only_balanced_simple_emphasis(answer)
        or re.match(r"\s*(?:#{1,6}\s|>\s|[-+•]\s|\d{1,2}[.)]\s|\|)", answer)
    ):
        return answer

    # The angle-literal repair deliberately runs first.  Permit exactly that
    # request-owned carrier, but reject every other raw angle/HTML construct.
    visible = answer
    if "<" in visible or ">" in visible:
        wrapped = f"<{literal}>" if literal is not None else ""
        angle_cues = [
            match
            for match in _ANGLE_CUE.finditer(request)
            if not _inside_reported_quote(request, match.start())
            and not _NEGATION_BEFORE_CUE.search(request[max(0, match.start() - 48) : match.start()])
        ]
        if (
            not wrapped
            or not angle_cues
            or literal is None
            or literal.casefold() in _HTML_TAG_NAMES
            or visible.count(wrapped) != 1
        ):
            return answer
        visible = visible.replace(wrapped, literal, 1)
        if "<" in visible or ">" in visible:
            return answer

    boundaries = list(_TERMINAL_SENTENCE_BOUNDARY.finditer(answer))
    if len(boundaries) <= 1:
        return answer
    if boundaries[-1].end() != len(answer.rstrip()):
        return answer
    intermediate = boundaries[:-1]
    if any(match.group(0) != "." for match in intermediate):
        return answer
    numeric_boundaries = [
        match for match in intermediate if re.search(r"(?<!\d)\d{1,3}$", answer[: match.start()].rstrip())
    ]
    if len(numeric_boundaries) >= 2 or any(
        _INLINE_NUMERIC_ENUMERATOR_PREFIX.search(answer[: match.start()].rstrip())
        for match in numeric_boundaries
    ):
        return answer
    if any(
        _COMMON_ABBREVIATION_PREFIX.search(answer[: match.start()].rstrip())
        or _LONE_ATOM_PREFIX.search(answer[: match.start()].rstrip())
        or _NUMERIC_ENUMERATOR_PREFIX.search(answer[: match.start()].rstrip())
        or _ROMAN_ENUMERATOR_PREFIX.search(answer[: match.start()].rstrip())
        or _PAREN_ENUMERATOR_PREFIX.search(answer[: match.start()].rstrip())
        or _SHORT_MEASURE_PREFIX.search(answer[: match.start()].rstrip())
        for match in intermediate
    ):
        return answer

    pieces: list[str] = []
    cursor = 0
    for match in intermediate:
        pieces.append(answer[cursor : match.start()])
        pieces.append(";")
        cursor = match.end()
    pieces.append(answer[cursor:])
    repaired = "".join(pieces)
    return repaired if len(_TERMINAL_SENTENCE_BOUNDARY.findall(repaired)) <= 1 else answer


def _has_unresolved_emphasis_marker(text: str) -> bool:
    for position, character in enumerate(text):
        if character == "*":
            line_start = text.rfind("\n", 0, position) + 1
            before_on_line = text[line_start:position]
            following = text[position + 1 : position + 2]
            previous = text[position - 1 : position]
            if not before_on_line.strip() and following in " \t":
                continue
            if previous.isspace() and following.isspace():
                continue
            return True
        if character == "_":
            previous = text[position - 1 : position]
            following = text[position + 1 : position + 2]
            if previous.isalnum() and following.isalnum():
                continue
            return True
    return False


def _has_only_balanced_simple_emphasis(text: str) -> bool:
    """Return whether every emphasis marker belongs to a parsed simple span."""

    masked = list(text)
    for match in _SIMPLE_EMPHASIS.finditer(text):
        content = next(
            (
                str(match.group(name))
                for name in (
                    "bold_star_text",
                    "bold_under_text",
                    "italic_star_text",
                    "italic_under_text",
                )
                if match.group(name) is not None
            ),
            "",
        )
        if not content or _has_unresolved_emphasis_marker(content):
            return False
        for position in range(match.start(), match.end()):
            if masked[position] != "\n":
                masked[position] = " "
    return not _has_unresolved_emphasis_marker("".join(masked))


def _requested_emphasis_styles(request: str) -> set[str]:
    styles: set[str] = set()
    bold_requested = _has_positive_cue(request, _BOLD_REQUEST_CUE)
    italic_requested = _has_positive_cue(request, _ITALIC_REQUEST_CUE)
    if bold_requested:
        styles.add("bold")
    if italic_requested:
        styles.add("italic")
    if not styles and _has_positive_cue(request, _GENERIC_EMPHASIS_REQUEST_CUE):
        styles.update(("bold", "italic"))
    return styles


def _repair_missing_requested_emphasis(
    request: str,
    answer: str,
    literal: str | None,
) -> str:
    target = _single_emphasis_target(request)
    if target is None or target == literal or "\n" in answer or "\r" in answer:
        return answer
    bold_requested = _has_positive_cue(request, _BOLD_REQUEST_CUE)
    italic_requested = _has_positive_cue(request, _ITALIC_REQUEST_CUE)
    generic_requested = _has_positive_cue(request, _GENERIC_EMPHASIS_REQUEST_CUE)
    if (bold_requested and italic_requested) or not (bold_requested or italic_requested or generic_requested):
        return answer
    if (
        "`" in answer
        or "<" in answer
        or ">" in answer
        or _MARKDOWN_LINK.search(answer)
        or _SIMPLE_EMPHASIS.search(answer)
        or _has_unresolved_emphasis_marker(answer)
    ):
        return answer
    # Emphasis is a presentation contract, not an opaque-literal identity
    # check.  A requested lower-case word may legitimately begin the answer
    # with an upper-case letter; wrap the observed spelling instead of missing
    # that sole safe target.  Keep the shared literal matcher case-sensitive:
    # markers, identifiers and tokens must retain their exact byte-visible
    # spelling on every other repair path.
    occurrences = [
        match
        for match in re.finditer(
            rf"(?<![\w-]){re.escape(target)}(?![\w-])",
            answer,
            re.IGNORECASE,
        )
        # Python's Unicode IGNORECASE deliberately folds a few characters
        # beyond ordinary casing (for example ASCII i and dotted/dotless I).
        # They are useful candidates, not authority to rewrite a different
        # code point sequence.  Exact casefold equality keeps that boundary
        # closed while still accepting sentence-initial capitalization.
        if match.group(0).casefold() == target.casefold()
    ]
    if len(occurrences) != 1:
        return answer
    match = occurrences[0]
    opening, closing = ("*", "*") if italic_requested and not bold_requested else ("**", "**")
    return f"{answer[: match.start()]}{opening}{match.group(0)}{closing}{answer[match.end() :]}"


def _repair_unrequested_emphasis(request: str, answer: str, literal: str | None) -> str:
    del literal
    if "`" in answer or _MARKDOWN_LINK.search(answer) or "***" in answer or "___" in answer:
        return answer
    matches = list(_SIMPLE_EMPHASIS.finditer(answer))
    if not matches:
        return answer

    masked = list(answer)
    spans: list[tuple[int, int, str, str]] = []
    for match in matches:
        if match.group("bold_star") is not None:
            style, content = "bold", match.group("bold_star_text")
        elif match.group("bold_under") is not None:
            style, content = "bold", match.group("bold_under_text")
        elif match.group("italic_star") is not None:
            style, content = "italic", match.group("italic_star_text")
        else:
            style, content = "italic", match.group("italic_under_text")
        if not content or _has_unresolved_emphasis_marker(content):
            return answer
        spans.append((match.start(), match.end(), content, style))
        for position in range(match.start(), match.end()):
            if masked[position] != "\n":
                masked[position] = " "
    if _has_unresolved_emphasis_marker("".join(masked)):
        return answer

    requested = _requested_emphasis_styles(request)
    pieces: list[str] = []
    cursor = 0
    changed = False
    for start, end, content, style in spans:
        pieces.append(answer[cursor:start])
        if style in requested:
            pieces.append(answer[start:end])
        else:
            pieces.append(content)
            changed = True
        cursor = end
    pieces.append(answer[cursor:])
    return "".join(pieces) if changed else answer


def repair_explicit_text_shape(request: str, answer: str) -> str:
    """Apply bounded high-confidence shape repairs or return ``answer`` unchanged."""

    if (
        not isinstance(request, str)
        or not isinstance(answer, str)
        or not request
        or not answer
        or len(request) > _MAX_REQUEST_CHARS
        or len(answer) > _MAX_ANSWER_CHARS
        or not _DIRECT_COMPOSITION.search(request)
        or _NEGATED_SHAPE.search(request)
    ):
        return answer
    literal = _single_named_literal(request)
    source_authority = _has_source_authority(request)
    if literal is not None:
        collapsed_quote_owned, collapsed_quote_candidate = repair_collapsed_quote_explanation_shape(
            request, answer
        )
        if collapsed_quote_owned:
            return collapsed_quote_candidate
        quote_owned, quote_candidate = _repair_empty_quote_separator_before_explanation(
            request,
            answer,
            literal,
        )
        if quote_owned:
            return quote_candidate
        word_values_owned, word_values_candidate = _repair_word_list_values(request, answer, literal)
        if word_values_owned:
            return word_values_candidate
        suffix_owned, suffix_candidate = _repair_word_list_suffix_block(request, answer, literal)
        if suffix_owned:
            return suffix_candidate
        bullet_owned, bullet_candidate = _repair_bullet_colon_prefix(request, answer, literal)
        if bullet_owned:
            return bullet_candidate
        overflow_owned, overflow_candidate = _repair_terminal_literal_list_item_overflow(
            request,
            answer,
            literal,
        )
        if overflow_owned:
            return overflow_candidate
        append_owned, append_candidate = _append_missing_literal_to_control_anchored_list(
            request,
            answer,
            literal,
        )
        if append_owned:
            return append_candidate
        ampersand_owned, ampersand_candidate = _repair_missing_ampersand_carrier(
            request,
            answer,
            literal,
        )
        if ampersand_owned:
            return ampersand_candidate
    candidate = (
        _dedupe_named_literal(request, answer, literal)
        if literal is not None and not source_authority
        else answer
    )
    candidate = _repair_unrequested_emphasis(request, candidate, literal)
    if literal is not None and not source_authority:
        repairs = [_repair_blockquoted_exact_list]
        repairs.extend(
            (
                _repair_list_overflow,
                _repair_quoted_explanation,
                _repair_angle_literal,
            )
        )
        for repair in repairs:
            repaired = repair(request, candidate, literal)
            if repaired != candidate:
                candidate = repaired
                break
    if not source_authority:
        candidate = _repair_single_sentence_punctuation(request, candidate, literal)
    candidate = _repair_missing_requested_emphasis(request, candidate, literal)
    candidate = _repair_unrequested_emphasis(request, candidate, literal)
    return candidate
