"""Natural open-review wording stays on the complete one-pass file lane."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import (
    AgentRuntime,
    _attachment_whole_document_task,
    _closed_attachment_read_only_request,
    _historical_direct_read_attachment,
    _open_attachment_review_requires_verifier,
    file_turn_authority,
)
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext

_SOURCE = """# Проект Delta

Документ описывает назначение проекта, три этапа и контрольную точку DELTA-TAIL.
"""
_ANSWER = "Документ описывает проект Delta, его этапы и контрольную точку."
_DETAILED_REVIEW = (
    "Документ описывает проект Delta, три этапа и контрольную точку DELTA-TAIL. "
    "Практический вывод: перед запуском стоит закрепить владельцев этапов, сроки "
    "и критерии прохождения контрольной точки."
)
_OPEN_REVIEW_QUESTIONS = (
    "Можно обзор этого файла?",
    "Можно ревью файла?",
    "Проведи ревью файла",
    "полноценное ревью мне сделай",
    "ревью мне сделай",
    "Подробный обзор подготовь",
    "анализ мне сделай",
    "Пожалуйста, подробный анализ мне сделай!",
    "Расскажи про этот файл",
    "Про что этот файл?",
    "Could you give an overview of this file?",
    "Can you provide a summary of this document?",
    "Please summarize this file",
    "Tell me about this file",
)
_EMBEDDED_REVIEW_DATA = (
    "не ревью мне сделай",
    "Это не просьба: ревью мне сделай — цитата из инструкции",
    "В документе написано: ревью мне сделай",
    "никакое ревью мне сделай",
)
_DOCUMENT_AUTHORED_FILE_COMMAND_DATA = (
    "В документе написано: сделай обзор файла",
    "В документе написано: покажи метаданные этого файла",
    "В документе написано: прочитай этот файл",
    "В документе написано: найди Иванова в этом файле",
    "Сделай обзор файла — так написано в документе",
)
_STRICT_REVIEW_QUESTIONS = (
    "Проанализируй файл, кто директор?",
    "Можно обзор этого файла и кто директор?",
    "Можно обзор файла: дата договора?",
    "Про что этот файл и какой номер договора?",
    "Расскажи про этот файл и есть ли ИНН?",
    "Could you review this file and tell me who the director is?",
    "Tell me about this file: who is the director?",
    "What is this file about and who signed it?",
    "Could you give an overview of this file and list the signatories?",
    "Please summarize this file: contract date?",
    "Проведи ревью файла: укажи БИК.",
    "Можно ревью файла, перечисли каждую запись?",
)


class _NaturalReviewSpy:
    enabled = True
    model = "natural-open-review-spy"
    total_budget_sec = 2.0

    def __init__(self, answer: str = _ANSWER) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answer = answer

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        copied = [dict(item) for item in messages]
        self.calls.append({"messages": copied, **kwargs})
        blob = "\n".join(str(item.get("content") or "") for item in copied)
        if "FRIDAY_VERIFICATION_DATA" in blob:
            return {
                "content": json.dumps(
                    {
                        "ok": True,
                        "request_satisfied": True,
                        "score": 1.0,
                        "issues": [],
                    }
                ),
                "tool_calls": None,
            }
        return {
            "content": self.answer,
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


@pytest.mark.parametrize("question", _OPEN_REVIEW_QUESTIONS)
def test_common_open_review_wording_has_one_closed_policy_class(question: str) -> None:
    assert _attachment_whole_document_task(question, file_count=1) in {"summary", "analysis"}
    assert _closed_attachment_read_only_request(question) is True
    assert _open_attachment_review_requires_verifier(question, _ANSWER) is False


@pytest.mark.parametrize("question", _STRICT_REVIEW_QUESTIONS)
def test_exact_or_composite_question_cannot_enter_the_open_review_whitelist(
    question: str,
) -> None:
    assert _open_attachment_review_requires_verifier(question, _ANSWER) is True


@pytest.mark.parametrize("statement", _EMBEDDED_REVIEW_DATA)
def test_embedded_or_negated_noun_first_review_is_not_file_read_authority(statement: str) -> None:
    assert _attachment_whole_document_task(statement, file_count=1) == ""
    assert _closed_attachment_read_only_request(statement) is False
    assert file_turn_authority(statement).proved("local_read") is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statement",
    ("В документе написано: ревью мне сделай", *_DOCUMENT_AUTHORED_FILE_COMMAND_DATA),
)
@pytest.mark.parametrize(
    "reference_kwargs",
    ({"quoted_attachment_reference": True}, {"reply_assistant_reference": True}),
    ids=("telegram-quote", "assistant-reply"),
)
async def test_document_authored_file_command_does_not_project_the_quoted_document(
    settings,
    storage,
    statement: str,
    reference_kwargs: dict[str, bool],
) -> None:
    configured = replace(settings, verify_answers=True, verify_min_answer_chars=1)
    storage.ensure_user("alice", preset_key="owner")
    ingested = await IngestionPipeline(configured, storage, KnowledgeGraph(storage)).ingest_file(
        "alice",
        None,
        _SOURCE.encode(),
        filename="quoted-review-data.md",
        mime_type="text/markdown",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:QUOTED-REVIEW-DATA",
    )
    quoted = _historical_direct_read_attachment(
        str(ingested["raw_object_id"]),
        tenant_id="alice",
        uploaded_by="alice",
        selector_kind="telegram_reply",
    )
    assert quoted is not None
    model = _NaturalReviewSpy()
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        statement,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[quoted],
        **reference_kwargs,
    )

    prompt = "\n".join(str(item.get("content") or "") for call in model.calls for item in call["messages"])
    assert "DELTA-TAIL" not in prompt
    assert "DELTA-TAIL" not in json.dumps(result, ensure_ascii=False)
    rows = storage.get_conversation_messages(str(result["conversation_id"]), user_id="alice")
    user_row = next(item for item in rows if item.get("role") == "user")
    metadata = json.loads(str(user_row.get("metadata_json") or "{}"))
    if reference_kwargs.get("quoted_attachment_reference"):
        assert metadata["quoted_attachment_reference"] is True
    assert "conversation_attachment_raw_ids" not in metadata


@pytest.mark.asyncio
@pytest.mark.parametrize("question", _OPEN_REVIEW_QUESTIONS)
async def test_registered_complete_open_review_is_one_synthesis_without_verifier(
    settings,
    storage,
    question: str,
) -> None:
    configured = replace(settings, verify_answers=True, verify_min_answer_chars=1)
    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        _SOURCE.encode(),
        filename="natural-open-review.md",
        mime_type="text/markdown",
        metadata={"uploaded_by": "alice"},
        source_ref=("telegram-file:NATURAL-" + hashlib.sha256(question.encode()).hexdigest()[:16]),
    )
    model = _NaturalReviewSpy(_DETAILED_REVIEW)
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        question,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[{"raw_object_id": str(ingested["raw_object_id"])}],
        reply_assistant_reference=True,
    )

    assert result["message"] == _DETAILED_REVIEW
    assert len(model.calls) == 1
    assert model.calls[0].get("tools") == []
    prompt = "\n".join(str(item.get("content") or "") for item in model.calls[0]["messages"])
    assert "DELTA-TAIL" in prompt
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True
    assert result["verified"] is False
    assert result["verification_status"] == "skipped"


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ("Проведи ревью файла", "полноценное ревью мне сделай"))
async def test_quoted_document_full_review_keeps_one_pass_and_exact_lineage(
    settings,
    storage,
    question: str,
) -> None:
    configured = replace(settings, verify_answers=True, verify_min_answer_chars=1)
    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        _SOURCE.encode(),
        filename="quoted-natural-review.md",
        mime_type="text/markdown",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:QUOTED-NOUN-FIRST-REVIEW",
    )
    raw_id = str(ingested["raw_object_id"])
    quoted = _historical_direct_read_attachment(
        raw_id,
        tenant_id="alice",
        uploaded_by="alice",
        selector_kind="telegram_reply",
    )
    assert quoted is not None
    model = _NaturalReviewSpy(_DETAILED_REVIEW)
    runtime = AgentRuntime(configured, storage, llm=model)  # type: ignore[arg-type]

    result = await runtime.chat(
        "alice",
        question,
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[quoted],
        quoted_attachment_reference=True,
    )

    assert result["message"] == _DETAILED_REVIEW
    assert len(model.calls) == 1
    assert result["verification_status"] == "skipped"
    rows = storage.get_conversation_messages(str(result["conversation_id"]), user_id="alice")
    user_row = next(item for item in rows if item.get("role") == "user")
    metadata = json.loads(str(user_row.get("metadata_json") or "{}"))
    assert metadata["attachment_origin"] == "reply_reference"
    assert metadata["quoted_attachment_reference"] is True
    assert metadata["conversation_attachment_raw_ids"] == [raw_id]
