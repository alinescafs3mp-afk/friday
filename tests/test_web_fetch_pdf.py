"""Web fetch accepts PDF when DocumentExtractor yields real text (G22 / PROPOSALS №3)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

import jericho.web_surfer as web_surfer_module
from jericho.web_surfer import WebSurfer

# Hand-rolled one-page PDF with a Type1 Helvetica string. DocumentExtractor/pypdf
# already recovers MARKER-PDF-42 from this fixture (checked before enabling the
# allowlist). Not a full xref-perfect document — pypdf is tolerant.
_PDF_WITH_MARKER = b"""%PDF-1.4
1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj
2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj
3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>endobj
4 0 obj<< /Length 68 >>stream
BT /F1 12 Tf 50 100 Td (MARKER-PDF-42 hello world sentence) Tj ET
endstream
endobj
5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000266 00000 n 
0000000384 00000 n 
trailer<< /Size 6 /Root 1 0 R >>
startxref
459
%%EOF
"""

_BLANK_PDF = b"""%PDF-1.4
1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj
2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj
3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>endobj
4 0 obj<< /Length 0 >>stream
endstream
endobj
xref
0 5
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000208 00000 n 
trailer<< /Size 5 /Root 1 0 R >>
startxref
258
%%EOF
"""


class _FakeResponse:
    def __init__(self, content_type: str, status_code: int = 200) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.encoding = "utf-8"


@pytest.mark.asyncio
async def test_web_fetch_extracts_marker_from_pdf(settings) -> None:
    """Mutation: drop application/pdf from the allowlist — this test must go red."""
    surfer = WebSurfer(replace(settings, web_allow_private_networks=True))

    async def fake_request(_url: str):
        return _PDF_WITH_MARKER, _FakeResponse("application/pdf"), "http://example.invalid/doc.pdf"

    surfer._request_bytes = fake_request  # noqa: SLF001
    try:
        result = await surfer.fetch("http://example.invalid/doc.pdf")
    finally:
        await surfer.close()

    assert result.error == ""
    assert result.status_code == 200
    assert "MARKER-PDF-42" in result.text
    assert result.text_length == len(result.text)


@pytest.mark.asyncio
async def test_web_fetch_reports_empty_scanned_pdf_honestly(settings) -> None:
    surfer = WebSurfer(replace(settings, web_allow_private_networks=True))

    async def fake_request(_url: str):
        return _BLANK_PDF, _FakeResponse("application/pdf"), "http://example.invalid/blank.pdf"

    surfer._request_bytes = fake_request  # noqa: SLF001
    try:
        result = await surfer.fetch("http://example.invalid/blank.pdf")
    finally:
        await surfer.close()

    assert result.text == ""
    assert result.error
    assert "empty" in result.error.casefold() or "scanned" in result.error.casefold()


@pytest.mark.asyncio
async def test_web_fetch_pdf_parse_timeout_is_not_network_timeout(settings, monkeypatch) -> None:
    monkeypatch.setattr(web_surfer_module, "_PDF_PARSE_BUDGET", 0.05)

    def slow_parse(
        content: bytes, *, max_text_chars: int, parse_budget_sec: float | None = None
    ) -> tuple[str, str, str]:
        # Подмена нарочно ИГНОРИРУЕТ внутренний срок: этот тест про внешний
        # предохранитель — страница, которая молотит дольше всего бюджета и
        # изнутри не прерывается ничем.
        del content, max_text_chars, parse_budget_sec
        import time

        time.sleep(0.5)
        return "should-not-appear", "", ""

    surfer = WebSurfer(replace(settings, web_allow_private_networks=True))
    surfer._extract_pdf_text = slow_parse  # type: ignore[method-assign]

    async def fake_request(_url: str):
        return _PDF_WITH_MARKER, _FakeResponse("application/pdf"), "http://example.invalid/slow.pdf"

    surfer._request_bytes = fake_request  # noqa: SLF001
    try:
        started = asyncio.get_running_loop().time()
        result = await surfer.fetch("http://example.invalid/slow.pdf")
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        await surfer.close()

    assert elapsed < 2.0
    assert result.error == "parse_timeout"
    assert result.text == ""


@pytest.mark.asyncio
async def test_web_fetch_html_still_works_after_pdf_allowlist(settings) -> None:
    html = b"<html><head><title>T</title></head><body><p>hello html body</p></body></html>"
    surfer = WebSurfer(replace(settings, web_allow_private_networks=True))

    async def fake_request(_url: str):
        return html, _FakeResponse("text/html; charset=utf-8"), "http://example.invalid/page"

    surfer._request_bytes = fake_request  # noqa: SLF001
    try:
        result = await surfer.fetch("http://example.invalid/page")
    finally:
        await surfer.close()

    assert result.error == ""
    assert "hello html body" in result.text
    assert result.title == "T"


def test_pdf_is_on_the_content_type_allowlist() -> None:
    assert "application/pdf" in web_surfer_module._ALLOWED_CONTENT_TYPES


def _blank_pdf(pages: int) -> bytes:
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_the_parse_budget_stops_the_work_itself_not_only_the_waiting() -> None:
    """Срок обязан резать САМ разбор, между страницами.

    `asyncio.timeout` вокруг `to_thread` освобождает вызывающего, но поток
    продолжает молотить: один PDF с патологической страницей занимал поток
    общего пула до конца процесса, и каждая повторная попытка модели (research
    стартует до восьми загрузок разом) добавляла ещё один. Пул общий со всей
    фоновой работой, которая уходит с event loop.

    Стенд: пять страниц, каждая «читается» по 0.2 с, бюджет 0.3 с. Без проверки
    срока прочитались бы все пять (~1 с).

    Мутация: убрать проверку `self.deadline` в цикле по страницам — тест обязан
    покраснеть и по времени, и по числу прочитанных страниц.
    """
    import time

    import pypdf

    from jericho.documents import DocumentExtractor

    original = pypdf._page.PageObject.extract_text

    def slow_extract(self, *args, **kwargs):
        time.sleep(0.2)
        return original(self, *args, **kwargs)

    pypdf._page.PageObject.extract_text = slow_extract  # type: ignore[method-assign]
    try:
        started = time.monotonic()
        result = DocumentExtractor(parse_budget_sec=0.3).extract(_blank_pdf(5), "slow.pdf", "application/pdf")
        elapsed = time.monotonic() - started
    finally:
        pypdf._page.PageObject.extract_text = original  # type: ignore[method-assign]

    assert elapsed < 0.9, f"разбор шёл {elapsed:.2f} с — срок не останавливает работу, только ожидание"
    assert result.metadata.get("parse_deadline_reached") is True, "остановка по сроку не названа"
    assert int(result.metadata.get("pages_read") or 0) < 5, "прочитаны все страницы, несмотря на срок"


def test_a_pdf_without_a_budget_is_parsed_to_the_end() -> None:
    """Обратная сторона: без срока поведение прежнее — иначе правка не «ввела
    предохранитель», а обрезала разбор всем подряд."""
    from jericho.documents import DocumentExtractor

    result = DocumentExtractor().extract(_blank_pdf(3), "plain.pdf", "application/pdf")
    assert result.metadata.get("parse_deadline_reached") is None
    assert int(result.metadata.get("pages_read") or 0) == 3
