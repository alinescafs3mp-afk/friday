"""Sentinel organ — Friday watches its own health and speaks up.

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
import re
from collections.abc import Sequence
from typing import Any

from friday.diagnostics import collect_diagnostics
from friday.organs import (
    Organ,
    OrganWorker,
    ServiceContext,
    in_quiet_hours,
    is_service_recipient,
    local_now,
    may_push_to,
    resolve_chat_id,
)
from friday.workers._blocking import run_blocking

LOGGER = logging.getLogger(__name__)

# Diagnostics severities worth waking the owner for. "setup" is first-run
# guidance (e.g. database not initialised yet), not an operational fault.
_ALERT_SEVERITIES = {"error", "warning"}

#: Поломки, о которых говорят НЕМЕДЛЕННО, невзирая на тихие часы.
#:
#: Решение владельца 2026-08-03: «отказ ВСЕЙ системы будит всегда». Довод —
#: пока модель не отвечает, каждый пишущий получает испорченные ответы, а не
#: молчание; в живом отказе этих суток человек за двадцать минут получил восемь
#: таких и перестал писать вовсе.
#:
#: Список намеренно короткий и содержит только «система не работает». Состояние
#: воркеров, резервные копии, гигиена секретов, нехватка места — важное, но не
#: то, ради чего будят: оно дождётся утра и ничего за ночь не испортит.
_WAKES_THE_OWNER = {"llm_not_generating", "start_llm_runtime"}

# Reading host diagnostics over HTTP needs this capability; a push carries the
# same material, so it answers to the same gate. Otherwise the outbound channel
# is a way *around* the permission model instead of a use of it.
_DIAGNOSTICS_CAPABILITY = "admin.diagnostics"
# Fallback when no AuthorizationService was supplied (an organ context built by
# hand, e.g. in a test). Deliberately narrower than the real check.
_PRIVILEGED_PRESETS = {"owner", "admin"}

# An absolute filesystem path: a root, at least one intermediate separator, then a
# final segment. The lookbehind keeps a URL's path (`http://host:8001/v1`) intact —
# what must never leave the machine is where things live on this disk. The
# secret-hygiene report is the sharp case: its detail is literally
# "<path> содержит значение <secret>", which names both a secret and its location.
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w:/])(?:[A-Za-z]:[\\/]|/)(?:[^\s\\/]+[\\/])+[^\s\\/]*")
_PATH_PLACEHOLDER = "‹путь скрыт›"


def _without_paths(text: str) -> str:
    """Strip on-disk locations from anything about to leave this machine."""
    return _ABSOLUTE_PATH_RE.sub(_PATH_PLACEHOLDER, text)


def _format_alert(action: dict) -> str:
    icon = "🚨" if str(action.get("severity")) == "error" else "⚠️"
    title = str(action.get("title") or "Проблема").strip()
    detail = _without_paths(str(action.get("detail") or "").strip())
    command = _without_paths(str(action.get("command") or "").strip())
    lines = [f"{icon} Friday: {title}"]
    if detail:
        lines.append(detail)
    if command:
        lines.append(f"→ {command}")
    if _PATH_PLACEHOLDER in detail or _PATH_PLACEHOLDER in command:
        lines.append("Полные пути — на самой машине: `jericho doctor`.")
    return "\n".join(lines)


def _is_service_recipient(settings: Any, chat_id: str) -> bool:
    """Правило получателя служебных сообщений — общее на все органы.

    Жило здесь, пока служебным считалась одна диагностика хоста. Тотальный аудит
    показал, что недельная сводка и хроника дня — такие же служебные сообщения, а
    в общем архиве ещё и пересказывают чужой материал; правило переехало в
    `friday.organs`, к остальным двум органам.
    """
    return is_service_recipient(settings, chat_id)


def _may_see_diagnostics(ctx: ServiceContext, user_id: str) -> bool:
    auth = ctx.auth
    if auth is None:
        preset = str((ctx.storage.get_user(user_id) or {}).get("preset_key") or "")
        return preset in _PRIVILEGED_PRESETS
    try:
        actor = auth.actor_for_user(user_id, source="sentinel")
        return bool(auth.authorize(actor, _DIAGNOSTICS_CAPABILITY).allowed)
    except Exception as exc:  # an unknown preset or a missing capability means "no"
        LOGGER.debug("sentinel: cannot resolve diagnostics access (%s)", type(exc).__name__)
        return False


async def scan_health(ctx: ServiceContext) -> None:
    settings = ctx.settings
    if not settings.sentinel_enabled:
        return
    now = local_now(settings)
    # Тихие часы придерживают сообщение; неисправность дождётся конца окна, и
    # следующий обход обнаружит её заново (дедуп посуточный, ничего не теряется).
    #
    # КРОМЕ поломки всей системы. Решение владельца 2026-08-03, прямым ответом:
    # «отказ ВСЕЙ системы будит всегда». Довод — пока модель мертва, каждый
    # пишущий получает испорченные ответы; в живом отказе этих суток человек за
    # двадцать минут получил восемь таких и перестал писать. Ждать до восьми утра
    # означало бы восемь часов того же самого.
    #
    # Ночью проходят только эти коды, всё остальное по-прежнему ждёт утра:
    # состояние воркеров, резервные копии, гигиена секретов — не поломка, а
    # сведения, которые спокойно дождутся.
    quiet = in_quiet_hours(now.hour, settings.quiet_hours_start, settings.quiet_hours_end)
    allowed = settings.telegram_effective_allowed_chat_ids
    if not allowed:
        # No delivery target configured — do not even run diagnostics.
        return
    try:
        # Off the event loop. `collect_diagnostics` is fully synchronous and does a
        # blocking `socket.create_connection`, a `urllib.request.urlopen`, a
        # `PRAGMA integrity_check` over the whole database and a secret-hygiene scan
        # of two directory trees. Called directly from this coroutine it froze the
        # loop for the entire tick — including the `asyncio.timeout` that is supposed
        # to bound it, which cannot fire while the loop is not running.
        report = await run_blocking(
            collect_diagnostics,
            settings,
            ctx.storage,
            check_llm_port=bool(settings.sentinel_check_llm),
            check_secrets=True,
        )
    except Exception as exc:
        # Self-monitoring must never crash the worker loop it is meant to watch.
        LOGGER.error("sentinel: diagnostics collection failed (%s)", type(exc).__name__)
        return
    alerts = [
        action for action in report.get("actions", []) if str(action.get("severity")) in _ALERT_SEVERITIES
    ]
    if quiet:
        alerts = [action for action in alerts if str(action.get("code")) in _WAKES_THE_OWNER]
        if alerts:
            LOGGER.warning("sentinel: тихие часы, но система не работает — говорим сейчас")
    if not alerts:
        return

    day = now.date().isoformat()
    enqueued = 0
    audience = 0
    for user_id in ctx.storage.list_user_ids(active_only=True):
        # Host health is privileged material. Fanning it out to every active
        # account handed a guest — anyone who wrote once in an allowlisted group —
        # the worker state, the backup state and the secret-hygiene report of a
        # machine that is not theirs, while the same read over HTTP needs
        # `admin.diagnostics`.
        if not _may_see_diagnostics(ctx, user_id):
            continue
        chat_id = resolve_chat_id(ctx.storage, user_id)
        if not chat_id:
            continue
        # Служебное — только владельцу, в его чат. Права здесь перестали быть
        # границей: владелец попросил заводить каждого написавшего с полным
        # набором прав, и `admin.diagnostics` теперь есть у всех — то есть
        # состояние воркеров, резервных копий и гигиены секретов чужой машины
        # рассылалось бы каждому, кто однажды написал боту.
        #
        # Список owner-чатов задан — он и есть адресат. Не задан — остаётся
        # прежнее правило по способности: молчать совсем хуже, чем сказать
        # тому, кто и так всё видит.
        if not _is_service_recipient(settings, chat_id):
            continue
        audience += 1
        # Deny-by-default, re-checked here (the bridge re-checks again at send).
        if not may_push_to(settings, ctx.storage, user_id, chat_id):
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
    elif not audience:
        # Silence here would be indistinguishable from health. Say it out loud:
        # the instance is unwell and there is nobody it is allowed to tell.
        LOGGER.warning(
            "sentinel: %d health alert(s) with no recipient — no active account holds %s "
            "with a private Telegram chat on the allowlist",
            len(alerts),
            _DIAGNOSTICS_CAPABILITY,
        )


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
