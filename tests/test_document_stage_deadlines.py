from __future__ import annotations

import io
import time
import zipfile

import pytest

from friday.documents import (
    ArchiveDeadlineReached,
    DocumentExtractor,
    DocumentResult,
)
from friday.documents._office_convert import OfficeConversionResult
from friday.documents._office_structure import _docx_part_visible_text


def _silence_metadata(extractor: DocumentExtractor) -> None:
    extractor.extract_document_metadata = lambda *_args, **_kwargs: {}  # type: ignore[method-assign]


def test_pdf_budget_does_not_shorten_office_conversion() -> None:
    extractor = DocumentExtractor(secret_values=(), parse_budget_sec=8)
    _silence_metadata(extractor)
    observed: list[float | None] = []

    def converted(
        _content: bytes,
        source_format: str,
        *,
        deadline: float | None,
    ) -> DocumentResult:
        observed.append(deadline)
        return DocumentResult("office", {"format": source_format})

    extractor._extract_converted_office = converted  # type: ignore[method-assign]  # noqa: SLF001

    result = extractor.extract(b"legacy-office", "book.xls")

    assert result.success is True
    assert observed == [None]


def test_pdf_keeps_its_dedicated_parse_budget() -> None:
    extractor = DocumentExtractor(secret_values=(), parse_budget_sec=8)
    _silence_metadata(extractor)
    observed: list[float | None] = []

    def pdf(_content: bytes, *, deadline: float | None) -> DocumentResult:
        observed.append(deadline)
        return DocumentResult("pdf", {"format": "pdf"})

    extractor._extract_pdf = pdf  # type: ignore[method-assign]  # noqa: SLF001
    started = time.monotonic()
    result = extractor.extract(b"%PDF-synthetic", "paper.pdf", "application/pdf")

    assert result.success is True
    assert observed[0] is not None
    assert started + 7.9 <= float(observed[0]) <= time.monotonic() + 8.1


def test_archive_gets_one_size_scaled_upload_deadline() -> None:
    extractor = DocumentExtractor(secret_values=(), parse_budget_sec=8)
    _silence_metadata(extractor)
    observed: list[float | None] = []

    def archive(
        _content: bytes,
        _filename: str,
        _kind: str,
        _depth: int,
        _budget: object,
        deadline: float | None,
        _password: str | None,
    ) -> DocumentResult:
        observed.append(deadline)
        return DocumentResult("archive", {"format": "zip"})

    extractor._extract_archive = archive  # type: ignore[method-assign]  # noqa: SLF001
    started = time.monotonic()
    result = extractor.extract(b"PK-synthetic", "bundle.zip")

    assert result.success is True
    assert observed[0] is not None
    assert started + 59.9 <= float(observed[0]) <= time.monotonic() + 60.1


def test_parent_deadline_still_bounds_every_document_stage() -> None:
    extractor = DocumentExtractor(secret_values=(), parse_budget_sec=8)
    _silence_metadata(extractor)
    observed: list[float | None] = []

    def converted(
        _content: bytes,
        source_format: str,
        *,
        deadline: float | None,
    ) -> DocumentResult:
        observed.append(deadline)
        return DocumentResult("office", {"format": source_format})

    extractor._extract_converted_office = converted  # type: ignore[method-assign]  # noqa: SLF001
    parent = time.monotonic() + 3

    result = extractor.extract(b"legacy-office", "book.xls", _deadline=parent)

    assert result.success is True
    assert observed == [parent]


def test_parent_deadline_bounds_archive_stage_without_extension() -> None:
    extractor = DocumentExtractor(secret_values=(), parse_budget_sec=8)
    _silence_metadata(extractor)
    observed: list[float | None] = []

    def archive(
        _content: bytes,
        _filename: str,
        _kind: str,
        _depth: int,
        _budget: object,
        deadline: float | None,
        _password: str | None,
    ) -> DocumentResult:
        observed.append(deadline)
        return DocumentResult("archive", {"format": "zip"})

    extractor._extract_archive = archive  # type: ignore[method-assign]  # noqa: SLF001
    parent = time.monotonic() + 3

    result = extractor.extract(b"PK-synthetic", "bundle.zip", _deadline=parent)

    assert result.success is True
    assert observed == [parent]


def test_expired_native_office_deadline_is_reported_explicitly() -> None:
    from docx import Document

    source = io.BytesIO()
    document = Document()
    document.add_paragraph("deadline-bound office text")
    document.save(source)
    extractor = DocumentExtractor(secret_values=(), parse_budget_sec=8)

    result = extractor.extract(
        source.getvalue(),
        "bounded.docx",
        _deadline=time.monotonic() - 1,
    )

    assert result.success is False
    assert result.error == "document_parse_deadline"
    assert result.metadata["parse_deadline_reached"] is True


def test_archive_deadline_returns_truthful_partial_instead_of_generic_failure() -> None:
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("first.txt", "first")
        archive.writestr("second.txt", "second")
    extractor = DocumentExtractor(secret_values=(), parse_budget_sec=8)

    result = extractor.extract(
        source.getvalue(),
        "bounded.zip",
        _deadline=time.monotonic() - 1,
    )

    assert result.success is True
    assert result.metadata["archive_budget_exhausted"] is True
    assert result.metadata["parse_deadline_reached"] is True


def test_expired_metadata_only_deadline_never_starts_parser() -> None:
    extractor = DocumentExtractor(secret_values=())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("metadata parser started after its request deadline")

    extractor._extract_pdf_metadata = forbidden  # type: ignore[method-assign]  # noqa: SLF001

    metadata = extractor.extract_document_metadata(
        b"%PDF-synthetic",
        "bounded.pdf",
        deadline=time.monotonic() - 1,
    )

    assert metadata == {
        "format": "pdf",
        "metadata_parse_status": "partial",
        "technical_metadata_incomplete": True,
        "parse_deadline_reached": True,
    }


def test_body_worker_does_not_start_metadata_pass_after_expiry() -> None:
    extractor = DocumentExtractor(secret_values=())

    def forbidden(*_args, **_kwargs):
        raise AssertionError("second parser pass started after its request deadline")

    extractor.extract_document_metadata = forbidden  # type: ignore[method-assign]

    result = extractor.extract(
        b"body still has a deterministic cheap parser",
        "bounded.txt",
        _deadline=time.monotonic() - 1,
    )

    assert result.success is True
    assert result.metadata["parse_deadline_reached"] is True


def test_body_stage_deadline_flag_skips_second_metadata_pass() -> None:
    extractor = DocumentExtractor(secret_values=(), parse_budget_sec=8)

    extractor._extract_pdf = lambda *_args, **_kwargs: DocumentResult(  # type: ignore[method-assign]  # noqa: SLF001, E501
        "partial body",
        {"format": "pdf", "parse_deadline_reached": True},
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("metadata parser restarted work after body-stage deadline")

    extractor.extract_document_metadata = forbidden  # type: ignore[method-assign]

    result = extractor.extract(b"%PDF-synthetic", "bounded.pdf", "application/pdf")

    assert result.success is True
    assert result.metadata["metadata_parse_status"] == "partial"
    assert result.metadata["technical_metadata_incomplete"] is True
    assert result.metadata["parse_deadline_reached"] is True


def test_pdf_metadata_checks_deadline_after_reader_construction(monkeypatch) -> None:
    import pypdf

    class SlowReader:
        def __init__(self, *_args, **_kwargs) -> None:
            time.sleep(0.02)
            self.is_encrypted = False

    monkeypatch.setattr(pypdf, "PdfReader", SlowReader)
    extractor = DocumentExtractor(secret_values=())

    metadata = extractor.extract_document_metadata(
        b"%PDF-synthetic",
        "bounded.pdf",
        deadline=time.monotonic() + 0.005,
    )

    assert metadata["metadata_parse_status"] == "partial"
    assert metadata["technical_metadata_incomplete"] is True
    assert metadata["parse_deadline_reached"] is True


def test_ooxml_rebuild_passes_parent_deadline_to_member_stream(monkeypatch) -> None:
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            b"""<?xml version="1.0" encoding="UTF-8"?>
            <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
              <Override PartName="/word/document.xml"
                ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"/>
            </Types>""",
        )
        archive.writestr("word/document.xml", b"<document/>")
    extractor = DocumentExtractor(secret_values=())
    deadline = time.monotonic() + 10
    observed: list[float | None] = []

    def bounded_read(_stream, _limit: int, *, deadline: float | None = None) -> bytes:
        observed.append(deadline)
        raise ArchiveDeadlineReached("synthetic inherited deadline")

    monkeypatch.setattr(extractor, "_read_stream_limited", bounded_read)

    with pytest.raises(ArchiveDeadlineReached):
        extractor._normalized_ooxml_main_type(  # noqa: SLF001
            source.getvalue(),
            main_part="/word/document.xml",
            canonical_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
            ),
            alias_types=frozenset(
                {"application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml"}
            ),
            deadline=deadline,
        )

    assert observed == [deadline]


def test_docx_auxiliary_iterparse_honours_expired_deadline() -> None:
    text, deadline_reached = _docx_part_visible_text(
        io.BytesIO(b"<root><t>must not be parsed</t></root>"),
        limit=1_000,
        deadline=time.monotonic() - 1,
    )

    assert text == ""
    assert deadline_reached is True


def test_nested_deadline_is_propagated_to_parent_archive(monkeypatch) -> None:
    source = io.BytesIO()
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("nested.pdf", b"%PDF-synthetic")
    extractor = DocumentExtractor(secret_values=())

    monkeypatch.setattr(
        extractor,
        "_member_preview",
        lambda *_args, **_kwargs: ("", False, True),
    )

    result = extractor.extract(source.getvalue(), "nested.zip")

    assert result.success is True
    assert result.metadata["archive_budget_exhausted"] is True
    assert result.metadata["parse_deadline_reached"] is True


def test_plain_7z_decoder_deadline_returns_truthful_partial(monkeypatch) -> None:
    import py7zr

    source = io.BytesIO()
    with py7zr.SevenZipFile(source, mode="w") as archive:
        archive.writestr(b"bounded", "note.txt")

    def deadline_write(*_args, **_kwargs):
        raise ArchiveDeadlineReached("synthetic decoder deadline")

    monkeypatch.setattr("friday.documents._Bounded7zWriter.write", deadline_write)

    result = DocumentExtractor(secret_values=()).extract(source.getvalue(), "bounded.7z")

    assert result.success is True
    assert result.metadata["archive_budget_exhausted"] is True
    assert result.metadata["parse_deadline_reached"] is True


def test_encrypted_7z_validation_deadline_fails_closed(monkeypatch) -> None:
    import py7zr

    source = io.BytesIO()
    with py7zr.SevenZipFile(source, mode="w", password="correct-password") as archive:
        archive.writestr(b"bounded", "note.txt")

    def deadline_write(*_args, **_kwargs):
        raise ArchiveDeadlineReached("synthetic decoder deadline")

    monkeypatch.setattr("friday.documents._Bounded7zWriter.write", deadline_write)

    result = DocumentExtractor(secret_values=()).extract(
        source.getvalue(),
        "bounded.7z",
        archive_password="correct-password",
    )

    assert result.success is False
    assert result.error == "document_parse_deadline"
    assert result.metadata["parse_deadline_reached"] is True


def test_expired_rar_member_deadline_does_not_spawn_decoder(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("RAR decoder spawned after its deadline")

    monkeypatch.setattr("friday.documents.subprocess.Popen", forbidden)

    with pytest.raises(ArchiveDeadlineReached):
        DocumentExtractor._read_rar_member_with_tool(  # noqa: SLF001
            "/unused/input.rar",
            "note.txt",
            tool="/unused/unrar",
            password=None,
            limit=1_000,
            deadline=time.monotonic() - 1,
        )


def test_libreoffice_deadline_uses_document_deadline_contract(monkeypatch) -> None:
    extractor = DocumentExtractor(secret_values=())

    monkeypatch.setattr(
        "friday.documents._office_convert.convert_legacy_office",
        lambda *_args, **_kwargs: OfficeConversionResult(
            target_format="xlsx",
            error="libreoffice_deadline_reached",
        ),
    )

    result = extractor._extract_converted_office(  # noqa: SLF001
        b"legacy office",
        "xls",
        deadline=time.monotonic() + 10,
    )

    assert result.success is False
    assert result.error == "libreoffice_deadline_reached"
    assert result.metadata["parse_deadline_reached"] is True
