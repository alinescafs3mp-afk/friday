"""Ingest-by-URL — §27: web content becomes a review-gated Knowledge Object.

Web pages previously reached only the agent as a 12k preview and were never
stored; only the answer synthesis was saved. POST /api/ingest/url fetches a
public URL through the SSRF-hardened web_surfer and routes its cleaned text
through the ordinary ingestion pipeline (Raw Object → Inbox → KO after review).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jericho.permissions import LEGACY_OWNER_USER_ID
from jericho.server import create_app
from jericho.web_surfer import FetchResult


class _FakeWebSurfer:
    def __init__(self, result: FetchResult):
        self._result = result
        self.calls: list[str] = []

    async def fetch(self, url: str, **_: object) -> FetchResult:
        self.calls.append(url)
        return self._result

    async def close(self) -> None:  # pragma: no cover - lifespan shutdown
        pass


def _client_with_surfer(settings, result: FetchResult):
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()
    surfer = _FakeWebSurfer(result)
    app.state.web_surfer = surfer
    return app, client, surfer


def test_ingest_url_creates_review_gated_raw_object(settings):
    result = FetchResult(
        url="https://example.com/article",
        title="Отчёт по проекту Orion",
        text="Проект Orion переходит на PostgreSQL 16 в третьем квартале. " * 5,
        text_length=300,
        status_code=200,
    )
    app, client, surfer = _client_with_surfer(settings, result)
    try:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        response = client.post("/api/ingest/url", json={"url": "https://example.com/article"}, headers=owner)
        assert response.status_code == 200, response.text
        body = response.json()
        assert surfer.calls == ["https://example.com/article"]
        assert body["url"] == "https://example.com/article"
        assert body["title"] == "Отчёт по проекту Orion"
        raw_id = body["raw_object_id"]
        assert raw_id

        # Provenance: the Raw Object records source='web' and the URL.
        raw = app.state.storage.get_raw_object(raw_id, LEGACY_OWNER_USER_ID)
        assert raw["source"] == "web"
        assert raw["source_ref"] == "https://example.com/article"

        # It waits in the Inbox for review — it is not silently canonical.
        inbox = app.state.storage.find_inbox_by_raw(raw_id, LEGACY_OWNER_USER_ID)
        assert inbox is not None

        # Re-ingesting the same URL+content is idempotent, not a duplicate.
        replay = client.post("/api/ingest/url", json={"url": "https://example.com/article"}, headers=owner)
        assert replay.status_code == 200
        assert replay.json()["raw_object_id"] == raw_id
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "result",
    [
        FetchResult("https://blocked.example", "", "", 0, error="Blocked URL: private"),
        FetchResult("https://empty.example", "Пусто", "   ", 0, status_code=200),
    ],
)
def test_ingest_url_refuses_unfetchable_or_empty(settings, result):
    app, client, _ = _client_with_surfer(settings, result)
    try:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        response = client.post("/api/ingest/url", json={"url": result.url}, headers=owner)
        assert response.status_code == 422
        # Nothing was stored.
        count = app.state.storage.execute("SELECT COUNT(*) AS c FROM raw_objects").fetchone()["c"]
        assert count == 0
    finally:
        client.__exit__(None, None, None)


def test_ingest_url_requires_url(settings):
    app, client, _ = _client_with_surfer(settings, FetchResult("https://x", "", "text", 4, status_code=200))
    try:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        response = client.post("/api/ingest/url", json={}, headers=owner)
        assert response.status_code == 400
    finally:
        client.__exit__(None, None, None)
