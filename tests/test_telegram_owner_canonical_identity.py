"""A configured Telegram owner is one account on every persistence boundary.

The regression was silent: a fresh runtime DB had the canonical API owner but no
``user_identities`` row yet.  Telegram authentication therefore invented a
``telegram:...`` person while the shared archive still used the canonical UUID.
Conversation/session/idempotency/audit and raw/tool rows split across those ids,
so the owner appeared to have an empty history even inside one request.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.permissions import LEGACY_OWNER_USER_ID
from friday.security import sign_bridge_request

OWNER_TELEGRAM_ID = "467035772"


def _signed_request(
    client: TestClient,
    settings: Any,
    method: str,
    path: str,
    payload: dict[str, Any],
    *,
    sender: str,
    chat: str,
):
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    return client.request(
        method,
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": sender,
            "X-Friday-Chat": chat,
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": sign_bridge_request(
                settings.telegram_bridge_secret,
                timestamp=timestamp,
                method=method,
                path=path,
                external_user_id=sender,
                chat_id=chat,
                nonce=nonce,
                body=body,
            ),
        },
    )


def _install_bounded_chat(app: Any) -> None:
    """Replace generation only; keep real storage, ingestion and tool audit."""

    storage = app.state.storage
    kernel = app.state.kernel

    async def chat(
        user_id: str,
        message: str,
        *,
        actor: Any,
        conversation_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        # The assertions below are deliberately inside the production call
        # boundary: a projection-only fix cannot make this fake pass.
        assert user_id == LEGACY_OWNER_USER_ID
        assert actor.user_id == LEGACY_OWNER_USER_ID
        assert actor.own_id == LEGACY_OWNER_USER_ID
        conversation = (
            storage.get_conversation(conversation_id, actor.own_id) if conversation_id else None
        ) or storage.create_conversation(actor.own_id, title=message)
        conversation_id = str(conversation["id"])
        storage.store_message(conversation_id, actor.own_id, "user", message)
        tool_result = await kernel.execute("kg_stats", {}, actor=actor)
        assert tool_result.success is True
        assistant = storage.store_message(
            conversation_id,
            actor.own_id,
            "assistant",
            "Синтетическая проверка завершена.",
            {"tools_used": ["kg_stats"]},
        )
        return {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "message_id": assistant["id"],
            "message": assistant["content"],
            "message_format": "markdown",
            "tools_used": ["kg_stats"],
            "context": {"interaction_mode": "dialogue"},
        }

    app.state.agent.chat = chat


@pytest.mark.parametrize("shared_archive", [False, True])
def test_configured_owner_is_canonical_before_every_chat_write(settings, shared_archive: bool) -> None:
    from friday.server import create_app

    tuned = replace(
        settings,
        shared_archive=shared_archive,
        telegram_allowed_chat_ids=[],
        telegram_owner_chat_ids=[int(OWNER_TELEGRAM_ID)],
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        _install_bounded_chat(app)
        source_ref = "telegram-update:synthetic-canonical-owner-1"
        response = _signed_request(
            client,
            tuned,
            "POST",
            "/api/chat",
            {
                "message": "Синтетическая проверка единой личности владельца",
                "source_ref": source_ref,
                "telegram_message_id": 7001,
                "telegram_user": {
                    "id": int(OWNER_TELEGRAM_ID),
                    "first_name": "Владелец",
                    "username": "synthetic_owner",
                },
            },
            sender=OWNER_TELEGRAM_ID,
            chat=OWNER_TELEGRAM_ID,
        )
        assert response.status_code == 200, response.text
        assert response.json()["user_id"] == LEGACY_OWNER_USER_ID

        storage = app.state.storage
        synthetic_id = f"telegram:{tuned.telegram_realm_id}:{OWNER_TELEGRAM_ID}"
        assert storage.resolve_identity("telegram", OWNER_TELEGRAM_ID) == LEGACY_OWNER_USER_ID
        assert storage.get_user(synthetic_id) is None

        session = storage.get_channel_session(
            LEGACY_OWNER_USER_ID,
            "telegram",
            OWNER_TELEGRAM_ID,
        )
        assert session is not None
        conversation_id = str(session["conversation_id"])
        assert storage.get_conversation(conversation_id, LEGACY_OWNER_USER_ID) is not None
        messages = storage.get_conversation_messages(
            conversation_id,
            user_id=LEGACY_OWNER_USER_ID,
        )
        assert [row["role"] for row in messages] == ["user", "assistant"]

        raw = storage.find_raw_by_source_ref(LEGACY_OWNER_USER_ID, "telegram", source_ref)
        assert raw is not None and raw["user_id"] == LEGACY_OWNER_USER_ID
        idempotency = storage.execute(
            "SELECT user_id, response_json FROM request_idempotency WHERE request_key=?",
            (source_ref,),
        ).fetchone()
        assert idempotency is not None
        assert idempotency["user_id"] == LEGACY_OWNER_USER_ID
        assert json.loads(idempotency["response_json"])["conversation_id"] == conversation_id

        tool_audit = storage.execute(
            """SELECT user_id FROM audit_log
               WHERE action='tool.invoke' AND target_id='kg_stats'
               ORDER BY created_at DESC LIMIT 1"""
        ).fetchone()
        assert tool_audit is not None and tool_audit["user_id"] == LEGACY_OWNER_USER_ID

        for table in (
            "channel_sessions",
            "conversations",
            "messages",
            "raw_objects",
            "request_idempotency",
            "audit_log",
        ):
            leaked = storage.execute(
                f'SELECT COUNT(*) AS count FROM "{table}" WHERE user_id=?',  # nosec B608 - fixed tuple
                (synthetic_id,),
            ).fetchone()
            assert int(leaked["count"]) == 0, f"{table} split onto the synthetic identity"


def test_automatic_owner_binding_never_steals_an_explicit_account_link(settings) -> None:
    from friday.server import create_app

    tuned = replace(
        settings,
        shared_archive=False,
        telegram_allowed_chat_ids=[],
        telegram_owner_chat_ids=[int(OWNER_TELEGRAM_ID)],
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        storage = app.state.storage
        storage.ensure_user("separate-account", preset_key="user")
        link = storage.link_identity("Telegram", OWNER_TELEGRAM_ID, "separate-account")
        assert link["source"] == "telegram"

        response = _signed_request(
            client,
            tuned,
            "GET",
            "/api/me",
            {"telegram_user": {"id": int(OWNER_TELEGRAM_ID)}},
            sender=OWNER_TELEGRAM_ID,
            chat=OWNER_TELEGRAM_ID,
        )

        assert response.status_code == 401
        assert storage.resolve_identity("telegram", OWNER_TELEGRAM_ID) == "separate-account"


def test_automatic_owner_binding_detects_a_legacy_case_variant(settings) -> None:
    """An old mixed-case source row cannot be bypassed by a lowercase bootstrap."""

    from friday.server import create_app
    from friday.storage.models import utc_now

    tuned = replace(
        settings,
        shared_archive=False,
        telegram_allowed_chat_ids=[],
        telegram_owner_chat_ids=[int(OWNER_TELEGRAM_ID)],
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        storage = app.state.storage
        storage.ensure_user("separate-account", preset_key="user")
        with storage.transaction() as connection:
            connection.execute(
                """INSERT INTO user_identities(
                       source, external_id, user_id, linked_by, created_at
                   ) VALUES('Telegram', ?, 'separate-account', 'legacy-import', ?)""",
                (OWNER_TELEGRAM_ID, utc_now()),
            )

        response = _signed_request(
            client,
            tuned,
            "GET",
            "/api/me",
            {"telegram_user": {"id": int(OWNER_TELEGRAM_ID)}},
            sender=OWNER_TELEGRAM_ID,
            chat=OWNER_TELEGRAM_ID,
        )

        assert response.status_code == 401
        assert storage.resolve_identity("telegram", OWNER_TELEGRAM_ID) == "separate-account"
        rows = storage.execute(
            """SELECT source, user_id FROM user_identities
               WHERE jericho_casefold(source)='telegram' AND external_id=?""",
            (OWNER_TELEGRAM_ID,),
        ).fetchall()
        assert [(row["source"], row["user_id"]) for row in rows] == [("Telegram", "separate-account")]


def test_owner_group_members_and_first_time_nonowners_keep_separate_accounts(settings) -> None:
    from friday.server import create_app

    owner_group = "5001"
    newcomer = "6002"
    tuned = replace(
        settings,
        shared_archive=False,
        telegram_allowed_chat_ids=[int(newcomer)],
        telegram_owner_chat_ids=[int(owner_group)],
    )
    app = create_app(tuned)
    with TestClient(app) as client:
        group_member = _signed_request(
            client,
            tuned,
            "GET",
            "/api/me",
            {"telegram_user": {"id": 1001}},
            sender="1001",
            chat=owner_group,
        )
        assert group_member.status_code == 200
        group_member_id = f"telegram:{tuned.telegram_realm_id}:1001"
        assert app.state.storage.get_user(group_member_id) is not None
        assert app.state.storage.resolve_identity("telegram", "1001") is None

        first_private_turn = _signed_request(
            client,
            tuned,
            "GET",
            "/api/me",
            {"telegram_user": {"id": int(newcomer)}},
            sender=newcomer,
            chat=newcomer,
        )
        assert first_private_turn.status_code == 200
        newcomer_id = f"telegram:{tuned.telegram_realm_id}:{newcomer}"
        assert app.state.storage.get_user(newcomer_id) is not None
        assert app.state.storage.resolve_identity("telegram", newcomer) is None


def test_non_rebinding_identity_claim_is_atomic_and_idempotent(storage) -> None:
    storage.ensure_user("canonical-owner", preset_key="owner")
    storage.ensure_user("other-account", preset_key="user")
    first = storage.link_identity(
        "telegram",
        OWNER_TELEGRAM_ID,
        "canonical-owner",
        linked_by="canonical-owner",
        allow_rebind=False,
    )
    repeated = storage.link_identity(
        "telegram",
        OWNER_TELEGRAM_ID,
        "canonical-owner",
        linked_by="racing-request",
        allow_rebind=False,
    )
    assert repeated == first

    with pytest.raises(ValueError, match="different account"):
        storage.link_identity(
            "telegram",
            OWNER_TELEGRAM_ID,
            "other-account",
            allow_rebind=False,
        )
    assert storage.resolve_identity("telegram", OWNER_TELEGRAM_ID) == "canonical-owner"

    # Exercise the actual database race through two repository instances (and
    # therefore two independent Python write locks). SQLite must serialize the
    # no-rebind check with its insert: exactly one claimant wins.
    from friday.storage import FridayStorage

    contender = FridayStorage(storage.settings)
    contender.get_user("canonical-owner")  # initialize its independent connection
    barrier = threading.Barrier(2)
    racing_external_id = f"{OWNER_TELEGRAM_ID}-race"

    def claim(repository: Any, user_id: str) -> str:
        barrier.wait(timeout=5)
        try:
            repository.link_identity(
                "telegram",
                racing_external_id,
                user_id,
                allow_rebind=False,
            )
        except ValueError:
            return "refused"
        return "linked"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda pair: claim(*pair),
                    ((storage, "canonical-owner"), (contender, "other-account")),
                )
            )
        assert sorted(results) == ["linked", "refused"]
        assert storage.resolve_identity("telegram", racing_external_id) in {
            "canonical-owner",
            "other-account",
        }
    finally:
        contender.close(final=True)
