"""Offline contract for the two model-owned A-P09 reproducibility prompts."""

from __future__ import annotations

import copy
import json
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import synthetic_live_battery as battery  # noqa: E402


@pytest.fixture(autouse=True)
def _deny_ip_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep oracle tests pure while permitting harmless local AF_UNIX use."""

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(selected: socket.socket, address: Any) -> Any:
        if selected.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("network access is forbidden in the P09 prompt contract")
        return original_connect(selected, address)

    def guarded_connect_ex(selected: socket.socket, address: Any) -> int:
        if selected.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError("network access is forbidden in the P09 prompt contract")
        return original_connect_ex(selected, address)

    def guarded_create_connection(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("network access is forbidden in the P09 prompt contract")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)


EXPECTED_A09_08 = (
    "Коротко объясни, какие зависимости и параметры окружения нужно контролировать, "
    "чтобы повторный запуск теста давал тот же результат, и почему. Контроль SYN-A09-08."
)
EXPECTED_A09_10 = (
    "Одним абзацем объясни, почему при фиксированном seed повторный запуск получает ту же "
    "псевдослучайную последовательность и как это помогает воспроизводить и отлаживать сбои. "
    "Контроль SYN-A09-10."
)

_CANONICAL_AND_BOUNDED_PARAPHRASES = {
    8: (
        "Для воспроизводимого тестирования зафиксируйте все зависимости версии библиотек, "
        "окружение и используйте детерминированные входные данные, чтобы исключить влияние "
        "случайных факторов.",
        "Всегда фиксируйте версии зависимостей и окружение, чтобы повторный прогон выдавал "
        "одинаковый результат.",
        "Зафиксируйте входные параметры и окружение, чтобы при повторном запуске тест возвращал "
        "тот же результат.",
    ),
    10: (
        "Зафиксированный seed полезен тем, что он делает случайные процессы "
        "детерминированными: при каждом запуске с одинаковым начальным значением генератора "
        "случайных чисел получается одна и та же последовательность. Это гарантирует точно "
        "воспроизводить результаты тестирования, легко отлаживать ошибки, связанные со "
        "случайностью, и сравнивать производительность или поведение системы в разных условиях "
        "без «шума» от случайных изменений.",
        "Фиксированный seed гарантирует, что генератор будет выдавать одну и ту же "
        "последовательность при каждом запуске.",
        "Зафиксированный seed обеспечивает, что генератор выдаёт ту же последовательность, упрощая отладку.",
    ),
}

_SEMANTIC_NEGATIVES = {
    8: {
        "negated": "Зафиксируйте seed, но каждый запуск даёт не тот же результат.",
        "quoted": "Фраза: «Зафиксируйте seed, чтобы каждый запуск давал тот же результат».",
        "meta": ("По словам отчёта, зафиксируйте seed, чтобы каждый запуск давал тот же результат."),
        "unrelated_owner": "Зафиксируйте seed, а сервер завершает запуск тем же результатом.",
    },
    10: {
        "negated": (
            "Фиксированный seed гарантирует, что генератор не будет выдавать одну и ту же "
            "последовательность при каждом запуске."
        ),
        "quoted": (
            "Фраза: «Фиксированный seed гарантирует, что генератор будет выдавать одну и ту же "
            "последовательность при каждом запуске»."
        ),
        "meta": (
            "Согласно отчёту, фиксированный seed гарантирует, что генератор будет выдавать одну "
            "и ту же последовательность при каждом запуске."
        ),
        "unrelated_owner": (
            "Фиксированный seed гарантирует, а оркестратор заставляет генератор выдавать одну "
            "и ту же последовательность."
        ),
    },
}


def _manifest() -> dict[str, Any]:
    return battery.load_manifest(ROOT / "tests/fixtures/synthetic_live_battery_a.json")


def _p09_cases() -> dict[int, battery.ExpandedCase]:
    return {
        case.question_index: case
        for case in battery.expand_manifest_cases(_manifest())
        if case.pass_id == "A-P09"
    }


def _closed_record(case: battery.ExpandedCase, message: str) -> dict[str, Any]:
    oracle = battery.oracle_for_case(case)
    state = copy.deepcopy(oracle["state"]["equals"])
    state.update({key: 0 for key in oracle["state"]["max"]})
    state.update(copy.deepcopy(oracle["state"]["min"]))
    response = {
        "conversation_id": f"conversation-{case.id}",
        "message": message,
        "message_id": f"message-{case.id}",
        "tools_used": [],
    }
    return {
        "status_code": 200,
        "response": response,
        "raw_response": json.dumps(response, ensure_ascii=False),
        "state": state,
        "privacy_canaries": ["SYN-PRIVATE-DO-NOT-LEAK"],
    }


def test_manifest_frozen_raw_and_content_hashes_are_current() -> None:
    manifest_path = ROOT / "tests/fixtures/synthetic_live_battery_a.json"
    manifest = _manifest()
    observed = (
        battery.file_sha256(manifest_path),
        battery._sha256_bytes(battery._canonical_json_bytes(manifest)),
    )
    expected = (
        battery.FROZEN_MANIFEST_SHA256["A"],
        battery.FROZEN_MANIFEST_CONTENT_SHA256["A"],
    )

    assert expected == observed


def test_manifest_keeps_all_ids_and_a_p09_model_owned_contract_in_order() -> None:
    manifest = _manifest()
    assert battery.manifest_complaints(manifest, expected_battery="A") == []
    frozen_audit = battery.audit_frozen_manifests()
    assert frozen_audit["valid"] is True
    assert frozen_audit["complaints"] == []

    cases = battery.expand_manifest_cases(manifest)
    expected_ids = [
        f"SYN-A{pass_index:02d}-{question_index:02d}"
        for pass_index in range(1, 11)
        for question_index in range(1, 21)
    ]
    assert len(cases) == 200
    assert [case.id for case in cases] == expected_ids

    p09 = [case for case in cases if case.pass_id == "A-P09"]
    assert len(p09) == 20
    assert [case.id for case in p09] == expected_ids[160:180]
    assert p09[7].question == EXPECTED_A09_08
    assert p09[9].question == EXPECTED_A09_10

    for case in (p09[7], p09[9]):
        oracle = battery.oracle_for_case(case)
        assert battery._package_a_code_owned_case(case) is False
        assert oracle["state"]["equals"]["tools_enabled"] is False
        assert oracle["state"]["equals"]["expected_tool"] == ""
        assert oracle["state"]["min"]["model_http_attempts"] == 1


@pytest.mark.parametrize(
    ("question_index", "message"),
    [
        (question_index, message)
        for question_index, messages in _CANONICAL_AND_BOUNDED_PARAPHRASES.items()
        for message in messages
    ],
)
def test_canonical_and_bounded_paraphrases_pass_the_full_oracle(
    question_index: int,
    message: str,
) -> None:
    case = _p09_cases()[question_index]

    result = battery.evaluate_case(case, _closed_record(case, message), latency_ms=1)

    assert result["passed"] is True
    assert result["failure_codes"] == []


@pytest.mark.parametrize(
    ("question_index", "negative_kind", "message"),
    [
        (question_index, negative_kind, message)
        for question_index, negatives in _SEMANTIC_NEGATIVES.items()
        for negative_kind, message in negatives.items()
    ],
)
def test_negated_quoted_meta_and_unrelated_owner_surfaces_are_exact_semantic_red(
    question_index: int,
    negative_kind: str,
    message: str,
) -> None:
    del negative_kind
    case = _p09_cases()[question_index]

    result = battery.evaluate_case(case, _closed_record(case, message), latency_ms=1)

    assert result["passed"] is False
    assert result["failure_codes"] == ["content_semantic_group_missing"]
