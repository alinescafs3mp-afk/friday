from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.mixed_journey_file_facts import (
    MixedJourneyFileFactsState,
    build_mixed_journey_file_facts,
)

DIGEST = "a" * 64


def test_empty_present_and_mapping_round_trip() -> None:
    assert build_mixed_journey_file_facts().state is MixedJourneyFileFactsState.EMPTY
    result = build_mixed_journey_file_facts("file-1", DIGEST, "application/pdf")
    assert result.state is MixedJourneyFileFactsState.PRESENT
    assert result.file_id == "file-1"
    assert build_mixed_journey_file_facts(result.to_mapping()) == result


@pytest.mark.parametrize(
    "facts",
    [
        {"file_id": "/home/user/private.pdf", "sha256": DIGEST},
        {"file_id": "../private", "sha256": DIGEST},
        {"file_id": "secret_file", "sha256": DIGEST},
        {"file_id": "file-1", "sha256": DIGEST, "path": "/tmp/private.pdf"},
        {"file_id": "file-1", "sha256": "not-a-digest"},
    ],
)
def test_private_paths_secret_names_and_invalid_facts_block_without_leak(facts: dict[str, object]) -> None:
    result = build_mixed_journey_file_facts(facts)
    assert result.state is MixedJourneyFileFactsState.BLOCKED
    assert result.file_id is None
    assert result.sha256 is None
    assert "/" not in str(result.to_mapping())


def test_result_is_frozen() -> None:
    result = build_mixed_journey_file_facts("file-1", DIGEST)
    with pytest.raises(FrozenInstanceError):
        result.file_id = "other"  # type: ignore[misc]
