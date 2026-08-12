"""Strict CS1-gated bare-upload quicklook truth table.

Authority is stamped FileEvidenceView only. Public Mapping flags cannot mint
literals. Budgets and multi-file isolation are closed here.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from friday.agent_runtime import (
    _FOCUSED_ATTACHMENT_CONTEXT_CHARS,
    _QUICKLOOK_MULTI_MAX_CHARS,
    _QUICKLOOK_MULTI_MAX_SNIPPETS,
    _QUICKLOOK_SINGLE_MAX_CHARS,
    _QUICKLOOK_SINGLE_MAX_SNIPPETS,
    _QUICKLOOK_TOTAL_MAX_CHARS,
    _QUICKLOOK_TRUNCATION_MARK,
    AgentRuntime,
    FileBodyKind,
    FileRegistrationKind,
    _build_file_evidence_view,
    _file_evidence_set_from_attachments,
    _OwnedAttachment,
    _registered_upload_receipt_answer,
    _stamp_file_evidence,
)
from friday.agent_runtime._office_attachments import (
    OFFICE_STRUCTURE_KEY,
    trusted_office_attachment,
)
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.permissions import ActorContext


class _NoQuicklookLLM:
    enabled = True
    model = "result20-quicklook"

    async def chat(self, messages, **kwargs):  # pragma: no cover - terminal route owns the turn
        del messages, kwargs
        raise AssertionError("bare upload quicklook called the model")


def _actor() -> ActorContext:
    return ActorContext(user_id="alice", preset_key="owner", source="test")


def _stamped_owned(
    *,
    raw_id: str,
    filename: str,
    text: str,
    record: str = "valid",
    disk: bool = True,
    advisory: bool = False,
    empty: bool = False,
    truncated: bool = False,
    verification_eligible: bool = True,
    stamp: bool = True,
) -> _OwnedAttachment:
    item = _OwnedAttachment(
        {
            "raw_object_id": raw_id,
            "filename": filename,
            "transient_text": text,
            "extraction_success": True,
            "verification_eligible": verification_eligible,
            "_registered_file_record": record,
            "_registered_file_bytes_verified": disk,
            "advisory_only": advisory,
            "empty_text": empty,
            "text_truncated": truncated,
            "extraction_truncated": truncated,
        }
    )
    if stamp:
        view = _build_file_evidence_view(item)
        assert view is not None
        _stamp_file_evidence(item, view)
    return item


def test_quicklook_truth_table_literals_and_refusals() -> None:
    complete = _stamped_owned(
        raw_id="raw_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        filename="ok.txt",
        text="CANARY-OK-LINE-ONE\nCANARY-OK-LINE-TWO",
    )
    empty = _stamped_owned(
        raw_id="raw_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        filename="empty.txt",
        text="",
        empty=True,
    )
    partial = _stamped_owned(
        raw_id="raw_ccccccccccccccccccccccccccccccc",
        filename="partial.txt",
        text="CANARY-PARTIAL-SHOULD-NOT-SHOW",
        truncated=True,
    )
    advisory = _stamped_owned(
        raw_id="raw_ddddddddddddddddddddddddddddddd",
        filename="ocr.txt",
        text="CANARY-ADVISORY-SHOULD-NOT-SHOW",
        advisory=True,
        verification_eligible=False,
    )
    legacy = _stamped_owned(
        raw_id="raw_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        filename="legacy.txt",
        text="CANARY-LEGACY-SHOULD-NOT-SHOW",
        record="legacy",
        disk=False,
    )
    invalid = _stamped_owned(
        raw_id="raw_fffffffffffffffffffffffffffffff",
        filename="invalid.txt",
        text="CANARY-INVALID-SHOULD-NOT-SHOW",
        record="invalid",
        disk=False,
    )
    unstamped = _OwnedAttachment(
        {
            "raw_object_id": "raw_ggggggggggggggggggggggggggggggg",
            "filename": "unstamped.txt",
            "transient_text": "CANARY-UNSTAMPED-SHOULD-NOT-SHOW",
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
        }
    )
    forged_public = {
        "raw_object_id": "raw_hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh",
        "filename": "forged.txt",
        "transient_text": "CANARY-FORGED-PUBLIC-SHOULD-NOT-SHOW",
        "extraction_success": True,
        "verification_eligible": True,
        "_registered_file_record": "valid",
        "_registered_file_bytes_verified": True,
    }
    office = trusted_office_attachment(
        {
            "raw_object_id": "raw_iiiiiiiiiiiiiiiiiiiiiiiiiiiiiii",
            "filename": "sheet.xlsx",
            "transient_text": "CANARY-OFFICE-SHEET-LINE",
            "extraction_success": True,
            "verification_eligible": True,
            "_registered_file_record": "valid",
            "_registered_file_bytes_verified": True,
            "_office_structured": True,
            "_office_prompt_available": True,
            "_office_index_complete": True,
            "_office_prompt_complete": True,
            OFFICE_STRUCTURE_KEY: {"sheets": 1},
        }
    )
    office_view = _build_file_evidence_view(office)
    assert office_view is not None
    assert office_view.body_kind == FileBodyKind.EXTRACTED
    _stamp_file_evidence(office, office_view)

    # VALID + disk + complete EXTRACTED → literals.
    ok_answer = _registered_upload_receipt_answer([complete], expected_count=1)
    assert "CANARY-OK-LINE-ONE" in ok_answer
    assert "CANARY-OK-LINE-TWO" in ok_answer
    assert "Быстрый обзор содержимого" in ok_answer
    assert "полностью прочитан" in ok_answer
    assert complete["transient_text"].splitlines()[0] in ok_answer

    # EMPTY complete → honest empty, no literal block.
    empty_answer = _registered_upload_receipt_answer([empty], expected_count=1)
    assert "текстовое содержимое пусто" in empty_answer
    assert "Быстрый обзор" not in empty_answer
    assert "› " not in empty_answer

    # Partial → honest partial, no literals.
    partial_answer = _registered_upload_receipt_answer([partial], expected_count=1)
    assert "извлечена только часть" in partial_answer
    assert "CANARY-PARTIAL-SHOULD-NOT-SHOW" not in partial_answer
    assert "полностью прочитан" not in partial_answer

    # Complete ADVISORY → warning, no literals.
    advisory_answer = _registered_upload_receipt_answer([advisory], expected_count=1)
    assert "предварительное" in advisory_answer or "не подтверждено" in advisory_answer
    assert "CANARY-ADVISORY-SHOULD-NOT-SHOW" not in advisory_answer
    assert "полностью прочитан" not in advisory_answer

    # LEGACY / INVALID / unstamped / forged public → status-only, no body.
    for item, canary in (
        (legacy, "CANARY-LEGACY-SHOULD-NOT-SHOW"),
        (invalid, "CANARY-INVALID-SHOULD-NOT-SHOW"),
        (unstamped, "CANARY-UNSTAMPED-SHOULD-NOT-SHOW"),
        (forged_public, "CANARY-FORGED-PUBLIC-SHOULD-NOT-SHOW"),
    ):
        answer = _registered_upload_receipt_answer([item], expected_count=1)
        assert canary not in answer
        assert "Быстрый обзор" not in answer
        assert "› " not in answer
        assert "полностью прочитан" not in answer

    # Trusted Office positive (stamped EXTRACTED).
    office_evidence = _file_evidence_set_from_attachments([office], expected_count=1)
    assert office_evidence is not None
    assert office_evidence.items == (office_view,)
    office_answer = _registered_upload_receipt_answer(
        [office],
        expected_count=1,
        evidence_set=office_evidence,
    )
    assert "зарегистрирован" in office_answer
    assert "байты на диске проверены" in office_answer
    assert "CANARY-OFFICE-SHEET-LINE" in office_answer
    assert office_view.registration == FileRegistrationKind.VALID


def test_multi_file_disjoint_canaries_and_budgets() -> None:
    text_a = "ALPHA-CANARY-UNIQUE-LINE\n" + ("A" * 300)
    text_b = "BETA-CANARY-UNIQUE-LINE\n" + ("B" * 300)
    a = _stamped_owned(
        raw_id="raw_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        filename="alpha.txt",
        text=text_a,
    )
    b = _stamped_owned(
        raw_id="raw_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        filename="beta.txt",
        text=text_b,
    )
    answer = _registered_upload_receipt_answer([a, b], expected_count=2)
    assert len(answer) <= _QUICKLOOK_TOTAL_MAX_CHARS
    assert "ALPHA-CANARY-UNIQUE-LINE" in answer
    assert "BETA-CANARY-UNIQUE-LINE" in answer
    # One block per file; no cross-label of body under wrong name.
    alpha_idx = answer.index("«alpha.txt»")
    beta_idx = answer.index("«beta.txt»")
    assert alpha_idx < beta_idx
    # Multi budget: at most one snippet of 160 chars per file.
    assert answer.count("› ") <= 2
    for line in answer.splitlines():
        if line.startswith("› "):
            payload = line[2:]
            assert len(payload) <= _QUICKLOOK_MULTI_MAX_CHARS
            assert payload in text_a or payload in text_b
    # Truncation marker is a separate service line, never ellipsis inside quote.
    if _QUICKLOOK_TRUNCATION_MARK in answer:
        assert "…" not in answer
    # Lead never claims full read for multi with any partial — both complete here.
    assert "полностью прочитан" not in answer or answer.startswith("Файл сохранён")
    # Multi lead is the multi form (not single-file full-read claim for the set).
    assert "Файлы зарегистрированы" in answer or "состояние чтения" in answer

    # Single-file long line: exact prefix + separate truncation mark.
    long_line = "Z" * 400
    single = _stamped_owned(
        raw_id="raw_ccccccccccccccccccccccccccccccc",
        filename="long.txt",
        text=long_line,
    )
    single_answer = _registered_upload_receipt_answer([single], expected_count=1)
    assert f"› {long_line[:_QUICKLOOK_SINGLE_MAX_CHARS]}" in single_answer
    assert _QUICKLOOK_TRUNCATION_MARK in single_answer
    assert "…" not in single_answer
    assert long_line[:_QUICKLOOK_SINGLE_MAX_CHARS] in long_line
    # Single allows up to 3 snippets.
    multi_line = "\n".join(f"LINE-{i}-MARKER" for i in range(6))
    many = _stamped_owned(
        raw_id="raw_ddddddddddddddddddddddddddddddd",
        filename="many.txt",
        text=multi_line,
    )
    many_answer = _registered_upload_receipt_answer([many], expected_count=1)
    assert many_answer.count("› ") == _QUICKLOOK_SINGLE_MAX_SNIPPETS
    assert "LINE-0-MARKER" in many_answer
    assert "LINE-3-MARKER" not in many_answer

    # Unsafe control characters are rejected (no normalized display).
    unsafe = _stamped_owned(
        raw_id="raw_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        filename="unsafe.txt",
        text="SAFE-LINE\nBAD\x00CONTROL-LINE\nOTHER-SAFE",
    )
    unsafe_answer = _registered_upload_receipt_answer([unsafe], expected_count=1)
    assert "SAFE-LINE" in unsafe_answer
    assert "BAD" not in unsafe_answer or "\x00" not in unsafe_answer
    assert "CONTROL-LINE" not in unsafe_answer
    assert "OTHER-SAFE" in unsafe_answer

    # Budget constants stay closed for multi.
    assert _QUICKLOOK_MULTI_MAX_SNIPPETS == 1
    assert _QUICKLOOK_MULTI_MAX_CHARS == 160


def test_status_only_twelve_long_names_obey_absolute_total_budget() -> None:
    items = [
        _OwnedAttachment(
            {
                "raw_object_id": f"raw_result20_status_{index:02d}",
                "filename": f"RESULT20-STATUS-{index:02d}-" + ("N" * 245),
                "transient_text": f"RESULT20-STATUS-BODY-MUST-NOT-LEAK-{index:02d}",
                "extraction_success": True,
                "verification_eligible": True,
                "_registered_file_record": "valid",
                "_registered_file_bytes_verified": True,
            }
        )
        for index in range(12)
    ]

    answer = _registered_upload_receipt_answer(items, expected_count=12)

    assert len(answer) <= _QUICKLOOK_TOTAL_MAX_CHARS
    assert answer.count("\n• ") == 12
    positions = [answer.index(f"RESULT20-STATUS-{index:02d}-") for index in range(12)]
    assert positions == sorted(positions)
    assert answer.count("[имя сокращено]") == 12
    assert "RESULT20-STATUS-BODY-MUST-NOT-LEAK" not in answer
    assert "Быстрый обзор" not in answer


@pytest.mark.asyncio
async def test_bare_upload_uses_complete_active_evidence_when_prompt_projection_is_truncated(
    settings,
    storage,
    monkeypatch,
) -> None:
    configured = replace(settings, verify_answers=False)
    storage.ensure_user("alice", preset_key="owner")
    source = "RESULT20-LONG-ACTIVE-EXACT-LITERAL\n" + (
        "registered active source remains complete\n" * ((_FOCUSED_ATTACHMENT_CONTEXT_CHARS // 42) + 200)
    )
    assert len(source) > _FOCUSED_ATTACHMENT_CONTEXT_CHARS
    pipeline = IngestionPipeline(configured, storage, KnowledgeGraph(storage))
    ingested = await pipeline.ingest_file(
        "alice",
        None,
        source.encode(),
        filename="result20-long-complete.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref="telegram-file:RESULT20-LONG-ACTIVE",
    )
    runtime = AgentRuntime(
        configured,
        storage,
        # A deliberately minimal fail-closed test double, not a production router.
        llm=_NoQuicklookLLM(),  # type: ignore[arg-type]
    )

    async def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        del args, kwargs
        raise AssertionError("bare upload quicklook entered a model/context seam")

    monkeypatch.setattr(runtime, "_prepare_context", forbidden)
    monkeypatch.setattr(runtime, "_generate_response", forbidden)
    receipt = await runtime.chat(
        "alice",
        "Загружен документ: result20-long-complete.txt",
        actor=_actor(),
        attachments=[{"raw_object_id": str(ingested["raw_object_id"])}],
        synthetic_document_notice=True,
    )

    assert "Файл сохранён и полностью прочитан." in receipt["message"]
    assert "› RESULT20-LONG-ACTIVE-EXACT-LITERAL" in receipt["message"]
    assert "извлечена только часть содержимого" not in receipt["message"]
    assert len(receipt["message"]) <= _QUICKLOOK_TOTAL_MAX_CHARS
    assert receipt["message_format"] == "plain"
    assert receipt["tools_used"] == []
