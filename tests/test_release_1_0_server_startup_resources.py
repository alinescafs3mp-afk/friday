"""Server lifespan must retire every SQLite handle even before reaching yield."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

import friday.server as server
from friday.diagnostics.runtime_lease import ProcessLease, inspect_process_lease
from friday.storage import StorageClosedError


@pytest.mark.parametrize("point", ["normal", "workers", "mcp"])
def test_server_startup_exit_permanently_closes_storage_before_releasing_role(settings, monkeypatch, point):
    configured = replace(
        settings,
        mcp_enabled=point == "mcp",
        obsidian_enabled=False,
        semantic_supervisor_mode="off",
        secondary_llm_enabled=False,
        workers_enabled=False,
    )
    captured, connections, fault_seen = [], [], []
    original_init = server.init_storage
    original_workers_start = server.WorkersManager.start
    lock = configured.state_dir / "backend.lock"

    def capture_storage(received):
        assert received is configured
        assert inspect_process_lease(lock, protocol="friday.backend.v1")["active"] is True
        storage = original_init(received)
        captured.append(storage)
        # Open only the real lifespan-thread handle. Later connections are
        # captured at the fault boundary, without creating a main-thread one.
        connections.append(storage.conn)
        return storage

    def fail_here(name):
        assert len(captured) == 1
        storage = captured[0]
        assert not storage._shut_down
        with storage._registry_lock:
            for connection in storage._connections:
                if not any(connection is old for old in connections):
                    connections.append(connection)
        fault_seen.append(name)
        raise RuntimeError("owned startup fault: " + name)

    async def start_workers(workers):
        if point == "workers":
            fail_here("workers")
        await original_workers_start(workers)

    async def fail_mcp(_manager):
        fail_here("mcp")

    monkeypatch.setattr(server, "init_storage", capture_storage)
    monkeypatch.setattr(server.WorkersManager, "start", start_workers)
    if point == "mcp":
        # Real MCP manager/context construction, refusal before its process or
        # network starts. Its normal close is still executed by the lifespan.
        monkeypatch.setattr(server.MCPClientManager, "start", fail_mcp)
    app = server.create_app(configured)
    try:
        if point == "normal":
            with TestClient(app) as client:
                response = client.get("/api/health")
                assert response.status_code == 200
                assert inspect_process_lease(lock, protocol="friday.backend.v1")["active"] is True
            assert fault_seen == []
        else:
            with pytest.raises(RuntimeError, match="owned startup fault: " + point), TestClient(app):
                pytest.fail("startup fault unexpectedly reached ready state")
            assert fault_seen == [point]
        assert len(captured) == 1 and connections
        storage = captured[0]
        closed = []
        for connection in connections:
            try:
                connection.execute("SELECT 1").fetchone()
            except sqlite3.ProgrammingError as error:
                assert "closed" in str(error)
                closed.append(True)
            else:
                closed.append(False)
        # Measure all properties before the first failure and before fallback,
        # so a failing snapshot includes actual lease and connection evidence.
        observed = {
            "final": storage._shut_down,
            "registered_connections": len(storage._connections),
            "captured_connections_closed": closed,
            "lease_active": inspect_process_lease(lock, protocol="friday.backend.v1")["active"],
        }
        assert observed == {
            "final": True,
            "registered_connections": 0,
            "captured_connections_closed": [True] * len(connections),
            "lease_active": False,
        }
        with pytest.raises(StorageClosedError):
            storage.execute("SELECT 1")
        with ProcessLease(lock, protocol="friday.backend.v1"):
            assert inspect_process_lease(lock, protocol="friday.backend.v1")["active"] is True
    finally:
        # Isolated fixture fallback after all product observations; never
        # cleanup credit. The TestClient already exited its owned event loop.
        for storage in captured:
            storage.close(final=True)


@pytest.mark.asyncio
async def test_server_early_startup_failure_finalizes_storage_before_role_release(settings, monkeypatch):
    captured = []
    original_init = server.init_storage

    def capture(received):
        storage = original_init(received)
        captured.append(storage)
        return storage

    def fail_namespace(_connection):
        raise RuntimeError("owned namespace startup fault")

    monkeypatch.setattr(server, "init_storage", capture)
    monkeypatch.setattr(server, "load_trace_namespace_key", fail_namespace)
    app = server.create_app(settings)
    try:
        with pytest.raises(RuntimeError, match="owned namespace startup fault"):
            async with app.router.lifespan_context(app):
                pytest.fail("early startup refusal reached ready state")
        assert len(captured) == 1
        assert captured[0]._shut_down and not captured[0]._connections
        with pytest.raises(StorageClosedError):
            captured[0].execute("SELECT 1")
        assert not inspect_process_lease(settings.state_dir / "backend.lock", protocol="friday.backend.v1")[
            "active"
        ]
    finally:
        for storage in captured:
            storage.close(final=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True], ids=["error", "cancelled"])
async def test_server_startup_retirement_waits_for_physical_database_work(settings, monkeypatch, cancelled):
    import asyncio
    import contextlib
    import threading

    from friday.workers._blocking import current_task, run_blocking, wait_until_idle_async

    configured = replace(settings, mcp_enabled=False, obsidian_enabled=False, workers_enabled=False)
    captured, observations = [], []
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    draining = asyncio.Event()
    original_init = server.init_storage
    lock = configured.state_dir / "backend.lock"

    def capture(received):
        storage = original_init(received)
        captured.append(storage)
        return storage

    def physical_reader():
        storage = captured[0]
        connection = storage.conn
        started.set()
        try:
            assert release.wait(5), "fixture did not release the physical reader"
            observations.append(
                (
                    connection.execute("SELECT 1").fetchone()[0],
                    storage._shut_down,
                    inspect_process_lease(lock, protocol="friday.backend.v1")["active"],
                )
            )
        finally:
            finished.set()

    async def fail_workers(_workers):
        token = current_task.set("owned-startup-reader")
        try:
            work = asyncio.create_task(run_blocking(physical_reader))
        finally:
            current_task.reset(token)
        assert await asyncio.to_thread(started.wait, 5)
        work.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await work
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError("owned startup reader fault")

    async def observe_drain(timeout):
        draining.set()
        return await wait_until_idle_async(timeout)

    monkeypatch.setattr(server, "init_storage", capture)
    monkeypatch.setattr(server.WorkersManager, "start", fail_workers)
    monkeypatch.setattr(server, "wait_until_idle_async", observe_drain)
    app = server.create_app(configured)

    async def enter():
        async with app.router.lifespan_context(app):
            pytest.fail("failed startup reached ready state")

    lifetime = asyncio.create_task(enter())
    try:
        await asyncio.wait_for(draining.wait(), 5)
        assert not lifetime.done() and not finished.is_set()
        assert not captured[0]._shut_down
        assert inspect_process_lease(lock, protocol="friday.backend.v1")["active"]
        release.set()
        with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
            await asyncio.wait_for(lifetime, 5)
        assert observations == [(1, False, True)]
        assert captured[0]._shut_down and not captured[0]._connections
        assert not inspect_process_lease(lock, protocol="friday.backend.v1")["active"]
        with pytest.raises(StorageClosedError):
            captured[0].execute("SELECT 1")
    finally:
        release.set()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(lifetime, 5)
        assert await asyncio.to_thread(finished.wait, 5)
        for storage in captured:
            storage.close(final=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["workers-drain", "restart-off", "restart-assist"])
async def test_server_repeated_task_cancellation_retains_physical_reader_and_role(
    settings, monkeypatch, point
):
    import asyncio
    import contextlib
    import threading

    from friday.workers._blocking import current_task, run_blocking, wait_until_idle_async

    configured = replace(
        settings,
        mcp_enabled=False,
        obsidian_enabled=False,
        workers_enabled=False,
        semantic_supervisor_mode="assist" if point == "restart-assist" else "off",
    )
    captured, observations = [], []
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    drains = asyncio.Queue()
    original_init = server.init_storage
    lock = configured.state_dir / "backend.lock"

    def capture(received):
        storage = original_init(received)
        captured.append(storage)
        return storage

    def physical_reader():
        storage = captured[0]
        connection = storage.conn
        started.set()
        try:
            assert release.wait(5), "fixture did not release the physical reader"
            observations.append(
                (
                    connection.execute("SELECT 1").fetchone()[0],
                    storage._shut_down,
                    inspect_process_lease(lock, protocol="friday.backend.v1")["active"],
                )
            )
            return ()
        finally:
            finished.set()

    async def blocked_workers(_workers):
        token = current_task.set("owned-startup-reader")
        try:
            await run_blocking(physical_reader)
        finally:
            current_task.reset(token)

    async def observe_drain(timeout):
        drains.put_nowait(True)
        return await wait_until_idle_async(timeout)

    monkeypatch.setattr(server, "init_storage", capture)
    monkeypatch.setattr(server, "wait_until_idle_async", observe_drain)
    if point == "workers-drain":
        monkeypatch.setattr(server.WorkersManager, "start", blocked_workers)
    else:
        monkeypatch.setattr(
            server.SupervisorAssistGraphAdapter,
            "reconcile_all_active_after_restart",
            lambda _adapter, **_kwargs: physical_reader(),
        )
        monkeypatch.setattr(server, "build_supervisor_assist_production_runtime", lambda **_kwargs: None)
    app = server.create_app(configured)

    async def enter():
        async with app.router.lifespan_context(app):
            pytest.fail("cancelled startup reached ready state")

    lifetime = asyncio.create_task(enter())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        lifetime.cancel()
        await asyncio.wait_for(drains.get(), 5)
        for _ in range(2):
            assert not lifetime.done() and not finished.is_set()
            assert not captured[0]._shut_down
            assert inspect_process_lease(lock, protocol="friday.backend.v1")["active"]
            lifetime.cancel()
            await asyncio.wait_for(drains.get(), 5)
        assert not lifetime.done() and not finished.is_set()
        assert inspect_process_lease(lock, protocol="friday.backend.v1")["active"]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(lifetime, 5)
        assert observations == [(1, False, True)]
        assert captured[0]._shut_down and not captured[0]._connections
        assert not inspect_process_lease(lock, protocol="friday.backend.v1")["active"]
        with pytest.raises(StorageClosedError):
            captured[0].execute("SELECT 1")
    finally:
        release.set()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(lifetime, 5)
        assert await asyncio.to_thread(finished.wait, 5)
        for storage in captured:
            storage.close(final=True)
