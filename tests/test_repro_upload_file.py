"""Репродукция: загрузка .pdf без подписи -> Пятница присылает свой файл."""

from __future__ import annotations

import base64

import pytest


@pytest.mark.asyncio
async def test_uploading_a_pdf_makes_friday_generate_a_file(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)

    with TestClient(app) as client:
        async def _fake_ingest_file(*_args, **_kwargs):
            return {
                "transcript_text": "",
                "queued_for_review": True,
                "knowledge_object": {"id": "ko-1"},
            }

        monkeypatch.setattr(app.state.ingestion, "ingest_file", _fake_ingest_file)

        response = client.post(
            "/api/chat",
            json={
                "message": "",
                "document": {
                    "filename": "приказ.pdf",
                    "content_base64": base64.b64encode(b"%PDF-1.4 fake").decode(),
                    "mime_type": "application/pdf",
                },
            },
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        print("MESSAGE:", data.get("message")[:400])
        files = data.get("files") or []
        print("FILES:", [(f.get("filename"), f.get("mime_type"), len(f.get("content_base64") or "")) for f in files])
        assert not files, f"Пятница прислала свой файл в ответ на загрузку: {files}"


@pytest.mark.asyncio
async def test_uploading_a_pdf_with_live_model(settings, monkeypatch):
    """То же, но с включённой моделью — как на демо."""
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)

    with TestClient(app) as client:
        async def _fake_ingest_file(*_args, **_kwargs):
            return {
                "transcript_text": "",
                "queued_for_review": True,
                "knowledge_object": {"id": "ko-1"},
            }

        monkeypatch.setattr(app.state.ingestion, "ingest_file", _fake_ingest_file)

        llm = app.state.agent.llm
        calls: list[list[dict]] = []

        async def _fake_chat(messages, tools=None, **_kwargs):
            calls.append(list(messages))
            if not tools:
                # второй, «чистый» вызов — содержимое документа
                return {"content": "Сводка по архиву\nЗаписей в базе: 0\nЭто выдуманный документ."}
            return {"content": "Файл принят и поставлен во «Входящие» на разбор.", "tool_calls": []}

        monkeypatch.setattr(llm, "chat", _fake_chat)
        monkeypatch.setattr(type(llm), "enabled", property(lambda self: True), raising=False)

        response = client.post(
            "/api/chat",
            json={
                "message": "",
                "document": {
                    "filename": "приказ.pdf",
                    "content_base64": base64.b64encode(b"%PDF-1.4 fake").decode(),
                    "mime_type": "application/pdf",
                },
            },
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        print("MESSAGE:", (data.get("message") or "")[:400])
        print("LLM CALLS:", len(calls))
        files = data.get("files") or []
        print("FILES:", [(f.get("filename"), f.get("mime_type"), len(f.get("content_base64") or "")) for f in files])
        assert not files, f"Пятница прислала свой файл в ответ на загрузку: {files}"
