"""Focused adversarial checks for LAB-270 Office count HTTP census.

Independent classification is prompt + production routing, not observed FAIL.
Structural-zero: Lab268 predicate six plus independently proved nominal
count_records (C conjunction). Every remaining model-owned case requires HTTP.
No pass-wide waiver. Tests are authored, not executed, in the lab turn.
"""

from __future__ import annotations

import copy

import pytest

from tools import synthetic_live_battery as battery

_B_STRUCTURAL = frozenset(
    {
        "SYN-B03-02",
        "SYN-B03-05",
        "SYN-B03-06",
        "SYN-B03-08",
        "SYN-B03-09",
        "SYN-B03-10",
        "SYN-B03-11",
        "SYN-B03-12",
        "SYN-B03-16",
        "SYN-B03-17",
        "SYN-B03-18",
        "SYN-B03-19",
        "SYN-B03-20",
    }
)
_B_MODEL = frozenset(
    {
        "SYN-B03-01",
        "SYN-B03-03",
        "SYN-B03-04",
        "SYN-B03-07",
        "SYN-B03-13",
        "SYN-B03-14",
        "SYN-B03-15",
    }
)
_A_STRUCTURAL = frozenset(
    {
        "SYN-A03-03",
        "SYN-A03-06",
        "SYN-A03-08",
        "SYN-A03-15",
        "SYN-A03-19",
        "SYN-A03-20",
    }
)
_A_MODEL = frozenset(
    {
        "SYN-A03-01",
        "SYN-A03-02",
        "SYN-A03-04",
        "SYN-A03-05",
        "SYN-A03-07",
        "SYN-A03-09",
        "SYN-A03-10",
        "SYN-A03-11",
        "SYN-A03-12",
        "SYN-A03-13",
        "SYN-A03-14",
        "SYN-A03-16",
        "SYN-A03-17",
        "SYN-A03-18",
    }
)
_REVIEWED_NOMINAL_BODIES = frozenset({"SYN-B03-02", "SYN-B03-06", "SYN-B03-19"})


def _pass_cases(battery_id: str) -> list[battery.ExpandedCase]:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS[battery_id])
    return [
        case
        for case in battery.expand_manifest_cases(manifest)
        if case.oracle_profile == "package_c_exact_documents"
    ]


def _closed_case_delta(case: battery.ExpandedCase) -> dict[str, int]:
    model_owned = not battery._package_c_structural_case(case)
    delta = {
        **dict.fromkeys(battery._P01_CODE_OWNED_DELTA_ZERO_COUNTERS, 0),
        "model_http": int(model_owned),
        "embedding_http": 0,
        "reranker_http": 0,
        "other_http": 0,
    }
    delta.update(dict.fromkeys(battery._HTTP_PRIVACY_COUNTER_KEYS, 0))
    return delta


def _route_evidence(case: battery.ExpandedCase) -> dict[str, bool | None]:
    return {
        "fabricated_outside_deed_request": False,
        "answer_present": True,
        "model_spoke": not battery._package_c_structural_case(case),
        "outside_deed_replaced": False,
        "supported_deed_replaced": False,
        "remainder_known": True,
        "llm_failed": False,
    }


def _ledgers(
    cases: list[battery.ExpandedCase],
) -> tuple[list[tuple[str, dict[str, int]]], list[tuple[str, dict[str, bool | None]]], dict[str, int]]:
    delta_ledger = [(case.id, _closed_case_delta(case)) for case in cases]
    evidence_ledger = [(case.id, _route_evidence(case)) for case in cases]
    total = {key: sum(delta[key] for _case_id, delta in delta_ledger) for key in delta_ledger[0][1]}
    return delta_ledger, evidence_ledger, total


def test_twenty_cases_retained_on_both_package_c_passes() -> None:
    for battery_id in ("A", "B"):
        cases = _pass_cases(battery_id)
        assert len(cases) == battery.QUESTIONS_PER_PASS == 20
        assert [case.question_index for case in cases] == list(range(1, 21))


def test_b_p03_structural_zero_ids_match_independent_matrix() -> None:
    cases = _pass_cases("B")
    observed = {case.id for case in cases if battery._package_c_structural_case(case)}
    model = {case.id for case in cases if not battery._package_c_structural_case(case)}
    assert observed == _B_STRUCTURAL
    assert model == _B_MODEL
    assert observed | model == {case.id for case in cases}
    assert not (observed & model)
    assert observed >= _REVIEWED_NOMINAL_BODIES
    assert observed >= battery._PACKAGE_C_PREDICATE_ZERO_IDS


def test_a_p03_structural_zero_ids_match_independent_matrix() -> None:
    cases = _pass_cases("A")
    observed = {case.id for case in cases if battery._package_c_structural_case(case)}
    model = {case.id for case in cases if not battery._package_c_structural_case(case)}
    assert observed == _A_STRUCTURAL
    assert model == _A_MODEL
    assert len(model) == 14
    assert not any(case.id in battery._PACKAGE_C_PREDICATE_ZERO_IDS for case in cases)


def test_closed_mixed_census_accepts_b_and_a() -> None:
    for battery_id, expected_http in (("B", 7), ("A", 14)):
        cases = _pass_cases(battery_id)
        delta_ledger, evidence_ledger, total = _ledgers(cases)
        assert total["model_http"] == expected_http
        assert battery._http_probe_reconciliation_exact(cases, delta_ledger, evidence_ledger, total) is True


@pytest.mark.parametrize("case_id", sorted(_B_MODEL | _A_MODEL))
def test_missing_http_on_every_model_owned_case_is_rejected(case_id: str) -> None:
    battery_id = case_id[4]
    cases = _pass_cases(battery_id)
    delta_ledger, evidence_ledger, _total = _ledgers(cases)
    index = next(i for i, case in enumerate(cases) if case.id == case_id)
    assert battery._package_c_structural_case(cases[index]) is False
    forged = copy.deepcopy(delta_ledger)
    forged[index][1]["model_http"] = 0
    total = {key: sum(delta[key] for _case_id, delta in forged) for key in forged[0][1]}
    assert battery._http_probe_reconciliation_exact(cases, forged, evidence_ledger, total) is False


@pytest.mark.parametrize("case_id", sorted(_B_STRUCTURAL | _A_STRUCTURAL))
def test_forged_http_on_every_structural_zero_case_is_rejected(case_id: str) -> None:
    battery_id = case_id[4]
    cases = _pass_cases(battery_id)
    delta_ledger, evidence_ledger, _total = _ledgers(cases)
    index = next(i for i, case in enumerate(cases) if case.id == case_id)
    assert battery._package_c_structural_case(cases[index]) is True
    forged = copy.deepcopy(delta_ledger)
    forged[index][1]["model_http"] = 1
    total = {key: sum(delta[key] for _case_id, delta in forged) for key in forged[0][1]}
    assert battery._http_probe_reconciliation_exact(cases, forged, evidence_ledger, total) is False


@pytest.mark.parametrize("battery_id", ["A", "B"])
def test_contradictory_swap_census_is_rejected(battery_id: str) -> None:
    cases = _pass_cases(battery_id)
    delta_ledger, evidence_ledger, _total = _ledgers(cases)
    structural = next(i for i, case in enumerate(cases) if battery._package_c_structural_case(case))
    model = next(i for i, case in enumerate(cases) if not battery._package_c_structural_case(case))
    forged = copy.deepcopy(delta_ledger)
    forged[structural][1]["model_http"] = 1
    forged[model][1]["model_http"] = 0
    total = {key: sum(delta[key] for _case_id, delta in forged) for key in forged[0][1]}
    assert total["model_http"] == sum(1 for case in cases if not battery._package_c_structural_case(case))
    assert battery._http_probe_reconciliation_exact(cases, forged, evidence_ledger, total) is False


@pytest.mark.parametrize("battery_id", ["A", "B"])
def test_incomplete_pass_is_rejected(battery_id: str) -> None:
    cases = _pass_cases(battery_id)
    delta_ledger, evidence_ledger, total = _ledgers(cases)
    assert (
        battery._http_probe_reconciliation_exact(cases[:-1], delta_ledger[:-1], evidence_ledger[:-1], total)
        is False
    )
    assert battery._http_probe_reconciliation_exact(cases[:0], [], [], {"model_http": 0}) is False


@pytest.mark.parametrize("battery_id", ["A", "B"])
def test_pass_wide_zero_http_waiver_is_rejected(battery_id: str) -> None:
    cases = _pass_cases(battery_id)
    delta_ledger, evidence_ledger, _total = _ledgers(cases)
    waived = copy.deepcopy(delta_ledger)
    for _case_id, delta in waived:
        delta["model_http"] = 0
    total = {key: sum(delta[key] for _case_id, delta in waived) for key in waived[0][1]}
    assert total["model_http"] == 0
    assert battery._http_probe_reconciliation_exact(cases, waived, evidence_ledger, total) is False


@pytest.mark.parametrize("battery_id", ["A", "B"])
def test_pass_wide_all_model_http_one_waiver_is_rejected(battery_id: str) -> None:
    cases = _pass_cases(battery_id)
    delta_ledger, evidence_ledger, _total = _ledgers(cases)
    if not any(battery._package_c_structural_case(case) for case in cases):
        raise AssertionError("package_c pass unexpectedly has no structural-zero case")
    forced = copy.deepcopy(delta_ledger)
    for _case_id, delta in forced:
        delta["model_http"] = 1
    total = {key: sum(delta[key] for _case_id, delta in forced) for key in forced[0][1]}
    assert total["model_http"] == battery.QUESTIONS_PER_PASS
    assert battery._http_probe_reconciliation_exact(cases, forced, evidence_ledger, total) is False
