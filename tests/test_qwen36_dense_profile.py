from __future__ import annotations

from friday.config import PROFILES, load_settings, profile_public_dict


def test_qwen36_dense_profile_matches_the_deployed_text_only_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FRIDAY_PROFILE", "qwen36-27b-nvfp4-nvidia")
    settings = load_settings()
    profile = settings.profile

    assert profile is PROFILES["qwen36-27b-nvfp4-nvidia"]
    assert profile.model_dir_name == "qwen3.6-27b-nvfp4-nvidia"
    assert settings.llm_model == "dispatcher"
    assert settings.model_dir == settings.model_root / profile.model_dir_name
    assert profile.max_model_len == 32768
    assert profile.gpu_memory_utilization == 0.76
    assert profile.kv_cache_dtype == "fp8"
    assert profile.max_num_seqs == 1
    assert profile.quantization == "modelopt_mixed"
    assert profile.tokenizer_mode == "auto"
    assert profile.vision_capable is False
    assert profile.suppress_model_thinking is True

    extra = profile.vllm_extra_args
    assert extra.language_model_only is True
    assert extra.skip_mm_profiling is False
    assert extra.mm_processor_cache_gb is None
    assert extra.limit_mm_per_prompt is None
    assert extra.max_num_batched_tokens == 4096
    assert extra.reasoning_parser == "qwen3"
    assert extra.tool_call_parser == "qwen3_coder"
    assert extra.enable_auto_tool_choice is True


def test_qwen36_dense_profile_exposes_quantization_without_a_vision_claim():
    public = profile_public_dict(PROFILES["qwen36-27b-nvfp4-nvidia"])

    assert public["quantization"] == "modelopt_mixed"
    assert public["vision_capable"] is False
    assert public["vllm_image"] == (
        "vllm/vllm-openai@sha256:2238154357f576523db1df2866cbf591734d70db8f6d50b9a7897f3c60e18940"
    )


def test_dense_aligned_profile_is_the_only_recommended_default(monkeypatch, tmp_path):
    monkeypatch.setenv("FRIDAY_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FRIDAY_PROFILE", raising=False)

    assert load_settings().profile is PROFILES["qwen36-27b-nvfp4-nvidia"]
    assert PROFILES["qwen36-27b-nvfp4-nvidia"].default_recommended is True
    assert PROFILES["qwen36-vl"].default_recommended is False
