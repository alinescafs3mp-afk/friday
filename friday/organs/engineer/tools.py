"""Owner-only engineer tools with fail-closed network authority."""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from friday.execution_kernel import ToolSpec
from friday.file_delivery import AuthorizedFileReadError, FileRecordUnavailable, read_authorized_file
from friday.organs import ServiceContext
from friday.permissions import ActorContext
from friday.workers._blocking import run_blocking

from . import advice, artifacts, authority, environment, hosts, hunt, local_binaries, sandbox
from .targets import PinnedTarget

_RAW_ID = re.compile(r"^raw_[0-9a-f]{16}$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_NETWORK_TIMEOUT_SEC = 115.0
_ARTIFACT_TIMEOUT_SEC = 45.0


def _parameters(properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def _safe_filename(name: str, suffix: str) -> str:
    stem = _SAFE_NAME.sub("_", Path(str(name or "artifact")).name)[:80] or "artifact"
    normalized_suffix = suffix if suffix.startswith(".") else "." + suffix
    if stem.casefold().endswith(normalized_suffix.casefold()):
        return stem
    return stem + normalized_suffix


def _read_owned(ctx: ServiceContext, actor: ActorContext, raw_id: str) -> Any:
    if not _RAW_ID.fullmatch(str(raw_id or "")):
        raise ValueError("raw_id is not a file handle")
    settings = ctx.settings
    return read_authorized_file(
        ctx.storage,
        Path(settings.files_dir),
        raw_id,
        actor.user_id,
        person_id=actor.own_id,
        max_bytes=min(
            int(getattr(settings, "max_upload_bytes", artifacts.MAX_ANALYZE_BYTES)),
            artifacts.MAX_ANALYZE_BYTES,
        ),
    )


def _verified_target(
    actor: ActorContext,
    host: str,
    target_ticket: str,
    *,
    allowed_cidrs: Sequence[str],
    allow_public: bool,
) -> tuple[PinnedTarget, float]:
    verified = authority.verify_target_ticket(
        target_ticket,
        actor_id=actor.own_id,
        exact_host=host,
    )
    # Ticket authenticity proves who minted the exact address set, not that the
    # operator policy still admits it at the execution seam.  Recheck without
    # DNS before the first socket.  Engineer v1 deliberately has no public HITL
    # carrier, so public targets remain denied here even when the feature flag
    # is enabled.
    hosts.admit_pinned_target_policy(
        verified.target,
        allowed_cidrs=allowed_cidrs,
        allow_public=allow_public,
        public_action_approved=False,
    )
    lifetime = max(0.001, min(_NETWORK_TIMEOUT_SEC, verified.expires_at - time.time()))
    return verified.target, time.monotonic() + lifetime


def _authorized_target_ports(
    target: PinnedTarget,
    ports: Sequence[int] | None,
) -> list[int] | None:
    """Keep an explicit URL port inside the exact signed target scope."""

    implied = target.implied_port
    if implied is None:
        return list(ports) if ports is not None else None
    requested = list(ports or [implied])
    if len(requested) != 1 or isinstance(requested[0], bool) or int(requested[0]) != implied:
        raise ValueError("target ticket does not authorize the requested port")
    return [implied]


def _authorized_http_port(target: PinnedTarget, port: int | None) -> int:
    """Resolve an HTTP port without widening an explicit URL target."""

    selected = target.implied_port if port is None and target.implied_port is not None else port
    if selected is None:
        selected = 80
    if isinstance(selected, bool) or not 1 <= int(selected) <= 65535:
        raise ValueError("port must be between 1 and 65535")
    normalized = int(selected)
    if target.implied_port is not None and normalized != target.implied_port:
        raise ValueError("target ticket does not authorize the requested port")
    return normalized


def _patched_attachment(
    content: bytes,
    operations: Sequence[dict[str, Any]],
    *,
    filename: str,
    max_bytes: int,
    deadline: float,
    workspace_root: Path,
) -> tuple[bytes, list[dict[str, Any]], dict[str, Any]]:
    patched, log, receipt = sandbox.patch_artifact(
        content,
        operations,
        filename,
        deadline=deadline,
        workspace_root=workspace_root,
    )
    if len(patched) > max_bytes:
        raise ValueError("patched artifact exceeds the upload cap")
    return patched, log, receipt


def build_engineer_tools(ctx: ServiceContext) -> tuple[ToolSpec, ...]:
    allowed_cidrs = tuple(getattr(ctx.settings, "host_allowed_cidrs", ()) or ())
    allow_public = bool(getattr(ctx.settings, "host_public_network_enabled", False))

    async def hunt_named(
        *,
        actor: ActorContext,
        host: str,
        target_ticket: str,
        ports: list[int] | None = None,
    ) -> dict[str, Any]:
        try:
            target, deadline = _verified_target(
                actor,
                host,
                target_ticket,
                allowed_cidrs=allowed_cidrs,
                allow_public=allow_public,
            )
            authorized_ports = _authorized_target_ports(target, ports)
            report = await run_blocking(
                hunt.hunt_target,
                target,
                authorized_ports,
                deadline=deadline,
            )
        except (ValueError, TimeoutError) as exc:
            return {"ok": False, "error": str(exc)}
        dossier: dict[str, Any] = {
            "ok": True,
            "hosts": [report],
            "artifacts": [],
            "targets": [target.public_dict()],
            "active_probes_sent": bool(report.get("active_probes_sent")),
            "active_probes": list(report.get("active_probes") or []),
            "exploit_payloads_sent": False,
        }
        dossier["markdown"] = hunt.dossier_markdown(dossier)
        return await hunt.with_secondary(ctx, dossier)

    async def analyze_artifact(*, actor: ActorContext, raw_id: str) -> dict[str, Any]:
        try:
            stored = await run_blocking(_read_owned, ctx, actor, raw_id)
        except FileRecordUnavailable:
            return {"ok": False, "error": "file is not available to this actor"}
        except AuthorizedFileReadError as exc:
            return {"ok": False, "error": exc.reason}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        deadline = time.monotonic() + _ARTIFACT_TIMEOUT_SEC
        try:
            report = await run_blocking(
                hunt.hunt_artifact,
                stored.content,
                stored.filename,
                deadline=deadline,
                workspace_root=Path(ctx.settings.state_dir) / "engineer-tmp",
            )
        except sandbox.EngineerSandboxError as exc:
            return {"ok": False, "error": exc.code, "raw_id": raw_id}
        report["raw_id"] = raw_id
        report["secondary"] = await advice.advise(
            ctx,
            "artifact",
            artifacts.public_finding_payload(report),
        )
        return report

    async def patch_artifact(
        *,
        actor: ActorContext,
        raw_id: str,
        operations: list[dict[str, Any]],
        filename: str = "",
    ) -> dict[str, Any]:
        try:
            stored = await run_blocking(_read_owned, ctx, actor, raw_id)
        except FileRecordUnavailable:
            return {"ok": False, "error": "file is not available to this actor"}
        except AuthorizedFileReadError as exc:
            return {"ok": False, "error": exc.reason}
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        max_bytes = min(
            int(getattr(ctx.settings, "max_upload_bytes", artifacts.MAX_ANALYZE_BYTES)),
            artifacts.MAX_ANALYZE_BYTES,
        )
        try:
            patched, log, receipt = await run_blocking(
                _patched_attachment,
                stored.content,
                operations,
                filename=stored.filename,
                max_bytes=max_bytes,
                deadline=time.monotonic() + _ARTIFACT_TIMEOUT_SEC,
                workspace_root=Path(ctx.settings.state_dir) / "engineer-tmp",
            )
        except (ValueError, TimeoutError, sandbox.EngineerSandboxError) as exc:
            return {"ok": False, "error": str(exc)}
        source_name = filename or stored.filename or "artifact.bin"
        suffix = Path(source_name).suffix or ".bin"
        out_name = _safe_filename(Path(source_name).stem + ".patched", suffix)
        mime = stored.mime_type or "application/octet-stream"
        return {
            "ok": True,
            "raw_id": raw_id,
            "original_sha256": str(receipt.get("original_sha256") or ""),
            "patched_sha256": str(receipt.get("patched_sha256") or ""),
            "size_bytes": len(patched),
            "operations": log,
            "filename": out_name,
            "sandbox": receipt.get("sandbox"),
            "toolchain": receipt.get("toolchain"),
            "_attachment": {
                "kind": "document",
                "filename": out_name,
                "mime_type": mime,
                "content_base64": base64.b64encode(patched).decode("ascii"),
            },
        }

    async def http_enum(
        *,
        actor: ActorContext,
        host: str,
        target_ticket: str,
        port: int | None = None,
    ) -> dict[str, Any]:
        try:
            target, deadline = _verified_target(
                actor,
                host,
                target_ticket,
                allowed_cidrs=allowed_cidrs,
                allow_public=allow_public,
            )
            authorized_port = _authorized_http_port(target, port)
            use_tls = authorized_port in hosts.TLS_PORTS
            hits = await run_blocking(
                hosts.http_hunt,
                target,
                authorized_port,
                use_tls,
                deadline=deadline,
            )
        except (ValueError, TimeoutError) as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "host": target.host,
            "probed_address": target.connect_address,
            "port": authorized_port,
            "hits": hits,
            "active_probes_sent": True,
            "active_probes": ["http_path_head"],
            "exploit_payloads_sent": False,
        }

    async def dns_lookup(
        *,
        actor: ActorContext,
        host: str,
        target_ticket: str,
    ) -> dict[str, Any]:
        try:
            target, deadline = _verified_target(
                actor,
                host,
                target_ticket,
                allowed_cidrs=allowed_cidrs,
                allow_public=allow_public,
            )
            records = await run_blocking(
                local_binaries.dig_records,
                target.host,
                deadline=deadline,
            )
        except (ValueError, TimeoutError) as exc:
            return {"ok": False, "error": str(exc)}
        attempted = bool(records.get("attempted")) or records.get("error") != "resolver_missing"
        return {
            "ok": True,
            **target.public_dict(),
            "dns": records,
            "active_probes_sent": attempted,
            "active_probes": ["dns_lookup"] if attempted else [],
            "exploit_payloads_sent": False,
        }

    async def tool_inventory(*, actor: ActorContext) -> dict[str, Any]:
        del actor
        binaries = await run_blocking(local_binaries.inventory)
        return {
            "ok": True,
            "binaries": {name: bool(path) for name, path in sorted(binaries.items())},
            "environment": environment.environment_passport(
                allowed_cidrs=allowed_cidrs,
                binaries=binaries,
                host_control_enabled=bool(getattr(ctx.settings, "host_control_enabled", False)),
                package_install_enabled=bool(getattr(ctx.settings, "host_package_install_enabled", False)),
            ),
        }

    async def scan_configured_network(
        *,
        actor: ActorContext,
        cidr: str,
        profile: str = "discover",
    ) -> dict[str, Any]:
        del actor
        try:
            snapshot = hosts.configured_private_network_snapshot(
                allowed_cidrs,
                requested_cidr=str(cidr or ""),
            )
        except hosts.EngineerTargetPolicyError as exc:
            return {"ok": False, "error": exc.code, "tool": "nmap"}
        if snapshot.execution_targets != (str(cidr or ""),):
            return {"ok": False, "error": "configured_network_identity_mismatch", "tool": "nmap"}
        try:
            result = await run_blocking(
                local_binaries.nmap_network_scan,
                snapshot,
                profile=profile,
                deadline=time.monotonic() + _NETWORK_TIMEOUT_SEC,
            )
        except TimeoutError:
            return {"ok": False, "error": "deadline", "tool": "nmap"}
        return {
            **result,
            "profile": profile,
            "scope": cidr,
            "target_count": snapshot.target_count,
            "active_probes_sent": result.get("used") is True,
            "active_probes": [f"nmap_{profile}"] if result.get("used") is True else [],
            "exploit_payloads_sent": False,
        }

    port_schema = {
        "type": "array",
        "items": {"type": "integer", "minimum": 1, "maximum": 65535},
        "maxItems": hosts.MAX_PORTS,
        "uniqueItems": True,
    }
    ticket_schema = {
        "type": "string",
        "minLength": 80,
        "maxLength": authority.MAX_TICKET_CHARS,
        "description": "Code-issued current-turn target authority ticket.",
    }
    target_properties = {
        "host": {"type": "string", "minLength": 1, "maxLength": 253},
        "target_ticket": ticket_schema,
    }
    network_required = ("host", "target_ticket")
    return (
        ToolSpec(
            name="engineer_hunt",
            description="Bounded assessment of the single code-authorized current-turn target.",
            parameters=_parameters({**target_properties, "ports": port_schema}, network_required),
            security_id="engineer.host.audit",
            risk="observe",
            timeout_sec=120.0,
            handler=hunt_named,
        ),
        ToolSpec(
            name="engineer_analyze_artifact",
            description="Bounded static analysis of an artifact owned by the current actor.",
            parameters=_parameters(
                {"raw_id": {"type": "string", "minLength": 20, "maxLength": 24}},
                required=("raw_id",),
            ),
            security_id="engineer.artifact.analyze",
            risk="observe",
            timeout_sec=60.0,
            handler=analyze_artifact,
        ),
        ToolSpec(
            name="engineer_patch_artifact",
            description="Emit a bounded patched copy; the owned source Raw is unchanged.",
            parameters=_parameters(
                {
                    "raw_id": {"type": "string", "minLength": 20, "maxLength": 24},
                    "filename": {"type": "string", "maxLength": 180},
                    "operations": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": artifacts.MAX_PATCH_OPS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {
                                    "type": "string",
                                    "enum": ["write_at", "replace_bytes", "zip_replace"],
                                },
                                "offset": {"type": "integer", "minimum": 0},
                                "bytes": {"type": "string", "maxLength": 8192},
                                "find": {"type": "string", "maxLength": 8192},
                                "replace": {"type": "string", "maxLength": 8192},
                                "all": {"type": "boolean"},
                                "name": {"type": "string", "maxLength": 260},
                            },
                            "required": ["kind"],
                            "additionalProperties": False,
                        },
                    },
                },
                required=("raw_id", "operations"),
            ),
            security_id="engineer.artifact.patch",
            risk="mutate",
            timeout_sec=60.0,
            handler=patch_artifact,
        ),
        ToolSpec(
            name="engineer_audit_host",
            description="Audit the exact current-turn target authorized by target_ticket.",
            parameters=_parameters({**target_properties, "ports": port_schema}, network_required),
            security_id="engineer.host.audit",
            risk="observe",
            timeout_sec=120.0,
            handler=hunt_named,
        ),
        ToolSpec(
            name="engineer_http_enum",
            description="Issue bounded HEAD requests to fixed paths on the authorized target.",
            parameters=_parameters(
                {
                    **target_properties,
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                },
                network_required,
            ),
            security_id="engineer.host.audit",
            risk="observe",
            timeout_sec=60.0,
            handler=http_enum,
        ),
        ToolSpec(
            name="engineer_dns",
            description="Bounded DNS evidence for the target authorized by target_ticket.",
            parameters=_parameters(target_properties, network_required),
            security_id="engineer.host.audit",
            risk="observe",
            timeout_sec=30.0,
            handler=dns_lookup,
        ),
        ToolSpec(
            name="engineer_local_tools",
            description="Report only availability of optional local diagnostic binaries.",
            parameters=_parameters({}),
            security_id="engineer.use",
            risk="observe",
            handler=tool_inventory,
        ),
        ToolSpec(
            name="engineer_scan_configured_network",
            description=(
                "Code-owned nmap scan of the sole operator-configured private network; "
                "never exposed as a free-form model target."
            ),
            parameters=_parameters(
                {
                    "cidr": {"type": "string", "minLength": 3, "maxLength": 80},
                    "profile": {"type": "string", "enum": ["discover", "services"]},
                },
                required=("cidr", "profile"),
            ),
            security_id="engineer.host.audit",
            risk="observe",
            timeout_sec=120.0,
            handler=scan_configured_network,
        ),
        ToolSpec(
            name="engineer_adversary_rehearsal",
            description="Defensive rehearsal from bounded evidence; no exploit payload execution.",
            parameters=_parameters({**target_properties, "ports": port_schema}, network_required),
            security_id="engineer.host.audit",
            risk="observe",
            timeout_sec=120.0,
            handler=hunt_named,
        ),
    )


__all__ = ["build_engineer_tools"]
