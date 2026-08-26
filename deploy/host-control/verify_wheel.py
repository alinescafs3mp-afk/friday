#!/usr/bin/python3
"""Fail-closed validation for the offline Friday Host Control wheel."""

from __future__ import annotations

import hashlib
import hmac
import re
import stat
import sys
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import NoReturn

_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}").fullmatch
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,79}").fullmatch


def _fail(message: str) -> NoReturn:  # pragma: no cover
    raise ValueError(message)


def verify_wheel(path: Path, expected_sha256: str) -> tuple[str, str]:
    """Verify the copied artifact's digest and closed Friday wheel layout."""

    if _SHA256(expected_sha256) is None:
        _fail("expected SHA-256 must be 64 lowercase hexadecimal characters")
    if not path.is_file() or path.is_symlink():
        _fail("artifact must be one regular non-symlink file")
    size = path.stat().st_size
    if size <= 0 or size > _MAX_ARCHIVE_BYTES:
        _fail("artifact size is outside the release envelope")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if not hmac.compare_digest(digest, expected_sha256):
        _fail("artifact SHA-256 does not match the release manifest")

    with zipfile.ZipFile(path) as wheel:
        infos = wheel.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            _fail("wheel contains duplicate member names")
        total_size = 0
        for info in infos:
            member = PurePosixPath(info.filename)
            if (
                not info.filename
                or "\\" in info.filename
                or "\x00" in info.filename
                or member.is_absolute()
                or any(part in {"", ".", ".."} for part in member.parts)
                or info.flag_bits & 0x1
            ):
                _fail("wheel contains an unsafe member path or encryption")
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(unix_mode):
                _fail("wheel contains a symbolic link")
            if info.file_size > _MAX_MEMBER_BYTES:
                _fail("wheel member exceeds the release envelope")
            total_size += info.file_size
        if total_size > _MAX_UNCOMPRESSED_BYTES:
            _fail("wheel expands beyond the release envelope")

        metadata_names = [name for name in names if re.fullmatch(r"friday-[^/]+\.dist-info/METADATA", name)]
        if len(metadata_names) != 1:
            _fail("wheel must contain one Friday distribution identity")
        dist_info = metadata_names[0].removesuffix("METADATA")
        allowed_roots = ("friday/", "friday_host_agent/", "friday_package_broker/", dist_info)
        if any(not name.startswith(allowed_roots) for name in names):
            _fail("wheel contains an unexpected top-level payload")

        required = {
            "friday/host_control/__init__.py",
            "friday_host_agent/__main__.py",
            "friday_host_agent/daemon.py",
            "friday_package_broker/__main__.py",
            "friday_package_broker/approval.py",
            "friday_package_broker/daemon.py",
            f"{dist_info}WHEEL",
            f"{dist_info}RECORD",
            f"{dist_info}entry_points.txt",
            f"{dist_info}licenses/LICENSE",
        }
        if missing := sorted(required.difference(names)):
            _fail(f"wheel is missing Host Control release members: {missing}")

        metadata = Parser().parsestr(wheel.read(metadata_names[0]).decode("utf-8"))
        if str(metadata.get("Name") or "").casefold() != "friday":
            _fail("wheel project identity is not Friday")
        version = str(metadata.get("Version") or "")
        if _VERSION(version) is None:
            _fail("wheel has an invalid release version")
        entry_points = wheel.read(f"{dist_info}entry_points.txt").decode("utf-8")
        for expected in (
            "friday-host-agent = friday_host_agent.__main__:main",
            "friday-package-broker = friday_package_broker.__main__:main",
        ):
            if expected not in entry_points.splitlines():
                _fail("wheel is missing an exact Host Control entrypoint")
        if len(wheel.read(f"{dist_info}licenses/LICENSE")) < 256:
            _fail("wheel carries an invalid license payload")
    return version, digest


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 2:
        print("usage: verify_wheel.py WHEEL SHA256", file=sys.stderr)
        return 2
    try:
        version, digest = verify_wheel(Path(arguments[0]), arguments[1])
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
        print(f"Friday wheel verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"friday {version} sha256:{digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
