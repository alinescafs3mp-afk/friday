"""Bounded, injection-resistant model projection of host application evidence."""

from __future__ import annotations

import json
import re
from typing import Any

from .contracts import ContractError, ParsedActionResult

MAX_PROJECTION_BYTES = 64 * 1024
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_TOOL_MARKUP = re.compile(
    r"</?(?:tool_call|tool_result|function_call|assistant|system)(?:\s[^>]*)?>",
    re.IGNORECASE,
)


def _clean_text(value: object, limit: int) -> str:
    text = _ANSI.sub("", str(value or ""))
    text = _CONTROL.sub("", text)
    text = _TOOL_MARKUP.sub("[APPLICATION_MARKUP_REMOVED]", text)
    return " ".join(text.split())[:limit]


def _bounded(value: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "[DEPTH_LIMIT]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _clean_text(value, 512)
    if isinstance(value, list):
        projected_items = [_bounded(item, depth=depth + 1) for item in value[:256]]
        if len(value) > 256:
            projected_items.append("[ITEM_LIMIT]")
        return projected_items
    if isinstance(value, dict):
        projected_mapping: dict[str, Any] = {}
        for raw_key in sorted(value, key=str)[:128]:
            key = _clean_text(raw_key, 80)
            if key:
                projected_mapping[key] = _bounded(value[raw_key], depth=depth + 1)
        return projected_mapping
    return _clean_text(type(value).__name__, 80)


def project_action_result(
    result: ParsedActionResult,
    *,
    maximum_bytes: int = MAX_PROJECTION_BYTES,
) -> dict[str, Any]:
    if isinstance(maximum_bytes, bool) or not 1024 <= maximum_bytes <= MAX_PROJECTION_BYTES:
        raise ContractError("model result projection byte cap is invalid")
    projection: dict[str, Any] = {
        "coverage": result.coverage.to_payload(),
        "evidence": [item.to_payload() for item in result.evidence[:16]],
        "label": "UNTRUSTED_HOST_APPLICATION_EVIDENCE",
        "parser_id": result.parser_id,
        "parser_status": result.parser_status.value,
        "result": _bounded(result.structured),
        "warnings": [_clean_text(item, 240) for item in result.warnings[:32]],
    }
    encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) <= maximum_bytes:
        return projection
    # Never slice JSON or a field mid-escape. Deterministically reduce the large
    # structured carrier while retaining status, coverage, warnings and refs.
    projection["result"] = {
        "projection_truncated": True,
        "result_digest": result.digest,
    }
    encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > maximum_bytes:  # pragma: no cover - fixed metadata is far below 1 KiB
        raise ContractError("model result metadata exceeds projection cap")
    return projection


__all__ = ["MAX_PROJECTION_BYTES", "project_action_result"]
