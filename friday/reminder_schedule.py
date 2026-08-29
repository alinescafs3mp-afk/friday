"""Small, private scheduling contract shared by reminder creation and delivery."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

_CLOCK_PREFIX = "friday-reminder-clock:"
_CLOCK = re.compile(r"^friday-reminder-clock:(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)$")

ReminderDueState = Literal["early", "due", "expired", "invalid"]


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


def reminder_due_state(event: Mapping[str, Any], now: datetime, *, lead_days: int) -> ReminderDueState:
    """Classify one projected event against its closed delivery window.

    Personal reminders are never queued before their requested day/time; a
    bounded lookback lets one missed during downtime arrive after restart.
    Events extracted from documents keep the historical advance lead window.
    """

    raw_day = str(event.get("occurred_at") or "")[:10]
    try:
        event_day = date.fromisoformat(raw_day)
    except ValueError:
        return "invalid"
    today = now.date()
    source = str(event.get("source") or "")
    clock = reminder_clock(event) if source.startswith("reminder:") else ""
    description = str(event.get("description") or "").strip()
    if source.startswith("reminder:") and description.startswith(_CLOCK_PREFIX) and not clock:
        return "invalid"
    if clock:
        hour, minute = (int(part) for part in clock.split(":", 1))
        scheduled = datetime.combine(event_day, time(hour, minute), tzinfo=now.tzinfo)
        if scheduled > now:
            return "early"
        return "due" if now - scheduled <= timedelta(days=7) else "expired"
    if source.startswith("reminder:"):
        if event_day > today:
            return "early"
        return "due" if today - event_day <= timedelta(days=7) else "expired"
    if event_day < today:
        return "expired"
    return "due" if event_day <= today + timedelta(days=max(0, int(lead_days))) else "early"


def reminder_is_due(event: Mapping[str, Any], now: datetime, *, lead_days: int) -> bool:
    """Whether a projected event belongs in the outbound queue at ``now``."""

    return reminder_due_state(event, now, lead_days=lead_days) == "due"


def reminder_when_text(event: Mapping[str, Any], today: date) -> str:
    raw_day = str(event.get("occurred_at") or "")[:10]
    when = raw_day
    if raw_day == today.isoformat():
        when = "сегодня"
    elif raw_day == (today + timedelta(days=1)).isoformat():
        when = "завтра"
    clock = reminder_clock(event)
    return f"{when} в {clock}" if clock else when
