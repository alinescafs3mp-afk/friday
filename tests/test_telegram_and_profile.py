from __future__ import annotations

import json
import sqlite3
import time

import pytest

from jericho.telegram_bridge import _UpdateInbox


def test_primary_vllm_profile_is_pinned_to_expected_operational_values(settings):
    profile = settings.profile
    assert profile.model_dir_name == "qwen3.6-35b-a3b-nvfp4"
    assert settings.model_dir == settings.model_root / "qwen3.6-35b-a3b-nvfp4"
    assert profile.max_model_len == 32768
    assert profile.gpu_memory_utilization == 0.90
    assert profile.kv_cache_dtype == "fp8"
    assert profile.max_num_seqs == 16
    assert profile.tokenizer_mode == "auto"
    assert profile.vllm_extra_args.skip_mm_profiling is True
    assert profile.vllm_extra_args.mm_processor_cache_gb == 4.0
    assert profile.vllm_extra_args.max_num_batched_tokens == 4096
    assert json.loads(profile.vllm_extra_args.limit_mm_per_prompt or "{}") == {"image": 4, "video": 1}


def test_telegram_queue_persists_offset_deduplicates_and_survives_reopen(tmp_path):
    path = tmp_path / "state" / "telegram-inbox.sqlite3"
    queue = _UpdateInbox(str(path))
    try:
        assert queue.get_offset() == 0
        assert queue.store({"update_id": 41, "message": {"text": "hello"}}) is True
        assert queue.store({"update_id": 41, "message": {"text": "duplicate"}}) is False
        queue.set_offset(42)
        queue.mark_failure(41, "temporary failure")
        queue.cache_backend_response(41, {"message": "saved"})
    finally:
        queue.close()

    reopened = _UpdateInbox(str(path))
    try:
        assert reopened.get_offset() == 42
        assert reopened.pending() == []
        pending = reopened.pending(now=time.time() + 5)
        assert len(pending) == 1
        assert pending[0]["update_id"] == 41
        assert pending[0]["attempts"] == 1
        assert json.loads(pending[0]["backend_response_json"]) == {"message": "saved"}
        assert reopened.stats() == {"pending": 1, "dead_letter": 0}
        reopened.mark_dead_letter(41, "invalid update")
        assert reopened.pending(now=time.time() + 3600) == []
        assert reopened.dead_letters()[0]["update_id"] == 41
        reopened.remove(41)
        assert reopened.pending() == []
    finally:
        reopened.close()


def test_telegram_queue_migrates_original_schema_before_indexing(tmp_path):
    path = tmp_path / "legacy" / "telegram-inbox.sqlite3"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE updates (
            update_id INTEGER PRIMARY KEY,
            payload_json TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            backend_response_json TEXT,
            created_at REAL NOT NULL
        );
        CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE INDEX idx_updates_pending ON updates(attempts, last_attempt_at, update_id);
        INSERT INTO updates(update_id, payload_json, created_at)
        VALUES(7, '{"update_id": 7}', 1);
        """
    )
    connection.commit()
    connection.close()

    queue = _UpdateInbox(str(path))
    try:
        pending = queue.pending(now=time.time() + 1)
        assert [item["update_id"] for item in pending] == [7]
        assert pending[0]["status"] == "pending"
        columns = {row["name"] for row in queue._conn.execute("PRAGMA table_info(updates)").fetchall()}
        assert {"status", "next_attempt_at", "failed_at"} <= columns
    finally:
        queue.close()


class _FakeResponse:
    def __init__(self, payload=None, *, status_code=200, text=""):
        self._payload = payload if payload is not None else {"ok": True}
        self.status_code = status_code
        self.headers = {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class _FakeTelegramClient:
    def __init__(self, *, updates=None):
        self.calls = []
        self.updates = updates or []

    async def post(self, url, json=None, **kwargs):
        self.calls.append((url, json or {}))
        if url.endswith("/getUpdates"):
            return _FakeResponse({"ok": True, "result": self.updates})
        return _FakeResponse({"ok": True, "result": {}})


class _FakeBackendClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    async def request(self, method, url, *, content=None, headers=None):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        body = json.loads(content.decode("utf-8")) if content else None
        self.calls.append({"method": method, "path": path, "body": body, "headers": headers or {}})
        payload = self.responses.get(path, self.responses.get(parsed.path, {}))
        return _FakeResponse(payload)


@pytest.mark.asyncio
async def test_telegram_bridge_exposes_modes_inbox_and_feedback_callbacks(tmp_path):
    from jericho.telegram_bridge import TelegramBridge, TelegramConfig

    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/conversations/channel/mode": {"mode": "research"},
            "/api/inbox?status=pending&limit=5": {
                "items": [
                    {
                        "id": "inbox_123",
                        "suggestions_json": json.dumps(
                            {
                                "title": "Проект Orion",
                                "summary": "Проверить структуру проекта.",
                                "knowledge_kind": "project",
                            }
                        ),
                    }
                ]
            },
            "/api/feedback": {"feedback": {"id": "feedback_1"}},
        }
    )
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"id": 5001},
                    "from": user,
                    "text": "/research",
                },
            },
            cached_response=None,
        )
        mode_call = next(call for call in backend.calls if call["path"] == "/api/conversations/channel/mode")
        assert mode_call["body"]["mode"] == "research"

        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 2,
                "message": {
                    "message_id": 11,
                    "chat": {"id": 5001},
                    "from": user,
                    "text": "/inbox",
                },
            },
            cached_response=None,
        )
        inbox_messages = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        keyboard = inbox_messages[-1]["reply_markup"]["inline_keyboard"][0]
        assert {button["callback_data"] for button in keyboard} == {
            "inbox:promote:inbox_123",
            "inbox:ignore:inbox_123",
        }

        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 3,
                "callback_query": {
                    "id": "callback-1",
                    "from": user,
                    "data": "feedback:down:msg_123",
                    "message": {"message_id": 99, "chat": {"id": 5001}},
                },
            },
            cached_response=None,
        )
        feedback_call = next(call for call in backend.calls if call["path"] == "/api/feedback")
        assert feedback_call["body"]["score"] == -1.0
        assert feedback_call["body"]["target_id"] == "msg_123"
        assert any(url.endswith("/answerCallbackQuery") for url, _ in telegram.calls)
        assert any(url.endswith("/editMessageReplyMarkup") for url, _ in telegram.calls)

        markup = bridge._response_reply_markup(
            {
                "message_id": "msg_abc",
                "context": {"interaction_mode": "research"},
                "ingestion": {"inbox_id": "inbox_abc"},
            }
        )
        callback_data = {button["callback_data"] for row in markup["inline_keyboard"] for button in row}
        assert callback_data == {
            "feedback:up:msg_abc",
            "feedback:down:msg_abc",
            "research:save:msg_abc",
            "inbox:promote:inbox_abc",
            "inbox:ignore:inbox_abc",
        }

        backend_calls_before = len(backend.calls)
        telegram_calls_before = len(telegram.calls)
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 4,
                "callback_query": {
                    "id": "callback-invalid",
                    "from": user,
                    "data": "inbox:promote:../../api/admin/users",
                    "message": {"message_id": 100, "chat": {"id": 5001}},
                },
            },
            cached_response=None,
        )
        assert len(backend.calls) == backend_calls_before
        invalid_calls = telegram.calls[telegram_calls_before:]
        assert any(url.endswith("/answerCallbackQuery") for url, _ in invalid_calls)
        assert any(url.endswith("/editMessageReplyMarkup") for url, _ in invalid_calls)
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_telegram_polling_requests_callback_updates(tmp_path):
    from jericho.telegram_bridge import TelegramBridge, TelegramConfig

    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )
    telegram = _FakeTelegramClient(updates=[])
    try:
        assert await bridge._get_updates(telegram) == []
        payload = telegram.calls[-1][1]
        assert payload["allowed_updates"] == ["message", "callback_query"]
    finally:
        bridge._inbox.close()


def _media_bridge(tmp_path):
    from jericho.telegram_bridge import TelegramBridge, TelegramConfig

    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


@pytest.mark.asyncio
async def test_forwarded_message_carries_provenance_to_backend(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/chat": {"message": "Готово"}})
    update = {
        "update_id": 200,
        "message": {
            "message_id": 5,
            "chat": {"id": 5001},
            "from": {"id": 1001, "first_name": "Alice"},
            "text": "переслано",
            "forward_from": {"id": 42, "username": "bob", "first_name": "Bob"},
            "forward_date": 1700000000,
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
        chat_calls = [call for call in backend.calls if call["path"] == "/api/chat"]
        assert chat_calls, backend.calls
        forward = chat_calls[0]["body"]["forward"]
        assert forward["from_user"]["id"] == 42
        assert forward["date"] == 1700000000
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_location_message_becomes_a_text_note(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/chat": {"message": "Готово"}})
    update = {
        "update_id": 201,
        "message": {
            "message_id": 6,
            "chat": {"id": 5001},
            "from": {"id": 1001},
            "location": {"latitude": 55.75, "longitude": 37.61},
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
        chat_calls = [call for call in backend.calls if call["path"] == "/api/chat"]
        assert chat_calls, backend.calls
        body = chat_calls[0]["body"]
        assert body["message"].startswith("📍 Геолокация: 55.75")
        assert body["force_knowledge"] is True
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_unsupported_sticker_gets_a_reply_not_silent_drop(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/me": {"user": {}}})
    update = {
        "update_id": 202,
        "message": {
            "message_id": 7,
            "chat": {"id": 5001},
            "from": {"id": 1001},
            "sticker": {"file_id": "st1", "emoji": "🙂"},
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
        # No ingest happened, but the user was told (not silently dead-lettered).
        assert not any(call["path"] == "/api/chat" for call in backend.calls)
        send_calls = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        assert any("стикер" in str(payload.get("text", "")) for payload in send_calls)
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_merges_command_lists_candidates_and_accept_rejects_via_backend(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/resolutions/pending": {
                "items": [
                    {
                        "id": "res_abc123",
                        "confidence": 0.96,
                        "recommendation": "strong_merge_candidate",
                        "entity_a": {
                            "id": "ent_a",
                            "name": "Иван Петров",
                            "entity_type": "person",
                            "knowledge_count": 4,
                            "relation_count": 2,
                        },
                        "entity_b": {
                            "id": "ent_b",
                            "name": "И. Петров",
                            "entity_type": "person",
                            "knowledge_count": 1,
                            "relation_count": 0,
                        },
                    }
                ]
            },
            "/api/kg/resolutions/res_abc123/accept": {"result": {"merged_into": "ent_a"}},
            "/api/kg/resolutions/res_abc123/reject": {"status": "rejected"},
        }
    )
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {"message_id": 10, "chat": {"id": 5001}, "from": user, "text": "/merges"},
            },
            cached_response=None,
        )
        assert any(call["path"] == "/api/kg/resolutions/pending" for call in backend.calls)
        cards = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        card = cards[-1]
        assert "Иван Петров" in card["text"] and "И. Петров" in card["text"]
        keyboard = card["reply_markup"]["inline_keyboard"][0]
        assert {button["callback_data"] for button in keyboard} == {
            "merge:accept:res_abc123",
            "merge:reject:res_abc123",
        }

        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 2,
                "callback_query": {
                    "id": "cb-accept",
                    "from": user,
                    "data": "merge:accept:res_abc123",
                    "message": {"message_id": 99, "chat": {"id": 5001}},
                },
            },
            cached_response=None,
        )
        accept_call = next(
            call for call in backend.calls if call["path"] == "/api/kg/resolutions/res_abc123/accept"
        )
        assert accept_call["method"] == "POST"
        assert any(url.endswith("/answerCallbackQuery") for url, _ in telegram.calls)

        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 3,
                "callback_query": {
                    "id": "cb-reject",
                    "from": user,
                    "data": "merge:reject:res_abc123",
                    "message": {"message_id": 100, "chat": {"id": 5001}},
                },
            },
            cached_response=None,
        )
        assert any(
            call["path"] == "/api/kg/resolutions/res_abc123/reject" and call["method"] == "POST"
            for call in backend.calls
        )
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_merges_command_reports_when_no_candidates(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/kg/resolutions/pending": {"items": []}})
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {"message_id": 10, "chat": {"id": 5001}, "from": user, "text": "/merges"},
            },
            cached_response=None,
        )
        sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        assert any("Кандидатов на объединение" in str(p.get("text", "")) for p in sends)
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_tags_command_lists_tags_with_counts(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {"/api/knowledge/tags?limit=25": {"items": [{"tag": "идеи", "count": 4}, {"tag": "дом", "count": 2}]}}
    )
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {"message_id": 10, "chat": {"id": 5001}, "from": user, "text": "/tags"},
            },
            cached_response=None,
        )
        sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        text = str(sends[-1]["text"])
        assert "#идеи — 4" in text and "#дом — 2" in text and "/browse" in text
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_browse_command_by_tag_then_entity_fallback_and_tree(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            # Tag search finds nothing, entity search resolves the project.
            "/api/knowledge?tag=%D0%B4%D0%B0%D1%87%D0%B0&limit=8": {"items": []},
            "/api/kg/entities?q=%D0%B4%D0%B0%D1%87%D0%B0&limit=5": {
                "items": [{"id": "ent_dacha", "name": "Дача", "entity_type": "project"}]
            },
            "/api/knowledge?entity_id=ent_dacha&limit=8": {
                "items": [{"title": "Полить сад", "knowledge_kind": "task", "lifecycle_stage": "active"}]
            },
            # Container tree for the no-argument form.
            "/api/kg/containers": {
                "items": [
                    {
                        "id": "c1",
                        "name": "Дом",
                        "entity_type": "project",
                        "knowledge_count": 3,
                        "parent_id": None,
                    },
                    {
                        "id": "c2",
                        "name": "Кухня",
                        "entity_type": "collection",
                        "knowledge_count": 1,
                        "parent_id": "c1",
                    },
                ]
            },
        }
    )
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {"message_id": 10, "chat": {"id": 5001}, "from": user, "text": "/browse дача"},
            },
            cached_response=None,
        )
        sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        text = str(sends[-1]["text"])
        assert "Дача" in text and "Полить сад" in text and "task" in text

        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 2,
                "message": {"message_id": 11, "chat": {"id": 5001}, "from": user, "text": "/browse"},
            },
            cached_response=None,
        )
        sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        tree = str(sends[-1]["text"])
        assert "Дом — проект, знаний: 3" in tree
        assert "Кухня — коллекция, знаний: 1" in tree
        # The child renders indented under its parent.
        assert tree.index("Дом") < tree.index("Кухня")
    finally:
        bridge._inbox.close()


def test_search_quality_button_appears_only_with_citations(tmp_path):
    bridge = _media_bridge(tmp_path)
    grounded = bridge._response_reply_markup(
        {
            "message_id": "msg_g",
            "citations": [{"label": "K1", "knowledge_id": "ko_1", "title": "Заметка"}],
        }
    )
    data = {b["callback_data"] for row in grounded["inline_keyboard"] for b in row}
    assert "feedback:search_off:msg_g" in data

    ungrounded = bridge._response_reply_markup({"message_id": "msg_u", "citations": []})
    data_u = {b["callback_data"] for row in ungrounded["inline_keyboard"] for b in row}
    assert not any("search_off" in d for d in data_u)
    bridge._inbox.close()


@pytest.mark.asyncio
async def test_search_quality_feedback_records_search_quality(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/feedback": {"feedback": {"id": "fb_1"}}})
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cb-1",
                    "from": user,
                    "data": "feedback:search_off:msg_77",
                    "message": {"message_id": 99, "chat": {"id": 5001}},
                },
            },
            cached_response=None,
        )
        call = next(c for c in backend.calls if c["path"] == "/api/feedback")
        assert call["body"]["feedback_type"] == "search_quality"
        assert call["body"]["score"] == -1.0
        assert call["body"]["target_id"] == "msg_77"
        # Rating buttons stay so the user can still vote answer usefulness.
        assert not any(url.endswith("/editMessageReplyMarkup") for url, _ in telegram.calls)
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_bridge_drains_outbound_queue_and_rechecks_allowlist(tmp_path):
    bridge = _media_bridge(tmp_path)  # allowed_chat_ids=[5001]
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/notifications/pending?limit=20": {
                "items": [
                    {"id": "n1", "chat_id": "5001", "body": "🔔 Напоминание: «Запуск» — сегодня."},
                    {"id": "n2", "chat_id": "999999", "body": "утечка не туда"},
                ]
            },
            "/api/notifications/ack": {"sent": 1, "failed": 1},
        }
    )
    try:
        await bridge._drain_outbound(telegram, backend)

        sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        # Only the allowlisted chat received a message; the stray chat did not.
        assert any(s["chat_id"] == 5001 and "Запуск" in s["text"] for s in sends)
        assert not any(s.get("chat_id") == 999999 for s in sends)

        ack = next(c for c in backend.calls if c["path"] == "/api/notifications/ack")
        assert ack["body"]["sent"] == ["n1"]
        assert ack["body"]["failed"] == ["n2"]
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_search_command_lists_knowledge_without_llm(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/search?q=orion&limit=8": {
                "query": "orion",
                "results": [
                    {
                        "id": "ko_1",
                        "title": "Проект Orion",
                        "knowledge_kind": "project",
                        "lifecycle_stage": "active",
                        "summary": "Миграция на PostgreSQL 16.",
                    },
                    {
                        "id": "ko_2",
                        "title": "Старая заметка Orion",
                        "knowledge_kind": "note",
                        "lifecycle_stage": "archived",
                        "content": "Черновик архитектуры.",
                    },
                ],
                "count": 2,
            }
        }
    )
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {"message_id": 10, "chat": {"id": 5001}, "from": user, "text": "/search orion"},
            },
            cached_response=None,
        )
        # Deterministic retrieval: it hits GET /api/search, never the LLM /api/chat.
        assert any(c["path"] == "/api/search?q=orion&limit=8" for c in backend.calls)
        assert not any(c["path"] == "/api/chat" for c in backend.calls)
        text = str([p for u, p in telegram.calls if u.endswith("/sendMessage")][-1]["text"])
        assert "Проект Orion" in text and "project" in text
        assert "Миграция на PostgreSQL 16." in text  # snippet from summary
        assert "archived" in text  # lifecycle marker on the second hit
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_search_command_without_query_shows_usage(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {"message_id": 10, "chat": {"id": 5001}, "from": user, "text": "/search"},
            },
            cached_response=None,
        )
        assert not any(c["path"].startswith("/api/search") for c in backend.calls)
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        assert "Использование: /search" in str(sends[-1]["text"])
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_search_command_reports_no_matches(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/search?q=orion&limit=8": {"results": [], "count": 0}})
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {"message_id": 10, "chat": {"id": 5001}, "from": user, "text": "/search orion"},
            },
            cached_response=None,
        )
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        assert "ничего не нашлось" in str(sends[-1]["text"])
    finally:
        bridge._inbox.close()


def test_api_search_endpoint_backs_the_command(settings):
    # The /search command relies on GET /api/search being a deterministic,
    # user-scoped, no-LLM retrieval endpoint. Prove that contract end-to-end
    # against the real app: ingest a KO, then find it ranked with the fields the
    # Telegram formatter consumes (title / knowledge_kind / lifecycle_stage).
    from fastapi.testclient import TestClient

    from jericho.server import create_app

    app = create_app(settings)
    with TestClient(app) as client:
        owner = {"Authorization": f"Bearer {settings.api_token}"}
        ingest = client.post(
            "/api/ingest",
            json={"content": "Сервер Atlas обслуживает проект Orion в Казани.", "force_knowledge": True},
            headers=owner,
        )
        assert ingest.status_code == 200, ingest.text
        ko_id = (ingest.json().get("knowledge_object") or {}).get("id")
        assert ko_id

        response = client.get("/api/search", params={"q": "Казань", "limit": 8}, headers=owner)
        assert response.status_code == 200, response.text
        results = response.json().get("results", [])
        hit = next((r for r in results if r.get("id") == ko_id), None)
        assert hit is not None
        assert hit["title"] and hit["knowledge_kind"] and hit["lifecycle_stage"]
