import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace

from friday.organs.coding.worker_cgroup import (
    coding_tree_enforcement_available,
    run_admitted_coding_tree,
)


def test_cgroup_module_does_not_import_docker_or_engineer() -> None:
    source = Path(coding_tree_enforcement_available.__code__.co_filename).read_text(encoding="utf-8")
    assert "import docker" not in source
    assert "friday.organs.engineer" not in source
    assert "from docker" not in source
    assert "friday_host_agent" not in source


def test_tree_enforcement_is_available_on_the_supported_host() -> None:
    assert coding_tree_enforcement_available() is True


def test_unproven_argv_does_not_start_a_tree(monkeypatch) -> None:
    import friday.organs.coding.worker_cgroup as coding_tree

    def forbidden(*args, **kwargs):
        raise AssertionError("unproven tree started a process")

    monkeypatch.setattr(coding_tree, "_allocate", forbidden)
    assert run_admitted_coding_tree(("/usr/bin/false",), 0, memory_bytes=1024) == 126


def test_probe_rejects_child_join_failure_and_stops_unit(tmp_path: Path, monkeypatch) -> None:
    import friday.organs.coding.worker_cgroup as coding_tree

    tree = SimpleNamespace(unit="probe.service", cgroup=tmp_path)
    joined: list[Path] = []
    stopped: list[str] = []
    monkeypatch.setattr(coding_tree, "_trusted_root_executable", lambda _path: True)
    monkeypatch.setattr(coding_tree, "_holder_executable", lambda: True)
    monkeypatch.setattr(coding_tree, "_allocate", lambda _limits: tree)
    monkeypatch.setattr(coding_tree, "_prove_limits", lambda _cgroup, _limits: None)
    monkeypatch.setattr(coding_tree, "_stop_unit", stopped.append)

    def deny_join(cgroup: Path) -> None:
        joined.append(cgroup)
        raise PermissionError("cgroup join denied")

    def fail_join(_argv, **kwargs):
        try:
            kwargs["preexec_fn"]()
        except PermissionError as exc:
            raise subprocess.SubprocessError("Exception occurred in preexec_fn.") from exc
        raise AssertionError("failed preexec unexpectedly returned")

    monkeypatch.setattr(coding_tree, "_join_cgroup", deny_join)
    monkeypatch.setattr(coding_tree.subprocess, "Popen", fail_join)

    assert coding_tree._probe_tree_enforcement() is False
    assert joined == [tmp_path]
    assert stopped == ["probe.service"]


def test_probe_proves_child_join_and_cleans_probe(tmp_path: Path, monkeypatch) -> None:
    import friday.organs.coding.worker_cgroup as coding_tree

    tree = SimpleNamespace(unit="probe.service", cgroup=tmp_path)
    joined: list[Path] = []
    proved: list[Path] = []
    killed_trees: list[Path] = []
    killed_groups: list[tuple[int, signal.Signals]] = []
    stopped: list[str] = []

    class ProbeProcess:
        pid = 4321

        def __init__(self) -> None:
            self.waited: list[int] = []

        def poll(self):
            return None

        def wait(self, *, timeout: int):
            self.waited.append(timeout)
            return 0

    process = ProbeProcess()

    def start_probe(argv, **kwargs):
        assert argv == (coding_tree.SLEEP_EXECUTABLE, "infinity")
        kwargs["preexec_fn"]()
        return process

    monkeypatch.setattr(coding_tree, "_trusted_root_executable", lambda _path: True)
    monkeypatch.setattr(coding_tree, "_holder_executable", lambda: True)
    monkeypatch.setattr(coding_tree, "_allocate", lambda _limits: tree)
    monkeypatch.setattr(coding_tree, "_join_cgroup", joined.append)
    monkeypatch.setattr(coding_tree, "_pid_in_tree", lambda pid, cgroup: (pid, cgroup) == (4321, tmp_path))
    monkeypatch.setattr(coding_tree, "_prove_limits", lambda cgroup, _limits: proved.append(cgroup))
    monkeypatch.setattr(coding_tree, "_kill_tree", killed_trees.append)
    monkeypatch.setattr(coding_tree.os, "killpg", lambda pid, sig: killed_groups.append((pid, sig)))
    monkeypatch.setattr(coding_tree, "_stop_unit", stopped.append)
    monkeypatch.setattr(coding_tree.subprocess, "Popen", start_probe)

    assert coding_tree._probe_tree_enforcement() is True
    assert joined == [tmp_path]
    assert proved == [tmp_path, tmp_path]
    assert killed_trees == [tmp_path]
    assert killed_groups == [(4321, signal.SIGKILL)]
    assert process.waited == [5]
    assert stopped == ["probe.service"]


def test_preexec_failure_is_fail_closed_and_stops_unit(tmp_path: Path, monkeypatch) -> None:
    import friday.organs.coding.worker_cgroup as coding_tree

    tree = SimpleNamespace(unit="worker.service", cgroup=tmp_path)
    joined: list[Path] = []
    stopped: list[str] = []
    monkeypatch.setattr(coding_tree, "_allocate", lambda _limits: tree)
    monkeypatch.setattr(coding_tree, "_stop_unit", stopped.append)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("post-launch proof ran after failed Popen")

    def deny_join(cgroup: Path) -> None:
        joined.append(cgroup)
        raise PermissionError("cgroup join denied")

    def fail_join(_argv, **kwargs):
        try:
            kwargs["preexec_fn"]()
        except PermissionError as exc:
            raise subprocess.SubprocessError("Exception occurred in preexec_fn.") from exc
        raise AssertionError("failed preexec unexpectedly returned")

    monkeypatch.setattr(coding_tree, "_pid_in_tree", forbidden)
    monkeypatch.setattr(coding_tree, "_prove_limits", forbidden)
    monkeypatch.setattr(coding_tree, "_join_cgroup", deny_join)
    monkeypatch.setattr(coding_tree.subprocess, "Popen", fail_join)

    assert run_admitted_coding_tree(("/usr/bin/false",), 1, memory_bytes=1024) == 126
    assert joined == [tmp_path]
    assert stopped == ["worker.service"]


def test_failed_membership_kills_reaps_and_stops_tree(tmp_path: Path, monkeypatch) -> None:
    import friday.organs.coding.worker_cgroup as coding_tree

    tree = SimpleNamespace(unit="worker.service", cgroup=tmp_path)
    events: list[object] = []

    class Process:
        pid = 4321
        stderr = None

        def poll(self):
            events.append("poll")
            return None

        def wait(self, *, timeout: int):
            events.append(("wait", timeout))
            return -signal.SIGKILL

    monkeypatch.setattr(coding_tree, "_allocate", lambda _limits: tree)
    monkeypatch.setattr(coding_tree.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(coding_tree, "_pid_in_tree", lambda _pid, _cgroup: False)
    monkeypatch.setattr(coding_tree, "_prove_limits", lambda *_args: events.append("prove"))
    monkeypatch.setattr(coding_tree, "_kill_tree", lambda cgroup: events.append(("kill", cgroup)))
    monkeypatch.setattr(coding_tree.os, "killpg", lambda pid, sig: events.append(("killpg", pid, sig)))
    monkeypatch.setattr(coding_tree, "_stop_unit", lambda unit: events.append(("stop", unit)))

    report = coding_tree.run_admitted_coding_tree_report(
        ("/usr/bin/false",),
        1,
        memory_bytes=1024,
        stderr_limit=0,
    )

    assert report == coding_tree.CodingTreeCommandV1(126, b"")
    assert events == [
        "poll",
        ("kill", tmp_path),
        ("killpg", 4321, signal.SIGKILL),
        ("wait", 5),
        ("stop", "worker.service"),
    ]


def test_joined_child_preserves_result_and_stops_unit(tmp_path: Path, monkeypatch) -> None:
    import friday.organs.coding.worker_cgroup as coding_tree

    tree = SimpleNamespace(unit="worker.service", cgroup=tmp_path)
    events: list[object] = []

    class Process:
        pid = 4321
        stderr = object()
        returncode = 7

        def poll(self):
            events.append("poll")
            return self.returncode

        def communicate(self, *, timeout: int):
            events.append(("communicate", timeout))
            return (b"", b"abcdef")

    def start_process(_argv, **kwargs):
        events.append("popen")
        kwargs["preexec_fn"]()
        return Process()

    def observe_membership(pid: int, cgroup: Path) -> bool:
        events.append(("membership", pid, cgroup))
        return True

    monkeypatch.setattr(coding_tree, "_allocate", lambda _limits: tree)
    monkeypatch.setattr(coding_tree, "_join_cgroup", lambda cgroup: events.append(("join", cgroup)))
    monkeypatch.setattr(coding_tree, "_pid_in_tree", observe_membership)
    monkeypatch.setattr(
        coding_tree, "_prove_limits", lambda cgroup, _limits: events.append(("prove", cgroup))
    )
    monkeypatch.setattr(coding_tree.subprocess, "Popen", start_process)
    monkeypatch.setattr(coding_tree, "_stop_unit", lambda unit: events.append(("stop", unit)))

    report = coding_tree.run_admitted_coding_tree_report(
        ("/usr/bin/true",),
        3,
        memory_bytes=1024,
        stderr_limit=4,
    )

    assert report == coding_tree.CodingTreeCommandV1(7, b"abcd")
    assert events == [
        "popen",
        ("join", tmp_path),
        ("membership", 4321, tmp_path),
        ("prove", tmp_path),
        ("communicate", 3),
        "poll",
        ("stop", "worker.service"),
    ]
