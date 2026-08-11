"""Scan-only PDFs are rendered and OCRed as an honest contiguous page prefix."""

from __future__ import annotations

import asyncio
import io
import json
import math
import re
import time
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw

from friday.config import PROFILES
from friday.documents import DocumentExtractor
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph


def _raster_pdf(pages: int) -> bytes:
    images: list[Image.Image] = []
    for page in range(1, pages + 1):
        image = Image.new("RGB", (320, 240), "white")
        ImageDraw.Draw(image).text((24, 24), f"SYNTHETIC PAGE {page}", fill="black")
        images.append(image)
    output = io.BytesIO()
    images[0].save(output, format="PDF", save_all=True, append_images=images[1:])
    return output.getvalue()


class _PageVision:
    enabled = True
    model = "offline-page-vision"

    def __init__(
        self,
        *,
        fail_from_page: int | None = None,
        omit_from_page: int | None = None,
        delay_sec: float = 0.0,
    ) -> None:
        self.fail_from_page = fail_from_page
        self.omit_from_page = omit_from_page
        self.delay_sec = delay_sec
        self.calls: list[list[int]] = []

    async def chat(self, messages, **kwargs):
        assert kwargs["temperature"] == 0.0
        content = messages[-1]["content"]
        descriptors = [
            str(item.get("text") or "")
            for item in content
            if item.get("type") == "text" and str(item.get("text") or "").startswith("ASSET ")
        ]
        pairs: list[tuple[str, int]] = []
        for descriptor in descriptors:
            match = re.search(r"ASSET (A\d+): source=pdf-page-(\d+)-render", descriptor)
            assert match, descriptor
            pairs.append((match.group(1), int(match.group(2))))
        assert 1 <= len(pairs) <= 4
        assert sum(item.get("type") == "image_url" for item in content) == len(pairs)
        pages = [page for _asset_id, page in pairs]
        self.calls.append(pages)
        if self.delay_sec:
            await asyncio.sleep(self.delay_sec)
        if self.fail_from_page is not None and pages[0] == self.fail_from_page:
            raise RuntimeError("synthetic batch failure")
        reported_pairs = pairs[:-1] if self.omit_from_page == pages[0] else pairs
        return {
            "content": json.dumps(
                {
                    "pages": [
                        {"asset_id": asset_id, "text": f"OCR PAGE {page}"}
                        for asset_id, page in reported_pairs
                    ],
                    "text": "",
                    "title": "Synthetic raster scan",
                    "summary": f"Pages {pages[0]}-{pages[-1]}",
                    "document_type": "scan",
                    "entities": [],
                    "evidence": [],
                    "warnings": [],
                    "confidence": 0.7,
                }
            )
        }


def _pipeline(settings, storage, llm) -> IngestionPipeline:
    return IngestionPipeline(
        replace(settings, profile=PROFILES["qwen36-vl"]),
        storage,
        KnowledgeGraph(storage),
        llm,
    )


def test_pdf_renderer_bounds_the_actual_rounded_bitmap_before_allocation(monkeypatch) -> None:
    """An extreme MediaBox cannot turn an 8M-pixel budget into an 80M-wide row."""

    import pypdfium2 as pdfium

    observed: list[tuple[int, int]] = []

    class Bitmap:
        def to_pil(self):
            return Image.new("RGB", (8, 8), "white")

        def close(self) -> None:
            return None

    class Page:
        def get_size(self):
            return 80_000_000.0, 0.1

        def render(self, *, scale):
            width = max(1, math.ceil(80_000_000.0 * scale))
            height = max(1, math.ceil(0.1 * scale))
            observed.append((width, height))
            assert width <= 16_384 and height <= 16_384
            assert width * height <= 8_000_000
            return Bitmap()

        def close(self) -> None:
            return None

    class Document:
        def __len__(self) -> int:
            return 1

        def __getitem__(self, _index):
            return Page()

        def close(self) -> None:
            return None

    monkeypatch.setattr(pdfium, "PdfDocument", lambda _content: Document())
    rendered = DocumentExtractor(secret_values=()).render_pdf_pages(
        b"%PDF synthetic",
        "extreme.pdf",
        "application/pdf",
        max_pixels=8_000_000,
    )

    assert observed and rendered.pages_rendered == 1


@pytest.mark.asyncio
async def test_single_png_keeps_legacy_one_call_json_without_pages(settings, storage) -> None:
    class LegacySingleVision:
        enabled = True
        model = "offline-single-vision"

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, **kwargs):
            del kwargs
            self.calls += 1
            content = messages[-1]["content"]
            assert sum(item.get("type") == "image_url" for item in content) == 1
            assert any("ASSET A1" in str(item.get("text") or "") for item in content)
            # Exact pre-multipage response shape: no `pages` field.
            return {
                "content": json.dumps(
                    {
                        "text": "LEGACY SINGLE IMAGE OCR",
                        "title": "Single image",
                        "summary": "One image",
                        "entities": [],
                        "evidence": [],
                        "warnings": [],
                        "confidence": 0.7,
                    }
                )
            }

    image = Image.new("RGB", (320, 240), "white")
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    llm = LegacySingleVision()
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        encoded.getvalue(),
        filename="single.png",
        mime_type="image/png",
    )

    assert result is not None and result["success"] is True
    assert result["text"] == "LEGACY SINGLE IMAGE OCR"
    assert result["pages_read"] == result["pages_total"] == 1
    assert result["pages_truncated"] is False and llm.calls == 1


@pytest.mark.asyncio
async def test_six_page_raster_pdf_is_rendered_and_fully_ocr_batched(settings, storage) -> None:
    llm = _PageVision()
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(6),
        filename="six-page-scan.pdf",
        mime_type="application/pdf",
    )

    assert result is not None and result["success"] is True
    assert sorted(llm.calls) == [[1, 2, 3], [4, 5, 6]]
    assert result["pages_read"] == result["pages_total"] == 6
    assert result["pages_truncated"] is False
    assert result["batches_read"] == result["batches_total"] == 2
    positions = [result["text"].index(f"OCR PAGE {page}") for page in range(1, 7)]
    assert positions == sorted(positions), result["text"]


@pytest.mark.asyncio
async def test_five_page_scan_balances_concurrent_batches_without_a_one_page_tail(
    settings,
    storage,
) -> None:
    llm = _PageVision()
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(5),
        filename="five-page-scan.pdf",
        mime_type="application/pdf",
    )

    assert result is not None and result["success"] is True
    assert sorted(llm.calls) == [[1, 2, 3], [4, 5]]
    assert result["pages_read"] == result["pages_total"] == 5
    assert result["pages_truncated"] is False
    assert "OCR PAGE 5" in result["text"]


@pytest.mark.asyncio
async def test_failed_second_vision_batch_keeps_only_honest_contiguous_prefix(settings, storage) -> None:
    llm = _PageVision(fail_from_page=5)
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(10),
        filename="ten-page-scan.pdf",
        mime_type="application/pdf",
    )

    assert result is not None and result["success"] is True
    assert sorted(llm.calls) == [[1, 2, 3, 4], [5, 6, 7]]
    assert result["pages_total"] == 10
    assert result["pages_read"] == 4
    assert result["pages_truncated"] is True and result["partial"] is True
    assert result["error"] == "vision_request_failed:RuntimeError"
    assert "OCR PAGE 4" in result["text"] and "OCR PAGE 5" not in result["text"]
    assert "vision_batch_failed" in result["warnings"]

    # A syntactically valid JSON answer which silently omits one supplied page
    # is a batch failure too.  Counting the other three would turn a hole in the
    # middle into a misleading prefix length.
    omitted = await _pipeline(settings, storage, _PageVision(omit_from_page=5))._extract_visual_document(  # noqa: SLF001
        _raster_pdf(10),
        filename="ten-page-omission.pdf",
        mime_type="application/pdf",
    )
    assert omitted is not None and omitted["pages_read"] == 4
    assert omitted["error"] == "vision_batch_page_coverage_incomplete"
    assert omitted["pages_truncated"] is True and "OCR PAGE 5" not in omitted["text"]


@pytest.mark.asyncio
async def test_scan_page_cap_and_common_deadline_are_reported_without_fake_completeness(
    settings,
    storage,
    monkeypatch,
) -> None:
    capped_llm = _PageVision()
    capped = await _pipeline(settings, storage, capped_llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(41),
        filename="forty-one-page-scan.pdf",
        mime_type="application/pdf",
    )
    assert capped is not None and capped["success"] is True
    assert capped["pages_total"] == 41 and capped["pages_read"] == 40
    assert capped["page_cap_reached"] is True
    assert capped["pages_truncated"] is True and capped["partial"] is True
    assert len(capped_llm.calls) == 10 and all(len(batch) <= 4 for batch in capped_llm.calls)

    # Reuse a real PDFium render so this half measures the asynchronous common
    # OCR deadline only, without depending on machine-specific render speed.
    deadline_pdf = _raster_pdf(6)
    deadline_llm = _PageVision(delay_sec=0.05)
    deadline_pipeline = _pipeline(settings, storage, deadline_llm)
    pre_rendered = deadline_pipeline._doc_extractor.render_pdf_pages(  # noqa: SLF001
        deadline_pdf,
        "deadline.pdf",
        "application/pdf",
        deadline=time.monotonic() + 5,
    )
    assert pre_rendered.pages_rendered == 6
    monkeypatch.setattr(
        deadline_pipeline._doc_extractor,  # noqa: SLF001
        "render_pdf_pages",
        lambda *args, **kwargs: pre_rendered,
    )
    monkeypatch.setattr("friday.ingestion._files._VISION_OCR_BUDGET_SEC", 0.01)
    deadline = await deadline_pipeline._extract_visual_document(  # noqa: SLF001
        deadline_pdf,
        filename="deadline.pdf",
        mime_type="application/pdf",
    )
    assert deadline is not None and deadline["success"] is False
    assert deadline["pages_total"] == 6 and deadline["pages_read"] == 0
    assert deadline["deadline_reached"] is True
    assert deadline["pages_truncated"] is True and deadline["partial"] is True
    assert deadline["error"] == "vision_deadline_reached"
