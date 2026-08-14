#!/usr/bin/env python3
"""Fail closed unless the canonical offline quality toolchain is exact."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

REQUIRED_PYTHON = (3, 14, 4)
REQUIRED_DISTRIBUTIONS = {
    "numpy": "2.5.1",
    "playwright": "1.61.0",
}
REQUIRED_NODE = "v22.23.2"
REQUIRED_UNRAR_BANNER = "UNRAR 7.20 freeware"
REQUIRED_UNRAR_SHA256 = "718db45ff7a132043f33928af3b7692dbb5b93630c84fead0c04d73e77155c0d"
REQUIRED_CHROMIUM_REVISION = "1228"

ProgramProbe = Callable[[str, tuple[str, ...]], tuple[int, str]]
ProgramDigestProbe = Callable[[str], tuple[int, str]]
VersionProbe = Callable[[str], str]


def _program_output(program: str, arguments: tuple[str, ...]) -> tuple[int, str]:
    resolved = shutil.which(program)
    if not resolved:
        return 127, ""
    binary = Path(resolved).resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return 126, ""
    try:
        completed = subprocess.run(  # noqa: S603 - exact PATH program is version-attested below
            (str(binary), *arguments),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 126, ""
    return completed.returncode, f"{completed.stdout}\n{completed.stderr}".strip()


def _program_sha256(program: str) -> tuple[int, str]:
    resolved = shutil.which(program)
    if not resolved:
        return 127, ""
    try:
        binary = Path(resolved).resolve(strict=True)
        before = binary.stat(follow_symlinks=False)
        descriptor = os.open(
            binary,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        return 126, ""
    try:
        opened = os.fstat(descriptor)
        current = binary.stat(follow_symlinks=False)
        if not stat.S_ISREG(opened.st_mode) or not os.access(binary, os.X_OK):
            return 126, ""
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or (
            opened.st_dev,
            opened.st_ino,
        ) != (current.st_dev, current.st_ino):
            return 126, ""
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return 0, digest.hexdigest()
    finally:
        os.close(descriptor)


def _browser_revision() -> str:
    distribution = metadata.distribution("playwright")
    manifest = Path(str(distribution.locate_file("playwright/driver/package/browsers.json")))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    browsers = payload.get("browsers")
    if not isinstance(browsers, list):
        return ""
    for browser in browsers:
        if isinstance(browser, dict) and browser.get("name") == "chromium":
            revision = browser.get("revision")
            return revision if isinstance(revision, str) else ""
    return ""


def _browser_executable() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return playwright.chromium.executable_path


def _browser_executable_is_installed(path: str, revision: str) -> bool:
    if not path:
        return False
    try:
        executable = Path(path).resolve(strict=True)
        details = executable.stat(follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(details.st_mode)
        and os.access(executable, os.X_OK)
        and f"chromium-{revision}" in executable.parts
    )


def toolchain_complaints(
    *,
    program_probe: ProgramProbe = _program_output,
    program_digest_probe: ProgramDigestProbe = _program_sha256,
    version_probe: VersionProbe = metadata.version,
    browser_revision_probe: Callable[[], str] = _browser_revision,
    browser_executable_probe: Callable[[], str] = _browser_executable,
) -> list[str]:
    """Return closed, value-free mismatch codes for the release toolchain."""

    complaints: list[str] = []
    if sys.version_info[:3] != REQUIRED_PYTHON:
        complaints.append("python_version_mismatch")
    for distribution, expected in REQUIRED_DISTRIBUTIONS.items():
        try:
            actual = version_probe(distribution)
        except metadata.PackageNotFoundError:
            complaints.append(f"{distribution}_missing")
            continue
        if actual != expected:
            complaints.append(f"{distribution}_version_mismatch")

    node_status, node_output = program_probe("node", ("--version",))
    if node_status != 0:
        complaints.append("node_unavailable")
    elif node_output.strip() != REQUIRED_NODE:
        complaints.append("node_version_mismatch")

    unrar_status, unrar_output = program_probe("unrar", ())
    if unrar_status != 0:
        complaints.append("unrar_unavailable")
    elif REQUIRED_UNRAR_BANNER not in unrar_output:
        complaints.append("unrar_version_mismatch")
    else:
        unrar_digest_status, unrar_digest = program_digest_probe("unrar")
        if unrar_digest_status != 0:
            complaints.append("unrar_identity_unavailable")
        elif unrar_digest != REQUIRED_UNRAR_SHA256:
            complaints.append("unrar_identity_mismatch")

    try:
        browser_revision = browser_revision_probe()
    except (OSError, ValueError, json.JSONDecodeError, metadata.PackageNotFoundError):
        complaints.append("playwright_browser_manifest_unavailable")
    else:
        if browser_revision != REQUIRED_CHROMIUM_REVISION:
            complaints.append("chromium_revision_mismatch")
    try:
        browser_executable = browser_executable_probe()
    except Exception:  # noqa: BLE001 - any driver/package failure is a closed preflight
        complaints.append("chromium_executable_unavailable")
    else:
        if not _browser_executable_is_installed(browser_executable, REQUIRED_CHROMIUM_REVISION):
            complaints.append("chromium_executable_unavailable")
    return complaints


def main() -> int:
    complaints = toolchain_complaints()
    if complaints:
        print(f"Quality toolchain: FAILED ({','.join(sorted(complaints))})", file=sys.stderr)
        return 1
    print("Quality toolchain: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
