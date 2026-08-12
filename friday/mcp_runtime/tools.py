"""Fixed Friday tool wrappers for the local filesystem MCP exchange."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any

from friday.execution_kernel import ToolSpec
from friday.mcp_runtime.client import MCPClientManager, MCPServerDefinition, MCPUnavailableError
from friday.mcp_runtime.workspace_fs import (
    WorkspaceFileDescriptor,
    WorkspacePathError,
    _relative_parts,
    read_workspace_file,
)

if TYPE_CHECKING:
    from friday.config import FridaySettings
    from friday.execution_kernel import ExecutionKernel
    from friday.ingestion import IngestionPipeline
    from friday.permissions import ActorContext

_SERVER_ALIAS = "workspace"
_SERVER_TOOLS = frozenset({"exchange_list", "exchange_search", "exchange_resolve", "exchange_create"})
_MAX_TOOL_ROWS = 200
_MAX_TEXT_CREATE_BYTES = 256 * 1024
_TOOL_RESULT_CHAR_BUDGET = 8_000
_TEXT_OUTPUT_SUFFIXES = frozenset(
    {".csv", ".htm", ".html", ".json", ".log", ".md", ".tsv", ".txt", ".xml", ".yaml", ".yml"}
)


def _require_workspace_actor(actor: ActorContext) -> None:
    # The exchange is installation-global, not shared-tenant data.  A copied
    # `owner` preset is deliberately not ownership in shared-archive mode.
    if not (actor.is_owner or actor.preset_key == "admin"):
        raise PermissionError("workspace exchange is restricted to the operator")


def workspace_server_definition(settings: FridaySettings) -> MCPServerDefinition:
    inbox = settings.mcp_workspace_inbox_dir
    outbox = settings.mcp_workspace_outbox_dir
    if inbox is None or outbox is None:
        raise ValueError("MCP workspace directories are not configured")
    # Keep the venv launcher symlink: resolving it selects the system Python,
    # where the pinned MCP dependency is intentionally not installed.
    executable = Path(sys.executable).absolute()
    # The backend may run from an immutable release selected through
    # PYTHONPATH while the venv itself is an editable checkout.  Pass the
    # code-owned root of the module which built this definition so the MCP
    # child imports that exact release instead of drifting to the checkout.
    release_root = Path(__file__).resolve(strict=True).parents[2]
    return MCPServerDefinition(
        alias=_SERVER_ALIAS,
        command=str(executable),
        args=(
            "-P",
            "-m",
            "friday.mcp_runtime.workspace_fs",
            "--inbox",
            str(inbox),
            "--outbox",
            str(outbox),
            "--max-output-bytes",
            str(settings.max_upload_bytes),
        ),
        allowed_tools=_SERVER_TOOLS,
        cwd=settings.home,
        environment={"PYTHONPATH": str(release_root)},
        startup_timeout_sec=settings.mcp_startup_timeout_sec,
        call_timeout_sec=settings.mcp_call_timeout_sec,
    )


def _strict_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise MCPUnavailableError(f"invalid MCP {field}")
    return value


def _strict_int(value: Any, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MCPUnavailableError(f"invalid MCP {field}")
    return value


def _project_listing(payload: Mapping[str, Any], *, requested_cursor: int) -> dict[str, Any]:
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list) or len(raw_entries) > _MAX_TOOL_ROWS:
        raise MCPUnavailableError("invalid MCP listing")
    if _strict_int(payload.get("returned"), field="returned count") != len(raw_entries):
        raise MCPUnavailableError("invalid MCP listing count")
    canonical_entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise MCPUnavailableError("invalid MCP listing row")
        path = str(raw.get("path") or "")
        parts = _relative_parts(path, allow_empty=False)
        name = str(raw.get("name") or "")
        if name != parts[-1]:
            raise MCPUnavailableError("invalid MCP listing identity")
        entry_type = str(raw.get("type") or "")
        if entry_type not in {"file", "directory"}:
            raise MCPUnavailableError("invalid MCP listing type")
        canonical_entries.append(
            {
                "path": PurePath(*parts).as_posix(),
                "name": name,
                "type": entry_type,
                "size_bytes": _strict_int(raw.get("size_bytes"), field="size"),
                "modified_ns": _strict_int(raw.get("modified_ns"), field="mtime"),
            }
        )
    next_cursor_raw = payload.get("next_cursor")
    next_cursor = None if next_cursor_raw is None else _strict_int(next_cursor_raw, field="next cursor")
    server_complete = _strict_bool(payload.get("complete"), field="completeness")
    scan_limit_reached = _strict_bool(payload.get("scan_limit_reached"), field="scan limit")
    snapshot_sha256 = str(payload.get("snapshot_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256):
        raise MCPUnavailableError("invalid MCP listing snapshot")
    matched_at_least = _strict_int(payload.get("matched_at_least"), field="match count")
    if matched_at_least < requested_cursor + len(canonical_entries):
        raise MCPUnavailableError("invalid MCP listing match count")
    if server_complete:
        if next_cursor is not None or scan_limit_reached:
            raise MCPUnavailableError("invalid MCP listing completion")
    elif next_cursor is None or next_cursor != requested_cursor + len(canonical_entries):
        raise MCPUnavailableError("invalid MCP listing continuation")
    base: dict[str, Any] = {
        "scope": "workspace_inbox",
        "matched_at_least": matched_at_least,
        "scan_limit_reached": scan_limit_reached,
        "snapshot_sha256": snapshot_sha256,
    }
    entries: list[dict[str, Any]] = []
    for row in canonical_entries:
        candidate = {
            **base,
            "entries": [*entries, row],
            "returned": len(entries) + 1,
            "complete": False,
            "projection_truncated": True,
            "next_cursor": requested_cursor + len(entries) + 1,
        }
        if len(json.dumps(candidate, ensure_ascii=False, indent=2)) > _TOOL_RESULT_CHAR_BUDGET:
            break
        entries.append(row)
    projection_truncated = len(entries) < len(canonical_entries)
    return {
        **base,
        "entries": entries,
        "returned": len(entries),
        "complete": server_complete and not projection_truncated,
        "projection_truncated": projection_truncated,
        "next_cursor": requested_cursor + len(entries) if projection_truncated else next_cursor,
    }


def _project_read_result(
    metadata: Mapping[str, Any],
    *,
    source: str,
    start: int,
    max_source_chars: int,
) -> dict[str, Any]:
    """Keep workspace text and its continuation metadata in one valid JSON result."""

    upper = min(len(source), start + max_source_chars)

    def candidate(end: int) -> dict[str, Any]:
        return {
            **metadata,
            "text": source[start:end],
            "offset": start,
            "next_offset": end if end < len(source) else None,
            "text_chars": len(source),
            "projection_complete": end >= len(source),
        }

    empty = candidate(start)
    if len(json.dumps(empty, ensure_ascii=False, indent=2)) > _TOOL_RESULT_CHAR_BUDGET:
        raise MCPUnavailableError("workspace read metadata is too large")
    low = start
    high = upper
    while low < high:
        middle = (low + high + 1) // 2
        projected = candidate(middle)
        if len(json.dumps(projected, ensure_ascii=False, indent=2)) <= _TOOL_RESULT_CHAR_BUDGET:
            low = middle
        else:
            high = middle - 1
    return candidate(low)


def _descriptor(payload: Mapping[str, Any]) -> WorkspaceFileDescriptor:
    relative_path = str(payload.get("relative_path") or "")
    parts = _relative_parts(relative_path, allow_empty=False)
    filename = str(payload.get("filename") or "")
    if filename != parts[-1]:
        raise MCPUnavailableError("invalid MCP file identity")
    return WorkspaceFileDescriptor(
        relative_path=PurePath(*parts).as_posix(),
        filename=filename,
        size_bytes=_strict_int(payload.get("size_bytes"), field="file size"),
        modified_ns=_strict_int(payload.get("modified_ns"), field="file mtime"),
        changed_ns=_strict_int(payload.get("changed_ns"), field="file ctime"),
        device=_strict_int(payload.get("device"), field="file device"),
        inode=_strict_int(payload.get("inode"), field="file inode"),
    )


def _source_was_incomplete(transient: Mapping[str, Any]) -> bool:
    return any(
        bool(transient.get(field))
        for field in (
            "_runtime_source_truncated",
            "parse_deadline_reached",
            "parse_pages_truncated",
            "archive_truncated",
            "source_truncated_for_parse",
        )
    )


def _loss_projection(transient: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_truncated": bool(transient.get("_runtime_source_truncated")),
        "parse_deadline_reached": bool(transient.get("parse_deadline_reached")),
        "parse_pages_read": max(0, int(transient.get("parse_pages_read") or 0)),
        "parse_total_pages": max(0, int(transient.get("parse_total_pages") or 0)),
        "parse_pages_truncated": bool(transient.get("parse_pages_truncated")),
        "archive_truncated": bool(transient.get("archive_truncated")),
        "source_truncated_for_parse": bool(transient.get("source_truncated_for_parse")),
    }


def bind_workspace_mcp_tools(
    kernel: ExecutionKernel,
    manager: MCPClientManager,
    ingestion: IngestionPipeline,
    settings: FridaySettings,
) -> bool:
    """Register only the fixed wrappers when the configured server is healthy."""

    if not manager.is_available(_SERVER_ALIAS):
        return False
    inbox = settings.mcp_workspace_inbox_dir
    if inbox is None:
        return False

    async def workspace_list(
        *,
        actor: ActorContext,
        relative_dir: str = "",
        recursive: bool = False,
        cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        _require_workspace_actor(actor)
        result = await manager.call_tool(
            _SERVER_ALIAS,
            "exchange_list",
            {
                "relative_dir": relative_dir,
                "recursive": recursive,
                "cursor": cursor,
                "limit": limit,
            },
        )
        return _project_listing(result, requested_cursor=cursor)

    async def workspace_search(
        *,
        actor: ActorContext,
        query: str,
        relative_dir: str = "",
        cursor: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        _require_workspace_actor(actor)
        result = await manager.call_tool(
            _SERVER_ALIAS,
            "exchange_search",
            {
                "query": query,
                "relative_dir": relative_dir,
                "cursor": cursor,
                "limit": limit,
            },
        )
        return _project_listing(result, requested_cursor=cursor)

    async def workspace_read(
        *,
        actor: ActorContext,
        relative_path: str,
        offset: int = 0,
    ) -> dict[str, Any]:
        _require_workspace_actor(actor)
        raw_descriptor = await manager.call_tool(
            _SERVER_ALIAS,
            "exchange_resolve",
            {"relative_path": relative_path},
        )
        descriptor = _descriptor(raw_descriptor)
        try:
            content = await asyncio.to_thread(
                read_workspace_file,
                inbox,
                descriptor,
                max_bytes=settings.max_upload_bytes,
            )
        except WorkspacePathError as exc:
            raise MCPUnavailableError("workspace file could not be revalidated") from exc
        transient = await ingestion.inspect_file_transient(
            content,
            filename=descriptor.filename,
            preview_chars=min(48_000, settings.mcp_result_chars),
        )
        source = str(transient.get("_runtime_source_text") or "")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= len(source):
            raise ValueError("workspace read offset is outside the extracted text")
        start = offset
        extraction_success = bool(transient.get("extraction_success"))
        source_complete = extraction_success and not _source_was_incomplete(transient)
        return _project_read_result(
            {
                "scope": "workspace_inbox",
                "path": descriptor.relative_path,
                "filename": descriptor.filename,
                "mime_type": str(transient.get("mime_type") or "application/octet-stream")[:128],
                "size_bytes": descriptor.size_bytes,
                "sha256": hashlib.sha256(content).hexdigest(),
                # Every continuation reparses the registered bytes.  Pin the
                # resulting canonical source as well as the file bytes so a
                # caller cannot assemble pages produced by different OCR or
                # parser outcomes for the same immutable file.
                "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                "readable": bool(extraction_success and source.strip()),
                "source_complete": source_complete,
                "advisory_only": bool(transient.get("advisory_only")),
                "verification_eligible": bool(transient.get("verification_eligible")),
                "unsupported_format": bool(transient.get("unsupported_format")),
                "extraction_status": ("readable" if extraction_success and source.strip() else "unreadable"),
                **_loss_projection(transient),
            },
            source=source,
            start=start,
            max_source_chars=settings.mcp_result_chars,
        )

    async def workspace_create(
        *,
        actor: ActorContext,
        filename: str,
        content: str,
    ) -> dict[str, Any]:
        _require_workspace_actor(actor)
        if not isinstance(filename, str) or not isinstance(content, str):
            raise ValueError("workspace_create requires text arguments")
        parts = _relative_parts(filename, allow_empty=False)
        if len(parts) != 1 or parts[0] != filename:
            raise ValueError("workspace_create accepts a filename, not a path")
        if PurePath(filename).suffix.casefold() not in _TEXT_OUTPUT_SUFFIXES:
            raise ValueError("workspace_create accepts a safe text-file suffix")
        encoded_content = content.encode("utf-8")
        if len(encoded_content) > min(settings.max_upload_bytes, _MAX_TEXT_CREATE_BYTES):
            raise ValueError("workspace text output is too large")
        result = await manager.call_tool(
            _SERVER_ALIAS,
            "exchange_create",
            {
                "filename": filename,
                "content_base64": base64.b64encode(encoded_content).decode("ascii"),
            },
        )
        if result.get("created") is not True:
            raise MCPUnavailableError("MCP output was not confirmed")
        result_filename = str(result.get("filename") or "")
        parts = _relative_parts(result_filename, allow_empty=False)
        size_bytes = _strict_int(result.get("size_bytes"), field="created size")
        digest = str(result.get("sha256") or "")
        expected_digest = hashlib.sha256(encoded_content).hexdigest()
        if (
            len(parts) != 1
            or parts[0] != filename
            or size_bytes != len(encoded_content)
            or digest != expected_digest
        ):
            raise MCPUnavailableError("MCP output postcondition failed")
        return {
            "scope": "workspace_outbox",
            "created": True,
            "filename": result_filename,
            "size_bytes": size_bytes,
            "sha256": digest,
            "overwrite": False,
        }

    common_directory = {
        "relative_dir": {"type": "string", "maxLength": 1024},
        "cursor": {"type": "integer", "minimum": 0, "maximum": 5000},
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_TOOL_ROWS},
    }
    kernel.register(
        ToolSpec(
            name="workspace_list",
            description=(
                "Показать файлы во внешнем read-only inbox, который владелец явно "
                "подключил к Пятнице. complete=false означает неполную страницу."
            ),
            security_id="mcp.files.read",
            risk="observe",
            parameters={
                "type": "object",
                "properties": {
                    **common_directory,
                    "recursive": {"type": "boolean"},
                },
                "additionalProperties": False,
            },
            handler=workspace_list,
        )
    )
    kernel.register(
        ToolSpec(
            name="workspace_search",
            description=(
                "Найти файл по части имени в подключённом read-only inbox. "
                "Это поиск имён, не утверждение об отсутствии текста внутри документов."
            ),
            security_id="mcp.files.read",
            risk="observe",
            parameters={
                "type": "object",
                "properties": {
                    **common_directory,
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=workspace_search,
        )
    )
    kernel.register(
        ToolSpec(
            name="workspace_read",
            description=(
                "Без сохранения разобрать один файл из подключённого inbox. Для длинного "
                "текста продолжай с next_offset; source_complete=false запрещает вывод о всём файле."
            ),
            security_id="mcp.files.read",
            risk="observe",
            parameters={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": settings.max_extracted_text_chars,
                    },
                },
                "required": ["relative_path"],
                "additionalProperties": False,
            },
            handler=workspace_read,
            timeout_sec=min(
                settings.agent_turn_budget_sec,
                settings.mcp_call_timeout_sec + settings.pdf_parse_budget_sec + settings.llm_call_budget_sec,
            ),
        )
    )
    kernel.register(
        ToolSpec(
            name="workspace_create",
            description=(
                "Создать новый UTF-8 текстовый файл во внешнем outbox. Существующий файл "
                "никогда не перезаписывается; для DOCX/XLSX/PDF используй make_file."
            ),
            security_id="mcp.files.create",
            risk="mutate",
            parameters={
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "minLength": 1, "maxLength": 255},
                    "content": {"type": "string", "maxLength": _MAX_TEXT_CREATE_BYTES},
                },
                "required": ["filename", "content"],
                "additionalProperties": False,
            },
            handler=workspace_create,
        )
    )
    return True
