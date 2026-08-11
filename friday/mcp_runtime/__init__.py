"""MCP connectors owned by Friday's existing authorization boundary.

MCP is a transport, not a second policy engine.  Server-advertised schemas and
annotations never go straight to the model: code-owned adapters in this package
expose a deliberately small surface through :class:`ExecutionKernel`.
"""

from .client import MCPClientManager, MCPServerDefinition, MCPUnavailableError

__all__ = [
    "MCPClientManager",
    "MCPServerDefinition",
    "MCPUnavailableError",
]
