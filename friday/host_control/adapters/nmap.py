"""Reviewed nmap adapter: closed argv profiles and safe bounded XML parsing."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import subprocess
import xml.etree.ElementTree as ET
from typing import Any

from ..contracts import (
    ContractError,
    Coverage,
    CoverageGrade,
    EvidenceRef,
    ExecutableAttestation,
    ExecutionProfile,
    ParsedActionResult,
    ParserStatus,
    RiskClass,
)
from ..plans import HostActionPlan
from ..policy import NetworkTargetSnapshot
from .base import (
    ActionSpec,
    AdapterSpec,
    ExecutableRequirement,
    ExecutionSpec,
    PackageRequirement,
    attest_plan,
)

MAX_XML_BYTES = 8 * 1024 * 1024
MAX_PORTS = 64
MAX_HOST_ROWS = 4096
MAX_PORT_ROWS = 8192
NMAP_EXECUTABLE = "/usr/bin/nmap"
SERVICE_PORTS = (22, 25, 53, 80, 110, 143, 443, 445, 587, 993, 995, 3000, 5432, 8000, 8080, 8443)
_FORBIDDEN_XML = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_CANONICAL_NMAP_DOCTYPE = b"<!DOCTYPE nmaprun>"


NMAP_SPEC = AdapterSpec(
    adapter_id="network.nmap",
    adapter_schema_version=1,
    implementation_version=1,
    summary="Discover hosts and inspect services with bounded unprivileged nmap profiles.",
    categories=("network", "inspection"),
    supported_platforms=("ubuntu",),
    packages=(PackageRequirement("apt", "nmap"),),
    executable=ExecutableRequirement("nmap", "nmap", (NMAP_EXECUTABLE,)),
    actions=(
        ActionSpec(
            action_id="discover",
            capability_id="network.nmap.scan",
            summary="Bounded host discovery over an exact policy-approved target set.",
            security_id="host.network.scan",
            risk_class=RiskClass.NETWORK_OBSERVE,
            execution_profile=ExecutionProfile.CLI_NETWORK_UNPRIVILEGED,
            input_schema_id="nmap_discover_v1",
            output_parser_id="nmap_xml_v1",
            timeout_sec=300,
            max_output_bytes=MAX_XML_BYTES,
        ),
        ActionSpec(
            action_id="services",
            capability_id="network.nmap.scan",
            summary="Unprivileged TCP connect scan with light service identification.",
            security_id="host.network.scan",
            risk_class=RiskClass.NETWORK_OBSERVE,
            execution_profile=ExecutionProfile.CLI_NETWORK_UNPRIVILEGED,
            input_schema_id="nmap_services_v1",
            output_parser_id="nmap_xml_v1",
            timeout_sec=300,
            max_output_bytes=MAX_XML_BYTES,
        ),
        ActionSpec(
            action_id="selected_ports",
            capability_id="network.nmap.scan",
            summary="Unprivileged TCP connect scan of at most 64 exact ports.",
            security_id="host.network.scan",
            risk_class=RiskClass.NETWORK_OBSERVE,
            execution_profile=ExecutionProfile.CLI_NETWORK_UNPRIVILEGED,
            input_schema_id="nmap_selected_ports_v1",
            output_parser_id="nmap_xml_v1",
            timeout_sec=300,
            max_output_bytes=MAX_XML_BYTES,
        ),
    ),
)


def probe_nmap_version(
    executable: str = NMAP_EXECUTABLE,
    executable_fd: int | None = None,
) -> str:
    """Return the bounded code-owned version identity used by every nmap executor."""

    if executable != NMAP_EXECUTABLE:
        raise ContractError("nmap version probe path is not the reviewed fixed path")
    launcher = executable if executable_fd is None else f"/proc/self/fd/{executable_fd}"
    pass_fds = () if executable_fd is None else (executable_fd,)
    try:
        completed = subprocess.run(  # noqa: S603 - executable and argv are fixed above
            (NMAP_EXECUTABLE, "--version"),
            executable=launcher,
            pass_fds=pass_fds,
            env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError("nmap version probe failed") from exc
    output = completed.stdout or completed.stderr
    if completed.returncode != 0 or not output or len(output) > 4096:
        raise ContractError("nmap version probe failed")
    first_line = output.decode("utf-8", errors="replace").splitlines()[0].strip()
    if not first_line.startswith("Nmap version ") or len(first_line) > 240:
        raise ContractError("nmap version identity is invalid")
    return first_line


def _safe_int(value: object, *, minimum: int = 0, maximum: int = 2**31 - 1) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def _bounded_attr(element: ET.Element, name: str, limit: int) -> str:
    return " ".join(str(element.attrib.get(name) or "").split())[:limit]


def _normalize_selected_ports(value: object) -> tuple[int, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_PORTS:
        raise ContractError("nmap selected port list is invalid")
    if any(isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 65535 for item in value):
        raise ContractError("nmap selected port is invalid")
    ports = tuple(sorted(set(value)))
    if len(ports) != len(value):
        raise ContractError("nmap selected ports must be unique")
    return ports


def normalize_nmap_arguments(
    action_id: str,
    arguments: dict[str, Any],
    *,
    target_snapshot: NetworkTargetSnapshot,
) -> dict[str, Any]:
    """Bind one closed nmap action to the exact shared target snapshot."""

    NMAP_SPEC.action(action_id)
    allowed = (
        {"target_snapshot_digest", "ports"} if action_id == "selected_ports" else {"target_snapshot_digest"}
    )
    if set(arguments) - allowed:
        raise ContractError("nmap arguments contain unsupported fields")
    if arguments.get("target_snapshot_digest") != target_snapshot.digest:
        raise ContractError("nmap arguments are not bound to the target snapshot")
    normalized: dict[str, Any] = {
        "target_count": target_snapshot.target_count,
        "target_snapshot_digest": target_snapshot.digest,
        "targets": list(target_snapshot.execution_targets),
    }
    if action_id == "selected_ports":
        normalized["ports"] = list(_normalize_selected_ports(arguments.get("ports")))
    return normalized


def _validated_execution_targets(target_snapshot: NetworkTargetSnapshot) -> tuple[str, ...]:
    targets = target_snapshot.execution_targets
    if not targets or len(targets) > 64:
        raise ContractError("nmap execution target set is invalid")
    for target in targets:
        try:
            parsed: (
                ipaddress.IPv4Address | ipaddress.IPv6Address | ipaddress.IPv4Network | ipaddress.IPv6Network
            )
            parsed = (
                ipaddress.ip_network(target, strict=True) if "/" in target else ipaddress.ip_address(target)
            )
        except ValueError as exc:
            raise ContractError("nmap execution target is not an IP/CIDR") from exc
        if str(parsed) != target:
            raise ContractError("nmap execution target is not canonical")
    return targets


def build_nmap_execution(
    action_id: str,
    *,
    target_snapshot: NetworkTargetSnapshot,
    ports: list[int] | None = None,
    executable: str = NMAP_EXECUTABLE,
) -> ExecutionSpec:
    """Build the only reviewed nmap argv used by host control and Engineer Mode."""

    if executable != NMAP_EXECUTABLE:
        raise ContractError("nmap executable path is not the reviewed fixed path")
    action = NMAP_SPEC.action(action_id)
    targets = _validated_execution_targets(target_snapshot)
    argv = [
        NMAP_EXECUTABLE,
        "-n",
        "-v",
        "--max-retries",
        "2",
        "--host-timeout",
        "60s",
        "--max-rate",
        "100",
        "--max-hostgroup",
        "32",
    ]
    if action_id == "discover":
        if ports is not None:
            raise ContractError("nmap discovery cannot carry selected ports")
        argv.extend(("-sn", "-oX", "-"))
    elif action_id == "services":
        if ports is not None:
            raise ContractError("nmap services profile cannot carry selected ports")
        argv.extend(
            (
                "-Pn",
                "-sT",
                "-sV",
                "--version-light",
                "-p",
                ",".join(str(item) for item in SERVICE_PORTS),
                "-oX",
                "-",
            )
        )
    elif action_id == "selected_ports":
        selected_ports = _normalize_selected_ports(ports)
        argv.extend(
            (
                "-Pn",
                "-sT",
                "-sV",
                "--version-light",
                "-p",
                ",".join(str(item) for item in selected_ports),
                "-oX",
                "-",
            )
        )
    else:  # pragma: no cover - action() already closes this branch
        raise ContractError("nmap action is unsupported")
    argv.extend(targets)
    return ExecutionSpec(
        executable=NMAP_EXECUTABLE,
        argv=tuple(argv),
        profile=action.execution_profile,
        timeout_sec=action.timeout_sec,
        max_output_bytes=action.max_output_bytes,
    )


class NmapAdapter:
    spec = NMAP_SPEC

    def normalize_arguments(
        self,
        action_id: str,
        arguments: dict[str, Any],
        *,
        target_snapshot: NetworkTargetSnapshot | None = None,
    ) -> dict[str, Any]:
        if target_snapshot is None:
            raise ContractError("nmap requires an exact target snapshot")
        return normalize_nmap_arguments(
            action_id,
            arguments,
            target_snapshot=target_snapshot,
        )

    def build_execution(
        self,
        plan: HostActionPlan,
        attestation: ExecutableAttestation,
    ) -> ExecutionSpec:
        action_id = plan.action_id
        normalized_arguments = plan.normalized_arguments
        attest_plan(self.spec, plan, attestation)
        snapshot_payload = plan.target_snapshot
        if snapshot_payload is None:
            raise ContractError("nmap plan lost its exact target snapshot")
        target_snapshot = NetworkTargetSnapshot.from_payload(snapshot_payload)
        if (
            target_snapshot.digest != plan.target_snapshot_digest
            or normalized_arguments.get("target_snapshot_digest") != target_snapshot.digest
        ):
            raise ContractError("nmap plan lost its exact target snapshot binding")
        supplied: dict[str, Any] = {"target_snapshot_digest": target_snapshot.digest}
        ports: list[int] | None = None
        if action_id == "selected_ports":
            raw_ports = normalized_arguments.get("ports")
            if isinstance(raw_ports, list):
                ports = raw_ports
            supplied["ports"] = raw_ports
        expected = normalize_nmap_arguments(
            action_id,
            supplied,
            target_snapshot=target_snapshot,
        )
        if normalized_arguments != expected:
            raise ContractError("nmap normalized plan arguments drifted")
        return build_nmap_execution(
            action_id,
            target_snapshot=target_snapshot,
            ports=ports,
            executable=attestation.canonical_path,
        )

    def parse_xml(
        self,
        payload: bytes,
        *,
        target_snapshot: NetworkTargetSnapshot,
        exit_code: int | None,
        timed_out: bool = False,
        truncated: bool = False,
        evidence: tuple[EvidenceRef, ...] = (),
    ) -> ParsedActionResult:
        return parse_nmap_xml(
            payload,
            target_snapshot=target_snapshot,
            exit_code=exit_code,
            timed_out=timed_out,
            truncated=truncated,
            evidence=evidence,
        )


def parse_nmap_xml(
    payload: bytes,
    *,
    target_snapshot: NetworkTargetSnapshot,
    exit_code: int | None,
    timed_out: bool = False,
    truncated: bool = False,
    evidence: tuple[EvidenceRef, ...] = (),
) -> ParsedActionResult:
    warnings: list[str] = []
    unavailable_reason = ""
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_XML_BYTES:
        unavailable_reason = "xml_missing_or_oversized"
    parse_payload = payload
    if not unavailable_reason:
        # Nmap's own ``-oX -`` output contains one inert canonical doctype.
        # Strip only that exact declaration; every other DTD/entity spelling
        # remains forbidden before ElementTree sees the bytes.
        if parse_payload.count(_CANONICAL_NMAP_DOCTYPE) == 1:
            parse_payload = parse_payload.replace(_CANONICAL_NMAP_DOCTYPE, b"", 1)
        if _FORBIDDEN_XML.search(parse_payload):
            unavailable_reason = "xml_entities_forbidden"
    root: ET.Element | None = None
    if not unavailable_reason:
        try:
            parser = ET.XMLParser()
            root = ET.fromstring(parse_payload, parser=parser)
        except ET.ParseError:
            unavailable_reason = "xml_malformed_or_truncated"
    if root is None or root.tag != "nmaprun":
        reason = unavailable_reason or "xml_root_invalid"
        return ParsedActionResult.create(
            parser_id="nmap_xml_v1",
            parser_status=ParserStatus.UNAVAILABLE,
            structured={
                "hosts": [],
                "nmap_version": "",
                "open_ports": 0,
                "targets_requested": target_snapshot.target_count,
                "targets_scanned": 0,
            },
            coverage=Coverage(
                CoverageGrade.UNAVAILABLE,
                requested=target_snapshot.target_count,
                accounted=0,
                reasons=(reason,),
            ),
            warnings=(reason,),
            evidence=evidence,
        )

    hosts: list[dict[str, Any]] = []
    open_ports = 0
    port_rows = 0
    requested_addresses: set[str] = set()
    for execution_target in target_snapshot.execution_targets:
        try:
            network = ipaddress.ip_network(execution_target, strict=True)
        except ValueError:
            network = ipaddress.ip_network(
                f"{ipaddress.ip_address(execution_target)}/{ipaddress.ip_address(execution_target).max_prefixlen}",
                strict=True,
            )
        requested_addresses.update(str(item) for item in network)
    observed_addresses: set[str] = set()
    duplicate_addresses = False
    out_of_scope_addresses = False
    for host in root.findall("host")[:MAX_HOST_ROWS]:
        status_element = host.find("status")
        state = _bounded_attr(status_element, "state", 24) if status_element is not None else "unknown"
        addresses = []
        for address in host.findall("address")[:8]:
            value = _bounded_attr(address, "addr", 80)
            kind = _bounded_attr(address, "addrtype", 16)
            if value:
                addresses.append({"address": value, "type": kind})
                if kind in {"ipv4", "ipv6"}:
                    try:
                        normalized_address = str(ipaddress.ip_address(value))
                    except ValueError:
                        out_of_scope_addresses = True
                    else:
                        if normalized_address in observed_addresses:
                            duplicate_addresses = True
                        observed_addresses.add(normalized_address)
                        if normalized_address not in requested_addresses:
                            out_of_scope_addresses = True
        names = [
            _bounded_attr(item, "name", 253)
            for item in host.findall("hostnames/hostname")[:8]
            if _bounded_attr(item, "name", 253)
        ]
        ports: list[dict[str, Any]] = []
        for port in host.findall("ports/port"):
            if port_rows >= MAX_PORT_ROWS:
                warnings.append("port_rows_truncated")
                break
            port_number = _safe_int(port.attrib.get("portid"), minimum=1, maximum=65535)
            protocol = _bounded_attr(port, "protocol", 12)
            state_node = port.find("state")
            port_state = _bounded_attr(state_node, "state", 24) if state_node is not None else "unknown"
            if port_number is None or protocol not in {"tcp", "udp", "sctp"}:
                continue
            service = port.find("service")
            service_projection = None
            if service is not None:
                confidence = _safe_int(service.attrib.get("conf"), minimum=0, maximum=10)
                service_projection = {
                    "confidence": confidence,
                    "name": _bounded_attr(service, "name", 80),
                    "product": _bounded_attr(service, "product", 120),
                    "version": _bounded_attr(service, "version", 80),
                }
            ports.append(
                {
                    "port": port_number,
                    "protocol": protocol,
                    "service": service_projection,
                    "state": port_state,
                }
            )
            port_rows += 1
            if port_state == "open":
                open_ports += 1
        hosts.append({"addresses": addresses, "hostnames": names, "ports": ports, "state": state})

    runstats = root.find("runstats/hosts")
    reported_total = (
        _safe_int(runstats.attrib.get("total"), maximum=target_snapshot.target_count)
        if runstats is not None
        else None
    )
    accounted = len(observed_addresses.intersection(requested_addresses))
    if reported_total != target_snapshot.target_count:
        warnings.append("runstats_target_total_mismatch")
    if duplicate_addresses:
        warnings.append("duplicate_target_account")
    if out_of_scope_addresses:
        warnings.append("out_of_scope_target_result")
    if timed_out:
        warnings.append("process_timeout")
    if truncated:
        warnings.append("raw_xml_truncated")
    if exit_code not in {0, None}:
        warnings.append("nmap_nonzero_exit")
    if accounted != target_snapshot.target_count:
        warnings.append("target_accounting_incomplete")
    raw_evidence_verified = any(
        item.sha256 == hashlib.sha256(payload).hexdigest()
        and item.size_bytes == len(payload)
        and item.media_type in {"application/xml", "text/xml"}
        for item in evidence
    )
    if not raw_evidence_verified:
        warnings.append("raw_xml_evidence_unverified")
    complete = bool(
        exit_code == 0
        and not timed_out
        and not truncated
        and accounted == target_snapshot.target_count
        and raw_evidence_verified
        and not duplicate_addresses
        and not out_of_scope_addresses
        and reported_total == target_snapshot.target_count
        and "port_rows_truncated" not in warnings
    )
    grade = CoverageGrade.COMPLETE if complete else CoverageGrade.PARTIAL
    parser_status = ParserStatus.COMPLETE if not truncated else ParserStatus.PARTIAL
    unique_warnings = tuple(dict.fromkeys(warnings))[:32]
    finished_element = root.find("runstats/finished")
    return ParsedActionResult.create(
        parser_id="nmap_xml_v1",
        parser_status=parser_status,
        structured={
            "finished_time": _bounded_attr(finished_element, "timestr", 80)
            if finished_element is not None
            else "",
            "hosts": hosts,
            "hosts_down_or_unknown": _safe_int(runstats.attrib.get("down")) if runstats is not None else None,
            "hosts_up": _safe_int(runstats.attrib.get("up")) if runstats is not None else None,
            "nmap_version": _bounded_attr(root, "version", 40),
            "open_ports": open_ports,
            "scan_start": _bounded_attr(root, "startstr", 80),
            "targets_requested": target_snapshot.target_count,
            "targets_scanned": accounted,
        },
        coverage=Coverage(
            grade,
            requested=target_snapshot.target_count,
            accounted=accounted,
            reasons=unique_warnings,
        ),
        warnings=unique_warnings,
        evidence=evidence,
    )


__all__ = [
    "MAX_PORTS",
    "MAX_XML_BYTES",
    "NMAP_EXECUTABLE",
    "NMAP_SPEC",
    "NmapAdapter",
    "build_nmap_execution",
    "normalize_nmap_arguments",
    "parse_nmap_xml",
    "probe_nmap_version",
]
