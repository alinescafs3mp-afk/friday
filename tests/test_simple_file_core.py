from __future__ import annotations

import io
import zipfile
from dataclasses import replace

import pytest

from friday.agent_runtime import (
    AgentRuntime,
    _closed_attachment_read_only_request,
    _is_document_metadata_request,
)
from friday.execution_kernel import ToolResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext
from friday.storage.models import InboxStatus, KnowledgeObject, new_id


class _NoDirectLLM:
    enabled = True
    model = "simple-file-core"

    async def chat(self, messages, **kwargs):  # pragma: no cover - closed routes own these calls
        del messages, kwargs
        raise AssertionError("unexpected direct model call")


def _odt_with_metadata_and_records() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr(
            "meta.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <office:meta><dc:title>FILE-CORE-ODT-TITLE</dc:title>
 <dc:creator>FILE-CORE-ODT-CREATOR</dc:creator></office:meta>
</office:document-meta>""",
        )
        archive.writestr(
            "content.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text>
  <text:p>1. FILE-CORE-ODT-FIRST</text:p>
  <text:p>2. FILE-CORE-ODT-LAST</text:p>
  <text:p>Подпись: FILE-CORE-ODT-FOOTER</text:p>
 </office:text></office:body>
</office:document-content>""",
        )
    return payload.getvalue()


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


@pytest.mark.asyncio
async def test_registered_upload_receipt_and_two_file_read_use_only_selected_disk_sources(
    settings,
    storage,
    monkeypatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    first_text = "Контрольное поле: FILE-CORE-AUG12-A\nПервый источник: северный маршрут."
    second_text = "Контрольное поле B: FILE-CORE-AUG12-B\nВторой источник: южный маршрут."
    first = await pipeline.ingest_file(
        "alice",
        None,
        first_text.encode(),
        filename="route-north-aug12.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:FILE-CORE-AUG12-A",
    )
    second = await pipeline.ingest_file(
        "alice",
        None,
        second_text.encode(),
        filename="route-south-aug12.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:FILE-CORE-AUG12-B",
    )
    first_id = str(first["raw_object_id"])
    second_id = str(second["raw_object_id"])
    runtime = AgentRuntime(configured, storage, llm=_NoDirectLLM())

    async def forbidden_prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("pure file contour entered general context preparation")

    async def forbidden_generate(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("bare upload receipt called the model")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_prepare)
    monkeypatch.setattr(runtime, "_generate_response", forbidden_generate)
    receipt = await runtime.chat(
        "alice",
        "Загружен документ: route-north-aug12.txt",
        actor=_actor(),
        attachments=[{"raw_object_id": first_id}],
        synthetic_document_notice=True,
    )
    assert "зарегистрирован" in receipt["message"]
    assert "байты на диске проверены" in receipt["message"]
    assert "содержимое извлечено полностью" in receipt["message"]
    assert "Быстрый обзор содержимого" in receipt["message"]
    assert f"› {first_text.splitlines()[0]}" in receipt["message"]
    assert f"› {first_text.splitlines()[1]}" in receipt["message"]
    # Literals are exact substrings of the registered Raw body.
    for line in first_text.splitlines():
        if f"› {line}" in receipt["message"]:
            assert line in first_text
    assert receipt["message_format"] == "plain"
    assert receipt["tools_used"] == []
    assert "…" not in receipt["message"]
    # Regenerate of synthetic intake must reproduce the same deterministic text.
    regenerated = await runtime.chat(
        "alice",
        "Загружен документ: route-north-aug12.txt",
        actor=_actor(),
        attachments=[{"raw_object_id": first_id}],
        synthetic_document_notice=True,
    )
    assert regenerated["message"] == receipt["message"]
    assert regenerated["message_format"] == "plain"
    assert regenerated["tools_used"] == []

    generation_calls: list[list[dict]] = []

    async def generate(context, message, attachments):  # noqa: ANN001
        del context
        snapshot = [dict(item) for item in attachments]
        generation_calls.append(snapshot)
        visible = "\n".join(str(item.get("transient_text") or "") for item in snapshot)
        if len(snapshot) == 1:
            assert first_text in visible
            assert second_text not in visible
            return {"content": "Северный маршрут подтверждён.", "tools_used": []}
        assert len(snapshot) == 2
        assert first_text in visible and second_text in visible
        return {"content": "Сравнение построено только по двум выбранным маршрутам.", "tools_used": []}

    monkeypatch.setattr(runtime, "_generate_response", generate)
    followup = await runtime.chat(
        "alice",
        "Что внутри этого файла?",
        actor=_actor(),
        conversation_id=receipt["conversation_id"],
        attachments=[],
    )
    assert followup["message"] == "Северный маршрут подтверждён."

    compared = await runtime.chat(
        "alice",
        "Сравни два приложенных файла. Сначала назови значение после «Контрольное поле» "
        "из ODT, затем значение после «Контрольное поле B» из TXT.",
        actor=_actor(),
        attachments=[{"raw_object_id": first_id}, {"raw_object_id": second_id}],
    )
    assert compared["message"] == "Сравнение построено только по двум выбранным маршрутам."
    assert compared["tools_used"] == []
    assert compared["attachment_context_expected_count"] == 2
    assert compared["attachment_context_readable_count"] == 2
    assert compared["attachment_query_status"] == "matched"
    assert compared["attachment_query_files_matched"] == 2
    assert [len(call) for call in generation_calls] == [1, 2]


@pytest.mark.asyncio
async def test_current_upload_and_exact_named_prior_use_registered_disk_sources_only(
    settings,
    storage,
    monkeypatch,
) -> None:
    request = (
        "Сравни этот файл с ранее загруженным файлом «codex-live-a-aug12.txt». "
        "Верни дословно значение после «Контрольный код A» и значение после "
        "«Контрольный код B»."
    )
    assert _closed_attachment_read_only_request(request) is True
    for unsafe_tail in (
        " И отправь файл.",
        " И сохрани результат в память.",
        " И верни файл в MCP outbox.",
    ):
        assert _closed_attachment_read_only_request(request + unsafe_tail) is False

    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    sources = (
        (
            "codex-live-a-aug12.txt",
            "Контрольный код A\nALPHA-REGISTERED-AUG12\nПервый источник зарегистрирован.",
            "telegram-file:CURRENT-PRIOR-A",
        ),
        (
            "codex-live-b-aug12.txt",
            "Контрольный код B\nBETA-REGISTERED-AUG12\nВторой источник зарегистрирован.",
            "telegram-file:CURRENT-PRIOR-B",
        ),
        (
            "ambient-decoy-aug12.txt",
            "DECOY-MUST-NOT-REACH-THE-MODEL",
            "telegram-file:CURRENT-PRIOR-DECOY",
        ),
    )
    ingested = []
    for filename, body, source_ref in sources:
        ingested.append(
            await pipeline.ingest_file(
                "alice",
                None,
                body.encode(),
                filename=filename,
                mime_type="text/plain",
                metadata={"uploaded_by": "alice"},
                source_ref=source_ref,
            )
        )
    first_id, second_id, decoy_id = (str(item["raw_object_id"]) for item in ingested)
    runtime = AgentRuntime(configured, storage, llm=_NoDirectLLM())

    first_receipt = await runtime.chat(
        "alice",
        "Загружен документ: codex-live-a-aug12.txt",
        actor=_actor(),
        attachments=[{"raw_object_id": first_id}],
        synthetic_document_notice=True,
    )
    await runtime.chat(
        "alice",
        "Загружен документ: ambient-decoy-aug12.txt",
        actor=_actor(),
        conversation_id=first_receipt["conversation_id"],
        attachments=[{"raw_object_id": decoy_id}],
        synthetic_document_notice=True,
    )

    seen: list[list[dict]] = []

    async def forbidden_prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("closed file read entered general context preparation")

    async def forbidden_agentic(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("closed file read entered the agentic loop")

    def forbidden_tools(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("closed file read built tool definitions")

    async def generate(context, message, attachments):  # noqa: ANN001
        del message
        assert context.focused_attachment_turn is True
        assert context.conversation_history == []
        assert context.ingestion == {}
        assert context.knowledge_hits == []
        snapshot = [dict(item) for item in attachments]
        seen.append(snapshot)
        visible = "\n".join(str(item.get("transient_text") or "") for item in snapshot)
        assert "ALPHA-REGISTERED-AUG12" in visible
        assert "BETA-REGISTERED-AUG12" in visible
        assert "DECOY-MUST-NOT-REACH-THE-MODEL" not in visible
        return {
            "content": "ALPHA-REGISTERED-AUG12\nBETA-REGISTERED-AUG12",
            "tools_used": [],
        }

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_prepare)
    monkeypatch.setattr(runtime, "_agentic_loop", forbidden_agentic)
    monkeypatch.setattr(runtime.kernel, "get_tool_definitions", forbidden_tools)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    result = await runtime.chat(
        "alice",
        request,
        actor=_actor(),
        conversation_id=first_receipt["conversation_id"],
        attachments=[{"raw_object_id": second_id}],
    )

    assert result["message"] == "ALPHA-REGISTERED-AUG12\nBETA-REGISTERED-AUG12"
    assert result["tools_used"] == []
    assert result["attachment_context_expected_count"] == 2
    assert result["attachment_context_readable_count"] == 2
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True
    assert len(seen) == 1
    assert [str(item.get("raw_object_id") or "") for item in seen[0]] == [
        first_id,
        second_id,
    ]


@pytest.mark.asyncio
async def test_emitted_citation_restores_registered_file_and_returns_exact_last_item_without_model(
    settings,
    storage,
    monkeypatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    source = (
        "Телефонный перечень\n"
        "1. Первый пункт — FILE-CORE-FIRST\n"
        "2. Последний пункт — FILE-CORE-LAST\n"
        "   продолжение последнего пункта\n"
        "Подпись: FILE-CORE-CITATION-FOOTER"
    )
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        source.encode(),
        filename="phones-aug12.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:FILE-CORE-CITATION-AUG12",
    )
    raw_id = str(ingested["raw_object_id"])
    knowledge = storage.store_knowledge_object(
        KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw_id,
            content=source,
            title="Телефонный перечень",
        )
    )
    conversation = storage.create_conversation("alice")
    storage.store_message(conversation["id"], "alice", "user", "Найди телефонный перечень")
    storage.store_message(
        conversation["id"],
        "alice",
        "assistant",
        "Нашла источник [K1].",
        metadata={"knowledge_citations": {"K1": knowledge.id}},
    )
    runtime = AgentRuntime(configured, storage, llm=_NoDirectLLM())

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("exact citation/last-item contour called a model seam")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    result = await runtime.chat(
        "alice",
        "Какой там последний пункт в нём?",
        actor=_actor(),
        conversation_id=conversation["id"],
        attachments=[],
    )

    assert "FILE-CORE-LAST" in result["message"]
    assert "продолжение последнего пункта" in result["message"]
    assert "FILE-CORE-FIRST" not in result["message"]
    assert "FILE-CORE-CITATION-FOOTER" not in result["message"]
    assert result["tools_used"] == []
    stored_user = next(
        item
        for item in reversed(storage.get_conversation_messages(conversation["id"], user_id="alice"))
        if item["role"] == "user"
    )
    assert raw_id in str(stored_user["metadata_json"])


@pytest.mark.asyncio
async def test_exact_named_historical_ignored_file_direct_read_last_item_without_model(
    settings,
    storage,
    monkeypatch,
) -> None:
    """Exact unique filename reopens one ignored historical file for direct-read only."""

    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    filename = "historical-ignored-direct-aug12.txt"
    source = (
        "Исторический перечень\n"
        "1. Первый пункт — HIST-IGNORED-FIRST\n"
        "2. Последний пункт — HIST-IGNORED-LAST\n"
        "   продолжение ignored last\n"
        "Подпись: HIST-IGNORED-FOOTER"
    )
    ambient_body = "AMBIENT-DECOY-MUST-NOT-REACH\n1. Ambient only"
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        source.encode(),
        filename=filename,
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:HIST-IGNORED-DIRECT-AUG12",
    )
    raw_id = str(ingested["raw_object_id"])
    ambient = await pipeline.ingest_file(
        "alice",
        None,
        ambient_body.encode(),
        filename="ambient-decoy-direct-aug12.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:HIST-IGNORED-AMBIENT-AUG12",
    )
    ambient_id = str(ambient["raw_object_id"])

    inbox = storage.get_inbox_by_raw(raw_id, "alice")
    assert inbox is not None
    assert storage.update_inbox_status(str(inbox["id"]), InboxStatus.IGNORED, reviewed_by="alice")

    # Ambient / fuzzy / content / ordinary exact lookup keep ignored invisible.
    assert storage.find_owned_files_by_filename("alice", "alice", filename) == []
    catalog_ids = {str(row["id"]) for row in storage.list_owned_file_catalog("alice", "alice")}
    assert raw_id not in catalog_ids
    assert ambient_id in catalog_ids
    content_page = storage.search_owned_file_content("alice", "alice", "HIST-IGNORED-LAST")
    assert content_page.get("available") is True
    assert all(str(row.get("id") or "") != raw_id for row in content_page.get("results") or [])
    searchable = storage.get_searchable_file_sources(
        "alice",
        [raw_id],
        uploaded_by="alice",
        limit=1,
        include_content=True,
    )
    assert searchable == []

    runtime = AgentRuntime(configured, storage, llm=_NoDirectLLM())

    async def forbidden_prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("ignored exact-name direct-read entered general context preparation")

    async def forbidden_agentic(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("ignored exact-name direct-read entered the agentic loop")

    async def forbidden_generate(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("ignored exact-name direct-read called the model")

    def forbidden_tools(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("ignored exact-name direct-read built tool definitions")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_prepare)
    monkeypatch.setattr(runtime, "_agentic_loop", forbidden_agentic)
    monkeypatch.setattr(runtime, "_generate_response", forbidden_generate)
    monkeypatch.setattr(runtime.kernel, "get_tool_definitions", forbidden_tools)

    green = await runtime.chat(
        "alice",
        f"Какой последний пункт в файле «{filename}»?",
        actor=_actor(),
        attachments=[],
    )
    assert "HIST-IGNORED-LAST" in green["message"]
    assert "продолжение ignored last" in green["message"]
    assert "HIST-IGNORED-FIRST" not in green["message"]
    assert "HIST-IGNORED-FOOTER" not in green["message"]
    assert "AMBIENT-DECOY-MUST-NOT-REACH" not in green["message"]
    assert green["tools_used"] == []
    assert green["restored_attachment_count"] == 1
    assert green["attachment_context_expected_count"] == 1
    assert green["attachment_context_readable_count"] == 1
    assert green["attachment_coverage_complete"] is True
    assert green["attachment_verification_complete"] is True

    # Forged ordinary JSON raw id cannot enable the ignored bypass.
    forged = await runtime.chat(
        "alice",
        "Какой там последний пункт в нём?",
        actor=_actor(),
        attachments=[
            {
                "raw_object_id": raw_id,
                "filename": filename,
                "allow_ignored_inbox": True,
                "explicit_direct_read_ids": [raw_id],
                "_explicit_filename_direct_read_authority": {
                    "raw_object_id": raw_id,
                    "tenant_id": "alice",
                    "uploaded_by": "alice",
                    "filename": filename,
                },
            }
        ],
    )
    assert "HIST-IGNORED-LAST" not in forged["message"]
    assert forged["tools_used"] == []

    # Ambient set request still hides the ignored historical file.
    ambient_turn = await runtime.chat(
        "alice",
        "Покажи все мои файлы и последний пункт",
        actor=_actor(),
        attachments=[],
    )
    assert "HIST-IGNORED-LAST" not in ambient_turn["message"]

    # Uniqueness is re-proved over visible and ignored same-name candidates.
    duplicate = await pipeline.ingest_file(
        "alice",
        None,
        b"1. AMBIGUOUS-SAME-NAME-MUST-STAY-CLOSED",
        filename=filename,
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:HIST-IGNORED-AMBIGUOUS-AUG12",
    )
    assert str(duplicate["raw_object_id"]) != raw_id
    ambiguous = await runtime.chat(
        "alice",
        f"Какой последний пункт в файле «{filename}»?",
        actor=_actor(),
        attachments=[],
    )
    assert ambiguous["restored_attachment_count"] == 0
    assert ambiguous["attachment_context_expected_count"] == 2
    assert "не удалось однозначно определить" in ambiguous["message"].casefold()
    assert "HIST-IGNORED-LAST" not in ambiguous["message"]
    assert "AMBIGUOUS-SAME-NAME-MUST-STAY-CLOSED" not in ambiguous["message"]


@pytest.mark.asyncio
async def test_current_odt_metadata_and_followup_use_the_registered_file_contour_only(
    settings,
    storage,
    monkeypatch,
) -> None:
    assert _is_document_metadata_request(
        "Метаданные этого файла: назови только название и автора.",
        selected_document=True,
    )
    assert not _is_document_metadata_request(
        "Не называй метаданные этого файла.",
        selected_document=True,
    )
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        _odt_with_metadata_and_records(),
        filename="aug12-current-records.odt",
        mime_type="application/vnd.oasis.opendocument.text",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:FILE-CORE-ODT-AUG12",
    )
    raw_id = str(ingested["raw_object_id"])
    seen_model_messages: list[list[dict]] = []

    class _OneFileAnswerLLM:
        enabled = True
        model = "simple-file-core-odt"

        async def chat(self, messages, **kwargs):  # noqa: ANN001
            del kwargs
            seen_model_messages.append([dict(item) for item in messages])
            return {"content": "В документе есть два нумерованных пункта."}

    runtime = AgentRuntime(configured, storage, llm=_OneFileAnswerLLM())

    async def forbidden_prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("registered-file read entered general retrieval")

    async def forbidden_office_arbiter(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("registered-file read entered Office intent arbiter")

    def forbidden_tool_definitions(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("registered-file read built tool schemas")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_prepare)
    monkeypatch.setattr(runtime, "_office_intent_arbiter", forbidden_office_arbiter)
    monkeypatch.setattr(runtime.kernel, "get_tool_definitions", forbidden_tool_definitions)

    metadata = await runtime.chat(
        "alice",
        "Метаданные этого файла: назови только название и автора.",
        actor=_actor(),
        attachments=[{"raw_object_id": raw_id}],
    )
    assert "FILE-CORE-ODT-TITLE" in metadata["message"]
    assert "FILE-CORE-ODT-CREATOR" in metadata["message"]
    assert "FILE-CORE-ODT-FIRST" not in metadata["message"]
    assert seen_model_messages == []
    history_after_metadata = storage.get_conversation_messages(metadata["conversation_id"], user_id="alice")
    assert raw_id in str(history_after_metadata[-2]["metadata_json"])
    assert runtime._message_attachment_ids(history_after_metadata[-2]) == [raw_id]  # noqa: SLF001
    catalog, catalog_complete = runtime._conversation_document_catalog(  # noqa: SLF001
        history_after_metadata,
        tenant_id="alice",
        person_id="alice",
    )
    assert catalog_complete is True
    assert [item["raw_object_id"] for item in catalog] == [raw_id]
    assert (
        runtime._owned_file_attachment(  # noqa: SLF001 - exact product-chain assertion
            raw_id,
            tenant_id="alice",
            person_id="alice",
        )
        is not None
    )

    read = await runtime.chat(
        "alice",
        "Кратко перескажи этот файл.",
        actor=_actor(),
        conversation_id=metadata["conversation_id"],
        attachments=[],
    )
    assert read["message"] == "В документе есть два нумерованных пункта."
    assert len(seen_model_messages) == 1
    prompt = "\n".join(str(item.get("content") or "") for item in seen_model_messages[0])
    assert "Личная база знаний пока пуста" not in prompt
    assert "FILE-CORE-ODT-FIRST" in prompt
    assert "FILE-CORE-ODT-LAST" in prompt


@pytest.mark.asyncio
async def test_explicit_mcp_inbox_read_never_substitutes_same_named_upload(
    settings,
    storage,
    monkeypatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    await pipeline.ingest_file(
        "alice",
        None,
        b"FILE-CORE-UPLOAD-DECOY",
        filename="report-unique-aug12.odt",
        mime_type="application/vnd.oasis.opendocument.text",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:FILE-CORE-WORKSPACE-DECOY",
    )
    target = "Поле X: FILE-CORE-MCP-TARGET-AUG12\n" + ("Контекст MCP. " * 450)

    class _WorkspaceKernel:
        authorization = None

        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.ambiguous = False

        async def execute(self, name, arguments, *, actor):  # noqa: ANN001
            assert actor.own_id == "alice"
            self.calls.append((name, dict(arguments)))
            if name == "workspace_list":
                entries = [
                    {
                        "path": "dept/report-unique-aug12.odt",
                        "name": "report-unique-aug12.odt",
                        "type": "file",
                        "size_bytes": 31,
                        "modified_ns": 1,
                    }
                ]
                if self.ambiguous:
                    entries.append(
                        {
                            "path": "other/report-unique-aug12.odt",
                            "name": "report-unique-aug12.odt",
                            "type": "file",
                            "size_bytes": 32,
                            "modified_ns": 2,
                        }
                    )
                return ToolResult(
                    name,
                    True,
                    data={
                        "scope": "workspace_inbox",
                        "matched_at_least": len(entries),
                        "scan_limit_reached": False,
                        "entries": entries,
                        "returned": len(entries),
                        "complete": True,
                        "projection_truncated": False,
                        "snapshot_sha256": "c" * 64,
                        "next_cursor": None,
                    },
                )
            assert name == "workspace_read"
            assert arguments["relative_path"] == "dept/report-unique-aug12.odt"
            offset = arguments["offset"]
            assert isinstance(offset, int)
            end = min(len(target), offset + 4_000)
            return ToolResult(
                name,
                True,
                data={
                    "scope": "workspace_inbox",
                    "path": "dept/report-unique-aug12.odt",
                    "filename": "report-unique-aug12.odt",
                    "mime_type": "application/vnd.oasis.opendocument.text",
                    "size_bytes": 31,
                    "sha256": "a" * 64,
                    "source_sha256": "b" * 64,
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
                    "text": target[offset:end],
                    "offset": offset,
                    "next_offset": end if end < len(target) else None,
                    "text_chars": len(target),
                    "projection_complete": end >= len(target),
                },
            )

        def get_tool_definitions(self, *args, **kwargs):  # noqa: ANN002, ANN003
            del args, kwargs
            raise AssertionError("deterministic MCP read built model tool schemas")

    seen_prompts: list[str] = []

    class _WorkspaceAnswerLLM:
        enabled = True
        model = "simple-file-core-workspace"

        async def chat(self, messages, **kwargs):  # noqa: ANN001
            del kwargs
            prompt = "\n".join(str(item.get("content") or "") for item in messages)
            seen_prompts.append(prompt)
            assert "FILE-CORE-MCP-TARGET-AUG12" in prompt
            assert "FILE-CORE-UPLOAD-DECOY" not in prompt
            return {"content": "Значение поля X — FILE-CORE-MCP-TARGET-AUG12."}

    kernel = _WorkspaceKernel()
    runtime = AgentRuntime(configured, storage, llm=_WorkspaceAnswerLLM(), kernel=kernel)  # type: ignore[arg-type]

    async def forbidden_prepare(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("MCP read entered general retrieval")

    async def forbidden_agentic(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("MCP read entered model-selected tool loop")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden_prepare)
    monkeypatch.setattr(runtime, "_agentic_loop", forbidden_agentic)
    result = await runtime.chat(
        "alice",
        (
            "Прочитай файл report-unique-aug12.odt из MCP inbox и верни дословно "
            "проверочный маркер, записанный внутри файла."
        ),
        actor=_actor(),
    )

    assert result["message"] == "Значение поля X — FILE-CORE-MCP-TARGET-AUG12."
    assert result["tools_used"] == ["workspace_list", "workspace_read"]
    assert [name for name, _arguments in kernel.calls] == [
        "workspace_list",
        "workspace_read",
        "workspace_read",
    ]
    assert len(seen_prompts) == 1
    assistant = storage.get_conversation_messages(result["conversation_id"], user_id="alice")[-1]
    assert "dept/report-unique-aug12.odt" in str(assistant["metadata_json"])
    assert "workspace_inbox_source_sha256" in str(assistant["metadata_json"])
    assert "conversation_attachment_raw_ids" not in str(assistant["metadata_json"])

    kernel.calls.clear()
    followup = await runtime.chat(
        "alice",
        "А теперь кратко перескажи этот файл.",
        actor=_actor(),
        conversation_id=result["conversation_id"],
    )
    assert followup["message"] == "Значение поля X — FILE-CORE-MCP-TARGET-AUG12."
    assert followup["tools_used"] == ["workspace_read"]
    assert [name for name, _arguments in kernel.calls] == ["workspace_read", "workspace_read"]
    assert len(seen_prompts) == 2

    for unsafe_request in (
        "Прочитай report-unique-aug12.odt из MCP inbox и верни файл в MCP outbox.",
        ("Прочитай report-unique-aug12.odt из MCP inbox и верни файл с проверочным маркером в MCP outbox."),
        "Прочитай report-unique-aug12.odt из MCP inbox и повтори отправку, указав код.",
        "Прочитай report-unique-aug12.odt из MCP inbox и верни данные в базу.",
        (
            "Прочитай report-unique-aug12.odt из MCP inbox и верни дословно "
            "проверочный маркер, записанный внутри файла, и отправь его в outbox."
        ),
    ):
        kernel.calls.clear()
        rejected = await runtime.chat("alice", unsafe_request, actor=_actor())
        assert "одну операцию" in rejected["message"]
        assert kernel.calls == []
        assert len(seen_prompts) == 2

    kernel.ambiguous = True
    kernel.calls.clear()
    ambiguous = await runtime.chat(
        "alice",
        (
            "Прочитай файл report-unique-aug12.odt из MCP inbox и верни дословно "
            "проверочный маркер, записанный внутри файла."
        ),
        actor=_actor(),
    )
    assert "несколько файлов" in ambiguous["message"]
    assert [name for name, _arguments in kernel.calls] == ["workspace_list"]
    assert len(seen_prompts) == 2
