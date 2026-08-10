"""G45: natural PDF refusals and production-path scan gates without prepare_context mocks.

Every body and answer here is synthetic.  No provider, network or vision call.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import (
    AgentRuntime,
    _is_false_readable_attachment_refusal,
)
from friday.execution_kernel import ExecutionKernel
from friday.permissions import AuthorizationService
from friday.storage.models import RawObject, new_id

OWNER = "g45-natural-refusal-owner"
PDF_SENTINEL = "PDF-GROUNDED-CLAUSE-417"
ADVISORY = "ADVISORY-SCAN-BODY-MUST-NOT-LEAK-288"
HALLUC = "MIXED-HALLUCINATION-999"

# Live-style paraphrases that the previous single-regex detector missed.
_NATURAL_FALSE_REFUSALS = (
    "К сожалению, содержимое PDF-файла мне сейчас недоступно. Попробуйте загрузить документ ещё раз.",
    "Не вижу текст из приложенного файла. Пришлите PDF повторно, пожалуйста.",
    "Файл загружен, однако извлечь данные из него не получается. Отправьте файл ещё раз.",
    "Я получил PDF, но прочитать его содержимое не могу — отправьте снова.",
    "Содержимое файла недоступно. Повторите загрузку.",
    "Не получается открыть приложенный PDF.",
    "PDF-файл не удалось прочитать. Пришлите ещё раз.",
    "У меня нет доступа к содержимому файла. Загрузите повторно.",
    "Файл есть, но текст из него недоступен. Загрузите снова.",
    "Я не могу открыть PDF. Загрузите его снова.",
    "Я вижу файл, но не могу открыть его. Пришлите снова.",
    "Файл получил. Открыть его не могу.",
    "Мне недоступен загруженный PDF.",
    "Содержимое документа не отображается.",
)


class _HostileScanModel:
    enabled = True
    model = "hostile-scan"
    total_budget_sec = 30.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        del messages, kwargs
        self.calls += 1
        return {
            "content": (
                f"Серия: {HALLUC}\nНомер: 123456\nДата: 01.01.2000\n"
                "Код: 770-001\nВыдан: 02.02.2001\nA:12\nB:34\nC:56\nD:78\nE:90\nF:11\nG:22\nH:33\nI:44"
            )
        }


class _FalseThenGrounded:
    enabled = True
    model = "false-then-grounded"
    total_budget_sec = 30.0

    def __init__(self, first: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self.first = first

    async def chat(self, messages: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"messages": [dict(m) for m in messages], "kwargs": dict(kwargs)})
        if len(self.calls) == 1:
            return {"content": self.first}
        if len(self.calls) == 2:
            return {"content": f"В документе указано: {PDF_SENTINEL}."}
        raise AssertionError("more than one recovery model call")


def _store_pdf(storage: Any) -> RawObject:
    storage.ensure_user(OWNER, preset_key="owner")
    raw = RawObject(
        id=new_id("raw"),
        user_id=OWNER,
        source="g45-synth",
        source_ref=new_id("source"),
        raw_content=f"Synthetic PDF evidence: {PDF_SENTINEL}.",
        content_type="file",
        metadata_json={
            "filename": "synthetic-evidence.pdf",
            "mime_type": "application/pdf",
            "uploaded_by": OWNER,
            "extraction_success": True,
            "text_extraction_success": True,
            "vision_review_required": False,
            "verification_eligible": True,
            "parse_pages_read": 1,
            "parse_total_pages": 1,
        },
    )
    storage.store_raw_object(raw)
    return raw


def _store_jpeg(storage: Any, *, media_kind: str = "photo") -> RawObject:
    storage.ensure_user(OWNER, preset_key="owner")
    raw = RawObject(
        id=new_id("raw"),
        user_id=OWNER,
        source="g45-synth",
        source_ref=new_id("source"),
        raw_content=f"[File: image/jpeg; {ADVISORY}]",
        content_type="file",
        metadata_json={
            "filename": "telegram-photo-801.jpg" if media_kind == "photo" else "scan.jpg",
            "mime_type": "image/jpeg",
            "media_kind": media_kind,
            "uploaded_by": OWNER,
            "extraction_success": False,
            "text_extraction_success": False,
            "vision_review_required": False,
            "vision_used": False,
            "verification_eligible": False,
            "parse_pages_read": 0,
            "parse_total_pages": 0,
        },
    )
    storage.store_raw_object(raw)
    return raw


@pytest.mark.parametrize("refusal", _NATURAL_FALSE_REFUSALS)
def test_natural_whole_file_refusals_are_detected(refusal: str) -> None:
    assert _is_false_readable_attachment_refusal(refusal) is True


def test_field_level_uncertainty_is_not_whole_file_refusal() -> None:
    assert _is_false_readable_attachment_refusal("Не могу разобрать одну цифру в строке 12.") is False
    assert _is_false_readable_attachment_refusal(f"В документе указано: {PDF_SENTINEL}.") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", _NATURAL_FALSE_REFUSALS)
async def test_natural_pdf_refusal_gets_tool_free_retry_without_prepare_context_mock(
    settings: Any,
    storage: Any,
    refusal: str,
) -> None:
    raw = _store_pdf(storage)
    auth = AuthorizationService(storage)
    model = _FalseThenGrounded(refusal)
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=ExecutionKernel(auth, settings),
    )
    # Intentionally leave _prepare_context live: c51bccf tests monkeypatched it
    # and therefore could not prove the production mutation boundary.
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

    assert len(model.calls) == 2
    assert all(call["kwargs"].get("tools") == [] for call in model.calls)
    assert all(
        PDF_SENTINEL in "\n".join(str(item.get("content") or "") for item in call["messages"])
        for call in model.calls
    )
    assert result["message"] == f"В документе указано: {PDF_SENTINEL}."
    assert "загруз" not in result["message"].casefold()
    assert result["attachment_context_readable_count"] == 1
    assert result["attachment_context_available"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["current", "restored"])
@pytest.mark.parametrize("media_kind", ["photo", "document"])
async def test_unreadable_jpeg_photo_and_document_are_code_owned_without_prepare_context_mock(
    settings: Any,
    storage: Any,
    route: str,
    media_kind: str,
) -> None:
    raw = _store_jpeg(storage, media_kind=media_kind)
    auth = AuthorizationService(storage)
    model = _HostileScanModel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=model,
        kernel=ExecutionKernel(auth, settings),
    )

    conversation_id: str | None = None
    attachments: list[dict[str, Any]]
    question: str
    if route == "current":
        attachments = [
            {
                "raw_object_id": raw.id,
                "filename": str(raw.metadata_json["filename"]),
                "transient_text": "",
                "extraction_success": False,
                "verification_eligible": False,
                "advisory_only": False,
            }
        ]
        question = "Что указано в этом изображении?"
    else:
        conversation = storage.create_conversation(OWNER, title="g45 scan restore")
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
        question = f"Что указано в {raw.metadata_json['filename']}?"

    result = await runtime.chat(
        OWNER,
        question,
        actor=auth.actor_for_user(OWNER, source="test"),
        conversation_id=conversation_id,
        attachments=attachments,
        enable_tools=False,
    )

    answer = result["message"].casefold()
    assert model.calls == 0
    assert result["tools_used"] == []
    assert result["attachment_context_expected_count"] == 1
    assert result["attachment_context_readable_count"] == 0
    assert result["attachment_context_available"] is False
    assert result["verification_status"] == "unknown"
    assert result["verified"] is False
    assert HALLUC not in result["message"]
    assert ADVISORY not in json.dumps(result, ensure_ascii=False)
    assert any(
        phrase in answer
        for phrase in (
            "прочитать не удалось",
            "не удалось прочитать",
            "содержимое не прочитано",
        )
    )
    assert any(
        phrase in answer
        for phrase in (
            "не буду угадывать",
            "не буду выдумывать",
        )
    )

    rows = storage.get_conversation_messages(result["conversation_id"], user_id=OWNER)
    assistant_metadata = json.loads(rows[-1]["metadata_json"] or "{}")
    assert assistant_metadata["structural"]["model_spoke"] is False
