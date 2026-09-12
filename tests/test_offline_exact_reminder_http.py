"""HTTP regression for exact reminders while the primary model is disabled."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from tests.test_api_vertical_slice import _bridge_request

_CANARY = "OFFLINE-REMINDER-HTTP-CANARY-089"
_DATE = "2035-09-05"
_MESSAGE = f"На 5 сентября 2035 года поставь напоминание «{_CANARY}»."


@pytest.fixture
def offline_owner_chat(settings, monkeypatch: pytest.MonkeyPatch):
    from friday.server import create_app

    configured = replace(
        settings,
        shared_archive=False,
        llm_enabled=False,
        embeddings_enabled=False,
        verify_answers=False,
        workers_enabled=False,
        reminders_enabled=True,
        quiet_hours_start=0,
        quiet_hours_end=0,
        telegram_allowed_chat_ids=[5001],
        telegram_owner_chat_ids=[5001],
        telegram_open_registration=False,
    )
    app = create_app(configured)
    model_calls: list[str] = []

    async def forbidden_model(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        model_calls.append(str(kwargs.get("priority") or "unspecified"))
        pytest.fail("offline exact-reminder HTTP path called a classifier or response model")

    with TestClient(app) as client:
        monkeypatch.setattr(app.state.agent.llm, "chat", forbidden_model)
        yield {
            "app": app,
            "client": client,
            "settings": configured,
            "model_calls": model_calls,
        }


def _payload(message: str, *, source_ref: str, telegram_message_id: int, enable_tools: bool = True):
    return {
        "message": message,
        "enable_tools": enable_tools,
        "source_ref": source_ref,
        "telegram_message_id": telegram_message_id,
        "telegram_user": {"id": 5001, "first_name": "Owner", "username": "owner"},
    }


def _send(ctx, payload):
    return _bridge_request(ctx["client"], ctx["settings"], "/api/chat", payload)


def _complete_effect_state(storage):
    queries = {
        "entities": "SELECT * FROM entities ORDER BY id",
        "times": "SELECT * FROM entity_time ORDER BY entity_id",
        "owners": "SELECT * FROM private_entity_owners ORDER BY entity_id",
    }
    return {name: [dict(row) for row in storage.execute(sql).fetchall()] for name, sql in queries.items()}


def _owned_assistant_publications(storage):
    rows = storage.execute(
        """
        SELECT id, conversation_id, user_id, role, content, metadata_json, reply_to, created_at
          FROM messages
         WHERE user_id=? AND role='assistant'
         ORDER BY created_at, id
        """,
        (LEGACY_OWNER_USER_ID,),
    ).fetchall()
    return [dict(row) for row in rows]


def _added_rows(before, after, *, key: str):
    before_by_key = {row[key]: row for row in before}
    after_by_key = {row[key]: row for row in after}
    assert set(before_by_key) <= set(after_by_key)
    assert {item: after_by_key[item] for item in before_by_key} == before_by_key
    return [row for row in after if row[key] not in before_by_key]


def _one_new_publication(before, after):
    added = _added_rows(before, after, key="id")
    assert len(added) == 1
    return added[0]


def _assert_no_effect(ctx, before) -> None:
    assert _complete_effect_state(ctx["app"].state.storage) == before
    assert ctx["model_calls"] == []


def test_exact_owner_reminder_is_created_and_published_once_while_model_is_off(
    offline_owner_chat,
) -> None:
    ctx = offline_owner_chat
    storage = ctx["app"].state.storage
    source_ref = "telegram-update:89001"
    payload = _payload(_MESSAGE, source_ref=source_ref, telegram_message_id=89001)
    effects_before = _complete_effect_state(storage)
    publications_before = _owned_assistant_publications(storage)

    first = _send(ctx, payload)

    assert first.status_code == 200, first.text
    first_data = first.json()
    assert first_data["tools_used"] == ["remind"]
    assert first_data.get("idempotent_replay") is not True
    assert first_data["message"].count(_CANARY) == 1
    assert first_data["message"].startswith(f"Напоминание поставлено: «{_CANARY}»")
    assert "Доставка в чат запланирована." in first_data["message"]
    assert ctx["model_calls"] == []

    effects_after = _complete_effect_state(storage)
    entities = _added_rows(effects_before["entities"], effects_after["entities"], key="id")
    times = _added_rows(effects_before["times"], effects_after["times"], key="entity_id")
    owners = _added_rows(effects_before["owners"], effects_after["owners"], key="entity_id")
    assert len(entities) == len(times) == len(owners) == 1

    entity = entities[0]
    entity_id = entity["id"]
    assert {
        "user_id": entity["user_id"],
        "name": entity["name"],
        "entity_type": entity["entity_type"],
    } == {
        "user_id": LEGACY_OWNER_USER_ID,
        "name": _CANARY,
        "entity_type": "event",
    }
    assert {
        "entity_id": times[0]["entity_id"],
        "user_id": times[0]["user_id"],
        "occurred_at": times[0]["occurred_at"],
        "occurred_end": times[0]["occurred_end"],
        "precision": times[0]["precision"],
        "source": times[0]["source"],
    } == {
        "entity_id": entity_id,
        "user_id": LEGACY_OWNER_USER_ID,
        "occurred_at": _DATE,
        "occurred_end": None,
        "precision": "day",
        "source": f"reminder:{LEGACY_OWNER_USER_ID}",
    }
    assert {
        "entity_id": owners[0]["entity_id"],
        "person_id": owners[0]["person_id"],
        "privacy_kind": owners[0]["privacy_kind"],
    } == {
        "entity_id": entity_id,
        "person_id": LEGACY_OWNER_USER_ID,
        "privacy_kind": "reminder",
    }

    publications_after = _owned_assistant_publications(storage)
    publication = _one_new_publication(publications_before, publications_after)
    assert publication["user_id"] == LEGACY_OWNER_USER_ID
    assert publication["role"] == "assistant"
    assert publication["content"] == first_data["message"]
    assert json.loads(publication["metadata_json"])["tools_used"] == ["remind"]

    replay = _send(ctx, payload)

    assert replay.status_code == 200, replay.text
    replay_data = replay.json()
    assert replay_data["idempotent_replay"] is True
    assert replay_data["message_id"] == first_data["message_id"]
    assert replay_data["conversation_id"] == first_data["conversation_id"]
    assert replay_data["message"] == first_data["message"]
    assert replay_data["tools_used"] == ["remind"]
    assert _complete_effect_state(storage) == effects_after
    assert _owned_assistant_publications(storage) == publications_after
    assert ctx["model_calls"] == []


@pytest.mark.parametrize(
    ("message", "enable_tools", "deny_write", "telegram_message_id"),
    [
        (_MESSAGE, True, True, 89101),
        (_MESSAGE, False, False, 89102),
        (
            f"Не ставь напоминание «{_CANARY}» на 5 сентября 2035 года.",
            True,
            False,
            89103,
        ),
        (
            f"В примере «поставь напоминание на 5 сентября 2035 года» точный текст «{_CANARY}».",
            True,
            False,
            89104,
        ),
        (f"Напомни «{_CANARY}» когда-нибудь.", True, False, 89105),
    ],
    ids=[
        "permission-denied",
        "tools-disabled",
        "negated",
        "quoted",
        "ambiguous-model-off",
    ],
)
def test_offline_controls_never_create_or_claim_a_reminder(
    offline_owner_chat,
    message: str,
    enable_tools: bool,
    deny_write: bool,
    telegram_message_id: int,
) -> None:
    ctx = offline_owner_chat
    storage = ctx["app"].state.storage
    if deny_write:
        storage.set_permission_override(
            LEGACY_OWNER_USER_ID,
            "kg.write",
            "deny",
        )
    effects_before = _complete_effect_state(storage)
    publications_before = _owned_assistant_publications(storage)
    payload = _payload(
        message,
        source_ref=f"telegram-update:{telegram_message_id}",
        telegram_message_id=telegram_message_id,
        enable_tools=enable_tools,
    )

    response = _send(ctx, payload)

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["tools_used"] == []
    assert "Напоминание поставлено" not in data["message"]
    assert "Напоминание сохранено" not in data["message"]
    _assert_no_effect(ctx, effects_before)
    publication = _one_new_publication(
        publications_before,
        _owned_assistant_publications(storage),
    )
    assert publication["content"] == data["message"]
    assert json.loads(publication["metadata_json"])["tools_used"] == []
