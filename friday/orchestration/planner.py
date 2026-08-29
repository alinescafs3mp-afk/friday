"""The bounded model call which produces a TurnPlan and nothing else."""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any, Literal, Protocol, cast

from friday.model_input_hygiene import model_messages_are_secret_free
from friday.model_profiles import (
    ModelCapability,
    ModelEffect,
    ModelProfileLease,
    ModelRequirements,
)
from friday.orchestration.contracts import TURN_PLAN_SCHEMA, TurnInput, TurnPlan
from friday.orchestration.turn_context import AuthenticatedTurnContext, TurnContextError
from friday.orchestration.turn_context_runtime import current_primary_authenticated_turn_context

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
_BASE_CONTEXT_TOKENS = 8_192
_MAX_MEASURED_CONTEXT_TOKENS = 40_960
_CONTEXT_TOKEN_TIERS = (_BASE_CONTEXT_TOKENS, _MAX_MEASURED_CONTEXT_TOKENS)
_EXACT_JSON_FENCE_PREFIX = "```json\n"
_EXACT_JSON_FENCE_SUFFIX = "\n```"
_PLANNER_REQUIREMENTS_BY_CONTEXT = tuple(
    ModelRequirements(
        capabilities=frozenset(
            {
                ModelCapability.TURN_PLAN_V1,
                ModelCapability.RU_PLANNING,
                ModelCapability.CONTEXT_8K,
                ModelCapability.REMOTE_CANCELLATION,
            }
        ),
        required_context_tokens=context_tokens,
        prepared_evidence_items=0,
        max_tool_steps=0,
        max_tool_rounds=0,
        max_tool_calls=0,
        effect=ModelEffect.READ,
        # Promoted routes require the same generation to support verification.
        verifier_required=True,
    )
    for context_tokens in _CONTEXT_TOKEN_TIERS
)


def _planner_requirements(required_context_tokens: int = _BASE_CONTEXT_TOKENS) -> ModelRequirements:
    if type(required_context_tokens) is not int or required_context_tokens not in _CONTEXT_TOKEN_TIERS:
        raise ValueError("planner context is outside the closed measured tiers")
    return _PLANNER_REQUIREMENTS_BY_CONTEXT[_CONTEXT_TOKEN_TIERS.index(required_context_tokens)]


_PLANNER_REQUIREMENTS = _planner_requirements()


def _planner_lease_matches_requirements(
    lease: object,
    requirements: ModelRequirements,
) -> bool:
    if not isinstance(lease, ModelProfileLease) or type(lease) is not ModelProfileLease:
        return False
    try:
        expected_requirements = _planner_requirements(requirements.required_context_tokens)
    except (AttributeError, TypeError, ValueError):
        return False
    return bool(
        type(requirements) is ModelRequirements
        and requirements is expected_requirements
        and lease.requirements_sha256 == requirements.canonical_sha256()
        and lease.capabilities == requirements.capabilities
        and lease.required_context_tokens == requirements.required_context_tokens
        and lease.prepared_evidence_items == requirements.prepared_evidence_items
        and lease.max_tool_steps == requirements.max_tool_steps
        and lease.max_tool_rounds == requirements.max_tool_rounds
        and lease.max_tool_calls == requirements.max_tool_calls
        and lease.effect is requirements.effect
        and lease.verifier_required is requirements.verifier_required
    )


def _planner_future_deadline(deadline: object, *, stage: str) -> float:
    if type(deadline) not in (int, float):
        raise TypeError(f"turn planning deadline is invalid {stage}")
    value = float(cast("int | float", deadline))
    if not math.isfinite(value):
        raise ValueError(f"turn planning deadline is invalid {stage}")
    if value <= time.monotonic():
        raise TimeoutError(f"turn planning deadline has expired {stage}")
    return value


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

    def available_context_tokens(self) -> int: ...

    async def acquire_lease(
        self,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> ModelProfileLease | None: ...

    async def lease_is_current(
        self,
        lease: object,
        requirements: ModelRequirements,
        *,
        absolute_deadline: float,
    ) -> bool: ...

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


async def _planner_lease_is_current_before_deadline(
    runtime: AttestedPlannerRuntime,
    lease: object,
    requirements: ModelRequirements = _PLANNER_REQUIREMENTS,
    *,
    absolute_deadline: float,
) -> bool:
    deadline = _planner_future_deadline(absolute_deadline, stage="before lease check")
    remaining = deadline - time.monotonic()
    if not _planner_lease_matches_requirements(lease, requirements):
        return False
    current = await asyncio.wait_for(
        runtime.lease_is_current(
            lease,
            requirements,
            absolute_deadline=deadline,
        ),
        timeout=remaining,
    )
    return type(current) is bool and current and _planner_lease_matches_requirements(lease, requirements)


def _planner_turn_context(
    turn: TurnInput,
    expected: AuthenticatedTurnContext | None,
) -> AuthenticatedTurnContext | None:
    try:
        current = current_primary_authenticated_turn_context(expected)
    except TurnContextError:
        raise RuntimeError("authenticated planner context is unavailable") from None
    if current is not expected or (current is not None and current.model_input is not turn):
        raise RuntimeError("authenticated planner context changed")
    return current


def _planner_inherited_limits(
    context: AuthenticatedTurnContext | None,
    *,
    deadline: float,
) -> tuple[float, int]:
    deadline = _planner_future_deadline(deadline, stage="before inherited parent clamp")
    if context is None:
        return deadline, 512
    parent = context.inherited_budget
    parent_deadline = _planner_future_deadline(
        math.nextafter(
            parent.safety_deadline.monotonic_ns / 1_000_000_000,
            -math.inf,
        ),
        stage="at inherited parent clamp",
    )
    deadline = _planner_future_deadline(
        min(deadline, parent_deadline),
        stage="after inherited parent clamp",
    )
    child = parent.derive_child(
        safety_deadline_monotonic_ns=parent.safety_deadline.monotonic_ns,
        max_model_calls=1,
        max_model_retries=0,
        max_tool_calls=0,
        max_tool_rounds=0,
        max_advisory_calls=0,
        max_output_tokens=512,
    )
    if child.resources.max_tool_calls != 0 or child.resources.max_tool_rounds != 0:
        raise RuntimeError("planner gained inherited tool authority")
    return deadline, min(512, child.resources.max_output_tokens)


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
        deadline = _planner_future_deadline(
            time.monotonic() + self._timeout_sec,
            stage="before local clamp",
        )
        if turn_deadline is not None:
            inherited = _planner_future_deadline(
                turn_deadline,
                stage="before local clamp",
            )
            deadline = min(deadline, inherited)
        return _planner_future_deadline(deadline, stage="after local clamp")

    @staticmethod
    def _messages(
        turn: TurnInput,
        required_context_tokens: int = _BASE_CONTEXT_TOKENS,
    ) -> list[dict[str, Any]]:
        if required_context_tokens not in _CONTEXT_TOKEN_TIERS:
            raise ValueError("planner context is outside the closed measured tiers")
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
        input_limit = (_MAX_ATTESTED_INPUT_UTF8_BYTES * required_context_tokens) // _BASE_CONTEXT_TOKENS
        if len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > input_limit:
            raise ValueError("planner input exceeds the attested context tier")
        if not model_messages_are_secret_free(messages):
            raise ValueError("planner input requires a secret projection")
        return messages

    @staticmethod
    def _attested_requirements(
        runtime: AttestedPlannerRuntime,
        turn: TurnInput,
    ) -> tuple[list[dict[str, Any]], ModelRequirements]:
        method = getattr(runtime, "available_context_tokens", None)
        if not callable(method):
            available = _BASE_CONTEXT_TOKENS
        else:
            try:
                value = method()
            except Exception:
                value = 0
            available = value if type(value) is int else 0
        for context_tokens in _CONTEXT_TOKEN_TIERS:
            if context_tokens > available:
                break
            try:
                messages = V12Planner._messages(turn, context_tokens)
            except ValueError:
                continue
            return messages, _planner_requirements(context_tokens)
        raise ValueError("planner input exceeds the available measured context")

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
        try:
            context = current_primary_authenticated_turn_context()
        except TurnContextError:
            raise RuntimeError("authenticated planner context is unavailable") from None
        _planner_turn_context(turn, context)
        deadline, max_tokens = _planner_inherited_limits(context, deadline=deadline)
        if deadline <= time.monotonic():
            raise TimeoutError("turn planning deadline has expired")
        runtime = self._attested_runtime
        if runtime is None:
            raise RuntimeError("attested V12 planner runtime is unavailable")
        messages, requirements = self._attested_requirements(runtime, turn)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("turn planning deadline has expired before lease acquisition")
        lease = await asyncio.wait_for(
            runtime.acquire_lease(
                requirements,
                absolute_deadline=deadline,
            ),
            timeout=remaining,
        )
        if type(lease) is not ModelProfileLease:
            raise RuntimeError("attested V12 planner lease is unavailable")
        _planner_turn_context(turn, context)
        if not await _planner_lease_is_current_before_deadline(
            runtime,
            lease,
            requirements,
            absolute_deadline=deadline,
        ):
            raise RuntimeError("attested V12 planner lease changed before planning")
        _planner_turn_context(turn, context)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("turn planning deadline has expired before dispatch")
        response = await asyncio.wait_for(
            runtime.complete(
                lease,
                requirements,
                messages,
                max_tokens=max_tokens,
                priority="background",
                absolute_deadline=deadline,
            ),
            timeout=remaining,
        )
        _planner_turn_context(turn, context)
        if not await _planner_lease_is_current_before_deadline(
            runtime,
            lease,
            requirements,
            absolute_deadline=deadline,
        ):
            raise RuntimeError("attested V12 planner lease changed after planning")
        _planner_turn_context(turn, context)
        return self._plan_from_response(response)
