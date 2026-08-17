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
hours are respected. Ordinary issues alert at most once per day; generation
stalls use persisted healthy→failed episodes so one continuous outage never
spams, while a new outage after observed recovery is not hidden by today's old
dedup row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from friday.diagnostics import _llm_generates, collect_diagnostics
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

# The full sentinel scan is intentionally expensive and stays on its 15-minute
# cadence.  This probe is the opposite: one fixed token, no database/filesystem
# diagnostics, and one bounded HTTP request.  Together with the configured
# interval cap (60s), the whole 35-second worker budget keeps worst-case alert
# enqueue below 95 seconds and leaves headroom for the outbound queue to drain.
_GENERATION_PROBE_TIMEOUT_SEC = 25.0
_GENERATION_AWAIT_TIMEOUT_SEC = 30.0
_GENERATION_WORKER_TIMEOUT_SEC = 35.0
# A completed real answer is stronger evidence than another synthetic token.
# Two watchdog intervals cover scheduling jitter without hiding a genuinely
# idle endpoint indefinitely.  Active foreground work also suppresses the
# competing probe; the request's own bounded transport and owner notification
# remain the authority if that work fails.
_GENERATION_RECENT_FOREGROUND_SEC = 120.0
_GENERATION_STATE_KEY = "sentinel:generation_watchdog"
_GENERATION_STATE_VERSION = 1

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


def _read_generation_state(ctx: ServiceContext) -> tuple[dict[str, Any], bool]:
    """Read the closed, non-personal watchdog transition state."""

    empty: dict[str, Any] = {
        "version": _GENERATION_STATE_VERSION,
        "status": "unknown",
        "episode": "",
    }
    try:
        raw = ctx.storage.kv_get(_GENERATION_STATE_KEY)
    except Exception as exc:  # noqa: BLE001 — observability cannot stop workers
        LOGGER.error("sentinel: cannot read generation state (%s)", type(exc).__name__)
        return empty, False
    if raw is None:
        return empty, True
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return empty, True
    if not isinstance(parsed, dict):
        return empty, True
    status = str(parsed.get("status") or "")
    episode = str(parsed.get("episode") or "")
    if (
        parsed.get("version") != _GENERATION_STATE_VERSION
        or status not in {"healthy", "failed"}
        or (episode and (len(episode) != 32 or not all(char in "0123456789abcdef" for char in episode)))
    ):
        return empty, True
    return {"version": _GENERATION_STATE_VERSION, "status": status, "episode": episode}, True


def _write_generation_state(ctx: ServiceContext, *, status: str, episode: str = "") -> bool:
    try:
        ctx.storage.kv_set(
            _GENERATION_STATE_KEY,
            json.dumps(
                {
                    "version": _GENERATION_STATE_VERSION,
                    "status": status,
                    "episode": episode,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — fall back to daily dedup below
        LOGGER.error("sentinel: cannot persist generation state (%s)", type(exc).__name__)
        return False
    return True


def _open_generation_episode(ctx: ServiceContext) -> str:
    """Return one stable dedup identity for the current continuous outage."""

    state, available = _read_generation_state(ctx)
    if not available:
        return ""
    if state["status"] == "failed" and state["episode"]:
        return str(state["episode"])
    episode = uuid.uuid4().hex
    return episode if _write_generation_state(ctx, status="failed", episode=episode) else ""


def _mark_generation_healthy(ctx: ServiceContext) -> None:
    """Close a failed episode so a later outage gets a fresh notification key."""

    state, available = _read_generation_state(ctx)
    if available and state["status"] != "healthy":
        _write_generation_state(ctx, status="healthy")


def _alert_dedup_key(ctx: ServiceContext, action: dict[str, Any], *, day: str) -> str:
    code = str(action.get("code") or "issue")
    if code == "llm_not_generating":
        # Daily dedup suppresses a second real incident after recovery.  A
        # persisted episode identity instead deduplicates only one continuous
        # outage and is shared by the fast probe and the full diagnostics scan.
        episode = _open_generation_episode(ctx)
        if episode:
            return f"sentinel:{code}:episode:{episode}"
    return f"sentinel:{code}:{day}"


def _enqueue_alerts(
    ctx: ServiceContext,
    alerts: Sequence[dict[str, Any]],
    *,
    day: str,
) -> int:
    """Queue already-filtered sentinel actions for the privileged owner audience."""

    settings = ctx.settings
    prepared = [(action, _alert_dedup_key(ctx, action, day=day)) for action in alerts]
    enqueued = 0
    audience = 0
    for user_id in ctx.storage.list_user_ids(active_only=True):
        # Host health is privileged material. Fanning it out to every active
        # account handed a guest the worker, backup and secret-hygiene state of
        # a machine which is not theirs, while the same HTTP read needs
        # ``admin.diagnostics``.
        if not _may_see_diagnostics(ctx, user_id):
            continue
        chat_id = resolve_chat_id(ctx.storage, user_id)
        if not chat_id or not _is_service_recipient(settings, chat_id):
            continue
        audience += 1
        # Deny-by-default, re-checked here (the bridge re-checks again at send).
        if not may_push_to(settings, ctx.storage, user_id, chat_id):
            continue
        for action, dedup_key in prepared:
            if ctx.storage.enqueue_notification(
                user_id,
                chat_id,
                _format_alert(action),
                kind="sentinel",
                dedup_key=dedup_key,
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
    return enqueued


async def watch_generation(ctx: ServiceContext) -> None:
    """Probe only the generation path and alert before a short outage can pass."""

    settings = ctx.settings
    if not (settings.sentinel_enabled and settings.sentinel_check_llm and settings.llm_enabled):
        return
    # No delivery target means there is nobody to alert.  Avoid spending even
    # the one-token probe when its result cannot leave this process.
    if not settings.telegram_effective_allowed_chat_ids:
        return
    activity = getattr(ctx.llm, "generation_watchdog_activity", None)
    if callable(activity):
        try:
            foreground_active, recent_success = activity(recent_success_sec=_GENERATION_RECENT_FOREGROUND_SEC)
        except Exception as exc:  # noqa: BLE001 — uncertainty falls back to the probe
            LOGGER.warning(
                "sentinel: cannot observe foreground model activity (%s)",
                type(exc).__name__,
            )
        else:
            if recent_success:
                _mark_generation_healthy(ctx)
                return
            if foreground_active:
                LOGGER.debug("sentinel: foreground generation is active; probe deferred")
                return
    try:
        # urllib's socket timeout normally returns first.  The coroutine-level
        # deadline is a second, independent boundary for a peer which keeps the
        # socket alive without ever completing the body.  It also lets us alert
        # before the supervisor's last-resort timeout merely records a worker
        # failure that the owner would not see until the full 15-minute scan.
        async with asyncio.timeout(_GENERATION_AWAIT_TIMEOUT_SEC):
            generation = await run_blocking(
                _llm_generates,
                settings.llm_base_url,
                settings.llm_model,
                api_key=settings.llm_api_key,
                timeout=_GENERATION_PROBE_TIMEOUT_SEC,
            )
    except TimeoutError:
        generation = {"generates": False, "seconds": _GENERATION_AWAIT_TIMEOUT_SEC}
    except Exception as exc:  # noqa: BLE001 — absence of a verdict is not health
        LOGGER.error("sentinel: generation probe failed to run (%s)", type(exc).__name__)
        generation = {"generates": False, "seconds": None}

    generates = generation.get("generates") if isinstance(generation, Mapping) else None
    if generates is True:
        _mark_generation_healthy(ctx)
        return

    elapsed = generation.get("seconds") if isinstance(generation, Mapping) else None
    detail = (
        "Проверочная однотокенная генерация не вернула ответа"
        + (f" за {elapsed} с" if isinstance(elapsed, (int, float)) else "")
        + ". Людям в это время могут уходить таймауты вместо обычных ответов. "
        "Нужна ручная проверка и, если зависание подтвердится, перезапуск сервера модели."
    )
    _enqueue_alerts(
        ctx,
        [
            {
                "code": "llm_not_generating",
                "severity": "error",
                "title": "Проверочная генерация не отвечает",
                "detail": detail,
            }
        ],
        day=local_now(settings).date().isoformat(),
    )


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

    _enqueue_alerts(ctx, alerts, day=now.date().isoformat())


class SentinelOrgan(Organ):
    name = "sentinel"
    version = "1.0"

    def workers(self, ctx: ServiceContext) -> Sequence[OrganWorker]:
        return (
            OrganWorker(
                name="sentinel_generation_watch",
                run=watch_generation,
                interval_sec=float(ctx.settings.sentinel_generation_interval_sec),
                enabled=bool(
                    ctx.settings.sentinel_enabled
                    and ctx.settings.sentinel_check_llm
                    and ctx.settings.llm_enabled
                ),
                # The probe is cheap and startup is exactly when the model may
                # have failed independently from the API process.
                run_immediately=True,
                timeout_sec=_GENERATION_WORKER_TIMEOUT_SEC,
            ),
            OrganWorker(
                name="sentinel_watch",
                run=scan_health,
                interval_sec=float(ctx.settings.sentinel_interval_sec),
                enabled=bool(ctx.settings.sentinel_enabled),
                run_immediately=False,
                timeout_sec=120.0,
            ),
        )
