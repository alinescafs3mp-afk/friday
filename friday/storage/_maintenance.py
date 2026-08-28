"""Storage methods for backup, restore, export, purge and diagnostics.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

import shutil
import stat
import tempfile
import unicodedata
import zlib
from dataclasses import dataclass

from friday.diagnostics.runtime_lease import ProcessLease, RuntimeLeaseError
from friday.document_catalog.schema import (
    register_document_catalog_connection_functions,
    validate_document_catalog_schema,
)
from friday.private_fs import (
    ensure_private_directory,
    prepare_private_sqlite,
    restrict_sqlite_files,
)
from friday.secondary_product_witness import (
    SECONDARY_PRODUCT_BACKUP_LEASE_FILENAME,
    SECONDARY_PRODUCT_BACKUP_LEASE_PROTOCOL,
    is_secondary_product_witness_raw,
)
from friday.storage._base import (
    ACCOUNT_DELETION_ELIGIBILITY_PREFIX,
    LOGGER,
    SCHEMA_VERSION,
    UTC,
    Any,
    Path,
    StorageShared,
    _chmod_private,
    _json_load,
    _safe_filename,
    _sha256_file,
    _stage_private_copy,
    _write_json_atomic,
    _write_recovery_bundle,
    datetime,
    hashlib,
    hmac,
    json,
    os,
    re,
    sqlite3,
    utc_now,
)
from friday.storage._knowledge import _public_knowledge_version_snapshot
from friday.storage._privacy import (
    _not_private_entity_material_dependency,
    _not_private_inbox_dependency,
    _not_private_knowledge_dependency,
    _not_private_notification_dependency,
    _not_private_raw_dependency,
    _not_private_relation_candidate_dependency,
    _not_private_relation_dependency,
    _not_private_reminder_entity,
    _private_entity_material_seeded_query,
)
from friday.storage._restore_barrier import (
    database_restore_intent_lstat,
    database_restore_intent_path,
)


def _engineer_command_backup_authority_required(settings: Any) -> bool:
    """Load the Engineer package only after the storage package is initialized.

    ``command.kernel`` depends on ``file_delivery``, which in turn imports the
    assembled storage surface. Importing the Engineer package while storage
    mixins are still being assembled therefore creates a real import cycle.
    Backup operations run after startup and can safely resolve this dependency
    lazily.
    """

    from friday.organs.engineer.command.backup_authority import (
        command_store_backup_authority_required,
    )

    return command_store_backup_authority_required(settings)


# Historical rows are an egress surface, not inert bookkeeping.  A corrupt or
# hostile database must not make export spend unbounded memory proving that a
# snapshot is safe.  Normal document input is capped at 50 MiB; 64 MiB leaves
# room for the surrounding JSON fields while deliberately failing closed for an
# object whose provenance cannot be inspected within a bounded budget.
_EXPORT_HISTORY_JSON_MAX_CHARS = 64 * 1024 * 1024
_PACKED_KNOWLEDGE_SNAPSHOT_MAGIC = b"zKOV1"
_EXPORT_CURRENT_BODY_MAX_BYTES = 64 * 1_048_576
_EXPORT_CURRENT_FIELD_MAX_BYTES = 1_048_576
_EXPORT_CURRENT_JSON_MAX_BYTES = 1_048_576
_EXPORT_INBOX_JSON_MAX_BYTES = 8_192
_EXPORT_INBOX_NOTES_MAX_CHARS = 4_000
_ENGINEER_BACKUP_AUTHORITY_FIELDS = frozenset(
    {
        "authority_sequence",
        "database_sha256",
        "mac",
        "quiescent",
        "schema",
        "store_id",
    }
)
_BACKUP_SCOPE = {
    "sqlite_database": "included",
    "raw_files": "external",
    "memory_vault": "external",
    "obsidian_profiles_and_vaults": "external",
    "engineer_command_ledger": "external",
    "model_weights": "external",
    "configuration_and_secrets": "external",
}
_RESTORE_INTENT_SCHEMA = "friday.database-restore-intent.v1"
_RESTORE_INTENT_FIELDS = frozenset(
    {
        "created_at",
        "database_path",
        "engineer_command_ledger_authority",
        "original_files",
        "phase",
        "recovery_manifest_sha256",
        "recovery_path",
        "retain_recovery",
        "schema",
        "target_database",
        "target_sha256",
    }
)


@dataclass(frozen=True, slots=True)
class _RestoreRecoveryFile:
    path: Path
    size_bytes: int
    sha256: str
    device: int
    inode: int
    link_count: int


_RestoreFileIdentity = tuple[int, int, int, int, int, int]
_RestoreDirectoryIdentity = tuple[int, int, int, int, int]


def _restore_file_identity_from_stat(observed: os.stat_result) -> _RestoreFileIdentity:
    return (
        int(observed.st_dev),
        int(observed.st_ino),
        int(observed.st_size),
        int(observed.st_mtime_ns),
        int(observed.st_ctime_ns),
        int(observed.st_nlink),
    )


def _restore_directory_identity_from_stat(
    observed: os.stat_result,
    *,
    error_message: str,
) -> _RestoreDirectoryIdentity:
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or observed.st_mode & 0o077
    ):
        raise RuntimeError(error_message)
    return (
        int(observed.st_dev),
        int(observed.st_ino),
        int(stat.S_IMODE(observed.st_mode)),
        int(observed.st_uid),
        int(observed.st_nlink),
    )


class _RestoreIntentCleanupDurabilityError(RuntimeError):
    """The data generation is known, but marker-unlink durability is not."""

    def __init__(self, outcome: str, cause: BaseException) -> None:
        self.outcome = outcome
        super().__init__(
            f"Database restore {outcome}, but restore-intent cleanup durability is uncertain: "
            f"{type(cause).__name__}: {cause}"
        )


@dataclass(frozen=True, slots=True)
class _LoadedRestoreIntent:
    intent: dict[str, Any]
    intent_device: int
    intent_inode: int
    intent_link_count: int
    intent_identity: _RestoreFileIdentity
    intent_sha256: str
    recovery_path: Path
    recovery_device: int
    recovery_inode: int
    recovery_files: dict[str, _RestoreRecoveryFile]


def _strict_fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    before = _restore_directory_identity_from_stat(
        path.lstat(),
        error_message="Restore durability directory changed",
    )
    descriptor = os.open(str(path), flags)
    try:
        opened = _restore_directory_identity_from_stat(
            os.fstat(descriptor),
            error_message="Restore durability directory changed",
        )
        lexical = _restore_directory_identity_from_stat(
            path.lstat(),
            error_message="Restore durability directory changed",
        )
        if not before == opened == lexical:
            raise RuntimeError("Restore durability directory changed")
        os.fsync(descriptor)
        if (
            _restore_directory_identity_from_stat(
                os.fstat(descriptor),
                error_message="Restore durability directory changed",
            )
            != before
            or _restore_directory_identity_from_stat(
                path.lstat(),
                error_message="Restore durability directory changed",
            )
            != before
        ):
            raise RuntimeError("Restore durability directory changed")
    finally:
        os.close(descriptor)


def _strict_fsync_file(
    path: Path,
    *,
    expected_identity: _RestoreFileIdentity | None = None,
) -> _RestoreFileIdentity:
    descriptor, parent_descriptor, parent_identity, identity = _open_pinned_restore_file(
        path,
        error_message="Restore durability path changed",
    )
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_mode & 0o077
            or observed.st_nlink != 1
            or (expected_identity is not None and identity != expected_identity)
        ):
            raise RuntimeError("Restore durability path is not a private regular file")
        if (
            _restore_identity_at(
                parent_descriptor,
                path.name,
                error_message="Restore durability path changed",
            )
            != identity
            or _validated_restore_file_identity(
                os.fstat(descriptor),
                error_message="Restore durability path changed",
            )
            != identity
        ):
            raise RuntimeError("Restore durability path changed")
        _assert_restore_parent_identity(
            path,
            parent_descriptor,
            parent_identity,
            error_message="Restore durability parent changed",
        )
        os.fsync(descriptor)
        final_identity = _validated_restore_file_identity(
            os.fstat(descriptor),
            error_message="Restore durability path changed while it was synced",
        )
        if (
            final_identity != identity
            or (expected_identity is not None and final_identity != expected_identity)
            or _restore_identity_at(
                parent_descriptor,
                path.name,
                error_message="Restore durability path changed while it was synced",
            )
            != identity
        ):
            raise RuntimeError("Restore durability path changed while it was synced")
        _assert_restore_parent_identity(
            path,
            parent_descriptor,
            parent_identity,
            error_message="Restore durability parent changed",
        )
        return final_identity
    finally:
        try:
            os.close(descriptor)
        finally:
            os.close(parent_descriptor)


def _read_private_restore_file(
    path: Path,
    *,
    maximum: int,
    expected_identity: _RestoreFileIdentity | None = None,
) -> bytes:
    descriptor, parent_descriptor, parent_identity, identity = _open_pinned_restore_file(
        path,
        error_message="Restore private file changed while it was opened",
    )
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_mode & 0o077
            or observed.st_nlink != 1
            or observed.st_size <= 0
            or observed.st_size > maximum
            or (expected_identity is not None and identity != expected_identity)
        ):
            raise RuntimeError("Restore private file is invalid")
        payload = os.read(descriptor, maximum + 1)
        if len(payload) > maximum or os.read(descriptor, 1):
            raise RuntimeError("Restore private file is invalid")
        if (
            _restore_file_identity_from_stat(os.fstat(descriptor)) != identity
            or _restore_identity_at(
                parent_descriptor,
                path.name,
                error_message="Restore private file changed while it was read",
            )
            != identity
        ):
            raise RuntimeError("Restore private file changed while it was read")
        _assert_restore_parent_identity(
            path,
            parent_descriptor,
            parent_identity,
            error_message="Restore private file parent changed while it was read",
        )
        return payload
    finally:
        try:
            os.close(descriptor)
        finally:
            os.close(parent_descriptor)


def _sha256_private_restore_file(
    path: Path,
    *,
    expected_identity: _RestoreFileIdentity | None = None,
) -> str:
    descriptor, parent_descriptor, parent_identity, identity = _open_pinned_restore_file(
        path,
        error_message="Restore private file changed while it was opened",
    )
    digest = hashlib.sha256()
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_mode & 0o077
            or observed.st_nlink != 1
            or (expected_identity is not None and identity != expected_identity)
        ):
            raise RuntimeError("Restore private file is invalid")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        if (
            _restore_file_identity_from_stat(os.fstat(descriptor)) != identity
            or _restore_identity_at(
                parent_descriptor,
                path.name,
                error_message="Restore private file changed while it was read",
            )
            != identity
        ):
            raise RuntimeError("Restore private file changed while it was read")
        _assert_restore_parent_identity(
            path,
            parent_descriptor,
            parent_identity,
            error_message="Restore private file parent changed while it was read",
        )
    finally:
        try:
            os.close(descriptor)
        finally:
            os.close(parent_descriptor)
    return digest.hexdigest()


def _restore_intent_path(settings: Any) -> Path:
    return database_restore_intent_path(Path(settings.state_dir))


def _write_restore_intent(path: Path, payload: dict[str, Any]) -> None:
    if set(payload) != _RESTORE_INTENT_FIELDS:
        raise RuntimeError("Restore intent is incomplete")
    _write_json_atomic(path, payload)
    _strict_fsync_file(path)
    _strict_fsync_directory(path.parent)


def _remove_restore_intent(
    path: Path,
    expected_identity: _RestoreFileIdentity,
) -> None:
    # ``missing_ok`` would turn an independently disappeared marker into a
    # successful commit/rollback acknowledgement.  Only an unlink of a marker
    # observed by lstat, followed by a durable directory sync and an lstat
    # ENOENT postcondition, is a completed cleanup.
    _unlink_pinned_restore_file(
        path,
        expected_identity,
        label="Restore intent",
        durable=True,
    )


def _finalize_restore_intent(
    path: Path,
    *,
    outcome: str,
    expected_identity: _RestoreFileIdentity,
    expected_generation: dict[Path, _RestoreFileIdentity | None] | None = None,
) -> None:
    """Remove a marker without turning unlink-success/fsync-failure into a lie."""

    if expected_generation is not None:
        _assert_restore_generation(
            expected_generation,
            error_message="Restore generation changed before restore-intent cleanup",
        )
    try:
        _remove_restore_intent(path, expected_identity)
        if expected_generation is not None:
            _assert_restore_generation(
                expected_generation,
                error_message="Restore generation changed during restore-intent cleanup",
            )
    except BaseException as exc:
        # The data generation is already fsynced at this point.  Whether the
        # marker remains or its unlink reached the running namespace, cleanup
        # failed and the caller must report that known generation rather than
        # infer a rollback from a second recovery attempt.
        try:
            database_restore_intent_lstat(path)
        except BaseException as inspection_error:
            raise _RestoreIntentCleanupDurabilityError(
                outcome,
                inspection_error,
            ) from exc
        raise _RestoreIntentCleanupDurabilityError(outcome, exc) from exc
    try:
        observed = database_restore_intent_lstat(path)
    except BaseException as exc:
        raise _RestoreIntentCleanupDurabilityError(outcome, exc) from exc
    if observed is not None:
        raise _RestoreIntentCleanupDurabilityError(
            outcome,
            RuntimeError("Restore intent remains after cleanup"),
        )


def _active_restore_paths(database_path: Path) -> tuple[Path, Path, Path]:
    return database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")


def _private_regular_files(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    present: list[Path] = []
    for path in paths:
        try:
            observed = path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(observed.st_mode)
            or stat.S_ISLNK(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_mode & 0o077
            or observed.st_nlink != 1
        ):
            raise RuntimeError(
                "Database path and SQLite sidecars must not be symlinks or hardlinks "
                "and must be private regular files"
            )
        present.append(path)
    return tuple(present)


def _durable_recovery_bundle(
    settings: Any,
    snapshots: dict[Path, Path],
    *,
    label: str,
    reason_type: str,
) -> dict[str, Any]:
    bundle = _write_recovery_bundle(
        settings,
        snapshots,
        label=label,
        reason_type=reason_type,
    )
    recovery_path = Path(str(bundle["path"]))
    manifest_path = Path(str(bundle["manifest_path"]))
    for item in bundle.get("files", []):
        if not isinstance(item, dict) or type(item.get("name")) is not str:
            raise RuntimeError("Restore recovery set is incomplete")
        _strict_fsync_file(recovery_path / str(item["name"]))
    _strict_fsync_file(manifest_path)
    _strict_fsync_directory(recovery_path)
    _strict_fsync_directory(recovery_path.parent)
    return bundle


def _load_restore_intent(
    settings: Any,
    database_path: Path,
) -> _LoadedRestoreIntent:
    intent_path = _restore_intent_path(settings)
    try:
        observed_intent = database_restore_intent_lstat(intent_path)
        if observed_intent is None:
            raise RuntimeError("Restore intent is missing")
        if (
            not stat.S_ISREG(observed_intent.st_mode)
            or stat.S_ISLNK(observed_intent.st_mode)
            or observed_intent.st_uid != os.geteuid()
            or observed_intent.st_mode & 0o077
            or observed_intent.st_nlink != 1
            or observed_intent.st_size <= 0
            or observed_intent.st_size > 32_768
        ):
            raise RuntimeError("Restore intent is invalid")
        intent_identity = _restore_file_identity_from_stat(observed_intent)
        intent_payload = _read_private_restore_file(
            intent_path,
            maximum=32_768,
            expected_identity=intent_identity,
        )
        observed_intent_after = intent_path.lstat()
        if (
            _validated_restore_file_identity(
                observed_intent_after,
                error_message="Restore intent changed while it was read",
            )
            != intent_identity
        ):
            raise RuntimeError("Restore intent changed while it was read")
        intent = json.loads(
            intent_payload,
            object_pairs_hook=_closed_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Restore intent is invalid") from exc
    if not isinstance(intent, dict) or set(intent) != _RESTORE_INTENT_FIELDS:
        raise RuntimeError("Restore intent is invalid")
    original_files = intent.get("original_files")
    engineer_authority = intent.get("engineer_command_ledger_authority")
    active_paths = _active_restore_paths(database_path)
    active_names = {path.name for path in active_paths}
    if (
        intent.get("schema") != _RESTORE_INTENT_SCHEMA
        or intent.get("phase") not in {"prepared", "committed"}
        or type(intent.get("created_at")) is not str
        or not str(intent.get("created_at"))
        or intent.get("database_path") != str(database_path)
        or type(intent.get("target_database")) is not str
        or Path(str(intent.get("target_database"))).name != intent.get("target_database")
        or type(intent.get("target_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", str(intent.get("target_sha256"))) is None
        or type(intent.get("recovery_manifest_sha256")) is not str
        or re.fullmatch(r"[0-9a-f]{64}", str(intent.get("recovery_manifest_sha256"))) is None
        or type(intent.get("retain_recovery")) is not bool
        or (
            engineer_authority is not None
            and (
                not isinstance(engineer_authority, dict)
                or set(engineer_authority) != _ENGINEER_BACKUP_AUTHORITY_FIELDS
            )
        )
        or not isinstance(original_files, list)
        or any(type(name) is not str or name not in active_names for name in original_files)
        or len(original_files) != len(set(original_files))
    ):
        raise RuntimeError("Restore intent is invalid")

    backups_dir = Path(settings.backups_dir).absolute()
    recovery_path = Path(str(intent.get("recovery_path")))
    if (
        not recovery_path.is_absolute()
        or recovery_path.parent != backups_dir
        or not recovery_path.name.startswith("recovery-")
    ):
        raise RuntimeError("Restore recovery path is invalid")
    try:
        directory_status = recovery_path.lstat()
        if (
            not stat.S_ISDIR(directory_status.st_mode)
            or stat.S_ISLNK(directory_status.st_mode)
            or directory_status.st_uid != os.geteuid()
            or directory_status.st_mode & 0o077
        ):
            raise RuntimeError("Restore recovery path is invalid")
    except OSError as exc:
        raise RuntimeError("Restore recovery path is invalid") from exc
    manifest_path = recovery_path / "recovery.json"
    try:
        manifest_identity = _restore_regular_file_identity(manifest_path)
        manifest_payload = _read_private_restore_file(
            manifest_path,
            maximum=65_536,
            expected_identity=manifest_identity,
        )
        if not hmac.compare_digest(
            hashlib.sha256(manifest_payload).hexdigest(),
            str(intent["recovery_manifest_sha256"]),
        ):
            raise RuntimeError("Restore recovery manifest changed")
        manifest = json.loads(
            manifest_payload,
            object_pairs_hook=_closed_json_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("Restore recovery manifest is invalid") from exc
    expected_manifest_fields = {
        "created_at",
        "label",
        "verified",
        "restorable_by_automatic_command",
        "reason_type",
        "files",
        "note",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != expected_manifest_fields
        or manifest.get("verified") is not False
        or manifest.get("restorable_by_automatic_command") is not False
        or not isinstance(manifest.get("files"), list)
    ):
        raise RuntimeError("Restore recovery manifest is invalid")
    recovery_files: dict[str, _RestoreRecoveryFile] = {}
    for item in manifest["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "sha256", "size_bytes"}
            or type(item.get("name")) is not str
            or item["name"] not in active_names
            or item["name"] in recovery_files
            or type(item.get("size_bytes")) is not int
            or item["size_bytes"] < 0
            or type(item.get("sha256")) is not str
            or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
        ):
            raise RuntimeError("Restore recovery manifest is invalid")
        source = recovery_path / item["name"]
        try:
            source_status = source.lstat()
        except OSError as exc:
            raise RuntimeError("Restore recovery set is incomplete") from exc
        if (
            not stat.S_ISREG(source_status.st_mode)
            or stat.S_ISLNK(source_status.st_mode)
            or source_status.st_uid != os.geteuid()
            or source_status.st_mode & 0o077
            or source_status.st_nlink != 1
            or source_status.st_size != item["size_bytes"]
            or not hmac.compare_digest(
                _sha256_private_restore_file(
                    source,
                    expected_identity=_restore_file_identity_from_stat(source_status),
                ),
                item["sha256"],
            )
        ):
            raise RuntimeError("Restore recovery set is invalid")
        recovery_files[item["name"]] = _RestoreRecoveryFile(
            path=source,
            size_bytes=int(item["size_bytes"]),
            sha256=str(item["sha256"]),
            device=int(source_status.st_dev),
            inode=int(source_status.st_ino),
            link_count=int(source_status.st_nlink),
        )
    if set(recovery_files) != set(original_files):
        raise RuntimeError("Restore recovery set is incomplete")
    return _LoadedRestoreIntent(
        intent=intent,
        intent_device=int(observed_intent.st_dev),
        intent_inode=int(observed_intent.st_ino),
        intent_link_count=int(observed_intent.st_nlink),
        intent_identity=intent_identity,
        intent_sha256=hashlib.sha256(intent_payload).hexdigest(),
        recovery_path=recovery_path,
        recovery_device=int(directory_status.st_dev),
        recovery_inode=int(directory_status.st_ino),
        recovery_files=recovery_files,
    )


def _reload_exact_restore_intent(
    settings: Any,
    database_path: Path,
    expected: _LoadedRestoreIntent,
) -> _LoadedRestoreIntent:
    """Revalidate every pathname-bound recovery authority before mutation."""

    observed = _load_restore_intent(settings, database_path)
    if (
        observed.intent != expected.intent
        or observed.intent_device != expected.intent_device
        or observed.intent_inode != expected.intent_inode
        or observed.intent_link_count != expected.intent_link_count
        or observed.intent_identity != expected.intent_identity
        or not hmac.compare_digest(observed.intent_sha256, expected.intent_sha256)
        or observed.recovery_path != expected.recovery_path
        or observed.recovery_device != expected.recovery_device
        or observed.recovery_inode != expected.recovery_inode
        or observed.recovery_files != expected.recovery_files
    ):
        raise RuntimeError("Restore intent or recovery authority changed")
    return observed


def _verify_recovery_directory(loaded: _LoadedRestoreIntent) -> None:
    try:
        observed = loaded.recovery_path.lstat()
    except OSError as exc:
        raise RuntimeError("Restore recovery path changed") from exc
    if (
        not stat.S_ISDIR(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or observed.st_mode & 0o077
        or int(observed.st_dev) != loaded.recovery_device
        or int(observed.st_ino) != loaded.recovery_inode
    ):
        raise RuntimeError("Restore recovery path changed")


def _restore_regular_file_identity(path: Path) -> _RestoreFileIdentity:
    try:
        observed = path.lstat()
    except OSError as exc:
        raise RuntimeError("Restore file identity changed") from exc
    return _validated_restore_file_identity(
        observed,
        error_message="Restore file identity changed",
    )


def _validated_restore_file_identity(
    observed: os.stat_result,
    *,
    error_message: str,
) -> _RestoreFileIdentity:
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or observed.st_mode & 0o077
        or observed.st_nlink != 1
    ):
        raise RuntimeError(error_message)
    return _restore_file_identity_from_stat(observed)


def _open_private_restore_parent(
    path: Path,
    *,
    error_message: str,
) -> tuple[int, _RestoreDirectoryIdentity]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        before = _restore_directory_identity_from_stat(
            path.parent.lstat(),
            error_message=error_message,
        )
        descriptor = os.open(str(path.parent), flags)
    except OSError as exc:
        raise RuntimeError(error_message) from exc
    try:
        opened = _restore_directory_identity_from_stat(
            os.fstat(descriptor),
            error_message=error_message,
        )
        after = _restore_directory_identity_from_stat(
            path.parent.lstat(),
            error_message=error_message,
        )
    except BaseException:
        os.close(descriptor)
        raise
    if not before == opened == after:
        os.close(descriptor)
        raise RuntimeError(error_message)
    return descriptor, opened


def _assert_restore_parent_identity(
    path: Path,
    descriptor: int,
    expected: _RestoreDirectoryIdentity,
    *,
    error_message: str,
) -> None:
    try:
        lexical = _restore_directory_identity_from_stat(
            path.parent.lstat(),
            error_message=error_message,
        )
        opened = _restore_directory_identity_from_stat(
            os.fstat(descriptor),
            error_message=error_message,
        )
    except OSError as exc:
        raise RuntimeError(error_message) from exc
    if lexical != expected or opened != expected:
        raise RuntimeError(error_message)


def _restore_identity_at(
    parent_descriptor: int,
    name: str,
    *,
    error_message: str,
) -> _RestoreFileIdentity | None:
    try:
        observed = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(error_message) from exc
    return _validated_restore_file_identity(observed, error_message=error_message)


def _open_pinned_restore_file(
    path: Path,
    *,
    error_message: str,
) -> tuple[int, int, _RestoreDirectoryIdentity, _RestoreFileIdentity]:
    """Open the lexical file through its verified, pinned parent directory."""

    parent_descriptor, parent_identity = _open_private_restore_parent(
        path,
        error_message=error_message,
    )
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                path.name,
                flags,
                dir_fd=parent_descriptor,
            )
        except OSError as exc:
            raise RuntimeError(error_message) from exc
        identity = _validated_restore_file_identity(
            os.fstat(descriptor),
            error_message=error_message,
        )
        if (
            _restore_identity_at(
                parent_descriptor,
                path.name,
                error_message=error_message,
            )
            != identity
        ):
            raise RuntimeError(error_message)
        _assert_restore_parent_identity(
            path,
            parent_descriptor,
            parent_identity,
            error_message=error_message,
        )
        return descriptor, parent_descriptor, parent_identity, identity
    except BaseException:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        finally:
            os.close(parent_descriptor)
        raise


def _unlink_pinned_restore_file(
    path: Path,
    expected_identity: _RestoreFileIdentity,
    *,
    label: str,
    durable: bool,
) -> None:
    """Unlink exactly one opened single-link inode through its pinned parent."""

    error_message = f"{label} changed before restore cleanup"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError as exc:
        raise RuntimeError(error_message) from exc
    parent_descriptor = -1
    try:
        parent_descriptor, parent_identity = _open_private_restore_parent(
            path,
            error_message=error_message,
        )
        before = _validated_restore_file_identity(
            os.fstat(descriptor),
            error_message=error_message,
        )
        if before != expected_identity:
            raise RuntimeError(error_message)
        if (
            _restore_identity_at(
                parent_descriptor,
                path.name,
                error_message=error_message,
            )
            != expected_identity
            or _validated_restore_file_identity(
                os.fstat(descriptor),
                error_message=error_message,
            )
            != expected_identity
        ):
            raise RuntimeError(error_message)
        _assert_restore_parent_identity(
            path,
            parent_descriptor,
            parent_identity,
            error_message=error_message,
        )
        os.unlink(path.name, dir_fd=parent_descriptor)
        after = os.fstat(descriptor)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_uid != os.geteuid()
            or after.st_mode & 0o077
            or _restore_file_identity_from_stat(after)[:4] != expected_identity[:4]
            or after.st_nlink != 0
        ):
            raise RuntimeError(error_message)
        _assert_restore_parent_identity(
            path,
            parent_descriptor,
            parent_identity,
            error_message=error_message,
        )
        if durable:
            os.fsync(parent_descriptor)
        if (
            _restore_identity_at(
                parent_descriptor,
                path.name,
                error_message=error_message,
            )
            is not None
        ):
            raise RuntimeError(error_message)
        _assert_restore_parent_identity(
            path,
            parent_descriptor,
            parent_identity,
            error_message=error_message,
        )
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        os.close(descriptor)


def _optional_restore_file_identity(path: Path) -> _RestoreFileIdentity | None:
    try:
        return _restore_regular_file_identity(path)
    except RuntimeError as exc:
        try:
            path.lstat()
        except FileNotFoundError:
            return None
        except OSError as inspection_error:
            raise RuntimeError("Restore file identity is ambiguous") from inspection_error
        raise exc


def _assert_restore_path_identity(
    path: Path,
    expected: _RestoreFileIdentity | None,
    *,
    label: str,
) -> None:
    if _optional_restore_file_identity(path) != expected:
        raise RuntimeError(f"{label} changed before restore mutation")


def _assert_restore_generation(
    expected: dict[Path, _RestoreFileIdentity | None],
    *,
    error_message: str,
) -> None:
    """Verify one complete active generation through a pinned parent directory."""

    if not expected:
        return
    parents = {path.parent for path in expected}
    if len(parents) != 1:
        raise RuntimeError(error_message)
    representative = next(iter(expected))
    parent_descriptor, parent_identity = _open_private_restore_parent(
        representative,
        error_message=error_message,
    )
    try:
        for path, identity in expected.items():
            if (
                path.parent != representative.parent
                or _restore_identity_at(
                    parent_descriptor,
                    path.name,
                    error_message=error_message,
                )
                != identity
            ):
                raise RuntimeError(error_message)
        _assert_restore_parent_identity(
            representative,
            parent_descriptor,
            parent_identity,
            error_message=error_message,
        )
        for path, identity in expected.items():
            if (
                _restore_identity_at(
                    parent_descriptor,
                    path.name,
                    error_message=error_message,
                )
                != identity
            ):
                raise RuntimeError(error_message)
        _assert_restore_parent_identity(
            representative,
            parent_descriptor,
            parent_identity,
            error_message=error_message,
        )
    finally:
        os.close(parent_descriptor)


def _unlink_expected_restore_path(
    path: Path,
    expected: _RestoreFileIdentity | None,
) -> None:
    if expected is None:
        _assert_restore_path_identity(path, None, label="Active database path")
        return
    _unlink_pinned_restore_file(
        path,
        expected,
        label="Active database path",
        durable=False,
    )


def _replace_expected_restore_path(
    prepared: Path,
    destination: Path,
    *,
    prepared_identity: _RestoreFileIdentity,
    destination_identity: _RestoreFileIdentity | None,
) -> _RestoreFileIdentity:
    prepared_error = "Restore staged copy changed before restore mutation"
    destination_error = "Active database path changed before restore mutation"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(prepared), flags)
    except OSError as exc:
        raise RuntimeError(prepared_error) from exc
    destination_descriptor = -1
    source_parent = -1
    destination_parent = -1
    try:
        source_parent, source_parent_identity = _open_private_restore_parent(
            prepared,
            error_message=prepared_error,
        )
        destination_parent, destination_parent_identity = _open_private_restore_parent(
            destination,
            error_message=destination_error,
        )
        if destination_identity is not None:
            try:
                destination_descriptor = os.open(
                    destination.name,
                    flags,
                    dir_fd=destination_parent,
                )
            except OSError as exc:
                raise RuntimeError(destination_error) from exc
        if (
            _validated_restore_file_identity(
                os.fstat(descriptor),
                error_message=prepared_error,
            )
            != prepared_identity
            or _restore_identity_at(
                source_parent,
                prepared.name,
                error_message=prepared_error,
            )
            != prepared_identity
            or _restore_identity_at(
                destination_parent,
                destination.name,
                error_message=destination_error,
            )
            != destination_identity
            or (
                destination_descriptor >= 0
                and _validated_restore_file_identity(
                    os.fstat(destination_descriptor),
                    error_message=destination_error,
                )
                != destination_identity
            )
        ):
            raise RuntimeError(prepared_error)
        # Repeat both pathname bindings immediately before the atomic rename.
        if (
            _validated_restore_file_identity(
                os.fstat(descriptor),
                error_message=prepared_error,
            )
            != prepared_identity
            or _restore_identity_at(
                source_parent,
                prepared.name,
                error_message=prepared_error,
            )
            != prepared_identity
            or _restore_identity_at(
                destination_parent,
                destination.name,
                error_message=destination_error,
            )
            != destination_identity
            or (
                destination_descriptor >= 0
                and _validated_restore_file_identity(
                    os.fstat(destination_descriptor),
                    error_message=destination_error,
                )
                != destination_identity
            )
        ):
            raise RuntimeError(prepared_error)
        _assert_restore_parent_identity(
            prepared,
            source_parent,
            source_parent_identity,
            error_message=prepared_error,
        )
        _assert_restore_parent_identity(
            destination,
            destination_parent,
            destination_parent_identity,
            error_message=destination_error,
        )
        os.replace(
            prepared.name,
            destination.name,
            src_dir_fd=source_parent,
            dst_dir_fd=destination_parent,
        )
        if destination_descriptor >= 0:
            if destination_identity is None:
                raise RuntimeError(destination_error)
            displaced = os.fstat(destination_descriptor)
            if (
                not stat.S_ISREG(displaced.st_mode)
                or displaced.st_uid != os.geteuid()
                or displaced.st_mode & 0o077
                or displaced.st_nlink != 0
                or _restore_file_identity_from_stat(displaced)[:4] != destination_identity[:4]
            ):
                raise RuntimeError(destination_error)
        _assert_restore_parent_identity(
            prepared,
            source_parent,
            source_parent_identity,
            error_message=prepared_error,
        )
        _assert_restore_parent_identity(
            destination,
            destination_parent,
            destination_parent_identity,
            error_message=destination_error,
        )
        after = _validated_restore_file_identity(
            os.fstat(descriptor),
            error_message=prepared_error,
        )
        observed = _restore_identity_at(
            destination_parent,
            destination.name,
            error_message=prepared_error,
        )
        # rename may update ctime, but never the inode, length, content mtime or
        # single-link invariant.  The source name must be gone from the pinned dir.
        if (
            after[:4] != prepared_identity[:4]
            or observed != after
            or _restore_identity_at(
                source_parent,
                prepared.name,
                error_message=prepared_error,
            )
            is not None
        ):
            raise RuntimeError(prepared_error)
        _assert_restore_parent_identity(
            prepared,
            source_parent,
            source_parent_identity,
            error_message=prepared_error,
        )
        _assert_restore_parent_identity(
            destination,
            destination_parent,
            destination_parent_identity,
            error_message=destination_error,
        )
        return after
    finally:
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if destination_parent >= 0:
            os.close(destination_parent)
        if source_parent >= 0:
            os.close(source_parent)
        os.close(descriptor)


def _verify_recovery_source(snapshot: _RestoreRecoveryFile) -> None:
    try:
        observed = snapshot.path.lstat()
    except OSError as exc:
        raise RuntimeError("Restore recovery source changed") from exc
    observed_identity = _restore_file_identity_from_stat(observed)
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or observed.st_mode & 0o077
        or int(observed.st_dev) != snapshot.device
        or int(observed.st_ino) != snapshot.inode
        or int(observed.st_nlink) != snapshot.link_count
        or observed.st_nlink != 1
        or int(observed.st_size) != snapshot.size_bytes
        or not hmac.compare_digest(
            _sha256_private_restore_file(
                snapshot.path,
                expected_identity=observed_identity,
            ),
            snapshot.sha256,
        )
    ):
        raise RuntimeError("Restore recovery source changed")


def _stage_verified_recovery_copy(
    snapshot: _RestoreRecoveryFile,
    destination: Path,
) -> Path:
    """Copy one recovery member from a pinned no-follow descriptor."""

    ensure_private_directory(destination.parent)
    source_descriptor = -1
    source_parent_descriptor = -1
    source_parent_identity: _RestoreDirectoryIdentity | None = None
    source_identity: _RestoreFileIdentity | None = None
    target_descriptor = -1
    temporary: Path | None = None
    digest = hashlib.sha256()
    copied = 0
    try:
        try:
            (
                source_descriptor,
                source_parent_descriptor,
                source_parent_identity,
                source_identity,
            ) = _open_pinned_restore_file(
                snapshot.path,
                error_message="Restore recovery source changed",
            )
        except (OSError, RuntimeError) as exc:
            raise RuntimeError("Restore recovery source changed") from exc
        source_status = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_status.st_mode)
            or source_status.st_uid != os.geteuid()
            or source_status.st_mode & 0o077
            or int(source_status.st_dev) != snapshot.device
            or int(source_status.st_ino) != snapshot.inode
            or int(source_status.st_nlink) != snapshot.link_count
            or source_status.st_nlink != 1
            or int(source_status.st_size) != snapshot.size_bytes
        ):
            raise RuntimeError("Restore recovery source changed")
        target_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.restore-",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        os.fchmod(target_descriptor, 0o600)
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_descriptor, view)
                if written <= 0:
                    raise OSError("short restore recovery write")
                view = view[written:]
        final_source_status = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(final_source_status.st_mode)
            or final_source_status.st_uid != os.geteuid()
            or final_source_status.st_mode & 0o077
            or (int(final_source_status.st_dev), int(final_source_status.st_ino))
            != (snapshot.device, snapshot.inode)
            or int(final_source_status.st_nlink) != snapshot.link_count
            or final_source_status.st_nlink != 1
            or int(final_source_status.st_size) != snapshot.size_bytes
            or _restore_file_identity_from_stat(final_source_status) != source_identity
            or copied != snapshot.size_bytes
            or not hmac.compare_digest(digest.hexdigest(), snapshot.sha256)
        ):
            raise RuntimeError("Restore recovery source changed")
        if (
            _restore_identity_at(
                source_parent_descriptor,
                snapshot.path.name,
                error_message="Restore recovery source changed",
            )
            != source_identity
        ):
            raise RuntimeError("Restore recovery source changed")
        if source_parent_identity is None:
            raise RuntimeError("Restore recovery source changed")
        _assert_restore_parent_identity(
            snapshot.path,
            source_parent_descriptor,
            source_parent_identity,
            error_message="Restore recovery source changed",
        )
        os.fsync(target_descriptor)
        target_status = os.fstat(target_descriptor)
        if (
            not stat.S_ISREG(target_status.st_mode)
            or target_status.st_uid != os.geteuid()
            or target_status.st_mode & 0o077
            or target_status.st_nlink != 1
            or int(target_status.st_size) != snapshot.size_bytes
        ):
            raise RuntimeError("Restore recovery staged copy is invalid")
        os.close(target_descriptor)
        target_descriptor = -1
        os.close(source_descriptor)
        source_descriptor = -1
        os.close(source_parent_descriptor)
        source_parent_descriptor = -1
        return temporary
    except BaseException:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if source_parent_descriptor >= 0:
            os.close(source_parent_descriptor)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _verify_staged_recovery_copy(
    path: Path,
    snapshot: _RestoreRecoveryFile,
) -> _RestoreFileIdentity:
    return _verify_exact_restore_copy(
        path,
        size_bytes=snapshot.size_bytes,
        sha256=snapshot.sha256,
        error_message="Restore recovery staged copy is invalid",
    )


def _verify_exact_restore_copy(
    path: Path,
    *,
    size_bytes: int,
    sha256: str,
    error_message: str,
) -> _RestoreFileIdentity:
    """Hash the inode still named by *path* and reject every identity drift."""

    try:
        initial_identity = _restore_regular_file_identity(path)
        descriptor = os.open(
            str(path),
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise RuntimeError(error_message) from exc
    except RuntimeError as exc:
        raise RuntimeError(error_message) from exc
    digest = hashlib.sha256()
    copied = 0
    try:
        observed = os.fstat(descriptor)
        descriptor_identity = _restore_file_identity_from_stat(observed)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or observed.st_mode & 0o077
            or observed.st_nlink != 1
            or descriptor_identity != initial_identity
        ):
            raise RuntimeError(error_message)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            digest.update(chunk)
        final = os.fstat(descriptor)
        final_identity = _restore_file_identity_from_stat(final)
        if (
            final_identity != initial_identity
            or _restore_regular_file_identity(path) != initial_identity
            or copied != size_bytes
            or not hmac.compare_digest(digest.hexdigest(), sha256)
        ):
            raise RuntimeError(error_message)
        return initial_identity
    finally:
        os.close(descriptor)


def _verify_restore_intent_authority(
    settings: Any,
    authority: Any | None,
    evidence: object,
    *,
    database_sha256: str,
) -> tuple[str, int, bool] | None:
    required = _engineer_command_backup_authority_required(settings) or evidence is not None
    if not required:
        return None
    if evidence is None:
        raise RuntimeError("Restore intent Engineer authority evidence is missing")
    if not isinstance(evidence, dict) or set(evidence) != _ENGINEER_BACKUP_AUTHORITY_FIELDS:
        raise RuntimeError("Restore intent Engineer authority evidence is invalid")
    if authority is None:
        raise RuntimeError("Restore intent Engineer authority is unavailable")
    try:
        verified = authority.verify_main_database_backup_authority(
            evidence,
            database_sha256,
        )
    except Exception as exc:
        raise RuntimeError("Restore intent Engineer authority changed") from exc
    snapshot = _engineer_backup_snapshot(verified)
    if (
        snapshot[0] != evidence.get("store_id")
        or snapshot[1] != evidence.get("authority_sequence")
        or snapshot[2] is not evidence.get("quiescent")
    ):
        raise RuntimeError("Restore intent Engineer authority changed")
    return snapshot


def _discard_restore_recovery(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    try:
        if expected_identity is not None:
            observed = path.lstat()
            if (
                not stat.S_ISDIR(observed.st_mode)
                or stat.S_ISLNK(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or (int(observed.st_dev), int(observed.st_ino)) != expected_identity
            ):
                raise OSError("restore recovery directory identity changed")
        shutil.rmtree(path)
        _strict_fsync_directory(path.parent)
    except OSError as exc:
        LOGGER.warning("Could not prune completed restore recovery set (%s)", type(exc).__name__)


def _recover_interrupted_restore(
    settings: Any,
    database_path: Path,
    *,
    engineer_authority: Any | None = None,
    expected_committed_generation: dict[Path, _RestoreFileIdentity | None] | None = None,
) -> str:
    """Finish an interrupted restore transaction under stopped process leases."""

    intent_path = _restore_intent_path(settings)
    if database_restore_intent_lstat(intent_path) is None:
        return "absent"
    loaded = _load_restore_intent(settings, database_path)
    intent = loaded.intent
    authority_evidence = intent.get("engineer_command_ledger_authority")
    # Refuse before even creating temporary staging files if the external ledger
    # moved while this process was down.
    _verify_restore_intent_authority(
        settings,
        engineer_authority,
        authority_evidence,
        database_sha256=str(intent["target_sha256"]),
    )
    if intent["phase"] == "committed":
        # Committed means never rewind the new generation.  Recheck immediately
        # before cleanup so an advanced external ledger cannot make an old marker
        # silently disappear.
        loaded = _reload_exact_restore_intent(settings, database_path, loaded)
        authority_evidence = loaded.intent.get("engineer_command_ledger_authority")
        _verify_restore_intent_authority(
            settings,
            engineer_authority,
            authority_evidence,
            database_sha256=str(intent["target_sha256"]),
        )
        _finalize_restore_intent(
            intent_path,
            outcome="committed",
            expected_identity=loaded.intent_identity,
            expected_generation=expected_committed_generation,
        )
        if not intent["retain_recovery"]:
            _discard_restore_recovery(
                loaded.recovery_path,
                expected_identity=(loaded.recovery_device, loaded.recovery_inode),
            )
        return "committed"

    active_paths = _active_restore_paths(database_path)
    active_originals = _private_regular_files(active_paths)
    active_identities = {path: _restore_regular_file_identity(path) for path in active_originals}
    staged: dict[Path, tuple[Path, _RestoreRecoveryFile]] = {}
    restored_identities: dict[Path, _RestoreFileIdentity] = {}
    try:
        for active_path in active_paths:
            source = loaded.recovery_files.get(active_path.name)
            if source is not None:
                _verify_recovery_source(source)
                staged[active_path] = (
                    _stage_verified_recovery_copy(source, active_path),
                    source,
                )
        if set(staged) != {path for path in active_paths if path.name in loaded.recovery_files}:
            raise RuntimeError("Restore recovery staging is incomplete")
        _verify_recovery_directory(loaded)
        for _prepared, source in staged.values():
            _verify_recovery_source(source)
        observed_active = _private_regular_files(active_paths)
        if set(observed_active) != set(active_originals) or any(
            _restore_regular_file_identity(path) != active_identities[path] for path in observed_active
        ):
            raise RuntimeError("Active database changed during restore recovery staging")
        # The public restore path holds the exclusive ledger owner for this
        # complete window.  Refusal therefore happens before active/recovery data
        # mutation, while this final check also proves exact identity/quiescence.
        _verify_restore_intent_authority(
            settings,
            engineer_authority,
            authority_evidence,
            database_sha256=str(intent["target_sha256"]),
        )
        # Hash the staged bytes (not merely their inode/size) after the final
        # authority decision and immediately before the first active unlink or
        # replacement.  Every later replacement rechecks identity and bytes.
        staged_identities = {
            prepared: _verify_staged_recovery_copy(prepared, source) for prepared, source in staged.values()
        }
        # The durable bundle is still the crash-recovery authority until the
        # marker is removed.  Reject a directory/member pathname swap before
        # touching the active generation, even though this attempt already has
        # independently verified staged bytes.
        _verify_recovery_directory(loaded)
        for _prepared, source in staged.values():
            _verify_recovery_source(source)
        loaded = _reload_exact_restore_intent(settings, database_path, loaded)
        authority_evidence = loaded.intent.get("engineer_command_ledger_authority")
        _verify_restore_intent_authority(
            settings,
            engineer_authority,
            authority_evidence,
            database_sha256=str(intent["target_sha256"]),
        )
        for prepared, source in staged.values():
            if _verify_staged_recovery_copy(prepared, source) != staged_identities[prepared]:
                raise RuntimeError("Restore recovery staged copy changed")
        for active_path in active_paths:
            if active_path not in staged:
                _unlink_expected_restore_path(
                    active_path,
                    active_identities.get(active_path),
                )
        for original, (prepared, source) in staged.items():
            # A second full content proof closes in-place writes after the batch
            # verification above.  os.replace never follows a final symlink, and
            # no path-based chmod is performed after the replacement.
            prepared_identity = _verify_staged_recovery_copy(prepared, source)
            if prepared_identity != staged_identities[prepared]:
                raise RuntimeError("Restore recovery staged copy changed")
            replaced_identity = _replace_expected_restore_path(
                prepared,
                original,
                prepared_identity=prepared_identity,
                destination_identity=active_identities.get(original),
            )
            verified_identity = _verify_staged_recovery_copy(original, source)
            if verified_identity != replaced_identity:
                raise RuntimeError("Restore recovery staged copy changed")
            restored_identities[original] = verified_identity
        if set(restored_identities) != set(staged):
            raise RuntimeError("Restore recovery final generation is incomplete")
        expected_generation = {
            active_path: restored_identities.get(active_path) for active_path in active_paths
        }
        for original, identity in restored_identities.items():
            if _strict_fsync_file(original, expected_identity=identity) != identity:
                raise RuntimeError("Restore recovery final generation changed")
        _strict_fsync_directory(database_path.parent)
        _assert_restore_generation(
            expected_generation,
            error_message="Restore recovery final generation changed",
        )
        _finalize_restore_intent(
            intent_path,
            outcome="rolled_back",
            expected_identity=loaded.intent_identity,
            expected_generation=expected_generation,
        )
        if not intent["retain_recovery"]:
            _discard_restore_recovery(
                loaded.recovery_path,
                expected_identity=(loaded.recovery_device, loaded.recovery_inode),
            )
        return "rolled_back"
    finally:
        for prepared, _source in staged.values():
            prepared.unlink(missing_ok=True)


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _engineer_backup_snapshot(value: object) -> tuple[str, int, bool]:
    if (
        not isinstance(value, tuple)
        or len(value) != 3
        or type(value[0]) is not str
        or re.fullmatch(r"[0-9a-f]{32}", value[0]) is None
        or type(value[1]) is not int
        or not 0 <= value[1] <= 9_223_372_036_854_775_806
        or type(value[2]) is not bool
    ):
        raise RuntimeError("Engineer command backup authority returned an invalid snapshot")
    if not value[2]:
        raise RuntimeError("Backup is blocked by unresolved Engineer command state")
    return value[0], value[1], True


def _engineer_backup_evidence_identity(value: object) -> tuple[str, int, bool]:
    if not isinstance(value, dict) or set(value) != _ENGINEER_BACKUP_AUTHORITY_FIELDS:
        raise RuntimeError("Engineer command backup authority returned invalid evidence")
    return _engineer_backup_snapshot(
        (
            value.get("store_id"),
            value.get("authority_sequence"),
            value.get("quiescent"),
        )
    )


def _main_engineer_delivery_is_quiescent(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM outbound_notifications
                    WHERE kind IN (
                              'engineer_command_terminal',
                              'engineer_command_terminal_text',
                              'engineer_command_unknown',
                              'engineer_command_progress'
                          )
                      AND status='pending'
               )"""
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("Engineer delivery quiescence is unavailable") from exc
    if row is None or type(row[0]) is not int or row[0] not in {0, 1}:
        raise RuntimeError("Engineer delivery quiescence is invalid")
    return row[0] == 0


def _contains_secondary_product_witness(conn: sqlite3.Connection) -> bool:
    """Inspect one transactionally consistent SQLite view for the exact probe."""

    rows = conn.execute(
        """SELECT source, source_ref, raw_content, content_hash, metadata_json
             FROM raw_objects
            WHERE source='api' AND source_ref LIKE 'secondary-product-witness:%'"""
    ).fetchall()
    return any(
        is_secondary_product_witness_raw(
            {
                "source": row[0],
                "source_ref": row[1],
                "raw_content": row[2],
                "content_hash": row[3],
                "metadata_json": row[4],
            }
        )
        for row in rows
    )


def _privacy_casefold(value: Any) -> str:
    return unicodedata.normalize(
        "NFC",
        unicodedata.normalize("NFC", str(value)).casefold(),
    )


def _bounded_export_json_object(value: Any, *, packed: bool = False) -> tuple[str, dict[str, Any]] | None:
    """Decode one historical JSON object without turning parse failure into egress.

    ``knowledge_object_versions`` may contain the zlib-packed representation;
    entity and merge histories are plain text.  Nothing from the rejected value
    is logged or included in the export.
    """

    try:
        if packed and isinstance(value, bytes):
            if len(value) > _EXPORT_HISTORY_JSON_MAX_CHARS:
                return None
            if value.startswith(_PACKED_KNOWLEDGE_SNAPSHOT_MAGIC):
                decompressor = zlib.decompressobj()
                raw = decompressor.decompress(
                    value[len(_PACKED_KNOWLEDGE_SNAPSHOT_MAGIC) :],
                    _EXPORT_HISTORY_JSON_MAX_CHARS + 1,
                )
                if (
                    len(raw) > _EXPORT_HISTORY_JSON_MAX_CHARS
                    or decompressor.unconsumed_tail
                    or not decompressor.eof
                ):
                    return None
                text = raw.decode("utf-8")
            else:
                text = value.decode("utf-8")
        else:
            text = str(value or "")
    except (UnicodeError, ValueError, OSError, zlib.error):
        return None
    if not text or len(text) > _EXPORT_HISTORY_JSON_MAX_CHARS:
        return None
    try:
        decoded = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    return text, decoded


def _bounded_export_text(value: Any, *, max_bytes: int) -> bool:
    try:
        return len(str(value or "").encode("utf-8")) <= max(1, int(max_bytes))
    except UnicodeError:
        return False


def _bounded_export_json_shape(
    value: Any,
    *,
    expected_type: type[dict] | type[list],
    max_bytes: int,
    reject_nested_json: bool = True,
    reject_only_valid_nested_json: bool = False,
) -> bool:
    """Mirror public-view JSON shape checks without applying global person scope."""

    if not isinstance(value, (str, bytes)):
        return False
    try:
        raw = value if isinstance(value, bytes) else value.encode("utf-8")
        if len(raw) > max(1, int(max_bytes)):
            return False
        decoded = json.loads(raw)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return False
    if not isinstance(decoded, expected_type):
        return False
    if not reject_nested_json:
        return True

    def nested_json_text(item: object) -> bool:
        if not isinstance(item, str) or not item.lstrip().startswith(("{", "[", '"')):
            return False
        if not reject_only_valid_nested_json:
            return True
        try:
            json.loads(item)
        except (TypeError, ValueError, RecursionError):
            return False
        return True

    pending = [decoded]
    visited = 0
    while pending:
        visited += 1
        if visited > max(1, int(max_bytes)):
            return False
        item = pending.pop()
        if isinstance(item, dict):
            for key, nested in item.items():
                if nested_json_text(str(key)):
                    return False
                pending.append(nested)
        elif isinstance(item, list):
            pending.extend(item)
        elif nested_json_text(item):
            return False
    return True


def _export_entity_material_shape_is_valid(row: dict[str, Any]) -> bool:
    return (
        _bounded_export_text(row.get("name"), max_bytes=_EXPORT_CURRENT_FIELD_MAX_BYTES)
        and _bounded_export_text(row.get("description"), max_bytes=_EXPORT_CURRENT_FIELD_MAX_BYTES)
        and _bounded_export_json_shape(
            row.get("aliases_json"),
            expected_type=list,
            max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
        )
        and _bounded_export_json_shape(
            row.get("metadata_json"),
            expected_type=dict,
            max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
        )
    )


def _export_raw_material_shape_is_valid(row: dict[str, Any]) -> bool:
    return (
        _bounded_export_text(row.get("raw_content"), max_bytes=_EXPORT_CURRENT_BODY_MAX_BYTES)
        and _bounded_export_text(row.get("source_ref"), max_bytes=_EXPORT_CURRENT_FIELD_MAX_BYTES)
        and _bounded_export_text(row.get("source"), max_bytes=_EXPORT_CURRENT_FIELD_MAX_BYTES)
        and _bounded_export_text(row.get("content_type"), max_bytes=_EXPORT_CURRENT_FIELD_MAX_BYTES)
        and _bounded_export_json_shape(
            row.get("metadata_json"),
            expected_type=dict,
            max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
        )
    )


def _export_knowledge_material_shape_is_valid(row: dict[str, Any]) -> bool:
    return (
        _bounded_export_text(row.get("content"), max_bytes=_EXPORT_CURRENT_BODY_MAX_BYTES)
        and _bounded_export_text(row.get("title"), max_bytes=_EXPORT_CURRENT_FIELD_MAX_BYTES)
        and _bounded_export_text(row.get("summary"), max_bytes=_EXPORT_CURRENT_FIELD_MAX_BYTES)
        and _bounded_export_text(row.get("content_type"), max_bytes=_EXPORT_CURRENT_FIELD_MAX_BYTES)
        and _bounded_export_text(row.get("knowledge_kind"), max_bytes=_EXPORT_CURRENT_FIELD_MAX_BYTES)
        and _bounded_export_json_shape(
            row.get("tags_json"),
            expected_type=list,
            max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
        )
        and _bounded_export_json_shape(
            row.get("metadata_json"),
            expected_type=dict,
            max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
        )
    )


def _export_inbox_material_shape_is_valid(row: dict[str, Any]) -> bool:
    return (
        _bounded_export_json_shape(
            row.get("suggestions_json"),
            expected_type=dict,
            max_bytes=_EXPORT_INBOX_JSON_MAX_BYTES,
            reject_only_valid_nested_json=True,
        )
        and _bounded_export_json_shape(
            row.get("suggested_tags_json"),
            expected_type=list,
            max_bytes=_EXPORT_INBOX_JSON_MAX_BYTES,
            reject_nested_json=False,
        )
        and len(str(row.get("classification_notes") or "")) <= _EXPORT_INBOX_NOTES_MAX_CHARS
    )


class MaintenanceMixin(StorageShared):
    def list_purgeable_knowledge(
        self,
        user_id: str | None = None,
        *,
        older_than_days: int = 30,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Soft-deleted objects whose retention window has elapsed and may be purged."""
        cutoff = f"-{max(0, int(older_than_days))} days"
        bounded = max(1, min(int(limit), 2000))
        base = (
            "SELECT k.id, k.user_id, k.title, k.deleted_at FROM knowledge_objects k "
            "WHERE k.deleted_at IS NOT NULL AND datetime(k.deleted_at) <= datetime('now', ?) "
            f"AND {_not_private_knowledge_dependency('k')}"  # nosec B608 - code-owned predicate
        )
        if user_id:
            rows = self.execute(
                f"{base} AND k.user_id=? ORDER BY k.deleted_at LIMIT ?",
                (cutoff, user_id, bounded),
            ).fetchall()
        else:
            rows = self.execute(
                f"{base} ORDER BY k.deleted_at LIMIT ?",
                (cutoff, bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def purge_knowledge_object(
        self,
        ko_id: str,
        user_id: str | None = None,
        *,
        require_soft_deleted: bool = True,
    ) -> dict[str, Any]:
        """Irreversibly hard-delete one Knowledge Object and every trace of it.

        Child rows are removed in foreign-key dependency order (versions, entity
        links, usage, embeddings, inbox, conflicts, feedback); dangling supersession
        pointers are nulled; then the object itself is deleted, and its ``AFTER
        DELETE`` trigger removes the FTS entry automatically. The backing Raw Object
        (and its on-disk file, reported to the caller) is removed only when no other
        object or inbox row still references it, and never when the content-addressed
        file is shared by another Raw Object. This is the one place the system
        intentionally destroys provenance, so it refuses anything not already
        soft-deleted unless explicitly overridden.
        """
        deleted: dict[str, int] = {}
        raw_removed = False
        raw_file_path = ""
        unlink_file = False
        # The guardrail is re-read INSIDE the write transaction. It used to be
        # checked on a snapshot taken before the lock, and nothing in the
        # transaction looked at `deleted_at` again — so an object restored between
        # the check and `BEGIN IMMEDIATE` was hard-deleted anyway, together with
        # its versions, its Raw Object, its file and its vault note. This is the
        # one place the system destroys provenance on purpose; the window where it
        # does so by accident must not exist. `transaction()` is reentrant, so the
        # read simply moves inside it.
        with self.transaction() as conn:
            current = self.get_knowledge_object(ko_id, user_id)
            if not current:
                return {"existed": False, "knowledge_object_id": ko_id}
            owner = str(current["user_id"])
            if require_soft_deleted and not current.get("deleted_at"):
                raise ValueError("Knowledge object must be soft-deleted before it can be purged")
            raw_object_id = str(current.get("raw_object_id") or "")

            def _del(table: str, where: str, params: tuple[Any, ...]) -> None:
                cursor = conn.execute(f"DELETE FROM {table} WHERE {where}", params)  # nosec B608
                if cursor.rowcount:
                    deleted[table] = deleted.get(table, 0) + cursor.rowcount

            # Chunk rows first: an orphan here fails PRAGMA foreign_key_check, which
            # makes create_backup delete its own backup and raise, so the first
            # symptom would be "backups stopped working", not a write error.
            _del("knowledge_chunk_embeddings", "knowledge_object_id=? AND user_id=?", (ko_id, owner))
            _del("knowledge_embeddings", "knowledge_object_id=? AND user_id=?", (ko_id, owner))
            _del("knowledge_usage", "knowledge_object_id=? AND user_id=?", (ko_id, owner))
            _del("knowledge_entity_links", "knowledge_object_id=? AND user_id=?", (ko_id, owner))
            _del(
                "knowledge_conflicts",
                "user_id=? AND (knowledge_a_id=? OR knowledge_b_id=?)",
                (owner, ko_id, ko_id),
            )
            _del("knowledge_object_versions", "knowledge_object_id=? AND user_id=?", (ko_id, owner))
            _del("inbox", "knowledge_object_id=? AND user_id=?", (ko_id, owner))
            # feedback_state references feedback(id), so it must go first.
            _del("feedback_state", "target_id=? AND user_id=?", (ko_id, owner))
            _del("feedback", "target_id=? AND user_id=?", (ko_id, owner))
            conn.execute(
                "UPDATE knowledge_objects SET superseded_by_id=NULL WHERE user_id=? AND superseded_by_id=?",
                (owner, ko_id),
            )
            cursor = conn.execute("DELETE FROM knowledge_objects WHERE id=? AND user_id=?", (ko_id, owner))
            deleted["knowledge_objects"] = cursor.rowcount

            if raw_object_id:
                still_used = (
                    conn.execute(
                        "SELECT 1 FROM knowledge_objects WHERE raw_object_id=? LIMIT 1",
                        (raw_object_id,),
                    ).fetchone()
                    or conn.execute(
                        "SELECT 1 FROM inbox WHERE raw_object_id=? LIMIT 1",
                        (raw_object_id,),
                    ).fetchone()
                )
                if not still_used:
                    raw_row = conn.execute(
                        "SELECT metadata_json, content_type, content_hash FROM raw_objects "
                        "WHERE id=? AND user_id=?",
                        (raw_object_id, owner),
                    ).fetchone()
                    if raw_row is not None:
                        metadata = _json_load(raw_row["metadata_json"], {})
                        raw_file_path = str(metadata.get("stored_path") or "")
                        if str(raw_row["content_type"]) == "file" and raw_file_path:
                            shared = conn.execute(
                                "SELECT 1 FROM raw_objects WHERE user_id=? AND content_type='file' "
                                "AND content_hash=? AND id<>? LIMIT 1",
                                (owner, str(raw_row["content_hash"] or ""), raw_object_id),
                            ).fetchone()
                            unlink_file = shared is None
                        # Installations upgraded from before source aliases
                        # existed do not have ON DELETE CASCADE on this table.
                        # Remove the transport bindings in the same transaction
                        # before destroying their immutable Raw target.
                        _del(
                            "file_source_aliases",
                            "raw_object_id=? AND user_id=?",
                            (raw_object_id, owner),
                        )
                        _del("document_catalog", "raw_object_id=?", (raw_object_id,))
                        conn.execute(
                            "DELETE FROM raw_objects WHERE id=? AND user_id=?",
                            (raw_object_id, owner),
                        )
                        deleted["raw_objects"] = 1
                        raw_removed = True
        return {
            "existed": True,
            "knowledge_object_id": ko_id,
            "user_id": owner,
            "title": str(current.get("title") or ""),
            "raw_object_id": raw_object_id,
            "raw_removed": raw_removed,
            "raw_file_path": raw_file_path,
            "unlink_file": unlink_file,
            "deleted": deleted,
        }

    def _verify_backup_conn(self, backup_conn: sqlite3.Connection) -> tuple[str, list[Any], int]:
        """Integrity / foreign-key / schema check of a backup copy.

        Runs entirely against the backup's own connection, independent of the
        live per-thread connections, so a backup never freezes concurrent
        requests during the full-DB scan.
        """
        register_document_catalog_connection_functions(backup_conn)
        integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = backup_conn.execute("PRAGMA foreign_key_check").fetchall()
        schema_row = backup_conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        backup_schema_version = int(schema_row[0]) if schema_row else -1
        from friday.interaction_control_plane.engineer_work_item_schema import (
            validate_engineer_work_item_schema,
        )
        from friday.interaction_control_plane.work_item_schema import (
            validate_work_item_schema,
        )

        validate_work_item_schema(backup_conn)
        validate_engineer_work_item_schema(backup_conn)
        validate_document_catalog_schema(backup_conn)
        if _contains_secondary_product_witness(backup_conn):
            raise RuntimeError("Backup snapshot contains a transient secondary product witness")
        if _engineer_command_backup_authority_required(
            self.settings
        ) and not _main_engineer_delivery_is_quiescent(backup_conn):
            raise RuntimeError("Backup snapshot contains unresolved Engineer delivery")
        return integrity, foreign_key_violations, backup_schema_version

    def _required_engineer_backup_authority(self) -> Any | None:
        if not _engineer_command_backup_authority_required(self.settings):
            return None
        authority = self._engineer_command_backup_authority
        if authority is None:
            raise RuntimeError("Engineer command backup authority is unavailable")
        return authority

    def _verify_engineer_backup_authority(
        self,
        evidence: object,
        *,
        database_sha256: str,
    ) -> tuple[str, int, bool] | None:
        required = _engineer_command_backup_authority_required(self.settings)
        if evidence is None and not required:
            return None
        if evidence is None:
            raise RuntimeError("Engineer command backup authority evidence is missing")
        if not isinstance(evidence, dict) or set(evidence) != _ENGINEER_BACKUP_AUTHORITY_FIELDS:
            raise RuntimeError("Engineer command backup authority evidence is invalid")
        authority = self._engineer_command_backup_authority
        if authority is None:
            raise RuntimeError("Engineer command backup authority is unavailable")
        try:
            verified = authority.verify_main_database_backup_authority(
                evidence,
                database_sha256,
            )
        except Exception as exc:
            raise RuntimeError("Engineer command backup authority does not match") from exc
        return _engineer_backup_snapshot(verified)

    def create_backup(self, *, label: str = "manual") -> dict[str, Any]:
        boundary = ProcessLease(
            self.settings.state_dir / SECONDARY_PRODUCT_BACKUP_LEASE_FILENAME,
            protocol=SECONDARY_PRODUCT_BACKUP_LEASE_PROTOCOL,
        )
        try:
            boundary.acquire()
        except (OSError, RuntimeLeaseError) as exc:
            raise RuntimeError("Backup is blocked by the secondary product witness boundary") from exc
        try:
            authority = self._required_engineer_backup_authority()
            authority_before = (
                _engineer_backup_snapshot(authority.backup_authority_snapshot())
                if authority is not None
                else None
            )
            if authority is not None and not _main_engineer_delivery_is_quiescent(self.conn):
                raise RuntimeError("Backup is blocked by unresolved Engineer delivery")
            # The lease is acquired before even creating a destination.  A committed
            # probe makes the source preflight fail, while a backup that got here first
            # prevents reserved ingest until the manifest is durable.  OS ownership
            # releases on process death, so no copied crash artefact can contain a probe.
            if _contains_secondary_product_witness(self.conn):
                raise RuntimeError("Backup source contains a transient secondary product witness")
            ensure_private_directory(self.settings.backups_dir)
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            stem = f"jericho-{timestamp}-{_safe_filename(label)}"
            destination = self.settings.backups_dir / f"{stem}.sqlite3"
            suffix = 1
            while destination.exists():
                destination = self.settings.backups_dir / f"{stem}-{suffix}.sqlite3"
                suffix += 1

            prepare_private_sqlite(destination)
            backup_conn = sqlite3.connect(str(destination))
            restrict_sqlite_files(destination)
            try:
                # Checkpoint + copy run on this thread's own connection. SQLite's online
                # backup API takes a transactionally consistent snapshot and restarts if
                # a concurrent writer modifies the source, so no cross-thread lock is
                # needed; a PASSIVE checkpoint never blocks other connections. The
                # integrity/foreign-key/schema scan then runs on the backup copy.
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                self.conn.backup(backup_conn)
                integrity, foreign_key_violations, backup_schema_version = self._verify_backup_conn(
                    backup_conn
                )
            except BaseException:
                for candidate in (
                    destination,
                    Path(f"{destination}-wal"),
                    Path(f"{destination}-shm"),
                ):
                    candidate.unlink(missing_ok=True)
                raise
            finally:
                backup_conn.close()
                restrict_sqlite_files(destination)
            if integrity != "ok":
                destination.unlink(missing_ok=True)
                raise RuntimeError(f"Backup integrity check failed: {integrity}")
            if foreign_key_violations:
                destination.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Backup foreign-key check failed: {len(foreign_key_violations)} violation(s)"
                )
            if backup_schema_version != SCHEMA_VERSION:
                destination.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Backup schema mismatch: database={backup_schema_version}, expected={SCHEMA_VERSION}"
                )
            if authority is not None and not _main_engineer_delivery_is_quiescent(self.conn):
                destination.unlink(missing_ok=True)
                raise RuntimeError("Backup is blocked by unresolved Engineer delivery")

            _chmod_private(destination)
            digest = _sha256_file(destination)
            authority_evidence: dict[str, Any] | None = None
            if authority is not None:
                try:
                    authority_after = _engineer_backup_snapshot(authority.backup_authority_snapshot())
                    authority_evidence = authority.attest_main_database_backup(digest)
                    authority_attested = _engineer_backup_evidence_identity(authority_evidence)
                    authority_verified = _engineer_backup_snapshot(
                        authority.verify_main_database_backup_authority(
                            authority_evidence,
                            digest,
                        )
                    )
                    if not (authority_before == authority_after == authority_attested == authority_verified):
                        raise RuntimeError("Engineer command authority changed during the database backup")
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
            manifest = {
                "schema_version": backup_schema_version,
                "created_at": utc_now(),
                "label": label,
                "database": destination.name,
                "size_bytes": destination.stat().st_size,
                "sha256": digest,
                "integrity_check": integrity,
                "foreign_key_violations": 0,
                **(
                    {"engineer_command_ledger_authority": authority_evidence}
                    if authority_evidence is not None
                    else {}
                ),
                # A database backup is transactionally consistent, but binary raw
                # files and the Markdown vault deliberately remain separate so an
                # operator cannot mistake this for a full installation backup.
                # The Engineer command kernel owns an independent, monotonic
                # authority ledger.  This closed scope is verified byte-for-
                # meaning on restore; a manifest may not relabel the ordinary
                # main-DB image as a complete effect backup.
                "scope": dict(_BACKUP_SCOPE),
            }
            manifest_path = destination.with_suffix(".manifest.json")
            _write_json_atomic(manifest_path, manifest)
            return {**manifest, "path": str(destination), "manifest_path": str(manifest_path)}
        finally:
            boundary.release()

    def prune_backups(self, *, keep: int) -> dict[str, Any]:
        """Delete all but the ``keep`` newest verified backups. ``keep <= 0`` is off.

        A daily backup that is never removed is a disk that fills: the schedule adds
        a full copy of the database every 24 hours and nothing in the codebase ever
        took one away. Retention was the missing half of the backup story, not a
        nicety — the failure mode is the machine running out of space, which takes
        the live instance down along with the backups.

        Only complete ``(database, manifest)`` pairs are eligible. A database without
        a manifest is left alone: that is the shape of an interrupted mirror write,
        and deleting it would destroy evidence rather than reclaim space.

        «Verified» в этой строке долго было обещанием без проверки: тело считало
        только дату из имени. Воспроизведено — испорченная страница в НОВЕЙШЕЙ
        копии, исправная старая, `prune_backups(keep=1)` удалял исправную и
        оставлял битую. Незаметность полная: доктор читает первый валидный
        манифест и печатает «Latest backup: verified». Порча носителя на свежей
        копии означала, что суточный воркер сам удаляет последнюю годную.

        Поэтому счёт `keep` ведётся по копиям, ПРОШЕДШИМ проверку, а битые
        удаляются сверх этого счёта — место они занимают, а восстановиться из них
        нельзя. Единственная копия не трогается никогда, даже не пройдя проверку:
        битый архив лучше, чем никакого, и удалять последнее свидетельство —
        решение человека, а не суточного воркера.
        """
        keep = int(keep)
        if keep <= 0:
            return {"enabled": False, "removed": 0, "kept": 0}
        # `list_backups` is already newest-first (the stem carries a sortable UTC
        # timestamp) and already refuses symlinks and paths outside backups_dir.
        backups = self.list_backups()
        healthy: list[dict[str, Any]] = []
        broken: list[dict[str, Any]] = []
        for entry in backups:
            name = Path(str(entry.get("path") or "")).name
            try:
                ok = bool(self.verify_backup(name).get("ok"))
            except (OSError, FileNotFoundError, ValueError) as exc:
                LOGGER.warning("Could not verify backup (%s)", type(exc).__name__)
                ok = False
            (healthy if ok else broken).append(entry)
        doomed = healthy[keep:] + broken
        if len(backups) - len(doomed) < 1 and backups:
            # Ни одной копии не остаётся — оставляем новейшую, какой бы она ни была.
            keeper = backups[0]
            doomed = [entry for entry in doomed if entry is not keeper]
            LOGGER.warning("Every backup failed verification; keeping the newest one anyway")
        if broken:
            LOGGER.warning("Backup verification failed for %d copy/copies", len(broken))
        removed: list[str] = []
        for entry in doomed:
            database = Path(str(entry.get("path") or ""))
            manifest = Path(str(entry.get("manifest_path") or ""))
            try:
                # -wal/-shm can be left beside a copy taken from a live database.
                for path in (
                    database,
                    database.with_name(database.name + "-wal"),
                    database.with_name(database.name + "-shm"),
                    manifest,
                ):
                    path.unlink(missing_ok=True)
            except OSError as exc:  # a locked or read-only file must not fail the tick
                LOGGER.warning("Could not prune backup (%s)", type(exc).__name__)
                continue
            removed.append(database.name)
        if removed:
            LOGGER.info("Pruned %d backup(s), keeping the newest %d", len(removed), keep)
        return {
            "enabled": True,
            "removed": len(removed),
            "kept": len(backups) - len(removed),
            # Сколько копий не прошли проверку — это то, о чём владелец должен
            # узнать раньше, чем в день восстановления.
            "unverified": len(broken),
        }

    def list_backups(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return newest valid manifests, optionally stopping after a bounded page.

        The overview needs five cards, not a parse of every retained manifest.  A
        caller which omits ``limit`` keeps the complete operator-facing inventory.
        Invalid manifests do not consume the limit: it bounds returned, usable
        entries rather than filenames inspected.
        """
        ensure_private_directory(self.settings.backups_dir)
        wanted = None if limit is None else max(0, int(limit))
        if wanted == 0:
            return []
        results: list[dict[str, Any]] = []
        for manifest_path in sorted(self.settings.backups_dir.glob("*.manifest.json"), reverse=True):
            try:
                if manifest_path.is_symlink():
                    continue
                data = json.loads(
                    manifest_path.read_text(encoding="utf-8"),
                    object_pairs_hook=_closed_json_object,
                )
                if not isinstance(data, dict):
                    continue
                database_name = str(data["database"])
                if Path(database_name).name != database_name or not database_name.endswith(".sqlite3"):
                    continue
                database_candidate = self.settings.backups_dir / database_name
                if database_candidate.is_symlink():
                    continue
                database_path = database_candidate.resolve()
                if database_path.parent != self.settings.backups_dir.resolve():
                    continue
                data["path"] = str(database_path)
                data["manifest_path"] = str(manifest_path)
                results.append(data)
                if wanted is not None and len(results) >= wanted:
                    break
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue
        return results

    def verify_backup(self, filename: str) -> dict[str, Any]:
        requested_name = str(filename or "")
        safe_name = Path(requested_name).name
        if (
            not requested_name
            or requested_name != safe_name
            or "/" in requested_name
            or "\\" in requested_name
        ):
            raise FileNotFoundError("Backup filename must not contain path components")
        candidate = self.settings.backups_dir / safe_name
        if candidate.is_symlink():
            raise FileNotFoundError("Backup symlinks are not allowed")
        path = candidate.resolve()
        if (
            path.parent != self.settings.backups_dir.resolve()
            or path.suffix.casefold() != ".sqlite3"
            or not path.is_file()
        ):
            raise FileNotFoundError("Backup not found")
        integrity = "error"
        database_error: str | None = None
        database_schema_version: int | None = None
        database_schema_supported = False
        foreign_key_violations: int | None = None
        conn = sqlite3.connect(str(path))
        try:
            register_document_catalog_connection_functions(conn)
            conn.execute("PRAGMA query_only=ON")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
            schema_row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if schema_row is None:
                database_error = "Database does not contain a schema_version marker"
            else:
                try:
                    database_schema_version = int(schema_row[0])
                    database_schema_supported = 0 <= database_schema_version <= SCHEMA_VERSION
                    if database_schema_version == SCHEMA_VERSION:
                        from friday.interaction_control_plane.engineer_work_item_schema import (
                            validate_engineer_work_item_schema,
                        )
                        from friday.interaction_control_plane.work_item_schema import (
                            validate_work_item_schema,
                        )

                        validate_work_item_schema(conn)
                        validate_engineer_work_item_schema(conn)
                        validate_document_catalog_schema(conn)
                except (TypeError, ValueError):
                    database_error = "Database schema_version marker is invalid"
        except sqlite3.DatabaseError as exc:
            database_error = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()
        actual_size = path.stat().st_size
        digest = _sha256_file(path)
        manifest_path = path.with_suffix(".manifest.json")
        expected_digest: str | None = None
        manifest_error: str | None = None
        manifest_database_matches: bool | None = None
        manifest_size_matches: bool | None = None
        manifest_schema_supported: bool | None = None
        manifest_schema_matches_database: bool | None = None
        manifest_scope_matches: bool | None = None
        engineer_authority_matches: bool | None = None
        engineer_command_store_id: str | None = None
        engineer_command_authority_sequence: int | None = None
        engineer_authority_required = _engineer_command_backup_authority_required(self.settings)
        manifest_present = manifest_path.is_file() and not manifest_path.is_symlink()
        if manifest_path.is_symlink():
            manifest_error = "Manifest symlinks are not allowed"
        elif manifest_present:
            try:
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8"),
                    object_pairs_hook=_closed_json_object,
                )
                if not isinstance(manifest, dict):
                    raise ValueError("Manifest root must be an object")
                expected_digest = str(manifest.get("sha256") or "") or None
                if expected_digest is None:
                    manifest_error = "Manifest does not contain sha256"
                manifest_database_matches = str(manifest.get("database") or "") == path.name
                manifest_size_value = manifest.get("size_bytes")
                try:
                    if manifest_size_value is None or isinstance(manifest_size_value, bool):
                        raise TypeError
                    manifest_size_matches = int(manifest_size_value) == actual_size
                except (TypeError, ValueError):
                    manifest_size_matches = False
                manifest_schema_value = manifest.get("schema_version")
                try:
                    if manifest_schema_value is None or isinstance(manifest_schema_value, bool):
                        raise TypeError
                    manifest_schema = int(manifest_schema_value)
                    manifest_schema_supported = 0 <= manifest_schema <= SCHEMA_VERSION
                    manifest_schema_matches_database = (
                        database_schema_version is not None and manifest_schema == database_schema_version
                    )
                except (TypeError, ValueError):
                    manifest_schema_supported = False
                    manifest_schema_matches_database = False
                validation_errors: list[str] = []
                if not manifest_database_matches:
                    validation_errors.append("database filename does not match")
                if not manifest_size_matches:
                    validation_errors.append("size_bytes does not match")
                if not manifest_schema_supported:
                    validation_errors.append("schema_version is unsupported")
                elif not manifest_schema_matches_database:
                    validation_errors.append("schema_version does not match the database")
                manifest_scope_matches = manifest.get("scope") == _BACKUP_SCOPE
                if not manifest_scope_matches:
                    validation_errors.append("backup scope is missing or invalid")
                authority_evidence = manifest.get("engineer_command_ledger_authority")
                engineer_authority_required = engineer_authority_required or authority_evidence is not None
                if engineer_authority_required:
                    try:
                        authority_identity = self._verify_engineer_backup_authority(
                            authority_evidence,
                            database_sha256=digest,
                        )
                        if authority_identity is None:
                            raise RuntimeError("Engineer command backup authority evidence is missing")
                        (
                            engineer_command_store_id,
                            engineer_command_authority_sequence,
                            _authority_quiescent,
                        ) = authority_identity
                        engineer_authority_matches = True
                    except RuntimeError as exc:
                        engineer_authority_matches = False
                        validation_errors.append(str(exc))
                if validation_errors:
                    manifest_error = "; ".join(validation_errors)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                manifest_error = f"{type(exc).__name__}: {exc}"
        else:
            manifest_error = "Manifest is missing"
        hash_matches_manifest = bool(expected_digest and hmac.compare_digest(digest, expected_digest))
        return {
            "database": path.name,
            "size_bytes": actual_size,
            "sha256": digest,
            "integrity_check": integrity,
            "database_schema_version": database_schema_version,
            "database_schema_supported": database_schema_supported,
            "foreign_key_violations": foreign_key_violations,
            "database_error": database_error,
            "manifest_present": manifest_present,
            "hash_matches_manifest": hash_matches_manifest if manifest_present else None,
            "manifest_database_matches": manifest_database_matches,
            "manifest_size_matches": manifest_size_matches,
            "manifest_schema_supported": manifest_schema_supported,
            "manifest_schema_matches_database": manifest_schema_matches_database,
            "manifest_scope_matches": manifest_scope_matches,
            "engineer_command_authority_matches": engineer_authority_matches,
            "engineer_command_store_id": engineer_command_store_id,
            "engineer_command_authority_sequence": engineer_command_authority_sequence,
            "manifest_error": manifest_error,
            "ok": (
                integrity == "ok"
                and database_error is None
                and database_schema_supported
                and foreign_key_violations == 0
                and manifest_present
                and manifest_error is None
                and hash_matches_manifest
                and manifest_database_matches is True
                and manifest_size_matches is True
                and manifest_schema_supported is True
                and manifest_schema_matches_database is True
                and manifest_scope_matches is True
                and (
                    engineer_authority_matches is True
                    if engineer_authority_required
                    else engineer_authority_matches is None
                )
            ),
        }

    def restore_backup(self, filename: str, *, safety_label: str = "pre-restore") -> dict[str, Any]:
        """Atomically restore a verified SQLite backup while Friday is stopped.

        The Telegram bridge lease is held for the complete verification and
        replacement window.  Merely observing that the bridge looks stopped is
        racy: it could dequeue or ACK a terminal notification immediately after
        the observation and make an older main database publish it again.
        """

        bridge_boundary = ProcessLease(
            self.settings.state_dir / "telegram-inbox.sqlite3.lock",
            protocol="friday.telegram-bridge.v1",
        )
        try:
            bridge_boundary.acquire()
        except (OSError, RuntimeLeaseError) as exc:
            raise RuntimeError("Database restore requires the Telegram bridge to be stopped") from exc
        try:
            # Freeze every managed writer for the complete preflight/snapshot/
            # replacement window.  In particular, the raw DB/WAL/SHM rollback
            # set below must describe one byte-stable generation while live
            # connections are still open.
            with self._write_lock:
                return self._restore_backup_with_stopped_bridge(
                    filename,
                    safety_label=safety_label,
                )
        finally:
            bridge_boundary.release()

    def _restore_backup_with_stopped_bridge(
        self,
        filename: str,
        *,
        safety_label: str,
    ) -> dict[str, Any]:
        """Restore under an already-held Telegram bridge process boundary.

        The current process must own the exclusive backend lease.  The exact
        active DB/WAL/SHM bytes are staged first for automatic rollback.  A
        verified online safety backup is created when the active database can be
        opened; otherwise an explicitly unverified recovery bundle preserves the
        original files instead of making a destructive restore unavoidable.

        Only the SQLite knowledge database is restored.  Content-addressed
        files, Markdown vault, Telegram queue, the monotonic Engineer command
        ledger, model weights and secrets remain external, matching the backup
        manifest.
        """

        from friday.diagnostics.runtime_lease import process_owns_lease

        lease_path = self.settings.state_dir / "backend.lock"
        if not process_owns_lease(lease_path, protocol="friday.backend.v1"):
            raise RuntimeError(
                "Database restore requires the exclusive backend process lease; "
                "stop Friday and use `jericho restore-backup ... --yes`"
            )

        database_path = self._db_path.absolute()
        intent_path = _restore_intent_path(self.settings)
        # A previous process may have died between durable intent and commit.
        # Resolve that transaction before opening, verifying, or migrating the
        # possibly half-replaced active image.  An ordinary restore with no
        # marker deliberately keeps the connections open: closing the final WAL
        # connection can checkpoint/delete sidecars and is itself an active-file
        # mutation, so it may happen only after this restore has a durable exact
        # recovery intent.
        if database_restore_intent_lstat(intent_path) is not None:
            self.close()
            _recover_interrupted_restore(
                self.settings,
                database_path,
                engineer_authority=self._engineer_command_backup_authority,
            )

        verification = self.verify_backup(filename)
        if not verification.get("ok"):
            problems = [
                str(verification.get(key))
                for key in ("database_error", "manifest_error", "integrity_check")
                if verification.get(key) not in (None, "", "ok")
            ]
            detail = "; ".join(problems) or "backup verification failed"
            raise RuntimeError(f"Refusing to restore unverified backup: {detail}")

        backup_name = str(verification["database"])
        backup_path = (self.settings.backups_dir / backup_name).resolve()
        active_paths = _active_restore_paths(database_path)
        _private_regular_files(active_paths)

        staged: Path | None = None
        staged_identity: _RestoreFileIdentity | None = None
        pre_restore_stages: dict[Path, Path] = {}
        safety_backup: dict[str, Any] | None = None
        recovery_snapshot: dict[str, Any] | None = None
        recovery_bundle: dict[str, Any] | None = None
        recovery_bundle_identity: tuple[int, int] | None = None
        intent_payload: dict[str, Any] | None = None
        loaded_prepared_intent: _LoadedRestoreIntent | None = None
        restore_authority_evidence: dict[str, Any] | None = None
        prepared_marker_written = False
        commit_marker_write_started = False
        committed_marker_written = False
        committed_generation: dict[Path, _RestoreFileIdentity | None] | None = None
        result: dict[str, Any] | None = None
        restore_open_previous = self._begin_database_restore_open()
        try:
            manifest_path = backup_path.with_suffix(".manifest.json")
            try:
                restore_manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8"),
                    object_pairs_hook=_closed_json_object,
                )
                if not isinstance(restore_manifest, dict):
                    raise ValueError("Manifest root must be an object")
                raw_restore_authority_evidence = restore_manifest.get("engineer_command_ledger_authority")
                restore_authority = self._verify_engineer_backup_authority(
                    raw_restore_authority_evidence,
                    database_sha256=str(verification["sha256"]),
                )
                verified_authority = (
                    str(verification.get("engineer_command_store_id") or ""),
                    verification.get("engineer_command_authority_sequence"),
                    True,
                )
                if restore_authority is not None and restore_authority != verified_authority:
                    raise RuntimeError("Engineer command backup authority changed before restore")
                if restore_authority is not None:
                    if not isinstance(raw_restore_authority_evidence, dict):
                        raise RuntimeError("Engineer command backup authority evidence is invalid")
                    restore_authority_evidence = dict(raw_restore_authority_evidence)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError("Engineer command backup authority could not be revalidated") from exc

            staged = _stage_private_copy(backup_path, database_path)
            staged_identity = _verify_exact_restore_copy(
                staged,
                size_bytes=int(verification["size_bytes"]),
                sha256=str(verification["sha256"]),
                error_message="Backup changed while it was staged for restore",
            )

            # Build the *complete* rollback set before mutation.  A failure on
            # the second or third copy leaves every original byte untouched;
            # the partial temporary set is never treated as rollback authority.
            active_originals = _private_regular_files(active_paths)
            pre_intent_identities = {path: _restore_regular_file_identity(path) for path in active_originals}
            pre_intent_digests = {
                path: _sha256_private_restore_file(
                    path,
                    expected_identity=pre_intent_identities[path],
                )
                for path in active_originals
            }
            for active_path in active_originals:
                pre_restore_stages[active_path] = _stage_private_copy(
                    active_path,
                    active_path,
                )
            if set(pre_restore_stages) != set(active_originals):
                raise RuntimeError("Could not stage a complete restore rollback set")
            for active_path, prepared in pre_restore_stages.items():
                _verify_exact_restore_copy(
                    prepared,
                    size_bytes=pre_intent_identities[active_path][2],
                    sha256=pre_intent_digests[active_path],
                    error_message="Restore rollback stage does not match the active database",
                )
            if set(_private_regular_files(active_paths)) != set(active_originals) or any(
                _restore_regular_file_identity(path) != pre_intent_identities[path]
                or not hmac.compare_digest(
                    _sha256_private_restore_file(
                        path,
                        expected_identity=pre_intent_identities[path],
                    ),
                    pre_intent_digests[path],
                )
                for path in active_originals
            ):
                raise RuntimeError("Active database changed during restore staging")

            recovery_bundle = _durable_recovery_bundle(
                self.settings,
                pre_restore_stages,
                label=safety_label,
                reason_type="RestoreTransaction",
            )
            recovery_path = Path(str(recovery_bundle["path"]))
            recovery_status = recovery_path.lstat()
            if (
                not stat.S_ISDIR(recovery_status.st_mode)
                or stat.S_ISLNK(recovery_status.st_mode)
                or recovery_status.st_uid != os.geteuid()
                or recovery_status.st_mode & 0o077
            ):
                raise RuntimeError("Restore recovery path changed before intent")
            recovery_bundle_identity = (
                int(recovery_status.st_dev),
                int(recovery_status.st_ino),
            )
            if set(_private_regular_files(active_paths)) != set(active_originals) or any(
                _restore_regular_file_identity(path) != pre_intent_identities[path]
                or not hmac.compare_digest(
                    _sha256_private_restore_file(
                        path,
                        expected_identity=pre_intent_identities[path],
                    ),
                    pre_intent_digests[path],
                )
                for path in active_originals
            ):
                raise RuntimeError("Active database changed before restore intent")
            for prepared in pre_restore_stages.values():
                prepared.unlink(missing_ok=True)
            pre_restore_stages.clear()
            if active_originals:
                recovery_snapshot = recovery_bundle

            recovery_manifest = Path(str(recovery_bundle["manifest_path"]))
            intent_payload = {
                "created_at": utc_now(),
                "database_path": str(database_path),
                "engineer_command_ledger_authority": restore_authority_evidence,
                "original_files": [path.name for path in active_originals],
                "phase": "prepared",
                "recovery_manifest_sha256": _sha256_private_restore_file(recovery_manifest),
                "recovery_path": str(Path(str(recovery_bundle["path"])).absolute()),
                "retain_recovery": recovery_snapshot is not None,
                "schema": _RESTORE_INTENT_SCHEMA,
                "target_database": backup_name,
                "target_sha256": str(verification["sha256"]),
            }
            _write_restore_intent(intent_path, intent_payload)
            prepared_marker_written = True
            loaded_prepared_intent = _load_restore_intent(
                self.settings,
                database_path,
            )
            if loaded_prepared_intent.intent != intent_payload:
                raise RuntimeError("Restore intent changed after preparation")

            _verify_restore_intent_authority(
                self.settings,
                self._engineer_command_backup_authority,
                restore_authority_evidence,
                database_sha256=str(verification["sha256"]),
            )

            # The target image proves its own delivery state was quiescent, but
            # replacing the current main DB can orphan a newer carrier.  This
            # check may have to open SQLite and create/update WAL sidecars; run
            # it only after the exact recovery intent is durable.  A failed
            # safety backup is not a substitute for this proof.
            if (
                _engineer_command_backup_authority_required(self.settings)
                and database_path.is_file()
                and not _main_engineer_delivery_is_quiescent(self.conn)
            ):
                raise RuntimeError("Database restore is blocked by unresolved current Engineer delivery")

            # create_backup() performs a PASSIVE WAL checkpoint.  That changes
            # active DB bytes even when a later backup step fails, so it may run
            # only after the exact raw bundle and prepared intent are durable.
            # Any failure from here is handled by ordinary intent recovery.
            if database_path.is_file():
                try:
                    safety_backup = self.create_backup(label=safety_label)
                except BaseException:
                    safety_backup = None
                if safety_backup is not None and recovery_snapshot is not None:
                    recovery_snapshot = None
                    intent_payload = {**intent_payload, "retain_recovery": False}
                    _write_restore_intent(intent_path, intent_payload)
                    loaded_prepared_intent = _load_restore_intent(
                        self.settings,
                        database_path,
                    )
                    if loaded_prepared_intent.intent != intent_payload:
                        raise RuntimeError("Restore intent changed after safety backup")

            # Closing the last SQLite connection can checkpoint the WAL or
            # remove sidecars.  It is now covered by a durable prepared intent.
            self.close()
            mutation_originals = _private_regular_files(active_paths)
            active_identities = {path: _restore_regular_file_identity(path) for path in mutation_originals}

            # Nothing after the durable prepared marker may trust the earlier
            # pathname observations.  Re-hash the target stage and compare every
            # active inode immediately before the first mutation.
            final_staged_identity = _verify_exact_restore_copy(
                staged,
                size_bytes=int(verification["size_bytes"]),
                sha256=str(verification["sha256"]),
                error_message="Backup changed while it was staged for restore",
            )
            if final_staged_identity != staged_identity:
                raise RuntimeError("Backup changed while it was staged for restore")
            if loaded_prepared_intent is None:
                raise RuntimeError("Restore intent was not validated")
            loaded_prepared_intent = _reload_exact_restore_intent(
                self.settings,
                database_path,
                loaded_prepared_intent,
            )
            restore_authority_evidence = loaded_prepared_intent.intent.get(
                "engineer_command_ledger_authority"
            )
            _verify_restore_intent_authority(
                self.settings,
                self._engineer_command_backup_authority,
                restore_authority_evidence,
                database_sha256=str(verification["sha256"]),
            )
            mutation_staged_identity = _verify_exact_restore_copy(
                staged,
                size_bytes=int(verification["size_bytes"]),
                sha256=str(verification["sha256"]),
                error_message="Backup changed while it was staged for restore",
            )
            if mutation_staged_identity != final_staged_identity:
                raise RuntimeError("Backup changed while it was staged for restore")
            for active_path in active_paths[1:]:
                _unlink_expected_restore_path(
                    active_path,
                    active_identities.get(active_path),
                )
            replaced_identity = _replace_expected_restore_path(
                staged,
                database_path,
                prepared_identity=mutation_staged_identity,
                destination_identity=active_identities.get(database_path),
            )
            verified_target_identity = _verify_exact_restore_copy(
                database_path,
                size_bytes=int(verification["size_bytes"]),
                sha256=str(verification["sha256"]),
                error_message="Restored database changed during replacement",
            )
            if verified_target_identity != replaced_identity:
                raise RuntimeError("Restored database changed during replacement")
            if (
                _strict_fsync_file(
                    database_path,
                    expected_identity=verified_target_identity,
                )
                != verified_target_identity
            ):
                raise RuntimeError("Restored database changed before durability sync")
            _strict_fsync_directory(database_path.parent)

            # Opening performs only supported forward migrations.  Health must
            # pass before rollback snapshots are discarded.
            health = self.diagnostics()
            if not health.get("ok"):
                raise RuntimeError(
                    "Restored database failed health checks: "
                    f"integrity={health.get('integrity_check')}, "
                    f"foreign_keys={len(health.get('foreign_key_violations') or [])}"
                )
            # Telegram queue/files/vault are intentionally NOT rolled back with
            # SQLite.  A clean-history marker from the older snapshot therefore
            # cannot still prove that no external activity happened after it.
            # Invalidate all such proofs before certifying the restored database;
            # newly admin-created local accounts receive a fresh marker normally.
            with self.transaction() as conn:
                eligibility_cursor = conn.execute(
                    "DELETE FROM runtime_kv WHERE substr(key,1,?)=?",
                    (
                        len(ACCOUNT_DELETION_ELIGIBILITY_PREFIX),
                        ACCOUNT_DELETION_ELIGIBILITY_PREFIX,
                    ),
                )
                invalidated_deletion_eligibility = max(0, int(eligibility_cursor.rowcount))
            result = {
                "ok": True,
                "restored_from": backup_name,
                "restored_sha256": verification["sha256"],
                "source_schema_version": verification["database_schema_version"],
                "active_schema_version": health["schema_version"],
                "database_path": str(database_path),
                "safety_backup": safety_backup,
                "recovery_snapshot": recovery_snapshot,
                "scope": {
                    "sqlite_database": "restored",
                    "raw_files": "unchanged",
                    "memory_vault": "unchanged",
                    "obsidian_profiles_and_vaults": "unchanged",
                    "telegram_queue": "unchanged",
                    "engineer_command_ledger": "unchanged",
                    "model_weights": "unchanged",
                    "configuration_and_secrets": "unchanged",
                },
                "integrity_check": health["integrity_check"],
                "foreign_key_violations": len(health.get("foreign_key_violations") or []),
                "invalidated_deletion_eligibility": invalidated_deletion_eligibility,
            }
            # Commit the restore transaction only after SQLite health and the
            # post-restore mutation are durable.  A crash before this marker
            # rolls back; a crash after it keeps the restored generation.
            self.close()
            committed_files = _private_regular_files(active_paths)
            committed_file_identities = {
                active_path: _restore_regular_file_identity(active_path) for active_path in committed_files
            }
            committed_database_identity = committed_file_identities.get(database_path)
            if (
                committed_database_identity is None
                or committed_database_identity[:2] != replaced_identity[:2]
            ):
                raise RuntimeError("Restored database inode changed before commit")
            committed_generation = {
                active_path: committed_file_identities.get(active_path) for active_path in active_paths
            }
            for active_path, identity in committed_file_identities.items():
                if _strict_fsync_file(active_path, expected_identity=identity) != identity:
                    raise RuntimeError("Restored database generation changed before commit")
            _strict_fsync_directory(database_path.parent)
            _assert_restore_generation(
                committed_generation,
                error_message="Restored database generation changed before commit",
            )
            commit_marker_write_started = True
            committed_intent_payload = {**intent_payload, "phase": "committed"}
            _write_restore_intent(intent_path, committed_intent_payload)
            committed_marker_written = True
            committed_intent = _load_restore_intent(
                self.settings,
                database_path,
            )
            if committed_intent.intent != committed_intent_payload:
                raise RuntimeError("Restore intent changed after commit")
            _finalize_restore_intent(
                intent_path,
                outcome="committed",
                expected_identity=committed_intent.intent_identity,
                expected_generation=committed_generation,
            )
            if recovery_snapshot is None:
                _discard_restore_recovery(
                    Path(str(recovery_bundle["path"])),
                    expected_identity=recovery_bundle_identity,
                )
            return result
        except BaseException as restore_error:
            marker_status: os.stat_result | None = None
            rollback_error: BaseException | None = None
            recovery_outcome = "absent"
            observed_recovery_phase: str | None = None
            try:
                marker_status = database_restore_intent_lstat(intent_path)
                if marker_status is not None:
                    # A close/checkpoint is safe only because this marker names
                    # the durable exact pre-close generation used below.
                    self.close()
                    observed_recovery_phase = str(
                        _load_restore_intent(self.settings, database_path).intent["phase"]
                    )
                    recovery_outcome = _recover_interrupted_restore(
                        self.settings,
                        database_path,
                        engineer_authority=self._engineer_command_backup_authority,
                        expected_committed_generation=committed_generation,
                    )
                elif (
                    recovery_bundle is not None
                    and recovery_bundle_identity is not None
                    and not prepared_marker_written
                ):
                    # Preparation failed before the durable mutation boundary.
                    # Remove only this call's unreferenced recovery directory;
                    # the active DB/WAL/SHM were never closed or changed.
                    _discard_restore_recovery(
                        Path(str(recovery_bundle["path"])),
                        expected_identity=recovery_bundle_identity,
                    )
            except BaseException as exc:
                # The durable marker and recovery directory are deliberately
                # retained.  A later stopped restore can resume from the same
                # complete copies; never unlink the last surviving recovery.
                rollback_error = exc
            if (
                committed_marker_written
                or observed_recovery_phase == "committed"
                or recovery_outcome == "committed"
                or (
                    isinstance(restore_error, _RestoreIntentCleanupDurabilityError)
                    and restore_error.outcome == "committed"
                )
                or (
                    isinstance(rollback_error, _RestoreIntentCleanupDurabilityError)
                    and rollback_error.outcome == "committed"
                )
            ):
                if rollback_error is not None:
                    raise RuntimeError(
                        "Database restore committed, but durable commit cleanup is pending: "
                        f"cleanup={type(rollback_error).__name__}: {rollback_error}; "
                        f"intent={intent_path}"
                    ) from restore_error
                if recovery_outcome == "committed" and result is not None:
                    return {**result, "commit_cleanup_recovered": True}
                raise RuntimeError(
                    "Database restore committed, but restore-intent cleanup durability is "
                    f"uncertain: cleanup={type(restore_error).__name__}: {restore_error}; "
                    f"intent={intent_path}"
                ) from restore_error
            if (
                isinstance(rollback_error, _RestoreIntentCleanupDurabilityError)
                and rollback_error.outcome == "rolled_back"
            ):
                raise RuntimeError(
                    "Database restore failed; the exact previous database files were restored, "
                    "but restore-intent cleanup durability is uncertain: "
                    f"cleanup={type(rollback_error).__name__}: {rollback_error}; "
                    f"intent={intent_path}"
                ) from restore_error
            if commit_marker_write_started and rollback_error is not None:
                raise RuntimeError(
                    "Database restore reached its durable commit boundary, but the exact "
                    "commit/rollback state is unreadable; recovery is pending: "
                    f"recovery={type(rollback_error).__name__}: {rollback_error}; "
                    f"intent={intent_path}"
                ) from restore_error
            if rollback_error is not None:
                raise RuntimeError(
                    "Database restore failed and durable rollback is pending: "
                    f"restore={type(restore_error).__name__}: {restore_error}; "
                    f"rollback={type(rollback_error).__name__}: {rollback_error}; "
                    f"intent={intent_path}"
                ) from restore_error
            if recovery_outcome == "rolled_back":
                recovery = "the exact previous database files were restored automatically"
            elif prepared_marker_written:
                recovery = (
                    "the durable restore marker disappeared before recovery could certify either generation"
                )
            elif marker_status is None and database_path.is_file():
                recovery = "the restore never started and the active database was left untouched"
            else:
                recovery = "no database was present and none was created"
            raise RuntimeError(
                f"Database restore failed; {recovery}: {type(restore_error).__name__}: {restore_error}"
            ) from restore_error
        finally:
            self._end_database_restore_open(restore_open_previous)
            if staged is not None:
                staged.unlink(missing_ok=True)
            for prepared in pre_restore_stages.values():
                prepared.unlink(missing_ok=True)

    def export_user(self, user_id: str) -> dict[str, Any]:
        # One SQLite snapshot is part of the privacy boundary.  Reading entities,
        # then their owner marker and dependencies in unrelated autocommit reads
        # could combine a pre-quarantine entity with post-quarantine edges in one
        # permanent JSON artifact.
        conn = self.conn
        if conn.in_transaction:
            raise RuntimeError("User export cannot run inside another database transaction")
        conn.execute("BEGIN")
        path: Path | None = None
        try:
            user_row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if user_row is None:
                raise ValueError("User not found")
            user = dict(user_row)

            reminder_source = f"reminder:{user_id}"
            visible_entity_ids = {
                str(row["id"])
                for row in conn.execute(
                    """SELECT e.id FROM entities e
                         WHERE e.user_id=? AND (
                           (
                             NOT EXISTS (
                               SELECT 1 FROM private_entity_owners owner
                                WHERE owner.entity_id=e.id
                             )
                             AND NOT EXISTS (
                               SELECT 1 FROM entity_time et
                                WHERE et.entity_id=e.id
                                  AND et.source LIKE 'reminder:%'
                             )
                           )
                           OR (
                             EXISTS (
                               SELECT 1 FROM private_entity_owners owner
                                WHERE owner.entity_id=e.id
                                  AND owner.person_id=?
                                  AND owner.privacy_kind='reminder'
                             )
                             AND NOT EXISTS (
                               SELECT 1 FROM private_entity_owners owner
                                WHERE owner.entity_id=e.id
                                  AND (
                                    owner.person_id<>?
                                    OR owner.privacy_kind<>'reminder'
                                  )
                             )
                             AND EXISTS (
                               SELECT 1 FROM entity_time et
                                WHERE et.entity_id=e.id AND et.user_id=e.user_id
                                  AND et.source=?
                             )
                             AND NOT EXISTS (
                               SELECT 1 FROM entity_time et
                                WHERE et.entity_id=e.id AND et.user_id=e.user_id
                                  AND et.source LIKE 'reminder:%'
                                  AND et.source<>?
                             )
                           )
                         )""",
                    (user_id, user_id, user_id, reminder_source, reminder_source),
                ).fetchall()
            }

            table_queries = {
                # Personal account state is already stored by own_id, not by the
                # shared archive tenant.
                "obsidian_sync_profiles": (
                    "SELECT * FROM obsidian_sync_profiles WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_android_devices": (
                    "SELECT * FROM obsidian_android_devices WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_vaults": (
                    "SELECT * FROM obsidian_vaults WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_onboarding_sessions": (
                    "SELECT * FROM obsidian_onboarding_sessions WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_pairing_candidates": (
                    "SELECT * FROM obsidian_pairing_candidates WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_operations": (
                    "SELECT * FROM obsidian_operations WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_conflicts": (
                    "SELECT * FROM obsidian_conflicts WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_note_bindings": (
                    "SELECT * FROM obsidian_note_bindings WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_note_index": (
                    "SELECT * FROM obsidian_note_index WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_note_links": (
                    "SELECT * FROM obsidian_note_links WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_candidate_sets": (
                    "SELECT * FROM obsidian_candidate_sets WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_candidate_set_items": (
                    "SELECT * FROM obsidian_candidate_set_items WHERE user_id=?",
                    (user_id,),
                ),
                "obsidian_active_frames": (
                    "SELECT * FROM obsidian_active_frames WHERE user_id=?",
                    (user_id,),
                ),
                "user_identities": ("SELECT * FROM user_identities WHERE user_id=?", (user_id,)),
                "raw_objects": ("SELECT * FROM raw_objects WHERE user_id=?", (user_id,)),
                "knowledge_objects": ("SELECT * FROM knowledge_objects WHERE user_id=?", (user_id,)),
                "inbox": ("SELECT * FROM inbox WHERE user_id=?", (user_id,)),
                "entities": ("SELECT * FROM entities WHERE user_id=?", (user_id,)),
                "entity_time": ("SELECT * FROM entity_time WHERE user_id=?", (user_id,)),
                "private_entity_owners": (
                    """SELECT owner.* FROM private_entity_owners owner
                         JOIN entities e ON e.id=owner.entity_id
                        WHERE e.user_id=?""",
                    (user_id,),
                ),
                "knowledge_entity_links": (
                    "SELECT * FROM knowledge_entity_links WHERE user_id=?",
                    (user_id,),
                ),
                "relations": ("SELECT * FROM relations WHERE user_id=?", (user_id,)),
                "relation_revisions": (
                    "SELECT * FROM relation_revisions WHERE user_id=? ORDER BY event_seq",
                    (user_id,),
                ),
                "entity_resolution_candidates": (
                    "SELECT * FROM entity_resolution_candidates WHERE user_id=?",
                    (user_id,),
                ),
                "knowledge_object_versions": (
                    "SELECT * FROM knowledge_object_versions WHERE user_id=?",
                    (user_id,),
                ),
                "entity_versions": ("SELECT * FROM entity_versions WHERE user_id=?", (user_id,)),
                "entity_merge_history": (
                    "SELECT * FROM entity_merge_history WHERE user_id=?",
                    (user_id,),
                ),
                "user_permission_overrides": (
                    "SELECT * FROM user_permission_overrides WHERE user_id=?",
                    (user_id,),
                ),
                "feedback": ("SELECT * FROM feedback WHERE user_id=?", (user_id,)),
                "feedback_state": ("SELECT * FROM feedback_state WHERE user_id=?", (user_id,)),
                "knowledge_usage": ("SELECT * FROM knowledge_usage WHERE user_id=?", (user_id,)),
                "relation_candidates": (
                    "SELECT * FROM relation_candidates WHERE user_id=?",
                    (user_id,),
                ),
                "knowledge_conflicts": (
                    "SELECT * FROM knowledge_conflicts WHERE user_id=?",
                    (user_id,),
                ),
                "work_item_compare_current_file_web_graphs": (
                    "SELECT * FROM work_item_compare_current_file_web_graphs WHERE user_id=?",
                    (user_id,),
                ),
                "work_item_compare_current_file_web_restart_rebinds": (
                    "SELECT * FROM work_item_compare_current_file_web_restart_rebinds "
                    "WHERE graph_id IN (SELECT id FROM "
                    "work_item_compare_current_file_web_graphs WHERE user_id=?)",
                    (user_id,),
                ),
                "work_item_compare_current_file_web_restart_rebind_steps": (
                    "SELECT * FROM work_item_compare_current_file_web_restart_rebind_steps "
                    "WHERE graph_id IN (SELECT id FROM "
                    "work_item_compare_current_file_web_graphs WHERE user_id=?)",
                    (user_id,),
                ),
                "engineer_work_items": (
                    "SELECT * FROM engineer_work_items WHERE owner_id=?",
                    (user_id,),
                ),
                # Durable command fences contain only exact body-free identity
                # hashes.  Export their raw rows: unlike the live Work Item they
                # have no child material to reconstruct and intentionally survive
                # retirement of that Work Item until account erasure.
                "engineer_work_item_command_fences": (
                    """SELECT owner_id,idempotency_key,work_item_id,expected_revision,
                              step_ordinal,source_binding_sha256,command_digest,retired_at
                         FROM engineer_work_item_command_fences
                        WHERE owner_id=? ORDER BY retired_at,idempotency_key""",
                    (user_id,),
                ),
                "work_items": ("SELECT * FROM work_items WHERE user_id=?", (user_id,)),
                "conversations": ("SELECT * FROM conversations WHERE user_id=?", (user_id,)),
                "messages": ("SELECT * FROM messages WHERE user_id=?", (user_id,)),
                "channel_sessions": ("SELECT * FROM channel_sessions WHERE user_id=?", (user_id,)),
                "monitors": ("SELECT * FROM monitors WHERE user_id=?", (user_id,)),
                # Idempotency response bodies are an operational second copy of a
                # prior HTTP response and cannot be ownership-proven retroactively.
                "request_idempotency": (
                    """SELECT user_id, request_key, request_hash, state, created_at, updated_at
                         FROM request_idempotency WHERE user_id=? AND state='complete'""",
                    (user_id,),
                ),
                # Proposal 29 guarantees that every persisted audit scalar and
                # payload has already crossed the v2 content-free sink.
                "audit_log": ("SELECT * FROM audit_log WHERE user_id=?", (user_id,)),
            }
            rows_by_table = {
                key: [dict(row) for row in conn.execute(query, params).fetchall()]
                for key, (query, params) in table_queries.items()
            }
            allowed_monitor_creators = {user_id}
            if not self.settings.shared_archive:
                # Pre-created personal-install rows had no author column.  In a
                # shared archive that empty value proves no person's ownership
                # and therefore fails closed instead of exporting a private
                # query/chat destination into the tenant archive.
                allowed_monitor_creators.add("")
            rows_by_table["monitors"] = [
                row
                for row in rows_by_table["monitors"]
                if str(row.get("created_by") or "") in allowed_monitor_creators
            ]

            all_entity_ids = {str(row["id"]) for row in rows_by_table["entities"]}
            entities_by_id = {str(row["id"]): row for row in rows_by_table["entities"]}
            invalid_current_entity_ids = {
                entity_id
                for entity_id, row in entities_by_id.items()
                if not _export_entity_material_shape_is_valid(row)
            }
            visible_entity_ids.difference_update(invalid_current_entity_ids)
            identity_names_by_entity: dict[str, set[str]] = {entity_id: set() for entity_id in all_entity_ids}
            for row in conn.execute(
                """SELECT identity_token.id, identity_token.name
                     FROM private_entity_identity_tokens identity_token
                     JOIN entities identity_entity ON identity_entity.id=identity_token.id
                    WHERE identity_entity.user_id=?""",
                (user_id,),
            ).fetchall():
                entity_id = str(row["id"] or "")
                name = str(row["name"] or "")
                if entity_id in identity_names_by_entity and name:
                    identity_names_by_entity[entity_id].add(name)

            # A public current row is not enough to establish public history.
            # Version snapshots are full copies and may still point at (or carry
            # the old text of) an entity quarantined after the version was made.
            # Parse every parent as a unit, validate its tenant/identity, then
            # close merged_into references until the visible set stops shrinking.
            parsed_entity_versions: dict[str, tuple[str, dict[str, Any]]] = {}
            invalid_version_entity_ids: set[str] = set()
            for row in rows_by_table["entity_versions"]:
                parent_id = str(row.get("entity_id") or "")
                parsed = _bounded_export_json_object(row.get("snapshot_json"))
                if parent_id not in all_entity_ids or parsed is None:
                    if parent_id:
                        invalid_version_entity_ids.add(parent_id)
                    continue
                text, snapshot = parsed
                material_fields_valid = all(
                    isinstance(snapshot.get(field), str)
                    for field in ("name", "description", "aliases_json", "metadata_json")
                )
                material_json_valid = (
                    material_fields_valid
                    and _bounded_export_json_shape(
                        snapshot.get("aliases_json"),
                        expected_type=list,
                        max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
                    )
                    and _bounded_export_json_shape(
                        snapshot.get("metadata_json"),
                        expected_type=dict,
                        max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
                    )
                )
                if (
                    str(snapshot.get("id") or "") != parent_id
                    or str(snapshot.get("user_id") or "") != user_id
                    or type(snapshot.get("version")) is not int
                    or int(snapshot["version"]) != int(row.get("version") or 0)
                    or not material_fields_valid
                    or not material_json_valid
                ):
                    invalid_version_entity_ids.add(parent_id)
                    continue
                parsed_entity_versions[str(row["id"])] = (text, snapshot)

            visible_entity_ids.difference_update(invalid_version_entity_ids)
            while True:
                before = len(visible_entity_ids)
                for entity_id in tuple(visible_entity_ids):
                    current_target = str(entities_by_id[entity_id].get("merged_into_id") or "")
                    if current_target and current_target not in visible_entity_ids:
                        visible_entity_ids.discard(entity_id)
                for row in rows_by_table["entity_versions"]:
                    parent_id = str(row.get("entity_id") or "")
                    parsed = parsed_entity_versions.get(str(row.get("id") or ""))
                    if parent_id not in visible_entity_ids or parsed is None:
                        continue
                    historical_target = str(parsed[1].get("merged_into_id") or "")
                    if historical_target and historical_target not in visible_entity_ids:
                        visible_entity_ids.discard(parent_id)
                if len(visible_entity_ids) == before:
                    break

            hidden_private_tokens: set[str] = set()
            hidden_private_names: set[str] = set()
            hidden_private_folded_names: set[str] = set()

            def add_hidden_entity_identity_tokens(entity_ids: set[str]) -> None:
                """Extend the fixed point with every authenticated identity of hidden rows."""

                hidden_private_tokens.update(entity_ids)
                names = {
                    name
                    for entity_id in entity_ids
                    for name in (
                        identity_names_by_entity.get(entity_id, set())
                        | {str(entities_by_id.get(entity_id, {}).get("name") or "")}
                    )
                    if name
                }
                hidden_private_tokens.update(names)
                hidden_private_names.update(names)
                hidden_private_folded_names.update(_privacy_casefold(name) for name in names)

            hidden_entity_ids = all_entity_ids - visible_entity_ids
            add_hidden_entity_identity_tokens(hidden_entity_ids)
            # Export has a person-specific exception that the generic graph does
            # not: exactly this person's valid private reminder may leave in their
            # own archive.  Compute a fresh global fixed point from every OTHER
            # direct private seed.  Starting from direct rows only would stop at a
            # foreign carrier (Bob -> Charlie -> Alice); subtracting this person's
            # id from the global cache would wrongly retain carriers derived solely
            # from their own permitted reminder.  Invalid current/history material
            # is seeded unconditionally by the shared helper and has no exception.
            direct_private = _not_private_reminder_entity("closure_seed_entity")
            disallowed_seed_predicate = f"""NOT ({direct_private}) AND NOT (
                EXISTS (
                  SELECT 1 FROM private_entity_owners owner
                   WHERE owner.entity_id=closure_seed_entity.id
                     AND owner.person_id=?
                     AND owner.privacy_kind='reminder'
                )
                AND NOT EXISTS (
                  SELECT 1 FROM private_entity_owners owner
                   WHERE owner.entity_id=closure_seed_entity.id
                     AND (owner.person_id<>? OR owner.privacy_kind<>'reminder')
                )
                AND EXISTS (
                  SELECT 1 FROM entity_time et
                   WHERE et.entity_id=closure_seed_entity.id
                     AND et.user_id=closure_seed_entity.user_id
                     AND et.source=?
                )
                AND NOT EXISTS (
                  SELECT 1 FROM entity_time et
                   WHERE et.entity_id=closure_seed_entity.id
                     AND et.user_id=closure_seed_entity.user_id
                     AND et.source LIKE 'reminder:%'
                     AND et.source<>?
                )
            )"""
            disallowed_material_rows = conn.execute(
                _private_entity_material_seeded_query(disallowed_seed_predicate),
                (user_id, user_id, reminder_source, reminder_source),
            ).fetchall()
            disallowed_material_ids = {
                str(row["id"] or "") for row in disallowed_material_rows if str(row["id"] or "")
            }
            local_disallowed_material_ids = disallowed_material_ids & all_entity_ids
            visible_entity_ids.difference_update(local_disallowed_material_ids)
            add_hidden_entity_identity_tokens(local_disallowed_material_ids)
            hidden_private_tokens.update(
                token
                for row in disallowed_material_rows
                for token in (str(row["id"] or ""), str(row["name"] or ""))
                if token
            )
            hidden_private_names.update(
                str(row["name"] or "") for row in disallowed_material_rows if str(row["name"] or "")
            )
            hidden_private_folded_names.update(
                _privacy_casefold(row["name"] or "")
                for row in disallowed_material_rows
                if str(row["name"] or "")
            )

            def contains_hidden_private_material(*values: Any) -> bool:
                # Scan decoded JSON keys/values as well as raw text.  Exact
                # private Cyrillic/ASCII tokens can otherwise be hidden behind
                # JSON ``\uXXXX`` escapes and materialise only after the export
                # consumer calls json.loads.  Nested JSON strings occur in old
                # evidence, so inspect object/list strings iteratively too.
                if not hidden_private_tokens:
                    return False
                pending = list(values)
                visited = 0
                while pending:
                    visited += 1
                    if visited > 1_000_000:
                        return True
                    value = pending.pop()
                    if isinstance(value, dict):
                        pending.extend(value.keys())
                        pending.extend(value.values())
                        continue
                    if isinstance(value, (list, tuple)):
                        pending.extend(value)
                        continue
                    if value in (None, ""):
                        continue
                    text = str(value)
                    if len(text) > _EXPORT_HISTORY_JSON_MAX_CHARS:
                        return True
                    if any(token in text for token in hidden_private_tokens):
                        return True
                    if hidden_private_folded_names:
                        normalized_text = _privacy_casefold(text)
                        if any(name in normalized_text for name in hidden_private_folded_names):
                            return True
                    candidate = text.lstrip()
                    if not candidate.startswith(("{", "[", '"')):
                        continue
                    try:
                        decoded = json.loads(text)
                    except (TypeError, ValueError, RecursionError):
                        # A JSON-shaped evidence field that cannot be inspected
                        # cannot prove absence of escaped private material.
                        return True
                    if isinstance(decoded, (dict, list, str)) and decoded != text:
                        pending.append(decoded)
                return False

            # Private names/ids copied into an otherwise public entity's aliases,
            # description or historical snapshot make that entity dependent too.
            # Iterate because hiding it creates a new private token for any other
            # row that copied *its* material.
            while True:
                newly_hidden: set[str] = set()
                for entity_id in visible_entity_ids:
                    current = entities_by_id[entity_id]
                    if contains_hidden_private_material(
                        current.get("name"),
                        current.get("aliases_json"),
                        current.get("description"),
                        current.get("metadata_json"),
                    ):
                        newly_hidden.add(entity_id)
                        continue
                    for version_row in rows_by_table["entity_versions"]:
                        if str(version_row.get("entity_id") or "") != entity_id:
                            continue
                        parsed = parsed_entity_versions.get(str(version_row.get("id") or ""))
                        if parsed is None or contains_hidden_private_material(
                            parsed[1].get("name"),
                            parsed[1].get("aliases_json"),
                            parsed[1].get("description"),
                            parsed[1].get("metadata_json"),
                        ):
                            newly_hidden.add(entity_id)
                            break
                for entity_id in visible_entity_ids - newly_hidden:
                    current_target = str(entities_by_id[entity_id].get("merged_into_id") or "")
                    historical_targets = {
                        str(parsed_entity_versions[str(row["id"])][1].get("merged_into_id") or "")
                        for row in rows_by_table["entity_versions"]
                        if str(row.get("entity_id") or "") == entity_id
                        and str(row.get("id") or "") in parsed_entity_versions
                    }
                    if (current_target and current_target not in visible_entity_ids - newly_hidden) or any(
                        target not in visible_entity_ids - newly_hidden
                        for target in historical_targets
                        if target
                    ):
                        newly_hidden.add(entity_id)
                if not newly_hidden:
                    break
                visible_entity_ids.difference_update(newly_hidden)
                add_hidden_entity_identity_tokens(newly_hidden)

            hidden_entity_ids = all_entity_ids - visible_entity_ids
            rows_by_table["entities"] = [
                row for row in rows_by_table["entities"] if str(row["id"]) in visible_entity_ids
            ]
            rows_by_table["entity_versions"] = [
                {
                    **row,
                    "snapshot_json": parsed_entity_versions[str(row["id"])][0],
                }
                for row in rows_by_table["entity_versions"]
                if str(row["entity_id"]) in visible_entity_ids and str(row["id"]) in parsed_entity_versions
            ]
            rows_by_table["entity_time"] = [
                row for row in rows_by_table["entity_time"] if str(row["entity_id"]) in visible_entity_ids
            ]
            rows_by_table["private_entity_owners"] = [
                row
                for row in rows_by_table["private_entity_owners"]
                if str(row["entity_id"]) in visible_entity_ids
                and str(row["person_id"]) == user_id
                and str(row["privacy_kind"]) == "reminder"
            ]

            # A Knowledge Object is excluded as a unit when any graph pointer can
            # reach a quarantined/missing entity.  Its raw source, versions and
            # dependent review rows form the same privacy closure.
            all_raw_ids = {str(row["id"]) for row in rows_by_table["raw_objects"]}
            raw_by_id = {str(row["id"]): row for row in rows_by_table["raw_objects"]}
            private_material_raw_ids = {
                raw_id
                for raw_id, row in raw_by_id.items()
                if is_secondary_product_witness_raw(row)
                or not _export_raw_material_shape_is_valid(row)
                or contains_hidden_private_material(
                    row.get("source_ref"),
                    row.get("raw_content"),
                    row.get("metadata_json"),
                )
            }
            knowledge_by_id = {str(row["id"]): row for row in rows_by_table["knowledge_objects"]}
            all_knowledge_ids = set(knowledge_by_id)
            parsed_knowledge_versions: dict[str, tuple[str, dict[str, Any]]] = {}
            historical_supersession: dict[str, set[str]] = {}
            hidden_knowledge_ids = {
                knowledge_id
                for knowledge_id, row in knowledge_by_id.items()
                if not _export_knowledge_material_shape_is_valid(row)
                or str(row.get("raw_object_id") or "") not in all_raw_ids
                or str(row.get("raw_object_id") or "") in private_material_raw_ids
                or (bool(row.get("entity_id")) and str(row["entity_id"]) not in visible_entity_ids)
                or contains_hidden_private_material(
                    row.get("content"),
                    row.get("title"),
                    row.get("summary"),
                    row.get("tags_json"),
                    row.get("metadata_json"),
                )
            }
            for row in rows_by_table["knowledge_object_versions"]:
                parent_id = str(row.get("knowledge_object_id") or "")
                parsed = _bounded_export_json_object(row.get("snapshot_json"), packed=True)
                if parent_id not in all_knowledge_ids or parsed is None:
                    if parent_id:
                        hidden_knowledge_ids.add(parent_id)
                    continue
                text, snapshot = parsed
                snapshot_identity = str(snapshot.get("id") or "")
                snapshot_user = str(snapshot.get("user_id") or "")
                current_raw_id = str(knowledge_by_id[parent_id].get("raw_object_id") or "")
                historical_raw_id = str(snapshot.get("raw_object_id") or "")
                historical_entity_id = str(snapshot.get("entity_id") or "")
                if (
                    snapshot_identity != parent_id
                    or snapshot_user != user_id
                    or not _export_knowledge_material_shape_is_valid(snapshot)
                    or historical_raw_id != current_raw_id
                    or historical_raw_id not in all_raw_ids
                    or (historical_entity_id and historical_entity_id not in visible_entity_ids)
                    or contains_hidden_private_material(
                        snapshot.get("content"),
                        snapshot.get("title"),
                        snapshot.get("summary"),
                        snapshot.get("tags_json"),
                        snapshot.get("metadata_json"),
                    )
                ):
                    hidden_knowledge_ids.add(parent_id)
                    continue
                superseded_by_id = str(snapshot.get("superseded_by_id") or "")
                if superseded_by_id:
                    historical_supersession.setdefault(parent_id, set()).add(superseded_by_id)
                parsed_knowledge_versions[str(row["id"])] = (text, snapshot)
            for link in rows_by_table["knowledge_entity_links"]:
                knowledge_id = str(link.get("knowledge_object_id") or "")
                if knowledge_id in all_knowledge_ids and (
                    str(link.get("entity_id") or "") not in visible_entity_ids
                    or not _bounded_export_json_shape(
                        link.get("evidence_json"),
                        expected_type=dict,
                        max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
                    )
                    or contains_hidden_private_material(link.get("evidence_json"))
                ):
                    hidden_knowledge_ids.add(knowledge_id)
            for inbox_row in rows_by_table["inbox"]:
                knowledge_id = str(inbox_row.get("knowledge_object_id") or "")
                suggested = str(inbox_row.get("suggested_entity_id") or "")
                if knowledge_id in all_knowledge_ids and (
                    not _export_inbox_material_shape_is_valid(inbox_row)
                    or (suggested and suggested not in visible_entity_ids)
                    or contains_hidden_private_material(
                        inbox_row.get("suggested_tags_json"),
                        inbox_row.get("suggestions_json"),
                        inbox_row.get("classification_notes"),
                    )
                ):
                    hidden_knowledge_ids.add(knowledge_id)

            excluded_inbox_ids: set[str] = set()
            hidden_raw_ids: set[str] = set(private_material_raw_ids)
            while True:
                previous = (
                    len(visible_entity_ids),
                    len(hidden_knowledge_ids),
                    len(excluded_inbox_ids),
                    len(hidden_raw_ids),
                )
                # Hidden graph ids become privacy tokens too.  Re-run the
                # arbitrary-payload checks until a fixed point: a public Inbox
                # row may copy a hidden KO id, its raw id may then be copied by
                # another KO, and an entity snapshot may in turn copy that id.
                # A single forward pass would export whichever table happened
                # to be filtered before the new token was discovered.
                hidden_private_tokens.update(
                    token for token in hidden_knowledge_ids | hidden_raw_ids | excluded_inbox_ids if token
                )

                newly_hidden_entities: set[str] = set()
                for entity_id in tuple(visible_entity_ids):
                    current = entities_by_id[entity_id]
                    if contains_hidden_private_material(
                        current.get("name"),
                        current.get("aliases_json"),
                        current.get("description"),
                        current.get("metadata_json"),
                    ):
                        newly_hidden_entities.add(entity_id)
                        continue
                    current_target = str(current.get("merged_into_id") or "")
                    if current_target and current_target not in visible_entity_ids:
                        newly_hidden_entities.add(entity_id)
                        continue
                    for version_row in rows_by_table["entity_versions"]:
                        if str(version_row.get("entity_id") or "") != entity_id:
                            continue
                        parsed = parsed_entity_versions.get(str(version_row.get("id") or ""))
                        if parsed is None:
                            newly_hidden_entities.add(entity_id)
                            break
                        snapshot = parsed[1]
                        historical_target = str(snapshot.get("merged_into_id") or "")
                        if (
                            historical_target and historical_target not in visible_entity_ids
                        ) or contains_hidden_private_material(
                            snapshot.get("name"),
                            snapshot.get("aliases_json"),
                            snapshot.get("description"),
                            snapshot.get("metadata_json"),
                        ):
                            newly_hidden_entities.add(entity_id)
                            break
                if newly_hidden_entities:
                    visible_entity_ids.difference_update(newly_hidden_entities)
                    add_hidden_entity_identity_tokens(newly_hidden_entities)

                for raw_id, raw_row in raw_by_id.items():
                    if contains_hidden_private_material(
                        raw_row.get("source_ref"),
                        raw_row.get("raw_content"),
                        raw_row.get("metadata_json"),
                    ):
                        hidden_raw_ids.add(raw_id)

                for knowledge_id, row in knowledge_by_id.items():
                    entity_id = str(row.get("entity_id") or "")
                    if (
                        entity_id and entity_id not in visible_entity_ids
                    ) or contains_hidden_private_material(
                        row.get("content"),
                        row.get("title"),
                        row.get("summary"),
                        row.get("tags_json"),
                        row.get("metadata_json"),
                    ):
                        hidden_knowledge_ids.add(knowledge_id)
                for version_row in rows_by_table["knowledge_object_versions"]:
                    parent_id = str(version_row.get("knowledge_object_id") or "")
                    parsed = parsed_knowledge_versions.get(str(version_row.get("id") or ""))
                    if parent_id not in all_knowledge_ids or parsed is None:
                        if parent_id:
                            hidden_knowledge_ids.add(parent_id)
                        continue
                    snapshot = parsed[1]
                    historical_entity_id = str(snapshot.get("entity_id") or "")
                    if (
                        historical_entity_id and historical_entity_id not in visible_entity_ids
                    ) or contains_hidden_private_material(
                        snapshot.get("content"),
                        snapshot.get("title"),
                        snapshot.get("summary"),
                        snapshot.get("tags_json"),
                        snapshot.get("metadata_json"),
                    ):
                        hidden_knowledge_ids.add(parent_id)
                for link in rows_by_table["knowledge_entity_links"]:
                    knowledge_id = str(link.get("knowledge_object_id") or "")
                    if knowledge_id in all_knowledge_ids and (
                        str(link.get("entity_id") or "") not in visible_entity_ids
                        or contains_hidden_private_material(link.get("evidence_json"))
                    ):
                        hidden_knowledge_ids.add(knowledge_id)

                hidden_raw_ids.update(
                    str(knowledge_by_id[knowledge_id].get("raw_object_id") or "")
                    for knowledge_id in hidden_knowledge_ids
                    if knowledge_id in knowledge_by_id
                )
                for inbox_row in rows_by_table["inbox"]:
                    inbox_id = str(inbox_row["id"])
                    raw_id = str(inbox_row.get("raw_object_id") or "")
                    knowledge_id = str(inbox_row.get("knowledge_object_id") or "")
                    suggested = str(inbox_row.get("suggested_entity_id") or "")
                    if (
                        not _export_inbox_material_shape_is_valid(inbox_row)
                        or raw_id not in all_raw_ids
                        or raw_id in hidden_raw_ids
                        or (knowledge_id and knowledge_id not in all_knowledge_ids)
                        or knowledge_id in hidden_knowledge_ids
                        or (suggested and suggested not in visible_entity_ids)
                        or contains_hidden_private_material(
                            inbox_row.get("suggested_tags_json"),
                            inbox_row.get("suggestions_json"),
                            inbox_row.get("classification_notes"),
                        )
                    ):
                        excluded_inbox_ids.add(inbox_id)
                        if raw_id:
                            hidden_raw_ids.add(raw_id)
                for knowledge_id, row in knowledge_by_id.items():
                    if str(row.get("raw_object_id") or "") in hidden_raw_ids:
                        hidden_knowledge_ids.add(knowledge_id)
                    superseded_by_id = str(row.get("superseded_by_id") or "")
                    if superseded_by_id and (
                        superseded_by_id not in all_knowledge_ids or superseded_by_id in hidden_knowledge_ids
                    ):
                        hidden_knowledge_ids.add(knowledge_id)
                    if any(
                        target not in all_knowledge_ids or target in hidden_knowledge_ids
                        for target in historical_supersession.get(knowledge_id, set())
                    ):
                        hidden_knowledge_ids.add(knowledge_id)
                if previous == (
                    len(visible_entity_ids),
                    len(hidden_knowledge_ids),
                    len(excluded_inbox_ids),
                    len(hidden_raw_ids),
                ):
                    break

            hidden_entity_ids = all_entity_ids - visible_entity_ids
            rows_by_table["entities"] = [
                row for row in rows_by_table["entities"] if str(row["id"]) in visible_entity_ids
            ]
            rows_by_table["entity_versions"] = [
                row for row in rows_by_table["entity_versions"] if str(row["entity_id"]) in visible_entity_ids
            ]
            rows_by_table["entity_time"] = [
                row for row in rows_by_table["entity_time"] if str(row["entity_id"]) in visible_entity_ids
            ]
            rows_by_table["private_entity_owners"] = [
                row
                for row in rows_by_table["private_entity_owners"]
                if str(row["entity_id"]) in visible_entity_ids
            ]
            visible_knowledge_ids = all_knowledge_ids - hidden_knowledge_ids
            visible_raw_ids = all_raw_ids - hidden_raw_ids
            rows_by_table["raw_objects"] = [
                row for row in rows_by_table["raw_objects"] if str(row["id"]) in visible_raw_ids
            ]
            rows_by_table["knowledge_objects"] = [
                {
                    **row,
                    "superseded_by_id": (
                        row.get("superseded_by_id")
                        if not row.get("superseded_by_id")
                        or str(row["superseded_by_id"]) in visible_knowledge_ids
                        else None
                    ),
                }
                for row in rows_by_table["knowledge_objects"]
                if str(row["id"]) in visible_knowledge_ids
            ]
            rows_by_table["inbox"] = [
                row
                for row in rows_by_table["inbox"]
                if str(row["id"]) not in excluded_inbox_ids
                and str(row.get("raw_object_id") or "") in visible_raw_ids
            ]
            visible_inbox_ids = {str(row["id"]) for row in rows_by_table["inbox"]}
            rows_by_table["knowledge_entity_links"] = [
                row
                for row in rows_by_table["knowledge_entity_links"]
                if str(row.get("knowledge_object_id") or "") in visible_knowledge_ids
                and str(row.get("entity_id") or "") in visible_entity_ids
            ]
            visible_link_ids = {str(row["id"]) for row in rows_by_table["knowledge_entity_links"]}
            rows_by_table["knowledge_object_versions"] = [
                {
                    **row,
                    "snapshot_json": parsed_knowledge_versions[str(row["id"])][0],
                }
                for row in rows_by_table["knowledge_object_versions"]
                if str(row.get("knowledge_object_id") or "") in visible_knowledge_ids
                and str(row.get("id") or "") in parsed_knowledge_versions
            ]
            rows_by_table["knowledge_usage"] = [
                row
                for row in rows_by_table["knowledge_usage"]
                if str(row.get("knowledge_object_id") or "") in visible_knowledge_ids
            ]
            rows_by_table["knowledge_conflicts"] = [
                row
                for row in rows_by_table["knowledge_conflicts"]
                if str(row.get("knowledge_a_id") or "") in visible_knowledge_ids
                and str(row.get("knowledge_b_id") or "") in visible_knowledge_ids
                and _bounded_export_json_shape(
                    row.get("evidence_json"),
                    expected_type=dict,
                    max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
                )
                and not contains_hidden_private_material(
                    row.get("evidence_json"),
                    row.get("resolution_note"),
                )
            ]

            hidden_private_tokens.update(hidden_knowledge_ids | hidden_raw_ids | excluded_inbox_ids)
            invalid_relation_ids = {
                str(row.get("id") or row.get("relation_id") or "")
                for table in ("relations", "relation_revisions")
                for row in rows_by_table[table]
                if str(row.get("source_entity_id") or "") not in visible_entity_ids
                or str(row.get("target_entity_id") or "") not in visible_entity_ids
                or not _bounded_export_json_shape(
                    row.get("metadata_json"),
                    expected_type=dict,
                    max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
                    reject_nested_json=False,
                )
                or contains_hidden_private_material(row.get("metadata_json"))
            }
            rows_by_table["relations"] = [
                row
                for row in rows_by_table["relations"]
                if str(row.get("id") or "") not in invalid_relation_ids
            ]
            rows_by_table["relation_revisions"] = [
                row
                for row in rows_by_table["relation_revisions"]
                if str(row.get("relation_id") or "") not in invalid_relation_ids
            ]
            visible_relation_ids = {
                *(str(row["id"]) for row in rows_by_table["relations"]),
                *(str(row["relation_id"]) for row in rows_by_table["relation_revisions"]),
            }
            for table in ("relations", "relation_revisions"):
                for row in rows_by_table[table]:
                    if row.get("superseded_by") and str(row["superseded_by"]) not in visible_relation_ids:
                        row["superseded_by"] = None

            rows_by_table["relation_candidates"] = [
                row
                for row in rows_by_table["relation_candidates"]
                if str(row.get("source_entity_id") or "") in visible_entity_ids
                and str(row.get("target_entity_id") or "") in visible_entity_ids
                and _bounded_export_json_shape(
                    row.get("evidence_json"),
                    expected_type=dict,
                    max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
                )
                and not contains_hidden_private_material(row.get("evidence_json"))
            ]
            visible_relation_candidate_ids = {str(row["id"]) for row in rows_by_table["relation_candidates"]}
            rows_by_table["entity_resolution_candidates"] = [
                row
                for row in rows_by_table["entity_resolution_candidates"]
                if str(row.get("entity_a_id") or "") in visible_entity_ids
                and str(row.get("entity_b_id") or "") in visible_entity_ids
                and _bounded_export_json_shape(
                    row.get("evidence_json"),
                    expected_type=dict,
                    max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
                )
                and not contains_hidden_private_material(row.get("evidence_json"))
            ]
            visible_resolution_ids = {str(row["id"]) for row in rows_by_table["entity_resolution_candidates"]}

            structural_entity_keys = {
                "entity_id",
                "source_entity_id",
                "target_entity_id",
                "entity_a_id",
                "entity_b_id",
                "merged_into_id",
                "suggested_entity_id",
            }
            structural_knowledge_keys = {
                "knowledge_object_id",
                "knowledge_a_id",
                "knowledge_b_id",
            }

            def merge_structure_is_visible(value: Any, *, key: str = "") -> bool:
                if isinstance(value, dict):
                    return all(
                        merge_structure_is_visible(item, key=str(item_key))
                        for item_key, item in value.items()
                    )
                if isinstance(value, list):
                    return all(merge_structure_is_visible(item, key=key) for item in value)
                if value in (None, ""):
                    return True
                text = str(value)
                if key in structural_entity_keys:
                    return text in visible_entity_ids
                if key in structural_knowledge_keys:
                    return text in visible_knowledge_ids
                if key == "primary_moved":
                    return text in visible_knowledge_ids
                if key == "raw_object_id":
                    return text in visible_raw_ids
                if key == "target_link_id":
                    return text in visible_link_ids
                if key in {"kept_relation_id", "relation_id", "before", "after"}:
                    return text in visible_relation_ids
                if key == "closed_candidates":
                    return text in visible_resolution_ids
                if key == "user_id":
                    return text == user_id
                if key == "source" and text.startswith("reminder:"):
                    return text == reminder_source
                return not contains_hidden_private_material(text)

            def merge_history_is_visible(row: dict[str, Any]) -> bool:
                source_id = str(row.get("source_entity_id") or "")
                target_id = str(row.get("target_entity_id") or "")
                if source_id not in visible_entity_ids or target_id not in visible_entity_ids:
                    return False
                parsed_fields: dict[str, tuple[str, dict[str, Any]]] = {}
                for field in (
                    "source_snapshot_json",
                    "target_before_json",
                    "target_after_json",
                    "transfer_json",
                ):
                    parsed = _bounded_export_json_object(row.get(field))
                    if parsed is None or contains_hidden_private_material(parsed[0]):
                        return False
                    parsed_fields[field] = parsed
                source_snapshot = parsed_fields["source_snapshot_json"][1]
                target_before = parsed_fields["target_before_json"][1]
                target_after = parsed_fields["target_after_json"][1]
                if (
                    str(source_snapshot.get("id") or "") != source_id
                    or str(source_snapshot.get("user_id") or "") != user_id
                ):
                    return False
                if any(
                    str(snapshot.get("id") or "") != target_id
                    or str(snapshot.get("user_id") or "") != user_id
                    for snapshot in (target_before, target_after)
                ):
                    return False
                if not all(merge_structure_is_visible(parsed[1]) for parsed in parsed_fields.values()):
                    return False
                for field, parsed in parsed_fields.items():
                    row[field] = parsed[0]
                return True

            rows_by_table["entity_merge_history"] = [
                row for row in rows_by_table["entity_merge_history"] if merge_history_is_visible(row)
            ]
            visible_merge_ids = {str(row["id"]) for row in rows_by_table["entity_merge_history"]}

            conversation_ids = {str(row["id"]) for row in rows_by_table["conversations"]}
            rows_by_table["messages"] = [
                row
                for row in rows_by_table["messages"]
                if str(row.get("conversation_id") or "") in conversation_ids
            ]
            message_ids = {str(row["id"]) for row in rows_by_table["messages"]}
            # Import the transaction-local validator only on this export path.
            # Keeping it out of storage package initialisation avoids coupling
            # schema bootstrap to the orchestration/store dependency graph.
            from friday.interaction_control_plane.archive_candidate_selection_store import (
                get_archive_candidate_selection_work_item_for_export_in_transaction,
            )
            from friday.interaction_control_plane.archive_evidence_work_item_store import (
                get_recall_selected_archive_evidence_work_item_for_export_in_transaction,
            )
            from friday.interaction_control_plane.compare_conversation_document_store import (
                get_compare_conversation_with_document_work_item_for_export_in_transaction,
            )
            from friday.interaction_control_plane.compare_current_file_web_work_graph_store import (
                get_compare_current_file_web_work_graph_in_transaction,
            )
            from friday.interaction_control_plane.engineer_work_item import (
                EngineerWorkItemChannel,
                get_engineer_work_item_in_transaction,
            )
            from friday.interaction_control_plane.work_item_store import (
                get_recall_conversation_work_item_for_export_in_transaction,
            )

            exported_engineer_work_items: list[dict[str, object]] = []
            for row in rows_by_table["engineer_work_items"]:
                try:
                    item = get_engineer_work_item_in_transaction(
                        conn,
                        work_item_id=str(row.get("id") or ""),
                        owner_id=user_id,
                        tenant_id=str(row.get("tenant_id") or ""),
                        conversation_id=str(row.get("conversation_id") or ""),
                        channel=EngineerWorkItemChannel(str(row.get("channel") or "")),
                    )
                except (TypeError, ValueError):
                    continue
                if item is not None and item.conversation_id in conversation_ids:
                    exported_engineer_work_items.append(item.to_payload())
            rows_by_table["engineer_work_items"] = exported_engineer_work_items

            exported_work_items: list[dict[str, object]] = []
            for row in rows_by_table["work_items"]:
                item_payload: dict[str, object] | None = None
                try:
                    if row.get("kind") == "select_archive_candidate_and_replay_evidence":
                        candidate_item = get_archive_candidate_selection_work_item_for_export_in_transaction(
                            conn,
                            work_item_id=str(row.get("id") or ""),
                            user_id=user_id,
                            conversation_id=str(row.get("conversation_id") or ""),
                        )
                        if candidate_item is not None and candidate_item.conversation_id in conversation_ids:
                            item_payload = candidate_item.to_payload()
                    elif row.get("kind") == "compare_conversation_with_document":
                        comparison_item = (
                            get_compare_conversation_with_document_work_item_for_export_in_transaction(
                                conn,
                                work_item_id=str(row.get("id") or ""),
                                user_id=user_id,
                                conversation_id=str(row.get("conversation_id") or ""),
                            )
                        )
                        if (
                            comparison_item is not None
                            and comparison_item.conversation_id in conversation_ids
                        ):
                            item_payload = comparison_item.to_payload()
                    elif row.get("kind") == "recall_selected_archive_evidence":
                        archive_item = (
                            get_recall_selected_archive_evidence_work_item_for_export_in_transaction(
                                conn,
                                work_item_id=str(row.get("id") or ""),
                                user_id=user_id,
                                conversation_id=str(row.get("conversation_id") or ""),
                            )
                        )
                        if archive_item is not None and archive_item.conversation_id in conversation_ids:
                            item_payload = archive_item.to_payload()
                    elif row.get("kind") == "recall_conversation":
                        recall_item = get_recall_conversation_work_item_for_export_in_transaction(
                            conn,
                            work_item_id=str(row.get("id") or ""),
                            user_id=user_id,
                            conversation_id=str(row.get("conversation_id") or ""),
                        )
                        if recall_item is not None and recall_item.conversation_id in conversation_ids:
                            item_payload = recall_item.to_payload()
                except (TypeError, ValueError):
                    continue
                if item_payload is not None:
                    exported_work_items.append(item_payload)
            rows_by_table["work_items"] = exported_work_items
            exported_graphs: list[dict[str, object]] = []
            for row in rows_by_table["work_item_compare_current_file_web_graphs"]:
                try:
                    graph = get_compare_current_file_web_work_graph_in_transaction(
                        conn,
                        graph_id=str(row.get("id") or ""),
                        user_id=user_id,
                        conversation_id=str(row.get("conversation_id") or ""),
                    )
                except (TypeError, ValueError):
                    continue
                if (
                    graph is not None
                    and graph.conversation_id in conversation_ids
                    and graph.current_file_raw_object_id in visible_raw_ids
                ):
                    exported_graphs.append(graph.payload())
            rows_by_table["work_item_compare_current_file_web_graphs"] = exported_graphs
            exported_graph_ids = {
                str(row.get("id") or "") for row in exported_graphs if isinstance(row, dict)
            }
            for history_table in (
                "work_item_compare_current_file_web_restart_rebinds",
                "work_item_compare_current_file_web_restart_rebind_steps",
            ):
                rows_by_table[history_table] = [
                    row
                    for row in rows_by_table[history_table]
                    if str(row.get("graph_id") or "") in exported_graph_ids
                ]
            rows_by_table["channel_sessions"] = [
                row
                for row in rows_by_table["channel_sessions"]
                if not row.get("conversation_id") or str(row["conversation_id"]) in conversation_ids
            ]

            hidden_target_ids = hidden_entity_ids | hidden_knowledge_ids | hidden_raw_ids | excluded_inbox_ids
            known_feedback_targets = {
                "answer": message_ids,
                "classification": visible_raw_ids,
                "entity": visible_entity_ids,
                "entity_resolution_candidate": visible_resolution_ids,
                "inbox": visible_inbox_ids,
                "knowledge_entity_link": visible_link_ids,
                "knowledge_object": visible_knowledge_ids,
                "merge": visible_merge_ids,
                "raw": visible_raw_ids,
                "raw_object": visible_raw_ids,
                "relation": visible_relation_ids,
                "relation_candidate": visible_relation_candidate_ids,
                "resolution": visible_resolution_ids,
            }
            known_feedback_types = {
                "answer_usefulness",
                "classification",
                "entity_link",
                "general",
                "search_quality",
            }

            def feedback_is_visible(row: dict[str, Any]) -> bool:
                target_id = str(row.get("target_id") or "")
                if not target_id or target_id in hidden_target_ids:
                    return False
                if not _bounded_export_json_shape(
                    row.get("context_json"),
                    expected_type=dict,
                    max_bytes=_EXPORT_CURRENT_JSON_MAX_BYTES,
                ):
                    return False
                if contains_hidden_private_material(*row.values()):
                    return False
                allowed = known_feedback_targets.get(str(row.get("target_type") or ""))
                return (
                    allowed is not None
                    and target_id in allowed
                    and str(row.get("feedback_type") or "") in known_feedback_types
                )

            rows_by_table["feedback"] = [row for row in rows_by_table["feedback"] if feedback_is_visible(row)]
            visible_feedback_ids = {str(row["id"]) for row in rows_by_table["feedback"]}
            rows_by_table["feedback_state"] = [
                row
                for row in rows_by_table["feedback_state"]
                if str(row.get("feedback_id") or "") in visible_feedback_ids and feedback_is_visible(row)
            ]
            rows_by_table["monitors"] = [
                row
                for row in rows_by_table["monitors"]
                if not contains_hidden_private_material(*row.values())
            ]

            payload: dict[str, Any] = {
                "format": "jericho-user-export-v3",
                "exported_at": utc_now(),
                "scope": {
                    "main_database_rows": "included",
                    # Work Items below contain only opaque receipt digests.  The
                    # authoritative command/job ledger and its output carriers
                    # are intentionally a separate operational store.
                    "engineer_command_ledger": "external",
                },
                "user": user,
                **rows_by_table,
            }
            ensure_private_directory(self.settings.exports_dir)
            identity_hash = hashlib.sha256(user_id.encode("utf-8", errors="replace")).hexdigest()[:12]
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            path = self.settings.exports_dir / (
                f"jericho-export-{_safe_filename(user_id)}--{identity_hash}-{timestamp}.json"
            )
            _write_json_atomic(path, payload)
            conn.commit()
            return {"path": str(path), "filename": path.name, "size_bytes": path.stat().st_size}
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            if path is not None:
                path.unlink(missing_ok=True)
            raise

    def diagnostics(self) -> dict[str, Any]:
        integrity = self.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [dict(row) for row in self.execute("PRAGMA foreign_key_check").fetchall()]

        def count(sql: str, params: tuple[Any, ...] = ()) -> int:
            row = self.execute(sql, params).fetchone()
            return int(row["count"] if row else 0)

        raw_public = _not_private_raw_dependency("diagnostic_raw")
        knowledge_public = _not_private_knowledge_dependency("diagnostic_knowledge")
        inbox_public = _not_private_inbox_dependency("diagnostic_inbox")
        entity_public = _not_private_entity_material_dependency("diagnostic_entity")
        relation_public = _not_private_relation_dependency("diagnostic_relation")
        relation_source_public = _not_private_entity_material_dependency("diagnostic_relation_source")
        relation_target_public = _not_private_entity_material_dependency("diagnostic_relation_target")
        candidate_public = _not_private_relation_candidate_dependency("diagnostic_candidate")
        candidate_source_public = _not_private_entity_material_dependency("diagnostic_candidate_source")
        candidate_target_public = _not_private_entity_material_dependency("diagnostic_candidate_target")
        conflict_a_public = _not_private_knowledge_dependency("diagnostic_conflict_a")
        conflict_b_public = _not_private_knowledge_dependency("diagnostic_conflict_b")
        usage_public = _not_private_knowledge_dependency("diagnostic_usage_knowledge")
        feedback_users = {
            str(row["user_id"])
            for row in self.execute(
                """SELECT user_id FROM feedback
                    UNION SELECT user_id FROM feedback_state"""
            ).fetchall()
        }
        counts: dict[str, int] = {
            "users": count("SELECT COUNT(*) AS count FROM users"),
            "raw_objects": count(
                f"""SELECT COUNT(*) AS count FROM raw_objects diagnostic_raw
                     WHERE {raw_public}"""  # nosec B608 - code-owned predicate
            ),
            "knowledge_objects": count(
                f"""SELECT COUNT(*) AS count FROM knowledge_objects diagnostic_knowledge
                     WHERE {knowledge_public}"""  # nosec B608
            ),
            "inbox": count(
                f"""SELECT COUNT(*) AS count FROM inbox diagnostic_inbox
                     WHERE {inbox_public}"""  # nosec B608
            ),
            "entities": count(
                f"""SELECT COUNT(*) AS count FROM entities diagnostic_entity
                     WHERE {entity_public}"""  # nosec B608
            ),
            "relations": count(
                f"""SELECT COUNT(*) AS count FROM relations diagnostic_relation
                     JOIN entities diagnostic_relation_source
                       ON diagnostic_relation_source.id=diagnostic_relation.source_entity_id
                      AND diagnostic_relation_source.user_id=diagnostic_relation.user_id
                      AND {relation_source_public}
                     JOIN entities diagnostic_relation_target
                       ON diagnostic_relation_target.id=diagnostic_relation.target_entity_id
                      AND diagnostic_relation_target.user_id=diagnostic_relation.user_id
                      AND {relation_target_public}
                     WHERE {relation_public}"""  # nosec B608
            ),
            "relation_candidates": count(
                f"""SELECT COUNT(*) AS count FROM relation_candidates diagnostic_candidate
                     JOIN entities diagnostic_candidate_source
                       ON diagnostic_candidate_source.id=diagnostic_candidate.source_entity_id
                      AND diagnostic_candidate_source.user_id=diagnostic_candidate.user_id
                      AND {candidate_source_public}
                     JOIN entities diagnostic_candidate_target
                       ON diagnostic_candidate_target.id=diagnostic_candidate.target_entity_id
                      AND diagnostic_candidate_target.user_id=diagnostic_candidate.user_id
                      AND {candidate_target_public}
                     WHERE {candidate_public}"""  # nosec B608
            ),
            "knowledge_conflicts": count(
                f"""SELECT COUNT(*) AS count FROM knowledge_conflicts diagnostic_conflict
                     JOIN knowledge_objects diagnostic_conflict_a
                       ON diagnostic_conflict_a.id=diagnostic_conflict.knowledge_a_id
                      AND diagnostic_conflict_a.user_id=diagnostic_conflict.user_id
                      AND {conflict_a_public}
                     JOIN knowledge_objects diagnostic_conflict_b
                       ON diagnostic_conflict_b.id=diagnostic_conflict.knowledge_b_id
                      AND diagnostic_conflict_b.user_id=diagnostic_conflict.user_id
                      AND {conflict_b_public}"""  # nosec B608
            ),
            "feedback": sum(
                int(bucket.get("count") or 0)
                for user_id in feedback_users
                for bucket in self.get_feedback_stats(user_id).values()
            ),
            "feedback_state": sum(self.count_feedback_state(user_id) for user_id in feedback_users),
            "knowledge_usage": count(
                f"""SELECT COUNT(*) AS count FROM knowledge_usage diagnostic_usage
                     JOIN knowledge_objects diagnostic_usage_knowledge
                       ON diagnostic_usage_knowledge.id=diagnostic_usage.knowledge_object_id
                      AND diagnostic_usage_knowledge.user_id=diagnostic_usage.user_id
                      AND {usage_public}"""  # nosec B608
            ),
            # These tables are personal, but the diagnostic exposes only physical
            # row counts and never graph/reminder-derived material.
            "conversations": count("SELECT COUNT(*) AS count FROM conversations"),
            "messages": count("SELECT COUNT(*) AS count FROM messages"),
            "channel_sessions": count("SELECT COUNT(*) AS count FROM channel_sessions"),
        }
        # Pending Inbox material is not knowledge yet: it cannot be found by search, so a
        # forgotten backlog is imported material the owner can no longer reach. Reported
        # here as well as in the offline `_database_status`, because this is the path a
        # running instance takes — and therefore the one Sentinel actually sees.
        backlog = self.execute(
            f"""SELECT COUNT(*) AS count, MIN(created_at) AS oldest
                  FROM inbox diagnostic_inbox
                 WHERE status='pending' AND {inbox_public}"""  # nosec B608
        ).fetchone()
        # Возраст важнее размера и здесь тоже: очередь наполняет backend, а
        # разгребает МОСТ. Мёртвый мост backend видел (count рос) и молчал —
        # застрявшее уведомление неотличимо от только что положенного без
        # отметки времени. Живой экземпляр ходит ЭТИМ путём, не `_database_status`.
        outbound_public = _not_private_notification_dependency("diagnostic_outbound")
        outbound_entity_public = _not_private_reminder_entity("diagnostic_outbound_entity")
        outbound = self.execute(
            f"""SELECT COUNT(*) AS count, MIN(diagnostic_outbound.created_at) AS oldest
                  FROM outbound_notifications diagnostic_outbound
                 WHERE diagnostic_outbound.status='pending'
                   AND {outbound_public}
                   AND NOT EXISTS (
                       SELECT 1 FROM entities diagnostic_outbound_entity
                        WHERE NOT ({outbound_entity_public})
                          AND (
                              instr(COALESCE(diagnostic_outbound.body,''),
                                    diagnostic_outbound_entity.id)>0
                              OR instr(COALESCE(diagnostic_outbound.dedup_key,''),
                                       diagnostic_outbound_entity.id)>0
                              OR (diagnostic_outbound_entity.name<>'' AND instr(
                                  jericho_casefold(COALESCE(diagnostic_outbound.body,'')),
                                  jericho_casefold(diagnostic_outbound_entity.name))>0)
                              OR (diagnostic_outbound_entity.name<>'' AND instr(
                                  jericho_casefold(COALESCE(diagnostic_outbound.dedup_key,'')),
                                  jericho_casefold(diagnostic_outbound_entity.name))>0)
                          )
                   )"""  # nosec B608
        ).fetchone()
        # Версии хранят ПОЛНЫЙ content в каждом снапшоте, чистки нет нигде:
        # массовое ре-обогащение добавляет копию корпуса в базу навсегда и
        # раздувает каждый из 14 суточных бэкапов. Пока рост хотя бы виден;
        # ретеншн (N полных + сжатие старых) — отдельная работа.
        version_public = _not_private_knowledge_dependency("diagnostic_version_knowledge")
        version_rows = self.execute(
            f"""SELECT diagnostic_version.snapshot_json AS snapshot_json,
                       LENGTH(diagnostic_version.snapshot_json) AS stored_bytes,
                       diagnostic_version.user_id AS version_user_id,
                       diagnostic_version_knowledge.id AS knowledge_object_id,
                       diagnostic_version_knowledge.raw_object_id AS raw_object_id
                  FROM knowledge_object_versions diagnostic_version
                  JOIN knowledge_objects diagnostic_version_knowledge
                    ON diagnostic_version_knowledge.id=diagnostic_version.knowledge_object_id
                   AND diagnostic_version_knowledge.user_id=diagnostic_version.user_id
                   AND {version_public}"""  # nosec B608
        )
        versions_count = 0
        versions_bytes = 0
        for version_row in version_rows:
            user_id = str(version_row["version_user_id"] or "")
            snapshot = _public_knowledge_version_snapshot(
                self,
                version_row["snapshot_json"],
                user_id=user_id,
                knowledge_object={
                    "id": str(version_row["knowledge_object_id"] or ""),
                    "user_id": user_id,
                    "raw_object_id": str(version_row["raw_object_id"] or ""),
                },
            )
            if snapshot is None:
                continue
            versions_count += 1
            versions_bytes += int(version_row["stored_bytes"] or 0)
        outbound_oldest_minutes: float | None = None
        if outbound and outbound["oldest"]:
            try:
                oldest_at = datetime.fromisoformat(str(outbound["oldest"]))
                if oldest_at.tzinfo is None:
                    oldest_at = oldest_at.replace(tzinfo=UTC)
                outbound_oldest_minutes = round((datetime.now(UTC) - oldest_at).total_seconds() / 60, 1)
            except (TypeError, ValueError):
                outbound_oldest_minutes = None
        return {
            "database_path": str(self._db_path),
            "database_size_bytes": self._db_path.stat().st_size if self._db_path.exists() else 0,
            "schema_version": SCHEMA_VERSION,
            "integrity_check": integrity,
            "foreign_key_violations": foreign_keys,
            "fts_available": self._fts_available,
            "counts": counts,
            "inbox_pending": int(backlog["count"] if backlog else 0),
            "inbox_oldest_pending_at": (str(backlog["oldest"]) if backlog and backlog["oldest"] else None),
            "outbound_pending": int(outbound["count"] if outbound else 0),
            "outbound_oldest_minutes": outbound_oldest_minutes,
            "versions_rows": versions_count,
            "versions_bytes": versions_bytes,
            "ok": integrity == "ok" and not foreign_keys,
        }
