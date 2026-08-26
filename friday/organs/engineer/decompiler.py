"""Pinned, observe-only Ghidra Headless adapter for native artifacts.

The submitted program is imported as data into Ghidra; it is never executed.
The adapter is reachable only from the networkless Engineer bubblewrap worker.
Both the host paths which are mounted and the paths visible inside the sandbox
are verified against a small pinned identity and a recursively safe tree before
the vendor launcher is entered.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, TypedDict

GHIDRA_VERSION = "12.1.3"
JDK_VERSION = "21.0.12.1+1"
GHIDRA_ARCHIVE_SHA256 = "93a5d11a9ad510622acaaf908c556a7b9b764d338e78a7567f3689bf5081fd54"
JDK_ARCHIVE_SHA256 = "ce79869e1307ed8ee1e2baa86a412b1eb5b75d10a01006d788a6f968bcfaee94"
GHIDRA_TREE_SHA256 = "7e40cc12fd330b50478fa3d199a5399a313cc247d4226f7ce674f409727c0200"
JDK_TREE_SHA256 = "662d527334f464cb798d04518f2b1c9fbeea75d8a8230a1127fbc5deff8142a4"
TOOLS_ROOT = Path("/home/jericho/.jericho/tools")
GHIDRA_ROOT = TOOLS_ROOT / "ghidra-12.1.3"
JDK_ROOT = TOOLS_ROOT / "jdk-21.0.12.1+1"
SANDBOX_GHIDRA_ROOT = Path("/opt/friday-ghidra")
SANDBOX_JDK_ROOT = Path("/opt/friday-jdk")

SCHEMA = "friday.engineer.decompile.v1"
TOOL_NAME = "ghidra-headless"
SUPPORTED_KINDS = frozenset({"pe", "elf"})
_CLASSIFIED_KINDS = frozenset(
    {"pe", "elf", "dos", "macho", "macho_fat", "dex", "apk", "jar", "zip", "unknown"}
)
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_HEADLESS_JSON_BYTES = 512 * 1024
MAX_FUNCTIONS = 32
MAX_SCANNED_FUNCTIONS = 4096
MAX_FUNCTION_NAME_CHARS = 256
MAX_SIGNATURE_CHARS = 1024
MAX_PSEUDOCODE_CHARS_PER_FUNCTION = 6_000
MAX_TOTAL_PSEUDOCODE_CHARS = 160_000
HEADLESS_TIMEOUT_SECONDS = 225.0
ANALYSIS_TIMEOUT_SECONDS = 150
MAX_TREE_ENTRIES = 16_000
MAX_GHIDRA_TREE_BYTES = 2 * 1024 * 1024 * 1024
MAX_JDK_TREE_BYTES = 1024 * 1024 * 1024

# These hashes are from the two reviewed upstream archives.  Checking the
# executable launch chain and release identities, then recursively requiring a
# single-owner, non-writable tree, avoids trusting PATH discovery or a mutable
# system Java installation.  The checks are repeated after the read-only bind.
_GHIDRA_FILES = {
    "bom.json": "8068d86dbf015df33828b210d74bd350d008dfe91397d0be348b21fb588986bc",
    "Ghidra/application.properties": ("fb9b6292c801e4a18b7c20829437d3b18b2241b273aea9b1cbdebdf3e5d0dc15"),
    "Ghidra/Framework/Utility/lib/Utility.jar": (
        "6f5c4ae8de0ef8f93f796a1b66f1b9dba047b423c54d3d0d845b486d8d08323d"
    ),
    "support/analyzeHeadless": ("302880328a0024ee24cfe0326d4d9a61c2237116d95f2e0e0df090f747f95e30"),
    "support/launch.sh": "32be94ba54ee447c2a71d0191398de4fa7998817f078c315f996350d06df31b5",
    "support/LaunchSupport.jar": ("39132175862e018e6fd250195f4cb4119bc522e84f29748b48216fa81c5dd9a5"),
    "support/launch.properties": ("0004073d11c448edd8e5a766fada962720b4e15fd5c1826aed4e29ea1ff1e4e9"),
}
_JDK_FILES = {
    "release": "ebbd70bf00bab6d39addf51969c5ff244030b86b29de400b9fb10612ebd3ceec",
    "bin/java": "2a207f5e7d075afa01d97f8048389a64432a44c4a5af0f5e77d6e286ec5f401d",
    "lib/libjli.so": "6703dbc0a7d5953ba248f45afca71b13807e5be9682110ed205f4361640f496a",
    "lib/modules": "70c5465dabf8e1bec2860631ea3ffd342f0ec23a5e2efd3a389d2abe32448f98",
}
_TOOLCHAIN_FAILURES = frozenset(
    {
        "toolchain_missing",
        "toolchain_incomplete",
        "toolchain_untrusted",
    }
)


class DecompileFunction(TypedDict):
    address: str
    name: str
    signature: str
    pseudocode: str
    decompile_status: Literal["completed", "failed", "timeout"]
    pseudocode_truncated: bool
    thunk: bool


class DecompileReport(TypedDict, total=False):
    ok: bool
    status: Literal["completed", "partial", "unsupported", "unavailable", "failed"]
    error: str
    schema: str
    tool_name: str
    tool_version: str
    jdk_version: str
    format: str
    language_id: str
    compiler_spec_id: str
    analysis_timed_out: bool
    function_count_lower_bound: int
    function_index_truncated: bool
    pseudocode_chars: int
    output_truncated: bool
    functions: list[DecompileFunction]
    warnings: list[str]
    observe_only: bool
    sample_executed: bool
    network: Literal["none"]


def _failure(kind: str, status: str, code: str) -> DecompileReport:
    # Every failure is deliberately selected from code-owned literals.  In
    # particular, Ghidra stderr, Java exception text and artifact paths never
    # cross the worker boundary.
    return {
        "ok": False,
        "status": status,  # type: ignore[typeddict-item]
        "error": code,
        "schema": SCHEMA,
        "tool_name": TOOL_NAME,
        "tool_version": GHIDRA_VERSION,
        "jdk_version": JDK_VERSION,
        "format": kind if kind in _CLASSIFIED_KINDS else "unknown",
        "functions": [],
        "warnings": [],
        "observe_only": True,
        "sample_executed": False,
        "network": "none",
    }


def _sha256_regular(path: Path, expected: os.stat_result | None = None) -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size < 0:
            return None
        if expected is not None and (
            details.st_dev,
            details.st_ino,
            details.st_mode,
            details.st_size,
            details.st_nlink,
        ) != (
            expected.st_dev,
            expected.st_ino,
            expected.st_mode,
            expected.st_size,
            expected.st_nlink,
        ):
            return None
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _safe_owner(details: os.stat_result) -> bool:
    return details.st_uid in {0, os.geteuid()}


def _safe_ancestors(path: Path) -> bool:
    current = path
    while True:
        try:
            details = current.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            return False
        if not _safe_owner(details) or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False
        if current.parent == current:
            return True
        current = current.parent


def _digest_field(payload: bytes) -> bytes:
    return len(payload).to_bytes(8, "big") + payload


def _tree_identity(
    root: Path,
    *,
    maximum_bytes: int,
    maximum_entries: int = MAX_TREE_ENTRIES,
) -> str | None:
    """Hash every admitted tree entry, including modes and symlink targets."""

    try:
        if root.resolve(strict=True) != root or not _safe_ancestors(root):
            return None
        resolved_root = root.resolve(strict=True)
        root_details = root.lstat()
    except OSError:
        return None

    entries = 0
    total_bytes = 0
    pending = [root]
    inventory: list[tuple[bytes, Path, os.stat_result]] = []
    while pending:
        directory = pending.pop()
        try:
            children = list(os.scandir(directory))
        except OSError:
            return None
        for child in children:
            entries += 1
            if entries > maximum_entries:
                return None
            path = Path(child.path)
            try:
                details = child.stat(follow_symlinks=False)
            except OSError:
                return None
            if not _safe_owner(details):
                return None
            relative = os.fsencode(path.relative_to(root).as_posix())
            inventory.append((relative, path, details))
            if stat.S_ISLNK(details.st_mode):
                try:
                    target = path.resolve(strict=True)
                    target.relative_to(resolved_root)
                except (OSError, ValueError, RuntimeError):
                    return None
                continue
            if details.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                return None
            if stat.S_ISDIR(details.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(details.st_mode) or details.st_size < 0 or details.st_nlink != 1:
                return None
            total_bytes += details.st_size
            if total_bytes > maximum_bytes:
                return None

    identity = hashlib.sha256()
    identity.update(b"D" + _digest_field(b".") + stat.S_IMODE(root_details.st_mode).to_bytes(4, "big"))
    for relative, path, details in sorted(inventory, key=lambda item: item[0]):
        mode = stat.S_IMODE(details.st_mode).to_bytes(4, "big")
        if stat.S_ISDIR(details.st_mode):
            identity.update(b"D" + _digest_field(relative) + mode)
            continue
        if stat.S_ISLNK(details.st_mode):
            try:
                link_target = os.fsencode(os.readlink(path))
                current = path.lstat()
            except OSError:
                return None
            if (current.st_dev, current.st_ino, current.st_mode) != (
                details.st_dev,
                details.st_ino,
                details.st_mode,
            ):
                return None
            identity.update(b"L" + _digest_field(relative) + mode + _digest_field(link_target))
            continue
        file_sha256 = _sha256_regular(path, details)
        if file_sha256 is None:
            return None
        identity.update(
            b"F"
            + _digest_field(relative)
            + mode
            + details.st_size.to_bytes(8, "big")
            + bytes.fromhex(file_sha256)
        )
    return identity.hexdigest()


def _fixed_files_match(root: Path, expected: Mapping[str, str]) -> bool:
    for relative, expected_sha256 in expected.items():
        path = root / relative
        if _sha256_regular(path) != expected_sha256:
            return False
    return True


def verify_toolchain(ghidra_root: Path, jdk_root: Path) -> dict[str, Any]:
    """Verify an exact mounted toolchain without returning filesystem content."""

    ghidra = Path(ghidra_root)
    jdk = Path(jdk_root)
    if not ghidra.exists() or not jdk.exists():
        return {"ok": False, "reason": "toolchain_missing"}
    if not ghidra.is_dir() or not jdk.is_dir():
        return {"ok": False, "reason": "toolchain_untrusted"}
    required = tuple(ghidra / relative for relative in _GHIDRA_FILES) + tuple(
        jdk / relative for relative in _JDK_FILES
    )
    if any(not path.exists() for path in required):
        return {"ok": False, "reason": "toolchain_incomplete"}
    if _tree_identity(ghidra, maximum_bytes=MAX_GHIDRA_TREE_BYTES) != GHIDRA_TREE_SHA256:
        return {"ok": False, "reason": "toolchain_untrusted"}
    if _tree_identity(jdk, maximum_bytes=MAX_JDK_TREE_BYTES) != JDK_TREE_SHA256:
        return {"ok": False, "reason": "toolchain_untrusted"}
    if not _fixed_files_match(ghidra, _GHIDRA_FILES) or not _fixed_files_match(jdk, _JDK_FILES):
        return {"ok": False, "reason": "toolchain_untrusted"}
    try:
        launcher = (ghidra / "support/analyzeHeadless").stat()
        java = (jdk / "bin/java").stat()
    except OSError:
        return {"ok": False, "reason": "toolchain_untrusted"}
    if not launcher.st_mode & stat.S_IXUSR or not java.st_mode & stat.S_IXUSR:
        return {"ok": False, "reason": "toolchain_untrusted"}
    return {
        "ok": True,
        "identity": "pinned_full_tree",
        "tool_name": TOOL_NAME,
        "tool_version": GHIDRA_VERSION,
        "jdk_version": JDK_VERSION,
    }


def host_toolchain_preflight() -> dict[str, Any]:
    """Admit only the fixed owner-local install locations."""

    if GHIDRA_ROOT != TOOLS_ROOT / "ghidra-12.1.3" or JDK_ROOT != TOOLS_ROOT / "jdk-21.0.12.1+1":
        return {"ok": False, "reason": "toolchain_untrusted"}
    return verify_toolchain(GHIDRA_ROOT, JDK_ROOT)


def sandbox_toolchain_preflight() -> dict[str, Any]:
    """Repeat identity checks after bubblewrap has made both trees read-only."""

    return verify_toolchain(SANDBOX_GHIDRA_ROOT, SANDBOX_JDK_ROOT)


def _headless_argv(input_path: Path, output_path: Path, script_path: Path) -> list[str]:
    """Return the complete code-owned argv; no artifact value becomes an option."""

    return [
        str(SANDBOX_GHIDRA_ROOT / "support/analyzeHeadless"),
        "/work",
        "friday-decompile",
        "-import",
        str(input_path),
        "-readOnly",
        "-deleteProject",
        "-recursive",
        "0",
        "-analysisTimeoutPerFile",
        str(ANALYSIS_TIMEOUT_SECONDS),
        "-scriptPath",
        str(script_path),
        "-postScript",
        "FridayDecompile.java",
        str(output_path),
    ]


def _headless_environment() -> dict[str, str]:
    return {
        "HOME": "/tmp",
        "JAVA_HOME": str(SANDBOX_JDK_ROOT),
        "PATH": f"{SANDBOX_JDK_ROOT}/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_CACHE_HOME": "/tmp",
        "XDG_CONFIG_HOME": "/tmp",
        "TMPDIR": "/tmp",
        "GHIDRA_HEADLESS_MAXMEM": "3072M",
        "JAVA_TOOL_OPTIONS": (
            "-Xms64m -Xmx3072m -Xss512k -XX:ActiveProcessorCount=4 "
            "-XX:CompressedClassSpaceSize=128m -XX:ReservedCodeCacheSize=192m "
            "-XX:MaxMetaspaceSize=384m -XX:MaxDirectMemorySize=512m "
            "-XX:+DisableAttachMechanism -XX:+ExitOnOutOfMemoryError "
            "-XX:ErrorFile=/tmp/hs_err_pid%p.log"
        ),
        "GHIDRA_HEADLESS_JAVA_OPTIONS": (
            "-XX:ActiveProcessorCount=4 -XX:+DisableAttachMechanism "
            "-Dcpu.core.override=4 -Djava.io.tmpdir=/tmp "
            "-Dapplication.tempdir=/tmp -Dapplication.cachedir=/tmp "
            "-Dapplication.settingsdir=/tmp"
        ),
    }


def _read_headless_json(path: Path) -> Mapping[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_size <= 0
            or details.st_size > MAX_HEADLESS_JSON_BYTES
        ):
            return None
        payload = bytearray()
        while len(payload) <= MAX_HEADLESS_JSON_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_HEADLESS_JSON_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > MAX_HEADLESS_JSON_BYTES:
            return None
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        parsed = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _read_at(descriptor: int, offset: int, length: int) -> bytes:
    payload = bytearray()
    while len(payload) < length:
        try:
            chunk = os.pread(descriptor, length - len(payload), offset + len(payload))
        except OSError:
            return b""
        if not chunk:
            break
        payload.extend(chunk)
    return bytes(payload)


def _elf_header_valid(descriptor: int, size: int, prefix: bytes) -> bool:
    if len(prefix) < 16 or prefix[:4] != b"\x7fELF":
        return False
    elf_class, data_encoding, ident_version = prefix[4], prefix[5], prefix[6]
    if elf_class not in {1, 2} or data_encoding not in {1, 2} or ident_version != 1:
        return False
    header_size = 52 if elf_class == 1 else 64
    if size < header_size:
        return False
    header = _read_at(descriptor, 0, header_size)
    if len(header) != header_size:
        return False
    byteorder: Literal["little", "big"] = "little" if data_encoding == 1 else "big"

    def field(offset: int, length: int) -> int:
        return int.from_bytes(header[offset : offset + length], byteorder)

    if field(16, 2) not in {1, 2, 3, 4} or field(18, 2) == 0 or field(20, 4) != 1:
        return False
    if elf_class == 1:
        program_offset = field(28, 4)
        section_offset = field(32, 4)
        declared_header_size = field(40, 2)
        program_entry_size, program_count = field(42, 2), field(44, 2)
        section_entry_size, section_count = field(46, 2), field(48, 2)
        minimum_program_entry, minimum_section_entry = 32, 40
    else:
        program_offset = field(32, 8)
        section_offset = field(40, 8)
        declared_header_size = field(52, 2)
        program_entry_size, program_count = field(54, 2), field(56, 2)
        section_entry_size, section_count = field(58, 2), field(60, 2)
        minimum_program_entry, minimum_section_entry = 56, 64
    if declared_header_size != header_size:
        return False
    if program_count and (
        program_offset < header_size
        or program_entry_size < minimum_program_entry
        or program_offset + program_entry_size * program_count > size
    ):
        return False
    return not (
        section_count
        and (
            section_offset < header_size
            or section_entry_size < minimum_section_entry
            or section_offset + section_entry_size * section_count > size
        )
    )


def _pe_header_valid(descriptor: int, size: int, prefix: bytes) -> bool:
    if len(prefix) < 64 or prefix[:2] != b"MZ":
        return False
    pe_offset = int.from_bytes(prefix[60:64], "little")
    if pe_offset < 64 or pe_offset > size - 26:
        return False
    coff = _read_at(descriptor, pe_offset, 26)
    if len(coff) != 26 or coff[:4] != b"PE\0\0":
        return False
    machine = int.from_bytes(coff[4:6], "little")
    section_count = int.from_bytes(coff[6:8], "little")
    optional_size = int.from_bytes(coff[20:22], "little")
    optional_magic = int.from_bytes(coff[24:26], "little")
    minimum_optional_size = 96 if optional_magic == 0x10B else 112
    if (
        machine == 0
        or not 1 <= section_count <= 96
        or optional_magic not in {0x10B, 0x20B}
        or optional_size < minimum_optional_size
        or optional_size > 4096
    ):
        return False
    section_table_end = pe_offset + 24 + optional_size + section_count * 40
    return section_table_end <= size


def _validated_native_kind(path: Path) -> tuple[str | None, str | None]:
    """Classify only a minimally valid native header via a no-follow fd."""

    artifact = Path(path)
    try:
        before = artifact.lstat()
        if artifact.resolve(strict=True) != artifact or not stat.S_ISREG(before.st_mode):
            return None, "input_size_invalid"
    except OSError:
        return None, "input_unavailable"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(artifact, flags)
    except OSError:
        return None, "input_unavailable"
    try:
        details = os.fstat(descriptor)
        identity = (details.st_dev, details.st_ino, details.st_mode, details.st_size)
        if identity != (before.st_dev, before.st_ino, before.st_mode, before.st_size):
            return None, "input_unavailable"
        if details.st_size <= 0 or details.st_size > MAX_INPUT_BYTES:
            return None, "input_size_invalid"
        prefix = _read_at(descriptor, 0, min(details.st_size, 64))
        if _elf_header_valid(descriptor, details.st_size, prefix):
            detected = "elf"
        elif _pe_header_valid(descriptor, details.st_size, prefix):
            detected = "pe"
        else:
            detected = None
        current = artifact.lstat()
        current_identity = (current.st_dev, current.st_ino, current.st_mode, current.st_size)
        if current_identity != identity:
            return None, "input_unavailable"
        return detected, None
    except OSError:
        return None, "input_unavailable"
    finally:
        os.close(descriptor)


def _required_text(raw: Mapping[str, Any], key: str, maximum: int) -> str | None:
    value = raw.get(key)
    if type(value) is not str:
        return None
    return value[:maximum]


def _normalize_report(raw: Mapping[str, Any], kind: str) -> DecompileReport | None:
    if raw.get("schema") != SCHEMA:
        return None
    raw_functions = raw.get("functions")
    if not isinstance(raw_functions, list) or len(raw_functions) > MAX_FUNCTIONS:
        return None
    functions: list[DecompileFunction] = []
    total_pseudocode = 0
    for raw_function in raw_functions:
        if not isinstance(raw_function, Mapping):
            return None
        address = _required_text(raw_function, "address", 32)
        name = _required_text(raw_function, "name", MAX_FUNCTION_NAME_CHARS)
        signature = _required_text(raw_function, "signature", MAX_SIGNATURE_CHARS)
        raw_pseudocode = _required_text(raw_function, "pseudocode", MAX_PSEUDOCODE_CHARS_PER_FUNCTION)
        status = raw_function.get("decompile_status")
        pseudocode_truncated = raw_function.get("pseudocode_truncated")
        thunk = raw_function.get("thunk")
        if (
            address is None
            or name is None
            or signature is None
            or raw_pseudocode is None
            or type(status) is not str
            or type(pseudocode_truncated) is not bool
            or type(thunk) is not bool
        ):
            return None
        if status not in {"completed", "failed", "timeout"}:
            return None
        remaining = max(0, MAX_TOTAL_PSEUDOCODE_CHARS - total_pseudocode)
        pseudocode = raw_pseudocode[:remaining]
        bounded_pseudocode = raw_function["pseudocode"]
        assert isinstance(bounded_pseudocode, str)
        pseudocode_truncated = (
            pseudocode_truncated
            or len(bounded_pseudocode) > len(raw_pseudocode)
            or len(raw_pseudocode) > len(pseudocode)
        )
        total_pseudocode += len(pseudocode)
        functions.append(
            {
                "address": address,
                "name": name,
                "signature": signature,
                "pseudocode": pseudocode,
                "decompile_status": status,  # type: ignore[typeddict-item]
                "pseudocode_truncated": pseudocode_truncated,
                "thunk": thunk,
            }
        )
    language_id = _required_text(raw, "language_id", 160)
    compiler_spec_id = _required_text(raw, "compiler_spec_id", 160)
    analysis_timed_out = raw.get("analysis_timed_out")
    index_truncated = raw.get("function_index_truncated")
    raw_output_truncated = raw.get("output_truncated")
    function_count = raw.get("function_count_lower_bound")
    reported_pseudocode_chars = raw.get("pseudocode_chars")
    if (
        language_id is None
        or compiler_spec_id is None
        or type(analysis_timed_out) is not bool
        or type(index_truncated) is not bool
        or type(raw_output_truncated) is not bool
        or type(function_count) is not int
        or not len(functions) <= function_count <= MAX_SCANNED_FUNCTIONS
        or type(reported_pseudocode_chars) is not int
        or not total_pseudocode <= reported_pseudocode_chars <= MAX_TOTAL_PSEUDOCODE_CHARS
    ):
        return None
    output_truncated = (
        raw_output_truncated
        or total_pseudocode >= MAX_TOTAL_PSEUDOCODE_CHARS
        or any(item["pseudocode_truncated"] for item in functions)
    )
    warnings: list[str] = []
    if analysis_timed_out:
        warnings.append("analysis_timeout")
    if index_truncated:
        warnings.append("function_index_truncated")
    if output_truncated:
        warnings.append("pseudocode_truncated")
    partial = bool(warnings) or any(item["decompile_status"] != "completed" for item in functions)
    return {
        "ok": True,
        "status": "partial" if partial else "completed",
        "schema": SCHEMA,
        "tool_name": TOOL_NAME,
        "tool_version": GHIDRA_VERSION,
        "jdk_version": JDK_VERSION,
        "format": kind,
        "language_id": language_id,
        "compiler_spec_id": compiler_spec_id,
        "analysis_timed_out": analysis_timed_out,
        "function_count_lower_bound": function_count,
        "function_index_truncated": index_truncated,
        "pseudocode_chars": total_pseudocode,
        "output_truncated": output_truncated,
        "functions": functions,
        "warnings": warnings,
        "observe_only": True,
        "sample_executed": False,
        "network": "none",
    }


def decompile_artifact(
    input_path: Path,
    kind: str,
    toolchain_admission: Mapping[str, Any] | None,
) -> DecompileReport:
    """Run one bounded native decompilation inside the Engineer sandbox."""

    normalized_kind = str(kind or "unknown").casefold()[:16]
    artifact = Path(input_path)
    detected_kind, input_error = _validated_native_kind(artifact)
    if input_error is not None:
        return _failure(normalized_kind, "failed", input_error)
    if detected_kind not in SUPPORTED_KINDS:
        return _failure("unknown", "unsupported", "unsupported_format")
    # The bounded fd-backed parser is authoritative.  The worker's broader
    # artifact classifier intentionally includes filename fallbacks and only a
    # small prefix, so a long but valid PE DOS stub may arrive as ``dos``.
    normalized_kind = detected_kind
    admitted = toolchain_admission if isinstance(toolchain_admission, Mapping) else {}
    if admitted.get("ok") is not True:
        reason = str(admitted.get("reason") or "toolchain_missing")
        if reason not in _TOOLCHAIN_FAILURES:
            reason = "toolchain_untrusted"
        return _failure(normalized_kind, "unavailable", reason)
    mounted = sandbox_toolchain_preflight()
    if mounted.get("ok") is not True:
        reason = str(mounted.get("reason") or "toolchain_untrusted")
        if reason not in _TOOLCHAIN_FAILURES:
            reason = "toolchain_untrusted"
        return _failure(normalized_kind, "unavailable", reason)

    output_path = artifact.parent / "ghidra-decompile.json"
    if output_path.exists():
        return _failure(normalized_kind, "failed", "workspace_not_clean")
    script_path = Path(__file__).resolve().parent / "ghidra_scripts"
    argv = _headless_argv(artifact, output_path, script_path)
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - every argv token is code-owned
            argv,
            executable=argv[0],
            env=_headless_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        try:
            process.wait(timeout=HEADLESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return _failure(normalized_kind, "failed", "decompiler_timeout")
    except OSError:
        if process is not None and process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        return _failure(normalized_kind, "failed", "decompiler_launch_failed")
    if process.returncode != 0:
        return _failure(normalized_kind, "failed", "decompiler_failed")
    raw = _read_headless_json(output_path)
    if raw is None:
        return _failure(normalized_kind, "failed", "decompiler_output_invalid")
    normalized = _normalize_report(raw, normalized_kind)
    if normalized is None:
        return _failure(normalized_kind, "failed", "decompiler_output_invalid")
    return normalized


__all__ = [
    "DecompileFunction",
    "DecompileReport",
    "GHIDRA_ARCHIVE_SHA256",
    "GHIDRA_ROOT",
    "GHIDRA_TREE_SHA256",
    "GHIDRA_VERSION",
    "JDK_ROOT",
    "JDK_ARCHIVE_SHA256",
    "JDK_TREE_SHA256",
    "JDK_VERSION",
    "decompile_artifact",
    "host_toolchain_preflight",
    "verify_toolchain",
]
