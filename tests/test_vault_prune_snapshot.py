"""The vault prune set must be a snapshot, not a sum of pages.

`_vault_sync` renders the corpus page by page and then deletes every note whose
object is not in the set it collected. The set used to be accumulated FROM those
pages — and the pages are ordered by `importance DESC, updated_at DESC`, both of
which change under concurrent edits. A row that moves across a page boundary
between two reads appears in NEITHER page, so a live object's note is deleted
from the user's vault while the object itself is perfectly healthy.

Editing a Knowledge Object is the commonest thing there is, and the vault sync
runs every five minutes, so the two overlap by construction.
"""

from __future__ import annotations

import hashlib

import pytest

from friday import workers as workers_module
from friday.memory import MemoryVault
from friday.storage.models import Entity, EntityType, KnowledgeObject, RawObject, new_id
from friday.workers import WorkersManager


def _user_notes(tmp_path):
    """The per-user directory carries an id digest, so it is found, not spelled."""
    users = tmp_path / "vault" / "users"
    directories = [path for path in users.iterdir() if path.is_dir()] if users.exists() else []
    assert len(directories) == 1, directories
    return sorted(path for path in directories[0].glob("*.md") if path.name != "README.md")


def _make_ko(storage, user_id: str, title: str, *, importance: float) -> str:
    content = f"Тело заметки {title}."
    raw = RawObject(
        id=new_id("raw"),
        user_id=user_id,
        source="test",
        source_ref=new_id("source"),
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
        importance=importance,
    )
    storage.store_knowledge_object(ko)
    return ko.id


@pytest.mark.asyncio
async def test_an_edit_between_pages_does_not_delete_a_live_note(settings, storage, tmp_path, monkeypatch):
    """A concurrent edit reorders the pages; every live note must survive it."""
    # Two objects per page, six objects: three pages, two boundaries to fall
    # through. With one page there is no boundary and the broken version passes.
    monkeypatch.setattr(workers_module, "_VAULT_PAGE", 2)
    storage.ensure_user("alice")
    ids = [_make_ko(storage, "alice", f"Заметка {index}", importance=index / 20) for index in range(6)]

    vault = MemoryVault(tmp_path / "vault")
    manager = WorkersManager(settings, storage, None, None)
    manager.memory_vault = vault

    # First pass renders everything, so every object has a note on disk.
    await manager._vault_sync("alice")  # noqa: SLF001
    assert len(_user_notes(tmp_path)) == len(ids)

    # The interleaving. Pages come back `importance DESC`, so the LEAST important
    # object is on the last page. After page 1 is read, it is bumped to the top:
    # it moves BACKWARD into a region already scanned, every later page shifts by
    # one, and the object is never returned at all. That is the skew — an object
    # that is perfectly alive and appears in no page of this sweep.
    real_list = storage.list_knowledge_objects
    pages = {"count": 0}

    def list_with_an_edit_after_the_first_page(*args, **kwargs):
        objects = real_list(*args, **kwargs)
        pages["count"] += 1
        if pages["count"] == 1:
            storage.update_knowledge_fields(ids[0], "alice", importance=0.99)
        return objects

    monkeypatch.setattr(storage, "list_knowledge_objects", list_with_an_edit_after_the_first_page)
    await manager._vault_sync("alice")  # noqa: SLF001

    survivors = _user_notes(tmp_path)
    assert len(survivors) == len(ids), f"a live note was pruned: {sorted(path.name for path in survivors)}"


@pytest.mark.asyncio
async def test_a_deleted_object_still_loses_its_note(settings, storage, tmp_path):
    """The prune must keep doing its job: a soft-deleted object leaves no plaintext."""
    storage.ensure_user("alice")
    keep = _make_ko(storage, "alice", "Живая", importance=0.5)
    drop = _make_ko(storage, "alice", "Удалённая", importance=0.5)

    vault = MemoryVault(tmp_path / "vault")
    manager = WorkersManager(settings, storage, None, None)
    manager.memory_vault = vault
    await manager._vault_sync("alice")  # noqa: SLF001
    assert len(list(_user_notes(tmp_path))) == 2

    storage.soft_delete_knowledge_object(drop, "alice")
    await manager._vault_sync("alice")  # noqa: SLF001

    remaining = [path.name for path in _user_notes(tmp_path)]
    assert len(remaining) == 1, remaining
    assert keep[-8:] in remaining[0] or "Живая" in remaining[0]


@pytest.mark.asyncio
async def test_quarantine_after_start_snapshot_removes_plaintext_note(
    settings,
    storage,
    tmp_path,
    monkeypatch,
):
    """A stale initial live set cannot retain a KO that became private mid-sync."""

    user_id = "alice"
    sentinel = "PRIVATE VAULT RACE SENTINEL"
    storage.ensure_user(user_id)
    knowledge_id = _make_ko(storage, user_id, sentinel, importance=0.5)
    hidden = storage.create_entity(
        Entity(
            id="ent-vault-race-private",
            user_id=user_id,
            name="Private vault dependency",
            entity_type=EntityType.EVENT,
        )
    )
    storage.link_knowledge_entity(user_id, knowledge_id, hidden.id, status="accepted")

    vault = MemoryVault(tmp_path / "vault")
    manager = WorkersManager(settings, storage, None, None)
    manager.memory_vault = vault
    await manager._vault_sync(user_id)  # noqa: SLF001
    notes = _user_notes(tmp_path)
    assert len(notes) == 1 and sentinel in notes[0].read_text(encoding="utf-8")

    real_list_live = storage.list_live_knowledge_ids
    snapshots = {"count": 0}

    def quarantine_after_first_snapshot(*args, **kwargs):
        live = real_list_live(*args, **kwargs)
        snapshots["count"] += 1
        if snapshots["count"] == 1:
            with storage.transaction() as conn:
                conn.execute(
                    """INSERT INTO private_entity_owners(
                           entity_id, person_id, privacy_kind, created_at
                       ) VALUES(?, ?, 'reminder', ?)""",
                    (hidden.id, "bob", "2026-08-05T00:00:00Z"),
                )
        return live

    monkeypatch.setattr(storage, "list_live_knowledge_ids", quarantine_after_first_snapshot)
    await manager._vault_sync(user_id)  # noqa: SLF001

    assert snapshots["count"] == 2
    assert _user_notes(tmp_path) == []
    assert sentinel not in "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "vault").rglob("*.md")
    )
