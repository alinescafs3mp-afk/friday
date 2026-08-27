"""Storage methods for runtime key-value state, outbound queue and idempotency.

Moved verbatim out of the single 5900-line ``FridayStorage``: same names,
signatures and bodies. Mixed back into that class, so ``self.execute`` and
``self.transaction`` resolve exactly as before and no call site moved.
"""

from __future__ import annotations

from friday.storage._base import (
    RUNTIME_EVENT_CAP,
    UTC,
    Any,
    DeletedAccountError,
    Sequence,
    StorageShared,
    _json_load,
    datetime,
    deleted_account_tombstone_key,
    hmac,
    json,
    known_runtime_key_owners,
    new_id,
    re,
    timedelta,
    utc_now,
    validate_user_id,
)
from friday.storage._privacy import _not_private_notification_dependency


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
        notification_id = new_id("notif")
        with self.transaction() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO outbound_notifications(
                       id, user_id, chat_id, kind, dedup_key, body, status, attempts, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, 'pending', 0, ?)""",
                (notification_id, user_id, str(chat_id), kind, dedup_key, body, utc_now()),
            )
            queued = cursor.rowcount > 0
            if queued:
                visible = conn.execute(
                    f"""SELECT 1 FROM outbound_notifications n WHERE n.id=?
                         AND {_not_private_notification_dependency("n")}""",  # nosec B608
                    (notification_id,),
                ).fetchone()
                if visible is None:
                    conn.execute("DELETE FROM outbound_notifications WHERE id=?", (notification_id,))
                    queued = False
        return queued

    def list_pending_notifications(self, *, limit: int = 20, max_attempts: int = 5) -> list[dict[str, Any]]:
        rows = self.execute(
            f"""SELECT n.id, n.user_id, n.chat_id, n.kind, n.dedup_key, n.body
                  FROM outbound_notifications n
                 WHERE n.status='pending' AND n.attempts < ?
                   AND (n.kind IN ('engineer_command_terminal','engineer_command_progress')
                        OR {_not_private_notification_dependency("n")})
                 ORDER BY n.created_at ASC LIMIT ?""",  # nosec B608
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
            f"""SELECT n.id, n.user_id, n.chat_id, n.kind, n.dedup_key, n.body,
                       n.status, n.created_at
                  FROM outbound_notifications n
                 WHERE n.user_id=? AND n.kind='reminder' AND n.status='pending'
                   AND {_not_private_notification_dependency("n")}
                 ORDER BY n.created_at ASC LIMIT ?""",  # nosec B608
            (user_id, max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(row) for row in rows]

    def silence_reminder(self, user_id: str, dedup_key: str, *, chat_id: str = "") -> bool:
        """«Не напоминай мне об этом» — независимо от того, отправлено уже или нет.

        `dismiss_notification` умеет гасить только строку в состоянии `pending`, а
        мост дренирует очередь раз в пятнадцать секунд. То есть кнопка «Снять»
        работала в пятнадцатисекундном окне после скана и промахивалась всё
        остальное время: строка уже `sent`, гасить нечего, а следующий скан
        поставит напоминание снова, пока живёт событие.

        Здесь снимается САМО напоминание, а не строка очереди: если гасить нечего,
        заводится запись с тем же `dedup_key` сразу в состоянии `dismissed`.
        Частичный уникальный индекс по `(user_id, dedup_key)` после этого не даст
        `scan_reminders` поставить его заново — ровно тем же механизмом, каким
        держится обычный дедуп.
        """
        user_id = validate_user_id(user_id)
        dedup_key = str(dedup_key or "").strip()
        if not dedup_key:
            return False
        with self.transaction() as conn:
            cursor = conn.execute(
                f"""UPDATE outbound_notifications AS n
                   SET status='dismissed'
                   WHERE user_id=? AND dedup_key=? AND kind='reminder'
                     AND status IN ('pending', 'sent')
                     AND {_not_private_notification_dependency("n")}""",  # nosec B608
                (user_id, dedup_key),
            )
            if cursor.rowcount > 0:
                return True
            notification_id = new_id("notif")
            inserted = conn.execute(
                """INSERT OR IGNORE INTO outbound_notifications(
                       id, user_id, chat_id, kind, dedup_key, body, status, attempts, created_at)
                   VALUES(?, ?, ?, 'reminder', ?, ?, 'dismissed', 0, ?)""",
                (
                    notification_id,
                    user_id,
                    str(chat_id),
                    dedup_key,
                    "снято до отправки",
                    utc_now(),
                ),
            )
            if inserted.rowcount > 0:
                visible = conn.execute(
                    f"""SELECT 1 FROM outbound_notifications n WHERE n.id=?
                         AND {_not_private_notification_dependency("n")}""",  # nosec B608
                    (notification_id,),
                ).fetchone()
                if visible is None:
                    conn.execute(
                        "DELETE FROM outbound_notifications WHERE id=?",
                        (notification_id,),
                    )
                    return False
        return inserted.rowcount > 0

    def reminder_states(self, user_id: str, dedup_keys: Sequence[str]) -> dict[str, str]:
        """Состояние напоминания по каждому ключу: pending / sent / dismissed."""
        user_id = validate_user_id(user_id)
        keys = [str(key) for key in dedup_keys if str(key)]
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        rows = self.execute(
            f"""SELECT n.dedup_key, n.status FROM outbound_notifications n
                 WHERE n.user_id=? AND n.kind='reminder'
                   AND n.dedup_key IN ({placeholders})
                   AND {_not_private_notification_dependency("n")}""",  # nosec B608
            (user_id, *keys),
        ).fetchall()
        return {str(row["dedup_key"]): str(row["status"]) for row in rows}

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
                f"""UPDATE outbound_notifications AS n
                   SET status='dismissed'
                   WHERE id=? AND user_id=? AND kind='reminder' AND status='pending'
                     AND {_not_private_notification_dependency("n")}""",  # nosec B608
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
        matter again if the chat is ever re-allowed. Strict Engineer terminal
        and sparse-progress rows are the exception: their producer needs the
        original kind/key and must never stage the same checkpoint again.
        """
        return len(self.discard_notifications_verified(ids, reason=reason))

    def discard_notifications_verified(self, ids: Sequence[str], *, reason: str) -> list[str]:
        """Retire undeliverable rows and return only ids changed by this transaction."""

        cleaned = list(dict.fromkeys(str(value) for value in ids if str(value)))
        if not cleaned:
            return []
        marker = f"undeliverable:{reason}"[:64]
        changed: list[str] = []
        with self.transaction() as conn:
            for notification_id in cleaned:
                cursor = conn.execute(
                    f"""UPDATE outbound_notifications AS n
                       SET status='failed',
                           dedup_key=CASE WHEN kind IN (
                                              'engineer_command_terminal',
                                              'engineer_command_progress'
                                          )
                                          THEN dedup_key ELSE '' END,
                           kind=CASE
                               WHEN kind IN (
                                   'engineer_command_terminal',
                                   'engineer_command_progress'
                               )
                               THEN kind ELSE ? END
                       WHERE id=? AND status='pending'
                         AND (kind IN ('engineer_command_terminal','engineer_command_progress')
                              OR {_not_private_notification_dependency("n")})""",  # nosec B608
                    (marker, notification_id),
                )
                if cursor.rowcount > 0:
                    changed.append(notification_id)
        return changed

    def acknowledge_notifications(
        self,
        sent_ids: Sequence[str] = (),
        failed_ids: Sequence[str] = (),
        uncertain_ids: Sequence[str] = (),
        *,
        max_attempts: int = 5,
    ) -> dict[str, list[str]]:
        """Apply one bridge ACK and return exact durable states under the same lock.

        The response is evidence, not an echo of caller input. It lets a bridge
        retire a pre-write delivery fence only after SQLite proves the matching
        row is terminal. ``pending`` means a failed transport attempt was
        recorded but remains retryable, so any already-confirmed strict parts
        must stay checkpointed.
        """

        sent = list(dict.fromkeys(str(value) for value in sent_ids if str(value)))
        failed = list(dict.fromkeys(str(value) for value in failed_ids if str(value)))
        uncertain = list(dict.fromkeys(str(value) for value in uncertain_ids if str(value)))
        requested = list(dict.fromkeys([*sent, *failed, *uncertain]))
        states: dict[str, list[str]] = {
            "sent": [],
            "failed": [],
            "uncertain": [],
            "pending": [],
            "dismissed": [],
            "missing": [],
            "unconfirmed": [],
        }
        attempt_cap = max(1, int(max_attempts))
        with self.transaction() as conn:
            for notif_id in sent:
                conn.execute(
                    f"""UPDATE outbound_notifications AS n SET status='sent', sent_at=?
                         WHERE id=?
                           AND (status='pending'
                                OR (kind IN (
                                        'engineer_command_terminal',
                                        'engineer_command_progress'
                                    ) AND status='failed'
                                    AND attempts < ?))
                           AND (kind IN ('engineer_command_terminal','engineer_command_progress')
                                OR {_not_private_notification_dependency("n")})""",  # nosec B608
                    (utc_now(), notif_id, attempt_cap),
                )
            for notif_id in failed:
                # A failed send stays pending for retry until the attempt cap;
                # only the ordinary terminal transition releases its dedup key.
                conn.execute(
                    f"""UPDATE outbound_notifications AS n
                       SET attempts=attempts+1,
                           status=CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END,
                           dedup_key=CASE
                               WHEN attempts + 1 >= ?
                                AND kind NOT IN (
                                    'engineer_command_terminal',
                                    'engineer_command_progress'
                                )
                               THEN '' ELSE dedup_key END
                       WHERE id=? AND status='pending'
                         AND (kind IN ('engineer_command_terminal','engineer_command_progress')
                              OR {_not_private_notification_dependency("n")})""",  # nosec B608
                    (attempt_cap, attempt_cap, notif_id),
                )
            for notif_id in uncertain:
                # Ambiguous strict delivery is terminal but not claimed sent.
                # Retaining the dedup key prevents a possibly accepted document
                # from being re-enqueued under a fresh notification id.
                conn.execute(
                    """UPDATE outbound_notifications AS n
                       SET status='uncertain', sent_at=?
                       WHERE id=? AND kind='engineer_command_terminal'
                         AND (status='pending' OR (status='failed' AND attempts < ?))
                       """,
                    (utc_now(), notif_id, attempt_cap),
                )
            for notif_id in requested:
                row = conn.execute(
                    "SELECT n.status, n.kind FROM outbound_notifications n WHERE n.id=?",
                    (notif_id,),
                ).fetchone()
                if row is None:
                    states["missing"].append(notif_id)
                    continue
                status = str(row["status"] or "")
                if str(row["kind"] or "") not in {
                    "engineer_command_terminal",
                    "engineer_command_progress",
                }:
                    visible = conn.execute(
                        f"""SELECT 1 FROM outbound_notifications n
                             WHERE n.id=? AND {_not_private_notification_dependency("n")}""",  # nosec B608
                        (notif_id,),
                    ).fetchone()
                    if visible is None:
                        states["unconfirmed"].append(notif_id)
                        continue
                if status in states and status != "unconfirmed":
                    states[status].append(notif_id)
                else:
                    states["unconfirmed"].append(notif_id)
        return states

    def mark_notifications(
        self,
        sent_ids: Sequence[str] = (),
        failed_ids: Sequence[str] = (),
        uncertain_ids: Sequence[str] = (),
        *,
        max_attempts: int = 5,
    ) -> None:
        self.acknowledge_notifications(
            sent_ids,
            failed_ids,
            uncertain_ids,
            max_attempts=max_attempts,
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
        now_text = now.isoformat(timespec="microseconds")
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
                pending_response = _json_load(row["response_json"], {})
                if pending_response.get("idempotency_effect_uncertain") is True:
                    # A durable effect fence is not an abandoned pre-effect
                    # lease.  While fresh it reports ordinary in-progress below;
                    # after process death makes it stale, freeze and replay the
                    # bounded uncertainty response instead of stealing the key
                    # and executing a possibly committed effect again.
                    conn.execute(
                        """UPDATE request_idempotency
                           SET state='complete', lease_token='', updated_at=?
                           WHERE user_id=? AND request_key=? AND state='pending'""",
                        (now_text, user_id, request_key),
                    )
                    return {
                        "status": "replay",
                        "response": pending_response,
                        "lease_token": "",
                        "stale_effect_fence_recovered": True,
                    }
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
                (datetime.now(UTC).isoformat(timespec="microseconds"), user_id, request_key, lease_token),
            )
        return cursor.rowcount == 1

    def idempotency_mark_effect_possible(
        self,
        user_id: str,
        request_key: str,
        lease_token: str,
        response: dict[str, Any],
    ) -> bool:
        """Durably make a pending request non-stealable before a possible effect.

        A normal pending lease may be recovered after its heartbeat expires.
        Once a mutating handler is about to start, however, process death can no
        longer prove that the effect did not commit.  The existing ``complete``
        response sentinel is a durable non-stealable fence: fresh concurrent
        retries still see ``in_progress``; after SIGKILL makes the lease stale,
        ``idempotency_claim`` freezes and replays the bounded uncertainty response
        instead of executing the effect twice.
        """

        if not request_key or not lease_token:
            return False
        with self.transaction() as conn:
            cursor = conn.execute(
                """UPDATE request_idempotency
                   SET response_json=?, updated_at=?
                   WHERE user_id=? AND request_key=? AND state='pending' AND lease_token=?""",
                (
                    json.dumps(response, ensure_ascii=False, sort_keys=True),
                    datetime.now(UTC).isoformat(timespec="microseconds"),
                    user_id,
                    request_key,
                    lease_token,
                ),
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
                    datetime.now(UTC).isoformat(timespec="microseconds"),
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
        now = datetime.now(UTC).isoformat(timespec="microseconds")
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
                   WHERE (
                         datetime(COALESCE(NULLIF(updated_at, ''), created_at)) < datetime('now', ?)
                         OR (
                           request_key LIKE 'secondary-product-witness-purge:%'
                           AND state='complete'
                           AND datetime(COALESCE(NULLIF(updated_at, ''), created_at))
                               < datetime('now', '-24 hours')
                         )
                       )
                     AND request_key NOT LIKE 'secondary-document-map-shadow:%'
                     AND request_key NOT LIKE 'secondary-document-map-shadow-one-shot:%'
                     AND CASE WHEN json_valid(response_json)
                              THEN COALESCE(
                                  json_extract(response_json, '$.idempotency_effect_uncertain'),
                                  0
                              )
                              ELSE 0
                         END <> 1""",
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
        event_payload = payload or {}
        owners: set[str] = set()
        user_id = event_payload.get("user_id")
        if isinstance(user_id, str) and user_id not in {"", "*"}:
            owners.add(user_id)
        user_ids = event_payload.get("user_ids")
        if isinstance(user_ids, list):
            owners.update(item for item in user_ids if isinstance(item, str) and item)
        if str(event_type) == "graph.entities_pruned" and not owners:
            raise ValueError("graph.entities_pruned requires an explicit user_ids scope")
        with self.transaction() as conn:
            for owner in owners:
                try:
                    tombstone_key = deleted_account_tombstone_key(owner)
                except ValueError:
                    if str(event_type) == "graph.entities_pruned":
                        raise
                    continue
                if conn.execute(
                    "SELECT 1 FROM runtime_kv WHERE key=?",
                    (tombstone_key,),
                ).fetchone():
                    raise DeletedAccountError(
                        "Operational history cannot be published for a permanently deleted account"
                    )
            conn.execute(
                "INSERT INTO runtime_events(id, event_type, payload, created_at) VALUES(?,?,?,?)",
                (event_id, str(event_type)[:64], json.dumps(event_payload, ensure_ascii=False), utc_now()),
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
            for owner in known_runtime_key_owners(str(key)):
                if conn.execute(
                    "SELECT 1 FROM runtime_kv WHERE key=?",
                    (deleted_account_tombstone_key(owner),),
                ).fetchone():
                    raise DeletedAccountError(
                        "Runtime state cannot be published for a permanently deleted account"
                    )
            conn.execute(
                """INSERT INTO runtime_kv(key, value, updated_at) VALUES(?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (key, value, utc_now()),
            )

    def bump_daily_counter(self, name: str, user_id: str, day: str) -> int:
        """Счётчик «сколько раз за эти сутки», атомарный. Возвращает НОВОЕ значение.

        Читать-прибавить-записать здесь нельзя: за веб-инструментами ходят
        параллельные ходы разговора и фоновые органы, и два одновременных вызова
        прочитали бы одно и то же число. Прибавление идёт ВНУТРИ SQL, поэтому
        считает база, а не питон.

        Ключ содержит сутки, поэтому вчерашний счёт не мешает сегодняшнему и
        чистить ничего не нужно: `sweep_daily_counters` уносит старое пачкой.
        """

        key = f"quota:{name}:{user_id}:{day}"
        with self.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM runtime_kv WHERE key=?",
                (deleted_account_tombstone_key(user_id),),
            ).fetchone():
                raise DeletedAccountError("Counters cannot be published for a permanently deleted account")
            row = conn.execute(
                """INSERT INTO runtime_kv(key, value, updated_at) VALUES(?, '1', ?)
                   ON CONFLICT(key) DO UPDATE SET
                       value = CAST(runtime_kv.value AS INTEGER) + 1,
                       updated_at = excluded.updated_at
                   RETURNING value""",
                (key, utc_now()),
            ).fetchone()
        return int(row["value"]) if row else 1

    def daily_counter(self, name: str, user_id: str, day: str) -> int:
        raw = self.kv_get(f"quota:{name}:{user_id}:{day}")
        try:
            return int(raw or 0)
        except ValueError:  # pragma: no cover - только при ручной правке базы
            return 0

    def sweep_daily_counters(self, name: str, *, keep_days: str) -> int:
        """Унести счётчики старее названного дня. Возвращает, сколько унесено."""

        with self.transaction() as conn:
            cursor = conn.execute(
                # Ключ кончается датой в ISO — сравнение строк здесь и есть
                # сравнение дат, ровно потому что формат фиксированной ширины.
                "DELETE FROM runtime_kv WHERE key LIKE ? AND substr(key, -10) < ?",
                (f"quota:{name}:%", keep_days),
            )
        return int(cursor.rowcount or 0)

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

    def register_data_source(
        self,
        user_id: str,
        *,
        name: str,
        kind: str,
        dsn_env: str,
        description: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        """Объявить внешнюю базу источником. Строки подключения здесь НЕТ.

        Хранится имя переменной окружения: резервные копии этой базы лежат рядом
        с архивом, а экспорт аккаунта отдаётся человеку целиком — пароль от чужой
        боевой базы уехал бы и туда, и туда.
        """

        with self.transaction() as conn:
            conn.execute(
                """INSERT INTO data_sources(name, user_id, kind, dsn_env, description,
                   created_at, created_by)
                   VALUES(?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, name) DO UPDATE SET kind=excluded.kind,
                   dsn_env=excluded.dsn_env, description=excluded.description""",
                (name, user_id, kind, dsn_env, description, utc_now(), created_by),
            )
        stored = self.get_data_source(user_id, name)
        if stored is None:  # pragma: no cover - только при гонке удаления
            raise ValueError("Источник исчез сразу после объявления")
        return stored

    def get_data_source(self, user_id: str, name: str) -> dict[str, Any] | None:
        row = self.execute(
            "SELECT * FROM data_sources WHERE name=? AND user_id=?", (name, user_id)
        ).fetchone()
        return dict(row) if row else None

    def list_data_sources(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.execute("SELECT * FROM data_sources WHERE user_id=? ORDER BY name", (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def forget_data_source(self, user_id: str, name: str) -> bool:
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM data_sources WHERE name=? AND user_id=?", (name, user_id))
        return cursor.rowcount > 0

    def touch_data_source(self, user_id: str, name: str) -> None:
        """Отметить, что источником пользовались: молчащий источник видно сразу."""

        with self.transaction() as conn:
            conn.execute(
                "UPDATE data_sources SET last_used_at=? WHERE name=? AND user_id=?",
                (utc_now(), name, user_id),
            )

    def live_service_heartbeat_age(self, *, fresh_within: float = 180.0) -> float | None:
        """Сколько секунд назад живая служба последний раз отметилась, или None.

        Нужно проходам CLI, которые ПИШУТ в базу. Два процесса, одновременно
        пишущие в одну базу SQLite, — это не теория: живой экземпляр упал
        2026-08-05 в 00:22:29 с сигналом 7 (SIGBUS) внутри libsqlite3, обращаясь
        по адресу внутри отображения `jericho.sqlite3-shm`, пока по той же базе
        шёл проход `retag-documents --apply` вторым процессом. База уцелела,
        служба поднялась сама через 19 секунд, но запросы человека в эти секунды
        оборвались.

        Отметку ведут воркеры (`workers:health:*`), и она не про здоровье
        конкретного воркера, а про то, что процесс службы ЖИВ и держит базу.
        `PRAGMA persist_wal` тут не помогает: такой прагмы в SQLite нет вовсе,
        неизвестные прагмы молча игнорируются — проверено исполнением.
        """

        rows = self.kv_list_prefix("workers:health:")
        if not rows:
            return None
        newest = max(str(row.get("updated_at") or "") for row in rows)
        if not newest:
            return None
        try:
            stamp = datetime.fromisoformat(newest.replace("Z", "+00:00"))
        except ValueError:
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - stamp).total_seconds()
        return age if age <= fresh_within else None

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

    def create_monitor(
        self, user_id: str, query: str, *, chat_id: str = "", created_by: str = ""
    ) -> dict[str, Any]:
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
        # Потолок считается НА ЧЕЛОВЕКА, а не на архив: в общем архиве иначе
        # один участник исчерпывал бы лимит для всех остальных.
        active = self.execute(
            "SELECT COUNT(*) AS count FROM monitors WHERE user_id=? AND active=1 AND created_by=?",
            (user_id, str(created_by or "")),
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
                """INSERT INTO monitors(id, user_id, created_by, query, chat_id, active,
                   last_seen_rowid, last_seen_at, last_checked_at, matches_reported, created_at)
                   VALUES(?, ?, ?, ?, ?, 1, ?, ?, NULL, 0, ?)""",
                (
                    monitor_id,
                    user_id,
                    str(created_by or ""),
                    clean,
                    str(chat_id or ""),
                    int(row["cursor"] or 0),
                    now,
                    now,
                ),
            )
        return self.get_monitor(monitor_id, user_id, created_by=created_by) or {}

    def get_monitor(
        self, monitor_id: str, user_id: str, *, created_by: str | None = None
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM monitors WHERE id=? AND user_id=?"
        params: list[Any] = [monitor_id, user_id]
        if created_by is not None:
            query += " AND created_by=?"
            params.append(str(created_by or ""))
        row = self.execute(query, tuple(params)).fetchone()
        return dict(row) if row else None

    def list_monitors(
        self, user_id: str, *, active_only: bool = True, created_by: str | None = None
    ) -> list[dict[str, Any]]:
        """Слежения арендатора, а при `created_by` — только ЭТОГО человека.

        В общем архиве `user_id` один на всех, и без второй границы «свои
        слежения» означало «все слежения»: участник читал чужие темы, а текст
        запроса — это личный интерес («увольнение такого-то»). Найдено ревью
        2026-08-04.

        `None` означает «без разбора автора» и оставлен для владельца и фонового
        обхода: первому надзор положен, второму нужны все.
        """
        query = "SELECT * FROM monitors WHERE user_id=?"
        params: list[Any] = [user_id]
        if created_by is not None:
            query += " AND created_by=?"
            params.append(str(created_by or ""))
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

    def stop_monitor(self, monitor_id: str, user_id: str, *, created_by: str | None = None) -> bool:
        """Снять слежение. При `created_by` — только своё.

        Без этой границы участник снимал чужое слежение по идентификатору из
        общего списка, и владелец переставал получать то, за чем следил, без
        всякого следа.
        """
        query = "UPDATE monitors SET active=0 WHERE id=? AND user_id=? AND active=1"
        params: list[Any] = [monitor_id, user_id]
        if created_by is not None:
            query += " AND created_by=?"
            params.append(str(created_by or ""))
        with self.transaction() as conn:
            cursor = conn.execute(query, tuple(params))
        return cursor.rowcount == 1

    def latest_monitor_notification(self, user_id: str, monitor_id: str) -> dict[str, Any] | None:
        """Последняя строка очереди этого монитора: что с ней стало.

        Курсор монитора двигался по факту ПОСТАНОВКИ в очередь, а не доставки.
        Любое терминальное завершение строки — исчерпанные попытки при
        недоступном Telegram, недоставляемый чат — означало потерю совпадения
        навсегда: материал с rowid не больше курсора больше не читается. При этом
        `/watching` бодро показывал «сообщений: 1». У напоминаний этот случай
        закрыт (они выводятся из таймлайна заново каждый скан), у мониторов
        эквивалента не было.
        """
        user_id = validate_user_id(user_id)
        row = self.execute(
            f"""SELECT n.id, n.status, n.dedup_key, n.attempts
                  FROM outbound_notifications n
                 WHERE n.user_id=? AND n.kind='monitor' AND n.dedup_key LIKE ?
                   AND {_not_private_notification_dependency("n")}
                 ORDER BY n.created_at DESC, n.id DESC LIMIT 1""",  # nosec B608
            (user_id, f"monitor:{monitor_id}:%"),
        ).fetchone()
        return dict(row) if row else None

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
