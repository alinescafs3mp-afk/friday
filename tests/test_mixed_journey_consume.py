from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from test_mixed_file_archive_web_comparison import (
    _DEFAULT_ANSWER,
    _archive_file,
    _ComparisonModel,
    _current_file,
    _full_web,
    _web_evidence,
)

from friday.agent_runtime import AgentRuntime
from friday.orchestration.engineer_result_carrier import EngineerResultCarrierKind
from friday.orchestration.mixed_journey_store_projection import MixedJourneyStoreProjectionState
from friday.orchestration.transient_web_comparison import (
    TransientWebEvidenceStatus,
    TransientWebUnavailableReason,
)
from friday.orchestration.web_provider_policy import WebProviderDecision
from friday.orchestration.web_research_consumption import (
    WebResearchConsumptionReason,
    WebResearchConsumptionState,
)
from friday.organs.mixed_journey.admit import mixed_status_admitted
from friday.organs.mixed_journey.consume import (
    MixedJourneyConsumeLedger,
    handle_mixed_file_archive_web_turn,
)
from friday.organs.mixed_journey.observe import observe_mixed_journey
from friday.permissions import ActorContext

_MIXED = "Сравни этот договор с архивом contract.txt и текущими публичными правилами в интернете."
_ATTACHMENTS = [{"filename": "current.txt", "mime_type": "text/plain"}]


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


async def _handle(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "user_id": "alice",
        "actor": _actor(),
        "message": _MIXED,
        "conversation_id": "conv_0123456789abcdef",
        "attachments": _ATTACHMENTS,
        "model": _ComparisonModel(),
        "prepared_file": _current_file(),
        "prepared_archive": _archive_file(),
        "web_evidence": _full_web(),
        "authenticated_turn_id": "turn-1",
        "projection_id": "mixed:turn-1",
        "accepted_plan_sha256": "9" * 64,
    }
    values.update(overrides)
    return await handle_mixed_file_archive_web_turn(**values)


@pytest.mark.asyncio
async def test_injected_evidence_publishes_text_only_with_file_archive_web_citations() -> None:
    model = _ComparisonModel()
    reply = await _handle(model=model)
    assert reply["message"] == _DEFAULT_ANSWER
    assert reply["files"] == []
    assert reply["context"]["carrier"] == EngineerResultCarrierKind.TEXT.value
    assert [item["label"] for item in reply["citations"]] == ["F1", "A1", "W1", "W2", "W3"]
    assert reply["web_evidence_status"] == "sourced"
    assert len(reply["web_sources"]) == 3
    organs = set(reply["context"]["mixed_organs"])
    assert organs == {"file", "archive", "web"}
    assert not reply.get("knowledge_objects")
    assert reply["context"]["mixed_archive_member_count"] == 1
    prepared_file = _current_file()
    prepared_archive = _archive_file()
    observed = observe_mixed_journey(
        "mixed:turn-1",
        "turn-1",
        files=(
            {
                "id": prepared_file.raw_ids[0],
                "sha256": prepared_file.snapshot_tokens[0].source.identity_sha256,
            },
        ),
        archives=(
            {
                "id": prepared_archive.raw_ids[0],
                "sha256": prepared_archive.snapshot_tokens[0].source.identity_sha256,
                "member_count": 1,
            },
        ),
        tables=reply.get("knowledge_objects") or (),
        web=reply.get("web_research_consumption"),
    )
    assert observed.state is MixedJourneyStoreProjectionState.PROJECTED
    assert observed.view is not None
    assert set(observed.view.organs.present_organs) == {"file", "archive", "web"}
    assert mixed_status_admitted(observed) is True
    assert model.acquire_calls == 1
    consumption = reply["web_research_consumption"]
    assert consumption.selected_provider_id == "brave"
    assert consumption.usability is WebResearchConsumptionState.CONSUMABLE
    assert consumption.reason is WebResearchConsumptionReason.PRIMARY_SOURCES


@pytest.mark.asyncio
@pytest.mark.parametrize("table_reply", [False, True])
async def test_prepared_comparison_table_uses_same_shape_contract_without_invented_object(
    table_reply,
) -> None:
    answer = (
        "| Источник | Состояние |\n| --- | --- |\n"
        "| Файл [F1] | исходное |\n| Архив [A1] | та же основа |\n"
        "| Веб [W1] [W2] [W3] | контекст, изменение и границы |"
    )
    model = _ComparisonModel(answer=answer if table_reply else _DEFAULT_ANSWER)
    reply = await _handle(
        model=model,
        message=_MIXED + " Ответ оформи таблицей.",
        ledger=MixedJourneyConsumeLedger(),
    )
    assert not reply.get("knowledge_objects")
    if table_reply:
        assert reply["message"] == answer
        assert reply["message_format"] == "markdown"
        assert "table" not in reply["context"]["mixed_organs"]
        assert len(model.calls) == 2
    else:
        assert reply["message"] != _DEFAULT_ANSWER
        assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_projected_fallback_evidence_stays_degraded_through_mixed_consumption() -> None:
    web = _web_evidence(
        "Первый публичный источник описывает текущее состояние.",
        "Второй публичный источник подтверждает изменение.",
        "Третий публичный источник задаёт область применимости.",
        provider_primary_id="yandex",
        selected_provider_id="brave",
        provider_used_fallback=True,
    )
    assert web.provider_decision is WebProviderDecision.FALLBACK_USED

    reply = await _handle(web_evidence=web)

    consumption = reply["web_research_consumption"]
    assert consumption.selected_provider_id == "brave"
    assert consumption.admitted_source_count == 3
    assert consumption.usability is WebResearchConsumptionState.CONSUMABLE_DEGRADED
    assert consumption.reason is WebResearchConsumptionReason.FALLBACK_SOURCES


@pytest.mark.asyncio
async def test_duplicate_identity_is_idempotent_and_does_not_recall_the_model() -> None:
    ledger = MixedJourneyConsumeLedger()
    model = _ComparisonModel()
    first = await _handle(model=model, ledger=ledger)
    second = await _handle(model=_ComparisonModel(), ledger=ledger)
    assert first["message"] == second["message"] == _DEFAULT_ANSWER
    assert model.acquire_calls == 1
    assert (
        first["context"]["mixed_source_identity_sha256"] == second["context"]["mixed_source_identity_sha256"]
    )


@pytest.mark.asyncio
async def test_cancel_while_running_does_not_publish() -> None:
    ledger = MixedJourneyConsumeLedger()
    model = _ComparisonModel(hanging_call=1)
    cancel = asyncio.Event()
    task = asyncio.create_task(_handle(model=model, ledger=ledger, cancel_event=cancel))
    await asyncio.wait_for(model.dispatched.wait(), timeout=2)
    cancel.set()
    reply = await asyncio.wait_for(task, timeout=2)
    assert "отменено" in reply["message"].casefold()
    assert reply["files"] == []


class _CleanupModel(_ComparisonModel):
    def __init__(self) -> None:
        super().__init__(hanging_call=1)
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.task: asyncio.Task[Any] | None = None

    async def complete(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.task = asyncio.current_task()
        try:
            return await super().complete(*args, **kwargs)
        finally:
            self.cleanup_started.set()
            await self.cleanup_release.wait()
            self.cleaned.set()


class _TrackedCancelEvent(asyncio.Event):
    active_waiters = 0

    async def wait(self) -> bool:
        self.active_waiters += 1
        try:
            return await super().wait()
        finally:
            self.active_waiters -= 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "with_event", "repeat_cancel"),
    [
        ("caller", False, False),
        ("caller", True, False),
        ("event", True, False),
        ("caller", True, True),
        ("event", True, True),
    ],
)
async def test_cancel_drains_model_cleanup_and_waiter_before_returning(
    mode: str,
    with_event: bool,
    repeat_cancel: bool,
) -> None:
    ledger = MixedJourneyConsumeLedger()
    model = _CleanupModel()
    cancel = _TrackedCancelEvent() if with_event else None
    storage = Mock()
    sends: list[dict[str, Any]] = []
    task = asyncio.create_task(
        _handle(model=model, ledger=ledger, cancel_event=cancel, publisher=sends.append, storage=storage)
    )
    try:
        await asyncio.wait_for(model.dispatched.wait(), timeout=2)
        if mode == "event":
            assert cancel is not None
            cancel.set()
        else:
            task.cancel()
        await asyncio.wait_for(model.cleanup_started.wait(), timeout=2)
        assert not task.done(), "consume returned before owned model cleanup finished"
        if repeat_cancel:
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done(), "repeated cancellation interrupted cleanup"
        model.cleanup_release.set()
        if mode == "caller" or repeat_cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        else:
            reply = await asyncio.wait_for(task, timeout=2)
            assert "отменено" in reply["message"].casefold()
        assert model.cleaned.is_set()
        assert model.task is not None and model.task.done()
        assert cancel is None or cancel.active_waiters == 0
        assert sends == []
        assert storage.mock_calls == []
        retry_model = _ComparisonModel()
        retry = await _handle(model=retry_model, ledger=ledger, publisher=sends.append)
        assert "отменено" in retry["message"].casefold()
        assert retry_model.acquire_calls == 0
        assert sends == []
    finally:
        # Also clean up the deliberately exposed orphan on the unfixed source.
        model.cleanup_release.set()
        owned = [child for child in (task, model.task) if child is not None]
        for child in owned:
            if not child.done():
                child.cancel()
        await asyncio.gather(*owned, return_exceptions=True)


@pytest.mark.asyncio
async def test_success_drains_unused_cancel_waiter() -> None:
    cancel = _TrackedCancelEvent()
    reply = await _handle(ledger=MixedJourneyConsumeLedger(), cancel_event=cancel)
    assert reply["message"] == _DEFAULT_ANSWER
    assert cancel.active_waiters == 0


@pytest.mark.asyncio
async def test_cancel_during_waiter_cleanup_prevents_persistence_and_publication() -> None:
    class _CancelDuringCleanup(_TrackedCancelEvent):
        async def wait(self) -> bool:
            try:
                return await super().wait()
            finally:
                self.set()

    cancel = _CancelDuringCleanup()
    storage = Mock()
    publisher = Mock()
    reply = await _handle(
        ledger=MixedJourneyConsumeLedger(),
        cancel_event=cancel,
        storage=storage,
        publisher=publisher,
    )
    assert "отменено" in reply["message"].casefold()
    assert cancel.active_waiters == 0
    assert storage.mock_calls == []
    publisher.assert_not_called()


@pytest.mark.asyncio
async def test_web_restart_after_running_is_fail_closed_without_a_second_model_call() -> None:
    ledger = MixedJourneyConsumeLedger()
    model = _ComparisonModel(hanging_call=1)
    cancel = asyncio.Event()
    first = asyncio.create_task(_handle(model=model, ledger=ledger, cancel_event=cancel))
    await asyncio.wait_for(model.dispatched.wait(), timeout=2)
    cancel.set()
    await asyncio.wait_for(first, timeout=2)
    idle = _ComparisonModel()
    reply = await _handle(model=idle, ledger=ledger, restarted=True)
    assert "перезапуска" in reply["message"].casefold()
    assert idle.acquire_calls == 0


@pytest.mark.asyncio
async def test_send_unknown_does_not_send_again() -> None:
    ledger = MixedJourneyConsumeLedger()
    sends: list[str] = []

    def publisher(reply: dict[str, Any]) -> bool:
        sends.append(str(reply["message"]))
        raise TimeoutError("transport unknown")

    first = await _handle(ledger=ledger, publisher=publisher)
    second = await _handle(ledger=ledger, publisher=publisher)
    assert first["message"] == second["message"] == _DEFAULT_ANSWER
    assert sends == [_DEFAULT_ANSWER]


@pytest.mark.asyncio
async def test_consume_does_not_invent_a_provider_when_evidence_omits_one() -> None:
    web = _web_evidence(
        "Первый публичный источник описывает текущее состояние.",
        "Второй публичный источник подтверждает изменение.",
        "Третий публичный источник задаёт область применимости.",
    )
    reply = await _handle(web_evidence=web)
    consumption = reply["web_research_consumption"]
    assert consumption.selected_provider_id is None
    assert consumption.usability is WebResearchConsumptionState.UNAVAILABLE
    assert consumption.admitted_source_count == 0
    assert consumption.reason is WebResearchConsumptionReason.PROVIDER_UNAVAILABLE


@pytest.mark.asyncio
async def test_consume_does_not_invent_primary_decision_for_legacy_selected_provider() -> None:
    web = _web_evidence(
        "Первый публичный источник описывает текущее состояние.",
        "Второй публичный источник подтверждает изменение.",
        "Третий публичный источник задаёт область применимости.",
        selected_provider_id="brave",
    )
    assert web.status.value == "sourced"
    assert web.selected_provider_id == "brave"
    assert web.provider_primary_id is None
    assert web.provider_decision is None

    reply = await _handle(web_evidence=web)

    consumption = reply["web_research_consumption"]
    assert consumption.selected_provider_id is None
    assert consumption.admitted_source_count == 0
    assert consumption.usability is WebResearchConsumptionState.UNAVAILABLE
    assert consumption.reason is WebResearchConsumptionReason.PROVIDER_FACTS_INVALID


@pytest.mark.asyncio
async def test_invalid_projected_provider_provenance_stays_invalid_in_partial_consumption() -> None:
    web = _web_evidence(
        "Публичный источник описывает текущее состояние.",
        provider_primary_id="brave",
        selected_provider_id="brave",
        provider_used_fallback=True,
    )
    assert web.status is TransientWebEvidenceStatus.UNAVAILABLE
    assert web.unavailable_reason is TransientWebUnavailableReason.PROVIDER_ERROR

    reply = await _handle(
        web_evidence=web,
        model=_ComparisonModel(answer="Локальные источники доступны, веб недоступен. [F1] [A1]"),
    )

    consumption = reply["web_research_consumption"]
    assert consumption.selected_provider_id is None
    assert consumption.admitted_source_count == 0
    assert consumption.usability is WebResearchConsumptionState.UNAVAILABLE
    assert consumption.reason is WebResearchConsumptionReason.PROVIDER_FACTS_INVALID


@pytest.mark.asyncio
async def test_consume_does_not_invent_a_provider_when_evidence_names_an_unknown_one() -> None:
    web = _web_evidence(
        "Первый публичный источник описывает текущее состояние.",
        selected_provider_id="not-a-closed-provider",
    )
    assert web.selected_provider_id is None
    reply = await _handle(web_evidence=web)
    consumption = reply.get("web_research_consumption")
    assert consumption is None or consumption.selected_provider_id is None
    if consumption is not None:
        assert consumption.usability is WebResearchConsumptionState.UNAVAILABLE
        assert consumption.admitted_source_count == 0
    assert "yandex" not in str(reply).casefold()


@pytest.mark.asyncio
async def test_missing_prepared_evidence_is_fail_closed() -> None:
    reply = await handle_mixed_file_archive_web_turn(
        user_id="alice",
        actor=_actor(),
        message=_MIXED,
        attachments=_ATTACHMENTS,
        model=_ComparisonModel(),
    )
    assert "доказательства не подготовлены" in reply["message"].casefold()
    assert reply["files"] == []


class _NeverRouter:
    enabled = True
    total_budget_sec = 1.0

    async def chat(self, messages, **kwargs):  # noqa: ANN001, ARG002
        raise AssertionError("mixed dispatch reached a model")


class _NoToolKernel:
    authorization = None

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        return []

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        raise AssertionError("mixed dispatch reached a tool")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        _MIXED,
        "Сравни этот договор с архивом @contract.txt и текущими публичными правилами в интернете.",
        "Сравни этот договор с архивом contract.txt! Текущие публичные правила в интернете.",
        "Сравни этот договор с архивом raw_0123456789abcdef. Текущие публичные правила в интернете.",
    ],
)
async def test_agent_runtime_dispatches_admitted_mixed_turn_fail_closed_without_evidence(
    settings,
    storage,
    message: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_NeverRouter(),
        kernel=_NoToolKernel(),
    )
    reply = await runtime.chat(
        "alice",
        message,
        actor=_actor(),
        attachments=_ATTACHMENTS,
    )
    assert "доказательства не подготовлены" in reply["message"].casefold()
    assert reply["files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector",
    ["raw_ABCDEF0123456789", "raw_0123456789abcdef0", "private deal.txt", "/etc/private.txt"],
)
async def test_runtime_rejects_invalid_mixed_reference_without_generic_web_fallback(
    settings,
    storage,
    selector: str,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    model = _NeverRouter()
    model.chat = AsyncMock(side_effect=AssertionError("unexpected model call"))
    kernel = _NoToolKernel()
    kernel.execute = AsyncMock(side_effect=AssertionError("unexpected tool call"))
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=model, kernel=kernel)
    message = f"Сравни этот файл с архивом {selector} и текущими публичными правилами в интернете."
    reply = await runtime.chat("alice", message, actor=_actor(), attachments=_ATTACHMENTS)
    assert "не принято" in reply["message"].casefold()
    model.chat.assert_not_called()
    kernel.execute.assert_not_called()
