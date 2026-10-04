"""Independent bounded ingress. Presented bytes cannot select authority."""

from resource_meter import HashlibProxy
hashlib = HashlibProxy()
from pathlib import Path

from canonical import parse_exact
from resource_meter import bounded_file
from contract import META_SCHEMA_DEPTH, ContractError, is_digest
from pins import SCHEMA_PINS
from schema_validate import compile_schema, validate_document

_ROOT = Path(__file__).resolve().parent.parent
_AUTHORITY_KEYS = (
    "approved_fingerprint",
    "approved_issuer_id",
    "expected_result_sha256",
    "node_authoritative_sha256",
    "node_signer_sha256",
    "root_issuer_sha256",
    "schema_pin",
    "signer_fingerprint",
)


def load_pinned_schema(schema_id):
    if type(schema_id) is not str or schema_id not in SCHEMA_PINS:
        raise ContractError("schema_unknown")
    path = _ROOT / "schemas" / (schema_id + ".json")
    try:
        raw = bounded_file(path, 400000)
    except OSError as exc:
        raise ContractError("schema_pin") from exc
    pin = SCHEMA_PINS[schema_id]
    if not is_digest(pin) or hashlib.sha256(raw).hexdigest() != pin:
        raise ContractError("schema_pin")
    parsed = parse_exact(raw, max_bytes=400000, max_depth=META_SCHEMA_DEPTH, max_items=512, max_string=400)
    return compile_schema(parsed)


def admit_document(raw, schema_id):
    if type(raw) is not bytes:
        raise ContractError("document_type")
    schema = load_pinned_schema(schema_id)
    document = parse_exact(
        raw,
        max_bytes=schema.get("body_max_bytes", 2000000),
        max_depth=schema["max_depth"],
        max_items=schema.get("body_max_items", 512),
        max_string=schema.get("body_max_string", 512),
    )
    validate_document(document, schema)
    return document


def admit_ingress(authority_raw, expected_raw, presented_raw):
    authority = admit_document(authority_raw, "friday.lab820.authority-ingress.v1")
    if authority["schema_pin"] != SCHEMA_PINS["friday.lab820.expected-bill.v1"]:
        raise ContractError("schema_pin")
    if authority["effects_granted"] is not False or authority["produced_by_this_package"] is not False:
        raise ContractError("presented_authority")
    digest = authority["node_authoritative_sha256"]
    fingerprint = authority["signer_fingerprint"]
    if digest is not None and not is_digest(digest):
        raise ContractError("node_authority")
    if fingerprint is not None and len(fingerprint) != 40:
        raise ContractError("fingerprint")
    expected = admit_document(expected_raw, "friday.lab820.expected-bill.v1")
    if type(presented_raw) is not bytes:
        raise ContractError("document_type")
    peeked = parse_exact(presented_raw, max_bytes=2000000, max_depth=12, max_items=512, max_string=8000)
    if type(peeked) is dict and any(key in peeked for key in _AUTHORITY_KEYS):
        raise ContractError("presented_authority")
    presented = admit_document(presented_raw, "friday.lab820.presented-observation.v1")
    return authority, expected, presented
