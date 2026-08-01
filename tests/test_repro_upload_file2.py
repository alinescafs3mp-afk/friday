"""Вариант: модель отвечает длинной репликой о приёме файла (>=2 блоков)."""

from __future__ import annotations

import base64

import pytest


@pytest.mark.parametrize("filename", ["приказ.pdf", "штат.docx", "скан.png", "смета.xlsx", "заметка.txt"])
@pytest.mark.asyncio
async def test_long_acknowledgement(settings, monkeypatch, filename):
    from fastapi.testclient import TestClient

    from friday.server import create_app

    app = create_app(settings)

    with TestClient(app) as client:
        async def _fake_ingest_file(*_args, **_kwargs):
            return {"transcript_text": "", "queued_for_review": True, "knowledge_object": {"id": "ko-1"}}

        monkeypatch.setattr(app.state.ingestion, "ingest_file", _fake_ingest_file)

        llm = app.state.agent.llm
        calls = []

        async def _fake_chat(messages, tools=None, **_kwargs):
            calls.append(bool(tools))
            return {
                "content": (
                    "Документ принят и поставлен во «Входящие» на разбор.\n"
                    "Как только он будет разобран, содержимое станет доступно поиску.\n"
                    "Сейчас в архиве 0 записей."
                ),
                "tool_calls": [],
            }

        monkeypatch.setattr(llm, "chat", _fake_chat)
        monkeypatch.setattr(type(llm), "enabled", property(lambda self: True), raising=False)

        response = client.post(
            "/api/chat",
            json={
                "message": "",
                "document": {
                    "filename": filename,
                    "content_base64": base64.b64encode(b"data").decode(),
                    "mime_type": "application/octet-stream",
                },
            },
            headers={"Authorization": f"Bearer {settings.api_token}"},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        files = data.get("files") or []
        print(
            f"UPLOAD {filename} -> llm_calls={len(calls)} files="
            + str([(f.get("filename"), f.get("mime_type")) for f in files])
        )
