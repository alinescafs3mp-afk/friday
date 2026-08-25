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
PROTOCOL_VERSION = 1
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_REQUEST_BYTES = 512 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 50 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_WALL_SECONDS = 45.0
MAX_CPU_SECONDS = 30
MAX_ADDRESS_SPACE_BYTES = 768 * 1024 * 1024
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
    return {
        "ok": True,
        "boundary": "bubblewrap",
        "network": "none",
        "protocol": PROTOCOL_VERSION,
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


def _user_task_count() -> int:
    uid = os.getuid()
    total = 0
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return 256
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            status_text = (entry / "status").read_text(encoding="ascii", errors="ignore")
        except OSError:
            continue
        real_uid = next(
            (line.split()[1] for line in status_text.splitlines() if line.startswith("Uid:")),
            "",
        )
        if real_uid != str(uid):
            continue
        try:
            tasks = len(list((entry / "task").iterdir()))
        except OSError:
            tasks = 1
        total += max(1, tasks)
    return max(1, total)


def _limit_worker_resources(nproc_limit: int) -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    resource.setrlimit(
        resource.RLIMIT_AS,
        (MAX_ADDRESS_SPACE_BYTES, MAX_ADDRESS_SPACE_BYTES),
    )
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    with suppress(OSError, ValueError):
        resource.setrlimit(resource.RLIMIT_NPROC, (nproc_limit, nproc_limit))


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
        nproc_limit = _user_task_count() + 16
        with open_private_text_write(stderr_path) as stderr_handle:
            process = subprocess.Popen(  # noqa: S603 - fixed trusted bwrap argv
                _sandbox_argv(workspace),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle,
                close_fds=True,
                start_new_session=True,
                preexec_fn=lambda: _limit_worker_resources(nproc_limit),
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
    if (
        result.get("ok") is not True
        or not isinstance(admission, Mapping)
        or admission.get("ok") is not True
        or admission.get("boundary") != "bubblewrap"
        or admission.get("network") != "none"
        or admission.get("protocol") != PROTOCOL_VERSION
    ):
        return {"ok": False, "reason": "sandbox_smoke_failed"}
    admitted = {
        "ok": True,
        "boundary": admission.get("boundary"),
        "network": admission.get("network"),
        "protocol": admission.get("protocol"),
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
