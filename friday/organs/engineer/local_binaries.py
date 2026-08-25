"""Optional host binaries. Present tools are used; missing ones are named, not faked."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from typing import Any

from .redaction import redact_text

INTERESTING = ("nmap", "file", "strings", "readelf", "objdump", "openssl", "dig", "host")
MAX_PORTS = 64


def inventory() -> dict[str, str | None]:
    return {name: shutil.which(name) for name in INTERESTING}


def remaining_timeout(deadline: float | None, ceiling: float) -> float:
    """Return one stage timeout without minting time beyond the turn deadline."""

    timeout = max(0.001, float(ceiling))
    if deadline is None:
        return timeout
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("engineer deadline expired")
    return min(timeout, remaining)


def run_argv(
    argv: Sequence[str],
    *,
    timeout_sec: float = 20.0,
    deadline: float | None = None,
    stdin: bytes | None = None,
) -> dict[str, Any]:
    if not argv or not shutil.which(str(argv[0])) and "/" not in str(argv[0]):
        return {"ok": False, "error": "binary_missing", "argv": [str(argv[0]) if argv else ""]}
    try:
        completed = subprocess.run(  # noqa: S603 - argv only, never shell
            [str(item) for item in argv],
            input=stdin,
            capture_output=True,
            timeout=remaining_timeout(deadline, timeout_sec),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "argv": [str(argv[0])]}
    except OSError as exc:
        return {"ok": False, "error": type(exc).__name__, "argv": [str(argv[0])]}
    stdout = redact_text(
        completed.stdout.decode("utf-8", errors="replace"),
        limit=12_000,
        single_line=False,
    )
    stderr = redact_text(
        completed.stderr.decode("utf-8", errors="replace"),
        limit=2_000,
        single_line=False,
    )
    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "binary": str(argv[0]),
    }


def describe_bytes(data: bytes, *, deadline: float | None = None) -> dict[str, Any]:
    path = shutil.which("file")
    if not path or not data:
        return {"ok": False, "error": "file_missing"}
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=True) as handle:
        handle.write(data)
        handle.flush()
        result = run_argv([path, "-b", "--", handle.name], timeout_sec=5.0, deadline=deadline)
    result["tool"] = "file"
    return result


def nmap_connect_scan(
    host: str,
    ports: Sequence[int],
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    path = shutil.which("nmap")
    if not path:
        return {"ok": False, "error": "nmap_missing"}
    spec = ",".join(str(int(port)) for port in list(ports)[:MAX_PORTS])
    if not spec:
        return {"ok": False, "error": "no_ports"}
    result = run_argv(
        [
            path,
            "-sT",
            "-Pn",
            "-n",
            "-T4",
            "--host-timeout",
            "25s",
            "--max-retries",
            "1",
            "-sV",
            "--version-light",
            "-p",
            spec,
            str(host),
        ],
        timeout_sec=40.0,
        deadline=deadline,
    )
    result["tool"] = "nmap"
    return result


def dig_records(host: str, *, deadline: float | None = None) -> dict[str, Any]:
    path = shutil.which("dig") or shutil.which("host")
    if not path:
        return {"ok": False, "error": "resolver_missing"}
    if path.endswith("dig"):
        argv = [
            path,
            "+time=2",
            "+tries=1",
            "+noall",
            "+answer",
            str(host),
            "A",
            str(host),
            "AAAA",
            str(host),
            "MX",
            str(host),
            "TXT",
            str(host),
            "NS",
        ]
        # dig takes one qname/type; do sequential small queries.
        blobs: list[str] = []
        attempted = False
        for rtype in ("A", "AAAA", "MX", "NS", "TXT"):
            try:
                item = run_argv(
                    [path, "+time=2", "+tries=1", "+short", str(host), rtype],
                    timeout_sec=4.0,
                    deadline=deadline,
                )
            except TimeoutError:
                return {
                    "ok": False,
                    "error": "deadline",
                    "tool": "dig",
                    "records": blobs,
                    "attempted": attempted,
                }
            attempted = True
            if item.get("stdout"):
                records = sorted(line.strip() for line in str(item["stdout"]).splitlines() if line.strip())[
                    :16
                ]
                if records:
                    blobs.append(f"{rtype}: {' | '.join(records)}")
        return {
            "ok": True,
            "tool": "dig",
            "records": blobs,
            "argv": argv[:1],
            "attempted": attempted,
        }
    item = run_argv([path, str(host)], timeout_sec=4.0, deadline=deadline)
    item["tool"] = "host"
    item["attempted"] = True
    return item
