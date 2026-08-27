"""Held-FD spawn inside a proven systemd/cgroup scope."""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .boundary import ProvenScope
from .contracts import (
    BWRAP_BLOCK_FD,
    MAX_OUTPUT_DEPTH,
    MAX_OUTPUT_DIRS,
    MAX_OUTPUT_FILES,
    MAX_OUTPUT_TREE_BYTES,
    CommandError,
    HeldExecutable,
    IsolationProfile,
    PathRoot,
    ResourceLimits,
)
from .isolate import OUTPUT_EXPORT_IMPL, OUTPUT_EXPORT_SCRIPT, bwrap_argv, extra_ro_binds
from .resolve import confirm_held, confirm_path_roots, sealed_payload_memfd
from .spawn_helper import SpawnBroker, StartedJob
from .workspace import JobWorkspace

_POLL_SEC = 0.05
_KILL_GRACE_SEC = 2.0
_EOF_GRACE_SEC = 2.0


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


def _terminate_process(proc: _SpawnedProcess, scope: ProvenScope | None) -> bool:
    """Signal only the pidfd identity; use cgroup.kill for the full tree."""
    pidfd = proc.pidfd
    if pidfd is not None:
        with contextlib.suppress(OSError):
            signal.pidfd_send_signal(pidfd, signal.SIGTERM)
    deadline = time.monotonic() + _KILL_GRACE_SEC
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is None and pidfd is not None:
        with contextlib.suppress(OSError):
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
    return False if scope is None else scope.kill()


class _SpawnedProcess:
    """bwrap child of the spawn helper. Exit status arrives on the control socket."""

    def __init__(self, pid: int, pidfd: int | None, ctrl_fd: int) -> None:
        self.pid = int(pid)
        self.pidfd = pidfd
        self.ctrl_fd = int(ctrl_fd)
        self.returncode: int | None = None
        self._buf = bytearray()

    def _read_ctrl(self) -> None:
        if self.returncode is not None or self.ctrl_fd < 0:
            return
        try:
            chunk = os.read(self.ctrl_fd, 4096)
        except BlockingIOError:
            return
        except OSError:
            self.returncode = -signal.SIGKILL
            return
        if not chunk:
            if self.returncode is None:
                self.returncode = -signal.SIGKILL
            return
        self._buf.extend(chunk)
        if b"\n" not in self._buf:
            return
        line, _, rest = bytes(self._buf).partition(b"\n")
        self._buf = bytearray(rest)
        try:
            payload = json.loads(line.decode("ascii"))
        except (ValueError, UnicodeError):
            self.returncode = -signal.SIGKILL
            return
        if not isinstance(payload, dict):
            self.returncode = -signal.SIGKILL
            return
        if "returncode" in payload:
            self.returncode = int(payload["returncode"])
        elif payload.get("error"):
            self.returncode = -signal.SIGKILL

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        self._read_ctrl()
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while deadline is None or time.monotonic() < deadline:
            code = self.poll()
            if code is not None:
                return code
            time.sleep(0.01)
        raise subprocess.TimeoutExpired("bwrap", float(timeout or 0))

    def close_ctrl(self) -> None:
        if self.ctrl_fd >= 0:
            fd = self.ctrl_fd
            self.ctrl_fd = -1
            with contextlib.suppress(OSError):
                os.close(fd)

    def close_pidfd(self) -> None:
        if self.pidfd is not None:
            fd = self.pidfd
            self.pidfd = None
            with contextlib.suppress(OSError):
                os.close(fd)


def _output_usage(output: Path) -> tuple[int, int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(str(output), flags)
    except OSError as exc:
        raise CommandError("output_unreadable") from exc
    try:
        files, total, _dirs = _usage_walk(dir_fd, depth=0, dirs=1)
    finally:
        os.close(dir_fd)
    if files > MAX_OUTPUT_FILES or total > MAX_OUTPUT_TREE_BYTES:
        raise CommandError("output_quota_exceeded")
    return files, total


def _usage_walk(dir_fd: int, *, depth: int, dirs: int) -> tuple[int, int, int]:
    if depth > MAX_OUTPUT_DEPTH:
        raise CommandError("output_depth_overflow")
    try:
        names = os.listdir(f"/proc/self/fd/{dir_fd}")
    except OSError as exc:
        raise CommandError("output_unreadable") from exc
    files = 0
    total = 0
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    for name in names:
        if name in {".", ".."} or "/" in name:
            raise CommandError("path_escape")
        try:
            child = os.open(name, flags | os.O_DIRECTORY, dir_fd=dir_fd)
            nested = True
        except OSError:
            try:
                child = os.open(name, flags, dir_fd=dir_fd)
            except OSError as exc:
                raise CommandError("output_unreadable") from exc
            nested = False
        try:
            st = os.fstat(child)
            if nested:
                dirs += 1
                if dirs > MAX_OUTPUT_DIRS:
                    raise CommandError("output_tree_overflow")
                nested_files, nested_bytes, dirs = _usage_walk(child, depth=depth + 1, dirs=dirs)
                files += nested_files
                total += nested_bytes
            elif stat.S_ISREG(st.st_mode):
                files += 1
                total += int(st.st_size)
                if files > MAX_OUTPUT_FILES or total > MAX_OUTPUT_TREE_BYTES:
                    raise CommandError("output_quota_exceeded")
            else:
                raise CommandError("output_unreadable")
        finally:
            os.close(child)
    return files, total, dirs


@dataclass
class SpawnedCommand:
    workspace: JobWorkspace
    timeout_sec: int | None
    max_stdout_bytes: int
    max_stderr_bytes: int
    isolation: IsolationProfile
    limits: ResourceLimits
    process: _SpawnedProcess | None = None
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
    scope: ProvenScope | None = None
    quota_exceeded: bool = False
    quota_code: str = ""
    _cancel: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def spawn(
        self,
        held: HeldExecutable,
        *,
        stdin: bytes,
        env: dict[str, str],
        path_roots: tuple[PathRoot, ...],
        bwrap: HeldExecutable | None,
        scope: ProvenScope,
        broker: SpawnBroker,
    ) -> None:
        if self.isolation is IsolationProfile.HOST_USER:
            self._spawn_host(
                held,
                stdin=stdin,
                env=env,
                scope=scope,
                broker=broker,
            )
            return
        if self.isolation is not IsolationProfile.ISOLATED_WORKSPACE:
            raise CommandError("invalid_isolation_profile")
        if bwrap is None:
            raise CommandError("bubblewrap_unavailable")
        confirm_held(held)
        confirm_held(bwrap)
        confirm_path_roots(path_roots)
        os.lseek(held.executable_fd, 0, os.SEEK_SET)
        if held.script_fd is not None:
            os.lseek(held.script_fd, 0, os.SEEK_SET)
        extra = extra_ro_binds(path_roots)
        argv = bwrap_argv(
            workspace=self.workspace,
            held=held,
            env=env,
            extra_binds=extra,
            limits=self.limits,
            sync_fd=BWRAP_BLOCK_FD,
        )
        child_env = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}
        export_fd = sealed_payload_memfd(OUTPUT_EXPORT_SCRIPT, label="friday-export")
        export_impl_fd = sealed_payload_memfd(OUTPUT_EXPORT_IMPL, label="friday-export-impl")
        stdin_payload_fd = sealed_payload_memfd(stdin, label="friday-stdin")
        output_dir_fd = os.open(
            str(self.workspace.output),
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        stdout_r, stdout_w = os.pipe()
        stderr_r, stderr_w = os.pipe()
        os.set_inheritable(stdout_w, True)
        os.set_inheritable(stderr_w, True)
        self._stdout_r = stdout_r
        self._stdout_w = stdout_w
        self._stderr_r = stderr_r
        self._stderr_w = stderr_w
        self.scope = scope
        started: StartedJob | None = None
        try:
            self.started_at = time.time()
            # Re-attest the exact unsealed, root-owned bwrap inode at the
            # transfer seam.  User payload executables remain sealed memfds.
            confirm_held(bwrap)
            started = broker.start_job(
                argv=argv,
                env=child_env,
                cgroup=str(scope.cgroup),
                fsize=int(self.limits.fsize_bytes),
                stdin_r=output_dir_fd,
                stdout_w=stdout_w,
                stderr_w=stderr_w,
                exec_fd=held.executable_fd,
                launcher_fd=bwrap.executable_fd,
                export_fd=export_fd,
                export_impl_fd=export_impl_fd,
                stdin_payload_fd=stdin_payload_fd,
                script_fd=held.script_fd,
                path_root_fds=[root.dir_fd for root in path_roots],
            )
            self.effect_boundary_crossed = True
            self.pid = int(started.pid)
            self.pid_starttime = (
                started.starttime if started.starttime is not None else _pid_starttime(self.pid)
            )
            self.process = _SpawnedProcess(started.pid, started.pidfd, started.ctrl_fd)
            self.pidfd = self.process.pidfd
            os.set_blocking(started.ctrl_fd, False)
            os.close(stdout_w)
            os.close(stderr_w)
            self._stdout_w = -1
            self._stderr_w = -1
        except CommandError:
            self.abort()
            raise
        except Exception as exc:
            self.abort()
            self._close_pipes()
            detail = str(exc)
            if detail in {"resource_boundary_unproven", "spawn_helper_unavailable"}:
                raise CommandError(detail) from exc
            raise CommandError("spawn_failed") from exc
        finally:
            with contextlib.suppress(OSError):
                os.close(export_fd)
            with contextlib.suppress(OSError):
                os.close(export_impl_fd)
            with contextlib.suppress(OSError):
                os.close(output_dir_fd)
            with contextlib.suppress(OSError):
                os.close(stdin_payload_fd)

    def _spawn_host(
        self,
        held: HeldExecutable,
        *,
        stdin: bytes,
        env: dict[str, str],
        scope: ProvenScope,
        broker: SpawnBroker,
    ) -> None:
        confirm_held(held)
        if held.script_fd is not None or held.resolved.canonical_path != "/usr/bin/bash":
            raise CommandError("bash_path_mismatch")
        os.lseek(held.executable_fd, 0, os.SEEK_SET)
        stdin_fd = sealed_payload_memfd(stdin, label="friday-host-stdin")
        stdout_r, stdout_w = os.pipe()
        stderr_r, stderr_w = os.pipe()
        os.set_inheritable(stdout_w, True)
        os.set_inheritable(stderr_w, True)
        self._stdout_r = stdout_r
        self._stdout_w = stdout_w
        self._stderr_r = stderr_r
        self._stderr_w = stderr_w
        self.scope = scope
        try:
            self.started_at = time.time()
            confirm_held(held)
            started = broker.start_host_job(
                argv=[held.resolved.canonical_path, *held.inner_rest],
                env=env,
                cwd=env["FRIDAY_WORK_DIR"],
                cgroup=str(scope.cgroup),
                stdin_r=stdin_fd,
                stdout_w=stdout_w,
                stderr_w=stderr_w,
                launcher_fd=held.executable_fd,
            )
            self.effect_boundary_crossed = True
            self.pid = int(started.pid)
            self.pid_starttime = (
                started.starttime if started.starttime is not None else _pid_starttime(self.pid)
            )
            self.process = _SpawnedProcess(started.pid, started.pidfd, started.ctrl_fd)
            self.pidfd = self.process.pidfd
            os.set_blocking(started.ctrl_fd, False)
            os.close(stdout_w)
            os.close(stderr_w)
            self._stdout_w = -1
            self._stderr_w = -1
        except CommandError:
            self.abort()
            raise
        except Exception as exc:
            self.abort()
            self._close_pipes()
            detail = str(exc)
            if detail in {"resource_boundary_unproven", "spawn_helper_unavailable"}:
                raise CommandError(detail) from exc
            raise CommandError("spawn_failed") from exc
        finally:
            with contextlib.suppress(OSError):
                os.close(stdin_fd)

    def abort(self) -> None:
        proc = self.process
        if proc is not None:
            self.tree_empty = _terminate_process(proc, self.scope)
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)
            proc.close_ctrl()
        elif self.scope is not None:
            self.tree_empty = self.scope.kill()
        self._close_pipes()

    def request_cancel(self) -> None:
        self._cancel.set()
        proc = self.process
        if proc is not None and proc.poll() is None:
            _terminate_process(proc, self.scope)
        elif self.scope is not None:
            self.scope.kill()

    def request_shutdown(self) -> None:
        """Signal cancellation for process shutdown without serial cleanup waits."""

        self._cancel.set()
        proc = self.process
        if proc is not None and proc.pidfd is not None:
            with contextlib.suppress(OSError):
                signal.pidfd_send_signal(proc.pidfd, signal.SIGTERM)
        if self.scope is not None:
            self.scope.request_kill()

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
        selector.register(self._stdout_r, selectors.EVENT_READ, "stdout")
        selector.register(self._stderr_r, selectors.EVENT_READ, "stderr")
        if proc.ctrl_fd >= 0:
            selector.register(proc.ctrl_fd, selectors.EVENT_READ, "exit")
        deadline = None if self.timeout_sec is None else time.monotonic() + self.timeout_sec
        timed_out = False
        cancelled = False
        reader_failed = False

        def _consume_event(key: selectors.SelectorKey, mask: int) -> None:
            nonlocal stdout_captured, stderr_captured, stdout_trunc, stderr_trunc, reader_failed
            kind = key.data
            try:
                if kind in {"stdout", "stderr"} and mask & selectors.EVENT_READ:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fd)
                        self._close_output_fd(kind, key.fd)
                        return
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
                elif kind == "exit" and mask & selectors.EVENT_READ:
                    proc._read_ctrl()
                    if proc.returncode is not None:
                        with contextlib.suppress(Exception):
                            selector.unregister(proc.ctrl_fd)
                        proc.close_ctrl()
            except OSError:
                reader_failed = True
                with contextlib.suppress(Exception):
                    selector.unregister(key.fd)
                if kind in {"stdout", "stderr"}:
                    self._close_output_fd(kind, key.fd)
                elif kind == "exit":
                    proc.close_ctrl()

        try:
            while proc.poll() is None:
                if not cancelled and self._cancel.is_set():
                    cancelled = True
                    _terminate_process(proc, self.scope)
                elif not timed_out and deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    _terminate_process(proc, self.scope)
                try:
                    files, nbytes = _output_usage(self.workspace.output)
                    if files > MAX_OUTPUT_FILES or nbytes > MAX_OUTPUT_TREE_BYTES:
                        raise CommandError("output_quota_exceeded")
                except CommandError as exc:
                    if exc.code in {
                        "output_quota_exceeded",
                        "output_depth_overflow",
                        "output_tree_overflow",
                    }:
                        self.quota_exceeded = True
                        self.quota_code = exc.code
                        _terminate_process(proc, self.scope)
                    elif exc.code.startswith("output_"):
                        pass
                    else:
                        raise
                timeout = _POLL_SEC
                try:
                    events = selector.select(timeout)
                except InterruptedError:
                    continue
                for key, mask in events:
                    _consume_event(key, mask)
                with self._lock:
                    self.stdout_bytes = stdout_captured
                    self.stderr_bytes = stderr_captured
                    self.truncated_stdout = stdout_trunc
                    self.truncated_stderr = stderr_trunc
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
            if self._cancel.is_set() and not timed_out:
                cancelled = True
            eof_deadline = time.monotonic() + _EOF_GRACE_SEC
            while selector.get_map() and time.monotonic() < eof_deadline:
                for key, mask in selector.select(0.05):
                    _consume_event(key, mask)
            self.eof_proven = not selector.get_map() and not reader_failed
        finally:
            stdout_handle.close()
            stderr_handle.close()
            self._close_remaining(selector)
        if proc.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)
        code = proc.returncode
        if self.scope is not None:
            self.tree_empty = bool(self.scope.kill())
        else:
            self.tree_empty = False
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
        proc = self.process
        if proc is not None:
            proc.close_pidfd()
            proc.close_ctrl()
        elif self.pidfd is not None:
            with contextlib.suppress(OSError):
                os.close(self.pidfd)
        self.pidfd = None

    def _close_pipes(self) -> None:
        for attr in ("_stdin_r", "_stdin_w", "_stdout_r", "_stdout_w", "_stderr_r", "_stderr_w"):
            fd = getattr(self, attr, -1)
            if isinstance(fd, int) and fd >= 0:
                setattr(self, attr, -1)
                with contextlib.suppress(OSError):
                    os.close(fd)

    def _close_output_fd(self, kind: str, fd: int) -> None:
        attr = "_stdout_r" if kind == "stdout" else "_stderr_r"
        if getattr(self, attr, -1) != fd:
            return
        setattr(self, attr, -1)
        with contextlib.suppress(OSError):
            os.close(fd)

    def _close_remaining(self, selector: selectors.DefaultSelector) -> None:
        for key in list(selector.get_map().values()):
            with contextlib.suppress(Exception):
                selector.unregister(key.fd)
            if key.data in {"stdout", "stderr"}:
                self._close_output_fd(str(key.data), key.fd)
            elif key.data == "exit" and self.process is not None:
                self.process.close_ctrl()
        self._close_pipes()


def _cap(chunk: bytes, captured: int, limit: int, truncated: bool) -> tuple[bytes, bool]:
    remaining = limit - captured
    if remaining <= 0:
        return b"", True
    if len(chunk) > remaining:
        return chunk[:remaining], True
    return chunk, truncated
