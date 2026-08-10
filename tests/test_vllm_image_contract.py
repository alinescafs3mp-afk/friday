from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path


def _load_patcher():
    path = Path("docker/vllm-asyncio/patch_serve.py")
    spec = importlib.util.spec_from_file_location("jericho_vllm_patch", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_multimodal_dispatcher_is_pinned_to_the_dense_modelopt_runtime():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    dispatcher_block = compose.split("  dispatcher:", 1)[1].split("\n  backend:", 1)[0]
    assert "vllm/vllm-openai@sha256:2238154357" in dispatcher_block
    assert "--model /models/qwen3.6-27b-nvfp4-nvidia" in dispatcher_block
    assert "--quantization modelopt_mixed" in dispatcher_block
    assert "--no-language-model-only" in dispatcher_block
    assert "--language-model-only" not in dispatcher_block.replace("--no-language-model-only", "")
    assert '--limit-mm-per-prompt \'{"image":4,"video":0}\'' in dispatcher_block
    assert "--skip-mm-profiling" not in dispatcher_block
    assert ("FRIDAY_LLM_BASE_URL: ${FRIDAY_DOCKER_LLM_BASE_URL:-http://dispatcher:8001/v1}") in compose
    assert (
        "FRIDAY_EMBEDDINGS_BASE_URL: ${FRIDAY_DOCKER_EMBEDDINGS_BASE_URL:-http://dispatcher:8001/v1}"
    ) in compose
    assert "http://127.0.0.1:${FRIDAY_API_PORT:-8000}/api/health" in compose
    assert "--max-model-len 32768" in compose
    assert "--gpu-memory-utilization 0.76" in compose
    assert "--max-num-seqs 1" in compose
    assert "--max-num-batched-tokens 4096" in compose


def test_every_bootstrap_surface_selects_the_multimodal_dense_profile():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    example = Path(".env.example").read_text(encoding="utf-8")
    cli = Path("friday/cli.py").read_text(encoding="utf-8")
    entrypoint = Path("docker/entrypoint.sh").read_text(encoding="utf-8")

    expected = "qwen36-27b-nvfp4-nvidia"
    assert f"FRIDAY_PROFILE: ${{FRIDAY_PROFILE:-{expected}}}" in compose
    assert f"FRIDAY_PROFILE={expected}" in example
    assert f"FRIDAY_PROFILE={expected}" in cli
    assert "/runtime/models/qwen3.6-27b-nvfp4-nvidia" in entrypoint


def test_compose_propagates_security_and_resource_limits():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    required = {
        "FRIDAY_API_USER_RATE_LIMIT_PER_MINUTE",
        "FRIDAY_TRUST_PROXY_HEADERS",
        "FRIDAY_TELEGRAM_SIGNATURE_MAX_AGE_SEC",
        "FRIDAY_TELEGRAM_USER_RATE_LIMIT_PER_MINUTE",
        "FRIDAY_TELEGRAM_GLOBAL_RATE_LIMIT_PER_MINUTE",
        "FRIDAY_CODE_EXECUTION_TIMEOUT_SEC",
        "FRIDAY_WEB_MAX_RESPONSE_BYTES",
        "FRIDAY_MAX_UPLOAD_BYTES",
        "FRIDAY_MAX_ARCHIVE_ENTRIES",
        "FRIDAY_MAX_ARCHIVE_UNCOMPRESSED_BYTES",
    }
    assert all(f"{name}:" in compose for name in required)


def test_vllm_patcher_is_fail_closed_and_changes_only_reviewed_anchors(monkeypatch):
    patcher = _load_patcher()
    source = b"import argparse\n\nasync def main():\n    if True:\n            uvloop.run(run_server(args))\n"
    monkeypatch.setattr(patcher, "EXPECTED_SOURCE_SHA256", hashlib.sha256(source).hexdigest())
    patched = patcher.patch_source(source)
    assert b"import argparse\nimport asyncio\n" in patched
    assert b"asyncio.run(run_server(args))" in patched
    assert b"uvloop.run(run_server(args))" not in patched

    monkeypatch.setattr(patcher, "EXPECTED_SOURCE_SHA256", "0" * 64)
    try:
        patcher.patch_source(source)
    except patcher.PatchError:
        pass
    else:
        raise AssertionError("patcher must reject an unreviewed upstream hash")
