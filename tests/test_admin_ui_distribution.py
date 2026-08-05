"""The installed service must carry the Admin UI it serves."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_real_wheel_ignores_a_stale_manifest_and_carries_admin_ui(tmp_path):
    """Build the artifact users install, including with poisoned local metadata."""
    project = tmp_path / "project"
    shutil.copytree(
        REPO / "friday",
        project / "friday",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO / name, project / name)

    # Setuptools normally carries this ignored cache across builds.  One tool
    # invocation with an absolute script_name used to poison it permanently and
    # make every later ``python -m build`` fail before producing a wheel.
    egg_info = project / "friday.egg-info"
    egg_info.mkdir()
    (egg_info / "SOURCES.txt").write_text(
        f"pyproject.toml\n{project / 'pyproject.toml'}\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        (
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(project / "dist"),
        ),
        cwd=project,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    wheels = list((project / "dist").glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        packaged = set(wheel.namelist())

    assert {
        "friday/admin_ui/static/index.html",
        "friday/admin_ui/static/app.js",
        "friday/admin_ui/static/app.css",
    } <= packaged
