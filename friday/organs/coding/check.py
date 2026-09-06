"""Check the exact durable Python source bytes, without importing or executing them.

Compilation is not behavioral testing. This bounded compiler uses the existing
Bubblewrap toolchain, never a host fallback or the untrusted unittest runner.
There is no workspace, mutable source path, model call or new durable store.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import re
import signal
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, TypeVar

from friday.organs.coding.revision import CodingRevisionUnavailable, load_coding_revision
from friday.organs.coding.worker_spawn import BWRAP_EXECUTABLE, PRLIMIT_EXECUTABLE, PYTHON_EXECUTABLE
from friday.permissions import ActorContext, AuthorizationError

MAX_CHECK_FILES = 256
MAX_CHECK_SOURCE_BYTES = 1024 * 1024
MAX_CHECK_INPUT_BYTES = 2 * 1024 * 1024
MAX_CHECK_OUTPUT_BYTES = 32 * 1024
MAX_CHECK_ERRORS = 16
CHECK_BUDGET_SEC = 30.0
CHECK_MEMORY_BYTES = 128 * 1024 * 1024
CHECK_CPU_SEC = 10
CHECK_SCHEMA = "friday.coding-python-check.v1"
_PREFIX = re.compile(r"(?i)^(?:check|проверь)(?:\s|$)")
_REQUEST = re.compile(r"(?i)^(?:check|проверь) (msg_[0-9a-f]{16}) ([0-9a-f]{64})$")

# Fixed trusted compiler, not a Python module supplied by the project. Only
# ordinal/line diagnostics leave it; SyntaxError.text/msg and source names do not.
_CHECK_PROGRAM = f"""
import base64, hashlib, json, sys, warnings
warnings.simplefilter('ignore')
raw = sys.stdin.buffer.read({MAX_CHECK_INPUT_BYTES + 1})
if not raw or len(raw) > {MAX_CHECK_INPUT_BYTES}:
    raise SystemExit(3)
files = json.loads(raw)
if type(files) is not list or not 1 <= len(files) <= {MAX_CHECK_FILES}:
    raise SystemExit(3)
errors = []
error_count = 0
total = 0
for index, value in enumerate(files):
    body = base64.b64decode(value, validate=True)
    total += len(body)
    if total > {MAX_CHECK_SOURCE_BYTES}:
        raise SystemExit(3)
    try:
        compile(body, '<coding-source>', 'exec', dont_inherit=True, optimize=0)
    except SyntaxError as error:
        error_count += 1
        if len(errors) < {MAX_CHECK_ERRORS}:
            errors.append({{'index': index, 'line': max(0, error.lineno or 0),
                           'column': max(0, error.offset or 0)}})
result = {{'schema': {CHECK_SCHEMA!r}, 'input_sha256': hashlib.sha256(raw).hexdigest(),
          'checked_files': len(files), 'error_count': error_count, 'errors': errors,
          'python_version': '.'.join(str(n) for n in sys.version_info[:3])}}
sys.stdout.write(json.dumps(result, sort_keys=True, separators=(',', ':')))
""".strip()


@dataclass(frozen=True, slots=True)
class CodingRevisionCheck:
    message: str
    state: str
    source_message_id: str | None = None
    revision_sha256: str | None = None
    project_id: str | None = None
    report: dict[str, Any] | None = field(default=None, repr=False)


def check_requested(message: str, *, has_attachments: bool = False) -> bool:
    text = message.strip()
    if _PREFIX.match(text) is None:
        return False
    # Preserve ordinary checks of a current upload. An explicit saved-message
    # reference still belongs here and rejects any attempt to mix source sets.
    return not has_attachments or re.match(r"(?i)^(?:check|проверь)\s+msg_", text) is not None


def _check_argv() -> tuple[str, ...]:
    # No workspace/export, owner home, /run, network, database or writable host
    # mount is visible. The source travels through stdin, never argv or a path.
    return (
        BWRAP_EXECUTABLE,
        "--unshare-all",
        "--unshare-user",
        "--uid",
        str(os.geteuid()),
        "--gid",
        str(os.getegid()),
        "--cap-drop",
        "ALL",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind-try",
        "/lib",
        "/lib",
        "--ro-bind-try",
        "/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--chdir",
        "/tmp",
        "--",
        PRLIMIT_EXECUTABLE,
        f"--as={CHECK_MEMORY_BYTES}:{CHECK_MEMORY_BYTES}",
        f"--cpu={CHECK_CPU_SEC}:{CHECK_CPU_SEC}",
        "--nofile=64:64",
        "--fsize=0:0",
        "--core=0:0",
        "--",
        PYTHON_EXECUTABLE,
        "-I",
        "-B",
        "-S",
        "-c",
        _CHECK_PROGRAM,
    )


async def _stop_compiler(process: asyncio.subprocess.Process) -> bool:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
    # Drain only after killing the closed namespace. This avoids Process.wait's
    # pipe-backpressure deadlock, including the output-overflow rejection path.
    try:
        await asyncio.wait_for(process.communicate(), 5.0)
    except (OSError, TimeoutError):
        return False
    return process.returncode is not None


_T = TypeVar("_T")


async def _finish_after_cancel(task: asyncio.Task[_T]) -> _T:
    """Finish owned cleanup despite repeated cancellation, then let caller re-raise."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def run_source_compiler(payload: bytes, deadline: float) -> bytes | None:
    """One bounded trusted subprocess. Cancellation kills/reaps before returning."""

    if (
        type(payload) is not bytes
        or not payload
        or len(payload) > MAX_CHECK_INPUT_BYTES
        or type(deadline) not in (int, float)
        or not math.isfinite(deadline)
        or deadline <= time.monotonic()
    ):
        return None
    end = min(deadline, time.monotonic() + CHECK_BUDGET_SEC)
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *_check_argv(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
            limit=MAX_CHECK_OUTPUT_BYTES,
        )
    )
    try:
        # Shield process creation so cancellation cannot lose a newly forked PID.
        process = await asyncio.shield(launch)
    except asyncio.CancelledError:
        try:
            process = await _finish_after_cancel(launch)
        except OSError:
            pass
        else:
            await _finish_after_cancel(asyncio.create_task(_stop_compiler(process)))
        raise
    except (OSError, ValueError):
        return None
    assert process.stdin is not None and process.stdout is not None

    async def send() -> None:
        assert process.stdin is not None
        try:
            for offset in range(0, len(payload), 16384):
                process.stdin.write(payload[offset : offset + 16384])
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def receive() -> bytes:
        assert process.stdout is not None
        chunks = bytearray()
        while True:
            chunk = await process.stdout.read(min(8192, MAX_CHECK_OUTPUT_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_CHECK_OUTPUT_BYTES:
                raise ValueError("compiler report exceeded bound")
        return bytes(chunks)

    sender = asyncio.create_task(send())
    receiver = asyncio.create_task(receive())
    exchange = asyncio.gather(sender, receiver)
    result = None
    try:
        _, output = await asyncio.wait_for(exchange, max(0.001, end - time.monotonic()))
        await asyncio.wait_for(process.wait(), max(0.001, end - time.monotonic()))
        result = output if process.returncode == 0 and time.monotonic() < end else None
    except (OSError, ValueError, TimeoutError):
        pass
    finally:
        sender.cancel()
        receiver.cancel()
        with suppress(asyncio.CancelledError, OSError, ValueError):
            await exchange
        # Even successful compile must be reaped. No detached worker continues
        # against another turn after timeout, cancellation or malformed output.
        cleanup = asyncio.create_task(_stop_compiler(process))
        try:
            stopped = await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await _finish_after_cancel(cleanup)
            raise
    return result if stopped else None


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate compiler field")
        value[key] = item
    return value


def _checked_report(raw: bytes, payload: bytes, names: tuple[str, ...]) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_CHECK_OUTPUT_BYTES:
        raise ValueError("invalid compiler report")
    value = json.loads(raw, object_pairs_hook=_unique)
    if (
        type(value) is not dict
        or set(value)
        != {"schema", "input_sha256", "checked_files", "error_count", "errors", "python_version"}
        or value["schema"] != CHECK_SCHEMA
        or value["input_sha256"] != hashlib.sha256(payload).hexdigest()
        or type(value["checked_files"]) is not int
        or value["checked_files"] != len(names)
        or type(value["error_count"]) is not int
        or not 0 <= value["error_count"] <= len(names)
        or type(value["errors"]) is not list
        or len(value["errors"]) != min(value["error_count"], MAX_CHECK_ERRORS)
        or type(value["python_version"]) is not str
        or re.fullmatch(r"3\.[0-9]{1,3}\.[0-9]{1,3}", value["python_version"]) is None
    ):
        raise ValueError("compiler result does not match selected sources")
    errors = []
    previous = -1
    for error in value["errors"]:
        if (
            type(error) is not dict
            or set(error) != {"index", "line", "column"}
            or any(type(number) is not int for number in error.values())
            or not previous < error["index"] < len(names)
            or not 0 <= error["line"] <= MAX_CHECK_SOURCE_BYTES
            or not 0 <= error["column"] <= MAX_CHECK_SOURCE_BYTES
        ):
            raise ValueError("invalid compiler diagnostic")
        previous = error["index"]
        errors.append({"path": names[previous], "line": error["line"], "column": error["column"]})
    report = {
        "schema": CHECK_SCHEMA,
        "state": "syntax_failed" if errors else "syntax_passed",
        "checked_files": len(names),
        "error_count": value["error_count"],
        "errors": errors,
        "diagnostics_truncated": value["error_count"] > len(errors),
        "python_version": value["python_version"],
        "behavior_tested": False,
    }
    # Path identities can be much larger than the child ordinal diagnostics.
    # Drop whole trailing entries rather than truncating an ambiguous path.
    while len(json.dumps(report, ensure_ascii=False).encode("utf-8")) > MAX_CHECK_OUTPUT_BYTES - 512:
        errors.pop()
        report["diagnostics_truncated"] = True
    return report


async def prepare_revision_check(
    *,
    storage: Any,
    user_id: str,
    actor: ActorContext,
    message: str,
    conversation_id: str | None,
    attachments: list[dict[str, Any]] | None,
    turn_deadline: float | None,
) -> CodingRevisionCheck:
    """Load one explicit source revision; never choose latest or call a model."""

    if not actor.is_owner or not actor.is_private_telegram_chat:
        raise AuthorizationError("Coding check requires the installation owner's private chat")
    if turn_deadline is not None and (
        type(turn_deadline) not in (float, int) or not math.isfinite(turn_deadline)
    ):
        return CodingRevisionCheck(message, "deadline")
    deadline = (
        min(turn_deadline, time.monotonic() + CHECK_BUDGET_SEC)
        if turn_deadline is not None
        else time.monotonic() + CHECK_BUDGET_SEC
    )
    if deadline <= time.monotonic():
        return CodingRevisionCheck(message, "deadline")
    selected = _REQUEST.fullmatch(message.strip()) if len(message) <= 160 else None
    person_id = actor.own_id if actor.shared_tenant else user_id
    if selected is None or attachments or storage is None or not conversation_id:
        return CodingRevisionCheck(message, "invalid_request")
    conversation = storage.get_conversation(conversation_id, person_id)
    if not conversation or conversation.get("is_archived"):
        return CodingRevisionCheck(message, "source_unavailable")
    try:
        source = load_coding_revision(
            storage,
            storage.settings.files_dir,
            person_id=person_id,
            tenant_id=actor.user_id,
            conversation_id=conversation_id,
            message_id=selected[1],
            revision_sha256=selected[2],
        )
    except CodingRevisionUnavailable:
        return CodingRevisionCheck(message, "source_unavailable")
    files = tuple((name, body) for name, body in source.members if name.endswith(".py"))
    if not files:
        return CodingRevisionCheck(message, "no_python_sources")
    if len(files) > MAX_CHECK_FILES or sum(len(body) for _, body in files) > MAX_CHECK_SOURCE_BYTES:
        return CodingRevisionCheck(message, "input_rejected")
    names = tuple(name for name, _ in files)
    payload = json.dumps(
        [base64.b64encode(body).decode("ascii") for _, body in files], separators=(",", ":")
    ).encode()
    raw = await run_source_compiler(payload, deadline)
    if time.monotonic() >= deadline:
        return CodingRevisionCheck(message, "deadline")
    if raw is None:
        return CodingRevisionCheck(message, "compiler_unavailable")
    try:
        report = _checked_report(raw, payload, names)
    except (ValueError, TypeError, RecursionError):
        return CodingRevisionCheck(message, "report_rejected")
    report.update({"source_message_id": selected[1], "revision_sha256": source.revision_sha256})
    return CodingRevisionCheck(
        message, report["state"], selected[1], source.revision_sha256, source.project_id, report
    )


def reauthorize_revision_check(
    storage: Any,
    files_root: Any,
    *,
    binding: object,
    conversation_id: str,
    person_id: str,
    tenant_id: str,
) -> None:
    if type(binding) is not dict or set(binding) != {"message_id", "revision_sha256", "project_id"}:
        raise CodingRevisionUnavailable("check source unavailable")
    conversation = storage.get_conversation(conversation_id, person_id)
    if not conversation or conversation.get("is_archived"):
        raise CodingRevisionUnavailable("check source unavailable")
    source = load_coding_revision(
        storage,
        files_root,
        person_id=person_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        message_id=binding["message_id"],
        revision_sha256=binding["revision_sha256"],
    )
    if source.project_id != binding["project_id"]:
        raise CodingRevisionUnavailable("check source unavailable")
