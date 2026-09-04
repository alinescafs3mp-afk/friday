import pytest

from friday.orchestration.mixed_journey_revoke import (
    MixedJourneyRevokeState,
    build_mixed_journey_revoke,
)


def test_empty_and_held_never_admit_publication() -> None:
    empty = build_mixed_journey_revoke("journey", "turn")
    assert empty.state is MixedJourneyRevokeState.EMPTY
    held = build_mixed_journey_revoke(
        "journey", "turn", facts={"revoked": False, "publication_claimed": True}
    )
    assert held.state is MixedJourneyRevokeState.HELD
    assert held.publication_claimed is True
    assert held.can_publish is False
    assert held.publication_admitted is False
    assert build_mixed_journey_revoke(held.to_mapping()) == held


def test_revoke_before_publish_is_terminal_and_not_publishable() -> None:
    revoked = build_mixed_journey_revoke(
        "journey", "turn", facts={"revoked": True, "publication_claimed": False}
    )
    assert revoked.state is MixedJourneyRevokeState.REVOKED
    assert revoked.can_publish is False
    claimed_after_revoke = build_mixed_journey_revoke(
        "journey", "turn", facts={"revoked": True, "publication_claimed": True}
    )
    assert claimed_after_revoke.state is MixedJourneyRevokeState.REVOKED
    assert claimed_after_revoke.publication_admitted is False


@pytest.mark.parametrize(
    "facts",
    [{"revoked": "yes"}, {"publication_claimed": 1}, {"published": True}, {"revoked": True, "secret": "x"}],
)
def test_invalid_revoke_facts_fail_closed(facts: dict[str, object]) -> None:
    result = build_mixed_journey_revoke("journey", "turn", facts=facts)
    assert result.state is MixedJourneyRevokeState.BLOCKED
    assert result.publication_claimed is False
