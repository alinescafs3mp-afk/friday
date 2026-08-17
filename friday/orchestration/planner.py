"""The bounded model call which produces a TurnPlan and nothing else."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Protocol

from friday.model_input_hygiene import model_messages_are_secret_free
from friday.orchestration.contracts import TURN_PLAN_SCHEMA, TurnInput, TurnPlan

_PLANNER_SYSTEM_PROMPT = f"""\
You are Friday's semantic turn planner. Return exactly one JSON object and no prose.
You describe intent; you do not authorize users, read files, execute tools, or answer the user.
The required schema is {TURN_PLAN_SCHEMA} with exactly these keys:
schema, route, objective, evidence_requests, tool_intents, output, confidence, fallback, reason_code.
route: small_talk|ordinary_dialogue|file_read|archive_read|web_read|effect|unknown.
Each evidence request has exactly kind, query, max_items, required.
kind: attached_files|archive|web|conversation; max_items is 1..20.
Each tool intent has exactly name, arguments, effect, purpose.
effect: read|write|high. Any write/high intent requires route=effect.
output has exactly format, language, require_citations, one_message.
format: text|table|document. fallback: legacy|refuse.
Every source-backed route must set require_citations=true. one_message must always be true.
file_read uses only attached_files/conversation evidence; archive_read only archive/conversation;
web_read only web/conversation. In the initial read-only canary, tool_intents must be empty.
Creating a document is an effect route even when its contents came from read-only evidence.
reason_code is a short machine label, not hidden reasoning.
For requests to inspect, compare, summarize, OCR, or find facts in supplied files, choose file_read
and request attached_files evidence. For earlier stored files, choose archive_read and archive evidence.
For current external information, choose web_read and web evidence. Never invent a tool or source.
"""
_MAX_ATTESTED_INPUT_UTF8_BYTES = 5_500


class PlannerModel(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        priority: str = "foreground",
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        reject_repeated_token_degeneration: bool = True,
        allow_retries: bool = True,
        absolute_deadline: float | None = None,
        open_silent_cooldown: bool = True,
        require_full_context: bool = False,
    ) -> dict[str, Any]: ...


class V12Planner:
    """Translate one normalized turn into the closed plan contract."""

    def __init__(self, model: PlannerModel, *, timeout_sec: float = 12.0) -> None:
        self._model = model
        self._timeout_sec = max(1.0, min(float(timeout_sec), 60.0))

    async def plan(self, turn: TurnInput, *, turn_deadline: float | None = None) -> TurnPlan:
        deadline = time.monotonic() + self._timeout_sec
        if turn_deadline is not None:
            deadline = min(deadline, turn_deadline)
        if deadline <= time.monotonic():
            raise TimeoutError("turn planning deadline has expired")
        messages = [
            {"role": "system", "content": _PLANNER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    turn.model_payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]
        if len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > (
            _MAX_ATTESTED_INPUT_UTF8_BYTES
        ):
            raise ValueError("planner input exceeds the attested context tier")
        if not model_messages_are_secret_free(messages):
            raise ValueError("planner input requires a secret projection")
        response = await asyncio.wait_for(
            self._model.chat(
                messages,
                temperature=0.0,
                max_tokens=512,
                priority="background",
                tools=None,
                allow_retries=False,
                absolute_deadline=deadline,
                open_silent_cooldown=False,
                require_full_context=True,
            ),
            timeout=max(0.001, deadline - time.monotonic()),
        )
        content = response.get("content") if isinstance(response, dict) else None
        if (
            not isinstance(content, str)
            or response.get("finish_reason") != "stop"
            or response.get("tool_calls") not in (None, [])
        ):
            raise ValueError("planner response is incomplete or effectful")
        return TurnPlan.parse(content)
