"""Code-owned wall-clock budgets for bounded foreground stages."""

from __future__ import annotations

import math
import time

_MIB = 1 << 20


def size_scaled_budget_sec(
    *,
    size_bytes: int,
    base_sec: float,
    seconds_per_mib: float,
    maximum_sec: float,
) -> float:
    """Grow a stage ceiling by admitted MiB while retaining a hard cap."""

    size = int(size_bytes)
    base = float(base_sec)
    per_mib = float(seconds_per_mib)
    maximum = float(maximum_sec)
    if (
        size < 0
        or not math.isfinite(base)
        or not math.isfinite(per_mib)
        or not math.isfinite(maximum)
        or base <= 0.0
        or per_mib < 0.0
        or maximum < base
    ):
        raise ValueError("invalid size-scaled work budget")
    admitted_mib = max(1, math.ceil(size / _MIB))
    return min(maximum, base + max(0, admitted_mib - 1) * per_mib)


def stage_deadline(
    budget_sec: float,
    *,
    parent_deadline: float | None = None,
    now: float | None = None,
) -> float:
    """Create one monotonic stage deadline without extending its parent."""

    budget = float(budget_sec)
    started = time.monotonic() if now is None else float(now)
    if not math.isfinite(budget) or budget <= 0.0 or not math.isfinite(started):
        raise ValueError("invalid work stage deadline")
    own_deadline = started + budget
    if parent_deadline is None:
        return own_deadline
    parent = float(parent_deadline)
    if not math.isfinite(parent):
        raise ValueError("invalid parent work deadline")
    return min(own_deadline, parent)


__all__ = ["size_scaled_budget_sec", "stage_deadline"]
