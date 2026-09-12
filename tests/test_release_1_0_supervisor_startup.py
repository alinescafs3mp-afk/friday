"""Release 1.0 Supervisor startup-failure ownership oracles.

These tests use only exact local child commands.  They observe Supervisor-owned
processes, sessions, logs, and signal handlers before a test-only fallback
reclaims the same handles.  The fallback keeps a failing FIRST finite; it is not
evidence that Supervisor performed the cleanup.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from friday.supervisor import ChildSpec, Supervisor

_SIGNALS = (signal.SIGINT, signal.SIGTERM)
# Every fixture child has its own finite failsafe. The normal-stop child stays
# alive until Supervisor terminates it, so successful exit cannot race _on_exit
# into clearing the very handle whose cleanup the control must observe.
_PAUSE = "import signal\nsignal.alarm(30)\nsignal.pause()\n"
_STOP_PARENT = "import os, signal\nsignal.alarm(15)\nos.kill(os.getppid(), signal.SIGTERM)\nsignal.pause()\n"


def _python_spec(tmp_path: Path, name: str, body: str) -> ChildSpec:
    return ChildSpec(
        name=name,
        argv=[sys.executable, "-I", "-B", "-c", body],
        log_path=tmp_path / f"{name}.log",
        cwd=tmp_path,
    )


def _missing_spec(tmp_path: Path, name: str) -> ChildSpec:
    return ChildSpec(
        name=name,
        argv=[str(tmp_path / name)],
        log_path=tmp_path / f"{name}.log",
        cwd=tmp_path,
    )


def _signals() -> dict[int, Any]:
    return {sig: signal.getsignal(sig) for sig in _SIGNALS}


def _child_observation(child: Any) -> dict[str, Any]:
    process = child.process
    returncode = process.poll() if process is not None else None
    running = process is not None and returncode is None
    group_alive = False
    session_leader: bool | None = None
    if process is not None:
        try:
            pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            pass
        else:
            group_alive = True
            session_leader = pgid == process.pid
    handle = child.log_handle
    path = child.spec.log_path
    return {
        "name": child.spec.name,
        "argv": child.spec.argv,
        "process_present": process is not None,
        "running": running,
        "returncode": returncode,
        "group_alive": group_alive,
        "session_leader": session_leader,
        "log_exists": path.is_file(),
        "log_mode": stat.S_IMODE(path.stat().st_mode) if path.exists() else None,
        "log_closed": handle is None or handle.closed,
    }


def _observe(supervisor: Supervisor, original_signals: dict[int, Any]) -> dict[str, Any]:
    return {
        "signals_restored": {
            signal.Signals(sig).name: signal.getsignal(sig) == handler
            for sig, handler in original_signals.items()
        },
        "children": [_child_observation(child) for child in supervisor._children],
    }


def _fallback_cleanup(
    supervisor: Supervisor,
    original_signals: dict[int, Any],
) -> tuple[list[str], dict[str, Any]]:
    """Reclaim only Popen/log/signal handles owned by this exact fixture."""

    errors: list[str] = []
    for child in supervisor._children:
        process = child.process
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{child.spec.name}:process:{type(exc).__name__}")
        finally:
            handle = child.log_handle
            if handle is not None and not handle.closed:
                handle.close()
    for sig, handler in original_signals.items():
        if signal.getsignal(sig) != handler:
            signal.signal(sig, handler)
    return errors, _observe(supervisor, original_signals)


def _run_with_owned_fallback(supervisor: Supervisor) -> dict[str, Any]:
    original_signals = _signals()
    returned: int | None = None
    error: dict[str, Any] | None = None
    before_fallback: dict[str, Any] | None = None
    cleanup_errors: list[str] = []
    after_fallback: dict[str, Any] | None = None
    try:
        try:
            returned = supervisor.run()
        except Exception as exc:  # observation is the point of the refusal oracle
            error = {
                "type": type(exc).__name__,
                "filename": getattr(exc, "filename", None),
            }
        before_fallback = _observe(supervisor, original_signals)
    finally:
        cleanup_errors, after_fallback = _fallback_cleanup(supervisor, original_signals)
    return {
        "returned": returned,
        "error": error,
        "before_fallback": before_fallback,
        "cleanup_errors": cleanup_errors,
        "after_fallback": after_fallback,
    }


def _assert_fixture_reclaimed_everything(outcome: dict[str, Any]) -> None:
    assert outcome["cleanup_errors"] == []
    after = outcome["after_fallback"]
    assert after["signals_restored"] == {"SIGINT": True, "SIGTERM": True}
    assert all(not child["running"] for child in after["children"])
    assert all(not child["group_alive"] for child in after["children"])
    assert all(child["log_closed"] for child in after["children"])


def _assert_supervisor_left_no_owned_resource(observation: dict[str, Any]) -> None:
    assert observation["signals_restored"] == {"SIGINT": True, "SIGTERM": True}
    assert all(not child["running"] for child in observation["children"])
    assert all(not child["group_alive"] for child in observation["children"])
    assert all(child["log_closed"] for child in observation["children"])


def _assert_private_logs(observation: dict[str, Any]) -> None:
    assert all(child["log_exists"] for child in observation["children"])
    assert all(child["log_mode"] == 0o600 for child in observation["children"])


def test_supervisor_initial_popen_refusal_closes_log_and_restores_signals(tmp_path: Path) -> None:
    missing = _missing_spec(tmp_path, "missing-first")
    supervisor = Supervisor([missing], poll_interval_sec=0.001)
    outcome = _run_with_owned_fallback(supervisor)

    _assert_fixture_reclaimed_everything(outcome)
    assert outcome["returned"] is None
    assert outcome["error"] == {"type": "FileNotFoundError", "filename": missing.argv[0]}
    observed = outcome["before_fallback"]
    assert [child["name"] for child in observed["children"]] == ["missing-first"]
    assert observed["children"][0]["process_present"] is False
    _assert_private_logs(observed)
    _assert_supervisor_left_no_owned_resource(observed)


def test_supervisor_later_popen_refusal_reaps_started_child_and_both_logs(tmp_path: Path) -> None:
    steady = _python_spec(tmp_path, "steady", _PAUSE)
    missing = _missing_spec(tmp_path, "missing-second")
    supervisor = Supervisor([steady, missing], poll_interval_sec=0.001)
    outcome = _run_with_owned_fallback(supervisor)

    _assert_fixture_reclaimed_everything(outcome)
    assert outcome["returned"] is None
    assert outcome["error"] == {"type": "FileNotFoundError", "filename": missing.argv[0]}
    observed = outcome["before_fallback"]
    assert [child["name"] for child in observed["children"]] == ["steady", "missing-second"]
    assert observed["children"][0]["process_present"] is True
    assert observed["children"][1]["process_present"] is False
    _assert_private_logs(observed)
    _assert_supervisor_left_no_owned_resource(observed)


def test_supervisor_normal_stop_reaps_child_closes_log_and_restores_signals(tmp_path: Path) -> None:
    stopper = _python_spec(tmp_path, "stop-parent", _STOP_PARENT)
    supervisor = Supervisor(
        [stopper],
        max_rapid_crashes=1,
        poll_interval_sec=0.001,
    )
    outcome = _run_with_owned_fallback(supervisor)

    _assert_fixture_reclaimed_everything(outcome)
    assert outcome["error"] is None
    assert outcome["returned"] == 0
    observed = outcome["before_fallback"]
    assert [child["argv"] for child in observed["children"]] == [stopper.argv]
    assert observed["children"][0]["process_present"] is True
    _assert_private_logs(observed)
    _assert_supervisor_left_no_owned_resource(observed)


def test_supervisor_cleanup_error_still_reaps_all_children_and_restores_signals(
    tmp_path: Path, monkeypatch
) -> None:
    stopper = _python_spec(tmp_path, "cleanup-error-stop", _STOP_PARENT)
    steady = _python_spec(tmp_path, "cleanup-error-steady", _PAUSE)
    supervisor = Supervisor([stopper, steady], poll_interval_sec=0.001)
    original_killpg = os.killpg
    injected = False

    def signal_group_then_report_error(pgid, sent):
        nonlocal injected
        original_killpg(pgid, sent)
        if sent == signal.SIGTERM and not injected:
            injected = True
            raise OSError("synthetic cleanup refusal")

    monkeypatch.setattr(os, "killpg", signal_group_then_report_error)
    outcome = _run_with_owned_fallback(supervisor)

    _assert_fixture_reclaimed_everything(outcome)
    assert injected is True
    assert outcome["returned"] is None
    assert outcome["error"] == {"type": "OSError", "filename": None}
    observed = outcome["before_fallback"]
    assert [child["name"] for child in observed["children"]] == [
        "cleanup-error-stop",
        "cleanup-error-steady",
    ]
    _assert_private_logs(observed)
    _assert_supervisor_left_no_owned_resource(observed)


def _assert_owned_descendant_retired(tmp_path, monkeypatch, point, *, prove_identity_order=False):
    import json
    import select
    import time

    ready = tmp_path / "owned-grandchild-ready.json"
    # A real inherited-session grandchild ignores TERM. No shell, external
    # service, or user process participates; fallback signals an exact pidfd.
    grandchild_code = (
        "import json,os,signal,time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).write_text(json.dumps(dict(pid=os.getpid(), pgid=os.getpgrp(), sid=os.getsid(0))))\n"
        "time.sleep(120)\n"
    )
    leader_code = (
        "import subprocess,sys,time\n"
        "from pathlib import Path\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild_code!r}])\n"
        f"while not Path({str(ready)!r}).exists(): time.sleep(0.01)\n"
        + ("sys.exit(3)\n" if point == "leader-crash" else "time.sleep(120)\n")
    )
    leader = _python_spec(tmp_path, "owned-descendant-leader", leader_code)
    specs = [leader]
    if point == "later-refusal":
        specs.append(_missing_spec(tmp_path, "owned-descendant-missing"))
    supervisor = Supervisor(specs, max_rapid_crashes=1, poll_interval_sec=0.001)
    original_start = supervisor._start
    original_signals = {sig: signal.getsignal(sig) for sig in _SIGNALS}
    owned = {}
    identity_order = []

    def start_and_bind_descendant(child):
        original_start(child)
        if child.spec is not leader:
            return
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "owned descendant did not announce readiness"
        # The file may be observed between creation and its short write.
        while True:
            try:
                record = json.loads(ready.read_text())
                break
            except json.JSONDecodeError:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        assert record["pgid"] == record["sid"] == child.process.pid
        owned["pidfd"] = os.pidfd_open(record["pid"])
        poller = select.poll()
        poller.register(owned["pidfd"], select.POLLIN)
        owned["poller"] = poller
        assert not poller.poll(0), "descendant exited before product cleanup"
        if point == "normal-stop":
            supervisor.stop()

    monkeypatch.setattr(supervisor, "_start", start_and_bind_descendant)
    if prove_identity_order:
        original_killpg = os.killpg
        original_wait = subprocess.Popen.wait

        def signal_while_leader_is_waitable(pgid, sent):
            child = supervisor._children[0]
            process = child.process
            if process is not None and pgid == process.pid and sent == signal.SIGKILL:
                held = os.waitid(
                    os.P_PID,
                    process.pid,
                    os.WEXITED | os.WNOHANG | os.WNOWAIT,
                )
                assert held is not None, "group signal did not retain the exited leader"
                assert process.returncode is None
                identity_order.append("group-signal")
            return original_killpg(pgid, sent)

        def reap_only_after_retirement(process, *args, **kwargs):
            child = supervisor._children[0]
            if process is child.process and not identity_order.count("leader-reap"):
                assert child.group_retired is True
                assert identity_order == ["group-signal"]
                identity_order.append("leader-reap")
            return original_wait(process, *args, **kwargs)

        monkeypatch.setattr(os, "killpg", signal_while_leader_is_waitable)
        monkeypatch.setattr(subprocess.Popen, "wait", reap_only_after_retirement)
    try:
        code, failure = None, None
        try:
            code = supervisor.run()
        except FileNotFoundError as error:
            failure = error
        assert (failure is not None) == (point == "later-refusal")
        if failure is None:
            assert code == (1 if point == "leader-crash" else 0)
        assert "pidfd" in owned
        observed = _observe(supervisor, original_signals)
        descendant_terminal = bool(owned["poller"].poll(0))
        _assert_supervisor_left_no_owned_resource(observed)
        assert descendant_terminal, "owned descendant remains live after Supervisor returned"
        if prove_identity_order:
            assert identity_order == ["group-signal", "leader-reap"]
    finally:
        errors, _ = _fallback_cleanup(supervisor, original_signals)
        try:
            if "pidfd" in owned:
                if not owned["poller"].poll(0):
                    signal.pidfd_send_signal(owned["pidfd"], signal.SIGKILL)
                assert owned["poller"].poll(5000), "exact owned descendant cleanup unconfirmed"
        finally:
            if "pidfd" in owned:
                os.close(owned["pidfd"])
        assert not errors


def test_supervisor_normal_stop_retires_owned_descendant(tmp_path, monkeypatch):
    _assert_owned_descendant_retired(tmp_path, monkeypatch, "normal-stop")


def test_supervisor_later_refusal_retires_owned_descendant(tmp_path, monkeypatch):
    _assert_owned_descendant_retired(tmp_path, monkeypatch, "later-refusal")


def test_supervisor_holds_crashed_leader_identity_until_group_retirement(tmp_path, monkeypatch):
    _assert_owned_descendant_retired(
        tmp_path,
        monkeypatch,
        "leader-crash",
        prove_identity_order=True,
    )


def test_supervisor_stops_leader_with_non_utf8_linux_comm(tmp_path: Path) -> None:
    # Linux comm is a bounded byte string, not a UTF-8 protocol field. Keep the
    # exact leader waitable during cleanup so its /proc stat cannot disappear.
    set_comm = (
        "import ctypes\n"
        "libc = ctypes.CDLL(None)\n"
        "assert libc.prctl(15, ctypes.c_char_p(b'friday-\\xff'), 0, 0, 0) == 0\n"
    )
    stopper = _python_spec(tmp_path, "non-utf8-comm-stop", set_comm + _STOP_PARENT)
    supervisor = Supervisor([stopper], poll_interval_sec=0.001)
    outcome = _run_with_owned_fallback(supervisor)
    _assert_fixture_reclaimed_everything(outcome)
    assert outcome["error"] is None, outcome["error"]
    assert outcome["returned"] == 0
    _assert_supervisor_left_no_owned_resource(outcome["before_fallback"])
