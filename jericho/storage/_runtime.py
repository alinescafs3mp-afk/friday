"""Storage methods for runtime key-value state, outbound queue and idempotency.

Moved verbatim out of the single 5900-line ``JerichoStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from jericho.storage._base import (
    RUNTIME_EVENT_CAP,
    UTC,
    Any,
    Sequence,
    StorageShared,
    _json_load,
    datetime,
    hmac,
    json,
    new_id,
    re,
    timedelta,
    utc_now,
    validate_user_id,
)


class RuntimeMixin(StorageShared):
    def enqueue_notification(
        self,
        user_id: str,
        chat_id: str,
        body: str,
        *,
        kind: str = "",
        dedup_key: str = "",
    ) -> bool:
        """Queue a push message. Returns False when a same dedup_key already exists."""
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO outbound_notifications(
                       id, user_id, chat_id, kind, dedup_key, body, status, attempts, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (new_id("notif"), user_id, str(chat_id), kind, dedup_key, body, utc_now()),
            )
        return cursor.rowcount > 0

    def list_pending_notifications(self, *, limit: int = 20, max_attempts: int = 5) -> list[dict[str, Any]]:
        rows = self.execute(
            "SELECT id, user_id, chat_id, kind, body FROM outbound_notifications "
            "WHERE status='pending' AND attempts < ? ORDER BY created_at ASC LIMIT ?",
            (max(1, int(max_attempts)), max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_notifications(
        self,
        sent_ids: Sequence[str] = (),
        failed_ids: Sequence[str] = (),
        *,
        max_attempts: int = 5,
    ) -> None:
        with self.transaction() as conn:
            for notif_id in sent_ids:
                conn.execute(
                    "UPDATE outbound_notifications SET status='sent', sent_at=? WHERE id=?",
                    (utc_now(), str(notif_id)),
                )
            for notif_id in failed_ids:
                # A failed send stays pending for retry until the attempt cap;
                # then it becomes a terminal 'failed' row rather than looping.
                conn.execute(
                    """UPDATE outbound_notifications
                       SET attempts=attempts+1,
                           status=CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END
                       WHERE id=?""",
                    (max(1, int(max_attempts)), str(notif_id)),
                )

    def idempotency_get(
        self,
        user_id: str,
        request_key: str,
        *,
        request_hash: str = "",
    ) -> dict[str, Any] | None:
        if not request_key:
            return None
        row = self.execute(
            """SELECT request_hash, response_json FROM request_idempotency
               WHERE user_id=? AND request_key=? AND state='complete'""",
            (user_id, request_key),
        ).fetchone()
        if not row:
            return None
        stored_hash = str(row["request_hash"] or "")
        if request_hash and (not stored_hash or not hmac.compare_digest(stored_hash, request_hash)):
            return None
        return _json_load(row["response_json"], {})

    def idempotency_claim(
        self,
        user_id: str,
        request_key: str,
        *,
        request_hash: str = "",
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        """Atomically claim a request key before any externally visible side effect.

        The durable pending lease closes the check-then-act race both between
        concurrent asyncio tasks and between multiple API worker processes.
        """
        if not request_key:
            return {"status": "disabled", "lease_token": ""}
        user_id = validate_user_id(user_id)
        request_hash = str(request_hash or "").strip().casefold()
        if request_hash and not re.fullmatch(r"[0-9a-f]{64}", request_hash):
            raise ValueError("request_hash must be a lowercase SHA-256 hex digest")
        now = datetime.now(UTC)
        now_text = now.isoformat(timespec="seconds")
        token = new_id("lease")
        with self.transaction() as conn:
            row = conn.execute(
                """SELECT request_hash, response_json, state, lease_token, created_at, updated_at
                   FROM request_idempotency WHERE user_id=? AND request_key=?""",
                (user_id, request_key),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO request_idempotency(
                           user_id, request_key, request_hash, response_json, state,
                           lease_token, created_at, updated_at
                       ) VALUES(?, ?, ?, '{}', 'pending', ?, ?, ?)""",
                    (user_id, request_key, request_hash, token, now_text, now_text),
                )
                return {"status": "acquired", "lease_token": token}

            stored_hash = str(row["request_hash"] or "")
            if request_hash and stored_hash and not hmac.compare_digest(stored_hash, request_hash):
                return {"status": "conflict", "reason": "request_hash_mismatch", "lease_token": ""}

            if row["state"] == "complete":
                # Rows created before schema v7 have no trustworthy payload
                # fingerprint. Replaying them against an arbitrary new body
                # would reintroduce silent data loss, so require a new key.
                if request_hash and not stored_hash:
                    return {"status": "conflict", "reason": "legacy_unbound_key", "lease_token": ""}
                return {
                    "status": "replay",
                    "response": _json_load(row["response_json"], {}),
                    "lease_token": "",
                }

            timestamp = str(row["updated_at"] or row["created_at"] or "")
            try:
                updated_at = datetime.fromisoformat(timestamp)
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
            except ValueError:
                updated_at = datetime.min.replace(tzinfo=UTC)
            if updated_at <= now - timedelta(seconds=max(1, int(lease_seconds))):
                conn.execute(
                    """UPDATE request_idempotency
                       SET request_hash=?, lease_token=?, response_json='{}', state='pending', updated_at=?
                       WHERE user_id=? AND request_key=?""",
                    (request_hash or stored_hash, token, now_text, user_id, request_key),
                )
                return {"status": "acquired", "lease_token": token, "stale_lease_recovered": True}
            return {"status": "in_progress", "lease_token": ""}

    def idempotency_renew(self, user_id: str, request_key: str, lease_token: str) -> bool:
        if not request_key or not lease_token:
            return False
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE request_idempotency SET updated_at=?
                   WHERE user_id=? AND request_key=? AND state='pending' AND lease_token=?""",
                (utc_now(), user_id, request_key, lease_token),
            )
        return cursor.rowcount == 1

    def idempotency_complete(
        self,
        user_id: str,
        request_key: str,
        lease_token: str,
        response: dict[str, Any],
    ) -> bool:
        if not request_key or not lease_token:
            return False
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE request_idempotency
                   SET response_json=?, state='complete', lease_token='', updated_at=?
                   WHERE user_id=? AND request_key=? AND state='pending' AND lease_token=?""",
                (
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                    user_id,
                    request_key,
                    lease_token,
                ),
            )
        return cursor.rowcount == 1

    def idempotency_release(self, user_id: str, request_key: str, lease_token: str) -> bool:
        if not request_key or not lease_token:
            return False
        with self.transaction() as conn:
            cursor = conn.execute(
                """DELETE FROM request_idempotency
                   WHERE user_id=? AND request_key=? AND state='pending' AND lease_token=?""",
                (user_id, request_key, lease_token),
            )
        return cursor.rowcount == 1

    def idempotency_store(
        self,
        user_id: str,
        request_key: str,
        response: dict[str, Any],
        *,
        request_hash: str = "",
    ) -> None:
        """Compatibility helper for callers that already completed their work."""
        if not request_key:
            return
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO request_idempotency(
                       user_id, request_key, request_hash, response_json, state,
                       lease_token, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, 'complete', '', ?, ?)
                   ON CONFLICT(user_id, request_key) DO UPDATE SET
                     request_hash=CASE
                       WHEN request_idempotency.state='complete' THEN request_idempotency.request_hash
                       ELSE excluded.request_hash
                     END,
                     response_json=CASE
                       WHEN request_idempotency.state='complete' THEN request_idempotency.response_json
                       ELSE excluded.response_json
                     END,
                     state=CASE
                       WHEN request_idempotency.state='complete' THEN request_idempotency.state
                       ELSE 'complete'
                     END,
                     lease_token=CASE
                       WHEN request_idempotency.state='complete' THEN request_idempotency.lease_token
                       ELSE ''
                     END,
                     updated_at=excluded.updated_at""",
                (
                    user_id,
                    request_key,
                    request_hash,
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )

    def idempotency_prune(self, *, days: int = 30) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                """DELETE FROM request_idempotency
                   WHERE datetime(COALESCE(NULLIF(updated_at, ''), created_at)) < datetime('now', ?)""",
                (f"-{max(1, min(int(days), 365))} days",),
            )
        return cursor.rowcount

    # A machine that runs fourteen background workers, a bridge and a backup schedule
    # answers "what happened while I was asleep" from its logs today — which means
    # grepping tracebacks and correlating timestamps by hand. These events exist to
    # answer it directly.
    #
    # Bounded from the start: a journal that grows without limit is a worse defect than
    # the empty table this replaces. Callers record TRANSITIONS, not states, so a worker
    # broken all night costs two rows rather than one per tick.
    def record_event(self, event_type: str, payload: dict[str, Any] | None = None) -> str:
        """Append one operational event and trim the journal to its cap."""
        event_id = new_id("evt")
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO runtime_events(id, event_type, payload, created_at) VALUES(?,?,?,?)",
                (event_id, str(event_type)[:64], json.dumps(payload or {}, ensure_ascii=False), utc_now()),
            )
            # Trim by row count rather than age: age alone lets a burst blow the table
            # up inside the retention window, and a count is what bounds disk.
            #
            # Ordered by rowid within a timestamp, never by `id`. `created_at` has
            # one-second resolution and `id` is random, so a burst — exactly when this
            # journal earns its keep — would otherwise trim and list in arbitrary
            # order, discarding the newest events instead of the oldest.
            conn.execute(
                """DELETE FROM runtime_events WHERE id IN (
                       SELECT id FROM runtime_events ORDER BY created_at DESC, rowid DESC
                       LIMIT -1 OFFSET ?)""",
                (RUNTIME_EVENT_CAP,),
            )
        return event_id

    def list_events(
        self,
        *,
        event_type: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        rows = self.execute(
            # ``clauses`` holds fixed predicates only; every value stays a bound parameter.
            f"SELECT * FROM runtime_events {where} ORDER BY created_at DESC, rowid DESC LIMIT ?",  # nosec B608
            tuple(params),
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["payload"] = _json_load(event.get("payload"), {})
            events.append(event)
        return events

    def count_events(self) -> int:
        row = self.execute("SELECT COUNT(*) AS count FROM runtime_events").fetchone()
        return int(row["count"] if row else 0)

    def kv_get(self, key: str) -> str | None:
        row = self.execute("SELECT value FROM runtime_kv WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO runtime_kv(key, value, updated_at) VALUES(?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, utc_now()),
            )

    def kv_delete(self, key: str) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM runtime_kv WHERE key=?", (key,))

    def kv_list_prefix(self, prefix: str) -> list[dict[str, Any]]:
        escaped = prefix.replace("%", r"\%").replace("_", r"\_")
        rows = self.execute(
            "SELECT * FROM runtime_kv WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
            (f"{escaped}%",),
        ).fetchall()
        return [dict(row) for row in rows]

    def claim_bridge_nonce(self, nonce: str) -> bool:
        """Atomically record a single-use bridge nonce; return False on replay.

        The claim is a race-free INSERT-if-absent under BEGIN IMMEDIATE, so two
        concurrent requests carrying the same nonce cannot both be accepted.
        """
        if not nonce:
            return False
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO runtime_kv(key, value, updated_at) VALUES(?, '1', ?)
                   ON CONFLICT(key) DO NOTHING""",
                (f"{self._BRIDGE_NONCE_PREFIX}{nonce}", utc_now()),
            )
        return cursor.rowcount == 1

    def prune_bridge_nonces(self, *, max_age_sec: int) -> int:
        """Drop bridge nonces older than the replay window; return rows removed."""
        cutoff = max(1, int(max_age_sec))
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM runtime_kv WHERE key LIKE ? ESCAPE '\\' "  # nosec B608
                "AND datetime(updated_at) < datetime('now', ?)",
                (f"{self._BRIDGE_NONCE_PREFIX}%", f"-{cutoff} seconds"),
            )
        return cursor.rowcount
