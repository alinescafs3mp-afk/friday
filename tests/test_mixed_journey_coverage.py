from hashlib import sha256

import pytest

from friday.orchestration.mixed_journey_coverage import (
    MixedJourneyCoverageState,
    build_mixed_journey_coverage,
)
from friday.orchestration.mixed_journey_organs import build_mixed_journey_organs


def _organs():
    return build_mixed_journey_organs(
        "journey",
        "turn",
        facts={
            "file": True,
            "archive": True,
            "conversation": True,
            "web": True,
            "table": True,
            "engineer": False,
            "coding": False,
        },
    )


def _digests():
    return {
        name: sha256(name.encode()).hexdigest()
        for name in ("file", "archive", "conversation", "web", "table")
    }


def test_empty_partial_and_complete_coverage() -> None:
    assert build_mixed_journey_coverage("journey", "turn").state is MixedJourneyCoverageState.EMPTY
    partial = build_mixed_journey_coverage("journey", "turn", _organs(), {"file": _digests()["file"]})
    assert partial.state is MixedJourneyCoverageState.PARTIAL
    complete = build_mixed_journey_coverage("journey", "turn", _organs(), _digests())
    assert complete.state is MixedJourneyCoverageState.COMPLETE
    assert set(name for name, _ in complete.summary_digests) == {
        "file",
        "archive",
        "conversation",
        "web",
        "table",
    }


@pytest.mark.parametrize(
    "summaries",
    [
        {"file": {"body": "private"}},
        {"file": {"digest": "/private/path"}},
        {"file": {"digest": "https://example"}},
        {"unknown": "a" * 64},
    ],
)
def test_coverage_body_path_url_and_unknown_organ_hazards_block(summaries: dict[str, object]) -> None:
    result = build_mixed_journey_coverage("journey", "turn", _organs(), summaries)
    assert result.state is MixedJourneyCoverageState.BLOCKED
    assert result.summary_digests == ()


def test_known_absent_summary_does_not_block() -> None:
    result = build_mixed_journey_coverage("journey", "turn", _organs(), {"engineer": "a" * 64})
    assert result.state is MixedJourneyCoverageState.PARTIAL


def test_absent_organs_do_not_block_completion() -> None:
    organs = build_mixed_journey_organs(
        "journey",
        "turn",
        facts={
            name: False for name in ("file", "archive", "conversation", "web", "table", "engineer", "coding")
        },
    )
    result = build_mixed_journey_coverage("journey", "turn", organs, {})
    assert result.state is MixedJourneyCoverageState.COMPLETE
