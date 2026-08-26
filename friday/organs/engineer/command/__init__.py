"""Isolated universal Engineer command-runner kernel.

This package executes argv or exact-owner shell under an explicit short-lived
grant bound to an authenticated owner source. Inventory, PATH, model output,
documents and the typed installed-tool registry cannot mint that source.
Conversational wiring lives outside.
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
    DestructiveApproval,
    GeneratedFile,
    IsolationProfile,
    OwnerSource,
    ResolvedExecutable,
    TrustedPathContract,
    VerifiedCommandGrant,
)
from .grant import CommandGrantAuthority
from .kernel import CommandKernel
from .source import OwnerSourceAuthority

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
    "DestructiveApproval",
    "GeneratedFile",
    "IsolationProfile",
    "OwnerSource",
    "OwnerSourceAuthority",
    "ResolvedExecutable",
    "TrustedPathContract",
    "VerifiedCommandGrant",
]
