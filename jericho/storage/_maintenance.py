"""Storage methods for backup, restore, export, purge and diagnostics.

Moved verbatim out of the single 5900-line ``JerichoStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.storage._base import (
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
    unpack_snapshot,
    utc_now,
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
            "SELECT id, user_id, title, deleted_at FROM knowledge_objects "
            "WHERE deleted_at IS NOT NULL AND datetime(deleted_at) <= datetime('now', ?)"
        )
        if user_id:
            rows = self.execute(
                f"{base} AND user_id=? ORDER BY deleted_at LIMIT ?",
                (cutoff, user_id, bounded),
            ).fetchall()
        else:
            rows = self.execute(
                f"{base} ORDER BY deleted_at LIMIT ?",
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
        self.settings.backups_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        stem = f"jericho-{timestamp}-{_safe_filename(label)}"
        destination = self.settings.backups_dir / f"{stem}.sqlite3"
        suffix = 1
        while destination.exists():
            destination = self.settings.backups_dir / f"{stem}-{suffix}.sqlite3"
            suffix += 1

        backup_conn = sqlite3.connect(str(destination))
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
            destination.unlink(missing_ok=True)
            raise
        finally:
            backup_conn.close()
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
        """
        keep = int(keep)
        if keep <= 0:
            return {"enabled": False, "removed": 0, "kept": 0}
        # `list_backups` is already newest-first (the stem carries a sortable UTC
        # timestamp) and already refuses symlinks and paths outside backups_dir.
        backups = self.list_backups()
        removed: list[str] = []
        for entry in backups[keep:]:
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
                LOGGER.warning("Could not prune backup %s: %s", database.name, exc)
                continue
            removed.append(database.name)
        if removed:
            LOGGER.info("Pruned %d backup(s), keeping the newest %d", len(removed), keep)
        return {"enabled": True, "removed": len(removed), "kept": min(len(backups), keep)}

    def list_backups(self) -> list[dict[str, Any]]:
        self.settings.backups_dir.mkdir(parents=True, exist_ok=True)
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
        """Atomically restore a verified SQLite backup while Jericho is stopped.

        The current process must own the exclusive backend lease.  The exact
        active DB/WAL/SHM bytes are staged first for automatic rollback.  A
        verified online safety backup is created when the active database can be
        opened; otherwise an explicitly unverified recovery bundle preserves the
        original files instead of making a destructive restore unavoidable.

        Only the SQLite knowledge database is restored.  Content-addressed
        files, Markdown vault, Telegram queue, model weights and secrets remain
        external, matching the backup manifest.
        """

        from jericho.diagnostics.runtime_lease import process_owns_lease

        lease_path = self.settings.state_dir / "backend.lock"
        if not process_owns_lease(lease_path, protocol="jericho.backend.v1"):
            raise RuntimeError(
                "Database restore requires the exclusive backend process lease; "
                "stop Jericho and use `jericho restore-backup ... --yes`"
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
            }
        except BaseException as restore_error:
            self.close()
            rollback_error: BaseException | None = None
            try:
                for active_path in active_paths:
                    active_path.unlink(missing_ok=True)
                for original, snapshot in rollback_snapshots.items():
                    os.replace(snapshot, original)
                    _chmod_private(original)
                _fsync_directory(database_path.parent)
            except BaseException as exc:
                rollback_error = exc
            if rollback_error is not None:
                raise RuntimeError(
                    "Database restore failed and exact-file rollback also failed: "
                    f"restore={type(restore_error).__name__}: {restore_error}; "
                    f"rollback={type(rollback_error).__name__}: {rollback_error}"
                ) from restore_error
            recovery = (
                "the exact previous database files were restored automatically"
                if rollback_snapshots
                else "the failed replacement was removed; no previous database existed"
            )
            raise RuntimeError(
                f"Database restore failed; {recovery}: {type(restore_error).__name__}: {restore_error}"
            ) from restore_error
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
            for snapshot in rollback_snapshots.values():
                snapshot.unlink(missing_ok=True)

    def export_user(self, user_id: str) -> dict[str, Any]:
        user = self.get_user(user_id)
        if not user:
            raise ValueError("User not found")
        table_queries = {
            # Персональные данные: какими способами человек входит в этот аккаунт.
            "user_identities": ("SELECT * FROM user_identities WHERE user_id=?", (user_id,)),
            "raw_objects": ("SELECT * FROM raw_objects WHERE user_id=?", (user_id,)),
            "knowledge_objects": ("SELECT * FROM knowledge_objects WHERE user_id=?", (user_id,)),
            "inbox": ("SELECT * FROM inbox WHERE user_id=?", (user_id,)),
            "entities": ("SELECT * FROM entities WHERE user_id=?", (user_id,)),
            "knowledge_entity_links": ("SELECT * FROM knowledge_entity_links WHERE user_id=?", (user_id,)),
            "relations": ("SELECT * FROM relations WHERE user_id=?", (user_id,)),
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
            "relation_candidates": ("SELECT * FROM relation_candidates WHERE user_id=?", (user_id,)),
            "knowledge_conflicts": ("SELECT * FROM knowledge_conflicts WHERE user_id=?", (user_id,)),
            "conversations": ("SELECT * FROM conversations WHERE user_id=?", (user_id,)),
            "messages": ("SELECT * FROM messages WHERE user_id=?", (user_id,)),
            "channel_sessions": ("SELECT * FROM channel_sessions WHERE user_id=?", (user_id,)),
            "request_idempotency": (
                """SELECT user_id, request_key, request_hash, response_json, state, created_at, updated_at
                   FROM request_idempotency WHERE user_id=? AND state='complete'""",
                (user_id,),
            ),
            "audit_log": ("SELECT * FROM audit_log WHERE user_id=?", (user_id,)),
        }
        payload: dict[str, Any] = {
            "format": "jericho-user-export-v3",
            "exported_at": utc_now(),
            "user": user,
        }
        for key, (query, params) in table_queries.items():
            rows = [dict(row) for row in self.execute(query, params).fetchall()]
            if key == "knowledge_object_versions":
                # Сжатый хвост версий — байты; выгрузка обязана нести прежний
                # текст, иначе json.dumps испортил бы снимки молча.
                for row in rows:
                    row["snapshot_json"] = unpack_snapshot(row.get("snapshot_json"))
            payload[key] = rows
        self.settings.exports_dir.mkdir(parents=True, exist_ok=True)
        identity_hash = hashlib.sha256(user_id.encode("utf-8", errors="replace")).hexdigest()[:12]
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        path = self.settings.exports_dir / (
            f"jericho-export-{_safe_filename(user_id)}--{identity_hash}-{timestamp}.json"
        )
        _write_json_atomic(path, payload)
        return {"path": str(path), "filename": path.name, "size_bytes": path.stat().st_size}

    def diagnostics(self) -> dict[str, Any]:
        integrity = self.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = [dict(row) for row in self.execute("PRAGMA foreign_key_check").fetchall()]
        counts: dict[str, int] = {}
        for table in (
            "users",
            "raw_objects",
            "knowledge_objects",
            "inbox",
            "entities",
            "relations",
            "relation_candidates",
            "knowledge_conflicts",
            "feedback",
            "feedback_state",
            "knowledge_usage",
            "conversations",
            "messages",
            "channel_sessions",
        ):
            # ``table`` comes only from the fixed diagnostic allowlist above.
            row = self.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()  # nosec B608
            counts[table] = int(row["count"] if row else 0)
        # Pending Inbox material is not knowledge yet: it cannot be found by search, so a
        # forgotten backlog is imported material the owner can no longer reach. Reported
        # here as well as in the offline `_database_status`, because this is the path a
        # running instance takes — and therefore the one Sentinel actually sees.
        backlog = self.execute(
            "SELECT COUNT(*) AS count, MIN(created_at) AS oldest FROM inbox WHERE status='pending'"
        ).fetchone()
        # Возраст важнее размера и здесь тоже: очередь наполняет backend, а
        # разгребает МОСТ. Мёртвый мост backend видел (count рос) и молчал —
        # застрявшее уведомление неотличимо от только что положенного без
        # отметки времени. Живой экземпляр ходит ЭТИМ путём, не `_database_status`.
        outbound = self.execute(
            "SELECT COUNT(*) AS count, MIN(created_at) AS oldest "
            "FROM outbound_notifications WHERE status='pending'"
        ).fetchone()
        # Версии хранят ПОЛНЫЙ content в каждом снапшоте, чистки нет нигде:
        # массовое ре-обогащение добавляет копию корпуса в базу навсегда и
        # раздувает каждый из 14 суточных бэкапов. Пока рост хотя бы виден;
        # ретеншн (N полных + сжатие старых) — отдельная работа.
        versions = self.execute(
            "SELECT COUNT(*) AS count, COALESCE(SUM(LENGTH(snapshot_json)), 0) AS bytes "
            "FROM knowledge_object_versions"
        ).fetchone()
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
            "versions_rows": int(versions["count"] if versions else 0),
            "versions_bytes": int(versions["bytes"] if versions else 0),
            "ok": integrity == "ok" and not foreign_keys,
        }
