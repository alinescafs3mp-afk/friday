"""Create-once fresh current evidence reports from strictly consumed receipts.

Static source preparation does not execute this producer or give runtime credit.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[2]


def module(name, path):
    owner=sys.modules.get("_friday_receipt_owner")
    if owner is not None:return owner.observed_load(name,path)
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


R = module("a049_report_receipts", PACKAGE / "tests/receipt_contract.py")
C = None


def new(relative, value):
    return C.write_new(relative, value)


def old_named_proof():
    pins, _ = R.object_member(PACKAGE, "schemas/inherited-evidence-pins.v1.json")
    checked = []
    for row in pins["old_named_members"]:
        if C.external_owned_digest(row["path"]) != row["sha256"]:
            raise ValueError("exact old named bytes drift:" + row["path"])
        checked.append({"path": row["path"], "sha256": row["sha256"], "metadata_unchanged": True})
    return {"schema": "friday.a049.named-old-pin-recheck.v1", "checked": checked,
        "whole_old_tree_walked": False, "old_credit_transferred": False}


def current_components(focused, hashes, preparation):
    historical, contracts = C.load_pinned_histories(preparation)
    components, refs = {}, {}
    for lane in C.COMPONENTS:
        coverage = contracts[lane]
        required = sorted({method for row in coverage for method in row["test_symbols"]})
        if lane in ("harness-trust", "harness-graph"):
            required = ["harness_selfcheck.main"]
        observed = sorted(set(required) & set(focused["method_ids"]))
        absent = sorted(set(required) - set(observed))
        if absent or not required:
            raise ValueError("fresh required component method execution absent:" + lane)
        report = {"schema": "friday.a049.focused-component.v1", "assignment": C.ASSIGNMENT,
            "generation": 1, "lane": lane, "package_root": str(PACKAGE),
            "final_source_hashes": hashes, "run_context": R.run_binding(),
            "evidence_origin": "FRESH_CURRENT_RUN", "coverage": coverage,
            "focused_revalidation": {"status": "METHOD_REVALIDATED", "evidence_ref": focused["ref"],
                "evidence_sha256": focused["sha256"], "required_method_ids": required,
                "observed_method_ids": observed, "missing_method_ids": absent,
                "historical_basis_sha256": historical[lane]},
            "limitations": ["INHERITED_OLD_EVIDENCE/NOT_NEW_CREDIT; new credit requires this exact current RUN graph.",
                "Source recording only; kernel/startup/material/channel proofs NOT_PROVEN."],
            "source_package_accepted": False, "GO": False, "independent_acceptance": False}
        components[lane] = report
        relative = "fixtures/coordination/" + lane + "-result.json"
        refs[lane] = {"ref": relative, "sha256": new(relative, report)}
    binding = C.component_snapshot_binding(components, hashes, focused, historical, contracts)
    if not binding["complete"]:
        raise ValueError("fresh component binding incomplete:" + json.dumps(binding))
    return components, refs, binding


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair-label")
    args = parser.parse_args()
    R.load_run_context(PACKAGE)
    C.RECEIPTS.load_run_context(PACKAGE)
    os.umask(0o077)
    hashes = R.source_inventory(PACKAGE)
    preparation, _ = R.object_member(PACKAGE, "fixtures/coordination/preparation.json")
    focused = C.read_focused()
    if not focused.get("ref"):
        raise ValueError("no new authenticated focused evidence")
    components, refs, binding = current_components(focused, hashes, preparation)
    pair, observations, passed_ids = C.read_pair(args.pair_label)
    findings, findings_sha = R.object_member(PACKAGE, "fixtures/coordination/review-findings.json")
    pins, _ = R.object_member(PACKAGE, "schemas/inherited-evidence-pins.v1.json")
    if findings_sha != pins["review_findings"]["sha256"]:
        raise ValueError("all30 original finding bytes changed")
    old_proof_sha = new("fixtures/coordination/old-named-pin-recheck.json", old_named_proof())
    index = C.symbols()
    observation_index = {}
    for row in observations.values():
        for observation in row:
            observation_index.setdefault(observation["category"] + ":" + observation["key"], []).append(observation)
    mapping, unresolved = C.normalized_coverage(components, index, observation_index, passed_ids,
        focused["control_contract"])
    full = pair["success"] and pair["complete"] and binding["complete"] and not unresolved
    integration = {"schema": "friday.a049.integration-review.v1", "assignment": C.ASSIGNMENT,
        "generation": 1, "final_source_hashes": hashes, "component_refs": refs,
        "run_context": R.run_binding(), "evidence_origin": "FRESH_CURRENT_RUN",
        "focused_evidence_ref": focused["ref"], "focused_evidence_sha256": focused["sha256"],
        "review_findings_sha256": findings_sha, "all30_findings": sorted(row["id"] for row in findings["findings"]),
        "official_pair_ref": pair.get("execution_ref"), "official_pair_sha256": pair.get("execution_sha256"),
        "mandatory_source_controls_complete": full,
        "remaining_gaps": [] if full else ["Two complete final-source passes NOT_RUN/FAILED; no official credit."],
        "child_lifecycle": [],
        "old_named_pins_rechecked": {"ref": "fixtures/coordination/old-named-pin-recheck.json", "sha256": old_proof_sha},
        "independent_acceptance": False, "source_package_accepted": False, "GO": False}
    preservation = C.validate_preservation(integration, preparation)
    if not C.integration_snapshot_binding(integration, hashes, refs, focused, findings_sha, pair, preservation):
        raise ValueError("strict fresh integration binding incomplete")
    new("fixtures/coordination/integration-review.json", integration)
    print(json.dumps({"focused_bindings_complete": binding["complete"],
        "official_pair_complete": pair["success"] and pair["complete"],
        "source_package_accepted": False, "GO": False}, sort_keys=True))


if __name__ == "__main__":
    import time
    _wall,_mono=time.time(),time.monotonic()
    R.install_root_observer("reports",(_wall+30,_mono+30))
    R.load_run_context(PACKAGE)
    _end=R.instant(R.run_context()["deadline_at_utc"]).timestamp()
    R.raw_client().ends=(_end,_mono+_end-_wall)
    C = R.observed_load("a049_report_closer",PACKAGE/"tests/close_package.py")
    R.instrument_root_module(globals(),"reports")
    C.RECEIPTS=R
    R.instrument_root_module(C.__dict__,"terminal_helper")
    _exit=125
    try:
        _value=R.observe_root_call("reports.main",main)
        _exit=0 if _value is None else _value
    finally:R.observer_terminal(_exit)
    raise SystemExit(_exit)
