from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "deploy" / "secondary-brain" / "windows-sglang"
SCRIPTS = BUNDLE / "scripts"
RUNTIME = BUNDLE / "runtime"


@pytest.fixture(scope="module", autouse=True)
def _bundle_import_path() -> Iterator[None]:
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(RUNTIME))
    try:
        yield
    finally:
        sys.path.remove(str(RUNTIME))
        sys.path.remove(str(SCRIPTS))


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _write(path: Path, value: Any) -> Path:
    path.write_bytes(_canonical(value))
    return path


def _hardware() -> dict[str, Any]:
    value = json.loads((BUNDLE / "hardware-runtime-receipt.example.json").read_text(encoding="utf-8"))
    value["status"] = "accepted"
    return value


def _candidate_args(tmp_path: Path) -> argparse.Namespace:
    manifest_contract = importlib.import_module("converted_model_manifest")
    hardware = _write(tmp_path / "hardware.json", _hardware())
    conversion_value = json.loads(
        (BUNDLE / "modelopt-converter-manifest.example.json").read_text(encoding="utf-8")
    )
    conversion_value["status"] = "accepted"
    conversion = _write(tmp_path / "conversion.json", conversion_value)
    conversion_sha256 = hashlib.sha256(conversion.read_bytes()).hexdigest()
    converted = _write(
        tmp_path / "converted.json",
        {
            "schema": "friday.secondary-modelopt-conversion-output.v1",
            "status": "accepted",
            "source": {
                "repository": "openai/gpt-oss-20b",
                "revision": "6cee5e81ee83917806bbde320786a8fb61efebee",
                "manifest_semantic_sha256": manifest_contract.SOURCE_MANIFEST_SEMANTIC_SHA256,
            },
            "converter": {
                "image": manifest_contract.SEALED_ALTERNATIVE_IMAGE,
                "accepted_converter_manifest_sha256": conversion_sha256,
                "modelopt_commit": manifest_contract.MODELOPT_COMMIT,
                "artifacts": manifest_contract.ARTIFACTS,
                "package_versions": manifest_contract.PACKAGE_VERSIONS,
            },
            "recipe": {
                "qformat": "nvfp4_mlp_only",
                "cast_mxfp4_to_nvfp4": True,
                "kv_cache_qformat": "none",
                "calibration_sha256": manifest_contract.CALIBRATION_SHA256,
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
            "file_count": 1,
            "total_bytes": manifest_contract.MIN_OUTPUT_BYTES,
            "files": [
                {
                    "path": "model-00001-of-00001.safetensors",
                    "size": manifest_contract.MIN_OUTPUT_BYTES,
                    "sha256": "1" * 64,
                }
            ],
            "note": manifest_contract._NOTE,
        },
    )
    runtime_value = json.loads((BUNDLE / "runtime-manifest.example.json").read_text(encoding="utf-8"))
    runtime_value.update(
        {
            "status": "accepted",
            "image_ref": (
                "lmsysorg/sglang@sha256:7a038aa31356fdd1a5b591fc756397bc2e9eb5ac91442c407f55cd2ae8bee738"
            ),
            "image_id": "sha256:" + "2" * 64,
            "gateway_image_id": "sha256:" + "3" * 64,
            "sglang_version": "0.5.16",
            "sglang_git_revision": "a" * 40,
            "cuda_runtime_version": "13.0.1",
            "pytorch_version": "2.10.0a0",
            "flashinfer_version": "0.6.6",
            "sgl_kernel_version": "0.3.21",
            "nvidia_driver_version": "610.88",
            "gpu_name": "NVIDIA GeForce RTX 5080 Laptop GPU",
            "gpu_vram_mib": 16303,
            "gpu_compute_capability": "12.0",
        }
    )
    runtime = _write(tmp_path / "runtime.json", runtime_value)
    ca = tmp_path / "ca.crt"
    ca.write_bytes(b"-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----\n")
    return argparse.Namespace(
        hardware_receipt=hardware,
        converted_model_manifest=converted,
        conversion_manifest=conversion,
        runtime_manifest=runtime,
        ca_certificate=ca,
        context_tokens=4096,
        max_output_tokens=512,
        mem_fraction_static=0.92,
        kv_cache_dtype="none",
        chunked_prefill_size=1024,
        allowed_modes="assist,shadow",
        allowed_workloads="extract",
        profile_id_output=tmp_path / "profile.id",
        output=tmp_path / "profile.candidate.json",
    )


def _identity(profile: dict[str, Any], raw: bytes) -> dict[str, Any]:
    return {
        "candidate_profile_id": profile["profile_id"],
        "candidate_profile_sha256": hashlib.sha256(raw).hexdigest(),
        "served_model_alias": profile["served_model_alias"],
        "gateway_ca_certificate_sha256": profile["gateway_ca_certificate_sha256"],
    }


def test_operator_builds_one_runtime_loadable_candidate(tmp_path: Path) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    contract = importlib.import_module("profile_contract")
    args = _candidate_args(tmp_path)

    result = operator.build_candidate(args)
    profile = contract.load_launch_profile(
        args.output,
        args.profile_id_output,
        actual_runtime_image=operator.RUNTIME_IMAGE,
    )

    assert result["status"] == "candidate_created"
    assert profile.status == "candidate"
    assert profile.hardware_runtime_receipt_sha256 == operator.EXPECTED_HARDWARE_RUNTIME_RECEIPT_SHA256
    assert profile.served_model_alias == f"friday-secondary-{profile.profile_id}"
    assert args.profile_id_output.read_text(encoding="ascii") == profile.profile_id


def test_operator_rejects_hardware_receipt_drift_before_outputs(tmp_path: Path) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    args = _candidate_args(tmp_path)
    hardware = json.loads(args.hardware_receipt.read_text(encoding="utf-8"))
    hardware["gpu"]["driver_version"] = "changed"
    args.hardware_receipt.write_bytes(_canonical(hardware))

    with pytest.raises(operator.ProfileOperatorError, match="code-owned identity"):
        operator.build_candidate(args)
    assert not args.output.exists()
    assert not args.profile_id_output.exists()


def test_capacity_and_profile_promotion_are_candidate_epoch_bound(tmp_path: Path) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    contract = importlib.import_module("profile_contract")
    candidate_args = _candidate_args(tmp_path)
    operator.build_candidate(candidate_args)
    candidate_raw = candidate_args.output.read_bytes()
    candidate = json.loads(candidate_raw)
    identity = _identity(candidate, candidate_raw)
    rows = [
        {
            "context_target_tokens": 4096,
            "prompt_near_limit": True,
            "generated_envelope_met": True,
            "headroom_met": True,
        }
        for _ in range(3)
    ]
    trial = {
        "schema": "friday.secondary-context-capacity-trial.v1",
        "status": "measured_not_yet_certified",
        **identity,
        "candidates": [4096],
        "largest_passing_trial_tokens": 4096,
        "repeats_per_candidate": 3,
        "mem_fraction_static": 0.92,
        "runtime_process_start_time_seconds": "1700000000",
        "trials": rows,
    }
    initial = _write(tmp_path / "capacity-initial.json", trial)
    cold = _write(
        tmp_path / "capacity-cold.json",
        {**trial, "runtime_process_start_time_seconds": "1700000100"},
    )
    soak_value = {
        "schema": "friday.secondary-sglang-soak.v1",
        "status": "passed",
        **identity,
        "completed_requests": 100,
        "elapsed_sec": 1800,
        "failures": 0,
        "runtime_process_start_time_seconds": "1700000100",
        "raw_content_retained": False,
        "api_key_retained": False,
    }
    soak = _write(tmp_path / "soak.json", soak_value)
    capacity_output = tmp_path / "capacity.accepted.json"
    with pytest.raises(operator.ProfileOperatorError, match="runtime epoch"):
        operator.accept_capacity(
            argparse.Namespace(
                candidate=candidate_args.output,
                initial_trial=initial,
                cold_restart_trial=initial,
                soak=soak,
                output=tmp_path / "copied-capacity.json",
            )
        )
    operator.accept_capacity(
        argparse.Namespace(
            candidate=candidate_args.output,
            initial_trial=initial,
            cold_restart_trial=cold,
            soak=soak,
            output=capacity_output,
        )
    )
    quality = _write(
        tmp_path / "quality.json",
        {
            "schema": "friday.secondary-quality-battery.v1",
            "status": "passed",
            **identity,
            "cases": [
                {
                    "case": name,
                    "status": "passed",
                    "latency_sec": 0.01,
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "output_sha256": "" if name == "stream_cancellation" else "1" * 64,
                }
                for name in sorted(operator.QUALITY_CASES)
            ],
            "raw_content_retained": False,
            "api_key_retained": False,
        },
    )
    failure = _write(
        tmp_path / "failure.json",
        {
            "schema": "friday.secondary-failure-battery.v1",
            "status": "passed",
            **identity,
            "source_head": "a" * 40,
            "runner_sha256": hashlib.sha256((SCRIPTS / "failure_battery.py").read_bytes()).hexdigest(),
            "journey_contract_sha256": operator.journey_contract_sha256(),
            "suite_file_sha256": {name: "a" * 64 for name in operator.SUITE_FILES},
            "test_count": 15,
            "journeys": {
                name: {
                    "status": "passed",
                    "assertion_test": operator.JOURNEY_TESTS[name],
                }
                for name in operator.FAILURE_JOURNEYS
            },
            "primary_fallback_exactly_once": True,
            "effect_replay_observed": False,
            "v12_readiness_changed": False,
            "primary_only_flag_verified": True,
            "raw_content_retained": False,
            "credentials_retained": False,
        },
    )
    accepted_output = tmp_path / "profile.accepted.json"
    result = operator.accept_profile(
        argparse.Namespace(
            candidate=candidate_args.output,
            quality=quality,
            capacity=capacity_output,
            soak=soak,
            failure=failure,
            output=accepted_output,
        )
    )
    accepted = contract.load_launch_profile(
        accepted_output,
        candidate_args.profile_id_output,
        actual_runtime_image=operator.RUNTIME_IMAGE,
    )

    assert result["status"] == "profile_accepted"
    assert accepted.status == "accepted"
    assert accepted.profile_id == candidate["profile_id"]
    assert accepted.manifest_sha256 == hashlib.sha256(accepted_output.read_bytes()).hexdigest()


def test_profile_promotion_rejects_evidence_from_another_candidate(tmp_path: Path) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    args = _candidate_args(tmp_path)
    operator.build_candidate(args)
    candidate = json.loads(args.output.read_text(encoding="utf-8"))
    identity = _identity(candidate, args.output.read_bytes())
    identity["candidate_profile_sha256"] = "f" * 64
    evidence = _write(
        tmp_path / "wrong.json",
        {"schema": "friday.secondary-quality-battery.v1", "status": "passed", **identity},
    )

    with pytest.raises(operator.ProfileOperatorError, match="quality evidence"):
        operator.accept_profile(
            argparse.Namespace(
                candidate=args.output,
                quality=evidence,
                capacity=evidence,
                soak=evidence,
                failure=evidence,
                output=tmp_path / "accepted.json",
            )
        )


def test_quality_contract_rejects_anonymous_pass_rows_and_matches_the_runner() -> None:
    operator = importlib.import_module("runtime_profile_operator")
    battery = importlib.import_module("quality_battery")
    actual = {
        "exact_model_alias",
        *(case.name for case in battery._live_cases()),
        *(name for name, _content in battery._protocol_rejection_cases()),
        "tool_call_shape",
        "tool_result_continuation",
        "stream_cancellation",
        "client_disconnect_recovery",
    }

    assert actual == operator.QUALITY_CASES
    with pytest.raises(operator.ProfileOperatorError, match="quality evidence"):
        operator._validate_quality_cases([{"status": "passed"} for _ in operator.QUALITY_CASES])


def test_failure_runner_emits_only_measured_closed_journeys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    runner = importlib.import_module("failure_battery")
    args = _candidate_args(tmp_path)
    operator.build_candidate(args)
    monkeypatch.setattr(runner, "_require_clean_suite", lambda: None)
    monkeypatch.setattr(runner, "_git_head", lambda: "a" * 40)
    monkeypatch.setattr(
        runner,
        "_test_names",
        lambda _path: (set(runner.JOURNEY_TESTS.values()), len(runner.JOURNEY_TESTS) + 1),
    )
    monkeypatch.setattr(runner.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    output = tmp_path / "failure.accepted.json"

    result = runner.run_battery(candidate=args.output, ca_file=args.ca_certificate, output=output)
    evidence = json.loads(output.read_text(encoding="utf-8"))

    assert result["status"] == "passed"
    assert set(evidence["journeys"]) == set(runner.JOURNEY_TESTS)
    assert all(row["status"] == "passed" for row in evidence["journeys"].values())
    assert evidence["raw_content_retained"] is False
    assert evidence["credentials_retained"] is False
