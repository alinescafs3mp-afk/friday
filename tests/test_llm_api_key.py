"""Bearer-token auth for the OpenAI-compatible LLM/embeddings endpoints.

Lets Jericho talk to a model on the LAN (e.g. a vLLM started with ``--api-key``)
without exposing the token in the public config. The token itself is redacted in
logs by the existing ``JERICHO_*_API_KEY``-matching secret scrubber.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace

import pytest

from jericho.agent_runtime.llm import LLMRouter
from jericho.retrieval import EmbeddingBackend


def test_config_loads_key_and_embeddings_defaults_to_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("JERICHO_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JERICHO_LLM_API_KEY", "sk-lan-123")
    monkeypatch.setenv("JERICHO_LLM_BASE_URL", "http://192.168.1.50:8001/v1")
    monkeypatch.delenv("JERICHO_EMBEDDINGS_API_KEY", raising=False)
    from jericho.config import load_settings

    s = load_settings()
    assert s.llm_api_key == "sk-lan-123"
    assert s.llm_base_url == "http://192.168.1.50:8001/v1"
    # A separate embeddings service shares the LLM token unless overridden.
    assert s.embeddings_api_key == "sk-lan-123"

    monkeypatch.setenv("JERICHO_EMBEDDINGS_API_KEY", "sk-emb-9")
    assert load_settings().embeddings_api_key == "sk-emb-9"


def test_router_sends_bearer_only_when_key_set(settings):
    keyed = replace(settings, llm_api_key="sk-lan-123")
    assert LLMRouter(keyed)._auth_headers() == {"Authorization": "Bearer sk-lan-123"}
    assert LLMRouter(replace(settings, llm_api_key=""))._auth_headers() == {}


def test_public_dict_flags_auth_without_leaking_key(settings):
    keyed = replace(settings, llm_api_key="sk-secret", embeddings_api_key="sk-secret")
    pub = keyed.public_dict()
    assert pub["llm"]["auth"] is True
    assert pub["embeddings"]["auth"] is True
    assert "sk-secret" not in json.dumps(pub)


@pytest.mark.asyncio
async def test_embedding_backend_sends_bearer_header(settings, monkeypatch):
    tuned = replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://192.168.1.9:8002/v1",
        embeddings_model="bge",
        embeddings_api_key="sk-emb-9",
    )
    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"data": [{"embedding": [0.1, 0.2]}]}

    class _FakeClient:
        def __init__(self, *args, headers=None, **kwargs) -> None:
            captured["headers"] = dict(headers or {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def post(self, url, json=None):
            captured["url"] = url
            return _FakeResp()

    monkeypatch.setattr("jericho.retrieval.httpx.AsyncClient", _FakeClient)
    out = await EmbeddingBackend(tuned).embed(["hi"])
    assert out == [[0.1, 0.2]]
    assert captured["headers"].get("Authorization") == "Bearer sk-emb-9"


@pytest.mark.asyncio
async def test_embedding_backend_no_header_without_key(settings, monkeypatch):
    tuned = replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:8002/v1",
        embeddings_model="bge",
        embeddings_api_key="",
    )
    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"data": [{"embedding": [1.0]}]}

    class _FakeClient:
        def __init__(self, *args, headers=None, **kwargs) -> None:
            captured["headers"] = dict(headers or {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> bool:
            return False

        async def post(self, url, json=None):
            return _FakeResp()

    monkeypatch.setattr("jericho.retrieval.httpx.AsyncClient", _FakeClient)
    await EmbeddingBackend(tuned).embed(["hi"])
    assert "Authorization" not in captured["headers"]


def test_llm_endpoint_status_sends_authorization(settings, monkeypatch):
    import jericho.diagnostics as diag

    monkeypatch.setattr(diag, "_port_reachable", lambda *a, **k: {"reachable": True})
    captured: dict = {}

    class _Resp:
        def read(self) -> bytes:
            return b'{"data": [{"id": "dispatcher"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

    def _fake_urlopen(request, timeout=None):
        captured["auth"] = request.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(diag.urllib.request, "urlopen", _fake_urlopen)
    status = diag._llm_endpoint_status("http://192.168.1.50:8001/v1", "dispatcher", api_key="sk-probe")
    assert status["model_served"] is True
    assert captured["auth"] == "Bearer sk-probe"


def test_init_env_template_includes_api_key_vars(tmp_path, monkeypatch):
    from jericho.cli import _init_environment

    monkeypatch.setenv("JERICHO_HOME", str(tmp_path / "home"))
    env_file = tmp_path / ".env.local"
    args = argparse.Namespace(home=str(tmp_path / "home"), env_file=str(env_file), force=False)
    assert _init_environment(args) == 0
    text = env_file.read_text(encoding="utf-8")
    assert "JERICHO_LLM_API_KEY=" in text
    assert "JERICHO_EMBEDDINGS_API_KEY=" in text
    # The token vars ship empty (local unauthenticated default).
    assert "JERICHO_LLM_API_KEY=\n" in text
    os.environ.pop("JERICHO_HOME", None)
