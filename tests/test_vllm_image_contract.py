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


def test_vllm_derivative_is_pinned_and_compose_builds_it():
    dockerfile = Path("docker/vllm-asyncio/Dockerfile").read_text(encoding="utf-8")
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    assert "vllm/vllm-openai:v0.25.1@sha256:" in dockerfile
    assert "docker/vllm-asyncio/patch_serve.py" in dockerfile
    assert "dockerfile: docker/vllm-asyncio/Dockerfile" in compose
    dispatcher_block = compose.split("  dispatcher:", 1)[1].split("\n  backend:", 1)[0]
    assert dispatcher_block.count("\n    build:") == 1
    assert ("JERICHO_LLM_BASE_URL: ${JERICHO_DOCKER_LLM_BASE_URL:-http://dispatcher:8001/v1}") in compose
    assert (
        "JERICHO_EMBEDDINGS_BASE_URL: ${JERICHO_DOCKER_EMBEDDINGS_BASE_URL:-http://dispatcher:8001/v1}"
    ) in compose
    assert "http://127.0.0.1:${JERICHO_API_PORT:-8000}/api/health" in compose
    assert "--max-model-len 32768" in compose
    assert "--gpu-memory-utilization 0.90" in compose
    assert "--max-num-batched-tokens 4096" in compose


def test_compose_propagates_security_and_resource_limits():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    required = {
        "JERICHO_API_USER_RATE_LIMIT_PER_MINUTE",
        "JERICHO_TRUST_PROXY_HEADERS",
        "JERICHO_TELEGRAM_SIGNATURE_MAX_AGE_SEC",
        "JERICHO_TELEGRAM_USER_RATE_LIMIT_PER_MINUTE",
        "JERICHO_TELEGRAM_GLOBAL_RATE_LIMIT_PER_MINUTE",
        "JERICHO_CODE_EXECUTION_TIMEOUT_SEC",
        "JERICHO_WEB_MAX_RESPONSE_BYTES",
        "JERICHO_MAX_UPLOAD_BYTES",
        "JERICHO_MAX_ARCHIVE_ENTRIES",
        "JERICHO_MAX_ARCHIVE_UNCOMPRESSED_BYTES",
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
