"""Closed temporal constraint updates for ``RecallConversation`` work items.

The first durable continuation slice accepts only a single local calendar day.
It deliberately does not interpret actions, people, replies, clocks, ranges or
time-zone text: all of those stay on the ordinary routing path.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MAX_SURFACE_LENGTH = 128
_IANA_ZONE_RE = re.compile(r"[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*")
_RUSSIAN_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_MONTH_PATTERN = "|".join(_RUSSIAN_MONTHS)
_FOLLOWUP_RE = re.compile(
    rf"^(?:(?:а|и),? )?(?:за )?(?:"
    rf"(?P<relative>сегодня|вчера|позавчера)|"
    rf"(?P<iso>[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})|"
    rf"(?P<numeric>[0-9]{{1,2}}(?P<separator>[./])[0-9]{{1,2}}(?P=separator)[0-9]{{4}})|"
    rf"(?P<day>[0-9]{{1,2}})(?:-?(?:го|е|ое))? "
    rf"(?P<month>{_MONTH_PATTERN})(?: (?P<year>[0-9]{{4}})(?: года)?)?"
    rf")\??$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class MessageWindowTemporalUpdate:
    """One exact local day represented as a half-open UTC interval."""

    local_date: str
    since_utc: str
    until_utc: str


def _canonical_surface(message: object) -> str | None:
    if not isinstance(message, str) or not message or len(message) > _MAX_SURFACE_LENGTH:
        return None
    if any(unicodedata.category(character).startswith("C") for character in message):
        return None
    surface = " ".join(message.split())
    if not surface or len(surface) > _MAX_SURFACE_LENGTH:
        return None
    return surface.casefold()


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _nearest_past_date(*, month: int, day: int, today: date) -> date | None:
    """Resolve an omitted year without ever selecting a future calendar day."""

    # Four hundred years cover one complete Gregorian leap-year cycle.
    for year in range(today.year, max(0, today.year - 400), -1):
        candidate = _safe_date(year, month, day)
        if candidate is not None and candidate <= today:
            return candidate
    return None


def _target_date(match: re.Match[str], *, today: date) -> date | None:
    relative = match.group("relative")
    if relative == "сегодня":
        return today
    if relative == "вчера":
        return today - timedelta(days=1)
    if relative == "позавчера":
        return today - timedelta(days=2)

    iso = match.group("iso")
    if iso is not None:
        try:
            return date.fromisoformat(iso)
        except ValueError:
            return None

    numeric = match.group("numeric")
    if numeric is not None:
        separator = match.group("separator")
        day_text, month_text, year_text = numeric.split(separator)
        return _safe_date(int(year_text), int(month_text), int(day_text))

    month_text = match.group("month")
    day_text = match.group("day")
    if month_text is None or day_text is None:
        return None
    month = _RUSSIAN_MONTHS.get(month_text.casefold())
    if month is None:
        return None
    year_text = match.group("year")
    if year_text is not None:
        return _safe_date(int(year_text), month, int(day_text))
    return _nearest_past_date(month=month, day=int(day_text), today=today)


def _iana_zone(timezone_name: object) -> ZoneInfo:
    if (
        not isinstance(timezone_name, str)
        or not timezone_name
        or timezone_name != timezone_name.strip()
        or len(timezone_name) > 128
        or _IANA_ZONE_RE.fullmatch(timezone_name) is None
        or any(part in {"", ".", ".."} for part in timezone_name.split("/"))
    ):
        raise ValueError("timezone_name must be an installed IANA zone")
    try:
        return ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone_name must be an installed IANA zone") from exc


def _local_midnight(day: date, zone: ZoneInfo) -> datetime | None:
    try:
        local = datetime.combine(day, time.min, tzinfo=zone)
        # A few historical IANA transitions skipped a local calendar date.  Never
        # widen such an absent day to whichever instant ``zoneinfo`` guesses.
        round_trip = local.astimezone(UTC).astimezone(zone)
    except (OverflowError, ValueError):
        return None
    return local if round_trip.date() == day and round_trip.time() == time.min else None


def parse_recall_conversation_temporal_followup(
    message: str,
    *,
    timezone_name: str,
    today: date,
) -> MessageWindowTemporalUpdate | None:
    """Parse one temporal-only follow-up into canonical half-open UTC bounds.

    ``today`` is the caller's already established local calendar date.  Invalid
    user text and future/unsupported dates return ``None``; invalid controller
    inputs raise ``TypeError``/``ValueError`` rather than silently changing the
    authority scope.
    """

    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date")
    zone = _iana_zone(timezone_name)
    surface = _canonical_surface(message)
    if surface is None:
        return None
    match = _FOLLOWUP_RE.fullmatch(surface)
    if match is None:
        return None
    try:
        target = _target_date(match, today=today)
    except OverflowError:
        return None
    if target is None or target > today:
        return None
    try:
        next_day = target + timedelta(days=1)
    except OverflowError:
        return None
    local_start = _local_midnight(target, zone)
    local_end = _local_midnight(next_day, zone)
    if local_start is None or local_end is None:
        return None
    try:
        start = local_start.astimezone(UTC)
        end = local_end.astimezone(UTC)
    except (OverflowError, ValueError):
        return None
    if start >= end:
        return None
    return MessageWindowTemporalUpdate(
        local_date=target.isoformat(),
        since_utc=start.isoformat(timespec="seconds"),
        until_utc=end.isoformat(timespec="seconds"),
    )


__all__ = [
    "MessageWindowTemporalUpdate",
    "parse_recall_conversation_temporal_followup",
]
