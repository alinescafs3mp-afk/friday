from pathlib import Path

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
