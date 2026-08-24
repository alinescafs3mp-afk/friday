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
            "gateway_image_id": ("sha256:d61d7ef52430df468e74ed6ee6e914429b80e20ba988e3176278a73165f876cf"),
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
        kv_cache_dtype="bf16",
        prefill_attention_backend="triton",
        decode_attention_backend="triton",
        sampling_backend="pytorch",
        page_size=1,
        radix_cache_enabled="true",
        overlap_schedule_enabled="true",
        swa_full_tokens_ratio="0.80",
        cuda_graph_backend_decode="disabled",
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


def _deterministic_failure(
    operator: Any,
    identity: dict[str, Any],
    *,
    source_head: str = "a" * 40,
) -> dict[str, Any]:
    suite_hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in operator.SUITE_FILES
    }
    return {
        "schema": operator.DETERMINISTIC_FAILURE_SCHEMA,
        "status": "passed",
        "evidence_scope": "deterministic_mock_contract",
        "live_physical_journeys_observed": False,
        **identity,
        "source_head": source_head,
        "runner_sha256": hashlib.sha256((SCRIPTS / "failure_battery.py").read_bytes()).hexdigest(),
        "journey_contract_sha256": operator.journey_contract_sha256(),
        "suite_file_sha256": suite_hashes,
        "test_count": len(operator.FAILURE_JOURNEYS),
        "journeys": {
            name: {"status": "passed", "assertion_test": operator.JOURNEY_TESTS[name]}
            for name in operator.FAILURE_JOURNEYS
        },
        "primary_fallback_exactly_once": True,
        "effect_replay_observed": False,
        "v12_readiness_changed": False,
        "primary_only_flag_verified": True,
        "raw_content_retained": False,
        "credentials_retained": False,
    }


def _controlled_live_failure(operator: Any, identity: dict[str, Any]) -> dict[str, Any]:
    live = importlib.import_module("live_failure_battery")
    return {
        "schema": live.SCHEMA,
        "status": "passed",
        "evidence_scope": live.EVIDENCE_SCOPE,
        **identity,
        "source_head": "a" * 40,
        "endpoint_base_url": live.ENDPOINT,
        "runner_sha256": hashlib.sha256((SCRIPTS / "live_failure_battery.py").read_bytes()).hexdigest(),
        "control_surface": {
            "ssh_host_alias": live.SSH_HOST_ALIAS,
            "authentication": "key_only_batch",
            "remote_bundle_path": live.REMOTE_BUNDLE_PATH,
            "command_set": sorted(live.CONTROL_ACTIONS),
            "ssh_output_retained": False,
        },
        "controlled_gateway_stop_observed": True,
        "tls_endpoint_loss_observed": True,
        "exact_candidate_gateway_recovery_observed": True,
        "gateway_recovery_preserved_runtime_epoch": True,
        "controlled_runtime_restart_observed": True,
        "runtime_application_outage_observed": True,
        "exact_candidate_runtime_recovery_observed": True,
        "runtime_epoch_before_sha256": "1" * 64,
        "runtime_epoch_after_sha256": "2" * 64,
        "runtime_epoch_changed": True,
        "physical_laptop_power_loss_observed": False,
        "friday_primary_process_continuity_observed": False,
        "primary_fallback_exactly_once_observed": False,
        "mid_turn_primary_fallback_observed": False,
        "raw_content_retained": False,
        "credentials_retained": False,
    }


def _physical_failure_begin(identity: dict[str, Any], *, source_head: str = "a" * 40) -> dict[str, Any]:
    return {
        "schema": "friday.secondary-physical-failure-state.v1",
        "status": "awaiting_physical_power_loss",
        **identity,
        "observer_source_head": source_head,
        "observer_runner_sha256": hashlib.sha256(
            (SCRIPTS / "live_failure_battery.py").read_bytes()
        ).hexdigest(),
        "primary_pid": 2613,
        "primary_process_epoch_before_sha256": "6" * 64,
        "primary_version": "0.207.8",
        "laptop_boot_epoch_before_sha256": "4" * 64,
        "raw_content_retained": False,
        "credentials_retained": False,
    }


def _physical_failure_state(
    identity: dict[str, Any],
    *,
    begin_sha256: str,
    source_head: str = "a" * 40,
) -> dict[str, Any]:
    return {
        **_physical_failure_begin(identity, source_head=source_head),
        "status": "physical_power_loss_observed_awaiting_recovery",
        "physical_begin_state_sha256": begin_sha256,
        "physical_tls_endpoint_unavailable_observed": True,
        "primary_process_epoch_while_off_sha256": "6" * 64,
        "physical_laptop_power_loss_operator_observed": True,
        "ordinary_primary_fallback_exactly_once_operator_observed": True,
        "mid_turn_primary_fallback_exactly_once_operator_observed": True,
        "effect_replay_operator_observed": False,
        "v12_readiness_changed_operator_observed": False,
    }


def _physical_failure_observation(
    operator: Any,
    identity: dict[str, Any],
    *,
    source_head: str = "a" * 40,
    state_sha256: str = "7" * 64,
) -> dict[str, Any]:
    return {
        "schema": operator.PHYSICAL_FAILURE_SCHEMA,
        "status": "observed",
        **identity,
        "observation_scope": "physical_power_loss_with_existing_primary_process",
        "observation_method": "code_owned_manual_state_machine",
        "observation_state_sha256": state_sha256,
        "observer_source_head": source_head,
        "observer_runner_sha256": hashlib.sha256(
            (SCRIPTS / "live_failure_battery.py").read_bytes()
        ).hexdigest(),
        "laptop_boot_epoch_before_sha256": "4" * 64,
        "laptop_boot_epoch_after_sha256": "5" * 64,
        "friday_primary_process_epoch_before_sha256": "6" * 64,
        "friday_primary_process_epoch_after_sha256": "6" * 64,
        "physical_laptop_power_loss_observed": True,
        "friday_primary_process_continuity_observed": True,
        "ordinary_primary_fallback_exactly_once_operator_observed": True,
        "mid_turn_primary_fallback_exactly_once_operator_observed": True,
        "readmitted_without_primary_restart_operator_observed": True,
        "effect_replay_operator_observed": False,
        "v12_readiness_changed_operator_observed": False,
        "raw_content_retained": False,
        "credentials_retained": False,
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
    assert profile.kv_cache_scale_policy == "not_applicable"
    assert profile.prefill_attention_backend == "triton"
    assert profile.decode_attention_backend == "triton"
    assert profile.sampling_backend == "pytorch"
    assert profile.moe_runner_backend == "flashinfer_mxfp4"
    assert profile.mxfp4_moe_precision == "default"
    assert profile.page_size == 1
    assert profile.radix_cache_enabled is True
    assert profile.overlap_schedule_enabled is True
    assert profile.hybrid_swa_memory_enabled is True
    assert profile.swa_full_tokens_ratio == "0.80"
    assert profile.cuda_graph_backend_decode == "disabled"
    assert profile.cuda_graph_max_bs_decode == 0
    assert profile.cuda_graph_bs_decode == ()
    assert profile.cuda_graph_backend_prefill == "disabled"
    assert profile.served_model_alias == f"friday-secondary-{profile.profile_id}"
    assert args.profile_id_output.read_text(encoding="ascii") == profile.profile_id


def test_operator_builds_the_closed_fp8_graph_candidate_surface(tmp_path: Path) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    contract = importlib.import_module("profile_contract")
    args = _candidate_args(tmp_path)
    args.context_tokens = 65536
    args.mem_fraction_static = 0.95
    args.kv_cache_dtype = "fp8_e4m3"
    args.decode_attention_backend = "trtllm_mha"
    args.sampling_backend = "flashinfer"
    args.page_size = 16
    args.radix_cache_enabled = "false"
    args.overlap_schedule_enabled = "false"
    args.swa_full_tokens_ratio = "0.25"
    args.cuda_graph_backend_decode = "full"

    operator.build_candidate(args)
    profile = contract.load_launch_profile(
        args.output,
        args.profile_id_output,
        actual_runtime_image=operator.RUNTIME_IMAGE,
    )

    assert profile.context_tokens == 65536
    assert profile.mem_fraction_static == "0.95"
    assert profile.kv_cache_dtype == "fp8_e4m3"
    assert profile.kv_cache_scale_policy == "implicit_unit"
    assert profile.decode_attention_backend == "trtllm_mha"
    assert profile.sampling_backend == "flashinfer"
    assert profile.page_size == 16
    assert profile.radix_cache_enabled is False
    assert profile.overlap_schedule_enabled is False
    assert profile.swa_full_tokens_ratio == "0.25"
    assert profile.cuda_graph_backend_decode == "full"
    assert profile.cuda_graph_max_bs_decode == 1
    assert profile.cuda_graph_bs_decode == (1,)


def test_operator_rejects_chunked_prefill_outside_the_closed_grid(tmp_path: Path) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    args = _candidate_args(tmp_path)
    args.chunked_prefill_size = 513

    with pytest.raises(operator.ProfileOperatorError, match="chunked prefill"):
        operator.build_candidate(args)


def test_candidate_cli_defaults_preserve_the_safe_baseline(tmp_path: Path) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    args = operator._parser().parse_args(
        [
            "candidate",
            "--hardware-receipt",
            str(tmp_path / "hardware.json"),
            "--source-model-manifest",
            str(tmp_path / "source.json"),
            "--runtime-manifest",
            str(tmp_path / "runtime.json"),
            "--ca-certificate",
            str(tmp_path / "ca.crt"),
            "--context-tokens",
            "4096",
            "--mem-fraction-static",
            "0.92",
            "--profile-id-output",
            str(tmp_path / "profile.id"),
            "--output",
            str(tmp_path / "profile.json"),
        ]
    )

    assert (
        args.kv_cache_dtype,
        args.prefill_attention_backend,
        args.decode_attention_backend,
        args.sampling_backend,
        args.page_size,
        args.radix_cache_enabled,
        args.overlap_schedule_enabled,
        args.swa_full_tokens_ratio,
        args.cuda_graph_backend_decode,
    ) == ("bf16", "triton", "triton", "pytorch", 1, "true", "true", "0.80", "disabled")


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("kv_cache_scale_policy", "implicit_unit"),
        ("cuda_graph_max_bs_decode", 1),
        ("cuda_graph_bs_decode", [1]),
        ("hybrid_swa_memory_enabled", False),
    ),
)
def test_candidate_validation_rejects_incoherent_engine_relationships(
    tmp_path: Path,
    key: str,
    value: Any,
) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    args = _candidate_args(tmp_path)
    operator.build_candidate(args)
    candidate = json.loads(args.output.read_text(encoding="utf-8"))
    candidate[key] = value
    binding = operator._engine_sha256(candidate)
    candidate["engine_binding_sha256"] = binding
    candidate["profile_id"] = f"gptoss20b-{binding}"
    candidate["served_model_alias"] = f"friday-secondary-{candidate['profile_id']}"
    raw = _canonical(candidate)

    with pytest.raises(operator.ProfileOperatorError, match="candidate profile"):
        operator._validate_candidate(candidate, raw)


def test_endpoint_identity_accepts_only_the_closed_candidate_specific_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    endpoint = importlib.import_module("endpoint_common")
    args = _candidate_args(tmp_path)
    args.context_tokens = 40960
    args.kv_cache_dtype = "fp8_e4m3"
    args.decode_attention_backend = "trtllm_mha"
    args.sampling_backend = "flashinfer"
    args.page_size = 16
    args.swa_full_tokens_ratio = "1.00"
    args.cuda_graph_backend_decode = "full"
    operator.build_candidate(args)
    monkeypatch.setattr(endpoint, "_ENDPOINT_IDENTITY", None)
    monkeypatch.setattr(endpoint, "EXPECTED_MODEL", "friday-secondary-gptoss20b")

    alias = endpoint.configure_expected_model(args.output, args.ca_certificate)

    assert alias.startswith("friday-secondary-gptoss20b-")
    assert endpoint.configured_profile_context_tokens() == 40960

    candidate = json.loads(args.output.read_text(encoding="utf-8"))
    candidate["kv_cache_scale_policy"] = "not_applicable"
    binding = operator._engine_sha256(candidate)
    candidate["engine_binding_sha256"] = binding
    candidate["profile_id"] = f"gptoss20b-{binding}"
    candidate["served_model_alias"] = f"friday-secondary-{candidate['profile_id']}"
    invalid = _write(tmp_path / "invalid-profile.json", candidate)

    with pytest.raises(endpoint.EndpointError, match="runtime profile identity"):
        endpoint.configure_expected_model(invalid, args.ca_certificate)


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
                failure_deterministic=placeholder,
                failure_live=placeholder,
                failure_physical_begin=placeholder,
                failure_physical_state=placeholder,
                failure_physical_observation=placeholder,
                output=tmp_path / "accepted.json",
            )
        )


def test_capacity_and_profile_promotion_are_candidate_epoch_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    monkeypatch.setattr(operator, "_repository_head", lambda: "a" * 40)
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
                    "prompt_tokens": (
                        4096 - operator.LONG_CONTEXT_PROMPT_RESERVE
                        if name == operator.LONG_CONTEXT_QUALITY_CASE
                        else 1
                    ),
                    "completion_tokens": 1,
                    "output_sha256": "" if name == "stream_cancellation" else "1" * 64,
                }
                for name in sorted(operator.QUALITY_CASES)
            ],
            "raw_content_retained": False,
            "api_key_retained": False,
        },
    )
    deterministic = _write(
        tmp_path / "failure.deterministic.json",
        _deterministic_failure(operator, identity),
    )
    controlled_live = _write(
        tmp_path / "failure.live.json",
        _controlled_live_failure(operator, identity),
    )
    physical_begin = _write(
        tmp_path / "failure.physical-begin.json",
        _physical_failure_begin(identity),
    )
    physical_state = _write(
        tmp_path / "failure.physical-state.json",
        _physical_failure_state(
            identity,
            begin_sha256=hashlib.sha256(physical_begin.read_bytes()).hexdigest(),
        ),
    )
    physical = _write(
        tmp_path / "failure.physical.json",
        _physical_failure_observation(
            operator,
            identity,
            state_sha256=hashlib.sha256(physical_state.read_bytes()).hexdigest(),
        ),
    )
    mixed_begin_value = _physical_failure_begin(identity)
    mixed_begin_value["laptop_boot_epoch_before_sha256"] = "9" * 64
    mixed_begin = _write(tmp_path / "failure.physical-begin-mixed.json", mixed_begin_value)
    with pytest.raises(operator.ProfileOperatorError, match="physical failure state"):
        operator.accept_failure(
            argparse.Namespace(
                candidate=candidate_args.output,
                deterministic=deterministic,
                live=controlled_live,
                physical_begin=mixed_begin,
                physical_state=physical_state,
                physical_observation=physical,
                output=tmp_path / "failure.mixed-chain.json",
            )
        )
    stale_live_value = _controlled_live_failure(operator, identity)
    stale_live_value["source_head"] = "b" * 40
    stale_live = _write(tmp_path / "failure.live-stale.json", stale_live_value)
    with pytest.raises(operator.ProfileOperatorError, match="current tested source epoch"):
        operator.accept_failure(
            argparse.Namespace(
                candidate=candidate_args.output,
                deterministic=deterministic,
                live=stale_live,
                physical_begin=physical_begin,
                physical_state=physical_state,
                physical_observation=physical,
                output=tmp_path / "failure.stale-source.json",
            )
        )
    failure = tmp_path / "failure.accepted.json"
    failure_result = operator.accept_failure(
        argparse.Namespace(
            candidate=candidate_args.output,
            deterministic=deterministic,
            live=controlled_live,
            physical_begin=physical_begin,
            physical_state=physical_state,
            physical_observation=physical,
            output=failure,
        )
    )
    assert failure_result["status"] == "failure_accepted"
    assert json.loads(failure.read_text(encoding="utf-8"))["schema"] == operator.FAILURE_SCHEMA

    with pytest.raises(operator.ProfileOperatorError, match="failure evidence is not accepted"):
        operator.accept_profile(
            argparse.Namespace(
                candidate=candidate_args.output,
                hardware_receipt=candidate_args.hardware_receipt,
                source_model_manifest=candidate_args.source_model_manifest,
                runtime_manifest=candidate_args.runtime_manifest,
                ca_certificate=candidate_args.ca_certificate,
                quality=quality,
                capacity=capacity_output,
                soak=soak,
                failure=deterministic,
                failure_deterministic=deterministic,
                failure_live=controlled_live,
                failure_physical_begin=physical_begin,
                failure_physical_state=physical_state,
                failure_physical_observation=physical,
                output=tmp_path / "profile.mock-only.json",
            )
        )
    forged_failure_value = json.loads(failure.read_text(encoding="utf-8"))
    forged_failure_value["controlled_live_failure_sha256"] = "f" * 64
    forged_failure = _write(tmp_path / "failure.forged-composite.json", forged_failure_value)
    with pytest.raises(operator.ProfileOperatorError, match="source receipts"):
        operator.accept_profile(
            argparse.Namespace(
                candidate=candidate_args.output,
                hardware_receipt=candidate_args.hardware_receipt,
                source_model_manifest=candidate_args.source_model_manifest,
                runtime_manifest=candidate_args.runtime_manifest,
                ca_certificate=candidate_args.ca_certificate,
                quality=quality,
                capacity=capacity_output,
                soak=soak,
                failure=forged_failure,
                failure_deterministic=deterministic,
                failure_live=controlled_live,
                failure_physical_begin=physical_begin,
                failure_physical_state=physical_state,
                failure_physical_observation=physical,
                output=tmp_path / "profile.forged-composite.json",
            )
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
            failure_deterministic=deterministic,
            failure_live=controlled_live,
            failure_physical_begin=physical_begin,
            failure_physical_state=physical_state,
            failure_physical_observation=physical,
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
                failure_deterministic=evidence,
                failure_live=evidence,
                failure_physical_begin=evidence,
                failure_physical_state=evidence,
                failure_physical_observation=evidence,
                output=tmp_path / "accepted.json",
            )
        )


def test_quality_contract_rejects_anonymous_pass_rows_and_matches_the_runner() -> None:
    operator = importlib.import_module("runtime_profile_operator")
    battery = importlib.import_module("quality_battery")
    actual = {
        "exact_model_alias",
        *(case.name for case in battery._live_cases()),
        battery.LONG_CONTEXT_CASE_NAME,
        *(name for name, _content in battery._protocol_rejection_cases()),
        "tool_call_shape",
        "tool_result_continuation",
        "stream_cancellation",
        "client_disconnect_recovery",
    }

    assert actual == operator.QUALITY_CASES
    with pytest.raises(operator.ProfileOperatorError, match="quality evidence"):
        operator._validate_quality_cases(
            [{"status": "passed"} for _ in operator.QUALITY_CASES],
            context_tokens=4096,
        )


def test_near_limit_quality_case_is_context_derived_bounded_and_content_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    battery = importlib.import_module("quality_battery")
    context_tokens = 65536
    quality_case = battery._near_limit_long_context_case(context_tokens)
    serialized_prompt = json.dumps(quality_case.messages, ensure_ascii=False)
    captured: dict[str, Any] = {}

    def completion_request(**kwargs: Any) -> Any:
        captured["messages"] = kwargs["messages"]
        return battery.SanitizedCompletion(
            content=battery._LONG_CONTEXT_MARKER,
            latency_sec=0.1,
            prompt_tokens=context_tokens - 512,
            completion_tokens=8,
            finish_reason="stop",
            reasoning_present=False,
        )

    monkeypatch.setattr(battery, "_completion_request", completion_request)
    row = battery._run_live_case(
        quality_case,
        base_url="https://192.168.1.35:8443/v1",
        api_key="a" * 64,
        timeout_sec=10.0,
        ca_file=Path("private-ca.crt"),
    )

    assert quality_case.name == battery.LONG_CONTEXT_CASE_NAME
    assert quality_case.max_tokens == 128
    assert len(serialized_prompt.encode("utf-8")) < battery._LONG_CONTEXT_MAX_PROMPT_BYTES
    assert serialized_prompt.count(battery._LONG_CONTEXT_MARKER) == 1
    assert captured["messages"] == quality_case.messages
    assert row["status"] == "passed"
    assert row["prompt_tokens"] == context_tokens - 512
    assert battery._LONG_CONTEXT_MARKER not in json.dumps(row)
    assert " x x x" not in json.dumps(row)

    below_limit = battery.SanitizedCompletion(
        content=battery._LONG_CONTEXT_MARKER,
        latency_sec=0.1,
        prompt_tokens=context_tokens - battery._LONG_CONTEXT_ACCEPTANCE_RESERVE - 1,
        completion_tokens=8,
        finish_reason="stop",
        reasoning_present=False,
    )
    assert quality_case.validator(below_limit) is False


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


def test_controlled_live_failure_runner_uses_fixed_key_only_surface_and_never_claims_power_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    live = importlib.import_module("live_failure_battery")
    args = _candidate_args(tmp_path)
    operator.build_candidate(args)
    api_key = tmp_path / "gateway.key"
    api_key.write_text("a" * 64, encoding="ascii")
    controls: list[str] = []
    tls = iter((True, False))
    endpoint_error = importlib.import_module("endpoint_common").EndpointError
    epochs: Iterator[str | BaseException] = iter(
        ("1700000000", "1700000000", endpoint_error("runtime unavailable"), "1700000100")
    )

    def ready_with_restart(*_args: Any, **_kwargs: Any) -> str:
        value = next(epochs)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(live, "_run_control", controls.append)
    monkeypatch.setattr(live, "_tls_handshake_available", lambda *_args, **_kwargs: next(tls))
    monkeypatch.setattr(live, "_ready_epoch", ready_with_restart)
    monkeypatch.setattr(
        live,
        "_source_identity",
        lambda: (
            "a" * 40,
            hashlib.sha256((SCRIPTS / "live_failure_battery.py").read_bytes()).hexdigest(),
        ),
    )
    output = tmp_path / "failure.live.json"

    result = live.run_battery(
        candidate=args.output,
        api_key_file=api_key,
        ca_file=args.ca_certificate,
        output=output,
        timeout_sec=1.0,
        recovery_timeout_sec=60.0,
    )
    evidence = json.loads(output.read_text(encoding="utf-8"))

    assert controls == ["stop_gateway", "start_gateway", "restart_runtime"]
    assert result["status"] == "controlled_live_failure_passed"
    assert evidence["control_surface"] == {
        "ssh_host_alias": "friday-secondary-brain",
        "authentication": "key_only_batch",
        "remote_bundle_path": r"C:\ProgramData\FridaySecondary\bundle",
        "command_set": sorted(live.CONTROL_ACTIONS),
        "ssh_output_retained": False,
    }
    assert evidence["physical_laptop_power_loss_observed"] is False
    assert evidence["friday_primary_process_continuity_observed"] is False
    assert evidence["primary_fallback_exactly_once_observed"] is False
    assert evidence["credentials_retained"] is False


def test_controlled_live_failure_restores_bundle_when_gateway_journey_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    live = importlib.import_module("live_failure_battery")
    args = _candidate_args(tmp_path)
    operator.build_candidate(args)
    api_key = tmp_path / "gateway.key"
    api_key.write_text("a" * 64, encoding="ascii")
    controls: list[str] = []
    monkeypatch.setattr(live, "_run_control", controls.append)
    monkeypatch.setattr(live, "_tls_handshake_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(live, "_ready_epoch", lambda *_args, **_kwargs: "1700000000")
    monkeypatch.setattr(
        live,
        "_source_identity",
        lambda: (
            "a" * 40,
            hashlib.sha256((SCRIPTS / "live_failure_battery.py").read_bytes()).hexdigest(),
        ),
    )
    monkeypatch.setattr(
        live,
        "_wait_for_tls_loss",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(live.LiveFailureBatteryError("endpoint stayed up")),
    )

    with pytest.raises(live.LiveFailureBatteryError, match="gateway outage journey"):
        live.run_battery(
            candidate=args.output,
            api_key_file=api_key,
            ca_file=args.ca_certificate,
            output=tmp_path / "must-not-exist.json",
            timeout_sec=1.0,
            recovery_timeout_sec=60.0,
        )

    assert controls == ["stop_gateway", "recover_all"]
    assert not (tmp_path / "must-not-exist.json").exists()


def test_live_control_command_is_bounded_key_only_and_overrides_long_stop_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = importlib.import_module("live_failure_battery")
    captured: list[str] = []

    def run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        captured.extend(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(live.subprocess, "run", run)
    live._run_control("restart_runtime")
    script = importlib.import_module("base64").b64decode(captured[-1]).decode("utf-16-le")

    assert "BatchMode=yes" in captured
    assert "PasswordAuthentication=no" in captured
    assert "KbdInteractiveAuthentication=no" in captured
    assert "StrictHostKeyChecking=yes" in captured
    assert "friday-secondary-brain" in captured
    assert r"C:\ProgramData\FridaySecondary\bundle" in script
    assert "restart --timeout 60 sglang" in script
    with pytest.raises(live.LiveFailureBatteryError, match="closed command set"):
        live._run_control("arbitrary-command")


def test_physical_failure_receipt_requires_the_code_owned_three_stage_witness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    live = importlib.import_module("live_failure_battery")
    args = _candidate_args(tmp_path)
    operator.build_candidate(args)
    api_key = tmp_path / "gateway.key"
    api_key.write_text("a" * 64, encoding="ascii")
    runner_sha = hashlib.sha256((SCRIPTS / "live_failure_battery.py").read_bytes()).hexdigest()
    boot_epochs = iter(("4" * 64, "5" * 64))
    monkeypatch.setattr(live, "_source_identity", lambda: ("a" * 40, runner_sha))
    monkeypatch.setattr(live, "_friday_backend_main_pid", lambda: 2613)
    monkeypatch.setattr(live, "_ready_epoch", lambda *_args, **_kwargs: "1700000000")
    monkeypatch.setattr(live, "_primary_process_epoch_sha256", lambda _pid: "6" * 64)
    monkeypatch.setattr(live, "_primary_health", lambda _timeout: "0.207.8")
    monkeypatch.setattr(live, "_laptop_boot_epoch_sha256", lambda: next(boot_epochs))

    begin = tmp_path / "physical.begin.json"
    with pytest.raises(live.LiveFailureBatteryError, match="friday-backend.service MainPID"):
        live.begin_physical_observation(
            candidate=args.output,
            api_key_file=api_key,
            ca_file=args.ca_certificate,
            primary_pid=999,
            output=tmp_path / "wrong-primary.json",
        )
    live.begin_physical_observation(
        candidate=args.output,
        api_key_file=api_key,
        ca_file=args.ca_certificate,
        primary_pid=2613,
        output=begin,
    )
    monkeypatch.setattr(live, "_tls_handshake_available", lambda *_args, **_kwargs: False)
    with pytest.raises(live.LiveFailureBatteryError, match="must be explicit"):
        live.record_physical_power_loss(
            candidate=args.output,
            ca_file=args.ca_certificate,
            state_path=begin,
            output=tmp_path / "unconfirmed.json",
            physical_power_loss_observed=True,
            ordinary_fallback_observed=True,
            mid_turn_fallback_observed=False,
            no_effect_replay_observed=True,
            v12_readiness_unchanged_observed=True,
        )
    off = tmp_path / "physical.off.json"
    live.record_physical_power_loss(
        candidate=args.output,
        ca_file=args.ca_certificate,
        state_path=begin,
        output=off,
        physical_power_loss_observed=True,
        ordinary_fallback_observed=True,
        mid_turn_fallback_observed=True,
        no_effect_replay_observed=True,
        v12_readiness_unchanged_observed=True,
    )
    assert json.loads(off.read_text(encoding="utf-8"))["physical_begin_state_sha256"] == (
        hashlib.sha256(begin.read_bytes()).hexdigest()
    )
    receipt = tmp_path / "physical.observed.json"
    live.finish_physical_observation(
        candidate=args.output,
        api_key_file=api_key,
        ca_file=args.ca_certificate,
        state_path=off,
        output=receipt,
        readmitted_without_primary_restart_observed=True,
    )
    evidence = json.loads(receipt.read_text(encoding="utf-8"))

    assert evidence["observation_method"] == "code_owned_manual_state_machine"
    assert evidence["observer_runner_sha256"] == runner_sha
    assert evidence["physical_laptop_power_loss_observed"] is True
    assert (
        evidence["friday_primary_process_epoch_before_sha256"]
        == evidence["friday_primary_process_epoch_after_sha256"]
    )
    assert evidence["raw_content_retained"] is False


def test_composite_failure_rejects_a_forged_physical_observer_runner(tmp_path: Path) -> None:
    operator = importlib.import_module("runtime_profile_operator")
    args = _candidate_args(tmp_path)
    operator.build_candidate(args)
    candidate_raw = args.output.read_bytes()
    candidate = json.loads(candidate_raw)
    identity = _identity(candidate, candidate_raw)
    deterministic = _write(
        tmp_path / "deterministic.json",
        _deterministic_failure(operator, identity),
    )
    controlled = _write(tmp_path / "live.json", _controlled_live_failure(operator, identity))
    physical_begin = _write(tmp_path / "physical-begin.json", _physical_failure_begin(identity))
    physical_state = _write(
        tmp_path / "physical-state.json",
        _physical_failure_state(
            identity,
            begin_sha256=hashlib.sha256(physical_begin.read_bytes()).hexdigest(),
        ),
    )
    physical_value = _physical_failure_observation(
        operator,
        identity,
        state_sha256=hashlib.sha256(physical_state.read_bytes()).hexdigest(),
    )
    physical_value["observer_runner_sha256"] = "f" * 64
    physical = _write(tmp_path / "physical.json", physical_value)

    with pytest.raises(operator.ProfileOperatorError, match="physical failure observation"):
        operator.accept_failure(
            argparse.Namespace(
                candidate=args.output,
                deterministic=deterministic,
                live=controlled,
                physical_begin=physical_begin,
                physical_state=physical_state,
                physical_observation=physical,
                output=tmp_path / "failure.accepted.json",
            )
        )
    assert not (tmp_path / "failure.accepted.json").exists()
