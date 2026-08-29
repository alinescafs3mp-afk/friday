from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import FrozenInstanceError

import pytest

from friday.retrieval_benchmark._canonical import canonical_json
from friday.retrieval_benchmark.cli import build_parser
from friday.retrieval_benchmark.contracts import case_manifest_sha256
from friday.retrieval_benchmark.parity import (
    PARITY_REPORT_SCHEMA,
    UNSUPPORTED_REASON_CODES,
    ParityReportV1,
    run_parity_ephemeral,
)
from friday.retrieval_benchmark.synthetic import synthetic_cases

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PRIVATE_KEYS = {
    "actor_id",
    "body",
    "content",
    "excerpt",
    "path",
    "person_id",
    "principal_id",
    "query",
    "raw_id",
    "tenant_id",
}
_PRIVATE_SENTINELS = (
    "recall-parity-tenant",
    "recall-parity-principal",
    "raw_a100000000000001",
    "raw_a100000000000004",
    "raw_a100000000000002",
    "raw_a100000000000003",
    "ko_a100000000000001",
    "conv_a100000000000001",
    "conv_a100000000000002",
    "conv_a100000000000003",
    "conv_a100000000000004",
    "conv_a100000000000005",
    "msg_a100000000000001",
    "msg_a100000000000002",
    "msg_a100000000000003",
    "msg_a100000000000004",
    "msg_a100000000000005",
    "msg_a100000000000006",
    "saffronneedle",
    "quartzpendingneedle",
    "cobaltpromotedneedle",
    "lanternmessageneedle",
    "Uhfabr lt;ehcnd",
    "фдзрф иуеф",
    "График дежурств на август утверждён.",
    "/home/",
)


@pytest.fixture(scope="module")
def parity_reports() -> tuple[ParityReportV1, ParityReportV1]:
    return run_parity_ephemeral(), run_parity_ephemeral()


def _keys(value: object) -> set[str]:
    if type(value) is dict:
        mapping = value
        return set(mapping) | set().union(*(_keys(item) for item in mapping.values()))
    if type(value) is list:
        return set().union(*(_keys(item) for item in value))
    return set()


def test_parity_report_is_canonical_deterministic_and_body_free(
    parity_reports: tuple[ParityReportV1, ParityReportV1],
) -> None:
    first, second = parity_reports
    serialized = first.to_json()
    payload = first.to_payload()

    assert serialized == second.to_json()
    assert serialized == canonical_json(payload)
    assert serialized.encode("ascii").decode("ascii") == serialized
    assert json.loads(serialized) == payload
    assert payload["schema"] == PARITY_REPORT_SCHEMA
    assert payload["case_count"] == 6
    assert payload["identity_kind"] == "deterministic_synthetic_pseudonym_v1"
    assert not (_keys(payload) & _PRIVATE_KEYS)
    for sentinel in _PRIVATE_SENTINELS:
        assert sentinel not in serialized
        assert json.dumps(sentinel, ensure_ascii=True)[1:-1] not in serialized


def test_parity_matrix_uses_only_synthetic_pseudonyms_and_aggregate_facts(
    parity_reports: tuple[ParityReportV1, ParityReportV1],
) -> None:
    report, _second = parity_reports
    cases = report.cases

    assert Counter(item.expected_corpus.value for item in cases) == {
        "documents": 2,
        "knowledge": 1,
        "messages": 3,
    }
    assert Counter(item.adapter for item in cases) == {
        "memory_search": 1,
        "message_search": 3,
        "source_search": 2,
    }
    for item in cases:
        assert _DIGEST.fullmatch(item.case_id)
        assert _DIGEST.fullmatch(item.case_sha256)
        assert _DIGEST.fullmatch(item.expected_source_identity)
        assert all(_DIGEST.fullmatch(value) for value in item.archive_source_identities)
        assert all(_DIGEST.fullmatch(value) for value in item.archive_publication_source_identities)
        assert all(_DIGEST.fullmatch(value) for value in item.adapter_source_identities)
        assert set(item.archive_source_identities) == set(item.adapter_source_identities)
        assert item.membership_status == "parity"
        assert item.reason_code is None

    multi_hit_cases = tuple(item for item in cases if len(item.archive_source_identities) > 1)
    assert len(multi_hit_cases) >= 3
    publication_exclusions = tuple(
        item
        for item in cases
        if item.expected_source_identity not in item.archive_publication_source_identities
    )
    assert len(publication_exclusions) == 1
    pending = publication_exclusions[0]
    assert pending.adapter == "source_search"
    assert pending.expected_corpus.value == "documents"
    assert pending.expected_source_identity in pending.archive_source_identities
    assert pending.archive_publication_source_identities == tuple(
        source_identity
        for source_identity in pending.archive_source_identities
        if source_identity != pending.expected_source_identity
    )
    for canonical in cases:
        if canonical is not pending:
            assert canonical.archive_publication_source_identities == canonical.archive_source_identities

    dimensions = {item.name: item for item in report.dimensions}
    assert dimensions["candidate_membership"].status == "parity"
    assert dimensions["candidate_membership"].matched == 6
    assert dimensions["candidate_order"].status == "mismatch"
    assert dimensions["candidate_order"].matched == 5
    assert dimensions["candidate_order"].mismatched == 1
    order_mismatches = tuple(item for item in cases if item.order_status == "mismatch")
    assert len(order_mismatches) == 1
    assert order_mismatches[0].adapter == "message_search"
    assert order_mismatches[0].archive_expected_rank == 2
    assert order_mismatches[0].adapter_expected_rank == 1


def test_unsupported_dimensions_are_explicit_and_never_claim_parity(
    parity_reports: tuple[ParityReportV1, ParityReportV1],
) -> None:
    report, _second = parity_reports
    by_name = {item.name: item for item in report.dimensions}

    for name, reason in UNSUPPORTED_REASON_CODES.items():
        dimension = by_name[name]
        assert dimension.status == "unsupported"
        assert dimension.compared == 0
        assert dimension.matched == 0
        assert dimension.mismatched == 0
        assert dimension.reason_codes == (reason,)


def test_parity_contract_is_immutable_and_cli_is_separate(
    parity_reports: tuple[ParityReportV1, ParityReportV1],
) -> None:
    report, _second = parity_reports
    with pytest.raises(FrozenInstanceError):
        report.report_sha256 = "0" * 64  # type: ignore[misc]

    args = build_parser().parse_args(["run-parity-ephemeral"])
    assert args.handler.__name__ == "_run_parity_ephemeral"


def test_existing_recall_manifest_remains_the_21_case_contract() -> None:
    cases = synthetic_cases()

    assert len(cases) == 21
    assert case_manifest_sha256(cases) == case_manifest_sha256(tuple(cases))
