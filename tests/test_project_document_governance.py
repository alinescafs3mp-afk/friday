from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_BACKLOG = "outer_sol/PROJECT_BACKLOG.md"


def _tracked_paths() -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(path for path in result.stdout.decode("utf-8").split("\0") if path)


def test_project_has_one_backlog_and_no_legacy_coordination_registers() -> None:
    tracked = _tracked_paths()
    backlog_paths = tuple(path for path in tracked if "backlog" in Path(path).name.casefold())

    assert backlog_paths == (CANONICAL_BACKLOG,)
    assert not any(path.startswith(("sol/", "grok/", "artifacts/")) for path in tracked)
    assert not any(Path(path).name.casefold() in {"open.md", "tasks.md", "proposals.md"} for path in tracked)
    assert not any("_IMPLEMENTATION_STATUS." in Path(path).name for path in tracked)
    assert not any(Path(path).name.startswith("HANDOFF_") for path in tracked)
    assert not any(Path(path).name.startswith("STATE_20") for path in tracked)


def test_architecture_inputs_defer_live_state_to_the_canonical_backlog() -> None:
    architecture_inputs = tuple(
        path
        for path in _tracked_paths()
        if path.startswith("outer_sol/") and path.endswith(".md") and path != CANONICAL_BACKLOG
    )

    assert architecture_inputs
    for path in architecture_inputs:
        assert "PROJECT_BACKLOG.md" in (ROOT / path).read_text(encoding="utf-8"), path

    backlog = (ROOT / CANONICAL_BACKLOG).read_text(encoding="utf-8")
    assert "project's only backlog and mutable status register" in backlog
    assert "No other tracked file may become a mutable backlog or status log." in backlog
