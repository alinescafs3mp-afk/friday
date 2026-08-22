from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from friday.organs.obsidian.base_spec import friday_active_notes_spec, render_base
from friday.organs.obsidian.contracts import (
    IdempotencyConflictError,
    NoteAlreadyExistsError,
    RevisionConflictError,
)
from friday.organs.obsidian.note_merge import build_preserve_both_preview
from friday.organs.obsidian.operations import (
    DurableWorkflowResult,
    ObsidianOperationService,
    OperationCommitUncertain,
    OperationTerminalError,
)
from friday.organs.obsidian.service import ObsidianService
from friday.organs.obsidian.vault_store import VaultStore
from friday.storage import FridayStorage


def _operations(
    storage: FridayStorage,
    tmp_path: Path,
) -> tuple[ObsidianOperationService, ObsidianService]:
    owner = "workflow-owner"
    storage.ensure_user(owner)
    root = tmp_path / "vault"
    notes = ObsidianService(VaultStore(root))
    storage.create_obsidian_bundle(
        owner,
        config_root=str(tmp_path / "config"),
        database_root=str(tmp_path / "database"),
        api_endpoint=f"unix://{tmp_path}/syncthing.sock",
        api_key_ref="secret:obsidian:workflow-owner",
        server_path=str(root),
        folder_id="friday-workflow-owner",
        setup_token_hash=hashlib.sha256(b"workflow-token").hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )
    return ObsidianOperationService(storage, notes, owner_id=owner), notes


def test_replace_section_reconciles_the_receipt_gap_without_a_second_edit(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    created = notes.create_note(
        "Projects/Friday.md",
        "# Friday\n\n## Status\n\nold\n\n## Notes\n\nkeep\n",
    )
    transition = storage.transition_obsidian_operation
    fail_once = True

    def lose_receipt(owner: str, operation_id: str, state: str, **kwargs: Any) -> dict:
        nonlocal fail_once
        if operation_id == "replace-gap" and state == "committed" and fail_once:
            fail_once = False
            raise OSError("synthetic receipt gap")
        return transition(owner, operation_id, state, **kwargs)

    monkeypatch.setattr(storage, "transition_obsidian_operation", lose_receipt)
    with pytest.raises(OperationCommitUncertain):
        operations.replace_section(
            "replace-gap",
            "Projects/Friday.md",
            "Status",
            "new",
            expected_revision=created.revision,
        )

    recovered = operations.replace_section(
        "replace-gap",
        "Projects/Friday.md",
        "Status",
        "new",
        expected_revision=created.revision,
    )

    assert recovered.replayed is True
    assert recovered.applied is False
    assert recovered.previous_revision == created.revision
    content = notes.read_note("Projects/Friday.md").content
    assert content.count("new") == 1
    assert "## Notes\n\nkeep\n" in content


def test_replace_section_cas_failure_never_changes_peer_text(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    created = notes.create_note("Replace.md", "## Status\n\nold\n")
    peer = notes.append_note(
        "Replace.md",
        "peer",
        operation_id="peer-edit",
        expected_revision=created.revision,
    )

    with pytest.raises(RevisionConflictError):
        operations.replace_section(
            "replace-stale",
            "Replace.md",
            "Status",
            "must not appear",
            expected_revision=created.revision,
        )

    assert notes.read_note("Replace.md").revision == peer.revision
    assert "must not appear" not in notes.read_note("Replace.md").content


def test_create_base_is_validated_no_clobber_and_exact_once(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    content = render_base(friday_active_notes_spec())

    created = operations.create_base("base-create", "Views/Friday", content)
    replay = operations.create_base("base-create", "Views/Friday.base", content)

    assert created.created is True and created.applied is True
    assert replay.replayed is True
    assert notes.store.read_text("Views/Friday.base").text() == content
    with pytest.raises(NoteAlreadyExistsError):
        operations.create_base("base-other", "Views/Friday.base", content)
    with pytest.raises(OperationTerminalError):
        operations.create_base("base-other", "Views/Friday.base", content)


def test_create_base_reconciles_a_lost_receipt_without_clobbering(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    content = render_base(friday_active_notes_spec())
    transition = storage.transition_obsidian_operation
    fail_once = True

    def lose_receipt(owner: str, operation_id: str, state: str, **kwargs: Any) -> dict:
        nonlocal fail_once
        if operation_id == "base-gap" and state == "committed" and fail_once:
            fail_once = False
            raise OSError("synthetic receipt gap")
        return transition(owner, operation_id, state, **kwargs)

    monkeypatch.setattr(storage, "transition_obsidian_operation", lose_receipt)
    with pytest.raises(OperationCommitUncertain):
        operations.create_base("base-gap", "Views/Gap.base", content)

    recovered = operations.create_base("base-gap", "Views/Gap.base", content)

    assert recovered.replayed is True and recovered.applied is False
    assert notes.store.read_text("Views/Gap.base").text() == content


def test_delete_receipt_gap_becomes_an_explicit_tombstone_and_never_recreates(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    created = notes.create_note("Scratch/Delete.md", "temporary")
    transition = storage.transition_obsidian_operation
    fail_once = True

    def lose_receipt(owner: str, operation_id: str, state: str, **kwargs: Any) -> dict:
        nonlocal fail_once
        if operation_id == "delete-gap" and state == "committed" and fail_once:
            fail_once = False
            raise OSError("synthetic receipt gap")
        return transition(owner, operation_id, state, **kwargs)

    monkeypatch.setattr(storage, "transition_obsidian_operation", lose_receipt)
    with pytest.raises(OperationCommitUncertain):
        operations.delete_note(
            "delete-gap",
            "Scratch/Delete.md",
            expected_revision=created.revision,
        )
    assert not notes.store.exists("Scratch/Delete.md")

    recovered = operations.delete_note(
        "delete-gap",
        "Scratch/Delete.md",
        expected_revision=created.revision,
    )

    assert isinstance(recovered, DurableWorkflowResult)
    assert recovered.replayed is True and recovered.applied is False
    assert recovered.revision is None
    assert recovered.tombstones == ("Scratch/Delete.md",)
    assert recovered.changed_revisions == (("Scratch/Delete.md", None),)
    row = storage.get_obsidian_operation("workflow-owner", "delete-gap")
    assert row is not None
    assert json.loads(row["result_json"])["tombstones"] == ["Scratch/Delete.md"]

    notes.store.write_text("Scratch/Delete.md", "new peer note", create_only=True)
    replay = operations.delete_note(
        "delete-gap",
        "Scratch/Delete.md",
        expected_revision=created.revision,
    )
    assert replay.replayed is True
    assert notes.store.read_text("Scratch/Delete.md").text() == "new peer note"


def test_delete_crash_before_effect_replays_the_prepared_tombstone_plan(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    created = notes.create_note("Crash/Delete.md", "temporary")
    real_delete = notes.store.delete

    class SyntheticCrash(BaseException):
        pass

    monkeypatch.setattr(
        notes.store, "delete", lambda *_args, **_kwargs: (_ for _ in ()).throw(SyntheticCrash)
    )
    with pytest.raises(SyntheticCrash):
        operations.delete_note(
            "delete-crash",
            "Crash/Delete.md",
            expected_revision=created.revision,
        )
    row = storage.get_obsidian_operation("workflow-owner", "delete-crash")
    assert row is not None and row["status"] == "prepared"

    monkeypatch.setattr(notes.store, "delete", real_delete)
    recovered = operations.delete_note(
        "delete-crash",
        "Crash/Delete.md",
        expected_revision=created.revision,
    )
    assert recovered.replayed is True and recovered.applied is True
    assert not notes.store.exists("Crash/Delete.md")


def test_move_reports_all_paths_and_only_rewrites_unambiguous_links(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    target = notes.create_note("Projects/Friday.md", "# Friday\n")
    notes.create_note("Archive/Friday.md", "other\n")
    notes.create_note(
        "Notes/Search.md",
        "[[Projects/Friday]] [[Friday]] [[Missing]]\n",
    )

    moved = operations.move_note(
        "move-links",
        "Projects/Friday.md",
        "Architecture/Friday.md",
        expected_revision=target.revision,
    )
    replay = operations.move_note(
        "move-links",
        "Projects/Friday.md",
        "Architecture/Friday.md",
        expected_revision=target.revision,
    )

    assert moved.applied is True
    assert replay.replayed is True
    assert moved.changed_paths == (
        "Projects/Friday.md",
        "Architecture/Friday.md",
        "Notes/Search.md",
    )
    assert [issue.target for issue in moved.ambiguous] == ["Friday"]
    assert [issue.target for issue in moved.unresolved] == ["Missing"]
    assert notes.store.read_text("Notes/Search.md").text() == (
        "[[Architecture/Friday]] [[Friday]] [[Missing]]\n"
    )
    assert not notes.store.exists("Projects/Friday.md")
    assert notes.store.read_text("Architecture/Friday.md").revision == target.revision


def test_move_crash_gap_rebuilds_the_same_frozen_plan(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    target = notes.create_note("Projects/Friday.md", "target")
    notes.create_note("Notes/Search.md", "[[Projects/Friday]]")

    class SyntheticCrash(BaseException):
        pass

    monkeypatch.setattr(
        "friday.organs.obsidian.operations.execute_move_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SyntheticCrash),
    )
    with pytest.raises(SyntheticCrash):
        operations.move_note(
            "move-crash",
            "Projects/Friday.md",
            "Architecture/Friday.md",
            expected_revision=target.revision,
        )
    row = storage.get_obsidian_operation("workflow-owner", "move-crash")
    assert row is not None and row["status"] == "prepared"

    monkeypatch.undo()
    recovered = operations.move_note(
        "move-crash",
        "Projects/Friday.md",
        "Architecture/Friday.md",
        expected_revision=target.revision,
    )
    assert recovered.replayed is True and recovered.applied is True
    assert notes.store.read_text("Notes/Search.md").text() == "[[Architecture/Friday]]"


def test_move_reconciles_after_the_rename_but_before_a_link_rewrite(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    target = notes.create_note("Projects/Friday.md", "target")
    notes.create_note("Notes/Search.md", "[[Projects/Friday]]")
    real_write = notes.store.write_text

    class SyntheticCrash(BaseException):
        pass

    def crash_on_backlink(path: str, *args: Any, **kwargs: Any) -> Any:
        if path == "Notes/Search.md":
            raise SyntheticCrash
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(notes.store, "write_text", crash_on_backlink)
    with pytest.raises(SyntheticCrash):
        operations.move_note(
            "move-partial",
            "Projects/Friday.md",
            "Architecture/Friday.md",
            expected_revision=target.revision,
        )
    assert not notes.store.exists("Projects/Friday.md")
    assert notes.store.exists("Architecture/Friday.md")
    assert notes.store.read_text("Notes/Search.md").text() == "[[Projects/Friday]]"

    monkeypatch.setattr(notes.store, "write_text", real_write)
    recovered = operations.move_note(
        "move-partial",
        "Projects/Friday.md",
        "Architecture/Friday.md",
        expected_revision=target.revision,
    )

    assert recovered.replayed is True and recovered.applied is True
    assert notes.store.read_text("Notes/Search.md").text() == "[[Architecture/Friday]]"


def test_move_partial_recovery_preserves_a_raced_link_note(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    target = notes.create_note("Projects/Friday.md", "target")
    search = notes.create_note("Notes/Search.md", "[[Projects/Friday]]")
    real_write = notes.store.write_text

    class SyntheticCrash(BaseException):
        pass

    def crash_on_backlink(path: str, *args: Any, **kwargs: Any) -> Any:
        if path == "Notes/Search.md":
            raise SyntheticCrash
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(notes.store, "write_text", crash_on_backlink)
    with pytest.raises(SyntheticCrash):
        operations.move_note(
            "move-raced-link",
            "Projects/Friday.md",
            "Architecture/Friday.md",
            expected_revision=target.revision,
        )

    monkeypatch.setattr(notes.store, "write_text", real_write)
    peer = notes.store.write_text(
        "Notes/Search.md",
        "peer [[Projects/Friday]]",
        expected_revision=search.revision,
    )
    with pytest.raises(OperationCommitUncertain):
        operations.move_note(
            "move-raced-link",
            "Projects/Friday.md",
            "Architecture/Friday.md",
            expected_revision=target.revision,
        )
    assert notes.store.read_text("Notes/Search.md").text() == "peer [[Projects/Friday]]"

    notes.store.write_text(
        "Notes/Search.md",
        "[[Projects/Friday]]",
        expected_revision=peer.revision,
    )
    recovered = operations.move_note(
        "move-raced-link",
        "Projects/Friday.md",
        "Architecture/Friday.md",
        expected_revision=target.revision,
    )
    assert recovered.status == "reconciled"
    assert notes.store.read_text("Notes/Search.md").text() == "[[Architecture/Friday]]"


def test_move_reconciles_a_lost_multi_path_receipt_without_rewriting_again(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    target = notes.create_note("Projects/Friday.md", "target")
    notes.create_note("Notes/Search.md", "[[Projects/Friday]]")
    transition = storage.transition_obsidian_operation
    fail_once = True

    def lose_receipt(owner: str, operation_id: str, state: str, **kwargs: Any) -> dict:
        nonlocal fail_once
        if operation_id == "move-receipt" and state == "committed" and fail_once:
            fail_once = False
            raise OSError("synthetic receipt gap")
        return transition(owner, operation_id, state, **kwargs)

    monkeypatch.setattr(storage, "transition_obsidian_operation", lose_receipt)
    with pytest.raises(OperationCommitUncertain):
        operations.move_note(
            "move-receipt",
            "Projects/Friday.md",
            "Architecture/Friday.md",
            expected_revision=target.revision,
        )

    recovered = operations.move_note(
        "move-receipt",
        "Projects/Friday.md",
        "Architecture/Friday.md",
        expected_revision=target.revision,
    )

    assert recovered.replayed is True and recovered.applied is False
    assert notes.store.read_text("Notes/Search.md").text() == "[[Architecture/Friday]]"


def test_move_is_revision_guarded_and_never_clobbers_destination(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    source = notes.create_note("Source.md", "source")
    notes.create_note("Destination.md", "destination")

    with pytest.raises(NoteAlreadyExistsError):
        operations.move_note(
            "move-clobber",
            "Source.md",
            "Destination.md",
            expected_revision=source.revision,
            update_links=False,
        )
    assert notes.store.read_text("Source.md").text() == "source"
    assert notes.store.read_text("Destination.md").text() == "destination"
    with pytest.raises(OperationTerminalError):
        operations.move_note(
            "move-clobber",
            "Source.md",
            "Destination.md",
            expected_revision=source.revision,
            update_links=False,
        )

    with pytest.raises(RevisionConflictError):
        operations.move_note(
            "move-stale",
            "Source.md",
            "Elsewhere.md",
            expected_revision="0" * 64,
            update_links=False,
        )
    assert notes.store.read_text("Source.md").text() == "source"


def test_operation_id_cannot_cross_workflow_methods_or_arguments(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    created = notes.create_note("Delete.md", "temporary")
    operations.delete_note("workflow-id", "Delete.md", expected_revision=created.revision)

    with pytest.raises(IdempotencyConflictError):
        operations.delete_note("workflow-id", "Other.md", expected_revision=created.revision)


def test_conflict_merge_is_server_rendered_exact_once_and_preserves_the_artifact(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    canonical = notes.create_note("Projects/Friday.md", "# Friday\n\nFriday edit\n")
    artifact_path = "Projects/Friday.sync-conflict-20260822.md"
    artifact = notes.store.write_text(artifact_path, "# Friday\n\nAndroid edit\n", create_only=True)
    canonical_content = notes.store.read_text(canonical.path).text()
    expected = build_preserve_both_preview(canonical_content, artifact.text())

    first = operations.apply_conflict_merge(
        "merge-conflict",
        "obsconf_exact",
        canonical.path,
        artifact_path,
        expected_revision=canonical.revision,
        conflict_revision=artifact.revision,
    )
    replay = operations.apply_conflict_merge(
        "merge-conflict",
        "obsconf_exact",
        canonical.path,
        artifact_path,
        expected_revision=canonical.revision,
        conflict_revision=artifact.revision,
    )

    assert first.applied is True
    assert replay.replayed is True
    assert notes.store.read_text(canonical.path).text() == expected.merged_content
    assert notes.store.read_text(artifact_path).text() == artifact.text()
    row = storage.get_obsidian_operation("workflow-owner", "merge-conflict")
    assert row is not None
    durable = json.loads(row["result_json"])
    assert durable["conflict_id"] == "obsconf_exact"
    assert durable["conflict_path"] == artifact_path
    assert durable["conflict_revision"] == artifact.revision
    assert durable["target_revision"] == first.revision


def test_conflict_merge_artifact_race_is_uncertain_and_reconciles_without_clobbering(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    canonical = notes.create_note("Projects/Friday.md", "Friday edit\n")
    artifact_path = "Projects/Friday.sync-conflict-20260822.md"
    artifact = notes.store.write_text(artifact_path, "Android edit\n", create_only=True)
    canonical_content = notes.store.read_text(canonical.path).text()
    expected = build_preserve_both_preview(canonical_content, artifact.text())
    real_write = notes.store.write_text
    peer_revision = ""

    def race_artifact(path: str, content: str, **kwargs: Any) -> Any:
        nonlocal peer_revision
        written = real_write(path, content, **kwargs)
        if path == canonical.path:
            peer = real_write(
                artifact_path,
                "Android peer edit\n",
                expected_revision=artifact.revision,
            )
            peer_revision = peer.revision
        return written

    monkeypatch.setattr(notes.store, "write_text", race_artifact)
    with pytest.raises(OperationCommitUncertain):
        operations.apply_conflict_merge(
            "merge-race",
            "obsconf_race",
            canonical.path,
            artifact_path,
            expected_revision=canonical.revision,
            conflict_revision=artifact.revision,
        )
    assert notes.store.read_text(canonical.path).text() == expected.merged_content
    assert notes.store.read_text(artifact_path).text() == "Android peer edit\n"

    monkeypatch.setattr(notes.store, "write_text", real_write)
    real_write(
        artifact_path,
        artifact.text(),
        expected_revision=peer_revision,
    )
    recovered = operations.apply_conflict_merge(
        "merge-race",
        "obsconf_race",
        canonical.path,
        artifact_path,
        expected_revision=canonical.revision,
        conflict_revision=artifact.revision,
    )
    assert recovered.replayed is True and recovered.applied is False
    assert notes.store.read_text(artifact_path).text() == artifact.text()


def test_conflict_merge_rejects_either_stale_side_before_writing(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    operations, notes = _operations(storage, tmp_path)
    canonical = notes.create_note("Projects/Friday.md", "Friday edit\n")
    artifact_path = "Projects/Friday.sync-conflict-20260822.md"
    artifact = notes.store.write_text(artifact_path, "Android edit\n", create_only=True)
    peer = notes.store.write_text(
        artifact_path,
        "Android peer edit\n",
        expected_revision=artifact.revision,
    )

    with pytest.raises(RevisionConflictError):
        operations.apply_conflict_merge(
            "merge-stale-artifact",
            "obsconf_stale",
            canonical.path,
            artifact_path,
            expected_revision=canonical.revision,
            conflict_revision=artifact.revision,
        )
    assert notes.store.read_text(canonical.path).text() == "Friday edit\n"
    assert notes.store.read_text(artifact_path).revision == peer.revision
