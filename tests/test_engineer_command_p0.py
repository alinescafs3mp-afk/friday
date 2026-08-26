"""Focused, non-systemd regression tests for command-kernel P0 boundaries."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from friday.organs.engineer.command import boundary as boundary_module
from friday.organs.engineer.command.boundary import ProvenScope, SystemdCgroupBoundary
from friday.organs.engineer.command.confirm import OwnerConfirmationAuthority
from friday.organs.engineer.command.contracts import (
    CommandError,
    CommandLane,
    CommandOrigin,
    CommandRequest,
    CommandStatus,
    HeldExecutable,
    IsolationProfile,
    ResolvedExecutable,
    ResourceLimits,
    TrustedPathContract,
    VerifiedCommandGrant,
    sha256_bytes,
)
from friday.organs.engineer.command.isolate import bwrap_argv, extra_ro_binds
from friday.organs.engineer.command.kernel import CommandKernel
from friday.organs.engineer.command.resolve import (
    attest_trusted_path,
    confirm_path_roots,
    require_destructive_grant,
    resolve_held,
)
from friday.organs.engineer.command.runner import _output_usage, _terminate_process
from friday.organs.engineer.command.spawn_helper import (
    _pidfd_identity,
    _recv_fds_message,
    _recv_socket_line,
    _send_fds_message,
)
from friday.organs.engineer.command.store import CommandJobStore
from friday.organs.engineer.command.workspace import JobWorkspace


def _resolved(path: str, *, owner_uid: int = 0) -> ResolvedExecutable:
    return ResolvedExecutable(
        requested=path,
        canonical_path=path,
        owner_uid=owner_uid,
        owner_gid=0,
        mode=0o100755,
        device=1,
        inode=2,
        size_bytes=1,
        mtime_ns=1,
        sha256="a" * 64,
    )


def _request(*argv: str) -> CommandRequest:
    return CommandRequest(
        lane=CommandLane.ARGV,
        origin=CommandOrigin.OWNER_TURN,
        argv=argv,
        idempotency_key="p0-request",
    )


def _grant(request: CommandRequest, *, confirmed: bool = False) -> VerifiedCommandGrant:
    return VerifiedCommandGrant(
        actor_id="owner",
        tenant_id="tenant",
        conversation_id="conversation",
        channel="cli_test",
        source_row_id="row",
        source_hash="b" * 64,
        telegram_update_id="update",
        isolation_profile=IsolationProfile.ISOLATED_WORKSPACE,
        idempotency_key=request.idempotency_key,
        command_digest=request.digest,
        argv_sha256=request.argv_sha256,
        lane=request.lane,
        origin=request.origin,
        destructive_confirmed=confirmed,
        confirmation_nonce="confirm" if confirmed else "",
        confirmation_expires_at=2**31 if confirmed else 0,
        expires_at=2**31,
        nonce="grant",
    )


def test_confirmation_ingress_row_and_update_are_each_one_shot_across_restart(tmp_path: Path) -> None:
    root = tmp_path / "store"
    store = CommandJobStore(root)
    authority = OwnerConfirmationAuthority(b"c" * 32, clock=lambda: 1_000)
    authority.bind_store(store)
    def _ingest(row: str, update: str) -> str:
        return authority.ingest(
            actor_id="owner",
            tenant_id="tenant",
            conversation_id="conversation",
            channel="cli_test",
            confirmation_row_id=row,
            confirmation_update_id=update,
            command_digest=sha256_bytes(b"command"),
            body_hash=sha256_bytes(b"confirmation"),
            expires_at=1_060,
        )

    _ingest("immutable-row", "immutable-update")
    with pytest.raises(CommandError, match="confirmation_replay"):
        _ingest("immutable-row", "different-update")
    store.close()

    restarted = CommandJobStore(root)
    authority.bind_store(restarted)
    try:
        with pytest.raises(CommandError, match="confirmation_replay"):
            _ingest("different-row", "immutable-update")
    finally:
        restarted.close()


@pytest.mark.parametrize(
    ("argv", "path"),
    [
        (("/usr/bin/bash", "-c", "true"), "/usr/bin/bash"),
        (("/usr/bin/env", "bash", "-c", "true"), "/usr/bin/env"),
        (("/usr/bin/python3", "-c", "print(1)"), "/usr/bin/python3"),
        (("/usr/bin/xargs", "echo"), "/usr/bin/xargs"),
        (("/usr/bin/find", ".", "-exec", "sh", "{}", ";"), "/usr/bin/find"),
    ],
)
def test_argv_dispatchers_require_distinct_confirmation(argv: tuple[str, ...], path: str) -> None:
    request = _request(*argv)
    with pytest.raises(CommandError, match="destructive_confirmation_required"):
        require_destructive_grant(request, _grant(request), _resolved(path))
    require_destructive_grant(request, _grant(request, confirmed=True), _resolved(path))


def test_plain_env_and_non_dispatching_find_remain_non_destructive() -> None:
    env = _request("/usr/bin/env")
    require_destructive_grant(env, _grant(env), _resolved("/usr/bin/env"))
    find = _request("/usr/bin/find", ".", "-maxdepth", "0")
    require_destructive_grant(find, _grant(find), _resolved("/usr/bin/find"))


def test_owner_writable_executable_requires_confirmation() -> None:
    request = _request("/private/tool")
    with pytest.raises(CommandError, match="destructive_confirmation_required"):
        require_destructive_grant(request, _grant(request), _resolved("/private/tool", owner_uid=12345))


def test_fifo_output_is_rejected_without_blocking_and_no_host_rw_bind_is_emitted(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    os.mkfifo(output / "trap")
    started = time.monotonic()
    with pytest.raises(CommandError, match="output_unreadable"):
        _output_usage(output)
    assert time.monotonic() - started < 1.0

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    workspace = JobWorkspace(job_dir)
    workspace.materialize()
    held = HeldExecutable(
        resolved=_resolved("/usr/bin/true"),
        executable_fd=-1,
        inner_rest=(),
    )
    argv = bwrap_argv(
        workspace=workspace,
        held=held,
        env=workspace.env(path_value="/usr/bin:/bin", isolated=True),
        extra_binds=(),
        limits=ResourceLimits.default(),
        sync_fd=5,
    )
    assert "--bind" not in argv
    assert str(workspace.output) not in argv
    assert "/run/friday/stdin" in argv


def test_stream_fd_frame_handles_short_send_and_split_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    pipe_r, pipe_w = os.pipe()
    original = socket.send_fds

    def _short(sock: socket.socket, buffers: list[bytes], fds: list[int]) -> int:
        frame = b"".join(buffers)
        return int(original(sock, [frame[:3]], fds))

    monkeypatch.setattr(socket, "send_fds", _short)
    received: list[int] = []
    try:
        _send_fds_message(sender, b'{"op":"safe"}', [pipe_r])
        line, received = _recv_fds_message(receiver)
        assert line == b'{"op":"safe"}'
        assert len(received) == 1
    finally:
        sender.close()
        receiver.close()
        os.close(pipe_r)
        os.close(pipe_w)
        for fd in received:
            os.close(fd)


def test_stream_ack_has_a_finite_deadline() -> None:
    sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with pytest.raises(TimeoutError):
            _recv_socket_line(receiver, timeout=0.01)
    finally:
        sender.close()
        receiver.close()


def test_termination_signals_pidfd_and_never_numeric_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []
    state = {"dead": False, "scope_kills": 0}

    class _Process:
        pidfd = 77

        @staticmethod
        def poll() -> int | None:
            return 0 if state["dead"] else None

    class _Scope:
        @staticmethod
        def kill() -> bool:
            state["scope_kills"] += 1
            return True

    def _pidfd_send(fd: int, sig: int) -> None:
        calls.append((fd, sig))
        state["dead"] = True

    monkeypatch.setattr(signal, "pidfd_send_signal", _pidfd_send)
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("numeric PID signal used"))
    _terminate_process(_Process(), _Scope())  # type: ignore[arg-type]
    assert calls == [(77, signal.SIGTERM)]
    assert state["scope_kills"] == 1


def test_pidfd_identity_is_transferred_without_reopening_a_numeric_pid() -> None:
    pidfd = os.pidfd_open(os.getpid(), 0)
    try:
        assert _pidfd_identity(pidfd) == os.getpid()
    finally:
        os.close(pidfd)


def test_unheld_missing_cgroup_is_not_an_empty_tree_proof(tmp_path: Path) -> None:
    scope = ProvenScope(
        job_id="e" * 32,
        unit=f"friday-ecmd-{'e' * 32}.service",
        cgroup=tmp_path / "missing",
        limits=ResourceLimits.default(),
    )
    assert scope.tree_empty() is False


def test_final_receipt_commit_failure_aborts_and_unblocks_waiters() -> None:
    request = _request("/usr/bin/true")
    resolved = _resolved("/usr/bin/true")

    class _Workspace:
        @staticmethod
        def admit_generated_files() -> tuple[()]:
            return ()

    class _Spawned:
        workspace = _Workspace()
        quota_exceeded = False
        quota_code = ""
        eof_proven = True
        tree_empty = True
        timed_out = False
        cancelled = False
        exit_code = 0
        signal_num = None
        truncated_stdout = False
        truncated_stderr = False
        started_at = 1.0
        finished_at = 2.0
        stdout = b""
        stderr = b""
        effect_boundary_crossed = True
        aborts = 0

        @staticmethod
        def wait() -> None:
            return None

        @classmethod
        def abort(cls) -> None:
            cls.aborts += 1

        @staticmethod
        def close_pidfd() -> None:
            return None

    class _Closable:
        def __init__(self) -> None:
            self.resolved = resolved

        @staticmethod
        def close() -> None:
            return None

    class _Store:
        commits = 0
        updates: list[dict[str, object]] = []

        @classmethod
        @contextmanager
        def transaction(cls):
            yield
            cls.commits += 1
            if cls.commits == 1:
                raise CommandError("durable_write_failed")

        @classmethod
        def update_job(cls, _job_id: str, fields: dict[str, object]) -> None:
            cls.updates.append(dict(fields))

    kernel = CommandKernel.__new__(CommandKernel)
    kernel_any: Any = kernel
    kernel_any.store = _Store()
    kernel_any.authority = SimpleNamespace(sign_receipt=lambda _payload: "receipt-mac")
    kernel._lock = threading.Lock()
    spawned: Any = _Spawned()
    kernel._live = {"job": spawned}
    kernel._threads = {"job": threading.current_thread()}
    kernel._receipts = {}
    grant = SimpleNamespace(
        isolation_profile=IsolationProfile.ISOLATED_WORKSPACE,
        source_hash="b" * 64,
    )
    kernel._reap("job", request, grant, _Closable(), _Closable(), spawned)
    receipt = kernel._receipts["job"]
    assert receipt.status is CommandStatus.UNKNOWN
    assert receipt.error_code == "final_receipt_persist_failed"
    assert _Spawned.aborts == 1
    assert kernel._live == {}
    assert kernel._threads == {}
    assert _Store.updates[-1]["status"] == CommandStatus.UNKNOWN.value


def test_restart_never_writes_or_stops_unvalidated_persisted_scope(tmp_path: Path) -> None:
    job_id = "a" * 32
    fake_cgroup = tmp_path / f"friday-ecmd-{job_id}.service"
    fake_cgroup.mkdir()
    kill_file = fake_cgroup / "cgroup.kill"
    kill_file.write_text("sentinel", encoding="ascii")

    class _Store:
        updates: list[dict[str, object]] = []

        @staticmethod
        def list_unreaped() -> list[dict[str, object]]:
            return [
                {
                    "job_id": job_id,
                    "systemd_unit": f"friday-ecmd-{job_id}.service",
                    "cgroup_path": str(fake_cgroup),
                    "timeout_sec": 30,
                }
            ]

        @staticmethod
        @contextmanager
        def transaction():
            yield

        @classmethod
        def update_job(cls, _job_id: str, fields: dict[str, object]) -> None:
            cls.updates.append(dict(fields))

    kernel = CommandKernel.__new__(CommandKernel)
    kernel.store = _Store()  # type: ignore[assignment]
    kernel.boundary = SystemdCgroupBoundary()
    kernel.limits = ResourceLimits.default()
    kernel._reconcile_stale()
    assert kill_file.read_text(encoding="ascii") == "sentinel"
    assert _Store.updates[-1]["status"] == CommandStatus.UNKNOWN.value


def test_transient_scope_requests_collect_and_cleanup_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def _run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(tuple(argv))
        if "show" in argv:
            return SimpleNamespace(returncode=1, stdout="LoadState=not-found\n")
        return SimpleNamespace(returncode=0, stdout="", stderr=b"")

    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    monkeypatch.setattr(boundary_module.subprocess, "run", _run)
    monkeypatch.setattr(boundary_module, "_wait_unit_cgroup", lambda _unit: cgroup)
    monkeypatch.setattr(boundary_module, "_prove_limits", lambda _cgroup, _limits: None)
    monkeypatch.setattr(
        boundary_module,
        "_prove_unit_contract",
        lambda _unit, *, runtime_sec: None,
    )
    job_id = "d" * 32
    scope = SystemdCgroupBoundary().allocate(job_id, ResourceLimits.default(), timeout_sec=5)
    launch = calls[0]
    assert launch[:4] == ("/usr/bin/systemd-run", "--user", "--no-block", "--collect")
    assert scope.unit == f"friday-ecmd-{job_id}.service"

    assert boundary_module._stop_and_collect(scope.unit) is True
    verbs = [call[2] for call in calls if len(call) > 2 and call[1] == "--user"]
    assert "stop" in verbs
    assert "reset-failed" in verbs
    assert "show" in verbs
    assert scope.cgroup_fd is not None
    os.close(scope.cgroup_fd)
    scope.cgroup_fd = None


def test_path_lookup_and_bind_stay_on_held_root_after_rename(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted-bin"
    trusted.mkdir()
    trusted.chmod(0o755)
    original = trusted / "tool"
    shutil.copyfile("/usr/bin/true", original)
    original.chmod(0o755)
    contract = TrustedPathContract(directories=(str(trusted),))
    roots = attest_trusted_path(contract)
    moved = tmp_path / "moved-bin"
    trusted.rename(moved)
    trusted.mkdir()
    replacement = trusted / "tool"
    shutil.copyfile("/usr/bin/false", replacement)
    replacement.chmod(0o755)
    held = None
    try:
        held = resolve_held("tool", trusted_path=contract, path_roots=roots)
        assert held.resolved.sha256 == sha256_bytes(Path("/usr/bin/true").read_bytes())
        confirm_path_roots(roots)
        binds = extra_ro_binds(roots)
        assert binds == ((7, str(trusted)),)
    finally:
        if held is not None:
            held.close()
        for root in roots:
            os.close(root.dir_fd)
