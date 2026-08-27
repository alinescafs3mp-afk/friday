from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import zipfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from friday.agent_runtime import AgentContext, AgentRuntime
from friday.config import validate_settings
from friday.execution_kernel import ToolResult
from friday.file_delivery import (
    AuthorizedCurrentMessageUploadBatch,
    AuthorizedFileBytes,
    CurrentMessageUploadBatchIdentity,
    CurrentMessageUploadFileIdentity,
)
from friday.organs import ServiceContext
from friday.organs.engineer import ENGINEER_COMMAND_RUN, command_tools
from friday.organs.engineer.command import (
    CommandError,
    CommandLane,
    CommandOrigin,
    CommandProgress,
    CommandReceipt,
    CommandStatus,
    GeneratedFile,
    IsolationProfile,
)
from friday.organs.engineer.command.contracts import sha256_bytes
from friday.organs.engineer.command_tools import EngineerCommandService, build_engineer_command_tools
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


class _DependentHostCommandModel(_SchemaSpy):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.tool_messages: list[str] = []
        self.commands = (
            'printf first > "$FRIDAY_WORK_DIR/state.txt"',
            (
                "printf -- '-second' >> \"$FRIDAY_WORK_DIR/state.txt\"; "
                'cp "$FRIDAY_WORK_DIR/state.txt" "$FRIDAY_OUTPUT_DIR/final.txt"; '
                "printf vertical-ok"
            ),
        )

    async def chat(self, messages, *, tools=None, **_kwargs):  # noqa: ANN001
        self.calls += 1
        self.schemas.append([dict(item) for item in (tools or [])])
        self.tool_messages = [
            str(item.get("content") or "") for item in messages if item.get("role") == "tool"
        ]
        if self.calls <= len(self.commands):
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"dependent-host-step-{self.calls}",
                        "function": {
                            "name": "engineer_command_run",
                            "arguments": json.dumps({"command": self.commands[self.calls - 1]}),
                        },
                    }
                ],
                "finish_reason": "tool_calls",
                "_queue_wait_sec": 0.0,
            }
        return {
            "content": "DEPENDENT-HOST-DONE",
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


def test_autonomous_command_tool_has_no_hitl_and_keeps_runtime_authority_hidden() -> None:
    observed: dict[str, object] = {}

    def execute(**arguments):  # noqa: ANN003, ANN201
        observed.update(arguments)
        return {"ok": True}

    ctx = ServiceContext(
        settings=SimpleNamespace(engineer_command_enabled=True),
        storage=None,
        kg=None,
        ingestion=None,
    )
    fake_service = SimpleNamespace(execute=execute)
    tools = {item.name: item for item in build_engineer_command_tools(ctx, service=fake_service)}
    tool = tools["engineer_command_run"]

    assert ENGINEER_COMMAND_RUN.default_requires_hitl is False
    assert tool.risk == "mutate"
    assert tool.approval_predicate is None
    assert tool.parameters["required"] == ["command"]
    assert tool.parameters["additionalProperties"] is False
    properties = tool.parameters["properties"]
    assert set(properties) == {"command", "timeout_sec"}
    assert properties["timeout_sec"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 2_147_483_647,
    }
    assert "default" not in properties["timeout_sec"]
    assert "_step_id" not in properties
    assert "approval" not in str(tool.parameters).casefold()
    result = asyncio.run(
        tool.handler(
            actor="owner",
            command="printf autonomous",
            _conversation_id="conv_owner",
            _source_message_id="msg_0123456789abcdef",
            _telegram_update_id="100",
            _step_id="ecstep-" + "a" * 32,
        )
    )
    assert result == {"ok": True}
    assert observed["timeout_sec"] is None


def test_command_tools_exist_only_when_the_private_kernel_is_configured(settings, tmp_path: Path) -> None:
    from friday.server import create_app

    disabled = create_app(replace(settings, engineer_mode_enabled=True))
    with TestClient(disabled):
        names = set(
            disabled.state.kernel.get_tool_names(ActorContext(LEGACY_OWNER_USER_ID, "owner", "api-token"))
        )
        assert "engineer_command_run" not in names

    configured = create_app(_configured(settings, tmp_path))
    with TestClient(configured):
        names = set(
            configured.state.kernel.get_tool_names(
                ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
            )
        )
        assert {"engineer_command_run", "engineer_command_status", "engineer_command_cancel"} <= names


def test_real_owner_service_runs_dependent_host_shell_steps_without_approvals(
    settings,
    tmp_path: Path,
) -> None:
    """Exercise ExecutionKernel -> service -> HOST_USER runner on the native host."""

    from friday.server import create_app

    configured = replace(
        _configured(settings, tmp_path),
        telegram_allowed_chat_ids=[5001],
        telegram_owner_chat_ids=[5001],
    )
    app = create_app(configured)
    actor = ActorContext(
        LEGACY_OWNER_USER_ID,
        "owner",
        "telegram-bridge",
        identity_id="5001",
    )
    with TestClient(app):
        app.state.storage.ensure_user(
            actor.own_id,
            preset_key="owner",
            metadata={"chat_id": "5001"},
        )
        app.state.storage.link_identity("telegram", "5001", actor.own_id, linked_by=actor.own_id)
        conversation = app.state.storage.create_conversation(
            actor.user_id,
            title="autonomous host vertical",
            mode="engineer",
        )
        source = app.state.storage.store_message(
            conversation["id"],
            actor.user_id,
            "user",
            "Создай рабочий файл, измени его следующим шагом и верни результат.",
            metadata={
                "conversation_uploaded_raw_ids": [],
                "telegram_update_id": "7001",
            },
        )
        model = _DependentHostCommandModel()
        runtime = AgentRuntime(
            configured,
            app.state.storage,
            llm=model,  # type: ignore[arg-type]
            kernel=app.state.kernel,
        )
        context = AgentContext(
            conversation_id=conversation["id"],
            user_id=actor.user_id,
            person_id=actor.own_id,
            interaction_mode="engineer",
            source_search_lineage_user_message_id=source["id"],
            effect_root_user_message_id=source["id"],
            engineer_command_telegram_update_id="7001",
        )
        command_tools = [
            schema
            for schema in app.state.kernel.get_tool_definitions(actor, topic="")
            if str((schema.get("function") or {}).get("name") or "") == "engineer_command_run"
        ]
        response = asyncio.run(
            runtime._agentic_loop(  # noqa: SLF001
                context,
                "Создай рабочий файл, измени его следующим шагом и верни результат.",
                actor,
                command_tools,
                attachments=None,
            )
        )

        assert response["content"] == "DEPENDENT-HOST-DONE"
        assert response["tools_used"] == ["engineer_command_run", "engineer_command_run"]
        assert model.calls == 3
        assert len(model.tool_messages) == 2
        assert "vertical-ok" in model.tool_messages[-1]
        assert '"isolated": false' in model.tool_messages[-1]
        sealed_outputs = list(configured.engineer_command_store_dir.glob("jobs/*/sealed/final.txt"))
        assert len(sealed_outputs) == 1
        assert sealed_outputs[0].read_bytes() == b"first-second"
        assert (
            app.state.storage.count_action_approvals(
                actor.user_id,
                status="pending",
                person_id=actor.own_id,
            )
            == 0
        )


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
    names = {str((schema.get("function") or {}).get("name") or "") for schema in model.schemas[0]}
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
    second_names = {str((schema.get("function") or {}).get("name") or "") for schema in model.schemas[1]}
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


@pytest.mark.parametrize("tool_name", ["engineer_command_status", "engineer_command_cancel"])
def test_command_management_schema_resolves_the_current_conversation_job(
    settings,
    tmp_path: Path,
    tool_name: str,
) -> None:
    from friday.server import create_app

    app = create_app(_configured(settings, tmp_path))
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    with TestClient(app):
        tool = app.state.kernel.get_tool(tool_name)
        assert "required" not in tool.parameters
        result = asyncio.run(tool.handler(actor=actor, _conversation_id="conv-owner"))
    assert result["ok"] is False
    assert result["error_code"] == "current_job_not_found"


def test_generic_api_cannot_admit_an_engineer_command_or_create_an_approval(
    settings,
    tmp_path: Path,
) -> None:
    from friday.server import create_app

    configured = _configured(settings, tmp_path)
    app = create_app(configured)
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "api-token")
    arguments = {
        "command": "/usr/bin/true",
        "timeout_sec": 10,
        "_conversation_id": "conv_test",
        "_source_message_id": "msg_0123456789abcdef",
        "_telegram_update_id": "100",
        "_step_id": "ecstep-" + "1" * 32,
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
        assert requested.data["error_code"] == "authorization_denied"
        assert requested.data["approval_id"] == ""
        assert (
            app.state.storage.count_action_approvals(
                actor.user_id,
                status="pending",
                person_id=actor.own_id,
            )
            == 0
        )
        legacy = app.state.storage.create_action_approval(
            actor.user_id,
            tool="engineer_command_run",
            payload={"argv": ["/usr/bin/true"], "timeout_sec": 10},
            summary="legacy Engineer callback",
            risk="mutate",
            requested_by=actor.own_id,
        )

        direct = asyncio.run(app.state.kernel.execute_approved(legacy["id"], actor=actor))
        assert direct.success is False
        assert "прежнему контуру" in str(direct.error)
        assert app.state.storage.get_action_approval(legacy["id"], actor.user_id)["status"] == ("pending")

        response = client.post(
            f"/api/approvals/{legacy['id']}/decide",
            headers={"Authorization": f"Bearer {configured.api_token}"},
            json={"decision": "approve"},
        )
        assert response.status_code == 409
        assert app.state.storage.get_action_approval(legacy["id"], actor.user_id)["status"] == ("pending")


def test_startup_retires_only_legacy_engineer_approval_rows_and_pushes(storage) -> None:
    actor_id = LEGACY_OWNER_USER_ID
    storage.ensure_user(actor_id, preset_key="owner")
    legacy = storage.create_action_approval(
        actor_id,
        tool="engineer_command_run",
        payload={"argv": ["/usr/bin/true"], "timeout_sec": 10},
        requested_by=actor_id,
        risk="mutate",
    )
    unrelated = storage.create_action_approval(
        actor_id,
        tool="kg_merge",
        payload={"left": "a", "right": "b"},
        requested_by=actor_id,
        risk="high",
    )
    assert storage.enqueue_notification(
        actor_id,
        "5001",
        "legacy engineer approval",
        kind="approval",
        dedup_key=f"approval:{legacy['id']}",
    )
    assert storage.enqueue_notification(
        actor_id,
        "5001",
        "unrelated approval",
        kind="approval",
        dedup_key=f"approval:{unrelated['id']}",
    )
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.storage = storage

    service._retire_legacy_command_approvals()  # noqa: SLF001

    retired = storage.get_action_approval(legacy["id"], actor_id)
    retained = storage.get_action_approval(unrelated["id"], actor_id)
    assert retired["status"] == "rejected"
    assert retired["decided_by"] == "engineer-autonomous-migration"
    assert retained["status"] == "pending"
    notifications = {
        str(row["body"]): dict(row)
        for row in storage.execute(
            "SELECT body,status,kind,dedup_key FROM outbound_notifications ORDER BY body"
        ).fetchall()
    }
    assert notifications["legacy engineer approval"] == {
        "body": "legacy engineer approval",
        "dedup_key": "",
        "kind": "undeliverable:engineer_autonomous_no_approval",
        "status": "failed",
    }
    assert notifications["unrelated approval"] == {
        "body": "unrelated approval",
        "dedup_key": f"approval:{unrelated['id']}",
        "kind": "approval",
        "status": "pending",
    }


class _IngressStorage:
    def __init__(
        self,
        actor: ActorContext,
        *,
        source_update_id: str = "100",
        uploaded_raw_ids: tuple[str, ...] = (),
    ) -> None:
        self.actor = actor
        self.source_update_id = source_update_id
        self.uploaded_raw_ids = uploaded_raw_ids

    def get_message(self, message_id: str, person_id: str):
        assert person_id == self.actor.own_id
        return {
            "id": message_id,
            "conversation_id": "conv_owner",
            "role": "user",
            "content": "Запусти true",
            "metadata_json": json.dumps(
                {
                    "conversation_uploaded_raw_ids": list(self.uploaded_raw_ids),
                    "telegram_update_id": self.source_update_id,
                }
            ),
        }

    def resolve_identity(self, source: str, external_id: str) -> str | None:
        assert (source, external_id) == ("telegram", "5001")
        return self.actor.own_id

    def get_user(self, user_id: str):
        assert user_id == self.actor.own_id
        return {
            "id": user_id,
            "preset_key": "owner",
            "status": "active",
            "metadata_json": json.dumps({"chat_id": "5001"}),
        }


class _FreshAuthorization:
    def __init__(self, actor: ActorContext) -> None:
        self.actor = actor
        self.required: list[str] = []

    def actor_for_user(self, user_id: str, *, source: str, identity_id: str):
        assert user_id == self.actor.own_id
        assert source == "engineer-command-service"
        assert identity_id == "5001"
        return ActorContext(
            self.actor.user_id,
            "owner",
            source,
            identity_id=identity_id,
        )

    def require(self, actor: ActorContext, capability: str) -> None:
        assert actor.is_owner
        self.required.append(capability)


class _FakeSourceAuthority:
    def __init__(self) -> None:
        self.attestations: list[dict[str, object]] = []
        self.delegations: list[tuple[object, int]] = []

    def attest(self, **arguments):  # noqa: ANN003, ANN201
        self.attestations.append(dict(arguments))
        return SimpleNamespace(**arguments)

    def delegate_autonomous(self, source, *, expires_at: int):  # noqa: ANN001, ANN201
        self.delegations.append((source, expires_at))
        return "sealed-autonomous-delegation"


class _FakeCommandAuthority:
    def __init__(self) -> None:
        self.source_authority = _FakeSourceAuthority()
        self.issued: list[tuple[object, object, str, int]] = []

    def issue_autonomous(
        self,
        request,
        *,
        source,
        delegation: str,
        ttl_sec: int,
    ) -> str:  # noqa: ANN001
        self.issued.append((request, source, delegation, ttl_sec))
        return "sealed-autonomous-grant"


class _FakeCommandKernel:
    def __init__(self) -> None:
        self.authority = _FakeCommandAuthority()
        self.requests: list[object] = []
        self.delivery_chat_ids: list[str] = []

    def submit(self, request, grant: str, *, actor_id: str, delivery_chat_id: str = "") -> str:
        assert grant == "sealed-autonomous-grant"
        assert actor_id == LEGACY_OWNER_USER_ID
        assert delivery_chat_id == "5001"
        self.requests.append(request)
        self.delivery_chat_ids.append(delivery_chat_id)
        return "1" * 32

    def wait(
        self,
        job_id: str,
        *,
        actor_id: str,
        conversation_id: str,
        timeout_sec: float,
    ) -> None:
        assert (job_id, actor_id, conversation_id, timeout_sec) == (
            "1" * 32,
            LEGACY_OWNER_USER_ID,
            "conv_owner",
            15.0,
        )
        raise CommandError("wait_timeout")

    def progress(
        self,
        job_id: str,
        *,
        actor_id: str,
        conversation_id: str | None = None,
    ) -> CommandProgress:
        assert job_id == "1" * 32
        assert actor_id == LEGACY_OWNER_USER_ID
        assert conversation_id == "conv_owner"
        return CommandProgress(
            job_id=job_id,
            status=CommandStatus.RUNNING,
            elapsed_sec=0.01,
            stdout_bytes=0,
            stderr_bytes=0,
            output_activity=False,
            isolation_profile=IsolationProfile.HOST_USER,
        )


def _direct_service(actor: ActorContext, *, source_update_id: str = "100") -> EngineerCommandService:
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.storage = _IngressStorage(actor, source_update_id=source_update_id)
    service.kernel = _FakeCommandKernel()
    service.settings = SimpleNamespace(
        engineer_command_enabled=True,
        engineer_mode_enabled=True,
        telegram_effective_allowed_chat_ids={5001},
        telegram_open_registration=False,
    )
    service.authorization = _FreshAuthorization(actor)
    service.files_root = Path("/not-read-without-uploads")
    service.max_upload_bytes = 4 * 1024 * 1024
    return service


class _InputAwareKernel(_FakeCommandKernel):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events
        self.submitted_inputs: tuple[object, object, tuple[AuthorizedFileBytes, ...]] | None = None

    def submit(
        self,
        request,
        grant: str,
        *,
        actor_id: str,
        delivery_chat_id: str,
        input_manifest,
        input_batch_identity,
        input_files: tuple[AuthorizedFileBytes, ...],
    ) -> str:  # noqa: ANN001
        self.events.append("submit")
        self.submitted_inputs = (input_manifest, input_batch_identity, input_files)
        assert grant == "sealed-autonomous-grant"
        assert actor_id == LEGACY_OWNER_USER_ID
        assert delivery_chat_id == "5001"
        self.requests.append(request)
        self.delivery_chat_ids.append(delivery_chat_id)
        return "1" * 32


def test_exact_current_upload_is_reauthorized_and_passed_only_through_private_submit_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = ActorContext(
        LEGACY_OWNER_USER_ID,
        "owner",
        "telegram-bridge",
        identity_id="5001",
    )
    raw_id = "raw_0123456789abcdef"
    body = b"private current-message bytes"
    content_sha256 = sha256_bytes(body)
    file_identity = CurrentMessageUploadFileIdentity(
        raw_id=raw_id,
        source_identity_sha256="7" * 64,
        content_sha256=content_sha256,
        size_bytes=len(body),
        filename="input.bin",
        mime_type="application/octet-stream",
    )
    batch_identity = CurrentMessageUploadBatchIdentity(
        source_message_id="msg_0123456789abcdef",
        conversation_id="conv_owner",
        source_message_identity_sha256="8" * 64,
        telegram_update_id="100",
        uploaded_raw_ids=(raw_id,),
        files=(file_identity,),
    )
    authorized_file = AuthorizedFileBytes(
        raw_id=raw_id,
        filename="input.bin",
        mime_type="application/octet-stream",
        content=body,
    )
    first_batch = AuthorizedCurrentMessageUploadBatch(batch_identity, (authorized_file,))
    spawn_batch = AuthorizedCurrentMessageUploadBatch(batch_identity, (authorized_file,))
    events: list[str] = []
    request_calls: list[dict[str, object]] = []
    service = _direct_service(actor)
    service.storage = _IngressStorage(actor, uploaded_raw_ids=(raw_id,))
    kernel = _InputAwareKernel(events)
    service.kernel = kernel

    def authorize(*_args, **arguments):  # noqa: ANN002, ANN003, ANN201
        events.append("authorize")
        assert arguments == {
            "conversation_id": "conv_owner",
            "source_message_id": "msg_0123456789abcdef",
            "telegram_update_id": "100",
            "uploaded_raw_ids": (raw_id,),
            "max_bytes_per_file": command_tools.MAX_INPUT_FILE_BYTES,
        }
        return first_batch

    def reauthorize(*_args, **arguments):  # noqa: ANN002, ANN003, ANN201
        events.append("reauthorize")
        assert arguments["expected"] is batch_identity
        return spawn_batch

    def request_factory(**arguments):  # noqa: ANN003, ANN201
        request_calls.append(dict(arguments))
        return SimpleNamespace(
            digest=sha256_bytes(
                json.dumps(
                    {
                        "command": arguments["command"],
                        "manifest": arguments["input_manifest"].to_payload(),
                        "timeout_sec": arguments["timeout_sec"],
                    },
                    sort_keys=True,
                ).encode()
            ),
            idempotency_key=arguments["idempotency_key"],
            lane=CommandLane.SHELL,
            origin=CommandOrigin.MODEL,
            shell_command=arguments["command"],
            timeout_sec=arguments["timeout_sec"],
        )

    original_attest = kernel.authority.source_authority.attest
    original_delegate = kernel.authority.source_authority.delegate_autonomous
    original_issue = kernel.authority.issue_autonomous

    def attest(**arguments):  # noqa: ANN003, ANN201
        events.append("attest")
        return original_attest(**arguments)

    def delegate(source, *, expires_at: int):  # noqa: ANN001, ANN201
        events.append("delegate")
        return original_delegate(source, expires_at=expires_at)

    def issue(request, *, source, delegation: str, ttl_sec: int) -> str:  # noqa: ANN001
        events.append("issue")
        return original_issue(
            request,
            source=source,
            delegation=delegation,
            ttl_sec=ttl_sec,
        )

    monkeypatch.setattr(command_tools, "authorize_current_message_upload_batch", authorize)
    monkeypatch.setattr(command_tools, "reauthorize_current_message_upload_batch", reauthorize)
    monkeypatch.setattr(command_tools, "_command_request", request_factory)
    monkeypatch.setattr(kernel.authority.source_authority, "attest", attest)
    monkeypatch.setattr(kernel.authority.source_authority, "delegate_autonomous", delegate)
    monkeypatch.setattr(kernel.authority, "issue_autonomous", issue)

    result = service.execute(
        actor=actor,
        command='sha256sum "$FRIDAY_INPUT_DIR/01-input.bin"',
        timeout_sec=10,
        _conversation_id="conv_owner",
        _source_message_id="msg_0123456789abcdef",
        _telegram_update_id="100",
        _step_id="ecstep-" + "5" * 32,
    )

    assert result["ok"] is True
    assert events == ["authorize", "attest", "delegate", "issue", "reauthorize", "submit"]
    assert len(request_calls) == 2
    manifest = request_calls[-1]["input_manifest"]
    assert manifest.files[0].raw_id == raw_id
    assert manifest.files[0].sandbox_path == "/job/input/01-input.bin"
    assert request_calls[-1]["idempotency_key"].startswith("ecmd-")
    assert kernel.submitted_inputs == (manifest, batch_identity, (authorized_file,))
    assert body not in repr(request_calls).encode()


class _TerminalDirectKernel(_FakeCommandKernel):
    def __init__(self) -> None:
        super().__init__()
        generated = GeneratedFile(
            relative_path="reports/result.txt",
            size_bytes=6,
            sha256=sha256_bytes(b"result"),
            mode=0o640,
        )
        self.receipt = CommandReceipt(
            job_id="1" * 32,
            status=CommandStatus.COMPLETED,
            lane=CommandLane.SHELL,
            origin=CommandOrigin.MODEL,
            isolation_profile=IsolationProfile.HOST_USER,
            command_digest="3" * 64,
            argv_sha256="4" * 64,
            source_hash="5" * 64,
            exit_code=7,
            signal=None,
            timed_out=False,
            cancelled=False,
            truncated_stdout=False,
            truncated_stderr=False,
            started_at=10.0,
            finished_at=11.0,
            executable=None,
            stdout_sha256=sha256_bytes(b"stdout\n"),
            stderr_sha256=sha256_bytes(b"stderr\n"),
            stdout=b"stdout\n",
            stderr=b"stderr\n",
            generated_files=(generated,),
            error_code="",
            effect_boundary_crossed=True,
            receipt_mac="6" * 64,
        )
        self.pending_archive_delivery = True
        self.terminal_receipt_reads = 0

    def wait(self, *_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        return None

    def progress(
        self,
        job_id: str,
        *,
        actor_id: str,
        conversation_id: str | None = None,
    ) -> CommandProgress:
        assert (job_id, actor_id, conversation_id) == (
            self.receipt.job_id,
            LEGACY_OWNER_USER_ID,
            "conv_owner",
        )
        return CommandProgress(
            job_id=job_id,
            status=CommandStatus.COMPLETED,
            elapsed_sec=1.0,
            stdout_bytes=len(self.receipt.stdout),
            stderr_bytes=len(self.receipt.stderr),
            output_activity=True,
            isolation_profile=IsolationProfile.HOST_USER,
        )

    def terminal_receipt(
        self,
        job_id: str,
        *,
        actor_id: str,
        conversation_id: str,
        timeout_sec: float,
    ) -> tuple[CommandReceipt, int]:
        assert (job_id, actor_id, conversation_id, timeout_sec) == (
            self.receipt.job_id,
            LEGACY_OWNER_USER_ID,
            "conv_owner",
            0.1,
        )
        self.terminal_receipt_reads += 1
        return self.receipt, 2

    def terminal_result(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
        raise AssertionError("direct model result must not consume the archive delivery bytes")


def test_terminal_autonomous_step_returns_receipt_without_consuming_archive_delivery() -> None:
    actor = ActorContext(
        LEGACY_OWNER_USER_ID,
        "owner",
        "telegram-bridge",
        identity_id="5001",
    )
    service = _direct_service(actor)
    kernel = _TerminalDirectKernel()
    service.kernel = kernel

    result = service.execute(
        actor=actor,
        command="printf autonomous",
        timeout_sec=10,
        _conversation_id="conv_owner",
        _source_message_id="msg_0123456789abcdef",
        _telegram_update_id="100",
        _step_id="ecstep-" + "4" * 32,
    )

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["exit_code"] == 7
    assert result["stdout"] == "stdout\n"
    assert result["stderr"] == "stderr\n"
    assert result["receipt"]["mac_version"] == 2
    assert result["receipt"]["generated_files_authenticated"] is True
    assert result["generated_files"] == [
        {
            "mode": 0o640,
            "relative_path": "reports/result.txt",
            "sha256": sha256_bytes(b"result"),
            "size_bytes": 6,
        }
    ]
    assert "_attachment" not in result
    assert kernel.terminal_receipt_reads == 1
    assert kernel.pending_archive_delivery is True


class _TerminalOutputKernel:
    def __init__(self, *, receipt_mac_version: int = 2) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.receipt_mac_version = receipt_mac_version
        payload = b"exact output bytes\n"
        descriptor = GeneratedFile(
            relative_path="reports/result.txt",
            size_bytes=len(payload),
            sha256=sha256_bytes(payload),
            mode=0o640,
        )
        self.outputs = ((descriptor, payload),)
        self.receipt = CommandReceipt(
            job_id="2" * 32,
            status=CommandStatus.COMPLETED,
            lane=CommandLane.ARGV,
            origin=CommandOrigin.OWNER_TURN,
            isolation_profile=IsolationProfile.ISOLATED_WORKSPACE,
            command_digest="3" * 64,
            argv_sha256="4" * 64,
            source_hash="5" * 64,
            exit_code=0,
            signal=None,
            timed_out=False,
            cancelled=False,
            truncated_stdout=False,
            truncated_stderr=False,
            started_at=10.0,
            finished_at=11.0,
            executable=None,
            stdout_sha256=sha256_bytes(b"done\n"),
            stderr_sha256=sha256_bytes(b""),
            stdout=b"done\n",
            stderr=b"",
            generated_files=(descriptor,),
            error_code="",
            effect_boundary_crossed=True,
            receipt_mac="6" * 64,
        )

    def resolve_job_reference(
        self,
        job_id: str | None,
        *,
        actor_id: str,
        tenant_id: str,
        conversation_id: str,
        channel: str,
        operation: str = "status",
    ) -> str:
        assert job_id in {None, self.receipt.job_id}
        assert tenant_id == LEGACY_OWNER_USER_ID
        assert channel == "telegram"
        assert operation == "status"
        self.calls.append(("resolve", actor_id, conversation_id))
        return self.receipt.job_id

    def progress(
        self,
        job_id: str,
        *,
        actor_id: str,
        conversation_id: str | None = None,
    ) -> CommandProgress:
        self.calls.append(("progress", actor_id, conversation_id))
        return CommandProgress(
            job_id=job_id,
            status=CommandStatus.COMPLETED,
            elapsed_sec=1.0,
            stdout_bytes=5,
            stderr_bytes=0,
            output_activity=True,
            isolation_profile=IsolationProfile.ISOLATED_WORKSPACE,
        )

    def terminal_result(
        self,
        job_id: str,
        *,
        actor_id: str,
        conversation_id: str | None,
        timeout_sec: float,
    ):
        assert job_id == self.receipt.job_id
        assert timeout_sec == 0.1
        self.calls.append(("terminal_result", actor_id, conversation_id))
        return self.receipt, self.outputs

    def terminal_receipt(
        self,
        job_id: str,
        *,
        actor_id: str,
        conversation_id: str | None,
        timeout_sec: float,
    ) -> tuple[CommandReceipt, int]:
        assert job_id == self.receipt.job_id
        assert timeout_sec == 0.1
        self.calls.append(("terminal_receipt", actor_id, conversation_id))
        return self.receipt, self.receipt_mac_version


def test_terminal_status_builds_one_exact_private_delivery_archive() -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    kernel = _TerminalOutputKernel()
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.kernel = kernel
    service.max_upload_bytes = 4 * 1024 * 1024

    result = service.status(
        actor=actor,
        _conversation_id="conv-owner",
    )
    repeated = service.status(
        actor=actor,
        job_id=kernel.receipt.job_id,
        _conversation_id="conv-owner",
    )

    assert result["ok"] is True
    assert result["artifact_delivery"]["available"] is True
    assert repeated["_attachment"] == result["_attachment"]
    assert kernel.calls == [
        ("resolve", actor.own_id, "conv-owner"),
        ("progress", actor.own_id, "conv-owner"),
        ("terminal_receipt", actor.own_id, "conv-owner"),
        ("terminal_result", actor.own_id, "conv-owner"),
        ("resolve", actor.own_id, "conv-owner"),
        ("progress", actor.own_id, "conv-owner"),
        ("terminal_receipt", actor.own_id, "conv-owner"),
    ]
    attachment = result.pop("_attachment")
    archive_bytes = base64.b64decode(attachment["content_base64"], validate=True)
    assert sha256_bytes(archive_bytes) == result["artifact_delivery"]["sha256"]
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert archive.read("outputs/reports/result.txt") == b"exact output bytes\n"
        assert json.loads(archive.read("RECEIPT.json"))["job_id"] == kernel.receipt.job_id


def test_legacy_terminal_status_is_readable_without_publishing_outputs() -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    kernel = _TerminalOutputKernel(receipt_mac_version=1)
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.kernel = kernel
    service.max_upload_bytes = 4 * 1024 * 1024

    result = service.status(
        actor=actor,
        job_id=kernel.receipt.job_id,
        _conversation_id="conv-owner",
    )

    assert result["ok"] is True
    assert result["receipt"]["mac_version"] == 1
    assert result["receipt"]["generated_files_authenticated"] is False
    assert "generated_files_sha256" not in result["receipt"]
    assert result["generated_files"] == []
    assert result["artifact_delivery"] == {
        "available": False,
        "error_code": "legacy_output_receipt_unpublishable",
    }
    assert "_attachment" not in result
    assert not any(call[0] == "terminal_result" for call in kernel.calls)


def test_unknown_after_restart_remains_honest_without_reading_a_receipt_or_output() -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")

    class _UnknownKernel:
        def resolve_job_reference(self, job_id: str | None, **_kwargs) -> str:  # noqa: ANN003
            return str(job_id)

        def progress(
            self,
            job_id: str,
            *,
            actor_id: str,
            conversation_id: str | None = None,
        ) -> CommandProgress:
            return CommandProgress(
                job_id=job_id,
                status=CommandStatus.UNKNOWN,
                elapsed_sec=1.0,
                stdout_bytes=0,
                stderr_bytes=0,
                output_activity=False,
                isolation_profile=IsolationProfile.ISOLATED_WORKSPACE,
            )

        def terminal_receipt(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
            raise AssertionError("UNKNOWN must not read a terminal receipt")

    service = EngineerCommandService.__new__(EngineerCommandService)
    service.kernel = _UnknownKernel()
    service.max_upload_bytes = 4 * 1024 * 1024

    result = service.status(
        actor=actor,
        job_id="7" * 32,
        _conversation_id="conv-owner",
    )

    assert result["ok"] is True
    assert result["status"] == "unknown"
    assert result["artifact_delivery"] == {
        "available": False,
        "error_code": "job_output_unpublishable",
    }


@pytest.mark.parametrize(
    ("actor", "conversation_id", "error_code"),
    [
        (
            ActorContext("ordinary-user", "user", "telegram-bridge"),
            "conv-owner",
            "authorization_denied",
        ),
        (
            ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge"),
            "",
            "conversation_required",
        ),
    ],
)
def test_command_management_requires_owner_and_exact_conversation(
    actor: ActorContext,
    conversation_id: str,
    error_code: str,
) -> None:
    kernel = _TerminalOutputKernel()
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.kernel = kernel
    service.max_upload_bytes = 4 * 1024 * 1024

    result = service.status(
        actor=actor,
        job_id=kernel.receipt.job_id,
        _conversation_id=conversation_id,
    )

    assert result["ok"] is False
    assert result["error_code"] == error_code
    assert kernel.calls == []


def test_command_status_normalizes_corrupt_ledger_failures() -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "telegram-bridge")
    service = EngineerCommandService.__new__(EngineerCommandService)
    service.kernel = SimpleNamespace(
        resolve_job_reference=lambda *_args, **_kwargs: "2" * 32,
        progress=lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("private corruption")),
    )
    service.max_upload_bytes = 4 * 1024 * 1024

    result = service.status(
        actor=actor,
        job_id="2" * 32,
        _conversation_id="conv-owner",
    )

    assert result == {
        "effect_boundary_crossed": False,
        "error_code": "corrupt_job_state",
        "ok": False,
        "status": "failed",
    }


def test_distinct_code_owned_steps_admit_distinct_autonomous_shell_jobs() -> None:
    actor = ActorContext(
        LEGACY_OWNER_USER_ID,
        "owner",
        "telegram-bridge",
        identity_id="5001",
    )
    service = _direct_service(actor)

    results = [
        service.execute(
            actor=actor,
            command="printf autonomous",
            timeout_sec=10,
            _conversation_id="conv_owner",
            _source_message_id="msg_0123456789abcdef",
            _telegram_update_id="100",
            _step_id=step_id,
        )
        for step_id in (
            "ecstep-" + "1" * 32,
            "ecstep-" + "2" * 32,
            "ecstep-" + "1" * 32,
        )
    ]

    assert all(item["ok"] is True for item in results)
    assert all(item["job_id"] == "1" * 32 for item in results)
    kernel = service.kernel
    requests = kernel.requests
    assert all(item.lane is CommandLane.SHELL for item in requests)
    assert all(item.origin is CommandOrigin.MODEL for item in requests)
    assert all(item.shell_command == "printf autonomous" for item in requests)
    assert requests[0].idempotency_key != requests[1].idempotency_key
    assert requests[0].idempotency_key == requests[2].idempotency_key
    assert kernel.delivery_chat_ids == ["5001", "5001", "5001"]
    assert (
        service.authorization.required
        == [
            "engineer.use",
            "engineer.command.run",
        ]
        * 3
    )
    attestations = kernel.authority.source_authority.attestations
    assert all(item["telegram_update_id"] == "100" for item in attestations)
    assert all(item["isolation_profile"] is IsolationProfile.HOST_USER for item in attestations)
    assert [item["idempotency_key"] for item in attestations] == [item.idempotency_key for item in requests]


def test_model_cannot_supply_legacy_approval_or_argv_arguments() -> None:
    actor = ActorContext(
        LEGACY_OWNER_USER_ID,
        "owner",
        "telegram-bridge",
        identity_id="5001",
    )
    service = _direct_service(actor)

    with pytest.raises(TypeError):
        service.execute(  # type: ignore[call-arg]
            actor=actor,
            command="true",
            argv=["/usr/bin/false"],
            _conversation_id="conv_owner",
            _source_message_id="msg_0123456789abcdef",
            _telegram_update_id="100",
            _step_id="ecstep-" + "1" * 32,
            _approval_id="apr_0123456789abcdef",
        )

    assert service.kernel.requests == []


def test_source_row_must_bind_the_original_telegram_update() -> None:
    actor = ActorContext(
        LEGACY_OWNER_USER_ID,
        "owner",
        "telegram-bridge",
        identity_id="5001",
    )
    service = _direct_service(actor, source_update_id="100")
    result = service.execute(
        actor=actor,
        command="/usr/bin/true",
        timeout_sec=10,
        _conversation_id="conv_owner",
        _source_message_id="msg_0123456789abcdef",
        _telegram_update_id="999",
        _step_id="ecstep-" + "3" * 32,
    )
    assert result["ok"] is False
    assert result["error_code"] == "owner_source_unavailable"
    assert service.kernel.requests == []


@pytest.mark.parametrize("revocation", ("inactive", "identity", "capability"))
def test_autonomous_admission_rechecks_current_owner_authority(revocation: str) -> None:
    actor = ActorContext(
        LEGACY_OWNER_USER_ID,
        "owner",
        "telegram-bridge",
        identity_id="5001",
    )
    service = _direct_service(actor)
    if revocation == "inactive":
        current_get_user = service.storage.get_user

        def inactive(user_id: str):  # noqa: ANN202
            return {**current_get_user(user_id), "status": "disabled"}

        service.storage.get_user = inactive
    elif revocation == "identity":
        service.storage.resolve_identity = lambda _source, _external_id: "another-user"
    else:
        service.authorization.require = lambda _actor, _capability: (_ for _ in ()).throw(
            PermissionError("revoked")
        )

    result = service.execute(
        actor=actor,
        command="/usr/bin/true",
        timeout_sec=10,
        _conversation_id="conv_owner",
        _source_message_id="msg_0123456789abcdef",
        _telegram_update_id="100",
        _step_id="ecstep-" + "6" * 32,
    )

    assert result["ok"] is False
    assert result["error_code"] == "authorization_denied"
    assert service.kernel.requests == []


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
