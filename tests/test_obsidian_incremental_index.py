from __future__ import annotations

import hashlib
from pathlib import Path

from friday.organs.obsidian.indexing import refresh_incremental_index
from friday.organs.obsidian.service import ObsidianService
from friday.organs.obsidian.vault_store import VaultStore
from friday.storage import FridayStorage


def _bundle(storage: FridayStorage, vault: Path) -> dict:
    storage.ensure_user("alice")
    return storage.create_obsidian_bundle(
        "alice",
        config_root=str(vault.parent / "config"),
        database_root=str(vault.parent / "data"),
        api_endpoint="unix:///tmp/friday-index.sock",
        api_key_ref="secret:obsidian:alice:index",
        server_path=str(vault),
        folder_id="friday-alice-index",
        setup_token_hash=hashlib.sha256(b"index-token").hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )


def test_incremental_index_only_republishes_changed_revision_and_links(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    aggregate = _bundle(storage, vault)
    notes = ObsidianService(VaultStore(vault))
    notes.create_note("Projects/Friday", "# Friday\n")
    notes.create_note("Notes/Search", "[[Projects/Friday]]\n")

    first = refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(aggregate["vault"]["id"]),
    )
    replay = refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(aggregate["vault"]["id"]),
    )
    notes.append_note("Notes/Search", "Фиолетовый маршрутизатор", operation_id="phone-edit")
    changed = refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(aggregate["vault"]["id"]),
    )

    assert first.indexed == 2 and first.links_published == 1
    assert replay.indexed == 0 and replay.unchanged == 2
    assert changed.indexed == 1
    assert changed.changed_paths == ("Notes/Search.md",)
    bindings = storage.list_obsidian_note_bindings("alice", include_deleted=False)
    target = next(item for item in bindings if item["current_path"] == "Projects/Friday.md")
    backlinks = storage.list_obsidian_note_links(
        "alice",
        target_binding_id=str(target["id"]),
        resolution_state="resolved",
    )
    assert len(backlinks) == 1


def test_missing_file_becomes_tombstone_and_cannot_remain_in_live_index(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    aggregate = _bundle(storage, vault)
    notes = ObsidianService(VaultStore(vault))
    created = notes.create_note("Scratch/Delete Me", "temporary")
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(aggregate["vault"]["id"]),
    )
    notes.store.delete(created.path, expected_revision=created.revision)

    result = refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(aggregate["vault"]["id"]),
    )

    assert result.tombstoned == 1
    assert storage.list_obsidian_note_bindings("alice") == []
    deleted = storage.list_obsidian_note_bindings("alice", include_deleted=True)
    assert len(deleted) == 1 and deleted[0]["deleted_at"] is not None
    assert storage.get_obsidian_note_index("alice", str(deleted[0]["id"])) is None
