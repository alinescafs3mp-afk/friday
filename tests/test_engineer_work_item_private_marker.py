from __future__ import annotations

import json
from typing import Any

import pytest

from friday.execution_kernel import ExecutionKernel, ToolResult, ToolSpec
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
from friday.permissions import AuthorizationService


def _continuation() -> EngineerContinuationState:
    return EngineerContinuationState(
        work_item_id="ewi_" + "1" * 32,
        owner_id="private-marker-owner",
        tenant_id="private-marker-owner",
        conversation_id="conv_" + "2" * 16,
        channel=EngineerWorkItemChannel.TELEGRAM,
        state=EngineerWorkItemState.WAITING_FOR_INPUT,
        transition=EngineerWorkItemTransition.TERMINAL_OBSERVED,
        revision=3,
        step_ordinal=1,
        step_state=EngineerWorkItemStepState.SETTLED,
        source_binding_sha256="3" * 64,
        idempotency_key="ecmd-" + "4" * 64,
        command_digest="5" * 64,
        job_receipt_sha256="6" * 64,
        terminal_receipt_sha256="7" * 64,
        ledger_disposition=EngineerCommandLedgerDisposition.EXACT,
        command_job_id="8" * 32,
        command_status=CommandStatus.COMPLETED,
    )


def _ledger_observation() -> EngineerCommandLedgerObservation:
    return EngineerCommandLedgerObservation(
        owner_id="private-marker-owner",
        tenant_id="private-marker-owner",
        conversation_id="conv_" + "2" * 16,
        job_id="8" * 32,
        status=CommandStatus.COMPLETED,
    )


def _kernel_actor(settings: Any, storage: Any) -> tuple[ExecutionKernel, Any]:
    storage.ensure_user("private-marker-owner", preset_key="owner")
    authorization = AuthorizationService(storage)
    kernel = ExecutionKernel(authorization, settings)

    async def unavailable(*, actor: Any, **_arguments: Any) -> dict[str, Any]:
        del actor
        raise AssertionError("test handler was not installed")

    for name, security_id, risk in (
        ("engineer_command_run", "knowledge.create", "mutate"),
        ("engineer_command_status", "knowledge.read", "observe"),
        ("engineer_command_cancel", "knowledge.create", "mutate"),
    ):
        kernel.register(
            ToolSpec(
                name=name,
                description="Synthetic Engineer private-carrier boundary.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                security_id=security_id,
                risk=risk,
                handler=unavailable,
            )
        )
    return kernel, authorization.actor_for_user(
        "private-marker-owner",
        source="private-marker-test",
    )


@pytest.mark.parametrize(
    "tool_name",
    ["engineer_command_run", "engineer_command_status", "engineer_command_cancel"],
)
@pytest.mark.parametrize("ok", [True, False])
async def test_engineer_continuation_is_carried_only_out_of_band(
    settings: Any,
    storage: Any,
    tool_name: str,
    ok: bool,
) -> None:
    marker = _continuation()
    kernel, actor = _kernel_actor(settings, storage)

    async def handler(*, actor: Any, **_arguments: Any) -> dict[str, Any]:
        del actor
        return {
            "ok": ok,
            "status": "completed" if ok else "failed",
            "error_code": "synthetic_refusal",
            "summary": "bounded public summary",
            "_engineer_work_item_continuation": marker,
        }

    kernel._tools[tool_name].handler = handler  # noqa: SLF001 - exact kernel boundary test
    result = await kernel.execute(tool_name, {}, actor=actor)

    assert result.success is ok
    assert result.engineer_work_item_continuation is marker
    assert isinstance(result.data, dict)
    assert "_engineer_work_item_continuation" not in result.data
    public = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
    model = result.to_llm_message()
    assert marker.work_item_id not in public
    assert marker.work_item_id not in model
    assert "_engineer_work_item_continuation" not in public
    assert "_engineer_work_item_continuation" not in model


class _ForgedContinuation(EngineerContinuationState):
    pass


def _forged_continuation() -> _ForgedContinuation:
    marker = _continuation()
    return _ForgedContinuation(
        work_item_id=marker.work_item_id,
        owner_id=marker.owner_id,
        tenant_id=marker.tenant_id,
        conversation_id=marker.conversation_id,
        channel=marker.channel,
        state=marker.state,
        transition=marker.transition,
        revision=marker.revision,
        step_ordinal=marker.step_ordinal,
        step_state=marker.step_state,
        source_binding_sha256=marker.source_binding_sha256,
        idempotency_key=marker.idempotency_key,
        command_digest=marker.command_digest,
        job_receipt_sha256=marker.job_receipt_sha256,
        terminal_receipt_sha256=marker.terminal_receipt_sha256,
        ledger_disposition=marker.ledger_disposition,
        command_job_id=marker.command_job_id,
        command_status=marker.command_status,
    )


@pytest.mark.parametrize("malformed", [None, {}, _forged_continuation()])
async def test_engineer_continuation_rejects_every_non_exact_carrier(
    settings: Any,
    storage: Any,
    malformed: object,
) -> None:
    kernel, actor = _kernel_actor(settings, storage)

    async def handler(*, actor: Any, **_arguments: Any) -> dict[str, Any]:
        del actor
        return {
            "ok": True,
            "private_body": "MUST_NOT_CROSS_THE_BOUNDARY",
            "_engineer_work_item_continuation": malformed,
        }

    kernel._tools["engineer_command_status"].handler = handler  # noqa: SLF001
    result = await kernel.execute("engineer_command_status", {}, actor=actor)

    assert result.success is False
    assert result.data is None
    assert result.handler_entered is True
    assert result.engineer_work_item_continuation is None
    assert "MUST_NOT_CROSS_THE_BOUNDARY" not in json.dumps(result.to_dict())
    assert "MUST_NOT_CROSS_THE_BOUNDARY" not in result.to_llm_message()


async def test_non_engineer_handler_cannot_forge_the_private_carrier(
    settings: Any,
    storage: Any,
) -> None:
    kernel, actor = _kernel_actor(settings, storage)

    async def handler(*, actor: Any, **_arguments: Any) -> dict[str, Any]:
        del actor
        return {
            "ok": True,
            "_engineer_work_item_continuation": _continuation(),
        }

    kernel._tools["list_tags"].handler = handler  # noqa: SLF001
    result = await kernel.execute("list_tags", {}, actor=actor)

    assert result.success is False
    assert result.engineer_work_item_continuation is None
    assert "private continuation validation" in result.error


def test_tool_result_never_serializes_a_direct_private_carrier() -> None:
    marker = _continuation()
    result = ToolResult(
        "engineer_command_status",
        True,
        data={"ok": True},
        engineer_work_item_continuation=marker,
    )

    assert marker.work_item_id not in json.dumps(result.to_dict(), sort_keys=True)
    assert marker.work_item_id not in result.to_llm_message()


@pytest.mark.parametrize("tool_name", ["engineer_command_status", "engineer_command_cancel"])
async def test_historical_ledger_observation_is_carried_only_out_of_band(
    settings: Any,
    storage: Any,
    tool_name: str,
) -> None:
    observation = _ledger_observation()
    kernel, actor = _kernel_actor(settings, storage)

    async def handler(*, actor: Any, **_arguments: Any) -> dict[str, Any]:
        del actor
        return {
            "ok": True,
            "job_id": observation.job_id,
            "status": observation.status.value,
            "summary": "bounded public summary",
            "_engineer_command_ledger_observation": observation,
        }

    kernel._tools[tool_name].handler = handler  # noqa: SLF001 - exact kernel boundary test
    result = await kernel.execute(tool_name, {}, actor=actor)

    assert result.success is True
    assert result.engineer_command_ledger_observation is observation
    assert result.engineer_work_item_continuation is None
    assert isinstance(result.data, dict)
    assert "_engineer_command_ledger_observation" not in result.data
    public = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
    model = result.to_llm_message()
    assert observation.conversation_id not in public
    assert observation.conversation_id not in model
    assert "_engineer_command_ledger_observation" not in public
    assert "_engineer_command_ledger_observation" not in model


class _ForgedLedgerObservation(EngineerCommandLedgerObservation):
    pass


def _forged_ledger_observation() -> _ForgedLedgerObservation:
    observation = _ledger_observation()
    return _ForgedLedgerObservation(
        owner_id=observation.owner_id,
        tenant_id=observation.tenant_id,
        conversation_id=observation.conversation_id,
        job_id=observation.job_id,
        status=observation.status,
    )


@pytest.mark.parametrize("malformed", [None, {}, _forged_ledger_observation()])
async def test_historical_ledger_observation_rejects_every_non_exact_carrier(
    settings: Any,
    storage: Any,
    malformed: object,
) -> None:
    kernel, actor = _kernel_actor(settings, storage)

    async def handler(*, actor: Any, **_arguments: Any) -> dict[str, Any]:
        del actor
        return {
            "ok": True,
            "private_body": "HISTORICAL_LEDGER_PRIVATE_CANARY",
            "_engineer_command_ledger_observation": malformed,
        }

    kernel._tools["engineer_command_status"].handler = handler  # noqa: SLF001
    result = await kernel.execute("engineer_command_status", {}, actor=actor)

    assert result.success is False
    assert result.data is None
    assert result.handler_entered is True
    assert result.engineer_command_ledger_observation is None
    assert "HISTORICAL_LEDGER_PRIVATE_CANARY" not in json.dumps(result.to_dict())
    assert "HISTORICAL_LEDGER_PRIVATE_CANARY" not in result.to_llm_message()


@pytest.mark.parametrize("tool_name", ["engineer_command_run", "list_tags"])
async def test_only_status_and_cancel_may_emit_a_historical_ledger_observation(
    settings: Any,
    storage: Any,
    tool_name: str,
) -> None:
    kernel, actor = _kernel_actor(settings, storage)

    async def handler(*, actor: Any, **_arguments: Any) -> dict[str, Any]:
        del actor
        return {
            "ok": True,
            "_engineer_command_ledger_observation": _ledger_observation(),
        }

    kernel._tools[tool_name].handler = handler  # noqa: SLF001
    result = await kernel.execute(tool_name, {}, actor=actor)

    assert result.success is False
    assert result.engineer_command_ledger_observation is None
    assert "private ledger validation" in result.error


async def test_kernel_rejects_conflicting_engineer_private_carriers(
    settings: Any,
    storage: Any,
) -> None:
    kernel, actor = _kernel_actor(settings, storage)

    async def handler(*, actor: Any, **_arguments: Any) -> dict[str, Any]:
        del actor
        return {
            "ok": True,
            "private_body": "CONFLICTING_ENGINEER_CARRIER_CANARY",
            "_engineer_work_item_continuation": _continuation(),
            "_engineer_command_ledger_observation": _ledger_observation(),
        }

    kernel._tools["engineer_command_status"].handler = handler  # noqa: SLF001
    result = await kernel.execute("engineer_command_status", {}, actor=actor)

    assert result.success is False
    assert result.data is None
    assert result.engineer_work_item_continuation is None
    assert result.engineer_command_ledger_observation is None
    assert "conflicting private authority" in result.error
    assert "CONFLICTING_ENGINEER_CARRIER_CANARY" not in json.dumps(result.to_dict())
    assert "CONFLICTING_ENGINEER_CARRIER_CANARY" not in result.to_llm_message()


def test_tool_result_never_serializes_a_direct_historical_ledger_observation() -> None:
    observation = _ledger_observation()
    result = ToolResult(
        "engineer_command_status",
        True,
        data={"ok": True},
        engineer_command_ledger_observation=observation,
    )

    public = json.dumps(result.to_dict(), sort_keys=True)
    model = result.to_llm_message()
    assert observation.conversation_id not in public
    assert observation.conversation_id not in model
    assert "engineer_command_ledger_observation" not in public
    assert "engineer_command_ledger_observation" not in model
