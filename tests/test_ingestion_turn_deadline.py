"""HTTP admission's absolute clock also bounds pre-agent ingestion."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from friday.documents import DocumentResult
from friday.ingestion import IngestionPipeline
from friday.ingestion import _files as ingestion_files
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
    started = asyncio.Event()
    finished = threading.Event()
    worker_finished = asyncio.Event()
    clipped = asyncio.Event()
    timeouts: list[float | None] = []
    loop = asyncio.get_running_loop()
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage), None)

    def slow_extract(*_args, **_kwargs):
        loop.call_soon_threadsafe(started.set)
        try:
            release.wait()
            return DocumentResult(text="eventually parsed")
        finally:
            finished.set()
            loop.call_soon_threadsafe(worker_finished.set)

    async def observed_wait_for(awaitable, *, timeout):
        timeouts.append(timeout)
        worker = asyncio.ensure_future(awaitable)
        try:
            # Executor startup is fixture setup, outside the timer under test.
            await started.wait()
            try:
                return await asyncio.wait_for(worker, timeout=timeout)
            except TimeoutError:
                clipped.set()
                raise
        finally:
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    monkeypatch.setattr(pipeline._doc_extractor, "extract", slow_extract)  # noqa: SLF001
    # Freeze only the ingestion clock, leaving asyncio's real timer untouched.
    # Assert its precise budget and ordering instead of host scheduling latency.
    monkeypatch.setattr(ingestion_files, "time", SimpleNamespace(monotonic=lambda: 100.0))
    monkeypatch.setattr(
        ingestion_files,
        "asyncio",
        SimpleNamespace(to_thread=asyncio.to_thread, wait_for=observed_wait_for),
    )
    source_ref = f"deadline:{filename}"
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                pipeline.ingest_file(
                    "alice",
                    None,
                    b"synthetic bytes",
                    filename=filename,
                    mime_type=mime_type,
                    source_ref=source_ref,
                    turn_deadline=100.03,
                ),
                timeout=10.0,  # Deadlock watchdog; cannot satisfy `clipped` below.
            )
        assert timeouts == [pytest.approx(0.03)]
        assert started.is_set()
        assert clipped.is_set()
        assert not finished.is_set()
        assert storage.find_raw_by_source_ref("alice", "upload", source_ref) is None
    finally:
        release.set()
        if started.is_set():
            await asyncio.wait_for(worker_finished.wait(), timeout=10.0)


@pytest.mark.asyncio
async def test_ingestion_stage_waits_spend_one_absolute_budget(monkeypatch) -> None:
    clock = SimpleNamespace(now=100.0)
    timeouts: list[float | None] = []
    entered: list[str] = []

    async def stage(name):
        entered.append(name)

    async def observed_wait_for(awaitable, *, timeout):
        timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(ingestion_files, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(ingestion_files, "asyncio", SimpleNamespace(wait_for=observed_wait_for))
    await ingestion_files._await_with_turn_deadline(stage("first"), 100.03)  # noqa: SLF001
    clock.now = 100.02
    await ingestion_files._await_with_turn_deadline(stage("second"), 100.03)  # noqa: SLF001
    clock.now = 100.04
    expired = stage("expired")
    with pytest.raises(TimeoutError, match="expired before ingestion stage"):
        await ingestion_files._await_with_turn_deadline(expired, 100.03)  # noqa: SLF001

    assert timeouts == pytest.approx([0.03, 0.01])
    assert entered == ["first", "second"]
    assert expired.cr_frame is None


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
