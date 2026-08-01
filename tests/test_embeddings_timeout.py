"""The embeddings request must wait as long as the operator said it may.

Every other HTTP client in the codebase uses the configured timeout. This one took
`min(llm_timeout_sec, 60.0)` — a ceiling that silently overrode the setting, and the
only place that did.

It is not a theoretical loss. Re-measured on the installed service the throughput is
768 characters/second, not the ~2800 the request-size cap was calibrated against, so
a full-size request takes 51.6 seconds against that 60-second ceiling. The owner's
log holds 83 failed embedding requests between 00:00 and 03:00 on one night, all
while indexing a single large document, with `llm_timeout_sec` set to 240 throughout.
A failed request loses the whole batch, and the objects in it keep no vector.
"""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from friday.retrieval import EmbeddingBackend


class _CapturingClient:
    """Stands in for `httpx.AsyncClient` and remembers the timeout it was handed."""

    seen: list[httpx.Timeout] = []

    def __init__(self, *, timeout, **_kwargs):
        _CapturingClient.seen.append(timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, *_args, **_kwargs):
        raise httpx.ReadTimeout("stand-in")


@pytest.fixture()
def embeddings_settings(settings):
    return replace(
        settings,
        embeddings_enabled=True,
        embeddings_base_url="http://127.0.0.1:9/v1",
        embeddings_model="test-embed",
    )


@pytest.mark.asyncio
async def test_the_configured_timeout_is_used_verbatim(embeddings_settings, monkeypatch):
    _CapturingClient.seen.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    generous = replace(embeddings_settings, llm_timeout_sec=240.0)

    await EmbeddingBackend(generous).embed(["текст"])

    assert _CapturingClient.seen, "the client was never constructed"
    assert _CapturingClient.seen[-1].read == pytest.approx(240.0)


@pytest.mark.asyncio
async def test_a_short_configured_timeout_is_also_honoured(embeddings_settings, monkeypatch):
    """Honoured means honoured in both directions — no floor either."""
    _CapturingClient.seen.clear()
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    impatient = replace(embeddings_settings, llm_timeout_sec=5.0)

    await EmbeddingBackend(impatient).embed(["текст"])

    assert _CapturingClient.seen[-1].read == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_a_timeout_returns_nothing_rather_than_raising(embeddings_settings, monkeypatch):
    """The worker defers the batch; it must not take the tick down with it."""
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    assert await EmbeddingBackend(embeddings_settings).embed(["текст"]) is None
