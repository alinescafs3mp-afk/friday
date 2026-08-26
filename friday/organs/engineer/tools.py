"""Owner-only engineer tools with fail-closed network authority."""

from __future__ import annotations

import base64
import hashlib
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from friday.execution_kernel import ToolSpec
from friday.file_delivery import AuthorizedFileReadError, FileRecordUnavailable, read_authorized_file
from friday.organs import ServiceContext
from friday.permissions import ActorContext
from friday.workers._blocking import run_blocking

from . import advice, artifacts, authority, decompiler, environment, hosts, hunt, local_binaries, sandbox
from .targets import PinnedTarget

_RAW_ID = re.compile(r"^raw_[0-9a-f]{16}$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_NETWORK_TIMEOUT_SEC = 115.0
_ARTIFACT_TIMEOUT_SEC = 45.0
_DECOMPILE_TIMEOUT_SEC = 245.0
_DECOMPILE_MARKDOWN_MAX_BYTES = 1024 * 1024
_DECOMPILE_FUNCTION_STATUSES = frozenset({"completed", "failed", "timeout"})
_DECOMPILE_WARNING_CODES = frozenset({"analysis_timeout", "function_index_truncated", "pseudocode_truncated"})
_DECOMPILE_FAILURE_STATUSES = {
    "decompiler_busy": "unavailable",
    "unsupported_format": "unsupported",
    "toolchain_missing": "unavailable",
    "toolchain_incomplete": "unavailable",
    "toolchain_untrusted": "unavailable",
    "decompiler_timeout": "failed",
    "decompiler_launch_failed": "failed",
    "decompiler_failed": "failed",
    "decompiler_output_invalid": "failed",
    "input_size_invalid": "failed",
    "input_unavailable": "failed",
    "workspace_not_clean": "failed",
    "decompiler_report_invalid": "failed",
    "decompiler_report_exceeds_cap": "failed",
    "deadline_expired": "unavailable",
    "sandbox_unavailable": "unavailable",
    "file_unavailable": "unavailable",
    "file_access_denied": "unavailable",
    "invalid_artifact_handle": "failed",
}


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
    authorization = ctx.auth
    if authorization is None or not authorization.authorize(actor, "files.read").allowed:
        raise AuthorizedFileReadError("artifact", "file_access_denied")
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


def _decompile_failure(error: str, *, work_started: bool) -> dict[str, Any]:
    code = error if error in _DECOMPILE_FAILURE_STATUSES else "decompiler_report_invalid"
    return {
        "ok": False,
        "status": _DECOMPILE_FAILURE_STATUSES[code],
        "error": code,
        "_work_started": work_started,
    }


def _bounded_report_text(value: object, maximum: int, *, raw_id: str) -> tuple[str, bool]:
    source = value if isinstance(value, str) else ""
    text = source[:maximum]
    truncated = len(source) > maximum
    if raw_id and raw_id in text:
        text = text.replace(raw_id, "[artifact-redacted]")
    cleaned = "".join(
        char if char in {"\n", "\t"} or not unicodedata.category(char).startswith("C") else "\ufffd"
        for char in text
    )
    return cleaned, truncated


def _bounded_report_count(value: object, maximum: int) -> int:
    if type(value) is not int:
        return 0
    return max(0, min(int(value), maximum))


def _project_decompile_success(
    raw: Mapping[str, Any],
    *,
    raw_id: str,
) -> tuple[str, dict[str, Any], list[dict[str, Any]], str, str] | None:
    """Validate the worker carrier and split structural facts from artifact text."""

    status = raw.get("status")
    kind = raw.get("format")
    if (
        raw.get("ok") is not True
        or status not in {"completed", "partial"}
        or raw.get("schema") != decompiler.SCHEMA
        or raw.get("tool_name") != decompiler.TOOL_NAME
        or raw.get("tool_version") != decompiler.GHIDRA_VERSION
        or raw.get("jdk_version") != decompiler.JDK_VERSION
        or kind not in decompiler.SUPPORTED_KINDS
        or raw.get("observe_only") is not True
        or raw.get("sample_executed") is not False
        or raw.get("network") != "none"
    ):
        return None

    raw_functions = raw.get("functions")
    if not isinstance(raw_functions, list):
        return None
    emitted_source = raw_functions[: decompiler.MAX_FUNCTIONS]
    index_truncated = bool(raw.get("function_index_truncated")) or len(raw_functions) > len(emitted_source)
    remaining_pseudocode = decompiler.MAX_TOTAL_PSEUDOCODE_CHARS
    functions: list[dict[str, Any]] = []
    text_was_truncated = False
    for item in emitted_source:
        if not isinstance(item, Mapping):
            return None
        function_status = item.get("decompile_status")
        if function_status not in _DECOMPILE_FUNCTION_STATUSES:
            function_status = "failed"
        address, address_truncated = _bounded_report_text(item.get("address"), 32, raw_id=raw_id)
        name, name_truncated = _bounded_report_text(
            item.get("name"), decompiler.MAX_FUNCTION_NAME_CHARS, raw_id=raw_id
        )
        signature, signature_truncated = _bounded_report_text(
            item.get("signature"), decompiler.MAX_SIGNATURE_CHARS, raw_id=raw_id
        )
        pseudocode_limit = min(decompiler.MAX_PSEUDOCODE_CHARS_PER_FUNCTION, remaining_pseudocode)
        pseudocode, pseudocode_truncated = _bounded_report_text(
            item.get("pseudocode"), pseudocode_limit, raw_id=raw_id
        )
        remaining_pseudocode -= len(pseudocode)
        item_truncated = bool(item.get("pseudocode_truncated")) or pseudocode_truncated
        text_was_truncated = text_was_truncated or any(
            (address_truncated, name_truncated, signature_truncated, item_truncated)
        )
        functions.append(
            {
                "address": address,
                "name": name,
                "signature": signature,
                "pseudocode": pseudocode,
                "decompile_status": function_status,
                "pseudocode_truncated": item_truncated,
                "thunk": item.get("thunk") is True,
            }
        )

    warnings: list[str] = []
    raw_warnings = raw.get("warnings")
    for warning in raw_warnings if isinstance(raw_warnings, list) else ():
        if warning in _DECOMPILE_WARNING_CODES and warning not in warnings:
            warnings.append(warning)
    analysis_timed_out = raw.get("analysis_timed_out") is True
    if analysis_timed_out and "analysis_timeout" not in warnings:
        warnings.append("analysis_timeout")
    if index_truncated and "function_index_truncated" not in warnings:
        warnings.append("function_index_truncated")
    output_truncated = raw.get("output_truncated") is True or text_was_truncated
    if output_truncated and "pseudocode_truncated" not in warnings:
        warnings.append("pseudocode_truncated")
    functions_decompiled = sum(item["decompile_status"] == "completed" for item in functions)
    functions_timed_out = sum(item["decompile_status"] == "timeout" for item in functions)
    final_status = (
        "partial"
        if status == "partial"
        or warnings
        or any(item["decompile_status"] != "completed" for item in functions)
        else "completed"
    )
    report = {
        "schema": decompiler.SCHEMA,
        "format": kind,
        "tool_name": decompiler.TOOL_NAME,
        "tool_version": decompiler.GHIDRA_VERSION,
        "jdk_version": decompiler.JDK_VERSION,
        "function_count_lower_bound": max(
            len(functions),
            _bounded_report_count(raw.get("function_count_lower_bound"), decompiler.MAX_SCANNED_FUNCTIONS),
        ),
        "functions_emitted": len(functions),
        "functions_decompiled": functions_decompiled,
        "functions_timed_out": functions_timed_out,
        "pseudocode_chars": sum(len(item["pseudocode"]) for item in functions),
        "analysis_timed_out": analysis_timed_out,
        "function_index_truncated": index_truncated,
        "output_truncated": output_truncated,
        "warnings": warnings,
        "observe_only": True,
        "sample_executed": False,
        "network": "none",
        "report_prepared": True,
    }
    language_id, _ = _bounded_report_text(raw.get("language_id"), 160, raw_id=raw_id)
    compiler_spec_id, _ = _bounded_report_text(raw.get("compiler_spec_id"), 160, raw_id=raw_id)
    closed_identifier = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,159}$")
    if closed_identifier.fullmatch(language_id) is None:
        language_id = ""
    if closed_identifier.fullmatch(compiler_spec_id) is None:
        compiler_spec_id = ""
    return final_status, report, functions, language_id, compiler_spec_id


def _decompile_markdown(
    *,
    status: str,
    report: Mapping[str, Any],
    functions: Sequence[Mapping[str, Any]],
    language_id: str,
    compiler_spec_id: str,
) -> bytes:
    """Render a bounded report without source handles, host paths or raw stderr."""

    lines = [
        "# Friday Engineer decompilation report",
        "",
        "Static, network-isolated analysis. The uploaded program was not executed.",
        "",
        "## Structural result",
        "",
        f"- Status: {status}",
        f"- Format: {report['format']}",
        f"- Tool: {report['tool_name']} {report['tool_version']}",
        f"- JDK: {report['jdk_version']}",
        f"- Functions found (lower bound): {report['function_count_lower_bound']}",
        f"- Functions emitted: {report['functions_emitted']}",
        f"- Functions decompiled: {report['functions_decompiled']}",
        f"- Functions timed out: {report['functions_timed_out']}",
        f"- Analysis timed out: {str(report['analysis_timed_out']).lower()}",
        f"- Function index truncated: {str(report['function_index_truncated']).lower()}",
        f"- Output truncated: {str(report['output_truncated']).lower()}",
        "- Sample executed: false",
        "- Network: none",
        "",
        "### Language ID",
        "",
        "    " + (language_id or "not reported"),
        "",
        "### Compiler specification ID",
        "",
        "    " + (compiler_spec_id or "not reported"),
    ]
    warnings = report.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.extend(("", "### Warnings", ""))
        lines.extend(f"- {warning}" for warning in warnings)
    for index, function in enumerate(functions, start=1):
        lines.extend(("", f"## Function {index}", "", "### Address", ""))
        lines.append("    " + (str(function.get("address") or "not reported")))
        lines.extend(("", "### Name", ""))
        lines.extend("    " + line for line in str(function.get("name") or "not reported").split("\n"))
        lines.extend(("", "### Signature", ""))
        lines.extend("    " + line for line in str(function.get("signature") or "not reported").split("\n"))
        lines.extend(
            (
                "",
                f"- Decompile status: {function.get('decompile_status')}",
                f"- Thunk: {str(function.get('thunk') is True).lower()}",
                f"- Pseudocode truncated: {str(function.get('pseudocode_truncated') is True).lower()}",
                "",
                "### Pseudocode",
                "",
            )
        )
        pseudocode = str(function.get("pseudocode") or "")
        lines.extend("    " + line for line in (pseudocode.split("\n") or [""]))
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8", errors="strict")


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

    async def decompile_owned_artifact(*, actor: ActorContext, raw_id: str) -> dict[str, Any]:
        try:
            stored = await run_blocking(_read_owned, ctx, actor, raw_id)
        except FileRecordUnavailable:
            return _decompile_failure("file_unavailable", work_started=False)
        except AuthorizedFileReadError:
            return _decompile_failure("file_access_denied", work_started=False)
        except ValueError:
            return _decompile_failure("invalid_artifact_handle", work_started=False)
        try:
            raw_report = await run_blocking(
                sandbox.decompile_artifact,
                stored.content,
                stored.filename,
                deadline=time.monotonic() + _DECOMPILE_TIMEOUT_SEC,
                workspace_root=Path(ctx.settings.state_dir) / "engineer-tmp",
            )
        except sandbox.EngineerSandboxError as exc:
            error = (
                "decompiler_timeout"
                if exc.code == "worker_timeout"
                else exc.code
                if exc.code in _DECOMPILE_FAILURE_STATUSES
                else "sandbox_unavailable"
            )
            return _decompile_failure(error, work_started=exc.work_started)
        if not isinstance(raw_report, Mapping):
            return _decompile_failure("decompiler_report_invalid", work_started=True)
        if raw_report.get("ok") is not True:
            return _decompile_failure(
                str(raw_report.get("error") or "decompiler_failed"),
                work_started=True,
            )
        projected = _project_decompile_success(raw_report, raw_id=raw_id)
        if projected is None:
            return _decompile_failure("decompiler_report_invalid", work_started=True)
        status, report, functions, language_id, compiler_spec_id = projected
        markdown = _decompile_markdown(
            status=status,
            report=report,
            functions=functions,
            language_id=language_id,
            compiler_spec_id=compiler_spec_id,
        )
        configured_file_cap = max(0, int(getattr(ctx.settings, "max_upload_bytes", 0)))
        report_byte_cap = min(_DECOMPILE_MARKDOWN_MAX_BYTES, configured_file_cap)
        if not report_byte_cap or len(markdown) > report_byte_cap:
            return _decompile_failure("decompiler_report_exceeds_cap", work_started=True)
        report["report_sha256"] = hashlib.sha256(markdown).hexdigest()
        source_stem = Path(str(stored.filename or "artifact")).stem or "artifact"
        source_stem = re.sub(re.escape(raw_id), "artifact", source_stem, flags=re.IGNORECASE)
        output_name = _safe_filename(source_stem + ".decompiled", ".md")
        return {
            "ok": True,
            "_work_started": True,
            "status": status,
            "summary": (
                "Static decompilation completed; the bounded report is prepared for delivery."
                if status == "completed"
                else "Static decompilation completed partially; the bounded report is prepared for delivery."
            ),
            "report": report,
            "_attachment": {
                "kind": "document",
                "filename": output_name,
                "mime_type": "text/markdown",
                "content_base64": base64.b64encode(markdown).decode("ascii"),
            },
        }

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

    async def assess_host_vulnerabilities(
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
            snapshot = hosts.admit_pinned_target_policy(
                target,
                allowed_cidrs=allowed_cidrs,
                allow_public=False,
                public_action_approved=False,
            )
            classifications = {item.classification for item in snapshot.bindings}
            if (
                snapshot.target_count != 1
                or not classifications
                or not classifications.issubset({"operator_approved_private", "approved_ipv6_ula"})
            ):
                raise hosts.EngineerTargetPolicyError("private_single_host_required")
            result = await run_blocking(
                hosts.assess_target_vulnerabilities,
                target,
                deadline=deadline,
            )
        except (ValueError, TimeoutError) as exc:
            return {
                "ok": False,
                "error": str(exc),
                "active_probes_sent": False,
                "exploit_payloads_sent": False,
                "_work_started": False,
            }
        return {**result, "_work_started": True}

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
            name="engineer_decompile_artifact",
            description="Internal bounded static decompilation of one code-authorized owned artifact.",
            parameters=_parameters(
                {"raw_id": {"type": "string", "minLength": 20, "maxLength": 24}},
                required=("raw_id",),
            ),
            security_id="engineer.artifact.analyze",
            risk="observe",
            timeout_sec=250.0,
            model_visible=False,
            handler=decompile_owned_artifact,
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
            name="engineer_assess_host_vulnerabilities",
            description=(
                "Internal code-owned light nmap service/exposure assessment of one exact "
                "authorized private host; no arbitrary flags, exploits or CVE claims."
            ),
            parameters=_parameters(target_properties, network_required),
            security_id="engineer.host.audit",
            risk="observe",
            timeout_sec=120.0,
            model_visible=False,
            handler=assess_host_vulnerabilities,
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
