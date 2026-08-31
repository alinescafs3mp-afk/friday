from __future__ import annotations

import socket
from collections import Counter

import pytest

from friday.retrieval.archive_evidence_replay import ArchiveEvidenceReplayStatus
from friday.retrieval.archive_search_contract import ArchiveMatchChannel
from friday.retrieval.contracts import AbsenceDecision
from friday.retrieval_benchmark.contracts import RecallOutcomeV1
from friday.retrieval_benchmark.document_harness import (
    EphemeralDocumentRecallRunV1,
    document_measurements_json,
    run_document_ephemeral,
)
from friday.retrieval_benchmark.document_synthetic import (
    DocumentRecallClassV1,
    document_synthetic_plan,
)
from friday.retrieval_benchmark.harness import observations_jsonl


@pytest.fixture(scope="module")
def document_run() -> EphemeralDocumentRecallRunV1:
    return run_document_ephemeral()


def _measurement(
    run: EphemeralDocumentRecallRunV1,
    recall_class: DocumentRecallClassV1,
):
    matches = tuple(item for item in run.measurements if item.recall_class is recall_class)
    assert len(matches) == 1
    return matches[0]


def test_document_manifest_is_one_closed_five_class_corpus(
    document_run: EphemeralDocumentRecallRunV1,
) -> None:
    assert len(document_run.cases) == 5
    assert Counter(item.recall_class for item in document_run.measurements) == {
        DocumentRecallClassV1.FILENAME: 1,
        DocumentRecallClassV1.ALIAS: 1,
        DocumentRecallClassV1.FORMAT: 1,
        DocumentRecallClassV1.DATE: 1,
        DocumentRecallClassV1.TRUNCATION: 1,
    }
    assert tuple(case.case_id for case in document_run.cases) == tuple(
        f"document.case.{ordinal:04d}" for ordinal in range(1, 6)
    )
    assert tuple(item.case_id for item in document_run.measurements) == tuple(
        case.opaque_case_id for case in document_run.cases
    )
    assert document_run.restart_performed is True
    assert len(document_synthetic_plan().documents) == 10


def test_filename_class_is_closed_on_the_public_archive_path(
    document_run: EphemeralDocumentRecallRunV1,
) -> None:
    item = _measurement(document_run, DocumentRecallClassV1.FILENAME)

    assert item.gap_codes == ()
    assert item.target_recalled and item.passage_exact and item.authorized_only
    assert item.match_channels == (ArchiveMatchChannel.CATALOG, ArchiveMatchChannel.LEXICAL)
    assert item.discovery_target_visible and item.discovery_navigation_only
    assert item.discovery_absence_decision is AbsenceDecision.EVIDENCE_FOUND


def test_alias_class_is_closed_on_the_public_archive_path(
    document_run: EphemeralDocumentRecallRunV1,
) -> None:
    item = _measurement(document_run, DocumentRecallClassV1.ALIAS)

    assert item.gap_codes == ()
    assert item.target_recalled and item.passage_exact and item.authorized_only
    assert item.match_channels == (ArchiveMatchChannel.CATALOG, ArchiveMatchChannel.LEXICAL)
    assert item.discovery_target_visible and item.discovery_navigation_only
    assert item.discovery_absence_decision is AbsenceDecision.EVIDENCE_FOUND


def test_format_class_reproduces_the_current_catalog_gap(
    document_run: EphemeralDocumentRecallRunV1,
) -> None:
    item = _measurement(document_run, DocumentRecallClassV1.FORMAT)

    assert item.gap_codes == (
        "channel_mismatch",
        "discovery_false_absence",
        "discovery_miss",
    )
    assert item.target_recalled and item.passage_exact and item.authorized_only
    assert item.match_channels == (ArchiveMatchChannel.LEXICAL,)
    assert item.discovery_target_visible is False
    assert item.discovery_absence_decision is AbsenceDecision.NOT_ESTABLISHED


def test_date_class_reproduces_the_current_own_date_gap(
    document_run: EphemeralDocumentRecallRunV1,
) -> None:
    item = _measurement(document_run, DocumentRecallClassV1.DATE)

    assert item.gap_codes == (
        "channel_mismatch",
        "discovery_false_absence",
        "discovery_miss",
        "passage_mismatch",
        "qrel_miss",
        "replay_not_exact",
        "target_not_recalled",
        "temporal_role_mismatch",
    )
    assert item.target_recalled is False
    assert item.discovery_absence_decision is AbsenceDecision.NOT_ESTABLISHED
    assert item.replay_status is None


def test_truncation_class_is_closed_without_false_complete_or_false_absence(
    document_run: EphemeralDocumentRecallRunV1,
) -> None:
    item = _measurement(document_run, DocumentRecallClassV1.TRUNCATION)

    assert item.gap_codes == ()
    assert item.target_recalled and item.passage_exact and item.authorized_only
    assert item.match_channels == (ArchiveMatchChannel.LEXICAL,)
    assert item.discovery_absence_decision is AbsenceDecision.EVIDENCE_FOUND
    assert item.safety_absence_decision is AbsenceDecision.NOT_ESTABLISHED
    assert item.safety_exhaustive is False


def test_four_current_positive_sources_replay_exactly_after_clean_restart(
    document_run: EphemeralDocumentRecallRunV1,
) -> None:
    exact = [
        item
        for item in document_run.measurements
        if item.replay_status is ArchiveEvidenceReplayStatus.EXACT
    ]

    assert len(exact) == 4
    assert all(item.replay_model_sha256 is not None for item in exact)
    date = _measurement(document_run, DocumentRecallClassV1.DATE)
    assert date.replay_status is None and "replay_not_exact" in date.gap_codes
    assert {item.outcome for item in document_run.case_results} == {
        RecallOutcomeV1.HIT,
        RecallOutcomeV1.MISS,
    }


def test_document_public_evidence_is_body_query_path_and_locator_free(
    document_run: EphemeralDocumentRecallRunV1,
) -> None:
    serialized = (
        observations_jsonl(document_run.observations)
        + document_run.report.to_json()
        + document_measurements_json(document_run.measurements)
        + repr(document_run.measurements)
    )
    forbidden = (
        "Frosted archive evidence",
        "s4r7-filename-saffron",
        "s4r7-historical-alias",
        "text/plain",
        "Saffron own-date evidence",
        "Visible cobalt truncation",
        "ultraviolettail9853",
        "document-recall-foreign-principal",
        "recall-benchmark-principal",
        "raw_e000",
        '"query":',
        '"locator":',
        '"excerpt":',
        "/home/",
        "/var/tmp/",
    )
    assert all(value not in serialized for value in forbidden)


def test_second_offline_document_run_is_byte_identical_and_network_forbidden(
    document_run: EphemeralDocumentRecallRunV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("document benchmark attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    second = run_document_ephemeral()

    assert second.report.to_json() == document_run.report.to_json()
    assert observations_jsonl(second.observations) == observations_jsonl(document_run.observations)
    assert document_measurements_json(second.measurements) == document_measurements_json(
        document_run.measurements
    )
