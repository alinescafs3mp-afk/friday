"""Universal Engineer command-runner kernel.

This package executes isolated owner commands or explicitly delegated autonomous
HOST_USER model shells under a short-lived grant bound to an authenticated owner
source. Conversational wiring lives outside.
"""

from __future__ import annotations

from .confirm import OwnerConfirmationAuthority
from .contracts import (
    AutonomousDelegation,
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
    "AutonomousDelegation",
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
