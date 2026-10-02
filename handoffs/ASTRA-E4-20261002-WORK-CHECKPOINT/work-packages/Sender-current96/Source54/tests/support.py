"""Retained private test roots and actual-control observations, never authority."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys

PACKAGE = Path(__file__).resolve().parents[1]
OBSERVATIONS = []
MATRICES = {}
MATRIX_TEST_IDS = {}
CURRENT_TEST_ID = None
EXPECTED_MATRICES = {}
REQUIRED_OBSERVATIONS = set()
OBSERVATION_SEMANTICS = {}
MATRIX_OWNERS = {}
DECLARED_MODULES = []
_COUNTS = {}


def retained_root(label):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,160}", label):
        raise ValueError("invalid retained-root label")
    configured = os.environ.get("SOL017_RUN_ROOT")
    if not configured:
        raise ValueError("explicit SOL017_RUN_ROOT required; no default temp root")
    parent = Path(configured)
    parent = parent.resolve(strict=True)
    official = (PACKAGE / "fixtures" / "official-runs").resolve(strict=True)
    lane = os.environ.get("SOL019_DIAGNOSTIC_LANE")
    if lane:
        if lane not in ("sol019_foundation", "sol019_install", "sol019_runtime", "sol019_handoff",
                        "sol020_controls", "sol020_install", "sol020_harness"):
            raise ValueError("invalid bounded diagnostic lane")
        official = (PACKAGE / "fixtures/coordination" / lane / "diagnostic-runs").resolve(strict=True)
    if official not in parent.parents or parent.is_symlink():
        raise ValueError("run root outside retained official-runs")
    count = _COUNTS.get(label, 0) + 1
    _COUNTS[label] = count
    root = parent / f"{label}-{count:05d}"
    root.mkdir(mode=0o700)
    return root


def observe(category, key, outcome, **evidence):
    """Tests call only after executing the real parser/state-machine assertion."""
    if any(type(value) is not str or not value for value in (category, key, outcome)) or outcome not in {"PASS", "REFUSED", "CRASH_RETAINED", "INCOMPLETE"}:
        raise ValueError("observation exact types/outcome semantics")
    if outcome == "INCOMPLETE" and not category.endswith("-gap"):
        raise ValueError("incomplete proof must remain an explicit gap")
    identity = category + ":" + key
    if {"test_id": CURRENT_TEST_ID, "outcome": outcome} not in OBSERVATION_SEMANTICS.get(identity, []):
        raise ValueError("observation differs from declared method/outcome semantics: " + identity)
    if any((r["category"], r["key"], r["test_id"]) == (category, key, CURRENT_TEST_ID) for r in OBSERVATIONS):
        raise ValueError("duplicate observation identity")
    record = {"category": category, "key": key, "outcome": outcome,
              "evidence": evidence, "test_id": CURRENT_TEST_ID}
    json.dumps(record, allow_nan=False, sort_keys=True)
    OBSERVATIONS.append(record)


def matrix(name, expected, observed):
    expected, observed = sorted(expected), sorted(observed)
    if expected != observed or len(set(expected)) != len(expected):
        raise AssertionError(f"unclosed or duplicated fault matrix: {name}")
    if name in MATRICES:
        raise AssertionError(f"matrix recorded more than once: {name}")
    if name not in EXPECTED_MATRICES or expected != EXPECTED_MATRICES[name]:
        raise AssertionError(f"matrix differs from pre-execution collection: {name}")
    if MATRIX_OWNERS[name] != CURRENT_TEST_ID:
        raise AssertionError(f"matrix differs from declared ordinary method: {name}")
    MATRICES[name] = {"expected": expected, "observed": observed}
    MATRIX_TEST_IDS[name] = CURRENT_TEST_ID


def control_semantics(module, class_name, groups):
    """Explicit source-assertion partitions, evaluated before ordinary methods run.

    A group is (ordinary_method_name, exact_outcome, independently listed keys).
    This helper never reads observations or a prior receipt.
    """
    semantics = {}
    for method, outcome, keys in groups:
        owner = module + "." + class_name + "." + method
        _ordinary_owner(module, owner)
        if outcome not in {"PASS", "REFUSED", "CRASH_RETAINED", "INCOMPLETE"}:
            raise ValueError("declared outcome")
        for key in keys:
            pair = {"test_id": owner, "outcome": outcome}
            if pair in semantics.setdefault(key, []):
                raise ValueError("duplicate declared observation pair: " + key)
            semantics[key].append(pair)
    return {key: sorted(pairs, key=lambda pair: (pair["test_id"], pair["outcome"]))
            for key, pairs in sorted(semantics.items())}


def _ordinary_owner(module, owner):
    if type(owner) is not str or owner.count(".") != 2:
        raise ValueError("exact ordinary method owner")
    declared_module, class_name, method = owner.split(".")
    klass = getattr(sys.modules.get(module), class_name, None)
    if declared_module != module or not method.startswith("test_") or not callable(getattr(klass, method, None)):
        raise ValueError("owner absent from declaring module ordinary methods")


def declare_control_contract(module, contract):
    """Collect independently derived row sets before executing any test case."""
    if module in DECLARED_MODULES or type(contract) is not dict:
        raise ValueError("duplicate/malformed control contract")
    if set(contract) != {"matrices", "required_observations", "observation_semantics", "matrix_owners"}:
        raise ValueError("control contract keys")
    matrices, required = contract["matrices"], contract["required_observations"]
    if type(matrices) is not dict or type(required) is not list:
        raise ValueError("control contract containers")
    for name, rows in matrices.items():
        if type(name) is not str or not name or name in EXPECTED_MATRICES:
            raise ValueError("duplicate/malformed expected matrix name")
        if type(rows) is not list or not rows or len(rows) > 1_000_000:
            raise ValueError("empty/malformed/unbounded matrix")
        if any(type(row) is not str or not row or len(row) > 2048 for row in rows):
            raise ValueError("matrix row types")
        if rows != sorted(set(rows)):
            raise ValueError("expected matrix rows must be sorted and unique")
    if any(type(key) is not str or ":" not in key or len(key) > 2048 for key in required):
        raise ValueError("required observation type")
    if required != sorted(set(required)):
        raise ValueError("required observations must be sorted and unique")
    semantics, owners = contract["observation_semantics"], contract["matrix_owners"]
    if type(semantics) is not dict or type(owners) is not dict or set(owners) != set(matrices):
        raise ValueError("exact semantic and matrix owner maps")
    if not set(required) <= set(semantics) or set(semantics) & set(OBSERVATION_SEMANTICS):
        raise ValueError("missing/duplicate declared key semantics")
    for key, pairs in semantics.items():
        if type(key) is not str or ":" not in key or len(key) > 2048 or type(pairs) is not list or not pairs:
            raise ValueError("declared observation semantic types")
        identities = []
        for pair in pairs:
            if type(pair) is not dict or set(pair) != {"test_id", "outcome"}:
                raise ValueError("exact declared semantic pair")
            _ordinary_owner(module, pair["test_id"])
            outcome = pair["outcome"]
            if type(outcome) is not str or outcome not in {"PASS", "REFUSED", "CRASH_RETAINED", "INCOMPLETE"}:
                raise ValueError("declared outcome type/value")
            if outcome == "INCOMPLETE" and not key.split(":", 1)[0].endswith("-gap"):
                raise ValueError("declared incomplete proof must be gap")
            identities.append((pair["test_id"], outcome))
        if identities != sorted(set(identities)):
            raise ValueError("duplicate/unsorted semantic owner/outcome pairs")
    for name, owner in owners.items():
        _ordinary_owner(module, owner)
    EXPECTED_MATRICES.update({name: list(rows) for name, rows in matrices.items()})
    REQUIRED_OBSERVATIONS.update(required)
    OBSERVATION_SEMANTICS.update({key: list(pairs) for key, pairs in semantics.items()})
    MATRIX_OWNERS.update(owners)
    DECLARED_MODULES.append(module)


def control_contract():
    return {"matrices": dict(sorted(EXPECTED_MATRICES.items())),
            "required_observations": sorted(REQUIRED_OBSERVATIONS),
            "observation_semantics": dict(sorted(OBSERVATION_SEMANTICS.items())),
            "matrix_owners": dict(sorted(MATRIX_OWNERS.items())),
            "declared_modules": sorted(DECLARED_MODULES)}


def control_closure():
    actual = {row["category"] + ":" + row["key"] for row in OBSERVATIONS}
    missing = sorted(set(EXPECTED_MATRICES) - set(MATRICES))
    extra = sorted(set(MATRICES) - set(EXPECTED_MATRICES))
    mismatched = sorted(name for name in set(EXPECTED_MATRICES) & set(MATRICES)
        if MATRICES[name]["expected"] != EXPECTED_MATRICES[name]
        or MATRICES[name]["observed"] != EXPECTED_MATRICES[name])
    absent = sorted(REQUIRED_OBSERVATIONS - actual)
    return {"complete": not (missing or extra or mismatched or absent),
            "missing_matrices": missing, "unexpected_matrices": extra,
            "mismatched_matrices": mismatched, "missing_observations": absent,
            "kernel_or_privileged_enforcement_credit": False}


def load_source(name):
    """The driver exact-spec loads the closed source set; never add a sys.path."""
    if name not in sys.modules:
        raise ValueError(f"source module not authenticated by test driver: {name}")
    return sys.modules[name]


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()
