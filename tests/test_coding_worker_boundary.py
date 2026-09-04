from pathlib import Path

from friday.organs.coding.worker_boundary import (
    default_coding_worker_boundary,
    observe_coding_worker_isolation,
)


def _homes(tmp_path: Path) -> dict[str, str]:
    return {
        "friday_home": str(tmp_path / "friday-home"),
        "owner_home": str(tmp_path / "owner"),
        "database_path": str(tmp_path / "friday-home" / "data" / "state"),
    }


def test_dedicated_root_hides_host_hazards(tmp_path: Path) -> None:
    homes = _homes(tmp_path)
    boundary = default_coding_worker_boundary(**homes, worker_root=str(tmp_path / "friday-coding-worker"))

    facts = observe_coding_worker_isolation(boundary)

    assert facts == {
        "host_secrets_visible": False,
        "docker_socket_present": False,
        "production_database_reachable": False,
        "owner_ssh_keys_visible": False,
    }


def test_host_home_as_visible_path_blocks(tmp_path: Path) -> None:
    homes = _homes(tmp_path)
    boundary = default_coding_worker_boundary(**homes, worker_root=str(tmp_path / "friday-coding-worker"))
    exposed = default_coding_worker_boundary(
        **homes,
        worker_root=str(tmp_path / "friday-coding-worker"),
    )
    exposed = type(boundary)(
        worker_root=exposed.worker_root,
        visible_paths=(homes["owner_home"],),
        friday_home=homes["friday_home"],
        owner_home=homes["owner_home"],
        database_path=homes["database_path"],
        workspace_path="work/op",
        export_path="out/op",
    )

    facts = observe_coding_worker_isolation(exposed)

    assert facts["owner_ssh_keys_visible"] is True
    assert facts["host_secrets_visible"] is False


def test_friday_home_as_worker_root_is_unsafe(tmp_path: Path) -> None:
    homes = _homes(tmp_path)
    boundary = default_coding_worker_boundary(**homes, worker_root=homes["friday_home"])

    facts = observe_coding_worker_isolation(boundary)

    assert facts["host_secrets_visible"] is True
    assert facts["production_database_reachable"] is True


def test_docker_socket_bind_is_present(tmp_path: Path) -> None:
    homes = _homes(tmp_path)
    boundary = default_coding_worker_boundary(**homes, worker_root=str(tmp_path / "friday-coding-worker"))
    exposed = type(boundary)(
        worker_root=boundary.worker_root,
        visible_paths=("/var/run/docker.sock",),
        friday_home=homes["friday_home"],
        owner_home=homes["owner_home"],
        database_path=homes["database_path"],
        workspace_path="work/op",
        export_path="out/op",
    )

    facts = observe_coding_worker_isolation(exposed)

    assert facts["docker_socket_present"] is True
    assert facts["host_secrets_visible"] is False


def test_relative_or_traversal_root_is_unsafe(tmp_path: Path) -> None:
    homes = _homes(tmp_path)
    boundary = default_coding_worker_boundary(**homes, worker_root="relative/worker")

    assert observe_coding_worker_isolation(boundary)["host_secrets_visible"] is True
