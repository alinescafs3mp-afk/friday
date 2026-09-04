import pytest

from friday.orchestration.coding_result_archive_manifest import build_coding_result_archive_manifest
from friday.orchestration.coding_result_archive_pack_admission import (
    build_coding_result_archive_pack_admission,
)
from friday.orchestration.coding_result_archive_plan import build_coding_result_archive_plan
from friday.orchestration.coding_result_restart_admission import (
    CodingResultRestartAdmissionReason,
    CodingResultRestartAdmissionState,
    build_coding_result_restart_admission,
    validate_coding_result_restart_admission,
)

SHA256 = "a" * 64


def _pack(*, turn: str = "turn-1", blocked: bool = False) -> object:
    plan = build_coding_result_archive_plan(
        "plan-1", turn, files=["a.py", "b.py"] if not blocked else ["../escape.py"]
    )
    manifest = build_coding_result_archive_manifest("manifest-1", turn, {"a.py": SHA256, "b.py": SHA256})
    return build_coding_result_archive_pack_admission("pack-1", turn, plan, manifest)


def test_no_restart_facts_are_empty() -> None:
    result = build_coding_result_restart_admission("restart-1", "turn-1")
    assert result.admission is CodingResultRestartAdmissionState.EMPTY


def test_exact_operation_and_admitted_pack_are_restartable() -> None:
    result = build_coding_result_restart_admission(
        "restart-1", "turn-1", "operation-1", _pack(), revision_selector="revision-1"
    )

    assert result.admission is CodingResultRestartAdmissionState.ADMITTED
    assert result.operation_id == "operation-1"
    assert result.pack_id == "pack-1"
    assert result.revision_selector == "revision-1"


@pytest.mark.parametrize("selector", ("latest", "HEAD", "newest", "current"))
def test_recency_selectors_fail_closed(selector: str) -> None:
    result = build_coding_result_restart_admission(
        "restart-1", "turn-1", "operation-1", _pack(), revision_selector=selector
    )

    assert result.admission is CodingResultRestartAdmissionState.BLOCKED
    assert result.reason is CodingResultRestartAdmissionReason.RECENCY_SELECTOR
    assert result.operation_id is None


def test_blocked_pack_cannot_be_restarted() -> None:
    result = build_coding_result_restart_admission("restart-1", "turn-1", "operation-1", _pack(blocked=True))

    assert result.admission is CodingResultRestartAdmissionState.BLOCKED
    assert result.reason is CodingResultRestartAdmissionReason.PACK_BLOCKED


def test_missing_pack_and_missing_operation_are_blocked() -> None:
    missing_pack = build_coding_result_restart_admission("restart-1", "turn-1", "operation-1")
    missing_operation = build_coding_result_restart_admission("restart-1", "turn-1", None, _pack())

    assert missing_pack.reason is CodingResultRestartAdmissionReason.MISSING_PACK
    assert missing_operation.reason is CodingResultRestartAdmissionReason.INVALID_OPERATION_ID


def test_turn_mismatch_is_blocked() -> None:
    result = build_coding_result_restart_admission(
        "restart-1", "turn-1", "operation-1", _pack(turn="turn-other")
    )
    assert result.reason is CodingResultRestartAdmissionReason.IDENTITY_MISMATCH


def test_mapping_roundtrip_and_invalid_serialization() -> None:
    result = build_coding_result_restart_admission("restart-1", "turn-1", "operation-1", _pack())
    encoded = result.to_mapping()

    assert validate_coding_result_restart_admission(encoded) is True
    assert build_coding_result_restart_admission(encoded) == result
    assert validate_coding_result_restart_admission({**encoded, "extra": 1}) is False
