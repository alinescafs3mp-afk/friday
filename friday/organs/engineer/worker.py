"""Private entry point for :mod:`friday.organs.engineer.sandbox`.

This module is not a service and has no network path.  It runs only inside the
bubblewrap namespace assembled by the parent process.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import artifacts, local_binaries, toolchain

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 512 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 50 * 1024 * 1024


def _read_regular(path: Path, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size < 0 or details.st_size > maximum:
            raise ValueError("input_size_invalid")
        payload = bytearray()
        while len(payload) <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > maximum:
            raise ValueError("input_size_invalid")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _write_regular(path: Path, payload: bytes, maximum: int) -> None:
    if len(payload) > maximum:
        raise ValueError("output_size_invalid")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ValueError("output_write_failed")
            view = view[written:]
    finally:
        os.close(descriptor)


def _bounded_result(payload: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_RESULT_BYTES:
        fallback = {
            "protocol": PROTOCOL_VERSION,
            "ok": False,
            "error": "result_exceeds_cap",
        }
        encoded = json.dumps(fallback, sort_keys=True, separators=(",", ":")).encode("ascii")
    return encoded


def run(request_path: Path, input_path: Path, result_path: Path, output_path: Path) -> int:
    try:
        request_payload = json.loads(_read_regular(request_path, MAX_REQUEST_BYTES).decode("utf-8"))
        if not isinstance(request_payload, dict) or request_payload.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("request_protocol_invalid")
        action = str(request_payload.get("action") or "")
        filename = Path(str(request_payload.get("filename") or "artifact.bin")).name[:180]
        data = _read_regular(input_path, artifacts.MAX_ANALYZE_BYTES)
        if action == "preflight":
            result: dict[str, Any] = {
                "protocol": PROTOCOL_VERSION,
                "ok": True,
            }
        elif action == "analyze":
            report = artifacts.analyze_bytes(data, filename)
            report["file_identification"] = local_binaries.describe_bytes(data)
            report["toolchain"] = toolchain.inspect_artifact(input_path, str(report.get("kind") or ""))
            report["markdown"] = (
                artifacts.render_markdown(report) + "\n\n" + toolchain.render_markdown(report["toolchain"])
            )
            result = {
                **report,
                "protocol": PROTOCOL_VERSION,
                "ok": bool(report.get("ok")),
            }
        elif action == "patch":
            raw_operations = request_payload.get("operations")
            if not isinstance(raw_operations, list) or not all(
                isinstance(item, Mapping) for item in raw_operations
            ):
                raise ValueError("operations_invalid")
            patched, operation_log = artifacts.apply_patches(data, raw_operations)
            if not patched or len(patched) > MAX_OUTPUT_BYTES:
                raise ValueError("patched_output_size_invalid")
            _write_regular(output_path, patched, MAX_OUTPUT_BYTES)
            result = {
                "protocol": PROTOCOL_VERSION,
                "ok": True,
                "operations": operation_log,
                "original_sha256": artifacts.digest_bytes(data)["sha256"],
                "patched_sha256": artifacts.digest_bytes(patched)["sha256"],
                "size_bytes": len(patched),
            }
        else:
            raise ValueError("action_invalid")
    except Exception as exc:  # noqa: BLE001 - worker crosses only a fixed error code
        code = str(exc) if type(exc) is ValueError and str(exc).isidentifier() else type(exc).__name__
        result = {
            "protocol": PROTOCOL_VERSION,
            "ok": False,
            "error": code[:80],
        }
    _write_regular(result_path, _bounded_result(result), MAX_RESULT_BYTES)
    return 0


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 4:
        return 64
    return run(*(Path(value) for value in values))


if __name__ == "__main__":
    raise SystemExit(main())
