"""Local process-group backend. Not the production systemd-user host-agent runner."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .contracts import CommandError
from .resolve import reopen_and_confirm
from .workspace import JobWorkspace

_PR_SET_PDEATHSIG = 1
_KILL_GRACE_SEC = 2.0
_POLL_SEC = 0.05


def _prctl_pdeathsig() -> None:
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    except Exception:
        return


def _preexec() -> None:
    os.umask(0o077)
    _prctl_pdeathsig()


def _kill_group(pid: int) -> None:
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


@dataclass
class SpawnedCommand:
    argv: tuple[str, ...]
    workspace: JobWorkspace
    timeout_sec: int
    max_stdout_bytes: int
    max_stderr_bytes: int
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
    _cancel: threading.Event = field(default_factory=threading.Event)
    _done: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def spawn(self, resolved) -> None:
        reopen_and_confirm(resolved)
        stdin_fd = os.open(str(self.workspace.stdin_path), os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            self.started_at = time.time()
            self.process = subprocess.Popen(
                list(self.argv),
                cwd=str(self.workspace.job_dir),
                env=self.workspace.env(),
                stdin=stdin_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
                preexec_fn=_preexec,
            )
            self.effect_boundary_crossed = True
        except OSError as exc:
            raise CommandError("spawn_failed") from exc
        finally:
            os.close(stdin_fd)

    def request_cancel(self) -> None:
        self._cancel.set()
        proc = self.process
        if proc is not None and proc.poll() is None:
            _kill_group(proc.pid)

    def wait(self) -> None:
        proc = self.process
        if proc is None:
            raise CommandError("not_started")
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        def _reader(stream, chunks: list[bytes], *, kind: str) -> None:
            limit = self.max_stdout_bytes if kind == "stdout" else self.max_stderr_bytes
            path = self.workspace.stdout_path if kind == "stdout" else self.workspace.stderr_path
            captured = 0
            truncated = False
            with path.open("ab") as handle:
                while True:
                    data = stream.read(64 * 1024)
                    if not data:
                        break
                    self.output_activity = True
                    remaining = limit - captured
                    if remaining > 0:
                        take = data[:remaining]
                        chunks.append(take)
                        handle.write(take)
                        handle.flush()
                        captured += len(take)
                        if len(data) > remaining:
                            truncated = True
                    else:
                        truncated = True
            with self._lock:
                if kind == "stdout":
                    self.stdout_bytes = captured
                    self.truncated_stdout = truncated
                else:
                    self.stderr_bytes = captured
                    self.truncated_stderr = truncated

        assert proc.stdout is not None and proc.stderr is not None
        stdout_thread = threading.Thread(target=_reader, args=(proc.stdout, stdout_chunks), kwargs={"kind": "stdout"}, daemon=True)
        stderr_thread = threading.Thread(target=_reader, args=(proc.stderr, stderr_chunks), kwargs={"kind": "stderr"}, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        deadline = time.monotonic() + self.timeout_sec
        timed_out = False
        while proc.poll() is None:
            if self._cancel.is_set():
                _kill_group(proc.pid)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_group(proc.pid)
                break
            with self._lock:
                self.stdout_bytes = sum(len(part) for part in stdout_chunks)
                self.stderr_bytes = sum(len(part) for part in stderr_chunks)
            time.sleep(_POLL_SEC)
        proc.wait()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        code = proc.returncode
        with self._lock:
            self.stdout = b"".join(stdout_chunks)
            self.stderr = b"".join(stderr_chunks)
            self.stdout_bytes = len(self.stdout)
            self.stderr_bytes = len(self.stderr)
            self.finished_at = time.time()
            self.timed_out = timed_out
            self.cancelled = self._cancel.is_set() and not timed_out
            if code is None:
                self.exit_code = None
            elif code < 0:
                self.exit_code = None
                self.signal_num = -code
            else:
                self.exit_code = int(code)
            self._done.set()
