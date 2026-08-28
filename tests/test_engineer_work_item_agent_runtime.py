from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime, _engineer_cancel_requested
from friday.execution_kernel import ToolResult
from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItemChannel,
    EngineerWorkItemState,
    EngineerWorkItemStepState,
    EngineerWorkItemTransition,
)
from friday.orchestration.engineer_work_item_coordinator import (
    EngineerCommandLedgerDisposition,
    EngineerCommandLedgerObservation,
    EngineerContinuationState,
)
from friday.organs.engineer.command.contracts import CommandStatus
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext


def _schema(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
    }


def _context() -> AgentContext:
    return AgentContext(
        conversation_id="conv_0123456789abcdef",
        user_id=LEGACY_OWNER_USER_ID,
        person_id=LEGACY_OWNER_USER_ID,
        interaction_mode="engineer",
        source_search_lineage_user_message_id="msg_0123456789abcdef",
        effect_root_user_message_id="msg_fedcba9876543210",
        engineer_command_telegram_update_id="123456",
    )


def _marker(
    step_state: EngineerWorkItemStepState,
    status: CommandStatus,
    *,
    ordinal: int = 1,
) -> EngineerContinuationState:
    terminal = step_state is EngineerWorkItemStepState.SETTLED
    unknown = step_state is EngineerWorkItemStepState.UNKNOWN
    return EngineerContinuationState(
        work_item_id="ewi_" + "1" * 32,
        owner_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id="conv_0123456789abcdef",
        channel=EngineerWorkItemChannel.TELEGRAM,
        state=(
            EngineerWorkItemState.WAITING_FOR_INPUT
            if terminal
            else EngineerWorkItemState.UNCERTAIN
            if unknown
            else EngineerWorkItemState.WAITING_FOR_CAPABILITY
        ),
        transition=(
            EngineerWorkItemTransition.TERMINAL_OBSERVED
            if terminal
            else EngineerWorkItemTransition.COMMAND_UNKNOWN
            if unknown
            else EngineerWorkItemTransition.COMMAND_ADMITTED
        ),
        revision=ordinal + 1,
        step_ordinal=ordinal,
        step_state=step_state,
        source_binding_sha256=f"{ordinal + 1:x}" * 64,
        idempotency_key="ecmd-" + f"{ordinal + 2:x}" * 64,
        command_digest=f"{ordinal + 3:x}" * 64,
        job_receipt_sha256=f"{ordinal + 4:x}" * 64,
        terminal_receipt_sha256=(f"{ordinal + 5:x}" * 64 if terminal else ""),
        ledger_disposition=EngineerCommandLedgerDisposition.EXACT,
        command_job_id=f"{ordinal + 6:x}" * 32,
        command_status=status,
    )


def _historical_observation(
    status: CommandStatus = CommandStatus.RUNNING,
) -> EngineerCommandLedgerObservation:
    return EngineerCommandLedgerObservation(
        owner_id=LEGACY_OWNER_USER_ID,
        tenant_id=LEGACY_OWNER_USER_ID,
        conversation_id="conv_0123456789abcdef",
        job_id="9" * 32,
        status=status,
    )


def _prepared_marker() -> EngineerContinuationState:
    admitted = _marker(EngineerWorkItemStepState.ADMITTED, CommandStatus.ADMITTED)
    return replace(
        admitted,
        state=EngineerWorkItemState.ACTIVE,
        transition=EngineerWorkItemTransition.CREATED,
        revision=1,
        step_state=EngineerWorkItemStepState.PREPARED,
        job_receipt_sha256="",
        terminal_receipt_sha256="",
        ledger_disposition=EngineerCommandLedgerDisposition.ABSENT,
        command_job_id=None,
        command_status=None,
    )


class _NeverModel:
    enabled = True
    total_budget_sec = 0.1
    calls = 0

    async def chat(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls += 1
        raise AssertionError("model must not run while durable work is active")


class _OneAnswerModel:
    enabled = True
    total_budget_sec = 0.1

    def __init__(self, *, next_command: bool = False) -> None:
        self.calls = 0
        self.messages: list[dict[str, object]] = []
        self.next_command = next_command

    async def chat(self, messages, **_kwargs):  # noqa: ANN001, ANN201
        self.calls += 1
        self.messages = [dict(item) for item in messages]
        if self.next_command:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "next-step",
                        "function": {
                            "name": "engineer_command_run",
                            "arguments": json.dumps({"command": "printf next"}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        return {"content": "Итог по проверенному результату.", "finish_reason": "stop"}


class _CommandThenAnswerModel:
    enabled = True
    total_budget_sec = 0.1

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, _messages, **_kwargs):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.calls == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "rolled-back-step",
                        "function": {
                            "name": "engineer_command_run",
                            "arguments": json.dumps({"command": "printf next"}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
            }
        return {"content": "Шаг не запущен; предыдущий результат сохранён.", "finish_reason": "stop"}


class _WorkItemKernel:
    def __init__(
        self,
        resumed: ToolResult,
        *,
        command_result: ToolResult | None = None,
    ) -> None:
        self.resumed = resumed
        self.command_result = command_result
        self.executions: list[tuple[str, dict[str, object]]] = []
        self.hidden = SimpleNamespace(
            name="engineer_work_item_resume",
            model_visible=False,
            handler=lambda: None,
            risk="mutate",
            timeout_sec=None,
        )
        self.command = SimpleNamespace(
            name="engineer_command_run",
            model_visible=True,
            handler=lambda: None,
            risk="mutate",
            timeout_sec=None,
        )

    def get_tool(self, name: str):  # noqa: ANN201
        if name == "engineer_work_item_resume":
            return self.hidden
        if name == "engineer_command_run":
            return self.command
        return None

    @staticmethod
    def tool_is_approval_free(_name: str) -> bool:
        return True

    async def execute(self, name, arguments, *, actor):  # noqa: ANN001, ANN201, ARG002
        self.executions.append((name, dict(arguments)))
        if name == "engineer_work_item_resume":
            return self.resumed
        assert self.command_result is not None
        return self.command_result


def _result(marker: EngineerContinuationState) -> ToolResult:
    return ToolResult(
        "engineer_work_item_resume",
        True,
        data={
            "active": True,
            "job_id": marker.command_job_id,
            "ok": True,
            "status": marker.command_status.value,
            "stdout": "verified output" if marker.step_state is EngineerWorkItemStepState.SETTLED else "",
        },
        engineer_work_item_continuation=marker,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step_state", "status"),
    [
        (EngineerWorkItemStepState.ADMITTED, CommandStatus.RUNNING),
        (EngineerWorkItemStepState.UNKNOWN, CommandStatus.UNKNOWN),
    ],
)
async def test_active_work_item_never_replays_or_calls_model(
    settings,
    storage,
    monkeypatch,
    step_state: EngineerWorkItemStepState,
    status: CommandStatus,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    marker = _marker(step_state, status)
    model = _NeverModel()
    kernel = _WorkItemKernel(_result(marker))
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _cap: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Как там работа?",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert model.calls == 0
    assert [name for name, _args in kernel.executions] == ["engineer_work_item_resume"]
    assert "повторно" in response["content"].casefold()


@pytest.mark.asyncio
async def test_cancel_followup_reaches_hidden_resume_without_model(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    marker = _marker(EngineerWorkItemStepState.ADMITTED, CommandStatus.RUNNING)
    model = _NeverModel()
    kernel = _WorkItemKernel(_result(marker))
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _cap: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Отмени текущую задачу.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert model.calls == 0
    assert kernel.executions[0][1]["_cancel_requested"] is True
    assert "отмен" in response["content"].casefold()


@pytest.mark.parametrize(
    "speech",
    (
        "Не останавливай текущую задачу.",
        "Оно остановилось?",
        "Составь план остановки сервиса.",
        'В логе написано: "stop current job".',
        "Останови сервис после завершения текущего сканирования.",
    ),
)
@pytest.mark.asyncio
async def test_non_cancel_language_only_observes_running_work_item(
    settings,
    storage,
    monkeypatch,
    speech: str,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    marker = _marker(EngineerWorkItemStepState.ADMITTED, CommandStatus.RUNNING)
    kernel = _WorkItemKernel(_result(marker))
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=_NeverModel(),  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _cap: current)

    await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        speech,
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert kernel.executions[0][1]["_cancel_requested"] is False


@pytest.mark.parametrize(
    "speech",
    (
        "Стоп.",
        "Пожалуйста, останови текущую задачу.",
        "Можешь ли отменить это сканирование?",
        "Could you please cancel the current job?",
    ),
)
def test_cancel_classifier_accepts_only_direct_current_job_requests(speech: str) -> None:
    assert _engineer_cancel_requested(speech) is True


@pytest.mark.asyncio
async def test_settled_resume_injects_one_untrusted_observation(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    marker = _marker(EngineerWorkItemStepState.SETTLED, CommandStatus.COMPLETED)
    model = _OneAnswerModel()
    kernel = _WorkItemKernel(_result(marker))
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _cap: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Продолжай и подведи итог.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert model.calls == 1
    joined = "\n".join(str(item.get("content") or "") for item in model.messages)
    assert joined.count("ENGINEER_VERIFIED_COMMAND_OBSERVATION") == 1
    assert "недоверенные данные" in joined
    assert response["content"] == "Итог по проверенному результату."


@pytest.mark.asyncio
async def test_terminal_result_can_start_exact_next_step_in_same_work_item(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    settled = _marker(EngineerWorkItemStepState.SETTLED, CommandStatus.COMPLETED)
    next_step = replace(
        _marker(EngineerWorkItemStepState.ADMITTED, CommandStatus.RUNNING, ordinal=2),
        work_item_id=settled.work_item_id,
        revision=settled.revision + 2,
    )
    command_result = ToolResult(
        "engineer_command_run",
        True,
        data={"job_id": next_step.command_job_id, "ok": True, "status": "running"},
        engineer_work_item_continuation=next_step,
    )
    model = _OneAnswerModel(next_command=True)
    kernel = _WorkItemKernel(_result(settled), command_result=command_result)
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _cap: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Продолжай следующим шагом.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert model.calls == 1
    assert [name for name, _args in kernel.executions] == [
        "engineer_work_item_resume",
        "engineer_command_run",
    ]
    assert "повторно" in response["content"].casefold()


@pytest.mark.asyncio
async def test_later_step_presubmit_refusal_rolls_back_and_replans(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    settled = _marker(EngineerWorkItemStepState.SETTLED, CommandStatus.COMPLETED)
    rolled_back = replace(
        settled,
        transition=EngineerWorkItemTransition.PREPARED_STEP_DISCARDED,
        revision=settled.revision + 2,
    )
    command_result = ToolResult(
        "engineer_command_run",
        False,
        data={
            "approval_id": "",
            "effect_boundary_crossed": False,
            "error_code": "spawn_failed",
            "job_id": "",
            "status": "failed",
            "summary": "",
        },
        error="Host control refused: spawn_failed",
        handler_entered=True,
        engineer_work_item_continuation=rolled_back,
    )
    model = _CommandThenAnswerModel()
    kernel = _WorkItemKernel(_result(settled), command_result=command_result)
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _cap: current)

    response = await runtime._agentic_loop(  # noqa: SLF001
        _context(),
        "Продолжай следующим шагом.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert model.calls == 2
    assert [name for name, _args in kernel.executions] == [
        "engineer_work_item_resume",
        "engineer_command_run",
    ]
    assert response.get("llm_failed", False) is False
    assert response["content"] == "Шаг не запущен; предыдущий результат сохранён."


@pytest.mark.asyncio
async def test_missing_marker_on_durable_resume_fails_closed_and_stale_marker_clears(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    stale = _marker(EngineerWorkItemStepState.ADMITTED, CommandStatus.RUNNING)
    missing = ToolResult(
        "engineer_work_item_resume",
        True,
        data={"active": True, "job_id": "9" * 32, "ok": True, "status": "running"},
    )
    model = _NeverModel()
    kernel = _WorkItemKernel(missing)
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True, engineer_command_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _cap: current)
    context = _context()
    context.engineer_work_item_continuation = stale

    response = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Продолжай.",
        actor,
        tools=[_schema("engineer_command_run")],
        attachments=None,
    )

    assert response["llm_failed"] is True
    assert model.calls == 0

    empty = ToolResult(
        "engineer_work_item_resume",
        True,
        data={"active": False, "ok": True},
    )
    context.engineer_work_item_continuation = stale
    assert runtime._adopt_engineer_continuation(context, empty, actor=actor) is True  # noqa: SLF001
    assert context.engineer_work_item_continuation is None


def test_private_continuation_accepts_only_reachable_revision_transitions() -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    context = _context()
    prepared = _prepared_marker()
    context.engineer_work_item_continuation = prepared
    settled = replace(
        _marker(EngineerWorkItemStepState.SETTLED, CommandStatus.COMPLETED),
        revision=prepared.revision + 2,
    )
    legal = ToolResult(
        "engineer_command_run",
        True,
        data={"job_id": settled.command_job_id, "ok": True, "status": "completed"},
        engineer_work_item_continuation=settled,
    )
    assert AgentRuntime._adopt_engineer_continuation(context, legal, actor=actor) is True  # noqa: SLF001

    skipped = replace(settled, revision=settled.revision + 4)
    invalid = replace(legal, engineer_work_item_continuation=skipped)
    assert AgentRuntime._adopt_engineer_continuation(context, invalid, actor=actor) is False  # noqa: SLF001
    assert context.engineer_work_item_continuation is settled


def test_private_continuation_rejects_illegal_phase_jump() -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    context = _context()
    prepared = _prepared_marker()
    context.engineer_work_item_continuation = prepared
    ready = replace(
        _marker(EngineerWorkItemStepState.SETTLED, CommandStatus.COMPLETED),
        state=EngineerWorkItemState.READY_TO_ANSWER,
        transition=EngineerWorkItemTransition.ANSWER_READY,
        revision=prepared.revision + 1,
    )
    result = ToolResult(
        "engineer_command_run",
        True,
        data={"job_id": ready.command_job_id, "ok": True, "status": "completed"},
        engineer_work_item_continuation=ready,
    )
    assert AgentRuntime._adopt_engineer_continuation(context, result, actor=actor) is False  # noqa: SLF001
    assert context.engineer_work_item_continuation is prepared


def test_private_continuation_accepts_exact_later_step_rollback_payloads() -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    settled = _marker(EngineerWorkItemStepState.SETTLED, CommandStatus.COMPLETED)
    rolled_back = replace(
        settled,
        transition=EngineerWorkItemTransition.PREPARED_STEP_DISCARDED,
        revision=settled.revision + 2,
    )
    refusal = ToolResult(
        "engineer_command_run",
        False,
        data={
            "effect_boundary_crossed": False,
            "error_code": "spawn_failed",
            "job_id": "",
            "status": "failed",
        },
        engineer_work_item_continuation=rolled_back,
    )
    context = _context()
    context.engineer_work_item_continuation = settled
    assert AgentRuntime._adopt_engineer_continuation(context, refusal, actor=actor) is True  # noqa: SLF001
    assert context.engineer_work_item_continuation is rolled_back

    replay = ToolResult(
        "engineer_work_item_resume",
        True,
        data={
            "job_id": rolled_back.command_job_id,
            "ok": True,
            "status": rolled_back.command_status.value,
        },
        engineer_work_item_continuation=rolled_back,
    )
    context.engineer_work_item_continuation = settled
    assert AgentRuntime._adopt_engineer_continuation(context, replay, actor=actor) is True  # noqa: SLF001

    for invalid_data in (
        None,
        {},
        {"effect_boundary_crossed": False, "error_code": "spawn_failed"},
        {"job_id": rolled_back.command_job_id, "status": rolled_back.command_status.value},
    ):
        invalid = replace(refusal, data=invalid_data)
        context.engineer_work_item_continuation = settled
        assert AgentRuntime._adopt_engineer_continuation(context, invalid, actor=actor) is False  # noqa: SLF001
        assert context.engineer_work_item_continuation is settled

    for mismatch in (
        {"effect_boundary_crossed": True},
        {"error_code": ""},
        {"job_id": "9" * 32},
        {"ok": True},
    ):
        invalid = replace(refusal, data={**refusal.data, **mismatch})
        context.engineer_work_item_continuation = settled
        assert AgentRuntime._adopt_engineer_continuation(context, invalid, actor=actor) is False  # noqa: SLF001
        assert context.engineer_work_item_continuation is settled


@pytest.mark.parametrize(
    ("tool_name", "status"),
    [
        ("engineer_command_status", CommandStatus.RUNNING),
        ("engineer_command_cancel", CommandStatus.CANCELLED),
    ],
)
def test_exact_historical_status_or_cancel_claim_is_accepted_without_an_ewi_marker(
    tool_name: str,
    status: CommandStatus,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    context = _context()
    observation = _historical_observation(status)
    result = ToolResult(
        tool_name,
        True,
        data={
            "ok": True,
            "job_id": observation.job_id,
            "status": observation.status.value,
            **({"cancel_requested": True} if tool_name == "engineer_command_cancel" else {}),
        },
        engineer_command_ledger_observation=observation,
    )

    accepted = AgentRuntime._adopt_engineer_continuation(  # noqa: SLF001
        context,
        result,
        actor=actor,
    )

    assert accepted is True
    assert context.engineer_work_item_continuation is None
    serialized = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
    rendered = result.to_llm_message()
    assert observation.conversation_id not in serialized
    assert observation.conversation_id not in rendered
    assert "engineer_command_ledger_observation" not in serialized
    assert "engineer_command_ledger_observation" not in rendered


@pytest.mark.parametrize(
    "mismatch",
    ["owner", "tenant", "conversation", "job_id", "status"],
)
def test_historical_command_claim_rejects_every_scope_or_payload_mismatch(
    mismatch: str,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    context = _context()
    observation = _historical_observation()
    data = {
        "ok": True,
        "job_id": observation.job_id,
        "status": observation.status.value,
    }
    if mismatch == "owner":
        observation = replace(observation, owner_id="another-owner")
    elif mismatch == "tenant":
        observation = replace(observation, tenant_id="another-tenant")
    elif mismatch == "conversation":
        observation = replace(observation, conversation_id="conv_fedcba9876543210")
    elif mismatch == "job_id":
        data["job_id"] = "8" * 32
    else:
        data["status"] = CommandStatus.COMPLETED.value
    result = ToolResult(
        "engineer_command_status",
        True,
        data=data,
        engineer_command_ledger_observation=observation,
    )

    assert (
        AgentRuntime._adopt_engineer_continuation(  # noqa: SLF001
            context,
            result,
            actor=actor,
        )
        is False
    )
    assert context.engineer_work_item_continuation is None


class _ForgedHistoricalObservation(EngineerCommandLedgerObservation):
    pass


@pytest.mark.parametrize("invalid", ["wrong_tool", "both_carriers", "forged_type"])
def test_historical_command_claim_rejects_wrong_tool_conflicting_carrier_or_subclass(
    invalid: str,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    context = _context()
    observation = _historical_observation()
    carrier: EngineerCommandLedgerObservation = observation
    tool_name = "engineer_command_status"
    continuation = None
    if invalid == "wrong_tool":
        tool_name = "engineer_command_run"
    elif invalid == "both_carriers":
        continuation = _marker(EngineerWorkItemStepState.SETTLED, CommandStatus.COMPLETED)
    else:
        carrier = _ForgedHistoricalObservation(
            owner_id=observation.owner_id,
            tenant_id=observation.tenant_id,
            conversation_id=observation.conversation_id,
            job_id=observation.job_id,
            status=observation.status,
        )
    result = ToolResult(
        tool_name,
        True,
        data={
            "ok": True,
            "job_id": observation.job_id,
            "status": observation.status.value,
        },
        engineer_work_item_continuation=continuation,
        engineer_command_ledger_observation=carrier,
    )

    assert (
        AgentRuntime._adopt_engineer_continuation(  # noqa: SLF001
            context,
            result,
            actor=actor,
        )
        is False
    )
    assert context.engineer_work_item_continuation is None
