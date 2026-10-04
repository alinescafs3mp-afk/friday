"""Terminal byte closure; never grants product installation or live authority."""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import stat
import sys

PACKAGE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("sol019_terminal_receipts", PACKAGE / "tests/receipt_contract.py")
RECEIPTS = sys.modules.get("_friday_receipt_owner")
if RECEIPTS is None:
    RECEIPTS = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name]=RECEIPTS
    _spec.loader.exec_module(RECEIPTS)
OUTER = PACKAGE.parent
SOURCE_NAMES = ("canonical", "pinned_fs", "manifest", "provenance", "archive", "assemble",
                "broker_bootstrap", "broker_runtime", "ledger", "custody_linux",
                "install_bootstrap", "install", "recover", "uninstall")
TEST_NAMES = ("canonical", "manifest", "provenance", "archive", "broker", "ledger",
              "custody", "install_recovery", "uninstall", "effect_bills")
SCHEMA_NAMES = ("package-index", "material-provenance", "snapshot-manifest", "install-grant",
                "installed-authority", "live-grant", "install-journal", "consumed-attempt",
                "attempt-runtime", "terminal-evidence")
BILL_NAMES = ("private-source-preparation", "download-only-provenance", "root-install",
              "live-one-attempt", "revoke-remove")
TERMINALS = {"package-index.v1.json", "manifest.json", "result.json", "test-results.json",
             "source-review-index.json"}
COMPONENTS = ("foundation", "install", "runtime", "foundation-handoff",
              "harness-trust", "harness-graph")
ASSIGNMENT = "ASTRA-E4-QUALITY-STABLE-ROOT-NAMESPACE-CLOSURE-A074"


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("ascii")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load(relative):
    return RECEIPTS.object_member(PACKAGE, relative)


def write_new(relative, value):
    raw = canonical(value)
    path = PACKAGE / relative
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            wrote = os.write(fd, raw[offset:])
            if wrote <= 0:
                raise RuntimeError("terminal short write")
            offset += wrote
        os.fsync(fd)
    finally:
        os.close(fd)
    return digest(raw)


def inventory(normalize=False):
    files, directories = [], []
    if OUTER.is_symlink() or PACKAGE.is_symlink():
        raise RuntimeError("output root substituted")
    if normalize:
        os.chmod(OUTER, 0o700)
    for parent, child_dirs, names in os.walk(PACKAGE, followlinks=False):
        directory = Path(parent)
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeError("foreign/special directory")
        if normalize:
            os.chmod(directory, 0o700)
            metadata = directory.lstat()
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise RuntimeError("terminal directory mode")
        if metadata.st_nlink not in (1, 2 + len(child_dirs)):
            raise RuntimeError("inconsistent directory links")
        directories.append({"path": str(directory.relative_to(PACKAGE)), "mode": 0o700,
                            "nlink": metadata.st_nlink})
        for name in child_dirs:
            if not stat.S_ISDIR((directory / name).lstat().st_mode):
                raise RuntimeError("symlink/special directory entry")
        for name in names:
            path = directory / name
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
                raise RuntimeError("foreign/linked/special terminal member")
            if normalize:
                os.chmod(path, 0o600)
                before = path.lstat()
            if stat.S_IMODE(before.st_mode) != 0o600:
                raise RuntimeError("terminal regular mode")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            try:
                opened = os.fstat(fd)
                h = hashlib.sha256()
                size = 0
                while chunk := os.read(fd, 65536):
                    h.update(chunk)
                    size += len(chunk)
                if normalize:
                    # One terminal durability pass, separate from fake modeled
                    # per-effect fsyncs used by the bounded test matrices.
                    os.fsync(fd)
                after_fd = os.fstat(fd)
            finally:
                os.close(fd)
            after = path.lstat()
            identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
                                  s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if not identity(before) == identity(opened) == identity(after_fd) == identity(after) or size != before.st_size:
                raise RuntimeError("terminal member changed while hashing")
            relative = str(path.relative_to(PACKAGE))
            prefix = relative.split("/", 1)[0]
            role = {"src": "source", "schemas": "schema", "templates": "template",
                    "effects": "effect", "tests": "test", "fixtures": "fixture"}.get(prefix, "document")
            files.append({"path": relative, "role": role, "mode": 0o600,
                          "size": size, "sha256": h.hexdigest()})
    return sorted(files, key=lambda r: r["path"]), sorted(directories, key=lambda r: r["path"])


def symbols():
    result = {}
    for directory in ("src", "tests"):
        for path in sorted((PACKAGE / directory).glob("*.py")):
            module = path.stem
            tree = ast.parse(path.read_bytes(), filename=str(path))
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            result[module + "." + target.id] = {"path": str(path.relative_to(PACKAGE)), "line": node.lineno}
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    key = module + "." + node.name
                    result[key] = {"path": str(path.relative_to(PACKAGE)), "line": node.lineno}
                    if isinstance(node, ast.ClassDef):
                        for method in node.body:
                            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                result[key + "." + method.name] = {"path": str(path.relative_to(PACKAGE)), "line": method.lineno}
    return result


def normalize_reference(value, index):
    """Normalize file-qualified refs without inventing or aliasing symbols."""
    if type(value) is not str or not value:
        raise ValueError("empty/non-string structural reference")
    if ":" in value:
        path, symbol = value.split(":", 1)
        if not re.fullmatch(r"(?:src|tests)/[A-Za-z0-9_]+\.py", path):
            raise ValueError("invalid reference path")
        value = Path(path).stem + "." + symbol
    if value not in index:
        raise ValueError("reference absent from actual AST: " + value)
    return value


def legacy_reference_normalization(index):
    retained, sha = load("fixtures/coordination/legacy-reference-input.json")
    originals = retained["unresolved_symbols"]
    if len(originals) != 995 or len(retained["expanded_phase_effect_bill_rows"]) != 545:
        raise RuntimeError("pinned legacy reference universe changed")
    normalized, unresolved = [], []
    for original in originals:
        match = re.fullmatch(r"(.*):((?:src|tests)/[A-Za-z0-9_]+\.py):([A-Za-z0-9_.]+)", original)
        if match is None:
            unresolved.append({"original": original, "reason": "legacy-reference-shape"})
            continue
        requirement, path, symbol = match.groups()
        try:
            key = normalize_reference(path + ":" + symbol, index)
            normalized.append({"original": original, "requirement": requirement,
                "key": key, **index[key], "historical_execution_credit": False})
        except ValueError as error:
            unresolved.append({"original": original, "reason": str(error)})
    return {"input_ref": "fixtures/coordination/legacy-reference-input.json", "input_sha256": sha,
        "original_reference_count": 995, "normalized": normalized, "unresolved": unresolved,
        "original_expanded_effect_rows": retained["expanded_phase_effect_bill_rows"],
        "historical_effect_rows_are_not_final_snapshot_execution": True}


def historical_contract_projection(lane, history):
    """Old authenticated author data supplies obligations, never new credit."""
    if (type(history) is not dict or history.get("schema") != "friday.sol020.focused-component.v1"
            or history.get("lane") != lane or
            any(history.get(key) is not False for key in ("source_package_accepted", "GO", "independent_acceptance"))):
        raise ValueError("strict inherited author basis; no current authority")
    fields = ("requirement", "status", "source_symbols", "test_symbols", "evidence_keys")
    coverage = [{key: row[key] for key in fields} for row in history.get("coverage", [])]
    for row in coverage:
        if row["status"] not in {"SOURCE_CONTROLLED", "PARTIAL", "UNRESOLVED"}:
            raise ValueError("typed inherited obligation status")
        for field in fields[2:]:
            values = row[field]
            if type(values) is not list or not values or values != list(dict.fromkeys(values)):
                raise ValueError("strict inherited unique references")
    return coverage


def fresh_coverage_contract(lane, inherited):
    """Keep every old obligation row, explicitly add new same-consumer controls."""
    coverage = json.loads(canonical(inherited))
    existing = {row["requirement"] for row in inherited}
    if lane == "install":
        for requirement in ("A019-F12", "A020-F07", "A020-F08", "A020-F17"):
            if requirement in existing:
                coverage.append({"requirement": requirement, "status": "SOURCE_CONTROLLED",
                    "source_symbols": ["install.InstallJournal._parse", "manifest.installation_binding"],
                    "test_symbols": ["test_install_recovery.InstallRecoveryTests.test_pending_copy_semantic_owned_lineage_controls"],
                    "evidence_keys": ["install-pending-copy-semantic:" + key for key in
                        ("unchanged-positive", "absent-owned-object", "member-create-completion-missing")]})
        for requirement in ("A020-F03", "A020-F07", "A020-F08", "A020-F10", "A020-F17"):
            if requirement in existing:
                coverage.append({"requirement": requirement, "status": "SOURCE_CONTROLLED",
                    "source_symbols": ["uninstall.Remover.remove", "install.Installer._resolve_pending",
                        "install.InstallJournal.commit", "uninstall.Remover._fence", "uninstall.Remover.zero_residue"],
                    "test_symbols": ["test_uninstall.UninstallTests.test_direct_removal_pending_copy_catalogue"],
                    "evidence_keys": ["remove-direct-pending-copy:" + key for key in
                        ("empty-stage", "empty-published", "full-applied", "empty-granted", "partial",
                         "wrong-digest", "changed-inode", "changed-metadata", "identity-drift-on-read",
                         "pre-intent-foreign", "partial-then-granted", "wrong-then-granted")]})
    return coverage


def load_pinned_histories(preparation):
    pins, _ = load("schemas/inherited-evidence-pins.v1.json")
    if pins.get("schema") != "friday.a049.inherited-evidence-pins.v1" or pins.get("credit") != "INHERITED_OLD_EVIDENCE/NOT_NEW_CREDIT":
        raise ValueError("strict inherited data pin authority")
    hashes, contracts = {}, {}
    for lane in COMPONENTS:
        row = pins["components"][lane]
        history, sha = load(row["ref"])
        if sha != row["sha256"]:
            raise ValueError("inherited component bytes changed")
        hashes[lane] = sha
        contracts[lane] = fresh_coverage_contract(lane, historical_contract_projection(lane, history))
    return hashes, contracts


def component_snapshot_binding(components, source_hashes, focused=None, historical_hashes=None, historical_contracts=None):
    """Current component authority needs exact schema AND fresh method evidence.

    The focused graph is validated independently once by the terminal consumer;
    historical author reports, current hashes and readiness booleans are not
    substitutes for successful current source-method executions.
    """
    missing, stale, problems = [], [], []
    focused = focused or {"method_ids": [], "observations": [], "complete": False}
    expected_keys = {"schema", "assignment", "generation", "lane", "package_root",
        "final_source_hashes", "coverage", "focused_revalidation", "limitations",
        "source_package_accepted", "GO", "independent_acceptance", "run_context", "evidence_origin"}
    if set(components) != set(COMPONENTS):
        problems.append("exact six component roles absent")
    for lane, component in components.items():
        if not component:
            missing.append(lane)
            continue
        if type(component) is not dict or set(component) != expected_keys or (
            component.get("schema") != "friday.a049.focused-component.v1" or
            component.get("assignment") != ASSIGNMENT or
            component.get("run_context") != RECEIPTS.run_binding() or
            component.get("evidence_origin") != "FRESH_CURRENT_RUN" or
            type(component.get("generation")) is not int or component.get("generation") != 1 or
            component.get("lane") != lane or component.get("package_root") != str(PACKAGE) or
            any(component.get(k) is not False for k in ("source_package_accepted", "GO", "independent_acceptance"))):
            problems.append(lane + ":strict-component-schema-or-authority")
            continue
        pins = component.get("final_source_hashes")
        if pins != source_hashes:
            stale.append({"lane": lane, "reason": "not exact entire final source/harness/config inventory"})
        coverage = component.get("coverage")
        if coverage != (historical_contracts or {}).get(lane):
            problems.append(lane + ":coverage-not-authenticated-historical-contract")
        revalidation = component.get("focused_revalidation")
        if type(coverage) is not list or type(revalidation) is not dict or set(revalidation) != {
            "status", "evidence_ref", "evidence_sha256", "required_method_ids", "observed_method_ids",
            "missing_method_ids", "historical_basis_sha256"}:
            problems.append(lane + ":strict-focused-evidence-schema")
            continue
        required = sorted({method for row in coverage if type(row) is dict
            for method in row.get("test_symbols", [])})
        if lane in ("harness-trust", "harness-graph"):
            required = ["harness_selfcheck.main"]
        observed = sorted(set(required) & set(focused["method_ids"]))
        absent = sorted(set(required) - set(observed))
        if (revalidation.get("required_method_ids") != required or
            revalidation.get("observed_method_ids") != observed or
            revalidation.get("missing_method_ids") != absent or
            type(revalidation.get("historical_basis_sha256")) is not str or
            not re.fullmatch(r"[0-9a-f]{64}", revalidation["historical_basis_sha256"]) or
            revalidation.get("historical_basis_sha256") != (historical_hashes or {}).get(lane) or
            revalidation.get("evidence_ref") != focused.get("ref") or
            revalidation.get("evidence_sha256") != focused.get("sha256") or
            revalidation.get("status") != ("METHOD_REVALIDATED" if required and not absent else "PARTIAL_NOT_RUN")):
            problems.append(lane + ":focused-method-evidence-crosslink")
        if absent or not required:
            problems.append(lane + ":missing-focused-method-execution")
        observations = {}
        for row in focused.get("observations", []):
            observations.setdefault(row["category"] + ":" + row["key"], []).append(row)
        semantics = focused.get("control_contract", {}).get("observation_semantics", {})
        for contract_row in coverage:
            for key in contract_row.get("evidence_keys", []):
                candidates = observations.get(key, [])
                if not candidates or any(row.get("test_id") not in contract_row["test_symbols"] or
                    {"test_id": row.get("test_id"), "outcome": row.get("outcome")} not in semantics.get(key, []) or
                    (contract_row["status"] == "SOURCE_CONTROLLED" and row.get("outcome") == "INCOMPLETE") for row in candidates):
                    problems.append(lane + ":missing-or-wrong-required-key-specific-evidence:" + key)
    return {"complete": not (missing or stale or problems), "missing": missing, "stale": stale,
            "problems": problems, "not_independent_acceptance": True}


def normalized_coverage(components, index, observations, test_ids, contract=None):
    rows, unresolved = [], []
    for lane, component in components.items():
        for original in component.get("coverage", []):
            if type(original) is not dict:
                unresolved.append({"lane": lane, "reason": "malformed coverage row"})
                continue
            row = dict(original)
            row["lane"] = lane
            row["verified_references"] = {}
            problems = []
            for field in ("source_symbols", "test_symbols"):
                refs = row.get(field, [])
                if type(refs) is not list or not refs:
                    problems.append("empty-" + field)
                    continue
                verified = []
                for reference in refs:
                    try:
                        key = normalize_reference(reference, index)
                        verified.append({"key": key, **index[key]})
                    except ValueError as error:
                        problems.append(str(error))
                row["verified_references"][field] = verified
            keys = row.get("evidence_keys", [])
            if not keys and row.get("evidence_key"):
                keys = [row["evidence_key"]]
            if type(keys) is not list or not keys or any(type(k) is not str or ":" not in k for k in keys):
                problems.append("evidence-key-container")
                keys = []
            elif len(set(keys)) != len(keys):
                problems.append("duplicate-evidence-keys")
            retained = []
            semantics = (contract or {}).get("observation_semantics", {})
            for key in keys:
                candidates = observations.get(key, [])
                if not candidates:
                    problems.append("observation-not-in-final-pair:" + str(key))
                    continue
                for observed in candidates:
                    if observed.get("test_id") not in test_ids:
                        problems.append("unattributed-observation:" + str(key))
                    elif observed.get("test_id") not in {r["key"] for r in row["verified_references"].get("test_symbols", [])}:
                        problems.append("wrong-owning-test-method:" + str(key))
                    if observed.get("outcome") not in {"PASS", "REFUSED", "CRASH_RETAINED", "INCOMPLETE"} or (
                            row.get("status") == "SOURCE_CONTROLLED" and observed.get("outcome") == "INCOMPLETE"):
                        problems.append("wrong-evidence-outcome:" + str(key))
                    exact_pair = {"test_id": observed.get("test_id"), "outcome": observed.get("outcome")}
                    if exact_pair not in semantics.get(key, []):
                        problems.append("wrong-or-undeclared-key-specific-owner-outcome:" + str(key))
                    retained.append({"key": key, "test_id": observed.get("test_id"),
                                     "outcome": observed.get("outcome")})
            row["retained_actual_observations"] = retained
            row["mapping_complete"] = not problems
            if problems:
                unresolved.append({"lane": lane, "requirement": row.get("requirement"),
                                   "reasons": problems})
            rows.append(row)
    return rows, unresolved


def read_focused():
    ref = "fixtures/coordination/a049-focused.json"
    if not (PACKAGE / ref).exists():
        return {"method_ids": [], "observations": [], "complete": False, "ref": None, "sha256": None}
    value, sha = load(ref)
    # Producer/consumer use one strict verifier; no synthetic reconstruction of
    # missing launches, and no claims learned from an untrusted local report.
    validated = RECEIPTS.validate_focused(PACKAGE, value)
    return {**validated, "ref": ref, "sha256": sha}


def integration_snapshot_binding(integration, hashes, component_refs, focused, findings_sha, pair=None, preservation=None):
    keys = {"schema", "assignment", "generation", "final_source_hashes", "component_refs",
        "focused_evidence_ref", "focused_evidence_sha256", "review_findings_sha256",
        "all30_findings", "mandatory_source_controls_complete", "remaining_gaps", "child_lifecycle",
        "old_named_pins_rechecked", "independent_acceptance", "source_package_accepted", "GO",
        "official_pair_ref", "official_pair_sha256", "run_context", "evidence_origin"}
    pair = pair or {}
    preservation = preservation or {}
    expected_ids = {"A019-F%02d" % n for n in range(1, 13)} | {"A020-F%02d" % n for n in range(1, 19)}
    return (type(integration) is dict and set(integration) == keys and
        integration.get("schema") == "friday.a049.integration-review.v1" and
        integration.get("assignment") == ASSIGNMENT and integration.get("run_context") == RECEIPTS.run_binding()
        and integration.get("evidence_origin") == "FRESH_CURRENT_RUN" and type(integration.get("generation")) is int and
        integration.get("generation") == 1 and integration.get("final_source_hashes") == hashes and
        integration.get("component_refs") == component_refs and
        integration.get("focused_evidence_ref") == focused.get("ref") and
        integration.get("focused_evidence_sha256") == focused.get("sha256") and
        integration.get("review_findings_sha256") == findings_sha and
        integration.get("official_pair_ref") == pair.get("execution_ref") and
        integration.get("official_pair_sha256") == pair.get("execution_sha256") and
        type(integration.get("all30_findings")) is list and set(integration["all30_findings"]) == expected_ids and
        len(integration["all30_findings"]) == 30 and type(integration.get("remaining_gaps")) is list and
        type(integration.get("mandatory_source_controls_complete")) is bool and
        preservation.get("complete") is True and
        integration.get("child_lifecycle") == preservation.get("child_lifecycle") and
        integration.get("old_named_pins_rechecked") == preservation.get("old_named_pins_rechecked") and
        (not integration["mandatory_source_controls_complete"] or (pair.get("success") is True and pair.get("complete") is True)) and
        all(integration.get(k) is False for k in ("independent_acceptance", "source_package_accepted", "GO")))


def external_owned_digest(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022 or path.is_symlink():
        raise ValueError("external lifecycle member unsafe")
    before = (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    raw = path.read_bytes()
    after = path.lstat()
    if before != (after.st_dev, after.st_ino, after.st_mode, after.st_uid, after.st_gid, after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("external lifecycle member drift")
    return digest(raw)


def validate_preservation(integration, preparation):
    """Bounded named old data preservation; no native registry/history access."""
    if (preparation.get("schema") != "friday.a049.preparation.v1"
            or preparation.get("assignment") != ASSIGNMENT or preparation.get("child_assignments") != []
            or integration.get("child_lifecycle") != []):
        raise ValueError("exact no-child SOURCE preparation")
    ref = integration.get("old_named_pins_rechecked")
    if type(ref) is not dict or set(ref) != {"ref", "sha256"} or ref["ref"] != "fixtures/coordination/old-named-pin-recheck.json":
        raise ValueError("typed named old-pin proof")
    proof, sha = load(ref["ref"])
    if (sha != ref["sha256"] or set(proof) != {"schema", "checked", "whole_old_tree_walked", "old_credit_transferred"}
            or proof["schema"] != "friday.a049.named-old-pin-recheck.v1"
            or proof["whole_old_tree_walked"] is not False or proof["old_credit_transferred"] is not False):
        raise ValueError("old-pin proof schema/hash/authority")
    pins, _ = load("schemas/inherited-evidence-pins.v1.json")
    named = pins["old_named_members"]
    expected = [{"path": row["path"], "sha256": row["sha256"], "metadata_unchanged": True} for row in named]
    if proof["checked"] != expected:
        raise ValueError("exact bounded old named universe")
    for row in named:
        if external_owned_digest(row["path"]) != row["sha256"]:
            raise ValueError("old named bytes changed")
    return {"complete": True, "child_lifecycle": [], "old_named_pins_rechecked": ref}


def read_pair(label):
    result = {"success": False, "complete": False, "runs": [], "reason": "not executed"}
    if not label:
        return result, {}, set()
    pair_path = "fixtures/official-runs/" + label + ".execution.json"
    if not (PACKAGE / pair_path).exists():
        result["reason"] = "execution receipt absent"
        return result, {}, set()
    pair, sha = load(pair_path)
    result.update({"execution_ref": pair_path, "execution_sha256": sha, "execution": pair,
                   "success": pair.get("success") is True, "complete": pair.get("complete") is True})
    if not (result["success"] and result["complete"]):
        result["reason"] = "retained pair incomplete or failed; no official credit"
        return result, {}, set()
    # The producer and terminal consumer apply the SAME strict graph checks.
    RECEIPTS.validate_pair(PACKAGE, pair, label)
    projections = []
    for number in (1, 2):
        base = "fixtures/official-runs/" + label + "-" + str(number) + "/"
        run, run_sha = load(base + "run-result.json")
        invocation, invocation_sha = load(base + "invocation.json")
        projection, projection_sha = load(base + "semantic-projection.json")
        if run.get("success") is not True or type(run["rc"]) is not int or run["rc"] != 0 or run.get("full_inventory") is not True:
            raise RuntimeError("claimed complete run did not pass")
        if run.get("host_effect_fence_installed") is not True or projection["forbidden_effect_attempts"]:
            raise RuntimeError("official host-effect fence/receipt gap")
        if run.get("control_closure", {}).get("complete") is not True:
            raise RuntimeError("claimed pair lacks mandatory source-method control closure")
        if set(run["selected_test_modules"]) != {"test_" + n for n in TEST_NAMES}:
            raise RuntimeError("mandatory ten-module collection changed")
        if set(invocation["isolation"]) != {"isolated", "no_site", "dont_write_bytecode"} or not all(type(v) is int and v == 1 for v in invocation["isolation"].values()):
            raise RuntimeError("isolation receipt invalid")
        for path, expected_sha in projection["source_hashes"].items():
            if digest((PACKAGE / path).read_bytes()) != expected_sha:
                raise RuntimeError("final source/harness differs from executed pair: " + path)
        result["runs"].append({"result_ref": base + "run-result.json", "result_sha256": run_sha,
            "result": run, "invocation_ref": base + "invocation.json",
            "invocation_sha256": invocation_sha, "invocation": invocation,
            "semantic_ref": base + "semantic-projection.json", "semantic_sha256": projection_sha})
        projections.append(projection)
    if result["runs"][0]["semantic_sha256"] != result["runs"][1]["semantic_sha256"]:
        raise RuntimeError("two entire final-source semantic projections differ")
    projection = projections[1]
    expected = projection["control_contract"]
    if set(expected["matrices"]) != set(projection["matrices"]):
        raise RuntimeError("exact precollected matrix union not retained")
    for name, rows in expected["matrices"].items():
        if projection["matrices"][name] != {"expected": rows, "observed": rows}:
            raise RuntimeError("unclosed actual mandatory matrix: " + name)
    observations = {}
    passed_ids = {r["id"] for r in projection["controls"] if r["status"] == "PASS"}
    for observed in projection["observations"]:
        if observed.get("test_id") not in passed_ids:
            raise RuntimeError("observation lacks actual successful method attribution")
        key = observed["category"] + ":" + observed["key"]
        observations.setdefault(key, []).append(observed)
    if set(expected["required_observations"]) - set(observations):
        raise RuntimeError("missing mandatory negative observation")
    result["reason"] = None
    result["same_final_source_harness_configuration"] = True
    result["semantic_match"] = True
    result["kernel_or_privileged_proof"] = False
    return result, observations, passed_ids


def original_finding_accounting(finding, matched):
    complete = bool(matched) and all(r["mapping_complete"] for r in matched)
    assessment = "UNRESOLVED"
    if complete:
        assessments = {r.get("status", "PARTIAL") for r in matched}
        assessment = "SOURCE_CONTROLLED" if assessments == {"SOURCE_CONTROLLED"} else "PARTIAL"
    result = {"original": finding, "status": "UNRESOLVED",
        "source_control_evidence_assessment": assessment,
        "structural_and_actual_evidence_mapping_complete": complete,
        "coverage_rows": matched, "independent_acceptance": False}
    validate_original_finding_accounting(result)
    return result


def validate_original_finding_accounting(row):
    RECEIPTS.require(type(row) is dict and set(row) == {"original", "status",
        "source_control_evidence_assessment", "structural_and_actual_evidence_mapping_complete",
        "coverage_rows", "independent_acceptance"} and row["status"] == "UNRESOLVED"
        and row["independent_acceptance"] is False
        and row["source_control_evidence_assessment"] in {"UNRESOLVED", "PARTIAL", "SOURCE_CONTROLLED"}
        and type(row["structural_and_actual_evidence_mapping_complete"]) is bool
        and type(row["coverage_rows"]) is list,
        "public original obligation remains open; typed source evidence is separate")
    return row


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--accepted-msk", required=True)
    parser.add_argument("--pair-label")
    parser.add_argument("--incomplete-reason", action="append", default=[])
    args = parser.parse_args()
    RECEIPTS.load_run_context(PACKAGE)
    if RECEIPTS.instant(args.accepted_msk) != RECEIPTS.instant(RECEIPTS.run_context()["accepted_at_utc"]):
        raise ValueError("terminal acceptance must equal the same independently admitted RUN")
    os.umask(0o077)
    if any((PACKAGE / p).exists() for p in TERMINALS):
        raise RuntimeError("terminal collision: no overwrite/reuse/cleanup")
    expected = ["README.contract.md", "templates/friday-quality-gate.sudoers.in",
        "fixtures/coordination/finalize_reports.py", "schemas/control-inventory.v1.json",
        "schemas/inherited-evidence-pins.v1.json", "schemas/affected-check-plan.v1.json",
        "schemas/run-context.v1.md"]
    expected += ["tests/" + name + ".py" for name in ("run_tests", "execute_pair", "receipt_contract", "harness_selfcheck", "close_package", "support", "install_controls", "admission_custodian")]
    expected += ["src/" + n + ".py" for n in SOURCE_NAMES]
    expected += ["tests/test_" + n + ".py" for n in TEST_NAMES]
    expected += ["schemas/" + n + ".v1.md" for n in SCHEMA_NAMES]
    expected += ["effects/" + n + ".v1.json" for n in BILL_NAMES]
    missing = [p for p in expected if not (PACKAGE / p).is_file()]
    components, component_refs, limitations = {}, {}, list(args.incomplete_reason)
    for name in COMPONENTS:
        ref = "fixtures/coordination/" + name + "-result.json"
        if not (PACKAGE / ref).exists():
            limitations.append("component terminal result absent:" + name)
            components[name] = {}
            continue
        value, sha = load(ref)
        components[name] = value
        component_refs[name] = {"ref": ref, "sha256": sha}
        limitations.extend(value.get("limitations", []))
    preparation, preparation_sha = load("fixtures/coordination/preparation.json")
    findings, findings_sha = load("fixtures/coordination/review-findings.json")
    integration, integration_sha = load("fixtures/coordination/integration-review.json")
    current_source = RECEIPTS.source_inventory(PACKAGE)
    focused = read_focused()
    historical_hashes, historical_contracts = load_pinned_histories(preparation)
    snapshot_binding = component_snapshot_binding(components, current_source, focused, historical_hashes, historical_contracts)
    pair, observations, passed_ids = read_pair(args.pair_label)
    preservation = validate_preservation(integration, preparation)
    integration_binding = integration_snapshot_binding(integration, current_source,
        component_refs, focused, findings_sha, pair, preservation)
    index = symbols()
    for key, entry in index.items():
        path = PACKAGE / entry["path"]
        entry["source_sha256"] = digest(path.read_bytes())
    contract = focused.get("control_contract", {})
    coverage, unresolved = normalized_coverage(components, index, observations, passed_ids, contract)
    focused_observations = {}
    for row in focused.get("observations", []):
        focused_observations.setdefault(row["category"] + ":" + row["key"], []).append(row)
    focused_coverage, focused_unresolved = normalized_coverage(components, index,
        focused_observations, set(focused["method_ids"]), contract)
    legacy = legacy_reference_normalization(index)
    by_finding = {}
    for finding in findings["findings"]:
        identifier = finding["id"]
        matched = [r for r in coverage if r.get("requirement") == identifier]
        by_finding[identifier] = original_finding_accounting(finding, matched)
    if set(by_finding) != {"A019-F%02d" % n for n in range(1, 13)} | {"A020-F%02d" % n for n in range(1, 19)}:
        raise RuntimeError("all30 pinned review findings not retained")
    source_controls_complete = (pair["success"] and pair["complete"] and not missing
        and not unresolved and integration.get("mandatory_source_controls_complete") is True
        and not legacy["unresolved"]
        and snapshot_binding["complete"] and integration_binding
        and all(f["structural_and_actual_evidence_mapping_complete"] and f["source_control_evidence_assessment"] == "SOURCE_CONTROLLED"
            for f in by_finding.values()))
    status = "READY_FOR_INDEPENDENT_CORRECTION_REVIEW" if source_controls_complete else "INCOMPLETE_CORRECTION"
    # This assignment expressly does not select or implement an accepted kernel boundary.
    proof_gaps = [
        "Real same-UID post-exec and descendant whole-channel custody: NOT_PROVEN; production GO remains refused.",
        "Executing protected bootstrap/interpreter startup lifetime and authentic external anchor: NOT_PROVEN.",
        "Actual raw publisher provenance and full material/toolchain compatibility: NOT_PROVEN.",
        "Host kernel/ABI/lease proof and real protected install/live/final quality gate: NOT_EXECUTED/NOT_AUTHORIZED."
    ]
    limitations = sorted(set(str(item) for item in limitations + proof_gaps))
    inventory(normalize=True)
    test_sha = write_new("test-results.json", {"schema": "friday.a049.test-results.v1",
        "run_context": RECEIPTS.run_binding(), "official_pair": pair, "source_controls_complete": source_controls_complete,
        "focused_revalidation": focused,
        "historical_diagnostics": preparation["historical_diagnostics"],
        "new_diagnostic_receipts_are_not_official_credit": True,
        "fixture_metadata_sealing": "Retained model metadata remains in receipts; physical container dirs0700/files0600 before seal.",
        "privileged_effects": 0, "network_calls": 0, "product_model_calls": 0})
    review_sha = write_new("source-review-index.json", {"schema": "friday.a049.source-review-index.v1",
        "run_context": RECEIPTS.run_binding(),
        "review_key": "cecd28a9/A049/all30/context-G2-G3/source-closure/g1",
        "blueprint_ref": "/home/jericho/.jericho/runtime/subagent-lifecycle/ASTRA-E4-PROTECTED-BROKER-BLUEPRINT-A009-G2-RESULT.md",
        "blueprint_sha256": "4020a7121dc4a23c0d98700c3495172feafef4e191b4a4eb0f57363349e0904f",
        "reference_normalization": "src/file.py:Symbol -> file.Symbol, each exact AST and byte digest; no guessed aliases.",
        "symbols": index, "components": component_refs, "normalized_coverage": coverage,
        "focused_coverage": focused_coverage, "focused_unresolved_references": focused_unresolved,
        "focused_coverage_is_not_entire_official_pair_credit": True,
        "legacy_reference_normalization": legacy,
        "final_executed_matrices": (pair["runs"][1]["result"]["control_closure"] if pair["success"] else {}),
        "all30_findings": by_finding, "review_findings_sha256": findings_sha,
        "unresolved_references": unresolved, "integration_review": integration,
        "component_snapshot_binding": snapshot_binding, "integration_snapshot_binding": integration_binding,
        "integration_review_sha256": integration_sha, "independent_review": "PENDING"})
    accepted = datetime.fromisoformat(args.accepted_msk)
    completed = datetime.now(timezone.utc).astimezone(accepted.tzinfo)
    result = {"schema": "friday.a049.source-result.v1",
        "assignment": ASSIGNMENT, "generation": 1,
        "run_context": RECEIPTS.run_binding(), "execution_assignment": RECEIPTS.run_context()["assignment"],
        "source_assignment": RECEIPTS.SOURCE_ASSIGNMENT,
        "accepted_at_msk": args.accepted_msk, "completed_at_msk": completed.isoformat(),
        "elapsed_seconds": (completed - accepted).total_seconds(),
        "source_package": "BLOCKED_SOURCE_PACKAGE_INCOMPLETE",
        "source_correction_status": status, "source_controls_complete": source_controls_complete,
        "focused_component_bindings_complete": snapshot_binding["complete"],
        "focused_mapping_unresolved": focused_unresolved,
        "source_package_accepted": False, "independent": False, "independent_review": "PENDING",
        "all30_statuses": {key: item["status"] for key, item in by_finding.items()},
        "all30_source_control_evidence_assessments": {
            key: item["source_control_evidence_assessment"] for key, item in by_finding.items()},
        "original_obligation_acceptance": "NOT_ACCEPTED; separate actual independent final acceptance required",
        "missing_files": missing, "limitations": limitations, "unresolved_references": unresolved,
        "official_full_pair_completed": pair["success"] and pair["complete"],
        "test_results_sha256": test_sha, "source_review_index_sha256": review_sha,
        "preparation_sha256": preparation_sha, "component_results": component_refs,
        "material_set": "NO_COMPLETE_OFFLINE_PROVENANCE", "root_install": "NOT_AUTHORIZED",
        "live_attempt": "NOT_AUTHORIZED", "release_credit": "NONE",
        "SOL018_architecture_selected": False, "production_GO_refusal_preserved": True,
        "candidate_mutated": False, "shared_repository_mutated": False, "network_calls": 0,
        "configured_task_children": len(preparation["child_assignments"]),
        "product_model_calls": 0, "privileged_effects": 0, "protected_toolchain_installed": False,
        "quality_gate_executed": False, "r6_created": False, "build": False,
        "install": False, "release": False, "GO": False}
    result_sha = write_new("result.json", result)
    files, directories = inventory()
    indexed = [r for r in files if r["path"] not in TERMINALS]
    index_sha = write_new("package-index.v1.json", {"schema": "friday.package-index.v1", "members": indexed})
    files, directories = inventory()
    manifest_sha = write_new("manifest.json", {"schema": "friday.a049.terminal-manifest.v1",
        "run_context": RECEIPTS.run_binding(),
        "package_index_sha256": index_sha, "members": files, "directories": directories})
    final_files, final_dirs = inventory()
    if [r for r in final_files if r["path"] != "manifest.json"] != files or final_dirs != directories:
        raise RuntimeError("post-seal exact inventory drift")
    for relative in sorted((r["path"] for r in final_dirs), key=lambda p: (-p.count("/"), p)):
        fd = os.open(PACKAGE / relative, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    fd = os.open(OUTER, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    print(json.dumps({"source_package": result["source_package"], "source_correction_status": status,
        "result_sha256": result_sha, "manifest_sha256": manifest_sha, "package_index_sha256": index_sha,
        "regular_members": len(final_files), "directory_members": len(final_dirs),
        "completed_at_msk": result["completed_at_msk"], "elapsed_seconds": result["elapsed_seconds"]}, sort_keys=True))


if __name__ == "__main__":
    import time
    _wall,_mono=time.time(),time.monotonic()
    RECEIPTS.install_root_observer("terminal",(_wall+30,_mono+30))
    RECEIPTS.load_run_context(PACKAGE)
    _end=RECEIPTS.instant(RECEIPTS.run_context()["deadline_at_utc"]).timestamp()
    RECEIPTS.raw_client().ends=(_end,_mono+_end-_wall)
    RECEIPTS.instrument_root_module(globals(),"terminal")
    _exit=125
    try:
        _value=RECEIPTS.observe_root_call("terminal.main",main)
        _exit=0 if _value is None else _value
    finally:RECEIPTS.observer_terminal(_exit)
    raise SystemExit(_exit)
