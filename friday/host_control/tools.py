"""Owner-only model tools for the authenticated Host Capability Plane."""

from __future__ import annotations

from typing import Any

from friday.execution_kernel import ToolSpec
from friday.permissions import AuthorizationError

from .client import HostControlClient, HostControlClientError
from .contracts import ContractError
from .service import (
    HostActionUnknown,
    HostCapabilityUnavailable,
    HostControlService,
    PreparedHostAction,
)


def build_host_control_tools(ctx: Any) -> tuple[ToolSpec, ...]:
    """Return no schemas unless the authenticated agent is reachable now."""

    if not ctx.settings.host_control_enabled:
        return ()
    try:
        client = HostControlClient(
            ctx.settings.host_agent_socket,
            key_file=ctx.settings.host_agent_key_file,
            agent_id=ctx.settings.host_agent_id,
            timeout_sec=min(3_600.0, ctx.settings.host_action_default_timeout_sec + 30.0),
        )
    except HostControlClientError:
        return ()
    if not client.available(timeout_sec=0.5):
        return ()
    service = HostControlService(ctx, client)

    async def capability_search(
        *,
        actor: Any,
        query: str,
        category: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        try:
            return {"ok": True, **await service.search(query=query, category=category, limit=limit)}
        except (ContractError, HostCapabilityUnavailable) as exc:
            return _known_refusal(exc)

    async def capability_describe(*, actor: Any, capability_id: str) -> dict[str, Any]:
        try:
            return {"ok": True, **await service.describe(capability_id=capability_id)}
        except (ContractError, HostCapabilityUnavailable) as exc:
            return _known_refusal(exc)

    async def action_run(
        *,
        actor: Any,
        capability_id: str,
        action_id: str,
        targets: list[str],
        ports: list[int] | None = None,
        _conversation_id: str,
        _source_message_id: str,
    ) -> dict[str, Any]:
        prepared: PreparedHostAction | None = None
        try:
            candidate = await service.prepare_network_action(
                actor=actor,
                capability_id=capability_id,
                action_id=action_id,
                targets=targets,
                ports=ports,
                conversation_id=_conversation_id,
                source_message_id=_source_message_id,
            )
            if isinstance(candidate, dict):
                return _prepared_action_result(candidate)
            prepared = candidate
            if candidate.job.get("status") == "awaiting_approval":
                approval = service.request_action_approval(candidate, actor=actor)
                return {
                    "approval_id": approval["id"],
                    "effect_boundary_crossed": False,
                    "error_code": "approval_required",
                    "job_id": candidate.job["id"],
                    "ok": False,
                    "status": "awaiting_approval",
                    "summary": approval["summary"],
                }
            result = await service.run_prepared(candidate, actor=actor)
            return _action_execution_result(result, job_id=str(candidate.job["id"]))
        except HostActionUnknown:
            return _unknown_action(str((prepared.job if prepared else {}).get("id") or ""))
        except (AuthorizationError, ContractError, HostCapabilityUnavailable) as exc:
            return _known_refusal(exc, job_id=str((prepared.job if prepared else {}).get("id") or ""))
        except Exception:  # noqa: BLE001 - after preparation the action outcome is uncertain
            if prepared is not None:
                return _unknown_action(str(prepared.job.get("id") or ""))
            raise

    async def action_execute(
        *,
        actor: Any,
        job_id: str,
        plan: dict[str, Any],
        plan_digest: str,
    ) -> dict[str, Any]:
        try:
            result = await service.execute_approved_action(
                actor=actor,
                job_id=job_id,
                plan=plan,
                plan_digest=plan_digest,
            )
        except HostActionUnknown:
            return _unknown_action(job_id)
        except (AuthorizationError, ContractError, HostCapabilityUnavailable) as exc:
            return _known_refusal(exc, job_id=job_id)
        except Exception:  # noqa: BLE001 - an admitted approved action must be reconciled
            return _unknown_action(job_id)
        return _action_execution_result(result, job_id=job_id)

    async def json_extract(
        *,
        actor: Any,
        fields: list[str],
        compact: bool = True,
        _conversation_id: str,
        _raw_id: str,
        _source_message_id: str,
    ) -> dict[str, Any]:
        prepared: PreparedHostAction | None = None
        try:
            candidate = await service.prepare_file_action(
                actor=actor,
                capability_id="data.jq.extract",
                action_id="extract_fields",
                raw_id=_raw_id,
                fields=fields,
                compact=compact,
                conversation_id=_conversation_id,
                source_message_id=_source_message_id,
            )
            if isinstance(candidate, dict):
                return _prepared_action_result(candidate)
            prepared = candidate
            result = await service.run_prepared(candidate, actor=actor)
            return _action_execution_result(result, job_id=str(candidate.job["id"]))
        except HostActionUnknown:
            return _unknown_action(str((prepared.job if prepared else {}).get("id") or ""))
        except (AuthorizationError, ContractError, HostCapabilityUnavailable) as exc:
            return _known_refusal(exc, job_id=str((prepared.job if prepared else {}).get("id") or ""))
        except Exception:  # noqa: BLE001 - after preparation the action outcome is uncertain
            if prepared is not None:
                return _unknown_action(str(prepared.job.get("id") or ""))
            raise

    async def software_install_execute(
        *,
        actor: Any,
        job_id: str,
        package_plan: dict[str, Any],
        plan_digest: str,
    ) -> dict[str, Any]:
        try:
            result = await service.execute_approved_install(
                actor=actor,
                job_id=job_id,
                package_plan=package_plan,
                plan_digest=plan_digest,
            )
        except HostActionUnknown:
            return _unknown_install(job_id)
        except (AuthorizationError, ContractError, HostCapabilityUnavailable) as exc:
            return _known_refusal(exc, job_id=job_id)
        except Exception:  # noqa: BLE001 - after admission an unclassified failure is uncertain
            return _unknown_install(job_id)
        return _install_execution_result(result, job_id=job_id)

    async def job_status(*, actor: Any, job_id: str) -> dict[str, Any]:
        try:
            return {"ok": True, **await service.status(actor=actor, job_id=job_id)}
        except HostActionUnknown:
            return {
                "effect_boundary_crossed": True,
                "error_code": "host_action_unknown",
                "job_id": job_id,
                "ok": False,
                "status": "unknown",
            }
        except (ContractError, HostControlClientError) as exc:
            return _known_refusal(exc, job_id=job_id)

    async def job_cancel(*, actor: Any, job_id: str) -> dict[str, Any]:
        try:
            return {"ok": True, **await service.cancel(actor=actor, job_id=job_id)}
        except HostActionUnknown:
            return {
                "effect_boundary_crossed": True,
                "error_code": "cancel_outcome_unknown",
                "job_id": job_id,
                "ok": False,
                "status": "unknown",
            }
        except HostControlClientError:
            return {
                "effect_boundary_crossed": True,
                "error_code": "cancel_outcome_unknown",
                "job_id": job_id,
                "ok": False,
                "status": "unknown",
            }
        except (ContractError, HostCapabilityUnavailable) as exc:
            return _known_refusal(exc, job_id=job_id)

    tools: tuple[ToolSpec, ...] = (
        ToolSpec(
            name="host_capability_search",
            description=(
                "Найти проверенные возможности приложений на локальном Ubuntu-хосте. "
                "Показывает только состояние reviewed adapters; ничего не запускает."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 240},
                    "category": {"type": ["string", "null"], "maxLength": 64},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            security_id="host.capabilities.read",
            risk="observe",
            handler=capability_search,
        ),
        ToolSpec(
            name="host_capability_describe",
            description="Описать точный reviewed adapter, его действия, риски и доступность.",
            parameters={
                "type": "object",
                "properties": {"capability_id": {"type": "string", "maxLength": 128}},
                "required": ["capability_id"],
                "additionalProperties": False,
            },
            security_id="host.capabilities.read",
            risk="observe",
            handler=capability_describe,
        ),
        ToolSpec(
            name="host_action_run",
            description=(
                "Запустить ограниченное действие reviewed adapter. Для nmap передай точные IP/CIDR "
                "и один профиль discover, services или selected_ports; произвольные flags/NSE запрещены."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "capability_id": {"type": "string", "enum": ["network.nmap.scan"]},
                    "action_id": {
                        "type": "string",
                        "enum": ["discover", "services", "selected_ports"],
                    },
                    "targets": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "items": {"type": "string", "minLength": 1, "maxLength": 253},
                    },
                    "ports": {
                        "type": ["array", "null"],
                        "maxItems": 64,
                        "items": {"type": "integer", "minimum": 1, "maximum": 65535},
                    },
                },
                "required": ["capability_id", "action_id", "targets"],
                "additionalProperties": False,
            },
            security_id="host.actions.execute",
            risk="mutate",
            handler=action_run,
            timeout_sec=min(3_600.0, ctx.settings.host_action_default_timeout_sec + 45.0),
        ),
        ToolSpec(
            name="host_job_status",
            description="Проверить durable status точной host-action задачи без повторного запуска.",
            parameters=_job_parameters(),
            security_id="host.jobs.manage",
            risk="observe",
            handler=job_status,
        ),
        ToolSpec(
            name="host_json_extract",
            description=(
                "Извлечь закрытый список полей из принадлежащего владельцу Raw JSON-файла через "
                "reviewed jq adapter. Произвольная jq-программа и host path не принимаются."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "items": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                            "pattern": (
                                "^[A-Za-z_][A-Za-z0-9_-]{0,63}(?:\\.[A-Za-z_][A-Za-z0-9_-]{0,63}){0,15}$"
                            ),
                        },
                    },
                    "compact": {"type": "boolean"},
                },
                "required": ["fields"],
                "additionalProperties": False,
            },
            security_id="host.files.read",
            risk="observe",
            handler=json_extract,
            timeout_sec=min(3_600.0, ctx.settings.host_action_default_timeout_sec + 45.0),
        ),
        ToolSpec(
            name="host_job_cancel",
            description="Остановить exact systemd cgroup указанной host-action задачи и проверить исход.",
            parameters=_job_parameters(),
            security_id="host.jobs.manage",
            risk="mutate",
            handler=job_cancel,
        ),
        ToolSpec(
            name="host_action_execute",
            description="Internal exact-plan host action executor.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "plan": {"type": "object"},
                    "plan_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
                "required": ["job_id", "plan", "plan_digest"],
                "additionalProperties": False,
            },
            security_id="host.actions.execute",
            risk="high",
            handler=action_execute,
            timeout_sec=min(3_600.0, ctx.settings.host_action_default_timeout_sec + 45.0),
            model_visible=False,
            approval_predicate=lambda _arguments: True,
        ),
    )
    if ctx.settings.host_package_install_enabled:
        tools += (
            ToolSpec(
                name="software_install_execute",
                description="Internal exact-plan APT installation executor.",
                parameters={
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "pattern": "^hjob_[0-9a-f]{32}$",
                        },
                        "package_plan": {"type": "object"},
                        "plan_digest": {
                            "type": "string",
                            "pattern": "^[0-9a-f]{64}$",
                        },
                    },
                    "required": ["job_id", "package_plan", "plan_digest"],
                    "additionalProperties": False,
                },
                security_id="host.packages.install",
                risk="high",
                handler=software_install_execute,
                timeout_sec=3_630.0,
                model_visible=False,
                approval_predicate=lambda _arguments: True,
            ),
        )
    return tools


def _job_parameters() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"job_id": {"type": "string", "pattern": "^hjob_[0-9a-f]{32}$"}},
        "required": ["job_id"],
        "additionalProperties": False,
    }


def _known_refusal(exc: Exception, *, job_id: str = "") -> dict[str, Any]:
    if isinstance(exc, AuthorizationError):
        code = "authorization_denied"
    elif isinstance(exc, HostCapabilityUnavailable):
        code = "host_control_unavailable"
    elif isinstance(exc, ContractError):
        code = "invalid_host_action"
    else:
        code = "host_control_unavailable"
    return {
        "effect_boundary_crossed": False,
        "error_code": code,
        "job_id": job_id,
        "ok": False,
        "status": "failed",
    }


def _unknown_install(job_id: str) -> dict[str, Any]:
    return {
        "effect_boundary_crossed": True,
        "error_code": "package_install_unknown",
        "job_id": job_id,
        "ok": False,
        "status": "unknown",
    }


def _unknown_action(job_id: str) -> dict[str, Any]:
    return {
        "effect_boundary_crossed": True,
        "error_code": "host_action_unknown",
        "job_id": job_id,
        "ok": False,
        "status": "unknown",
    }


def _prepared_action_result(result: dict[str, Any]) -> dict[str, Any]:
    """Do not report an unavailable or unfinished action as a successful invocation."""

    if result.get("ok") is False:
        return dict(result)
    status = str(result.get("status") or "")
    job_id = str(result.get("job_id") or "")
    if status in {"unknown", "reconciling", "reconciled"} and job_id:
        return _unknown_action(job_id)
    if status in {"failed", "cancelled"} and job_id:
        return _action_execution_result(result, job_id=job_id)
    if status in {
        "disabled",
        "missing_package",
        "needs_setup",
        "quarantined",
        "unattested",
        "unsupported_version",
    }:
        return {
            **result,
            "effect_boundary_crossed": False,
            "error_code": "host_capability_unavailable",
            "job_id": job_id,
            "ok": False,
        }
    return {
        **result,
        "effect_boundary_crossed": False,
        "error_code": "host_action_not_completed",
        "job_id": job_id,
        "ok": False,
    }


def _action_execution_result(result: object, *, job_id: str) -> dict[str, Any]:
    """Convert a verified service result into the kernel's fail-closed envelope."""

    if not isinstance(result, dict) or result.get("job_id") != job_id:
        return _unknown_action(job_id)
    status = str(result.get("status") or "")
    terminal_outcome = str(result.get("terminal_outcome") or "")
    if status == "reconciled":
        outcome = terminal_outcome
    elif terminal_outcome:
        if terminal_outcome != status:
            return _unknown_action(job_id)
        outcome = terminal_outcome
    else:
        outcome = status

    if outcome in {"completed", "partial"}:
        return {**result, "ok": True}
    if outcome in {"failed", "cancelled"}:
        default_code = "host_action_failed" if outcome == "failed" else "host_action_cancelled"
        error_code = result.get("error_code")
        if not isinstance(error_code, str) or not error_code:
            error_code = default_code
        return {
            **result,
            "effect_boundary_crossed": False,
            "error_code": error_code,
            "ok": False,
        }
    return _unknown_action(job_id)


def _install_execution_result(result: object, *, job_id: str) -> dict[str, Any]:
    """Attach the kernel outcome envelope without weakening broker receipts."""

    if not isinstance(result, dict) or result.get("job_id") != job_id:
        return _unknown_install(job_id)
    outcome = result.get("package_outcome")
    status = result.get("status")
    if outcome == "failed_before_effect" and status == "failed":
        return {
            **result,
            "effect_boundary_crossed": False,
            "error_code": str(result.get("error_code") or "package_failed_before_effect"),
            "ok": False,
        }
    if outcome == "cancelled_before_commit" and status == "cancelled":
        return {
            **result,
            "effect_boundary_crossed": False,
            "error_code": "cancelled_before_commit",
            "ok": False,
        }
    if outcome == "unknown" or status == "unknown":
        return _unknown_install(job_id)
    if outcome not in {"completed", "already_satisfied"} or status not in {
        "cancelled",
        "completed",
        "failed",
        "partial",
        "reconciled",
    }:
        return _unknown_install(job_id)

    crossed = outcome == "completed"
    resumed = result.get("resumed")
    if outcome == "already_satisfied" and isinstance(resumed, dict):
        crossed = resumed.get("status") != "awaiting_approval"
    return {**result, "effect_boundary_crossed": crossed, "ok": True}


__all__ = ["build_host_control_tools"]
