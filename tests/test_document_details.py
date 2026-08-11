from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace

import pytest

from friday.agent_runtime import (
    AgentContext,
    AgentRuntime,
    _document_metadata_answer,
    _document_metadata_request_scope,
    _OwnedAttachment,
)
from friday.document_details import (
    DocumentDetailsCoverage,
    render_document_details,
    validate_document_detail_payload,
)
from friday.documents import DocumentExtractor
from friday.permissions import ActorContext
from friday.storage.models import InboxItem, InboxStatus, RawObject, new_id


class _DocumentDetailsLLM:
    enabled = True
    model = "document-details-test"

    def __init__(self, *, fail_on_call: int = 0) -> None:
        self.calls: list[str] = []
        self.fail_on_call = fail_on_call

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        assert kwargs.get("tools") == []
        payload = str(messages[-1]["content"])
        self.calls.append(payload)
        if self.fail_on_call == len(self.calls):
            raise TimeoutError("synthetic detail timeout")
        details: list[dict[str, str]] = []
        if "полковник Иванов И.И." in payload:
            details.extend(
                [
                    {
                        "kind": "signatory",
                        "evidence": "Подписал: командир части полковник Иванов И.И.",
                    },
                    {
                        "kind": "classification",
                        "evidence": "СОВЕРШЕННО СЕКРЕТНО (ЭТОЙ СТРОКИ НЕТ)",
                    },
                ]
            )
        if "Дата документа: 15.07.2026" in payload:
            details.append({"kind": "date", "evidence": "Дата документа: 15.07.2026"})
        return {"content": json.dumps({"details": details}, ensure_ascii=False)}


def _stored_odt(  # noqa: ANN001
    storage,
    *,
    text: str,
    filename: str = "приказ.odt",
    extra_metadata: dict[str, object] | None = None,
) -> RawObject:
    storage.ensure_user("alice")
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="upload",
        source_ref=new_id("source"),
        raw_content=text,
        content_type="file",
        metadata_json={
            "filename": filename,
            "uploaded_by": "alice",
            "mime_type": "application/vnd.oasis.opendocument.text",
            "format": "odt",
            "metadata_schema_version": 2,
            "title": "Учебный приказ",
            "extraction_success": True,
            "text_extraction_success": True,
            **(extra_metadata or {}),
        },
    )
    storage.store_raw_object(raw)
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id="alice",
            raw_object_id=raw.id,
            status=InboxStatus.PENDING,
            suggested_action="review",
        )
    )
    return raw


def test_only_literal_document_detail_evidence_is_accepted() -> None:
    text = (
        "ГРИФ: ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ\n"
        "УТВЕРЖДАЮ\nКомандир части полковник Иванов И.И.\n"
        "Подписал: начальник штаба майор Петров П.П."
    )
    payload = json.dumps(
        {
            "details": [
                {
                    "kind": "classification",
                    "evidence": "ГРИФ: ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ",
                },
                {
                    "kind": "approver",
                    "evidence": "УТВЕРЖДАЮ\nКомандир части полковник Иванов И.И.",
                },
                {
                    "kind": "signatory",
                    "evidence": "Подписал: генерал Несуществующий",
                },
            ]
        },
        ensure_ascii=False,
    )

    records = validate_document_detail_payload(
        payload,
        chunk_text=text,
        filename="приказ.odt",
        file_index=1,
        chunk_index=2,
        chunk_start=20_000,
    )

    assert [record.kind for record in records] == ["classification", "approver"]
    assert records[0].start == 20_000
    assert all("Несуществующий" not in record.quote for record in records)


def test_renderer_never_turns_empty_or_partial_extraction_into_absence() -> None:
    coverage = DocumentDetailsCoverage(
        files_total=1,
        files_readable=1,
        source_complete=True,
        chunks_required=4,
        chunks_planned=4,
        chunks_processed=3,
        chunks_failed=1,
    )

    rendered = render_document_details([], coverage)

    assert "не означает, что их нет" in rendered
    assert "Покрытие частичное" in rendered
    assert "3 из 4" in rendered


def test_metadata_scope_separates_technical_properties_from_body_details() -> None:
    assert _document_metadata_request_scope("Покажи метаданные этого документа") == "both"
    assert _document_metadata_request_scope("Покажи технические свойства этого документа") == "technical"
    assert _document_metadata_request_scope("Покажи реквизиты этого документа") == "details"
    assert (
        _document_metadata_request_scope(
            "Покажи все технические метаданные контейнера и все видимые реквизиты этого документа"
        )
        == "both"
    )
    assert _document_metadata_request_scope("Кем подписан этот документ?") == "details"
    assert _document_metadata_request_scope("Какой гриф у этого документа?") == "details"
    assert _document_metadata_request_scope("Покажи реквизиты компании в этом документе") == ""


def test_nested_odf_metadata_redacts_this_instance_secret() -> None:
    secret = "INSTANCE-SECRET-1234567890"
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", "<office/>")
        archive.writestr(
            "meta.xml",
            f"""<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0">
 <office:meta><meta:user-defined meta:name="Ключ" meta:value-type="string">
 {secret}</meta:user-defined></office:meta></office:document-meta>""",
        )
        archive.writestr(
            "META-INF/documentsignatures.xml",
            f"""<dsig:document-signatures
 xmlns:dsig="urn:oasis:names:tc:opendocument:xmlns:digitalsignature:1.0"
 xmlns:ds="http://www.w3.org/2000/09/xmldsig#">
 <ds:Signature Id="s"><ds:KeyInfo><ds:X509Data>
 <ds:X509SubjectName>CN={secret}</ds:X509SubjectName>
 </ds:X509Data></ds:KeyInfo></ds:Signature></dsig:document-signatures>""",
        )

    metadata = DocumentExtractor(secret_values=(secret,)).extract_document_metadata(
        payload.getvalue(), "secret.odt"
    )

    assert secret not in json.dumps(metadata, ensure_ascii=False)
    assert "[секрет удалён]" in metadata["user_defined"][0]["value"]
    assert "[секрет удалён]" in metadata["signature_subjects"][0]


def test_more_than_32_odf_custom_properties_publish_total_shown_and_incomplete() -> None:
    payload = io.BytesIO()
    custom = "".join(
        f'<meta:user-defined meta:name="Поле {index}" meta:value-type="string">'
        f"Значение {index}</meta:user-defined>"
        for index in range(40)
    )
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", "<office/>")
        archive.writestr(
            "meta.xml",
            """<office:document-meta
 xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0">
 <office:meta>"""
            + custom
            + "</office:meta></office:document-meta>",
        )

    metadata = DocumentExtractor(secret_values=()).extract_document_metadata(
        payload.getvalue(), "many-properties.odt"
    )

    assert metadata["user_defined_total"] == 40
    assert metadata["user_defined_shown"] == 32
    assert len(metadata["user_defined"]) == 32
    assert metadata["technical_metadata_incomplete"] is True
    assert metadata["metadata_parse_status"] == "partial"


def test_technical_metadata_renderer_reserves_an_explicit_omission_notice() -> None:
    custom = [{"name": f"Поле {index}", "value_type": "string", "value": "X" * 1_000} for index in range(40)]
    attachment = _OwnedAttachment(
        {
            "_safe_document_metadata": {
                "filename": "large-meta.odt",
                "format": "odt",
                "metadata_schema_version": 2,
                "user_defined": custom,
                "user_defined_total": 40,
                "user_defined_shown": 32,
            }
        }
    )

    rendered = _document_metadata_answer([attachment])

    assert len(rendered) <= 16_000
    assert "Пользовательские свойства, всего: 40" in rendered
    assert "Пользовательские свойства, показано: 32" in rendered
    assert "технические метаданные показаны частично" in rendered.casefold()
    assert "в предел ответа не вошло строк" in rendered


@pytest.mark.asyncio
async def test_metadata_route_adds_only_literal_body_details_in_one_small_call(
    settings,
    storage,
) -> None:
    text = "ПРИКАЗ № 17\nДля служебного пользования\nПодписал: командир части полковник Иванов И.И."
    raw = _stored_odt(storage, text=text)
    conversation = storage.create_conversation("alice")
    storage.store_message(
        conversation["id"],
        "alice",
        "user",
        "загрузил приказ",
        metadata={
            "had_attachments": True,
            "attachment_count": 1,
            "attachment_origin": "upload",
            "conversation_attachment_raw_ids": [raw.id],
        },
    )
    llm = _DocumentDetailsLLM()
    runtime = AgentRuntime(replace(settings, llm_timeout_sec=10), storage, llm=llm)

    result = await runtime.chat(
        "alice",
        "Покажи метаданные этого документа",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        conversation_id=conversation["id"],
        enable_tools=True,
    )

    assert len(llm.calls) == 1
    assert "Технические свойства файла (как сохранено)" in result["message"]
    assert "Учебный приказ" in result["message"]
    assert "Подписант и должность" in result["message"]
    assert "полковник Иванов И.И." in result["message"]
    assert "СОВЕРШЕННО СЕКРЕТНО" not in result["message"]
    assert "проверен весь доступный извлечённый текст (1/1" in result["message"]


@pytest.mark.asyncio
async def test_large_document_details_scan_the_tail_and_publish_partial_failure(
    settings,
    storage,
) -> None:
    prefix = "A" * 20_000
    tail = "B" * 4_500 + "\nПодписал: командир части полковник Иванов И.И."
    source = _OwnedAttachment(
        {
            "filename": "большой.odt",
            "transient_text": prefix + tail,
            "extraction_success": True,
            "verification_eligible": True,
        }
    )
    llm = _DocumentDetailsLLM(fail_on_call=1)
    runtime = AgentRuntime(replace(settings, llm_timeout_sec=10), storage, llm=llm)
    context = AgentContext(
        conversation_id="conversation-details",
        user_id="alice",
        person_id="alice",
        current_attachment_present=True,
    )

    rendered = await runtime._document_content_details_answer(context, [source])  # noqa: SLF001

    assert len(llm.calls) == 2
    assert "полковник Иванов И.И." in rendered
    assert "Покрытие частичное" in rendered
    assert "обработано 1 из 2" in rendered


@pytest.mark.asyncio
async def test_container_date_is_not_presented_as_the_visible_document_date(
    settings,
    storage,
) -> None:
    raw = _stored_odt(
        storage,
        text="ПРИКАЗ № 41\nДата документа: 15.07.2026",
        filename="date-conflict.odt",
        extra_metadata={"document_date": "2013-01-02"},
    )
    llm = _DocumentDetailsLLM()
    runtime = AgentRuntime(replace(settings, llm_timeout_sec=10), storage, llm=llm)

    result = await runtime.chat(
        "alice",
        "Покажи метаданные этого документа",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[{"raw_object_id": raw.id}],
        enable_tools=True,
    )

    assert len(llm.calls) == 1
    assert "Дата в свойствах контейнера: 2013-01-02" in result["message"]
    assert "Дата: «Дата документа: 15.07.2026»" in result["message"]
    assert "Дата документа: 2013-01-02" not in result["message"]


@pytest.mark.asyncio
async def test_all_metadata_fields_for_one_document_never_becomes_all_documents_guard(
    settings,
    storage,
) -> None:
    raw = _stored_odt(
        storage,
        text=(
            "ДЛЯ СЛУЖЕБНОГО ПОЛЬЗОВАНИЯ\n"
            "Дата документа: 15.07.2026\n"
            "Подписал: командир части полковник Иванов И.И."
        ),
        extra_metadata={
            "creator": "Редактор Контейнера",
            "document_date": "2013-01-02",
            "creation_date": "2013-01-02T03:04:05Z",
        },
    )
    llm = _DocumentDetailsLLM()
    runtime = AgentRuntime(replace(settings, llm_timeout_sec=10), storage, llm=llm)

    result = await runtime.chat(
        "alice",
        "Покажи все технические метаданные контейнера и все видимые реквизиты этого документа",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[{"raw_object_id": raw.id}],
        enable_tools=True,
    )

    assert len(llm.calls) == 1
    assert "Учебный приказ" in result["message"]
    assert "Редактор Контейнера" in result["message"]
    assert "Дата в свойствах контейнера: 2013-01-02" in result["message"]
    assert "Дата: «Дата документа: 15.07.2026»" in result["message"]
    assert "Подписант и должность" in result["message"]
    assert "не могу надёжно обобщить все 1 документа" not in result["message"].casefold()


@pytest.mark.asyncio
async def test_ordinary_all_two_documents_request_keeps_missing_set_guard(settings, storage) -> None:
    raw = _stored_odt(storage, text="Единственный доступный документ")

    class NoModel:
        enabled = True
        model = "must-not-run"

        async def chat(self, *_args, **_kwargs):
            raise AssertionError("incomplete two-document set reached the model")

    runtime = AgentRuntime(settings, storage, llm=NoModel())
    result = await runtime.chat(
        "alice",
        "Обобщи все 2 документа",
        actor=ActorContext(user_id="alice", preset_key="owner", source="test"),
        attachments=[{"raw_object_id": raw.id}],
        enable_tools=True,
    )

    assert "не могу надёжно обобщить все 2 документа" in result["message"].casefold()
    assert "1 из 2" in result["message"]
