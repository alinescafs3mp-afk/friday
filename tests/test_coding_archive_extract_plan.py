from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.coding_archive_extract_admission import (
    CodingArchiveExtractAdmissionState,
    CodingArchiveExtractAdmissionV1,
    CodingArchiveFileKind,
    CodingArchiveLinkKind,
    CodingArchiveMemberV1,
    build_coding_archive_extract_admission,
)
from friday.orchestration.coding_archive_extract_plan import (
    CODING_ARCHIVE_EXTRACT_PLAN_SCHEMA,
    CodingArchiveExtractPlanError,
    CodingArchiveExtractPlanReason,
    CodingArchiveExtractPlanState,
    CodingArchiveExtractPlanV1,
    build_coding_archive_extract_plan,
    validate_coding_archive_extract_plan,
)
from friday.orchestration.coding_archive_member_catalog import (
    CodingArchiveMemberCatalogV1,
    build_coding_archive_member_catalog,
)


def member(path: str = "src/main.py") -> CodingArchiveMemberV1:
    return CodingArchiveMemberV1(
        path=path,
        compressed_size=100,
        uncompressed_size=1_000,
        link_kind=CodingArchiveLinkKind.NONE,
        file_kind=CodingArchiveFileKind.REGULAR_FILE,
    )


def facts(
    members: tuple[CodingArchiveMemberV1, ...],
    *,
    turn: str = "turn:1",
) -> tuple[CodingArchiveMemberCatalogV1, CodingArchiveExtractAdmissionV1]:
    catalog = build_coding_archive_member_catalog("catalog:1", turn, members)
    admission = build_coding_archive_extract_admission("admission:1", turn, members)
    return catalog, admission


def test_empty_catalog_and_empty_admission_make_an_empty_plan() -> None:
    catalog, admission = facts(())
    result = build_coding_archive_extract_plan("plan:empty", "turn:1", catalog, admission)

    assert result.plan is CodingArchiveExtractPlanState.EMPTY
    assert result.reason is CodingArchiveExtractPlanReason.NO_MEMBERS
    assert result.destination_paths == ()


def test_admitted_members_make_a_relative_destination_plan() -> None:
    catalog, admission = facts((member("src\\main.py"), member("README.md")))
    result = build_coding_archive_extract_plan("plan:1", "turn:1", catalog, admission)

    assert result.plan is CodingArchiveExtractPlanState.PLANNED
    assert result.reason is CodingArchiveExtractPlanReason.ALL_DESTINATIONS_PLANNED
    assert result.destination_paths == ("src/main.py", "README.md")
    assert result.planned_member_count == result.member_count == 2
    with pytest.raises(FrozenInstanceError):
        result.destination_paths = ()  # type: ignore[misc]


def test_non_admitted_members_cannot_be_planned() -> None:
    catalog, _ = facts((member("link"),))
    blocked_admission = build_coding_archive_extract_admission(
        "admission:blocked",
        "turn:1",
        (  # type: ignore[arg-type]
            CodingArchiveMemberV1(
                path="link",
                compressed_size=100,
                uncompressed_size=1_000,
                link_kind=CodingArchiveLinkKind.SYMLINK,
                file_kind=CodingArchiveFileKind.REGULAR_FILE,
            ),
        ),
    )
    result = build_coding_archive_extract_plan("plan:blocked", "turn:1", catalog, blocked_admission)

    assert blocked_admission.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.plan is CodingArchiveExtractPlanState.BLOCKED
    assert result.reason is CodingArchiveExtractPlanReason.ADMISSION_NOT_GRANTED
    assert result.destination_paths == ()
    assert result.member_count == 0


def test_identity_mismatch_blocks_without_destinations() -> None:
    catalog, admission = facts((member(),), turn="turn:1")
    result = build_coding_archive_extract_plan("plan:mismatch", "turn:other", catalog, admission)

    assert result.plan is CodingArchiveExtractPlanState.BLOCKED
    assert result.reason is CodingArchiveExtractPlanReason.IDENTITY_MISMATCH
    assert result.destination_paths == ()


def test_invalid_catalog_or_admission_fails_closed() -> None:
    result = build_coding_archive_extract_plan(
        "plan:invalid",
        "turn:1",
        {"catalog_id": "catalog:1", "authenticated_turn_id": "turn:1", "members": "bad"},
        None,
    )

    assert result.plan is CodingArchiveExtractPlanState.BLOCKED
    assert result.reason is CodingArchiveExtractPlanReason.CATALOG_INVALID
    assert result.destination_paths == ()


def test_conventional_plan_id_catalog_admission_positional_order_is_supported() -> None:
    catalog, admission = facts((member(),))
    result = build_coding_archive_extract_plan("plan:ordered", catalog, admission)

    assert result.plan is CodingArchiveExtractPlanState.PLANNED


def test_mapping_and_serialized_result_round_trip() -> None:
    catalog, admission = facts((member(),))
    result = build_coding_archive_extract_plan(
        {
            "schema": CODING_ARCHIVE_EXTRACT_PLAN_SCHEMA,
            "plan_id": "plan:mapping",
            "authenticated_turn_id": "turn:1",
            "catalog": catalog.to_mapping(),
            "admission": admission.to_mapping(),
        }
    )

    assert result.plan is CodingArchiveExtractPlanState.PLANNED
    encoded = result.to_mapping()
    assert build_coding_archive_extract_plan(encoded) == result
    assert validate_coding_archive_extract_plan(encoded) is True


def test_validator_rejects_unknown_fields_and_unsafe_serialized_paths() -> None:
    catalog, admission = facts((member(),))
    result = build_coding_archive_extract_plan("plan:1", catalog, admission)
    encoded = result.to_mapping()

    assert validate_coding_archive_extract_plan({**encoded, "extra": "nope"}) is False
    assert validate_coding_archive_extract_plan({**encoded, "destination_paths": ["../outside"]}) is False


def test_direct_plan_rejects_paths_on_blocked_result() -> None:
    with pytest.raises(CodingArchiveExtractPlanError):
        CodingArchiveExtractPlanV1(
            "plan:bad",
            "turn:1",
            CodingArchiveExtractPlanState.BLOCKED,
            1,
            1,
            ("src/main.py",),
            CodingArchiveExtractPlanReason.INVALID_FACTS,
        )
