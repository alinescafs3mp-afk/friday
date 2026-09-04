from friday.orchestration.coding_result_archive_manifest import build_coding_result_archive_manifest
from friday.orchestration.coding_result_archive_pack_admission import (
    build_coding_result_archive_pack_admission,
)
from friday.orchestration.coding_result_archive_plan import build_coding_result_archive_plan
from friday.orchestration.coding_result_restart_admission import build_coding_result_restart_admission
from friday.orchestration.coding_result_rollback_admission import build_coding_result_rollback_admission
from friday.orchestration.coding_result_uncertainty import (
    CODING_RESULT_UNCERTAINTY_SCHEMA,
    CodingResultUncertaintyReason,
    CodingResultUncertaintyState,
    build_coding_result_uncertainty,
    validate_coding_result_uncertainty,
)

SHA256 = "a" * 64


def _archive_plan() -> object:
    return build_coding_result_archive_plan("plan-1", "turn-1", files=["a.py", "b.py"])


def _pack() -> object:
    return build_coding_result_archive_pack_admission(
        "pack-1",
        "turn-1",
        _archive_plan(),
        build_coding_result_archive_manifest("manifest-1", "turn-1", {"a.py": SHA256, "b.py": SHA256}),
    )


def test_no_facts_are_empty() -> None:
    result = build_coding_result_uncertainty("uncertainty-1", "turn-1")
    assert result.uncertainty is CodingResultUncertaintyState.EMPTY


def test_matching_archive_plan_and_pack_are_known() -> None:
    result = build_coding_result_uncertainty("uncertainty-1", "turn-1", _archive_plan(), _pack())

    assert result.uncertainty is CodingResultUncertaintyState.KNOWN
    assert result.reason is CodingResultUncertaintyReason.STABLE_ARCHIVE
    assert result.pack_id == "pack-1"


def test_file_plan_without_pack_is_known() -> None:
    plan = build_coding_result_archive_plan("plan-1", "turn-1", files=["only.py"])
    result = build_coding_result_uncertainty("uncertainty-1", "turn-1", plan)

    assert result.uncertainty is CodingResultUncertaintyState.KNOWN
    assert result.reason is CodingResultUncertaintyReason.STABLE_FILE


def test_archive_pack_without_matching_plan_is_unknown() -> None:
    result = build_coding_result_uncertainty("uncertainty-1", "turn-1", None, _pack())

    assert result.uncertainty is CodingResultUncertaintyState.UNKNOWN
    assert result.reason is CodingResultUncertaintyReason.PACK_WITHOUT_ARCHIVE_PLAN
    assert result.pack_id is None


def test_archive_plan_without_pack_is_unknown() -> None:
    result = build_coding_result_uncertainty("uncertainty-1", "turn-1", _archive_plan())

    assert result.uncertainty is CodingResultUncertaintyState.UNKNOWN
    assert result.reason is CodingResultUncertaintyReason.ARCHIVE_PACK_PLAN_MISMATCH


def test_restart_and_rollback_together_are_unknown() -> None:
    restart = build_coding_result_restart_admission("restart-1", "turn-1", "operation-1", _pack())
    rollback = build_coding_result_rollback_admission("rollback-1", "turn-1", "operation-1", "revision-1")
    result = build_coding_result_uncertainty(
        "uncertainty-1", "turn-1", _archive_plan(), _pack(), restart, rollback
    )

    assert result.uncertainty is CodingResultUncertaintyState.UNKNOWN
    assert result.reason is CodingResultUncertaintyReason.RESTART_ROLLBACK_CONFLICT


def test_blocked_component_takes_precedence() -> None:
    blocked_plan = build_coding_result_archive_plan("plan-1", "turn-1", files=["../escape.py"])
    result = build_coding_result_uncertainty("uncertainty-1", "turn-1", blocked_plan, _pack())

    assert result.uncertainty is CodingResultUncertaintyState.BLOCKED
    assert result.reason is CodingResultUncertaintyReason.COMPONENT_BLOCKED


def test_mapping_roundtrip_and_validator() -> None:
    result = build_coding_result_uncertainty("uncertainty-1", "turn-1", _archive_plan(), _pack())
    encoded = result.to_mapping()

    assert encoded["schema"] == CODING_RESULT_UNCERTAINTY_SCHEMA
    assert build_coding_result_uncertainty(encoded) == result
    assert validate_coding_result_uncertainty(encoded) is True
    assert validate_coding_result_uncertainty({**encoded, "extra": 1}) is False
