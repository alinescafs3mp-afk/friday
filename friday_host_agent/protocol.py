"""Host-agent aliases for the one shared backend wire contract."""

from __future__ import annotations

from typing import Any

from friday.host_control.contracts import (
    MAX_BODY_BYTES,
    MAX_WIRE_BYTES,
    PROTOCOL_MAJOR,
    PROTOCOL_VERSION,
    ContractError,
    RequestEnvelope,
    WireRequest,
    body_sha256,
    canonical_json_bytes,
)


class ProtocolError(ContractError):
    """A request failed a host-agent admission check with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_json(value: Any, *, max_bytes: int = MAX_BODY_BYTES) -> bytes:
    try:
        return canonical_json_bytes(value, maximum=max_bytes)
    except ContractError as exc:
        raise ProtocolError("invalid_json", "value is not canonical JSON") from exc


__all__ = [
    "MAX_BODY_BYTES",
    "MAX_WIRE_BYTES",
    "PROTOCOL_MAJOR",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "RequestEnvelope",
    "WireRequest",
    "body_sha256",
    "canonical_json",
]
