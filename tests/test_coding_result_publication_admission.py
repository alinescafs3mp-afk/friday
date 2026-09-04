from friday.orchestration.coding_result_archive_manifest import build_coding_result_archive_manifest
from friday.orchestration.coding_result_archive_pack_admission import (
    build_coding_result_archive_pack_admission,
)
from friday.orchestration.coding_result_archive_plan import build_coding_result_archive_plan
from friday.orchestration.coding_result_publication_admission import (
    CODING_RESULT_PUBLICATION_ADMISSION_SCHEMA,
    CodingResultPublicationAdmissionReason,
    CodingResultPublicationAdmissionState,
    build_coding_result_publication_admission,
    validate_coding_result_publication_admission,
)
from friday.orchestration.coding_result_uncertainty import build_coding_result_uncertainty

SHA256 = "a" * 64


def _file_plan() -> object:
    return build_coding_result_archive_plan("plan-file", "turn-1", files=["only.py"])


def _archive_plan() -> object:
    return build_coding_result_archive_plan("plan-archive", "turn-1", files=["a.py", "b.py"])


def _archive_pack(plan: object = None) -> object:
    plan = _archive_plan() if plan is None else plan
    return build_coding_result_archive_pack_admission(
        "pack-1",
        "turn-1",
        plan,
        build_coding_result_archive_manifest("manifest-1", "turn-1", {"a.py": SHA256, "b.py": SHA256}),
    )


def test_no_facts_are_empty() -> None:
    result = build_coding_result_publication_admission("publication-1", "turn-1")
    assert result.admission is CodingResultPublicationAdmissionState.EMPTY


def test_file_publication_is_admitted_without_a_pack() -> None:
    plan = _file_plan()
    uncertainty = build_coding_result_uncertainty("uncertainty-1", "turn-1", plan)
    result = build_coding_result_publication_admission(
        "publication-1", "turn-1", plan, uncertainty=uncertainty
    )

    assert result.admission is CodingResultPublicationAdmissionState.ADMITTED
    assert result.carrier == "file"
    assert result.pack_id is None


def test_archive_publication_requires_matching_admitted_pack_and_known_uncertainty() -> None:
    plan = _archive_plan()
    pack = _archive_pack(plan)
    uncertainty = build_coding_result_uncertainty("uncertainty-1", "turn-1", plan, pack)
    result = build_coding_result_publication_admission(
        "publication-1", "turn-1", plan, pack, uncertainty=uncertainty
    )

    assert result.admission is CodingResultPublicationAdmissionState.ADMITTED
    assert result.carrier == "archive"
    assert result.pack_id == "pack-1"


def test_unknown_uncertainty_is_not_admitted() -> None:
    plan = _archive_plan()
    uncertainty = build_coding_result_uncertainty("uncertainty-1", "turn-1", plan)
    result = build_coding_result_publication_admission(
        "publication-1", "turn-1", plan, uncertainty=uncertainty
    )

    assert result.admission is CodingResultPublicationAdmissionState.BLOCKED
    assert result.reason is CodingResultPublicationAdmissionReason.UNCERTAINTY_UNKNOWN


def test_archive_without_pack_and_file_with_pack_are_blocked() -> None:
    plan = _archive_plan()
    unknown = build_coding_result_uncertainty("uncertainty-1", "turn-1", plan)
    missing_pack = build_coding_result_publication_admission(
        "publication-1", "turn-1", plan, uncertainty=unknown
    )
    file_plan = _file_plan()
    file_pack = _archive_pack()
    file_uncertainty = build_coding_result_uncertainty("uncertainty-1", "turn-1", file_plan)
    file_with_pack = build_coding_result_publication_admission(
        "publication-1", "turn-1", file_plan, file_pack, uncertainty=file_uncertainty
    )

    assert missing_pack.reason is CodingResultPublicationAdmissionReason.UNCERTAINTY_UNKNOWN
    assert file_with_pack.reason is CodingResultPublicationAdmissionReason.PACK_FORBIDDEN_FOR_FILE


def test_blocked_input_and_mismatch_fail_closed() -> None:
    blocked_plan = build_coding_result_archive_plan("plan-file", "turn-1", files=["../escape.py"])
    blocked_uncertainty = build_coding_result_uncertainty("uncertainty-1", "turn-1", blocked_plan)
    blocked = build_coding_result_publication_admission(
        "publication-1", "turn-1", blocked_plan, uncertainty=blocked_uncertainty
    )
    plan = _archive_plan()
    pack = _archive_pack()
    mismatched_uncertainty = build_coding_result_uncertainty(
        "uncertainty-2", "turn-1", build_coding_result_archive_plan("other-plan", "turn-1", files=["a.py"])
    )
    mismatch = build_coding_result_publication_admission(
        "publication-1", "turn-1", plan, pack, uncertainty=mismatched_uncertainty
    )

    assert blocked.reason is CodingResultPublicationAdmissionReason.COMPONENT_BLOCKED
    assert mismatch.reason is CodingResultPublicationAdmissionReason.IDENTITY_MISMATCH


def test_mapping_roundtrip_and_validator() -> None:
    plan = _archive_plan()
    pack = _archive_pack(plan)
    uncertainty = build_coding_result_uncertainty("uncertainty-1", "turn-1", plan, pack)
    result = build_coding_result_publication_admission(
        "publication-1", "turn-1", plan, pack, uncertainty=uncertainty
    )
    encoded = result.to_mapping()

    assert encoded["schema"] == CODING_RESULT_PUBLICATION_ADMISSION_SCHEMA
    assert build_coding_result_publication_admission(encoded) == result
    assert validate_coding_result_publication_admission(encoded) is True
    assert validate_coding_result_publication_admission({**encoded, "extra": 1}) is False
