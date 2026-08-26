"""Held-FD spawn. isolated_workspace uses bwrap; host_user is not isolated."""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import CommandError, HeldExecutable, IsolationProfile
from .isolate import (
    bwrap_argv,
    cgroup_pids,
    cgroup_populated,
    create_job_cgroup,
    extra_ro_binds,
    host_user_argv,
    move_pid,
    pass_fds_for,
    remove_cgroup,
)
from .resolve import confirm_held
from .workspace import JobWorkspace

_POLL_SEC = 0.05
_KILL_GRACE_SEC = 2.0
_EOF_GRACE_SEC = 2.0
_CGROUP_GRACE_SEC = 2.0


def _pid_starttime(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    fields = raw[close + 1 :].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


def _kill_session(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + _KILL_GRACE_SEC
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def _drain_cgroup(cgroup: Path | None) -> bool:
    if cgroup is None:
        return True
    deadline = time.monotonic() + _CGROUP_GRACE_SEC
    while time.monotonic() < deadline:
        populated = cgroup_populated(cgroup)
        if populated is False:
            return True
        for pid in cgroup_pids(cgroup):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except PermissionError:
                return False
        time.sleep(0.05)
    return cgroup_populated(cgroup) is False


@dataclass
class SpawnedCommand:
    workspace: JobWorkspace
    timeout_sec: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    isolation: IsolationProfile
    process: subprocess.Popen[bytes] | None = None
    started_at: float = 0.0
    finished_at: float | None = None
    exit_code: int | None = None
    signal_num: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    truncated_stdout: bool = False
    truncated_stderr: bool = False
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    output_activity: bool = False
    effect_boundary_crossed: bool = False
    eof_proven: bool = False
    tree_empty: bool = False
    pid: int | None = None
    pid_starttime: int | None = None
    pidfd: int | None = None
    cgroup: Path | None = None
    _cancel: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def spawn(
        self,
        held: HeldExecutable,
        *,
        stdin: bytes,
        env: dict[str, str],
        trusted_path,
        bwrap: HeldExecutable | None,
        job_id: str,
    ) -> None:
        confirm_held(held)
        os.lseek(held.executable_fd, 0, os.SEEK_SET)
        if held.script_fd is not None:
            os.lseek(held.script_fd, 0, os.SEEK_SET)
        if self.isolation is IsolationProfile.ISOLATED_WORKSPACE:
            if bwrap is None:
                raise CommandError("bubblewrap_unavailable")
            confirm_held(bwrap)
            extra = extra_ro_binds(trusted_path)
            argv = bwrap_argv(workspace=self.workspace, held=held, env=env, extra_binds=extra)
            launcher = f"/proc/self/fd/{bwrap.executable_fd}"
            inherited = pass_fds_for(held, bwrap_fd=bwrap.executable_fd)
            cwd = None
            child_env = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
        elif self.isolation is IsolationProfile.HOST_USER:
            argv = host_user_argv(held)
            launcher = f"/proc/self/fd/{held.executable_fd}"
            inherited = pass_fds_for(held, bwrap_fd=None)
            cwd = str(self.workspace.job_dir)
            child_env = env
        else:
            raise CommandError("invalid_isolation_profile")

        stdin_r, stdin_w = os.pipe()
        stdout_r, stdout_w = os.pipe()
        stderr_r, stderr_w = os.pipe()
        os.set_inheritable(stdin_r, True)
        os.set_inheritable(stdout_w, True)
        os.set_inheritable(stderr_w, True)
        self._stdin_r = stdin_r
        self._stdin_w = stdin_w
        self._stdout_r = stdout_r
        self._stdout_w = stdout_w
        self._stderr_r = stderr_r
        self._stderr_w = stderr_w
        self._stdin_payload = stdin
        self._stdin_offset = 0
        self.cgroup = create_job_cgroup(job_id)
        try:
            self.started_at = time.time()
            self.process = subprocess.Popen(  # noqa: S603 - held-FD launcher, closed argv
                argv,
                executable=launcher,
                cwd=cwd,
                env=child_env,
                stdin=stdin_r,
                stdout=stdout_w,
                stderr=stderr_w,
                shell=False,
                close_fds=True,
                pass_fds=inherited,
                start_new_session=True,
            )
            self.effect_boundary_crossed = True
            self.pid = int(self.process.pid)
            self.pid_starttime = _pid_starttime(self.pid)
            try:
                self.pidfd = os.pidfd_open(self.pid, 0)
            except OSError:
                self.pidfd = None
            if self.cgroup is not None:
                try:
                    move_pid(self.cgroup, self.pid)
                except CommandError:
                    self.cgroup = None
        except OSError as exc:
            self._close_pipes()
            remove_cgroup(self.cgroup)
            raise CommandError("spawn_failed") from exc
        os.close(stdin_r)
        os.close(stdout_w)
        os.close(stderr_w)
        self._stdin_r = -1
        self._stdout_w = -1
        self._stderr_w = -1

    def request_cancel(self) -> None:
        self._cancel.set()
        proc = self.process
        if proc is not None and proc.poll() is None and proc.pid:
            _kill_session(proc.pid)

    def wait(self) -> None:
        proc = self.process
        if proc is None:
            raise CommandError("not_started")
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        stdout_captured = 0
        stderr_captured = 0
        stdout_trunc = False
        stderr_trunc = False
        stdout_handle = os.fdopen(self.workspace.open_evidence("stdout.bin"), "wb")
        stderr_handle = os.fdopen(self.workspace.open_evidence("stderr.bin"), "wb")
        selector = selectors.DefaultSelector()
        os.set_blocking(self._stdout_r, False)
        os.set_blocking(self._stderr_r, False)
        os.set_blocking(self._stdin_w, False)
        selector.register(self._stdout_r, selectors.EVENT_READ, "stdout")
        selector.register(self._stderr_r, selectors.EVENT_READ, "stderr")
        if self._stdin_payload:
            selector.register(self._stdin_w, selectors.EVENT_WRITE, "stdin")
        else:
            os.close(self._stdin_w)
            self._stdin_w = -1
        deadline = time.monotonic() + self.timeout_sec
        timed_out = False
        cancelled = False
        reader_failed = False
        try:
            while selector.get_map() or proc.poll() is None:
                if not cancelled and self._cancel.is_set():
                    cancelled = True
                    _kill_session(proc.pid)
                elif not timed_out and time.monotonic() >= deadline:
                    timed_out = True
                    _kill_session(proc.pid)
                timeout = _POLL_SEC
                try:
                    events = selector.select(timeout)
                except InterruptedError:
                    continue
                for key, mask in events:
                    kind = key.data
                    try:
                        if kind == "stdin" and mask & selectors.EVENT_WRITE:
                            remaining = self._stdin_payload[self._stdin_offset :]
                            if not remaining:
                                selector.unregister(self._stdin_w)
                                os.close(self._stdin_w)
                                self._stdin_w = -1
                                continue
                            written = os.write(self._stdin_w, remaining)
                            self._stdin_offset += written
                            if self._stdin_offset >= len(self._stdin_payload):
                                selector.unregister(self._stdin_w)
                                os.close(self._stdin_w)
                                self._stdin_w = -1
                        elif kind in {"stdout", "stderr"} and mask & selectors.EVENT_READ:
                            chunk = os.read(key.fd, 65536)
                            if not chunk:
                                selector.unregister(key.fd)
                                os.close(key.fd)
                                if kind == "stdout":
                                    self._stdout_r = -1
                                else:
                                    self._stderr_r = -1
                                continue
                            self.output_activity = True
                            if kind == "stdout":
                                take, stdout_trunc = _cap(chunk, stdout_captured, self.max_stdout_bytes, stdout_trunc)
                                stdout_chunks.append(take)
                                stdout_captured += len(take)
                                stdout_handle.write(take)
                                stdout_handle.flush()
                            else:
                                take, stderr_trunc = _cap(chunk, stderr_captured, self.max_stderr_bytes, stderr_trunc)
                                stderr_chunks.append(take)
                                stderr_captured += len(take)
                                stderr_handle.write(take)
                                stderr_handle.flush()
                    except OSError:
                        reader_failed = True
                        with contextlib.suppress(Exception):
                            selector.unregister(key.fd)
                with self._lock:
                    self.stdout_bytes = stdout_captured
                    self.stderr_bytes = stderr_captured
                    self.truncated_stdout = stdout_trunc
                    self.truncated_stderr = stderr_trunc
            proc.wait(timeout=2)
            if self._cancel.is_set() and not timed_out:
                cancelled = True
            eof_deadline = time.monotonic() + _EOF_GRACE_SEC
            while selector.get_map() and time.monotonic() < eof_deadline:
                for key, _mask in selector.select(0.05):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except OSError:
                        reader_failed = True
                        chunk = b""
                    if not chunk:
                        selector.unregister(key.fd)
                        with contextlib.suppress(OSError):
                            os.close(key.fd)
            self.eof_proven = not selector.get_map() and not reader_failed
        finally:
            stdout_handle.close()
            stderr_handle.close()
            self._close_remaining(selector)
        if proc.poll() is None:
            _kill_session(proc.pid)
            proc.wait()
        drained = _drain_cgroup(self.cgroup)
        self.tree_empty = True if self.isolation is IsolationProfile.ISOLATED_WORKSPACE else drained
        remove_cgroup(self.cgroup)
        code = proc.returncode
        with self._lock:
            self.stdout = b"".join(stdout_chunks)
            self.stderr = b"".join(stderr_chunks)
            self.stdout_bytes = len(self.stdout)
            self.stderr_bytes = len(self.stderr)
            self.truncated_stdout = stdout_trunc
            self.truncated_stderr = stderr_trunc
            self.finished_at = time.time()
            self.timed_out = timed_out
            self.cancelled = cancelled and not timed_out
            if code is None:
                self.exit_code = None
            elif code < 0:
                self.exit_code = None
                self.signal_num = -code
            else:
                self.exit_code = int(code)

    def close_pidfd(self) -> None:
        if self.pidfd is not None:
            with contextlib.suppress(OSError):
                os.close(self.pidfd)
            self.pidfd = None

    def _close_pipes(self) -> None:
        for attr in ("_stdin_r", "_stdin_w", "_stdout_r", "_stdout_w", "_stderr_r", "_stderr_w"):
            fd = getattr(self, attr, -1)
            if isinstance(fd, int) and fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
                setattr(self, attr, -1)

    def _close_remaining(self, selector: selectors.DefaultSelector) -> None:
        for key in list(selector.get_map().values()):
            with contextlib.suppress(Exception):
                selector.unregister(key.fd)
            with contextlib.suppress(OSError):
                os.close(key.fd)
        self._close_pipes()


def _cap(chunk: bytes, captured: int, limit: int, truncated: bool) -> tuple[bytes, bool]:
    remaining = limit - captured
    if remaining <= 0:
        return b"", True
    if len(chunk) > remaining:
        return chunk[:remaining], True
    return chunk, truncated
