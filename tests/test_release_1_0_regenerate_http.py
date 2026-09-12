"""Release 1.0 HTTP oracles for one real ``POST /api/me/regenerate`` path.

The positive nodes keep the server route, authentication, AgentRuntime and SQLite
storage intact.  Only the final language-model boundary is scripted.  The refusal
nodes use the same app and prove that no agent/model or idempotency effect is
reachable before the exact public rejection.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.server import create_app

_LATEST_QUESTION = "какая столица у Франции?"
_EARLIER_QUESTION = "какая столица у Италии?"


class _ScriptedFinalModel:
    """Use the established real-AgentRuntime model seam and record every call."""

    enabled = True
    model = "release-1.0-regenerate-scripted-final"
    total_budget_sec = 5.0

    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, Any], ...]] = []
        self.final_calls: list[tuple[dict[str, Any], ...]] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        frozen = tuple(dict(item) for item in messages)
        self.calls.append(frozen)
        joined = " ".join(str(item.get("content") or "") for item in messages)
        if "РАЗГОВОР или ЗАПРОС" in joined:
            return {"content": "ЗАПРОС", "tool_calls": None}
        if '"вид": "интернет' in joined:
            return {
                "content": '{"вид":"знание","правило":"","запрос":"","кто":"","дни":[]}',
                "tool_calls": None,
            }
        self.final_calls.append(frozen)
        return {
            "content": f"Синтетическая альтернатива {len(self.final_calls)}.",
            "tool_calls": None,
            "_queue_wait_sec": 0.0,
        }


def _tuned(settings: Any, *, shared_archive: bool = False) -> Any:
    return replace(
        settings,
        shared_archive=shared_archive,
        verify_answers=False,
        workers_enabled=False,
    )


def _owner_headers(settings: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.api_token}"}


def _issue_scoped_token(storage: Any, user_id: str, secret: str) -> dict[str, str]:
    storage.ensure_user(
        user_id,
        source="api-token",
        display_name=user_id,
        preset_key="user",
    )
    storage.update_user(user_id, preset_key="user")
    storage.create_api_token(
        user_id,
        hashlib.sha256(secret.encode("utf-8")).hexdigest(),
        label="regenerate oracle",
        created_by="test",
    )
    return {"Authorization": f"Bearer {secret}"}


def _seed_dialogue(storage: Any, user_id: str, *, title: str) -> tuple[dict[str, Any], dict[str, Any]]:
    conversation = storage.create_conversation(user_id, title=title)
    conversation_id = str(conversation["id"])
    earlier = storage.store_message(
        conversation_id,
        user_id,
        "user",
        _EARLIER_QUESTION,
        metadata={"tools_enabled": False, "interaction_mode": "dialogue"},
    )
    earlier_answer = storage.store_message(
        conversation_id,
        user_id,
        "assistant",
        "Рим.",
        reply_to=str(earlier["id"]),
    )
    latest = storage.store_message(
        conversation_id,
        user_id,
        "user",
        _LATEST_QUESTION,
        metadata={"tools_enabled": False, "interaction_mode": "dialogue"},
        reply_to=str(earlier_answer["id"]),
    )
    storage.store_message(
        conversation_id,
        user_id,
        "assistant",
        "Париж, первый ответ.",
        reply_to=str(latest["id"]),
    )
    return conversation, latest


def _messages(storage: Any, conversation_id: str, user_id: str) -> list[dict[str, Any]]:
    return storage.get_conversation_messages(
        conversation_id,
        user_id=user_id,
        limit=1000,
    )


def _all_message_rows(storage: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in storage.execute("SELECT * FROM messages ORDER BY rowid").fetchall()]


def _idempotency_rows(storage: Any) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in storage.execute(
            "SELECT * FROM request_idempotency ORDER BY user_id, request_key"
        ).fetchall()
    ]


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    decoded = json.loads(str(row.get("metadata_json") or "{}"))
    assert isinstance(decoded, dict)
    return decoded


def _assert_real_appended_pair(
    *,
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    body: dict[str, Any],
    owner_id: str,
    conversation_id: str,
    expected_answer: str,
    parent_id: str,
    root_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    assert after[: len(before)] == before
    assert len(after) == len(before) + 2
    replay_user, replay_assistant = after[-2:]
    assert {
        "role": replay_user["role"],
        "content": replay_user["content"],
        "user_id": replay_user["user_id"],
        "conversation_id": replay_user["conversation_id"],
    } == {
        "role": "user",
        "content": _LATEST_QUESTION,
        "user_id": owner_id,
        "conversation_id": conversation_id,
    }
    assert {
        "role": replay_assistant["role"],
        "content": replay_assistant["content"],
        "user_id": replay_assistant["user_id"],
        "conversation_id": replay_assistant["conversation_id"],
        "reply_to": replay_assistant["reply_to"],
    } == {
        "role": "assistant",
        "content": expected_answer,
        "user_id": owner_id,
        "conversation_id": conversation_id,
        "reply_to": replay_user["id"],
    }
    replay_metadata = _metadata(replay_user)
    assert replay_metadata["regenerate_parent_user_message_id"] == parent_id
    assert replay_metadata["regenerate_root_user_message_id"] == root_id
    assert replay_metadata["tools_enabled"] is False
    assert replay_metadata["interaction_mode"] == "dialogue"
    assert body["conversation_id"] == conversation_id
    assert body["message_id"] == replay_assistant["id"]
    assert body["message"] == expected_answer
    return replay_user, replay_assistant


def _assert_final_model_saw_exact_question(model: _ScriptedFinalModel, index: int) -> None:
    final = model.final_calls[index]
    assert any(
        item.get("role") == "user" and str(item.get("content") or "") == _LATEST_QUESTION for item in final
    )


def test_regenerate_http_real_runtime_replays_exact_last_user_and_appends_lineage(settings: Any) -> None:
    app = create_app(_tuned(settings))
    model = _ScriptedFinalModel()
    headers = _owner_headers(settings)

    with TestClient(app) as client:
        app.state.agent.llm = model
        storage = app.state.storage
        conversation, source = _seed_dialogue(
            storage,
            LEGACY_OWNER_USER_ID,
            title="exact last own user",
        )
        conversation_id = str(conversation["id"])
        before = _messages(storage, conversation_id, LEGACY_OWNER_USER_ID)

        response = client.post(
            "/api/me/regenerate",
            json={"conversation_id": conversation_id, "operation_id": "regen-last-001"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        after = _messages(storage, conversation_id, LEGACY_OWNER_USER_ID)

        _assert_real_appended_pair(
            before=before,
            after=after,
            body=body,
            owner_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            expected_answer="Синтетическая альтернатива 1.",
            parent_id=str(source["id"]),
            root_id=str(source["id"]),
        )
        assert after[-2]["content"] != _EARLIER_QUESTION
        assert len(model.final_calls) == 1
        _assert_final_model_saw_exact_question(model, 0)
        claim_rows = _idempotency_rows(storage)
        assert len(claim_rows) == 1
        assert claim_rows[0]["user_id"] == LEGACY_OWNER_USER_ID
        assert claim_rows[0]["state"] == "complete"
        assert claim_rows[0]["lease_token"] == ""


def test_regenerate_http_operation_replay_is_effect_free_and_new_operation_is_alternative(
    settings: Any,
) -> None:
    app = create_app(_tuned(settings))
    model = _ScriptedFinalModel()
    headers = _owner_headers(settings)

    with TestClient(app) as client:
        app.state.agent.llm = model
        storage = app.state.storage
        conversation, root = _seed_dialogue(
            storage,
            LEGACY_OWNER_USER_ID,
            title="operation identity",
        )
        conversation_id = str(conversation["id"])
        baseline = _messages(storage, conversation_id, LEGACY_OWNER_USER_ID)
        first_payload = {"conversation_id": conversation_id, "operation_id": "regen-click-A"}

        first = client.post("/api/me/regenerate", json=first_payload, headers=headers)
        assert first.status_code == 200, first.text
        first_body = first.json()
        after_first = _messages(storage, conversation_id, LEGACY_OWNER_USER_ID)
        first_replay_user, _ = _assert_real_appended_pair(
            before=baseline,
            after=after_first,
            body=first_body,
            owner_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            expected_answer="Синтетическая альтернатива 1.",
            parent_id=str(root["id"]),
            root_id=str(root["id"]),
        )
        claims_after_first = _idempotency_rows(storage)
        calls_after_first = tuple(model.calls)

        same_operation = client.post(
            "/api/me/regenerate",
            json=first_payload,
            headers=headers,
        )
        assert same_operation.status_code == 200, same_operation.text
        assert same_operation.json() == {**first_body, "idempotent_replay": True}
        assert _messages(storage, conversation_id, LEGACY_OWNER_USER_ID) == after_first
        assert _idempotency_rows(storage) == claims_after_first
        assert tuple(model.calls) == calls_after_first

        second = client.post(
            "/api/me/regenerate",
            json={"conversation_id": conversation_id, "operation_id": "regen-click-B"},
            headers=headers,
        )
        assert second.status_code == 200, second.text
        second_body = second.json()
        after_second = _messages(storage, conversation_id, LEGACY_OWNER_USER_ID)
        _assert_real_appended_pair(
            before=after_first,
            after=after_second,
            body=second_body,
            owner_id=LEGACY_OWNER_USER_ID,
            conversation_id=conversation_id,
            expected_answer="Синтетическая альтернатива 2.",
            parent_id=str(first_replay_user["id"]),
            root_id=str(root["id"]),
        )
        assert len(model.final_calls) == 2
        _assert_final_model_saw_exact_question(model, 1)
        claims_after_second = _idempotency_rows(storage)
        assert len(claims_after_second) == 2
        expected_keys = {
            f"regenerate:{conversation_id}:operation:{hashlib.sha256(operation.encode('ascii')).hexdigest()}"
            for operation in ("regen-click-A", "regen-click-B")
        }
        assert {row["request_key"] for row in claims_after_second} == expected_keys
        assert {row["state"] for row in claims_after_second} == {"complete"}
        assert {row["lease_token"] for row in claims_after_second} == {""}


def test_regenerate_http_shared_archive_keeps_conversations_person_owned(settings: Any) -> None:
    app = create_app(_tuned(settings, shared_archive=True))
    model = _ScriptedFinalModel()

    with TestClient(app) as client:
        app.state.agent.llm = model
        storage = app.state.storage
        alice_headers = _issue_scoped_token(storage, "alice", "jrc_regenerate_alice")
        bob_headers = _issue_scoped_token(storage, "bob", "jrc_regenerate_bob")
        alice_conversation, alice_root = _seed_dialogue(
            storage,
            "alice",
            title="alice private conversation",
        )
        alice_conversation_id = str(alice_conversation["id"])
        bob_conversation, _ = _seed_dialogue(
            storage,
            "bob",
            title="bob private canary",
        )
        bob_conversation_id = str(bob_conversation["id"])
        alice_before = _messages(storage, alice_conversation_id, "alice")
        bob_before = _messages(storage, bob_conversation_id, "bob")
        idempotency_before = _idempotency_rows(storage)

        foreign = client.post(
            "/api/me/regenerate",
            json={"conversation_id": alice_conversation_id, "operation_id": "foreign-click"},
            headers=bob_headers,
        )
        assert foreign.status_code == 400
        assert foreign.json() == {"detail": "Разговор не найден"}
        assert _messages(storage, alice_conversation_id, "alice") == alice_before
        assert _messages(storage, bob_conversation_id, "bob") == bob_before
        assert _idempotency_rows(storage) == idempotency_before
        assert model.calls == []

        own = client.post(
            "/api/me/regenerate",
            json={"conversation_id": alice_conversation_id, "operation_id": "alice-click"},
            headers=alice_headers,
        )
        assert own.status_code == 200, own.text
        alice_after = _messages(storage, alice_conversation_id, "alice")
        _assert_real_appended_pair(
            before=alice_before,
            after=alice_after,
            body=own.json(),
            owner_id="alice",
            conversation_id=alice_conversation_id,
            expected_answer="Синтетическая альтернатива 1.",
            parent_id=str(alice_root["id"]),
            root_id=str(alice_root["id"]),
        )
        assert _messages(storage, bob_conversation_id, "bob") == bob_before
        assert storage.get_conversation(alice_conversation_id, "bob") is None
        assert storage.get_conversation(alice_conversation_id, LEGACY_OWNER_USER_ID) is None
        assert all(row["user_id"] == "alice" for row in alice_after)
        claim_rows = _idempotency_rows(storage)
        assert len(claim_rows) == len(idempotency_before) + 1
        assert claim_rows[-1]["user_id"] == "alice"
        assert claim_rows[-1]["state"] == "complete"
        _assert_final_model_saw_exact_question(model, 0)


def test_regenerate_http_missing_auth_stops_before_agent_and_idempotency(settings: Any) -> None:
    app = create_app(_tuned(settings))
    model = _ScriptedFinalModel()

    with TestClient(app) as client:
        app.state.agent.llm = model
        storage = app.state.storage
        conversation, _ = _seed_dialogue(
            storage,
            LEGACY_OWNER_USER_ID,
            title="anonymous refusal",
        )
        messages_before = _all_message_rows(storage)
        claims_before = _idempotency_rows(storage)

        response = client.post(
            "/api/me/regenerate",
            json={"conversation_id": conversation["id"], "operation_id": "anon-click"},
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "Missing authentication"}
        assert _all_message_rows(storage) == messages_before
        assert _idempotency_rows(storage) == claims_before
        assert model.calls == []


def test_regenerate_http_explicit_chat_deny_stops_before_agent_and_idempotency(settings: Any) -> None:
    app = create_app(_tuned(settings))
    model = _ScriptedFinalModel()

    with TestClient(app) as client:
        app.state.agent.llm = model
        storage = app.state.storage
        headers = _issue_scoped_token(storage, "denied-user", "jrc_regenerate_denied")
        conversation, _ = _seed_dialogue(
            storage,
            "denied-user",
            title="permission refusal",
        )
        storage.set_permission_override("denied-user", "chat.use", "deny")
        messages_before = _all_message_rows(storage)
        claims_before = _idempotency_rows(storage)

        response = client.post(
            "/api/me/regenerate",
            json={"conversation_id": conversation["id"], "operation_id": "denied-click"},
            headers=headers,
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "Access denied for chat.use (explicit_deny)"}
        assert _all_message_rows(storage) == messages_before
        assert _idempotency_rows(storage) == claims_before
        assert model.calls == []


def test_regenerate_http_missing_conversation_stops_before_agent_and_idempotency(settings: Any) -> None:
    app = create_app(_tuned(settings))
    model = _ScriptedFinalModel()

    with TestClient(app) as client:
        app.state.agent.llm = model
        storage = app.state.storage
        messages_before = _all_message_rows(storage)
        claims_before = _idempotency_rows(storage)

        response = client.post(
            "/api/me/regenerate",
            json={"operation_id": "missing-conversation-click"},
            headers=_owner_headers(settings),
        )
        assert response.status_code == 400
        assert response.json() == {"detail": "Нет активного разговора для повтора"}
        assert _all_message_rows(storage) == messages_before
        assert _idempotency_rows(storage) == claims_before
        assert model.calls == []


def test_regenerate_http_empty_conversation_stops_before_agent_and_idempotency(settings: Any) -> None:
    app = create_app(_tuned(settings))
    model = _ScriptedFinalModel()

    with TestClient(app) as client:
        app.state.agent.llm = model
        storage = app.state.storage
        conversation = storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="empty regenerate conversation",
        )
        messages_before = _all_message_rows(storage)
        claims_before = _idempotency_rows(storage)

        response = client.post(
            "/api/me/regenerate",
            json={"conversation_id": conversation["id"], "operation_id": "empty-click"},
            headers=_owner_headers(settings),
        )
        assert response.status_code == 400
        assert response.json() == {"detail": "В разговоре нет вопроса для повтора"}
        assert _all_message_rows(storage) == messages_before
        assert _idempotency_rows(storage) == claims_before
        assert model.calls == []
