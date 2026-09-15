from __future__ import annotations

import errno
import gc
import os
import sqlite3
import threading
import weakref
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from friday.server import create_app
from friday.storage import FridayStorage, _core


def _assert_closed(fd: int) -> None:
    with pytest.raises(OSError) as error:
        os.fstat(fd)
    assert error.value.errno == errno.EBADF


def _database_fds(path: Path) -> dict[int, str]:
    result = {}
    for entry in Path("/proc/self/fd").iterdir():
        try:
            target = os.readlink(entry)
        except FileNotFoundError:
            continue
        if target in {str(path), str(path) + "-wal", str(path) + "-shm"}:
            result[int(entry.name)] = target
    return result


@pytest.mark.parametrize("cycle", [False, True], ids=["abandoned", "owner-cycle"])
def test_abandoned_storage_gc_releases_provenance_and_sqlite_handles(settings, cycle):
    def abandon():
        store = FridayStorage(settings)
        conn = store.conn
        token = store._main_file_provenance_token(conn)
        if cycle:
            # The cyclic owner retains its thread-local connection and provenance token.
            store.owner_cycle = store
        assert conn.execute("SELECT 1").fetchone()[0] == 1
        return token._fd, weakref.ref(store)

    gc.collect()
    assert _database_fds(settings.database_path) == {}
    descriptor, owner = abandon()
    try:
        gc.collect()
        assert owner() is None
        _assert_closed(descriptor)
        assert _database_fds(settings.database_path) == {}
    finally:
        # Keep the failing pre-fix observation isolated, too.
        remaining = owner()
        if remaining is not None:
            remaining.close(final=True)
        if descriptor in _database_fds(settings.database_path):
            os.close(descriptor)


@pytest.mark.parametrize("final", [False, True], ids=["reopenable", "final"])
def test_explicit_storage_close_releases_handles_before_gc(settings, final):
    store = FridayStorage(settings)
    conn = store.conn
    token = store._main_file_provenance_token(conn)
    descriptor = token._fd
    store.close(final=final)
    store.close(final=final)
    _assert_closed(descriptor)
    assert _database_fds(settings.database_path) == {}
    with pytest.raises(sqlite3.OperationalError, match="provenance is invalid"):
        token.validate(conn)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")
    if not final:
        reopened = store.conn
        assert reopened is not conn
        store._main_file_provenance_token(reopened).validate(reopened)
        store.close(final=True)
    assert _database_fds(settings.database_path) == {}


@pytest.mark.parametrize("failure", ["connect", "schema", "token-construction", "finalizer-registration"])
def test_failed_real_storage_open_releases_untransferred_provenance(settings, monkeypatch, failure):
    store = FridayStorage(settings)
    descriptors = []
    acquire = _core._acquire_main_file_provenance
    closed = []
    original_close = os.close

    def track_close(fd):
        if fd in descriptors:
            closed.append(fd)
        original_close(fd)

    def capture(path):
        held = acquire(path)
        descriptors.append(held[0])
        return held

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("injected open boundary failure")

    monkeypatch.setattr(_core, "_acquire_main_file_provenance", capture)
    monkeypatch.setattr(_core.os, "close", track_close)
    if failure == "connect":
        monkeypatch.setattr(_core.sqlite3, "connect", fail)
    elif failure == "schema":
        monkeypatch.setattr(store, "_ensure_schema", fail)
    elif failure == "token-construction":
        monkeypatch.setattr(_core._MainFileProvenanceToken, "__init__", fail)
    else:
        # Fail after validated token fields exist, before cleanup registration.
        monkeypatch.setattr(_core.weakref, "finalize", fail)
    with pytest.raises(sqlite3.OperationalError, match="injected open boundary failure"):
        _ = store.conn
    store.close(final=True)
    gc.collect()
    assert len(descriptors) == 1
    assert closed == descriptors
    _assert_closed(descriptors[0])
    assert _database_fds(settings.database_path) == {}


@pytest.mark.parametrize("invalid", ["connection", "device", "inode"])
def test_rejected_token_construction_leaves_descriptor_with_caller(tmp_path, invalid):
    path = tmp_path / "untransferred"
    path.touch()
    descriptor = os.open(path, os.O_RDONLY)
    conn = sqlite3.connect(":memory:")
    metadata = os.fstat(descriptor)
    arguments = dict(connection=conn, fd=descriptor, device=metadata.st_dev, inode=metadata.st_ino)
    arguments[invalid] = object() if invalid == "connection" else -1
    try:
        with pytest.raises(sqlite3.OperationalError, match="provenance is invalid"):
            _core._MainFileProvenanceToken(**arguments)
        gc.collect()
        assert os.fstat(descriptor).st_ino == metadata.st_ino
    finally:
        conn.close()
        os.close(descriptor)


@pytest.mark.parametrize("concurrent", [False, True], ids=["repeated", "concurrent"])
def test_closed_token_gc_never_closes_a_reused_descriptor(tmp_path, concurrent):
    path = tmp_path / "original"
    path.touch()
    descriptor = os.open(path, os.O_RDONLY)
    metadata = os.fstat(descriptor)
    conn = sqlite3.connect(":memory:")
    token = _core._MainFileProvenanceToken(
        connection=conn,
        fd=descriptor,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )
    try:
        if concurrent:
            barrier = threading.Barrier(5)

            def close(held, rendezvous):
                rendezvous.wait(timeout=5)
                held.close()

            workers = [threading.Thread(target=close, args=(token, barrier)) for _ in range(4)]
            for worker in workers:
                worker.start()
            barrier.wait(timeout=5)
            for worker in workers:
                worker.join(timeout=5)
                assert not worker.is_alive()
        else:
            token.close()
        _assert_closed(descriptor)
        reused = os.open(tmp_path / "replacement", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            # No unrelated descriptor is overwritten if allocation was noncontiguous.
            if reused != descriptor:
                os.dup2(reused, descriptor)
                os.close(reused)
                reused = descriptor
            token.close()
            del token
            gc.collect()
            os.write(reused, b"still owned by replacement")
            os.lseek(reused, 0, os.SEEK_SET)
            assert os.read(reused, 64) == b"still owned by replacement"
        finally:
            os.close(reused)
    finally:
        conn.close()


def test_live_token_retains_connection_identity_until_its_owner_releases_it(settings):
    store = FridayStorage(settings)
    conn = store.conn
    token = store._main_file_provenance_token(conn)
    foreign = sqlite3.connect(":memory:")
    try:
        gc.collect()
        token.validate(conn)
        assert token.bound_to(conn)
        with pytest.raises(sqlite3.OperationalError, match="provenance is invalid"):
            token.validate(foreign)
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        foreign.close()
        store.close(final=True)


def test_successful_testclient_shutdown_closes_every_owned_connection(settings):
    def run_app():
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/api/obsidian/status").status_code == 401
            store = app.state.storage
            connections = tuple(store._connections)
            tokens = tuple(store._provenance_tokens)
            descriptors = tuple(token._fd for token in tokens)
            assert connections and descriptors
        assert store._shut_down
        assert store._connections == []
        assert store._provenance_tokens == []
        for fd in descriptors:
            _assert_closed(fd)
        for conn in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                conn.execute("SELECT 1")
        # The read-only memory adapter belongs to app, independently of Storage.
        # Observe collection after releasing that owner as well as the client.
        return weakref.ref(app), weakref.ref(store)

    app, store = run_app()
    gc.collect()
    assert app() is None
    assert store() is None
    assert _database_fds(settings.database_path) == {}
