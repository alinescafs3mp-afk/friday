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
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "docker/Dockerfile.backend"
LOCKFILE = REPO / "requirements.lock"
DEV_LOCKFILE = REPO / "requirements-dev.lock"
PYPROJECT = REPO / "pyproject.toml"


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked(lockfile: Path = LOCKFILE) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in lockfile.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        pins[_canonical(name)] = version
    return pins


def _direct_requirements() -> list[Requirement]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return [Requirement(requirement) for requirement in data["project"]["dependencies"]]


def _dev_and_build_requirements() -> list[Requirement]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    requirements = [
        *data["project"]["optional-dependencies"]["dev"],
        *data["build-system"]["requires"],
    ]
    return [Requirement(requirement) for requirement in requirements]


def _dependency_closure(requirements: list[Requirement]) -> set[str]:
    """Resolve the active installed metadata, including requested extras.

    The lock is a constraint file, not merely a list of top-level wishes.  Walking
    the metadata here catches the easy-to-miss half of ``uvicorn[standard]``:
    uvicorn itself was pinned while its five extra dependencies still floated.
    """

    closure: set[str] = set()
    pending = list(requirements)
    visited: set[tuple[str, tuple[str, ...]]] = set()
    while pending:
        requirement = pending.pop()
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        name = _canonical(requirement.name)
        extras = tuple(sorted(requirement.extras))
        key = (name, extras)
        if key in visited:
            continue
        visited.add(key)
        closure.add(name)
        try:
            children = metadata.requires(requirement.name) or []
        except metadata.PackageNotFoundError as exc:
            raise AssertionError(f"runtime dependency is not installed: {requirement.name}") from exc
        marker_extras = extras or ("",)
        for child_text in children:
            child = Requirement(child_text)
            if child.marker and not any(child.marker.evaluate({"extra": extra}) for extra in marker_extras):
                continue
            # The marker was evaluated in the PARENT distribution's extra
            # context.  Carrying it into the child queue and evaluating it again
            # with extra="" would incorrectly discard every optional dependency.
            pending.append(Requirement(str(child).split(";", 1)[0].strip()))
    return closure


def _runtime_closure() -> set[str]:
    return _dependency_closure(_direct_requirements())


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
    missing = [_canonical(item.name) for item in _direct_requirements() if _canonical(item.name) not in pins]
    assert not missing, f"not pinned in requirements.lock: {missing} — regenerate it"


def test_the_active_runtime_dependency_closure_is_pinned():
    pins = _locked()
    missing = sorted(_runtime_closure() - pins.keys())
    assert not missing, f"runtime transitive dependencies are not pinned: {missing}"


def test_the_tested_runtime_matches_the_locked_versions():
    pins = _locked()
    mismatched = {
        name: {"installed": metadata.version(name), "locked": pins[name].split(";", 1)[0].strip()}
        for name in sorted(_runtime_closure())
        if name in pins and metadata.version(name) != pins[name].split(";", 1)[0].strip()
    }
    assert not mismatched, f"tests ran against versions other than requirements.lock: {mismatched}"


def test_development_and_build_dependency_closure_is_pinned_and_tested():
    pins = {**_locked(), **_locked(DEV_LOCKFILE)}
    closure = _dependency_closure(_dev_and_build_requirements())
    missing = sorted(closure - pins.keys())
    assert not missing, f"development/build transitive dependencies are not pinned: {missing}"
    mismatched = {
        name: {"installed": metadata.version(name), "locked": pins[name].split(";", 1)[0].strip()}
        for name in sorted(closure)
        if name in pins and metadata.version(name) != pins[name].split(";", 1)[0].strip()
    }
    assert not mismatched, f"dev tests ran against versions other than the lockfiles: {mismatched}"


def test_lockfile_pins_exact_versions_only():
    for lockfile in (LOCKFILE, DEV_LOCKFILE):
        for line in lockfile.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            assert "==" in line, f"lockfile entry is not an exact pin: {line!r}"
