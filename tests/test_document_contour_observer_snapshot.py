"""Owner-only, content-free state used by the inter-run live observer."""

from __future__ import annotations

import inspect
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


def _backend_lease(settings):
    from friday.diagnostics.runtime_lease import ProcessLease

    return ProcessLease(settings.state_dir / "backend.lock", protocol="friday.backend.v1")


def _stopped_queue(settings, *, pending_update_id: int | None = None):
    from friday.telegram_bridge import _UpdateInbox

    path = settings.state_dir / "telegram-inbox.sqlite3"
    inbox = _UpdateInbox(str(path))
    try:
        if pending_update_id is not None:
            inbox.store({"update_id": pending_update_id, "message": {"text": "private"}})
        else:
            inbox.get_offset()
    finally:
        inbox.close()
    return path


def _hide_pending_notification(storage) -> str:
    from friday.storage.models import Entity, EntityType, new_id

    storage.ensure_user("owner", preset_key="owner")
    private = Entity(new_id("ent"), "owner", "PRIVATE-OBSERVER-CANARY-5d88", EntityType.EVENT)
    storage.create_entity(private)
    assert storage.enqueue_notification(
        "owner",
        "5001",
        f"Chronicle copied {private.name}",
        kind="chronicle",
        dedup_key="chronicle:document-contour-observer",
    )
    with storage.transaction() as conn:
        conn.execute(
            """INSERT INTO entity_time(
                   entity_id, user_id, occurred_at, precision, source, updated_at)
               VALUES(?, 'owner', '2026-08-14T09:00:00Z', 'day', 'reminder:bob', ?)""",
            (private.id, "2026-08-14T08:00:00Z"),
        )
        conn.execute(
            """INSERT INTO private_entity_owners(entity_id, person_id, privacy_kind, created_at)
               VALUES(?, 'bob', 'reminder', ?)""",
            (private.id, "2026-08-14T08:00:00Z"),
        )
    return private.name


def test_observer_counts_physical_outbound_and_only_bridge_aggregates(settings, storage):
    from friday.diagnostics import collect_document_contour_observer_snapshot
    from friday.telegram_bridge import _UpdateInbox

    private_canary = _hide_pending_notification(storage)
    assert storage.diagnostics()["outbound_pending"] == 0

    queue_path = settings.state_dir / "telegram-inbox.sqlite3"
    inbox = _UpdateInbox(str(queue_path))
    try:
        inbox.store({"update_id": 1, "message": {"text": "pending-private-body"}})
        inbox.store({"update_id": 2, "message": {"text": "dead-private-body"}})
        inbox.mark_dead_letter(2, "SECRET-LAST-ERROR-0bc6")
    finally:
        inbox.close()

    lease = _backend_lease(settings)
    lease.acquire()
    try:
        snapshot = collect_document_contour_observer_snapshot(settings, storage)
    finally:
        lease.release()

    assert snapshot == {
        "schema": "friday.document-contour-observer-snapshot.v1",
        "backend_pid": os.getpid(),
        "backend_lease_owned": True,
        "physical_outbound_pending": 1,
        "bridge_queue_state": "present",
        "bridge_lease_acquired_for_snapshot": True,
        "bridge_lease_released": True,
        "inbound_pending": 1,
        "dead_letter": 1,
    }
    public = repr(snapshot)
    for forbidden in (
        private_canary,
        "pending-private-body",
        "dead-private-body",
        "SECRET-LAST-ERROR",
        str(queue_path),
    ):
        assert forbidden not in public


def test_observer_queue_projection_never_reads_last_error() -> None:
    import friday.diagnostics as diagnostics

    source = inspect.getsource(diagnostics._bridge_queue_counts_only)  # noqa: SLF001
    assert "last_error" not in source
    assert "payload_json" not in source


def test_active_bridge_lease_prevents_any_queue_open(settings, storage, monkeypatch):
    from friday.diagnostics import collect_document_contour_observer_snapshot
    from friday.diagnostics.runtime_lease import ProcessLease

    backend = _backend_lease(settings)
    bridge = ProcessLease(
        settings.state_dir / "telegram-inbox.sqlite3.lock",
        protocol="friday.telegram-bridge.v1",
    )
    backend.acquire()
    bridge.acquire()
    monkeypatch.setattr(
        "friday.diagnostics._bridge_queue_counts_only",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("active queue was opened")),
    )
    try:
        snapshot = collect_document_contour_observer_snapshot(settings, storage)
    finally:
        bridge.release()
        backend.release()

    assert snapshot["bridge_queue_state"] == "active_uninspected"
    assert snapshot["bridge_lease_acquired_for_snapshot"] is False
    assert snapshot["bridge_lease_released"] is False
    assert snapshot["inbound_pending"] is None
    assert snapshot["dead_letter"] is None


def test_ambiguous_lost_bridge_lease_is_closed_without_queue_open(settings, storage, monkeypatch):
    import friday.diagnostics as diagnostics
    from friday.diagnostics.runtime_lease import RuntimeLeaseError

    class LosingBoundary:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def acquire(self) -> None:
            raise RuntimeLeaseError("synthetic race")

    backend = _backend_lease(settings)
    backend.acquire()
    monkeypatch.setattr(diagnostics, "ProcessLease", LosingBoundary)
    monkeypatch.setattr(
        diagnostics,
        "inspect_process_lease",
        lambda *_a, **_k: {"state": "unknown", "active": None, "protocol_matches": True},
    )
    monkeypatch.setattr(
        diagnostics,
        "_bridge_queue_counts_only",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("uncertain queue was opened")),
    )
    try:
        snapshot = diagnostics.collect_document_contour_observer_snapshot(settings, storage)
    finally:
        backend.release()

    assert snapshot["bridge_queue_state"] == "lease_unavailable"
    assert snapshot["bridge_lease_acquired_for_snapshot"] is False
    assert snapshot["bridge_lease_released"] is False
    assert snapshot["inbound_pending"] is None
    assert snapshot["dead_letter"] is None


def test_release_failure_cannot_return_a_successful_snapshot(settings, storage, monkeypatch):
    import friday.diagnostics as diagnostics
    from friday.diagnostics.runtime_lease import ProcessLease

    class BrokenRelease:
        def __init__(self, *args, **kwargs) -> None:
            self.delegate = ProcessLease(*args, **kwargs)

        @property
        def acquired(self) -> bool:
            return self.delegate.acquired

        @property
        def held_file_identity(self):
            return self.delegate.held_file_identity

        def acquire(self) -> None:
            self.delegate.acquire()

        def release(self) -> None:
            self.delegate.release()
            raise OSError("synthetic release uncertainty")

    _stopped_queue(settings)
    backend = _backend_lease(settings)
    backend.acquire()
    monkeypatch.setattr(diagnostics, "ProcessLease", BrokenRelease)
    try:
        with pytest.raises(OSError, match="release uncertainty"):
            diagnostics.collect_document_contour_observer_snapshot(settings, storage)
    finally:
        backend.release()


def test_snapshot_requires_this_process_to_own_the_backend_lease(settings, storage, monkeypatch):
    import friday.diagnostics as diagnostics

    monkeypatch.setattr(
        diagnostics,
        "_bridge_queue_counts_only",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("queue was opened without backend authority")),
    )
    with pytest.raises(RuntimeError, match="live backend lease"):
        diagnostics.collect_document_contour_observer_snapshot(settings, storage)


def test_http_snapshot_is_owner_only_and_numeric_loopback_only(settings):
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app, client=("127.0.0.1", 9000)) as local:
        _stopped_queue(settings)
        response = local.get("/api/admin/document-contour-observer-snapshot", headers=headers)
        assert response.status_code == 200
        assert response.json()["backend_pid"] == os.getpid()

    remote_app = create_app(settings)
    with TestClient(remote_app, client=("203.0.113.9", 9000)) as remote:
        response = remote.get("/api/admin/document-contour-observer-snapshot", headers=headers)
        assert response.status_code == 403
        assert response.json() == {"detail": "Проверка барьера релиза доступна только локально на сервере"}


def test_delegated_diagnostics_capability_is_not_owner_authority(settings, storage):
    from friday.admin_api._overview import _document_contour_observer_snapshot_sync
    from friday.permissions import ActorContext

    class AllowingAuth:
        def require(self, *_args, **_kwargs) -> None:
            pass

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_service=AllowingAuth(),
                settings=settings,
                storage=storage,
            )
        ),
        state=SimpleNamespace(
            actor=ActorContext(
                "tenant",
                "owner",
                "api",
                shared_tenant=True,
                person_id="delegate",
            )
        ),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    with pytest.raises(HTTPException) as raised:
        _document_contour_observer_snapshot_sync(request)
    assert raised.value.status_code == 403


def test_queue_path_replaced_after_validation_cannot_publish_zero(settings, storage, monkeypatch):
    import friday.diagnostics as diagnostics
    from friday.admin_api._overview import _document_contour_observer_snapshot_sync
    from friday.permissions import ActorContext

    queue = _stopped_queue(settings, pending_update_id=41)
    replacement_settings = SimpleNamespace(state_dir=settings.state_dir / "replacement")
    replacement = _stopped_queue(replacement_settings)
    saved = queue.with_name("saved-one-pending.sqlite3")
    original_connect = diagnostics.sqlite3.connect
    swapped = False

    def swapping_connect(database, *args, **kwargs):
        nonlocal swapped
        if not swapped and "/proc/self/fd/" in str(database):
            os.replace(queue, saved)
            queue.symlink_to(replacement)
            swapped = True
        return original_connect(database, *args, **kwargs)

    class AllowingAuth:
        def require(self, *_args, **_kwargs) -> None:
            pass

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_service=AllowingAuth(),
                settings=settings,
                storage=storage,
            )
        ),
        state=SimpleNamespace(actor=ActorContext("owner", "owner", "api-token")),
        client=SimpleNamespace(host="127.0.0.1"),
    )
    backend = _backend_lease(settings)
    backend.acquire()
    monkeypatch.setattr(diagnostics.sqlite3, "connect", swapping_connect)
    try:
        with pytest.raises(HTTPException) as raised:
            _document_contour_observer_snapshot_sync(request)
    finally:
        backend.release()
    assert swapped is True
    assert raised.value.status_code == 503
    assert raised.value.detail == "Снимок барьера релиза недоступен"


def test_queue_aba_restore_is_detected_even_though_the_original_inode_returns(
    settings,
    storage,
    monkeypatch,
):
    import friday.diagnostics as diagnostics

    queue = _stopped_queue(settings, pending_update_id=51)
    replacement_settings = SimpleNamespace(state_dir=settings.state_dir / "replacement")
    replacement = _stopped_queue(replacement_settings)
    saved = queue.with_name("saved-one-pending.sqlite3")
    original_connect = diagnostics.sqlite3.connect
    swapped = False

    def aba_connect(database, *args, **kwargs):
        nonlocal swapped
        if not swapped and "/proc/self/fd/" in str(database):
            os.replace(queue, saved)
            os.replace(replacement, queue)
            connection = original_connect(database, *args, **kwargs)
            os.replace(queue, replacement)
            os.replace(saved, queue)
            swapped = True
            return connection
        return original_connect(database, *args, **kwargs)

    backend = _backend_lease(settings)
    backend.acquire()
    monkeypatch.setattr(diagnostics.sqlite3, "connect", aba_connect)
    try:
        with pytest.raises(RuntimeError, match="directory changed"):
            diagnostics.collect_document_contour_observer_snapshot(settings, storage)
    finally:
        backend.release()
    assert swapped is True


def test_hardlinked_or_uncheckpointed_queue_cannot_report_zero(settings, storage):
    import friday.diagnostics as diagnostics

    queue = _stopped_queue(settings)
    hardlink = queue.with_name("queue-hardlink.sqlite3")
    os.link(queue, hardlink)
    backend = _backend_lease(settings)
    backend.acquire()
    try:
        with pytest.raises(RuntimeError, match="not private"):
            diagnostics.collect_document_contour_observer_snapshot(settings, storage)
    finally:
        backend.release()

    hardlink.unlink()
    queue.with_name(f"{queue.name}-wal").write_bytes(b"uncheckpointed")
    backend = _backend_lease(settings)
    backend.acquire()
    try:
        with pytest.raises(RuntimeError, match="not a checkpointed"):
            diagnostics.collect_document_contour_observer_snapshot(settings, storage)
    finally:
        backend.release()


def test_bridge_lock_replacement_during_read_is_closed(settings, storage, monkeypatch):
    import friday.diagnostics as diagnostics

    _stopped_queue(settings)
    lock = settings.state_dir / "telegram-inbox.sqlite3.lock"
    saved = lock.with_name("saved-bridge.lock")
    original_connect = diagnostics.sqlite3.connect
    replaced = False

    def replacing_connect(database, *args, **kwargs):
        nonlocal replaced
        if not replaced and "/proc/self/fd/" in str(database):
            os.replace(lock, saved)
            lock.write_text("replacement", encoding="utf-8")
            lock.chmod(0o600)
            replaced = True
        return original_connect(database, *args, **kwargs)

    backend = _backend_lease(settings)
    backend.acquire()
    monkeypatch.setattr(diagnostics.sqlite3, "connect", replacing_connect)
    try:
        with pytest.raises(RuntimeError, match="directory changed|lease file changed"):
            diagnostics.collect_document_contour_observer_snapshot(settings, storage)
    finally:
        backend.release()
    assert replaced is True


def test_outbound_mutation_during_snapshot_cannot_publish_stale_zero(settings, storage, monkeypatch):
    import friday.diagnostics as diagnostics

    _stopped_queue(settings)
    storage.ensure_user("owner", preset_key="owner")
    original_counts = diagnostics._bridge_queue_counts_only  # noqa: SLF001

    def mutate_after_queue(queue):
        result = original_counts(queue)
        assert storage.enqueue_notification(
            "owner",
            "5001",
            "late observer notification",
            kind="system",
            dedup_key="observer:late",
        )
        return result

    backend = _backend_lease(settings)
    backend.acquire()
    monkeypatch.setattr(diagnostics, "_bridge_queue_counts_only", mutate_after_queue)
    try:
        with pytest.raises(RuntimeError, match="physical outbound queue changed"):
            diagnostics.collect_document_contour_observer_snapshot(settings, storage)
    finally:
        backend.release()


def test_absent_or_unknown_queue_state_cannot_be_reported_as_zero(settings, storage):
    import sqlite3

    import friday.diagnostics as diagnostics

    backend = _backend_lease(settings)
    backend.acquire()
    try:
        with pytest.raises(FileNotFoundError):
            diagnostics.collect_document_contour_observer_snapshot(settings, storage)
    finally:
        backend.release()

    queue = _stopped_queue(settings, pending_update_id=61)
    connection = sqlite3.connect(queue)
    try:
        connection.execute("UPDATE updates SET status='processing' WHERE update_id=61")
        connection.commit()
    finally:
        connection.close()
    backend = _backend_lease(settings)
    backend.acquire()
    try:
        with pytest.raises(RuntimeError, match="unknown state"):
            diagnostics.collect_document_contour_observer_snapshot(settings, storage)
    finally:
        backend.release()


def test_guarded_queue_snapshot_keeps_the_exact_external_lease_held(settings):
    from friday.diagnostics import collect_document_contour_guarded_bridge_queue_snapshot
    from friday.diagnostics.runtime_lease import ProcessLease, process_owns_lease
    from friday.telegram_bridge import _UpdateInbox

    queue_path = settings.state_dir / "telegram-inbox.sqlite3"
    inbox = _UpdateInbox(str(queue_path))
    try:
        inbox.store({"update_id": 71, "message": {"text": "private pending body"}})
        inbox.store({"update_id": 72, "message": {"text": "private dead body"}})
        inbox.mark_dead_letter(72, "private diagnostic")
    finally:
        inbox.close()

    lease_path = queue_path.with_name(f"{queue_path.name}.lock")
    boundary = ProcessLease(lease_path, protocol="friday.telegram-bridge.v1")
    boundary.acquire()
    try:
        snapshot = collect_document_contour_guarded_bridge_queue_snapshot(settings, boundary)
        assert snapshot == {
            "schema": "friday.document-contour-guarded-bridge-queue.v1",
            "bridge_guard_held": True,
            "bridge_queue_state": "present",
            "inbound_pending": 1,
            "dead_letter": 1,
        }
        assert boundary.acquired is True
        assert process_owns_lease(lease_path, protocol="friday.telegram-bridge.v1") is True
        assert "private" not in repr(snapshot)
    finally:
        boundary.release()


def test_guarded_queue_snapshot_rejects_the_wrong_or_unheld_boundary(settings):
    from friday.diagnostics import collect_document_contour_guarded_bridge_queue_snapshot
    from friday.diagnostics.runtime_lease import ProcessLease

    _stopped_queue(settings)
    lease_path = settings.state_dir / "telegram-inbox.sqlite3.lock"
    unheld = ProcessLease(lease_path, protocol="friday.telegram-bridge.v1")
    with pytest.raises(RuntimeError, match="not held"):
        collect_document_contour_guarded_bridge_queue_snapshot(settings, unheld)

    wrong_path = ProcessLease(settings.state_dir / "other.lock", protocol="friday.telegram-bridge.v1")
    wrong_path.acquire()
    try:
        with pytest.raises(RuntimeError, match="identity is invalid"):
            collect_document_contour_guarded_bridge_queue_snapshot(settings, wrong_path)
        assert wrong_path.acquired is True
    finally:
        wrong_path.release()

    wrong_protocol = ProcessLease(lease_path, protocol="friday.telegram-bridge.v2")
    wrong_protocol.acquire()
    try:
        with pytest.raises(RuntimeError, match="identity is invalid"):
            collect_document_contour_guarded_bridge_queue_snapshot(settings, wrong_protocol)
        assert wrong_protocol.acquired is True
    finally:
        wrong_protocol.release()


def test_guarded_queue_snapshot_failure_never_releases_the_external_lease(settings):
    from friday.diagnostics import collect_document_contour_guarded_bridge_queue_snapshot
    from friday.diagnostics.runtime_lease import ProcessLease, process_owns_lease

    queue_path = settings.state_dir / "telegram-inbox.sqlite3"
    lease_path = queue_path.with_name(f"{queue_path.name}.lock")
    boundary = ProcessLease(lease_path, protocol="friday.telegram-bridge.v1")
    boundary.acquire()
    try:
        with pytest.raises(FileNotFoundError):
            collect_document_contour_guarded_bridge_queue_snapshot(settings, boundary)
        assert boundary.acquired is True
        assert process_owns_lease(lease_path, protocol="friday.telegram-bridge.v1") is True
    finally:
        boundary.release()
