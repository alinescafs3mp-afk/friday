"""Deidentified regressions for owner file turns followed by public web turns.

Every file, model response, and web result in this module is synthetic.  The
tests exercise the full ``AgentRuntime.chat`` boundary without a live model,
private corpus, network provider, or production account.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from docx import Document

from friday.agent_runtime import AgentRuntime, asks_for_the_web
from friday.agent_runtime._office_attachments import (
    OFFICE_EXACT_UNAVAILABLE_MESSAGE,
    OFFICE_STRUCTURE_KEY,
    validate_runtime_office_index,
)
from friday.documents import DocumentExtractor
from friday.execution_kernel import ToolResult
from friday.office_attestation import (
    OFFICE_STRUCTURE_ATTESTATION_METADATA_KEY,
    sign_office_structure_index,
)
from friday.permissions import ActorContext
from friday.storage.models import RawObject, new_id

OWNER = "owner_web_file_regression"
NEWS_REQUEST = "Покажешь свежие новости за прошедшие сутки?"
PUBLIC_URL = "https://public.synthetic.example.com/news"
PUBLIC_FACT = "Синтетическая публичная новость подтверждена источником."
PRIVATE_PREFIX = "PRIVATE-SYNTHETIC-DOC-CANARY"


def _actor() -> ActorContext:
    return ActorContext(user_id=OWNER, preset_key="owner", source="synthetic-test")


class _AllowAll:
    def authorize(self, actor, capability, **kwargs):  # noqa: ANN001, ARG002
        return SimpleNamespace(allowed=True)


def _tool(name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "synthetic bounded public read",
            "parameters": {"type": "object"},
        },
    }


class _SyntheticWebKernel:
    authorization = _AllowAll()

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_tool_definitions(self, actor, topic=""):  # noqa: ANN001, ARG002
        return [_tool("web_research")]

    async def execute(self, tool, params, actor=None):  # noqa: ANN001, ARG002
        assert tool == "web_research"
        self.calls.append((str(tool), dict(params)))
        return ToolResult(
            tool,
            True,
            {
                "outbound_attempted": True,
                "sources": [
                    {
                        "url": PUBLIC_URL,
                        "title": "Synthetic public news source",
                        "text": PUBLIC_FACT,
                        "text_length": len(PUBLIC_FACT),
                        "status_code": 200,
                        "error": "",
                        "truncated": False,
                    }
                ],
                "requested_sources": 1,
                "completed_sources": 1,
                "failed_sources": 0,
                "timed_out_sources": 0,
                "search_timed_out": False,
            },
        )


class _ScriptedModel:
    enabled = True
    model = "synthetic-owner-regression"
    total_budget_sec = 1.0

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = dict(answers)
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        snapshot = {
            "messages": [dict(item) for item in messages],
            "tools": [dict(item) for item in (tools or [])],
        }
        self.calls.append(snapshot)
        rendered = json.dumps(snapshot, ensure_ascii=False)
        if PUBLIC_FACT in rendered and PUBLIC_URL in rendered:
            return {
                "content": f"{PUBLIC_FACT} {PUBLIC_URL}",
                "tool_calls": None,
                "_queue_wait_sec": 0.0,
            }
        for marker, answer in self.answers.items():
            if marker in rendered:
                return {
                    "content": answer,
                    "tool_calls": None,
                    "_queue_wait_sec": 0.0,
                }
        raise AssertionError("synthetic model received an uncontracted payload")


class _NeverModel:
    enabled = True
    model = "synthetic-never-called"
    total_budget_sec = 1.0

    async def chat(self, messages, tools=None, **kwargs):  # noqa: ANN001, ARG002
        raise AssertionError("code-owned file/privacy path reached the model")


@dataclass(frozen=True)
class _StoredDocument:
    raw: RawObject
    attachment: dict[str, Any]
    text: str
    names: tuple[str, ...]
    marker: str


def _render_docx(
    *,
    document_number: int,
    padding: int,
    prefix: str = PRIVATE_PREFIX,
) -> tuple[bytes, Any, tuple[str, ...]]:
    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "ФИО"
    table.rows[0].cells[1].text = "Должность"
    names = tuple(f"{prefix}-{document_number}-PERSON-{row_number:02d}" for row_number in range(1, 4))
    for row_number, name in enumerate(names, start=1):
        cells = table.add_row().cells
        cells[0].text = name
        cells[1].text = f"SYNTHETIC-ROLE-{document_number}-{row_number:02d}" + (
            "Z" * padding if row_number == 3 else ""
        )
    stream = io.BytesIO()
    document.save(stream)
    payload = stream.getvalue()
    extracted = DocumentExtractor().extract(payload, f"synthetic-{document_number}.docx")
    assert extracted.success is True
    return payload, extracted, names


def _store_docx(
    storage,
    *,
    target_chars: int,
    document_number: int,
    complete: bool = True,
    prefix: str = PRIVATE_PREFIX,
) -> _StoredDocument:
    _, baseline, _ = _render_docx(
        document_number=document_number,
        padding=0,
        prefix=prefix,
    )
    padding = target_chars - len(baseline.text)
    assert padding >= 0
    payload, extracted, names = _render_docx(
        document_number=document_number,
        padding=padding,
        prefix=prefix,
    )
    assert len(extracted.text) == target_chars
    index = extracted.office_structure_index
    assert isinstance(index, dict)
    assert index.get("complete") is True
    assert validate_runtime_office_index(index, extracted.text) == index

    source_hash = hashlib.sha256(payload).hexdigest()
    metadata: dict[str, Any] = {
        "filename": f"synthetic-{document_number}-{target_chars}.docx",
        "uploaded_by": OWNER,
        "extraction_success": True,
        "text_extraction_success": True,
    }
    if complete:
        token = sign_office_structure_index(storage, index, source_hash)
        assert isinstance(token, str)
        metadata.update(
            {
                OFFICE_STRUCTURE_KEY: index,
                OFFICE_STRUCTURE_ATTESTATION_METADATA_KEY: token,
            }
        )
    else:
        # The text is readable, but the parser explicitly cannot prove that the
        # source ended where the extracted text ends.
        metadata["source_truncated_for_parse"] = True

    raw = RawObject(
        id=new_id("raw"),
        user_id=OWNER,
        source="synthetic-docx",
        source_ref=new_id("synthetic-source"),
        raw_content=extracted.text,
        content_type="file",
        content_hash=source_hash,
        metadata_json=metadata,
    )
    storage.store_raw_object(raw)
    attachment = {
        "raw_object_id": raw.id,
        "filename": metadata["filename"],
        "transient_text": extracted.text,
        "extraction_success": True,
        "verification_eligible": True,
        **({"source_truncated_for_parse": True} if not complete else {}),
    }
    return _StoredDocument(
        raw=raw,
        attachment=attachment,
        text=extracted.text,
        names=names,
        marker=names[0],
    )


def _store_generic_text(storage) -> _StoredDocument:
    marker = "PRIVATE-SYNTHETIC-LOCAL-NEWS-CANARY"
    text = f"{marker}\nНовости в документе за прошедшие сутки: синтетическая локальная запись."
    raw = RawObject(
        id=new_id("raw"),
        user_id=OWNER,
        source="synthetic-text",
        source_ref=new_id("synthetic-source"),
        raw_content=text,
        content_type="file",
        metadata_json={
            "filename": "synthetic-local-news.txt",
            "uploaded_by": OWNER,
            "extraction_success": True,
            "text_extraction_success": True,
        },
    )
    storage.store_raw_object(raw)
    return _StoredDocument(
        raw=raw,
        attachment={
            "raw_object_id": raw.id,
            "filename": "synthetic-local-news.txt",
            "transient_text": text,
            "extraction_success": True,
            "verification_eligible": True,
        },
        text=text,
        names=(),
        marker=marker,
    )


def _stored_metadata(storage, response: dict[str, Any]) -> dict[str, Any]:
    row = storage.get_message(str(response["message_id"]), OWNER)
    assert row is not None
    metadata = json.loads(str(row["metadata_json"] or "{}"))
    assert isinstance(metadata, dict)
    return metadata


async def _upload_notice(
    runtime: AgentRuntime,
    document: _StoredDocument,
    *,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    return await runtime.chat(
        OWNER,
        f"Загружен документ: {document.attachment['filename']}",
        actor=_actor(),
        conversation_id=conversation_id,
        attachments=[document.attachment],
        synthetic_document_notice=True,
    )


def _source_user_message(storage, response: dict[str, Any]) -> dict[str, Any]:
    rows = storage.get_conversation_messages(str(response["conversation_id"]), user_id=OWNER)
    source = next(item for item in reversed(rows) if item["role"] == "user")
    return source


@pytest.mark.asyncio
async def test_three_complete_bare_docx_summaries_survive_then_news_keeps_history(
    settings,
    storage,
) -> None:
    storage.ensure_user(OWNER, preset_key="owner")
    documents = [
        _store_docx(storage, target_chars=target, document_number=number)
        for number, target in enumerate((832, 1416, 1590), start=1)
    ]
    expected_summaries = {
        document.marker: (
            f"Синтетическая сводка {number}: в документе указаны ровно 3 позиции — "
            f"{', '.join(document.names)}."
        )
        for number, document in enumerate(documents, start=1)
    }
    model = _ScriptedModel(expected_summaries)
    kernel = _SyntheticWebKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )

    conversation_id: str | None = None
    for document in documents:
        response = await _upload_notice(runtime, document, conversation_id=conversation_id)
        conversation_id = str(response["conversation_id"])

        assert response["message"] == expected_summaries[document.marker]
        assert response["message"] != OFFICE_EXACT_UNAVAILABLE_MESSAGE
        assert response["tools_used"] == []
        assert response["attachment_context_expected_count"] == 1
        assert response["attachment_context_readable_count"] == 1
        assert response["attachment_coverage_complete"] is True
        assert response["attachment_verification_complete"] is True
        assert response["restored_attachment_count"] == 0
        metadata = _stored_metadata(storage, response)
        assert metadata["structural"]["model_spoke"] is True
        assert metadata["structural"]["verdict_kind"] == ""
        assert metadata["attachment_coverage_complete"] is True

    assert len(model.calls) == 3
    assert kernel.calls == []
    assert asks_for_the_web(NEWS_REQUEST) is True

    news = await runtime.chat(
        OWNER,
        NEWS_REQUEST,
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert kernel.calls == [
        (
            "web_research",
            {"query": runtime.web_query_from(NEWS_REQUEST), "max_sources": 3},
        )
    ]
    assert news["tools_used"] == ["web_research"]
    assert news["web_evidence_status"] == "sourced"
    assert news["web_sources"] == [{"url": PUBLIC_URL, "title": "Synthetic public news source"}]
    assert news["attachment_context_expected_count"] == 0
    assert news["attachment_context_readable_count"] == 0
    assert news["restored_attachment_count"] == 0
    metadata = _stored_metadata(storage, news)
    assert metadata["private_context_lineage"] is True
    assert metadata["structural"].get("private_web_search_blocked") is not True

    assert len(model.calls) > 3
    public_payload = json.dumps(model.calls[-1], ensure_ascii=False)
    outbound_payload = json.dumps(kernel.calls, ensure_ascii=False)
    assert NEWS_REQUEST in public_payload
    assert PUBLIC_FACT in public_payload
    assert PRIVATE_PREFIX in public_payload
    assert "Загружен документ" in public_payload
    assert PRIVATE_PREFIX not in outbound_payload


@pytest.mark.asyncio
async def test_old_attachment_lineage_beyond_prompt_tail_still_allows_only_current_news(
    settings,
    storage,
) -> None:
    storage.ensure_user(OWNER, preset_key="owner")
    document = _store_docx(storage, target_chars=832, document_number=41)
    summary = "Синтетическая закрытая сводка с PRIVATE-HISTORICAL-ANSWER-CANARY."
    model = _ScriptedModel({document.marker: summary})
    kernel = _SyntheticWebKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )
    uploaded = await _upload_notice(runtime, document)
    conversation_id = str(uploaded["conversation_id"])

    stale = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    with storage.transaction() as connection:
        connection.execute(
            "UPDATE messages SET created_at=? WHERE conversation_id=? AND user_id=?",
            (stale, conversation_id, OWNER),
        )
    for number in range(24):
        storage.store_message(
            conversation_id,
            OWNER,
            "user",
            f"SYNTHETIC-CLEAN-USER-{number:02d}",
            metadata={"private_context_lineage": True},
        )
        storage.store_message(
            conversation_id,
            OWNER,
            "assistant",
            f"SYNTHETIC-CLEAN-ASSISTANT-{number:02d}",
            metadata={"private_context_lineage": True},
        )

    response = await runtime.chat(
        OWNER,
        NEWS_REQUEST,
        actor=_actor(),
        conversation_id=conversation_id,
    )

    assert response["tools_used"] == ["web_research"]
    assert response["web_evidence_status"] == "sourced"
    assert response["restored_attachment_count"] == 0
    assert len(kernel.calls) == 1
    public_payload = json.dumps(model.calls[-1], ensure_ascii=False)
    assert PRIVATE_PREFIX not in public_payload
    assert "PRIVATE-HISTORICAL-ANSWER-CANARY" not in public_payload
    assert "SYNTHETIC-CLEAN-USER" not in public_payload
    assert PRIVATE_PREFIX not in json.dumps(kernel.calls, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "lists_people"),
    [
        ("Перечисли всех людей из файла.", True),
        ("Сколько людей в файле?", False),
    ],
)
async def test_complete_signed_docx_exact_list_and_count_are_code_owned(
    settings,
    storage,
    question: str,
    lists_people: bool,
) -> None:
    storage.ensure_user(OWNER, preset_key="owner")
    document = _store_docx(storage, target_chars=1416, document_number=52)
    kernel = _SyntheticWebKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=_NeverModel(),
        kernel=kernel,
    )

    response = await runtime.chat(
        OWNER,
        question,
        actor=_actor(),
        attachments=[document.attachment],
    )

    assert response["verification_status"] == "passed"
    assert response["verified"] is True
    assert response["message_format"] == "plain"
    assert "3" in response["message"]
    if lists_people:
        assert all(name in response["message"] for name in document.names)
    else:
        assert all(name not in response["message"] for name in document.names)
    assert response["tools_used"] == []
    assert response["web_evidence_status"] == "none"
    assert response["attachment_coverage_complete"] is True
    assert kernel.calls == []
    metadata = _stored_metadata(storage, response)
    assert metadata["structural"]["verdict_kind"] == "office_exact"
    assert metadata["structural"]["model_spoke"] is False


@pytest.mark.asyncio
async def test_readable_incomplete_docx_is_partial_then_exact_followup_is_unknown(
    settings,
    storage,
) -> None:
    storage.ensure_user(OWNER, preset_key="owner")
    document = _store_docx(
        storage,
        target_chars=832,
        document_number=63,
        complete=False,
        prefix="PRIVATE-SYNTHETIC-INCOMPLETE-CANARY",
    )
    partial = f"По доступной части документа видна позиция {document.names[0]}; полный состав неизвестен."
    model = _ScriptedModel({document.marker: partial})
    kernel = _SyntheticWebKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )

    uploaded = await _upload_notice(runtime, document)

    # A structurally incomplete source may be settled before synthesis.  What
    # matters publicly is that readable bytes remain readable while whole-file
    # certainty is UNKNOWN; it must not be reported as an absent/lost upload.
    assert uploaded["message"] != OFFICE_EXACT_UNAVAILABLE_MESSAGE
    assert "неполон" in uploaded["message"].casefold()
    assert "неизвест" in uploaded["message"].casefold()
    assert "прикреп" not in uploaded["message"].casefold()
    assert "файл отсутств" not in uploaded["message"].casefold()
    assert uploaded["attachment_context_available"] is True
    assert uploaded["attachment_context_readable_count"] == 1
    assert uploaded["attachment_coverage_complete"] is False
    assert uploaded["attachment_verification_complete"] is False
    assert uploaded["verification_status"] == "unknown"
    assert uploaded["tools_used"] == []
    calls_after_partial = len(model.calls)

    exact = await runtime.chat(
        OWNER,
        "Перечисли всех людей из файла.",
        actor=_actor(),
        conversation_id=str(uploaded["conversation_id"]),
    )

    assert exact["message"] == OFFICE_EXACT_UNAVAILABLE_MESSAGE
    assert exact["verification_status"] == "unknown"
    assert exact["verified"] is False
    assert exact["restored_attachment_count"] == 1
    assert exact["attachment_context_available"] is True
    assert exact["attachment_context_readable_count"] == 1
    assert exact["attachment_coverage_complete"] is False
    assert exact["tools_used"] == []
    assert len(model.calls) == calls_after_partial
    assert kernel.calls == []
    metadata = _stored_metadata(storage, exact)
    assert metadata["structural"]["verdict_kind"] == "office_exact"
    assert metadata["structural"]["model_spoke"] is False


@pytest.mark.asyncio
async def test_news_inside_a_current_document_is_local_not_a_web_request(
    settings,
    storage,
) -> None:
    storage.ensure_user(OWNER, preset_key="owner")
    request = "Покажи новости в документе за прошедшие сутки."
    assert asks_for_the_web(request) is False
    document = _store_generic_text(storage)
    answer = "В документе есть одна синтетическая локальная новостная запись."
    model = _ScriptedModel({document.marker: answer})
    kernel = _SyntheticWebKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )

    response = await runtime.chat(
        OWNER,
        request,
        actor=_actor(),
        attachments=[document.attachment],
    )

    assert response["message"] == answer
    assert response["tools_used"] == []
    assert response["web_evidence_status"] == "none"
    assert response["attachment_context_expected_count"] == 1
    assert response["attachment_context_readable_count"] == 1
    assert kernel.calls == []
    metadata = _stored_metadata(storage, response)
    assert metadata["structural"].get("private_web_search_blocked") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("carrier", "expected_count", "expected_restored"),
    [
        ("current", 1, 0),
        ("explicit_reference", 1, 1),
        ("deictic_reference", 1, 1),
        ("reply_reference", 0, 0),
        ("replayed_attachment", 1, 1),
    ],
)
async def test_attachment_derived_news_carriers_can_use_web(
    settings,
    storage,
    carrier: str,
    expected_count: int,
    expected_restored: int,
) -> None:
    storage.ensure_user(OWNER, preset_key="owner")
    document = _store_docx(storage, target_chars=832, document_number=74)
    summary = f"Синтетическая сводка: {document.names[0]}."
    model = _ScriptedModel({document.marker: summary})
    kernel = _SyntheticWebKernel()
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )

    conversation_id: str | None = None
    query = NEWS_REQUEST
    chat_kwargs: dict[str, Any] = {}
    if carrier in {"explicit_reference", "deictic_reference", "reply_reference"}:
        uploaded = await _upload_notice(runtime, document)
        conversation_id = str(uploaded["conversation_id"])
        if carrier == "explicit_reference":
            query = "Найди в интернете свежие новости из загруженного документа за прошедшие сутки."
        elif carrier == "deictic_reference":
            query = "Найди в интернете свежие новости по нему за прошедшие сутки."
        else:
            chat_kwargs["reply_to"] = f"Ответ по файлу: {document.marker}"
    elif carrier == "current":
        chat_kwargs["attachments"] = [document.attachment]
    else:
        first = await runtime.chat(
            OWNER,
            query,
            actor=_actor(),
            attachments=[document.attachment],
        )
        conversation_id = str(first["conversation_id"])
        source = _source_user_message(storage, first)
        chat_kwargs["replay_source_message_id"] = str(source["id"])

    model_calls_before = len(model.calls)
    kernel_calls_before = len(kernel.calls)
    assert asks_for_the_web(query) is True
    response = await runtime.chat(
        OWNER,
        query,
        actor=_actor(),
        conversation_id=conversation_id,
        **chat_kwargs,
    )

    assert response["tools_used"] == ["web_research"]
    assert response["web_evidence_status"] == "sourced"
    assert response["attachment_context_expected_count"] == expected_count
    assert response["restored_attachment_count"] == expected_restored
    assert len(model.calls) > model_calls_before
    assert kernel.calls[kernel_calls_before:] == [
        (
            "web_research",
            {"query": runtime.web_query_from(query), "max_sources": 3},
        )
    ]
    metadata = _stored_metadata(storage, response)
    assert metadata["structural"].get("private_web_search_blocked") is not True
    assert metadata["structural"]["model_spoke"] is True
