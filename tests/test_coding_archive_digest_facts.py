from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.coding_archive_digest_facts import (
    CODING_ARCHIVE_DIGEST_FACTS_SCHEMA,
    CodingArchiveDigestFactsError,
    CodingArchiveDigestFactsReason,
    CodingArchiveDigestFactsState,
    CodingArchiveDigestFactsV1,
    CodingArchiveDigestInputV1,
    build_coding_archive_digest_facts,
    validate_coding_archive_digest_facts,
)

SHA256 = "a" * 64


def test_missing_digest_is_empty() -> None:
    result = build_coding_archive_digest_facts("digest:empty", "turn:1")

    assert result.digest_state is CodingArchiveDigestFactsState.EMPTY
    assert result.reason is CodingArchiveDigestFactsReason.NO_DIGEST
    assert result.sha256 is None


def test_lowercase_sha256_is_bound_without_hashing_bytes() -> None:
    result = build_coding_archive_digest_facts("digest:1", "turn:1", SHA256)

    assert result.digest_state is CodingArchiveDigestFactsState.BOUND
    assert result.reason is CodingArchiveDigestFactsReason.SHA256_BOUND
    assert result.sha256 == SHA256
    assert result.digest_sha256 == SHA256
    assert result.archive_sha256 == SHA256


@pytest.mark.parametrize("value", ("A" * 64, "g" * 64, "a" * 63, b"a" * 64, 1))
def test_non_sha256_values_are_blocked_without_exposing_digest(value: object) -> None:
    result = build_coding_archive_digest_facts("digest:bad", "turn:1", value)

    assert result.digest_state is CodingArchiveDigestFactsState.BLOCKED
    assert result.reason is CodingArchiveDigestFactsReason.INVALID_DIGEST
    assert result.sha256 is None


def test_input_mapping_and_frozen_input_are_supported() -> None:
    mapping_result = build_coding_archive_digest_facts(
        {
            "schema": CODING_ARCHIVE_DIGEST_FACTS_SCHEMA,
            "digest_id": "digest:mapping",
            "authenticated_turn_id": "turn:mapping",
            "archive_sha256": SHA256,
        }
    )
    frozen_result = build_coding_archive_digest_facts(
        "digest:frozen",
        "turn:1",
        CodingArchiveDigestInputV1(SHA256),
    )

    assert mapping_result.digest_state is CodingArchiveDigestFactsState.BOUND
    assert frozen_result.sha256 == SHA256


def test_mapping_and_serialized_result_round_trip() -> None:
    result = build_coding_archive_digest_facts("digest:1", "turn:1", SHA256)
    encoded = result.to_mapping()

    assert build_coding_archive_digest_facts(encoded) == result
    assert validate_coding_archive_digest_facts(encoded) is True


def test_malformed_nested_digest_fact_fails_closed() -> None:
    result = build_coding_archive_digest_facts("digest:nested", "turn:1", {"unexpected": SHA256})

    assert result.digest_state is CodingArchiveDigestFactsState.BLOCKED
    assert result.reason is CodingArchiveDigestFactsReason.INVALID_FACTS
    assert result.sha256 is None


def test_result_is_frozen_and_validator_is_closed() -> None:
    result = build_coding_archive_digest_facts("digest:1", "turn:1", SHA256)
    with pytest.raises(FrozenInstanceError):
        result.sha256 = None  # type: ignore[misc]
    with pytest.raises(CodingArchiveDigestFactsError):
        CodingArchiveDigestFactsV1(
            "digest:bad",
            "turn:1",
            CodingArchiveDigestFactsState.BOUND,
            None,
            CodingArchiveDigestFactsReason.SHA256_BOUND,
        )

    assert validate_coding_archive_digest_facts({**result.to_mapping(), "extra": "nope"}) is False
    assert validate_coding_archive_digest_facts({**result.to_mapping(), "schema": "invented"}) is False
