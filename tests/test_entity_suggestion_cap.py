"""A cap meant for a noisy rule must not eat confirmed links.

`_MAX_SUGGESTIONS_PER_METHOD = 8` exists because `capitalized_person_name` produced
64 of 103 candidates on a real document — Title Case in headings, not people. It was
applied to every method, including `existing_entity_exact_mention`: an exact literal
mention of an entity the user already has in their graph, confidence 0.97. A note
naming more than eight known entities lost the surplus, and lost it silently — the
dropped ones appear in neither `graph_links` nor `unresolved_entities`. Because every
such candidate carries the same confidence, the survivors were decided alphabetically.

On the stand archive this was already biting: one object has exactly eight
`existing_entity_exact_mention` links — the cap — while its text names 22 of the 47
canonical entities.
"""

from __future__ import annotations

from jericho.ingestion._advice import (
    _MAX_SUGGESTIONS,
    _MAX_SUGGESTIONS_PER_METHOD,
    _capped_per_method,
)
from jericho.ingestion._base import DECLARED_ENTITY_METHODS, EVIDENCE_ONLY_ENTITY_METHODS


def _candidates(method: str, count: int, confidence: float) -> list[dict]:
    return [
        {"name": f"{method}-{index:02d}", "method": method, "confidence": confidence}
        for index in range(count)
    ]


def test_exact_mentions_of_known_entities_all_survive():
    ordered = _candidates("existing_entity_exact_mention", 20, 0.97)
    kept = _capped_per_method(
        ordered,
        per_method=_MAX_SUGGESTIONS_PER_METHOD,
        total=_MAX_SUGGESTIONS,
        exempt=DECLARED_ENTITY_METHODS,
    )
    assert len(kept) == 20, "confirmed links to entities the user already has were dropped"


def test_the_noisy_rule_is_still_capped():
    """The reason the cap exists in the first place."""
    ordered = _candidates("capitalized_person_name", 40, 0.55)
    kept = _capped_per_method(
        ordered,
        per_method=_MAX_SUGGESTIONS_PER_METHOD,
        total=_MAX_SUGGESTIONS,
        exempt=DECLARED_ENTITY_METHODS,
    )
    assert len(kept) == _MAX_SUGGESTIONS_PER_METHOD


def test_a_noisy_rule_cannot_crowd_out_declared_ones():
    """Confidence order does the work; the cap only has to not undo it."""
    ordered = _candidates("existing_entity_exact_mention", 12, 0.97) + _candidates(
        "capitalized_person_name", 40, 0.55
    )
    kept = _capped_per_method(
        ordered,
        per_method=_MAX_SUGGESTIONS_PER_METHOD,
        total=_MAX_SUGGESTIONS,
        exempt=DECLARED_ENTITY_METHODS,
    )
    methods = [item["method"] for item in kept]
    assert methods.count("existing_entity_exact_mention") == 12
    assert methods.count("capitalized_person_name") == _MAX_SUGGESTIONS_PER_METHOD


def test_the_exemption_covers_declared_methods_only():
    assert not (DECLARED_ENTITY_METHODS & EVIDENCE_ONLY_ENTITY_METHODS)
    assert "existing_entity_exact_mention" in DECLARED_ENTITY_METHODS
    assert "capitalized_person_name" in EVIDENCE_ONLY_ENTITY_METHODS


def test_the_global_ceiling_still_holds():
    ordered = _candidates("existing_entity_exact_mention", _MAX_SUGGESTIONS + 15, 0.97)
    kept = _capped_per_method(
        ordered,
        per_method=_MAX_SUGGESTIONS_PER_METHOD,
        total=_MAX_SUGGESTIONS,
        exempt=DECLARED_ENTITY_METHODS,
    )
    assert len(kept) == _MAX_SUGGESTIONS
