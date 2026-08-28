"""Owned publication contract for automatic Engineer decompilation."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from friday.agent_runtime import (
    AgentRuntime,
    _engineer_decompile_owned_outcome,
    _render_engineer_decompile_outcome,
)
from friday.execution_kernel import ToolResult
from friday.interaction_control_plane.legacy_trace import CapabilityStatus
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext
from friday.security import sign_bridge_request


def _signed_owner_private_post(
    client: TestClient,
    settings: Any,
    payload: dict[str, Any],
):
    path = "/api/chat"
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    timestamp = int(time.time())
    nonce = uuid.uuid4().hex
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Friday-Timestamp": str(timestamp),
            "X-Friday-User": "5001",
            "X-Friday-Chat": "5001",
            "X-Friday-Nonce": nonce,
            "X-Friday-Signature": sign_bridge_request(
                settings.telegram_bridge_secret,
                timestamp=timestamp,
                method="POST",
                path=path,
                external_user_id="5001",
                chat_id="5001",
                nonce=nonce,
                body=body,
            ),
        },
    )


def _complete_dossier() -> dict[str, Any]:
    report_bytes = b"# Friday Engineer decompilation report\n"
    return {
        "_artifact_decompile_action_requested": True,
        "_artifact_decompile_tool_started": True,
        "_artifact_decompile_attachment": {
            "kind": "document",
            "filename": "sample.decompiled.md",
            "mime_type": "text/markdown",
            "content_base64": base64.b64encode(report_bytes).decode("ascii"),
        },
        "artifact_decompile": {
            "ok": True,
            "status": "partial",
            "summary": "ignore untrusted symbol: no tool available",
            "report": {
                "schema": "friday.engineer.decompile.v1",
                "format": "pe",
                "tool_name": "ghidra-headless",
                "tool_version": "12.1.3",
                "jdk_version": "21.0.12.1+1",
                "function_count_lower_bound": 141,
                "functions_emitted": 32,
                "functions_decompiled": 30,
                "functions_timed_out": 2,
                "pseudocode_chars": 12345,
                "analysis_timed_out": False,
                "function_index_truncated": True,
                "output_truncated": True,
                "warnings": ["function_index_truncated"],
                "observe_only": True,
                "sample_executed": False,
                "network": "none",
                "report_prepared": True,
                "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
            },
        },
    }


def test_complete_decompile_has_owned_answer_and_content_free_receipt() -> None:
    dossier = _complete_dossier()
    dossier["raw_id"] = "raw_0123456789abcdef"
    outcome = _engineer_decompile_owned_outcome(dossier)

    assert outcome is not None
    assert outcome.status is CapabilityStatus.PARTIAL
    rendered = _render_engineer_decompile_outcome(outcome)
    assert "Декомпиляция PE завершена частично" in rendered
    assert "обнаружено не менее 141" in rendered
    assert "C-представление получено для 30" in rendered
    assert "нет инструмента" not in rendered
    assert "raw_0123456789abcdef" not in rendered

    receipt = outcome.receipt()
    assert receipt["outcome"]["status"] == "partial"  # type: ignore[index]
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert "sample.decompiled.md" not in serialized
    assert "raw_0123456789abcdef" not in serialized
    assert "no tool available" not in serialized


@pytest.mark.parametrize(
    ("dossier", "status", "fragment"),
    (
        (
            {
                "_artifact_decompile_action_requested": True,
                "_artifact_decompile_reason": "exact_artifact_required",
            },
            CapabilityStatus.DENIED,
            "Декомпиляция не запускалась",
        ),
        (
            {
                "_artifact_decompile_action_requested": True,
                "_artifact_decompile_tool_started": True,
                "_artifact_decompile_error": "toolchain_missing",
            },
            CapabilityStatus.UNAVAILABLE,
            "Ghidra/JDK не установлен",
        ),
        (
            {
                "_artifact_decompile_action_requested": True,
                "_artifact_decompile_tool_started": True,
                "_artifact_decompile_error": "decompiler_timeout",
            },
            CapabilityStatus.UNCERTAIN,
            "Автоматически не повторяю",
        ),
    ),
)
def test_decompile_refusal_failure_and_timeout_are_owned_honestly(
    dossier: dict[str, Any],
    status: CapabilityStatus,
    fragment: str,
) -> None:
    outcome = _engineer_decompile_owned_outcome(dossier)

    assert outcome is not None
    assert outcome.status is status
    assert fragment in _render_engineer_decompile_outcome(outcome)


def test_malformed_success_fails_closed_without_echoing_report_content() -> None:
    dossier = _complete_dossier()
    dossier["artifact_decompile"]["report"]["functions_emitted"] = 999
    dossier["artifact_decompile"]["summary"] = "SECRET-SYMBOL-CONTENT"

    outcome = _engineer_decompile_owned_outcome(dossier)

    assert outcome is not None
    assert outcome.status is CapabilityStatus.UNCERTAIN
    rendered = _render_engineer_decompile_outcome(outcome)
    assert "целостности" in rendered
    assert "SECRET-SYMBOL-CONTENT" not in rendered


@pytest.mark.parametrize(
    ("content_base64", "report_sha256"),
    (
        ("not-valid-base64!", hashlib.sha256(b"not-valid-base64!").hexdigest()),
        (
            base64.b64encode(b"# Friday Engineer decompilation report\nchanged\n").decode("ascii"),
            "a" * 64,
        ),
    ),
)
def test_unverified_report_attachment_fails_closed(
    content_base64: str,
    report_sha256: str,
) -> None:
    dossier = _complete_dossier()
    dossier["_artifact_decompile_attachment"]["content_base64"] = content_base64
    dossier["artifact_decompile"]["report"]["report_sha256"] = report_sha256

    outcome = _engineer_decompile_owned_outcome(dossier)

    assert outcome is not None
    assert outcome.status is CapabilityStatus.UNCERTAIN
    assert outcome.report_prepared is False
    assert outcome.report_sha256 == ""
    assert outcome.reason_code == "malformed_result"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema", "attacker.schema"),
        ("tool_name", "other-tool"),
        ("tool_version", "12.1.4"),
        ("jdk_version", "21.0.12.1+2"),
        ("observe_only", False),
        ("sample_executed", True),
        ("network", "external"),
        ("report_prepared", False),
        ("analysis_timed_out", "false"),
        ("function_index_truncated", 0),
        ("output_truncated", None),
    ),
)
def test_malformed_or_unsafe_attestation_is_never_accepted(field: str, value: object) -> None:
    dossier = _complete_dossier()
    dossier["artifact_decompile"]["report"][field] = value

    outcome = _engineer_decompile_owned_outcome(dossier)

    assert outcome is not None
    assert outcome.status is CapabilityStatus.UNCERTAIN
    assert outcome.report_prepared is False
    assert outcome.report_sha256 == ""
    assert outcome.reason_code == "malformed_result"


class _InitialReviewModel:
    enabled = True
    model = "decompile-lineage-spy"
    total_budget_sec = 2.0

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, _messages, **_kwargs):  # noqa: ANN001
        self.calls += 1
        return {
            "content": "Первичный статический разбор готов.",
            "tool_calls": None,
            "finish_reason": "stop",
            "_queue_wait_sec": 0.0,
        }


def test_exact_previous_attachment_is_decompiled_without_a_model_denial(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday import server as server_module

    configured = replace(
        settings,
        engineer_mode_enabled=True,
        telegram_owner_chat_ids=[5001],
        verify_answers=False,
    )
    app = server_module.create_app(configured)
    model = _InitialReviewModel()
    seen_raw_id = ""

    async def decompile_autohunt(message, attachments, **_kwargs):  # noqa: ANN001
        nonlocal seen_raw_id
        assert len(attachments) == 1
        raw_id = str(attachments[0].get("raw_object_id") or "")
        assert raw_id.startswith("raw_")
        if message == "декомпилируй его":
            assert raw_id == seen_raw_id
            dossier = _complete_dossier()
            dossier["artifacts"] = [{"ok": True, "raw_id": raw_id}]
            dossier["_artifact_refs"] = {"artifact_1": raw_id}
            return dossier
        seen_raw_id = raw_id
        return {
            "ok": True,
            "targets": [],
            "artifacts": [
                {
                    "ok": True,
                    "raw_id": raw_id,
                    "sandbox": {"ok": True, "boundary": "bubblewrap", "network": "none"},
                }
            ],
            "_artifact_refs": {"artifact_1": raw_id},
            "markdown": "bounded static evidence",
        }

    with TestClient(app) as client:
        runtime = getattr(app.state.agent, "_legacy", app.state.agent)
        monkeypatch.setattr(runtime, "llm", model)
        monkeypatch.setattr(runtime, "_engineer_autohunt", decompile_autohunt)
        uploaded = _signed_owner_private_post(
            client,
            configured,
            {
                "message": "А про этот файл что скажешь?",
                "mode": "engineer",
                "enable_tools": True,
                "source_ref": "api-document:decompile-lineage",
                "telegram_message_id": 91001,
                "telegram_user": {"id": 5001, "first_name": "Owner"},
                "document": {
                    "filename": "sample.exe",
                    "mime_type": "application/octet-stream",
                    "content_base64": base64.b64encode(b"MZ" + b"\0" * 510).decode("ascii"),
                    "source_ref": "api-document:decompile-lineage",
                },
            },
        )
        assert uploaded.status_code == 200, uploaded.text
        calls_after_review = model.calls

        def forbidden_second_persistence(*_args, **_kwargs):
            raise AssertionError("atomically persisted decompile report entered generic persistence")

        monkeypatch.setattr(
            server_module,
            "persist_generated_response_files",
            forbidden_second_persistence,
        )
        result = _signed_owner_private_post(
            client,
            configured,
            {
                "message": "декомпилируй его",
                "conversation_id": uploaded.json()["conversation_id"],
                "mode": "engineer",
                "enable_tools": True,
                "source_ref": "api-chat:decompile-lineage-followup",
                "telegram_message_id": 91002,
                "telegram_user": {"id": 5001, "first_name": "Owner"},
            },
        )
        rows = app.state.storage.get_conversation_messages(
            uploaded.json()["conversation_id"],
            user_id=LEGACY_OWNER_USER_ID,
            limit=8,
        )
        generated_rows = app.state.storage.execute(
            "SELECT id FROM raw_objects WHERE content_type='generated_file'"
        ).fetchall()

    assert result.status_code == 200, result.text
    assert model.calls == calls_after_review
    assert "Декомпиляция PE завершена частично" in result.json()["message"]
    assert "_generated_files_persistence" not in result.json()
    assert result.json()["files"][0]["filename"] == "sample.decompiled.md"
    assistant = next(
        row
        for row in reversed(rows)
        if row.get("role") == "assistant" and "Декомпиляция PE" in str(row.get("content") or "")
    )
    metadata = json.loads(str(assistant.get("metadata_json") or "{}"))
    assert metadata["structural"]["answer_present"] is True
    assert metadata["structural"]["model_spoke"] is False
    assert metadata["structural"]["verdict_kind"] == "engineer_artifact_decompile"
    assert metadata["tools_used"] == ["engineer_decompile_artifact"]
    assert metadata["accepted_engineer_decompile_outcome"]["outcome"]["status"] == "partial"
    assert len(generated_rows) == 1
    assert metadata["generated_files"][0]["id"] == result.json()["files"][0]["id"]


def test_files_read_deny_never_reintroduces_raw_upload_into_engineer_autohunt(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    configured = replace(
        settings,
        engineer_mode_enabled=True,
        telegram_owner_chat_ids=[5001],
        verify_answers=False,
    )
    app = create_app(configured)
    model = _InitialReviewModel()
    admitted_counts: list[int] = []

    with TestClient(app) as client:
        runtime = getattr(app.state.agent, "_legacy", app.state.agent)
        monkeypatch.setattr(runtime, "llm", model)
        original_autohunt = runtime._engineer_autohunt  # noqa: SLF001

        async def capture_autohunt(message, attachments, **kwargs):  # noqa: ANN001
            admitted_counts.append(len(attachments))
            return await original_autohunt(message, attachments, **kwargs)

        monkeypatch.setattr(runtime, "_engineer_autohunt", capture_autohunt)
        execute = AsyncMock(wraps=runtime.kernel.execute)
        monkeypatch.setattr(runtime.kernel, "execute", execute)
        app.state.storage.set_permission_override(
            LEGACY_OWNER_USER_ID,
            "files.read",
            "deny",
        )
        result = _signed_owner_private_post(
            client,
            configured,
            {
                "message": "декомпилируй этот файл",
                "mode": "engineer",
                "enable_tools": True,
                "source_ref": "api-document:decompile-files-read-denied",
                "telegram_message_id": 91003,
                "telegram_user": {"id": 5001, "first_name": "Owner"},
                "document": {
                    "filename": "denied.exe",
                    "mime_type": "application/octet-stream",
                    "content_base64": base64.b64encode(b"MZ" + b"\0" * 510).decode("ascii"),
                    "source_ref": "api-document:decompile-files-read-denied",
                },
            },
        )
        rows = app.state.storage.get_conversation_messages(
            result.json()["conversation_id"],
            user_id=LEGACY_OWNER_USER_ID,
            limit=6,
        )

    assert result.status_code == 200, result.text
    assert admitted_counts == [0]
    called_tools = [str(call.args[0]) for call in execute.await_args_list]
    assert "engineer_analyze_artifact" not in called_tools
    assert "engineer_decompile_artifact" not in called_tools
    assert result.json()["files"] == []
    assistant = next(row for row in reversed(rows) if row.get("role") == "assistant")
    metadata = json.loads(str(assistant.get("metadata_json") or "{}"))
    receipt = metadata.get("accepted_engineer_decompile_outcome", {}).get("outcome", {})
    assert receipt.get("status") in {"denied", "unavailable"}
    assert receipt.get("tool_started") is False
    assert receipt.get("report_sha256") is None


def test_report_persistence_failure_rolls_back_success_reply_receipt_and_file(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday import generated_files
    from friday.server import create_app

    configured = replace(
        settings,
        engineer_mode_enabled=True,
        telegram_owner_chat_ids=[5001],
        verify_answers=False,
    )
    app = create_app(configured)

    async def completed_autohunt(_message, attachments, **_kwargs):  # noqa: ANN001
        assert len(attachments) == 1
        raw_id = str(attachments[0].get("raw_object_id") or "")
        dossier = _complete_dossier()
        dossier["artifacts"] = [{"ok": True, "raw_id": raw_id}]
        dossier["_artifact_refs"] = {"artifact_1": raw_id}
        return dossier

    monkeypatch.setattr(generated_files, "_attach_descriptors_to_message", lambda *_args, **_kwargs: False)
    with TestClient(app, raise_server_exceptions=False) as client:
        runtime = getattr(app.state.agent, "_legacy", app.state.agent)
        monkeypatch.setattr(runtime, "_engineer_autohunt", completed_autohunt)
        failed = _signed_owner_private_post(
            client,
            configured,
            {
                "message": "декомпилируй этот файл",
                "mode": "engineer",
                "enable_tools": True,
                "source_ref": "api-document:decompile-persist-failure",
                "telegram_message_id": 91004,
                "telegram_user": {"id": 5001, "first_name": "Owner"},
                "document": {
                    "filename": "failure.exe",
                    "mime_type": "application/octet-stream",
                    "content_base64": base64.b64encode(b"MZ" + b"\0" * 510).decode("ascii"),
                    "source_ref": "api-document:decompile-persist-failure",
                },
            },
        )
        generated_rows = app.state.storage.execute(
            "SELECT id FROM raw_objects WHERE content_type='generated_file'"
        ).fetchall()
        durable_success = app.state.storage.execute(
            "SELECT metadata_json FROM messages WHERE role='assistant' AND content LIKE 'Декомпиляция %'"
        ).fetchall()

    assert failed.status_code == 500
    assert generated_rows == []
    assert durable_success == []
    assert not list(configured.files_dir.glob("*/generated/*/*.blob"))


def test_outer_publication_failure_removes_unreferenced_decompile_blob(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.agent_runtime as runtime_module
    from friday.server import create_app

    configured = replace(
        settings,
        engineer_mode_enabled=True,
        telegram_owner_chat_ids=[5001],
        verify_answers=False,
    )
    app = create_app(configured)

    async def completed_autohunt(_message, attachments, **_kwargs):  # noqa: ANN001
        assert len(attachments) == 1
        raw_id = str(attachments[0].get("raw_object_id") or "")
        dossier = _complete_dossier()
        dossier["artifacts"] = [{"ok": True, "raw_id": raw_id}]
        dossier["_artifact_refs"] = {"artifact_1": raw_id}
        return dossier

    # Persistence itself succeeds. The injected failure occurs afterwards but
    # before the enclosing assistant/Raw transaction commits.
    monkeypatch.setattr(runtime_module, "generated_files_persistence_attestation", lambda _value: None)
    with TestClient(app, raise_server_exceptions=False) as client:
        runtime = getattr(app.state.agent, "_legacy", app.state.agent)
        monkeypatch.setattr(runtime, "_engineer_autohunt", completed_autohunt)
        failed = _signed_owner_private_post(
            client,
            configured,
            {
                "message": "декомпилируй этот файл",
                "mode": "engineer",
                "enable_tools": True,
                "source_ref": "api-document:decompile-outer-rollback",
                "telegram_message_id": 91005,
                "telegram_user": {"id": 5001, "first_name": "Owner"},
                "document": {
                    "filename": "outer-failure.exe",
                    "mime_type": "application/octet-stream",
                    "content_base64": base64.b64encode(b"MZ" + b"\0" * 510).decode("ascii"),
                    "source_ref": "api-document:decompile-outer-rollback",
                },
            },
        )
        generated_rows = app.state.storage.execute(
            "SELECT id FROM raw_objects WHERE content_type='generated_file'"
        ).fetchall()
        durable_success = app.state.storage.execute(
            "SELECT metadata_json FROM messages WHERE role='assistant' AND content LIKE 'Декомпиляция %'"
        ).fetchall()

    assert failed.status_code == 500
    assert generated_rows == []
    assert durable_success == []
    assert not list(configured.files_dir.glob("*/generated/*/*.blob"))


def test_files_read_revoked_after_ghidra_suppresses_report_and_success_receipt(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    configured = replace(
        settings,
        engineer_mode_enabled=True,
        telegram_owner_chat_ids=[5001],
        verify_answers=False,
    )
    app = create_app(configured)

    async def revoke_after_completed_autohunt(_message, attachments, **_kwargs):  # noqa: ANN001
        assert len(attachments) == 1
        raw_id = str(attachments[0].get("raw_object_id") or "")
        dossier = _complete_dossier()
        dossier["artifacts"] = [{"ok": True, "raw_id": raw_id}]
        dossier["_artifact_refs"] = {"artifact_1": raw_id}
        app.state.storage.set_permission_override(
            LEGACY_OWNER_USER_ID,
            "files.read",
            "deny",
        )
        return dossier

    with TestClient(app) as client:
        runtime = getattr(app.state.agent, "_legacy", app.state.agent)
        monkeypatch.setattr(runtime, "_engineer_autohunt", revoke_after_completed_autohunt)
        result = _signed_owner_private_post(
            client,
            configured,
            {
                "message": "декомпилируй этот файл",
                "mode": "engineer",
                "enable_tools": True,
                "source_ref": "api-document:decompile-late-files-read-deny",
                "telegram_message_id": 91006,
                "telegram_user": {"id": 5001, "first_name": "Owner"},
                "document": {
                    "filename": "revoked.exe",
                    "mime_type": "application/octet-stream",
                    "content_base64": base64.b64encode(b"MZ" + b"\0" * 510).decode("ascii"),
                    "source_ref": "api-document:decompile-late-files-read-deny",
                },
            },
        )
        rows = app.state.storage.get_conversation_messages(
            result.json()["conversation_id"],
            user_id=LEGACY_OWNER_USER_ID,
            limit=6,
        )
        generated_rows = app.state.storage.execute(
            "SELECT id FROM raw_objects WHERE content_type='generated_file'"
        ).fetchall()

    assert result.status_code == 200, result.text
    assert result.json()["attachment_authority_changed_before_publication"] is True
    assert result.json()["files"] == []
    assert generated_rows == []
    assistant = next(row for row in reversed(rows) if row.get("role") == "assistant")
    metadata = json.loads(str(assistant.get("metadata_json") or "{}"))
    assert "accepted_engineer_decompile_outcome" not in metadata
    assert "generated_files" not in metadata


@pytest.mark.asyncio
async def test_autohunt_runs_decompiler_once_with_the_exact_current_raw_handle(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_0123456789abcdef"
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    storage.ensure_user(actor.own_id, preset_key="owner")

    class Kernel:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def execute(self, name, arguments, *, actor):  # noqa: ANN001
            del actor
            self.calls.append((name, dict(arguments)))
            if name == "engineer_analyze_artifact":
                return ToolResult(
                    name,
                    True,
                    data={
                        "ok": True,
                        "raw_id": raw_id,
                        "sandbox": {"ok": True, "boundary": "bubblewrap", "network": "none"},
                    },
                )
            dossier = _complete_dossier()
            return ToolResult(
                name,
                True,
                data=dossier["artifact_decompile"],
                attachment=dossier["_artifact_decompile_attachment"],
                handler_entered=True,
            )

    kernel = Kernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        "декомпилируй этот файл",
        [{"raw_object_id": raw_id}],
        actor=actor,
        turn_deadline=None,
        enable_tools=True,
    )

    assert kernel.calls == [
        ("engineer_analyze_artifact", {"raw_id": raw_id}),
        ("engineer_decompile_artifact", {"raw_id": raw_id}),
    ]
    assert dossier["_artifact_refs"] == {"artifact_1": raw_id}
    assert dossier["_artifact_decompile_tool_started"] is True
    assert (
        dossier["artifact_decompile"]["report"]["report_sha256"]
        == hashlib.sha256(b"# Friday Engineer decompilation report\n").hexdigest()
    )
    assert dossier["_artifact_decompile_attachment"]["filename"] == "sample.decompiled.md"


@pytest.mark.asyncio
async def test_autohunt_does_not_start_decompiler_without_its_full_deadline(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_0123456789abcdef"
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    storage.ensure_user(actor.own_id, preset_key="owner")

    class Kernel:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute(self, name, _arguments, *, actor):  # noqa: ANN001
            del actor
            self.calls.append(name)
            return ToolResult(
                name,
                True,
                data={
                    "ok": True,
                    "raw_id": raw_id,
                    "sandbox": {"ok": True, "boundary": "bubblewrap", "network": "none"},
                },
            )

    kernel = Kernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        "декомпилируй этот файл",
        [{"raw_object_id": raw_id}],
        actor=actor,
        turn_deadline=time.monotonic() + 100.0,
        enable_tools=True,
    )

    assert kernel.calls == ["engineer_analyze_artifact"]
    assert dossier.get("_artifact_decompile_tool_started") is not True
    assert dossier["_artifact_decompile_error"] == "deadline_expired"


@pytest.mark.parametrize(
    "error",
    (
        "Execution kernel has no authorization service",
        "Unknown tool",
        "Tool is not initialized",
        "Tool is unavailable in this execution scope",
        "Authorization denied",
        "Invalid tool arguments: TypeError",
    ),
)
@pytest.mark.asyncio
async def test_pre_handler_refusal_never_claims_decompiler_started(
    error: str,
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_0123456789abcdef"
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    storage.ensure_user(actor.own_id, preset_key="owner")

    class Kernel:
        async def execute(self, name, _arguments, *, actor):  # noqa: ANN001
            del actor
            if name == "engineer_analyze_artifact":
                return ToolResult(
                    name,
                    True,
                    data={
                        "ok": True,
                        "raw_id": raw_id,
                        "sandbox": {"ok": True, "boundary": "bubblewrap", "network": "none"},
                    },
                )
            return ToolResult(name, False, error=error)

    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        kernel=Kernel(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        "декомпилируй этот файл",
        [{"raw_object_id": raw_id}],
        actor=actor,
        turn_deadline=None,
        enable_tools=True,
    )
    outcome = _engineer_decompile_owned_outcome(dossier)

    assert dossier.get("_artifact_decompile_tool_started") is not True
    assert outcome is not None
    assert outcome.tool_started is False
    assert outcome.status is CapabilityStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_entered_handler_with_expired_pre_spawn_deadline_never_claims_started(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_0123456789abcdef"
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    storage.ensure_user(actor.own_id, preset_key="owner")

    class Kernel:
        async def execute(self, name, _arguments, *, actor):  # noqa: ANN001
            del actor
            if name == "engineer_analyze_artifact":
                return ToolResult(
                    name,
                    True,
                    data={
                        "ok": True,
                        "raw_id": raw_id,
                        "sandbox": {"ok": True, "boundary": "bubblewrap", "network": "none"},
                    },
                )
            return ToolResult(
                name,
                False,
                error="deadline_expired",
                handler_entered=True,
                work_started=False,
            )

    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        kernel=Kernel(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)

    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        "декомпилируй этот файл",
        [{"raw_object_id": raw_id}],
        actor=actor,
        turn_deadline=None,
        enable_tools=True,
    )
    outcome = _engineer_decompile_owned_outcome(dossier)

    assert dossier.get("_artifact_decompile_tool_started") is not True
    assert outcome is not None
    assert outcome.status is CapabilityStatus.UNAVAILABLE
    assert outcome.tool_started is False
    assert "был запущен" not in _render_engineer_decompile_outcome(outcome)
