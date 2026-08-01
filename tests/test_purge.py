"""Hard-delete / purge — the §9 coordinated destruction path.

Soft delete only sets a tombstone; the audit found that purged-in-name knowledge
physically survived in the FTS index and no code removed rows, raw files, vault
copies, or graph links. These tests pin the two-phase policy (purge refuses
non-soft-deleted objects), full multi-table + FTS cleanup, deduplicated-file
safety, retention-window eligibility, and admin-only capability gating.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from friday.memory import MemoryVault
from friday.permissions import ActorContext, AuthorizationService
from friday.purge import purge_knowledge
from friday.retrieval import pack_vector
from friday.storage import SCHEMA_VERSION
from friday.storage.models import FeedbackItem, FeedbackType, KnowledgeObject, RawObject, new_id


def _text_ko(storage, user_id: str, content: str, *, title: str) -> dict:
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("src"),
        raw_content=content,
        content_type="text",
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content=content,
        content_type="text",
        title=title,
        summary=content,
    )
    storage.store_knowledge_object(ko)
    return storage.get_knowledge_object(ko.id, user_id) or {}


def _file_ko(storage, settings, user_id: str, payload: bytes, *, title: str, digest: str) -> tuple[str, Path]:
    file_dir = settings.files_dir / user_id / digest[:2]
    file_dir.mkdir(parents=True, exist_ok=True)
    file_path = file_dir / f"{digest}.bin"
    file_path.write_bytes(payload)
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="upload",
        source_ref=new_id("src"),
        raw_content="",
        content_type="file",
        content_hash=digest,
        metadata_json={"stored_path": str(file_path), "sha256": digest},
    )
    storage.store_raw_object(raw)
    ko = KnowledgeObject(
        id=new_id("ko"),
        user_id=user_id,
        raw_object_id=raw.id,
        content="[file]",
        content_type="file",
        title=title,
        summary="s",
    )
    storage.store_knowledge_object(ko)
    return ko.id, file_path


def _count(storage, sql: str, params: tuple) -> int:
    row = storage.execute(sql, params).fetchone()
    return int(row[0]) if row else 0


def test_purge_refuses_objects_not_soft_deleted(storage):
    ko = _text_ko(storage, "alice", "Активный объект", title="A")
    with pytest.raises(ValueError, match="soft-deleted"):
        storage.purge_knowledge_object(ko["id"], "alice")
    assert storage.get_knowledge_object(ko["id"], "alice") is not None


def test_purge_removes_every_trace_including_fts(storage):
    ko = _text_ko(storage, "alice", "Уникальнейшийтокен про проект", title="Проект")
    ko_id = ko["id"]
    raw_id = ko["raw_object_id"]

    storage.update_knowledge_fields(ko_id, "alice", content="Уникальнейшийтокен про проект второй")
    storage.upsert_knowledge_embeddings(
        [
            {
                "knowledge_object_id": ko_id,
                "user_id": "alice",
                "model": "m",
                "dim": 3,
                "source_version": 2,
                "content_hash": "h",
                "vector": pack_vector([1.0, 0.0, 0.0]),
            }
        ]
    )
    storage.upsert_knowledge_vectors(
        [],
        {
            ko_id: [
                {
                    "chunk_index": index,
                    "user_id": "alice",
                    "model": "m",
                    "dim": 3,
                    "source_version": 2,
                    "chunk_scheme": "v1:1200:200:64",
                    "start_char": index * 10,
                    "end_char": index * 10 + 10,
                    "content_hash": f"h{index}",
                    "vector": pack_vector([1.0, 0.0, 0.0]),
                }
                for index in range(3)
            ]
        },
    )
    storage.record_knowledge_usage("alice", [ko_id], used_in_answer=True)
    storage.store_feedback(
        FeedbackItem(
            id=new_id("fb"),
            user_id="alice",
            target_type="knowledge_object",
            target_id=ko_id,
            feedback_type=FeedbackType.ANSWER_USEFULNESS,
            score=1.0,
        )
    )

    assert (
        _count(
            storage, "SELECT COUNT(*) FROM knowledge_object_versions WHERE knowledge_object_id=?", (ko_id,)
        )
        >= 1
    )
    assert storage.count_knowledge_embeddings("alice") == 1
    assert storage.count_knowledge_chunk_embeddings("alice") == 3
    assert _count(storage, "SELECT COUNT(*) FROM knowledge_usage WHERE knowledge_object_id=?", (ko_id,)) == 1
    assert _count(storage, "SELECT COUNT(*) FROM feedback WHERE target_id=?", (ko_id,)) == 1

    fts = storage._fts_available
    if fts:
        match = "SELECT COUNT(*) FROM knowledge_fts WHERE knowledge_fts MATCH ?"
        assert _count(storage, match, ("Уникальнейшийтокен",)) == 1

    assert storage.soft_delete_knowledge_object(ko_id, "alice")
    # The §9 gap: soft delete leaves the content physically in the FTS index.
    if fts:
        assert _count(storage, match, ("Уникальнейшийтокен",)) == 1

    report = storage.purge_knowledge_object(ko_id, "alice")
    assert report["existed"] is True
    assert report["deleted"]["knowledge_objects"] == 1
    assert report["raw_removed"] is True

    assert storage.get_knowledge_object(ko_id, "alice") is None
    assert (
        _count(
            storage, "SELECT COUNT(*) FROM knowledge_object_versions WHERE knowledge_object_id=?", (ko_id,)
        )
        == 0
    )
    assert storage.count_knowledge_embeddings("alice") == 0
    assert storage.count_knowledge_chunk_embeddings("alice") == 0
    # A surviving chunk row would be an orphan, and an orphan makes foreign_key_check
    # non-empty — which makes create_backup delete its own backup and raise. The first
    # symptom of that class of bug is "backups stopped working", so assert both.
    assert storage.execute("PRAGMA foreign_key_check").fetchall() == []
    assert storage.create_backup(label="purge-check")["schema_version"] == SCHEMA_VERSION
    assert _count(storage, "SELECT COUNT(*) FROM knowledge_usage WHERE knowledge_object_id=?", (ko_id,)) == 0
    assert _count(storage, "SELECT COUNT(*) FROM feedback WHERE target_id=?", (ko_id,)) == 0
    assert _count(storage, "SELECT COUNT(*) FROM raw_objects WHERE id=?", (raw_id,)) == 0
    if fts:
        assert _count(storage, match, ("Уникальнейшийтокен",)) == 0


def test_purge_deletes_raw_file_and_vault_copy(storage, settings):
    digest = hashlib.sha256(b"filebytes").hexdigest()
    ko_id, file_path = _file_ko(storage, settings, "alice", b"filebytes", title="Файл", digest=digest)
    vault = MemoryVault(settings.memory_vault_dir)
    vault_path = vault.sync_object(storage.get_knowledge_object(ko_id, "alice"))
    assert vault_path and Path(vault_path).is_file()
    assert file_path.is_file()

    storage.soft_delete_knowledge_object(ko_id, "alice")
    report = purge_knowledge(storage, settings, vault, ko_id, "alice")

    assert report["raw_removed"] is True
    assert report["file_unlinked"] is True
    assert report["vault_removed"] is True
    assert not file_path.exists()
    assert not Path(vault_path).exists()


def test_purge_keeps_a_deduplicated_file_until_the_last_reference(storage, settings):
    digest = hashlib.sha256(b"shared-bytes").hexdigest()
    ko1, file_path = _file_ko(storage, settings, "alice", b"shared-bytes", title="A", digest=digest)
    ko2, same_path = _file_ko(storage, settings, "alice", b"shared-bytes", title="B", digest=digest)
    assert file_path == same_path
    vault = MemoryVault(settings.memory_vault_dir)

    storage.soft_delete_knowledge_object(ko1, "alice")
    storage.soft_delete_knowledge_object(ko2, "alice")

    first = purge_knowledge(storage, settings, vault, ko1, "alice")
    assert first["raw_removed"] is True
    assert first["unlink_file"] is False  # a sibling Raw Object still shares the file
    assert file_path.exists()

    second = purge_knowledge(storage, settings, vault, ko2, "alice")
    assert second["unlink_file"] is True
    assert not file_path.exists()


def test_list_purgeable_respects_retention_window(storage):
    ko = _text_ko(storage, "alice", "объект", title="X")
    assert storage.list_purgeable_knowledge(older_than_days=0) == []

    storage.soft_delete_knowledge_object(ko["id"], "alice")
    eligible_now = storage.list_purgeable_knowledge(older_than_days=0)
    assert [row["id"] for row in eligible_now] == [ko["id"]]
    # Just-deleted objects are not yet past a 30-day retention window.
    assert storage.list_purgeable_knowledge(older_than_days=30) == []


def test_purge_capability_is_admin_and_owner_only(storage):
    from friday.permissions import CORE_CAPABILITIES

    cap = next(c for c in CORE_CAPABILITIES if c.security_id == "admin.data.purge")
    assert cap.risk_level == 4
    assert cap.default_presets == ("admin",)

    auth = AuthorizationService(storage)

    def _allowed(preset: str) -> bool:
        actor = ActorContext(user_id=f"u-{preset}", preset_key=preset, source="test")
        return auth.authorize(actor, "admin.data.purge").allowed

    assert _allowed("owner") is True
    assert _allowed("admin") is True
    assert _allowed("moderator") is False
    assert _allowed("user") is False
    assert _allowed("guest") is False
