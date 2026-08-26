"""Owner-only Host Capability Plane organ."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from friday.organs import Organ, ServiceContext
from friday.permissions import CapabilityDefinition

if TYPE_CHECKING:
    from friday.execution_kernel import ToolSpec

HOST_CAPABILITIES_READ = CapabilityDefinition(
    "host.capabilities.read", "Inspect reviewed host capabilities", "host", 2, ("owner",), source="organ"
)
HOST_ACTIONS_EXECUTE = CapabilityDefinition(
    "host.actions.execute", "Execute reviewed host actions", "host", 3, ("owner",), source="organ"
)
HOST_NETWORK_SCAN = CapabilityDefinition(
    "host.network.scan", "Scan an explicitly authorized network scope", "host", 3, ("owner",), source="organ"
)
HOST_FILES_READ = CapabilityDefinition(
    "host.files.read", "Read exact files granted to a host job", "host", 3, ("owner",), source="organ"
)
HOST_JOBS_MANAGE = CapabilityDefinition(
    "host.jobs.manage", "Inspect and cancel owned host jobs", "host", 3, ("owner",), source="organ"
)
HOST_PACKAGES_READ = CapabilityDefinition(
    "host.packages.read", "Inspect allowlisted package candidates", "host", 3, ("owner",), source="organ"
)
HOST_PACKAGES_INSTALL = CapabilityDefinition(
    "host.packages.install",
    "Install an exact approved package transaction",
    "host",
    4,
    ("owner",),
    source="organ",
)


class HostControlOrgan(Organ):
    name = "host_control"
    version = "1.0"

    def capabilities(self) -> Sequence[CapabilityDefinition]:
        return (
            HOST_CAPABILITIES_READ,
            HOST_ACTIONS_EXECUTE,
            HOST_NETWORK_SCAN,
            HOST_FILES_READ,
            HOST_JOBS_MANAGE,
            HOST_PACKAGES_READ,
            HOST_PACKAGES_INSTALL,
        )

    def tools(self, ctx: ServiceContext) -> Sequence[ToolSpec]:
        from friday.host_control.tools import build_host_control_tools

        return build_host_control_tools(ctx)


__all__ = ["HostControlOrgan"]
