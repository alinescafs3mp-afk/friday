import pytest

from friday.orchestration.mixed_journey_archive_facts import (
    MAX_MEMBER_COUNT,
    MixedJourneyArchiveFactsState,
    build_mixed_journey_archive_facts,
)

DIGEST = "b" * 64


def test_empty_present_and_mapping_round_trip() -> None:
    assert build_mixed_journey_archive_facts().state is MixedJourneyArchiveFactsState.EMPTY
    result = build_mixed_journey_archive_facts("archive-1", DIGEST, 3)
    assert result.state is MixedJourneyArchiveFactsState.PRESENT
    assert result.member_count == 3
    assert build_mixed_journey_archive_facts(result.to_mapping()) == result


@pytest.mark.parametrize(
    "facts",
    [
        {"archive_id": "/home/user/archive.zip", "sha256": DIGEST, "member_count": 1},
        {"archive_id": "../archive", "sha256": DIGEST, "member_count": 1},
        {"archive_id": "private_archive", "sha256": DIGEST, "member_count": 1},
        {"archive_id": "archive-1", "sha256": DIGEST, "member_count": MAX_MEMBER_COUNT + 1},
        {"archive_id": "archive-1", "sha256": DIGEST, "member_count": 1, "member_names": ["secret.txt"]},
    ],
)
def test_archive_hazards_block_without_names(facts: dict[str, object]) -> None:
    result = build_mixed_journey_archive_facts(facts)
    assert result.state is MixedJourneyArchiveFactsState.BLOCKED
    assert result.archive_id is None
    assert result.sha256 is None
    assert result.member_count is None
    assert "secret.txt" not in str(result.to_mapping())
