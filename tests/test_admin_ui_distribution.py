"""The installed service must carry the Admin UI it serves."""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import Distribution
from setuptools.command.build_py import build_py
from setuptools.config.pyprojecttoml import apply_configuration

REPO = Path(__file__).resolve().parents[1]


def test_the_distribution_carries_every_admin_ui_asset(tmp_path, monkeypatch):
    """Exercise setuptools' package-data selection in a clean source tree."""
    project = tmp_path / "project"
    package = project / "friday"
    shutil.copytree(REPO / "friday" / "admin_ui", package / "admin_ui")
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(REPO / name, project / name)

    monkeypatch.chdir(project)
    distribution = Distribution()
    distribution.script_name = "pyproject.toml"
    apply_configuration(distribution, "pyproject.toml")

    command = build_py(distribution)
    command.ensure_finalized()
    command.build_lib = str(project / "wheel-layout")
    command.run()

    packaged = Path(command.build_lib) / "friday" / "admin_ui" / "static"
    assert {"index.html", "app.js", "app.css"} <= {path.name for path in packaged.iterdir()}
