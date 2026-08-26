"""Backend contracts for Friday's optional host capability plane.

The package is intentionally inert: it can validate and project plans, but it
cannot spawn a process or execute a shell.
"""

from .capability_catalog import BUILTIN_CATALOG, CapabilityCatalog, CapabilityEntry
from .contracts import (
    PROTOCOL_VERSION,
    AdapterState,
    ContractError,
    Coverage,
    CoverageGrade,
    EffectOutcome,
    EvidenceRef,
    ExecutableAttestation,
    ExecutionProfile,
    ParsedActionResult,
    ParserStatus,
    RequestEnvelope,
    RiskClass,
    WireRequest,
    canonical_digest,
    canonical_json_bytes,
)
from .plans import HostActionPlan, WorkspaceGrant, assert_plan_current, create_action_plan
from .policy import NetworkPolicy, NetworkTargetSnapshot, normalize_network_targets

__all__ = [
    "BUILTIN_CATALOG",
    "PROTOCOL_VERSION",
    "AdapterState",
    "CapabilityCatalog",
    "CapabilityEntry",
    "ContractError",
    "Coverage",
    "CoverageGrade",
    "EffectOutcome",
    "EvidenceRef",
    "ExecutableAttestation",
    "ExecutionProfile",
    "HostActionPlan",
    "NetworkPolicy",
    "NetworkTargetSnapshot",
    "ParsedActionResult",
    "ParserStatus",
    "RequestEnvelope",
    "RiskClass",
    "WireRequest",
    "WorkspaceGrant",
    "assert_plan_current",
    "canonical_digest",
    "canonical_json_bytes",
    "create_action_plan",
    "normalize_network_targets",
]
