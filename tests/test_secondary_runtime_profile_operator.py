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
    hardware = _write(tmp_path / "hardware.json", _hardware())
    source_manifest = tmp_path / "source-manifest.json"
    source_manifest.write_bytes(b"sealed-source-manifest-fixture")
    runtime_value = json.loads((BUNDLE / "runtime-manifest.example.json").read_text(encoding="utf-8"))
    runtime_value.update(
        {
            "status": "accepted",
            "gateway_image_id": ("sha256:8d764dd92e0b48d0ca94887dc0fe1df6dffc5200b25b2efcc2deb7ffb61d714c"),
            "cuda_runtime_version": "13.0",
            "pytorch_version": "2.11.0+cu130",
            "flashinfer_version": "0.6.15.post1",
            "sgl_kernel_version": "0.4.5",
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
        source_model_manifest=source_manifest,
        runtime_manifest=runtime,
        ca_certificate=ca,
        context_tokens=4096,
        max_output_tokens=512,
        mem_fraction_static=0.92,
        chunked_prefill_size=1024,
        allowed_modes="assist,shadow",
        allowed_workloads="extract",
        profile_id_output=tmp_path / "profile.id",
        output=tmp_path / "profile.candidate.json",
    )


@pytest.fixture(autouse=True)
def _verified_source_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    source_contract = importlib.import_module("source_model_manifest")

    def verify(path: Path, expected_sha256: str) -> Any:
        assert path.name == "source-manifest.json"
        assert expected_sha256 == source_contract.SOURCE_MANIFEST_RAW_SHA256
        return source_contract.SourceModelReceipt(
            manifest_sha256=source_contract.SOURCE_MANIFEST_RAW_SHA256,
            source_revision=source_contract.SOURCE_REVISION,
            file_count=source_contract.SOURCE_FILE_COUNT,
            total_bytes=source_contract.SOURCE_TOTAL_BYTES,
        )

    monkeypatch.setattr(operator, "verify_source_model_manifest", verify)


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
    assert profile.source_model_manifest_sha256 == operator.SOURCE_MANIFEST_RAW_SHA256
    assert profile.runtime_image_config_digest == operator.RUNTIME_IMAGE_CONFIG_DIGEST
    assert profile.runtime_image_oci_manifest_digest == operator.RUNTIME_IMAGE_OCI_MANIFEST_DIGEST
    assert profile.quantization == "mxfp4"
    assert profile.dtype == "bfloat16"
    assert profile.kv_cache_dtype == "bf16"
    assert profile.moe_runner_backend == "flashinfer_mxfp4"
    assert profile.mxfp4_moe_precision == "default"
    assert profile.cuda_graph_backend_decode == "disabled"
    assert profile.cuda_graph_backend_prefill == "disabled"
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


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("cuda_runtime_version", "REPLACE_AFTER_LIVE_PROBE"),
        ("pytorch_version", "REPLACE_AFTER_LIVE_PROBE"),
        ("flashinfer_version", "REPLACE_AFTER_LIVE_PROBE"),
        ("sgl_kernel_version", "REPLACE_AFTER_LIVE_PROBE"),
        ("gateway_image_id", "sha256:" + "3" * 64),
    ),
)
def test_operator_rejects_unmeasured_or_unbound_runtime_identity(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    args = _candidate_args(tmp_path)
    runtime = json.loads(args.runtime_manifest.read_text(encoding="utf-8"))
    runtime[key] = value
    args.runtime_manifest.write_bytes(_canonical(runtime))

    with pytest.raises(operator.ProfileOperatorError, match="runtime manifest identity"):
        operator.build_candidate(args)
    assert not args.output.exists()
    assert not args.profile_id_output.exists()


def test_profile_promotion_rechecks_the_source_runtime_chain(tmp_path: Path) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    args = _candidate_args(tmp_path)
    operator.build_candidate(args)
    args.ca_certificate.write_bytes(b"-----BEGIN CERTIFICATE-----\nchanged\n-----END CERTIFICATE-----\n")
    placeholder = _write(tmp_path / "placeholder.json", {})

    with pytest.raises(operator.ProfileOperatorError, match="no longer matches"):
        operator.accept_profile(
            argparse.Namespace(
                candidate=args.output,
                hardware_receipt=args.hardware_receipt,
                source_model_manifest=args.source_model_manifest,
                runtime_manifest=args.runtime_manifest,
                ca_certificate=args.ca_certificate,
                quality=placeholder,
                capacity=placeholder,
                soak=placeholder,
                failure=placeholder,
                output=tmp_path / "accepted.json",
            )
        )


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
            hardware_receipt=candidate_args.hardware_receipt,
            source_model_manifest=candidate_args.source_model_manifest,
            runtime_manifest=candidate_args.runtime_manifest,
            ca_certificate=candidate_args.ca_certificate,
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
                hardware_receipt=args.hardware_receipt,
                source_model_manifest=args.source_model_manifest,
                runtime_manifest=args.runtime_manifest,
                ca_certificate=args.ca_certificate,
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


def test_endpoint_helpers_fix_the_certified_gpt_oss_sampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = importlib.import_module("endpoint_common")
    captured: dict[str, Any] = {}

    def request_json(_method: str, _url: str, **kwargs: Any) -> tuple[dict[str, Any], float]:
        captured.update(kwargs["payload"])
        return (
            {
                "model": endpoint.EXPECTED_MODEL,
                "choices": [
                    {
                        "message": {"content": "ready"},
                        "finish_reason": "stop",
                    }
                ],
            },
            0.01,
        )

    monkeypatch.setattr(endpoint, "request_json", request_json)
    endpoint.chat_completion(
        "http://127.0.0.1:30000/v1",
        api_key="a" * 64,
        messages=[{"role": "user", "content": "ready"}],
        timeout_sec=1.0,
        max_tokens=16,
    )

    assert {key: captured[key] for key in ("reasoning_effort", "temperature", "top_p", "seed")} == {
        "reasoning_effort": "low",
        "temperature": 1.0,
        "top_p": 1.0,
        "seed": 0,
    }
    with pytest.raises(endpoint.EndpointError, match="cannot be overridden"):
        endpoint.chat_completion(
            "http://127.0.0.1:30000/v1",
            api_key="a" * 64,
            messages=[{"role": "user", "content": "ready"}],
            timeout_sec=1.0,
            max_tokens=16,
            extra={"seed": 1},
        )


def test_failure_runner_labels_mocked_contracts_as_not_live_physical_journeys(
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

    assert result["status"] == "deterministic_contract_passed"
    assert result["live_physical_journeys_observed"] is False
    assert evidence["evidence_scope"] == "deterministic_mock_contract"
    assert evidence["live_physical_journeys_observed"] is False
    assert set(evidence["journeys"]) == set(runner.JOURNEY_TESTS)
    assert all(row["status"] == "passed" for row in evidence["journeys"].values())
    assert evidence["raw_content_retained"] is False
    assert evidence["credentials_retained"] is False
