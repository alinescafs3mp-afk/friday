from __future__ import annotations

import base64
import hashlib
import importlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.execution_kernel import ExecutionKernel, ToolResult, ToolSpec
from friday.interaction_control_plane.engineer_work_item import (
    EngineerWorkItemChannel,
    EngineerWorkItemState,
    EngineerWorkItemStepState,
    EngineerWorkItemTransition,
    bind_engineer_command_receipts_in_transaction,
    create_engineer_work_item_in_transaction,
    engineer_source_binding_sha256,
    get_engineer_work_item_in_transaction,
    mark_engineer_command_unknown_in_transaction,
    mark_engineer_work_item_ready_to_answer_in_transaction,
    settle_engineer_terminal_receipt_in_transaction,
)
from friday.interaction_control_plane.engineer_work_item_schema import (
    ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256,
)
from friday.orchestration.engineer_work_item_coordinator import (
    EngineerCommandLedgerDisposition,
    EngineerContinuationState,
)
from friday.organs.engineer import ENGINEER_COMMAND_MANAGE, ENGINEER_USE
from friday.organs.engineer.command.contracts import CommandStatus
from friday.organs.engineer.publication import exact_generated_file_batch
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext, AuthorizationService

_SOURCE_ROW_ID = "msg_engineer_publication_source"
_SOURCE_STEP_ID = "ecstep-" + "1" * 32
_SOURCE_HASH = "2" * 64
_IDEMPOTENCY_KEY = "ecmd-" + "3" * 64
_COMMAND_DIGEST = "4" * 64
_JOB_ID = "5" * 32
_TERMINAL_RECEIPT = "6" * 64
_TELEGRAM_UPDATE_ID = "424242"
_DELIVERY_CHAT_ID = "123456789"
_MODEL_ANSWER = "Проверенный итог Engineer-задачи."
_FILE_PAYLOAD = b"engineer-publication-rollback"
_BEFORE_EXPIRY = "2098-12-31T23:59:59+00:00"
_FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"
_AFTER_EXPIRY = "2099-01-01T00:00:01+00:00"


class _UnusedModel:
    enabled = True
    model = "unused-publication-model"
    total_budget_sec = 0.1

    async def chat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("the focused publication fixture replaces generation")


class _ResumeKernel(ExecutionKernel):
    def __init__(
        self,
        authorization: AuthorizationService,
        settings: Any,
        observation: Callable[[int], tuple[EngineerContinuationState, dict[str, Any]]],
    ) -> None:
        super().__init__(authorization, settings)
        self._observation = observation
        self.resume_calls = 0

        async def hidden_handler(*, actor: ActorContext, **_kwargs: Any) -> dict[str, Any]:
            del actor
            return {"ok": True}

        self.register(
            ToolSpec(
                name="engineer_work_item_resume",
                description="Private Engineer Work Item reconciliation seam.",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                security_id="engineer.command.manage",
                risk="mutate",
                handler=hidden_handler,
                model_visible=False,
            )
        )

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext | None = None,
        execution_scope: str = "dialogue",
    ) -> ToolResult:
        if name != "engineer_work_item_resume":
            return await super().execute(
                name,
                arguments,
                actor=actor,
                execution_scope=execution_scope,
            )
        self.resume_calls += 1
        marker, data = self._observation(self.resume_calls)
        return ToolResult(
            name,
            True,
            data=data,
            engineer_work_item_continuation=marker,
            handler_entered=True,
        )


@dataclass(frozen=True)
class _PublicationFixture:
    runtime: AgentRuntime
    actor: ActorContext
    conversation_id: str
    marker: EngineerContinuationState
    kernel: _ResumeKernel


def _continuation_from_item(
    item: Any,
    *,
    command_status: CommandStatus,
) -> EngineerContinuationState:
    return EngineerContinuationState(
        work_item_id=item.id,
        owner_id=item.owner_id,
        tenant_id=item.tenant_id,
        conversation_id=item.conversation_id,
        channel=item.channel,
        state=item.state,
        transition=item.transition,
        revision=item.revision,
        step_ordinal=item.step_ordinal,
        step_state=item.current_step.state,
        source_binding_sha256=item.current_step.source_binding_sha256,
        idempotency_key=item.current_step.idempotency_key,
        command_digest=item.current_step.command_digest,
        job_receipt_sha256=item.current_step.job_receipt_sha256,
        terminal_receipt_sha256=item.current_step.terminal_receipt_sha256,
        ledger_disposition=EngineerCommandLedgerDisposition.EXACT,
        command_job_id=_JOB_ID,
        command_status=command_status,
    )


def _seed_work_item(
    storage: Any,
    conversation_id: str,
    *,
    step_state: EngineerWorkItemStepState,
    expires_at: str | None = None,
    now: str | None = None,
    ready_to_answer: bool = False,
) -> EngineerContinuationState:
    scope = {
        "owner_id": LEGACY_OWNER_USER_ID,
        "tenant_id": LEGACY_OWNER_USER_ID,
        "conversation_id": conversation_id,
        "channel": EngineerWorkItemChannel.TELEGRAM,
    }
    source_binding = engineer_source_binding_sha256(
        **scope,
        source_row_id=_SOURCE_ROW_ID,
        source_step_id=_SOURCE_STEP_ID,
        source_hash=_SOURCE_HASH,
        telegram_update_id=_TELEGRAM_UPDATE_ID,
        delivery_chat_id=_DELIVERY_CHAT_ID,
    )
    with storage.transaction() as conn:
        prepared = create_engineer_work_item_in_transaction(
            conn,
            **scope,
            source_binding_sha256=source_binding,
            completion_contract_sha256=ENGINEER_WORK_ITEM_COMPLETION_CONTRACT_SHA256,
            idempotency_key=_IDEMPOTENCY_KEY,
            command_digest=_COMMAND_DIGEST,
            expires_at=expires_at,
            now=now,
        )
        admitted = bind_engineer_command_receipts_in_transaction(
            conn,
            **scope,
            work_item_id=prepared.id,
            expected_revision=prepared.revision,
            ledger_binding={
                "job_id": _JOB_ID,
                "actor_id": LEGACY_OWNER_USER_ID,
                "tenant_id": LEGACY_OWNER_USER_ID,
                "conversation_id": conversation_id,
                "channel": EngineerWorkItemChannel.TELEGRAM.value,
                "source_row_id": _SOURCE_ROW_ID,
                "source_step_id": _SOURCE_STEP_ID,
                "source_hash": _SOURCE_HASH,
                "telegram_update_id": _TELEGRAM_UPDATE_ID,
                "idempotency_key": _IDEMPOTENCY_KEY,
                "command_digest": _COMMAND_DIGEST,
                "delivery_chat_id": _DELIVERY_CHAT_ID,
            },
            now=now,
        )
        if step_state is EngineerWorkItemStepState.ADMITTED:
            current = admitted
            status = CommandStatus.RUNNING
        elif step_state is EngineerWorkItemStepState.UNKNOWN:
            current = mark_engineer_command_unknown_in_transaction(
                conn,
                **scope,
                work_item_id=prepared.id,
                expected_revision=admitted.revision,
                now=now,
            )
            status = CommandStatus.UNKNOWN
        elif step_state is EngineerWorkItemStepState.SETTLED:
            current = settle_engineer_terminal_receipt_in_transaction(
                conn,
                **scope,
                work_item_id=prepared.id,
                expected_revision=admitted.revision,
                verified_terminal_receipt_sha256=_TERMINAL_RECEIPT,
                now=now,
            )
            status = CommandStatus.COMPLETED
        else:  # pragma: no cover - focused fixture contract
            raise AssertionError("unsupported publication fixture step state")
        if ready_to_answer:
            if step_state is not EngineerWorkItemStepState.SETTLED:
                raise AssertionError("only a settled fixture can be ready to answer")
            current = mark_engineer_work_item_ready_to_answer_in_transaction(
                conn,
                **scope,
                work_item_id=prepared.id,
                expected_revision=current.revision,
                now=now,
            )
    return _continuation_from_item(current, command_status=status)


def _seed_settled_work_item(storage: Any, conversation_id: str) -> EngineerContinuationState:
    return _seed_work_item(
        storage,
        conversation_id,
        step_state=EngineerWorkItemStepState.SETTLED,
    )


def _publication_fixture(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    observation: Callable[[EngineerContinuationState, int], tuple[EngineerContinuationState, dict[str, Any]]]
    | None = None,
    step_state: EngineerWorkItemStepState = EngineerWorkItemStepState.SETTLED,
    expires_at: str | None = None,
    now: str | None = None,
    ready_to_answer: bool = False,
) -> _PublicationFixture:
    configured = replace(
        settings,
        engineer_mode_enabled=True,
        engineer_command_enabled=True,
        verify_answers=False,
    )
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    conversation = storage.create_conversation(
        LEGACY_OWNER_USER_ID,
        title="Engineer publication safety",
        mode="engineer",
    )
    conversation_id = str(conversation["id"])
    marker = _seed_work_item(
        storage,
        conversation_id,
        step_state=step_state,
        expires_at=expires_at,
        now=now,
        ready_to_answer=ready_to_answer,
    )
    authorization = AuthorizationService(storage)
    authorization.register_capability(ENGINEER_USE)
    authorization.register_capability(ENGINEER_COMMAND_MANAGE)

    def observed(call: int) -> tuple[EngineerContinuationState, dict[str, Any]]:
        if observation is not None:
            return observation(marker, call)
        return marker, {
            "active": True,
            "ok": True,
            "job_id": marker.command_job_id,
            "status": marker.command_status.value,
            "stdout": "retention-sensitive terminal output",
        }

    kernel = _ResumeKernel(authorization, configured, observed)
    runtime = AgentRuntime(
        configured,
        storage,
        llm=_UnusedModel(),  # type: ignore[arg-type]
        kernel=kernel,
    )
    actor = authorization.actor_for_user(LEGACY_OWNER_USER_ID, source="telegram-bridge")

    async def prepare(
        user_id: str,
        message: str,
        prepared_conversation_id: str,
        **_kwargs: Any,
    ) -> AgentContext:
        del message
        return AgentContext(
            conversation_id=prepared_conversation_id,
            user_id=user_id,
            person_id=user_id,
            interaction_mode="engineer",
        )

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    return _PublicationFixture(runtime, actor, conversation_id, marker, kernel)


def _patch_generation(
    fixture: _PublicationFixture,
    monkeypatch: pytest.MonkeyPatch,
    *,
    storage: Any,
    files: list[dict[str, Any]] | None = None,
    revoke_manage: bool = False,
    before_publication: Callable[[AgentContext], None] | None = None,
) -> None:
    mutation_applied = False

    def response_for(context: AgentContext) -> dict[str, Any]:
        nonlocal mutation_applied
        context.engineer_work_item_continuation = fixture.marker
        if files:
            context.engineer_command_generated_file_batch = exact_generated_file_batch(
                files,
                max_bytes=fixture.runtime.settings.max_upload_bytes,
            )
        if revoke_manage:
            storage.set_permission_override(
                LEGACY_OWNER_USER_ID,
                "engineer.command.manage",
                "deny",
            )
        if before_publication is not None and not mutation_applied:
            mutation_applied = True
            before_publication(context)
        return {
            "content": _MODEL_ANSWER,
            "tools_used": [],
            "tool_evidence": [],
            "file_clips": list(files or []),
            "_model_generated": True,
        }

    async def generate(
        context: AgentContext,
        message: str,
        attachments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del message, attachments
        return response_for(context)

    async def agentic(
        context: AgentContext,
        message: str,
        actor: ActorContext,
        tools: list[dict[str, Any]],
        attachments: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        del message, actor, tools, attachments
        return response_for(context)

    monkeypatch.setattr(fixture.runtime, "_generate_response", generate)
    monkeypatch.setattr(fixture.runtime, "_agentic_loop", agentic)


async def _chat(fixture: _PublicationFixture) -> dict[str, Any]:
    return await fixture.runtime.chat(
        LEGACY_OWNER_USER_ID,
        "Подведи итог выполненной Engineer-задачи.",
        actor=fixture.actor,
        conversation_id=fixture.conversation_id,
        mode="engineer",
        telegram_update_id="777777",
    )


def _stored_work_item(storage: Any, fixture: _PublicationFixture) -> Any:
    with storage.transaction() as conn:
        return get_engineer_work_item_in_transaction(
            conn,
            work_item_id=fixture.marker.work_item_id,
            owner_id=LEGACY_OWNER_USER_ID,
            tenant_id=LEGACY_OWNER_USER_ID,
            conversation_id=fixture.conversation_id,
            channel=EngineerWorkItemChannel.TELEGRAM,
        )


def _assert_still_waiting(storage: Any, fixture: _PublicationFixture) -> None:
    current = _stored_work_item(storage, fixture)
    assert current is not None
    assert current.state is EngineerWorkItemState.WAITING_FOR_INPUT
    assert current.transition is EngineerWorkItemTransition.TERMINAL_OBSERVED
    assert current.current_step.state is EngineerWorkItemStepState.SETTLED
    assert current.revision == fixture.marker.revision
    assert current.closed_at is None


@pytest.mark.asyncio
async def test_assistant_persistence_failure_rolls_back_ready_and_completion(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _publication_fixture(settings, storage, monkeypatch)
    _patch_generation(fixture, monkeypatch, storage=storage)
    runtime_module = importlib.import_module("friday.agent_runtime")
    original = runtime_module.store_message_in_transaction

    def persist_then_fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        stored = original(*args, **kwargs)
        if len(args) >= 4 and args[3] == "assistant":
            raise RuntimeError("synthetic assistant persistence failure")
        return stored

    monkeypatch.setattr(runtime_module, "store_message_in_transaction", persist_then_fail)

    with pytest.raises(RuntimeError, match="assistant persistence failure"):
        await _chat(fixture)

    _assert_still_waiting(storage, fixture)
    assert fixture.kernel.resume_calls == 1
    assistants = storage.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant'",
        (fixture.conversation_id,),
    ).fetchone()[0]
    assert assistants == 0


@pytest.mark.asyncio
async def test_generated_file_persistence_failure_rolls_back_the_whole_completion_unit(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _publication_fixture(settings, storage, monkeypatch)
    generated = {
        "kind": "document",
        "filename": "engineer-result.bin",
        "mime_type": "application/octet-stream",
        "content_base64": base64.b64encode(_FILE_PAYLOAD).decode("ascii"),
    }
    _patch_generation(fixture, monkeypatch, storage=storage, files=[generated])
    runtime_module = importlib.import_module("friday.agent_runtime")
    original = runtime_module.persist_exact_generated_file_batch

    def persist_then_fail(*args: Any, **kwargs: Any) -> Any:
        original(*args, **kwargs)
        raise RuntimeError("synthetic generated-file persistence failure")

    monkeypatch.setattr(runtime_module, "persist_exact_generated_file_batch", persist_then_fail)

    with pytest.raises(RuntimeError, match="generated-file persistence failure"):
        await _chat(fixture)

    _assert_still_waiting(storage, fixture)
    assert fixture.kernel.resume_calls == 1
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='assistant'",
            (fixture.conversation_id,),
        ).fetchone()[0]
        == 0
    )
    assert (
        storage.execute(
            "SELECT COUNT(*) FROM raw_objects WHERE content_type='generated_file'",
        ).fetchone()[0]
        == 0
    )
    digest = hashlib.sha256(_FILE_PAYLOAD).hexdigest()
    artifact = Path(settings.files_dir) / LEGACY_OWNER_USER_ID / "generated" / digest[:2] / f"{digest}.blob"
    assert not artifact.exists()


@pytest.mark.asyncio
async def test_fresh_manage_revoke_denies_completion_output_and_leaves_work_open(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _publication_fixture(settings, storage, monkeypatch)
    _patch_generation(fixture, monkeypatch, storage=storage, revoke_manage=True)

    response = await _chat(fixture)

    _assert_still_waiting(storage, fixture)
    assert fixture.kernel.resume_calls == 0
    assert _MODEL_ANSWER not in response["message"]
    assert "право или durable-состояние" in response["message"]
    assert response["files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["output_retired", "marker_changed"])
async def test_final_hidden_resume_revalidation_denies_retired_or_changed_result(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    def observation(
        marker: EngineerContinuationState,
        _call: int,
    ) -> tuple[EngineerContinuationState, dict[str, Any]]:
        observed = marker
        output_retired = changed == "output_retired"
        if changed == "marker_changed":
            observed = replace(
                marker,
                state=EngineerWorkItemState.READY_TO_ANSWER,
                transition=EngineerWorkItemTransition.ANSWER_READY,
                revision=marker.revision + 1,
            )
        return observed, {
            "active": True,
            "ok": True,
            "job_id": observed.command_job_id,
            "status": observed.command_status.value,
            "output_retired": output_retired,
        }

    fixture = _publication_fixture(
        settings,
        storage,
        monkeypatch,
        observation=observation,
    )
    _patch_generation(fixture, monkeypatch, storage=storage)

    response = await _chat(fixture)

    _assert_still_waiting(storage, fixture)
    assert fixture.kernel.resume_calls == 1
    assert _MODEL_ANSWER not in response["message"]
    assert "retention-sensitive terminal output" not in response["message"]
    assert "право или durable-состояние" in response["message"]
    assert response["files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step_state",
    [
        pytest.param(EngineerWorkItemStepState.ADMITTED, id="admitted"),
        pytest.param(EngineerWorkItemStepState.UNKNOWN, id="unknown"),
    ],
)
@pytest.mark.parametrize(
    "changed",
    [
        pytest.param("manage_revoked", id="manage-revoked"),
        pytest.param("account_disabled", id="account-disabled"),
        pytest.param("conversation_archived", id="conversation-archived"),
        pytest.param("terminal_settled", id="terminal-settled"),
    ],
)
async def test_active_publication_revalidates_authority_scope_and_exact_phase(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    step_state: EngineerWorkItemStepState,
    changed: str,
) -> None:
    refreshed_markers: list[EngineerContinuationState] = []

    def observation(
        marker: EngineerContinuationState,
        _call: int,
    ) -> tuple[EngineerContinuationState, dict[str, Any]]:
        observed = refreshed_markers[-1] if refreshed_markers else marker
        assert observed.command_job_id is not None
        assert observed.command_status is not None
        return observed, {
            "active": True,
            "ok": True,
            "job_id": observed.command_job_id,
            "status": observed.command_status.value,
        }

    fixture = _publication_fixture(
        settings,
        storage,
        monkeypatch,
        observation=observation,
        step_state=step_state,
    )

    def mutate_before_publication(_context: AgentContext) -> None:
        if changed == "manage_revoked":
            storage.set_permission_override(
                LEGACY_OWNER_USER_ID,
                "engineer.command.manage",
                "deny",
            )
            return
        if changed == "account_disabled":
            updated = storage.update_user(LEGACY_OWNER_USER_ID, status="disabled")
            assert updated is not None and updated["status"] == "disabled"
            return
        if changed == "conversation_archived":
            assert storage.archive_conversation(fixture.conversation_id, LEGACY_OWNER_USER_ID)
            return
        assert changed == "terminal_settled"
        with storage.transaction() as conn:
            settled = settle_engineer_terminal_receipt_in_transaction(
                conn,
                work_item_id=fixture.marker.work_item_id,
                owner_id=LEGACY_OWNER_USER_ID,
                tenant_id=LEGACY_OWNER_USER_ID,
                conversation_id=fixture.conversation_id,
                channel=EngineerWorkItemChannel.TELEGRAM,
                expected_revision=fixture.marker.revision,
                verified_terminal_receipt_sha256=_TERMINAL_RECEIPT,
            )
        refreshed_markers.append(
            _continuation_from_item(
                settled,
                command_status=CommandStatus.COMPLETED,
            )
        )

    _patch_generation(
        fixture,
        monkeypatch,
        storage=storage,
        before_publication=mutate_before_publication,
    )

    response = await _chat(fixture)

    assert _MODEL_ANSWER not in response["message"]
    assert "право или durable-состояние" in response["message"]
    assert response["files"] == []
    assert response["message_format"] == "plain"
    current = _stored_work_item(storage, fixture)
    assert current is not None
    assert current.state is not EngineerWorkItemState.COMPLETED
    if changed == "terminal_settled":
        assert fixture.kernel.resume_calls == 1
        assert current.state is EngineerWorkItemState.WAITING_FOR_INPUT
        assert current.transition is EngineerWorkItemTransition.TERMINAL_OBSERVED
        assert current.current_step.state is EngineerWorkItemStepState.SETTLED
        assert current.revision == fixture.marker.revision + 1
    else:
        assert current.state is fixture.marker.state
        assert current.transition is fixture.marker.transition
        assert current.current_step.state is step_state
        assert current.revision == fixture.marker.revision
        if changed in {"manage_revoked", "account_disabled"}:
            assert fixture.kernel.resume_calls == 0
        else:
            assert fixture.kernel.resume_calls == 1
    if changed == "conversation_archived":
        conversation = storage.get_conversation(fixture.conversation_id, LEGACY_OWNER_USER_ID)
        assert conversation is not None and conversation["is_archived"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "step_state",
    [
        pytest.param(EngineerWorkItemStepState.ADMITTED, id="admitted"),
        pytest.param(EngineerWorkItemStepState.UNKNOWN, id="unknown"),
    ],
)
async def test_unchanged_active_phase_publishes_progress_without_closing_work_item(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    step_state: EngineerWorkItemStepState,
) -> None:
    fixture = _publication_fixture(
        settings,
        storage,
        monkeypatch,
        step_state=step_state,
    )
    _patch_generation(fixture, monkeypatch, storage=storage)

    response = await _chat(fixture)

    assert response["message"] == _MODEL_ANSWER
    assert response["files"] == []
    assert fixture.kernel.resume_calls == 1
    current = _stored_work_item(storage, fixture)
    assert current is not None
    assert current.state is fixture.marker.state
    assert current.transition is fixture.marker.transition
    assert current.current_step.state is step_state
    assert current.revision == fixture.marker.revision
    assert current.closed_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ready_to_answer",
    [
        pytest.param(False, id="waiting-for-input"),
        pytest.param(True, id="ready-to-answer"),
    ],
)
async def test_completion_expiring_after_final_resume_falls_back_without_close(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    ready_to_answer: bool,
) -> None:
    runtime_module = importlib.import_module("friday.agent_runtime")
    work_item_module = importlib.import_module("friday.interaction_control_plane.engineer_work_item")
    original_work_item_now = work_item_module._now

    class _AfterExpiryDateTime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            instant = datetime.fromisoformat(_AFTER_EXPIRY)
            return instant.replace(tzinfo=None) if tz is None else instant.astimezone(tz)

    def observation(
        marker: EngineerContinuationState,
        _call: int,
    ) -> tuple[EngineerContinuationState, dict[str, Any]]:
        # The hidden resume observed the item immediately before its deadline;
        # advance the publication-side clock only after that exact observation.
        monkeypatch.setattr(runtime_module, "datetime", _AfterExpiryDateTime)
        monkeypatch.setattr(
            work_item_module,
            "_now",
            lambda value: _AFTER_EXPIRY if value is None else original_work_item_now(value),
        )
        assert marker.command_job_id is not None
        assert marker.command_status is not None
        return marker, {
            "active": True,
            "ok": True,
            "job_id": marker.command_job_id,
            "status": marker.command_status.value,
            "stdout": "expired retention-sensitive terminal output",
        }

    fixture = _publication_fixture(
        settings,
        storage,
        monkeypatch,
        observation=observation,
        expires_at=_FUTURE_EXPIRY,
        now=_BEFORE_EXPIRY,
        ready_to_answer=ready_to_answer,
    )
    _patch_generation(fixture, monkeypatch, storage=storage)

    response = await _chat(fixture)

    assert fixture.kernel.resume_calls == 1
    assert _MODEL_ANSWER not in response["message"]
    assert "expired retention-sensitive terminal output" not in response["message"]
    assert "право или durable-состояние" in response["message"]
    assert response["files"] == []
    assert response["message_format"] == "plain"
    current = _stored_work_item(storage, fixture)
    assert current is not None
    assert current.state is fixture.marker.state
    assert current.transition is fixture.marker.transition
    assert current.current_step.state is EngineerWorkItemStepState.SETTLED
    assert current.revision == fixture.marker.revision
    assert current.closed_at is None
