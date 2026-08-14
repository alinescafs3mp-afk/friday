"""Offline contract tests for the one-shot document contour operator."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import tools.document_contour_release_operator as operator

COMMIT = "a" * 40


def _canonical(payload: dict[str, Any]) -> bytes:
    return operator._canonical_json(payload) + b"\n"


def _private_json(path: Path, payload: dict[str, Any]) -> bytes:
    encoded = _canonical(payload)
    path.write_bytes(encoded)
    path.chmod(0o600)
    return encoded


def _receipt(
    run_index: int,
    *,
    run_hash: str = "b" * 64,
    worker_hash: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": operator.RUN_RECEIPT_SCHEMA,
        "commit": COMMIT,
        "run_id_hash": run_hash,
        "run_index": run_index,
        "worker_report_sha256": worker_hash or ("c" if run_index == 1 else "d") * 64,
        "worker_status": "passed",
        "worker_exit_code": 0,
        "worker_reaped": True,
        "process_group_clear_initial": True,
        "process_group_clear": True,
        "process_cleanup_failure_codes": [],
        "lifecycle_contract_clear": True,
        "lifecycle_teardown_clear": True,
        "lifecycle_failure_codes": [],
        "teardown_clear": True,
    }


def _case_report(case_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "case_id": case_id,
        "status": "passed",
        "failure_codes": [],
        "duration_ms": 1,
        "checks": {"clear": True},
        "counters": {},
        "fresh_database": True,
    }
    if case_id == "D10":
        attempts = {
            "llm_chat_attempts": 0,
            "late_make_file_attempts": 0,
            "workspace_create_kernel_attempts": 0,
            "workspace_create_mcp_attempts": 0,
        }
        result["diagnostics"] = {
            "subturns": {
                "metadata": {
                    "duration_ms": 1,
                    "http_returned": True,
                    "llm_failed": False,
                    "files_count": 0,
                    "tools_count": 0,
                    "attempts": attempts,
                },
                "regular": {
                    "duration_ms": 1,
                    "http_returned": True,
                    "llm_failed": False,
                    "files_count": 1,
                    "tools_count": 0,
                    "attempts": attempts,
                    "reply_ref_bound_before": True,
                },
                "mcp": {
                    "duration_ms": 1,
                    "http_returned": True,
                    "llm_failed": False,
                    "files_count": 0,
                    "tools_count": 1,
                    "attempts": attempts,
                    "reply_ref_bound_before": True,
                },
            }
        }
    return result


def _worker_report(run_index: int) -> dict[str, Any]:
    return {
        "schema": operator.BATTERY_WORKER_SCHEMA,
        "run_index": run_index,
        "run_id_hash": "b" * 64,
        "status": "passed",
        "failure_codes": [],
        "lifecycle_teardown_clear": True,
        "lifecycle_failure_codes": [],
        "duration_ms": 1,
        "cases": [_case_report(case_id) for case_id in operator.BATTERY_CASE_IDS],
        "teardown": {
            "worker_report_identity_clear": True,
            "worker_exit_code": 0,
            "worker_reaped": True,
            "process_group_clear_initial": True,
            "process_group_clear": True,
            "process_cleanup_failure_codes": [],
            "lifecycle_contract_clear": True,
            "lifecycle_teardown_clear": True,
            "lifecycle_failure_codes": [],
            "teardown_clear": True,
        },
    }


def _request(receipt: dict[str, Any], receipt_bytes: bytes) -> dict[str, Any]:
    return {
        "schema": operator.OBSERVER_REQUEST_SCHEMA,
        "commit": COMMIT,
        "run_id_hash": receipt["run_id_hash"],
        "run_index": 1,
        "run_receipt_sha256": operator._sha256(receipt_bytes),
        "worker_report_sha256": receipt["worker_report_sha256"],
        "challenge": "e" * 64,
    }


def _active_snapshot(pid: int) -> dict[str, Any]:
    return {
        "schema": operator.OBSERVER_SNAPSHOT_SCHEMA,
        "backend_pid": pid,
        "backend_lease_owned": True,
        "physical_outbound_pending": 0,
        "bridge_queue_state": "active_uninspected",
        "bridge_lease_acquired_for_snapshot": False,
        "bridge_lease_released": False,
        "inbound_pending": None,
        "dead_letter": None,
    }


def _stopped_snapshot(pid: int) -> dict[str, Any]:
    return {
        "schema": operator.OBSERVER_SNAPSHOT_SCHEMA,
        "backend_pid": pid,
        "backend_lease_owned": True,
        "physical_outbound_pending": 0,
        "bridge_queue_state": "present",
        "bridge_lease_acquired_for_snapshot": True,
        "bridge_lease_released": True,
        "inbound_pending": 0,
        "dead_letter": 0,
    }


def _guarded_snapshot() -> dict[str, Any]:
    return {
        "schema": operator.GUARDED_QUEUE_SCHEMA,
        "bridge_guard_held": True,
        "bridge_queue_state": "present",
        "inbound_pending": 0,
        "dead_letter": 0,
    }


BACKEND = operator.ServiceFingerprint(
    unit_id="friday-backend.service",
    main_pid=41001,
    invocation_id="1" * 32,
    nrestarts=0,
    exec_started_monotonic=100,
    control_group="/user.slice/backend",
    process_start_ticks=101,
    boot_id="11111111-1111-1111-1111-111111111111",
)
BRIDGE = operator.ServiceFingerprint(
    unit_id="friday-bridge.service",
    main_pid=41002,
    invocation_id="2" * 32,
    nrestarts=0,
    exec_started_monotonic=200,
    control_group="/user.slice/bridge",
    process_start_ticks=202,
    boot_id=BACKEND.boot_id,
)


class FakeGuard:
    acquired = True


class FakeChild:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = b""
        self.request_written = False
        self.finished = False
        self.run_reports: list[dict[str, Any]] = []


class FakeRuntime:
    def __init__(
        self,
        barrier_path: Path,
        *,
        fail_method: str = "",
        fail_call: int = 1,
        failure: BaseException | None = None,
        start_online: bool = True,
    ) -> None:
        self.barrier_path = barrier_path
        self.fail_method = fail_method
        self.fail_call = fail_call
        self.failure = failure or operator.OperatorFailure("injected_failure")
        self.start_online = start_online
        self.calls: dict[str, int] = {}
        self.events: list[str] = []
        self.now = 0.0
        self.bridge_running = True
        self.guard: FakeGuard | None = None
        self.child: FakeChild | None = None
        self.released = False
        self.start_calls = 0
        self.closed = False

    def _record(self, name: str) -> None:
        self.events.append(name)
        self.calls[name] = self.calls.get(name, 0) + 1
        if name == self.fail_method and self.calls[name] == self.fail_call:
            raise self.failure

    def monotonic(self) -> float:
        self.now += 0.001
        return self.now

    def pause(self, seconds: float) -> None:
        self.now += seconds

    def revalidate_environment(self) -> None:
        self._record("env")

    def backend_identity(self) -> operator.ServiceFingerprint:
        self._record("backend")
        return BACKEND

    def backend_identity_alive(self, expected: operator.ServiceFingerprint) -> bool:
        self._record("backend_alive")
        return expected == BACKEND

    def bridge_running_identity(self) -> operator.ServiceFingerprint:
        self._record("bridge_running")
        return BRIDGE

    def pre_stop_bridge_lease_matches(self, pid: int) -> bool:
        self._record("bridge_lease_match")
        return pid == BRIDGE.main_pid

    def health(self) -> dict[str, Any]:
        self._record("health")
        return {"status": "ok"}

    def observer_snapshot(self) -> dict[str, Any]:
        self._record("snapshot")
        return (
            _active_snapshot(BACKEND.main_pid)
            if self.bridge_running or self.guard
            else _stopped_snapshot(BACKEND.main_pid)
        )

    def dispatcher_epoch(self) -> str:
        self._record("epoch")
        return "9" * 64

    def stop_bridge(self) -> None:
        self._record("stop")
        self.bridge_running = False

    def bridge_inactive(self, previous: operator.ServiceFingerprint) -> bool:
        self._record("inactive")
        return previous == BRIDGE and not self.bridge_running

    def acquire_guard(self, owner: operator.ExecutionState) -> FakeGuard:
        self._record("acquire")
        self.guard = FakeGuard()
        owner.guard = self.guard
        return self.guard

    def guard_held(self, boundary: Any) -> bool:
        self._record("guard_held")
        return boundary is self.guard and bool(getattr(boundary, "acquired", False))

    def guarded_queue_snapshot(self, boundary: Any) -> dict[str, Any]:
        self._record("queue")
        assert boundary is self.guard
        return _guarded_snapshot()

    def spawn_battery(
        self,
        config: operator.OperatorConfig,
        owner: operator.ExecutionState,
    ) -> FakeChild:
        self._record("spawn")
        assert config.freeze_commit == COMMIT
        assert self.guard is not None and self.guard.acquired
        self.child = FakeChild()
        owner.child = self.child
        return self.child

    def _write_request(self, child: FakeChild) -> None:
        first_report = _worker_report(1)
        child.run_reports.append(first_report)
        first = _receipt(1, worker_hash=operator._sha256(operator._canonical_json(first_report)))
        first_bytes = _private_json(self.barrier_path / "run-1-receipt.json", first)
        _private_json(
            self.barrier_path / "run-1-observer-request.json",
            _request(first, first_bytes),
        )

    def _finish_successfully(self, child: FakeChild) -> None:
        second_report = _worker_report(2)
        child.run_reports.append(second_report)
        second = _receipt(2, worker_hash=operator._sha256(operator._canonical_json(second_report)))
        second_bytes = _private_json(self.barrier_path / "run-2-receipt.json", second)
        first_bytes = (self.barrier_path / "run-1-receipt.json").read_bytes()
        first = json.loads(first_bytes.decode("utf-8"))
        response_bytes = (self.barrier_path / "run-1-observer.json").read_bytes()
        response_sha = operator._sha256(response_bytes)
        report = {
            "schema": operator.BATTERY_REPORT_SCHEMA,
            "commit": COMMIT,
            "run_id_hash": "b" * 64,
            "status": "passed",
            "runs_expected": 2,
            "runs_completed": 2,
            "cases_expected_per_run": len(operator.BATTERY_CASE_IDS),
            "failure_codes": [],
            "run_receipts": [
                {
                    "run_index": 1,
                    "sha256": operator._sha256(first_bytes),
                    "worker_report_sha256": first["worker_report_sha256"],
                    "teardown_clear": True,
                },
                {
                    "run_index": 2,
                    "sha256": operator._sha256(second_bytes),
                    "worker_report_sha256": second["worker_report_sha256"],
                    "teardown_clear": True,
                },
            ],
            "inter_run_observer": {
                "schema": operator.OBSERVER_RESPONSE_SCHEMA,
                "status": "passed",
                "run_index": 1,
                "run_receipt_sha256": operator._sha256(first_bytes),
                "worker_report_sha256": first["worker_report_sha256"],
                "response_sha256": response_sha,
                **{key: True for key in operator._OBSERVER_BOOLEAN_FIELDS},
            },
            "runs": child.run_reports,
        }
        child.stdout = json.dumps(report, ensure_ascii=False, sort_keys=True).encode() + b"\n"
        child.returncode = 0

    def poll_child(self, child: FakeChild) -> int | None:
        self._record("poll")
        if not child.request_written:
            self._write_request(child)
            child.request_written = True
        elif (self.barrier_path / "run-1-observer.json").exists() and child.returncode is None:
            self._finish_successfully(child)
        return child.returncode

    def child_contour_alive(self, child: FakeChild) -> bool:
        self._record("child_alive")
        return child.returncode is None and not child.finished

    def finish_child(self, child: FakeChild) -> operator.BatteryOutcome:
        self._record("finish")
        assert child.returncode is not None
        child.finished = True
        return operator.BatteryOutcome(child.returncode, child.stdout, True, False)

    def cleanup_child(self, child: FakeChild) -> operator.BatteryOutcome:
        self._record("cleanup_child")
        child.returncode = 143
        child.finished = True
        return operator.BatteryOutcome(143, b"", True, True)

    def release_guard(self, boundary: Any) -> None:
        self._record("release")
        assert boundary is self.guard
        boundary.acquired = False
        self.released = True

    def start_bridge_once(self) -> bool:
        self._record("start")
        self.start_calls += 1
        assert self.released or self.guard is None
        self.bridge_running = self.start_online
        return self.start_online

    def close(self) -> None:
        self._record("close")
        self.closed = True


def _config(tmp_path: Path) -> operator.OperatorConfig:
    return operator.OperatorConfig(
        freeze_commit=COMMIT,
        env_file=tmp_path / "env",
        barrier_dir=tmp_path / "barrier",
        backend_unit="friday-backend.service",
        bridge_unit="friday-bridge.service",
    )


def _barrier(tmp_path: Path) -> operator.PinnedBarrier:
    path = tmp_path / "barrier"
    path.mkdir(mode=0o700)
    return operator.PinnedBarrier(path)


def test_golden_contour_holds_guard_through_both_runs_and_restarts_once(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(config.barrier_dir)
    barrier = _barrier(tmp_path)
    try:
        report, exit_code = operator.execute_operator(config, runtime, barrier)
    finally:
        barrier.close()

    assert exit_code == 0
    assert report["status"] == "passed"
    assert report["failure_codes"] == []
    assert all(report["checks"].values())
    assert runtime.start_calls == 1
    assert runtime.released is True
    assert runtime.closed is True
    assert runtime.events.index("acquire") < runtime.events.index("spawn")
    assert runtime.events.index("finish") < runtime.events.index("release")
    assert runtime.events.index("release") < runtime.events.index("start")
    response = json.loads((config.barrier_dir / "run-1-observer.json").read_text())
    assert response == {
        "schema": operator.OBSERVER_RESPONSE_SCHEMA,
        "commit": COMMIT,
        "run_id_hash": "b" * 64,
        "run_index": 1,
        "run_receipt_sha256": report["evidence_sha256"]["run_1_receipt_sha256"],
        "worker_report_sha256": operator._sha256(operator._canonical_json(_worker_report(1))),
        "challenge": "e" * 64,
        "status": "passed",
        "bridge_stopped": True,
        "bridge_operator_guard_held": True,
        "backend_healthy": True,
        "backend_unchanged": True,
        "outbound_pending_zero": True,
        "inbound_pending_zero": True,
        "dead_letter_zero": True,
        "dispatcher_unchanged": True,
    }
    assert set(report["evidence_sha256"]) == {
        "battery_report_sha256",
        "observer_request_sha256",
        "observer_response_sha256",
        "run_1_receipt_sha256",
        "run_2_receipt_sha256",
    }


def test_battery_report_projection_is_exact_and_bound_to_receipts(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(config.barrier_dir)
    barrier = _barrier(tmp_path)
    try:
        report, exit_code = operator.execute_operator(config, runtime, barrier)
    finally:
        barrier.close()
    assert exit_code == 0
    assert runtime.child is not None
    payload = json.loads(runtime.child.stdout.decode("utf-8"))
    receipt_bytes = {
        index: (config.barrier_dir / f"run-{index}-receipt.json").read_bytes() for index in (1, 2)
    }
    receipts = {index: json.loads(encoded.decode("utf-8")) for index, encoded in receipt_bytes.items()}
    arguments = {
        "commit": COMMIT,
        "response_sha256": report["evidence_sha256"]["observer_response_sha256"],
        "receipt_hashes": {index: operator._sha256(encoded) for index, encoded in receipt_bytes.items()},
        "receipt_payloads": receipts,
    }
    operator._validate_battery_report(payload, **arguments)

    mutations: list[dict[str, Any]] = []
    extra = json.loads(json.dumps(payload))
    extra["private_run_dir"] = "/private/path"
    mutations.append(extra)
    observer_false = json.loads(json.dumps(payload))
    observer_false["inter_run_observer"]["bridge_operator_guard_held"] = False
    mutations.append(observer_false)
    receipt_substitution = json.loads(json.dumps(payload))
    receipt_substitution["run_receipts"][1]["worker_report_sha256"] = "f" * 64
    mutations.append(receipt_substitution)
    case_substitution = json.loads(json.dumps(payload))
    case_substitution["runs"][0]["cases"][0]["checks"]["clear"] = False
    mutations.append(case_substitution)
    for mutation in mutations:
        with pytest.raises(operator.OperatorFailure):
            operator._validate_battery_report(mutation, **arguments)


def test_failure_before_stop_never_starts_or_stops_the_bridge(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(config.barrier_dir, fail_method="health")
    barrier = _barrier(tmp_path)
    try:
        report, exit_code = operator.execute_operator(config, runtime, barrier)
    finally:
        barrier.close()

    assert exit_code == 1
    assert report["status"] == "failed"
    assert report["failure_codes"] == ["injected_failure"]
    assert "stop" not in runtime.events
    assert runtime.start_calls == 0


@pytest.mark.parametrize(
    ("method", "call_number"),
    (
        ("stop", 1),
        ("inactive", 1),
        ("snapshot", 2),
        ("epoch", 2),
        ("acquire", 1),
        ("queue", 1),
        ("spawn", 1),
        ("poll", 1),
        ("child_alive", 1),
        ("health", 3),
        ("finish", 1),
    ),
)
def test_every_failure_after_stop_armed_restarts_exactly_once(
    tmp_path,
    method,
    call_number,
) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(
        config.barrier_dir,
        fail_method=method,
        fail_call=call_number,
    )
    barrier = _barrier(tmp_path)
    try:
        report, exit_code = operator.execute_operator(config, runtime, barrier)
    finally:
        barrier.close()

    assert exit_code == 1
    assert report["status"] == "failed"
    assert runtime.events.count("start") == 1
    assert runtime.start_calls == 1
    if runtime.child is not None and "finish" not in runtime.events:
        assert runtime.events.count("cleanup_child") == 1
    if runtime.guard is not None:
        assert runtime.events.index("release") < runtime.events.index("start")


@pytest.mark.parametrize(
    "failure",
    (
        KeyboardInterrupt(),
        SystemExit(9),
        operator.OperatorSignal(int(signal.SIGINT)),
        operator.OperatorSignal(int(signal.SIGTERM)),
    ),
)
def test_baseexceptions_after_stop_use_the_same_one_start_finalizer(tmp_path, failure) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(
        config.barrier_dir,
        fail_method="queue",
        fail_call=1,
        failure=failure,
    )
    barrier = _barrier(tmp_path)
    try:
        report, exit_code = operator.execute_operator(config, runtime, barrier)
    finally:
        barrier.close()

    assert runtime.start_calls == 1
    assert report["status"] == "failed"
    if isinstance(failure, operator.OperatorSignal):
        assert exit_code == 128 + failure.signal_number
    else:
        assert exit_code == 1


def test_signal_arriving_during_blocked_cleanup_is_projected_after_one_start(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(config.barrier_dir)
    original_close = runtime.close

    def close_and_signal() -> None:
        original_close()
        signal.pthread_kill(threading.get_ident(), signal.SIGTERM)

    runtime.close = close_and_signal  # type: ignore[method-assign]
    barrier = _barrier(tmp_path)
    signal_state = operator._install_signal_handlers()
    operator._activate_signal_handlers(signal_state)
    try:
        report, exit_code = operator.execute_operator(
            config,
            runtime,
            barrier,
            signal_state=signal_state,
        )
    finally:
        # Idempotently restore the test runner's dispositions even if an
        # assertion or implementation regression interrupts the call above.
        operator._finalize_signal_handlers(signal_state, lambda: None)
        barrier.close()

    assert exit_code == 128 + signal.SIGTERM
    assert report["status"] == "failed"
    assert report["signal"] == "SIGTERM"
    assert report["failure_codes"] == ["interrupted_sigterm"]
    assert runtime.start_calls == 1
    assert runtime.bridge_running is True


def test_signal_at_successful_workflow_return_cannot_skip_restoration(tmp_path, monkeypatch) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(config.barrier_dir)
    barrier = _barrier(tmp_path)
    signal_state = operator._install_signal_handlers()
    operator._activate_signal_handlers(signal_state)
    original_block = operator._block_control_signals
    calls = 0

    def block_with_boundary_signal() -> frozenset[Any]:
        nonlocal calls
        calls += 1
        previous = original_block()
        # Call 1 binds the battery child; call 2 is the successful workflow
        # return boundary immediately before the outer finalizer.
        if calls == 2:
            signal_state.first_signal = int(signal.SIGINT)
            raise operator.OperatorSignal(int(signal.SIGINT))
        return previous

    monkeypatch.setattr(operator, "_block_control_signals", block_with_boundary_signal)
    try:
        report, exit_code = operator.execute_operator(
            config,
            runtime,
            barrier,
            signal_state=signal_state,
        )
    finally:
        operator._finalize_signal_handlers(signal_state, lambda: None)
        barrier.close()

    assert exit_code == 128 + signal.SIGINT
    assert report["signal"] == "SIGINT"
    assert report["failure_codes"] == ["interrupted_sigint"]
    assert runtime.start_calls == 1
    assert runtime.bridge_running is True


def test_signal_after_guard_acquisition_releases_exact_guard_before_one_start(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(config.barrier_dir)
    original_acquire = runtime.acquire_guard

    def acquire_and_signal(owner: operator.ExecutionState) -> FakeGuard:
        boundary = original_acquire(owner)
        signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
        return boundary

    runtime.acquire_guard = acquire_and_signal  # type: ignore[method-assign]
    barrier = _barrier(tmp_path)
    signal_state = operator._install_signal_handlers()
    operator._activate_signal_handlers(signal_state)
    try:
        report, exit_code = operator.execute_operator(
            config,
            runtime,
            barrier,
            signal_state=signal_state,
        )
    finally:
        operator._finalize_signal_handlers(signal_state, lambda: None)
        barrier.close()

    assert exit_code == 128 + signal.SIGTERM
    assert report["failure_codes"] == ["interrupted_sigterm"]
    assert runtime.released is True
    assert runtime.start_calls == 1
    assert runtime.events.index("release") < runtime.events.index("start")


def test_signal_after_child_spawn_handoff_cleans_whole_contour_before_start(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(config.barrier_dir)
    original_spawn = runtime.spawn_battery

    def spawn_and_signal(
        child_config: operator.OperatorConfig,
        owner: operator.ExecutionState,
    ) -> FakeChild:
        child = original_spawn(child_config, owner)
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)
        return child

    runtime.spawn_battery = spawn_and_signal  # type: ignore[method-assign]
    barrier = _barrier(tmp_path)
    signal_state = operator._install_signal_handlers()
    operator._activate_signal_handlers(signal_state)
    try:
        report, exit_code = operator.execute_operator(
            config,
            runtime,
            barrier,
            signal_state=signal_state,
        )
    finally:
        operator._finalize_signal_handlers(signal_state, lambda: None)
        barrier.close()

    assert exit_code == 128 + signal.SIGINT
    assert "interrupted_sigint" in report["failure_codes"]
    assert runtime.events.count("cleanup_child") == 1
    assert runtime.events.index("cleanup_child") < runtime.events.index("release")
    assert runtime.events.index("release") < runtime.events.index("start")
    assert runtime.start_calls == 1


def test_signal_handlers_normalize_an_inherited_control_signal_mask() -> None:
    original_mask = frozenset(signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM}))
    signal_state: operator.SignalHandlers | None = None
    try:
        signal_state = operator._install_signal_handlers()
        operator._activate_signal_handlers(signal_state)
        active_mask = frozenset(signal.pthread_sigmask(signal.SIG_BLOCK, frozenset()))
        assert signal.SIGTERM not in active_mask
    finally:
        if signal_state is not None:
            operator._finalize_signal_handlers(signal_state, lambda: None)
        signal.pthread_sigmask(signal.SIG_SETMASK, original_mask)


def test_repeat_signal_drain_is_nonblocking_and_strictly_bounded(monkeypatch) -> None:
    waits: list[tuple[frozenset[Any], float]] = []
    monkeypatch.setattr(operator.signal, "sigpending", lambda: {signal.SIGTERM})

    def timed_wait(pending, timeout):
        waits.append((frozenset(pending), timeout))
        return SimpleNamespace(si_signo=signal.SIGTERM)

    monkeypatch.setattr(operator.signal, "sigtimedwait", timed_wait)
    operator._drain_pending_control_signals()
    assert waits == [(frozenset({signal.SIGTERM}), 0) for _ in range(operator.MAX_SIGNAL_DRAIN_ATTEMPTS)]


def _patch_main_preflight(monkeypatch, tmp_path):
    config = _config(tmp_path)
    arguments = SimpleNamespace(freeze_commit=COMMIT)
    parser = SimpleNamespace(parse_args=lambda _argv: arguments)
    pinned = SimpleNamespace(close=lambda: None)
    barrier = SimpleNamespace(close=lambda: None)
    runtime = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(operator, "build_parser", lambda: parser)
    monkeypatch.setattr(operator, "_config_from_args", lambda _args: config)
    monkeypatch.setattr(operator, "_validate_candidate", lambda _commit: None)
    monkeypatch.setattr(operator, "PinnedPrivateFile", lambda *_args, **_kwargs: pinned)
    monkeypatch.setattr(operator, "PinnedBarrier", lambda _path: barrier)
    monkeypatch.setattr(operator, "_build_runtime", lambda _config, _pinned: runtime)
    return config


def test_main_projects_a_signal_that_interrupts_handler_activation(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _patch_main_preflight(monkeypatch, tmp_path)
    signal_state = operator.SignalHandlers(
        previous={},
        previous_mask=frozenset(),
        first_signal=int(signal.SIGTERM),
    )
    monkeypatch.setattr(operator, "_install_signal_handlers", lambda: signal_state)
    monkeypatch.setattr(
        operator,
        "_activate_signal_handlers",
        lambda _state: (_ for _ in ()).throw(operator.OperatorSignal(int(signal.SIGTERM))),
    )
    monkeypatch.setattr(
        operator,
        "_finalize_signal_handlers",
        lambda _state, cleanup: cleanup(),
    )

    assert operator.main([]) == 128 + signal.SIGTERM
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["signal"] == "SIGTERM"
    assert report["failure_codes"] == ["interrupted_sigterm"]


def test_main_projects_a_signal_first_seen_by_the_outer_final_drain(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    _patch_main_preflight(monkeypatch, tmp_path)
    signal_state = operator.SignalHandlers(previous={}, previous_mask=frozenset())
    monkeypatch.setattr(operator, "_install_signal_handlers", lambda: signal_state)
    monkeypatch.setattr(operator, "_activate_signal_handlers", lambda _state: None)
    monkeypatch.setattr(
        operator,
        "execute_operator",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    def finalize(_state, cleanup):
        cleanup()
        signal_state.first_signal = int(signal.SIGINT)

    monkeypatch.setattr(operator, "_finalize_signal_handlers", finalize)

    assert operator.main([]) == 128 + signal.SIGINT
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["signal"] == "SIGINT"
    assert report["failure_codes"] == ["interrupted_sigint", "operator_baseexception"]


def test_start_failure_is_not_retried(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(config.barrier_dir, start_online=False)
    barrier = _barrier(tmp_path)
    try:
        report, exit_code = operator.execute_operator(config, runtime, barrier)
    finally:
        barrier.close()

    assert exit_code == 1
    assert runtime.start_calls == 1
    assert runtime.events.count("start") == 1
    assert "bridge_start_not_confirmed" in report["failure_codes"]
    assert report["checks"]["bridge_online_after"] is False


def test_backend_identity_is_rechecked_after_bridge_restoration(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(config.barrier_dir)
    original_alive = runtime.backend_identity_alive

    def changed_after_start(expected: operator.ServiceFingerprint) -> bool:
        if runtime.start_calls:
            runtime._record("backend_alive")
            return False
        return original_alive(expected)

    runtime.backend_identity_alive = changed_after_start  # type: ignore[method-assign]
    barrier = _barrier(tmp_path)
    try:
        report, exit_code = operator.execute_operator(config, runtime, barrier)
    finally:
        barrier.close()

    assert exit_code == 1
    assert runtime.start_calls == 1
    assert runtime.bridge_running is True
    assert report["checks"]["bridge_online_after"] is True
    assert report["checks"]["backend_unchanged"] is False
    assert "backend_identity_changed" in report["failure_codes"]


def test_cleanup_failure_cannot_skip_guard_release_or_duplicate_start(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(
        config.barrier_dir,
        fail_method="cleanup_child",
        failure=RuntimeError("private cleanup detail"),
    )
    runtime.fail_method = "poll"
    runtime.fail_call = 1
    runtime.failure = operator.OperatorFailure("poll_failed")

    original_cleanup = runtime.cleanup_child

    def broken_cleanup(child):
        runtime.events.append("cleanup_child")
        raise RuntimeError("private cleanup detail")

    runtime.cleanup_child = broken_cleanup  # type: ignore[method-assign]
    barrier = _barrier(tmp_path)
    try:
        report, _exit_code = operator.execute_operator(config, runtime, barrier)
    finally:
        runtime.cleanup_child = original_cleanup  # type: ignore[method-assign]
        barrier.close()

    assert "battery_cleanup_exception" in report["failure_codes"]
    assert runtime.events.count("release") == 1
    assert runtime.events.count("start") == 1


def test_sanitized_report_never_contains_private_values(tmp_path) -> None:
    config = _config(tmp_path)
    runtime = FakeRuntime(
        config.barrier_dir,
        fail_method="health",
        failure=RuntimeError("TOKEN-PRIVATE https://127.0.0.1:8000 /secret/env 41001 challenge-PRIVATE"),
    )
    barrier = _barrier(tmp_path)
    try:
        report, _exit_code = operator.execute_operator(config, runtime, barrier)
    finally:
        barrier.close()
    encoded = json.dumps(report, sort_keys=True)
    for forbidden in (
        "TOKEN-PRIVATE",
        "127.0.0.1",
        "/secret/env",
        "41001",
        "challenge-PRIVATE",
        "RuntimeError",
    ):
        assert forbidden not in encoded


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        ({"worker_status": "failed"}, "run_receipt_invalid"),
        ({"worker_exit_code": True}, "run_receipt_invalid"),
        ({"worker_reaped": False}, "run_receipt_not_clear"),
        ({"process_cleanup_failure_codes": ["worker_timeout"]}, "run_receipt_not_clear"),
        ({"lifecycle_failure_codes": ["private-detail"]}, "run_receipt_invalid"),
        ({"teardown_clear": False}, "run_receipt_not_clear"),
        ({"extra": True}, "run_receipt_invalid"),
    ),
)
def test_receipt_validator_is_exact_and_fail_closed(mutation, code) -> None:
    payload = {**_receipt(1), **mutation}
    with pytest.raises(operator.OperatorFailure, match=code):
        operator._validate_run_receipt(payload, commit=COMMIT, run_index=1)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("commit", "f" * 40),
        ("run_id_hash", "0" * 64),
        ("run_index", 2),
        ("run_receipt_sha256", "1" * 64),
        ("worker_report_sha256", "2" * 64),
        ("challenge", "3" * 63),
    ),
)
def test_request_must_bind_the_exact_canonical_receipt(field, replacement) -> None:
    receipt = _receipt(1)
    receipt_bytes = _canonical(receipt)
    request = _request(receipt, receipt_bytes)
    request[field] = replacement
    with pytest.raises(operator.OperatorFailure, match="observer_request_invalid"):
        operator._validate_observer_request(request, receipt, receipt_bytes, commit=COMMIT)


@pytest.mark.parametrize(
    "projection",
    (
        {**_stopped_snapshot(BACKEND.main_pid), "physical_outbound_pending": 1},
        {**_stopped_snapshot(BACKEND.main_pid), "inbound_pending": 1},
        {**_stopped_snapshot(BACKEND.main_pid), "dead_letter": 1},
        {**_stopped_snapshot(BACKEND.main_pid), "bridge_lease_released": False},
        {**_stopped_snapshot(BACKEND.main_pid), "backend_pid": BACKEND.main_pid + 1},
        {**_stopped_snapshot(BACKEND.main_pid), "private": "body"},
    ),
)
def test_stopped_snapshot_never_projects_uncertain_or_nonzero_state(projection) -> None:
    with pytest.raises(operator.OperatorFailure):
        operator._validate_stopped_snapshot(projection, BACKEND.main_pid)


@pytest.mark.parametrize(
    "projection",
    (
        {**_active_snapshot(BACKEND.main_pid), "physical_outbound_pending": 1},
        {**_active_snapshot(BACKEND.main_pid), "inbound_pending": 0},
        {**_active_snapshot(BACKEND.main_pid), "bridge_queue_state": "lease_unavailable"},
        {**_active_snapshot(BACKEND.main_pid), "bridge_lease_acquired_for_snapshot": True},
    ),
)
def test_held_snapshot_requires_the_external_guard_projection(projection) -> None:
    with pytest.raises(operator.OperatorFailure):
        operator._validate_held_snapshot(projection, BACKEND.main_pid)


@pytest.mark.parametrize(
    "projection",
    (
        {**_guarded_snapshot(), "inbound_pending": 1},
        {**_guarded_snapshot(), "dead_letter": 1},
        {**_guarded_snapshot(), "bridge_guard_held": False},
        {**_guarded_snapshot(), "bridge_queue_state": "absent"},
        {**_guarded_snapshot(), "payload_json": "private"},
    ),
)
def test_guarded_queue_projection_is_exact_and_zero(projection) -> None:
    with pytest.raises(operator.OperatorFailure):
        operator._validate_guarded_queue(projection)


def test_live_guarded_read_uses_only_the_public_descriptor_bound_collector(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    runtime.settings = SimpleNamespace(state_dir=tmp_path)
    boundary = operator.ProcessLease(
        tmp_path / "telegram-inbox.sqlite3.lock",
        protocol="friday.telegram-bridge.v1",
    )
    observed: list[tuple[Any, Any]] = []

    def collect(settings, lease):
        observed.append((settings, lease))
        return _guarded_snapshot()

    monkeypatch.setattr(
        operator,
        "collect_document_contour_guarded_bridge_queue_snapshot",
        collect,
    )
    assert runtime.guarded_queue_snapshot(boundary) == _guarded_snapshot()
    assert observed == [(runtime.settings, boundary)]


def test_live_guard_acquisition_releases_a_partially_acquired_lease_on_baseexception(
    tmp_path,
    monkeypatch,
) -> None:
    released: list[bool] = []

    class Lease:
        def __init__(self, path, *, protocol):
            assert path == tmp_path / "telegram-inbox.sqlite3.lock"
            assert protocol == "friday.telegram-bridge.v1"

        def acquire(self) -> None:
            raise SystemExit(9)

        def release(self) -> None:
            released.append(True)

    runtime = object.__new__(operator.LiveRuntime)
    runtime.settings = SimpleNamespace(state_dir=tmp_path)
    monkeypatch.setattr(operator, "ProcessLease", Lease)
    with pytest.raises(SystemExit, match="9"):
        runtime.acquire_guard(operator.ExecutionState(started_at=0.0))
    assert released == [True]


def test_bridge_service_lease_match_requires_exact_recorded_protocol(tmp_path, monkeypatch) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    runtime.settings = SimpleNamespace(state_dir=tmp_path)

    def inspect(_path, *, protocol):
        assert protocol == "friday.telegram-bridge.v1"
        return {
            "active": True,
            "protocol_matches": True,
            "recorded_protocol": None,
            "pid": BRIDGE.main_pid,
        }

    monkeypatch.setattr(operator, "inspect_process_lease", inspect)
    assert runtime.pre_stop_bridge_lease_matches(BRIDGE.main_pid) is False


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        (b"process_start_time_seconds 1700000000\n", None),
        (b"# HELP x y\nprocess_start_time_seconds 1700000000.5\n", None),
        (b"", "dispatcher_metrics_epoch_missing"),
        (
            b"process_start_time_seconds 1\nprocess_start_time_seconds 2\n",
            "dispatcher_metrics_epoch_missing",
        ),
        (b"process_start_time_seconds NaN\n", "dispatcher_metrics_invalid"),
        (b"process_start_time_seconds +Inf\n", "dispatcher_metrics_invalid"),
        (b"process_start_time_seconds 0\n", "dispatcher_metrics_invalid"),
    ),
)
def test_dispatcher_epoch_parser_requires_one_positive_finite_metric(body, expected) -> None:
    if expected is None:
        value = operator._parse_dispatcher_epoch(body)
        assert len(value) == 64
    else:
        with pytest.raises(operator.OperatorFailure, match=expected):
            operator._parse_dispatcher_epoch(body)


def test_dispatcher_epoch_comparison_does_not_collapse_distinct_decimal_samples() -> None:
    first = operator._parse_dispatcher_epoch(b"process_start_time_seconds 1700000000.00000001\n")
    second = operator._parse_dispatcher_epoch(b"process_start_time_seconds 1700000000.00000002\n")
    assert first != second
    assert operator._parse_dispatcher_epoch(b"process_start_time_seconds 1.0\n") == (
        operator._parse_dispatcher_epoch(b"process_start_time_seconds 1e0\n")
    )


def test_streamed_http_body_has_a_total_deadline_even_for_small_trickle_chunks() -> None:
    response = SimpleNamespace(iter_bytes=lambda: iter((b"a", b"b", b"c")))
    ticks = iter((1.0, 2.0, 3.0))

    with pytest.raises(operator.OperatorFailure, match="http_response_deadline_exceeded"):
        operator._bounded_response_body(
            response,
            maximum_bytes=100,
            deadline=3.0,
            monotonic=lambda: next(ticks),
        )

    assert (
        operator._bounded_response_body(
            response,
            maximum_bytes=100,
            deadline=1.0,
            monotonic=lambda: 0.0,
        )
        == b"abc"
    )


def test_private_env_is_descriptor_pinned_and_rejects_link_or_mutation(tmp_path) -> None:
    env = tmp_path / "runtime.env"
    env.write_text("FRIDAY_API_TOKEN=" + "x" * 48 + "\n", encoding="utf-8")
    env.chmod(0o600)
    pinned = operator.PinnedPrivateFile(
        env,
        maximum_bytes=operator.MAX_ENV_BYTES,
        invalid_code="env_file_invalid",
    )
    try:
        pinned.revalidate()
        replacement = tmp_path / "replacement.env"
        replacement.write_text("FRIDAY_API_TOKEN=" + "y" * 48 + "\n", encoding="utf-8")
        replacement.chmod(0o600)
        os.replace(replacement, env)
        with pytest.raises(operator.OperatorFailure, match="env_file_invalid"):
            pinned.revalidate()
    finally:
        pinned.close()

    target = tmp_path / "target.env"
    target.write_text("FRIDAY_API_TOKEN=" + "z" * 48 + "\n", encoding="utf-8")
    target.chmod(0o600)
    linked = tmp_path / "linked.env"
    linked.symlink_to(target)
    with pytest.raises(operator.OperatorFailure, match="env_file_invalid"):
        operator.PinnedPrivateFile(
            linked,
            maximum_bytes=operator.MAX_ENV_BYTES,
            invalid_code="env_file_invalid",
        )

    actual = tmp_path / "actual"
    private = actual / "private"
    private.mkdir(parents=True, mode=0o700)
    aliased_env = private / "aliased.env"
    aliased_env.write_text("FRIDAY_API_TOKEN=x\n", encoding="utf-8")
    aliased_env.chmod(0o600)
    (tmp_path / "alias").symlink_to(actual, target_is_directory=True)
    with pytest.raises(operator.OperatorFailure, match="env_file_invalid"):
        operator.PinnedPrivateFile(
            tmp_path / "alias" / "private" / "aliased.env",
            maximum_bytes=operator.MAX_ENV_BYTES,
            invalid_code="env_file_invalid",
        )


def test_user_systemd_runtime_must_be_owner_private_and_lexical(tmp_path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    assert operator._user_runtime_directory() == runtime_dir
    runtime_dir.chmod(0o755)
    with pytest.raises(operator.OperatorFailure, match="user_systemd_runtime_invalid"):
        operator._user_runtime_directory()


def test_env_parser_rejects_duplicates_and_does_not_expand_values() -> None:
    assert operator._parse_env(b"FRIDAY_API_TOKEN='$literal value'\n") == {
        "FRIDAY_API_TOKEN": "$literal value"
    }
    with pytest.raises(operator.OperatorFailure, match="env_file_invalid"):
        operator._parse_env(b"FRIDAY_API_TOKEN=a\nFRIDAY_API_TOKEN=b\n")
    with pytest.raises(operator.OperatorFailure, match="env_alias_conflict"):
        operator._parse_env(b"FRIDAY_API_TOKEN=a\nJERICHO_API_TOKEN=b\n")


def test_barrier_is_empty_private_and_descriptor_pinned(tmp_path) -> None:
    barrier_path = tmp_path / "barrier"
    barrier_path.mkdir(mode=0o700)
    barrier = operator.PinnedBarrier(barrier_path)
    try:
        replacement = tmp_path / "replacement"
        replacement.mkdir(mode=0o700)
        saved = tmp_path / "saved"
        os.replace(barrier_path, saved)
        os.replace(replacement, barrier_path)
        with pytest.raises(operator.OperatorFailure, match="barrier_parent_changed|barrier_changed"):
            barrier.names()
    finally:
        barrier.close()


def test_barrier_requires_a_private_stable_parent(tmp_path) -> None:
    shared_parent = tmp_path / "shared"
    shared_parent.mkdir(mode=0o755)
    barrier_path = shared_parent / "barrier"
    barrier_path.mkdir(mode=0o700)
    with pytest.raises(operator.OperatorFailure, match="private_directory_invalid"):
        operator.PinnedBarrier(barrier_path)

    actual = tmp_path / "actual"
    private = actual / "private"
    private.mkdir(parents=True, mode=0o700)
    (private / "barrier").mkdir(mode=0o700)
    (tmp_path / "alias").symlink_to(actual, target_is_directory=True)
    with pytest.raises(operator.OperatorFailure, match="barrier_changed"):
        operator.PinnedBarrier(tmp_path / "alias" / "private" / "barrier")


def test_barrier_rejects_noncanonical_symlink_hardlink_and_extra_files(tmp_path) -> None:
    barrier_path = tmp_path / "barrier"
    barrier_path.mkdir(mode=0o700)
    barrier = operator.PinnedBarrier(barrier_path)
    try:
        noncanonical = barrier_path / "noncanonical.json"
        noncanonical.write_text('{"b": 2, "a": 1}\n', encoding="utf-8")
        noncanonical.chmod(0o600)
        with pytest.raises(operator.OperatorFailure, match="not_canonical"):
            barrier.read_canonical_json(noncanonical.name)
        noncanonical.unlink()

        target = barrier_path / "target.json"
        _private_json(target, {"a": 1})
        linked = barrier_path / "link.json"
        linked.symlink_to(target)
        with pytest.raises(operator.OperatorFailure):
            barrier.read_canonical_json(linked.name)
        linked.unlink()

        hardlink = barrier_path / "hard.json"
        os.link(target, hardlink)
        with pytest.raises(operator.OperatorFailure):
            barrier.read_canonical_json(target.name)
    finally:
        barrier.close()


def test_atomic_response_is_create_only_private_and_canonical(tmp_path) -> None:
    barrier = _barrier(tmp_path)
    response = {"schema": "test", "ok": True}
    try:
        encoded = barrier.atomic_write_json("response.json", response)
        assert encoded == _canonical(response)
        metadata = os.lstat(tmp_path / "barrier" / "response.json")
        assert stat_mode(metadata.st_mode) == 0o600
        assert metadata.st_nlink == 1
        with pytest.raises(operator.OperatorFailure, match="observer_response_exists"):
            barrier.atomic_write_json("response.json", response)
    finally:
        barrier.close()


def test_atomic_response_cannot_replace_a_target_raced_into_place(tmp_path, monkeypatch) -> None:
    barrier = _barrier(tmp_path)
    original = operator._rename_noreplace
    raced_bytes = _canonical({"raced": True})

    def race_then_publish(source_dir, source_name, target_dir, target_name):
        descriptor = os.open(
            target_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=target_dir,
        )
        try:
            os.write(descriptor, raced_bytes)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        original(source_dir, source_name, target_dir, target_name)

    monkeypatch.setattr(operator, "_rename_noreplace", race_then_publish)
    try:
        with pytest.raises(operator.OperatorFailure, match="observer_response_exists"):
            barrier.atomic_write_json("response.json", {"ours": True})
        assert (tmp_path / "barrier" / "response.json").read_bytes() == raced_bytes
        assert barrier.names() == {"response.json"}
    finally:
        barrier.close()


def test_cgroup_proof_is_recursive_and_fails_closed_without_events(tmp_path) -> None:
    root = tmp_path / "cgroup"
    group = root / "user.slice" / "battery.scope"
    group.mkdir(parents=True)
    (group / "cgroup.procs").write_text("", encoding="ascii")
    (group / "cgroup.threads").write_text("", encoding="ascii")
    (group / "cgroup.events").write_text("populated 0\nfrozen 0\n", encoding="ascii")
    assert (
        operator._cgroup_populated(
            "/user.slice/battery.scope",
            cgroup_root=root,
        )
        is False
    )

    # `populated` is recursive: an empty scope root can still own a worker in
    # a descendant cgroup, which direct cgroup.procs inspection would miss.
    (group / "cgroup.events").write_text("populated 1\nfrozen 0\n", encoding="ascii")
    assert (
        operator._cgroup_populated(
            "/user.slice/battery.scope",
            cgroup_root=root,
        )
        is True
    )

    (group / "cgroup.events").unlink()
    with pytest.raises(operator.OperatorFailure, match="cgroup_state_unavailable"):
        operator._cgroup_populated("/user.slice/battery.scope", cgroup_root=root)
    with pytest.raises(operator.OperatorFailure, match="cgroup_identity_invalid"):
        operator._cgroup_populated("/../../outside", cgroup_root=root)


def test_spawn_closes_first_private_stream_when_second_acquisition_aborts(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    first = SimpleNamespace(closed=False)

    def close() -> None:
        first.closed = True

    first.close = close
    calls = 0

    def temporary_file(*, mode):
        nonlocal calls
        assert mode == "w+b"
        calls += 1
        if calls == 1:
            return first
        raise SystemExit(7)

    monkeypatch.setattr(operator.tempfile, "TemporaryFile", temporary_file)
    with pytest.raises(SystemExit, match="7"):
        runtime.spawn_battery(_config(tmp_path), operator.ExecutionState(started_at=0.0))
    assert first.closed is True


def test_spawn_hands_a_started_unbound_controller_to_whole_scope_cleanup(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    runtime.model_environment = {}
    runtime.user_runtime_directory = tmp_path
    cleanup_children: list[operator.LiveBatteryProcess] = []

    class Process:
        pid_reads = 0
        returncode = None

        @property
        def pid(self) -> int:
            self.pid_reads += 1
            if self.pid_reads == 1:
                raise SystemExit(6)
            return 43212

    def cleanup(child):
        cleanup_children.append(child)
        child.stdout_file.close()
        child.stderr_file.close()
        child.finished = True
        return operator.BatteryOutcome(143, b"", True, True)

    monkeypatch.setattr(operator.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(runtime, "cleanup_child", cleanup)
    with pytest.raises(SystemExit, match="6"):
        runtime.spawn_battery(_config(tmp_path), operator.ExecutionState(started_at=0.0))
    assert len(cleanup_children) == 1
    assert cleanup_children[0].process_group == 43212
    assert cleanup_children[0].scope_unit.endswith(".scope")


def test_spawn_wraps_exactly_one_canonical_battery_in_one_private_env_scope(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    secret = "MODEL-SECRET-NOT-IN-ARGV"
    runtime.model_environment = {key: "" for key in operator._MODEL_ENV_ALLOWLIST}
    runtime.model_environment["FRIDAY_LLM_API_KEY"] = secret
    runtime.user_runtime_directory = tmp_path
    captured: list[tuple[list[str], dict[str, Any]]] = []

    class Process:
        pid = 43210
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    def popen(command, **kwargs):
        captured.append((list(command), dict(kwargs)))
        return Process()

    pidfd = os.open("/dev/null", os.O_RDONLY)
    monkeypatch.setattr(operator.subprocess, "Popen", popen)
    monkeypatch.setattr(operator, "_pidfd_open", lambda _pid: pidfd)
    monkeypatch.setattr(operator, "_pidfd_alive", lambda _descriptor: True)
    monkeypatch.setattr(operator, "_cgroup_populated", lambda _group: True)
    monkeypatch.setattr(operator.secrets, "token_hex", lambda _length: "1" * 12)
    monkeypatch.setattr(
        runtime,
        "_wait_scope_control_group",
        lambda unit, process: f"/user.slice/{unit}",
    )
    monkeypatch.setenv("UNRELATED_PRIVATE_VALUE", "must-not-cross")
    monkeypatch.setenv("PATH", "/private/substituted-bin")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/private/substituted-loader")

    marker_barrier = tmp_path / "${FRIDAY_LLM_API_KEY}" / "barrier"
    config = operator.OperatorConfig(
        freeze_commit=COMMIT,
        env_file=tmp_path / "env",
        barrier_dir=marker_barrier,
        backend_unit="friday-backend.service",
        bridge_unit="friday-bridge.service",
    )
    owner = operator.ExecutionState(started_at=0.0)
    child = runtime.spawn_battery(config, owner)
    assert owner.child is child
    try:
        assert len(captured) == 1
        command, kwargs = captured[0]
        runner = str(operator.ROOT / "tools/document_contour_live_battery.py")
        assert command.count(runner) == 1
        assert command[:5] == [
            operator._SYSTEMD_RUN_BINARY,
            "--user",
            "--scope",
            "--quiet",
            "--collect",
        ]
        assert "--property=KillMode=control-group" in command
        assert "--expand-environment=no" in command
        assert command.index("--expand-environment=no") < command.index("--")
        assert "--" in command
        assert command[command.index("--") + 1 :] == [
            operator.sys.executable,
            "-B",
            runner,
            "--run-live",
            "--freeze-commit",
            COMMIT,
            "--operator-model-env-only",
            "--bridge-stopped",
            "--inter-run-barrier-dir",
            str(marker_barrier),
        ]
        assert secret not in "\0".join(command)
        assert "${FRIDAY_LLM_API_KEY}" in "\0".join(command)
        assert kwargs["env"]["FRIDAY_LLM_API_KEY"] == secret
        assert "FRIDAY_ENV_FILE" not in kwargs["env"]
        assert str(tmp_path / "env") not in command
        assert "UNRELATED_PRIVATE_VALUE" not in kwargs["env"]
        assert "PATH" not in kwargs["env"]
        assert "LD_LIBRARY_PATH" not in kwargs["env"]
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] is subprocess.DEVNULL
    finally:
        os.close(child.pidfd)
        child.stdout_file.close()
        child.stderr_file.close()


def test_spawn_scope_binding_failure_cleans_the_started_contour(tmp_path, monkeypatch) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    runtime.model_environment = {}
    runtime.user_runtime_directory = tmp_path
    cleanup_children: list[operator.LiveBatteryProcess] = []

    class Process:
        pid = 43211
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

    monkeypatch.setattr(operator.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    pidfd = os.open("/dev/null", os.O_RDONLY)
    monkeypatch.setattr(operator, "_pidfd_open", lambda _pid: pidfd)
    monkeypatch.setattr(
        runtime,
        "_wait_scope_control_group",
        lambda _unit, _process: (_ for _ in ()).throw(operator.OperatorFailure("battery_scope_unavailable")),
    )

    def cleanup(child):
        cleanup_children.append(child)
        os.close(child.pidfd)
        child.pidfd = -1
        child.stdout_file.close()
        child.stderr_file.close()
        child.finished = True
        return operator.BatteryOutcome(143, b"", True, True)

    monkeypatch.setattr(runtime, "cleanup_child", cleanup)
    with pytest.raises(operator.OperatorFailure, match="battery_scope_unavailable"):
        runtime.spawn_battery(_config(tmp_path), operator.ExecutionState(started_at=0.0))
    assert len(cleanup_children) == 1
    assert cleanup_children[0].process is not None
    assert cleanup_children[0].scope_unit.endswith(".scope")


@pytest.mark.parametrize("uncertain", ("controller", "pidfd", "cgroup"))
def test_spawn_rejects_unbound_controller_or_scope_population(
    tmp_path,
    monkeypatch,
    uncertain,
) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    runtime.model_environment = {}
    runtime.user_runtime_directory = tmp_path
    cleanup_children: list[operator.LiveBatteryProcess] = []

    class Process:
        pid = 43213
        returncode = None

        @staticmethod
        def poll():
            return 1 if uncertain == "controller" else None

    monkeypatch.setattr(operator.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    pidfd = os.open("/dev/null", os.O_RDONLY)
    monkeypatch.setattr(operator, "_pidfd_open", lambda _pid: pidfd)
    monkeypatch.setattr(operator, "_pidfd_alive", lambda _descriptor: uncertain != "pidfd")
    monkeypatch.setattr(operator, "_cgroup_populated", lambda _group: uncertain != "cgroup")
    monkeypatch.setattr(
        runtime,
        "_wait_scope_control_group",
        lambda _unit, _process: "/user.slice/battery.scope",
    )

    def cleanup(child):
        cleanup_children.append(child)
        os.close(child.pidfd)
        child.pidfd = -1
        child.stdout_file.close()
        child.stderr_file.close()
        child.finished = True
        return operator.BatteryOutcome(143, b"", True, True)

    monkeypatch.setattr(runtime, "cleanup_child", cleanup)
    with pytest.raises(operator.OperatorFailure, match="battery_scope_unavailable"):
        runtime.spawn_battery(_config(tmp_path), operator.ExecutionState(started_at=0.0))
    assert len(cleanup_children) == 1


def test_scope_cleanup_attempts_kill_after_term_command_baseexception(monkeypatch) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    commands: list[list[str]] = []
    cgroup_states = iter((False, True, True))
    clock = iter(range(20))

    def run_command(command, *, timeout):
        del timeout
        rendered = list(command)
        commands.append(rendered)
        if "--signal=SIGTERM" in rendered:
            raise KeyboardInterrupt()
        return subprocess.CompletedProcess(rendered, 0, b"", b"")

    class Process:
        pid = 44444
        returncode = 143

        @staticmethod
        def wait(*, timeout):
            del timeout
            return 143

    runtime._run_command = run_command
    runtime._cgroup_empty = lambda _group: next(cgroup_states)
    runtime.monotonic = lambda: float(next(clock))
    runtime.pause = lambda _seconds: None
    killed_groups: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        operator.os,
        "killpg",
        lambda group, selected: killed_groups.append((group, selected)),
    )
    monkeypatch.setattr(operator, "_pidfd_alive", lambda _descriptor: False)
    monkeypatch.setattr(operator, "CHILD_TERM_GRACE_SEC", 0.0)
    monkeypatch.setattr(operator, "CHILD_KILL_GRACE_SEC", 0.0)
    pidfd = os.open("/dev/null", os.O_RDONLY)
    stdout = BytesIO(b"closed-output")
    child = operator.LiveBatteryProcess(
        process=Process(),
        stdout_file=stdout,
        stderr_file=BytesIO(),
        process_group=44444,
        scope_unit="friday-document-contour-test.scope",
        scope_control_group="/user.slice/test.scope",
        pidfd=pidfd,
    )

    outcome = runtime.cleanup_child(child)

    assert [item[-2] for item in commands] == ["--signal=SIGTERM", "--signal=SIGKILL"]
    assert killed_groups == [(44444, signal.SIGKILL)]
    assert outcome.process_group_clear is True
    assert outcome.cleanup_used is True
    assert outcome.stdout == b"closed-output"
    assert child.finished is True
    assert child.pidfd == -1


@pytest.mark.parametrize("leader_failure", ("timeout", "baseexception"))
def test_cgroup_empty_after_term_but_unreaped_leader_forces_one_kill_and_second_wait(
    monkeypatch,
    leader_failure,
) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    commands: list[list[str]] = []
    killed_groups: list[tuple[int, signal.Signals]] = []

    def run_command(command, *, timeout):
        del timeout
        rendered = list(command)
        commands.append(rendered)
        return subprocess.CompletedProcess(rendered, 0, b"", b"")

    class Process:
        pid = 45555
        returncode = None
        wait_calls = 0

        def wait(self, *, timeout):
            self.wait_calls += 1
            if self.wait_calls == 1:
                if leader_failure == "timeout":
                    raise subprocess.TimeoutExpired("systemd-run", timeout)
                raise SystemExit(9)
            self.returncode = -int(signal.SIGKILL)
            return self.returncode

    runtime._run_command = run_command
    runtime._cgroup_empty = lambda _group: True
    runtime.monotonic = lambda: 0.0
    runtime.pause = lambda _seconds: None
    monkeypatch.setattr(
        operator.os,
        "killpg",
        lambda group, selected: killed_groups.append((group, selected)),
    )
    monkeypatch.setattr(operator, "_pidfd_alive", lambda _descriptor: False)
    pidfd = os.open("/dev/null", os.O_RDONLY)
    process = Process()
    child = operator.LiveBatteryProcess(
        process=process,
        stdout_file=BytesIO(b"closed-output"),
        stderr_file=BytesIO(),
        process_group=process.pid,
        scope_unit="friday-document-contour-test.scope",
        scope_control_group="/user.slice/test.scope",
        pidfd=pidfd,
    )

    outcome = runtime.cleanup_child(child)

    assert process.wait_calls == 2
    assert [item[-2] for item in commands] == ["--signal=SIGTERM", "--signal=SIGKILL"]
    assert killed_groups == [(process.pid, signal.SIGKILL)]
    assert outcome.returncode == -int(signal.SIGKILL)
    assert outcome.process_group_clear is True
    assert child.pidfd == -1


def test_scope_wait_baseexception_fails_closed_into_the_kill_path() -> None:
    runtime = object.__new__(operator.LiveRuntime)
    runtime.monotonic = lambda: 0.0
    runtime._cgroup_empty = lambda _group: False
    runtime.pause = lambda _seconds: (_ for _ in ()).throw(SystemExit(8))
    child = SimpleNamespace(scope_control_group="/user.slice/test.scope")
    assert runtime._wait_scope_empty(child, 1.0) is False


def stat_mode(mode: int) -> int:
    return mode & 0o777


@pytest.mark.parametrize(
    "unit",
    (
        "",
        "--now.service",
        "../bridge.service",
        "bridge",
        "bridge.service/other",
        "bridge service.service",
    ),
)
def test_unit_names_cannot_inject_systemctl_arguments(unit) -> None:
    with pytest.raises(operator.OperatorFailure, match="systemd_unit_invalid"):
        operator._validate_unit_name(unit)


def test_systemctl_show_parser_requires_exact_identity_tuple() -> None:
    unit = "friday-backend.service"
    values = {
        "Id": unit,
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "MainPID": "42",
        "ControlPID": "0",
        "InvocationID": "1" * 32,
        "NRestarts": "0",
        "ExecMainStartTimestampMonotonic": "100",
        "ControlGroup": "/user.slice/backend",
    }
    raw = "".join(f"{key}={value}\n" for key, value in values.items()).encode()
    assert operator._parse_systemctl_show(raw, expected_unit=unit) == values
    with pytest.raises(operator.OperatorFailure, match="systemd_state_invalid"):
        operator._parse_systemctl_show(raw + b"Environment=SECRET\n", expected_unit=unit)
    with pytest.raises(operator.OperatorFailure, match="systemd_state_invalid"):
        operator._parse_systemctl_show(raw + b"MainPID=99\n", expected_unit=unit)


def test_scope_show_parser_rejects_missing_duplicate_or_extra_identity() -> None:
    values = {
        "Id": "battery.scope",
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "ControlGroup": "/user.slice/battery.scope",
        "KillMode": "control-group",
    }
    raw = "".join(f"{key}={value}\n" for key, value in values.items()).encode()
    assert operator._parse_scope_show(raw) == values
    for mutation in (
        raw.replace(b"KillMode=control-group\n", b""),
        raw + b"Id=other.scope\n",
        raw + b"Environment=PRIVATE\n",
    ):
        with pytest.raises(operator.OperatorFailure, match="battery_scope_invalid"):
            operator._parse_scope_show(mutation)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("Id", "other.scope"),
        ("LoadState", "not-found"),
        ("ActiveState", "inactive"),
        ("SubState", "dead"),
        ("ControlGroup", "relative.scope"),
        ("KillMode", "process"),
    ),
)
def test_scope_binding_requires_exact_active_control_group_contract(
    field,
    replacement,
) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    values = {
        "Id": "battery.scope",
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "ControlGroup": "/user.slice/battery.scope",
        "KillMode": "control-group",
    }
    values[field] = replacement
    raw = "".join(f"{key}={value}\n" for key, value in values.items()).encode()
    commands: list[list[str]] = []

    def run_command(command, *, timeout):
        del timeout
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, raw, b"")

    clock = iter((0.0, 6.0))
    runtime._run_command = run_command
    runtime.monotonic = lambda: next(clock)
    runtime.pause = lambda _seconds: None
    process = SimpleNamespace(poll=lambda: None)

    with pytest.raises(operator.OperatorFailure, match="battery_scope_unavailable"):
        runtime._wait_scope_control_group("battery.scope", process)
    assert commands[0] == [
        operator._SYSTEMCTL_BINARY,
        "--user",
        "show",
        "--no-pager",
        "--property=Id",
        "--property=LoadState",
        "--property=ActiveState",
        "--property=SubState",
        "--property=ControlGroup",
        "--property=KillMode",
        "battery.scope",
    ]


def test_scope_binding_accepts_exact_active_control_group_contract() -> None:
    runtime = object.__new__(operator.LiveRuntime)
    raw = (
        b"Id=battery.scope\n"
        b"LoadState=loaded\n"
        b"ActiveState=active\n"
        b"SubState=running\n"
        b"ControlGroup=/user.slice/battery.scope\n"
        b"KillMode=control-group\n"
    )
    runtime._run_command = lambda command, timeout: subprocess.CompletedProcess(
        command,
        0,
        raw,
        b"",
    )
    runtime.monotonic = lambda: 0.0
    runtime.pause = lambda _seconds: None
    process = SimpleNamespace(poll=lambda: None)

    assert runtime._wait_scope_control_group("battery.scope", process) == "/user.slice/battery.scope"


def test_http_clients_keep_credentials_in_headers_and_disable_ambient_routing(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    class Client:
        def __init__(self, **kwargs) -> None:
            calls.append(dict(kwargs))

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(operator.httpx, "Client", Client)
    monkeypatch.setattr(operator, "_user_runtime_directory", lambda: Path("/run/user/1000"))
    settings = SimpleNamespace(
        api_host="127.0.0.1",
        api_port=8765,
        api_tls_enabled=False,
        api_token="OWNER-TOKEN-IN-MEMORY",
        llm_base_url="https://10.0.0.8:9443/v1",
        llm_api_key="MODEL-TOKEN-IN-MEMORY",
    )
    runtime = operator.LiveRuntime(
        settings,
        {},
        SimpleNamespace(revalidate=lambda: None),
        backend_unit="friday-backend.service",
        bridge_unit="friday-bridge.service",
    )
    try:
        assert len(calls) == 2
        backend_client, dispatcher_client = calls
        assert backend_client["headers"]["Authorization"] == "Bearer OWNER-TOKEN-IN-MEMORY"
        assert dispatcher_client["headers"]["Authorization"] == "Bearer MODEL-TOKEN-IN-MEMORY"
        assert backend_client["trust_env"] is False
        assert dispatcher_client["trust_env"] is False
        assert backend_client["follow_redirects"] is False
        assert dispatcher_client["follow_redirects"] is False
        assert backend_client["verify"] is True
        assert dispatcher_client["verify"] is True
    finally:
        runtime.close()


def test_live_runtime_close_continues_after_baseexception_and_closes_pidfds() -> None:
    events: list[str] = []

    class Client:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        def close(self) -> None:
            events.append(self.name)
            if self.fail:
                raise SystemExit(5)

    runtime = object.__new__(operator.LiveRuntime)
    runtime._backend_client = Client("backend", fail=True)
    runtime._dispatcher_client = Client("dispatcher")
    backend_fd = os.open("/dev/null", os.O_RDONLY)
    bridge_fd = os.open("/dev/null", os.O_RDONLY)
    runtime._backend_pidfd = backend_fd
    runtime._bridge_pidfd = bridge_fd
    with pytest.raises(operator.OperatorFailure, match="runtime_close_failed"):
        runtime.close()
    assert events == ["backend", "dispatcher"]
    assert runtime._backend_pidfd == -1
    assert runtime._bridge_pidfd == -1
    for descriptor in (backend_fd, bridge_fd):
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_systemd_commands_drop_proxy_remote_bus_and_unrelated_environment(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    runtime.user_runtime_directory = tmp_path
    observed: list[dict[str, str]] = []
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(list(command))
        observed.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(operator.subprocess, "run", run)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")
    monkeypatch.setenv("SYSTEMD_HOST", "remote.invalid")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "tcp:host=remote.invalid")
    monkeypatch.setenv("UNRELATED_PRIVATE_VALUE", "private")
    monkeypatch.setenv("PATH", "/private/substituted-bin")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/private/substituted-loader")
    monkeypatch.setenv("SSL_CERT_FILE", "/private/owner-ca.pem")
    runtime._run_command(
        [operator._SYSTEMCTL_BINARY, "--user", "show", "x.service"],
        timeout=1.0,
    )
    assert commands == [[operator._SYSTEMCTL_BINARY, "--user", "show", "x.service"]]
    assert len(observed) == 1
    assert observed[0]["XDG_RUNTIME_DIR"] == str(tmp_path)
    assert observed[0]["SSL_CERT_FILE"] == "/private/owner-ca.pem"
    for forbidden in (
        "HTTP_PROXY",
        "SYSTEMD_HOST",
        "DBUS_SESSION_BUS_ADDRESS",
        "UNRELATED_PRIVATE_VALUE",
        "PATH",
        "LD_LIBRARY_PATH",
    ):
        assert forbidden not in observed[0]


def test_cli_requires_explicit_units_and_keeps_report_outside_barrier(tmp_path) -> None:
    args = SimpleNamespace(
        run_live=True,
        freeze_commit=COMMIT,
        env_file=str(tmp_path / "env"),
        inter_run_barrier_dir=str(tmp_path / "barrier"),
        backend_unit="friday-backend.service",
        bridge_unit="friday-bridge.service",
        report=str(tmp_path / "barrier" / "report.json"),
    )
    with pytest.raises(operator.OperatorFailure, match="report_must_be_outside_barrier"):
        operator._config_from_args(args)

    args.report = ""
    args.bridge_unit = args.backend_unit
    with pytest.raises(operator.OperatorFailure, match="systemd_units_not_distinct"):
        operator._config_from_args(args)

    args.bridge_unit = "friday-bridge.service"
    args.env_file = "relative.env"
    with pytest.raises(operator.OperatorFailure, match="operator_path_not_absolute"):
        operator._config_from_args(args)

    args.env_file = str(tmp_path / "env")
    args.report = str(tmp_path / "env")
    with pytest.raises(operator.OperatorFailure, match="report_conflicts_with_env"):
        operator._config_from_args(args)

    args.report = str(tmp_path / "barrier" / "nested" / "report.json")
    with pytest.raises(operator.OperatorFailure, match="report_must_be_outside_barrier"):
        operator._config_from_args(args)


def test_sanitized_report_is_private_create_only_and_never_clobbers(tmp_path) -> None:
    report_path = tmp_path / "operator-report.json"
    payload = {"schema": operator.OPERATOR_SCHEMA, "status": "failed"}
    operator._atomic_private_report(report_path, payload)
    assert report_path.read_bytes() == _canonical(payload)
    assert stat_mode(report_path.stat().st_mode) == 0o600
    original = report_path.read_bytes()
    with pytest.raises(operator.OperatorFailure, match="report_path_exists"):
        operator._atomic_private_report(report_path, {"private": "replacement"})
    assert report_path.read_bytes() == original


def test_sanitized_report_rejects_post_publish_content_substitution(tmp_path, monkeypatch) -> None:
    report_path = tmp_path / "operator-report.json"
    original_publish = operator._rename_noreplace
    substitute = _canonical({"schema": operator.OPERATOR_SCHEMA, "status": "substituted"})

    def publish_then_substitute(source_dir, source_name, target_dir, target_name):
        original_publish(source_dir, source_name, target_dir, target_name)
        descriptor = os.open(target_name, os.O_WRONLY | os.O_TRUNC, dir_fd=target_dir)
        try:
            os.write(descriptor, substitute)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    monkeypatch.setattr(operator, "_rename_noreplace", publish_then_substitute)
    with pytest.raises(operator.OperatorFailure, match="report_write_failed"):
        operator._atomic_private_report(
            report_path,
            {"schema": operator.OPERATOR_SCHEMA, "status": "passed"},
        )
    assert report_path.read_bytes() == substitute


def test_git_candidate_checks_ignore_ambient_repository_and_network_controls(monkeypatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/private/alternate.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/private/alternate-tree")
    monkeypatch.setenv("HTTPS_PROXY", "http://private-proxy.invalid")
    observed: dict[str, Any] = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout=f"{COMMIT}\n", stderr="")

    monkeypatch.setattr(operator.subprocess, "run", run)
    assert operator._git_output("rev-parse", "HEAD") == COMMIT
    assert observed["command"][:3] == [
        operator._GIT_BINARY,
        "-c",
        "core.fsmonitor=false",
    ]
    environment = observed["environment"]
    assert "GIT_DIR" not in environment
    assert "GIT_WORK_TREE" not in environment
    assert "HTTPS_PROXY" not in environment
    assert "PATH" not in environment
    assert "LD_LIBRARY_PATH" not in environment
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_dependency_hashes_are_frozen_to_the_authorized_inputs() -> None:
    assert operator._EXPECTED_DEPENDENCY_HASHES == {
        "tools/document_contour_live_battery.py": (
            "45f2944158f240cd1a61988aa27436b936a638e1c76051fdcefc019dc08cc3d1"
        ),
        "friday/diagnostics/__init__.py": (
            "86ce0798ec2666b3ebe05318fc1483042c2c9e35994f60d7f588cae47c779c06"
        ),
        "friday/diagnostics/runtime_lease.py": (
            "6986bcef0d21d1754672ad784746fbc205b4822de708c71b16dd93576f3d1926"
        ),
        "friday/admin_api/_overview.py": ("a72f76b59d7ab8ac19a56dc80d8ae1887fb02c07898f346f59fe4449444e6b51"),
    }


def test_declared_dependency_hashes_match_assembled_source_bytes() -> None:
    assembled_root = Path(__file__).resolve().parents[1]
    for relative, expected in operator._EXPECTED_DEPENDENCY_HASHES.items():
        dependency = assembled_root / relative
        assert dependency.is_file(), relative
        observed = hashlib.sha256(dependency.read_bytes()).hexdigest()
        assert observed == expected, relative
