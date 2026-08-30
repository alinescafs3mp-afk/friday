from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tools import immutable_release_operator as release_operator
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
    _sqlite(store / "kernel.sqlite", schema=None, marker="engineer-before")
    (store / "job.bin").write_bytes(b"private-result")
    (store / "job.bin").chmod(0o600)
    key = data / "engineer-command.key"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    anchor = state / "engineer-command-store.anchor.json"
    anchor.write_text('{"test":true}\n', encoding="ascii")
    anchor.chmod(0o600)
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
        assert engineer.execute("SELECT value FROM marker").fetchone() == ("engineer-before",)
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


def _candidate(home: Path, ordinal: int = 1) -> dict[str, Any]:
    backup = _private(home / "data/backups" / f"backup-{ordinal}")
    release = _private(home / "releases" / f"{ordinal:040x}")
    digest = lambda offset: f"{ordinal * 16 + offset:064x}"  # noqa: E731
    return {
        "backup_directory": str(backup),
        "backup_record_sha256": digest(1),
        "database_receipt_sha256": digest(2),
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


def _auth_receipt(ordinal: int = 1) -> dict[str, Any]:
    digest = lambda offset: f"{ordinal * 32 + offset:064x}"  # noqa: E731
    core: dict[str, Any] = {
        "activation_journal_file_sha256": digest(1),
        "activation_journal_sha256": digest(2),
        "activation_receipt_file_sha256": digest(3),
        "activation_receipt_sha256": digest(4),
        "backup_manifest_sha256": digest(5),
        "restore_operator_sha256": digest(6),
        "schema": dr_auth.AUTHENTICATION_RECEIPT_SCHEMA,
        "status": "authenticated",
        "surface_receipts": {
            "database": digest(7),
            "engineer": digest(8),
            "inbox": digest(9),
            "obsidian": digest(10),
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
    monkeypatch.setattr(
        release_operator,
        "load_release_identity",
        lambda root, **_kwargs: by_root[root],
    )
    monkeypatch.setattr(
        rehearsal,
        "_run_release_store",
        lambda release, _config: {
            "fk": 0,
            "integrity": "ok",
            "schema": release.max_schema,
            "version": release.version,
        },
    )
    material = dr_auth.AuthenticatedDRMaterial(
        authenticated=dr_auth.AuthenticatedDRCandidate(_candidate(source.friday_home), _auth_receipt()),
        backup=backup,
        activation_candidate=releases[0],
        activation_previous=releases[1],
        restore_fallback=releases[2],
    )
    scratch = _private(tmp_path / "isolated")

    result = rehearsal._run_isolated_rehearsal(material, scratch)

    assert result.schema_version == 46
    assert result.rollback_tree_sha256 == releases[1].tree_manifest_sha256
    assert len(result.four_surface_sha256) == 64
    assert (scratch / "data/friday.sqlite3").exists()


def test_exact_release_store_open_uses_closed_bounded_interpreter_invocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    config = _fresh_config(tmp_path / "scratch")
    release = _capable_release(tmp_path / "sealed-release", "c")
    expected = {
        "fk": 0,
        "integrity": "ok",
        "schema": release.max_schema,
        "version": release.version,
    }
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_canonical(expected) + b"\n",
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("MUST_NOT_REACH_REHEARSAL_CHILD", "secret")

    assert rehearsal._run_release_store(release, config) == expected
    command, kwargs = calls[0]
    assert command[:4] == [str(release.root / "venv/bin/python"), "-I", "-B", "-c"]
    assert kwargs["timeout"] == 600
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert "MUST_NOT_REACH_REHEARSAL_CHILD" not in kwargs["env"]
    assert kwargs["env"]["FRIDAY_HOME"] == str(config.friday_home)


@pytest.mark.parametrize("failure", ("stderr", "timeout", "wrong_receipt"))
def test_exact_release_store_open_fails_closed_without_stderr_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    tmp_path.chmod(0o700)
    config = _fresh_config(tmp_path / "scratch")
    release = _capable_release(tmp_path / "sealed-release", "c")

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 600, stderr=b"private-timeout-body")
        receipt = {
            "fk": 0,
            "integrity": "ok",
            "schema": release.max_schema,
            "version": "tampered" if failure == "wrong_receipt" else release.version,
        }
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=_canonical(receipt) + b"\n",
            stderr=b"private-stderr-body" if failure == "stderr" else b"",
        )

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(release_operator.ReleaseFailure) as raised:
        rehearsal._run_release_store(release, config)

    assert "private" not in str(raised.value)


def test_isolated_port_rejects_tampered_sealed_release_identity_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    config = _fresh_config(tmp_path / "scratch")
    releases = (
        _capable_release(tmp_path / "candidate", "c"),
        _capable_release(tmp_path / "previous", "a"),
        _capable_release(tmp_path / "fallback", "f"),
    )
    port = rehearsal._IsolatedActivationPort(config, releases=releases)
    monkeypatch.setattr(
        release_operator,
        "load_release_identity",
        lambda *_args, **_kwargs: releases[1],
    )

    with pytest.raises(
        release_operator.ReleaseFailure,
        match="^dr_rehearsal_release_identity_changed$",
    ):
        port.verify_release(releases[0])


def _authenticated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    Path,
    dr_index.DurableDRGenerationIndex,
    dr_auth.AuthenticatedDRMaterial,
    Path,
]:
    home = _private(tmp_path / "friday-home")
    state = _private(home / "data/state")
    _private(home / "data/backups")
    monkeypatch.setenv("FRIDAY_HOME", str(home))
    candidate = _candidate(home)
    authentication = _auth_receipt()
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
    activation_receipt.write_text("{}\n", encoding="ascii")
    material = dr_auth.AuthenticatedDRMaterial(
        authenticated=dr_auth.AuthenticatedDRCandidate(candidate, authentication),
        backup=release_operator.DatabaseBackup(46, "1" * 64, "2" * 64),
        activation_candidate=_release(tmp_path / "candidate", "c"),
        activation_previous=_release(tmp_path / "previous", "a"),
        restore_fallback=_release(tmp_path / "fallback", "f"),
    )
    return home, index, material, activation_receipt


def test_controller_records_only_rehearsal_and_retry_does_not_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, index, material, activation_receipt = _authenticated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    monkeypatch.setattr(dr_auth, "_authenticate_material_locked", lambda **_kwargs: material)
    monkeypatch.setattr(rehearsal, "_engineer_authority_present", lambda _backup: True)
    calls: list[Path] = []

    def run(_material: dr_auth.AuthenticatedDRMaterial, scratch: Path) -> rehearsal._RunResult:
        calls.append(scratch)
        return rehearsal._RunResult(46, "a" * 64, "9" * 64, True)

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
        lambda *_args: rehearsal._RunResult(46, "a" * 64, "9" * 64, False),
    )
    before = index.load()

    with pytest.raises(rehearsal.DRGenerationRehearsalError, match="^dr_rehearsal_source_changed$"):
        rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)

    assert index.load() == before
    assert not any(path.name.startswith(rehearsal._SCRATCH_PREFIX) for path in tmp_path.iterdir())


def test_controller_run_failure_cleans_scratch_and_leaves_authenticated_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
) -> None:
    _home, index, material, activation_receipt = _authenticated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    monkeypatch.setattr(dr_auth, "_authenticate_material_locked", lambda **_kwargs: material)
    replacement: list[Path] = []

    def swap(_material: dr_auth.AuthenticatedDRMaterial, scratch: Path) -> rehearsal._RunResult:
        scratch.rename(scratch.with_name(f"{scratch.name}.displaced"))
        scratch.mkdir(mode=0o700)
        replacement.append(scratch)
        return rehearsal._RunResult(46, "a" * 64, "9" * 64, False)

    monkeypatch.setattr(rehearsal, "_run_isolated_rehearsal", swap)
    before = index.load()

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^dr_rehearsal_scratch_cleanup_failed$",
    ):
        rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)

    assert index.load() == before
    assert replacement and replacement[0].is_dir()


def test_scratch_cleanup_requires_descriptor_safe_rmtree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    scratch = rehearsal._new_scratch()
    identity = rehearsal._scratch_identity(scratch)
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)

    with pytest.raises(
        rehearsal.DRGenerationRehearsalError,
        match="^dr_rehearsal_safe_cleanup_unavailable$",
    ):
        rehearsal._remove_current_scratch(scratch, expected_identity=identity)

    assert scratch.is_dir()
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", True)
    rehearsal._remove_current_scratch(scratch, expected_identity=identity)
    assert not scratch.exists()


def test_unrelated_sigkill_leftover_is_never_discovered_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
        lambda *_args: rehearsal._RunResult(46, "a" * 64, "9" * 64, False),
    )

    rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)

    assert index.load()["phase"] == "rehearsed"
    assert marker.read_bytes() == b"retain"


def test_receipt_publication_before_cas_is_restart_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home, index, material, activation_receipt = _authenticated_home(tmp_path, monkeypatch)
    monkeypatch.setattr(rehearsal, "_SCRATCH_PARENT", tmp_path)
    monkeypatch.setattr(dr_auth, "_authenticate_material_locked", lambda **_kwargs: material)
    monkeypatch.setattr(rehearsal, "_engineer_authority_present", lambda _backup: False)
    run_calls: list[int] = []
    monkeypatch.setattr(
        rehearsal,
        "_run_isolated_rehearsal",
        lambda *_args: run_calls.append(1) or rehearsal._RunResult(46, "a" * 64, "9" * 64, False),
    )
    original = dr_index.DurableDRGenerationIndex._cas_replace_locked  # noqa: SLF001
    fail_once = [True]

    def crash_after_receipt(
        self: dr_index.DurableDRGenerationIndex,
        current: Any,
        following: Any,
        pins: Any,
    ) -> dict[str, Any]:
        if following.get("phase") == "rehearsed" and fail_once[0]:
            fail_once[0] = False
            raise dr_index.DRGenerationIndexError("simulated_receipt_cas_crash")
        return original(self, current, following, pins)

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

    receipt = rehearsal.rehearse_authenticated_generation(activation_receipt=activation_receipt)

    assert receipt["status"] == "rehearsed"
    assert index.load()["phase"] == "rehearsed"
    assert len(run_calls) == 2
    assert len(tuple(receipt_directory.glob("rehearsal-*.json"))) == 1


def test_pending_identity_returns_bodies_from_one_authenticated_cas_epoch(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    home = _private(tmp_path / "home")
    state = _private(home / "data/state")
    _private(home / "data/backups")
    candidate = _candidate(home)
    authentication = _auth_receipt()
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
