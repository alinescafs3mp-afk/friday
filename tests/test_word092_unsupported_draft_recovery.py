"""Named-version DOCX behavior after an unsupported-deed draft."""

from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import logging
from dataclasses import replace

import docx
import pytest
from fastapi.testclient import TestClient
from test_attachment_conversation_continuity import _current_attachment, _pending_file
from test_long_document_query_contract import _DocumentLLM, _messages_blob

from friday.agent_runtime import (
    _MODEL_ANY_DOMAIN_OR_IP,
    _UNCONFIRMED_SUPPORTED_DEED,
    AgentContext,
    AgentRuntime,
    _direct_complete_source_file_body,
    _direct_complete_source_file_line_count,
    _reconcile_attachment_web_literals,
    _runtime_unconfirmed_supported_deed,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.server import create_app

SOURCE_NAME = "заметки-v2.txt"
OUTPUT_NAME = "отчёт-v2.docx"
SOURCE_BODY = "Проект: КУХНЯ\nОтветственный: Соколова\nКоличество участников: 17"
NAMED_VERSION_REQUEST = (
    "Из приложенного заметки-v2.txt сделай отчёт-v2.docx. Нужен скачиваемый Word-документ, "
    "содержащий все три исходные строки с точными значениями."
)
GROUNDED_COMPLETION = "Сведения об исходнике\n\n" + "\n\n".join(SOURCE_BODY.splitlines())
UNSUPPORTED_COMPLETION = "Я создала и прикрепила отчёт-v2.docx."
UNSUPPORTED_WITHOUT_FILENAME = "Я создала и прикрепила документ."
STRIPPED_PUBLISHED = "Я создала и прикрепила ."
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
INITIAL_GUARD_LOG = "supported-deed: неподтверждённое завершение заменено"
REPAIR_GUARD_LOG = "supported-deed: repair вернул неподтверждённое завершение"
LATE_CARRIER_LOG = "output-carrier: поздний файл отклонён до рендера"
RECOVERY_LOG = "output-carrier: authenticated complete-source recovery selected"
FILL_PROMPT = "Напиши СОДЕРЖИМОЕ документа"
ACTOR = ActorContext(user_id="alice", preset_key="owner", source="telegram-bridge")


class _BoundaryLLM(_DocumentLLM):
    """Existing document fixture with only the LLM/network boundary faked."""

    async def chat(self, messages: list[dict], **kwargs):  # noqa: ANN001
        blob = _messages_blob(messages)
        if "РАЗГОВОР или ЗАПРОС" in blob:
            copied = [copy.deepcopy(item) for item in messages]
            self.calls.append({"messages": copied, "kwargs": copy.deepcopy(kwargs)})
            return {"content": "ЗАПРОС"}
        return await super().chat(messages, **kwargs)


def _prompt_text(llm: _DocumentLLM) -> str:
    chunks: list[str] = []
    for call in llm.calls:
        for item in call["messages"]:
            chunks.append(_flatten_content(item.get("content")))
    return "\n".join(chunks)


def _flatten_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_flatten_content(part) for part in content)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content or "")


def _assistant_text(payload: dict) -> str:
    message = payload.get("message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    if isinstance(message, str) and message:
        return message
    return str(payload.get("answer") or "")


def _assert_docx_has_exact_source_body(content: bytes, source_body: str = SOURCE_BODY) -> None:
    document = docx.Document(io.BytesIO(content))
    paragraphs = [paragraph.text for paragraph in document.paragraphs if paragraph.text]
    assert paragraphs[1:] == source_body.splitlines(), paragraphs
    assert UNSUPPORTED_COMPLETION not in "\n".join(paragraphs)
    assert UNSUPPORTED_WITHOUT_FILENAME not in "\n".join(paragraphs)


def _deed_unconfirmed(text: str) -> bool:
    return _runtime_unconfirmed_supported_deed(
        text,
        requested_effects=frozenset({"file"}),
        has_file=False,
        reminder_succeeded=False,
        reminder_delivery_scheduled=False,
        voice_succeeded=False,
        passive_source_state=False,
        read_only_attachment_review=False,
    )


async def _named_version_runtime_turn(
    settings,
    storage,
    monkeypatch,
    llm: _BoundaryLLM,
    *,
    request: str = NAMED_VERSION_REQUEST,
    source_body: str = SOURCE_BODY,
) -> dict:
    storage.ensure_user("alice", source="test", preset_key="owner")
    source = _pending_file(
        storage,
        "alice",
        "alice",
        source_body,
        filename=SOURCE_NAME,
        extra_metadata={"stored_path": "alice/" + SOURCE_NAME, "mime_type": "text/plain"},
    )
    attachment = _current_attachment(storage, source)
    assert attachment.get("transient_text") == source_body or source_body in str(
        attachment.get("transient_text") or attachment.get("text") or ""
    )
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=llm)
    runtime.kernel.bind_services(storage, None, None, None)

    async def prepare(user_id, _message, conversation_id, **_kwargs):
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=user_id)

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    return await runtime.chat(
        "alice",
        request,
        actor=ACTOR,
        attachments=[attachment],
        enable_tools=True,
    )


def _install_boundary_llm(app, llm: _BoundaryLLM) -> None:
    app.state.agent.llm = llm
    app.state.llm = llm


def _named_version_http_turn(
    settings,
    llm: _BoundaryLLM,
    source_ref: str,
    *,
    request: str = NAMED_VERSION_REQUEST,
    source_body: str = SOURCE_BODY,
) -> dict:
    app = create_app(replace(settings, verify_answers=False))
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    body = {
        "message": request,
        "source_ref": source_ref,
        "enable_tools": True,
        "document": {
            "filename": SOURCE_NAME,
            "mime_type": "text/plain",
            "content_base64": base64.b64encode(source_body.encode("utf-8")).decode("ascii"),
        },
    }
    with TestClient(app) as client:
        _install_boundary_llm(app, llm)
        response = client.post("/api/chat", headers=headers, json=body)
        assert response.status_code == 200, response.text
        payload = response.json()
        ingestion = payload.get("file_ingestion") or {}
        extraction = ingestion.get("extraction") or {}
        assert extraction.get("success") is True
        assert extraction.get("text_success") is True or extraction.get("chars", 0) >= len(source_body)
        files = payload.get("files") or []
        if len(files) == 1 and not files[0].get("content_base64"):
            download = client.get(files[0]["download_url"], headers=headers)
            assert download.status_code == 200, download.text
            payload["_test_download_bytes"] = download.content
        return payload


def _assert_source_recovered_file(payload: dict, llm: _BoundaryLLM, records: str) -> None:
    prompt = _prompt_text(llm)
    assert all(line in prompt for line in SOURCE_BODY.splitlines()), prompt[-1500:]
    assert FILL_PROMPT not in prompt
    published = _assistant_text(payload)
    assert published == f"Готово — файл {OUTPUT_NAME} приложен к ответу."
    assert published != _UNCONFIRMED_SUPPORTED_DEED
    assert UNSUPPORTED_COMPLETION not in published
    assert UNSUPPORTED_WITHOUT_FILENAME not in published
    files = payload.get("files") or []
    assert len(files) == 1
    generated = files[0]
    assert generated["filename"] == OUTPUT_NAME
    assert generated["mime_type"] == DOCX_MIME
    content = (
        base64.b64decode(generated["content_base64"], validate=True)
        if generated.get("content_base64")
        else payload["_test_download_bytes"]
    )
    _assert_docx_has_exact_source_body(content)
    assert payload.get("tools_used") == ["make_file"]
    assert REPAIR_GUARD_LOG not in records
    assert LATE_CARRIER_LOG not in records
    assert RECOVERY_LOG in records


def test_named_output_filename_is_stripped_as_web_literal_before_deed_guard() -> None:
    matches = [item.group(0) for item in _MODEL_ANY_DOMAIN_OR_IP.finditer(UNSUPPORTED_COMPLETION)]
    assert matches == [OUTPUT_NAME]
    stripped, changed = _reconcile_attachment_web_literals(UNSUPPORTED_COMPLETION, allowed=frozenset())
    assert changed is True
    assert stripped == STRIPPED_PUBLISHED
    assert _deed_unconfirmed(UNSUPPORTED_COMPLETION) is True
    assert _deed_unconfirmed(STRIPPED_PUBLISHED) is False


@pytest.mark.parametrize(
    "source_body",
    [
        "Поле A: 1\nПоле B: 2",
        "Поле A: 1\nПоле B: 2\nПоле C: 3\nПоле D: 4",
        "Поле A: 1\n\nПоле C: 3",
        "Произвольная строка\nПоле B: 2\nПоле C: 3",
        "Поле A: 1\nполе a: 2\nПоле C: 3",
        "Поле A: 1\nПоле B: \nПоле C: 3",
        "Поле A: 1\nПоле B: 2\x00\nПоле C: 3",
        "Поле A: " + "X" * 2_001 + "\nПоле B: 2\nПоле C: 3",
    ],
    ids=(
        "two-lines",
        "four-lines",
        "blank-line",
        "arbitrary-prose",
        "duplicate-label",
        "empty-value",
        "nul",
        "oversized-value",
    ),
)
def test_complete_source_literal_contract_rejects_ambiguous_bodies(source_body: str) -> None:
    assert _direct_complete_source_file_body(NAMED_VERSION_REQUEST, source_body) is None


@pytest.mark.asyncio
async def test_named_version_correct_body_still_delivers_docx(settings, storage, monkeypatch) -> None:
    llm = _BoundaryLLM(GROUNDED_COMPLETION)
    result = await _named_version_runtime_turn(settings, storage, monkeypatch, llm)
    assert all(line in _prompt_text(llm) for line in SOURCE_BODY.splitlines())
    assert len(result["files"]) == 1, result["message"]
    generated = result["files"][0]
    assert generated["filename"] == OUTPUT_NAME
    assert generated["mime_type"] == DOCX_MIME
    _assert_docx_has_exact_source_body(base64.b64decode(generated["content_base64"], validate=True))
    assert _assistant_text(result) != _UNCONFIRMED_SUPPORTED_DEED


@pytest.mark.parametrize(
    ("draft", "initial_guard_expected"),
    [
        (UNSUPPORTED_COMPLETION, False),
        (UNSUPPORTED_WITHOUT_FILENAME, True),
    ],
    ids=("filename-stripped-before-guard", "native-initial-guard-shape"),
)
@pytest.mark.asyncio
async def test_named_version_unsupported_prose_recovers_exact_docx(
    settings, storage, monkeypatch, caplog, draft, initial_guard_expected
) -> None:
    llm = _BoundaryLLM(draft)
    with caplog.at_level(logging.WARNING, logger="friday.agent_runtime"):
        result = await _named_version_runtime_turn(settings, storage, monkeypatch, llm)
    records = "\n".join(record.getMessage() for record in caplog.records)
    _assert_source_recovered_file(result, llm, records)
    assert (INITIAL_GUARD_LOG in records) is initial_guard_expected


def test_named_version_correct_body_public_http_delivers_docx(settings) -> None:
    llm = _BoundaryLLM(GROUNDED_COMPLETION)
    payload = _named_version_http_turn(settings, llm, "sol162-word092:before-http-success")
    assert all(line in _prompt_text(llm) for line in SOURCE_BODY.splitlines())
    files = payload.get("files") or []
    assert len(files) == 1, payload.get("message")
    generated = files[0]
    assert generated["filename"] == OUTPUT_NAME
    assert generated["mime_type"] == DOCX_MIME
    content = (
        base64.b64decode(generated["content_base64"], validate=True)
        if generated.get("content_base64")
        else payload["_test_download_bytes"]
    )
    _assert_docx_has_exact_source_body(content)
    assert _assistant_text(payload) != _UNCONFIRMED_SUPPORTED_DEED


@pytest.mark.parametrize(
    ("draft", "initial_guard_expected"),
    [
        (UNSUPPORTED_COMPLETION, False),
        (UNSUPPORTED_WITHOUT_FILENAME, True),
    ],
    ids=("filename-stripped-before-guard", "native-initial-guard-shape"),
)
def test_named_version_unsupported_prose_public_http_recovers_exact_docx(
    settings, caplog, draft, initial_guard_expected
) -> None:
    llm = _BoundaryLLM(draft)
    with caplog.at_level(logging.WARNING, logger="friday.agent_runtime"):
        payload = _named_version_http_turn(
            settings,
            llm,
            "sol162-word092:after-http-guarded",
        )
    records = "\n".join(record.getMessage() for record in caplog.records)
    _assert_source_recovered_file(payload, llm, records)
    assert (INITIAL_GUARD_LOG in records) is initial_guard_expected


@pytest.mark.parametrize(
    ("user_message", "source_body"),
    [
        (
            "Из приложенного заметки-v2.txt сделай отчёт-v2.docx, но добавь резюме. "
            "Сохрани все три исходные строки с точными значениями.",
            SOURCE_BODY,
        ),
        (NAMED_VERSION_REQUEST, "Первая произвольная строка\nВторая строка\nТретья строка"),
        (NAMED_VERSION_REQUEST, SOURCE_BODY + "\nЛишняя строка: запрещена"),
        (
            "Не создавай отчёт-v2.docx из заметки-v2.txt, даже если там все три "
            "исходные строки с точными значениями.",
            SOURCE_BODY,
        ),
    ],
    ids=("transform", "arbitrary-prose", "count-mismatch", "denied"),
)
@pytest.mark.asyncio
async def test_literal_recovery_fails_closed_outside_exact_contract(
    settings, storage, monkeypatch, caplog, user_message, source_body
) -> None:
    assert (
        _direct_complete_source_file_line_count(user_message) is None
        or _direct_complete_source_file_body(
            user_message,
            source_body,
        )
        is None
    )
    llm = _BoundaryLLM(UNSUPPORTED_WITHOUT_FILENAME)
    with caplog.at_level(logging.WARNING, logger="friday.agent_runtime"):
        result = await _named_version_runtime_turn(
            settings,
            storage,
            monkeypatch,
            llm,
            request=user_message,
            source_body=source_body,
        )
    assert result.get("files") in ([], None)
    assert RECOVERY_LOG not in "\n".join(record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_source_revocation_after_draft_prevents_literal_recovery(
    settings, storage, monkeypatch
) -> None:
    storage.ensure_user("alice", source="test", preset_key="owner")
    source = _pending_file(
        storage,
        "alice",
        "alice",
        SOURCE_BODY,
        filename=SOURCE_NAME,
        extra_metadata={"stored_path": "alice/" + SOURCE_NAME, "mime_type": "text/plain"},
    )

    class _RevokingLLM(_BoundaryLLM):
        mutated = False

        async def chat(self, messages: list[dict], **kwargs):  # noqa: ANN001
            result = await super().chat(messages, **kwargs)
            if not self.mutated and SOURCE_BODY in _messages_blob(messages):
                storage.execute(
                    "UPDATE raw_objects SET deleted_at='2026-09-13T00:00:00Z' WHERE id=?",
                    (source.id,),
                )
                storage.commit()
                self.mutated = True
            return result

    llm = _RevokingLLM(UNSUPPORTED_WITHOUT_FILENAME)
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=llm)
    runtime.kernel.bind_services(storage, None, None, None)

    async def prepare(user_id, _message, conversation_id, **_kwargs):
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=user_id)

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    result = await runtime.chat(
        "alice",
        NAMED_VERSION_REQUEST,
        actor=ACTOR,
        attachments=[_current_attachment(storage, source)],
        enable_tools=True,
    )
    assert llm.mutated is True
    assert result["files"] == []
    assert result["attachment_authority_changed_before_publication"] is True


@pytest.mark.asyncio
async def test_renderer_authorization_denial_has_no_carrier_or_success_claim(
    settings, storage, monkeypatch
) -> None:
    storage.ensure_user("alice", source="test", preset_key="owner")
    AuthorizationService(storage).deny_permission("alice", "knowledge.read")
    llm = _BoundaryLLM(UNSUPPORTED_WITHOUT_FILENAME)

    result = await _named_version_runtime_turn(settings, storage, monkeypatch, llm)

    assert result["files"] == []
    assert result["tools_used"] == ["make_file"]
    assert not _assistant_text(result).startswith("Готово")
    audit = [row for row in storage.list_audit_log("alice") if str(row.get("target_id") or "") == "make_file"]
    assert len(audit) == 1
    receipt = json.loads(str(audit[0]["after_json"]))
    assert receipt["success"] is False
    assert receipt["reason"] == "authorization_denied"


@pytest.mark.asyncio
async def test_cancelled_draft_cannot_reach_literal_recovery(settings, storage, monkeypatch) -> None:
    storage.ensure_user("alice", source="test", preset_key="owner")
    source = _pending_file(
        storage,
        "alice",
        "alice",
        SOURCE_BODY,
        filename=SOURCE_NAME,
        extra_metadata={"stored_path": "alice/" + SOURCE_NAME, "mime_type": "text/plain"},
    )
    started = asyncio.Event()

    class _BlockingLLM:
        enabled = True
        model = "word092-cancel-boundary"
        total_budget_sec = 5.0

        async def chat(self, _messages, **_kwargs):
            started.set()
            await asyncio.Event().wait()

    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_BlockingLLM())
    runtime.kernel.bind_services(storage, None, None, None)
    executed: list[str] = []
    original_execute = runtime.kernel.execute

    async def record_execute(name, *args, **kwargs):  # noqa: ANN001
        executed.append(str(name))
        return await original_execute(name, *args, **kwargs)

    async def prepare(user_id, _message, conversation_id, **_kwargs):
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=user_id)

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime.kernel, "execute", record_execute)
    task = asyncio.create_task(
        runtime.chat(
            "alice",
            NAMED_VERSION_REQUEST,
            actor=ACTOR,
            attachments=[_current_attachment(storage, source)],
            enable_tools=True,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "make_file" not in executed
