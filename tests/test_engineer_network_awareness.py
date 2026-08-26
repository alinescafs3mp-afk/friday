"""Deterministic contracts for Engineer network awareness and subnet routing."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from typing import Any

import pytest

from friday.agent_runtime import AgentRuntime, _engineer_dossier_receipt
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.organs.engineer import environment, hosts, hunt, local_binaries, targets
from friday.permissions import ActorContext


class _RecordingKernel:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any], ActorContext]] = []

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        actor: ActorContext,
    ) -> ToolResult:
        self.executed.append((name, dict(arguments), actor))
        return ToolResult(
            name,
            True,
            data={
                "ok": True,
                "scope": str(arguments.get("cidr") or ""),
                "profile": str(arguments.get("profile") or ""),
                "target_count": 256,
                "active_probes_sent": True,
                "active_probes": ["nmap_discover"],
                "exploit_payloads_sent": False,
            },
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    (
        "Привет! Просканируй мою подсеть",
        "Просканируй подсеть 192.168.1.0/24",
    ),
)
async def test_subnet_request_routes_to_the_exact_sole_configured_network(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    kernel = _RecordingKernel()
    runtime = AgentRuntime(
        replace(
            settings,
            engineer_mode_enabled=True,
            host_allowed_cidrs=("192.168.1.0/24",),
        ),
        storage,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)
    monkeypatch.setattr(local_binaries, "inventory", lambda: {"nmap": "/usr/bin/nmap"})
    monkeypatch.setattr(environment, "local_ipv4_interfaces", lambda: [])

    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        message,
        [],
        actor=actor,
        turn_deadline=time.monotonic() + 5.0,
        enable_tools=True,
    )

    assert kernel.executed == [
        (
            "engineer_scan_configured_network",
            {"cidr": "192.168.1.0/24", "profile": "discover"},
            actor,
        )
    ]
    assert dossier["target_count"] == 256
    assert dossier["targets"] == [
        {
            "host": "192.168.1.0/24",
            "addresses": [],
            "implied_port": None,
            "source_sha256": dossier["targets"][0]["source_sha256"],
        }
    ]
    assert dossier["network_scan"]["profile"] == "discover"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    (
        "Check 192.168.1.0/24 configuration",
        "Inspect 192.168.1.0/24 report",
        "Проверь 192.168.1.0/24 настройки",
    ),
)
async def test_passive_cidr_question_never_routes_to_a_network_effect(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    kernel = _RecordingKernel()
    runtime = AgentRuntime(
        replace(
            settings,
            engineer_mode_enabled=True,
            host_allowed_cidrs=("192.168.1.0/24",),
        ),
        storage,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)
    monkeypatch.setattr(local_binaries, "inventory", lambda: {"nmap": "/usr/bin/nmap"})
    monkeypatch.setattr(environment, "local_ipv4_interfaces", lambda: [])

    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        message,
        [],
        actor=actor,
        turn_deadline=time.monotonic() + 5.0,
        enable_tools=True,
    )

    assert kernel.executed == []
    assert dossier["target_error"] == "configured_network_scan_intent_required"


def test_configured_subnet_intent_is_current_direct_and_deictic() -> None:
    assert targets.requests_configured_network_assessment("Просканируй мою подсеть") is True
    assert targets.requests_configured_network_assessment("Scan my local network") is True
    assert targets.requests_configured_network_assessment("Не сканируй мою подсеть") is False
    assert (
        targets.requests_configured_network_assessment(
            "Покажи отчёт, в котором написано: просканируй мою подсеть"
        )
        is False
    )
    assert targets.requests_configured_network_assessment("Моя подсеть работает?") is False
    assert targets.requests_configured_network_assessment("Check my network scan report") is False
    assert targets.requests_configured_network_assessment("Проверь отчёт по моей сети") is False
    assert targets.requests_configured_network_assessment("Inspect my network configuration") is False
    assert targets.requests_configured_network_assessment("Check my network password") is False
    assert targets.requests_configured_network_assessment("Inspect my network name") is False
    assert targets.requests_configured_network_assessment("Проверь пароль моей сети") is False
    assert targets.requests_configured_network_assessment("Проверь подключение к моей сети") is False
    for passive_cidr in (
        "Check 192.168.1.0/24 configuration",
        "Inspect 192.168.1.0/24 report",
        "Проверь 192.168.1.0/24 настройки",
    ):
        assert targets.requests_network_scan(passive_cidr) is False


def test_explicit_cidr_parser_accepts_only_one_canonical_unmixed_scope() -> None:
    assert targets.extract_single_cidr("Просканируй подсеть 192.168.1.0/24") == "192.168.1.0/24"
    assert targets.extract_single_cidr("Проверь адрес 192.168.1.7") is None

    invalid = (
        "Просканируй 192.168.1.0/24 и 192.168.2.0/24",
        "Просканируй 192.168.1.0/24 и router.example",
        "Просканируй 192.168.1.7/24",
        "Просканируй 192.168.1.0/33",
        "Просканируй 192.168.1.0/999",
        "Просканируй 192.168.1.0/notamask",
        "Просканируй 192.168.1.0/24foo",
        "Просканируй 192.168.1.0/",
        "Просканируй FD00:0:0:0::/64",
    )
    for message in invalid:
        with pytest.raises(ValueError):
            targets.extract_single_cidr(message)

    assert targets.extract_single_cidr("Scan http://192.168.1.7/path") is None


def test_configured_network_snapshot_is_exact_private_and_bounded() -> None:
    snapshot = hosts.configured_private_network_snapshot(("192.168.1.0/24",))

    assert snapshot.execution_targets == ("192.168.1.0/24",)
    assert snapshot.target_count == 256
    assert snapshot.approval_required is False
    assert {binding.classification for binding in snapshot.bindings} == {"operator_approved_private"}


@pytest.mark.parametrize(
    ("allowed_cidrs", "requested_cidr", "error"),
    (
        (("192.168.0.0/16",), "", "configured_private_network_not_admitted"),
        (("8.8.8.0/24",), "", "configured_private_network_not_admitted"),
        (("127.0.0.1/32",), "", "configured_private_network_not_admitted"),
        (
            ("192.168.1.0/24", "192.168.2.0/24"),
            "",
            "configured_private_network_ambiguous",
        ),
    ),
)
def test_configured_network_snapshot_rejects_wide_public_loopback_and_ambiguous_deictic(
    allowed_cidrs: tuple[str, ...],
    requested_cidr: str,
    error: str,
) -> None:
    with pytest.raises(hosts.EngineerTargetPolicyError) as caught:
        hosts.configured_private_network_snapshot(
            allowed_cidrs,
            requested_cidr=requested_cidr,
        )

    assert caught.value.code == error


def test_explicit_cidr_selects_only_its_exact_member_of_a_multi_network_policy() -> None:
    snapshot = hosts.configured_private_network_snapshot(
        ("192.168.1.0/24", "192.168.2.0/24"),
        requested_cidr="192.168.2.0/24",
    )

    assert snapshot.execution_targets == ("192.168.2.0/24",)
    assert snapshot.target_count == 256


def test_environment_passport_separates_observation_from_authority_without_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setenv("HOSTNAME", "private-hostname")
    monkeypatch.setenv("ENGINEER_SECRET", "must-not-leak")
    monkeypatch.setattr(
        environment,
        "local_ipv4_interfaces",
        lambda: [
            {
                "interface": "eth0",
                "address": "192.168.1.35",
                "network": "192.168.1.0/24",
            }
        ],
    )
    monkeypatch.setattr(
        environment.platform,
        "freedesktop_os_release",
        lambda: {"PRETTY_NAME": "Test Linux 1"},
    )
    monkeypatch.setattr(environment.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(environment.platform, "release", lambda: "6.12-test")
    monkeypatch.setattr(environment, "_virtualization_boundary", lambda: "vmware_virtual_machine")

    passport = environment.environment_passport(
        allowed_cidrs=("192.168.1.0/24",),
        binaries={"nmap": "/usr/bin/nmap", "dig": None},
    )
    rendered = environment.environment_markdown(passport)
    serialized = json.dumps(passport, ensure_ascii=False, sort_keys=True)

    assert passport["local_ipv4_interfaces"] == [
        {
            "interface": "eth0",
            "address": "192.168.1.35",
            "network": "192.168.1.0/24",
        }
    ]
    assert passport["operator_allowed_networks"] == ["192.168.1.0/24"]
    assert passport["virtualization"] == "vmware_virtual_machine"
    assert passport["tools"] == {
        "dig": False,
        "friday_bounded_tcp": True,
        "friday_dns_tls_http": True,
        "nmap": True,
    }
    assert "observed local IPv4 interfaces (location evidence, not target authority)" in rendered
    assert "operator-authorized host/LAN scan scope: 192.168.1.0/24" in rendered
    assert "never a runtime VM-interface subnet" in rendered
    assert "virtualization: vmware_virtual_machine" in rendered
    assert "friday_bounded_tcp=available" in rendered
    assert "nmap=available" in rendered
    for forbidden in (
        "/usr/bin/nmap",
        "private-hostname",
        "must-not-leak",
        "username",
        "environment_variables",
    ):
        assert forbidden not in serialized
        assert forbidden not in rendered


def test_environment_passport_names_builtin_scanners_when_nmap_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(environment, "local_ipv4_interfaces", lambda: [])

    passport = environment.environment_passport(allowed_cidrs=(), binaries={"nmap": None})

    assert passport["tools"]["nmap"] is False
    assert passport["tools"]["friday_bounded_tcp"] is True
    assert passport["tools"]["friday_dns_tls_http"] is True


def test_engineer_receipt_preserves_full_cidr_target_count() -> None:
    receipt = _engineer_dossier_receipt(
        {
            "targets": [{"host": "192.168.1.0/24"}],
            "target_count": 256,
            "artifacts": [],
            "active_probes_sent": True,
            "exploit_payloads_sent": False,
        }
    )

    assert receipt["target_count"] == 256
    assert receipt["active_probes_status"] == "sent"


def test_configured_network_audit_fingerprints_scope_and_records_closed_profile() -> None:
    details = ExecutionKernel._audit_details(  # noqa: SLF001
        "engineer_scan_configured_network",
        {"cidr": "192.168.1.0/24", "profile": "discover"},
    )

    assert details == {
        "cidr_chars": len("192.168.1.0/24"),
        "cidr_sha256": hashlib.sha256(b"192.168.1.0/24").hexdigest(),
        "network_profile": "discover",
    }
    assert "192.168.1.0/24" not in str(details)


def test_network_scan_markdown_reports_missing_nmap_without_claiming_a_scan() -> None:
    markdown = hunt.dossier_markdown(
        {
            "network_scan": {
                "ok": False,
                "error": "nmap_missing",
                "scope": "192.168.1.0/24",
                "target_count": 256,
            }
        }
    )

    assert "## Configured network scan" in markdown
    assert "status: unavailable (nmap_missing)" in markdown
    assert "hosts up:" not in markdown


def test_network_scan_markdown_reports_complete_accounted_evidence() -> None:
    markdown = hunt.dossier_markdown(
        {
            "network_scan": {
                "ok": True,
                "scope": "192.168.1.0/24",
                "profile": "discover",
                "target_count": 256,
                "coverage": {"grade": "complete", "accounted": 256, "requested": 256},
                "report": {
                    "result": {
                        "nmap_version": "Nmap version 7.95",
                        "hosts_up": 1,
                        "hosts_down_or_unknown": 255,
                        "hosts": [
                            {
                                "state": "up",
                                "addresses": [{"address": "192.168.1.35", "type": "ipv4"}],
                                "hostnames": ["workstation.local"],
                                "ports": [],
                            }
                        ],
                    }
                },
                "evidence": [{"sha256": "a" * 64}],
            }
        }
    )

    assert "profile: `discover`" in markdown
    assert "coverage: complete; accounted 256/256" in markdown
    assert "nmap version: Nmap version 7.95" in markdown
    assert "hosts up: 1; down/unknown: 255" in markdown
    assert f"nmap XML evidence sha256: `{'a' * 64}`" in markdown


def test_nmap_network_scan_fails_closed_when_reviewed_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = hosts.configured_private_network_snapshot(("192.168.1.0/24",))
    monkeypatch.setattr(
        local_binaries,
        "_inspect_nmap_executable",
        lambda: local_binaries._NmapExecutableState(None, "nmap_missing"),  # noqa: SLF001
    )

    result = local_binaries.nmap_network_scan(snapshot, profile="discover")

    assert result == {"ok": False, "error": "nmap_missing", "tool": "nmap"}
