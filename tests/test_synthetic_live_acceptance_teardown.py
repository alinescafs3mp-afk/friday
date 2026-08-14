"""Exclusive-run and fail-fast teardown regressions for live acceptance."""

from __future__ import annotations

import fcntl
import os
import select
import signal
import stat
import subprocess
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


@pytest.fixture
def isolated_acceptance_anchor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    protocol = f"friday.synthetic-live-acceptance.unit.{os.getpid()}.{tmp_path.name}"
    monkeypatch.setattr(acceptance, "_ACCEPTANCE_LOCK_PROTOCOL", protocol.encode("ascii"))


def test_uid_host_lock_is_home_and_release_independent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "runtime" / "releases" / "candidate-a"
    second_root = tmp_path / "runtime" / "releases" / "candidate-b"
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "home-a"))
    monkeypatch.setenv("HOME", str(tmp_path / "account-a"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-a"))
    monkeypatch.setattr(acceptance, "ROOT", first_root)
    first = acceptance._acceptance_lock_path()
    first_anchor = acceptance._acceptance_anchor_address()
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "home-b"))
    monkeypatch.setenv("HOME", str(tmp_path / "account-b"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-b"))
    monkeypatch.setattr(acceptance, "ROOT", second_root)
    second = acceptance._acceptance_lock_path()
    second_anchor = acceptance._acceptance_anchor_address()

    assert first == second
    assert first_anchor == second_anchor
    assert first_anchor.startswith(b"\0friday.synthetic-live-acceptance.")
    assert first == (
        Path("/tmp")
        / f"friday-synthetic-live-acceptance-{os.getuid()}"
        / "runtime"
        / "locks"
        / "synthetic-live-acceptance.lock"
    )


_LOCK_SUBPROCESS = r"""
import os
import signal
import sys
from pathlib import Path

repository = Path(os.environ["ACCEPTANCE_TEST_REPOSITORY"])
sys.path.insert(0, str(repository / "tools"))
import synthetic_live_acceptance as acceptance
import synthetic_live_battery as battery

release_root = Path(os.environ["ACCEPTANCE_TEST_RELEASE_ROOT"])
acceptance.ROOT = release_root
battery.ROOT = release_root
acceptance._ACCEPTANCE_LOCK_HOST_ROOT = Path(os.environ["ACCEPTANCE_TEST_LOCK_HOST_ROOT"])
acceptance._ACCEPTANCE_LOCK_PROTOCOL = os.environ["ACCEPTANCE_TEST_LOCK_PROTOCOL"].encode("ascii")
mode = sys.argv[1]
lock_path = acceptance._acceptance_lock_path()

class AbortRun(BaseException):
    pass

try:
    with acceptance._TerminationSignalGuard(), acceptance._ExclusiveAcceptanceRun(lock_path):
        print(f"LOCKED\t{lock_path}", flush=True)
        if mode == "hold":
            sys.stdin.buffer.read(1)
        elif mode == "exception":
            raise RuntimeError("expected")
        elif mode == "baseexception":
            raise AbortRun()
        elif mode == "sigterm":
            os.kill(os.getpid(), signal.SIGTERM)
        elif mode != "normal":
            raise AssertionError(f"unknown mode: {mode}")
except battery.BatteryContractError as exc:
    if str(exc) == "acceptance_run_already_active":
        print(f"BUSY\t{lock_path}", flush=True)
        raise SystemExit(23)
    raise
except RuntimeError:
    if mode != "exception":
        raise
except AbortRun:
    if mode != "baseexception":
        raise
except acceptance._AcceptanceTerminationRequested:
    if mode != "sigterm":
        raise
"""


def _lock_process_environment(
    *,
    home: Path,
    release_root: Path,
    host_root: Path,
    protocol: str,
) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "ACCEPTANCE_TEST_LOCK_HOST_ROOT": str(host_root),
            "ACCEPTANCE_TEST_LOCK_PROTOCOL": protocol,
            "ACCEPTANCE_TEST_RELEASE_ROOT": str(release_root),
            "ACCEPTANCE_TEST_REPOSITORY": str(ROOT),
            "FRIDAY_HOME": str(home),
            "HOME": str(home),
            "XDG_RUNTIME_DIR": str(home / "xdg"),
        }
    )
    return environment


def _run_lock_process(
    mode: str,
    *,
    home: Path,
    release_root: Path,
    host_root: Path,
    protocol: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - exact interpreter and code-owned test program
        [sys.executable, "-c", _LOCK_SUBPROCESS, mode],
        cwd=release_root,
        env=_lock_process_environment(
            home=home,
            release_root=release_root,
            host_root=host_root,
            protocol=protocol,
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=10.0,
    )


def test_process_lock_serializes_isolated_homes_and_survives_every_unwind(tmp_path: Path) -> None:
    first_home = tmp_path / "home-a"
    second_home = tmp_path / "home-b"
    first_release = tmp_path / "releases" / "candidate-a"
    second_release = tmp_path / "releases" / "candidate-b"
    host_root = tmp_path / "host-lock-root"
    protocol = f"friday.synthetic-live-acceptance.test.{os.getpid()}.{tmp_path.name}"
    for directory in (
        first_home,
        first_home / "xdg",
        second_home,
        second_home / "xdg",
        first_release,
        second_release,
        host_root,
    ):
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)

    holder = subprocess.Popen(  # noqa: S603 - exact interpreter and code-owned test program
        [sys.executable, "-c", _LOCK_SUBPROCESS, "hold"],
        cwd=first_release,
        env=_lock_process_environment(
            home=first_home,
            release_root=first_release,
            host_root=host_root,
            protocol=protocol,
        ),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        readable, _, _ = select.select([holder.stdout], [], [], 10.0)
        assert readable, "holder did not acquire the acceptance lock"
        holder_line = holder.stdout.readline().strip()
        assert holder_line.startswith("LOCKED\t")

        started = time.monotonic()
        contender = _run_lock_process(
            "normal",
            home=second_home,
            release_root=second_release,
            host_root=host_root,
            protocol=protocol,
        )
        assert time.monotonic() - started < 2.0
        assert contender.returncode == 23, contender.stderr
        assert contender.stdout == holder_line.replace("LOCKED\t", "BUSY\t") + "\n"

        lock_path = Path(holder_line.partition("\t")[2])
        assert lock_path.is_relative_to(host_root)
        held_inode_path = lock_path.with_name("synthetic-live-acceptance.held")
        lock_path.rename(held_inode_path)
        replacement_canary = b"replacement-must-remain-untouched"
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, replacement_canary)
        finally:
            os.close(descriptor)
        replacement = _run_lock_process(
            "normal",
            home=second_home,
            release_root=second_release,
            host_root=host_root,
            protocol=protocol,
        )
        assert replacement.returncode == 23, replacement.stderr
        assert lock_path.read_bytes() == replacement_canary
    finally:
        if holder.poll() is None and holder.stdin is not None:
            try:
                holder.stdin.write("x")
                holder.stdin.close()
            except BrokenPipeError:
                pass
        try:
            holder.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=10.0)
    assert holder.returncode == 0
    assert held_inode_path.is_file()

    for mode in ("normal", "exception", "baseexception", "sigterm"):
        unwound = _run_lock_process(
            mode,
            home=first_home,
            release_root=first_release,
            host_root=host_root,
            protocol=protocol,
        )
        assert unwound.returncode == 0, unwound.stderr
        assert unwound.stdout.startswith("LOCKED\t")
        reusable = _run_lock_process(
            "normal",
            home=second_home,
            release_root=second_release,
            host_root=host_root,
            protocol=protocol,
        )
        assert reusable.returncode == 0, reusable.stderr
        assert reusable.stdout == unwound.stdout


def test_exclusive_lock_fails_fast_and_is_reusable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_acceptance_anchor: None,
) -> None:
    del isolated_acceptance_anchor
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


def test_exclusive_lock_rejects_preexisting_non_private_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "runtime" / "locks" / "acceptance.lock"
    lock_path.parent.mkdir(parents=True, mode=0o700)
    lock_path.parent.chmod(0o750)
    monkeypatch.setattr(battery, "_assert_ignored_or_external", lambda _path: None)

    class FakeAnchor:
        closed = False

        def close(self) -> None:
            self.closed = True

    anchor = FakeAnchor()
    monkeypatch.setattr(acceptance, "_open_acceptance_anchor", lambda: anchor)

    with (
        pytest.raises(battery.BatteryContractError, match="acceptance_lock_unavailable"),
        acceptance._ExclusiveAcceptanceRun(lock_path),
    ):
        raise AssertionError("an unsafe lock directory must never be repaired and entered")

    assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o750
    assert not lock_path.exists()
    assert anchor.closed is True


def test_kernel_anchor_failure_precedes_every_filesystem_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "runtime" / "locks" / "acceptance.lock"
    filesystem_preflight_called = False

    def refuse_anchor() -> None:
        raise battery.BatteryContractError("acceptance_lock_unavailable")

    def observe_filesystem_preflight(_path: Path) -> None:
        nonlocal filesystem_preflight_called
        filesystem_preflight_called = True

    monkeypatch.setattr(acceptance, "_open_acceptance_anchor", refuse_anchor)
    monkeypatch.setattr(battery, "_assert_ignored_or_external", observe_filesystem_preflight)

    with (
        pytest.raises(battery.BatteryContractError, match="acceptance_lock_unavailable"),
        acceptance._ExclusiveAcceptanceRun(lock_path),
    ):
        raise AssertionError("a missing kernel anchor must never downgrade to the file lock")

    assert filesystem_preflight_called is False
    assert not lock_path.parent.parent.exists()


def test_file_lock_is_released_before_the_kernel_anchor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "runtime" / "locks" / "acceptance.lock"
    observed: list[str] = []
    monkeypatch.setattr(battery, "_assert_ignored_or_external", lambda _path: None)

    class AnchorProbe:
        def close(self) -> None:
            descriptor = os.open(lock_path, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                observed.append("file_released_before_anchor")
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    monkeypatch.setattr(acceptance, "_open_acceptance_anchor", AnchorProbe)

    with acceptance._ExclusiveAcceptanceRun(lock_path):
        observed.append("body")

    assert observed == ["body", "file_released_before_anchor"]


def test_run_lock_covers_the_entire_locked_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_acceptance_anchor: None,
) -> None:
    del isolated_acceptance_anchor
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
    isolated_acceptance_anchor: None,
) -> None:
    del isolated_acceptance_anchor
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


def test_thread_start_interruption_cannot_escape_worker_teardown(
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
    worker_entered = threading.Event()
    release_worker = threading.Event()
    model_environment = {"FRIDAY_LLM_BASE_URL": "http://127.0.0.1:8001/v1"}

    class StartInterrupted(BaseException):
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

        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            worker_entered.set()
            release_worker.wait()
            raise RuntimeError("private detail")

    original_start = threading.Thread.start
    starts = 0

    def interrupt_second_start(thread: threading.Thread) -> None:
        nonlocal starts
        starts += 1
        if starts == 2:
            assert worker_entered.wait(timeout=1.0)
            raise StartInterrupted()
        original_start(thread)

    def observe_hard_exit() -> None:
        assert isinstance(battery.subprocess, acceptance._TrackedSubprocessModule)
        raise HardExitObserved()

    monkeypatch.setattr(battery, "_candidate_source_paths", lambda **_kwargs: candidate_files)
    monkeypatch.setattr(battery, "_candidate_source_digest", lambda **_kwargs: candidate_digest)
    monkeypatch.setattr(battery, "SubprocessPassExecutor", FakeExecutor)
    monkeypatch.setattr(threading.Thread, "start", interrupt_second_start)
    monkeypatch.setattr(acceptance, "WORKER_THREAD_TEARDOWN_WAIT_SEC", 0.02)
    monkeypatch.setattr(acceptance, "_hard_exit_after_incomplete_teardown", observe_hard_exit)

    try:
        with pytest.raises(HardExitObserved):
            acceptance._execute_sealed(
                sealed,
                concurrency=2,
                model_environment=model_environment,
            )
    finally:
        release_worker.set()


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
