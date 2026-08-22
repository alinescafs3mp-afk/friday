from __future__ import annotations

import json
from typing import Any

import pytest

from friday.telegram_bridge import TelegramBridge, TelegramConfig
from friday.telegram_bridge._obsidian import obsidian_panel

FRIDAY_DEVICE_ID = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
SETUP_URL = "https://setup.friday.example/obsidian/session_opaque"


def test_panel_reports_the_exact_recent_operation_delivery_state() -> None:
    text, _markup = obsidian_panel(
        {
            "state": "ready",
            "message": "Vault подключён; Android сейчас офлайн.",
            "operations": [
                {
                    "operation_id": "offline-panel-op",
                    "method": "create",
                    "status": "delivery_pending",
                    "path": "Offline/Pending Delivery.md",
                    "server_scan_complete": True,
                    "android_connected": False,
                    "android_received": False,
                }
            ],
        }
    )

    assert "Offline/Pending Delivery.md" in text
    assert "delivery_pending" in text
    assert "server scan — готов" in text
    assert "Android — ожидается" in text
    assert "offline-panel-op" in text


def test_ready_panel_surfaces_open_conflict_and_preserved_artifact_paths() -> None:
    text, _markup = obsidian_panel(
        {
            "state": "ready",
            "message": "Vault подключён; Android сейчас на связи.",
            "conflict_count": 1,
            "conflicts": [
                {
                    "id": "obsconf_0123456789abcdef",
                    "canonical_path": "Projects/Friday Test.md",
                    "conflict_path": "Projects/Friday Test.sync-conflict-20260822.md",
                    "detected_at": "2026-08-22T06:42:00+00:00",
                }
            ],
        }
    )

    assert "Открытые конфликты: 1" in text
    assert "Projects/Friday Test.md" in text
    assert "Projects/Friday Test.sync-conflict-20260822.md" in text
    assert "не удаляются автоматически" in text


class _Response:
    def __init__(self, payload: dict[str, Any] | None = None, *, status_code: int = 200) -> None:
        self._payload = payload if payload is not None else {"ok": True, "result": {}}
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.text = json.dumps(self._payload, ensure_ascii=False)

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Telegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, json: dict[str, Any] | None = None, **_: Any) -> _Response:
        self.calls.append((url, dict(json or {})))
        return _Response()

    def messages(self) -> list[dict[str, Any]]:
        return [payload for url, payload in self.calls if url.endswith("/sendMessage")]


class _Backend:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> _Response:
        path = "/" + url.split("/", 3)[-1].split("?", 1)[0]
        self.calls.append(
            {
                "method": method,
                "path": path,
                "body": json.loads(content.decode("utf-8")) if content else None,
                "headers": dict(headers or {}),
            }
        )
        return _Response(self.response)


def _panel_response() -> dict[str, Any]:
    return {
        "message": "Добавьте Friday как удалённое устройство в Syncthing-Fork на Android.",
        "state": "multiple_pending_devices",
        "server_device_id": FRIDAY_DEVICE_ID,
        "setup_url": SETUP_URL,
        "candidates": [
            {
                "id": "cand_phone_7p2k",
                "display_name": "Pixel",
                "short_suffix": "…7P2K",
            }
        ],
        "actions": ["check", "confirm_open", "retry", "cancel"],
    }


def _bridge(tmp_path, *, allowed_chat_ids: list[int] | None = None) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=allowed_chat_ids or [5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
            obsidian_enabled=True,
        )
    )


def _disabled_bridge(tmp_path) -> TelegramBridge:
    return TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram-disabled.sqlite3"),
        )
    )


def _command_update(
    update_id: int,
    *,
    chat_id: int,
    user_id: int,
    chat_type: str,
    text: str = "/obsidian",
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "first_name": "Владелец"},
            "text": text,
        },
    }


def _callback(data: str, *, chat_id: int = 5001, user_id: int = 5001, chat_type: str = "private"):
    return {
        "id": f"cb-{data}-{user_id}",
        "from": {"id": user_id, "first_name": "Владелец"},
        "data": data,
        "message": {
            "message_id": 404,
            "chat": {"id": chat_id, "type": chat_type},
            "reply_markup": {"inline_keyboard": [[{"callback_data": data, "text": "Действие"}]]},
        },
    }


@pytest.mark.asyncio
async def test_obsidian_is_private_resumable_and_copy_text_is_the_exact_first_button(tmp_path) -> None:
    bridge = _bridge(tmp_path, allowed_chat_ids=[5001, -5001])
    telegram = _Telegram()
    backend = _Backend(_panel_response())
    try:
        await bridge._process_update(
            telegram,
            backend,
            _command_update(1, chat_id=-5001, user_id=5001, chat_type="group"),
            cached_response=None,
        )
        assert backend.calls == []
        assert "только в личной переписке" in telegram.messages()[-1]["text"]

        for update_id in (2, 3):
            await bridge._process_update(
                telegram,
                backend,
                _command_update(update_id, chat_id=5001, user_id=5001, chat_type="private"),
                cached_response=None,
            )

        starts = [call for call in backend.calls if call["path"] == "/api/obsidian/onboarding/start"]
        assert len(starts) == 2, "повторный /obsidian обязан возобновить durable session через start"
        assert all(call["method"] == "POST" and call["body"] is None for call in starts)
        assert all(call["headers"]["X-Friday-User"] == "5001" for call in starts)
        assert all(call["headers"]["X-Friday-Chat"] == "5001" for call in starts)
        assert all(call["headers"]["X-Friday-Signature"] for call in starts)

        panel = telegram.messages()[-1]
        keyboard = panel["reply_markup"]["inline_keyboard"]
        first_button = keyboard[0][0]
        assert first_button["copy_text"] == {"text": FRIDAY_DEVICE_ID}
        assert len(first_button["copy_text"]["text"]) <= 256
        assert FRIDAY_DEVICE_ID in panel["text"], "полный ID должен оставаться выделяемым в тексте"
        assert SETUP_URL in panel["text"], "HTTPS guide — обязательный fallback старому Telegram"
        assert any(row[0].get("url") == SETUP_URL for row in keyboard)

        callbacks = [
            button["callback_data"] for row in keyboard for button in row if "callback_data" in button
        ]
        assert set(callbacks) == {
            "obs:select:cand_phone_7p2k",
            "obs:check:current",
            "obs:opened:current",
            "obs:retry:current",
            "obs:cancel:current",
        }
        assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)
        assert all(FRIDAY_DEVICE_ID not in value for value in callbacks)
        assert "скопирован" not in panel["text"].casefold()
    finally:
        bridge._inbox.close()


@pytest.mark.parametrize(
    ("callback_data", "expected_path", "expected_body"),
    [
        ("obs:check:current", "/api/obsidian/onboarding/check", None),
        (
            "obs:select:cand_phone_7p2k",
            "/api/obsidian/onboarding/select-device",
            {"candidate_id": "cand_phone_7p2k"},
        ),
        ("obs:opened:current", "/api/obsidian/onboarding/confirm-open", None),
        ("obs:retry:current", "/api/obsidian/onboarding/retry", None),
        ("obs:cancel:current", "/api/obsidian/onboarding/cancel", None),
    ],
)
@pytest.mark.asyncio
async def test_obsidian_callbacks_are_opaque_bounded_and_owner_signed(
    tmp_path, callback_data: str, expected_path: str, expected_body: dict[str, Any] | None
) -> None:
    bridge = _bridge(tmp_path)
    telegram = _Telegram()
    backend = _Backend(_panel_response())
    try:
        assert len(callback_data.encode("utf-8")) <= 64
        assert FRIDAY_DEVICE_ID not in callback_data
        await bridge._process_callback_query(telegram, backend, _callback(callback_data))

        assert len(backend.calls) == 1
        call = backend.calls[0]
        assert (call["method"], call["path"], call["body"]) == ("POST", expected_path, expected_body)
        assert call["headers"]["X-Friday-User"] == "5001"
        assert call["headers"]["X-Friday-Chat"] == "5001"
        assert call["headers"]["X-Friday-Signature"]
    finally:
        bridge._inbox.close()


@pytest.mark.parametrize(
    "callback",
    [
        _callback("obs:cancel:current", user_id=6002),
        _callback("obs:cancel:current", chat_id=-5001, chat_type="group"),
    ],
)
@pytest.mark.asyncio
async def test_obsidian_callback_from_another_user_or_group_never_reaches_backend(tmp_path, callback) -> None:
    bridge = _bridge(tmp_path, allowed_chat_ids=[5001, -5001])
    telegram = _Telegram()
    backend = _Backend(_panel_response())
    try:
        await bridge._process_callback_query(telegram, backend, callback)
        assert backend.calls == []
        answers = [payload for url, payload in telegram.calls if url.endswith("/answerCallbackQuery")]
        assert answers[-1]["show_alert"] is True
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_disabled_obsidian_is_hidden_and_never_calls_the_backend(tmp_path) -> None:
    bridge = _disabled_bridge(tmp_path)
    telegram = _Telegram()
    backend = _Backend(_panel_response())
    try:
        await bridge._register_commands(telegram)
        menu = next(payload for url, payload in telegram.calls if url.endswith("/setMyCommands"))
        commands = {item["command"] for item in menu["commands"]}
        assert "obsidian" not in commands
        assert "obsidian_alias" not in commands

        await bridge._process_update(
            telegram,
            backend,
            _command_update(99, chat_id=5001, user_id=5001, chat_type="private"),
            cached_response=None,
        )
        assert backend.calls == []
        assert "не включена" in telegram.messages()[-1]["text"]
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_unicode_alias_command_is_private_owner_signed_and_exact(tmp_path) -> None:
    bridge = _bridge(tmp_path, allowed_chat_ids=[5001, -5001])
    telegram = _Telegram()
    backend = _Backend(_panel_response())
    try:
        group = _command_update(
            200,
            chat_id=-5001,
            user_id=5001,
            chat_type="group",
            text="/obsidian_alias Личный Vault",
        )
        await bridge._process_update(telegram, backend, group, cached_response=None)
        assert backend.calls == []

        private = _command_update(
            201,
            chat_id=5001,
            user_id=5001,
            chat_type="private",
            text="/obsidian_alias Личный Vault",
        )
        await bridge._process_update(telegram, backend, private, cached_response=None)
        call = backend.calls[-1]
        assert (call["method"], call["path"], call["body"]) == (
            "POST",
            "/api/obsidian/onboarding/vault-alias",
            {"alias": "Личный Vault"},
        )
        assert call["headers"]["X-Friday-User"] == "5001"
        assert "обновлено" in telegram.messages()[-2]["text"]
    finally:
        bridge._inbox.close()


@pytest.mark.asyncio
async def test_open_note_button_uses_https_launcher_not_unsupported_custom_scheme(tmp_path) -> None:
    bridge = _bridge(tmp_path)
    telegram = _Telegram()
    response = _panel_response()
    response.update(
        {
            "state": "awaiting_obsidian_vault_registration",
            "actions": ["open_test_note", "confirm_open"],
            "vault": {
                "android_alias": "Личный Vault",
                "open_url": (
                    "https://friday.example/obsidian/open#"
                    "vault=%D0%9B%D0%B8%D1%87%D0%BD%D1%8B%D0%B9+Vault&"
                    "file=Friday+Connection+Test.md"
                ),
            },
        }
    )
    backend = _Backend(response)
    try:
        await bridge._process_update(
            telegram,
            backend,
            _command_update(202, chat_id=5001, user_id=5001, chat_type="private"),
            cached_response=None,
        )
        keyboard = telegram.messages()[-1]["reply_markup"]["inline_keyboard"]
        open_button = next(
            row[0] for row in keyboard if row[0]["text"] == "Открыть тестовую заметку в Obsidian"
        )
        assert open_button["url"].startswith("https://friday.example/obsidian/open#")
        assert not open_button["url"].startswith("obsidian://")
        assert any(row[0]["text"] == "Тестовая заметка открылась" for row in keyboard)
        assert "/obsidian_alias точное имя" in telegram.messages()[-1]["text"]
    finally:
        bridge._inbox.close()
