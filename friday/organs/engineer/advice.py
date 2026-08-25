"""Optional secondary EXTRACT over code-owned engineer findings.

Mirrors Inbox advice: assist + extract only, no tools, no publication.
Any miss falls back to the primary report unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

_MAX_NARRATIVE = 2_400
_MAX_PRIORITIES = 8


def _secondary_from(ctx: Any) -> Any:
    ingestion = getattr(ctx, "ingestion", None)
    return getattr(ingestion, "secondary_brain", None)


def unused(reason: str) -> dict[str, Any]:
    return {"used": False, "reason": reason}


def _valid(payload: Mapping[str, Any] | None) -> bool:
    if not isinstance(payload, Mapping):
        return False
    narrative = payload.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip() or len(narrative) > _MAX_NARRATIVE:
        return False
    priorities = payload.get("priorities")
    if priorities is None:
        return True
    if not isinstance(priorities, list) or len(priorities) > _MAX_PRIORITIES:
        return False
    return all(isinstance(item, str) and item.strip() and len(item) <= 240 for item in priorities)


async def advise(ctx: Any, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Ask the live secondary brain to sharpen a finding list.

    `payload` must already be the secret-stripped public projection. Raw bytes,
    strings dumps and banners stay on the primary.
    """

    secondary = _secondary_from(ctx)
    if secondary is None:
        return unused("absent")
    try:
        from friday.secondary_brain import (
            EffectClass,
            ModelModality,
            ModelPriority,
            ModelRequest,
            ModelWorkload,
            SecondaryMode,
            SecondaryResult,
        )
    except Exception:
        return unused("unavailable")

    mode = getattr(secondary, "mode", None)
    if mode is not SecondaryMode.ASSIST:
        return unused("not_assist")
    allowed = getattr(secondary, "allowed_workloads", ()) or ()
    if ModelWorkload.EXTRACT not in allowed:
        return unused("extract_not_admitted")
    if not getattr(secondary, "allow_private_text", False):
        return unused("private_text_disallowed")

    body = json.dumps({"kind": kind, "findings": dict(payload)}, ensure_ascii=False, sort_keys=True)
    messages = (
        {
            "role": "system",
            "content": (
                "You extract a short operator-facing narrative from a JSON finding list. "
                'Reply with JSON only: {"narrative": string, "priorities": [string, ...]}. '
                "Do not invent CVEs, exploits, credentials, or hosts that are not in the JSON. "
                "Do not propose payload bytes."
            ),
        },
        {"role": "user", "content": body[:8_000]},
    )

    async def primary_fallback() -> dict[str, Any]:
        return unused("primary_fallback")

    def validator(result: SecondaryResult) -> bool:
        return _valid(result.structured_output if isinstance(result.structured_output, Mapping) else None)

    try:
        request = ModelRequest(
            workload=ModelWorkload.EXTRACT,
            messages=messages,
            max_output_tokens=256,
            absolute_deadline_monotonic=secondary.new_advisory_deadline(),
            priority=ModelPriority.BACKGROUND,
            effect_class=EffectClass.NONE,
            modality=ModelModality.TEXT,
            require_structured_output=True,
            require_independent_model=True,
            contains_private_text=True,
        )
        selected = await secondary.secondary_preferred_required_result(
            request,
            primary_fallback,
            validator=validator,
        )
    except Exception:
        return unused("scheduler_error")
    if isinstance(selected, dict) and selected.get("used") is False:
        return selected
    if not isinstance(selected, SecondaryResult) or not _valid(
        selected.structured_output if isinstance(selected.structured_output, Mapping) else None
    ):
        return unused("invalid_shape")
    structured = dict(selected.structured_output)  # type: ignore[arg-type]
    raw_priorities = structured.get("priorities")
    priorities = raw_priorities if isinstance(raw_priorities, list) else []
    return {
        "used": True,
        "reason": "assist_extract",
        "model": str(selected.served_model_alias or "")[:120],
        "narrative": str(structured.get("narrative") or "").strip()[:_MAX_NARRATIVE],
        "priorities": [str(item).strip()[:240] for item in priorities][:_MAX_PRIORITIES],
    }
