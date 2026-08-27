"""Engineer organ — owner-only workbench for artifacts and host recon."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING

from friday.organs import Organ, OrganWorker, ServiceContext
from friday.permissions import CapabilityDefinition

from .authority import issue_target_ticket, verify_target_ticket
from .targets import PinnedTarget

if TYPE_CHECKING:
    from friday.execution_kernel import ToolSpec

    from .command_tools import EngineerCommandService

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
ENGINEER_BUILD = CapabilityDefinition(
    "engineer.artifact.build",
    "Compile one owned source with a fixed bounded profile",
    "engineer",
    3,
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
ENGINEER_COMMAND_RUN = CapabilityDefinition(
    "engineer.command.run",
    "Run autonomous model-planned shell commands as the Friday host user",
    "engineer",
    3,
    (),
    source="organ",
)
ENGINEER_COMMAND_MANAGE = CapabilityDefinition(
    "engineer.command.manage",
    "Inspect or cancel an owned Engineer command job",
    "engineer",
    2,
    (),
    source="organ",
)


class EngineerOrgan(Organ):
    name = "engineer"
    version = "1.0"

    def __init__(self) -> None:
        self._command_service: EngineerCommandService | None = None

    def _service(self, ctx: ServiceContext) -> EngineerCommandService | None:
        if not bool(getattr(ctx.settings, "engineer_command_enabled", False)):
            return None
        if self._command_service is None:
            from .command_tools import EngineerCommandService

            self._command_service = EngineerCommandService(ctx)
        return self._command_service

    def capabilities(self) -> Sequence[CapabilityDefinition]:
        capabilities = [ENGINEER_USE, ENGINEER_ANALYZE, ENGINEER_BUILD, ENGINEER_PATCH, ENGINEER_AUDIT]
        return (*capabilities, ENGINEER_COMMAND_RUN, ENGINEER_COMMAND_MANAGE)

    def tools(self, ctx: ServiceContext) -> Sequence[ToolSpec]:
        from .command_tools import build_engineer_command_tools
        from .tools import build_engineer_tools

        service = self._service(ctx)
        return (
            *build_engineer_tools(ctx),
            *build_engineer_command_tools(ctx, service=service),
        )

    def workers(self, ctx: ServiceContext) -> Sequence[OrganWorker]:
        service = self._service(ctx)
        if service is None:
            return ()

        async def publish_terminal_jobs(_ctx: ServiceContext) -> None:
            await asyncio.to_thread(service.publish_terminal_jobs)
            await asyncio.to_thread(service.publish_progress_jobs)

        async def retain_terminal_jobs(_ctx: ServiceContext) -> None:
            await asyncio.to_thread(service.retain_terminal_jobs)

        return (
            OrganWorker(
                name="engineer_command_terminal_delivery",
                run=publish_terminal_jobs,
                interval_sec=5.0,
                run_immediately=True,
                timeout_sec=120.0,
            ),
            OrganWorker(
                name="engineer_command_retention",
                run=retain_terminal_jobs,
                interval_sec=3600.0,
                run_immediately=True,
                timeout_sec=300.0,
            ),
        )

    async def close(self) -> None:
        service = self._command_service
        if service is None:
            return
        await asyncio.to_thread(service.close)


__all__ = [
    "ENGINEER_ANALYZE",
    "ENGINEER_AUDIT",
    "ENGINEER_BUILD",
    "ENGINEER_COMMAND_MANAGE",
    "ENGINEER_COMMAND_RUN",
    "ENGINEER_PATCH",
    "ENGINEER_USE",
    "EngineerOrgan",
]

__all__ += ["PinnedTarget", "issue_target_ticket", "verify_target_ticket"]
