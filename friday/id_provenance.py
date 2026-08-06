"""In-process provenance markers for identifiers minted by Friday itself."""

from __future__ import annotations


class _GeneratedId(str):
    """A generated identifier that has not yet crossed a serialization boundary."""


def mark_generated_id(value: str) -> str:
    """Attach provenance without changing the identifier's string behaviour."""

    return _GeneratedId(value)


def mark_verified_id(value: object) -> str:
    """Mark an identifier that code has just proved against authoritative state."""

    return _GeneratedId(str(value))


def is_marked_generated_id(value: object) -> bool:
    """Tell the audit boundary whether an ID was minted in this process."""

    return isinstance(value, _GeneratedId)


__all__ = ["is_marked_generated_id", "mark_generated_id", "mark_verified_id"]
