"""G45: natural PDF refusals and production-path scan gates without prepare_context mocks.

Every body and answer here is synthetic.  No provider, network or vision call.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

import friday.agent_runtime as agent_runtime_module
from friday.agent_runtime import (
    AgentContext,
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
VISION_TEXT = "VISION-READ-BODY-773: synthetic visible scan text"

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


class _TransientVisionProbe:
    """Current vision result returned without changing the legacy Raw Object."""

    def __init__(self, *, succeeds: bool) -> None:
        self.succeeds = succeeds
        self.calls: list[dict[str, Any]] = []

    async def inspect_file_transient(
        self,
        file_content: bytes,
        *,
        filename: str = "",
        mime_type: str = "",
        preview_chars: int = 24_000,
        preferred_language: str = "",
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "file_content": file_content,
                "filename": filename,
                "mime_type": mime_type,
                "preview_chars": preview_chars,
                "preferred_language": preferred_language,
            }
        )
        text = VISION_TEXT if self.succeeds else ""
        return {
            "filename": filename,
            "mime_type": mime_type,
            "transient": True,
            "persisted": False,
            "extraction_success": bool(text),
            "extraction_error": "" if text else "vision_request_failed:RuntimeError",
            "text_preview": text,
            "_runtime_source_text": text,
            "_runtime_source_truncated": False,
            "text_truncated": False,
            "parse_deadline_reached": False,
            "parse_pages_read": 1 if text else 0,
            "parse_pages_truncated": False,
            "parse_total_pages": 1,
            "vision_pages_read": 1 if text else 0,
            "vision_pages_total": 1,
            "vision_used": bool(text),
            "advisory_only": bool(text),
            "verification_eligible": False,
            "unsupported_format": False,
        }


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


def _store_stale_visual_file(
    settings: Any,
    storage: Any,
    *,
    mime_type: str,
) -> tuple[RawObject, bytes]:
    """Persist bytes with the pre-vision receipt seen on old uploads/replays."""

    storage.ensure_user(OWNER, preset_key="owner")
    suffix = ".jpg" if mime_type == "image/jpeg" else ".pdf"
    filename = f"stale-visual{suffix}"
    body = f"synthetic-stale-visual-bytes:{mime_type}".encode()
    digest = hashlib.sha256(body).hexdigest()
    relative = f"{OWNER}/{digest[:2]}/{digest}.bin"
    target = settings.files_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    carrier = "photo" if mime_type.startswith("image/") else "document"
    raw = RawObject(
        id=new_id("raw"),
        user_id=OWNER,
        source="g45-pre-vision-upload",
        source_ref=f"g45-pre-vision:{filename}",
        raw_content=f"[{carrier}: {filename}; type={mime_type}; size={len(body)}]",
        content_type="file",
        content_hash=digest,
        metadata_json={
            "filename": filename,
            "stored_path": relative,
            "mime_type": mime_type,
            "size_bytes": len(body),
            "sha256": digest,
            "uploaded_by": OWNER,
            "extraction_receipt_version": 1,
            "extraction_success": mime_type == "application/pdf",
            "text_extraction_success": False,
            "extraction_chars": 0,
            "vision_review_required": False,
            "vision_used": False,
            "verification_eligible": False,
            "parse_pages_read": 0,
            "parse_total_pages": 1 if mime_type == "application/pdf" else 0,
        },
    )
    storage.store_raw_object(raw)
    return raw, body


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


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", ["image/jpeg", "application/pdf"])
@pytest.mark.parametrize("route", ["current", "restored", "replay"])
async def test_old_visual_raw_is_read_again_with_current_vision_for_every_attachment_route(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    mime_type: str,
    route: str,
) -> None:
    """Dedup/replay may reuse Raw provenance, but not a stale no-vision verdict."""

    raw, original_bytes = _store_stale_visual_file(
        settings,
        storage,
        mime_type=mime_type,
    )
    auth = AuthorizationService(storage)
    model = _HostileScanModel()
    kernel = ExecutionKernel(auth, settings)
    vision = _TransientVisionProbe(succeeds=True)
    kernel.ingestion = vision
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=model,
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)

    synthesis_calls: list[list[dict[str, Any]]] = []

    async def synthesize(
        context: AgentContext,
        message: str,
        attachments: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        del context, message
        projected = list(attachments or [])
        synthesis_calls.append(projected)
        assert len(projected) == 1
        assert VISION_TEXT in str(projected[0].get("transient_text") or "")
        assert projected[0].get("advisory_only") is True
        assert projected[0].get("verification_eligible") is False
        return {
            "content": f"На скане распознано: {VISION_TEXT}",
            "tools_used": [],
            "_model_generated": True,
        }

    monkeypatch.setattr(runtime, "_generate_response", synthesize)
    filename = str(raw.metadata_json["filename"])
    question = f"Что указано в {filename}?"
    conversation_id: str | None = None
    replay_source_message_id: str | None = None
    attachments: list[dict[str, Any]] = []
    if route == "current":
        attachments = [{"raw_object_id": raw.id, "filename": filename}]
    else:
        conversation = storage.create_conversation(OWNER, title=f"g45 {route} stale scan")
        conversation_id = str(conversation["id"])
        source = storage.store_message(
            conversation_id,
            OWNER,
            "user",
            question if route == "replay" else "прикрепляю старый синтетический скан",
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
            metadata={"attachment_context_used": True},
        )
        if route == "replay":
            replay_source_message_id = str(source["id"])

    result = await runtime.chat(
        OWNER,
        question,
        actor=auth.actor_for_user(OWNER, source="test"),
        conversation_id=conversation_id,
        attachments=attachments,
        replay_source_message_id=replay_source_message_id,
        enable_tools=False,
    )

    assert len(vision.calls) == 1
    assert vision.calls[0]["file_content"] == original_bytes
    assert vision.calls[0]["filename"] == filename
    assert vision.calls[0]["mime_type"] == mime_type
    assert len(synthesis_calls) == 1
    assert VISION_TEXT in result["message"]
    # Vision text is useful for synthesis, but remains advisory rather than
    # silently becoming authenticated source evidence.
    assert result["attachment_context_readable_count"] == 1
    assert result["attachment_context_available"] is True
    assert result["attachment_verification_complete"] is False
    assert result["verified"] is False
    persisted = storage.get_raw_object(raw.id, OWNER)
    assert persisted is not None
    assert VISION_TEXT not in str(persisted["raw_content"])


@pytest.mark.asyncio
async def test_transient_visual_recovery_cache_never_crosses_a_chat_turn(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw, original_bytes = _store_stale_visual_file(
        settings,
        storage,
        mime_type="image/jpeg",
    )
    auth = AuthorizationService(storage)
    kernel = ExecutionKernel(auth, settings)
    vision = _TransientVisionProbe(succeeds=True)
    kernel.ingestion = vision
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_HostileScanModel(),
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)

    async def synthesize(
        context: AgentContext,
        message: str,
        attachments: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        del context, message
        assert VISION_TEXT in str((attachments or [{}])[0].get("transient_text") or "")
        return {"content": VISION_TEXT, "tools_used": [], "_model_generated": True}

    monkeypatch.setattr(runtime, "_generate_response", synthesize)
    filename = str(raw.metadata_json["filename"])
    for _ in range(2):
        result = await runtime.chat(
            OWNER,
            f"Что указано в {filename}?",
            actor=auth.actor_for_user(OWNER, source="test"),
            attachments=[{"raw_object_id": raw.id, "filename": filename}],
            enable_tools=False,
        )
        assert VISION_TEXT in result["message"]

    # Each turn performs one current parser read; only the redundant projected
    # reauth inside that same turn may reuse it.
    assert [call["file_content"] for call in vision.calls] == [original_bytes, original_bytes]


@pytest.mark.asyncio
async def test_transient_visual_recovery_cache_misses_after_exact_source_change(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw, original_bytes = _store_stale_visual_file(
        settings,
        storage,
        mime_type="image/jpeg",
    )
    replacement_bytes = b"synthetic-stale-visual-bytes:image/jpeg:replacement"
    replacement_sha256 = hashlib.sha256(replacement_bytes).hexdigest()
    replacement_relative = f"{OWNER}/{replacement_sha256[:2]}/{replacement_sha256}.bin"
    replacement_target = settings.files_dir / replacement_relative
    replacement_target.parent.mkdir(parents=True, exist_ok=True)
    replacement_target.write_bytes(replacement_bytes)

    auth = AuthorizationService(storage)
    kernel = ExecutionKernel(auth, settings)
    vision = _TransientVisionProbe(succeeds=True)
    kernel.ingestion = vision
    runtime = AgentRuntime(
        replace(settings, verify_answers=False),
        storage,
        llm=_HostileScanModel(),
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)

    async def synthesize(
        context: AgentContext,
        message: str,
        attachments: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        del context, message
        assert VISION_TEXT in str((attachments or [{}])[0].get("transient_text") or "")
        return {"content": VISION_TEXT, "tools_used": [], "_model_generated": True}

    monkeypatch.setattr(runtime, "_generate_response", synthesize)
    canonical_project = agent_runtime_module._projected_attachment_from_source
    source_changed = False

    def project_then_replace_registered_source(source: Any, fields: Any) -> Any:
        nonlocal source_changed
        projected = canonical_project(source, fields)
        if not source_changed and source.get("_runtime_file_reparsed") is True:
            metadata = dict(raw.metadata_json)
            metadata.update(
                {
                    "stored_path": replacement_relative,
                    "sha256": replacement_sha256,
                    "size_bytes": len(replacement_bytes),
                }
            )
            with storage.transaction() as connection:
                cursor = connection.execute(
                    "UPDATE raw_objects SET content_hash=?, metadata_json=? WHERE id=?",
                    (
                        replacement_sha256,
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        raw.id,
                    ),
                )
                assert cursor.rowcount == 1
            source_changed = True
        return projected

    monkeypatch.setattr(
        agent_runtime_module,
        "_projected_attachment_from_source",
        project_then_replace_registered_source,
    )
    filename = str(raw.metadata_json["filename"])
    result = await runtime.chat(
        OWNER,
        f"Что указано в {filename}?",
        actor=auth.actor_for_user(OWNER, source="test"),
        attachments=[{"raw_object_id": raw.id, "filename": filename}],
        enable_tools=False,
    )

    assert source_changed is True
    assert [call["file_content"] for call in vision.calls] == [original_bytes, replacement_bytes]
    assert VISION_TEXT not in result["message"]
    assert result["attachment_authority_changed_before_publication"] is True
    assert result["verification"]["issues"] == ["attachment_authority_changed_before_publication"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mime_type", ["image/jpeg", "application/pdf"])
async def test_failed_on_demand_vision_keeps_the_code_owned_no_guess_answer(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    mime_type: str,
) -> None:
    raw, original_bytes = _store_stale_visual_file(
        settings,
        storage,
        mime_type=mime_type,
    )
    auth = AuthorizationService(storage)
    model = _HostileScanModel()
    kernel = ExecutionKernel(auth, settings)
    vision = _TransientVisionProbe(succeeds=False)
    kernel.ingestion = vision
    runtime = AgentRuntime(
        replace(settings, verify_answers=True, verify_min_answer_chars=1),
        storage,
        llm=model,
        kernel=kernel,
    )
    monkeypatch.setattr(runtime, "_prepare_context", _simple_context)

    async def should_not_generate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AssertionError("failed vision reached answer synthesis")

    monkeypatch.setattr(runtime, "_generate_response", should_not_generate)
    result = await runtime.chat(
        OWNER,
        f"Что указано в {raw.metadata_json['filename']}?",
        actor=auth.actor_for_user(OWNER, source="test"),
        attachments=[{"raw_object_id": raw.id, "filename": raw.metadata_json["filename"]}],
        enable_tools=False,
    )

    assert len(vision.calls) == 1
    assert vision.calls[0]["file_content"] == original_bytes
    answer = result["message"].casefold()
    assert model.calls == 0
    assert "не буду угадывать" in answer or "не буду выдумывать" in answer
    assert HALLUC not in result["message"]
    assert result["attachment_context_readable_count"] == 0
    assert result["attachment_context_available"] is False
    assert result["verification_status"] == "unknown"
    assert result["verified"] is False
