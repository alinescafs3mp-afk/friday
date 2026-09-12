"""Actual HTTP authorization for one durable mixed answer; external providers are fixtures."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_api_vertical_slice import _bridge_get
from test_mixed_journey_runtime import _ANSWER, _MESSAGE, _Model
from test_transient_web_comparison import _RecordingWeb, _report, _source
from test_v12_file_evidence_reader import _register

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app


def _digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _snapshot(state):
    # Authentication bookkeeping is outside this selected content-state oracle.
    tables = ("messages", "conversations", "raw_objects", "request_idempotency")
    rows = {
        table: tuple(tuple(row) for row in state.storage.execute(f"SELECT * FROM {table} ORDER BY rowid"))
        for table in tables
    }
    files = {
        path.relative_to(state.settings.files_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in state.settings.files_dir.rglob("*")
        if path.is_file()
    }
    return rows, files, len(state.model.calls), len(state.web.calls)


def _assert_body(response, expected):
    assert response.status_code == 200, "delivery_http_status"
    # JSON serialization distinguishes True from 1 and rejects extra fields.
    assert json.dumps(response.json(), sort_keys=True) == json.dumps(expected, sort_keys=True), (
        "delivery_http_body"
    )


def _assert_unchanged(state, before):
    assert _snapshot(state) == before, "delivery_content_state"


@pytest.fixture
def delivery_turn(settings):
    scoped = replace(settings, shared_archive=True, telegram_owner_chat_ids=[5001], workers_enabled=False)
    app = create_app(scoped)
    owner = {"Authorization": f"Bearer {scoped.api_token}"}
    with TestClient(app) as client:
        storage = app.state.storage
        archive, _ = _register(
            storage,
            scoped,
            user_id=LEGACY_OWNER_USER_ID,
            uploaded_by=LEGACY_OWNER_USER_ID,
            filename="archive.txt",
            text="ARCHIVE TERMS",
        )
        conversation = storage.create_conversation(LEGACY_OWNER_USER_ID, "Mixed delivery HTTP")
        model = _Model(answer=_ANSWER)
        web = _RecordingWeb(
            {
                **_report(_source(1, text="PUBLIC TERMS")),
                "provider_primary_id": "brave",
                "selected_provider_id": "brave",
                "provider_used_fallback": False,
            }
        )
        app.state.agent._selected_archive_model = model
        app.state.agent.kernel.web_surfer = web
        app.state.auth_service.grant_permission(LEGACY_OWNER_USER_ID, "web.compare.transient")
        first = client.post(
            "/api/chat",
            headers=owner,
            json={
                "message": _MESSAGE,
                "conversation_id": conversation["id"],
                "source_ref": "mixed-delivery-http:001",
                "document": {
                    "filename": "current.txt",
                    "mime_type": "text/plain",
                    "content_base64": base64.b64encode(b"CURRENT TERMS").decode(),
                },
            },
        )
        assert first.status_code == 200 and first.json()["message"] == _ANSWER
        assert len(model.calls) == 2 and len(web.calls) == 1
        message_id = first.json()["message_id"]
        message = storage.get_message(message_id, LEGACY_OWNER_USER_ID)
        metadata = json.loads(message["metadata_json"])
        current = [
            row["id"]
            for row in storage.execute("SELECT id, metadata_json FROM raw_objects")
            if json.loads(row["metadata_json"]).get("filename") == "current.txt"
        ]
        assert len(current) == 1
        facts = {
            "schema": "friday.mixed-journey-source-facts.v1",
            "authenticated_turn_id": metadata["_mixed_journey_source_facts"]["authenticated_turn_id"],
            "files": [{"file_id": current[0], "sha256": _digest("CURRENT TERMS")}],
            "archives": [{"archive_id": archive.id, "sha256": _digest("ARCHIVE TERMS"), "member_count": 1}],
        }
        # Pin opaque durable IDs; source identities and policy fields remain independent literal expectations.
        consumption = {
            "consumption_id": metadata["web_research_consumption"]["consumption_id"],
            "authenticated_turn_id": facts["authenticated_turn_id"],
            "usability": "consumable",
            "selected_provider_id": "brave",
            "admitted_source_count": 1,
            "reason": "primary_sources",
        }
        bridge_body = {
            "authorized": True,
            "mixed_required": True,
            "mixed_journey": {
                "conversation_id": conversation["id"],
                "message_format": "plain",
                "_mixed_journey_source_facts": facts,
                "web_research_consumption": consumption,
            },
        }
        foreign_id, secret = "mixed-foreign", "jrc_mixed_delivery_foreign_fixture"
        storage.ensure_user(foreign_id, source="api-token", display_name=foreign_id, preset_key="user")
        storage.create_api_token(
            foreign_id, _digest(secret), label="mixed delivery oracle", created_by="test"
        )
        foreign_conversation = storage.create_conversation(foreign_id, "FOREIGN-MIXED-PRIVATE-TITLE")
        storage.store_message(
            foreign_conversation["id"], foreign_id, "assistant", "FOREIGN-MIXED-PRIVATE-CONTENT"
        )
        yield SimpleNamespace(
            app=app,
            client=client,
            settings=scoped,
            storage=storage,
            model=model,
            web=web,
            owner=owner,
            foreign={"Authorization": f"Bearer {secret}"},
            foreign_id=foreign_id,
            conversation_id=conversation["id"],
            message_id=message_id,
            archive_id=archive.id,
            path=f"/api/me/mixed-deliveries/{message_id}?answer_sha256={_digest(_ANSWER)}",
            bridge_body=bridge_body,
        )


def test_mixed_delivery_http_preserves_exact_owner_and_bridge_projection(delivery_turn):
    state = delivery_turn
    before = _snapshot(state)
    _assert_body(state.client.get(state.path, headers=state.owner), {"authorized": True})
    _assert_body(_bridge_get(state.client, state.settings, state.path), state.bridge_body)
    _assert_unchanged(state, before)
    # The same public route classifies an owned ordinary durable answer for the bridge only.
    ordinary = state.storage.store_message(
        state.conversation_id,
        LEGACY_OWNER_USER_ID,
        "assistant",
        "ORDINARY-OWNED-ANSWER",
        metadata={},
    )
    path = f"/api/me/mixed-deliveries/{ordinary['id']}?answer_sha256={_digest('ORDINARY-OWNED-ANSWER')}"
    before = _snapshot(state)
    _assert_body(
        _bridge_get(state.client, state.settings, path),
        {
            "authorized": True,
            "mixed_required": False,
            "conversation_id": state.conversation_id,
        },
    )
    _assert_body(state.client.get(path, headers=state.owner), {"authorized": False})
    _assert_unchanged(state, before)


@pytest.mark.parametrize(
    "refusal",
    [
        "anonymous",
        "chat-denied",
        "sha-short",
        "sha-charset",
        "sha-missing",
        "wrong-digest",
        "foreign-bearer",
        "foreign-bridge",
        "missing-message",
        "user-message",
        "archive-revoked",
        "files-denied",
    ],
)
def test_mixed_delivery_http_refusals_preserve_content_and_do_not_rerun(delivery_turn, monkeypatch, refusal):
    from friday.organs.mixed_journey import replay

    state = delivery_turn
    headers, path, status = state.owner, state.path, 200
    if refusal == "anonymous":
        headers, status = {}, 401
    elif refusal == "chat-denied":
        state.storage.set_permission_override(state.foreign_id, "chat.use", "deny")
        headers, status = state.foreign, 403
    elif refusal.startswith("sha-"):
        suffix = {"sha-short": "a" * 63, "sha-charset": "g" * 64, "sha-missing": None}[refusal]
        path = state.path.split("?", 1)[0] + (f"?answer_sha256={suffix}" if suffix is not None else "")
        status = 422
    elif refusal == "wrong-digest":
        path = state.path.split("?", 1)[0] + "?answer_sha256=" + "0" * 64
    elif refusal == "foreign-bearer":
        headers = state.foreign
    elif refusal == "missing-message":
        path = "/api/me/mixed-deliveries/missing-owned-answer?answer_sha256=" + _digest(_ANSWER)
    elif refusal == "user-message":
        row = state.storage.execute(
            "SELECT id, content FROM messages WHERE conversation_id=? AND role='user'",
            (state.conversation_id,),
        ).fetchone()
        path = f"/api/me/mixed-deliveries/{row['id']}?answer_sha256={_digest(row['content'])}"
    elif refusal == "archive-revoked":
        with state.storage.transaction() as conn:
            conn.execute(
                "UPDATE raw_objects SET deleted_at='2026-09-07T00:00:00Z' WHERE id=?", (state.archive_id,)
            )
    elif refusal == "files-denied":
        state.storage.set_permission_override(LEGACY_OWNER_USER_ID, "files.read", "deny")
    calls = []
    original = replay.authorized_mixed_reply_delivery

    def record(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(replay, "authorized_mixed_reply_delivery", record)
    before = _snapshot(state)
    response = (
        _bridge_get(state.client, state.settings, path, user="5002", chat="5002")
        if refusal == "foreign-bridge"
        else state.client.get(path, headers=headers)
    )
    assert response.status_code == status, "delivery_refusal_status"
    if status == 200:
        _assert_body(response, {"authorized": False})
    elif status in {401, 403}:
        expected = "Missing authentication" if status == 401 else "Access denied for chat.use (explicit_deny)"
        assert response.json() == {"detail": expected}, "delivery_refusal_body"
    else:
        body = response.json()
        assert set(body) == {"detail"} and len(body["detail"]) == 1, "delivery_validation_shape"
        assert body["detail"][0]["loc"] == ["query", "answer_sha256"], "delivery_validation_field"
        error = {
            "sha-short": "string_too_short",
            "sha-charset": "string_pattern_mismatch",
            "sha-missing": "missing",
        }[refusal]
        assert body["detail"][0]["type"] == error, "delivery_validation_kind"
    assert len(calls) == (1 if status == 200 else 0), "delivery_refusal_before_authorization"
    assert _ANSWER not in response.text and "FOREIGN-MIXED-PRIVATE" not in response.text
    _assert_unchanged(state, before)


@pytest.mark.parametrize(
    "fault",
    [
        "owner-number",
        "owner-extra",
        "bridge-missing",
        "bridge-wrong-file",
        "bridge-wrong-turn",
        "false-authority",
        "wrong-status",
        "message-write",
    ],
)
def test_mixed_delivery_http_oracles_reject_corrupted_responses_and_writes(delivery_turn, fault):
    state = delivery_turn
    before = _snapshot(state)
    bridge = fault.startswith("bridge-")
    expected = state.bridge_body if bridge else {"authorized": True}
    response = (
        _bridge_get(state.client, state.settings, state.path)
        if bridge
        else state.client.get(state.path, headers=state.owner)
    )
    _assert_body(response, expected)
    _assert_unchanged(state, before)
    if fault == "message-write":
        state.storage.store_message(
            state.conversation_id,
            LEGACY_OWNER_USER_ID,
            "assistant",
            "CORRUPTED-DELIVERY-CONTENT",
            metadata={},
        )
        with pytest.raises(AssertionError, match="delivery_content_state"):
            _assert_unchanged(state, before)
        return
    body = copy.deepcopy(response.json())
    status = 200
    if fault == "owner-number":
        body["authorized"] = 1
    elif fault == "owner-extra":
        body["mixed_journey"] = state.bridge_body["mixed_journey"]
    elif fault == "bridge-missing":
        del body["mixed_journey"]
    elif fault == "bridge-wrong-file":
        body["mixed_journey"]["_mixed_journey_source_facts"]["files"][0]["sha256"] = "0" * 64
    elif fault == "bridge-wrong-turn":
        body["mixed_journey"]["conversation_id"] = "foreign-conversation"
    elif fault == "false-authority":
        denied = state.client.get(state.path, headers=state.foreign)
        _assert_body(denied, {"authorized": False})
        expected, body = {"authorized": False}, {"authorized": True}
    elif fault == "wrong-status":
        status = 403
    corrupted = httpx.Response(status, json=body, request=response.request)
    with pytest.raises(AssertionError, match="delivery_http_"):
        _assert_body(corrupted, expected)
    _assert_unchanged(state, before)


def _replace_delivery_metadata(state, metadata):
    with state.storage.transaction() as conn:
        conn.execute(
            "UPDATE messages SET metadata_json=? WHERE id=?", (json.dumps(metadata), state.message_id)
        )


@pytest.mark.parametrize(
    "fault",
    [
        "facts-schema",
        "facts-extra",
        "file-extra",
        "archive-bool",
        "facts-turn",
        "web-extra",
        "web-count-bool",
        "web-id-private",
        "web-state",
        "web-turn",
        "format",
    ],
)
def test_mixed_delivery_http_refuses_unclosed_durable_projection(delivery_turn, fault):
    state = delivery_turn
    metadata = json.loads(state.storage.get_message(state.message_id, LEGACY_OWNER_USER_ID)["metadata_json"])
    facts, web = metadata["_mixed_journey_source_facts"], metadata["web_research_consumption"]
    if fault == "facts-schema":
        facts["schema"] = "future-unrecognized-schema"
    elif fault == "facts-extra":
        facts["private_body"] = "PRIVATE-METADATA-CANARY"
    elif fault == "file-extra":
        facts["files"][0]["private_body"] = "PRIVATE-METADATA-CANARY"
    elif fault == "archive-bool":
        facts["archives"][0]["member_count"] = True
    elif fault == "facts-turn":
        facts["authenticated_turn_id"] = "another-turn"
    elif fault == "web-extra":
        web["private_body"] = "PRIVATE-METADATA-CANARY"
    elif fault == "web-count-bool":
        web["admitted_source_count"] = True
    elif fault == "web-id-private":
        web["consumption_id"] = "/private/provider/path"
    elif fault == "web-state":
        web["usability"] = "unknown"
    elif fault == "web-turn":
        web["authenticated_turn_id"] = "another-turn"
    else:
        metadata["message_format"] = "untrusted-format"
    _replace_delivery_metadata(state, metadata)
    before = _snapshot(state)
    _assert_body(_bridge_get(state.client, state.settings, state.path), {"authorized": False})
    _assert_body(state.client.get(state.path, headers=state.owner), {"authorized": False})
    _assert_unchanged(state, before)


@pytest.mark.parametrize("lane", ["mixed", "ordinary"])
@pytest.mark.parametrize("revoke", ["override", "status", "preset", "custom-grant"])
def test_mixed_delivery_http_rechecks_chat_after_authentication(delivery_turn, monkeypatch, lane, revoke):
    from friday.organs.mixed_journey import replay

    state = delivery_turn
    path = state.path
    if lane == "ordinary":
        message = state.storage.store_message(
            state.conversation_id, LEGACY_OWNER_USER_ID, "assistant", "ORDINARY"
        )
        path = f"/api/me/mixed-deliveries/{message['id']}?answer_sha256={_digest('ORDINARY')}"
    auth = state.app.state.auth_service
    if revoke in {"preset", "custom-grant"}:
        # Keep file access granted, so losing chat.use alone must veto delivery.
        auth.create_custom_preset("delivery-reader", "Reader", {"files.read", "chat.use"}, created_by="test")
        auth.create_custom_preset("delivery-files-only", "Files", {"files.read"}, created_by="test")
        if revoke == "custom-grant":
            auth.set_user_preset(LEGACY_OWNER_USER_ID, "delivery-reader")
    original = replay.authorized_mixed_reply_delivery
    observations = []

    def after_authentication(*args, **kwargs):
        # Public route authentication/_require has already admitted this actor.
        if revoke == "override":
            state.storage.set_permission_override(LEGACY_OWNER_USER_ID, "chat.use", "deny")
        elif revoke == "status":
            state.storage.update_user(LEGACY_OWNER_USER_ID, status="disabled")
        elif revoke == "preset":
            auth.set_user_preset(LEGACY_OWNER_USER_ID, "delivery-files-only")
        else:
            auth.create_custom_preset("delivery-reader", "Reader", {"files.read"}, created_by="test")
        observations.append(_snapshot(state))
        return original(*args, **kwargs)

    monkeypatch.setattr(replay, "authorized_mixed_reply_delivery", after_authentication)
    _assert_body(_bridge_get(state.client, state.settings, path), {"authorized": False})
    assert len(observations) == 1
    _assert_unchanged(state, observations[0])


@pytest.mark.parametrize("change", ["projection", "source-receipt", "chat-override"])
def test_mixed_delivery_http_refuses_changes_after_source_preparation(delivery_turn, monkeypatch, change):
    from friday.organs.mixed_journey import replay

    state = delivery_turn
    original = replay.prepare_registered_file_evidence
    observations = []

    def change_after_preparation(*args, **kwargs):
        prepared = original(*args, **kwargs)
        if change == "chat-override":
            state.storage.set_permission_override(LEGACY_OWNER_USER_ID, "chat.use", "deny")
        else:
            metadata = json.loads(
                state.storage.get_message(state.message_id, LEGACY_OWNER_USER_ID)["metadata_json"]
            )
            facts = metadata["_mixed_journey_source_facts"]
            if change == "projection":
                facts["authenticated_turn_id"] = "replacement-turn"
                metadata["web_research_consumption"].update(
                    consumption_id="replacement-turn", authenticated_turn_id="replacement-turn"
                )
            else:
                receipt = metadata["mixed_file_archive_web_sources_v1"]
                receipt["current"], receipt["archive"] = receipt["archive"], receipt["current"]
                receipt["archive_filename"] = ""
                facts["files"] = [
                    {
                        "file_id": receipt["current"]["raw_object_id"],
                        "sha256": receipt["current"]["content_sha256"],
                    }
                ]
                facts["archives"] = [
                    {
                        "archive_id": receipt["archive"]["raw_object_id"],
                        "sha256": receipt["archive"]["content_sha256"],
                        "member_count": 1,
                    }
                ]
            _replace_delivery_metadata(state, metadata)
        observations.append(_snapshot(state))
        return prepared

    monkeypatch.setattr(replay, "prepare_registered_file_evidence", change_after_preparation)
    _assert_body(_bridge_get(state.client, state.settings, state.path), {"authorized": False})
    assert len(observations) == 1, "real final-snapshot change was exercised"
    _assert_unchanged(state, observations[0])


@pytest.mark.parametrize("web_state", ["consumable_degraded", "unavailable"])
def test_mixed_delivery_http_retains_closed_partial_web_delivery(delivery_turn, web_state):
    state = delivery_turn
    metadata = json.loads(state.storage.get_message(state.message_id, LEGACY_OWNER_USER_ID)["metadata_json"])
    metadata["web_research_consumption"].update(
        usability=web_state,
        reason="partial_sources" if web_state == "consumable_degraded" else "provider_unavailable",
        selected_provider_id="brave" if web_state == "consumable_degraded" else None,
        admitted_source_count=1 if web_state == "consumable_degraded" else 0,
    )
    _replace_delivery_metadata(state, metadata)
    expected = copy.deepcopy(state.bridge_body)
    expected["mixed_journey"]["web_research_consumption"] = metadata["web_research_consumption"]
    before = _snapshot(state)
    _assert_body(_bridge_get(state.client, state.settings, state.path), expected)
    _assert_body(state.client.get(state.path, headers=state.owner), {"authorized": True})
    _assert_unchanged(state, before)
