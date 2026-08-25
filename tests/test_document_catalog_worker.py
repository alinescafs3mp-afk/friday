"""Bounded, tenant-safe orchestration for DocumentCatalog convergence."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace

import pytest

from friday.workers import (
    _DOCUMENT_CATALOG_TICK_LIMIT,
    WorkersManager,
    WorkerSupervisor,
)


class _CatalogStorage:
    def __init__(
        self,
        tenants: list[str],
        *,
        reconcile_used: int = 2,
        fail_on: str | None = None,
        secret: str = "private-document-body",
    ) -> None:
        self.tenants = tenants
        self.reconcile_used = reconcile_used
        self.fail_on = fail_on
        self.secret = secret
        self.calls: list[tuple[str, str, int, int]] = []
        self.thread_ids: list[int] = []

    def list_user_ids(self, *, active_only: bool) -> list[str]:
        assert active_only is True
        self.thread_ids.append(threading.get_ident())
        return list(self.tenants)

    def reconcile_document_catalog(self, user_id: str, *, limit: int) -> dict[str, object]:
        self.thread_ids.append(threading.get_ident())
        used = min(limit, self.reconcile_used)
        self.calls.append(("reconcile", user_id, limit, used))
        if user_id == self.fail_on:
            raise RuntimeError(f"catalog failure includes {self.secret}")
        return {
            "examined": used,
            "remaining_retryable": 7,
            "raw_object_ids": [self.secret],
        }

    def backfill_document_catalog(self, user_id: str, *, limit: int) -> dict[str, object]:
        self.thread_ids.append(threading.get_ident())
        self.calls.append(("backfill", user_id, limit, limit))
        return {
            "processed": limit,
            "remaining_retryable": 3,
            "body": self.secret,
        }


def _manager(settings, storage: _CatalogStorage) -> WorkersManager:
    return WorkersManager(replace(settings, shared_archive=False), storage, None, None)


def test_catalog_worker_is_registered_immediately_with_a_short_timeout(settings) -> None:
    manager = _manager(settings, _CatalogStorage(["alice"]))

    manager.register_all()

    task = next(
        task for task in manager.supervisor._tasks if task.name == "document_catalog_reconcile"  # noqa: SLF001
    )
    assert task.func == manager._document_catalog_reconcile_all  # noqa: SLF001
    assert task.enabled is True
    assert task.run_immediately is True
    assert 45.0 <= task.interval_sec <= 75.0
    assert task.timeout_sec <= 120.0


@pytest.mark.asyncio
async def test_catalog_storage_calls_run_off_the_event_loop(settings) -> None:
    storage = _CatalogStorage(["alice"], reconcile_used=1)
    manager = _manager(settings, storage)
    event_loop_thread = threading.get_ident()

    await manager._document_catalog_reconcile_all()  # noqa: SLF001

    assert [call[0] for call in storage.calls] == ["reconcile", "backfill"]
    assert storage.thread_ids
    assert all(thread_id != event_loop_thread for thread_id in storage.thread_ids)


@pytest.mark.asyncio
async def test_catalog_budget_is_divided_without_starving_a_tenant(settings) -> None:
    tenants = ["alice", "bob", "carol"]
    storage = _CatalogStorage(tenants, reconcile_used=2)
    manager = _manager(settings, storage)

    await manager._document_catalog_reconcile_all()  # noqa: SLF001

    assert [(phase, tenant) for phase, tenant, _, _ in storage.calls] == [
        ("reconcile", "alice"),
        ("backfill", "alice"),
        ("reconcile", "bob"),
        ("backfill", "bob"),
        ("reconcile", "carol"),
        ("backfill", "carol"),
    ]
    actual_by_tenant = {
        tenant: sum(used for _, called_tenant, _, used in storage.calls if called_tenant == tenant)
        for tenant in tenants
    }
    assert sum(actual_by_tenant.values()) == _DOCUMENT_CATALOG_TICK_LIMIT
    assert max(actual_by_tenant.values()) - min(actual_by_tenant.values()) <= 1

    for tenant in tenants:
        reconcile = next(call for call in storage.calls if call[:2] == ("reconcile", tenant))
        backfill = next(call for call in storage.calls if call[:2] == ("backfill", tenant))
        assert backfill[2] == actual_by_tenant[tenant] - reconcile[3]


@pytest.mark.asyncio
async def test_catalog_budget_remainder_rotates_when_tenants_outnumber_it(settings) -> None:
    tenants = [f"tenant-{index}" for index in range(_DOCUMENT_CATALOG_TICK_LIMIT + 2)]
    storage = _CatalogStorage(tenants)
    manager = _manager(settings, storage)
    ticks: list[list[tuple[str, str, int, int]]] = []

    for _ in range(3):
        before = len(storage.calls)
        await manager._document_catalog_reconcile_all()  # noqa: SLF001
        ticks.append(storage.calls[before:])

    assert all(sum(used for _, _, _, used in tick) <= _DOCUMENT_CATALOG_TICK_LIMIT for tick in ticks)
    phases_by_tenant = {
        tenant: {phase for tick in ticks for phase, called, _, _ in tick if called == tenant}
        for tenant in tenants
    }
    assert all(phases == {"reconcile", "backfill"} for phases in phases_by_tenant.values())


@pytest.mark.asyncio
async def test_tenant_failure_is_isolated_and_marks_worker_health_error(
    settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tenants = ["private-before", "private-broken", "private-after"]
    secret = "body and raw_catalog_secret_id"
    storage = _CatalogStorage(tenants, reconcile_used=1, fail_on=tenants[1], secret=secret)
    manager = _manager(settings, storage)
    supervisor = WorkerSupervisor()
    supervisor.register(
        "document_catalog_reconcile",
        manager._document_catalog_reconcile_all,  # noqa: SLF001
        interval_sec=60.0,
        timeout_sec=120.0,
    )
    task = supervisor._tasks[0]  # noqa: SLF001
    supervisor._running = True  # noqa: SLF001

    with caplog.at_level(logging.INFO, logger="friday.workers"):
        handle = asyncio.create_task(supervisor._run_task(task))  # noqa: SLF001
        try:
            for _ in range(200):
                if supervisor.snapshot().get(task.name, {}).get("status") == "error":
                    break
                await asyncio.sleep(0.005)
            state = supervisor.snapshot()[task.name]
        finally:
            supervisor._running = False  # noqa: SLF001
            handle.cancel()
            await asyncio.gather(handle, return_exceptions=True)

    assert state["status"] == "error"
    assert state["error_type"] == "WorkerBatchError"
    assert state["consecutive_failures"] == 1
    assert [(phase, tenant) for phase, tenant, _, _ in storage.calls] == [
        ("reconcile", tenants[0]),
        ("backfill", tenants[0]),
        ("reconcile", tenants[1]),
        ("reconcile", tenants[2]),
        ("backfill", tenants[2]),
    ]
    assert secret not in caplog.text
    assert all(tenant not in caplog.text for tenant in tenants)
