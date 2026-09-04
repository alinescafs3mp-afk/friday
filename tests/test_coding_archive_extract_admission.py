from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.coding_archive_extract_admission import (
    CODING_ARCHIVE_EXTRACT_ADMISSION_SCHEMA,
    MAX_ARCHIVE_COMPRESSED_SIZE,
    MAX_ARCHIVE_COMPRESSION_RATIO,
    MAX_ARCHIVE_MEMBER_COUNT,
    MAX_ARCHIVE_NESTING_DEPTH,
    MAX_ARCHIVE_UNCOMPRESSED_SIZE,
    CodingArchiveExtractAdmissionError,
    CodingArchiveExtractAdmissionReason,
    CodingArchiveExtractAdmissionState,
    CodingArchiveExtractAdmissionV1,
    CodingArchiveFileKind,
    CodingArchiveLinkKind,
    CodingArchiveMemberV1,
    build_coding_archive_extract_admission,
    validate_coding_archive_extract_admission,
)


def member(
    path: str = "src/main.py",
    *,
    compressed_size: int = 100,
    uncompressed_size: int = 1_000,
    link_kind: CodingArchiveLinkKind = CodingArchiveLinkKind.NONE,
    file_kind: CodingArchiveFileKind = CodingArchiveFileKind.REGULAR_FILE,
) -> CodingArchiveMemberV1:
    return CodingArchiveMemberV1(
        path=path,
        compressed_size=compressed_size,
        uncompressed_size=uncompressed_size,
        link_kind=link_kind,
        file_kind=file_kind,
    )


def build(
    members: tuple[CodingArchiveMemberV1, ...] = (),
) -> CodingArchiveExtractAdmissionV1:
    return build_coding_archive_extract_admission("admission:1", "turn:1", members)


def test_empty_members_are_empty_not_admitted() -> None:
    result = build()

    assert result.admission is CodingArchiveExtractAdmissionState.EMPTY
    assert result.reason is CodingArchiveExtractAdmissionReason.NO_MEMBERS
    assert result.admitted_member_count == 0
    assert result.member_count == 0


def test_safe_regular_files_and_directories_are_admitted() -> None:
    result = build(
        (
            member("src/main.py"),
            member("src", file_kind=CodingArchiveFileKind.DIRECTORY, compressed_size=0, uncompressed_size=0),
        )
    )

    assert result.admission is CodingArchiveExtractAdmissionState.ADMITTED
    assert result.reason is CodingArchiveExtractAdmissionReason.ALL_MEMBERS_SAFE
    assert result.admitted_member_count == 2
    assert result.member_count == 2


@pytest.mark.parametrize(
    ("path", "reason"),
    (
        ("../outside.txt", CodingArchiveExtractAdmissionReason.PATH_TRAVERSAL),
        ("src/../../outside.txt", CodingArchiveExtractAdmissionReason.PATH_TRAVERSAL),
        ("/etc/passwd", CodingArchiveExtractAdmissionReason.ABSOLUTE_PATH),
        (r"C:\\Windows\\system.ini", CodingArchiveExtractAdmissionReason.ABSOLUTE_PATH),
        (r"\\\\server\\share\\file.txt", CodingArchiveExtractAdmissionReason.ABSOLUTE_PATH),
    ),
)
def test_unsafe_paths_are_blocked_without_counts(
    path: str, reason: CodingArchiveExtractAdmissionReason
) -> None:
    result = build((member(path),))

    assert result.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.reason is reason
    assert result.admitted_member_count == 0
    assert result.member_count == 0


@pytest.mark.parametrize(
    ("link_kind", "reason"),
    (
        (CodingArchiveLinkKind.SYMLINK, CodingArchiveExtractAdmissionReason.SYMLINK),
        (CodingArchiveLinkKind.HARDLINK, CodingArchiveExtractAdmissionReason.HARDLINK),
    ),
)
def test_links_are_blocked(
    link_kind: CodingArchiveLinkKind, reason: CodingArchiveExtractAdmissionReason
) -> None:
    result = build((member(link_kind=link_kind),))

    assert result.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.reason is reason
    assert result.member_count == 0


def test_device_members_are_blocked() -> None:
    result = build((member(file_kind=CodingArchiveFileKind.DEVICE),))

    assert result.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.reason is CodingArchiveExtractAdmissionReason.DEVICE_FILE
    assert result.member_count == 0


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    (
        (
            {"compressed_size": MAX_ARCHIVE_COMPRESSED_SIZE + 1},
            CodingArchiveExtractAdmissionReason.COMPRESSED_SIZE_LIMIT,
        ),
        (
            {"uncompressed_size": MAX_ARCHIVE_UNCOMPRESSED_SIZE + 1},
            CodingArchiveExtractAdmissionReason.UNCOMPRESSED_SIZE_LIMIT,
        ),
        (
            {"compressed_size": 1, "uncompressed_size": int(MAX_ARCHIVE_COMPRESSION_RATIO) + 2},
            CodingArchiveExtractAdmissionReason.COMPRESSION_BOMB,
        ),
    ),
)
def test_size_and_bomb_limits_are_blocked(
    kwargs: dict[str, int], reason: CodingArchiveExtractAdmissionReason
) -> None:
    result = build((member(**kwargs),))  # type: ignore[arg-type]

    assert result.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.reason is reason
    assert result.member_count == 0


def test_zero_compressed_bytes_with_content_is_a_bomb() -> None:
    result = build((member(compressed_size=0, uncompressed_size=1),))

    assert result.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.reason is CodingArchiveExtractAdmissionReason.COMPRESSION_BOMB


def test_aggregate_sizes_and_member_count_are_bounded() -> None:
    compressed = MAX_ARCHIVE_COMPRESSED_SIZE // 2 + 1
    result = build(
        (
            member("a.bin", compressed_size=compressed, uncompressed_size=compressed),
            member("b.bin", compressed_size=compressed, uncompressed_size=compressed),
        )
    )
    assert result.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.reason is CodingArchiveExtractAdmissionReason.COMPRESSED_SIZE_LIMIT

    too_many = tuple(member(f"file-{index}.txt") for index in range(MAX_ARCHIVE_MEMBER_COUNT + 1))
    result = build(too_many)
    assert result.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.reason is CodingArchiveExtractAdmissionReason.MEMBER_COUNT_LIMIT
    assert result.member_count == 0


def test_nesting_depth_is_bounded() -> None:
    path = "/".join(f"level{index}" for index in range(MAX_ARCHIVE_NESTING_DEPTH + 1)) + "/file.py"
    result = build((member(path),))

    assert result.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.reason is CodingArchiveExtractAdmissionReason.NESTING_DEPTH_LIMIT


def test_casefold_collisions_are_blocked() -> None:
    result = build((member("src/README.md"), member("src/readme.md")))

    assert result.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.reason is CodingArchiveExtractAdmissionReason.CASEFOLD_COLLISION
    assert result.member_count == 0


def test_mapping_members_and_serialized_result_round_trip() -> None:
    result = build_coding_archive_extract_admission(
        {
            "schema": CODING_ARCHIVE_EXTRACT_ADMISSION_SCHEMA,
            "admission_id": "admission:mapping",
            "authenticated_turn_id": "turn:mapping",
            "member_facts": [member("src/app.py").to_mapping()],
        }
    )

    assert result.admission is CodingArchiveExtractAdmissionState.ADMITTED
    encoded = result.to_mapping()
    assert build_coding_archive_extract_admission(encoded) == result
    assert validate_coding_archive_extract_admission(encoded) is True


def test_invalid_member_facts_fail_closed_without_partial_counts() -> None:
    result = build_coding_archive_extract_admission(
        "admission:invalid",
        "turn:1",
        (
            {
                "path": "src/app.py",
                "compressed_size": 100,
                "uncompressed_size": 1_000,
                "link_kind": "none",
                "file_kind": "unknown",
            },
        ),  # type: ignore[arg-type]
    )

    assert result.admission is CodingArchiveExtractAdmissionState.BLOCKED
    assert result.reason is CodingArchiveExtractAdmissionReason.INVALID_FACTS
    assert result.admitted_member_count == 0
    assert result.member_count == 0


def test_frozen_member_and_result_contracts_reject_invalid_direct_values() -> None:
    with pytest.raises(FrozenInstanceError):
        member().path = "other.py"  # type: ignore[misc]

    with pytest.raises(CodingArchiveExtractAdmissionError):
        CodingArchiveMemberV1("", 0, 0, CodingArchiveLinkKind.NONE, CodingArchiveFileKind.REGULAR_FILE)
    with pytest.raises(CodingArchiveExtractAdmissionError):
        CodingArchiveExtractAdmissionV1(
            "admission:1",
            "turn:1",
            CodingArchiveExtractAdmissionState.ADMITTED,
            0,
            0,
            CodingArchiveExtractAdmissionReason.NO_MEMBERS,
        )


def test_validator_rejects_wrong_schema_unknown_fields_and_malformed_counts() -> None:
    result = build((member(),))
    encoded = result.to_mapping()

    assert validate_coding_archive_extract_admission({**encoded, "schema": "invented"}) is False
    assert validate_coding_archive_extract_admission({**encoded, "extra": "nope"}) is False
    assert validate_coding_archive_extract_admission({**encoded, "member_count": 0}) is False
    assert (
        validate_coding_archive_extract_admission(
            {key: value for key, value in encoded.items() if key != "schema"}
        )
        is False
    )


def test_output_and_fact_representations_cannot_be_mixed() -> None:
    with pytest.raises(CodingArchiveExtractAdmissionError):
        build_coding_archive_extract_admission(
            {
                "admission_id": "admission:mixed",
                "authenticated_turn_id": "turn:1",
                "members": [member().to_mapping()],
                "admission": "admitted",
                "admitted_member_count": 1,
                "member_count": 1,
                "reason": "all_members_safe",
            }
        )
