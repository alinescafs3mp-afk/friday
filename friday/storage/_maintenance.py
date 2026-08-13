"""Storage methods for backup, restore, export, purge and diagnostics.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

import unicodedata
import zlib

from friday.private_fs import (
    ensure_private_directory,
    prepare_private_sqlite,
    restrict_sqlite_files,
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
    _fsync_directory,
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
    pending = [decoded]
    visited = 0
    while pending:
        visited += 1
        if visited > max(1, int(max_bytes)):
            return False
        item = pending.pop()
        if isinstance(item, dict):
            for key, nested in item.items():
                if str(key).lstrip().startswith(("{", "[", '"')):
                    return False
                pending.append(nested)
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and item.lstrip().startswith(("{", "[", '"')):
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
        integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = backup_conn.execute("PRAGMA foreign_key_check").fetchall()
        schema_row = backup_conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        backup_schema_version = int(schema_row[0]) if schema_row else -1
        return integrity, foreign_key_violations, backup_schema_version

    def create_backup(self, *, label: str = "manual") -> dict[str, Any]:
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
            integrity, foreign_key_violations, backup_schema_version = self._verify_backup_conn(backup_conn)
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
            raise RuntimeError(f"Backup foreign-key check failed: {len(foreign_key_violations)} violation(s)")
        if backup_schema_version != SCHEMA_VERSION:
            destination.unlink(missing_ok=True)
            raise RuntimeError(
                f"Backup schema mismatch: database={backup_schema_version}, expected={SCHEMA_VERSION}"
            )

        _chmod_private(destination)
        digest = _sha256_file(destination)
        manifest = {
            "schema_version": backup_schema_version,
            "created_at": utc_now(),
            "label": label,
            "database": destination.name,
            "size_bytes": destination.stat().st_size,
            "sha256": digest,
            "integrity_check": integrity,
            "foreign_key_violations": 0,
            # A database backup is transactionally consistent, but binary raw
            # files and the Markdown vault deliberately remain separate so an
            # operator cannot mistake this for a full installation backup.
            "scope": {
                "sqlite_database": "included",
                "raw_files": "external",
                "memory_vault": "external",
                "model_weights": "external",
                "configuration_and_secrets": "external",
            },
        }
        manifest_path = destination.with_suffix(".manifest.json")
        _write_json_atomic(manifest_path, manifest)
        return {**manifest, "path": str(destination), "manifest_path": str(manifest_path)}

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
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
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
        manifest_present = manifest_path.is_file() and not manifest_path.is_symlink()
        if manifest_path.is_symlink():
            manifest_error = "Manifest symlinks are not allowed"
        elif manifest_present:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
            ),
        }

    def restore_backup(self, filename: str, *, safety_label: str = "pre-restore") -> dict[str, Any]:
        """Atomically restore a verified SQLite backup while Friday is stopped.

        The current process must own the exclusive backend lease.  The exact
        active DB/WAL/SHM bytes are staged first for automatic rollback.  A
        verified online safety backup is created when the active database can be
        opened; otherwise an explicitly unverified recovery bundle preserves the
        original files instead of making a destructive restore unavoidable.

        Only the SQLite knowledge database is restored.  Content-addressed
        files, Markdown vault, Telegram queue, model weights and secrets remain
        external, matching the backup manifest.
        """

        from friday.diagnostics.runtime_lease import process_owns_lease

        lease_path = self.settings.state_dir / "backend.lock"
        if not process_owns_lease(lease_path, protocol="friday.backend.v1"):
            raise RuntimeError(
                "Database restore requires the exclusive backend process lease; "
                "stop Friday and use `jericho restore-backup ... --yes`"
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
        database_path = self._db_path.absolute()
        active_paths = [database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")]
        if any(path.is_symlink() for path in active_paths):
            raise RuntimeError("Database path and SQLite sidecars must not be symlinks during restore")

        # Stop using the active database before taking exact rollback copies.
        self.close()
        rollback_snapshots: dict[Path, Path] = {}
        # Дошли ли мы до подмены активной базы. Без этого различения ветка отказа
        # не могла отличить «нечего откатывать, потому что базы не было» от
        # «нечего откатывать, потому что снимок не снялся» — и во втором случае
        # удаляла живую базу, которой сбой ещё не коснулся.
        replaced = False
        staged: Path | None = None
        safety_backup: dict[str, Any] | None = None
        recovery_snapshot: dict[str, Any] | None = None
        try:
            for active_path in active_paths:
                if active_path.is_file():
                    rollback_snapshots[active_path] = _stage_private_copy(active_path, active_path)

            if database_path.is_file():
                try:
                    safety_backup = self.create_backup(label=safety_label)
                except BaseException as exc:
                    self.close()
                    if not rollback_snapshots:
                        raise RuntimeError("Could not preserve the active database before restore") from exc
                    recovery_snapshot = _write_recovery_bundle(
                        self.settings,
                        rollback_snapshots,
                        label=safety_label,
                        reason_type=type(exc).__name__,
                    )
                finally:
                    self.close()

            staged = _stage_private_copy(backup_path, database_path)
            staged_size = staged.stat().st_size
            staged_digest = _sha256_file(staged)
            if staged_size != int(verification["size_bytes"]) or not hmac.compare_digest(
                staged_digest,
                str(verification["sha256"]),
            ):
                raise RuntimeError("Backup changed while it was staged for restore")

            for active_path in active_paths[1:]:
                active_path.unlink(missing_ok=True)
            os.replace(staged, database_path)
            replaced = True
            _chmod_private(database_path)
            _fsync_directory(database_path.parent)

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
            return {
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
                    "telegram_queue": "unchanged",
                    "model_weights": "unchanged",
                    "configuration_and_secrets": "unchanged",
                },
                "integrity_check": health["integrity_check"],
                "foreign_key_violations": len(health.get("foreign_key_violations") or []),
                "invalidated_deletion_eligibility": invalidated_deletion_eligibility,
            }
        except BaseException as restore_error:
            self.close()
            rollback_error: BaseException | None = None
            try:
                # Удалять активные файлы можно ТОЛЬКО когда есть чем их заменить
                # либо когда мы сами их и положили. Прежняя редакция делала это
                # безусловно: ошибка на подготовке — нехватка места (restore
                # требует тройного размера базы), EIO на умирающем диске,
                # перемонтирование в read-only — уводила сюда ДО того, как снят
                # откатный снимок, и живая база, WAL и SHM удалялись. Следующее
                # обращение молча создавало пустую базу со схемой: Friday
                # поднималась с нулевым архивом, а не с ошибкой, и повторный
                # restore проходил — человек получал данные из копии и никогда не
                # узнавал, что потерял всё записанное после неё.
                if rollback_snapshots:
                    for active_path in active_paths:
                        active_path.unlink(missing_ok=True)
                    for original, snapshot in rollback_snapshots.items():
                        os.replace(snapshot, original)
                        _chmod_private(original)
                    _fsync_directory(database_path.parent)
                elif replaced:
                    # Базы раньше не было, но неудачная замена уже легла на место.
                    for active_path in active_paths:
                        active_path.unlink(missing_ok=True)
                    _fsync_directory(database_path.parent)
            except BaseException as exc:
                rollback_error = exc
            if rollback_error is not None:
                raise RuntimeError(
                    "Database restore failed and exact-file rollback also failed: "
                    f"restore={type(restore_error).__name__}: {restore_error}; "
                    f"rollback={type(rollback_error).__name__}: {rollback_error}"
                ) from restore_error
            if rollback_snapshots:
                recovery = "the exact previous database files were restored automatically"
            elif replaced:
                recovery = "the failed replacement was removed; no previous database existed"
            elif database_path.is_file():
                # Самое важное сообщение из трёх: восстановление не начиналось, и
                # база на месте. Раньше здесь безусловно печаталось «no previous
                # database existed» — прямая ложь ровно в тот момент, когда база
                # была и только что была удалена этой же веткой.
                recovery = "the restore never started and the active database was left untouched"
            else:
                recovery = "no database was present and none was created"
            raise RuntimeError(
                f"Database restore failed; {recovery}: {type(restore_error).__name__}: {restore_error}"
            ) from restore_error
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
            for snapshot in rollback_snapshots.values():
                snapshot.unlink(missing_ok=True)

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
                if not _export_raw_material_shape_is_valid(row)
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
