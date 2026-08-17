"""One request clock bounds every later model stage."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect
import textwrap
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday import agent_runtime as agent_runtime_module
from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _file_evidence_set_from_attachments,
    _maybe_bounded_file_overview,
    _OwnedAttachment,
    _stamp_file_evidence,
)
from friday.execution_kernel import ToolResult, ToolSpec
from friday.permissions import ActorContext
from friday.server import create_app
from friday.source_identity import authorized_file_snapshot_token, raw_source_identity_sha256


class _DisabledModel:
    enabled = False
    total_budget_sec = 100.0


class _DeadlineProbeModel:
    enabled = True
    total_budget_sec = 100.0

    def __init__(self) -> None:
        self.tools_seen: list[list[dict[str, Any]] | None] = []

    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.tools_seen.append(tools)
        if tools:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-too-late",
                        "function": {"name": "noop_tool", "arguments": "{}"},
                    }
                ],
                "_queue_wait_sec": 0.0,
            }
        return {"content": "bounded final", "tool_calls": None, "_queue_wait_sec": 0.0}


class _NoopKernel:
    @staticmethod
    def get_tool_definitions(_actor, topic=""):  # noqa: ANN001, ARG004
        del topic
        return []

    async def execute(self, name, _arguments, *, actor=None):  # noqa: ANN001
        del actor
        return ToolResult(name, True, data={})


class _ThreeToolModel:
    enabled = True
    total_budget_sec = 1.0

    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
        if tools:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call-{index}",
                        "function": {"name": "slow_mutator", "arguments": "{}"},
                    }
                    for index in range(3)
                ],
                "_queue_wait_sec": 0.0,
            }
        return {"content": "too late", "tool_calls": None, "_queue_wait_sec": 0.0}


class _SlowMutatingKernel:
    def __init__(self, clock: dict[str, float]) -> None:
        self.clock = clock
        self.started: list[str] = []
        self.completed: list[str] = []

    async def execute(self, name, _arguments, *, actor=None):  # noqa: ANN001
        del actor
        self.started.append(name)
        # An entered mutator is allowed to finish, even though it carries the
        # monotonic clock beyond the request wall.
        self.clock["now"] = 11.0
        self.completed.append(name)
        return ToolResult(name, True, data={"committed": True})

    @staticmethod
    def get_tool(name: str) -> ToolSpec:
        return ToolSpec(
            name=name,
            description="synthetic mutator",
            parameters={"type": "object"},
            security_id="synthetic.mutate",
            risk="mutate",
        )


class _RecordingEffectKernel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, name, _arguments, *, actor=None):  # noqa: ANN001
        del actor
        self.calls.append(name)
        return ToolResult(name, True, data={"created": True}, attachment={"filename": "late.bin"})


def _expired_context(*, conversation_id: str = "expired") -> AgentContext:
    return AgentContext(
        conversation_id=conversation_id,
        user_id="alice",
        person_id="alice",
        interaction_mode="dialogue",
        turn_deadline=time.monotonic() - 1.0,
    )


def _schema(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "parameters": {"type": "object", "properties": {}}},
    }


@pytest.mark.asyncio
async def test_expired_turn_starts_no_late_file_voice_or_reminder_effect(settings) -> None:
    kernel = _RecordingEffectKernel()
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = kernel
    runtime.llm = _DisabledModel()
    runtime.settings = settings
    context = _expired_context()
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    made = await runtime._file_for_a_request_that_wanted_one(  # noqa: SLF001
        "Сделай ответ файлом txt",
        "Заголовок\n\nПервый содержательный абзац.\n\nВторой содержательный абзац.",
        actor,
        context=context,
    )
    spoken = await runtime._voice_of_the_final_answer(  # noqa: SLF001
        None,
        "Безопасный синтетический ответ",
        warning="",
        caution="",
        actor=actor,
        asked_for_voice=True,
        turn_deadline=context.turn_deadline,
    )
    tools = [_schema("remind")]
    reminded = await runtime._prefetch_a_reminder_if_asked(  # noqa: SLF001
        "Поставь напоминание на 5 сентября 2035 года с текстом «сдать отчёт».",
        context,
        actor,
        tools,
        [],
        [],
        [],
    )

    assert made is None and spoken is None and reminded is False
    assert context.late_make_file_attempts == 0
    assert kernel.calls == []


@pytest.mark.asyncio
async def test_expired_exact_workspace_turn_starts_no_workspace_create(settings) -> None:
    kernel = _RecordingEffectKernel()
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = kernel
    runtime.llm = _DisabledModel()
    runtime.settings = settings
    prompt = (
        "Используй именно workspace_create и создай в MCP outbox файл mcp-metadata.txt. "
        "Первая строка — только значение номера документа без подписи. Вторая строка — "
        "только значение контрольного маркера без подписи. Никаких других строк."
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        _expired_context(conversation_id="expired-workspace"),
        prompt,
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        [_schema("workspace_create")],
        None,
        workspace_authority_message=prompt,
        workspace_exact_content="DOC-42\nCONTROL-MARKER\n",
        workspace_exact_direct_authorized=True,
    )

    assert kernel.calls == []
    assert result["tools_used"] == []
    assert "Файл не создан" in result["content"]


@pytest.mark.asyncio
async def test_expired_archive_collection_starts_no_mutator_or_file_clip() -> None:
    kernel = _RecordingEffectKernel()
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = kernel
    context = _expired_context(conversation_id="expired-archive-collection")
    context.outward_verdict = ("файл", "10,13,25")
    clips: list[dict[str, Any]] = []
    used: list[str] = []

    collected = await runtime._prefetch_the_archive_if_asked(  # noqa: SLF001
        context,
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        [],
        used,
        [],
        clips,
        [_schema("collect_files")],
        message="Собери присланные файлы за 10, 13 и 25 число",
    )

    assert collected is False
    assert context.asked_for_an_archive is True
    assert kernel.calls == []
    assert clips == [] and used == []


@pytest.mark.asyncio
async def test_expired_read_only_prefetch_cluster_starts_zero_kernel_calls(
    settings,
    storage,
) -> None:
    storage.ensure_user("alice", preset_key="owner", display_name="Owner")
    storage.ensure_user("jbl", preset_key="user", display_name="JBL", username="jbl")
    kernel = _RecordingEffectKernel()
    runtime = AgentRuntime(settings, storage, llm=_DisabledModel(), kernel=kernel)
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")

    source_context = _expired_context(conversation_id="expired-source-prefetch")
    source_context.source_search_query = "иванов"
    source_context.source_search_focus = "иванов должност"
    source_owned = await runtime._prefetch_archived_source_if_asked(  # noqa: SLF001
        "Найди в моих загруженных источниках должность Иванова",
        actor,
        [_schema("source_search")],
        [],
        [],
        [],
        source_context,
        None,
        authorized=True,
    )
    assert source_owned is True

    own_message_used: list[str] = []
    own_message_owned = await runtime._prefetch_own_messages(  # noqa: SLF001
        "О чём мы вчера говорили?",
        actor,
        [_schema("message_search")],
        [],
        own_message_used,
        [],
        turn_deadline=time.monotonic() - 1.0,
    )
    assert own_message_owned is False and own_message_used == []

    person_owned = await runtime._prefetch_person_activity(  # noqa: SLF001
        "Что писал JBL?",
        actor,
        [_schema("user_activity")],
        [],
        [],
        [],
        _expired_context(conversation_id="expired-person-prefetch"),
    )
    assert person_owned is False

    archive_used: list[str] = []
    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        "Сколько всего файлов в моём архиве?",
        actor,
        [_schema("kg_stats")],
        [],
        archive_used,
        [],
        _expired_context(conversation_id="expired-archive-stats"),
    )
    assert archive_used == []

    timeline_used: list[str] = []
    await runtime._prefetch_the_timeline_if_asked(  # noqa: SLF001
        "Что было вчера?",
        actor,
        [_schema("what_happened"), _schema("upcoming")],
        [],
        timeline_used,
        [],
        _expired_context(conversation_id="expired-timeline"),
    )
    assert timeline_used == []
    assert kernel.calls == []


@pytest.mark.asyncio
async def test_archive_prefetch_rechecks_deadline_before_second_sibling_read(
    settings,
    storage,
    monkeypatch,
) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(agent_runtime_module.time, "monotonic", lambda: clock["now"])

    class _FirstReadSpendsTurn:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute(self, name, _arguments, *, actor=None):  # noqa: ANN001
            del actor
            self.calls.append(name)
            if name != "kg_stats":
                raise AssertionError("deadline admitted a second sibling read")
            clock["now"] = 10.0
            return ToolResult(
                name,
                True,
                data={
                    "knowledge_object_count": 7,
                    "raw_object_count": 5,
                    "file_count": 3,
                    "entity_count": 2,
                    "relation_count": 1,
                },
            )

    kernel = _FirstReadSpendsTurn()
    runtime = AgentRuntime(settings, storage, llm=_DisabledModel(), kernel=kernel)

    async def no_remainder(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_settle_structural_remainder", no_remainder)
    context = AgentContext(
        conversation_id="archive-sibling-deadline",
        user_id="alice",
        person_id="alice",
        outward_verdict=("архив", None),
        turn_deadline=10.0,
    )
    used: list[str] = []

    await runtime._prefetch_archive_numbers(  # noqa: SLF001
        "Покажи теги и скажи, сколько всего файлов в архиве.",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        [_schema("kg_stats"), _schema("list_tags")],
        [],
        used,
        [],
        context,
    )

    assert kernel.calls == ["kg_stats"]
    assert used == ["kg_stats"]


@pytest.mark.asyncio
async def test_trusted_upload_overview_uses_remaining_admission_clock_and_cancels() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _HangingOverview:
        enabled = True

        async def chat(self, *_args, **_kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    item = _OwnedAttachment(
        {
            "raw_object_id": "raw_deadline_overview_aaaaaaaaaaaa",
            "filename": "trusted.txt",
            "transient_text": "Первая проверенная строка. Вторая проверенная строка.",
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
            "text_truncated": False,
            "extraction_truncated": False,
        }
    )
    view = agent_runtime_module._build_file_evidence_view(item)  # noqa: SLF001
    assert view is not None
    _stamp_file_evidence(item, view)
    evidence = _file_evidence_set_from_attachments([item], expected_count=1)
    fallback = "Файл зарегистрирован; состояние чтения указано ниже."
    started_at = time.monotonic()

    answer, used = await _maybe_bounded_file_overview(
        _HangingOverview(),
        fallback,
        [item],
        evidence_set=evidence,
        turn_deadline=time.monotonic() + 0.03,
    )

    assert time.monotonic() - started_at < 0.5
    assert started.is_set() and cancelled.is_set()
    assert answer == fallback and used is False


@pytest.mark.asyncio
async def test_served_model_discovery_does_not_block_loop_past_turn_deadline(
    settings,
    monkeypatch,
) -> None:
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.settings = settings

    def blocking_name() -> str:
        started.set()
        try:
            release.wait(timeout=2.0)
            return "too-late-model"
        finally:
            finished.set()

    async def forbidden_remainder(*_args, **_kwargs):
        raise AssertionError("a later model/remainder stage started after expiry")

    monkeypatch.setattr(runtime, "_served_model_name", blocking_name)
    monkeypatch.setattr(runtime, "_remainder_after", forbidden_remainder)
    context = AgentContext(
        conversation_id="served-model-deadline",
        user_id="alice",
        turn_deadline=time.monotonic() + 0.03,
    )
    try:
        started_at = time.monotonic()
        await runtime._say_what_i_am_if_asked("Какая ты модель?", context)  # noqa: SLF001
        assert time.monotonic() - started_at < 0.5
        assert started.is_set()
        assert settings.llm_model in context.structural_answer
        assert "too-late-model" not in context.structural_answer
        assert context.remainder_known is True and context.open_remainder == ""
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1.0)


@pytest.mark.asyncio
async def test_chat_passes_its_entry_deadline_into_context_preparation(
    settings,
    storage,
    monkeypatch,
) -> None:
    """A long pre-context stage cannot cause a new clock after it returns."""

    clock = {"now": 10.0}
    monkeypatch.setattr(agent_runtime_module.time, "monotonic", lambda: clock["now"])
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(settings, storage, llm=_DisabledModel(), kernel=_NoopKernel())
    observed: dict[str, Any] = {}

    async def delayed_prepare(user_id, _message, conversation_id, **kwargs):  # noqa: ANN001
        observed["deadline_argument"] = kwargs.get("turn_deadline")
        clock["now"] += 80.0
        context = AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            turn_deadline=kwargs.get("turn_deadline"),
        )
        observed["context"] = context
        return context

    monkeypatch.setattr(runtime, "_prepare_context", delayed_prepare)

    await runtime.chat(
        "alice",
        "Объясни устройство синтетического механизма подробно",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
    )

    expected = 10.0 + settings.agent_turn_budget_sec
    assert observed["deadline_argument"] == expected
    assert observed["context"].turn_deadline == expected
    assert clock["now"] == 90.0


@pytest.mark.asyncio
async def test_chat_preserves_an_earlier_admission_deadline(
    settings,
    storage,
    monkeypatch,
) -> None:
    clock = {"now": 10.0}
    monkeypatch.setattr(agent_runtime_module.time, "monotonic", lambda: clock["now"])
    storage.ensure_user("alice", preset_key="owner")
    runtime = AgentRuntime(settings, storage, llm=_DisabledModel(), kernel=_NoopKernel())
    observed: dict[str, Any] = {}

    async def delayed_prepare(user_id, _message, conversation_id, **kwargs):  # noqa: ANN001
        observed["deadline"] = kwargs.get("turn_deadline")
        clock["now"] = 24.0
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            turn_deadline=kwargs.get("turn_deadline"),
        )

    monkeypatch.setattr(runtime, "_prepare_context", delayed_prepare)
    await runtime.chat(
        "alice",
        "Объясни синтетический механизм",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        turn_deadline=25.0,
    )

    assert observed["deadline"] == 25.0


@pytest.mark.asyncio
async def test_agentic_loop_does_not_renew_a_deadline_spent_before_the_loop(
    settings,
    storage,
    monkeypatch,
) -> None:
    """150 seconds of pre-loop work leave 50, not a fresh 200-second loop."""

    clock = {"now": 150.0}
    monkeypatch.setattr(agent_runtime_module.time, "monotonic", lambda: clock["now"])
    storage.ensure_user("alice", preset_key="owner")
    model = _DeadlineProbeModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=_NoopKernel(),
    )
    context = AgentContext(
        conversation_id="fixed-turn-clock",
        user_id="alice",
        person_id="alice",
        turn_deadline=200.0,
    )

    result = await runtime._agentic_loop(  # noqa: SLF001
        context,
        "синтетический вопрос",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        [
            {
                "type": "function",
                "function": {"name": "noop_tool", "parameters": {"type": "object"}},
            }
        ],
        None,
    )

    assert model.tools_seen == [[]], "a fresh loop clock admitted a tool round after pre-loop work"
    assert result["content"] == "bounded final"
    assert context.turn_deadline == 200.0


def test_absolute_turn_clock_keeps_bounded_attachment_stage_limits(
    settings,
    storage,
    monkeypatch,
) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(agent_runtime_module.time, "monotonic", lambda: clock["now"])
    runtime = AgentRuntime(replace(settings, llm_timeout_sec=240.0), storage)

    roomy = AgentContext(
        conversation_id="roomy",
        user_id="alice",
        current_attachment_present=True,
        turn_deadline=1_000.0,
    )
    assert runtime._ensure_attachment_prepass_deadline(  # noqa: SLF001
        roomy,
        requested_budget_sec=480.0,
    ) == pytest.approx(480.0)
    assert runtime._ensure_attachment_primary_deadline(roomy) == pytest.approx(180.0)  # noqa: SLF001

    nearly_spent = AgentContext(
        conversation_id="nearly-spent",
        user_id="alice",
        current_attachment_present=True,
        turn_deadline=50.0,
    )
    assert runtime._ensure_attachment_primary_deadline(nearly_spent) == pytest.approx(50.0)  # noqa: SLF001


@pytest.mark.asyncio
async def test_attachment_primary_applies_only_its_missing_output_budget(
    settings,
    storage,
) -> None:
    """The central attachment boundary caps omissions without rewriting specialists."""

    class RecordingModel:
        enabled = True
        total_budget_sec = 30.0

        def __init__(self) -> None:
            self.kwargs: list[dict[str, Any]] = []

        async def chat(self, _messages, **kwargs):  # noqa: ANN001
            self.kwargs.append(dict(kwargs))
            return {"content": "bounded"}

    model = RecordingModel()
    runtime = AgentRuntime(settings, storage, llm=model)
    attachment = AgentContext(
        conversation_id="bounded-attachment-answer",
        user_id="alice",
        current_attachment_present=True,
    )
    ordinary = AgentContext(
        conversation_id="ordinary-answer",
        user_id="alice",
        current_attachment_present=False,
    )

    await runtime._attachment_primary_chat(attachment, [], tools=[])  # noqa: SLF001
    await runtime._attachment_primary_chat(  # noqa: SLF001
        attachment,
        [],
        tools=[],
        max_tokens=900,
    )
    await runtime._attachment_primary_chat(ordinary, [], tools=[])  # noqa: SLF001

    assert model.kwargs[0]["max_tokens"] == agent_runtime_module._ATTACHMENT_PRIMARY_MODEL_OUTPUT_TOKENS
    assert model.kwargs[1]["max_tokens"] == 900
    assert "max_tokens" not in model.kwargs[2]


@pytest.mark.asyncio
async def test_direct_attachment_route_sets_the_answer_budget_before_transport(
    settings,
    storage,
) -> None:
    """The real direct-answer branch, not only its helper, owns the ceiling."""

    class RecordingModel:
        enabled = True
        total_budget_sec = 30.0

        def __init__(self) -> None:
            self.kwargs: list[dict[str, Any]] = []

        async def chat(self, _messages, **kwargs):  # noqa: ANN001
            self.kwargs.append(dict(kwargs))
            return {"content": "bounded attachment answer"}

    model = RecordingModel()
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=model)
    context = AgentContext(
        conversation_id="direct-attachment-route",
        user_id="alice",
        current_attachment_present=False,
    )

    result = await runtime._generate_response(  # noqa: SLF001
        context,
        "Загружен документ: synthetic.odt",
        [{"filename": "synthetic.odt", "transient_text": "alpha beta gamma"}],
    )

    assert result["content"] == "bounded attachment answer"
    assert context.current_attachment_present is True
    assert len(model.kwargs) == 1
    assert model.kwargs[0]["max_tokens"] == agent_runtime_module._ATTACHMENT_PRIMARY_MODEL_OUTPUT_TOKENS


@pytest.mark.asyncio
async def test_expiry_after_first_selected_mutator_does_not_start_its_siblings(
    settings,
    storage,
    monkeypatch,
) -> None:
    clock = {"now": 0.0}
    monkeypatch.setattr(agent_runtime_module.time, "monotonic", lambda: clock["now"])
    storage.ensure_user("alice", preset_key="owner")
    kernel = _SlowMutatingKernel(clock)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_ThreeToolModel(),
        kernel=kernel,
    )
    context = AgentContext(
        conversation_id="three-selected-effects",
        user_id="alice",
        person_id="alice",
        turn_deadline=10.0,
    )

    await runtime._agentic_loop(  # noqa: SLF001
        context,
        "выполни три синтетических действия",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        [
            {
                "type": "function",
                "function": {"name": "slow_mutator", "parameters": {"type": "object"}},
            }
        ],
        None,
    )

    assert kernel.started == ["slow_mutator"]
    assert kernel.completed == ["slow_mutator"]


@pytest.mark.asyncio
async def test_agentic_loop_cancels_an_entered_observe_tool_at_the_turn_wall(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class _ObserveKernel:
        @staticmethod
        def get_tool(name: str) -> ToolSpec:
            return ToolSpec(
                name=name,
                description="synthetic observation",
                parameters={"type": "object"},
                security_id="synthetic.observe",
                risk="observe",
            )

        async def execute(self, name, _arguments, *, actor=None):  # noqa: ANN001
            del name, actor
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    class _OneObserveModel:
        enabled = True
        total_budget_sec = 0.01

        async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
            if tools:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "observe-deadline",
                            "function": {"name": "slow_observe", "arguments": "{}"},
                        }
                    ],
                    "_queue_wait_sec": 0.0,
                }
            return {"content": "", "tool_calls": None, "_queue_wait_sec": 0.0}

    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_OneObserveModel(),
        kernel=_ObserveKernel(),
    )
    context = AgentContext(
        conversation_id="observe-tool-deadline",
        user_id="alice",
        person_id="alice",
        turn_deadline=time.monotonic() + 0.03,
    )

    await runtime._agentic_loop(  # noqa: SLF001
        context,
        "выполни синтетическое чтение",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        [_schema("slow_observe")],
        None,
    )

    assert started.is_set() and cancelled.is_set()


def test_api_chat_passes_one_admission_deadline_through_ingestion_and_agent(
    settings,
    monkeypatch,
) -> None:
    app = create_app(settings)
    observed: dict[str, float] = {}

    with TestClient(app) as client:

        async def ingest_text(*_args, **kwargs):  # noqa: ANN002
            observed["ingestion_deadline"] = kwargs["turn_deadline"]
            observed["ingestion_started"] = time.monotonic()
            # Represent admission/recovery work before AgentRuntime starts.
            import asyncio

            await asyncio.sleep(0.03)
            return {"promoted": False, "queued_for_review": False, "action": "transient"}

        async def agent_chat(user_id, _message, **kwargs):  # noqa: ANN001
            observed["agent_deadline"] = kwargs["turn_deadline"]
            observed["agent_started"] = time.monotonic()
            conversation_id = str(kwargs.get("conversation_id") or "")
            if not conversation_id:
                conversation_id = str(app.state.storage.create_conversation(user_id)["id"])
            return {
                "conversation_id": conversation_id,
                "message": "ok",
                "context": {"interaction_mode": "dialogue"},
            }

        monkeypatch.setattr(app.state.ingestion, "ingest_text", ingest_text)
        monkeypatch.setattr(app.state.agent, "chat", agent_chat)
        response = client.post(
            "/api/chat",
            json={"message": "synthetic admission deadline"},
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )

    assert response.status_code == 200, response.text
    assert observed["ingestion_deadline"] == observed["agent_deadline"]
    admission_deadline = observed["agent_deadline"]
    remaining_at_agent = admission_deadline - observed["agent_started"]
    assert remaining_at_agent < settings.agent_turn_budget_sec - 0.02
    assert observed["agent_started"] > observed["ingestion_started"]


def test_every_chat_route_ingestion_and_runtime_call_forwards_the_admission_clock() -> None:
    """Deletion-sensitive guard for recovery, no-save, text, and regenerate seams."""

    tree = ast.parse(textwrap.dedent(inspect.getsource(create_app)))
    guarded_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"ingest_file", "inspect_file_transient", "ingest_text", "chat"}
        and (
            node.func.attr != "chat"
            or isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "agent"
        )
    ]

    assert guarded_calls
    assert all(any(keyword.arg == "turn_deadline" for keyword in call.keywords) for call in guarded_calls)


@pytest.mark.asyncio
async def test_context_retrieval_timeout_cancels_and_joins_the_parallel_arbiter(
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("alice", preset_key="owner")
    conversation = storage.create_conversation("alice")
    runtime = AgentRuntime(settings, storage, llm=_DeadlineProbeModel(), kernel=_NoopKernel())
    search_started = asyncio.Event()
    search_cancelled = asyncio.Event()
    arbiter_started = asyncio.Event()
    arbiter_cancelled = asyncio.Event()

    class _HangingSearcher:
        async def search(self, *_args, **_kwargs):
            search_started.set()
            try:
                await asyncio.Future()
            finally:
                search_cancelled.set()

    async def hanging_arbiter(*_args, **_kwargs):
        arbiter_started.set()
        try:
            await asyncio.Future()
        finally:
            arbiter_cancelled.set()

    monkeypatch.setattr(runtime, "_turn_web_query_by_arbiter", hanging_arbiter)
    started = time.monotonic()
    context = await runtime._prepare_context(  # noqa: SLF001
        "alice",
        "Объясни устройство синтетического протокола подробно",
        str(conversation["id"]),
        prior_history=[],
        searcher=_HangingSearcher(),
        turn_deadline=time.monotonic() + 0.03,
    )

    assert time.monotonic() - started < 0.5
    assert search_started.is_set() and search_cancelled.is_set()
    assert arbiter_started.is_set() and arbiter_cancelled.is_set()
    assert context.knowledge_hits == []
    assert context.outward_verdict is None


@pytest.mark.asyncio
async def test_registered_file_authorization_await_is_clipped_before_parser_starts(
    settings,
    monkeypatch,
) -> None:
    release = threading.Event()
    read_started = threading.Event()
    read_finished = threading.Event()
    parser_calls: list[bool] = []

    def blocking_read(*_args, **_kwargs):
        read_started.set()
        try:
            release.wait(timeout=2.0)
            return SimpleNamespace(content=b"body", filename="legacy.txt", mime_type="text/plain")
        finally:
            read_finished.set()

    async def forbidden_parser(*_args, **_kwargs):
        parser_calls.append(True)
        raise AssertionError("parser started after the authorization deadline")

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.storage = object()
    runtime.settings = SimpleNamespace(
        files_dir=settings.files_dir,
        max_upload_bytes=settings.max_upload_bytes,
    )
    runtime.kernel = SimpleNamespace(ingestion=SimpleNamespace(inspect_file_transient=forbidden_parser))

    def canonical(raw_id: str, **_kwargs):
        return agent_runtime_module._OwnedAttachment(  # noqa: SLF001
            {
                "raw_object_id": raw_id,
                "filename": "legacy.txt",
                "mime_type": "text/plain",
                "transient_text": "",
                "extraction_success": False,
                "_registered_file_record": "valid",
            }
        )

    runtime._owned_file_attachment = canonical  # type: ignore[method-assign]  # noqa: SLF001
    items = [canonical("raw_deadline_one"), canonical("raw_deadline_two")]
    monkeypatch.setattr(agent_runtime_module, "read_authorized_file", blocking_read)
    try:
        started = time.monotonic()
        result = await runtime._verify_registered_file_attachments(  # noqa: SLF001
            items,
            tenant_id="alice",
            person_id="alice",
            turn_deadline=time.monotonic() + 0.03,
        )
        assert time.monotonic() - started < 0.5
        assert read_started.is_set()
        assert parser_calls == []
        assert len(result) == 2
        assert all(item.get("_registered_file_bytes_verified") is False for item in result)
    finally:
        release.set()
        if read_started.is_set():
            assert await asyncio.to_thread(read_finished.wait, 1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("verify", "metadata"))
async def test_legacy_file_inspection_receives_and_obeys_the_same_turn_deadline(
    settings,
    monkeypatch,
    operation: str,
) -> None:
    inspection_started = asyncio.Event()
    inspection_cancelled = asyncio.Event()
    seen_deadlines: list[float | None] = []

    async def hanging_inspection(*_args, turn_deadline=None, **_kwargs):
        seen_deadlines.append(turn_deadline)
        inspection_started.set()
        try:
            await asyncio.Future()
        finally:
            inspection_cancelled.set()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.storage = object()
    runtime.settings = SimpleNamespace(
        files_dir=settings.files_dir,
        max_upload_bytes=settings.max_upload_bytes,
    )
    runtime.kernel = SimpleNamespace(ingestion=SimpleNamespace(inspect_file_transient=hanging_inspection))

    body = b"body"
    body_sha256 = hashlib.sha256(body).hexdigest()

    def raw_projection(raw_id: str) -> dict[str, str]:
        return {
            "id": raw_id,
            "source": "upload",
            "source_ref": f"deadline:{raw_id}",
            "content_type": "file",
            "received_at": "2026-08-14T00:00:00+00:00",
            "content_hash": body_sha256,
            "_raw_content": "",
            "_raw_metadata": "{}",
        }

    def canonical(raw_id: str, **_kwargs):
        raw = raw_projection(raw_id)
        return agent_runtime_module._OwnedAttachment(  # noqa: SLF001
            {
                "raw_object_id": raw_id,
                "filename": "legacy.doc",
                "mime_type": "application/msword",
                "transient_text": "",
                "extraction_success": False,
                "_registered_file_record": "valid",
                agent_runtime_module._RAW_SOURCE_IDENTITY_KEY: raw_source_identity_sha256(raw),  # noqa: SLF001
            }
        )

    def authorized(*args, **_kwargs):  # noqa: ANN002, ANN003
        del _kwargs
        raw_id = str(args[2])
        token = authorized_file_snapshot_token(
            raw_projection(raw_id),
            content_sha256=body_sha256,
        )
        assert token is not None
        return SimpleNamespace(
            content=body,
            filename="legacy.doc",
            mime_type="application/msword",
            snapshot_token=token,
        )

    runtime._owned_file_attachment = canonical  # type: ignore[method-assign]  # noqa: SLF001
    monkeypatch.setattr(
        agent_runtime_module,
        "read_authorized_file",
        authorized,
    )
    items = [canonical("raw_inspect_one"), canonical("raw_inspect_two")]
    deadline = time.monotonic() + 0.03
    if operation == "verify":
        result = await runtime._verify_registered_file_attachments(  # noqa: SLF001
            items,
            tenant_id="alice",
            person_id="alice",
            turn_deadline=deadline,
        )
    else:
        result = await runtime._hydrate_legacy_document_metadata(  # noqa: SLF001
            items,
            tenant_id="alice",
            person_id="alice",
            turn_deadline=deadline,
        )

    assert len(result) == 2
    assert inspection_started.is_set() and inspection_cancelled.is_set()
    assert seen_deadlines == [deadline]


@pytest.mark.asyncio
async def test_workspace_pagination_uses_one_total_deadline_and_starts_no_second_page() -> None:
    cancelled = asyncio.Event()

    class _HangingWorkspaceKernel:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, *_args, **_kwargs):
            self.calls += 1
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _HangingWorkspaceKernel()
    started = time.monotonic()
    rows, complete, attempts = await runtime._workspace_listing(  # noqa: SLF001
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        tool_name="workspace_list",
        arguments={"relative_dir": "", "recursive": True},
        turn_deadline=time.monotonic() + 0.03,
    )

    assert time.monotonic() - started < 0.5
    assert rows == [] and complete is False
    assert attempts == ("workspace_list",)
    assert runtime.kernel.calls == 1
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_timed_out_local_person_lookup_fails_local_and_never_calls_web(
    monkeypatch,
) -> None:
    release = threading.Event()
    lookup_started = threading.Event()
    lookup_finished = threading.Event()

    class _Graph:
        def search_entities(self, *_args, **_kwargs):
            lookup_started.set()
            try:
                release.wait(timeout=2.0)
                return []
            finally:
                lookup_finished.set()

    class _Kernel:
        def __init__(self) -> None:
            self.kg = _Graph()
            self.calls: list[str] = []

        async def execute(self, name, *_args, **_kwargs):
            self.calls.append(name)
            return ToolResult(name, True, data={})

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _Kernel()
    context = AgentContext(
        conversation_id="local-privacy-timeout",
        user_id="alice",
        person_id="alice",
        outward_verdict=("интернет", "Хасанова"),
        turn_deadline=time.monotonic() + 0.03,
    )
    try:
        await runtime._prefetch_the_web_if_asked(  # noqa: SLF001
            "Расскажи про Хасанову",
            ActorContext(user_id="alice", preset_key="owner", source="test"),
            [{"function": {"name": "web_research"}}],
            [],
            [],
            [],
            [],
            context,
        )
        assert lookup_started.is_set()
        assert runtime.kernel.calls == []
    finally:
        release.set()
        if lookup_started.is_set():
            assert await asyncio.to_thread(lookup_finished.wait, 1.0)


@pytest.mark.asyncio
async def test_expired_explicit_news_turn_starts_no_web_research() -> None:
    class _RecordingWebKernel:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute(self, name, *_args, **_kwargs):
            self.calls.append(name)
            return ToolResult(name, True, data={})

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.kernel = _RecordingWebKernel()
    context = AgentContext(
        conversation_id="expired-explicit-news",
        user_id="alice",
        person_id="alice",
        isolated_outbound_turn=True,
        outward_verdict=("интернет", "latest synthetic public news"),
        turn_deadline=time.monotonic() - 1.0,
    )
    tools_used: list[str] = []

    await runtime._prefetch_the_web_if_asked(  # noqa: SLF001
        "Найди в интернете последние синтетические новости",
        ActorContext(user_id="alice", preset_key="owner", source="test"),
        [{"function": {"name": "web_research"}}],
        [],
        tools_used,
        [],
        [],
        context,
    )

    assert runtime.kernel.calls == []
    assert tools_used == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("learner_name", "verdict_kind", "listing_name"),
    [
        ("_learn_a_standing_rule", "правило", "_standing_rules"),
        ("_learn_a_correction", "поправка", "_corrections"),
    ],
)
async def test_expired_optional_rule_arbitration_falls_back_without_http_failure(
    learner_name: str,
    verdict_kind: str,
    listing_name: str,
) -> None:
    runtime = AgentRuntime.__new__(AgentRuntime)
    setattr(runtime, listing_name, lambda _user_id: [])
    arbiter_started = False

    async def arbiter(*_args, **_kwargs):
        nonlocal arbiter_started
        arbiter_started = True
        return "", "", "", None

    runtime._standing_rule_by_arbiter = arbiter  # type: ignore[method-assign]  # noqa: SLF001
    context = AgentContext(
        conversation_id="expired-rule-arbitration",
        user_id="alice",
        person_id="alice",
        outward_verdict=(verdict_kind, "candidate"),
        turn_deadline=time.monotonic() - 1.0,
    )

    handled = await getattr(runtime, learner_name)("synthetic instruction", context)

    assert handled is False
    assert arbiter_started is False
    assert context.structural_answer == ""
