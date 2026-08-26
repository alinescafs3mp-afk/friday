"""Deterministic orchestration for one pinned target and bounded artifacts."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import advice, artifacts, environment, hosts, sandbox
from .redaction import redact_text
from .targets import PinnedTarget, extract_single_target, requests_active_assessment

MAX_DOSSIER_FILES = 8
MAX_DOSSIER_MARKDOWN_CHARS = 24_000


def hunt_target(
    target: PinnedTarget,
    ports: Sequence[int] | None = None,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Hunt an authority resolved from the current human speech."""

    report = hosts.audit_target(target, ports, rehearsal=True, deadline=deadline)
    report["playbook"] = hosts.rehearsal_playbook(report)
    report["markdown"] = hosts.host_markdown(report)
    return report


def hunt_host(
    host: str,
    ports: Sequence[int] | None = None,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Compatibility entry for trusted callers; model tools use ``hunt_target``."""

    target = hosts.authorize_target(host, source_token=str(host or ""), deadline=deadline)
    return hunt_target(target, ports, deadline=deadline)


def hunt_artifact(
    data: bytes,
    filename: str = "",
    *,
    deadline: float | None = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    return sandbox.analyze_artifact(
        data,
        filename,
        deadline=deadline,
        workspace_root=workspace_root,
    )


def hunt_from_speech(
    speech: str,
    files: Sequence[Mapping[str, Any]] | None = None,
    *,
    deadline: float | None = None,
    workspace_root: Path | None = None,
    allowed_cidrs: Sequence[str] = (),
    allow_public: bool = False,
    public_action_approved: bool = False,
) -> dict[str, Any]:
    """Run an explicitly requested, policy-admitted current-turn assessment."""

    dossier: dict[str, Any] = {
        "ok": True,
        "hosts": [],
        "artifacts": [],
        "targets": [],
        "active_probes_sent": False,
        "active_probes": [],
        "exploit_payloads_sent": False,
        "network_request_status": (
            "explicit_active_request" if requests_active_assessment(speech) else "not_requested"
        ),
    }
    selected = None
    if dossier["network_request_status"] == "explicit_active_request":
        try:
            selected = extract_single_target(speech)
        except ValueError as exc:
            dossier["ok"] = False
            dossier["target_error"] = str(exc)
    if selected is not None:
        try:
            target = hosts.pin_target_from_speech(
                speech,
                deadline=deadline,
                allowed_cidrs=allowed_cidrs,
                allow_public=allow_public,
                public_action_approved=public_action_approved,
            )
            if target is not None:
                dossier["targets"] = [target.public_dict()]
                port = selected.get("port")
                ports = [int(port)] if isinstance(port, int) and not isinstance(port, bool) else None
                report = hunt_target(target, ports, deadline=deadline)
                dossier["hosts"].append(report)
                dossier["active_probes_sent"] = bool(report.get("active_probes_sent"))
                dossier["active_probes"] = list(report.get("active_probes") or [])[:32]
        except (ValueError, TimeoutError) as exc:
            dossier["ok"] = False
            dossier["hosts"].append({"ok": False, "error": str(exc)})
    for item in list(files or ())[:MAX_DOSSIER_FILES]:
        if deadline is not None and time.monotonic() >= deadline:
            dossier["ok"] = False
            dossier["artifact_error"] = "engineer deadline expired"
            break
        if not isinstance(item, Mapping):
            continue
        payload = item.get("content")
        name = str(item.get("filename") or item.get("name") or "artifact")
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            continue
        report = hunt_artifact(
            bytes(payload),
            name,
            deadline=deadline,
            workspace_root=workspace_root,
        )
        report["raw_id"] = str(item.get("raw_id") or item.get("raw_object_id") or "")
        dossier["artifacts"].append(report)
    dossier["markdown"] = dossier_markdown(dossier)
    return dossier


async def with_secondary(ctx: Any, dossier: Mapping[str, Any]) -> dict[str, Any]:
    public: dict[str, Any] = {
        "hosts": [
            hosts.public_host_payload(item)
            for item in list(dossier.get("hosts") or [])
            if isinstance(item, Mapping) and item.get("ok")
        ],
        "artifacts": [
            artifacts.public_finding_payload(item)
            for item in list(dossier.get("artifacts") or [])
            if isinstance(item, Mapping) and item.get("ok")
        ],
    }
    enriched = dict(dossier)
    enriched["secondary"] = await advice.advise(ctx, "hunt", public)
    # The optional advice is useful only if the primary Qwen turn can see it.
    # Render it as explicitly untrusted evidence; disabled/failure advice adds
    # no bytes and therefore preserves the deterministic primary markdown.
    enriched["markdown"] = dossier_markdown(enriched)
    return enriched


def _secondary_markdown(value: object) -> str:
    if not isinstance(value, Mapping) or value.get("used") is not True:
        return ""
    narrative = value.get("narrative")
    if not isinstance(narrative, str) or not narrative.strip() or len(narrative) > 2_400:
        return ""
    raw_priorities = value.get("priorities")
    if raw_priorities is not None and (
        not isinstance(raw_priorities, list)
        or len(raw_priorities) > 8
        or any(not isinstance(item, str) or not item.strip() or len(item) > 240 for item in raw_priorities)
    ):
        return ""
    priorities = raw_priorities if isinstance(raw_priorities, list) else []
    lines = [
        "## Untrusted secondary advisory",
        redact_text(narrative, limit=2_400, single_line=False),
    ]
    lines.extend(f"- {redact_text(item, limit=240)}" for item in priorities)
    return "\n".join(lines)


def _network_scan_markdown(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    scope = redact_text(value.get("scope"), limit=80)
    target_count = value.get("target_count")
    if value.get("ok") is not True:
        return (
            "## Configured network scan\n"
            f"scope: `{scope or 'unknown'}`\n"
            f"status: unavailable ({redact_text(value.get('error'), limit=120) or 'unknown'})"
        )
    projection = value.get("report")
    projection = projection if isinstance(projection, Mapping) else {}
    structured = projection.get("result")
    structured = structured if isinstance(structured, Mapping) else {}
    coverage = value.get("coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    lines = [
        "## Configured network scan",
        f"scope: `{scope or 'unknown'}`",
        f"profile: `{redact_text(value.get('profile'), limit=24) or 'unknown'}`",
        "coverage: "
        f"{redact_text(coverage.get('grade'), limit=24) or 'unknown'}; "
        f"accounted {coverage.get('accounted', '?')}/{coverage.get('requested', target_count or '?')}",
        f"nmap version: {redact_text(structured.get('nmap_version'), limit=80) or 'unknown'}",
        f"hosts up: {structured.get('hosts_up', '?')}; "
        f"down/unknown: {structured.get('hosts_down_or_unknown', '?')}",
        "exploit payloads sent: no",
    ]
    raw_hosts = structured.get("hosts")
    host_rows = raw_hosts if isinstance(raw_hosts, list) else []
    for item in host_rows[:256]:
        if not isinstance(item, Mapping) or str(item.get("state") or "") != "up":
            continue
        addresses = item.get("addresses")
        addresses = addresses if isinstance(addresses, list) else []
        address_text = ", ".join(
            redact_text(entry.get("address"), limit=80)
            for entry in addresses[:8]
            if isinstance(entry, Mapping) and entry.get("address")
        )
        names = item.get("hostnames")
        names = names if isinstance(names, list) else []
        name_text = ", ".join(redact_text(name, limit=253) for name in names[:8] if name)
        ports = item.get("ports")
        ports = ports if isinstance(ports, list) else []
        open_ports = [
            str(entry.get("port"))
            for entry in ports[:64]
            if isinstance(entry, Mapping) and entry.get("state") == "open"
        ]
        detail = f"- `{address_text or 'address unavailable'}`"
        if name_text:
            detail += f" names: {name_text}"
        if open_ports:
            detail += " open: " + ",".join(open_ports)
        lines.append(detail)
    evidence = value.get("evidence")
    evidence = evidence if isinstance(evidence, list) else []
    first = evidence[0] if evidence and isinstance(evidence[0], Mapping) else {}
    digest = str(first.get("sha256") or "")
    if len(digest) == 64:
        lines.append(f"nmap XML evidence sha256: `{digest}`")
    return "\n".join(lines)[:12_000]


def dossier_markdown(dossier: Mapping[str, Any]) -> str:
    parts: list[str] = []
    passport = dossier.get("environment")
    if isinstance(passport, Mapping):
        parts.append(environment.environment_markdown(passport))
    target_error = dossier.get("target_error")
    if target_error:
        parts.append("Network target refused: " + redact_text(target_error, limit=240))
    for report in list(dossier.get("hosts") or [])[:1]:
        if not isinstance(report, Mapping):
            continue
        if report.get("markdown"):
            parts.append(str(report["markdown"])[:12_000])
        elif report.get("error"):
            parts.append("# Target unavailable\nerror: " + redact_text(report["error"], limit=240))
    network_scan = _network_scan_markdown(dossier.get("network_scan"))
    if network_scan:
        parts.append(network_scan)
    secondary = _secondary_markdown(dossier.get("secondary"))
    if secondary:
        parts.append(secondary)
    for report in list(dossier.get("artifacts") or [])[:MAX_DOSSIER_FILES]:
        if not isinstance(report, Mapping):
            continue
        artifact_ref = str(report.get("artifact_ref") or "")
        prefix = f"Artifact reference: `{artifact_ref}`\n" if artifact_ref else ""
        if report.get("markdown"):
            parts.append(prefix + str(report["markdown"])[:6_000])
        report_secondary = _secondary_markdown(report.get("secondary"))
        if report_secondary:
            parts.append(report_secondary)
    if not parts:
        parts.append("No authorized target or readable artifact was supplied in this turn.")
    return "\n\n".join(parts)[:MAX_DOSSIER_MARKDOWN_CHARS]


__all__ = [
    "dossier_markdown",
    "hunt_artifact",
    "hunt_from_speech",
    "hunt_host",
    "hunt_target",
    "with_secondary",
]
