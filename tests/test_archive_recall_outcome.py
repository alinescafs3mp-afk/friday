from __future__ import annotations

import json
from dataclasses import replace

import pytest

from friday.orchestration.archive_recall_outcome import (
    ACCEPTED_ARCHIVE_RECALL_OUTCOME_METADATA_KEY,
    AcceptedArchiveRecallOutcomeReceipt,
    ArchiveRecallLane,
    ArchiveRecallOutcome,
    ArchiveRecallOutcomeError,
    ArchiveRecallStatus,
    attach_accepted_archive_recall_outcome_receipt,
    load_accepted_archive_recall_outcome_receipt,
)
from friday.retrieval.archive_search_authority import (
    ARCHIVE_AUTHORITY_MAX_CANDIDATES,
    ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES,
    ArchiveSearchCoverageGrade,
)

_PLAN = "1" * 64
_EVIDENCE = "2" * 64
_COVERAGE = "3" * 64
_ANSWER = "4" * 64


def _outcome(
    *,
    grade: ArchiveSearchCoverageGrade = ArchiveSearchCoverageGrade.COMPLETE,
    candidate_count: int = 2,
    labels: tuple[str, ...] = ("A1.1",),
) -> ArchiveRecallOutcome:
    status = (
        ArchiveRecallStatus.COMPLETE
        if candidate_count and grade is ArchiveSearchCoverageGrade.COMPLETE
        else ArchiveRecallStatus.PARTIAL
        if candidate_count
        else ArchiveRecallStatus.EMPTY
        if grade is ArchiveSearchCoverageGrade.COMPLETE
        else ArchiveRecallStatus.INCOMPLETE_EMPTY
    )
    return ArchiveRecallOutcome(
        lane=ArchiveRecallLane.FEDERATED_SEARCH,
        status=status,
        plan_sha256=_PLAN,
        evidence_sha256=_EVIDENCE,
        coverage_sha256=_COVERAGE,
        coverage_grade=grade,
        candidate_count=candidate_count,
        used_citation_labels=labels,
        selected_evidence=None,
        publication_attested=True,
        semantic_verified=False,
        answer_sha256=_ANSWER,
    )


@pytest.mark.parametrize(
    ("grade", "candidate_count", "status"),
    (
        (ArchiveSearchCoverageGrade.COMPLETE, 2, ArchiveRecallStatus.COMPLETE),
        (ArchiveSearchCoverageGrade.PARTIAL, 2, ArchiveRecallStatus.PARTIAL),
        (ArchiveSearchCoverageGrade.COMPLETE, 0, ArchiveRecallStatus.EMPTY),
        (ArchiveSearchCoverageGrade.PARTIAL, 0, ArchiveRecallStatus.INCOMPLETE_EMPTY),
    ),
)
def test_all_honest_archive_recall_statuses_round_trip(
    grade: ArchiveSearchCoverageGrade,
    candidate_count: int,
    status: ArchiveRecallStatus,
) -> None:
    labels = ("A1.1",) if candidate_count else ()
    outcome = _outcome(grade=grade, candidate_count=candidate_count, labels=labels)

    assert outcome.status is status
    assert outcome.publication_attested is True
    assert outcome.semantic_verified is False
    assert ArchiveRecallOutcome.parse(outcome.to_json()) == outcome
    assert ArchiveRecallOutcome.parse(outcome.to_payload()) == outcome
    assert "query" not in outcome.to_payload()
    assert "excerpt" not in outcome.to_payload()
    assert "title" not in outcome.to_payload()


def test_archive_recall_receipt_attaches_and_loads_without_using_capability_outcome_v1() -> None:
    outcome = _outcome()
    metadata: dict[str, object] = {"existing": {"kept": True}}

    receipt = attach_accepted_archive_recall_outcome_receipt(metadata, outcome)
    loaded = load_accepted_archive_recall_outcome_receipt(metadata, expected_outcome=outcome)
    encoded = json.dumps(metadata, ensure_ascii=False, sort_keys=True)

    assert loaded == receipt == AcceptedArchiveRecallOutcomeReceipt.from_outcome(outcome)
    assert metadata["existing"] == {"kept": True}
    assert ACCEPTED_ARCHIVE_RECALL_OUTCOME_METADATA_KEY in metadata
    assert "accepted_capability_outcome" not in metadata
    assert "PRIVATE QUERY CANARY" not in encoded
    assert "PRIVATE EXCERPT CANARY" not in encoded
    assert AcceptedArchiveRecallOutcomeReceipt.parse(receipt.to_json()) == receipt


@pytest.mark.parametrize(
    "mutation",
    (
        {"status": ArchiveRecallStatus.PARTIAL},
        {"publication_attested": False},
        {"semantic_verified": True},
        {"candidate_count": True},
        {"used_citation_labels": ("A0",)},
    ),
)
def test_archive_recall_outcome_rejects_false_semantics(mutation: dict[str, object]) -> None:
    with pytest.raises(ArchiveRecallOutcomeError):
        replace(_outcome(), **mutation)


def test_archive_recall_parser_rejects_open_shape_tamper_and_noncanonical_json() -> None:
    outcome = _outcome()
    payload = outcome.to_payload()

    opened = {**payload, "excerpt": "PRIVATE EXCERPT CANARY"}
    with pytest.raises(ArchiveRecallOutcomeError, match="keys"):
        ArchiveRecallOutcome.parse(opened)

    count_drift = {**payload, "used_citation_count": 2}
    with pytest.raises(ArchiveRecallOutcomeError, match="count"):
        ArchiveRecallOutcome.parse(count_drift)

    noncanonical = json.dumps(payload, ensure_ascii=True, sort_keys=False, indent=2)
    with pytest.raises(ArchiveRecallOutcomeError, match="not canonical"):
        ArchiveRecallOutcome.parse(noncanonical)

    duplicate = outcome.to_json().replace(
        '"answer_sha256":"4444444444444444444444444444444444444444444444444444444444444444"',
        '"answer_sha256":"4444444444444444444444444444444444444444444444444444444444444444",'
        '"answer_sha256":"4444444444444444444444444444444444444444444444444444444444444444"',
    )
    with pytest.raises(ArchiveRecallOutcomeError, match="duplicate"):
        ArchiveRecallOutcome.parse(duplicate)

    oversized_integer = "9" * 5_000
    with pytest.raises(ArchiveRecallOutcomeError, match="one JSON object"):
        ArchiveRecallOutcome.parse(oversized_integer)
    with pytest.raises(ArchiveRecallOutcomeError, match="one JSON object"):
        AcceptedArchiveRecallOutcomeReceipt.parse(oversized_integer)
    with pytest.raises(ArchiveRecallOutcomeError, match="one JSON object"):
        load_accepted_archive_recall_outcome_receipt(
            '{"accepted_archive_recall_outcome":' + oversized_integer + "}"
        )

    oversized_mapping = {
        **payload,
        "used_citation_count": 1,
        "used_citation_labels": ["A" + "1" * 60_000],
    }
    with pytest.raises(ArchiveRecallOutcomeError, match="citation labels"):
        ArchiveRecallOutcome.parse(oversized_mapping)

    too_many_labels = ARCHIVE_AUTHORITY_MAX_MODEL_BATCHES * ARCHIVE_AUTHORITY_MAX_CANDIDATES * 9 + 1
    oversized_label_array = {
        **payload,
        "used_citation_count": too_many_labels,
        "used_citation_labels": ["A1"] * too_many_labels,
    }
    with pytest.raises(ArchiveRecallOutcomeError, match="citation labels"):
        ArchiveRecallOutcome.parse(oversized_label_array)


def test_archive_recall_receipt_tamper_overwrite_and_budget_fail_atomically() -> None:
    outcome = _outcome()
    receipt = AcceptedArchiveRecallOutcomeReceipt.from_outcome(outcome)
    tampered = receipt.to_payload()
    tampered["outcome_sha256"] = "f" * 64
    with pytest.raises(ArchiveRecallOutcomeError, match="does not match"):
        AcceptedArchiveRecallOutcomeReceipt.parse(tampered)

    metadata: dict[str, object] = {}
    attach_accepted_archive_recall_outcome_receipt(metadata, outcome)
    retained = json.loads(json.dumps(metadata))
    with pytest.raises(ArchiveRecallOutcomeError, match="already attached"):
        attach_accepted_archive_recall_outcome_receipt(metadata, outcome)
    assert metadata == retained

    too_small: dict[str, object] = {"existing": "kept"}
    with pytest.raises(ArchiveRecallOutcomeError, match="exceeds"):
        attach_accepted_archive_recall_outcome_receipt(
            too_small,
            outcome,
            max_serialized_bytes=32,
        )
    assert too_small == {"existing": "kept"}

    with pytest.raises(ArchiveRecallOutcomeError, match="does not match"):
        load_accepted_archive_recall_outcome_receipt(
            metadata,
            expected_outcome=replace(outcome, answer_sha256="a" * 64),
        )
