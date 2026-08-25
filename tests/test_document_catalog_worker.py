"""Bounded, restart-safe orchestration for DocumentCatalog convergence."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from dataclasses import replace

import pytest

from friday.workers import (
    _DOCUMENT_CATALOG_CURSOR_KEY,
    _DOCUMENT_CATALOG_TICK_LIMIT,
    WorkerBatchError,
    WorkersManager,
    WorkerSupervisor,
)


class _CatalogStorage:
    def __init__(
        self,
        tenants: list[str],
        *,
        reconcile_used: int = 2,
        reconcile_fail: set[str] | None = None,
        backfill_fail: set[str] | None = None,
        block_once: str | None = None,
        secret: str = "private-document-body",
    ) -> None:
        self.tenants = tenants
        self.reconcile_used = reconcile_used
        self.reconcile_fail = reconcile_fail or set()
        self.backfill_fail = backfill_fail or set()
        self.block_once = block_once
        self.secret = secret
        self.calls: list[tuple[str, str, int, int]] = []
        self.thread_ids: list[int] = []
        self.kv: dict[str, str] = {}
        self.kv_writes: list[tuple[str, str]] = []
        self.count_calls = 0
        self.block_started = threading.Event()
        self.block_release = threading.Event()
        self.block_finished = threading.Event()
        self._blocked = False

    def _thread(self) -> None:
        self.thread_ids.append(threading.get_ident())

    def list_user_ids(self, *, active_only: bool) -> list[str]:
        assert active_only is True
        self._thread()
        return list(self.tenants)

    def kv_get(self, key: str) -> str | None:
        self._thread()
        return self.kv.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self._thread()
        self.kv[key] = value
        self.kv_writes.append((key, value))

    def reconcile_document_catalog(self, user_id: str, *, limit: int) -> dict[str, object]:
        self._thread()
        if user_id in self.reconcile_fail:
            self.calls.append(("reconcile", user_id, limit, 0))
            raise RuntimeError(f"catalog failure includes {self.secret}")
        used = min(limit, self.reconcile_used)
        self.calls.append(("reconcile", user_id, limit, used))
        if user_id == self.block_once and not self._blocked:
            self._blocked = True
            self.block_started.set()
            self.block_release.wait(timeout=5)
            self.block_finished.set()
        return {
            "examined": used,
            "has_more": True,
            "raw_object_ids": [self.secret],
        }

    def backfill_document_catalog(self, user_id: str, *, limit: int) -> dict[str, object]:
        self._thread()
        if user_id in self.backfill_fail:
            self.calls.append(("backfill", user_id, limit, 0))
            raise RuntimeError(f"backfill failure includes {self.secret}")
        self.calls.append(("backfill", user_id, limit, limit))
        return {
            "processed": limit,
            "has_more": False,
            "body": self.secret,
        }

    def count_document_catalog_retryable(self, _user_id: str) -> int:
        self.count_calls += 1
        raise AssertionError("worker must not issue an exact corpus count")


def _manager(settings, storage: _CatalogStorage) -> WorkersManager:
    return WorkersManager(replace(settings, shared_archive=False), storage, None, None)


def _cursor(storage: _CatalogStorage) -> dict[str, int]:
    return json.loads(storage.kv.get(_DOCUMENT_CATALOG_CURSOR_KEY, "{}"))


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
async def test_catalog_calls_are_off_loop_sorted_and_share_one_global_budget(
    settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tenants = ["carol", "alice", "bob"]
    secret = "body-and-raw-id-canary"
    storage = _CatalogStorage(tenants, reconcile_used=2, secret=secret)
    manager = _manager(settings, storage)
    event_loop_thread = threading.get_ident()

    with caplog.at_level(logging.INFO, logger="friday.workers"):
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
    assert storage.thread_ids
    assert all(thread_id != event_loop_thread for thread_id in storage.thread_ids)
    assert secret not in caplog.text
    assert all(tenant not in caplog.text for tenant in tenants)
    assert "reconcile_has_more=3" in caplog.text
    assert "backfill_has_more=0" in caplog.text


@pytest.mark.asyncio
async def test_rotation_and_single_item_phases_survive_manager_restarts(settings) -> None:
    tenants = [f"tenant-{index:03d}" for index in range(_DOCUMENT_CATALOG_TICK_LIMIT + 2)]
    storage = _CatalogStorage(list(reversed(tenants)), reconcile_used=1)
    ticks: list[list[tuple[str, str, int, int]]] = []
    writes_per_tick: list[int] = []

    for _ in range(3):
        before = len(storage.calls)
        writes_before = len(storage.kv_writes)
        await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001
        ticks.append(storage.calls[before:])
        writes_per_tick.append(len(storage.kv_writes) - writes_before)

    assert all(len(tick) == _DOCUMENT_CATALOG_TICK_LIMIT for tick in ticks)
    assert all(sum(used for _, _, _, used in tick) <= _DOCUMENT_CATALOG_TICK_LIMIT for tick in ticks)
    assert writes_per_tick == [1, 1, 1], "cursor persistence regressed to one write per tenant"
    assert [tenant for _, tenant, _, _ in ticks[0]] == sorted(tenants)[:_DOCUMENT_CATALOG_TICK_LIMIT]
    phases_by_tenant = {
        tenant: {phase for tick in ticks for phase, called, _, _ in tick if called == tenant}
        for tenant in tenants
    }
    assert all(phases == {"reconcile", "backfill"} for phases in phases_by_tenant.values())

    assert storage.kv_writes
    for key, payload in storage.kv_writes:
        assert key == _DOCUMENT_CATALOG_CURSOR_KEY
        assert set(json.loads(payload)) == {"cursor", "round"}
        assert all(tenant not in payload for tenant in tenants)


@pytest.mark.asyncio
async def test_cancellation_checkpoints_only_completed_allocations(settings) -> None:
    storage = _CatalogStorage(
        ["charlie", "bravo", "alpha"],
        reconcile_used=0,
        block_once="bravo",
    )
    task = asyncio.create_task(_manager(settings, storage)._document_catalog_reconcile_all())  # noqa: SLF001
    for _ in range(200):
        if storage.block_started.is_set():
            break
        await asyncio.sleep(0.005)
    assert storage.block_started.is_set()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    storage.block_release.set()
    assert await asyncio.to_thread(storage.block_finished.wait, 2.0)

    assert _cursor(storage) == {}
    assert storage.kv_writes == [], "cancellation must replay instead of guessing a checkpoint"
    storage.calls.clear()
    await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001
    assert storage.calls[0][1] == "alpha"
    assert any(tenant == "bravo" for _, tenant, _, _ in storage.calls), (
        "the first unprocessed tenant was skipped after cancel"
    )


@pytest.mark.asyncio
async def test_persistent_tenant_failure_stays_selected_and_keeps_health_failed(
    settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tenants = [f"tenant-{index:03d}" for index in range(_DOCUMENT_CATALOG_TICK_LIMIT + 2)]
    broken = tenants[5]
    secret = "failure-body-and-raw-id"
    storage = _CatalogStorage(tenants, reconcile_fail={broken}, secret=secret)
    manager = _manager(settings, storage)
    supervisor = WorkerSupervisor()
    supervisor.register(
        "document_catalog_reconcile",
        manager._document_catalog_reconcile_all,  # noqa: SLF001
        interval_sec=1.0,
        timeout_sec=120.0,
    )
    task = supervisor._tasks[0]  # noqa: SLF001
    supervisor._running = True  # noqa: SLF001
    state: dict[str, object] = {}

    with caplog.at_level(logging.INFO, logger="friday.workers"):
        handle = asyncio.create_task(supervisor._run_task(task))  # noqa: SLF001
        try:
            for _ in range(500):
                state = supervisor.snapshot().get(task.name, {})
                if state.get("status") == "error" and state.get("consecutive_failures") == 2:
                    break
                await asyncio.sleep(0.01)
        finally:
            supervisor._running = False  # noqa: SLF001
            handle.cancel()
            await asyncio.gather(handle, return_exceptions=True)

    assert state["status"] == "error"
    assert state["error_type"] == "WorkerBatchError"
    assert state["consecutive_failures"] == 2
    assert _cursor(storage) == {"cursor": 5, "round": 0}
    assert len(storage.kv_writes) == 1
    assert sum(phase == "reconcile" and tenant == broken for phase, tenant, _, _ in storage.calls) >= 2
    assert secret not in caplog.text
    assert broken not in caplog.text


@pytest.mark.asyncio
async def test_reconcile_failure_uses_only_reserved_backfill_and_isolates_next_tenant(settings) -> None:
    storage = _CatalogStorage(["bob", "alice"], reconcile_fail={"alice"})
    manager = _manager(settings, storage)

    with pytest.raises(WorkerBatchError, match="1 tenant operation"):
        await manager._document_catalog_reconcile_all()  # noqa: SLF001

    assert storage.calls == [
        ("reconcile", "alice", 63, 0),
        ("backfill", "alice", 1, 1),
        ("reconcile", "bob", 63, 2),
        ("backfill", "bob", 62, 62),
    ]
    assert _cursor(storage) == {}, "a later success advanced past the failed allocation"


@pytest.mark.asyncio
async def test_backfill_failure_does_not_undo_reconcile_or_skip_the_next_tenant(settings) -> None:
    storage = _CatalogStorage(["bob", "alice"], backfill_fail={"alice"})

    with pytest.raises(WorkerBatchError, match="1 tenant operation"):
        await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001

    assert storage.calls == [
        ("reconcile", "alice", 63, 2),
        ("backfill", "alice", 62, 0),
        ("reconcile", "bob", 63, 2),
        ("backfill", "bob", 62, 62),
    ]
    assert _cursor(storage) == {}


@pytest.mark.asyncio
async def test_worker_uses_bounded_has_more_and_never_requests_an_exact_count(settings) -> None:
    storage = _CatalogStorage(["alice"], reconcile_used=1)

    await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001

    source = inspect.getsource(WorkersManager._document_catalog_reconcile_all)  # noqa: SLF001
    source += inspect.getsource(WorkersManager._document_catalog_reconcile)  # noqa: SLF001
    assert "remaining_retryable" not in source
    assert '"has_more"' in source
    assert storage.count_calls == 0
