"""Bounded MCP client connections for Friday.

Only code-owned server definitions may be connected.  Discovery proves that the
small allowlist exists; remote descriptions, annotations and schemas are never
published to the model.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters, stdio_client

LOGGER = logging.getLogger(__name__)


class MCPUnavailableError(RuntimeError):
    """A configured connector cannot return a trustworthy bounded result."""


@dataclass(frozen=True)
class MCPServerDefinition:
    alias: str
    command: str
    args: tuple[str, ...]
    allowed_tools: frozenset[str]
    cwd: Path | None = None
    environment: dict[str, str] = field(default_factory=dict)
    startup_timeout_sec: float = 15.0
    call_timeout_sec: float = 20.0

    def __post_init__(self) -> None:
        if not self.alias or not self.alias.replace("_", "").isalnum():
            raise ValueError("MCP server alias must be alphanumeric with optional underscores")
        if not Path(self.command).is_absolute():
            raise ValueError("MCP stdio command must be an absolute path")
        if not self.allowed_tools:
            raise ValueError("MCP server must have a non-empty tool allowlist")
        if self.startup_timeout_sec <= 0 or self.call_timeout_sec <= 0:
            raise ValueError("MCP timeouts must be positive")


class _MCPConnection:
    def __init__(self, definition: MCPServerDefinition) -> None:
        self.definition = definition
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._lock = asyncio.Lock()
        self.available = False

    @staticmethod
    def _safe_environment(extra: dict[str, str]) -> dict[str, str]:
        allowed: dict[str, str] = {}
        for name in ("HOME", "LANG", "LC_ALL", "LOGNAME", "PATH", "SHELL", "TERM", "USER"):
            value = os.environ.get(name)
            if value:
                allowed[name] = value
        for name, value in extra.items():
            if not name or "\x00" in name or "\x00" in value:
                raise ValueError("invalid MCP subprocess environment")
            allowed[name] = value
        return allowed

    async def connect(self) -> None:
        async with self._lock:
            if self._client is not None:
                return
            stack = AsyncExitStack()
            try:
                # MCP owns the handle for exactly this AsyncExitStack lifetime.
                # Suppressing server stderr avoids reflecting filenames or parser
                # errors from a private exchange into the durable service journal.
                errlog = stack.enter_context(
                    os.fdopen(os.open(os.devnull, os.O_WRONLY), "w", encoding="utf-8")
                )
                parameters = StdioServerParameters(
                    command=self.definition.command,
                    args=list(self.definition.args),
                    env=self._safe_environment(self.definition.environment),
                    cwd=self.definition.cwd,
                )
                transport = stdio_client(parameters, errlog=errlog)
                async with asyncio.timeout(self.definition.startup_timeout_sec):
                    client = await stack.enter_async_context(
                        Client(
                            transport,
                            mode="auto",
                            read_timeout_seconds=self.definition.call_timeout_sec,
                        )
                    )
                    discovered: set[str] = set()
                    cursor: str | None = None
                    for _ in range(10):
                        page = await client.list_tools(cursor=cursor, cache_mode="reload")
                        discovered.update(str(tool.name) for tool in page.tools)
                        cursor = page.next_cursor
                        if not cursor:
                            break
                    else:
                        raise MCPUnavailableError("MCP tool discovery exceeded its page limit")
                missing = self.definition.allowed_tools - discovered
                if missing:
                    raise MCPUnavailableError("MCP server is missing required allowlisted tools")
                self._stack = stack
                self._client = client
                self.available = True
                LOGGER.info(
                    "MCP server %s connected (%d allowlisted tools)",
                    self.definition.alias,
                    len(self.definition.allowed_tools),
                )
            except BaseException:
                with suppress(BaseException):
                    await stack.aclose()
                self.available = False
                raise

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in self.definition.allowed_tools:
            raise MCPUnavailableError("MCP tool is not allowlisted")
        failed_stack: AsyncExitStack | None = None
        async with self._lock:
            client = self._client
            if client is None or not self.available:
                raise MCPUnavailableError("MCP server is unavailable")
            try:
                async with asyncio.timeout(self.definition.call_timeout_sec):
                    result = await client.call_tool(
                        tool_name,
                        arguments,
                        read_timeout_seconds=self.definition.call_timeout_sec,
                    )
            except BaseException:
                self.available = False
                self._client = None
                failed_stack = self._stack
                self._stack = None
                if failed_stack is not None:
                    with suppress(BaseException):
                        await failed_stack.aclose()
                raise
            if result.is_error:
                raise MCPUnavailableError("MCP tool returned an error")
            structured = result.structured_content
            if not isinstance(structured, dict):
                raise MCPUnavailableError("MCP tool returned no structured result")
            return dict(structured)

    async def close(self) -> None:
        async with self._lock:
            stack = self._stack
            self._stack = None
            self._client = None
            self.available = False
            if stack is not None:
                with suppress(BaseException):
                    await stack.aclose()


class MCPClientManager:
    """Own a small set of persistent, allowlisted MCP stdio connections."""

    def __init__(self, definitions: list[MCPServerDefinition]) -> None:
        aliases = [definition.alias for definition in definitions]
        if len(aliases) != len(set(aliases)):
            raise ValueError("duplicate MCP server alias")
        self._connections = {definition.alias: _MCPConnection(definition) for definition in definitions}

    async def start(self) -> None:
        for alias, connection in self._connections.items():
            try:
                await connection.connect()
            except Exception as exc:
                LOGGER.warning("MCP server %s unavailable at startup (%s)", alias, type(exc).__name__)

    def is_available(self, alias: str) -> bool:
        connection = self._connections.get(alias)
        return bool(connection and connection.available)

    async def call_tool(self, alias: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        connection = self._connections.get(alias)
        if connection is None:
            raise MCPUnavailableError("unknown MCP server")
        try:
            return await connection.call(tool_name, arguments)
        except MCPUnavailableError:
            raise
        except Exception as exc:
            LOGGER.warning("MCP call %s/%s failed (%s)", alias, tool_name, type(exc).__name__)
            raise MCPUnavailableError("MCP server call failed") from exc

    async def close(self) -> None:
        for connection in reversed(tuple(self._connections.values())):
            await connection.close()
