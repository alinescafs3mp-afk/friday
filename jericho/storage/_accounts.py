"""Storage methods for users, presets, API tokens and the audit log.

Moved verbatim out of the single 5900-line ``JerichoStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.storage._base import (
    MAX_API_TOKEN_TTL_SECONDS,
    UTC,
    Any,
    AuditEntry,
    StorageShared,
    _json_load,
    datetime,
    json,
    new_id,
    re,
    timedelta,
    utc_now,
    validate_user_id,
)


class AccountsMixin(StorageShared):
    def ensure_user(
        self,
        user_id: str,
        *,
        source: str = "local",
        external_id: str = "",
        display_name: str = "",
        username: str = "",
        preset_key: str = "user",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_id = validate_user_id(user_id)
        now = utc_now()
        with self.transaction() as conn:
            existing = conn.execute("SELECT metadata_json FROM users WHERE id=?", (user_id,)).fetchone()
            merged_metadata = _json_load(existing["metadata_json"], {}) if existing else {}
            if not isinstance(merged_metadata, dict):
                merged_metadata = {}
            if metadata:
                merged_metadata.update(metadata)
            conn.execute(
                """INSERT INTO users(id, source, external_id, display_name, username, preset_key,
                   status, metadata_json, created_at, updated_at, last_seen_at)
                   VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     source=CASE WHEN excluded.source<>'' THEN excluded.source ELSE users.source END,
                     external_id=CASE WHEN excluded.external_id<>'' THEN excluded.external_id ELSE users.external_id END,
                     display_name=CASE WHEN excluded.display_name<>'' THEN excluded.display_name ELSE users.display_name END,
                     username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE users.username END,
                     metadata_json=excluded.metadata_json,
                     updated_at=excluded.updated_at,
                     last_seen_at=excluded.last_seen_at""",
                (
                    user_id,
                    source,
                    external_id,
                    display_name,
                    username,
                    preset_key,
                    json.dumps(merged_metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )
        return self.get_user(user_id) or {}

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        row = self.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT * FROM users ORDER BY last_seen_at DESC, id DESC LIMIT ? OFFSET ?",
            (max(1, min(limit, 5000)), max(0, offset)),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_user_ids(self, *, active_only: bool = True) -> list[str]:
        query = "SELECT id FROM users"
        params: tuple[Any, ...] = ()
        if active_only:
            query += " WHERE status='active'"
        return [row["id"] for row in self.execute(query, params).fetchall()]

    def update_user(self, user_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"display_name", "username", "preset_key", "status", "metadata_json"}
        updates: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "metadata_json" and not isinstance(value, str):
                value = json.dumps(value or {}, ensure_ascii=False, sort_keys=True)
            updates.append(f"{key}=?")
            values.append(value)
        if not updates:
            return self.get_user(user_id)
        updates.append("updated_at=?")
        values.extend([utc_now(), user_id])
        with self.transaction() as conn:
            # Column assignments are built only from the fixed ``allowed`` field map above.
            conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE id=?",  # nosec B608
                tuple(values),
            )
        return self.get_user(user_id)

    def create_api_token(
        self,
        user_id: str,
        token_sha256: str,
        *,
        label: str = "",
        created_by: str = "",
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Store the SHA-256 of a scoped API token; the plaintext is never persisted.

        ``ttl_seconds`` (a positive number of seconds) makes the token expire that
        long after creation; ``None`` mints a non-expiring token. created_at and
        expires_at come from the same instant so the lifetime is exact.
        """
        token_id = new_id("tok")
        now = datetime.now(UTC)
        created_at = now.isoformat(timespec="seconds")
        expires_at: str | None = None
        if ttl_seconds is not None:
            if ttl_seconds <= 0:
                raise ValueError("ttl_seconds must be a positive number of seconds")
            if ttl_seconds > MAX_API_TOKEN_TTL_SECONDS:
                raise ValueError(f"ttl_seconds must not exceed {MAX_API_TOKEN_TTL_SECONDS} (~100 years)")
            expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO api_tokens(id, user_id, token_sha256, label, created_by, created_at, expires_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (token_id, user_id, token_sha256, label[:200], created_by, created_at, expires_at),
            )
        row = self.execute("SELECT * FROM api_tokens WHERE id=?", (token_id,)).fetchone()
        return dict(row) if row else {}

    def find_api_token(self, token_sha256: str) -> dict[str, Any] | None:
        """Return the live token row for a SHA-256: non-revoked AND not expired."""
        if not token_sha256:
            return None
        row = self.execute(
            """SELECT * FROM api_tokens
               WHERE token_sha256=? AND revoked_at IS NULL
                 AND (expires_at IS NULL OR expires_at > ?)""",
            (token_sha256, utc_now()),
        ).fetchone()
        return dict(row) if row else None

    def touch_api_token(self, token_id: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE api_tokens SET last_used_at=? WHERE id=?",
                (utc_now(), token_id),
            )

    def list_api_tokens(
        self,
        user_id: str | None = None,
        *,
        include_revoked: bool = False,
    ) -> list[dict[str, Any]]:
        """Token metadata only — never the hash or plaintext."""
        clauses: list[str] = []
        params: list[Any] = []
        if user_id:
            clauses.append("user_id=?")
            params.append(user_id)
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.execute(
            "SELECT id, user_id, label, created_by, created_at, last_used_at, revoked_at, expires_at "
            f"FROM api_tokens{where} ORDER BY created_at DESC",  # nosec B608
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_api_token(self, token_id: str) -> dict[str, Any] | None:
        """The token record by id, hash excluded — enough to know whose token it is.

        Revocation has to answer "whose?" before it answers "yes", or a delegated
        administrator can lock the owner out of their own instance.
        """
        row = self.execute(
            "SELECT id, user_id, label, created_by, created_at, expires_at, revoked_at, last_used_at"
            " FROM api_tokens WHERE id=?",
            (token_id,),
        ).fetchone()
        return dict(row) if row else None

    def revoke_api_token(self, token_id: str, *, user_id: str | None = None) -> bool:
        clause = " AND user_id=?" if user_id else ""
        params: tuple[Any, ...] = (utc_now(), token_id, user_id) if user_id else (utc_now(), token_id)
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE api_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL"  # nosec B608
                + clause,
                params,
            )
        return cursor.rowcount > 0

    def upsert_custom_preset(
        self,
        preset_key: str,
        name: str,
        capabilities: set[str],
        *,
        description: str = "",
        created_by: str,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{1,63}", preset_key):
            raise ValueError("Invalid preset key")
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO custom_presets(preset_key, name, description, created_by, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(preset_key) DO UPDATE SET
                     name=excluded.name, description=excluded.description, updated_at=excluded.updated_at""",
                (preset_key, name, description, created_by, now, now),
            )
            conn.execute("DELETE FROM preset_capabilities WHERE preset_key=?", (preset_key,))
            conn.executemany(
                "INSERT INTO preset_capabilities(preset_key, security_id) VALUES(?, ?)",
                [(preset_key, security_id) for security_id in sorted(capabilities)],
            )
        return self.get_custom_preset(preset_key) or {}

    def get_custom_preset(self, preset_key: str) -> dict[str, Any] | None:
        row = self.execute("SELECT * FROM custom_presets WHERE preset_key=?", (preset_key,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["capabilities"] = [
            item["security_id"]
            for item in self.execute(
                "SELECT security_id FROM preset_capabilities WHERE preset_key=? ORDER BY security_id",
                (preset_key,),
            ).fetchall()
        ]
        return result

    def list_custom_presets(self) -> list[dict[str, Any]]:
        rows = self.execute("SELECT preset_key FROM custom_presets ORDER BY preset_key").fetchall()
        return [item for row in rows if (item := self.get_custom_preset(row["preset_key"]))]

    def set_permission_override(self, user_id: str, security_id: str, effect: str | None) -> None:
        self.ensure_user(user_id)
        with self.transaction() as conn:
            if effect is None:
                conn.execute(
                    "DELETE FROM user_permission_overrides WHERE user_id=? AND security_id=?",
                    (user_id, security_id),
                )
            else:
                if effect not in {"allow", "deny"}:
                    raise ValueError("effect must be allow, deny, or None")
                conn.execute(
                    """INSERT INTO user_permission_overrides(user_id, security_id, effect, updated_at)
                       VALUES(?, ?, ?, ?)
                       ON CONFLICT(user_id, security_id) DO UPDATE SET
                         effect=excluded.effect, updated_at=excluded.updated_at""",
                    (user_id, security_id, effect, utc_now()),
                )

    def get_permission_overrides(self, user_id: str) -> dict[str, str]:
        rows = self.execute(
            "SELECT security_id, effect FROM user_permission_overrides WHERE user_id=?",
            (user_id,),
        ).fetchall()
        return {row["security_id"]: row["effect"] for row in rows}

    def log_audit(self, entry: AuditEntry) -> AuditEntry:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO audit_log(id, user_id, action, target_type, target_id,
                   before_json, after_json, ip_address, request_id, created_at)
                   VALUES(:id, :user_id, :action, :target_type, :target_id,
                   :before_json, :after_json, :ip_address, :request_id, :created_at)""",
                entry.to_row(),
            )
        return entry

    def count_audit_log(self, user_id: str | None = None) -> int:
        """Total, so a page can say what it is a page OF.

        Without it the client had only `len(items)`, which on a full page equals the
        limit — indistinguishable from «that is all there is». The audit log grows
        faster than anything else here, so it hit that first.
        """
        if user_id:
            row = self.execute(
                "SELECT COUNT(*) AS count FROM audit_log WHERE user_id=?", (user_id,)
            ).fetchone()
        else:
            row = self.execute("SELECT COUNT(*) AS count FROM audit_log").fetchone()
        return int(row["count"] if row else 0)

    def list_audit_log(
        self,
        user_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        if user_id:
            rows = self.execute(
                "SELECT * FROM audit_log WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (user_id, max(1, min(limit, 5000)), max(0, offset)),
            ).fetchall()
        else:
            rows = self.execute(
                "SELECT * FROM audit_log ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (max(1, min(limit, 5000)), max(0, offset)),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_recent_audit(self, action: str, since: str, *, limit: int | None = None) -> int:
        """Count audit-log entries for ``action`` created at/after an ISO timestamp.

        ``limit`` caps the scan (``SELECT 1 ... LIMIT n``): callers that only need a
        threshold comparison stay O(limit) regardless of how bloated the log is (a
        request flood keeps auditing ``auth.failed`` even while being rate-limited).
        """
        if limit is not None:
            row = self.execute(
                "SELECT COUNT(*) AS count FROM "
                "(SELECT 1 FROM audit_log WHERE action=? AND created_at>=? LIMIT ?)",
                (action, since, max(0, limit)),
            ).fetchone()
        else:
            row = self.execute(
                "SELECT COUNT(*) AS count FROM audit_log WHERE action=? AND created_at>=?",
                (action, since),
            ).fetchone()
        return int(row["count"] if row else 0)
