"""Closed temporal routing and calendar arithmetic for chat prefetches.

The model may classify meaning, but it never gets to invent absolute dates.
Only the enums in :class:`TimeIntent` cross that boundary; this module anchors
them to the configured local day and returns a bounded inclusive interval.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta

TIME_DIRECTIONS = frozenset({"past", "future", "none"})
TIME_WINDOW_KINDS = frozenset(
    {
        "single_day",
        "single_hour",
        "rolling_days",
        "calendar_week",
        "calendar_month",
        "explicit_range",
        "none",
    }
)


@dataclass(frozen=True)
class TimeIntent:
    direction: str
    window_kind: str


@dataclass(frozen=True)
class TimeWindow:
    since: str
    until: str


_MONTHS = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "ма": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}
_MONTH_WORD_PATTERN = (
    r"(?:январ(?:ь|я|е|ю|[её]м)|феврал(?:ь|я|е|ю|[её]м)|"
    r"март(?:а|е|у|ом)?|апрел(?:ь|я|е|ю|ем)|ма(?:й|я|е|ю|ем)|"
    r"июн(?:ь|я|е|ю|ем)|июл(?:ь|я|е|ю|ем)|август(?:а|е|у|ом)?|"
    r"сентябр(?:ь|я|е|ю|[её]м)|октябр(?:ь|я|е|ю|[её]м)|"
    r"ноябр(?:ь|я|е|ю|[её]м)|декабр(?:ь|я|е|ю|[её]м))"
)
_WEEKDAYS = {
    "понедельник": 0,
    "вторник": 1,
    "сред": 2,
    "четверг": 3,
    "пятниц": 4,
    "суббот": 5,
    "воскресен": 6,
}
_WEEKDAY_WORDS = (
    (re.compile(r"\bпонедельник(?:а|у|ом|е|и|ов|ам|ами|ах)?\b", re.IGNORECASE), 0),
    (re.compile(r"\bвторник(?:а|у|ом|е|и|ов|ам|ами|ах)?\b", re.IGNORECASE), 1),
    # ``в среду`` is the weekday.  ``в среде разработки`` is an environment;
    # the dative boundary ``к среде`` is relational and unsupported anyway.
    (re.compile(r"\bсред(?:а|ы|у|ой|ою|ам|ами|ах)\b", re.IGNORECASE), 2),
    (re.compile(r"\bчетверг(?:а|у|ом|е|и|ов|ам|ами|ах)?\b", re.IGNORECASE), 3),
    (re.compile(r"\bпятниц(?:а|ы|е|у|ей|ею|ам|ами|ах)\b", re.IGNORECASE), 4),
    (re.compile(r"\bсуббот(?:а|ы|е|у|ой|ою|ам|ами|ах)\b", re.IGNORECASE), 5),
    (
        re.compile(
            # Keep the soft-sign weekday lexeme separate from ``воскресение``
            # (resurrection), whose oblique forms share the longer stem.
            r"\bвоскресен(?:ье|ья|ью|ьем|ьи)\b",
            re.IGNORECASE,
        ),
        6,
    ),
)
_NUMBERS = {
    "один": 1,
    "одна": 1,
    "одну": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
}

_NUMBER_WORD_PATTERN = "|".join(sorted(_NUMBERS, key=len, reverse=True))
_NUMBER_TOKEN_PATTERN = r"\d{1,2}|(?:" + _NUMBER_WORD_PATTERN + r")(?:\s+(?:" + _NUMBER_WORD_PATTERN + r"))?"
_RELATIVE_UNIT_FUTURE = re.compile(
    r"\bчерез\s+(?:(" + _NUMBER_TOKEN_PATTERN + r")\s+)?(недел\w*|месяц\w*)\b",
    re.IGNORECASE,
)
_RELATIVE_UNIT_PAST = re.compile(
    r"\b(?:за\s+)?(?:(" + _NUMBER_TOKEN_PATTERN + r")\s+)?"
    r"(недел\w*|месяц\w*)\s+назад\b",
    re.IGNORECASE,
)
_RELATIVE_UNIT_SHAPE = re.compile(
    r"\b(?:через\b.{0,32}\b(?:недел|месяц)\w*|(?:недел|месяц)\w*\s+назад)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_QUANTITY_TOKEN = re.compile(
    r"^(?:ноль|сто|сотн\w*|двест\w*|трист\w*|четырест\w*|"
    r"пятисот\w*|шестисот\w*|семисот\w*|восьмисот\w*|девятисот\w*|"
    r"тысяч\w*|полтора|полторы|полутора|оба|обе|обоих|пар[ауые]?|"
    r"нескольк\w*|мног\w*|мал\w*|"
    r"десят\w*|дюжин\w*)$",
    re.IGNORECASE,
)

_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_RUS_DATE = re.compile(
    r"(?<!\d)(\d{1,2})(?:\s*-?\s*(?:го|е|ое))?\s+"
    r"(" + _MONTH_WORD_PATTERN + r")\b"
    r"(?:\s+(\d{4})(?:\s+года)?)?",
    re.IGNORECASE,
)
_MONTH_YEAR = re.compile(
    r"\b(?:в\s+)?(" + _MONTH_WORD_PATTERN + r")"
    r"\s+(\d{4})(?:\s+года)?\b",
    re.IGNORECASE,
)
_NAMED_MONTH = re.compile(
    r"\b(?:в|на)?\s*(" + _MONTH_WORD_PATTERN + r")\b",
    re.IGNORECASE,
)
_SAME_MONTH_RANGE = re.compile(
    r"\b(?:с|от)\s+(\d{1,2})(?:\s*-?\s*(?:го|е|ое))?\s+"
    r"(?:по|до)\s+(\d{1,2})(?:\s*-?\s*(?:го|е|ое))?\s+"
    r"(" + _MONTH_WORD_PATTERN + r")\b"
    r"(?:\s+(\d{4})(?:\s+года)?)?",
    re.IGNORECASE,
)
_CLOCK = re.compile(r"\b(?:в\s+)?([01]?\d|2[0-3])[:.]([0-5]\d)\b")
_CLOCK_HOUR = re.compile(
    r"\b(?:в|к)\s+([01]?\d|2[0-3])\s*(?:час\w*|ч\b)",
    re.IGNORECASE,
)
_CLOCK_WITH_PERIOD = re.compile(
    r"\b(?:в|к)\s+(0?[1-9]|1[0-2])\s*(?:час\w*|ч\b)?\s*"
    r"(утра|дня|вечера|ночи)\b",
    re.IGNORECASE,
)
_CLOCK_UNIT_WITH_PERIOD = re.compile(
    r"\b(0?[1-9]|1[0-2])\s*(?:час\w*|ч\b)\s*(утра|дня|вечера|ночи)\b",
    re.IGNORECASE,
)
_HOUR_WORDS = {word: value for word, value in _NUMBERS.items() if 1 <= value <= 12}
_SPOKEN_CLOCK_WITH_PREPOSITION = re.compile(
    r"\bв\s+(час|"
    + "|".join(sorted(_HOUR_WORDS, key=len, reverse=True))
    + r")(?:\s+час\w*)?\s+(утра|дня|вечера|ночи)\b",
    re.IGNORECASE,
)
_SPOKEN_CLOCK_WITH_UNIT = re.compile(
    r"\b(" + "|".join(sorted(_HOUR_WORDS, key=len, reverse=True)) + r")\s+час\w*\s+(утра|дня|вечера|ночи)\b",
    re.IGNORECASE,
)
_ORDINAL_DAY = re.compile(
    r"\b(\d{1,2})\s*-?\s*(?:го|е|ое)\b"
    r"(?=\s*(?:$|[?.!,;:]|числ\w*\b|" + _MONTH_WORD_PATTERN + r"\b))",
    re.IGNORECASE,
)
_PAST = re.compile(
    r"\b(?:происход\w*|что\s+(?:уже\s+)?было|что\s+уже\s+произош\w*|"
    r"чем\s+заним\w*|покаж\w*\s+(?:событ\w*|активност\w*|лент\w*)|"
    r"событ\w*\s+(?:появ\w*|произош\w*|был\w*))\b",
    re.IGNORECASE,
)
_PLAN_WORD = r"(?:планир\w*|план(?:а|у|ом|е|ы|ов|ам|ами|ах)?)"
_FUTURE = re.compile(
    r"\b(?:запланир\w*|" + _PLAN_WORD + r"|предсто\w*|намеч\w*|стоит\s+в\s+календар\w*)\b",
    re.IGNORECASE,
)
_PAST_WINDOW_ANCHOR = re.compile(
    r"\b(?:вчера|позавчера|прошл\w*|позапрошл\w*|последн\w*|минувш\w*|"
    r"предыдущ\w*|прошедш\w*|ист[её]кш\w*|назад)\b",
    re.IGNORECASE,
)
_FUTURE_WINDOW_ANCHOR = re.compile(
    r"\b(?:завтра|послезавтра|следующ\w*|ближайш\w*|будущ\w*|грядущ\w*|"
    r"предстоящ\w*|наступающ\w*|через)\b",
    re.IGNORECASE,
)
_TEMPORAL_READ_REQUEST = re.compile(
    r"^\s*(?:(?:а|и|ну)\s+)?"
    r"(?:(?:скаж\w*\s*,?\s*|можно\s+(?:ли\s+)?узна\w*\s*,?\s*|"
    r"мне\s+интересн\w*\s*,?\s*))?"
    r"(?:что|чем|какие|каков\w*|покаж\w*|дай|расскаж\w*|перечисл\w*|"
    r"есть\s+ли|" + _PLAN_WORD + r"|событи\w*|лент\w*|календар\w*)\b",
    re.IGNORECASE,
)
_ABSOLUTE_TIMELINE_READ_ACT = re.compile(
    r"^\s*(?:(?:а|и|ну)\s+)?"
    r"(?:(?:скаж\w*\s*,?\s*|можно\s+(?:ли\s+)?узна\w*\s*,?\s*|"
    r"мне\s+интересн\w*\s*,?\s*))?"
    r"(?:как(?:ое|ой|ая|ие)\b|как\s+называ\w*\b|что\b|покаж\w*\b|"
    r"назов\w*\b|прочит\w*\b|найд\w*\b|сообщ\w*\b|дай\b|привед\w*\b|"
    r"расскаж\w*\b|перечисл\w*\b)",
    re.IGNORECASE,
)
_ABSOLUTE_TIMELINE_SUBJECT = re.compile(
    r"\b(?:событи\w*|хронолог\w*)\b|"
    r"\bвременн\w*\s+(?:лини\w*|индекс\w*)\b|"
    r"\bкалендарн\w*\s+истори\w*\b|"
    r"\bсинтетическ\w*\s+(?:лент\w*|архив\w*)\b|"
    r"\b(?:синтетическ\w*|тестов\w*)\s+(?:факт\w*|запис\w*)\b|"
    r"\bфакт\w*\s+из\s+(?:синтетическ\w*\s+)?лент\w*\b",
    re.IGNORECASE,
)
_NON_TIMELINE_ABSOLUTE_SUBJECT = re.compile(
    r"\b(?:погод\w*|новост\w*|курс\w*|цен\w*|стоимост\w*|прогноз\w*|"
    r"документ\w*|акт(?:а|у|ом|е|ы|ов|ам|ами|ах)?|протокол\w*|"
    r"приказ\w*|файл\w*|вложени\w*|"
    r"таблиц\w*|страниц\w*|текст\w*)\b",
    re.IGNORECASE,
)
_QUOTED_TEMPORAL_DATA = re.compile(
    r"«[^»]*»|“[^”]*”|„[^“]*“|\"[^\"]*\"|'[^']*'",
    re.DOTALL,
)
_EXPLICIT_TIMEZONE = re.compile(
    r"(?<![\w/])(?:UTC|GMT)(?:\s*[+-]\s*(?:[01]?\d|2[0-3])(?::?[0-5]\d)?)?(?!\w)|"
    r"(?<!\w)(?:MSK|CET|CEST|EET|EEST|PST|PDT|MST|MDT|CST|CDT|EST|EDT|JST|МСК)(?!\w)|"
    r"(?<![\w/])[A-Za-z]+/[A-Za-z_+-]+(?:/[A-Za-z_+-]+)?(?![\w/])|"
    r"\b(?:часов\w*\s+пояс\w*|по\s+(?:московск\w*|местн\w*|"
    r"владивостокск\w*|екатеринбургск\w*|калининградск\w*)\s+времен\w*|"
    r"по\s+(?:времен\w+\s+)?(?:москв\w*|нью[- ]йорк\w*|владивосток\w*|"
    r"екатеринбург\w*|калининград\w*)|"
    r"по\s+(?:[а-яё-]{3,40}(?:ск|цк|нск|йск)\w*)\s+времен\w*|"
    r"по\s+времен\w+\s+[а-яёa-z][а-яёa-z-]{2,40})\b",
    re.IGNORECASE,
)


def _month_number(word: str) -> int:
    folded = word.casefold()
    return next((number for prefix, number in _MONTHS.items() if folded.startswith(prefix)), 0)


def _weekday_number(text: str) -> int | None:
    return next((number for pattern, number in _WEEKDAY_WORDS if pattern.search(text)), None)


def has_explicit_timezone(message: str) -> bool:
    """Whether a calendar phrase names a zone this parser cannot silently discard."""

    return bool(_EXPLICIT_TIMEZONE.search(" ".join(str(message or "").split())))


def temporal_routing_text(message: str) -> str:
    """Visible speech owned by the asker, excluding bounded quoted data."""

    text = " ".join(str(message or "").split())
    return " ".join(_QUOTED_TEMPORAL_DATA.sub(" ", text).split())


def _explicit_absolute_dates(message: str) -> list[date]:
    """Valid visible dates whose year is supplied by the asker."""

    text = temporal_routing_text(message)
    hits: list[date] = []
    for match in _ISO_DATE.finditer(text):
        parsed = _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if parsed is not None:
            hits.append(parsed)
    for match in _RUS_DATE.finditer(text):
        parsed = (
            _safe_date(
                int(match.group(3)),
                _month_number(match.group(2)),
                int(match.group(1)),
            )
            if match.group(3)
            else None
        )
        if parsed is not None:
            hits.append(parsed)
    return hits


def _is_absolute_timeline_read_request(message: str) -> bool:
    """Conservative speech-act boundary for a dated timeline/event read.

    The ordinary temporal request grammar intentionally stays small.  This
    second path covers neutral event questions (``which event is attached to
    DATE``) only when the request verb, timeline subject and one fully anchored
    date all agree.  A date in a document, weather question, quote or statement
    therefore receives no authority to read the personal timeline.
    """

    text = temporal_routing_text(message)
    return bool(
        _ABSOLUTE_TIMELINE_READ_ACT.search(text)
        and _ABSOLUTE_TIMELINE_SUBJECT.search(text)
        and not _NON_TIMELINE_ABSOLUTE_SUBJECT.search(text)
        and len(_explicit_absolute_dates(text)) == 1
    )


def is_temporal_read_request(message: str) -> bool:
    """Whether the visible utterance itself asks to read a timeline/calendar."""

    text = temporal_routing_text(message)
    return bool(_TEMPORAL_READ_REQUEST.search(text) or _is_absolute_timeline_read_request(text))


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _month_shift(day: date, offset: int) -> tuple[int, int]:
    absolute = day.year * 12 + (day.month - 1) + offset
    return absolute // 12, absolute % 12 + 1


def _month_window(year: int, month: int) -> tuple[date, date] | None:
    try:
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    except ValueError:
        return None


def _bounded(start: date, end: date) -> TimeWindow | None:
    # Inclusive calendar windows contain one more day than their subtraction.
    # A 60-day public ceiling therefore permits a date delta of at most 59.
    if end < start or (end - start).days >= 60:
        return None
    return TimeWindow(start.isoformat(), end.isoformat())


def _bounded_number(expression: str, *, maximum: int = 60) -> int | None:
    """Parse one bounded Russian cardinal without silently taking its tail.

    Calendar routing only needs 1..60.  Supporting the ordinary ``twenty
    five`` shape is safer than letting a regex begin at ``five`` and turning a
    25-day request into five days; every other composition fails closed.
    """

    folded = " ".join(str(expression or "").casefold().split())
    if folded.isdigit():
        value = int(folded)
    else:
        parts = folded.split()
        values = [_NUMBERS.get(part, 0) for part in parts]
        if len(values) == 1:
            value = values[0]
        elif len(values) == 2 and values[0] in {20, 30, 40, 50} and 1 <= values[1] <= 9:
            value = values[0] + values[1]
        else:
            return None
    return value if 1 <= value <= maximum else None


def _unsupported_quantity_precedes(text: str, position: int) -> bool:
    prefix = str(text or "")[: max(0, position)]
    match = re.search(r"([A-Za-zА-Яа-яЁё-]+)\s*$", prefix)
    return bool(match and _UNSUPPORTED_QUANTITY_TOKEN.fullmatch(match.group(1)))


def _nearest_implicit_date(month: int, day: int, today: date, direction: str) -> date | None:
    if direction == "past":
        years = range(today.year, today.year - 9, -1)
    elif direction == "future":
        years = range(today.year, today.year + 9)
    else:
        years = range(today.year, today.year + 9)
    for year in years:
        parsed = _safe_date(year, month, day)
        if parsed is None:
            continue
        if direction == "past" and parsed > today:
            continue
        if direction == "future" and parsed < today:
            continue
        return parsed
    return None


def _absolute_dates(text: str, today: date, *, direction: str = "") -> list[date]:
    # Keep raw month/day components until range context supplies the year.  A
    # leap day omitted on the left of ``... по 1 марта 2024`` is invalid in the
    # current non-leap year but perfectly valid in the explicitly bound year.
    hits: list[tuple[int, int, int, int | None]] = []
    for match in _ISO_DATE.finditer(text):
        hits.append(
            (
                match.start(),
                int(match.group(2)),
                int(match.group(3)),
                int(match.group(1)),
            )
        )
    for match in _RUS_DATE.finditer(text):
        hits.append(
            (
                match.start(),
                _month_number(match.group(2)),
                int(match.group(1)),
                int(match.group(3)) if match.group(3) else None,
            )
        )
    ordered = sorted(hits)
    resolved: list[date] = []
    implicit: list[bool] = []
    for index, (_, month, day, explicit_year) in enumerate(ordered):
        parsed = _safe_date(explicit_year, month, day) if explicit_year is not None else None
        if explicit_year is None:
            right_anchor: date | None = None
            for _, later_month, later_day, later_year in ordered[index + 1 :]:
                if later_year is None:
                    continue
                right_anchor = _safe_date(later_year, later_month, later_day)
                if right_anchor is not None:
                    break
            if right_anchor is not None:
                # In a Russian range the year is commonly written once, after
                # the right endpoint.  It binds the omitted left endpoint too;
                # a December-to-January range therefore starts one year before
                # that explicitly named January.
                year = right_anchor.year - int((month, day) > (right_anchor.month, right_anchor.day))
                parsed = _safe_date(year, month, day)
            elif resolved:
                previous = resolved[-1]
                year = previous.year + int((month, day) < (previous.month, previous.day))
                while parsed is None and year <= previous.year + 8:
                    parsed = _safe_date(year, month, day)
                    year += 1
            else:
                parsed = _nearest_implicit_date(month, day, today, direction)
        if parsed is None:
            continue
        resolved.append(parsed)
        implicit.append(explicit_year is None)
    if len(resolved) > 1 and all(implicit):
        shift = -1 if direction == "past" and resolved[-1] > today else 0
        shift = 1 if direction == "future" and resolved[0] < today else shift
        if shift:
            shifted = [_safe_date(item.year + shift, item.month, item.day) for item in resolved]
            if all(item is not None for item in shifted):
                resolved = [item for item in shifted if item is not None]
    return resolved


def _number_near_days(text: str) -> int | None:
    match = re.search(
        r"\b(" + _NUMBER_TOKEN_PATTERN + r")\s+(?:дн\w*|ден\w*|сут\w*)\b",
        text,
        re.IGNORECASE,
    )
    if not match or _unsupported_quantity_precedes(text, match.start(1)):
        return None
    return _bounded_number(match.group(1))


def _relative_week_or_month(text: str, today: date, direction: str) -> date | None:
    match = _RELATIVE_UNIT_FUTURE.search(text) if direction == "future" else _RELATIVE_UNIT_PAST.search(text)
    if not match:
        return None
    quantity_position = match.start(1) if match.group(1) is not None else match.start(2)
    if _unsupported_quantity_precedes(text, quantity_position):
        return None
    amount = _bounded_number(str(match.group(1) or "один"))
    if amount is None:
        return None
    sign = 1 if direction == "future" else -1
    unit = match.group(2).casefold()
    if unit.startswith("недел"):
        return today + timedelta(days=sign * amount * 7)
    year, month = _month_shift(today, sign * amount)
    return _safe_date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def _hour_with_period(hour: int, period: str) -> int:
    name = period.casefold()
    if name == "утра":
        return 0 if hour == 12 else hour
    if name == "ночи":
        if hour == 12:
            return 0
        return hour + 12 if 9 <= hour <= 11 else hour
    if hour == 12:
        return 12 if name == "дня" else 0
    return hour + 12


def _clock_from_text(text: str) -> tuple[int, int] | None:
    """Return a named wall-clock without asking the semantic arbiter for numbers."""

    explicit = _CLOCK.search(text)
    if explicit:
        return int(explicit.group(1)), int(explicit.group(2))
    folded = text.casefold()
    if re.search(r"\bполноч\w*", folded):
        return 0, 0
    if re.search(r"\bполд(?:ень|ня)\b", folded):
        return 12, 0
    numeric_period = _CLOCK_WITH_PERIOD.search(text) or _CLOCK_UNIT_WITH_PERIOD.search(text)
    if numeric_period:
        return _hour_with_period(int(numeric_period.group(1)), numeric_period.group(2)), 0
    spoken = _SPOKEN_CLOCK_WITH_PREPOSITION.search(text) or _SPOKEN_CLOCK_WITH_UNIT.search(text)
    if spoken:
        token = spoken.group(1).casefold()
        hour = 1 if token == "час" else _HOUR_WORDS[token]
        return _hour_with_period(hour, spoken.group(2)), 0
    numeric_hour = _CLOCK_HOUR.search(text)
    if numeric_hour:
        return int(numeric_hour.group(1)), 0
    return None


def _relative_day(text: str, today: date) -> date | None:
    folded = text.casefold()
    if "послезавтра" in folded:
        return today + timedelta(days=2)
    if re.search(r"\bзавтра\b", folded):
        return today + timedelta(days=1)
    if "позавчера" in folded:
        return today - timedelta(days=2)
    if re.search(r"\bвчера\b", folded):
        return today - timedelta(days=1)
    if re.search(r"\bсегодня\b", folded):
        return today
    if "назад" in folded:
        amount = _number_near_days(folded)
        if amount is not None:
            return today - timedelta(days=amount)
    # A syntactically complete day+month which failed date validation must not
    # degrade into the ordinal-day shorthand.  ``31-го февраля`` is invalid,
    # not the nearest past 31st of an unrelated month.
    if _RUS_DATE.search(folded):
        return None
    ordinal = _ORDINAL_DAY.search(folded)
    if ordinal:
        wanted = int(ordinal.group(1))
        year, month = today.year, today.month
        for _ in range(14):
            parsed = _safe_date(year, month, wanted)
            if parsed is not None and parsed <= today:
                return parsed
            month -= 1
            if month == 0:
                year, month = year - 1, 12
    return None


def _weekday_on_or_after(text: str, start: date) -> date | None:
    target = _weekday_number(text)
    if target is None:
        return None
    return start + timedelta(days=(target - start.weekday()) % 7)


def has_mixed_time_direction(message: str) -> bool:
    """Whether separate clauses ask both backward and forward calendar reads."""

    clauses = [
        clause.strip()
        for clause in re.split(
            r"(?:\s+\b(?:и|а)\b\s+|\s+с\s+тем,?\s+|\s+затем\s+|,\s*(?:затем\s+)?|[;!?]+)",
            str(message or ""),
            flags=re.IGNORECASE,
        )
        if clause.strip()
    ]
    if len(clauses) < 2:
        return False
    classified: list[tuple[bool, bool]] = []
    for clause in clauses:
        past = bool(
            _PAST.search(clause)
            or re.search(r"\b(?:был|была|было|были|вчерашн|прошедш)\w*\b", clause, re.IGNORECASE)
            or re.search(
                r"\b(?:лент|событ|активност)\w*\b[^.!?]{0,40}\b(?:за\s+)?вчера\b",
                clause,
                re.IGNORECASE,
            )
        )
        future = bool(
            _FUTURE.search(clause)
            or re.search(r"\b(?:будет|будут|завтрашн)\w*\b", clause, re.IGNORECASE)
            or re.search(
                r"\bкалендар\w*\b[^.!?]{0,40}\b(?:на\s+)?завтра\b",
                clause,
                re.IGNORECASE,
            )
        )
        classified.append((past, future))
    return any(past and not future for past, future in classified) and any(
        future and not past for past, future in classified
    )


def _merged_span_count(spans: list[tuple[int, int]]) -> int:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return len(merged)


def has_multiple_time_targets(message: str) -> bool:
    """Whether one direction names a union of targets the tools cannot express."""

    text = " ".join(str(message or "").split())
    range_like = bool(re.search(r"\b(?:с|от|между)\b.{1,100}\b(?:по|до|и)\b", text, re.IGNORECASE))
    date_spans = [
        match.span()
        for match in re.finditer(
            r"\b(?:послезавтра|позавчера|завтра|вчера|сегодня)\b",
            text,
            re.IGNORECASE,
        )
    ]
    date_spans.extend(match.span() for match in _ISO_DATE.finditer(text))
    date_spans.extend(match.span() for match in _RUS_DATE.finditer(text))
    date_spans.extend(
        match.span()
        for prefix in _WEEKDAYS
        for match in re.finditer(rf"\b{re.escape(prefix)}\w*\b", text, re.IGNORECASE)
    )

    # In Russian same-month ranges/lists normally omit the month on their first
    # endpoint (``с 1 по 3 августа`` / ``7 и 8 августа``).  Count that bare
    # endpoint explicitly; it is not matched by ``_RUS_DATE``.
    shorthand = re.search(
        r"\b(\d{1,2})(?:\s*-?\s*(?:го|е|ое))?\s+(?:по|до|и|,)\s+"
        r"(\d{1,2})(?:\s*-?\s*(?:го|е|ое))?\s+"
        r"(?:январ|феврал|март|апрел|ма[йяе]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*",
        text,
        re.IGNORECASE,
    )
    if shorthand:
        date_spans.append(shorthand.span(1))
    date_count = _merged_span_count(date_spans)
    if date_count > 1 and not (range_like and date_count == 2):
        return True

    month_spans = [match.span() for match in _NAMED_MONTH.finditer(text)]
    if _merged_span_count(month_spans) > 1 and not (range_like and date_count == 2):
        return True
    has_week = bool(re.search(r"\bнедел\w*\b", text, re.IGNORECASE))
    has_month = bool(month_spans or re.search(r"\bмесяц\w*\b", text, re.IGNORECASE))
    if has_week and has_month:
        return True
    # Spoken ordinal dates are not parsed by ``_RUS_DATE``.  Letting one fall
    # through turns ``седьмого августа`` into the whole month; a conjunction
    # such as ``первого и второго августа`` is worse still.  Until the exact
    # day parser owns these forms, fail closed instead of silently widening.
    if re.search(
        r"\b(?:перв|втор|треть|четв[её]рт|пят|шест|седьм|восьм|девят|десят|"
        r"одиннадцат|двенадцат|тринадцат|четырнадцат|пятнадцат|шестнадцат|"
        r"семнадцат|восемнадцат|девятнадцат|двадцат|тридцат)\w*\s+"
        r"(?:январ|феврал|март|апрел|ма[йяе]|июн|июл|август|сентябр|октябр|"
        r"ноябр|декабр)\w*\b",
        text,
        re.IGNORECASE,
    ):
        return True
    quantified_period = re.search(
        r"\b(?:последн|минувш|прошл|прошедш|следующ|будущ|грядущ|предстоящ|"
        r"наступающ|ближайш)\w*\s+"
        r"([A-Za-zА-Яа-яЁё0-9-]+(?:\s+[A-Za-zА-Яа-яЁё0-9-]+)?)\s+"
        r"(?:недел|месяц)\w*\b",
        text,
        re.IGNORECASE,
    )
    if quantified_period:
        amount = _bounded_number(quantified_period.group(1))
        if amount is None or amount > 1:
            return True
    # Case-inflected cardinals occur after ``в течение``/``около`` and are not
    # members of the nominative/accusative parser used for exact relative
    # targets (``через две недели``).  If silently ignored, ``двух недель``
    # collapses to the current week.  These forms all denote more than one
    # period and therefore cannot be represented by one calendar window here.
    if re.search(
        r"\b(?:двух|обоих|тр[её]х|четыр[её]х|пяти|шести|семи|восьми|девяти|"
        r"десяти|одиннадцати|двенадцати|тринадцати|четырнадцати|пятнадцати|"
        r"шестнадцати|семнадцати|восемнадцати|девятнадцати|двадцати|тридцати|"
        r"сорока|пятидесяти|шестидесяти)(?:\s+(?:одной|двух|тр[её]х|четыр[её]х|"
        r"пяти|шести|семи|восьми|девяти))?\s+(?:недел|месяц)\w*\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if re.search(
        r"\b(?:" + _NUMBER_TOKEN_PATTERN + r")\s+с\s+половин\w*\s+"
        r"(?:недел|месяц)\w*\b",
        text,
        re.IGNORECASE,
    ):
        return True
    for match in re.finditer(
        r"\b(" + _NUMBER_TOKEN_PATTERN + r")\s+(?:недел|месяц)\w*\b",
        text,
        re.IGNORECASE,
    ):
        amount = _bounded_number(match.group(1))
        if _unsupported_quantity_precedes(text, match.start(1)):
            amount = None
        prefix = text[: match.start()].casefold().rstrip()
        suffix = text[match.end() :].casefold().lstrip()
        relative_single_target = bool(re.search(r"\bчерез\s*$", prefix) or re.match(r"^назад\b", suffix))
        if (amount is None or amount > 1) and not relative_single_target:
            return True
    unsupported_period_quantity = re.search(
        r"\b([A-Za-zА-Яа-яЁё-]+)\s+(?:недел|месяц)\w*\b",
        text,
        re.IGNORECASE,
    )
    if unsupported_period_quantity and _UNSUPPORTED_QUANTITY_TOKEN.fullmatch(
        unsupported_period_quantity.group(1)
    ):
        prefix = text[: unsupported_period_quantity.start()].casefold().rstrip()
        suffix = text[unsupported_period_quantity.end() :].casefold().lstrip()
        if not (re.search(r"\bчерез\s*$", prefix) or re.match(r"^назад\b", suffix)):
            return True
    clock_patterns = (
        _CLOCK,
        _CLOCK_HOUR,
        _CLOCK_WITH_PERIOD,
        _CLOCK_UNIT_WITH_PERIOD,
        _SPOKEN_CLOCK_WITH_PREPOSITION,
        _SPOKEN_CLOCK_WITH_UNIT,
    )
    clock_spans = [match.span() for pattern in clock_patterns for match in pattern.finditer(text)]
    return _merged_span_count(clock_spans) > 1


def has_relational_clock_boundary(message: str) -> bool:
    """A named clock modified by before/after is a range, not one hour."""

    text = " ".join(str(message or "").split())
    return bool(
        re.search(
            r"\b(?:(?:не\s+)?раньше|(?:не\s+)?позже|после|до|с|по)\s+(?:в\s+)?(?:"
            r"(?:[0-2]?\d:[0-5]\d)|(?:[0-2]?\d\s*(?:час\w*|ч\b))|"
            r"[0-2]?\d\s+(?:утра|дня|вечера|ночи)|"
            r"[а-яё]+\s+час\w*|[а-яё]+\s+(?:утра|дня|вечера|ночи)|"
            r"полудн\w*|полуноч\w*|"
            r"(?:[01]?\d|2[0-3]|часа|одного|двух|тр[её]х|четыр[её]х|пяти|"
            r"шести|семи|восьми|девяти|десяти|одиннадцати|двенадцати|"
            r"тринадцати|четырнадцати|пятнадцати|шестнадцати|семнадцати|"
            r"восемнадцати|девятнадцати|двадцати)(?=\s*(?:[?.!,;:]|$)))",
            text,
            re.IGNORECASE,
        )
    )


def has_unsupported_time_granularity(message: str) -> bool:
    """Whether a temporal phrase would otherwise be widened beyond what was asked."""

    text = " ".join(str(message or "").split())
    hour_words = "|".join(sorted(_HOUR_WORDS, key=len, reverse=True))
    bare_clock = re.search(
        rf"\bв\s+(?:[01]?\d|2[0-3]|{hour_words})\b(?=\s*(?:[?.!,;]|$))",
        text,
        re.IGNORECASE,
    )
    approximate_clock = re.search(
        rf"\b(?:около|примерно|приблизительно)\s+(?:в\s+)?"
        rf"(?:[01]?\d|2[0-3]|{hour_words}|часа|одного|двух|тр[её]х|"
        rf"четыр[её]х|пяти|шести|семи|восьми|девяти|десяти|одиннадцати|"
        rf"двенадцати)(?:\s+час\w*)?\b",
        text,
        re.IGNORECASE,
    )
    part_of_day = re.search(r"\b(?:утром|дн[её]м|вечером|ночью)\b", text, re.IGNORECASE)
    # The frozen routing contract deliberately treats a future "today in the
    # evening" overview as today's calendar.  Preserve that one broad overview;
    # past/date-qualified sub-day filters still cannot be represented exactly.
    if part_of_day and _FUTURE.search(text) and re.search(r"\bсегодня\b", text, re.IGNORECASE):
        part_of_day = None
    vague_calendar_part = re.search(
        r"\b(?:в|на)\s+(?:сам\w*\s+)?(?:начал|кон(?:ец|ц)|середин)\w*\s+"
        r"(?:недел|месяц|январ|феврал|март|апрел|ма[йяе]|июн|июл|август|"
        r"сентябр|октябр|ноябр|декабр|год)\w*\b",
        text,
        re.IGNORECASE,
    )
    invalid_day_quantity = re.search(
        r"(?<![\w])(?:[+-]\s*\d{1,3}|\d{1,3}[.,/]\d{1,3})\s*"
        r"(?:дн\w*|ден\w*|сут\w*)\b",
        text,
        re.IGNORECASE,
    )
    return bool(bare_clock or approximate_clock or part_of_day or vague_calendar_part or invalid_day_quantity)


def has_invalid_clock_expression(message: str) -> bool:
    """Whether clock-looking input is present but outside a real wall clock."""

    text = " ".join(str(message or "").split())
    for match in re.finditer(r"(?<!\d)(\d{1,3}):([0-9]{2})(?!\d)", text):
        if int(match.group(1)) > 23 or int(match.group(2)) > 59:
            return True
    return any(
        int(match.group(1)) > 23
        for match in re.finditer(
            r"\b(?:в|к|после|до|раньше|позже)\s+(\d{1,3})\s*(?:час\w*|ч\b)",
            text,
            re.IGNORECASE,
        )
    )


def lexical_time_window_kind(message: str, *, today: date | None = None) -> str | None:
    """Derive a closed window shape from calendar words, independent of intent.

    A semantic arbiter may decide whether a sentence asks for past or future
    personal time.  It may not reinterpret one explicit day as a month or one
    named month as a week: that would silently widen the code-owned query.
    """

    text = temporal_routing_text(message)
    folded = text.casefold()
    explicit_range = bool(re.search(r"\b(?:с|от|между)\b.+\b(?:по|до|и)\b", folded))
    current_period_to_now = bool(
        re.search(
            r"\bс\s+начала\s+(?:эт\w*|текущ\w*)\s+(?:недел|месяц)\w*\s+до\s+"
            r"(?:текущ\w+\s+момент\w*|сегодня|сейчас)\b",
            folded,
        )
    )
    if explicit_range and not current_period_to_now:
        return "explicit_range"
    if re.search(
        r"\b(?:(?:эт|прошл|следующ)\w*\s+недел\w*|"
        r"(?:с\s+начала|до\s+конца)\s+(?:эт\w*\s+)?недел\w*)\b",
        folded,
    ):
        return "calendar_week"
    if re.search(
        r"\b(?:с\s+начала|до\s+конца)\s+(?:эт\w*\s+)?месяц\w*\b",
        folded,
    ):
        return "calendar_month"
    if explicit_range:
        # The one safe range-looking shorthand above is exactly the elapsed
        # part of one current calendar container.
        return "calendar_week" if "недел" in folded else "calendar_month"
    if _RELATIVE_UNIT_FUTURE.search(folded) or _RELATIVE_UNIT_PAST.search(folded):
        return "single_day"
    if re.search(
        r"(?:\b(?:последн\w*|минувш\w*|ближайш\w*)\b.+|\bна\s+)"
        r"(?:" + _NUMBER_TOKEN_PATTERN + r")\s+(?:дн\w*|ден\w*|сут\w*)\b",
        folded,
    ):
        return "rolling_days"
    if re.search(r"\bнедел\w*\b", folded):
        return "calendar_week"
    if _clock_from_text(folded) is not None:
        return "single_hour"
    local_today = today or date.today()
    if (
        _ISO_DATE.search(folded)
        or _RUS_DATE.search(folded)
        or _relative_day(folded, local_today)
        or _weekday_number(folded) is not None
    ):
        return "single_day"
    if "месяц" in folded or _MONTH_YEAR.search(folded) or _NAMED_MONTH.search(folded):
        return "calendar_month"
    if re.search(r"\bкогда(?:-|\s)?нибудь\b", folded):
        # An explicit but unbounded time request remains temporal so the
        # runtime can ask for clarification instead of exposing a broad tool.
        return "single_day"
    return None


def fast_time_intent(message: str, *, today: date | None = None) -> TimeIntent | None:
    """Cheap unambiguous route; semantic misses go to the model arbiter."""

    text = temporal_routing_text(message)
    folded = text.casefold()
    if not is_temporal_read_request(text):
        return None
    # A strong non-timeline subject wins over generic words such as
    # ``покажи событие``.  Otherwise a dated document or weather event would
    # take the older broad ``покажи события`` past route before the conservative
    # absolute-date classifier gets a chance to reject it.
    if _NON_TIMELINE_ABSOLUTE_SUBJECT.search(text) and _explicit_absolute_dates(text):
        return None
    past_state = bool(
        re.search(r"\b(?:был|была|было|были)\b", folded)
        and re.search(r"\b(?:вчера|позавчера|прошл\w*|минувш\w*)\b", folded)
    )
    future_anchor = bool(
        re.search(
            r"\b(?:завтра|послезавтра|будущ\w*|следующ\w*|ближайш\w*|грядущ\w*|"
            r"предстоящ\w*|наступающ\w*)\b",
            folded,
        )
    )
    # A declarative sentence about plans that *were* scheduled yesterday is
    # neither a request for the future calendar nor a request to read the past
    # timeline.  Let the closed arbiter return none instead of routing from the
    # temporal vocabulary alone.
    if past_state and not future_anchor and not _PAST.search(text):
        direction = ""
    else:
        direction = "past" if _PAST.search(text) else "future" if _FUTURE.search(text) else ""
    if not direction and _is_absolute_timeline_read_request(text):
        anchored = _explicit_absolute_dates(text)
        local_today = today or date.today()
        if len(anchored) == 1 and anchored[0] != local_today:
            direction = "past" if anchored[0] < local_today else "future"
    if not direction:
        return None
    kind = lexical_time_window_kind(text, today=today)
    if kind is None:
        return None
    return TimeIntent(direction, kind)


def build_time_window(message: str, intent: TimeIntent, *, today: date) -> TimeWindow | None:
    """Anchor a closed intent to an inclusive, at-most-60-day local interval."""

    if intent.direction not in {"past", "future"} or intent.window_kind not in TIME_WINDOW_KINDS:
        return None
    text = temporal_routing_text(message)
    folded = text.casefold()

    # The classifier owns only direction/kind, not permission to rewrite the
    # person's lexical window.  A future intent over "last week" or a past
    # intent over "next week" is contradictory and must fail closed.
    if intent.direction == "future" and _PAST_WINDOW_ANCHOR.search(folded):
        return None
    if intent.direction == "past" and _FUTURE_WINDOW_ANCHOR.search(folded):
        return None

    lexical_kind = lexical_time_window_kind(text, today=today)
    if lexical_kind is not None and lexical_kind != intent.window_kind:
        return None

    relative_unit_day = _relative_week_or_month(folded, today, intent.direction)
    if relative_unit_day is not None:
        return _bounded(relative_unit_day, relative_unit_day)
    if _RELATIVE_UNIT_SHAPE.search(folded):
        # A relative unit with an unsupported/ambiguous quantity (``через
        # несколько недель``, ``полтора месяца назад``) must not collapse into
        # the current calendar week/month.
        return None

    if intent.window_kind == "explicit_range":
        # V1 represents explicit ranges as whole inclusive dates.  A named
        # wall-clock on either endpoint cannot be silently discarded: doing so
        # widens e.g. ``по 10:00 2 августа`` through the end of that day.
        if _clock_from_text(text) is not None or has_relational_clock_boundary(text):
            return None
        same_month = _SAME_MONTH_RANGE.search(text)
        if same_month:
            explicit_year = bool(same_month.group(4))
            year = int(same_month.group(4) or today.year)
            month = _month_number(same_month.group(3))
            start = _safe_date(year, month, int(same_month.group(1)))
            end = _safe_date(year, month, int(same_month.group(2)))
            if start is not None and end is not None and not explicit_year:
                if intent.direction == "past" and end > today:
                    start = _safe_date(year - 1, month, start.day)
                    end = _safe_date(year - 1, month, end.day)
                elif intent.direction == "future" and start < today:
                    start = _safe_date(year + 1, month, start.day)
                    end = _safe_date(year + 1, month, end.day)
            return _bounded(start, end) if start is not None and end is not None else None
        dates = _absolute_dates(text, today, direction=intent.direction)
        if len(dates) >= 2:
            return _bounded(dates[0], dates[1])
        if "сегодня" in folded:
            end = _weekday_on_or_after(folded.split("до", 1)[-1], today)
            return _bounded(today, end) if end is not None else None
        return None

    if intent.window_kind == "rolling_days":
        amount = _number_near_days(text)
        if amount is None:
            return None
        if intent.direction == "past":
            return _bounded(today - timedelta(days=amount - 1), today)
        return _bounded(today, today + timedelta(days=amount - 1))

    if intent.window_kind == "calendar_week":
        monday = today - timedelta(days=today.weekday())
        if intent.direction == "past" and "позапрошл" in folded:
            return _bounded(monday - timedelta(days=14), monday - timedelta(days=8))
        if intent.direction == "past" and re.search(
            r"\b(?:прошл|минувш|предыдущ|прошедш|ист[её]кш)\w*", folded
        ):
            return _bounded(monday - timedelta(days=7), monday - timedelta(days=1))
        if intent.direction == "future" and re.search(
            r"\b(?:следующ|будущ|грядущ|предстоящ|наступающ)\w*", folded
        ):
            return _bounded(monday + timedelta(days=7), monday + timedelta(days=13))
        if intent.direction == "past":
            return _bounded(monday, today)
        return _bounded(today, monday + timedelta(days=6))

    if intent.window_kind == "calendar_month":
        named = _MONTH_YEAR.search(text)
        if named:
            year = int(named.group(2))
            month = _month_number(named.group(1))
            window = _month_window(year, month)
            if window is None:
                return None
            if year == today.year and month == today.month:
                return (
                    _bounded(window[0], today) if intent.direction == "past" else _bounded(today, window[1])
                )
            return _bounded(*window)
        named_without_year = _NAMED_MONTH.search(text)
        if named_without_year:
            month = _month_number(named_without_year.group(1))
            year = today.year
            if intent.direction == "past" and month > today.month:
                year -= 1
            elif intent.direction == "future" and month < today.month:
                year += 1
            window = _month_window(year, month)
            if window is None:
                return None
            if year == today.year and month == today.month:
                return (
                    _bounded(window[0], today) if intent.direction == "past" else _bounded(today, window[1])
                )
            return _bounded(*window)
        if "позапрошл" in folded:
            year, month = _month_shift(today, -2)
            window = _month_window(year, month)
            return _bounded(*window) if window is not None else None
        if re.search(r"\b(?:прошл|минувш|предыдущ|прошедш|ист[её]кш)\w*", folded):
            year, month = _month_shift(today, -1)
            window = _month_window(year, month)
            return _bounded(*window) if window is not None else None
        if re.search(r"\b(?:следующ|будущ|грядущ|предстоящ|наступающ)\w*", folded):
            year, month = _month_shift(today, 1)
            window = _month_window(year, month)
            return _bounded(*window) if window is not None else None
        current = _month_window(today.year, today.month)
        if current is None:
            return None
        return _bounded(current[0], today) if intent.direction == "past" else _bounded(today, current[1])

    absolute = _absolute_dates(text, today, direction=intent.direction)
    day = absolute[0] if absolute else _relative_day(text, today)
    if day is None:
        target = _weekday_number(folded)
        if target is not None:
            if intent.direction == "past":
                offset = (today.weekday() - target) % 7
                if offset == 0 and re.search(r"\b(?:прошл|минувш|предыдущ|прошедш|ист[её]кш)\w*", folded):
                    offset = 7
                day = today - timedelta(days=offset)
            else:
                offset = (target - today.weekday()) % 7
                if offset == 0 and re.search(r"\b(?:следующ|будущ|грядущ|предстоящ|наступающ)\w*", folded):
                    offset = 7
                day = today + timedelta(days=offset)
    if day is None:
        return None
    if intent.direction == "past" and day > today:
        return None
    if intent.direction == "future" and day < today:
        return None
    if intent.window_kind == "single_hour":
        clock = _clock_from_text(text)
        if clock is None:
            return None
        hour, minute = clock
        return TimeWindow(
            f"{day.isoformat()}T{hour:02d}:{minute:02d}:00",
            f"{day.isoformat()}T{hour:02d}:59:59",
        )
    if intent.window_kind == "single_day":
        return _bounded(day, day)
    return None
