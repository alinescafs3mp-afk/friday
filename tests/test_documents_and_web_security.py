from __future__ import annotations

import io
import zipfile

import pytest

from friday.documents import ArchiveLimitError, DocumentExtractor
from friday.web_surfer import UnsafeURLError, validate_public_url


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_archive_limits_and_safe_preview():
    extractor = DocumentExtractor(
        max_archive_entries=3,
        max_archive_uncompressed_bytes=4096,
        max_text_chars=20_000,
    )
    good = extractor.extract(_zip({"notes.txt": b"hello world"}), "notes.zip")
    assert good.success is True
    assert "hello world" in good.text

    too_many = extractor.extract(
        _zip({f"{index}.txt": b"x" for index in range(4)}),
        "too-many.zip",
    )
    assert too_many.success is False
    assert too_many.error == "archive_limit_exceeded"

    oversized = extractor.extract(_zip({"large.txt": b"x" * 5000}), "large.zip")
    assert oversized.success is False
    assert oversized.error == "archive_limit_exceeded"


def test_parser_exceptions_cannot_become_durable_extraction_text(monkeypatch):
    private = "DOCUMENT-EXCEPTION-SENTINEL-6c81fa"
    extractor = DocumentExtractor()

    def parser_failure(*_args, **_kwargs):
        raise RuntimeError(f"private parser detail {private}")

    monkeypatch.setattr(extractor, "_extract_pdf", parser_failure)
    generic = extractor.extract(b"%PDF synthetic", "synthetic.pdf", "application/pdf")
    assert generic.error == "document_extract_failed:RuntimeError"
    assert private not in generic.error

    def archive_failure(*_args, **_kwargs):
        raise ArchiveLimitError(f"private member {private}")

    monkeypatch.setattr(extractor, "_extract_archive", archive_failure)
    archive = extractor.extract(b"PK synthetic", "synthetic.zip", "application/zip")
    assert archive.error == "archive_limit_exceeded"
    assert private not in archive.error

    from friday.documents import _ole

    def ole_failure(_content):
        raise _ole.OleError(f"private stream {private}")

    monkeypatch.setattr(_ole, "extract_doc_text", ole_failure)
    legacy = extractor.extract(b"synthetic", "synthetic.doc", "application/msword")
    assert legacy.error == "unsupported_legacy_doc"
    assert private not in legacy.error


def _office_zip_with_a_corrupted_image() -> bytes:
    """A .docx-shaped zip whose FIRST embedded image has a broken deflate stream —
    same length, same CRC/size fields, only the compressed payload flipped — so the
    zip stays structurally valid and only `zlib.error` fires on decompression. The
    second image is untouched, to prove one corrupt member doesn't take the rest
    down with it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<xml>hello</xml>")
        archive.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\n" + b"A" * 200)
        archive.writestr("word/media/image2.png", b"\x89PNG\r\n\x1a\n" + b"B" * 200)

    raw = bytearray(buffer.getvalue())
    with zipfile.ZipFile(io.BytesIO(bytes(raw))) as archive:
        info = archive.getinfo("word/media/image1.png")
        offset = info.header_offset + 30 + len(info.filename.encode())
        comp_size = info.compress_size
    for index in range(offset, offset + comp_size):
        raw[index] ^= 0xFF
    return bytes(raw)


def test_a_corrupted_embedded_image_does_not_crash_office_extraction():
    """Found by adversarial review: a bit-corrupted embedded picture inside a real
    .docx/.pptx/.xlsx (flaky transfer, interrupted save, or a torture-test upload)
    raised `zlib.error` out of `_office_embedded_images` uncaught — not a subclass of
    the `(OSError, zipfile.BadZipFile, RuntimeError)` tuple the function guarded
    against. That propagated through ingestion, past `/api/chat`'s only
    `except BaseException: raise`, into the Telegram bridge as a plain HTTP 500 that
    is not a `PermanentUpdateError` — so the same corrupted file got retried
    identically for up to a day (288 attempts) with no message to the user.

    Mutation: narrow the inner `except Exception` in `_office_embedded_images` back
    to `(OSError, zipfile.BadZipFile, RuntimeError)` — this test must go red with an
    uncaught `zlib.error`.
    """
    from friday.documents import DocumentExtractor

    data = _office_zip_with_a_corrupted_image()
    images = DocumentExtractor._office_embedded_images(data, max_candidates=5)

    # The corrupted image is skipped; the healthy second one still comes through.
    assert len(images) == 1
    _, name = images[0]
    assert name == "word/media/image2.png"


def test_large_csv_is_streamed_into_a_bounded_result():
    extractor = DocumentExtractor(max_text_chars=10_000, max_input_bytes=4 * 1024 * 1024)
    content = ("name,value\n" + "alpha," + "x" * 80 + "\n") * 20_000

    result = extractor.extract(content.encode(), "large.csv", "text/csv")

    assert result.success is True
    assert len(result.text) <= 10_000
    assert result.metadata["rows_read"] < 20_000
    assert result.metadata["rows_truncated"] is True
    assert result.metadata["text_truncated"] is True


def test_xlsx_stops_at_text_budget_without_quadratic_assembly():
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet("Data")
    for index in range(2_000):
        sheet.append((index, "x" * 80))
    buffer = io.BytesIO()
    workbook.save(buffer)

    extractor = DocumentExtractor(max_text_chars=10_000)
    result = extractor.extract(buffer.getvalue(), "large.xlsx")

    assert result.success is True
    assert len(result.text) <= 10_000
    assert result.metadata["rows_read"] < 2_000
    assert result.metadata["extraction_truncated"] is True


def test_ssrf_validation_rejects_local_and_non_http_destinations():
    for url in (
        "http://127.0.0.1/admin",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http://user:pass@example.com/",
    ):
        with pytest.raises(UnsafeURLError):
            validate_public_url(url)
    assert validate_public_url("http://127.0.0.1/", allow_private_networks=True).startswith("http://")


def test_the_archive_preview_cap_counts_decompressions_not_successes():
    """A member that yields no text used to cost nothing against the cap.

    `previewed += 1` sat inside `if preview:`, so an inner archive full of
    zero-filled binary members never advanced the 24-file limit and the loop
    decompressed EVERY member — nested, exactly the decompression bomb the cap
    exists to bound. Measured on this machine with a 107 KB upload
    (24 x 24 x 500 zero-filled members): **30.1 s before, 2.1 s after**, and all
    of it synchronous on the event loop. The reported `previewed_files: 24` was
    itself misleading — it claimed the cap had been reached while the work
    continued.

    The wall-clock bound below is loose on purpose: it fails a return to
    success-counting, not a slow machine.
    """
    import time

    inner = _zip({f"z{index}.bin": bytes(131_071) for index in range(500)})
    middle = _zip({f"m{index}.zip": inner for index in range(24)})
    outer = _zip({f"o{index}.zip": middle for index in range(24)})
    assert len(outer) < 200_000  # a small upload, by design

    extractor = DocumentExtractor()
    started = time.perf_counter()
    result = extractor.extract(outer, "bomb.zip")
    elapsed = time.perf_counter() - started

    assert result.success
    assert result.metadata["previewed_files"] <= 24
    assert elapsed < 10.0, f"archive preview took {elapsed:.1f}s — is the cap counting successes again?"


@pytest.mark.anyio
async def test_a_redirect_into_the_local_network_is_blocked(settings):
    """Мутация: проверять адрес только один раз перед циклом — тест краснеет.

    Классическая дыра: внешний адрес проходит проверку и отвечает 302 на
    `http://127.0.0.1:8000/api/admin/users`. Если адрес проверяется только на
    входе, ассистент послушно сходит во внутреннюю сеть от имени машины
    владельца. Здесь адрес валидируется на КАЖДОМ шаге цепочки.
    """
    import httpx

    from friday.web_surfer import WebSurfer

    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        if request.url.host == "example.org":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/api/admin/users"})
        return httpx.Response(200, text="СЕКРЕТ внутренней сети")

    surfer = WebSurfer(settings)
    surfer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    result = await surfer.fetch("https://example.org/start")
    await surfer.close()

    assert "СЕКРЕТ" not in result.text, "ассистент сходил во внутреннюю сеть по редиректу"
    assert result.error, "переход внутрь сети прошёл молча"
    assert not any("127.0.0.1" in hop for hop in hops), "запрос к локальному адресу всё-таки ушёл"


@pytest.mark.anyio
async def test_a_redirect_chain_cannot_run_forever(settings):
    """Цикл редиректов — это отказ, а не бесконечная работа."""
    import httpx

    from friday.web_surfer import WebSurfer

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.org/next"})

    surfer = WebSurfer(settings)
    surfer._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # noqa: SLF001
    result = await surfer.fetch("https://example.org/start")
    await surfer.close()
    assert result.error, "бесконечная цепочка редиректов прошла как успех"
