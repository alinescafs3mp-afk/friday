"""Focused adversarial checks for LAB-268 harness repairs.

HTTP census: Lab268 predicate six remain structural-zero. Lab270 extends
the mixed census with independently proved nominal count_records zeros.
A09-14: leftover previous-run data is a bounded isolation synonym.
These tests are authored, not executed, in the lab turn.
"""

from __future__ import annotations

import copy

import pytest

from tools import synthetic_live_battery as battery

_STRUCTURAL_B03_IDS = {
    "SYN-B03-08",
    "SYN-B03-09",
    "SYN-B03-11",
    "SYN-B03-12",
    "SYN-B03-16",
    "SYN-B03-17",
}


def _pass_cases(profile: str, *, battery_id: str) -> list[battery.ExpandedCase]:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS[battery_id])
    return [case for case in battery.expand_manifest_cases(manifest) if case.oracle_profile == profile]


def _closed_case_delta(case: battery.ExpandedCase) -> dict[str, int]:
    model_owned = (
        case.oracle_profile not in {"tenant_privacy", "reminder_creation"}
        and not battery._package_a_code_owned_case(case)
        and not battery._package_a_code_owned_temporal_case(case)
        and not battery._package_c_structural_case(case)
    )
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


def test_b_p03_predicate_six_remain_structural_zero() -> None:
    cases = _pass_cases("package_c_exact_documents", battery_id="B")
    assert len(cases) == battery.QUESTIONS_PER_PASS
    observed = {case.id for case in cases if battery._package_c_structural_case(case)}
    assert observed >= _STRUCTURAL_B03_IDS
    assert _STRUCTURAL_B03_IDS == battery._PACKAGE_C_PREDICATE_ZERO_IDS


def test_b_p03_http_census_requires_zero_on_structural_and_send_on_others() -> None:
    cases = _pass_cases("package_c_exact_documents", battery_id="B")
    delta_ledger, evidence_ledger, total = _ledgers(cases)
    expected_model = sum(1 for case in cases if not battery._package_c_structural_case(case))
    assert total["model_http"] == expected_model
    assert expected_model == 7
    assert battery._http_probe_reconciliation_exact(cases, delta_ledger, evidence_ledger, total) is True


def test_b_p03_rejects_structural_model_http_one() -> None:
    cases = _pass_cases("package_c_exact_documents", battery_id="B")
    delta_ledger, evidence_ledger, _total = _ledgers(cases)
    index = next(i for i, case in enumerate(cases) if case.id == "SYN-B03-08")
    forged = copy.deepcopy(delta_ledger)
    forged[index][1]["model_http"] = 1
    total = {key: sum(delta[key] for _case_id, delta in forged) for key in forged[0][1]}
    assert battery._http_probe_reconciliation_exact(cases, forged, evidence_ledger, total) is False


def test_b_p03_rejects_nonstructural_model_http_zero() -> None:
    cases = _pass_cases("package_c_exact_documents", battery_id="B")
    delta_ledger, evidence_ledger, _total = _ledgers(cases)
    index = next(i for i, case in enumerate(cases) if case.id == "SYN-B03-01")
    forged = copy.deepcopy(delta_ledger)
    forged[index][1]["model_http"] = 0
    total = {key: sum(delta[key] for _case_id, delta in forged) for key in forged[0][1]}
    assert battery._http_probe_reconciliation_exact(cases, forged, evidence_ledger, total) is False


@pytest.mark.parametrize("counter", ["http_foreign_model", "other_http"])
def test_b_p03_rejects_invalid_privacy_or_other_http_counters(counter: str) -> None:
    cases = _pass_cases("package_c_exact_documents", battery_id="B")
    delta_ledger, evidence_ledger, _total = _ledgers(cases)
    forged = copy.deepcopy(delta_ledger)
    forged[0][1][counter] = 1
    total = {key: sum(delta[key] for _case_id, delta in forged) for key in forged[0][1]}
    assert battery._http_probe_reconciliation_exact(cases, forged, evidence_ledger, total) is False


def test_a_p03_mixed_census_rejects_model_owned_http_zero() -> None:
    cases = _pass_cases("package_c_exact_documents", battery_id="A")
    delta_ledger, evidence_ledger, total = _ledgers(cases)
    expected_model = sum(1 for case in cases if not battery._package_c_structural_case(case))
    assert total["model_http"] == expected_model
    assert expected_model == 14
    assert battery._http_probe_reconciliation_exact(cases, delta_ledger, evidence_ledger, total) is True
    index = next(i for i, case in enumerate(cases) if not battery._package_c_structural_case(case))
    forged = copy.deepcopy(delta_ledger)
    forged[index][1]["model_http"] = 0
    forged_total = {key: sum(delta[key] for _case_id, delta in forged) for key in forged[0][1]}
    assert battery._http_probe_reconciliation_exact(cases, forged, evidence_ledger, forged_total) is False


def test_b_p03_does_not_waive_pass_wide_when_all_model_http_zero() -> None:
    cases = _pass_cases("package_c_exact_documents", battery_id="B")
    delta_ledger, evidence_ledger, _total = _ledgers(cases)
    waived = copy.deepcopy(delta_ledger)
    for _case_id, delta in waived:
        delta["model_http"] = 0
    total = {key: sum(delta[key] for _case_id, delta in waived) for key in waived[0][1]}
    assert battery._http_probe_reconciliation_exact(cases, waived, evidence_ledger, total) is False


@pytest.mark.parametrize(
    "message",
    [
        (
            "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
            "данных, оставшихся от предыдущих запусков."
        ),
        ("Каждый тестовый проход получает новую базу, чтобы исключить влияние данных предыдущих запусков."),
        (
            "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
            "предыдущих тестов на текущие результаты. Это гарантирует, что тесты "
            "независимы друг от друга: данные, созданные или изменённые в одном "
            "тесте, не «просачиваются» в другой. Благодаря этому ошибки легче "
            "локализовать, а результаты тестирования становятся воспроизводимыми "
            "и предсказуемыми."
        ),
    ],
)
def test_a09_14_accepts_leftover_previous_run_data_isolation_synonyms(message: str) -> None:
    assert battery._a09_14_relation_is_exact(message) is True


@pytest.mark.parametrize(
    "message",
    [
        (
            "Каждый тестовый проход получает старую базу, чтобы исключить влияние "
            "данных, оставшихся от предыдущих запусков."
        ),
        (
            "Каждый тестовый проход получает новую базу, чтобы не исключить влияние "
            "данных, оставшихся от предыдущих запусков."
        ),
        (
            "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
            "данных, оставшихся от предыдущих запусков. Остатки продолжают влиять "
            "на следующий тест."
        ),
        (
            "Каждый тестовый проход может получать новую базу, чтобы исключить "
            "влияние данных, оставшихся от предыдущих запусков."
        ),
        (
            "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
            "текущих результатов на предыдущие тесты."
        ),
        (
            "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
            "данных, оставшихся от предыдущих отчётов."
        ),
        (
            "Каждый тестовый проход выполняется, а сервер получает новую базу, "
            "чтобы исключить влияние данных, оставшихся от предыдущих запусков."
        ),
        "Каждый тестовый проход получает новую базу, чтобы ускорить доставку курьера.",
        (
            "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
            "данных, оставшихся от предыдущих запусков. Данные прошлого запуска "
            "искажают результат."
        ),
    ],
)
def test_a09_14_rejects_contamination_negation_hedge_and_unrelated_claims(message: str) -> None:
    assert battery._a09_14_relation_is_exact(message) is False
