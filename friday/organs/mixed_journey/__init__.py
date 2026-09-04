"""Mixed-journey I/O observer — not a registry organ."""

from __future__ import annotations

from typing import Any

__all__ = ["mixed_status_admitted", "observe_mixed_journey"]


def __getattr__(name: str) -> Any:
    if name == "mixed_status_admitted":
        from friday.organs.mixed_journey.admit import mixed_status_admitted

        return mixed_status_admitted
    if name == "observe_mixed_journey":
        from friday.organs.mixed_journey.observe import observe_mixed_journey

        return observe_mixed_journey
    raise AttributeError(name)
