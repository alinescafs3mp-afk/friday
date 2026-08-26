from __future__ import annotations

import hashlib

import pytest

from friday.host_control.adapters.jq import JQ_SPEC, JqAdapter
from friday.host_control.adapters.nmap import NMAP_SPEC, SERVICE_PORTS, NmapAdapter, parse_nmap_xml
from friday.host_control.capability_catalog import BUILTIN_CATALOG
from friday.host_control.contracts import (
    AdapterState,
    ContractError,
    CoverageGrade,
    EvidenceRef,
    ExecutableAttestation,
    ParserStatus,
)
from friday.host_control.plans import WorkspaceGrant, create_action_plan
from friday.host_control.policy import NetworkPolicy, normalize_network_targets


def executable(adapter: str, name: str) -> ExecutableAttestation:
    return ExecutableAttestation(
        schema_version=1,
        canonical_path=f"/usr/bin/{name}",
        device=8,
        inode=42,
        mode=0o755,
        owner_uid=0,
        owner_gid=0,
        size_bytes=1234,
        mtime_ns=100,
        sha256="a" * 64,
        package_name=name,
        package_version="1.0-1ubuntu1",
        architecture="amd64",
        observed_version=f"{name} 1.0",
        adapter_id=adapter,
        adapter_schema_version=1,
        implementation_version=1,
    )


def snapshot(targets: list[str]):  # noqa: ANN201
    return normalize_network_targets(
        targets,
        NetworkPolicy(connected_cidrs=("192.168.1.0/24",), max_targets=256),
    )


def nmap_plan(action: str, target_values: list[str], ports: list[int] | None = None):  # noqa: ANN201
    adapter = NmapAdapter()
    target_snapshot = snapshot(target_values)
    supplied = {"target_snapshot_digest": target_snapshot.digest}
    if ports is not None:
        supplied["ports"] = ports
    normalized = adapter.normalize_arguments(action, supplied, target_snapshot=target_snapshot)
    plan = create_action_plan(
        plan_id="plan_0123456789abcdef",
        actor_user_id="owner",
        actor_own_id="owner",
        conversation_id="conv_0123456789abcdef",
        source_message_id="msg_0123456789abcdef",
        host_agent_id="agent_0123456789abcdef",
        idempotency_key="idem_0123456789abcdef",
        adapter=NMAP_SPEC,
        action=NMAP_SPEC.action(action),
        normalized_arguments=normalized,
        executable_attestation=executable("network.nmap", "nmap"),
        target_snapshot=target_snapshot.to_payload(),
        now=100,
    )
    return adapter, target_snapshot, plan


def xml_evidence(payload: bytes) -> tuple[EvidenceRef, ...]:
    return (
        EvidenceRef(
            evidence_id="evidence_0123456789abcdef",
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            media_type="application/xml",
        ),
    )


def nmap_xml(addresses: list[str], *, total: int) -> bytes:
    hosts = "".join(
        f'<host><status state="up"/><address addr="{address}" addrtype="ipv4"/>'
        '<ports><port protocol="tcp" portid="443"><state state="open"/>'
        '<service name="https" product="example" version="1" conf="7"/></port></ports></host>'
        for address in addresses
    )
    return (
        f'<?xml version="1.0"?><nmaprun version="7.94" startstr="now">{hosts}'
        f'<runstats><finished timestr="later"/><hosts up="{len(addresses)}" down="0" '
        f'total="{total}"/></runstats></nmaprun>'
    ).encode()


def test_nmap_services_argv_is_absolute_fixed_and_rate_bounded() -> None:
    adapter, _targets, plan = nmap_plan("services", ["192.168.1.7"])
    execution = adapter.build_execution(plan, executable("network.nmap", "nmap"))
    assert execution.argv[0] == "/usr/bin/nmap"
    assert "--max-rate" in execution.argv and execution.argv[execution.argv.index("--max-rate") + 1] == "100"
    assert "--max-hostgroup" in execution.argv
    assert execution.argv[execution.argv.index("-p") + 1] == ",".join(str(item) for item in SERVICE_PORTS)
    assert len(SERVICE_PORTS) <= 64
    assert all("shell" not in item for item in execution.argv)


def test_nmap_selected_ports_are_closed_and_raw_flags_never_enter_argv() -> None:
    adapter, _targets, plan = nmap_plan("selected_ports", ["192.168.1.7"], [443, 22])
    execution = adapter.build_execution(plan, executable("network.nmap", "nmap"))
    assert execution.argv[execution.argv.index("-p") + 1] == "22,443"
    with pytest.raises(ContractError):
        nmap_plan("selected_ports", ["192.168.1.7"], [443] * 65)
    with pytest.raises(ContractError):
        snapshot(["-sS"])


def test_nmap_xml_complete_requires_closed_accounting_and_verified_raw_evidence() -> None:
    _adapter, target_snapshot, _plan = nmap_plan("discover", ["192.168.1.7"])
    payload = nmap_xml(["192.168.1.7"], total=1)
    result = parse_nmap_xml(
        payload,
        target_snapshot=target_snapshot,
        exit_code=0,
        evidence=xml_evidence(payload),
    )
    assert result.parser_status is ParserStatus.COMPLETE
    assert result.coverage.grade is CoverageGrade.COMPLETE
    assert result.structured["open_ports"] == 1


def test_nmap_forged_total_out_of_scope_duplicate_and_missing_evidence_are_partial() -> None:
    _adapter, two_targets, _plan = nmap_plan("discover", ["192.168.1.7", "192.168.1.8"])
    forged = nmap_xml(["192.168.1.7"], total=2)
    forged_result = parse_nmap_xml(
        forged,
        target_snapshot=two_targets,
        exit_code=0,
        evidence=xml_evidence(forged),
    )
    assert forged_result.coverage.grade is CoverageGrade.PARTIAL
    assert "target_accounting_incomplete" in forged_result.warnings

    _adapter, one_target, _plan = nmap_plan("discover", ["192.168.1.7"])
    outside = nmap_xml(["192.168.1.9"], total=1)
    outside_result = parse_nmap_xml(
        outside,
        target_snapshot=one_target,
        exit_code=0,
        evidence=xml_evidence(outside),
    )
    assert "out_of_scope_target_result" in outside_result.warnings
    duplicate = nmap_xml(["192.168.1.7", "192.168.1.7"], total=1)
    duplicate_result = parse_nmap_xml(
        duplicate,
        target_snapshot=one_target,
        exit_code=0,
        evidence=xml_evidence(duplicate),
    )
    assert "duplicate_target_account" in duplicate_result.warnings
    no_evidence = parse_nmap_xml(
        payload := nmap_xml(["192.168.1.7"], total=1), target_snapshot=one_target, exit_code=0
    )
    assert payload
    assert "raw_xml_evidence_unverified" in no_evidence.warnings
    assert no_evidence.coverage.grade is CoverageGrade.PARTIAL


def test_nmap_parser_rejects_entities_and_malformed_xml_without_expansion() -> None:
    _adapter, target_snapshot, _plan = nmap_plan("discover", ["192.168.1.7"])
    entity = b'<!DOCTYPE x [<!ENTITY boom "secret">]><nmaprun>&boom;</nmaprun>'
    result = parse_nmap_xml(entity, target_snapshot=target_snapshot, exit_code=0)
    assert result.parser_status is ParserStatus.UNAVAILABLE
    assert result.warnings == ("xml_entities_forbidden",)
    malformed = parse_nmap_xml(b"<nmaprun>", target_snapshot=target_snapshot, exit_code=0)
    assert malformed.parser_status is ParserStatus.UNAVAILABLE


def test_jq_adapter_accepts_only_grants_and_constructs_its_own_program() -> None:
    adapter = JqAdapter()
    grant = WorkspaceGrant(
        "grant_0123456789abcdef",
        "owner",
        "read",
        "input/document.json",
        "b" * 64,
    )
    normalized = adapter.normalize_arguments(
        "extract_fields",
        {"input_grant": grant.grant_id, "fields": ["person.name", "meta.id"], "compact": True},
    )
    plan = create_action_plan(
        plan_id="plan_0123456789abcdef",
        actor_user_id="owner",
        actor_own_id="owner",
        conversation_id="conv_0123456789abcdef",
        source_message_id="msg_0123456789abcdef",
        host_agent_id="agent_0123456789abcdef",
        idempotency_key="idem_0123456789abcdef",
        adapter=JQ_SPEC,
        action=JQ_SPEC.action("extract_fields"),
        normalized_arguments=normalized,
        executable_attestation=executable("data.jq", "jq"),
        workspace_grants=(grant,),
        now=100,
    )
    execution = adapter.build_execution(plan, executable("data.jq", "jq"))
    assert execution.argv[-1] == "document.json"
    assert execution.working_directory_ref == "job_input"
    assert "getpath" in execution.argv[-2]
    with pytest.raises(ContractError):
        adapter.normalize_arguments(
            "extract_fields",
            {"input_grant": grant.grant_id, "fields": ["person.name | env"]},
        )


def test_catalog_contains_general_network_and_local_data_adapters() -> None:
    entries = BUILTIN_CATALOG.entries(
        adapter_states={"network.nmap": AdapterState.MISSING_PACKAGE, "data.jq": AdapterState.UNATTESTED},
        candidate_refs={"network.nmap": "candidate_0123456789abcdef"},
    )
    assert {item.adapter_id for item in entries} == {"network.nmap", "data.jq"}
    found = BUILTIN_CATALOG.search("scan network", entries=entries, category="network")
    assert found[0].capability_id == "network.nmap.scan"
    assert found[0].to_public_payload().get("package_candidate_ref") == "candidate_0123456789abcdef"
