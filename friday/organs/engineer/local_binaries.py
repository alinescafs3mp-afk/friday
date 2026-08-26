"""Optional host binaries. Present tools are used; missing ones are named, not faked."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, BinaryIO, cast

from friday.host_control.adapters.base import ExecutionSpec, attest_execution
from friday.host_control.adapters.nmap import (
    MAX_PORTS,
    NMAP_EXECUTABLE,
    NMAP_SPEC,
    NmapAdapter,
    build_nmap_execution,
    probe_nmap_version,
)
from friday.host_control.contracts import (
    ContractError,
    EvidenceRef,
    ExecutableAttestation,
    ParserStatus,
)
from friday.host_control.policy import NetworkPolicy, NetworkTargetSnapshot, normalize_network_targets
from friday.host_control.result_projection import project_action_result

from .redaction import redact_text

INTERESTING = ("nmap", "file", "strings", "readelf", "objdump", "openssl", "dig", "host")
_NMAP_STDERR_BYTES = 4 * 1024


def inventory() -> dict[str, str | None]:
    result = {name: shutil.which(name) for name in INTERESTING if name != "nmap"}
    result["nmap"] = _fixed_nmap_path()
    return result


def _fixed_nmap_path() -> str | None:
    state = _inspect_nmap_executable()
    return state.attestation.canonical_path if state.attestation is not None else None


def remaining_timeout(deadline: float | None, ceiling: float) -> float:
    """Return one stage timeout without minting time beyond the turn deadline."""

    timeout = max(0.001, float(ceiling))
    if deadline is None:
        return timeout
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("engineer deadline expired")
    return min(timeout, remaining)


def run_argv(
    argv: Sequence[str],
    *,
    timeout_sec: float = 20.0,
    deadline: float | None = None,
    stdin: bytes | None = None,
) -> dict[str, Any]:
    if not argv or not shutil.which(str(argv[0])) and "/" not in str(argv[0]):
        return {"ok": False, "error": "binary_missing", "argv": [str(argv[0]) if argv else ""]}
    try:
        completed = subprocess.run(  # noqa: S603 - argv only, never shell
            [str(item) for item in argv],
            input=stdin,
            capture_output=True,
            timeout=remaining_timeout(deadline, timeout_sec),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "argv": [str(argv[0])]}
    except OSError as exc:
        return {"ok": False, "error": type(exc).__name__, "argv": [str(argv[0])]}
    stdout = redact_text(
        completed.stdout.decode("utf-8", errors="replace"),
        limit=12_000,
        single_line=False,
    )
    stderr = redact_text(
        completed.stderr.decode("utf-8", errors="replace"),
        limit=2_000,
        single_line=False,
    )
    return {
        "ok": completed.returncode == 0,
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "binary": str(argv[0]),
    }


def describe_bytes(data: bytes, *, deadline: float | None = None) -> dict[str, Any]:
    path = shutil.which("file")
    if not path or not data:
        return {"ok": False, "error": "file_missing"}
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=True) as handle:
        handle.write(data)
        handle.flush()
        result = run_argv([path, "-b", "--", handle.name], timeout_sec=5.0, deadline=deadline)
    result["tool"] = "file"
    return result


@dataclass(frozen=True, slots=True)
class _NmapProcessOutput:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool


@dataclass(frozen=True, slots=True)
class _NmapExecutableState:
    attestation: ExecutableAttestation | None
    error: str


def _inspect_nmap_executable() -> _NmapExecutableState:
    """Use the host-agent inventory contract instead of PATH availability."""

    from friday_host_agent.inventory import DpkgPackageResolver, ExecutableInventory

    try:
        inventory_snapshot = ExecutableInventory(
            (NMAP_SPEC,),
            package_resolver=DpkgPackageResolver(),
            version_probes={NMAP_SPEC.adapter_id: probe_nmap_version},
            allowed_owner_uids=(0,),
        ).inspect(NMAP_SPEC.adapter_id)
    except (OSError, RuntimeError, ValueError):
        return _NmapExecutableState(None, "nmap_unattested")
    if inventory_snapshot.attestation is not None and inventory_snapshot.state == "available":
        try:
            attest_execution(NMAP_SPEC, inventory_snapshot.attestation)
        except ContractError:
            return _NmapExecutableState(None, "nmap_unattested")
        return _NmapExecutableState(inventory_snapshot.attestation, "")
    error = "nmap_missing" if inventory_snapshot.state == "missing_package" else "nmap_unattested"
    return _NmapExecutableState(None, error)


def _verify_nmap_execution(
    execution: ExecutionSpec,
    attestation: ExecutableAttestation,
) -> None:
    """Reverify exact executable bytes at the Engineer execution seam."""

    from friday_host_agent.executable_attestation import (
        ExecutableAttestationError,
        verify_executable,
    )

    try:
        attest_execution(NMAP_SPEC, attestation)
        observed = verify_executable(attestation, allowed_owner_uids=(0,))
    except (ExecutableAttestationError, OSError, ValueError) as exc:
        raise ContractError("Engineer nmap executable attestation failed") from exc
    if observed != attestation or execution.executable != attestation.canonical_path:
        raise ContractError("Engineer nmap execution drifted from its executable attestation")


def _exact_ip_snapshot(host: str) -> NetworkTargetSnapshot:
    raw = str(host or "")
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise ContractError("Engineer nmap target is not an exact IP") from exc
    canonical = str(address)
    if raw != canonical:
        raise ContractError("Engineer nmap target is not a canonical exact IP")
    exact_network = str(ipaddress.ip_network(f"{canonical}/{address.max_prefixlen}", strict=True))
    policy = NetworkPolicy(
        connected_cidrs=(),
        allowed_cidrs=(exact_network,),
        allow_public=True,
        max_targets=1,
        max_target_tokens=1,
    )
    return normalize_network_targets((canonical,), policy)


def _read_pipe(descriptor: int, size: int = 64 * 1024) -> bytes:
    return os.read(descriptor, size)


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _capture_nmap_execution(
    execution: ExecutionSpec,
    *,
    deadline: float | None,
) -> _NmapProcessOutput:
    """Run one shared nmap spec while bounding combined stdout/stderr in memory."""

    timeout = remaining_timeout(deadline, float(execution.timeout_sec))
    process: subprocess.Popen[bytes] | None = None
    stdout = bytearray()
    stderr = bytearray()
    truncated = False
    timed_out = False
    try:
        process = subprocess.Popen(  # noqa: S603 - shared adapter owns every argv token
            execution.argv,
            executable=execution.executable,
            env=dict(execution.environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            close_fds=True,
        )
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, stdout)
            selector.register(process.stderr, selectors.EVENT_READ, stderr)
            expires_at = time.monotonic() + timeout
            while selector.get_map():
                if not timed_out and time.monotonic() >= expires_at:
                    timed_out = True
                    _terminate_process(process)
                for key, _mask in selector.select(0.05):
                    stream = cast(BinaryIO, key.fileobj)
                    chunk = _read_pipe(stream.fileno())
                    if not chunk:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    destination: bytearray = key.data
                    remaining = max(0, execution.max_output_bytes - len(stdout) - len(stderr))
                    destination.extend(chunk[:remaining])
                    truncated = truncated or len(chunk) > remaining
        return_code = process.wait(timeout=2)
        return _NmapProcessOutput(
            exit_code=return_code,
            stdout=bytes(stdout),
            stderr=bytes(stderr),
            timed_out=timed_out,
            truncated=truncated,
        )
    except BaseException:
        if process is not None:
            _terminate_process(process)
        raise


def _nmap_evidence(payload: bytes) -> tuple[EvidenceRef, ...]:
    if not payload:
        return ()
    digest = hashlib.sha256(payload).hexdigest()
    return (
        EvidenceRef(
            evidence_id=f"evidence_{digest[:32]}",
            sha256=digest,
            size_bytes=len(payload),
            media_type="application/xml",
        ),
    )


def _nmap_error(output: _NmapProcessOutput, parser_status: ParserStatus) -> str:
    if output.timed_out:
        return "timeout"
    if output.truncated:
        return "output_truncated"
    if output.exit_code != 0:
        return "nmap_failed"
    if parser_status is ParserStatus.UNAVAILABLE:
        return "xml_unavailable"
    return ""


def _nmap_snapshot_scan(
    target_snapshot: NetworkTargetSnapshot,
    action_id: str,
    *,
    ports: Sequence[int] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Execute the shared reviewed adapter against one code-owned snapshot."""

    adapter = NmapAdapter()
    try:
        supplied: dict[str, Any] = {"target_snapshot_digest": target_snapshot.digest}
        if action_id == "selected_ports":
            supplied["ports"] = list(ports or ())
        normalized = adapter.normalize_arguments(
            action_id,
            supplied,
            target_snapshot=target_snapshot,
        )
        raw_normalized_ports = normalized.get("ports")
        normalized_ports = raw_normalized_ports if isinstance(raw_normalized_ports, list) else None
    except ContractError:
        return {"ok": False, "error": "invalid_arguments", "tool": "nmap"}
    executable_state = _inspect_nmap_executable()
    attestation = executable_state.attestation
    if attestation is None:
        return {"ok": False, "error": executable_state.error, "tool": "nmap"}
    try:
        execution = build_nmap_execution(
            action_id,
            target_snapshot=target_snapshot,
            ports=normalized_ports,
            executable=attestation.canonical_path,
        )
        _verify_nmap_execution(execution, attestation)
    except ContractError:
        return {"ok": False, "error": "nmap_unattested", "tool": "nmap"}
    try:
        output = _capture_nmap_execution(execution, deadline=deadline)
    except TimeoutError:
        raise
    except OSError as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "binary": NMAP_EXECUTABLE,
            "tool": "nmap",
        }
    evidence = _nmap_evidence(output.stdout)
    parsed = adapter.parse_xml(
        output.stdout,
        target_snapshot=target_snapshot,
        exit_code=output.exit_code,
        timed_out=output.timed_out,
        truncated=output.truncated,
        evidence=evidence,
    )
    error = _nmap_error(output, parsed.parser_status)
    result: dict[str, Any] = {
        "binary": NMAP_EXECUTABLE,
        "coverage": parsed.coverage.to_payload(),
        "evidence": [item.to_payload() for item in evidence],
        "evidence_retention": "digest_only",
        "executable_attestation": {
            "architecture": attestation.architecture,
            "binary_sha256": attestation.sha256,
            "digest": attestation.digest,
            "observed_version": attestation.observed_version,
            "package_name": attestation.package_name,
            "package_version": attestation.package_version,
        },
        "exit_code": output.exit_code,
        "ok": not error,
        "parser_status": parsed.parser_status.value,
        "report": project_action_result(parsed),
        "stderr": redact_text(
            output.stderr.decode("utf-8", errors="replace"),
            limit=_NMAP_STDERR_BYTES,
            single_line=False,
        ),
        "target_snapshot_digest": target_snapshot.digest,
        "tool": "nmap",
        "used": True,
    }
    if error:
        result["error"] = error
    return result


def nmap_connect_scan(
    host: str,
    ports: Sequence[int],
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run shared ``selected_ports`` against one already-authorized pinned IP."""

    if not ports:
        return {"ok": False, "error": "no_ports", "tool": "nmap"}
    if len(ports) > MAX_PORTS:
        return {"ok": False, "error": "invalid_ports", "tool": "nmap"}
    try:
        target_snapshot = _exact_ip_snapshot(host)
    except ContractError:
        return {"ok": False, "error": "invalid_target", "tool": "nmap"}
    result = _nmap_snapshot_scan(
        target_snapshot,
        "selected_ports",
        ports=ports,
        deadline=deadline,
    )
    if result.get("error") == "invalid_arguments":
        result["error"] = "invalid_ports"
    return result


def nmap_network_scan(
    target_snapshot: NetworkTargetSnapshot,
    *,
    profile: str = "discover",
    deadline: float | None = None,
) -> dict[str, Any]:
    """Run one bounded subnet profile selected by code, never raw nmap flags."""

    if profile not in {"discover", "services"}:
        return {"ok": False, "error": "invalid_profile", "tool": "nmap"}
    if target_snapshot.approval_required or not 1 <= target_snapshot.target_count <= 256:
        return {"ok": False, "error": "invalid_target", "tool": "nmap"}
    return _nmap_snapshot_scan(target_snapshot, profile, deadline=deadline)


def dig_records(host: str, *, deadline: float | None = None) -> dict[str, Any]:
    path = shutil.which("dig") or shutil.which("host")
    if not path:
        return {"ok": False, "error": "resolver_missing"}
    if path.endswith("dig"):
        argv = [
            path,
            "+time=2",
            "+tries=1",
            "+noall",
            "+answer",
            str(host),
            "A",
            str(host),
            "AAAA",
            str(host),
            "MX",
            str(host),
            "TXT",
            str(host),
            "NS",
        ]
        # dig takes one qname/type; do sequential small queries.
        blobs: list[str] = []
        attempted = False
        for rtype in ("A", "AAAA", "MX", "NS", "TXT"):
            try:
                item = run_argv(
                    [path, "+time=2", "+tries=1", "+short", str(host), rtype],
                    timeout_sec=4.0,
                    deadline=deadline,
                )
            except TimeoutError:
                return {
                    "ok": False,
                    "error": "deadline",
                    "tool": "dig",
                    "records": blobs,
                    "attempted": attempted,
                }
            attempted = True
            if item.get("stdout"):
                records = sorted(line.strip() for line in str(item["stdout"]).splitlines() if line.strip())[
                    :16
                ]
                if records:
                    blobs.append(f"{rtype}: {' | '.join(records)}")
        return {
            "ok": True,
            "tool": "dig",
            "records": blobs,
            "argv": argv[:1],
            "attempted": attempted,
        }
    item = run_argv([path, str(host)], timeout_sec=4.0, deadline=deadline)
    item["tool"] = "host"
    item["attempted"] = True
    return item
