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

    def list_pending_reminders(self, user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Pending reminder pushes for one tenant only (self-service list).

        Unlike ``list_pending_notifications`` (bridge drain, all users), this
        method requires ``user_id`` so a self-service handler cannot leak another
        person's queue. Only ``kind='reminder'`` and ``status='pending'`` —
        dismissed/sent/failed rows stay out of the list and out of the drain.
        """
        user_id = validate_user_id(user_id)
        rows = self.execute(
            "SELECT id, user_id, chat_id, kind, dedup_key, body, status, created_at "
            "FROM outbound_notifications "
            "WHERE user_id=? AND kind='reminder' AND status='pending' "
            "ORDER BY created_at ASC LIMIT ?",
            (user_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(row) for row in rows]

    def dismiss_notification(self, user_id: str, notification_id: str) -> bool:
        """Mark a pending reminder dismissed without releasing its dedup key.

        Opposite of ``discard_notifications`` / terminal failure: those clear
        ``dedup_key`` so the organ can re-raise the matter. Dismiss means the
        person saw the reminder and cancelled it — the partial unique index on
        ``(user_id, dedup_key)`` must keep blocking the next ``scan_reminders``
        enqueue of the same key. Only ``kind='reminder'`` and
        ``status='pending'`` rows of this tenant transition — same scope as
        ``list_pending_reminders``. Other kinds (chronicle/sentinel/…), foreign
        or already-terminal ids return False (→ 404). Keeping non-reminder
        rows out matters: dismiss intentionally leaves ``dedup_key`` in place,
        which would permanently block re-raise for a non-reminder organ.
        """
        user_id = validate_user_id(user_id)
        notification_id = str(notification_id or "").strip()
        if not notification_id:
            return False
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE outbound_notifications
                   SET status='dismissed'
                   WHERE id=? AND user_id=? AND kind='reminder' AND status='pending'""",
                (notification_id, user_id),
            )
        return cursor.rowcount > 0

    def discard_notifications(self, ids: Sequence[str], *, reason: str) -> int:
        """Terminate rows that can never be delivered, without spending attempts.

        A pending row that the bridge is not allowed to deliver — its chat is no
        longer in the allow-list — used to be skipped in the route and left
        exactly as it was: `status='pending'`, `attempts=0`, no marker, no path
        to cleanup. Since the queue is drained `ORDER BY created_at ASC LIMIT 20`,
        twenty such rows fill every slot forever and NOTHING else is ever
        delivered again: no reminders, no digest, no sentinel alert — silently,
        with no error anywhere.

        The dedup key is released for the same reason it is released at the
        attempt cap: the row is gone, so the organ must be able to raise the
        matter again if the chat is ever re-allowed.
        """
        cleaned = [str(value) for value in ids if str(value)]
        if not cleaned:
            return 0
        marker = f"undeliverable:{reason}"[:64]
        with self.transaction() as conn:
            for notification_id in cleaned:
                conn.execute(
                    """UPDATE outbound_notifications
                       SET status='failed', dedup_key='', kind=?
                       WHERE id=? AND status='pending'""",
                    (marker, notification_id),
                )
        return len(cleaned)

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
                #
                # Reaching that cap also releases the dedup key. There is no delay
                # column, and the bridge drains every 15 s, so five attempts are
                # spent in about 75 seconds — a two-minute network outage exhausts
                # every queued reminder. The key kept the row in `uq_outbound_dedup`
                # (partial: `WHERE dedup_key <> ''`), so `enqueue_notification` then
                # refused to queue that reminder ever again: an event silently lost
                # its only notification because the WAN blinked. Clearing it lets the
                # organ re-enqueue on its next scan. Only on the terminal transition
                # — a `sent` row keeps its key, or the same message would be sent
                # twice.
                conn.execute(
                    """UPDATE outbound_notifications
                       SET attempts=attempts+1,
                           status=CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END,
                           dedup_key=CASE WHEN attempts + 1 >= ? THEN '' ELSE dedup_key END
                       WHERE id=?""",
                    (max(1, int(max_attempts)), max(1, int(max_attempts)), str(notif_id)),
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

    # ------------------------------------------------------------------
    # Мониторы: сохранённый вопрос, за которым система следит сама
    # (спека v3 §6). Условие — текст запроса, а не выражение на своём языке:
    # второй язык условий означал бы вторую реализацию «что считается
    # совпадением», и она разошлась бы с поиском молча.

    # Сколько слежений человек может держать одновременно. Число небольшое
    # намеренно: монитор — это внимание системы, а не хранилище закладок.
    MAX_ACTIVE_MONITORS = 20

    def create_monitor(self, user_id: str, query: str, *, chat_id: str = "") -> dict[str, Any]:
        """Завести монитор.

        Граница «что уже видели» ставится по КУРСОРУ (`rowid` последнего знания на
        этот момент), а не по времени: `utc_now()` здесь секундной точности, и
        документ, пришедший в ту же секунду, что и создание монитора, при
        сравнении по времени потерялся бы навсегда — тихо и невоспроизводимо.
        Заодно это и есть ответ на «не вываливать старое»: всё, что было до, имеет
        меньший rowid.
        """
        clean = " ".join(str(query or "").split())[:500]
        if len(clean) < 2:
            raise ValueError("Запрос монитора слишком короткий")
        self.ensure_user(user_id)
        # Потолок на человека. Без него один аккаунт (а открытая регистрация
        # включена) заводит сотни слежений: список показывает двести новейших,
        # остальные нельзя ни увидеть, ни снять — а обход платит за каждое.
        active = self.execute(
            "SELECT COUNT(*) AS count FROM monitors WHERE user_id=? AND active=1", (user_id,)
        ).fetchone()
        if int((active["count"] if active else 0) or 0) >= self.MAX_ACTIVE_MONITORS:
            raise ValueError("Слишком много слежений; снимите лишние")
        now = utc_now()
        monitor_id = new_id("mon")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(rowid), 0) AS cursor FROM knowledge_objects WHERE user_id=?",
                (user_id,),
            ).fetchone()
            conn.execute(
                """INSERT INTO monitors(id, user_id, query, chat_id, active,
                   last_seen_rowid, last_seen_at, last_checked_at, matches_reported, created_at)
                   VALUES(?, ?, ?, ?, 1, ?, ?, NULL, 0, ?)""",
                (monitor_id, user_id, clean, str(chat_id or ""), int(row["cursor"] or 0), now, now),
            )
        return self.get_monitor(monitor_id, user_id) or {}

    def get_monitor(self, monitor_id: str, user_id: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM monitors WHERE id=? AND user_id=?", (monitor_id, user_id)
        ).fetchone()
        return dict(row) if row else None

    def list_monitors(self, user_id: str, *, active_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM monitors WHERE user_id=?"
        params: list[Any] = [user_id]
        if active_only:
            query += " AND active=1"
        rows = self.execute(query + " ORDER BY created_at DESC LIMIT 200", tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def iter_active_monitors(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Все живые мониторы всех арендаторов — для фонового обхода.

        Арендатор не теряется: каждая строка несёт свой `user_id`, и проверка
        монитора идёт под ним же. Обход без него означал бы поиск от лица
        воркера, то есть по чужим данным.
        """
        rows = self.execute(
            "SELECT * FROM monitors WHERE active=1 ORDER BY COALESCE(last_checked_at, created_at) LIMIT ?",
            (max(1, min(int(limit), 5000)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def stop_monitor(self, monitor_id: str, user_id: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE monitors SET active=0 WHERE id=? AND user_id=? AND active=1",
                (monitor_id, user_id),
            )
        return cursor.rowcount == 1

    def mark_monitor_checked(
        self, monitor_id: str, user_id: str, *, seen_rowid: int, reported: int = 0
    ) -> None:
        """Подвинуть границу «что уже показывали».

        Курсор двигается ТОЛЬКО вперёд и только до последней ПРОСМОТРЕННОЙ строки:
        материал, появившийся во время прохода, имеет больший rowid и попадёт в
        следующий проход, а не потеряется в щели между двумя.
        """
        now = utc_now()
        with self.transaction() as conn:
            conn.execute(
                """UPDATE monitors
                   SET last_checked_at=?, matches_reported=matches_reported+?, last_seen_at=?,
                       last_seen_rowid=CASE WHEN ?>last_seen_rowid THEN ? ELSE last_seen_rowid END
                   WHERE id=? AND user_id=?""",
                (
                    now,
                    max(0, int(reported)),
                    now,
                    int(seen_rowid),
                    int(seen_rowid),
                    monitor_id,
                    user_id,
                ),
            )
