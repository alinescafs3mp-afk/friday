"""Local-only system telemetry.

No metric leaves the machine.  The helpers intentionally use the standard
library and tolerate environments without NVIDIA tooling.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _memory_snapshot() -> dict[str, int | None]:
    result: dict[str, int | None] = {"total_bytes": None, "available_bytes": None}
    if platform.system() == "Linux":
        values: dict[str, int] = {}
        try:
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition(":")
                if value:
                    values[key] = int(value.strip().split()[0]) * 1024
            result["total_bytes"] = values.get("MemTotal")
            result["available_bytes"] = values.get("MemAvailable")
        except (OSError, ValueError, IndexError):
            pass
    return result


def _gpu_snapshot() -> list[dict[str, Any]]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=index,name,temperature.gpu,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    devices: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 6:
            continue
        try:
            devices.append(
                {
                    "index": int(parts[0]),
                    "name": parts[1],
                    "temperature_c": int(parts[2]),
                    "memory_total_mib": int(parts[3]),
                    "memory_used_mib": int(parts[4]),
                    "utilization_percent": int(parts[5]),
                }
            )
        except ValueError:
            continue
    return devices


@dataclass(frozen=True)
class SystemTelemetry:
    """Produce point-in-time local resource snapshots."""

    disk_root: Path

    def snapshot(self) -> dict[str, Any]:
        # Diagnostics must also work before `jericho init`.  Measure the
        # nearest existing parent rather than failing on a missing home path.
        measured_root = self.disk_root.expanduser()
        while not measured_root.exists() and measured_root.parent != measured_root:
            measured_root = measured_root.parent
        try:
            usage = shutil.disk_usage(measured_root)
            disk = {
                "path": str(self.disk_root),
                "measured_at": str(measured_root),
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            }
        except OSError as exc:
            disk = {
                "path": str(self.disk_root),
                "measured_at": str(measured_root),
                "total_bytes": None,
                "used_bytes": None,
                "free_bytes": None,
                "error": str(exc),
            }
        try:
            load = list(os.getloadavg())
        except (AttributeError, OSError):
            load = []
        return {
            "captured_at_unix": time.time(),
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python": platform.python_version(),
            "process_id": os.getpid(),
            "cpu_count": os.cpu_count(),
            "load_average": load,
            "memory": _memory_snapshot(),
            "disk": disk,
            "gpu": _gpu_snapshot(),
        }


__all__ = ["SystemTelemetry"]
