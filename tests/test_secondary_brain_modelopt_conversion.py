"""Closed contracts for the offline ModelOpt conversion operator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "deploy" / "secondary-brain" / "windows-sglang"
SCRIPTS = BUNDLE / "scripts"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool() -> ModuleType:
    return _load_module("modelopt_conversion_tool_test", SCRIPTS / "modelopt_conversion_tool.py")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_conversion_recipe_is_one_exact_offline_non_low_memory_command(tool: ModuleType) -> None:
    command = tool.conversion_command()
    assert command == (
        "/artifacts/hf_ptq.py",
        "--pyt_ckpt_path",
        "/source/snapshot",
        "--export_path",
        "/output/candidate",
        "--qformat",
        "nvfp4_mlp_only",
        "--cast_mxfp4_to_nvfp4",
        "--kv_cache_qformat",
        "none",
        "--dataset",
        "/calibration/calibration.jsonl",
        "--calib_size",
        "256",
        "--calib_seq",
        "512",
        "--batch_size",
        "1",
        "--use_seq_device_map",
        "--gpu_max_mem_percentage",
        "0.70",
        "--skip_generate",
    )
    assert "--low_memory_mode" not in command
    assert tool.SOURCE_REVISION == "6cee5e81ee83917806bbde320786a8fb61efebee"
    assert tool.MODELOPT_COMMIT == "ec87a82927d003986d44fb7f4fa8b3d10c31b095"
    assert tool.PREFERRED_CONVERSION_IMAGE.endswith(
        "@sha256:7202108ab373557e0562f78ef3c0f65bdc70e18cc0b040c8d6805a5cde897a0d"
    )


def test_artifact_and_calibration_inputs_are_content_addressed(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    payloads = {"a.whl": b"wheel", "hf_ptq.py": b"official script"}
    for name, payload in payloads.items():
        (artifacts / name).write_bytes(payload)
    monkeypatch.setattr(tool, "ARTIFACT_DIRECTORY", artifacts)
    monkeypatch.setattr(
        tool,
        "ARTIFACTS",
        {name: hashlib.sha256(payload).hexdigest() for name, payload in payloads.items()},
    )
    assert tool.validate_artifacts() == dict(sorted(tool.ARTIFACTS.items()))
    (artifacts / "hf_ptq.py").write_bytes(b"tampered")
    with pytest.raises(tool.ConversionError):
        tool.validate_artifacts()

    generator = _load_module("generate_calibration_conversion_test", SCRIPTS / "generate_calibration.py")
    corpus = tmp_path / "calibration.jsonl"
    manifest = tmp_path / "calibration.observed.json"
    generator.generate(corpus, manifest)
    monkeypatch.setattr(tool, "CALIBRATION_FILE", corpus)
    monkeypatch.setattr(tool, "CALIBRATION_MANIFEST", manifest)
    assert tool.validate_calibration()["sha256"] == tool.CALIBRATION_SHA256
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_value["rows"] = 255
    manifest.chmod(0o644)
    _write_json(manifest, manifest_value)
    with pytest.raises(tool.ConversionError):
        tool.validate_calibration()


def test_source_volume_manifest_and_snapshot_must_match_exactly(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    snapshot = source / "snapshot"
    snapshot.mkdir(parents=True)
    payloads = {"config.json": b"{}", "model.safetensors": b"weights"}
    rows: dict[str, dict[str, Any]] = {}
    for name, payload in payloads.items():
        (snapshot / name).write_bytes(payload)
        rows[name] = {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
    total_bytes = sum(len(payload) for payload in payloads.values())
    manifest = {
        "schema": tool.SOURCE_SCHEMA,
        "status": "verified",
        "repository": tool.SOURCE_REPOSITORY,
        "revision": tool.SOURCE_REVISION,
        "root_only": True,
        "excluded_prefixes": tool.SOURCE_EXCLUDED_PREFIXES,
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "files": rows,
    }
    _write_json(source / "source-manifest.json", manifest)
    monkeypatch.setattr(tool, "SOURCE_ROOT", source)
    monkeypatch.setattr(tool, "SOURCE_SNAPSHOT", snapshot)
    monkeypatch.setattr(tool, "SOURCE_MANIFEST", source / "source-manifest.json")
    monkeypatch.setattr(tool, "SOURCE_FILE_COUNT", len(rows))
    monkeypatch.setattr(tool, "SOURCE_TOTAL_BYTES", total_bytes)
    monkeypatch.setattr(
        tool,
        "SOURCE_FILES",
        {name: (row["bytes"], row["sha256"]) for name, row in rows.items()},
    )
    monkeypatch.setattr(
        tool,
        "SOURCE_MANIFEST_RAW_SHA256",
        hashlib.sha256((source / "source-manifest.json").read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(tool, "SOURCE_MANIFEST_SEMANTIC_SHA256", tool._canonical_sha256(manifest))

    observed, semantic_sha256 = tool.validate_source()
    assert observed == manifest
    assert len(semantic_sha256) == 64
    (snapshot / "extra.txt").write_text("unexpected", encoding="utf-8")
    with pytest.raises(tool.ConversionError):
        tool.validate_source()


def test_output_manifest_is_observed_only_and_verify_allows_only_status_change(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "output"
    candidate = output / "candidate"
    candidate.mkdir(parents=True)
    _write_json(
        candidate / "config.json",
        {
            "model_type": "gpt_oss",
            "architectures": ["GptOssForCausalLM"],
            "quantization_config": {
                "quant_method": "modelopt",
                "quant_algo": "NVFP4",
                "producer": {"name": "modelopt", "version": "0.45.0"},
            },
        },
    )
    _write_json(
        candidate / "hf_quant_config.json",
        {
            "producer": {"name": "modelopt", "version": "0.45.0"},
            "quantization": {"quant_algo": "NVFP4", "kv_cache_quant_algo": None},
        },
    )
    shard = "model-00001-of-00001.safetensors"
    (candidate / shard).write_bytes(b"bounded fake safetensors fixture")
    _write_json(candidate / "model.safetensors.index.json", {"weight_map": {"x": shard}})
    _write_json(candidate / "tokenizer.json", {"version": "1"})
    _write_json(candidate / "tokenizer_config.json", {"model_max_length": 131072})

    monkeypatch.setattr(tool, "OUTPUT_VOLUME", output)
    monkeypatch.setattr(tool, "OUTPUT_CANDIDATE", candidate)
    monkeypatch.setattr(tool, "MIN_OUTPUT_BYTES", 1)
    monkeypatch.setattr(tool, "MAX_OUTPUT_BYTES", 1_000_000)
    monkeypatch.setattr(tool, "validate_artifacts", lambda: {"artifact": "a" * 64})
    monkeypatch.setattr(tool, "validate_source", lambda: ({}, "b" * 64))
    monkeypatch.setattr(tool, "validate_calibration", lambda: {})

    observed = tool.observed_output_manifest(conversion_image=tool.PREFERRED_CONVERSION_IMAGE)
    assert observed["status"] == "observed_unaccepted"
    assert observed["recipe"]["low_memory_mode"] is False
    assert observed["metadata"]["kv_cache_quant_algo"] == "none"
    accepted = dict(observed)
    accepted["status"] = "accepted"
    accepted_path = tmp_path / "accepted.json"
    _write_json(accepted_path, accepted)
    receipt = tool.verify_accepted_output(accepted_path, conversion_image=tool.PREFERRED_CONVERSION_IMAGE)
    assert receipt["status"] == "passed"
    assert receipt["accepted_manifest_file_sha256"] == hashlib.sha256(accepted_path.read_bytes()).hexdigest()

    (candidate / "tokenizer.json").write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(tool.ConversionError):
        tool.verify_accepted_output(accepted_path, conversion_image=tool.PREFERRED_CONVERSION_IMAGE)


def test_runtime_verifier_hashes_profile_bound_manifest_and_every_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    verifier = _load_module(
        "converted_model_manifest_test", BUNDLE / "runtime" / "converted_model_manifest.py"
    )
    snapshot = tmp_path / "candidate"
    snapshot.mkdir()
    (snapshot / "config.json").write_bytes(b"config")
    (snapshot / "model.safetensors").write_bytes(b"weights")
    rows = [
        {
            "path": path.name,
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(snapshot.iterdir())
    ]
    total_bytes = sum(row["size"] for row in rows)
    monkeypatch.setattr(verifier, "MIN_OUTPUT_BYTES", 1)
    monkeypatch.setattr(verifier, "MAX_OUTPUT_BYTES", 1_000_000)
    manifest = {
        "schema": verifier.SCHEMA,
        "status": "accepted",
        "source": {
            "repository": verifier.SOURCE_REPOSITORY,
            "revision": verifier.SOURCE_REVISION,
            "manifest_semantic_sha256": verifier.SOURCE_MANIFEST_SEMANTIC_SHA256,
        },
        "converter": {
            "image": verifier.PREFERRED_CONVERTER_IMAGE,
            "accepted_converter_manifest_sha256": None,
            "modelopt_commit": verifier.MODELOPT_COMMIT,
            "artifacts": verifier.ARTIFACTS,
            "package_versions": verifier.PACKAGE_VERSIONS,
        },
        "recipe": {
            "qformat": "nvfp4_mlp_only",
            "cast_mxfp4_to_nvfp4": True,
            "kv_cache_qformat": "none",
            "calibration_sha256": verifier.CALIBRATION_SHA256,
            "calib_size": 256,
            "calib_seq": 512,
            "batch_size": 1,
            "use_seq_device_map": True,
            "gpu_max_mem_percentage": 0.70,
            "skip_generate": True,
            "low_memory_mode": False,
            "network": "none",
        },
        "output_directory": "candidate",
        "metadata": {
            "architecture": "GptOssForCausalLM",
            "model_type": "gpt_oss",
            "modelopt_version": "0.45.0",
            "quant_algo": "NVFP4",
            "kv_cache_quant_algo": "none",
            "safetensors_shards": 1,
            "weight_map_entries": 1,
        },
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "files": rows,
        "note": verifier._NOTE,
    }
    manifest_path = tmp_path / "accepted-conversion.json"
    manifest_path.write_bytes(verifier._canonical_json(manifest))
    manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    receipt = verifier.verify_converted_model_snapshot(snapshot, manifest_path, manifest_sha256)
    assert receipt.manifest_sha256 == manifest_sha256
    assert receipt.file_count == 2
    assert receipt.total_bytes == total_bytes

    (snapshot / "model.safetensors").write_bytes(b"changed")
    with pytest.raises(verifier.ConvertedModelManifestError):
        verifier.verify_converted_model_snapshot(snapshot, manifest_path, manifest_sha256)


def test_powershell_operator_has_no_implicit_authority_or_mutable_resolution() -> None:
    source = (SCRIPTS / "convert-modelopt-nvfp4.ps1").read_text(encoding="utf-8")
    assert "[switch]$Apply" in source
    assert "if (-not $Apply -or $Mode -eq 'Plan')" in source
    assert "--network', 'none'" in source
    assert "--pull', 'never'" in source
    assert "docker pull" not in source
    assert "docker volume create" not in source
    assert "friday-secondary-source-gptoss20b" in source
    assert "friday-secondary-modelopt-conversion-output" in source
    assert "Convert requires OutputManifest" in source
    assert "conversion refuses a non-empty output volume" in (
        SCRIPTS / "modelopt_conversion_tool.py"
    ).read_text(encoding="utf-8")
    assert "TokenFile" not in source and "Password" not in source and "Bearer" not in source
    assert "nvcr.io/nvidia/tensorrt-llm/release@sha256:7202108a" in source
    assert "AcceptedConverterManifest" in source
    assert "pip_check" in source


def test_alternative_converter_manifest_is_an_explicit_nonaccepted_template() -> None:
    manifest = json.loads((BUNDLE / "modelopt-converter-manifest.example.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "friday.secondary-modelopt-converter-image.v1"
    assert manifest["status"] == "template_not_accepted"
    assert manifest["base_image"].endswith(
        "@sha256:7a038aa31356fdd1a5b591fc756397bc2e9eb5ac91442c407f55cd2ae8bee738"
    )
    assert manifest["image_id"] == ("sha256:b801dc95ca304701242aeeaaeaf64332d67134ba8e56c8c0e74ab2dc77569c7a")
    assert manifest["pip_check"]["status"] == "passed"
    assert manifest["package_freeze"]["lines"] == 313
    assert len(manifest["wheelhouse"]) == 7
    assert manifest["removed_distributions"] == ["sglang", "nixl"]
    assert manifest["build_network"] == "none"
    assert manifest["packages"] == {
        "accelerate": "1.12.0",
        "nvidia-modelopt": "0.45.0",
        "transformers": "5.9.0",
    }
