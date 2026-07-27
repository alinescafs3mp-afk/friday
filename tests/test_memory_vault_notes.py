"""The vault has to be usable by a person opening it outside Jericho.

Until now a note was `ko_<12 hex>.md` with no links: the folder was a pile of
hashes, and Obsidian's graph view — the reason to keep a Markdown projection at
all — was empty. These tests pin the readable half (name follows the title,
identity follows the id) and the link half, plus the two ways the old naming
scheme could have broken while looking fine: a stale twin after a retitle, and
a delete that misses the file because the title moved.
"""

from __future__ import annotations

import json

import pytest

from jericho.memory import MemoryVault

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
