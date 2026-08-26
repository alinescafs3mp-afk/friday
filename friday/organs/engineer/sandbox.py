"""Bubblewrap boundary for owner-supplied artifact parsing and mutation.

The backend never imports a third-party binary parser over untrusted bytes.  A
small stdlib-only worker receives one private temporary workspace, the shipped
Friday code read-only, no network namespace and strict process limits.  Only a
bounded JSON result and, for mutation, one bounded derived file cross back.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from friday.private_fs import ensure_private_directory, open_private_text_write

BWRAP = Path("/usr/bin/bwrap")
PYTHON = Path("/usr/bin/python3")
PRLIMIT = Path("/usr/bin/prlimit")
CGROUP_ROOT = Path("/sys/fs/cgroup")
SELF_CGROUP = Path("/proc/self/cgroup")
PROTOCOL_VERSION = 1
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 512 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 50 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_WALL_SECONDS = 45.0
MAX_CPU_SECONDS = 30
MAX_ADDRESS_SPACE_BYTES = 768 * 1024 * 1024
MAX_ADMITTED_CGROUP_PIDS = 65_536
_SMOKE_SUCCESS_KEY: tuple[object, ...] | None = None
_SMOKE_SUCCESS_RESULT: dict[str, Any] | None = None


class EngineerSandboxError(ValueError):
    """A closed, content-free artifact worker failure."""

    def __init__(self, code: str) -> None:
        self.code = str(code or "sandbox_failed")[:80]
        super().__init__(self.code)


def _trusted_root_executable(path: Path) -> bool:
    try:
        details = path.stat()
    except OSError:
        return False
    return bool(
        path.is_absolute()
        and stat.S_ISREG(details.st_mode)
        and details.st_uid == 0
        and not details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        and os.access(path, os.X_OK)
    )


def preflight() -> dict[str, Any]:
    """Return a content-free admission result; no untrusted input is touched."""

    if os.name != "posix" or not _trusted_root_executable(BWRAP):
        return {"ok": False, "reason": "bubblewrap_unavailable"}
    if not _trusted_root_executable(PYTHON):
        return {"ok": False, "reason": "python_untrusted"}
    if not _trusted_root_executable(PRLIMIT):
        return {"ok": False, "reason": "prlimit_unavailable"}
    pids_limit = _current_cgroup_pids_limit()
    if pids_limit is None or pids_limit > MAX_ADMITTED_CGROUP_PIDS:
        return {"ok": False, "reason": "pid_cgroup_unbounded"}
    return {
        "ok": True,
        "boundary": "bubblewrap",
        "network": "none",
        "protocol": PROTOCOL_VERSION,
        "pids_limit": pids_limit,
    }


def _write_private_bytes(path: Path, payload: bytes) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - regular-file invariant
                raise EngineerSandboxError("workspace_write_failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_bounded_regular(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EngineerSandboxError("worker_output_missing") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size < 0 or details.st_size > maximum:
            raise EngineerSandboxError("worker_output_exceeds_cap")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            raise EngineerSandboxError("worker_output_exceeds_cap")
        return payload
    finally:
        os.close(descriptor)


def _cgroup_path(raw_path: str) -> Path | None:
    """Map a kernel-reported cgroup path below its conventional mount."""

    if not raw_path.startswith("/") or "\x00" in raw_path or len(raw_path) > 4096:
        return None
    parts = tuple(part for part in raw_path.split("/") if part)
    if any(part in {".", ".."} for part in parts):
        return None
    return Path(*parts)


def _current_cgroup_pids_limit() -> int | None:
    """Return the finite PID-cgroup ceiling which bounds this process.

    RLIMIT_NPROC is intentionally not used here.  In the supported combined
    Host Control contour the container has the desktop user's real UID but a
    private PID namespace, while the kernel accounts RLIMIT_NPROC across that
    UID's hidden host tasks.  A limit derived from container ``/proc`` can thus
    make bubblewrap fail before it starts.  Docker's PID cgroup is independent
    of UID visibility and is the authoritative process-count boundary.
    """

    try:
        lines = SELF_CGROUP.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError):
        return None

    candidates: list[Path] = []
    for line in lines:
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        hierarchy, controllers, raw_path = fields
        relative = _cgroup_path(raw_path)
        if relative is None:
            continue
        if hierarchy == "0" and not controllers:
            candidates.append(CGROUP_ROOT / relative / "pids.max")
        elif "pids" in controllers.split(","):
            candidates.append(CGROUP_ROOT / "pids" / relative / "pids.max")

    # A private cgroup namespace commonly reports '/' and exposes the current
    # cgroup directly at the mount root.  Keep explicit v2/v1 fallbacks for it.
    candidates.extend((CGROUP_ROOT / "pids.max", CGROUP_ROOT / "pids" / "pids.max"))
    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            continue
        try:
            details = os.fstat(descriptor)
            payload = os.read(descriptor, 64)
            overflow = os.read(descriptor, 1)
        except OSError:
            continue
        finally:
            os.close(descriptor)
        if not stat.S_ISREG(details.st_mode) or overflow:
            return None
        value = payload.strip()
        if not value or value == b"max" or not value.isdigit():
            return None
        limit = int(value)
        return limit if limit > 0 else None
    return None


def _remaining_timeout(deadline: float | None) -> float:
    if deadline is None:
        return MAX_WALL_SECONDS
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise EngineerSandboxError("deadline_expired")
    return min(MAX_WALL_SECONDS, remaining)


def _sandbox_argv(workspace: Path) -> list[str]:
    package_root = Path(__file__).resolve().parents[2]
    if package_root.name != "friday" or not package_root.is_dir() or package_root.is_symlink():
        raise EngineerSandboxError("package_root_untrusted")
    argv = [
        str(BWRAP),
        "--unshare-all",
        "--unshare-user",
        "--die-with-parent",
        "--new-session",
        "--disable-userns",
        "--cap-drop",
        "ALL",
        "--hostname",
        "friday-engineer",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind-try",
        "/lib",
        "/lib",
        "--ro-bind-try",
        "/lib64",
        "/lib64",
        "--dir",
        "/etc",
        "--ro-bind-try",
        "/etc/ld.so.cache",
        "/etc/ld.so.cache",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/app",
        "--ro-bind",
        str(package_root),
        "/app/friday",
        "--bind",
        str(workspace),
        "/work",
        "--chdir",
        "/work",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "PYTHONPATH",
        "/app",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--setenv",
        "PYTHONHASHSEED",
        "0",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "LC_ALL",
        "C.UTF-8",
        "--",
        str(PYTHON),
        "-S",
        "-B",
        "-m",
        "friday.organs.engineer.worker",
        "/work/request.json",
        "/work/input.bin",
        "/work/result.json",
        "/work/output.bin",
    ]
    return argv


def _limited_sandbox_argv(workspace: Path) -> list[str]:
    """Apply non-PID limits in a trusted executable, never a Python fork hook."""

    return [
        str(PRLIMIT),
        "--core=0:0",
        f"--cpu={MAX_CPU_SECONDS}:{MAX_CPU_SECONDS + 1}",
        f"--fsize={MAX_OUTPUT_BYTES}:{MAX_OUTPUT_BYTES}",
        f"--as={MAX_ADDRESS_SPACE_BYTES}:{MAX_ADDRESS_SPACE_BYTES}",
        "--nofile=64:64",
        "--",
        *_sandbox_argv(workspace),
    ]


def _run_worker(
    action: str,
    data: bytes,
    filename: str,
    *,
    operations: Sequence[Mapping[str, Any]] | None = None,
    deadline: float | None = None,
    workspace_root: Path | None = None,
) -> tuple[dict[str, Any], bytes | None]:
    admission = preflight()
    if not admission.get("ok"):
        raise EngineerSandboxError(str(admission.get("reason") or "sandbox_unavailable"))
    if action not in {"analyze", "patch", "preflight"}:
        raise EngineerSandboxError("unknown_action")
    if not isinstance(data, bytes) or not data or len(data) > MAX_INPUT_BYTES:
        raise EngineerSandboxError("input_size_invalid")
    request = {
        "protocol": PROTOCOL_VERSION,
        "action": action,
        "filename": Path(str(filename or "artifact.bin")).name[:180],
        "operations": [dict(item) for item in (operations or ())],
    }
    if action == "preflight":
        try:
            request["parent_netns"] = os.readlink("/proc/self/ns/net")
        except OSError as exc:
            raise EngineerSandboxError("network_namespace_unavailable") from exc
    encoded_request = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded_request) > MAX_REQUEST_BYTES:
        raise EngineerSandboxError("request_exceeds_cap")

    parent = None
    if workspace_root is not None:
        parent = ensure_private_directory(Path(workspace_root))
    with tempfile.TemporaryDirectory(prefix="friday-engineer-", dir=parent) as directory:
        workspace = Path(directory)
        workspace.chmod(0o700)
        request_path = workspace / "request.json"
        input_path = workspace / "input.bin"
        result_path = workspace / "result.json"
        output_path = workspace / "output.bin"
        stderr_path = workspace / "stderr.log"
        _write_private_bytes(request_path, encoded_request)
        _write_private_bytes(input_path, data)
        with open_private_text_write(stderr_path) as stderr_handle:
            process = subprocess.Popen(  # noqa: S603 - fixed trusted prlimit/bwrap argv
                _limited_sandbox_argv(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle,
                close_fds=True,
                start_new_session=True,
            )
            try:
                process.wait(timeout=_remaining_timeout(deadline))
            except subprocess.TimeoutExpired as exc:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise EngineerSandboxError("worker_timeout") from exc
        if process.returncode != 0:
            # Never carry parser-controlled stderr or filesystem paths into logs,
            # audit rows or a model prompt.
            with suppress(EngineerSandboxError):
                _read_bounded_regular(stderr_path, MAX_STDERR_BYTES)
            raise EngineerSandboxError("worker_failed")
        raw_result = _read_bounded_regular(result_path, MAX_RESULT_BYTES)
        try:
            parsed = json.loads(raw_result.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EngineerSandboxError("worker_result_invalid") from exc
        if not isinstance(parsed, dict) or parsed.get("protocol") != PROTOCOL_VERSION:
            raise EngineerSandboxError("worker_protocol_mismatch")
        parsed["sandbox"] = admission
        output = None
        if action == "patch" and parsed.get("ok") is True:
            output = _read_bounded_regular(output_path, MAX_OUTPUT_BYTES)
            if not output:
                raise EngineerSandboxError("worker_output_empty")
        return parsed, output


def analyze_artifact(
    data: bytes,
    filename: str = "",
    *,
    deadline: float | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    result, _output = _run_worker(
        "analyze",
        data,
        filename,
        deadline=deadline,
        workspace_root=workspace_root,
    )
    return result


def patch_artifact(
    data: bytes,
    operations: Sequence[Mapping[str, Any]],
    filename: str = "",
    *,
    deadline: float | None = None,
    workspace_root: Path | None = None,
) -> tuple[bytes, list[dict[str, Any]], dict[str, Any]]:
    result, output = _run_worker(
        "patch",
        data,
        filename,
        operations=operations,
        deadline=deadline,
        workspace_root=workspace_root,
    )
    if result.get("ok") is not True or output is None:
        raise EngineerSandboxError(str(result.get("error") or "patch_failed"))
    raw_operations = result.get("operations")
    operation_log = [dict(item) for item in raw_operations] if isinstance(raw_operations, list) else []
    return output, operation_log, result


def smoke_preflight(
    *,
    workspace_root: Path | None = None,
    timeout_sec: float = 8.0,
) -> dict[str, Any]:
    """Prove that the configured kernel can enter the real sandbox boundary."""

    global _SMOKE_SUCCESS_KEY, _SMOKE_SUCCESS_RESULT
    try:
        bwrap_stat = BWRAP.stat()
        python_stat = PYTHON.stat()
        prlimit_stat = PRLIMIT.stat()
        root_key = str(Path(workspace_root).resolve()) if workspace_root is not None else ""
        cache_key: tuple[object, ...] = (
            str(BWRAP),
            bwrap_stat.st_dev,
            bwrap_stat.st_ino,
            bwrap_stat.st_mtime_ns,
            bwrap_stat.st_size,
            str(PYTHON),
            python_stat.st_dev,
            python_stat.st_ino,
            python_stat.st_mtime_ns,
            python_stat.st_size,
            str(PRLIMIT),
            prlimit_stat.st_dev,
            prlimit_stat.st_ino,
            prlimit_stat.st_mtime_ns,
            prlimit_stat.st_size,
            root_key,
            PROTOCOL_VERSION,
        )
    except OSError:
        cache_key = ("unavailable", str(BWRAP), str(PYTHON), str(workspace_root or ""))
    if cache_key == _SMOKE_SUCCESS_KEY and _SMOKE_SUCCESS_RESULT is not None:
        return dict(_SMOKE_SUCCESS_RESULT)

    try:
        result, _output = _run_worker(
            "preflight",
            b"friday-engineer-sandbox-preflight-v1",
            "preflight.bin",
            deadline=time.monotonic() + max(0.1, min(float(timeout_sec), 8.0)),
            workspace_root=workspace_root,
        )
    except EngineerSandboxError as exc:
        return {"ok": False, "reason": exc.code}
    admission = result.get("sandbox")
    network_proof = result.get("network_proof")
    if (
        result.get("ok") is not True
        or not isinstance(admission, Mapping)
        or admission.get("ok") is not True
        or admission.get("boundary") != "bubblewrap"
        or admission.get("network") != "none"
        or admission.get("protocol") != PROTOCOL_VERSION
        or not isinstance(network_proof, Mapping)
        or network_proof.get("namespace") != "isolated"
        or network_proof.get("external_interfaces") != 0
        or network_proof.get("external_routes") != 0
        or network_proof.get("ipv4_connectivity") != "blocked"
        or network_proof.get("ipv6_connectivity") != "blocked"
    ):
        return {"ok": False, "reason": "sandbox_smoke_failed"}
    admitted = {
        "ok": True,
        "boundary": admission.get("boundary"),
        "network": admission.get("network"),
        "protocol": admission.get("protocol"),
        "network_namespace": network_proof.get("namespace"),
        "external_interfaces": network_proof.get("external_interfaces"),
        "external_routes": network_proof.get("external_routes"),
        "ipv4_connectivity": network_proof.get("ipv4_connectivity"),
        "ipv6_connectivity": network_proof.get("ipv6_connectivity"),
    }
    _SMOKE_SUCCESS_KEY = cache_key
    _SMOKE_SUCCESS_RESULT = dict(admitted)
    return admitted


__all__ = [
    "EngineerSandboxError",
    "analyze_artifact",
    "patch_artifact",
    "preflight",
    "smoke_preflight",
]
