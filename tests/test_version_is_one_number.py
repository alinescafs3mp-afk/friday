"""One release, one version number.

`pyproject.toml` and `jericho.__version__` are both hand-edited and had drifted
nineteen releases apart: the package said 0.132.0 while `jericho --version`,
`jericho status --json` and `GET /api/health` all reported 0.113.0. Everything a
person can read off a running instance comes from `__version__`, so the number the
owner sees when diagnosing a problem was the wrong one.
"""

from __future__ import annotations

import pathlib
import re

import jericho

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    assert match, "pyproject.toml has no version"
    return match.group(1)


def test_the_package_and_the_runtime_agree():
    assert jericho.__version__ == _pyproject_version()


def test_the_changelog_documents_this_release():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {jericho.__version__} " in changelog, f"CHANGELOG.md has no entry for {jericho.__version__}"
