"""Bounded code-owned runtime facts for the owner-only Engineer workbench."""

from __future__ import annotations

import ipaddress
import os
import platform
import socket
import struct
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from friday import __version__

try:
    import fcntl
except ImportError:  # pragma: no cover - Engineer production is Linux-only
    fcntl = None  # type: ignore[assignment]

_SIOCGIFADDR = 0x8915
_SIOCGIFNETMASK = 0x891B
_MAX_INTERFACES = 32


def _virtualization_boundary() -> str:
    try:
        product = Path("/sys/class/dmi/id/product_name").read_text(encoding="utf-8", errors="replace")[:160]
    except OSError:
        return "undetected"
    normalized = product.casefold()
    for marker, label in (
        ("vmware", "vmware_virtual_machine"),
        ("virtualbox", "virtualbox_virtual_machine"),
        ("kvm", "kvm_virtual_machine"),
        ("qemu", "qemu_virtual_machine"),
        ("virtual machine", "hyperv_virtual_machine"),
    ):
        if marker in normalized:
            return label
    return "physical_or_undetected"


def _interface_ipv4(name: str, request: int) -> str:
    encoded = str(name or "").encode("ascii", errors="ignore")[:15]
    if not encoded or fcntl is None:
        return ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as handle:
            payload = fcntl.ioctl(handle.fileno(), request, struct.pack("256s", encoded))
        return socket.inet_ntoa(payload[20:24])
    except OSError:
        return ""


def local_ipv4_interfaces() -> list[dict[str, str]]:
    """Return a bounded Linux interface projection without shelling out."""

    rows: list[dict[str, str]] = []
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return rows
    for _index, raw_name in interfaces[:_MAX_INTERFACES]:
        name = str(raw_name or "")[:32]
        address = _interface_ipv4(name, _SIOCGIFADDR)
        netmask = _interface_ipv4(name, _SIOCGIFNETMASK)
        if not address or not netmask:
            continue
        try:
            interface = ipaddress.ip_interface(f"{address}/{netmask}")
        except ValueError:
            continue
        rows.append(
            {
                "address": str(interface.ip),
                "interface": name,
                "network": str(interface.network),
            }
        )
    return rows


def environment_passport(
    *,
    allowed_cidrs: Sequence[str],
    binaries: Mapping[str, str | None],
    host_control_enabled: bool = False,
    package_install_enabled: bool = False,
) -> dict[str, Any]:
    """Describe only stable local facts useful to Engineer reasoning.

    The passport is owner-only transient evidence. It intentionally excludes the
    hostname, usernames, paths, environment variables and arbitrary system
    inventory.
    """

    try:
        os_release = platform.freedesktop_os_release()
    except OSError:
        os_release = {}
    os_name = " ".join(
        str(os_release.get("PRETTY_NAME") or os_release.get("NAME") or platform.system()).split()
    )[:160]
    tools = {str(name)[:40]: bool(path) for name, path in sorted(binaries.items()) if str(name).strip()}
    tools["friday_bounded_tcp"] = True
    tools["friday_dns_tls_http"] = True
    runtime = (
        "container"
        if Path("/.dockerenv").exists()
        else "native_systemd"
        if os.environ.get("INVOCATION_ID")
        else "native_process"
    )
    return {
        "schema": "friday.engineer-environment.v1",
        "architecture": platform.machine()[:40],
        "kernel": platform.release()[:120],
        "local_ipv4_interfaces": local_ipv4_interfaces(),
        "operator_allowed_networks": [str(item)[:80] for item in allowed_cidrs[:32]],
        "operating_system": os_name,
        "product_version": __version__,
        "host_control_enabled": host_control_enabled is True,
        "package_install_enabled": package_install_enabled is True,
        "runtime": runtime,
        "virtualization": _virtualization_boundary(),
        "tools": tools,
    }


def environment_markdown(value: Mapping[str, Any]) -> str:
    """Render the closed passport as data for the primary model."""

    interfaces = value.get("local_ipv4_interfaces")
    interface_rows = interfaces if isinstance(interfaces, list) else []
    interface_text = ", ".join(
        f"{str(item.get('interface') or '')}={str(item.get('address') or '')} ({str(item.get('network') or '')})"
        for item in interface_rows[:_MAX_INTERFACES]
        if isinstance(item, Mapping)
    )
    allowed = value.get("operator_allowed_networks")
    allowed_rows = allowed if isinstance(allowed, list) else []
    tools = value.get("tools")
    tool_rows = tools if isinstance(tools, Mapping) else {}
    tool_text = ", ".join(
        f"{str(name)}={'available' if available is True else 'unavailable'}"
        for name, available in sorted(tool_rows.items(), key=lambda item: str(item[0]))
    )
    lines = [
        "## Friday runtime environment",
        f"operating system: {str(value.get('operating_system') or 'unknown')[:160]}",
        f"kernel/architecture: {str(value.get('kernel') or 'unknown')[:120]} / "
        f"{str(value.get('architecture') or 'unknown')[:40]}",
        f"runtime: {str(value.get('runtime') or 'unknown')[:40]}",
        f"virtualization: {str(value.get('virtualization') or 'undetected')[:40]}",
        f"Friday version: {str(value.get('product_version') or 'unknown')[:40]}",
        "observed local IPv4 interfaces (location evidence, not target authority): "
        + (interface_text or "unavailable"),
        "operator-authorized host/LAN scan scope: "
        + (", ".join(str(item) for item in allowed_rows[:32]) or "not configured"),
        (
            "deictic network rule: ‘my subnet’ means the sole operator-authorized "
            "host/LAN scope above, never a runtime VM-interface subnet"
            if len(allowed_rows) == 1
            else "deictic network rule: ambiguous; an explicit authorized CIDR is required"
        ),
        "host control/package installation: "
        f"{'enabled' if value.get('host_control_enabled') is True else 'disabled'}/"
        f"{'enabled' if value.get('package_install_enabled') is True else 'disabled'}",
        "diagnostic tools: " + (tool_text or "unavailable"),
    ]
    return "\n".join(lines)[:8_000]


__all__ = ["environment_markdown", "environment_passport", "local_ipv4_interfaces"]
