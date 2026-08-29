"""Bounded, restart-safe orchestration for DocumentCatalog convergence."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import replace

import pytest

from friday.account_deletion import (
    AccountDeletionConflict,
    _mark_account_deletion_history_clean,
    delete_account,
    preflight_account_deletion,
)
from friday.document_catalog.worker_state import (
    DocumentCatalogTenantState,
    DocumentCatalogWorkerState,
    decode_document_catalog_worker_state,
    document_catalog_worker_tenant_key,
    encode_document_catalog_worker_state,
    load_document_catalog_worker_namespace_key,
)
from friday.storage._base import deleted_account_tombstone_key
from friday.storage.models import RawObject
from friday.workers import (
    _DOCUMENT_CATALOG_CURSOR_KEY,
    _DOCUMENT_CATALOG_TICK_LIMIT,
    WorkerBatchError,
    WorkersManager,
    WorkerSupervisor,
    _document_catalog_phase_page,
)

_NAMESPACE_KEY = b"k" * 32


def _tenant_key(user_id: str, *, namespace_key: bytes = _NAMESPACE_KEY) -> str:
    return document_catalog_worker_tenant_key(user_id, namespace_key=namespace_key)


class _OneRow:
    def __init__(self, value: str) -> None:
        self.value = value

    def fetchone(self) -> tuple[str]:
        return (self.value,)


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
        self.calls: list[tuple[str, str, str | None, int, int]] = []
        self.thread_ids: list[int] = []
        self.kv: dict[str, str] = {}
        self.kv_writes: list[tuple[str, str]] = []
        self.count_calls = 0
        self.block_started = threading.Event()
        self.block_release = threading.Event()
        self.block_finished = threading.Event()
        self._blocked = False
        self.checkpoint_hook: Callable[[], None] | None = None

    def _thread(self) -> None:
        self.thread_ids.append(threading.get_ident())

    def list_user_ids(self, *, active_only: bool) -> list[str]:
        assert active_only is True
        self._thread()
        return list(self.tenants)

    def list_document_catalog_owner_ids(self) -> list[str]:
        self._thread()
        return list(self.tenants)

    def execute(self, query: str):
        assert "audit_privacy_hmac_key" in query
        self._thread()
        return _OneRow(_NAMESPACE_KEY.hex())

    def kv_get(self, key: str) -> str | None:
        self._thread()
        return self.kv.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self._thread()
        self.kv[key] = value
        self.kv_writes.append((key, value))

    def checkpoint_document_catalog_worker_state(
        self,
        *,
        expected_value: str | None,
        value: str,
        tenant_ids: list[str],
    ) -> bool:
        self._thread()
        if self.checkpoint_hook is not None:
            self.checkpoint_hook()
        if tenant_ids != sorted(set(self.tenants)):
            return False
        if self.kv.get(_DOCUMENT_CATALOG_CURSOR_KEY) != expected_value:
            return False
        state, supported = decode_document_catalog_worker_state(value)
        if not supported:
            return False
        valid_keys = {_tenant_key(tenant) for tenant in tenant_ids}
        if not set(state.tenants).issubset(valid_keys):
            return False
        self.kv[_DOCUMENT_CATALOG_CURSOR_KEY] = value
        self.kv_writes.append((_DOCUMENT_CATALOG_CURSOR_KEY, value))
        return True

    def reconcile_document_catalog(
        self,
        user_id: str,
        *,
        after_raw_object_id: str | None,
        limit: int,
    ) -> dict[str, object]:
        self._thread()
        if user_id in self.reconcile_fail:
            self.calls.append(("reconcile", user_id, after_raw_object_id, limit, 0))
            raise RuntimeError(f"catalog failure includes {self.secret}")
        used = min(limit, self.reconcile_used)
        self.calls.append(("reconcile", user_id, after_raw_object_id, limit, used))
        if user_id == self.block_once and not self._blocked:
            self._blocked = True
            self.block_started.set()
            self.block_release.wait(timeout=5)
            self.block_finished.set()
        return {
            "examined": used,
            "has_more": True,
            "next_after_raw_object_id": f"reconcile-cursor-{len(self.calls)}",
            "raw_object_ids": [self.secret],
        }

    def backfill_document_catalog(
        self,
        user_id: str,
        *,
        after_raw_object_id: str | None,
        limit: int,
        include_document_passages: bool = False,
    ) -> dict[str, object]:
        self._thread()
        if user_id in self.backfill_fail:
            self.calls.append(("backfill", user_id, after_raw_object_id, limit, 0))
            raise RuntimeError(f"backfill failure includes {self.secret}")
        self.calls.append(("backfill", user_id, after_raw_object_id, limit, limit))
        return {
            "examined": limit,
            "processed": limit,
            "passage_processed": 0,
            "passage_changed": 0,
            "has_more": False,
            "next_after_raw_object_id": None,
            "body": self.secret,
        }

    def count_document_catalog_retryable(self, _user_id: str) -> int:
        self.count_calls += 1
        raise AssertionError("worker must not issue an exact corpus count")


class _KeysetCatalogStorage(_CatalogStorage):
    """In-memory owner pages with the exact storage keyset contract."""

    def __init__(self, raw_ids: list[str], *, retryable: set[str]) -> None:
        super().__init__(["alice"], reconcile_used=0)
        self.raw_ids = sorted(raw_ids)
        self.retryable = set(retryable)

    def _page(self, after_raw_object_id: str | None, limit: int) -> list[str]:
        candidates = [
            raw_id for raw_id in self.raw_ids if after_raw_object_id is None or raw_id > after_raw_object_id
        ]
        return candidates[:limit]

    def reconcile_document_catalog(
        self,
        user_id: str,
        *,
        after_raw_object_id: str | None,
        limit: int,
    ) -> dict[str, object]:
        assert user_id == "alice"
        self._thread()
        page = self._page(after_raw_object_id, limit)
        remaining = self._page(after_raw_object_id, limit + 1)
        has_more = len(remaining) > len(page)
        self.calls.append(("reconcile", user_id, after_raw_object_id, limit, len(page)))
        return {
            "examined": len(page),
            "has_more": has_more,
            "next_after_raw_object_id": page[-1] if has_more and page else None,
        }

    def backfill_document_catalog(
        self,
        user_id: str,
        *,
        after_raw_object_id: str | None,
        limit: int,
        include_document_passages: bool = False,
    ) -> dict[str, object]:
        assert user_id == "alice"
        self._thread()
        page = self._page(after_raw_object_id, limit)
        remaining = self._page(after_raw_object_id, limit + 1)
        processed = sum(raw_id in self.retryable for raw_id in page)
        self.retryable.difference_update(page)
        has_more = len(remaining) > len(page)
        self.calls.append(("backfill", user_id, after_raw_object_id, limit, len(page)))
        return {
            "examined": len(page),
            "processed": processed,
            "passage_processed": 0,
            "passage_changed": 0,
            "has_more": has_more,
            "next_after_raw_object_id": page[-1] if has_more and page else None,
        }


class _SkewedCatalogStorage(_CatalogStorage):
    """Independent phase pages for one large and several tiny owners."""

    def __init__(self, sizes: dict[str, int]) -> None:
        super().__init__(list(sizes), reconcile_used=0)
        self.sizes = sizes

    def _phase_page(
        self,
        phase: str,
        user_id: str,
        after_raw_object_id: str | None,
        limit: int,
    ) -> dict[str, object]:
        start = 0 if after_raw_object_id is None else int(after_raw_object_id) + 1
        examined = min(limit, max(0, self.sizes[user_id] - start))
        end = start + examined
        has_more = end < self.sizes[user_id]
        self._thread()
        self.calls.append((phase, user_id, after_raw_object_id, limit, examined))
        return {
            "examined": examined,
            "processed": examined,
            "passage_processed": 0,
            "passage_changed": 0,
            "has_more": has_more,
            "next_after_raw_object_id": str(end - 1) if has_more and examined else None,
        }

    def reconcile_document_catalog(
        self,
        user_id: str,
        *,
        after_raw_object_id: str | None,
        limit: int,
    ) -> dict[str, object]:
        return self._phase_page("reconcile", user_id, after_raw_object_id, limit)

    def backfill_document_catalog(
        self,
        user_id: str,
        *,
        after_raw_object_id: str | None,
        limit: int,
        include_document_passages: bool = False,
    ) -> dict[str, object]:
        return self._phase_page("backfill", user_id, after_raw_object_id, limit)


class _RecordingCatalogStorage:
    def __init__(self, storage) -> None:
        self.storage = storage
        self.reports: list[tuple[str, str, int, int]] = []

    def __getattr__(self, name: str):
        return getattr(self.storage, name)

    def reconcile_document_catalog(
        self,
        user_id: str,
        *,
        after_raw_object_id: str | None,
        limit: int,
    ) -> dict[str, object]:
        report = self.storage.reconcile_document_catalog(
            user_id,
            after_raw_object_id=after_raw_object_id,
            limit=limit,
        )
        self.reports.append(("reconcile", user_id, limit, int(report["examined"])))
        return report

    def backfill_document_catalog(
        self,
        user_id: str,
        *,
        after_raw_object_id: str | None,
        limit: int,
        include_document_passages: bool = False,
    ) -> dict[str, object]:
        report = self.storage.backfill_document_catalog(
            user_id,
            after_raw_object_id=after_raw_object_id,
            limit=limit,
            include_document_passages=include_document_passages,
        )
        self.reports.append(("backfill", user_id, limit, int(report["examined"])))
        return report


def _receipt(body: str) -> dict[str, object]:
    normalized = " ".join(body.split())
    return {
        "extraction_receipt_version": 1,
        "extraction_success": True,
        "extraction_error": "",
        "text_extraction_success": True,
        "text_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        "extraction_chars": len(body),
        "text_truncated": False,
        "archive_truncated": False,
        "source_truncated_for_parse": False,
        "parse_deadline_reached": False,
        "parse_pages_read": 0,
        "parse_pages_truncated": False,
        "parse_total_pages": 0,
        "vision_pages_total": 0,
        "vision_pages_read": 0,
        "archive_files": 0,
        "archive_files_read": 0,
        "vision_used": False,
        "vision_review_required": False,
        "unsupported_format": False,
    }


def _manager(settings, storage: _CatalogStorage) -> WorkersManager:
    return WorkersManager(replace(settings, shared_archive=False), storage, None, None)


def _state(storage: _CatalogStorage):
    state, supported = decode_document_catalog_worker_state(storage.kv.get(_DOCUMENT_CATALOG_CURSOR_KEY))
    assert supported
    return state


def test_catalog_worker_is_registered_immediately_with_a_short_timeout(settings) -> None:
    manager = _manager(settings, _CatalogStorage(["alice"]))

    manager.register_all()

    task = next(
        task
        for task in manager.supervisor._tasks
        if task.name == "document_catalog_reconcile"  # noqa: SLF001
    )
    assert task.func == manager._document_catalog_reconcile_all  # noqa: SLF001
    assert task.enabled is True
    assert task.run_immediately is True
    assert 45.0 <= task.interval_sec <= 75.0
    assert task.timeout_sec <= 120.0


def test_worker_modules_import_in_a_clean_process_without_a_storage_cycle() -> None:
    for module in ("friday.document_catalog.worker_state", "friday.account_deletion"):
        subprocess.run([sys.executable, "-c", f"import {module}"], check=True)


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

    assert [(phase, tenant) for phase, tenant, _, _, _ in storage.calls] == [
        ("reconcile", "alice"),
        ("backfill", "alice"),
        ("reconcile", "bob"),
        ("backfill", "bob"),
        ("reconcile", "carol"),
        ("backfill", "carol"),
    ]
    actual_by_tenant = {
        tenant: sum(used for _, called_tenant, _, _, used in storage.calls if called_tenant == tenant)
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
async def test_skewed_tenants_reclaim_unused_share_for_backfill_without_overspending(settings) -> None:
    storage = _SkewedCatalogStorage(
        {
            "tenant-large": 1_000,
            "tenant-small-a": 1,
            "tenant-small-b": 1,
        }
    )

    await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001

    large_backfill = [call for call in storage.calls if call[0] == "backfill" and call[1] == "tenant-large"]
    assert [limit for _phase, _tenant, _cursor, limit, _examined in large_backfill] == [1, 81]
    assert sum(examined for *_prefix, examined in large_backfill) == 82
    assert sum(examined for *_prefix, examined in storage.calls) == _DOCUMENT_CATALOG_TICK_LIMIT
    assert len(storage.kv_writes) == 1


@pytest.mark.asyncio
async def test_rotation_and_single_item_phases_survive_manager_restarts(settings) -> None:
    tenants = [f"tenant-{index:03d}" for index in range(_DOCUMENT_CATALOG_TICK_LIMIT + 2)]
    storage = _CatalogStorage(list(reversed(tenants)), reconcile_used=1)
    ticks: list[list[tuple[str, str, str | None, int, int]]] = []
    writes_per_tick: list[int] = []

    for _ in range(3):
        before = len(storage.calls)
        writes_before = len(storage.kv_writes)
        await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001
        ticks.append(storage.calls[before:])
        writes_per_tick.append(len(storage.kv_writes) - writes_before)

    assert all(len(tick) == _DOCUMENT_CATALOG_TICK_LIMIT for tick in ticks)
    assert all(sum(used for _, _, _, _, used in tick) <= _DOCUMENT_CATALOG_TICK_LIMIT for tick in ticks)
    assert writes_per_tick == [1, 1, 1], "cursor persistence regressed to one write per tenant"
    assert [tenant for _, tenant, _, _, _ in ticks[0]] == sorted(tenants)[:_DOCUMENT_CATALOG_TICK_LIMIT]
    phases_by_tenant = {
        tenant: {phase for tick in ticks for phase, called, _, _, _ in tick if called == tenant}
        for tenant in tenants
    }
    assert all(phases == {"reconcile", "backfill"} for phases in phases_by_tenant.values())

    assert storage.kv_writes
    for key, payload in storage.kv_writes:
        assert key == _DOCUMENT_CATALOG_CURSOR_KEY
        persisted, supported = decode_document_catalog_worker_state(payload)
        assert supported
        assert persisted.tenants
        assert all(tenant not in payload for tenant in tenants)


@pytest.mark.asyncio
async def test_shared_archive_converges_every_active_file_owner(settings) -> None:
    tenants = ["legacy", "telegram:42", "local:alice"]
    storage = _CatalogStorage(tenants, reconcile_used=1)
    manager = WorkersManager(replace(settings, shared_archive=True), storage, None, None)

    await manager._document_catalog_reconcile_all()  # noqa: SLF001

    assert {tenant for _phase, tenant, *_rest in storage.calls} == set(tenants)


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

    assert _DOCUMENT_CATALOG_CURSOR_KEY not in storage.kv
    assert storage.kv_writes == [], "cancellation must replay instead of guessing a checkpoint"
    storage.calls.clear()
    await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001
    assert storage.calls[0][1] == "alpha"
    assert any(tenant == "bravo" for _, tenant, _, _, _ in storage.calls), (
        "the first unprocessed tenant was skipped after cancel"
    )


@pytest.mark.asyncio
async def test_persistent_tenant_failure_stays_selected_and_keeps_health_failed(
    settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tenants = [f"tenant-{index:03d}" for index in range(_DOCUMENT_CATALOG_TICK_LIMIT * 2 + 2)]
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
    assert (_state(storage).cursor, _state(storage).round) != (5, 0)
    assert len(storage.kv_writes) == 2
    persisted = _state(storage)
    broken_state = persisted.tenants[_tenant_key(broken)]
    assert broken_state.reconcile is None
    assert broken_state.reconcile_failed is True
    assert any(tenant == tenants[200] for _phase, tenant, *_rest in storage.calls)
    assert sum(phase == "reconcile" and tenant == broken for phase, tenant, _, _, _ in storage.calls) >= 2
    assert secret not in caplog.text
    assert broken not in caplog.text


@pytest.mark.asyncio
async def test_persistent_phase_failure_does_not_starve_its_healthy_sibling(settings) -> None:
    tenants = [f"tenant-{index:03d}" for index in range(_DOCUMENT_CATALOG_TICK_LIMIT + 1)]
    broken = tenants[0]

    class _SiblingProgressStorage(_CatalogStorage):
        def backfill_document_catalog(
            self,
            user_id: str,
            *,
            after_raw_object_id: str | None,
            limit: int,
            include_document_passages: bool = False,
        ) -> dict[str, object]:
            if user_id != broken:
                return super().backfill_document_catalog(
                    user_id,
                    after_raw_object_id=after_raw_object_id,
                    limit=limit,
                    include_document_passages=include_document_passages,
                )
            self._thread()
            next_cursor = f"healthy-backfill-{len(self.calls)}"
            self.calls.append(("backfill", user_id, after_raw_object_id, limit, 1))
            return {
                "examined": 1,
                "processed": 1,
                "passage_processed": 0,
                "passage_changed": 0,
                "has_more": True,
                "next_after_raw_object_id": next_cursor,
            }

    storage = _SiblingProgressStorage(tenants, reconcile_fail={broken})
    for _ in range(2):
        with pytest.raises(WorkerBatchError):
            await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001

    broken_state = _state(storage).tenants[_tenant_key(broken)]
    assert broken_state.reconcile_failed is True
    assert broken_state.backfill is not None
    assert any(phase == "backfill" and tenant == broken for phase, tenant, *_rest in storage.calls)


@pytest.mark.asyncio
async def test_reconcile_failure_uses_only_reserved_backfill_and_isolates_next_tenant(settings) -> None:
    storage = _CatalogStorage(["bob", "alice"], reconcile_fail={"alice"})
    manager = _manager(settings, storage)

    with pytest.raises(WorkerBatchError, match="1 tenant operation"):
        await manager._document_catalog_reconcile_all()  # noqa: SLF001

    assert storage.calls == [
        ("reconcile", "alice", None, 63, 0),
        ("backfill", "alice", None, 1, 1),
        ("reconcile", "bob", None, 63, 2),
        ("backfill", "bob", None, 62, 62),
    ]
    state = _state(storage)
    assert (state.cursor, state.round) == (0, 1)
    alice = state.tenants[_tenant_key("alice")]
    assert alice == DocumentCatalogTenantState(reconcile_failed=True)


@pytest.mark.asyncio
async def test_backfill_failure_does_not_undo_reconcile_or_skip_the_next_tenant(settings) -> None:
    storage = _CatalogStorage(["bob", "alice"], backfill_fail={"alice"})

    with pytest.raises(WorkerBatchError, match="1 tenant operation"):
        await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001

    assert storage.calls == [
        ("reconcile", "alice", None, 63, 2),
        ("backfill", "alice", None, 62, 0),
        ("reconcile", "bob", None, 63, 2),
        ("backfill", "bob", None, 62, 62),
    ]
    state = _state(storage)
    assert (state.cursor, state.round) == (0, 1)
    alice = state.tenants[_tenant_key("alice")]
    assert alice.reconcile == "reconcile-cursor-1"
    assert alice.backfill is None
    assert alice.backfill_failed is True


@pytest.mark.asyncio
async def test_worker_uses_bounded_has_more_and_never_requests_an_exact_count(settings) -> None:
    storage = _CatalogStorage(["alice"], reconcile_used=1)

    await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001

    source = inspect.getsource(WorkersManager._document_catalog_reconcile_all)  # noqa: SLF001
    source += inspect.getsource(WorkersManager._document_catalog_reconcile)  # noqa: SLF001
    source += inspect.getsource(_document_catalog_phase_page)
    assert "remaining_retryable" not in source
    assert '"has_more"' in source
    assert storage.count_calls == 0


def test_worker_state_round_trips_opaque_text_without_plaintext_identity() -> None:
    tenant = "tenant-secret"
    cursors = {"reconcile": "", "backfill": " tenant-secret\n" + "ю" * 201}
    state = DocumentCatalogWorkerState(
        cursor=7,
        round=11,
        tenants={
            _tenant_key(tenant): DocumentCatalogTenantState(
                reconcile=cursors["reconcile"],
                backfill=cursors["backfill"],
            )
        },
    )

    payload = encode_document_catalog_worker_state(state)
    decoded, supported = decode_document_catalog_worker_state(payload)

    assert supported
    assert decoded == state
    assert tenant not in payload
    assert cursors["backfill"] not in payload
    with pytest.raises(ValueError, match="tenant checkpoint"):
        encode_document_catalog_worker_state(
            DocumentCatalogWorkerState(
                tenants={tenant: DocumentCatalogTenantState()},
            )
        )


@pytest.mark.asyncio
async def test_current_prefix_pending_tail_converges_across_restart_keysets(settings) -> None:
    raw_ids = [f"raw-{index:03d}" for index in range(150)]
    tail = raw_ids[-1]
    storage = _KeysetCatalogStorage(raw_ids, retryable={tail})

    calls_by_tick: list[tuple[tuple[str, str, str | None, int, int], ...]] = []
    for _ in range(12):
        writes_before = len(storage.kv_writes)
        calls_before = len(storage.calls)
        await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001
        calls_by_tick.append(tuple(storage.calls[calls_before:]))
        assert len(storage.kv_writes) - writes_before <= 1
        if not storage.retryable:
            break

    assert storage.retryable == set(), "the current prefix starved the pending tail"
    backfill_cursors = [
        cursor for phase, _tenant, cursor, _limit, _used in storage.calls if phase == "backfill"
    ]
    assert any(cursor is not None for cursor in backfill_cursors[1:])
    tenant_state = _state(storage).tenants[_tenant_key("alice")]
    assert tenant_state == DocumentCatalogTenantState()
    assert all(
        sum(used for *_prefix, used in tick_calls) <= _DOCUMENT_CATALOG_TICK_LIMIT
        for tick_calls in calls_by_tick
    )


@pytest.mark.asyncio
async def test_exact_empty_phase_cursor_is_passed_after_restart(settings) -> None:
    storage = _CatalogStorage(["alice"], reconcile_used=1)
    tenant_key = _tenant_key("alice")
    initial = DocumentCatalogWorkerState(
        tenants={
            tenant_key: DocumentCatalogTenantState(
                reconcile=" " + "x" * 201,
                backfill="",
            )
        },
    )
    storage.kv[_DOCUMENT_CATALOG_CURSOR_KEY] = encode_document_catalog_worker_state(initial)

    await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001

    assert storage.calls[0][2] == " " + "x" * 201
    assert next(call for call in storage.calls if call[0] == "backfill")[2] == ""


@pytest.mark.asyncio
async def test_malformed_state_fails_health_without_mutation(settings) -> None:
    storage = _CatalogStorage(["alice"], reconcile_used=1)
    storage.kv[_DOCUMENT_CATALOG_CURSOR_KEY] = '{"version":999,"tenant":"alice"}'

    with pytest.raises(RuntimeError, match="state is unsupported"):
        await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001

    assert storage.calls == []
    assert storage.kv_writes == []
    assert storage.kv[_DOCUMENT_CATALOG_CURSOR_KEY] == '{"version":999,"tenant":"alice"}'


@pytest.mark.asyncio
async def test_deleted_owner_cannot_be_resurrected_by_expected_none_checkpoint(settings) -> None:
    storage = _CatalogStorage(["alice"], reconcile_used=1)

    def delete_before_checkpoint() -> None:
        storage.tenants.clear()

    storage.checkpoint_hook = delete_before_checkpoint

    with pytest.raises(RuntimeError, match="changed during checkpoint"):
        await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001

    assert storage.kv.get(_DOCUMENT_CATALOG_CURSOR_KEY) is None
    assert storage.kv_writes == []


def test_storage_checkpoint_rejects_an_active_owner_tombstone(storage) -> None:
    owner = "local:catalog-tombstone"
    storage.ensure_user(owner)
    body = "# Tombstone race\nBody"
    storage.store_raw_object(
        RawObject(
            id="raw-catalog-tombstone",
            user_id=owner,
            source="upload",
            source_ref="catalog-tombstone:1",
            raw_content=body,
            content_type="file",
            metadata_json=_receipt(body),
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
        )
    )
    namespace_key = load_document_catalog_worker_namespace_key(storage)
    state = DocumentCatalogWorkerState(
        tenants={
            _tenant_key(owner, namespace_key=namespace_key): DocumentCatalogTenantState(
                reconcile="raw-catalog-tombstone"
            )
        }
    )
    storage.kv_set(deleted_account_tombstone_key(owner), "{}")

    assert (
        storage.checkpoint_document_catalog_worker_state(
            expected_value=None,
            value=encode_document_catalog_worker_state(state),
            tenant_ids=[owner],
        )
        is False
    )
    assert storage.kv_get(_DOCUMENT_CATALOG_CURSOR_KEY) is None


@pytest.mark.asyncio
async def test_owner_page_cost_excludes_disabled_foreign_corpus(settings, storage) -> None:
    owners = ("alice", "foreign")
    for owner in owners:
        storage.ensure_user(owner)
    for index in range(40):
        body = f"# Foreign {index}\nBody"
        storage.store_raw_object(
            RawObject(
                id=f"raw-foreign-{index:03d}",
                user_id="foreign",
                source="upload",
                source_ref=f"foreign:{index}",
                raw_content=body,
                content_type="file",
                metadata_json=_receipt(body),
                content_hash=hashlib.sha256(f"foreign-{index}".encode()).hexdigest(),
            )
        )
    target_body = "# Target\nBody"
    target = storage.store_raw_object(
        RawObject(
            id="raw-target",
            user_id="alice",
            source="upload",
            source_ref="target:1",
            raw_content=target_body,
            content_type="file",
            metadata_json=_receipt(target_body),
            content_hash=hashlib.sha256(b"target").hexdigest(),
        )
    )
    with storage.transaction() as conn:
        conn.execute(
            """UPDATE document_catalog
                  SET enrichment_status='incomplete',incomplete_reason='backfill_pending',
                      extracted_text_sha256=NULL,semantic_title=NULL
                WHERE raw_object_id=?""",
            (target.id,),
        )
    storage.update_user("foreign", status="disabled")
    recording = _RecordingCatalogStorage(storage)

    statements: list[str] = []
    storage.conn.set_trace_callback(statements.append)
    try:
        assert storage.list_document_catalog_owner_ids() == ["alice"]
    finally:
        storage.conn.set_trace_callback(None)
    inventory_query = next(
        statement for statement in statements if "idx_document_catalog_source_owner_id" in statement
    )
    plan = storage.execute("EXPLAIN QUERY PLAN " + inventory_query).fetchall()
    details = "\n".join(str(row[3]) for row in plan)
    assert "idx_document_catalog_source_owner_id" in details
    assert "TEMP B-TREE" not in details

    await _manager(settings, recording)._document_catalog_reconcile_all()  # noqa: SLF001

    assert recording.reports
    assert {user_id for _phase, user_id, _limit, _examined in recording.reports} == {"alice"}
    assert all(examined == 1 and examined <= limit for _phase, _user, limit, examined in recording.reports)
    entry = storage.get_document_catalog_entry("alice", target.id)
    assert entry is not None and entry["enrichment_status"] == "current"


@pytest.mark.asyncio
async def test_out_of_bounds_phase_report_fails_without_advancing_that_cursor(settings) -> None:
    class _OverspendingStorage(_CatalogStorage):
        def reconcile_document_catalog(
            self,
            user_id: str,
            *,
            after_raw_object_id: str | None,
            limit: int,
        ) -> dict[str, object]:
            self.calls.append(("reconcile", user_id, after_raw_object_id, limit, limit + 1))
            return {
                "examined": limit + 1,
                "has_more": True,
                "next_after_raw_object_id": "must-not-commit",
            }

    storage = _OverspendingStorage(["alice"])

    with pytest.raises(WorkerBatchError):
        await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001

    tenant_state = _state(storage).tenants[_tenant_key("alice")]
    assert tenant_state.reconcile is None
    assert tenant_state.reconcile_failed is True
    assert all(
        limit <= _DOCUMENT_CATALOG_TICK_LIMIT for _phase, _user, _cursor, limit, _used in storage.calls
    )


@pytest.mark.asyncio
async def test_out_of_bounds_passage_report_fails_without_advancing_backfill_cursor(settings) -> None:
    class _OverspendingPassageStorage(_CatalogStorage):
        def backfill_document_catalog(
            self,
            user_id: str,
            *,
            after_raw_object_id: str | None,
            limit: int,
            include_document_passages: bool = False,
        ) -> dict[str, object]:
            assert include_document_passages is True
            self.calls.append(("backfill", user_id, after_raw_object_id, limit, 1))
            return {
                "examined": 1,
                "processed": 0,
                "passage_processed": 2,
                "passage_changed": 1,
                "has_more": True,
                "next_after_raw_object_id": "must-not-commit",
            }

    storage = _OverspendingPassageStorage(["alice"], reconcile_used=0)

    with pytest.raises(WorkerBatchError):
        await _manager(settings, storage)._document_catalog_reconcile_all()  # noqa: SLF001

    tenant_state = _state(storage).tenants[_tenant_key("alice")]
    assert tenant_state.backfill is None
    assert tenant_state.backfill_failed is True


def test_account_deletion_owns_only_its_hashed_worker_checkpoint(storage) -> None:
    actor = "local:catalog-worker-admin"
    # This id is also a substring of the global runtime key. Ownership must come
    # from the hashed entry, never from an ambiguous substring match.
    target = "catalog"
    neighbour = "local:catalog-worker-neighbour"
    storage.ensure_user(actor, preset_key="admin")
    storage.ensure_user(target)
    storage.ensure_user(neighbour)
    assert _mark_account_deletion_history_clean(storage, target) is True
    namespace_key = load_document_catalog_worker_namespace_key(storage)
    target_key = _tenant_key(target, namespace_key=namespace_key)
    neighbour_key = _tenant_key(neighbour, namespace_key=namespace_key)
    worker_state = DocumentCatalogWorkerState(
        cursor=2,
        round=3,
        tenants={
            target_key: DocumentCatalogTenantState(reconcile="target-raw"),
            neighbour_key: DocumentCatalogTenantState(backfill="neighbour-raw"),
        },
    )
    storage.kv_set(
        _DOCUMENT_CATALOG_CURSOR_KEY,
        encode_document_catalog_worker_state(worker_state),
    )
    storage.update_user(target, status="disabled")

    plan = preflight_account_deletion(storage, target, quiescence_available=True)
    assert plan["ready"] is True, plan
    assert plan["counts"]["document_catalog_worker_state"] == 1
    result = delete_account(
        storage,
        target,
        expected_fingerprint=plan["fingerprint"],
        actor_user_id=actor,
        quiescence_verified=True,
    )

    assert result["deleted"]["document_catalog_worker_state"] == 1
    retained, supported = decode_document_catalog_worker_state(storage.kv_get(_DOCUMENT_CATALOG_CURSOR_KEY))
    assert supported
    assert target_key not in retained.tenants
    assert retained.tenants[neighbour_key].backfill == "neighbour-raw"


@pytest.mark.parametrize("initial_cursor", [None, "before-review"])
def test_account_deletion_fingerprint_binds_the_exact_worker_entry(storage, initial_cursor) -> None:
    suffix = "absent" if initial_cursor is None else "changed"
    actor = f"local:catalog-worker-admin-{suffix}"
    target = f"local:catalog-worker-target-{suffix}"
    storage.ensure_user(actor, preset_key="admin")
    storage.ensure_user(target)
    assert _mark_account_deletion_history_clean(storage, target) is True
    namespace_key = load_document_catalog_worker_namespace_key(storage)
    target_key = _tenant_key(target, namespace_key=namespace_key)
    if initial_cursor is not None:
        initial_state = DocumentCatalogWorkerState(
            tenants={target_key: DocumentCatalogTenantState(reconcile=initial_cursor)}
        )
        storage.kv_set(
            _DOCUMENT_CATALOG_CURSOR_KEY,
            encode_document_catalog_worker_state(initial_state),
        )
    storage.update_user(target, status="disabled")
    plan = preflight_account_deletion(storage, target, quiescence_available=True)
    assert plan["ready"] is True, plan

    changed_state = DocumentCatalogWorkerState(
        tenants={target_key: DocumentCatalogTenantState(reconcile="after-review")}
    )
    storage.kv_set(
        _DOCUMENT_CATALOG_CURSOR_KEY,
        encode_document_catalog_worker_state(changed_state),
    )
    with pytest.raises(AccountDeletionConflict, match="изменилась"):
        delete_account(
            storage,
            target,
            expected_fingerprint=plan["fingerprint"],
            actor_user_id=actor,
            quiescence_verified=True,
        )

    assert storage.get_user(target) is not None
