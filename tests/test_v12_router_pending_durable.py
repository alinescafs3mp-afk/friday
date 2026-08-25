from __future__ import annotations

import gc
import time
from typing import Any

import pytest

from friday.orchestration import OrchestrationRouter, TurnInput, TurnPlan
from friday.permissions import ActorContext
from friday.turn_intent_policy import TurnIntent, TurnPolicyDecision

_ACTOR = ActorContext("owner", "owner", "test")
_CONVERSATION_ID = "conversation-private-id"
_MESSAGE = "второй"


def _ordinary_plan() -> TurnPlan:
    return TurnPlan.parse(
        {
            "schema": "friday.turn-plan.v1",
            "route": "ordinary_dialogue",
            "objective": "Ответить на обычную реплику.",
            "evidence_requests": [],
            "tool_intents": [],
            "output": {
                "format": "text",
                "language": "ru",
                "require_citations": False,
                "one_message": True,
            },
            "confidence": 0.9,
            "fallback": "legacy",
            "reason_code": "ordinary_dialogue",
        }
    )


class _Planner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TurnInput, float | None]] = []

    async def plan(self, turn: TurnInput, *, turn_deadline: float | None = None) -> TurnPlan:
        self.calls.append(("shadow", turn, turn_deadline))
        return _ordinary_plan()

    async def plan_attested(
        self,
        turn: TurnInput,
        *,
        turn_deadline: float | None = None,
    ) -> TurnPlan:
        self.calls.append(("attested", turn, turn_deadline))
        return _ordinary_plan()


class _PendingRuntime:
    def __init__(self, admission: object = True, *, error: Exception | None = None) -> None:
        self.admission = admission
        self.error = error
        self.admission_calls: list[tuple[str, str, ActorContext, str | None]] = []
        self.chat_calls: list[tuple[str, str, dict[str, Any]]] = []

    def owns_pending_durable_turn(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
    ) -> object:
        self.admission_calls.append((user_id, message, actor, conversation_id))
        if self.error is not None:
            raise self.error
        return self.admission

    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        self.chat_calls.append((user_id, message, kwargs))
        return {"message": "legacy", "conversation_id": "c-1"}


class _AsyncPendingRuntime(_PendingRuntime):
    def owns_pending_durable_turn(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
    ) -> object:
        self.admission_calls.append((user_id, message, actor, conversation_id))

        async def result() -> bool:
            return True

        return result()


class _ConversationScopedPendingRuntime(_PendingRuntime):
    def __init__(self, *, person_id: str, conversation_id: str) -> None:
        super().__init__()
        self.person_id = person_id
        self.conversation_id = conversation_id

    def owns_pending_durable_turn(
        self,
        user_id: str,
        message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
    ) -> bool:
        self.admission_calls.append((user_id, message, actor, conversation_id))
        return user_id == self.person_id and conversation_id == self.conversation_id


class _RuntimeWithoutAdmission:
    def __init__(self) -> None:
        self.chat_calls = 0

    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        del user_id, message, kwargs
        self.chat_calls += 1
        return {"message": "legacy", "conversation_id": "c-1"}


def _router(
    mode: str,
    runtime: _PendingRuntime | _RuntimeWithoutAdmission,
    planner: _Planner,
) -> OrchestrationRouter:
    return OrchestrationRouter(
        runtime,
        planner,
        mode=mode,
        canary_user_ids=("owner",),
    )


def _chat_kwargs() -> dict[str, Any]:
    return {
        "actor": _ACTOR,
        "conversation_id": _CONVERSATION_ID,
        "turn_deadline": time.monotonic() + 60,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "canary", "v12"])
async def test_pending_durable_turn_bypasses_every_planner_mode(mode: str) -> None:
    runtime = _PendingRuntime(True)
    planner = _Planner()
    router = _router(mode, runtime, planner)

    result = await router.chat("tenant-user", _MESSAGE, **_chat_kwargs())
    await router.drain_shadow()

    assert result == {"message": "legacy", "conversation_id": "c-1"}
    assert runtime.admission_calls == [("tenant-user", _MESSAGE, _ACTOR, _CONVERSATION_ID)]
    assert len(runtime.chat_calls) == 1
    assert planner.calls == []
    assert router.observations[-1].status == "durable_turn_owned"
    assert router.observations[-1].selected_runtime == "legacy"
    assert router.observations[-1].plan_sha256 == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "canary", "v12"])
async def test_exact_false_admission_retains_ordinary_routing(mode: str) -> None:
    runtime = _PendingRuntime(False)
    planner = _Planner()
    router = _router(mode, runtime, planner)

    result = await router.chat("private-tenant-user", _MESSAGE, **_chat_kwargs())
    await router.drain_shadow()

    assert result["message"] == "legacy"
    assert len(runtime.admission_calls) == 1
    assert len(runtime.chat_calls) == 1
    assert len(planner.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shadow", "canary", "v12"])
@pytest.mark.parametrize(
    ("admission", "error"),
    [
        (1, None),
        (None, None),
        (True, RuntimeError("private lookup failure")),
    ],
)
async def test_invalid_or_failed_admission_fails_closed_to_legacy(
    mode: str,
    admission: object,
    error: Exception | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _PendingRuntime(admission, error=error)
    planner = _Planner()
    router = _router(mode, runtime, planner)

    result = await router.chat("private-tenant-user", _MESSAGE, **_chat_kwargs())
    await router.drain_shadow()

    assert result["message"] == "legacy"
    assert len(runtime.admission_calls) == 1
    assert len(runtime.chat_calls) == 1
    assert planner.calls == []
    assert router.observations[-1].status == "durable_turn_ownership_uncertain"
    assert "private lookup failure" not in caplog.text
    assert "private-tenant-user" not in caplog.text
    assert _MESSAGE not in caplog.text


@pytest.mark.asyncio
async def test_awaitable_admission_is_closed_and_fails_closed_to_legacy(
    recwarn: pytest.WarningsRecorder,
) -> None:
    runtime = _AsyncPendingRuntime()
    planner = _Planner()
    router = _router("v12", runtime, planner)

    result = await router.chat("tenant-user", _MESSAGE, **_chat_kwargs())
    gc.collect()

    assert result["message"] == "legacy"
    assert len(runtime.admission_calls) == 1
    assert planner.calls == []
    assert router.observations[-1].status == "durable_turn_ownership_uncertain"
    assert not [warning for warning in recwarn if "was never awaited" in str(warning.message)]


@pytest.mark.asyncio
async def test_missing_optional_admission_retains_ordinary_routing() -> None:
    runtime = _RuntimeWithoutAdmission()
    planner = _Planner()
    router = _router("v12", runtime, planner)

    result = await router.chat("tenant-user", _MESSAGE, **_chat_kwargs())

    assert result["message"] == "legacy"
    assert runtime.chat_calls == 1
    assert len(planner.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"attachments": [{"mime_type": "text/plain"}]},
        {"ingestion_result": {}},
        {"synthetic_document_notice": True},
        {"replay_source_message_id": "msg_0000000000000001"},
        {"answer_with_voice": True},
        {"reply_to": "предыдущее сообщение"},
        {"quoted_attachment_reference": True},
        {"reply_assistant_reference": True},
        {"reply_assistant_message_id": "msg_0000000000000002"},
        {"turn_policy": TurnPolicyDecision(intent=TurnIntent.PASSTHROUGH)},
        {"enable_tools": False},
        {"mode": "dialogue"},
        {"mode": "research"},
    ],
)
async def test_effect_bearing_or_contextual_surface_skips_admission(
    override: dict[str, Any],
) -> None:
    runtime = _PendingRuntime(True)
    planner = _Planner()
    router = _router("shadow", runtime, planner)
    kwargs = _chat_kwargs()
    kwargs.update(override)

    result = await router.chat("tenant-user", _MESSAGE, **kwargs)
    await router.drain_shadow()

    assert result["message"] == "legacy"
    assert runtime.admission_calls == []
    assert len(runtime.chat_calls) == 1
    assert len(planner.calls) == 1


@pytest.mark.asyncio
async def test_nonshared_impersonation_never_reaches_admission_or_planner() -> None:
    actor = ActorContext("attacker", "user", "test")
    runtime = _PendingRuntime(True)
    planner = _Planner()
    router = _router("v12", runtime, planner)

    result = await router.chat(
        "victim",
        _MESSAGE,
        actor=actor,
        conversation_id="victim-conversation",
        turn_deadline=time.monotonic() + 60,
    )

    assert result["message"] == "legacy"
    assert runtime.admission_calls == []
    assert planner.calls == []
    assert router.observations[-1].status == "durable_turn_ownership_uncertain"


@pytest.mark.asyncio
async def test_shared_archive_admission_uses_exact_person_scope() -> None:
    actor = ActorContext(
        "shared-tenant",
        "owner",
        "test",
        shared_tenant=True,
        person_id="person-a",
    )
    runtime = _PendingRuntime(True)
    planner = _Planner()
    router = _router("v12", runtime, planner)

    result = await router.chat(
        "untrusted-transport-user",
        _MESSAGE,
        actor=actor,
        conversation_id="person-a-conversation",
        turn_deadline=time.monotonic() + 60,
    )

    assert result["message"] == "legacy"
    assert runtime.admission_calls == [
        ("person-a", _MESSAGE, actor, "person-a-conversation")
    ]
    assert planner.calls == []


@pytest.mark.asyncio
async def test_foreign_conversation_cannot_claim_pending_ownership() -> None:
    runtime = _ConversationScopedPendingRuntime(
        person_id="owner",
        conversation_id="owned-conversation",
    )
    planner = _Planner()
    router = _router("v12", runtime, planner)

    result = await router.chat(
        "owner",
        _MESSAGE,
        actor=_ACTOR,
        conversation_id="foreign-conversation",
        turn_deadline=time.monotonic() + 60,
    )

    assert result["message"] == "legacy"
    assert runtime.admission_calls == [
        ("owner", _MESSAGE, _ACTOR, "foreign-conversation")
    ]
    assert len(planner.calls) == 1
