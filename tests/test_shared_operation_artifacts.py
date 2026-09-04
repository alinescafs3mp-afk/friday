from dataclasses import FrozenInstanceError

import pytest

from friday.orchestration.shared_operation_artifacts import (
    SharedOperationArtifactsState,
    build_shared_operation_artifacts,
    validate_shared_operation_artifacts,
)


def test_empty_artifacts_have_no_summary() -> None:
    result = build_shared_operation_artifacts("artifacts-1", "turn-1")
    assert result.state is SharedOperationArtifactsState.EMPTY
    assert result.artifact_count == 0
    assert result.artifact_digest is None


def test_artifact_summary_is_body_free_and_round_trips() -> None:
    result = build_shared_operation_artifacts(
        "artifacts-1",
        "turn-1",
        facts={
            "artifact_class": "document",
            "artifact_count": 2,
            "artifact_digest": "a" * 64,
            "terminal_evidence_class": "confirmed",
        },
    )
    assert result.state is SharedOperationArtifactsState.SUMMARISED
    assert result.artifact_class == "document"
    assert result.terminal_evidence_class == "confirmed"
    assert "body" not in result.to_mapping()
    assert build_shared_operation_artifacts(result.to_mapping()) == result
    assert validate_shared_operation_artifacts(result.to_mapping())


@pytest.mark.parametrize(
    "facts",
    [
        {"artifact_class": "document", "artifact_count": 1, "artifact_digest": "a" * 64, "body": "secret"},
        {"artifact_class": "document", "artifact_count": 1, "artifact_digest": "a" * 64, "path": "/tmp/x"},
        {
            "artifact_class": "document",
            "artifact_count": 1,
            "artifact_digest": "a" * 64,
            "url": "https://example.test",
        },
        {"artifact_class": "document", "artifact_count": 1, "artifact_digest": "a" * 64, "secret": "value"},
    ],
)
def test_body_path_url_and_secret_facts_fail_closed(facts: dict[str, object]) -> None:
    result = build_shared_operation_artifacts("artifacts-1", "turn-1", facts=facts)
    assert result.state is SharedOperationArtifactsState.BLOCKED
    assert result.artifact_class is None
    assert result.artifact_count == 0
    assert result.artifact_digest is None


@pytest.mark.parametrize(
    "facts",
    [
        {"artifact_class": "document", "artifact_count": -1, "artifact_digest": "a" * 64},
        {"artifact_class": "document", "artifact_count": 1, "artifact_digest": "bad"},
        {"artifact_count": 1, "artifact_digest": "a" * 64},
    ],
)
def test_invalid_artifact_summary_fails_closed(facts: dict[str, object]) -> None:
    result = build_shared_operation_artifacts("artifacts-1", "turn-1", facts=facts)
    assert result.state is SharedOperationArtifactsState.BLOCKED


def test_artifacts_are_frozen() -> None:
    result = build_shared_operation_artifacts(
        "artifacts-1", "turn-1", artifact_class="text", artifact_count=1, artifact_digest="a" * 64
    )
    with pytest.raises(FrozenInstanceError):
        result.artifact_count = 2  # type: ignore[misc]
