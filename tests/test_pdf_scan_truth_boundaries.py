"""Offline regressions for unreadable scans and false PDF refusals.

Every filename, body and answer in this module is synthetic.  No provider,
network tool or vision service is available to these tests.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _attachment_evidence_chunks,
    _attachment_whole_source_plan,
    _OwnedAttachment,
)
from friday.execution_kernel import ExecutionKernel
from friday.permissions import AuthorizationService
from friday.storage.models import RawObject, new_id

OWNER = "synthetic-pdf-scan-owner"
UNREADABLE_SENTINEL = "UNREADABLE-IMAGE-BODY-MUST-NOT-BECOME-AN-ANSWER"
PDF_SENTINEL = "PDF-GROUNDED-CLAUSE-417"
ADVISORY_SENTINEL = "ADVISORY-SCAN-BODY-MUST-NOT-BECOME-AN-ANSWER"


def _stored_file(
    storage: Any,
    *,
    filename: str,
    body: str,
    mime_type: str,
    extraction_success: bool,
) -> RawObject:
    storage.ensure_user(OWNER, preset_key="owner")
    raw = RawObject(
        id=new_id("raw"),
        user_id=OWNER,
        source="synthetic-offline-upload",
        source_ref=new_id("source"),
        raw_content=body,
        content_type="file",
        metadata_json={
            "filename": filename,
            "mime_type": mime_type,
            "uploaded_by": OWNER,
            "extraction_success": extraction_success,
            "text_extraction_success": extraction_success,
            "vision_review_required": False,
            "verification_eligible": extraction_success,
        },
    )
    storage.store_raw_object(raw)
    return raw


class _ForbiddenModel:
    enabled = True
    model = "forbidden-unreadable-scan-model"
    total_budget_sec = 30.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        del messages, kwargs
        self.calls += 1
        raise AssertionError("unreadable image reached a model stage")


class _FalsePdfRefusalThenGroundedModel:
    enabled = True
    model = "synthetic-pdf-refusal-recovery-model"
    total_budget_sec = 30.0

    def __init__(self, first_refusal: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self.first_refusal = first_refusal

    async def chat(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        copied = [dict(item) for item in messages]
        self.calls.append({"messages": copied, "kwargs": dict(kwargs)})
        if len(self.calls) == 1:
            return {"content": self.first_refusal}
        if len(self.calls) == 2:
            return {"content": f"В документе указано: {PDF_SENTINEL}."}
        raise AssertionError("false PDF refusal started more than one recovery call")


class _AlwaysFalsePdfRefusalModel:
    enabled = True
    model = "synthetic-repeated-pdf-refusal-model"
    total_budget_sec = 30.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        del messages, kwargs
        self.calls += 1
        if self.calls > 2:
            raise AssertionError("failure fallback started a third model call")
        return {"content": "Я не могу открыть PDF. Загрузите файл ещё раз."}


class _FalseAdvisoryScanRefusalModel:
    enabled = True
    model = "synthetic-advisory-refusal-model"
    total_budget_sec = 30.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        del messages, kwargs
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("an advisory scan refusal started another model attempt")
        return {"content": "Я не могу открыть PDF. Загрузите файл ещё раз."}


class _FailedAdvisoryScanSummaryModel:
    enabled = True
    model = "synthetic-advisory-timeout-model"
    total_budget_sec = 30.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        del messages, kwargs
        self.calls += 1
        raise TimeoutError("synthetic advisory summary deadline")


async def _simple_context(
    user_id: str,
    message: str,
    conversation_id: str,
    **kwargs: Any,
) -> AgentContext:
    del kwargs
    return AgentContext(
        conversation_id=conversation_id,
        user_id=user_id,
        person_id=user_id,
        search_query=message,
        current_attachment_present=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["current", "restored"])
async def test_unreadable_jpeg_is_code_owned_unknown_without_guessing_or_model(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    raw = _stored_file(
        storage,
        filename="synthetic-scan.jpg",
        body=f"[File: image/jpeg; {UNREADABLE_SENTINEL}]",
        mime_type="image/jpeg",
        extraction_success=False,
    )
    auth = AuthorizationService(storage)
    model = _ForbiddenModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=model,
        kernel=ExecutionKernel(auth, settings),
    )
    owned = runtime._owned_file_attachment(  # noqa: SLF001
        raw.id,
        tenant_id=OWNER,
        person_id=OWNER,
    )
    assert owned is not None
    assert _attachment_evidence_chunks([owned]) == []
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)

    async def should_not_generate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AssertionError("unreadable image reached the answer generator")

    async def should_not_verify(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AssertionError("unreadable image reached the model verifier")

    monkeypatch.setattr(runtime, "_generate_response", should_not_generate)
    monkeypatch.setattr(runtime, "_verify_response", should_not_verify)

    conversation_id: str | None = None
    attachments: list[dict[str, Any]] = [
        {
            "raw_object_id": raw.id,
            "filename": "synthetic-scan.jpg",
            "transient_text": UNREADABLE_SENTINEL,
            "extraction_success": False,
            "verification_eligible": False,
        }
    ]
    question = "Что указано в этом изображении?"
    if route == "restored":
        conversation = storage.create_conversation(OWNER, title="synthetic unreadable scan")
        conversation_id = str(conversation["id"])
        storage.store_message(
            conversation_id,
            OWNER,
            "user",
            "прикрепляю синтетический скан",
            metadata={
                "had_attachments": True,
                "attachment_count": 1,
                "conversation_attachment_raw_ids": [raw.id],
                "conversation_uploaded_raw_ids": [raw.id],
            },
        )
        storage.store_message(
            conversation_id,
            OWNER,
            "assistant",
            "Синтетический файл принят.",
            metadata={"attachment_context_used": False},
        )
        attachments = []
        question = "Что указано в synthetic-scan.jpg?"

    result = await runtime.chat(
        OWNER,
        question,
        actor=auth.actor_for_user(OWNER, source="test"),
        conversation_id=conversation_id,
        attachments=attachments,
        enable_tools=False,
    )

    answer = result["message"].casefold()
    assert any(
        phrase in answer
        for phrase in (
            "не удалось прочитать",
            "прочитать не удалось",
            "не удалось разобрать",
            "содержимое не прочитано",
            "содержимое недоступно",
        )
    ), "the code-owned answer did not say that the image content was unread"
    assert any(
        phrase in answer
        for phrase in (
            "не буду угадывать",
            "не стану угадывать",
            "не буду выдумывать",
            "не стану выдумывать",
            "не буду додумывать",
        )
    ), "the code-owned answer did not explicitly rule out guessing"
    assert model.calls == 0
    assert result["tools_used"] == []
    assert result["attachment_context_expected_count"] == 1
    assert result["attachment_context_readable_count"] == 0
    assert result["attachment_context_available"] is False
    assert result["attachment_verification_complete"] is False
    assert result["verification_status"] == "unknown"
    assert result["verified"] is False
    assert UNREADABLE_SENTINEL not in json.dumps(result, ensure_ascii=False)

    rows = storage.get_conversation_messages(result["conversation_id"], user_id=OWNER)
    assistant_metadata = json.loads(rows[-1]["metadata_json"] or "{}")
    assert assistant_metadata["structural"]["model_spoke"] is False
    assert UNREADABLE_SENTINEL not in rows[-1]["content"]
    assert UNREADABLE_SENTINEL not in rows[-1]["metadata_json"]


@pytest.mark.asyncio
async def test_mixed_readable_and_advisory_selected_set_reaches_synthesis_but_stays_unknown(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _stored_file(
        storage,
        filename="report.pdf",
        body=f"Synthetic PDF evidence: {PDF_SENTINEL}.",
        mime_type="application/pdf",
        extraction_success=True,
    )
    scan = RawObject(
        id=new_id("raw"),
        user_id=OWNER,
        source="synthetic-offline-upload",
        source_ref=new_id("source"),
        raw_content=ADVISORY_SENTINEL,
        content_type="file",
        metadata_json={
            "filename": "scan.jpg",
            "mime_type": "image/jpeg",
            "uploaded_by": OWNER,
            "extraction_success": True,
            "text_extraction_success": False,
            "vision_review_required": True,
            "advisory_only": True,
            "verification_eligible": False,
        },
    )
    storage.store_raw_object(scan)
    auth = AuthorizationService(storage)
    model = _ForbiddenModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=model,
        kernel=ExecutionKernel(auth, settings),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)

    async def synthesize(
        context: AgentContext,
        message: str,
        attachments: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        del context, message
        shown = json.dumps(attachments, ensure_ascii=False)
        assert PDF_SENTINEL in shown
        assert ADVISORY_SENTINEL in shown
        return {
            "content": f"Сопоставлены {PDF_SENTINEL} и {ADVISORY_SENTINEL}.",
            "tools_used": [],
            "_model_generated": True,
        }

    async def should_not_verify(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AssertionError("a partially unreadable selected set reached the model verifier")

    monkeypatch.setattr(runtime, "_generate_response", synthesize)
    monkeypatch.setattr(runtime, "_verify_response", should_not_verify)
    result = await runtime.chat(
        OWNER,
        "Сравни report.pdf и scan.jpg",
        actor=auth.actor_for_user(OWNER, source="test"),
        attachments=[
            {"raw_object_id": report.id, "filename": "report.pdf"},
            {
                "raw_object_id": scan.id,
                "filename": "scan.jpg",
                "advisory_only": True,
                "verification_eligible": False,
            },
        ],
        enable_tools=False,
    )

    assert PDF_SENTINEL in result["message"]
    assert ADVISORY_SENTINEL in result["message"]
    assert "результат локального распознавания" in result["message"]
    assert "сверяйте критичные данные с оригиналом" in result["message"].casefold()
    assert model.calls == 0
    assert result["tools_used"] == []
    assert result["attachment_context_expected_count"] == 2
    assert result["attachment_context_readable_count"] == 2
    assert result["attachment_context_available"] is True
    assert result["attachment_verification_complete"] is False
    assert result["verification_status"] == "unknown"
    assert result["verified"] is False
    assert result["attachment_coverage_complete"] is True

    rows = storage.get_conversation_messages(result["conversation_id"], user_id=OWNER)
    assert ADVISORY_SENTINEL in rows[-1]["content"]
    assert ADVISORY_SENTINEL not in rows[-1]["metadata_json"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_refusal",
    [
        "Я не могу открыть или увидеть PDF. Загрузите файл целиком ещё раз.",
        "Извините, но я не могу открыть PDF. Пришлите его снова.",
        "Я не вижу содержимого PDF. Загрузите файл ещё раз.",
        "Не удалось открыть PDF. Загрузите его повторно.",
        "PDF не открылся. Отправьте его ещё раз.",
        "Я не могу извлечь текст из PDF. Загрузите скан снова.",
        "У меня нет возможности открыть этот PDF. Пришлите его заново.",
        "Файл вижу, но прочитать PDF не могу. Загрузите снова.",
        "Я вижу файл, но не могу открыть его. Пришлите снова.",
        "Файл получил. Открыть его не могу. Отправьте ещё раз.",
        "Вижу вложение, но не могу прочитать его содержимое.",
        "Мне недоступен загруженный PDF. Загрузите повторно.",
        "Содержимое документа не отображается. Прикрепите снова.",
    ],
)
async def test_readable_pdf_false_refusal_gets_one_tool_free_grounded_retry(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    first_refusal: str,
) -> None:
    raw = _stored_file(
        storage,
        filename="synthetic-evidence.pdf",
        body=f"Synthetic PDF evidence: {PDF_SENTINEL}.",
        mime_type="application/pdf",
        extraction_success=True,
    )
    auth = AuthorizationService(storage)
    model = _FalsePdfRefusalThenGroundedModel(first_refusal)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=ExecutionKernel(auth, settings),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)

    result = await runtime.chat(
        OWNER,
        "Что указано в synthetic-evidence.pdf?",
        actor=auth.actor_for_user(OWNER, source="test"),
        attachments=[
            {
                "raw_object_id": raw.id,
                "filename": "synthetic-evidence.pdf",
                "transient_text": f"Synthetic PDF evidence: {PDF_SENTINEL}.",
                "extraction_success": True,
                "verification_eligible": True,
            }
        ],
        enable_tools=False,
    )

    assert len(model.calls) == 2, "a readable-PDF refusal must get exactly one bounded retry"
    assert all(call["kwargs"].get("tools") == [] for call in model.calls)
    assert all(
        PDF_SENTINEL in "\n".join(str(item.get("content") or "") for item in call["messages"])
        for call in model.calls
    ), "the retry lost the authenticated PDF evidence"
    assert result["message"] == f"В документе указано: {PDF_SENTINEL}."
    assert "загруз" not in result["message"].casefold()
    assert "не могу открыть" not in result["message"].casefold()
    assert result["tools_used"] == []
    assert result["attachment_context_available"] is True
    assert result["attachment_context_readable_count"] == 1


@pytest.mark.asyncio
async def test_bare_advisory_pdf_upload_salvages_literal_ocr_without_a_second_model_or_web_claim(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _stored_file(
        storage,
        filename="synthetic-advisory.pdf",
        body=ADVISORY_SENTINEL,
        mime_type="application/pdf",
        extraction_success=True,
    )
    model = _FalseAdvisoryScanRefusalModel()
    auth = AuthorizationService(storage)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=ExecutionKernel(auth, settings),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)

    result = await runtime.chat(
        OWNER,
        "Загружен документ: synthetic-advisory.pdf",
        actor=auth.actor_for_user(OWNER, source="test"),
        attachments=[
            {
                "raw_object_id": raw.id,
                "filename": "synthetic-advisory.pdf",
                "transient_text": ADVISORY_SENTINEL,
                "extraction_success": True,
                "advisory_only": True,
                "verification_eligible": False,
            }
        ],
        enable_tools=False,
        synthetic_document_notice=True,
    )

    assert model.calls == 1
    assert ADVISORY_SENTINEL in result["message"]
    assert "распознанный текст без интерпретации" in result["message"]
    assert "интернет-выдачу" not in result["message"]
    assert "загрузите файл" not in result["message"].casefold()
    stored = storage.get_message(str(result["message_id"]), OWNER)
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["structural"].get("output_guards", {}).get("web_evidence_replaced") is not True


@pytest.mark.asyncio
async def test_failed_advisory_summary_returns_literal_ocr_without_retry(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _stored_file(
        storage,
        filename="synthetic-timeout-scan.pdf",
        body=ADVISORY_SENTINEL,
        mime_type="application/pdf",
        extraction_success=True,
    )
    model = _FailedAdvisoryScanSummaryModel()
    auth = AuthorizationService(storage)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=ExecutionKernel(auth, settings),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)

    result = await runtime.chat(
        OWNER,
        "Загружен документ: synthetic-timeout-scan.pdf",
        actor=auth.actor_for_user(OWNER, source="test"),
        attachments=[
            {
                "raw_object_id": raw.id,
                "filename": "synthetic-timeout-scan.pdf",
                "transient_text": ADVISORY_SENTINEL,
                "extraction_success": True,
                "advisory_only": True,
                "verification_eligible": False,
            }
        ],
        enable_tools=False,
        synthetic_document_notice=True,
    )

    assert model.calls == 1
    assert ADVISORY_SENTINEL in result["message"]
    assert "распознанный текст без интерпретации" in result["message"]
    assert "повторите запрос позже" not in result["message"].casefold()
    assert "загрузите файл" not in result["message"].casefold()


@pytest.mark.asyncio
async def test_two_advisory_scans_are_returned_together_after_one_failed_summary(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = [f"{ADVISORY_SENTINEL}-ONE", f"{ADVISORY_SENTINEL}-TWO"]
    raws = [
        _stored_file(
            storage,
            filename=f"scan-{index}.pdf",
            body=body,
            mime_type="application/pdf",
            extraction_success=True,
        )
        for index, body in enumerate(bodies, 1)
    ]
    model = _FalseAdvisoryScanRefusalModel()
    auth = AuthorizationService(storage)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=ExecutionKernel(auth, settings),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)

    result = await runtime.chat(
        OWNER,
        "Загружены документы: scan-1.pdf, scan-2.pdf",
        actor=auth.actor_for_user(OWNER, source="test"),
        attachments=[
            {
                "raw_object_id": raw.id,
                "filename": f"scan-{index}.pdf",
                "transient_text": body,
                "extraction_success": True,
                "advisory_only": True,
                "verification_eligible": False,
            }
            for index, (raw, body) in enumerate(zip(raws, bodies, strict=True), 1)
        ],
        enable_tools=False,
        synthetic_document_notice=True,
    )

    assert model.calls == 1
    assert all(body in result["message"] for body in bodies)
    assert result["message"].count("распознанный текст без интерпретации") == 1
    assert result["message"].index("scan-1.pdf") < result["message"].index("scan-2.pdf")
    assert result["attachment_context_expected_count"] == 2
    assert result["attachment_context_readable_count"] == 2


@pytest.mark.asyncio
async def test_repeated_pdf_refusal_cannot_build_a_file_from_the_failure_fallback(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _stored_file(
        storage,
        filename="synthetic-review.pdf",
        body=f"Synthetic PDF evidence: {PDF_SENTINEL}.",
        mime_type="application/pdf",
        extraction_success=True,
    )
    auth = AuthorizationService(storage)
    model = _AlwaysFalsePdfRefusalModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=ExecutionKernel(auth, settings),
    )
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)

    async def forbidden_file_builder(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AssertionError("model failure fallback reached the late file builder")

    monkeypatch.setattr(runtime, "_file_for_a_request_that_wanted_one", forbidden_file_builder)
    result = await runtime.chat(
        OWNER,
        "Прочитай synthetic-review.pdf и оформи результат в Word.",
        actor=auth.actor_for_user(OWNER, source="test"),
        attachments=[
            {
                "raw_object_id": raw.id,
                "filename": "synthetic-review.pdf",
                "transient_text": f"Synthetic PDF evidence: {PDF_SENTINEL}.",
                "extraction_success": True,
                "verification_eligible": True,
            }
        ],
        enable_tools=False,
    )

    assert model.calls == 2
    assert "Вложение прочитано, но модель не сформировала ответ" in result["message"]
    assert "загруз" not in result["message"].casefold()
    assert result["files"] == []
    assert result["tools_used"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("advisory_only", "verification_eligible"),
    [(True, True), (False, False), (True, False)],
)
async def test_unverified_or_advisory_text_never_enters_hierarchy_evidence(
    settings: Any,
    storage: Any,
    advisory_only: bool,
    verification_eligible: bool,
) -> None:
    advisory_text = "ADVISORY-OCR-BODY-MUST-NOT-BECOME-HIERARCHY-EVIDENCE"
    attachment = _OwnedAttachment(
        {
            "filename": "synthetic-advisory-scan.jpg",
            "transient_text": advisory_text,
            "extraction_success": True,
            "advisory_only": advisory_only,
            "verification_eligible": verification_eligible,
        }
    )

    (
        chunks,
        _files,
        files_total,
        files_readable,
        source_complete,
        _chunks_required,
        _source_chars_total,
        _source_chars_planned,
    ) = _attachment_whole_source_plan([attachment])

    assert chunks == []
    assert files_total == 1
    assert files_readable == 0
    assert source_complete is False

    model = _ForbiddenModel()
    runtime = AgentRuntime(settings, storage, llm=model)
    bundle, complete = await runtime._build_attachment_hierarchy_bundle(  # noqa: SLF001
        AgentContext(
            conversation_id="synthetic-advisory-hierarchy",
            user_id=OWNER,
            person_id=OWNER,
            current_attachment_present=True,
        ),
        "Разбери весь синтетический файл.",
        [attachment],
        task_kind="summary",
    )

    assert model.calls == 0
    assert bundle.files_total == 1
    assert bundle.files_readable == 0
    assert bundle.records_available is False
    assert complete is False
    assert advisory_text not in bundle.evidence
