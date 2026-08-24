"""Bounded NVIDIA telemetry pinned to the accepted Friday laptop GPU."""

from __future__ import annotations

import math
import subprocess  # nosec B404
import threading
import time
from dataclasses import dataclass

# This module executes one fixed nvidia-smi projection and never invokes a shell.


class GpuTelemetryError(RuntimeError):
    pass


EXPECTED_GPU_UUID = "GPU-d7ef849e-55f5-f33c-2812-9dc32b644b07"
EXPECTED_GPU_NAME = "NVIDIA GeForce RTX 5080 Laptop GPU"
EXPECTED_GPU_MEMORY_TOTAL_MIB = 16_303


@dataclass(frozen=True, slots=True)
class GpuSample:
    uuid: str
    name: str
    total_mib: float
    used_mib: float
    free_mib: float
    temperature_c: float
    power_w: float
    utilization_pct: float


def sample_gpu() -> GpuSample:
    command = [
        "nvidia-smi",
        f"--id={EXPECTED_GPU_UUID}",
        (
            "--query-gpu=uuid,name,memory.total,memory.used,memory.free,"
            "temperature.gpu,power.draw,utilization.gpu"
        ),
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GpuTelemetryError("bounded nvidia-smi projection failed") from exc
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise GpuTelemetryError("nvidia-smi did not return exactly one GPU row")
    parts = [part.strip() for part in lines[0].split(",")]
    if len(parts) != 8:
        raise GpuTelemetryError("nvidia-smi GPU row has the wrong shape")
    try:
        numbers = [float(part) for part in parts[2:]]
    except ValueError as exc:
        raise GpuTelemetryError("nvidia-smi GPU row is non-numeric") from exc
    if any(not math.isfinite(value) or value < 0 for value in numbers):
        raise GpuTelemetryError("nvidia-smi GPU row is outside finite bounds")
    sample = GpuSample(parts[0], parts[1], *numbers)
    _validate_identity(sample)
    return sample


def _validate_identity(sample: GpuSample) -> None:
    if (
        sample.uuid != EXPECTED_GPU_UUID
        or sample.name != EXPECTED_GPU_NAME
        or sample.total_mib != EXPECTED_GPU_MEMORY_TOTAL_MIB
    ):
        raise GpuTelemetryError("telemetry GPU differs from the accepted laptop receipt")


def expected_gpu_identity() -> dict[str, str | int]:
    """Return the content-free GPU identity bound into every telemetry sample."""

    return {
        "uuid": EXPECTED_GPU_UUID,
        "name": EXPECTED_GPU_NAME,
        "memory_total_mib": EXPECTED_GPU_MEMORY_TOTAL_MIB,
    }


class GpuSampler:
    def __init__(self, interval_sec: float = 0.2) -> None:
        self._interval_sec = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.samples: list[GpuSample] = []
        self.error: GpuTelemetryError | None = None

    def __enter__(self) -> GpuSampler:
        def collect() -> None:
            while not self._stop.is_set():
                try:
                    self.samples.append(sample_gpu())
                except GpuTelemetryError as exc:
                    self.error = exc
                    return
                self._stop.wait(self._interval_sec)

        self._thread = threading.Thread(target=collect, name="secondary-gpu-sampler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._thread is not None and self._thread.is_alive():
            self.error = GpuTelemetryError("GPU telemetry thread did not stop")
        time.sleep(0)


def sample_summary(samples: list[GpuSample]) -> dict[str, float]:
    if not samples:
        raise GpuTelemetryError("no GPU telemetry samples were collected")
    for sample in samples:
        _validate_identity(sample)
    return {
        "total_mib": min(sample.total_mib for sample in samples),
        "peak_used_mib": max(sample.used_mib for sample in samples),
        "minimum_free_mib": min(sample.free_mib for sample in samples),
        "peak_temperature_c": max(sample.temperature_c for sample in samples),
        "peak_power_w": max(sample.power_w for sample in samples),
        "peak_utilization_pct": max(sample.utilization_pct for sample in samples),
    }
