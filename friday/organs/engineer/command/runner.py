"""Held-FD spawn inside a proven systemd/cgroup scope. host_user is not in-process."""

from __future__ import annotations

import contextlib
import os
import resource
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
    MAX_OUTPUT_FILES,
    MAX_OUTPUT_TREE_BYTES,
    CommandError,
    HeldExecutable,
    IsolationProfile,
    PathRoot,
    ResourceLimits,
)
from .isolate import bwrap_argv, extra_ro_binds
from .resolve import confirm_held, confirm_path_roots
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


def _kill_session(pid: int) -> None:
    def _kill(sig: int) -> bool:
        try:
            os.killpg(pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, sig)
                return True
            except ProcessLookupError:
                return False
            except OSError:
                return False

    if not _kill(signal.SIGTERM):
        return
    deadline = time.monotonic() + _KILL_GRACE_SEC
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    _kill(signal.SIGKILL)


class _SpawnedProcess:
    """Child reaped via pidfd. The spawn helper is the actual parent."""

    def __init__(self, pid: int, pidfd: int | None) -> None:
        self.pid = int(pid)
        self.pidfd = pidfd
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        waited, status = os.waitpid(self.pid, os.WNOHANG)
        if waited == 0:
            return None
        self.returncode = os.waitstatus_to_exitcode(status)
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


def _output_usage(output: Path) -> tuple[int, int]:
    files = 0
    total = 0
    try:
        dir_fd = os.open(
            str(output),
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        return 0, 0
    try:
        files, total = _usage_walk(dir_fd, depth=0)
    finally:
        os.close(dir_fd)
    return files, total


def _usage_walk(dir_fd: int, *, depth: int) -> tuple[int, int]:
    if depth > 8:
        return 0, 0
    files = 0
    total = 0
    try:
        names = os.listdir(f"/proc/self/fd/{dir_fd}")
    except OSError:
        return 0, 0
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    for name in names:
        if name in {".", ".."} or "/" in name:
            continue
        try:
            child = os.open(name, flags | os.O_DIRECTORY, dir_fd=dir_fd)
            nested = True
        except OSError:
            try:
                child = os.open(name, flags, dir_fd=dir_fd)
            except OSError:
                continue
            nested = False
        try:
            st = os.fstat(child)
            if nested:
                nested_files, nested_bytes = _usage_walk(child, depth=depth + 1)
                files += nested_files
                total += nested_bytes
            elif stat.S_ISREG(st.st_mode):
                files += 1
                total += int(st.st_size)
        finally:
            os.close(child)
    return files, total


@dataclass
class SpawnedCommand:
    workspace: JobWorkspace
    timeout_sec: int
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
    ) -> None:
        if self.isolation is IsolationProfile.HOST_USER:
            raise CommandError("host_user_requires_broker")
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
        block_r, block_w = os.pipe()
        block_child = os.dup(block_r)
        os.set_inheritable(block_child, True)
        os.close(block_r)
        argv = bwrap_argv(
            workspace=self.workspace,
            held=held,
            env=env,
            extra_binds=extra,
            limits=self.limits,
            sync_fd=block_child,
        )
        # Exec the root-owned bwrap path (uid_map is denied from a memfd). The
        # job executable/interpreter/script still enter via sealed memfds.
        launcher = bwrap.resolved.canonical_path
        child_env = {"PATH": "/usr/bin", "LANG": "C.UTF-8"}

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
        self.scope = scope
        try:
            self.started_at = time.time()
            actions: list[tuple] = [
                (os.POSIX_SPAWN_DUP2, stdin_r, 0),
                (os.POSIX_SPAWN_DUP2, stdout_w, 1),
                (os.POSIX_SPAWN_DUP2, stderr_w, 2),
                (os.POSIX_SPAWN_DUP2, held.executable_fd, 3),
            ]
            if held.script_fd is not None:
                actions.append((os.POSIX_SPAWN_DUP2, held.script_fd, 4))
            else:
                actions.append((os.POSIX_SPAWN_CLOSE, 4))
            actions.append((os.POSIX_SPAWN_DUP2, block_child, 5))
            actions.append((os.POSIX_SPAWN_CLOSEFROM, 6))
            pid = os.posix_spawn(launcher, argv, child_env, file_actions=actions)
            self.effect_boundary_crossed = True
            self.pid = int(pid)
            self.pid_starttime = _pid_starttime(self.pid)
            try:
                self.pidfd = os.pidfd_open(self.pid, 0)
            except OSError:
                self.pidfd = None
            self.process = _SpawnedProcess(pid, self.pidfd)
            os.close(block_child)
            try:
                resource.prlimit(
                    self.pid,
                    resource.RLIMIT_FSIZE,
                    (int(self.limits.fsize_bytes), int(self.limits.fsize_bytes)),
                )
            except (OSError, ValueError) as exc:
                if block_w >= 0:
                    os.close(block_w)
                self.abort()
                raise CommandError("resource_boundary_unproven") from exc
            expected = str(scope.cgroup).removeprefix("/sys/fs/cgroup")
            moved = False
            last_relative = ""
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                try:
                    procs_fd = os.open(
                        str(scope.cgroup / "cgroup.procs"),
                        os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
                    )
                    try:
                        os.write(procs_fd, f"{int(self.pid)}\n".encode("ascii"))
                    finally:
                        os.close(procs_fd)
                except OSError as exc:
                    last_relative = str(exc)
                    time.sleep(0.02)
                    continue
                try:
                    raw = Path(f"/proc/{self.pid}/cgroup").read_text(encoding="ascii")
                except OSError as exc:
                    if block_w >= 0:
                        os.close(block_w)
                    self.abort()
                    raise CommandError("resource_boundary_unproven") from exc
                last_relative = ""
                for line in raw.splitlines():
                    if line.startswith("0::"):
                        last_relative = line[3:]
                        break
                if last_relative == expected or last_relative.startswith(expected.rstrip("/") + "/"):
                    moved = True
                    break
                time.sleep(0.02)
            if not moved:
                if block_w >= 0:
                    os.close(block_w)
                self.abort()
                raise CommandError(
                    "resource_boundary_unproven",
                    detail=f"cgroup {last_relative!r} != {expected!r}",
                )
            if block_w >= 0:
                try:
                    os.write(block_w, b"x")
                except OSError as exc:
                    os.close(block_w)
                    self.abort()
                    raise CommandError("spawn_failed") from exc
                os.close(block_w)
        except CommandError:
            raise
        except OSError as exc:
            self.abort()
            self._close_pipes()
            raise CommandError("spawn_failed") from exc
        os.close(stdin_r)
        os.close(stdout_w)
        os.close(stderr_w)
        self._stdin_r = -1
        self._stdout_w = -1
        self._stderr_w = -1

    def abort(self) -> None:
        proc = self.process
        if proc is not None and proc.pid:
            _kill_session(proc.pid)
            with contextlib.suppress(Exception):
                proc.wait(timeout=2)
        if self.scope is not None:
            self.scope.kill()
        self._close_pipes()

    def request_cancel(self) -> None:
        self._cancel.set()
        proc = self.process
        if proc is not None and proc.poll() is None and proc.pid:
            _kill_session(proc.pid)
        if self.scope is not None:
            self.scope.kill()

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
                    if self.scope is not None:
                        self.scope.kill()
                elif not timed_out and time.monotonic() >= deadline:
                    timed_out = True
                    _kill_session(proc.pid)
                    if self.scope is not None:
                        self.scope.kill()
                files, nbytes = _output_usage(self.workspace.output)
                if files > MAX_OUTPUT_FILES or nbytes > MAX_OUTPUT_TREE_BYTES:
                    self.quota_exceeded = True
                    _kill_session(proc.pid)
                    if self.scope is not None:
                        self.scope.kill()
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
