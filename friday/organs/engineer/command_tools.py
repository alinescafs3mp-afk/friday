"""Owner-confirmed exact-argv tools for the isolated Engineer command kernel."""

from __future__ import annotations

import hashlib
import hmac
import json
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
from .command.publication import (
    CommandOutputArchive,
    CommandOutputPublicationError,
    build_command_output_archive,
)
from .publication import ExactGeneratedFilePublicationError, exact_generated_file_batch
from .terminal_delivery import (
    TerminalDeliveryError,
    parse_terminal_envelope,
    stage_terminal_archive,
    terminal_notification_status,
    verify_sent_terminal_notification_artifact,
)

_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}")
_UPDATE_ID = re.compile(r"[0-9]{1,20}")
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
_WORKSPACE_RETENTION_SEC = 30 * 24 * 60 * 60
_WORKSPACE_RETENTION_BATCH_MAX = 20


def _source_update_id(row: Mapping[str, Any]) -> str:
    raw = row.get("metadata_json")
    if isinstance(raw, Mapping):
        metadata = raw
    elif isinstance(raw, str) and len(raw) <= 16_384:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return ""
        metadata = parsed if isinstance(parsed, Mapping) else {}
    else:
        return ""
    value = metadata.get("telegram_update_id")
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


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


def _idempotency_key(source_message_id: str, request: CommandRequest) -> str:
    material = f"{source_message_id}\x00{request.digest}".encode("ascii")
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
        self._retention_lock = threading.Lock()

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
        argv: list[str],
        timeout_sec: int,
        _conversation_id: str,
        _source_message_id: str,
        _telegram_update_id: str,
        _approval_id: str,
        _confirmation_update_id: str,
        _confirmation_body_hash: str,
        _delivery_chat_id: str = "",
    ) -> dict[str, Any]:
        if not actor.is_owner:
            return _refusal("authorization_denied")
        if (
            _MESSAGE_ID.fullmatch(str(_source_message_id or "")) is None
            or _UPDATE_ID.fullmatch(str(_telegram_update_id or "")) is None
            or _UPDATE_ID.fullmatch(str(_confirmation_update_id or "")) is None
            or str(_telegram_update_id) == str(_confirmation_update_id)
            or not re.fullmatch(r"apr_[0-9a-f]{16}", str(_approval_id or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(_confirmation_body_hash or ""))
        ):
            return _refusal("authenticated_confirmation_required")
        source_row = self.storage.get_message(str(_source_message_id), actor.own_id)
        if (
            not isinstance(source_row, Mapping)
            or str(source_row.get("id") or "") != str(_source_message_id)
            or str(source_row.get("conversation_id") or "") != str(_conversation_id)
            or str(source_row.get("role") or "") != "user"
            or _source_update_id(source_row) != str(_telegram_update_id)
        ):
            return _refusal("owner_source_unavailable")
        try:
            linked_actor_id = self.storage.resolve_identity(
                "telegram",
                str(_delivery_chat_id or ""),
            )
        except ValueError:
            return _refusal("authenticated_confirmation_required")
        if (
            actor.source != "telegram-bridge"
            or not re.fullmatch(r"[1-9][0-9]{0,19}", str(_delivery_chat_id or ""))
            or str(actor.identity_id or "") != str(_delivery_chat_id)
            or linked_actor_id != actor.own_id
            or resolve_chat_id(self.storage, actor.own_id) != str(_delivery_chat_id)
            or not may_push_to(
                self.settings,
                self.storage,
                actor.own_id,
                str(_delivery_chat_id),
            )
        ):
            return _refusal("authenticated_confirmation_required")
        approval = self.storage.get_action_approval(str(_approval_id), actor.user_id, person_id=actor.own_id)
        if (
            not isinstance(approval, Mapping)
            or str(approval.get("tool") or "") != "engineer_command_run"
            or str(approval.get("status") or "") != "claimed"
            or str(approval.get("requested_by") or "") != actor.own_id
        ):
            return _refusal("authenticated_confirmation_required")
        try:
            preliminary = CommandRequest(
                lane=CommandLane.ARGV,
                origin=CommandOrigin.OWNER_TURN,
                argv=tuple(argv),
                timeout_sec=int(timeout_sec),
                idempotency_key="pending",
            )
            request = CommandRequest(
                lane=preliminary.lane,
                origin=preliminary.origin,
                argv=preliminary.argv,
                timeout_sec=preliminary.timeout_sec,
                idempotency_key=_idempotency_key(str(_source_message_id), preliminary),
            )
            source_text = str(source_row.get("content") or "")
            owner_source = self.kernel.authority.source_authority.attest(
                actor_id=actor.own_id,
                tenant_id=actor.user_id,
                conversation_id=str(_conversation_id),
                channel="telegram",
                source_row_id=str(_source_message_id),
                source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                telegram_update_id=str(_telegram_update_id),
                isolation_profile=IsolationProfile.ISOLATED_WORKSPACE,
                idempotency_key=request.idempotency_key,
            )
            expires_at = int(time.time()) + 120
            handle = self.kernel.authority.confirm_authority.ingest(
                actor_id=actor.own_id,
                tenant_id=actor.user_id,
                conversation_id=str(_conversation_id),
                channel="telegram",
                confirmation_row_id=str(_approval_id),
                confirmation_update_id=str(_confirmation_update_id),
                command_digest=request.digest,
                body_hash=str(_confirmation_body_hash),
                expires_at=expires_at,
            )
            confirmation = self.kernel.authority.confirm_authority.seal(
                handle,
                command_digest=request.digest,
            )
            grant = self.kernel.authority.issue(
                request,
                source=owner_source,
                confirmation=confirmation,
                ttl_sec=120,
            )
            job_id = self.kernel.submit(
                request,
                grant,
                actor_id=actor.own_id,
                delivery_chat_id=str(_delivery_chat_id),
            )
            progress = self.kernel.progress(
                job_id,
                actor_id=actor.own_id,
                conversation_id=str(_conversation_id),
            )
        except CommandError as exc:
            return _refusal(
                exc.code,
                effect_boundary_crossed=exc.code in _UNCERTAIN_SUBMIT_ERRORS,
            )
        return {"ok": True, **progress.to_public_payload()}

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
                receipt, receipt_mac_version = self.kernel.terminal_receipt(
                    resolved_job_id,
                    actor_id=actor.own_id,
                    conversation_id=conversation_id,
                    timeout_sec=0.1,
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
                    except CommandOutputPublicationError as exc:
                        payload["artifact_delivery"] = {
                            "available": False,
                            "error_code": exc.code,
                        }
                    else:
                        payload["artifact_delivery"] = {
                            "available": True,
                            "filename": archive.filename,
                            "sha256": archive.sha256,
                            "size_bytes": len(archive.payload),
                        }
                        payload["_attachment"] = attachment
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
                        stored = verify_sent_terminal_notification_artifact(
                            self.storage,
                            self.files_root,
                            row,
                            tenant_id=str(job.get("tenant_id") or ""),
                            actor_id=str(job.get("actor_id") or ""),
                            max_bytes=self.max_upload_bytes,
                        )
                        receipt, outputs = self.kernel.terminal_result_for_retention(
                            job_id,
                            actor_id=str(job.get("actor_id") or ""),
                            conversation_id=str(job.get("conversation_id") or ""),
                        )
                        if (
                            receipt.status.value != str(job.get("status") or "")
                            or receipt.receipt_mac != str(job.get("receipt_mac") or "")
                        ):
                            raise TerminalDeliveryError("terminal_retention_identity_changed")
                        archive = build_command_output_archive(
                            receipt,
                            outputs,
                            max_archive_bytes=self.max_upload_bytes,
                        )
                        if (
                            archive.filename != stored.filename
                            or archive.mime_type != stored.mime_type
                            or len(archive.payload) != len(stored.content)
                            or not hmac.compare_digest(
                                archive.sha256,
                                hashlib.sha256(stored.content).hexdigest(),
                            )
                        ):
                            raise TerminalDeliveryError("terminal_archive_identity_changed")
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
                ):
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

    async def run_exact(**arguments: Any) -> dict[str, Any]:
        return service.execute(**arguments)

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
                "Prepare one exact installed-program argv for isolated execution. "
                "The owner sees the exact argv and must confirm it before any process starts. "
                "There is no implicit or privileged host shell; an explicitly requested shell remains "
                "an exact confirmed argv inside the same sandbox. Host data, host network, inherited "
                "credentials and the Docker socket are unavailable. "
                "The program starts in /job; use /job/workspace for scratch data and write every "
                "deliverable file below /job/output so Friday can inventory it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": {"type": "string", "minLength": 1, "maxLength": 512},
                    },
                    "timeout_sec": {"type": "integer", "minimum": 1, "maximum": 3600},
                },
                "required": ["argv", "timeout_sec"],
                "additionalProperties": False,
            },
            security_id="engineer.command.run",
            risk="high",
            timeout_sec=30.0,
            handler=run_exact,
            approval_predicate=lambda _arguments: True,
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
