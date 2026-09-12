from __future__ import annotations

import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from test_api_vertical_slice import _bridge_get
from test_mixed_journey_runtime import _ANSWER, _MESSAGE, _TABLE_ANSWER, _Model
from test_transient_web_comparison import _RecordingWeb, _report, _source
from test_v12_file_evidence_reader import _register

from friday.permissions import LEGACY_OWNER_USER_ID


@pytest.mark.parametrize(
    ("cache_drift", "table_requested"),
    [
        (None, False),
        ("missing_context", False),
        ("changed_mode", False),
        ("missing_identity", False),
        (None, True),
    ],
)
def test_http_mixed_upload_uses_real_runtime_and_replay_rechecks_archive(
    settings, cache_drift, table_requested
) -> None:
    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        storage = app.state.storage
        archive, _ = _register(
            storage,
            settings,
            user_id=LEGACY_OWNER_USER_ID,
            uploaded_by=LEGACY_OWNER_USER_ID,
            filename="archive.txt",
            text="ARCHIVE TERMS",
        )
        conversation_id = str(storage.create_conversation(LEGACY_OWNER_USER_ID, "Mixed HTTP")["id"])
        answer = _TABLE_ANSWER if table_requested else _ANSWER
        message_format = "markdown" if table_requested else "plain"
        model = _Model(answer=answer)
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
        payload = {
            "message": _MESSAGE + (" Ответ оформи таблицей." if table_requested else ""),
            "conversation_id": conversation_id,
            "source_ref": "mixed:http:001",
            "document": {
                "filename": "current.txt",
                "mime_type": "text/plain",
                "content_base64": base64.b64encode(b"CURRENT TERMS").decode("ascii"),
            },
        }
        first = client.post("/api/chat", json=payload, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["message"] == answer
        assert first.json()["message_format"] == message_format
        assert "_mixed_journey_source_facts" not in first.json()
        message_id = first.json()["message_id"]
        digest = hashlib.sha256(answer.encode()).hexdigest()
        delivery_path = f"/api/me/mixed-deliveries/{message_id}?answer_sha256={digest}"
        assert client.get(delivery_path, headers=headers).json() == {"authorized": True}
        assert client.get(
            f"/api/me/mixed-deliveries/{message_id}?answer_sha256={'0' * 64}", headers=headers
        ).json() == {"authorized": False}
        assert _bridge_get(client, settings, delivery_path, user="5002", chat="5002").json() == {
            "authorized": False
        }
        assert storage.count_messages(conversation_id, user_id=LEGACY_OWNER_USER_ID) == 2
        rows = storage.execute(
            "SELECT metadata_json FROM messages WHERE conversation_id=? ORDER BY rowid", (conversation_id,)
        ).fetchall()
        publications = [json.loads(row["metadata_json"])["authenticated_turn_publication"] for row in rows]
        assert [item["publication_role"] for item in publications] == ["user", "assistant"]
        assert publications[0]["turn_id"] == publications[1]["turn_id"]
        replay = client.post("/api/chat", json=payload, headers=headers)
        assert replay.status_code == 200, replay.text
        assert replay.json()["message"] == answer
        assert replay.json()["message_format"] == message_format
        assert replay.json()["idempotent_replay"] is True
        assert "_mixed_journey_source_facts" not in replay.json()
    # Reconstruct the application over durable storage: no process ledger,
    # comparison object or fake provider is carried into the replaying app.
    resumed_app = create_app(settings)
    with TestClient(resumed_app) as client:
        storage = resumed_app.state.storage
        restored = client.post("/api/chat", json=payload, headers=headers)
        assert restored.status_code == 200, restored.text
        assert restored.json()["message"] == answer
        assert restored.json()["message_format"] == message_format
        assert restored.json()["idempotent_replay"] is True
        cached = first.json()
        with storage.transaction() as conn:
            conn.execute("UPDATE raw_objects SET deleted_at='2026-09-07T00:00:00Z' WHERE id=?", (archive.id,))
            if cache_drift:
                row = conn.execute(
                    "SELECT response_json FROM request_idempotency WHERE user_id=? AND request_key=?",
                    (LEGACY_OWNER_USER_ID, payload["source_ref"]),
                ).fetchone()
                cached = json.loads(row["response_json"])
                if cache_drift == "changed_mode":
                    cached["context"]["answer_mode"] = "ordinary_dialogue"
                else:
                    cached.pop("context", None)
                if cache_drift == "missing_identity":
                    cached.pop("message_id", None)
                    cached.pop("conversation_id", None)
                conn.execute(
                    "UPDATE request_idempotency SET response_json=? WHERE user_id=? AND request_key=?",
                    (json.dumps(cached), LEGACY_OWNER_USER_ID, payload["source_ref"]),
                )
        assert client.get(delivery_path, headers=headers).json() == {"authorized": False}
        revoked = client.post("/api/chat", json=payload, headers=headers)
        assert revoked.status_code == 200, revoked.text
        assert answer not in revoked.text
        assert revoked.json()["idempotent_replay"] is True
        # Exercise the other HTTP replay entry with the same real durable
        # answer. Only this completed retry-cache row is seeded by the test.
        operation_id = "mixed-replay-boundary-001"
        regenerate_key = (
            f"regenerate:{conversation_id}:operation:{hashlib.sha256(operation_id.encode()).hexdigest()}"
        )
        claim = storage.idempotency_claim(LEGACY_OWNER_USER_ID, regenerate_key)
        assert storage.idempotency_complete(
            LEGACY_OWNER_USER_ID, regenerate_key, claim["lease_token"], cached
        )
        regenerated = client.post(
            "/api/me/regenerate",
            json={"conversation_id": conversation_id, "operation_id": operation_id},
            headers=headers,
        )
        assert regenerated.status_code == 200, regenerated.text
        assert answer not in regenerated.text
        assert regenerated.json()["idempotent_replay"] is True
        assert len(model.calls) == 2 and len(web.calls) == 1
        assert storage.count_messages(conversation_id, user_id=LEGACY_OWNER_USER_ID) == 2
