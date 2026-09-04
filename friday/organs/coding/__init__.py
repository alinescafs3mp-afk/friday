"""Coding Mode organ — not a registry organ."""

from __future__ import annotations

from typing import Any

__all__ = ["handle_coding_static_turn"]


def __getattr__(name: str) -> Any:
    if name == "handle_coding_static_turn":
        from friday.organs.coding.static_turn import handle_coding_static_turn

        return handle_coding_static_turn
    raise AttributeError(name)
