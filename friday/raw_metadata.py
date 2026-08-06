"""One bounded decoder for private Raw Object metadata envelopes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

RAW_FILE_METADATA_MAX_BYTES = 128 * 1024


def bounded_raw_file_metadata(value: Any) -> dict[str, Any]:
    """Decode one metadata object without accepting an unbounded carrier."""

    if isinstance(value, Mapping):
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, RecursionError, UnicodeError):
            return {}
        return dict(value) if len(encoded) <= RAW_FILE_METADATA_MAX_BYTES else {}
    if not isinstance(value, str):
        return {}
    try:
        encoded = value.encode("utf-8")
    except UnicodeError:
        return {}
    if len(encoded) > RAW_FILE_METADATA_MAX_BYTES:
        return {}
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


__all__ = ["RAW_FILE_METADATA_MAX_BYTES", "bounded_raw_file_metadata"]
