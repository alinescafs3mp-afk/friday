"""Static and unit contracts for the detachable Windows/SGLang node bundle."""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "deploy" / "secondary-brain" / "windows-sglang"
SCRIPTS = BUNDLE / "scripts"
RUNTIME = BUNDLE / "runtime"
SGLANG_IMAGE = "lmsysorg/sglang@sha256:297f0bfea5e9f92680f8dd49ae18d048c9634f953be50b37f9bfe9509e947405"
SGLANG_CONFIG_DIGEST = "sha256:f7adc6c05df9ff711b82ad291cf1db6eaf30590c4d929833d632abfef3895efc"
GATEWAY_INDEX_DIGEST = "sha256:d61d7ef52430df468e74ed6ee6e914429b80e20ba988e3176278a73165f876cf"
SOURCE_MANIFEST_SHA256 = "438df0a0b2f6b4164c2fd9d9ed309925abbc94ed8deb056b692d2ccad7887fd9"


@pytest.fixture(scope="module", autouse=True)
def _script_import_path() -> Iterator[None]:
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(RUNTIME))
    try:
        yield
    finally:
        sys.path.remove(str(RUNTIME))
        sys.path.remove(str(SCRIPTS))


def _compose() -> dict[str, Any]:
    value = yaml.safe_load((BUNDLE / "compose.yml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_bundle_has_the_closed_operator_surface() -> None:
    required = {
        "README.md",
        ".gitattributes",
        "compose.yml",
        ".env.example",
        "gateway.conf.template",
        "model-manifest.example.json",
        "runtime-manifest.example.json",
        "hardware-runtime-receipt.example.json",
        "runtime/hardware_runtime_contract.py",
        "runtime/launch_sglang_secure.py",
        "runtime/profile_contract.py",
        "runtime/source_model_manifest.py",
        "runtime/render_gateway_secure.sh",
        "scripts/accept-hardware-runtime-receipt.ps1",
        "scripts/accept_runtime_manifest.py",
        "scripts/preflight.ps1",
        "scripts/install-openssh.ps1",
        "scripts/firewall.ps1",
        "scripts/firewall-classifier.ps1",
        "scripts/test-firewall-classifier.ps1",
        "scripts/provision-secrets.ps1",
        "scripts/model_volume_tool.py",
        "scripts/populate-model-volume.ps1",
        "scripts/probe_endpoint.py",
        "scripts/quality_battery.py",
        "scripts/failure_battery.py",
        "scripts/live_failure_battery.py",
        "scripts/tune_context.py",
        "scripts/soak.py",
        "scripts/runtime_profile_operator.py",
    }
    assert required <= {path.relative_to(BUNDLE).as_posix() for path in BUNDLE.rglob("*") if path.is_file()}


def test_native_runtime_replaces_the_obsolete_internal_compat_image() -> None:
    assert not any(path.is_file() for path in (BUNDLE / "runtime-compat").glob("*"))
    obsolete = {
        "modelopt-converter-manifest.example.json",
        "runtime/converted_model_manifest.py",
        "scripts/convert-modelopt-nvfp4.ps1",
        "scripts/generate_calibration.py",
        "scripts/modelopt_conversion_tool.py",
    }
    assert not any((BUNDLE / path).exists() for path in obsolete)
    compose = _compose()
    assert compose["services"]["sglang"]["image"].startswith("${FRIDAY_SECONDARY_SGLANG_IMAGE:?")
    assert SGLANG_IMAGE in (BUNDLE / ".env.example").read_text(encoding="utf-8")
    assert "flashinfer_mxfp4" in (RUNTIME / "profile_contract.py").read_text(encoding="utf-8")


def test_only_tls_gateway_is_published_to_lan() -> None:
    compose = _compose()
    services = compose["services"]
    assert isinstance(services, dict)
    engine = services["sglang"]
    gateway = services["gateway"]
    assert isinstance(engine, dict) and isinstance(gateway, dict)
    assert "ports" not in engine
    assert engine["expose"] == ["30000"]
    assert gateway["ports"] == [
        "${FRIDAY_SECONDARY_BIND_ADDRESS:?exact laptop LAN address is required}:8443:8443"
    ]
    assert engine["networks"] == ["engine-internal"]
    assert gateway["networks"] == ["engine-internal", "gateway-publish"]
    assert engine["entrypoint"] == ["python3"]
    assert engine["command"] == ["/run/friday-bootstrap/launch_sglang_secure.py"]
    assert gateway["entrypoint"] == ["/bin/sh"]
    assert gateway["command"] == ["/run/friday-bootstrap/render_gateway_secure.sh"]
    networks = compose["networks"]
    assert networks["engine-internal"]["internal"] is True
    assert networks["gateway-publish"]["internal"] is False
    assert networks["gateway-publish"]["attachable"] is False


def test_compose_is_digest_gated_and_has_no_host_authority() -> None:
    compose = _compose()
    services = compose["services"]
    for name in ("sglang", "gateway"):
        service = services[name]
        assert service["pull_policy"] == "never"
        assert service["read_only"] is True
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["cap_drop"] == ["ALL"]
        assert service.get("privileged") is None
        assert service.get("network_mode") is None
    assert services["sglang"]["image"].startswith("${FRIDAY_SECONDARY_SGLANG_IMAGE:?")
    assert services["sglang"]["environment"]["FRIDAY_SECONDARY_RUNTIME_IMAGE"] == services["sglang"]["image"]
    assert services["gateway"]["image"] == (
        "nginxinc/nginx-unprivileged@sha256:d61d7ef52430df468e74ed6ee6e914429b80e20ba988e3176278a73165f876cf"
    )
    assert services["gateway"]["user"] == "101"
    text = (BUNDLE / "compose.yml").read_text(encoding="utf-8")
    assert "/var/run/docker.sock" not in text
    assert "network_mode: host" not in text
    assert "privileged:" not in text
    assert "latest" not in text.casefold()


def test_model_is_read_only_and_cache_and_tmp_are_the_only_mutable_runtime_surfaces() -> None:
    engine = _compose()["services"]["sglang"]
    volumes = engine["volumes"]
    model = next(row for row in volumes if row["target"] == "/source")
    assert model == {
        "type": "volume",
        "source": "model-snapshot",
        "target": "/source",
        "read_only": True,
    }
    assert not any(row.get("target") == "/run/friday-model/accepted.json" for row in volumes)
    accepted_hardware = next(row for row in volumes if row["target"] == "/run/friday-hardware/accepted.json")
    assert accepted_hardware == {
        "type": "bind",
        "source": (
            "${FRIDAY_SECONDARY_HARDWARE_RUNTIME_RECEIPT_PATH:?accepted hardware/runtime receipt is required}"
        ),
        "target": "/run/friday-hardware/accepted.json",
        "read_only": True,
        "bind": {"create_host_path": False},
    }
    assert engine["tmpfs"] == ["/tmp:size=2g,mode=1777", "/run:size=16m,mode=0755"]
    assert not any(volume.get("target") == "/var/run/docker.sock" for volume in volumes)
    assert engine["environment"]["TRITON_CACHE_DIR"] == "/root/.cache/triton"
    assert engine["environment"]["CUDA_CACHE_PATH"] == "/root/.cache/cuda"
    assert engine["environment"]["TORCHINDUCTOR_CACHE_DIR"] == "/root/.cache/torchinductor"


def test_gateway_uses_distinct_file_secrets_tls_and_a_closed_route_set() -> None:
    compose = _compose()
    engine = compose["services"]["sglang"]
    gateway = compose["services"]["gateway"]
    engine_mounts = {row["target"] for row in engine["volumes"] if row["type"] == "bind"}
    gateway_mounts = {row["target"] for row in gateway["volumes"] if row["type"] == "bind"}
    assert engine_mounts == {
        "/run/friday-secrets/sglang-api-key",
        "/run/friday-bootstrap/launch_sglang_secure.py",
        "/run/friday-bootstrap/profile_contract.py",
        "/run/friday-bootstrap/source_model_manifest.py",
        "/run/friday-bootstrap/hardware_runtime_contract.py",
        "/run/friday-hardware/accepted.json",
        "/run/friday-profile/accepted.json",
        "/run/friday-profile/id",
    }
    assert {
        "/run/friday-secrets/gateway-api-key",
        "/run/friday-secrets/sglang-api-key",
        "/run/friday-tls/ca.crt",
        "/run/friday-tls/server.crt",
        "/run/friday-tls/server.key",
        "/run/friday-bootstrap/render_gateway_secure.sh",
        "/run/friday-profile/accepted.json",
        "/run/friday-profile/id",
    } <= gateway_mounts
    assert "environment" not in gateway

    renderer = (BUNDLE / "runtime" / "render_gateway_secure.sh").read_text(encoding="utf-8")
    assert "umask 077" in renderer
    assert "if IFS= read -r SECRET_VALUE" in renderer
    assert 'test "$gateway_key" != "$upstream_key"' in renderer
    assert 'test "${#SECRET_VALUE}" -eq 64' in renderer
    assert "exec nginx -c /tmp/gateway.conf" in renderer
    assert "sed " not in renderer

    launcher = (BUNDLE / "runtime" / "launch_sglang_secure.py").read_text(encoding="utf-8")
    assert "ServerArgs.__repr__ = redacted_repr" in launcher
    assert 'rendered.replace(repr(secret), repr("<redacted>"))' in launcher
    profile_contract = (BUNDLE / "runtime" / "profile_contract.py").read_text(encoding="utf-8")
    assert '"--api-key",' in profile_contract
    assert '"--quantization",' in profile_contract
    assert 'value["quantization"] != "mxfp4"' in profile_contract
    assert 'value["moe_runner_backend"] != "flashinfer_mxfp4"' in profile_contract
    assert '"--flashinfer-mxfp4-moe-precision",' in profile_contract
    assert launcher.index("profile = load_launch_profile(") < launcher.index(
        "from sglang.launch_server import run_server"
    )
    assert launcher.index("verify_source_model_snapshot(") < launcher.index(
        "from sglang.launch_server import run_server"
    )
    assert launcher.index("verify_live_hardware_runtime(") < launcher.index("verify_source_model_snapshot(")
    assert "FRIDAY_SECONDARY_CONTEXT_TOKENS" not in launcher
    assert 'if __name__ == "__main__":' in launcher
    assert launcher.index('if __name__ == "__main__":') < launcher.index(
        "main()", launcher.index('if __name__ == "__main__":')
    )
    compose_text = (BUNDLE / "compose.yml").read_text(encoding="utf-8")
    assert "Authorization: Bearer $$(cat" not in compose_text
    assert "401 Unauthorized" in compose_text
    assert "http://127.0.0.1:30000/v1/models" in compose_text
    assert "p['served_model_alias']" in compose_text
    assert "http://127.0.0.1:30000/health" not in compose_text
    assert "FRIDAY_SECONDARY_CONTEXT_TOKENS" not in compose_text

    policy = (BUNDLE / "gateway.conf.template").read_text(encoding="utf-8")
    assert "listen 8443 ssl;" in policy
    assert "access_log off;" in policy
    assert '"Bearer __GATEWAY_BEARER__" 1;' in policy
    assert 'Authorization "Bearer __SGLANG_BEARER__"' in policy
    assert "location = /v1/models" in policy
    assert "location = /v1/friday-profile" in policy
    assert policy.count('add_header X-Friday-Secondary-Profile-Id "__PROFILE_ID__" always;') == 2
    assert policy.count('add_header X-Friday-Secondary-Profile-Sha256 "__PROFILE_SHA256__" always;') == 2
    assert "proxy_hide_header X-Friday-Secondary-Profile-Id" in policy
    assert 'add_header X-Friday-Secondary-Profile-Sha256 "__PROFILE_SHA256__" always' in policy
    assert "location = /metrics" in policy
    assert "location = /v1/chat/completions" in policy
    assert "location /" in policy and "return 404;" in policy
    assert "proxy_pass http://friday_secondary_sglang" in policy


def test_examples_have_sealed_source_and_nonaccepted_runtime_placeholders() -> None:
    model_raw = (BUNDLE / "model-manifest.example.json").read_bytes()
    model = json.loads(model_raw)
    runtime = json.loads((BUNDLE / "runtime-manifest.example.json").read_text(encoding="utf-8"))
    hardware = json.loads((BUNDLE / "hardware-runtime-receipt.example.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(model_raw).hexdigest() == SOURCE_MANIFEST_SHA256
    assert model["schema"] == "friday.secondary-source-manifest.v1"
    assert model["status"] == "verified"
    assert model["repository"] == "openai/gpt-oss-20b"
    assert model["revision"] == "6cee5e81ee83917806bbde320786a8fb61efebee"
    assert model["root_only"] is True
    assert model["excluded_prefixes"] == ["metal/", "original/"]
    assert model["file_count"] == len(model["files"]) == 14
    assert model["total_bytes"] == 13_789_264_674
    assert runtime["status"] == "template_not_accepted"
    assert runtime["image_ref"] == SGLANG_IMAGE
    assert runtime["image_id"] == SGLANG_IMAGE.removeprefix("lmsysorg/sglang@")
    assert runtime["image_config_digest"] == SGLANG_CONFIG_DIGEST
    assert runtime["image_oci_manifest_digest"] == SGLANG_IMAGE.removeprefix("lmsysorg/sglang@")
    assert runtime["sglang_version"] == "0.5.17"
    assert runtime["sglang_git_revision"] == "29481685462732237d80d86076d6563e1f658102"
    assert runtime["cuda_runtime_version"] == "13.0"
    assert runtime["pytorch_version"] == "2.11.0+cu130"
    assert runtime["flashinfer_version"] == "0.6.15.post1"
    assert runtime["sgl_kernel_version"] == "0.4.5"
    assert runtime["gateway_expected_version"] == "1.31.3"
    assert runtime["gateway_expected_user"] == "101"
    assert runtime["gateway_expected_platform_manifest_digest"] == (
        "sha256:8d764dd92e0b48d0ca94887dc0fe1df6dffc5200b25b2efcc2deb7ffb61d714c"
    )
    assert runtime["gateway_image_id"] == GATEWAY_INDEX_DIGEST
    assert runtime["plain_sglang_lan_published"] is False
    assert runtime["published_endpoint"] == "https://192.168.1.35:8443/v1"
    assert hardware["status"] == "template_not_accepted"
    assert hardware["schema"] == "friday.secondary-hardware-runtime.v1"
    assert hardware["docker"]["client_version"] == "29.7.2"
    assert hardware["docker"]["client_api_version"] == "1.55"
    assert hardware["gpu"] == {
        "compute_capability": "12.0",
        "driver_version": "610.88",
        "memory_total_mib": 16303,
        "name": "NVIDIA GeForce RTX 5080 Laptop GPU",
        "uuid": "GPU-d7ef849e-55f5-f33c-2812-9dc32b644b07",
    }
    env = (BUNDLE / ".env.example").read_text(encoding="utf-8")
    assert f"FRIDAY_SECONDARY_SGLANG_IMAGE={SGLANG_IMAGE}" in env
    assert "FRIDAY_SECONDARY_MODEL_VOLUME=friday-secondary-source-gptoss20b" in env
    assert "FRIDAY_SECONDARY_CONVERTED_MODEL_MANIFEST_PATH" not in env
    assert "FRIDAY_SECONDARY_GATEWAY_IMAGE" not in env
    assert ("FRIDAY_SECONDARY_HARDWARE_RUNTIME_RECEIPT_PATH=./evidence/hardware-runtime.accepted.json") in env
    assert "latest" not in env.casefold()


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "# HELP process_start_time_seconds start\nprocess_start_time_seconds 1700000000.5000\n",
            "1700000000.5",
        ),
        ("process_start_time_seconds 1700000000\n", "1700000000"),
    ],
)
def test_runtime_process_epoch_is_exact_and_normalized(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
    expected: str,
) -> None:
    endpoint = importlib.import_module("endpoint_common")
    monkeypatch.setattr(endpoint, "request_text", lambda *_args, **_kwargs: (body, 0.01))

    assert (
        endpoint.runtime_process_epoch(
            "https://192.168.1.35:8443/v1",
            api_key="a" * 64,
            timeout_sec=1.0,
            ca_file=Path("ca.crt"),
        )
        == expected
    )


@pytest.mark.parametrize(
    "body",
    [
        "",
        "process_start_time_seconds NaN\n",
        "process_start_time_seconds 1\nprocess_start_time_seconds 2\n",
    ],
)
def test_runtime_process_epoch_rejects_missing_ambiguous_or_nonfinite(
    monkeypatch: pytest.MonkeyPatch,
    body: str,
) -> None:
    endpoint = importlib.import_module("endpoint_common")
    monkeypatch.setattr(endpoint, "request_text", lambda *_args, **_kwargs: (body, 0.01))

    with pytest.raises(endpoint.EndpointError, match="runtime process epoch"):
        endpoint.runtime_process_epoch(
            "https://192.168.1.35:8443/v1",
            api_key="a" * 64,
            timeout_sec=1.0,
            ca_file=Path("ca.crt"),
        )


def test_windows_mutations_are_explicit_and_firewall_is_closed_to_primary() -> None:
    openssh = (SCRIPTS / "install-openssh.ps1").read_text(encoding="utf-8")
    firewall = (SCRIPTS / "firewall.ps1").read_text(encoding="utf-8")
    firewall_classifier = (SCRIPTS / "firewall-classifier.ps1").read_text(encoding="utf-8")
    firewall_test = (SCRIPTS / "test-firewall-classifier.ps1").read_text(encoding="utf-8")
    population = (SCRIPTS / "populate-model-volume.ps1").read_text(encoding="utf-8")
    provisioning = (SCRIPTS / "provision-secrets.ps1").read_text(encoding="utf-8")
    for source in (openssh, firewall, population, provisioning):
        assert "[switch]$Apply" in source
        assert "if (-not $Apply" in source
    assert "password_authentication_changed = $false" in openssh
    assert "192.168.1.78" in openssh
    assert "192.168.1.78" in firewall
    assert "-LocalPort 8443" in firewall
    assert "-LocalPort 30000" not in firewall
    assert "Friday.Secondary.SGLang.PrimaryOnly.TCP8443" not in firewall
    assert "Friday.Secondary.SGLang.Allow.TrustedIPv4.TCP8443" in firewall
    assert "Friday.Secondary.SGLang.ApplyGuard.All.TCP8443" in firewall
    assert "Friday.Secondary.SGLang.Block.Complement.IPv4.TCP8443" in firewall
    assert "Friday.Secondary.SGLang.Block.All.IPv6.TCP8443" in firewall
    for address_range in (
        "0.0.0.0-192.168.1.34",
        "192.168.1.36-192.168.1.77",
        "192.168.1.79-255.255.255.255",
        "::/0",
    ):
        assert address_range in firewall
    assert "@($PrimaryFridayHost, $localFridayHost)" in firewall
    assert "-Name $managedRuleName" in firewall
    assert "-PolicyStore ActiveStore" in firewall
    assert "-TracePolicyStore" in firewall
    assert "Assert-FridayFirewallProfilesEnabled" in firewall
    assert "Get-Service -Name MpsSvc -ErrorAction Stop" in firewall
    assert "Windows Defender Firewall service must be running." in firewall
    assert "DisabledInterfaceAliases" in firewall
    assert "Get-FridayAuthenticatedBypassConflicts" in firewall
    bypass_preflight = firewall.index("if (@(Get-FridayAuthenticatedBypassConflicts).Count -ne 0)")
    guard_create = firewall.index("New-NetFirewallRule -Name $applyGuardName")
    complement_repair = firewall.index("# Under the verified guard")
    first_remove = firewall.index("Remove-NetFirewallRule", complement_repair)
    allow_create = firewall.index("New-NetFirewallRule -Name $managedRuleName")
    pre_guard_removal_audit = firewall.index("Assert-FridayFinalCoverage", allow_create)
    guard_remove = firewall.index("$guardPersistentReadback[0] | Remove-NetFirewallRule")
    post_guard_removal_audit = firewall.rindex("Assert-FridayFinalCoverage")
    assert bypass_preflight < guard_create < complement_repair < first_remove < allow_create
    assert allow_create < pre_guard_removal_audit < guard_remove < post_guard_removal_audit
    assert firewall.count("Assert-FridayFinalCoverage") >= 3
    assert firewall.rindex("New-NetFirewallRule -Name $applyGuardName") > guard_remove
    assert "Pre-removal firewall coverage audit failed; the Apply guard remains installed." in firewall
    assert "Post-removal firewall coverage audit failed; closed-state recovery was attempted." in firewall
    assert "Where-Object { [string]$_.DisplayName" not in firewall
    assert "Get-FridayFirewallRuleAssessment" in firewall_classifier
    assert "Get-FridayPortRelation8443" in firewall_classifier
    assert "Test-FridayCanonicalSpecificAppContainerSid" in firewall_classifier
    assert "S-1-15-2-" in firewall_classifier
    assert "-In-Allow-ServerCapability" in firewall_classifier
    assert "PackageFamilyName" in firewall_classifier
    assert "PolicyStoreSourceType" in firewall_classifier
    assert "Test-FridayManagedBlockFirewallRuleExact" in firewall_classifier
    assert "Get-FridayAuthenticatedBypassAssessment" in firewall_classifier
    assert "OverrideBlockRules" in firewall_classifier
    assert "DynamicTarget" in firewall_classifier
    assert "InterfaceAlias" in firewall_classifier
    assert "Owner" in firewall_classifier
    assert "display_name_does_not_exempt_broad_remote_address" in firewall_test
    assert "exact_managed_complement_block_is_accepted" in firewall_test
    assert "authenticated_any_port_bypass_conflicts" in firewall_test
    assert "normal_allow_skips_irrelevant_port_parsing" in firewall_test
    assert "narrowed_interface_alias_fails_exact_allow" in firewall_test
    assert "missing_application_filter_fails_closed" in firewall_test
    assert "malformed_installed_pfn_fails_closed" in firewall_test
    assert "ConvertTo-Json" in firewall_test
    assert ".\\scripts\\test-firewall-classifier.ps1" in (BUNDLE / "README.md").read_text(encoding="utf-8")
    assert "'--network', 'none'" in population
    assert "--pull', 'never" in population
    assert "docker pull" not in population
    assert "friday-secondary-source-gptoss20b" in population
    assert "openai/gpt-oss-20b" in population
    assert "6cee5e81ee83917806bbde320786a8fb61efebee" in population
    assert SGLANG_IMAGE in population
    assert SGLANG_IMAGE.removeprefix("lmsysorg/sglang@") in population
    assert "$DownloaderImage -cne $expectedDownloaderImage" in population
    assert "$downloaderInspection.Id -cne $expectedDownloaderImageId" in population
    assert "$downloaderInspection.Descriptor.digest -cne $expectedDownloaderManifest" in population
    assert "TokenFile" not in population
    assert "New-RandomHex 32" in provisioning
    assert "distinct_bearers_verified = $true" in provisioning
    assert "IP Address:192\\.168\\.1\\.35" in provisioning
    assert "secret_values_reported = $false" in provisioning
    assert "$acl.SetAccessRuleProtection($true, $false)" in provisioning
    assert "$acl.RemoveAccessRuleSpecific($existing)" in provisioning
    assert "$observed.AreAccessRulesProtected" in provisioning
    assert "Secret ACL readback differs from the exact owner/SYSTEM allowlist" in provisioning
    preflight = (SCRIPTS / "preflight.ps1").read_text(encoding="utf-8")
    promotion = (SCRIPTS / "accept-hardware-runtime-receipt.ps1").read_text(encoding="utf-8")
    assert "[switch]$InspectGatewayImage" in preflight
    assert SGLANG_IMAGE in preflight
    assert SGLANG_CONFIG_DIGEST in preflight
    assert "$sglangInspection.Id -cne $expectedSglangImageId" in preflight
    assert "$sglangInspection.RepoDigests" in preflight
    assert "$sglangInspection.Descriptor.digest -cne $expectedSglangOciManifestDigest" in preflight
    assert "Descriptor.annotations" not in preflight
    assert "ImageManifestDescriptor" in preflight
    assert "'ps', '--all', '--quiet', 'selector'" in preflight
    capture_start = preflight.index("function Invoke-Captured")
    capture_end = preflight.index("\n}\n\nfunction Get-TextSha256", capture_start)
    assert "2>&1" not in preflight[capture_start:capture_end]
    assert "$ErrorActionPreference = 'Continue'" in preflight
    assert "$startInfo.StandardOutputEncoding = [Text.Encoding]::Unicode" in preflight
    assert "$wslVersion = Invoke-WslCaptured '--version'" in preflight
    assert "$wslStatus = Invoke-WslCaptured '--status'" in preflight
    assert "Invoke-Captured 'wsl.exe'" not in preflight
    assert "CudaCanaryImage" not in preflight
    assert "$SglangImage, '-c', $canaryProgram" in preflight
    assert '$normalized = $Value.Replace("`r`n", "`n")' in preflight
    assert "Container program contains a non-CRLF carriage return." in preflight
    for payload in ("canaryProgram", "versionProbeCode", "gatewayRuntimeProbeProgram"):
        assert f"${payload} = ConvertTo-LfContainerProgram ${payload}" in preflight
    assert "torch.device('cuda:0')" in preflight
    assert "m.version('flashinfer-python')" in preflight
    assert 'torch.device("cuda:0")' not in preflight
    assert 'm.version("flashinfer-python")' not in preflight
    assert "$gatewayObservation = $null" in preflight
    assert "$gatewayImage = $null" not in preflight
    assert "NGINX_VERSION=1.31.3" in preflight
    assert "$gatewayInspection.Id -cne $expectedGatewayIndex" in preflight
    assert "$gatewayInspection.Descriptor.digest -cne $expectedGatewayIndex" in preflight
    assert "$gatewayInspection.Descriptor.mediaType -cne $ociIndexMediaType" in preflight
    assert "$GatewayImage $expectedGatewayIndex $expectedGatewayPlatformManifest" in preflight
    assert "Config.User -cne '101'" in preflight
    assert "test $(id -u) = 101" in preflight
    assert "grep -Fqx 'nginx version: nginx/1.31.3'" in preflight
    assert 'test "$(id -u)" = 101' not in preflight
    assert "nginx version: nginx/1.31.3" in preflight
    assert "inventory_incomplete" in preflight
    for expected in (
        "0JzQsNC50LrRgNC+0YHQvtGE0YIgV2luZG93cyAxMSBQcm8=",
        "10.0.26200.9168",
        "6.6.114.1-1",
        "Docker Desktop.exe",
        "4.87.0.236836",
        "29.7.2",
        "NVIDIA GeForce RTX 5080 Laptop GPU",
        "GPU-d7ef849e-55f5-f33c-2812-9dc32b644b07",
        "--query-gpu=uuid,name,memory.total,compute_cap,driver_version",
        "HardwareRuntimeReceiptOutputPath",
        "friday.secondary-hardware-runtime.v1",
    ):
        assert expected in preflight
    assert all(byte < 128 for byte in (SCRIPTS / "preflight.ps1").read_bytes())
    assert all(byte < 128 for byte in (SCRIPTS / "accept-hardware-runtime-receipt.ps1").read_bytes())
    assert "$env:" not in preflight.casefold()
    assert "--pull', 'never" in preflight
    for flag in (
        "--dtype",
        "--moe-runner-backend",
        "--flashinfer-mxfp4-moe-precision",
        "--cuda-graph-backend-decode",
        "--cuda-graph-backend-prefill",
    ):
        assert flag in preflight
    for version in ("2.11.0+cu130", "0.6.15.post1", "0.4.5"):
        assert version in preflight
    assert "m.version('sglang-kernel')" in preflight
    assert "m.version('sgl-kernel')" not in preflight
    assert "[switch]$Apply" in promotion
    assert "if ($Apply)" in promotion
    assert "[IO.FileMode]::CreateNew" in promotion
    assert "observed_unaccepted" in promotion
    assert "overwritten = $false" in promotion
    assert "$env:" not in promotion.casefold()


def test_missing_model_volume_is_a_normal_powershell5_population_state() -> None:
    source = (SCRIPTS / "populate-model-volume.ps1").read_text(encoding="utf-8")
    function_start = source.index("function Test-DockerVolumeExists")
    function_end = source.index("\n}\n\nInvoke-DockerCapture", function_start) + 2
    function = source[function_start:function_end]
    continue_index = function.index("$ErrorActionPreference = 'Continue'")
    inspect_index = function.index("& docker volume inspect $Name")
    exit_code_index = function.index("$inspectExitCode = $LASTEXITCODE")
    restore_index = function.index("$ErrorActionPreference = $previousErrorActionPreference")
    assert continue_index < inspect_index < exit_code_index < restore_index
    assert "if ($inspectExitCode -eq 1)" in function
    assert "if (Test-DockerVolumeExists $VolumeName)" in source[function_end:]
    assert "& docker volume inspect $VolumeName *> $null" not in source


def test_bundle_contains_no_supplied_bootstrap_credentials() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in BUNDLE.rglob("*")
        if path.is_file()
        and path.suffix in {".cnf", ".example", ".json", ".md", ".ps1", ".py", ".template", ".yml"}
    )
    assert "321" not in text
    assert re.search(r"\bDest\b", text) is None
    assert "FRIDAY_LLM_API_KEY" not in text


def test_generated_certificates_and_bearers_are_ignored() -> None:
    ignored = (BUNDLE / ".gitignore").read_text(encoding="utf-8")
    assert "secrets/*" in ignored
    assert "secrets/tls/*" in ignored
    assert "!secrets/tls/.gitkeep" in ignored
    tracked = {path.relative_to(BUNDLE).as_posix() for path in BUNDLE.rglob("*") if path.is_file()}
    assert "secrets/gateway-api-key" not in tracked
    assert "secrets/sglang-api-key" not in tracked
    assert "secrets/tls/ca.crt" not in tracked
    assert "secrets/tls/server.crt" not in tracked


def test_linux_container_entrypoints_are_checkout_stable_lf() -> None:
    attributes = (BUNDLE / ".gitattributes").read_text(encoding="utf-8")
    assert "runtime/*.sh text eol=lf" in attributes
    assert "runtime/*.py text eol=lf" in attributes


def test_endpoint_url_rejects_plain_lan_and_embedded_credentials() -> None:
    common = importlib.import_module("endpoint_common")
    assert common.normalize_base_url("http://127.0.0.1:30000") == "http://127.0.0.1:30000/v1"
    assert common.normalize_base_url("https://192.168.1.35:8443/v1") == "https://192.168.1.35:8443/v1"
    with pytest.raises(common.EndpointError):
        common.normalize_base_url("http://192.168.1.35:30000/v1")
    with pytest.raises(common.EndpointError):
        common.normalize_base_url("https://user:secret@192.168.1.35:8443/v1")


def test_completion_projection_drops_reasoning_and_rejects_alias_or_markers() -> None:
    common = importlib.import_module("endpoint_common")
    body = {
        "model": common.EXPECTED_MODEL,
        "choices": [
            {
                "message": {"content": "Готово.", "reasoning_content": "private chain"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    result = common.parse_completion(body, latency_sec=0.1)
    assert result.content == "Готово."
    assert result.reasoning_present is True
    assert not hasattr(result, "reasoning_content")
    wrong = {**body, "model": "wrong"}
    with pytest.raises(common.EndpointError):
        common.parse_completion(wrong, latency_sec=0.1)
    leaked = json.loads(json.dumps(body))
    leaked["choices"][0]["message"]["content"] = "<|channel|>analysis"
    with pytest.raises(common.EndpointError):
        common.parse_completion(leaked, latency_sec=0.1)
    leaked["choices"][0]["message"]["content"] = "<think>private chain</think>visible"
    with pytest.raises(common.EndpointError):
        common.parse_completion(leaked, latency_sec=0.1)
    numerical = json.loads(json.dumps(body))
    numerical["choices"][0]["message"]["content"] = "value = NaN"
    with pytest.raises(common.EndpointError):
        common.parse_completion(numerical, latency_sec=0.1)


def test_model_volume_manifest_matches_the_runtime_source_contract() -> None:
    tool = importlib.import_module("model_volume_tool")
    runtime = importlib.import_module("source_model_manifest")
    raw = (BUNDLE / "model-manifest.example.json").read_bytes()

    assert raw == tool.canonical_manifest_bytes()
    assert hashlib.sha256(raw).hexdigest() == SOURCE_MANIFEST_SHA256
    assert tool.SCHEMA == runtime.SCHEMA
    assert tool.MODEL_REPOSITORY == runtime.SOURCE_REPOSITORY
    assert tool.MODEL_REVISION == runtime.SOURCE_REVISION
    assert tool.SOURCE_FILES == runtime.SOURCE_FILES
    assert tool.SOURCE_FILE_COUNT == runtime.SOURCE_FILE_COUNT
    assert tool.SOURCE_TOTAL_BYTES == runtime.SOURCE_TOTAL_BYTES
    assert tool.SOURCE_MANIFEST_RAW_SHA256 == runtime.SOURCE_MANIFEST_RAW_SHA256
    assert tool.SOURCE_MANIFEST_SEMANTIC_SHA256 == runtime.SOURCE_MANIFEST_SEMANTIC_SHA256


def test_model_volume_verifier_is_strict_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = importlib.import_module("model_volume_tool")
    source = tmp_path / "source"
    snapshot = source / "snapshot"
    snapshot.mkdir(parents=True)
    content = b"bounded model fixture"
    (snapshot / "config.json").write_bytes(content)
    files = {"config.json": (len(content), hashlib.sha256(content).hexdigest())}
    monkeypatch.setattr(tool, "SOURCE_FILES", files)
    monkeypatch.setattr(tool, "SOURCE_FILE_COUNT", 1)
    monkeypatch.setattr(tool, "SOURCE_TOTAL_BYTES", len(content))
    manifest: dict[str, Any] = {
        "schema": tool.SCHEMA,
        "status": "verified",
        "repository": tool.MODEL_REPOSITORY,
        "revision": tool.MODEL_REVISION,
        "root_only": True,
        "excluded_prefixes": tool.SOURCE_EXCLUDED_PREFIXES,
        "file_count": 1,
        "total_bytes": len(content),
        "files": {
            "config.json": {
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        },
    }
    raw = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    semantic = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(tool, "SOURCE_MANIFEST_RAW_SHA256", manifest_sha256)
    monkeypatch.setattr(tool, "SOURCE_MANIFEST_SEMANTIC_SHA256", hashlib.sha256(semantic).hexdigest())
    internal_manifest = source / "source-manifest.json"
    external_manifest = tmp_path / "source-model.verified.json"
    internal_manifest.write_bytes(raw)
    external_manifest.write_bytes(raw)

    result = tool.verify_manifest(source, external_manifest)
    assert result["status"] == "passed"
    assert result["manifest_raw_sha256"] == manifest_sha256

    manifest["status"] = "template_not_accepted"
    external_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(tool.ModelVolumeError):
        tool.verify_manifest(source, external_manifest)

    external_manifest.write_bytes(raw)
    (source / "unexpected").write_bytes(b"no")
    with pytest.raises(tool.ModelVolumeError):
        tool.verify_manifest(source, external_manifest)


def _candidate_runtime_profile() -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "friday.secondary-runtime-profile.v2",
        "status": "candidate",
        "profile_id": "pending",
        "engine_binding_sha256": "0" * 64,
        "endpoint_base_url": "https://192.168.1.35:8443/v1",
        "served_model_alias": "pending",
        "source_model_repository": "openai/gpt-oss-20b",
        "source_model_revision": "6cee5e81ee83917806bbde320786a8fb61efebee",
        "hardware_runtime_receipt_sha256": (
            "0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f"
        ),
        "source_model_manifest_sha256": SOURCE_MANIFEST_SHA256,
        "gateway_ca_certificate_sha256": "1" * 64,
        "runtime_image": SGLANG_IMAGE,
        "runtime_image_config_digest": SGLANG_CONFIG_DIGEST,
        "runtime_image_oci_manifest_digest": SGLANG_IMAGE.removeprefix("lmsysorg/sglang@"),
        "runtime_source_revision": "29481685462732237d80d86076d6563e1f658102",
        "runtime_manifest_sha256": "f" * 64,
        "model_path": "/source/snapshot",
        "quantization": "mxfp4",
        "dtype": "bfloat16",
        "kv_cache_dtype": "bf16",
        "kv_cache_scale_policy": "not_applicable",
        "attention_backend": "triton",
        "prefill_attention_backend": "triton",
        "decode_attention_backend": "triton",
        "sampling_backend": "pytorch",
        "moe_runner_backend": "flashinfer_mxfp4",
        "mxfp4_moe_precision": "default",
        "page_size": 1,
        "radix_cache_enabled": True,
        "overlap_schedule_enabled": True,
        "hybrid_swa_memory_enabled": True,
        "swa_full_tokens_ratio": "0.50",
        "context_tokens": 4096,
        "max_total_tokens": 4096,
        "mem_fraction_static": "0.97",
        "max_running_requests": 1,
        "max_output_tokens": 512,
        "chunked_prefill_size": 1024,
        "cuda_graph_backend_decode": "disabled",
        "cuda_graph_max_bs_decode": 0,
        "cuda_graph_bs_decode": [],
        "cuda_graph_backend_prefill": "disabled",
        "allowed_modes": ["assist", "shadow"],
        "allowed_workloads": ["extract"],
        "no_cpu_offload": True,
        "quality_evidence_sha256": "0" * 64,
        "capacity_evidence_sha256": "0" * 64,
        "soak_evidence_sha256": "0" * 64,
        "failure_evidence_sha256": "0" * 64,
    }
    _seal_runtime_profile(value)
    return value


def _seal_runtime_profile(value: dict[str, Any]) -> None:
    binding = importlib.import_module("profile_contract").engine_binding_sha256(value)
    value["engine_binding_sha256"] = binding
    value["profile_id"] = f"gptoss20b-{binding}"
    value["served_model_alias"] = f"friday-secondary-{value['profile_id']}"


def _write_profile_fixture(tmp_path: Path, value: dict[str, Any]) -> tuple[Path, Path]:
    manifest = tmp_path / "profile.json"
    profile_id = tmp_path / "profile.id"
    manifest.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    profile_id.write_bytes(str(value["profile_id"]).encode("ascii"))
    return manifest, profile_id


def test_shared_profile_contract_derives_every_capacity_argument(tmp_path: Path) -> None:
    contract = importlib.import_module("profile_contract")
    manifest, profile_id = _write_profile_fixture(tmp_path, _candidate_runtime_profile())

    profile = contract.load_launch_profile(
        manifest,
        profile_id,
        actual_runtime_image=SGLANG_IMAGE,
    )
    arguments = profile.server_arguments("a" * 64)

    assert profile.manifest_sha256 == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert profile.model_path == "/source/snapshot"
    assert profile.source_model_manifest_sha256 == SOURCE_MANIFEST_SHA256
    assert profile.hardware_runtime_receipt_sha256 == (
        "0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f"
    )
    assert arguments == [
        "--model-path",
        "/source/snapshot",
        "--served-model-name",
        profile.served_model_alias,
        "--quantization",
        "mxfp4",
        "--dtype",
        "bfloat16",
        "--host",
        "0.0.0.0",
        "--port",
        "30000",
        "--api-key",
        "a" * 64,
        "--reasoning-parser",
        "gpt-oss",
        "--tool-call-parser",
        "gpt-oss",
        "--attention-backend",
        "triton",
        "--prefill-attention-backend",
        "triton",
        "--decode-attention-backend",
        "triton",
        "--sampling-backend",
        "pytorch",
        "--moe-runner-backend",
        "flashinfer_mxfp4",
        "--flashinfer-mxfp4-moe-precision",
        "default",
        "--kv-cache-dtype",
        "bf16",
        "--page-size",
        "1",
        "--swa-full-tokens-ratio",
        "0.50",
        "--chunked-prefill-size",
        "1024",
        "--max-running-requests",
        "1",
        "--cuda-graph-backend-decode",
        "disabled",
        "--cuda-graph-backend-prefill",
        "disabled",
        "--context-length",
        "4096",
        "--max-total-tokens",
        "4096",
        "--mem-fraction-static",
        "0.97",
        "--enable-metrics",
        "--enable-cache-report",
    ]
    assert profile.runtime_image == SGLANG_IMAGE
    assert profile.runtime_image_config_digest == SGLANG_CONFIG_DIGEST


def test_shared_profile_contract_emits_the_full_optimized_surface(tmp_path: Path) -> None:
    contract = importlib.import_module("profile_contract")
    candidate = _candidate_runtime_profile()
    candidate.update(
        {
            "kv_cache_dtype": "fp8_e4m3",
            "kv_cache_scale_policy": "implicit_unit",
            "decode_attention_backend": "trtllm_mha",
            "sampling_backend": "flashinfer",
            "page_size": 16,
            "radix_cache_enabled": False,
            "overlap_schedule_enabled": False,
            "swa_full_tokens_ratio": "1.00",
            "context_tokens": 65536,
            "max_total_tokens": 65536,
            "mem_fraction_static": "0.95",
            "cuda_graph_backend_decode": "full",
            "cuda_graph_max_bs_decode": 1,
            "cuda_graph_bs_decode": [1],
        }
    )
    _seal_runtime_profile(candidate)
    manifest, profile_id = _write_profile_fixture(tmp_path, candidate)

    profile = contract.load_launch_profile(
        manifest,
        profile_id,
        actual_runtime_image=SGLANG_IMAGE,
    )
    arguments = profile.server_arguments("a" * 64)

    assert arguments[arguments.index("--kv-cache-dtype") + 1] == "fp8_e4m3"
    assert arguments[arguments.index("--decode-attention-backend") + 1] == "trtllm_mha"
    assert arguments[arguments.index("--sampling-backend") + 1] == "flashinfer"
    assert arguments[arguments.index("--page-size") + 1] == "16"
    assert arguments[arguments.index("--swa-full-tokens-ratio") + 1] == "1.00"
    assert arguments[arguments.index("--cuda-graph-max-bs-decode") + 1] == "1"
    assert arguments[arguments.index("--cuda-graph-bs-decode") + 1] == "1"
    assert arguments[arguments.index("--context-length") + 1] == "65536"
    assert arguments[arguments.index("--mem-fraction-static") + 1] == "0.95"
    assert arguments.count("--disable-radix-cache") == 1
    assert arguments.count("--disable-overlap-schedule") == 1


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("status", "accepted"),
        ("endpoint_base_url", "https://192.168.1.36:8443/v1"),
        ("served_model_alias", "wrong"),
        ("hardware_runtime_receipt_sha256", "3" * 64),
        ("source_model_manifest_sha256", "3" * 64),
        ("runtime_image", "lmsysorg/sglang:latest"),
        ("runtime_image_config_digest", "sha256:" + "3" * 64),
        ("runtime_image_oci_manifest_digest", "sha256:" + "3" * 64),
        ("runtime_source_revision", "3" * 40),
        ("quantization", "modelopt_fp4"),
        ("dtype", "float16"),
        ("kv_cache_dtype", "fp8_e5m2"),
        ("kv_cache_scale_policy", "explicit"),
        ("attention_backend", "flashinfer"),
        ("prefill_attention_backend", "flashinfer"),
        ("decode_attention_backend", "flashinfer"),
        ("sampling_backend", "triton"),
        ("moe_runner_backend", "cutlass"),
        ("mxfp4_moe_precision", "bf16"),
        ("page_size", 8),
        ("radix_cache_enabled", 1),
        ("overlap_schedule_enabled", 1),
        ("hybrid_swa_memory_enabled", False),
        ("swa_full_tokens_ratio", "0.75"),
        ("cuda_graph_backend_decode", "flashinfer"),
        ("cuda_graph_max_bs_decode", 2),
        ("cuda_graph_bs_decode", [2]),
        ("cuda_graph_backend_prefill", "flashinfer"),
        ("context_tokens", 10_000),
        ("max_total_tokens", 12_288),
        ("max_running_requests", 2),
        ("chunked_prefill_size", 513),
        ("mem_fraction_static", "0.99"),
        ("no_cpu_offload", False),
    ],
)
def test_shared_profile_contract_rejects_mutation(tmp_path: Path, key: str, value: Any) -> None:
    contract = importlib.import_module("profile_contract")
    candidate = copy.deepcopy(_candidate_runtime_profile())
    candidate[key] = value
    if key != "served_model_alias":
        _seal_runtime_profile(candidate)
    manifest, profile_id = _write_profile_fixture(tmp_path, candidate)
    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            manifest,
            profile_id,
            actual_runtime_image=SGLANG_IMAGE,
        )


@pytest.mark.parametrize(
    ("kv_cache_dtype", "kv_cache_scale_policy"),
    [("bf16", "implicit_unit"), ("fp8_e4m3", "not_applicable")],
)
def test_shared_profile_rejects_mismatched_kv_scale_policy(
    tmp_path: Path,
    kv_cache_dtype: str,
    kv_cache_scale_policy: str,
) -> None:
    contract = importlib.import_module("profile_contract")
    candidate = _candidate_runtime_profile()
    candidate["kv_cache_dtype"] = kv_cache_dtype
    candidate["kv_cache_scale_policy"] = kv_cache_scale_policy
    _seal_runtime_profile(candidate)
    manifest, profile_id = _write_profile_fixture(tmp_path, candidate)

    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            manifest,
            profile_id,
            actual_runtime_image=SGLANG_IMAGE,
        )


@pytest.mark.parametrize(
    ("backend", "maximum", "batch_sizes"),
    [
        ("disabled", 1, [1]),
        ("disabled", 0, [1]),
        ("full", 0, []),
        ("full", 1, []),
    ],
)
def test_shared_profile_rejects_inexact_decode_graph_shape(
    tmp_path: Path,
    backend: str,
    maximum: int,
    batch_sizes: list[int],
) -> None:
    contract = importlib.import_module("profile_contract")
    candidate = _candidate_runtime_profile()
    candidate["cuda_graph_backend_decode"] = backend
    candidate["cuda_graph_max_bs_decode"] = maximum
    candidate["cuda_graph_bs_decode"] = batch_sizes
    _seal_runtime_profile(candidate)
    manifest, profile_id = _write_profile_fixture(tmp_path, candidate)

    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            manifest,
            profile_id,
            actual_runtime_image=SGLANG_IMAGE,
        )


@pytest.mark.parametrize("mutation", ["missing", "unknown"])
def test_shared_profile_surface_is_closed(tmp_path: Path, mutation: str) -> None:
    contract = importlib.import_module("profile_contract")
    candidate = _candidate_runtime_profile()
    if mutation == "missing":
        candidate.pop("sampling_backend")
    else:
        candidate["unreviewed_engine_flag"] = True
    manifest, profile_id = _write_profile_fixture(tmp_path, candidate)

    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            manifest,
            profile_id,
            actual_runtime_image=SGLANG_IMAGE,
        )


def test_shared_profile_rejects_actual_runtime_image_drift(tmp_path: Path) -> None:
    contract = importlib.import_module("profile_contract")
    manifest, profile_id = _write_profile_fixture(tmp_path, _candidate_runtime_profile())
    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            manifest,
            profile_id,
            actual_runtime_image="lmsysorg/sglang@sha256:" + "0" * 64,
        )


def test_hardware_receipt_hash_changes_the_engine_binding() -> None:
    contract = importlib.import_module("profile_contract")
    candidate = _candidate_runtime_profile()
    original = candidate["engine_binding_sha256"]
    candidate["hardware_runtime_receipt_sha256"] = "3" * 64
    assert contract.engine_binding_sha256(candidate) != original


def test_profile_contract_rejects_symlinks_and_oversized_inputs(tmp_path: Path) -> None:
    contract = importlib.import_module("profile_contract")
    manifest, profile_id = _write_profile_fixture(tmp_path, _candidate_runtime_profile())

    manifest_link = tmp_path / "profile-link.json"
    manifest_link.symlink_to(manifest)
    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            manifest_link,
            profile_id,
            actual_runtime_image=SGLANG_IMAGE,
        )

    profile_id_link = tmp_path / "profile-link.id"
    profile_id_link.symlink_to(profile_id)
    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            manifest,
            profile_id_link,
            actual_runtime_image=SGLANG_IMAGE,
        )

    oversized_id = tmp_path / "oversized.id"
    oversized_id.write_bytes(b"a" * 81)
    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            manifest,
            oversized_id,
            actual_runtime_image=SGLANG_IMAGE,
        )

    oversized_manifest = tmp_path / "oversized.json"
    oversized_manifest.write_bytes(b"{" + b" " * 65_536)
    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            oversized_manifest,
            profile_id,
            actual_runtime_image=SGLANG_IMAGE,
        )


def _accepted_hardware_receipt(contract: Any) -> dict[str, Any]:
    return {
        "docker": copy.deepcopy(contract.EXPECTED_DOCKER),
        "gpu": copy.deepcopy(contract.EXPECTED_GPU),
        "schema": contract.SCHEMA,
        "status": "accepted",
        "windows": copy.deepcopy(contract.EXPECTED_WINDOWS),
        "wsl": copy.deepcopy(contract.EXPECTED_WSL),
    }


def test_hardware_receipt_and_live_gpu_are_exactly_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = importlib.import_module("hardware_runtime_contract")
    receipt = _accepted_hardware_receipt(contract)
    raw = contract.canonical_receipt_json(receipt)
    receipt_path = tmp_path / "hardware.accepted.json"
    receipt_path.write_bytes(raw)
    invocation: dict[str, Any] = {}

    class Result:
        returncode = 0
        stdout = (
            b"GPU-d7ef849e-55f5-f33c-2812-9dc32b644b07, "
            b"NVIDIA GeForce RTX 5080 Laptop GPU, 16303, 12.0, 610.88\n"
        )
        stderr = b""

    def fake_run(command: tuple[str, ...], **kwargs: Any) -> Result:
        invocation["command"] = command
        invocation.update(kwargs)
        return Result()

    monkeypatch.setenv("FRIDAY_UNTRUSTED_EXPECTED_GPU", "wrong")
    monkeypatch.setattr(contract.subprocess, "run", fake_run)
    verified = contract.verify_live_hardware_runtime(receipt_path, hashlib.sha256(raw).hexdigest())

    assert hashlib.sha256(raw).hexdigest() == contract.EXPECTED_ACCEPTED_RECEIPT_SHA256
    assert verified.gpu_uuid == contract.EXPECTED_GPU["uuid"]
    assert invocation["command"] == contract.NVIDIA_SMI_COMMAND
    assert invocation["timeout"] == 5
    assert "FRIDAY_UNTRUSTED_EXPECTED_GPU" not in invocation["env"]
    assert invocation["stdin"] is contract.subprocess.DEVNULL


def test_hardware_receipt_fails_closed_on_status_hash_shape_and_gpu_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = importlib.import_module("hardware_runtime_contract")
    receipt = _accepted_hardware_receipt(contract)
    receipt_path = tmp_path / "hardware.json"

    def write(value: dict[str, Any]) -> str:
        raw = contract.canonical_receipt_json(value)
        receipt_path.write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()

    receipt["status"] = "observed_unaccepted"
    with pytest.raises(contract.HardwareRuntimeContractError):
        contract.verify_live_hardware_runtime(receipt_path, write(receipt))

    receipt["status"] = "accepted"
    expected_hash = write(receipt)
    with pytest.raises(contract.HardwareRuntimeContractError):
        contract.verify_live_hardware_runtime(receipt_path, "0" * 64)

    receipt["unexpected"] = True
    with pytest.raises(contract.HardwareRuntimeContractError):
        contract.verify_live_hardware_runtime(receipt_path, write(receipt))
    receipt.pop("unexpected")

    pretty = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode()
    receipt_path.write_bytes(pretty)
    with pytest.raises(contract.HardwareRuntimeContractError):
        contract.verify_live_hardware_runtime(receipt_path, hashlib.sha256(pretty).hexdigest())
    expected_hash = write(receipt)

    class DriftedResult:
        returncode = 0
        stdout = (
            b"GPU-d7ef849e-55f5-f33c-2812-9dc32b644b07, "
            b"NVIDIA GeForce RTX 5080 Laptop GPU, 16303, 12.0, 999.0\n"
        )
        stderr = b""

    monkeypatch.setattr(contract.subprocess, "run", lambda *_args, **_kwargs: DriftedResult())
    with pytest.raises(contract.HardwareRuntimeContractError):
        contract.verify_live_hardware_runtime(receipt_path, expected_hash)

    receipt_path.unlink()
    receipt_path.symlink_to(tmp_path / "missing-receipt.json")
    with pytest.raises(contract.HardwareRuntimeContractError):
        contract.verify_live_hardware_runtime(receipt_path, expected_hash)


def test_certification_profile_pins_ca_headers_and_exact_remote_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = importlib.import_module("endpoint_common")
    monkeypatch.setattr(common, "_ENDPOINT_IDENTITY", None)
    monkeypatch.setattr(common, "EXPECTED_MODEL", "friday-secondary-gptoss20b")
    ca_bytes = b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(ca_bytes)
    candidate = _candidate_runtime_profile()
    candidate["gateway_ca_certificate_sha256"] = hashlib.sha256(ca_bytes).hexdigest()
    manifest, _profile_id_path = _write_profile_fixture(tmp_path, candidate)
    alias = common.configure_expected_model(manifest, ca_file)
    assert alias == candidate["served_model_alias"]
    assert common.configured_profile_context_tokens() == candidate["context_tokens"]
    assert common.configured_profile_mem_fraction_static() == candidate["mem_fraction_static"]
    completion_body = {
        "model": alias,
        "choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}],
        "usage": {},
    }
    assert common.parse_completion(completion_body, latency_sec=0.1).content == "ready"
    completion_body["model"] = "friday-secondary-gptoss20b"
    with pytest.raises(common.EndpointError):
        common.parse_completion(completion_body, latency_sec=0.1)
    wrong_ca = tmp_path / "wrong-ca.crt"
    wrong_ca.write_bytes(ca_bytes + b"extra")
    with pytest.raises(common.EndpointError):
        common.configure_expected_model(manifest, wrong_ca)

    profile_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()

    class Headers:
        def __init__(self, *, duplicate: bool = False) -> None:
            self.duplicate = duplicate

        def get_all(self, name: str) -> list[str]:
            if name == "X-Friday-Secondary-Profile-Id":
                rows = [str(candidate["profile_id"])]
                return rows * 2 if self.duplicate else rows
            return [profile_sha256]

    class Response:
        status = 200

        def __init__(self, body: bytes, *, duplicate: bool = False) -> None:
            self.body = body
            self.headers = Headers(duplicate=duplicate)

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return self.body

    monkeypatch.setattr(common, "build_tls_context", lambda *_args: object())
    responses = iter(
        [
            Response(manifest.read_bytes() + b"stale"),
            Response(manifest.read_bytes(), duplicate=True),
            Response(manifest.read_bytes()),
        ]
    )
    monkeypatch.setattr(common.urllib.request, "urlopen", lambda *_args, **_kwargs: next(responses))
    arguments = {
        "api_key": "a" * 64,
        "timeout_sec": 1.0,
        "ca_file": ca_file,
    }
    with pytest.raises(common.EndpointError):
        common.verify_remote_profile_epoch("http://127.0.0.1:30000/v1", **arguments)
    with pytest.raises(common.EndpointError):
        common.verify_remote_profile_epoch("https://192.168.1.35:8443/v1/", **arguments)
    with pytest.raises(common.EndpointError):
        common.verify_remote_profile_epoch(
            "https://192.168.1.35:8443/v1",
            **{**arguments, "ca_file": wrong_ca},
        )
    with pytest.raises(common.EndpointError):
        common.verify_remote_profile_epoch("https://192.168.1.35:8443/v1", **arguments)
    with pytest.raises(common.EndpointError):
        common.verify_remote_profile_epoch("https://192.168.1.35:8443/v1", **arguments)
    common.verify_remote_profile_epoch("https://192.168.1.35:8443/v1", **arguments)


def test_engine_and_gateway_mount_the_identical_profile_bytes() -> None:
    compose = _compose()
    engine = compose["services"]["sglang"]
    gateway = compose["services"]["gateway"]

    def profile_sources(service: dict[str, Any]) -> dict[str, str]:
        return {
            row["target"]: row["source"]
            for row in service["volumes"]
            if row.get("target") in {"/run/friday-profile/accepted.json", "/run/friday-profile/id"}
        }

    assert (
        profile_sources(engine)
        == profile_sources(gateway)
        == {
            "/run/friday-profile/accepted.json": (
                "${FRIDAY_SECONDARY_PROFILE_MANIFEST_PATH:?accepted runtime profile manifest is required}"
            ),
            "/run/friday-profile/id": (
                "${FRIDAY_SECONDARY_PROFILE_ID_PATH:?accepted runtime profile id file is required}"
            ),
        }
    )


def test_capacity_and_soak_minimums_are_not_decorative() -> None:
    tuner = importlib.import_module("tune_context")
    soak = importlib.import_module("soak")
    assert tuner._parse_candidates("4096,8192,12288") == (4096, 8192, 12288)
    with pytest.raises(argparse.ArgumentTypeError):
        tuner._parse_candidates("8192,4096")
    assert soak._duration("1800") == 1800
    assert soak._minimum_requests("100") == 100
    with pytest.raises(argparse.ArgumentTypeError):
        soak._duration("60")
    with pytest.raises(argparse.ArgumentTypeError):
        soak._minimum_requests("10")


def test_capacity_accepts_profile_memory_grid_and_reserves_generation_tokens() -> None:
    tuner = importlib.import_module("tune_context")
    args = tuner._parser().parse_args(
        [
            "--base-url",
            "https://192.168.1.35:8443/v1",
            "--api-key-file",
            "key",
            "--ca-file",
            "ca.crt",
            "--profile-manifest",
            "candidate.json",
            "--output",
            "capacity.json",
            "--candidates",
            "4096",
            "--mem-fraction-static",
            "0.97",
        ]
    )
    assert args.mem_fraction_static == 0.97
    messages = tuner._context_prompt(4096, 320)
    body = messages[-1]["content"]
    assert body.count("probe ") == 4096 - 320 - tuner._PROTOCOL_TOKEN_RESERVE
    checks = tuner._usage_checks(
        context_tokens=4096,
        generation_tokens=320,
        prompt_tokens=3400,
        completion_tokens=256,
    )
    assert all(checks.values())
    overcommitted = tuner._usage_checks(
        context_tokens=4096,
        generation_tokens=320,
        prompt_tokens=3800,
        completion_tokens=256,
    )
    assert overcommitted["generation_reserve_met"] is False
    assert overcommitted["prompt_near_limit"] is False


def test_capacity_and_soak_require_the_exact_profile_endpoint_and_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = importlib.import_module("endpoint_common")
    tuner = importlib.import_module("tune_context")
    soak = importlib.import_module("soak")
    monkeypatch.setattr(common, "_ENDPOINT_IDENTITY", None)
    monkeypatch.setattr(common, "EXPECTED_MODEL", "friday-secondary-gptoss20b")
    ca_bytes = b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n"
    ca_file = tmp_path / "ca.crt"
    ca_file.write_bytes(ca_bytes)
    wrong_ca = tmp_path / "wrong-ca.crt"
    wrong_ca.write_bytes(ca_bytes + b"changed")
    candidate = _candidate_runtime_profile()
    candidate["gateway_ca_certificate_sha256"] = hashlib.sha256(ca_bytes).hexdigest()
    manifest, _profile_id_path = _write_profile_fixture(tmp_path, candidate)
    common.configure_expected_model(manifest, ca_file)
    capacity_arguments = {
        "base_url": "https://192.168.1.35:8443/v1",
        "api_key": "a" * 64,
        "candidates": (4096,),
        "repeats": 3,
        "timeout_sec": 1.0,
        "generation_tokens": 320,
        "mem_fraction_static": 0.97,
        "ca_file": ca_file,
    }
    with pytest.raises(common.EndpointError, match="exact profile context"):
        tuner.run_ladder(**{**capacity_arguments, "candidates": (8192,)})
    with pytest.raises(common.EndpointError, match="exact profile value"):
        tuner.run_ladder(**{**capacity_arguments, "mem_fraction_static": 0.96})
    with pytest.raises(common.EndpointError, match="HTTPS endpoint identity"):
        tuner.run_ladder(
            **{**capacity_arguments, "base_url": "http://127.0.0.1:30000/v1"}
        )
    with pytest.raises(common.EndpointError, match="HTTPS endpoint identity"):
        tuner.run_ladder(
            **{**capacity_arguments, "base_url": "https://192.168.1.36:8443/v1"}
        )
    with pytest.raises(common.EndpointError, match="private CA identity"):
        tuner.run_ladder(**{**capacity_arguments, "ca_file": wrong_ca})

    soak_arguments = {
        "base_url": "https://192.168.1.35:8443/v1",
        "api_key": "a" * 64,
        "duration_sec": 1800,
        "minimum_requests": 100,
        "timeout_sec": 1.0,
        "maximum_temperature_c": 87.0,
        "checkpoint": tmp_path / "soak.checkpoint.json",
        "ca_file": ca_file,
    }
    with pytest.raises(common.EndpointError, match="HTTPS endpoint identity"):
        soak.run_soak(**{**soak_arguments, "base_url": "http://127.0.0.1:30000/v1"})
    with pytest.raises(common.EndpointError, match="private CA identity"):
        soak.run_soak(**{**soak_arguments, "ca_file": wrong_ca})

    def reject_epoch(*_args: object, **_kwargs: object) -> None:
        raise common.EndpointError("remote epoch witness required")

    monkeypatch.setattr(tuner, "verify_remote_profile_epoch", reject_epoch)
    monkeypatch.setattr(soak, "verify_remote_profile_epoch", reject_epoch)
    with pytest.raises(common.EndpointError, match="remote epoch witness required"):
        tuner.run_ladder(**capacity_arguments)
    with pytest.raises(common.EndpointError, match="remote epoch witness required"):
        soak.run_soak(**soak_arguments)


def test_gpu_telemetry_is_bound_to_exact_laptop_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry = importlib.import_module("gpu_telemetry")

    class Result:
        stdout = (
            "GPU-d7ef849e-55f5-f33c-2812-9dc32b644b07, "
            "NVIDIA GeForce RTX 5080 Laptop GPU, 16303, 12000, 4303, 70, 100, 80\n"
        )

    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> Result:
        commands.append(command)
        return Result()

    monkeypatch.setattr(telemetry.subprocess, "run", run)
    sample = telemetry.sample_gpu()
    assert commands[0][1] == "--id=GPU-d7ef849e-55f5-f33c-2812-9dc32b644b07"
    assert sample.uuid == telemetry.EXPECTED_GPU_UUID
    assert telemetry.sample_summary([sample])["total_mib"] == 16303

    drifted_identities = (
        ("GPU-other", telemetry.EXPECTED_GPU_NAME, 16303),
        (telemetry.EXPECTED_GPU_UUID, "NVIDIA GeForce RTX 5090", 16303),
        (telemetry.EXPECTED_GPU_UUID, telemetry.EXPECTED_GPU_NAME, 24564),
    )
    for uuid, name, total_mib in drifted_identities:
        drifted = telemetry.GpuSample(uuid, name, total_mib, 12000, 4303, 70, 100, 80)
        with pytest.raises(telemetry.GpuTelemetryError, match="accepted laptop receipt"):
            telemetry.sample_summary([drifted])
