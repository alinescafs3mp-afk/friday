#!/usr/bin/env python3
"""Review and drive one authenticated release-retention maintenance transaction.

The controller is installed as its own root-owned component.  It authenticates
the existing sealed retention toolchain but is deliberately not a member of
that toolchain.  A durable admission contains only identities that survive a
reboot; every execution attempt additionally requires a fresh current-boot
maintenance binding.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import importlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUEST_SCHEMA = "friday.release-artifact-retention-maintenance-request.v1"
RESULT_SCHEMA = "friday.release-artifact-retention-maintenance-result.v1"
COMPLETION_SCHEMA = "friday.release-artifact-retention-maintenance-completion.v1"
EXECUTION_ADMISSION_SCHEMA = "friday.release-artifact-retention-maintenance-execution-admission.v2"
MAINTENANCE_RECOVERY_SCHEMA = "friday.release-artifact-retention-maintenance-recovery-binding.v1"
INSTALLED_CONTROLLER_PATH = Path("/usr/libexec/friday/release_artifact_retention_maintenance.py")
MAINTENANCE_AUTHORITY_PATH = Path("/run/friday-retention/maintenance-premount-authority.v1.json")
MAX_REQUEST_BYTES = 64 << 20
MAX_PROFILE_FILE_BYTES = 256 << 20
MAX_CONVERGENCE_PASSES = 64
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_BOOT_ID = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\n?\Z")
_FILESYSTEM_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_RENAME_NOREPLACE = 1

# This is the already-released sealed toolchain contract.  The maintenance
# controller is intentionally absent and is authenticated independently.
_SEALED_TOOLCHAIN_MODULES = (
    "__init__.py",
    "immutable_release_operator.py",
    "release_artifact_proc_probe.py",
    "release_artifact_retention.py",
    "release_artifact_retention_operator.py",
    "release_dr_generation_authentication.py",
    "release_dr_generation_enrollment.py",
    "release_dr_generation_index.py",
    "release_dr_generation_rehearsal.py",
    "release_dr_generation_lifecycle.py",
)

proc_probe: Any = None
retention: Any = None
operator: Any = None


class MaintenanceError(RuntimeError):
    """A body-free, fail-closed maintenance error."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CurrentMaintenanceBinding:
    """Fresh volatile proof for one execution attempt in the current boot."""

    transaction_id: str
    request_file_sha256: str
    request_sha256: str
    executing_initrd_sha256: str
    maintenance_boot_id_sha256: str
    maintenance_authority_sha256: str
    maintenance_premount_receipt_sha256: str
    maintenance_namespace_epoch_sha256: str
    maintenance_process_epoch_sha256: str
    maintenance_target_receipt_sha256: str


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _absolute(path: Path, *, code: str) -> Path:
    lexical = Path(os.path.abspath(path))
    if path != lexical or not lexical.name or any(character in str(path) for character in "\x00\r\n"):
        raise MaintenanceError(code)
    return lexical


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _stable_file(
    path: Path,
    *,
    maximum: int,
    code: str,
    allowed_modes: frozenset[int],
    expected_uid: int | None = None,
) -> bytes:
    lexical = _absolute(path, code=code)
    descriptor = -1
    try:
        before = os.lstat(lexical)
        descriptor = os.open(
            lexical,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1 << 20, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise MaintenanceError(code)
        after = os.lstat(lexical)
    except (OSError, MaintenanceError) as exc:
        if isinstance(exc, MaintenanceError):
            raise
        raise MaintenanceError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    raw = b"".join(chunks)
    if (
        not raw
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) not in allowed_modes
        or expected_uid is not None
        and before.st_uid != expected_uid
        or _identity(before) != _identity(opened)
        or _identity(before) != _identity(after)
    ):
        raise MaintenanceError(code)
    return raw


def _read_json_authority(
    path: Path,
    *,
    maximum: int,
    code: str,
    allowed_modes: frozenset[int],
    expected_uid: int | None = None,
) -> tuple[dict[str, Any], bytes]:
    raw = _stable_file(
        path,
        maximum=maximum,
        code=code,
        allowed_modes=allowed_modes,
        expected_uid=expected_uid,
    )
    try:
        value = json.loads(raw.decode("ascii"), parse_constant=lambda _value: None)
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise MaintenanceError(code) from exc
    if not isinstance(value, dict) or raw != _canonical(value) + b"\n":
        raise MaintenanceError(code)
    return value, raw


def _rename_noreplace(source: Path, target: Path) -> None:
    """Atomically publish one Linux-local file without a hard-link window."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = renameat2(
            -100,  # AT_FDCWD
            os.fsencode(source),
            -100,
            os.fsencode(target),
            _RENAME_NOREPLACE,
        )
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError) as exc:
        raise OSError(errno.ENOSYS, "renameat2") from exc
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def _write_no_replace(path: Path, value: Mapping[str, Any], *, mode: int) -> None:
    lexical = _absolute(path, code="maintenance_output_invalid")
    payload = _canonical(dict(value)) + b"\n"
    parent = lexical.parent
    temporary: Path | None = None
    try:
        parent_status = os.lstat(parent)
        if (
            not stat.S_ISDIR(parent_status.st_mode)
            or stat.S_IMODE(parent_status.st_mode) & 0o077
            or parent_status.st_uid != os.geteuid()
        ):
            raise MaintenanceError("maintenance_output_invalid")
        temporary = parent / f".{lexical.name}.{os.getpid()}.{os.urandom(8).hex()}.new"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise MaintenanceError("maintenance_output_invalid")
                offset += written
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            _rename_noreplace(temporary, lexical)
        except FileExistsError as exc:
            existing, existing_raw = _read_json_authority(
                lexical,
                maximum=max(len(payload), 1 << 20),
                code="maintenance_output_changed",
                allowed_modes=frozenset({mode}),
                expected_uid=os.geteuid(),
            )
            if existing_raw != payload or existing != dict(value):
                raise MaintenanceError("maintenance_output_changed") from exc
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
            temporary = None
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except (OSError, MaintenanceError) as exc:
        if temporary is not None:
            with suppress(OSError):
                os.unlink(temporary)
        if isinstance(exc, MaintenanceError):
            raise
        raise MaintenanceError("maintenance_output_invalid") from exc


def _file_sha256(
    path: Path,
    *,
    code: str,
    allowed_modes: frozenset[int] | None = None,
    expected_uid: int | None = None,
) -> str:
    lexical = _absolute(path, code=code)
    modes = allowed_modes if allowed_modes is not None else frozenset(range(0o400, 0o776))
    descriptor = -1
    try:
        before = os.lstat(lexical)
        descriptor = os.open(
            lexical,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= MAX_PROFILE_FILE_BYTES
            or stat.S_IMODE(before.st_mode) not in modes
            or expected_uid is not None
            and before.st_uid != expected_uid
            or _identity(before) != _identity(opened)
        ):
            raise MaintenanceError(code)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(
                descriptor,
                min(1 << 20, MAX_PROFILE_FILE_BYTES + 1 - total),
            )
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > MAX_PROFILE_FILE_BYTES:
                raise MaintenanceError(code)
        opened_after = os.fstat(descriptor)
        after = os.lstat(lexical)
    except (OSError, MaintenanceError) as exc:
        if isinstance(exc, MaintenanceError):
            raise
        raise MaintenanceError(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if total == 0 or _identity(before) != _identity(opened_after) or _identity(before) != _identity(after):
        raise MaintenanceError(code)
    return digest.hexdigest()


def _proc_bytes(path: Path, *, maximum: int, code: str) -> bytes:
    try:
        before = os.lstat(path)
        raw = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise MaintenanceError(code) from exc
    if (
        not raw
        or len(raw) > maximum
        or not stat.S_ISREG(before.st_mode)
        or _identity(before) != _identity(after)
    ):
        raise MaintenanceError(code)
    return raw


def _boot_id_sha256(*, code: str) -> str:
    raw = _proc_bytes(
        Path("/proc/sys/kernel/random/boot_id"),
        maximum=37,
        code=code,
    )
    if _BOOT_ID.fullmatch(raw) is None:
        raise MaintenanceError(code)
    return hashlib.sha256(raw.strip()).hexdigest()


def _root_device_id() -> str:
    raw = _proc_bytes(
        Path("/proc/self/mountinfo"),
        maximum=4 << 20,
        code="ordinary_profile_invalid",
    )
    roots: list[str] = []
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[4] == b"/":
            try:
                value = fields[2].decode("ascii")
            except UnicodeError as exc:
                raise MaintenanceError("ordinary_profile_invalid") from exc
            if re.fullmatch(r"[0-9]+:[0-9]+", value) is None:
                raise MaintenanceError("ordinary_profile_invalid")
            roots.append(value)
    if len(roots) != 1:
        raise MaintenanceError("ordinary_profile_invalid")
    return roots[0]


def _root_filesystem_uuid(cmdline: bytes, *, code: str) -> str:
    roots = [token for token in cmdline.split() if token.startswith(b"root=")]
    if len(roots) != 1 or not roots[0].startswith(b"root=UUID="):
        raise MaintenanceError(code)
    try:
        value = roots[0][len(b"root=UUID=") :].decode("ascii")
    except UnicodeError as exc:
        raise MaintenanceError(code) from exc
    if _FILESYSTEM_UUID.fullmatch(value) is None:
        raise MaintenanceError(code)
    return value


def _root_uuid_device_id(root_filesystem_uuid: str, *, code: str) -> str:
    if _FILESYSTEM_UUID.fullmatch(root_filesystem_uuid) is None:
        raise MaintenanceError(code)
    path = Path("/dev/disk/by-uuid") / root_filesystem_uuid
    try:
        before = os.lstat(path)
        target = os.stat(path)
        after = os.lstat(path)
    except OSError as exc:
        raise MaintenanceError(code) from exc
    if (
        not stat.S_ISLNK(before.st_mode)
        or before.st_uid != 0
        or before.st_nlink != 1
        or _identity(before) != _identity(after)
        or not stat.S_ISBLK(target.st_mode)
    ):
        raise MaintenanceError(code)
    return f"{os.major(target.st_rdev)}:{os.minor(target.st_rdev)}"


def _ordinary_profile(
    *,
    kernel_image: Path,
    kernel_config: Path,
    ordinary_initrd: Path,
) -> dict[str, Any]:
    io_uring_raw = _proc_bytes(
        Path("/proc/sys/kernel/io_uring_disabled"),
        maximum=3,
        code="ordinary_profile_invalid",
    )
    if io_uring_raw not in {b"0\n", b"1\n"}:
        raise MaintenanceError("ordinary_profile_invalid")
    cmdline = _proc_bytes(
        Path("/proc/cmdline"),
        maximum=64 << 10,
        code="ordinary_profile_invalid",
    ).rstrip(b"\n")
    root_device_id = _root_device_id()
    root_filesystem_uuid = _root_filesystem_uuid(
        cmdline,
        code="ordinary_profile_invalid",
    )
    if (
        _root_uuid_device_id(
            root_filesystem_uuid,
            code="ordinary_profile_invalid",
        )
        != root_device_id
    ):
        raise MaintenanceError("ordinary_profile_invalid")
    uname = os.uname()
    return {
        "cmdline_sha256": hashlib.sha256(cmdline).hexdigest(),
        "io_uring_disabled": int(io_uring_raw[:1]),
        "kernel_config_path": str(_absolute(kernel_config, code="ordinary_profile_invalid")),
        "kernel_config_sha256": _file_sha256(
            kernel_config,
            code="ordinary_profile_invalid",
        ),
        "kernel_image_path": str(_absolute(kernel_image, code="ordinary_profile_invalid")),
        "kernel_image_sha256": _file_sha256(
            kernel_image,
            code="ordinary_profile_invalid",
        ),
        "kernel_release": uname.release,
        "kernel_version_sha256": hashlib.sha256(uname.version.encode("utf-8")).hexdigest(),
        "ordinary_initrd_path": str(_absolute(ordinary_initrd, code="ordinary_profile_invalid")),
        "ordinary_initrd_sha256": _file_sha256(
            ordinary_initrd,
            code="ordinary_profile_invalid",
        ),
        "root_device_id": root_device_id,
        "root_filesystem_uuid": root_filesystem_uuid,
    }


def _validate_toolchain(
    root: Path,
    *,
    expected_manifest_sha256: str,
    expected_uid: int,
) -> None:
    """Authenticate the existing sealed toolchain without adding this file."""

    lexical = _absolute(root, code="maintenance_toolchain_invalid")
    tools_dir = lexical / "tools"
    try:
        root_status = os.lstat(lexical)
        tools_status = os.lstat(tools_dir)
        root_entries = {item.name for item in lexical.iterdir()}
        tool_entries = {item.name for item in tools_dir.iterdir()}
    except OSError as exc:
        raise MaintenanceError("maintenance_toolchain_invalid") from exc
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or not stat.S_ISDIR(tools_status.st_mode)
        or root_status.st_uid != expected_uid
        or tools_status.st_uid != expected_uid
        or stat.S_IMODE(root_status.st_mode) != 0o500
        or stat.S_IMODE(tools_status.st_mode) != 0o500
        or root_entries != {"manifest.json", "tools"}
        or tool_entries != set(_SEALED_TOOLCHAIN_MODULES)
    ):
        raise MaintenanceError("maintenance_toolchain_invalid")
    manifest_raw = _stable_file(
        lexical / "manifest.json",
        maximum=1 << 20,
        code="maintenance_toolchain_invalid",
        allowed_modes=frozenset({0o400}),
        expected_uid=expected_uid,
    )
    if hashlib.sha256(manifest_raw).hexdigest() != expected_manifest_sha256:
        raise MaintenanceError("maintenance_toolchain_invalid")
    try:
        manifest = json.loads(manifest_raw.decode("ascii"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise MaintenanceError("maintenance_toolchain_invalid") from exc
    entries = manifest.get("files") if isinstance(manifest, Mapping) else None
    if (
        not isinstance(manifest, Mapping)
        or manifest_raw != _canonical(manifest) + b"\n"
        or set(manifest) != {"contract", "files", "schema"}
        or manifest.get("contract") != "sealed-release-retention-dr-v1"
        or manifest.get("schema") != "friday.immutable-release-retention-toolchain-manifest.v1"
        or not isinstance(entries, list)
        or len(entries) != len(_SEALED_TOOLCHAIN_MODULES)
    ):
        raise MaintenanceError("maintenance_toolchain_invalid")
    for module, entry in zip(_SEALED_TOOLCHAIN_MODULES, entries, strict=True):
        if (
            not isinstance(entry, Mapping)
            or set(entry) != {"path", "sha256", "size"}
            or entry.get("path") != f"artifacts/release-retention-toolchain-v1/tools/{module}"
            or not _is_hex64(entry.get("sha256"))
            or type(entry.get("size")) is not int
            or not 0 < int(entry["size"]) <= 4 << 20
        ):
            raise MaintenanceError("maintenance_toolchain_invalid")
        content = _stable_file(
            tools_dir / module,
            maximum=4 << 20,
            code="maintenance_toolchain_invalid",
            allowed_modes=frozenset({0o400}),
            expected_uid=expected_uid,
        )
        if len(content) != entry["size"] or hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise MaintenanceError("maintenance_toolchain_invalid")


def _load_toolchain_modules(root: Path) -> None:
    global operator, proc_probe, retention  # noqa: PLW0603
    expected = {
        "proc_probe": root / "tools/release_artifact_proc_probe.py",
        "retention": root / "tools/release_artifact_retention.py",
        "operator": root / "tools/release_artifact_retention_operator.py",
    }
    if all(globals()[name] is not None for name in expected):
        if any(
            Path(getattr(globals()[name], "__file__", "")).resolve() != path
            for name, path in expected.items()
        ):
            raise MaintenanceError("maintenance_toolchain_invalid")
        return
    root_text = str(root)
    sys.path.insert(0, root_text)
    try:
        proc_probe = importlib.import_module("tools.release_artifact_proc_probe")
        retention = importlib.import_module("tools.release_artifact_retention")
        operator = importlib.import_module("tools.release_artifact_retention_operator")
    except (ImportError, OSError, ValueError) as exc:
        raise MaintenanceError("maintenance_toolchain_invalid") from exc
    finally:
        if sys.path and sys.path[0] == root_text:
            sys.path.pop(0)
    if any(
        Path(getattr(globals()[name], "__file__", "")).resolve() != path for name, path in expected.items()
    ):
        raise MaintenanceError("maintenance_toolchain_invalid")


def _bind_toolchain(
    root: Path,
    *,
    expected_manifest_sha256: str,
    expected_uid: int,
) -> None:
    _validate_toolchain(
        root,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_uid=expected_uid,
    )
    _load_toolchain_modules(root)


def _authenticate_installed_controller(request: Mapping[str, Any]) -> None:
    path = Path(__file__).resolve()
    expected_path = Path(str(request.get("installed_controller_path", "")))
    expected_sha256 = request.get("controller_sha256")
    if path != INSTALLED_CONTROLLER_PATH or expected_path != INSTALLED_CONTROLLER_PATH:
        raise MaintenanceError("maintenance_controller_invalid")
    observed = _file_sha256(
        path,
        code="maintenance_controller_invalid",
        allowed_modes=frozenset({0o555}),
        expected_uid=0,
    )
    if not _is_hex64(expected_sha256) or observed != expected_sha256:
        raise MaintenanceError("maintenance_controller_invalid")


def _scope_seed(
    *,
    activation_journal: Path,
    unit_journal: Path,
    reviewed_scratch_targets: Sequence[Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        scope = retention.load_retention_scope_authority(activation_journal=activation_journal)
        bindings = retention.build_retention_authority_bindings(
            activation_journal=activation_journal,
            unit_journal=unit_journal,
            canonical_evidence_roots=scope.canonical_evidence_roots,
        )
        seed = retention.plan_release_artifact_retention(
            activation_journal=activation_journal,
            unit_journal=unit_journal,
            backup_root=scope.backup_root,
            inventory_roots=scope.inventory_roots,
            backup_inventory_roots=scope.backup_inventory_roots,
            reviewed_scratch_targets=reviewed_scratch_targets,
            open_inventory=retention.OpenInventorySnapshot(
                source="code_owned_candidate_scope_seed_v1",
                complete=True,
            ),
            authority_bindings=bindings,
            executable=True,
            _scope_seed=True,
            _retention_scope=scope.receipt,
        )
    except (retention.RetentionPlanError, OSError, ValueError) as exc:
        raise MaintenanceError("maintenance_review_invalid") from exc
    if seed.get("classification_status") != "scope_seed":
        raise MaintenanceError("maintenance_review_invalid")
    inputs = {
        "activation_journal": str(activation_journal),
        "backup_inventory_roots": [str(path) for path in scope.backup_inventory_roots],
        "backup_root": str(scope.backup_root),
        "canonical_evidence_roots": [
            {
                "authority_path": str(item.authority_path),
                "authority_sha256": item.authority_sha256,
                "path": str(item.path),
            }
            for item in scope.canonical_evidence_roots
        ],
        "inventory_roots": [str(path) for path in scope.inventory_roots],
        "reviewed_scratch_targets": [
            {"inventory_sha256": item.inventory_sha256, "path": str(item.path)}
            for item in reviewed_scratch_targets
        ],
        "unit_journal": str(unit_journal),
    }
    return seed, inputs


def _candidate_review_identities(
    plan: Mapping[str, Any],
    *,
    allow_boot_rebind: bool = False,
) -> list[dict[str, Any]]:
    """Use the sealed operator's sole portable review projection contract."""

    try:
        projected = operator.portable_reviewed_candidate_identities(
            plan,
            allow_boot_rebind=allow_boot_rebind,
        )
    except (OSError, operator.RetentionApplyError) as exc:
        raise MaintenanceError("maintenance_review_changed") from exc
    return [dict(item) for item in projected]


def _current_review_inputs(
    inputs: Mapping[str, Any],
    *,
    reviewed_candidates: object,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rebuild exact scratch digests, then authenticate the whole portable set."""

    try:
        accepted = operator._validate_portable_reviewed_candidates(  # noqa: SLF001
            reviewed_candidates
        )
    except operator.RetentionApplyError as exc:
        raise MaintenanceError("maintenance_review_changed") from exc
    rebound: list[Any] = []
    for item in inputs.get("reviewed_scratch_targets", ()):
        if not isinstance(item, retention.ReviewedScratchTarget):
            raise MaintenanceError("maintenance_review_changed")
        try:
            before = retention._snapshot(item.path)  # noqa: SLF001
            after = retention._snapshot(item.path)  # noqa: SLF001
        except (OSError, retention.RetentionPlanError) as exc:
            raise MaintenanceError("maintenance_review_changed") from exc
        if before != after:
            raise MaintenanceError("maintenance_review_changed")
        rebound.append(
            retention.ReviewedScratchTarget(
                path=item.path,
                inventory_sha256=hashlib.sha256(_canonical(before.records)).hexdigest(),
                contour=item.contour,
            )
        )
    current = dict(inputs)
    current["reviewed_scratch_targets"] = tuple(rebound)
    seed, _projection = _scope_seed(
        activation_journal=current["activation_journal"],
        unit_journal=current["unit_journal"],
        reviewed_scratch_targets=current["reviewed_scratch_targets"],
    )
    if _candidate_review_identities(seed) != accepted:
        raise MaintenanceError("maintenance_review_changed")
    return current, seed


def _stored_plan_request_binding(
    plan: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    request_file_sha256: str,
) -> None:
    """Authenticate stable request fields before permitting boot rebinding."""

    try:
        authority = operator._maintenance_plan_authority(plan)  # noqa: SLF001
    except operator.RetentionApplyError as exc:
        raise MaintenanceError("maintenance_plan_invalid") from exc
    if (
        authority is None
        or authority.get("transaction_id") != request.get("transaction_id")
        or authority.get("request_file_sha256") != request_file_sha256
    ):
        raise MaintenanceError("maintenance_plan_invalid")


def create_review_request(
    *,
    activation_journal: Path,
    unit_journal: Path,
    kernel_image: Path,
    kernel_config: Path,
    ordinary_initrd: Path,
    toolchain_root: Path,
    output: Path,
    reviewed_scratch_targets: Sequence[Any] = (),
) -> dict[str, Any]:
    owner_uid = os.geteuid()
    if owner_uid <= 0:
        raise MaintenanceError("maintenance_owner_invalid")
    toolchain = _absolute(toolchain_root, code="maintenance_toolchain_invalid")
    manifest_sha256 = _file_sha256(
        toolchain / "manifest.json",
        code="maintenance_toolchain_invalid",
    )
    _bind_toolchain(
        toolchain,
        expected_manifest_sha256=manifest_sha256,
        expected_uid=owner_uid,
    )
    normalized_scratch: list[Any] = []
    for item in reviewed_scratch_targets:
        if isinstance(item, tuple) and len(item) == 2:
            normalized_scratch.append(
                retention.ReviewedScratchTarget(
                    path=Path(item[0]),
                    inventory_sha256=str(item[1]),
                )
            )
        elif isinstance(item, retention.ReviewedScratchTarget):
            normalized_scratch.append(item)
        else:
            raise MaintenanceError("maintenance_review_invalid")
    activation = _absolute(activation_journal, code="maintenance_review_invalid")
    unit = _absolute(unit_journal, code="maintenance_review_invalid")
    seed, inputs = _scope_seed(
        activation_journal=activation,
        unit_journal=unit,
        reviewed_scratch_targets=tuple(normalized_scratch),
    )
    candidates = _candidate_review_identities(seed)
    if not candidates:
        raise MaintenanceError("maintenance_nothing_to_do")
    transaction_id = os.urandom(32).hex()
    state_dir = activation.parent
    maintenance_dir = state_dir / "release-artifact-retention-maintenance.v1"
    try:
        maintenance_dir.mkdir(mode=0o700, exist_ok=True)
        status = os.lstat(maintenance_dir)
    except OSError as exc:
        raise MaintenanceError("maintenance_output_invalid") from exc
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != owner_uid
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise MaintenanceError("maintenance_output_invalid")
    profile = _ordinary_profile(
        kernel_image=kernel_image,
        kernel_config=kernel_config,
        ordinary_initrd=ordinary_initrd,
    )
    ordinary_cmdline = _proc_bytes(
        Path("/proc/cmdline"),
        maximum=64 << 10,
        code="ordinary_profile_invalid",
    ).rstrip(b"\n")
    if any(token.startswith(b"rd.friday.retention=") for token in ordinary_cmdline.split()):
        raise MaintenanceError("ordinary_profile_invalid")
    maintenance_cmdline = ordinary_cmdline + b" rd.friday.retention=" + transaction_id.encode("ascii")
    controller_path = Path(__file__).resolve()
    controller_sha256 = _file_sha256(
        controller_path,
        code="maintenance_controller_invalid",
    )
    profile_sha256 = hashlib.sha256(_canonical(profile)).hexdigest()
    core: dict[str, Any] = {
        "candidate_count": len(candidates),
        "candidate_set_sha256": hashlib.sha256(_canonical(candidates)).hexdigest(),
        "completion_output_path": str(maintenance_dir / f"completion-{transaction_id}.json"),
        "controller_sha256": controller_sha256,
        "inputs": inputs,
        "installed_controller_path": str(INSTALLED_CONTROLLER_PATH),
        "maintenance_cmdline_sha256": hashlib.sha256(maintenance_cmdline).hexdigest(),
        "ordinary_profile": profile,
        "ordinary_profile_sha256": profile_sha256,
        "owner_uid": owner_uid,
        "plan_output_path": str(maintenance_dir / f"plan-{transaction_id}.json"),
        "result_output_path": str(maintenance_dir / f"result-{transaction_id}.json"),
        "reviewed_candidates": candidates,
        "scope_seed_plan_sha256": str(seed["plan_sha256"]),
        "schema": REQUEST_SCHEMA,
        "toolchain_manifest_sha256": manifest_sha256,
        "toolchain_root": str(toolchain),
        "transaction_id": transaction_id,
    }
    request = {**core, "request_sha256": hashlib.sha256(_canonical(core)).hexdigest()}
    _write_no_replace(output, request, mode=0o600)
    return request


def _validate_profile(value: object) -> dict[str, Any]:
    expected = {
        "cmdline_sha256",
        "io_uring_disabled",
        "kernel_config_path",
        "kernel_config_sha256",
        "kernel_image_path",
        "kernel_image_sha256",
        "kernel_release",
        "kernel_version_sha256",
        "ordinary_initrd_path",
        "ordinary_initrd_sha256",
        "root_device_id",
        "root_filesystem_uuid",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise MaintenanceError("maintenance_request_invalid")
    result = dict(value)
    if (
        type(result["io_uring_disabled"]) is not int
        or result["io_uring_disabled"] not in {0, 1}
        or not isinstance(result["kernel_release"], str)
        or not 1 <= len(result["kernel_release"]) <= 256
        or any(
            not _is_hex64(result[name])
            for name in (
                "cmdline_sha256",
                "kernel_config_sha256",
                "kernel_image_sha256",
                "kernel_version_sha256",
                "ordinary_initrd_sha256",
            )
        )
        or not isinstance(result["root_device_id"], str)
        or re.fullmatch(r"[0-9]+:[0-9]+", result["root_device_id"]) is None
        or not isinstance(result["root_filesystem_uuid"], str)
        or _FILESYSTEM_UUID.fullmatch(result["root_filesystem_uuid"]) is None
    ):
        raise MaintenanceError("maintenance_request_invalid")
    for name in ("kernel_config_path", "kernel_image_path", "ordinary_initrd_path"):
        _absolute(Path(str(result[name])), code="maintenance_request_invalid")
    return result


def _inputs(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "activation_journal",
        "backup_inventory_roots",
        "backup_root",
        "canonical_evidence_roots",
        "inventory_roots",
        "reviewed_scratch_targets",
        "unit_journal",
    }
    if set(value) != expected:
        raise MaintenanceError("maintenance_request_invalid")
    try:
        evidence = tuple(
            retention.CanonicalEvidenceRoot(
                path=_absolute(Path(str(item["path"])), code="maintenance_request_invalid"),
                authority_path=_absolute(
                    Path(str(item["authority_path"])),
                    code="maintenance_request_invalid",
                ),
                authority_sha256=str(item["authority_sha256"]),
            )
            for item in value["canonical_evidence_roots"]
        )
        scratch = tuple(
            retention.ReviewedScratchTarget(
                path=_absolute(Path(str(item["path"])), code="maintenance_request_invalid"),
                inventory_sha256=str(item["inventory_sha256"]),
            )
            for item in value["reviewed_scratch_targets"]
        )
        result = {
            "activation_journal": _absolute(
                Path(str(value["activation_journal"])),
                code="maintenance_request_invalid",
            ),
            "backup_inventory_roots": tuple(
                _absolute(Path(str(item)), code="maintenance_request_invalid")
                for item in value["backup_inventory_roots"]
            ),
            "backup_root": _absolute(
                Path(str(value["backup_root"])),
                code="maintenance_request_invalid",
            ),
            "canonical_evidence_roots": evidence,
            "inventory_roots": tuple(
                _absolute(Path(str(item)), code="maintenance_request_invalid")
                for item in value["inventory_roots"]
            ),
            "reviewed_scratch_targets": scratch,
            "unit_journal": _absolute(
                Path(str(value["unit_journal"])),
                code="maintenance_request_invalid",
            ),
        }
    except (KeyError, TypeError, ValueError, MaintenanceError) as exc:
        if isinstance(exc, MaintenanceError):
            raise
        raise MaintenanceError("maintenance_request_invalid") from exc
    if any(not _is_hex64(item.authority_sha256) for item in evidence) or any(
        not _is_hex64(item.inventory_sha256) for item in scratch
    ):
        raise MaintenanceError("maintenance_request_invalid")
    return result


def _load_request(
    path: Path,
    *,
    expected_sha256: str,
    require_root: bool,
) -> tuple[dict[str, Any], bytes]:
    if not _is_hex64(expected_sha256):
        raise MaintenanceError("maintenance_request_invalid")
    value, raw = _read_json_authority(
        path,
        maximum=MAX_REQUEST_BYTES,
        code="maintenance_request_invalid",
        allowed_modes=(frozenset({0o444}) if require_root else frozenset({0o400, 0o600})),
        expected_uid=0 if require_root else os.geteuid(),
    )
    expected = {
        "candidate_count",
        "candidate_set_sha256",
        "completion_output_path",
        "controller_sha256",
        "inputs",
        "installed_controller_path",
        "maintenance_cmdline_sha256",
        "ordinary_profile",
        "ordinary_profile_sha256",
        "owner_uid",
        "plan_output_path",
        "request_sha256",
        "result_output_path",
        "reviewed_candidates",
        "schema",
        "scope_seed_plan_sha256",
        "toolchain_manifest_sha256",
        "toolchain_root",
        "transaction_id",
    }
    digest = value.get("request_sha256")
    core = {name: item for name, item in value.items() if name != "request_sha256"}
    profile = _validate_profile(value.get("ordinary_profile"))
    candidates = value.get("reviewed_candidates")
    if (
        set(value) != expected
        or value.get("schema") != REQUEST_SCHEMA
        or digest != expected_sha256
        or digest != hashlib.sha256(_canonical(core)).hexdigest()
        or not _is_hex64(value.get("transaction_id"))
        or not _is_hex64(value.get("candidate_set_sha256"))
        or not _is_hex64(value.get("scope_seed_plan_sha256"))
        or not _is_hex64(value.get("ordinary_profile_sha256"))
        or not _is_hex64(value.get("maintenance_cmdline_sha256"))
        or not _is_hex64(value.get("controller_sha256"))
        or not _is_hex64(value.get("toolchain_manifest_sha256"))
        or value.get("installed_controller_path") != str(INSTALLED_CONTROLLER_PATH)
        or type(value.get("owner_uid")) is not int
        or int(value["owner_uid"]) <= 0
        or value["ordinary_profile_sha256"] != hashlib.sha256(_canonical(profile)).hexdigest()
        or not isinstance(candidates, list)
        or not candidates
        or type(value.get("candidate_count")) is not int
        or value["candidate_count"] != len(candidates)
        or value["candidate_set_sha256"] != hashlib.sha256(_canonical(candidates)).hexdigest()
        or not isinstance(value.get("inputs"), Mapping)
    ):
        raise MaintenanceError("maintenance_request_invalid")
    toolchain = _absolute(
        Path(str(value["toolchain_root"])),
        code="maintenance_request_invalid",
    )
    _bind_toolchain(
        toolchain,
        expected_manifest_sha256=str(value["toolchain_manifest_sha256"]),
        expected_uid=int(value["owner_uid"]),
    )
    normalized_inputs = _inputs(dict(value["inputs"]))
    maintenance_dir = (
        normalized_inputs["activation_journal"].parent / "release-artifact-retention-maintenance.v1"
    )
    transaction_id = str(value["transaction_id"])
    expected_outputs = {
        "completion_output_path": maintenance_dir / f"completion-{transaction_id}.json",
        "plan_output_path": maintenance_dir / f"plan-{transaction_id}.json",
        "result_output_path": maintenance_dir / f"result-{transaction_id}.json",
    }
    if any(
        _absolute(Path(str(value[name])), code="maintenance_request_invalid") != expected_path
        for name, expected_path in expected_outputs.items()
    ):
        raise MaintenanceError("maintenance_request_invalid")
    return dict(value), raw


def _execution_admission_path(plan_path: Path) -> Path:
    return plan_path.with_name(f".{plan_path.name}.execution-admission.v2.json")


def _admission_value(
    *,
    request: Mapping[str, Any],
    request_file_sha256: str,
    executing_initrd_sha256: str,
    plan_sha256: str,
) -> dict[str, Any]:
    if not all(
        _is_hex64(value)
        for value in (
            request_file_sha256,
            executing_initrd_sha256,
            plan_sha256,
            request.get("request_sha256"),
            request.get("candidate_set_sha256"),
            request.get("transaction_id"),
        )
    ):
        raise MaintenanceError("maintenance_execution_admission_invalid")
    core = {
        "executing_initrd_sha256": executing_initrd_sha256,
        "plan_sha256": plan_sha256,
        "request_file_sha256": request_file_sha256,
        "request_sha256": request["request_sha256"],
        "reviewed_candidate_set_sha256": request["candidate_set_sha256"],
        "schema": EXECUTION_ADMISSION_SCHEMA,
        "transaction_id": request["transaction_id"],
    }
    return {**core, "admission_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _execution_admission(
    *,
    path: Path,
    request: Mapping[str, Any],
    request_file_sha256: str,
    executing_initrd_sha256: str,
    plan_sha256: str,
    create: bool,
) -> dict[str, Any]:
    """Create/read a boot-neutral admission before any candidate mutation."""

    expected = _admission_value(
        request=request,
        request_file_sha256=request_file_sha256,
        executing_initrd_sha256=executing_initrd_sha256,
        plan_sha256=plan_sha256,
    )
    if create:
        _write_no_replace(path, expected, mode=0o400)
    value, _raw = _read_json_authority(
        path,
        maximum=1 << 20,
        code="maintenance_execution_admission_invalid",
        allowed_modes=frozenset({0o400}),
        expected_uid=int(request["owner_uid"]),
    )
    if value != expected:
        raise MaintenanceError("maintenance_execution_admission_invalid")
    return value


def _current_maintenance_binding(
    *,
    request_path: Path,
    request: Mapping[str, Any],
    request_raw: bytes,
) -> CurrentMaintenanceBinding:
    """Obtain and authenticate one fresh privileged current-boot receipt."""

    try:
        before = os.lstat(request_path)
        authority = retention.build_maintenance_effect_authority(
            target_path=request_path,
        )
        authority = retention._validated_maintenance_effect_authority(  # noqa: SLF001
            authority,
            code="maintenance_effect_authority_invalid",
        )
        after = os.lstat(request_path)
    except (OSError, retention.RetentionPlanError) as exc:
        raise MaintenanceError("maintenance_boot_authority_invalid") from exc
    request_file_sha256 = hashlib.sha256(request_raw).hexdigest()
    current_boot_id_sha256 = _boot_id_sha256(code="maintenance_boot_authority_invalid")
    fields = {
        "executing_initrd_sha256": getattr(authority, "executing_initrd_sha256", None),
        "maintenance_authority_sha256": getattr(authority, "authority_sha256", None),
        "maintenance_boot_id_sha256": getattr(authority, "boot_id_sha256", None),
        "maintenance_namespace_epoch_sha256": getattr(
            authority,
            "namespace_epoch_sha256",
            None,
        ),
        "maintenance_premount_receipt_sha256": getattr(
            authority,
            "premount_receipt_sha256",
            None,
        ),
        "maintenance_process_epoch_sha256": getattr(
            authority,
            "process_epoch_sha256",
            None,
        ),
        "maintenance_target_receipt_sha256": getattr(
            authority,
            "target_receipt_sha256",
            None,
        ),
    }
    if (
        _identity(before) != _identity(after)
        or getattr(authority, "transaction_id", None) != request["transaction_id"]
        or getattr(authority, "request_file_sha256", None) != request_file_sha256
        or fields["maintenance_boot_id_sha256"] != current_boot_id_sha256
        or any(not _is_hex64(value) for value in fields.values())
    ):
        raise MaintenanceError("maintenance_boot_authority_invalid")
    return CurrentMaintenanceBinding(
        transaction_id=str(request["transaction_id"]),
        request_file_sha256=request_file_sha256,
        request_sha256=str(request["request_sha256"]),
        executing_initrd_sha256=str(fields["executing_initrd_sha256"]),
        maintenance_boot_id_sha256=str(fields["maintenance_boot_id_sha256"]),
        maintenance_authority_sha256=str(fields["maintenance_authority_sha256"]),
        maintenance_premount_receipt_sha256=str(fields["maintenance_premount_receipt_sha256"]),
        maintenance_namespace_epoch_sha256=str(fields["maintenance_namespace_epoch_sha256"]),
        maintenance_process_epoch_sha256=str(fields["maintenance_process_epoch_sha256"]),
        maintenance_target_receipt_sha256=str(fields["maintenance_target_receipt_sha256"]),
    )


def _maintenance_recovery(
    *,
    admission: Mapping[str, Any],
    binding: CurrentMaintenanceBinding,
) -> dict[str, Any]:
    stable = {
        "executing_initrd_sha256": binding.executing_initrd_sha256,
        "plan_sha256": admission.get("plan_sha256"),
        "request_file_sha256": binding.request_file_sha256,
        "request_sha256": binding.request_sha256,
        "reviewed_candidate_set_sha256": admission.get("reviewed_candidate_set_sha256"),
        "transaction_id": binding.transaction_id,
    }
    if any(admission.get(name) != value for name, value in stable.items()):
        raise MaintenanceError("maintenance_execution_admission_invalid")
    core = {
        **stable,
        "maintenance_authority_sha256": binding.maintenance_authority_sha256,
        "maintenance_boot_id_sha256": binding.maintenance_boot_id_sha256,
        "maintenance_namespace_epoch_sha256": (binding.maintenance_namespace_epoch_sha256),
        "maintenance_premount_receipt_sha256": (binding.maintenance_premount_receipt_sha256),
        "maintenance_process_epoch_sha256": binding.maintenance_process_epoch_sha256,
        "maintenance_target_receipt_sha256": binding.maintenance_target_receipt_sha256,
        "schema": MAINTENANCE_RECOVERY_SCHEMA,
    }
    return {**core, "recovery_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def execute_request(*, request_path: Path, expected_request_sha256: str) -> dict[str, Any]:
    request, request_raw = _load_request(
        request_path,
        expected_sha256=expected_request_sha256,
        require_root=True,
    )
    _authenticate_installed_controller(request)
    if os.geteuid() != request["owner_uid"]:
        raise MaintenanceError("maintenance_owner_invalid")
    inputs = _inputs(dict(request["inputs"]))

    plan_path = Path(str(request["plan_output_path"]))
    admission_path = _execution_admission_path(plan_path)
    request_file_sha256 = hashlib.sha256(request_raw).hexdigest()
    create_admission = False
    if plan_path.exists() or plan_path.is_symlink():
        stored, _stored_raw = _read_json_authority(
            plan_path,
            maximum=operator.MAX_PLAN_BYTES,
            code="maintenance_plan_invalid",
            allowed_modes=frozenset({0o600}),
            expected_uid=int(request["owner_uid"]),
        )
        plan_sha256 = stored.get("plan_sha256")
        if not _is_hex64(plan_sha256):
            raise MaintenanceError("maintenance_plan_invalid")
        try:
            plan = operator._read_plan(  # noqa: SLF001
                plan_path,
                expected_sha256=str(plan_sha256),
            )
        except operator.RetentionApplyError as exc:
            raise MaintenanceError("maintenance_plan_invalid") from exc
        if not (admission_path.exists() or admission_path.is_symlink()):
            # No mutation is reachable before this create-only marker.  Its
            # absence therefore requires the whole reviewed set to remain.
            _current_inputs, _current_seed = _current_review_inputs(
                inputs,
                reviewed_candidates=request["reviewed_candidates"],
            )
            _stored_plan_request_binding(
                plan,
                request=request,
                request_file_sha256=request_file_sha256,
            )
            if (
                _candidate_review_identities(
                    plan,
                    allow_boot_rebind=True,
                )
                != request["reviewed_candidates"]
            ):
                raise MaintenanceError("maintenance_review_changed")
            create_admission = True
    else:
        current_inputs, seed = _current_review_inputs(
            inputs,
            reviewed_candidates=request["reviewed_candidates"],
        )
        try:
            plan = retention._build_maintenance_eligible_retention_plan(  # noqa: SLF001
                **current_inputs
            )
        except retention.RetentionPlanError as exc:
            raise MaintenanceError("maintenance_live_plan_blocked") from exc
        if (
            plan.get("apply_authority") is not True
            or plan.get("open_inventory", {}).get("source") != retention._MAINTENANCE_OPEN_SOURCE  # noqa: SLF001
            or _candidate_review_identities(plan) != request["reviewed_candidates"]
        ):
            raise MaintenanceError("maintenance_review_changed")
        _write_no_replace(plan_path, plan, mode=0o600)
        create_admission = True

    result_path = Path(str(request["result_output_path"]))
    if result_path.exists() or result_path.is_symlink():
        result = _validate_result(
            result_path,
            request=request,
            request_file_sha256=request_file_sha256,
            expected_uid=int(request["owner_uid"]),
        )
        _validate_result_convergence(
            result,
            activation_journal=inputs["activation_journal"],
        )
        return result

    # This call is deliberately made once for every invocation, after all
    # non-effectful plan work and immediately before admission/operator use.
    # Durable admission never substitutes for current-boot authority.
    binding = _current_maintenance_binding(
        request_path=request_path,
        request=request,
        request_raw=request_raw,
    )
    admission = _execution_admission(
        path=admission_path,
        request=request,
        request_file_sha256=request_file_sha256,
        executing_initrd_sha256=binding.executing_initrd_sha256,
        plan_sha256=str(plan["plan_sha256"]),
        create=create_admission,
    )
    recovery = _maintenance_recovery(admission=admission, binding=binding)
    convergence: dict[str, Any] | None = None
    try:
        for _attempt in range(MAX_CONVERGENCE_PASSES):
            convergence = operator.converge_retention_cycle(
                reviewed_plan_path=plan_path,
                expected_reviewed_plan_sha256=str(plan["plan_sha256"]),
                state_dir=inputs["activation_journal"].parent,
                _maintenance_recovery=recovery,
            )
            if (
                convergence.get("status") == "in_progress"
                and convergence.get("maintenance_recovery_sha256") != recovery["recovery_sha256"]
            ):
                raise MaintenanceError("maintenance_recovery_not_consumed")
            if convergence.get("status") != "in_progress":
                break
    except operator.RetentionApplyError as exc:
        raise MaintenanceError("maintenance_apply_failed_closed") from exc
    if convergence is None:
        raise MaintenanceError("maintenance_apply_failed_closed")
    if convergence.get("status") == "in_progress":
        raise MaintenanceError("maintenance_transaction_in_progress")
    if convergence.get("status") != "converged" or not _is_hex64(convergence.get("receipt_sha256")):
        raise MaintenanceError("maintenance_apply_failed_closed")
    try:
        convergence_authority = operator._maintenance_convergence_authority_for_state(  # noqa: SLF001
            state_dir=inputs["activation_journal"].parent,
            activation_receipt=operator._current_activation_receipt_path(  # noqa: SLF001
                inputs["activation_journal"].parent
            ),
        )
        recorded_recovery = convergence_authority["maintenance_recovery"]
        recovery_sha256 = recorded_recovery["recovery_sha256"]
    except (KeyError, TypeError, operator.RetentionApplyError) as exc:
        raise MaintenanceError("maintenance_apply_failed_closed") from exc
    if not _is_hex64(recovery_sha256):
        raise MaintenanceError("maintenance_apply_failed_closed")
    core = {
        "completion_required": True,
        "converged_maintenance_boot_id_sha256": recorded_recovery["maintenance_boot_id_sha256"],
        "convergence_receipt_sha256": convergence["receipt_sha256"],
        "convergence_status": convergence["status"],
        "executing_initrd_sha256": recorded_recovery["executing_initrd_sha256"],
        "maintenance_recovery_sha256": recovery_sha256,
        "ordinary_profile_sha256": request["ordinary_profile_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "request_file_sha256": request_file_sha256,
        "request_sha256": expected_request_sha256,
        "schema": RESULT_SCHEMA,
        "transaction_id": request["transaction_id"],
    }
    result = {**core, "result_sha256": hashlib.sha256(_canonical(core)).hexdigest()}
    _write_no_replace(result_path, result, mode=0o400)
    return result


def _validate_result(
    path: Path,
    *,
    request: Mapping[str, Any],
    request_file_sha256: str,
    expected_uid: int,
) -> dict[str, Any]:
    value, _raw = _read_json_authority(
        path,
        maximum=1 << 20,
        code="maintenance_result_invalid",
        allowed_modes=frozenset({0o400}),
        expected_uid=expected_uid,
    )
    expected = {
        "completion_required",
        "converged_maintenance_boot_id_sha256",
        "convergence_receipt_sha256",
        "convergence_status",
        "executing_initrd_sha256",
        "maintenance_recovery_sha256",
        "ordinary_profile_sha256",
        "plan_sha256",
        "request_file_sha256",
        "request_sha256",
        "result_sha256",
        "schema",
        "transaction_id",
    }
    digest = value.get("result_sha256")
    core = {name: item for name, item in value.items() if name != "result_sha256"}
    if (
        set(value) != expected
        or value.get("schema") != RESULT_SCHEMA
        or value.get("completion_required") is not True
        or value.get("convergence_status") != "converged"
        or not _is_hex64(digest)
        or digest != hashlib.sha256(_canonical(core)).hexdigest()
        or value.get("request_sha256") != request["request_sha256"]
        or value.get("request_file_sha256") != request_file_sha256
        or value.get("transaction_id") != request["transaction_id"]
        or value.get("ordinary_profile_sha256") != request["ordinary_profile_sha256"]
        or any(
            not _is_hex64(value.get(name))
            for name in (
                "converged_maintenance_boot_id_sha256",
                "convergence_receipt_sha256",
                "executing_initrd_sha256",
                "maintenance_recovery_sha256",
                "plan_sha256",
            )
        )
    ):
        raise MaintenanceError("maintenance_result_invalid")
    return dict(value)


def _validate_result_convergence(
    result: Mapping[str, Any],
    *,
    activation_journal: Path,
) -> dict[str, Any]:
    """Reauthenticate the durable terminal chain named by a result."""

    state_dir = activation_journal.parent
    try:
        convergence = operator._converged_receipt_for_state(  # noqa: SLF001
            state_dir=state_dir,
            activation_receipt=operator._current_activation_receipt_path(  # noqa: SLF001
                state_dir
            ),
            maintenance_recovery_sha256=str(result.get("maintenance_recovery_sha256", "")),
        )
    except (OSError, operator.RetentionApplyError) as exc:
        raise MaintenanceError("maintenance_result_invalid") from exc
    if (
        convergence.get("status") != "converged"
        or convergence.get("receipt_sha256") != result.get("convergence_receipt_sha256")
        or convergence.get("accepted_root_plan_sha256") != result.get("plan_sha256")
    ):
        raise MaintenanceError("maintenance_result_invalid")
    return dict(convergence)


def finalize_request(
    *,
    request_path: Path,
    expected_request_sha256: str,
    result_path: Path,
) -> dict[str, Any]:
    request, request_raw = _load_request(
        request_path,
        expected_sha256=expected_request_sha256,
        require_root=True,
    )
    _authenticate_installed_controller(request)
    if os.geteuid() != request["owner_uid"]:
        raise MaintenanceError("maintenance_owner_invalid")
    expected_result_path = _absolute(
        Path(str(request["result_output_path"])),
        code="maintenance_result_invalid",
    )
    if _absolute(result_path, code="maintenance_result_invalid") != expected_result_path:
        raise MaintenanceError("maintenance_result_invalid")
    result = _validate_result(
        expected_result_path,
        request=request,
        request_file_sha256=hashlib.sha256(request_raw).hexdigest(),
        expected_uid=int(request["owner_uid"]),
    )
    inputs = _inputs(dict(request["inputs"]))
    _validate_result_convergence(
        result,
        activation_journal=inputs["activation_journal"],
    )
    current_authority = getattr(
        proc_probe,
        "MAINTENANCE_AUTHORITY_PATH",
        MAINTENANCE_AUTHORITY_PATH,
    )
    if Path(current_authority).exists() or Path(current_authority).is_symlink():
        raise MaintenanceError("ordinary_profile_not_restored")
    profile = dict(request["ordinary_profile"])
    current_boot = _boot_id_sha256(code="ordinary_profile_not_restored")
    current_cmdline_raw = _proc_bytes(
        Path("/proc/cmdline"),
        maximum=64 << 10,
        code="ordinary_profile_not_restored",
    ).rstrip(b"\n")
    current_cmdline = hashlib.sha256(current_cmdline_raw).hexdigest()
    current_root_filesystem_uuid = _root_filesystem_uuid(
        current_cmdline_raw,
        code="ordinary_profile_not_restored",
    )
    current_root_device_id = _root_device_id()
    io_uring = _proc_bytes(
        Path("/proc/sys/kernel/io_uring_disabled"),
        maximum=3,
        code="ordinary_profile_not_restored",
    )
    uname = os.uname()
    if (
        current_boot == result["converged_maintenance_boot_id_sha256"]
        or current_cmdline != profile["cmdline_sha256"]
        or current_root_filesystem_uuid != profile["root_filesystem_uuid"]
        or _root_uuid_device_id(
            current_root_filesystem_uuid,
            code="ordinary_profile_not_restored",
        )
        != current_root_device_id
        or io_uring != f"{profile['io_uring_disabled']}\n".encode("ascii")
        or uname.release != profile["kernel_release"]
        or hashlib.sha256(uname.version.encode("utf-8")).hexdigest() != profile["kernel_version_sha256"]
        or _file_sha256(
            Path(profile["kernel_image_path"]),
            code="ordinary_profile_not_restored",
        )
        != profile["kernel_image_sha256"]
        or _file_sha256(
            Path(profile["kernel_config_path"]),
            code="ordinary_profile_not_restored",
        )
        != profile["kernel_config_sha256"]
        or _file_sha256(
            Path(profile["ordinary_initrd_path"]),
            code="ordinary_profile_not_restored",
        )
        != profile["ordinary_initrd_sha256"]
    ):
        raise MaintenanceError("ordinary_profile_not_restored")
    core = {
        "ordinary_boot_id_sha256": current_boot,
        "ordinary_profile_sha256": request["ordinary_profile_sha256"],
        "request_file_sha256": hashlib.sha256(request_raw).hexdigest(),
        "request_sha256": expected_request_sha256,
        "result_sha256": result["result_sha256"],
        "schema": COMPLETION_SCHEMA,
        "status": "complete_after_ordinary_reboot",
        "transaction_id": request["transaction_id"],
    }
    completion = {
        **core,
        "completion_sha256": hashlib.sha256(_canonical(core)).hexdigest(),
    }
    _write_no_replace(Path(str(request["completion_output_path"])), completion, mode=0o400)
    return completion


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    review = subcommands.add_parser("review")
    review.add_argument("--activation-journal", required=True, type=Path)
    review.add_argument("--unit-journal", required=True, type=Path)
    review.add_argument("--kernel-image", required=True, type=Path)
    review.add_argument("--kernel-config", required=True, type=Path)
    review.add_argument("--ordinary-initrd", required=True, type=Path)
    review.add_argument("--toolchain-root", required=True, type=Path)
    review.add_argument("--output", required=True, type=Path)
    review.add_argument("--reviewed-scratch", nargs=2, action="append", default=[])
    execute = subcommands.add_parser("execute")
    execute.add_argument("--request", required=True, type=Path)
    execute.add_argument("--expected-request-sha256", required=True)
    finalize = subcommands.add_parser("finalize")
    finalize.add_argument("--request", required=True, type=Path)
    finalize.add_argument("--expected-request-sha256", required=True)
    finalize.add_argument("--result", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "review":
            value = create_review_request(
                activation_journal=args.activation_journal,
                unit_journal=args.unit_journal,
                kernel_image=args.kernel_image,
                kernel_config=args.kernel_config,
                ordinary_initrd=args.ordinary_initrd,
                toolchain_root=args.toolchain_root,
                output=args.output,
                reviewed_scratch_targets=tuple(
                    (Path(path), digest) for path, digest in args.reviewed_scratch
                ),
            )
        elif args.command == "execute":
            value = execute_request(
                request_path=args.request,
                expected_request_sha256=args.expected_request_sha256,
            )
        elif args.command == "finalize":
            value = finalize_request(
                request_path=args.request,
                expected_request_sha256=args.expected_request_sha256,
                result_path=args.result,
            )
        else:  # pragma: no cover
            raise MaintenanceError("maintenance_command_invalid")
        sys.stdout.buffer.write(_canonical(value) + b"\n")
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, MaintenanceError) else "maintenance_unexpected_failure"
        if not isinstance(code, str) or not code or len(code) > 128:
            code = "maintenance_unexpected_failure"
        failure = {"failure_code": code, "status": "failed_closed"}
        sys.stderr.buffer.write(_canonical(failure) + b"\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
