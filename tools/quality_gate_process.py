"""Exclusive Linux command ownership without changing the exact-host namespaces.

The caller remains the existing gate parent. A subreaper adopts orphaned command
descendants, including setsid/double-fork children. Only handles proven to be its
direct children are signalled; kernel ECHILD is the final quiescence oracle.
This module does not schedule tests, set case deadlines or certify a gate.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_LOCK = threading.Lock()
_WAIT_FLAGS = getattr(os, "WEXITED", 0) | getattr(os, "WNOHANG", 0) | getattr(os, "WNOWAIT", 0)


class ProcessScopeError(RuntimeError):
    """Closed diagnostic code; command output belongs in private gate evidence."""


def _subreaper(value: int | None = None) -> int:
    library = ctypes.CDLL(None, use_errno=True)
    observed = ctypes.c_int()
    operation = _PR_GET_CHILD_SUBREAPER if value is None else _PR_SET_CHILD_SUBREAPER
    argument = ctypes.byref(observed) if value is None else value
    if library.prctl(operation, argument, 0, 0, 0) != 0:
        raise ProcessScopeError("gate_subreaper_unavailable")
    return observed.value if value is None else value


def _kernel_has_no_children() -> bool:
    try:
        os.waitid(os.P_ALL, 0, _WAIT_FLAGS)
    except ChildProcessError:
        return True
    # None means live children; a result means an unreaped exited child. Neither
    # gives an exclusive new command permission to own someone else's children.
    return False


def _direct_children() -> set[int]:
    children: set[int] = set()
    for task in Path("/proc/self/task").iterdir():
        try:
            children.update(map(int, (task / "children").read_text().split()))
        except FileNotFoundError:
            continue  # A sampler thread can finish between directory and read.
    return children


@dataclass(frozen=True)
class CleanupProof:
    schema: str
    leader_returncode: int | None
    leader_reaped: bool
    reaped_descendants: int
    forced_leader: bool
    forced_descendants: bool
    kernel_echild: bool
    subreaper_restored: bool
    failure_codes: tuple[str, ...]

    @property
    def cleanup_clear(self) -> bool:
        return self.kernel_echild and self.subreaper_restored and not self.failure_codes

    @property
    def clean_exit(self) -> bool:
        return (
            self.cleanup_clear
            and self.leader_reaped
            and self.leader_returncode == 0
            and not self.forced_leader
            and not self.forced_descendants
        )


class OwnedCommandScope:
    """One command and all descendants in a parent with no pre-existing children.

    Use only in the gate parent, which controls all child creation while this
    scope is active. The process and thread owner are checked on every operation.
    The caller must turn catchable controller signals into exceptions and keep
    cancellation deferred across start(), so Popen ownership is recorded first.
    close() also runs on cancellation/errors. Uncertain cleanup leaves adoption
    enabled and must fence scratch deletion and every later gate phase. This is
    not containment against SIGKILL of the gate parent or a child deliberately
    entering a foreign subreaper; neither can produce a valid completion proof.
    """

    def __init__(self, *, cleanup_timeout_s: float = 5.0):
        if type(cleanup_timeout_s) not in {int, float} or not 0 < cleanup_timeout_s <= 30:
            raise ValueError("gate_cleanup_timeout_invalid")
        self.timeout = cleanup_timeout_s
        self.owner = (os.getpid(), threading.get_ident())
        self.previous: int | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.proof: CleanupProof | None = None
        self.entered = False
        self.reaped_descendants = 0

    def _owner(self) -> None:
        if self.owner != (os.getpid(), threading.get_ident()):
            raise ProcessScopeError("gate_command_scope_foreign_owner")

    def __enter__(self) -> OwnedCommandScope:
        self._owner()
        if (
            sys.platform != "linux"
            or not hasattr(os, "pidfd_open")
            or not hasattr(os, "P_PIDFD")
            or not hasattr(signal, "pidfd_send_signal")
        ):
            raise ProcessScopeError("gate_recursive_cleanup_unavailable")
        if self.entered or self.proof is not None or not _LOCK.acquire(blocking=False):
            raise ProcessScopeError("gate_command_scope_busy")
        try:
            if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL or not _kernel_has_no_children():
                raise ProcessScopeError("gate_command_scope_not_exclusive")
            self.previous = _subreaper()
            _subreaper(1)
            self.entered = True
            return self
        except BaseException:
            _LOCK.release()
            raise

    def start(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        stdout: Any = None,
        stderr: Any = None,
        child_umask: int | None = None,
    ) -> subprocess.Popen[bytes]:
        self._owner()
        if not self.entered or self.proof is not None or self.process is not None:
            raise ProcessScopeError("gate_command_scope_start_invalid")
        if not _kernel_has_no_children():
            raise ProcessScopeError("gate_command_scope_not_exclusive")
        if child_umask is not None and (type(child_umask) is not int or not 0 <= child_umask <= 0o777):
            raise ProcessScopeError("gate_command_umask_invalid")
        self.process = subprocess.Popen(  # noqa: S603 - caller is the existing closed gate plan
            argv,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            umask=-1 if child_umask is None else child_umask,
        )
        return self.process

    def poll(self) -> int | None:
        """Reap adopted zombies while the controller still runs, without signals.

        The parent event loop must call this regularly: nested sandbox owners
        check their own process-group disappearance before the phase finishes.
        Deferring reaping until close() would keep their exited children visible.
        """
        self._owner()
        if not self.entered or self.proof is not None or self.process is None:
            raise ProcessScopeError("gate_command_scope_poll_invalid")
        self.process.poll()
        for pid in _direct_children():
            if pid == self.process.pid:
                continue
            try:
                descriptor = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            try:
                try:
                    state = os.waitid(os.P_PIDFD, descriptor, _WAIT_FLAGS)
                except ChildProcessError:
                    continue
                if state is not None:
                    reaped, _status = os.waitpid(pid, os.WNOHANG)
                    if reaped != pid:
                        raise ProcessScopeError("gate_child_reap_raced")
                    self.reaped_descendants += 1
            finally:
                os.close(descriptor)
        return self.process.returncode

    def wait(self, timeout: float) -> int:
        """Bounded command wait; case scheduling remains in the gate event loop."""
        if type(timeout) not in {int, float} or not 0 < timeout < float("inf"):
            raise ValueError("gate_command_wait_timeout_invalid")
        deadline = time.monotonic() + timeout
        while (returncode := self.poll()) is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                assert self.process is not None
                raise subprocess.TimeoutExpired(self.process.args, timeout)
            time.sleep(min(0.02, remaining))
        return returncode

    def close(self) -> CleanupProof:
        self._owner()
        if self.proof is not None:
            return self.proof
        if not self.entered:
            raise ProcessScopeError("gate_command_scope_not_entered")
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM})
        faults: set[str] = set()
        reaped_descendants = self.reaped_descendants
        forced_leader = forced_descendants = False
        quiescent = restored = False
        process = self.process
        try:
            if process is not None:
                process.poll()
            elif not _kernel_has_no_children():
                # Nothing was launched here. An admission violation cannot grant
                # permission to signal a child created by another caller.
                raise ProcessScopeError("gate_command_scope_not_exclusive")
            if _subreaper() != 1:
                raise ProcessScopeError("gate_subreaper_changed")
            deadline = time.monotonic() + self.timeout
            while not (quiescent := _kernel_has_no_children()):
                if time.monotonic() >= deadline:
                    faults.add("gate_recursive_cleanup_uncertain")
                    break
                try:
                    children = _direct_children()
                except (OSError, ValueError):
                    faults.add("gate_child_census_failed")
                    break
                for pid in children:
                    try:
                        descriptor = os.pidfd_open(pid)
                    except ProcessLookupError:
                        continue
                    try:
                        try:
                            state = os.waitid(os.P_PIDFD, descriptor, _WAIT_FLAGS)
                        except ChildProcessError:
                            # A stale /proc entry or recycled PID cannot authorize
                            # a signal to a process which is not our direct child.
                            continue
                        leader = process is not None and process.returncode is None and pid == process.pid
                        if state is None:
                            try:
                                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                            except ProcessLookupError:
                                continue  # Exited after waitid; reap on next pass.
                            if leader:
                                forced_leader = True
                            else:
                                forced_descendants = True
                        else:
                            reaped_pid, status = os.waitpid(pid, os.WNOHANG)
                            if reaped_pid != pid:
                                raise ProcessScopeError("gate_child_reap_raced")
                            if leader:
                                assert process is not None
                                process.returncode = os.waitstatus_to_exitcode(status)
                            else:
                                reaped_descendants += 1
                    finally:
                        os.close(descriptor)
                time.sleep(0.01)
        except ProcessScopeError as exc:
            faults.add(str(exc))
        except OSError:
            faults.add("gate_recursive_cleanup_failed")
        finally:
            if quiescent:
                try:
                    _subreaper(self.previous)
                    restored = _subreaper() == self.previous
                except ProcessScopeError:
                    faults.add("gate_subreaper_restore_failed")
            self.proof = CleanupProof(
                schema="friday.quality-gate-child-cleanup.v1",
                leader_returncode=process.returncode if process is not None else None,
                leader_reaped=process is not None and process.returncode is not None,
                reaped_descendants=reaped_descendants,
                forced_leader=forced_leader,
                forced_descendants=forced_descendants,
                kernel_echild=quiescent,
                subreaper_restored=restored,
                failure_codes=tuple(sorted(faults)),
            )
            self.entered = False
            _LOCK.release()
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        return self.proof

    def __exit__(self, *_exc: Any) -> None:
        self.close()
