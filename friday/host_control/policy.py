"""Code-owned admission policy for bounded host-network actions."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import ContractError, canonical_digest

_HOSTNAME = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)
_COMMAND_SHAPED = re.compile(r"[\s;|&`$<>\\\x00-\x1f\x7f]")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str], Sequence[str]]

_IPV4_PRIVATE: tuple[ipaddress.IPv4Network, ...] = tuple(
    ipaddress.IPv4Network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_FORBIDDEN_V4 = tuple(
    ipaddress.ip_network(value)
    for value in (
        "0.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)
_FORBIDDEN_V6 = tuple(
    ipaddress.ip_network(value)
    for value in ("::/128", "::ffff:0:0/96", "100::/64", "2001:db8::/32", "ff00::/8")
)


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Snapshot of operator configuration and code-observed connected routes."""

    connected_cidrs: tuple[str, ...]
    allowed_cidrs: tuple[str, ...] = ()
    allow_public: bool = False
    max_targets: int = 256
    max_target_tokens: int = 16

    def __post_init__(self) -> None:
        if isinstance(self.max_targets, bool) or not 1 <= self.max_targets <= 256:
            raise ContractError("network max_targets is invalid")
        if isinstance(self.max_target_tokens, bool) or not 1 <= self.max_target_tokens <= 64:
            raise ContractError("network max_target_tokens is invalid")
        _parse_network_set(self.connected_cidrs, field="connected_cidrs")
        _parse_network_set(self.allowed_cidrs, field="allowed_cidrs")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "allow_public": self.allow_public,
                "allowed_cidrs": sorted(self.allowed_cidrs),
                "connected_cidrs": sorted(self.connected_cidrs),
                "max_target_tokens": self.max_target_tokens,
                "max_targets": self.max_targets,
                "schema_version": 1,
            }
        )


@dataclass(frozen=True, slots=True)
class TargetBinding:
    requested: str
    execution_targets: tuple[str, ...]
    resolved_addresses: tuple[str, ...]
    address_count: int
    classification: str
    route_evidence: tuple[str, ...]
    approval_required: bool

    def __post_init__(self) -> None:
        if not self.requested or len(self.requested) > 253:
            raise ContractError("target binding request is invalid")
        if (
            not self.execution_targets
            or len(self.execution_targets) > 64
            or any(
                not isinstance(item, str) or not item or len(item) > 253 for item in self.execution_targets
            )
        ):
            raise ContractError("target binding execution scope is invalid")
        if len(self.resolved_addresses) > 16 or any(
            not isinstance(item, str) or not item or len(item) > 80 for item in self.resolved_addresses
        ):
            raise ContractError("target binding resolution is invalid")
        if isinstance(self.address_count, bool) or self.address_count < 1:
            raise ContractError("target binding address count is invalid")
        if not self.classification or len(self.classification) > 64:
            raise ContractError("target classification is invalid")
        if len(self.route_evidence) > 16 or any(
            not isinstance(item, str) or not item or len(item) > 160 for item in self.route_evidence
        ):
            raise ContractError("target route evidence is invalid")
        if not isinstance(self.approval_required, bool):
            raise ContractError("target approval classification is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "address_count": self.address_count,
            "approval_required": self.approval_required,
            "classification": self.classification,
            "execution_targets": list(self.execution_targets),
            "requested": self.requested,
            "resolved_addresses": list(self.resolved_addresses),
            "route_evidence": list(self.route_evidence),
        }

    @classmethod
    def from_payload(cls, value: Any) -> TargetBinding:
        if not isinstance(value, dict) or set(value) != {
            "address_count",
            "approval_required",
            "classification",
            "execution_targets",
            "requested",
            "resolved_addresses",
            "route_evidence",
        }:
            raise ContractError("target binding fields are invalid")
        if any(
            not isinstance(value.get(name), list)
            for name in ("execution_targets", "resolved_addresses", "route_evidence")
        ):
            raise ContractError("target binding collections are invalid")
        try:
            return cls(
                requested=value["requested"],
                execution_targets=tuple(value["execution_targets"]),
                resolved_addresses=tuple(value["resolved_addresses"]),
                address_count=value["address_count"],
                classification=value["classification"],
                route_evidence=tuple(value["route_evidence"]),
                approval_required=value["approval_required"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("target binding payload is invalid") from exc


@dataclass(frozen=True, slots=True)
class NetworkTargetSnapshot:
    schema_version: int
    policy_digest: str
    bindings: tuple[TargetBinding, ...]
    target_count: int
    approval_required: bool

    def __post_init__(self) -> None:
        if self.schema_version != 1 or not re.fullmatch(r"[0-9a-f]{64}", self.policy_digest):
            raise ContractError("network target snapshot version/digest is invalid")
        if not self.bindings or isinstance(self.target_count, bool) or self.target_count < 1:
            raise ContractError("network target snapshot is empty")
        if sum(item.address_count for item in self.bindings) != self.target_count:
            raise ContractError("network target snapshot accounting is invalid")
        if not isinstance(self.approval_required, bool) or self.approval_required != any(
            item.approval_required for item in self.bindings
        ):
            raise ContractError("network target approval classification is inconsistent")

    @property
    def execution_targets(self) -> tuple[str, ...]:
        return tuple(target for binding in self.bindings for target in binding.execution_targets)

    def to_payload(self) -> dict[str, Any]:
        return {
            "approval_required": self.approval_required,
            "bindings": [item.to_payload() for item in self.bindings],
            "policy_digest": self.policy_digest,
            "schema_version": self.schema_version,
            "target_count": self.target_count,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())

    @classmethod
    def from_payload(cls, value: Any) -> NetworkTargetSnapshot:
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "approval_required",
                "bindings",
                "policy_digest",
                "schema_version",
                "target_count",
            }
            or not isinstance(value.get("bindings"), list)
        ):
            raise ContractError("network target snapshot fields are invalid")
        try:
            return cls(
                schema_version=value["schema_version"],
                policy_digest=value["policy_digest"],
                bindings=tuple(TargetBinding.from_payload(item) for item in value["bindings"]),
                target_count=value["target_count"],
                approval_required=value["approval_required"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError("network target snapshot payload is invalid") from exc


def _parse_network_set(values: Iterable[str], *, field: str) -> tuple[IPNetwork, ...]:
    parsed: list[IPNetwork] = []
    for raw in values:
        try:
            network = ipaddress.ip_network(str(raw), strict=True)
        except ValueError as exc:
            raise ContractError(f"{field} contains an invalid canonical CIDR") from exc
        if str(network) != str(raw):
            raise ContractError(f"{field} CIDR is not canonical")
        parsed.append(network)
    if len(parsed) > 128:
        raise ContractError(f"{field} exceeds route limit")
    return tuple(parsed)


def _containing_networks(candidate: IPNetwork, policy: NetworkPolicy) -> tuple[tuple[str, IPNetwork], ...]:
    matches: list[tuple[str, IPNetwork]] = []
    for source, networks in (
        ("connected", _parse_network_set(policy.connected_cidrs, field="connected_cidrs")),
        ("configured", _parse_network_set(policy.allowed_cidrs, field="allowed_cidrs")),
    ):
        for permitted in networks:
            if (
                isinstance(candidate, ipaddress.IPv4Network)
                and isinstance(permitted, ipaddress.IPv4Network)
                and candidate.subnet_of(permitted)
                or isinstance(candidate, ipaddress.IPv6Network)
                and isinstance(permitted, ipaddress.IPv6Network)
                and candidate.subnet_of(permitted)
            ):
                matches.append((source, permitted))
    return tuple(matches)


def _is_ula(network: IPNetwork) -> bool:
    return isinstance(network, ipaddress.IPv6Network) and network.subnet_of(ipaddress.IPv6Network("fc00::/7"))


def _is_ipv4_private(network: IPNetwork) -> bool:
    return isinstance(network, ipaddress.IPv4Network) and any(
        network.subnet_of(item) for item in _IPV4_PRIVATE
    )


def _reject_special_use(network: IPNetwork) -> None:
    forbidden = _FORBIDDEN_V4 if network.version == 4 else _FORBIDDEN_V6
    if any(network.overlaps(item) for item in forbidden):
        exact_loopback = network.prefixlen == network.max_prefixlen and network.network_address.is_loopback
        if not exact_loopback:
            raise ContractError("special-use network target is forbidden")


def _classify_and_admit(network: IPNetwork, policy: NetworkPolicy) -> tuple[str, tuple[str, ...], bool]:
    _reject_special_use(network)
    matches = _containing_networks(network, policy)
    route_evidence = tuple(f"{source}:{permitted}" for source, permitted in matches[:16])
    exact_loopback = network.prefixlen == network.max_prefixlen and network.network_address.is_loopback
    if exact_loopback:
        return "loopback_exact", ("builtin:loopback_exact",), False
    if not matches:
        raise ContractError("network target is outside connected/configured policy")

    has_connected = any(source == "connected" for source, _network in matches)
    has_configured = any(source == "configured" for source, _network in matches)
    if _is_ipv4_private(network) and has_connected:
        return "connected_private_ipv4", route_evidence, False
    if (
        network.version == 6
        and (network.is_link_local or _is_ula(network))
        and (has_connected or has_configured)
    ):
        classification = "connected_ipv6_link_local" if network.is_link_local else "approved_ipv6_ula"
        return classification, route_evidence, False
    if has_configured and (_is_ipv4_private(network) or _is_ula(network)):
        return "operator_approved_private", route_evidence, False
    if has_configured and policy.allow_public:
        return "operator_approved_public", route_evidence, True
    raise ContractError("public network scanning is disabled")


def _address_binding(raw: str, address: IPAddress, policy: NetworkPolicy) -> TargetBinding:
    network = ipaddress.ip_network(f"{address}/{address.max_prefixlen}", strict=True)
    classification, evidence, approval = _classify_and_admit(network, policy)
    canonical = str(address)
    return TargetBinding(
        requested=raw,
        execution_targets=(canonical,),
        resolved_addresses=(canonical,),
        address_count=1,
        classification=classification,
        route_evidence=evidence,
        approval_required=approval,
    )


def normalize_network_targets(
    requested: Sequence[str],
    policy: NetworkPolicy,
    *,
    resolver: Resolver | None = None,
) -> NetworkTargetSnapshot:
    """Normalize and pin a closed target set without accepting command syntax.

    Hostnames are resolved by the caller-supplied code-owned resolver.  Their
    resolved IPs, rather than the hostname, become execution arguments so a
    later DNS change cannot silently widen the plan.
    """

    if not requested or len(requested) > policy.max_target_tokens:
        raise ContractError("network target token count is invalid")
    bindings: list[TargetBinding] = []
    admitted_networks: list[IPNetwork] = []
    total = 0
    execution_token_count = 0
    for value in requested:
        raw = str(value or "")
        if raw != raw.strip() or not raw or len(raw) > 253 or _COMMAND_SHAPED.search(raw):
            raise ContractError("network target token is command-shaped or invalid")
        network: IPNetwork | None = None
        address: IPAddress | None = None
        if "/" in raw:
            try:
                network = ipaddress.ip_network(raw, strict=True)
            except ValueError as exc:
                raise ContractError("network target CIDR is invalid") from exc
        else:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                address = None
        binding_networks: list[IPNetwork]
        if network is not None:
            canonical = str(network)
            if canonical != raw:
                raise ContractError("network target CIDR is not canonical")
            count = int(network.num_addresses)
            classification, evidence, approval = _classify_and_admit(network, policy)
            binding = TargetBinding(
                requested=raw,
                execution_targets=(canonical,),
                resolved_addresses=(),
                address_count=count,
                classification=classification,
                route_evidence=evidence,
                approval_required=approval,
            )
            binding_networks = [network]
        else:
            if address is not None:
                if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
                    address = address.ipv4_mapped
                binding = _address_binding(raw, address, policy)
                binding_networks = [ipaddress.ip_network(f"{address}/{address.max_prefixlen}", strict=True)]
            else:
                if not _HOSTNAME.fullmatch(raw) or resolver is None:
                    raise ContractError("hostname target cannot be pinned")
                canonical_host = raw.rstrip(".").encode("idna").decode("ascii").casefold()
                try:
                    resolved_values: set[IPAddress] = set()
                    for item in resolver(canonical_host):
                        parsed_address = ipaddress.ip_address(item)
                        if isinstance(parsed_address, ipaddress.IPv6Address) and parsed_address.ipv4_mapped:
                            parsed_address = parsed_address.ipv4_mapped
                        resolved_values.add(parsed_address)
                    resolved = tuple(sorted(resolved_values, key=lambda item: (item.version, int(item))))
                except (TypeError, ValueError) as exc:
                    raise ContractError("hostname resolution is invalid") from exc
                if not resolved or len(resolved) > 16:
                    raise ContractError("hostname resolution is empty or oversized")
                child_bindings = [_address_binding(raw, address_item, policy) for address_item in resolved]
                binding = TargetBinding(
                    requested=canonical_host,
                    execution_targets=tuple(item.execution_targets[0] for item in child_bindings),
                    resolved_addresses=tuple(item.execution_targets[0] for item in child_bindings),
                    address_count=len(child_bindings),
                    classification="hostname_pinned",
                    route_evidence=tuple(
                        dict.fromkeys(evidence for item in child_bindings for evidence in item.route_evidence)
                    )[:16],
                    approval_required=any(item.approval_required for item in child_bindings),
                )
                binding_networks = [
                    ipaddress.ip_network(f"{item}/{item.max_prefixlen}", strict=True) for item in resolved
                ]
        if any(
            candidate.overlaps(existing) for candidate in binding_networks for existing in admitted_networks
        ):
            raise ContractError("network target set contains overlapping scope")
        admitted_networks.extend(binding_networks)
        total += binding.address_count
        execution_token_count += len(binding.execution_targets)
        if total > policy.max_targets:
            raise ContractError("network target set exceeds configured address cap")
        if execution_token_count > 64:
            raise ContractError("network target set exceeds execution-token cap")
        bindings.append(binding)
    return NetworkTargetSnapshot(
        schema_version=1,
        policy_digest=policy.digest,
        bindings=tuple(bindings),
        target_count=total,
        approval_required=any(item.approval_required for item in bindings),
    )


def assert_target_snapshot_current(snapshot: NetworkTargetSnapshot, policy: NetworkPolicy) -> None:
    if snapshot.policy_digest != policy.digest:
        raise ContractError("network policy changed after planning")
    if snapshot.target_count > policy.max_targets:
        raise ContractError("network target snapshot exceeds current policy")
    try:
        current = normalize_network_targets(snapshot.execution_targets, policy)
    except ContractError as exc:
        raise ContractError("network target is no longer admitted by current policy") from exc
    if (
        current.execution_targets != snapshot.execution_targets
        or current.target_count != snapshot.target_count
        or current.approval_required != snapshot.approval_required
    ):
        raise ContractError("network target identity changed under current policy")


__all__ = [
    "NetworkPolicy",
    "NetworkTargetSnapshot",
    "Resolver",
    "TargetBinding",
    "assert_target_snapshot_current",
    "normalize_network_targets",
]
