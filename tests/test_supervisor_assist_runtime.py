from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from friday.orchestration.supervisor_assist_controller import (
    AssistPendingGraphDisposition,
    SupervisorAssistOutcome,
    SupervisorAssistResult,
)
from friday.orchestration.supervisor_assist_runtime import (
    SemanticSupervisorAssistRuntime,
    SupervisorAssistRuntimeError,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext

_PERSON = "assist-runtime-person"
_CONVERSATION = "conv_0123456789abcdef"


@dataclass
class _Primary:
    response: dict[str, Any]
    pending: object = False
    calls: int = 0
    pending_calls: int = 0
    kwargs: dict[str, Any] | None = None

    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        del user_id, message
        self.calls += 1
        self.kwargs = kwargs
        return self.response

    def pending_durable_turn_admission(self, *_args: Any, **_kwargs: Any) -> object:
        self.pending_calls += 1
        return self.pending


class _Controller:
    def __init__(self) -> None:
        self.pending: object = False
        self.execute_mode = SupervisorAssistOutcome.LEGACY
        self.execute_calls = 0
        self.cancel_calls = 0
        self.reconcile_calls = 0
        self.reconcile_disposition = AssistPendingGraphDisposition.LIVE_IN_PROCESS
        self.closed = 0
        self.surface: object = None

    def semantic_supervisor_status(self) -> dict[str, object]:
        return {
            "schema": "friday.semantic-supervisor-assist-controller-status.v1",
            "effective_mode": "assist",
            "promotion_admitted": True,
            "closed": False,
        }

    def pending_durable_turn_admission(self, *_args: Any, **_kwargs: Any) -> object:
        return self.pending

    async def execute(
        self,
        surface: object,
        *,
        legacy_primary: Any,
        absolute_deadline: float,
    ) -> SupervisorAssistResult:
        assert absolute_deadline > 0
        self.execute_calls += 1
        self.surface = surface
        if self.execute_mode is SupervisorAssistOutcome.LEGACY:
            response = await legacy_primary()
            return SupervisorAssistResult(
                outcome=SupervisorAssistOutcome.LEGACY,
                response=response,
            )
        return SupervisorAssistResult(
            outcome=self.execute_mode,
            response={
                "message": "promoted",
                "conversation_id": _CONVERSATION,
                "message_id": "msg_0123456789abcdef",
            },
        )

    async def cancel_active(self, *_args: Any, **_kwargs: Any) -> SupervisorAssistResult | None:
        self.cancel_calls += 1
        return None

    async def reconcile_pending_before_legacy(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> AssistPendingGraphDisposition:
        self.reconcile_calls += 1
        return self.reconcile_disposition

    async def close(self) -> None:
        self.closed += 1


def _actor() -> ActorContext:
    return ActorContext(
        user_id=_PERSON,
        preset_key="owner",
        source="api-token",
        identity_id="token-1",
    )


def _runtime(
    primary: _Primary,
    controller: _Controller,
    *,
    observer: Any = None,
) -> SemanticSupervisorAssistRuntime:
    return SemanticSupervisorAssistRuntime(
        settings=SimpleNamespace(
            semantic_supervisor_mode="assist",
            semantic_supervisor_timeout_sec=12.0,
        ),
        primary=primary,
        controller=controller,  # type: ignore[arg-type]
        conversation_is_dialogue=lambda person, conversation: (
            person == _PERSON and conversation == _CONVERSATION
        ),
        ordinary_observer=observer,
    )


@pytest.mark.asyncio
async def test_ordinary_turn_calls_primary_once_and_observes_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.supervisor_assist_runtime as module

    monkeypatch.setattr(module, "prepare_current_file_web_assist_surface", lambda *_a, **_k: None)
    response = {
        "message": "legacy",
        "conversation_id": _CONVERSATION,
        "message_id": "msg_0123456789abcdef",
    }
    primary = _Primary(response)
    controller = _Controller()
    observed: list[tuple[object, ActorContext]] = []

    async def observer(value: object, actor: ActorContext) -> None:
        assert primary.calls == 1
        observed.append((value, actor))

    runtime = _runtime(primary, controller, observer=observer)
    actual = await runtime.chat(
        _PERSON,
        "обычный вопрос",
        actor=_actor(),
        conversation_id=_CONVERSATION,
    )

    assert actual is response
    assert primary.calls == 1
    assert controller.execute_calls == 1
    assert controller.surface is None
    assert observed == [(response, _actor())]
    assert runtime.semantic_supervisor_status()["ordinary_event_success_total"] == 1


@pytest.mark.asyncio
async def test_promoted_response_never_crosses_legacy_or_ordinary_observer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.supervisor_assist_runtime as module

    surface = object()
    monkeypatch.setattr(
        module,
        "prepare_current_file_web_assist_surface",
        lambda *_a, **_k: surface,
    )
    primary = _Primary({"message": "must not run"})
    controller = _Controller()
    controller.execute_mode = SupervisorAssistOutcome.PUBLISHED
    observed = 0

    def observer(_response: object, _actor: ActorContext) -> None:
        nonlocal observed
        observed += 1

    runtime = _runtime(primary, controller, observer=observer)
    result = await runtime.chat(
        _PERSON,
        "сравни файл с актуальными правилами в интернете",
        actor=_actor(),
        conversation_id=_CONVERSATION,
        attachments=[{"opaque": True}],
        ingestion_result={
            "promoted": False,
            "queued_for_review": False,
            "action": "transient",
            "category": "web_request",
            "reason": "explicit",
        },
    )

    assert result["message"] == "promoted"
    assert controller.surface is surface
    assert primary.calls == 0
    assert observed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disposition",
    [
        AssistPendingGraphDisposition.LIVE_IN_PROCESS,
        AssistPendingGraphDisposition.RETIRED,
    ],
)
async def test_existing_graph_bypasses_new_planning_and_does_not_bind_legacy_work_item(
    monkeypatch: pytest.MonkeyPatch,
    disposition: AssistPendingGraphDisposition,
) -> None:
    import friday.orchestration.supervisor_assist_runtime as module

    def forbidden_surface(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("surface recognition must stay behind durable ownership")

    monkeypatch.setattr(module, "prepare_current_file_web_assist_surface", forbidden_surface)
    primary = _Primary(
        {
            "message": "ordinary overlap",
            "conversation_id": _CONVERSATION,
            "message_id": "msg_0123456789abcdef",
        }
    )
    controller = _Controller()
    controller.pending = PendingDurableTurnAdmission.owned(
        person_id=_PERSON,
        conversation_id=_CONVERSATION,
        work_graph_id="graph_0123456789abcdef",
        revision=3,
    )
    controller.reconcile_disposition = disposition
    runtime = _runtime(primary, controller)

    result = await runtime.chat(
        _PERSON,
        "ещё один вопрос",
        actor=_actor(),
        conversation_id=_CONVERSATION,
        _pending_durable_admission=controller.pending,  # type: ignore[arg-type]
    )

    assert result["message"] == "ordinary overlap"
    assert controller.execute_calls == 0
    assert controller.reconcile_calls == 1
    assert primary.calls == 1
    assert primary.kwargs is not None
    assert primary.kwargs["_pending_durable_admission"] is None


@pytest.mark.asyncio
async def test_pending_reconciliation_uncertainty_never_calls_legacy() -> None:
    primary = _Primary({"message": "must not run"})
    controller = _Controller()
    controller.pending = PendingDurableTurnAdmission.owned(
        person_id=_PERSON,
        conversation_id=_CONVERSATION,
        work_graph_id="graph_0123456789abcdef",
        revision=3,
    )
    controller.reconcile_disposition = AssistPendingGraphDisposition.UNCERTAIN
    runtime = _runtime(primary, controller)

    with pytest.raises(SupervisorAssistRuntimeError, match="reconciliation is uncertain"):
        await runtime.chat(
            _PERSON,
            "новый ход",
            actor=_actor(),
            conversation_id=_CONVERSATION,
        )
    assert controller.reconcile_calls == 1
    assert primary.calls == controller.execute_calls == 0


def test_pending_graph_precedes_primary_and_uncertainty_suppresses_ingestion() -> None:
    primary = _Primary({"message": "unused"})
    controller = _Controller()
    controller.pending = PendingDurableTurnAdmission.owned(
        person_id=_PERSON,
        conversation_id=_CONVERSATION,
        work_graph_id="graph_0123456789abcdef",
        revision=3,
    )
    runtime = _runtime(primary, controller)

    assert (
        runtime.pending_durable_turn_admission(
            _PERSON,
            "continue",
            actor=_actor(),
            conversation_id=_CONVERSATION,
        )
        is controller.pending
    )
    assert primary.pending_calls == 0

    controller.pending = None
    uncertain = runtime.pending_durable_turn_admission(
        _PERSON,
        "continue",
        actor=_actor(),
        conversation_id=_CONVERSATION,
    )
    assert isinstance(uncertain, PendingDurableTurnAdmission)
    assert not uncertain.is_owned
    assert primary.pending_calls == 0


@pytest.mark.asyncio
async def test_uncertain_graph_never_falls_through_and_observer_failure_is_non_authorizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.supervisor_assist_runtime as module

    monkeypatch.setattr(module, "prepare_current_file_web_assist_surface", lambda *_a, **_k: None)
    primary = _Primary(
        {
            "message": "legacy",
            "conversation_id": _CONVERSATION,
            "message_id": "msg_0123456789abcdef",
        }
    )
    controller = _Controller()
    controller.pending = None
    runtime = _runtime(primary, controller)
    with pytest.raises(SupervisorAssistRuntimeError, match="ownership is uncertain"):
        await runtime.chat(
            _PERSON,
            "request",
            actor=_actor(),
            conversation_id=_CONVERSATION,
        )
    assert primary.calls == controller.execute_calls == 0

    controller.pending = False

    def broken_observer(_response: object, _actor: ActorContext) -> None:
        raise RuntimeError("private body must not escape")

    runtime = _runtime(primary, controller, observer=broken_observer)
    response = await runtime.chat(
        _PERSON,
        "ordinary",
        actor=_actor(),
        conversation_id=_CONVERSATION,
    )
    assert response is primary.response
    assert runtime.semantic_supervisor_status()["ordinary_event_failure_total"] == 1


@pytest.mark.asyncio
async def test_close_is_idempotent_and_does_not_close_primary() -> None:
    primary = _Primary({"message": "unused"})
    controller = _Controller()
    runtime = _runtime(primary, controller)

    await runtime.close()
    await runtime.close()

    assert controller.closed == 1
    assert runtime.semantic_supervisor_status()["effective_mode"] == "off"
    with pytest.raises(SupervisorAssistRuntimeError, match="closed"):
        await runtime.chat(
            _PERSON,
            "request",
            actor=_actor(),
            conversation_id=_CONVERSATION,
        )
