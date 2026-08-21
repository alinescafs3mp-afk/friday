#!/usr/bin/env python3
"""Install the reviewed Syncthing release during the backend image build."""

from __future__ import annotations

import hashlib
import hmac
import io
import os
import stat
import sys
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

VERSION = "2.1.3"
ARCHIVES = {
    "amd64": "f929eb8e5b72a85543eeeefb2c38f34a68e0c530e70758a2905b78840c76602c",
    "arm64": "a5c046965b590a8de2f8c8c16a0dbf9201d99600b0cafd604040232b603e4586",
}
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_BINARY_BYTES = 32 * 1024 * 1024
MAX_LICENSE_BYTES = 128 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_MEMBERS = 4_096
MAX_MEMBER_NAME_CHARS = 1_024


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Friday-image-builder/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw_length = response.headers.get("Content-Length")
        if raw_length is not None and int(raw_length) > MAX_ARCHIVE_BYTES:
            raise RuntimeError("Syncthing archive exceeds the build-time size limit")
        payload = response.read(MAX_ARCHIVE_BYTES + 1)
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise RuntimeError("Syncthing archive exceeds the build-time size limit")
    return payload


def _filename(architecture: str) -> str:
    return f"syncthing-linux-{architecture}-v{VERSION}.tar.gz"


def _root_name(architecture: str) -> str:
    return _filename(architecture).removesuffix(".tar.gz")


def _validated_members(archive: tarfile.TarFile, *, architecture: str) -> list[tarfile.TarInfo]:
    """Validate the complete archive namespace before reading selected files."""

    expected_root = _root_name(architecture)
    members = archive.getmembers()
    if not members or len(members) > MAX_MEMBERS:
        raise RuntimeError("Syncthing archive has an invalid member count")
    names: set[str] = set()
    expanded_bytes = 0
    for item in members:
        raw_name = item.name.rstrip("/")
        if not raw_name or len(raw_name) > MAX_MEMBER_NAME_CHARS or "\\" in raw_name or "\x00" in raw_name:
            raise RuntimeError("Syncthing archive contains an unsafe member path")
        raw_parts = raw_name.split("/")
        path = PurePosixPath(raw_name)
        if (
            path.is_absolute()
            or any(part in {"", ".", ".."} for part in raw_parts)
            or not path.parts
            or path.parts[0] != expected_root
        ):
            raise RuntimeError("Syncthing archive contains a path outside its expected root")
        canonical = path.as_posix()
        if canonical in names:
            raise RuntimeError("Syncthing archive contains duplicate member paths")
        names.add(canonical)
        if not (item.isfile() or item.isdir()) or item.issym() or item.islnk():
            raise RuntimeError("Syncthing archive contains an unsafe member type")
        if item.size < 0:
            raise RuntimeError("Syncthing archive contains an invalid member size")
        if item.isfile():
            expanded_bytes += item.size
            if expanded_bytes > MAX_EXPANDED_BYTES:
                raise RuntimeError("Syncthing archive exceeds the expanded size limit")
    return members


def _member(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    expected_path: str,
    *,
    maximum: int,
) -> bytes:
    matches = [item for item in members if PurePosixPath(item.name).as_posix() == expected_path]
    if len(matches) != 1:
        raise RuntimeError(f"Syncthing archive must contain exactly one {expected_path}")
    item = matches[0]
    if not item.isfile() or item.issym() or item.islnk() or item.size > maximum:
        raise RuntimeError(f"Syncthing archive contains an unsafe {expected_path}")
    opened = archive.extractfile(item)
    if opened is None:
        raise RuntimeError(f"Could not read {expected_path} from Syncthing archive")
    payload = opened.read(maximum + 1)
    if len(payload) > maximum or len(payload) != item.size:
        raise RuntimeError(f"Syncthing {expected_path} has an invalid extracted size")
    return payload


def _install(path: str | os.PathLike[str], payload: bytes, mode: int) -> None:
    destination = os.fspath(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, mode)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError(f"Short write while installing {destination}")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    installed = os.stat(destination, follow_symlinks=False)
    if not stat.S_ISREG(installed.st_mode) or stat.S_IMODE(installed.st_mode) != mode:
        raise RuntimeError(f"Installed file has wrong type or permissions: {destination}")


def install_archive(
    payload: bytes,
    architecture: str,
    *,
    binary_path: str | os.PathLike[str],
    license_path: str | os.PathLike[str],
) -> str:
    """Verify and install one pinned official release archive."""

    expected = ARCHIVES.get(architecture)
    if expected is None:
        raise RuntimeError(f"No reviewed Syncthing binary for architecture {architecture!r}")
    actual = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual, expected):
        raise RuntimeError(f"Syncthing archive digest mismatch: expected {expected}, got {actual}")
    root = _root_name(architecture)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = _validated_members(archive, architecture=architecture)
        binary = _member(archive, members, f"{root}/syncthing", maximum=MAX_BINARY_BYTES)
        license_text = _member(archive, members, f"{root}/LICENSE.txt", maximum=MAX_LICENSE_BYTES)
    license_parent = Path(license_path).parent
    os.makedirs(license_parent, mode=0o755, exist_ok=False)
    _install(binary_path, binary, 0o755)
    _install(license_path, license_text, 0o644)
    return actual


def main() -> int:
    architecture = os.environ.get("TARGETARCH", "").strip() or "amd64"
    if architecture not in ARCHIVES:
        raise RuntimeError(f"No reviewed Syncthing binary for architecture {architecture!r}")
    filename = _filename(architecture)
    url = f"https://github.com/syncthing/syncthing/releases/download/v{VERSION}/{filename}"
    payload = _download(url)
    actual = install_archive(
        payload,
        architecture,
        binary_path="/usr/local/bin/syncthing",
        license_path="/usr/share/licenses/syncthing/LICENSE.txt",
    )
    print(f"installed Syncthing v{VERSION} for linux/{architecture} ({actual})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
