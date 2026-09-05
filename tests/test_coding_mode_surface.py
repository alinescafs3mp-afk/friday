"""Coding Mode surface: owner-only /coding, static inspect, no legacy dump."""

from __future__ import annotations

import base64
import io
import zipfile
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from friday.orchestration.coding_inspect_report import CodingInspectReportState
from friday.orchestration.router import OrchestrationRouter
from friday.organs.coding.static_turn import handle_coding_static_turn
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext
from friday.storage import normalize_conversation_mode
from tests.test_api_vertical_slice import _bridge_json, _bridge_request
from tests.test_interaction_failure_routes import _Planner


def _owner_actor() -> ActorContext:
    return ActorContext(
        LEGACY_OWNER_USER_ID,
        "owner",
        "telegram-bridge",
        identity_id="5001",
        telegram_chat_id="5001",
    )


def _guest_actor() -> ActorContext:
    return ActorContext(
        "bob",
        "user",
        "telegram-bridge",
        identity_id="5002",
        telegram_chat_id="5002",
    )


class _LegacySpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.storage = None

    async def chat(self, user_id: str, message: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((user_id, message, kwargs))
        return {"message": "legacy", "conversation_id": "conv_0000000000000001"}


def test_normalize_conversation_mode_accepts_coding() -> None:
    assert normalize_conversation_mode("coding") == "coding"
    with pytest.raises(ValueError, match="coding"):
        normalize_conversation_mode("unsafe-autonomy")


def test_static_inspect_from_members_is_inspected() -> None:
    result = handle_coding_static_turn(
        storage=None,
        user_id=LEGACY_OWNER_USER_ID,
        actor=_owner_actor(),
        message="осмотри main.py",
        conversation_id=None,
        attachments=[{"filename": "main.py", "size": 12}],
        enable_tools=True,
    )
    assert result["context"]["interaction_mode"] == "coding"
    assert result["context"]["coding_inspect_report"] == CodingInspectReportState.INSPECTED.value
    assert result["context"]["coding_execution_attempted"] is False
    assert result["context"]["coding_member_count"] == 1
    assert "статический осмотр завершён" in result["message"].casefold()
    assert "не допущен" in result["message"].casefold()
    assert "legacy" not in result["message"].casefold()


def test_empty_coding_turn_is_empty() -> None:
    result = handle_coding_static_turn(
        storage=None,
        user_id=LEGACY_OWNER_USER_ID,
        actor=_owner_actor(),
        message="что умеешь",
        conversation_id=None,
        attachments=[],
    )
    assert result["context"]["coding_inspect_report"] == CodingInspectReportState.EMPTY.value
    assert result["context"]["coding_member_count"] == 0
    assert "нет исходников" in result["message"]


def test_blocked_inspect_does_not_expose_names() -> None:
    result = handle_coding_static_turn(
        storage=None,
        user_id=LEGACY_OWNER_USER_ID,
        actor=_owner_actor(),
        message="осмотри",
        conversation_id=None,
        attachments=[{"filename": "../escape.py", "size": 4}],
    )
    assert result["context"]["coding_inspect_report"] == CodingInspectReportState.BLOCKED.value
    assert "coding_member_count" not in result["context"]
    assert "../escape.py" not in result["message"]
    assert "escape" not in result["message"]


def test_execute_claim_is_refused_and_does_not_execute(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from friday.organs.coding.worker_boundary import default_coding_worker_boundary

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("coding turn spawned a process")

    spawned: list[tuple[str, ...]] = []

    def _runner(argv: tuple[str, ...], timeout_sec: int) -> int:
        spawned.append(argv)
        del timeout_sec
        return 0

    monkeypatch.setattr("subprocess.Popen", _boom, raising=False)
    monkeypatch.setattr("subprocess.run", _boom, raising=False)
    boundary = default_coding_worker_boundary(
        friday_home=str(tmp_path / "friday-home"),
        owner_home=str(tmp_path / "owner"),
        database_path=str(tmp_path / "friday-home" / "data" / "state"),
        worker_root=str(tmp_path / "friday-coding-worker"),
    )
    result = handle_coding_static_turn(
        storage=None,
        user_id=LEGACY_OWNER_USER_ID,
        actor=_owner_actor(),
        message="запусти pytest",
        conversation_id=None,
        attachments=[{"filename": "test_app.py", "size": 8, "content_b64": "cHJpbnQoMSk="}],
        worker_boundary=boundary,
        spawn_runner=_runner,
    )
    assert result["context"]["coding_execution_attempted"] is False
    assert result["context"]["coding_worker_admission"] == "admitted"
    assert result["context"]["coding_worker_spawned"] is True
    assert result["context"]["coding_worker_probe"] == "confirmed"
    assert "запрос на выполнение отклонён" in result["message"].casefold()
    assert result["context"]["coding_inspect_report"] == CodingInspectReportState.INSPECTED.value
    assert len(spawned) == 1
    assert spawned[0][0] == "/usr/bin/bwrap"
    assert "--unshare-all" in spawned[0]


def _zip_attachment(members: dict[str, bytes]) -> dict[str, Any]:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    raw = buffer.getvalue()
    return {
        "filename": "app.zip",
        "size": len(raw),
        "content_b64": base64.standard_b64encode(raw).decode("ascii"),
    }


def test_execute_claim_extracts_admitted_archive_without_running_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from friday.organs.coding.worker_boundary import default_coding_worker_boundary

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("coding turn spawned a host process")

    spawned: list[tuple[str, ...]] = []

    def _runner(argv: tuple[str, ...], timeout_sec: int) -> int:
        spawned.append(argv)
        del timeout_sec
        return 0

    monkeypatch.setattr("subprocess.Popen", _boom, raising=False)
    monkeypatch.setattr("subprocess.run", _boom, raising=False)
    worker_root = tmp_path / "friday-coding-worker"
    boundary = default_coding_worker_boundary(
        friday_home=str(tmp_path / "friday-home"),
        owner_home=str(tmp_path / "owner"),
        database_path=str(tmp_path / "friday-home" / "data" / "state"),
        worker_root=str(worker_root),
    )
    result = handle_coding_static_turn(
        storage=None,
        user_id=LEGACY_OWNER_USER_ID,
        actor=_owner_actor(),
        message="запусти pytest",
        conversation_id=None,
        attachments=[_zip_attachment({"src/main.py": b"print(1)\n"})],
        worker_boundary=boundary,
        spawn_runner=_runner,
    )
    extracted = list(worker_root.glob("work/*/src/main.py"))
    assert result["context"]["coding_execution_attempted"] is False
    assert result["context"]["coding_archive_extract"] == "extracted"
    assert result["context"]["coding_archive_extracted_count"] == 1
    assert result["context"]["coding_worker_spawned"] is True
    assert "архив распакован" in result["message"].casefold()
    assert "не допущен" in result["message"].casefold()
    assert len(extracted) == 1
    assert extracted[0].read_bytes() == b"print(1)\n"
    assert len(spawned) == 1
    assert spawned[0][0] == "/usr/bin/bwrap"


def test_inspect_turn_does_not_extract_archive(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from friday.organs.coding.worker_boundary import default_coding_worker_boundary

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("inspect turn spawned a process")

    monkeypatch.setattr("subprocess.Popen", _boom, raising=False)
    monkeypatch.setattr("subprocess.run", _boom, raising=False)
    worker_root = tmp_path / "friday-coding-worker"
    boundary = default_coding_worker_boundary(
        friday_home=str(tmp_path / "friday-home"),
        owner_home=str(tmp_path / "owner"),
        database_path=str(tmp_path / "friday-home" / "data" / "state"),
        worker_root=str(worker_root),
    )
    result = handle_coding_static_turn(
        storage=None,
        user_id=LEGACY_OWNER_USER_ID,
        actor=_owner_actor(),
        message="осмотри app.zip",
        conversation_id=None,
        attachments=[_zip_attachment({"src/main.py": b"print(1)\n"})],
        worker_boundary=boundary,
        spawn_runner=_boom,
    )
    assert result["context"]["coding_archive_extract"] == "empty"
    assert result["context"]["coding_archive_extracted_count"] == 0
    assert result["context"]["coding_worker_spawned"] is False
    assert result["context"]["coding_execution_attempted"] is False
    assert list(worker_root.glob("work/*/src/main.py")) == []


def test_zip_slip_execute_claim_does_not_write_outside_workspace(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from friday.organs.coding.worker_boundary import default_coding_worker_boundary

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../escape.txt", b"nope")
    raw = buffer.getvalue()

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("coding turn spawned a host process")

    def _runner(argv: tuple[str, ...], timeout_sec: int) -> int:
        del argv, timeout_sec
        return 0

    monkeypatch.setattr("subprocess.Popen", _boom, raising=False)
    monkeypatch.setattr("subprocess.run", _boom, raising=False)
    worker_root = tmp_path / "friday-coding-worker"
    boundary = default_coding_worker_boundary(
        friday_home=str(tmp_path / "friday-home"),
        owner_home=str(tmp_path / "owner"),
        database_path=str(tmp_path / "friday-home" / "data" / "state"),
        worker_root=str(worker_root),
    )
    result = handle_coding_static_turn(
        storage=None,
        user_id=LEGACY_OWNER_USER_ID,
        actor=_owner_actor(),
        message="запусти pytest",
        conversation_id=None,
        attachments=[
            {
                "filename": "evil.zip",
                "size": len(raw),
                "content_b64": base64.standard_b64encode(raw).decode("ascii"),
            }
        ],
        worker_boundary=boundary,
        spawn_runner=_runner,
    )
    assert result["context"]["coding_archive_extract"] == "blocked"
    assert result["context"]["coding_execution_attempted"] is False
    assert "распаковка архива не допущена" in result["message"].casefold()
    assert not (tmp_path / "escape.txt").exists()
    assert not (worker_root / "escape.txt").exists()


def test_inspect_turn_composes_admission_without_spawn(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from friday.organs.coding.worker_boundary import default_coding_worker_boundary

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("inspect turn spawned a process")

    monkeypatch.setattr("subprocess.Popen", _boom, raising=False)
    monkeypatch.setattr("subprocess.run", _boom, raising=False)
    boundary = default_coding_worker_boundary(
        friday_home=str(tmp_path / "friday-home"),
        owner_home=str(tmp_path / "owner"),
        database_path=str(tmp_path / "friday-home" / "data" / "state"),
        worker_root=str(tmp_path / "friday-coding-worker"),
    )
    result = handle_coding_static_turn(
        storage=None,
        user_id=LEGACY_OWNER_USER_ID,
        actor=_owner_actor(),
        message="осмотри main.py",
        conversation_id=None,
        attachments=[{"filename": "main.py", "size": 12}],
        worker_boundary=boundary,
        spawn_runner=_boom,
    )
    assert result["context"]["coding_worker_admission"] == "admitted"
    assert result["context"]["coding_worker_spawned"] is False
    assert result["context"]["coding_worker_probe"] == "skipped"
    assert result["context"]["coding_execution_attempted"] is False


def test_guest_cannot_enter_coding_turn() -> None:
    from friday.permissions import AuthorizationError

    with pytest.raises(AuthorizationError):
        handle_coding_static_turn(
            storage=None,
            user_id="bob",
            actor=_guest_actor(),
            message="осмотри",
            conversation_id=None,
            attachments=[],
        )


@pytest.mark.asyncio
async def test_router_does_not_call_legacy_for_coding() -> None:
    legacy = _LegacySpy()
    router = OrchestrationRouter(legacy, _Planner(), mode="legacy")
    result = await router.chat(
        LEGACY_OWNER_USER_ID,
        "осмотри main.py",
        actor=_owner_actor(),
        conversation_id=None,
        attachments=[{"filename": "main.py", "size": 4}],
        enable_tools=True,
        mode="coding",
    )
    assert legacy.calls == []
    assert result["context"]["interaction_mode"] == "coding"


def test_owner_private_telegram_can_select_coding(settings) -> None:
    from friday.server import create_app

    configured = replace(settings, telegram_owner_chat_ids=[5001], engineer_mode_enabled=False)
    app = create_app(configured)
    with TestClient(app) as client:
        selected = _bridge_json(
            client,
            configured,
            "POST",
            "/api/conversations/channel/mode",
            {
                "channel": "telegram",
                "channel_id": "5001",
                "mode": "coding",
                "telegram_user": {"id": 5001, "first_name": "Owner"},
            },
            user="5001",
            chat="5001",
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["mode"] == "coding"


def test_api_owner_cannot_select_coding_outside_private_telegram(settings) -> None:
    from friday.server import create_app

    app = create_app(replace(settings, telegram_owner_chat_ids=[5001]))
    owner_headers = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        denied = client.post(
            "/api/conversations/channel/mode",
            headers=owner_headers,
            json={"channel": "api", "channel_id": "owner-desk", "mode": "coding"},
        )
        assert denied.status_code == 403, denied.text


def test_guest_cannot_select_coding_on_chat(settings, monkeypatch) -> None:
    from friday.server import create_app

    scoped = replace(settings, telegram_allowed_chat_ids=[5001], telegram_owner_chat_ids=[])
    app = create_app(scoped)
    with TestClient(app) as client:
        chat = AsyncMock(wraps=app.state.agent.chat)
        monkeypatch.setattr(app.state.agent, "chat", chat)
        response = _bridge_request(
            client,
            scoped,
            "/api/chat",
            {
                "message": "проверка доступа к coding",
                "mode": "coding",
                "enable_tools": True,
                "source_ref": "telegram-update:coding-direct-denied",
            },
            user="5001",
            chat="5001",
        )
        assert response.status_code == 403, response.text
        assert chat.await_count == 0


def test_persisted_coding_mode_is_forwarded_with_tools_off(settings, monkeypatch) -> None:
    from friday.server import create_app

    configured = replace(
        settings,
        telegram_owner_chat_ids=[5001],
        engineer_mode_enabled=False,
        verify_answers=False,
    )
    app = create_app(configured)
    with TestClient(app) as client:
        conversation = app.state.storage.create_conversation(
            LEGACY_OWNER_USER_ID,
            title="persisted coding contract",
            mode="coding",
        )
        captured: dict[str, Any] = {}

        async def capture(_user_id, _message, **kwargs):  # noqa: ANN001
            captured.update(kwargs)
            return {
                "conversation_id": conversation["id"],
                "message": "статический осмотр",
                "context": {"interaction_mode": "coding"},
            }

        chat = AsyncMock(side_effect=capture)
        monkeypatch.setattr(app.state.agent, "chat", chat)
        response = _bridge_request(
            client,
            configured,
            "/api/chat",
            {
                "conversation_id": conversation["id"],
                "message": "продолжи осмотр",
                "enable_tools": True,
                "source_ref": "telegram-update:92002",
                "telegram_user": {"id": 5001, "first_name": "Owner"},
            },
        )

    assert response.status_code == 200, response.text
    assert chat.await_count == 1
    assert captured.get("mode") == "coding"
    assert captured.get("enable_tools") is False


def test_coding_turn_persists_inspect_without_legacy(settings) -> None:
    from friday.server import create_app

    configured = replace(
        settings,
        telegram_owner_chat_ids=[5001],
        engineer_mode_enabled=False,
        verify_answers=False,
        llm_enabled=False,
    )
    app = create_app(configured)
    with TestClient(app) as client:
        selected = _bridge_json(
            client,
            configured,
            "POST",
            "/api/conversations/channel/mode",
            {
                "channel": "telegram",
                "channel_id": "5001",
                "mode": "coding",
                "telegram_user": {"id": 5001, "first_name": "Owner"},
            },
            user="5001",
            chat="5001",
        )
        assert selected.status_code == 200, selected.text
        session = selected.json().get("session") or {}
        conversation_id = str(session.get("conversation_id") or "")
        assert conversation_id
        response = _bridge_request(
            client,
            configured,
            "/api/chat",
            {
                "conversation_id": conversation_id,
                "mode": "coding",
                "message": "осмотри",
                "attachments": [{"filename": "app.py", "size": 24}],
                "enable_tools": True,
                "source_ref": "telegram-update:coding-inspect-live",
                "telegram_user": {"id": 5001, "first_name": "Owner"},
            },
            user="5001",
            chat="5001",
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["context"]["interaction_mode"] == "coding"
        assert body["context"]["coding_inspect_report"] == "inspected"
        assert body["context"]["coding_execution_attempted"] is False
        assert "не допущен" in body["message"].casefold()


@pytest.mark.asyncio
async def test_telegram_coding_command_sets_the_mode(tmp_path) -> None:
    from friday.telegram_bridge import TelegramBridge, TelegramConfig
    from tests.test_telegram_and_profile import _FakeBackendClient, _FakeTelegramClient

    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
            engineer_mode_enabled=False,
        )
    )
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/conversations/channel/mode": {"mode": "coding"}})
    user = {"id": 5001, "first_name": "Owner"}
    await bridge._process_update(
        telegram,
        backend,
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 5001},
                "from": user,
                "text": "/coding",
            },
        },
        cached_response=None,
    )
    mode_call = next(call for call in backend.calls if call["path"] == "/api/conversations/channel/mode")
    assert mode_call["body"]["mode"] == "coding"
    sent = [payload for url, payload in telegram.calls if str(url).endswith("/sendMessage")]
    assert any("Статический осмотр исходников" in str(item.get("text") or "") for item in sent)
    assert any("не допущен" in str(item.get("text") or "") for item in sent)


@pytest.mark.asyncio
async def test_telegram_coding_command_explains_owner_only_403(tmp_path) -> None:
    from friday.telegram_bridge import TelegramBridge, TelegramConfig
    from tests.test_telegram_and_profile import _FakeBackendClient, _FakeTelegramClient

    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )
    telegram = _FakeTelegramClient()
    backend = _FakeBackendClient({"/api/conversations/channel/mode": (403, "forbidden")})
    await bridge._process_update(
        telegram,
        backend,
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "chat": {"id": 5001},
                "from": {"id": 5001, "first_name": "Guest"},
                "text": "/coding",
            },
        },
        cached_response=None,
    )
    sent = [payload for url, payload in telegram.calls if str(url).endswith("/sendMessage")]
    assert any("только владельцу" in str(item.get("text") or "") for item in sent)
