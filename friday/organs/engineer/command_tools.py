"""Owner-confirmed exact-argv tools for the isolated Engineer command kernel."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from friday.execution_kernel import ToolSpec
from friday.organs import ServiceContext

from .command import (
    CommandError,
    CommandGrantAuthority,
    CommandKernel,
    CommandLane,
    CommandOrigin,
    CommandRequest,
    CommandStatus,
    IsolationProfile,
    OwnerConfirmationAuthority,
    OwnerSourceAuthority,
    TrustedPathContract,
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
_UNCERTAIN_SUBMIT_ERRORS = frozenset(
    {
        "unknown_after_spawn",
        "tree_or_eof_unproven",
        "receipt_persist_failed",
    }
)
_DISPLAY_BYTES = 64 * 1024


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
        approval = self.storage.get_action_approval(
            str(_approval_id), actor.user_id, person_id=actor.own_id
        )
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
            job_id = self.kernel.submit(request, grant, actor_id=actor.own_id)
            progress = self.kernel.progress(job_id, actor_id=actor.own_id)
        except CommandError as exc:
            return _refusal(
                exc.code,
                effect_boundary_crossed=exc.code in _UNCERTAIN_SUBMIT_ERRORS,
            )
        return {"ok": True, **progress.to_public_payload()}

    def status(self, *, actor: Any, job_id: str) -> dict[str, Any]:
        try:
            progress = self.kernel.progress(str(job_id), actor_id=actor.own_id)
            payload = {"ok": True, **progress.to_public_payload()}
            if progress.status in _TERMINAL:
                receipt = self.kernel.wait(str(job_id), actor_id=actor.own_id, timeout_sec=0.1)
                payload["receipt"] = receipt.to_public_payload()
                payload["stdout"] = _safe_output(receipt.stdout)
                payload["stderr"] = _safe_output(receipt.stderr)
                payload["stdout_display_truncated"] = len(receipt.stdout) > _DISPLAY_BYTES
                payload["stderr_display_truncated"] = len(receipt.stderr) > _DISPLAY_BYTES
                payload["generated_files"] = [
                    {
                        "relative_path": item.relative_path,
                        "sha256": item.sha256,
                        "size_bytes": item.size_bytes,
                    }
                    for item in receipt.generated_files
                ]
            return payload
        except CommandError as exc:
            return _refusal(exc.code)

    def cancel(self, *, actor: Any, job_id: str) -> dict[str, Any]:
        try:
            self.kernel.cancel(str(job_id), actor_id=actor.own_id)
            progress = self.kernel.progress(str(job_id), actor_id=actor.own_id)
        except CommandError as exc:
            return _refusal(exc.code)
        return {"ok": True, "cancel_requested": True, **progress.to_public_payload()}


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


def build_engineer_command_tools(ctx: ServiceContext) -> tuple[ToolSpec, ...]:
    if not bool(getattr(ctx.settings, "engineer_command_enabled", False)):
        return ()
    service = EngineerCommandService(ctx)

    async def run_exact(**arguments: Any) -> dict[str, Any]:
        return service.execute(**arguments)

    async def status_exact(*, actor: Any, job_id: str) -> dict[str, Any]:
        return service.status(actor=actor, job_id=job_id)

    async def cancel_exact(*, actor: Any, job_id: str) -> dict[str, Any]:
        return service.cancel(actor=actor, job_id=job_id)

    job_parameters = {
        "type": "object",
        "properties": {"job_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}},
        "required": ["job_id"],
        "additionalProperties": False,
    }
    return (
        ToolSpec(
            name="engineer_command_run",
            description=(
                "Prepare one exact installed-program argv for isolated execution. "
                "The owner sees the exact argv and must confirm it before any process starts. "
                "No shell, host files, host network, inherited credentials or Docker socket are available. "
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
            description="Read the real state and bounded stdout/stderr of an owned Engineer command job.",
            parameters=job_parameters,
            security_id="engineer.command.manage",
            risk="observe",
            handler=status_exact,
        ),
        ToolSpec(
            name="engineer_command_cancel",
            description="Request cancellation of one currently running owned Engineer command job.",
            parameters=job_parameters,
            security_id="engineer.command.manage",
            risk="mutate",
            handler=cancel_exact,
        ),
    )


__all__ = ["EngineerCommandService", "build_engineer_command_tools"]
