"""Static and unit contracts for the detachable Windows/SGLang node bundle."""

from __future__ import annotations

import argparse
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


@pytest.fixture(scope="module", autouse=True)
def _script_import_path() -> Iterator[None]:
    sys.path.insert(0, str(SCRIPTS))
    try:
        yield
    finally:
        sys.path.remove(str(SCRIPTS))


def _compose() -> dict[str, Any]:
    value = yaml.safe_load((BUNDLE / "compose.yml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_bundle_has_the_closed_operator_surface() -> None:
    required = {
        "README.md",
        "compose.yml",
        ".env.example",
        "gateway.conf.template",
        "model-manifest.example.json",
        "runtime-manifest.example.json",
        "scripts/preflight.ps1",
        "scripts/install-openssh.ps1",
        "scripts/firewall.ps1",
        "scripts/provision-secrets.ps1",
        "scripts/populate-model-volume.ps1",
        "scripts/probe_endpoint.py",
        "scripts/tune_context.py",
        "scripts/soak.py",
    }
    assert required <= {path.relative_to(BUNDLE).as_posix() for path in BUNDLE.rglob("*") if path.is_file()}


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
    assert engine["entrypoint"] == ["/bin/bash", "-ceu"]
    assert gateway["entrypoint"] == ["/bin/sh", "-ceu"]
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
    assert engine["tmpfs"] == ["/tmp:size=2g,mode=1777", "/run:size=16m,mode=0755"]
    assert not any(volume.get("target") == "/var/run/docker.sock" for volume in volumes)


def test_gateway_uses_distinct_file_secrets_tls_and_a_closed_route_set() -> None:
    compose = _compose()
    engine = compose["services"]["sglang"]
    gateway = compose["services"]["gateway"]
    engine_mounts = {row["target"] for row in engine["volumes"] if row["type"] == "bind"}
    gateway_mounts = {row["target"] for row in gateway["volumes"] if row["type"] == "bind"}
    assert engine_mounts == {"/run/friday-secrets/sglang-api-key"}
    assert {
        "/run/friday-secrets/gateway-api-key",
        "/run/friday-secrets/sglang-api-key",
        "/run/friday-tls/ca.crt",
        "/run/friday-tls/server.crt",
        "/run/friday-tls/server.key",
    } <= gateway_mounts
    command = "\n".join(gateway["command"])
    assert 'test "$${gateway_key}" != "$${upstream_key}"' in command
    assert 'test "$${#gateway_key}" -eq 64' in command
    assert "exec nginx -c /tmp/gateway.conf" in command
    assert "environment" not in gateway

    policy = (BUNDLE / "gateway.conf.template").read_text(encoding="utf-8")
    assert "listen 8443 ssl;" in policy
    assert "access_log off;" in policy
    assert '"Bearer __GATEWAY_BEARER__" 1;' in policy
    assert 'Authorization "Bearer __SGLANG_BEARER__"' in policy
    assert "location = /v1/models" in policy
    assert "location = /v1/chat/completions" in policy
    assert "location /" in policy and "return 404;" in policy
    assert "proxy_pass http://friday_secondary_sglang" in policy


def test_examples_are_honest_nonaccepted_placeholders() -> None:
    model = json.loads((BUNDLE / "model-manifest.example.json").read_text(encoding="utf-8"))
    runtime = json.loads((BUNDLE / "runtime-manifest.example.json").read_text(encoding="utf-8"))
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
    env = (BUNDLE / ".env.example").read_text(encoding="utf-8")
    assert "REPLACE_WITH_lmsysorg_sglang_AT_sha256_DIGEST" in env
    assert "FRIDAY_SECONDARY_GATEWAY_IMAGE" not in env
    assert "latest" not in env.casefold()


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
    assert "--network none" in population
    assert "docker pull" not in population
    assert "New-RandomHex 32" in provisioning
    assert "distinct_bearers_verified = $true" in provisioning
    assert "IP Address:192\\.168\\.1\\.35" in provisioning
    assert "secret_values_reported = $false" in provisioning
    assert "/inheritance:r /T /C" in provisioning
    preflight = (SCRIPTS / "preflight.ps1").read_text(encoding="utf-8")
    assert "[switch]$InspectGatewayImage" in preflight
    assert "NGINX_VERSION=1.31.3" in preflight
    assert "Config.User -cne '101'" in preflight
    assert 'test "$(id -u)" = 101' in preflight
    assert "nginx version: nginx/1.31.3" in preflight
    assert "inventory_incomplete" in preflight


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
