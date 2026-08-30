#!/usr/bin/env python3
"""Rehearse one authenticated DR generation on an isolated production copy.

The controller owns no publication or deletion authority.  It may only move an
already-authenticated pending generation to ``rehearsed`` after the exact four
surfaces survive a bounded activation fault and rollback in an owner-private
``/var/tmp`` contour.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools import immutable_release_operator as release_operator
from tools import release_dr_generation_authentication as dr_auth
from tools import release_dr_generation_index as dr_index

REHEARSAL_RECEIPT_SCHEMA = "friday.immutable-release-dr-rehearsal-receipt.v1"
_SCRATCH_PARENT = Path("/var/tmp")
_SCRATCH_PREFIX = "friday-dr-rehearsal-"
_HEX64 = frozenset("0123456789abcdef")
_CHECKS = (
    "authenticated_pending_bound",
    "activation_source_reauthenticated",
    "retained_release_identities_verified",
    "database_materialized",
    "inbox_materialized",
    "obsidian_materialized_exactly",
    "engineer_materialized_with_fresh_identity",
    "scratch_checkpoint_authenticated",
    "fault_after_migration_before_provision",
    "rollback_restore_attempted",
    "activation_rolled_back",
    "four_surface_restore_exact",
    "database_reopened_twice",
    "inbox_reopened_twice",
    "zero_systemctl_or_network_calls",
    "scratch_removed_before_admission",
    "source_unchanged_before_cas",
)
_RECEIPT_KEYS = frozenset(
    {
        "authentication_receipt_sha256",
        "candidate_sha256",
        "check_count",
        "checkset_sha256",
        "database_foreign_keys_clear",
        "database_integrity_clear",
        "database_reopen_count",
        "database_schema",
        "engineer_authority_present",
        "engineer_exact",
        "fault_boundary",
        "four_surface_exact",
        "four_surface_sha256",
        "index_journal_sha256",
        "index_revision",
        "index_transaction_id",
        "inbox_foreign_keys_clear",
        "inbox_integrity_clear",
        "inbox_reopen_count",
        "network_call_count",
        "obsidian_exact",
        "production_surface_write_count",
        "receipt_sha256",
        "restore_release",
        "rollback_restore_observed",
        "rollback_tree_sha256",
        "rolled_back",
        "schema",
        "scratch_removed",
        "source",
        "status",
        "systemctl_call_count",
    }
)


class DRGenerationRehearsalError(RuntimeError):
    """A closed rehearsal failure with a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _InjectedRehearsalFault(RuntimeError):
    pass


@dataclass(frozen=True)
class _RunResult:
    schema_version: int
    rollback_tree_sha256: str
    four_surface_sha256: str
    engineer_authority_present: bool


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_noncanonical") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


def _canonical_friday_home() -> Path:
    raw = os.environ.get("FRIDAY_HOME")
    if not raw or any(character in raw for character in "\x00\r\n"):
        raise DRGenerationRehearsalError("dr_rehearsal_friday_home_invalid")
    home = Path(raw)
    lexical = Path(os.path.abspath(home))
    try:
        status = os.lstat(home)
        resolved = home.resolve(strict=True)
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_friday_home_invalid") from exc
    if (
        not home.is_absolute()
        or home != lexical
        or resolved != home
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_friday_home_invalid")
    return home


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    status = os.lstat(path)
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o700
        or path.resolve(strict=True) != path
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_invalid")
    return path


def _new_scratch() -> Path:
    try:
        parent_status = os.lstat(_SCRATCH_PARENT)
        if not stat.S_ISDIR(parent_status.st_mode) or stat.S_ISLNK(parent_status.st_mode):
            raise DRGenerationRehearsalError("dr_rehearsal_scratch_parent_invalid")
        scratch = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=_SCRATCH_PARENT))
        os.chmod(scratch, 0o700)
        return _private_directory(scratch)
    except DRGenerationRehearsalError:
        raise
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_unavailable") from exc


def _remove_current_scratch(
    path: Path,
    *,
    expected_identity: tuple[int, int, int, int],
) -> None:
    lexical = Path(os.path.abspath(path))
    try:
        status = os.lstat(path)
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_failed") from exc
    if (
        path != lexical
        or path.parent != _SCRATCH_PARENT
        or not path.name.startswith(_SCRATCH_PREFIX)
        or not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o700
        or (
            int(status.st_dev),
            int(status.st_ino),
            int(status.st_uid),
            stat.S_IMODE(status.st_mode),
        )
        != expected_identity
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_refused")
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise DRGenerationRehearsalError("dr_rehearsal_safe_cleanup_unavailable")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_failed") from exc
    if path.exists() or path.is_symlink():
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_failed")


def _scratch_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        status = os.lstat(path)
    except OSError as exc:
        raise DRGenerationRehearsalError("dr_rehearsal_scratch_invalid") from exc
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_uid),
        stat.S_IMODE(status.st_mode),
    )


def _scratch_config(scratch: Path, *, obsidian_present: bool) -> release_operator.SystemdConfig:
    data = _private_directory(scratch / "data")
    state = _private_directory(data / "state")
    backup = _private_directory(data / "backups")
    unit_dir = _private_directory(scratch / "units")
    cache = _private_directory(scratch / "cache")
    logs = _private_directory(scratch / "logs")
    env_file = scratch / "friday.env"
    health_ca = scratch / "health-ca.pem"
    environment_values = {
        "FRIDAY_CACHE_DIR": str(cache),
        "FRIDAY_DATABASE_MUST_EXIST": "1",
        "FRIDAY_DATABASE_PATH": str(data / "friday.sqlite3"),
        "FRIDAY_DATA_DIR": str(data),
        "FRIDAY_ENGINEER_COMMAND_ENABLED": "0",
        "FRIDAY_ENGINEER_MODE_ENABLED": "0",
        "FRIDAY_HOME": str(scratch),
        "FRIDAY_LOG_DIR": str(logs),
        "FRIDAY_MEMORY_VAULT_MODE": "disabled",
        "FRIDAY_OBSIDIAN_ENABLED": "1" if obsidian_present else "0",
        "FRIDAY_OBSIDIAN_ROOT": str(data / "obsidian"),
        "FRIDAY_PROFILE": "qwen36-27b-nvfp4-nvidia",
        "FRIDAY_SECONDARY_LLM_ENABLED": "0",
        "FRIDAY_SEMANTIC_SUPERVISOR_MODE": "off",
        "FRIDAY_STATE_DIR": str(state),
    }
    env_file.write_text(
        "".join(f"{key}={value}\n" for key, value in environment_values.items()),
        encoding="ascii",
    )
    health_ca.write_bytes(b"isolated-dr-rehearsal\n")
    env_file.chmod(0o600)
    health_ca.chmod(0o600)
    return release_operator.SystemdConfig(
        anchor=scratch / "active",
        env_file=env_file,
        env_file_sha256=_sha256(env_file.read_bytes()),
        friday_home=scratch,
        unit_dir=unit_dir,
        database=data / "friday.sqlite3",
        inbox_database=state / "telegram-inbox.sqlite3",
        backup_dir=backup,
        state_dir=state,
        health_ca=health_ca,
        health_ca_sha256=_sha256(health_ca.read_bytes()),
        obsidian_mode="enabled" if obsidian_present else "disabled",
        obsidian_root=data / "obsidian",
    )


def _release_store_environment(config: release_operator.SystemdConfig) -> dict[str, str]:
    return {
        "FRIDAY_DATABASE_MUST_EXIST": "1",
        "FRIDAY_DATABASE_PATH": str(config.database),
        "FRIDAY_ENV_FILE": str(config.env_file),
        "FRIDAY_HOME": str(config.friday_home),
        "HOME": str(config.friday_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _run_release_store(
    release: release_operator.ReleaseIdentity,
    config: release_operator.SystemdConfig,
) -> dict[str, Any]:
    """Open/migrate scratch state with the exact sealed release interpreter."""

    script = r"""
import json,logging,os,pathlib,sys
logging.disable(logging.CRITICAL)
os.environ["FRIDAY_ENV_FILE"]=sys.argv[1]
from friday import __version__
from friday.config import load_local_env_file,load_settings
from friday.storage import FridayStorage,SCHEMA_VERSION
load_local_env_file(); settings=load_settings()
assert settings.database_path.resolve(strict=True)==pathlib.Path(sys.argv[2]).resolve(strict=True)
assert settings.database_must_exist is True
assert settings.home.resolve(strict=True)==pathlib.Path(sys.argv[3]).resolve(strict=True)
assert SCHEMA_VERSION==int(sys.argv[4])
store=FridayStorage(settings)
try:
 row=store.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
 assert row is not None
 out={"fk":len(store.conn.execute("PRAGMA foreign_key_check").fetchall()),"integrity":store.conn.execute("PRAGMA integrity_check").fetchone()[0],"schema":int(row[0]),"version":__version__}
finally: store.close(final=True)
print(json.dumps(out,sort_keys=True,separators=(",",":")))
"""
    try:
        completed = subprocess.run(  # noqa: S603
            [
                str(release.root / "venv/bin/python"),
                "-I",
                "-B",
                "-c",
                script,
                str(config.env_file),
                str(config.database),
                str(config.friday_home),
                str(release.max_schema),
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=_release_store_environment(config),
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise release_operator.ReleaseFailure("dr_rehearsal_release_open_timeout") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise release_operator.ReleaseFailure("dr_rehearsal_release_open_failed") from exc
    if (
        completed.returncode != 0
        or completed.stderr
        or not 0 < len(completed.stdout) <= 4_096
        or not completed.stdout.endswith(b"\n")
    ):
        raise release_operator.ReleaseFailure("dr_rehearsal_release_open_failed")

    def unique_pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise release_operator.ReleaseFailure("dr_rehearsal_release_receipt_invalid")
            result[key] = value
        return result

    try:
        receipt = json.loads(
            completed.stdout.decode("ascii"),
            object_pairs_hook=unique_pairs,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise release_operator.ReleaseFailure("dr_rehearsal_release_receipt_invalid") from exc
    expected = {
        "fk": 0,
        "integrity": "ok",
        "schema": release.max_schema,
        "version": release.version,
    }
    if (
        not isinstance(receipt, dict)
        or receipt != expected
        or completed.stdout != _canonical(expected) + b"\n"
    ):
        raise release_operator.ReleaseFailure("dr_rehearsal_release_receipt_invalid")
    return receipt


def _surface_digest(
    config: release_operator.SystemdConfig,
    *,
    include_sqlite: bool = True,
) -> str:
    sqlite_files: list[dict[str, Any]] = []
    for label, database in (("database", config.database), ("inbox", config.inbox_database)):
        for suffix in ("", "-wal"):
            path = Path(f"{database}{suffix}")
            if not path.exists():
                continue
            status = os.stat(path, follow_symlinks=False)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_uid != os.geteuid()
                or status.st_nlink != 1
                or stat.S_IMODE(status.st_mode) & 0o077
            ):
                raise DRGenerationRehearsalError("dr_rehearsal_surface_invalid")
            sqlite_files.append(
                {
                    "name": f"{label}.sqlite3{suffix}",
                    "sha256": release_operator._sha256_file(path),  # noqa: SLF001
                    "size": int(status.st_size),
                }
            )
    obsidian, _identities = release_operator._capture_obsidian_tree(  # noqa: SLF001
        release_operator._obsidian_root(config),  # noqa: SLF001
        destination=None,
    )
    engineer = release_operator._scan_engineer_artifacts(config, destination=None)  # noqa: SLF001
    # The fresh-copy API intentionally assigns a new Engineer database inode.
    # Device/inode values therefore prove local restore continuity in memory,
    # but must never make the durable rehearsal receipt nondeterministic.
    engineer = dict(engineer)
    engineer["entries"] = [
        {
            key: value
            for key, value in item.items()
            if not (item.get("path") == "store/kernel.sqlite" and key in {"device", "inode"})
        }
        for item in engineer["entries"]
    ]
    projection: dict[str, Any] = {
        "engineer": engineer,
        "obsidian": obsidian,
    }
    if include_sqlite:
        projection["sqlite"] = sqlite_files
    return _sha256(_canonical(projection))


def _engineer_authority_present(backup: release_operator.DatabaseBackup) -> bool:
    payload = backup.opaque
    if not isinstance(payload, release_operator._ExactBackupPayload):  # noqa: SLF001
        raise DRGenerationRehearsalError("dr_rehearsal_backup_identity_invalid")
    descriptor = payload.engineer
    if descriptor is None:
        raise DRGenerationRehearsalError("dr_rehearsal_four_surface_required")
    manifest = release_operator._verify_engineer_backup(  # noqa: SLF001
        payload.directory,
        descriptor,
    )
    return isinstance(manifest.get("engineer_command_ledger_authority"), dict)


class _TrackingJournal:
    def __init__(self, journal: release_operator.DurableActivationJournal) -> None:
        self.journal = journal
        self.phases: list[str] = []

    def begin(
        self,
        *,
        candidate: release_operator.ReleaseIdentity,
        previous: release_operator.ReleaseIdentity,
        fallback: release_operator.ReleaseIdentity,
    ) -> None:
        self.journal.begin(candidate=candidate, previous=previous, fallback=fallback)
        self.phases.append("prepared")

    def record(self, phase: str, **kwargs: Any) -> None:
        self.journal.record(phase, **kwargs)
        self.phases.append(phase)

    def load(self) -> Mapping[str, Any]:
        return self.journal.load()

    def release_identities(
        self,
    ) -> tuple[
        release_operator.ReleaseIdentity,
        release_operator.ReleaseIdentity,
        release_operator.ReleaseIdentity,
    ]:
        return self.journal.release_identities()

    def database_backup(self) -> release_operator.DatabaseBackup | None:
        return self.journal.database_backup()


class _IsolatedActivationPort:
    """Activation port that can touch only its private scratch contour."""

    def __init__(
        self,
        config: release_operator.SystemdConfig,
        *,
        releases: Sequence[release_operator.ReleaseIdentity],
    ) -> None:
        self.config = config
        self.releases = tuple(releases)
        self.leases = False
        self.checkpoint: release_operator.DatabaseBackup | None = None
        self.checkpoint_digest = ""
        self.checkpoint_non_sqlite_digest = ""
        self.restored_digest = ""
        self.rollback_tree_sha256 = ""
        self.release_open_trees: list[str] = []
        self.rollback_release_receipt: dict[str, Any] | None = None
        self.systemctl_calls = 0
        self.network_calls = 0
        self.production_write_calls = 0

    def activation_policy_receipt(self) -> Mapping[str, str]:
        raise release_operator.ReleaseFailure("dr_rehearsal_fault_not_reached")

    def verify_release(
        self,
        release: release_operator.ReleaseIdentity,
        *,
        use_predecessor_config: bool = False,
    ) -> None:
        del use_predecessor_config
        if release not in self.releases:
            raise release_operator.ReleaseFailure("dr_rehearsal_release_identity_invalid")
        observed = release_operator.load_release_identity(
            release.root,
            expected_tree_sha256=release.tree_manifest_sha256,
        )
        if observed != release:
            raise release_operator.ReleaseFailure("dr_rehearsal_release_identity_changed")

    def verify_units(self, candidate: release_operator.ReleaseIdentity) -> None:
        if candidate != self.releases[0]:
            raise release_operator.ReleaseFailure("dr_rehearsal_candidate_changed")

    def verify_active_anchor(
        self,
        previous: release_operator.ReleaseIdentity,
        candidate: release_operator.ReleaseIdentity,
    ) -> None:
        if previous != self.releases[1] or candidate != self.releases[0]:
            raise release_operator.ReleaseFailure("dr_rehearsal_anchor_identity_changed")

    def stop_bridge(self) -> None:
        return

    def stop_backend(self) -> None:
        return

    def services_inactive(self) -> bool:
        return True

    def writer_leases_held(self) -> bool:
        return self.leases

    def acquire_writer_leases(self) -> None:
        if self.leases:
            return
        self.leases = True

    def release_writer_leases(self) -> None:
        self.leases = False

    def validate_staged_config_transition(self, *_args: Any) -> None:
        raise release_operator.ReleaseFailure("dr_rehearsal_staged_transition_forbidden")

    def activate_staged_config_transition(self, *_args: Any) -> None:
        raise release_operator.ReleaseFailure("dr_rehearsal_staged_transition_forbidden")

    def select_predecessor_config_transition(self, *_args: Any) -> None:
        raise release_operator.ReleaseFailure("dr_rehearsal_staged_transition_forbidden")

    def validate_engineer_recovery_contour(
        self,
        releases: Sequence[release_operator.ReleaseIdentity],
    ) -> None:
        if tuple(releases) != self.releases or any(
            release.root == self.config.friday_home or release.root.is_relative_to(self.config.friday_home)
            for release in releases
        ):
            raise release_operator.ReleaseFailure("dr_rehearsal_release_contour_invalid")

    def engineer_store_lifecycle_required(self) -> bool:
        manifest = release_operator._scan_engineer_artifacts(  # noqa: SLF001
            self.config,
            destination=None,
        )
        return bool(manifest["store_present"] or manifest["key_present"])

    def engineer_store_lifecycle_provisioned(self) -> bool:
        _store, _key, state = release_operator._engineer_artifact_paths(self.config)  # noqa: SLF001
        return (state / "engineer-command-store.anchor.json").exists()

    def provision_engineer_store(self, release: release_operator.ReleaseIdentity) -> None:
        del release
        raise release_operator.ReleaseFailure("dr_rehearsal_provision_boundary_crossed")

    def backup_database(
        self,
        release: release_operator.ReleaseIdentity,
    ) -> release_operator.DatabaseBackup:
        if release != self.releases[0] or not self.leases:
            raise release_operator.ReleaseFailure("dr_rehearsal_checkpoint_not_authorized")
        checkpoint = release_operator._exact_sqlite_backup(self.config)  # noqa: SLF001
        self.checkpoint = checkpoint
        self.checkpoint_digest = _surface_digest(self.config)
        self.checkpoint_non_sqlite_digest = _surface_digest(
            self.config,
            include_sqlite=False,
        )
        return checkpoint

    @staticmethod
    def _mutate_sqlite(path: Path, table: str) -> None:
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.execute(f"CREATE TABLE {table}(value INTEGER NOT NULL)")
            connection.execute(f"INSERT INTO {table}(value) VALUES (1)")
        finally:
            connection.close()

    def offline_migrate(
        self,
        release: release_operator.ReleaseIdentity,
        backup: release_operator.DatabaseBackup,
    ) -> None:
        if release != self.releases[0] or backup != self.checkpoint or not self.leases:
            raise release_operator.ReleaseFailure("dr_rehearsal_migration_not_authorized")
        _run_release_store(release, self.config)
        self.release_open_trees.append(release.tree_manifest_sha256)
        self._mutate_sqlite(self.config.database, "friday_dr_rehearsal_main_fault")
        self._mutate_sqlite(self.config.inbox_database, "friday_dr_rehearsal_inbox_fault")
        obsidian = release_operator._obsidian_root(self.config)  # noqa: SLF001
        obsidian.mkdir(mode=0o700, exist_ok=True)
        (obsidian / ".friday-dr-rehearsal-fault").write_bytes(b"fault\n")
        (obsidian / ".friday-dr-rehearsal-fault").chmod(0o600)
        store, _key, _state = release_operator._engineer_artifact_paths(self.config)  # noqa: SLF001
        store.mkdir(parents=True, mode=0o700, exist_ok=True)
        store.chmod(0o700)
        (store / ".friday-dr-rehearsal-fault").write_bytes(b"fault\n")
        (store / ".friday-dr-rehearsal-fault").chmod(0o600)

    def repair_file_aliases(
        self,
        release: release_operator.ReleaseIdentity,
        backup: release_operator.DatabaseBackup,
    ) -> Mapping[str, Any]:
        if release != self.releases[0] or backup != self.checkpoint:
            raise release_operator.ReleaseFailure("dr_rehearsal_fault_boundary_changed")
        raise _InjectedRehearsalFault("after_migration_before_provision")

    def switch_anchor(self, release: release_operator.ReleaseIdentity) -> None:
        if release not in self.releases:
            raise release_operator.ReleaseFailure("dr_rehearsal_rollback_release_changed")
        self.rollback_tree_sha256 = release.tree_manifest_sha256

    def start_backend(self, release: release_operator.ReleaseIdentity) -> None:
        if release not in self.releases:
            raise release_operator.ReleaseFailure("dr_rehearsal_rollback_release_changed")

    def accept_backend(self, release: release_operator.ReleaseIdentity) -> None:
        if release not in self.releases or release.tree_manifest_sha256 != self.rollback_tree_sha256:
            raise release_operator.ReleaseFailure("dr_rehearsal_rollback_release_changed")
        self.rollback_release_receipt = _run_release_store(release, self.config)
        self.release_open_trees.append(release.tree_manifest_sha256)

    def start_bridge(self, release: release_operator.ReleaseIdentity) -> None:
        if release not in self.releases:
            raise release_operator.ReleaseFailure("dr_rehearsal_rollback_release_changed")

    def accept_bridge(self, release: release_operator.ReleaseIdentity) -> None:
        if release not in self.releases:
            raise release_operator.ReleaseFailure("dr_rehearsal_rollback_release_changed")

    def restore_database(
        self,
        backup: release_operator.DatabaseBackup,
        release: release_operator.ReleaseIdentity,
    ) -> None:
        if backup != self.checkpoint or release != self.releases[0] or not self.leases:
            raise release_operator.ReleaseFailure("dr_rehearsal_restore_not_authorized")
        release_operator._restore_exact_sqlite_backup(self.config, backup)  # noqa: SLF001
        self.restored_digest = _surface_digest(self.config)
        if self.restored_digest != self.checkpoint_digest:
            raise release_operator.ReleaseFailure("dr_rehearsal_four_surface_restore_mismatch")


def _run_isolated_rehearsal(
    material: dr_auth.AuthenticatedDRMaterial,
    scratch: Path,
) -> _RunResult:
    payload = material.backup.opaque
    if not isinstance(payload, release_operator._ExactBackupPayload):  # noqa: SLF001
        raise DRGenerationRehearsalError("dr_rehearsal_backup_identity_invalid")
    obsidian = payload.obsidian
    if obsidian is None or payload.engineer is None:
        raise DRGenerationRehearsalError("dr_rehearsal_four_surface_required")
    config = _scratch_config(scratch, obsidian_present=obsidian.present)
    materialized = release_operator.materialize_exact_backup_into_fresh_contour(
        config,
        material.backup,
    )
    releases = (
        material.activation_candidate,
        material.activation_previous,
        material.restore_fallback,
    )
    port = _IsolatedActivationPort(config, releases=releases)
    journal = _TrackingJournal(
        release_operator.DurableActivationJournal(
            config.state_dir / "immutable-release-activation.v1.json",
            backup_root=config.backup_dir,
            config_identity_sha256=_sha256(b"isolated-dr-rehearsal-config-v1"),
            memory_vault_mode="disabled",
            obsidian_mode=config.obsidian_mode,
            obsidian_root_sha256=_sha256(str(config.obsidian_root).encode("utf-8")),
        )
    )
    try:
        release_operator.activate_release(
            port,
            journal,
            candidate=material.activation_candidate,
            previous=material.activation_previous,
            schema_capable_fallback=material.restore_fallback,
        )
    except release_operator.ReleaseFailure as exc:
        if str(exc) != "activation_failed_rolled_back" or not isinstance(
            exc.__cause__,
            _InjectedRehearsalFault,
        ):
            raise DRGenerationRehearsalError("dr_rehearsal_activation_failed") from exc
    else:  # pragma: no cover - the injected boundary is unconditional.
        raise DRGenerationRehearsalError("dr_rehearsal_fault_not_observed")
    state = dict(journal.load())
    if (
        state.get("phase") != "rolled_back"
        or "rollback_restore_attempted" not in journal.phases
        or not journal.phases
        or journal.phases[-1] != "rolled_back"
        or port.checkpoint is None
        or not port.checkpoint_digest
        or port.restored_digest != port.checkpoint_digest
        or not port.rollback_tree_sha256
        or port.systemctl_calls != 0
        or port.network_calls != 0
        or port.production_write_calls != 0
        or port.release_open_trees
        != [
            material.activation_candidate.tree_manifest_sha256,
            port.rollback_tree_sha256,
        ]
        or port.rollback_release_receipt is None
        or port.rollback_release_receipt.get("schema") != materialized.schema_version
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_rollback_not_exact")
    schema_second = release_operator._sqlite_integrity(  # noqa: SLF001
        config.database,
        require_schema=True,
    )
    release_operator._sqlite_integrity(config.inbox_database, require_schema=False)  # noqa: SLF001
    if (
        schema_second != materialized.schema_version
        or _surface_digest(config, include_sqlite=False) != port.checkpoint_non_sqlite_digest
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_second_reopen_mismatch")
    return _RunResult(
        schema_version=schema_second,
        rollback_tree_sha256=port.rollback_tree_sha256,
        four_surface_sha256=port.checkpoint_digest,
        engineer_authority_present=_engineer_authority_present(material.backup),
    )


def _candidate_sha256(candidate: Mapping[str, Any]) -> str:
    return _sha256(_canonical(dr_index.normalize_generation_candidate(candidate)))


def _authentication_reference(receipt: Mapping[str, Any]) -> str:
    supplied = receipt.get("receipt_sha256")
    core = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if not isinstance(supplied, str) or len(supplied) != 64 or supplied != _sha256(_canonical(core)):
        raise DRGenerationRehearsalError("dr_rehearsal_authentication_receipt_invalid")
    return supplied


def _bind_pending(
    pending: dr_index.PendingDRGenerationIdentity,
    material: dr_auth.AuthenticatedDRMaterial,
) -> None:
    authenticated = material.authenticated
    if (
        pending.candidate != authenticated.candidate
        or pending.candidate_sha256 != _candidate_sha256(authenticated.candidate)
        or pending.authentication_receipt != authenticated.authentication_receipt
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_pending_identity_mismatch")


def _source_projection(receipt: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "activation_journal_file_sha256",
        "activation_journal_sha256",
        "activation_receipt_file_sha256",
        "activation_receipt_sha256",
        "backup_manifest_sha256",
        "restore_operator_sha256",
        "surface_receipts",
    )
    projection = {key: receipt.get(key) for key in keys}
    if any(value is None for value in projection.values()) or not isinstance(
        projection["surface_receipts"],
        dict,
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_authentication_receipt_invalid")
    return projection


def _receipt(
    *,
    pending: dr_index.PendingDRGenerationIdentity,
    material: dr_auth.AuthenticatedDRMaterial,
    result: _RunResult,
) -> dict[str, Any]:
    authentication = material.authenticated.authentication_receipt
    restore = pending.candidate["restore_release"]
    restore_identity = {
        key: restore[key]
        for key in ("commit", "max_schema", "tree_manifest_sha256", "version", "wheel_sha256")
    }
    core: dict[str, Any] = {
        "authentication_receipt_sha256": _authentication_reference(authentication),
        "candidate_sha256": pending.candidate_sha256,
        "check_count": len(_CHECKS),
        "checkset_sha256": _sha256(_canonical(_CHECKS)),
        "database_foreign_keys_clear": True,
        "database_integrity_clear": True,
        "database_reopen_count": 2,
        "database_schema": result.schema_version,
        "engineer_authority_present": result.engineer_authority_present,
        "engineer_exact": True,
        "fault_boundary": "after_migration_before_provision_or_network",
        "four_surface_exact": True,
        "four_surface_sha256": result.four_surface_sha256,
        "index_journal_sha256": pending.authenticated_journal_sha256,
        "index_revision": pending.index_revision,
        "index_transaction_id": pending.index_transaction_id,
        "inbox_foreign_keys_clear": True,
        "inbox_integrity_clear": True,
        "inbox_reopen_count": 2,
        "network_call_count": 0,
        "obsidian_exact": True,
        "production_surface_write_count": 0,
        "restore_release": restore_identity,
        "rollback_restore_observed": True,
        "rollback_tree_sha256": result.rollback_tree_sha256,
        "rolled_back": True,
        "schema": REHEARSAL_RECEIPT_SCHEMA,
        "scratch_removed": True,
        "source": _source_projection(authentication),
        "status": "rehearsed",
        "systemctl_call_count": 0,
    }
    return {**core, "receipt_sha256": _sha256(_canonical(core))}


def _validate_existing_receipt(
    receipt: Mapping[str, Any],
    *,
    pending: dr_index.PendingDRGenerationIdentity,
    material: dr_auth.AuthenticatedDRMaterial,
) -> dict[str, Any]:
    supplied = receipt.get("receipt_sha256")
    core = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    candidate = material.authenticated.candidate
    restore = candidate["restore_release"]
    allowed_rollbacks = {
        material.activation_previous.tree_manifest_sha256,
        material.restore_fallback.tree_manifest_sha256,
    }
    if (
        set(receipt) != _RECEIPT_KEYS
        or supplied != _sha256(_canonical(core))
        or receipt.get("schema") != REHEARSAL_RECEIPT_SCHEMA
        or receipt.get("status") != "rehearsed"
        or receipt.get("candidate_sha256") != pending.candidate_sha256
        or receipt.get("authentication_receipt_sha256")
        != _authentication_reference(material.authenticated.authentication_receipt)
        or receipt.get("index_transaction_id") != pending.index_transaction_id
        or receipt.get("index_revision") != pending.index_revision - 1
        or receipt.get("index_journal_sha256") != pending.authenticated_journal_sha256
        or receipt.get("check_count") != len(_CHECKS)
        or receipt.get("checkset_sha256") != _sha256(_canonical(_CHECKS))
        or receipt.get("database_schema") != material.backup.schema_version
        or receipt.get("database_reopen_count") != 2
        or receipt.get("inbox_reopen_count") != 2
        or receipt.get("database_integrity_clear") is not True
        or receipt.get("database_foreign_keys_clear") is not True
        or receipt.get("inbox_integrity_clear") is not True
        or receipt.get("inbox_foreign_keys_clear") is not True
        or receipt.get("fault_boundary") != "after_migration_before_provision_or_network"
        or not _is_hex64(receipt.get("four_surface_sha256"))
        or receipt.get("restore_release")
        != {
            key: restore[key]
            for key in ("commit", "max_schema", "tree_manifest_sha256", "version", "wheel_sha256")
        }
        or receipt.get("source") != _source_projection(material.authenticated.authentication_receipt)
        or receipt.get("rollback_tree_sha256") not in allowed_rollbacks
        or receipt.get("rolled_back") is not True
        or receipt.get("rollback_restore_observed") is not True
        or receipt.get("four_surface_exact") is not True
        or receipt.get("obsidian_exact") is not True
        or receipt.get("engineer_exact") is not True
        or receipt.get("scratch_removed") is not True
        or receipt.get("systemctl_call_count") != 0
        or receipt.get("network_call_count") != 0
        or receipt.get("production_surface_write_count") != 0
        or receipt.get("engineer_authority_present") != _engineer_authority_present(material.backup)
    ):
        raise DRGenerationRehearsalError("dr_rehearsal_existing_receipt_invalid")
    return dict(receipt)


def rehearse_authenticated_generation(*, activation_receipt: Path) -> dict[str, Any]:
    """Rehearse the exact authenticated pending generation and CAS only that state."""

    friday_home = _canonical_friday_home()
    state_directory = friday_home / "data/state"
    backup_root = friday_home / "data/backups"
    activation_journal = state_directory / "immutable-release-activation.v1.json"
    index = dr_index.DurableDRGenerationIndex(state_directory)
    try:
        with release_operator.OperatorTransactionLock(state_directory / "immutable-release-operator.v1.lock"):
            state = index.load()
            pending = index.pending_generation_identity(
                expected_journal_sha256=str(state["journal_sha256"]),
            )
            material = dr_auth._authenticate_material_locked(  # noqa: SLF001
                activation_journal=activation_journal,
                activation_receipt=activation_receipt,
                backup_root=backup_root,
            )
            _bind_pending(pending, material)
            if pending.index_phase == "rehearsed":
                if pending.rehearsal_receipt is None:
                    raise DRGenerationRehearsalError("dr_rehearsal_existing_receipt_missing")
                return _validate_existing_receipt(
                    pending.rehearsal_receipt,
                    pending=pending,
                    material=material,
                )

            scratch = _new_scratch()
            scratch_identity = _scratch_identity(scratch)
            run_error: BaseException | None = None
            result: _RunResult | None = None
            try:
                result = _run_isolated_rehearsal(material, scratch)
            except BaseException as exc:  # cleanup must precede any error propagation.
                run_error = exc
            try:
                _remove_current_scratch(
                    scratch,
                    expected_identity=scratch_identity,
                )
            except BaseException as cleanup_error:
                raise DRGenerationRehearsalError("dr_rehearsal_scratch_cleanup_failed") from cleanup_error
            if run_error is not None:
                raise DRGenerationRehearsalError("dr_rehearsal_isolated_run_failed") from run_error
            assert result is not None

            material_after = dr_auth._authenticate_material_locked(  # noqa: SLF001
                activation_journal=activation_journal,
                activation_receipt=activation_receipt,
                backup_root=backup_root,
            )
            if material_after != material:
                raise DRGenerationRehearsalError("dr_rehearsal_source_changed")
            pending_after = index.pending_generation_identity(
                expected_journal_sha256=pending.index_journal_sha256,
            )
            if pending_after != pending:
                raise DRGenerationRehearsalError("dr_rehearsal_index_changed")
            body = _receipt(pending=pending, material=material_after, result=result)
            rehearsed = index.record_rehearsed(
                receipt=body,
                expected_journal_sha256=pending.index_journal_sha256,
            )
            if rehearsed["phase"] != "rehearsed" or rehearsed["revision"] != pending.index_revision + 1:
                raise DRGenerationRehearsalError("dr_rehearsal_index_publication_failed")
            return body
    except DRGenerationRehearsalError:
        raise
    except (
        dr_auth.DRGenerationAuthenticationError,
        dr_index.DRGenerationIndexError,
        release_operator.ReleaseFailure,
    ) as exc:
        raise DRGenerationRehearsalError(str(exc)) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation-receipt", type=Path, required=True)
    args = parser.parse_args()
    receipt = rehearse_authenticated_generation(activation_receipt=args.activation_receipt)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "DRGenerationRehearsalError",
    "REHEARSAL_RECEIPT_SCHEMA",
    "rehearse_authenticated_generation",
]
