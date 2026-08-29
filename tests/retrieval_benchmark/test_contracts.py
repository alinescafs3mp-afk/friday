from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from friday.retrieval.contracts import (
    CoverageState,
    PassageLocatorKind,
    SearchCorpus,
    SearchLane,
    TextSpanLocator,
)
from friday.retrieval_benchmark._canonical import RecallContractError, parse_canonical_json
from friday.retrieval_benchmark.contracts import (
    RecallAlternativeV1,
    RecallCandidateV1,
    RecallCaseV1,
    RecallCoverageV1,
    RecallObservationV1,
    RecallReportV1,
    RecallTaxonomyV1,
    case_manifest_sha256,
    opaque_passage_window_identity,
)
from friday.retrieval_benchmark.metrics import score_recall
from friday.retrieval_benchmark.synthetic import _DOCUMENTS, _document_passage_ref, synthetic_cases
from tests.retrieval_benchmark.conftest import candidate_for, observation_for


def test_exact_ten_class_taxonomy() -> None:
    assert tuple(item.value for item in RecallTaxonomyV1) == (
        "approximate_content",
        "approximate_date",
        "old_file",
        "pending_file",
        "unhelpful_filename",
        "typo_layout",
        "person_topic",
        "topic_month",
        "message_paraphrase",
        "unknown_corpus",
    )


def test_case_round_trip_and_manifest_are_permutation_invariant() -> None:
    cases = synthetic_cases()
    assert RecallCaseV1.parse(cases[0].to_json()) == cases[0]
    assert case_manifest_sha256(cases) == case_manifest_sha256(reversed(cases))


@pytest.mark.parametrize(
    "value",
    (
        b'{"a":1,"a":1}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":1.0}',
        b'{"b":1,"a":2}',
        b'{ "a":1}',
        b'{"a":"\\u0000"}',
        b'{"a":"\\ud800"}',
    ),
)
def test_canonical_json_rejects_ambiguous_or_unsafe_values(value: bytes) -> None:
    with pytest.raises(RecallContractError):
        parse_canonical_json(value, label="adversarial")


def test_case_rejects_unknown_key(recall_case: RecallCaseV1) -> None:
    payload = recall_case.to_payload()
    payload["query_body"] = "must-not-enter"
    with pytest.raises(RecallContractError):
        RecallCaseV1.parse(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def test_bool_is_not_a_candidate_rank(recall_case: RecallCaseV1) -> None:
    candidate = candidate_for(recall_case)
    payload = candidate.to_payload()
    payload["rank"] = True
    with pytest.raises(RecallContractError):
        RecallCandidateV1.from_payload(payload)


def test_duplicate_alternative_source_is_rejected(recall_case: RecallCaseV1) -> None:
    alternative = recall_case.alternatives[0]
    with pytest.raises(RecallContractError):
        RecallCaseV1(
            recall_case.case_id,
            recall_case.privacy_key_hex,
            recall_case.taxonomy,
            recall_case.evidence_source,
            recall_case.request,
            recall_case.expected_corpus,
            (alternative, alternative),
            False,
        )


def test_duplicate_passage_identity_is_rejected(recall_case: RecallCaseV1) -> None:
    alternative = recall_case.alternatives[0]
    with pytest.raises(RecallContractError):
        RecallAlternativeV1(
            alternative.source_identity,
            (alternative.passage_window_identities[0],) * 2,
            alternative.locator_kind,
            alternative.relevance_grade,
        )


def test_rank_collision_is_rejected(recall_case: RecallCaseV1) -> None:
    first = candidate_for(recall_case, rank=1)
    second = RecallCandidateV1(
        1,
        first.corpus,
        "a" * 64,
        ("b" * 64,),
        first.locator_kinds,
        (),
    )
    with pytest.raises(RecallContractError):
        observation_for(recall_case, candidates=(first, second), complete=True)


def test_context_contracts_are_immutable(recall_case: RecallCaseV1) -> None:
    with pytest.raises(FrozenInstanceError):
        recall_case.case_id = "case.changed"  # type: ignore[misc]


def test_observation_rejects_forged_digest(recall_case: RecallCaseV1) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    payload = observation.to_payload()
    payload["observation_sha256"] = "0" * 64
    with pytest.raises(RecallContractError):
        RecallObservationV1.from_payload(payload)


def test_report_rejects_forged_digest(recall_case: RecallCaseV1) -> None:
    observation = observation_for(
        recall_case,
        candidates=(candidate_for(recall_case),),
        complete=True,
    )
    report = score_recall((recall_case,), (observation,))
    payload = report.to_payload()
    payload["report_sha256"] = "0" * 64
    with pytest.raises(RecallContractError):
        RecallReportV1.from_payload(payload)


def test_body_field_cannot_enter_observation(recall_case: RecallCaseV1) -> None:
    observation = observation_for(recall_case, complete=False)
    payload = observation.to_payload()
    payload["excerpt"] = "privacy sentinel"
    with pytest.raises(RecallContractError):
        RecallObservationV1.from_payload(payload)


def test_coverage_rejects_embedding_state_on_non_dense_lane() -> None:
    with pytest.raises(RecallContractError):
        RecallCoverageV1(
            SearchCorpus.RAW_DOCUMENTS,
            SearchLane.LEXICAL,
            (CoverageState.EMBEDDING_INCOMPATIBLE, CoverageState.PARTIAL),
            None,
            0,
            0,
            0,
            None,
            False,
            True,
            True,
        )


def test_contract_byte_bound_is_enforced(recall_case: RecallCaseV1) -> None:
    with pytest.raises(RecallContractError):
        RecallCaseV1.parse(recall_case.to_json().encode("ascii") + b" " * 300_000)


def test_locator_kind_is_closed() -> None:
    with pytest.raises(RecallContractError):
        RecallAlternativeV1("a" * 64, ("b" * 64,), "text_span", 1)  # type: ignore[arg-type]
    assert PassageLocatorKind.TEXT_SPAN.value == "text_span"


def test_exact_passage_identity_distinguishes_spans_in_one_source() -> None:
    case = synthetic_cases()[0]
    first = _document_passage_ref(_DOCUMENTS[0])
    assert isinstance(first.locator, TextSpanLocator)
    second = replace(first, locator=replace(first.locator, end_char=first.locator.end_char - 1))
    privacy_key = bytes.fromhex(case.privacy_key_hex)
    assert opaque_passage_window_identity(first, privacy_key) != opaque_passage_window_identity(
        second,
        privacy_key,
    )
