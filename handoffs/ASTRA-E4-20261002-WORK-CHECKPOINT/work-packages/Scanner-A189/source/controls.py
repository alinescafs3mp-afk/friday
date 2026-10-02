"""Historical controls are abstract DATA; ordinary contracts compare exact output.

An independently fixed contract is an input, never authority minted by this module.
No C30/whole202 contract is inferred from the C55 census calibration.
"""

import json
import os
from .canonical import canonical_loads
from .public import scan_retained_bytes


def _load(relative,resources=None,meter=None):
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), relative)
    from .bounds import new_meter
    from .custody import owned_metadata_bytes
    from .digests import DigestStop
    if meter is None: meter=new_meter()
    raw,pin,cause=owned_metadata_bytes(path,1048576,resources,meter)
    if cause: raise DigestStop(cause)
    # Historical control JSON is inert UTF8 DATA; its physical allocation
    # belongs to the same selected native entry owner, before JSON construction.
    return json.loads(raw.decode("utf-8"))


def load_catalog(resources=None,meter=None):
    return _load("controls/catalog.json",resources,meter)


def load_contract(resources=None,meter=None):
    return _load("controls/independent-contract.json",resources,meter)


def dispatch_control(control_id):
    catalog = load_catalog()
    found = [item for item in catalog["controls"] if item["id"] == control_id]
    if len(found) != 1:
        return {"control_id": control_id, "status": "FAIL", "cause": "unknown_control"}
    return {"control_id": control_id, "status": "NOT_RUN", "classification": "ABSTRACT_DATA",
            "catalog_spec": found[0], "execution_authorized": False,
            "fixture_evidence_is_authority": False, "public_consumer": "scan_retained"}


def run_ordinary_census(index_raw, held_raw, admission_raw, independent_contract):
    """Future ordinary DATA caller; no archive or unsafe fixture construction route."""
    if not isinstance(independent_contract, dict) or independent_contract.get("id") != "C55":
        return {"control_id": "C55", "status": "FAIL", "cause": "control_output_unbound"}
    expected_inputs = independent_contract.get("input_sha256")
    from .digests import content_sha256
    actual_inputs = [content_sha256(raw) for raw in (index_raw, held_raw, admission_raw)]
    if actual_inputs != expected_inputs:
        return {"control_id": "C55", "status": "FAIL", "cause": "control_input_unbound"}
    raw = scan_retained_bytes(index_raw, held_raw, admission_raw)
    observed = canonical_loads(raw,max_bytes=67108864,max_depth=64)
    expected = independent_contract.get("expected_output")
    expected_bytes = independent_contract.get("expected_output_ascii")
    exact = (isinstance(expected, dict) and observed == expected and
             raw.decode("ascii") == expected_bytes and
             content_sha256(raw) == independent_contract.get("expected_output_sha256") and
             observed.get("stage") == "AUTHENTICATED_INGRESS")
    return {"control_id": "C55", "status": "PASS" if exact else "FAIL", "observed": observed,
            "full_output_matched": exact, "public_consumer": "scan_retained",
            "fixture_evidence_is_authority": False}


def produce_ordinary_contract(control_id,index_raw,held_raw,admission_raw,independent_output_raw,actual_selected_trace,custody_lifetime,independently_selected,resources,meter):
    """Typed future producer consumes complete independent ordinary DATA.
    It never executes a fixture/scanner to manufacture its own expected result;
    current PID/time/native/FD/root facts cannot be substituted with constants.
    Independence/provenance still must be established outside this adapter.
    """
    from .digests import content_sha256
    from .bounds import reserve
    from .schema_validate import validate_role
    if control_id not in ("C30","W-POSITIVE-202") or not isinstance(independent_output_raw,bytes) or independently_selected is not True:
        return None,"control_output_unbound"
    size=len(independent_output_raw)
    if size>67108864: return None,"control_output_unbound"
    tick=reserve(resources,meter,read_bytes=size,live_bytes=size*64,work_bytes=size*8)
    if tick: return None,tick
    output=canonical_loads(independent_output_raw,max_bytes=67108864,max_depth=64)
    if output.get("status")!="OBSERVED" or output.get("cause") is not None or output.get("phase_trace")!=actual_selected_trace:
        return None,"control_output_unbound"
    contract={"id":control_id,"purpose":"retained-202-whole" if control_id=="W-POSITIVE-202" else "retained-exact-subset",
        "input_sha256":[content_sha256(raw,resources,meter) for raw in (index_raw,held_raw,admission_raw)],
        "expected_output":output,"expected_output_ascii":independent_output_raw.decode("ascii"),
        "expected_output_sha256":content_sha256(independent_output_raw,resources,meter),
        "expected_trace":actual_selected_trace,"independently_selected":independently_selected,"custody_lifetime":custody_lifetime}
    cause=validate_role("ordinary_contract",contract,resources,meter)
    return (None,cause) if cause else (contract,None)


def run_ordinary_contract(index_raw,held_raw,admission_raw,stock_context,independent_contract):
    """Future complete normal DATA comparator; no historical fixture route.

    The embedding selects the input triplet, complete output and reached-phase
    trace before this call. An absent full contract stays unaccepted. C30 is an
    explicit calibration and cannot be credited as the performing202 prerequisite.
    """
    from .digests import content_sha256
    if not isinstance(independent_contract,dict):
        return {"status":"FAIL","cause":"control_output_unbound"}
    from .bounds import new_meter
    from .schema_validate import validate_role
    from .digests import DigestStop
    # This metadata comparator owner is explicit and typed.  Its resource
    # readings are not silently credited as the scanner's whole task owner.
    comparator_meter = new_meter()
    try:
        typed = validate_role('ordinary_contract',independent_contract,None,comparator_meter)
    except DigestStop as stop:
        from tools.native_support import retain_source_origin
        retain_source_origin(None,stop)
        typed = stop.cause
    except (MemoryError,OSError,ValueError,KeyError,TypeError) as raw_origin:
        from tools.native_support import retain_source_origin
        retain_source_origin(None,raw_origin)
        typed = 'control_output_unbound'
    if typed:
        return {"status":"FAIL","cause":typed,"comparator_owner":"separate_metadata_owner_not_whole_resource_PASS"}
    selected_id = independent_contract.get("id")
    required = {"id","purpose","input_sha256","expected_output","expected_output_ascii",
                "expected_output_sha256","expected_trace","independently_selected","custody_lifetime"}
    if set(independent_contract) != required or selected_id not in ("C30","W-POSITIVE-202") or independent_contract["independently_selected"] is not True:
        return {"control_id":selected_id,"status":"FAIL","cause":"control_output_unbound"}
    whole = selected_id == "W-POSITIVE-202"
    if independent_contract["purpose"] != ("retained-202-whole" if whole else "retained-exact-subset"):
        return {"control_id":selected_id,"status":"FAIL","cause":"control_output_unbound"}
    from .custody import ROOT_LIFETIME
    lifetime = ROOT_LIFETIME if whole else {"mode":"per-material-complete-observation-generation.v1",
        "max_simultaneous_archive_fds":1,"aggregate_is_simultaneous_custody":False,"reopen_substitution":False}
    if independent_contract["custody_lifetime"] != lifetime:
        return {"control_id":selected_id,"status":"FAIL","cause":"control_output_unbound"}
    inputs = [content_sha256(raw) for raw in (index_raw,held_raw,admission_raw)]
    if inputs != independent_contract["input_sha256"]:
        return {"control_id":selected_id,"status":"FAIL","cause":"control_input_unbound"}
    raw = scan_retained_bytes(index_raw,held_raw,admission_raw,stock_context)
    observed = canonical_loads(raw,max_bytes=67108864,max_depth=64)
    expected = independent_contract["expected_output"]
    trace = observed.get("phase_trace")
    exact = (observed == expected and raw.decode("ascii") == independent_contract["expected_output_ascii"] and
             content_sha256(raw) == independent_contract["expected_output_sha256"] and
             trace == independent_contract["expected_trace"] and observed.get("status") == "OBSERVED" and
             observed.get("cause") is None)
    if whole:
        exact = (exact and len(observed.get("archive_observations",[])) == 202 and len(observed.get("source_joins",[])) == 202 and
            len(observed.get("material_output_receipts",[])) == 202 and
            observed.get("final_custody",{}).get("completion") == "ROOT_ORIGINAL_GENERATIONS_HELD_THROUGH_EXTERNAL_COMMIT" and
            len(observed.get("snapshot",{}).get("members",[])) == 512 and
            len(observed.get("provenance",{}).get("artifacts",[])) == 350)
    return {"control_id":selected_id,"status":"NOT_RUN" if whole and exact else "PASS" if exact else "FAIL","observed":observed,
            "full_output_matched":bool(exact),"actual_trace_matched":trace == independent_contract["expected_trace"],
            # The Source comparator does not observe Root's later publication
            # or FD retirement. Exact Source bytes are not independent Root
            # admission/whole-terminal acceptance, even with a live peer ACK.
            "whole202_credit":False,"source_whole202_observation_match":bool(whole and exact),
            "independent_root_publication":"REQUIRED_NOT_RUN" if whole else "NOT_APPLICABLE",
            "public_consumer":"scan_retained_bytes",
            "fixture_evidence_is_authority":False}
