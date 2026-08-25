"""Narrow pre-ingestion ownership for durable conversation/document comparison."""

from __future__ import annotations

import base64
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import friday.agent_runtime as agent_runtime_module
from friday.agent_runtime import AgentRuntime
from friday.file_evidence import stamp_current_turn_file_reference
from friday.interaction_control_plane.compare_conversation_document import (
    DocumentReferenceQuestionKind,
    DocumentReferenceQuestionState,
)
from friday.interaction_control_plane.selected_archive_evidence import SelectedArchiveCorpus
from friday.interaction_control_plane.work_item_contract import WorkState
from friday.orchestration.router import OrchestrationRouter
from friday.pending_durable_turn import PendingDurableTurnAdmission
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext, AuthorizationService

_OWNER = "compare-admission-owner"
_WORK_ID = "work_1212121212121212"
_SELECTED_WORK_ID = "work_3434343434343434"


def _runtime_scope(settings: Any, storage: Any) -> tuple[AgentRuntime, ActorContext, str]:
    storage.ensure_user(_OWNER, preset_key="owner")
    conversation = storage.create_conversation(_OWNER, "comparison admission")
    actor = AuthorizationService(storage).actor_for_user(_OWNER, source="comparison-admission-test")
    return AgentRuntime(settings, storage), actor, str(conversation["id"])


def _comparison(*, state: WorkState = WorkState.WAITING_FOR_INPUT, q1: bool = True) -> Any:
    question = SimpleNamespace(
        kind=(
            DocumentReferenceQuestionKind.PROVIDE_DOCUMENT_REFERENCE
            if q1
            else DocumentReferenceQuestionKind.SELECT_DOCUMENT_CANDIDATE
        ),
        state=DocumentReferenceQuestionState.WAITING,
    )
    return SimpleNamespace(
        id=_WORK_ID,
        revision=1 if q1 and state is WorkState.WAITING_FOR_INPUT else 2,
        state=state,
        document_questions=(question,) if q1 else (SimpleNamespace(), question),
    )


@pytest.mark.parametrize("message", ["report.pdf", "это совсем не имя файла"])
@pytest.mark.parametrize("state", [WorkState.WAITING_FOR_INPUT, WorkState.ACTIVE])
def test_current_comparison_owns_scalar_before_archive_parsers(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    state: WorkState,
) -> None:
    runtime, actor, conversation_id = _runtime_scope(settings, storage)
    monkeypatch.setattr(
        agent_runtime_module,
        "get_current_compare_conversation_with_document_work_item_in_transaction",
        lambda *_args, **_kwargs: _comparison(state=state),
    )

    def forbidden_candidate(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("comparison ownership fell through to archive candidate parsing")

    monkeypatch.setattr(
        agent_runtime_module,
        "get_waiting_archive_candidate_selection_work_item_in_transaction",
        forbidden_candidate,
    )
    admission = runtime.pending_durable_turn_admission(
        _OWNER,
        message,
        actor=actor,
        conversation_id=conversation_id,
    )

    assert isinstance(admission, PendingDurableTurnAdmission)
    assert (admission.work_item_id, admission.revision) == (
        _WORK_ID,
        1 if state is WorkState.WAITING_FOR_INPUT else 2,
    )


@pytest.mark.parametrize(
    ("corpus", "admitted"),
    [
        (SelectedArchiveCorpus.MESSAGES, True),
        (SelectedArchiveCorpus.DOCUMENTS, False),
    ],
)
def test_initial_comparison_followup_binds_only_selected_message_evidence(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    corpus: SelectedArchiveCorpus,
    admitted: bool,
) -> None:
    runtime, actor, conversation_id = _runtime_scope(settings, storage)
    monkeypatch.setattr(
        agent_runtime_module,
        "get_current_compare_conversation_with_document_work_item_in_transaction",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        agent_runtime_module,
        "get_waiting_archive_candidate_selection_work_item_in_transaction",
        lambda *_args, **_kwargs: None,
    )
    selected = SimpleNamespace(
        id=_SELECTED_WORK_ID,
        revision=3,
        selected_evidence=SimpleNamespace(corpus=corpus),
    )
    monkeypatch.setattr(
        agent_runtime_module,
        "get_current_recall_selected_archive_evidence_work_item_in_transaction",
        lambda *_args, **_kwargs: selected,
    )

    admission = runtime.pending_durable_turn_admission(
        _OWNER,
        "Сравни выбранные сообщения с этим документом.",
        actor=actor,
        conversation_id=conversation_id,
    )

    if admitted:
        assert isinstance(admission, PendingDurableTurnAdmission)
        assert (admission.work_item_id, admission.revision) == (_SELECTED_WORK_ID, 3)
    else:
        assert admission is False


@pytest.mark.parametrize(
    ("comparison", "count", "admitted"),
    [
        (_comparison(), 1, True),
        (_comparison(q1=False), 1, False),
        (_comparison(state=WorkState.ACTIVE), 1, False),
        (None, 1, False),
        (_comparison(), 2, False),
    ],
)
def test_only_waiting_comparison_q1_owns_one_current_attachment(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    comparison: Any,
    count: int,
    admitted: bool,
) -> None:
    runtime, actor, conversation_id = _runtime_scope(settings, storage)
    monkeypatch.setattr(
        agent_runtime_module,
        "get_current_compare_conversation_with_document_work_item_in_transaction",
        lambda *_args, **_kwargs: comparison,
    )

    def forbidden_candidate(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("attachment widened archive candidate routing")

    monkeypatch.setattr(
        agent_runtime_module,
        "get_waiting_archive_candidate_selection_work_item_in_transaction",
        forbidden_candidate,
    )
    admission = runtime.pending_durable_turn_admission(
        _OWNER,
        "report.pdf",
        actor=actor,
        conversation_id=conversation_id,
        current_attachment_count=count,
    )

    if admitted:
        assert isinstance(admission, PendingDurableTurnAdmission)
        assert admission.work_item_id == _WORK_ID
    else:
        assert admission is False


class _Carrier(dict[str, Any]):
    pass


def _current_attachment(raw_id: str) -> _Carrier:
    carrier = _Carrier(raw_object_id=raw_id)
    raw = {
        "id": raw_id,
        "source": "upload",
        "source_ref": f"api-document:{raw_id}",
        "content_type": "file",
        "received_at": "2026-08-25T10:00:00+00:00",
        "content_hash": "1" * 64,
        "raw_content": "PRIVATE-CURRENT-BODY",
        "metadata_json": {"uploaded_by": _OWNER},
    }
    return stamp_current_turn_file_reference(carrier, raw)


class _LegacySpy:
    def __init__(self, *, admission: PendingDurableTurnAdmission | bool) -> None:
        self.admission = admission
        self.admission_calls: list[int] = []
        self.chat_calls: list[dict[str, Any]] = []

    def pending_durable_turn_admission(
        self,
        _user_id: str,
        _message: str,
        *,
        actor: ActorContext,
        conversation_id: str | None,
        current_attachment_count: int = 0,
    ) -> PendingDurableTurnAdmission | bool:
        del actor, conversation_id
        self.admission_calls.append(current_attachment_count)
        return self.admission

    async def chat(self, _user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        self.chat_calls.append(kwargs)
        return {"message": "legacy", "context": {"interaction_mode": "dialogue"}}


class _PlannerSpy:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        raise RuntimeError("ordinary route remains available")

    async def plan_attested(self, *_args: Any, **_kwargs: Any) -> Any:
        return await self.plan(*_args, **_kwargs)


@pytest.mark.asyncio
async def test_router_passes_one_current_attachment_and_routes_bound_receipt_to_legacy() -> None:
    conversation_id = "conv_1212121212121212"
    actor = ActorContext(user_id=_OWNER, preset_key="owner", source="test")
    receipt = PendingDurableTurnAdmission.owned(
        person_id=_OWNER,
        conversation_id=conversation_id,
        work_item_id=_WORK_ID,
        revision=1,
    )
    legacy = _LegacySpy(admission=receipt)
    planner = _PlannerSpy()
    router = OrchestrationRouter(legacy, planner, mode="v12")  # type: ignore[arg-type]

    result = await router.chat(
        _OWNER,
        "report.pdf",
        actor=actor,
        conversation_id=conversation_id,
        attachments=[_current_attachment("raw_1212121212121212")],
    )

    assert result["message"] == "legacy"
    assert legacy.admission_calls == [1]
    assert len(legacy.chat_calls) == 1
    assert planner.calls == 0
    assert legacy.chat_calls[0]["_pending_durable_admission"] == receipt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attachments",
    [
        [{"raw_object_id": "raw_5656565656565656"}],
        [
            _current_attachment("raw_7878787878787878"),
            _current_attachment("raw_9090909090909090"),
        ],
    ],
)
async def test_router_leaves_ordinary_or_multiple_attachments_outside_durable_admission(
    attachments: list[dict[str, Any]],
) -> None:
    conversation_id = "conv_3434343434343434"
    actor = ActorContext(user_id=_OWNER, preset_key="owner", source="test")
    legacy = _LegacySpy(admission=True)
    planner = _PlannerSpy()
    router = OrchestrationRouter(legacy, planner, mode="v12")  # type: ignore[arg-type]

    result = await router.chat(
        _OWNER,
        "ordinary attachment question",
        actor=actor,
        conversation_id=conversation_id,
        attachments=attachments,
    )

    assert result["message"] == "legacy"
    assert legacy.admission_calls == []
    assert planner.calls == 1
    assert len(legacy.chat_calls) == 1
    assert "_pending_durable_admission" not in legacy.chat_calls[0]


@pytest.mark.asyncio
async def test_foreign_carried_admission_fails_closed_to_legacy_without_planning() -> None:
    conversation_id = "conv_5656565656565656"
    actor = ActorContext(user_id=_OWNER, preset_key="owner", source="test")
    legacy = _LegacySpy(admission=False)
    planner = _PlannerSpy()
    router = OrchestrationRouter(legacy, planner, mode="v12")  # type: ignore[arg-type]
    stale = PendingDurableTurnAdmission.owned(
        person_id="another-person",
        conversation_id=conversation_id,
        work_item_id=_WORK_ID,
        revision=1,
    )

    result = await router.chat(
        _OWNER,
        "report.pdf",
        actor=actor,
        conversation_id=conversation_id,
        _pending_durable_admission=stale,
    )

    assert result["message"] == "legacy"
    assert legacy.admission_calls == []
    assert planner.calls == 0
    assert len(legacy.chat_calls) == 1
    assert "_pending_durable_admission" not in legacy.chat_calls[0]


def test_api_passes_one_current_attachment_before_text_ingestion(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    app = create_app(replace(settings, router_mode="shadow"))
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    conversation_id = "conv_pending_durable_attachment_api"
    receipt = PendingDurableTurnAdmission.owned(
        person_id=LEGACY_OWNER_USER_ID,
        conversation_id=conversation_id,
        work_item_id=_WORK_ID,
        revision=1,
    )
    admission_calls: list[int] = []
    legacy_calls: list[dict[str, Any]] = []
    planner_calls: list[str] = []

    async def forbidden_ingest(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("owned comparison attachment reached text ingestion")

    async def legacy_chat(_user_id: str, _message: str, **kwargs: Any) -> dict[str, Any]:
        legacy_calls.append(kwargs)
        return {
            "conversation_id": conversation_id,
            "message_id": "msg_1212121212121212",
            "message": "durable comparison attachment",
            "message_format": "plain",
            "tools_used": [],
            "files": [],
            "voice": None,
            "context": {"interaction_mode": "dialogue"},
        }

    async def forbidden_plan(*_args: Any, **_kwargs: Any) -> Any:
        planner_calls.append("called")
        raise AssertionError("owned comparison attachment reached planner")

    with TestClient(app) as client:
        router = app.state.agent
        assert isinstance(router, OrchestrationRouter)
        legacy = router._legacy  # noqa: SLF001 - production composition regression

        def owner_check(
            _person_id: str,
            _message: str,
            *,
            actor: ActorContext,
            conversation_id: str | None,
            current_attachment_count: int = 0,
        ) -> PendingDurableTurnAdmission:
            del actor, conversation_id
            admission_calls.append(current_attachment_count)
            return receipt

        monkeypatch.setattr(legacy, "pending_durable_turn_admission", owner_check)
        monkeypatch.setattr(legacy, "chat", legacy_chat)
        monkeypatch.setattr(router._planner, "plan", forbidden_plan)  # noqa: SLF001
        monkeypatch.setattr(router._planner, "plan_attested", forbidden_plan)  # noqa: SLF001
        monkeypatch.setattr(app.state.ingestion, "ingest_text", forbidden_ingest)
        response = client.post(
            "/api/chat",
            json={
                "message": "report.pdf",
                "conversation_id": conversation_id,
                "document": {
                    "filename": "report.pdf",
                    "mime_type": "application/pdf",
                    "content_base64": base64.b64encode(b"plain current document").decode("ascii"),
                    "source_ref": "api-document:pending-comparison",
                },
            },
            headers=headers,
        )

    assert response.status_code == 200, response.text
    assert admission_calls == [1]
    assert planner_calls == []
    assert len(legacy_calls) == 1
    assert legacy_calls[0]["ingestion_result"] is None
    assert legacy_calls[0]["_pending_durable_admission"] == receipt
