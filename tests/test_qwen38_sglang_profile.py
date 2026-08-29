from __future__ import annotations

from dataclasses import replace
from pathlib import Path

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

_PROXY_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "handoffs"
    / "SGLang-Qwen38-Abliterated-V12-Attested"
    / "remote"
    / "default.conf.template"
)


def test_attested_proxy_uses_exact_case_sensitive_bearer_guards() -> None:
    template = _PROXY_TEMPLATE.read_text(encoding="utf-8")
    guard = 'if ($http_authorization != "Bearer ${JARVIS_LLM_API_KEY}") { return 401; }'
    protected_locations = (
        "/v1/chat/completions",
        "/v1/models",
        "/metrics",
        "/server_info",
        "/_friday/v1/deployment-witness",
    )

    assert "map $http_authorization" not in template
    assert template.count(guard) == len(protected_locations)
    for location in protected_locations:
        start = template.index(f"location = {location} {{")
        end = template.index("\n    }", start)
        assert template[start:end].count(guard) == 1


def test_qwen38_runtime_profile_matches_the_attested_live_graph(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FRIDAY_PROFILE", "qwen38-27b-nvfp4-sglang")
    monkeypatch.setenv("FRIDAY_LLM_MODEL", "dispatcher")

    settings = load_settings()
    profile = settings.profile

    assert profile is PROFILES["qwen38-27b-nvfp4-sglang"]
    assert settings.llm_model == "dispatcher"
    assert profile.inference_backend == "sglang"
    assert profile.model_dir_name == "qwen3.8-27b-abliterated-nvfp4-vtuber-43aa7ff5"
    assert profile.model_repository == "Vtuber-plan/Huihui-Qwen3.8-27B-abliterated-NVFP4"
    assert profile.model_revision == "43aa7ff5eef05ab50a3bfa6aca581085312c7a04"
    assert profile.model_quantization == "W4A4_NVFP4_FP8_KV"
    assert profile.runtime_image == (
        "lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124"
    )
    assert profile.runtime_source_revision == "c4271c3fe1262fc2adbd162c33b25de5255251c5"
    assert profile.runtime_reported_version == "0.0.0.dev0+qwen38.27b.g561c8f3"
    assert profile.engine_image_id == (
        "sha256:62ae2bb57a54a1dfcc33c05cdfd200cc69705ac94ad503cd4ec00a409804acaf"
    )
    assert profile.engine_base_image_digest == profile.runtime_image
    assert profile.engine_base_image_id == (
        "sha256:317b75ce527f3b6ee482e9437c753e98f4df6e6b17a335f8681af5d86a8a9de8"
    )
    assert profile.model_snapshot_manifest_sha256 == (
        "e5fa0d366c3bcf6546f9f3d0cb418b8e2530e2701a5a1506367f88fd08d1d1a4"
    )
    assert profile.launch_manifest_sha256 == (
        "ed18fc43f7a865dc0d01c568f22200fb71eebdcc2cef354f859860c966f3a19a"
    )
    assert profile.proxy_image_id == (
        "sha256:2227ed08bc4360eea50b1bba31b0f07d5652ba63344a0ab0f135aec63fb680de"
    )
    assert profile.proxy_policy_sha256 == ("d51c092ca2ef566f092ef9d55320e302c2d10b710d319d27a6d982aba018dcfe")
    assert profile.max_model_len == 40_960
    assert profile.max_num_seqs == 6
    assert profile.gpu_memory_utilization == 0.90
    assert profile.kv_cache_dtype == "fp8_e4m3"
    assert profile.document_map_max_concurrency == 1
    assert profile.vision_capable is True
    assert profile.suppress_model_thinking is True
    assert profile.certification == "quick_smoke_only"
    assert profile.interactive_certified is False
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
    assert public["model_repository"] == "Vtuber-plan/Huihui-Qwen3.8-27B-abliterated-NVFP4"
    assert public["model_revision"] == "43aa7ff5eef05ab50a3bfa6aca581085312c7a04"
    assert public["model_quantization"] == "W4A4_NVFP4_FP8_KV"
    assert public["runtime_image"] == (
        "lmsysorg/sglang@sha256:506525a5907ea22c9d445afb7c03603959b912de034d86915cf17da814f1a124"
    )
    assert public["runtime_source_revision"] == "c4271c3fe1262fc2adbd162c33b25de5255251c5"
    assert public["runtime_reported_version"] == "0.0.0.dev0+qwen38.27b.g561c8f3"


def test_qwen38_v12_profile_is_exact_registered_v12_15_with_v12_13_authority() -> None:
    profile = QWEN38_27B_SGLANG_V12_PROFILE

    assert profile.profile_id == "qwen38-27b-nvfp4-sglang:dispatcher:v12.15"
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
    assert profile.minimum_context_tokens == 8_192
    assert profile.max_context_tokens == 40_960
    assert profile.max_prepared_evidence_items == 2
    assert profile.max_tool_steps == 0
    assert profile.max_tool_rounds == 0
    assert profile.max_tool_calls == 0
    assert profile.allowed_effects == frozenset({ModelEffect.READ})
    assert profile.verifier_required is True
    assert ModelCapability.RAW_VISION not in profile.allowed_capabilities
    assert ModelCapability.NATIVE_TOOL_CALLS not in profile.allowed_capabilities

    V12ModelGate(
        profile,
        endpoint_binding_sha256="a" * 64,
        installation_context_tokens=40_960,
    )
    with pytest.raises(ValueError, match="registered code-owned"):
        V12ModelGate(
            replace(profile),
            endpoint_binding_sha256="a" * 64,
            installation_context_tokens=40_960,
        )
