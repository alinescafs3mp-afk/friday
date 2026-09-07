"""Process lifecycle checks. Mocks here do not certify Linux namespace isolation."""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

import friday.organs.coding.worker_cgroup as coding_tree
import friday.organs.coding.worker_spawn as worker
from friday.organs.coding.worker_programs import ORACLE, PROBE, TEST


def _argv(tmp_path: Path, source: str = PROBE):
    return worker.coding_worker_bwrap_argv(
        worker_root=str(tmp_path / "worker"),
        workspace_path="work/op",
        export_path="out/op",
        hazards=(),
        uid=1000,
        gid=1000,
        python_c=source,
    )


@pytest.mark.parametrize("source", (TEST, ORACLE, "print('unexpected program')"))
def test_default_runner_refuses_uploaded_execution_before_popen(tmp_path, monkeypatch, source) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("untrusted execution reached Popen")

    monkeypatch.setattr(coding_tree, "coding_tree_enforcement_available", lambda: False)
    monkeypatch.setattr(worker.subprocess, "Popen", forbidden)
    monkeypatch.setattr(coding_tree.subprocess, "Popen", forbidden)
    monkeypatch.setattr(coding_tree.subprocess, "run", forbidden)
    assert worker.default_coding_worker_runner(_argv(tmp_path, source), 1) == 126


def test_runner_discards_output_and_does_not_inherit_stdin_or_environment(tmp_path, monkeypatch) -> None:
    calls = []

    class Process:
        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def popen(argv, **kwargs):
        calls.append(kwargs)
        return Process()

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    assert worker.default_coding_worker_runner(_argv(tmp_path), 3) == 0
    kwargs = calls[0]
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert "capture_output" not in kwargs
    assert kwargs["start_new_session"] is True
    assert set(kwargs["env"]) == {"PATH", "HOME", "LANG"}


@pytest.mark.parametrize("failure", ("timeout", "interrupt"))
def test_interrupted_runner_kills_and_reaps_its_process_group(tmp_path, monkeypatch, failure) -> None:
    waits = []
    kills = []

    class Process:
        pid = 12345

        def wait(self, timeout=None):
            waits.append(timeout)
            if len(waits) == 1:
                if failure == "interrupt":
                    raise KeyboardInterrupt
                raise subprocess.TimeoutExpired("bwrap", timeout)
            return -signal.SIGKILL

        def poll(self):
            return None

    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(worker.os, "killpg", lambda pid, sig: kills.append((pid, sig)))
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            worker.default_coding_worker_runner(_argv(tmp_path), 2)
    else:
        assert worker.default_coding_worker_runner(_argv(tmp_path), 2) == 124
    assert kills == [(12345, signal.SIGKILL)]
    assert waits == [2, None]


@pytest.mark.parametrize("timeout", (0, -1, True, None, 901))
def test_invalid_timeout_cannot_start_an_unbounded_process(tmp_path, monkeypatch, timeout) -> None:
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("spawned"))
    assert worker.default_coding_worker_runner(_argv(tmp_path), timeout) == 126


@pytest.mark.parametrize(
    "workspace,export",
    (
        (".", "out/op"),
        ("../x", "out/op"),
        ("work/op", "work/op/out"),
        ("work/op", "work/op"),
        ("work\\op", "out/op"),
    ),
)
def test_unsafe_or_overlapping_mount_paths_are_not_constructed(tmp_path, workspace, export) -> None:
    with pytest.raises(ValueError):
        worker.coding_worker_bwrap_argv(
            worker_root=str(tmp_path / "worker"),
            workspace_path=workspace,
            export_path=export,
            hazards=(),
            uid=1000,
            gid=1000,
        )
