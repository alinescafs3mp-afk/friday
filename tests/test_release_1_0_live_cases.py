"""Model-free protocol/oracle tests using actual Friday upload and delivery."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from dataclasses import replace

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from tools import release_1_0_live_cases as cases


def _docx(lines):
    document = Document()
    for line in lines:
        document.add_paragraph(line)
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def _exercise(settings, monkeypatch, case_id, fault="", principal="owner"):
    from friday.server import create_app

    fixture = cases.word_fixture(case_id, "1" * 32)
    if principal == "owner":
        configured = replace(settings, telegram_owner_chat_ids=[42])
        chat_id = 42
    elif principal in {"user", "guest"}:
        configured = replace(
            settings,
            telegram_owner_chat_ids=[],
            new_account_preset="" if principal == "user" else "guest",
        )
        chat_id = 42
    elif principal in {"wrong_owner", "forged_owner"}:
        configured = replace(
            settings,
            telegram_owner_chat_ids=[42],
            new_account_preset="owner",
        )
        chat_id = 5001
    else:
        raise AssertionError("unknown principal fixture")
    app = create_app(configured)
    seen = []

    async def generated(_user_id, message, *, actor, **_kwargs):
        seen.append(message)
        assert fixture.filename in message
        assert fixture.required_text[0] not in message
        # Synthetic model boundary only. Real HTTP ingestion, owned file storage,
        # generated publication and authenticated downloads remain operational.
        source_id = app.state.storage.resolve_owned_file_source_ref(
            actor.user_id, actor.own_id, fixture.source_ref
        )
        assert source_id
        conversation = app.state.storage.create_conversation(actor.own_id, "R10 oracle control")
        app.state.storage.store_message(conversation["id"], actor.own_id, "user", message)
        if fault == "prior_turn":
            app.state.storage.store_message(conversation["id"], actor.own_id, "user", "old unrelated turn")
        assistant = app.state.storage.store_message(
            conversation["id"], actor.own_id, "assistant", "Файл готов."
        )
        payload = _docx(fixture.required_text if fault != "wrong_docx_facts" else ("unrelated content",))
        if fault == "invalid_docx":
            payload = b"not a Word package"
        return {
            "message_id": assistant["id"],
            "conversation_id": conversation["id"],
            "message": "Файл готов. " + " ".join(fixture.required_text),
            "files": [
                {
                    "kind": "document",
                    "filename": fixture.output_filename,
                    "mime_type": cases.DOCX_MIME,
                    "content_base64": base64.b64encode(payload).decode(),
                }
            ],
        }

    original_request = TestClient.request

    def damaged(client, method, url, *args, **kwargs):
        response = original_request(client, method, url, *args, **kwargs)
        if method == "GET" and url == "/api/me" and principal == "forged_owner":
            body = response.json()
            body["actor"] = {
                "user_id": LEGACY_OWNER_USER_ID,
                "preset_key": "owner",
                "source": "telegram-bridge",
            }
            body["user"] = app.state.storage.get_user(LEGACY_OWNER_USER_ID)
            return httpx.Response(response.status_code, json=body)
        if method == "POST" and url == "/api/chat" and response.status_code == 200:
            body = response.json()
            if fault == "missing_file":
                body["files"] = []
            elif fault == "duplicate_file":
                body["files"] = body["files"] * 2
            elif fault == "foreign_url":
                body["files"][0]["download_url"] = "https://foreign.invalid/artifact"
            elif fault == "wrong_name":
                body["files"][0]["filename"] = "another.docx"
            elif fault == "inline_mismatch":
                body["files"][0]["content_base64"] = base64.b64encode(b"foreign").decode()
            elif fault == "missing_message":
                body.pop("message_id")
            return httpx.Response(response.status_code, json=body)
        if method == "GET" and str(url).startswith("/api/files/"):
            is_docx = response.content.startswith(b"PK")
            if fault == "missing_download" and is_docx:
                return httpx.Response(404)
            if fault == "changed_download" and is_docx:
                return httpx.Response(200, content=b"changed bytes")
            if fault == "changed_source" and not is_docx:
                return httpx.Response(200, content=b"foreign source")
        return response

    monkeypatch.setattr(TestClient, "request", damaged)
    with TestClient(app) as client:
        app.state.agent.chat = generated
        original_message = app.state.storage.get_message

        def history(message_id, user_id):
            row = original_message(message_id, user_id)
            if row and fault == "missing_history":
                row = {**row, "metadata_json": "{}"}
            return row

        monkeypatch.setattr(app.state.storage, "get_message", history)
        session = cases.SignedSession(
            client,
            bridge_secret=configured.telegram_bridge_secret,
            chat_id=chat_id,
        )
        report = cases.run_word_case(session, app.state.storage, fixture)
    return report, session, seen


@pytest.mark.parametrize("case_id", [row[0] for row in cases.WORD_VARIANTS])
def test_named_source_word_oracle_uses_real_owned_upload_download_and_history(settings, monkeypatch, case_id):
    report, session, seen = _exercise(settings, monkeypatch, case_id)
    assert report["status"] == "PASS", (report, session.evidence)
    assert len(seen) == session.chat_submissions == 1
    assert report["attempt"] == 1
    assert report["observed_safe"]["artifact_sha256"]
    assert session.evidence[0]["response"]["actor"] == {
        "user_id": LEGACY_OWNER_USER_ID,
        "preset_key": "owner",
        "source": "telegram-bridge",
    }
    posts = [row for row in session.evidence if row["method"] == "POST"]
    assert len(posts) == 1 and posts[0]["path"] == "/api/chat"
    request = posts[0]["request"]
    assert "conversation_id" not in request and "reply_document_source_ref" not in request
    assert request["document"]["filename"] in request["message"]
    assert request["source_ref"] != request["document"]["source_ref"]
    assert "Signature" not in json.dumps(session.evidence)


@pytest.mark.parametrize(
    ("principal", "preset_key"),
    [("user", "user"), ("guest", "guest"), ("wrong_owner", "owner")],
)
def test_word_owner_oracle_refuses_real_non_owner_principals(settings, monkeypatch, principal, preset_key):
    report, session, seen = _exercise(
        settings,
        monkeypatch,
        cases.WORD_VARIANTS[0][0],
        principal=principal,
    )
    actor = session.evidence[0]["response"]["actor"]
    assert actor["preset_key"] == preset_key
    assert actor["user_id"] != LEGACY_OWNER_USER_ID
    assert report["status"] == "FAIL"
    assert report["failure_codes"] == ["case_owner_actor_mismatch"]
    assert seen == []
    assert session.chat_submissions == 0


def test_word_owner_oracle_binds_claimed_owner_to_signed_principal(settings, monkeypatch):
    report, session, seen = _exercise(
        settings,
        monkeypatch,
        cases.WORD_VARIANTS[0][0],
        principal="forged_owner",
    )
    assert session.evidence[0]["response"]["actor"]["user_id"] == LEGACY_OWNER_USER_ID
    assert report["status"] == "FAIL"
    assert report["failure_codes"] == ["case_owner_principal_mismatch"]
    assert seen == []
    assert session.chat_submissions == 0


@pytest.mark.parametrize(
    "fault",
    [
        "missing_file",
        "duplicate_file",
        "foreign_url",
        "wrong_name",
        "inline_mismatch",
        "missing_message",
        "missing_download",
        "changed_download",
        "changed_source",
        "missing_history",
        "prior_turn",
        "invalid_docx",
        "wrong_docx_facts",
    ],
)
def test_word_scenario_goes_red_for_damaged_actual_delivery(settings, monkeypatch, fault):
    report, session, seen = _exercise(settings, monkeypatch, cases.WORD_VARIANTS[0][0], fault)
    assert report["status"] == "FAIL", report
    assert report["failure_codes"]
    assert len(seen) == session.chat_submissions == 1
    assert all(not row["path"].startswith("https:") for row in session.evidence)


def test_failed_submission_is_first_attempt_and_cannot_be_retried():
    requests = []

    def failed(request):
        requests.append(request)
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(failed), base_url="http://test") as client:
        session = cases.SignedSession(client, bridge_secret="synthetic", chat_id=42)
        fixture = cases.word_fixture(cases.WORD_VARIANTS[0][0], "a" * 32)
        with pytest.raises(cases.CaseProtocolError, match="case_http_status"):
            session.first_chat(fixture)
        with pytest.raises(cases.CaseProtocolError, match="case_first_attempt_already_submitted"):
            session.first_chat(fixture)
    assert len(requests) == session.chat_submissions == 1


def test_artifact_reader_refuses_expanded_oversize_package():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * ((16 << 20) + 1))
    with pytest.raises(cases.CaseProtocolError, match="docx_package_invalid"):
        cases._docx_text(stream.getvalue())
