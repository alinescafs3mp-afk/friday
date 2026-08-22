from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import stat
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

_INSTALLER_PATH = Path("docker/install_syncthing.py")
_SPEC = importlib.util.spec_from_file_location("friday_install_syncthing", _INSTALLER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
installer: ModuleType = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(installer)


def _archive(entries: list[tuple[str, bytes, bytes | None]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, payload, kind in entries:
            item = tarfile.TarInfo(name)
            if kind == tarfile.SYMTYPE:
                item.type = tarfile.SYMTYPE
                item.linkname = "../../outside"
                item.size = 0
                archive.addfile(item)
            elif kind == tarfile.DIRTYPE:
                item.type = tarfile.DIRTYPE
                item.size = 0
                archive.addfile(item)
            else:
                item.mode = 0o755 if name.endswith("/syncthing") else 0o644
                item.size = len(payload)
                archive.addfile(item, io.BytesIO(payload))
    return output.getvalue()


def _official_shape(*, binary: bytes = b"official binary", license_text: bytes = b"license") -> list:
    root = installer._root_name("amd64")
    return [
        (root, b"", tarfile.DIRTYPE),
        (f"{root}/syncthing", binary, None),
        (f"{root}/LICENSE.txt", license_text, None),
        # Current release archives also have a same-basename packaging file.
        # It must never compete with the root executable.
        (f"{root}/etc/firewall-ufw/syncthing", b"not the executable", None),
    ]


def _trust_fixture(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    monkeypatch.setitem(installer.ARCHIVES, "amd64", hashlib.sha256(payload).hexdigest())


def test_installer_selects_exact_files_below_the_single_release_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive(_official_shape(binary=b"root executable", license_text=b"LICENSE text"))
    _trust_fixture(monkeypatch, payload)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary_path = binary_dir / "syncthing"
    license_path = tmp_path / "licenses" / "syncthing" / "LICENSE.txt"

    digest = installer.install_archive(
        payload,
        "amd64",
        binary_path=binary_path,
        license_path=license_path,
    )

    assert digest == hashlib.sha256(payload).hexdigest()
    assert binary_path.read_bytes() == b"root executable"
    assert license_path.read_bytes() == b"LICENSE text"
    assert stat.S_IMODE(binary_path.stat().st_mode) == 0o755
    assert stat.S_IMODE(license_path.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("extra_name", "error"),
    [
        ("../escape", "outside its expected root"),
        ("other-root/file", "outside its expected root"),
    ],
)
def test_installer_rejects_traversal_and_multiple_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra_name: str,
    error: str,
) -> None:
    root = installer._root_name("amd64")
    name = f"{root}/{extra_name}" if extra_name.startswith("..") else extra_name
    payload = _archive([*_official_shape(), (name, b"escape", None)])
    _trust_fixture(monkeypatch, payload)
    (tmp_path / "bin").mkdir()

    with pytest.raises(RuntimeError, match=error):
        installer.install_archive(
            payload,
            "amd64",
            binary_path=tmp_path / "bin" / "syncthing",
            license_path=tmp_path / "licenses" / "syncthing" / "LICENSE.txt",
        )


def test_installer_rejects_a_link_at_the_exact_binary_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = installer._root_name("amd64")
    entries = [entry for entry in _official_shape() if entry[0] != f"{root}/syncthing"]
    payload = _archive([*entries, (f"{root}/syncthing", b"", tarfile.SYMTYPE)])
    _trust_fixture(monkeypatch, payload)
    (tmp_path / "bin").mkdir()

    with pytest.raises(RuntimeError, match="unsafe member type"):
        installer.install_archive(
            payload,
            "amd64",
            binary_path=tmp_path / "bin" / "syncthing",
            license_path=tmp_path / "licenses" / "syncthing" / "LICENSE.txt",
        )


def test_installer_rejects_missing_exact_license_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = installer._root_name("amd64")
    entries = [entry for entry in _official_shape() if entry[0] != f"{root}/LICENSE.txt"]
    payload = _archive([*entries, (f"{root}/LICENSE", b"wrong old name", None)])
    _trust_fixture(monkeypatch, payload)
    (tmp_path / "bin").mkdir()

    with pytest.raises(RuntimeError, match="LICENSE.txt"):
        installer.install_archive(
            payload,
            "amd64",
            binary_path=tmp_path / "bin" / "syncthing",
            license_path=tmp_path / "licenses" / "syncthing" / "LICENSE.txt",
        )


def test_installer_checks_archive_hash_before_creating_outputs(tmp_path: Path) -> None:
    payload = _archive(_official_shape())
    binary_path = tmp_path / "bin" / "syncthing"
    binary_path.parent.mkdir()

    with pytest.raises(RuntimeError, match="digest mismatch"):
        installer.install_archive(
            payload,
            "amd64",
            binary_path=binary_path,
            license_path=tmp_path / "licenses" / "syncthing" / "LICENSE.txt",
        )
    assert not binary_path.exists()
    assert not (tmp_path / "licenses").exists()


def test_installer_enforces_selected_member_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _archive(_official_shape(binary=b"too large"))
    _trust_fixture(monkeypatch, payload)
    monkeypatch.setattr(installer, "MAX_BINARY_BYTES", 4)
    (tmp_path / "bin").mkdir()

    with pytest.raises(RuntimeError, match="unsafe.*syncthing"):
        installer.install_archive(
            payload,
            "amd64",
            binary_path=tmp_path / "bin" / "syncthing",
            license_path=tmp_path / "licenses" / "syncthing" / "LICENSE.txt",
        )


def test_real_pinned_amd64_archive_installs_when_cached_or_opted_in(tmp_path: Path) -> None:
    """Run with a cache or FRIDAY_TEST_SYNCTHING_INSTALLER_LIVE=1; never network by default."""

    filename = installer._filename("amd64")
    explicit = (
        os.environ.get("FRIDAY_SYNCTHING_AMD64_TARBALL", "").strip()
        or os.environ.get("QUALITY_GATE_SYNCTHING_AMD64_TARBALL", "").strip()
    )
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    cached = Path(explicit) if explicit else cache_home / "friday" / "test-assets" / filename
    if cached.is_file():
        payload = cached.read_bytes()
    elif os.environ.get("FRIDAY_TEST_SYNCTHING_INSTALLER_LIVE", "").strip() == "1":
        payload = installer._download(
            f"https://github.com/syncthing/syncthing/releases/download/v{installer.VERSION}/{filename}"
        )
    else:
        pytest.skip("set FRIDAY_TEST_SYNCTHING_INSTALLER_LIVE=1 or FRIDAY_SYNCTHING_AMD64_TARBALL")
    assert len(payload) <= installer.MAX_ARCHIVE_BYTES
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary_path = binary_dir / "syncthing"
    license_path = tmp_path / "licenses" / "syncthing" / "LICENSE.txt"

    digest = installer.install_archive(
        payload,
        "amd64",
        binary_path=binary_path,
        license_path=license_path,
    )

    assert digest == installer.ARCHIVES["amd64"]
    assert binary_path.read_bytes().startswith(b"\x7fELF")
    assert len(license_path.read_bytes()) > 100
