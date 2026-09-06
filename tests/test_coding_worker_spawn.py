from dataclasses import replace
from pathlib import Path

from friday.orchestration.coding_worker_admission import CodingWorkerAdmissionState
from friday.organs.coding.worker_boundary import default_coding_worker_boundary
from friday.organs.coding.worker_spawn import (
    BWRAP_EXECUTABLE,
    coding_worker_bwrap_argv,
    compose_coding_worker_admission,
    spawn_coding_worker,
)


def test_spawn_module_does_not_import_docker_or_engineer() -> None:
    source = Path(spawn_coding_worker.__code__.co_filename).read_text(encoding="utf-8")
    assert "import docker" not in source
    assert "friday.organs.engineer" not in source
    assert "from docker" not in source
    assert BWRAP_EXECUTABLE in source


SNAPSHOT = "a" * 64


def _boundary(tmp_path: Path, **overrides: object):
    homes = {
        "friday_home": str(tmp_path / "friday-home"),
        "owner_home": str(tmp_path / "owner"),
        "database_path": str(tmp_path / "friday-home" / "data" / "state"),
        "worker_root": str(tmp_path / "friday-coding-worker"),
        "workspace_path": "work/operation.1",
        "export_path": "out/operation.1",
    }
    homes.update(overrides)
    return default_coding_worker_boundary(**homes)


def _admission(tmp_path: Path, boundary=None):
    boundary = boundary or _boundary(tmp_path)
    return compose_coding_worker_admission(
        admission_id="admission.1",
        authenticated_turn_id="turn.1",
        worker_id="worker.1",
        operation_id="operation.1",
        project_id="project.1",
        revision_selector=SNAPSHOT,
        boundary=boundary,
    )


def test_dedicated_boundary_is_admitted_without_process(tmp_path: Path) -> None:
    result = _admission(tmp_path)

    assert result.admission is CodingWorkerAdmissionState.ADMITTED
    assert result.isolation is not None
    assert result.isolation.host_secrets_visible is False
    assert result.network is not None
    assert result.network.network.value == "disabled"


def test_blocked_admission_does_not_spawn(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path, worker_root=str(tmp_path / "friday-home"))
    admission = _admission(tmp_path, boundary)
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], timeout_sec: int) -> int:
        calls.append(argv)
        del timeout_sec
        return 0

    spawn = spawn_coding_worker(admission, boundary, runner=runner)

    assert admission.admission is CodingWorkerAdmissionState.BLOCKED
    assert spawn.spawned is False
    assert spawn.probe == "skipped"
    assert spawn.untrusted_execute is False
    assert calls == []


def test_admitted_admission_spawns_probe_once(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], timeout_sec: int) -> int:
        calls.append(argv)
        del timeout_sec
        return 0

    spawn = spawn_coding_worker(admission, boundary, runner=runner)

    assert admission.admission is CodingWorkerAdmissionState.ADMITTED
    assert spawn.spawned is True
    assert spawn.probe == "confirmed"
    assert spawn.untrusted_execute is False
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == BWRAP_EXECUTABLE
    assert "--unshare-all" in argv
    text = " ".join(argv)
    assert "/var/run/docker.sock" in text
    assert argv[argv.index("--bind") + 1] == str(tmp_path / "friday-coding-worker" / boundary.workspace_path)
    assert "/var/run/docker.sock" not in argv[argv.index("--bind") + 1]


def test_bwrap_argv_does_not_bind_host_hazards(tmp_path: Path) -> None:
    root = str(tmp_path / "friday-coding-worker")
    argv = coding_worker_bwrap_argv(
        worker_root=root,
        workspace_path="work/operation.1",
        export_path="out/operation.1",
        hazards=(
            "/var/run/docker.sock",
            str(tmp_path / "friday-home"),
            str(tmp_path / "owner" / ".ssh"),
        ),
        uid=1000,
        gid=1000,
    )
    bind_target = argv[argv.index("--bind") + 1]
    assert bind_target == str(Path(root) / "work/operation.1")
    assert root not in argv
    assert "docker.sock" not in bind_target
    assert argv[0] == BWRAP_EXECUTABLE
    assert "friday.organs.engineer" not in " ".join(argv)


def test_real_coding_bwrap_probe_hides_hazards(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)
    spawn = spawn_coding_worker(admission, boundary)

    assert admission.admission is CodingWorkerAdmissionState.ADMITTED
    assert spawn.spawned is True
    assert spawn.probe == "confirmed"
    assert spawn.untrusted_execute is False


def test_host_network_boundary_is_blocked(tmp_path: Path) -> None:
    boundary = replace(_boundary(tmp_path), host_network=True, network_disabled=False)
    result = _admission(tmp_path, boundary)

    assert result.admission is CodingWorkerAdmissionState.BLOCKED
    assert result.network is None


def test_only_the_current_operation_directories_are_mounted(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)
    calls = []
    result = spawn_coding_worker(admission, boundary, runner=lambda argv, timeout: calls.append(argv) or 0)
    assert result.probe == "confirmed"
    argv = calls[0]
    binds = [(argv[index + 1], argv[index + 2]) for index, value in enumerate(argv) if value == "--bind"]
    assert binds == [
        (str(Path(boundary.worker_root) / boundary.workspace_path), "/work/work/operation.1"),
        (str(Path(boundary.worker_root) / boundary.export_path), "/work/out/operation.1"),
    ]
    assert boundary.worker_root not in argv


def test_changed_boundary_is_rejected_before_creating_directories(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = _admission(tmp_path, boundary)
    changed = replace(boundary, worker_root=str(tmp_path / "other-worker"))
    calls = []
    result = spawn_coding_worker(admission, changed, runner=lambda argv, timeout: calls.append(argv) or 0)
    assert result.probe == "failed"
    assert calls == []
    assert not Path(changed.worker_root).exists()


def test_symlink_ancestor_is_not_followed_by_probe_preparation(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    boundary = _boundary(tmp_path)
    root = Path(boundary.worker_root)
    root.mkdir()
    (root / "work").symlink_to(outside, target_is_directory=True)
    result = spawn_coding_worker(_admission(tmp_path, boundary), boundary, runner=lambda argv, timeout: 0)
    assert result.probe == "failed"
    assert list(outside.iterdir()) == []


def test_resource_limits_from_admission_reach_the_actual_python_invocation(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    admission = compose_coding_worker_admission(
        admission_id="admission.limited",
        authenticated_turn_id="turn.1",
        worker_id="worker.1",
        operation_id="operation.1",
        project_id="project.1",
        revision_selector=SNAPSHOT,
        boundary=boundary,
        memory_bytes=80 * 1024 * 1024,
        cpu_sec=7,
        wall_clock_sec=9,
    )
    calls = []
    result = spawn_coding_worker(
        admission, boundary, runner=lambda argv, timeout: calls.append((argv, timeout)) or 0
    )
    assert result.probe == "confirmed"
    argv, timeout = calls[0]
    assert "--as=83886080:83886080" in argv
    assert "--cpu=7:7" in argv
    assert "--core=0:0" in argv
    assert timeout == 9
    assert argv[argv.index("-c") - 2 : argv.index("-c")] == ("/usr/bin/python3", "-I")


def test_boolean_return_code_is_not_a_successful_probe(tmp_path: Path) -> None:
    boundary = _boundary(tmp_path)
    result = spawn_coding_worker(_admission(tmp_path, boundary), boundary, runner=lambda argv, timeout: False)
    assert result.probe == "failed"
    assert result.spawned is False
