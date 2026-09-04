from __future__ import annotations

import pytest

from friday.orchestration.coding_project_identity import (
    CodingProjectIdentityError,
    CodingProjectIdentityFactsV1,
    CodingProjectIdentityReason,
    CodingProjectIdentityState,
    CodingProjectIdentityV1,
    build_coding_project_identity,
)


def test_mapping_with_exact_project_and_revision_is_identified_and_frozen() -> None:
    result = build_coding_project_identity(
        "identity-1",
        "turn-1",
        {"project_id": "friday", "revision_selector": "a" * 40},
    )
    assert result.identity is CodingProjectIdentityState.IDENTIFIED
    assert result.project_id == "friday"
    assert result.revision_selector == "a" * 40
    assert result.reason is CodingProjectIdentityReason.IDENTIFIED
    with pytest.raises(AttributeError):
        result.project_id = "other"  # type: ignore[misc]


def test_frozen_input_dataclass_and_explicit_keyword_facts_are_identified() -> None:
    frozen = build_coding_project_identity(
        "identity-1",
        "turn-1",
        CodingProjectIdentityFactsV1("friday", "rev-2026-09-04"),
    )
    explicit = build_coding_project_identity(
        "identity-2",
        "turn-1",
        project_id="friday",
        revision_selector="rev-2026-09-04",
    )
    assert frozen.identity is CodingProjectIdentityState.IDENTIFIED
    assert explicit.identity is CodingProjectIdentityState.IDENTIFIED


def test_empty_facts_are_empty_not_identified() -> None:
    assert build_coding_project_identity("identity-1", "turn-1").identity is CodingProjectIdentityState.EMPTY
    result = build_coding_project_identity(
        "identity-1", "turn-1", {"project_id": None, "revision_selector": None}
    )
    assert result.identity is CodingProjectIdentityState.EMPTY
    assert result.reason is CodingProjectIdentityReason.NO_FACTS
    assert result.project_id is None
    assert result.revision_selector is None


@pytest.mark.parametrize(
    "selector",
    ("latest", "HEAD", "newest", "current", "", "  ", " Latest "),
)
def test_recency_and_empty_revision_selectors_are_blocked(selector: str) -> None:
    result = build_coding_project_identity(
        "identity-1",
        "turn-1",
        {"project_id": "friday", "revision_selector": selector},
    )
    assert result.identity is CodingProjectIdentityState.BLOCKED
    assert result.reason is CodingProjectIdentityReason.RECENCY_REVISION_SELECTOR
    assert result.project_id is None
    assert result.revision_selector is None


def test_missing_one_identity_fact_is_blocked_without_leaking_the_other() -> None:
    project_only = build_coding_project_identity("identity-1", "turn-1", project_id="friday")
    revision_only = build_coding_project_identity("identity-2", "turn-1", revision_selector="rev-1")
    assert project_only.identity is CodingProjectIdentityState.BLOCKED
    assert project_only.reason is CodingProjectIdentityReason.MISSING_REVISION_SELECTOR
    assert revision_only.identity is CodingProjectIdentityState.BLOCKED
    assert revision_only.reason is CodingProjectIdentityReason.MISSING_PROJECT_ID
    assert project_only.project_id is None
    assert revision_only.revision_selector is None


@pytest.mark.parametrize(
    ("project_id", "revision_selector"),
    (("", "rev-1"), ("friday/project", "rev-1"), ("friday", "rev/1"), (1, "rev-1")),
)
def test_non_opaque_identity_facts_are_blocked(project_id: object, revision_selector: object) -> None:
    result = build_coding_project_identity(
        "identity-1",
        "turn-1",
        project_id=project_id,
        revision_selector=revision_selector,
    )
    assert result.identity is CodingProjectIdentityState.BLOCKED
    assert result.reason is CodingProjectIdentityReason.INVALID_FACTS
    assert result.project_id is None
    assert result.revision_selector is None


def test_positional_project_and_revision_values_are_supported() -> None:
    result = build_coding_project_identity("identity-1", "turn-1", "friday", "rev-1")
    assert result.identity is CodingProjectIdentityState.IDENTIFIED
    assert result.project_id == "friday"
    assert result.revision_selector == "rev-1"


def test_unknown_mapping_fields_and_conflicting_input_forms_are_blocked() -> None:
    unknown = build_coding_project_identity(
        "identity-1", "turn-1", {"project_id": "friday", "extra": "value"}
    )
    conflict = build_coding_project_identity(
        "identity-2",
        "turn-1",
        {"project_id": "friday"},
        project_id="friday",
        revision_selector="rev-1",
    )
    assert unknown.identity is CodingProjectIdentityState.BLOCKED
    assert conflict.identity is CodingProjectIdentityState.BLOCKED
    assert unknown.reason is CodingProjectIdentityReason.INVALID_FACTS
    assert conflict.reason is CodingProjectIdentityReason.INVALID_FACTS


def test_invalid_identity_arguments_raise_at_the_contract_boundary() -> None:
    with pytest.raises(CodingProjectIdentityError):
        build_coding_project_identity("/private", "turn-1")
    with pytest.raises(CodingProjectIdentityError):
        CodingProjectIdentityV1(
            "identity-1",
            "turn-1",
            CodingProjectIdentityState.IDENTIFIED,
            None,
            None,
            CodingProjectIdentityReason.IDENTIFIED,
        )
