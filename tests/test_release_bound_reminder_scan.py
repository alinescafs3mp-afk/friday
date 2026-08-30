"""Exact bounded-scan nodes used by release-bound durable-work evidence."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

import friday.organs.reminders as reminders_module
from friday.organs import ServiceContext


def _admitted_context(settings: Any, storage: Any) -> tuple[ServiceContext, str]:
    person_id = "telegram:test:5001"
    storage.ensure_user(person_id, source="telegram", metadata={"chat_id": "5001"})
    tuned = replace(
        settings,
        local_timezone="UTC",
        reminders_enabled=True,
        reminders_lead_days=1,
        quiet_hours_start=0,
        quiet_hours_end=0,
    )
    return ServiceContext(settings=tuned, storage=storage, kg=None, ingestion=None), person_id


@pytest.mark.asyncio
async def test_release_evidence_scan_stops_at_exact_ten_pages_of_two_hundred(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context, person_id = _admitted_context(settings, storage)
    fixed_now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    calls: list[tuple[object, int]] = []

    def page(
        _storage: Any,
        _tenant: str,
        selected_person: str,
        *,
        start: str,
        end: str,
        after: object,
        limit: int,
    ) -> tuple[list[dict[str, object]], tuple[str, str, str], bool]:
        assert selected_person == person_id
        assert start == "2026-08-22"
        assert end == "2026-08-30"
        calls.append((after, limit))
        index = len(calls)
        return [], (f"2026-08-29T12:{index:02d}:00Z", f"cursor-{index}", f"event-{index}"), True

    monkeypatch.setattr(reminders_module, "local_now", lambda _settings: fixed_now)
    monkeypatch.setattr(reminders_module, "_bounded_visible_reminder_event_page", page)
    with caplog.at_level(logging.WARNING, logger=reminders_module.LOGGER.name):
        await reminders_module.scan_reminders(context)

    assert len(calls) == reminders_module._REMINDER_SCAN_MAX_PAGES == 10  # noqa: SLF001
    assert (
        {limit for _after, limit in calls}
        == {
            reminders_module._REMINDER_SCAN_PAGE_SIZE  # noqa: SLF001
        }
        == {200}
    )
    assert calls[0][0] is None
    assert all(calls[index][0] is not None for index in range(1, len(calls)))
    assert caplog.messages == ["Reminder scan reached its bounded page cap"]
    assert person_id not in caplog.text and "5001" not in caplog.text


@pytest.mark.asyncio
async def test_release_evidence_scan_stops_when_continuation_cursor_is_missing(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    context, person_id = _admitted_context(settings, storage)
    fixed_now = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
    calls = 0

    def cursorless_page(
        _storage: Any,
        _tenant: str,
        selected_person: str,
        *,
        start: str,
        end: str,
        after: object,
        limit: int,
    ) -> tuple[list[dict[str, object]], None, bool]:
        nonlocal calls
        calls += 1
        assert selected_person == person_id
        assert (start, end, after, limit) == ("2026-08-22", "2026-08-30", None, 200)
        return [], None, True

    monkeypatch.setattr(reminders_module, "local_now", lambda _settings: fixed_now)
    monkeypatch.setattr(
        reminders_module,
        "_bounded_visible_reminder_event_page",
        cursorless_page,
    )
    with caplog.at_level(logging.ERROR, logger=reminders_module.LOGGER.name):
        await reminders_module.scan_reminders(context)

    assert calls == 1
    assert caplog.messages == ["Reminder scan page omitted its continuation cursor"]
    assert person_id not in caplog.text and "5001" not in caplog.text
