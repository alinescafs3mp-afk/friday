from __future__ import annotations

from dataclasses import replace

import pytest

from friday.config import PROFILES, SglangExtraArgs, load_settings, profile_public_dict
from friday.model_profiles import (
    QWEN36_27B_V12_PROFILE,
    QWEN38_27B_SGLANG_V12_PROFILE,
    ModelCapability,
    ModelEffect,
    V12ModelGate,
    v12_model_profile_for,
)


def test_qwen38_runtime_profile_matches_the_attested_live_graph(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FRIDAY_PROFILE", "qwen38-27b-nvfp4-sglang")
    monkeypatch.setenv("FRIDAY_LLM_MODEL", "dispatcher")

    settings = load_settings()
    profile = settings.profile

    assert profile is PROFILES["qwen38-27b-nvfp4-sglang"]
    assert settings.llm_model == "dispatcher"
    assert profile.inference_backend == "sglang"
    assert profile.model_dir_name == "qwen3.8-27b-nvfp4-a2genesis-bfd9b312"
    assert profile.model_repository == "a2genesis/Qwen3.8-27B-NVFP4"
    assert profile.model_revision == "bfd9b31207712e0850eec9da32261e8c5ee16af7"
    assert profile.model_quantization == "W4A16_NVFP4"
    assert profile.runtime_image == (
        "lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124"
    )
    assert profile.runtime_source_revision == "c4271c3fe1262fc2adbd162c33b25de5255251c5"
    assert profile.runtime_reported_version == "0.0.0.dev0+qwen38.27b.g561c8f3"
    assert profile.engine_image_id == (
        "sha256:7f27e2885eca5041860a8c28c0bc3304b43b9fce072f298da043393866aa5887"
    )
    assert profile.engine_base_image_digest == profile.runtime_image
    assert profile.engine_base_image_id == (
        "sha256:317b75ce527f3b6ee482e9437c753e98f4df6e6b17a335f8681af5d86a8a9de8"
    )
    assert profile.model_snapshot_manifest_sha256 == (
        "da435c4b7556d8d5feed8551024914b0da0b48bb3fe85850536a0eb3b2489333"
    )
    assert profile.launch_manifest_sha256 == (
        "640a1ea428b2526ff6f3b3e412c18fef8e48f1fa882b3a94f9859a190678f62b"
    )
    assert profile.proxy_image_id == (
        "sha256:2bf585895ba4ede01899f4b17db5c690dd893d77c3e1da9ac4dfb2482e22c091"
    )
    assert profile.proxy_policy_sha256 == ("47e6b9c2dadea4a1e9395b8f8305699033b52a09ecba14d82afcdf77e7d9f3ae")
    assert profile.max_model_len == 40_960
    assert profile.max_num_seqs == 6
    assert profile.gpu_memory_utilization == 0.90
    assert profile.kv_cache_dtype == "fp8_e4m3"
    assert profile.document_map_max_concurrency == 1
    assert profile.vision_capable is True
    assert profile.suppress_model_thinking is True
    assert profile.vllm_image == ""
    assert profile.default_recommended is False
    assert PROFILES["qwen36-27b-nvfp4-nvidia"].default_recommended is True

    extra = profile.sglang_extra_args
    assert isinstance(extra, SglangExtraArgs)
    assert extra.mem_fraction_static == 0.90
    assert extra.max_total_tokens == 40_960
    assert extra.chunked_prefill_size == 2_048
    assert extra.mamba_ssm_dtype == "bfloat16"
    assert extra.max_mamba_cache_size == 6
    assert extra.radix_cache_enabled is False
    assert extra.cuda_graph_backend_decode == "full"
    assert extra.cuda_graph_max_bs_decode == 6
    assert extra.cuda_graph_bs_decode == (1, 2, 3, 4, 5, 6)
    assert extra.cuda_graph_backend_prefill == "disabled"
    assert extra.attention_backend == "flashinfer"
    assert extra.reasoning_parser == "qwen3"
    assert extra.tool_call_parser == "qwen3_coder"
    assert extra.mm_feature_transport == "cpu"
    assert extra.limit_mm_data_per_request == '{"image":4,"video":0,"audio":0}'
    assert extra.metrics_enabled is True
    assert extra.weight_version == "default"
    assert extra.speculative_algorithm is None


def test_qwen38_public_profile_exposes_immutable_model_and_runtime_provenance() -> None:
    public = profile_public_dict(PROFILES["qwen38-27b-nvfp4-sglang"])

    assert public["inference_backend"] == "sglang"
    assert public["model_repository"] == "a2genesis/Qwen3.8-27B-NVFP4"
    assert public["model_revision"] == "bfd9b31207712e0850eec9da32261e8c5ee16af7"
    assert public["model_quantization"] == "W4A16_NVFP4"
    assert public["runtime_image"] == (
        "lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124"
    )
    assert public["runtime_source_revision"] == "c4271c3fe1262fc2adbd162c33b25de5255251c5"
    assert public["runtime_reported_version"] == "0.0.0.dev0+qwen38.27b.g561c8f3"


def test_qwen38_v12_profile_is_exact_registered_v12_14_with_v12_13_authority() -> None:
    profile = QWEN38_27B_SGLANG_V12_PROFILE

    assert profile.profile_id == "qwen38-27b-nvfp4-sglang:dispatcher:v12.14"
    assert profile.runtime_profile_name == "qwen38-27b-nvfp4-sglang"
    assert profile.served_model_alias == "dispatcher"
    assert v12_model_profile_for("qwen38-27b-nvfp4-sglang", "dispatcher") is profile
    assert v12_model_profile_for("qwen38-27b-nvfp4-sglang", "Dispatcher") is None
    assert profile.planner_contract_sha256 == QWEN36_27B_V12_PROFILE.planner_contract_sha256
    assert profile.probe_suite_sha256 == QWEN36_27B_V12_PROFILE.probe_suite_sha256
    assert profile.allowed_capabilities == frozenset(
        {
            ModelCapability.TURN_PLAN_V1,
            ModelCapability.RU_PLANNING,
            ModelCapability.PREPARED_EVIDENCE_2,
            ModelCapability.CONTEXT_8K,
            ModelCapability.REMOTE_CANCELLATION,
        }
    )
    assert profile.required_capabilities == profile.allowed_capabilities
    assert profile.minimum_context_tokens == profile.max_context_tokens == 8_192
    assert profile.max_prepared_evidence_items == 2
    assert profile.max_tool_steps == 0
    assert profile.allowed_effects == frozenset({ModelEffect.READ})
    assert profile.verifier_required is True
    assert ModelCapability.RAW_VISION not in profile.allowed_capabilities
    assert ModelCapability.NATIVE_TOOL_CALLS not in profile.allowed_capabilities

    V12ModelGate(profile, endpoint_binding_sha256="a" * 64)
    with pytest.raises(ValueError, match="registered code-owned"):
        V12ModelGate(replace(profile), endpoint_binding_sha256="a" * 64)
