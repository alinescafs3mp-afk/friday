"""Focused adversarial checks for LAB-272 negative-control repairs.

B09-20: direct negation/quote/contradiction at evaluate_case, not the
predicate alone. Rejected new relation cannot re-enter via legacy
«проверяемого» stems. Marker competitor rejects every malformed
word-continuation. TIME/REMINDER/TELEGRAM cardinality unchanged.
Tests are authored, not executed, in the lab turn.
"""

from __future__ import annotations

from tools import synthetic_live_battery as battery

_COUNTER_NEGATED = (
    "Структурный oracle не независим от тестируемого кода: он проверяет "
    "структуру результата по заранее известным правилам."
)
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
_REPORTED_QUOTE_CORRECTED = (
    "Утверждение «структурный oracle не независим от тестируемого кода» "
    "неверно: он независим от тестируемого кода и проверяет структуру "
    "результата по заранее известным правилам."
)
_REPORTED_QUOTE_ONLY = (
    "Утверждение «структурный oracle независим от тестируемого кода: он "
    "проверяет структуру результата по заранее известным правилам»."
)
_LEGACY_PROVERYAEMOGO = (
    "Структурный oracle не независим от проверяемого кода: он проверяет "
    "структуру результата по заранее известным правилам."
)
_SUBSTANTIVE_LEGACY = (
    "Главное свойство структурного oracle — точность: он однозначно "
    "отличает корректную структуру результата от нарушенной."
)
_EXPECTED_ATTACHMENT = "syn-attachment-0123456789abcdef0123"
_COUNTER_UNDERSCORE = "syn-attachment-0123456789abcdef0123 syn-attachment-0123456789abcdef0123_wrong"


def _b09_20_case() -> battery.ExpandedCase:
    manifest = battery.load_manifest(battery.MANIFEST_PATHS["B"])
    matched = [case for case in battery.expand_manifest_cases(manifest) if case.id == "SYN-B09-20"]
    assert matched, "SYN-B09-20 missing from frozen B manifest"
    return matched[0]


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


def _semantic_missing(message: str) -> bool:
    result = battery.evaluate_case(_b09_20_case(), _record(message), latency_ms=1)
    return "content_semantic_group_missing" in result["failure_codes"]


def test_b09_20_root_negated_independence_fails_at_evaluate_case() -> None:
    assert battery._b09_20_relation_is_exact(_COUNTER_NEGATED) is False
    assert _semantic_missing(_COUNTER_NEGATED) is True


def test_b09_20_justified_paraphrase_still_accepted_at_evaluate_case() -> None:
    assert battery._b09_20_relation_is_exact(_JUSTIFIED) is True
    assert _semantic_missing(_JUSTIFIED) is False


def test_b09_20_scoped_non_copy_explanation_is_accepted() -> None:
    assert battery._b09_20_relation_is_exact(_SCOPED_NON_COPY) is True
    assert _semantic_missing(_SCOPED_NON_COPY) is False


def test_b09_20_reported_quote_of_negation_does_not_block_live_correction() -> None:
    assert battery._b09_20_relation_is_exact(_REPORTED_QUOTE_CORRECTED) is True
    assert _semantic_missing(_REPORTED_QUOTE_CORRECTED) is False


def test_b09_20_reported_quote_only_is_rejected_at_evaluate_case() -> None:
    assert battery._b09_20_relation_is_exact(_REPORTED_QUOTE_ONLY) is False
    assert _semantic_missing(_REPORTED_QUOTE_ONLY) is True


def test_b09_20_legacy_proveryaemogo_cannot_rescue_negated_relation() -> None:
    assert battery._b09_20_relation_is_exact(_LEGACY_PROVERYAEMOGO) is False
    assert _semantic_missing(_LEGACY_PROVERYAEMOGO) is True


def test_b09_20_substantive_legacy_precision_property_accepted_at_evaluate_case() -> None:
    assert battery._b09_20_relation_is_exact(_SUBSTANTIVE_LEGACY) is True
    assert _semantic_missing(_SUBSTANTIVE_LEGACY) is False


def test_b09_20_oracle_does_not_reattach_legacy_semantic_groups() -> None:
    oracle = battery.oracle_for_case(_b09_20_case())
    assert oracle["content"]["semantic_profile"] == "b09_20"
    assert oracle["content"]["semantic_groups"] == []


def test_marker_competitor_detects_underscore_junk_beside_valid_token() -> None:
    folded = _COUNTER_UNDERSCORE.casefold()
    assert battery._opaque_attachment_has_competitor(folded, _EXPECTED_ATTACHMENT) is True
    assert (
        battery._closed_marker_exact(
            _COUNTER_UNDERSCORE, _EXPECTED_ATTACHMENT, kind="ATTACHMENT", exact_once=False
        )
        is False
    )


def test_marker_competitor_accepts_exact_token_and_rejects_truncation_foreign() -> None:
    expected = _EXPECTED_ATTACHMENT
    assert battery._opaque_attachment_has_competitor(expected, expected) is False
    assert battery._closed_marker_exact(expected, expected, kind="ATTACHMENT", exact_once=False) is True
    truncated = f"{expected} syn-attachment-0123456789abcdef012"
    foreign = f"{expected} syn-attachment-fffffffffffffffff000"
    glued = f"{expected}xx"
    assert battery._opaque_attachment_has_competitor(truncated, expected) is True
    assert battery._opaque_attachment_has_competitor(foreign, expected) is True
    assert battery._opaque_attachment_has_competitor(glued, expected) is True
    assert battery._closed_marker_exact(truncated, expected, kind="ATTACHMENT", exact_once=False) is False
    assert battery._closed_marker_exact(foreign, expected, kind="ATTACHMENT", exact_once=False) is False
    assert battery._closed_marker_exact(glued, expected, kind="ATTACHMENT", exact_once=False) is False


def test_time_reminder_telegram_cardinality_unchanged() -> None:
    time_marker = "SYN-TIME-aaaaaaaaaaaaaaaaaaaa"
    reminder = "SYN-REMINDER-B08-01"
    telegram = "SYN-TELEGRAM-B10-16"
    assert battery._closed_marker_exact(f"{time_marker} {time_marker}", time_marker, kind="TIME") is False
    assert battery._closed_marker_exact(f"{reminder} {reminder}", reminder, kind="REMINDER") is False
    assert battery._closed_marker_exact(f"{telegram} {telegram}", telegram, kind="TELEGRAM") is False
    assert battery._closed_marker_exact(f"{telegram} передан без ошибок.", telegram, kind="TELEGRAM") is True
