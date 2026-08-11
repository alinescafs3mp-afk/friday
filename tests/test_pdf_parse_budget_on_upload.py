"""Загрузка файла соблюдает срок разбора PDF — и говорит, когда в него упёрлась.

Веб-путь получил срок в 6d5f688; путь загрузки файла его не получил, потому что
`CoreMixin.__init__` строит свой `DocumentExtractor` и про `parse_budget_sec` не
знал. Замерено на стенде: PDF в 41 КБ — 250 страниц, у каждой content stream из
40 000 текстовых операторов — занимал поток пула на **35 секунд**; тот же файл со
сроком 8 с отдаёт частичный текст за 8.3 с. Потолок страниц (250) такой файл не
ловит: дорога не каждая страница, а разбор одной, и стоимость растёт с числом
операторов, а не с размером файла.

Вторая половина, и она важнее первой: оборванный разбор нельзя молча выдать за
целый документ. Пометка `parse_deadline_reached` обязана дойти до трёх мест —
метаданных хранимого объекта, ответа `ingest_file` и предпросмотра, — потому что
`success=True` с частичным текстом внешне неотличим от полного разбора.
"""

from __future__ import annotations

import json

import pytest

from friday.documents import DocumentResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph

_PDF = b"%PDF-1.4\n%%EOF\n"


def _json(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


class _TruncatingExtractor:
    """Разбор, оборванный по сроку: текст ЕСТЬ, но он не весь."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def extract(self, *args, **kwargs):
        del args, kwargs
        return DocumentResult(
            "Договор аренды. " * 60,
            {
                "format": "pdf",
                "pages_read": 12,
                "parse_deadline_reached": True,
                "document_date": "2024-03-11",
            },
        )

    def extract_visual_assets(self, *args, **kwargs):
        del args, kwargs
        return {"success": False, "text": ""}


class _GenericTextBudgetExtractor(_TruncatingExtractor):
    """A non-page parser stopped at its generic extracted-text ceiling."""

    def extract(self, *args, **kwargs):
        del args, kwargs
        return DocumentResult(
            "Начало большой презентации.",
            {"format": "pptx", "extraction_truncated": True},
        )


class _EmptyVisualExtractor:
    """Native parser opened the carrier, but found no text layer."""

    def extract(self, *args, **kwargs):
        del args, kwargs
        return DocumentResult("", {"format": "visual", "total_pages": 1})


def test_the_upload_extractor_is_built_with_a_parse_budget(settings, storage) -> None:
    """Мутация: убрать `parse_budget_sec` из `CoreMixin.__init__` — тест краснеет."""
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    budget = pipeline._doc_extractor.parse_budget_sec  # noqa: SLF001
    assert budget is not None, "путь загрузки разбирает PDF без срока — поток занят сколько попросит файл"
    assert budget == pytest.approx(settings.pdf_parse_budget_sec)


def test_a_reused_extractor_does_not_outlive_its_own_deadline() -> None:
    """Срок ставится на каждый разбор, а не один раз на жизнь объекта.

    `IngestionPipeline` строит `DocumentExtractor` ОДИН раз на процесс. Срок,
    вычисленный в конструкторе (`self.deadline = monotonic() + budget`), сгорал бы
    через 8 секунд после старта бэкенда — и дальше КАЖДЫЙ PDF читался бы на ноль
    страниц, необратимо и молча. Мутация: вернуть вычисление срока в `__init__` —
    тест краснеет.

    Второй повод для той же правки: `ingest_file` разбирает в `asyncio.to_thread`,
    поэтому два файла делят один экстрактор в двух потоках — общий срок один
    затирал бы другому.
    """
    import io
    import time

    from pypdf import PdfWriter

    from friday.documents import DocumentExtractor

    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    pdf = buf.getvalue()

    extractor = DocumentExtractor(parse_budget_sec=0.5)
    assert extractor.extract(pdf, "a.pdf", "application/pdf").metadata["pages_read"] == 3

    time.sleep(0.7)  # дольше бюджета — как бэкенд, проживший минуту
    second = extractor.extract(pdf, "b.pdf", "application/pdf")
    assert second.metadata["pages_read"] == 3, (
        "экстрактор пережил свой срок: следующий PDF разобран на "
        f"{second.metadata['pages_read']} страниц из 3"
    )
    assert not second.metadata.get("parse_deadline_reached")


def test_the_budget_is_one_knob_for_both_intake_paths(settings) -> None:
    """Веб-путь и загрузка не могут расходиться в этом сроке."""
    assert settings.pdf_parse_budget_sec > 0
    from friday.web_surfer import WebSurfer

    surfer = WebSurfer(settings)
    assert surfer.settings.pdf_parse_budget_sec == settings.pdf_parse_budget_sec


@pytest.mark.asyncio
async def test_a_truncated_parse_is_recorded_on_the_stored_object(settings, storage, monkeypatch) -> None:
    """Частичность — свойство хранимого документа, а не подробность одного ответа."""
    import friday.ingestion._core as core

    monkeypatch.setattr(core, "DocumentExtractor", _TruncatingExtractor)
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))

    result = await pipeline.ingest_file(
        "alice", None, _PDF, filename="lease.pdf", mime_type="application/pdf", source_ref="upload:lease"
    )

    extraction = result["extraction"]
    assert extraction["parse_deadline_reached"] is True, (
        "загрузивший получил «файл принят» и ничего о том, что принято только начало"
    )
    assert extraction["parse_pages_read"] == 12

    stored = _json(storage.get_raw_object(result["raw_object_id"], "alice")["metadata_json"])
    assert stored.get("parse_deadline_reached") is True, (
        "в хранилище частичный документ неотличим от целого — для поиска и для модели тоже"
    )
    assert stored.get("parse_pages_read") == 12
    # Соседнее поле не должно пострадать: дата документа читается из того же словаря.
    assert stored.get("document_date") == "2024-03-11"


@pytest.mark.asyncio
async def test_a_complete_parse_does_not_claim_truncation(settings, storage) -> None:
    """Обратная сторона: обычный файл не помечается частичным."""
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    result = await pipeline.ingest_file(
        "alice",
        None,
        "Заметка о встрече с подрядчиком по кровле, смета согласована.".encode(),
        filename="note.txt",
        mime_type="text/plain",
        source_ref="upload:note",
    )
    assert result["extraction"]["parse_deadline_reached"] is False
    stored = _json(storage.get_raw_object(result["raw_object_id"], "alice")["metadata_json"])
    # Versioned extraction receipts preserve the complete first-upload verdict
    # for exact replay.  An explicit false is still a proven complete parse,
    # never a truncation claim or an absent/unknown legacy value.
    assert stored["extraction_receipt_version"] == 1
    assert stored["parse_deadline_reached"] is False


@pytest.mark.asyncio
async def test_the_transient_preview_separates_its_two_truncations(settings, storage, monkeypatch) -> None:
    """`text_truncated` — короткий предпросмотр; обрыв разбора — это другое.

    Читатель, которому показали только первое, решит, что полный текст есть и его
    просто не показали целиком.
    """
    import friday.ingestion._core as core

    monkeypatch.setattr(core, "DocumentExtractor", _TruncatingExtractor)
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))

    preview = await pipeline.inspect_file_transient(_PDF, filename="lease.pdf", mime_type="application/pdf")
    assert preview["parse_deadline_reached"] is True
    assert preview["parse_pages_read"] == 12
    assert preview["text_truncated"] is False, "предпросмотр влез целиком — обрезки предпросмотра тут нет"


@pytest.mark.asyncio
async def test_generic_extractor_loss_reaches_the_transient_attachment(
    settings, storage, monkeypatch
) -> None:
    import friday.ingestion._core as core

    monkeypatch.setattr(core, "DocumentExtractor", _GenericTextBudgetExtractor)
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))

    preview = await pipeline.inspect_file_transient(
        b"synthetic-presentation",
        filename="large.pptx",
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )

    assert preview["_runtime_source_truncated"] is True
    assert preview["text_truncated"] is True
    assert preview["parse_deadline_reached"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "mime_type"),
    [
        ("scan.jpg", "image/jpeg"),
        ("scan.pdf", "application/pdf"),
    ],
)
async def test_no_save_visual_attachment_uses_vision_as_ephemeral_advisory_text(
    settings,
    storage,
    monkeypatch,
    filename: str,
    mime_type: str,
) -> None:
    """No-save must have the same eyes while keeping OCR out of evidence."""

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    pipeline._doc_extractor = _EmptyVisualExtractor()  # noqa: SLF001
    calls = []
    visible_text = "TRANSIENT-VISION-BODY-551: synthetic scan"

    async def visual_extract(file_content, *, filename, mime_type):
        calls.append((file_content, filename, mime_type))
        return {
            "success": True,
            "error": "",
            "confidence": 0.91,
            "text": visible_text,
            "title": "Synthetic scan",
            "summary": visible_text,
            "entities": [],
            "evidence": [{"asset_id": "A1", "quote": "synthetic scan", "claim": visible_text}],
            "warnings": [],
            "pages_total": 1,
            "pages_read": 1,
            "assets": [],
            "advisory_only": True,
        }

    monkeypatch.setattr(pipeline, "_extract_visual_document", visual_extract)
    source = b"synthetic-no-save-visual-bytes"
    preview = await pipeline.inspect_file_transient(
        source,
        filename=filename,
        mime_type=mime_type,
    )

    assert calls == [(source, filename, mime_type)]
    assert preview["text_preview"] == visible_text
    assert preview["_runtime_source_text"] == visible_text
    assert preview["extraction_success"] is True
    assert preview["vision_used"] is True
    assert preview["vision_pages_read"] == 1
    assert preview["vision_pages_total"] == 1
    assert preview["advisory_only"] is True
    assert preview["verification_eligible"] is False
    assert not any(path.is_file() for path in settings.files_dir.rglob("*"))
    assert storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 0
