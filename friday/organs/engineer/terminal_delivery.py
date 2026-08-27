"""Durable, content-free carrier for automatic Engineer terminal delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from friday.file_delivery import (
    AuthorizedFileBytes,
    AuthorizedFileReadError,
    FileRecordUnavailable,
    read_authorized_generated_file,
)
from friday.generated_files import (
    GeneratedFilePersistenceError,
    GeneratedFilesPersistenceRollbackGuard,
    generated_file_descriptor,
    generated_files_publication_transaction,
)
from friday.organs.engineer.publication import (
    ExactGeneratedFileBatch,
    ExactGeneratedFilePublicationError,
    ExpectedGeneratedFile,
    persist_exact_generated_file_batch,
)
from friday.storage._conversations import store_message_in_transaction
from friday.storage._privacy import _not_private_raw_dependency
from friday.storage.models import new_id

TERMINAL_NOTIFICATION_KIND = "engineer_command_terminal"
TERMINAL_ENVELOPE_SCHEMA = "friday.engineer-command-terminal.v1"

_JOB_ID = re.compile(r"[0-9a-f]{32}")
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}")
_NOTIFICATION_ID = re.compile(r"notif_[0-9a-f]{16}")
_RAW_ID = re.compile(r"raw_[0-9a-f]{16}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CHAT_ID = re.compile(r"[1-9][0-9]{0,19}")
_MIME_TYPE = re.compile(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timeout"})
_ENVELOPE_MAX_BYTES = 8 * 1024
_CAPTION_MAX_CHARS = 900
_TERMINAL_NOTIFICATION_MAX_ATTEMPTS = 5


class TerminalDeliveryError(RuntimeError):
    """Content-free terminal publication failure."""

    def __init__(self, code: str) -> None:
        clean = str(code or "terminal_delivery_failed")
        self.code = clean if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", clean) else "terminal_delivery_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class StagedTerminalNotification:
    job_id: str
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
        raise TerminalDeliveryError("terminal_envelope_invalid") from exc
    if len(encoded.encode("ascii")) > _ENVELOPE_MAX_BYTES:
        raise TerminalDeliveryError("terminal_envelope_too_large")
    return encoded


def _safe_caption(job_id: str, status: str, *, has_artifact: bool) -> str:
    status_text = {
        "completed": "завершено",
        "failed": "завершилось с ошибкой",
        "cancelled": "отменено",
        "timeout": "остановлено по тайм-ауту",
    }.get(status)
    if status_text is None or _JOB_ID.fullmatch(job_id) is None:
        raise TerminalDeliveryError("terminal_identity_invalid")
    suffix = " Проверенный архив результата приложен." if has_artifact else ""
    value = f"Engineer-задание {job_id} {status_text}.{suffix}"
    if not value or len(value) > _CAPTION_MAX_CHARS:
        raise TerminalDeliveryError("terminal_caption_invalid")
    return value


def terminal_dedup_key(job_id: str, receipt_mac: str, *, has_artifact: bool) -> str:
    if _JOB_ID.fullmatch(job_id) is None or _SHA256.fullmatch(receipt_mac) is None:
        raise TerminalDeliveryError("terminal_identity_invalid")
    lane = "archive" if has_artifact else "text"
    return f"engineer-terminal:{lane}:{job_id}:{receipt_mac}"


def parse_terminal_envelope(value: object) -> dict[str, Any]:
    """Parse one exact canonical envelope; caller-shaped aliases are refused."""

    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > _ENVELOPE_MAX_BYTES:
        raise TerminalDeliveryError("terminal_envelope_invalid")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, RecursionError) as exc:
        raise TerminalDeliveryError("terminal_envelope_invalid") from exc
    if not isinstance(parsed, dict) or _canonical_json(parsed) != value:
        raise TerminalDeliveryError("terminal_envelope_noncanonical")
    if set(parsed) != {
        "actor_id",
        "artifact",
        "assistant_message_id",
        "caption",
        "conversation_id",
        "delivery_chat_id",
        "job_id",
        "notification_id",
        "receipt_mac",
        "schema",
        "source_message_id",
        "status",
        "tenant_id",
    }:
        raise TerminalDeliveryError("terminal_envelope_shape_invalid")
    artifact = parsed.get("artifact")
    if not isinstance(artifact, dict) or set(artifact) != {
        "filename",
        "mime_type",
        "raw_id",
        "sha256",
        "size_bytes",
    }:
        raise TerminalDeliveryError("terminal_envelope_shape_invalid")
    filename = artifact.get("filename")
    mime_type = artifact.get("mime_type")
    size_bytes = artifact.get("size_bytes")
    if (
        parsed.get("schema") != TERMINAL_ENVELOPE_SCHEMA
        or not isinstance(parsed.get("actor_id"), str)
        or not parsed["actor_id"]
        or not isinstance(parsed.get("tenant_id"), str)
        or not parsed["tenant_id"]
        or not isinstance(parsed.get("conversation_id"), str)
        or not parsed["conversation_id"]
        or _MESSAGE_ID.fullmatch(str(parsed.get("source_message_id") or "")) is None
        or _MESSAGE_ID.fullmatch(str(parsed.get("assistant_message_id") or "")) is None
        or _NOTIFICATION_ID.fullmatch(str(parsed.get("notification_id") or "")) is None
        or _JOB_ID.fullmatch(str(parsed.get("job_id") or "")) is None
        or _SHA256.fullmatch(str(parsed.get("receipt_mac") or "")) is None
        or _CHAT_ID.fullmatch(str(parsed.get("delivery_chat_id") or "")) is None
        or parsed.get("status") not in _TERMINAL_STATUSES
        or not isinstance(parsed.get("caption"), str)
        or not parsed["caption"]
        or len(parsed["caption"]) > _CAPTION_MAX_CHARS
        or _RAW_ID.fullmatch(str(artifact.get("raw_id") or "")) is None
        or not isinstance(filename, str)
        or not filename
        or filename != filename.strip()
        or len(filename) > 128
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or not isinstance(mime_type, str)
        or _MIME_TYPE.fullmatch(mime_type) is None
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
        or _SHA256.fullmatch(str(artifact.get("sha256") or "")) is None
    ):
        raise TerminalDeliveryError("terminal_envelope_invalid")
    return parsed


def _existing_notification(storage: Any, *, chat_id: str, dedup_key: str) -> dict[str, Any] | None:
    row = storage.execute(
        """SELECT n.id,n.user_id,n.chat_id,n.kind,n.dedup_key,n.body,n.status
              FROM outbound_notifications AS n
             WHERE n.chat_id=? AND n.dedup_key=?
               AND n.kind='engineer_command_terminal'""",
        (str(chat_id), str(dedup_key)),
    ).fetchone()
    return dict(row) if row is not None else None


def _exact_existing_archive(
    storage: Any,
    row: Mapping[str, Any],
    *,
    actor_id: str,
    tenant_id: str,
    conversation_id: str,
    source_message_id: str,
    delivery_chat_id: str,
    job_id: str,
    status: str,
    receipt_mac: str,
    expected_artifact: ExpectedGeneratedFile,
) -> StagedTerminalNotification:
    expected_dedup_key = terminal_dedup_key(job_id, receipt_mac, has_artifact=True)
    if (
        row.get("kind") != TERMINAL_NOTIFICATION_KIND
        or row.get("user_id") != actor_id
        or str(row.get("chat_id") or "") != delivery_chat_id
        or not hmac.compare_digest(str(row.get("dedup_key") or ""), expected_dedup_key)
    ):
        raise TerminalDeliveryError("terminal_dedup_conflict")
    envelope = parse_terminal_envelope(row.get("body"))
    artifact = envelope["artifact"]
    expected = {
        "actor_id": actor_id,
        "tenant_id": tenant_id,
        "conversation_id": conversation_id,
        "source_message_id": source_message_id,
        "delivery_chat_id": delivery_chat_id,
        "job_id": job_id,
        "status": status,
        "receipt_mac": receipt_mac,
        "notification_id": str(row.get("id") or ""),
    }
    if any(envelope.get(key) != expected_value for key, expected_value in expected.items()):
        raise TerminalDeliveryError("terminal_dedup_conflict")
    if (
        artifact.get("filename") != expected_artifact.filename
        or artifact.get("mime_type") != expected_artifact.mime_type
        or artifact.get("size_bytes") != expected_artifact.size_bytes
        or not hmac.compare_digest(
            str(artifact.get("sha256") or ""),
            expected_artifact.content_sha256,
        )
    ):
        raise TerminalDeliveryError("terminal_artifact_replay_changed")
    terminal_notification_projection(
        storage,
        row,
        tenant_id=tenant_id,
        actor_id=actor_id,
    )
    digest = hashlib.sha256(str(row["body"]).encode("ascii")).hexdigest()
    return StagedTerminalNotification(
        job_id, str(row["id"]), str(row["dedup_key"]), digest, str(row["status"])
    )


def stage_terminal_archive(
    storage: Any,
    files_root: Path,
    *,
    actor_id: str,
    tenant_id: str,
    conversation_id: str,
    source_message_id: str,
    delivery_chat_id: str,
    job_id: str,
    status: str,
    receipt_mac: str,
    attachment: Mapping[str, Any],
    batch: ExactGeneratedFileBatch,
    max_bytes: int,
) -> StagedTerminalNotification:
    """Atomically freeze ZIP, assistant receipt and content-free queue pointer."""

    if type(batch) is not ExactGeneratedFileBatch or len(batch.files) != 1:
        raise TerminalDeliveryError("terminal_artifact_batch_invalid")
    expected_artifact = batch.files[0]
    dedup_key = terminal_dedup_key(job_id, receipt_mac, has_artifact=True)
    existing = _existing_notification(storage, chat_id=delivery_chat_id, dedup_key=dedup_key)
    if existing is not None:
        return _exact_existing_archive(
            storage,
            existing,
            actor_id=actor_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            delivery_chat_id=delivery_chat_id,
            job_id=job_id,
            status=status,
            receipt_mac=receipt_mac,
            expected_artifact=expected_artifact,
        )

    notification_id = new_id("notif")
    caption = _safe_caption(job_id, status, has_artifact=True)
    guard = GeneratedFilesPersistenceRollbackGuard(Path(files_root))
    try:
        with generated_files_publication_transaction(storage, guard) as conn:
            raced = conn.execute(
                "SELECT id FROM outbound_notifications WHERE chat_id=? AND dedup_key=?",
                (delivery_chat_id, dedup_key),
            ).fetchone()
            if raced is not None:
                raise TerminalDeliveryError("terminal_dedup_race")
            source = conn.execute(
                """SELECT id FROM messages
                     WHERE id=? AND conversation_id=? AND user_id=? AND role='user'""",
                (source_message_id, conversation_id, actor_id),
            ).fetchone()
            if source is None:
                raise TerminalDeliveryError("terminal_source_unavailable")
            terminal_metadata = {
                "schema": TERMINAL_ENVELOPE_SCHEMA,
                "notification_id": notification_id,
                "job_id": job_id,
                "status": status,
                "receipt_mac": receipt_mac,
                "delivery_chat_id": delivery_chat_id,
                "source_message_id": source_message_id,
            }
            assistant = store_message_in_transaction(
                conn,
                conversation_id,
                actor_id,
                "assistant",
                caption,
                metadata={
                    "engineer_command_terminal": terminal_metadata,
                    "tools_used": ["engineer_command_run"],
                },
                reply_to=source_message_id,
            )
            assistant_message_id = str(assistant.get("id") or "")
            publication = persist_exact_generated_file_batch(
                storage,
                Path(files_root),
                {"message_id": assistant_message_id, "files": [dict(attachment)]},
                batch,
                tenant_id=tenant_id,
                person_id=actor_id,
                max_bytes=max_bytes,
                rollback_guard=guard,
            )
            files = publication.response.get("files")
            if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], Mapping):
                raise TerminalDeliveryError("terminal_artifact_persistence_failed")
            artifact = files[0]
            envelope = {
                "schema": TERMINAL_ENVELOPE_SCHEMA,
                "notification_id": notification_id,
                "actor_id": actor_id,
                "tenant_id": tenant_id,
                "conversation_id": conversation_id,
                "source_message_id": source_message_id,
                "assistant_message_id": assistant_message_id,
                "delivery_chat_id": delivery_chat_id,
                "job_id": job_id,
                "status": status,
                "receipt_mac": receipt_mac,
                "caption": caption,
                "artifact": {
                    "raw_id": artifact.get("id"),
                    "filename": artifact.get("filename"),
                    "mime_type": artifact.get("mime_type"),
                    "size_bytes": artifact.get("size_bytes"),
                    "sha256": artifact.get("sha256"),
                },
            }
            body = _canonical_json(envelope)
            parse_terminal_envelope(body)
            cursor = conn.execute(
                """INSERT INTO outbound_notifications(
                       id,user_id,chat_id,kind,dedup_key,body,status,attempts,created_at)
                   VALUES(?,?,?,?,?,?,'pending',0,strftime('%Y-%m-%dT%H:%M:%SZ','now'))""",
                (
                    notification_id,
                    actor_id,
                    delivery_chat_id,
                    TERMINAL_NOTIFICATION_KIND,
                    dedup_key,
                    body,
                ),
            )
            if cursor.rowcount != 1:
                raise TerminalDeliveryError("terminal_notification_not_staged")
            visible = conn.execute(
                """SELECT 1 FROM outbound_notifications AS n
                     WHERE n.id=? AND n.kind='engineer_command_terminal'
                       AND n.status='pending'""",
                (notification_id,),
            ).fetchone()
            if visible is None:
                raise TerminalDeliveryError("terminal_notification_quarantined")
    except ExactGeneratedFilePublicationError as exc:
        raise TerminalDeliveryError(exc.code) from exc
    except GeneratedFilePersistenceError as exc:
        raise TerminalDeliveryError("terminal_artifact_persistence_failed") from exc
    return StagedTerminalNotification(
        job_id,
        notification_id,
        dedup_key,
        hashlib.sha256(body.encode("ascii")).hexdigest(),
        "pending",
    )


def terminal_notification_status(
    storage: Any,
    notification_id: str,
    dedup_key: str,
    envelope_sha256: str,
) -> str:
    row = storage.execute(
        "SELECT status,kind,dedup_key,body,attempts FROM outbound_notifications WHERE id=?",
        (str(notification_id),),
    ).fetchone()
    if row is None:
        return "missing"
    body = str(row["body"] or "")
    try:
        body_sha256 = hashlib.sha256(body.encode("ascii")).hexdigest()
    except UnicodeEncodeError:
        return "invalid"
    if (
        str(row["kind"] or "") != TERMINAL_NOTIFICATION_KIND
        or not hmac.compare_digest(str(row["dedup_key"] or ""), str(dedup_key))
        or not hmac.compare_digest(body_sha256, str(envelope_sha256))
    ):
        return "invalid"
    status = str(row["status"] or "")
    # A strict row can be retired by a fresh authority check while a previously
    # delivered archive's ACK is still in flight.  Unlike five proven Telegram
    # rejections, that low-attempt `failed` state cannot prove non-delivery.
    if status == "failed" and int(row["attempts"] or 0) < _TERMINAL_NOTIFICATION_MAX_ATTEMPTS:
        return "uncertain"
    return status if status in {"pending", "sent", "uncertain", "failed"} else "invalid"


def terminal_notification_projection(
    storage: Any,
    row: Mapping[str, Any],
    *,
    tenant_id: str,
    actor_id: str,
) -> dict[str, Any]:
    """Revalidate the content-free pending envelope for the bridge listing."""

    envelope = parse_terminal_envelope(row.get("body"))
    expected_dedup_key = terminal_dedup_key(
        envelope["job_id"],
        envelope["receipt_mac"],
        has_artifact=True,
    )
    if (
        row.get("kind") != TERMINAL_NOTIFICATION_KIND
        or row.get("status") not in {None, "pending", "sent", "uncertain", "failed"}
        or str(row.get("id") or "") != envelope["notification_id"]
        or str(row.get("user_id") or "") != actor_id
        or str(row.get("chat_id") or "") != envelope["delivery_chat_id"]
        or envelope["actor_id"] != actor_id
        or envelope["tenant_id"] != tenant_id
        or not hmac.compare_digest(str(row.get("dedup_key") or ""), expected_dedup_key)
    ):
        raise TerminalDeliveryError("terminal_scope_changed")
    descriptor = envelope["artifact"]
    current_descriptor = generated_file_descriptor(
        storage,
        descriptor["raw_id"],
        tenant_id=tenant_id,
        person_id=actor_id,
    )
    if (
        not isinstance(current_descriptor, Mapping)
        or current_descriptor.get("id") != descriptor.get("raw_id")
        or any(
            current_descriptor.get(key) != descriptor.get(key)
            for key in ("filename", "mime_type", "size_bytes", "sha256")
        )
    ):
        raise TerminalDeliveryError("terminal_artifact_unavailable")
    source = storage.get_message(str(envelope["source_message_id"]), actor_id)
    if (
        not isinstance(source, Mapping)
        or str(source.get("conversation_id") or "") != envelope["conversation_id"]
        or str(source.get("role") or "") != "user"
    ):
        raise TerminalDeliveryError("terminal_source_changed")
    message = storage.get_message(str(envelope["assistant_message_id"]), actor_id)
    if (
        not isinstance(message, Mapping)
        or str(message.get("conversation_id") or "") != envelope["conversation_id"]
        or str(message.get("role") or "") != "assistant"
        or str(message.get("content") or "") != envelope["caption"]
    ):
        raise TerminalDeliveryError("terminal_message_changed")
    try:
        metadata = json.loads(str(message.get("metadata_json") or ""))
    except (TypeError, ValueError, RecursionError) as exc:
        raise TerminalDeliveryError("terminal_message_changed") from exc
    terminal = metadata.get("engineer_command_terminal") if isinstance(metadata, Mapping) else None
    files = metadata.get("generated_files") if isinstance(metadata, Mapping) else None
    if (
        not isinstance(terminal, Mapping)
        or terminal.get("schema") != TERMINAL_ENVELOPE_SCHEMA
        or terminal.get("notification_id") != envelope["notification_id"]
        or terminal.get("job_id") != envelope["job_id"]
        or terminal.get("status") != envelope["status"]
        or terminal.get("receipt_mac") != envelope["receipt_mac"]
        or terminal.get("delivery_chat_id") != envelope["delivery_chat_id"]
        or terminal.get("source_message_id") != envelope["source_message_id"]
        or not isinstance(files, list)
        or len(files) != 1
        or not isinstance(files[0], Mapping)
        or files[0].get("id") != descriptor.get("raw_id")
        or any(
            files[0].get(key) != descriptor.get(key)
            for key in ("filename", "mime_type", "size_bytes", "sha256")
        )
    ):
        raise TerminalDeliveryError("terminal_message_changed")
    return {
        "caption": envelope["caption"],
        "artifact": {
            "filename": descriptor["filename"],
            "mime_type": descriptor["mime_type"],
            "size_bytes": descriptor["size_bytes"],
            "sha256": descriptor["sha256"],
            "path": f"/api/notifications/{envelope['notification_id']}/artifact",
        },
    }


def read_terminal_notification_artifact(
    storage: Any,
    files_root: Path,
    row: Mapping[str, Any],
    *,
    tenant_id: str,
    actor_id: str,
    max_bytes: int,
) -> AuthorizedFileBytes:
    """Read one exact pending Raw carrier and recheck its envelope afterwards."""

    projection = terminal_notification_projection(
        storage,
        row,
        tenant_id=tenant_id,
        actor_id=actor_id,
    )
    envelope = parse_terminal_envelope(row.get("body"))
    descriptor = envelope["artifact"]
    try:
        stored = read_authorized_generated_file(
            storage,
            Path(files_root),
            str(descriptor["raw_id"]),
            tenant_id,
            actor_id,
            max_bytes=max_bytes,
        )
    except (FileRecordUnavailable, AuthorizedFileReadError) as exc:
        raise TerminalDeliveryError("terminal_artifact_unavailable") from exc
    if (
        stored.filename != projection["artifact"]["filename"]
        or stored.mime_type != projection["artifact"]["mime_type"]
        or len(stored.content) != projection["artifact"]["size_bytes"]
        or not hmac.compare_digest(
            hashlib.sha256(stored.content).hexdigest(),
            projection["artifact"]["sha256"],
        )
    ):
        raise TerminalDeliveryError("terminal_artifact_changed")
    current = storage.execute(
        """SELECT n.id,n.user_id,n.chat_id,n.kind,n.dedup_key,n.body,n.status
              FROM outbound_notifications AS n
             WHERE n.id=? AND n.status='pending'
               AND n.kind='engineer_command_terminal'""",
        (str(row.get("id") or ""),),
    ).fetchone()
    if current is None or dict(current) != dict(row):
        raise TerminalDeliveryError("terminal_notification_changed")
    raw = storage.execute(
        f"""SELECT 1 FROM raw_objects AS r WHERE r.id=? AND r.user_id=?
               AND {_not_private_raw_dependency("r")}""",  # nosec B608
        (str(descriptor["raw_id"]), actor_id),
    ).fetchone()
    if raw is None:
        raise TerminalDeliveryError("terminal_artifact_unavailable")
    return stored


__all__ = [
    "StagedTerminalNotification",
    "TERMINAL_ENVELOPE_SCHEMA",
    "TERMINAL_NOTIFICATION_KIND",
    "TerminalDeliveryError",
    "parse_terminal_envelope",
    "read_terminal_notification_artifact",
    "stage_terminal_archive",
    "terminal_dedup_key",
    "terminal_notification_projection",
    "terminal_notification_status",
]
