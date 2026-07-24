"""Reminders organ — the first proactive organ.

Scans dated event entities (``entity_time``, the §11 timeline) for events
inside a lead window and enqueues a one-off push per event. This turns Jericho
from a passive librarian into something that reaches out — while still writing
nothing to the knowledge graph. Delivery happens through the outbound
notification queue, drained by the Telegram bridge.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from jericho.organs import Organ, OrganWorker, ServiceContext, in_quiet_hours, resolve_chat_id

LOGGER = logging.getLogger(__name__)


def _format_reminder(event: dict, today) -> str:
    name = str(event.get("name") or "событие").strip()
    occurred_at = str(event.get("occurred_at") or "")
    when = occurred_at
    if occurred_at == today.isoformat():
        when = "сегодня"
    elif occurred_at == (today + timedelta(days=1)).isoformat():
        when = "завтра"
    return f"🔔 Напоминание: «{name}» — {when}."


async def scan_reminders(ctx: ServiceContext) -> None:
    settings = ctx.settings
    if not settings.reminders_enabled:
        return
    now = datetime.now(UTC)
    # Quiet hours gate enqueue, so nothing lands in the user's chat overnight;
    # a due event simply waits until the quiet window ends.
    if in_quiet_hours(now.hour, settings.quiet_hours_start, settings.quiet_hours_end):
        return
    allowed = settings.telegram_effective_allowed_chat_ids
    if not allowed:
        return
    today = now.date()
    start = today.isoformat()
    end = (today + timedelta(days=max(0, settings.reminders_lead_days))).isoformat()

    enqueued = 0
    for user_id in ctx.storage.list_user_ids(active_only=True):
        chat_id = resolve_chat_id(ctx.storage, user_id)
        if not chat_id:
            continue
        # Deny-by-default, re-checked here (bridge re-checks again at send time).
        try:
            if int(chat_id) not in allowed:
                continue
        except ValueError:
            continue
        for event in ctx.storage.list_events_in_range(user_id, start=start, end=end):
            dedup_key = f"reminder:{event.get('entity_id')}:{event.get('occurred_at')}"
            if ctx.storage.enqueue_notification(
                user_id,
                chat_id,
                _format_reminder(event, today),
                kind="reminder",
                dedup_key=dedup_key,
            ):
                enqueued += 1
    if enqueued:
        LOGGER.info("Reminders organ queued %d event reminder(s)", enqueued)


class RemindersOrgan(Organ):
    name = "reminders"
    version = "1.0"

    def workers(self, ctx: ServiceContext) -> Sequence[OrganWorker]:
        return (
            OrganWorker(
                name="reminders_scan",
                run=scan_reminders,
                interval_sec=float(ctx.settings.reminders_poll_interval_sec),
                enabled=bool(ctx.settings.reminders_enabled),
                run_immediately=False,
                timeout_sec=300.0,
            ),
        )
