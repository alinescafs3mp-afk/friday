from __future__ import annotations

import asyncio
import contextlib
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItemChannel,
    EngineerWorkItemState,
    EngineerWorkItemStepState,
    EngineerWorkItemTransition,
)
from friday.orchestration.engineer_work_item_coordinator import (
    EngineerCommandLedgerDisposition,
    EngineerContinuationState,
    EngineerWorkItemCoordinatorError,
)
from friday.organs import ServiceContext
from friday.organs.engineer import ENGINEER_COMMAND_MANAGE, EngineerOrgan
from friday.organs.engineer.command.contracts import CommandStatus
from friday.organs.engineer.command_tools import (
    EngineerCommandResumeObservation,
    build_engineer_command_tools,
)
from friday.permissions import AuthorizationService


def _continuation(owner_id: str = "hidden-resume-owner") -> EngineerContinuationState:
    return EngineerContinuationState(
        work_item_id="ewi_" + "1" * 32,
        owner_id=owner_id,
        tenant_id=owner_id,
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


class _ResumeService:
    def __init__(self, observation: EngineerCommandResumeObservation | None) -> None:
        self.observation = observation
        self.calls: list[tuple[object, str, bool, int]] = []

    def __bool__(self) -> bool:
        return False

    def resume_current(
        self,
        *,
        actor: object,
        conversation_id: str,
        cancel_requested: bool = False,
    ) -> object:
        self.calls.append((actor, conversation_id, cancel_requested, threading.get_ident()))
        return self.observation


def _context() -> ServiceContext:
    return ServiceContext(
        settings=cast(Any, SimpleNamespace(engineer_command_enabled=True)),
        storage=None,
        kg=None,
        ingestion=None,
    )


@pytest.mark.asyncio
async def test_hidden_resume_is_threaded_bounded_and_never_model_visible() -> None:
    marker = _continuation()
    service = _ResumeService(
        EngineerCommandResumeObservation(
            continuation=marker,
            payload={"ok": True, "status": "completed", "summary": "bounded"},
        )
    )
    tools = {
        item.name: item
        for item in build_engineer_command_tools(_context(), service=service)  # type: ignore[arg-type]
    }
    tool = tools["engineer_work_item_resume"]

    assert tool.model_visible is False
    assert tool.security_id == "engineer.command.manage"
    assert tool.risk == "mutate"
    assert tool.parameters == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert tool.handler is not None
    event_loop_thread = threading.get_ident()
    result = await tool.handler(actor="owner", _conversation_id=marker.conversation_id)

    assert result == {
        "active": True,
        "ok": True,
        "status": "completed",
        "summary": "bounded",
        "_engineer_work_item_continuation": marker,
    }
    assert service.calls == [("owner", marker.conversation_id, False, service.calls[0][3])]
    assert service.calls[0][3] != event_loop_thread


@pytest.mark.asyncio
async def test_hidden_resume_marker_crosses_only_the_private_kernel_carrier(
    settings: Any,
    storage: Any,
) -> None:
    owner_id = "hidden-resume-owner"
    marker = _continuation(owner_id)
    service = _ResumeService(
        EngineerCommandResumeObservation(
            continuation=marker,
            payload={"ok": True, "status": "completed"},
        )
    )
    tool = next(
        item
        for item in build_engineer_command_tools(_context(), service=service)  # type: ignore[arg-type]
        if item.name == "engineer_work_item_resume"
    )
    storage.ensure_user(owner_id, preset_key="owner")
    authorization = AuthorizationService(storage)
    authorization.register_capability(ENGINEER_COMMAND_MANAGE)
    actor = authorization.actor_for_user(owner_id, source="hidden-resume-test")
    kernel = ExecutionKernel(authorization, settings)
    kernel.register(tool)

    result = await kernel.execute(
        "engineer_work_item_resume",
        {"_conversation_id": marker.conversation_id},
        actor=actor,
    )

    assert "engineer_work_item_resume" not in kernel.get_tool_names(actor)
    assert result.success is True
    assert result.data == {"active": True, "ok": True, "status": "completed"}
    assert result.engineer_work_item_continuation is marker
    model_message = result.to_llm_message()
    assert model_message is not None
    assert marker.work_item_id not in model_message
    assert "_engineer_work_item_continuation" not in model_message


@pytest.mark.asyncio
async def test_hidden_resume_absence_and_malformed_observation_fail_closed() -> None:
    absent = _ResumeService(None)
    absent_tool = next(
        item
        for item in build_engineer_command_tools(_context(), service=absent)  # type: ignore[arg-type]
        if item.name == "engineer_work_item_resume"
    )
    assert absent_tool.handler is not None
    assert await absent_tool.handler(actor="owner", _conversation_id="conv_absent") == {
        "active": False,
        "ok": True,
    }

    malformed = _ResumeService(None)
    malformed.observation = SimpleNamespace(payload={"ok": True})  # type: ignore[assignment]
    malformed_tool = next(
        item
        for item in build_engineer_command_tools(_context(), service=malformed)  # type: ignore[arg-type]
        if item.name == "engineer_work_item_resume"
    )
    assert malformed_tool.handler is not None
    with pytest.raises(EngineerWorkItemCoordinatorError, match="engineer_resume_observation_invalid"):
        await malformed_tool.handler(actor="owner", _conversation_id="conv_malformed")

    attachment = _ResumeService(
        EngineerCommandResumeObservation(
            continuation=_continuation(),
            payload={"ok": True, "status": "completed"},
            attachment={"kind": "document"},
        )
    )
    attachment_tool = next(
        item
        for item in build_engineer_command_tools(_context(), service=attachment)  # type: ignore[arg-type]
        if item.name == "engineer_work_item_resume"
    )
    assert attachment_tool.handler is not None
    with pytest.raises(EngineerWorkItemCoordinatorError, match="engineer_resume_observation_invalid"):
        await attachment_tool.handler(actor="owner", _conversation_id="conv_attachment")


def test_engineer_organ_reuses_one_service_for_tools_workers_and_accessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.organs.engineer import command_tools
    from friday.organs.engineer import tools as engineer_tools

    created: list[object] = []

    class FalseyService:
        def __bool__(self) -> bool:
            return False

        def execute(self, **_arguments: object) -> dict[str, object]:
            return {"ok": True}

        status = execute
        cancel = execute
        resume_current = execute

        def publish_terminal_jobs(self) -> None:
            return None

        publish_progress_jobs = publish_terminal_jobs
        retain_terminal_jobs = publish_terminal_jobs

        def close(self) -> None:
            return None

    def factory(_ctx: ServiceContext) -> FalseyService:
        service = FalseyService()
        created.append(service)
        return service

    monkeypatch.setattr(command_tools, "EngineerCommandService", factory)
    monkeypatch.setattr(engineer_tools, "build_engineer_tools", lambda _ctx: ())
    organ = EngineerOrgan()
    context = _context()

    first = organ.command_service(context)
    first_tools = organ.tools(context)
    second_tools = organ.tools(context)
    workers = organ.workers(context)

    assert len(created) == 1
    assert first is created[0]
    assert len(workers) == 2
    assert [tool.name for tool in first_tools] == [tool.name for tool in second_tools]
    assert sum(tool.name == "engineer_work_item_resume" for tool in first_tools) == 1


@pytest.mark.asyncio
async def test_cancelled_engineer_worker_keeps_its_physical_thread_visible() -> None:
    """A supervisor timeout must not hide a command-ledger worker from shutdown."""

    from friday.workers._blocking import current_task, in_flight

    started = threading.Event()
    release = threading.Event()

    class BlockingService:
        def publish_terminal_jobs(self) -> None:
            started.set()
            release.wait(5.0)

        def publish_progress_jobs(self) -> None:
            raise AssertionError("cancelled terminal pass continued into progress")

        def retain_terminal_jobs(self) -> None:
            return None

    organ = EngineerOrgan()
    organ._command_service = cast(Any, BlockingService())  # noqa: SLF001
    context = _context()
    worker = organ.workers(context)[0]
    token = current_task.set(worker.name)
    task = asyncio.create_task(worker.run(context))
    try:
        assert await asyncio.to_thread(started.wait, 2.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert in_flight(worker.name) == 1
        release.set()
        for _ in range(100):
            if in_flight(worker.name) == 0:
                break
            await asyncio.sleep(0.01)
        assert in_flight(worker.name) == 0
    finally:
        release.set()
        current_task.reset(token)
