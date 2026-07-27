from __future__ import annotations

import io
import zipfile

import pytest

from jericho.documents import DocumentExtractor
from jericho.web_surfer import UnsafeURLError, validate_public_url


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
    assert "entry count" in too_many.error.casefold()

    oversized = extractor.extract(_zip({"large.txt": b"x" * 5000}), "large.zip")
    assert oversized.success is False
    assert "size" in oversized.error.casefold() or "ratio" in oversized.error.casefold()


def test_large_csv_is_streamed_into_a_bounded_result():
    extractor = DocumentExtractor(max_text_chars=10_000, max_input_bytes=4 * 1024 * 1024)
    content = ("name,value\n" + "alpha," + "x" * 80 + "\n") * 20_000

    result = extractor.extract(content.encode(), "large.csv", "text/csv")

    assert result.success is True
    assert len(result.text) <= 10_000
    assert result.metadata["rows_read"] < 20_000
    assert result.metadata["rows_truncated"] is True


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
