"""Harness contracts for the RC → 1.0 acceptance wrapper."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _acceptance():
    from tools import release_1_0_acceptance as acceptance

    return acceptance


def test_matrix_is_canonical_and_closed() -> None:
    acceptance = _acceptance()
    matrix = acceptance.load_matrix()
    ids = [item["id"] for item in matrix["capabilities"]]
    assert matrix["schema"] == acceptance.MATRIX_SCHEMA
    assert matrix["not_a_backlog"] is True
    assert len(ids) == len(set(ids))
    assert "CAP-FILES" in ids
    assert "CAP-GEMINI-PARITY" in ids
    cases = {item["id"]: item for item in matrix["cases"]}
    assert cases["R10-J01-UPLOAD-SEARCH-RESTART"]["executable"] is True
    assert cases["R10-LIVE-ANDROID"]["executable"] is False
    assert cases["R10-LIVE-DOC-WORD-FIRST-GEN"]["release_required"] is True
    assert cases["R10-LIVE-DOC-WORD-FIRST-GEN"]["expected_outcome"] == "pass"


def test_wrapper_cannot_emit_go_or_rewrite_sealed_manifests() -> None:
    acceptance = _acceptance()
    plan = acceptance.plan_commands("diagnostic-baseline")
    assert plan["go_emitted"] is False if "go_emitted" in plan else True
    text = json.dumps(plan)
    assert "emit GO" in text or "never prints GO" in plan["go_rule"]
    assert "rewrite sealed A/B manifests" in plan["cannot"]
    a = ROOT / "tests" / "fixtures" / "synthetic_live_battery_a.json"
    b = ROOT / "tests" / "fixtures" / "synthetic_live_battery_b.json"
    assert a.is_file() and b.is_file()


def test_negative_controls_turn_the_oracle_red() -> None:
    report = _acceptance().negative_controls()
    assert report["valid"] is True
    ids = {item["id"] for item in report["results"]}
    assert ids == {
        "R10-NEG-CTRL-WRONG-DIGEST",
        "R10-NEG-CTRL-EMPTY-COLLECTION",
        "R10-NEG-CTRL-FOREIGN-CANARY",
    }


def test_empty_collection_is_a_harness_error_not_pass() -> None:
    acceptance = _acceptance()
    with pytest.raises(acceptance.AcceptanceError, match="zero_collected_cases"):
        acceptance.evaluate_oracle({"expected_outcome": "pass"}, {"collected": False})


def test_audit_only_binds_sealed_batteries_and_does_not_touch_models() -> None:
    acceptance = _acceptance()
    sealed = acceptance.audit_sealed_batteries()
    assert sealed["valid"] is True
    assert sealed["pair"]["cases"] == 400
    assert sealed["acceptance"]["all"] == 160
    assert sealed["acceptance"]["focused"] == 120
    assert sealed["acceptance"]["p06"] == 40


def test_surface_scan_classifies_telegram_ui_and_cli() -> None:
    acceptance = _acceptance()
    matrix = acceptance.load_matrix()
    surfaces = acceptance.discover_surfaces()
    assert any(item == "telegram:chat" for item in surfaces["telegram"])
    assert any(item == "ui:inbox" for item in surfaces["ui"])
    assert any(item == "cli:backup" for item in surfaces["cli"])
    classified = acceptance.classify_surfaces(surfaces, matrix)
    assert classified["classified"] >= 80
    assert classified["unknown"] == []


def test_openapi_surface_is_fully_classified(settings) -> None:
    acceptance = _acceptance()
    matrix = acceptance.load_matrix()
    surfaces = acceptance.discover_surfaces(settings=settings)
    classified = acceptance.classify_surfaces(surfaces, matrix)
    assert classified["unknown"] == []
    assert classified["classified"] == sum(len(group) for group in surfaces.values())


def test_preflight_does_not_emit_secrets() -> None:
    report = _acceptance().preflight()
    blob = json.dumps(report)
    assert "secrets_emitted" in report
    assert report["secrets_emitted"] is False
    assert "api_token" not in blob.lower()
    assert "bearer" not in blob.lower()


def test_plan_final_keeps_red_a_from_starting_b() -> None:
    plan = _acceptance().plan_commands("final")
    assert any("B only if A is green" in item for item in plan["order"])
    assert "tools/synthetic_live_battery.py" in json.dumps(plan["commands"]["live"])
    assert "tools/quality_gate.py --tier exact-release" in " ".join(plan["commands"]["exact_release"])
