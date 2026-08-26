"""Static identity gates for the isolated Qwen3.8 abliterated bundle."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_HANDOFF = _ROOT / "handoffs" / "SGLang-Qwen38-Abliterated-V12-Attested"
_REMOTE = _HANDOFF / "remote"
_TRANSPORT = _HANDOFF / "transport"
_MODEL_SHA = "e5fa0d366c3bcf6546f9f3d0cb418b8e2530e2701a5a1506367f88fd08d1d1a4"
_LAUNCH_SHA = "ed18fc43f7a865dc0d01c568f22200fb71eebdcc2cef354f859860c966f3a19a"
_ENGINE_ID = "sha256:62ae2bb57a54a1dfcc33c05cdfd200cc69705ac94ad503cd4ec00a409804acaf"
_PROXY_ID = "sha256:2227ed08bc4360eea50b1bba31b0f07d5652ba63344a0ab0f135aec63fb680de"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _semantic_json_sha256(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _sha_manifest(path: Path) -> dict[str, str]:
    records: dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)", line)
        assert match is not None
        digest, name = match.groups()
        assert name not in records
        records[name] = digest
    return records


def test_abliterated_model_and_launch_manifests_are_exact() -> None:
    model_path = _REMOTE / "qwen38-model-manifest.v1.json"
    launch_path = _REMOTE / "launch-manifest.v1.json"
    model = json.loads(model_path.read_text(encoding="utf-8"))
    launch = json.loads(launch_path.read_text(encoding="utf-8"))

    assert _semantic_json_sha256(model_path) == _MODEL_SHA
    assert _semantic_json_sha256(launch_path) == _LAUNCH_SHA
    assert model["model_repository"] == "Vtuber-plan/Huihui-Qwen3.8-27B-abliterated-NVFP4"
    assert model["model_revision"] == "43aa7ff5eef05ab50a3bfa6aca581085312c7a04"
    assert model["model_quantization"] == "W4A4_NVFP4_FP8_KV"
    assert model["snapshot_directory"] == "qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5"
    assert model["file_count"] == len(model["files"]) == 18
    assert model["total_bytes"] == sum(row["size"] for row in model["files"]) == 20_613_780_167
    assert len({row["path"] for row in model["files"]}) == 18
    assert launch["profile_id"] == "qwen38-27b-nvfp4-sglang:dispatcher:v12.15"
    assert launch["served_model_alias"] == "dispatcher"
    assert launch["model_mount_path"].endswith(model["snapshot_directory"])
    assert launch["arguments"][launch["arguments"].index("--context-length") + 1] == "40960"


def test_bundle_binds_new_candidate_and_preserved_stable_separately() -> None:
    common = (_REMOTE / "AttestedBundle.Common.ps1").read_text(encoding="utf-8")
    switch = (_REMOTE / "Switch-Qwen38AbliteratedV12Attested.ps1").read_text(encoding="utf-8")
    receipt = json.loads((_REMOTE / "build-attestation.v1.json").read_text(encoding="utf-8"))

    for exact in (
        "ProfileId = 'qwen38-27b-nvfp4-sglang:dispatcher:v12.15'",
        "StableProfileId = 'qwen38-27b-nvfp4-sglang:dispatcher:v12.14'",
        f"ModelManifestSha256 = '{_MODEL_SHA}'",
        f"LaunchManifestSha256 = '{_LAUNCH_SHA}'",
        f"CandidateEngineImageId = '{_ENGINE_ID}'",
        f"CandidateProxyImageId = '{_PROXY_ID}'",
        "StableEngineName = 'jarvis-gpt-sglang-qwen38-v12-attested'",
        "StableProxyName = 'jarvis-gpt-sglang-qwen38-v12-attested-api'",
    ):
        assert exact in common
    assert receipt["engine"]["image_id"] == _ENGINE_ID
    assert receipt["proxy"]["image_id"] == _PROXY_ID
    assert receipt["engine"]["model_snapshot_manifest_sha256"] == _MODEL_SHA
    assert receipt["engine"]["launch_manifest_sha256"] == _LAUNCH_SHA
    assert "quick_smokes = @('health', 'models', 'chat', 'tool')" in switch
    assert switch.count("extended_acceptance_run = $false") == 2
    assert "if ($false) {" in switch
    assert switch.index("if ($false) {") < switch.index("$stage = 'soak_settle'")
    assert "armed_restart_policy = 'unless-stopped'" in switch
    assert "if ($mutationStarted -and -not $switchSucceeded)" in switch
    assert "Restore-Stable" in switch


def test_compose_is_isolated_read_only_and_restart_resilient() -> None:
    compose_path = _REMOTE / "docker-compose.attested.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    common = (_REMOTE / "AttestedBundle.Common.ps1").read_text(encoding="utf-8")
    services = compose["services"]

    assert set(services) == {"engine", "proxy"}
    assert all(service["restart"] == "unless-stopped" for service in services.values())
    assert "ports" not in services["engine"]
    assert "ports" not in services["proxy"]
    assert services["engine"]["container_name"] == "jarvis-gpt-sglang-qwen38-abliterated-v12-attested"
    assert services["proxy"]["container_name"].endswith("-abliterated-v12-attested-api")
    assert any(
        volume.get("source") == "model-snapshot" and volume.get("read_only") is True
        for volume in services["engine"]["volumes"]
    )
    assert compose["volumes"]["model-snapshot"] == {
        "name": "jarvis-gpt-qwen38-abliterated-v12-attested-model-e5fa0d366c3bcf65",
        "external": True,
    }
    assert f"ComposeSha256 = '{_sha256(compose_path)}'" in common


def test_source_sha_manifests_and_create_new_transport_are_exact() -> None:
    core = _sha_manifest(_REMOTE / "CORE-SHA256SUMS")
    orchestration = _sha_manifest(_REMOTE / "ORCHESTRATION-SHA256SUMS")
    transport = _sha_manifest(_TRANSPORT / "TRANSPORT-FILES.v1")
    remote_files = {path.name for path in _REMOTE.iterdir() if path.is_file()}

    for records in (core, orchestration, transport):
        for name, digest in records.items():
            assert _sha256(_REMOTE / name) == digest
    assert set(transport) == remote_files
    assert "build-verification.v1.json" not in remote_files
    assert not (_REMOTE / "__pycache__").exists()

    wrapper = (_HANDOFF / "Sync-Qwen38AbliteratedV12AttestedBundle.sh").read_text(encoding="utf-8")
    applier_path = _TRANSPORT / "Apply-Qwen38AbliteratedV12AttestedBundle.ps1"
    applier = applier_path.read_text(encoding="utf-8")
    assert f"expected_manifest_sha256='{_sha256(_TRANSPORT / 'TRANSPORT-FILES.v1')}'" in wrapper
    assert f"expected_applier_sha256='{_sha256(applier_path)}'" in wrapper
    assert "network_connection=false mutation_authorized=false" in wrapper
    assert "create-new transport refuses replacement" in applier
    assert "[IO.Directory]::Move($temporaryRoot, $liveRoot)" in applier
    assert "[IO.FileShare]::None" in applier
    assert "docker " not in applier.lower()


def test_candidate_facing_docs_contain_no_stale_primary_identity() -> None:
    docs = "\n".join(
        (_REMOTE / name).read_text(encoding="utf-8")
        for name in ("README.md", "ORCHESTRATION.md", "deployment.lock.env.example")
    )
    for stale in (
        "a2genesis/Qwen3.8-27B-NVFP4",
        "bfd9b31207712e0850eec9da32261e8c5ee16af7",
        "sha256:4a38144134d84d6f78c1844314f209c48ef69c4bd8bf7da1e5c400f9abda6f26",
        "sha256:37ae13a39a5d8a0780b0b0f226065753c0d929c31956be27f7f375f79cdef750",
        "21,952,105,742",
        "exact 17-file",
        "SGLang-Qwen38-V12-Attested",
    ):
        assert stale not in docs
