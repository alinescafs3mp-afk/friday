"""First-party free Android Obsidian organ."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from friday.organs import Organ, OrganWorker, ServiceContext
from friday.permissions import CapabilityDefinition

if TYPE_CHECKING:
    from friday.execution_kernel import ToolSpec

OBSIDIAN_CONNECT = CapabilityDefinition(
    "obsidian.connect",
    "Connect and inspect an actor-owned Android Obsidian vault",
    "obsidian",
    1,
    ("admin", "moderator", "user"),
    source="organ",
)
OBSIDIAN_READ = CapabilityDefinition(
    "obsidian.read",
    "Read notes from an actor-owned Obsidian vault",
    "obsidian",
    0,
    ("admin", "moderator", "user"),
    source="organ",
)
OBSIDIAN_WRITE = CapabilityDefinition(
    "obsidian.write",
    "Create or update notes in an actor-owned Obsidian vault",
    "obsidian",
    1,
    ("admin", "moderator", "user"),
    source="organ",
)


async def reconcile_obsidian(ctx: ServiceContext) -> Any:
    if ctx.obsidian is None:
        return {"checked": 0, "failed": 0}
    return await ctx.obsidian.reconcile()


class ObsidianOrgan(Organ):
    name = "obsidian"
    version = "1.0"

    def capabilities(self) -> Sequence[CapabilityDefinition]:
        return (OBSIDIAN_CONNECT, OBSIDIAN_READ, OBSIDIAN_WRITE)

    def workers(self, ctx: ServiceContext) -> Sequence[OrganWorker]:
        return (
            OrganWorker(
                name="obsidian_reconcile",
                run=reconcile_obsidian,
                interval_sec=float(ctx.settings.obsidian_reconcile_interval_sec),
                enabled=bool(ctx.settings.obsidian_enabled and ctx.obsidian is not None),
                run_immediately=False,
                timeout_sec=120.0,
            ),
        )

    def tools(self, ctx: ServiceContext) -> Sequence[ToolSpec]:
        from .tools import build_obsidian_tools

        return build_obsidian_tools(ctx)

    def router(self):
        from .router import router

        return router


__all__ = [
    "OBSIDIAN_CONNECT",
    "OBSIDIAN_READ",
    "OBSIDIAN_WRITE",
    "ObsidianOrgan",
    "reconcile_obsidian",
]
