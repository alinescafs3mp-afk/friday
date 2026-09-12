"""Disposable bridge -> signed HTTP -> real mixed runtime transport checks.

External Telegram, web and model are fixtures. This is not LIVE_OWNER evidence.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace

import httpx
import pytest
from test_mixed_journey_runtime import _ANSWER, _MESSAGE, _TABLE_ANSWER, _Model
from test_transient_web_comparison import _RecordingWeb, _report, _source
from test_v12_file_evidence_reader import _register

from friday.organs.mixed_journey import mixed_status_admitted
from friday.server import create_app
from friday.telegram_bridge import TelegramBridge, TelegramConfig
from friday.telegram_bridge._base import PermanentUpdateError
from friday.telegram_bridge._mixed_delivery import require_mixed_delivery_authority

_CHAT = 5001
_UPDATE = 993001


class _TelegramHTTP:
    def __init__(self, *, failure: str = "") -> None:
        self.failure = failure
        self.calls: list[tuple[str, dict]] = []
        self.accepted: list[dict] = []
        self.status_created = asyncio.Event()
        self.status_message_id: int | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content) if request.content else {}
        self.calls.append((method, payload))
        if request.method == "GET":
            assert method == "current.txt"
            return httpx.Response(200, content=b"CURRENT TERMS")
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "current.txt"}})
        if method == "sendMessage":
            if ("Текущий файл" in payload["text"]) and self.failure == "connect":
                raise httpx.ConnectError("synthetic pre-accept failure", request=request)
            self.accepted.append(payload)
            if "Текущий файл" not in payload["text"]:
                self.status_message_id = 7100 + len(self.calls)
                self.status_created.set()
            if ("Текущий файл" in payload["text"]) and self.failure == "accepted_timeout":
                raise httpx.ReadTimeout("synthetic post-accept failure", request=request)
        assert method in {"sendMessage", "editMessageText", "sendChatAction"}
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7100 + len(self.calls)}})


def _bridge(settings, path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:fixture",
            bridge_secret=settings.telegram_bridge_secret,
            allowed_chat_ids=[_CHAT],
            inbox_db_path=str(path),
        )
    )


def _update() -> dict:
    return {
        "update_id": _UPDATE,
        "message": {
            "message_id": 991,
            "chat": {"id": _CHAT, "type": "private"},
            "from": {"id": _CHAT, "first_name": "Fixture"},
            "caption": _MESSAGE,
            "document": {
                "file_id": "current-fixture",
                "file_unique_id": "current-fixture-unique",
                "file_name": "current.txt",
                "mime_type": "text/plain",
                "file_size": len(b"CURRENT TERMS"),
            },
        },
    }


async def _run(bridge, telegram, backend) -> None:
    row = next(row for row in bridge._inbox.pending(now=time.time() + 3600) if row["update_id"] == _UPDATE)
    await bridge._run_update(telegram, backend, row)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "",
        "accepted_timeout",
        "connect",
        "connect_revoked",
        "connect_revoked_missing_context",
        "connect_revoked_changed_mode",
        "connect_missing_context",
        "revoke_before_send",
        "slow_status",
        "web_timeout",
        "retry_unidentified",
        "table",
        "table_three_spaces",
        "table_one_tab",
        "connect_table",
        "connect_table_changed_format",
        "table_web_timeout",
    ],
)
async def test_mixed_document_signed_backend_and_delivery_restart(
    settings, tmp_path, monkeypatch, failure
) -> None:
    scoped = replace(settings, telegram_owner_chat_ids=[_CHAT])
    app = create_app(scoped)
    path = tmp_path / "mixed-bridge.sqlite3"
    bridge = _bridge(scoped, path)
    reconnect = failure.startswith("connect") or failure == "accepted_timeout"
    revoked_on_retry = failure.startswith("connect_revoked")
    table_requested = "table" in failure
    changed_format = failure == "connect_table_changed_format"
    web_unavailable = failure.endswith("web_timeout")
    edge = _TelegramHTTP(failure="connect" if failure.startswith("connect") else failure)
    model = _Model(answer=_TABLE_ANSWER if table_requested else _ANSWER)
    if failure in {"table_three_spaces", "table_one_tab"}:
        indent = "   " if failure == "table_three_spaces" else "\t"
        model.answer = "\n".join(indent + line for line in _TABLE_ANSWER.splitlines())
    web = _RecordingWeb(
        {
            **_report(_source(1, text="PUBLIC TERMS")),
            "provider_primary_id": "brave",
            "selected_provider_id": "brave",
            "provider_used_fallback": False,
        }
    )
    backend_paths: list[str] = []
    if web_unavailable:
        web.raises = TimeoutError("fixture public provider timeout")
        model.answer = (
            _TABLE_ANSWER.replace("PUBLIC TERMS [W1]", "Недоступен")
            if table_requested
            else "Текущий файл [F1] отличается от архива [A1]."
        )
    from friday.telegram_bridge import _commands

    original_observe = _commands._observe_chat_result
    projections = []
    observed_responses = []

    def record_projection(state, response):
        original_observe(state, response)
        observed_responses.append(response)
        if state.mixed_projection is not None:
            projections.append(state.mixed_projection)

    monkeypatch.setattr(_commands, "_observe_chat_result", record_projection)
    if failure == "slow_status":
        original_complete = model.complete
        progress_delays = 0

        async def first_progress_now(_delay: float) -> None:
            nonlocal progress_delays
            progress_delays += 1
            if progress_delays == 1:
                await asyncio.sleep(0)
            else:
                await asyncio.Event().wait()

        async def held_model(*args, **kwargs):
            await asyncio.wait_for(edge.status_created.wait(), 5)
            return await original_complete(*args, **kwargs)

        monkeypatch.setattr(_commands, "_progress_sleep", first_progress_now)
        model.complete = held_model

    async def observe_backend(request: httpx.Request) -> None:
        backend_paths.append(request.url.path)
        if failure == "revoke_before_send" and request.url.path.startswith("/api/me/mixed-deliveries/"):
            with app.state.storage.transaction() as conn:
                conn.execute(
                    "UPDATE raw_objects SET deleted_at='2026-09-07T00:00:00Z' WHERE id=?", (archive.id,)
                )

    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), event_hooks={"request": [observe_backend]}
            ) as backend,
            httpx.AsyncClient(transport=httpx.MockTransport(edge)) as telegram,
        ):
            identity = await bridge._backend_json(backend, "GET", "/api/me", None, str(_CHAT), str(_CHAT))
            user_id = identity["user"]["id"]
            archive, _ = _register(
                app.state.storage,
                scoped,
                user_id=user_id,
                uploaded_by=user_id,
                filename="archive.txt",
                text="ARCHIVE TERMS",
            )
            app.state.agent._selected_archive_model = model
            app.state.agent.kernel.web_surfer = web
            app.state.auth_service.grant_permission(user_id, "web.compare.transient")
            update = _update()
            if table_requested:
                update["message"]["caption"] += " Ответ оформи без лишнего текста в виде таблицы."
            bridge._inbox.store(update)
            await _run(bridge, telegram, backend)
            final = [item for item in edge.accepted if ("Текущий файл" in item["text"])]
            assert len(final) == (
                0 if failure.startswith("connect") or failure == "revoke_before_send" else 1
            ), edge.calls
            if final:
                assert final[0]["reply_parameters"]["message_id"] == 991
            if final and not web_unavailable:
                assert projections and mixed_status_admitted(projections[-1])
                projection = projections[-1]
                assert set(projection.view.organs.present_organs) == {
                    "file",
                    "archive",
                    "conversation",
                    "web",
                }
                assert projection.projection_id == f"chat:{_UPDATE}"
                assert projection.authenticated_turn_id != f"chat:{_UPDATE}"
                assert "archive.txt" not in json.dumps(projection.to_mapping(), ensure_ascii=False)
            if web_unavailable:
                assert observed_responses[-1]["context"]["comparison_status"] == "partial"
                assert observed_responses[-1]["web_sources"] == []
            if failure in {"", "table"}:
                # The real default schedule must acknowledge even a fast turn;
                # accelerating the 12-second notifier would hide this defect.
                status_creates = [
                    payload for payload in edge.accepted if "Текущий файл" not in payload["text"]
                ]
                assert len(status_creates) == 1, edge.calls
                methods = [method for method, _ in edge.calls]
                assert methods.index("sendMessage") < methods.index("getFile")
                status_edits = [payload for method, payload in edge.calls if method == "editMessageText"]
                assert status_edits and {payload["message_id"] for payload in status_edits} == {
                    edge.status_message_id
                }
                assert "Смешанный маршрут" in status_edits[-1]["text"]
            assert len(model.calls) == 2 and len(web.calls) == 1
            assert backend_paths.count("/api/chat") == 1
            assert [method for method, _ in edge.calls].count("getFile") == 1
            pending = bridge._inbox.pending(now=time.time() + 3600)
            if reconnect:
                assert len(pending) == 1
                if failure == "accepted_timeout":
                    assert pending[0]["delivery_uncertainty"] == 1
                else:
                    assert pending[0]["chunks_sent"] == 0
                if failure.endswith(("missing_context", "changed_mode")):
                    cached = json.loads(pending[0]["backend_response_json"])
                    if failure.endswith("missing_context"):
                        cached.pop("context", None)
                    else:
                        cached["context"]["answer_mode"] = "ordinary_dialogue"
                    forged = json.loads(json.dumps(observed_responses[-1]["_mixed_journey_source_facts"]))
                    forged["files"][0]["file_id"] = "forged_cached_file"
                    cached["_mixed_journey_source_facts"] = forged
                    bridge._inbox.cache_backend_response(_UPDATE, cached)
                if changed_format:
                    cached = json.loads(pending[0]["backend_response_json"])
                    cached["message_format"] = "plain"
                    bridge._inbox.cache_backend_response(_UPDATE, cached)
                if revoked_on_retry:
                    with app.state.storage.transaction() as conn:
                        conn.execute(
                            "UPDATE raw_objects SET deleted_at='2026-09-07T00:00:00Z' WHERE id=?",
                            (archive.id,),
                        )
                bridge._inbox.close()
                bridge = _bridge(scoped, path)
                resumed_edge = _TelegramHTTP()
                async with httpx.AsyncClient(transport=httpx.MockTransport(resumed_edge)) as resumed:
                    await _run(bridge, resumed, backend)
                resumed_final = [item for item in resumed_edge.accepted if ("Текущий файл" in item["text"])]
                assert len(resumed_final) == (
                    1 if failure.startswith("connect") and not revoked_on_retry and not changed_format else 0
                )
                if resumed_final:
                    assert (
                        observed_responses[-1]["_mixed_journey_source_facts"]["files"][0]["file_id"]
                        != "forged_cached_file"
                    )
                if failure == "accepted_timeout":
                    assert any("доставка не подтверждена" in item["text"] for item in resumed_edge.accepted)
                assert len(model.calls) == 2 and len(web.calls) == 1
                assert backend_paths.count("/api/chat") == 1
                assert len([p for p in backend_paths if p.startswith("/api/me/mixed-deliveries/")]) == 2
                final.extend(resumed_final)
            if table_requested and not changed_format:
                assert len(final) == 1
                assert final[0]["parse_mode"] == "HTML"
                assert "<pre>" in final[0]["text"] and "| ---" not in final[0]["text"]
                assert all(
                    value in final[0]["text"] for value in ("CURRENT TERMS", "ARCHIVE TERMS", "[F1]", "[A1]")
                )
                if web_unavailable:
                    assert "Охват сравнения неполный" in final[0]["text"]
                    assert "[W1]" not in final[0]["text"]
                else:
                    assert "PUBLIC TERMS [W1]" in final[0]["text"]
                assert observed_responses[-1]["message_format"] == "markdown"
                assert not observed_responses[-1].get("knowledge_objects")
            if failure == "slow_status":
                status_creates = [
                    payload for payload in edge.accepted if "Текущий файл" not in payload["text"]
                ]
                status_edits = [payload for method, payload in edge.calls if method == "editMessageText"]
                assert len(status_creates) == 1
                assert status_edits and {payload["message_id"] for payload in status_edits} == {
                    edge.status_message_id
                }
                assert "Смешанный маршрут" in status_edits[-1]["text"]
            if failure in {"", "retry_unidentified"}:
                # A completed /retry cache uses a separate command-delivery
                # branch. Seed only its cache from the real owned answer;
                # no new generation is permitted after reconnect/revocation.
                retry_id = _UPDATE + 1
                retry_update = {
                    "update_id": retry_id,
                    "message": {
                        "message_id": 992,
                        "chat": {"id": _CHAT, "type": "private"},
                        "from": {"id": _CHAT},
                        "text": "/retry",
                    },
                }
                cached_retry = json.loads(json.dumps(observed_responses[-1]))
                cached_retry.pop("context", None)
                if failure == "retry_unidentified":
                    # The /retry command does not repeat the original source
                    # cues. Losing all cache identity must still fail closed.
                    cached_retry = {"message": cached_retry["message"], "verified": True}
                with app.state.storage.transaction() as conn:
                    conn.execute(
                        "UPDATE raw_objects SET deleted_at='2026-09-07T00:00:00Z' WHERE id=?",
                        (archive.id,),
                    )
                bridge._inbox.store(retry_update)
                bridge._inbox.cache_backend_response(retry_id, cached_retry)
                bridge._inbox.close()
                bridge = _bridge(scoped, path)
                row = next(
                    row
                    for row in bridge._inbox.pending(now=time.time() + 3600)
                    if row["update_id"] == retry_id
                )
                retry_edge = _TelegramHTTP()
                async with httpx.AsyncClient(transport=httpx.MockTransport(retry_edge)) as retry_telegram:
                    await bridge._run_update(retry_telegram, backend, row)
                assert not any("Текущий файл" in payload["text"] for payload in retry_edge.accepted)
                assert backend_paths.count("/api/me/regenerate") == 0
                assert len([p for p in backend_paths if p.startswith("/api/me/mixed-deliveries/")]) == (
                    1 if failure == "retry_unidentified" else 2
                )
                assert len(model.calls) == 2 and len(web.calls) == 1
            assert bridge._inbox.pending(now=time.time() + 3600) == []
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [None, "changed_body", "foreign_owner"])
async def test_ordinary_cached_retry_is_classified_by_owned_durable_body(settings, tmp_path, drift) -> None:
    scoped = replace(settings, telegram_owner_chat_ids=[_CHAT])
    app = create_app(scoped)
    bridge = _bridge(scoped, tmp_path / "ordinary-replay.sqlite3")
    paths = []
    telegram_edge = _TelegramHTTP()
    update = {
        "update_id": _UPDATE,
        "message": {
            "message_id": 991,
            "chat": {"id": _CHAT, "type": "private"},
            "from": {"id": _CHAT},
            "text": "/retry",
        },
    }

    async def record(request):
        paths.append(request.url.path)

    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), event_hooks={"request": [record]}
            ) as backend,
            httpx.AsyncClient(transport=httpx.MockTransport(telegram_edge)) as telegram,
        ):
            identity = await bridge._backend_json(backend, "GET", "/api/me", None, str(_CHAT), str(_CHAT))
            user_id = identity["user"]["id"]
            if drift == "foreign_owner":
                user_id = "unrelated-person"
                app.state.storage.ensure_user(user_id)
            conv = app.state.storage.create_conversation(user_id, "Ordinary")
            row = app.state.storage.store_message(conv["id"], user_id, "assistant", "Ordinary answer")
            response = {
                "message_id": row["id"],
                "conversation_id": conv["id"],
                "message": "Changed body" if drift == "changed_body" else row["content"],
                "context": {"answer_mode": "ordinary_dialogue"},
                "_mixed_journey_source_facts": {"forged_cache_fact": True},
            }
            if drift:
                with pytest.raises(PermanentUpdateError):
                    await bridge._process_update(telegram, backend, update, cached_response=response)
                assert telegram_edge.accepted == []
            else:
                await bridge._process_update(telegram, backend, update, cached_response=response)
                assert response["message"] == row["content"]
                assert len(telegram_edge.accepted) == 1
                assert row["content"] in telegram_edge.accepted[0]["text"]
            assert "_mixed_journey_source_facts" not in response
            assert len([path for path in paths if path.startswith("/api/me/mixed-deliveries/")]) == 1
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("request_message", ["Привет", _MESSAGE])
@pytest.mark.parametrize("has_mixed_facts", [False, True])
async def test_unidentified_cache_discards_internal_facts_before_any_legacy_exit(
    request_message, has_mixed_facts
) -> None:
    from contextlib import nullcontext

    response = {"message": "Legacy notice"}
    if has_mixed_facts:
        response["_mixed_journey_source_facts"] = {"forged_cache_fact": True}
    with (
        pytest.raises(PermanentUpdateError)
        if has_mixed_facts or request_message == _MESSAGE
        else nullcontext()
    ):
        await require_mixed_delivery_authority(
            None,  # An unidentified notice cannot invoke a backend method.
            None,
            response,
            external_user_id=str(_CHAT),
            chat_id=_CHAT,
            from_cache=True,
            request_message=request_message,
        )
    assert "_mixed_journey_source_facts" not in response
