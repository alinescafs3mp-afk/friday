from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.coding_result_archive_manifest import (
    CODING_RESULT_ARCHIVE_MANIFEST_SCHEMA,
    CodingResultArchiveManifestError,
    CodingResultArchiveManifestReason,
    CodingResultArchiveManifestState,
    CodingResultArchiveManifestV1,
    build_coding_result_archive_manifest,
    validate_coding_result_archive_manifest,
)

SHA256 = "a" * 64


def test_no_members_are_empty() -> None:
    result = build_coding_result_archive_manifest("manifest-1", "turn-1")

    assert result.manifest is CodingResultArchiveManifestState.EMPTY
    assert result.members == ()
    assert result.files == ()


def test_mapping_of_names_to_digests_is_listed_deterministically() -> None:
    result = build_coding_result_archive_manifest(
        "manifest-1",
        "turn-1",
        {"b.py": SHA256, "a.py": "b" * 64},
    )

    assert result.manifest is CodingResultArchiveManifestState.LISTED
    assert result.files == ("a.py", "b.py")
    assert result.digests["a.py"] == "b" * 64


def test_sequence_entries_and_digest_alias_are_supported() -> None:
    result = build_coding_result_archive_manifest(
        "manifest-1",
        "turn-1",
        [{"path": "src/main.py", "digest": SHA256}],
    )

    assert result.entries[0].path == "src/main.py"
    assert result.entries[0].digest == SHA256


@pytest.mark.parametrize("name", ("../escape.py", "/tmp/file", "C:\\tmp\\file", "a/../../b"))
def test_unsafe_paths_are_blocked_without_exposing_names(name: str) -> None:
    result = build_coding_result_archive_manifest("manifest-1", "turn-1", [(name, SHA256)])

    assert result.manifest is CodingResultArchiveManifestState.BLOCKED
    assert result.reason is CodingResultArchiveManifestReason.UNSAFE_PATH
    assert result.members == ()
    assert name not in str(result.to_mapping())


@pytest.mark.parametrize("name", (".env", "config/secrets.json", "id_rsa"))
def test_secret_names_are_blocked(name: str) -> None:
    result = build_coding_result_archive_manifest("manifest-1", "turn-1", [(name, SHA256)])

    assert result.reason is CodingResultArchiveManifestReason.SECRET_NAME
    assert result.members == ()


def test_casefold_collision_is_blocked() -> None:
    result = build_coding_result_archive_manifest(
        "manifest-1", "turn-1", [("Readme.md", SHA256), ("README.md", SHA256)]
    )

    assert result.reason is CodingResultArchiveManifestReason.CASEFOLD_COLLISION
    assert result.members == ()


@pytest.mark.parametrize("digest", ("A" * 64, "g" * 64, "a" * 63, b"a" * 64, None))
def test_invalid_or_missing_digests_fail_closed(digest: object) -> None:
    result = build_coding_result_archive_manifest("manifest-1", "turn-1", [("a.py", digest)])

    assert result.manifest is CodingResultArchiveManifestState.BLOCKED
    assert result.reason in {
        CodingResultArchiveManifestReason.INVALID_DIGEST,
        CodingResultArchiveManifestReason.INVALID_FACTS,
    }
    assert result.members == ()


def test_serialized_roundtrip_validator_and_frozen_contract() -> None:
    result = build_coding_result_archive_manifest("manifest-1", "turn-1", {"a.py": SHA256})
    encoded = result.to_mapping()

    assert encoded["schema"] == CODING_RESULT_ARCHIVE_MANIFEST_SCHEMA
    assert build_coding_result_archive_manifest(encoded) == result
    assert validate_coding_result_archive_manifest(encoded) is True
    with pytest.raises(FrozenInstanceError):
        result.manifest = CodingResultArchiveManifestState.EMPTY  # type: ignore[misc]


def test_invalid_serialized_shapes_are_rejected() -> None:
    result = build_coding_result_archive_manifest("manifest-1", "turn-1", {"a.py": SHA256})

    assert validate_coding_result_archive_manifest({**result.to_mapping(), "extra": 1}) is False
    assert validate_coding_result_archive_manifest({**result.to_mapping(), "schema": "other"}) is False
    with pytest.raises(CodingResultArchiveManifestError):
        CodingResultArchiveManifestV1(
            "manifest-1",
            "turn-1",
            CodingResultArchiveManifestState.LISTED,
            (),
            CodingResultArchiveManifestReason.FILES_LISTED,
        )
