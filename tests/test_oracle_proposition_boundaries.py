"""An oracle must preserve proposition ownership and complete marker identity."""

from __future__ import annotations

import pytest

from tools import synthetic_live_battery as battery

_AFFIRMATIVE = (
    "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
    "данных, оставшихся от предыдущих запусков."
)


def _semantic_missing(package, case_id, message):
    manifest = battery.load_manifest(battery.MANIFEST_PATHS[package])
    case = next(c for c in battery.expand_manifest_cases(manifest) if c.id == case_id)
    record = {
        "status_code": 200,
        "response": {"conversation_id": "conv", "message": message, "message_id": "mid", "tools_used": []},
        "state": {},
        "privacy_canaries": [],
    }
    result = battery.evaluate_case(case, record, latency_ms=1)
    return "content_semantic_group_missing" in result["failure_codes"]


@pytest.mark.parametrize(
    "message,expected",
    [
        (
            "Главное свойство структурного oracle — точность: он однозначно отличает корректную структуру результата от нарушенной.",
            True,
        ),
        ("Структурный oracle является точным: он однозначно различает корректные результаты.", True),
        ("Точность — главное свойство структурного oracle.", True),
        ("Детерминированность — основное свойство структурного оракула.", True),
        (
            "Главное свойство структурного oracle — независимость; точность часов относится к другой теме.",
            False,
        ),
        (
            "Главное свойство структурного oracle — независимость, а точность часов относится к другой теме.",
            False,
        ),
        (
            "Главное свойство структурного oracle — независимость и точность часов относится к другой теме.",
            False,
        ),
        (
            "Точность часов относится к другой теме; главное свойство структурного oracle — независимость.",
            False,
        ),
        ("Структурный oracle упомянут, а свойством часов является точность.", False),
        ("Главное свойство структурного oracle — не точность, а независимость.", False),
    ],
)
def test_precision_is_the_oracles_property_not_a_nearby_subject(message, expected):
    assert battery._b09_20_relation_is_exact(message) is expected
    assert _semantic_missing("B", "SYN-B09-20", message) is not expected


@pytest.mark.parametrize(
    "tail,expected",
    [
        (
            "Данные прошлого запуска не искажают результат, но данные прошлого запуска искажают результат.",
            False,
        ),
        (
            "Данные прошлого запуска не искажают результат и данные прошлого запуска искажают результат.",
            False,
        ),
        ("Если бы данные прошлого запуска искажали результат, следующий проход не был бы изолирован.", True),
        ("Если бы данные прошлого запуска искажали результат, это было бы плохо.", True),
        (
            "Если бы данные прошлого запуска искажали результат, это было бы плохо, и данные прошлого запуска искажают результат.",
            False,
        ),
        (
            "Если бы данные прошлого запуска искажали результат, это было бы плохо; данные прошлого запуска искажают результат.",
            False,
        ),
        (
            "Если бы данные прошлого запуска искажали результат, это было бы плохо и данные прошлого запуска искажают результат.",
            False,
        ),
        (
            "Данные прошлого запуска искажают результат. Если бы это не происходило, результаты были бы верными.",
            False,
        ),
        ("Если бы данные прошлого запуска искажали результат, следующий проход не изолирован.", False),
    ],
)
def test_counterfactual_explanation_cannot_erase_a_factual_contamination_tail(tail, expected):
    message = _AFFIRMATIVE + " " + tail
    assert battery._a09_14_relation_is_exact(message) is expected
    assert _semantic_missing("A", "SYN-A09-14", message) is not expected


@pytest.mark.parametrize("suffix", ["--wrong", "-", "--", "_-wrong", "___", "-__wrong"])
def test_repeated_attachment_marker_with_broken_separator_is_not_exact(suffix):
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["B"])
    case = next(c for c in battery.expand_manifest_cases(manifest) if c.id == "SYN-B07-10")
    marker = battery._marker(case, "ATTACHMENT")
    message = f"{marker} {marker}{suffix}"
    assert not battery._closed_marker_exact(message, marker, kind="ATTACHMENT", exact_once=False)
    record = {
        "status_code": 200,
        "response": {"conversation_id": "conv", "message": message, "message_id": "mid", "tools_used": []},
        "state": {},
        "privacy_canaries": [],
    }
    result = battery.evaluate_case(case, record, latency_ms=1)
    assert "content_attachment_marker_not_exact" in result["failure_codes"]


@pytest.mark.parametrize("separator", [", ", "; ", " — "])
def test_complete_marker_repeat_still_accepts_separate_punctuation(separator):
    marker = "SYN-ATTACHMENT-0123456789abcdef0123"
    assert battery._closed_marker_exact(
        marker + separator + marker, marker, kind="ATTACHMENT", exact_once=False
    )
