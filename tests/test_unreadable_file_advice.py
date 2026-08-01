"""What Friday advises for a file it could not read at all.

Routing was never the problem: `not extraction_succeeded` forces the Inbox, so an
unreadable file has never become a Knowledge Object on its own. The ADVICE was the
problem, and the advice is what a person acts on — the Telegram inline button maps
the suggested action straight to "Добавлено в знания".

Measured on this installation: a repository upload produced 34 Inbox items whose
whole content was `[File: NAME.png; type=image/png; size=7008]`, every one of them
advising promotion. The owner rejected all 34 — the right verdict, and 34 decisions
of pure friction. Promoting even one produces an object that is indexed, embedded,
retrievable and says nothing.
"""

from __future__ import annotations

import json

import pytest

from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph


def _json(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


async def _ingest(settings, storage, content: bytes, *, filename: str, mime: str):
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    result = await pipeline.ingest_file(
        "alice", None, content, filename=filename, mime_type=mime, source_ref=f"upload:{filename}"
    )
    return pipeline, result


@pytest.mark.asyncio
async def test_an_unreadable_file_is_not_advised_for_promotion(settings, storage):
    _, result = await _ingest(
        settings,
        storage,
        b"\x89PNG\r\n\x1a\n" + b"\x01" * 4096,
        filename="BLACK_VLESS_RUS-QR.png",
        mime="image/png",
    )
    assert result["promoted"] is False
    assert result["queued_for_review"] is True

    inbox = storage.find_inbox_by_raw(result["raw_object_id"], "alice")
    assert inbox["status"] == "pending"
    assert inbox["suggested_action"] != "promote"
    # `suggested_action` is derived from the score, not from the assessment's own
    # `action` field — so the score is the load-bearing half and is asserted first.
    assert float(inbox["promotion_score"]) < 0.5

    assessment = _json(storage.get_raw_object(result["raw_object_id"], "alice")["metadata_json"]).get(
        "promotion_assessment", {}
    )
    # The other half: the recorded verdict, which `_replay_file_source` reads and
    # which the classification notes show a person. It must not say "promote" either.
    assert assessment.get("action") == "review"
    assert "no_extractable_text" in assessment.get("penalties", [])


@pytest.mark.asyncio
async def test_the_file_itself_is_still_kept_and_findable(settings, storage):
    """Not promoting must not mean losing it — that would be a worse defect."""
    _, result = await _ingest(
        settings,
        storage,
        b"\x89PNG\r\n\x1a\n" + b"\x02" * 4096,
        filename="WHITE-CIDR-RU-all-QR.png",
        mime="image/png",
    )
    raw = storage.get_raw_object(result["raw_object_id"], "alice")
    assert raw is not None and result["stored_path"]
    assert "WHITE-CIDR-RU-all-QR.png" in raw["raw_content"]
    found = storage.search_raw_objects("alice", "WHITE-CIDR-RU-all-QR")
    assert [item["id"] for item in found] == [result["raw_object_id"]]


@pytest.mark.asyncio
async def test_a_readable_file_keeps_its_promotion_advice(settings, storage):
    """The filter is about unreadable bytes, not about uploads in general."""
    text = (
        "Договор аренды квартиры на Мира 12. Ежемесячная плата 45 тысяч рублей, "
        "коммунальные услуги оплачиваются отдельно. Залог равен одному месяцу и "
        "возвращается при выезде, если нет повреждений. Срок действия договора "
        "истекает 31 августа, продление обсуждается в июле.\n"
    ).encode()
    _, result = await _ingest(settings, storage, text, filename="dogovor.txt", mime="text/plain")

    raw = storage.get_raw_object(result["raw_object_id"], "alice")
    assessment = _json(raw["metadata_json"]).get("promotion_assessment", {})
    assert assessment.get("action") == "promote"
    assert "no_extractable_text" not in assessment.get("penalties", [])


@pytest.mark.asyncio
async def test_a_file_that_parses_but_yields_nothing_still_says_so(settings, storage):
    """A scan parses perfectly and contains no text — the two are different failures.

    The flag was keyed on `extraction.success`, which answers "did the parser run
    without error". Measured on the owner's folder: all 18 unreadable PDFs are scans,
    `success=True` and `chars=0`, and not one of them carried the marker that tells a
    reviewer they are looking at a document with nothing in it.
    """
    from friday.documents import DocumentResult

    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    # A parser that succeeds and returns nothing: exactly a scanned page.
    pipeline._doc_extractor.extract = lambda *args, **kwargs: DocumentResult(  # noqa: SLF001
        "", {"format": "pdf"}, True, ""
    )
    result = await pipeline.ingest_file(
        "alice",
        None,
        b"%PDF-1.4 scan",
        filename="скан.pdf",
        mime_type="application/pdf",
        source_ref="upload:scan",
    )

    assessment = _json(storage.get_raw_object(result["raw_object_id"], "alice")["metadata_json"]).get(
        "promotion_assessment", {}
    )
    assert assessment.get("action") == "review"
    assert "no_extractable_text" in assessment.get("penalties", [])
    # The parser did not fail; only the document is empty. Keep them distinguishable.
    assert "extraction_failed" not in assessment.get("penalties", [])
