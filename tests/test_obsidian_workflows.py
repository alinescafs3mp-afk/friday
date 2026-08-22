from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

import friday.organs.obsidian.workflows as workflow_module
from friday.organs.obsidian.frontmatter import parse_frontmatter
from friday.organs.obsidian.indexing import refresh_incremental_index
from friday.organs.obsidian.note_merge import build_preserve_both_preview
from friday.organs.obsidian.operations import ObsidianOperationService, OperationCommitUncertain
from friday.organs.obsidian.service import ObsidianService
from friday.organs.obsidian.vault_store import VaultStore
from friday.organs.obsidian.workflows import ObsidianWorkflowService
from friday.storage import FridayStorage


def _workflow(
    storage: FridayStorage,
    tmp_path: Path,
) -> tuple[ObsidianWorkflowService, ObsidianService, str, dict]:
    owner = "alice"
    storage.ensure_user(owner)
    conversation = storage.create_conversation(owner, "Obsidian acceptance")
    root = tmp_path / "vault"
    notes = ObsidianService(VaultStore(root), clock=lambda: date(2026, 8, 22))
    bundle = storage.create_obsidian_bundle(
        owner,
        config_root=str(tmp_path / "config"),
        database_root=str(tmp_path / "data"),
        api_endpoint=f"unix://{tmp_path}/syncthing.sock",
        api_key_ref="secret:obsidian:workflow",
        server_path=str(root),
        folder_id="friday-alice-workflow",
        setup_token_hash=hashlib.sha256(b"workflow-token").hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )
    operations = ObsidianOperationService(storage, notes, owner_id=owner)
    workflow = ObsidianWorkflowService(
        storage,
        notes,
        operations,
        owner_id=owner,
        context_key=str(conversation["id"]),
    )
    return workflow, notes, str(conversation["id"]), bundle


def test_task_workflow_adds_one_concrete_incomplete_task_and_replays(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, _conversation, _bundle = _workflow(storage, tmp_path)
    payload = {
        "action": "add_task",
        "day": "2026-08-22",
        "due_date": "2026-08-23",
        "due_time": "10:00",
        "text": "Проверить поиск в Obsidian",
    }

    first = workflow.execute_write("task-operation", payload)
    replay = workflow.execute_write("task-operation", payload)
    found = workflow.execute_read({"action": "search_tasks", "query": "Obsidian", "incomplete_only": True})

    document = notes.read_note("Daily/2026-08-22.md")
    assert document.content.count("- [ ] Проверить поиск в Obsidian") == 1
    assert "📅 2026-08-23 ⏰ 10:00" in document.content
    assert first.revision == replay.revision
    assert "Daily/2026-08-22.md" in found.body
    assert "2026-08-23T10:00" in found.body


def test_committed_workflow_resumes_projection_tail_without_duplicate_effect(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, notes, conversation, _bundle = _workflow(storage, tmp_path)
    refresh = workflow_module.refresh_incremental_index
    fail_once = True

    def lose_index(*args: object, **kwargs: object) -> object:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("synthetic post-commit index gap")
        return refresh(*args, **kwargs)

    monkeypatch.setattr(workflow_module, "refresh_incremental_index", lose_index)
    payload = {
        "action": "add_task",
        "day": "2026-08-22",
        "due_date": "2026-08-23",
        "due_time": "10:00",
        "text": "Проверить поиск в Obsidian",
    }
    with pytest.raises(OperationCommitUncertain):
        workflow.execute_write("task-projection-gap", payload)
    assert notes.read_note("Daily/2026-08-22.md").content.count("Проверить поиск в Obsidian") == 1
    pending = storage.get_obsidian_active_frame("alice", conversation)
    assert pending is not None
    assert json.loads(pending["frame_json"])["kind"] == "workflow_projection_pending_v1"

    resumed = workflow.execute_write(
        "new-transport-operation",
        {"action": "resume_previous"},
    )
    assert resumed.status == "resumed"
    assert resumed.operation_id == "task-projection-gap"
    assert notes.read_note("Daily/2026-08-22.md").content.count("Проверить поиск в Obsidian") == 1
    active = storage.get_obsidian_active_frame("alice", conversation)
    assert active is not None and json.loads(active["frame_json"])["kind"] == "workflow"


def test_metadata_workflow_merges_tags_and_preserves_body_and_unrelated_fields(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, _conversation, _bundle = _workflow(storage, tmp_path)
    notes.create_note(
        "Projects/Friday Test",
        "Body remains byte-identical.\n",
        properties={"aliases": ["legacy"], "tags": ["obsidian", "keep"]},
    )
    before = notes.read_note("Projects/Friday Test.md")

    receipt = workflow.execute_write(
        "metadata-operation",
        {
            "action": "update_metadata",
            "path": "Projects/Friday Test.md",
            "status": "review",
            "project": "Friday",
            "tags": ["integration", "obsidian", "test"],
        },
    )

    after = notes.read_note("Projects/Friday Test.md")
    properties = parse_frontmatter(after.content).properties
    assert receipt.path == "Projects/Friday Test.md"
    assert after.body == before.body
    assert properties["aliases"].value == ("legacy",)
    assert properties["tags"].value == ("obsidian", "keep", "integration", "test")


def test_stable_candidate_selection_keeps_exact_second_note_for_followup(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, conversation, bundle = _workflow(storage, tmp_path)
    first = notes.create_note("Projects/First", "Friday поиск first")
    second = notes.create_note("Projects/Second", "Friday поиск second")
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    bindings = {str(item["current_path"]): item for item in storage.list_obsidian_note_bindings("alice")}
    candidate_set = storage.create_obsidian_candidate_set(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        query={"text": "Friday и поиск"},
        candidates=[
            {
                "binding_id": bindings[first.path]["id"],
                "revision": first.revision,
                "path": first.path,
                "title": "First",
                "score": 20,
                "match_channels": ["lexical"],
            },
            {
                "binding_id": bindings[second.path]["id"],
                "revision": second.revision,
                "path": second.path,
                "title": "Second",
                "score": 19,
                "match_channels": ["lexical"],
            },
        ],
    )
    storage.upsert_obsidian_active_frame(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        frame_id=conversation,
        candidate_set_id=str(candidate_set["id"]),
        frame={"used_paths": [first.path, second.path]},
    )

    selected = workflow.execute_read({"action": "select_candidate", "ordinal": 2})
    changed = workflow.execute_write(
        "append-selected",
        {
            "action": "append_active_section",
            "section": "Следующие шаги",
            "item": "- Проверка семантического индекса",
        },
    )

    assert selected.path == second.path
    assert changed.path == second.path
    assert "Следующие шаги" not in notes.read_note(first.path).content
    assert notes.read_note(second.path).content.count("## Следующие шаги") == 1


def test_backlinks_template_summary_and_followup_links_are_server_owned(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, conversation, bundle = _workflow(storage, tmp_path)
    target = notes.create_note("Projects/Friday", "# Friday\n")
    notes.create_note("Notes/Search", "[[Projects/Friday]]\n")
    notes.create_note("Notes/Obsidian", "[[Projects/Friday]]\n")
    notes.create_note(
        "Templates/Meeting",
        (
            "---\ntype: meeting\ndate: {{date}}\nproject: {{project}}\n---\n\n"
            "# {{title}}\n\n## Participants\n\n{{participants}}\n\n"
            "## Discussion\n\n{{discussion}}\n\n## Actions\n\n{{actions}}\n"
        ),
    )
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    target_binding = next(
        item for item in storage.list_obsidian_note_bindings("alice") if item["current_path"] == target.path
    )
    storage.upsert_obsidian_active_frame(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        frame_id=conversation,
        active_binding_id=str(target_binding["id"]),
        frame={"used_paths": ["Projects/Friday.md", "Notes/Search.md"]},
    )
    storage.store_message(conversation, "alice", "user", "Что осталось проверить?")
    storage.store_message(conversation, "alice", "assistant", "Базовая синхронизация работает.")

    backlinks = workflow.execute_read({"action": "backlinks", "target_path": "Projects/Friday.md"})
    meeting = workflow.execute_write(
        "meeting-create",
        {
            "action": "create_from_template",
            "template_name": "Meeting",
            "title": "Проверка интеграции Obsidian",
            "project": "Friday",
            "participants": ["Алиса", "Борис"],
            "discussion": "Базовая синхронизация работает.",
            "actions": "- [ ] Проверить конфликты",
            "day": "2026-08-22",
        },
    )
    summary = workflow.execute_write(
        "summary-create",
        {
            "action": "save_summary",
            "path": "Research/Conversation Summary.md",
            "day": "2026-08-22",
        },
    )
    summary_operation = storage.get_obsidian_operation("alice", "summary-create")
    assert summary_operation is not None
    work_item_id = str(summary_operation["work_item_id"])
    assert work_item_id.startswith("obswork_")
    workflow = ObsidianWorkflowService(
        storage,
        notes,
        ObsidianOperationService(storage, notes, owner_id="alice"),
        owner_id="alice",
        context_key=conversation,
    )
    linked = workflow.execute_write(
        "summary-links",
        {"action": "append_summary_links", "day": "2026-08-22"},
    )

    assert set(backlinks.changed_paths) == {"Notes/Search.md", "Notes/Obsidian.md"}
    assert meeting.path == "Meetings/2026-08-22 Проверка интеграции Obsidian.md"
    meeting_body = notes.read_note(str(meeting.path)).content
    assert "Алиса, Борис" in meeting_body and "date: 2026-08-22" in meeting_body
    summary_body = notes.read_note(str(summary.path)).content
    assert all(
        heading in summary_body for heading in ("## Conclusions", "## Open questions", "## Next actions")
    )
    assert linked.path == summary.path
    assert "## Related notes" in notes.read_note(str(summary.path)).content
    linked_operation = storage.get_obsidian_operation("alice", "summary-links")
    assert linked_operation is not None and linked_operation["work_item_id"] == work_item_id
    work_frame = storage.get_obsidian_active_frame("alice", work_item_id=work_item_id)
    assert work_frame is not None and work_frame["active_path"] == summary.path


def test_summary_keeps_only_genuinely_unresolved_questions_and_actions(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, conversation, _bundle = _workflow(storage, tmp_path)
    storage.store_message(conversation, "alice", "user", "Создай тестовую заметку.")
    storage.store_message(conversation, "alice", "assistant", "Заметка создана; revision подтверждена.")
    storage.store_message(conversation, "alice", "user", "Создание завершено?")
    storage.store_message(conversation, "alice", "assistant", "Да, локальная запись подтверждена.")
    storage.store_message(conversation, "alice", "user", "Доставлено на Android?")
    storage.store_message(conversation, "alice", "user", "Проверь конфликт.")

    receipt = workflow.execute_write(
        "summary-facts",
        {
            "action": "save_summary",
            "path": "Research/Conversation Summary.md",
            "day": "2026-08-22",
        },
    )
    content = notes.read_note(str(receipt.path)).content

    assert "Заметка создана; revision подтверждена." in content
    assert "Создание завершено?" not in content
    assert "Создай тестовую заметку." not in content
    assert "Доставлено на Android?" in content
    assert "Проверь конфликт." in content


def test_move_preserves_binding_identity_updates_links_and_reports_skipped_targets(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, _conversation, bundle = _workflow(storage, tmp_path)
    target = notes.create_note("Projects/Friday.md", "# Friday\n")
    notes.create_note("Archive/Friday.md", "other")
    notes.create_note("Notes/Search.md", "[[Projects/Friday]] [[Friday]]")
    notes.create_note("Notes/Obsidian.md", "[[Projects/Friday]]")
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    before = next(
        item for item in storage.list_obsidian_note_bindings("alice") if item["current_path"] == target.path
    )

    moved = workflow.execute_write(
        "move-workflow",
        {
            "action": "move_note",
            "source_path": target.path,
            "destination_path": "Architecture/Friday.md",
            "update_links": True,
        },
    )
    backlinks = workflow.execute_read({"action": "backlinks", "target_path": "Architecture/Friday.md"})
    after = next(
        item
        for item in storage.list_obsidian_note_bindings("alice")
        if item["current_path"] == "Architecture/Friday.md"
    )

    assert before["integration_id"] == after["integration_id"]
    assert not notes.store.exists(target.path)
    assert "[[Architecture/Friday]]" in notes.read_note("Notes/Search.md").content
    assert set(backlinks.changed_paths) == {"Notes/Search.md", "Notes/Obsidian.md"}
    assert set(moved.changed_paths) >= {
        target.path,
        "Architecture/Friday.md",
        "Notes/Search.md",
        "Notes/Obsidian.md",
    }
    assert "Неоднозначные ссылки: Notes/Search.md -> Friday" in moved.body


def test_move_replays_after_commit_gap_and_recovers_the_original_binding(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, notes, _conversation, bundle = _workflow(storage, tmp_path)
    target = notes.create_note("Projects/Friday.md", "# Friday\n")
    notes.create_note("Notes/Search.md", "[[Projects/Friday]]\n")
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    original = next(
        item for item in storage.list_obsidian_note_bindings("alice") if item["current_path"] == target.path
    )
    original = storage.upsert_obsidian_note_binding(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        integration_id=str(original["integration_id"]),
        current_path=target.path,
        current_revision=target.revision,
        ownership_mode="projection",
        origin="projection",
        projection_kind="research",
        projection={"source": "friday"},
        friday_object_kind="memory",
        friday_object_id="memory-1",
        expected_current_revision=target.revision,
    )
    upsert = storage.upsert_obsidian_note_binding
    fail_once = True

    def lose_projection(*args: object, **kwargs: object) -> dict:
        nonlocal fail_once
        if fail_once and kwargs.get("current_path") == "Architecture/Friday.md":
            fail_once = False
            raise OSError("synthetic binding projection gap")
        return upsert(*args, **kwargs)

    monkeypatch.setattr(storage, "upsert_obsidian_note_binding", lose_projection)
    payload = {
        "action": "move_note",
        "source_path": target.path,
        "destination_path": "Architecture/Friday.md",
        "update_links": False,
    }
    with pytest.raises(OperationCommitUncertain):
        workflow.execute_write("move-projection-gap", payload)
    assert not notes.store.exists(target.path)
    assert notes.store.exists("Architecture/Friday.md")

    # A background index pass may run in the gap and discover a temporary
    # destination identity. Recovery must still revive the source identity.
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
        discovered_origin="friday",
    )
    recovered = workflow.execute_write("move-projection-gap", payload)

    bindings = storage.list_obsidian_note_bindings(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        include_deleted=True,
    )
    active = [item for item in bindings if item["deleted_at"] is None]
    moved = next(item for item in active if item["current_path"] == "Architecture/Friday.md")
    assert moved["id"] == original["id"]
    assert moved["integration_id"] == original["integration_id"]
    assert moved["ownership_mode"] == "projection"
    assert moved["origin"] == "projection"
    assert moved["projection_kind"] == "research"
    assert json.loads(moved["projection_json"]) == {"source": "friday"}
    assert moved["friday_object_kind"] == "memory"
    assert moved["friday_object_id"] == "memory-1"
    assert recovered.path == "Architecture/Friday.md"
    assert "[[Projects/Friday]]" in notes.read_note("Notes/Search.md").content
    frame = storage.get_obsidian_active_frame("alice", workflow.context_key)
    assert frame is not None
    assert json.loads(frame["frame_json"])["replay"]["update_links"] is False


def test_delete_replays_after_file_commit_before_tombstone_projection(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, notes, _conversation, bundle = _workflow(storage, tmp_path)
    doomed = notes.create_note("Scratch/Delete Me.md", "temporary")
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    binding = next(
        item for item in storage.list_obsidian_note_bindings("alice") if item["current_path"] == doomed.path
    )
    tombstone = storage.tombstone_obsidian_note_binding
    fail_once = True

    def lose_tombstone(*args: object, **kwargs: object) -> dict:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("synthetic tombstone gap")
        return tombstone(*args, **kwargs)

    monkeypatch.setattr(storage, "tombstone_obsidian_note_binding", lose_tombstone)
    payload = {"action": "delete_note", "path": doomed.path}
    with pytest.raises(OperationCommitUncertain):
        workflow.execute_write("delete-projection-gap", payload)
    assert not notes.store.exists(doomed.path)

    recovered = workflow.execute_write("delete-projection-gap", payload)
    projected = storage.get_obsidian_note_binding(
        "alice",
        str(binding["integration_id"]),
        include_deleted=True,
    )
    assert recovered.revision is None
    assert projected is not None and projected["deleted_at"] is not None


def test_base_evaluator_and_delete_lifecycle_follow_current_revisions(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, conversation, bundle = _workflow(storage, tmp_path)
    active = notes.create_note(
        "Projects/Active.md",
        "active",
        properties={"project": "Friday", "status": "active"},
    )
    done = notes.create_note(
        "Projects/Done.md",
        "done",
        properties={"project": "Friday", "status": "done"},
    )
    doomed = notes.create_note("Scratch/Delete Me.md", "temporary")
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    doomed_binding = next(
        item for item in storage.list_obsidian_note_bindings("alice") if item["current_path"] == doomed.path
    )
    storage.upsert_obsidian_active_frame(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        frame_id=conversation,
        active_binding_id=str(doomed_binding["id"]),
        frame={"used_paths": [doomed.path]},
    )

    base = workflow.execute_write(
        "base-workflow",
        {
            "action": "create_base",
            "name": "Friday Active Notes",
            "project": "Friday",
            "excluded_status": "done",
            "columns": ["file.name", "status", "file.mtime"],
        },
    )
    initial_query = workflow.execute_read({"action": "query_base", "name": "Friday Active Notes"})
    notes.set_properties(
        active.path,
        {"status": "done"},
        expected_revision=notes.read_note(active.path).revision,
    )
    current_query = workflow.execute_read({"action": "query_base", "name": "Friday Active Notes"})
    deleted = workflow.execute_write(
        "delete-workflow",
        {"action": "delete_note", "path": doomed.path},
    )

    assert base.path == "Bases/Friday Active Notes.base"
    assert notes.store.exists(str(base.path))
    assert "file.name=Active" in initial_query.body
    assert "file.name=Done" not in initial_query.body
    assert "актуальных строк: 0" in current_query.body
    assert "file.name=Active" not in current_query.body
    assert active.path in {item.path for item in notes.list_notes()}
    assert done.path in {item.path for item in notes.list_notes()}
    assert deleted.revision is None and deleted.open_uri is None
    assert not notes.store.exists(doomed.path)
    tombstone = storage.get_obsidian_note_binding(
        "alice",
        str(doomed_binding["integration_id"]),
        include_deleted=True,
    )
    assert tombstone is not None and tombstone["deleted_at"] is not None
    assert storage.get_obsidian_active_frame("alice", conversation) is None


def test_conflict_preview_preserves_and_displays_both_versions_without_applying(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, conversation, bundle = _workflow(storage, tmp_path)
    canonical = notes.create_note(
        "Projects/Friday Test.md",
        "# Friday Test\n\n## Проверка дополнения\n\nСтарая версия\n\n## Keep\n\nСохранить\n",
    )
    conflict_path = "Projects/Friday Test.sync-conflict-20260822.md"
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    binding = next(
        item
        for item in storage.list_obsidian_note_bindings("alice")
        if item["current_path"] == canonical.path
    )
    storage.upsert_obsidian_active_frame(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        frame_id=conversation,
        active_binding_id=str(binding["id"]),
        frame={"used_paths": [canonical.path]},
    )
    replaced = workflow.execute_write(
        "replace-friday-section",
        {
            "action": "replace_active_section",
            "section": "Проверка дополнения",
            "text": "Версия, записанная Friday",
        },
    )
    notes.store.write_text(
        conflict_path,
        "# Friday Test\n\n## Проверка дополнения\n\nВерсия Android\n\n## Keep\n\nСохранить\n",
        create_only=True,
    )
    storage.record_obsidian_conflict(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        canonical_path=canonical.path,
        conflict_path=conflict_path,
    )

    preview = workflow.execute_read({"action": "conflict_preview"})

    assert preview.status == "preview"
    assert replaced.path == canonical.path
    assert "Версия, записанная Friday" in preview.body and "Версия Android" in preview.body
    assert "Merged preview (not applied)" in preview.body
    assert "## Keep\n\nСохранить" in notes.read_note(canonical.path).content
    assert "Версия Android" in notes.store.read_text(conflict_path).text()
    frame = storage.get_obsidian_active_frame("alice", conversation)
    assert frame is not None and frame["active_binding_id"] is None
    frozen = json.loads(frame["frame_json"])
    assert frozen["kind"] == "conflict_preview_v1"
    assert frozen["canonical_binding_id"] == binding["id"]
    assert frozen["canonical_path"] == canonical.path
    assert frozen["canonical_revision"] == replaced.revision
    assert frozen["conflict_path"] == conflict_path
    assert frozen["conflict_revision"] == notes.store.read_text(conflict_path).revision
    assert "merged_content" not in frozen and "unified_diff" not in frozen


def test_accept_conflict_merge_applies_both_versions_resolves_and_replays(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, conversation, bundle = _workflow(storage, tmp_path)
    canonical = notes.create_note("Projects/Friday.md", "# Friday\n\nFriday edit\n")
    conflict_path = "Projects/Friday.sync-conflict-20260822.md"
    artifact = notes.store.write_text(
        conflict_path,
        "# Friday\n\nAndroid edit\n",
        create_only=True,
    )
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    conflict = storage.record_obsidian_conflict(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        canonical_path=canonical.path,
        conflict_path=conflict_path,
    )
    expected = build_preserve_both_preview(
        notes.store.read_text(canonical.path).text(),
        artifact.text(),
    )

    workflow.execute_read({"action": "conflict_preview"})
    accepted = workflow.execute_write(
        "accept-conflict",
        {"action": "accept_conflict_merge"},
    )
    replay = workflow.execute_write(
        "accept-conflict",
        {"action": "accept_conflict_merge"},
    )

    assert accepted.path == canonical.path
    assert accepted.revision == replay.revision
    assert accepted.changed_paths == (canonical.path,)
    assert notes.store.read_text(canonical.path).text() == expected.merged_content
    assert notes.store.read_text(conflict_path).text() == artifact.text()
    resolved = storage.get_obsidian_conflict("alice", str(conflict["id"]))
    assert resolved is not None and resolved["status"] == "resolved"
    resolution = json.loads(resolved["resolution_json"])
    assert resolution["operation_id"] == "accept-conflict"
    assert resolution["conflict_revision"] == artifact.revision
    assert resolution["merged_revision"] == accepted.revision
    again = storage.resolve_obsidian_conflict(
        "alice",
        str(conflict["id"]),
        vault_id=str(bundle["vault"]["id"]),
        canonical_path=canonical.path,
        conflict_path=conflict_path,
        canonical_revision=canonical.revision,
        conflict_revision=artifact.revision,
        merged_revision=str(accepted.revision),
        operation_id="accept-conflict",
    )
    assert again["status"] == "resolved"
    storage.ensure_user("bob")
    assert storage.get_obsidian_conflict("bob", str(conflict["id"])) is None
    with pytest.raises(ValueError):
        storage.resolve_obsidian_conflict(
            "alice",
            str(conflict["id"]),
            vault_id=str(bundle["vault"]["id"]),
            canonical_path=canonical.path,
            conflict_path=conflict_path,
            canonical_revision=canonical.revision,
            conflict_revision="0" * 64,
            merged_revision=str(accepted.revision),
            operation_id="accept-conflict",
        )
    rescanned = storage.record_obsidian_conflict(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        canonical_path=canonical.path,
        conflict_path=conflict_path,
    )
    assert rescanned["status"] == "resolved"
    frame = storage.get_obsidian_active_frame("alice", conversation)
    assert frame is not None and frame["last_operation_id"] == "accept-conflict"
    assert json.loads(frame["frame_json"])["kind"] == "conflict_resolution_v1"


def test_conflict_resolution_receipt_gap_replays_without_a_second_merge(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, notes, _conversation, bundle = _workflow(storage, tmp_path)
    canonical = notes.create_note("Projects/Friday.md", "Friday edit\n")
    conflict_path = "Projects/Friday.sync-conflict-20260822.md"
    artifact = notes.store.write_text(conflict_path, "Android edit\n", create_only=True)
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    conflict = storage.record_obsidian_conflict(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        canonical_path=canonical.path,
        conflict_path=conflict_path,
    )
    workflow.execute_read({"action": "conflict_preview"})
    resolve = storage.resolve_obsidian_conflict
    fail_once = True

    def lose_resolution(*args: object, **kwargs: object) -> dict:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("synthetic resolution receipt gap")
        return resolve(*args, **kwargs)

    monkeypatch.setattr(storage, "resolve_obsidian_conflict", lose_resolution)
    with pytest.raises(OperationCommitUncertain):
        workflow.execute_write("resolve-gap", {"action": "accept_conflict_merge"})
    merged_once = notes.store.read_text(canonical.path)
    open_conflict = storage.get_obsidian_conflict("alice", str(conflict["id"]))
    assert open_conflict is not None and open_conflict["status"] == "open"
    assert notes.store.read_text(conflict_path).revision == artifact.revision

    recovered = workflow.execute_write(
        "resolve-gap",
        {"action": "accept_conflict_merge"},
    )
    assert recovered.revision == merged_once.revision
    assert notes.store.read_text(canonical.path).revision == merged_once.revision
    resolved = storage.get_obsidian_conflict("alice", str(conflict["id"]))
    assert resolved is not None and resolved["status"] == "resolved"


def test_resolved_conflict_recovers_index_and_frame_with_original_operation(
    storage: FridayStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, notes, conversation, bundle = _workflow(storage, tmp_path)
    canonical = notes.create_note("Projects/Friday.md", "Friday edit\n")
    conflict_path = "Projects/Friday.sync-conflict-20260822.md"
    notes.store.write_text(conflict_path, "Android edit\n", create_only=True)
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    conflict = storage.record_obsidian_conflict(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        canonical_path=canonical.path,
        conflict_path=conflict_path,
    )
    workflow.execute_read({"action": "conflict_preview"})
    upsert_frame = storage.upsert_obsidian_active_frame
    fail_once = True

    def lose_resolution_frame(*args: object, **kwargs: object) -> dict:
        nonlocal fail_once
        frame = kwargs.get("frame")
        if fail_once and isinstance(frame, dict) and frame.get("kind") == "conflict_resolution_v1":
            fail_once = False
            raise OSError("synthetic resolved-frame gap")
        return upsert_frame(*args, **kwargs)

    monkeypatch.setattr(storage, "upsert_obsidian_active_frame", lose_resolution_frame)
    with pytest.raises(OperationCommitUncertain):
        workflow.execute_write("original-conflict-operation", {"action": "accept_conflict_merge"})
    resolved = storage.get_obsidian_conflict("alice", str(conflict["id"]))
    assert resolved is not None and resolved["status"] == "resolved"

    recovered = workflow.execute_write(
        "new-transport-operation",
        {"action": "accept_conflict_merge"},
    )
    assert recovered.operation_id == "original-conflict-operation"
    assert storage.get_obsidian_operation("alice", "new-transport-operation") is None
    frame = storage.get_obsidian_active_frame("alice", conversation)
    assert frame is not None and frame["last_operation_id"] == "original-conflict-operation"


def test_conflict_accept_rejects_an_artifact_changed_after_preview(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, _conversation, bundle = _workflow(storage, tmp_path)
    canonical = notes.create_note("Projects/Friday.md", "Friday edit\n")
    conflict_path = "Projects/Friday.sync-conflict-20260822.md"
    artifact = notes.store.write_text(conflict_path, "Android edit\n", create_only=True)
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    conflict = storage.record_obsidian_conflict(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        canonical_path=canonical.path,
        conflict_path=conflict_path,
    )
    workflow.execute_read({"action": "conflict_preview"})
    notes.store.write_text(
        conflict_path,
        "Android peer edit\n",
        expected_revision=artifact.revision,
    )

    with pytest.raises(ValueError, match="artifact changed"):
        workflow.execute_write("reject-race", {"action": "accept_conflict_merge"})
    assert notes.store.read_text(canonical.path).text() == "Friday edit\n"
    still_open = storage.get_obsidian_conflict("alice", str(conflict["id"]))
    assert still_open is not None and still_open["status"] == "open"


def test_pathless_replace_uses_one_unique_heading_not_the_active_frame(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, conversation, bundle = _workflow(storage, tmp_path)
    target = notes.create_note(
        "Projects/Target.md",
        "# Target\n\n## Проверка дополнения\n\nold\n",
    )
    active = notes.create_note("Projects/Unrelated.md", "# Unrelated\n")
    refresh_incremental_index(
        storage,
        notes,
        owner_id="alice",
        vault_id=str(bundle["vault"]["id"]),
    )
    active_binding = next(
        item for item in storage.list_obsidian_note_bindings("alice") if item["current_path"] == active.path
    )
    storage.upsert_obsidian_active_frame(
        "alice",
        vault_id=str(bundle["vault"]["id"]),
        frame_id=conversation,
        active_binding_id=str(active_binding["id"]),
        frame={"kind": "unrelated"},
    )
    payload = {
        "action": "replace_active_section",
        "section": "Проверка дополнения",
        "text": "new",
    }

    first = workflow.execute_write("unique-heading", payload)
    replay = workflow.execute_write("unique-heading", payload)

    assert first.path == target.path and replay.revision == first.revision
    assert "## Проверка дополнения\n\nnew\n" in notes.read_note(target.path).content
    assert notes.read_note(active.path).content == "# Unrelated\n"


def test_pathless_replace_fails_closed_for_duplicate_headings(
    storage: FridayStorage,
    tmp_path: Path,
) -> None:
    workflow, notes, _conversation, _bundle = _workflow(storage, tmp_path)
    notes.create_note("Projects/One.md", "## Status\n\none\n")
    notes.create_note("Projects/Two.md", "## status\n\ntwo\n")

    with pytest.raises(ValueError, match="not unique"):
        workflow.execute_write(
            "ambiguous-heading",
            {
                "action": "replace_active_section",
                "section": "STATUS",
                "text": "must not appear",
            },
        )
    assert "must not appear" not in notes.read_note("Projects/One.md").content
    assert "must not appear" not in notes.read_note("Projects/Two.md").content
