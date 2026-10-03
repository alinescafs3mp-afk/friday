"""Ubuntu and Node receipt chains. Missing authority stays pending after real parsing."""

from resource_meter import HashlibProxy
hashlib = HashlibProxy()

from canonical import domain_digest
from capability import authenticate_capability
from contract import ContractError, is_digest
from formats import parse_clearsign, parse_release, select_package, select_shasum
from pins import (
    NODE_DIAGNOSTIC_SHA256,
    NODE_FILENAME,
    NODE_SIZE,
    RESOLUTE_UPDATES_INRELEASE_HISTORICAL,
    RESOLUTE_UPDATES_INRELEASE_OBSERVED,
)
from schema_validate import validate_document
from semantics import validate_receipt_semantics

_RECEIPT_SCHEMA = "friday.lab815.raw-publisher-receipt.v1"


def retain_expected_digest(expected_sha256, observed_sha256):
    if not is_digest(expected_sha256) or not is_digest(observed_sha256):
        raise ContractError("digest")
    if observed_sha256 != expected_sha256:
        return {
            "status": "NOT_PROVEN",
            "adopted_sha256": expected_sha256,
            "replaced": False,
            "cause": "host_bytes_differ",
            "publisher_proof": False,
        }
    return {
        "status": "PIN_MATCH",
        "adopted_sha256": expected_sha256,
        "replaced": False,
        "cause": "pin_retained",
        "publisher_proof": False,
    }


def compare_archive(archive, expected_sha, expected_size):
    if archive is None:
        return {
            "status": "NOT_PROVEN",
            "cause": "archive_bytes_absent",
            "archive_sha256": None,
            "publisher_proof": False,
        }
    if type(archive) is not bytes:
        raise ContractError("archive_type")
    digest = hashlib.sha256(archive).hexdigest()
    if digest == NODE_DIAGNOSTIC_SHA256 and expected_sha is None:
        return {
            "status": "NOT_PROVEN",
            "cause": "diagnostic_not_authority",
            "archive_sha256": digest,
            "publisher_proof": False,
        }
    if expected_sha is None or expected_size is None:
        return {
            "status": "NOT_PROVEN",
            "cause": "authoritative_checksum_absent",
            "archive_sha256": digest,
            "publisher_proof": False,
        }
    if len(archive) != expected_size:
        raise ContractError("archive_size")
    if digest != expected_sha:
        raise ContractError("archive_checksum_mismatch")
    return {
        "status": "STRUCTURALLY_BOUND",
        "cause": "archive_bound",
        "archive_sha256": digest,
        "publisher_proof": False,
    }


def select_authoritative_row(document, filename):
    if type(document) is not dict or type(document.get("rows")) is not list or not document["rows"]:
        raise ContractError("authoritative_document")
    found = None
    for row in document["rows"]:
        if type(row) is not dict:
            raise ContractError("authoritative_row")
        if row.get("filename") != filename:
            continue
        if found is not None:
            raise ContractError("duplicate_shasum")
        if not is_digest(row.get("sha256")) or type(row.get("size")) is not int or isinstance(row.get("size"), bool):
            raise ContractError("authoritative_row")
        found = row
    if found is None:
        raise ContractError("shasum_missing")
    return found


def _blank_receipt(kind, capability, cause, proof_status):
    return {
        "schema": _RECEIPT_SCHEMA,
        "document_kind": kind,
        "proof_status": proof_status,
        "cause": cause,
        "publisher_proof": False,
        "effects_denied": True,
        "signature_verified": False,
        "verified_by_tool": capability["verified_by_tool"],
        "algorithm_status": "NOT_PROVEN",
        "hash_header": None,
        "hash_header_digest": None,
        "signed_body_sha256": None,
        "raw_document_sha256": None,
        "signature_armor_sha256": None,
        "verification_result_sha256": None,
        "archive_filename": None,
        "archive_sha256": None,
        "archive_size": None,
        "archive_authoritative": False,
        "member_name": None,
        "member_sha256": None,
        "member_size": None,
        "verifier_capability": capability,
    }


_HEADER_CLASS = {"SHA256": "openpgp-sha256", "SHA512": "openpgp-sha512"}


def correspond_algorithms(parsed, capability, result):
    if parsed is None:
        return
    header_class = _HEADER_CLASS.get(parsed["hash_header"])
    if header_class is None or header_class != capability["algorithm_class"]:
        raise ContractError("algorithm_correspondence")
    if result is not None and result["algorithm"] != capability["algorithm_class"]:
        raise ContractError("algorithm_correspondence")
    if result is not None and result["algorithm"] != header_class:
        raise ContractError("algorithm_correspondence")


def _set_archive_size(receipt, archive_bytes, archive_decision, archive_pin):
    if archive_bytes is not None:
        receipt["archive_size"] = len(archive_bytes)
        return
    if archive_decision.get("status") == "STRUCTURALLY_BOUND" and archive_pin is not None:
        size = archive_pin.get("size")
        if type(size) is int and not isinstance(size, bool):
            receipt["archive_size"] = size


def _finish(receipt, schema):
    validate_receipt_semantics(receipt)
    validate_document(receipt, schema)
    return receipt


def compare_archive_pin(pin_sha, pin_size, expected_sha, expected_size):
    if pin_sha is None or pin_size is None:
        return {
            "status": "NOT_PROVEN",
            "cause": "archive_bytes_absent",
            "archive_sha256": None,
            "publisher_proof": False,
        }
    if not is_digest(pin_sha) or type(pin_size) is not int or isinstance(pin_size, bool):
        raise ContractError("archive_type")
    if pin_sha == NODE_DIAGNOSTIC_SHA256 and expected_sha is None:
        if pin_size != NODE_SIZE:
            raise ContractError("archive_size")
        return {
            "status": "NOT_PROVEN",
            "cause": "diagnostic_not_authority",
            "archive_sha256": pin_sha,
            "publisher_proof": False,
        }
    if expected_sha is None or expected_size is None:
        return {
            "status": "NOT_PROVEN",
            "cause": "authoritative_checksum_absent",
            "archive_sha256": pin_sha,
            "publisher_proof": False,
        }
    if pin_size != expected_size:
        raise ContractError("archive_size")
    if pin_sha != expected_sha:
        raise ContractError("archive_checksum_mismatch")
    return {
        "status": "STRUCTURALLY_BOUND",
        "cause": "archive_bound",
        "archive_sha256": pin_sha,
        "publisher_proof": False,
    }


def _bind_expected_result(decision, receipt, expected_result_sha256):
    if decision["status"] != "AUTHENTICATED":
        return decision
    actual = receipt.get("verification_result_sha256")
    if expected_result_sha256 is None:
        return {
            "status": "NOT_PROVEN",
            "cause": "independent_result_absent",
            "publisher_proof": False,
            "effects_denied": True,
            "verified_by_tool": decision.get("verified_by_tool") is True,
        }
    if actual != expected_result_sha256:
        raise ContractError("result_binding")
    return decision


def assess_ubuntu_chain(
    capability,
    expected,
    approved_fingerprint,
    inrelease_bytes,
    packages_bytes,
    archive_bytes,
    release_expected,
    package_expected,
    capability_schema,
    result_schema,
    receipt_schema,
    result=None,
    archive_pin=None,
    expected_result_sha256=None,
):
    if type(capability) is dict and capability.get("document_kind") != "ubuntu-inrelease":
        raise ContractError("document_kind")
    parsed = parse_clearsign(inrelease_bytes) if type(inrelease_bytes) is bytes else None
    decision = authenticate_capability(
        capability,
        expected,
        result,
        inrelease_bytes if parsed is not None else None,
        parsed["normalized_body"] if parsed is not None else None,
        parsed["signature_armor"] if parsed is not None else None,
        archive_bytes,
        capability_schema,
        result_schema,
        approved_fingerprint,
    )
    receipt = _blank_receipt("ubuntu-inrelease", capability, decision["cause"], "NOT_PROVEN")
    receipt["verified_by_tool"] = decision.get("verified_by_tool") is True
    if parsed is not None:
        receipt["raw_document_sha256"] = parsed["raw_document_sha256"]
        receipt["signed_body_sha256"] = parsed["signed_body_sha256"]
        receipt["signature_armor_sha256"] = parsed["signature_armor_sha256"]
        receipt["hash_header"] = parsed["hash_header"]
        receipt["hash_header_digest"] = parsed["hash_header_digest"]
        receipt["algorithm_status"] = parsed["algorithm_status"]
        correspond_algorithms(parsed, capability, result)
        if release_expected is not None:
            selected = parse_release(parsed["cleartext"], release_expected)
            receipt["member_name"] = selected["member_name"]
            receipt["member_sha256"] = selected["packages_sha256"]
            receipt["member_size"] = selected["size"]
    if type(packages_bytes) is bytes and package_expected is not None:
        chosen = select_package(packages_bytes, package_expected)
        if receipt["member_sha256"] is not None and hashlib.sha256(packages_bytes).hexdigest() != receipt["member_sha256"]:
            raise ContractError("member_identity")
        receipt["archive_filename"] = chosen["filename"]
    expected_sha = package_expected["sha256"] if package_expected is not None else None
    expected_size = package_expected["size"] if package_expected is not None else None
    if archive_bytes is None and archive_pin is not None:
        archive_decision = compare_archive_pin(archive_pin.get("sha256"), archive_pin.get("size"), expected_sha, expected_size)
    else:
        archive_decision = compare_archive(archive_bytes, expected_sha, expected_size)
    receipt["archive_sha256"] = archive_decision["archive_sha256"]
    _set_archive_size(receipt, archive_bytes, archive_decision, archive_pin)
    if result is not None:
        receipt["verification_result_sha256"] = domain_digest("friday.lab815.verification-result.v1", result)
    decision = _bind_expected_result(decision, receipt, expected_result_sha256)
    retained = retain_expected_digest(RESOLUTE_UPDATES_INRELEASE_HISTORICAL, RESOLUTE_UPDATES_INRELEASE_OBSERVED)
    if retained["replaced"] is not False or retained["adopted_sha256"] != RESOLUTE_UPDATES_INRELEASE_HISTORICAL:
        raise ContractError("historical_pin_replaced")
    member_ready = (
        receipt["member_sha256"] is not None
        and type(packages_bytes) is bytes
        and hashlib.sha256(packages_bytes).hexdigest() == receipt["member_sha256"]
        and receipt["member_size"] == len(packages_bytes)
        and receipt["archive_filename"] is not None
    )
    structural = (
        decision["status"] in ("STRUCTURALLY_BOUND", "AUTHENTICATED")
        and archive_decision["status"] == "STRUCTURALLY_BOUND"
        and member_ready
        and receipt["signed_body_sha256"] is not None
    )
    if structural and decision["status"] == "AUTHENTICATED":
        receipt["proof_status"] = "AUTHENTICATED"
        receipt["cause"] = "ubuntu_authenticated"
        receipt["signature_verified"] = True
        receipt["archive_authoritative"] = True
    elif structural:
        receipt["proof_status"] = "STRUCTURALLY_BOUND"
        receipt["cause"] = "ubuntu_structurally_bound"
    else:
        receipt["proof_status"] = "NOT_PROVEN"
        if decision["cause"]:
            receipt["cause"] = decision["cause"]
    return _finish(receipt, receipt_schema)


def assess_node_chain(
    capability,
    expected,
    approved_fingerprint,
    authoritative,
    clearsign_bytes,
    archive_bytes,
    capability_schema,
    result_schema,
    receipt_schema,
    result=None,
    archive_pin=None,
    expected_result_sha256=None,
    authority_archive_sha256=None,
):
    if type(capability) is dict and capability.get("document_kind") != "node-shasums256":
        raise ContractError("document_kind")
    if authoritative is not None and authority_archive_sha256 is not None:
        bound_row = select_authoritative_row(authoritative, NODE_FILENAME)
        if bound_row["size"] != NODE_SIZE:
            raise ContractError("archive_size")
        if bound_row["sha256"] != authority_archive_sha256:
            raise ContractError("node_authority")
    if approved_fingerprint is not None and len(approved_fingerprint) != 40:
        raise ContractError("fingerprint")
    parsed = parse_clearsign(clearsign_bytes) if type(clearsign_bytes) is bytes else None
    decision = authenticate_capability(
        capability,
        expected,
        result,
        clearsign_bytes if parsed is not None else None,
        parsed["normalized_body"] if parsed is not None else None,
        parsed["signature_armor"] if parsed is not None else None,
        archive_bytes,
        capability_schema,
        result_schema,
        approved_fingerprint,
    )
    receipt = _blank_receipt("node-shasums256", capability, decision["cause"], "NOT_PROVEN")
    receipt["verified_by_tool"] = decision.get("verified_by_tool") is True
    receipt["archive_filename"] = NODE_FILENAME
    selected = None
    if parsed is not None:
        receipt["raw_document_sha256"] = parsed["raw_document_sha256"]
        receipt["signed_body_sha256"] = parsed["signed_body_sha256"]
        receipt["signature_armor_sha256"] = parsed["signature_armor_sha256"]
        receipt["hash_header"] = parsed["hash_header"]
        receipt["hash_header_digest"] = parsed["hash_header_digest"]
        receipt["algorithm_status"] = parsed["algorithm_status"]
        correspond_algorithms(parsed, capability, result)
        authoritative_sha = None
        if authoritative is not None:
            row = select_authoritative_row(authoritative, NODE_FILENAME)
            if row["size"] != NODE_SIZE:
                raise ContractError("archive_size")
            authoritative_sha = row["sha256"]
        selected = select_shasum(parsed["cleartext"], NODE_FILENAME, authoritative_sha)
    expected_sha = selected["line_sha256"] if selected is not None and selected["status"] == "STRUCTURALLY_BOUND" else None
    expected_size = NODE_SIZE if expected_sha is not None else None
    if archive_bytes is None and archive_pin is not None:
        archive_decision = compare_archive_pin(archive_pin.get("sha256"), archive_pin.get("size"), expected_sha, expected_size)
    else:
        archive_decision = compare_archive(archive_bytes, expected_sha, expected_size)
    receipt["archive_sha256"] = archive_decision["archive_sha256"]
    _set_archive_size(receipt, archive_bytes, archive_decision, archive_pin)
    if result is not None:
        receipt["verification_result_sha256"] = domain_digest("friday.lab815.verification-result.v1", result)
    decision = _bind_expected_result(decision, receipt, expected_result_sha256)
    node_ready = selected is not None and selected["status"] == "STRUCTURALLY_BOUND" and authoritative is not None
    if (
        decision["status"] == "AUTHENTICATED"
        and archive_decision["status"] == "STRUCTURALLY_BOUND"
        and node_ready
    ):
        receipt["proof_status"] = "AUTHENTICATED"
        receipt["cause"] = "node_authenticated"
        receipt["signature_verified"] = True
        receipt["archive_authoritative"] = True
    elif decision["status"] in ("STRUCTURALLY_BOUND", "AUTHENTICATED") and archive_decision["status"] == "STRUCTURALLY_BOUND" and node_ready:
        receipt["proof_status"] = "STRUCTURALLY_BOUND"
        receipt["cause"] = "node_structurally_bound"
    else:
        receipt["proof_status"] = "NOT_PROVEN"
        receipt["cause"] = archive_decision["cause"] if selected is not None and decision["status"] == "NOT_PROVEN" else decision["cause"]
        if selected is not None and archive_decision["cause"] == "authoritative_checksum_absent":
            receipt["cause"] = "authoritative_checksum_absent"
    return _finish(receipt, receipt_schema)
