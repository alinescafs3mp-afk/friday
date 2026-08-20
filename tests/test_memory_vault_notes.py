"""The vault has to be usable by a person opening it outside Friday.

Until now a note was `ko_<12 hex>.md` with no links: the folder was a pile of
hashes, and Obsidian's graph view — the reason to keep a Markdown projection at
all — was empty. These tests pin the readable half (name follows the title,
identity follows the id) and the link half, plus the two ways the old naming
scheme could have broken while looking fine: a stale twin after a retitle, and
a delete that misses the file because the title moved.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from friday.memory import MemoryVault, VaultProjectionBoundaryError

USER = "telegram:telegram:777"


@pytest.fixture()
def vault(tmp_path):
    return MemoryVault(tmp_path / "vault")


def _ko(**overrides):
    record = {
        "id": "ko_1a2b3c",
        "user_id": USER,
        "title": "Аренда квартиры",
        "summary": "краткое",
        "content": "тело заметки",
        "tags_json": json.dumps(["дом"]),
        "importance": 0.6,
        "lifecycle_stage": "active",
        "version": 1,
        "raw_object_id": "raw_1",
    }
    record.update(overrides)
    return record


def test_note_filename_carries_the_russian_title(vault):
    path = vault.sync_object(_ko())
    assert path is not None
    assert path.name.startswith("Аренда квартиры--")
    # The identity half stays ASCII hex, so the id -> file mapping is exact.
    assert path.stem.rsplit("--", 1)[-1].isalnum()


def test_an_unchanged_note_is_not_rewritten(vault):
    """Цикл синхронизации рендерит ВЕСЬ корпус каждые 5 минут; mkstemp + fsync +
    replace на каждую нетронутую заметку — это ~442 тысячи fsync и гигабайты
    паразитной записи в сутки на корпусе, который никто не редактировал, на том
    же диске, где живёт база. Неизменённая заметка не должна трогать диск."""
    first = vault.sync_object(_ko())
    assert first is not None
    before = os.stat(first)

    second = vault.sync_object(_ko())
    assert second == first
    after = os.stat(first)
    # os.replace даёт новый inode; неизменённый файл обязан сохранить прежний.
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    changed = vault.sync_object(_ko(content="новое тело заметки"))
    assert changed == first
    assert os.stat(first).st_ino != before.st_ino, "изменённая заметка не переписана"


def test_unchanged_private_note_does_not_fsync(vault, monkeypatch):
    path = vault.sync_object(_ko())
    assert path is not None
    fsync_calls: list[int] = []
    monkeypatch.setattr(os, "fsync", fsync_calls.append)

    assert vault.sync_object(_ko()) == path

    assert fsync_calls == []


def test_unchanged_note_repairs_permissions_without_rewriting(vault, monkeypatch):
    path = vault.sync_object(_ko())
    assert path is not None
    original_inode = path.stat().st_ino
    original_body = path.read_bytes()
    path.chmod(0o644)
    real_fsync = os.fsync
    fsync_calls: list[int] = []

    def record_fsync(descriptor: int) -> None:
        fsync_calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record_fsync)

    assert vault.sync_object(_ko()) == path

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_ino == original_inode
    assert path.read_bytes() == original_body
    assert fsync_calls


def test_unchanged_sync_removes_a_crash_stale_twin(vault):
    canonical = vault.sync_object(_ko())
    assert canonical is not None
    original_inode = canonical.stat().st_ino
    stale = canonical.with_name(f"old-title--{canonical.stem.rsplit('--', 1)[-1]}.md")
    stale.write_bytes(canonical.read_bytes())

    assert vault.sync_object(_ko()) == canonical

    assert canonical.stat().st_ino == original_inode
    assert canonical.is_file()
    assert not stale.exists()


def test_retitling_renames_the_note_instead_of_leaving_a_twin(vault):
    first = vault.sync_object(_ko())
    second = vault.sync_object(_ko(title="Аренда квартиры на Мира"))
    assert first is not None and second is not None
    assert first != second
    assert not first.exists()
    assert [note["title"] for note in vault.read_vault(USER)] == ["Аренда квартиры на Мира"]


def test_delete_finds_the_note_after_a_retitle(vault):
    vault.sync_object(_ko())
    path = vault.sync_object(_ko(title="Совсем другое имя"))
    assert path is not None
    vault.delete_object("ko_1a2b3c", USER)
    assert not path.exists()
    assert vault.read_vault(USER) == []


def test_account_directory_symlink_cannot_escape_delete_prune_or_read(
    tmp_path: Path,
) -> None:
    vault = MemoryVault(tmp_path / "vault")
    external = tmp_path / "outside-vault"
    external.mkdir()
    account = vault._user_dir("alice")  # noqa: SLF001
    account.symlink_to(external, target_is_directory=True)
    digest = vault._note_stem("ko_escape")  # noqa: SLF001
    outside_note = external / f"not-a-vault-note--{digest}.md"
    sentinel = "OUTSIDE-VAULT-SENTINEL"
    outside_note.write_text(sentinel, encoding="utf-8")

    with pytest.raises(VaultProjectionBoundaryError):
        vault.delete_object("ko_escape", "alice")
    with pytest.raises(VaultProjectionBoundaryError):
        vault.prune_orphans("alice", [])
    assert vault.read_vault("alice") == []
    assert outside_note.read_text(encoding="utf-8") == sentinel


def test_targeted_delete_removes_final_and_crash_temp_but_not_unrelated_temp(vault) -> None:
    final = vault.sync_object(_ko())
    assert final is not None
    crash_temp = final.parent / f".{final.stem}.crash123.tmp"
    crash_temp.write_text("full plaintext crash copy", encoding="utf-8")
    unrelated = final.parent / ".other--000000000000.crash123.tmp"
    unrelated.write_text("unrelated", encoding="utf-8")

    removed = vault.delete_object("ko_1a2b3c", USER)

    assert removed == 2
    assert not final.exists() and not crash_temp.exists()
    assert unrelated.read_text(encoding="utf-8") == "unrelated"


def test_prune_orphans_keeps_live_notes_and_the_readme(vault):
    live = vault.sync_object(_ko())
    dead = vault.sync_object(_ko(id="ko_dead", title="Устаревшее"))
    assert live is not None and dead is not None
    removed = vault.prune_orphans(USER, ["ko_1a2b3c"])
    assert removed == 1
    assert live.exists() and not dead.exists()
    assert (live.parent / "README.md").is_file()


def test_windows_forbidden_characters_never_reach_the_filename(vault):
    path = vault.sync_object(_ko(title='Отчёт: Q1/Q2 <черновик> "v2" | 50%?'))
    assert path is not None and path.is_file()
    assert not set(path.name) & set('<>:"/\\|?*')
    assert not path.stem.endswith((".", " "))


def test_two_objects_with_the_same_title_do_not_collide(vault):
    first = vault.sync_object(_ko(id="ko_one"))
    second = vault.sync_object(_ko(id="ko_two"))
    assert first is not None and second is not None
    assert first != second
    assert first.exists() and second.exists()
    assert len(vault.read_vault(USER)) == 2


def test_entity_names_are_rendered_as_wikilinks(vault):
    path = vault.sync_object(_ko(_entity_names=["Квартира на Мира", "Иван", "Иван"]))
    assert path is not None
    body = path.read_text(encoding="utf-8")
    assert "## Связи" in body
    # Deduplicated, order preserved: a repeated link is noise in the graph.
    assert body.count("[[Иван]]") == 1
    assert body.index("[[Квартира на Мира]]") < body.index("[[Иван]]")


def test_a_note_without_entities_has_no_empty_links_section(vault):
    path = vault.sync_object(_ko(_entity_names=[]))
    assert path is not None
    assert "## Связи" not in path.read_text(encoding="utf-8")


def test_readme_names_the_tenant_and_is_not_a_note(vault):
    path = vault.sync_object(_ko())
    assert path is not None
    readme = path.parent / "README.md"
    assert USER in readme.read_text(encoding="utf-8")
    # Read back with no filter: the README must not surface as a knowledge note.
    assert [note["id"] for note in vault.read_vault()] == ["ko_1a2b3c"]


def test_readme_is_not_rewritten_once_the_owner_edits_it(vault):
    path = vault.sync_object(_ko())
    assert path is not None
    readme = path.parent / "README.md"
    readme.write_text("мои заметки\n", encoding="utf-8")
    vault.sync_object(_ko(title="Другое"))
    assert readme.read_text(encoding="utf-8") == "мои заметки\n"


def test_an_old_scheme_note_is_renamed_rather_than_duplicated(vault):
    """Every note already on disk is `ko_<hash>--<digest>.md`. It has to convert.

    The digest half is the same in both schemes, so the stale-twin sweep matches the
    old name and the first sync after the upgrade renames the file in place. Had the
    two schemes not shared it, every user would have silently ended up with two
    copies of every note — the second one never updated again.
    """
    user_dir = vault._user_dir(USER)  # noqa: SLF001
    user_dir.mkdir(parents=True, exist_ok=True)
    new_path = vault._note_path(user_dir, _ko())  # noqa: SLF001
    legacy = user_dir / f"ko_1a2b3c--{new_path.stem.rsplit('--', 1)[-1]}.md"
    legacy.write_text("старое содержимое\n", encoding="utf-8")

    written = vault.sync_object(_ko())

    assert written == new_path and written.is_file()
    assert not legacy.exists()
    assert len(vault.read_vault(USER)) == 1
