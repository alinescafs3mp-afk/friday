"""Sentinel organ (#6) — Jericho monitors itself and pushes health alerts.

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

from jericho.organs import ServiceContext, build_registry
from jericho.organs.sentinel import SentinelOrgan, _format_alert, scan_health


def _sentinel_settings(*, quiet_start: int = 0, quiet_end: int = 0):
    from jericho.config import load_settings

    return replace(
        load_settings(),
        sentinel_enabled=True,
        sentinel_check_llm=False,  # never probe a real port from the suite
        workers_enabled=True,  # required for worker-health diagnostics
        quiet_hours_start=quiet_start,
        quiet_hours_end=quiet_end,
    )


def _seed_telegram_user(storage, chat_id: str) -> str:
    user_id = f"telegram:test:{chat_id}"
    storage.ensure_user(user_id, source="telegram", metadata={"chat_id": chat_id})
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
    assert msg.startswith("🚨 Jericho: Сбой")
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
    assert any("Jericho:" in n["body"] for n in pending)
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
