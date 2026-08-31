from __future__ import annotations

import hashlib
import json
import os
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools import immutable_release_operator as release_operator
from tools import release_artifact_retention_operator as retention_apply
from tools import release_dr_generation_authentication as dr_auth
from tools import release_dr_generation_index as dr_index
from tools import release_dr_generation_rehearsal as rehearsal


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _private(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _sqlite(path: Path, *, schema: int | None, marker: str) -> None:
    connection = sqlite3.connect(path)
    if schema is not None:
        connection.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        connection.execute("INSERT INTO schema_meta VALUES('schema_version',?)", (str(schema),))
    connection.execute("CREATE TABLE marker(value TEXT NOT NULL)")
    connection.execute("INSERT INTO marker VALUES(?)", (marker,))
    connection.commit()
    connection.close()
    path.chmod(0o600)


def _source_config(root: Path) -> release_operator.SystemdConfig:
    root.chmod(0o700)
    data = _private(root / "data")
    state = _private(data / "state")
    backups = _private(data / "backups")
    units = _private(root / "units")
    database = data / "friday.sqlite3"
    inbox = state / "telegram-inbox.sqlite3"
    _sqlite(database, schema=46, marker="main-before")
    _sqlite(inbox, schema=None, marker="inbox-before")
    obsidian = _private(data / "obsidian")
    (obsidian / "note.md").write_text("private note\n", encoding="utf-8")
    (obsidian / "note.md").chmod(0o600)
    store = _private(data / "engineer-command")
    key = data / "engineer-command.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    from friday.organs.engineer.command_tools import provision_engineer_command_store

    provision_engineer_command_store(
        SimpleNamespace(
            engineer_command_enabled=True,
            engineer_command_key_file=key,
            engineer_command_store_dir=store,
            state_dir=state,
        )
    )
    (store / "job.bin").write_bytes(b"private-result")
    (store / "job.bin").chmod(0o600)
    env = root / "env"
    ca = root / "ca"
    env.write_bytes(b"# test\n")
    ca.write_bytes(b"test-ca\n")
    env.chmod(0o600)
    ca.chmod(0o600)
    return release_operator.SystemdConfig(
        anchor=root / "active",
        env_file=env,
        env_file_sha256=hashlib.sha256(env.read_bytes()).hexdigest(),
        friday_home=root,
        unit_dir=units,
        database=database,
        inbox_database=inbox,
        backup_dir=backups,
        state_dir=state,
        health_ca=ca,
        health_ca_sha256=hashlib.sha256(ca.read_bytes()).hexdigest(),
        obsidian_mode="enabled",
        obsidian_root=obsidian,
    )


def _fresh_config(root: Path) -> release_operator.SystemdConfig:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    data = _private(root / "data")
    state = _private(data / "state")
    backups = _private(data / "backups")
    units = _private(root / "units")
    env = root / "env"
    ca = root / "ca"
    env.write_bytes(b"# test\n")
    ca.write_bytes(b"test-ca\n")
    env.chmod(0o600)
    ca.chmod(0o600)
    return release_operator.SystemdConfig(
        anchor=root / "active",
        env_file=env,
        env_file_sha256=hashlib.sha256(env.read_bytes()).hexdigest(),
        friday_home=root,
        unit_dir=units,
        database=data / "friday.sqlite3",
        inbox_database=state / "telegram-inbox.sqlite3",
        backup_dir=backups,
        state_dir=state,
        health_ca=ca,
        health_ca_sha256=hashlib.sha256(ca.read_bytes()).hexdigest(),
        obsidian_mode="enabled",
        obsidian_root=data / "obsidian",
    )


def test_fresh_materialization_assigns_new_engineer_identity_without_weakening_restore_fence(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    source_inode = (source.friday_home / "data/engineer-command/kernel.sqlite").stat().st_ino
    target = _fresh_config(tmp_path / "target")

    result = release_operator.materialize_exact_backup_into_fresh_contour(target, backup)

    target_database = target.friday_home / "data/engineer-command/kernel.sqlite"
    assert result.schema_version == 46
    assert target_database.stat().st_ino != source_inode
    main = sqlite3.connect(target.database)
    inbox = sqlite3.connect(target.inbox_database)
    engineer = sqlite3.connect(target_database)
    try:
        assert main.execute("SELECT value FROM marker").fetchone() == ("main-before",)
        assert inbox.execute("SELECT value FROM marker").fetchone() == ("inbox-before",)
        assert engineer.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        main.close()
        inbox.close()
        engineer.close()
    assert (target.obsidian_root / "note.md").read_text(encoding="utf-8") == "private note\n"
    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^engineer_store_database_identity_changed$",
    ):
        release_operator._restore_exact_sqlite_backup(target, backup)  # noqa: SLF001


@pytest.mark.parametrize("occupied", ("database", "inbox", "obsidian", "engineer"))
def test_fresh_materialization_refuses_every_preexisting_target(
    tmp_path: Path,
    occupied: str,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    target = _fresh_config(tmp_path / "target")
    paths = {
        "database": target.database,
        "inbox": target.inbox_database,
        "obsidian": target.obsidian_root,
        "engineer": target.friday_home / "data/engineer-command",
    }
    path = paths[occupied]
    if occupied in {"obsidian", "engineer"}:
        path.mkdir(mode=0o700)
    else:
        path.write_bytes(b"occupied")
        path.chmod(0o600)

    with pytest.raises(release_operator.ReleaseFailure, match="target_not_absent"):
        release_operator.materialize_exact_backup_into_fresh_contour(target, backup)


@pytest.mark.parametrize("entry_kind", ("symlink", "hardlink", "fifo"))
def test_fresh_materialization_refuses_target_escape_entries_without_touching_external_file(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    target = _fresh_config(tmp_path / "target")
    external = tmp_path / "external-private"
    external.write_bytes(b"must-survive")
    external.chmod(0o600)
    if entry_kind == "symlink":
        target.database.symlink_to(external)
    elif entry_kind == "hardlink":
        os.link(external, target.database)
    else:
        os.mkfifo(target.database, mode=0o600)

    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^fresh_materialization_target_not_absent$",
    ):
        release_operator.materialize_exact_backup_into_fresh_contour(target, backup)

    assert external.read_bytes() == b"must-survive"


@pytest.mark.parametrize("surface", ("database", "inbox", "obsidian", "engineer"))
def test_fresh_materialization_rejects_each_tampered_backup_surface(
    tmp_path: Path,
    surface: str,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, release_operator._ExactBackupPayload)  # noqa: SLF001
    paths = {
        "database": payload.directory / "database.sqlite3",
        "inbox": payload.directory / "inbox.sqlite3",
        "obsidian": payload.directory / "obsidian-root/note.md",
        "engineer": payload.directory / "engineer-recovery/store/job.bin",
    }
    target = paths[surface]
    target.chmod(0o600)
    target.write_bytes(target.read_bytes() + b"tamper")
    target.chmod(0o400)

    with pytest.raises(release_operator.ReleaseFailure):
        release_operator.materialize_exact_backup_into_fresh_contour(
            _fresh_config(tmp_path / "target"),
            backup,
        )


def test_fresh_materialization_rejects_source_modify_copy_restore_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, release_operator._ExactBackupPayload)  # noqa: SLF001
    source_database = payload.directory / "database.sqlite3"
    original_copy = release_operator._copy_private  # noqa: SLF001
    injected = [False]

    def copy_with_aba(source_path: Path, destination: Path, **kwargs: Any) -> None:
        if source_path == source_database and not injected[0]:
            injected[0] = True
            original = source_path.read_bytes()
            source_path.write_bytes(original + b"temporary-source-drift")
            source_path.chmod(0o600)
            try:
                original_copy(source_path, destination, **kwargs)
            finally:
                source_path.write_bytes(original)
                source_path.chmod(0o600)
            return
        original_copy(source_path, destination, **kwargs)

    monkeypatch.setattr(release_operator, "_copy_private", copy_with_aba)

    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^fresh_materialization_destination_mismatch$",
    ):
        release_operator.materialize_exact_backup_into_fresh_contour(
            _fresh_config(tmp_path / "target"),
            backup,
        )

    assert injected == [True]


def test_fresh_materialization_accepts_zero_byte_engineer_wal_without_mutating_source(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    source_wal = source.friday_home / "data/engineer-command/kernel.sqlite-wal"
    source_wal.write_bytes(b"")
    source_wal.chmod(0o600)
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, release_operator._ExactBackupPayload)  # noqa: SLF001
    target = _fresh_config(tmp_path / "target")

    result = release_operator.materialize_exact_backup_into_fresh_contour(target, backup)

    target_wal = target.friday_home / "data/engineer-command/kernel.sqlite-wal"
    assert result.engineer_fresh_identity_assigned is True
    assert source_wal.read_bytes() == b""
    assert target_wal.is_file() and target_wal.read_bytes() == b""


def test_fresh_materialization_preserves_zero_byte_main_and_inbox_wals(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    for database in (source.database, source.inbox_database):
        wal = Path(f"{database}-wal")
        wal.write_bytes(b"")
        wal.chmod(0o600)
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    target = _fresh_config(tmp_path / "target")

    release_operator.materialize_exact_backup_into_fresh_contour(target, backup)

    assert Path(f"{target.database}-wal").read_bytes() == b""
    assert Path(f"{target.inbox_database}-wal").read_bytes() == b""


def test_fresh_materialization_preserves_committed_inbox_wal_without_creating_shm(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    connection = sqlite3.connect(source.inbox_database)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE committed_wal(value TEXT NOT NULL)")
        connection.execute("INSERT INTO committed_wal VALUES('must-survive')")
        connection.commit()
        source_wal = Path(f"{source.inbox_database}-wal")
        assert source_wal.stat().st_size > 32
        backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    finally:
        connection.close()

    payload = backup.opaque
    assert isinstance(payload, release_operator._ExactBackupPayload)  # noqa: SLF001
    backup_wal = payload.directory / "inbox.sqlite3-wal"
    backup_wal_bytes = backup_wal.read_bytes()
    backup_wal_sha256 = hashlib.sha256(backup_wal_bytes).hexdigest()
    assert len(backup_wal_bytes) > 32
    assert not (payload.directory / "inbox.sqlite3-shm").exists()
    target = _fresh_config(tmp_path / "target")

    release_operator.materialize_exact_backup_into_fresh_contour(target, backup)

    target_wal = Path(f"{target.inbox_database}-wal")
    assert target_wal.read_bytes() == backup_wal_bytes
    assert hashlib.sha256(target_wal.read_bytes()).hexdigest() == backup_wal_sha256
    assert backup_wal.read_bytes() == backup_wal_bytes
    assert not Path(f"{target.inbox_database}-shm").exists()


def test_exact_restore_verifies_wal_mode_databases_without_synthesizing_sidecars(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    for database in (source.database, source.inbox_database):
        connection = sqlite3.connect(database)
        try:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        finally:
            connection.close()
        assert not Path(f"{database}-wal").exists()
        assert not Path(f"{database}-shm").exists()
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, release_operator._ExactBackupPayload)  # noqa: SLF001
    expected = {
        name: (payload.directory / name).read_bytes() for name in ("database.sqlite3", "inbox.sqlite3")
    }

    for database in (source.database, source.inbox_database):
        connection = sqlite3.connect(database)
        try:
            connection.execute("UPDATE marker SET value='changed'")
            connection.commit()
        finally:
            connection.close()

    release_operator._restore_exact_sqlite_backup(source, backup)  # noqa: SLF001

    assert source.database.read_bytes() == expected["database.sqlite3"]
    assert source.inbox_database.read_bytes() == expected["inbox.sqlite3"]
    for database in (source.database, source.inbox_database):
        assert not Path(f"{database}-wal").exists()
        assert not Path(f"{database}-shm").exists()


def test_fresh_materialization_reports_no_identity_when_engineer_database_absent(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    shutil.rmtree(source.friday_home / "data/engineer-command")
    (source.friday_home / "data/engineer-command.key").unlink()
    for name in release_operator._ENGINEER_LIFECYCLE_FILENAMES:  # noqa: SLF001
        (source.state_dir / name).unlink(missing_ok=True)
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001

    result = release_operator.materialize_exact_backup_into_fresh_contour(
        _fresh_config(tmp_path / "target"),
        backup,
    )

    assert result.engineer_fresh_identity_assigned is False


def test_fresh_engineer_verification_scratch_is_never_created_by_source_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, release_operator._ExactBackupPayload)  # noqa: SLF001
    target = _fresh_config(tmp_path / "target")
    original = release_operator.tempfile.mkdtemp
    directories: list[Path] = []

    def bounded_mkdtemp(*args: Any, **kwargs: Any) -> str:
        directory = Path(kwargs["dir"])
        directories.append(directory)
        if directory == payload.directory.parent:
            raise AssertionError("production backup root was used as verification scratch")
        return original(*args, **kwargs)

    monkeypatch.setattr(release_operator.tempfile, "mkdtemp", bounded_mkdtemp)

    release_operator.materialize_exact_backup_into_fresh_contour(target, backup)

    assert directories and payload.directory.parent not in directories
    assert target.backup_dir in directories


def _candidate(home: Path, ordinal: int = 1) -> dict[str, Any]:
    backup = _private(home / "data/backups" / f"backup-{ordinal}")
    release = _private(home / "releases" / f"{ordinal:040x}")
    digest = lambda offset: f"{ordinal * 16 + offset:064x}"  # noqa: E731
    return {
        "allowed_rollback_tree_sha256s": sorted({digest(6), "a" * 64}),
        "backup_directory": str(backup),
        "backup_record_sha256": digest(1),
        "database_receipt_sha256": digest(2),
        "database_schema": 46,
        "engineer_receipt_sha256": digest(3),
        "inbox_receipt_sha256": digest(4),
        "obsidian_receipt_sha256": digest(5),
        "restore_release": {
            "commit": f"{ordinal:040x}",
            "max_schema": 50,
            "root": str(release),
            "tree_manifest_sha256": digest(6),
            "version": f"0.207.{ordinal}",
            "wheel_sha256": digest(7),
        },
        "schema": dr_index.GENERATION_CANDIDATE_SCHEMA,
        "source_kind": "terminal_activation",
        "source_receipt_sha256": digest(8),
        "source_transaction_id": digest(9),
    }


def _auth_receipt(candidate: dict[str, Any], ordinal: int = 1) -> dict[str, Any]:
    digest = lambda offset: f"{ordinal * 32 + offset:064x}"  # noqa: E731
    backup = Path(candidate["backup_directory"])
    backup_status = backup.stat()
    core: dict[str, Any] = {
        "allowed_rollback_tree_sha256s": candidate["allowed_rollback_tree_sha256s"],
        "activation_journal_file_sha256": digest(1),
        "activation_journal_sha256": digest(2),
        "activation_receipt_file_sha256": digest(3),
        "activation_receipt_sha256": candidate["source_receipt_sha256"],
        "backup_directory": {
            "device": backup_status.st_dev,
            "inode": backup_status.st_ino,
            "path": str(backup),
        },
        "backup_manifest_sha256": digest(5),
        "candidate_sha256": hashlib.sha256(
            _canonical(dr_index.normalize_generation_candidate(candidate))
        ).hexdigest(),
        "database_schema": candidate["database_schema"],
        "restore_operator_sha256": digest(6),
        "schema": dr_index.AUTHENTICATION_RECEIPT_SCHEMA,
        "source_transaction_id": candidate["source_transaction_id"],
        "status": "authenticated",
        "surface_receipts": {
            "database": candidate["database_receipt_sha256"],
            "engineer": candidate["engineer_receipt_sha256"],
            "inbox": candidate["inbox_receipt_sha256"],
            "obsidian": candidate["obsidian_receipt_sha256"],
        },
    }
    return {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _release(root: Path, character: str) -> release_operator.ReleaseIdentity:
    return release_operator.ReleaseIdentity(
        root=root,
        commit=character * 40,
        version="0.207.84",
        tree_manifest_sha256=character * 64,
        max_schema=50,
    )


@pytest.mark.parametrize("mismatch", ("database_schema", "allowed_rollback_trees"))
def test_material_authentication_binds_candidate_schema_and_rollback_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    payload = backup.opaque
    assert isinstance(payload, release_operator._ExactBackupPayload)  # noqa: SLF001
    releases = (
        _release(tmp_path / "candidate", "c"),
        _release(tmp_path / "previous", "a"),
        _release(tmp_path / "fallback", "f"),
    )

    def release_record(release: release_operator.ReleaseIdentity) -> dict[str, Any]:
        return {
            "commit": release.commit,
            "max_schema": release.max_schema,
            "root": str(release.root),
            "tree_manifest_sha256": release.tree_manifest_sha256,
            "version": release.version,
            "wheel_sha256": "1" * 64,
        }

    monkeypatch.setattr(dr_auth, "_release_record", release_record)
    candidate = {
        **_candidate(source.friday_home),
        "allowed_rollback_tree_sha256s": sorted(
            {releases[1].tree_manifest_sha256, releases[2].tree_manifest_sha256}
        ),
        "backup_directory": str(payload.directory),
        "database_receipt_sha256": backup.receipt_sha256,
        "database_schema": backup.schema_version,
        "engineer_receipt_sha256": backup.engineer_receipt_sha256,
        "inbox_receipt_sha256": backup.inbox_receipt_sha256,
        "obsidian_receipt_sha256": backup.obsidian_receipt_sha256,
        "restore_release": release_record(releases[2]),
    }
    authenticated = dr_auth.AuthenticatedDRCandidate(candidate, _auth_receipt(candidate))
    observed_backup = backup
    observed_releases = releases
    if mismatch == "database_schema":
        observed_backup = release_operator.DatabaseBackup(
            backup.schema_version + 1,
            backup.receipt_sha256,
            backup.inbox_receipt_sha256,
            backup.opaque,
            backup.obsidian_receipt_sha256,
            backup.engineer_receipt_sha256,
        )
    else:
        observed_releases = (releases[0], _release(tmp_path / "changed-previous", "d"), releases[2])

    class FakeJournal:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def database_backup(self, **_kwargs: Any) -> release_operator.DatabaseBackup:
            return observed_backup

        def release_identities(
            self,
        ) -> tuple[
            release_operator.ReleaseIdentity,
            release_operator.ReleaseIdentity,
            release_operator.ReleaseIdentity,
        ]:
            return observed_releases

    monkeypatch.setattr(dr_auth, "_authenticate_locked", lambda **_kwargs: authenticated)
    monkeypatch.setattr(release_operator, "DurableActivationJournal", FakeJournal)

    with pytest.raises(
        dr_auth.DRGenerationAuthenticationError,
        match="^dr_rehearsal_material_mismatch$",
    ):
        dr_auth._authenticate_material_locked(  # noqa: SLF001
            activation_journal=tmp_path / "activation-journal.json",
            activation_receipt=tmp_path / "activation-receipt.json",
            backup_root=source.backup_dir,
        )


def _capable_release(root: Path, character: str) -> release_operator.ReleaseIdentity:
    return release_operator.ReleaseIdentity(
        root=root,
        commit=character * 40,
        version="0.207.84",
        tree_manifest_sha256=character * 64,
        max_schema=46,
        venv_relocation_contract=release_operator.VENV_RELOCATION_CONTRACT,
        obsidian_cutover_contract=release_operator.OBSIDIAN_CUTOVER_CONTRACT,
        engineer_command_lifecycle_contract=(release_operator.ENGINEER_COMMAND_LIFECYCLE_CONTRACT),
    )


def _sealed(
    release: release_operator.ReleaseIdentity, root: Path | None = None
) -> rehearsal._SealedReleaseCopy:
    copied = release_operator.ReleaseIdentity(
        **{**vars(release), "root": root or release.root},
    )
    return rehearsal._SealedReleaseCopy(release, copied.root, copied)


def _local_engineer_authority(
    _sealed_release: rehearsal._SealedReleaseCopy,
    config: release_operator.SystemdConfig,
    *,
    action: str,
    database_sha256: str = "",
    evidence: Any = None,
) -> object:
    from friday.organs.engineer.command_tools import open_engineer_command_backup_authority

    store, key, state = release_operator._engineer_artifact_paths(config)  # noqa: SLF001
    settings = SimpleNamespace(
        engineer_command_enabled=True,
        engineer_command_key_file=key,
        engineer_command_store_dir=store,
        state_dir=state,
    )
    with open_engineer_command_backup_authority(settings) as authority:
        if action == "snapshot":
            return authority.backup_authority_snapshot()
        before = authority.backup_authority_snapshot()
        if action == "attest":
            proof = authority.attest_main_database_backup(database_sha256)
            verified = authority.verify_main_database_backup_authority(proof, database_sha256)
            after = authority.backup_authority_snapshot()
            return {"before": before, "evidence": proof, "verified": verified, "after": after}
        verified = authority.verify_main_database_backup_authority(evidence, database_sha256)
        after = authority.backup_authority_snapshot()
        return {"before": before, "verified": verified, "after": after}


def test_isolated_rehearsal_runs_real_four_surface_rollback_without_systemctl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    source = _source_config(_private(tmp_path / "source"))
    backup = release_operator._exact_sqlite_backup(source)  # noqa: SLF001
    releases = (
        _capable_release(tmp_path / "candidate", "c"),
        _capable_release(tmp_path / "previous", "a"),
        _capable_release(tmp_path / "fallback", "f"),
    )
    by_root = {release.root: release for release in releases}

    def load_identity(root: Path, **_kwargs: Any) -> release_operator.ReleaseIdentity:
        return by_root[root]

    monkeypatch.setattr(
        release_operator,
        "load_release_identity",
        load_identity,
    )
    monkeypatch.setattr(
        rehearsal,
        "_load_sealed_release_copy_identity",
        lambda release, _root: by_root[release.root],
    )
    monkeypatch.setattr(
        rehearsal,
        "_materialize_exact_releases",
        lambda values, _root: {release: _sealed(release) for release in values},
    )
    monkeypatch.setattr(
        rehearsal,
        "_run_release_store",
        lambda sealed, _config: {
            "inbox_fk": 0,
            "inbox_integrity": "ok",
            "main_fk": 0,
            "main_integrity": "ok",
            "main_schema": sealed.source.max_schema,
            "version": sealed.source.version,
        },
    )
    monkeypatch.setattr(rehearsal, "_run_release_engineer_authority", _local_engineer_authority)

    restored_configs: list[release_operator.SystemdConfig] = []

    def local_exact_restore(
        _sealed_release: rehearsal._SealedReleaseCopy,
        config: release_operator.SystemdConfig,
        *,
        activation_journal: Path,
        backup: release_operator.DatabaseBackup,
        expected_operator_sha256: str,
    ) -> dict[str, Any]:
        assert activation_journal.name == "immutable-release-activation.v1.json"
        assert len(expected_operator_sha256) == 64
        release_operator._restore_exact_sqlite_backup(  # noqa: SLF001
            config,
            backup,
            require_engineer_authority=True,
            engineer_authority_verify=lambda evidence, digest: _local_engineer_authority(
                _sealed_release,
                config,
                action="verify",
                database_sha256=digest,
                evidence=evidence,
            ),
        )
        for database in (config.database, config.inbox_database):
            wal = Path(f"{database}-wal")
            shm = Path(f"{database}-shm")
            wal.write_bytes(b"")
            shm.write_bytes(b"\0" * (32 << 10))
            wal.chmod(0o600)
            shm.chmod(0o600)
        restored_configs.append(config)
        return {"status": "restored"}

    monkeypatch.setattr(rehearsal, "_run_exact_fallback_restore", local_exact_restore)
    candidate = _candidate(source.friday_home)
    material = dr_auth.AuthenticatedDRMaterial(
        authenticated=dr_auth.AuthenticatedDRCandidate(candidate, _auth_receipt(candidate)),
        backup=backup,
        activation_candidate=releases[0],
        activation_previous=releases[1],
        restore_fallback=releases[2],
    )
    scratch = _private(tmp_path / "isolated")

    result = rehearsal._run_isolated_rehearsal(material, scratch)

    assert result.schema_version == 46
    assert result.rollback_tree_sha256 == releases[1].tree_manifest_sha256
    assert result.four_surface_sha256 == rehearsal._four_surface_receipt_sha256(backup)
    assert (scratch / "work/data/friday.sqlite3").exists()
    restored = restored_configs[0]
    for database in (restored.database, restored.inbox_database):
        assert not Path(f"{database}-wal").exists()
        assert not Path(f"{database}-shm").exists()
        assert len(list(database.parent.glob(".friday-dr-historical-*"))) == 2
    fifo_wal = Path(f"{restored.database}-wal")
    os.mkfifo(fifo_wal, mode=0o600)
    real_os_open = rehearsal.os.open

    def reject_fifo_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == fifo_wal.name and kwargs.get("dir_fd") is not None:
            raise AssertionError("special sidecar must be rejected before open")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(rehearsal.os, "open", reject_fifo_open)
    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^dr_rehearsal_historical_sidecar_invalid$",
    ):
        rehearsal._remove_historical_restore_derived_sidecars(restored, backup)  # noqa: SLF001
    monkeypatch.setattr(rehearsal.os, "open", real_os_open)
    fifo_wal.unlink()
    unexpected_wal = Path(f"{restored.database}-wal")
    unexpected_wal.write_bytes(b"not-inert")
    unexpected_wal.chmod(0o600)
    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^dr_rehearsal_historical_sidecar_invalid$",
    ):
        rehearsal._remove_historical_restore_derived_sidecars(restored, backup)  # noqa: SLF001
    unexpected_wal.unlink()

    real_rename_noreplace = rehearsal._rename_noreplace  # noqa: SLF001
    race_source = Path(f"{restored.database}-wal")
    race_source.write_bytes(b"")
    race_source.chmod(0o600)
    replacement = restored.database.parent / ".replacement-sidecar"
    replacement_payload = b"racing-nonempty-sidecar"
    replacement.write_bytes(replacement_payload)
    replacement.chmod(0o600)
    replacement_identity = (replacement.stat().st_dev, replacement.stat().st_ino)
    race_quarantine: list[str] = []

    def replace_before_quarantine(directory_fd: int, source_name: str, target_name: str) -> None:
        if source_name == race_source.name and not race_quarantine:
            os.replace(
                replacement.name,
                source_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            race_quarantine.append(target_name)
        real_rename_noreplace(directory_fd, source_name, target_name)

    monkeypatch.setattr(rehearsal, "_rename_noreplace", replace_before_quarantine)
    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^dr_rehearsal_historical_sidecar_invalid$",
    ):
        rehearsal._remove_historical_restore_derived_sidecars(restored, backup)  # noqa: SLF001
    quarantined_replacement = restored.database.parent / race_quarantine[0]
    assert quarantined_replacement.read_bytes() == replacement_payload
    assert (
        quarantined_replacement.stat().st_dev,
        quarantined_replacement.stat().st_ino,
    ) == replacement_identity
    assert quarantined_replacement.stat().st_nlink == 1

    collision_source = Path(f"{restored.inbox_database}-wal")
    collision_source.write_bytes(b"")
    collision_source.chmod(0o600)
    collision_identity = (collision_source.stat().st_dev, collision_source.stat().st_ino)
    collision_payload = b"collision-must-survive"
    collision_quarantine: list[str] = []

    def collide_with_quarantine(directory_fd: int, source_name: str, target_name: str) -> None:
        if source_name == collision_source.name and not collision_quarantine:
            target_fd = os.open(
                target_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.write(target_fd, collision_payload)
                os.fsync(target_fd)
            finally:
                os.close(target_fd)
            collision_quarantine.append(target_name)
        real_rename_noreplace(directory_fd, source_name, target_name)

    monkeypatch.setattr(rehearsal, "_rename_noreplace", collide_with_quarantine)
    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^dr_rehearsal_historical_sidecar_invalid$",
    ):
        rehearsal._remove_historical_restore_derived_sidecars(restored, backup)  # noqa: SLF001
    assert collision_source.read_bytes() == b""
    assert (collision_source.stat().st_dev, collision_source.stat().st_ino) == collision_identity
    collision_target = restored.inbox_database.parent / collision_quarantine[0]
    assert collision_target.read_bytes() == collision_payload
    assert collision_target.stat().st_nlink == 1


def test_exact_release_store_open_uses_closed_bounded_interpreter_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    config = _fresh_config(tmp_path / "scratch")
    release = _capable_release(tmp_path / "sealed-release", "c")
    copied_root = tmp_path / "copied-release"
    copied_root.mkdir(mode=0o500)
    copied_root.chmod(0o500)
    sealed = _sealed(release, copied_root)
    expected = {
        "inbox_fk": 0,
        "inbox_integrity": "ok",
        "main_fk": 0,
        "main_integrity": "ok",
        "main_schema": release.max_schema,
        "version": release.version,
    }
    calls: list[tuple[list[str], bytes, int, tuple[int, ...]]] = []

    def run(
        command: list[str],
        *,
        input_bytes: bytes,
        timeout: int,
        pass_fds: tuple[int, ...],
        resource_check: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        resource_check()
        calls.append((command, input_bytes, timeout, pass_fds))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_canonical(expected) + b"\n",
            stderr=b"",
        )

    monkeypatch.setattr(rehearsal, "_execute_bwrap", run)
    monkeypatch.setattr(release_operator, "load_release_identity", lambda *_args, **_kwargs: sealed.identity)
    monkeypatch.setattr(rehearsal, "_load_sealed_release_copy_identity", lambda *_args: sealed.identity)
    monkeypatch.setenv("MUST_NOT_REACH_REHEARSAL_CHILD", "secret")

    assert rehearsal._run_release_store(sealed, config) == expected
    command, input_bytes, timeout, pass_fds = calls[0]
    assert command[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in command and "--unshare-net" in command
    assert "--proc" in command and "--dev" in command and "--tmpfs" in command
    assert "--clearenv" in command and "--chdir" in command
    assert "--close-fd" not in command
    assert "/usr/bin/systemctl" not in command
    assert "/usr/bin" not in command
    assert ["/usr", "/usr"] not in [
        command[index + 1 : index + 3] for index, value in enumerate(command) if value == "--ro-bind"
    ]
    assert "/usr/bin:/bin" not in command
    assert "/run/friday/no-executables" in command
    assert command[command.index("--chdir") + 1] == str(config.friday_home)
    assert [
        command[index + 1 : index + 3] for index, value in enumerate(command) if value == "--bind-fd"
    ] == [[str(pass_fds[1]), str(config.friday_home)]]
    assert [str(pass_fds[0]), str(release.root)] in [
        command[index + 1 : index + 3] for index, value in enumerate(command) if value == "--ro-bind-fd"
    ]
    assert not any(
        command[index + 2] == "/tmp"
        for index, value in enumerate(command[:-2])
        if value in {"--bind", "--bind-fd", "--ro-bind", "--ro-bind-fd"}
    )
    separator = command.index("--")
    assert command[separator + 1 : separator + 4] == [
        str(release.root / "venv/bin/python"),
        "-I",
        "-B",
    ]
    assert "MUST_NOT_REACH_REHEARSAL_CHILD" not in "\0".join(command)
    assert "from friday.telegram_bridge import _UpdateInbox" in command[separator + 5]
    assert input_bytes == b""
    assert timeout == 600
    assert len(pass_fds) == 2


def test_installed_bwrap_accepts_pinned_fd_mounts_without_close_fd(
    tmp_path: Path,
) -> None:
    source = _private(tmp_path / "source")
    work = _private(tmp_path / "work")
    marker = source / "marker"
    marker.write_bytes(b"exact")
    marker.chmod(0o600)
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    source_fd = os.open(source, flags)
    work_fd = os.open(work, flags)
    command = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--unshare-net",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--ro-bind",
        "/lib",
        "/lib",
        "--ro-bind",
        "/lib64",
        "/lib64",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind-fd",
        str(source_fd),
        "/sealed",
        "--bind-fd",
        str(work_fd),
        "/work",
        "--",
        "/usr/bin/test",
        "-f",
        "/sealed/marker",
    ]
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            cwd=Path("/"),
            env={"LANG": "C", "LC_ALL": "C"},
            pass_fds=(source_fd, work_fd),
            timeout=10,
        )
    finally:
        os.close(work_fd)
        os.close(source_fd)

    assert completed.returncode == 0
    assert completed.stdout == completed.stderr == b""


def test_real_bwrap_hides_host_tools_cwd_tmp_and_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    config = _fresh_config(tmp_path / "work")
    copied_root = tmp_path / "copied-release"
    binary_directory = _private(copied_root / "venv/bin")
    shutil.copy2("/usr/bin/python3", binary_directory / "python")
    (binary_directory / "python").chmod(0o500)
    pyvenv = copied_root / "venv/pyvenv.cfg"
    pyvenv.write_text(
        "home = /usr/bin\ninclude-system-site-packages = false\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}\n",
        encoding="ascii",
    )
    pyvenv.chmod(0o400)
    binary_directory.chmod(0o500)
    (copied_root / "venv").chmod(0o500)
    copied_root.chmod(0o500)
    release = _capable_release(Path("/run/friday-rehearsal-test/source"), "c")
    sealed = _sealed(release, copied_root)
    monkeypatch.setattr(
        release_operator,
        "load_release_identity",
        lambda *_args, **_kwargs: sealed.identity,
    )
    monkeypatch.setattr(rehearsal, "_load_sealed_release_copy_identity", lambda *_args: sealed.identity)
    host_tmp = Path("/tmp") / f"friday-host-marker-{os.getpid()}"
    host_tmp.write_bytes(b"host-only")
    host_tmp.chmod(0o600)
    script = r"""
import json,os,pathlib,socket
network_visible=True
probe=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
try:
 probe.connect(('203.0.113.1',9))
except OSError:
 network_visible=False
finally:
 probe.close()
print(json.dumps({'cwd':os.getcwd(),'host_cwd':pathlib.Path('/home/jericho/jericho/pyproject.toml').exists(),'host_tmp':pathlib.Path(__import__('sys').argv[1]).exists(),'network':network_visible,'path':os.environ.get('PATH'),'systemctl':pathlib.Path('/usr/bin/systemctl').exists()},sort_keys=True,separators=(',',':')))
"""
    try:
        output = rehearsal._run_release_python(
            sealed,
            config,
            script=script,
            arguments=(str(host_tmp),),
        )
    finally:
        host_tmp.unlink(missing_ok=True)

    assert json.loads(output) == {
        "cwd": str(config.friday_home),
        "host_cwd": False,
        "host_tmp": False,
        "network": False,
        "path": "/run/friday/no-executables",
        "systemctl": False,
    }


def test_exact_release_mount_rejects_post_launch_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    config = _fresh_config(tmp_path / "scratch")
    release = _capable_release(tmp_path / "source-release", "c")
    copied_root = tmp_path / "copied-release"
    copied_root.mkdir(mode=0o500)
    copied_root.chmod(0o500)
    sealed = _sealed(release, copied_root)
    displaced = tmp_path / "displaced-release"

    def swap(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        copied_root.rename(displaced)
        copied_root.mkdir(mode=0o500)
        copied_root.chmod(0o500)
        return subprocess.CompletedProcess(command, 0, stdout=b"{}\n", stderr=b"")

    monkeypatch.setattr(rehearsal, "_execute_bwrap", swap)
    monkeypatch.setattr(
        release_operator,
        "load_release_identity",
        lambda *_args, **_kwargs: sealed.identity,
    )
    monkeypatch.setattr(rehearsal, "_load_sealed_release_copy_identity", lambda *_args: sealed.identity)

    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^dr_rehearsal_mount_identity_changed$",
    ):
        rehearsal._run_release_python(
            sealed,
            config,
            script="print('{}')",
            arguments=(),
        )

    assert copied_root.is_dir()
    assert displaced.is_dir()


def test_release_copy_reauthenticates_source_after_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    source_root = _private(tmp_path / "source-release")
    source_file = source_root / "sealed"
    source_file.write_bytes(b"exact")
    source_file.chmod(0o400)
    source_root.chmod(0o500)
    destination_parent = _private(tmp_path / "copies")
    destination = destination_parent / "copy"
    release = _capable_release(source_root, "c")
    copied = release_operator.ReleaseIdentity(**{**vars(release), "root": destination})
    changed = release_operator.ReleaseIdentity(**{**vars(release), "version": "0.207.85"})
    source_calls = 0

    def load(root: Path, **_kwargs: Any) -> release_operator.ReleaseIdentity:
        nonlocal source_calls
        if root == destination:
            return copied
        source_calls += 1
        return release if source_calls == 1 else changed

    monkeypatch.setattr(release_operator, "load_release_identity", load)
    monkeypatch.setattr(release_operator, "_load_release_identity_at_bound_root", load)

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^dr_rehearsal_release_copy_changed$",
    ):
        rehearsal._materialize_exact_release_copy(release, destination)

    assert source_calls == 2


def test_release_copy_authenticates_venv_for_exact_source_bind_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    source_root = _private(tmp_path / ("c" * 40))
    source_file = source_root / "sealed"
    source_file.write_bytes(b"exact")
    source_file.chmod(0o400)
    source_root.chmod(0o500)
    destination_parent = _private(tmp_path / "copies")
    destination = destination_parent / "copy"
    release = _capable_release(source_root, "c")
    copied = release_operator.ReleaseIdentity(**{**vars(release), "root": destination})
    calls: list[tuple[Path, Path | None]] = []

    def load(root: Path, **kwargs: Any) -> release_operator.ReleaseIdentity:
        calls.append((root, kwargs.get("venv_bound_root")))
        return copied if root == destination else release

    monkeypatch.setattr(release_operator, "load_release_identity", load)
    monkeypatch.setattr(release_operator, "_load_release_identity_at_bound_root", load)

    sealed = rehearsal._materialize_exact_release_copy(release, destination)

    assert sealed == rehearsal._SealedReleaseCopy(release, destination, copied)
    assert calls == [
        (source_root, None),
        (destination, source_root),
        (source_root, None),
    ]


@pytest.mark.parametrize("failure", ("stderr", "timeout", "wrong_receipt"))
def test_exact_release_store_open_fails_closed_without_stderr_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    tmp_path.chmod(0o700)
    config = _fresh_config(tmp_path / "scratch")
    release = _capable_release(tmp_path / "sealed-release", "c")
    copied_root = tmp_path / "copied-release"
    copied_root.mkdir(mode=0o500)
    copied_root.chmod(0o500)
    sealed = _sealed(release, copied_root)

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if failure == "timeout":
            raise release_operator.ReleaseFailure("dr_rehearsal_release_open_timeout")
        receipt = {
            "inbox_fk": 0,
            "inbox_integrity": "ok",
            "main_fk": 0,
            "main_integrity": "ok",
            "main_schema": release.max_schema,
            "version": "tampered" if failure == "wrong_receipt" else release.version,
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_canonical(receipt) + b"\n",
            stderr=b"private-stderr-body" if failure == "stderr" else b"",
        )

    monkeypatch.setattr(rehearsal, "_execute_bwrap", run)
    monkeypatch.setattr(release_operator, "load_release_identity", lambda *_args, **_kwargs: sealed.identity)
    monkeypatch.setattr(rehearsal, "_load_sealed_release_copy_identity", lambda *_args: sealed.identity)

    with pytest.raises(release_operator.ReleaseFailure) as raised:
        rehearsal._run_release_store(sealed, config)

    assert "private" not in str(raised.value)


def test_bwrap_timeout_kills_the_entire_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TimedOutProcess:
        pid = 74123
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: int) -> int:
            assert timeout == 5
            return -9

    process = TimedOutProcess()
    popen_kwargs: list[dict[str, Any]] = []
    signals: list[int] = []
    monkeypatch.setattr(rehearsal, "_trusted_bwrap_identity", lambda: (1, 2, 3, 4))

    def popen(*_args: Any, **kwargs: Any) -> TimedOutProcess:
        popen_kwargs.append(kwargs)
        return process

    def killpg(pid: int, sent: int) -> None:
        assert pid == process.pid
        signals.append(sent)
        if sent == 0:
            raise ProcessLookupError

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(os, "killpg", killpg)

    def timeout(*_args: Any, **_kwargs: Any) -> tuple[bytes, bytes]:
        raise subprocess.TimeoutExpired(["/usr/bin/bwrap"], 1)

    monkeypatch.setattr(rehearsal, "_bounded_communicate", timeout)

    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^dr_rehearsal_release_open_timeout$",
    ):
        rehearsal._execute_bwrap(
            ["/usr/bin/bwrap"],
            input_bytes=b"",
            timeout=1,
            pass_fds=(11, 12),
        )

    assert signals == [signal.SIGKILL]
    assert callable(popen_kwargs[0].pop("preexec_fn"))
    assert popen_kwargs == [
        {
            "cwd": Path("/"),
            "env": {"LANG": "C", "LC_ALL": "C"},
            "pass_fds": (11, 12),
            "start_new_session": True,
            "stderr": subprocess.PIPE,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
        }
    ]


def test_bwrap_never_signals_a_reaped_process_group_by_reused_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReapedProcess:
        pid = 74124
        returncode = 0

        @staticmethod
        def poll() -> int:
            return 0

    process = ReapedProcess()
    signals: list[int] = []
    monkeypatch.setattr(rehearsal, "_trusted_bwrap_identity", lambda: (1, 2, 3, 4))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(
        rehearsal,
        "_bounded_communicate",
        lambda *_args, **_kwargs: (b"ok\n", b""),
    )

    def killpg(_pid: int, sent: int) -> None:
        signals.append(sent)

    monkeypatch.setattr(os, "killpg", killpg)

    completed = rehearsal._execute_bwrap(
        ["/usr/bin/bwrap"],
        input_bytes=b"",
        timeout=1,
    )

    assert completed.returncode == 0
    assert signals == []


def test_child_output_is_bounded_before_receipt_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rehearsal, "_trusted_bwrap_identity", lambda: (1, 2, 3, 4))

    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^dr_rehearsal_release_output_too_large$",
    ):
        rehearsal._execute_bwrap(
            [
                "/usr/bin/python3",
                "-I",
                "-c",
                "import os;os.write(2,b'x'*40000)",
            ],
            input_bytes=b"",
            timeout=10,
        )


@pytest.mark.parametrize("tampered_index", (0, 1, 2))
def test_isolated_port_rejects_each_tampered_sealed_release_identity_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tampered_index: int,
) -> None:
    tmp_path.chmod(0o700)
    config = _fresh_config(tmp_path / "scratch")
    releases = (
        _capable_release(tmp_path / "candidate", "c"),
        _capable_release(tmp_path / "previous", "a"),
        _capable_release(tmp_path / "fallback", "f"),
    )
    port = rehearsal._IsolatedActivationPort(
        config,
        releases=releases,
        sealed_releases={release: _sealed(release) for release in releases},
    )
    monkeypatch.setattr(
        release_operator,
        "load_release_identity",
        lambda *_args, **_kwargs: releases[(tampered_index + 1) % len(releases)],
    )
    monkeypatch.setattr(
        rehearsal,
        "_load_sealed_release_copy_identity",
        lambda *_args: releases[(tampered_index + 1) % len(releases)],
    )

    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^dr_rehearsal_release_identity_changed$",
    ):
        port.verify_release(releases[tampered_index])


def _authenticated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    v2: bool = False,
) -> tuple[
    Path,
    dr_index.DurableDRGenerationIndex,
    dr_auth.AuthenticatedDRMaterial,
    Path,
]:
    home = _private(tmp_path / "friday-home")
    data = _private(home / "data")
    state = _private(data / "state")
    _private(data / "backups")
    monkeypatch.setenv("FRIDAY_HOME", str(home))
    candidate = _candidate(home)
    if v2:
        activation_core = {
            "alias_repair": {},
            "backend_accepted": True,
            "backup_receipt_sha256": candidate["database_receipt_sha256"],
            "bridge_accepted": True,
            "candidate_tree_sha256": "c" * 64,
            "database_schema_before": candidate["database_schema"],
            "engineer_backup_receipt_sha256": candidate["engineer_receipt_sha256"],
            "inbox_backup_receipt_sha256": candidate["inbox_receipt_sha256"],
            "obsidian_backup_receipt_sha256": candidate["obsidian_receipt_sha256"],
            "runtime_policy": {},
            "schema": release_operator.ACTIVATION_RECEIPT_SCHEMA,
            "status": "clear",
        }
        activation_body = {
            **activation_core,
            "operator_schema": release_operator.OPERATOR_SCHEMA,
            "receipt_sha256": hashlib.sha256(_canonical(activation_core)).hexdigest(),
        }
        candidate["source_receipt_sha256"] = activation_body["receipt_sha256"]
        backup_status = Path(candidate["backup_directory"]).stat()
        previous_record = {
            "commit": "a" * 40,
            "max_schema": 50,
            "root": str(_private(home / "releases/previous-v2")),
            "tree_manifest_sha256": "a" * 64,
            "version": "0.207.0",
            "wheel_sha256": "b" * 64,
        }
        authentication_core = {
            "activation_receipt": activation_body,
            "allowed_rollback_tree_sha256s": candidate["allowed_rollback_tree_sha256s"],
            "activation_journal_file_sha256": "1" * 64,
            "activation_journal_sha256": "2" * 64,
            "activation_receipt_file_sha256": hashlib.sha256(_canonical(activation_body) + b"\n").hexdigest(),
            "activation_receipt_sha256": activation_body["receipt_sha256"],
            "backup_directory": {
                "device": backup_status.st_dev,
                "inode": backup_status.st_ino,
                "path": candidate["backup_directory"],
            },
            "backup_manifest_sha256": "3" * 64,
            "candidate_sha256": hashlib.sha256(_canonical(candidate)).hexdigest(),
            "database_schema": candidate["database_schema"],
            "release_records": {
                "fallback": candidate["restore_release"],
                "previous": previous_record,
            },
            "restore_operator_sha256": "4" * 64,
            "schema": dr_index.AUTHENTICATION_RECEIPT_SCHEMA_V2,
            "source_transaction_id": candidate["source_transaction_id"],
            "status": "authenticated",
            "surface_receipts": {
                "database": candidate["database_receipt_sha256"],
                "engineer": candidate["engineer_receipt_sha256"],
                "inbox": candidate["inbox_receipt_sha256"],
                "obsidian": candidate["obsidian_receipt_sha256"],
            },
        }
        authentication = {
            **authentication_core,
            "receipt_sha256": hashlib.sha256(_canonical(authentication_core)).hexdigest(),
        }
    else:
        activation_body = None
        authentication = _auth_receipt(candidate)
    index = dr_index.DurableDRGenerationIndex(state)
    initial = index.initialize()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )
    activation_receipt = tmp_path / "activation.json"
    activation_receipt.write_bytes(
        (_canonical(activation_body) if activation_body is not None else b"{}") + b"\n"
    )
    material = dr_auth.AuthenticatedDRMaterial(
        authenticated=dr_auth.AuthenticatedDRCandidate(candidate, authentication),
        backup=release_operator.DatabaseBackup(
            46,
            candidate["database_receipt_sha256"],
            candidate["inbox_receipt_sha256"],
            obsidian_receipt_sha256=candidate["obsidian_receipt_sha256"],
            engineer_receipt_sha256=candidate["engineer_receipt_sha256"],
        ),
        activation_candidate=_release(tmp_path / "candidate", "c"),
        activation_previous=_release(tmp_path / "previous", "a"),
        restore_fallback=_release(tmp_path / "fallback", "f"),
    )
    return home, index, material, activation_receipt


def test_rehearsal_blocks_on_unfinished_retention_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    home, _index, _material, activation_receipt = _authenticated_home(tmp_path, monkeypatch)
    state_dir = home / "data/state"
    plan_sha256 = "a" * 64
    core = retention_apply._new_journal(  # noqa: SLF001
        {
            "plan_sha256": plan_sha256,
            "retention_scope": {
                "file_sha256": "b" * 64,
                "schema": "friday.release-artifact-retention-scope.v1",
            },
        },
        (),
        durable_plan=(
            state_dir / retention_apply.APPLY_PLAN_DIRECTORY / f"plan-{plan_sha256}.json",
            1,
            1,
        ),
        filesystem_before=(),
    )
    core["phase"] = "applying"
    retention_apply._write_journal(  # noqa: SLF001
        state_dir / retention_apply.APPLY_JOURNAL_NAME,
        core,
        guard=lambda: None,
    )
    monkeypatch.setattr(
        dr_auth,
        "_authenticate_material_locked",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("authentication must not start")),
    )

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^unfinished_retention_apply_requires_recovery$",
    ):
        rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)


def test_controller_records_only_rehearsal_and_retry_does_not_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    home, index, material, activation_receipt = _authenticated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    monkeypatch.setattr(dr_auth, "_authenticate_material_locked", lambda **_kwargs: material)
    monkeypatch.setattr(rehearsal, "_engineer_authority_present", lambda _backup: True)
    calls: list[Path] = []

    def run(_material: dr_auth.AuthenticatedDRMaterial, scratch: Path) -> rehearsal._RunResult:
        calls.append(scratch)
        return rehearsal._RunResult(
            46,
            "a" * 64,
            rehearsal._four_surface_receipt_sha256(material.backup),
            True,
        )

    monkeypatch.setattr(rehearsal, "_run_isolated_rehearsal", run)

    receipt = rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)
    first_state = index.load()
    retry = rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)

    assert retry == receipt
    assert len(calls) == 1
    assert first_state == index.load()
    assert first_state["phase"] == "rehearsed"
    assert first_state["current"] is None
    assert receipt["scratch_removed"] is True
    assert receipt["systemctl_call_count"] == 0
    assert receipt["network_call_count"] == 0
    assert receipt["production_surface_write_count"] == 0
    assert str(home) not in json.dumps(receipt)
    assert "root" not in receipt["restore_release"]
    assert not any(path.name.startswith(rehearsal._SCRATCH_PREFIX) for path in tmp_path.iterdir())


def test_controller_source_drift_after_run_never_records_rehearsal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    _home, index, material, activation_receipt = _authenticated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    changed = dr_auth.AuthenticatedDRMaterial(
        authenticated=material.authenticated,
        backup=release_operator.DatabaseBackup(47, "1" * 64, "2" * 64),
        activation_candidate=material.activation_candidate,
        activation_previous=material.activation_previous,
        restore_fallback=material.restore_fallback,
    )
    outcomes = iter((material, changed))
    monkeypatch.setattr(dr_auth, "_authenticate_material_locked", lambda **_kwargs: next(outcomes))
    monkeypatch.setattr(
        rehearsal,
        "_run_isolated_rehearsal",
        lambda *_args: rehearsal._RunResult(
            46,
            "a" * 64,
            rehearsal._four_surface_receipt_sha256(material.backup),
            False,
        ),
    )
    before = index.load()

    with pytest.raises(rehearsal.DRGenerationRehearsalError, match="^dr_rehearsal_source_changed$"):
        rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)

    assert index.load() == before
    assert not any(path.name.startswith(rehearsal._SCRATCH_PREFIX) for path in tmp_path.iterdir())


def test_controller_run_failure_cleans_scratch_and_leaves_authenticated_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    _home, index, material, activation_receipt = _authenticated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    monkeypatch.setattr(dr_auth, "_authenticate_material_locked", lambda **_kwargs: material)

    def fail(*_args: Any) -> None:
        raise RuntimeError("isolated failure")

    monkeypatch.setattr(rehearsal, "_run_isolated_rehearsal", fail)
    before = index.load()

    with pytest.raises(rehearsal.DRGenerationRehearsalError, match="^dr_rehearsal_isolated_run_failed$"):
        rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)

    assert index.load() == before
    assert not any(path.name.startswith(rehearsal._SCRATCH_PREFIX) for path in tmp_path.iterdir())


def test_controller_refuses_scratch_namespace_replacement_without_deleting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    _home, index, material, activation_receipt = _authenticated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    monkeypatch.setattr(dr_auth, "_authenticate_material_locked", lambda **_kwargs: material)
    replacement: list[Path] = []

    def swap(_material: dr_auth.AuthenticatedDRMaterial, scratch: Path) -> rehearsal._RunResult:
        scratch.rename(scratch.with_name(f"{scratch.name}.displaced"))
        scratch.mkdir(mode=0o700)
        replacement.append(scratch)
        return rehearsal._RunResult(
            46,
            "a" * 64,
            rehearsal._four_surface_receipt_sha256(material.backup),
            False,
        )

    monkeypatch.setattr(rehearsal, "_run_isolated_rehearsal", swap)
    before = index.load()

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^dr_rehearsal_scratch_cleanup_failed$",
    ):
        rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)

    assert index.load() == before
    assert replacement and replacement[0].is_dir()


def test_scratch_cleanup_requires_descriptor_safe_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    scratch = rehearsal._new_scratch(
        transaction_id="a" * 64,
        candidate_sha256="b" * 64,
    )
    supported = os.supports_dir_fd
    monkeypatch.setattr(os, "supports_dir_fd", supported - {os.unlink})

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^dr_rehearsal_safe_cleanup_unavailable$",
    ):
        rehearsal._remove_current_scratch(scratch)

    assert scratch.root.is_dir()
    monkeypatch.setattr(os, "supports_dir_fd", supported)
    rehearsal._remove_current_scratch(scratch)
    assert not scratch.root.exists()


def test_private_registry_symlink_is_refused_without_chmodding_external_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    external = tmp_path / "external"
    external.mkdir(mode=0o755)
    external.chmod(0o755)
    (tmp_path / rehearsal._SCRATCH_REGISTRY).symlink_to(external, target_is_directory=True)

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^dr_rehearsal_scratch_invalid$",
    ):
        rehearsal._new_scratch(
            transaction_id="a" * 64,
            candidate_sha256="b" * 64,
        )

    assert external.stat().st_mode & 0o777 == 0o755


def test_exact_transaction_reclaims_only_its_durable_sigkill_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    first = rehearsal._new_scratch(
        transaction_id="a" * 64,
        candidate_sha256="b" * 64,
    )
    marker = first.root / "sigkill-leftover"
    marker.write_bytes(b"bounded")
    marker.chmod(0o600)

    second = rehearsal._new_scratch(
        transaction_id="a" * 64,
        candidate_sha256="b" * 64,
    )

    assert second.identity != first.identity
    assert not (second.root / marker.name).exists()
    rehearsal._remove_current_scratch(second)


def test_scratch_nonce_prevents_identity_reuse_with_identical_stat_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    fixed_stat_identity = (7, 11, os.geteuid(), 0o700)
    monkeypatch.setattr(rehearsal, "_scratch_stat_identity", lambda _status: fixed_stat_identity)
    nonces = iter((b"a" * 32, b"b" * 32))

    def next_nonce(size: int) -> bytes:
        assert size == rehearsal._SCRATCH_IDENTITY_BYTES
        return next(nonces)

    monkeypatch.setattr(os, "urandom", next_nonce)
    first = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)
    assert rehearsal._remove_registered_tree(  # noqa: SLF001
        first.registry,
        first.root.name,
        expected_identity=first.identity,
    )

    second = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)

    assert first.identity[:4] == second.identity[:4] == fixed_stat_identity
    assert first.identity[4] == hashlib.sha256(b"a" * 32).hexdigest()
    assert second.identity[4] == hashlib.sha256(b"b" * 32).hexdigest()
    assert first.identity != second.identity
    rehearsal._remove_current_scratch(second)


def test_scratch_cleanup_refuses_bound_inode_with_changed_kernel_nonce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    scratch = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)
    original = os.getxattr(scratch.root, rehearsal._SCRATCH_IDENTITY_XATTR)
    os.setxattr(
        scratch.root,
        rehearsal._SCRATCH_IDENTITY_XATTR,
        b"replacement-identity".ljust(rehearsal._SCRATCH_IDENTITY_BYTES, b"!"),
        flags=os.XATTR_REPLACE,
    )

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^dr_rehearsal_scratch_cleanup_refused$",
    ):
        rehearsal._remove_current_scratch(scratch)

    assert scratch.root.is_dir()
    assert scratch.record.is_file()
    os.setxattr(
        scratch.root,
        rehearsal._SCRATCH_IDENTITY_XATTR,
        original,
        flags=os.XATTR_REPLACE,
    )
    rehearsal._remove_current_scratch(scratch)


def test_cleanup_never_deletes_a_quarantine_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    scratch = rehearsal._new_scratch(
        transaction_id="a" * 64,
        candidate_sha256="b" * 64,
    )
    payload = scratch.root / "payload"
    payload.write_bytes(b"private")
    payload.chmod(0o600)
    original = rehearsal._empty_pinned_scratch_directory
    replacement_marker = scratch.registry / "replacement-marker"

    def swap_after_quarantine(directory_fd: int) -> None:
        quarantine = scratch.registry / f".{scratch.root.name}.cleanup"
        displaced = scratch.registry / f".{scratch.root.name}.displaced"
        quarantine.rename(displaced)
        quarantine.mkdir(mode=0o700)
        replacement_marker.write_bytes(b"do-not-delete")
        replacement_marker.chmod(0o600)
        (quarantine / replacement_marker.name).write_bytes(replacement_marker.read_bytes())
        (quarantine / replacement_marker.name).chmod(0o600)
        original(directory_fd)

    monkeypatch.setattr(rehearsal, "_empty_pinned_scratch_directory", swap_after_quarantine)

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^dr_rehearsal_scratch_cleanup_refused$",
    ):
        rehearsal._remove_current_scratch(scratch)

    quarantine = scratch.registry / f".{scratch.root.name}.cleanup"
    assert (quarantine / replacement_marker.name).read_bytes() == b"do-not-delete"


def test_unrelated_sigkill_leftover_is_never_discovered_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    _home, index, material, activation_receipt = _authenticated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    leftover = _private(tmp_path / f"{rehearsal._SCRATCH_PREFIX}old-sigkill")
    marker = leftover / "forensic-marker"
    marker.write_bytes(b"retain")
    marker.chmod(0o600)
    monkeypatch.setattr(dr_auth, "_authenticate_material_locked", lambda **_kwargs: material)
    monkeypatch.setattr(rehearsal, "_engineer_authority_present", lambda _backup: False)
    monkeypatch.setattr(
        rehearsal,
        "_run_isolated_rehearsal",
        lambda *_args: rehearsal._RunResult(
            46,
            "a" * 64,
            rehearsal._four_surface_receipt_sha256(material.backup),
            False,
        ),
    )

    rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)

    assert index.load()["phase"] == "rehearsed"
    assert marker.read_bytes() == b"retain"


def test_receipt_publication_before_cas_is_restart_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    home, index, material, activation_receipt = _authenticated_home(
        tmp_path,
        monkeypatch,
        v2=True,
    )
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    authenticated_paths: list[Path] = []

    def authenticate_from_durable(**kwargs: Any) -> dr_auth.AuthenticatedDRMaterial:
        authenticated_paths.append(kwargs["activation_receipt"])
        return material

    monkeypatch.setattr(dr_auth, "_authenticate_material_locked", authenticate_from_durable)
    monkeypatch.setattr(rehearsal, "_engineer_authority_present", lambda _backup: False)
    run_calls: list[int] = []
    monkeypatch.setattr(
        rehearsal,
        "_run_isolated_rehearsal",
        lambda *_args: (
            run_calls.append(1)
            or rehearsal._RunResult(
                46,
                "a" * 64,
                rehearsal._four_surface_receipt_sha256(material.backup),
                False,
            )
        ),
    )
    original = dr_index.DurableDRGenerationIndex._cas_replace_locked  # noqa: SLF001
    fail_once = [True]

    def crash_after_receipt(
        self: dr_index.DurableDRGenerationIndex,
        current: Any,
        following: Any,
        pins: Any,
        namespace_guard: Any = None,
    ) -> dict[str, Any]:
        if following.get("phase") == "rehearsed" and fail_once[0]:
            fail_once[0] = False
            raise dr_index.DRGenerationIndexError("simulated_receipt_cas_crash")
        return original(
            self,
            current,
            following,
            pins,
            namespace_guard=namespace_guard,
        )

    monkeypatch.setattr(
        dr_index.DurableDRGenerationIndex,
        "_cas_replace_locked",
        crash_after_receipt,
    )

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^simulated_receipt_cas_crash$",
    ):
        rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)

    assert index.load()["phase"] == "authenticated"
    receipt_directory = home / "data/state/immutable-release-dr-generation-receipts"
    assert len(tuple(receipt_directory.glob("rehearsal-*.json"))) == 1
    activation_receipt.unlink()

    receipt = rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)

    assert receipt["status"] == "rehearsed"
    assert receipt["schema"] == dr_index.REHEARSAL_RECEIPT_SCHEMA_V2
    assert (
        receipt["exercised_release"]
        == material.authenticated.authentication_receipt["release_records"]["previous"]
    )
    assert index.load()["phase"] == "rehearsed"
    assert len(run_calls) == 2
    assert len(tuple(receipt_directory.glob("rehearsal-*.json"))) == 1
    assert authenticated_paths
    assert all(path.parent == receipt_directory for path in authenticated_paths)
    assert all(path.name.startswith("activation-") for path in authenticated_paths)


def test_pending_identity_returns_bodies_from_one_authenticated_cas_epoch(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    home = _private(tmp_path / "home")
    data = _private(home / "data")
    state = _private(data / "state")
    _private(data / "backups")
    candidate = _candidate(home)
    authentication = _auth_receipt(candidate)
    index = dr_index.DurableDRGenerationIndex(state)
    initial = index.initialize()
    prepared = index.prepare(
        intent="bootstrap_current",
        candidate=candidate,
        expected_journal_sha256=initial["journal_sha256"],
    )
    with pytest.raises(
        dr_index.DRGenerationIndexError,
        match="^dr_generation_pending_not_authenticated$",
    ):
        index.pending_generation_identity(expected_journal_sha256=prepared["journal_sha256"])
    authenticated = index.record_authenticated(
        receipt=authentication,
        expected_journal_sha256=prepared["journal_sha256"],
    )

    identity = index.pending_generation_identity(
        expected_journal_sha256=authenticated["journal_sha256"],
    )

    assert identity.index_phase == "authenticated"
    assert identity.authenticated_journal_sha256 == authenticated["journal_sha256"]
    assert identity.candidate == candidate
    assert identity.authentication_receipt == authentication
    assert identity.rehearsal_receipt is None


def test_restart_resumes_exact_quarantine_and_removes_sealed_0500_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    first = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)
    sealed = first.root / "sealed"
    nested = sealed / "release"
    nested.mkdir(parents=True, mode=0o700)
    payload = nested / "payload"
    payload.write_bytes(b"exact")
    payload.chmod(0o400)
    nested.chmod(0o500)
    sealed.chmod(0o500)
    quarantine = first.registry / f".{first.root.name}.cleanup"
    first.root.rename(quarantine)

    second = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)

    assert not quarantine.exists()
    assert second.identity != first.identity
    rehearsal._remove_current_scratch(second)


def test_restart_after_quarantine_rmdir_removes_only_bound_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    first = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)
    assert rehearsal._remove_registered_tree(  # noqa: SLF001
        first.registry,
        first.root.name,
        expected_identity=first.identity,
    )
    assert first.record.exists()

    second = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)

    assert second.identity != first.identity
    rehearsal._remove_current_scratch(second)


def test_bounded_partial_cleanup_is_restart_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    first = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)
    for name in ("a", "b"):
        payload = first.root / name
        payload.write_bytes(b"exact")
        payload.chmod(0o600)
    monkeypatch.setattr(rehearsal, "_SCRATCH_MAX_INODES", 1)

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^dr_rehearsal_scratch_cleanup_refused$",
    ):
        rehearsal._remove_current_scratch(first)

    quarantine = first.registry / f".{first.root.name}.cleanup"
    assert quarantine.is_dir()
    monkeypatch.setattr(rehearsal, "_SCRATCH_MAX_INODES", 500_000)
    second = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)
    assert not quarantine.exists()
    rehearsal._remove_current_scratch(second)


def test_scratch_cleanup_unlinks_contained_hardlinks_and_cleans_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    lease = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)
    source = lease.root / "source"
    source.write_bytes(b"exact")
    source.chmod(0o600)
    os.link(source, lease.root / "second-name")

    rehearsal._remove_current_scratch(lease)

    assert not lease.root.exists()
    assert not lease.record.exists()
    assert list(lease.registry.iterdir()) == []


@pytest.mark.parametrize("hazard", ("unsafe_file_mode", "unsafe_directory_mode", "fifo"))
def test_scratch_cleanup_hazard_retry_eventually_cleans_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hazard: str,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    first = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)
    entry = first.root / "hazard"
    if hazard == "unsafe_directory_mode":
        entry.mkdir(mode=0o770)
        entry.chmod(0o770)
    elif hazard == "unsafe_file_mode":
        entry.write_bytes(b"unsafe")
        entry.chmod(0o620)
    else:
        os.mkfifo(entry, mode=0o600)

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^dr_rehearsal_scratch_cleanup_refused$",
    ):
        rehearsal._remove_current_scratch(first)

    quarantine = first.registry / f".{first.root.name}.cleanup"
    assert quarantine.is_dir()
    assert first.record.exists()
    quarantined_entry = quarantine / entry.name
    if hazard == "fifo":
        quarantined_entry.unlink()
    else:
        quarantined_entry.chmod(0o700 if hazard == "unsafe_directory_mode" else 0o600)

    second = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)
    assert not quarantine.exists()
    rehearsal._remove_current_scratch(second)

    assert not second.record.exists()
    assert list(second.registry.iterdir()) == []


@pytest.mark.parametrize("helper", ("scratch_record", "scratch_cleanup"))
def test_rehearsal_scratch_fifo_substitution_is_bounded(
    tmp_path: Path,
    helper: str,
) -> None:
    directory = _private(tmp_path / "scratch")
    target = directory / "mutable"
    if helper == "scratch_record":
        os.mkfifo(target, mode=0o600)
    else:
        target.write_bytes(b"exact")
        target.chmod(0o600)
    child = r"""
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from tools import release_dr_generation_rehearsal as module

directory = Path(sys.argv[2])
helper = sys.argv[3]
target = directory / "mutable"
real_open = os.open
real_close = os.close
real_unlink = os.unlink
real_mkfifo = os.mkfifo
directory_fd = real_open(directory, os.O_RDONLY | os.O_DIRECTORY)
swapped = [False]

def swap_open(path, flags, *args, **kwargs):
    if path == target.name and kwargs.get("dir_fd") == directory_fd and not swapped[0]:
        swapped[0] = True
        real_unlink(target)
        real_mkfifo(target, 0o600)
    return real_open(path, flags, *args, **kwargs)

if helper == "scratch_cleanup":
    module.os.open = swap_open
try:
    try:
        if helper == "scratch_record":
            module._read_scratch_record(directory_fd, target.name)
        else:
            module._empty_pinned_scratch_directory_bounded(
                directory_fd,
                depth=0,
                counter=[0],
            )
    except module.DRGenerationRehearsalError as exc:
        expected = {
            "scratch_record": "dr_rehearsal_scratch_record_invalid",
            "scratch_cleanup": "dr_rehearsal_scratch_cleanup_refused",
        }[helper]
        if str(exc) != expected:
            raise
    else:
        raise AssertionError("FIFO substitution was accepted")
finally:
    real_close(directory_fd)
if helper == "scratch_cleanup" and not swapped[0]:
    raise AssertionError("FIFO substitution was not exercised")
"""

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            child,
            str(Path(__file__).resolve().parents[1]),
            str(directory),
            helper,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr


def test_prepared_record_binds_empty_inode_before_restart_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    first = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)
    registry_fd = os.open(first.registry, os.O_RDONLY | os.O_DIRECTORY)
    try:
        rehearsal._write_scratch_record(  # noqa: SLF001
            registry_fd,
            first.record.name,
            rehearsal._scratch_record(  # noqa: SLF001
                key=first.key,
                transaction_id=first.transaction_id,
                candidate_sha256=first.candidate_sha256,
                scratch_name=first.root.name,
                phase="prepared",
                identity=None,
            ),
        )
    finally:
        os.close(registry_fd)

    second = rehearsal._new_scratch(transaction_id="a" * 64, candidate_sha256="b" * 64)

    assert second.identity != first.identity
    rehearsal._remove_current_scratch(second)


def test_post_popen_identity_probe_failure_kills_live_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LiveProcess:
        pid = 74125
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: int) -> int:
            assert timeout == rehearsal._CHILD_KILL_GRACE_SECONDS
            return -signal.SIGKILL

    probes = [True, False]

    def identity() -> tuple[int, int, int, int]:
        if probes.pop(0):
            return (1, 2, 3, 4)
        raise release_operator.ReleaseFailure("identity_probe_failed")

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(rehearsal, "_trusted_bwrap_identity", identity)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: LiveProcess())
    monkeypatch.setattr(os, "killpg", lambda pid, sent: signals.append((pid, sent)))

    with pytest.raises(release_operator.ReleaseFailure, match="^identity_probe_failed$"):
        rehearsal._execute_bwrap(["/usr/bin/bwrap"], input_bytes=b"", timeout=1)

    assert signals == [(LiveProcess.pid, signal.SIGKILL)]


def test_live_resource_budget_failure_kills_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LiveProcess:
        pid = 74126
        returncode = None

        @staticmethod
        def poll() -> None:
            return None

        @staticmethod
        def wait(*, timeout: int) -> int:
            assert timeout == rehearsal._CHILD_KILL_GRACE_SECONDS
            return -signal.SIGKILL

    def communicate(*_args: Any, resource_check: Any, **_kwargs: Any) -> tuple[bytes, bytes]:
        resource_check()
        raise AssertionError("unreachable")

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(rehearsal, "_trusted_bwrap_identity", lambda: (1, 2, 3, 4))
    monkeypatch.setattr(rehearsal, "_bounded_communicate", communicate)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: LiveProcess())
    monkeypatch.setattr(os, "killpg", lambda pid, sent: signals.append((pid, sent)))

    def over_budget() -> None:
        raise release_operator.ReleaseFailure("dr_rehearsal_resource_budget_exceeded")

    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^dr_rehearsal_resource_budget_exceeded$",
    ):
        rehearsal._execute_bwrap(
            ["/usr/bin/bwrap"],
            input_bytes=b"",
            timeout=1,
            resource_check=over_budget,
        )

    assert signals == [(LiveProcess.pid, signal.SIGKILL)]


def test_child_rlimits_cover_memory_process_file_and_descriptor_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied: dict[int, tuple[int, int]] = {}
    monkeypatch.setattr(resource, "getrlimit", lambda _kind: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    monkeypatch.setattr(resource, "setrlimit", lambda kind, value: applied.__setitem__(kind, value))

    rehearsal._limit_rehearsal_child()

    assert applied[resource.RLIMIT_AS] == (rehearsal._CHILD_ADDRESS_SPACE_LIMIT_BYTES,) * 2
    assert applied[resource.RLIMIT_NPROC] == (rehearsal._CHILD_PROCESS_LIMIT,) * 2
    assert applied[resource.RLIMIT_FSIZE] == (rehearsal._CHILD_FILE_LIMIT_BYTES,) * 2
    assert applied[resource.RLIMIT_NOFILE] == (rehearsal._CHILD_OPEN_FILE_LIMIT,) * 2
    assert applied[resource.RLIMIT_CORE] == (0, 0)


@pytest.mark.parametrize("limit_name", ("_SCRATCH_MAX_BYTES", "_SCRATCH_MAX_INODES"))
def test_descriptor_tree_meter_rejects_storage_and_inode_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
) -> None:
    root = _private(tmp_path / "metered")
    for index in range(2):
        path = root / f"payload-{index}"
        path.write_bytes(b"bounded")
        path.chmod(0o600)
    monkeypatch.setattr(rehearsal, limit_name, 2)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(
            release_operator.ReleaseFailure,
            match="^dr_rehearsal_resource_budget_exceeded$",
        ):
            rehearsal._tree_usage_fd(descriptor)  # noqa: SLF001
    finally:
        os.close(descriptor)


def test_candidate_schema_upgrade_is_checked_against_candidate_not_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    config = _source_config(_private(tmp_path / "work"))
    checkpoint = release_operator._exact_sqlite_backup(config)  # noqa: SLF001
    candidate = release_operator.ReleaseIdentity(
        **{**vars(_capable_release(tmp_path / "candidate", "c")), "max_schema": 47}
    )
    previous = _capable_release(tmp_path / "previous", "a")
    fallback = _capable_release(tmp_path / "fallback", "f")
    releases = (candidate, previous, fallback)
    port = rehearsal._IsolatedActivationPort(
        config,
        releases=releases,
        sealed_releases={release: _sealed(release) for release in releases},
    )
    port.leases = True
    port.checkpoint = checkpoint
    monkeypatch.setattr(
        rehearsal,
        "_run_release_store",
        lambda *_args: {"main_schema": candidate.max_schema},
    )

    port.offline_migrate(candidate, checkpoint)

    assert port.database_reopen_count == 1


def test_restore_uses_authenticated_fallback_operator_not_candidate_surrogate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    config = _source_config(_private(tmp_path / "work"))
    checkpoint = release_operator._exact_sqlite_backup(config)  # noqa: SLF001
    releases = (
        _capable_release(tmp_path / "candidate", "c"),
        _capable_release(tmp_path / "previous", "a"),
        _capable_release(tmp_path / "fallback", "f"),
    )
    expected_operator = "d" * 64
    port = rehearsal._IsolatedActivationPort(
        config,
        releases=releases,
        sealed_releases={release: _sealed(release) for release in releases},
        restore_operator_sha256=expected_operator,
    )
    port.leases = True
    port.checkpoint = checkpoint
    port.checkpoint_digest = "exact-checkpoint"
    observed: list[tuple[rehearsal._SealedReleaseCopy, str]] = []

    def restore(
        sealed: rehearsal._SealedReleaseCopy,
        _config: release_operator.SystemdConfig,
        **kwargs: Any,
    ) -> dict[str, Any]:
        observed.append((sealed, kwargs["expected_operator_sha256"]))
        return {"status": "restored"}

    monkeypatch.setattr(rehearsal, "_run_exact_fallback_restore", restore)
    monkeypatch.setattr(rehearsal, "_surface_digest", lambda *_args, **_kwargs: "exact-checkpoint")

    port.restore_database(checkpoint, releases[0])

    assert observed == [(port.sealed_releases[releases[2]], expected_operator)]


def test_exact_fallback_restore_projects_only_authenticated_operator_and_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    config = _fresh_config(tmp_path / "work")
    release = _capable_release(tmp_path / "fallback-source", "f")
    copied = tmp_path / "fallback-copy"
    copied.mkdir(mode=0o500)
    copied.chmod(0o500)
    sealed = _sealed(release, copied)
    backup = release_operator.DatabaseBackup(
        46,
        "1" * 64,
        "2" * 64,
        obsidian_receipt_sha256="3" * 64,
        engineer_receipt_sha256="4" * 64,
    )
    operator_sha256 = "5" * 64
    journal = config.state_dir / "immutable-release-activation.v1.json"
    captured: dict[str, Any] = {}

    def run(
        selected: rehearsal._SealedReleaseCopy,
        _config: release_operator.SystemdConfig,
        *,
        script: str,
        arguments: tuple[str, ...],
        input_bytes: bytes,
        maximum_output: int,
    ) -> bytes:
        captured.update(
            selected=selected,
            script=script,
            arguments=arguments,
            input_bytes=input_bytes,
            maximum_output=maximum_output,
        )
        expected = {
            **json.loads(input_bytes),
            "operator_sha256": operator_sha256,
            "status": "restored",
        }
        return _canonical(expected) + b"\n"

    monkeypatch.setattr(rehearsal, "_run_release_python", run)

    receipt = rehearsal._run_exact_fallback_restore(  # noqa: SLF001
        sealed,
        config,
        activation_journal=journal,
        backup=backup,
        expected_operator_sha256=operator_sha256,
    )

    assert captured["selected"] == sealed
    assert captured["arguments"][0] == str(release.root / "artifacts/immutable_release_operator.py")
    assert captured["arguments"][1] == operator_sha256
    assert captured["arguments"][-1] == str(journal)
    assert "spec_from_file_location" in captured["script"]
    assert receipt["operator_sha256"] == operator_sha256


def test_retry_rejects_forged_alternate_four_surface_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_operator_transaction_domain: Path,
) -> None:
    _home, index, material, activation_receipt = _authenticated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    monkeypatch.setattr(dr_auth, "_authenticate_material_locked", lambda **_kwargs: material)
    monkeypatch.setattr(rehearsal, "_engineer_authority_present", lambda _backup: False)
    monkeypatch.setattr(
        rehearsal,
        "_run_isolated_rehearsal",
        lambda *_args: rehearsal._RunResult(
            46,
            "a" * 64,
            rehearsal._four_surface_receipt_sha256(material.backup),
            False,
        ),
    )
    receipt = rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)
    state = index.load()
    pending = index.pending_generation_identity(
        expected_journal_sha256=state["journal_sha256"],
    )
    forged = {**receipt, "four_surface_sha256": "f" * 64}
    forged_core = {key: value for key, value in forged.items() if key != "receipt_sha256"}
    forged["receipt_sha256"] = hashlib.sha256(_canonical(forged_core)).hexdigest()

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^dr_rehearsal_existing_receipt_invalid$",
    ):
        rehearsal._validate_existing_receipt(  # noqa: SLF001
            forged,
            pending=pending,
            material=material,
        )


@pytest.mark.parametrize("launch", ("direct", "module"))
def test_cli_failure_from_outside_repository_is_canonical_and_body_free(
    tmp_path: Path,
    launch: str,
) -> None:
    repository = Path(rehearsal.__file__).resolve().parents[1]
    tool = repository / "tools/release_dr_generation_rehearsal.py"
    activation_receipt = tmp_path / "private-activation-path.json"
    private_home = tmp_path / "private-friday-home"
    environment = dict(os.environ)
    environment["FRIDAY_HOME"] = str(private_home)
    environment.pop("PYTHONPATH", None)
    if launch == "direct":
        command = [sys.executable, str(tool)]
    else:
        environment["PYTHONPATH"] = str(repository)
        command = [sys.executable, "-m", "tools.release_dr_generation_rehearsal"]

    completed = subprocess.run(  # noqa: S603
        [*command, "--activation-receipt", str(activation_receipt)],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        timeout=10,
    )
    expected = {
        "failure_code": "dr_rehearsal_friday_home_invalid",
        "schema": rehearsal.REHEARSAL_RECEIPT_SCHEMA,
        "status": "failed_closed",
    }

    assert completed.returncode == 2
    assert completed.stdout == b""
    assert completed.stderr == _canonical(expected) + b"\n"
    assert b"Traceback" not in completed.stderr
    assert os.fsencode(activation_receipt) not in completed.stderr
    assert os.fsencode(private_home) not in completed.stderr


def test_cli_success_is_canonical_and_unexpected_failure_is_body_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    activation_receipt = tmp_path / "private-activation-path.json"
    success = {"z": 2, "a": {"status": "rehearsed"}}
    monkeypatch.setattr(
        rehearsal,
        "rehearse_authenticated_generation",
        lambda **_kwargs: success,
    )

    assert rehearsal.main(["--activation-receipt", str(activation_receipt)]) == 0
    captured = capfd.readouterr()
    assert captured.out.encode("ascii") == _canonical(success) + b"\n"
    assert captured.err == ""

    def unexpected(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(f"must-not-leak:{activation_receipt}")

    monkeypatch.setattr(rehearsal, "rehearse_authenticated_generation", unexpected)
    assert rehearsal.main(["--activation-receipt", str(activation_receipt)]) == 2
    captured = capfd.readouterr()
    expected = {
        "failure_code": "dr_rehearsal_unexpected_failure",
        "schema": rehearsal.REHEARSAL_RECEIPT_SCHEMA,
        "status": "failed_closed",
    }
    assert captured.out == ""
    assert captured.err.encode("ascii") == _canonical(expected) + b"\n"
    assert str(activation_receipt) not in captured.err
