"""Foreground child-process supervisor for ``jericho up``.

Keeps the backend and the Telegram bridge alive from one terminal: starts each
child, restarts it with backoff after a crash, and gives up on a child that
crash-loops (N rapid exits in a row) instead of burning CPU forever — the
операторская ошибка (занятый порт, битый конфиг) должна стать сообщением, а не
бесконечным рестартом. A child that fails permanently does not take down its
siblings: a broken bridge leaves the backend running.

The class is generic over argv so it is testable with plain shell commands;
``jericho up`` wires the real children.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess  # nosec B404 - supervising our own CLI subcommands
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from friday.private_fs import (
    copy_private_file,
    open_private_binary_append,
    restrict_private_file,
    restrict_private_tree,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class ChildSpec:
    name: str
    argv: list[str]
    log_path: Path
    # Children run from an explicit directory so a stray cwd (e.g. one whose
    # subdirectory shadows the installed package) cannot break them.
    cwd: Path | None = None


@dataclass
class _ChildState:
    spec: ChildSpec
    process: subprocess.Popen[Any] | None = None
    log_handle: Any = None
    started_at: float = 0.0
    rapid_crashes: int = 0
    backoff: float = 0.0
    restart_at: float = 0.0
    failed: bool = False
    restarts: int = 0
    group_retired: bool = True
    stopping: bool = field(default=False, repr=False)


class Supervisor:
    def __init__(
        self,
        children: list[ChildSpec],
        *,
        backoff_initial: float = 2.0,
        backoff_max: float = 60.0,
        crash_window_sec: float = 15.0,
        max_rapid_crashes: int = 3,
        poll_interval_sec: float = 0.5,
        log_max_bytes: int = 0,
        log_backups: int = 3,
        log_check_interval_sec: float = 30.0,
    ) -> None:
        self._children = [_ChildState(spec=spec) for spec in children]
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._crash_window = crash_window_sec
        self._max_rapid_crashes = max_rapid_crashes
        self._poll_interval = poll_interval_sec
        self._log_max_bytes = max(0, int(log_max_bytes))
        self._log_backups = max(0, int(log_backups))
        self._log_check_interval = max(0.0, float(log_check_interval_sec))
        self._next_log_check = 0.0
        self._stopping = False

    # -- log rotation ------------------------------------------------------

    def _rotate(self, path: Path) -> None:
        """Copy-truncate ``path``, keeping ``_log_backups`` numbered generations.

        Copy-truncate, not rename: the child holds an inherited fd on this exact
        inode, so a rename would leave it writing into the rotated-away file and
        the live log would stay empty until the next restart. Truncation is safe
        because the fd was opened ``"ab"`` (``O_APPEND``) — the kernel re-seeks to
        end-of-file on every write, so the child continues at offset 0 instead of
        leaving a multi-megabyte sparse hole.

        The window between copy and truncate can drop the handful of lines written
        inside it. That is the standard ``logrotate copytruncate`` trade and it is
        the right one here: the alternative is piping every child through the
        supervisor, which makes a stalled supervisor able to block the backend.
        """
        restrict_private_file(path)
        if self._log_backups:
            path.with_name(f"{path.name}.{self._log_backups}").unlink(missing_ok=True)
            for index in range(self._log_backups - 1, 0, -1):
                generation = path.with_name(f"{path.name}.{index}")
                if generation.is_symlink():
                    raise ValueError("rotated log cannot be a symlink")
                if generation.exists():
                    shifted = generation.replace(path.with_name(f"{path.name}.{index + 1}"))
                    restrict_private_file(shifted)
            copy_private_file(path, path.with_name(f"{path.name}.1"))
        os.truncate(path, 0)

    def _rotate_logs_if_needed(self) -> None:
        if not self._log_max_bytes:
            return
        now = time.monotonic()
        if now < self._next_log_check:
            return
        self._next_log_check = now + self._log_check_interval
        for child in self._children:
            path = child.spec.log_path
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < self._log_max_bytes:
                continue
            try:
                self._rotate(path)
            except (OSError, ValueError) as exc:
                # An unrotatable log must not take the supervisor down with it.
                LOGGER.warning("Не смог провернуть лог (%s)", type(exc).__name__)
                continue
            LOGGER.info("Лог превысил %d байт — провёрнут", self._log_max_bytes)

    # -- lifecycle ---------------------------------------------------------

    def _start(self, child: _ChildState) -> None:
        restrict_private_tree(child.spec.log_path.parent)
        log_handle = open_private_binary_append(child.spec.log_path)
        try:
            process = subprocess.Popen(  # nosec B603 - fixed argv, no shell
                child.spec.argv,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(child.spec.cwd) if child.spec.cwd else None,
            )
        except BaseException:
            # Opening the private log transfers no ownership to Popen when the
            # exec itself is refused.
            log_handle.close()
            raise
        child.log_handle = log_handle
        child.process = process
        child.group_retired = False
        child.started_at = time.monotonic()
        LOGGER.info("Запущен %s (pid %s)", child.spec.name, child.process.pid)

    def _on_exit(self, child: _ChildState, code: int) -> None:
        lived = time.monotonic() - child.started_at
        if child.log_handle is not None:
            child.log_handle.close()
            child.log_handle = None
        child.process = None
        if self._stopping:
            return
        if lived < self._crash_window:
            child.rapid_crashes += 1
        else:
            child.rapid_crashes = 0
            child.backoff = 0.0
        if child.rapid_crashes >= self._max_rapid_crashes:
            child.failed = True
            LOGGER.error(
                "%s упал %d раз подряд (код %s) — больше не перезапускаю",
                child.spec.name,
                child.rapid_crashes,
                code,
            )
            return
        child.backoff = min(self._backoff_max, child.backoff * 2 if child.backoff else self._backoff_initial)
        child.restart_at = time.monotonic() + child.backoff
        child.restarts += 1
        LOGGER.warning(
            "%s завершился (код %s) — перезапуск через %.0f с", child.spec.name, code, child.backoff
        )

    @staticmethod
    def _leader_exited(child: _ChildState) -> bool:
        """Observe the leader without releasing its PID/process-group identity."""

        process = child.process
        if process is None or child.group_retired:
            return True
        if os.name != "posix":
            return process.poll() is not None
        if process.returncode is not None:
            raise RuntimeError("owned_process_group_identity_lost")
        while True:
            try:
                status = os.waitid(
                    os.P_PID,
                    process.pid,
                    os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
            except InterruptedError:
                continue
            except ChildProcessError as exc:
                raise RuntimeError("owned_process_group_identity_lost") from exc
            return status is not None

    @staticmethod
    def _group_has_live_member(pgid: int) -> bool:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                fields = (entry / "stat").read_bytes().rsplit(b")", 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if int(fields[2]) == pgid and fields[0] not in {b"Z", b"X"}:
                return True
        return False

    def _signal_process_group(self, child: _ChildState, sent: int) -> None:
        process = child.process
        if process is None or child.group_retired:
            return
        # waitid(WNOWAIT) proves that this exact direct child is still ours,
        # alive or zombie, so its PID cannot be reused as somebody else's PGID.
        self._leader_exited(child)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, sent)

    @staticmethod
    def _wait_for_group_death(child: _ChildState, deadline: float) -> bool:
        process = child.process
        if process is None:
            return True
        while Supervisor._group_has_live_member(process.pid):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _wait_for_leader_exit(self, child: _ChildState, deadline: float) -> bool:
        while not self._leader_exited(child):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def _force_retire_process_group(self, child: _ChildState) -> None:
        process = child.process
        if process is None or child.group_retired:
            return
        self._signal_process_group(child, signal.SIGKILL)
        if not self._wait_for_group_death(child, time.monotonic() + 5.0):
            raise RuntimeError("owned_process_group_cleanup_unconfirmed")
        # The retirement marker is committed while the exact leader is still
        # waitable; only the following dedicated reap may release its PID.
        child.group_retired = True

    @staticmethod
    def _reap_retired_leader(child: _ChildState) -> int:
        process = child.process
        if process is None:
            raise RuntimeError("owned_process_missing_before_reap")
        if not child.group_retired:
            raise RuntimeError("owned_process_group_not_retired")
        return process.wait(timeout=5.0)

    def _terminate_all(self) -> None:
        cleanup_errors: list[Exception] = []
        # Signal every owned group before waiting for any one of them, keeping
        # graceful shutdown bounded for siblings as a set.
        for child in self._children:
            process = child.process
            if process is None:
                continue
            try:
                self._signal_process_group(child, signal.SIGTERM)
            except Exception as exc:  # cleanup must continue for the other owned children
                cleanup_errors.append(exc)
        deadline = time.monotonic() + 10.0
        for child in self._children:
            process = child.process
            try:
                if process is not None:
                    # Once the leader has accepted graceful TERM, immediately
                    # retire any inherited descendants which ignored it.
                    self._wait_for_leader_exit(child, deadline)
                    self._force_retire_process_group(child)
                    self._reap_retired_leader(child)
            except Exception as exc:  # preserve the first error after every child/log was visited
                cleanup_errors.append(exc)
                if process is not None:
                    try:
                        self._force_retire_process_group(child)
                        self._reap_retired_leader(child)
                    except Exception as fallback_exc:
                        cleanup_errors.append(fallback_exc)
            finally:
                handle = child.log_handle
                if handle is not None:
                    try:
                        handle.close()
                    except Exception as exc:
                        cleanup_errors.append(exc)
                        if getattr(handle, "closed", False):
                            child.log_handle = None
                    else:
                        child.log_handle = None
        if cleanup_errors:
            raise cleanup_errors[0]

    # -- main loop ---------------------------------------------------------

    def run(self) -> int:
        """Blocking supervision loop; 0 on clean shutdown, 1 when every child failed."""

        def _request_stop(signum: int, frame: Any) -> None:
            del frame
            LOGGER.info("Получен сигнал %s — останавливаю детей", signum)
            self._stopping = True

        # Signal handlers are only installable from the main thread; an embedded
        # supervisor (tests, future orchestration) is stopped via ``stop()``.
        import threading as _threading

        previous: dict[int, Any] = {}
        try:
            if _threading.current_thread() is _threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM):
                    previous[sig] = signal.signal(sig, _request_stop)
            try:
                for child in self._children:
                    self._start(child)
                while not self._stopping:
                    alive = 0
                    for child in self._children:
                        if child.failed:
                            continue
                        if child.process is None:
                            if time.monotonic() >= child.restart_at:
                                self._start(child)
                            alive += 1  # scheduled for restart still counts as managed
                            continue
                        if not self._leader_exited(child):
                            alive += 1
                            continue
                        self._force_retire_process_group(child)
                        code = self._reap_retired_leader(child)
                        self._on_exit(child, code)
                        if not child.failed:
                            alive += 1
                    if alive == 0:
                        LOGGER.error("Все процессы остановлены с ошибками — завершаю supervision")
                        return 1
                    self._rotate_logs_if_needed()
                    time.sleep(self._poll_interval)
                return 0
            finally:
                # This is the sole owner-cleanup boundary for normal stop,
                # startup refusal, restart refusal, and unexpected loop exits.
                self._terminate_all()
        finally:
            for signal_number, handler in previous.items():
                signal.signal(signal_number, handler)

    def stop(self) -> None:
        """Request a clean shutdown (thread-safe alternative to signals)."""
        self._stopping = True

    @property
    def snapshot(self) -> list[dict[str, Any]]:
        return [
            {
                "name": child.spec.name,
                "running": child.process is not None and not self._leader_exited(child),
                "failed": child.failed,
                "restarts": child.restarts,
            }
            for child in self._children
        ]
