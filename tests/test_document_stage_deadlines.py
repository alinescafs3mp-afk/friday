from __future__ import annotations

import io
import time
import zipfile

from friday.documents import DocumentExtractor, DocumentResult


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
