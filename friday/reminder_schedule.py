"""Small, private scheduling contract shared by reminder creation and delivery."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any

_CLOCK_PREFIX = "friday-reminder-clock:"
_CLOCK = re.compile(r"^friday-reminder-clock:(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)$")


def reminder_clock_description(clock: str) -> str:
    """Encode only a validated clock, never reminder content, in entity metadata."""

    value = str(clock or "").strip()
    return f"{_CLOCK_PREFIX}{value}" if _CLOCK.fullmatch(f"{_CLOCK_PREFIX}{value}") else ""


def reminder_clock(value: Mapping[str, Any] | str) -> str:
    """Read the optional clock from a bounded event projection/description."""

    description = str(value.get("description") or "") if isinstance(value, Mapping) else str(value or "")
    match = _CLOCK.fullmatch(description.strip())
    if not match:
        return ""
    return f"{match.group('hour')}:{match.group('minute')}"


def reminder_is_due(event: Mapping[str, Any], now: datetime, *, lead_days: int) -> bool:
    """Whether a projected event belongs in the outbound queue at ``now``.

    Personal reminders are never queued before their requested day/time; a
    bounded lookback lets one missed during downtime arrive after restart.
    Events extracted from documents keep the historical advance lead window.
    """

    raw_day = str(event.get("occurred_at") or "")[:10]
    try:
        event_day = date.fromisoformat(raw_day)
    except ValueError:
        return False
    today = now.date()
    source = str(event.get("source") or "")
    clock = reminder_clock(event) if source.startswith("reminder:") else ""
    if clock:
        hour, minute = (int(part) for part in clock.split(":", 1))
        scheduled = datetime.combine(event_day, time(hour, minute), tzinfo=now.tzinfo)
        return scheduled <= now and now - scheduled <= timedelta(days=7)
    if source.startswith("reminder:"):
        return event_day <= today and today - event_day <= timedelta(days=7)
    return today <= event_day <= today + timedelta(days=max(0, int(lead_days)))


def reminder_when_text(event: Mapping[str, Any], today: date) -> str:
    raw_day = str(event.get("occurred_at") or "")[:10]
    when = raw_day
    if raw_day == today.isoformat():
        when = "сегодня"
    elif raw_day == (today + timedelta(days=1)).isoformat():
        when = "завтра"
    clock = reminder_clock(event)
    return f"{when} в {clock}" if clock else when
