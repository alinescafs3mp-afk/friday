"""Closed, content-free carriers for sparse Engineer command progress."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from friday.storage.models import new_id, utc_now
from friday.user_ids import USER_ID_RE

PROGRESS_NOTIFICATION_KIND = "engineer_command_progress"
PROGRESS_ENVELOPE_SCHEMA = "friday.engineer-command-progress.v2"
_LEGACY_PROGRESS_ENVELOPE_SCHEMA = "friday.engineer-command-progress.v1"
PROGRESS_CHECKPOINTS_SEC = (60, 300, 900, 1800)

_ENVELOPE_MAX_BYTES = 4 * 1024
_MAX_COUNTER = 2**63 - 1
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_CONVERSATION_ID = re.compile(r"conv_[0-9a-f]{16}")
_NOTIFICATION_ID = re.compile(r"notif_[0-9a-f]{16}")
_CHAT_ID = re.compile(r"[1-9][0-9]{0,19}")


class ProgressDeliveryError(RuntimeError):
    """A sparse progress carrier failed closed validation."""

    def __init__(self, code: str) -> None:
        clean = str(code or "progress_delivery_failed")
        self.code = clean if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", clean) else "progress_delivery_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class StagedProgressNotification:
    job_id: str
    checkpoint_sec: int
    notification_id: str
    dedup_key: str
    envelope_sha256: str
    status: str


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProgressDeliveryError("progress_envelope_invalid") from exc
    if len(encoded.encode("ascii")) > _ENVELOPE_MAX_BYTES:
        raise ProgressDeliveryError("progress_envelope_too_large")
    return encoded


def _valid_counter(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= _MAX_COUNTER


def progress_dedup_key(job_id: str, checkpoint_sec: int) -> str:
    """Return the sole durable identity for one job/checkpoint pair."""

    if _JOB_ID.fullmatch(str(job_id or "")) is None or checkpoint_sec not in PROGRESS_CHECKPOINTS_SEC:
        raise ProgressDeliveryError("progress_identity_invalid")
    return f"engineer-progress:v1:{job_id}:{checkpoint_sec}"


def parse_progress_envelope(value: object) -> dict[str, Any]:
    """Parse one exact canonical carrier; aliases and extra facts are refused."""

    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _ENVELOPE_MAX_BYTES:
        raise ProgressDeliveryError("progress_envelope_invalid")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProgressDeliveryError("progress_envelope_invalid") from exc
    if not isinstance(parsed, dict) or _canonical_json(parsed) != value:
        raise ProgressDeliveryError("progress_envelope_noncanonical")
    common_shape = {
        "actor_id",
        "checkpoint_sec",
        "conversation_id",
        "delivery_chat_id",
        "job_id",
        "notification_id",
        "output_activity",
        "schema",
        "status",
        "stderr_bytes",
        "stdout_bytes",
        "tenant_id",
    }
    schema = parsed.get("schema")
    expected_shape = (
        common_shape
        if schema == _LEGACY_PROGRESS_ENVELOPE_SCHEMA
        else common_shape | {"elapsed_sec", "remaining_sec", "stage", "timeout_sec"}
    )
    if set(parsed) != expected_shape:
        raise ProgressDeliveryError("progress_envelope_shape_invalid")
    if (
        schema not in {PROGRESS_ENVELOPE_SCHEMA, _LEGACY_PROGRESS_ENVELOPE_SCHEMA}
        or parsed.get("status") != "running"
        or not isinstance(parsed.get("actor_id"), str)
        or USER_ID_RE.fullmatch(parsed["actor_id"]) is None
        or not isinstance(parsed.get("tenant_id"), str)
        or USER_ID_RE.fullmatch(parsed["tenant_id"]) is None
        or _CONVERSATION_ID.fullmatch(str(parsed.get("conversation_id") or "")) is None
        or _NOTIFICATION_ID.fullmatch(str(parsed.get("notification_id") or "")) is None
        or _JOB_ID.fullmatch(str(parsed.get("job_id") or "")) is None
        or _CHAT_ID.fullmatch(str(parsed.get("delivery_chat_id") or "")) is None
        or parsed.get("checkpoint_sec") not in PROGRESS_CHECKPOINTS_SEC
        or not _valid_counter(parsed.get("stdout_bytes"))
        or not _valid_counter(parsed.get("stderr_bytes"))
        or not isinstance(parsed.get("output_activity"), bool)
    ):
        raise ProgressDeliveryError("progress_envelope_invalid")
    if schema == PROGRESS_ENVELOPE_SCHEMA:
        remaining_sec = parsed.get("remaining_sec")
        if (
            parsed.get("stage") != "command_running"
            or not _valid_counter(parsed.get("elapsed_sec"))
            or not _valid_counter(parsed.get("timeout_sec"))
            or (remaining_sec is not None and not _valid_counter(remaining_sec))
            or int(parsed["elapsed_sec"]) < int(parsed["checkpoint_sec"])
            or (int(parsed["timeout_sec"]) == 0 and remaining_sec is not None)
            or (
                int(parsed["timeout_sec"]) > 0
                and remaining_sec != max(0, int(parsed["timeout_sec"]) - int(parsed["elapsed_sec"]))
            )
        ):
            raise ProgressDeliveryError("progress_envelope_invalid")
    return parsed


def _progress_text(envelope: Mapping[str, Any]) -> str:
    if envelope.get("schema") == PROGRESS_ENVELOPE_SCHEMA:
        elapsed_sec = int(envelope["elapsed_sec"])
        elapsed = f"{elapsed_sec // 60} мин {elapsed_sec % 60} с" if elapsed_sec >= 60 else f"{elapsed_sec} с"
        timeout_sec = int(envelope["timeout_sec"])
        if timeout_sec:
            remaining_sec = int(envelope["remaining_sec"])
            remaining = (
                f"{remaining_sec // 60} мин {remaining_sec % 60} с"
                if remaining_sec >= 60
                else f"{remaining_sec} с"
            )
            deadline = f"До заданного тайм-аута: около {remaining}."
        else:
            deadline = "Жёсткий тайм-аут не задан."
        output = (
            f"Получено вывода: stdout {envelope['stdout_bytes']} Б, stderr {envelope['stderr_bytes']} Б."
            if envelope["output_activity"]
            else "Текстового вывода пока нет."
        )
        return (
            f"⏳ Engineer-задача `{envelope['job_id']}` выполняется {elapsed}. "
            f"Этап: выполняется команда. {output} {deadline}"
        )
    activity = "да" if envelope["output_activity"] else "нет"
    return (
        f"Engineer-задача {envelope['job_id']} на момент проверки выполнялась не менее "
        f"{envelope['checkpoint_sec']} с. stdout: {envelope['stdout_bytes']} байт; "
        f"stderr: {envelope['stderr_bytes']} байт; активность вывода: {activity}."
    )


def _exact_envelope(
    row: Mapping[str, Any],
    *,
    actor_id: str,
    tenant_id: str,
    conversation_id: str,
    delivery_chat_id: str,
    job_id: str,
    checkpoint_sec: int,
) -> dict[str, Any]:
    envelope = parse_progress_envelope(row.get("body"))
    expected = {
        "actor_id": actor_id,
        "checkpoint_sec": checkpoint_sec,
        "conversation_id": conversation_id,
        "delivery_chat_id": delivery_chat_id,
        "job_id": job_id,
        "notification_id": str(row.get("id") or ""),
        "status": "running",
        "tenant_id": tenant_id,
    }
    dedup_key = progress_dedup_key(job_id, checkpoint_sec)
    if (
        row.get("kind") != PROGRESS_NOTIFICATION_KIND
        or str(row.get("user_id") or "") != actor_id
        or str(row.get("chat_id") or "") != delivery_chat_id
        or not hmac.compare_digest(str(row.get("dedup_key") or ""), dedup_key)
        or any(envelope.get(key) != expected_value for key, expected_value in expected.items())
    ):
        raise ProgressDeliveryError("progress_dedup_conflict")
    return envelope


def stage_progress_notification(
    storage: Any,
    *,
    actor_id: str,
    tenant_id: str,
    conversation_id: str,
    delivery_chat_id: str,
    job_id: str,
    checkpoint_sec: int,
    stdout_bytes: int,
    stderr_bytes: int,
    output_activity: bool,
    elapsed_sec: int | None = None,
    timeout_sec: int = 0,
) -> StagedProgressNotification:
    """Stage or prove the exact durable carrier before a producer checkpoint CAS."""

    dedup_key = progress_dedup_key(job_id, checkpoint_sec)
    # Validate caller values through the same parser that owns bridge projection.
    elapsed = checkpoint_sec if elapsed_sec is None else elapsed_sec
    if not _valid_counter(elapsed) or int(elapsed) < checkpoint_sec or not _valid_counter(timeout_sec):
        raise ProgressDeliveryError("progress_envelope_invalid")
    remaining_sec = None if timeout_sec == 0 else max(0, int(timeout_sec) - int(elapsed))
    probe = {
        "schema": PROGRESS_ENVELOPE_SCHEMA,
        "notification_id": "notif_" + "0" * 16,
        "actor_id": actor_id,
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "delivery_chat_id": delivery_chat_id,
        "job_id": job_id,
        "status": "running",
        "checkpoint_sec": checkpoint_sec,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "output_activity": output_activity,
        "elapsed_sec": int(elapsed),
        "remaining_sec": remaining_sec,
        "stage": "command_running",
        "timeout_sec": int(timeout_sec),
    }
    parse_progress_envelope(_canonical_json(probe))

    with storage.transaction() as conn:
        existing = conn.execute(
            """SELECT n.id,n.user_id,n.chat_id,n.kind,n.dedup_key,n.body,n.status
                  FROM outbound_notifications AS n
                 WHERE n.chat_id=? AND n.dedup_key=?""",
            (delivery_chat_id, dedup_key),
        ).fetchone()
        if existing is not None:
            row = dict(existing)
            _exact_envelope(
                row,
                actor_id=actor_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                delivery_chat_id=delivery_chat_id,
                job_id=job_id,
                checkpoint_sec=checkpoint_sec,
            )
            body = str(row["body"])
            return StagedProgressNotification(
                job_id=job_id,
                checkpoint_sec=checkpoint_sec,
                notification_id=str(row["id"]),
                dedup_key=dedup_key,
                envelope_sha256=hashlib.sha256(body.encode("ascii")).hexdigest(),
                status=str(row["status"]),
            )

        conversation = conn.execute(
            "SELECT 1 FROM conversations WHERE id=? AND user_id=?",
            (conversation_id, actor_id),
        ).fetchone()
        if conversation is None:
            raise ProgressDeliveryError("progress_conversation_unavailable")
        notification_id = new_id("notif")
        envelope = {**probe, "notification_id": notification_id}
        body = _canonical_json(envelope)
        parse_progress_envelope(body)
        cursor = conn.execute(
            """INSERT INTO outbound_notifications(
                   id,user_id,chat_id,kind,dedup_key,body,status,attempts,created_at)
               VALUES(?,?,?,?,?,?,'pending',0,?)""",
            (
                notification_id,
                actor_id,
                delivery_chat_id,
                PROGRESS_NOTIFICATION_KIND,
                dedup_key,
                body,
                utc_now(),
            ),
        )
        if cursor.rowcount != 1:
            raise ProgressDeliveryError("progress_notification_not_staged")
    return StagedProgressNotification(
        job_id=job_id,
        checkpoint_sec=checkpoint_sec,
        notification_id=notification_id,
        dedup_key=dedup_key,
        envelope_sha256=hashlib.sha256(body.encode("ascii")).hexdigest(),
        status="pending",
    )


def progress_notification_projection(
    storage: Any,
    row: Mapping[str, Any],
    *,
    tenant_id: str,
    actor_id: str,
) -> dict[str, str]:
    """Project only code-owned facts after the API rebuilt current authority."""

    envelope = parse_progress_envelope(row.get("body"))
    _exact_envelope(
        row,
        actor_id=actor_id,
        tenant_id=tenant_id,
        conversation_id=str(envelope["conversation_id"]),
        delivery_chat_id=str(envelope["delivery_chat_id"]),
        job_id=str(envelope["job_id"]),
        checkpoint_sec=int(envelope["checkpoint_sec"]),
    )
    if row.get("status") not in {None, "pending"}:
        raise ProgressDeliveryError("progress_notification_unavailable")
    conversation = storage.get_conversation(str(envelope["conversation_id"]), actor_id)
    if not isinstance(conversation, Mapping):
        raise ProgressDeliveryError("progress_conversation_unavailable")
    return {"body": _progress_text(envelope)}


def retire_pending_progress_notifications(
    conn: Any,
    *,
    actor_id: str,
    tenant_id: str,
    conversation_id: str,
    delivery_chat_id: str,
    job_id: str,
) -> list[str]:
    """Atomically retire the at-most-four exact pending notices before terminal publish."""

    prefix = f"engineer-progress:v1:{job_id}:"
    progress_dedup_key(job_id, PROGRESS_CHECKPOINTS_SEC[0])
    rows = conn.execute(
        """SELECT id,user_id,chat_id,kind,dedup_key,body,status
              FROM outbound_notifications
             WHERE user_id=? AND chat_id=? AND kind=? AND status='pending'
               AND dedup_key LIKE ?
             ORDER BY created_at ASC LIMIT 5""",
        (actor_id, delivery_chat_id, PROGRESS_NOTIFICATION_KIND, prefix + "%"),
    ).fetchall()
    if len(rows) > len(PROGRESS_CHECKPOINTS_SEC):
        raise ProgressDeliveryError("progress_retirement_overflow")
    verified: list[tuple[str, str]] = []
    for raw in rows:
        row = dict(raw)
        envelope = parse_progress_envelope(row.get("body"))
        checkpoint_sec = int(envelope["checkpoint_sec"])
        _exact_envelope(
            row,
            actor_id=actor_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            delivery_chat_id=delivery_chat_id,
            job_id=job_id,
            checkpoint_sec=checkpoint_sec,
        )
        verified.append((str(row["id"]), str(row["body"])))
    retired: list[str] = []
    for notification_id, body in verified:
        changed = conn.execute(
            """UPDATE outbound_notifications
                  SET status='failed'
                WHERE id=? AND kind=? AND status='pending' AND body=?""",
            (notification_id, PROGRESS_NOTIFICATION_KIND, body),
        )
        if changed.rowcount != 1:
            raise ProgressDeliveryError("progress_retirement_race")
        retired.append(notification_id)
    return retired


__all__ = [
    "PROGRESS_CHECKPOINTS_SEC",
    "PROGRESS_ENVELOPE_SCHEMA",
    "PROGRESS_NOTIFICATION_KIND",
    "ProgressDeliveryError",
    "StagedProgressNotification",
    "parse_progress_envelope",
    "progress_dedup_key",
    "progress_notification_projection",
    "retire_pending_progress_notifications",
    "stage_progress_notification",
]
