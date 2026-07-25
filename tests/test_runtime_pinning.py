"""The container must run the interpreter the tests ran on, with pinned dependencies.

Dev on 3.14 while the image shipped 3.12 meant the code running in production was
never the code that was verified — a whole class of "works on my machine" defects
that no test can catch after the fact. These guards keep the two from drifting apart
again, and keep the lockfile honest when a dependency is added.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "docker/Dockerfile.backend"
LOCKFILE = REPO / "requirements.lock"
PYPROJECT = REPO / "pyproject.toml"


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in LOCKFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        pins[_canonical(name)] = version
    return pins


def _direct_dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return [
        _canonical(re.split(r"[<>=!~\[;\s]", requirement, maxsplit=1)[0])
        for requirement in data["project"]["dependencies"]
    ]


def test_container_python_matches_the_tested_interpreter():
    """The image tag must track the interpreter the suite is verified on."""
    match = re.search(r"^FROM python:(\d+)\.(\d+)-slim", DOCKERFILE.read_text(), re.MULTILINE)
    assert match, "Dockerfile.backend must pin an explicit python:X.Y-slim base"
    image_version = (int(match.group(1)), int(match.group(2)))
    assert image_version == sys.version_info[:2], (
        f"container runs python {image_version[0]}.{image_version[1]} but the suite runs on "
        f"{sys.version_info.major}.{sys.version_info.minor} — the shipped code would not be "
        "the verified code"
    )


def test_every_runtime_dependency_is_pinned():
    """A dependency added without regenerating the lock would resolve freely at build
    time, which is exactly what the lock exists to prevent."""
    pins = _locked()
    missing = [name for name in _direct_dependencies() if name not in pins]
    assert not missing, f"not pinned in requirements.lock: {missing} — regenerate it"


def test_lockfile_pins_exact_versions_only():
    for line in LOCKFILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"lockfile entry is not an exact pin: {line!r}"
