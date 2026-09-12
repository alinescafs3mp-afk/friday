"""Real child lifecycle checks for the shared R10 process boundary; no models."""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time

import pytest

from tools import document_contour_live_battery as lifecycle

_UNBLOCK = "import signal;signal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGINT,signal.SIGTERM});"


@pytest.fixture
def fast_cleanup(monkeypatch):
    monkeypatch.setattr(lifecycle, "PROCESS_GROUP_EXIT_GRACE_SEC", 0.2)
    monkeypatch.setattr(lifecycle, "PROCESS_GROUP_TERM_GRACE_SEC", 0.05)
    monkeypatch.setattr(lifecycle, "PROCESS_GROUP_KILL_GRACE_SEC", 1.0)


@pytest.mark.parametrize("behavior", ["normal", "overflow", "stderr_overflow", "deadline"])
def test_bounded_child_reports_observed_output_deadline_and_cleanup(tmp_path, fast_cleanup, behavior):
    del fast_cleanup
    programs = {
        "normal": "print('actual-child-result')",
        "overflow": "import os;os.write(1,b'x'*262144)",
        "stderr_overflow": "import os;os.write(2,b'x'*262144)",
        "deadline": "import time;time.sleep(30)",
    }
    with (tmp_path / "private.log").open("wb") as log:
        started = time.monotonic()
        outcome = lifecycle._run_worker_process(
            [sys.executable, "-I", "-c", _UNBLOCK + programs[behavior]],
            environment={},
            private_log=log,
            timeout_sec=0.3,
            stdout_limit_bytes=1024,
        )
    assert time.monotonic() - started < 4
    assert outcome.worker_reaped is True and outcome.process_group_clear is True
    assert len(outcome.stdout) <= 1024
    if behavior == "normal":
        assert outcome.stdout == b"actual-child-result\n"
        assert outcome.returncode == 0 and not outcome.cleanup_failure_codes
    elif behavior in {"overflow", "stderr_overflow"}:
        assert "worker_stdout_oversized" in outcome.cleanup_failure_codes
        assert (tmp_path / "private.log").stat().st_size <= 1024
    else:
        assert outcome.timed_out is True
        assert "worker_timeout" in outcome.cleanup_failure_codes


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 1801])
def test_invalid_case_deadline_cannot_spawn(tmp_path, monkeypatch, timeout):
    def forbidden(*_args, **_kwargs):
        pytest.fail("spawn before deadline validation")

    monkeypatch.setattr(lifecycle.subprocess, "Popen", forbidden)
    with (
        (tmp_path / "private.log").open("wb") as log,
        pytest.raises(lifecycle.BatteryFailure, match="worker_timeout_invalid"),
    ):
        lifecycle._run_worker_process(["unused"], environment={}, private_log=log, timeout_sec=timeout)


def test_controller_cancel_reaps_the_bounded_reader_child(tmp_path, fast_cleanup):
    del fast_cleanup
    state = lifecycle._install_controller_signal_handlers()
    try:
        lifecycle._activate_controller_signal_handlers(state)
        program = _UNBLOCK + f"import os,time;os.kill({os.getpid()},signal.SIGTERM);time.sleep(30)"
        with (
            (tmp_path / "private.log").open("wb") as log,
            pytest.raises(lifecycle.ControllerSignal) as caught,
        ):
            lifecycle._run_worker_process(
                [sys.executable, "-I", "-c", program],
                environment={},
                private_log=log,
                controller_signal_handlers=state,
                timeout_sec=2,
                stdout_limit_bytes=1024,
            )
        assert caught.value.signal_number == signal.SIGTERM
        assert caught.value.worker_cleanup_clear is True
        assert state.worker_cleanup_clear is True
    finally:
        lifecycle._finalize_controller_signal_handlers(state, lambda: None)


def test_detached_pipe_holder_keeps_controller_cleanup_uncertain(tmp_path, fast_cleanup):
    """An escaped grandchild is alive despite a reaped leader and empty PGID."""
    del fast_cleanup
    pid_path = tmp_path / "detached.pid"
    state = lifecycle._install_controller_signal_handlers()
    child = "import signal;signal.pause()"
    program = _UNBLOCK + (
        "import subprocess,pathlib;"
        f"p=subprocess.Popen([{sys.executable!r},'-I','-c',{child!r}],start_new_session=True);"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(p.pid))"
    )
    try:
        lifecycle._activate_controller_signal_handlers(state)
        with (tmp_path / "process.log").open("wb") as log:
            outcome = lifecycle._run_worker_process(
                [sys.executable, "-I", "-c", program],
                environment={},
                private_log=log,
                controller_signal_handlers=state,
                timeout_sec=2,
                stdout_limit_bytes=1024,
            )
        os.kill(int(pid_path.read_text()), 0)
        assert outcome.worker_reaped and outcome.process_group_clear
        assert "worker_stdout_reader_not_clear" in outcome.cleanup_failure_codes
        assert state.worker_cleanup_clear is False
    finally:
        if pid_path.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_path.read_text()), signal.SIGKILL)
        lifecycle._finalize_controller_signal_handlers(state, lambda: None)
