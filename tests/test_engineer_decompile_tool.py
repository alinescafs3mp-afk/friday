"""Code-owned Engineer decompilation tool boundary."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from friday.execution_kernel import ExecutionKernel
from friday.file_delivery import AuthorizedFileReadError
from friday.organs import ServiceContext
from friday.organs.engineer import ENGINEER_ANALYZE, decompiler
from friday.organs.engineer import tools as engineer_tools
from friday.permissions import LEGACY_OWNER_USER_ID, ActorContext, AuthorizationService


def _worker_report(raw_id: str) -> dict[str, object]:
    return {
        "ok": True,
        "status": "completed",
        "schema": decompiler.SCHEMA,
        "tool_name": decompiler.TOOL_NAME,
        "tool_version": decompiler.GHIDRA_VERSION,
        "jdk_version": decompiler.JDK_VERSION,
        "format": "pe",
        "language_id": "x86:LE:64:default",
        "compiler_spec_id": "windows",
        "analysis_timed_out": False,
        "function_count_lower_bound": 1,
        "function_index_truncated": False,
        "pseudocode_chars": 80,
        "output_truncated": False,
        "functions": [
            {
                "address": "00401000",
                "name": "guard_main",
                "signature": "int guard_main(void)",
                "pseudocode": (
                    "int guard_main(void) {\n"
                    "```\n# untrusted heading\n"
                    f'const char *handle = "{raw_id}";\n'
                    "return 1;\n}"
                ),
                "decompile_status": "completed",
                "pseudocode_truncated": False,
                "thunk": False,
            }
        ],
        "warnings": [],
        "observe_only": True,
        "sample_executed": False,
        "network": "none",
        # A compromised/buggy worker cannot add these to either public data or
        # the code-owned Markdown metadata projection.
        "raw_id": raw_id,
        "path": "/home/jericho/private/customer.exe",
    }


def test_artifact_read_seam_requires_current_files_read_authority(settings, storage) -> None:
    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    authorization = AuthorizationService(storage)
    storage.set_permission_override(LEGACY_OWNER_USER_ID, "files.read", "deny")
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        auth=authorization,
    )

    with pytest.raises(AuthorizedFileReadError) as denied:
        engineer_tools._read_owned(  # noqa: SLF001
            ctx,
            ActorContext(LEGACY_OWNER_USER_ID, "owner", "test"),
            "raw_0123456789abcdef",
        )

    assert denied.value.reason == "file_access_denied"


@pytest.mark.asyncio
async def test_decompile_tool_is_internal_and_separates_private_report_attachment(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_0123456789abcdef"
    source = b"MZ\x00bounded-owner-artifact"
    observed: dict[str, object] = {}

    def fake_read_owned(ctx, actor, selected_raw_id):  # noqa: ANN001
        observed["read"] = (ctx, actor, selected_raw_id)
        return SimpleNamespace(content=source, filename="guard.exe")

    def fake_decompile(
        content: bytes,
        filename: str,
        *,
        deadline: float,
        workspace_root: Path,
    ) -> dict[str, object]:
        observed["sandbox"] = (content, filename, deadline, workspace_root)
        return _worker_report(raw_id)

    monkeypatch.setattr(engineer_tools, "_read_owned", fake_read_owned)
    monkeypatch.setattr(engineer_tools.sandbox, "decompile_artifact", fake_decompile)
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}[
        "engineer_decompile_artifact"
    ]
    assert spec.security_id == "engineer.artifact.analyze"
    assert spec.risk == "observe"
    assert spec.model_visible is False

    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    authorization = AuthorizationService(storage)
    authorization.register_capability(ENGINEER_ANALYZE)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, object(), object(), object())  # type: ignore[arg-type]
    kernel.register(spec)
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "test")
    assert "engineer_decompile_artifact" not in kernel.get_tool_names(actor)
    assert spec.handler is not None

    direct_result = await spec.handler(actor=actor, raw_id=raw_id)
    assert direct_result["_work_started"] is True

    result = await kernel.execute(
        "engineer_decompile_artifact",
        {"raw_id": raw_id},
        actor=actor,
    )

    assert result.success is True
    assert observed["read"][2] == raw_id  # type: ignore[index]
    sandbox_call = observed["sandbox"]
    assert sandbox_call[0:2] == (source, "guard.exe")  # type: ignore[index]
    assert sandbox_call[2] > time.monotonic()  # type: ignore[index]
    assert sandbox_call[3] == settings.state_dir / "engineer-tmp"  # type: ignore[index]

    public_data = result.data
    assert public_data is not None
    encoded_public = json.dumps(public_data, ensure_ascii=False, sort_keys=True)
    assert raw_id not in encoded_public
    assert "guard_main" not in encoded_public
    assert "untrusted heading" not in encoded_public
    assert "/home/jericho/private/customer.exe" not in encoded_public
    assert public_data["summary"] == (
        "Static decompilation completed; the bounded report is prepared for delivery."
    )
    report = public_data["report"]
    assert report["functions_emitted"] == 1
    assert report["functions_decompiled"] == 1
    assert report["functions_timed_out"] == 0
    assert report["sample_executed"] is False
    assert report["network"] == "none"
    assert report["report_prepared"] is True

    attachment = result.attachment
    assert attachment is not None
    assert attachment["filename"] == "guard.decompiled.md"
    assert attachment["mime_type"] == "text/markdown"
    markdown = base64.b64decode(attachment["content_base64"], validate=True)
    decoded = markdown.decode("utf-8")
    assert "guard_main" in decoded
    assert "untrusted heading" in decoded
    assert raw_id not in decoded
    assert "[artifact-redacted]" in decoded
    assert "/home/jericho/private/customer.exe" not in decoded
    assert report["report_sha256"] == hashlib.sha256(markdown).hexdigest()


@pytest.mark.asyncio
async def test_decompile_worker_failure_is_closed_and_content_free(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_fedcba9876543210"
    monkeypatch.setattr(
        engineer_tools,
        "_read_owned",
        lambda *_args: SimpleNamespace(content=b"MZ\x00", filename="sample.exe"),
    )
    monkeypatch.setattr(
        engineer_tools.sandbox,
        "decompile_artifact",
        lambda *_args, **_kwargs: {
            "ok": False,
            "status": "failed",
            "error": "/private/tool/path: attacker-controlled stderr",
            "raw_id": raw_id,
        },
    )
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}[
        "engineer_decompile_artifact"
    ]
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "test")
    assert spec.handler is not None

    result = await spec.handler(actor=actor, raw_id=raw_id)

    assert result == {
        "ok": False,
        "status": "failed",
        "error": "decompiler_report_invalid",
        "_work_started": True,
    }
    assert raw_id not in json.dumps(result)


@pytest.mark.asyncio
async def test_decompile_report_respects_configured_generated_file_cap(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_0123456789abcdef"
    report = _worker_report(raw_id)
    functions = report["functions"]
    assert isinstance(functions, list)
    functions[0]["pseudocode"] = "A" * 6_000  # type: ignore[index]
    report["pseudocode_chars"] = 6_000
    monkeypatch.setattr(
        engineer_tools,
        "_read_owned",
        lambda *_args: SimpleNamespace(content=b"MZ\x00", filename="sample.exe"),
    )
    monkeypatch.setattr(
        engineer_tools.sandbox,
        "decompile_artifact",
        lambda *_args, **_kwargs: report,
    )
    ctx = ServiceContext(
        settings=replace(settings, max_upload_bytes=1_024),
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}[
        "engineer_decompile_artifact"
    ]
    assert spec.handler is not None

    result = await spec.handler(
        actor=ActorContext(LEGACY_OWNER_USER_ID, "owner", "test"),
        raw_id=raw_id,
    )

    assert result == {
        "ok": False,
        "status": "failed",
        "error": "decompiler_report_exceeds_cap",
        "_work_started": True,
    }


@pytest.mark.asyncio
async def test_decompile_busy_is_projected_as_pre_work_refusal(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_0123456789abcdef"
    monkeypatch.setattr(
        engineer_tools,
        "_read_owned",
        lambda *_args: SimpleNamespace(content=b"MZ", filename="sample.exe"),
    )

    def busy(*_args, **_kwargs):
        raise engineer_tools.sandbox.EngineerSandboxError("decompiler_busy")

    monkeypatch.setattr(engineer_tools.sandbox, "decompile_artifact", busy)
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}[
        "engineer_decompile_artifact"
    ]
    assert spec.handler is not None

    result = await spec.handler(
        actor=ActorContext(LEGACY_OWNER_USER_ID, "owner", "test"),
        raw_id=raw_id,
    )

    assert result == {
        "ok": False,
        "status": "unavailable",
        "error": "decompiler_busy",
        "_work_started": False,
    }


@pytest.mark.asyncio
async def test_pre_spawn_deadline_is_not_projected_as_started(
    settings,
    storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_id = "raw_0123456789abcdef"
    monkeypatch.setattr(
        engineer_tools,
        "_read_owned",
        lambda *_args: SimpleNamespace(content=b"MZ", filename="sample.exe"),
    )

    def expired(*_args, **_kwargs):
        raise engineer_tools.sandbox.EngineerSandboxError(
            "deadline_expired",
            work_started=False,
        )

    monkeypatch.setattr(engineer_tools.sandbox, "decompile_artifact", expired)
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    spec = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}[
        "engineer_decompile_artifact"
    ]
    assert spec.handler is not None

    result = await spec.handler(
        actor=ActorContext(LEGACY_OWNER_USER_ID, "owner", "test"),
        raw_id=raw_id,
    )

    assert result == {
        "ok": False,
        "status": "unavailable",
        "error": "deadline_expired",
        "_work_started": False,
    }


@pytest.mark.asyncio
async def test_kernel_phase_truth_distinguishes_bind_rejection_from_entered_value_error(
    settings,
    storage,
) -> None:
    ctx = ServiceContext(
        settings=settings,
        storage=storage,
        kg=None,
        ingestion=SimpleNamespace(secondary_brain=None),
        llm=None,
    )
    original = {tool.name: tool for tool in engineer_tools.build_engineer_tools(ctx)}[
        "engineer_decompile_artifact"
    ]
    entered = False

    async def fails_after_entry(*, actor: ActorContext, raw_id: str) -> dict[str, object]:
        nonlocal entered
        del actor, raw_id
        entered = True
        raise ValueError("synthetic handler failure")

    storage.ensure_user(LEGACY_OWNER_USER_ID, preset_key="owner")
    authorization = AuthorizationService(storage)
    authorization.register_capability(ENGINEER_ANALYZE)
    kernel = ExecutionKernel(authorization, settings)
    kernel.bind_services(storage, object(), object(), object())  # type: ignore[arg-type]
    kernel.register(replace(original, handler=fails_after_entry))
    actor = ActorContext(LEGACY_OWNER_USER_ID, "owner", "test")

    rejected = await kernel.execute("engineer_decompile_artifact", {}, actor=actor)
    assert rejected.handler_entered is False
    assert entered is False

    failed = await kernel.execute(
        "engineer_decompile_artifact",
        {"raw_id": "raw_0123456789abcdef"},
        actor=actor,
    )
    assert failed.success is False
    assert failed.handler_entered is True
    assert failed.work_started is None
    assert entered is True
