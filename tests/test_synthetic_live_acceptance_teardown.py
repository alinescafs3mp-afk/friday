"""Exclusive-run and fail-fast teardown regressions for live acceptance."""

from __future__ import annotations

import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_acceptance as acceptance  # noqa: E402
import synthetic_live_battery as battery  # noqa: E402


def test_operator_home_lock_is_release_independent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from friday import config

    operator_home = tmp_path / "operator-home"
    monkeypatch.setattr(config, "default_home", lambda: operator_home)

    first_root = tmp_path / "runtime" / "releases" / "candidate-a"
    second_root = tmp_path / "runtime" / "releases" / "candidate-b"
    monkeypatch.setattr(acceptance, "ROOT", first_root)
    first = acceptance._acceptance_lock_path()
    monkeypatch.setattr(acceptance, "ROOT", second_root)
    second = acceptance._acceptance_lock_path()

    assert first == second
    assert first == operator_home / "runtime" / "locks" / "synthetic-live-acceptance.lock"


def test_exclusive_lock_fails_fast_and_is_reusable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lock_path = tmp_path / "runtime" / "locks" / "acceptance.lock"
    monkeypatch.setattr(battery, "_assert_ignored_or_external", lambda _path: None)

    with acceptance._ExclusiveAcceptanceRun(lock_path):
        started = time.monotonic()
        with (
            pytest.raises(battery.BatteryContractError, match="acceptance_run_already_active"),
            acceptance._ExclusiveAcceptanceRun(lock_path),
        ):
            raise AssertionError("a second acceptance must never enter")
        assert time.monotonic() - started < 0.5

    with acceptance._ExclusiveAcceptanceRun(lock_path):
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o700


def test_run_lock_covers_the_entire_locked_body(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lock_path = tmp_path / "runtime" / "locks" / "acceptance.lock"
    observed = False

    monkeypatch.setattr(battery, "_assert_ignored_or_external", lambda _path: None)
    monkeypatch.setattr(acceptance, "_acceptance_lock_path", lambda: lock_path)

    def fake_locked(*_args: Any, **_kwargs: Any) -> tuple[int, dict[str, Any]]:
        nonlocal observed
        with (
            pytest.raises(battery.BatteryContractError, match="acceptance_run_already_active"),
            acceptance._ExclusiveAcceptanceRun(lock_path),
        ):
            raise AssertionError("readiness/dispatch body ran without its lock")
        observed = True
        return 4, {"status": "red"}

    monkeypatch.setattr(acceptance, "_run_acceptance_locked", fake_locked)

    code, summary = acceptance.run_acceptance(
        "p06",
        run_directory=tmp_path / "unused",
        concurrency=1,
        artifact_id="PRE-RELEASE-P06-0123456789abcdef",
    )

    assert (code, summary) == (4, {"status": "red"})
    assert observed is True
    with acceptance._ExclusiveAcceptanceRun(lock_path):
        pass


def test_sigterm_unwinds_the_locked_run_and_restores_the_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "runtime" / "locks" / "acceptance.lock"
    previous = signal.getsignal(signal.SIGTERM)

    monkeypatch.setattr(battery, "_assert_ignored_or_external", lambda _path: None)
    monkeypatch.setattr(acceptance, "_acceptance_lock_path", lambda: lock_path)

    def terminate(*_args: Any, **_kwargs: Any) -> tuple[int, dict[str, Any]]:
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("SIGTERM handler did not unwind the run")

    monkeypatch.setattr(acceptance, "_run_acceptance_locked", terminate)

    with pytest.raises(acceptance._AcceptanceTerminationRequested):
        acceptance.run_acceptance(
            "p06",
            run_directory=tmp_path / "unused",
            concurrency=1,
            artifact_id="PRE-RELEASE-P06-0123456789abcdef",
        )

    assert signal.getsignal(signal.SIGTERM) is previous
    with acceptance._ExclusiveAcceptanceRun(lock_path):
        pass


def test_base_exception_kills_and_reaps_every_started_worker_group(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "acceptance"
    run_root.mkdir(mode=0o700)
    sealed = acceptance._preseal_passes("p06", run_root, acceptance._load_manifests())
    candidate_files = (
        "tools/synthetic_live_acceptance.py",
        "tools/synthetic_live_battery.py",
    )
    candidate_digest = "a" * 64
    started = threading.Event()
    reaped = threading.Event()
    killed: list[tuple[int, signal.Signals]] = []
    model_environment = {"FRIDAY_LLM_BASE_URL": "http://127.0.0.1:8001/v1"}

    class AbortRun(BaseException):
        pass

    class FakeProcess:
        pid = 43210

        def poll(self) -> int | None:
            return 137 if reaped.is_set() else None

        def wait(self, timeout: float | None = None) -> int:
            assert timeout is not None
            assert 0 < timeout <= acceptance.WORKER_TEARDOWN_WAIT_SEC
            reaped.set()
            return 137

        def kill(self) -> None:
            reaped.set()

    process = FakeProcess()

    def fake_popen(*_args: Any, **kwargs: Any) -> FakeProcess:
        assert kwargs["start_new_session"] is True
        started.set()
        return process

    class FakeExecutor:
        def __init__(self, environment: dict[str, str], *, instrument_path: Path) -> None:
            assert environment == model_environment
            assert instrument_path == acceptance.RUNNER_PATH
            self._candidate_files = candidate_files
            self._candidate_source_sha256 = candidate_digest

        def __enter__(self) -> FakeExecutor:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def _assert_candidate_unchanged(self) -> None:
            return None

        def __call__(self, _manifest: Any, _spec: Any, _cases: Any, context: Any) -> Any:
            if context.pass_id == "A-P06":
                battery.subprocess.Popen(["synthetic-worker"], start_new_session=True)
                assert reaped.wait(timeout=1.0), "teardown did not reap the blocked worker"
                raise RuntimeError("private detail")
            assert started.wait(timeout=1.0), "abort raced ahead of worker registration"
            raise AbortRun()

    monkeypatch.setattr(battery, "_candidate_source_paths", lambda **_kwargs: candidate_files)
    monkeypatch.setattr(battery, "_candidate_source_digest", lambda **_kwargs: candidate_digest)
    monkeypatch.setattr(battery, "SubprocessPassExecutor", FakeExecutor)
    monkeypatch.setattr(battery.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        acceptance.os,
        "killpg",
        lambda pid, sig: killed.append((pid, signal.Signals(sig))),
    )

    started_at = time.monotonic()
    with pytest.raises(AbortRun):
        acceptance._execute_sealed(
            sealed,
            concurrency=2,
            model_environment=model_environment,
        )

    assert time.monotonic() - started_at < 1.0
    assert killed == [(process.pid, signal.SIGKILL)]
    assert reaped.is_set()
    assert battery.subprocess.Popen is fake_popen


def test_cancellation_hostile_worker_forces_process_exit_before_proxy_or_lock_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "acceptance"
    run_root.mkdir(mode=0o700)
    sealed = acceptance._preseal_passes("p06", run_root, acceptance._load_manifests())
    candidate_files = (
        "tools/synthetic_live_acceptance.py",
        "tools/synthetic_live_battery.py",
    )
    candidate_digest = "a" * 64
    hostile_started = threading.Event()
    release_hostile = threading.Event()
    hostile_finished = threading.Event()
    model_environment = {"FRIDAY_LLM_BASE_URL": "http://127.0.0.1:8001/v1"}

    class AbortRun(BaseException):
        pass

    class HardExitObserved(BaseException):
        pass

    class FakeExecutor:
        def __init__(self, _environment: dict[str, str], *, instrument_path: Path) -> None:
            assert instrument_path == acceptance.RUNNER_PATH
            self._candidate_files = candidate_files
            self._candidate_source_sha256 = candidate_digest

        def __enter__(self) -> FakeExecutor:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def _assert_candidate_unchanged(self) -> None:
            return None

        def __call__(self, _manifest: Any, _spec: Any, _cases: Any, context: Any) -> Any:
            if context.pass_id == "A-P06":
                hostile_started.set()
                release_hostile.wait()
                hostile_finished.set()
                raise RuntimeError("private detail")
            assert hostile_started.wait(timeout=1.0)
            raise AbortRun()

    monkeypatch.setattr(battery, "_candidate_source_paths", lambda **_kwargs: candidate_files)
    monkeypatch.setattr(battery, "_candidate_source_digest", lambda **_kwargs: candidate_digest)
    monkeypatch.setattr(battery, "SubprocessPassExecutor", FakeExecutor)
    monkeypatch.setattr(acceptance, "WORKER_THREAD_TEARDOWN_WAIT_SEC", 0.02)

    def observe_hard_exit() -> None:
        assert isinstance(battery.subprocess, acceptance._TrackedSubprocessModule)
        raise HardExitObserved()

    monkeypatch.setattr(
        acceptance,
        "_hard_exit_after_incomplete_teardown",
        observe_hard_exit,
    )

    try:
        with pytest.raises(HardExitObserved):
            acceptance._execute_sealed(
                sealed,
                concurrency=2,
                model_environment=model_environment,
            )
    finally:
        release_hostile.set()

    assert hostile_finished.wait(timeout=1.0)


def test_registry_refuses_spawn_after_teardown_begins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        battery.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("stopped registry must not call Popen"),
    )

    with acceptance._WorkerProcessRegistry() as registry:
        assert registry.stop_and_reap() is True
        with pytest.raises(
            battery.BatteryContractError,
            match="acceptance_worker_dispatch_stopped",
        ):
            battery.subprocess.Popen(["synthetic-worker"], start_new_session=True)


def test_killpg_error_is_reported_without_skipping_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waited = False

    class FakeProcess:
        pid = 43211

        def poll(self) -> int | None:
            return 0 if waited else None

        def wait(self, timeout: float | None = None) -> int:
            nonlocal waited
            assert timeout is not None and timeout > 0
            waited = True
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr(
        acceptance.os,
        "killpg",
        lambda *_args: (_ for _ in ()).throw(PermissionError("denied")),
    )

    with acceptance._WorkerProcessRegistry() as registry:
        registry._processes[FakeProcess.pid] = FakeProcess()  # noqa: SLF001 - lifecycle mutation test
        assert registry.stop_and_reap() is False

    assert waited is True
