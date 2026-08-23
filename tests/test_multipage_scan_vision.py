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
from friday.ingestion import _files as ingestion_files
from friday.knowledge_graph import KnowledgeGraph


def _raster_pdf(pages: int, *, width: int = 320, height: int = 240) -> bytes:
    images: list[Image.Image] = []
    for page in range(1, pages + 1):
        image = Image.new("RGB", (width, height), "white")
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
        multi_delay_sec: float = 0.0,
    ) -> None:
        self.fail_from_page = fail_from_page
        self.omit_from_page = omit_from_page
        self.delay_sec = delay_sec
        self.multi_delay_sec = multi_delay_sec
        self.calls: list[list[int]] = []
        self.call_pixels: list[int] = []

    async def chat(self, messages, **kwargs):
        assert kwargs["temperature"] == 0.0
        content = messages[-1]["content"]
        instruction = str(content[0].get("text") or "")
        assert "sideways or upside down" in instruction
        assert "all four right-angle orientations" in instruction
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
        dimensions = [re.search(r"dimensions=(\d+)x(\d+)", descriptor) for descriptor in descriptors]
        assert all(match is not None for match in dimensions)
        self.call_pixels.append(
            sum(int(match.group(1)) * int(match.group(2)) for match in dimensions if match)
        )
        delay = self.multi_delay_sec if len(pages) > 1 and self.multi_delay_sec else self.delay_sec
        if delay:
            await asyncio.sleep(delay)
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


class _TruncatedThenCompactVision:
    enabled = True
    model = "offline-truncated-vision"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def chat(self, messages, **kwargs):
        content = messages[-1]["content"]
        prompt = str(content[0].get("text") or "")
        self.calls.append({"prompt": prompt, "max_tokens": kwargs["max_tokens"]})
        if "TARGETED OCR REREAD" in prompt:
            return {
                "content": json.dumps(
                    {"asset_id": "A1", "text": "RECOVERED JPEG OCR"},
                    ensure_ascii=False,
                )
            }
        # Exact production failure shape: the structured response ended at its
        # output ceiling before one JSON object was complete.
        return {"content": '{"pages":[{"asset_id":"A1","text":"truncated'}


class _LowConfidenceReadableVision:
    enabled = True
    model = "offline-low-confidence-vision"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):
        del kwargs
        self.calls += 1
        content = messages[-1]["content"]
        assert sum(item.get("type") == "image_url" for item in content) == 1
        return {
            "content": json.dumps(
                {
                    "pages": [{"asset_id": "A1", "text": "BLURRY BUT READABLE OCR"}],
                    "text": "",
                    "title": "",
                    "summary": "",
                    "document_type": "scan",
                    # A model may honestly rate a difficult scan below the
                    # former 0.2 gate while still returning useful visible text.
                    "confidence": 0.0,
                    "entities": [],
                    "evidence": [],
                    "warnings": ["low visual confidence"],
                }
            )
        }


class _InvalidAggregateVision(_PageVision):
    async def chat(self, messages, **kwargs):
        content = messages[-1]["content"]
        if sum(item.get("type") == "image_url" for item in content) > 1:
            descriptors = [
                str(item.get("text") or "")
                for item in content
                if str(item.get("text") or "").startswith("ASSET ")
            ]
            matches = [re.search(r"pdf-page-(\d+)-render", item) for item in descriptors]
            assert all(match is not None for match in matches)
            pages = [int(match.group(1)) for match in matches if match is not None]
            self.calls.append(pages)
            return {"content": '{"pages":['}
        return await super().chat(messages, **kwargs)


class _HealthyThenDeadJpegVision:
    enabled = True
    model = "offline-isolated-jpeg-vision"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **kwargs):
        del kwargs
        self.calls += 1
        content = messages[-1]["content"]
        prompt = str(content[0].get("text") or "")
        if self.calls == 1:
            return {
                "content": json.dumps(
                    {
                        "pages": [{"asset_id": "A1", "text": "HEALTHY JPEG OCR"}],
                        "text": "",
                        "title": "",
                        "summary": "",
                        "document_type": "image",
                        "confidence": 0.9,
                        "entities": [],
                        "evidence": [],
                        "warnings": [],
                    }
                )
            }
        if "TARGETED OCR REREAD" in prompt:
            return {"content": '{"asset_id":"A1","text":'}
        return {"content": '{"pages":['}


class _GlobalEvidenceVision(_PageVision):
    async def chat(self, messages, **kwargs):
        response = await super().chat(messages, **kwargs)
        payload = json.loads(response["content"])
        evidence = []
        for page in payload["pages"]:
            if page["asset_id"] in {"A1", "A5"}:
                evidence.append(
                    {
                        "asset_id": page["asset_id"],
                        "quote": page["text"],
                        "claim": f"Evidence for {page['asset_id']}",
                    }
                )
        payload["evidence"] = evidence
        response["content"] = json.dumps(payload)
        return response


class _SecondPageEmptyVision(_PageVision):
    def __init__(self) -> None:
        super().__init__()
        self.rereads = 0

    async def chat(self, messages, **kwargs):
        content = messages[-1]["content"]
        prompt = str(content[0].get("text") or "")
        if "TARGETED OCR REREAD" in prompt:
            self.rereads += 1
            return {"content": json.dumps({"asset_id": "A2", "text": ""})}
        response = await super().chat(messages, **kwargs)
        payload = json.loads(response["content"])
        for page in payload["pages"]:
            if page["asset_id"] == "A2":
                page["text"] = ""
        payload["summary"] = (
            "HALLUCINATED BOTH PAGES SUMMARY" if len(payload["pages"]) > 1 else "READABLE PAGE ONE SUMMARY"
        )
        response["content"] = json.dumps(payload)
        return response


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
async def test_single_png_without_pages_never_uses_merged_top_level_text(settings, storage) -> None:
    class MissingPagesVision:
        enabled = True
        model = "offline-single-vision"

        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages, **kwargs):
            del kwargs
            self.calls += 1
            content = messages[-1]["content"]
            assert sum(item.get("type") == "image_url" for item in content) == 1
            prompt = str(content[0].get("text") or "")
            if "TARGETED OCR REREAD" in prompt:
                return {"content": json.dumps({"asset_id": "A1", "text": "RECOVERED SINGLE IMAGE OCR"})}
            assert any("ASSET A1" in str(item.get("text") or "") for item in content)
            # A syntactically valid response without the required page carrier
            # must not be accepted from this merged, unattributed field.
            return {
                "content": json.dumps(
                    {
                        "text": "MERGED TOP LEVEL TEXT MUST NOT BE USED",
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
    llm = MissingPagesVision()
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        encoded.getvalue(),
        filename="single.png",
        mime_type="image/png",
    )

    assert result is not None and result["success"] is True
    assert result["text"] == "RECOVERED SINGLE IMAGE OCR"
    assert "MERGED TOP LEVEL" not in result["text"]
    assert result["warnings"] == ["vision_structured_response_recovered"]
    assert result["pages_read"] == result["pages_total"] == 1
    assert result["pages_truncated"] is False and llm.calls == 2


@pytest.mark.asyncio
async def test_exact_empty_page_carrier_cannot_be_rescued_by_hallucinated_summary(settings, storage) -> None:
    class EmptyCarrierVision:
        enabled = True
        model = "offline-empty-carrier-vision"

        async def chat(self, messages, **kwargs):
            del kwargs
            content = messages[-1]["content"]
            targeted = any(
                "TARGETED OCR REREAD" in str(item.get("text") or "")
                for item in content
                if item.get("type") == "text"
            )
            if targeted:
                return {"content": json.dumps({"asset_id": "A1", "text": ""})}
            return {
                "content": json.dumps(
                    {
                        "pages": [{"asset_id": "A1", "text": ""}],
                        "text": "MERGED TEXT MUST NOT BECOME THE CARRIER",
                        "title": "Invented title",
                        "summary": "Invented readable summary",
                        "document_type": "scan",
                        "confidence": 0.99,
                        "entities": [
                            {
                                "name": "Invented entity",
                                "entity_type": "other",
                                "confidence": 0.99,
                                "asset_id": "A1",
                                "evidence": "invented",
                            }
                        ],
                        "evidence": [{"asset_id": "A1", "quote": "invented", "claim": "invented"}],
                        "warnings": [],
                    }
                )
            }

    image = Image.new("RGB", (320, 240), "white")
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    result = await _pipeline(settings, storage, EmptyCarrierVision())._extract_visual_document(  # noqa: SLF001
        encoded.getvalue(),
        filename="empty-scan.png",
        mime_type="image/png",
    )

    assert result is not None and result["success"] is False
    assert result["text"] == "" and result["summary"] == "" and result["title"] == ""
    assert result["evidence"] == [] and result["entities"] == []
    assert result["grounded_evidence_count"] == 0
    assert "MERGED TEXT" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_truncated_jpeg_json_gets_one_compact_bounded_ocr_retry(settings, storage) -> None:
    image = Image.new("RGB", (1811, 2560), "white")
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG")
    llm = _TruncatedThenCompactVision()

    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        encoded.getvalue(),
        filename="stress-scan.jpg",
        mime_type="image/jpeg",
    )

    assert result is not None and result["success"] is True
    assert result["text"] == "RECOVERED JPEG OCR"
    assert result["pages_read"] == result["pages_total"] == 1
    assert result["warnings"] == ["vision_structured_response_recovered"]
    assert len(llm.calls) == 2
    assert {call["max_tokens"] for call in llm.calls} == {8_192}
    assert "only full OCR carrier" in str(llm.calls[0]["prompt"])
    assert "TARGETED OCR REREAD" in str(llm.calls[1]["prompt"])


@pytest.mark.asyncio
async def test_low_confidence_does_not_discard_a_complete_ocr_carrier(settings, storage) -> None:
    image = Image.new("RGB", (640, 480), "white")
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG")
    llm = _LowConfidenceReadableVision()

    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        encoded.getvalue(),
        filename="difficult-scan.jpg",
        mime_type="image/jpeg",
    )

    assert result is not None and result["success"] is True
    assert result["text"].endswith("BLURRY BUT READABLE OCR")
    assert result["confidence"] == 0.0
    assert result["pages_read"] == result["pages_total"] == 1
    assert result["pages_truncated"] is False
    assert result["warnings"] == ["low visual confidence"]
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_invalid_aggregate_json_retries_once_as_ordered_single_pages(settings, storage) -> None:
    llm = _InvalidAggregateVision()
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(2),
        filename="two-page-scan.pdf",
        mime_type="application/pdf",
    )

    assert result is not None and result["success"] is True
    assert llm.calls == [[1, 2], [1], [2]]
    assert result["pages_read"] == result["pages_total"] == 2
    assert result["pages_truncated"] is False
    assert result["batch_fallback_used"] is True


@pytest.mark.asyncio
async def test_failed_jpeg_does_not_zero_its_healthy_neighbor(settings, storage) -> None:
    image = Image.new("RGB", (640, 480), "white")
    encoded = io.BytesIO()
    image.save(encoded, format="JPEG")
    llm = _HealthyThenDeadJpegVision()
    pipeline = _pipeline(settings, storage, llm)

    healthy = await pipeline._extract_visual_document(  # noqa: SLF001
        encoded.getvalue(),
        filename="healthy.jpg",
        mime_type="image/jpeg",
    )
    failed = await pipeline._extract_visual_document(  # noqa: SLF001
        encoded.getvalue(),
        filename="failed.jpg",
        mime_type="image/jpeg",
    )

    assert healthy is not None and healthy["success"] is True
    assert "HEALTHY JPEG OCR" in healthy["text"]
    assert failed is not None and failed["success"] is False
    assert failed["pages_read"] == 0
    assert failed["pages_truncated"] is True


@pytest.mark.asyncio
async def test_six_page_raster_pdf_is_rendered_and_fully_ocr_batched(settings, storage) -> None:
    llm = _PageVision()
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(6),
        filename="six-page-scan.pdf",
        mime_type="application/pdf",
    )

    assert result is not None and result["success"] is True
    assert sorted(llm.calls) == [[1, 2], [3, 4], [5, 6]]
    assert result["pages_read"] == result["pages_total"] == 6
    assert result["pages_truncated"] is False
    assert result["batches_read"] == result["batches_total"] == 3
    positions = [result["text"].index(f"OCR PAGE {page}") for page in range(1, 7)]
    assert positions == sorted(positions), result["text"]


@pytest.mark.asyncio
async def test_five_page_scan_balances_low_resolution_batches_within_aggregate_cap(
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
    assert sorted(llm.calls) == [[1, 2], [3, 4], [5]]
    assert llm.call_pixels and max(llm.call_pixels) <= ingestion_files._VISION_BATCH_MAX_PIXELS
    assert result["pages_read"] == result["pages_total"] == 5
    assert result["pages_truncated"] is False
    assert "OCR PAGE 5" in result["text"]


@pytest.mark.asyncio
async def test_evidence_ids_remain_document_global_across_multiple_batches(settings, storage) -> None:
    llm = _GlobalEvidenceVision()
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(6),
        filename="six-page-evidence.pdf",
        mime_type="application/pdf",
    )

    assert result is not None and result["success"] is True
    assert sorted(llm.calls) == [[1, 2], [3, 4], [5, 6]]
    assert [item["asset_id"] for item in result["evidence"]] == ["A1", "A5"]
    assert [item["quote"] for item in result["evidence"]] == ["OCR PAGE 1", "OCR PAGE 5"]
    assert result["grounded_evidence_count"] == 2
    assert result["evidence_text_inconsistent"] is False


@pytest.mark.asyncio
async def test_empty_second_page_is_not_counted_read_or_rescued_by_batch_summary(settings, storage) -> None:
    llm = _SecondPageEmptyVision()
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(2),
        filename="one-readable-one-empty.pdf",
        mime_type="application/pdf",
    )

    assert result is not None and result["success"] is True
    assert llm.calls == [[1, 2], [1], [2]]
    assert llm.rereads == 1
    assert result["pages_total"] == 2 and result["pages_read"] == 1
    assert result["pages_truncated"] is True and result["partial"] is True
    assert "OCR PAGE 1" in result["text"] and "OCR PAGE 2" not in result["text"]
    assert "HALLUCINATED BOTH PAGES SUMMARY" not in result["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reread_agrees", [True, False])
async def test_asset_evidence_quote_requires_one_agreeing_targeted_reread(
    settings,
    storage,
    reread_agrees: bool,
) -> None:
    quote = "VISIBLE REFERENCE 7F3A"

    class EvidenceGapVision:
        enabled = True
        model = "offline-evidence-gap-vision"

        def __init__(self) -> None:
            self.calls = 0
            self.rereads = 0

        async def chat(self, messages, **kwargs):
            del kwargs
            self.calls += 1
            content = messages[-1]["content"]
            targeted = any(
                "TARGETED OCR REREAD" in str(item.get("text") or "")
                for item in content
                if item.get("type") == "text"
            )
            if targeted:
                self.rereads += 1
                value = quote if reread_agrees else "A DIFFERENT VISIBLE VALUE"
                return {"content": json.dumps({"asset_id": "A1", "text": value})}
            return {
                "content": json.dumps(
                    {
                        "pages": [{"asset_id": "A1", "text": "VISIBLE PAGE HEADING"}],
                        "text": "",
                        "title": "Synthetic raster scan",
                        "summary": "One page",
                        "document_type": "scan",
                        "entities": [
                            {
                                "name": "Reference entity",
                                "entity_type": "other",
                                "confidence": 0.9,
                                "asset_id": "A1",
                                "evidence": quote,
                            }
                        ],
                        "evidence": [{"asset_id": "A1", "quote": quote, "claim": "Visible reference"}],
                        "warnings": [],
                        "confidence": 0.8,
                    }
                )
            }

    llm = EvidenceGapVision()
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(1),
        filename="evidence-gap.pdf",
        mime_type="application/pdf",
    )

    assert result is not None and result["success"] is True
    assert llm.calls == 2 and llm.rereads == 1
    assert result["evidence_reread_attempted"] is True
    assert (quote in result["text"]) is reread_agrees
    assert result["evidence_reread_confirmed"] is reread_agrees
    assert result["evidence_text_inconsistent"] is (not reread_agrees)
    assert result["partial"] is (not reread_agrees)
    assert result["text_truncated"] is False
    assert ("vision_evidence_text_inconsistent" in result["warnings"]) is (not reread_agrees)
    assert result["grounded_evidence_count"] == int(reread_agrees)
    assert bool(result["evidence"]) is reread_agrees
    assert bool(result["entities"])
    if reread_agrees:
        assert result["entities"][0]["evidence"] == quote
    else:
        assert result["entities"][0]["evidence"] == ""
        assert result["entities"][0]["confidence"] <= 0.35


@pytest.mark.asyncio
async def test_pdf_pages_and_each_vision_request_have_independent_pixel_bounds(
    settings,
    storage,
    monkeypatch,
) -> None:
    assert ingestion_files._VISION_OCR_BUDGET_SEC == 240.0
    llm = _PageVision()
    pipeline = _pipeline(settings, storage, llm)
    original_render = pipeline._doc_extractor.render_pdf_pages  # noqa: SLF001
    requested_page_bounds: list[int] = []

    def render(*args, **kwargs):  # noqa: ANN002, ANN003
        requested_page_bounds.append(int(kwargs["max_pixels"]))
        return original_render(*args, **kwargs)

    monkeypatch.setattr(pipeline._doc_extractor, "render_pdf_pages", render)  # noqa: SLF001
    result = await pipeline._extract_visual_document(  # noqa: SLF001
        _raster_pdf(5, width=800, height=1100),
        filename="large-five-page-scan.pdf",
        mime_type="application/pdf",
    )

    assert result is not None and result["success"] is True
    assert requested_page_bounds == [ingestion_files._VISION_PAGE_MAX_PIXELS]
    assert llm.calls == [[1], [2], [3], [4], [5]]
    assert llm.call_pixels and max(llm.call_pixels) <= ingestion_files._VISION_BATCH_MAX_PIXELS
    assert result["pages_read"] == result["pages_total"] == 5
    assert result["batch_fallback_used"] is False


@pytest.mark.asyncio
async def test_timed_out_multi_page_batches_retry_as_one_contiguous_single_page_prefix(
    settings,
    storage,
    monkeypatch,
) -> None:
    monkeypatch.setattr(ingestion_files, "_VISION_OCR_BUDGET_SEC", 0.45)
    monkeypatch.setattr(ingestion_files, "_VISION_OCR_FALLBACK_RESERVE_SEC", 0.3)
    llm = _PageVision(delay_sec=0.03, multi_delay_sec=0.25)
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(5),
        filename="fallback-five-page-scan.pdf",
        mime_type="application/pdf",
    )

    assert result is not None and result["success"] is True
    assert sorted(llm.calls[:2]) == [[1, 2], [3, 4]]
    assert llm.calls[2:] == [[1], [2], [3], [4], [5]]
    assert result["pages_read"] == result["pages_total"] == 5
    assert result["pages_truncated"] is False
    assert result["deadline_reached"] is False
    assert result["batch_fallback_used"] is True
    assert result["batches_read"] == 5
    assert result["batches_total"] == 7

    # A single page cannot be made smaller as a batch.  It receives the whole
    # common deadline instead of being cut off at the fallback boundary.
    single_llm = _PageVision(delay_sec=0.2)
    single = await _pipeline(settings, storage, single_llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(1),
        filename="single-page-control.pdf",
        mime_type="application/pdf",
    )
    assert single is not None and single["success"] is True
    assert single["batch_fallback_used"] is False
    assert single_llm.calls == [[1]]


@pytest.mark.asyncio
async def test_failed_second_vision_batch_keeps_only_honest_contiguous_prefix(settings, storage) -> None:
    llm = _PageVision(fail_from_page=5)
    result = await _pipeline(settings, storage, llm)._extract_visual_document(  # noqa: SLF001
        _raster_pdf(10),
        filename="ten-page-scan.pdf",
        mime_type="application/pdf",
    )

    assert result is not None and result["success"] is True
    # The failed aggregate receives one bounded singleton retry. Page 5 still
    # fails, so the honest contiguous prefix remains pages 1..4 and no later
    # success is admitted across the hole.
    assert sorted(llm.calls) == [[1, 2], [3, 4], [5], [5, 6], [7, 8]]
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
    assert omitted["batch_fallback_used"] is True


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
    assert len(capped_llm.calls) == 20 and all(len(batch) == 2 for batch in capped_llm.calls)

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
