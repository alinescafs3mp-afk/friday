"""Isolated universal Engineer command-runner kernel.

This package executes argv or exact-owner shell under an explicit short-lived
grant bound to an authenticated owner source. Inventory, PATH, model output,
documents and the typed installed-tool registry cannot mint that source.
Conversational wiring lives outside.
"""

from __future__ import annotations

from .confirm import OwnerConfirmationAuthority
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
    OwnerConfirmation,
    OwnerSource,
    ResolvedExecutable,
    ResourceLimits,
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
    "OwnerConfirmation",
    "OwnerConfirmationAuthority",
    "OwnerSource",
    "OwnerSourceAuthority",
    "ResolvedExecutable",
    "ResourceLimits",
    "TrustedPathContract",
    "VerifiedCommandGrant",
]
