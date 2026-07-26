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
