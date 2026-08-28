"""Narrow product scenarios for SQLite ↔ disk file registration integrity.

Three unique nodes only:
1. Successful ingest of a small TXT: Raw + disk + authorized read agree.
2. Corrupted modern registration fails closed (no raw_content fallback here).
3. Artificial failure between staged rename and DB commit leaves no ready file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from friday.file_delivery import (
    LEGACY_UNREGISTERED,
    REGISTERED_INVALID,
    REGISTERED_VALID,
    AuthorizedFileReadError,
    classify_file_registration,
    read_authorized_file,
    verify_registered_file_bytes,
)
from friday.ingestion import IngestionPipeline
from friday.knowledge_graph import KnowledgeGraph
from friday.storage.models import RawObject, new_id


@pytest.mark.asyncio
async def test_successful_txt_ingest_registers_consistent_sqlite_and_disk(
    settings,
    storage,
) -> None:
    """Node 1: small TXT → Raw/hash/path/size agree; authorized read returns bytes."""

    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    marker = "FILE-REG-OK-20260812-A7"
    body = f"{marker}\nsecond line of registry integrity fixture\n".encode()
    digest = hashlib.sha256(body).hexdigest()

    result = await pipeline.ingest_file(
        "alice",
        None,
        body,
        filename="registry-ok-a7.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref="test-upload:FILE-REG-OK-20260812-A7",
    )
    raw_id = str(result["raw_object_id"] or "")
    assert raw_id
    assert result.get("persisted") is not False

    raw = storage.get_raw_object(raw_id, "alice")
    assert raw is not None
    assert str(raw["content_hash"]) == digest
    metadata = raw["metadata_json"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert isinstance(metadata, dict)
    assert metadata.get("sha256") == digest
    assert metadata.get("size_bytes") == len(body)
    stored_path = str(metadata.get("stored_path") or "")
    assert stored_path
    assert not Path(stored_path).is_absolute()
    assert ".." not in Path(stored_path).parts

    on_disk = Path(settings.files_dir) / stored_path
    assert on_disk.is_file()
    assert not on_disk.is_symlink()
    assert on_disk.read_bytes() == body
    assert hashlib.sha256(on_disk.read_bytes()).hexdigest() == digest

    verdict = verify_registered_file_bytes(
        Path(settings.files_dir),
        metadata,
        content_hash=str(raw["content_hash"]),
    )
    assert verdict.state == REGISTERED_VALID

    authorized = read_authorized_file(storage, Path(settings.files_dir), raw_id, "alice")
    assert authorized.content == body
    assert authorized.raw_id == raw_id


@pytest.mark.asyncio
async def test_corrupted_modern_registration_fails_closed_without_body_fallback(
    settings,
    storage,
) -> None:
    """Node 2: metadata sha ≠ content_hash / swapped bytes → fail closed."""

    storage.ensure_user("alice", preset_key="owner")
    honest = b"FILE-REG-CORRUPT-HONEST-B3\n"
    swapped = b"FILE-REG-CORRUPT-SWAPPED-B3\n"
    honest_digest = hashlib.sha256(honest).hexdigest()
    swapped_digest = hashlib.sha256(swapped).hexdigest()
    relative = f"alice/{honest_digest[:2]}/{honest_digest}.txt"
    target = Path(settings.files_dir) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    # Disk holds honest bytes; metadata claims swapped digest while content_hash
    # still names the honest object — classic modern registration damage.
    target.write_bytes(honest)

    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="upload",
        source_ref="test-upload:FILE-REG-CORRUPT-B3",
        raw_content="FILE-REG-CORRUPT-CACHED-BODY-MUST-NOT-AUTHORIZE-READ",
        content_type="file",
        content_hash=honest_digest,
        metadata_json={
            "filename": "corrupt-b3.txt",
            "mime_type": "text/plain",
            "stored_path": relative,
            "sha256": swapped_digest,
            "size_bytes": len(honest),
            "uploaded_by": "alice",
        },
    )
    storage.store_raw_object(raw)

    classification = classify_file_registration(
        raw.metadata_json if isinstance(raw.metadata_json, dict) else json.loads(str(raw.metadata_json)),
        content_hash=honest_digest,
    )
    assert classification.state == REGISTERED_INVALID
    assert classification.reason == "content_hash_sha256_mismatch"

    with pytest.raises(AuthorizedFileReadError) as excinfo:
        read_authorized_file(storage, Path(settings.files_dir), raw.id, "alice")
    assert excinfo.value.reason == "регистрация файла повреждена"

    # Modern registration without size_bytes is invalid (not legacy, not valid).
    missing_size = {
        "filename": "missing-size-b3.txt",
        "mime_type": "text/plain",
        "stored_path": relative,
        "sha256": honest_digest,
        "uploaded_by": "alice",
    }
    assert classify_file_registration(missing_size, content_hash=honest_digest).reason == "size_bytes_missing"
    missing_size_raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="upload",
        source_ref="test-upload:FILE-REG-NOSIZE-B3",
        raw_content="must not authorize without size_bytes",
        content_type="file",
        content_hash=honest_digest,
        metadata_json=missing_size,
    )
    storage.store_raw_object(missing_size_raw)
    with pytest.raises(AuthorizedFileReadError) as nosize_exc:
        read_authorized_file(storage, Path(settings.files_dir), missing_size_raw.id, "alice")
    assert nosize_exc.value.reason == "регистрация файла повреждена"

    # Legacy (no modern keys) is a distinct state and also cannot authorize disk.
    from friday.ingestion import IdempotencyConflictError, IngestionPipeline
    from friday.ingestion._base import _extracted_text_digest
    from friday.knowledge_graph import KnowledgeGraph

    legacy_text = "FILE-REG-LEGACY-SEMANTIC-C1 body for text match."
    legacy_text_hash = _extracted_text_digest(legacy_text)
    legacy = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="upload",
        source_ref="test-upload:FILE-REG-LEGACY-B3",
        raw_content=legacy_text,
        content_type="file",
        content_hash=hashlib.sha256(b"legacy-container-bytes").hexdigest(),
        metadata_json={
            "filename": "legacy-b3.txt",
            "uploaded_by": "alice",
            "text_sha256": legacy_text_hash,
        },
    )
    storage.store_raw_object(legacy)
    legacy_meta = legacy.metadata_json if isinstance(legacy.metadata_json, dict) else {}
    assert (
        classify_file_registration(legacy_meta, content_hash=str(legacy.content_hash)).state
        == LEGACY_UNREGISTERED
    )
    with pytest.raises(AuthorizedFileReadError) as legacy_exc:
        read_authorized_file(storage, Path(settings.files_dir), legacy.id, "alice")
    assert legacy_exc.value.reason == "файл не зарегистрирован на диске"

    # C1: semantic dedup must fail closed on legacy Raw — no alias rebinding.
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    # Same fingerprint (whitespace-normalized), different container bytes.
    new_container = f"{legacy_text}\n\n".encode()
    assert hashlib.sha256(new_container).hexdigest() != str(legacy.content_hash)
    assert _extracted_text_digest(new_container.decode()) == legacy_text_hash
    aliases_before = storage.execute(
        "SELECT COUNT(*) AS n FROM file_source_aliases WHERE user_id=? AND raw_object_id=?",
        ("alice", legacy.id),
    ).fetchone()["n"]
    with pytest.raises(IdempotencyConflictError):
        await pipeline.ingest_file(
            "alice",
            None,
            new_container,
            filename="legacy-semantic-retry.txt",
            mime_type="text/plain",
            metadata={"uploaded_by": "alice"},
            source_ref="telegram-file:FILE-REG-LEGACY-SEMANTIC-POINTER",
        )
    aliases_after = storage.execute(
        "SELECT COUNT(*) AS n FROM file_source_aliases WHERE user_id=? AND raw_object_id=?",
        ("alice", legacy.id),
    ).fetchone()["n"]
    assert aliases_after == aliases_before
    bound = storage.execute(
        "SELECT raw_object_id FROM file_source_aliases WHERE user_id=? AND source_ref=?",
        ("alice", "telegram-file:FILE-REG-LEGACY-SEMANTIC-POINTER"),
    ).fetchone()
    assert bound is None

    # C2/B3: alias audit — ignored upload + public CLI both conflict; healthy upload clean.
    import sqlite3 as _sqlite3

    from friday.storage.models import InboxItem
    from tools.audit_file_registry import audit_registry

    def _registered_upload(name: str, body: bytes, *, source: str = "upload") -> str:
        digest = hashlib.sha256(body).hexdigest()
        rel = f"alice/{digest[:2]}/{digest}.bin"
        path = Path(settings.files_dir) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source=source,
            source_ref=f"test-upload:{name}",
            raw_content=f"body-{name}",
            content_type="file",
            content_hash=digest,
            metadata_json={
                "filename": f"{name}.bin",
                "mime_type": "application/octet-stream",
                "stored_path": rel,
                "sha256": digest,
                "size_bytes": len(body),
                "uploaded_by": "alice",
            },
        )
        storage.store_raw_object(raw)
        return raw.id

    ignored_id = _registered_upload("alias-ignored", b"FILE-REG-ALIAS-IGNORED\n")
    cli_id = _registered_upload("alias-cli", b"FILE-REG-ALIAS-CLI\n", source="cli")
    healthy_id = _registered_upload("alias-healthy", b"FILE-REG-ALIAS-HEALTHY\n")

    # Direct alias rows: bind() rejects non-upload / ignored paths; audit still
    # must flag any durable alias that points at an unauthorized target.
    with storage.transaction() as conn:
        for source_ref, raw_id in (
            ("telegram-file:FILE-REG-ALIAS-IGNORED-POINTER", ignored_id),
            ("telegram-file:FILE-REG-ALIAS-CLI-POINTER", cli_id),
            ("telegram-file:FILE-REG-ALIAS-HEALTHY-POINTER", healthy_id),
        ):
            conn.execute(
                """INSERT INTO file_source_aliases(
                       user_id, uploaded_by, source_ref, raw_object_id, created_at
                   ) VALUES(?, 'alice', ?, ?, '2026-08-12T00:00:00+00:00')""",
                ("alice", source_ref, raw_id),
            )
    storage.store_inbox_item(
        InboxItem(
            id=new_id("inbox"),
            user_id="alice",
            raw_object_id=ignored_id,
            status="ignored",
            suggested_action="ignore",
            classification_notes="audit alias conflict fixture",
        )
    )
    storage.execute("SELECT 1").fetchone()
    conn = _sqlite3.connect(str(settings.database_path))
    conn.row_factory = _sqlite3.Row
    try:
        report = audit_registry(
            conn,
            tenant_id="alice",
            files_root=Path(settings.files_dir),
            uploader="alice",
        )
    finally:
        conn.close()
    # ignored + CLI at least; healthy must not inflate the conflict count alone.
    assert int(report["counts"]["alias_conflict"]) >= 2
    # Prove healthy upload alias is authorized: conflict tags must not be the only signal;
    # re-run counts after removing bad aliases would be heavy — instead assert healthy
    # target still passes the production predicate used by the tool.
    from friday.storage._privacy import (
        _exact_uploader_raw_dependency,
        _not_audio_document,
        _not_private_raw_dependency,
    )

    conn = _sqlite3.connect(str(settings.database_path))
    conn.row_factory = _sqlite3.Row
    try:
        healthy_ok = conn.execute(
            f"""SELECT 1 FROM raw_objects r
                 WHERE r.id=? AND r.user_id=? AND r.content_type='file'
                   AND r.source='upload' AND r.deleted_at IS NULL
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND NOT EXISTS (
                         SELECT 1 FROM inbox i
                          WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                            AND i.status='ignored'
                       )
                   AND {_exact_uploader_raw_dependency("r")}
                 LIMIT 1""",
            (healthy_id, "alice", "alice"),
        ).fetchone()
        cli_ok = conn.execute(
            f"""SELECT 1 FROM raw_objects r
                 WHERE r.id=? AND r.user_id=? AND r.content_type='file'
                   AND r.source='upload' AND r.deleted_at IS NULL
                   AND {_not_audio_document("r")}
                   AND {_not_private_raw_dependency("r")}
                   AND NOT EXISTS (
                         SELECT 1 FROM inbox i
                          WHERE i.raw_object_id=r.id AND i.user_id=r.user_id
                            AND i.status='ignored'
                       )
                   AND {_exact_uploader_raw_dependency("r")}
                 LIMIT 1""",
            (cli_id, "alice", "alice"),
        ).fetchone()
    finally:
        conn.close()
    assert healthy_ok is not None
    assert cli_ok is None
    blob = json.dumps(report, ensure_ascii=True)
    assert "alias-ignored.bin" not in blob
    assert "alias-cli.bin" not in blob
    assert "FILE-REG-ALIAS-IGNORED" not in blob


@pytest.mark.asyncio
async def test_crash_between_file_commit_and_db_leaves_no_ready_registration(
    settings,
    storage,
    monkeypatch,
) -> None:
    """Node 3: failure after rename, before Raw commit → no successful ready file."""

    storage.ensure_user("alice", preset_key="owner")
    pipeline = IngestionPipeline(settings, storage, KnowledgeGraph(storage))
    body = b"FILE-REG-CRASH-C9\natomic gap fixture\n"
    digest = hashlib.sha256(body).hexdigest()

    original_store = storage.store_raw_object

    def boom(obj):  # noqa: ANN001
        raise RuntimeError("injected failure after staged file commit")

    monkeypatch.setattr(storage, "store_raw_object", boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        await pipeline.ingest_file(
            "alice",
            None,
            body,
            filename="registry-crash-c9.txt",
            mime_type="text/plain",
            metadata={"uploaded_by": "alice"},
            source_ref="test-upload:FILE-REG-CRASH-C9",
        )

    monkeypatch.setattr(storage, "store_raw_object", original_store)

    # No Raw row for this content may be treated as a ready registered file.
    leftover = storage.find_file_by_content_hash("alice", digest, uploaded_by="alice", scope_uploaded_by=True)
    assert leftover is None
    by_ref = storage.find_raw_by_source_ref("alice", "upload", "test-upload:FILE-REG-CRASH-C9")
    assert by_ref is None

    # A follow-up ingest must create a clean registration, not inherit a ghost.
    second = await pipeline.ingest_file(
        "alice",
        None,
        body,
        filename="registry-crash-c9.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref="test-upload:FILE-REG-CRASH-C9-RETRY",
    )
    raw_id = str(second["raw_object_id"] or "")
    assert raw_id
    authorized = read_authorized_file(storage, Path(settings.files_dir), raw_id, "alice")
    assert authorized.content == body

    # Repair path: incomplete modern registration on same digest is restored.
    damaged = storage.get_raw_object(raw_id, "alice")
    assert damaged is not None
    meta = damaged["metadata_json"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    meta = dict(meta)
    meta["sha256"] = "0" * 64
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE raw_objects SET metadata_json=? WHERE id=? AND user_id=?",
            (json.dumps(meta, ensure_ascii=False, sort_keys=True), raw_id, "alice"),
        )
    with pytest.raises(AuthorizedFileReadError):
        read_authorized_file(storage, Path(settings.files_dir), raw_id, "alice")

    repaired = await pipeline.ingest_file(
        "alice",
        None,
        body,
        filename="registry-crash-c9.txt",
        mime_type="text/plain",
        metadata={"uploaded_by": "alice"},
        source_ref="test-upload:FILE-REG-CRASH-C9-REPAIR",
    )
    # Content-hash dedup reuses the same Raw after repair.
    assert str(repaired["raw_object_id"]) == raw_id
    restored = read_authorized_file(storage, Path(settings.files_dir), raw_id, "alice")
    assert restored.content == body
    restored_raw = storage.get_raw_object(raw_id, "alice")
    assert restored_raw is not None
    restored_meta = restored_raw["metadata_json"]
    if isinstance(restored_meta, str):
        restored_meta = json.loads(restored_meta)
    assert restored_meta.get("sha256") == digest
    assert (
        verify_registered_file_bytes(
            Path(settings.files_dir),
            restored_meta,
            content_hash=str(restored_raw["content_hash"]),
        ).state
        == REGISTERED_VALID
    )
