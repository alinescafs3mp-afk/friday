"""`jericho model-check` has to fail when the endpoint is unusable, not when it is down.

Connecting proves nothing. The failure that cost real time here was an endpoint that
was reachable, served the right model, answered HTTP 200 — and spent its entire output
budget narrating, so callers got a monologue or nothing. Every probe below is written
against that class of failure rather than against connectivity.

The endpoint is faked in-process: the real one is on a LAN this suite cannot assume.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jericho.model_check import check_model

CLEAN_ANSWER = "Париж"
THINKING_ANSWER = (
    "Here's a thinking process:\n1. The user asks for the capital of France.\n"
    "2. It is Paris, in Russian Париж.\n</think>\n\nПариж"
)


def _endpoint(
    monkeypatch,
    *,
    content=CLEAN_ANSWER,
    finish="stop",
    tokens=4,
    models=("dispatcher",),
    status=200,
    json_content=None,
    embeddings=None,
):
    """Serve OpenAI-compatible responses in-process."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            if status != 200:
                return httpx.Response(status, json={"error": "Unauthorized"})
            return httpx.Response(200, json={"data": [{"id": name} for name in models]})
        if path.endswith("/embeddings"):
            if embeddings is None:
                return httpx.Response(404, json={"detail": "Not Found"})
            return httpx.Response(200, json={"data": [{"embedding": [0.1] * embeddings}]})
        body = json.loads(request.content)
        asked_for_json = "JSON" in str(body["messages"][0]["content"])
        text = json_content if asked_for_json and json_content is not None else content
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": text}, "finish_reason": finish}],
                "usage": {"completion_tokens": tokens},
            },
        )

    transport = httpx.MockTransport(handler)
    original = httpx.Client

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def _probe(report, name):
    return next(p for p in report.probes if p.name == name)


def test_a_healthy_endpoint_passes_every_probe(settings, monkeypatch):
    _endpoint(monkeypatch, json_content='{"kind":"note","importance":0.5}')
    report = check_model(settings)
    assert report.ok, report.to_dict()
    assert _probe(report, "chat").ok and _probe(report, "json output").ok


def test_a_leaking_chain_of_thought_fails_the_check(settings, monkeypatch):
    """The whole reason this command exists: HTTP 200, right model, unusable output."""
    _endpoint(monkeypatch, content=THINKING_ANSWER)
    report = check_model(settings)

    assert not report.ok
    leak = _probe(report, "reasoning suppressed")
    assert not leak.ok and "</think>" in leak.detail


def test_spending_the_whole_budget_thinking_is_reported_as_no_answer(settings, monkeypatch):
    """Measured on the real endpoint: 2000 tokens and the tag never closes."""
    _endpoint(monkeypatch, content="I need to think about this carefully. First", finish="length", tokens=256)
    report = check_model(settings)

    chat = _probe(report, "chat")
    assert not chat.ok
    assert "finish_reason=length" in chat.detail and "256 tokens" in chat.detail


def test_the_configured_model_not_being_served_is_caught(settings, monkeypatch):
    """A typo in JERICHO_LLM_MODEL passes a TCP probe and fails every real request."""
    _endpoint(monkeypatch, models=("some-other-model",))
    report = check_model(settings)

    endpoint = _probe(report, "endpoint")
    assert not endpoint.ok and "NOT among them" in endpoint.detail


def test_missing_credentials_say_which_variable_to_set(settings, monkeypatch):
    _endpoint(monkeypatch, status=401)
    report = check_model(settings)

    endpoint = _probe(report, "endpoint")
    assert not endpoint.ok and "JERICHO_LLM_API_KEY" in endpoint.detail
    # It stops there: probing generation against an endpoint that refuses auth is noise.
    assert len(report.probes) == 1


def test_unparseable_structured_output_fails(settings, monkeypatch):
    """Ingestion advice, entity extraction and vision all json.loads the reply."""
    _endpoint(monkeypatch, json_content="Конечно! Вот ваш ответ, надеюсь он поможет.")
    report = check_model(settings)

    assert not _probe(report, "json output").ok


@pytest.mark.parametrize(
    "enabled,dimensions,expected_ok", [(False, None, True), (True, 1024, True), (True, None, False)]
)
def test_embeddings_probe_reflects_configuration(settings, monkeypatch, enabled, dimensions, expected_ok):
    """Disabled is a legitimate state — it is reported, not failed. Enabled but absent
    is a real fault: dense recall silently degrades to lexical-only search."""
    import dataclasses

    settings = dataclasses.replace(settings, embeddings_enabled=enabled)
    _endpoint(monkeypatch, json_content='{"kind":"note"}', embeddings=dimensions)
    report = check_model(settings)

    assert _probe(report, "embeddings").ok is expected_ok


# --- local-weights advice must know where the model actually runs ---------


@pytest.mark.parametrize(
    "base_url,expect_warning",
    [
        ("http://127.0.0.1:8001/v1", True),
        ("http://localhost:8001/v1", True),
        ("http://203.0.113.11:8001/v1", False),
    ],
)
def test_local_weights_advice_only_when_this_host_serves_the_model(settings, base_url, expect_warning):
    """With the endpoint on another machine there is nothing to put in model_dir, and
    the advice tells the owner to configure what they already configured."""
    import dataclasses

    from jericho.diagnostics import collect_diagnostics

    settings = dataclasses.replace(settings, llm_enabled=True, llm_base_url=base_url)
    codes = [a["code"] for a in collect_diagnostics(settings)["actions"]]
    assert ("install_model_weights" in codes) is expect_warning


# --- batch size: reachable is not the same as usable ----------------------


def _batched_endpoint(monkeypatch, *, max_batch: int, dimensions: int = 1024):
    """An embeddings service that answers single inputs and caps its batch size."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "dispatcher"}]})
        if path.endswith("/embeddings"):
            body = json.loads(request.content)
            values = body["input"]
            values = values if isinstance(values, list) else [values]
            if len(values) > max_batch:
                return httpx.Response(
                    422,
                    json={
                        "message": f"batch size {len(values)} > maximum allowed batch size {max_batch}",
                        "code": 422,
                    },
                )
            return httpx.Response(200, json={"data": [{"embedding": [0.1] * dimensions} for _ in values]})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"kind":"note"}'}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 6},
            },
        )

    transport = httpx.MockTransport(handler)
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: original(*a, **{**k, "transport": transport}))


def test_a_service_that_answers_one_input_but_rejects_the_batch_fails_the_check(settings, monkeypatch):
    """The failure this probe exists for, measured against a real service.

    One input returned a 1024-dimension vector, so the single-input probe passed and
    the endpoint looked healthy — while every indexing request failed 422 and the
    corpus silently ended up with no vectors at all. Retrieval then scored exactly as
    it had without embeddings, which is indistinguishable from "embeddings did not
    help" unless something checks.
    """
    import dataclasses

    settings = dataclasses.replace(settings, embeddings_enabled=True, embeddings_max_inputs_per_request=64)
    _batched_endpoint(monkeypatch, max_batch=32)

    report = check_model(settings)

    assert _probe(report, "embeddings").ok, "a single input still works — that is the trap"
    batch = _probe(report, "embeddings batch")
    assert not batch.ok
    assert "maximum allowed batch size 32" in batch.detail
    assert "JERICHO_EMBEDDINGS_MAX_INPUTS_PER_REQUEST" in batch.detail
    assert not report.ok


def test_a_batch_within_the_limit_passes(settings, monkeypatch):
    import dataclasses

    settings = dataclasses.replace(settings, embeddings_enabled=True, embeddings_max_inputs_per_request=32)
    _batched_endpoint(monkeypatch, max_batch=32)

    report = check_model(settings)

    assert _probe(report, "embeddings batch").ok
    assert _probe(report, "embeddings batch").detail == "32/32 векторов"


# --- key resolution: the check must resolve it the way the code does ------


def test_an_empty_embeddings_key_inherits_the_llm_key(monkeypatch):
    """`.get(name, default)` falls back only when the variable is ABSENT.

    An env file line `JERICHO_EMBEDDINGS_API_KEY=` supplies an empty VALUE, so the
    intended inheritance did not happen: the backend sent no Authorization header and
    every indexing request came back 401 while the corpus quietly stayed unvectorised.
    """
    from jericho.config import load_settings

    monkeypatch.setenv("JERICHO_LLM_API_KEY", "shared-key-value")
    monkeypatch.setenv("JERICHO_EMBEDDINGS_API_KEY", "")
    assert load_settings().embeddings_api_key == "shared-key-value"

    monkeypatch.delenv("JERICHO_EMBEDDINGS_API_KEY")
    assert load_settings().embeddings_api_key == "shared-key-value"

    monkeypatch.setenv("JERICHO_EMBEDDINGS_API_KEY", "its-own-key")
    assert load_settings().embeddings_api_key == "its-own-key"


def test_the_check_authenticates_exactly_as_the_backend_does(settings, monkeypatch):
    """A checker that resolves configuration its own way blesses broken setups.

    model-check used `settings.embeddings_api_key or llm_key`, which treats empty as
    absent; EmbeddingBackend uses settings.embeddings_api_key verbatim. So the probe
    passed against an endpoint that rejected every real request.
    """
    import dataclasses

    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            seen.append(request.headers.get("authorization"))
            return httpx.Response(200, json={"data": [{"embedding": [0.1] * 8}]})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "dispatcher"}]})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"k":1}'}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 3},
            },
        )

    transport = httpx.MockTransport(handler)
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: original(*a, **{**k, "transport": transport}))

    configured = dataclasses.replace(
        settings, embeddings_enabled=True, embeddings_api_key="", llm_api_key="llm-only"
    )
    check_model(configured)

    assert seen, "the embeddings probe did not run"
    assert all(header is None for header in seen), (
        "the probe invented an Authorization header the real backend would not send"
    )
