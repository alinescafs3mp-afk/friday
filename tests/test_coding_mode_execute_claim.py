from __future__ import annotations

from friday.orchestration.coding_mode_execute_claim import (
    CodingModeExecuteClaimReason,
    CodingModeExecuteClaimState,
    build_coding_mode_execute_claim,
)
from friday.orchestration.coding_mode_intent import build_coding_mode_intent


def test_no_facts_are_empty_and_inspect_is_static_without_worker() -> None:
    assert build_coding_mode_execute_claim("claim-1", "turn-1").state is CodingModeExecuteClaimState.EMPTY
    intent = build_coding_mode_intent("intent-1", "turn-1", inspect=True)
    result = build_coding_mode_execute_claim("claim-1", "turn-1", intent)
    assert result.state is CodingModeExecuteClaimState.STATIC
    assert result.worker_admission_id is None


def test_untrusted_upload_build_without_admitted_worker_is_blocked() -> None:
    intent = build_coding_mode_intent("intent-1", "turn-1", upload={"name": "source.zip"})
    result = build_coding_mode_execute_claim("claim-1", "turn-1", intent, operation="build")
    assert result.state is CodingModeExecuteClaimState.BLOCKED
    assert result.reason is CodingModeExecuteClaimReason.WORKER_REQUIRED


def test_blocked_intent_and_invalid_facts_fail_closed() -> None:
    intent = build_coding_mode_intent("intent-1", "turn-1", prompt="x", inspect=True)
    blocked = build_coding_mode_execute_claim("claim-1", "turn-1", intent)
    assert blocked.state is CodingModeExecuteClaimState.BLOCKED
    invalid = build_coding_mode_execute_claim("claim-1", "turn-1", intent={"unknown": True})
    assert invalid.state is CodingModeExecuteClaimState.BLOCKED


def test_mapping_roundtrip() -> None:
    intent = build_coding_mode_intent("intent-1", "turn-1", inspect=True)
    result = build_coding_mode_execute_claim("claim-1", "turn-1", intent)
    assert build_coding_mode_execute_claim(result.to_mapping()) == result
