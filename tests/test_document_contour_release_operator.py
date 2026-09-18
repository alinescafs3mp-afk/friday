"""Offline contract tests for the one-shot document contour operator."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import threading
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import tools.document_contour_release_operator as operator
from friday.config import PROFILES
from friday.model_profiles import QWEN38_27B_SGLANG_V12_PROFILE

COMMIT = "a" * 40
PROTECTED_IDENTITIES = ((101, "1" * 64), (202, "2" * 64))


def _canonical(payload: dict[str, Any]) -> bytes:
    return operator._canonical_json(payload) + b"\n"


def _private_json(path: Path, payload: dict[str, Any]) -> bytes:
    encoded = _canonical(payload)
    path.write_bytes(encoded)
    path.chmod(0o600)
    return encoded


def _protected_payload(
    identities: tuple[tuple[int, str], ...] = PROTECTED_IDENTITIES,
) -> dict[str, Any]:
    return {
        "schema": operator.PROTECTED_DEAD_LETTER_SET_SCHEMA,
        "row_fingerprint_schema": operator.DEAD_LETTER_ROW_FINGERPRINT_SCHEMA,
        "set_fingerprint_schema": operator.DEAD_LETTER_SET_FINGERPRINT_SCHEMA,
        "fingerprint_scope": operator.DEAD_LETTER_FINGERPRINT_SCOPE,
        "dead_letter_count": len(identities),
        "dead_letter_set_sha256": operator._dead_letter_set_sha256(identities),
        "dead_letter_identities": _identity_payload(identities),
    }


def _protected_pin(
    tmp_path: Path,
    identities: tuple[tuple[int, str], ...] = PROTECTED_IDENTITIES,
) -> tuple[operator.PinnedPrivateFile, operator.ProtectedDeadLetterSet]:
    path = tmp_path / "protected-dead-letter-set.json"
    _private_json(path, _protected_payload(identities))
    pinned = operator.PinnedPrivateFile(
        path,
        maximum_bytes=operator.MAX_PRIVATE_JSON_BYTES,
        invalid_code="protected_dead_letter_set_invalid",
    )
    return pinned, operator._parse_protected_dead_letter_set(pinned)


def _protected_set(
    identities: tuple[tuple[int, str], ...] = PROTECTED_IDENTITIES,
) -> operator.ProtectedDeadLetterSet:
    return operator.ProtectedDeadLetterSet(
        explicit=True,
        identities=identities,
        dead_letter_set_sha256=operator._dead_letter_set_sha256(identities),
    )


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


def _request(
    receipt: dict[str, Any],
    receipt_bytes: bytes,
    protected_set: operator.ProtectedDeadLetterSet = operator.EMPTY_PROTECTED_DEAD_LETTER_SET,
) -> dict[str, Any]:
    return {
        "schema": operator.OBSERVER_REQUEST_SCHEMA,
        "commit": COMMIT,
        "run_id_hash": receipt["run_id_hash"],
        "run_index": 1,
        "run_receipt_sha256": operator._sha256(receipt_bytes),
        "worker_report_sha256": receipt["worker_report_sha256"],
        "challenge": "e" * 64,
        "protected_dead_letter_count": protected_set.count if protected_set.explicit else None,
        "protected_dead_letter_set_sha256": (
            protected_set.dead_letter_set_sha256 if protected_set.explicit else None
        ),
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
        "dead_letter_set_sha256": None,
        "dead_letter_identities": None,
    }


def _identity_payload(identities: tuple[tuple[int, str], ...]) -> list[dict[str, Any]]:
    return [{"update_id": update_id, "row_fingerprint": fingerprint} for update_id, fingerprint in identities]


def _stopped_snapshot(
    pid: int,
    identities: tuple[tuple[int, str], ...] = (),
) -> dict[str, Any]:
    return {
        "schema": operator.OBSERVER_SNAPSHOT_SCHEMA,
        "backend_pid": pid,
        "backend_lease_owned": True,
        "physical_outbound_pending": 0,
        "bridge_queue_state": "present",
        "bridge_lease_acquired_for_snapshot": True,
        "bridge_lease_released": True,
        "inbound_pending": 0,
        "dead_letter": len(identities),
        "dead_letter_set_sha256": operator._dead_letter_set_sha256(identities),
        "dead_letter_identities": _identity_payload(identities),
    }


def _guarded_snapshot(identities: tuple[tuple[int, str], ...] = ()) -> dict[str, Any]:
    return {
        "schema": operator.GUARDED_QUEUE_SCHEMA,
        "bridge_guard_held": True,
        "bridge_queue_state": "present",
        "inbound_pending": 0,
        "dead_letter": len(identities),
        "dead_letter_set_sha256": operator._dead_letter_set_sha256(identities),
        "dead_letter_identities": _identity_payload(identities),
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
        stopped_snapshot: dict[str, Any] | None = None,
        guarded_snapshots: list[dict[str, Any]] | None = None,
    ) -> None:
        self.barrier_path = barrier_path
        self.fail_method = fail_method
        self.fail_call = fail_call
        self.failure = failure or operator.OperatorFailure("injected_failure")
        self.start_online = start_online
        self.stopped_snapshot = stopped_snapshot
        self.guarded_snapshots = list(guarded_snapshots or [])
        self.calls: dict[str, int] = {}
        self.events: list[str] = []
        self.now = 0.0
        self.bridge_running = True
        self.guard: FakeGuard | None = None
        self.child: FakeChild | None = None
        self.released = False
        self.start_calls = 0
        self.closed = False
        self.config: operator.OperatorConfig | None = None

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
        if self.bridge_running or self.guard:
            return _active_snapshot(BACKEND.main_pid)
        return dict(
            _stopped_snapshot(BACKEND.main_pid) if self.stopped_snapshot is None else self.stopped_snapshot
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
        if self.guarded_snapshots:
            return dict(self.guarded_snapshots.pop(0))
        return _guarded_snapshot()

    def spawn_battery(
        self,
        config: operator.OperatorConfig,
        owner: operator.ExecutionState,
    ) -> FakeChild:
        self._record("spawn")
        assert config.freeze_commit == COMMIT
        assert self.guard is not None and self.guard.acquired
        self.config = config
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
            _request(
                first,
                first_bytes,
                self.config.protected_dead_letter_set
                if self.config is not None
                else operator.EMPTY_PROTECTED_DEAD_LETTER_SET,
            ),
        )

    def _finish_successfully(self, child: FakeChild) -> None:
        second_report = _worker_report(2)
        child.run_reports.append(second_report)
        second = _receipt(2, worker_hash=operator._sha256(operator._canonical_json(second_report)))
        second_bytes = _private_json(self.barrier_path / "run-2-receipt.json", second)
        first_bytes = (self.barrier_path / "run-1-receipt.json").read_bytes()
        first = json.loads(first_bytes.decode("utf-8"))
        response_bytes = (self.barrier_path / "run-1-observer.json").read_bytes()
        response = json.loads(response_bytes.decode("utf-8"))
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
                "protected_dead_letter_count": response["protected_dead_letter_count"],
                "protected_dead_letter_set_sha256": response["protected_dead_letter_set_sha256"],
                "dead_letter_count": response["dead_letter_count"],
                "dead_letter_set_sha256": response["dead_letter_set_sha256"],
                "dead_letter_zero": response["dead_letter_zero"],
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


def _config(
    tmp_path: Path,
    *,
    protected_set: operator.ProtectedDeadLetterSet = operator.EMPTY_PROTECTED_DEAD_LETTER_SET,
    protected_path: Path | None = None,
) -> operator.OperatorConfig:
    return operator.OperatorConfig(
        freeze_commit=COMMIT,
        env_file=tmp_path / "env",
        barrier_dir=tmp_path / "barrier",
        backend_unit="friday-backend.service",
        bridge_unit="friday-bridge.service",
        protected_dead_letter_set_file=protected_path,
        protected_dead_letter_set=protected_set,
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
        "protected_dead_letter_count": None,
        "protected_dead_letter_set_sha256": None,
        "status": "passed",
        "bridge_stopped": True,
        "bridge_operator_guard_held": True,
        "backend_healthy": True,
        "backend_unchanged": True,
        "outbound_pending_zero": True,
        "inbound_pending_zero": True,
        "dead_letter_zero": True,
        "dead_letter_matches_protected_set": True,
        "dead_letter_count": 0,
        "dead_letter_set_sha256": operator.EMPTY_DEAD_LETTER_SET_SHA256,
        "dispatcher_unchanged": True,
    }
    assert set(report["evidence_sha256"]) == {
        "battery_report_sha256",
        "observer_request_sha256",
        "observer_response_sha256",
        "run_1_receipt_sha256",
        "run_2_receipt_sha256",
    }


def test_explicit_protected_set_passes_exactly_and_emits_truthful_nonzero_observer(
    tmp_path,
) -> None:
    pinned, protected = _protected_pin(tmp_path)
    config = _config(
        tmp_path,
        protected_set=protected,
        protected_path=pinned.path,
    )
    queue = _guarded_snapshot(PROTECTED_IDENTITIES)
    runtime = FakeRuntime(
        config.barrier_dir,
        stopped_snapshot=_stopped_snapshot(BACKEND.main_pid, PROTECTED_IDENTITIES),
        guarded_snapshots=[queue for _ in range(6)],
    )
    barrier = _barrier(tmp_path)
    try:
        report, exit_code = operator.execute_operator(
            config,
            runtime,
            barrier,
            protected_set_file=pinned,
        )
    finally:
        barrier.close()
        pinned.close()

    assert exit_code == 0
    assert report["status"] == "passed"
    assert runtime.calls["queue"] == 6
    response = json.loads((config.barrier_dir / "run-1-observer.json").read_text())
    assert response["protected_dead_letter_count"] == len(PROTECTED_IDENTITIES)
    assert response["protected_dead_letter_set_sha256"] == protected.dead_letter_set_sha256
    assert response["dead_letter_count"] == len(PROTECTED_IDENTITIES)
    assert response["dead_letter_set_sha256"] == protected.dead_letter_set_sha256
    assert response["dead_letter_zero"] is False
    assert response["dead_letter_matches_protected_set"] is True
    assert "dead_letter_identities" not in response
    encoded_report = json.dumps(report)
    assert "dead_letter_identities" not in encoded_report
    assert all(fingerprint not in encoded_report for _, fingerprint in PROTECTED_IDENTITIES)


def test_pinned_protected_set_change_after_stop_fails_closed_and_restores_bridge(tmp_path) -> None:
    pinned, protected = _protected_pin(tmp_path)
    config = _config(
        tmp_path,
        protected_set=protected,
        protected_path=pinned.path,
    )
    runtime = FakeRuntime(
        config.barrier_dir,
        stopped_snapshot=_stopped_snapshot(BACKEND.main_pid, PROTECTED_IDENTITIES),
    )
    original_stop = runtime.stop_bridge

    def stop_and_mutate_pin() -> None:
        original_stop()
        changed = ((101, "1" * 64), (202, "3" * 64))
        _private_json(pinned.path, _protected_payload(changed))

    runtime.stop_bridge = stop_and_mutate_pin  # type: ignore[method-assign]
    barrier = _barrier(tmp_path)
    try:
        report, exit_code = operator.execute_operator(
            config,
            runtime,
            barrier,
            protected_set_file=pinned,
        )
    finally:
        barrier.close()
        pinned.close()

    assert exit_code == 1
    assert report["failure_codes"] == ["protected_dead_letter_set_invalid"]
    assert runtime.start_calls == 1
    assert runtime.bridge_running is True
    assert "spawn" not in runtime.events


def test_inter_run_guarded_set_change_is_rejected_before_observer_publication(tmp_path) -> None:
    pinned, protected = _protected_pin(tmp_path)
    config = _config(
        tmp_path,
        protected_set=protected,
        protected_path=pinned.path,
    )
    exact = _guarded_snapshot(PROTECTED_IDENTITIES)
    changed = _guarded_snapshot(((101, "1" * 64), (202, "3" * 64)))
    runtime = FakeRuntime(
        config.barrier_dir,
        stopped_snapshot=_stopped_snapshot(BACKEND.main_pid, PROTECTED_IDENTITIES),
        guarded_snapshots=[exact, exact, changed],
    )
    barrier = _barrier(tmp_path)
    try:
        report, exit_code = operator.execute_operator(
            config,
            runtime,
            barrier,
            protected_set_file=pinned,
        )
    finally:
        barrier.close()
        pinned.close()

    assert exit_code == 1
    assert report["failure_codes"] == ["guarded_queue_protected_dead_letter_set_mismatch"]
    assert not (config.barrier_dir / "run-1-observer.json").exists()
    assert runtime.start_calls == 1
    assert runtime.released is True


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
    forged_zero = json.loads(json.dumps(payload))
    forged_zero["inter_run_observer"]["dead_letter_zero"] = False
    mutations.append(forged_zero)
    forged_count = json.loads(json.dumps(payload))
    forged_count["inter_run_observer"]["dead_letter_count"] = 1
    mutations.append(forged_count)
    forged_set_match = json.loads(json.dumps(payload))
    forged_set_match["inter_run_observer"]["dead_letter_matches_protected_set"] = False
    mutations.append(forged_set_match)
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
        ("protected_dead_letter_count", 0),
        ("protected_dead_letter_set_sha256", operator.EMPTY_DEAD_LETTER_SET_SHA256),
    ),
)
def test_request_must_bind_the_exact_canonical_receipt(field, replacement) -> None:
    receipt = _receipt(1)
    receipt_bytes = _canonical(receipt)
    request = _request(receipt, receipt_bytes)
    request[field] = replacement
    with pytest.raises(operator.OperatorFailure, match="observer_request_invalid"):
        operator._validate_observer_request(request, receipt, receipt_bytes, commit=COMMIT)


def test_exact_protected_set_file_schema_is_canonical_and_metadata_only() -> None:
    payload = _protected_payload()
    content = _canonical(payload)
    pinned = SimpleNamespace(content=content, content_sha256=operator._sha256(content))
    protected = operator._parse_protected_dead_letter_set(pinned)
    assert protected.explicit is True
    assert protected.identities == PROTECTED_IDENTITIES
    assert protected.count == 2
    assert protected.dead_letter_set_sha256 == operator._dead_letter_set_sha256(PROTECTED_IDENTITIES)
    assert "metadata_only" in operator.DEAD_LETTER_FINGERPRINT_SCOPE
    for excluded in ("payload_json", "backend_response_json", "last_error", "ordering_key"):
        assert excluded in operator.DEAD_LETTER_FINGERPRINT_SCOPE


def test_explicit_empty_pin_is_distinct_from_absent_pin() -> None:
    payload = _protected_payload(())
    content = _canonical(payload)
    pinned = SimpleNamespace(content=content, content_sha256=operator._sha256(content))
    protected = operator._parse_protected_dead_letter_set(pinned)
    assert protected.explicit is True
    assert protected.count == 0
    assert protected.dead_letter_set_sha256 == operator.EMPTY_DEAD_LETTER_SET_SHA256
    assert protected != operator.EMPTY_PROTECTED_DEAD_LETTER_SET


def test_protected_set_file_rejects_count_only_baselines_and_identity_ambiguity() -> None:
    mutations: list[dict[str, Any]] = []

    count_only = _protected_payload()
    count_only["dead_letter_identities"] = []
    count_only["dead_letter_count"] = len(PROTECTED_IDENTITIES)
    mutations.append(count_only)

    forged_digest = _protected_payload()
    forged_digest["dead_letter_set_sha256"] = "f" * 64
    mutations.append(forged_digest)

    unsorted = _protected_payload()
    unsorted["dead_letter_identities"] = list(reversed(unsorted["dead_letter_identities"]))
    mutations.append(unsorted)

    duplicate = _protected_payload()
    duplicate["dead_letter_identities"][1]["update_id"] = 101
    mutations.append(duplicate)

    body_bearing = _protected_payload()
    body_bearing["dead_letter_identities"][0]["payload_json"] = "forbidden-body"
    mutations.append(body_bearing)

    wrong_scope = _protected_payload()
    wrong_scope["fingerprint_scope"] = "whole_row"
    mutations.append(wrong_scope)

    boolean_id = _protected_payload()
    boolean_id["dead_letter_identities"][0]["update_id"] = True
    mutations.append(boolean_id)

    for payload in mutations:
        content = _canonical(payload)
        pinned = SimpleNamespace(content=content, content_sha256=operator._sha256(content))
        with pytest.raises(operator.OperatorFailure, match="^protected_dead_letter_set_invalid$"):
            operator._parse_protected_dead_letter_set(pinned)


def test_protected_set_file_rejects_noncanonical_bytes_and_identity_overflow() -> None:
    payload = _protected_payload()
    pretty = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    pinned = SimpleNamespace(content=pretty, content_sha256=operator._sha256(pretty))
    with pytest.raises(operator.OperatorFailure, match="^protected_dead_letter_set_invalid$"):
        operator._parse_protected_dead_letter_set(pinned)

    oversized = [
        {"update_id": index, "row_fingerprint": "a" * 64}
        for index in range(operator.MAX_PROTECTED_DEAD_LETTER_IDENTITIES + 1)
    ]
    with pytest.raises(operator.OperatorFailure, match="^protected_dead_letter_set_invalid$"):
        operator._parse_identity_list(oversized, code="protected_dead_letter_set_invalid")


@pytest.mark.parametrize(
    ("mutation", "failure_code"),
    (
        ({"backend_pid": BACKEND.main_pid + 1}, "stopped_snapshot_backend_identity_mismatch"),
        ({"backend_lease_owned": False}, "stopped_snapshot_backend_lease_not_owned"),
        ({"bridge_queue_state": "absent"}, "stopped_snapshot_bridge_queue_not_present"),
        (
            {"bridge_lease_acquired_for_snapshot": False},
            "stopped_snapshot_bridge_lease_not_acquired",
        ),
        ({"bridge_lease_released": False}, "stopped_snapshot_bridge_lease_not_released"),
        ({"physical_outbound_pending": 1}, "stopped_snapshot_outbound_not_empty"),
        ({"inbound_pending": 1}, "stopped_snapshot_inbound_not_empty"),
        ({"dead_letter": 1}, "stopped_snapshot_dead_letter_not_empty"),
    ),
)
def test_stopped_snapshot_failures_are_public_and_restore_without_battery_spawn(
    tmp_path,
    mutation,
    failure_code,
) -> None:
    config = _config(tmp_path)
    projection = {**_stopped_snapshot(BACKEND.main_pid), **mutation}
    runtime = FakeRuntime(config.barrier_dir, stopped_snapshot=projection)
    barrier = _barrier(tmp_path)
    try:
        report, exit_code = operator.execute_operator(config, runtime, barrier)
    finally:
        barrier.close()

    assert exit_code == 1
    assert report == {
        "schema": operator.OPERATOR_SCHEMA,
        "status": "failed",
        "commit": COMMIT,
        "duration_ms": report["duration_ms"],
        "failure_codes": [failure_code],
        "signal": None,
        "checks": {
            "preflight_clear": True,
            "bridge_stopped_clear": False,
            "bridge_guard_clear": False,
            "inter_run_observer_clear": False,
            "battery_clear": False,
            "backend_unchanged": False,
            "dispatcher_unchanged": False,
            "bridge_start_attempted": True,
            "bridge_online_after": True,
        },
        "evidence_sha256": {},
    }
    assert runtime.events.count("start") == 1
    assert runtime.start_calls == 1
    assert runtime.bridge_running is True
    assert "spawn" not in runtime.events
    assert runtime.child is None
    stopped_snapshot_index = runtime.events.index(
        "snapshot",
        runtime.events.index("snapshot") + 1,
    )
    assert stopped_snapshot_index < runtime.events.index("start")


@pytest.mark.parametrize(
    "projection",
    (
        {**_stopped_snapshot(BACKEND.main_pid), "private": "body"},
        {**_stopped_snapshot(BACKEND.main_pid), "schema": "private-schema"},
        {**_stopped_snapshot(BACKEND.main_pid), "backend_pid": True},
        {**_stopped_snapshot(BACKEND.main_pid), "backend_lease_owned": "private"},
        {**_stopped_snapshot(BACKEND.main_pid), "bridge_queue_state": "private-state"},
        {**_stopped_snapshot(BACKEND.main_pid), "bridge_queue_state": []},
        {**_stopped_snapshot(BACKEND.main_pid), "bridge_lease_acquired_for_snapshot": 1},
        {**_stopped_snapshot(BACKEND.main_pid), "bridge_lease_released": "private"},
        {**_stopped_snapshot(BACKEND.main_pid), "physical_outbound_pending": -1},
        {**_stopped_snapshot(BACKEND.main_pid), "inbound_pending": None},
        {**_stopped_snapshot(BACKEND.main_pid), "dead_letter": "private"},
        {
            **_stopped_snapshot(BACKEND.main_pid),
            "backend_pid": BACKEND.main_pid + 1,
            "dead_letter": "private",
        },
    ),
)
def test_stopped_snapshot_never_projects_uncertain_or_nonzero_state(projection) -> None:
    with pytest.raises(operator.OperatorFailure, match="^stopped_snapshot_invalid$"):
        operator._validate_stopped_snapshot(projection, BACKEND.main_pid)


def test_absent_pin_rejects_a_structurally_valid_nonempty_set() -> None:
    projection = _stopped_snapshot(BACKEND.main_pid, PROTECTED_IDENTITIES)
    with pytest.raises(operator.OperatorFailure, match="^stopped_snapshot_dead_letter_not_empty$"):
        operator._validate_stopped_snapshot(projection, BACKEND.main_pid)


@pytest.mark.parametrize(
    ("name", "observed"),
    (
        ("extra", (*PROTECTED_IDENTITIES, (303, "3" * 64))),
        ("missing", PROTECTED_IDENTITIES[:1]),
        ("replaced_update_id", ((101, "1" * 64), (303, "2" * 64))),
        ("mutated_metadata_fingerprint", ((101, "1" * 64), (202, "3" * 64))),
    ),
)
def test_stopped_snapshot_requires_exact_protected_metadata_identities(name, observed) -> None:
    del name
    protected = _protected_set()
    projection = _stopped_snapshot(BACKEND.main_pid, observed)
    with pytest.raises(
        operator.OperatorFailure,
        match="^stopped_snapshot_protected_dead_letter_set_mismatch$",
    ):
        operator._validate_stopped_snapshot(projection, BACKEND.main_pid, protected)


def test_stopped_snapshot_rejects_a_digest_inconsistent_with_its_identity_list() -> None:
    projection = _stopped_snapshot(BACKEND.main_pid, PROTECTED_IDENTITIES)
    projection["dead_letter_set_sha256"] = "f" * 64
    with pytest.raises(operator.OperatorFailure, match="^stopped_snapshot_invalid$"):
        operator._validate_stopped_snapshot(
            projection,
            BACKEND.main_pid,
            _protected_set(),
        )


@pytest.mark.parametrize(
    "projection",
    (
        {**_active_snapshot(BACKEND.main_pid), "physical_outbound_pending": 1},
        {**_active_snapshot(BACKEND.main_pid), "inbound_pending": 0},
        {**_active_snapshot(BACKEND.main_pid), "bridge_queue_state": "lease_unavailable"},
        {**_active_snapshot(BACKEND.main_pid), "bridge_lease_acquired_for_snapshot": True},
        {**_active_snapshot(BACKEND.main_pid), "dead_letter_set_sha256": "0" * 64},
        {**_active_snapshot(BACKEND.main_pid), "dead_letter_identities": []},
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


@pytest.mark.parametrize(
    "observed",
    (
        (*PROTECTED_IDENTITIES, (303, "3" * 64)),
        PROTECTED_IDENTITIES[:1],
        ((101, "1" * 64), (303, "2" * 64)),
        ((101, "1" * 64), (202, "3" * 64)),
    ),
)
def test_guarded_queue_rejects_any_protected_set_substitution(observed) -> None:
    with pytest.raises(
        operator.OperatorFailure,
        match="^guarded_queue_protected_dead_letter_set_mismatch$",
    ):
        operator._validate_guarded_queue(_guarded_snapshot(observed), _protected_set())


def test_guarded_queue_accepts_the_exact_metadata_identity_set() -> None:
    observed = operator._validate_guarded_queue(
        _guarded_snapshot(PROTECTED_IDENTITIES),
        _protected_set(),
    )
    assert observed.identities == PROTECTED_IDENTITIES


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


def test_live_bridge_stop_uses_cooperative_sigint_and_waits_for_clean_exit() -> None:
    runtime = object.__new__(operator.LiveRuntime)
    runtime.bridge_unit = "friday-bridge.service"
    commands: list[list[str]] = []
    states = iter(
        (
            {
                "ActiveState": "active",
                "SubState": "running",
                "MainPID": "43210",
                "ControlPID": "0",
            },
            {
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "ControlPID": "0",
            },
        )
    )
    clock = iter((0.0, 0.1))
    pauses: list[float] = []

    def run_command(command, *, timeout):  # noqa: ANN001
        assert timeout == operator.SYSTEMCTL_TIMEOUT_SEC
        rendered = list(command)
        commands.append(rendered)
        return subprocess.CompletedProcess(rendered, 0, b"", b"")

    runtime._run_command = run_command
    runtime._service_state = lambda _unit: next(states)
    runtime.monotonic = lambda: next(clock)
    runtime.pause = pauses.append

    runtime.stop_bridge()

    assert commands == [
        [
            operator._SYSTEMCTL_BINARY,
            "--user",
            "kill",
            "--kill-whom=main",
            "--signal=SIGINT",
            "friday-bridge.service",
        ]
    ]
    assert pauses == [operator.POLL_INTERVAL_SEC]


def _dispatcher_epoch_sglang_metrics(*, running: str = "0.0", waiting: str = "0.0") -> bytes:
    labels = 'engine_type="unified",model_name="dispatcher",moe_ep_rank="0",pp_rank="0",tp_rank="0"'
    return (
        f"sglang:num_running_reqs{{{labels}}} {running}\nsglang:num_queue_reqs{{{labels}}} {waiting}\n"
    ).encode()


def _dispatcher_epoch_server_info(*, random_seed: int = 786_846_033, **changes: Any) -> bytes:
    runtime_profile = PROFILES["qwen38-27b-nvfp4-sglang"]
    launch = runtime_profile.sglang_extra_args
    assert launch is not None
    value: dict[str, Any] = {
        "status": "ready",
        "version": runtime_profile.runtime_reported_version,
        "model_path": f"/models/{runtime_profile.model_dir_name}",
        "served_model_name": "dispatcher",
        "random_seed": random_seed,
        "context_length": runtime_profile.max_model_len,
        "max_running_requests": runtime_profile.max_num_seqs,
        "max_total_tokens": launch.max_total_tokens,
        "max_total_num_tokens": launch.max_total_tokens,
        "mem_fraction_static": launch.mem_fraction_static,
        "kv_cache_dtype": runtime_profile.kv_cache_dtype,
        "chunked_prefill_size": launch.chunked_prefill_size,
        "mamba_ssm_dtype": launch.mamba_ssm_dtype,
        "max_mamba_cache_size": launch.max_mamba_cache_size,
        "disable_radix_cache": not launch.radix_cache_enabled,
        "disable_cuda_graph": runtime_profile.eager_mode,
        "cuda_graph_backend_decode": launch.cuda_graph_backend_decode,
        "cuda_graph_max_bs_decode": launch.cuda_graph_max_bs_decode,
        "cuda_graph_bs_decode": list(launch.cuda_graph_bs_decode),
        "cuda_graph_backend_prefill": launch.cuda_graph_backend_prefill,
        "attention_backend": launch.attention_backend,
        "reasoning_parser": launch.reasoning_parser,
        "tool_call_parser": launch.tool_call_parser,
        "mm_feature_transport": launch.mm_feature_transport,
        "limit_mm_data_per_request": json.loads(launch.limit_mm_data_per_request),
        "enable_metrics": launch.metrics_enabled,
        "weight_version": launch.weight_version,
        "speculative_algorithm": launch.speculative_algorithm,
        "speculative_draft_model_path": None,
        "speculative_num_steps": None,
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":")).encode()


def _dispatcher_epoch_deployment_witness(
    *,
    random_seed: int = 786_846_033,
    nonce: str = "a" * 64,
    **changes: Any,
) -> bytes:
    runtime_profile = PROFILES["qwen38-27b-nvfp4-sglang"]
    value: dict[str, Any] = {
        "schema": "friday.sglang-deployment-witness.v1",
        "profile_id": QWEN38_27B_SGLANG_V12_PROFILE.profile_id,
        "engine_start_nonce": nonce,
        "engine_random_seed": random_seed,
        "engine_image_id": runtime_profile.engine_image_id,
        "engine_base_image_digest": runtime_profile.engine_base_image_digest,
        "engine_base_image_id": runtime_profile.engine_base_image_id,
        "runtime_source_revision": runtime_profile.runtime_source_revision,
        "runtime_reported_version": runtime_profile.runtime_reported_version,
        "model_repository": runtime_profile.model_repository,
        "model_revision": runtime_profile.model_revision,
        "model_snapshot_manifest_sha256": runtime_profile.model_snapshot_manifest_sha256,
        "model_quantization": runtime_profile.model_quantization,
        "served_model_alias": QWEN38_27B_SGLANG_V12_PROFILE.served_model_alias,
        "launch_manifest_sha256": runtime_profile.launch_manifest_sha256,
        "proxy_image_id": runtime_profile.proxy_image_id,
        "proxy_policy_sha256": runtime_profile.proxy_policy_sha256,
    }
    value.update(changes)
    return json.dumps(value, separators=(",", ":")).encode()


def _dispatcher_epoch_http_transport(
    bodies: dict[str, bytes],
    observed: list[httpx.Request],
    *,
    witness_after: bytes | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        path = request.url.path
        if path == "/_friday/v1/deployment-witness" and len(observed) == 4:
            body = bodies[path] if witness_after is None else witness_after
        else:
            body = bodies[path]
        return httpx.Response(200, content=body)

    return httpx.MockTransport(handler)


def _dispatcher_epoch_http_settings(settings):  # noqa: ANN001
    return replace(
        settings,
        profile=PROFILES["qwen38-27b-nvfp4-sglang"],
        llm_enabled=True,
        llm_model="dispatcher",
        llm_api_key="private-epoch-test-key",
        llm_base_url="http://127.0.0.1:8001/v1",
    )


_DISPATCHER_EPOCH_PATHS = (
    "/_friday/v1/deployment-witness",
    "/metrics",
    "/server_info",
    "/_friday/v1/deployment-witness",
)
_DISPATCHER_INVALID_SEEDS = (
    "null",
    "true",
    "0",
    "-1",
    "NaN",
    "Infinity",
    "-Infinity",
    "1.5",
    "1e0",
    '"1"',
    "1073741824",
)


@pytest.mark.parametrize(
    "case",
    (
        "valid",
        "minimum-seed",
        "maximum-seed",
        "missing-witness",
        "missing-seed",
        "duplicate-seed",
        "wrong-profile",
        "invalid-nonce",
        "missing-metric",
        "duplicate-metric",
        "nan-metric",
        "negative-metric",
        "missing-server-seed",
        "duplicate-server-seed",
        "wrong-server-model",
        "seed-mismatch",
        "torn-witness",
        *(f"witness-seed:{value}" for value in _DISPATCHER_INVALID_SEEDS),
        *(f"server-seed:{value}" for value in _DISPATCHER_INVALID_SEEDS),
    ),
)
def test_dispatcher_epoch_parser_requires_one_positive_finite_metric(settings, case: str) -> None:
    """Retain the registered name for V12's exact positive seed and complete epoch tuple.

    SGLang replaces the old decimal process-start metric with a deployment
    witness bracketing metrics/server_info. Only the HTTP responses are fake;
    the operator, router, transport, parsers and sampler execute unchanged.
    """
    witness_path, metrics_path, server_path, _ = _DISPATCHER_EPOCH_PATHS
    seed = {"minimum-seed": 1, "maximum-seed": (1 << 30) - 1}.get(case, 786_846_033)
    bodies = {
        witness_path: _dispatcher_epoch_deployment_witness(random_seed=seed),
        metrics_path: _dispatcher_epoch_sglang_metrics(),
        server_path: _dispatcher_epoch_server_info(random_seed=seed),
    }
    witness_after = None
    expected_calls = 4
    valid = case in {"valid", "minimum-seed", "maximum-seed"}
    if case.startswith(("witness-seed:", "server-seed:")):
        location, raw_seed = case.split(":", 1)
        path, key, expected_calls = (
            (witness_path, "engine_random_seed", 1)
            if location == "witness-seed"
            else (server_path, "random_seed", 3)
        )
        bodies[path] = bodies[path].replace(f'"{key}":{seed}'.encode(), f'"{key}":{raw_seed}'.encode())
    elif case in {"missing-witness", "missing-seed", "duplicate-seed", "wrong-profile", "invalid-nonce"}:
        expected_calls = 1
        if case == "missing-witness":
            bodies[witness_path] = b""
        elif case == "missing-seed":
            bodies[witness_path] = bodies[witness_path].replace(f'"engine_random_seed":{seed},'.encode(), b"")
        elif case == "duplicate-seed":
            bodies[witness_path] = bodies[witness_path][:-1] + f',"engine_random_seed":{seed}}}'.encode()
        elif case == "wrong-profile":
            bodies[witness_path] = _dispatcher_epoch_deployment_witness(profile_id="wrong-profile")
        else:
            bodies[witness_path] = _dispatcher_epoch_deployment_witness(nonce="A" * 64)
    elif case in {"missing-metric", "duplicate-metric", "nan-metric", "negative-metric"}:
        expected_calls = 2
        if case == "missing-metric":
            bodies[metrics_path] = bodies[metrics_path].splitlines(keepends=True)[0]
        elif case == "duplicate-metric":
            bodies[metrics_path] += bodies[metrics_path].splitlines(keepends=True)[0]
        else:
            bodies[metrics_path] = _dispatcher_epoch_sglang_metrics(
                running="NaN" if case == "nan-metric" else "-1"
            )
    elif case in {"missing-server-seed", "duplicate-server-seed", "wrong-server-model"}:
        expected_calls = 3
        if case == "missing-server-seed":
            bodies[server_path] = bodies[server_path].replace(f'"random_seed":{seed},'.encode(), b"")
        elif case == "duplicate-server-seed":
            bodies[server_path] = bodies[server_path][:-1] + f',"random_seed":{seed}}}'.encode()
        else:
            bodies[server_path] = _dispatcher_epoch_server_info(served_model_name="wrong")
    elif case == "seed-mismatch":
        bodies[server_path] = _dispatcher_epoch_server_info(random_seed=seed + 1)
    elif case == "torn-witness":
        witness_after = _dispatcher_epoch_deployment_witness(nonce="b" * 64)
    else:
        assert valid, case

    observed: list[httpx.Request] = []
    transport = _dispatcher_epoch_http_transport(bodies, observed, witness_after=witness_after)
    configured = _dispatcher_epoch_http_settings(settings)
    if valid:
        epoch = asyncio.run(operator._sample_dispatcher_epoch(configured, http_transport=transport))
        expected = json.dumps(json.loads(bodies[witness_path]), sort_keys=True, separators=(",", ":"))
        assert epoch == hashlib.sha256(expected.encode()).hexdigest()
    else:
        with pytest.raises(operator.OperatorFailure, match="^dispatcher_identity_failed$"):
            asyncio.run(operator._sample_dispatcher_epoch(configured, http_transport=transport))

    assert [(request.method, str(request.url)) for request in observed] == [
        ("GET", f"http://127.0.0.1:8001{path}") for path in _DISPATCHER_EPOCH_PATHS[:expected_calls]
    ]
    assert all(request.headers["authorization"] == "Bearer private-epoch-test-key" for request in observed)


def test_dispatcher_epoch_comparison_does_not_collapse_distinct_decimal_samples(settings) -> None:
    """Keep legacy precision coverage as exact V12 nonce/seed identity over HTTP.

    Decimal process-start samples are no longer identity inputs. Decimal-looking
    nonces that would collapse through float conversion must stay distinct;
    JSON formatting and equivalent load metric notation must stay stable.
    """
    witness_path, metrics_path, server_path, _ = _DISPATCHER_EPOCH_PATHS
    first_nonce = "0" * 48 + "9007199254740992"
    second_nonce = "0" * 48 + "9007199254740993"
    assert first_nonce != second_nonce and float(first_nonce) == float(second_nonce)
    configured = _dispatcher_epoch_http_settings(settings)
    epochs: list[str] = []
    for nonce, seed, reorder in (
        (first_nonce, 41, False),
        (first_nonce, 41, True),
        (second_nonce, 41, False),
        (second_nonce, 42, False),
    ):
        witness = _dispatcher_epoch_deployment_witness(random_seed=seed, nonce=nonce)
        reformatted = json.dumps(dict(reversed(tuple(json.loads(witness).items()))), indent=2).encode()
        bodies = {
            witness_path: reformatted if reorder else witness,
            metrics_path: _dispatcher_epoch_sglang_metrics(running="1e0" if reorder else "1.0"),
            server_path: _dispatcher_epoch_server_info(random_seed=seed),
        }
        observed: list[httpx.Request] = []
        transport = _dispatcher_epoch_http_transport(bodies, observed, witness_after=reformatted)
        epoch = asyncio.run(operator._sample_dispatcher_epoch(configured, http_transport=transport))
        canonical = json.dumps(json.loads(witness), sort_keys=True, separators=(",", ":")).encode()
        assert epoch == hashlib.sha256(canonical).hexdigest()
        epochs.append(epoch)
        assert [str(request.url) for request in observed] == [
            f"http://127.0.0.1:8001{path}" for path in _DISPATCHER_EPOCH_PATHS
        ]

    assert epochs[0] == epochs[1]
    assert len({epochs[0], epochs[2], epochs[3]}) == 3


def test_dispatcher_epoch_sampler_uses_registered_q38_identity_tuple(monkeypatch) -> None:
    settings = SimpleNamespace(
        profile=SimpleNamespace(name="qwen38-27b-nvfp4-sglang"),
        llm_model="dispatcher",
        llm_enabled=True,
    )
    router = object()
    transport = object()
    seam = object()
    observed: dict[str, Any] = {}

    monkeypatch.setattr(operator, "LLMRouter", lambda value: router if value is settings else None)

    def create_transport(value, *, http_transport=None):  # noqa: ANN001
        observed["router"] = value
        observed["seam"] = http_transport
        return transport

    async def sample(value, *, profile, metrics_deadline, absolute_deadline):  # noqa: ANN001
        observed["transport"] = value
        observed["profile"] = profile
        observed["deadline_equal"] = metrics_deadline == absolute_deadline
        return SimpleNamespace(process_epoch_sha256="a" * 64), None

    monkeypatch.setattr(operator, "RouterV12MetricsTransport", create_transport)
    monkeypatch.setattr(operator, "_sample_sglang_load_without_raw_retention", sample)

    assert asyncio.run(operator._sample_dispatcher_epoch(settings, http_transport=seam)) == "a" * 64
    assert observed == {
        "router": router,
        "seam": seam,
        "transport": transport,
        "profile": operator.QWEN38_27B_SGLANG_V12_PROFILE,
        "deadline_equal": True,
    }


def test_dispatcher_epoch_sampler_rejects_partial_tuple_and_wrong_profile(monkeypatch) -> None:
    q38 = SimpleNamespace(
        profile=SimpleNamespace(name="qwen38-27b-nvfp4-sglang"),
        llm_model="dispatcher",
        llm_enabled=True,
    )
    monkeypatch.setattr(operator, "LLMRouter", lambda _settings: object())
    monkeypatch.setattr(operator, "RouterV12MetricsTransport", lambda *_args, **_kwargs: object())

    async def rejected(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return None, SimpleNamespace(value="metrics_invalid")

    monkeypatch.setattr(operator, "_sample_sglang_load_without_raw_retention", rejected)
    with pytest.raises(operator.OperatorFailure, match="dispatcher_identity_failed"):
        asyncio.run(operator._sample_dispatcher_epoch(q38))

    wrong = SimpleNamespace(
        profile=SimpleNamespace(name="qwen36-27b-nvfp4-nvidia"),
        llm_model="dispatcher",
        llm_enabled=True,
    )
    with pytest.raises(operator.OperatorFailure, match="dispatcher_profile_mismatch"):
        asyncio.run(operator._sample_dispatcher_epoch(wrong))


def test_live_runtime_dispatcher_epoch_uses_sync_boundary(monkeypatch) -> None:
    runtime = object.__new__(operator.LiveRuntime)
    runtime.settings = object()

    async def sample(settings):  # noqa: ANN001
        assert settings is runtime.settings
        return "b" * 64

    monkeypatch.setattr(operator, "_sample_dispatcher_epoch", sample)
    assert runtime.dispatcher_epoch() == "b" * 64


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


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"FRIDAY_API_TOKEN=primary\nJERICHO_API_TOKEN=legacy\n", "primary"),
        (b"JERICHO_API_TOKEN=legacy\nFRIDAY_API_TOKEN=primary\n", "primary"),
        (b"FRIDAY_API_TOKEN=\nJERICHO_API_TOKEN=legacy\n", ""),
        (b"JERICHO_API_TOKEN=legacy\n", "legacy"),
        (b"FRIDAY_API_TOKEN=same\nJERICHO_API_TOKEN=same\n", "same"),
        (b"export FRIDAY_API_TOKEN='$literal value'\n", "$literal value"),
    ],
    ids=["primary", "reversed-order", "empty-primary", "legacy", "equal", "literal"],
)
def test_env_parser_preserves_product_alias_precedence(monkeypatch, content, expected) -> None:
    from friday.config import env

    values = operator._parse_env(content)
    for name in ("FRIDAY_API_TOKEN", "JERICHO_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    assert env("FRIDAY_API_TOKEN") == expected


@pytest.mark.parametrize(
    "content",
    [
        b"FRIDAY_API_TOKEN=a\nFRIDAY_API_TOKEN=b\n",
        b"JERICHO_API_TOKEN=a\nJERICHO_API_TOKEN=b\n",
        b"NOT-A-KEY=value\n",
        b"FRIDAY_API_TOKEN\n",
        b"\xff\n",
    ],
    ids=["duplicate-primary", "duplicate-legacy", "invalid-key", "missing-equals", "utf8"],
)
def test_env_parser_keeps_invalid_syntax_closed(content) -> None:
    with pytest.raises(operator.OperatorFailure, match="^env_file_invalid$"):
        operator._parse_env(content)


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
    shared_parent.chmod(0o755)
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
        assert len(calls) == 1
        backend_client = calls[0]
        assert backend_client["headers"]["Authorization"] == "Bearer OWNER-TOKEN-IN-MEMORY"
        assert backend_client["trust_env"] is False
        assert backend_client["follow_redirects"] is False
        assert backend_client["verify"] is True
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
    backend_fd = os.open("/dev/null", os.O_RDONLY)
    bridge_fd = os.open("/dev/null", os.O_RDONLY)
    runtime._backend_pidfd = backend_fd
    runtime._bridge_pidfd = bridge_fd
    with pytest.raises(operator.OperatorFailure, match="runtime_close_failed"):
        runtime.close()
    assert events == ["backend"]
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
            "3749abdbe19080d7f4108809df9508b88306d9ae7d006a0c616aa02190e782a0"
        ),
        "friday/diagnostics/__init__.py": (
            "0add438a16df83036a29e3fa68bf98425f3228d770ba2951bbe25985bdc1383a"
        ),
        "friday/diagnostics/runtime_lease.py": (
            "6986bcef0d21d1754672ad784746fbc205b4822de708c71b16dd93576f3d1926"
        ),
        "friday/admin_api/_overview.py": ("322873e979178985e2e3cffc7d2c8690cc97f27c5970f9a06db9b1fb9e524677"),
        "friday/config/__init__.py": ("1f78d346285fc75f6794d13ed0c48ec0aca82ef6e8792a47554ab72b0754dbcb"),
        "friday/agent_runtime/llm.py": ("67e68a012c7d94a24244579923341e18287a7215610ba2a3330bded612e71ec6"),
        "friday/model_profiles.py": ("21ea29d72540b07b26a54bfe927a806aa8aa1f9799578f69bfaa055042e3ac67"),
        "friday/v12_model_runtime.py": ("ea1c6b700a11114fdc3799fe9d744e0a6a7d527cb0cef949300d6fb5d254b3d7"),
        "friday/v12_model_transport.py": ("3f8d4c4e8ed513696a4c9ac39aad845595c46dad597b105f1d41c92e696f0363"),
    }


def test_declared_dependency_hashes_match_assembled_source_bytes() -> None:
    assembled_root = Path(__file__).resolve().parents[1]
    for relative, expected in operator._EXPECTED_DEPENDENCY_HASHES.items():
        dependency = assembled_root / relative
        assert dependency.is_file(), relative
        observed = hashlib.sha256(dependency.read_bytes()).hexdigest()
        assert observed == expected, relative
