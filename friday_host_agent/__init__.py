"""Unprivileged, authenticated host execution edge for Friday."""

from .adapter_registry import AdapterRegistry, AdapterValidationError, ValidatedAction
from .authentication import HMACAuthenticator, ReplayGuard
from .daemon import HostAgentDaemon
from .executable_attestation import ExecutableAttestationError, attest_executable, verify_executable
from .inventory import DpkgPackageResolver, ExecutableInventory, InventoryEntry, PackageIdentity
from .network_policy import load_agent_network_policy
from .process_runner import (
    DirectExecTestBackend,
    ProcessResult,
    ProcessRunner,
    ResourceBudgets,
    RunnerUnavailable,
    SystemdUserBackend,
    WorkspaceGrant,
)
from .protocol import PROTOCOL_VERSION, ProtocolError, RequestEnvelope, WireRequest
from .receipts import HostActionReceipt, ReceiptSigner, build_receipt

__all__ = [
    "AdapterRegistry",
    "AdapterValidationError",
    "DirectExecTestBackend",
    "DpkgPackageResolver",
    "ExecutableAttestationError",
    "ExecutableInventory",
    "HMACAuthenticator",
    "HostActionReceipt",
    "HostAgentDaemon",
    "InventoryEntry",
    "PROTOCOL_VERSION",
    "PackageIdentity",
    "ProcessResult",
    "ProcessRunner",
    "ProtocolError",
    "ReceiptSigner",
    "ReplayGuard",
    "RequestEnvelope",
    "ResourceBudgets",
    "RunnerUnavailable",
    "SystemdUserBackend",
    "ValidatedAction",
    "WireRequest",
    "WorkspaceGrant",
    "attest_executable",
    "build_receipt",
    "load_agent_network_policy",
    "verify_executable",
]
