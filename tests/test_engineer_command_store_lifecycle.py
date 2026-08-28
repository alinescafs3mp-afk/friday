"""Fail-closed lifecycle contract for the external command authority ledger."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from friday.engineer_source_binding import canonical_engineer_source_binding_sha256
from friday.organs import ServiceContext
from friday.organs.engineer.command import (
    CommandGrantAuthority,
    CommandKernel,
    OwnerConfirmationAuthority,
    OwnerSourceAuthority,
)
from friday.organs.engineer.command import store as store_module
from friday.organs.engineer.command import store_lifecycle as lifecycle_module
from friday.organs.engineer.command.contracts import CommandError
from friday.organs.engineer.command.store import CommandJobStore
from friday.organs.engineer.command_tools import (
    EngineerCommandService,
    provision_engineer_command_store,
)

_KEY = b"engineer-command-lifecycle-test!"
_OTHER_KEY = b"different-command-lifecycle-key!"
_SOURCE_STEP_ID = "ecstep-" + "1" * 32


def _meta(root: Path) -> tuple[str, int, int]:
    with sqlite3.connect(root / "kernel.sqlite") as connection:
        row = connection.execute(
            """SELECT store_id,schema_version,authority_sequence
                 FROM command_store_lifecycle_meta WHERE singleton=1"""
        ).fetchone()
    assert row is not None
    return str(row[0]), int(row[1]), int(row[2])


def _job_payload(
    *,
    job_digit: str = "2",
    step_digit: str = "1",
    key_digit: str = "4",
    command_digit: str = "5",
    source_row_id: str = "message-row",
    telegram_update_id: str = "telegram-update",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": job_digit * 32,
        "actor_id": "owner",
        "tenant_id": "tenant",
        "conversation_id": "conversation",
        "channel": "telegram",
        "source_row_id": source_row_id,
        "source_step_id": "ecstep-" + step_digit * 32,
        "source_hash": "3" * 64,
        "telegram_update_id": telegram_update_id,
        "isolation_profile": "host_user",
        "host_user_authorized": True,
        "idempotency_key": "ecmd-" + key_digit * 64,
        "command_digest": command_digit * 64,
        "input_manifest_sha256": "",
        "argv_sha256": "6" * 64,
        "lane": "argv",
        "origin": "model",
        "status": "admitted",
        "grant_nonce": "grant-nonce",
        "timeout_sec": 0,
        "max_stdout_bytes": 1024,
        "max_stderr_bytes": 1024,
        "created_at": time.time(),
        "executable_json": "{}",
        "delivery_chat_id": "123",
    }
    payload["source_binding_sha256"] = canonical_engineer_source_binding_sha256(
        owner_id="owner",
        tenant_id="tenant",
        conversation_id="conversation",
        channel="telegram",
        source_row_id=source_row_id,
        source_step_id="ecstep-" + step_digit * 32,
        source_hash="3" * 64,
        telegram_update_id=telegram_update_id,
        delivery_chat_id="123",
    )
    return payload


def _authority() -> CommandGrantAuthority:
    source = OwnerSourceAuthority(b"S" * 32)
    confirmation = OwnerConfirmationAuthority(b"C" * 32)
    return CommandGrantAuthority(b"G" * 32, source, confirmation)


def _interrupt_first_anchor(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_write = lifecycle_module._atomic_write_private

    def _fail_anchor(path: Path, payload: bytes) -> None:
        if path.name == "engineer-command-store.anchor.json":
            raise CommandError("command_store_anchor_write_failed")
        original_write(path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(lifecycle_module, "_atomic_write_private", _fail_anchor)
        with pytest.raises(CommandError, match="command_store_anchor_write_failed"):
            CommandJobStore.provision(root, lifecycle_key=_KEY)


def test_public_provisioner_derives_separate_lifecycle_authority(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    key_file = tmp_path / "engineer-command.key"
    master = b"M" * 32
    key_file.write_bytes(master)
    key_file.chmod(0o600)
    store_root = tmp_path / "command-store"
    settings = SimpleNamespace(
        engineer_command_key_file=key_file,
        engineer_command_store_dir=store_root,
        state_dir=state_dir,
    )

    result = provision_engineer_command_store(settings)

    assert result == {"status": "provisioned"}
    rendered = json.dumps(result)
    assert str(tmp_path) not in rendered
    assert master.hex() not in rendered
    lifecycle_key = hmac.new(
        master,
        b"friday-engineer-command-v1\x00store-lifecycle",
        hashlib.sha256,
    ).digest()
    runtime = CommandJobStore.open_runtime(
        store_root,
        lifecycle_key=lifecycle_key,
        lifecycle_state_dir=state_dir,
    )
    runtime.close()


def test_explicit_provision_adopts_a_nonempty_pre_lifecycle_ledger_once(tmp_path: Path) -> None:
    store_root = tmp_path / "legacy-command-store"
    legacy = CommandJobStore(store_root)
    with legacy.transaction():
        legacy.consume_nonce("preserved-legacy-nonce", exp=2**31, now=1)
    legacy.close()

    with sqlite3.connect(store_root / "kernel.sqlite") as connection:
        for name in (
            "command_store_lifecycle_meta_insert_guard",
            "command_store_lifecycle_meta_update_guard",
            "command_store_lifecycle_meta_delete_guard",
        ):
            connection.execute(f'DROP TRIGGER "{name}"')  # nosec B608 - fixed names
        connection.execute("DROP TABLE command_store_lifecycle_meta")
    (store_root / "engineer-command-store.anchor.json").unlink()
    (store_root / ".engineer-command-store.test.key").unlink()

    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    key_file = tmp_path / "engineer-command.key"
    master = b"M" * 32
    key_file.write_bytes(master)
    key_file.chmod(0o600)
    settings = SimpleNamespace(
        engineer_command_key_file=key_file,
        engineer_command_store_dir=store_root,
        state_dir=state_dir,
    )

    assert provision_engineer_command_store(settings) == {"status": "provisioned"}
    assert provision_engineer_command_store(settings) == {"status": "provisioned"}
    lifecycle_key = hmac.new(
        master,
        b"friday-engineer-command-v1\x00store-lifecycle",
        hashlib.sha256,
    ).digest()
    runtime = CommandJobStore.open_runtime(
        store_root,
        lifecycle_key=lifecycle_key,
        lifecycle_state_dir=state_dir,
    )
    try:
        row = runtime._conn.execute(  # noqa: SLF001 - preservation witness
            "SELECT kind,exp FROM grant_nonces WHERE nonce='preserved-legacy-nonce'"
        ).fetchone()
        assert tuple(row) == ("used", 2**31)
    finally:
        runtime.close()


def test_legacy_duplicate_sources_get_stable_v2_slots_without_ambiguous_aliases(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "legacy-command-store"
    legacy = CommandJobStore(store_root)
    duplicate_one = _job_payload(job_digit="2", step_digit="1", key_digit="4", command_digit="5")
    duplicate_two = _job_payload(job_digit="7", step_digit="2", key_digit="8", command_digit="9")
    singleton = _job_payload(
        job_digit="a",
        step_digit="3",
        key_digit="b",
        command_digit="c",
        source_row_id="other-message-row",
        telegram_update_id="other-telegram-update",
    )
    for payload in (duplicate_one, duplicate_two, singleton):
        legacy.insert_job(payload)
    legacy.close()

    with sqlite3.connect(store_root / "kernel.sqlite") as connection:
        source_slot_triggers = connection.execute(
            """SELECT name FROM sqlite_master
                 WHERE type='trigger' AND name GLOB 'trg_engineer_command_source_slot_*'"""
        ).fetchall()
        for (name,) in source_slot_triggers:
            connection.execute(f'DROP TRIGGER "{name}"')  # nosec B608 - sqlite schema name
        connection.execute("DROP TABLE engineer_command_source_slots")
        connection.execute("DROP TRIGGER trg_engineer_work_item_fence_job_identity_immutable")
        connection.execute(store_module._LEGACY_ENGINEER_WORK_ITEM_FENCE_JOB_IDENTITY_IMMUTABLE_SQL)  # noqa: SLF001
        connection.execute("ALTER TABLE jobs DROP COLUMN source_binding_sha256")
        connection.execute("ALTER TABLE jobs DROP COLUMN source_step_id")
        for name in (
            "command_store_lifecycle_meta_insert_guard",
            "command_store_lifecycle_meta_update_guard",
            "command_store_lifecycle_meta_delete_guard",
        ):
            connection.execute(f'DROP TRIGGER "{name}"')  # nosec B608 - fixed names
        connection.execute("DROP TABLE command_store_lifecycle_meta")
    (store_root / "engineer-command-store.anchor.json").unlink()
    (store_root / ".engineer-command-store.test.key").unlink()

    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    key_file = tmp_path / "engineer-command.key"
    master = b"M" * 32
    key_file.write_bytes(master)
    key_file.chmod(0o600)
    settings = SimpleNamespace(
        engineer_command_key_file=key_file,
        engineer_command_store_dir=store_root,
        state_dir=state_dir,
    )

    assert provision_engineer_command_store(settings) == {"status": "provisioned"}
    with sqlite3.connect(store_root / "kernel.sqlite") as connection:
        first_projection = connection.execute(
            """SELECT job_id,source_step_id,source_binding_sha256
                 FROM jobs ORDER BY job_id"""
        ).fetchall()
    assert provision_engineer_command_store(settings) == {"status": "provisioned"}
    with sqlite3.connect(store_root / "kernel.sqlite") as connection:
        second_projection = connection.execute(
            """SELECT job_id,source_step_id,source_binding_sha256
                 FROM jobs ORDER BY job_id"""
        ).fetchall()
    assert second_projection == first_projection
    assert len(first_projection) == 3
    assert len({str(row[1]) for row in first_projection}) == 3
    assert all(re.fullmatch(r"ecstep-[0-9a-f]{32}", str(row[1])) for row in first_projection)
    assert len({str(row[2]) for row in first_projection}) == 3

    lifecycle_key = hmac.new(
        master,
        b"friday-engineer-command-v1\x00store-lifecycle",
        hashlib.sha256,
    ).digest()
    runtime = CommandJobStore.open_runtime(
        store_root,
        lifecycle_key=lifecycle_key,
        lifecycle_state_dir=state_dir,
    )
    try:
        aliases = {
            str(row["job_id"]): row["legacy_source_binding_sha256"]
            for row in runtime._conn.execute(  # noqa: SLF001 - migration witness
                """SELECT job_id,legacy_source_binding_sha256
                     FROM engineer_command_source_slots WHERE target_kind='job'"""
            )
        }
        assert aliases[str(duplicate_one["job_id"])] is None
        assert aliases[str(duplicate_two["job_id"])] is None
        singleton_alias = aliases[str(singleton["job_id"])]
        assert isinstance(singleton_alias, str)
        assert (
            runtime.lookup_engineer_command_source_slot(
                "owner",
                "f" * 64,
                legacy_source_binding_sha256=singleton_alias,
            )["job_id"]
            == singleton["job_id"]
        )
        for payload in (duplicate_one, duplicate_two, singleton):
            assert (
                runtime.lookup_engineer_command_source_slot_by_key(
                    "owner",
                    str(payload["idempotency_key"]),
                )["job_id"]
                == payload["job_id"]
            )
    finally:
        runtime.close()


def test_kernel_and_service_runtime_open_never_provision_missing_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    missing_kernel_root = tmp_path / "missing-kernel-store"
    with pytest.raises(CommandError, match="command_store_state_dir_invalid"):
        CommandKernel(
            missing_kernel_root,
            _authority(),
            lifecycle_mode="runtime",
            lifecycle_key=_KEY,
            lifecycle_state_dir=state_dir,
        )
    assert not missing_kernel_root.exists()

    key_file = tmp_path / "engineer-command.key"
    key_file.write_bytes(b"M" * 32)
    key_file.chmod(0o600)
    missing_service_root = tmp_path / "missing-service-store"
    missing_service_root.mkdir(mode=0o700)
    (missing_service_root / "jobs").mkdir(mode=0o700)
    (missing_service_root / "workbenches").mkdir(mode=0o700)
    settings = SimpleNamespace(
        engineer_command_key_file=key_file,
        engineer_command_store_dir=missing_service_root,
        state_dir=state_dir,
    )
    context = ServiceContext(settings=settings, storage=None, kg=None, ingestion=None)
    with pytest.raises(CommandError, match="command_store_database_missing"):
        EngineerCommandService(context)
    assert not (missing_service_root / "kernel.sqlite").exists()
    assert not (state_dir / "engineer-command-store.anchor.json").exists()


def test_explicit_provision_rejects_a_store_root_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    alias = tmp_path / "command-store"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(CommandError, match="command_store_state_dir_invalid"):
        CommandJobStore.provision(
            alias,
            lifecycle_key=_KEY,
            lifecycle_state_dir=tmp_path / "state",
        )

    assert list(target.iterdir()) == []


def test_cli_provision_parser_requires_the_default_stopped_backend_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday import cli
    from friday.diagnostics import runtime_lease

    args = cli.build_parser().parse_args(["engineer-command-store-provision"])
    assert args.handler is cli._engineer_command_store_provision  # noqa: SLF001
    assert args.command not in cli._CLI_COMMANDS_SAFE_WITH_ACTIVE_BACKEND  # noqa: SLF001
    assert args.command not in cli._CLI_COMMANDS_WITHOUT_ACCOUNT_DATA  # noqa: SLF001
    assert args.command not in cli._CLI_COMMANDS_WITH_SELF_MANAGED_BACKEND_LEASE  # noqa: SLF001

    state_dir = tmp_path / "state"
    attempted: list[str] = []

    class RefusingBackendLease:
        def __init__(self, path: Path, *, protocol: str) -> None:
            self.path = path
            expected = {
                "account-deletion.lock": "friday.account-deletion.v1",
                "backend.lock": "friday.backend.v1",
            }
            assert protocol == expected[path.name]

        def __enter__(self):
            attempted.append(self.path.name)
            if self.path.name == "backend.lock":
                raise runtime_lease.RuntimeLeaseError("backend active")
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
            return None

    monkeypatch.setattr("friday.config.load_settings", lambda: SimpleNamespace(state_dir=state_dir))
    monkeypatch.setattr("friday.config.ensure_runtime_dirs", lambda _settings: [])
    monkeypatch.setattr(runtime_lease, "ProcessLease", RefusingBackendLease)
    with pytest.raises(runtime_lease.RuntimeLeaseError, match="backend active"):
        cli._run_cli_handler(args)  # noqa: SLF001
    assert attempted == ["account-deletion.lock", "backend.lock"]


def test_explicit_provision_and_exact_runtime_restart_advance_authenticated_sequence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "command-store"
    provisioned = CommandJobStore.provision(root, lifecycle_key=_KEY)
    store_id, version, sequence = _meta(root)
    assert len(store_id) == 32
    assert (version, sequence) == (1, 0)
    with provisioned.transaction():
        provisioned.consume_nonce("nonce-1", exp=2**31, now=1)
    provisioned.close()

    assert _meta(root) == (store_id, 1, 1)
    anchor = json.loads((root / "engineer-command-store.anchor.json").read_text())
    assert anchor["store_id"] == store_id
    assert anchor["authority_sequence"] == 1
    assert anchor["schema_version"] == 1
    assert anchor["mac"] not in {_KEY.hex(), _OTHER_KEY.hex()}

    restarted = CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    try:
        assert restarted.nonce_revoked("nonce-1") is False
        with restarted.transaction():
            restarted.consume_nonce("nonce-2", exp=2**31, now=1)
    finally:
        restarted.close()
    exact_restart = CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    exact_restart.close()
    assert _meta(root) == (store_id, 1, 2)


def test_first_provision_anchor_failure_recovers_from_authenticated_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "command-store"
    _interrupt_first_anchor(root, monkeypatch)

    store_id, version, sequence = _meta(root)
    bootstrap_path = root / "engineer-command-store.bootstrap.json"
    bootstrap = json.loads(bootstrap_path.read_text())
    database_stat = (root / "kernel.sqlite").stat()
    assert (version, sequence) == (1, 0)
    assert bootstrap["store_id"] == store_id
    assert bootstrap["authority_sequence"] == 0
    assert bootstrap["schema_version"] == 1
    assert bootstrap["database_device"] == database_stat.st_dev
    assert bootstrap["database_inode"] == database_stat.st_ino
    assert bootstrap_path.stat().st_mode & 0o777 == 0o600
    assert root.stat().st_mode & 0o777 == 0o700
    assert not (root / "engineer-command-store.anchor.json").exists()

    recovered = CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    recovered.close()

    assert not bootstrap_path.exists()
    anchor = json.loads((root / "engineer-command-store.anchor.json").read_text())
    assert anchor["store_id"] == store_id
    assert anchor["authority_sequence"] == 0


def test_first_provision_reuses_bootstrap_after_meta_transaction_never_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "command-store"
    with monkeypatch.context() as patch:
        patch.setattr(lifecycle_module, "_META_TABLE_SQL", "not valid sqlite")
        with pytest.raises(sqlite3.OperationalError):
            CommandJobStore.provision(root, lifecycle_key=_KEY)

    bootstrap_path = root / "engineer-command-store.bootstrap.json"
    bootstrap_store_id = json.loads(bootstrap_path.read_text())["store_id"]
    with sqlite3.connect(root / "kernel.sqlite") as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='command_store_lifecycle_meta'"
            ).fetchone()
            is None
        )

    recovered = CommandJobStore.provision(root, lifecycle_key=_KEY)
    recovered.close()

    assert _meta(root) == (bootstrap_store_id, 1, 0)
    assert not bootstrap_path.exists()
    anchor = json.loads((root / "engineer-command-store.anchor.json").read_text())
    assert anchor["store_id"] == bootstrap_store_id


def test_first_provision_recovers_anchor_published_before_bootstrap_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "command-store"
    original_remove = lifecycle_module.CommandStoreLifecycle._remove_private

    def _fail_bootstrap_remove(
        lifecycle: lifecycle_module.CommandStoreLifecycle,
        path: Path,
    ) -> None:
        if path == lifecycle.bootstrap_path:
            raise CommandError("command_store_anchor_write_failed")
        original_remove(lifecycle, path)

    with monkeypatch.context() as patch:
        patch.setattr(
            lifecycle_module.CommandStoreLifecycle,
            "_remove_private",
            _fail_bootstrap_remove,
        )
        with pytest.raises(CommandError, match="command_store_anchor_write_failed"):
            CommandJobStore.provision(root, lifecycle_key=_KEY)

    assert (root / "engineer-command-store.anchor.json").is_file()
    assert (root / "engineer-command-store.bootstrap.json").is_file()
    recovered = CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    recovered.close()
    assert not (root / "engineer-command-store.bootstrap.json").exists()


def test_first_provision_rejects_missing_or_forged_bootstrap_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_root = tmp_path / "missing-bootstrap"
    _interrupt_first_anchor(missing_root, monkeypatch)
    (missing_root / "engineer-command-store.bootstrap.json").unlink()
    with pytest.raises(CommandError, match="command_store_anchor_invalid"):
        CommandJobStore.open_runtime(missing_root, lifecycle_key=_KEY)

    forged_root = tmp_path / "forged-bootstrap"
    _interrupt_first_anchor(forged_root, monkeypatch)
    forged_path = forged_root / "engineer-command-store.bootstrap.json"
    forged = json.loads(forged_path.read_text())
    forged["store_id"] = "f" * 32
    forged_path.write_text(json.dumps(forged))
    forged_path.chmod(0o600)
    with pytest.raises(CommandError, match="command_store_lifecycle_mismatch"):
        CommandJobStore.open_runtime(forged_root, lifecycle_key=_KEY)


def test_first_provision_rejects_valid_bootstrap_for_a_different_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _interrupt_first_anchor(first_root, monkeypatch)
    _interrupt_first_anchor(second_root, monkeypatch)
    shutil.copyfile(
        second_root / "engineer-command-store.bootstrap.json",
        first_root / "engineer-command-store.bootstrap.json",
    )

    with pytest.raises(CommandError, match="command_store_lifecycle_mismatch"):
        CommandJobStore.open_runtime(first_root, lifecycle_key=_KEY)


def test_runtime_open_never_creates_a_missing_or_zero_byte_database(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing"
    with pytest.raises(CommandError, match="command_store_state_dir_invalid"):
        CommandJobStore.open_runtime(missing_root, lifecycle_key=_KEY)
    assert not missing_root.exists()

    root = tmp_path / "command-store"
    store = CommandJobStore.provision(root, lifecycle_key=_KEY)
    store.close()
    database = root / "kernel.sqlite"
    database.unlink()
    with pytest.raises(CommandError, match="command_store_database_missing"):
        CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    assert not database.exists()

    database.touch(mode=0o600)
    with pytest.raises(CommandError, match="command_store_database_empty"):
        CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    assert database.stat().st_size == 0


def test_runtime_rejects_replaced_database_even_when_other_store_is_valid(tmp_path: Path) -> None:
    root = tmp_path / "command-store"
    other_root = tmp_path / "other-command-store"
    first = CommandJobStore.provision(root, lifecycle_key=_KEY)
    first.close()
    other = CommandJobStore.provision(other_root, lifecycle_key=_KEY)
    other.close()

    os.replace(other_root / "kernel.sqlite", root / "kernel.sqlite")
    with pytest.raises(CommandError, match="command_store_lifecycle_mismatch"):
        CommandJobStore.open_runtime(root, lifecycle_key=_KEY)


def test_runtime_rejects_database_and_anchor_with_an_alias_path(tmp_path: Path) -> None:
    database_root = tmp_path / "database-alias"
    store = CommandJobStore.provision(database_root, lifecycle_key=_KEY)
    store.close()
    os.link(database_root / "kernel.sqlite", tmp_path / "kernel-alias.sqlite")
    with pytest.raises(CommandError, match="command_store_database_invalid"):
        CommandJobStore.open_runtime(database_root, lifecycle_key=_KEY)

    anchor_root = tmp_path / "anchor-alias"
    store = CommandJobStore.provision(anchor_root, lifecycle_key=_KEY)
    store.close()
    os.link(
        anchor_root / "engineer-command-store.anchor.json",
        tmp_path / "anchor-alias.json",
    )
    with pytest.raises(CommandError, match="command_store_anchor_invalid"):
        CommandJobStore.open_runtime(anchor_root, lifecycle_key=_KEY)


def test_runtime_rejects_a_missing_required_retry_column_without_migrating(
    tmp_path: Path,
) -> None:
    root = tmp_path / "missing-runtime-column"
    store = CommandJobStore.provision(root, lifecycle_key=_KEY)
    store.close()
    with sqlite3.connect(root / "kernel.sqlite") as connection:
        connection.execute("ALTER TABLE command_job_publications DROP COLUMN next_attempt_at")

    with pytest.raises(CommandError, match="schema_invalid"):
        CommandJobStore.open_runtime(root, lifecycle_key=_KEY)

    with sqlite3.connect(root / "kernel.sqlite") as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(command_job_publications)")}
    assert "next_attempt_at" not in columns


def test_runtime_rejects_in_place_rollback_to_an_older_authority_sequence(tmp_path: Path) -> None:
    root = tmp_path / "command-store"
    old_image = tmp_path / "old.sqlite"
    store = CommandJobStore.provision(root, lifecycle_key=_KEY)
    store.close()
    shutil.copyfile(root / "kernel.sqlite", old_image)

    current = CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    with current.transaction():
        current.consume_nonce("newer-authority", exp=2**31, now=1)
    current.close()
    assert _meta(root)[2] == 1

    # copyfile truncates/repopulates the existing inode, so this specifically
    # proves the sequence anchor rather than the inode replacement check.
    inode = (root / "kernel.sqlite").stat().st_ino
    shutil.copyfile(old_image, root / "kernel.sqlite")
    assert (root / "kernel.sqlite").stat().st_ino == inode
    with pytest.raises(CommandError, match="command_store_lifecycle_mismatch"):
        CommandJobStore.open_runtime(root, lifecycle_key=_KEY)


def test_runtime_rejects_corrupt_database_and_wrong_anchor_key(tmp_path: Path) -> None:
    wrong_key_root = tmp_path / "wrong-key"
    store = CommandJobStore.provision(wrong_key_root, lifecycle_key=_KEY)
    store.close()
    with pytest.raises(CommandError, match="command_store_lifecycle_mismatch"):
        CommandJobStore.open_runtime(wrong_key_root, lifecycle_key=_OTHER_KEY)

    corrupt_root = tmp_path / "corrupt"
    store = CommandJobStore.provision(corrupt_root, lifecycle_key=_KEY)
    store.close()
    database = corrupt_root / "kernel.sqlite"
    with database.open("r+b") as handle:
        handle.seek(0)
        handle.write(b"not sqlite format")
        handle.flush()
        os.fsync(handle.fileno())
    with pytest.raises(CommandError, match="command_store_database_corrupt"):
        CommandJobStore.open_runtime(corrupt_root, lifecycle_key=_KEY)


def test_anchor_publish_failure_never_returns_authority_and_poison_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "command-store"
    store = CommandJobStore.provision(root, lifecycle_key=_KEY)

    original_write = lifecycle_module._atomic_write_private

    def _fail_anchor(path: Path, payload: bytes) -> None:
        if path.name == "engineer-command-store.anchor.json":
            raise CommandError("command_store_anchor_write_failed")
        original_write(path, payload)

    monkeypatch.setattr(lifecycle_module, "_atomic_write_private", _fail_anchor)
    with pytest.raises(CommandError, match="command_store_anchor_write_failed"), store.transaction():
        store.consume_nonce("committed-without-anchor", exp=2**31, now=1)
    with pytest.raises(CommandError, match="command_store_lifecycle_unavailable"), store.transaction():
        pass
    store.close()
    monkeypatch.undo()

    # The DB commit is durable, but no caller crossed the return barrier. The
    # authenticated pending transition lets an exact restart finish the anchor.
    assert _meta(root)[2] == 1
    recovered = CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    recovered.close()
    assert not (root / "engineer-command-store.pending.json").exists()


def test_restart_retires_authenticated_pending_barrier_when_sqlite_never_committed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "command-store"
    store = CommandJobStore.provision(root, lifecycle_key=_KEY)
    # Simulate termination after the fsynced pre-commit intent but before BEGIN.
    assert store._lifecycle.begin_barrier(store._conn) == 1  # noqa: SLF001
    assert (root / "engineer-command-store.pending.json").is_file()
    store.close()

    recovered = CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    recovered.close()
    assert _meta(root)[2] == 0
    assert not (root / "engineer-command-store.pending.json").exists()


def test_base_exception_rolls_back_barrier_without_poisoning_live_store(tmp_path: Path) -> None:
    class SyntheticAbort(BaseException):
        pass

    root = tmp_path / "command-store"
    store = CommandJobStore.provision(root, lifecycle_key=_KEY)
    try:
        with pytest.raises(SyntheticAbort), store.transaction():
            store.consume_nonce("must-rollback", exp=2**31, now=1)
            raise SyntheticAbort
        assert not (root / "engineer-command-store.pending.json").exists()
        assert store._conn.in_transaction is False  # noqa: SLF001 - crash-boundary witness
        with store.transaction():
            store.consume_nonce("must-commit", exp=2**31, now=1)
    finally:
        store.close()

    assert _meta(root)[2] == 1
    runtime = CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    try:
        assert (
            runtime._conn.execute(  # noqa: SLF001 - exact rollback witness
                "SELECT 1 FROM grant_nonces WHERE nonce='must-rollback'"
            ).fetchone()
            is None
        )
        assert (
            runtime._conn.execute(  # noqa: SLF001 - exact commit witness
                "SELECT 1 FROM grant_nonces WHERE nonce='must-commit'"
            ).fetchone()
            is not None
        )
    finally:
        runtime.close()


def test_committed_barrier_turns_post_commit_database_rollback_into_hard_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "command-store"
    old_image = tmp_path / "old.sqlite"
    provisioned = CommandJobStore.provision(root, lifecycle_key=_KEY)
    provisioned.close()
    shutil.copyfile(root / "kernel.sqlite", old_image)

    store = CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    sequence = store._lifecycle.begin_barrier(store._conn)  # noqa: SLF001
    store._conn.execute("BEGIN IMMEDIATE")  # noqa: SLF001
    store._conn.execute(  # noqa: SLF001
        "INSERT INTO grant_nonces(nonce,kind,exp) VALUES('committed','used',2147483648)"
    )
    store._lifecycle.advance_in_transaction(store._conn, sequence)  # noqa: SLF001
    store._conn.execute("COMMIT")  # noqa: SLF001
    store._lifecycle.mark_committed(store._conn, sequence)  # noqa: SLF001
    store.close()
    assert (root / "engineer-command-store.committed.json").is_file()

    # A restore after the fsynced COMMIT proof must never be confused with the
    # safe pre-BEGIN crash window tested above.
    inode = (root / "kernel.sqlite").stat().st_ino
    shutil.copyfile(old_image, root / "kernel.sqlite")
    assert (root / "kernel.sqlite").stat().st_ino == inode
    with pytest.raises(CommandError, match="command_store_lifecycle_mismatch"):
        CommandJobStore.open_runtime(root, lifecycle_key=_KEY)


def test_runtime_persists_and_projects_exact_authenticated_source_step(tmp_path: Path) -> None:
    root = tmp_path / "command-store"
    provisioned = CommandJobStore.provision(root, lifecycle_key=_KEY)
    provisioned.close()
    runtime = CommandJobStore.open_runtime(root, lifecycle_key=_KEY)
    payload = _job_payload()
    try:
        with runtime.transaction():
            runtime.insert_job(payload)
        binding = runtime.lookup_idempotency_binding(
            str(payload["actor_id"]),
            str(payload["idempotency_key"]),
        )
        assert binding is not None
        assert binding["source_step_id"] == _SOURCE_STEP_ID
        assert runtime.read_job(str(payload["job_id"]))["source_step_id"] == _SOURCE_STEP_ID

        invalid = _job_payload()
        invalid["job_id"] = "7" * 32
        invalid["idempotency_key"] = "ecmd-" + "8" * 64
        invalid["source_step_id"] = ""
        with pytest.raises(CommandError, match="invalid_job_source_step"), runtime.transaction():
            runtime.insert_job(invalid)
    finally:
        runtime.close()
