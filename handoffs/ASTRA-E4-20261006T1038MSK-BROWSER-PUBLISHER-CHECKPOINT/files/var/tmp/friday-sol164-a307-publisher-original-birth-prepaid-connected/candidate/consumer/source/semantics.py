"""Cross-field correspondence that the exact schema shape cannot express alone."""

from contract import ContractError, is_digest, is_sha512


def validate_receipt_semantics(receipt):
    if receipt["publisher_proof"] is not False or receipt["effects_denied"] is not True:
        raise ContractError("receipt_credit")
    header = receipt["hash_header"]
    digest = receipt["hash_header_digest"]
    body = receipt["signed_body_sha256"]
    if body is not None and not is_digest(body):
        raise ContractError("signed_body_width")
    if header == "SHA512":
        if not is_sha512(digest):
            raise ContractError("hash_correspondence")
        if body is not None and len(body) != 64:
            raise ContractError("signed_body_width")
    elif header == "SHA256":
        if digest != body or not is_digest(digest):
            raise ContractError("hash_correspondence")
    elif header is None:
        if digest is not None:
            raise ContractError("hash_correspondence")
    else:
        raise ContractError("unsupported_hash")
    if receipt["proof_status"] in ("STRUCTURALLY_BOUND", "AUTHENTICATED"):
        for key in ("raw_document_sha256", "signed_body_sha256", "signature_armor_sha256"):
            if not is_digest(receipt[key]):
                raise ContractError("receipt_binding")
        if receipt["proof_status"] == "AUTHENTICATED" and (receipt["signature_verified"] is not True or not is_digest(receipt["verification_result_sha256"])):
            raise ContractError("verification_result_absent")
        if receipt["signature_verified"] is True and receipt["verification_result_sha256"] is None:
            raise ContractError("verification_result_absent")
    if receipt["proof_status"] == "NOT_PROVEN" and receipt["signature_verified"] is True:
        raise ContractError("verification_result_absent")
    return receipt


def validate_binding_semantics(record):
    evidence = record["compatibility_evidence"]
    statement = evidence["statement"]
    if statement == "MISMATCH":
        raise ContractError("compatibility_mismatch")
    if statement not in ("MATCH", "NOT_PROVEN"):
        raise ContractError("statement")
    if record["size"] is None or record["sha256"] is None:
        raise ContractError("material_incomplete")
    if statement == "MATCH":
        if evidence["issuer_id"] is None or evidence["evidence_sha256"] is None:
            raise ContractError("evidence_issuer")
        if evidence["target_sha256"] != record["sha256"]:
            raise ContractError("evidence_target")
    return record
