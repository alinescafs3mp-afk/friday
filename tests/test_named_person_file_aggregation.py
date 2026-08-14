"""Exact-uploader corpus selection for named-person document synthesis."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import replace
from typing import Any

import pytest
from openpyxl import Workbook

from friday.agent_runtime import (
    _UNCONFIRMED_SUPPORTED_DEED,
    AgentContext,
    AgentRuntime,
    _named_person_aggregation_scope,
    _named_uploader_exact_file_request,
)
from friday.execution_kernel import ExecutionKernel
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import AuthorizationService
from friday.storage.models import RawObject, new_id


def _odt_body(text: str) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "mimetype",
            "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr(
            "content.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
 <office:body><office:text><text:p>"""
            + text
            + """</text:p></office:text></office:body>
</office:document-content>""",
        )
    return payload.getvalue()


def _xlsx_body(text: str) -> bytes:
    payload = io.BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Состав", "Вооружение"])
    sheet.append([text, "АК-74 №609416"])
    workbook.save(payload)
    return payload.getvalue()


def _file(
    storage,
    tenant: str,
    uploader: str | None,
    filename: str,
    body: str,
    received_at: str,
    *,
    document_date: str = "",
) -> str:
    metadata: dict[str, Any] = {
        "filename": filename,
        "mime_type": "text/plain",
        "size_bytes": len(body.encode()),
        "extraction_success": True,
        "extraction_chars": len(body),
    }
    if uploader is not None:
        metadata["uploaded_by"] = uploader
    if document_date:
        metadata["document_date"] = document_date
    raw = RawObject(
        id=new_id("raw"),
        user_id=tenant,
        source="telegram",
        source_ref=new_id("src"),
        raw_content=body,
        content_type="file",
        content_hash=hashlib.sha256(f"{filename}:{body}".encode()).hexdigest(),
        metadata_json=metadata,
    )
    storage.store_raw_object(raw)
    storage.execute("UPDATE raw_objects SET received_at=? WHERE id=?", (received_at, raw.id))
    storage.commit()
    return raw.id


async def _registered_file(
    settings,
    storage,
    tenant: str,
    uploader: str,
    filename: str,
    body: str,
    received_at: str,
) -> str:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    ingested = await pipeline.ingest_file(
        tenant,
        None,
        body.encode(),
        filename=filename,
        mime_type="text/plain",
        metadata={"uploaded_by": uploader},
        source_ref=f"telegram-file:{new_id('src')}",
    )
    raw_id = str(ingested["raw_object_id"])
    storage.execute("UPDATE raw_objects SET received_at=? WHERE id=?", (received_at, raw_id))
    storage.commit()
    return raw_id


def _runtime(settings, storage):
    tenant = "shared-archive"
    storage.ensure_user(tenant, preset_key="owner", display_name="Archive")
    storage.ensure_user("owner", preset_key="owner", display_name="Owner")
    storage.ensure_user("usr_jbl", preset_key="user", display_name="JBL", username="jbl")
    storage.ensure_user("usr_anna", preset_key="user", display_name="Anna", username="anna")
    auth = AuthorizationService(storage, shared_tenant=tenant)
    kernel = ExecutionKernel(auth, settings)
    runtime = AgentRuntime(replace(settings, verify_answers=False), storage, kernel=kernel)
    return runtime, auth.actor_for_user("owner", source="test"), tenant


def test_received_range_is_exact_uploader_scoped_and_unique_short_typo_resolves(
    settings,
    storage,
) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    _file(
        storage,
        tenant,
        "usr_jbl",
        "jbl-a.odt",
        "JBL-FIRST-TAIL",
        "2026-08-08T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "jbl-b.odt",
        "JBL-SECOND-TAIL",
        "2026-08-10T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "usr_anna",
        "decoy.odt",
        "FOREIGN-DECOY-MUST-NOT-APPEAR",
        "2026-08-09T09:00:00+00:00",
    )
    scope = _named_person_aggregation_scope(
        "Обобщи данные, которые приходили от пользователя GBL с 7 по 11 августа",
        [],
    )

    selected = runtime._select_named_person_corpus(scope, actor=actor)  # noqa: SLF001

    assert selected.applies and selected.complete
    assert selected.person_id == "usr_jbl"
    assert selected.expected_count == selected.selected_count == 2
    bodies = [str(item.get("transient_text") or "") for item in selected.attachments]
    assert bodies == ["JBL-SECOND-TAIL", "JBL-FIRST-TAIL"]
    assert "FOREIGN-DECOY" not in " ".join(bodies)


def test_unqualified_document_period_uses_arrival_time_not_own_document_date() -> None:
    scope = _named_person_aggregation_scope(
        "Обобщи документы за период с 7 по 11 августа от пользователя JBL",
        [],
    )

    assert scope is not None
    assert scope.time_role == "received_at"


def test_latest_two_uses_arrival_order_not_document_date(settings, storage) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    _file(
        storage,
        tenant,
        "usr_jbl",
        "old-upload-new-document.txt",
        "OLD-UPLOAD",
        "2026-08-07T09:00:00+00:00",
        document_date="2026-08-11",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "middle.txt",
        "MIDDLE-UPLOAD",
        "2026-08-08T09:00:00+00:00",
        document_date="2026-08-01",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "latest.txt",
        "LATEST-UPLOAD",
        "2026-08-09T09:00:00+00:00",
        document_date="2025-01-01",
    )
    scope = _named_person_aggregation_scope(
        "Проанализируй последние 2 файла пользователя JBL",
        [],
    )

    selected = runtime._select_named_person_corpus(scope, actor=actor)  # noqa: SLF001

    assert selected.complete and selected.expected_count == 2
    assert [item["transient_text"] for item in selected.attachments] == [
        "LATEST-UPLOAD",
        "MIDDLE-UPLOAD",
    ]


def test_named_uploader_and_filename_allow_one_unique_approximation_but_not_a_decoy(
    settings,
    storage,
) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    wanted = _file(
        storage,
        tenant,
        "usr_jbl",
        "field-report-7969-final.odt",
        "JBL-7969-EXACT-BODY",
        "2026-08-09T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "owner",
        "field-report-7969-final.odt",
        "OWNER-SAME-NAME-DECOY",
        "2026-08-10T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "unrelated-budget.odt",
        "JBL-UNRELATED-DECOY",
        "2026-08-11T09:00:00+00:00",
    )
    request = _named_uploader_exact_file_request(
        "GBL присылал тебе файл field-report-7969-fianl.odt, о чём он?"
    )
    live_request = _named_uploader_exact_file_request("JBL скидывал тебе файл 7969.odt, о чём он?")

    selected = runtime._select_exact_uploader_file(request, actor=actor)  # noqa: SLF001

    assert request == ("GBL", "field-report-7969-fianl.odt")
    assert live_request == ("JBL", "7969.odt")
    assert selected.applies and selected.expected_count == 1
    assert selected.person_id == "usr_jbl"
    assert len(selected.attachments) == 1
    assert selected.attachments[0]["raw_object_id"] == wanted
    assert selected.attachments[0]["transient_text"] == "JBL-7969-EXACT-BODY"
    assert "OWNER-SAME-NAME-DECOY" not in str(selected.attachments)
    assert "JBL-UNRELATED-DECOY" not in str(selected.attachments)

    after = _named_uploader_exact_file_request("Покажи файл field-report-7969-final.odt от пользователя JBL")
    assert after == ("JBL", "field-report-7969-final.odt")
    assert _named_uploader_exact_file_request("Мы с JBL обсуждали field-report-7969-final.odt") is None
    assert (
        _named_uploader_exact_file_request(
            "Создай result.odt с подписью «JBL присылал тебе файл field-report-7969-final.odt»"
        )
        is None
    )
    assert (
        _named_uploader_exact_file_request(
            "JBL присылал тебе файлы field-report-7969-final.odt и unrelated-budget.odt"
        )
        is None
    )

    denied_actor = runtime.kernel.authorization.actor_for_user("usr_anna", source="test")
    denied = runtime._select_exact_uploader_file(  # noqa: SLF001
        ("JBL", "field-report-7969-final.odt"),
        actor=denied_actor,
    )
    assert denied.applies and denied.attachments == ()
    assert denied.reason == "access_denied"

    _file(
        storage,
        tenant,
        "usr_jbl",
        "field-report-7969-final.odt",
        "JBL-DUPLICATE-SAME-NAME",
        "2026-08-12T10:00:00+00:00",
    )
    duplicate = runtime._select_exact_uploader_file(  # noqa: SLF001
        ("JBL", "field-report-7969-final.odt"),
        actor=actor,
    )
    assert duplicate.applies and duplicate.attachments == ()
    assert duplicate.reason == "file_not_unique"


@pytest.mark.asyncio
async def test_named_uploader_approximate_filename_chat_reads_only_registered_uploader_bytes(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    configured = replace(settings, verify_answers=False)
    runtime.settings = configured
    runtime.llm.settings = configured
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    wanted = await pipeline.ingest_file(
        tenant,
        None,
        _odt_body("JBL-7969-REGISTERED-DISK-CANARY"),
        filename="field-report-7969-final.odt",
        mime_type="application/vnd.oasis.opendocument.text",
        metadata={"uploaded_by": "usr_jbl"},
        source_ref="telegram-file:JBL-7969-WANTED",
    )
    await pipeline.ingest_file(
        tenant,
        None,
        _odt_body("OWNER-SAME-NAME-REGISTERED-DECOY"),
        filename="field-report-7969-final.odt",
        mime_type="application/vnd.oasis.opendocument.text",
        metadata={"uploaded_by": "owner"},
        source_ref="telegram-file:OWNER-7969-DECOY",
    )
    seen: list[list[dict[str, Any]]] = []

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("named file selector entered ambient/tool context")

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message
        snapshot = [dict(item) for item in attachments or []]
        seen.append(snapshot)
        body = "\n".join(str(item.get("transient_text") or "") for item in snapshot)
        assert "JBL-7969-REGISTERED-DISK-CANARY" in body
        assert "OWNER-SAME-NAME-REGISTERED-DECOY" not in body
        return {"content": "Это зарегистрированный отчёт JBL.", "tools_used": []}

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_agentic_loop", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    result = await runtime.chat(
        actor.own_id,
        "GBL присылал тебе файл field-report-7969-fianl.odt, о чём он?",
        actor=actor,
        enable_tools=True,
    )

    assert len(seen) == 1
    assert [item["raw_object_id"] for item in seen[0]] == [str(wanted["raw_object_id"])]
    assert result["message"] == "Это зарегистрированный отчёт JBL."
    assert result["tools_used"] == []
    assert result["attachment_context_expected_count"] == 1
    assert result["attachment_context_readable_count"] == 1
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True

    missing = await runtime.chat(
        actor.own_id,
        "GBL присылал тебе файл field-report-7969-fianl.odt, найди CASE-404",
        actor=actor,
        enable_tools=True,
    )
    assert len(seen) == 1, "a residual body lookup was erased with named-uploader navigation"
    assert "совпадение по запросу не найдено" in missing["message"]


@pytest.mark.asyncio
async def test_filename_substring_inventory_lists_names_without_hydrating_bodies(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    _file(
        storage,
        tenant,
        "owner",
        "ШТАТКА 01.06.2024.docx",
        "OWNER-NAME-MATCH-BODY",
        "2026-08-09T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "owner",
        "штатка_назначение.xlsx",
        "OWNER-SECOND-NAME-MATCH-BODY",
        "2026-08-10T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "owner",
        "ordinary.txt",
        "ШТАТКА-ONLY-IN-BODY",
        "2026-08-11T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "JBL-ШТАТКА-DECOY.txt",
        "FOREIGN-UPLOADER-DECOY",
        "2026-08-12T09:00:00+00:00",
    )

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("filename inventory read a body or called the model")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    monkeypatch.setattr(runtime, "_agentic_loop", forbidden)
    result = await runtime.chat(
        actor.own_id,
        "У тебя есть файл, в названии которого содержится слово ШТАТКА?",
        actor=actor,
        enable_tools=True,
    )

    assert "найдено совпадений: 2" in result["message"]
    assert "ШТАТКА 01.06.2024.docx" in result["message"]
    assert "штатка_назначение.xlsx" in result["message"]
    assert "ordinary.txt" not in result["message"]
    assert "JBL-ШТАТКА-DECOY" not in result["message"]
    assert "ONLY-IN-BODY" not in result["message"]
    assert result["tools_used"] == []
    assert result["attachment_context_expected_count"] == 0
    assert result["attachment_context_readable_count"] == 0

    natural = await runtime.chat(
        actor.own_id,
        "В штатке посмотри, я кидал уже",
        actor=actor,
        enable_tools=True,
    )
    assert "найдено совпадений: 2" in natural["message"]
    assert "ШТАТКА 01.06.2024.docx" in natural["message"]
    assert "штатка_назначение.xlsx" in natural["message"]
    assert "JBL-ШТАТКА-DECOY" not in natural["message"]
    assert natural["tools_used"] == []
    assert natural["attachment_context_expected_count"] == 0
    assert natural["attachment_context_readable_count"] == 0


@pytest.mark.asyncio
async def test_two_term_approximate_filename_question_reads_the_unique_registered_file(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    configured = replace(settings, verify_answers=False)
    runtime.settings = configured
    runtime.llm.settings = configured
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    wanted = await pipeline.ingest_file(
        tenant,
        None,
        _xlsx_body("Цветков Никита Андреевич"),
        filename="БПЛА Штат v1.1.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        metadata={"uploaded_by": "owner"},
        source_ref="telegram-file:BPLA-STATE-WANTED",
    )
    await pipeline.ingest_file(
        tenant,
        None,
        b"AMBIENT-SAME-PERSON-DECOY",
        filename="ordinary-report.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "owner"},
        source_ref="telegram-file:BPLA-AMBIENT-DECOY",
    )
    seen: list[list[dict[str, Any]]] = []

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("approximate filename question entered ambient/tool context")

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message
        snapshot = [dict(item) for item in attachments or []]
        seen.append(snapshot)
        model_visible = "\n".join(
            str(item.get("_office_prompt_serialized") or item.get("transient_text") or "")
            for item in snapshot
        )
        assert "Цветков Никита Андреевич" in model_visible
        assert "609416" in model_visible
        assert "AMBIENT-SAME-PERSON-DECOY" not in model_visible
        return {"content": "Да, Цветков Никита Андреевич и автомат №609416 указаны.", "tools_used": []}

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_agentic_loop", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    result = await runtime.chat(
        actor.own_id,
        "Скажи, Цветков Никита Андреевич с АК-74 №609416 — БПЛА штат, он там есть?",
        actor=actor,
        enable_tools=True,
    )

    assert len(seen) == 1
    assert [item["raw_object_id"] for item in seen[0]] == [str(wanted["raw_object_id"])]
    assert "Цветков Никита Андреевич" in result["message"]
    assert result["tools_used"] == []
    assert result["attachment_context_expected_count"] == 1
    assert result["attachment_context_readable_count"] == 1
    assert result["attachment_coverage_complete"] is True
    assert result["attachment_verification_complete"] is True


@pytest.mark.asyncio
async def test_recent_file_pair_typo_and_plain_language_followups_keep_exact_lineage(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime, _actor, tenant = _runtime(settings, storage)
    configured = replace(settings, verify_answers=False)
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    first = await pipeline.ingest_file(
        tenant,
        None,
        b"FIRST-RECENT-FILE people movement list",
        filename="first-recent.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "owner"},
        source_ref="telegram-file:FIRST-RECENT",
    )
    second = await pipeline.ingest_file(
        tenant,
        None,
        "SECOND-RECENT-FILE список людей кому разрешено перемещение".encode(),
        filename="second-recent.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "owner"},
        source_ref="telegram-file:SECOND-RECENT",
    )
    ambient = await pipeline.ingest_file(
        tenant,
        None,
        b"NEWER-AMBIENT-FILE-MUST-NOT-ENTER-RECENT-PAIR",
        filename="newer-ambient.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "owner"},
        source_ref="telegram-file:NEWER-AMBIENT",
    )
    first_id = str(first["raw_object_id"])
    second_id = str(second["raw_object_id"])
    ambient_id = str(ambient["raw_object_id"])
    conversation = storage.create_conversation("owner")

    def record_turn(raw_id: str, label: str, answer: str) -> None:
        storage.store_message(
            str(conversation["id"]),
            "owner",
            "user",
            label,
            metadata={
                "had_attachments": True,
                "attachment_count": 1,
                "conversation_uploaded_raw_ids": [raw_id],
                "conversation_attachment_raw_ids": [raw_id],
            },
        )
        storage.store_message(
            str(conversation["id"]),
            "owner",
            "assistant",
            answer,
            metadata={
                "attachment_context_used": True,
                "conversation_attachment_raw_ids": [raw_id],
            },
        )

    record_turn(first_id, "first upload", "Первый документ прочитан.")
    record_turn(
        second_id,
        "second upload",
        "В документе приведён список людей, которым разрешено перемещение.",
    )
    history = storage.get_conversation_messages(str(conversation["id"]), user_id="owner")

    pair, pair_expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "обощи всё, опираясь на эти два файла",
        history,
        tenant_id=tenant,
        person_id="owner",
        allow_file_read=True,
    )
    mixed_history = [
        *history,
        {
            "role": "user",
            "content": "third upload without a completed assistant read",
            "created_at": "2026-08-13T18:10:00+00:00",
            "metadata_json": json.dumps(
                {
                    "had_attachments": True,
                    "attachment_count": 1,
                    "attachment_origin": "upload",
                    "conversation_uploaded_raw_ids": [ambient_id],
                    "conversation_attachment_raw_ids": [ambient_id],
                }
            ),
        },
    ]
    incomplete_three, incomplete_three_expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "обощи эти три файла",
        mixed_history,
        tenant_id=tenant,
        person_id="owner",
        allow_file_read=True,
    )
    topical, topical_expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "Дай список людей кому разрешено перемещение",
        history,
        tenant_id=tenant,
        person_id="owner",
        allow_file_read=True,
    )
    table, table_expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "а можешь мне его в виде таблички оформить?",
        history,
        tenant_id=tenant,
        person_id="owner",
        allow_file_read=True,
    )
    summary, summary_expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "дай сводку по этому файлу",
        history,
        tenant_id=tenant,
        person_id="owner",
        allow_file_read=True,
    )

    assert [item["raw_object_id"] for item in pair] == [first_id, second_id]
    assert ambient_id not in {str(item["raw_object_id"]) for item in pair}
    assert pair_expected == 2
    assert incomplete_three == []
    assert incomplete_three_expected == 3
    for label, restored, expected in (
        ("topical", topical, topical_expected),
        ("table", table, table_expected),
        ("summary", summary, summary_expected),
    ):
        assert [item["raw_object_id"] for item in restored] == [second_id], label
        assert expected == 1, label

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("local follow-up entered ambient archive/tool loop")

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message
        body = "\n".join(str(item.get("transient_text") or "") for item in attachments or [])
        assert "SECOND-RECENT-FILE" in body
        assert "FIRST-RECENT-FILE" not in body
        return {"content": "В файле перечислены люди с разрешением на перемещение.", "tools_used": []}

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_agentic_loop", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    result = await runtime.chat(
        "owner",
        "Дай список людей кому разрешено перемещение",
        actor=runtime.kernel.authorization.actor_for_user("owner", source="test"),
        conversation_id=str(conversation["id"]),
        enable_tools=True,
    )
    assert result["message"] == "В файле перечислены люди с разрешением на перемещение."
    assert result["tools_used"] == []
    assert result["attachment_context_expected_count"] == 1
    assert result["attachment_context_readable_count"] == 1

    # Once the exact adjacent lineage is no longer authorized, the same
    # continuation remains an expected one-file request and closes.  It must
    # not lose its selector and fall through to the newer ambient catalog row.
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET deleted_at='2026-08-13T18:00:00Z' WHERE id=?",
            (second_id,),
        )
    closed_history = storage.get_conversation_messages(str(conversation["id"]), user_id="owner")
    closed, closed_expected = runtime._restore_conversation_attachments(  # noqa: SLF001
        "Дай список людей кому разрешено перемещение",
        closed_history,
        tenant_id=tenant,
        person_id="owner",
        allow_file_read=True,
    )
    assert closed == []
    assert closed_expected == 1


def test_explicit_document_date_role_uses_own_date_and_reports_undated_ceiling(
    settings,
    storage,
) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    _file(
        storage,
        tenant,
        "usr_jbl",
        "inside.txt",
        "OWN-DATE-INSIDE",
        "2026-08-11T09:00:00+00:00",
        document_date="2026-08-08",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "outside.txt",
        "OWN-DATE-OUTSIDE",
        "2026-08-08T09:00:00+00:00",
        document_date="2026-08-12",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "undated.txt",
        "OWN-DATE-UNKNOWN",
        "2026-08-09T09:00:00+00:00",
    )
    scope = _named_person_aggregation_scope(
        "Обобщи документы пользователя JBL, датированные с 7 по 11 августа",
        [],
    )

    selected = runtime._select_named_person_corpus(scope, actor=actor)  # noqa: SLF001

    assert scope is not None and scope.time_role == "document_date"
    assert [item["transient_text"] for item in selected.attachments] == ["OWN-DATE-INSIDE"]
    assert selected.undated == 1
    assert selected.complete is False and selected.reason == "document_dates_incomplete"


def test_scope_only_typo_inherits_the_immediately_prior_range(settings, storage) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    _file(
        storage,
        tenant,
        "usr_jbl",
        "inside.txt",
        "INSIDE",
        "2026-08-09T09:00:00+00:00",
    )
    _file(
        storage,
        tenant,
        "usr_jbl",
        "outside.txt",
        "OUTSIDE",
        "2026-08-06T09:00:00+00:00",
    )
    history = [
        {
            "role": "user",
            "content": "Обобщи данные, которые приходили с 7 по 11 августа",
        },
        {"role": "assistant", "content": "Уточните пользователя"},
    ]
    scope = _named_person_aggregation_scope("данные от пользователя GBL", history)

    selected = runtime._select_named_person_corpus(scope, actor=actor)  # noqa: SLF001

    assert scope is not None and scope.inherited and scope.time_role == "received_at"
    assert selected.complete
    assert [item["transient_text"] for item in selected.attachments] == ["INSIDE"]


def test_self_corpus_uses_files_read_without_admin_oversight(settings, storage) -> None:
    runtime, _owner, tenant = _runtime(settings, storage)
    auth = runtime.kernel.authorization
    assert auth is not None
    actor = auth.actor_for_user("usr_jbl", source="test")
    assert auth.authorize(actor, "files.read").allowed
    assert not auth.authorize(actor, "admin.all_data.read").allowed
    _file(
        storage,
        tenant,
        "usr_jbl",
        "self.txt",
        "SELF-CORPUS",
        "2026-08-09T09:00:00+00:00",
    )
    scope = _named_person_aggregation_scope(
        "Обобщи данные, которые приходили с 7 по 11 августа",
        [],
    )

    selected = runtime._select_named_person_corpus(scope, actor=actor)  # noqa: SLF001

    assert selected.complete and selected.person_id == "usr_jbl"
    assert [item["transient_text"] for item in selected.attachments] == ["SELF-CORPUS"]


def test_ambiguity_and_corpus_cap_fail_closed(settings, storage) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    storage.ensure_user("usr_hbl", preset_key="user", display_name="HBL")
    ambiguous = runtime._select_named_person_corpus(  # noqa: SLF001
        _named_person_aggregation_scope("Обобщи данные от пользователя GBL", []),
        actor=actor,
    )
    assert ambiguous.reason == "person_ambiguous"
    storage.update_user("usr_hbl", status="disabled")
    for index in range(13):
        _file(
            storage,
            tenant,
            "usr_jbl",
            f"jbl-{index:02d}.txt",
            f"BODY-{index:02d}",
            f"2026-08-{index + 1:02d}T09:00:00+00:00",
        )
    capped = runtime._select_named_person_corpus(  # noqa: SLF001
        _named_person_aggregation_scope("Обобщи данные от пользователя JBL", []),
        actor=actor,
    )
    assert capped.available_total == capped.expected_count == 13
    assert capped.selected_count == 12
    assert capped.complete is False and capped.reason == "corpus_capped"


@pytest.mark.asyncio
async def test_chat_synthesis_receives_tails_from_each_selected_file(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    await _registered_file(
        settings,
        storage,
        tenant,
        "usr_jbl",
        "first.txt",
        "FIRST-HEAD " + "alpha " * 300 + "FIRST-TAIL",
        "2026-08-08T09:00:00+00:00",
    )
    await _registered_file(
        settings,
        storage,
        tenant,
        "usr_jbl",
        "second.txt",
        "SECOND-HEAD " + "beta " * 300 + "SECOND-TAIL",
        "2026-08-09T09:00:00+00:00",
    )
    seen: list[str] = []

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=actor.own_id)

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message
        joined = "\n".join(str(item.get("transient_text") or "") for item in attachments or [])
        seen.append(joined)
        return {"content": "Сводка по двум файлам.", "tools_used": [], "_model_generated": True}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    result = await runtime.chat(
        actor.own_id,
        "Обобщи данные от пользователя JBL",
        actor=actor,
        enable_tools=True,
    )

    assert seen and "FIRST-TAIL" in seen[0] and "SECOND-TAIL" in seen[0]
    assert "Сводка по двум файлам" in result["message"]
    assert "FOREIGN" not in result["message"]


@pytest.mark.asyncio
async def test_complete_named_person_corpus_owns_only_passive_historical_file_state(
    settings,
    storage,
    monkeypatch,
) -> None:
    runtime, actor, tenant = _runtime(settings, storage)
    runtime.settings = replace(
        runtime.settings,
        llm_enabled=True,
        verify_answers=True,
        verify_min_answer_chars=1,
    )
    runtime.llm.settings = runtime.settings
    await _registered_file(
        runtime.settings,
        storage,
        tenant,
        "usr_jbl",
        "first.txt",
        "KAPPA-731",
        "2026-08-08T09:00:00+00:00",
    )
    await _registered_file(
        runtime.settings,
        storage,
        tenant,
        "usr_jbl",
        "second.txt",
        "SIGMA-482",
        "2026-08-09T09:00:00+00:00",
    )
    await _registered_file(
        runtime.settings,
        storage,
        tenant,
        "usr_jbl",
        "third.txt",
        "OMEGA-915",
        "2026-08-10T09:00:00+00:00",
    )
    await _registered_file(
        runtime.settings,
        storage,
        tenant,
        "usr_jbl",
        "outside-window.txt",
        "OUTSIDE-WINDOW-004",
        "2026-08-12T09:00:00+00:00",
    )
    generated = iter(
        (
            ("Исторические файлы сохранены в архиве: OMEGA-915, SIGMA-482, KAPPA-731."),
            "Я создала и прикрепила файл с итогами.",
        )
    )
    seen_source_sequences: list[list[str]] = []
    verifier_evidence: list[list[str]] = []

    async def prepare(user_id, message, conversation_id, **kwargs):  # noqa: ANN001
        del message, kwargs
        return AgentContext(conversation_id=conversation_id, user_id=user_id, person_id=actor.own_id)

    async def generate(context, message, attachments):  # noqa: ANN001
        del context, message
        seen_source_sequences.append([str(item.get("transient_text") or "") for item in attachments or []])
        return {
            "content": next(generated),
            "tools_used": [],
            "_model_generated": True,
        }

    async def verify(question, answer, context, *, tool_evidence=None):  # noqa: ANN001
        del question, answer, context
        verifier_evidence.append(
            [
                str(item.get("output") or "")
                for item in tool_evidence or []
                if str(item.get("tool") or "") == "attachment"
            ]
        )
        return {"status": "passed", "ok": True, "score": 1.0, "issues": []}

    monkeypatch.setattr(runtime, "_prepare_context", prepare)
    monkeypatch.setattr(runtime, "_generate_response", generate)
    monkeypatch.setattr(runtime, "_verify_response", verify)
    request = "Обобщи данные, которые приходили от пользователя JBL с 7 по 11 августа"

    passive = await runtime.chat(actor.own_id, request, actor=actor, enable_tools=True)
    active = await runtime.chat(actor.own_id, request, actor=actor, enable_tools=True)

    assert seen_source_sequences == [
        ["OMEGA-915", "SIGMA-482", "KAPPA-731"],
        ["OMEGA-915", "SIGMA-482", "KAPPA-731"],
    ]
    assert verifier_evidence == []
    assert _UNCONFIRMED_SUPPORTED_DEED not in passive["message"]
    assert "Исторические файлы сохранены" in passive["message"]
    assert passive["message"].index("OMEGA-915") < passive["message"].index("SIGMA-482")
    assert passive["message"].index("SIGMA-482") < passive["message"].index("KAPPA-731")
    assert "OUTSIDE-WINDOW-004" not in passive["message"]
    assert passive["verification_status"] == "skipped"
    assert _UNCONFIRMED_SUPPORTED_DEED in active["message"]
    assert "Я создала и прикрепила" not in active["message"]
