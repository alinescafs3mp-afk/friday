"""A cancelled chat request must not replay an effect that may already exist.

The execution kernel already records that a mutator started before invoking its
handler.  Request idempotency used to throw its lease away on every
``BaseException`` regardless, so a bridge retry could execute the same action a
second time.  These tests cross the real HTTP, kernel and SQLite boundaries:
one cancellation happens after a durable synthetic effect, the other before any
mutating handler is entered.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import time
import uuid

import httpx
import pytest

from friday.execution_kernel import (
    ToolSpec,
    mark_request_effect_possible,
    rollback_staged_request_effect,
    stage_request_effect_possible_in_transaction,
    track_request_effects,
)
from friday.permissions import ActorContext
from friday.security import sign_bridge_request


def _chat_result(message: str) -> dict:
    return {
        "message": message,
        "answer": message,
        "context": {"interaction_mode": "dialogue"},
    }


def _bridge_headers(settings, method: str, path: str, body: bytes) -> dict[str, str]:
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    return {
        "Content-Type": "application/json",
        "X-Friday-Timestamp": str(timestamp),
        "X-Friday-User": "1001",
        "X-Friday-Chat": "5001",
        "X-Friday-Nonce": nonce,
        "X-Friday-Signature": sign_bridge_request(
            settings.telegram_bridge_secret,
            timestamp=timestamp,
            method=method,
            path=path,
            external_user_id="1001",
            chat_id="5001",
            nonce=nonce,
            body=body,
        ),
    }


def test_transaction_effect_fence_requires_the_exact_optional_request_binding() -> None:
    calls: list[object] = []
    connection = object()

    def in_transaction(candidate: object) -> bool:
        calls.append(candidate)
        return True

    assert (
        stage_request_effect_possible_in_transaction(
            connection,
            expected_request_binding_sha256="a" * 64,
        )
        is False
    )
    with (
        pytest.raises(ValueError, match="request effect binding"),
        track_request_effects(
            lambda: True,
            request_binding_sha256="not-a-digest",
        ),
    ):
        pass

    with track_request_effects(
        lambda: True,
        before_effect_in_transaction=in_transaction,
        request_binding_sha256="a" * 64,
    ) as effects:
        assert (
            stage_request_effect_possible_in_transaction(
                connection,
                expected_request_binding_sha256="b" * 64,
            )
            is False
        )
        assert calls == []
        assert (
            stage_request_effect_possible_in_transaction(
                connection,
                expected_request_binding_sha256="a" * 64,
            )
            is True
        )
        assert calls == [connection]
        assert effects.staged is True
        rollback_staged_request_effect()


@pytest.mark.asyncio
async def test_process_death_cannot_make_an_effectful_lease_stealable(settings) -> None:
    """The terminal fence must work without running the HTTP exception handler.

    This models SIGKILL immediately after the handler commits: the kernel and
    storage run, but no route ``except`` or normal response completion runs.
    Even an ancient timestamp must not make the row reclaimable afterward.
    """

    from friday.server import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        owner_id = "effect-safe-hard-kill"
        app.state.storage.ensure_user(owner_id, preset_key="owner")
        actor = ActorContext(user_id=owner_id, preset_key="owner", source="test")
        request_key = "effect-safe:hard-kill"
        request_hash = "a" * 64
        claim = app.state.storage.idempotency_claim(
            owner_id,
            request_key,
            request_hash=request_hash,
            lease_seconds=1,
        )
        lease_token = str(claim["lease_token"])
        effect_count = 0

        async def commits(*, actor=None):  # noqa: ANN001
            nonlocal effect_count
            effect_count += 1
            app.state.storage.kv_set("test:effect-safe-hard-kill", str(effect_count))
            return {"created": True}

        app.state.kernel.register(
            ToolSpec(
                name="synthetic_hard_kill_mutator",
                description="Synthetic mutation followed by simulated process death.",
                parameters={"type": "object", "properties": {}},
                security_id="knowledge.create",
                risk="mutate",
                handler=commits,
            )
        )
        uncertain = {
            "message": "outcome uncertain",
            "answer": "outcome uncertain",
            "idempotency_effect_uncertain": True,
        }
        with track_request_effects(
            functools.partial(
                app.state.storage.idempotency_mark_effect_possible,
                owner_id,
                request_key,
                lease_token,
                uncertain,
            )
        ):
            result = await app.state.kernel.execute(
                "synthetic_hard_kill_mutator",
                {},
                actor=actor,
            )
        assert result.success is True
        assert effect_count == 1

        row = app.state.storage.execute(
            """SELECT state, lease_token, response_json
                 FROM request_idempotency WHERE user_id=? AND request_key=?""",
            (owner_id, request_key),
        ).fetchone()
        assert row is not None
        assert row["state"] == "pending"
        # The surviving original request can still replace the sentinel with its
        # real response; a replacement process cannot guess this token.
        assert row["lease_token"] == lease_token
        assert json.loads(str(row["response_json"]))["idempotency_effect_uncertain"] is True

        app.state.storage.execute(
            """UPDATE request_idempotency SET updated_at='2000-01-01T00:00:00+00:00'
                 WHERE user_id=? AND request_key=?""",
            (owner_id, request_key),
        )
        recovered = app.state.storage.idempotency_claim(
            owner_id,
            request_key,
            request_hash=request_hash,
            lease_seconds=1,
        )
        assert recovered["status"] == "replay"
        assert recovered["response"]["idempotency_effect_uncertain"] is True
        assert effect_count == 1
        completed = app.state.storage.execute(
            """SELECT state, lease_token, response_json
                 FROM request_idempotency WHERE user_id=? AND request_key=?""",
            (owner_id, request_key),
        ).fetchone()
        assert completed["state"] == "complete"
        assert completed["lease_token"] == ""
        assert json.loads(str(completed["response_json"]))["idempotency_effect_uncertain"] is True


@pytest.mark.asyncio
async def test_prune_keeps_old_uncertain_effect_fences(settings) -> None:
    """Retention must never reopen a transport key whose effect may exist."""

    from friday.server import create_app

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        user_id = "effect-safe-prune"
        app.state.storage.ensure_user(user_id, preset_key="owner")
        app.state.storage.idempotency_store(user_id, "ordinary-old", {"message": "done"})
        app.state.storage.idempotency_store(
            user_id,
            "uncertain-old",
            _chat_result("unknown") | {"idempotency_effect_uncertain": True},
        )
        app.state.storage.execute(
            """UPDATE request_idempotency SET created_at='2000-01-01T00:00:00+00:00',
                                                    updated_at='2000-01-01T00:00:00+00:00'
                 WHERE user_id=?""",
            (user_id,),
        )

        assert app.state.storage.idempotency_prune(days=30) == 1
        assert app.state.storage.idempotency_get(user_id, "ordinary-old") is None
        preserved = app.state.storage.idempotency_get(user_id, "uncertain-old")
        assert preserved is not None
        assert preserved["idempotency_effect_uncertain"] is True


@pytest.mark.asyncio
async def test_cancel_after_mutating_tool_commit_becomes_terminal_replay(settings) -> None:
    """Deleting the effect witness makes the retry execute the handler twice."""

    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {
        # Explicit web intent keeps the ordinary ingestion pre-pass
        # side-effect-free; the only effect in this test is the tool below.
        "message": "Найди в интернете свежие новости",
        "source_ref": "effect-safe:cancel-after-commit",
    }

    async with app.router.lifespan_context(app):
        committed = asyncio.Event()
        never_release = asyncio.Event()
        effect_count = 0

        async def committed_then_waits(*, actor=None):  # noqa: ANN001
            nonlocal effect_count
            effect_count += 1
            app.state.storage.kv_set("test:effect-safe-idempotency", str(effect_count))
            committed.set()
            await never_release.wait()
            return {"created": True}

        app.state.kernel.register(
            ToolSpec(
                name="synthetic_effect_safe_mutator",
                description="Synthetic durable mutation for request cancellation testing.",
                parameters={"type": "object", "properties": {}},
                security_id="knowledge.create",
                risk="mutate",
                handler=committed_then_waits,
            )
        )

        async def mutating_chat(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            result = await app.state.kernel.execute(
                "synthetic_effect_safe_mutator",
                {},
                actor=kwargs["actor"],
            )
            return _chat_result("done" if result.success else result.error)

        app.state.agent.chat = mutating_chat
        transport = httpx.ASGITransport(app=app, client=("198.51.100.31", 8310))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/chat", json=payload, headers=headers))
            await asyncio.wait_for(committed.wait(), timeout=2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            row = app.state.storage.execute(
                """SELECT state, lease_token, response_json
                     FROM request_idempotency WHERE request_key=?""",
                (payload["source_ref"],),
            ).fetchone()
            assert row is not None, "uncertain effect lost its durable request fence"
            assert row["state"] == "complete"
            assert row["lease_token"] == ""
            stored = json.loads(str(row["response_json"]))
            assert stored["idempotency_effect_uncertain"] is True

            replay = await client.post("/api/chat", json=payload, headers=headers)
            assert replay.status_code == 200, replay.text
            body = replay.json()
            assert body["idempotent_replay"] is True
            assert body["idempotency_effect_uncertain"] is True
            assert "не повторяю" in body["message"].casefold()

        assert effect_count == 1, "the uncertain durable effect was executed again"
        assert app.state.storage.kv_get("test:effect-safe-idempotency") == "1"


@pytest.mark.asyncio
async def test_fenced_healthy_request_stays_in_progress_then_replays_real_answer(settings) -> None:
    """The crash sentinel must not escape while its original request is alive."""

    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {
        "message": "Найди в интернете свежие новости",
        "source_ref": "effect-safe:healthy-concurrency",
    }

    async with app.router.lifespan_context(app):
        committed = asyncio.Event()
        release = asyncio.Event()
        effect_count = 0

        async def commits_then_waits(*, actor=None):  # noqa: ANN001
            nonlocal effect_count
            effect_count += 1
            committed.set()
            await release.wait()
            return {"created": True}

        app.state.kernel.register(
            ToolSpec(
                name="synthetic_healthy_fenced_mutator",
                description="Synthetic mutation for live duplicate testing.",
                parameters={"type": "object", "properties": {}},
                security_id="knowledge.create",
                risk="mutate",
                handler=commits_then_waits,
            )
        )

        async def mutating_chat(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            result = await app.state.kernel.execute(
                "synthetic_healthy_fenced_mutator",
                {},
                actor=kwargs["actor"],
            )
            return _chat_result("real completed answer" if result.success else result.error)

        app.state.agent.chat = mutating_chat
        transport = httpx.ASGITransport(app=app, client=("198.51.100.35", 8314))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first_task = asyncio.create_task(client.post("/api/chat", json=payload, headers=headers))
            await asyncio.wait_for(committed.wait(), timeout=2)

            duplicate = await client.post("/api/chat", json=payload, headers=headers)
            assert duplicate.status_code == 409, duplicate.text
            assert duplicate.headers["Retry-After"] == "2"

            release.set()
            first = await first_task
            assert first.status_code == 200, first.text
            assert first.json()["message"] == "real completed answer"

            replay = await client.post("/api/chat", json=payload, headers=headers)
            assert replay.status_code == 200, replay.text
            assert replay.json()["message"] == "real completed answer"
            assert replay.json()["idempotent_replay"] is True
            assert replay.json().get("idempotency_effect_uncertain") is not True

        assert effect_count == 1


@pytest.mark.asyncio
async def test_cancel_before_any_mutator_releases_the_request_for_safe_retry(settings) -> None:
    """A router/model stall before persistence remains safe to retry."""

    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {
        # Web intent skips text ingestion, so the fake router below is reached
        # without any persistent request mutation.
        "message": "Найди в интернете свежие новости",
        "source_ref": "effect-safe:cancel-before-effect",
    }

    async with app.router.lifespan_context(app):
        entered = asyncio.Event()
        never_release = asyncio.Event()
        chat_calls = 0

        async def stalls_before_any_write(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            nonlocal chat_calls
            chat_calls += 1
            if chat_calls == 1:
                entered.set()
                await never_release.wait()
            return _chat_result("safe retry completed")

        app.state.agent.chat = stalls_before_any_write
        transport = httpx.ASGITransport(app=app, client=("198.51.100.32", 8311))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/chat", json=payload, headers=headers))
            await asyncio.wait_for(entered.wait(), timeout=2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            pending = app.state.storage.execute(
                "SELECT 1 FROM request_idempotency WHERE request_key=?",
                (payload["source_ref"],),
            ).fetchone()
            assert pending is None, "a provably pre-effect cancellation was made terminal"

            retry = await client.post("/api/chat", json=payload, headers=headers)
            assert retry.status_code == 200, retry.text
            assert retry.json()["message"] == "safe retry completed"
            assert retry.json().get("idempotency_effect_uncertain") is not True

        assert chat_calls == 2


@pytest.mark.asyncio
async def test_malformed_keyed_reply_recovery_remains_the_same_pre_effect_400(settings) -> None:
    """Validation failure must not be replaced by the crash-uncertainty sentinel."""

    from friday.server import create_app

    app = create_app(settings)
    payload = {
        "message": "покажи метаданные этого файла",
        "source_ref": "effect-safe:malformed-reply-recovery",
        "telegram_message_id": 7302,
        "reply_document_source_ref": "telegram-file:UNBOUND-HISTORICAL-FILE",
        "reply_document_message_id": 7301,
        "reply_document_file_unique_id": "STABLE-HISTORICAL-FILE",
        "reply_document_recovery": {},
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("198.51.100.38", 8317))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            me = await client.get(
                "/api/me",
                headers=_bridge_headers(settings, "GET", "/api/me", b""),
            )
            assert me.status_code == 200, me.text
            person_id = str(me.json()["actor"]["user_id"])
            app.state.storage.update_user(person_id, preset_key="user")

            first = await client.post(
                "/api/chat",
                content=encoded,
                headers=_bridge_headers(settings, "POST", "/api/chat", encoded),
            )
            second = await client.post(
                "/api/chat",
                content=encoded,
                headers=_bridge_headers(settings, "POST", "/api/chat", encoded),
            )

        assert first.status_code == 400, first.text
        assert second.status_code == 400, second.text
        assert first.json() == second.json() == {"detail": "Invalid reply media recovery"}
        row = app.state.storage.execute(
            "SELECT state, response_json FROM request_idempotency WHERE request_key=?",
            (payload["source_ref"],),
        ).fetchone()
        assert row is None, "a pre-effect 400 left a terminal uncertainty response"


@pytest.mark.asyncio
async def test_cancel_after_generated_file_persistence_does_not_persist_it_twice(
    settings,
    monkeypatch,
) -> None:
    """Server-owned postprocessing is covered even when a fake agent writes nothing."""

    import friday.server as server_module

    app = server_module.create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {
        "message": "Найди в интернете свежие новости",
        "source_ref": "effect-safe:cancel-after-generated-file",
    }
    file_bytes = b"effect-safe generated artifact"

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("198.51.100.39", 8318))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            me = await client.get("/api/me", headers=headers)
            assert me.status_code == 200, me.text
            person_id = str(me.json()["actor"]["user_id"])
            conversation = app.state.storage.create_conversation(person_id, title="generated fence")
            assistant = app.state.storage.store_message(
                conversation["id"],
                person_id,
                "assistant",
                "Файл готов.",
            )

            async def fake_agent_without_request_writes(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
                return {
                    "conversation_id": conversation["id"],
                    "message_id": assistant["id"],
                    "message": "Файл готов.",
                    "files": [
                        {
                            "kind": "document",
                            "filename": "result.txt",
                            "mime_type": "text/plain",
                            "content_base64": base64.b64encode(file_bytes).decode("ascii"),
                        }
                    ],
                }

            app.state.agent.chat = fake_agent_without_request_writes
            canonical_persist = server_module.persist_generated_response_files
            persist_calls = 0

            def persist_then_cancel(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
                nonlocal persist_calls
                persist_calls += 1
                canonical_persist(*args, **kwargs)
                raise asyncio.CancelledError

            monkeypatch.setattr(server_module, "persist_generated_response_files", persist_then_cancel)
            # Starlette's BaseHTTPMiddleware may translate a child-task
            # CancelledError into ``No response returned`` at the ASGI edge.
            with pytest.raises((asyncio.CancelledError, RuntimeError)):
                await client.post("/api/chat", json=payload, headers=headers)

            row = app.state.storage.execute(
                """SELECT state, response_json FROM request_idempotency
                     WHERE request_key=?""",
                (payload["source_ref"],),
            ).fetchone()
            assert row is not None
            assert row["state"] == "complete"
            assert json.loads(str(row["response_json"]))["idempotency_effect_uncertain"] is True

            retry = await client.post("/api/chat", json=payload, headers=headers)
            assert retry.status_code == 200, retry.text
            assert retry.json()["idempotency_effect_uncertain"] is True
            assert retry.json()["idempotent_replay"] is True

        generated_count = app.state.storage.execute(
            "SELECT COUNT(*) FROM raw_objects WHERE content_type='generated_file'"
        ).fetchone()[0]
        assert generated_count == 1
        assert persist_calls == 1


@pytest.mark.asyncio
async def test_expired_turn_does_not_start_generated_file_persistence(settings, monkeypatch) -> None:
    """A late model result cannot start a fresh durable artifact batch."""

    import friday.server as server_module

    monkeypatch.setattr(type(settings), "agent_turn_budget_sec", property(lambda self: 0.01))
    app = server_module.create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {
        "message": "Найди в интернете свежие новости",
        "source_ref": "effect-safe:expired-generated-file",
    }
    async with app.router.lifespan_context(app):
        me_transport = httpx.ASGITransport(app=app, client=("198.51.100.42", 8321))
        async with httpx.AsyncClient(transport=me_transport, base_url="http://test") as client:
            me = await client.get("/api/me", headers=headers)
        person_id = str(me.json()["actor"]["user_id"])
        conversation = app.state.storage.create_conversation(person_id, title="late file")
        assistant = app.state.storage.store_message(
            conversation["id"],
            person_id,
            "assistant",
            "Поздний файл.",
        )

        async def late_file(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            await asyncio.sleep(0.03)
            return {
                "conversation_id": conversation["id"],
                "message_id": assistant["id"],
                "message": "Поздний файл.",
                "files": [
                    {
                        "kind": "document",
                        "filename": "late.txt",
                        "mime_type": "text/plain",
                        "content_base64": base64.b64encode(b"late").decode("ascii"),
                    }
                ],
            }

        app.state.agent.chat = late_file
        persist_calls = 0

        def should_not_persist(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            nonlocal persist_calls
            persist_calls += 1
            return {}

        monkeypatch.setattr(server_module, "persist_generated_response_files", should_not_persist)
        transport = httpx.ASGITransport(
            app=app,
            client=("198.51.100.42", 8321),
            raise_app_exceptions=False,
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/chat", json=payload, headers=headers)

        assert response.status_code == 500
        assert persist_calls == 0
        assert (
            app.state.storage.execute(
                "SELECT 1 FROM request_idempotency WHERE request_key=?",
                (payload["source_ref"],),
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_chat_without_a_source_ref_still_executes_mutating_tools(settings) -> None:
    """Optional request idempotency must not become a tool-availability gate."""

    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {"message": "Найди в интернете свежие новости"}

    async with app.router.lifespan_context(app):
        effect_count = 0

        async def commits(*, actor=None):  # noqa: ANN001
            nonlocal effect_count
            effect_count += 1
            return {"created": True}

        app.state.kernel.register(
            ToolSpec(
                name="synthetic_unkeyed_mutator",
                description="Synthetic mutation on a request without an idempotency key.",
                parameters={"type": "object", "properties": {}},
                security_id="knowledge.create",
                risk="mutate",
                handler=commits,
            )
        )

        async def mutating_chat(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            result = await app.state.kernel.execute(
                "synthetic_unkeyed_mutator",
                {},
                actor=kwargs["actor"],
            )
            return _chat_result("tool completed" if result.success else result.error)

        app.state.agent.chat = mutating_chat
        transport = httpx.ASGITransport(app=app, client=("198.51.100.33", 8312))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/api/chat", json=payload, headers=headers)

        assert response.status_code == 200, response.text
        assert response.json()["message"] == "tool completed"
        assert effect_count == 1


@pytest.mark.asyncio
async def test_cancel_after_user_message_storage_does_not_append_the_turn_twice(settings) -> None:
    """Conversation writes use the same durable fence as model-selected tools."""

    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {
        "message": "Найди в интернете свежие новости",
        "source_ref": "effect-safe:cancel-after-user-row",
    }

    async with app.router.lifespan_context(app):
        stored = asyncio.Event()
        never_release = asyncio.Event()
        original_store_message = app.state.storage.store_message

        def store_then_signal(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            result = original_store_message(*args, **kwargs)
            if len(args) >= 3 and args[2] == "user":
                stored.set()
            return result

        app.state.storage.store_message = store_then_signal

        original_chat = app.state.agent.chat

        async def chat_hangs_after_runtime_write(*args, **kwargs):  # noqa: ANN002, ANN003
            result = await original_chat(*args, **kwargs)
            await never_release.wait()
            return result

        app.state.agent.chat = chat_hangs_after_runtime_write
        transport = httpx.ASGITransport(app=app, client=("198.51.100.34", 8313))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/chat", json=payload, headers=headers))
            await asyncio.wait_for(stored.wait(), timeout=2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            user_rows_before = app.state.storage.execute(
                "SELECT COUNT(*) FROM messages WHERE role='user' AND content=?",
                (payload["message"],),
            ).fetchone()[0]
            assert user_rows_before == 1

            retry = await client.post("/api/chat", json=payload, headers=headers)
            assert retry.status_code == 200, retry.text
            assert retry.json()["idempotency_effect_uncertain"] is True
            assert retry.json()["idempotent_replay"] is True

            user_rows_after = app.state.storage.execute(
                "SELECT COUNT(*) FROM messages WHERE role='user' AND content=?",
                (payload["message"],),
            ).fetchone()[0]
            assert user_rows_after == 1, "retry appended the same durable user turn twice"


@pytest.mark.asyncio
async def test_cancelled_regenerate_replays_uncertainty_instead_of_the_tool(settings) -> None:
    """Regenerate uses the same effect fence and public response projection."""

    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("198.51.100.36", 8315))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            me = await client.get("/api/me", headers=headers)
            assert me.status_code == 200, me.text
            user_id = str(me.json()["actor"]["user_id"])
            conversation = app.state.storage.create_conversation(user_id, title="regenerate fence")
            source_user = app.state.storage.store_message(
                conversation["id"],
                user_id,
                "user",
                "Повтори это действие",
            )

            committed = asyncio.Event()
            never_release = asyncio.Event()
            effect_count = 0

            async def commits_then_waits(*, actor=None):  # noqa: ANN001
                nonlocal effect_count
                effect_count += 1
                committed.set()
                await never_release.wait()
                return {"created": True}

            app.state.kernel.register(
                ToolSpec(
                    name="synthetic_regenerate_mutator",
                    description="Synthetic regenerate mutation.",
                    parameters={"type": "object", "properties": {}},
                    security_id="knowledge.create",
                    risk="mutate",
                    handler=commits_then_waits,
                )
            )

            async def mutating_chat(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
                assert mark_request_effect_possible()
                app.state.storage.store_message(
                    conversation["id"],
                    user_id,
                    "user",
                    "Повтори это действие",
                    metadata={
                        "regenerate_root_user_message_id": kwargs["replay_source_message_id"],
                    },
                )
                assert kwargs["replay_source_message_id"] == source_user["id"]
                result = await app.state.kernel.execute(
                    "synthetic_regenerate_mutator",
                    {},
                    actor=kwargs["actor"],
                )
                return _chat_result("done" if result.success else result.error)

            app.state.agent.chat = mutating_chat
            payload = {"conversation_id": conversation["id"]}
            first = asyncio.create_task(client.post("/api/me/regenerate", json=payload, headers=headers))
            await asyncio.wait_for(committed.wait(), timeout=2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            replay = await client.post("/api/me/regenerate", json=payload, headers=headers)
            assert replay.status_code == 200, replay.text
            assert replay.json()["idempotent_replay"] is True
            assert replay.json()["idempotency_effect_uncertain"] is True
            assert "не повторяю" in replay.json()["message"].casefold()

        assert effect_count == 1


@pytest.mark.asyncio
async def test_completed_regenerate_allows_a_deliberate_second_alternative(settings) -> None:
    """Stable retry keys must not turn every later regenerate into a replay."""

    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("198.51.100.40", 8319))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            me = await client.get("/api/me", headers=headers)
            user_id = str(me.json()["actor"]["user_id"])
            conversation = app.state.storage.create_conversation(user_id, title="two alternatives")
            app.state.storage.store_message(
                conversation["id"],
                user_id,
                "user",
                "Сделай ещё один вариант",
            )
            calls = 0

            async def completed_replay(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
                nonlocal calls
                calls += 1
                assert mark_request_effect_possible()
                app.state.storage.store_message(
                    conversation["id"],
                    user_id,
                    "user",
                    "Сделай ещё один вариант",
                    metadata={
                        "regenerate_parent_user_message_id": kwargs["replay_source_message_id"],
                        "regenerate_root_user_message_id": kwargs["replay_source_message_id"],
                    },
                )
                return {
                    **_chat_result(f"alternative {calls}"),
                    "conversation_id": conversation["id"],
                }

            app.state.agent.chat = completed_replay
            payload = {"conversation_id": conversation["id"]}
            first = await client.post("/api/me/regenerate", json=payload, headers=headers)
            second = await client.post("/api/me/regenerate", json=payload, headers=headers)

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["message"] == "alternative 1"
        assert second.json()["message"] == "alternative 2"
        assert second.json().get("idempotent_replay") is not True
        assert calls == 2


@pytest.mark.asyncio
async def test_regenerate_operation_id_replays_same_click_but_not_a_new_click(settings) -> None:
    """Transport retries and deliberate alternatives have distinct identities."""

    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("198.51.100.43", 8322))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            me = await client.get("/api/me", headers=headers)
            user_id = str(me.json()["actor"]["user_id"])
            conversation = app.state.storage.create_conversation(user_id, title="operation ids")
            app.state.storage.store_message(
                conversation["id"],
                user_id,
                "user",
                "Сделай новый вариант",
            )
            calls = 0

            async def alternative(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
                nonlocal calls
                calls += 1
                assert mark_request_effect_possible()
                app.state.storage.store_message(
                    conversation["id"],
                    user_id,
                    "user",
                    "Сделай новый вариант",
                    metadata={
                        "regenerate_parent_user_message_id": kwargs["replay_source_message_id"],
                        "regenerate_root_user_message_id": kwargs["replay_source_message_id"],
                    },
                )
                return {
                    **_chat_result(f"operation alternative {calls}"),
                    "conversation_id": conversation["id"],
                }

            app.state.agent.chat = alternative
            first_payload = {
                "conversation_id": conversation["id"],
                "operation_id": "telegram-update:91001",
            }
            first = await client.post("/api/me/regenerate", json=first_payload, headers=headers)
            same_click_retry = await client.post(
                "/api/me/regenerate",
                json=first_payload,
                headers=headers,
            )
            second_click = await client.post(
                "/api/me/regenerate",
                json={
                    "conversation_id": conversation["id"],
                    "operation_id": "telegram-update:91002",
                },
                headers=headers,
            )
            malformed = await client.post(
                "/api/me/regenerate",
                json={
                    "conversation_id": conversation["id"],
                    "operation_id": " telegram update with spaces ",
                },
                headers=headers,
            )

        assert first.status_code == 200, first.text
        assert same_click_retry.status_code == 200, same_click_retry.text
        assert same_click_retry.json()["idempotent_replay"] is True
        assert same_click_retry.json()["message"] == "operation alternative 1"
        assert second_click.status_code == 200, second_click.text
        assert second_click.json().get("idempotent_replay") is not True
        assert second_click.json()["message"] == "operation alternative 2"
        assert malformed.status_code == 400
        assert malformed.json() == {"detail": "Invalid regenerate operation_id"}
        assert calls == 2


@pytest.mark.asyncio
async def test_healthy_long_regenerate_renews_its_lease(settings, monkeypatch) -> None:
    """A live turn cannot become stale while it is still producing its answer."""

    import friday.server as server_module

    monkeypatch.setattr(server_module, "_REGENERATE_IDEMPOTENCY_LEASE_SECONDS", 1)
    monkeypatch.setattr(server_module, "_REGENERATE_IDEMPOTENCY_HEARTBEAT_SECONDS", 0.1)
    app = server_module.create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    async with app.router.lifespan_context(app):
        renewals = 0
        canonical_renew = app.state.storage.idempotency_renew

        def observed_renew(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            nonlocal renewals
            renewed = canonical_renew(*args, **kwargs)
            renewals += int(renewed)
            return renewed

        monkeypatch.setattr(app.state.storage, "idempotency_renew", observed_renew)
        transport = httpx.ASGITransport(app=app, client=("198.51.100.41", 8320))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            me = await client.get("/api/me", headers=headers)
            user_id = str(me.json()["actor"]["user_id"])
            conversation = app.state.storage.create_conversation(user_id, title="renew regenerate")
            app.state.storage.store_message(
                conversation["id"],
                user_id,
                "user",
                "Долго готовь новый вариант",
            )
            entered = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def slow_but_healthy(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
                nonlocal calls
                calls += 1
                assert mark_request_effect_possible()
                entered.set()
                await release.wait()
                return {
                    **_chat_result("real long answer"),
                    "conversation_id": conversation["id"],
                }

            app.state.agent.chat = slow_but_healthy
            payload = {"conversation_id": conversation["id"]}
            first_task = asyncio.create_task(client.post("/api/me/regenerate", json=payload, headers=headers))
            await asyncio.wait_for(entered.wait(), timeout=2)
            initial_updated = str(
                app.state.storage.execute(
                    "SELECT updated_at FROM request_idempotency WHERE request_key LIKE 'regenerate:%'"
                ).fetchone()["updated_at"]
            )
            await asyncio.sleep(1.05)
            async with asyncio.timeout(2):
                while True:
                    renewed_updated = str(
                        app.state.storage.execute(
                            "SELECT updated_at FROM request_idempotency WHERE request_key LIKE 'regenerate:%'"
                        ).fetchone()["updated_at"]
                    )
                    if renewed_updated != initial_updated:
                        break
                    await asyncio.sleep(0.02)
            assert renewals >= 2

            duplicate = await client.post("/api/me/regenerate", json=payload, headers=headers)
            assert duplicate.status_code == 409, duplicate.text
            assert duplicate.headers["Retry-After"] == "2"

            release.set()
            first = await first_task
            assert first.status_code == 200, first.text
            assert first.json()["message"] == "real long answer"

            replay = await client.post("/api/me/regenerate", json=payload, headers=headers)
            assert replay.status_code == 200, replay.text
            assert replay.json()["message"] == "real long answer"
            assert replay.json()["idempotent_replay"] is True

        assert calls == 1


@pytest.mark.asyncio
async def test_cancel_during_ingestion_keeps_the_effect_fence(settings) -> None:
    """A write before AgentRuntime must not be followed by lease deletion."""

    from friday.server import create_app

    app = create_app(settings)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    payload = {
        "message": "Запомни этот синтетический факт",
        "source_ref": "effect-safe:cancel-during-ingest",
    }

    async with app.router.lifespan_context(app):
        committed = asyncio.Event()
        never_release = asyncio.Event()
        ingest_calls = 0

        async def ingest_commits_then_waits(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
            nonlocal ingest_calls
            ingest_calls += 1
            app.state.storage.kv_set("test:effect-safe-ingest", str(ingest_calls))
            committed.set()
            await never_release.wait()
            return {"promoted": False, "action": "transient"}

        app.state.ingestion.ingest_text = ingest_commits_then_waits
        transport = httpx.ASGITransport(app=app, client=("198.51.100.37", 8316))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/chat", json=payload, headers=headers))
            await asyncio.wait_for(committed.wait(), timeout=2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            row = app.state.storage.execute(
                """SELECT state, lease_token, response_json FROM request_idempotency
                     WHERE request_key=?""",
                (payload["source_ref"],),
            ).fetchone()
            assert row is not None
            assert row["state"] == "complete"
            assert row["lease_token"] == ""
            assert json.loads(str(row["response_json"]))["idempotency_effect_uncertain"] is True

            replay = await client.post("/api/chat", json=payload, headers=headers)
            assert replay.status_code == 200, replay.text
            assert replay.json()["idempotency_effect_uncertain"] is True
            assert replay.json()["idempotent_replay"] is True

        assert ingest_calls == 1
        assert app.state.storage.kv_get("test:effect-safe-ingest") == "1"
