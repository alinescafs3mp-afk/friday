"""Root approval comparator and the broker material-provenance envelope."""

from bill import project_artifacts
from whole_join import admit_approved_kernel, derive_root_input
from canonical import domain_digest, projection_digest
from contract import ContractError, is_commit, is_digest, is_fingerprint
from pins import (
    A009_SHA256,
    CANDIDATE_COMMIT,
    CANDIDATE_TREE,
    GOLDEN_COMMIT,
    PRODUCER_MAY_MINT,
)
from schema_validate import validate_document
from semantics import validate_receipt_semantics

_BINDINGS = (
    "issuer_id",
    "key_fingerprint",
    "capability_sha256",
    "attempt_generation",
    "artifact_set_sha256",
    "signer_set_sha256",
    "manifest_sha256",
    "recipe_sha256",
    "rootfs_sha256",
    "golden_sha256",
    "tool_sha256",
    "candidate_commit",
    "candidate_tree",
    "golden_commit",
    "bill_sha256",
)


def approval_body_digest(approval):
    body = {key: value for key, value in approval.items() if key not in ("approval_sha256", "projection_digest")}
    return domain_digest("friday.lab815.approval-body.v1", body)


def require_external_issuer(approval, expected, schema, root_issuer_sha256, approval_result=None):
    validate_document(approval, schema)
    if approval["produced_by_this_package"] is not False or PRODUCER_MAY_MINT:
        raise ContractError("producer_minted_approval")
    if approval["effects_granted"] is not False:
        raise ContractError("effects_granted")
    if approval["candidate_commit"] != CANDIDATE_COMMIT or approval["candidate_tree"] != CANDIDATE_TREE:
        raise ContractError("candidate_binding")
    if approval["golden_commit"] != GOLDEN_COMMIT or not is_commit(approval["golden_commit"]):
        raise ContractError("golden_binding")
    body_digest = approval_body_digest(approval)
    if approval["approval_sha256"] == body_digest:
        raise ContractError("digest_cycle")
    if approval["projection_digest"] != projection_digest(approval):
        raise ContractError("projection_mismatch")
    if expected is None:
        return {
            "status": "NOT_PROVEN",
            "cause": "external_approval_absent",
            "projection_digest": approval["projection_digest"],
            "publisher_proof": False,
        }
    validate_document(expected, schema)
    if expected["attempt_generation"] != approval["attempt_generation"]:
        raise ContractError("generation_mismatch")
    for key in _BINDINGS:
        if approval[key] != expected[key]:
            raise ContractError("issuer_binding")
    if approval["signature_sha256"] != expected["signature_sha256"] or approval["custody_sha256"] != expected["custody_sha256"]:
        raise ContractError("issuer_custody")
    if not is_digest(approval["approval_sha256"]):
        raise ContractError("approval_digest")
    if root_issuer_sha256 is None:
        return {
            "status": "NOT_PROVEN",
            "cause": "root_issuer_unpinned",
            "projection_digest": approval["projection_digest"],
            "publisher_proof": False,
        }
    if approval["approval_sha256"] != root_issuer_sha256:
        raise ContractError("issuer_pin")
    if approval_result is None:
        return {
            "status": "NOT_PROVEN",
            "cause": "approval_result_absent",
            "projection_digest": approval["projection_digest"],
            "publisher_proof": False,
        }
    if type(approval_result) is not dict or approval_result.get("decision") != "AUTHENTICATED":
        return {
            "status": "NOT_PROVEN",
            "cause": "approval_result_unproven",
            "projection_digest": approval["projection_digest"],
            "publisher_proof": False,
        }
    if approval_result.get("body_sha256") != body_digest:
        raise ContractError("approval_body")
    identity_keys = (
        "capability_sha256",
        "custody_sha256",
        "dependency_closure_sha256",
        "environment_digest",
        "executable_sha256",
        "issuer_id",
        "key_fingerprint",
        "signature_sha256",
        "verifier_sha256",
    )
    for key in identity_keys:
        value = approval_result.get(key)
        if value is None or value == "":
            return {
                "status": "NOT_PROVEN",
                "cause": "approval_identity_incomplete",
                "projection_digest": approval["projection_digest"],
                "publisher_proof": False,
            }
    for key in ("capability_sha256", "custody_sha256", "issuer_id", "key_fingerprint", "signature_sha256"):
        if key not in approval:
            continue
        left = approval[key]
        if left is None or left == "":
            continue
        if left != approval_result[key]:
            raise ContractError("issuer_binding")
    for actual_key, expected_key in (
        ("capability_sha256", "expected_capability_sha256"),
        ("dependency_closure_sha256", "expected_dependency_closure_sha256"),
        ("environment_digest", "expected_environment_digest"),
        ("executable_sha256", "expected_executable_sha256"),
        ("verifier_sha256", "expected_verifier_sha256"),
    ):
        expected_value = approval_result.get(expected_key)
        if expected_value is None or expected_value == "":
            return {
                "status": "NOT_PROVEN",
                "cause": "approval_identity_unbound",
                "projection_digest": approval["projection_digest"],
                "publisher_proof": False,
            }
        if approval_result.get(actual_key) != expected_value:
            raise ContractError("issuer_binding")
    status = "STRUCTURALLY_BOUND"
    cause = "approval_compared"
    return {
        "status": status,
        "cause": cause,
        "projection_digest": approval["projection_digest"],
        "publisher_proof": False,
    }


def signer_set_digest(signers):
    if signers is None:
        return None
    if type(signers) is not list:
        raise ContractError("signer_set")
    rows = []
    for item in signers:
        if not is_fingerprint(item):
            raise ContractError("signer_fingerprint")
        rows.append(item)
    if len(rows) != len(set(rows)):
        raise ContractError("signer_set")
    if not rows:
        return None
    return domain_digest("friday.lab820.signer-set.v1", sorted(rows))


def materials_digest(artifacts, authority_identity, recipe_sha256):
    body = {
        "artifacts": [
            {
                "abi": item.get("abi"),
                "class": item["class"],
                "custody_sha256": item.get("custody_sha256"),
                "filename": item["filename"],
                "format": item.get("format"),
                "key_id": item.get("key_id"),
                "sha256": item.get("sha256"),
                "signature_sha256": item.get("signature_sha256"),
                "signer_fingerprint": item.get("signer_fingerprint"),
                "size": item.get("size"),
                "source": item.get("source"),
                "tag": item.get("tag"),
                "verifier_sha256": item.get("verifier_sha256"),
            }
            for item in artifacts
        ],
        "authority": authority_identity,
        "recipe_sha256": recipe_sha256,
    }
    return domain_digest("friday.lab820.materials.v1", body)


def build_material_provenance(approval, recipe, comparison_status, schema, observations=None, admitted_signers=None, computed=None, pins=None):
    computed = {} if computed is None else computed
    artifacts = project_artifacts(recipe, observations, pins)
    kernel=admit_approved_kernel(artifacts,computed,pins)
    manifest_sha256 = computed["manifest_sha256"] if "manifest_sha256" in computed else approval["manifest_sha256"]
    recipe_sha256 = computed["recipe_sha256"] if "recipe_sha256" in computed else approval["recipe_sha256"]
    rootfs_sha256 = computed["rootfs_sha256"] if "rootfs_sha256" in computed else approval["rootfs_sha256"]
    golden_sha256 = computed["golden_sha256"] if "golden_sha256" in computed else approval["golden_sha256"]
    tool_sha256 = computed["tool_sha256"] if "tool_sha256" in computed else approval["tool_sha256"]
    for artifact in artifacts:
        if artifact["class"] == "recipe" and artifact.get("sha256") is None and is_digest(recipe_sha256):
            artifact["sha256"] = recipe_sha256
            artifact["upstream_authority"] = "COMPUTED_OUTPUT"
        elif artifact["class"] == "tool" and artifact.get("sha256") is None and is_digest(tool_sha256):
            artifact["sha256"] = tool_sha256
            artifact["upstream_authority"] = "COMPUTED_OUTPUT"
        elif artifact["class"] == "golden" and artifact.get("sha256") is None and is_digest(golden_sha256):
            artifact["sha256"] = golden_sha256
            artifact["upstream_authority"] = "COMPUTED_OUTPUT"
    ordered = [
        {
            "class": artifact["class"],
            "custody_sha256": artifact.get("custody_sha256"),
            "filename": artifact["filename"],
            "key_id": artifact.get("key_id"),
            "sha256": artifact.get("sha256"),
            "signature_sha256": artifact.get("signature_sha256"),
            "signer_fingerprint": artifact.get("signer_fingerprint"),
            "size": artifact.get("size"),
            "source": artifact.get("source"),
            "verifier_sha256": artifact.get("verifier_sha256"),
        }
        for artifact in artifacts
    ]
    owner_status = "PENDING"
    authorities = []
    if comparison_status == "STRUCTURALLY_BOUND":
        owner_status = "STRUCTURALLY_BOUND"
        authorities = [approval["issuer_id"]]
    envelope = {
        "schema": "material-provenance.v1",
        "manifest_sha256": manifest_sha256,
        "assembly_recipe_sha256": recipe_sha256,
        "creation_tool_sha256": tool_sha256,
        "approved_authorities": authorities,
        "artifacts": artifacts,
        "owner_approval": {
            "status": owner_status,
            "artifact_order_sha256": domain_digest("friday.lab815.artifact-order.v1", ordered),
            "signer_set_sha256": signer_set_digest(admitted_signers),
            "manifest_sha256": manifest_sha256,
            "candidate_commit": approval["candidate_commit"],
            "attempt_generation": approval["attempt_generation"],
            "rootfs_sha256": rootfs_sha256,
            "golden_sha256": golden_sha256,
            "recipe_sha256": recipe_sha256,
            "broker_contract_sha256": A009_SHA256,
            "publisher_proof": False,
        },
    }
    validate_document(envelope, schema)
    return envelope


def project_trust(
    approval,
    expected,
    raw_receipt,
    approval_schema,
    receipt_schema,
    provenance_schema,
    projection_schema,
    recipe,
    root_issuer_sha256,
    approval_result=None,
    observations=None,
    admitted_signers=None,
    authority_identity=None,
    computed=None,
    pins=None,
    receipt_records=None,
):
    validate_document(raw_receipt, receipt_schema)
    validate_receipt_semantics(raw_receipt)
    if raw_receipt["proof_status"] not in ("NOT_PROVEN", "REFUSED", "STRUCTURALLY_BOUND", "AUTHENTICATED"):
        raise ContractError("receipt_status")
    if raw_receipt["proof_status"] == "AUTHENTICATED" and raw_receipt["verification_result_sha256"] is None:
        raise ContractError("verification_result_absent")
    decision = require_external_issuer(approval, expected, approval_schema, root_issuer_sha256, approval_result)
    envelope = build_material_provenance(
        approval, recipe, decision["status"], provenance_schema, observations, admitted_signers, computed, pins,
    )
    if approval["artifact_set_sha256"] is not None:
        observed = domain_digest("friday.lab822.artifact-set.v1", [
            {
                "class": item["class"],
                "custody_sha256": item.get("custody_sha256"),
                "filename": item["filename"],
                "key_id": item.get("key_id"),
                "sha256": item.get("sha256"),
                "signature_sha256": item.get("signature_sha256"),
                "signer_fingerprint": item.get("signer_fingerprint"),
                "size": item.get("size"),
                "source": item.get("source"),
                "verifier_sha256": item.get("verifier_sha256"),
            }
            for item in envelope["artifacts"]
        ])
        if observed != approval["artifact_set_sha256"]:
            raise ContractError("artifact_set")
    signers = []
    records = receipt_records if type(receipt_records) is list else []
    for record in records:
        if type(record) is not dict:
            continue
        if record.get("decision") not in ("STRUCTURALLY_BOUND", "AUTHENTICATED"):
            continue
        finger = record.get("key_fingerprint")
        if is_fingerprint(finger) and finger not in signers:
            signers.append(finger)
    if not signers:
        for item in observations or []:
            if type(item) is not dict:
                continue
            finger = item.get("signer_fingerprint")
            if is_fingerprint(finger) and finger not in signers:
                signers.append(finger)
    if not signers and type(admitted_signers) is list:
        for item in admitted_signers:
            if is_fingerprint(item) and item not in signers:
                signers.append(item)
    computed_signers = signer_set_digest(signers if signers else None)
    envelope["owner_approval"]["signer_set_sha256"] = computed_signers
    if approval["signer_set_sha256"] is not None and approval["signer_set_sha256"] != computed_signers:
        raise ContractError("signer_set")
    identity = authority_identity if authority_identity is not None else {
        "issuer_id": approval["issuer_id"],
        "key_fingerprint": approval["key_fingerprint"],
    }
    materials_sha256 = materials_digest(envelope["artifacts"], identity, envelope["assembly_recipe_sha256"])
    for key in ("manifest_sha256", "golden_sha256", "recipe_sha256", "rootfs_sha256"):
        if approval[key] is not None and envelope["owner_approval"][key] != approval[key]:
            raise ContractError("broker_identity")
    if approval["tool_sha256"] is not None and envelope["creation_tool_sha256"] != approval["tool_sha256"]:
        raise ContractError("broker_identity")
    release_trusted = False
    if release_trusted:
        raise ContractError("synthetic_release_trust")
    projection = {
        "schema": "friday.lab815.trusted-root-projection.v1",
        "comparison_status": decision["status"],
        "release_trusted": release_trusted,
        "reason": decision["cause"],
        "projection_digest": decision["projection_digest"],
        "issuer_id": approval["issuer_id"],
        "key_fingerprint": approval["key_fingerprint"],
        "artifact_set_sha256": approval["artifact_set_sha256"],
        "signer_set_sha256": approval["signer_set_sha256"],
        "rootfs_sha256": approval["rootfs_sha256"],
        "golden_sha256": approval["golden_sha256"],
        "tool_sha256": approval["tool_sha256"],
        "attempt_generation": approval["attempt_generation"],
        "effects_denied": True,
        "publisher_proof": False,
        "provenance_sha256": domain_digest("friday.lab815.provenance-link.v1", {
            "artifact_order_sha256": envelope["owner_approval"]["artifact_order_sha256"],
            "assembly_recipe_sha256": envelope["assembly_recipe_sha256"],
            "creation_tool_sha256": envelope["creation_tool_sha256"],
            "manifest_sha256": envelope["manifest_sha256"],
            "materials_sha256": materials_sha256,
            "owner_status": envelope["owner_approval"]["status"],
            "receipt_vector_sha256": domain_digest(
                "friday.lab832.receipt-vector.v1",
                receipt_records if type(receipt_records) is list else [],
            ),
        }),
    }
    validate_document(projection, projection_schema)
    kernel = admit_approved_kernel(envelope["artifacts"], computed, pins)
    root_input_path = derive_root_input(approval, raw_receipt, computed, envelope, kernel, approval_result)
    if projection['comparison_status']=='STRUCTURALLY_BOUND' and root_input_path['closed'] is not True:
        projection['comparison_status']='NOT_PROVEN'
        projection['reason']='root_input_incomplete'
    return {
        "projection": projection,
        "provenance": envelope,
        "materials_sha256": materials_sha256,
        "root_input_path": root_input_path,
    }
