"""Owned action and atomic publication contract for Java compilation."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday.agent_runtime import (
    AgentRuntime,
    _engineer_compile_owned_outcome,
    _EngineerCompileSourceBinding,
    _render_engineer_compile_outcome,
)
from friday.execution_kernel import ToolResult
from friday.interaction_control_plane.legacy_trace import CapabilityStatus
from friday.organs.engineer import ENGINEER_BUILD, ENGINEER_USE, compiler
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext
from friday.source_identity import authorized_file_snapshot_token


def _class_bytes() -> bytes:
    return b"\xca\xfe\xba\xbe\x00\x00\x00\x41bounded-main"


def _jar() -> bytes:
    payload = compiler._deterministic_jar([("Main.class", _class_bytes())])  # noqa: SLF001
    assert payload is not None
    return payload


def _source_binding(
    source: bytes,
    *,
    raw_id: str = "raw_0123456789abcdef",
    source_identity_sha256: str = "a" * 64,
) -> _EngineerCompileSourceBinding:
    return _EngineerCompileSourceBinding(
        raw_id=raw_id,
        filename="Main.java",
        source_sha256=hashlib.sha256(source).hexdigest(),
        source_identity_sha256=source_identity_sha256,
    )


def _authorized_source_bytes(raw_id: str, filename: str, source: bytes) -> SimpleNamespace:
    raw = {
        "id": raw_id,
        "source": "upload",
        "source_ref": f"test:{raw_id}",
        "content_type": "file",
        "received_at": "2026-08-26T00:00:00+00:00",
        "content_hash": hashlib.sha256(source).hexdigest(),
        "_raw_content": "",
        "_raw_metadata": "{}",
    }
    token = authorized_file_snapshot_token(
        raw,
        content_sha256=hashlib.sha256(source).hexdigest(),
    )
    assert token is not None
    return SimpleNamespace(
        raw_id=raw_id,
        filename=filename,
        content=source,
        snapshot_token=token,
    )


def _complete_dossier(
    source: bytes = b"public class Main {}\n",
    *,
    binding: _EngineerCompileSourceBinding | None = None,
) -> dict[str, Any]:
    jar = _jar()
    return {
        "_artifact_compile_action_requested": True,
        "_artifact_compile_tool_started": True,
        "_artifact_compile_source_binding": binding or _source_binding(source),
        "_artifact_compile_attachment": {
            "kind": "document",
            "filename": "Main.compiled.jar",
            "mime_type": "application/java-archive",
            "content_base64": base64.b64encode(jar).decode("ascii"),
        },
        "artifact_compile": {
            "ok": True,
            "status": "completed",
            "summary": "untrusted model-like compiler text",
            "sandbox": {
                "ok": True,
                "boundary": "bubblewrap",
                "network": "none",
                "compile_pids_limit": 512,
                "compile_memory_limit_bytes": 12 * 1024**3,
            },
            "report": {
                "schema": compiler.SCHEMA,
                "status": "completed",
                "profile": compiler.PROFILE,
                "tool_name": compiler.TOOL_NAME,
                "tool_version": compiler.JDK_VERSION,
                "jdk_version": compiler.JDK_VERSION,
                "source_sha256": hashlib.sha256(source).hexdigest(),
                "jar_sha256": hashlib.sha256(jar).hexdigest(),
                "source_size_bytes": len(source),
                "class_files": 1,
                "class_bytes": len(_class_bytes()),
                "jar_size_bytes": len(jar),
                "java_release": 21,
                "class_major_version": 65,
                "archive": "jar",
                "compression": "stored",
                "signed": False,
                "manifest": False,
                "runtime_validation": "not_performed",
                "sample_executed": False,
                "network": "none",
                "jar_prepared": True,
            },
        },
    }


def test_complete_compile_has_owned_answer_and_content_free_receipt() -> None:
    dossier = _complete_dossier()
    dossier["raw_id"] = "raw_0123456789abcdef"
    outcome = _engineer_compile_owned_outcome(dossier)

    assert outcome is not None
    assert outcome.status is CapabilityStatus.SUCCEEDED
    rendered = _render_engineer_compile_outcome(outcome)
    assert "Java 21" in rendered
    assert "1 class" in rendered
    assert "не запускались" in rendered
    assert "untrusted model-like compiler text" not in rendered
    assert "raw_0123456789abcdef" not in rendered

    receipt = outcome.receipt()
    assert receipt["outcome"]["status"] == "succeeded"  # type: ignore[index]
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert "Main.compiled.jar" not in serialized
    assert "raw_0123456789abcdef" not in serialized
    assert "untrusted model-like compiler text" not in serialized


@pytest.mark.parametrize(
    ("dossier", "status", "fragment"),
    (
        (
            {
                "_artifact_compile_action_requested": True,
                "_artifact_compile_reason": "exact_artifact_required",
            },
            CapabilityStatus.DENIED,
            "Компиляция не запускалась",
        ),
        (
            {
                "_artifact_compile_action_requested": True,
                "_artifact_compile_error": "toolchain_missing",
            },
            CapabilityStatus.UNAVAILABLE,
            "JDK не установлен",
        ),
        (
            {
                "_artifact_compile_action_requested": True,
                "_artifact_compile_tool_started": True,
                "_artifact_compile_error": "compiler_timeout",
            },
            CapabilityStatus.FAILED,
            "JAR не публиковался",
        ),
        (
            {
                "_artifact_compile_action_requested": True,
                "_artifact_compile_tool_started": True,
                "_artifact_compile_error": "compiler_failed",
            },
            CapabilityStatus.FAILED,
            "Компиляция завершилась ошибкой",
        ),
    ),
)
def test_compile_refusal_failure_and_timeout_are_owned_honestly(
    dossier: dict[str, Any],
    status: CapabilityStatus,
    fragment: str,
) -> None:
    outcome = _engineer_compile_owned_outcome(dossier)

    assert outcome is not None
    assert outcome.status is status
    assert fragment in _render_engineer_compile_outcome(outcome)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema", "attacker.schema"),
        ("profile", "arbitrary_flags"),
        ("tool_name", "other-tool"),
        ("jdk_version", "21.0.12.1+2"),
        ("java_release", 22),
        ("class_major_version", 66),
        ("compression", "deflated"),
        ("signed", True),
        ("manifest", True),
        ("runtime_validation", "passed"),
        ("sample_executed", True),
        ("network", "external"),
    ),
)
def test_malformed_or_unsafe_compile_attestation_is_never_accepted(
    field: str,
    value: object,
) -> None:
    dossier = _complete_dossier()
    dossier["artifact_compile"]["report"][field] = value

    outcome = _engineer_compile_owned_outcome(dossier)

    assert outcome is not None
    assert outcome.status is CapabilityStatus.UNCERTAIN
    assert outcome.jar_prepared is False
    assert outcome.jar_sha256 == ""
    assert outcome.reason_code == "malformed_result"


def test_changed_or_noncanonical_jar_attachment_fails_closed() -> None:
    dossier = _complete_dossier()
    changed = _jar() + b"trailing"
    dossier["_artifact_compile_attachment"]["content_base64"] = base64.b64encode(changed).decode("ascii")
    dossier["artifact_compile"]["report"]["jar_sha256"] = hashlib.sha256(changed).hexdigest()
    dossier["artifact_compile"]["report"]["jar_size_bytes"] = len(changed)

    outcome = _engineer_compile_owned_outcome(dossier)

    assert outcome is not None
    assert outcome.status is CapabilityStatus.UNCERTAIN
    assert outcome.jar_prepared is False
    assert outcome.reason_code == "malformed_result"


def test_compile_fresh_actor_requires_current_build_and_files_read(
    settings,
    storage,
) -> None:
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "test")
    storage.ensure_user(actor.own_id, preset_key="owner")
    runtime = AgentRuntime(replace(settings, engineer_mode_enabled=True), storage)
    runtime.kernel.authorization.register_capability(ENGINEER_USE)
    runtime.kernel.authorization.register_capability(ENGINEER_BUILD)

    assert runtime._fresh_engineer_actor(actor, "engineer.artifact.build") is not None  # noqa: SLF001

    storage.set_permission_override(actor.own_id, "files.read", "deny")
    assert runtime._fresh_engineer_actor(actor, "engineer.artifact.build") is None  # noqa: SLF001


def test_compile_source_preparation_selects_one_exact_named_authorized_sibling(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.agent_runtime as runtime_module

    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "test")
    raw_main = "raw_0123456789abcdef"
    raw_helper = "raw_fedcba9876543210"
    sources = {
        raw_main: _authorized_source_bytes(raw_main, "Main.java", b"class Main {}"),
        raw_helper: _authorized_source_bytes(raw_helper, "Helper.java", b"class Helper {}"),
    }
    runtime = AgentRuntime(replace(settings, engineer_mode_enabled=True), storage)
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)
    monkeypatch.setattr(
        runtime_module,
        "read_authorized_file",
        lambda _storage, _root, raw_id, _tenant, **_kwargs: sources[raw_id],
    )

    binding, reason = runtime._prepare_engineer_compile_source(  # noqa: SLF001
        actor,
        [raw_main, raw_helper],
        requested_filename="Main.java",
    )
    assert reason == "none"
    assert binding is not None and binding.raw_id == raw_main

    missing, missing_reason = runtime._prepare_engineer_compile_source(  # noqa: SLF001
        actor,
        [raw_main, raw_helper],
        requested_filename="main.java",
    )
    assert missing is None and missing_reason == "exact_artifact_required"

    ambiguous, ambiguous_reason = runtime._prepare_engineer_compile_source(  # noqa: SLF001
        actor,
        [raw_main, raw_helper],
        requested_filename=None,
    )
    assert ambiguous is None and ambiguous_reason == "ambiguous_artifact"

    sources[raw_helper] = _authorized_source_bytes(raw_helper, "Main.java", b"class Main2 {}")
    duplicate, duplicate_reason = runtime._prepare_engineer_compile_source(  # noqa: SLF001
        actor,
        [raw_main, raw_helper],
        requested_filename="Main.java",
    )
    assert duplicate is None and duplicate_reason == "ambiguous_artifact"


@pytest.mark.asyncio
async def test_autohunt_runs_compiler_once_with_exact_current_raw_handle(
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
                data=dossier["artifact_compile"],
                attachment=dossier["_artifact_compile_attachment"],
                handler_entered=True,
                work_started=True,
            )

    kernel = Kernel()
    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        kernel=kernel,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)
    binding = _source_binding(b"public class Main {}\n", raw_id=raw_id)
    monkeypatch.setattr(
        runtime,
        "_prepare_engineer_compile_source",
        lambda *_args, **_kwargs: (binding, "none"),
    )

    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        "скомпилируй этот Java-файл",
        [{"raw_object_id": raw_id}],
        actor=actor,
        turn_deadline=None,
        enable_tools=True,
    )

    assert kernel.calls == [
        ("engineer_analyze_artifact", {"raw_id": raw_id}),
        (
            "engineer_compile_java",
            {
                "raw_id": raw_id,
                "expected_filename": "Main.java",
                "expected_sha256": binding.source_sha256,
            },
        ),
    ]
    assert dossier["_artifact_refs"] == {"artifact_1": raw_id}
    assert dossier["_artifact_compile_tool_started"] is True
    assert dossier["artifact_compile"]["report"]["jar_sha256"] == hashlib.sha256(_jar()).hexdigest()
    assert dossier["_artifact_compile_attachment"]["filename"] == "Main.compiled.jar"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("work_started", "error", "status"),
    (
        (False, "Engineer tool refused: compiler_busy", CapabilityStatus.UNAVAILABLE),
        (True, "Engineer tool failed: compiler_timeout", CapabilityStatus.FAILED),
    ),
)
async def test_autohunt_compile_entry_truth_survives_kernel_projection(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
    work_started: bool,
    error: str,
    status: CapabilityStatus,
) -> None:
    raw_id = "raw_0123456789abcdef"
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    storage.ensure_user(actor.own_id, preset_key="owner")

    class Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001
            del arguments, actor
            if name == "engineer_analyze_artifact":
                return ToolResult(name, True, data={"ok": True, "raw_id": raw_id})
            return ToolResult(
                name,
                False,
                error=error,
                handler_entered=True,
                work_started=work_started,
            )

    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        kernel=Kernel(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)
    binding = _source_binding(b"public class Main {}\n", raw_id=raw_id)
    monkeypatch.setattr(
        runtime,
        "_prepare_engineer_compile_source",
        lambda *_args, **_kwargs: (binding, "none"),
    )

    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        "Compile Main.java",
        [{"raw_object_id": raw_id}],
        actor=actor,
        turn_deadline=None,
        enable_tools=True,
    )
    outcome = _engineer_compile_owned_outcome(dossier)

    assert dossier["_artifact_compile_tool_started"] is work_started
    assert outcome is not None
    assert outcome.status is status
    assert outcome.tool_started is work_started


@pytest.mark.asyncio
async def test_autohunt_compile_timeout_without_envelope_is_conservatively_entered(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_0123456789abcdef"
    actor = ActorContext(user_id="alice", preset_key="owner", source="test")
    storage.ensure_user(actor.own_id, preset_key="owner")

    class Kernel:
        async def execute(self, name, arguments, *, actor):  # noqa: ANN001
            del arguments, actor
            if name == "engineer_analyze_artifact":
                return ToolResult(name, True, data={"ok": True, "raw_id": raw_id})
            raise TimeoutError("private kernel timeout")

    runtime = AgentRuntime(
        replace(settings, engineer_mode_enabled=True),
        storage,
        kernel=Kernel(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runtime, "_fresh_engineer_actor", lambda current, _capability: current)
    binding = _source_binding(b"public class Main {}\n", raw_id=raw_id)
    monkeypatch.setattr(
        runtime,
        "_prepare_engineer_compile_source",
        lambda *_args, **_kwargs: (binding, "none"),
    )

    dossier = await runtime._engineer_autohunt(  # noqa: SLF001
        "Compile Main.java",
        [{"raw_object_id": raw_id}],
        actor=actor,
        turn_deadline=None,
        enable_tools=True,
    )
    outcome = _engineer_compile_owned_outcome(dossier)

    assert dossier["_artifact_compile_tool_started"] is True
    assert dossier["_artifact_compile_error"] == "deadline_expired"
    assert outcome is not None
    assert outcome.status is CapabilityStatus.UNCERTAIN
    assert outcome.tool_started is True


def test_successful_compile_reply_receipt_and_jar_publish_atomically(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    source = b"public class Main {}\n"
    configured = replace(settings, engineer_mode_enabled=True, verify_answers=False)
    app = create_app(configured)

    async def completed_autohunt(_message, attachments, **_kwargs):  # noqa: ANN001
        assert len(attachments) == 1
        raw_id = str(attachments[0].get("raw_object_id") or "")
        binding, reason = runtime._prepare_engineer_compile_source(  # noqa: SLF001
            _kwargs["actor"],
            [raw_id],
            requested_filename=None,
        )
        assert reason == "none" and binding is not None
        dossier = _complete_dossier(source, binding=binding)
        dossier["artifacts"] = [{"ok": True, "raw_id": raw_id}]
        dossier["_artifact_refs"] = {"artifact_1": raw_id}
        return dossier

    with TestClient(app) as client:
        runtime = getattr(app.state.agent, "_legacy", app.state.agent)
        monkeypatch.setattr(runtime, "_engineer_autohunt", completed_autohunt)
        result = client.post(
            "/api/chat",
            headers={"Authorization": f"Bearer {configured.api_token}"},
            json={
                "message": "скомпилируй этот Java-файл",
                "mode": "engineer",
                "enable_tools": True,
                "source_ref": "api-document:compile-atomic",
                "document": {
                    "filename": "Main.java",
                    "mime_type": "text/x-java-source",
                    "content_base64": base64.b64encode(source).decode("ascii"),
                    "source_ref": "api-document:compile-atomic",
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
    assert "Java 21" in result.json()["message"]
    assert result.json()["files"][0]["filename"] == "Main.compiled.jar"
    assistant = next(row for row in reversed(rows) if row.get("role") == "assistant")
    metadata = json.loads(str(assistant.get("metadata_json") or "{}"))
    assert metadata["structural"]["verdict_kind"] == "engineer_artifact_compile"
    assert metadata["tools_used"] == ["engineer_compile_java"]
    assert metadata["accepted_engineer_compile_outcome"]["outcome"]["status"] == "succeeded"
    assert len(generated_rows) == 1
    assert metadata["generated_files"][0]["id"] == result.json()["files"][0]["id"]


@pytest.mark.parametrize("failure", ("message_binding", "batch_identity"))
def test_compile_persistence_failure_rolls_back_reply_receipt_and_file(
    settings,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import friday.agent_runtime as runtime_module
    from friday import generated_files
    from friday.server import create_app

    source = b"public class Main {}\n"
    configured = replace(settings, engineer_mode_enabled=True, verify_answers=False)
    app = create_app(configured)

    async def completed_autohunt(_message, attachments, **_kwargs):  # noqa: ANN001
        raw_id = str(attachments[0].get("raw_object_id") or "")
        binding, reason = runtime._prepare_engineer_compile_source(  # noqa: SLF001
            _kwargs["actor"],
            [raw_id],
            requested_filename=None,
        )
        assert reason == "none" and binding is not None
        dossier = _complete_dossier(source, binding=binding)
        dossier["artifacts"] = [{"ok": True, "raw_id": raw_id}]
        dossier["_artifact_refs"] = {"artifact_1": raw_id}
        return dossier

    if failure == "message_binding":
        monkeypatch.setattr(
            generated_files,
            "_attach_descriptors_to_message",
            lambda *_args, **_kwargs: False,
        )
    else:
        persist = runtime_module.persist_generated_response_files

        def changed_persistence(*args, **kwargs):  # noqa: ANN002, ANN003
            projection = persist(*args, **kwargs)
            projection["files"][0]["mime_type"] = "application/octet-stream"
            return projection

        monkeypatch.setattr(runtime_module, "persist_generated_response_files", changed_persistence)
    with TestClient(app, raise_server_exceptions=False) as client:
        runtime = getattr(app.state.agent, "_legacy", app.state.agent)
        monkeypatch.setattr(runtime, "_engineer_autohunt", completed_autohunt)
        failed = client.post(
            "/api/chat",
            headers={"Authorization": f"Bearer {configured.api_token}"},
            json={
                "message": "скомпилируй этот Java-файл",
                "mode": "engineer",
                "enable_tools": True,
                "source_ref": f"api-document:compile-persist-failure:{failure}",
                "document": {
                    "filename": "Main.java",
                    "mime_type": "text/x-java-source",
                    "content_base64": base64.b64encode(source).decode("ascii"),
                    "source_ref": f"api-document:compile-persist-failure:{failure}",
                },
            },
        )
        generated_rows = app.state.storage.execute(
            "SELECT id FROM raw_objects WHERE content_type='generated_file'"
        ).fetchall()
        durable_success = app.state.storage.execute(
            "SELECT id FROM messages WHERE role='assistant' AND content LIKE '%Java 21%'"
        ).fetchall()

    assert failed.status_code == 500
    assert generated_rows == []
    assert durable_success == []
    assert not list(configured.files_dir.glob("*/generated/*/*.blob"))


@pytest.mark.parametrize(
    "capability",
    (
        "files.read",
        "engineer.use",
        "engineer.artifact.build",
        "owner_role",
        "source_row",
        "source_filename",
        "jar_duplicate",
    ),
)
def test_authority_revoked_after_compile_suppresses_jar_and_success_receipt(
    settings,
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
) -> None:
    from friday.server import create_app

    source = b"public class Main {}\n"
    configured = replace(settings, engineer_mode_enabled=True, verify_answers=False)
    app = create_app(configured)

    async def revoke_after_compile(_message, attachments, **_kwargs):  # noqa: ANN001
        raw_id = str(attachments[0].get("raw_object_id") or "")
        binding, reason = runtime._prepare_engineer_compile_source(  # noqa: SLF001
            _kwargs["actor"],
            [raw_id],
            requested_filename=None,
        )
        assert reason == "none" and binding is not None
        dossier = _complete_dossier(source, binding=binding)
        dossier["artifacts"] = [{"ok": True, "raw_id": raw_id}]
        dossier["_artifact_refs"] = {"artifact_1": raw_id}
        if capability == "source_filename":
            with app.state.storage.transaction() as conn:
                conn.execute(
                    "UPDATE raw_objects SET metadata_json=json_set(metadata_json, '$.filename', 'Other.java') "
                    "WHERE id=?",
                    (raw_id,),
                )
        elif capability == "source_row":
            with app.state.storage.transaction() as conn:
                conn.execute(
                    "UPDATE raw_objects SET deleted_at='2026-08-26T12:00:00Z' WHERE id=?",
                    (raw_id,),
                )
        elif capability == "owner_role":
            with app.state.storage.transaction() as conn:
                conn.execute(
                    "UPDATE users SET preset_key='user' WHERE id=?",
                    (LEGACY_OWNER_USER_ID,),
                )
        elif capability != "jar_duplicate":
            app.state.storage.set_permission_override(LEGACY_OWNER_USER_ID, capability, "deny")
        return dossier

    with TestClient(app) as client:
        runtime = getattr(app.state.agent, "_legacy", app.state.agent)
        if capability == "jar_duplicate":
            authorize_compile = runtime._engineer_compile_publication_authorized  # noqa: SLF001

            def duplicate_jar(*args, response_files, **kwargs):  # noqa: ANN002, ANN003
                files = list(response_files)
                files.append(dict(files[0]))
                return authorize_compile(*args, response_files=files, **kwargs)

            monkeypatch.setattr(
                runtime,
                "_engineer_compile_publication_authorized",
                duplicate_jar,
            )
        monkeypatch.setattr(runtime, "_engineer_autohunt", revoke_after_compile)
        result = client.post(
            "/api/chat",
            headers={"Authorization": f"Bearer {configured.api_token}"},
            json={
                "message": "скомпилируй этот Java-файл",
                "mode": "engineer",
                "enable_tools": True,
                "source_ref": f"api-document:compile-late-deny:{capability}",
                "document": {
                    "filename": "Main.java",
                    "mime_type": "text/x-java-source",
                    "content_base64": base64.b64encode(source).decode("ascii"),
                    "source_ref": f"api-document:compile-late-deny:{capability}",
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
    assert result.json()["compile_authority_changed_before_publication"] is True
    if capability == "files.read":
        assert result.json()["attachment_authority_changed_before_publication"] is True
    assert result.json()["files"] == []
    assert generated_rows == []
    assistant = next(row for row in reversed(rows) if row.get("role") == "assistant")
    metadata = json.loads(str(assistant.get("metadata_json") or "{}"))
    assert "accepted_engineer_compile_outcome" not in metadata
    assert "generated_files" not in metadata
