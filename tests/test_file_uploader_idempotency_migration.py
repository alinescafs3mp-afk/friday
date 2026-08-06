"""Uploader-scoped source keys remain compatible with pre-namespace rows.

The fixtures are wholly synthetic.  They model a rolling upgrade in which an
older worker wrote the caller's source_ref verbatim while recording the exact
uploader in metadata, and a newer worker prefixes uploader-scoped keys.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from friday.ingestion import IdempotencyConflictError, IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph

_MISSING = object()


@pytest.fixture
def pipeline(settings, storage):
    return IngestionPipeline(settings, storage, KnowledgeGraph(storage))


async def _legacy_upload(
    pipeline,
    storage,
    *,
    tenant_id: str,
    source_ref: str,
    content: bytes,
    recorded_uploader: object = _MISSING,
) -> dict:
    """Write the unprefixed shape produced before uploader namespacing."""

    result = await pipeline.ingest_file(
        tenant_id,
        None,
        content,
        filename="legacy-note.txt",
        mime_type="text/plain",
        source_ref=source_ref,
        force_review=True,
    )
    row = storage.execute(
        "SELECT metadata_json FROM raw_objects WHERE id=?",
        (result["raw_object_id"],),
    ).fetchone()
    metadata = json.loads(row["metadata_json"])
    if recorded_uploader is not _MISSING:
        metadata["uploaded_by"] = recorded_uploader
        storage.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), result["raw_object_id"]),
        )
        storage.commit()
    return result


@pytest.mark.asyncio
async def test_same_uploader_replays_a_legacy_unprefixed_source(
    pipeline,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = "synthetic-shared-tenant"
    uploader = "synthetic-person-a"
    source_ref = "synthetic-source:legacy-replay"
    content = b"A synthetic document written before uploader namespacing."
    legacy = await _legacy_upload(
        pipeline,
        storage,
        tenant_id=tenant_id,
        source_ref=source_ref,
        content=content,
        recorded_uploader=uploader,
    )

    def content_fallback_must_not_authorize(*_args, **_kwargs):
        raise AssertionError("legacy replay bypassed the durable source_ref binding")

    monkeypatch.setattr(storage, "find_file_by_content_hash", content_fallback_must_not_authorize)
    replay = await pipeline.ingest_file(
        tenant_id,
        None,
        content,
        filename="legacy-note.txt",
        mime_type="text/plain",
        source_ref=source_ref,
        metadata={"uploaded_by": uploader},
        force_review=True,
    )

    assert replay["idempotent_replay"] is True
    assert replay["raw_object_id"] == legacy["raw_object_id"]
    assert storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_legacy_binding_is_rechecked_inside_the_writer_transaction(
    pipeline,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = "synthetic-shared-tenant"
    uploader = "synthetic-person-a"
    source_ref = "synthetic-source:legacy-race"
    winning_bytes = b"Synthetic bytes already bound by an older worker."
    losing_bytes = b"Different synthetic bytes racing during a rolling upgrade."
    await _legacy_upload(
        pipeline,
        storage,
        tenant_id=tenant_id,
        source_ref=source_ref,
        content=winning_bytes,
        recorded_uploader=uploader,
    )

    original_find = storage.find_raw_by_source_ref
    legacy_lookups = 0

    def hide_only_the_optimistic_legacy_lookup(user_id: str, source: str, candidate_ref: str):
        nonlocal legacy_lookups
        if user_id == tenant_id and source == "upload" and candidate_ref == source_ref:
            legacy_lookups += 1
            if legacy_lookups == 1:
                return None
        return original_find(user_id, source, candidate_ref)

    monkeypatch.setattr(storage, "find_raw_by_source_ref", hide_only_the_optimistic_legacy_lookup)
    losing_digest = hashlib.sha256(losing_bytes).hexdigest()
    losing_target = pipeline._file_target(tenant_id, losing_digest, "legacy-note.txt")

    with pytest.raises(IdempotencyConflictError, match="different file"):
        await pipeline.ingest_file(
            tenant_id,
            None,
            losing_bytes,
            filename="legacy-note.txt",
            mime_type="text/plain",
            source_ref=source_ref,
            metadata={"uploaded_by": uploader},
            force_review=True,
        )

    assert legacy_lookups >= 2, "the compatible legacy key was not rechecked under the writer lock"
    assert not losing_target.exists()
    assert not list(losing_target.parent.glob(f".{losing_digest}.*.tmp"))
    assert storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded_uploader", [_MISSING, "synthetic-person-b"])
async def test_foreign_or_missing_legacy_uploader_is_not_borrowed(
    pipeline,
    storage,
    recorded_uploader: object,
) -> None:
    tenant_id = "synthetic-shared-tenant"
    current_uploader = "synthetic-person-a"
    source_ref = "synthetic-source:untrusted-legacy"
    content = b"Same synthetic bytes do not confer another uploader's authority."
    legacy = await _legacy_upload(
        pipeline,
        storage,
        tenant_id=tenant_id,
        source_ref=source_ref,
        content=content,
        recorded_uploader=recorded_uploader,
    )

    current = await pipeline.ingest_file(
        tenant_id,
        None,
        content,
        filename="current-note.txt",
        mime_type="text/plain",
        source_ref=source_ref,
        metadata={"uploaded_by": current_uploader},
        force_review=True,
    )

    assert current.get("idempotent_replay") is not True
    assert current["raw_object_id"] != legacy["raw_object_id"]
    row = storage.get_raw_object(current["raw_object_id"], tenant_id)
    assert str(row["source_ref"]).startswith("uploader:")
    assert storage.execute("SELECT COUNT(*) FROM raw_objects").fetchone()[0] == 2
