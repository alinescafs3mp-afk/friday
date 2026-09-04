from friday.orchestration.coding_result_rollback_admission import (
    CODING_RESULT_ROLLBACK_ADMISSION_SCHEMA,
    CodingResultRollbackAdmissionReason,
    CodingResultRollbackAdmissionState,
    build_coding_result_rollback_admission,
    validate_coding_result_rollback_admission,
)


def test_no_rollback_facts_are_empty() -> None:
    result = build_coding_result_rollback_admission("rollback-1", "turn-1")
    assert result.admission is CodingResultRollbackAdmissionState.EMPTY


def test_exact_previous_revision_is_admitted() -> None:
    result = build_coding_result_rollback_admission("rollback-1", "turn-1", "operation-1", "revision-1")

    assert result.admission is CodingResultRollbackAdmissionState.ADMITTED
    assert result.operation_id == "operation-1"
    assert result.previous_revision == "revision-1"


def test_sha256_previous_revision_is_exactly_bound() -> None:
    digest = "a" * 64
    result = build_coding_result_rollback_admission("rollback-1", "turn-1", "operation-1", digest)
    assert result.previous_revision == digest


def test_missing_previous_revision_is_blocked_not_empty() -> None:
    result = build_coding_result_rollback_admission("rollback-1", "turn-1", "operation-1")

    assert result.admission is CodingResultRollbackAdmissionState.BLOCKED
    assert result.reason is CodingResultRollbackAdmissionReason.MISSING_PREVIOUS_REVISION


def test_previous_revision_mapping_is_supported() -> None:
    result = build_coding_result_rollback_admission(
        "rollback-1",
        "turn-1",
        "operation-1",
        {"revision_id": "revision-1"},
    )
    assert result.previous_revision == "revision-1"


def test_recency_selectors_fail_closed() -> None:
    for selector in ("latest", "HEAD", "newest", "current"):
        result = build_coding_result_rollback_admission("rollback-1", "turn-1", "operation-1", selector)
        assert result.admission is CodingResultRollbackAdmissionState.BLOCKED
        assert result.reason is CodingResultRollbackAdmissionReason.RECENCY_SELECTOR


def test_invalid_revision_and_operation_are_blocked() -> None:
    invalid_revision = build_coding_result_rollback_admission(
        "rollback-1", "turn-1", "operation-1", "revision with spaces"
    )
    missing_operation = build_coding_result_rollback_admission("rollback-1", "turn-1", None, "revision-1")

    assert invalid_revision.reason is CodingResultRollbackAdmissionReason.INVALID_REVISION
    assert missing_operation.reason is CodingResultRollbackAdmissionReason.MISSING_OPERATION_ID


def test_mapping_roundtrip_and_closed_validator() -> None:
    result = build_coding_result_rollback_admission(
        "rollback-1", "turn-1", "operation-1", "revision-1", revision_selector="selector-1"
    )
    encoded = result.to_mapping()

    assert encoded["schema"] == CODING_RESULT_ROLLBACK_ADMISSION_SCHEMA
    assert build_coding_result_rollback_admission(encoded) == result
    assert validate_coding_result_rollback_admission(encoded) is True
    assert validate_coding_result_rollback_admission({**encoded, "extra": 1}) is False
