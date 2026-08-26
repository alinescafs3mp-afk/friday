"""The bounded model call which produces a TurnPlan and nothing else."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal, Protocol

from friday.model_input_hygiene import model_messages_are_secret_free
from friday.model_profiles import (
    ModelCapability,
    ModelEffect,
    ModelProfileLease,
    ModelRequirements,
)
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
file_read uses only attached_files/conversation evidence. For the bounded archive_read canary use
exactly one required archive request with max_items at least 2 and no conversation evidence;
web_read uses only web/conversation. Read-only routes must leave tool_intents empty. An effect route may
describe the requested write/high action declaratively in tool_intents, but never execute a protocol
tool call. Creating a document is an effect route even when its contents came from read-only evidence.
reason_code is a short machine label, not hidden reasoning.
For requests to inspect, compare, summarize, OCR, or find facts in supplied files, choose file_read
and request attached_files evidence. For earlier stored files, choose archive_read and archive evidence.
For current external information, choose web_read and web evidence. Never invent a tool or source.
"""
_MAX_ATTESTED_INPUT_UTF8_BYTES = 5_500
_EXACT_JSON_FENCE_PREFIX = "```json\n"
_EXACT_JSON_FENCE_SUFFIX = "\n```"
_PLANNER_REQUIREMENTS = ModelRequirements(
    capabilities=frozenset(
        {
            ModelCapability.TURN_PLAN_V1,
            ModelCapability.RU_PLANNING,
            ModelCapability.CONTEXT_8K,
            ModelCapability.REMOTE_CANCELLATION,
        }
    ),
    required_context_tokens=8_192,
    prepared_evidence_items=0,
    max_tool_steps=0,
    effect=ModelEffect.READ,
    # The first promoted route has an independent verifier.  Requiring that
    # capability at planning time prevents a planner-only attestation from
    # selecting a route that the same live generation cannot verify.
    verifier_required=True,
)


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


class AttestedPlannerRuntime(Protocol):
    """Narrow authority used by CANARY/V12 planning.

    SHADOW deliberately keeps using :class:`PlannerModel` directly because it
    has no publication or tool authority.  A promoted plan must instead reuse
    one live-generation lease through this interface.
    """

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None: ...

    async def complete(
        self,
        lease: ModelProfileLease,
        requirements: ModelRequirements,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int,
        priority: Literal["foreground", "background"],
        absolute_deadline: float,
    ) -> dict[str, Any]: ...


class V12Planner:
    """Translate one normalized turn into the closed plan contract."""

    def __init__(
        self,
        model: PlannerModel,
        *,
        timeout_sec: float = 12.0,
        attested_runtime: AttestedPlannerRuntime | None = None,
    ) -> None:
        self._model = model
        self._attested_runtime = attested_runtime
        self._timeout_sec = max(1.0, min(float(timeout_sec), 60.0))

    def _deadline(self, turn_deadline: float | None) -> float:
        deadline = time.monotonic() + self._timeout_sec
        if turn_deadline is not None:
            deadline = min(deadline, turn_deadline)
        if deadline <= time.monotonic():
            raise TimeoutError("turn planning deadline has expired")
        return deadline

    @staticmethod
    def _messages(turn: TurnInput) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
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
        return messages

    @staticmethod
    def _plan_from_response(response: object) -> TurnPlan:
        if not isinstance(response, dict):
            raise ValueError("planner response is incomplete or effectful")
        content = response.get("content")
        if (
            not isinstance(content, str)
            or response.get("finish_reason") != "stop"
            or response.get("tool_calls") not in (None, [])
        ):
            raise ValueError("planner response is incomplete or effectful")
        # Some otherwise conforming OpenAI-compatible profiles serialize the
        # single requested JSON object in one exact Markdown JSON fence.  Admit
        # only that closed transport wrapper: no surrounding whitespace/prose,
        # alternate fence label or second block.  TurnPlan still performs the
        # strict duplicate-key, schema and authority validation on the complete
        # unwrapped payload, and its public parser remains bare-JSON-only.
        if content.startswith(_EXACT_JSON_FENCE_PREFIX) and content.endswith(_EXACT_JSON_FENCE_SUFFIX):
            content = content[len(_EXACT_JSON_FENCE_PREFIX) : -len(_EXACT_JSON_FENCE_SUFFIX)]
        return TurnPlan.parse(content)

    async def plan(self, turn: TurnInput, *, turn_deadline: float | None = None) -> TurnPlan:
        """Effect-free SHADOW/standalone planner path.

        CANARY and V12 must call :meth:`plan_attested`; keeping the methods
        separate makes an accidental raw-model promotion mechanically visible.
        """

        deadline = self._deadline(turn_deadline)
        messages = self._messages(turn)
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
        return self._plan_from_response(response)

    async def plan_attested(
        self,
        turn: TurnInput,
        *,
        turn_deadline: float | None = None,
    ) -> TurnPlan:
        """Plan only through a current, code-owned live-model authority."""

        deadline = self._deadline(turn_deadline)
        messages = self._messages(turn)
        runtime = self._attested_runtime
        if runtime is None:
            raise RuntimeError("attested V12 planner runtime is unavailable")
        lease = await runtime.acquire_lease(
            _PLANNER_REQUIREMENTS,
            absolute_deadline=deadline,
        )
        if type(lease) is not ModelProfileLease:
            raise RuntimeError("attested V12 planner lease is unavailable")
        response = await asyncio.wait_for(
            runtime.complete(
                lease,
                _PLANNER_REQUIREMENTS,
                messages,
                max_tokens=512,
                priority="background",
                absolute_deadline=deadline,
            ),
            timeout=max(0.001, deadline - time.monotonic()),
        )
        return self._plan_from_response(response)
