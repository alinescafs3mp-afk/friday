from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from friday.retrieval_benchmark._canonical import RecallContractError, canonical_json
from friday.retrieval_benchmark.cutover_readiness import (
    CUTOVER_REPORT_SCHEMA,
    HISTORICAL_FAILURE_GROUPS,
    CutoverCaseV1,
    CutoverContour,
    CutoverEvidenceStatus,
    CutoverReadinessError,
    CutoverReadinessReportV1,
    HistoricalFailureClass,
    HistoricalFailureGroupV1,
    build_cutover_readiness_report,
)
from friday.retrieval_benchmark.parity import ParityReportV1, run_parity_ephemeral


@pytest.fixture(scope="module")
def report() -> CutoverReadinessReportV1:
    return build_cutover_readiness_report(run_parity_ephemeral())


def _message_case() -> CutoverCaseV1:
    return CutoverCaseV1(
        contour=CutoverContour.MESSAGE_WINDOW,
        status=CutoverEvidenceStatus.CONTRACT_ONLY,
        evidence_codes=("closed_contract",),
        blocker_codes=("runtime_not_wired",),
        required_shared_files=("friday/server.py",),
        executable_nodes=(
            "tests/test_message_exact_internal.py::test_queryless_current_scope_preserves_message_and_reply_identity",
        ),
        binding_sha256s=("a" * 64,),
    )


def _recreate_report(
    report: CutoverReadinessReportV1,
    cases: tuple[CutoverCaseV1, ...],
) -> CutoverReadinessReportV1:
    return CutoverReadinessReportV1.create(
        archive_release_sha256=report.archive_release_sha256,
        archive_parity_report_sha256=report.archive_parity_report_sha256,
        message_adapter_binding_sha256=report.message_adapter_binding_sha256,
        memory_adapter_binding_sha256=report.memory_adapter_binding_sha256,
        cases=cases,
    )


def test_ready_status_cannot_retain_a_blocker_or_shared_file() -> None:
    with pytest.raises(CutoverReadinessError):
        replace(_message_case(), status=CutoverEvidenceStatus.PARITY)


def test_incomplete_status_requires_both_blocker_and_exact_file() -> None:
    with pytest.raises(CutoverReadinessError):
        replace(_message_case(), blocker_codes=())
    with pytest.raises(CutoverReadinessError):
        replace(_message_case(), required_shared_files=())


@pytest.mark.parametrize("value", ["", None, {}, False, 0])
def test_ready_case_rejects_falsey_noncollection_blockers(value: object) -> None:
    ready = replace(
        _message_case(),
        status=CutoverEvidenceStatus.PARITY,
        blocker_codes=(),
        required_shared_files=(),
    )
    with pytest.raises(CutoverReadinessError):
        replace(ready, blocker_codes=value)  # type: ignore[arg-type]

    payload = ready.to_payload()
    payload["blocker_codes"] = value
    with pytest.raises(CutoverReadinessError):
        CutoverCaseV1.from_payload(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence_codes", ("UPPER",)),
        ("blocker_codes", ("not sorted",)),
        ("required_shared_files", ("/home/private.py",)),
        ("required_shared_files", ("friday/../private.py",)),
        ("required_shared_files", ("friday/./server.py",)),
        ("required_shared_files", ("friday//server.py",)),
        ("executable_nodes", ("tests/test_x.py",)),
        ("executable_nodes", ("/home/tests/test_x.py::test_x",)),
        ("executable_nodes", ("tests/./test_x.py::test_x",)),
        ("binding_sha256s", ("0" * 63,)),
    ],
)
def test_case_rejects_open_tokens_paths_nodes_and_digests(field: str, value: object) -> None:
    with pytest.raises((CutoverReadinessError, RecallContractError)):
        replace(_message_case(), **{field: value})


def test_case_rejects_duplicate_and_oversized_collections() -> None:
    with pytest.raises(CutoverReadinessError):
        replace(_message_case(), evidence_codes=["closed_contract"])  # type: ignore[arg-type]
    with pytest.raises(CutoverReadinessError):
        replace(_message_case(), blocker_codes=("same", "same"))
    with pytest.raises(CutoverReadinessError):
        replace(_message_case(), evidence_codes=tuple(f"evidence_{index:02d}" for index in range(33)))
    with pytest.raises(CutoverReadinessError):
        replace(
            _message_case(),
            executable_nodes=(f"tests/test_x.py::test_{'x' * 500}",),
        )


def test_history_group_rejects_bad_count_path_enum_and_open_requirements() -> None:
    group = HISTORICAL_FAILURE_GROUPS[0]
    with pytest.raises(CutoverReadinessError):
        replace(group, closed_foundations=["r8a"])  # type: ignore[arg-type]
    with pytest.raises(CutoverReadinessError):
        replace(group, failed_nodes=0)
    with pytest.raises(CutoverReadinessError):
        replace(group, test_file="/home/private.py")
    with pytest.raises(CutoverReadinessError):
        replace(group, failure_class="message_window")  # type: ignore[arg-type]
    with pytest.raises(CutoverReadinessError):
        replace(group, remaining_requirement_codes=("contains whitespace",))


def test_report_rejects_missing_duplicate_and_noncanonical_contours(
    report: CutoverReadinessReportV1,
) -> None:
    with pytest.raises(CutoverReadinessError):
        replace(report, cases=report.cases[:-1])
    with pytest.raises(CutoverReadinessError):
        replace(report, cases=report.cases[:-1] + (report.cases[0],))
    with pytest.raises(CutoverReadinessError):
        replace(report, cases=tuple(reversed(report.cases)))


def test_report_rejects_historical_count_substitution(
    report: CutoverReadinessReportV1,
) -> None:
    first = replace(HISTORICAL_FAILURE_GROUPS[0], failed_nodes=5)
    groups = (first, *HISTORICAL_FAILURE_GROUPS[1:])
    with pytest.raises(CutoverReadinessError):
        replace(report, historical_failure_groups=groups)


def test_report_rejects_same_total_historical_classification_substitution(
    report: CutoverReadinessReportV1,
) -> None:
    first = replace(HISTORICAL_FAILURE_GROUPS[0], failed_nodes=5)
    second = replace(HISTORICAL_FAILURE_GROUPS[1], failed_nodes=6)
    groups = (first, second, *HISTORICAL_FAILURE_GROUPS[2:])

    with pytest.raises(CutoverReadinessError):
        replace(report, historical_failure_groups=groups)


def test_report_rejects_digest_and_binding_substitution(
    report: CutoverReadinessReportV1,
) -> None:
    with pytest.raises(CutoverReadinessError):
        replace(report, report_sha256="0" * 64)
    with pytest.raises(CutoverReadinessError):
        replace(report, message_adapter_binding_sha256="0" * 64)
    with pytest.raises(CutoverReadinessError):
        replace(report, archive_release_sha256="0" * 64)


def test_report_rejects_recomputed_open_vocabulary_and_case_binding(
    report: CutoverReadinessReportV1,
) -> None:
    open_case = replace(report.cases[0], evidence_codes=("customersecret123",))
    with pytest.raises(CutoverReadinessError):
        _recreate_report(report, (open_case, *report.cases[1:]))

    message_index = next(
        index for index, case in enumerate(report.cases) if case.contour is CutoverContour.MESSAGE_WINDOW
    )
    forged_message = replace(report.cases[message_index], binding_sha256s=("f" * 64,))
    forged_cases = list(report.cases)
    forged_cases[message_index] = forged_message
    with pytest.raises(CutoverReadinessError):
        _recreate_report(report, tuple(forged_cases))


def test_parser_rejects_duplicate_keys_nonfinite_values_and_noncanonical_json(
    report: CutoverReadinessReportV1,
) -> None:
    serialized = report.to_json()
    duplicate = serialized.replace(
        f'"schema":"{CUTOVER_REPORT_SCHEMA}"',
        f'"schema":"{CUTOVER_REPORT_SCHEMA}","schema":"{CUTOVER_REPORT_SCHEMA}"',
        1,
    )
    nonfinite = serialized.replace('"historical_failure_count":60', '"historical_failure_count":NaN')
    spaced = json.dumps(report.to_payload(), ensure_ascii=True, sort_keys=True)

    with pytest.raises(RecallContractError):
        CutoverReadinessReportV1.parse(duplicate)
    with pytest.raises(RecallContractError):
        CutoverReadinessReportV1.parse(nonfinite)
    with pytest.raises(RecallContractError):
        CutoverReadinessReportV1.parse(spaced)


def test_parser_rejects_unknown_keys_enums_and_forged_ready_flag(
    report: CutoverReadinessReportV1,
) -> None:
    payload = report.to_payload()
    payload["extra"] = "forbidden"
    with pytest.raises(RecallContractError):
        CutoverReadinessReportV1.from_payload(payload)

    payload = report.to_payload()
    cases = payload["cases"]
    assert isinstance(cases, list) and isinstance(cases[0], dict)
    cases[0]["status"] = "almost_ready"
    with pytest.raises(CutoverReadinessError):
        CutoverReadinessReportV1.from_payload(payload)

    payload = report.to_payload()
    payload["cutover_ready"] = False
    with pytest.raises(CutoverReadinessError):
        CutoverReadinessReportV1.from_payload(payload)

    payload = report.to_payload()
    payload["memory_foundation_review_status"] = "integrated"
    with pytest.raises(CutoverReadinessError):
        CutoverReadinessReportV1.from_payload(payload)


def test_parser_rejects_absolute_shared_path_before_digest_check(
    report: CutoverReadinessReportV1,
) -> None:
    payload = report.to_payload()
    cases = payload["cases"]
    assert isinstance(cases, list) and isinstance(cases[0], dict)
    cases[0]["required_shared_files"] = ["/home/jericho/private.py"]

    with pytest.raises(CutoverReadinessError):
        CutoverReadinessReportV1.from_payload(payload)


def test_history_parser_rejects_unknown_schema_and_bool_count() -> None:
    payload = HISTORICAL_FAILURE_GROUPS[0].to_payload()
    payload["schema"] = "future"
    with pytest.raises(CutoverReadinessError):
        HistoricalFailureGroupV1.from_payload(payload)

    payload = HISTORICAL_FAILURE_GROUPS[0].to_payload()
    payload["failed_nodes"] = True
    with pytest.raises(CutoverReadinessError):
        HistoricalFailureGroupV1.from_payload(payload)


def test_case_parser_rejects_forged_ready_and_open_schema() -> None:
    payload = _message_case().to_payload()
    payload["ready"] = True
    with pytest.raises(CutoverReadinessError):
        CutoverCaseV1.from_payload(payload)

    payload = _message_case().to_payload()
    payload["schema"] = "future"
    with pytest.raises(CutoverReadinessError):
        CutoverCaseV1.from_payload(payload)


def test_build_rejects_duck_typed_or_untrusted_parity_reports(report: CutoverReadinessReportV1) -> None:
    with pytest.raises(CutoverReadinessError):
        build_cutover_readiness_report(object())  # type: ignore[arg-type]
    with pytest.raises(CutoverReadinessError):
        build_cutover_readiness_report(report)  # type: ignore[arg-type]


def test_build_rejects_same_type_alternate_parity_release_manifest_and_measurement() -> None:
    parity = run_parity_ephemeral()
    alternate_release = ParityReportV1.create(
        release_sha256="0" * 64,
        cases=parity.cases,
        dimensions=parity.dimensions,
    )
    with pytest.raises(CutoverReadinessError):
        build_cutover_readiness_report(alternate_release)

    changed_adapter = "message_search" if parity.cases[0].adapter != "message_search" else "source_search"
    alternate_measurement = ParityReportV1.create(
        release_sha256=parity.release_sha256,
        cases=(replace(parity.cases[0], adapter=changed_adapter), *parity.cases[1:]),  # type: ignore[arg-type]
        dimensions=parity.dimensions,
    )
    with pytest.raises(CutoverReadinessError):
        build_cutover_readiness_report(alternate_measurement)

    alternate_manifest = ParityReportV1.create(
        release_sha256=parity.release_sha256,
        cases=(replace(parity.cases[0], case_id="0" * 64), *parity.cases[1:]),
        dimensions=parity.dimensions,
    )
    with pytest.raises(CutoverReadinessError):
        build_cutover_readiness_report(alternate_manifest)


def test_payload_copy_cannot_mutate_the_frozen_report(report: CutoverReadinessReportV1) -> None:
    payload = report.to_payload()
    cases = payload["cases"]
    groups = payload["historical_failure_groups"]
    assert isinstance(cases, list) and isinstance(cases[0], dict)
    assert isinstance(groups, list) and isinstance(groups[0], dict)
    cases[0]["evidence_codes"] = ["forged"]
    groups[0]["failed_nodes"] = 60

    assert report.to_json() != canonical_json(payload)
    assert report == CutoverReadinessReportV1.parse(report.to_json())


def test_report_digest_is_not_plain_payload_sha256(report: CutoverReadinessReportV1) -> None:
    assert (
        report.report_sha256
        != hashlib.sha256(
            canonical_json(report._payload_without_digest()).encode("ascii")  # noqa: SLF001
        ).hexdigest()
    )


def test_closed_history_classification_has_no_unclassified_bucket() -> None:
    assert set(HistoricalFailureClass) == {
        HistoricalFailureClass.CLASSIFIER_JSON,
        HistoricalFailureClass.LEGACY_ADAPTER_RETIREMENT,
        HistoricalFailureClass.SMALL_TALK_CATALOG,
        HistoricalFailureClass.TOOL_TURN_ADMISSION,
        HistoricalFailureClass.TRANSPORT_CAPABILITY,
    }
