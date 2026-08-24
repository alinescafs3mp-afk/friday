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
        "runtime/render_gateway_secure.sh",
        "runtime-compat/.dockerignore",
        "runtime-compat/Dockerfile",
        "runtime-compat/apply_compat.py",
        "runtime-compat/compat.patch",
        "scripts/accept-hardware-runtime-receipt.ps1",
        "scripts/preflight.ps1",
        "scripts/install-openssh.ps1",
        "scripts/firewall.ps1",
        "scripts/provision-secrets.ps1",
        "scripts/populate-model-volume.ps1",
        "scripts/generate_calibration.py",
        "scripts/probe_endpoint.py",
        "scripts/quality_battery.py",
        "scripts/failure_battery.py",
        "scripts/tune_context.py",
        "scripts/soak.py",
        "scripts/runtime_profile_operator.py",
    }
    assert required <= {path.relative_to(BUNDLE).as_posix() for path in BUNDLE.rglob("*") if path.is_file()}


def test_runtime_compatibility_image_is_exact_offline_and_minimal() -> None:
    compat = BUNDLE / "runtime-compat"
    expected_hashes = {
        ".dockerignore": "2bcf7a28b6fd7575d1326a3f923e8750e1c1bcb38205b72c7d7e2a51fb898013",
        "Dockerfile": "4be190b91e49176951055aa4c2a8068b08067c32e7965d980c97511483a2f547",
        "apply_compat.py": "67182abfc5104facbf870af7ebd2a108445b2ace7e3da9194b9586ffa8b83726",
        "compat.patch": "0408f38a639c4a477e9ba14dacb488cb3d120fda0f4019b280fc999fa5fe0b5e",
    }
    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in compat.iterdir()
        if path.is_file()
    } == expected_hashes
    dockerfile = (compat / "Dockerfile").read_text(encoding="utf-8")
    assert (
        "FROM lmsysorg/sglang@sha256:7a038aa31356fdd1a5b591fc756397bc2e9eb5ac91442c407f55cd2ae8bee738"
        in dockerfile
    )
    assert "apt" not in dockerfile and "pip install" not in dockerfile and "curl" not in dockerfile


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
    model = next(row for row in volumes if row["target"] == "/models/gpt-oss-20b-nvfp4-modelopt")
    assert model == {
        "type": "volume",
        "source": "model-snapshot",
        "target": "/models/gpt-oss-20b-nvfp4-modelopt",
        "read_only": True,
    }
    accepted_manifest = next(row for row in volumes if row["target"] == "/run/friday-model/accepted.json")
    assert accepted_manifest == {
        "type": "bind",
        "source": (
            "${FRIDAY_SECONDARY_CONVERTED_MODEL_MANIFEST_PATH:?accepted converted model manifest is required}"
        ),
        "target": "/run/friday-model/accepted.json",
        "read_only": True,
        "bind": {"create_host_path": False},
    }
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
        "/run/friday-bootstrap/converted_model_manifest.py",
        "/run/friday-bootstrap/hardware_runtime_contract.py",
        "/run/friday-hardware/accepted.json",
        "/run/friday-model/accepted.json",
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
    assert 'value["quantization"] != "modelopt_fp4"' in profile_contract
    assert launcher.index("profile = load_launch_profile(") < launcher.index(
        "from sglang.launch_server import run_server"
    )
    assert launcher.index("verify_converted_model_snapshot(") < launcher.index(
        "from sglang.launch_server import run_server"
    )
    assert launcher.index("verify_live_hardware_runtime(") < launcher.index(
        "verify_converted_model_snapshot("
    )
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


def test_examples_are_honest_nonaccepted_placeholders() -> None:
    model = json.loads((BUNDLE / "model-manifest.example.json").read_text(encoding="utf-8"))
    runtime = json.loads((BUNDLE / "runtime-manifest.example.json").read_text(encoding="utf-8"))
    hardware = json.loads((BUNDLE / "hardware-runtime-receipt.example.json").read_text(encoding="utf-8"))
    assert model["status"] == "template_not_accepted"
    assert model["files"] == []
    assert model["model_revision"] == "fb9848e169d5b38cbc00ecf3383283ea1fc33a21"
    assert runtime["status"] == "template_not_accepted"
    assert runtime["gateway_expected_version"] == "1.31.3"
    assert runtime["gateway_expected_user"] == "101"
    assert runtime["gateway_expected_platform_manifest_digest"] == (
        "sha256:8d764dd92e0b48d0ca94887dc0fe1df6dffc5200b25b2efcc2deb7ffb61d714c"
    )
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
    assert "REPLACE_WITH_lmsysorg_sglang_AT_sha256_DIGEST" in env
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
    assert "'6', 'TCP', '256', 'Any'" in firewall
    assert "--network none" in population
    assert "docker pull" not in population
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
    assert "NGINX_VERSION=1.31.3" in preflight
    assert "Config.User -cne '101'" in preflight
    assert 'test "$(id -u)" = 101' in preflight
    assert "nginx version: nginx/1.31.3" in preflight
    assert "inventory_incomplete" in preflight
    for expected in (
        "Майкрософт Windows 11 Pro",
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
    assert "$env:" not in preflight.casefold()
    assert "--pull', 'never" in preflight
    assert "[switch]$Apply" in promotion
    assert "if ($Apply)" in promotion
    assert "[IO.FileMode]::CreateNew" in promotion
    assert "observed_unaccepted" in promotion
    assert "overwritten = $false" in promotion
    assert "$env:" not in promotion.casefold()


def test_missing_model_volume_is_a_normal_powershell5_discovery_state() -> None:
    source = (SCRIPTS / "populate-model-volume.ps1").read_text(encoding="utf-8")
    function_start = source.index("function Test-DockerVolumeExists")
    function_end = source.index("\n}\n\nif ($Mode -eq 'Discover')", function_start) + 2
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


def test_model_volume_manifest_requires_accepted_exact_file_set(tmp_path: Path) -> None:
    tool = importlib.import_module("model_volume_tool")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    content = b"bounded model fixture"
    (snapshot / "config.json").write_bytes(content)
    manifest = {
        "schema": tool.SCHEMA,
        "status": "accepted",
        "model_repository": tool.MODEL_REPOSITORY,
        "model_revision": tool.MODEL_REVISION,
        "snapshot_directory": tool.SNAPSHOT_DIRECTORY,
        "file_count": 1,
        "total_bytes": len(content),
        "files": [
            {
                "path": "config.json",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = tool.verify_manifest(snapshot, manifest_path)
    assert result["status"] == "passed"
    manifest["status"] = "template_not_accepted"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(tool.ModelVolumeError):
        tool.verify_manifest(snapshot, manifest_path)


def _candidate_runtime_profile() -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "friday.secondary-runtime-profile.v1",
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
        "converted_model_manifest_sha256": "b" * 64,
        "conversion_manifest_sha256": "c" * 64,
        "gateway_ca_certificate_sha256": "1" * 64,
        "runtime_image": "lmsysorg/sglang@sha256:" + "d" * 64,
        "runtime_source_revision": "e" * 40,
        "runtime_manifest_sha256": "f" * 64,
        "model_path": "/models/gpt-oss-20b-nvfp4-modelopt/candidate",
        "quantization": "modelopt_fp4",
        "kv_cache_dtype": "none",
        "attention_backend": "triton",
        "fp4_gemm_backend": "flashinfer_cutlass",
        "context_tokens": 8192,
        "max_total_tokens": 8192,
        "mem_fraction_static": "0.92",
        "max_running_requests": 1,
        "max_output_tokens": 2048,
        "chunked_prefill_size": 1024,
        "cuda_graph_max_bs": 1,
        "allowed_modes": ["assist", "shadow"],
        "allowed_workloads": ["extract"],
        "no_cpu_offload": True,
        "quality_evidence_sha256": "0" * 64,
        "capacity_evidence_sha256": "0" * 64,
        "soak_evidence_sha256": "0" * 64,
        "failure_evidence_sha256": "0" * 64,
    }
    binding = importlib.import_module("profile_contract").engine_binding_sha256(value)
    value["engine_binding_sha256"] = binding
    value["profile_id"] = f"gptoss20b-{binding}"
    value["served_model_alias"] = f"friday-secondary-{value['profile_id']}"
    return value


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
        actual_runtime_image="lmsysorg/sglang@sha256:" + "d" * 64,
    )
    arguments = profile.server_arguments("a" * 64)

    assert profile.manifest_sha256 == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert profile.model_path == "/models/gpt-oss-20b-nvfp4-modelopt/candidate"
    assert profile.converted_model_manifest_sha256 == "b" * 64
    assert profile.hardware_runtime_receipt_sha256 == (
        "0c1c9e6f54aa0004c3dfc89acd6904cfbb0f834d0988e971e34b9699b3d9031f"
    )
    assert arguments[arguments.index("--context-length") + 1] == "8192"
    assert arguments[arguments.index("--max-total-tokens") + 1] == "8192"
    assert arguments[arguments.index("--mem-fraction-static") + 1] == "0.92"
    assert arguments[arguments.index("--max-running-requests") + 1] == "1"
    assert "--kv-cache-dtype" not in arguments
    assert profile.runtime_image == "lmsysorg/sglang@sha256:" + "d" * 64


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("status", "accepted"),
        ("endpoint_base_url", "https://192.168.1.36:8443/v1"),
        ("served_model_alias", "wrong"),
        ("hardware_runtime_receipt_sha256", "3" * 64),
        ("runtime_image", "lmsysorg/sglang:latest"),
        ("context_tokens", 10_000),
        ("max_total_tokens", 12_288),
        ("max_running_requests", 2),
        ("mem_fraction_static", "0.99"),
        ("no_cpu_offload", False),
    ],
)
def test_shared_profile_contract_rejects_mutation(tmp_path: Path, key: str, value: Any) -> None:
    contract = importlib.import_module("profile_contract")
    candidate = copy.deepcopy(_candidate_runtime_profile())
    candidate[key] = value
    manifest, profile_id = _write_profile_fixture(tmp_path, candidate)
    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            manifest,
            profile_id,
            actual_runtime_image="lmsysorg/sglang@sha256:" + "d" * 64,
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
            actual_runtime_image="lmsysorg/sglang@sha256:" + "d" * 64,
        )

    profile_id_link = tmp_path / "profile-link.id"
    profile_id_link.symlink_to(profile_id)
    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            manifest,
            profile_id_link,
            actual_runtime_image="lmsysorg/sglang@sha256:" + "d" * 64,
        )

    oversized_id = tmp_path / "oversized.id"
    oversized_id.write_bytes(b"a" * 81)
    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            manifest,
            oversized_id,
            actual_runtime_image="lmsysorg/sglang@sha256:" + "d" * 64,
        )

    oversized_manifest = tmp_path / "oversized.json"
    oversized_manifest.write_bytes(b"{" + b" " * 65_536)
    with pytest.raises(contract.ProfileContractError):
        contract.load_launch_profile(
            oversized_manifest,
            profile_id,
            actual_runtime_image="lmsysorg/sglang@sha256:" + "d" * 64,
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


def test_internal_conversion_calibration_is_fixed_synthetic_and_content_addressed(
    tmp_path: Path,
) -> None:
    generator = importlib.import_module("generate_calibration")
    corpus = tmp_path / "friday-secondary.jsonl"
    manifest = tmp_path / "calibration.observed.json"

    report = generator.generate(corpus, manifest)

    assert report["schema"] == "friday.secondary-brain.calibration.v1"
    assert report["status"] == "observed_unaccepted"
    assert report["rows"] == 256
    assert report["synthetic_only"] is True
    assert report["operator_data_present"] is False
    assert report["bytes"] == corpus.stat().st_size
    assert report["sha256"] == hashlib.sha256(corpus.read_bytes()).hexdigest()
    assert json.loads(manifest.read_text(encoding="utf-8")) == report
    rows = [json.loads(line) for line in corpus.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 256
    assert all(set(row) == {"text"} and len(row["text"]) > 2_000 for row in rows)
    with pytest.raises(FileExistsError):
        generator.generate(corpus, tmp_path / "second-manifest.json")


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
