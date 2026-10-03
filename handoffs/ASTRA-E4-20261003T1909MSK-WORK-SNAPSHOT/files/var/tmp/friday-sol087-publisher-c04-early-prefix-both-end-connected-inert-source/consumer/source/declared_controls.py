"""Sixty-nine causal controls. Each calls plan_construction. The sealer does not invoke them."""

from resource_meter import HashlibProxy
hashlib = HashlibProxy()
import json
import pathlib

from canonical import canonical_bytes, domain_digest, projection_digest, parse_exact
from capability import capability_digest
from contract import ContractError
from fixtures import capability, clearsign, expected_fixture_bytes, load_bytes
from formats import parse_clearsign
from pins import (
    AUTHORITY_FIXTURE_SHA256,
    BILL_SHA256,
    CANDIDATE_COMMIT,
    CANDIDATE_TREE,
    CONTROL_COUNT,
    GOLDEN_COMMIT,
    NODESOURCE_FINGERPRINT_NOT_AUTHORITY,
    PRESENTED_FIXTURE_SHA256,
    TUPLE_MIGRATIONS,
    UBUNTU_FINGERPRINT,
    UNSUPPORTED_HASH_ALIAS,
)
from recipe_planner import plan_construction
from whole_join import expected_public_output
from resource_meter import bounded_file, WholeMeter, current, attach_refusal
from body_scope import document_scope
from document_vector import _Lease

_INERT_IDS=frozenset({'member_dotdot_refused','symlink_escape_refused'})

_DIGEST = "ab" * 32

CATALOG = [
    {"id": "ubuntu_known_pin_absent_tool", "status": "NOT_PROVEN", "cause": "verifier_material_absent", "match": "return", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "ubuntu_receipt"},
    {"id": "ubuntu_tool_flag_before_attestation", "status": "NOT_PROVEN", "cause": "tool_flag_without_result", "match": "return", "scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE", "stage": "ubuntu_receipt"},
    {"id": "ubuntu_fingerprint_mismatch", "status": "REFUSED", "cause": "fingerprint_mismatch", "match": "refuse", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "ubuntu_receipt"},
    {"id": "ubuntu_candidate_signer_refused", "status": "REFUSED", "cause": "candidate_signer", "match": "refuse", "scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE", "stage": "ubuntu_receipt"},
    {"id": "node_signer_unproven", "status": "NOT_PROVEN", "cause": "node_signer_unproven", "match": "return", "scenario": "NODE_RAW_SIGNED_CHECKSUM_CHAIN", "stage": "node_receipt"},
    {"id": "node_nodesource_fingerprint_refused", "status": "REFUSED", "cause": "nodesource_not_this_archive", "match": "refuse", "scenario": "NODE_RAW_SIGNED_CHECKSUM_CHAIN", "stage": "node_receipt"},
    {"id": "clearsign_sha512_structural", "status": "ACCEPTED", "cause": "sha512_correspondence", "match": "return", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "algorithm"},
    {"id": "clearsign_trailing_refused", "status": "REFUSED", "cause": "clearsign_trailing_material", "match": "refuse", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "algorithm"},
    {"id": "clearsign_unknown_hash", "status": "REFUSED", "cause": "unsupported_hash", "match": "refuse", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "algorithm"},
    {"id": "deb822_continuation_preserved", "status": "ACCEPTED", "cause": "continuation_preserved", "match": "return", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "deb822"},
    {"id": "deb822_orphan_continuation_refused", "status": "REFUSED", "cause": "orphan_continuation", "match": "refuse", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "deb822"},
    {"id": "release_uses_sha256_member", "status": "ACCEPTED", "cause": "release_member", "match": "return", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "release"},
    {"id": "release_wrong_suite_refused", "status": "REFUSED", "cause": "suite_mismatch", "match": "refuse", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "release"},
    {"id": "packages_unknown_field_kept", "status": "ACCEPTED", "cause": "unknown_field_preserved", "match": "return", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "package_fields"},
    {"id": "schema_bool_not_integer", "status": "REFUSED", "cause": "integer", "match": "refuse", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "ingress"},
    {"id": "schema_extra_key_refused", "status": "REFUSED", "cause": "exact_keys", "match": "refuse", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "ingress"},
    {"id": "projection_not_repr", "status": "ACCEPTED", "cause": "projection_digest", "match": "return", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "projection"},
    {"id": "issuer_absent_not_proven", "status": "NOT_PROVEN", "cause": "external_approval_absent", "match": "return", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "trust"},
    {"id": "issuer_self_mint_refused", "status": "REFUSED", "cause": "producer_minted_approval", "match": "refuse", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "trust"},
    {"id": "issuer_generation_mismatch_refused", "status": "REFUSED", "cause": "generation_mismatch", "match": "refuse", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "trust"},
    {"id": "binding_set_mismatch_refused", "status": "REFUSED", "cause": "binding_set", "match": "refuse", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "binding"},
    {"id": "binding_false_match_claim_refused", "status": "REFUSED", "cause": "evidence_issuer", "match": "refuse", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "binding"},
    {"id": "binding_abi_mismatch_refused", "status": "REFUSED", "cause": "binding_field", "match": "refuse", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "binding"},
    {"id": "recipe_closed_effects_refused", "status": "PLANNED_EFFECTS_UNAVAILABLE", "cause": "source_phase", "match": "return", "scenario": "ROOTFS_NATIVE_DATA_RECIPE", "stage": "plan"},
    {"id": "recipe_image_sha_not_mandatory", "status": "PLANNED_EFFECTS_UNAVAILABLE", "cause": "image_sha_optional", "match": "return", "scenario": "ROOTFS_NATIVE_DATA_RECIPE", "stage": "image"},
    {"id": "unrar_gap_explicit", "status": "BLOCKED_PUBLISHER_GAP", "cause": "unrar", "match": "return", "scenario": "ROOTFS_NATIVE_DATA_RECIPE", "stage": "unrar"},
    {"id": "browser_attribution_not_proven", "status": "NOT_PROVEN", "cause": "browser_attribution", "match": "return", "scenario": "ROOTFS_NATIVE_DATA_RECIPE", "stage": "browser"},
    {"id": "effect_denied", "status": "DENIED", "cause": "source_phase_denial", "match": "return", "scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE", "stage": "effect"},
    {"id": "expected_digest_not_replaced", "status": "NOT_PROVEN", "cause": "host_bytes_differ", "match": "return", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "retain"},
    {"id": "synthetic_algorithm_not_publisher", "status": "REFUSED", "cause": "receipt_credit", "match": "refuse", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "ubuntu_receipt"},
    {"id": "dependency_bound_claim_refused", "status": "REFUSED", "cause": "unproven_dependency_claim", "match": "refuse", "scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE", "stage": "ubuntu_receipt"},
    {"id": "project_trust_untrusted_without_issuer", "status": "NOT_PROVEN", "cause": "external_approval_absent", "match": "return", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "trust"},
    {"id": "sha512_receipt_consumer", "status": "ACCEPTED", "cause": "sha512_correspondence", "match": "return", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "algorithm"},
    {"id": "sha256_receipt_consumer", "status": "ACCEPTED", "cause": "sha256_correspondence", "match": "return", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "algorithm"},
    {"id": "sha512_body_width_refused", "status": "REFUSED", "cause": "signed_body_width", "match": "refuse", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "ubuntu_receipt"},
    {"id": "result_metadata_without_bytes", "status": "NOT_PROVEN", "cause": "verification_bytes_absent", "match": "return", "scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE", "stage": "ubuntu_receipt"},
    {"id": "result_body_mismatch", "status": "REFUSED", "cause": "result_binding", "match": "refuse", "scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE", "stage": "ubuntu_receipt"},
    {"id": "dependency_inventory_empty_refused", "status": "REFUSED", "cause": "dependency_inventory_empty", "match": "refuse", "scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE", "stage": "ubuntu_receipt"},
    {"id": "node_archive_byte_match", "status": "STRUCTURALLY_BOUND", "cause": "archive_bound", "match": "return", "scenario": "NODE_RAW_SIGNED_CHECKSUM_CHAIN", "stage": "probe"},
    {"id": "node_archive_checksum_mismatch", "status": "REFUSED", "cause": "archive_checksum_mismatch", "match": "refuse", "scenario": "NODE_RAW_SIGNED_CHECKSUM_CHAIN", "stage": "probe"},
    {"id": "node_authoritative_absent", "status": "NOT_PROVEN", "cause": "authoritative_checksum_absent", "match": "return", "scenario": "NODE_RAW_SIGNED_CHECKSUM_CHAIN", "stage": "probe"},
    {"id": "noncanonical_json_refused", "status": "REFUSED", "cause": "not_canonical", "match": "refuse", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "ingress"},
    {"id": "release_integer_text_refused", "status": "REFUSED", "cause": "integer_text", "match": "refuse", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "release"},
    {"id": "dash_unescape_identity", "status": "ACCEPTED", "cause": "dash_unescape", "match": "return", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "dash"},
    {"id": "receipt_missing_before_projection", "status": "REFUSED", "cause": "exact_keys", "match": "refuse", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "ingress"},
    {"id": "broker_envelope_absent_approval", "status": "NOT_PROVEN", "cause": "broker_envelope", "match": "return", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "trust"},
    {"id": "broker_fixture_not_release_trust", "status": "NOT_PROVEN", "cause": "release_untrusted", "match": "return", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "release_trust"},
    {"id": "compatibility_mismatch_refused", "status": "REFUSED", "cause": "compatibility_mismatch", "match": "refuse", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "binding"},
    {"id": "null_material_refused", "status": "REFUSED", "cause": "material_incomplete", "match": "refuse", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "binding"},
    {"id": "bill_recompute_ubuntu", "status": "ACCEPTED", "cause": "ubuntu_projection", "match": "return", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "bill"},
    {"id": "bill_mutated_row_refused", "status": "REFUSED", "cause": "ubuntu_projection", "match": "refuse", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "bill"},
    {"id": "wheel_tag_acquired", "status": "ACCEPTED", "cause": "wheel_tag", "match": "return", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "wheel"},
    {"id": "wheel_not_acquired_filename", "status": "REFUSED", "cause": "wheel_filename", "match": "refuse", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "wheel"},
    {"id": "unlinked_index_refused", "status": "REFUSED", "cause": "index_link", "match": "refuse", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "bill"},
    {"id": "member_dotdot_refused", "status": "REFUSED", "cause": "member_path", "match": "refuse", "scenario": "ROOTFS_NATIVE_DATA_RECIPE", "stage": "plan"},
    {"id": "symlink_escape_refused", "status": "REFUSED", "cause": "link_escape", "match": "refuse", "scenario": "ROOTFS_NATIVE_DATA_RECIPE", "stage": "plan"},
    {"id": "venv_site_flag", "status": "ACCEPTED", "cause": "venv_site", "match": "return", "scenario": "ROOTFS_NATIVE_DATA_RECIPE", "stage": "venv"},
    {"id": "missing_operation_refused", "status": "REFUSED", "cause": "recipe_operations", "match": "refuse", "scenario": "ROOTFS_NATIVE_DATA_RECIPE", "stage": "plan"},
    {"id": "canonical_ingress_refused", "status": "REFUSED", "cause": "not_canonical", "match": "refuse", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "ingress"},
    {"id": "unrelated_cause_not_substituted", "status": "CAUSE_MISMATCH", "cause": "member_path", "match": "unrelated", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "plan"},
]
EXTRA_CATALOG = [
    {"id": "s21_result_decision_consumed", "status": "NOT_PROVEN", "cause": "result_decision_unproven", "match": "return", "scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE", "stage": "ubuntu_receipt"},
    {"id": "s21_algorithm_cross_refused", "status": "REFUSED", "cause": "algorithm_correspondence", "match": "refuse", "scenario": "UBUNTU_RAW_SIGNATURE_CHAIN", "stage": "algorithm"},
    {"id": "s21_presented_absent", "status": "PLANNED_EFFECTS_UNAVAILABLE", "cause": "source_phase", "match": "return", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "plan"},
    {"id": "s21_self_bind_refused", "status": "REFUSED", "cause": "expected_bound_to_itself", "match": "refuse", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "binding"},
    {"id": "s21_schema_descriptor_admitted", "status": "ACCEPTED", "cause": "schema_descriptor_admitted", "match": "return", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "schema"},
    {"id": "s21_root_issuer_reachable", "status": "NOT_PROVEN", "cause": "approval_result_absent", "match": "return", "scenario": "INPUT_AND_EXTERNAL_AUTHORITY", "stage": "trust"},
    {"id": "s21_dag_cycle_refused", "status": "REFUSED", "cause": "operation_dependency", "match": "refuse", "scenario": "ROOTFS_NATIVE_DATA_RECIPE", "stage": "plan"},
    {"id": "s21_requires_python_normalization", "status": "ACCEPTED", "cause": "requires_python_normalization", "match": "return", "scenario": "MATERIAL_PLATFORM_COMPATIBILITY", "stage": "requires_python"},
    {"id": "s21_effects_stay_denied", "status": "PLANNED_EFFECTS_UNAVAILABLE", "cause": "source_phase", "match": "return", "scenario": "CAPABILITY_INVENTORY_FAILURE_CLOSURE", "stage": "plan"},
]
CATALOG.extend(EXTRA_CATALOG)


def _load():
    authority = parse_exact(load_bytes("authority.json", AUTHORITY_FIXTURE_SHA256))
    expected = parse_exact(expected_fixture_bytes())
    presented = parse_exact(load_bytes("presented.json", PRESENTED_FIXTURE_SHA256))
    return authority, expected, presented


def _arm(body, algorithm=b"SHA512"):
    if type(body) is str:
        body = body.encode("ascii")
    lines = body.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return clearsign(lines, algorithm=algorithm).decode("ascii")


def _release(suite="resolute", size="1", digest=None):
    digest = _DIGEST if digest is None else digest
    return (
        "Origin: Ubuntu\n"
        "Suite: " + suite + "\n"
        "Components: main\n"
        "Architectures: amd64\n"
        "SHA256:\n"
        " " + digest + " " + size + " main/binary-amd64/Packages\n"
    )


def _release_expected(size=1, digest=None, suite="resolute"):
    return {
        "architecture": "amd64",
        "component": "main",
        "packages_sha256": _DIGEST if digest is None else digest,
        "size": size,
        "suite": suite,
    }


def _package():
    return (
        "Package: demo\n"
        "Version: 1\n"
        "Architecture: amd64\n"
        "Filename: pool/demo.deb\n"
        "Size: 4\n"
        "SHA256: " + _DIGEST + "\n"
        "Extra: kept\n"
    )


def _package_expected():
    return {
        "architecture": "amd64",
        "filename": "pool/demo.deb",
        "name": "demo",
        "sha256": _DIGEST,
        "size": 4,
        "version": "1",
    }


def _result(cap, decision="NOT_PROVEN", raw=None, body=None, armor=None, body_sha=None):
    raw_bytes = b"" if raw is None else raw
    body_bytes = raw_bytes if body is None else body
    armor_bytes = raw_bytes if armor is None else armor
    return {
        "algorithm": cap["algorithm_class"],
        "archive_sha256": None,
        "archive_size": None,
        "argv": list(cap["argv"]),
        "armor_sha256": hashlib.sha256(armor_bytes).hexdigest(),
        "attestation_sha256": None,
        "body_sha256": _DIGEST if body_sha is not None else hashlib.sha256(body_bytes).hexdigest(),
        "capability_sha256": capability_digest(cap),
        "custody_sha256": None,
        "decision": decision,
        "dependency_closure_sha256": cap["dependency_closure_sha256"],
        "environment_digest": cap["environment_digest"],
        "issuer_id": cap["issuer_attestation"]["issuer_id"],
        "issuer_result_sha256": None,
        "keyring_sha256": cap["keyring_sha256"],
        "produced_by_this_package": False,
        "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "schema": "friday.lab815.verification-result.v1",
    }


def _approval(generation=1, produced=False):
    approval = {
        "approval_sha256": None,
        "artifact_set_sha256": None,
        "attempt_generation": generation,
        "bill_sha256": BILL_SHA256,
        "candidate_commit": CANDIDATE_COMMIT,
        "candidate_tree": CANDIDATE_TREE,
        "capability_sha256": None,
        "custody_sha256": None,
        "effects_granted": False,
        "golden_commit": GOLDEN_COMMIT,
        "golden_sha256": None,
        "issuer_id": "unpinned-root",
        "key_fingerprint": UBUNTU_FINGERPRINT,
        "manifest_sha256": _DIGEST,
        "produced_by_this_package": produced,
        "projection_digest": None,
        "recipe_sha256": None,
        "rootfs_sha256": None,
        "schema": "friday.lab815.external-issuer-approval.v1",
        "signature_sha256": None,
        "signer_set_sha256": None,
        "tool_sha256": None,
    }
    approval["approval_sha256"] = domain_digest("friday.lab815.external-approval-absent.v1", {"status": "ABSENT"})
    approval["projection_digest"] = projection_digest(approval)
    return approval


def _record(filename="pool/demo.deb", abi="cp314", statement="NOT_PROVEN", issuer="ubuntu-archive", size=4, version="1"):
    return {
        "abi": abi,
        "architecture": "amd64",
        "archive_format": "deb",
        "compatibility_evidence": {
            "evidence_sha256": "cd" * 32,
            "issuer_id": issuer,
            "statement": statement,
            "target_sha256": _DIGEST,
        },
        "consumer_id": "ubuntu-minimum",
        "filename": filename,
        "member_name": "main/binary-amd64/Packages",
        "origin_id": "cd" * 32,
        "platform": "ubuntu-26.04",
        "producer_id": "ubuntu-archive",
        "resource_role": "package",
        "sha256": _DIGEST,
        "size": size,
        "version": version,
    }


def _binding(records, statement="NOT_PROVEN"):
    return {
        "bill_sha256": BILL_SHA256,
        "records": records,
        "schema": "friday.lab815.producer-consumer-binding.v1",
        "statement": statement,
    }


def _python_observation(expected):
    item = None
    for candidate in expected["wheels"]["items"]:
        if candidate["name"] == "texttable" and candidate["version"] == "1.7.0":
            item = candidate
            break
    if item is None or item["requires_python"] is not None:
        raise ContractError("requires_python_normalization")
    return {
        "abi_tag": item["abi_tag"],
        "filename": item["filename"],
        "metadata_raw_sha256": item["metadata_raw_sha256"],
        "name": item["name"],
        "platform_tag": item["platform_tag"],
        "publisher_proof": False,
        "python_tag": item["python_tag"],
        "requires_python": None,
        "requires_python_raw": "",
        "sha256": item["sha256"],
        "size": item["size"],
        "version": item["version"],
    }


def _mutate(control_id, authority, expected, presented, ordinary_variant=None):
    if control_id in _INERT_IDS:
        raise ContractError('inert_historical_data')
    if ordinary_variant is not None:
        return tuple(ordinary_variant[k] for k in ('authority_raw','expected_raw','presented_raw','context_raw','streams'))
    if control_id in ("ubuntu_known_pin_absent_tool",):
        presented["ubuntu_capability"] = capability()
        presented["ubuntu_expected_capability"] = capability()
    elif control_id == "ubuntu_tool_flag_before_attestation":
        cap = capability()
        cap["verified_by_tool"] = True
        presented["ubuntu_capability"] = cap
    elif control_id == "ubuntu_fingerprint_mismatch":
        cap = capability()
        cap["key_fingerprint"] = "A" * 40
        presented["ubuntu_capability"] = cap
    elif control_id == "ubuntu_candidate_signer_refused":
        cap = capability()
        cap["signer_supplied_by_candidate"] = True
        presented["ubuntu_capability"] = cap
    elif control_id == "node_signer_unproven":
        presented["node_capability"] = capability("node-shasums256")
        presented["node_expected_capability"] = capability("node-shasums256")
    elif control_id == "node_nodesource_fingerprint_refused":
        cap = capability("node-shasums256")
        cap["key_fingerprint"] = NODESOURCE_FINGERPRINT_NOT_AUTHORITY
        presented["node_capability"] = cap
    elif control_id in ("clearsign_sha512_structural", "sha512_receipt_consumer"):
        presented["ubuntu_clearsign"] = _arm("Origin: Ubuntu\n", b"SHA512")
    elif control_id == "clearsign_trailing_refused":
        presented["ubuntu_clearsign"] = _arm("Origin: Ubuntu\n", b"SHA512") + "trailing\n"
    elif control_id == "clearsign_unknown_hash":
        presented["ubuntu_clearsign"] = _arm("Origin: Ubuntu\n", b"SHA1")
    elif control_id == "deb822_continuation_preserved":
        presented["packages_text"] = "Package: demo\nDescription: hello\n world\n"
    elif control_id == "deb822_orphan_continuation_refused":
        presented["packages_text"] = " world\n"
    elif control_id == "release_uses_sha256_member":
        presented["ubuntu_clearsign"] = _arm(_release(), b"SHA512")
        presented["release_cleartext"] = _release()
        presented["release_expected"] = _release_expected()
    elif control_id == "release_wrong_suite_refused":
        presented["ubuntu_clearsign"] = _arm("Origin: Ubuntu\n", b"SHA512")
        presented["release_cleartext"] = _release(suite="other")
        presented["release_expected"] = _release_expected()
    elif control_id == "packages_unknown_field_kept":
        presented["ubuntu_clearsign"] = _arm("Origin: Ubuntu\n", b"SHA512")
        presented["packages_text"] = _package()
        presented["package_expected"] = _package_expected()
    elif control_id == "schema_bool_not_integer":
        expected["ubuntu_minimum"]["packages"][0]["size"] = True
    elif control_id == "schema_extra_key_refused":
        presented["review_note"] = "x"
    elif control_id == "issuer_self_mint_refused":
        presented["approval"] = _approval(produced=True)
    elif control_id == "issuer_generation_mismatch_refused":
        presented["approval"] = _approval(1)
        presented["approval_expected"] = _approval(2)
    elif control_id == "binding_set_mismatch_refused":
        presented["ubuntu_binding"] = _binding([_record("pool/a.deb")])
        presented["ubuntu_binding_expected"] = _binding([_record("pool/b.deb")])
    elif control_id == "binding_false_match_claim_refused":
        presented["ubuntu_binding"] = _binding([_record(statement="MATCH", issuer="other-issuer")], "MATCH")
        presented["ubuntu_binding_expected"] = _binding([_record(statement="MATCH")], "MATCH")
    elif control_id == "binding_abi_mismatch_refused":
        presented["ubuntu_binding"] = _binding([_record(statement="MATCH", abi="none")], "MATCH")
        presented["ubuntu_binding_expected"] = _binding([_record(statement="MATCH", abi="cp314")], "MATCH")
    elif control_id == "synthetic_algorithm_not_publisher":
        presented["synthetic_publisher_proof"] = True
    elif control_id == "dependency_bound_claim_refused":
        cap = capability()
        cap["dependency_status"] = "BOUND"
        presented["ubuntu_capability"] = cap
    elif control_id == "sha256_receipt_consumer":
        cap = capability()
        cap["algorithm_class"] = "openpgp-sha256"
        presented["ubuntu_capability"] = cap
        presented["ubuntu_clearsign"] = _arm("Origin: Ubuntu\n", b"SHA256")
    elif control_id == "sha512_body_width_refused":
        presented["ubuntu_signed_body_override"] = "ab" * 64
    elif control_id == "result_metadata_without_bytes":
        cap = capability()
        presented["ubuntu_capability"] = cap
        presented["ubuntu_result"] = _result(cap)
    elif control_id == "result_body_mismatch":
        text = _arm("Origin: Ubuntu\n", b"SHA512")
        cap = capability()
        presented["ubuntu_capability"] = cap
        presented["ubuntu_clearsign"] = text
        presented["ubuntu_result"] = _result(cap, raw=text.encode("ascii"), body_sha=_DIGEST)
    elif control_id == "dependency_inventory_empty_refused":
        cap = capability()
        cap["dependency_closure_sha256"] = _DIGEST
        presented["ubuntu_capability"] = cap
    elif control_id == "node_archive_byte_match":
        raw = b"probe"
        presented["probe_archive_hex"] = raw.hex()
        presented["probe_expected_sha256"] = hashlib.sha256(raw).hexdigest()
        presented["probe_expected_size"] = len(raw)
    elif control_id == "node_archive_checksum_mismatch":
        presented["probe_archive_hex"] = b"probe".hex()
        presented["probe_expected_sha256"] = _DIGEST
        presented["probe_expected_size"] = 5
    elif control_id == "node_authoritative_absent":
        presented["probe_archive_hex"] = b"probe".hex()
    elif control_id == "noncanonical_json_refused":
        raw_presented = canonical_bytes(presented).replace(b"{", b"{ ", 1)
        return canonical_bytes(authority), canonical_bytes(expected), raw_presented
    elif control_id in ("release_integer_text_refused", "unrelated_cause_not_substituted"):
        presented["ubuntu_clearsign"] = _arm("Origin: Ubuntu\n", b"SHA512")
        presented["release_cleartext"] = _release(size="12345678901")
        presented["release_expected"] = _release_expected()
    elif control_id == "dash_unescape_identity":
        presented["ubuntu_clearsign"] = _arm("- Origin: Ubuntu\n", b"SHA512")
    elif control_id == "receipt_missing_before_projection":
        del presented["withhold_approval"]
    elif control_id == "broker_envelope_absent_approval":
        presented["withhold_approval"] = True
    elif control_id == "compatibility_mismatch_refused":
        presented["ubuntu_binding"] = _binding([], "MISMATCH")
        presented["ubuntu_binding_expected"] = _binding([], "MISMATCH")
    elif control_id == "null_material_refused":
        presented["ubuntu_binding"] = _binding([_record(size=None, version="1")])
        presented["ubuntu_binding_expected"] = _binding([_record(size=None, version="2")])
    elif control_id == "bill_mutated_row_refused":
        expected["ubuntu_minimum"]["packages"][0]["sha256"] = "cd" * 32
    elif control_id == "wheel_not_acquired_filename":
        item = dict(expected["wheels"]["items"][0])
        item["filename"] = "notacquired.whl"
        expected["wheels"]["items"].append(item)
    elif control_id == "unlinked_index_refused":
        row = dict(expected["ubuntu_minimum"]["packages"][0])
        row["name"] = "extralink"
        row["suite"] = "nosuite"
        expected["ubuntu_minimum"]["packages"].append(row)
    elif control_id == "missing_operation_refused":
        expected["operations"].pop()
    elif control_id == "canonical_ingress_refused":
        raw_authority = canonical_bytes(authority).replace(b"{", b"{ ", 1)
        return raw_authority, canonical_bytes(expected), canonical_bytes(presented)
    elif control_id == "s21_result_decision_consumed":
        text = _arm("Origin: Ubuntu\n", b"SHA512")
        raw = text.encode("ascii")
        parsed = parse_clearsign(raw)
        cap = capability()
        presented["ubuntu_capability"] = cap
        presented["ubuntu_clearsign"] = text
        presented["ubuntu_result"] = _result(
            cap,
            decision="NOT_PROVEN",
            raw=raw,
            body=parsed["normalized_body"],
            armor=parsed["signature_armor"],
        )
    elif control_id == "s21_algorithm_cross_refused":
        presented["ubuntu_clearsign"] = _arm("Origin: Ubuntu\n", b"SHA256")
    elif control_id == "s21_self_bind_refused":
        document = _binding([_record()])
        presented["ubuntu_binding"] = document
        presented["ubuntu_binding_expected"] = json.loads(json.dumps(document))
    elif control_id == "s21_root_issuer_reachable":
        # A Root packet must come from the ordinary externally selected variant.
        raise ContractError('full_root_variant_absent')
    elif control_id == "s21_dag_cycle_refused":
        expected["operations"][1]["dependencies"] = ["external-custody"]
    elif control_id == "s21_requires_python_normalization":
        presented["requires_python_observation"] = _python_observation(expected)
    elif control_id == "projection_not_repr":
        return None
    elif control_id == "issuer_absent_not_proven":
        return None
    elif control_id == "recipe_closed_effects_refused":
        return None
    elif control_id == "recipe_image_sha_not_mandatory":
        return None
    elif control_id == "unrar_gap_explicit":
        return None
    elif control_id == "browser_attribution_not_proven":
        return None
    elif control_id == "effect_denied":
        return None
    elif control_id == "expected_digest_not_replaced":
        return None
    elif control_id == "project_trust_untrusted_without_issuer":
        return None
    elif control_id == "broker_fixture_not_release_trust":
        return None
    elif control_id == "bill_recompute_ubuntu":
        return None
    elif control_id == "wheel_tag_acquired":
        return None
    elif control_id == "venv_site_flag":
        return None
    elif control_id == "s21_presented_absent":
        return None
    elif control_id == "s21_schema_descriptor_admitted":
        return None
    elif control_id == "s21_effects_stay_denied":
        return None
    return None


def resolve_control_id(control_id):
    migration = TUPLE_MIGRATIONS.get(control_id)
    if control_id in UNSUPPORTED_HASH_ALIAS:
        return UNSUPPORTED_HASH_ALIAS[control_id], TUPLE_MIGRATIONS.get("unsupported_hash")
    if migration is not None and migration.get("current_id") not in (None, control_id):
        return migration["current_id"], migration
    return control_id, migration


def _public_corpus():
    root = pathlib.Path(__file__).resolve().parent.parent
    context_raw = bounded_file(root / "benign" / "context.json",2000000)
    streams = parse_exact(bounded_file(root / "benign" / "streams.json",2000000),max_depth=12,max_string=2000000)
    if type(streams) is not dict:
        raise ContractError("held_stream")
    return context_raw, streams


_ASSERTION_IDS = frozenset({
    "bill_recompute_ubuntu",
    "broker_fixture_not_release_trust",
    "browser_attribution_not_proven",
    "effect_denied",
    "expected_digest_not_replaced",
    "issuer_absent_not_proven",
    "project_trust_untrusted_without_issuer",
    "projection_not_repr",
    "recipe_closed_effects_refused",
    "recipe_image_sha_not_mandatory",
    "s21_effects_stay_denied",
    "s21_presented_absent",
    "s21_schema_descriptor_admitted",
    "unrar_gap_explicit",
    "venv_site_flag",
    "wheel_tag_acquired",
})
_POSITIVE_IDS = frozenset({"clearsign_sha512_structural", "sha512_receipt_consumer"})


def _classify_mutation(control_id, spec):
    if control_id in _ASSERTION_IDS:
        return False, "assertion"
    if control_id in _POSITIVE_IDS and spec["match"] == "return":
        return False, "positive"
    return True, "mutation"


@document_scope
def _invoke_declared_control(control_id, ordinary_variant=None):
    if type(control_id) is not str or len(CATALOG) != CONTROL_COUNT:
        raise ContractError("control_unknown")
    resolved, migration = resolve_control_id(control_id)
    spec = None
    for row in CATALOG:
        if row["id"] == resolved:
            spec = row
            break
    if spec is None:
        raise ContractError("control_unknown")
    if resolved in _INERT_IDS:
        return {'id':resolved,'status':'NOT_RUN','runtime':'NOT_RUN','required':True,
                'waiver':False,'effects_denied':True,'publisher_proof':False,
                'historical_obligation':dict(spec),'operational_payload':None}
    authority = expected = presented = None
    golden=None
    if ordinary_variant is not None:
        if type(ordinary_variant) is not dict or set(ordinary_variant)!={'authority_raw','expected_raw','presented_raw','context_raw','streams','golden_raw','golden_ref','golden_producer','producer_id','selector_id'}:
            raise ContractError('control_variant')
        if ordinary_variant['producer_id']==ordinary_variant['selector_id']:
            raise ContractError('expected_bound_to_itself')
        context=parse_exact(ordinary_variant['context_raw'],max_depth=12,max_string=2000000)
        if context.get('performing_contracts') is None or context.get('require_predecessor_receipts') is not True or len(context.get('document_receipts',[]))<228 or any(r.get('body_held') is not True for r in context['document_receipts']):
            raise ContractError('control_prerequisite')
        # The whole expected output is independently selected before inspecting
        # the tested outcome. Its producer binds all five actual input domains.
        producer=ordinary_variant['golden_producer']
        keys={'producer_id','selector_id','input_sha256','output_sha256','output_size','resource_observer_id','produced_by_this_package'}
        if type(producer) is not dict or set(producer)!=keys or producer['produced_by_this_package'] is not False or producer['producer_id']!=ordinary_variant['producer_id'] or producer['selector_id']!=ordinary_variant['selector_id'] or producer['resource_observer_id'] in (producer['producer_id'],producer['selector_id']):
            raise ContractError('control_variant')
        inputs={'authority_sha256':hashlib.sha256(ordinary_variant['authority_raw']).hexdigest(),
                'expected_sha256':hashlib.sha256(ordinary_variant['expected_raw']).hexdigest(),
                'presented_sha256':hashlib.sha256(ordinary_variant['presented_raw']).hexdigest(),
                'context_sha256':hashlib.sha256(ordinary_variant['context_raw']).hexdigest(),
                'selected_pages':context['page_sequence'] if context['page_sequence'] is not None else context['streams']}
        if producer['input_sha256']!=domain_digest('friday.a128.full-five-argument-input.v1',inputs):
            raise ContractError('control_variant')
        raw=ordinary_variant['golden_raw']; ref=ordinary_variant['golden_ref']
        if (raw is None)==(ref is None):raise ContractError('control_variant')
        if ref is not None:
            from recipe_planner import _bind_streams, _ConstructionMeter
            if type(ref) is not dict or set(ref)!={'kind','path','sha256','size'} or ref['sha256']!=producer['output_sha256'] or ref['size']!=producer['output_size']:
                raise ContractError('control_variant')
            held=_bind_streams(context,ordinary_variant['streams'],_ConstructionMeter())
            with _Lease(held,ref['kind'],ref['path'],ref['sha256']) as lease:
                raw=lease.body
        if type(raw) is not bytes or len(raw)!=producer['output_size'] or len(raw)>33554432 or hashlib.sha256(raw).hexdigest()!=producer['output_sha256']:
            raise ContractError('control_variant')
        golden=parse_exact(raw,max_bytes=33554432,max_depth=24,max_string=2000000)
        if resolved=='s21_root_issuer_reachable':
            p=parse_exact(ordinary_variant['presented_raw'],max_depth=12,max_string=2000000)
            original=p['approval'];p['withhold_approval']=True
            preliminary=plan_construction(ordinary_variant['authority_raw'],ordinary_variant['expected_raw'],canonical_bytes(p),ordinary_variant['context_raw'],ordinary_variant['streams'])
            if type(original) is not dict or original.get('manifest_sha256')!=preliminary['manifest_sha256']:
                raise ContractError('control_prerequisite')
    if ordinary_variant is None:
        authority, expected, presented = _load()
        context_raw, streams = _public_corpus()
    else:
        context_raw=ordinary_variant['context_raw']; streams=ordinary_variant['streams']
    replacement = _mutate(resolved, authority, expected, presented,ordinary_variant)
    outcome = None
    actual_stage = spec["stage"]
    try:
        if replacement is None:
            outcome = plan_construction(
                canonical_bytes(authority),
                canonical_bytes(expected),
                canonical_bytes(presented),
                context_raw,
                streams,
            )
        elif len(replacement) == 5:
            outcome = plan_construction(*replacement)
        elif len(replacement) == 3:
            outcome = plan_construction(*replacement, context_raw, streams)
        else:
            raise ContractError("control_variant")
    except ContractError as exc:
        outcome=getattr(exc,"public_refusal",None)
        actual_cause = exc.cause
        actual_status = "REFUSED"
        actual_stage = exc.stage or spec["stage"]
    else:
        stage = outcome["stages"][spec["stage"]]
        actual_cause = stage["cause"]
        actual_status = stage["status"]
        actual_stage = spec["stage"]
    if actual_stage != spec["stage"]:
        reported = "CAUSE_MISMATCH"
    elif spec["match"] == "unrelated":
        reported = "CAUSE_MISMATCH" if actual_cause != spec["cause"] else actual_status
    elif actual_cause != spec["cause"]:
        reported = "CAUSE_MISMATCH"
    elif spec["match"] == "refuse":
        reported = actual_status
    else:
        reported = actual_status
    status_matches_intended = (
        actual_stage == spec["stage"]
        and actual_cause == spec["cause"]
        and actual_status == spec["status"]
    )
    expected_complete=expected_public_output(spec,golden)
    output_matches=(expected_complete is not None and outcome==expected_complete)
    if not output_matches and reported!='CAUSE_MISMATCH':reported='NOT_PROVEN'
    return {
        "actual_status": actual_status,
        "alias": dict(UNSUPPORTED_HASH_ALIAS),
        "cause": actual_cause,
        "effects_denied": True,
        "executed": True,
        "id": resolved,
        "intended_cause": spec["cause"],
        "intended_stage": spec["stage"],
        "intended_status": spec["status"],
        "publisher_proof": False,
        "requested_id": control_id,
        "scenario": spec["scenario"],
        "stage": actual_stage,
        "execution_authorized": False,
        "mutation_class": _classify_mutation(resolved, spec)[1],
        "mutation_present": _classify_mutation(resolved, spec)[0],
        "outcome": outcome,
        "status": reported,
        "status_matches_intended": status_matches_intended and output_matches,
        "tuple_migration": migration,
        "expected_public_output": expected_complete,
        'full_output_matches':output_matches,
        "held_shadow": "REFUSE_ON_DIVERGENCE",
        "root_preemption": "SOURCE_GUARD_PRESENT_EXECUTION_NOT_RUN",
    }


def invoke_declared_control(control_id, ordinary_variant=None):
    # Includes corpus read, decode, safe mutation, both real planner calls when
    # requested, full comparison and final complete serialization.
    whole=WholeMeter()
    try:
        with whole:
            result=_invoke_declared_control(control_id,ordinary_variant)
            wire=canonical_bytes(result)
            whole.check()
            return result
    except ContractError as exc:
        attach_refusal(exc,whole)
        raise
