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
    report = await ctx.obsidian.reconcile()
    failures = report.get("failed") if isinstance(report, dict) else None
    if type(failures) is not int or failures < 0:
        raise ObsidianReconcileError("Obsidian reconcile returned an invalid failure count")
    if failures:
        # Tenant failures are isolated inside the runtime, but the worker itself
        # must still be degraded.  Returning a report with ``failed > 0`` made the
        # generic supervisor publish a false ``ok`` heartbeat forever, hiding a
        # dead local Syncthing process from both health and recovery telemetry.
        raise ObsidianReconcileError(f"{failures} Obsidian profile operation(s) failed")
    return report


class ObsidianReconcileError(RuntimeError):
    """A complete tenant sweep that contained one or more isolated failures."""


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
                # Managed Syncthing is a child of the backend, not a standalone
                # systemd service.  Bootstrap it on every backend start instead of
                # waiting for a Telegram command or the first periodic interval.
                run_immediately=True,
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
    "ObsidianReconcileError",
    "reconcile_obsidian",
]
