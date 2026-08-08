"""Temporal tools own direction and inclusive-window invariants themselves."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.web_surfer import WebSurfer


@pytest.fixture
def temporal_kernel(settings, storage):
    storage.ensure_user("synthetic-owner", preset_key="owner")
    authorization = AuthorizationService(storage)
    graph = KnowledgeGraph(storage)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(
        storage,
        graph,
        WebSurfer(settings),
        IngestionPipeline(settings, storage, graph),
    )
    return kernel, authorization.actor_for_user("synthetic-owner", source="test")


@pytest.mark.asyncio
async def test_upcoming_rejects_a_past_window_even_when_called_directly(temporal_kernel) -> None:
    kernel, actor = temporal_kernel
    today = datetime.now(kernel._zone()).date()  # noqa: SLF001
    yesterday = today - timedelta(days=1)

    result = await kernel.execute(
        "upcoming",
        {"since": yesterday.isoformat(), "until": yesterday.isoformat()},
        actor=actor,
    )

    assert result.success
    assert result.data["understood"] is False
    assert result.data["items"] == []


@pytest.mark.asyncio
async def test_what_happened_rejects_a_future_window_even_when_called_directly(
    temporal_kernel,
) -> None:
    kernel, actor = temporal_kernel
    tomorrow = datetime.now(kernel._zone()).date() + timedelta(days=1)  # noqa: SLF001
    since = f"{tomorrow.isoformat()}T00:00:00"
    until = f"{tomorrow.isoformat()}T23:59:59"

    result = await kernel.execute(
        "what_happened",
        {"since": since, "until": until, "limit": 40},
        actor=actor,
    )

    assert result.success
    assert result.data["understood"] is False
    assert result.data["events"] == []


@pytest.mark.asyncio
async def test_exact_valid_boundaries_are_echoed_and_payloads_are_coherent(temporal_kernel) -> None:
    kernel, actor = temporal_kernel
    today = datetime.now(kernel._zone()).date()  # noqa: SLF001
    future = today + timedelta(days=2)

    upcoming = await kernel.execute(
        "upcoming",
        {"since": today.isoformat(), "until": future.isoformat()},
        actor=actor,
    )
    past = await kernel.execute(
        "what_happened",
        {
            "since": f"{today.isoformat()}T00:00:00",
            "until": f"{today.isoformat()}T23:59:59",
            "limit": 40,
        },
        actor=actor,
    )

    assert upcoming.data["understood"] is True
    assert upcoming.data["asked_about"]["since"] == today.isoformat()
    assert upcoming.data["asked_about"]["until"] == future.isoformat()
    assert upcoming.data["shown"] == len(upcoming.data["items"])
    assert upcoming.data["shown"] <= upcoming.data["total"]
    assert past.data["understood"] is True
    assert past.data["asked_about"]["since"] == f"{today.isoformat()}T00:00:00"
    checked_until = datetime.fromisoformat(past.data["asked_about"]["until"])
    assert checked_until.date() == today
    assert checked_until <= datetime.now(kernel._zone()).replace(tzinfo=None)  # noqa: SLF001
    assert past.data["shown"] == len(past.data["events"])
    assert past.data["shown"] <= past.data["total"]["total"]


@pytest.mark.asyncio
async def test_explicit_calendar_windows_are_at_most_sixty_inclusive_days(temporal_kernel) -> None:
    kernel, actor = temporal_kernel
    today = datetime.now(kernel._zone()).date()  # noqa: SLF001
    last_allowed = today + timedelta(days=59)
    first_refused = today + timedelta(days=60)

    allowed = await kernel.execute(
        "upcoming",
        {"since": today.isoformat(), "until": last_allowed.isoformat()},
        actor=actor,
    )
    refused = await kernel.execute(
        "upcoming",
        {"since": today.isoformat(), "until": first_refused.isoformat()},
        actor=actor,
    )

    assert allowed.data["understood"] is True
    assert allowed.data["days"] == 60
    assert refused.data["understood"] is False


@pytest.mark.asyncio
async def test_reversed_explicit_windows_are_refused_not_swapped(temporal_kernel) -> None:
    kernel, actor = temporal_kernel
    today = datetime.now(kernel._zone()).date()  # noqa: SLF001
    yesterday = today - timedelta(days=1)

    result = await kernel.execute(
        "what_happened",
        {
            "since": f"{today.isoformat()}T23:59:59",
            "until": f"{yesterday.isoformat()}T00:00:00",
        },
        actor=actor,
    )

    assert result.data["understood"] is False
    assert result.data["events"] == []
