"""Final publication reauthorizes every private attachment source.

These tests deliberately mutate durable authority only after Friday has read a
registered source: inside a primary model callback, after a structural renderer,
or at an explicit post-verification seam.  The assistant answer has not yet been
published or persisted.  A stale capability/ownership snapshot must therefore
close the whole answer instead of leaking model prose or a derived carrier.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime, _historical_direct_read_attachment
from friday.documents import DocumentResult
from friday.execution_kernel import ToolResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext, AuthorizationService
from friday.storage.models import InboxStatus
from friday.web_surfer import WebSurfer

_SOURCE_CANARY = "SOURCE-PUBLICATION-AUTHORITY-CANARY-814"
_MODEL_CANARY = "MODEL-PUBLICATION-AUTHORITY-CANARY-814"
_CARRIER_CANARY = "CARRIER-PUBLICATION-AUTHORITY-CANARY-814"
_SOURCE_DERIVED_REMINDER_DATE = "31 декабря 2035"
_SOURCE_DERIVED_REMINDER_WHEN = "2035-12-31"
_ATTACHMENT_CHUNK_PREFIX = "FRIDAY_ATTACHMENT_CHUNK_DATA"
_ATTACHMENT_REDUCE_PREFIX = "FRIDAY_ATTACHMENT_REDUCE_DATA"
_NORMAL_REVIEW = (
    "## Подробное ревью\n\n"
    f"Документ содержит контрольную строку `{_SOURCE_CANARY}`.\n\n"
    f"Итог модели: `{_MODEL_CANARY}`."
)
_AUTHORITY_CHANGED_ISSUE = "attachment_authority_changed_before_publication"


class _MutatingReviewModel:
    enabled = True
    model = "publication-reauthorization-mutation-spy"
    total_budget_sec = 3.0

    def __init__(self, mutate: Callable[[], None] | None = None) -> None:
        self.mutate = mutate
        self.prepass_calls = 0
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        copied = [dict(item) for item in messages]
        prompt = "\n".join(str(item.get("content") or "") for item in copied)
        assert _SOURCE_CANARY in prompt, "mutation ran before the registered source reached the model"
        if _ATTACHMENT_CHUNK_PREFIX in prompt or _ATTACHMENT_REDUCE_PREFIX in prompt:
            self.prepass_calls += 1
            return {
                "content": (
                    f"Сводка источника: {_SOURCE_CANARY}; {_CARRIER_CANARY}; "
                    f"срок {_SOURCE_DERIVED_REMINDER_DATE}."
                ),
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }
        self.calls.append({"messages": copied, **kwargs})
        if self.mutate is not None:
            self.mutate()
        return {
            "content": _NORMAL_REVIEW,
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


class _DocumentDetailsModel:
    enabled = True
    model = "publication-reauthorization-document-details"
    total_budget_sec = 3.0

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        copied = [dict(item) for item in messages]
        self.calls.append({"messages": copied, **kwargs})
        prompt = "\n".join(str(item.get("content") or "") for item in copied)
        assert "FRIDAY_DOCUMENT_DETAIL_DATA" in prompt
        assert _SOURCE_CANARY in prompt
        return {
            "content": json.dumps(
                {
                    "details": [
                        {
                            "kind": "registration",
                            "evidence": f"Контроль: {_SOURCE_CANARY}",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


class _NeverCalledModel:
    enabled = True
    model = "publication-reauthorization-no-model"
    total_budget_sec = 3.0

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        del messages, kwargs
        raise AssertionError("an all-unreadable structural answer reached a model")


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


async def _ingest(
    settings: Any,
    storage: Any,
    *,
    filename: str,
    suffix: str,
    uploaded_by: str = "alice",
) -> dict[str, Any]:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    return await pipeline.ingest_file(
        "alice",
        None,
        (f"# Registered source {suffix}\n\nКонтроль: {_SOURCE_CANARY}.\nСекция: {suffix}.\n").encode(),
        filename=filename,
        mime_type="text/markdown",
        metadata={"uploaded_by": uploaded_by},
        source_ref=f"telegram-file:PUBLICATION-REAUTH-{suffix}",
    )


def _soft_delete_raw(storage: Any, raw_id: str) -> None:
    with storage.transaction() as conn:
        cursor = conn.execute(
            "UPDATE raw_objects SET deleted_at='2026-08-14T00:00:00Z' WHERE id=?",
            (raw_id,),
        )
    assert cursor.rowcount == 1


def _stored_assistant(storage: Any, result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    stored = storage.get_message(str(result["message_id"]), "alice")
    assert stored is not None
    assert stored["role"] == "assistant"
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert isinstance(metadata, dict)
    return stored, metadata


def _assert_failed_closed(
    storage: Any,
    result: dict[str, Any],
    *,
    expected_count: int,
) -> None:
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert _SOURCE_CANARY not in serialized
    assert _MODEL_CANARY not in serialized
    assert _CARRIER_CANARY not in serialized
    assert result["attachment_authority_changed_before_publication"] is True
    assert "источник стал недоступен или изменился" in str(result["message"]).casefold()
    assert result["verified"] is False
    assert result["verification_status"] == "unknown"
    assert result["verification"]["issues"] == [_AUTHORITY_CHANGED_ISSUE]
    assert result["attachment_context_expected_count"] == expected_count
    assert result["attachment_context_readable_count"] == 0
    assert result["attachment_context_available"] is False
    assert result["attachment_coverage_complete"] is False
    assert result["attachment_verification_complete"] is False
    assert result["files"] == []
    assert result["voice"] is None
    assert result["context"]["attributed_knowledge_count"] == 0
    assert result["context"]["attachment_context_expected_count"] == expected_count
    assert result["context"]["attachment_context_readable_count"] == 0
    assert result["context"]["attachment_coverage_complete"] is False
    assert result["context"]["attachment_verification_complete"] is False

    stored, metadata = _stored_assistant(storage, result)
    assert stored["content"] == result["message"]
    durable = json.dumps({"content": stored["content"], "metadata": metadata}, ensure_ascii=False)
    assert _SOURCE_CANARY not in durable
    assert _MODEL_CANARY not in durable
    assert _CARRIER_CANARY not in durable
    assert metadata["verification_status"] == "unknown"
    assert metadata["verification"]["issues"] == [_AUTHORITY_CHANGED_ISSUE]
    assert metadata["attachment_context_expected_count"] == expected_count
    assert metadata["attachment_context_readable_count"] == 0
    assert metadata["attachment_coverage_complete"] is False
    assert metadata["attachment_verification_complete"] is False
    assert metadata["attachment_context_used"] is False
    assert metadata["knowledge_object_ids"] == []
    assert metadata["private_context_lineage"] is True
    assert "conversation_attachment_raw_ids" not in metadata
    assert "conversation_attachment_uploaders" not in metadata
    assert result["message_format"] == "plain"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "raw_soft_deleted",
        "files_read_denied",
        "principal_disabled",
        "principal_preset_downgraded",
    ],
)
async def test_single_registered_source_is_reauthorized_after_primary_model_callback(
    settings: Any,
    storage: Any,
    mutation: str,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    ingested = await _ingest(
        configured,
        storage,
        filename="publication-authority.md",
        suffix=mutation,
    )
    raw_id = str(ingested["raw_object_id"])

    def mutate() -> None:
        if mutation == "raw_soft_deleted":
            _soft_delete_raw(storage, raw_id)
        elif mutation == "files_read_denied":
            storage.set_permission_override("alice", "files.read", "deny")
        elif mutation == "principal_disabled":
            assert storage.update_user("alice", status="disabled") is not None
        elif mutation == "principal_preset_downgraded":
            assert storage.update_user("alice", preset_key="guest") is not None
        else:  # pragma: no cover - closed parametrization
            raise AssertionError(mutation)

    model = _MutatingReviewModel(mutate)
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]
    result = await runtime.chat(
        "alice",
        "Загружен документ: publication-authority.md",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        synthetic_document_notice=True,
    )

    assert len(model.calls) == 1
    _assert_failed_closed(storage, result, expected_count=1)


@pytest.mark.asyncio
async def test_all_unreadable_registered_source_is_reauthorized_after_verified_read(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A code-owned unreadable verdict still depends on the selected private Raw."""

    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    graph = KnowledgeGraph(storage)
    pipeline = IngestionPipeline(configured, storage, graph)
    pipeline._doc_extractor.extract = lambda *args, **kwargs: DocumentResult(  # noqa: SLF001
        "",
        {"format": "synthetic-unreadable"},
        False,
        "synthetic parser refusal",
    )
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        b"SYNTHETIC-REGISTERED-UNREADABLE-BYTES",
        filename="registered-unreadable.bin",
        mime_type="application/octet-stream",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:PUBLICATION-REAUTH-UNREADABLE",
    )
    raw_id = str(ingested["raw_object_id"])
    runtime = AgentRuntime(configured, storage, llm=_NeverCalledModel())  # type: ignore[arg-type]
    real_verify = runtime._verify_registered_file_attachments  # noqa: SLF001
    verified_reads = 0

    async def verify_then_revoke(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal verified_reads
        verified = await real_verify(*args, **kwargs)
        verified_reads += 1
        assert len(verified) == 1
        assert verified[0].get("_registered_file_bytes_verified") is True
        assert verified[0].get("extraction_success") is False
        assert not str(verified[0].get("transient_text") or "").strip()
        _soft_delete_raw(storage, raw_id)
        return verified

    monkeypatch.setattr(runtime, "_verify_registered_file_attachments", verify_then_revoke)
    result = await runtime.chat(
        "alice",
        "Что в этом файле?",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
    )

    assert verified_reads == 1
    _assert_failed_closed(storage, result, expected_count=1)
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert raw_id not in serialized
    assert "registered-unreadable.bin" not in serialized


@pytest.mark.asyncio
async def test_foreign_uploader_is_still_active_at_publication(
    settings: Any,
    storage: Any,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    storage.ensure_user("bob", preset_key="user")
    ingested = await _ingest(
        configured,
        storage,
        filename="foreign-uploader.md",
        suffix="foreign-uploader-disabled",
        uploaded_by="bob",
    )
    raw_id = str(ingested["raw_object_id"])
    historical = _historical_direct_read_attachment(
        raw_id,
        tenant_id="alice",
        uploaded_by="bob",
        selector_kind="telegram_reply",
    )
    assert historical is not None

    def disable_uploader() -> None:
        assert storage.update_user("bob", status="disabled") is not None

    model = _MutatingReviewModel(disable_uploader)
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]
    result = await runtime.chat(
        "alice",
        "о чём речь в этом файле?",
        actor=_actor(),
        attachments=[historical],
        reply_assistant_reference=True,
    )

    assert len(model.calls) == 1
    _assert_failed_closed(storage, result, expected_count=1)


@pytest.mark.asyncio
async def test_revoking_one_member_closes_the_whole_registered_file_set(
    settings: Any,
    storage: Any,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    first = await _ingest(configured, storage, filename="first.md", suffix="first-member")
    second = await _ingest(configured, storage, filename="second.md", suffix="second-member")
    raw_ids = [str(first["raw_object_id"]), str(second["raw_object_id"])]
    model = _MutatingReviewModel(lambda: _soft_delete_raw(storage, raw_ids[1]))
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        "дай подробное ревью обоих файлов",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id} for raw_id in raw_ids],
        reply_assistant_reference=True,
    )

    assert len(model.calls) == 1
    _assert_failed_closed(storage, result, expected_count=2)


@pytest.mark.asyncio
async def test_unchanged_registered_source_publishes_normally(
    settings: Any,
    storage: Any,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    ingested = await _ingest(
        configured,
        storage,
        filename="unchanged.md",
        suffix="unchanged-positive-control",
    )
    raw_id = str(ingested["raw_object_id"])
    model = _MutatingReviewModel()
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        "Загружен документ: unchanged.md",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        synthetic_document_notice=True,
    )

    assert len(model.calls) == 1
    assert result["message"] == _NORMAL_REVIEW
    assert result["attachment_authority_changed_before_publication"] is False
    assert result["attachment_context_expected_count"] == 1
    assert result["attachment_context_readable_count"] == 1
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True
    assert result["files"] == []
    assert result["voice"] is None
    stored, metadata = _stored_assistant(storage, result)
    assert stored["content"] == _NORMAL_REVIEW
    assert metadata["attachment_context_used"] is True
    assert metadata["conversation_attachment_raw_ids"] == [raw_id]
    assert metadata["private_context_lineage"] is True


@pytest.mark.asyncio
async def test_unchanged_typed_historical_ignored_source_remains_readable(
    settings: Any,
    storage: Any,
) -> None:
    """Final reauth preserves unforgeable history authority, not a blanket inbox ban."""

    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    ingested = await _ingest(
        configured,
        storage,
        filename="historical-ignored.md",
        suffix="historical-ignored-positive-control",
    )
    raw_id = str(ingested["raw_object_id"])
    inbox = storage.execute("SELECT id FROM inbox WHERE raw_object_id=?", (raw_id,)).fetchone()
    assert inbox is not None
    assert storage.update_inbox_status(
        str(inbox["id"]),
        InboxStatus.IGNORED,
        reviewed_by="alice",
        user_id="alice",
    )
    historical = _historical_direct_read_attachment(
        raw_id,
        tenant_id="alice",
        uploaded_by="alice",
        selector_kind="telegram_reply",
    )
    assert historical is not None
    model = _MutatingReviewModel()
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        "о чём речь в этом файле?",
        actor=_actor(),
        attachments=[historical],
        reply_assistant_reference=True,
    )

    assert len(model.calls) == 1
    assert result["message"] == _NORMAL_REVIEW
    assert result["attachment_authority_changed_before_publication"] is False
    assert result["attachment_context_expected_count"] == 1
    assert result["attachment_context_readable_count"] == 1
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True
    _, metadata = _stored_assistant(storage, result)
    assert metadata["attachment_context_used"] is True
    assert metadata["conversation_attachment_raw_ids"] == [raw_id]


@pytest.mark.asyncio
async def test_late_raw_revocation_discards_code_owned_document_details(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structural details answer is private source output, not a safe fallback."""

    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    ingested = await _ingest(
        configured,
        storage,
        filename="late-structural-details.md",
        suffix="late-structural-details",
    )
    raw_id = str(ingested["raw_object_id"])
    model = _DocumentDetailsModel()
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]
    real_details = runtime._document_content_details_answer  # noqa: SLF001
    structural_answer = ""

    async def render_then_revoke(
        context: Any,
        attachments: list[dict[str, Any]],
        *,
        evidence_set: Any = None,
    ) -> str:
        nonlocal structural_answer
        structural_answer = await real_details(
            context,
            attachments,
            evidence_set=evidence_set,
        )
        assert _SOURCE_CANARY in structural_answer
        _soft_delete_raw(storage, raw_id)
        return structural_answer

    monkeypatch.setattr(runtime, "_document_content_details_answer", render_then_revoke)
    result = await runtime.chat(
        "alice",
        "Покажи реквизиты этого документа",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        reply_assistant_reference=True,
    )

    assert len(model.calls) == 1
    assert structural_answer and _SOURCE_CANARY in structural_answer
    _assert_failed_closed(storage, result, expected_count=1)
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert raw_id not in serialized


@pytest.mark.asyncio
async def test_late_raw_revocation_keeps_only_generic_committed_reminder_notice(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real reminder survives; its attachment-derived arguments do not publish."""

    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    ingested = await _ingest(
        configured,
        storage,
        filename="compound-reminder-source.md",
        suffix=(f"Напоминание: {_CARRIER_CANARY}. Срок: {_SOURCE_DERIVED_REMINDER_DATE}."),
    )
    raw_id = str(ingested["raw_object_id"])
    model = _MutatingReviewModel(lambda: _soft_delete_raw(storage, raw_id))
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]
    graph = KnowledgeGraph(storage)
    runtime.kernel.bind_services(
        storage,
        graph,
        WebSurfer(configured),
        IngestionPipeline(configured, storage, graph),
    )
    real_prefetch = runtime._prefetch_a_reminder_if_asked  # noqa: SLF001
    reminder_context: Any = None

    async def prefetch_from_the_authenticated_source(
        message: str,
        context: Any,
        actor: ActorContext,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        tools_used: list[str],
        tool_evidence: list[dict[str, str]],
        *,
        authority: Any = None,
    ) -> bool:
        del message, authority
        nonlocal reminder_context
        reminder_context = context
        projected = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        assert _SOURCE_CANARY in projected
        assert _CARRIER_CANARY in projected
        assert _SOURCE_DERIVED_REMINDER_DATE in projected
        derived_request = (
            f"Поставь напоминание «{_CARRIER_CANARY}» на "
            f"{_SOURCE_DERIVED_REMINDER_DATE}; и о чём речь в этом файле?"
        )
        return await real_prefetch(
            derived_request,
            context,
            actor,
            tools,
            messages,
            tools_used,
            tool_evidence,
        )

    monkeypatch.setattr(
        runtime,
        "_prefetch_a_reminder_if_asked",
        prefetch_from_the_authenticated_source,
    )
    result = await runtime.chat(
        "alice",
        "Поставь напоминание по этому файлу; и о чём речь в этом файле?",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        enable_tools=True,
    )

    assert len(model.calls) == 1
    assert reminder_context is not None
    assert reminder_context.successful_reminders == [
        {
            "what": _CARRIER_CANARY,
            "when": _SOURCE_DERIVED_REMINDER_WHEN,
            "requested_when": _SOURCE_DERIVED_REMINDER_WHEN,
            "delivery_scheduled": False,
        }
    ]
    _assert_failed_closed(storage, result, expected_count=1)
    expected_notice = "Напоминание было сохранено; автоматическая доставка сейчас недоступна."
    message_parts = str(result["message"]).split("\n\n")
    assert message_parts[0] == expected_notice
    assert len(message_parts) == 2
    assert "источник стал недоступен или изменился" in message_parts[1].casefold()
    assert result["tools_used"] == ["remind"]
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    assert raw_id not in serialized
    assert _SOURCE_DERIVED_REMINDER_DATE not in serialized
    assert _SOURCE_DERIVED_REMINDER_WHEN not in serialized

    stored, metadata = _stored_assistant(storage, result)
    durable = json.dumps({"content": stored["content"], "metadata": metadata}, ensure_ascii=False)
    assert _SOURCE_DERIVED_REMINDER_DATE not in durable
    assert _SOURCE_DERIVED_REMINDER_WHEN not in durable
    assert raw_id not in durable
    assert "conversation_attachment_raw_ids" not in metadata
    assert "conversation_attachment_uploaders" not in metadata


@pytest.mark.asyncio
async def test_source_revoked_by_late_file_builder_discards_the_derived_carrier(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    ingested = await _ingest(
        configured,
        storage,
        filename="late-carrier-source.md",
        suffix="late-carrier",
    )
    raw_id = str(ingested["raw_object_id"])
    model = _MutatingReviewModel()
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]
    late_calls = 0

    async def classify_without_a_model(*args: Any, **kwargs: Any) -> tuple[str, None]:
        del args, kwargs
        return "файл", None

    async def build_after_revocation(
        request: str,
        answer: str,
        actor: ActorContext,
        *,
        evidence: list[dict[str, Any]] | None = None,
        context: Any = None,
        literal_source_text: str | None = None,
    ) -> dict[str, Any]:
        del request, answer, actor, evidence, context, literal_source_text
        nonlocal late_calls
        late_calls += 1
        _soft_delete_raw(storage, raw_id)
        return {
            "kind": "document",
            "filename": "revoked-derived.docx",
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "content_base64": _CARRIER_CANARY,
        }

    monkeypatch.setattr(runtime, "_attachment_web_query_by_arbiter", classify_without_a_model)
    monkeypatch.setattr(runtime, "_file_for_a_request_that_wanted_one", build_after_revocation)
    result = await runtime.chat(
        "alice",
        "Сделай подробное ревью этого файла и сохрани результат в Word.",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        reply_assistant_reference=True,
    )

    assert len(model.calls) == 1
    assert late_calls == 1
    _assert_failed_closed(storage, result, expected_count=1)


@pytest.mark.asyncio
async def test_revoked_source_discards_already_rendered_file_and_voice_carriers(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    ingested = await _ingest(
        configured,
        storage,
        filename="rendered-carriers-source.md",
        suffix="rendered-carriers",
    )
    raw_id = str(ingested["raw_object_id"])
    model = _MutatingReviewModel(lambda: _soft_delete_raw(storage, raw_id))
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    async def generate_with_stale_carriers(
        context: Any,
        message: str,
        attachments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        messages = runtime._build_initial_messages(  # noqa: SLF001
            context,
            message,
            attachments,
            tool_enabled=False,
        )
        draft = await model.chat(messages, tools=[])
        return {
            "content": str(draft["content"]),
            "tools_used": [],
            "web_query_notice": _CARRIER_CANARY,
            "file_clips": [
                {
                    "kind": "document",
                    "filename": "stale-derived.txt",
                    "content": _CARRIER_CANARY,
                }
            ],
            "voice_clip": {
                "kind": "voice",
                "mime_type": "audio/ogg",
                "content": _CARRIER_CANARY,
            },
            "_model_generated": True,
        }

    monkeypatch.setattr(runtime, "_generate_response", generate_with_stale_carriers)
    result = await runtime.chat(
        "alice",
        "Загружен документ: rendered-carriers-source.md",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        synthetic_document_notice=True,
    )

    assert len(model.calls) == 1
    _assert_failed_closed(storage, result, expected_count=1)


@pytest.mark.asyncio
async def test_workspace_identity_is_rechecked_after_the_model_returns(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    workspace_text = f"Поле X: {_SOURCE_CANARY}.\nКонтрольный документ MCP."

    class _MutableWorkspaceKernel:
        def __init__(self) -> None:
            self.authorization = AuthorizationService(storage)
            self.changed = False
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def execute(
            self,
            name: str,
            arguments: dict[str, Any],
            *,
            actor: ActorContext,
        ) -> ToolResult:
            assert actor.own_id == "alice"
            self.calls.append((name, dict(arguments)))
            if name == "workspace_list":
                return ToolResult(
                    name,
                    True,
                    data={
                        "scope": "workspace_inbox",
                        "matched_at_least": 1,
                        "scan_limit_reached": False,
                        "entries": [
                            {
                                "path": "dept/authority-report.md",
                                "name": "authority-report.md",
                                "type": "file",
                                "size_bytes": len(workspace_text.encode()),
                                "modified_ns": 1,
                            }
                        ],
                        "returned": 1,
                        "complete": True,
                        "projection_truncated": False,
                        "snapshot_sha256": "c" * 64,
                        "next_cursor": None,
                    },
                )
            assert name == "workspace_read"
            assert arguments == {"relative_path": "dept/authority-report.md", "offset": 0}
            return ToolResult(
                name,
                True,
                data={
                    "scope": "workspace_inbox",
                    "path": "dept/authority-report.md",
                    "filename": "authority-report.md",
                    "mime_type": "text/markdown",
                    "size_bytes": len(workspace_text.encode()),
                    "sha256": ("d" if self.changed else "a") * 64,
                    "source_sha256": ("e" if self.changed else "b") * 64,
                    "readable": True,
                    "source_complete": True,
                    "advisory_only": False,
                    "verification_eligible": True,
                    "unsupported_format": False,
                    "extraction_status": "readable",
                    "source_truncated": False,
                    "parse_deadline_reached": False,
                    "parse_pages_read": 0,
                    "parse_total_pages": 0,
                    "parse_pages_truncated": False,
                    "archive_truncated": False,
                    "source_truncated_for_parse": False,
                    "text": workspace_text,
                    "offset": 0,
                    "next_offset": None,
                    "text_chars": len(workspace_text),
                    "projection_complete": True,
                },
            )

        def get_tool_definitions(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            del args, kwargs
            raise AssertionError("deterministic workspace read built model-selected tool schemas")

    kernel = _MutableWorkspaceKernel()
    model = _MutatingReviewModel(lambda: setattr(kernel, "changed", True))
    runtime = AgentRuntime(configured, storage, llm=model, kernel=kernel)  # type: ignore[arg-type]

    async def forbidden_prepare(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("explicit MCP inbox read entered general retrieval")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_prepare)
    result = await runtime.chat(
        "alice",
        ("Прочитай файл authority-report.md из MCP inbox и дай подробное ревью его содержимого."),
        actor=_actor(),
    )

    assert len(model.calls) == 1
    assert [name for name, _arguments in kernel.calls] == [
        "workspace_list",
        "workspace_read",
        "workspace_read",
    ]
    _assert_failed_closed(storage, result, expected_count=1)
    _, metadata = _stored_assistant(storage, result)
    assert "workspace_inbox_relative_path" not in metadata
    assert "workspace_inbox_sha256" not in metadata
    assert "workspace_inbox_source_sha256" not in metadata
