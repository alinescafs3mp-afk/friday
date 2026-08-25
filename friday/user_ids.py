"""Neutral closed validation for stable Friday user identifiers."""

from __future__ import annotations

import re

USER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$")


def validate_user_id(user_id: str) -> str:
    """Validate stable tenant identifiers before they reach SQL or UI routes."""

    value = str(user_id or "").strip()
    if not USER_ID_RE.fullmatch(value):
        raise ValueError(
            "user_id must be 1-200 characters using letters, digits, dot, underscore, colon, @, +, or -"
        )
    return value


__all__ = ["USER_ID_RE", "validate_user_id"]
