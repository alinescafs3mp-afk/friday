#!/usr/bin/env python3
"""Audit and explicitly install Friday's rootless document toolchain.

The default action is read-only.  It asks the local Ubuntu APT solver for the
exact no-recommends closure, binds that answer to the current dpkg state, and
publishes only package metadata and hashes.  ``install`` must repeat the same
plan and receive its exact id as confirmation before it downloads anything.

Installation never invokes dpkg as a package manager.  Signed Ubuntu DEBs are
downloaded by APT, verified against their signed-index SHA256 records, extracted
under an owner-private versioned directory, and sealed without write bits.  The
only activation is an atomic replacement of trusted user-local command links;
Friday's environment and release directories are deliberately out of scope.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import stat
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn, Protocol, TextIO
from urllib.parse import urlsplit

AUDIT_SCHEMA = "friday.document-toolchain.audit.v1"
MANIFEST_SCHEMA = "friday.document-toolchain.manifest.v1"

APT_GET = "/usr/bin/apt-get"
APT_CACHE = "/usr/bin/apt-cache"
APT = "/usr/bin/apt"
DPKG = "/usr/bin/dpkg"
DPKG_DEB = "/usr/bin/dpkg-deb"
BWRAP = "/usr/bin/bwrap"

ROOT_PACKAGES = (
    "tesseract-ocr",
    "tesseract-ocr-rus",
    "libreoffice-core-nogui",
    "libreoffice-writer-nogui",
    "libreoffice-calc-nogui",
    "libreoffice-impress-nogui",
)
COMMAND_NAMES = ("tesseract", "libreoffice")

MAX_COMMAND_OUTPUT_BYTES = 16 << 20
MAX_PACKAGES = 256
MAX_TOTAL_DEB_BYTES = 2 << 30
MAX_DEB_BYTES = 512 << 20
MAX_OS_RELEASE_BYTES = 64 << 10
MAX_DPKG_STATUS_BYTES = 64 << 20
RESOLVE_TIMEOUT_SEC = 60.0
METADATA_TIMEOUT_SEC = 15.0
DOWNLOAD_TIMEOUT_SEC = 3_600.0
EXTRACT_TIMEOUT_SEC = 120.0

_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,127}")
_VERSION = re.compile(r"\S{1,255}")
_ARCHITECTURE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MULTIARCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_FAILURE_CODE = re.compile(r"[a-z0-9_]{1,96}")
_INSTALL_LINE = re.compile(
    r"^Inst (?P<package>[a-z0-9][a-z0-9+.-]{0,127})(?::[a-z0-9_-]+)? "
    r"(?:\[[^\]\r\n]+\] )?\((?P<version>\S{1,255}) [^\r\n]*"
    r"\[(?P<architecture>[a-z0-9][a-z0-9_-]{0,63})\]\)$"
)
_MULTIARCH_BY_DEB_ARCH = {
    "amd64": "x86_64-linux-gnu",
    "arm64": "aarch64-linux-gnu",
    "armhf": "arm-linux-gnueabihf",
    "i386": "i386-linux-gnu",
    "ppc64el": "powerpc64le-linux-gnu",
    "riscv64": "riscv64-linux-gnu",
    "s390x": "s390x-linux-gnu",
}


class ProvisionFailure(RuntimeError):
    """One closed failure code that is safe to place in a public receipt."""

    def __init__(self, code: str) -> None:
        if _FAILURE_CODE.fullmatch(code) is None:
            code = "invalid_failure_code"
        super().__init__(code)
        self.code = code


class Runner(Protocol):
    def capture(
        self,
        command: Sequence[str],
        *,
        failure_code: str,
        timeout: float,
        max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
    ) -> bytes: ...

    def quiet(
        self,
        command: Sequence[str],
        *,
        failure_code: str,
        timeout: float,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None: ...


class SubprocessRunner:
    """A closed-environment runner which never republishes child output."""

    _environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "DEBIAN_FRONTEND": "noninteractive",
    }

    def capture(
        self,
        command: Sequence[str],
        *,
        failure_code: str,
        timeout: float,
        max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
    ) -> bytes:
        try:
            completed = subprocess.run(  # nosec B603
                tuple(command),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._environment,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ProvisionFailure(failure_code) from None
        if completed.returncode != 0 or len(completed.stdout) > max_output_bytes:
            raise ProvisionFailure(failure_code)
        return completed.stdout

    def quiet(
        self,
        command: Sequence[str],
        *,
        failure_code: str,
        timeout: float,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        child_environment = dict(self._environment)
        if environment is not None:
            if set(environment) - {"HOME", "TMPDIR"}:
                raise ProvisionFailure("child_environment_invalid")
            child_environment.update(environment)
        try:
            completed = subprocess.run(  # nosec B603
                tuple(command),
                check=False,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child_environment,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ProvisionFailure(failure_code) from None
        if completed.returncode != 0:
            raise ProvisionFailure(failure_code)


@dataclass(frozen=True, slots=True)
class HostIdentity:
    os_id: str
    version_id: str
    architecture: str
    multiarch: str
    dpkg_status_sha256: str


@dataclass(frozen=True, slots=True)
class PackageRecord:
    package: str
    version: str
    architecture: str
    filename: str
    size: int
    sha256: str
    origin: str = "Ubuntu"
    apt_source: str = ""

    @property
    def apt_spec(self) -> str:
        architecture = "" if self.architecture == "all" else f":{self.architecture}"
        return f"{self.package}{architecture}={self.version}"


@dataclass(frozen=True, slots=True)
class ToolchainPlan:
    manifest: Mapping[str, object]
    packages: tuple[PackageRecord, ...]
    plan_sha256: str
    toolchain_id: str

    @property
    def manifest_bytes(self) -> bytes:
        return _canonical_json(self.manifest) + b"\n"


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise ProvisionFailure("package_file_invalid") from None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
            raise ProvisionFailure("package_file_invalid")
        while chunk := os.read(descriptor, 1 << 20):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _sha256_limited_file(path: Path, *, limit: int, failure_code: str) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1 << 20):
                total += len(chunk)
                if total > limit:
                    raise ProvisionFailure(failure_code)
                digest.update(chunk)
    except OSError:
        raise ProvisionFailure(failure_code) from None
    return digest.hexdigest()


def _read_os_release(path: Path) -> tuple[str, str]:
    try:
        raw = path.read_bytes()
    except OSError:
        raise ProvisionFailure("host_identity_unavailable") from None
    if len(raw) > MAX_OS_RELEASE_BYTES or b"\x00" in raw:
        raise ProvisionFailure("host_identity_invalid")
    values: dict[str, str] = {}
    for encoded_line in raw.splitlines():
        try:
            line = encoded_line.decode("utf-8")
        except UnicodeDecodeError:
            raise ProvisionFailure("host_identity_invalid") from None
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            parts = shlex.split(value, posix=True)
        except ValueError:
            raise ProvisionFailure("host_identity_invalid") from None
        if len(parts) != 1:
            raise ProvisionFailure("host_identity_invalid")
        values[key] = parts[0]
    os_id = values.get("ID", "").casefold()
    version_id = values.get("VERSION_ID", "")
    if os_id != "ubuntu" or re.fullmatch(r"[0-9]{2}\.[0-9]{2}", version_id) is None:
        raise ProvisionFailure("unsupported_host")
    return os_id, version_id


def _single_token(
    value: bytes,
    pattern: re.Pattern[str],
    *,
    failure_code: str,
) -> str:
    try:
        rendered = value.decode("ascii").strip()
    except UnicodeDecodeError:
        raise ProvisionFailure(failure_code) from None
    if pattern.fullmatch(rendered) is None:
        raise ProvisionFailure(failure_code)
    return rendered


def discover_host(
    runner: Runner,
    *,
    os_release: Path = Path("/etc/os-release"),
    dpkg_status: Path = Path("/var/lib/dpkg/status"),
) -> HostIdentity:
    os_id, version_id = _read_os_release(os_release)
    architecture = _single_token(
        runner.capture(
            (DPKG, "--print-architecture"),
            failure_code="architecture_unavailable",
            timeout=METADATA_TIMEOUT_SEC,
        ),
        _ARCHITECTURE,
        failure_code="architecture_invalid",
    )
    multiarch = _MULTIARCH_BY_DEB_ARCH.get(architecture, "")
    if _MULTIARCH.fullmatch(multiarch) is None:
        raise ProvisionFailure("multiarch_unavailable")
    return HostIdentity(
        os_id=os_id,
        version_id=version_id,
        architecture=architecture,
        multiarch=multiarch,
        dpkg_status_sha256=_sha256_limited_file(
            dpkg_status,
            limit=MAX_DPKG_STATUS_BYTES,
            failure_code="dpkg_status_invalid",
        ),
    )


def _parse_install_set(output: bytes, *, host_architecture: str) -> tuple[tuple[str, str, str], ...]:
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        raise ProvisionFailure("apt_solver_output_invalid") from None
    if "\nRemv " in f"\n{text}" or "WARNING: The following essential packages will be removed" in text:
        raise ProvisionFailure("apt_solver_unsafe")
    resolved: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        if not line.startswith("Inst "):
            continue
        match = _INSTALL_LINE.fullmatch(line)
        if match is None:
            raise ProvisionFailure("apt_solver_output_invalid")
        package = match.group("package")
        version = match.group("version")
        architecture = match.group("architecture")
        if (
            _PACKAGE.fullmatch(package) is None
            or _VERSION.fullmatch(version) is None
            or architecture not in {"all", host_architecture}
        ):
            raise ProvisionFailure("apt_solver_output_invalid")
        key = (package, architecture)
        previous = resolved.setdefault(key, version)
        if previous != version:
            raise ProvisionFailure("apt_solver_output_invalid")
    if not resolved or len(resolved) > MAX_PACKAGES:
        raise ProvisionFailure("apt_solver_closure_invalid")
    names = {package for package, _architecture in resolved}
    if not set(ROOT_PACKAGES).issubset(names):
        raise ProvisionFailure("apt_solver_closure_incomplete")
    return tuple(
        sorted(
            ((package, version, architecture) for (package, architecture), version in resolved.items()),
            key=lambda item: (item[0], item[2], item[1]),
        )
    )


def _deb822_stanzas(output: bytes) -> list[dict[str, str]]:
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError:
        raise ProvisionFailure("package_metadata_invalid") from None
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in (*text.splitlines(), ""):
        if not line:
            if current:
                stanzas.append(current)
                current = {}
            continue
        if line[0].isspace():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ProvisionFailure("package_metadata_invalid")
        current[key] = value.strip()
    return stanzas


def _package_metadata(
    runner: Runner,
    package: str,
    version: str,
    architecture: str,
    *,
    trusted_indexes: frozenset[str],
) -> PackageRecord:
    output = runner.capture(
        (APT_CACHE, "show", "--no-all-versions", f"{package}:{architecture}={version}"),
        failure_code="package_metadata_unavailable",
        timeout=METADATA_TIMEOUT_SEC,
    )
    matches = [
        stanza
        for stanza in _deb822_stanzas(output)
        if stanza.get("Package") == package
        and stanza.get("Version") == version
        and stanza.get("Architecture") == architecture
    ]
    if len(matches) != 1:
        raise ProvisionFailure("package_metadata_ambiguous")
    fields = matches[0]
    source_output = runner.capture(
        (APT, "show", f"{package}:{architecture}={version}"),
        failure_code="package_source_unavailable",
        timeout=METADATA_TIMEOUT_SEC,
    )
    try:
        source_lines = tuple(
            line.removeprefix("APT-Sources: ").strip()
            for line in source_output.decode("utf-8").splitlines()
            if line.startswith("APT-Sources: ")
        )
    except UnicodeDecodeError:
        raise ProvisionFailure("package_source_invalid") from None
    if (
        not source_lines
        or len(source_lines) > 8
        or any(source not in trusted_indexes for source in source_lines)
    ):
        raise ProvisionFailure("package_source_untrusted")
    sha256 = fields.get("SHA256", "").casefold()
    filename = fields.get("Filename", "")
    try:
        size = int(fields.get("Size", ""))
    except ValueError:
        raise ProvisionFailure("package_metadata_invalid") from None
    filename_path = PurePosixPath(filename)
    if (
        fields.get("Origin") != "Ubuntu"
        or _HEX64.fullmatch(sha256) is None
        or size <= 0
        or size > MAX_DEB_BYTES
        or filename_path.is_absolute()
        or not filename_path.parts
        or filename_path.parts[0] != "pool"
        or any(part in {"", ".", ".."} for part in filename_path.parts)
        or not filename.endswith(".deb")
    ):
        raise ProvisionFailure("package_metadata_untrusted")
    return PackageRecord(
        package=package,
        version=version,
        architecture=architecture,
        filename=filename,
        size=size,
        sha256=sha256,
        apt_source=source_lines[0],
    )


def _trusted_ubuntu_indexes(runner: Runner, *, architecture: str) -> frozenset[str]:
    output = runner.capture(
        (
            APT_GET,
            "indextargets",
            "--format",
            "$(IDENTIFIER)\t$(SITE)\t$(RELEASE)\t$(COMPONENT)\t"
            "$(ARCHITECTURE)\t$(ORIGIN)\t$(LABEL)",
        ),
        failure_code="apt_index_provenance_unavailable",
        timeout=METADATA_TIMEOUT_SEC,
    )
    try:
        lines = output.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise ProvisionFailure("apt_index_provenance_invalid") from None
    trusted: set[str] = set()
    for line in lines:
        parts = line.split("\t")
        if len(parts) != 7:
            raise ProvisionFailure("apt_index_provenance_invalid")
        identifier, site, release, component, item_architecture, origin, label = parts
        if identifier != "Packages" or item_architecture != architecture:
            continue
        if origin != "Ubuntu" or label != "Ubuntu":
            continue
        try:
            parsed_site = urlsplit(site)
            hostname = (parsed_site.hostname or "").casefold()
        except ValueError:
            raise ProvisionFailure("apt_index_provenance_invalid") from None
        official_site = (
            parsed_site.scheme in {"http", "https"}
            and not parsed_site.username
            and not parsed_site.password
            and parsed_site.query == ""
            and parsed_site.fragment == ""
            and (
                (
                    (hostname == "archive.ubuntu.com" or hostname.endswith(".archive.ubuntu.com"))
                    and parsed_site.path.rstrip("/") == "/ubuntu"
                )
                or (
                    hostname == "security.ubuntu.com"
                    and parsed_site.path.rstrip("/") == "/ubuntu"
                )
                or (
                    hostname == "ports.ubuntu.com"
                    and parsed_site.path.rstrip("/") == "/ubuntu-ports"
                )
            )
        )
        if (
            not official_site
            or re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,127}", release) is None
            or re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,63}", component) is None
        ):
            raise ProvisionFailure("apt_index_provenance_invalid")
        trusted.add(f"{site} {release}/{component} {architecture} Packages")
    if not trusted:
        raise ProvisionFailure("apt_index_provenance_unavailable")
    return frozenset(trusted)


def _tesseract_wrapper(multiarch: str) -> bytes:
    library_path = f"$root/rootfs/usr/lib/{multiarch}:$root/rootfs/usr/lib"
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "case $0 in */bin/tesseract) root=${0%/bin/tesseract} ;; *) exit 64 ;; esac\n"
        "[ -d \"$root/rootfs\" ] || exit 64\n"
        f"LD_LIBRARY_PATH=\"{library_path}\"\n"
        "TESSDATA_PREFIX=\"$root/rootfs/usr/share/tesseract-ocr/5/tessdata\"\n"
        "export LD_LIBRARY_PATH TESSDATA_PREFIX\n"
        "exec \"$root/rootfs/usr/bin/tesseract\" \"$@\"\n"
    ).encode("ascii")


def _libreoffice_wrapper(multiarch: str) -> bytes:
    private_root = "/opt/friday-document-toolchain"
    private_libs = (
        f"{private_root}/usr/lib/{multiarch}:"
        f"{private_root}/usr/lib/libreoffice/program:{private_root}/usr/lib"
    )
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "case $0 in */bin/libreoffice) root=${0%/bin/libreoffice} ;; *) exit 64 ;; esac\n"
        "case ${TMPDIR-} in /tmp/friday-office-*) ;; *) exit 64 ;; esac\n"
        "tmp_suffix=${TMPDIR#/tmp/friday-office-}\n"
        "case $tmp_suffix in ''|*/*|*[!A-Za-z0-9._-]*) exit 64 ;; esac\n"
        "[ \"${HOME-}\" = \"$TMPDIR\" ] || exit 64\n"
        "[ -d \"$TMPDIR\" ] || exit 64\n"
        "[ ! -L \"$TMPDIR\" ] || exit 64\n"
        "[ \"$(/usr/bin/readlink -f -- \"$TMPDIR\")\" = \"$TMPDIR\" ] || exit 64\n"
        "[ \"$(/usr/bin/stat -Lc %u -- \"$TMPDIR\")\" = \"$(/usr/bin/id -u)\" ] || exit 64\n"
        "[ \"$(/usr/bin/stat -Lc %a -- \"$TMPDIR\")\" = 700 ] || exit 64\n"
        "[ -d \"$root/rootfs/usr/lib/libreoffice\" ] || exit 64\n"
        f"exec {BWRAP} \\\n"
        "  --unshare-all --die-with-parent --new-session --hostname friday-document \\\n"
        "  --ro-bind /usr /host/usr \\\n"
        f"  --ro-bind \"$root/rootfs\" {private_root} \\\n"
        "  --perms 0755 --dir /usr --perms 0755 --dir /usr/lib \\\n"
        "  --perms 0755 --dir /usr/share \\\n"
        "  --symlink /host/usr/bin /usr/bin --symlink /host/usr/sbin /usr/sbin \\\n"
        f"  --ro-bind /usr/lib/{multiarch} /usr/lib/{multiarch} \\\n"
        "  --ro-bind /usr/lib64 /usr/lib64 \\\n"
        "  --symlink usr/bin /bin --symlink usr/sbin /sbin \\\n"
        "  --symlink usr/lib /lib --symlink usr/lib64 /lib64 \\\n"
        "  --ro-bind-try /usr/share/fonts /usr/share/fonts \\\n"
        "  --ro-bind-try /usr/share/fontconfig /usr/share/fontconfig \\\n"
        "  --ro-bind-try /usr/share/mime /usr/share/mime \\\n"
        "  --perms 0755 --dir /etc \\\n"
        "  --ro-bind-try /etc/fonts /etc/fonts \\\n"
        "  --ro-bind-try /etc/ld.so.cache /etc/ld.so.cache \\\n"
        "  --ro-bind-try /etc/localtime /etc/localtime \\\n"
        "  --ro-bind-try /etc/machine-id /etc/machine-id \\\n"
        "  --ro-bind-try /etc/nsswitch.conf /etc/nsswitch.conf \\\n"
        "  --ro-bind-try /etc/passwd /etc/passwd \\\n"
        "  --ro-bind-try /etc/group /etc/group \\\n"
        "  --perms 0755 --dir /var --perms 0755 --dir /var/cache \\\n"
        "  --ro-bind-try /var/cache/fontconfig /var/cache/fontconfig \\\n"
        "  --ro-bind \"$root/rootfs/usr/lib/libreoffice\" /usr/lib/libreoffice \\\n"
        "  --ro-bind \"$root/rootfs/usr/share/libreoffice\" /usr/share/libreoffice \\\n"
        "  --ro-bind \"$root/rootfs/etc/libreoffice\" /etc/libreoffice \\\n"
        "  --ro-bind-try \"$root/rootfs/usr/share/liblangtag\" /usr/share/liblangtag \\\n"
        "  --ro-bind-try \"$root/rootfs/usr/share/libexttextcat\" /usr/share/libexttextcat \\\n"
        "  --ro-bind-try \"$root/rootfs/usr/share/fonts/truetype/openoffice\" "
        "/usr/share/fonts/truetype/openoffice \\\n"
        "  --proc /proc --dev /dev --perms 0700 --dir /tmp \\\n"
        "  --perms 0700 --tmpfs /run --bind \"$TMPDIR\" \"$TMPDIR\" \\\n"
        "  --chdir \"$TMPDIR\" --clearenv \\\n"
        "  --setenv HOME \"$TMPDIR\" --setenv TMPDIR \"$TMPDIR\" \\\n"
        "  --setenv XDG_CACHE_HOME \"$TMPDIR/cache\" \\\n"
        "  --setenv XDG_CONFIG_HOME \"$TMPDIR/config\" \\\n"
        "  --setenv LANG C.UTF-8 --setenv LC_ALL C.UTF-8 \\\n"
        "  --setenv SAL_USE_VCLPLUGIN svp \\\n"
        f"  --setenv LD_LIBRARY_PATH {private_libs} \\\n"
        "  -- /usr/lib/libreoffice/program/soffice \"$@\"\n"
    ).encode("ascii")


def _make_plan(host: HostIdentity, packages: Sequence[PackageRecord]) -> ToolchainPlan:
    wrappers = {
        "libreoffice": _sha256_bytes(_libreoffice_wrapper(host.multiarch)),
        "tesseract": _sha256_bytes(_tesseract_wrapper(host.multiarch)),
    }
    core: dict[str, object] = {
        "schema": MANIFEST_SCHEMA,
        "host": asdict(host),
        "policy": {
            "allow_unauthenticated": False,
            "install_recommends": False,
            "rootless": True,
            "source_origin": "Ubuntu",
        },
        "root_packages": list(ROOT_PACKAGES),
        "packages": [asdict(package) for package in packages],
        "wrappers_sha256": wrappers,
        "runtime_contract": {
            "commands": list(COMMAND_NAMES),
            "languages": ["rus", "eng"],
            "legacy_formats": ["doc", "xls", "xlsb", "ppt"],
            "activation": "user-local-symlinks",
        },
    }
    plan_sha256 = _sha256_bytes(_canonical_json(core))
    safe_version = host.version_id.replace(".", "-")
    toolchain_id = f"ubuntu-{safe_version}-{host.architecture}-{plan_sha256[:20]}"
    manifest = {**core, "plan_sha256": plan_sha256, "toolchain_id": toolchain_id}
    return ToolchainPlan(
        manifest=manifest,
        packages=tuple(packages),
        plan_sha256=plan_sha256,
        toolchain_id=toolchain_id,
    )


def build_plan(
    runner: Runner,
    *,
    host: HostIdentity | None = None,
    os_release: Path = Path("/etc/os-release"),
    dpkg_status: Path = Path("/var/lib/dpkg/status"),
) -> ToolchainPlan:
    selected_host = host or discover_host(
        runner,
        os_release=os_release,
        dpkg_status=dpkg_status,
    )
    output = runner.capture(
        (
            APT_GET,
            "--simulate",
            "--reinstall",
            "--no-install-recommends",
            "--no-upgrade",
            "-o",
            "APT::Get::AllowUnauthenticated=false",
            "-o",
            "Acquire::AllowInsecureRepositories=false",
            "install",
            *ROOT_PACKAGES,
        ),
        failure_code="apt_solver_failed",
        timeout=RESOLVE_TIMEOUT_SEC,
    )
    closure = _parse_install_set(output, host_architecture=selected_host.architecture)
    trusted_indexes = _trusted_ubuntu_indexes(runner, architecture=selected_host.architecture)
    packages = tuple(
        _package_metadata(
            runner,
            package,
            version,
            architecture,
            trusted_indexes=trusted_indexes,
        )
        for package, version, architecture in closure
    )
    if sum(package.size for package in packages) > MAX_TOTAL_DEB_BYTES:
        raise ProvisionFailure("apt_solver_closure_too_large")
    return _make_plan(selected_host, packages)


def _trusted_program(path: str, *, failure_code: str) -> None:
    candidate = Path(path)
    try:
        details = candidate.stat()
    except OSError:
        raise ProvisionFailure(failure_code) from None
    if (
        not candidate.is_absolute()
        or not stat.S_ISREG(details.st_mode)
        or not os.access(candidate, os.X_OK)
        or details.st_uid != 0
        or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ProvisionFailure(failure_code)


def _installation_preflight(runner: Runner) -> None:
    if os.geteuid() == 0:
        raise ProvisionFailure("root_execution_forbidden")
    for path, code in (
        (APT_GET, "apt_get_untrusted"),
        (APT_CACHE, "apt_cache_untrusted"),
        (APT, "apt_untrusted"),
        (DPKG_DEB, "dpkg_deb_untrusted"),
        (BWRAP, "bubblewrap_untrusted"),
    ):
        _trusted_program(path, failure_code=code)
    runner.quiet(
        (
            BWRAP,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--ro-bind",
            "/usr",
            "/usr",
            "--symlink",
            "usr/bin",
            "/bin",
            "--symlink",
            "usr/lib",
            "/lib",
            "--symlink",
            "usr/lib64",
            "/lib64",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--",
            "/usr/bin/true",
        ),
        failure_code="bubblewrap_unavailable",
        timeout=METADATA_TIMEOUT_SEC,
    )


def _private_base(path: Path, *, uid: int) -> Path:
    lexical = Path(os.path.abspath(path))
    if not lexical.is_absolute():  # pragma: no cover - abspath invariant
        raise ProvisionFailure("toolchain_base_invalid")
    try:
        lexical.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = lexical.resolve(strict=True)
        details = os.lstat(lexical)
    except OSError:
        raise ProvisionFailure("toolchain_base_invalid") from None
    if (
        resolved != lexical
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != uid
        or details.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise ProvisionFailure("toolchain_base_untrusted")
    return lexical


def _activation_directory(path: Path, *, uid: int) -> Path:
    lexical = Path(os.path.abspath(path))
    try:
        lexical.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = lexical.resolve(strict=True)
        details = os.lstat(lexical)
    except OSError:
        raise ProvisionFailure("activation_directory_invalid") from None
    if (
        resolved != lexical
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != uid
        or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ProvisionFailure("activation_directory_untrusted")
    return lexical


def _write_exclusive(path: Path, value: bytes, *, mode: int) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError:
        raise ProvisionFailure("toolchain_write_failed") from None
    try:
        view = memoryview(value)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise ProvisionFailure("toolchain_write_failed")
            written += count
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except OSError:
        raise ProvisionFailure("toolchain_write_failed") from None
    finally:
        os.close(descriptor)


def _deb_identity(path: Path, runner: Runner) -> tuple[str, str, str]:
    output = runner.capture(
        (
            DPKG_DEB,
            "--show",
            "--showformat=${Package}\\t${Version}\\t${Architecture}\\n",
            str(path),
        ),
        failure_code="package_identity_unavailable",
        timeout=METADATA_TIMEOUT_SEC,
        max_output_bytes=4 << 10,
    )
    try:
        line = output.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ProvisionFailure("package_identity_invalid") from None
    parts = line.split("\t")
    if len(parts) != 3:
        raise ProvisionFailure("package_identity_invalid")
    package, version, architecture = parts
    if (
        _PACKAGE.fullmatch(package) is None
        or _VERSION.fullmatch(version) is None
        or _ARCHITECTURE.fullmatch(architecture) is None
    ):
        raise ProvisionFailure("package_identity_invalid")
    return package, version, architecture


def _download_and_verify(plan: ToolchainPlan, downloads: Path, runner: Runner) -> dict[str, Path]:
    downloads.mkdir(mode=0o700)
    runner.quiet(
        (
            APT_GET,
            "-o",
            "APT::Get::AllowUnauthenticated=false",
            "-o",
            "Acquire::AllowInsecureRepositories=false",
            "-o",
            "Acquire::AllowDowngradeToInsecureRepositories=false",
            "download",
            *(package.apt_spec for package in plan.packages),
        ),
        failure_code="package_download_failed",
        timeout=DOWNLOAD_TIMEOUT_SEC,
        cwd=downloads,
    )
    expected = {
        (package.package, package.version, package.architecture): package for package in plan.packages
    }
    found: dict[tuple[str, str, str], Path] = {}
    try:
        entries = tuple(downloads.iterdir())
    except OSError:
        raise ProvisionFailure("package_download_set_invalid") from None
    if len(entries) != len(expected):
        raise ProvisionFailure("package_download_set_invalid")
    for path in entries:
        try:
            details = os.lstat(path)
        except OSError:
            raise ProvisionFailure("package_file_invalid") from None
        if (
            path.suffix != ".deb"
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
        ):
            raise ProvisionFailure("package_file_invalid")
        identity = _deb_identity(path, runner)
        package = expected.get(identity)
        if package is None or identity in found:
            raise ProvisionFailure("package_download_set_invalid")
        if details.st_size != package.size or _sha256_file(path) != package.sha256:
            raise ProvisionFailure("package_digest_mismatch")
        found[identity] = path
    if found.keys() != expected.keys():
        raise ProvisionFailure("package_download_set_invalid")
    return {identity[0] + ":" + identity[2]: path for identity, path in found.items()}


def _extract_packages(
    plan: ToolchainPlan,
    package_paths: Mapping[str, Path],
    rootfs: Path,
    runner: Runner,
) -> None:
    rootfs.mkdir(mode=0o700)
    for package in plan.packages:
        path = package_paths.get(f"{package.package}:{package.architecture}")
        if path is None:
            raise ProvisionFailure("package_download_set_invalid")
        runner.quiet(
            (DPKG_DEB, "--extract", str(path), str(rootfs)),
            failure_code="package_extraction_failed",
            timeout=EXTRACT_TIMEOUT_SEC,
        )


def _path_inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _verify_payload(rootfs: Path) -> None:
    required_files = (
        rootfs / "usr/bin/tesseract",
        rootfs / "usr/lib/libreoffice/program/soffice",
        rootfs / "usr/share/tesseract-ocr/5/tessdata/rus.traineddata",
        rootfs / "usr/share/tesseract-ocr/5/tessdata/eng.traineddata",
    )
    required_directories = (
        rootfs / "usr/lib/libreoffice",
        rootfs / "usr/share/libreoffice",
        rootfs / "etc/libreoffice",
    )
    try:
        resolved_root = rootfs.resolve(strict=True)
        for path in required_files:
            resolved = path.resolve(strict=True)
            details = resolved.stat()
            if (
                not _path_inside(resolved_root, resolved)
                or not stat.S_ISREG(details.st_mode)
                or details.st_size <= 0
            ):
                raise ProvisionFailure("toolchain_payload_incomplete")
        for path in required_directories:
            resolved = path.resolve(strict=True)
            if not _path_inside(resolved_root, resolved) or not resolved.is_dir():
                raise ProvisionFailure("toolchain_payload_incomplete")
    except OSError:
        raise ProvisionFailure("toolchain_payload_incomplete") from None


def _symlink_target_allowed(root: Path, path: Path) -> bool:
    try:
        target = os.readlink(path)
    except OSError:
        return False
    if not target or len(os.fsencode(target)) > 4_096 or "\x00" in target:
        return False
    pure_target = PurePosixPath(target)
    if pure_target.is_absolute():
        normalized = PurePosixPath("/", *pure_target.parts[1:])
        if ".." in normalized.parts:
            return False
        return any(
            normalized == prefix or prefix in normalized.parents
            for prefix in (
                PurePosixPath("/bin"),
                PurePosixPath("/lib"),
                PurePosixPath("/lib64"),
                PurePosixPath("/sbin"),
                PurePosixPath("/usr"),
                PurePosixPath("/etc/libreoffice"),
            )
        )
    lexical_root = Path(os.path.abspath(root))
    lexical_target = Path(os.path.abspath(path.parent / target))
    return _path_inside(lexical_root, lexical_target)


def _freeze_tree(root: Path, *, uid: int) -> None:
    try:
        paths = sorted(root.rglob("*"), key=lambda candidate: len(candidate.parts), reverse=True)
    except OSError:
        raise ProvisionFailure("toolchain_seal_failed") from None
    for path in paths:
        try:
            details = os.lstat(path)
        except OSError:
            raise ProvisionFailure("toolchain_seal_failed") from None
        if details.st_uid != uid:
            raise ProvisionFailure("toolchain_owner_mismatch")
        if stat.S_ISLNK(details.st_mode):
            if not _symlink_target_allowed(root, path):
                raise ProvisionFailure("toolchain_symlink_invalid")
            continue
        if stat.S_ISDIR(details.st_mode):
            mode = 0o500
        elif stat.S_ISREG(details.st_mode):
            mode = 0o500 if details.st_mode & 0o111 else 0o400
        else:
            raise ProvisionFailure("toolchain_payload_type_invalid")
        try:
            os.chmod(path, mode, follow_symlinks=False)
        except OSError:
            raise ProvisionFailure("toolchain_seal_failed") from None
    try:
        os.chmod(root, 0o500)
    except OSError:
        raise ProvisionFailure("toolchain_seal_failed") from None


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        raise ProvisionFailure("toolchain_sync_failed") from None


def _manifest_matches(path: Path, plan: ToolchainPlan) -> bool:
    try:
        details = os.lstat(path)
        value = path.read_bytes()
    except OSError:
        return False
    return (
        stat.S_ISREG(details.st_mode)
        and details.st_uid == os.geteuid()
        and details.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
        and value == plan.manifest_bytes
    )


def _verify_existing_install(final: Path, plan: ToolchainPlan, *, uid: int) -> None:
    try:
        details = os.lstat(final)
    except OSError:
        raise ProvisionFailure("existing_toolchain_invalid") from None
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != uid
        or details.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        or not _manifest_matches(final / "manifest.json", plan)
    ):
        raise ProvisionFailure("existing_toolchain_invalid")
    expected = {
        "tesseract": _tesseract_wrapper(str(plan.manifest["host"]["multiarch"])),  # type: ignore[index]
        "libreoffice": _libreoffice_wrapper(str(plan.manifest["host"]["multiarch"])),  # type: ignore[index]
    }
    for name, value in expected.items():
        wrapper = final / "bin" / name
        try:
            wrapper_details = os.lstat(wrapper)
        except OSError:
            raise ProvisionFailure("existing_toolchain_invalid") from None
        if (
            not stat.S_ISREG(wrapper_details.st_mode)
            or wrapper_details.st_uid != uid
            or wrapper_details.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            or not os.access(wrapper, os.X_OK)
            or _sha256_file(wrapper) != _sha256_bytes(value)
        ):
            raise ProvisionFailure("existing_toolchain_invalid")


def _safe_remove_stage(stage: Path, base: Path, *, uid: int) -> None:
    try:
        details = os.lstat(stage)
    except FileNotFoundError:
        return
    except OSError:
        return
    if (
        stage.parent == base
        and stage.name.startswith(".staging-")
        and stat.S_ISDIR(details.st_mode)
        and details.st_uid == uid
    ):
        try:
            os.chmod(stage, 0o700)
            for path in stage.rglob("*"):
                if not path.is_symlink():
                    os.chmod(path, 0o700 if path.is_dir() else 0o600)
            shutil.rmtree(stage)
        except OSError:
            pass


def _probe_wrappers(stage: Path, runner: Runner) -> None:
    runner.capture(
        (str(stage / "bin/tesseract"), "--version"),
        failure_code="tesseract_canary_failed",
        timeout=METADATA_TIMEOUT_SEC,
        max_output_bytes=64 << 10,
    )
    listed_output = runner.capture(
        (str(stage / "bin/tesseract"), "--list-langs"),
        failure_code="tesseract_canary_failed",
        timeout=METADATA_TIMEOUT_SEC,
        max_output_bytes=64 << 10,
    )
    try:
        listed_languages = {line.strip() for line in listed_output.decode("ascii").splitlines()}
    except UnicodeDecodeError:
        raise ProvisionFailure("tesseract_canary_failed") from None
    if not {"rus", "eng"}.issubset(listed_languages):
        raise ProvisionFailure("tesseract_canary_failed")
    try:
        with tempfile.TemporaryDirectory(
            prefix="friday-office-",
            dir="/tmp",  # nosec B108
        ) as temporary:
            work = Path(temporary)
            work.chmod(0o700)
            runner.quiet(
                (str(stage / "bin/libreoffice"), "--headless", "--version"),
                failure_code="libreoffice_canary_failed",
                timeout=METADATA_TIMEOUT_SEC,
                environment={"HOME": str(work), "TMPDIR": str(work)},
            )
    except OSError:
        raise ProvisionFailure("libreoffice_canary_failed") from None


def _build_install(
    plan: ToolchainPlan,
    *,
    base: Path,
    runner: Runner,
    uid: int,
    perform_canary: bool,
) -> Path:
    final = base / plan.toolchain_id
    if final.exists():
        _verify_existing_install(final, plan, uid=uid)
        return final
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=base))
    try:
        os.chmod(stage, 0o700)
        package_paths = _download_and_verify(plan, stage / "downloads", runner)
        rootfs = stage / "rootfs"
        _extract_packages(plan, package_paths, rootfs, runner)
        _verify_payload(rootfs)
        bin_dir = stage / "bin"
        bin_dir.mkdir(mode=0o700)
        multiarch = str(plan.manifest["host"]["multiarch"])  # type: ignore[index]
        _write_exclusive(bin_dir / "tesseract", _tesseract_wrapper(multiarch), mode=0o500)
        _write_exclusive(bin_dir / "libreoffice", _libreoffice_wrapper(multiarch), mode=0o500)
        _write_exclusive(stage / "manifest.json", plan.manifest_bytes, mode=0o400)
        if perform_canary:
            _probe_wrappers(stage, runner)
        shutil.rmtree(stage / "downloads")
        _freeze_tree(stage, uid=uid)
        try:
            os.rename(stage, final)
        except FileExistsError:
            raise ProvisionFailure("toolchain_publish_conflict") from None
        except OSError:
            raise ProvisionFailure("toolchain_publish_failed") from None
        _fsync_directory(base)
        _verify_existing_install(final, plan, uid=uid)
        return final
    finally:
        _safe_remove_stage(stage, base, uid=uid)


def _trusted_old_link(link: Path, *, base: Path, uid: int) -> str | None:
    try:
        details = os.lstat(link)
    except FileNotFoundError:
        return None
    except OSError:
        raise ProvisionFailure("activation_link_invalid") from None
    if not stat.S_ISLNK(details.st_mode) or details.st_uid != uid:
        raise ProvisionFailure("activation_link_untrusted")
    try:
        raw_target = os.readlink(link)
        resolved = link.resolve(strict=True)
        resolved_base = base.resolve(strict=True)
    except OSError:
        raise ProvisionFailure("activation_link_untrusted") from None
    if (
        not _path_inside(resolved_base, resolved)
        or resolved.name != link.name
        or resolved.parent.name != "bin"
    ):
        raise ProvisionFailure("activation_link_untrusted")
    return raw_target


def _write_atomic_symlink(link: Path, target: Path) -> None:
    temporary = link.parent / f".friday-{link.name}-{secrets.token_hex(8)}.new"
    try:
        os.symlink(str(target), temporary)
        os.replace(temporary, link)
    except OSError:
        raise ProvisionFailure("activation_link_replace_failed") from None
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _restore_link(link: Path, old_target: str | None) -> None:
    temporary = link.parent / f".friday-{link.name}-{secrets.token_hex(8)}.rollback"
    try:
        if old_target is None:
            link.unlink(missing_ok=True)
        else:
            os.symlink(old_target, temporary)
            os.replace(temporary, link)
    except OSError:
        raise ProvisionFailure("activation_rollback_failed") from None
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _activate(final: Path, *, base: Path, activation_dir: Path, uid: int) -> None:
    directory = _activation_directory(activation_dir, uid=uid)
    targets = {name: final / "bin" / name for name in COMMAND_NAMES}
    previous = {
        name: _trusted_old_link(directory / name, base=base, uid=uid) for name in COMMAND_NAMES
    }
    for target in targets.values():
        try:
            details = os.lstat(target)
        except OSError:
            raise ProvisionFailure("activation_target_invalid") from None
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != uid
            or not os.access(target, os.X_OK)
            or details.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise ProvisionFailure("activation_target_invalid")
    replaced: list[str] = []
    try:
        for name in COMMAND_NAMES:
            link = directory / name
            if link.is_symlink() and link.resolve(strict=True) == targets[name].resolve(strict=True):
                continue
            _write_atomic_symlink(link, targets[name])
            replaced.append(name)
        _fsync_directory(directory)
        for name, target in targets.items():
            link = directory / name
            try:
                details = os.lstat(link)
                resolved = link.resolve(strict=True)
            except OSError:
                raise ProvisionFailure("activation_verification_failed") from None
            if (
                not stat.S_ISLNK(details.st_mode)
                or details.st_uid != uid
                or resolved != target.resolve()
            ):
                raise ProvisionFailure("activation_verification_failed")
    except (OSError, ProvisionFailure):
        for name in reversed(replaced):
            _restore_link(directory / name, previous[name])
        _fsync_directory(directory)
        raise ProvisionFailure("activation_failed") from None


def _prevalidate_activation(*, base: Path, activation_dir: Path, uid: int) -> None:
    """Reject command collisions before any package download or publication."""

    directory = _activation_directory(activation_dir, uid=uid)
    for name in COMMAND_NAMES:
        _trusted_old_link(directory / name, base=base, uid=uid)


def _validate_plan_integrity(plan: ToolchainPlan) -> None:
    host_payload = plan.manifest.get("host")
    if not isinstance(host_payload, Mapping):
        raise ProvisionFailure("installation_plan_invalid")
    try:
        host = HostIdentity(
            os_id=str(host_payload["os_id"]),
            version_id=str(host_payload["version_id"]),
            architecture=str(host_payload["architecture"]),
            multiarch=str(host_payload["multiarch"]),
            dpkg_status_sha256=str(host_payload["dpkg_status_sha256"]),
        )
    except KeyError:
        raise ProvisionFailure("installation_plan_invalid") from None
    if (
        host.os_id != "ubuntu"
        or re.fullmatch(r"[0-9]{2}\.[0-9]{2}", host.version_id) is None
        or _ARCHITECTURE.fullmatch(host.architecture) is None
        or _MULTIARCH.fullmatch(host.multiarch) is None
        or _HEX64.fullmatch(host.dpkg_status_sha256) is None
        or len(plan.packages) > MAX_PACKAGES
        or len({(package.package, package.architecture) for package in plan.packages})
        != len(plan.packages)
        or sum(package.size for package in plan.packages) > MAX_TOTAL_DEB_BYTES
        or not set(ROOT_PACKAGES).issubset({package.package for package in plan.packages})
    ):
        raise ProvisionFailure("installation_plan_invalid")
    for package in plan.packages:
        if (
            package.origin != "Ubuntu"
            or _PACKAGE.fullmatch(package.package) is None
            or _VERSION.fullmatch(package.version) is None
            or package.architecture not in {"all", host.architecture}
            or _HEX64.fullmatch(package.sha256) is None
            or package.size <= 0
            or package.size > MAX_DEB_BYTES
            or re.fullmatch(r"https?://\S+ \S+/\S+ \S+ Packages", package.apt_source) is None
        ):
            raise ProvisionFailure("installation_plan_invalid")
    expected = _make_plan(host, plan.packages)
    if (
        expected.toolchain_id != plan.toolchain_id
        or expected.plan_sha256 != plan.plan_sha256
        or expected.manifest_bytes != plan.manifest_bytes
    ):
        raise ProvisionFailure("installation_plan_invalid")


def _require_unchanged_host_state(
    plan: ToolchainPlan,
    *,
    dpkg_status: Path = Path("/var/lib/dpkg/status"),
) -> None:
    host_payload = plan.manifest.get("host")
    if not isinstance(host_payload, Mapping):  # pragma: no cover - integrity check owns this
        raise ProvisionFailure("installation_plan_invalid")
    expected = str(host_payload.get("dpkg_status_sha256", ""))
    current = _sha256_limited_file(
        dpkg_status,
        limit=MAX_DPKG_STATUS_BYTES,
        failure_code="dpkg_status_invalid",
    )
    if current != expected:
        raise ProvisionFailure("host_package_state_changed")


def install_plan(
    plan: ToolchainPlan,
    *,
    confirmation: str,
    runner: Runner,
    base_dir: Path,
    activation_dir: Path,
    perform_preflight: bool = True,
    perform_canary: bool = True,
    verify_host_state: bool = True,
) -> Mapping[str, object]:
    _validate_plan_integrity(plan)
    if confirmation != plan.toolchain_id:
        raise ProvisionFailure("installation_confirmation_mismatch")
    if perform_preflight:
        _installation_preflight(runner)
    if verify_host_state:
        _require_unchanged_host_state(plan)
    uid = os.geteuid()
    base = _private_base(base_dir, uid=uid)
    activation = _activation_directory(activation_dir, uid=uid)
    lock_path = base / ".install.lock"
    try:
        lock_descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except OSError:
        raise ProvisionFailure("installation_lock_unavailable") from None
    try:
        lock_details = os.fstat(lock_descriptor)
        if (
            not stat.S_ISREG(lock_details.st_mode)
            or lock_details.st_uid != uid
            or lock_details.st_nlink != 1
            or lock_details.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            raise ProvisionFailure("installation_lock_untrusted")
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        except OSError:
            raise ProvisionFailure("installation_lock_unavailable") from None
        _prevalidate_activation(base=base, activation_dir=activation, uid=uid)
        final = _build_install(
            plan,
            base=base,
            runner=runner,
            uid=uid,
            perform_canary=perform_canary,
        )
        if verify_host_state:
            _require_unchanged_host_state(plan)
        _activate(final, base=base, activation_dir=activation, uid=uid)
    finally:
        os.close(lock_descriptor)
    return {
        "schema": AUDIT_SCHEMA,
        "action": "install",
        "status": "installed",
        "toolchain_id": plan.toolchain_id,
        "plan_sha256": plan.plan_sha256,
        "package_count": len(plan.packages),
        "download_bytes": sum(package.size for package in plan.packages),
        "activated_commands": list(COMMAND_NAMES),
    }


def audit_receipt(plan: ToolchainPlan) -> Mapping[str, object]:
    return {
        "schema": AUDIT_SCHEMA,
        "action": "audit",
        "status": "planned",
        "install_requires_confirmation": True,
        "toolchain_id": plan.toolchain_id,
        "plan_sha256": plan.plan_sha256,
        "package_count": len(plan.packages),
        "download_bytes": sum(package.size for package in plan.packages),
        "manifest": plan.manifest,
    }


def _write_receipt(receipt: Mapping[str, object], *, stream: TextIO) -> None:
    encoded = _canonical_json(receipt).decode("ascii")
    print(encoded, file=stream)


def _default_paths() -> tuple[Path, Path]:
    try:
        home = Path(pwd.getpwuid(os.geteuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError):
        raise ProvisionFailure("owner_home_unavailable") from None
    return home / ".jericho/toolchains", home / ".local/bin"


class _ClosedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ProvisionFailure("arguments_invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _ClosedArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="audit", choices=("audit", "install"))
    parser.add_argument("--confirm", default="", help="exact toolchain id printed by audit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    runner = SubprocessRunner()
    try:
        arguments = _parser().parse_args(argv)
        plan = build_plan(runner)
        if arguments.action == "audit":
            if arguments.confirm:
                raise ProvisionFailure("confirmation_not_valid_for_audit")
            receipt = audit_receipt(plan)
        else:
            base_dir, activation_dir = _default_paths()
            receipt = install_plan(
                plan,
                confirmation=arguments.confirm,
                runner=runner,
                base_dir=base_dir,
                activation_dir=activation_dir,
            )
    except ProvisionFailure as exc:
        _write_receipt(
            {
                "schema": AUDIT_SCHEMA,
                "status": "failed",
                "failure_codes": [exc.code],
            },
            stream=sys.stderr,
        )
        return 1
    except Exception:  # noqa: BLE001 - public output must stay closed on every failure
        _write_receipt(
            {
                "schema": AUDIT_SCHEMA,
                "status": "failed",
                "failure_codes": ["unexpected_failure"],
            },
            stream=sys.stderr,
        )
        return 1
    _write_receipt(receipt, stream=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
