from __future__ import annotations

import socket

import pytest

import friday.retrieval_benchmark.harness as harness_module
from friday.retrieval.archive_search_contract import ArchiveSearchCorpus
from friday.retrieval.contracts import AbsenceDecision, PassageLocatorKind, TemporalRole
from friday.retrieval_benchmark.contracts import (
    RecallContractError,
    RecallEvidenceSourceV1,
    RecallOutcomeV1,
    RecallTaxonomyV1,
)
from friday.retrieval_benchmark.harness import (
    EphemeralRecallRunV1,
    archive_search_release_sha256,
    cases_jsonl,
    observations_jsonl,
    run_ephemeral,
)
from friday.retrieval_benchmark.io import parse_observations_jsonl
from friday.retrieval_benchmark.metrics import score_recall
from friday.retrieval_benchmark.synthetic import _DOCUMENTS, synthetic_cases


@pytest.fixture(scope="module")
def ephemeral_run() -> EphemeralRecallRunV1:
    return run_ephemeral()


def test_ephemeral_manifest_has_at_least_twenty_cases_and_all_ten_classes(
    ephemeral_run: EphemeralRecallRunV1,
) -> None:
    assert len(ephemeral_run.cases) >= 20
    counts = {taxonomy: 0 for taxonomy in RecallTaxonomyV1}
    for case in ephemeral_run.cases:
        counts[case.taxonomy] += 1
    assert all(count >= 2 for count in counts.values())


def test_ephemeral_manifest_covers_documents_messages_dates_pending_old_and_unknown(
    ephemeral_run: EphemeralRecallRunV1,
) -> None:
    corpora = {case.expected_corpus for case in ephemeral_run.cases}
    assert corpora == {ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.MESSAGES}
    assert sum(case.taxonomy is RecallTaxonomyV1.PENDING_FILE for case in ephemeral_run.cases) == 2
    assert sum(case.taxonomy is RecallTaxonomyV1.OLD_FILE for case in ephemeral_run.cases) == 2
    assert sum(bool(case.request.temporal_constraints) for case in ephemeral_run.cases) >= 6


def test_unknown_corpus_cases_are_cross_corpus_positive_qrels() -> None:
    unknown = [
        case
        for case in synthetic_cases()
        if case.taxonomy is RecallTaxonomyV1.UNKNOWN_CORPUS and not case.expected_no_hit
    ]
    assert len(unknown) == 2
    assert {case.expected_corpus for case in unknown} == {
        ArchiveSearchCorpus.DOCUMENTS,
        ArchiveSearchCorpus.MESSAGES,
    }
    assert all(
        case.request.corpora == (ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.MESSAGES)
        and not case.expected_no_hit
        and len(case.alternatives) == 1
        for case in unknown
    )
    by_corpus = {case.expected_corpus: case.alternatives[0] for case in unknown}
    assert by_corpus[ArchiveSearchCorpus.DOCUMENTS].locator_kind is PassageLocatorKind.TEXT_SPAN
    assert by_corpus[ArchiveSearchCorpus.MESSAGES].locator_kind is PassageLocatorKind.MESSAGE_WINDOW


def test_dated_qrels_cover_received_and_uploaded_roles_with_in_range_truth() -> None:
    cases = synthetic_cases()
    dated = [case for case in cases if case.request.temporal_constraints]
    assert {case.alternatives[0].temporal_role for case in dated} == {
        TemporalRole.RECEIVED_AT,
        TemporalRole.UPLOADED_AT,
    }
    for case in dated:
        constraint = case.request.temporal_constraints[0]
        assert case.alternatives[0].temporal_role is constraint.role
        spec = _DOCUMENTS[int(case.case_id.removeprefix("case.")) - 1]
        ground_truth = spec.received_at if constraint.role is TemporalRole.RECEIVED_AT else spec.uploaded_at
        assert ground_truth is not None
        assert constraint.start <= ground_truth < constraint.end


def test_pending_noncanonical_candidates_remain_visible_as_recall_loss(
    ephemeral_run: EphemeralRecallRunV1,
) -> None:
    outcomes = {item.case_id: item.outcome for item in ephemeral_run.case_results}
    pending = [case for case in ephemeral_run.cases if case.taxonomy is RecallTaxonomyV1.PENDING_FILE]
    assert {outcomes[case.opaque_case_id] for case in pending} == {RecallOutcomeV1.MISS}


def test_unknown_corpus_runs_both_lanes_and_finds_the_positive_qrels(
    ephemeral_run: EphemeralRecallRunV1,
) -> None:
    by_id = {item.case_id: item for item in ephemeral_run.observations}
    unknown = [
        case
        for case in ephemeral_run.cases
        if case.taxonomy is RecallTaxonomyV1.UNKNOWN_CORPUS and not case.expected_no_hit
    ]
    assert {by_id[case.opaque_case_id].absence_decision for case in unknown} == {
        AbsenceDecision.EVIDENCE_FOUND
    }
    outcomes = {item.case_id: item.outcome for item in ephemeral_run.case_results}
    assert {outcomes[case.opaque_case_id] for case in unknown} == {RecallOutcomeV1.HIT}


def test_unknown_corpus_explicit_no_hit_remains_uncertain(
    ephemeral_run: EphemeralRecallRunV1,
) -> None:
    no_hit = [case for case in ephemeral_run.cases if case.expected_no_hit]
    assert len(no_hit) == 1
    observation = {item.case_id: item for item in ephemeral_run.observations}[no_hit[0].opaque_case_id]
    outcome = {item.case_id: item.outcome for item in ephemeral_run.case_results}[no_hit[0].opaque_case_id]
    assert observation.absence_decision is AbsenceDecision.NOT_ESTABLISHED
    assert outcome is RecallOutcomeV1.UNCERTAIN_NO_HIT


def test_ephemeral_observations_and_report_are_body_free(
    ephemeral_run: EphemeralRecallRunV1,
) -> None:
    serialized = observations_jsonl(ephemeral_run.observations) + ephemeral_run.report.to_json()
    forbidden = (
        "Orchid nebula budget reconciliation",
        "scan_0009.bin",
        "synthetic current archive request",
        "recall-benchmark-tenant",
        "recall-benchmark-principal",
        "/home/",
        '"filename":',
        '"excerpt":',
        '"prompt":',
        '"tool_args":',
        '"tool_output":',
    )
    assert all(value not in serialized for value in forbidden)


def test_release_and_evidence_source_are_explicit(ephemeral_run: EphemeralRecallRunV1) -> None:
    assert ephemeral_run.report.release_sha256 == archive_search_release_sha256()
    assert ephemeral_run.report.evidence_source is RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL
    assert {observation.evidence_source for observation in ephemeral_run.observations} == {
        RecallEvidenceSourceV1.SYNTHETIC_EPHEMERAL
    }


def test_serialized_fixture_cannot_reclaim_in_process_shipped_provenance(
    ephemeral_run: EphemeralRecallRunV1,
) -> None:
    parsed = parse_observations_jsonl(observations_jsonl(ephemeral_run.observations).encode("ascii"))
    with pytest.raises(RecallContractError):
        score_recall(ephemeral_run.cases, parsed)


def test_jsonl_sidecar_serialization_is_bounded_before_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = synthetic_cases()[:2]
    monkeypatch.setattr(harness_module, "MAX_JSONL_ITEMS", 1)
    with pytest.raises(RecallContractError):
        cases_jsonl(cases)

    monkeypatch.setattr(harness_module, "MAX_JSONL_ITEMS", 1_000)
    monkeypatch.setattr(harness_module, "MAX_JSONL_BYTES", 1)
    with pytest.raises(RecallContractError):
        cases_jsonl(cases[:1])


def test_two_offline_real_path_runs_are_byte_identical(
    ephemeral_run: EphemeralRecallRunV1,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"prepare": 0, "attest": 0}
    prepared_origins = {}
    real_prepare = harness_module.prepare_archive_search_in_transaction
    real_attest = harness_module.attest_archive_search_before_publication

    def prepare(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["prepare"] += 1
        prepared = real_prepare(*args, **kwargs)
        request = kwargs["request"]
        if ArchiveSearchCorpus.MESSAGES in request.corpora:
            kind = "mixed" if len(request.corpora) > 1 else "messages"
            prepared_origins.setdefault(
                kind,
                (prepared, request, kwargs["snapshot_discriminator"]),
            )
        return prepared

    def attest(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["attest"] += 1
        return real_attest(*args, **kwargs)

    def no_network(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("ephemeral benchmark attempted a network connection")

    monkeypatch.setattr(harness_module, "prepare_archive_search_in_transaction", prepare)
    monkeypatch.setattr(harness_module, "attest_archive_search_before_publication", attest)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    second = run_ephemeral()
    assert calls["prepare"] > len(ephemeral_run.cases)
    assert calls["attest"] == len(ephemeral_run.cases)
    assert second.report.to_json() == ephemeral_run.report.to_json()
    assert observations_jsonl(second.observations) == observations_jsonl(ephemeral_run.observations)
    assert frozenset(prepared_origins) == {"messages", "mixed"}
    mismatched_request = synthetic_cases()[0].request
    for prepared, request, release_sha256 in prepared_origins.values():
        foreign_release = "0" * 64 if release_sha256 != "0" * 64 else "1" * 64
        assert prepared.attests_origin(request, release_sha256) is True
        assert prepared.attests_origin(request, foreign_release) is False
        assert prepared.attests_origin(mismatched_request, release_sha256) is False
        assert prepared.attests_origin(object(), release_sha256) is False
        for malformed in ("", " leading-space", "x" * 257, "\ud800"):
            assert prepared.attests_origin(request, malformed) is False
    invalid_prepared, request, release_sha256 = prepared_origins["messages"]
    object.__setattr__(invalid_prepared, "_seal", b"0" * 32)
    assert invalid_prepared.attests_origin(request, release_sha256) is False
