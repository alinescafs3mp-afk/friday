"""Presented and expected binding sets. MISMATCH and null material refuse."""

from canonical import canonical_bytes, domain_digest
from contract import ContractError, is_digest
from schema_validate import validate_document
from semantics import validate_binding_semantics

_FIELDS = (
    "filename",
    "member_name",
    "archive_format",
    "architecture",
    "platform",
    "version",
    "abi",
    "size",
    "sha256",
    "resource_role",
)


def _identity(record):
    return (record["producer_id"], record["consumer_id"], record["filename"])


def refuse_incomplete(record, issuer_id):
    validate_binding_semantics(record)
    evidence = record["compatibility_evidence"]
    if evidence["statement"] == "NOT_PROVEN":
        return "compatibility_evidence_absent"
    if evidence["issuer_id"] != issuer_id or not is_digest(evidence["evidence_sha256"]):
        raise ContractError("evidence_issuer")
    if record["member_name"] in (None, ""):
        raise ContractError("material_incomplete")
    return ""


def bind_producer_consumer(presented, expected, schema, issuer_id):
    if presented is expected:
        raise ContractError("expected_bound_to_itself")
    validate_document(presented, schema)
    validate_document(expected, schema)
    if presented["bill_sha256"] != expected["bill_sha256"]:
        raise ContractError("bill_identity")
    if presented["statement"] != expected["statement"]:
        raise ContractError("statement_mismatch")
    if presented["statement"] == "MISMATCH" or expected["statement"] == "MISMATCH":
        raise ContractError("compatibility_mismatch")
    expected_rows = {}
    for record in expected["records"]:
        key = _identity(record)
        if key in expected_rows:
            raise ContractError("duplicate_expected_binding")
        expected_rows[key] = record
    presented_rows = {}
    for record in presented["records"]:
        key = _identity(record)
        if key in presented_rows:
            raise ContractError("duplicate_binding")
        presented_rows[key] = record
    if set(expected_rows) != set(presented_rows):
        raise ContractError("binding_set")
    unproven = 0
    for key, want in expected_rows.items():
        have = presented_rows[key]
        refuse_incomplete(want, issuer_id)
        cause = refuse_incomplete(have, issuer_id)
        for field in _FIELDS:
            if have[field] != want[field]:
                raise ContractError("binding_field")
        if have["compatibility_evidence"] != want["compatibility_evidence"]:
            raise ContractError("compatibility_evidence")
        if have["origin_id"] == want["origin_id"]:
            raise ContractError("expected_bound_to_itself")
        if cause == "compatibility_evidence_absent":
            unproven += 1
    if unproven:
        return {
            "status": "NOT_PROVEN",
            "cause": "compatibility_evidence_absent",
            "compared": len(expected_rows),
            "publisher_proof": False,
            "effects_denied": True,
        }
    return {
        "status": "STRUCTURAL_COMPARED",
        "cause": "facts_aligned",
        "compared": len(expected_rows),
        "publisher_proof": False,
        "effects_denied": True,
    }
