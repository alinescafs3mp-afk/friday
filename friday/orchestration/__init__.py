"""Model-first orchestration contracts and the reversible runtime switch."""

from friday.orchestration.contracts import (
    EvidenceKind,
    EvidenceRequest,
    OutputContract,
    OutputFormat,
    PlanFallback,
    RouteClass,
    RouterMode,
    ToolEffect,
    ToolIntent,
    TurnInput,
    TurnPlan,
    TurnPlanError,
)
from friday.orchestration.router import (
    OrchestrationRouter,
    ReadOnlyAttachmentReference,
    ReadOnlyRoutePreparation,
    ReadOnlyRouteRequest,
    ReadOnlyRouteResult,
    build_orchestrated_agent,
)

__all__ = [
    "EvidenceKind",
    "EvidenceRequest",
    "OrchestrationRouter",
    "OutputContract",
    "OutputFormat",
    "PlanFallback",
    "ReadOnlyAttachmentReference",
    "ReadOnlyRoutePreparation",
    "ReadOnlyRouteRequest",
    "ReadOnlyRouteResult",
    "RouteClass",
    "RouterMode",
    "ToolEffect",
    "ToolIntent",
    "TurnInput",
    "TurnPlan",
    "TurnPlanError",
    "build_orchestrated_agent",
]
