from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.coding_archive_extract_admission import (
    CodingArchiveExtractAdmissionState,
    CodingArchiveFileKind,
    CodingArchiveLinkKind,
    CodingArchiveMemberV1,
    build_coding_archive_extract_admission,
)
from friday.orchestration.coding_archive_extract_plan import build_coding_archive_extract_plan
from friday.orchestration.coding_archive_member_catalog import build_coding_archive_member_catalog
from friday.orchestration.coding_archive_overwrite_plan import (
    CODING_ARCHIVE_OVERWRITE_PLAN_SCHEMA,
    CodingArchiveExistingDestinationFactV1,
    CodingArchiveOverwriteInputV1,
    CodingArchiveOverwritePlanError,
    CodingArchiveOverwritePlanReason,
    CodingArchiveOverwritePlanState,
    CodingArchiveOverwritePlanV1,
    build_coding_archive_overwrite_plan,
    validate_coding_archive_overwrite_plan,
)


def member(path: str = "src/main.py") -> CodingArchiveMemberV1:
    return CodingArchiveMemberV1(
        path=path,
        compressed_size=100,
        uncompressed_size=1_000,
        link_kind=CodingArchiveLinkKind.NONE,
        file_kind=CodingArchiveFileKind.REGULAR_FILE,
    )


def extract_plan(path: str = "src/main.py") -> object:
    members = (member(path),)
    catalog = build_coding_archive_member_catalog("catalog:1", "turn:1", members)
    admission = build_coding_archive_extract_admission("admission:1", "turn:1", members)
    return build_coding_archive_extract_plan("extract:1", "turn:1", catalog, admission)


def test_empty_extract_plan_is_empty_overwrite_plan() -> None:
    members: tuple[CodingArchiveMemberV1, ...] = ()
    catalog = build_coding_archive_member_catalog("catalog:empty", "turn:1", members)
    admission = build_coding_archive_extract_admission("admission:empty", "turn:1", members)
    plan = build_coding_archive_extract_plan("extract:empty", "turn:1", catalog, admission)
    result = build_coding_archive_overwrite_plan("overwrite:empty", "turn:1", plan)

    assert result.plan is CodingArchiveOverwritePlanState.EMPTY
    assert result.reason is CodingArchiveOverwritePlanReason.NO_DESTINATIONS
    assert result.destination_paths == ()


def test_planned_destinations_are_clear_when_no_existing_fact_matches() -> None:
    result = build_coding_archive_overwrite_plan(
        "overwrite:clear",
        "turn:1",
        extract_plan(),
        (CodingArchiveExistingDestinationFactV1("other.py", True),),
    )

    assert result.plan is CodingArchiveOverwritePlanState.CLEAR
    assert result.reason is CodingArchiveOverwritePlanReason.NO_COLLISIONS
    assert result.destination_paths == ("src/main.py",)
    assert result.collision_paths == ()
    with pytest.raises(FrozenInstanceError):
        result.destination_paths = ()  # type: ignore[misc]


def test_existing_destination_is_collision_and_is_not_clear() -> None:
    result = build_coding_archive_overwrite_plan(
        "overwrite:existing",
        "turn:1",
        extract_plan(),
        ({"path": "SRC\\MAIN.PY", "exists": True},),  # type: ignore[arg-type]
    )

    assert result.plan is CodingArchiveOverwritePlanState.COLLISION
    assert result.reason is CodingArchiveOverwritePlanReason.EXISTING_DESTINATION
    assert result.destination_paths == ("src/main.py",)
    assert result.collision_paths == ("src/main.py",)
    assert result.existing_destination_paths == ("SRC/MAIN.PY",)


def test_casefold_collision_is_collision_without_filesystem_access() -> None:
    result = build_coding_archive_overwrite_plan(
        "overwrite:casefold",
        "turn:1",
        destination_paths=("src/README.md", "src/readme.md"),
    )

    assert result.plan is CodingArchiveOverwritePlanState.COLLISION
    assert result.reason is CodingArchiveOverwritePlanReason.CASEFOLD_COLLISION
    assert result.collision_paths == ("src/README.md", "src/readme.md")


def test_false_existing_fact_does_not_create_collision() -> None:
    result = build_coding_archive_overwrite_plan(
        "overwrite:false",
        "turn:1",
        destination_paths=("src/main.py",),
        existing_destinations=(CodingArchiveExistingDestinationFactV1("src/main.py", False),),
    )

    assert result.plan is CodingArchiveOverwritePlanState.CLEAR
    assert result.existing_destination_paths == ()


def test_frozen_overwrite_input_is_supported() -> None:
    inputs = CodingArchiveOverwriteInputV1(
        destination_paths=("src/main.py",),
        existing_destinations=(CodingArchiveExistingDestinationFactV1("other.py", True),),
    )
    result = build_coding_archive_overwrite_plan("overwrite:input", "turn:1", inputs)

    assert result.plan is CodingArchiveOverwritePlanState.CLEAR


def test_blocked_extract_plan_blocks_without_exposing_paths() -> None:
    blocked_members = (
        CodingArchiveMemberV1(
            path="src/link",
            compressed_size=100,
            uncompressed_size=1_000,
            link_kind=CodingArchiveLinkKind.SYMLINK,
            file_kind=CodingArchiveFileKind.REGULAR_FILE,
        ),
    )
    catalog = build_coding_archive_member_catalog("catalog:blocked", "turn:1", blocked_members)
    admission = build_coding_archive_extract_admission("admission:blocked", "turn:1", blocked_members)
    plan = build_coding_archive_extract_plan("extract:blocked", "turn:1", catalog, admission)
    result = build_coding_archive_overwrite_plan("overwrite:blocked", "turn:1", plan)

    assert admission.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.plan is CodingArchiveOverwritePlanState.BLOCKED
    assert result.reason is CodingArchiveOverwritePlanReason.PLAN_INVALID
    assert result.destination_paths == result.collision_paths == result.existing_destination_paths == ()


def test_invalid_existing_facts_fail_closed_without_paths() -> None:
    result = build_coding_archive_overwrite_plan(
        "overwrite:invalid",
        "turn:1",
        destination_paths=("src/main.py",),
        existing_destinations=({"path": "../outside", "exists": True},),  # type: ignore[arg-type]
    )

    assert result.plan is CodingArchiveOverwritePlanState.BLOCKED
    assert result.reason is CodingArchiveOverwritePlanReason.INVALID_FACTS
    assert result.destination_paths == ()


def test_invalid_extract_plan_fails_closed_instead_of_becoming_empty() -> None:
    result = build_coding_archive_overwrite_plan(
        "overwrite:bad-plan",
        "turn:1",
        {"schema": "invented", "plan_id": "extract:1"},
    )

    assert result.plan is CodingArchiveOverwritePlanState.BLOCKED
    assert result.reason is CodingArchiveOverwritePlanReason.PLAN_INVALID
    assert result.destination_paths == ()


def test_mapping_and_serialized_result_round_trip() -> None:
    result = build_coding_archive_overwrite_plan(
        "overwrite:1",
        "turn:1",
        destination_paths=("src/main.py",),
    )
    encoded = result.to_mapping()
    encoded["schema"] = CODING_ARCHIVE_OVERWRITE_PLAN_SCHEMA

    assert build_coding_archive_overwrite_plan(encoded) == result
    assert validate_coding_archive_overwrite_plan(encoded) is True


def test_positional_extract_plan_and_existing_facts_are_supported() -> None:
    result = build_coding_archive_overwrite_plan(
        "overwrite:ordered",
        extract_plan(),
        ("src/main.py",),
    )

    assert result.plan is CodingArchiveOverwritePlanState.COLLISION


def test_validator_and_direct_result_reject_closed_shape_violations() -> None:
    result = build_coding_archive_overwrite_plan("overwrite:1", "turn:1", destination_paths=("src/main.py",))
    assert validate_coding_archive_overwrite_plan({**result.to_mapping(), "extra": "nope"}) is False
    assert validate_coding_archive_overwrite_plan({**result.to_mapping(), "schema": "invented"}) is False
    with pytest.raises(CodingArchiveOverwritePlanError):
        CodingArchiveOverwritePlanV1(
            "overwrite:bad",
            "turn:1",
            CodingArchiveOverwritePlanState.BLOCKED,
            ("src/main.py",),
            (),
            (),
            CodingArchiveOverwritePlanReason.INVALID_FACTS,
        )
