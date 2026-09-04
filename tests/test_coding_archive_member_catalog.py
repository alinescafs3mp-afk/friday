from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.coding_archive_extract_admission import (
    CodingArchiveFileKind,
    CodingArchiveLinkKind,
    CodingArchiveMemberV1,
)
from friday.orchestration.coding_archive_member_catalog import (
    CODING_ARCHIVE_MEMBER_CATALOG_SCHEMA,
    CodingArchiveMemberCatalogError,
    CodingArchiveMemberCatalogReason,
    CodingArchiveMemberCatalogState,
    CodingArchiveMemberCatalogV1,
    build_coding_archive_member_catalog,
    validate_coding_archive_member_catalog,
)


def member(path: str = "src/main.py") -> CodingArchiveMemberV1:
    return CodingArchiveMemberV1(
        path=path,
        compressed_size=100,
        uncompressed_size=1_000,
        link_kind=CodingArchiveLinkKind.NONE,
        file_kind=CodingArchiveFileKind.REGULAR_FILE,
    )


def test_empty_catalog_is_not_catalogued() -> None:
    result = build_coding_archive_member_catalog("catalog:1", "turn:1", ())

    assert result.catalog is CodingArchiveMemberCatalogState.EMPTY
    assert result.reason is CodingArchiveMemberCatalogReason.NO_MEMBERS
    assert result.catalogued_member_count == 0
    assert result.member_count == 0
    assert result.members == ()


def test_observed_members_are_catalogued_and_retained_frozen() -> None:
    result = build_coding_archive_member_catalog("catalog:1", "turn:1", (member(), member("README.md")))

    assert result.catalog is CodingArchiveMemberCatalogState.CATALOGUED
    assert result.reason is CodingArchiveMemberCatalogReason.ALL_MEMBERS_CATALOGUED
    assert result.catalogued_member_count == result.member_count == 2
    assert result.members[0].path == "src/main.py"
    with pytest.raises(FrozenInstanceError):
        result.members = ()  # type: ignore[misc]


def test_invalid_member_facts_fail_closed_without_counts() -> None:
    result = build_coding_archive_member_catalog(
        "catalog:invalid",
        "turn:1",
        (
            {
                "path": "src/main.py",
                "compressed_size": 100,
                "uncompressed_size": 1_000,
                "link_kind": "none",
                "file_kind": "unknown",
            },
        ),  # type: ignore[arg-type]
    )

    assert result.catalog is CodingArchiveMemberCatalogState.BLOCKED
    assert result.reason is CodingArchiveMemberCatalogReason.INVALID_FACTS
    assert result.catalogued_member_count == result.member_count == 0
    assert result.members == ()


def test_member_count_limit_is_closed() -> None:
    members = tuple(member(f"file-{index}.txt") for index in range(4_097))
    result = build_coding_archive_member_catalog("catalog:large", "turn:1", members)

    assert result.catalog is CodingArchiveMemberCatalogState.BLOCKED
    assert result.reason is CodingArchiveMemberCatalogReason.MEMBER_COUNT_LIMIT
    assert result.member_count == 0


def test_mapping_and_serialized_result_round_trip() -> None:
    result = build_coding_archive_member_catalog(
        {
            "schema": CODING_ARCHIVE_MEMBER_CATALOG_SCHEMA,
            "catalog_id": "catalog:mapping",
            "authenticated_turn_id": "turn:mapping",
            "member_facts": [member().to_mapping()],
        }
    )

    encoded = result.to_mapping()
    assert build_coding_archive_member_catalog(encoded) == result
    assert validate_coding_archive_member_catalog(encoded) is True


def test_validator_rejects_unknown_fields_and_malformed_result() -> None:
    result = build_coding_archive_member_catalog("catalog:1", "turn:1", (member(),))
    encoded = result.to_mapping()

    assert validate_coding_archive_member_catalog({**encoded, "extra": "nope"}) is False
    assert validate_coding_archive_member_catalog({**encoded, "schema": "invented"}) is False
    assert validate_coding_archive_member_catalog({**encoded, "member_count": 0}) is False


def test_direct_result_rejects_counts_on_blocked_catalog() -> None:
    with pytest.raises(CodingArchiveMemberCatalogError):
        CodingArchiveMemberCatalogV1(
            "catalog:1",
            "turn:1",
            CodingArchiveMemberCatalogState.BLOCKED,
            1,
            1,
            CodingArchiveMemberCatalogReason.INVALID_FACTS,
            (member(),),
        )
