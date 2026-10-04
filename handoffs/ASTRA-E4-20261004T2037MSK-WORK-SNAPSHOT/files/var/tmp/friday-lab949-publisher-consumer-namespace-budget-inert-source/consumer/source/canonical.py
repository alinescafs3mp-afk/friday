"""Canonical JSON and domain-separated digests. Parsing requires canonical bytes."""

from resource_meter import HashlibProxy
hashlib = HashlibProxy()
import json

from contract import MAX_DEPTH, MAX_DOCUMENT_BYTES, MAX_ITEMS, MAX_STRING, ContractError
from resource_meter import canonical_bound, json_preflight, checkpoint, reserve_allocation

PROJECTION_DOMAIN = "friday.lab815.projection.v1"
CANONICAL_DOMAIN = "friday.lab815.canonical.v1"


def canonical_bytes(document):
    canonical_bound(document)
    try:
        text = json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, RecursionError, MemoryError) as exc:
        raise ContractError("canonical_encoding") from exc
    return (text + "\n").encode("ascii")


def require_canonical(raw, value):
    if raw != canonical_bytes(value):
        raise ContractError("not_canonical")
    return value


def domain_digest(domain, document):
    if type(domain) is not str or not domain.isascii() or not 1 <= len(domain) <= 128:
        raise ContractError("domain")
    if any(character.isspace() for character in domain):
        raise ContractError("domain")
    body = canonical_bytes(document)
    return hashlib.sha256(domain.encode("ascii") + b"\0" + body).hexdigest()


def projection_digest(envelope):
    if type(envelope) is not dict:
        raise ContractError("envelope")
    body = {key: value for key, value in envelope.items() if key != "projection_digest"}
    return domain_digest(PROJECTION_DOMAIN, body)


def _pairs(pairs):
    seen = set()
    document = {}
    for key, value in pairs:
        if key in seen:
            raise ContractError("duplicate_key")
        seen.add(key)
        document[key] = value
    return document


def _reject_constant(_name):
    raise ContractError("nonfinite")


def _walk(value, depth, max_depth, max_items, max_string):
    if depth > max_depth:
        raise ContractError("depth")
    if type(value) is dict:
        reserve_allocation(32 + len(value) * 16)
        if len(value) > max_items:
            raise ContractError("count")
        for key, item in value.items():
            if type(key) is not str or len(key) > max_string:
                raise ContractError("key")
            _walk(item, depth + 1, max_depth, max_items, max_string)
        return
    if type(value) is list:
        reserve_allocation(32 + len(value) * 16)
        if len(value) > max_items:
            raise ContractError("count")
        for item in value:
            _walk(item, depth + 1, max_depth, max_items, max_string)
        return
    if type(value) is str and len(value) > max_string:
        raise ContractError("string_length")
    if type(value) not in (str, int, bool, type(None)):
        raise ContractError("json_type")


def parse_exact(text, max_bytes=MAX_DOCUMENT_BYTES, max_depth=MAX_DEPTH, max_items=MAX_ITEMS, max_string=MAX_STRING):
    if type(text) is str:
        if len(text)>max_bytes: raise ContractError('document_size')
        reserve_allocation(len(text)*4+1)
        raw = text.encode("utf-8")
    elif type(text) is bytes:
        raw = text
    else:
        raise ContractError("document_type")
    if len(raw) > max_bytes:
        raise ContractError("document_size")
    json_preflight(raw, max_bytes, max_depth, max_items, max_string)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except ContractError:
        raise
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError, MemoryError) as exc:
        raise ContractError("json") from exc
    _walk(value, 1, max_depth, max_items, max_string)
    return require_canonical(raw, value)
