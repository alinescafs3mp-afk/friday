"""Structural message reply edges are scoped facts, never quoted-text guesses."""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.agent_runtime import AgentRuntime
from friday.execution_kernel import ExecutionKernel
from friday.permissions import ActorContext, AuthorizationService
from friday.security import sign_bridge_request


class _NoModel:
    enabled = True
    model = "must-not-run"
    total_budget_sec = 1.0

    async def chat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("structural reply-edge acceptance reached the model")


class _MainPathModel:
    enabled = True
    model = "synthetic-main-path-model"
    total_budget_sec = 5.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        asked = " ".join(str(item.get("content") or "") for item in messages)
        if "РАЗГОВОР или ЗАПРОС" in asked:
            return {"content": "ЗАПРОС"}
        if '"вид": "интернет' in asked:
            return {"content": ('{"вид":"знание","правило":"","запрос":"","кто":"","дни":[]}')}
        return {"content": "Синтетический ответ основной модели."}


def _runtime(
    settings: Any,
    storage: Any,
    *,
    llm: Any | None = None,
) -> tuple[AgentRuntime, ActorContext]:
    storage.ensure_user("alice", preset_key="owner")
    authorization = AuthorizationService(storage)
    tuned = replace(settings, verify_answers=False)
    runtime = AgentRuntime(
        tuned,
        storage,
        llm=llm or _NoModel(),  # type: ignore[arg-type]
        kernel=ExecutionKernel(authorization, tuned),
    )
    return runtime, authorization.actor_for_user("alice", source="test")


def _last_turn(storage: Any, conversation_id: str, user_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = storage.get_conversation_messages(conversation_id, user_id=user_id, limit=1000)
    assert len(rows) >= 2
    user, assistant = rows[-2:]
    assert user["role"] == "user"
    assert assistant["role"] == "assistant"
    return user, assistant


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected_answer"),
    [
        ("на связи?", "На связи."),
        ("Стой", "Молчу."),
    ],
)
async def test_runtime_assistant_reply_edge_targets_the_exact_current_user_row(
    settings: Any,
    storage: Any,
    message: str,
    expected_answer: str,
) -> None:
    """Covers both an ordinary no-model turn and the emergency silence route."""

    runtime, actor = _runtime(settings, storage)
    conversation = storage.create_conversation("alice", title="reply edge")
    conversation_id = str(conversation["id"])

    response = await runtime.chat(
        "alice",
        message,
        actor=actor,
        conversation_id=conversation_id,
        enable_tools=False,
    )

    user, assistant = _last_turn(storage, conversation_id, "alice")
    assert response["message"] == expected_answer
    assert user["reply_to"] is None
    assert assistant["reply_to"] == user["id"]


@pytest.mark.asyncio
async def test_main_model_reply_edge_targets_the_exact_current_user_row(
    settings: Any,
    storage: Any,
) -> None:
    model = _MainPathModel()
    runtime, actor = _runtime(settings, storage, llm=model)
    conversation = storage.create_conversation("alice", title="main model reply edge")
    conversation_id = str(conversation["id"])

    response = await runtime.chat(
        "alice",
        "какая столица у Франции?",
        actor=actor,
        conversation_id=conversation_id,
        enable_tools=False,
    )

    user, assistant = _last_turn(storage, conversation_id, "alice")
    metadata = json.loads(str(assistant["metadata_json"] or "{}"))
    assert model.calls > 0
    assert response["message"] == "Синтетический ответ основной модели."
    assert metadata["structural"]["model_spoke"] is True
    assert assistant["reply_to"] == user["id"]


def test_two_concurrent_turns_keep_distinct_exact_reply_edges(settings: Any) -> None:
    from friday.server import create_app

    tuned = replace(settings, verify_answers=False)
    app = create_app(tuned)
    headers = {"Authorization": f"Bearer {tuned.api_token}"}
    prompts = (
        "ты хранишь всю историю переписки?",
        "а mcp какие тебе доступны?",
    )
    with TestClient(app) as client:
        owner = str(client.get("/api/me", headers=headers).json()["actor"]["user_id"])
        conversation = app.state.storage.create_conversation(owner, title="concurrent reply edges")
        conversation_id = str(conversation["id"])

        def issue(message: str) -> Any:
            return client.post(
                "/api/chat",
                json={"message": message, "conversation_id": conversation_id},
                headers=headers,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(issue, prompts))

        assert [response.status_code for response in responses] == [200, 200]
        rows = app.state.storage.get_conversation_messages(
            conversation_id,
            user_id=owner,
            limit=10,
        )
        users = {str(row["id"]): row for row in rows if row["role"] == "user"}
        assistants = [row for row in rows if row["role"] == "assistant"]
        assert {str(row["content"]) for row in users.values()} == set(prompts)
        assert len(assistants) == 2
        assert {str(row["reply_to"]) for row in assistants} == set(users)
        for assistant in assistants:
            parent_id = str(assistant["reply_to"])
            assert users[parent_id]["conversation_id"] == assistant["conversation_id"]
            assert users[parent_id]["user_id"] == assistant["user_id"]


def _signed_bridge_call(
    client: TestClient,
    settings: Any,
    payload: dict[str, Any],
    *,
    external_user_id: str = "1001",
    chat_id: str = "5001",
) -> Any:
    path = "/api/chat"
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": external_user_id,
            "X-Friday-Chat": chat_id,
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": sign_bridge_request(
                settings.telegram_bridge_secret,
                timestamp=timestamp,
                method="POST",
                path=path,
                external_user_id=external_user_id,
                chat_id=chat_id,
                nonce=nonce,
                body=body,
            ),
        },
    )


def _bridge_identity(client: TestClient, settings: Any) -> str:
    path = "/api/me"
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    response = client.get(
        path,
        headers={
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": "1001",
            "X-Friday-Chat": "5001",
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": sign_bridge_request(
                settings.telegram_bridge_secret,
                timestamp=timestamp,
                method="GET",
                path=path,
                external_user_id="1001",
                chat_id="5001",
                nonce=nonce,
                body=b"",
            ),
        },
    )
    assert response.status_code == 200, response.text
    return str(response.json()["actor"]["user_id"])


def test_trusted_bridge_reply_pointer_is_an_owned_assistant_edge_or_null(settings: Any) -> None:
    """Transport input may select one assistant row, never cross a chat boundary."""

    from friday.server import create_app

    tuned = replace(settings, verify_answers=False, shared_archive=True)
    app = create_app(tuned)
    with TestClient(app) as client:
        app.state.agent.llm = _NoModel()
        owner = _bridge_identity(client, tuned)
        storage = app.state.storage
        storage.update_user(owner, preset_key="user")
        conversation = storage.create_conversation(owner, title="trusted reply edge")
        conversation_id = str(conversation["id"])
        valid_parent = storage.store_message(
            conversation_id,
            owner,
            "assistant",
            "valid parent",
        )
        user_role_parent = storage.store_message(
            conversation_id,
            owner,
            "user",
            "not an assistant parent",
        )
        other_conversation = storage.create_conversation(owner, title="other conversation")
        other_parent = storage.store_message(
            str(other_conversation["id"]),
            owner,
            "assistant",
            "other conversation parent",
        )
        storage.ensure_user("foreign-person", preset_key="user")
        foreign_conversation = storage.create_conversation("foreign-person", title="foreign")
        foreign_parent = storage.store_message(
            str(foreign_conversation["id"]),
            "foreign-person",
            "assistant",
            "foreign parent",
        )
        storage.set_channel_conversation(owner, "telegram", "5001", conversation_id)
        cases = (
            ("valid", str(valid_parent["id"]), str(valid_parent["id"])),
            ("other-conversation", str(other_parent["id"]), None),
            ("foreign-owner", str(foreign_parent["id"]), None),
            ("user-role", str(user_role_parent["id"]), None),
            ("invalid", "msg_0000000000000000", None),
        )

        for index, (label, source_message_id, expected_parent) in enumerate(cases, start=1):
            response = _signed_bridge_call(
                client,
                tuned,
                {
                    "message": "Стой",
                    "conversation_id": conversation_id,
                    "source_ref": f"telegram-update:reply-edge-{index}",
                    "telegram_message_id": 800 + index,
                    "telegram_user": {"id": 1001, "first_name": "Alice"},
                    "reply_source_message_id": source_message_id,
                },
            )
            assert response.status_code == 200, f"{label}: {response.text}"
            user, assistant = _last_turn(storage, conversation_id, owner)
            assert user["reply_to"] == expected_parent, label
            assert assistant["reply_to"] == user["id"], label


def test_storage_never_keeps_a_cross_scope_reply_parent(storage: Any) -> None:
    """The low-level durable sink either rejects or clears every invalid edge."""

    storage.ensure_user("alice")
    storage.ensure_user("bob")
    conversation = storage.create_conversation("alice", title="target")
    conversation_id = str(conversation["id"])
    valid_parent = storage.store_message(conversation_id, "alice", "assistant", "valid")
    valid_child = storage.store_message(
        conversation_id,
        "alice",
        "user",
        "valid child",
        reply_to=str(valid_parent["id"]),
    )
    assert valid_child["reply_to"] == valid_parent["id"]

    other_conversation = storage.create_conversation("alice", title="other")
    other_parent = storage.store_message(
        str(other_conversation["id"]),
        "alice",
        "assistant",
        "other conversation",
    )
    foreign_conversation = storage.create_conversation("bob", title="foreign")
    foreign_parent = storage.store_message(
        str(foreign_conversation["id"]),
        "bob",
        "assistant",
        "foreign owner",
    )

    for label, parent_id in (
        ("other-conversation", str(other_parent["id"])),
        ("foreign-owner", str(foreign_parent["id"])),
        ("missing", "msg_ffffffffffffffff"),
    ):
        try:
            stored = storage.store_message(
                conversation_id,
                "alice",
                "user",
                f"invalid child: {label}",
                reply_to=parent_id,
            )
        except ValueError:
            stored = None
        if stored is not None:
            assert stored["reply_to"] is None, label
        leaked = storage.execute(
            "SELECT 1 FROM messages WHERE conversation_id=? AND user_id=? AND reply_to=? LIMIT 1",
            (conversation_id, "alice", parent_id),
        ).fetchone()
        assert leaked is None, label
