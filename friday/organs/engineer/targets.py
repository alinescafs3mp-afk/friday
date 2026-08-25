"""Code-owned target selection for the engineer workbench.

Only the current human speech may mint a target. Model output can refer to the
resulting :class:`PinnedTarget`, but cannot construct one from a free-form host
argument.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b")
_IPV6 = re.compile(r"(?<![0-9a-f:])(?:[0-9a-f]{1,4}:){2,7}[0-9a-f:.]{1,15}(?![0-9a-f:])", re.IGNORECASE)
_IPV6_LOOSE = re.compile(
    r"(?<![0-9a-z])(?:\[[0-9a-f:.]+\](?::\d{1,5})?|[0-9a-f:.]*:[0-9a-f:.]*:[0-9a-f:.]*)(?![0-9a-z])",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>'\"]+", re.IGNORECASE)
_HOSTNAME = re.compile(
    r"\b(?:localhost|(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63})\b",
    re.IGNORECASE,
)
_TRAILING = re.compile(r"[),.;,]+$")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.IGNORECASE)
_METADATA_V4 = ipaddress.ip_address("169.254.169.254")
_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")
_OTHER_METADATA_V4 = frozenset(
    {
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("192.0.0.192"),
    }
)


@dataclass(frozen=True, slots=True)
class PinnedTarget:
    """Resolved, current-speech authority consumed by network stages."""

    host: str
    addresses: tuple[str, ...]
    implied_port: int | None
    source_token: str
    source_sha256: str

    @property
    def connect_address(self) -> str:
        if not self.addresses:
            raise ValueError("pinned target has no authorized address")
        return self.addresses[0]

    def public_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "addresses": list(self.addresses),
            "implied_port": self.implied_port,
            "source_sha256": self.source_sha256,
        }


_PINNED_TARGET: ContextVar[PinnedTarget | None] = ContextVar("friday_engineer_pinned_target", default=None)


@contextmanager
def bind_pinned_target(target: PinnedTarget | None) -> Iterator[PinnedTarget | None]:
    """Bind code-owned target authority across one model/tool turn."""

    token = _PINNED_TARGET.set(target)
    try:
        yield target
    finally:
        _PINNED_TARGET.reset(token)


def current_pinned_target() -> PinnedTarget | None:
    return _PINNED_TARGET.get()


def normalize_ip_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Collapse IPv4-mapped IPv6 before applying destination policy."""

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def is_forbidden_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject metadata aliases and non-target network classes fail-closed."""

    normalized = normalize_ip_address(address)
    if normalized in {_METADATA_V4, _METADATA_V6, *_OTHER_METADATA_V4}:
        return True
    return bool(
        normalized.is_unspecified
        or normalized.is_multicast
        or normalized.is_reserved
        or normalized.is_link_local
    )


def _normalize_hostname(value: str) -> str:
    host = str(value or "").strip().rstrip(".").casefold()
    if not host or len(host) > 253 or "\x00" in host or any(char.isspace() for char in host):
        raise ValueError("host is empty or malformed")
    try:
        return str(normalize_ip_address(ipaddress.ip_address(host)))
    except ValueError:
        pass
    if host == "localhost":
        return host
    labels = host.split(".")
    if len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("host is empty or malformed")
    return host


def _validate_port(port: int | None) -> int | None:
    if port is None:
        return None
    if isinstance(port, bool) or not 1 <= int(port) <= 65535:
        raise ValueError("port is not in 1..65535")
    return int(port)


def parse_host_token(value: str) -> tuple[str, int | None]:
    raw = _TRAILING.sub("", str(value or "").strip())
    if not raw:
        raise ValueError("host is empty")
    if "://" in raw:
        parsed = urlsplit(raw)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("URL port is invalid") from exc
        return _normalize_hostname(parsed.hostname or ""), _validate_port(port)
    if raw.count(":") == 1 and not raw.startswith("["):
        host, maybe_port = raw.rsplit(":", 1)
        if maybe_port.isdigit():
            return _normalize_hostname(host), _validate_port(int(maybe_port))
    if raw.startswith("[") and "]" in raw:
        host = raw[1 : raw.index("]")]
        rest = raw[raw.index("]") + 1 :]
        if rest:
            if not rest.startswith(":") or not rest[1:].isdigit():
                raise ValueError("bracketed host has an invalid port")
            return _normalize_hostname(host), _validate_port(int(rest[1:]))
        return _normalize_hostname(host), None
    return _normalize_hostname(raw), None


def extract_targets(speech: str) -> list[dict[str, str | int | None]]:
    """Return distinct targets in textual appearance order, without overlap."""

    text = str(speech or "")
    candidates: list[tuple[int, int, int, str, str]] = []
    for priority, (kind, pattern) in enumerate(
        (
            ("url", _URL),
            ("ipv6", _IPV6_LOOSE),
            ("ipv4", _IPV4),
            ("ipv6", _IPV6),
            ("hostname", _HOSTNAME),
        )
    ):
        for match in pattern.finditer(text):
            candidates.append((match.start(), match.end(), priority, kind, match.group()))
    candidates.sort(key=lambda item: (item[0], item[2], -(item[1] - item[0])))

    accepted_ranges: list[tuple[int, int]] = []
    found: list[dict[str, str | int | None]] = []
    seen: set[tuple[str, int | None]] = set()
    for start, end, _priority, kind, token in candidates:
        if any(
            start < accepted_end and end > accepted_start for accepted_start, accepted_end in accepted_ranges
        ):
            continue
        try:
            host, port = parse_host_token(token)
        except ValueError:
            continue
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        accepted_ranges.append((start, end))
        found.append({"host": host, "port": port, "kind": kind, "token": token[:253]})
    return found


def extract_single_target(speech: str) -> dict[str, str | int | None] | None:
    """Select exactly one current-speech target or refuse the ambiguous turn."""

    targets = extract_targets(speech)
    if not targets:
        return None
    if len(targets) != 1:
        raise ValueError("engineer network turn must name exactly one target")
    return targets[0]


def target_source_sha256(speech: str, token: str) -> str:
    body = f"{str(speech or '')}\x00{str(token or '')}".encode("utf-8", errors="replace")
    return hashlib.sha256(body).hexdigest()


__all__ = [
    "PinnedTarget",
    "bind_pinned_target",
    "current_pinned_target",
    "extract_single_target",
    "extract_targets",
    "is_forbidden_address",
    "normalize_ip_address",
    "parse_host_token",
    "target_source_sha256",
]
