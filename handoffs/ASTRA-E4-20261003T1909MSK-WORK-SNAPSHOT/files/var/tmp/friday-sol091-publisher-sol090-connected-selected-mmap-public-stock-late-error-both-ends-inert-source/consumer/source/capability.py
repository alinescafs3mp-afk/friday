"""External verifier capability bound to hashed bytes and an expected authority."""

from resource_meter import HashlibProxy
hashlib = HashlibProxy()

from canonical import domain_digest
from contract import ContractError, is_digest, is_fingerprint
from pins import (
    A033_GPGV_SHA256,
    NODESOURCE_FINGERPRINT_NOT_AUTHORITY,
    UBUNTU_ARGV,
    UBUNTU_EXECUTABLE,
    UBUNTU_FINGERPRINT,
    UBUNTU_ISSUER_ID,
    UBUNTU_KEYRING_SHA256,
)
from schema_validate import validate_document

_TOKEN = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-/")


def closure_digest(dependencies):
    if type(dependencies) is not list:
        raise ContractError("dependency_inventory")
    rows = []
    seen = set()
    for item in dependencies:
        if type(item) is not dict:
            raise ContractError("dependency_inventory")
        if "path" not in item or "sha256" not in item:
            raise ContractError("dependency_inventory")
        path = item["path"]
        digest = item["sha256"]
        if type(path) is not str or not path or path in seen or not is_digest(digest):
            raise ContractError("dependency_inventory")
        seen.add(path)
        rows.append({
            "custody_sha256": item.get("custody_sha256"),
            "gid": item.get("gid"),
            "mode": item.get("mode"),
            "path": path,
            "sha256": digest,
            "size": item.get("size"),
            "uid": item.get("uid"),
        })
    rows.sort(key=lambda row: row["path"])
    if not rows:
        raise ContractError("dependency_inventory_empty")
    return domain_digest("friday.lab815.dependency-closure.v1", rows)


def _environment_rows(entries):
    if type(entries) is not list:
        raise ContractError("environment_entries")
    rows = []
    seen = set()
    for item in entries:
        if type(item) is not dict:
            raise ContractError("environment_entries")
        name = item.get("name")
        value = item.get("value")
        if type(name) is not str or type(value) is not str or name == "" or name in seen:
            raise ContractError("environment_entries")
        seen.add(name)
        rows.append({"name": name, "value": value})
    rows.sort(key=lambda row: row["name"])
    return rows


def capability_digest(capability):
    body = {key: value for key, value in capability.items() if key != "verified_by_tool"}
    return domain_digest("friday.lab815.capability-body.v1", body)


def _pending(cause):
    return {
        "status": "NOT_PROVEN",
        "cause": cause,
        "publisher_proof": False,
        "effects_denied": True,
        "verified_by_tool": False,
    }


def _bound(cause):
    return {
        "status": "STRUCTURALLY_BOUND",
        "cause": cause,
        "publisher_proof": False,
        "effects_denied": True,
        "verified_by_tool": False,
    }


def _argv_matches(argv, kind):
    if type(argv) is not list or len(argv) != 4:
        raise ContractError("argv")
    if tuple(argv[:3]) != UBUNTU_ARGV or argv[0] != UBUNTU_EXECUTABLE:
        raise ContractError("argv")
    tail = argv[3]
    if type(tail) is not str or not 1 <= len(tail) <= 80 or any(character not in _TOKEN for character in tail):
        raise ContractError("argv_target")
    if kind == "ubuntu-inrelease" and tail != "inrelease":
        raise ContractError("argv_target")
    if kind == "node-shasums256" and tail != "shasums256":
        raise ContractError("argv_target")
    return True


def bind_verification_result(result, capability, raw, body, armor, archive, schema):
    validate_document(result, schema)
    if result["produced_by_this_package"] is not False:
        raise ContractError("self_issued_result")
    if result["issuer_id"] != capability["issuer_attestation"]["issuer_id"]:
        raise ContractError("result_issuer")
    if result["algorithm"] != capability["algorithm_class"]:
        raise ContractError("algorithm_binding")
    if result["argv"] != capability["argv"]:
        raise ContractError("argv")
    if result["environment_digest"] != capability["environment_digest"]:
        raise ContractError("environment_digest")
    if result["keyring_sha256"] != capability["keyring_sha256"]:
        raise ContractError("keyring_mismatch")
    if result["capability_sha256"] != capability_digest(capability):
        raise ContractError("capability_binding")
    if raw is None or body is None or armor is None:
        return _pending("verification_bytes_absent")
    if hashlib.sha256(raw).hexdigest() != result["raw_sha256"]:
        raise ContractError("result_binding")
    if hashlib.sha256(body).hexdigest() != result["body_sha256"]:
        raise ContractError("result_binding")
    if hashlib.sha256(armor).hexdigest() != result["armor_sha256"]:
        raise ContractError("result_binding")
    if archive is None:
        if result["archive_sha256"] is not None or result["archive_size"] is not None:
            raise ContractError("archive_binding")
    elif hashlib.sha256(archive).hexdigest() != result["archive_sha256"] or len(archive) != result["archive_size"]:
        raise ContractError("archive_binding")
    if result["decision"] == "NOT_PROVEN":
        return _pending("result_decision_unproven")
    if result["decision"] not in ("STRUCTURALLY_BOUND", "AUTHENTICATED"):
        raise ContractError("result_decision")
    attested = capability["issuer_attestation"]
    if result["issuer_result_sha256"] is None or result["custody_sha256"] is None or result["attestation_sha256"] is None:
        return _pending("independent_result_absent")
    if result["attestation_sha256"] != attested["attestation_sha256"] or attested["attestation_sha256"] is None:
        raise ContractError("attestation_linkage")
    if result["custody_sha256"] != attested["custody_sha256"] or attested["custody_sha256"] is None:
        raise ContractError("custody_linkage")
    if result["dependency_closure_sha256"] != capability["dependency_closure_sha256"]:
        raise ContractError("dependency_closure")
    if attested["signature_sha256"] is None or result["issuer_result_sha256"] != attested["signature_sha256"]:
        return _pending("signature_unpinned")
    if result["decision"] == "AUTHENTICATED":
        if capability["dependency_status"] != "BOUND":
            return _pending("dependency_not_bound")
        if any(type(item) is not dict or item.get("status") != "BOUND" for item in capability["dependencies"]):
            return _pending("unproven_dependency_claim")
        return {
            "status": "AUTHENTICATED",
            "cause": "result_authenticated",
            "publisher_proof": False,
            "effects_denied": True,
            "verified_by_tool": True,
        }
    return _bound("result_bound")


def authenticate_capability(
    capability,
    expected,
    result,
    raw,
    body,
    armor,
    archive,
    capability_schema,
    result_schema,
    approved_fingerprint,
):
    validate_document(capability, capability_schema)
    if capability["produced_by_this_package"] is not False:
        raise ContractError("self_issued_capability")
    if capability["signer_supplied_by_candidate"] is not False:
        raise ContractError("candidate_signer")
    kind = capability["document_kind"]
    if capability["algorithm_class"] not in ("openpgp-sha256", "openpgp-sha512"):
        raise ContractError("algorithm_class")
    if capability["key_fingerprint"] == NODESOURCE_FINGERPRINT_NOT_AUTHORITY:
        raise ContractError("nodesource_not_this_archive")
    if not is_fingerprint(capability["key_fingerprint"]):
        raise ContractError("fingerprint")
    _argv_matches(capability["argv"], kind)
    if capability["executable"] != capability["argv"][0]:
        raise ContractError("executable_binding")
    environment_digest = domain_digest("friday.lab815.environment.v1", _environment_rows(capability["environment_entries"]))
    if capability["environment_digest"] != environment_digest:
        raise ContractError("environment_digest")
    attested = capability["issuer_attestation"]
    if attested["produced_by_this_package"] is not False:
        raise ContractError("self_issued_attestation")
    if kind == "ubuntu-inrelease":
        if capability["key_fingerprint"] != UBUNTU_FINGERPRINT:
            raise ContractError("fingerprint_mismatch")
        if capability["keyring_sha256"] != UBUNTU_KEYRING_SHA256:
            raise ContractError("keyring_mismatch")
        if attested["issuer_id"] != UBUNTU_ISSUER_ID:
            raise ContractError("issuer_mismatch")
        if capability["executable_sha256"] not in (None, A033_GPGV_SHA256):
            raise ContractError("executable_pin")
    elif kind != "node-shasums256":
        raise ContractError("document_kind")
    if capability["dependency_status"] == "BOUND" and not capability["dependencies"]:
        raise ContractError("unproven_dependency_claim")
    if capability["dependencies"]:
        digested = closure_digest(capability["dependencies"])
        if capability["dependency_closure_sha256"] != digested:
            raise ContractError("dependency_closure")
    elif capability["dependency_closure_sha256"] is not None or capability["dependency_status"] == "BOUND":
        raise ContractError("dependency_inventory_empty")
    if capability["verified_by_tool"] is True and result is None:
        return _pending("tool_flag_without_result")
    linked = None
    if result is not None:
        linked = bind_verification_result(result, capability, raw, body, armor, archive, result_schema)
        if linked["status"] not in ("STRUCTURALLY_BOUND", "AUTHENTICATED"):
            return linked
    if expected is None:
        return _pending("expected_authority_unpinned")
    validate_document(expected, capability_schema)
    for key in (
        "algorithm_class",
        "argv",
        "dependency_closure_sha256",
        "dependency_status",
        "document_kind",
        "environment_digest",
        "executable_sha256",
        "key_fingerprint",
        "keyring_sha256",
    ):
        if expected[key] != capability[key]:
            raise ContractError("expected_capability")
    if expected["executable"] != capability["executable"]:
        raise ContractError("executable_binding")
    if expected["environment_entries"] != capability["environment_entries"]:
        raise ContractError("environment_entries")
    if expected["dependencies"] != capability["dependencies"]:
        raise ContractError("dependency_inventory")
    expected_attestation = expected["issuer_attestation"]
    for key in ("attestation_sha256", "custody_sha256", "issuer_id", "signature_sha256"):
        if expected_attestation[key] != attested[key]:
            raise ContractError("attestation_linkage")
    if expected_attestation["issuer_id"] != attested["issuer_id"]:
        raise ContractError("issuer_mismatch")
    if kind == "node-shasums256":
        if approved_fingerprint is None:
            return _pending("node_signer_unproven")
        if capability["key_fingerprint"] != approved_fingerprint:
            raise ContractError("fingerprint_mismatch")
    if capability["executable_sha256"] is None or capability["dependency_closure_sha256"] is None:
        return _pending("verifier_material_absent")
    if linked is None:
        return _pending("verification_bytes_absent")
    if linked["status"] == "AUTHENTICATED":
        if capability["dependency_status"] != "BOUND":
            return _pending("dependency_not_bound")
        if any(type(item) is not dict or item.get("status") != "BOUND" for item in capability["dependencies"]):
            return _pending("unproven_dependency_claim")
        return {
            "status": "AUTHENTICATED",
            "cause": "capability_authenticated",
            "publisher_proof": False,
            "effects_denied": True,
            "verified_by_tool": True,
        }
    return _bound("capability_bound")
