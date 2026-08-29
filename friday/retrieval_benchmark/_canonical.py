"""Strict canonical JSON helpers for the offline retrieval benchmark."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable, Mapping
from itertools import islice
from typing import Any, NoReturn, cast

MAX_CONTRACT_BYTES = 262_144
MAX_MANIFEST_ITEMS = 10_000
MAX_JSONL_BYTES = 16 * 1024 * 1024
MAX_JSONL_ITEMS = 1_000


class RecallContractError(ValueError):
    """A benchmark value is outside its closed, body-free contract."""


def canonical_json(payload: Mapping[str, object] | list[object]) -> str:
    """Serialize one JSON value with the benchmark's only accepted encoding."""

    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(payload: Mapping[str, object] | list[object]) -> bytes:
    return canonical_json(payload).encode("ascii")


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RecallContractError("canonical JSON contains a duplicate key")
        result[key] = value
    return result


def _reject_float(_value: str) -> NoReturn:
    raise RecallContractError("canonical JSON numbers must be bounded integers")


def _reject_constant(_value: str) -> NoReturn:
    raise RecallContractError("canonical JSON contains a non-finite number")


def _bounded_json_int(value: str) -> int:
    if len(value) > 20:
        raise RecallContractError("canonical JSON integer exceeds the lexical bound")
    return int(value)


def _validate_json_tree(value: object, *, depth: int = 0) -> None:
    if depth > 32:
        raise RecallContractError("canonical JSON exceeds the nesting bound")
    if value is None or type(value) in {bool, int}:
        return
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise RecallContractError("canonical JSON contains invalid Unicode") from exc
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise RecallContractError("canonical JSON contains control or surrogate text")
        return
    if type(value) is list:
        for item in cast(list[object], value):
            _validate_json_tree(item, depth=depth + 1)
        return
    if type(value) is dict:
        for key, item in cast(dict[object, object], value).items():
            if not isinstance(key, str):
                raise RecallContractError("canonical JSON object keys must be text")
            _validate_json_tree(key, depth=depth + 1)
            _validate_json_tree(item, depth=depth + 1)
        return
    raise RecallContractError("canonical JSON contains an unsupported value")


def parse_canonical_json(
    value: str | bytes,
    *,
    label: str,
    maximum_bytes: int = MAX_CONTRACT_BYTES,
) -> object:
    """Parse exact ASCII canonical JSON, rejecting ambiguous spellings."""

    if type(value) is bytes:
        raw = value
        try:
            text = raw.decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise RecallContractError(f"{label} must use canonical ASCII JSON") from exc
    elif type(value) is str:
        text = value
        try:
            raw = text.encode("ascii", errors="strict")
        except UnicodeEncodeError as exc:
            raise RecallContractError(f"{label} must use canonical ASCII JSON") from exc
    else:
        raise RecallContractError(f"{label} must be canonical JSON text")
    if not raw or len(raw) > maximum_bytes or text != text.strip():
        raise RecallContractError(f"{label} exceeds its canonical byte contract")
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_duplicate_safe_object,
            parse_float=_reject_float,
            parse_int=_bounded_json_int,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RecallContractError(f"{label} is not one canonical JSON value") from exc
    try:
        _validate_json_tree(parsed)
        encoded = canonical_json(cast(Any, parsed))
    except RecursionError as exc:
        raise RecallContractError(f"{label} exceeds the nesting bound") from exc
    if text != encoded:
        raise RecallContractError(f"{label} is not canonical JSON")
    return parsed


def exact_object(value: object, keys: frozenset[str], *, label: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(cast(dict[object, object], value)) != keys:
        raise RecallContractError(f"{label} keys do not match the closed contract")
    return cast(dict[str, object], value)


def bounded_text(value: object, *, label: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RecallContractError(f"{label} must be non-empty canonical text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RecallContractError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum_bytes or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise RecallContractError(f"{label} exceeds its closed text contract")
    return value


def bounded_int(value: object, *, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RecallContractError(f"{label} is outside its closed integer range")
    return value


def bounded_optional_int(
    value: object,
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None:
        return None
    return bounded_int(value, label=label, minimum=minimum, maximum=maximum)


def sha256_text(value: object, *, label: str) -> str:
    text = bounded_text(value, label=label, maximum_bytes=64)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise RecallContractError(f"{label} must be a lowercase SHA-256 digest")
    return text


def digest_payload(domain: bytes, payload: Mapping[str, object] | list[object]) -> str:
    return hashlib.sha256(domain + b"\0" + canonical_bytes(payload)).hexdigest()


def canonical_manifest_sha256(domain: bytes, values: Iterable[str]) -> str:
    try:
        items = tuple(islice(iter(values), MAX_MANIFEST_ITEMS + 1))
    except Exception as exc:
        raise RecallContractError("manifest must be a bounded iterable") from exc
    if len(items) > MAX_MANIFEST_ITEMS or any(type(item) is not str for item in items):
        raise RecallContractError("manifest exceeds its closed item contract")
    if items != tuple(sorted(items)) or len(items) != len(set(items)):
        raise RecallContractError("manifest items must be sorted and unique")
    return digest_payload(domain, list(items))
