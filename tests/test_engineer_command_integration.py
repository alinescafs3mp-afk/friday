from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.config import validate_settings
from friday.execution_kernel import ToolResult
from friday.organs.engineer.command import (
    CommandGrantAuthority,
    CommandProgress,
    CommandStatus,
    IsolationProfile,
    OwnerConfirmationAuthority,
    OwnerSourceAuthority,
)
from friday.organs.engineer.command.store import CommandJobStore
from friday.organs.engineer.command_tools import EngineerCommandService
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext
from friday.telegram_bridge import TelegramBridge, TelegramConfig


class _SchemaSpy:
    enabled = True
    total_budget_sec = 2.0

    def __init__(self) -> None:
        self.schemas: list[list[dict[str, object]]] = []

    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.schemas.append([dict(item) for item in (tools or [])])
        return {
            "content": "OK",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


class _StatusThenFinishModel(_SchemaSpy):
    async def chat(self, _messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.schemas.append([dict(item) for item in (tools or [])])
        if len(self.schemas) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "status-call",
                        "function": {
                            "name": "engineer_command_status",
                            "arguments": json.dumps({"job_id": "0" * 32}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
                "_queue_wait_sec": 0.0,
            }
        return {
            "content": "DONE",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


class _StatusKernel:
    def get_tool(self, _name: str) -> SimpleNamespace:
        return SimpleNamespace(risk="observe", timeout_sec=10.0)

    async def execute(self, name, arguments, *, actor):  # noqa: ANN001
        del arguments, actor
        return ToolResult(
            name,
            True,
            data={"ok": True, "status": "completed", "stdout": "private output"},
        )


def _command_schema(name: str) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test command tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _configured(settings, tmp_path: Path):
    store = tmp_path / "command-store"
    store.mkdir(mode=0o700)
    key = tmp_path / "command.key"
    key.write_bytes(b"K" * 32)
    key.chmod(0o600)
    return replace(
        settings,
        engineer_mode_enabled=True,
        engineer_command_enabled=True,
        engineer_command_store_dir=store,
        engineer_command_key_file=key,
    )


def test_command_configuration_is_private_and_explicit(settings, tmp_path: Path) -> None:
    missing = replace(
        settings,
        engineer_mode_enabled=True,
        engineer_command_enabled=True,
        engineer_command_store_dir=tmp_path / "missing-store",
        engineer_command_key_file=tmp_path / "missing-key",
    )
    assert any("engineer command store must be pre-created" in item for item in validate_settings(missing))
    assert any("engineer command key must be pre-created" in item for item in validate_settings(missing))

    configured = _configured(settings, tmp_path)
    assert not [item for item in validate_settings(configured) if item.startswith("engineer command")]


def test_command_tools_exist_only_when_the_private_kernel_is_configured(settings, tmp_path: Path) -> None:
    from friday.server import create_app

    disabled = create_app(replace(settings, engineer_mode_enabled=True))
    with TestClient(disabled):
        names = set(disabled.state.kernel.get_tool_names(ActorContext(LEGACY_OWNER_USER_ID, "owner", "api-token")))
        assert "engineer_command_run" not in names

    configured = create_app(_configured(settings, tmp_path))
    with TestClient(configured):
        names = set(
            configured.state.kernel.get_tool_names(
                ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
            )
        )
        assert {"engineer_command_run", "engineer_command_status", "engineer_command_cancel"} <= names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("telegram_update_id", "run_is_offered"),
    [("", False), ("123", True)],
)
async def test_command_start_schema_requires_authenticated_telegram_provenance(
    settings,
    storage,
    telegram_update_id: str,
    run_is_offered: bool,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _SchemaSpy()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
    )
    context = AgentContext(
        conversation_id="conv-command-schema",
        user_id=actor.user_id,
        person_id=actor.own_id,
        interaction_mode="engineer",
        effect_root_user_message_id="msg_0123456789abcdef",
        engineer_command_telegram_update_id=telegram_update_id,
        engineer_dossier={"targets": [], "hosts": [], "artifacts": []},
    )
    await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Запусти установленную программу.",
        actor,
        tools=[
            _command_schema("engineer_command_run"),
            _command_schema("engineer_command_status"),
            _command_schema("engineer_command_cancel"),
        ],
        attachments=None,
    )
    names = {
        str((schema.get("function") or {}).get("name") or "")
        for schema in model.schemas[0]
    }
    assert ("engineer_command_run" in names) is run_is_offered
    assert {"engineer_command_status", "engineer_command_cancel"} <= names


@pytest.mark.asyncio
async def test_command_output_closes_web_egress_for_followup_round(
    settings,
    storage,
    monkeypatch,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    storage.ensure_user(actor.own_id, preset_key="owner")
    model = _StatusThenFinishModel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        llm=model,  # type: ignore[arg-type]
        kernel=_StatusKernel(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)
    context = AgentContext(
        conversation_id="conv-command-private-result",
        user_id=actor.user_id,
        person_id=actor.own_id,
        interaction_mode="engineer",
        engineer_dossier={"targets": [], "hosts": [], "artifacts": []},
    )
    await runtime._agentic_loop(  # noqa: SLF001
        context,
        "Проверь статус команды и затем поищи это в интернете.",
        actor,
        tools=[
            _command_schema("engineer_command_status"),
            _command_schema("web_search"),
        ],
        attachments=None,
    )
    assert len(model.schemas) == 2
    second_names = {
        str((schema.get("function") or {}).get("name") or "")
        for schema in model.schemas[1]
    }
    assert "engineer_command_status" in second_names
    assert "web_search" not in second_names


@pytest.mark.parametrize("tool_name", ["engineer_command_status", "engineer_command_cancel"])
def test_command_management_refusals_are_kernel_failures(
    settings,
    tmp_path: Path,
    tool_name: str,
) -> None:
    from friday.server import create_app

    app = create_app(_configured(settings, tmp_path))
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    with TestClient(app):
        result = asyncio.run(
            app.state.kernel.execute(
                tool_name,
                {"job_id": "0" * 32},
                actor=actor,
                execution_scope="dialogue",
            )
        )
    assert result.success is False
    assert result.data is not None
    assert result.data["status"] == "failed"
    assert re.fullmatch(r"[a-z][a-z0-9_]{0,79}", str(result.data["error_code"]))


def test_generic_api_cannot_confirm_an_engineer_command(settings, tmp_path: Path) -> None:
    from friday.server import create_app

    configured = _configured(settings, tmp_path)
    app = create_app(configured)
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "api-token")
    arguments = {
        "argv": ["/usr/bin/true"],
        "timeout_sec": 10,
        "_conversation_id": "conv_test",
        "_source_message_id": "msg_0123456789abcdef",
        "_telegram_update_id": "100",
    }
    with TestClient(app) as client:
        requested = asyncio.run(
            app.state.kernel.execute(
                "engineer_command_run",
                arguments,
                actor=actor,
                execution_scope="dialogue",
            )
        )
        assert requested.success is False
        assert requested.data is not None, requested.error
        approval_id = str(requested.data["approval_id"])
        response = client.post(
            f"/api/approvals/{approval_id}/decide",
            headers={"Authorization": f"Bearer {configured.api_token}"},
            json={"decision": "approve", "telegram_update_id": 101},
        )
        assert response.status_code == 400
        assert app.state.storage.get_action_approval(approval_id, actor.user_id)["status"] == "pending"


class _IngressStorage:
    def __init__(self, actor: ActorContext, command_payload: dict[str, object]) -> None:
        self.actor = actor
        self.command_payload = command_payload

    def get_message(self, message_id: str, person_id: str):
        assert person_id == self.actor.own_id
        return {
            "id": message_id,
            "conversation_id": "conv_owner",
            "role": "user",
            "content": "Запусти true",
            "metadata_json": json.dumps({"telegram_update_id": "100"}),
        }

    def get_action_approval(self, approval_id: str, user_id: str, *, person_id: str = ""):
        assert user_id == self.actor.user_id
        assert person_id == self.actor.own_id
        return {
            "id": approval_id,
            "tool": "engineer_command_run",
            "status": "claimed",
            "requested_by": self.actor.own_id,
            "payload": self.command_payload,
        }


class _FakeCommandKernel:
    def __init__(self, root: Path) -> None:
        source = OwnerSourceAuthority(b"S" * 32)
        confirmation = OwnerConfirmationAuthority(b"C" * 32)
        self.authority = CommandGrantAuthority(b"G" * 32, source, confirmation)
        self.authority.bind_store(CommandJobStore(root))
        self.parsed = None

    def submit(self, request, grant: str, *, actor_id: str) -> str:
        self.parsed = self.authority.parse(grant, request, actor_id=actor_id)
        return "1" * 32

    def progress(self, job_id: str, *, actor_id: str) -> CommandProgress:
        assert job_id == "1" * 32
        assert actor_id == LEGACY_OWNER_USER_ID
        return CommandProgress(
            job_id=job_id,
            status=CommandStatus.RUNNING,
            elapsed_sec=0.01,
            stdout_bytes=0,
            stderr_bytes=0,
            output_activity=False,
            isolation_profile=IsolationProfile.ISOLATED_WORKSPACE,
        )


def test_distinct_authenticated_callback_mints_one_bound_grant(tmp_path: Path) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    payload: dict[str, object] = {
        "argv": ["/usr/bin/true"],
        "timeout_sec": 10,
        "_conversation_id": "conv_owner",
        "_source_message_id": "msg_0123456789abcdef",
        "_telegram_update_id": "100",
    }
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.storage = _IngressStorage(actor, payload)
    service.kernel = _FakeCommandKernel(tmp_path / "ledger")

    result = service.execute(
        actor=actor,
        argv=["/usr/bin/true"],
        timeout_sec=10,
        _conversation_id="conv_owner",
        _source_message_id="msg_0123456789abcdef",
        _telegram_update_id="100",
        _approval_id="apr_0123456789abcdef",
        _confirmation_update_id="101",
        _confirmation_body_hash=hashlib.sha256(b"signed callback body").hexdigest(),
    )

    assert result["ok"] is True
    assert result["job_id"] == "1" * 32
    assert service.kernel.parsed.destructive_confirmed is True
    assert service.kernel.parsed.telegram_update_id == "100"


def test_same_update_cannot_confirm_its_own_command(tmp_path: Path) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.storage = SimpleNamespace()
    service.kernel = SimpleNamespace()
    result = service.execute(
        actor=actor,
        argv=["/usr/bin/true"],
        timeout_sec=10,
        _conversation_id="conv_owner",
        _source_message_id="msg_0123456789abcdef",
        _telegram_update_id="100",
        _approval_id="apr_0123456789abcdef",
        _confirmation_update_id="100",
        _confirmation_body_hash=hashlib.sha256(b"same update").hexdigest(),
    )
    assert result == {
        "effect_boundary_crossed": False,
        "error_code": "authenticated_confirmation_required",
        "ok": False,
        "status": "failed",
    }


def test_source_row_must_bind_the_original_telegram_update() -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    payload: dict[str, object] = {
        "argv": ["/usr/bin/true"],
        "timeout_sec": 10,
        "_conversation_id": "conv_owner",
        "_source_message_id": "msg_0123456789abcdef",
        "_telegram_update_id": "999",
    }
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.storage = _IngressStorage(actor, payload)
    service.kernel = SimpleNamespace()
    result = service.execute(
        actor=actor,
        argv=["/usr/bin/true"],
        timeout_sec=10,
        _conversation_id="conv_owner",
        _source_message_id="msg_0123456789abcdef",
        _telegram_update_id="999",
        _approval_id="apr_0123456789abcdef",
        _confirmation_update_id="1000",
        _confirmation_body_hash=hashlib.sha256(b"different update").hexdigest(),
    )
    assert result["ok"] is False
    assert result["error_code"] == "owner_source_unavailable"


class _TelegramOK:
    async def post(self, url: str, **_kwargs):
        return httpx.Response(200, json={"ok": True, "result": {}}, request=httpx.Request("POST", url))


class _ApprovalBackend:
    def __init__(self) -> None:
        self.payload: dict[str, object] = {}

    async def request(self, method: str, url: str, **kwargs):
        self.payload = json.loads(bytes(kwargs.get("content") or b"{}"))
        return httpx.Response(
            200,
            json={"executed": False, "error": "test refusal"},
            request=httpx.Request(method, url),
        )


@pytest.mark.asyncio
async def test_bridge_binds_outer_update_to_approval_callback(tmp_path: Path) -> None:
    bridge = TelegramBridge(
        TelegramConfig(
            bot_token="123:token",
            bridge_secret="B" * 48,
            allowed_chat_ids=[5001],
            inbox_db_path=str(tmp_path / "telegram.sqlite3"),
        )
    )
    backend = _ApprovalBackend()
    callback = {
        "id": "callback-1",
        "from": {"id": 5001},
        "message": {"message_id": 91, "chat": {"id": 5001, "type": "private"}},
        "data": "apr:yes:apr_0123456789abcdef",
    }
    try:
        await bridge._process_callback_query(  # noqa: SLF001
            _TelegramOK(),
            backend,
            callback,
            update_id=777,
        )
    finally:
        bridge._inbox.close()  # noqa: SLF001
    assert backend.payload["decision"] == "approve"
    assert backend.payload["telegram_update_id"] == 777
