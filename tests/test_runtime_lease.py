from __future__ import annotations

import json

import pytest

from jericho.diagnostics.runtime_lease import ProcessLease, RuntimeLeaseError


def test_process_lease_is_exclusive_and_recoverable(tmp_path):
    path = tmp_path / "state" / "telegram.lock"
    first = ProcessLease(path, protocol="jericho.test.v1")
    second = ProcessLease(path, protocol="jericho.test.v1")
    first.acquire()
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        assert metadata["protocol"] == "jericho.test.v1"
        assert isinstance(metadata["pid"], int)
        with pytest.raises(RuntimeLeaseError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    assert second.acquired is True
    second.release()
    assert second.acquired is False


def test_process_lease_inspection_distinguishes_active_and_stale_metadata(tmp_path):
    from jericho.diagnostics.runtime_lease import inspect_process_lease

    path = tmp_path / "state" / "backend.lock"
    lease = ProcessLease(path, protocol="jericho.backend.v1")
    lease.acquire()
    try:
        active = inspect_process_lease(path, protocol="jericho.backend.v1")
        assert active["state"] in {"active", "active_hint"}
        assert active["active"] is not False
        assert active["protocol_matches"] is True
    finally:
        lease.release()

    stale = inspect_process_lease(path, protocol="jericho.backend.v1")
    assert stale["state"] == "stale_metadata"
    assert stale["active"] is False or stale["pid_alive"] is True


def test_process_lease_inspection_rejects_broken_symlink(tmp_path):
    from jericho.diagnostics.runtime_lease import inspect_process_lease

    path = tmp_path / "state" / "backend.lock"
    path.parent.mkdir(parents=True)
    path.symlink_to(tmp_path / "missing-target")

    result = inspect_process_lease(path, protocol="jericho.backend.v1")

    assert result["exists"] is True
    assert result["state"] == "unsafe_symlink"
    assert result["healthy"] is False
