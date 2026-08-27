"""Owner-only autonomous host-user tools for the Engineer command kernel."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from friday.execution_kernel import ToolSpec
from friday.file_delivery import (
    AuthorizedCurrentMessageUploadBatch,
    FileRecordUnavailable,
    authorize_current_message_upload_batch,
    reauthorize_current_message_upload_batch,
)
from friday.organs import ServiceContext, may_push_to, resolve_chat_id

from .command import (
    CommandError,
    CommandGrantAuthority,
    CommandKernel,
    CommandLane,
    CommandOrigin,
    CommandReceipt,
    CommandRequest,
    CommandStatus,
    IsolationProfile,
    OwnerConfirmationAuthority,
    OwnerSourceAuthority,
    TrustedPathContract,
)
from .command.inputs import (
    EMPTY_INPUT_MANIFEST,
    MAX_INPUT_FILE_BYTES,
    CommandInputManifest,
    command_input_descriptor,
    command_input_manifest,
)
from .command.progress import (
    PROGRESS_CHECKPOINTS_SEC,
    ProgressDeliveryError,
    retire_pending_progress_notifications,
    stage_progress_notification,
)
from .command.publication import (
    CommandOutputArchive,
    CommandOutputPublicationError,
    build_command_output_archive,
)
from .publication import ExactGeneratedFilePublicationError, exact_generated_file_batch
from .terminal_delivery import (
    TERMINAL_ARTIFACT_MAX_BYTES,
    TerminalDeliveryError,
    parse_terminal_envelope,
    stage_terminal_archive,
    terminal_notification_status,
    verify_sent_terminal_notification_artifact,
)

_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}")
_UPDATE_ID = re.compile(r"[0-9]{1,20}")
_STEP_ID = re.compile(r"ecstep-[0-9a-f]{32}")
_PRIVATE_CHAT_ID = re.compile(r"[1-9][0-9]{0,19}")
_RAW_ID = re.compile(r"raw_[0-9a-f]{16}")
_TERMINAL = frozenset(
    {
        CommandStatus.COMPLETED,
        CommandStatus.FAILED,
        CommandStatus.CANCELLED,
        CommandStatus.TIMEOUT,
        CommandStatus.UNKNOWN,
    }
)
_PUBLISHABLE_TERMINAL = _TERMINAL - {CommandStatus.UNKNOWN}
_UNCERTAIN_SUBMIT_ERRORS = frozenset(
    {
        "unknown_after_spawn",
        "tree_or_eof_unproven",
        "receipt_persist_failed",
    }
)
_DISPLAY_BYTES = 64 * 1024
_AUTONOMOUS_GRANT_TTL_SEC = 120
_DIRECT_WAIT_SEC = 15.0
_MAX_EXPLICIT_TIMEOUT_SEC = 2_147_483_647
_WORKSPACE_RETENTION_SEC = 30 * 24 * 60 * 60
_WORKSPACE_RETENTION_BATCH_MAX = 20
_PROGRESS_BATCH_MAX = 20


def _source_metadata(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = row.get("metadata_json")
    if isinstance(raw, Mapping):
        return raw
    elif isinstance(raw, str) and len(raw) <= 16_384:

        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        try:
            parsed = json.loads(raw, object_pairs_hook=reject_duplicates)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _source_update_id(row: Mapping[str, Any]) -> str:
    metadata = _source_metadata(row)
    if metadata is None:
        return ""
    value = metadata.get("telegram_update_id")
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


def _source_uploaded_raw_ids(row: Mapping[str, Any]) -> tuple[str, ...] | None:
    metadata = _source_metadata(row)
    if metadata is None:
        return None
    if "conversation_uploaded_raw_ids" not in metadata:
        return ()
    uploaded = metadata.get("conversation_uploaded_raw_ids")
    if not isinstance(uploaded, list):
        return None
    frozen = tuple(uploaded)
    if (
        len(frozen) > 12
        or any(not isinstance(item, str) or _RAW_ID.fullmatch(item) is None for item in frozen)
        or len(set(frozen)) != len(frozen)
    ):
        return None
    return frozen


def _read_private_key(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or info.st_size != 32
        ):
            raise CommandError("invalid_command_key")
        value = os.read(fd, 33)
        if len(value) != 32 or os.read(fd, 1):
            raise CommandError("invalid_command_key")
        return value
    finally:
        os.close(fd)


def _derive(master: bytes, label: bytes) -> bytes:
    return hmac.new(master, b"friday-engineer-command-v1\x00" + label, hashlib.sha256).digest()


def _idempotency_key(source_message_id: str, step_id: str, request: CommandRequest) -> str:
    material = f"{source_message_id}\x00{step_id}\x00{request.digest}".encode("ascii")
    return "ecmd-" + hashlib.sha256(material).hexdigest()


def _safe_output(payload: bytes) -> str:
    text = payload[:_DISPLAY_BYTES].decode("utf-8", errors="replace")
    text = re.sub(
        r"</?(?:tool_call|tool_result|function_call|assistant|system)(?:\s[^>]*)?>",
        "[COMMAND_MARKUP_REMOVED]",
        text,
        flags=re.IGNORECASE,
    )
    return "".join(character for character in text if character in "\n\r\t" or character.isprintable())


def _issue_autonomous_grant(
    authority: CommandGrantAuthority,
    request: CommandRequest,
    *,
    source: Any,
    now: int,
) -> str:
    """Seal one exact current-owner delegation for the host-user request."""

    delegation = authority.source_authority.delegate_autonomous(
        source,
        expires_at=now + _AUTONOMOUS_GRANT_TTL_SEC,
    )
    return authority.issue_autonomous(
        request,
        source=source,
        delegation=delegation,
        ttl_sec=_AUTONOMOUS_GRANT_TTL_SEC,
    )


def _command_request(
    *,
    command: str,
    timeout_sec: int | None,
    idempotency_key: str,
    input_manifest: CommandInputManifest,
) -> CommandRequest:
    """Construct the canonical manifest-bound host-user request."""

    return CommandRequest(
        idempotency_key=idempotency_key,
        lane=CommandLane.SHELL,
        origin=CommandOrigin.MODEL,
        shell_command=command,
        timeout_sec=timeout_sec,
        input_manifest=input_manifest,
    )


def _input_manifest(batch: AuthorizedCurrentMessageUploadBatch) -> CommandInputManifest:
    return command_input_manifest(
        tuple(
            command_input_descriptor(
                position=position,
                raw_id=identity.raw_id,
                source_identity_sha256=identity.source_identity_sha256,
                content_sha256=identity.content_sha256,
                size_bytes=identity.size_bytes,
                original_filename=identity.filename,
                mime_type=identity.mime_type,
            )
            for position, identity in enumerate(batch.identity.files, start=1)
        )
    )


def _submit_autonomous_request(
    kernel: CommandKernel,
    request: CommandRequest,
    grant: str,
    *,
    actor_id: str,
    delivery_chat_id: str,
    input_manifest: CommandInputManifest,
    input_batch: AuthorizedCurrentMessageUploadBatch | None,
) -> str:
    """Submit one exact manifest and its process-owned current-upload bytes."""

    return kernel.submit(
        request,
        grant,
        actor_id=actor_id,
        delivery_chat_id=delivery_chat_id,
        input_batch_identity=input_batch.identity if input_batch is not None else None,
        input_files=input_batch.files if input_batch is not None else (),
        input_manifest=input_manifest,
    )


class EngineerCommandService:
    def __init__(self, ctx: ServiceContext) -> None:
        master = _read_private_key(Path(ctx.settings.engineer_command_key_file))
        source = OwnerSourceAuthority(_derive(master, b"source"))
        confirmation = OwnerConfirmationAuthority(_derive(master, b"confirmation"))
        authority = CommandGrantAuthority(_derive(master, b"grant"), source, confirmation)
        trusted = TrustedPathContract(
            ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")
        )
        self.kernel = CommandKernel(
            Path(ctx.settings.engineer_command_store_dir),
            authority,
            trusted_path=trusted,
        )
        self.storage = ctx.storage
        self.settings = ctx.settings
        self.authorization = ctx.auth
        self.files_root = Path(ctx.settings.files_dir)
        self.max_upload_bytes = int(ctx.settings.max_upload_bytes)
        self._archive_lock = threading.Lock()
        self._archive_cache: (
            tuple[
                tuple[str, str],
                CommandOutputArchive,
                dict[str, str],
            ]
            | None
        ) = None
        self._publication_lock = threading.Lock()
        self._progress_lock = threading.Lock()
        self._retention_lock = threading.Lock()
        self._retire_legacy_command_approvals()

    def close(self, *, timeout_sec: float = 30.0) -> None:
        """Drain live command jobs before the application releases storage."""

        self.kernel.close(timeout_sec=timeout_sec)

    def _retire_legacy_command_approvals(self) -> None:
        """Atomically make predecessor Engineer approval rows and pushes inert."""

        now = datetime.now(UTC).isoformat()
        marker = "undeliverable:engineer_autonomous_no_approval"
        with self.storage.transaction() as conn:
            pending = conn.execute(
                """SELECT id,user_id,requested_by FROM action_approvals
                     WHERE tool='engineer_command_run' AND status='pending'"""
            ).fetchall()
            for row in pending:
                approval_id = str(row["id"] or "")
                if re.fullmatch(r"apr_[0-9a-f]{16}", approval_id) is None:
                    continue
                changed = conn.execute(
                    """UPDATE action_approvals
                          SET status='rejected',decided_by='engineer-autonomous-migration',
                              decided_at=?,updated_at=?
                        WHERE id=? AND tool='engineer_command_run' AND status='pending'""",
                    (now, now, approval_id),
                )
                if changed.rowcount != 1:
                    continue
                conn.execute(
                    """UPDATE outbound_notifications
                          SET status='failed',kind=?,dedup_key=''
                        WHERE status='pending' AND kind='approval' AND dedup_key=?
                          AND user_id IN (?,?)""",
                    (
                        marker,
                        f"approval:{approval_id}",
                        str(row["user_id"] or ""),
                        str(row["requested_by"] or ""),
                    ),
                )

    def _archive_for_receipt(
        self,
        receipt: CommandReceipt,
        *,
        actor_id: str,
        conversation_id: str,
    ) -> tuple[CommandOutputArchive, dict[str, str]]:
        """Build one exact archive per receipt identity, with bounded single-flight caching."""

        lock = getattr(self, "_archive_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._archive_lock = lock
            self._archive_cache = None
        key = (receipt.job_id, receipt.receipt_mac)
        with lock:
            cached = self._archive_cache
            if cached is not None and cached[0] == key:
                return cached[1], dict(cached[2])
            frozen_receipt, outputs = self.kernel.terminal_result(
                receipt.job_id,
                actor_id=actor_id,
                conversation_id=conversation_id,
                timeout_sec=0.1,
            )
            if frozen_receipt != receipt:
                raise CommandOutputPublicationError("command_output_receipt_changed")
            archive = build_command_output_archive(
                receipt,
                outputs,
                max_archive_bytes=self.max_upload_bytes,
            )
            attachment = archive.attachment()
            self._archive_cache = (key, archive, attachment)
            return archive, dict(attachment)

    def execute(
        self,
        *,
        actor: Any,
        command: str,
        _conversation_id: str,
        _source_message_id: str,
        _telegram_update_id: str,
        _step_id: str,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        """Admit one model-planned shell step from an exact current owner turn."""

        conversation_id = str(_conversation_id or "").strip()
        source_message_id = str(_source_message_id or "").strip()
        telegram_update_id = str(_telegram_update_id or "").strip()
        step_id = str(_step_id or "").strip()
        chat_id = str(actor.identity_id or "").strip()
        if (
            not actor.is_owner
            or actor.source != "telegram-bridge"
            or _PRIVATE_CHAT_ID.fullmatch(chat_id) is None
        ):
            return _refusal("authorization_denied")
        if (
            not conversation_id
            or _MESSAGE_ID.fullmatch(source_message_id) is None
            or _UPDATE_ID.fullmatch(telegram_update_id) is None
            or _STEP_ID.fullmatch(step_id) is None
        ):
            return _refusal("authenticated_owner_source_required")
        if timeout_sec is not None and (
            type(timeout_sec) is not int or not 1 <= timeout_sec <= _MAX_EXPLICIT_TIMEOUT_SEC
        ):
            return _refusal("invalid_request")
        if (
            self.authorization is None
            or getattr(self.settings, "engineer_mode_enabled", False) is not True
            or getattr(self.settings, "engineer_command_enabled", False) is not True
        ):
            return _refusal("authorization_denied")
        user = self.storage.get_user(actor.own_id)
        if not isinstance(user, Mapping) or str(user.get("status") or "") != "active":
            return _refusal("authorization_denied")
        try:
            fresh_actor = self.authorization.actor_for_user(
                actor.own_id,
                source="engineer-command-service",
                identity_id=chat_id,
            )
            linked_actor_id = self.storage.resolve_identity("telegram", chat_id)
            if (
                not fresh_actor.is_owner
                or fresh_actor.own_id != actor.own_id
                or fresh_actor.user_id != actor.user_id
                or linked_actor_id != actor.own_id
                or resolve_chat_id(self.storage, actor.own_id) != chat_id
                or not may_push_to(self.settings, self.storage, actor.own_id, chat_id)
            ):
                return _refusal("authorization_denied")
            self.authorization.require(fresh_actor, "engineer.use")
            self.authorization.require(fresh_actor, "engineer.command.run")
        except Exception:
            return _refusal("authorization_denied")
        source_row = self.storage.get_message(source_message_id, actor.own_id)
        if (
            not isinstance(source_row, Mapping)
            or str(source_row.get("id") or "") != source_message_id
            or str(source_row.get("conversation_id") or "") != conversation_id
            or str(source_row.get("role") or "") != "user"
            or _source_update_id(source_row) != telegram_update_id
        ):
            return _refusal("owner_source_unavailable")
        uploaded_raw_ids = _source_uploaded_raw_ids(source_row)
        if uploaded_raw_ids is None:
            return _refusal("command_input_unavailable")
        input_batch: AuthorizedCurrentMessageUploadBatch | None = None
        input_manifest = EMPTY_INPUT_MANIFEST
        try:
            if uploaded_raw_ids:
                input_batch = authorize_current_message_upload_batch(
                    self.storage,
                    self.files_root,
                    self.authorization,
                    actor,
                    conversation_id=conversation_id,
                    source_message_id=source_message_id,
                    telegram_update_id=telegram_update_id,
                    uploaded_raw_ids=uploaded_raw_ids,
                    max_bytes_per_file=MAX_INPUT_FILE_BYTES,
                )
                input_manifest = _input_manifest(input_batch)
            preliminary = _command_request(
                command=command,
                timeout_sec=timeout_sec,
                idempotency_key="pending",
                input_manifest=input_manifest,
            )
            request = _command_request(
                command=command,
                timeout_sec=preliminary.timeout_sec,
                idempotency_key=_idempotency_key(source_message_id, step_id, preliminary),
                input_manifest=input_manifest,
            )
            if request.digest != preliminary.digest:
                raise CommandError("command_digest_changed")
            source_text = str(source_row.get("content") or "")
            owner_source = self.kernel.authority.source_authority.attest(
                actor_id=actor.own_id,
                tenant_id=actor.user_id,
                conversation_id=conversation_id,
                channel="telegram",
                source_row_id=source_message_id,
                source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                telegram_update_id=telegram_update_id,
                isolation_profile=IsolationProfile.HOST_USER,
                idempotency_key=request.idempotency_key,
            )
            grant = _issue_autonomous_grant(
                self.kernel.authority,
                request,
                source=owner_source,
                now=int(time.time()),
            )
            spawn_input_batch = input_batch
            if input_batch is not None:
                spawn_input_batch = reauthorize_current_message_upload_batch(
                    self.storage,
                    self.files_root,
                    self.authorization,
                    actor,
                    expected=input_batch.identity,
                    max_bytes_per_file=MAX_INPUT_FILE_BYTES,
                )
                if _input_manifest(spawn_input_batch) != input_manifest:
                    raise CommandError("command_input_identity_changed")
            job_id = _submit_autonomous_request(
                self.kernel,
                request,
                grant,
                actor_id=actor.own_id,
                delivery_chat_id=chat_id,
                input_manifest=input_manifest,
                input_batch=spawn_input_batch,
            )
            try:
                self.kernel.wait(
                    job_id,
                    actor_id=actor.own_id,
                    conversation_id=conversation_id,
                    timeout_sec=_DIRECT_WAIT_SEC,
                )
            except CommandError as exc:
                if exc.code != "wait_timeout":
                    raise
            progress = self.kernel.progress(
                job_id,
                actor_id=actor.own_id,
                conversation_id=conversation_id,
            )
            if progress.status not in _TERMINAL:
                return {"ok": True, **progress.to_public_payload()}
            receipt, receipt_mac_version = self.kernel.terminal_receipt(
                job_id,
                actor_id=actor.own_id,
                conversation_id=conversation_id,
                timeout_sec=0.1,
            )
        except CommandError as exc:
            return _refusal(
                exc.code,
                effect_boundary_crossed=exc.code in _UNCERTAIN_SUBMIT_ERRORS,
            )
        except FileRecordUnavailable:
            return _refusal("command_input_unavailable")
        public_receipt = receipt.to_public_payload()
        if receipt_mac_version < 2:
            public_receipt.pop("generated_files_sha256", None)
        public_receipt["mac_version"] = receipt_mac_version
        public_receipt["generated_files_authenticated"] = receipt_mac_version >= 2
        return {
            "exit_code": receipt.exit_code,
            "generated_files": (
                [
                    {
                        "mode": item.mode,
                        "relative_path": item.relative_path,
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                    }
                    for item in receipt.generated_files
                ]
                if receipt_mac_version >= 2
                else []
            ),
            "job_id": receipt.job_id,
            "ok": True,
            "receipt": public_receipt,
            "signal": receipt.signal,
            "status": receipt.status.value,
            "stderr": _safe_output(receipt.stderr),
            "stderr_display_truncated": len(receipt.stderr) > _DISPLAY_BYTES,
            "stdout": _safe_output(receipt.stdout),
            "stdout_display_truncated": len(receipt.stdout) > _DISPLAY_BYTES,
        }

    def status(
        self,
        *,
        actor: Any,
        job_id: str | None = None,
        _conversation_id: str = "",
    ) -> dict[str, Any]:
        conversation_id = str(_conversation_id or "").strip()
        if not actor.is_owner:
            return _refusal("authorization_denied")
        if not conversation_id:
            return _refusal("conversation_required")
        try:
            resolved_job_id = self.kernel.resolve_job_reference(
                job_id,
                actor_id=actor.own_id,
                tenant_id=actor.user_id,
                conversation_id=conversation_id,
                channel="telegram",
                operation="status",
            )
            progress = self.kernel.progress(
                resolved_job_id,
                actor_id=actor.own_id,
                conversation_id=conversation_id,
            )
            payload = {"ok": True, **progress.to_public_payload()}
            if progress.status in _PUBLISHABLE_TERMINAL:
                receipt, receipt_mac_version = self.kernel.terminal_receipt(
                    resolved_job_id,
                    actor_id=actor.own_id,
                    conversation_id=conversation_id,
                    timeout_sec=0.1,
                )
                retired_probe = getattr(self.kernel, "output_retired", None)
                retired = bool(
                    retired_probe(
                        resolved_job_id,
                        actor_id=actor.own_id,
                        conversation_id=conversation_id,
                    )
                    if callable(retired_probe)
                    else False
                )
                public_receipt = receipt.to_public_payload()
                if receipt_mac_version < 2:
                    public_receipt.pop("generated_files_sha256", None)
                payload["receipt"] = public_receipt
                payload["receipt"]["mac_version"] = receipt_mac_version
                payload["receipt"]["generated_files_authenticated"] = receipt_mac_version >= 2
                payload["stdout"] = _safe_output(receipt.stdout)
                payload["stderr"] = _safe_output(receipt.stderr)
                payload["stdout_display_truncated"] = len(receipt.stdout) > _DISPLAY_BYTES
                payload["stderr_display_truncated"] = len(receipt.stderr) > _DISPLAY_BYTES
                payload["generated_files"] = (
                    [
                        {
                            "relative_path": item.relative_path,
                            "sha256": item.sha256,
                            "size_bytes": item.size_bytes,
                        }
                        for item in receipt.generated_files
                    ]
                    if receipt_mac_version >= 2
                    else []
                )
                if receipt.generated_files and receipt_mac_version < 2:
                    payload["artifact_delivery"] = {
                        "available": False,
                        "error_code": "legacy_output_receipt_unpublishable",
                    }
                elif retired:
                    payload["output_retired"] = True
                    payload["artifact_delivery"] = {
                        "available": False,
                        "error_code": "job_output_retired",
                    }
                elif receipt.generated_files:
                    try:
                        archive, attachment = self._archive_for_receipt(
                            receipt,
                            actor_id=actor.own_id,
                            conversation_id=conversation_id,
                        )
                    except CommandError as exc:
                        if exc.code != "job_output_retired":
                            raise
                        payload["output_retired"] = True
                        payload["artifact_delivery"] = {
                            "available": False,
                            "error_code": "job_output_retired",
                        }
                    except CommandOutputPublicationError as exc:
                        payload["artifact_delivery"] = {
                            "available": False,
                            "error_code": exc.code,
                        }
                    else:
                        retired_after_read = bool(
                            retired_probe(
                                resolved_job_id,
                                actor_id=actor.own_id,
                                conversation_id=conversation_id,
                            )
                            if callable(retired_probe)
                            else False
                        )
                        if retired_after_read:
                            payload["output_retired"] = True
                            payload["artifact_delivery"] = {
                                "available": False,
                                "error_code": "job_output_retired",
                            }
                        else:
                            payload["artifact_delivery"] = {
                                "available": True,
                                "filename": archive.filename,
                                "sha256": archive.sha256,
                                "size_bytes": len(archive.payload),
                            }
                            payload["_attachment"] = attachment
                final_retired = bool(
                    retired_probe(
                        resolved_job_id,
                        actor_id=actor.own_id,
                        conversation_id=conversation_id,
                    )
                    if callable(retired_probe)
                    else False
                )
                if final_retired:
                    payload["stdout"] = ""
                    payload["stderr"] = ""
                    payload["stdout_display_truncated"] = False
                    payload["stderr_display_truncated"] = False
                    payload["output_retired"] = True
                    payload["artifact_delivery"] = {
                        "available": False,
                        "error_code": "job_output_retired",
                    }
                    payload.pop("_attachment", None)
            elif progress.status is CommandStatus.UNKNOWN:
                # A stale RUNNING job can be reconciled to UNKNOWN without a
                # trustworthy terminal receipt. Report only the scoped durable
                # progress state; never reinterpret it as corruption or read
                # possibly live output bytes.
                payload["artifact_delivery"] = {
                    "available": False,
                    "error_code": "job_output_unpublishable",
                }
            return payload
        except CommandError as exc:
            return _refusal(exc.code)
        except (KeyError, OSError, TypeError, ValueError, OverflowError, sqlite3.Error):
            return _refusal("corrupt_job_state")

    def cancel(
        self,
        *,
        actor: Any,
        job_id: str | None = None,
        _conversation_id: str = "",
    ) -> dict[str, Any]:
        conversation_id = str(_conversation_id or "").strip()
        if not actor.is_owner:
            return _refusal("authorization_denied")
        if not conversation_id:
            return _refusal("conversation_required")
        try:
            resolved_job_id = self.kernel.cancel_reference(
                job_id,
                actor_id=actor.own_id,
                tenant_id=actor.user_id,
                conversation_id=conversation_id,
                channel="telegram",
            )
            progress = self.kernel.progress(
                resolved_job_id,
                actor_id=actor.own_id,
                conversation_id=conversation_id,
            )
        except CommandError as exc:
            return _refusal(exc.code)
        except (KeyError, OSError, TypeError, ValueError, OverflowError, sqlite3.Error):
            return _refusal("corrupt_job_state")
        return {"ok": True, "cancel_requested": True, **progress.to_public_payload()}

    def _fresh_terminal_actor(self, job: Mapping[str, Any]) -> Any | None:
        """Rebuild exact owner authority immediately before outbound staging."""

        actor_id = str(job.get("actor_id") or "")
        tenant_id = str(job.get("tenant_id") or "")
        chat_id = str(job.get("delivery_chat_id") or "")
        if (
            not actor_id
            or not tenant_id
            or re.fullmatch(r"[1-9][0-9]{0,19}", chat_id) is None
            or self.authorization is None
            or getattr(self.settings, "engineer_mode_enabled", False) is not True
            or getattr(self.settings, "engineer_command_enabled", False) is not True
        ):
            return None
        user = self.storage.get_user(actor_id)
        if not isinstance(user, Mapping) or str(user.get("status") or "") != "active":
            return None
        try:
            actor = self.authorization.actor_for_user(
                actor_id,
                source="engineer-terminal-worker",
                identity_id=chat_id,
            )
            if (
                not actor.is_owner
                or actor.own_id != actor_id
                or actor.user_id != tenant_id
                or self.storage.resolve_identity("telegram", chat_id) != actor_id
                or resolve_chat_id(self.storage, actor_id) != chat_id
                or not may_push_to(self.settings, self.storage, actor_id, chat_id)
            ):
                return None
            for capability in (
                "engineer.use",
                "engineer.command.manage",
                "files.read",
            ):
                self.authorization.require(actor, capability)
        except Exception:
            return None
        return actor

    def _fresh_progress_actor(self, job: Mapping[str, Any]) -> Any | None:
        """Rebuild current owner/chat authority without requiring file access."""

        actor_id = str(job.get("actor_id") or "")
        tenant_id = str(job.get("tenant_id") or "")
        chat_id = str(job.get("delivery_chat_id") or "")
        if (
            not actor_id
            or not tenant_id
            or re.fullmatch(r"[1-9][0-9]{0,19}", chat_id) is None
            or self.authorization is None
            or getattr(self.settings, "engineer_mode_enabled", False) is not True
            or getattr(self.settings, "engineer_command_enabled", False) is not True
        ):
            return None
        user = self.storage.get_user(actor_id)
        if not isinstance(user, Mapping) or str(user.get("status") or "") != "active":
            return None
        try:
            actor = self.authorization.actor_for_user(
                actor_id,
                source="engineer-progress-worker",
                identity_id=chat_id,
            )
            if (
                not actor.is_owner
                or actor.own_id != actor_id
                or actor.user_id != tenant_id
                or self.storage.resolve_identity("telegram", chat_id) != actor_id
                or resolve_chat_id(self.storage, actor_id) != chat_id
                or not may_push_to(self.settings, self.storage, actor_id, chat_id)
            ):
                return None
            for capability in ("engineer.use", "engineer.command.manage"):
                self.authorization.require(actor, capability)
        except Exception:
            return None
        return actor

    def _retire_progress_scope(self, job: Mapping[str, Any]) -> None:
        with self.storage.transaction() as conn:
            retire_pending_progress_notifications(
                conn,
                actor_id=str(job.get("actor_id") or ""),
                tenant_id=str(job.get("tenant_id") or ""),
                conversation_id=str(job.get("conversation_id") or ""),
                delivery_chat_id=str(job.get("delivery_chat_id") or ""),
                job_id=str(job.get("job_id") or ""),
            )

    def _reconcile_stale_progress(self, *, now: float, limit: int) -> tuple[int, int]:
        retired = 0
        failed = 0
        for job in self.kernel.store.list_progress_retirement_candidates(now=now, limit=limit):
            job_id = str(job.get("job_id") or "")
            try:
                self._retire_progress_scope(job)
                self.kernel.store.finish_progress_retirement(
                    job_id,
                    retired_at=now,
                )
                retired += 1
            except (
                CommandError,
                ProgressDeliveryError,
                KeyError,
                OSError,
                TypeError,
                ValueError,
                OverflowError,
                sqlite3.Error,
            ) as exc:
                with suppress(
                    CommandError,
                    TypeError,
                    ValueError,
                    OverflowError,
                    sqlite3.Error,
                ):
                    self.kernel.store.record_progress_retirement_failure(
                        job_id,
                        error_code=str(getattr(exc, "code", "progress_retirement_failed")),
                        failed_at=now,
                    )
                failed += 1
        return retired, failed

    @staticmethod
    def _due_progress_checkpoint(
        *,
        now: float,
        started_at: object,
        previous_checkpoint_sec: object,
    ) -> tuple[int, int] | None:
        if (
            isinstance(started_at, bool)
            or not isinstance(started_at, (int, float))
            or isinstance(previous_checkpoint_sec, bool)
            or not isinstance(previous_checkpoint_sec, int)
        ):
            return None
        started = float(started_at)
        previous = previous_checkpoint_sec
        if not math.isfinite(started) or previous not in {0, *PROGRESS_CHECKPOINTS_SEC} or now < started:
            return None
        due = [
            checkpoint for checkpoint in PROGRESS_CHECKPOINTS_SEC if previous < checkpoint <= now - started
        ]
        return (previous, max(due)) if due else None

    def publish_progress_jobs(
        self,
        *,
        now: float | None = None,
        limit: int = _PROGRESS_BATCH_MAX,
    ) -> dict[str, int]:
        """Stage sparse, fact-only progress independently of any model turn."""

        lock = getattr(self, "_progress_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._progress_lock = lock
        if not lock.acquire(blocking=False):
            return {"staged": 0, "retired": 0, "failed": 0}
        bounded = max(1, min(int(limit), _PROGRESS_BATCH_MAX))
        moment = time.time() if now is None else float(now)
        staged = 0
        try:
            retired, failed = self._reconcile_stale_progress(now=moment, limit=bounded)
            for job in self.kernel.store.list_progress_publication_candidates(
                now=moment,
                limit=bounded,
            ):
                due = self._due_progress_checkpoint(
                    now=moment,
                    started_at=job.get("started_at"),
                    previous_checkpoint_sec=job.get("progress_checkpoint_sec"),
                )
                if due is None:
                    with suppress(CommandError, TypeError, ValueError, OverflowError, sqlite3.Error):
                        self.kernel.store.record_progress_publication_failure(
                            str(job.get("job_id") or ""),
                            error_code="progress_checkpoint_invalid",
                            failed_at=moment,
                        )
                    failed += 1
                    continue
                actor = self._fresh_progress_actor(job)
                if actor is None:
                    with suppress(CommandError, TypeError, ValueError, OverflowError, sqlite3.Error):
                        self.kernel.store.record_progress_publication_failure(
                            str(job.get("job_id") or ""),
                            error_code="authorization_denied",
                            failed_at=moment,
                        )
                    failed += 1
                    continue
                job_id = str(job.get("job_id") or "")
                try:
                    progress = self.kernel.progress(
                        job_id,
                        actor_id=actor.own_id,
                        conversation_id=str(job.get("conversation_id") or ""),
                    )
                    if progress.status is not CommandStatus.RUNNING:
                        self._retire_progress_scope(job)
                        with suppress(CommandError):
                            self.kernel.store.finish_progress_retirement(
                                job_id,
                                retired_at=moment,
                            )
                        retired += 1
                        continue
                    previous, checkpoint = due
                    stage_progress_notification(
                        self.storage,
                        actor_id=actor.own_id,
                        tenant_id=actor.user_id,
                        conversation_id=str(job.get("conversation_id") or ""),
                        delivery_chat_id=str(job.get("delivery_chat_id") or ""),
                        job_id=job_id,
                        checkpoint_sec=checkpoint,
                        stdout_bytes=progress.stdout_bytes,
                        stderr_bytes=progress.stderr_bytes,
                        output_activity=bool(progress.stdout_bytes or progress.stderr_bytes),
                    )
                    if self.kernel.store.advance_progress_checkpoint(
                        job_id,
                        previous_checkpoint_sec=previous,
                        checkpoint_sec=checkpoint,
                    ):
                        staged += 1
                    else:
                        self._retire_progress_scope(job)
                except (
                    CommandError,
                    ProgressDeliveryError,
                    KeyError,
                    OSError,
                    TypeError,
                    ValueError,
                    OverflowError,
                    sqlite3.Error,
                ) as exc:
                    with suppress(
                        CommandError,
                        TypeError,
                        ValueError,
                        OverflowError,
                        sqlite3.Error,
                    ):
                        self.kernel.store.record_progress_publication_failure(
                            job_id,
                            error_code=str(getattr(exc, "code", "progress_publication_failed")),
                            failed_at=moment,
                        )
                    failed += 1
            return {"staged": staged, "retired": retired, "failed": failed}
        finally:
            lock.release()

    def _reconcile_staged_publications(self) -> int:
        changed = 0
        for publication in self.kernel.store.list_staged_publications(limit=100):
            job_id = str(publication.get("job_id") or "")
            notification_id = str(publication.get("notification_id") or "")
            dedup_key = str(publication.get("dedup_key") or "")
            envelope_sha256 = str(publication.get("envelope_sha256") or "")
            state = terminal_notification_status(
                self.storage,
                notification_id,
                dedup_key,
                envelope_sha256,
            )
            try:
                if state == "sent":
                    self.kernel.store.finish_publication(job_id, state="sent")
                    changed += 1
                elif state == "uncertain":
                    self.kernel.store.finish_publication(job_id, state="uncertain")
                    changed += 1
                elif state == "failed":
                    self.kernel.store.finish_publication(job_id, state="blocked")
                    changed += 1
                elif state in {"missing", "invalid"}:
                    self.kernel.store.finish_publication(job_id, state="uncertain")
                    changed += 1
            except CommandError:
                continue
        return changed

    def publish_terminal_jobs(self, *, limit: int = 20) -> dict[str, int]:
        """Stage terminal notices without a model turn and reconcile bridge ACKs."""

        if not self._publication_lock.acquire(blocking=False):
            return {"staged": 0, "reconciled": 0, "failed": 0}
        staged = 0
        failed = 0
        try:
            reconciled = self._reconcile_staged_publications()
            for job in self.kernel.store.list_terminal_publication_candidates(limit=limit):
                job_id = str(job.get("job_id") or "")
                actor = self._fresh_terminal_actor(job)
                if actor is None:
                    with suppress(CommandError):
                        self.kernel.store.record_publication_attempt(job_id, "authorization_denied")
                    failed += 1
                    continue
                try:
                    receipt, mac_version = self.kernel.terminal_receipt(
                        job_id,
                        actor_id=actor.own_id,
                        conversation_id=str(job.get("conversation_id") or ""),
                        timeout_sec=0.1,
                    )
                    if (
                        mac_version < 2
                        or receipt.status.value != str(job.get("status") or "")
                        or receipt.status not in _PUBLISHABLE_TERMINAL
                    ):
                        raise TerminalDeliveryError("terminal_receipt_unpublishable")
                    archive, attachment = self._archive_for_receipt(
                        receipt,
                        actor_id=actor.own_id,
                        conversation_id=str(job.get("conversation_id") or ""),
                    )
                    batch = exact_generated_file_batch(
                        [attachment],
                        max_bytes=self.max_upload_bytes,
                    )
                    publication = stage_terminal_archive(
                        self.storage,
                        self.files_root,
                        actor_id=actor.own_id,
                        tenant_id=actor.user_id,
                        conversation_id=str(job.get("conversation_id") or ""),
                        source_message_id=str(job.get("source_row_id") or ""),
                        delivery_chat_id=str(job.get("delivery_chat_id") or ""),
                        job_id=job_id,
                        status=receipt.status.value,
                        receipt_mac=receipt.receipt_mac,
                        attachment=attachment,
                        batch=batch,
                        max_bytes=self.max_upload_bytes,
                    )
                    if archive.sha256 != batch.files[0].content_sha256:
                        raise TerminalDeliveryError("terminal_archive_identity_changed")
                    self.kernel.store.stage_publication(
                        job_id,
                        notification_id=publication.notification_id,
                        dedup_key=publication.dedup_key,
                        envelope_sha256=publication.envelope_sha256,
                    )
                    staged += 1
                except (
                    CommandError,
                    CommandOutputPublicationError,
                    ExactGeneratedFilePublicationError,
                    TerminalDeliveryError,
                    KeyError,
                    OSError,
                    TypeError,
                    ValueError,
                    OverflowError,
                    sqlite3.Error,
                ) as exc:
                    error_code = getattr(exc, "code", "terminal_publication_failed")
                    with suppress(CommandError):
                        self.kernel.store.record_publication_attempt(job_id, str(error_code))
                    failed += 1
            reconciled += self._reconcile_staged_publications()
            return {"staged": staged, "reconciled": reconciled, "failed": failed}
        finally:
            self._publication_lock.release()

    @staticmethod
    def _sent_at_epoch(value: object) -> float:
        if not isinstance(value, str) or not value:
            raise TerminalDeliveryError("terminal_sent_time_invalid")
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TerminalDeliveryError("terminal_sent_time_invalid") from exc
        if moment.tzinfo is None:
            raise TerminalDeliveryError("terminal_sent_time_invalid")
        return moment.astimezone(UTC).timestamp()

    def _exact_sent_retention_row(
        self,
        job: Mapping[str, Any],
        *,
        cutoff: float,
        missing_after_marker: bool,
    ) -> dict[str, Any] | None:
        notification_id = str(job.get("notification_id") or "")
        row = self.storage.execute(
            """SELECT n.id,n.user_id,n.chat_id,n.kind,n.dedup_key,n.body,n.status,n.sent_at
                  FROM outbound_notifications AS n WHERE n.id=?""",
            (notification_id,),
        ).fetchone()
        if row is None:
            if missing_after_marker:
                return None
            raise TerminalDeliveryError("terminal_notification_missing")
        exact = dict(row)
        body = str(exact.get("body") or "")
        try:
            body_sha256 = hashlib.sha256(body.encode("ascii")).hexdigest()
        except UnicodeEncodeError as exc:
            raise TerminalDeliveryError("terminal_envelope_invalid") from exc
        envelope = parse_terminal_envelope(body)
        if (
            exact.get("status") != "sent"
            or exact.get("kind") != "engineer_command_terminal"
            or str(exact.get("id") or "") != notification_id
            or str(exact.get("user_id") or "") != str(job.get("actor_id") or "")
            or str(exact.get("chat_id") or "") != str(job.get("delivery_chat_id") or "")
            or str(exact.get("dedup_key") or "") != str(job.get("publication_dedup_key") or "")
            or not hmac.compare_digest(
                body_sha256,
                str(job.get("envelope_sha256") or ""),
            )
            or envelope.get("notification_id") != notification_id
            or envelope.get("job_id") != str(job.get("job_id") or "")
            or envelope.get("actor_id") != str(job.get("actor_id") or "")
            or envelope.get("tenant_id") != str(job.get("tenant_id") or "")
            or envelope.get("conversation_id") != str(job.get("conversation_id") or "")
            or envelope.get("source_message_id") != str(job.get("source_row_id") or "")
            or envelope.get("delivery_chat_id") != str(job.get("delivery_chat_id") or "")
            or envelope.get("status") != str(job.get("status") or "")
            or envelope.get("receipt_mac") != str(job.get("receipt_mac") or "")
            or self._sent_at_epoch(exact.get("sent_at")) > float(cutoff)
        ):
            raise TerminalDeliveryError("terminal_retention_identity_changed")
        return exact

    def _delete_exact_sent_notification(self, row: Mapping[str, Any]) -> None:
        with self.storage.transaction() as conn:
            current = conn.execute(
                """SELECT n.id,n.user_id,n.chat_id,n.kind,n.dedup_key,n.body,n.status,n.sent_at
                      FROM outbound_notifications AS n WHERE n.id=?""",
                (str(row.get("id") or ""),),
            ).fetchone()
            if current is None or dict(current) != dict(row):
                raise TerminalDeliveryError("terminal_notification_changed")
            cursor = conn.execute(
                """DELETE FROM outbound_notifications
                    WHERE id=? AND user_id=? AND chat_id=?
                      AND kind='engineer_command_terminal' AND status='sent'
                      AND dedup_key=? AND body=? AND sent_at IS ?""",
                (
                    str(row.get("id") or ""),
                    str(row.get("user_id") or ""),
                    str(row.get("chat_id") or ""),
                    str(row.get("dedup_key") or ""),
                    str(row.get("body") or ""),
                    row.get("sent_at"),
                ),
            )
            if cursor.rowcount != 1:
                raise TerminalDeliveryError("terminal_notification_changed")

    def _evict_archive_cache(self, job_id: str) -> None:
        with self._archive_lock:
            cached = self._archive_cache
            if cached is not None and cached[0][0] == job_id:
                self._archive_cache = None

    def retain_terminal_jobs(
        self,
        *,
        now: float | None = None,
        limit: int = _WORKSPACE_RETENTION_BATCH_MAX,
    ) -> dict[str, int]:
        """Retire only verified old sent workspaces; preserve canonical delivered files."""

        lock = getattr(self, "_retention_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._retention_lock = lock
        if not lock.acquire(blocking=False):
            return {"retired": 0, "failed": 0, "ephemera": 0}
        moment = time.time() if now is None else float(now)
        cutoff = moment - _WORKSPACE_RETENTION_SEC
        retired_count = 0
        failed = 0
        try:
            ephemera = self.kernel.store.prune_expired_ephemera(
                now=int(moment),
                limit=100,
            )
            candidates = self.kernel.store.list_workspace_retention_candidates(
                cutoff=cutoff,
                now=moment,
                limit=max(1, min(int(limit), _WORKSPACE_RETENTION_BATCH_MAX)),
            )
            for job in candidates:
                job_id = str(job.get("job_id") or "")
                try:
                    marker = job.get("workspace_retired_at")
                    row = self._exact_sent_retention_row(
                        job,
                        cutoff=cutoff,
                        missing_after_marker=marker is not None,
                    )
                    if marker is None:
                        if row is None:
                            raise TerminalDeliveryError("terminal_notification_missing")
                        envelope = parse_terminal_envelope(row.get("body"))
                        artifact_size = int(envelope["artifact"]["size_bytes"])
                        if artifact_size > TERMINAL_ARTIFACT_MAX_BYTES:
                            raise TerminalDeliveryError("terminal_artifact_too_large")
                        verify_sent_terminal_notification_artifact(
                            self.storage,
                            self.files_root,
                            row,
                            tenant_id=str(job.get("tenant_id") or ""),
                            actor_id=str(job.get("actor_id") or ""),
                            max_bytes=artifact_size,
                        )
                        receipt, _outputs = self.kernel.terminal_result_for_retention(
                            job_id,
                            actor_id=str(job.get("actor_id") or ""),
                            conversation_id=str(job.get("conversation_id") or ""),
                        )
                        if receipt.status.value != str(job.get("status") or "") or receipt.receipt_mac != str(
                            job.get("receipt_mac") or ""
                        ):
                            raise TerminalDeliveryError("terminal_retention_identity_changed")
                        self.kernel.store.mark_workspace_retirement(
                            job_id,
                            notification_id=str(job.get("notification_id") or ""),
                            dedup_key=str(job.get("publication_dedup_key") or ""),
                            envelope_sha256=str(job.get("envelope_sha256") or ""),
                            cutoff=cutoff,
                            retired_at=moment,
                            stdout_bytes=len(receipt.stdout),
                            stderr_bytes=len(receipt.stderr),
                        )
                    self._evict_archive_cache(job_id)
                    self.kernel.retire_workspace(job_id)
                    if row is not None:
                        self._delete_exact_sent_notification(row)
                    self.kernel.store.finish_workspace_retirement(
                        job_id,
                        notification_id=str(job.get("notification_id") or ""),
                        dedup_key=str(job.get("publication_dedup_key") or ""),
                        envelope_sha256=str(job.get("envelope_sha256") or ""),
                        retired_at=moment,
                    )
                    retired_count += 1
                except (
                    CommandError,
                    CommandOutputPublicationError,
                    TerminalDeliveryError,
                    KeyError,
                    OSError,
                    TypeError,
                    ValueError,
                    OverflowError,
                    sqlite3.Error,
                ) as exc:
                    with suppress(
                        CommandError,
                        TypeError,
                        ValueError,
                        OverflowError,
                        sqlite3.Error,
                    ):
                        self.kernel.store.record_workspace_retention_failure(
                            job_id,
                            error_code=str(getattr(exc, "code", "retention_failed")),
                            failed_at=moment,
                        )
                    failed += 1
            return {"retired": retired_count, "failed": failed, "ephemera": ephemera}
        finally:
            lock.release()


def _refusal(code: str, *, effect_boundary_crossed: bool = False) -> dict[str, Any]:
    clean = str(code or "command_refused")
    if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", clean) is None:
        clean = "command_refused"
    return {
        "effect_boundary_crossed": bool(effect_boundary_crossed),
        "error_code": clean,
        "ok": False,
        "status": "unknown" if effect_boundary_crossed else "failed",
    }


def build_engineer_command_tools(
    ctx: ServiceContext,
    *,
    service: EngineerCommandService | None = None,
) -> tuple[ToolSpec, ...]:
    if not bool(getattr(ctx.settings, "engineer_command_enabled", False)):
        return ()
    service = service or EngineerCommandService(ctx)

    async def run_command(
        *,
        actor: Any,
        command: str,
        _conversation_id: str,
        _source_message_id: str,
        _telegram_update_id: str,
        _step_id: str,
        timeout_sec: int | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            service.execute,
            actor=actor,
            command=command,
            timeout_sec=timeout_sec,
            _conversation_id=_conversation_id,
            _source_message_id=_source_message_id,
            _telegram_update_id=_telegram_update_id,
            _step_id=_step_id,
        )

    async def status_exact(
        *,
        actor: Any,
        job_id: str | None = None,
        _conversation_id: str = "",
    ) -> dict[str, Any]:
        return service.status(
            actor=actor,
            job_id=job_id,
            _conversation_id=_conversation_id,
        )

    async def cancel_exact(
        *,
        actor: Any,
        job_id: str | None = None,
        _conversation_id: str = "",
    ) -> dict[str, Any]:
        return service.cancel(
            actor=actor,
            job_id=job_id,
            _conversation_id=_conversation_id,
        )

    job_parameters = {
        "type": "object",
        "properties": {"job_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}},
        "additionalProperties": False,
    }
    return (
        ToolSpec(
            name="engineer_command_run",
            description=(
                "Run one model-planned shell step as the Friday service user on its VM. "
                "Use any installed command, the service user's filesystem and network needed for the "
                "owner's task. Omit timeout_sec for a durable job that runs until completion, explicit "
                "cancellation or an OS failure; set it only when the task itself needs a deadline. The "
                "call waits briefly for a terminal receipt, then returns a job id for longer work. Put "
                "files intended for Telegram delivery below the "
                "absolute directory in FRIDAY_OUTPUT_DIR; job, scratch and immutable current-message "
                "inputs are exposed as FRIDAY_JOB_DIR, FRIDAY_WORK_DIR and FRIDAY_INPUT_DIR."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "minLength": 1, "maxLength": 16_384},
                    "timeout_sec": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_EXPLICIT_TIMEOUT_SEC,
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            security_id="engineer.command.run",
            risk="mutate",
            timeout_sec=30.0,
            handler=run_command,
        ),
        ToolSpec(
            name="engineer_command_status",
            description=(
                "Read the real state and bounded stdout/stderr of an owned Engineer command job. "
                "Omit job_id when the user asks about the current command in this conversation; "
                "provide it only when the user explicitly identifies a job."
            ),
            parameters=job_parameters,
            security_id="engineer.command.manage",
            risk="observe",
            handler=status_exact,
        ),
        ToolSpec(
            name="engineer_command_cancel",
            description=(
                "Request cancellation of an owned Engineer command job. Omit job_id for the current "
                "command in this conversation; provide it only when the user explicitly identifies a job."
            ),
            parameters=job_parameters,
            security_id="engineer.command.manage",
            risk="mutate",
            handler=cancel_exact,
        ),
    )


__all__ = ["EngineerCommandService", "build_engineer_command_tools"]
