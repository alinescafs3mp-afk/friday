"""Predicate and evaluate_case regressions for R211-01 through R211-05.

Authored against the private LAB-275 harness copy. Tests are not executed
in the lab turn. Portable import: from tools import synthetic_live_battery.
"""

from __future__ import annotations

from tools import synthetic_live_battery as battery

_JUSTIFIED = (
    "Главное свойство хорошего структурного oracle — независимость от "
    "тестируемого кода: проверка сверяет структуру результата по заранее "
    "известным правилам."
)
_SCOPED_NON_COPY = (
    "Главное свойство структурного oracle — независимость от тестируемого "
    "кода: он не копирует логику системы и проверяет структуру результата "
    "по заранее известным правилам."
)
_SUBSTANTIVE_LEGACY = (
    "Главное свойство структурного oracle — точность: он однозначно "
    "отличает корректную структуру результата от нарушенной."
)
_FAKE_CANNED = "oracle точн помогает сделать локальную проверку надёжной."
_B09_LEGACY_NEGATED = (
    "Главное свойство структурного oracle — не точность и не детерминированность; "
    "эти свойства не нужны для проверки."
)
_B09_LEGACY_QUOTE = (
    "В отчёте написано: «Главное свойство хорошего структурного oracle — точность проверки результата»."
)
_B09_LEGACY_UNRELATED = (
    "Структурный oracle упомянут в заметке. Точный прогноз погоды относится "
    "к другой теме и никак не описывает эту проверку."
)
_B09_LEGACY_QUESTION = (
    "Является ли главное свойство структурного oracle точностью и детерминированностью проверки?"
)
_B09_NEW_QUOTE_FRAME = (
    "Утверждение «Главное свойство хорошего структурного oracle — независимость "
    "от тестируемого кода: он проверяет структуру результата по заранее "
    "известным правилам.»."
)
_B09_NEW_CONTRADICTION_NEXT = (
    "Главное свойство хорошего структурного oracle — независимость от "
    "тестируемого кода: он проверяет структуру результата по заранее "
    "известным правилам. При этом он полностью копирует код системы и "
    "зависит от её реализации."
)
_B09_NEW_PROPERTY_ADVISORY = (
    "Главное свойство структурного oracle следует сделать независимым от "
    "тестируемого кода и проверять структуру результата по заранее известным "
    "правилам."
)
_B09_CORRECTED_QUOTE = (
    "Утверждение «структурный oracle не независим от тестируемого кода» "
    "неверно: он независим от тестируемого кода и проверяет структуру "
    "результата по заранее известным правилам."
)
_B09_REPORTED_QUOTE_ONLY = (
    "Утверждение «структурный oracle независим от тестируемого кода: он "
    "проверяет структуру результата по заранее известным правилам»."
)
_A09_AFFIRMATIVE = (
    "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
    "данных, оставшихся от предыдущих запусков."
)
_A09_PRIOR_DISTORTION = (
    "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
    "данных, оставшихся от предыдущих запусков. Данные прошлого запуска "
    "искажают результат."
)
_A09_COUNTERFACTUAL = (
    "Каждый тестовый проход получает новую базу, чтобы исключить влияние "
    "данных, оставшихся от предыдущих запусков. Если бы данные прошлого "
    "запуска искажали результат, следующий проход не был бы изолирован."
)
_EXPECTED_ATTACHMENT = "syn-attachment-0123456789abcdef0123"
_B07_SUFFIX_HYPHEN = "SYN-ATTACHMENT-0123456789abcdef0123 SYN-ATTACHMENT-0123456789abcdef0123-wrong"
_B07_MISSING_HYPHEN = "SYN-ATTACHMENT-0123456789abcdef0123 SYN-ATTACHMENT0123456789abcdef0123"


def _record(message: str) -> dict:
    return {
        "status_code": 200,
        "response": {
            "conversation_id": "conv",
            "message": message,
            "message_id": "mid",
            "tools_used": [],
        },
        "state": {},
        "privacy_canaries": [],
    }


def _case(package: str, case_id: str) -> battery.ExpandedCase:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS[package])
    matched = [case for case in battery.expand_manifest_cases(manifest) if case.id == case_id]
    assert matched, f"{case_id} missing from frozen {package} manifest"
    return matched[0]


def _semantic_missing(package: str, case_id: str, message: str) -> bool:
    result = battery.evaluate_case(_case(package, case_id), _record(message), latency_ms=1)
    return "content_semantic_group_missing" in result["failure_codes"]


def _attachment_not_exact(message: str) -> bool:
    result = battery.evaluate_case(_case("B", "SYN-B07-10"), _record(message), latency_ms=1)
    return "content_attachment_marker_not_exact" in result["failure_codes"]


def test_r211_01_legacy_property_is_substantive_and_oracle_owned() -> None:
    assert battery._b09_20_relation_is_exact(_SUBSTANTIVE_LEGACY) is True
    assert battery._b09_20_relation_is_exact(_FAKE_CANNED) is False
    assert battery._b09_20_relation_is_exact(_B09_LEGACY_NEGATED) is False
    assert battery._b09_20_relation_is_exact(_B09_LEGACY_QUOTE) is False
    assert battery._b09_20_relation_is_exact(_B09_LEGACY_UNRELATED) is False
    assert battery._b09_20_relation_is_exact(_B09_LEGACY_QUESTION) is False
    assert _semantic_missing("B", "SYN-B09-20", _SUBSTANTIVE_LEGACY) is False
    assert _semantic_missing("B", "SYN-B09-20", _FAKE_CANNED) is True
    assert _semantic_missing("B", "SYN-B09-20", _B09_LEGACY_NEGATED) is True
    assert _semantic_missing("B", "SYN-B09-20", _B09_LEGACY_QUOTE) is True
    assert _semantic_missing("B", "SYN-B09-20", _B09_LEGACY_UNRELATED) is True
    assert _semantic_missing("B", "SYN-B09-20", _B09_LEGACY_QUESTION) is True


def test_r211_02_quote_contradiction_and_advisory_rejected() -> None:
    assert battery._b09_20_relation_is_exact(_JUSTIFIED) is True
    assert battery._b09_20_relation_is_exact(_B09_NEW_QUOTE_FRAME) is False
    assert battery._b09_20_relation_is_exact(_B09_NEW_CONTRADICTION_NEXT) is False
    assert battery._b09_20_relation_is_exact(_B09_NEW_PROPERTY_ADVISORY) is False
    assert _semantic_missing("B", "SYN-B09-20", _JUSTIFIED) is False
    assert _semantic_missing("B", "SYN-B09-20", _B09_NEW_QUOTE_FRAME) is True
    assert _semantic_missing("B", "SYN-B09-20", _B09_NEW_CONTRADICTION_NEXT) is True
    assert _semantic_missing("B", "SYN-B09-20", _B09_NEW_PROPERTY_ADVISORY) is True


def test_r211_03_corrected_quote_accepted_reported_only_rejected() -> None:
    assert battery._b09_20_relation_is_exact(_B09_CORRECTED_QUOTE) is True
    assert battery._b09_20_relation_is_exact(_B09_REPORTED_QUOTE_ONLY) is False
    assert battery._b09_20_relation_is_exact(_SCOPED_NON_COPY) is True
    assert _semantic_missing("B", "SYN-B09-20", _B09_CORRECTED_QUOTE) is False
    assert _semantic_missing("B", "SYN-B09-20", _B09_REPORTED_QUOTE_ONLY) is True
    assert _semantic_missing("B", "SYN-B09-20", _SCOPED_NON_COPY) is False


def test_r211_04_leftover_runs_before_every_successful_a09_path() -> None:
    assert battery._a09_14_relation_is_exact(_A09_AFFIRMATIVE) is True
    assert battery._a09_14_relation_is_exact(_A09_PRIOR_DISTORTION) is False
    assert battery._a09_14_leftover_still_influences(_A09_PRIOR_DISTORTION.casefold()) is True
    assert battery._a09_14_relation_is_exact(_A09_COUNTERFACTUAL) is True
    assert _semantic_missing("A", "SYN-A09-14", _A09_AFFIRMATIVE) is False
    assert _semantic_missing("A", "SYN-A09-14", _A09_PRIOR_DISTORTION) is True
    assert _semantic_missing("A", "SYN-A09-14", _A09_COUNTERFACTUAL) is False


def test_r211_05_malformed_attachment_competitors_rejected() -> None:
    expected = _EXPECTED_ATTACHMENT
    assert battery._opaque_attachment_has_competitor(_B07_SUFFIX_HYPHEN.casefold(), expected) is True
    assert battery._opaque_attachment_has_competitor(_B07_MISSING_HYPHEN.casefold(), expected) is True
    assert (
        battery._closed_marker_exact(_B07_SUFFIX_HYPHEN, expected, kind="ATTACHMENT", exact_once=False)
        is False
    )
    assert (
        battery._closed_marker_exact(_B07_MISSING_HYPHEN, expected, kind="ATTACHMENT", exact_once=False)
        is False
    )
    assert battery._closed_marker_exact(expected, expected, kind="ATTACHMENT", exact_once=False) is True
    repeated = f"{expected} {expected}"
    assert battery._closed_marker_exact(repeated, expected, kind="ATTACHMENT", exact_once=False) is True
    assert battery._closed_marker_exact(repeated, expected, kind="ATTACHMENT", exact_once=True) is False
    case = _case("B", "SYN-B07-10")
    marker = battery._marker(case, "ATTACHMENT")
    assert _attachment_not_exact(f"{marker} {marker}-wrong") is True
    nonce = marker.rsplit("-", 1)[-1]
    assert _attachment_not_exact(f"{marker} SYN-ATTACHMENT{nonce}") is True
    assert _attachment_not_exact(f"{marker} {marker}") is False
