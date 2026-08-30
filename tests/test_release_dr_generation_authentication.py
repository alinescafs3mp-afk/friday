from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools import immutable_release_operator as release_operator
from tools import release_dr_generation_authentication as dr_auth


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _write(path: Path, raw: bytes, mode: int) -> None:
    path.write_bytes(raw)
    path.chmod(mode)


def _alias_receipt() -> dict[str, Any]:
    core: dict[str, Any] = {
        "applied_count": 0,
        "backup_database_sha256": "0" * 64,
        "backup_inbox_sha256": "0" * 64,
        "backup_manifest_sha256": "0" * 64,
        "plan_sha256": "0" * 64,
        "pre_apply_database_sha256": "0" * 64,
        "schema": release_operator.ALIAS_REPAIR_RECEIPT_SCHEMA,
        "status": "not_requested",
        "writer_quiescence_sha256": "0" * 64,
    }
    return {**core, "receipt_sha256": hashlib.sha256(_canonical(core)).hexdigest()}


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    exact_surfaces: bool = True,
    change_on_recheck: bool = False,
) -> tuple[Path, Path, Path]:
    state_directory = _private_directory(tmp_path / "state")
    backup_root = _private_directory(tmp_path / "backups")
    backup_directory = _private_directory(backup_root / "immutable-cutover-test")
    release_root = _private_directory(tmp_path / "release")
    artifacts = _private_directory(release_root / "artifacts")
    manifest = b'{"schema":"test"}\n'
    _write(backup_directory / "manifest.json", manifest, 0o600)
    _write(artifacts / "immutable_release_operator.py", b"# sealed operator\n", 0o400)
    metadata = {
        "commit": "c" * 40,
        "max_schema": 50,
        "version": "0.207.84",
        "wheel_sha256": "d" * 64,
    }
    _write(artifacts / "immutable-release.json", _canonical(metadata) + b"\n", 0o400)
    fallback = release_operator.ReleaseIdentity(
        release_root,
        "c" * 40,
        "0.207.84",
        "e" * 64,
        50,
    )
    surface = SimpleNamespace(
        directory=backup_directory,
        obsidian=object() if exact_surfaces else None,
        engineer=object() if exact_surfaces else None,
    )
    backup = release_operator.DatabaseBackup(
        schema_version=46,
        receipt_sha256="1" * 64,
        inbox_receipt_sha256="2" * 64,
        obsidian_receipt_sha256="3" * 64,
        engineer_receipt_sha256="4" * 64,
        opaque=surface,
    )
    changed_backup = release_operator.DatabaseBackup(
        schema_version=46,
        receipt_sha256="9" * 64,
        inbox_receipt_sha256="2" * 64,
        obsidian_receipt_sha256="3" * 64,
        engineer_receipt_sha256="4" * 64,
        opaque=surface,
    )
    raw_backup: dict[str, Any] = {
        "directory": str(backup_directory),
        "files": [],
        "inbox_receipt_sha256": backup.inbox_receipt_sha256,
        "obsidian": {},
        "obsidian_receipt_sha256": backup.obsidian_receipt_sha256,
        "engineer": {},
        "engineer_receipt_sha256": backup.engineer_receipt_sha256,
        "receipt_sha256": backup.receipt_sha256,
        "schema_version": backup.schema_version,
    }
    if not exact_surfaces:
        raw_backup.pop("engineer")
        raw_backup.pop("engineer_receipt_sha256")
    state: dict[str, Any] = {
        "backup": raw_backup,
        "candidate": {"tree_manifest_sha256": "a" * 64},
        "phase": "clear",
        "terminal_receipt_sha256": "",
        "transaction_id": "b" * 64,
    }
    alias = _alias_receipt()
    receipt_core: dict[str, Any] = {
        "alias_repair": alias,
        "backend_accepted": True,
        "backup_receipt_sha256": backup.receipt_sha256,
        "bridge_accepted": True,
        "candidate_tree_sha256": "a" * 64,
        "database_schema_before": backup.schema_version,
        "engineer_backup_receipt_sha256": backup.engineer_receipt_sha256,
        "inbox_backup_receipt_sha256": backup.inbox_receipt_sha256,
        "obsidian_backup_receipt_sha256": backup.obsidian_receipt_sha256,
        "runtime_policy": {"memory_vault_mode": "disabled"},
        "schema": release_operator.ACTIVATION_RECEIPT_SCHEMA,
        "status": "clear",
    }
    terminal_sha256 = hashlib.sha256(_canonical(receipt_core)).hexdigest()
    state["terminal_receipt_sha256"] = terminal_sha256
    activation_receipt = tmp_path / "activation-receipt.json"
    _write(
        activation_receipt,
        _canonical(
            {
                **receipt_core,
                "operator_schema": release_operator.OPERATOR_SCHEMA,
                "receipt_sha256": terminal_sha256,
            }
        )
        + b"\n",
        0o664,
    )
    activation_journal = state_directory / "immutable-release-activation.v1.json"
    journal_sha256 = hashlib.sha256(_canonical(state)).hexdigest()
    _write(
        activation_journal,
        _canonical({**state, "journal_sha256": journal_sha256}) + b"\n",
        0o600,
    )

    class FakeJournal:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.backup_calls = 0

        def load(self) -> dict[str, Any]:
            return dict(state)

        def database_backup(
            self,
            *,
            verify_engineer_sqlite_integrity: bool = True,
        ) -> release_operator.DatabaseBackup:
            assert verify_engineer_sqlite_integrity is False
            self.backup_calls += 1
            return changed_backup if change_on_recheck and self.backup_calls > 1 else backup

        def release_identities(
            self,
        ) -> tuple[
            release_operator.ReleaseIdentity,
            release_operator.ReleaseIdentity,
            release_operator.ReleaseIdentity,
        ]:
            return fallback, fallback, fallback

    monkeypatch.setattr(release_operator, "DurableActivationJournal", FakeJournal)
    return state_directory, activation_receipt, backup_root


def test_authenticates_only_exact_four_surface_terminal_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory, activation_receipt, backup_root = _fixture(tmp_path, monkeypatch)
    before = {
        path: path.read_bytes()
        for path in (state_directory / "immutable-release-activation.v1.json", activation_receipt)
    }

    result = dr_auth._authenticate_locked(  # noqa: SLF001
        activation_journal=state_directory / "immutable-release-activation.v1.json",
        activation_receipt=activation_receipt,
        backup_root=backup_root,
    )

    assert result.candidate["source_kind"] == "terminal_activation"
    assert result.candidate["source_transaction_id"] == "b" * 64
    assert result.authentication_receipt["status"] == "authenticated"
    assert {path: path.read_bytes() for path in before} == before


def test_rejects_terminal_backup_without_all_four_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory, activation_receipt, backup_root = _fixture(
        tmp_path,
        monkeypatch,
        exact_surfaces=False,
    )
    with pytest.raises(dr_auth.DRGenerationAuthenticationError, match="^dr_backup_identity_invalid$"):
        dr_auth._authenticate_locked(  # noqa: SLF001
            activation_journal=state_directory / "immutable-release-activation.v1.json",
            activation_receipt=activation_receipt,
            backup_root=backup_root,
        )


def test_rechecks_authenticated_backup_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory, activation_receipt, backup_root = _fixture(
        tmp_path,
        monkeypatch,
        change_on_recheck=True,
    )
    with pytest.raises(dr_auth.DRGenerationAuthenticationError, match="^dr_authentication_input_changed$"):
        dr_auth._authenticate_locked(  # noqa: SLF001
            activation_journal=state_directory / "immutable-release-activation.v1.json",
            activation_receipt=activation_receipt,
            backup_root=backup_root,
        )


def test_public_authentication_uses_shared_nonblocking_operator_lock(
    tmp_path: Path,
) -> None:
    state_directory = _private_directory(tmp_path / "state")
    backup_root = _private_directory(tmp_path / "backups")
    receipt = tmp_path / "activation.json"
    lock_path = state_directory / "immutable-release-operator.v1.lock"
    with (
        release_operator.OperatorTransactionLock(lock_path),
        pytest.raises(release_operator.ReleaseFailure, match="^operator_transaction_in_progress$"),
    ):
        dr_auth.authenticate_terminal_activation_backup(
            state_directory=state_directory,
            activation_receipt=receipt,
            backup_root=backup_root,
        )


def test_large_sparse_backup_digest_is_streamed_with_bounded_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "large.sqlite3"
    with database.open("wb") as stream:
        stream.truncate(64 << 20)
    database.chmod(0o600)
    real_read = dr_auth.os.read
    requested: list[int] = []

    def bounded_read(descriptor: int, count: int) -> bytes:
        requested.append(count)
        return real_read(descriptor, count)

    monkeypatch.setattr(dr_auth.os, "read", bounded_read)
    digest, size, _status = dr_auth._stable_private_file_digest(  # noqa: SLF001
        database,
        maximum=64 << 20,
        code="test_sparse_digest_invalid",
        allow_empty=True,
    )

    assert size == 64 << 20
    assert len(digest) == 64
    assert requested
    assert max(requested) <= 1 << 20
