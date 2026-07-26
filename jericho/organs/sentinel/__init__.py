"""Sentinel organ — Jericho watches its own health and speaks up.

The sixth organ closes a gap the others opened: an instance that reaches out to
its owner should also tell them when *it* is unwell. Sentinel runs the very same
read-only diagnostics that power the admin panel, and when a real problem
appears — a worker crash-looping, the backend not refreshing its state, a
missing or invalid backup, an unreachable vLLM — it pushes a deduplicated alert
through the outbound channel.

Like every organ it INITIATES COMMUNICATION but writes nothing to the graph: it
only reports what the diagnostics already know. Deny-by-default is preserved
(the allowlist is checked here and again by the bridge at send time), quiet
hours are respected, and each distinct issue alerts at most once per day so a
persistent fault never turns into a stream of pings.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from jericho.diagnostics import collect_diagnostics
from jericho.organs import Organ, OrganWorker, ServiceContext, in_quiet_hours, resolve_chat_id

LOGGER = logging.getLogger(__name__)

# Diagnostics severities worth waking the owner for. "setup" is first-run
# guidance (e.g. database not initialised yet), not an operational fault.
_ALERT_SEVERITIES = {"error", "warning"}


def _format_alert(action: dict) -> str:
    icon = "🚨" if str(action.get("severity")) == "error" else "⚠️"
    title = str(action.get("title") or "Проблема").strip()
    detail = str(action.get("detail") or "").strip()
    command = str(action.get("command") or "").strip()
    lines = [f"{icon} Jericho: {title}"]
    if detail:
        lines.append(detail)
    if command:
        lines.append(f"→ {command}")
    return "\n".join(lines)


async def scan_health(ctx: ServiceContext) -> None:
    settings = ctx.settings
    if not settings.sentinel_enabled:
        return
    now = datetime.now(UTC)
    # Quiet hours gate the push; a fault simply waits until the window ends and
    # the next tick re-detects it (dedup is per calendar day, so nothing is lost).
    if in_quiet_hours(now.hour, settings.quiet_hours_start, settings.quiet_hours_end):
        return
    allowed = settings.telegram_effective_allowed_chat_ids
    if not allowed:
        # No delivery target configured — do not even run diagnostics.
        return
    try:
        report = collect_diagnostics(
            settings,
            ctx.storage,
            check_llm_port=bool(settings.sentinel_check_llm),
            check_secrets=True,
        )
    except Exception:
        # Self-monitoring must never crash the worker loop it is meant to watch.
        LOGGER.exception("sentinel: diagnostics collection failed")
        return
    alerts = [
        action for action in report.get("actions", []) if str(action.get("severity")) in _ALERT_SEVERITIES
    ]
    if not alerts:
        return

    day = now.date().isoformat()
    enqueued = 0
    for user_id in ctx.storage.list_user_ids(active_only=True):
        chat_id = resolve_chat_id(ctx.storage, user_id)
        if not chat_id:
            continue
        # Deny-by-default, re-checked here (the bridge re-checks again at send).
        try:
            if int(chat_id) not in allowed:
                continue
        except ValueError:
            continue
        for action in alerts:
            code = str(action.get("code") or "issue")
            if ctx.storage.enqueue_notification(
                user_id,
                chat_id,
                _format_alert(action),
                kind="sentinel",
                dedup_key=f"sentinel:{code}:{day}",
            ):
                enqueued += 1
    if enqueued:
        LOGGER.info("Sentinel organ queued %d health alert(s)", enqueued)


class SentinelOrgan(Organ):
    name = "sentinel"
    version = "1.0"

    def workers(self, ctx: ServiceContext) -> Sequence[OrganWorker]:
        return (
            OrganWorker(
                name="sentinel_watch",
                run=scan_health,
                interval_sec=float(ctx.settings.sentinel_interval_sec),
                enabled=bool(ctx.settings.sentinel_enabled),
                run_immediately=False,
                timeout_sec=120.0,
            ),
        )
