#!/usr/bin/env python3
"""Build and verify the closed, deterministic Host Control release bundle."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
import zlib
from contextlib import suppress
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

SCHEMA = "friday.host-control-release-bundle.v1"
MANIFEST_NAME = "manifest.json"
GIT = "/usr/bin/git"
MAX_WHEEL_BYTES = 64 * 1024 * 1024
MAX_WHEEL_MEMBER_BYTES = 64 * 1024 * 1024
MAX_WHEEL_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_DEPLOY_FILE_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
MAX_ARCHIVE_BYTES = 96 * 1024 * 1024
MAX_TAR_BYTES = 128 * 1024 * 1024

DEPLOY_FILES = (
    "README.md",
    "compose.override.yml",
    "examples/host-agent-policy.toml",
    "examples/policy.toml",
    "install.sh",
    "prepare_user_assets.py",
    "systemd/system/friday-package-broker.service",
    "systemd/system/friday-package-broker.socket",
    "systemd/tmpfiles/friday-host-agent.conf.in",
    "systemd/user/friday-host-agent.service",
    "uninstall.sh",
    "verify_wheel.py",
)
EXECUTABLE_DEPLOY_FILES = frozenset(
    {"install.sh", "prepare_user_assets.py", "uninstall.sh", "verify_wheel.py"}
)

_HEX40 = re.compile(r"[0-9a-f]{40}").fullmatch
_HEX64 = re.compile(r"[0-9a-f]{64}").fullmatch
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)").fullmatch
_WHEEL_NAME = re.compile(r"friday-([0-9]+\.[0-9]+\.[0-9]+)-py3-none-any\.whl").fullmatch
_GZIP_HEADER = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff"


class BundleError(ValueError):
    """A closed release-bundle contract was not satisfied."""


@dataclass(frozen=True, slots=True)
class BundleReceipt:
    schema: str
    source_commit: str
    version: str
    archive_name: str
    archive_sha256: str
    archive_size: int
    wheel_name: str
    wheel_sha256: str

    def public_dict(self) -> dict[str, str | int]:
        return {
            "schema": self.schema,
            "source_commit": self.source_commit,
            "version": self.version,
            "archive_name": self.archive_name,
            "archive_sha256": self.archive_sha256,
            "archive_size": self.archive_size,
            "wheel_name": self.wheel_name,
            "wheel_sha256": self.wheel_sha256,
        }


@dataclass(frozen=True, slots=True)
class _Payload:
    name: str
    data: bytes
    mode: int


def _fail(message: str) -> NoReturn:
    raise BundleError(message)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_directory(path: Path, *, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        _fail(f"{label} must be an absolute canonical path")
    try:
        resolved = candidate.resolve(strict=True)
        details = os.lstat(candidate)
    except OSError as exc:
        raise BundleError(f"{label} is unavailable") from exc
    if resolved != candidate or not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        _fail(f"{label} must be one canonical non-symlink directory")
    return resolved


def _canonical_regular(path: Path, *, maximum: int, label: str) -> tuple[Path, bytes]:
    candidate = Path(path)
    if not candidate.is_absolute():
        _fail(f"{label} must be an absolute canonical path")
    try:
        resolved = candidate.resolve(strict=True)
        lexical = os.lstat(candidate)
    except OSError as exc:
        raise BundleError(f"{label} is unavailable") from exc
    if (
        resolved != candidate
        or stat.S_ISLNK(lexical.st_mode)
        or not stat.S_ISREG(lexical.st_mode)
        or lexical.st_size <= 0
        or lexical.st_size > maximum
    ):
        _fail(f"{label} must be one bounded canonical regular non-symlink file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (lexical.st_dev, lexical.st_ino)
            or opened.st_size != lexical.st_size
        ):
            _fail(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) != opened.st_size or len(data) > maximum:
            _fail(f"{label} changed or exceeded its bound while it was read")
    finally:
        os.close(descriptor)
    return resolved, data


def _safe_member_name(value: str) -> str:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        _fail("archive member names must be ASCII")
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or str(path) != value
    ):
        _fail("archive contains an unsafe member path")
    return value


def _git(root: Path, *arguments: str) -> bytes:
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/bin:/bin",
    }
    try:
        completed = subprocess.run(  # noqa: S603 - fixed /usr/bin/git and argv
            [GIT, "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            env=environment,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BundleError("Git attestation could not run") from exc
    if completed.returncode != 0:
        _fail("Git attestation failed")
    return completed.stdout


def _clean_source_commit(root: Path) -> str:
    top = _git(root, "rev-parse", "--show-toplevel").decode("utf-8", errors="strict").strip()
    if Path(top).resolve(strict=True) != root:
        _fail("source root is not the exact Git worktree root")
    replacement_refs = _git(root, "for-each-ref", "--format=%(refname)", "refs/replace/")
    if replacement_refs:
        _fail("Git replacement refs are forbidden in release source")
    commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    if _HEX40(commit) is None:
        _fail("Git HEAD is not one exact 40-hex commit")
    status = _git(
        root,
        "-c",
        "core.quotepath=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    if status:
        _fail("source worktree is dirty")
    return commit


def _head_blob(root: Path, relative: str) -> bytes:
    if relative.startswith("-") or _safe_member_name(relative) != relative:
        _fail("invalid source-relative path")
    return _git(root, "cat-file", "blob", f"HEAD:{relative}")


def _deploy_layout(root: Path) -> None:
    deploy = root / "deploy" / "host-control"
    if not deploy.is_dir() or deploy.is_symlink():
        _fail("deploy/host-control is missing or unsafe")
    expected_files = set(DEPLOY_FILES)
    expected_directories = {
        str(parent) for name in DEPLOY_FILES for parent in PurePosixPath(name).parents if str(parent) != "."
    }
    observed_files: set[str] = set()
    observed_directories: set[str] = set()
    for entry in deploy.rglob("*"):
        relative = entry.relative_to(deploy).as_posix()
        if "__pycache__" in PurePosixPath(relative).parts or relative.endswith((".pyc", ".pyo")):
            _fail("deploy/host-control contains Python cache artifacts")
        details = os.lstat(entry)
        if stat.S_ISLNK(details.st_mode):
            _fail("deploy/host-control contains a symbolic link")
        if stat.S_ISDIR(details.st_mode):
            observed_directories.add(relative)
        elif stat.S_ISREG(details.st_mode):
            observed_files.add(relative)
        else:
            _fail("deploy/host-control contains a special file")
    if observed_files != expected_files or observed_directories != expected_directories:
        _fail("deploy/host-control does not match the closed release file set")


def _source_version(root: Path) -> str:
    pyproject = _head_blob(root, "pyproject.toml")
    package = _head_blob(root, "friday/__init__.py")
    try:
        project = tomllib.loads(pyproject.decode("utf-8"))["project"]
    except (KeyError, TypeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise BundleError("pyproject release identity is invalid") from exc
    if not isinstance(project, dict):
        _fail("pyproject release identity is invalid")
    version = project.get("version")
    if project.get("name") != "friday" or not isinstance(version, str) or _VERSION(version) is None:
        _fail("pyproject release identity is invalid")
    try:
        declarations = re.findall(
            r'^__version__\s*=\s*"([^"]+)"\s*$',
            package.decode("utf-8"),
            flags=re.MULTILINE,
        )
    except UnicodeError as exc:
        raise BundleError("package release identity is invalid") from exc
    if declarations != [version]:
        _fail("package and pyproject versions do not match exactly")
    return version


def _record_digest(value: str) -> bytes:
    if not value.startswith("sha256="):
        _fail("wheel RECORD must use SHA-256")
    encoded = value.removeprefix("sha256=")
    if not encoded or "=" in encoded or re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
        _fail("wheel RECORD contains an invalid digest")
    try:
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as exc:
        raise BundleError("wheel RECORD contains an invalid digest") from exc
    if len(decoded) != 32:
        _fail("wheel RECORD contains an invalid SHA-256 digest")
    return decoded


def _validate_wheel(
    data: bytes,
    *,
    expected_name: str,
    expected_version: str,
) -> dict[str, bytes]:
    match = _WHEEL_NAME(expected_name)
    if match is None or match.group(1) != expected_version:
        _fail("wheel filename is not the canonical Friday release name")
    try:
        wheel = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise BundleError("wheel is not a valid ZIP archive") from exc
    with wheel:
        if wheel.comment:
            _fail("wheel ZIP comment is not permitted")
        infos = wheel.infolist()
        names = [item.filename for item in infos]
        if not names or len(names) != len(set(names)):
            _fail("wheel has no members or contains duplicate member names")
        total_size = 0
        payloads: dict[str, bytes] = {}
        for info in infos:
            name = _safe_member_name(info.filename)
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if (
                name.endswith("/")
                or "__pycache__" in PurePosixPath(name).parts
                or name.endswith((".pyc", ".pyo"))
                or info.flag_bits & 0x1
                or (unix_mode and not stat.S_ISREG(unix_mode))
                or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or info.file_size < 0
                or info.file_size > MAX_WHEEL_MEMBER_BYTES
            ):
                _fail("wheel contains an unsafe member")
            total_size += info.file_size
            if total_size > MAX_WHEEL_EXPANDED_BYTES:
                _fail("wheel expands beyond the release envelope")
            payload = wheel.read(info)
            if len(payload) != info.file_size:
                _fail("wheel member size changed while reading")
            payloads[name] = payload

        metadata_names = [name for name in names if re.fullmatch(r"friday-[^/]+\.dist-info/METADATA", name)]
        if metadata_names != [f"friday-{expected_version}.dist-info/METADATA"]:
            _fail("wheel has an invalid Friday distribution identity")
        dist_info = f"friday-{expected_version}.dist-info/"
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
        if required.difference(names):
            _fail("wheel is missing Host Control release members")

        try:
            metadata = Parser().parsestr(payloads[metadata_names[0]].decode("utf-8"))
        except UnicodeError as exc:
            raise BundleError("wheel metadata is not UTF-8") from exc
        if metadata.get_all("Name") != ["friday"] or metadata.get_all("Version") != [expected_version]:
            _fail("wheel metadata identity does not match the source release")
        entry_points = payloads[f"{dist_info}entry_points.txt"].decode("utf-8")
        for expected in (
            "friday-host-agent = friday_host_agent.__main__:main",
            "friday-package-broker = friday_package_broker.__main__:main",
        ):
            if expected not in entry_points.splitlines():
                _fail("wheel is missing an exact Host Control entrypoint")
        if len(payloads[f"{dist_info}licenses/LICENSE"]) < 256:
            _fail("wheel carries an invalid license payload")

        record_name = f"{dist_info}RECORD"
        try:
            rows = list(csv.reader(io.StringIO(payloads[record_name].decode("utf-8"), newline="")))
        except (csv.Error, UnicodeError) as exc:
            raise BundleError("wheel RECORD is invalid") from exc
        recorded: set[str] = set()
        for row in rows:
            if len(row) != 3 or row[0] in recorded or row[0] not in payloads:
                _fail("wheel RECORD layout is invalid")
            recorded.add(row[0])
            if row[0] == record_name:
                if row[1:] != ["", ""]:
                    _fail("wheel RECORD self-entry must be unhashed")
                continue
            if row[2] != str(len(payloads[row[0]])):
                _fail("wheel RECORD size does not match member bytes")
            if not hmac.compare_digest(_record_digest(row[1]), hashlib.sha256(payloads[row[0]]).digest()):
                _fail("wheel RECORD digest does not match member bytes")
        if recorded != set(names):
            _fail("wheel RECORD does not cover the exact archive")
    return {
        name: payload
        for name, payload in payloads.items()
        if name.startswith(("friday/", "friday_host_agent/", "friday_package_broker/"))
    }


def _head_package_payloads(root: Path) -> dict[str, bytes]:
    raw_entries = _git(
        root,
        "ls-tree",
        "-r",
        "-z",
        "HEAD",
        "--",
        "friday",
        "friday_host_agent",
        "friday_package_broker",
    )
    payloads: dict[str, bytes] = {}
    for raw_entry in raw_entries.split(b"\0"):
        if not raw_entry:
            continue
        header, separator, raw_name = raw_entry.partition(b"\t")
        fields = header.split(b" ")
        if not separator or len(fields) != 3:
            _fail("Git package tree has an invalid entry")
        mode, object_type, raw_object_id = fields
        try:
            name = raw_name.decode("utf-8")
        except UnicodeError as exc:
            raise BundleError("Git package path is not UTF-8") from exc
        _safe_member_name(name)
        if not (name.endswith(".py") or name.startswith("friday/admin_ui/static/")):
            continue
        try:
            object_id = raw_object_id.decode("ascii")
        except UnicodeError as exc:
            raise BundleError("Git package tree has an invalid object ID") from exc
        if mode not in {b"100644", b"100755"} or object_type != b"blob" or _HEX40(object_id) is None:
            _fail("Git package payload is not a tracked regular blob")
        payloads[name] = _git(root, "cat-file", "blob", object_id)
    if not payloads:
        _fail("Git commit contains no Friday package payload")
    return payloads


def _head_deploy_payloads(root: Path) -> dict[str, bytes]:
    """Return the exact closed deploy payload committed at HEAD.

    Git modes are part of the release input: accepting a symlink, gitlink, or a
    script whose executable bit drifted would make the archive's canonical mode
    an unaudited transformation instead of an attestation of the source commit.
    """

    prefix = "deploy/host-control/"
    expected = set(DEPLOY_FILES)
    raw_entries = _git(root, "ls-tree", "-r", "-z", "HEAD", "--", prefix.removesuffix("/"))
    payloads: dict[str, bytes] = {}
    for raw_entry in raw_entries.split(b"\0"):
        if not raw_entry:
            continue
        header, separator, raw_name = raw_entry.partition(b"\t")
        fields = header.split(b" ")
        if not separator or len(fields) != 3:
            _fail("Git deploy tree has an invalid entry")
        mode, object_type, raw_object_id = fields
        try:
            name = raw_name.decode("utf-8")
            object_id = raw_object_id.decode("ascii")
        except UnicodeError as exc:
            raise BundleError("Git deploy tree has an invalid path or object ID") from exc
        _safe_member_name(name)
        if not name.startswith(prefix):
            _fail("Git deploy tree escaped its release prefix")
        relative = name.removeprefix(prefix)
        if relative not in expected or relative in payloads:
            _fail("Git deploy tree does not match the closed release file set")
        expected_mode = b"100755" if relative in EXECUTABLE_DEPLOY_FILES else b"100644"
        if mode != expected_mode or object_type != b"blob" or _HEX40(object_id) is None:
            _fail("Git deploy payload has an invalid type or mode")
        payloads[relative] = _git(root, "cat-file", "blob", object_id)
    if set(payloads) != expected:
        _fail("Git deploy tree does not match the closed release file set")
    return payloads


def _attest_wheel_to_commit(root: Path, wheel_payloads: dict[str, bytes]) -> None:
    committed = _head_package_payloads(root)
    if set(wheel_payloads) != set(committed):
        _fail("wheel package inventory does not match the exact Git commit")
    for name, expected in committed.items():
        if not hmac.compare_digest(wheel_payloads[name], expected):
            _fail("wheel package bytes do not match the exact Git commit")


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )


def _strict_json(data: bytes) -> Any:
    if not data or len(data) > MAX_MANIFEST_BYTES or data.startswith(b"\xef\xbb\xbf"):
        _fail("bundle manifest size or encoding is invalid")

    def closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail("bundle manifest contains a duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("ascii"), object_pairs_hook=closed_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError("bundle manifest is not strict canonical JSON") from exc
    if _canonical_json(value) != data:
        _fail("bundle manifest is not canonical JSON")
    return value


def _manifest(
    *,
    commit: str,
    version: str,
    wheel_name: str,
    wheel_data: bytes,
    deploy_payloads: dict[str, bytes],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "source_commit": commit,
        "version": version,
        "wheel": {
            "name": wheel_name,
            "path": f"wheel/{wheel_name}",
            "sha256": _sha256(wheel_data),
            "size": len(wheel_data),
        },
        "deploy": [
            {
                "mode": "0755" if relative in EXECUTABLE_DEPLOY_FILES else "0644",
                "path": f"deploy/host-control/{relative}",
                "sha256": _sha256(deploy_payloads[relative]),
                "size": len(deploy_payloads[relative]),
            }
            for relative in DEPLOY_FILES
        ],
    }


def _payloads(
    manifest_data: bytes,
    wheel_name: str,
    wheel_data: bytes,
    deploy_payloads: dict[str, bytes],
) -> list[_Payload]:
    return [
        _Payload(MANIFEST_NAME, manifest_data, 0o644),
        _Payload(f"wheel/{wheel_name}", wheel_data, 0o644),
        *[
            _Payload(
                f"deploy/host-control/{relative}",
                deploy_payloads[relative],
                0o755 if relative in EXECUTABLE_DEPLOY_FILES else 0o644,
            )
            for relative in DEPLOY_FILES
        ],
    ]


def _tar_bytes(payloads: list[_Payload]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for payload in payloads:
            _safe_member_name(payload.name)
            info = tarfile.TarInfo(payload.name)
            info.type = tarfile.REGTYPE
            info.mode = payload.mode
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            info.size = len(payload.data)
            info.linkname = ""
            info.devmajor = 0
            info.devminor = 0
            info.pax_headers = {}
            archive.addfile(info, io.BytesIO(payload.data))
    data = output.getvalue()
    if not data or len(data) > MAX_TAR_BYTES:
        _fail("bundle tar stream exceeds the release envelope")
    return data


def _gzip_bytes(data: bytes) -> bytes:
    compressor = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=-zlib.MAX_WBITS)
    body = compressor.compress(data) + compressor.flush()
    footer = struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)
    return _GZIP_HEADER + body + footer


def _decompress_archive(data: bytes) -> bytes:
    if len(data) < 18 or data[:10] != _GZIP_HEADER:
        _fail("bundle does not use canonical deterministic gzip metadata")
    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    try:
        expanded = decompressor.decompress(data, MAX_TAR_BYTES + 1)
        if len(expanded) > MAX_TAR_BYTES or decompressor.unconsumed_tail:
            _fail("bundle expands beyond the release envelope")
        expanded += decompressor.flush(MAX_TAR_BYTES + 1 - len(expanded))
    except zlib.error as exc:
        raise BundleError("bundle gzip stream is invalid") from exc
    if (
        not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
        or len(expanded) > MAX_TAR_BYTES
    ):
        _fail("bundle gzip stream has trailing data or exceeds its envelope")
    return expanded


def _safe_output(path: Path, *, root: Path, wheel: Path, version: str) -> tuple[Path, Path]:
    output = Path(path)
    if not output.is_absolute() or output.name != f"friday-host-control-{version}.tar.gz":
        _fail("output must use the canonical absolute release filename")
    parent = _canonical_directory(output.parent, label="output parent")
    canonical = parent / output.name
    sidecar = Path(f"{canonical}.sha256")
    if canonical != output or canonical == wheel or canonical.is_relative_to(root):
        _fail("output path overlaps trusted release inputs")
    if os.path.lexists(canonical) or os.path.lexists(sidecar):
        _fail("output archive and sidecar must not already exist")
    return canonical, sidecar


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_new(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        handle = os.fdopen(descriptor, "wb", closefd=True)
        descriptor = -1
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        _fsync_directory(path.parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


def build_bundle(*, source_root: Path, wheel: Path, output: Path) -> BundleReceipt:
    """Build one deterministic archive from a clean exact source commit."""

    root = _canonical_directory(source_root, label="source root")
    _deploy_layout(root)
    commit = _clean_source_commit(root)
    committed_deploy_payloads = _head_deploy_payloads(root)
    version = _source_version(root)
    wheel_path, wheel_data = _canonical_regular(wheel, maximum=MAX_WHEEL_BYTES, label="wheel")
    if wheel_path.is_relative_to(root):
        _fail("wheel must be outside the source worktree")
    wheel_payloads = _validate_wheel(
        wheel_data,
        expected_name=wheel_path.name,
        expected_version=version,
    )
    _attest_wheel_to_commit(root, wheel_payloads)
    archive_path, sidecar_path = _safe_output(output, root=root, wheel=wheel_path, version=version)

    deploy_payloads: dict[str, bytes] = {}
    for relative in DEPLOY_FILES:
        source = root / "deploy" / "host-control" / relative
        _path, data = _canonical_regular(
            source,
            maximum=MAX_DEPLOY_FILE_BYTES,
            label=f"deploy file {relative}",
        )
        if not hmac.compare_digest(data, committed_deploy_payloads[relative]):
            _fail("deploy file bytes do not match Git HEAD")
        deploy_payloads[relative] = data

    manifest = _manifest(
        commit=commit,
        version=version,
        wheel_name=wheel_path.name,
        wheel_data=wheel_data,
        deploy_payloads=deploy_payloads,
    )
    manifest_data = _canonical_json(manifest)
    archive_data = _gzip_bytes(
        _tar_bytes(_payloads(manifest_data, wheel_path.name, wheel_data, deploy_payloads))
    )
    if len(archive_data) > MAX_ARCHIVE_BYTES:
        _fail("bundle archive exceeds the release envelope")

    _deploy_layout(root)
    if _clean_source_commit(root) != commit:
        _fail("source commit changed during bundle construction")
    _path, stable_wheel = _canonical_regular(wheel_path, maximum=MAX_WHEEL_BYTES, label="wheel")
    if not hmac.compare_digest(_sha256(stable_wheel), str(manifest["wheel"]["sha256"])):
        _fail("wheel changed during bundle construction")

    digest = _sha256(archive_data)
    sidecar_data = f"{digest}  {archive_path.name}\n".encode("ascii")
    try:
        _publish_new(archive_path, archive_data)
        _publish_new(sidecar_path, sidecar_data)
    except BaseException:
        try:
            archive_path.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)
            _fsync_directory(archive_path.parent)
        except OSError:
            pass
        raise
    return BundleReceipt(
        schema=SCHEMA,
        source_commit=commit,
        version=version,
        archive_name=archive_path.name,
        archive_sha256=digest,
        archive_size=len(archive_data),
        wheel_name=wheel_path.name,
        wheel_sha256=str(manifest["wheel"]["sha256"]),
    )


def _validate_manifest(value: Any) -> tuple[str, str, dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "source_commit",
        "version",
        "wheel",
        "deploy",
    }:
        _fail("bundle manifest top-level schema is invalid")
    commit = value.get("source_commit")
    version = value.get("version")
    wheel = value.get("wheel")
    deploy = value.get("deploy")
    if (
        value.get("schema") != SCHEMA
        or not isinstance(commit, str)
        or _HEX40(commit) is None
        or not isinstance(version, str)
        or _VERSION(version) is None
        or not isinstance(wheel, dict)
        or set(wheel) != {"name", "path", "sha256", "size"}
        or not isinstance(deploy, list)
        or len(deploy) != len(DEPLOY_FILES)
    ):
        _fail("bundle manifest identity is invalid")
    expected_wheel_name = f"friday-{version}-py3-none-any.whl"
    if (
        wheel.get("name") != expected_wheel_name
        or wheel.get("path") != f"wheel/{expected_wheel_name}"
        or not isinstance(wheel.get("sha256"), str)
        or _HEX64(wheel["sha256"]) is None
        or type(wheel.get("size")) is not int
        or not 0 < wheel["size"] <= MAX_WHEEL_BYTES
    ):
        _fail("bundle manifest wheel identity is invalid")
    typed_deploy: list[dict[str, Any]] = []
    for index, relative in enumerate(DEPLOY_FILES):
        item = deploy[index]
        expected_mode = "0755" if relative in EXECUTABLE_DEPLOY_FILES else "0644"
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "mode", "sha256", "size"}
            or item.get("path") != f"deploy/host-control/{relative}"
            or item.get("mode") != expected_mode
            or not isinstance(item.get("sha256"), str)
            or _HEX64(item["sha256"]) is None
            or type(item.get("size")) is not int
            or not 0 < item["size"] <= MAX_DEPLOY_FILE_BYTES
        ):
            _fail("bundle manifest deploy inventory is invalid")
        typed_deploy.append(item)
    return commit, version, wheel, typed_deploy


def _archive_payloads(tar_data: bytes) -> tuple[bytes, dict[str, bytes]]:
    try:
        payloads: dict[str, bytes] = {}
        with tarfile.open(fileobj=io.BytesIO(tar_data), mode="r:") as archive:
            members = archive.getmembers()
            if len(members) != len(DEPLOY_FILES) + 2:
                _fail("bundle archive member count is invalid")
            for member in members:
                name = _safe_member_name(member.name)
                if name in payloads:
                    _fail("bundle archive contains a duplicate member")
                if (
                    member.type != tarfile.REGTYPE
                    or not member.isreg()
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname != ""
                    or member.gname != ""
                    or member.mtime != 0
                    or member.linkname != ""
                    or member.pax_headers
                    or member.devmajor != 0
                    or member.devminor != 0
                    or member.size < 0
                    or member.size > MAX_WHEEL_BYTES
                ):
                    _fail("bundle archive member metadata is invalid")
                extracted = archive.extractfile(member)
                if extracted is None:
                    _fail("bundle regular member cannot be read")
                data = extracted.read(member.size + 1)
                if len(data) != member.size:
                    _fail("bundle member size does not match its header")
                payloads[name] = data
    except tarfile.TarError as exc:
        raise BundleError("bundle tar stream is invalid") from exc
    manifest_data = payloads.get(MANIFEST_NAME)
    if manifest_data is None:
        _fail("bundle manifest is missing")
    return manifest_data, payloads


def verify_bundle(*, archive: Path, expected_sha256: str) -> BundleReceipt:
    """Verify an externally digest-bound bundle completely without extraction."""

    if _HEX64(expected_sha256) is None:
        _fail("expected SHA-256 must be 64 lowercase hexadecimal characters")
    archive_path, archive_data = _canonical_regular(
        archive,
        maximum=MAX_ARCHIVE_BYTES,
        label="bundle archive",
    )
    observed_digest = _sha256(archive_data)
    if not hmac.compare_digest(observed_digest, expected_sha256):
        _fail("bundle archive does not match the external expected SHA-256")
    tar_data = _decompress_archive(archive_data)
    manifest_data, observed_payloads = _archive_payloads(tar_data)
    manifest = _strict_json(manifest_data)
    commit, version, wheel, deploy = _validate_manifest(manifest)
    if archive_path.name != f"friday-host-control-{version}.tar.gz":
        _fail("bundle archive filename does not match its manifest version")

    expected_names = [
        MANIFEST_NAME,
        str(wheel["path"]),
        *(str(item["path"]) for item in deploy),
    ]
    if list(observed_payloads) != expected_names:
        _fail("bundle archive order or closed member set is invalid")
    wheel_data = observed_payloads[str(wheel["path"])]
    if len(wheel_data) != wheel["size"] or not hmac.compare_digest(_sha256(wheel_data), str(wheel["sha256"])):
        _fail("bundle wheel bytes do not match the manifest")
    _validate_wheel(wheel_data, expected_name=str(wheel["name"]), expected_version=version)

    deploy_payloads: dict[str, bytes] = {}
    for relative, item in zip(DEPLOY_FILES, deploy, strict=True):
        data = observed_payloads[str(item["path"])]
        if len(data) != item["size"] or not hmac.compare_digest(_sha256(data), str(item["sha256"])):
            _fail("bundle deploy bytes do not match the manifest")
        deploy_payloads[relative] = data

    rebuilt_tar = _tar_bytes(_payloads(manifest_data, str(wheel["name"]), wheel_data, deploy_payloads))
    if not hmac.compare_digest(rebuilt_tar, tar_data):
        _fail("bundle tar stream is not canonical")
    return BundleReceipt(
        schema=SCHEMA,
        source_commit=commit,
        version=version,
        archive_name=archive_path.name,
        archive_sha256=observed_digest,
        archive_size=len(archive_data),
        wheel_name=str(wheel["name"]),
        wheel_sha256=str(wheel["sha256"]),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="build one clean-commit Host Control bundle")
    build.add_argument(
        "--source-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    build.add_argument("--wheel", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="verify a bundle against an external SHA-256")
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--expected-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if arguments.command == "build":
            receipt = build_bundle(
                source_root=arguments.source_root,
                wheel=arguments.wheel,
                output=arguments.output,
            )
        else:
            receipt = verify_bundle(
                archive=arguments.archive,
                expected_sha256=arguments.expected_sha256,
            )
    except (BundleError, OSError) as exc:
        print(f"Host Control release bundle failed: {exc}", file=sys.stderr)
        return 1
    print(_canonical_json(receipt.public_dict()).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
