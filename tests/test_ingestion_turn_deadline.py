"""HTTP admission's absolute clock also bounds pre-agent ingestion."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from friday.documents import DocumentResult
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "mime_type"),
    (
        ("slow.txt", "text/plain"),
        ("slow.zip", "application/zip"),
    ),
)
async def test_file_extraction_wait_is_clipped_before_any_raw_object_is_committed(
    settings,
    storage,
    monkeypatch,
    filename: str,
    mime_type: str,
) -> None:
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)

    def slow_extract(*_args, **_kwargs):
        started.set()
        try:
            release.wait(timeout=2.0)
            return DocumentResult(text="eventually parsed")
        finally:
            finished.set()

    monkeypatch.setattr(pipeline._doc_extractor, "extract", slow_extract)  # noqa: SLF001
    source_ref = f"deadline:{filename}"
    started_at = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            await pipeline.ingest_file(
                "alice",
                None,
                b"synthetic bytes",
                filename=filename,
                mime_type=mime_type,
                source_ref=source_ref,
                turn_deadline=time.monotonic() + 0.03,
            )
        assert time.monotonic() - started_at < 0.5
        assert storage.find_raw_by_source_ref("alice", "upload", source_ref) is None
    finally:
        release.set()
        if started.is_set():
            assert await asyncio.to_thread(finished.wait, 1.0)


@pytest.mark.asyncio
async def test_transient_inspection_uses_the_same_nonrenewable_deadline(
    settings,
    storage,
    monkeypatch,
) -> None:
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)

    def slow_extract(*_args, **_kwargs):
        started.set()
        try:
            release.wait(timeout=2.0)
            return DocumentResult(text="eventually parsed")
        finally:
            finished.set()

    monkeypatch.setattr(pipeline._doc_extractor, "extract", slow_extract)  # noqa: SLF001
    try:
        with pytest.raises(TimeoutError):
            await pipeline.inspect_file_transient(
                b"synthetic bytes",
                filename="private.txt",
                mime_type="text/plain",
                turn_deadline=time.monotonic() + 0.03,
            )
    finally:
        release.set()
        if started.is_set():
            assert await asyncio.to_thread(finished.wait, 1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transient", "filename", "mime_type"),
    (
        (False, "bounded.txt", "text/plain"),
        (False, "bounded.zip", "application/zip"),
        (True, "bounded.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ),
)
async def test_request_deadline_is_propagated_into_document_worker(
    settings,
    storage,
    monkeypatch,
    transient: bool,
    filename: str,
    mime_type: str,
) -> None:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    observed: list[float | None] = []

    def capture_extract(*_args, **kwargs):
        observed.append(kwargs.get("_deadline"))
        return DocumentResult(text="x" * 200)

    monkeypatch.setattr(pipeline._doc_extractor, "extract", capture_extract)  # noqa: SLF001
    deadline = time.monotonic() + 10

    if transient:
        await pipeline.inspect_file_transient(
            b"synthetic bytes",
            filename=filename,
            mime_type=mime_type,
            turn_deadline=deadline,
        )
    else:
        await pipeline.ingest_file(
            "alice",
            None,
            b"synthetic bytes",
            filename=filename,
            mime_type=mime_type,
            source_ref=f"propagated:{filename}",
            turn_deadline=deadline,
        )

    assert observed == [deadline]


@pytest.mark.asyncio
async def test_request_deadline_is_propagated_into_metadata_only_worker(
    settings,
    storage,
    monkeypatch,
) -> None:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)
    observed: list[float | None] = []

    def capture_metadata(*_args, **kwargs):
        observed.append(kwargs.get("deadline"))
        return {
            "format": "pdf",
            "metadata_parse_status": "partial",
            "technical_metadata_incomplete": True,
            "parse_deadline_reached": True,
        }

    monkeypatch.setattr(  # noqa: SLF001
        pipeline._doc_extractor,
        "extract_document_metadata",
        capture_metadata,
    )
    deadline = time.monotonic() + 10

    result = await pipeline.inspect_file_transient(
        b"%PDF-synthetic",
        filename="bounded.pdf",
        mime_type="application/pdf",
        metadata_only=True,
        turn_deadline=deadline,
    )

    assert observed == [deadline]
    assert result["_document_metadata"]["parse_deadline_reached"] is True


@pytest.mark.asyncio
async def test_expired_text_ingestion_fails_before_persistence(settings, storage) -> None:
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)

    with pytest.raises(TimeoutError):
        await pipeline.ingest_text(
            "alice",
            "Запомни синтетический факт",
            source_ref="expired-text",
            turn_deadline=time.monotonic() - 1.0,
        )

    assert storage.find_raw_by_source_ref("alice", "telegram", "expired-text") is None
