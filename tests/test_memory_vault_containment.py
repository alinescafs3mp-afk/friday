"""The optional Markdown vault never becomes an implicit plaintext copy."""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import friday.admin_api._knowledge as knowledge_admin_module
import friday.memory as memory_module
import friday.server as server_module
from friday.cli import _purge
from friday.config import load_settings
from friday.diagnostics import collect_diagnostics
from friday.memory import (
    MemoryVault,
    MemoryVaultDeletionHandle,
    VaultProjectionBoundaryError,
    _safe_component,
)
from friday.purge import VaultProjectionCleanupRequired, purge_knowledge
from friday.storage import init_storage
from friday.storage.models import KnowledgeObject, RawObject, new_id
from friday.workers import _sync_vault_page


def test_memory_vault_mode_defaults_disabled_and_rejects_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FRIDAY_MEMORY_VAULT_MODE", raising=False)
    monkeypatch.delenv("JERICHO_MEMORY_VAULT_MODE", raising=False)
    assert load_settings().memory_vault_mode == "disabled"

    monkeypatch.setenv("FRIDAY_MEMORY_VAULT_MODE", "")
    assert load_settings().memory_vault_mode == "disabled"

    monkeypatch.setenv("FRIDAY_MEMORY_VAULT_MODE", "full_owner")
    assert load_settings().memory_vault_mode == "full_owner"

    monkeypatch.setenv("FRIDAY_MEMORY_VAULT_MODE", "owner-ish")
    with pytest.raises(ValueError, match="Unknown FRIDAY_MEMORY_VAULT_MODE"):
        load_settings()


def test_injected_unknown_memory_vault_mode_is_rejected_before_startup(settings) -> None:
    with pytest.raises(ValueError, match="Unknown FRIDAY_MEMORY_VAULT_MODE"):
        server_module.create_app(replace(settings, memory_vault_mode="owner-ish"))


def test_disabled_backend_never_instantiates_or_registers_plaintext_projector(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = replace(settings, memory_vault_mode="disabled")

    class ForbiddenVault:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("disabled backend instantiated MemoryVault")

    monkeypatch.setattr(server_module, "MemoryVault", ForbiddenVault)
    assert not disabled.memory_vault_dir.exists()
    with TestClient(server_module.create_app(disabled)) as client:
        assert client.app.state.memory_vault is None
        client.app.state.workers.register_all()
        task_names = {task.name for task in client.app.state.workers.supervisor._tasks}  # noqa: SLF001
        assert "memory_vault_sync" not in task_names
        assert list(disabled.memory_vault_dir.rglob("*.md")) == []
        assert client.get("/api/health").json()["memory_vault"] == {
            "mode": "disabled",
            "body_free_mode": True,
            "body_projection_enabled": False,
        }
    assert not disabled.memory_vault_dir.exists()


def test_explicit_full_owner_instantiates_and_registers_projector(settings) -> None:
    enabled = replace(settings, memory_vault_mode="full_owner")
    with TestClient(server_module.create_app(enabled)) as client:
        assert isinstance(client.app.state.memory_vault, MemoryVault)
        client.app.state.workers.register_all()
        task_names = {task.name for task in client.app.state.workers.supervisor._tasks}  # noqa: SLF001
        assert "memory_vault_sync" in task_names
        assert client.get("/api/health").json()["memory_vault"] == {
            "mode": "full_owner",
            "body_free_mode": False,
            "body_projection_enabled": True,
        }


def test_full_owner_fails_before_creating_projection_when_no_follow_boundary_is_unavailable(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memory_module, "_descriptor_projection_supported", lambda: False)
    fresh_vault = settings.home / "unsupported-memory-vault"
    with pytest.raises(VaultProjectionBoundaryError, match="descriptor-relative"):
        MemoryVault(fresh_vault)
    assert not fresh_vault.exists()


def test_loaded_symlink_vault_root_is_not_resolved_and_purge_cannot_escape(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = settings.home / "outside-vault-target"
    configured = settings.home / "configured-memory-vault"
    account = outside / "users" / _safe_component("alice")
    account.mkdir(parents=True)
    configured.symlink_to(outside, target_is_directory=True)
    marker = "EXTERNAL-SYMLINK-BODY-MUST-SURVIVE"

    storage = init_storage(settings)
    try:
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("src"),
            raw_content=marker,
            content_type="text",
            content_hash=hashlib.sha256(marker.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            content=marker,
            title="Outside target",
            summary=marker,
        )
        storage.store_knowledge_object(knowledge)
        assert storage.soft_delete_knowledge_object(knowledge.id, "alice")
        digest = MemoryVault._note_stem(knowledge.id)  # noqa: SLF001
        outside_note = account / f"external--{digest}.md"
        outside_note.write_text(marker, encoding="utf-8")

        monkeypatch.setenv("FRIDAY_MEMORY_VAULT_DIR", str(configured))
        loaded = load_settings()
        assert loaded.memory_vault_dir == configured.absolute()
        assert loaded.memory_vault_dir != outside.resolve()
        public_settings = json.dumps(loaded.public_dict(), ensure_ascii=False)
        assert str(configured) not in public_settings
        assert str(outside) not in public_settings
        with pytest.raises(VaultProjectionCleanupRequired):
            purge_knowledge(
                storage,
                loaded,
                MemoryVaultDeletionHandle(loaded.memory_vault_dir),
                knowledge.id,
                "alice",
            )
        assert storage.get_knowledge_object(knowledge.id, "alice") is not None
        assert outside_note.read_text(encoding="utf-8") == marker
    finally:
        storage.close()


def test_nested_symlink_component_is_refused_for_purge_and_full_owner_write(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lexical = settings.home / "lexical-vault-parent"
    outside = settings.home / "outside-vault-parent"
    lexical.mkdir()
    outside.mkdir()
    (lexical / "hop").symlink_to(outside, target_is_directory=True)
    configured = lexical / "hop" / "vault"
    account = outside / "vault" / "users" / _safe_component("alice")
    account.mkdir(parents=True)
    ko_id = new_id("ko")
    digest = MemoryVault._note_stem(ko_id)  # noqa: SLF001
    outside_note = account / f"outside--{digest}.md"
    marker = "NESTED-SYMLINK-CONTROL-MARKER"
    outside_note.write_text(marker, encoding="utf-8")

    monkeypatch.setenv("FRIDAY_MEMORY_VAULT_DIR", str(configured))
    loaded = load_settings()
    assert loaded.memory_vault_dir == configured.absolute()
    with pytest.raises(VaultProjectionBoundaryError):
        MemoryVaultDeletionHandle(loaded.memory_vault_dir).delete_object(ko_id, "alice")
    with pytest.raises(VaultProjectionBoundaryError):
        MemoryVault(loaded.memory_vault_dir)
    assert outside_note.read_text(encoding="utf-8") == marker


def test_disabled_diagnostics_detect_users_level_plaintext_without_publishing_it(settings) -> None:
    disabled = replace(settings, memory_vault_mode="disabled")
    users = disabled.memory_vault_dir / "users"
    users.mkdir(parents=True)
    marker = "USERS-LEVEL-PLAINTEXT-MUST-NOT-LEAK"
    stray = users / "private-stray.md"
    stray.write_text(marker, encoding="utf-8")

    report = collect_diagnostics(disabled)
    projection = report["memory_vault"]
    assert projection["projection_note_count"] == 1
    assert projection["legacy_projection_present"] is True
    public = json.dumps(
        {"projection": projection, "actions": report["actions"]},
        ensure_ascii=False,
    )
    assert marker not in public
    assert stray.name not in public
    assert str(stray) not in public


@pytest.mark.parametrize("linked_component", ["root", "users"])
def test_disabled_diagnostics_report_unknown_for_refused_projection_boundary_without_leakage(
    settings,
    linked_component: str,
) -> None:
    configured = settings.home / f"diagnostic-{linked_component}-vault"
    outside = settings.home / f"diagnostic-{linked_component}-outside"
    marker = f"{linked_component.upper()}-LINK-PLAINTEXT-MUST-NOT-LEAK"
    private_name = f"{linked_component}-private-note.md"
    if linked_component == "root":
        account = outside / "users" / "private-account"
        account.mkdir(parents=True)
        configured.symlink_to(outside, target_is_directory=True)
    else:
        configured.mkdir()
        account = outside / "private-account"
        account.mkdir(parents=True)
        (configured / "users").symlink_to(outside, target_is_directory=True)
    note = account / private_name
    note.write_text(marker, encoding="utf-8")

    report = collect_diagnostics(replace(settings, memory_vault_dir=configured, memory_vault_mode="disabled"))
    projection = report["memory_vault"]
    assert projection["projection_root_present"] is None
    assert projection["projection_note_count"] == 0
    assert projection["legacy_projection_present"] is None
    assert projection["scan_complete"] is False
    warnings = [
        action
        for action in report["actions"]
        if str(action.get("code") or "").startswith("legacy_memory_vault_projection")
    ]
    assert [warning["code"] for warning in warnings] == ["legacy_memory_vault_projection_uninspected"]
    public_evidence = json.dumps(
        {"projection": projection, "warnings": warnings},
        ensure_ascii=False,
    )
    for private_value in (marker, private_name, account.name, str(note), str(outside), str(configured)):
        assert private_value not in public_evidence
    assert note.read_text(encoding="utf-8") == marker


def test_disabled_backend_does_not_delete_an_existing_legacy_note(settings) -> None:
    disabled = replace(settings, memory_vault_mode="disabled")
    account = disabled.memory_vault_dir / "users" / "legacy-account"
    account.mkdir(parents=True, exist_ok=True)
    note = account / "legacy-note.md"
    original = b"legacy plaintext retained for explicit owner review"
    note.write_bytes(original)

    with TestClient(server_module.create_app(disabled)):
        assert note.read_bytes() == original
    assert note.read_bytes() == original


def test_disabled_diagnostics_count_legacy_notes_without_names_paths_or_bodies(
    settings,
) -> None:
    disabled = replace(settings, memory_vault_mode="disabled")
    account = disabled.memory_vault_dir / "users" / "private-account-name"
    account.mkdir(parents=True, exist_ok=True)
    marker = "LEGACY-PLAINTEXT-BODY-MUST-NOT-LEAK-7E53"
    secret_name = account / "private-document-name.md"
    secret_name.write_text(marker, encoding="utf-8")
    second = account / "other.md"
    second.write_text("second private body", encoding="utf-8")
    (account / "README.md").write_text(marker, encoding="utf-8")

    report = collect_diagnostics(disabled)
    projection = report["memory_vault"]
    assert projection == {
        "mode": "disabled",
        "body_free_mode": True,
        "body_projection_enabled": False,
        "projection_root_present": True,
        "projection_note_count": 2,
        "legacy_projection_present": True,
        "scan_complete": True,
    }
    warning = next(
        action for action in report["actions"] if action["code"] == "legacy_memory_vault_projection"
    )
    public_evidence = json.dumps(
        {"projection": projection, "warning": warning},
        ensure_ascii=False,
        sort_keys=True,
    )
    assert marker not in public_evidence
    assert secret_name.name not in public_evidence
    assert str(disabled.memory_vault_dir) not in public_evidence
    assert secret_name.read_text(encoding="utf-8") == marker
    assert second.is_file()

    secret_name.unlink()
    second.unlink()
    readme_only = collect_diagnostics(disabled)["memory_vault"]
    assert readme_only["projection_note_count"] == 0
    assert readme_only["legacy_projection_present"] is True


def test_disabled_diagnostics_retire_stale_projector_worker_state(settings, storage) -> None:
    storage.kv_set(
        "workers:health:memory_vault_sync",
        json.dumps(
            {
                "status": "error",
                "interval_sec": 300,
                "consecutive_failures": 9,
                "error_type": "LegacyProjectorFailure",
            }
        ),
    )
    report = collect_diagnostics(replace(settings, workers_enabled=True), storage)
    assert "memory_vault_sync" not in report["workers"]["tasks"]
    assert "memory_vault_sync" not in report["workers"].get("degraded_tasks", [])
    assert report["workers"]["healthy"] is True


def test_disabled_diagnostics_detect_crash_temp_without_reading_it(settings) -> None:
    disabled = replace(settings, memory_vault_mode="disabled")
    account = disabled.memory_vault_dir / "users" / "legacy-account"
    account.mkdir(parents=True, exist_ok=True)
    marker = "TEMP-BODY-MUST-NOT-LEAK-99A1"
    crash_temp = account / ".private--0123456789ab.crash123.tmp"
    crash_temp.write_text(marker, encoding="utf-8")

    report = collect_diagnostics(disabled)
    projection = report["memory_vault"]
    assert projection["projection_note_count"] == 0
    assert projection["legacy_projection_present"] is True
    assert marker not in json.dumps(report["actions"], ensure_ascii=False)
    assert crash_temp.name not in json.dumps(report["actions"], ensure_ascii=False)


def test_disabled_cli_purge_can_remove_one_existing_legacy_projection(
    settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body_marker = "CLI-PURGE-BODY-MUST-NOT-BE-PUBLISHED"
    title_marker = "CLI-PURGE-TITLE-MUST-NOT-BE-PUBLISHED"
    storage = init_storage(settings)
    try:
        digest = hashlib.sha256(body_marker.encode()).hexdigest()
        raw_path = settings.files_dir / "alice" / digest[:2] / "CLI-RAW-PATH-MUST-NOT-LEAK.bin"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(body_marker.encode())
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("src"),
            raw_content="",
            content_type="file",
            content_hash=digest,
            metadata_json={"stored_path": str(raw_path)},
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            content=body_marker,
            content_type="file",
            title=title_marker,
            summary=body_marker,
        )
        storage.store_knowledge_object(knowledge)
        vault = MemoryVault(settings.memory_vault_dir)
        note = vault.sync_object(storage.get_knowledge_object(knowledge.id, "alice"))
        assert note is not None and note.is_file()
        assert storage.soft_delete_knowledge_object(knowledge.id, "alice")
    finally:
        storage.close()

    result = _purge(
        argparse.Namespace(
            yes=True,
            id=knowledge.id,
            user=None,
            older_than_days=None,
            limit=1,
        )
    )
    output = capsys.readouterr()
    assert result == 0
    assert not note.exists()
    assert not raw_path.exists()
    public_output = output.out + output.err
    assert body_marker not in public_output
    assert title_marker not in public_output
    assert "alice" not in public_output
    assert knowledge.id not in public_output
    assert raw.id not in public_output
    assert str(note) not in public_output
    assert str(raw_path) not in public_output
    assert hashlib.sha256(knowledge.id.encode()).hexdigest() in public_output


def _store_legacy_projection(storage, settings, *, marker: str) -> tuple[KnowledgeObject, Path]:
    raw = RawObject(
        id=new_id("raw"),
        user_id="alice",
        source="test",
        source_ref=new_id("src"),
        raw_content=marker,
        content_type="text",
        content_hash=hashlib.sha256(marker.encode()).hexdigest(),
    )
    storage.store_raw_object(raw)
    knowledge = KnowledgeObject(
        id=new_id("ko"),
        user_id="alice",
        raw_object_id=raw.id,
        content=marker,
        title="Legacy API projection",
        summary=marker,
    )
    storage.store_knowledge_object(knowledge)
    note = MemoryVault(settings.memory_vault_dir).sync_object(
        storage.get_knowledge_object(knowledge.id, "alice")
    )
    assert note is not None
    assert storage.soft_delete_knowledge_object(knowledge.id, "alice")
    return knowledge, note


def test_disabled_admin_purge_uses_deletion_only_handle_and_removes_crash_temp(settings) -> None:
    disabled = replace(settings, memory_vault_mode="disabled")
    with TestClient(server_module.create_app(disabled)) as client:
        storage = client.app.state.storage
        knowledge, note_value = _store_legacy_projection(
            storage,
            disabled,
            marker="disabled API legacy body",
        )
        note = note_value
        crash_temp = note.parent / f".{note.stem}.crash123.tmp"
        crash_temp.write_text("crash plaintext", encoding="utf-8")
        unrelated = note.parent / ".unrelated--000000000000.crash123.tmp"
        unrelated.write_text("unrelated", encoding="utf-8")

        response = client.post(
            f"/api/admin/knowledge/{knowledge.id}/purge?user_id=alice",
            json={},
            headers={"Authorization": f"Bearer {disabled.api_token}"},
        )

        assert response.status_code == 200, response.text
        public_report = response.json()["report"]
        assert public_report["vault_removed_count"] == 2
        assert (
            public_report["knowledge_object_ref_sha256"] == hashlib.sha256(knowledge.id.encode()).hexdigest()
        )
        response_blob = response.text
        assert "disabled API legacy body" not in response_blob
        assert knowledge.title not in response_blob
        assert knowledge.user_id not in response_blob
        assert knowledge.id not in response_blob
        assert knowledge.raw_object_id not in response_blob
        assert str(note) not in response_blob
        assert storage.get_knowledge_object(knowledge.id, "alice") is None
        assert not note.exists() and not crash_temp.exists()
        assert unrelated.read_text(encoding="utf-8") == "unrelated"
        assert client.app.state.memory_vault is None


def test_disabled_admin_purge_blocks_before_db_commit_when_legacy_unlink_fails(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = replace(settings, memory_vault_mode="disabled")
    with TestClient(server_module.create_app(disabled)) as client:
        storage = client.app.state.storage
        knowledge, note = _store_legacy_projection(
            storage,
            disabled,
            marker="must remain until exact cleanup",
        )

        def fail_unlink(_descriptor: int, _name: str) -> None:
            raise VaultProjectionBoundaryError("synthetic unlink failure")

        monkeypatch.setattr(
            client.app.state.memory_vault_deletion,
            "_unlink_regular_name",
            fail_unlink,
        )
        response = client.post(
            f"/api/admin/knowledge/{knowledge.id}/purge?user_id=alice",
            json={},
            headers={"Authorization": f"Bearer {disabled.api_token}"},
        )

        assert response.status_code == 503
        assert storage.get_knowledge_object(knowledge.id, "alice") is not None
        assert note.is_file()


def test_purge_writer_fence_rejects_a_stale_projector_page_after_unlink(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = init_storage(settings)
    try:
        knowledge, note = _store_legacy_projection(
            storage,
            settings,
            marker="STALE-WORKER-BODY-MUST-NOT-RETURN",
        )
        stale_page_item = storage.get_knowledge_object(knowledge.id, "alice")
        assert stale_page_item is not None and stale_page_item.get("deleted_at")
        stale_page_item = {**stale_page_item, "deleted_at": None}
        vault = MemoryVault(settings.memory_vault_dir)
        deletion = MemoryVaultDeletionHandle(settings.memory_vault_dir)
        real_unlink = deletion._unlink_regular_name  # noqa: SLF001

        def interleave_stale_worker(account_descriptor: int, name: str) -> None:
            real_unlink(account_descriptor, name)
            _sync_vault_page(vault, storage, [stale_page_item])

        monkeypatch.setattr(deletion, "_unlink_regular_name", interleave_stale_worker)
        report = purge_knowledge(
            storage,
            settings,
            deletion,
            knowledge.id,
            "alice",
        )

        assert report["vault_removed_count"] == 1
        assert storage.get_knowledge_object(knowledge.id, "alice") is None
        assert not note.exists()
        digest = MemoryVault._note_stem(knowledge.id)  # noqa: SLF001
        assert all(digest not in item.name for item in note.parent.iterdir())
    finally:
        storage.close()


def test_projector_writer_fence_orders_a_live_write_before_soft_delete_and_purge(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = init_storage(settings)
    worker_release = threading.Event()
    try:
        marker = "ORDERED-WORKER-BODY-MUST-BE-PURGED"
        raw = RawObject(
            id=new_id("raw"),
            user_id="alice",
            source="test",
            source_ref=new_id("src"),
            raw_content=marker,
            content_type="text",
            content_hash=hashlib.sha256(marker.encode()).hexdigest(),
        )
        storage.store_raw_object(raw)
        knowledge = KnowledgeObject(
            id=new_id("ko"),
            user_id="alice",
            raw_object_id=raw.id,
            content=marker,
            title="Ordered worker",
            summary=marker,
        )
        storage.store_knowledge_object(knowledge)
        page_item = storage.get_knowledge_object(knowledge.id, "alice")
        assert page_item is not None

        vault = MemoryVault(settings.memory_vault_dir)
        real_sync = vault.sync_object
        worker_entered = threading.Event()
        delete_done = threading.Event()
        notes: list[Path] = []
        thread_errors: list[BaseException] = []

        def blocking_sync(item: dict[str, object]) -> Path | None:
            worker_entered.set()
            if not worker_release.wait(timeout=5):
                raise RuntimeError("worker fence test timed out")
            note = real_sync(item)
            if note is not None:
                notes.append(note)
            return note

        def run_worker() -> None:
            try:
                _sync_vault_page(vault, storage, [page_item])
            except BaseException as exc:  # pragma: no cover - asserted below
                thread_errors.append(exc)

        def run_delete() -> None:
            try:
                assert storage.soft_delete_knowledge_object(knowledge.id, "alice")
            except BaseException as exc:  # pragma: no cover - asserted below
                thread_errors.append(exc)
            finally:
                delete_done.set()

        monkeypatch.setattr(vault, "sync_object", blocking_sync)
        worker_thread = threading.Thread(target=run_worker)
        delete_thread = threading.Thread(target=run_delete)
        worker_thread.start()
        assert worker_entered.wait(timeout=5)
        delete_thread.start()
        assert not delete_done.wait(timeout=0.1)
        worker_release.set()
        worker_thread.join(timeout=5)
        delete_thread.join(timeout=5)
        assert not worker_thread.is_alive() and not delete_thread.is_alive()
        assert thread_errors == []
        assert len(notes) == 1 and notes[0].is_file()

        report = purge_knowledge(
            storage,
            settings,
            MemoryVaultDeletionHandle(settings.memory_vault_dir),
            knowledge.id,
            "alice",
        )
        assert report["vault_removed_count"] == 1
        assert storage.get_knowledge_object(knowledge.id, "alice") is None
        assert not notes[0].exists()
    finally:
        worker_release.set()
        storage.close()


def test_admin_purge_reports_committed_cleanup_when_completion_audit_fails(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = replace(settings, memory_vault_mode="disabled")
    marker = "PURGED-BODY-MUST-NOT-ENTER-FAILURE-RECEIPT"
    with TestClient(server_module.create_app(disabled)) as client:
        storage = client.app.state.storage
        knowledge, note = _store_legacy_projection(storage, disabled, marker=marker)
        original_audit = knowledge_admin_module._audit  # noqa: SLF001
        audit_calls = 0
        before_attempted = storage.execute(
            "SELECT count(*) FROM audit_log WHERE action='admin.knowledge.purge_attempted'"
        ).fetchone()[0]
        before_completed = storage.execute(
            "SELECT count(*) FROM audit_log WHERE action='admin.knowledge.purge'"
        ).fetchone()[0]

        def fail_completion_audit(*args: object, **kwargs: object) -> None:
            nonlocal audit_calls
            audit_calls += 1
            if audit_calls == 2:
                raise OSError("synthetic completion audit failure")
            original_audit(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(knowledge_admin_module, "_audit", fail_completion_audit)
        response = client.post(
            f"/api/admin/knowledge/{knowledge.id}/purge?user_id=alice",
            json={},
            headers={"Authorization": f"Bearer {disabled.api_token}"},
        )

        assert response.status_code == 503
        assert response.json() == {
            "status": "purged_audit_unconfirmed",
            "purged": True,
            "knowledge_object_ref_sha256": hashlib.sha256(knowledge.id.encode()).hexdigest(),
            "existed": True,
            "deleted_row_count": 4,
            "raw_removed": True,
            "file_unlinked": False,
            "vault_removed": True,
            "vault_removed_count": 1,
        }
        assert storage.get_knowledge_object(knowledge.id, "alice") is None
        assert not note.exists()
        after_attempted = storage.execute(
            "SELECT count(*) FROM audit_log WHERE action='admin.knowledge.purge_attempted'"
        ).fetchone()[0]
        after_completed = storage.execute(
            "SELECT count(*) FROM audit_log WHERE action='admin.knowledge.purge'"
        ).fetchone()[0]
        assert after_attempted == before_attempted + 1
        assert after_completed == before_completed
        attempted = storage.execute(
            "SELECT action, before_json, after_json FROM audit_log "
            "WHERE action='admin.knowledge.purge_attempted' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert attempted is not None
        audit_blob = json.dumps(dict(attempted), ensure_ascii=False)
        response_blob = response.text
        assert marker not in audit_blob
        assert marker not in response_blob
        assert knowledge.id not in response_blob
        assert str(note) not in response_blob


def test_admin_purge_does_not_start_when_attempt_audit_is_unavailable(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = replace(settings, memory_vault_mode="disabled")
    marker = "PRE-AUDIT-FAILURE-MUST-KEEP-THE-OBJECT"
    with TestClient(server_module.create_app(disabled)) as client:
        storage = client.app.state.storage
        knowledge, note = _store_legacy_projection(storage, disabled, marker=marker)
        prior_purge_audits = storage.execute(
            "SELECT count(*) FROM audit_log WHERE action IN "
            "('admin.knowledge.purge_attempted','admin.knowledge.purge')"
        ).fetchone()[0]

        def fail_attempt_audit(*_args: object, **_kwargs: object) -> None:
            raise OSError("synthetic attempt audit failure")

        monkeypatch.setattr(knowledge_admin_module, "_audit", fail_attempt_audit)
        response = client.post(
            f"/api/admin/knowledge/{knowledge.id}/purge?user_id=alice",
            json={},
            headers={"Authorization": f"Bearer {disabled.api_token}"},
        )

        assert response.status_code == 503
        assert response.json() == {
            "status": "purge_audit_unavailable",
            "purged": False,
            "knowledge_object_ref_sha256": hashlib.sha256(knowledge.id.encode()).hexdigest(),
            "vault_removed_count": 0,
            "deleted_row_count": 0,
        }
        assert storage.get_knowledge_object(knowledge.id, "alice") is not None
        assert note.read_text(encoding="utf-8")
        assert (
            storage.execute(
                "SELECT count(*) FROM audit_log WHERE action IN "
                "('admin.knowledge.purge_attempted','admin.knowledge.purge')"
            ).fetchone()[0]
            == prior_purge_audits
        )
        assert marker not in response.text
        assert knowledge.id not in response.text
        assert str(note) not in response.text


def test_batch_cli_purge_immediately_audits_success_before_later_cleanup_failure(
    settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    storage = init_storage(settings)
    prior_audit_count = 0
    try:
        prior_audit_count = storage.execute(
            "SELECT count(*) FROM audit_log WHERE action='cli.knowledge.purge'"
        ).fetchone()[0]
        first, first_note = _store_legacy_projection(
            storage,
            settings,
            marker="FIRST-BATCH-BODY-MUST-NOT-LEAK",
        )
        second, second_note = _store_legacy_projection(
            storage,
            settings,
            marker="SECOND-BATCH-BODY-MUST-NOT-LEAK",
        )
        with storage.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_objects SET deleted_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", first.id),
            )
            connection.execute(
                "UPDATE knowledge_objects SET deleted_at=? WHERE id=?",
                ("2001-01-01T00:00:00+00:00", second.id),
            )
    finally:
        storage.close()

    original_delete = MemoryVaultDeletionHandle.delete_object

    def fail_second(self: MemoryVaultDeletionHandle, ko_id: str, user_id: str) -> int:
        if ko_id == second.id:
            raise VaultProjectionBoundaryError("synthetic second-item failure")
        return original_delete(self, ko_id, user_id)

    monkeypatch.setattr(MemoryVaultDeletionHandle, "delete_object", fail_second)
    result = _purge(
        argparse.Namespace(
            yes=True,
            id=None,
            user="alice",
            older_than_days=0,
            limit=10,
        )
    )
    captured = capsys.readouterr()

    assert result == 2
    receipt = json.loads(captured.out)
    first_ref = hashlib.sha256(first.id.encode()).hexdigest()
    assert receipt == {
        "status": "partial_failure",
        "purged": 1,
        "audited": 1,
        "items": [{"knowledge_object_ref_sha256": first_ref}],
        "failure_type": "VaultProjectionCleanupRequired",
    }
    public_output = captured.out + captured.err
    assert first.id not in public_output and second.id not in public_output
    assert "FIRST-BATCH-BODY-MUST-NOT-LEAK" not in public_output
    assert "SECOND-BATCH-BODY-MUST-NOT-LEAK" not in public_output

    storage = init_storage(settings)
    try:
        assert storage.get_knowledge_object(first.id, "alice") is None
        assert storage.get_knowledge_object(second.id, "alice") is not None
        audit_rows = storage.execute(
            "SELECT target_id, after_json FROM audit_log WHERE action='cli.knowledge.purge' ORDER BY rowid"
        ).fetchall()
        assert len(audit_rows) == prior_audit_count + 1
        assert str(audit_rows[-1]["target_id"]) != first.id
        assert json.loads(str(audit_rows[-1]["after_json"]))["status"] == "purged"
    finally:
        storage.close()
    assert not first_note.exists()
    assert second_note.exists()
