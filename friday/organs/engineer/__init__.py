"""Engineer organ — owner-only workbench for artifacts and host recon."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from friday.organs import Organ, ServiceContext
from friday.permissions import CapabilityDefinition

from .authority import issue_target_ticket, verify_target_ticket
from .targets import PinnedTarget

if TYPE_CHECKING:
    from friday.execution_kernel import ToolSpec

ENGINEER_USE = CapabilityDefinition(
    "engineer.use",
    "Enter engineer workbench mode",
    "engineer",
    2,
    (),
    source="organ",
)
ENGINEER_ANALYZE = CapabilityDefinition(
    "engineer.artifact.analyze",
    "Statically analyse an owned binary or archive",
    "engineer",
    2,
    (),
    source="organ",
)
ENGINEER_PATCH = CapabilityDefinition(
    "engineer.artifact.patch",
    "Emit a patched copy of an owned artifact",
    "engineer",
    3,
    (),
    source="organ",
)
ENGINEER_AUDIT = CapabilityDefinition(
    "engineer.host.audit",
    "Hunt a host the owner named in chat",
    "engineer",
    3,
    (),
    source="organ",
)


class EngineerOrgan(Organ):
    name = "engineer"
    version = "1.0"

    def capabilities(self) -> Sequence[CapabilityDefinition]:
        return (ENGINEER_USE, ENGINEER_ANALYZE, ENGINEER_PATCH, ENGINEER_AUDIT)

    def tools(self, ctx: ServiceContext) -> Sequence[ToolSpec]:
        from .tools import build_engineer_tools

        return build_engineer_tools(ctx)


__all__ = [
    "ENGINEER_ANALYZE",
    "ENGINEER_AUDIT",
    "ENGINEER_PATCH",
    "ENGINEER_USE",
    "EngineerOrgan",
]

__all__ += ["PinnedTarget", "issue_target_ticket", "verify_target_ticket"]
