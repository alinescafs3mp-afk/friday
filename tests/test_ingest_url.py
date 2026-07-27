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
        #
        # The old assertion was `inbox is not None`, which holds whether or not a
        # Knowledge Object was created: an inbox row exists on both paths. The route
        # called ingest_text WITHOUT force_review, so under the default policy the
        # classifier auto-promoted a fetched page and the object became canonical
        # with nobody having seen it. The comment above was true of the intent and
        # false of the code, and the test agreed with the comment.
        inbox = app.state.storage.find_inbox_by_raw(raw_id, LEGACY_OWNER_USER_ID)
        assert inbox is not None
        assert inbox["status"] == "pending"
        assert not inbox["knowledge_object_id"]
        assert body.get("promoted") is False
        assert body.get("knowledge_object") is None
        assert app.state.storage.count_knowledge_objects(LEGACY_OWNER_USER_ID) == 0

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


def test_an_article_shaped_page_still_waits_for_review(settings):
    """The gate has to hold for the material that actually trips the classifier.

    `test_ingest_url_creates_review_gated_raw_object` uses five repeats of one
    sentence, which scores below the promotion threshold — so it passed with or
    without the gate and guarded nothing. A real fetched page has headings, dates,
    names, deadlines and a contact address; measured, that body returns
    `promoted=True` and a canonical Knowledge Object under the default policy.

    That is the third path found bypassing the review gate (after
    `bulk_classify_inbox` and the disk importer), and a page from the open
    internet is precisely the material the gate exists for.
    """
    article = (
        "Руководство по настройке PostgreSQL 16 в проекте Orion. "
        "Мы решили перейти на версию 16 в третьем квартале 2026 года. "
        "Ответственный — Иван Петров из команды инфраструктуры. "
        "Основные шаги: обновить схему, проверить индексы, прогнать нагрузочные тесты. "
        "Крайний срок — 15 сентября 2026. Контакт: ops@example.com. "
        "Документация лежит в Confluence, раздел Orion/Migrations. "
    ) * 4
    result = FetchResult(
        url="https://example.com/guide",
        title="PostgreSQL 16 в Orion",
        text=article,
        text_length=len(article),
        status_code=200,
    )
    app, client, surfer = _client_with_surfer(settings, result)
    try:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        response = client.post("/api/ingest/url", json={"url": "https://example.com/guide"}, headers=owner)
        assert response.status_code == 200, response.text
        body = response.json()

        assert body.get("promoted") is False
        assert body.get("knowledge_object") is None
        assert app.state.storage.count_knowledge_objects(LEGACY_OWNER_USER_ID) == 0

        inbox = app.state.storage.find_inbox_by_raw(body["raw_object_id"], LEGACY_OWNER_USER_ID)
        assert inbox is not None and inbox["status"] == "pending"
        assert not inbox["knowledge_object_id"]
    finally:
        client.__exit__(None, None, None)
