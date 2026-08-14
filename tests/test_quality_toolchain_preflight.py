from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest

from tools import quality_toolchain_preflight as preflight


def _versions(name: str) -> str:
    return preflight.REQUIRED_DISTRIBUTIONS[name]


def _programs(program: str, _arguments: tuple[str, ...]) -> tuple[int, str]:
    if program == "node":
        return 0, preflight.REQUIRED_NODE
    if program == "unrar":
        return 0, f"\n{preflight.REQUIRED_UNRAR_BANNER} copyright"
    raise AssertionError(program)


def _digests(program: str) -> tuple[int, str]:
    assert program == "unrar"
    return 0, preflight.REQUIRED_UNRAR_SHA256


def _chromium(tmp_path: Path) -> str:
    executable = tmp_path / f"chromium-{preflight.REQUIRED_CHROMIUM_REVISION}" / "chrome"
    executable.parent.mkdir()
    executable.write_bytes(b"synthetic executable identity")
    executable.chmod(0o755)
    return str(executable)


def test_exact_offline_toolchain_is_clear(tmp_path: Path) -> None:
    assert (
        preflight.toolchain_complaints(
            program_probe=_programs,
            program_digest_probe=_digests,
            version_probe=_versions,
            browser_revision_probe=lambda: preflight.REQUIRED_CHROMIUM_REVISION,
            browser_executable_probe=lambda: _chromium(tmp_path),
        )
        == []
    )


def test_every_external_version_mismatch_is_closed() -> None:
    def wrong_versions(name: str) -> str:
        if name == "numpy":
            return "0"
        raise metadata.PackageNotFoundError(name)

    def wrong_programs(program: str, _arguments: tuple[str, ...]) -> tuple[int, str]:
        return (0, "v24.0.0") if program == "node" else (127, "")

    assert preflight.toolchain_complaints(
        program_probe=wrong_programs,
        program_digest_probe=lambda _program: (127, ""),
        version_probe=wrong_versions,
        browser_revision_probe=lambda: "9999",
        browser_executable_probe=lambda: "",
    ) == [
        "numpy_version_mismatch",
        "playwright_missing",
        "node_version_mismatch",
        "unrar_unavailable",
        "chromium_revision_mismatch",
        "chromium_executable_unavailable",
    ]


def test_missing_browser_manifest_is_closed(tmp_path: Path) -> None:
    def missing_manifest() -> str:
        raise OSError("closed synthetic failure")

    assert preflight.toolchain_complaints(
        program_probe=_programs,
        program_digest_probe=_digests,
        version_probe=_versions,
        browser_revision_probe=missing_manifest,
        browser_executable_probe=lambda: _chromium(tmp_path),
    ) == ["playwright_browser_manifest_unavailable"]


def test_banner_compatible_unrar_with_wrong_identity_is_closed(tmp_path: Path) -> None:
    assert preflight.toolchain_complaints(
        program_probe=_programs,
        program_digest_probe=lambda _program: (0, "0" * 64),
        version_probe=_versions,
        browser_revision_probe=lambda: preflight.REQUIRED_CHROMIUM_REVISION,
        browser_executable_probe=lambda: _chromium(tmp_path),
    ) == ["unrar_identity_mismatch"]


def test_missing_exact_chromium_executable_is_closed(tmp_path: Path) -> None:
    missing = tmp_path / f"chromium-{preflight.REQUIRED_CHROMIUM_REVISION}" / "chrome"

    assert preflight.toolchain_complaints(
        program_probe=_programs,
        program_digest_probe=_digests,
        version_probe=_versions,
        browser_revision_probe=lambda: preflight.REQUIRED_CHROMIUM_REVISION,
        browser_executable_probe=lambda: str(missing),
    ) == ["chromium_executable_unavailable"]


def test_program_digest_hashes_the_resolved_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "unrar"
    binary.write_bytes(b"official fixture bytes")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    status, digest = preflight._program_sha256("unrar")

    assert status == 0
    assert digest == "f751c195f1f22b8a815d6c10132b8ba8a1703183dd6817c9e3ebf6906061f837"
