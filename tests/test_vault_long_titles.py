"""A title nobody can type by hand still has to be a filename.

`_note_path` clipped the title slug to 60 CHARACTERS while the filesystem limit is
255 BYTES, and the name that actually has to fit is not the note itself but its
`mkstemp` twin — `.<stem>.<8 random>.tmp`, another 28 bytes. Sixty emoji is 240
bytes of slug, so `sync_object` raised OSError before its own try block, the whole
vault page died with it, pagination stopped, and `prune_orphans` never ran: the
plaintext of deleted and ignored objects stayed on disk for good.

Nothing about this needs a malicious user. `_generate_title` takes the first line
of an incoming message, so a note that opens with a row of emoji mints the title
on its own.
"""

from __future__ import annotations

import hashlib

import pytest

from friday.memory import MemoryVault
from friday.storage.models import KnowledgeObject, RawObject, new_id
from friday.workers import _sync_vault_page


def _object(title: str, content: str = "Тело заметки.") -> dict:
    return {
        "id": new_id("ko"),
        "user_id": "usr_owner",
        "title": title,
        "content": content,
        "tags_json": "[]",
        "metadata_json": "{}",
    }


@pytest.mark.parametrize(
    ("label", "title"),
    [
        ("emoji", "🎉" * 80),
        ("math bold", "𝐇𝐞𝐥𝐥𝐨 " * 20),
        ("cyrillic", "Очень длинный русский заголовок " * 10),
        ("mixed", "Отчёт 🎉 " * 30),
    ],
)
def test_a_wide_title_still_produces_a_writable_note(tmp_path, label, title):
    vault = MemoryVault(tmp_path)
    written = vault.sync_object(_object(title))
    assert written is not None, f"{label}: nothing was written"
    assert written.exists()
    # The temp twin is the longest name the write ever creates, so it is the one
    # that has to fit — not the note.
    twin = len(f".{written.stem}.".encode()) + len("XXXXXXXX.tmp")
    assert twin <= 255, f"{label}: the mkstemp twin would be {twin} bytes"


def test_a_poison_title_does_not_freeze_the_rest_of_the_vault(tmp_path, storage):
    """The page must survive an object it cannot write, and keep going.

    The byte clip above is the fix for the one cause we found; this is the second
    layer, and it must hold for causes we have not found. So the failure here is
    injected rather than produced by a title: a full disk, a permission change or a
    filename limit we have not thought of all arrive as OSError from `sync_object`,
    and any of them used to stop the tenant's whole vault — including the
    `prune_orphans` call that keeps deleted notes from lingering in plaintext.
    """
    storage.ensure_user("usr_owner")
    ids = []
    for title in ("Первый", "Ядовитый", "Третий"):
        content = f"Содержимое для {title}"
        raw = RawObject(
            id=new_id("raw"),
            user_id="usr_owner",
            source="test",
            source_ref=new_id("src"),
            raw_content=content,
            content_type="text",
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        ko = KnowledgeObject(
            id=new_id("ko"),
            user_id="usr_owner",
            raw_object_id=raw.id,
            content=content,
            content_type="text",
            title=title,
        )
        storage.store_knowledge_object(ko)
        ids.append(ko.id)

    real = MemoryVault(tmp_path)

    class OneBadObject:
        def sync_object(self, ko):
            if str(ko.get("title") or "") == "Ядовитый":
                raise OSError(36, "File name too long")
            return real.sync_object(ko)

    objects = [dict(storage.get_knowledge_object(ko_id, "usr_owner")) for ko_id in ids]
    _sync_vault_page(OneBadObject(), storage, objects)

    names = [path.name for path in (tmp_path / "users").glob("*/*.md")]
    assert any(name.startswith("Первый--") for name in names)
    assert any(name.startswith("Третий--") for name in names), (
        f"an object after the unwritable one never reached the vault: {names}"
    )
