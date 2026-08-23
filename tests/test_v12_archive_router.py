from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

import pytest

from friday.orchestration import OrchestrationRouter, RouteClass, ToolEffect, TurnInput, TurnPlan
from friday.orchestration.capability_outcome import CapabilityOutcome, CapabilityOutcomeStatus
from friday.orchestration.file_read_contract import archive_read_plan_supports_selection
from friday.orchestration.router import (
    ReadOnlyRoutePreparation,
    ReadOnlyRouteRequest,
    ReadOnlyRouteResult,
)
from friday.permissions import ActorContext


def _archive_plan_payload(
    *,
    max_items: int = 2,
    extra_evidence: bool = False,
    with_tool: bool = False,
    output_format: str = "text",
    language: str = "ru",
) -> dict[str, Any]:
    evidence_requests: list[dict[str, Any]] = [
        {
            "kind": "archive",
            "query": "обобщить последние два документа",
            "max_items": max_items,
            "required": True,
        }
    ]
    if extra_evidence:
        evidence_requests.append(
            {
                "kind": "conversation",
                "query": "учесть переписку",
                "max_items": 1,
                "required": False,
            }
        )
    tool_intents = (
        [
            {
                "name": "archive.lookup",
                "arguments": {},
                "effect": "read",
                "purpose": "найти документы",
            }
        ]
        if with_tool
        else []
    )
    return {
        "schema": "friday.turn-plan.v1",
        "route": "archive_read",
        "objective": "Обобщить выбранные документы и вернуть один ответ.",
        "evidence_requests": evidence_requests,
        "tool_intents": tool_intents,
        "output": {
            "format": output_format,
            "language": language,
            "require_citations": True,
            "one_message": True,
        },
        "confidence": 0.95,
        "fallback": "legacy",
        "reason_code": "bounded_archive_read",
    }


class _LegacyRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((user_id, message, kwargs))
        return {"message": "legacy", "conversation_id": "legacy-conversation"}


class _Planner:
    def __init__(self, plan: TurnPlan) -> None:
        self.plan_value = plan
        self.calls: list[TurnInput] = []

    async def plan(
        self,
        turn: TurnInput,
        *,
        turn_deadline: float | None = None,
    ) -> TurnPlan:
        del turn_deadline
        self.calls.append(turn)
        return self.plan_value

    async def plan_attested(
        self,
        turn: TurnInput,
        *,
        turn_deadline: float | None = None,
    ) -> TurnPlan:
        return await self.plan(turn, turn_deadline=turn_deadline)


class _ArchiveHandler:
    route = RouteClass.ARCHIVE_READ
    effect = ToolEffect.READ

    def __init__(self, *, selected_count: int = 1) -> None:
        self.selected_count = selected_count
        self.prepared: list[tuple[ReadOnlyRouteRequest, TurnInput, TurnPlan]] = []
        self.handled: list[tuple[ReadOnlyRouteRequest, TurnInput, TurnPlan]] = []

    async def prepare(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
    ) -> ReadOnlyRoutePreparation | None:
        self.prepared.append((request, turn, plan))
        if not archive_read_plan_supports_selection(plan, selected_count=self.selected_count):
            return None
        return ReadOnlyRoutePreparation(
            route=RouteClass.ARCHIVE_READ,
            plan_sha256=plan.canonical_sha256(),
            evidence_identity_sha256="1" * 64,
            private_payload=object(),
        )

    async def preparation_is_current(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> bool:
        del request, turn, plan, preparation
        return True

    async def handle(
        self,
        request: ReadOnlyRouteRequest,
        turn: TurnInput,
        plan: TurnPlan,
        preparation: ReadOnlyRoutePreparation,
    ) -> ReadOnlyRouteResult:
        self.handled.append((request, turn, plan))
        return ReadOnlyRouteResult(
            message="Архивный ответ [A1]",
            conversation_id="conv_0000000000000001",
            message_id="msg_0000000000000001",
            evidence_identity_sha256=preparation.evidence_identity_sha256,
            citation_labels=("A1",),
            verified=True,
            outcome=CapabilityOutcome(
                route=plan.route,
                status=CapabilityOutcomeStatus.COMPLETE,
                plan_sha256=plan.canonical_sha256(),
                evidence_identity_sha256=preparation.evidence_identity_sha256,
                citation_labels=("A1",),
                authority_rechecked=True,
                verified=True,
            ),
        )


def _chat_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "actor": ActorContext("owner", "owner", "test"),
        "conversation_id": "private-conversation",
        "attachments": [],
        "enable_tools": True,
        "synthetic_document_notice": False,
        "reply_to": None,
        "quoted_attachment_reference": False,
        "reply_assistant_reference": False,
        "reply_assistant_message_id": None,
    }
    values.update(overrides)
    return values


def _router(
    plan: TurnPlan,
    handler: _ArchiveHandler,
    *,
    legacy: _LegacyRuntime | None = None,
    allowed_routes: tuple[str, ...] = ("archive_read",),
    route_handlers: dict[RouteClass, Any] | None = None,
) -> tuple[OrchestrationRouter, _LegacyRuntime]:
    legacy_runtime = legacy or _LegacyRuntime()
    router = OrchestrationRouter(
        legacy_runtime,
        _Planner(plan),
        mode="v12",
        allowed_routes=allowed_routes,
        route_handlers=({RouteClass.ARCHIVE_READ: handler} if route_handlers is None else route_handlers),
    )
    return router, legacy_runtime


@pytest.mark.asyncio
async def test_exact_archive_shape_dispatches_to_the_registered_read_only_handler() -> None:
    plan = TurnPlan.parse(_archive_plan_payload(max_items=2))
    handler = _ArchiveHandler(selected_count=2)
    router, legacy = _router(plan, handler)

    result = await router.chat(
        "owner",
        "Обобщи последние два документа",
        **_chat_kwargs(),
    )

    assert result["message"] == "Архивный ответ [A1]"
    assert result["verified"] is True
    assert legacy.calls == []
    assert len(handler.prepared) == 1
    assert len(handler.handled) == 1
    request, turn, handled_plan = handler.handled[0]
    assert request.attachments == ()
    assert turn.attachments == ()
    assert handled_plan is plan
    assert [item.status for item in router.observations] == ["selected", "completed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "plan_changes", "chat_changes"),
    [
        (
            "attachment",
            {},
            {"attachments": [{"filename": "current.txt", "mime_type": "text/plain"}]},
        ),
        ("synthetic", {}, {"synthetic_document_notice": True}),
        (
            "quoted_attachment_reply",
            {},
            {"reply_to": "Прочитай этот файл", "quoted_attachment_reference": True},
        ),
        (
            "assistant_reply",
            {},
            {
                "reply_to": "Предыдущий ответ",
                "reply_assistant_reference": True,
                "reply_assistant_message_id": "msg_0000000000000002",
            },
        ),
        ("plain_reply", {}, {"reply_to": "Это документы Боба"}),
        ("extra_evidence", {"extra_evidence": True}, {}),
        ("tool", {"with_tool": True}, {}),
        ("table", {"output_format": "table"}, {}),
        ("non_ru", {"language": "en"}, {}),
    ],
)
async def test_unsupported_archive_shapes_fall_back_before_handler_preparation(
    case: str,
    plan_changes: dict[str, Any],
    chat_changes: dict[str, Any],
) -> None:
    del case
    plan = TurnPlan.parse(_archive_plan_payload(**plan_changes))
    handler = _ArchiveHandler()
    router, legacy = _router(plan, handler)

    result = await router.chat(
        "owner",
        "Обобщи документы",
        **_chat_kwargs(**chat_changes),
    )

    assert result["message"] == "legacy"
    assert len(legacy.calls) == 1
    assert handler.prepared == []
    assert handler.handled == []
    assert router.observations[-1].status == "legacy_fallback"
    assert all(item.status != "selected" for item in router.observations)


@pytest.mark.asyncio
async def test_archive_plan_with_insufficient_max_items_falls_back_before_preparation() -> None:
    plan = TurnPlan.parse(_archive_plan_payload(max_items=1))
    handler = _ArchiveHandler(selected_count=2)
    router, legacy = _router(plan, handler)

    result = await router.chat(
        "owner",
        "Обобщи последние два документа",
        **_chat_kwargs(),
    )

    assert result["message"] == "legacy"
    assert len(legacy.calls) == 1
    assert handler.prepared == []
    assert handler.handled == []
    assert router.observations[-1].status == "legacy_fallback"
    assert all(item.status != "selected" for item in router.observations)


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", ["not_allowed", "not_registered", "wrong_handler"])
async def test_archive_dispatch_requires_both_allowlist_and_exact_handler_mapping(
    configuration: str,
) -> None:
    plan = TurnPlan.parse(_archive_plan_payload())
    archive_handler = _ArchiveHandler()
    other_handler = _ArchiveHandler()
    if configuration == "not_allowed":
        allowed_routes = ("file_read",)
        handlers: dict[RouteClass, Any] = {RouteClass.ARCHIVE_READ: archive_handler}
    elif configuration == "not_registered":
        allowed_routes = ("archive_read",)
        handlers = {RouteClass.FILE_READ: other_handler}
    else:
        allowed_routes = ("archive_read",)
        other_handler.route = RouteClass.FILE_READ
        handlers = {RouteClass.ARCHIVE_READ: other_handler}
    router, legacy = _router(
        plan,
        archive_handler,
        allowed_routes=allowed_routes,
        route_handlers=handlers,
    )

    result = await router.chat("owner", "Найди документ", **_chat_kwargs())

    assert result["message"] == "legacy"
    assert len(legacy.calls) == 1
    assert archive_handler.prepared == []
    assert other_handler.prepared == []
    assert router.observations[-1].status == "legacy_fallback"


def test_server_filters_attested_handlers_by_configured_routes(settings, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import friday.server as server_module

    class _AttestedRuntime:
        async def attest(self, *, absolute_deadline: float) -> object:
            assert absolute_deadline > time.monotonic()
            return object()

        def public_status(self) -> dict[str, object]:
            return {
                "status": "canary_ready",
                "reason_code": "live_attestation_clear",
                "attestation_sha256": "a" * 64,
            }

    runtime = _AttestedRuntime()
    monkeypatch.setattr(server_module, "AttestedV12ModelRuntime", _AttestedRuntime)
    monkeypatch.setattr(
        server_module,
        "create_attested_v12_model_runtime",
        lambda _llm: runtime,
    )

    for configured, expected in (
        (("file_read",), ["file_read"]),
        (("file_read", "archive_read"), ["archive_read", "file_read"]),
    ):
        app = server_module.create_app(
            replace(
                settings,
                router_mode="canary",
                router_canary_routes=configured,
                router_canary_user_ids=("owner",),
            )
        )
        with TestClient(app) as client:
            assert type(app.state.agent) is OrchestrationRouter
            registered = sorted(route.value for route in app.state.agent._route_handlers)  # noqa: SLF001
            assert registered == expected
            health = client.get("/health").json()["orchestration"]
            assert health["registered_routes"] == expected
