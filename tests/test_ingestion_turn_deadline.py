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
