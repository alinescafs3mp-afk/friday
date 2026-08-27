"""Focused, non-systemd regression tests for command-kernel P0 boundaries."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from friday.organs.engineer.command import boundary as boundary_module
from friday.organs.engineer.command import spawn_helper as spawn_helper_module
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
    confirm_held,
    confirm_path_roots,
    require_destructive_grant,
    resolve_bwrap,
    resolve_held,
)
from friday.organs.engineer.command.runner import (
    SpawnedCommand,
    _output_usage,
    _SpawnedProcess,
    _terminate_process,
)
from friday.organs.engineer.command.spawn_helper import (
    _ACTION_SOURCE_FD_MIN,
    _HELD_LAUNCHER_PATH,
    BWRAP_BLOCK_FD,
    BWRAP_LAUNCHER_FD,
    HELPER_LAUNCHER,
    _bwrap_file_actions,
    _job_main,
    _make_collision_free_block_pipe,
    _move_cgroup,
    _pidfd_identity,
    _recv_fds_message,
    _recv_socket_line,
    _release_stopped_child,
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


def test_store_root_has_a_process_lifetime_single_kernel_lease(tmp_path: Path) -> None:
    root = tmp_path / "store"
    first = CommandJobStore(root)
    try:
        with pytest.raises(CommandError, match="command_kernel_already_active"):
            CommandJobStore(root)
    finally:
        first.close()
    reopened = CommandJobStore(root)
    reopened.close()


def test_store_lists_cleanup_pending_unknown_jobs_for_restart(tmp_path: Path) -> None:
    store = CommandJobStore(tmp_path / "store")
    request = _request("/usr/bin/true")
    job_id = "9" * 32
    try:
        with store.transaction():
            store.insert_job(
                {
                    "job_id": job_id,
                    "actor_id": "owner",
                    "tenant_id": "tenant",
                    "conversation_id": "conversation",
                    "channel": "cli_test",
                    "source_row_id": "row",
                    "source_hash": "b" * 64,
                    "telegram_update_id": "update",
                    "isolation_profile": IsolationProfile.ISOLATED_WORKSPACE.value,
                    "host_user_authorized": False,
                    "idempotency_key": request.idempotency_key,
                    "command_digest": request.digest,
                    "argv_sha256": request.argv_sha256,
                    "lane": request.lane.value,
                    "origin": request.origin.value,
                    "status": CommandStatus.UNKNOWN.value,
                    "grant_nonce": "grant",
                    "timeout_sec": 30,
                    "max_stdout_bytes": 1024,
                    "max_stderr_bytes": 1024,
                    "created_at": time.time(),
                    "executable_json": "{}",
                }
            )
            store.update_job(
                job_id,
                {
                    "cleanup_pending": 1,
                    "systemd_unit": f"friday-ecmd-{job_id}.service",
                    "cgroup_path": f"/sys/fs/cgroup/friday-ecmd-{job_id}.service",
                },
            )
        assert [row["job_id"] for row in store.list_unreaped()] == [job_id]
        with store.transaction():
            store.update_job(job_id, {"cleanup_pending": 0})
        assert store.list_unreaped() == []
    finally:
        store.close()


@pytest.mark.parametrize(
    "legacy_status",
    [CommandStatus.UNKNOWN, CommandStatus.FAILED, CommandStatus.COMPLETED],
)
def test_store_migration_backfills_every_legacy_scope_bearing_row(
    tmp_path: Path,
    legacy_status: CommandStatus,
) -> None:
    root = tmp_path / "legacy-store"
    root.mkdir()
    database = root / "kernel.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """CREATE TABLE jobs (
                   job_id TEXT PRIMARY KEY,
                   status TEXT NOT NULL,
                   systemd_unit TEXT,
                   cgroup_path TEXT
               )"""
        )
        job_id = "7" * 32
        connection.execute(
            "INSERT INTO jobs(job_id,status,systemd_unit,cgroup_path) VALUES(?,?,?,?)",
            (
                job_id,
                legacy_status.value,
                f"friday-ecmd-{job_id}.service",
                f"/sys/fs/cgroup/friday-ecmd-{job_id}.service",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    store = CommandJobStore(root)
    try:
        pending = store.list_unreaped()
        assert len(pending) == 1
        assert pending[0]["job_id"] == job_id
        assert pending[0]["cleanup_pending"] == 1
    finally:
        store.close()


@pytest.mark.parametrize(
    ("argv", "path"),
    [
        (("/usr/bin/bash", "-c", "true"), "/usr/bin/bash"),
        (("/usr/bin/env", "bash", "-c", "true"), "/usr/bin/env"),
        (("/usr/bin/python3", "-c", "print(1)"), "/usr/bin/python3"),
        (("/usr/bin/xargs", "echo"), "/usr/bin/xargs"),
        (("/usr/bin/find", ".", "-exec", "sh", "{}", ";"), "/usr/bin/find"),
        (("/usr/bin/ld-linux-x86-64.so.2", "/usr/bin/true"), "/usr/bin/ld-linux-x86-64.so.2"),
        (("/usr/bin/setpriv", "/usr/bin/true"), "/usr/bin/setpriv"),
        (("/usr/bin/flock", "/tmp/lock", "/usr/bin/true"), "/usr/bin/flock"),
        (("/usr/bin/tar", "-cf", "/tmp/a.tar", "."), "/usr/bin/tar"),
    ],
)
def test_every_argv_requires_distinct_confirmation(argv: tuple[str, ...], path: str) -> None:
    request = _request(*argv)
    with pytest.raises(CommandError, match="destructive_confirmation_required"):
        require_destructive_grant(request, _grant(request), _resolved(path))
    require_destructive_grant(request, _grant(request, confirmed=True), _resolved(path))


def test_plain_env_and_non_dispatching_find_still_require_confirmation() -> None:
    env = _request("/usr/bin/env")
    with pytest.raises(CommandError, match="destructive_confirmation_required"):
        require_destructive_grant(env, _grant(env), _resolved("/usr/bin/env"))
    require_destructive_grant(env, _grant(env, confirmed=True), _resolved("/usr/bin/env"))
    find = _request("/usr/bin/find", ".", "-maxdepth", "0")
    with pytest.raises(CommandError, match="destructive_confirmation_required"):
        require_destructive_grant(find, _grant(find), _resolved("/usr/bin/find"))
    require_destructive_grant(find, _grant(find, confirmed=True), _resolved("/usr/bin/find"))


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


def test_low_block_pipe_fd_is_duplicated_before_posix_spawn_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    inheritable: list[tuple[int, bool]] = []

    def _fcntl(fd: int, operation: int, minimum: int) -> int:
        assert fd == 3
        assert operation == spawn_helper_module.fcntl.F_DUPFD_CLOEXEC
        assert minimum == _ACTION_SOURCE_FD_MIN
        return minimum

    monkeypatch.setattr(os, "pipe", lambda: (3, 4))
    monkeypatch.setattr(spawn_helper_module.fcntl, "fcntl", _fcntl)
    monkeypatch.setattr(os, "set_inheritable", lambda fd, value: inheritable.append((fd, value)))
    monkeypatch.setattr(os, "close", closed.append)

    block_r, block_w = _make_collision_free_block_pipe()
    actions = _bwrap_file_actions(block_r=block_r, has_script=False, path_root_count=16)
    gate_index = next(
        index
        for index, action in enumerate(actions)
        if action[0] == os.POSIX_SPAWN_DUP2 and action[2] == BWRAP_BLOCK_FD
    )
    gate_action = actions[gate_index]
    overwritten_before_gate = {
        action[2] if action[0] == os.POSIX_SPAWN_DUP2 else action[1] for action in actions[:gate_index]
    }

    assert (block_r, block_w) == (_ACTION_SOURCE_FD_MIN, 4)
    assert gate_action == (os.POSIX_SPAWN_DUP2, _ACTION_SOURCE_FD_MIN, BWRAP_BLOCK_FD)
    assert block_r not in overwritten_before_gate
    assert inheritable == [(_ACTION_SOURCE_FD_MIN, True)]
    assert closed == [3]


def test_job_helper_executes_transferred_held_bwrap_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    request = {
        "argv": ["bwrap", "--version"],
        "cgroup": "/sys/fs/cgroup/mock",
        "env": {"PATH": "/usr/bin"},
        "fsize": 1024,
        "has_script": False,
        "path_root_count": 0,
    }

    def _posix_spawn(
        path: str,
        argv: list[str],
        env: dict[str, str],
        *,
        file_actions: list[tuple],
    ) -> int:
        captured.update(path=path, argv=argv, env=env, actions=file_actions)
        raise RuntimeError("stop after action inspection")

    monkeypatch.setattr(sys, "argv", ["spawn_helper.py", "--job", json.dumps(request)])
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(spawn_helper_module, "_make_collision_free_block_pipe", lambda: (96, 97))
    monkeypatch.setattr(spawn_helper_module, "_move_cgroup", lambda *_args: None)
    monkeypatch.setattr(os, "posix_spawn", _posix_spawn)
    monkeypatch.setattr(os, "close", lambda _fd: None)
    monkeypatch.setattr(spawn_helper_module, "_send_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(spawn_helper_module, "_send_json_fd", lambda *_args, **_kwargs: None)

    with pytest.raises(SystemExit):
        _job_main()

    actions = captured["actions"]
    assert isinstance(actions, list)
    assert captured["path"] == _HELD_LAUNCHER_PATH
    assert f"/proc/self/fd/{BWRAP_LAUNCHER_FD}" == _HELD_LAUNCHER_PATH
    assert (os.POSIX_SPAWN_DUP2, HELPER_LAUNCHER, BWRAP_LAUNCHER_FD) in actions


def test_bwrap_launcher_holds_and_reconfirms_root_owned_original_inode() -> None:
    held = resolve_bwrap()
    try:
        observed = os.fstat(held.executable_fd)
        assert held.executable_sealed is False
        assert (int(observed.st_dev), int(observed.st_ino)) == (
            held.resolved.device,
            held.resolved.inode,
        )
        confirm_held(held)
    finally:
        held.close()


def test_cgroup_move_rejects_transient_match_until_membership_is_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = "/user.slice/friday-ecmd-" + "a" * 32 + ".service"
    readings = iter(
        ["/user.slice/ptyxis.scope"] * 5
        + [target, "/user.slice/ptyxis.scope"]
        + ["/user.slice/ptyxis.scope"] * 4
        + [target] * 5
    )
    writes: list[bytes] = []

    monkeypatch.setattr(spawn_helper_module, "_read_child_cgroup", lambda _pid: next(readings))
    monkeypatch.setattr(os, "open", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(os, "write", lambda _fd, payload: writes.append(payload) or len(payload))
    monkeypatch.setattr(os, "close", lambda _fd: None)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    _move_cgroup("/sys/fs/cgroup" + target, 4321)

    assert writes == [b"4321\n", b"4321\n"]


def test_job_helper_enters_scope_before_spawn_and_releases_stopped_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, int | str]] = []
    request = {
        "argv": ["bwrap", "--version"],
        "cgroup": "/sys/fs/cgroup/mock",
        "env": {"PATH": "/usr/bin"},
        "fsize": 1024,
        "has_script": False,
        "path_root_count": 0,
    }
    waits = iter(((0, 0), (4321, 0)))

    def _move(_cgroup: str, pid: int) -> None:
        events.append(("move", pid))

    def _pidfd_signal(_pidfd: int, sig: int, *_args: object) -> None:
        events.append(("signal", sig))

    def _write(fd: int, payload: bytes) -> int:
        if fd == 97:
            events.append(("gate", payload.decode("ascii")))
        return len(payload)

    monkeypatch.setattr(sys, "argv", ["spawn_helper.py", "--job", json.dumps(request)])
    monkeypatch.setattr(signal, "signal", lambda *_args: None)
    monkeypatch.setattr(signal, "pidfd_send_signal", _pidfd_signal)
    monkeypatch.setattr(spawn_helper_module, "_make_collision_free_block_pipe", lambda: (96, 97))
    monkeypatch.setattr(spawn_helper_module, "_move_cgroup", _move)
    monkeypatch.setattr(
        spawn_helper_module, "_wait_child_stopped", lambda pid: events.append(("stopped", pid))
    )
    monkeypatch.setattr(os, "posix_spawn", lambda *_args, **_kwargs: events.append(("spawn", 4321)) or 4321)
    monkeypatch.setattr(os, "pidfd_open", lambda *_args: 88)
    monkeypatch.setattr(os, "waitpid", lambda *_args: next(waits))
    monkeypatch.setattr(os, "getpid", lambda: 1234)
    monkeypatch.setattr(os, "read", lambda *_args: b"1")
    monkeypatch.setattr(os, "write", _write)
    monkeypatch.setattr(os, "close", lambda _fd: None)
    monkeypatch.setattr(spawn_helper_module.resource, "prlimit", lambda *_args: None)
    monkeypatch.setattr(spawn_helper_module, "_send_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(spawn_helper_module, "_send_json_fd", lambda *_args, **_kwargs: None)

    _job_main()

    assert events == [
        ("move", 1234),
        ("spawn", 4321),
        ("signal", signal.SIGSTOP),
        ("stopped", 4321),
        ("move", 4321),
        ("signal", signal.SIGCONT),
        ("gate", "x"),
    ]


def test_failed_pidfd_resume_never_releases_bwrap_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[int] = []
    writes: list[tuple[int, bytes]] = []

    def _fail_resume(_pidfd: int, sig: int, *_args: object) -> None:
        assert sig == signal.SIGCONT
        raise OSError("stale pidfd")

    monkeypatch.setattr(signal, "pidfd_send_signal", _fail_resume)
    monkeypatch.setattr(spawn_helper_module, "_kill_pidfd", killed.append)
    monkeypatch.setattr(os, "write", lambda fd, payload: writes.append((fd, payload)) or len(payload))

    with pytest.raises(OSError, match="stale pidfd"):
        _release_stopped_child(88, 97)

    assert killed == [88]
    assert writes == []


def test_exit_frame_before_pipe_eof_still_drains_captured_output(tmp_path: Path) -> None:
    workspace = JobWorkspace(tmp_path / "job")
    workspace.materialize()
    stdout_r, stdout_w = os.pipe()
    stderr_r, stderr_w = os.pipe()
    ctrl_r, ctrl_w = os.pipe()
    os.write(stdout_w, b"late-output")
    os.close(stdout_w)
    os.close(stderr_w)
    os.write(ctrl_w, b'{"returncode":0}\n')
    os.close(ctrl_w)

    class _Scope:
        @staticmethod
        def kill() -> bool:
            return True

    try:
        proc = _SpawnedProcess(123, None, ctrl_r)
        spawned = SpawnedCommand(
            workspace=workspace,
            timeout_sec=1,
            max_stdout_bytes=1024,
            max_stderr_bytes=1024,
            isolation=IsolationProfile.ISOLATED_WORKSPACE,
            limits=ResourceLimits.default(),
            process=proc,
            scope=_Scope(),  # type: ignore[arg-type]
        )
        spawned._stdout_r = stdout_r
        spawned._stdout_w = -1
        spawned._stderr_r = stderr_r
        spawned._stderr_w = -1
        spawned.started_at = time.time()
        spawned.wait()
        assert spawned.stdout == b"late-output"
        assert workspace.stdout_path.read_bytes() == b"late-output"
        assert spawned.eof_proven is True
        assert spawned.exit_code == 0
    finally:
        if "proc" in locals():
            proc.close_ctrl()
        for fd in (stdout_r, stderr_r, ctrl_r):
            with suppress(OSError):
                os.close(fd)


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


def test_close_pidfd_invalidates_process_alias_before_fd_can_be_reused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    proc = _SpawnedProcess(123, 77, -1)
    proc.returncode = 0
    workspace = JobWorkspace(tmp_path / "job")
    spawned = SpawnedCommand(
        workspace=workspace,
        timeout_sec=1,
        max_stdout_bytes=1,
        max_stderr_bytes=1,
        isolation=IsolationProfile.ISOLATED_WORKSPACE,
        limits=ResourceLimits.default(),
        process=proc,
        pidfd=77,
    )
    monkeypatch.setattr(os, "close", closed.append)
    monkeypatch.setattr(
        signal,
        "pidfd_send_signal",
        lambda *_args: pytest.fail("closed pidfd alias was signaled"),
    )

    spawned.close_pidfd()
    _terminate_process(proc, None)

    assert closed == [77]
    assert spawned.pidfd is None
    assert proc.pidfd is None


def test_output_and_control_fds_are_invalidated_before_first_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    proc = _SpawnedProcess(123, None, 43)
    spawned = SpawnedCommand(
        workspace=JobWorkspace(tmp_path / "job"),
        timeout_sec=1,
        max_stdout_bytes=1,
        max_stderr_bytes=1,
        isolation=IsolationProfile.ISOLATED_WORKSPACE,
        limits=ResourceLimits.default(),
        process=proc,
    )
    spawned._stdout_r = 41
    spawned._stderr_r = 42
    monkeypatch.setattr(os, "close", closed.append)

    spawned._close_output_fd("stdout", 41)
    spawned._close_output_fd("stdout", 41)
    proc.close_ctrl()
    proc.close_ctrl()
    spawned._close_pipes()

    assert closed == [41, 43, 42]
    assert spawned._stdout_r == -1
    assert spawned._stderr_r == -1
    assert proc.ctrl_fd == -1


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


def test_scope_identity_commits_before_spawn_helper_can_release_go(tmp_path: Path) -> None:
    events: list[str] = []
    job_id = "a" * 32
    scope = ProvenScope(
        job_id=job_id,
        unit=f"friday-ecmd-{job_id}.service",
        cgroup=tmp_path / f"friday-ecmd-{job_id}.service",
        limits=ResourceLimits.default(),
    )

    class _Store:
        @staticmethod
        @contextmanager
        def transaction():
            events.append("transaction")
            yield
            events.append("commit")

        @staticmethod
        def update_job(_job_id: str, fields: dict[str, object]) -> None:
            assert fields == {
                "cgroup_path": str(scope.cgroup),
                "systemd_unit": scope.unit,
                "cleanup_pending": 1,
            }
            events.append("scope-row")

    class _Workspace:
        @staticmethod
        def env(*, path_value: str, isolated: bool) -> dict[str, str]:
            assert path_value == "/usr/bin"
            assert isolated is True
            events.append("env")
            return {"PATH": path_value}

    class _Spawned:
        scope = None

        @staticmethod
        def abort() -> None:
            events.append("abort")

        def spawn(self, *_args: object, **kwargs: object) -> None:
            assert self.scope is scope
            assert kwargs["scope"] is scope
            assert events.index("commit") < len(events)
            events.append("go")

    kernel = CommandKernel.__new__(CommandKernel)
    kernel_any: Any = kernel
    kernel_any.store = _Store()
    kernel_any.trusted_path = SimpleNamespace(runtime_path="/usr/bin")
    kernel_any.path_roots = ()
    kernel_any._broker = object()
    spawned: Any = _Spawned()
    kernel._spawn_in_durable_scope(
        job_id,
        _request("/usr/bin/true"),
        _Workspace(),  # type: ignore[arg-type]
        spawned,
        object(),
        object(),
        scope,
    )

    assert events == ["transaction", "scope-row", "commit", "env", "go"]


def test_scope_commit_failure_aborts_before_spawn_helper_go(tmp_path: Path) -> None:
    events: list[str] = []
    job_id = "c" * 32
    scope = ProvenScope(
        job_id=job_id,
        unit=f"friday-ecmd-{job_id}.service",
        cgroup=tmp_path / f"friday-ecmd-{job_id}.service",
        limits=ResourceLimits.default(),
    )

    class _Store:
        @staticmethod
        @contextmanager
        def transaction():
            yield
            raise CommandError("durable_write_failed")

        @staticmethod
        def update_job(_job_id: str, _fields: dict[str, object]) -> None:
            events.append("scope-row")

    class _Spawned:
        scope = None

        @staticmethod
        def abort() -> None:
            events.append("abort")

        @staticmethod
        def spawn(*_args: object, **_kwargs: object) -> None:
            events.append("go")

    kernel = CommandKernel.__new__(CommandKernel)
    kernel_any: Any = kernel
    kernel_any.store = _Store()
    kernel_any.trusted_path = SimpleNamespace(runtime_path="/usr/bin")
    kernel_any.path_roots = ()
    kernel_any._broker = object()
    spawned: Any = _Spawned()

    with pytest.raises(CommandError, match="durable_write_failed"):
        kernel._spawn_in_durable_scope(
            job_id,
            _request("/usr/bin/true"),
            SimpleNamespace(env=lambda **_kwargs: {}),  # type: ignore[arg-type]
            spawned,
            object(),
            object(),
            scope,
        )

    assert events == ["scope-row", "abort"]
    assert spawned.scope is scope


def test_restart_reconciles_admitted_job_with_persisted_scope(tmp_path: Path) -> None:
    events: list[str] = []
    job_id = "b" * 32
    scope = ProvenScope(
        job_id=job_id,
        unit=f"friday-ecmd-{job_id}.service",
        cgroup=tmp_path / f"friday-ecmd-{job_id}.service",
        limits=ResourceLimits.default(),
    )

    class _Store:
        @staticmethod
        def list_unreaped() -> list[dict[str, object]]:
            return [
                {
                    "job_id": job_id,
                    "status": CommandStatus.ADMITTED.value,
                    "systemd_unit": scope.unit,
                    "cgroup_path": str(scope.cgroup),
                    "timeout_sec": 30,
                }
            ]

        @staticmethod
        @contextmanager
        def transaction():
            yield

        @staticmethod
        def update_job(_job_id: str, fields: dict[str, object]) -> None:
            assert fields["status"] == CommandStatus.UNKNOWN.value
            events.append("unknown")

    class _Boundary:
        @staticmethod
        def recover_scope(*_args: object, **_kwargs: object) -> ProvenScope:
            events.append("recover")
            return scope

        @staticmethod
        def stop(recovered: ProvenScope) -> None:
            assert recovered is scope
            events.append("stop")

    kernel = CommandKernel.__new__(CommandKernel)
    kernel.store = _Store()  # type: ignore[assignment]
    kernel.boundary = _Boundary()  # type: ignore[assignment]
    kernel.limits = ResourceLimits.default()
    kernel._reconcile_stale()

    assert events == ["recover", "stop", "unknown"]


@pytest.mark.parametrize(("cleanup_proven", "pending"), [(False, 1), (True, 0)])
def test_restart_keeps_cleanup_marker_until_scope_collection_is_proven(
    tmp_path: Path,
    cleanup_proven: bool,
    pending: int,
) -> None:
    job_id = "e" * 32
    scope = ProvenScope(
        job_id=job_id,
        unit=f"friday-ecmd-{job_id}.service",
        cgroup=tmp_path / f"friday-ecmd-{job_id}.service",
        limits=ResourceLimits.default(),
    )
    updates: list[dict[str, object]] = []

    class _Store:
        @staticmethod
        def list_unreaped() -> list[dict[str, object]]:
            return [
                {
                    "job_id": job_id,
                    "status": CommandStatus.UNKNOWN.value,
                    "systemd_unit": scope.unit,
                    "cgroup_path": str(scope.cgroup),
                    "timeout_sec": 30,
                    "cleanup_pending": 1,
                }
            ]

        @staticmethod
        @contextmanager
        def transaction():
            yield

        @staticmethod
        def update_job(_job_id: str, fields: dict[str, object]) -> None:
            updates.append(dict(fields))

    class _Boundary:
        @staticmethod
        def recover_scope(*_args: object, **_kwargs: object) -> ProvenScope:
            return scope

        @staticmethod
        def stop(_scope: ProvenScope) -> bool:
            return cleanup_proven

    kernel = CommandKernel.__new__(CommandKernel)
    kernel.store = _Store()  # type: ignore[assignment]
    kernel.boundary = _Boundary()  # type: ignore[assignment]
    kernel.limits = ResourceLimits.default()
    kernel._reconcile_stale()

    assert updates[-1]["cleanup_pending"] == pending


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


def test_scope_retains_attested_cgroup_fd_until_cleanup_retry_is_proven(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cgroup = tmp_path / f"friday-ecmd-{'f' * 32}.service"
    cgroup.mkdir()
    (cgroup / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    (cgroup / "cgroup.kill").write_text("", encoding="ascii")
    fd = os.open(cgroup, os.O_RDONLY | os.O_DIRECTORY)
    outcomes = iter((False, False, True))
    retained: list[ProvenScope] = []
    monkeypatch.setattr(boundary_module, "_stop_and_collect", lambda _unit: next(outcomes))
    monkeypatch.setattr(boundary_module._SCOPE_CLEANUP_OWNER, "retain", retained.append)
    monkeypatch.setattr(boundary_module._SCOPE_CLEANUP_OWNER, "discard", lambda _scope: None)
    scope = ProvenScope(
        job_id="f" * 32,
        unit=f"friday-ecmd-{'f' * 32}.service",
        cgroup=cgroup,
        limits=ResourceLimits.default(),
        cgroup_fd=fd,
    )

    try:
        assert scope.kill() is False
        assert scope.cgroup_fd == fd
        assert retained == [scope]
        os.fstat(fd)
        assert scope.kill() is True
        assert scope.cgroup_fd is None
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        if scope.cgroup_fd is not None:
            os.close(scope.cgroup_fd)
            scope.cgroup_fd = None


def test_restart_proves_already_collected_validated_scope_without_path_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "8" * 32
    unit = f"friday-ecmd-{job_id}.service"
    calls: list[str] = []
    monkeypatch.setattr(
        boundary_module,
        "_stop_and_collect",
        lambda candidate: calls.append(candidate) or True,
    )

    boundary = SystemdCgroupBoundary()
    scope = boundary.recover_scope(
        job_id,
        unit,
        f"/sys/fs/cgroup/user.slice/{unit}",
        ResourceLimits.default(),
        timeout_sec=30,
    )

    assert scope.cgroup_fd is None
    assert scope.tree_empty() is True
    assert boundary.stop(scope) is True
    assert calls == [unit, unit]


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


@pytest.mark.parametrize("directory", ["/", "/dev", "/proc", "/run", "/run/friday", "/sys", "/var"])
def test_trusted_path_rejects_sandbox_control_roots_and_ancestors(directory: str) -> None:
    with pytest.raises(CommandError, match="invalid_trusted_path"):
        TrustedPathContract(directories=(directory,))


def test_trusted_path_attestation_rejects_alias_to_sensitive_root(tmp_path: Path) -> None:
    alias = tmp_path / "run-alias"
    alias.symlink_to("/run")
    contract = TrustedPathContract(directories=(str(alias),))
    with pytest.raises(CommandError, match="untrusted_path_root"):
        attest_trusted_path(contract)


def test_trusted_path_attestation_rejects_sensitive_root_through_parent_symlink(
    tmp_path: Path,
) -> None:
    safe = tmp_path / "safe"
    safe.mkdir()
    (safe / "link").symlink_to("/")
    contract = TrustedPathContract(directories=(str(safe / "link" / "proc"),))
    with pytest.raises(CommandError, match="untrusted_path_root"):
        attest_trusted_path(contract)
