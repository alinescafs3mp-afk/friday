"""Bounded canonical authority JSON. This module has no filesystem effects."""
import hashlib
import json
import math
import re

MAX_DOCUMENT_BYTES = 2_097_152
MAX_DEPTH = 32
MAX_ITEMS = 50_000
HARD_MAX_DOCUMENT_BYTES = 64 << 20
HARD_MAX_ITEMS = 10_000_000
MAX_PATH_BYTES = 4096
_SURROGATE = re.compile("[\ud800-\udfff]")


class ContractError(ValueError):
    """An authority, identity or safety contract could not be proved."""


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ContractError("exact object keys required")
    return value


def validate_digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContractError("lowercase SHA256 required")
    return value


def validate_integer(value, minimum=0, maximum=2**63 - 1):
    if type(minimum) is not int or type(maximum) is not int or minimum > maximum or type(value) is not int or not minimum <= value <= maximum:
        raise ContractError("bounded integer required")
    return value


def validate_path(value, absolute=False):
    if type(absolute) is not bool:
        raise ContractError("exact absolute-path boolean required")
    if type(value) is not str or not value or len(value) > MAX_PATH_BYTES:
        raise ContractError("bounded nonempty path required")
    try:
        value.encode("ascii")
    except UnicodeError as exc:
        raise ContractError("ASCII path required") from exc
    if any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in value):
        raise ContractError("unsafe path byte")
    if absolute != value.startswith("/") or value == "/":
        raise ContractError("path absolute/relative mismatch")
    components = value[1:].split("/") if absolute else value.split("/")
    if len(components) > MAX_DEPTH or any(c in ("", ".", "..") or len(c) > 255 for c in components):
        raise ContractError("unsafe path component")
    return value


def _values(value, depth=0):
    if depth > MAX_DEPTH:
        raise ContractError("value depth limit")
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise ContractError("JSON keys must be strings")
            _values(key, depth + 1)
            _values(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _values(item, depth + 1)
    elif type(value) is str:
        if _SURROGATE.search(value) is not None:
            raise ContractError("unpaired surrogate forbidden")
    elif type(value) is float:
        if not math.isfinite(value):
            raise ContractError("non-finite number forbidden")
    elif value is not None and type(value) not in (bool, int):
        raise ContractError("non-JSON value")


def canonical_bytes(value):
    _values(value)
    try:
        return (json.dumps(value, allow_nan=False, ensure_ascii=True,
                           separators=(",", ":"), sort_keys=True) + "\n").encode("ascii")
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise ContractError("invalid canonical value") from exc


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _scan(raw, max_depth, max_items):
    depth = items = 0
    quoted = escaped = False
    for ch in raw:
        if quoted:
            if escaped:
                escaped = False
            elif ch == 92:
                escaped = True
            elif ch == 34:
                quoted = False
        elif ch == 34:
            quoted = True
            items += 1
        elif ch in (123, 91):
            depth += 1
            items += 1
            if depth > max_depth:
                raise ContractError("document depth limit")
        elif ch in (125, 93):
            depth -= 1
        elif ch == 44:
            items += 1
        if items > max_items:
            raise ContractError("document item limit")


def parse_canonical_object(raw, keys, *, schema=None, expected_sha256=None,
                           max_bytes=MAX_DOCUMENT_BYTES, max_depth=MAX_DEPTH,
                           max_items=MAX_ITEMS):
    validate_integer(max_bytes, minimum=1, maximum=HARD_MAX_DOCUMENT_BYTES)
    validate_integer(max_depth, minimum=1, maximum=MAX_DEPTH)
    validate_integer(max_items, minimum=1, maximum=HARD_MAX_ITEMS)
    if type(raw) is not bytes or not raw or len(raw) > max_bytes:
        raise ContractError("document byte limit/type")
    if expected_sha256 is not None:
        validate_digest(expected_sha256)
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ContractError("external document digest mismatch")
    try:
        raw.decode("ascii")
    except UnicodeError as exc:
        raise ContractError("document must be ASCII") from exc
    _scan(raw, max_depth, max_items)

    def pairs(sequence):
        result = {}
        for key, value in sequence:
            if key in result:
                raise ContractError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value):
        raise ContractError("non-finite JSON number")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("invalid authority JSON") from exc
    exact_keys(value, keys)
    if schema is not None and value.get("schema") != schema:
        raise ContractError("schema mismatch")
    if canonical_bytes(value) != raw:
        raise ContractError("noncanonical document bytes")
    return value


def parse_authenticated_object(raw, keys, *, expected_sha256, **options):
    """Authority API: missing/null/aliased digests never downgrade to parsing."""
    validate_digest(expected_sha256)
    return parse_canonical_object(raw, keys, expected_sha256=expected_sha256, **options)


EFFECT_BILL_KEYS = ("schema", "bill_id", "version", "scope", "allowed_effects",
                    "forbidden_effects", "required_authority", "implies")


def parse_effect_bill(raw, expected_sha256, expected_bill_id, *, expected_version=1):
    validate_digest(expected_sha256)
    validate_integer(expected_version, minimum=1, maximum=2)
    if type(expected_bill_id) is not str or not expected_bill_id or len(expected_bill_id) > 256:
        raise ContractError("exact expected bill identity required")
    value = parse_canonical_object(raw, EFFECT_BILL_KEYS, schema="friday.effect-bill.v1",
                                   expected_sha256=expected_sha256)
    if value["bill_id"] != expected_bill_id or value["version"] != expected_version or type(value["version"]) is not int:
        raise ContractError("effect bill identity/version")
    if type(value["required_authority"]) is not str or not value["required_authority"] or len(value["required_authority"]) > 256:
        raise ContractError("effect bill authority")
    for key in ("scope", "allowed_effects", "forbidden_effects", "implies"):
        sequence = value[key]
        if type(sequence) is not list or len(sequence) > 256 or any(type(item) is not str or not item or len(item) > 256 for item in sequence):
            raise ContractError("effect bill list")
        if sequence != sorted(set(sequence)):
            raise ContractError("effect bill list order/duplicate")
    if value["implies"] or set(value["allowed_effects"]) & set(value["forbidden_effects"]):
        raise ContractError("effect bills imply no other authority")
    if not value["allowed_effects"] or not value["forbidden_effects"] or not value["scope"]:
        raise ContractError("effect bill incomplete")
    return value
