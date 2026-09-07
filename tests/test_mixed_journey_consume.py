from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

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
    assert organs == {"file", "archive", "web", "table"}
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
    assert set(observed.view.organs.present_organs) == {"file", "archive", "web", "table"}
    assert mixed_status_admitted(observed) is True
    assert model.acquire_calls == 1
    consumption = reply["web_research_consumption"]
    assert consumption.selected_provider_id == "brave"
    assert consumption.usability is WebResearchConsumptionState.CONSUMABLE
    assert consumption.reason is WebResearchConsumptionReason.PRIMARY_SOURCES


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
    assert consumption.reason is WebResearchConsumptionReason.NO_ADMITTED_SOURCES


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
async def test_agent_runtime_dispatches_admitted_mixed_turn_fail_closed_without_evidence(
    settings,
    storage,
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
        _MIXED,
        actor=_actor(),
        attachments=_ATTACHMENTS,
    )
    assert "доказательства не подготовлены" in reply["message"].casefold()
    assert reply["files"] == []
