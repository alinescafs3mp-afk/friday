from __future__ import annotations

import math
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from friday.orchestration.supervisor_assist_controller import (
    AssistPendingGraphDisposition,
    SupervisorAssistOutcome,
    SupervisorAssistResult,
)
from friday.orchestration.supervisor_assist_ingress import (
    SupervisorAssistIngressBindingV1,
    SupervisorAssistPendingDecision,
    SupervisorAssistPendingRelation,
)
from friday.orchestration.supervisor_assist_runtime import (
    SemanticSupervisorAssistRuntime,
    SupervisorAssistRuntimeError,
    _future_deadline,
)
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import ActorContext

_PERSON = "assist-runtime-person"
_CONVERSATION = "conv_0123456789abcdef"
_ROOT_INGRESS = SupervisorAssistIngressBindingV1.from_claimed_request(
    source_ref="assist-runtime:root",
    request_fingerprint_sha256="a" * 64,
)
_NEW_INGRESS = SupervisorAssistIngressBindingV1.from_claimed_request(
    source_ref="assist-runtime:new",
    request_fingerprint_sha256="b" * 64,
)


def _assist_decision(
    relation: SupervisorAssistPendingRelation,
) -> SupervisorAssistPendingDecision:
    current = _ROOT_INGRESS if relation is SupervisorAssistPendingRelation.ROOT_REPLAY else _NEW_INGRESS
    return SupervisorAssistPendingDecision.for_graph(
        relation=relation,
        pending=PendingDurableTurnAdmission.owned(
            person_id=_PERSON,
            conversation_id=_CONVERSATION,
            work_graph_id="graph_0123456789abcdef",
            revision=3,
        ),
        root_request_binding_sha256=_ROOT_INGRESS.canonical_sha256(),
        current=current,
    )


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
        self.assist_pending: object = False
        self.execute_mode = SupervisorAssistOutcome.LEGACY
        self.execute_calls = 0
        self.cancel_calls = 0
        self.cancel_result: SupervisorAssistResult | None = None
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

    def classify_supervisor_assist_pending(self, *_args: Any, **_kwargs: Any) -> object:
        return self.assist_pending

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
        return self.cancel_result

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
async def test_ordinary_turn_calls_primary_once_and_observes_only_after_commit() -> None:
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
        telegram_update_id="update-0123456789",
    )

    assert actual is response
    assert primary.calls == 1
    assert controller.execute_calls == 0
    assert controller.surface is None
    assert primary.kwargs is not None
    assert primary.kwargs["telegram_update_id"] == "update-0123456789"
    assert observed == [(response, _actor())]
    assert runtime.semantic_supervisor_status()["ordinary_event_success_total"] == 1


@pytest.mark.asyncio
async def test_contextless_fresh_candidate_stays_primary_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.orchestration.supervisor_assist_runtime as module

    def forbidden_surface(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("contextless request reached authenticated surface admission")

    monkeypatch.setattr(
        module.supervisor_assist_surface_module,
        "prepare_authenticated_current_file_web_assist_surface",
        forbidden_surface,
        raising=False,
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

    assert result["message"] == "must not run"
    assert controller.surface is None
    assert controller.execute_calls == 0
    assert primary.calls == 1
    assert observed == 1


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

    monkeypatch.setattr(
        module.supervisor_assist_surface_module,
        "prepare_authenticated_current_file_web_assist_surface",
        forbidden_surface,
        raising=False,
    )
    primary = _Primary(
        {
            "message": "ordinary overlap",
            "conversation_id": _CONVERSATION,
            "message_id": "msg_0123456789abcdef",
        }
    )
    controller = _Controller()
    controller.assist_pending = _assist_decision(SupervisorAssistPendingRelation.NEW_TURN)
    controller.reconcile_disposition = disposition
    runtime = _runtime(primary, controller)

    result = await runtime.chat(
        _PERSON,
        "ещё один вопрос",
        actor=_actor(),
        conversation_id=_CONVERSATION,
        _semantic_supervisor_ingress_binding=_NEW_INGRESS,
        _semantic_supervisor_pending_decision=controller.assist_pending,  # type: ignore[arg-type]
    )

    assert result["message"] == "ordinary overlap"
    assert controller.execute_calls == 0
    assert controller.reconcile_calls == 1
    assert primary.calls == 1
    assert primary.kwargs is not None
    assert primary.kwargs["_pending_durable_admission"] is None


@pytest.mark.asyncio
async def test_root_replay_never_crosses_legacy_or_controller_execution() -> None:
    primary = _Primary({"message": "must not run"})
    controller = _Controller()
    decision = _assist_decision(SupervisorAssistPendingRelation.ROOT_REPLAY)
    runtime = _runtime(primary, controller)

    with pytest.raises(SupervisorAssistRuntimeError, match="root replay"):
        await runtime.chat(
            _PERSON,
            "исходный запрос",
            actor=_actor(),
            conversation_id=_CONVERSATION,
            _semantic_supervisor_ingress_binding=_ROOT_INGRESS,
            _semantic_supervisor_pending_decision=decision,
        )

    assert primary.calls == controller.execute_calls == 0
    assert controller.cancel_calls == controller.reconcile_calls == 0


@pytest.mark.asyncio
async def test_explicit_cancel_uses_exact_decision_without_legacy() -> None:
    response = {"message": "cancelled", "conversation_id": _CONVERSATION}
    primary = _Primary({"message": "must not run"})
    controller = _Controller()
    decision = _assist_decision(SupervisorAssistPendingRelation.EXPLICIT_CANCEL)
    controller.cancel_result = SupervisorAssistResult(
        outcome=SupervisorAssistOutcome.CANCELLED,
        response=response,
    )
    runtime = _runtime(primary, controller)

    result = await runtime.chat(
        _PERSON,
        " Отмена ",
        actor=_actor(),
        conversation_id=_CONVERSATION,
        _semantic_supervisor_ingress_binding=_NEW_INGRESS,
        _semantic_supervisor_pending_decision=decision,
    )

    assert result is response
    assert controller.cancel_calls == 1
    assert primary.calls == controller.execute_calls == controller.reconcile_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relation", "message"),
    (
        (SupervisorAssistPendingRelation.EXPLICIT_CANCEL, "обычный вопрос"),
        (SupervisorAssistPendingRelation.NEW_TURN, "cancel"),
    ),
)
async def test_carried_relation_that_disagrees_with_message_fails_closed(
    relation: SupervisorAssistPendingRelation,
    message: str,
) -> None:
    primary = _Primary({"message": "must not run"})
    controller = _Controller()
    runtime = _runtime(primary, controller)

    with pytest.raises(SupervisorAssistRuntimeError, match="carried assist ingress decision"):
        await runtime.chat(
            _PERSON,
            message,
            actor=_actor(),
            conversation_id=_CONVERSATION,
            _semantic_supervisor_ingress_binding=_NEW_INGRESS,
            _semantic_supervisor_pending_decision=_assist_decision(relation),
        )

    assert primary.calls == controller.execute_calls == 0
    assert controller.cancel_calls == controller.reconcile_calls == 0


@pytest.mark.asyncio
async def test_pending_reconciliation_uncertainty_never_calls_legacy() -> None:
    primary = _Primary({"message": "must not run"})
    controller = _Controller()
    controller.assist_pending = _assist_decision(SupervisorAssistPendingRelation.NEW_TURN)
    controller.reconcile_disposition = AssistPendingGraphDisposition.UNCERTAIN
    runtime = _runtime(primary, controller)

    with pytest.raises(SupervisorAssistRuntimeError, match="reconciliation is uncertain"):
        await runtime.chat(
            _PERSON,
            "новый ход",
            actor=_actor(),
            conversation_id=_CONVERSATION,
            _semantic_supervisor_ingress_binding=_NEW_INGRESS,
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

    monkeypatch.setattr(
        module.supervisor_assist_surface_module,
        "prepare_authenticated_current_file_web_assist_surface",
        lambda *_a, **_k: None,
        raising=False,
    )
    primary = _Primary(
        {
            "message": "legacy",
            "conversation_id": _CONVERSATION,
            "message_id": "msg_0123456789abcdef",
        }
    )
    controller = _Controller()
    controller.assist_pending = SupervisorAssistPendingDecision.uncertain(
        person_id=_PERSON,
        conversation_id=_CONVERSATION,
        current=_NEW_INGRESS,
    )
    runtime = _runtime(primary, controller)
    with pytest.raises(SupervisorAssistRuntimeError, match="ownership is uncertain"):
        await runtime.chat(
            _PERSON,
            "request",
            actor=_actor(),
            conversation_id=_CONVERSATION,
            _semantic_supervisor_ingress_binding=_NEW_INGRESS,
        )
    assert primary.calls == controller.execute_calls == 0

    controller.assist_pending = False

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


def test_promoted_journey_deadline_inherits_authenticated_call_budget() -> None:
    settings = SimpleNamespace(semantic_supervisor_timeout_sec=12.0)
    inherited = time.monotonic() + 720.0
    deadline = _future_deadline(settings, inherited)
    assert deadline == inherited
    assert deadline - time.monotonic() > 700.0


def test_promoted_journey_deadline_falls_back_to_supervisor_timeout() -> None:
    settings = SimpleNamespace(semantic_supervisor_timeout_sec=12.0)
    before = time.monotonic()
    deadline = _future_deadline(settings, None)
    after = time.monotonic()
    assert before + 12.0 <= deadline <= after + 12.0
    assert math.isfinite(deadline)


def test_exhausted_inherited_deadline_stays_finite_now() -> None:
    settings = SimpleNamespace(semantic_supervisor_timeout_sec=12.0)
    before = time.monotonic()
    deadline = _future_deadline(settings, before - 5.0)
    after = time.monotonic()
    assert before <= deadline <= after
