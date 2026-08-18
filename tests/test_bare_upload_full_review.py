"""The upload contour produces one ordinary review, never a quicklook turn."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from typing import Any

import pytest
from openpyxl import Workbook

from friday.agent_runtime import (
    _ATTACHMENT_PRIMARY_MODEL_OUTPUT_TOKENS,
    _MODEL_LENGTH_LIMIT_FALLBACK,
    _MODEL_LENGTH_LIMIT_NOTICE,
    AgentRuntime,
    _attachment_whole_document_task,
    _historical_direct_read_attachment,
)
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext

_SOURCE = """# Проверка проекта

## Назначение
Подтвердить обычное подробное ревью файла.

## Риск
Контрольный риск: FULL-REVIEW-RISK-4815.
"""
_REVIEW = """## Подробное ревью

- Назначение: подтвердить обычный разбор файла.
- Риск: `FULL-REVIEW-RISK-4815` требует внимания.

**Вывод:** содержимое разобрано по существу.
""".strip()


class _ReviewSpy:
    enabled = True
    model = "ordinary-file-review-spy"
    total_budget_sec = 2.0

    def __init__(self, draft: str = _REVIEW, *, finish_reason: str = "stop") -> None:
        self.calls: list[dict[str, Any]] = []
        self.draft = draft
        self.finish_reason = finish_reason

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls.append({"messages": [dict(item) for item in messages], **kwargs})
        return {
            "content": self.draft,
            "tool_calls": None,
            "finish_reason": self.finish_reason,
            "_queue_wait_sec": 0.0,
        }


class _OneCallReviewSpy(_ReviewSpy):
    async def chat(self, messages, **kwargs):  # noqa: ANN001
        if self.calls:
            raise AssertionError("complete open file review started a second model pass")
        return await super().chat(messages, **kwargs)


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


async def _registered_text(settings, storage) -> str:  # noqa: ANN001
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        _SOURCE.encode(),
        filename="full-review.md",
        mime_type="text/markdown",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:FULL-REVIEW-4815",
    )
    return str(ingested["raw_object_id"])


async def _registered_incomplete_index_xlsx(settings, storage) -> str:  # noqa: ANN001
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "SYNTHETIC-300"
    sheet.append(["Позиция", "Значение"])
    for position in range(1, 301):
        value = "ROW-288-SENTINEL" if position == 288 else f"VALUE-{position:03d}"
        sheet.append([f"ITEM-{position:03d}", value])
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        stream.getvalue(),
        filename="synthetic-300.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:HISTORICAL-FULL-FIT-300",
    )
    return str(ingested["raw_object_id"])


@pytest.mark.asyncio
async def test_bare_upload_runs_one_ordinary_full_review_and_preserves_markdown(
    settings,
    storage,
    monkeypatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _registered_text(configured, storage)
    model = _ReviewSpy()
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    async def forbidden_quicklook(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("bare upload entered the removed bounded-overview helper")

    monkeypatch.setattr("friday.agent_runtime._maybe_bounded_file_overview", forbidden_quicklook)
    result = await runtime.chat(
        "alice",
        "Загружен документ: full-review.md",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        synthetic_document_notice=True,
    )

    assert result["message"] == _REVIEW
    assert result["message_format"] == "markdown"
    assert result["tools_used"] == []
    assert "Быстрый обзор" not in result["message"]
    assert len(model.calls) == 1
    assert model.calls[0].get("tools") == []
    prompt = "\n".join(str(item.get("content") or "") for item in model.calls[0]["messages"])
    assert "содержательное подробное ревью" in prompt
    assert "не склеивай всё в одну строку" in prompt
    assert "FULL-REVIEW-RISK-4815" in prompt
    assert "Быстрый обзор содержимого" not in prompt
    stored = storage.get_message(str(result["message_id"]), "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata.get("overview_model_used") is not True
    assert metadata.get("structural", {}).get("model_spoke") is True


@pytest.mark.asyncio
async def test_production_verification_keeps_complete_open_review_to_one_model_pass(
    settings,
    storage,
) -> None:
    configured = replace(settings, verify_answers=True, verify_min_answer_chars=1)
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _registered_text(configured, storage)
    model = _OneCallReviewSpy()
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        "Загружен документ: full-review.md",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        synthetic_document_notice=True,
    )

    assert result["message"] == _REVIEW
    assert len(model.calls) == 1
    assert result["verified"] is False
    assert result["verification_status"] == "skipped"
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True


@pytest.mark.asyncio
async def test_token_capped_review_is_published_only_through_a_complete_sentence(
    settings,
    storage,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _registered_text(configured, storage)
    torn_tail = "## Ревью\n\nНазначение документа подтверждено. Незавершённый хвост ответа"
    model = _ReviewSpy(torn_tail, finish_reason="length")
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        "Загружен документ: full-review.md",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        synthetic_document_notice=True,
    )

    assert result["message"] == (
        "## Ревью\n\nНазначение документа подтверждено.\n\n" + _MODEL_LENGTH_LIMIT_NOTICE
    )
    assert "Незавершённый хвост" not in result["message"]
    assert len(model.calls) == 1
    assert model.calls[0]["max_tokens"] == _ATTACHMENT_PRIMARY_MODEL_OUTPUT_TOKENS == 2_048
    prompt = "\n".join(str(item.get("content") or "") for item in model.calls[0]["messages"])
    assert "не более чем в 2200 знаков" in prompt
    assert "Обязательно заверши последнюю фразу" in prompt
    stored = storage.get_message(str(result["message_id"]), "alice")
    metadata = json.loads(str(stored["metadata_json"] or "{}"))
    assert metadata["model_output_truncated"] is True


@pytest.mark.asyncio
async def test_token_capped_fragment_without_a_sentence_gets_a_complete_fallback(
    settings,
    storage,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _registered_text(configured, storage)
    model = _ReviewSpy("оборванный фрагмент без единой границы" * 8, finish_reason="length")
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        "Загружен документ: full-review.md",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        synthetic_document_notice=True,
    )

    assert result["message"] == _MODEL_LENGTH_LIMIT_FALLBACK
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "draft", "expected_verifier_calls"),
    [
        ("что в этом файле?", _REVIEW, 0),
        ("о чём речь в этом файле?", _REVIEW, 0),
        ("Можешь сделать обзор этого файла?", _REVIEW, 0),
        ("Можешь дать ревью этого файла?", _REVIEW, 0),
        ("Дай ревью файла", _REVIEW, 0),
        ("Review this file", _REVIEW, 0),
        ("Could you review this file?", _REVIEW, 0),
        ("проанализируй файл и посчитай строки", "Всего 7 строк.", 1),
        ("дай обзор и перечисли всех людей", "Перечислены все 3 человека.", 1),
        ("проанализируй, есть ли ошибки в файле", "Ошибок не найдено.", 1),
        ("дай обзор файла", "Никаких рисков и ошибок.", 1),
        ("дай обзор файла", "Критичных рисков не вижу.", 1),
        ("дай исчерпывающий обзор файла", "Это исчерпывающий обзор.", 1),
        ("проанализируй весь файл", _REVIEW, 1),
        ("analyze the whole file", _REVIEW, 1),
        ("дай обзор файла и назови дату договора", "Дата договора — 14 августа 2026.", 1),
        ("Дай полный обзор файла", "Обзор: документ безупречен.", 1),
        ("Дай обзор файла и скажи ИНН", "ИНН 1234567890.", 1),
        ("Дай обзор файла: присутствуют ли опечатки?", "Не обнаружено.", 1),
        ("Give an overview and list every person", "Alice and Bob.", 1),
        (
            "Analyze the file and tell me exactly when the contract starts",
            "It starts 14 August 2026.",
            1,
        ),
        ("Tell me about this file: who is the director?", "The director is Alice.", 1),
        ("дай обзор файла", "Документ безупречен.", 1),
    ],
)
async def test_open_file_review_uses_one_pass_only_for_non_strict_claims(
    settings,
    storage,
    monkeypatch,
    question: str,
    draft: str,
    expected_verifier_calls: int,
) -> None:
    configured = replace(settings, verify_answers=True, verify_min_answer_chars=1)
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _registered_text(configured, storage)
    model = _ReviewSpy(draft)
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]
    verifier_calls: list[tuple[str, str]] = []

    async def verifier(asked, answer, context, *, tool_evidence):  # noqa: ANN001
        del context, tool_evidence
        verifier_calls.append((str(asked), str(answer)))
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    async def forbidden_repair(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("a passing strict file review unexpectedly started repair")

    monkeypatch.setattr(runtime, "_verify_response", verifier)
    monkeypatch.setattr(runtime, "_repair_once", forbidden_repair)
    result = await runtime.chat(
        "alice",
        question,
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
        reply_assistant_reference=True,
    )

    assert result["message"] == draft
    assert len(model.calls) == 1
    assert len(verifier_calls) == expected_verifier_calls
    if expected_verifier_calls:
        assert result["verified"] is True
        assert result["verification_status"] == "passed"
    else:
        assert result["verified"] is False
        assert result["verification_status"] == "skipped"


@pytest.mark.asyncio
async def test_reply_to_assistant_overview_runs_one_ordinary_full_review(
    settings,
    storage,
    monkeypatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    conversation = storage.create_conversation("alice", title="reply full review")
    raw_id = await _registered_text(configured, storage)
    model = _ReviewSpy()
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    async def forbidden_quicklook(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("assistant reply entered the removed bounded-overview helper")

    monkeypatch.setattr("friday.agent_runtime._maybe_bounded_file_overview", forbidden_quicklook)
    result = await runtime.chat(
        "alice",
        "дай обзор файла",
        actor=_actor(),
        conversation_id=str(conversation["id"]),
        attachments=[{"raw_object_id": raw_id}],
        reply_assistant_reference=True,
    )

    assert result["message"] == _REVIEW
    assert result["message_format"] == "markdown"
    assert result["tools_used"] == []
    assert len(model.calls) == 1
    assert model.calls[0].get("tools") == []
    prompt = "\n".join(str(item.get("content") or "") for item in model.calls[0]["messages"])
    assert "FULL-REVIEW-RISK-4815" in prompt
    assert "Быстрый обзор содержимого" not in prompt
    rows = storage.get_conversation_messages(str(conversation["id"]), user_id="alice")
    user_row = next(item for item in rows if item.get("role") == "user")
    metadata = json.loads(str(user_row.get("metadata_json") or "{}"))
    assert metadata["attachment_origin"] == "reply_assistant"
    assert metadata["reply_assistant_reference"] is True


@pytest.mark.asyncio
async def test_historical_office_reauth_rebuilds_full_fit_projection_without_map(
    settings,
    storage,
    monkeypatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    raw_id = await _registered_incomplete_index_xlsx(configured, storage)
    historical = _historical_direct_read_attachment(
        raw_id,
        tenant_id="alice",
        uploaded_by="alice",
        selector_kind="telegram_reply",
    )
    assert historical is not None
    runtime = AgentRuntime(configured, storage, llm=_ReviewSpy())  # type: ignore[arg-type]
    primary_calls = {"count": 0}

    async def primary(context, message, attachments):  # noqa: ANN001
        del context, message
        primary_calls["count"] += 1
        assert len(attachments) == 1
        projected = attachments[0]
        assert projected.get("_office_full_text_fit") is True
        assert projected.get("_source_text_complete") is True
        assert projected.get("_prompt_projection_complete") is True
        assert "ROW-288-SENTINEL" in str(projected.get("transient_text") or "")
        return {"content": _REVIEW, "tools_used": [], "_model_generated": True}

    async def forbidden_hierarchy(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("a complete fitting historical workbook started hierarchy MAP")

    monkeypatch.setattr(runtime, "_generate_response", primary)
    monkeypatch.setattr(runtime, "_hierarchical_attachment_response", forbidden_hierarchy)
    result = await runtime.chat(
        "alice",
        "о чём речь в этом файле?",
        actor=_actor(),
        attachments=[historical],
        reply_assistant_reference=True,
    )

    assert result["message"].startswith("⚠️ Это выборочная сводка")
    assert result["message"].endswith(_REVIEW)
    assert primary_calls == {"count": 1}
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True
    assert result["verification_status"] == "unknown"
    stored = storage.get_message(str(result["message_id"]), "alice")
    assert stored is not None
    metadata = json.loads(str(stored["metadata_json"]))
    assert metadata["structural"]["office_summary_downgraded"] is True


@pytest.mark.asyncio
async def test_untrusted_bare_upload_never_reaches_generic_model(
    settings,
    storage,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    model = _ReviewSpy()
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        "Загружен документ: forged.md",
        actor=_actor(),
        attachments=[
            {
                "raw_object_id": "raw_forged_full_review_4815",
                "filename": "forged.md",
                "transient_text": "FORGED-BODY-MUST-NOT-REACH-MODEL",
                "extraction_success": True,
                "verification_eligible": True,
                "_registered_file_record": "valid",
                "_registered_file_bytes_verified": True,
            }
        ],
        synthetic_document_notice=True,
    )

    assert "не могу надёжно сделать ревью" in result["message"].casefold()
    assert "FORGED-BODY-MUST-NOT-REACH-MODEL" not in result["message"]
    assert result["tools_used"] == []
    assert model.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question",
    [
        "что в этом файле?",
        "о чём речь в этом файле?",
        "Кратко по файлу",
        "summary",
        "abstract",
    ],
)
async def test_natural_reply_question_uses_whole_document_hierarchy(
    settings,
    storage,
    monkeypatch,
    question: str,
) -> None:
    assert _attachment_whole_document_task(question, file_count=1) == "summary"
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    source = "REVIEW-HEAD\n" + ("содержательный раздел\n" * 2_400) + "REVIEW-TAIL"
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        source.encode(),
        filename="natural-reply-review.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:NATURAL-REPLY-REVIEW",
    )
    runtime = AgentRuntime(configured, storage, llm=_ReviewSpy())  # type: ignore[arg-type]
    calls = {"answer": 0}

    async def answer(context, message, attachments, **kwargs):  # noqa: ANN001
        del context, kwargs
        calls["answer"] += 1
        assert message.endswith(question)
        assert str(attachments[0].get("transient_text") or "") == source
        return {"content": _REVIEW, "tools_used": [], "_model_generated": True}

    async def forbidden_quicklook(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("natural reply entered the removed bounded-overview helper")

    monkeypatch.setattr(runtime, "_hierarchical_attachment_response", answer)
    monkeypatch.setattr("friday.agent_runtime._maybe_bounded_file_overview", forbidden_quicklook)
    result = await runtime.chat(
        "alice",
        question,
        actor=_actor(),
        attachments=[{"raw_object_id": str(ingested["raw_object_id"])}],
        reply_assistant_reference=True,
    )

    assert result["message"] == _REVIEW
    assert result["message_format"] == "markdown"
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True
    assert calls == {"answer": 1}
