"""P0B observes the runtime owner and failure stage without retaining request text."""

from __future__ import annotations

from typing import Any

import pytest

from friday.interaction_control_plane import FailureStage
from friday.interaction_control_plane.failure_store import (
    FailureEntrypoint,
    FailureRoute,
    FailureTraceScope,
    bind_failure_trace_scope,
)
from friday.orchestration import OrchestrationRouter, RouteClass, ToolEffect, TurnPlan
from friday.orchestration.router import (
    ReadOnlyRoutePreparation,
    ReadOnlyRouteRequest,
)
from friday.permissions import ActorContext


class _LegacyRuntime:
    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        del user_id, message, kwargs
        return {"message": "legacy", "conversation_id": "conv_0000000000000001"}


class _Planner:
    def __init__(self) -> None:
        self.plan_value = TurnPlan.parse(
            {
                "schema": "friday.turn-plan.v1",
                "route": "web_read",
                "objective": "Read current public evidence.",
                "evidence_requests": [
                    {"kind": "web", "query": "public query", "max_items": 3, "required": True}
                ],
                "tool_intents": [],
                "output": {
                    "format": "text",
                    "language": "en",
                    "require_citations": True,
                    "one_message": True,
                },
                "confidence": 0.9,
                "fallback": "legacy",
                "reason_code": "web_evidence",
            }
        )

    async def plan(self, turn, *, turn_deadline=None):
        del turn, turn_deadline
        return self.plan_value

    async def plan_attested(self, turn, *, turn_deadline=None):
        return await self.plan(turn, turn_deadline=turn_deadline)


class _FailingWebHandler:
    route = RouteClass.WEB_READ
    effect = ToolEffect.READ

    async def prepare(self, request, turn, plan):
        del request, turn
        return ReadOnlyRoutePreparation(
            route=plan.route,
            plan_sha256=plan.canonical_sha256(),
            evidence_identity_sha256="1" * 64,
            private_payload=object(),
        )

    async def preparation_is_current(
        self,
        request: ReadOnlyRouteRequest,
        turn,
        plan,
        preparation: ReadOnlyRoutePreparation,
    ) -> bool:
        del request, turn, plan, preparation
        return True

    async def handle(self, request, turn, plan, preparation):
        del request, turn, plan, preparation
        raise RuntimeError("private provider body")


def _scope() -> FailureTraceScope:
    return FailureTraceScope(
        user_id="route-owner",
        entrypoint=FailureEntrypoint.API_CHAT,
        conversation_id="conv_0000000000000001",
    )


def _chat_kwargs() -> dict[str, Any]:
    return {
        "actor": ActorContext("route-owner", "owner", "test"),
        "conversation_id": "conv_0000000000000001",
        "attachments": [],
        "enable_tools": True,
    }


@pytest.mark.asyncio
async def test_legacy_route_marks_its_runtime_before_calling_it() -> None:
    scope = _scope()
    router = OrchestrationRouter(_LegacyRuntime(), _Planner(), mode="legacy")

    with bind_failure_trace_scope(scope):
        await router.chat("route-owner", "private request", **_chat_kwargs())

    assert scope.route is FailureRoute.LEGACY
    assert scope.stage is FailureStage.CAPABILITY


@pytest.mark.asyncio
async def test_selected_v12_route_and_handler_failure_are_structurally_observed() -> None:
    scope = _scope()
    router = OrchestrationRouter(
        _LegacyRuntime(),
        _Planner(),
        mode="v12",
        allowed_routes=("web_read",),
        route_handlers={RouteClass.WEB_READ: _FailingWebHandler()},
    )

    with bind_failure_trace_scope(scope), pytest.raises(RuntimeError, match="private provider body"):
        await router.chat("route-owner", "private request", **_chat_kwargs())

    assert scope.route is FailureRoute.WEB_READ
    assert scope.stage is FailureStage.CAPABILITY
