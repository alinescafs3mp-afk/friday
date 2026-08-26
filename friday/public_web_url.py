"""Pure structural validation and identity for public HTTP(S) URLs.

The check intentionally performs no DNS lookup.  Callers which make a request
must still validate every resolved address and redirect at the network edge.
"""

from __future__ import annotations

import ipaddress
import re
import unicodedata
import urllib.parse
from typing import Any

_LEGACY_NUMERIC_IPV4 = re.compile(
    r"(?:0x[0-9a-f]+|0[0-7]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|0[0-7]+|[0-9]+)){0,3}",
    re.IGNORECASE,
)
_PRIVATE_DNS_SUFFIXES = (
    ".alt",
    ".corp",
    ".example",
    ".home",
    ".home.arpa",
    ".internal",
    ".invalid",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
    ".onion",
    ".test",
)
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def sanitize_public_web_url(value: Any) -> str:
    """Return one bounded, structurally public HTTP(S) URL or ``""``."""

    if not isinstance(value, str) or not value or len(value) > 2_048:
        return ""
    url = value.strip()
    if (
        not url
        or any(
            char.isspace() or ord(char) == 127 or unicodedata.category(char).startswith("C") for char in url
        )
        or "\\" in url
    ):
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    raw_hostname = parsed.hostname.rstrip(".").casefold()
    if not raw_hostname or "%" in raw_hostname:
        return ""
    try:
        hostname = raw_hostname.encode("idna").decode("ascii").rstrip(".").casefold()
    except UnicodeError:
        return ""
    if (
        not hostname
        or hostname in {"home.arpa", "localhost", "localhost.localdomain"}
        or hostname.endswith(_PRIVATE_DNS_SUFFIXES)
    ):
        return ""
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        # Browsers and socket stacks accept shorthand/octal/hex IPv4 forms.
        if _LEGACY_NUMERIC_IPV4.fullmatch(hostname) or "." not in hostname:
            return ""
    else:
        if not address.is_global or address.is_multicast or address.is_reserved:
            return ""
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.casefold(),
            netloc,
            parsed.path,
            parsed.query,
            parsed.fragment,
        )
    )


def _normalize_component(value: str) -> str:
    return re.sub(
        r"%([0-9A-Fa-f]{2})",
        lambda match: (
            decoded
            if (decoded := chr(int(match.group(1), 16))) in _UNRESERVED
            else f"%{match.group(1).upper()}"
        ),
        value,
    )


def _remove_dot_segments(path: str) -> str:
    absolute = path.startswith("/")
    trailing = path.endswith(("/.", "/.."))
    output: list[str] = []
    for segment in path.split("/"):
        if segment == ".":
            continue
        if segment == "..":
            if output and output[-1] != ".." and not (absolute and len(output) == 1 and output[0] == ""):
                output.pop()
            elif not absolute:
                output.append(segment)
            continue
        output.append(segment)
    result = "/".join(output)
    if absolute and not result.startswith("/"):
        result = f"/{result}"
    if absolute and not result:
        result = "/"
    if trailing and result != "/" and not result.endswith("/"):
        result = f"{result}/"
    return result


def canonical_public_web_url_key(value: Any) -> str:
    """Return one public URL identity for cross-boundary deduplication."""

    safe = sanitize_public_web_url(value)
    if not safe:
        return ""
    try:
        parsed = urllib.parse.urlsplit(safe)
        port = parsed.port
    except ValueError:
        return ""
    hostname = str(parsed.hostname or "").casefold()
    if not hostname:
        return ""
    host = f"[{hostname}]" if ":" in hostname else hostname
    scheme = parsed.scheme.casefold()
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    return urllib.parse.urlunsplit(
        (
            scheme,
            host,
            _remove_dot_segments(_normalize_component(parsed.path or "/")),
            _normalize_component(parsed.query),
            "",
        )
    )


__all__ = ("canonical_public_web_url_key", "sanitize_public_web_url")
