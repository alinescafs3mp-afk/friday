from __future__ import annotations

import base64
import json
import sqlite3
import time
from urllib.parse import quote

import pytest

from friday.telegram_bridge import _UpdateInbox


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
        assert {"status", "next_attempt_at", "failed_at", "ordering_key"} <= columns
        assert pending[0]["ordering_key"] == "update:7"
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
        # sendMessage uses json=; sendDocument uses data=/files= multipart.
        if json is not None:
            self.calls.append((url, json))
        else:
            self.calls.append(
                (
                    url,
                    {
                        "data": kwargs.get("data"),
                        "files": kwargs.get("files"),
                    },
                )
            )
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
        # Plain-text backend routes (G20 export) store a str; keep JSON for the rest.
        if isinstance(payload, str):
            return _FakeResponse({}, text=payload)
        if isinstance(payload, tuple) and len(payload) == 2:
            status, body = payload
            if isinstance(body, str):
                return _FakeResponse({}, status_code=int(status), text=body)
            return _FakeResponse(body, status_code=int(status))
        return _FakeResponse(payload)


@pytest.mark.asyncio
async def test_telegram_bridge_exposes_modes_inbox_and_feedback_callbacks(tmp_path):
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

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
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

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
        # edited_message принимается, чтобы честно сказать «правки не подхватываю»
        # — иначе чат и база молча расходятся навсегда.
        assert payload["allowed_updates"] == ["message", "edited_message", "callback_query"]
    finally:
        bridge._inbox.close()


def _media_bridge(tmp_path):
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

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
async def test_note_command_marks_the_turn_as_an_explicit_save(tmp_path):
    """`/note` survives the safe review default by sending force_knowledge."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/chat": {"message": "Готово"}})
    update = {
        "update_id": 203,
        "message": {
            "message_id": 8,
            "chat": {"id": 5001},
            "from": {"id": 1001},
            "text": "/note синтетическая заметка о тестовом кластере",
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
        chat_calls = [call for call in backend.calls if call["path"] == "/api/chat"]
        assert len(chat_calls) == 1
        body = chat_calls[0]["body"]
        assert body["message"] == "синтетическая заметка о тестовом кластере"
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
async def test_conflicts_command_lists_and_decides_via_backend(tmp_path):
    """G4: conflicts had no chat path; /conflicts mirrors /merges for knowledge pairs."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/conflicts": {
                "items": [
                    {
                        "id": "kc_abc123",
                        "conflict_type": "near_duplicate",
                        "confidence": 0.91,
                        "knowledge_a_title": "Штатка март",
                        "knowledge_a_summary": "Первая редакция",
                        "knowledge_b_title": "Штатка март копия",
                        "knowledge_b_summary": "Повтор загрузки",
                    }
                ],
                "count": 1,
                "total": 200,
            },
            "/api/kg/conflicts/kc_abc123/decide": {"status": "dismissed"},
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
                    "text": "/conflicts",
                },
            },
            cached_response=None,
        )
        assert any(
            call["path"].startswith("/api/kg/conflicts") and call["method"] == "GET" for call in backend.calls
        )
        cards = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        assert any("200" in str(c.get("text", "")) for c in cards)
        card = cards[-1]
        assert "Штатка март" in card["text"]
        buttons = {
            button["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for button in row
        }
        assert buttons == {
            "conflict:keep_a:kc_abc123",
            "conflict:keep_b:kc_abc123",
            "conflict:dismiss:kc_abc123",
        }

        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 2,
                "callback_query": {
                    "id": "cb-dismiss",
                    "from": user,
                    "data": "conflict:dismiss:kc_abc123",
                    "message": {"message_id": 99, "chat": {"id": 5001}},
                },
            },
            cached_response=None,
        )
        decide = next(call for call in backend.calls if call["path"] == "/api/kg/conflicts/kc_abc123/decide")
        assert decide["method"] == "POST"
        assert decide["body"]["decision"] == "dismiss"
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
async def test_profile_command_shows_the_object_view(tmp_path):
    """Спека v3 §6 «вид объекта», детерминированная Telegram-команда (не
    зависит от того, решит ли модель позвать инструмент — см. TASKS.md #72).
    """
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/entity-profile?name=%D0%90%D1%82%D0%BB%D0%B0%D1%81": {
                "entity": {"id": "ent-1", "name": "Атлас"},
                "relations": [{"id": "rel-1"}],
                "pending_relations_count": 2,
                "knowledge_objects": [{"title": "Отчёт за январь"}, {"title": "Смета"}],
                "profile": {
                    "tags": ["отчёт", "смета"],
                    "document_date_range": {"earliest": "2026-01-15", "latest": "2026-03-02"},
                    "documents_without_own_date": 0,
                },
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
                "message": {"message_id": 10, "chat": {"id": 5001}, "from": user, "text": "/profile Атлас"},
            },
            cached_response=None,
        )
        sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        text = str(sends[-1]["text"])
        assert "Атлас" in text
        assert "#отчёт" in text and "#смета" in text
        assert "2026-01-15" in text and "2026-03-02" in text
        assert "Отчёт за январь" in text and "Смета" in text
        assert "1 подтверждено" in text and "2 ждут проверки" in text
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_profile_command_shows_when_an_event_occurred(tmp_path):
    """Спека v3 §4: occurred_at — отдельная строка от «даты документов», не
    смешивается с ними. Мутация: убрать блок `if event_time:` в
    `_send_entity_profile` — эта проба обязана покраснеть."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/entity-profile?name=%D0%A1%D0%BE%D0%B2%D0%B5%D1%89%D0%B0%D0%BD%D0%B8%D0%B5": {
                "entity": {"id": "ent-1", "name": "Совещание"},
                "relations": [],
                "pending_relations_count": 0,
                "knowledge_objects": [],
                "profile": {"tags": [], "document_date_range": None, "documents_without_own_date": 0},
                "event_time": {"occurred_at": "2026-05-10", "occurred_end": "2026-05-11"},
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
                "message": {
                    "message_id": 10,
                    "chat": {"id": 5001},
                    "from": user,
                    "text": "/profile Совещание",
                },
            },
            cached_response=None,
        )
        sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        text = str(sends[-1]["text"])
        assert "Когда произошло: 2026-05-10 — 2026-05-11" in text
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_profile_command_reports_not_found_honestly(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {"/api/kg/entity-profile?name=%D0%9D%D0%B5%D1%82": (404, "Сущность не найдена")}
    )
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {"message_id": 10, "chat": {"id": 5001}, "from": user, "text": "/profile Нет"},
            },
            cached_response=None,
        )
        sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        text = str(sends[-1]["text"])
        assert "ничего не нашлось" in text
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
    assert "doc:show:ko_1" in data

    ungrounded = bridge._response_reply_markup({"message_id": "msg_u", "citations": []})
    data_u = {b["callback_data"] for row in ungrounded["inline_keyboard"] for b in row}
    assert not any("search_off" in d for d in data_u)
    assert not any(item.startswith("doc:show:") for item in data_u)
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
async def test_a_voice_attachment_is_delivered_as_a_native_telegram_voice_message(tmp_path):
    """The `speak` tool's clip must actually reach the user, not just exist on the
    response dict. Mutation: stop calling `self._send_voice` inside
    `_deliver_voice_reply` — this must go red on the missing `/sendVoice` call."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    audio_bytes = b"OggS-fake-opus-payload"
    response = {
        "message": "Привет!",
        "voice": {
            "kind": "voice",
            "mime_type": "audio/ogg",
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
            "duration_sec": 1.5,
        },
    }

    await bridge._deliver_voice_reply(telegram, 5001, response)

    voice_calls = [payload for url, payload in telegram.calls if url.endswith("/sendVoice")]
    assert len(voice_calls) == 1
    data, files = voice_calls[0]["data"], voice_calls[0]["files"]
    assert data["chat_id"] == "5001"
    filename, content, mime_type = files["voice"]
    assert content == audio_bytes
    assert mime_type == "audio/ogg"
    assert filename.endswith(".ogg")


@pytest.mark.asyncio
async def test_no_voice_attachment_means_no_send_voice_call(tmp_path):
    """A text-only turn (the common case: `speak` was never asked for) must not
    produce a `sendVoice` call. Mutation: force `encoded` to a valid base64 value
    regardless of `response["voice"]` — this must go red."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()

    await bridge._deliver_voice_reply(telegram, 5001, {"message": "Привет!"})

    assert not any(url.endswith("/sendVoice") for url, _ in telegram.calls)


@pytest.mark.asyncio
async def test_a_failed_voice_delivery_does_not_break_the_turn(tmp_path, monkeypatch):
    """`sendVoice` failing (rate limit, oversized clip, transient network error)
    must not turn an already-successful text answer into an exception for the
    caller. Mutation: remove the try/except around `self._send_voice` — this
    must go red with a propagated exception."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()

    async def _boom(*args, **kwargs):
        raise RuntimeError("HTTP 429: rate limited")

    monkeypatch.setattr(bridge, "_send_voice", _boom)

    response = {
        "message": "Привет!",
        "voice": {"audio_base64": base64.b64encode(b"x").decode("ascii")},
    }
    await bridge._deliver_voice_reply(telegram, 5001, response)  # must not raise


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

    from friday.server import create_app

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


@pytest.mark.asyncio
async def test_register_commands_sets_the_telegram_menu(tmp_path):
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    try:
        await bridge._register_commands(telegram)
        posted = [payload for url, payload in telegram.calls if url.endswith("/setMyCommands")]
        assert posted, telegram.calls
        commands = posted[0]["commands"]
        names = {c["command"] for c in commands}
        assert {"search", "note", "inbox", "help", "status"} <= names
        # Valid Telegram command tokens: lowercase, no leading slash, described.
        assert all(c["command"].islower() and "/" not in c["command"] and c["description"] for c in commands)
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_dead_lettered_update_replies_to_the_user(tmp_path, monkeypatch):
    from friday.telegram_bridge import PermanentUpdateError

    bridge = _media_bridge(tmp_path)  # allowlist [5001]
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})

    private = "BRIDGE-DEAD-LETTER-SENTINEL-7d2e41"

    async def _reject(*args, **kwargs):
        raise PermanentUpdateError(f"backend echoed private text {private}")

    monkeypatch.setattr(bridge, "_process_update", _reject)
    bridge._inbox.store(
        {"update_id": 601, "message": {"chat": {"id": 5001}, "from": {"id": 5001}, "text": "hi"}}
    )
    try:
        await bridge._drain_inbox(telegram, backend)
        await bridge._await_inflight_updates()
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        assert sends and sends[-1]["chat_id"] == 5001
        assert "отклонено" in sends[-1]["text"]  # the user is told, not left silent
        assert bridge._inbox.stats()["dead_letter"] == 1
        dead = bridge._inbox.dead_letters()[0]
        assert dead["last_error"] == "PermanentUpdateError"
        assert private not in json.dumps(dead, ensure_ascii=False)
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_transient_update_failure_isolated_by_chat_without_reordering(tmp_path, monkeypatch):
    """A retryable update blocks its own chat, not the globally ordered inbox."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})
    attempts: list[int] = []
    processed: list[int] = []

    private = "BRIDGE-RETRY-SENTINEL-914c3b"

    async def _process(*args, **kwargs):
        update = args[2]
        update_id = int(update["update_id"])
        attempts.append(update_id)
        if update_id == 602 and attempts.count(update_id) == 1:
            raise RuntimeError(f"temporary backend echoed {private}")
        processed.append(update_id)

    monkeypatch.setattr(bridge, "_process_update", _process)
    for update_id, chat_id in ((601, 5001), (602, 5002), (603, 5002), (604, 5003)):
        bridge._inbox.store(
            {
                "update_id": update_id,
                "message": {"chat": {"id": chat_id}, "from": {"id": chat_id}, "text": "hi"},
            }
        )

    try:
        await bridge._drain_inbox(telegram, backend)
        await bridge._await_inflight_updates()

        # 604 belongs to another chat and must cross the failed 602 in this same
        # drain. 603 belongs to 602's chat and must not overtake it.
        assert attempts == [601, 602, 604]
        assert processed == [601, 604]
        pending = bridge._inbox.pending(now=time.time() + 3600)
        assert [row["update_id"] for row in pending] == [602]
        assert pending[0]["last_error"] == "RuntimeError"
        assert private not in json.dumps(pending[0], ensure_ascii=False)

        # The retry delay on 602 must keep 603 blocked on the next tick as well.
        await bridge._drain_inbox(telegram, backend)
        await bridge._await_inflight_updates()
        assert attempts == [601, 602, 604]

        bridge._inbox._conn.execute("UPDATE updates SET next_attempt_at=0 WHERE update_id=602")
        bridge._inbox._conn.commit()
        await bridge._drain_inbox(telegram, backend)
        await bridge._await_inflight_updates()
        assert attempts == [601, 602, 604, 602, 603]
        assert processed == [601, 604, 602, 603]
        assert bridge._inbox.stats() == {"pending": 0, "dead_letter": 0}
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_dead_letter_notice_is_denied_for_unallowlisted_chat(tmp_path):
    bridge = _media_bridge(tmp_path)  # allowlist [5001]
    telegram = _FakeTelegramClient()
    stray = {"message": {"chat": {"id": 999999}, "from": {"id": 999999}}}
    try:
        await bridge._notify_dead_letter(telegram, stray, permanent=True)
        assert not any(u.endswith("/sendMessage") for u, _ in telegram.calls)
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_a_self_registered_newcomer_hears_about_a_failure_instead_of_silence(tmp_path):
    """Открытая регистрация впускает чат, которого нет в статическом списке.

    Такому человеку бот уже шлёт исходящие и отвечает на кнопки — те две ветки
    спрашивают «в списке ИЛИ уже впущен». Уведомление о неудаче спрашивало только
    «в списке», поэтому у новичка сообщение просто исчезало: ни ответа, ни отказа.
    Он единственный, кто не может знать, почему бот молчит.

    Мутация: вернуть проверку `chat_id in self.config.allowed_chat_ids` — тест
    обязан покраснеть.
    """
    bridge = _media_bridge(tmp_path)  # allowlist [5001]
    telegram = _FakeTelegramClient()
    newcomer = {"message": {"chat": {"id": 7777}, "from": {"id": 7777}}}
    try:
        # До регистрации — по-прежнему тишина: deny-by-default не ослаблен.
        await bridge._notify_dead_letter(telegram, newcomer, permanent=True)
        assert not any(u.endswith("/sendMessage") for u, _ in telegram.calls)

        bridge._inbox.remember_registered_chat(7777)
        await bridge._notify_dead_letter(telegram, newcomer, permanent=True)
        sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
        assert sends, "впущенный открытой регистрацией чат остался без уведомления"
        assert sends[0]["chat_id"] == 7777
        assert "отклонено" in str(sends[0]["text"])
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_why_before_any_answer_explains_itself_instead_of_dead_letter(tmp_path):
    """/why в чате без единого ответа упирается в 404 бекенда.

    Раньше 404 превращался в PermanentUpdateError, обновление уходило в
    dead-letter, и человек сразу после /new читал «⚠️ сообщение отклонено» —
    текст, написанный для настоящих сбоев. «Ещё нечего объяснять» — не сбой.
    """
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )
    telegram = _FakeTelegramClient()

    class _WhyMissingBackend(_FakeBackendClient):
        async def request(self, method, url, *, content=None, headers=None):
            from urllib.parse import urlsplit

            if urlsplit(url).path == "/api/conversations/channel/why":
                self.calls.append({"method": method, "path": urlsplit(url).path})
                return _FakeResponse({"detail": "No conversation in this channel yet"}, status_code=404)
            return await super().request(method, url, content=content, headers=headers)

    backend = _WhyMissingBackend({})
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 41,
                "message": {
                    "message_id": 90,
                    "chat": {"id": 5001},
                    "from": {"id": 1001, "first_name": "Alice"},
                    "text": "/why",
                },
            },
            cached_response=None,
        )
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        assert sends, "владелец остался без ответа"
        assert "нечего объяснять" in sends[-1]["text"]
        assert "отклонено" not in sends[-1]["text"]
        assert bridge._inbox.stats().get("dead_letter", 0) == 0
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_why_names_its_sources_instead_of_bare_labels(tmp_path):
    """«Источники: K1, K10, K2» бесполезны, когда легенда 📎 уехала вверх по чату."""
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

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
            "/api/conversations/channel/why": {
                "search_query": "поверка",
                "answer_mode": "personal_knowledge",
                "knowledge_hits": 3,
                "citations": {"K10": "ko_c", "K1": "ko_a", "K2": "ko_b"},
                "trace": [
                    {"id": "ko_a", "title": "Приказ о поверке"},
                    {"id": "ko_b", "title": "Ведомость"},
                    {"id": "ko_c", "title": "Рапорт"},
                ],
            }
        }
    )
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 42,
                "message": {
                    "message_id": 91,
                    "chat": {"id": 5001},
                    "from": {"id": 1001, "first_name": "Alice"},
                    "text": "/why",
                },
            },
            cached_response=None,
        )
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        text = sends[-1]["text"]
        assert "[K1] Приказ о поверке" in text
        assert "[K2] Ведомость" in text
        # Числовой порядок меток, не строковый: K10 стоит ПОСЛЕ K2.
        assert text.index("[K2]") < text.index("[K10]")
    finally:
        bridge._inbox.close()


def test_the_file_fate_reaches_the_chat():
    """Бэкенд честно возвращал file_ingestion с каждым файлом — и ни бридж, ни
    модель его не читали. Владелец отправлял документ и не знал, стал тот
    знанием или завис в Inbox, а это разные следующие шаги."""
    from friday.telegram_bridge import TelegramBridge

    promoted = TelegramBridge._format_response_message(
        {"message": "Принял.", "file_ingestion": {"promoted": True}}
    )
    assert "стал знанием" in promoted

    queued = TelegramBridge._format_response_message(
        {
            "message": "Принял.",
            "file_ingestion": {
                "promoted": False,
                "queued_for_review": True,
                "inbox_id": "in_1",
                "extraction_success": False,
            },
        }
    )
    assert "/inbox" in queued
    assert "не удалось" in queued  # нечитаемый файл называет себя

    transient = TelegramBridge._format_response_message(
        {"message": "Принял.", "file_ingestion": {"action": "transient", "promoted": False}}
    )
    assert "НЕ сохранён" in transient

    plain = TelegramBridge._format_response_message({"message": "Ответ."})
    assert "стал знанием" not in plain and "/inbox" not in plain


def _ux_bridge(tmp_path):
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )


@pytest.mark.asyncio
async def test_a_button_press_retires_only_its_row(tmp_path):
    """Клавиатура ответа несёт до трёх НЕЗАВИСИМЫХ рядов, а очистка стирала все:
    👍 уносил с собой «✓ Подтвердить знание», и заметка зависала в pending."""
    bridge = _ux_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/feedback": {"feedback": {"id": "f1"}}})
    markup = {
        "inline_keyboard": [
            [{"text": "👍", "callback_data": "feedback:up:msg_1"}],
            [{"text": "✓ Подтвердить знание", "callback_data": "inbox:promote:in_1"}],
        ]
    }
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 51,
                "callback_query": {
                    "id": "cb-1",
                    "from": {"id": 1001},
                    "data": "feedback:up:msg_1",
                    "message": {"message_id": 77, "chat": {"id": 5001}, "reply_markup": markup},
                },
            },
            cached_response=None,
        )
        edits = [p for u, p in telegram.calls if u.endswith("/editMessageReplyMarkup")]
        assert edits, "клавиатура не перерисована"
        remaining = edits[-1]["reply_markup"]["inline_keyboard"]
        flat = [b["callback_data"] for row in remaining for b in row]
        assert flat == ["inbox:promote:in_1"], "чужой ряд кнопок унесло вместе с нажатым"
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_browse_with_namesakes_offers_a_choice(tmp_path):
    """Однофамильцы гарантированы; молчаливый выбор первого делал записи
    остальных несуществующими."""
    bridge = _ux_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/entities": {
                "items": [
                    {"id": "ent_a", "name": "Тарасов И.И.", "entity_type": "person"},
                    {"id": "ent_b", "name": "Тарасов П.С.", "entity_type": "person"},
                ]
            },
            "/api/kg/entities/ent_b": {
                "entity": {"id": "ent_b", "name": "Тарасов П.С."},
                "knowledge": [{"id": "ko_1", "title": "Личное дело", "summary": "сводка"}],
            },
        }
    )
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 52,
                "message": {"message_id": 80, "chat": {"id": 5001}, "from": user, "text": "/browse Тарасов"},
            },
            cached_response=None,
        )
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        chooser = sends[-1]
        assert "несколькими сущностями" in chooser["text"]
        buttons = [b["callback_data"] for row in chooser["reply_markup"]["inline_keyboard"] for b in row]
        assert buttons == ["ent:browse:ent_a", "ent:browse:ent_b"]

        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 53,
                "callback_query": {
                    "id": "cb-2",
                    "from": user,
                    "data": "ent:browse:ent_b",
                    "message": {"message_id": 81, "chat": {"id": 5001}},
                },
            },
            cached_response=None,
        )
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        assert "Тарасов П.С." in sends[-1]["text"]
        assert "Личное дело" in sends[-1]["text"]
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_source_command_searches_raw_files(tmp_path):
    """93% загруженных знаков живут только в raw_objects; из Telegram provenance-поиск
    был недостижим — только с хоста через админку."""
    bridge = _ux_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/knowledge/sources": {
                "items": [
                    {
                        "id": "raw_1",
                        "source_ref": "приказ_112.docx",
                        "excerpt": "…дословная фраза из документа…",
                        "knowledge_object_id": "ko_9",
                    }
                ],
                "count": 1,
            }
        }
    )
    user = {"id": 1001, "first_name": "Alice"}
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 54,
                "message": {
                    "message_id": 82,
                    "chat": {"id": 5001},
                    "from": user,
                    "text": "/source дословная фраза",
                },
            },
            cached_response=None,
        )
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        text = sends[-1]["text"]
        assert "приказ_112.docx" in text
        assert "дословная фраза" in text
        buttons = [b["callback_data"] for row in sends[-1]["reply_markup"]["inline_keyboard"] for b in row]
        assert buttons == ["doc:show:ko_9"]
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_an_edited_message_gets_an_honest_answer(tmp_path):
    """Правка сообщения не подхватывается — и раньше система просто не знала о ней:
    в чате один текст, в базе навсегда другой."""
    bridge = _ux_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 55,
                "edited_message": {
                    "message_id": 83,
                    "chat": {"id": 5001},
                    "from": {"id": 1001},
                    "text": "/note дежурный — Сидоров",
                },
            },
            cached_response=None,
        )
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        assert sends and "не подхватываю" in sends[-1]["text"]
        assert not backend.calls, "правка ушла в бекенд, хотя объявлена неподдержанной"
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_live_location_pings_do_not_spam_the_edit_notice(tmp_path):
    """Найдено состязательным ревью: Telegram доставляет каждый тик Live Location
    как edited_message с тем же message_id и БЕЗ text/caption — только меняющиеся
    координаты. Без проверки на текст это читалось как «пользователь исправил
    сообщение», и бот отвечал фразой «правку не подхватываю» на каждый пинг,
    раз в несколько секунд, пока трансляция активна.

    Мутация: убери проверку `edited.get("text") or edited.get("caption")` перед
    ранним `return` — тест обязан покраснеть (появится sendMessage на пустой ход)."""
    bridge = _ux_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})
    try:
        for update_id, lat, lon in [(60, 55.7558, 37.6173), (61, 55.7559, 37.6174)]:
            await bridge._process_update(
                telegram,
                backend,
                {
                    "update_id": update_id,
                    "edited_message": {
                        "message_id": 90,
                        "chat": {"id": 5001},
                        "from": {"id": 1001},
                        "location": {"latitude": lat, "longitude": lon, "live_period": 900},
                    },
                },
                cached_response=None,
            )
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        assert not sends, f"живая геолокация не должна вызывать sendMessage вовсе: {sends}"
        assert not backend.calls
    finally:
        bridge._inbox.close()


class _DeadBackend:
    async def get(self, url):
        raise RuntimeError("connection refused")


@pytest.mark.asyncio
async def test_the_bridge_warns_the_owner_when_backend_is_down(tmp_path):
    """Sentinel живёт ВНУТРИ backend и умирает вместе с ним: о падении не могло
    сообщить ничто, владелец узнавал по тишине бота спустя часы. Мост опрашивает
    backend каждые 15 секунд и держит токен бота — он первый узнаёт и
    единственный может сказать."""
    bridge = _ux_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    try:
        await bridge._warn_owner_if_backend_down(telegram, _DeadBackend())
        assert not [u for u, _ in telegram.calls if u.endswith("/sendMessage")], (
            "тревога раньше грейс-периода: каждый рестарт супервизора стал бы тревогой"
        )

        bridge._backend_down_since -= 300  # авария длится уже пять минут
        await bridge._warn_owner_if_backend_down(telegram, _DeadBackend())
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        assert sends and "не отвечает" in sends[-1]["text"]
        assert sends[-1]["chat_id"] == 5001

        await bridge._warn_owner_if_backend_down(telegram, _DeadBackend())
        assert len([p for u, p in telegram.calls if u.endswith("/sendMessage")]) == 1, (
            "повтор той же аварии должен молчать сутки"
        )

        await bridge._notify_backend_recovered(telegram)
        sends = [p for u, p in telegram.calls if u.endswith("/sendMessage")]
        assert "снова отвечает" in sends[-1]["text"], "тревога без отбоя учит игнорировать тревоги"
        assert bridge._backend_down_warned_at == 0.0
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_a_living_backend_resets_the_watchdog(tmp_path):
    """Сбой цикла может быть и на стороне Telegram: живой backend сбрасывает счёт."""
    bridge = _ux_bridge(tmp_path)
    telegram = _FakeTelegramClient()

    class _LiveBackend:
        async def get(self, url):
            return _FakeResponse({"status": "ok"})

    try:
        bridge._backend_down_since = 1.0
        await bridge._warn_owner_if_backend_down(telegram, _LiveBackend())
        assert bridge._backend_down_since == 0.0
        assert not [u for u, _ in telegram.calls if u.endswith("/sendMessage")]
    finally:
        bridge._inbox.close()


def test_registered_chats_table_remembers_and_forgets_nothing(tmp_path):
    """Direct unit test of the small table `_commands.py` writes to and every
    other gate point reads from. Durable and idempotent: re-admitting the same
    chat must not error or duplicate the row."""
    from friday.telegram_bridge import _UpdateInbox

    queue = _UpdateInbox(str(tmp_path / "telegram.sqlite3"))
    try:
        assert queue.is_registered_chat(7777) is False
        queue.remember_registered_chat(7777)
        queue.remember_registered_chat(7777)  # idempotent, must not raise
        assert queue.is_registered_chat(7777) is True
        assert queue.is_registered_chat(9999) is False
    finally:
        queue.close()

    reopened = _UpdateInbox(str(tmp_path / "telegram.sqlite3"))
    try:
        # Durable across restarts -- see the docstring on remember_registered_chat
        # for why this matters: losing it would silently re-lock out a person
        # who already has a real account, until their next message.
        assert reopened.is_registered_chat(7777) is True
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_open_registration_admits_a_private_stranger_and_remembers_the_chat(tmp_path):
    """Bridge-level counterpart to the backend test in test_api_vertical_slice.py:
    a private chat outside the static allowlist is processed (not silently
    dropped) when open_registration is on, and gets recorded so later gate
    points (callbacks, outbound push) recognise it without re-deriving
    "private" from a payload they do not have.
    """
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
            open_registration=True,
        )
    )
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    # A private chat: Telegram's own `type` field, not the
                    # id-equality heuristic used elsewhere -- the payload the
                    # gate actually has to work with.
                    "chat": {"id": 7777, "type": "private"},
                    "from": {"id": 7777, "first_name": "Newcomer"},
                    "text": "/research",
                },
            },
            cached_response=None,
        )
        assert backend.calls, "a stranger's private message was silently dropped"
        assert bridge._inbox.is_registered_chat(7777) is True
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_open_registration_does_not_admit_an_unlisted_group(tmp_path):
    """The exception is PRIVATE chats only. An unlisted group must stay silently
    refused even with the flag on -- otherwise open registration is a second,
    wider allowlist by accident, not what it was built for."""
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
            open_registration=True,
        )
    )
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})
    try:
        await bridge._process_update(
            telegram,
            backend,
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"id": 9001, "type": "group"},
                    "from": {"id": 1234, "first_name": "Someone"},
                    "text": "/research",
                },
            },
            cached_response=None,
        )
        assert not backend.calls, "an unlisted group was admitted by open registration"
        assert bridge._inbox.is_registered_chat(9001) is False
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_a_registered_chat_can_trigger_callbacks_without_being_statically_allowed(tmp_path):
    """`_callbacks.py` trusts `registered_chats`, not a live re-derivation of
    "private" (a callback carries no chat.type worth trusting for this). A chat
    admitted earlier through open registration must be able to press buttons."""
    from friday.telegram_bridge import TelegramBridge, TelegramConfig

    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
            open_registration=True,
        )
    )
    bridge._inbox.remember_registered_chat(7777)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})
    try:
        await bridge._process_callback_query(
            telegram,
            backend,
            {
                "id": "cb1",
                "from": {"id": 7777},
                "message": {"message_id": 1, "chat": {"id": 7777, "type": "private"}},
                "data": "conflict:dismiss:x",
            },
        )
        answers = [call for call in telegram.calls if call[0].endswith("/answerCallbackQuery")]
        assert answers, "callback got no answer at all"
        assert answers[0][1].get("text") != "Действие недоступно", (
            "a chat already admitted by open registration was refused a callback"
        )
    finally:
        bridge._inbox.close()


def _callback_update(data: str, *, chat_id: int = 5001, user_id: int = 5001) -> dict:
    return {
        "update_id": 900,
        "callback_query": {
            "id": "cbq-1",
            "data": data,
            "from": {"id": user_id},
            "message": {"message_id": 77, "chat": {"id": chat_id}},
        },
    }


@pytest.mark.asyncio
async def test_the_object_card_offers_actions_not_just_a_read_out(tmp_path):
    """Спека v3 §6: от объекта — к разрешённым действиям.

    Карточка была тупиком на чтение, а 4349 узлов-людей и 149 войсковых частей
    заведены АВТОМАТИЧЕСКИМИ правилами: первая же ошибка извлечения чинилась
    только уходом в админку — при том что Telegram основной интерфейс.

    Мутация: вернуть `reply_markup` только с кнопкой отката — тест обязан
    покраснеть на «Тип» и «Удалить».
    """
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/entity-profile": {
                "entity": {"id": "ent_abc123", "name": "Иванов", "entity_type": "person"},
                "profile": {"tags": [], "document_date_range": None, "documents_without_own_date": 0},
                "relations": [],
                "knowledge_objects": [],
                "knowledge_objects_total": 0,
                "pending_relations_count": 0,
                "event_time": None,
                "edits": {"versions": 1, "last_edited_at": None, "restorable_version": None},
            }
        }
    )
    try:
        await bridge._send_entity_profile(telegram, backend, 5001, "5001", {"id": 5001}, "Иванов")
    finally:
        bridge._inbox.close()

    sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
    assert sends, "карточка не отправлена"
    markup = sends[0].get("reply_markup")
    assert markup, "у карточки нет ни одного действия"
    labels = [button["callback_data"] for row in markup["inline_keyboard"] for button in row]
    assert any(item.startswith("ent:browse:") for item in labels), "нет перехода к документам"
    assert any(item.startswith("ent:types:") for item in labels), "нельзя исправить тип"
    assert any(item.startswith("ent:del:") for item in labels), "нельзя убрать ошибочный узел"
    # Откатывать нечего — кнопки отката быть не должно.
    assert not any(item.startswith("ent:undo:") for item in labels)
    for item in labels:
        assert len(item.encode()) <= 64, f"callback_data длиннее 64 байт: {item}"


@pytest.mark.asyncio
async def test_changing_the_object_type_goes_through_the_authorized_route(tmp_path):
    """Кнопка не меняет данные сама — она зовёт тот же гейтованный маршрут.

    Мутация: слать `PUT`/чужой путь или менять тип на стороне моста — тест
    обязан покраснеть.
    """
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {"/api/kg/entities/ent_abc123": {"entity": {"id": "ent_abc123", "name": "Меркурий"}}}
    )
    try:
        await bridge._process_callback_query(
            telegram, backend, _callback_update("ent:type:ent_abc123.organization")["callback_query"]
        )
    finally:
        bridge._inbox.close()

    patch_calls = [call for call in backend.calls if call["method"] == "PATCH"]
    assert patch_calls, "тип менялся мимо backend"
    assert patch_calls[0]["path"] == "/api/kg/entities/ent_abc123"
    assert patch_calls[0]["body"]["entity_type"] == "organization"


@pytest.mark.asyncio
async def test_an_unknown_entity_type_is_refused_before_the_backend(tmp_path):
    """Значение типа — перечисление; строка из кнопки не должна доезжать до
    хранилища непроверенной."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})
    try:
        # Обработчик сам превращает отказ в ответ человеку — проверяем результат,
        # а не исключение: наружу оно и не должно выходить.
        await bridge._process_callback_query(
            telegram, backend, _callback_update("ent:type:ent_abc123.risk")["callback_query"]
        )
        assert not backend.calls, "непроверенный тип ушёл в backend"
        answers = [payload for url, payload in telegram.calls if url.endswith("/answerCallbackQuery")]
        assert answers and "недоступно" in str(answers[-1].get("text") or ""), "человеку не сказано об отказе"
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_deleting_an_object_asks_first_and_only_the_person_who_asked(tmp_path):
    """Удаление подтверждается, и чужая кнопка не срабатывает.

    Тот же дефект уже ловили на удалении разговора: приглашение видно всему
    чату, а открытая регистрация делает второй способный аккаунт достижимым.

    Мутация: убрать проверку `invoker != external_user_id` — тест обязан
    покраснеть.
    """
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/kg/entities/ent_abc123": {"status": "soft_deleted"}})
    try:
        # Шаг 1: спрашиваем подтверждение, ничего не удаляя.
        await bridge._process_callback_query(
            telegram, backend, _callback_update("ent:del:ent_abc123")["callback_query"]
        )
        assert not [call for call in backend.calls if call["method"] == "DELETE"], (
            "объект удалён без подтверждения"
        )
        confirm = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")][-1]
        buttons = [b["callback_data"] for row in confirm["reply_markup"]["inline_keyboard"] for b in row]
        assert "ent:delyes:ent_abc123.5001" in buttons, "приглашение не несёт id вызвавшего"

        # Шаг 2: жмёт ДРУГОЙ человек — отказ.
        await bridge._process_callback_query(
            telegram,
            backend,
            _callback_update("ent:delyes:ent_abc123.5001", user_id=6002)["callback_query"],
        )
        assert not [call for call in backend.calls if call["method"] == "DELETE"], (
            "чужая кнопка удалила объект"
        )

        # Шаг 3: жмёт тот, кто открыл карточку.
        await bridge._process_callback_query(
            telegram,
            backend,
            _callback_update("ent:delyes:ent_abc123.5001", user_id=5001)["callback_query"],
        )
        deletes = [call for call in backend.calls if call["method"] == "DELETE"]
        assert deletes and deletes[0]["path"] == "/api/kg/entities/ent_abc123"
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_renaming_an_object_keeps_names_with_spaces_whole(tmp_path):
    """Переименование объекта — единственное действие, которое нельзя сделать
    кнопкой: новое имя надо ввести.

    Разделитель — стрелка, а не пробел, и это не украшение: имена в этом архиве
    состоят из пробелов целиком («Иванов Иван Иванович», «в/ч 12345»), и по
    пробелу их разделить нельзя в принципе.

    Мутация: разделять аргумент по первому пробелу — тест обязан покраснеть.
    """
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/entity-profile": {
                "entity": {"id": "ent_abc123", "name": "Иванов И.И."},
                "profile": {},
                "edits": {"versions": 1, "restorable_version": None},
            },
            "/api/kg/entities/ent_abc123": {"entity": {"id": "ent_abc123", "name": "Иванов Иван Иванович"}},
        }
    )
    update = {
        "update_id": 950,
        "message": {
            "message_id": 12,
            "chat": {"id": 5001},
            "from": {"id": 5001},
            "text": "/entity_rename Иванов И.И. => Иванов Иван Иванович",
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
    finally:
        bridge._inbox.close()

    patches = [call for call in backend.calls if call["method"] == "PATCH"]
    assert patches, "переименование не дошло до backend"
    assert patches[0]["path"] == "/api/kg/entities/ent_abc123"
    assert patches[0]["body"]["name"] == "Иванов Иван Иванович", "новое имя обрезано по пробелу"
    lookups = [call for call in backend.calls if "entity-profile" in call["path"]]
    assert lookups and "%D0%98" in lookups[0]["path"], "старое имя искалось не целиком"


@pytest.mark.asyncio
async def test_rename_without_the_arrow_explains_the_format(tmp_path):
    """Без стрелки команда не гадает, где кончается старое имя, — она объясняет."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({})
    update = {
        "update_id": 951,
        "message": {
            "message_id": 13,
            "chat": {"id": 5001},
            "from": {"id": 5001},
            "text": "/entity_rename Иванов Иван",
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
    finally:
        bridge._inbox.close()

    assert not [call for call in backend.calls if call["method"] == "PATCH"], "переименовало наугад"
    sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
    # Смотрим на то, что УВИДИТ человек: с `parse_mode=HTML` стрелка уходит по
    # проводу как `=&gt;` и отрисовывается как `=>`. Проверять сырой запрос —
    # значит проверять транспорт вместо смысла.
    import html as _html

    assert sends and "=>" in _html.unescape(str(sends[-1]["text"])), "формат не объяснён"


@pytest.mark.asyncio
async def test_an_alias_helps_search_without_merging_anything(tmp_path):
    """Псевдоним — безопасная альтернатива слиянию, и на этом корпусе она важнее.

    «Иванов И.И.» и «Иванов Иван Иванович» могут быть одним человеком, а могут и
    разными; цена ошибки несимметрична — два дубликата это неудобство, а два
    разных человека в одном узле порча данных, откатить которую нечем без
    unmerge. Псевдоним чинит поиск, ничего не соединяя.

    Мутация: слать `PATCH` с полем `name` вместо `aliases` (то есть переименовать
    вместо добавления псевдонима) — тест обязан покраснеть.
    """
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/entity-profile": {
                "entity": {
                    "id": "ent_abc123",
                    "name": "Иванов Иван Иванович",
                    "aliases_json": '["Иванов И.И."]',
                },
                "profile": {},
                "edits": {"versions": 1, "restorable_version": None},
            },
            "/api/kg/entities/ent_abc123": {"entity": {"id": "ent_abc123", "name": "Иванов Иван Иванович"}},
        }
    )
    update = {
        "update_id": 960,
        "message": {
            "message_id": 20,
            "chat": {"id": 5001},
            "from": {"id": 5001},
            "text": "/entity_alias Иванов Иван Иванович => Иван Иванов",
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
    finally:
        bridge._inbox.close()

    patches = [call for call in backend.calls if call["method"] == "PATCH"]
    assert patches, "псевдоним не дошёл до backend"
    body = patches[0]["body"]
    assert "name" not in body, "команда переименовала объект вместо добавления псевдонима"
    assert body["aliases"] == ["Иванов И.И.", "Иван Иванов"], "прежние псевдонимы потеряны"


@pytest.mark.asyncio
async def test_a_duplicate_alias_is_not_added_twice(tmp_path):
    """Повтор — не ошибка и не второй псевдоним: человек просто узнаёт, что он уже есть."""
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/entity-profile": {
                "entity": {"id": "ent_abc123", "name": "Атлас", "aliases_json": '["Атлант"]'},
                "profile": {},
                "edits": {"versions": 1, "restorable_version": None},
            }
        }
    )
    update = {
        "update_id": 961,
        "message": {
            "message_id": 21,
            "chat": {"id": 5001},
            "from": {"id": 5001},
            "text": "/entity_alias Атлас => Атлант",
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
    finally:
        bridge._inbox.close()

    assert not [call for call in backend.calls if call["method"] == "PATCH"], "псевдоним добавлен дважды"
    sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
    assert sends and "уже есть" in str(sends[-1]["text"])


@pytest.mark.asyncio
async def test_an_alias_that_is_another_object_is_refused_not_silently_merged(tmp_path):
    """«Псевдоним», который уже является ИМЕНЕМ другого узла, — заявка на слияние.

    Команда обещает «ничего не сливает», и обещание надо держать: на корпусе из
    4349 автоизвлечённых ФИО «Иванов И.И.» вполне может быть отдельным человеком,
    а два разных человека под одним узлом — порча данных, которую нечем откатить,
    кроме unmerge. Слияние делается отдельным решением в /merges.

    Мутация: убрать проверку столкновения — тест обязан покраснеть.
    """
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            f"/api/kg/entity-profile?name={quote('Иванов Иван Иванович', safe='')}": {
                "entity": {"id": "ent_main", "name": "Иванов Иван Иванович", "aliases_json": "[]"},
                "profile": {},
                "edits": {"versions": 1, "restorable_version": None},
            },
            # Псевдоним резолвится в ДРУГОЙ объект — это заявка на слияние.
            f"/api/kg/entity-profile?name={quote('Иванов И.И.', safe='')}": {
                "entity": {"id": "ent_other", "name": "Иванов И.И.", "aliases_json": "[]"},
                "profile": {},
                "edits": {"versions": 1, "restorable_version": None},
            },
        }
    )
    update = {
        "update_id": 962,
        "message": {
            "message_id": 22,
            "chat": {"id": 5001},
            "from": {"id": 5001},
            "text": "/entity_alias Иванов Иван Иванович => Иванов И.И.",
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
    finally:
        bridge._inbox.close()

    assert not [call for call in backend.calls if call["method"] == "PATCH"], (
        "псевдоним, совпадающий с именем другого объекта, применён — это скрытое слияние"
    )
    sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
    assert sends and "/merges" in str(sends[-1]["text"]), "человеку не сказано, где сливают по-настоящему"


@pytest.mark.asyncio
async def test_a_differently_spelled_duplicate_alias_is_recognised(tmp_path):
    """«Иванов И.И.» и «Иванов И. И.» — одно написание для всех потребителей.

    Точное сравнение строк добавило бы второй псевдоним, и человек получил бы
    «готово» вместо «уже есть».

    Мутация: сравнивать строки точно — тест обязан покраснеть.
    """
    bridge = _media_bridge(tmp_path)
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient(
        {
            "/api/kg/entity-profile": {
                "entity": {
                    "id": "ent_abc123",
                    "name": "Иванов Иван Иванович",
                    "aliases_json": '["Иванов И.И."]',
                },
                "profile": {},
                "edits": {"versions": 1, "restorable_version": None},
            }
        }
    )
    update = {
        "update_id": 963,
        "message": {
            "message_id": 23,
            "chat": {"id": 5001},
            "from": {"id": 5001},
            "text": "/entity_alias Иванов Иван Иванович => Иванов И. И.",
        },
    }
    try:
        await bridge._process_update(telegram, backend, update, cached_response=None)
    finally:
        bridge._inbox.close()

    assert not [call for call in backend.calls if call["method"] == "PATCH"], "добавлен дубль псевдонима"
    sends = [payload for url, payload in telegram.calls if url.endswith("/sendMessage")]
    assert sends and "уже есть" in str(sends[-1]["text"])
