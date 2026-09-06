"""Lightweight public contracts; load the runtime router only on demand.

Eagerly importing router here made leaf imports depend on startup order: router
imports Coding, while the Coding worker imports orchestration contracts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
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


_ROUTER_EXPORTS = frozenset(
    {
        "OrchestrationRouter",
        "ReadOnlyAttachmentReference",
        "ReadOnlyRoutePreparation",
        "ReadOnlyRouteRequest",
        "ReadOnlyRouteResult",
        "build_orchestrated_agent",
    }
)


def __getattr__(name: str) -> Any:
    if name in _ROUTER_EXPORTS:
        from friday.orchestration import router

        return getattr(router, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
