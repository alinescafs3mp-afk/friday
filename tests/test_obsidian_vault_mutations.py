from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from friday.organs.obsidian.contracts import (
    NoteAlreadyExistsError,
    RevisionConflictError,
    VaultPathError,
)
from friday.organs.obsidian.vault_store import VaultStore


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    root.mkdir()
    return root


def test_expected_revision_delete_is_durable_and_has_an_idempotent_postcondition(
    vault: Path,
) -> None:
    store = VaultStore(vault)
    created = store.write_text("Notes/Delete.md", "observed", create_only=True)

    with pytest.raises(RevisionConflictError):
        store.delete("Notes/Delete.md", expected_revision="0" * 64)
    assert store.read("Notes/Delete.md").text() == "observed"

    deleted = store.delete("Notes/Delete.md", expected_revision=created.revision)

    assert deleted.changed_paths == ("Notes/Delete.md",)
    assert deleted.changed_revisions == (("Notes/Delete.md", None),)
    assert store.delete_postcondition("Notes/Delete.md", expected_revision=created.revision)
    assert not list(vault.parent.glob(".friday-vault-*"))


def test_expected_revision_move_is_no_clobber_and_reconcilable(vault: Path) -> None:
    store = VaultStore(vault)
    source = store.write_text("Projects/Friday.md", "observed", create_only=True)
    store.write_text("Architecture/Friday.md", "peer", create_only=True)

    with pytest.raises(NoteAlreadyExistsError):
        store.move(
            "Projects/Friday.md",
            "Architecture/Friday.md",
            expected_revision=source.revision,
        )
    assert store.read("Projects/Friday.md").text() == "observed"
    assert store.read("Architecture/Friday.md").text() == "peer"

    moved = store.move(
        "Projects/Friday.md",
        "Architecture/Core.md",
        expected_revision=source.revision,
    )

    assert moved.changed_paths == ("Projects/Friday.md", "Architecture/Core.md")
    assert moved.changed_revisions == (
        ("Projects/Friday.md", None),
        ("Architecture/Core.md", source.revision),
    )
    assert store.move_postcondition(
        "Projects/Friday.md",
        "Architecture/Core.md",
        expected_revision=source.revision,
    )
    assert not store.move_postcondition(
        "Projects/Friday.md",
        "Architecture/Core.md",
        expected_revision="0" * 64,
    )


@pytest.mark.parametrize("operation", ["delete", "move"])
def test_mutations_reject_traversal_and_symlink_leaves(
    vault: Path,
    tmp_path: Path,
    operation: str,
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    (vault / "linked.md").symlink_to(outside)
    store = VaultStore(vault)
    revision = "0" * 64

    with pytest.raises(VaultPathError):
        if operation == "delete":
            store.delete("../outside.md", expected_revision=revision)
        else:
            store.move("../outside.md", "inside.md", expected_revision=revision)
    with pytest.raises(VaultPathError):
        if operation == "delete":
            store.delete("linked.md", expected_revision=revision)
        else:
            store.move("linked.md", "inside.md", expected_revision=revision)

    assert outside.read_text(encoding="utf-8") == "outside"


@pytest.mark.parametrize("operation", ["delete", "move"])
def test_source_replacement_race_preserves_both_revisions(
    vault: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    store = VaultStore(vault)
    observed = store.write_text("source.md", "observed", create_only=True)
    real_rename = store._rename_noreplace  # noqa: SLF001
    injected = False

    def replace_before_publish(*args: object) -> None:
        nonlocal injected
        if not injected and args[1] == "source.md":
            injected = True
            peer = vault / ".peer"
            peer.write_text("peer", encoding="utf-8")
            os.replace(peer, vault / "source.md")
        real_rename(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_rename_noreplace", replace_before_publish)

    with pytest.raises(RevisionConflictError):
        if operation == "delete":
            store.delete("source.md", expected_revision=observed.revision)
        else:
            store.move("source.md", "moved.md", expected_revision=observed.revision)

    contents = {
        item.text()
        for path in (*store.list_markdown_paths(), *store.list_sync_conflict_paths())
        if (item := store.read(path))
    }
    assert contents == {"observed", "peer"}
    assert not list(vault.parent.glob(".friday-vault-*"))


@pytest.mark.parametrize("operation", ["delete", "move"])
def test_interrupted_prepared_mutation_rolls_back_on_store_reopen(
    vault: Path,
    operation: str,
) -> None:
    store = VaultStore(vault)
    observed = store.write_text("source.md", "observed", create_only=True)
    script = """
import os
import signal
import sys
from friday.organs.obsidian.vault_store import VaultStore

store = VaultStore(sys.argv[1])
real_rename = store._rename_noreplace
def interrupt_after_rename(*args):
    real_rename(*args)
    os.kill(os.getpid(), signal.SIGKILL)
store._rename_noreplace = interrupt_after_rename
if sys.argv[3] == "delete":
    store.delete("source.md", expected_revision=sys.argv[2])
else:
    store.move("source.md", "Moved/source.md", expected_revision=sys.argv[2])
"""

    interrupted = subprocess.run(  # noqa: S603 - fixed interpreter and local test script
        [sys.executable, "-c", script, str(vault), observed.revision, operation],
        check=False,
    )
    assert interrupted.returncode == -signal.SIGKILL

    recovered = VaultStore(vault)

    assert recovered.read("source.md").text() == "observed"
    assert not recovered.exists("Moved/source.md")
    assert not list(vault.parent.glob(".friday-vault-*"))
