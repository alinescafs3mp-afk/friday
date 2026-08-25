"""Bounded adapters for optional static-analysis binaries inside the sandbox."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from .redaction import redact_text

MAX_CAPTURE_BYTES = 256 * 1024
MAX_PUBLIC_CHARS = 16_000
_COMMANDS = (
    "file",
    "strings",
    "readelf",
    "objdump",
    "capa",
    "rabin2",
    "apkid",
)


def inventory() -> dict[str, str | None]:
    """Fixed-name tool inventory; callers never supply an executable."""

    return {name: shutil.which(name) for name in _COMMANDS}


def _limit_child_output() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_CAPTURE_BYTES, MAX_CAPTURE_BYTES))


def _run(name: str, arguments: Sequence[str], *, timeout_sec: float) -> dict[str, Any]:
    if name not in _COMMANDS:
        return {"used": False, "reason": "tool_not_admitted"}
    executable = shutil.which(name)
    if not executable:
        return {"used": False, "reason": "tool_missing"}
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed executable inventory and argv
                [executable, *(str(item) for item in arguments)],
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
                start_new_session=True,
                preexec_fn=_limit_child_output,
            )
            try:
                process.wait(timeout=max(0.1, float(timeout_sec)))
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                return {"used": True, "ok": False, "reason": "timeout"}
        except OSError:
            return {"used": True, "ok": False, "reason": "launch_failed"}
        stdout.seek(0)
        stderr.seek(0)
        raw_stdout = stdout.read(MAX_CAPTURE_BYTES + 1)
        raw_stderr = stderr.read(64 * 1024 + 1)
    truncated = len(raw_stdout) > MAX_CAPTURE_BYTES or len(raw_stderr) > 64 * 1024
    text = redact_text(
        raw_stdout[:MAX_CAPTURE_BYTES].decode("utf-8", errors="replace"),
        limit=MAX_PUBLIC_CHARS,
        single_line=False,
    )
    error = redact_text(
        raw_stderr[: 64 * 1024].decode("utf-8", errors="replace"),
        limit=2_000,
        single_line=False,
    )
    return {
        "used": True,
        "ok": process.returncode == 0,
        "exit_code": process.returncode,
        "output": text,
        "diagnostic": error,
        "truncated": truncated or len(text) >= MAX_PUBLIC_CHARS,
    }


def _version(name: str) -> str:
    arguments = ("--version",)
    if name == "rabin2":
        arguments = ("-v",)
    result = _run(name, arguments, timeout_sec=2.0)
    value = str(result.get("output") or result.get("diagnostic") or "")
    return value.splitlines()[0][:160] if result.get("used") else ""


def inspect_artifact(path: Path, kind: str) -> dict[str, Any]:
    """Run a fixed, static-only adapter set and return bounded evidence."""

    artifact = Path(path)
    if artifact != artifact.resolve() or not artifact.is_file() or artifact.is_symlink():
        raise ValueError("artifact_path_invalid")
    kind = str(kind or "unknown").casefold()
    planned: list[tuple[str, tuple[str, ...], float]] = [
        ("file", ("-b", "--", str(artifact)), 4.0),
        ("strings", ("-a", "-n", "5", "--", str(artifact)), 6.0),
    ]
    if kind == "elf":
        planned.extend(
            [
                (
                    "readelf",
                    ("--file-header", "--program-headers", "--section-headers", "--wide", str(artifact)),
                    8.0,
                ),
                ("readelf", ("--dynamic", "--wide", str(artifact)), 8.0),
                ("objdump", ("-p", "-h", str(artifact)), 8.0),
            ]
        )
    elif kind in {"pe", "dos"}:
        planned.append(("objdump", ("-p", "-h", str(artifact)), 8.0))
    elif kind in {"apk", "dex"}:
        planned.append(("apkid", (str(artifact),), 12.0))

    # capa and rabin2 materially deepen the evidence when an operator installs
    # them, but their absence is an explicit fact rather than a fabricated pass.
    if kind in {"pe", "dos", "elf"}:
        planned.extend(
            [
                ("capa", ("--json", str(artifact)), 20.0),
                ("rabin2", ("-I", "-S", "-i", str(artifact)), 10.0),
            ]
        )

    evidence: list[dict[str, Any]] = []
    versions: dict[str, str] = {}
    for name, arguments, timeout in planned:
        result = _run(name, arguments, timeout_sec=timeout)
        evidence.append({"producer": name, "classification": "tool_output", **result})
        if result.get("used") and name not in versions:
            versions[name] = _version(name)
    return {
        "evidence": evidence,
        "versions": versions,
        "available": {name: bool(path) for name, path in inventory().items()},
    }


def render_markdown(report: dict[str, Any], *, max_chars: int = 8_000) -> str:
    """Bound the useful tool evidence before the dossier reaches model context."""

    lines = ["## Isolated tool evidence"]
    versions = report.get("versions")
    if isinstance(versions, dict) and versions:
        lines.append(
            "versions: " + ", ".join(f"{name}={str(value)[:120]}" for name, value in sorted(versions.items()))
        )
    evidence = report.get("evidence")
    for item in evidence if isinstance(evidence, list) else []:
        if not isinstance(item, dict) or item.get("used") is not True:
            continue
        producer = str(item.get("producer") or "tool")[:40]
        status = "ok" if item.get("ok") is True else str(item.get("reason") or "failed")[:40]
        lines.append(f"### {producer} ({status})")
        output = str(item.get("output") or "").strip()
        if output:
            lines.append("```text\n" + output[:2_500] + "\n```")
        if len("\n".join(lines)) >= max_chars:
            break
    return "\n".join(lines)[:max_chars]


__all__ = ["inspect_artifact", "inventory", "render_markdown"]
