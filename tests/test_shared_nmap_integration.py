from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from friday.host_control.adapters.nmap import (
    NMAP_EXECUTABLE,
    NMAP_SPEC,
    NmapAdapter,
    build_nmap_execution,
    probe_nmap_version,
)
from friday.host_control.contracts import ExecutableAttestation
from friday.host_control.plans import create_action_plan
from friday.host_control.policy import NetworkPolicy, normalize_network_targets
from friday.organs.engineer import hosts, local_binaries


def _attestation() -> ExecutableAttestation:
    return ExecutableAttestation(
        schema_version=1,
        canonical_path=NMAP_EXECUTABLE,
        device=8,
        inode=42,
        mode=0o755,
        owner_uid=0,
        owner_gid=0,
        size_bytes=1234,
        mtime_ns=100,
        sha256="a" * 64,
        package_name="nmap",
        package_version="7.94+dfsg1-1build1",
        architecture="amd64",
        observed_version="Nmap version 7.94",
        adapter_id=NMAP_SPEC.adapter_id,
        adapter_schema_version=NMAP_SPEC.adapter_schema_version,
        implementation_version=NMAP_SPEC.implementation_version,
    )


def _xml() -> bytes:
    return (
        b'<?xml version="1.0"?><nmaprun version="7.94" startstr="now">'
        b'<host><status state="up"/><address addr="192.168.1.7" addrtype="ipv4"/>'
        b'<ports><port protocol="tcp" portid="443"><state state="open"/>'
        b'<service name="https" product="example" version="1" conf="7"/>'
        b'</port></ports></host><runstats><finished timestr="later"/>'
        b'<hosts up="1" down="0" total="1"/></runstats></nmaprun>'
    )


def _host_execution(attestation: ExecutableAttestation):  # noqa: ANN202
    adapter = NmapAdapter()
    snapshot = normalize_network_targets(
        ("192.168.1.7",),
        NetworkPolicy(connected_cidrs=("192.168.1.0/24",), max_targets=64),
    )
    normalized = adapter.normalize_arguments(
        "selected_ports",
        {"ports": [443, 22], "target_snapshot_digest": snapshot.digest},
        target_snapshot=snapshot,
    )
    plan = create_action_plan(
        plan_id="plan_0123456789abcdef",
        actor_user_id="owner",
        actor_own_id="owner",
        conversation_id="conv_0123456789abcdef",
        source_message_id="msg_0123456789abcdef",
        host_agent_id="agent_0123456789abcdef",
        idempotency_key="idem_0123456789abcdef",
        adapter=NMAP_SPEC,
        action=NMAP_SPEC.action("selected_ports"),
        normalized_arguments=normalized,
        executable_attestation=attestation,
        target_snapshot=snapshot.to_payload(),
        now=100,
    )
    return adapter.build_execution(plan, attestation)


def test_engineer_and_host_share_exact_nmap_argv_parser_and_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = _attestation()
    observed: dict[str, Any] = {}

    monkeypatch.setattr(
        local_binaries,
        "_inspect_nmap_executable",
        lambda: local_binaries._NmapExecutableState(attestation, ""),
    )

    def verify(execution, supplied_attestation) -> None:  # noqa: ANN001
        observed["verified_execution"] = execution
        observed["verified_attestation"] = supplied_attestation

    def capture(execution, *, deadline):  # noqa: ANN001, ANN202
        observed["captured_execution"] = execution
        observed["deadline"] = deadline
        return local_binaries._NmapProcessOutput(
            exit_code=0,
            stdout=_xml(),
            stderr=b"",
            timed_out=False,
            truncated=False,
        )

    monkeypatch.setattr(local_binaries, "_verify_nmap_execution", verify)
    monkeypatch.setattr(local_binaries, "_capture_nmap_execution", capture)

    result = local_binaries.nmap_connect_scan("192.168.1.7", [443, 22], deadline=123.0)
    host_execution = _host_execution(attestation)
    engineer_execution = observed["captured_execution"]

    assert engineer_execution == observed["verified_execution"]
    assert observed["verified_attestation"] == attestation
    assert engineer_execution.argv == host_execution.argv
    assert engineer_execution.argv[0] == NMAP_EXECUTABLE
    assert engineer_execution.argv[-1] == "192.168.1.7"
    assert engineer_execution.argv[engineer_execution.argv.index("-p") + 1] == "22,443"
    assert engineer_execution.argv[engineer_execution.argv.index("-oX") + 1] == "-"
    assert {"-Pn", "-sT", "-sV", "--version-light"}.issubset(engineer_execution.argv)

    assert result["ok"] is True
    assert result["parser_status"] == "complete"
    assert result["coverage"]["grade"] == "complete"
    assert result["report"]["label"] == "UNTRUSTED_HOST_APPLICATION_EVIDENCE"
    assert result["report"]["result"]["targets_scanned"] == 1
    assert result["report"]["result"]["open_ports"] == 1
    assert result["executable_attestation"]["digest"] == attestation.digest
    assert result["executable_attestation"]["observed_version"] == "Nmap version 7.94"
    assert result["evidence"] == [
        {
            "evidence_id": f"evidence_{hashlib.sha256(_xml()).hexdigest()[:32]}",
            "media_type": "application/xml",
            "sha256": hashlib.sha256(_xml()).hexdigest(),
            "size_bytes": len(_xml()),
        }
    ]
    assert "stdout" not in result
    assert _xml().decode() not in str(result)

    markdown = hosts.host_markdown(
        {
            "host": "192.168.1.7",
            "addresses": ["192.168.1.7"],
            "open_ports": [443],
            "active_probes": ["nmap_service_detection"],
            "nmap": result,
        }
    )
    assert "nmap structured result: targets 1/1, open ports 1" in markdown
    assert "UNTRUSTED_HOST_APPLICATION_EVIDENCE" in markdown
    assert hashlib.sha256(_xml()).hexdigest() in markdown
    assert _xml().decode() not in markdown


def test_engineer_rejects_invalid_inputs_and_unattested_nmap_before_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection_calls = 0

    def inspect() -> local_binaries._NmapExecutableState:
        nonlocal inspection_calls
        inspection_calls += 1
        return local_binaries._NmapExecutableState(None, "nmap_unattested")

    monkeypatch.setattr(local_binaries, "_inspect_nmap_executable", inspect)
    monkeypatch.setattr(
        local_binaries,
        "_capture_nmap_execution",
        lambda *_args, **_kwargs: pytest.fail("rejected nmap request reached process capture"),
    )

    assert local_binaries.nmap_connect_scan("example.test", [443])["error"] == "invalid_target"
    assert local_binaries.nmap_connect_scan("192.168.1.7", [True])["error"] == "invalid_ports"
    assert local_binaries.nmap_connect_scan("192.168.1.7", [443, 443])["error"] == "invalid_ports"
    assert local_binaries.nmap_connect_scan("192.168.1.7", list(range(1, 66)))["error"] == ("invalid_ports")
    assert inspection_calls == 0

    result = local_binaries.nmap_connect_scan("192.168.1.7", [443])
    assert result == {"error": "nmap_unattested", "ok": False, "tool": "nmap"}
    assert inspection_calls == 1


def test_engineer_reverifies_with_the_shared_host_executable_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday_host_agent import executable_attestation as executable_attestation_module

    attestation = _attestation()
    snapshot = local_binaries._exact_ip_snapshot("192.168.1.7")
    execution = build_nmap_execution(
        "selected_ports",
        target_snapshot=snapshot,
        ports=[443],
    )
    verified: list[tuple[ExecutableAttestation, tuple[int, ...]]] = []

    def verify(
        expected: ExecutableAttestation,
        *,
        allowed_owner_uids: tuple[int, ...],
    ) -> ExecutableAttestation:
        verified.append((expected, allowed_owner_uids))
        return expected

    monkeypatch.setattr(executable_attestation_module, "verify_executable", verify)
    local_binaries._verify_nmap_execution(execution, attestation)

    assert verified == [(attestation, (0,))]


def test_shared_nmap_version_probe_is_fixed_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, Any] = {}

    def run(argv, **kwargs):  # noqa: ANN001, ANN202
        observed["argv"] = argv
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=b"Nmap version 7.94\nextra\n", stderr=b"")

    monkeypatch.setattr("friday.host_control.adapters.nmap.subprocess.run", run)

    assert probe_nmap_version() == "Nmap version 7.94"
    assert observed["argv"] == (NMAP_EXECUTABLE, "--version")
    assert observed["executable"] == NMAP_EXECUTABLE
    assert observed["timeout"] == 3
    assert observed["env"] == {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def test_engineer_timeout_keeps_structured_partial_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    attestation = _attestation()
    monkeypatch.setattr(
        local_binaries,
        "_inspect_nmap_executable",
        lambda: local_binaries._NmapExecutableState(attestation, ""),
    )
    monkeypatch.setattr(local_binaries, "_verify_nmap_execution", lambda *_args: None)
    monkeypatch.setattr(
        local_binaries,
        "_capture_nmap_execution",
        lambda *_args, **_kwargs: local_binaries._NmapProcessOutput(
            exit_code=None,
            stdout=_xml(),
            stderr=b"deadline reached",
            timed_out=True,
            truncated=False,
        ),
    )

    result = local_binaries.nmap_connect_scan("192.168.1.7", [443])

    assert result["ok"] is False
    assert result["error"] == "timeout"
    assert result["coverage"]["grade"] == "partial"
    assert result["report"]["result"]["targets_scanned"] == 1
    assert result["evidence_retention"] == "digest_only"
    assert "stdout" not in result
