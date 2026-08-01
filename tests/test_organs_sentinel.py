"""Sentinel organ (#6) — Friday monitors itself and pushes health alerts.

Covers: a degraded-worker fault becoming a deduplicated outbound alert, the
enable gate, allowlist deny-by-default, quiet-hours suppression, the message
formatting, and registry composition. Diagnostics collection reuses the same
`collect_diagnostics` that powers the admin panel; here it reads the seeded
worker-health kv from the passed storage, so the alert is deterministic.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from friday.organs import ServiceContext, build_registry
from friday.organs.sentinel import SentinelOrgan, _format_alert, scan_health


def _sentinel_settings(*, quiet_start: int = 0, quiet_end: int = 0):
    from friday.config import load_settings

    return replace(
        load_settings(),
        sentinel_enabled=True,
        sentinel_check_llm=False,  # never probe a real port from the suite
        workers_enabled=True,  # required for worker-health diagnostics
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
    )


def _seed_telegram_user(storage, chat_id: str, *, preset_key: str = "admin") -> str:
    # Default `admin`, because host diagnostics are privileged: the audience is the
    # accounts that hold `admin.diagnostics`, the same gate the HTTP read uses. The
    # old default (`user`) is what let a guest read this machine's health.
    user_id = f"telegram:test:{chat_id}"
    storage.ensure_user(user_id, source="telegram", preset_key=preset_key, metadata={"chat_id": chat_id})
    return user_id


def _seed_degraded_worker(storage, name: str = "reflection_digest") -> None:
    # consecutive_failures >= 3 marks a task degraded -> an 'error' diagnostics action.
    storage.kv_set(
        f"workers:health:{name}",
        json.dumps(
            {
                "name": name,
                "status": "error",
                "consecutive_failures": 4,
                "interval_sec": 86400,
                "last_finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "error_type": "RuntimeError",
            }
        ),
    )


# --- message formatting ---------------------------------------------------


def test_format_alert_shapes_message():
    msg = _format_alert(
        {"severity": "error", "title": "Сбой", "detail": "деталь", "command": "jericho doctor"}
    )
    assert msg.startswith("🚨 Friday: Сбой")
    assert "деталь" in msg
    assert "→ jericho doctor" in msg
    assert _format_alert({"severity": "warning", "title": "Мелочь"}).startswith("⚠️")


# --- worker scan ----------------------------------------------------------


@pytest.mark.asyncio
async def test_sentinel_pushes_alert_for_degraded_workers(storage):
    settings = _sentinel_settings()
    _seed_telegram_user(storage, "5001")  # on the conftest allowlist
    _seed_degraded_worker(storage)

    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)
    await scan_health(ctx)

    pending = storage.list_pending_notifications(limit=100)
    assert pending
    assert all(n["chat_id"] == "5001" and n["kind"] == "sentinel" for n in pending)
    assert any("Friday:" in n["body"] for n in pending)
    # The failed-workers diagnostics action reached the owner.
    assert any("Фоновые задачи" in n["body"] for n in pending)

    # A persistent fault does not turn into a stream: same day dedups.
    before = len(pending)
    await scan_health(ctx)
    assert len(storage.list_pending_notifications(limit=100)) == before


@pytest.mark.asyncio
async def test_sentinel_disabled_is_silent(storage):
    settings = replace(_sentinel_settings(), sentinel_enabled=False)
    _seed_telegram_user(storage, "5001")
    _seed_degraded_worker(storage)
    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)
    await scan_health(ctx)
    assert storage.list_pending_notifications(limit=100) == []


@pytest.mark.asyncio
async def test_sentinel_skips_unallowlisted_chat(storage):
    settings = _sentinel_settings()
    _seed_telegram_user(storage, "999999")  # not on the allowlist
    _seed_degraded_worker(storage)
    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)
    await scan_health(ctx)
    assert storage.list_pending_notifications(limit=100) == []


@pytest.mark.asyncio
async def test_sentinel_respects_quiet_hours(storage):
    now_hour = datetime.now(UTC).hour
    settings = _sentinel_settings(quiet_start=now_hour, quiet_end=(now_hour + 1) % 24)
    _seed_telegram_user(storage, "5001")
    _seed_degraded_worker(storage)
    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)
    await scan_health(ctx)
    assert storage.list_pending_notifications(limit=100) == []


# --- registry -------------------------------------------------------------


def test_registry_includes_sentinel_worker(settings):
    registry = build_registry(settings)
    assert any(isinstance(o, SentinelOrgan) for o in registry.organs)
    ctx = ServiceContext(settings=settings, storage=None, kg=None, ingestion=None)
    assert any(w.name == "sentinel_watch" for w in registry.workers(ctx))


# --- who may be told, and what may be said --------------------------------


@pytest.mark.asyncio
async def test_a_guest_is_never_told_about_the_host(storage):
    """Health of this machine is privileged; the push obeys the same gate as the read.

    The scan fanned out to every *active* account, so anyone provisioned by
    writing once in an allowlisted group — a `guest` — received the worker state,
    the backup state and the secret-hygiene report of a machine that is not
    theirs. Reading the identical report over HTTP requires `admin.diagnostics`.
    """
    from friday.permissions import AuthorizationService

    settings = _sentinel_settings()
    owner = _seed_telegram_user(storage, "5001", preset_key="owner")
    guest = _seed_telegram_user(storage, "5002", preset_key="guest")
    _seed_degraded_worker(storage)

    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=None,
        auth=AuthorizationService(storage),
    )
    await scan_health(ctx)

    recipients = {n["user_id"] for n in storage.list_pending_notifications(limit=100)}
    assert owner in recipients
    assert guest not in recipients


def test_no_filesystem_path_is_transmitted():
    """The secret-hygiene detail names a secret AND where it lives. Only one may travel.

    `secret_exposed_in_file` formats as "<path> содержит значение <secret>", and
    `secret_file_permissions` suggests `chmod 600 <path>`. Telegram is off this
    machine; an on-disk location is exactly what must not go there. The alert
    still fires — the path stays behind `jericho doctor` on the host.
    """
    exposed = _format_alert(
        {
            "severity": "error",
            "title": "Секрет Friday лежит в постороннем файле",
            "detail": "/home/jericho/notes/todo.txt содержит значение FRIDAY_TELEGRAM_BOT_TOKEN. "
            "Удалите файл и перевыпустите этот секрет.",
        }
    )
    assert "/home/jericho/notes/todo.txt" not in exposed
    assert "FRIDAY_TELEGRAM_BOT_TOKEN" in exposed  # which secret is still useful
    assert "jericho doctor" in exposed

    perms = _format_alert(
        {
            "severity": "warning",
            "title": "Файл с секретами доступен другим пользователям",
            "detail": "/home/jericho/.jericho/.env.local имеет права 644. Ожидается 600.",
            "command": "chmod 600 /home/jericho/.jericho/.env.local",
        }
    )
    assert ".env.local" not in perms
    assert "chmod 600" in perms

    # A URL is not an on-disk location and must survive intact, or the
    # "vLLM unreachable" alert stops naming the endpoint it could not reach.
    endpoint = _format_alert(
        {"severity": "error", "title": "vLLM недоступен", "detail": "http://127.0.0.1:8001/v1 не отвечает"}
    )
    assert "http://127.0.0.1:8001/v1" in endpoint


@pytest.mark.asyncio
async def test_the_scan_does_not_freeze_the_event_loop(storage, monkeypatch):
    """`collect_diagnostics` is fully synchronous and was awaited as if it were not.

    It does a blocking `socket.create_connection`, a `urllib.request.urlopen`, a
    `PRAGMA integrity_check` over the whole database and a secret-hygiene scan of
    two directory trees. Called straight from the coroutine it froze the loop for
    the whole tick — including the `asyncio.timeout` meant to bound it, which
    cannot fire while the loop is not running.

    Measured as the WORST GAP between heartbeat ticks, with the heartbeat already
    running before the scan starts. Counting ticks over the whole window does not
    work: a scan that blocks first and returns leaves the rest of the window free,
    and the tally comes out the same either way.
    """
    import asyncio
    import time

    import friday.organs.sentinel as sentinel

    def slow_diagnostics(*_args, **_kwargs):
        time.sleep(0.5)
        return {"actions": []}

    monkeypatch.setattr(sentinel, "collect_diagnostics", slow_diagnostics)
    settings = _sentinel_settings()
    _seed_telegram_user(storage, "5001")
    ctx = ServiceContext(settings=settings, storage=storage, kg=None, ingestion=None)

    worst = 0.0
    running = True

    async def heartbeat() -> None:
        nonlocal worst
        last = time.perf_counter()
        while running:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            worst = max(worst, now - last)
            last = now

    beat = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.05)  # the heartbeat is established BEFORE the scan starts
    try:
        await scan_health(ctx)
    finally:
        running = False
        await beat

    assert worst < 0.25, f"the event loop stalled for {worst:.2f}s during a 0.5s scan"
