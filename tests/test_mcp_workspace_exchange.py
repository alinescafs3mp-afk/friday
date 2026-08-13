from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from friday.config import ensure_runtime_dirs, validate_settings
from friday.execution_kernel import ExecutionKernel, ToolResult
from friday.mcp_runtime import client as mcp_client_module
from friday.mcp_runtime.client import (
    MCPClientManager,
    MCPServerDefinition,
    MCPUnavailableError,
    _MCPConnection,
)
from friday.mcp_runtime.tools import (
    _project_listing,
    _project_read_result,
    bind_workspace_mcp_tools,
    workspace_server_definition,
)
from friday.mcp_runtime.workspace_fs import (
    WorkspacePathError,
    create_workspace_file,
    create_workspace_server,
    describe_workspace_file,
    list_workspace_entries,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.server import create_app


class _TextOnlyIngestion:
    calls = 0

    async def inspect_file_transient(
        self,
        file_content: bytes,
        *,
        filename: str = "",
        mime_type: str = "",
        preview_chars: int = 24_000,
    ) -> dict[str, object]:
        del mime_type, preview_chars
        self.calls += 1
        text = file_content.decode("utf-8")
        return {
            "filename": filename,
            "mime_type": "text/plain",
            "extraction_success": True,
            "_runtime_source_text": text,
            "_runtime_source_truncated": False,
            "advisory_only": False,
            "verification_eligible": True,
        }


def test_workspace_child_imports_the_active_release_not_its_cwd(settings, tmp_path: Path) -> None:
    configured = replace(
        settings,
        mcp_enabled=True,
        mcp_workspace_inbox_dir=tmp_path / "inbox",
        mcp_workspace_outbox_dir=tmp_path / "outbox",
    )
    definition = workspace_server_definition(configured)
    decoy = tmp_path / "friday"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("raise RuntimeError('cwd decoy imported')\n", encoding="utf-8")

    completed = subprocess.run(  # noqa: S603 - exact code-owned interpreter and argv
        [
            definition.command,
            "-P",
            "-c",
            "from pathlib import Path; import friday; print(Path(friday.__file__).resolve())",
        ],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), **definition.environment},
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    imported = Path(completed.stdout.strip())
    assert imported.is_relative_to(Path(definition.environment["PYTHONPATH"]))


def test_exchange_rejects_traversal_symlinks_hardlinks_and_overwrite(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    outside = tmp_path / "outside.txt"
    inbox.mkdir()
    outbox.mkdir()
    outside.write_text("PRIVATE", encoding="utf-8")
    (inbox / "link.txt").symlink_to(outside)
    os.link(outside, inbox / "hard.txt")

    for path in ("../outside.txt", "link.txt", "hard.txt"):
        with pytest.raises(WorkspacePathError):
            describe_workspace_file(inbox, path)

    first = create_workspace_file(
        outbox,
        filename="result.txt",
        content_base64="0J/RgNC40LLQtdGC",
        max_bytes=1024,
    )
    assert first["created"] is True
    original = (outbox / "result.txt").read_bytes()
    with pytest.raises(WorkspacePathError):
        create_workspace_file(
            outbox,
            filename="result.txt",
            content_base64="bmV3",
            max_bytes=1024,
        )
    assert (outbox / "result.txt").read_bytes() == original
    assert outside.read_text(encoding="utf-8") == "PRIVATE"


def test_listing_reports_a_scan_ceiling_instead_of_false_completeness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for index in range(4):
        (inbox / f"{index}.txt").write_text(str(index), encoding="utf-8")
    monkeypatch.setattr("friday.mcp_runtime.workspace_fs._MAX_SCAN_ENTRIES", 2)

    page = list_workspace_entries(inbox, recursive=True, limit=20)

    assert page["complete"] is False
    assert page["scan_limit_reached"] is True
    assert page["matched_at_least"] == 2


def test_long_listing_keeps_valid_json_and_continuation_metadata() -> None:
    rows = [
        {
            "path": f"folder/{index:03d}-{'x' * 180}.txt",
            "name": f"{index:03d}-{'x' * 180}.txt",
            "type": "file",
            "size_bytes": index,
            "modified_ns": index,
        }
        for index in range(200)
    ]
    projected = _project_listing(
        {
            "entries": rows,
            "returned": len(rows),
            "matched_at_least": 201,
            "complete": False,
            "scan_limit_reached": False,
            "snapshot_sha256": "a" * 64,
            "next_cursor": 200,
        },
        requested_cursor=0,
    )
    message = ToolResult("workspace_list", True, data=projected).to_llm_message()
    decoded = json.loads(message.split("\n", 1)[1])

    assert projected["projection_truncated"] is True
    assert projected["complete"] is False
    assert 0 < projected["returned"] < 200
    assert projected["next_cursor"] == projected["returned"]
    assert decoded["next_cursor"] == projected["returned"]
    assert "… (truncated)" not in message


def test_escaped_workspace_text_keeps_valid_json_and_continuation_metadata() -> None:
    projected = _project_read_result(
        {
            "scope": "workspace_inbox",
            "path": "escapes.txt",
            "filename": "escapes.txt",
            "source_complete": True,
        },
        source="\n" * 7_000,
        start=0,
        max_source_chars=7_000,
    )
    message = ToolResult("workspace_read", True, data=projected).to_llm_message()
    decoded = json.loads(message.split("\n", 1)[1])

    assert "… (truncated)" not in message
    assert decoded["source_complete"] is True
    assert decoded["projection_complete"] is False
    assert decoded["next_offset"] == len(decoded["text"]) > 0


def test_exchange_roots_cannot_overlap_or_redirect(tmp_path: Path, settings) -> None:
    inbox = tmp_path / "exchange"
    outbox = inbox / "outbox"
    inbox.mkdir()
    outbox.mkdir()
    with pytest.raises(WorkspacePathError):
        create_workspace_server(inbox, outbox, max_output_bytes=1024)

    protected = tmp_path / "protected"
    protected.mkdir()
    link = tmp_path / "redirected"
    link.symlink_to(protected, target_is_directory=True)
    configured = replace(
        settings,
        mcp_enabled=True,
        mcp_workspace_inbox_dir=link,
        mcp_workspace_outbox_dir=tmp_path / "safe-outbox",
    )
    errors = validate_settings(configured)
    assert any("symlink" in error for error in errors)
    before_mode = protected.stat().st_mode
    with pytest.raises(ValueError):
        ensure_runtime_dirs(configured)
    assert protected.stat().st_mode == before_mode


@pytest.mark.asyncio
async def test_fixed_stdio_tools_use_kernel_auth_and_exact_exchange_bytes(settings) -> None:
    configured = replace(
        settings,
        mcp_enabled=True,
        mcp_workspace_inbox_dir=settings.home / "mcp-exchange" / "inbox",
        mcp_workspace_outbox_dir=settings.home / "mcp-exchange" / "outbox",
        mcp_result_chars=1_000,
    )
    ensure_runtime_dirs(configured)
    inbox = configured.mcp_workspace_inbox_dir
    outbox = configured.mcp_workspace_outbox_dir
    assert inbox is not None and outbox is not None
    (inbox / "note.txt").write_text("ORION-42\n" * 200, encoding="utf-8")

    definition = workspace_server_definition(configured)
    assert definition.args[:2] == ("-P", "-m")
    assert definition.environment["PYTHONPATH"] == str(
        Path(workspace_server_definition.__code__.co_filename).resolve().parents[2]
    )
    manager = MCPClientManager([definition])
    await manager.start()
    try:
        assert manager.is_available("workspace")
        with pytest.raises(MCPUnavailableError):
            await manager.call_tool("workspace", "delete", {"path": "note.txt"})

        kernel = ExecutionKernel(AuthorizationService(), configured)
        ingestion = _TextOnlyIngestion()
        assert bind_workspace_mcp_tools(kernel, manager, ingestion, configured)
        owner = ActorContext(user_id="owner", preset_key="owner", source="test")
        copied_owner = ActorContext(
            user_id="tenant",
            preset_key="owner",
            source="test",
            shared_tenant=True,
            person_id="someone-else",
        )
        assert set(kernel.get_tool_names(owner)) >= {
            "workspace_list",
            "workspace_search",
            "workspace_read",
            "workspace_create",
        }
        assert not any(name.startswith("workspace_") for name in kernel.get_tool_names(copied_owner))

        listing = await kernel.execute("workspace_search", {"query": "note"}, actor=owner)
        assert listing.success is True
        assert listing.data["entries"][0]["path"] == "note.txt"
        reading = await kernel.execute(
            "workspace_read",
            {"relative_path": "note.txt", "offset": 0},
            actor=owner,
        )
        assert reading.success is True
        assert reading.data["text"].startswith("ORION-42")
        assert (
            reading.data["source_sha256"] == hashlib.sha256(("ORION-42\n" * 200).encode("utf-8")).hexdigest()
        )
        assert reading.data["projection_complete"] is False
        assert reading.data["next_offset"] == 1_000

        created = await kernel.execute(
            "workspace_create",
            {"filename": "answer.txt", "content": "Привет"},
            actor=owner,
        )
        assert created.success is True
        assert (outbox / "answer.txt").read_bytes() == "Привет".encode()
        denied = await kernel.execute("workspace_list", {}, actor=copied_owner)
        assert denied.success is False
        assert denied.error == "Authorization denied"
    finally:
        await manager.close()


def test_workspace_audit_fingerprints_content_and_paths() -> None:
    details = ExecutionKernel._audit_details(
        "workspace_create",
        {"filename": "private-name.txt", "content": "PRIVATE-CONTENT"},
    )
    encoded = repr(details)
    assert "private-name" not in encoded
    assert "PRIVATE-CONTENT" not in encoded
    assert details["path_suffix"] == ".txt"
    assert len(details["path_sha256"]) == 64
    assert len(details["content_sha256"]) == 64


@pytest.mark.asyncio
async def test_failed_mcp_cleanup_never_holds_the_connection_lock(monkeypatch) -> None:
    entered_cleanup = asyncio.Event()
    release_cleanup = asyncio.Event()

    class _FailingClient:
        async def call_tool(self, *_args, **_kwargs):
            raise RuntimeError("synthetic transport failure")

    class _HangingStack:
        async def aclose(self) -> None:
            entered_cleanup.set()
            await release_cleanup.wait()

    definition = MCPServerDefinition(
        alias="synthetic",
        command="/bin/true",
        args=(),
        allowed_tools=frozenset({"read"}),
        call_timeout_sec=1.0,
    )
    connection = _MCPConnection(definition)
    connection._client = _FailingClient()  # type: ignore[assignment]  # noqa: SLF001
    connection._stack = _HangingStack()  # type: ignore[assignment]  # noqa: SLF001
    connection.available = True
    monkeypatch.setattr(mcp_client_module, "_MCP_CLOSE_TIMEOUT_SEC", 0.5)

    with pytest.raises(RuntimeError, match="synthetic transport failure"):
        await connection.call("read", {})
    assert not entered_cleanup.is_set(), "request task violated MCP cleanup task affinity"

    with pytest.raises(MCPUnavailableError, match="unavailable"):
        await asyncio.wait_for(connection.call("read", {}), timeout=0.05)
    with pytest.raises(MCPUnavailableError, match="lifecycle restart"):
        await connection.connect()

    closing = asyncio.create_task(connection.close())
    await asyncio.wait_for(entered_cleanup.wait(), timeout=0.2)
    with pytest.raises(MCPUnavailableError, match="unavailable"):
        await asyncio.wait_for(connection.call("read", {}), timeout=0.05)
    release_cleanup.set()
    await asyncio.wait_for(closing, timeout=0.2)


def test_backend_lifecycle_starts_and_closes_workspace_child(settings) -> None:
    configured = replace(
        settings,
        mcp_enabled=True,
        mcp_workspace_inbox_dir=settings.home / "mcp-exchange" / "inbox",
        mcp_workspace_outbox_dir=settings.home / "mcp-exchange" / "outbox",
    )
    app = create_app(configured)
    with TestClient(app):
        manager = app.state.mcp
        assert manager is not None
        assert manager.is_available("workspace")
        assert set(app.state.kernel.get_tool_names(ActorContext("owner", "owner", "test"))) >= {
            "workspace_list",
            "workspace_read",
        }
    assert manager.is_available("workspace") is False
