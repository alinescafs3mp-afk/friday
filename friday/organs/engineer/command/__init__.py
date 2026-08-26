"""Isolated universal Engineer command-runner kernel.

This package executes argv or exact-owner shell under an explicit short-lived
grant. It does not mint authority from inventory, PATH, model output, documents
or the typed installed-tool registry. Conversational wiring lives outside.
"""

from __future__ import annotations

from .contracts import (
    CommandError,
    CommandLane,
    CommandOrigin,
    CommandProgress,
    CommandReceipt,
    CommandRequest,
    CommandStatus,
    GeneratedFile,
    ResolvedExecutable,
    VerifiedCommandGrant,
)
from .grant import CommandGrantAuthority
from .kernel import CommandKernel

__all__ = [
    "CommandError",
    "CommandGrantAuthority",
    "CommandKernel",
    "CommandLane",
    "CommandOrigin",
    "CommandProgress",
    "CommandReceipt",
    "CommandRequest",
    "CommandStatus",
    "GeneratedFile",
    "ResolvedExecutable",
    "VerifiedCommandGrant",
]
