"""Private validation helpers for storage-independent retrieval contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar, cast

_MAX_JSON_BYTES = 65_536
_MIN_HANDLE_KEY_BYTES = 32
_MAX_COUNT = 1_000_000_000
EnumT = TypeVar("EnumT", bound=StrEnum)


class RetrievalContractError(ValueError):
    """A value is outside a closed retrieval contract."""


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RetrievalContractError("canonical JSON contains a duplicate key")
        result[key] = value
    return result


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def parse_canonical_object(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RetrievalContractError(f"{label} must be canonical JSON text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RetrievalContractError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > _MAX_JSON_BYTES:
        raise RetrievalContractError(f"{label} exceeds the closed byte limit")
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                RetrievalContractError(f"{label} contains a non-finite number")
            ),
            object_pairs_hook=_closed_object,
        )
    except json.JSONDecodeError as exc:
        raise RetrievalContractError(f"{label} must contain one JSON object") from exc
    if type(parsed) is not dict or value != canonical_json(parsed):
        raise RetrievalContractError(f"{label} must be closed canonical JSON")
    return cast(dict[str, Any], parsed)


def exact_object(value: object, keys: frozenset[str], *, label: str) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != keys:
        raise RetrievalContractError(f"{label} keys do not match the closed contract")
    return cast(dict[str, Any], value)


def enum_value(enum_type: type[EnumT], value: object, *, label: str) -> EnumT:
    if not isinstance(value, str) or len(value) > 80 or has_control(value):
        raise RetrievalContractError(f"{label} must be a closed enum value")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise RetrievalContractError(f"{label} must be a closed enum value") from exc


def has_control(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def bounded_text(value: object, *, label: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or has_control(value):
        raise RetrievalContractError(f"{label} must be bounded canonical text")
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise RetrievalContractError(f"{label} must be valid UTF-8") from exc
    if size > maximum_bytes:
        raise RetrievalContractError(f"{label} must be bounded canonical text")
    return value


def optional_bounded_text(value: object, *, label: str, maximum_bytes: int) -> str | None:
    if value is None:
        return None
    return bounded_text(value, label=label, maximum_bytes=maximum_bytes)


def lowercase_sha256(value: object, *, label: str) -> str:
    text = bounded_text(value, label=label, maximum_bytes=64)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise RetrievalContractError(f"{label} must be a lowercase SHA-256 digest")
    return text


def bounded_count(value: object, *, label: str, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_COUNT:
        raise RetrievalContractError(f"{label} is outside the closed count range")
    return value


def canonical_utc(value: object, *, label: str) -> str:
    text = bounded_text(value, label=label, maximum_bytes=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetrievalContractError(f"{label} must be an offset-aware instant") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RetrievalContractError(f"{label} must include an offset")
    canonical = parsed.astimezone(UTC).isoformat()
    if text != canonical:
        raise RetrievalContractError(f"{label} must already be normalized to UTC")
    return canonical


def utc_text(value: datetime, *, label: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RetrievalContractError(f"{label} must include an offset")
    return value.astimezone(UTC).isoformat()


def keyed_digest(domain: bytes, payload: Mapping[str, Any], key: object) -> str:
    if type(key) is not bytes or len(key) < _MIN_HANDLE_KEY_BYTES:
        raise RetrievalContractError("privacy handle key must contain at least 32 immutable bytes")
    material = domain + b"\0" + canonical_json(payload).encode("ascii")
    return hmac.new(key, material, hashlib.sha256).hexdigest()
