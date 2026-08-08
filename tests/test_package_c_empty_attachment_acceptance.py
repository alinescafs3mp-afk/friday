"""K07 acceptance: a successfully parsed empty file is not a broken file.

The distinction must survive current-turn projection, idempotent replay and
conversation restoration.  The runtime then owns a direct human verdict; it
must not ask a model which may mistake absence of file text for an empty archive.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.agent_runtime import AgentContext, AgentRuntime, _what_is_missing_from_this_attachment
from friday.documents import DocumentResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext
from friday.server import _current_turn_file_attachment, create_app
from friday.storage.models import RawObject, new_id
from friday.telegram_bridge._callbacks import _file_fate_line

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "package_c_document_holdout.json"


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _empty_current_attachment(case: dict[str, Any]) -> dict[str, Any]:
    replay = case["source"] == "idempotent_replay"
    ingestion: dict[str, Any] = {
        "raw_object_id": f"raw_{case['id']}",
        **({"idempotent_replay": True} if replay else {}),
    }
    if not replay:
        ingestion["extraction"] = {
            "success": True,
            "text_success": True,
            "chars": 0,
        }
    return _current_turn_file_attachment(
        filename=case["filename"],
        file_ingestion=ingestion,
        raw={
            "raw_content": "",
            "metadata_json": {
                "filename": case["filename"],
                "uploaded_by": "synthetic-user",
                "extraction_success": True,
                "text_extraction_success": True,
            },
        },
    )


def _stored_empty_attachment(case: dict[str, Any], settings, storage) -> dict[str, Any]:  # noqa: ANN001
    storage.ensure_user("synthetic-user", preset_key="owner")
    raw = RawObject(
        id=new_id("raw"),
        user_id="synthetic-user",
        source="synthetic-package-c",
        source_ref=f"synthetic:{case['id']}",
        raw_content="",
        content_type="file",
        metadata_json={
            "filename": case["filename"],
            "uploaded_by": "synthetic-user",
            "extraction_success": True,
            "text_extraction_success": True,
        },
    )
    storage.store_raw_object(raw)
    runtime = AgentRuntime(settings, storage)
    attachment = runtime._owned_file_attachment(  # noqa: SLF001
        raw.id,
        tenant_id="synthetic-user",
        person_id="synthetic-user",
    )
    assert attachment is not None
    return attachment


def _assert_successful_empty(attachment: dict[str, Any]) -> None:
    assert attachment["transient_text"] == ""
    assert attachment["extraction_success"] is True
    assert attachment["empty_text"] is True
    assert attachment["extraction_error"] == ""


@pytest.mark.parametrize(
    "case",
    _fixture()["k07_empty_cases"][:4],
    ids=lambda case: case["id"],
)
def test_k07_current_and_replayed_empty_extraction_keep_an_explicit_success_state(
    case: dict[str, Any],
) -> None:
    _assert_successful_empty(_empty_current_attachment(case))


def test_k07_restored_empty_extraction_keeps_the_same_explicit_success_state(settings, storage) -> None:
    case = _fixture()["k07_empty_cases"][4]
    _assert_successful_empty(_stored_empty_attachment(case, settings, storage))


def _control_attachment(case: dict[str, Any]) -> dict[str, Any]:
    kind = case["control_type"]
    extraction: dict[str, Any] = {"success": False, "text_success": False, "chars": 0}
    metadata: dict[str, Any] = {
        "filename": case["filename"],
        "uploaded_by": "synthetic-user",
        "extraction_success": False,
        "text_extraction_success": False,
    }
    raw_content = ""
    if kind == "unreadable":
        raw_content = "[File: synthetic-unreadable.bin]"
    elif kind == "parse_deadline":
        extraction["parse_deadline_reached"] = True
        metadata["parse_deadline_reached"] = True
    elif kind == "pages_truncated":
        extraction.update(parse_pages_read=0, parse_pages_truncated=True, parse_total_pages=3)
        metadata.update(parse_pages_read=0, parse_pages_truncated=True, parse_total_pages=3)
    elif kind == "advisory_ocr":
        extraction.update(success=True, vision=True)
        metadata.update(extraction_success=True, vision_review_required=True)
    elif kind == "nonempty":
        raw_content = "SYNTHETIC-NONEMPTY-TEXT"
        extraction.update(success=True, text_success=True, chars=len(raw_content))
        metadata.update(extraction_success=True, text_extraction_success=True)
    else:  # pragma: no cover - fixture control enum is frozen
        raise AssertionError(kind)
    return _current_turn_file_attachment(
        filename=case["filename"],
        file_ingestion={
            "raw_object_id": f"raw_{case['id']}",
            "extraction": extraction,
        },
        raw={"raw_content": raw_content, "metadata_json": metadata},
    )


@pytest.mark.parametrize("case", _fixture()["k07_controls"], ids=lambda case: case["id"])
def test_k07_failure_truncation_advisory_and_nonempty_states_are_not_called_empty(
    case: dict[str, Any],
) -> None:
    attachment = _control_attachment(case)

    assert attachment.get("empty_text") is not True
    if case["control_type"] == "nonempty":
        assert attachment["extraction_success"] is True
        assert attachment["transient_text"] == "SYNTHETIC-NONEMPTY-TEXT"
    elif case["control_type"] == "parse_deadline":
        assert attachment["parse_deadline_reached"] is True
    elif case["control_type"] == "pages_truncated":
        assert attachment["parse_pages_truncated"] is True
    elif case["control_type"] == "advisory_ocr":
        assert attachment["advisory_only"] is True
    else:
        assert attachment["extraction_success"] is False


class _NeverCalledLLM:
    enabled = True
    model = "synthetic-package-c-empty-never-called"
    total_budget_sec = 5.0

    async def chat(self, messages, **kwargs):  # pragma: no cover - empty verdict is code-owned
        del messages, kwargs
        raise AssertionError("an empty file reached the model")


def _runtime(settings, storage, monkeypatch) -> AgentRuntime:  # noqa: ANN001
    storage.ensure_user("synthetic-user", preset_key="owner")
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, llm=_NeverCalledLLM())

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(
            conversation_id=conversation_id,
            user_id=user_id,
            person_id=user_id,
            answer_mode="general_conversation",
        )

    async def forbidden_generate(*args, **kwargs):  # noqa: ANN001
        del args, kwargs
        raise AssertionError("a successfully empty file reached synthesis")

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", forbidden_generate)
    return runtime


@pytest.mark.parametrize("case", _fixture()["k07_empty_cases"], ids=lambda case: case["id"])
@pytest.mark.asyncio
async def test_k07_runtime_answers_a_successfully_empty_file_without_model_or_archive_language(
    case: dict[str, Any],
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime = _runtime(settings, storage, monkeypatch)
    attachment = (
        _stored_empty_attachment(case, settings, storage)
        if case["source"] == "restored_conversation"
        else _empty_current_attachment(case)
    )
    _assert_successful_empty(attachment)

    reply = await runtime.chat(
        "synthetic-user",
        case["question"],
        actor=ActorContext(user_id="synthetic-user", preset_key="owner", source="test"),
        attachments=[attachment],
        enable_tools=False,
    )

    expected = _fixture()["acceptance"]["required_empty_notice"]
    assert expected in reply["message"]
    assert "баз" not in reply["message"].casefold()
    assert "архив" not in reply["message"].casefold()
    stored = storage.get_message(str(reply["message_id"]), "synthetic-user")
    assert stored is not None and stored["content"] == reply["message"]


def _allow_bounded_model_fallback(
    runtime: AgentRuntime,
    monkeypatch,
    *,
    answer: str,
) -> None:  # noqa: ANN001
    async def generate(context, message, projected):  # noqa: ANN001
        del context, message, projected
        return {"content": answer, "tools_used": []}

    monkeypatch.setattr(runtime, "_generate_response", generate)


@pytest.mark.asyncio
async def test_k07_four_supplied_files_cannot_be_declared_empty_from_the_first_three(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime = _runtime(settings, storage, monkeypatch)
    _allow_bounded_model_fallback(
        runtime,
        monkeypatch,
        answer="SYNTHETIC-BOUNDED-ATTACHMENT-FALLBACK",
    )
    empty_case = _fixture()["k07_empty_cases"][0]
    empty_attachments = []
    for index in range(3):
        attachment = dict(_empty_current_attachment(empty_case))
        attachment["filename"] = f"synthetic-empty-{index}.txt"
        attachment["raw_object_id"] = f"raw_synthetic_empty_{index}"
        empty_attachments.append(attachment)
    nonempty_case = next(case for case in _fixture()["k07_controls"] if case["control_type"] == "nonempty")

    reply = await runtime.chat(
        "synthetic-user",
        "Что находится во всех четырёх приложенных файлах?",
        actor=ActorContext(user_id="synthetic-user", preset_key="owner", source="test"),
        attachments=[*empty_attachments, _control_attachment(nonempty_case)],
        enable_tools=False,
    )

    assert reply["message"] == "SYNTHETIC-BOUNDED-ATTACHMENT-FALLBACK"
    assert "Текста в файле не оказалось" not in reply["message"]


@pytest.mark.parametrize("signal", ["archive_truncated", "source_truncated_for_parse"])
@pytest.mark.asyncio
async def test_k07_restored_unread_tail_never_becomes_a_proven_empty_file(
    signal: str,
    settings,
    storage,
    monkeypatch,
) -> None:
    storage.ensure_user("synthetic-user", preset_key="owner")
    raw = RawObject(
        id=new_id("raw"),
        user_id="synthetic-user",
        source="synthetic-package-c-unread-tail",
        source_ref=f"synthetic:restored:{signal}",
        raw_content="",
        content_type="file",
        metadata_json={
            "filename": f"synthetic-{signal}.bin",
            "uploaded_by": "synthetic-user",
            "extraction_success": True,
            "text_extraction_success": True,
            signal: True,
        },
    )
    storage.store_raw_object(raw)
    runtime = _runtime(settings, storage, monkeypatch)
    attachment = runtime._owned_file_attachment(  # noqa: SLF001
        raw.id,
        tenant_id="synthetic-user",
        person_id="synthetic-user",
    )
    assert attachment is not None
    assert attachment[signal] is True
    assert attachment.get("empty_text") is not True
    _allow_bounded_model_fallback(
        runtime,
        monkeypatch,
        answer=f"SYNTHETIC-INCOMPLETE-{signal}",
    )

    reply = await runtime.chat(
        "synthetic-user",
        "Этот файл действительно пуст?",
        actor=ActorContext(user_id="synthetic-user", preset_key="owner", source="test"),
        attachments=[attachment],
        enable_tools=False,
    )

    assert reply["message"] == f"SYNTHETIC-INCOMPLETE-{signal}"
    assert "Текста в файле не оказалось" not in reply["message"]


def test_k07_mutation_empty_cannot_be_inferred_from_missing_text_alone() -> None:
    unreadable = _control_attachment(_fixture()["k07_controls"][0])
    assert str(unreadable.get("transient_text") or "") == ""
    assert unreadable["extraction_success"] is False
    assert unreadable.get("empty_text") is not True


@pytest.mark.parametrize(
    "signal",
    ["text_truncated", "archive_truncated", "source_truncated_for_parse"],
)
def test_k07_any_unread_tail_signal_forbids_a_successful_empty_verdict(signal: str) -> None:
    """A blank visible prefix cannot prove that the whole file has no text."""

    attachment = _current_turn_file_attachment(
        filename=f"synthetic-{signal}.txt",
        file_ingestion={
            "raw_object_id": f"raw_synthetic_{signal}",
            "extraction": {
                "success": True,
                "text_success": True,
                "chars": 0,
                signal: True,
            },
        },
        raw={
            "raw_content": "",
            "metadata_json": {
                "extraction_success": True,
                "text_extraction_success": True,
                signal: True,
            },
        },
    )

    assert attachment.get("empty_text") is not True
    assert attachment.get(signal) is True


@pytest.mark.parametrize(
    ("signal", "expected_note"),
    [
        ("archive_truncated", "архив разобран не целиком"),
        (
            "source_truncated_for_parse",
            "исходный файл перед разбором был доступен не целиком",
        ),
    ],
)
def test_k07_advisory_text_keeps_the_independent_unread_tail_warning(
    signal: str,
    expected_note: str,
) -> None:
    note = _what_is_missing_from_this_attachment(
        {
            "advisory_only": True,
            "extraction_success": True,
            signal: True,
        }
    )

    assert "текст получен распознаванием" in note
    assert expected_note in note


@pytest.mark.parametrize(
    ("parser_signal", "attachment_signal"),
    [
        ("archive_budget_exhausted", "archive_truncated"),
        ("source_truncated_for_parse", "source_truncated_for_parse"),
    ],
)
@pytest.mark.asyncio
async def test_k07_unread_tail_signals_survive_an_idempotent_file_replay(
    parser_signal: str,
    attachment_signal: str,
    settings,
    storage,
) -> None:
    """Skipping the parser on replay must not turn an unread tail into proof of emptiness."""

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    pipeline._doc_extractor.extract = lambda *args, **kwargs: DocumentResult(  # noqa: SLF001
        "",
        {"format": "synthetic", parser_signal: True},
        True,
        "",
    )
    file_bytes = f"SYNTHETIC-{parser_signal}".encode()
    source_ref = f"synthetic-k07-replay:{parser_signal}"

    first = await pipeline.ingest_file(
        "synthetic-user",
        None,
        file_bytes,
        filename=f"synthetic-{parser_signal}.bin",
        mime_type="application/octet-stream",
        metadata={attachment_signal: False},
        source_ref=source_ref,
    )
    first_raw = storage.get_raw_object(first["raw_object_id"], "synthetic-user")
    first_attachment = _current_turn_file_attachment(
        filename=f"synthetic-{parser_signal}.bin",
        file_ingestion=first,
        raw=first_raw,
    )
    assert first_attachment.get(attachment_signal) is True
    assert first_attachment.get("empty_text") is not True

    replay = await pipeline.ingest_file(
        "synthetic-user",
        None,
        file_bytes,
        filename=f"synthetic-{parser_signal}.bin",
        mime_type="application/octet-stream",
        metadata={attachment_signal: False},
        source_ref=source_ref,
    )
    assert replay.get("idempotent_replay") is True
    replay_extraction = replay.get("extraction")
    assert isinstance(replay_extraction, dict)
    assert replay_extraction.get(attachment_signal) is True
    replay_raw = storage.get_raw_object(replay["raw_object_id"], "synthetic-user")
    replay_attachment = _current_turn_file_attachment(
        filename=f"synthetic-{parser_signal}.bin",
        file_ingestion=replay,
        raw=replay_raw,
    )

    assert replay_attachment.get(attachment_signal) is True
    assert replay_attachment.get("empty_text") is not True


@pytest.mark.asyncio
async def test_k07_the_whole_parser_receipt_outranks_caller_metadata_and_survives_replay(
    settings,
    storage,
) -> None:
    """Every warning shown on first upload must remain true on an exact retry."""

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    pipeline._doc_extractor.extract = lambda *args, **kwargs: DocumentResult(  # noqa: SLF001
        "SYNTHETIC-PARTIAL-PREFIX",
        {
            "format": "synthetic",
            "text_truncated": True,
            "parse_deadline_reached": True,
            "pages_read": 7,
            "pages_truncated": True,
            "total_pages": 19,
            "archive_budget_exhausted": True,
            "files": 5,
            "previewed_files": 2,
            "source_truncated_for_parse": True,
        },
        True,
        "",
    )
    file_bytes = b"SYNTHETIC-PARSER-RECEIPT"
    source_ref = "synthetic-k07-complete-replay-receipt"
    hostile_metadata = {
        "filename": "caller-overwrite.bin",
        "sha256": "0" * 64,
        "stored_path": "caller/path",
        "extraction_success": False,
        "text_extraction_success": False,
        "text_truncated": False,
        "parse_deadline_reached": False,
        "parse_pages_read": 0,
        "parse_pages_truncated": False,
        "parse_total_pages": 0,
        "archive_truncated": False,
        "archive_files": 0,
        "archive_files_read": 0,
        "source_truncated_for_parse": False,
        "extraction_receipt_version": 99,
    }

    first = await pipeline.ingest_file(
        "synthetic-user",
        None,
        file_bytes,
        filename="synthetic-partial.txt",
        mime_type="text/plain",
        metadata=hostile_metadata,
        source_ref=source_ref,
        force_review=True,
    )
    raw = storage.get_raw_object(first["raw_object_id"], "synthetic-user")
    assert raw is not None
    metadata = json.loads(str(raw["metadata_json"]))
    assert metadata["filename"] == "synthetic-partial.txt"
    assert metadata["sha256"] != hostile_metadata["sha256"]
    assert metadata["stored_path"] != hostile_metadata["stored_path"]
    assert metadata["extraction_receipt_version"] == 1
    assert metadata["extraction_success"] is True
    assert metadata["text_extraction_success"] is True
    assert metadata["text_truncated"] is True
    assert metadata["parse_deadline_reached"] is True
    assert metadata["parse_pages_read"] == 7
    assert metadata["parse_pages_truncated"] is True
    assert metadata["parse_total_pages"] == 19
    assert metadata["archive_truncated"] is True
    assert metadata["archive_files"] == 5
    assert metadata["archive_files_read"] == 2
    assert metadata["source_truncated_for_parse"] is True

    replay = await pipeline.ingest_file(
        "synthetic-user",
        None,
        file_bytes,
        filename="synthetic-partial.txt",
        mime_type="text/plain",
        metadata=hostile_metadata,
        source_ref=source_ref,
        force_review=True,
    )
    assert replay.get("idempotent_replay") is True
    extraction = replay.get("extraction")
    assert isinstance(extraction, dict)
    expected = {
        "success": True,
        "text_success": True,
        "chars": len("SYNTHETIC-PARTIAL-PREFIX"),
        "text_truncated": True,
        "parse_deadline_reached": True,
        "parse_pages_read": 7,
        "parse_pages_truncated": True,
        "parse_total_pages": 19,
        "vision_pages_total": 0,
        "vision_pages_read": 0,
        "archive_truncated": True,
        "archive_files": 5,
        "archive_files_read": 2,
        "source_truncated_for_parse": True,
        "unsupported_format": False,
    }
    assert extraction == expected
    assert _file_fate_line(replay) == _file_fate_line(first)


def test_k07_transient_no_save_empty_file_keeps_the_same_explicit_empty_state(settings) -> None:
    """The privacy-preserving no-save route must not fall out of the K07 contract."""

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    captured: dict[str, Any] = {}

    with TestClient(app) as client:

        async def capture_chat(*args, **kwargs):  # noqa: ANN002, ANN003
            del args
            captured["attachments"] = kwargs.get("attachments")
            return {
                "conversation_id": "synthetic-transient-empty",
                "content": "SYNTHETIC-TRANSIENT-ANSWER",
            }

        app.state.agent.chat = capture_chat
        response = client.post(
            "/api/chat",
            headers=headers,
            json={
                "message": "Не сохраняй этот пустой файл, только скажи, есть ли в нём текст.",
                "source_ref": "synthetic-transient-empty:1",
                "document": {
                    "filename": "synthetic-empty-private.txt",
                    "mime_type": "text/plain",
                    "content_base64": base64.b64encode(b"").decode("ascii"),
                },
            },
        )

    assert response.status_code == 200, response.text
    attachments = captured.get("attachments")
    assert isinstance(attachments, list) and len(attachments) == 1
    assert attachments[0]["transient"] is True
    assert attachments[0]["persisted"] is False
    assert attachments[0]["transient_text"] == ""
    assert attachments[0]["extraction_success"] is True
    assert attachments[0]["empty_text"] is True
