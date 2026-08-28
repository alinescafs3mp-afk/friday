"""Cross-store backup authority prevents main/Engineer history rewinds."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday.diagnostics.runtime_lease import ProcessLease
from friday.engineer_source_binding import canonical_engineer_source_binding_sha256
from friday.organs.engineer.command.contracts import CommandError
from friday.organs.engineer.command.store import CommandJobStore
from friday.organs.engineer.command_tools import (
    open_engineer_command_backup_authority,
    provision_engineer_command_store,
)
from friday.storage import FridayStorage


@contextmanager
def _enabled_environment(settings: Any, tmp_path: Path) -> Iterator[tuple[Any, CommandJobStore]]:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    key_file = tmp_path / "engineer-command.key"
    key_file.write_bytes(b"B" * 32)
    key_file.chmod(0o600)
    configured = replace(
        settings,
        database_path=tmp_path / "friday.sqlite3",
        backups_dir=tmp_path / "backups",
        state_dir=state_dir,
        engineer_mode_enabled=True,
        engineer_command_enabled=True,
        engineer_command_store_dir=tmp_path / "engineer-command",
        engineer_command_key_file=key_file,
    )
    provision_engineer_command_store(configured)
    with open_engineer_command_backup_authority(configured, exclusive=True) as authority:
        assert isinstance(authority, CommandJobStore)
        storage = FridayStorage(configured)
        storage.bind_engineer_command_backup_authority(authority)
        try:
            yield storage, authority
        finally:
            storage.close()


def _advance(authority: CommandJobStore, nonce: str) -> None:
    with authority.transaction():
        authority.consume_nonce(nonce, exp=2_000_000_000, now=1)


def _publication_job_payload() -> dict[str, object]:
    source_step_id = "ecstep-" + "1" * 32
    source_binding = canonical_engineer_source_binding_sha256(
        owner_id="owner",
        tenant_id="owner",
        conversation_id="conversation",
        channel="telegram",
        source_row_id="msg_0123456789abcdef",
        source_step_id=source_step_id,
        source_hash="3" * 64,
        telegram_update_id="100",
        delivery_chat_id="5001",
    )
    return {
        "job_id": "2" * 32,
        "actor_id": "owner",
        "tenant_id": "owner",
        "conversation_id": "conversation",
        "channel": "telegram",
        "source_row_id": "msg_0123456789abcdef",
        "source_step_id": source_step_id,
        "source_binding_sha256": source_binding,
        "source_hash": "3" * 64,
        "telegram_update_id": "100",
        "isolation_profile": "host_user",
        "host_user_authorized": True,
        "idempotency_key": "ecmd-" + "4" * 64,
        "command_digest": "5" * 64,
        "input_manifest_sha256": "",
        "argv_sha256": "6" * 64,
        "lane": "argv",
        "origin": "model",
        "status": "completed",
        "grant_nonce": "grant-nonce",
        "timeout_sec": 60,
        "max_stdout_bytes": 1024,
        "max_stderr_bytes": 1024,
        "created_at": time.time(),
        "executable_json": "{}",
        "delivery_chat_id": "5001",
    }


def _backend_boundary(storage: Any) -> ProcessLease:
    return ProcessLease(
        storage.settings.state_dir / "backend.lock",
        protocol="friday.backend.v1",
    )


def test_enabled_backup_is_keyed_to_exact_external_authority_and_restores(settings, tmp_path) -> None:
    with _enabled_environment(settings, tmp_path) as (storage, authority):
        storage.ensure_user("before-backup")
        backup = storage.create_backup(label="authority-safe")
        evidence = backup["engineer_command_ledger_authority"]
        assert evidence["database_sha256"] == backup["sha256"]
        assert evidence["quiescent"] is True
        assert (evidence["store_id"], evidence["authority_sequence"], True) == (
            authority.backup_authority_snapshot()
        )
        with open_engineer_command_backup_authority(storage.settings) as online_observer:
            assert online_observer.backup_authority_snapshot() == (authority.backup_authority_snapshot())
        assert storage.verify_backup(backup["database"])["engineer_command_authority_matches"] is True

        storage.ensure_user("after-backup")
        with _backend_boundary(storage):
            restored = storage.restore_backup(backup["database"])
        assert restored["ok"] is True
        assert storage.get_user("before-backup") is not None
        assert storage.get_user("after-backup") is None


def test_backup_is_refused_before_ack_and_in_the_ack_reconciliation_crash_window(
    settings,
    tmp_path,
) -> None:
    with _enabled_environment(settings, tmp_path) as (storage, authority):
        storage.ensure_user("owner")
        authority.insert_job(_publication_job_payload())
        assert storage.enqueue_notification(
            "owner",
            "5001",
            "terminal",
            kind="engineer_command_terminal_text",
            dedup_key="engineer-terminal:ack-rewind",
        )
        notification_id = str(storage.list_pending_notifications()[0]["id"])
        authority.stage_publication(
            "2" * 32,
            notification_id=notification_id,
            dedup_key="engineer-terminal:ack-rewind",
            envelope_sha256="7" * 64,
        )

        with pytest.raises(RuntimeError, match="unresolved Engineer"):
            storage.create_backup(label="before-ack")

        # Telegram accepted the carrier and main SQLite durably records sent,
        # then the process crashes before external `staged -> sent` reconcile.
        storage.mark_notifications(sent_ids=[notification_id])
        with pytest.raises(RuntimeError, match="unresolved Engineer"):
            storage.create_backup(label="after-ack-before-reconcile")

        row = storage.execute(
            "SELECT status FROM outbound_notifications WHERE id=?",
            (notification_id,),
        ).fetchone()
        assert row is not None and row["status"] == "sent"


def test_backup_before_terminal_ingress_cannot_orphan_new_ledger_result(settings, tmp_path) -> None:
    with _enabled_environment(settings, tmp_path) as (storage, authority):
        storage.ensure_user("owner")
        backup = storage.create_backup(label="before-terminal-ingress")

        assert storage.enqueue_notification(
            "owner",
            "5001",
            "new terminal result",
            kind="engineer_command_terminal_text",
            dedup_key="engineer-terminal:new-after-backup",
        )
        notification_id = str(storage.list_pending_notifications()[0]["id"])
        _advance(authority, "terminal-created-after-main-backup")
        with (
            _backend_boundary(storage),
            pytest.raises(RuntimeError, match="unverified backup"),
        ):
            storage.restore_backup(backup["database"])

        assert (
            storage.execute(
                "SELECT 1 FROM outbound_notifications WHERE id=? AND status='pending'",
                (notification_id,),
            ).fetchone()
            is not None
        )


def test_manifest_cannot_transplant_store_id_or_rebind_a_changed_database(
    settings,
    tmp_path,
) -> None:
    with _enabled_environment(settings, tmp_path) as (storage, _authority):
        storage.ensure_user("authority-tamper")
        backup = storage.create_backup(label="authority-tamper")
        manifest_path = Path(backup["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["engineer_command_ledger_authority"]["store_id"] = "f" * 32
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        assert storage.verify_backup(backup["database"])["ok"] is False

        # Even a valid SQLite edit with a freshly recomputed ordinary manifest
        # hash cannot be attached to the old keyed authority proof.
        manifest["engineer_command_ledger_authority"]["store_id"] = backup[
            "engineer_command_ledger_authority"
        ]["store_id"]
        database_path = Path(backup["path"])
        with sqlite3.connect(database_path) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute(
                "INSERT OR REPLACE INTO runtime_kv(key,value,updated_at) VALUES(?,?,?)",
                ("tampered-after-backup", "1", "2026-08-27T00:00:00+00:00"),
            )
        manifest["size_bytes"] = database_path.stat().st_size
        manifest["sha256"] = hashlib.sha256(database_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        verification = storage.verify_backup(backup["database"])
        assert verification["ok"] is False
        assert verification["engineer_command_authority_matches"] is False


def test_old_manifest_without_authority_fails_closed_once_engineer_is_active(
    settings,
    tmp_path,
) -> None:
    with _enabled_environment(settings, tmp_path) as (storage, _authority):
        backup = storage.create_backup(label="missing-authority")
        manifest_path = Path(backup["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        del manifest["engineer_command_ledger_authority"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        verification = storage.verify_backup(backup["database"])
        assert verification["ok"] is False
        assert "authority evidence is missing" in str(verification["manifest_error"])


def test_disabled_after_provision_still_refuses_external_ledger_rewind(settings, tmp_path) -> None:
    with _enabled_environment(settings, tmp_path) as (storage, authority):
        storage.ensure_user("before-dormant-backup")
        backup = storage.create_backup(label="before-dormant-ledger-change")
        storage.ensure_user("must-survive-refused-restore")
        _advance(authority, "ledger-change-before-feature-disable")
        dormant_settings = replace(
            storage.settings,
            engineer_mode_enabled=False,
            engineer_command_enabled=False,
        )

    # CLI restore opens the already-provisioned store exclusively even though
    # the feature flag is now off. Disablement must never mean "no authority".
    with open_engineer_command_backup_authority(
        dormant_settings,
        exclusive=True,
    ) as dormant_authority:
        dormant = FridayStorage(dormant_settings)
        dormant.bind_engineer_command_backup_authority(dormant_authority)
        try:
            verification = dormant.verify_backup(backup["database"])
            assert verification["engineer_command_authority_matches"] is False
            with (
                _backend_boundary(dormant),
                pytest.raises(RuntimeError, match="unverified backup"),
            ):
                dormant.restore_backup(backup["database"])
            assert dormant.get_user("must-survive-refused-restore") is not None
        finally:
            dormant.close()


def _dormant_server_settings(settings: Any, tmp_path: Path, *, suffix: str) -> Any:
    state_dir = tmp_path / f"{suffix}-state"
    state_dir.mkdir(mode=0o700)
    return replace(
        settings,
        database_path=tmp_path / f"{suffix}-friday.sqlite3",
        backups_dir=tmp_path / f"{suffix}-backups",
        state_dir=state_dir,
        engineer_mode_enabled=False,
        engineer_command_enabled=False,
        engineer_command_store_dir=tmp_path / f"{suffix}-engineer-command",
        engineer_command_key_file=tmp_path / f"{suffix}-engineer-command.key",
    )


def test_disabled_server_binds_valid_dormant_authority_for_online_backup(
    settings,
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from friday.organs.engineer.command.backup_authority import (
        CommandStoreBackupAuthorityObserver,
    )
    from friday.server import create_app

    configured = _dormant_server_settings(settings, tmp_path, suffix="dormant-valid")
    key_path = Path(configured.engineer_command_key_file)
    key_path.write_bytes(b"D" * 32)
    key_path.chmod(0o600)
    provision_engineer_command_store(configured)
    with open_engineer_command_backup_authority(configured) as expected_authority:
        expected = expected_authority.backup_authority_snapshot()
    store_root = Path(configured.engineer_command_store_dir)
    entries_before = {path.relative_to(store_root) for path in store_root.rglob("*")}

    app = create_app(configured)
    with TestClient(app):
        bound = app.state.storage._engineer_command_backup_authority  # noqa: SLF001
        assert isinstance(bound, CommandStoreBackupAuthorityObserver)
        assert bound.backup_authority_snapshot() == expected
        backup = app.state.storage.create_backup(label="dormant-server")
        evidence = backup["engineer_command_ledger_authority"]
        assert (
            evidence["store_id"],
            evidence["authority_sequence"],
            evidence["quiescent"],
        ) == expected
        assert app.state.engineer_command_account_inventory is None
        assert all(organ.name != "engineer" for organ in app.state.organs.organs)

    with pytest.raises(CommandError, match="backup_authority_unavailable"):
        bound.backup_authority_snapshot()
    assert {path.relative_to(store_root) for path in store_root.rglob("*")} == entries_before


def test_disabled_server_without_command_authority_does_not_create_one(
    settings,
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from friday.server import create_app

    configured = _dormant_server_settings(settings, tmp_path, suffix="dormant-absent")
    store_root = Path(configured.engineer_command_store_dir)
    key_path = Path(configured.engineer_command_key_file)
    app = create_app(configured)
    with TestClient(app):
        assert app.state.storage._engineer_command_backup_authority is None  # noqa: SLF001
        backup = app.state.storage.create_backup(label="no-engineer-authority")
        assert "engineer_command_ledger_authority" not in backup

    assert not store_root.exists()
    assert not key_path.exists()
    assert not list(Path(configured.state_dir).glob("engineer-command-store.*"))


def test_disabled_server_refuses_forged_dormant_authority_without_healing_it(
    settings,
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from friday.server import create_app

    configured = _dormant_server_settings(settings, tmp_path, suffix="dormant-forged")
    key_path = Path(configured.engineer_command_key_file)
    key_path.write_bytes(b"F" * 32)
    key_path.chmod(0o600)
    provision_engineer_command_store(configured)
    anchor_path = Path(configured.state_dir) / "engineer-command-store.anchor.json"
    anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
    anchor["mac"] = "f" * 64 if anchor["mac"] != "f" * 64 else "e" * 64
    anchor_path.write_text(json.dumps(anchor), encoding="utf-8")
    anchor_path.chmod(0o600)
    forged_bytes = anchor_path.read_bytes()
    store_root = Path(configured.engineer_command_store_dir)
    entries_before = {path.relative_to(store_root) for path in store_root.rglob("*")}
    database_before = (store_root / "kernel.sqlite").read_bytes()

    app = create_app(configured)
    with (
        pytest.raises(CommandError, match="lifecycle_mismatch"),
        TestClient(app),
    ):
        pass

    assert anchor_path.read_bytes() == forged_bytes
    assert (store_root / "kernel.sqlite").read_bytes() == database_before
    entries_after = {path.relative_to(store_root) for path in store_root.rglob("*")}
    # SQLite may materialise its ordinary WAL coordination sidecars before the
    # authenticated lifecycle row/anchor comparison fails.  It must not create a
    # store, directory, job, or lifecycle authority while handling the forgery.
    assert entries_after - entries_before <= {
        Path("kernel.sqlite-wal"),
        Path("kernel.sqlite-shm"),
    }
    assert not any((store_root / relative).is_dir() for relative in entries_after - entries_before)


def test_disabled_server_refuses_partial_dormant_authority_without_provisioning(
    settings,
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    from friday.server import create_app

    configured = _dormant_server_settings(settings, tmp_path, suffix="dormant-partial")
    key_path = Path(configured.engineer_command_key_file)
    key_path.write_bytes(b"P" * 32)
    key_path.chmod(0o600)
    store_root = Path(configured.engineer_command_store_dir)

    app = create_app(configured)
    with (
        pytest.raises(CommandError, match="backup_authority_unavailable"),
        TestClient(app),
    ):
        pass

    assert not store_root.exists()
    assert not list(Path(configured.state_dir).glob("engineer-command-store.*"))


def test_partial_dormant_store_evidence_blocks_unbound_backup(settings, tmp_path) -> None:
    state_dir = tmp_path / "partial-state"
    state_dir.mkdir(mode=0o700)
    store_root = tmp_path / "partial-engineer-command"
    store_root.mkdir(mode=0o700)
    configured = replace(
        settings,
        database_path=tmp_path / "partial-friday.sqlite3",
        backups_dir=tmp_path / "partial-backups",
        state_dir=state_dir,
        engineer_mode_enabled=False,
        engineer_command_enabled=False,
        engineer_command_store_dir=store_root,
        engineer_command_key_file=tmp_path / "missing-engineer-command.key",
    )
    storage = FridayStorage(configured)
    try:
        with pytest.raises(RuntimeError, match="authority is unavailable"):
            storage.create_backup(label="must-not-assume-empty-ledger")
        assert not configured.backups_dir.exists() or not list(configured.backups_dir.iterdir())
    finally:
        storage.close()


def test_dormant_configured_key_alone_blocks_unbound_backup(settings, tmp_path) -> None:
    state_dir = tmp_path / "key-only-state"
    state_dir.mkdir(mode=0o700)
    key_file = tmp_path / "key-only-engineer-command.key"
    key_file.write_bytes(b"K" * 32)
    key_file.chmod(0o600)
    configured = replace(
        settings,
        database_path=tmp_path / "key-only-friday.sqlite3",
        backups_dir=tmp_path / "key-only-backups",
        state_dir=state_dir,
        engineer_mode_enabled=False,
        engineer_command_enabled=False,
        engineer_command_store_dir=tmp_path / "missing-engineer-command-store",
        engineer_command_key_file=key_file,
    )
    storage = FridayStorage(configured)
    try:
        with pytest.raises(RuntimeError, match="authority is unavailable"):
            storage.create_backup(label="must-not-ignore-dormant-key")
        assert not configured.backups_dir.exists() or not list(configured.backups_dir.iterdir())
    finally:
        storage.close()


def test_concurrent_ledger_mutation_aborts_online_backup_without_manifest(
    settings,
    tmp_path,
    monkeypatch,
) -> None:
    with _enabled_environment(settings, tmp_path) as (storage, authority):
        entered_verification = threading.Event()
        release_verification = threading.Event()
        original = storage._verify_backup_conn  # noqa: SLF001
        outcome: list[BaseException] = []

        def slow_verify(connection: sqlite3.Connection):  # noqa: ANN202
            entered_verification.set()
            assert release_verification.wait(10)
            return original(connection)

        def create() -> None:
            try:
                storage.create_backup(label="concurrent-ledger-mutation")
            except BaseException as exc:  # adversarial thread reports exact failure
                outcome.append(exc)

        monkeypatch.setattr(storage, "_verify_backup_conn", slow_verify)
        worker = threading.Thread(target=create)
        worker.start()
        assert entered_verification.wait(10)
        _advance(authority, "mutation-during-main-backup")
        release_verification.set()
        worker.join(10)

        assert not worker.is_alive()
        assert len(outcome) == 1
        assert "authority changed" in str(outcome[0]).casefold()
        assert list(storage.settings.backups_dir.glob("*.sqlite3")) == []
        assert list(storage.settings.backups_dir.glob("*.manifest.json")) == []


def test_restore_holds_bridge_lease_before_any_verification(settings, tmp_path) -> None:
    with _enabled_environment(settings, tmp_path) as (storage, _authority):
        backup = storage.create_backup(label="bridge-active")
        bridge = ProcessLease(
            storage.settings.state_dir / "telegram-inbox.sqlite3.lock",
            protocol="friday.telegram-bridge.v1",
        )
        with (
            bridge,
            _backend_boundary(storage),
            pytest.raises(
                RuntimeError,
                match="Telegram bridge to be stopped",
            ),
        ):
            storage.restore_backup(backup["database"])


def test_restore_refuses_to_orphan_current_main_delivery_even_at_same_external_sequence(
    settings,
    tmp_path,
) -> None:
    with _enabled_environment(settings, tmp_path) as (storage, _authority):
        storage.ensure_user("owner")
        backup = storage.create_backup(label="before-current-delivery")
        storage.ensure_user("must-survive-refused-restore")
        assert storage.enqueue_notification(
            "owner",
            "5001",
            "unresolved current terminal",
            kind="engineer_command_terminal_text",
            dedup_key="engineer-terminal:current-main-orphan",
        )

        with (
            _backend_boundary(storage),
            pytest.raises(RuntimeError, match="unresolved current Engineer delivery"),
        ):
            storage.restore_backup(backup["database"])

        assert storage.get_user("must-survive-refused-restore") is not None
        assert (
            storage.execute(
                """SELECT 1 FROM outbound_notifications
                 WHERE dedup_key=? AND status='pending'""",
                ("engineer-terminal:current-main-orphan",),
            ).fetchone()
            is not None
        )


def test_interrupted_restore_refuses_rollback_after_external_authority_advances(
    settings,
    tmp_path,
    monkeypatch,
) -> None:
    from friday.storage import _maintenance as storage_module

    with _enabled_environment(settings, tmp_path) as (storage, authority):
        storage.ensure_user("before-authority-recovery-race")
        backup = storage.create_backup(label="authority-recovery-race")
        storage.ensure_user("must-not-be-rewound-after-authority-race")
        original_recovery_stage = storage_module._stage_verified_recovery_copy
        original_diagnostics = storage.diagnostics

        def fail_recovery_stage(_snapshot, _destination):  # noqa: ANN001, ANN202
            raise OSError("injected crash during initial rollback")

        def fail_target_health():  # noqa: ANN202
            raise RuntimeError("injected target health failure")

        monkeypatch.setattr(
            storage_module,
            "_stage_verified_recovery_copy",
            fail_recovery_stage,
        )
        monkeypatch.setattr(storage, "diagnostics", fail_target_health)
        with (
            _backend_boundary(storage),
            pytest.raises(RuntimeError, match="durable rollback is pending"),
        ):
            storage.restore_backup(backup["database"])

        marker = storage.settings.state_dir / "database-restore.intent.json"
        intent = json.loads(marker.read_text(encoding="utf-8"))
        assert intent["engineer_command_ledger_authority"] == backup["engineer_command_ledger_authority"]
        recovery_path = Path(str(intent["recovery_path"]))
        protected = [
            marker,
            *(path for path in recovery_path.rglob("*") if path.is_file()),
            *(
                path
                for path in (
                    storage.settings.database_path,
                    Path(f"{storage.settings.database_path}-wal"),
                    Path(f"{storage.settings.database_path}-shm"),
                )
                if path.is_file()
            ),
        ]
        protected_before = {path: path.read_bytes() for path in protected}
        entries_before = sorted(str(path.relative_to(recovery_path)) for path in recovery_path.rglob("*"))

        monkeypatch.setattr(
            storage_module,
            "_stage_verified_recovery_copy",
            original_recovery_stage,
        )
        monkeypatch.setattr(storage, "diagnostics", original_diagnostics)
        _advance(authority, "authority-advanced-before-restore-recovery")

        with (
            _backend_boundary(storage),
            pytest.raises(RuntimeError, match="Engineer authority changed"),
        ):
            storage_module._recover_interrupted_restore(  # noqa: SLF001
                storage.settings,
                storage.settings.database_path.absolute(),
                engineer_authority=authority,
            )

        assert {path: path.read_bytes() for path in protected} == protected_before
        assert (
            sorted(str(path.relative_to(recovery_path)) for path in recovery_path.rglob("*"))
            == entries_before
        )
        assert not list(storage.settings.database_path.parent.glob("*.restore-*.tmp"))


def test_recovery_rechecks_authority_after_staging_before_first_active_mutation(
    settings,
    tmp_path,
    monkeypatch,
) -> None:
    from friday.storage import _maintenance as storage_module

    with _enabled_environment(settings, tmp_path) as (storage, authority):
        storage.ensure_user("before-final-authority-check")
        backup = storage.create_backup(label="final-authority-check")
        storage.ensure_user("must-not-be-rewound-by-late-authority-drift")
        original_recovery_stage = storage_module._stage_verified_recovery_copy
        original_staged_verify = storage_module._verify_staged_recovery_copy
        original_diagnostics = storage.diagnostics

        monkeypatch.setattr(
            storage_module,
            "_stage_verified_recovery_copy",
            lambda _snapshot, _destination: (_ for _ in ()).throw(
                OSError("injected initial recovery interruption")
            ),
        )
        monkeypatch.setattr(
            storage,
            "diagnostics",
            lambda: (_ for _ in ()).throw(RuntimeError("injected target health failure")),
        )
        with (
            _backend_boundary(storage),
            pytest.raises(RuntimeError, match="durable rollback is pending"),
        ):
            storage.restore_backup(backup["database"])

        marker = storage.settings.state_dir / "database-restore.intent.json"
        intent = json.loads(marker.read_text(encoding="utf-8"))
        recovery_path = Path(str(intent["recovery_path"]))
        protected = [
            marker,
            *(path for path in recovery_path.rglob("*") if path.is_file()),
            *(
                path
                for path in (
                    storage.settings.database_path,
                    Path(f"{storage.settings.database_path}-wal"),
                    Path(f"{storage.settings.database_path}-shm"),
                )
                if path.is_file()
            ),
        ]
        protected_before = {path: path.read_bytes() for path in protected}
        advanced = False

        def advance_after_staged_validation(path, snapshot):  # noqa: ANN001, ANN202
            nonlocal advanced
            identity = original_staged_verify(path, snapshot)
            if not advanced:
                _advance(authority, "authority-drift-after-recovery-staging")
                advanced = True
            return identity

        monkeypatch.setattr(
            storage_module,
            "_stage_verified_recovery_copy",
            original_recovery_stage,
        )
        monkeypatch.setattr(
            storage_module,
            "_verify_staged_recovery_copy",
            advance_after_staged_validation,
        )
        monkeypatch.setattr(storage, "diagnostics", original_diagnostics)
        with (
            _backend_boundary(storage),
            pytest.raises(RuntimeError, match="Engineer authority changed"),
        ):
            storage_module._recover_interrupted_restore(  # noqa: SLF001
                storage.settings,
                storage.settings.database_path.absolute(),
                engineer_authority=authority,
            )

        assert advanced is True
        assert {path: path.read_bytes() for path in protected} == protected_before
        assert not list(storage.settings.database_path.parent.glob("*.restore-*.tmp"))


@pytest.mark.parametrize("drift_kind", ["intent_hmac", "exact_store_identity"])
def test_recovery_rejects_late_authority_identity_drift_without_active_mutation(
    settings,
    tmp_path,
    monkeypatch,
    drift_kind,
) -> None:
    from friday.storage import _maintenance as storage_module

    with _enabled_environment(settings, tmp_path) as (storage, authority):
        storage.ensure_user(f"before-{drift_kind}")
        backup = storage.create_backup(label=f"late-{drift_kind}")
        storage.ensure_user(f"must-survive-{drift_kind}")
        original_recovery_stage = storage_module._stage_verified_recovery_copy
        original_staged_verify = storage_module._verify_staged_recovery_copy
        original_diagnostics = storage.diagnostics

        def fail_recovery_stage(_snapshot, _destination):  # noqa: ANN001, ANN202
            raise OSError("injected initial recovery interruption")

        def fail_target_health():  # noqa: ANN202
            raise RuntimeError("injected target health failure")

        monkeypatch.setattr(
            storage_module,
            "_stage_verified_recovery_copy",
            fail_recovery_stage,
        )
        monkeypatch.setattr(storage, "diagnostics", fail_target_health)
        with (
            _backend_boundary(storage),
            pytest.raises(RuntimeError, match="durable rollback is pending"),
        ):
            storage.restore_backup(backup["database"])

        marker = storage.settings.state_dir / "database-restore.intent.json"
        active_paths = [
            path
            for path in (
                storage.settings.database_path,
                Path(f"{storage.settings.database_path}-wal"),
                Path(f"{storage.settings.database_path}-shm"),
            )
            if path.is_file()
        ]
        active_before = {path: path.read_bytes() for path in active_paths}
        reached_post_stage = False

        def drift_after_staged_validation(path, snapshot):  # noqa: ANN001, ANN202
            nonlocal reached_post_stage
            identity = original_staged_verify(path, snapshot)
            if not reached_post_stage and drift_kind == "intent_hmac":
                changed = json.loads(marker.read_text(encoding="utf-8"))
                changed["engineer_command_ledger_authority"]["mac"] = "f" * 64
                marker.write_text(json.dumps(changed), encoding="utf-8")
                marker.chmod(0o600)
            reached_post_stage = True
            return identity

        class SameGenerationReplacement:
            def __init__(self) -> None:
                self.calls = 0

            def verify_main_database_backup_authority(self, evidence, digest):  # noqa: ANN001, ANN202
                self.calls += 1
                observed = authority.verify_main_database_backup_authority(evidence, digest)
                if self.calls >= 3:
                    return "f" * 32, observed[1], observed[2]
                return observed

        supplied_authority = (
            SameGenerationReplacement() if drift_kind == "exact_store_identity" else authority
        )
        monkeypatch.setattr(
            storage_module,
            "_stage_verified_recovery_copy",
            original_recovery_stage,
        )
        monkeypatch.setattr(
            storage_module,
            "_verify_staged_recovery_copy",
            drift_after_staged_validation,
        )
        monkeypatch.setattr(storage, "diagnostics", original_diagnostics)
        expected = (
            "Restore intent or recovery authority changed"
            if drift_kind == "intent_hmac"
            else "Engineer authority changed"
        )
        with (
            _backend_boundary(storage),
            pytest.raises(RuntimeError, match=expected),
        ):
            storage_module._recover_interrupted_restore(  # noqa: SLF001
                storage.settings,
                storage.settings.database_path.absolute(),
                engineer_authority=supplied_authority,
            )

        assert reached_post_stage is True
        assert {path: path.read_bytes() for path in active_paths} == active_before
        assert not list(storage.settings.database_path.parent.glob("*.restore-*.tmp"))


def test_online_observer_rejects_tampered_command_schema_and_triggers(
    settings,
    tmp_path,
) -> None:
    with _enabled_environment(settings, tmp_path) as (storage, _authority):
        with sqlite3.connect(storage.settings.engineer_command_store_dir / "kernel.sqlite") as raw:
            raw.execute("DROP TRIGGER trg_engineer_command_source_slot_immutable_delete")
        with (
            open_engineer_command_backup_authority(storage.settings) as observer,
            pytest.raises(CommandError, match="schema_invalid"),
        ):
            observer.backup_authority_snapshot()


def test_online_observer_rejects_kernel_lock_path_inode_replacement(
    settings,
    tmp_path,
) -> None:
    with _enabled_environment(settings, tmp_path) as (storage, _authority):
        lock_path = storage.settings.engineer_command_store_dir / "kernel.lock"
        displaced = lock_path.with_name("kernel.lock.displaced")
        with open_engineer_command_backup_authority(storage.settings) as observer:
            os.replace(lock_path, displaced)
            lock_path.write_bytes(b"")
            lock_path.chmod(0o600)
            with pytest.raises(CommandError, match="backup_authority_unavailable"):
                observer.backup_authority_snapshot()
