from __future__ import annotations

import json
from dataclasses import replace

import pytest

from friday.orchestration.capability_outcome import (
    CapabilityOutcomeError,
    CapabilityOutcomeStatus,
    CompletionGateDecision,
    attach_accepted_capability_outcome_receipt,
    evaluate_read_only_completion,
    load_accepted_capability_outcome_receipt,
)
from friday.orchestration.contracts import RouteClass
from friday.orchestration.simple_public_news_outcome import (
    SIMPLE_PUBLIC_NEWS_EVIDENCE_MARKER,
    SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK,
    SIMPLE_PUBLIC_NEWS_SYNTHESIS_FALLBACK,
    LegacySimplePublicNewsPlan,
    SimplePublicNewsEmptyKind,
    SimplePublicNewsEvidence,
    SimplePublicNewsEvidenceStatus,
    SimplePublicNewsOutcomeError,
    build_simple_public_news_result,
    evaluate_simple_public_news_completion,
    simple_public_news_model_envelope_identity,
    simple_public_news_outcome,
    simple_public_news_source_ledger_identity,
)

REQUEST = "private-query-sentinel news this week"
QUERY = "bounded-query-sentinel"
BODY = "model-evidence-body-sentinel"
ANSWER = "Grounded public-news summary."
SOURCES = [
    {
        "url": "https://public-source-sentinel.example/news",
        "title": "private-title-sentinel",
    }
]


def _plan() -> LegacySimplePublicNewsPlan:
    return LegacySimplePublicNewsPlan.from_request(
        REQUEST,
        QUERY,
        freshness="week",
        source_class="",
        topic_class="",
    )


def _report(*, requested: int = 1, completed: int = 1) -> dict[str, object]:
    report_sources = (
        [
            {
                **SOURCES[0],
                "text": BODY,
                "text_length": len(BODY),
                "status_code": 200,
                "error": "",
                "truncated": False,
            }
        ]
        if completed
        else []
    )
    return {
        "query": QUERY,
        "outbound_attempted": True,
        "freshness": "week",
        "applied_search_filters": {"freshness": "week"},
        "sources": report_sources,
        "requested_sources": requested,
        "completed_sources": completed,
        "failed_sources": 0,
        "timed_out_sources": 0,
        "search_failed": False,
        "search_timed_out": False,
        "refused": False,
        "quota_exhausted": False,
        "error": "",
    }


def _evidence(status: SimplePublicNewsEvidenceStatus) -> SimplePublicNewsEvidence:
    source_bearing = status in {
        SimplePublicNewsEvidenceStatus.SOURCED,
        SimplePublicNewsEvidenceStatus.PARTIAL,
    }
    report = _report() if source_bearing else _report(requested=0, completed=0)
    if status is SimplePublicNewsEvidenceStatus.PARTIAL:
        report["requested_sources"] = 2
        report["failed_sources"] = 1
    return SimplePublicNewsEvidence.from_projection(
        _plan(),
        status=status,
        executed_query=QUERY,
        outbound_attempted=True,
        research_call_count=1,
        report=report if status is not SimplePublicNewsEvidenceStatus.UNAVAILABLE else None,
        model_envelope=BODY if source_bearing else "",
        sources=SOURCES if source_bearing else [],
    )


def _gate(
    evidence: SimplePublicNewsEvidence,
    *,
    content: str,
    model_generated: bool,
    verifier_status: str,
    authority_allowed: bool = True,
) -> tuple[CapabilityOutcomeStatus, CompletionGateDecision, dict[str, object]]:
    source_bearing = authority_allowed and evidence.status in {
        SimplePublicNewsEvidenceStatus.SOURCED,
        SimplePublicNewsEvidenceStatus.PARTIAL,
    }
    ledger, labels = simple_public_news_source_ledger_identity(SOURCES if source_bearing else [])
    envelope = simple_public_news_model_envelope_identity(
        [
            {
                "tool": "web_research",
                "output": BODY,
                "evidence_scope": SIMPLE_PUBLIC_NEWS_EVIDENCE_MARKER,
            }
        ]
        if source_bearing
        else []
    )
    result = build_simple_public_news_result(
        evidence,
        content=content,
        source_ledger_sha256=ledger,
        model_generated=model_generated,
        verifier_status=verifier_status,
        legacy_web_status=(
            "failed"
            if authority_allowed and evidence.status is SimplePublicNewsEvidenceStatus.UNAVAILABLE
            else evidence.status.value
            if authority_allowed
            else "none"
        ),
        authority_allowed=authority_allowed,
    )
    outcome = simple_public_news_outcome(
        _plan(),
        evidence,
        result,
        authority_allowed=authority_allowed,
    )
    decision = evaluate_simple_public_news_completion(
        outcome,
        plan=_plan(),
        evidence=evidence,
        result=result,
        answer=content,
        current_source_ledger_sha256=ledger,
        current_citation_labels=labels,
        current_model_envelope_sha256=envelope,
        verified_content_sha256=(result.content_sha256 if model_generated else None),
        research_call_count=1,
        authority_rechecked=True,
        authority_allowed=authority_allowed,
    )
    metadata: dict[str, object] = {}
    attach_accepted_capability_outcome_receipt(metadata, outcome)
    assert load_accepted_capability_outcome_receipt(metadata, expected_outcome=outcome).outcome == outcome
    return outcome.status, decision, metadata


@pytest.mark.parametrize(
    ("evidence_status", "content", "model_generated", "verifier_status", "outcome_status", "decision"),
    (
        (
            SimplePublicNewsEvidenceStatus.SOURCED,
            ANSWER,
            True,
            "passed",
            CapabilityOutcomeStatus.COMPLETE,
            CompletionGateDecision.READY_TO_PUBLISH,
        ),
        (
            SimplePublicNewsEvidenceStatus.PARTIAL,
            ANSWER,
            True,
            "passed",
            CapabilityOutcomeStatus.PARTIAL,
            CompletionGateDecision.RETURN_PARTIAL,
        ),
        (
            SimplePublicNewsEvidenceStatus.SOURCED,
            SIMPLE_PUBLIC_NEWS_SYNTHESIS_FALLBACK,
            False,
            "skipped",
            CapabilityOutcomeStatus.PARTIAL,
            CompletionGateDecision.RETURN_PARTIAL,
        ),
        (
            SimplePublicNewsEvidenceStatus.EMPTY,
            SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK,
            False,
            "skipped",
            CapabilityOutcomeStatus.EMPTY,
            CompletionGateDecision.RETURN_EMPTY,
        ),
        (
            SimplePublicNewsEvidenceStatus.UNAVAILABLE,
            SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK,
            False,
            "skipped",
            CapabilityOutcomeStatus.UNAVAILABLE,
            CompletionGateDecision.RETURN_UNAVAILABLE,
        ),
    ),
)
def test_news_outcome_matrix_and_private_receipt(
    evidence_status: SimplePublicNewsEvidenceStatus,
    content: str,
    model_generated: bool,
    verifier_status: str,
    outcome_status: CapabilityOutcomeStatus,
    decision: CompletionGateDecision,
) -> None:
    status, actual_decision, metadata = _gate(
        _evidence(evidence_status),
        content=content,
        model_generated=model_generated,
        verifier_status=verifier_status,
    )

    assert (status, actual_decision) == (outcome_status, decision)
    receipt = json.dumps(metadata, ensure_ascii=False)
    for private_value in (REQUEST, QUERY, BODY, SOURCES[0]["url"], SOURCES[0]["title"]):
        assert private_value not in receipt
    assert "raw_id" not in receipt.casefold()
    assert "inbox" not in receipt.casefold()


def test_late_denial_clears_evidence_and_yields_denied_receipt() -> None:
    status, decision, _metadata = _gate(
        _evidence(SimplePublicNewsEvidenceStatus.SOURCED),
        content=SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK,
        model_generated=False,
        verifier_status="unknown",
        authority_allowed=False,
    )
    assert status is CapabilityOutcomeStatus.DENIED
    assert decision is CompletionGateDecision.DENY


def test_receipt_identity_binds_the_final_result_digest() -> None:
    evidence = _evidence(SimplePublicNewsEvidenceStatus.SOURCED)
    ledger, _labels = simple_public_news_source_ledger_identity(SOURCES)
    outcomes = []
    for answer in (ANSWER, ANSWER + " Another supported sentence."):
        result = build_simple_public_news_result(
            evidence,
            content=answer,
            source_ledger_sha256=ledger,
            model_generated=True,
            verifier_status="passed",
            legacy_web_status="sourced",
            authority_allowed=True,
        )
        outcomes.append(simple_public_news_outcome(_plan(), evidence, result, authority_allowed=True))

    assert outcomes[0].evidence_identity_sha256 != outcomes[1].evidence_identity_sha256
    assert outcomes[0].canonical_sha256() != outcomes[1].canonical_sha256()


@pytest.mark.parametrize(
    ("status", "report_mutation", "drop_key", "envelope", "projection_truncated"),
    (
        (
            SimplePublicNewsEvidenceStatus.SOURCED,
            {"requested_sources": 2, "failed_sources": 1},
            "",
            BODY,
            False,
        ),
        (
            SimplePublicNewsEvidenceStatus.SOURCED,
            {"requested_sources": 4, "completed_sources": 4},
            "",
            BODY,
            False,
        ),
        (SimplePublicNewsEvidenceStatus.SOURCED, {}, "failed_sources", BODY, False),
        (SimplePublicNewsEvidenceStatus.SOURCED, {}, "", "", False),
        (SimplePublicNewsEvidenceStatus.SOURCED, {}, "", BODY, True),
        (SimplePublicNewsEvidenceStatus.PARTIAL, {}, "", BODY, False),
    ),
)
def test_source_evidence_factory_rejects_forged_complete_and_undegraded_partial(
    status: SimplePublicNewsEvidenceStatus,
    report_mutation: dict[str, object],
    drop_key: str,
    envelope: str,
    projection_truncated: bool,
) -> None:
    report = _report()
    report.update(report_mutation)
    if drop_key:
        report.pop(drop_key)
    with pytest.raises(SimplePublicNewsOutcomeError):
        SimplePublicNewsEvidence.from_projection(
            _plan(),
            status=status,
            executed_query=QUERY,
            outbound_attempted=True,
            research_call_count=1,
            report=report,
            model_envelope=envelope,
            sources=SOURCES,
            projection_truncated=projection_truncated,
        )


def test_source_evidence_factory_requires_an_attested_report_and_private_seal() -> None:
    with pytest.raises(SimplePublicNewsOutcomeError, match="attested"):
        SimplePublicNewsEvidence.from_projection(
            _plan(),
            status=SimplePublicNewsEvidenceStatus.SOURCED,
            executed_query=QUERY,
            outbound_attempted=True,
            research_call_count=1,
            report=None,
            model_envelope=BODY,
            sources=SOURCES,
        )
    with pytest.raises(TypeError, match="_factory_seal"):
        SimplePublicNewsEvidence(
            plan_sha256=_plan().canonical_sha256(),
            executed_query_sha256=_plan().outbound_query_sha256,
            status=SimplePublicNewsEvidenceStatus.SOURCED,
            outbound_attempted=True,
            research_call_count=1,
            requested_sources=1,
            completed_sources=1,
            failed_sources=0,
            timed_out_sources=0,
            search_timed_out=False,
            topic_filtered_sources=0,
            projection_truncated=False,
            report_incomplete=False,
            empty_kind=SimplePublicNewsEmptyKind.NONE,
            empty_proof_sha256=None,
            model_envelope_sha256="a" * 64,
            source_ledger_sha256="b" * 64,
            citation_labels=("A1",),
        )


@pytest.mark.parametrize(
    ("key", "value"),
    (
        ("outbound_attempted", False),
        ("search_failed", True),
        ("search_timed_out", True),
        ("refused", True),
        ("quota_exhausted", True),
        ("error", "provider failed"),
    ),
)
def test_source_evidence_factory_rejects_provider_failure_claims(
    key: str,
    value: object,
) -> None:
    report = _report()
    report[key] = value
    with pytest.raises(SimplePublicNewsOutcomeError):
        SimplePublicNewsEvidence.from_projection(
            _plan(),
            status=SimplePublicNewsEvidenceStatus.SOURCED,
            executed_query=QUERY,
            outbound_attempted=True,
            research_call_count=1,
            report=report,
            model_envelope=BODY,
            sources=SOURCES,
        )


def test_source_evidence_factory_rejects_non_2xx_and_wrong_source_class() -> None:
    bad_status = _report()
    bad_status["sources"][0]["status_code"] = 500  # type: ignore[index]
    with pytest.raises(SimplePublicNewsOutcomeError, match="row"):
        SimplePublicNewsEvidence.from_projection(
            _plan(),
            status=SimplePublicNewsEvidenceStatus.SOURCED,
            executed_query=QUERY,
            outbound_attempted=True,
            research_call_count=1,
            report=bad_status,
            model_envelope=BODY,
            sources=SOURCES,
        )

    foreign_plan = LegacySimplePublicNewsPlan.from_request(
        REQUEST,
        QUERY,
        freshness="week",
        source_class="foreign",
        topic_class="",
    )
    russian_sources = [{"url": "https://example.ru/news", "title": "Russian source"}]
    wrong_class = _report()
    wrong_class["source_class"] = "foreign"
    wrong_class["sources"][0]["url"] = russian_sources[0]["url"]  # type: ignore[index]
    wrong_class["sources"][0]["title"] = russian_sources[0]["title"]  # type: ignore[index]
    with pytest.raises(SimplePublicNewsOutcomeError, match="row"):
        SimplePublicNewsEvidence.from_projection(
            foreign_plan,
            status=SimplePublicNewsEvidenceStatus.SOURCED,
            executed_query=QUERY,
            outbound_attempted=True,
            research_call_count=1,
            report=wrong_class,
            model_envelope=BODY,
            sources=russian_sources,
        )


@pytest.mark.parametrize("topic_fields", ({}, {"topic_class_satisfied": False}))
def test_source_evidence_factory_requires_code_owned_topic_attestation(
    topic_fields: dict[str, object],
) -> None:
    plan = LegacySimplePublicNewsPlan.from_request(
        REQUEST,
        QUERY,
        freshness="week",
        source_class="foreign",
        topic_class="public_news",
    )
    report = _report()
    report.update(
        {
            "source_class": "foreign",
            "topic_class": "public_news",
            "topic_class_satisfied": True,
            **topic_fields,
        }
    )
    if not topic_fields:
        report.pop("topic_class")
    with pytest.raises(SimplePublicNewsOutcomeError, match="attested"):
        SimplePublicNewsEvidence.from_projection(
            plan,
            status=SimplePublicNewsEvidenceStatus.SOURCED,
            executed_query=QUERY,
            outbound_attempted=True,
            research_call_count=1,
            report=report,
            model_envelope=BODY,
            sources=SOURCES,
        )


def test_source_evidence_factory_keeps_legacy_missing_row_fields_partial() -> None:
    report = _report()
    report["sources"][0].pop("error")  # type: ignore[index]
    evidence = SimplePublicNewsEvidence.from_projection(
        _plan(),
        status=SimplePublicNewsEvidenceStatus.PARTIAL,
        executed_query=QUERY,
        outbound_attempted=True,
        research_call_count=1,
        report=report,
        model_envelope=BODY,
        sources=SOURCES,
    )
    assert evidence.report_incomplete is True


def test_topical_plan_can_retain_an_honest_validated_zero() -> None:
    plan = LegacySimplePublicNewsPlan.from_request(
        REQUEST,
        QUERY,
        freshness="week",
        source_class="foreign",
        topic_class="public_news",
    )
    report = _report(requested=0, completed=0)
    report["source_class"] = "foreign"
    evidence = SimplePublicNewsEvidence.from_projection(
        plan,
        status=SimplePublicNewsEvidenceStatus.EMPTY,
        executed_query=QUERY,
        outbound_attempted=True,
        research_call_count=1,
        report=report,
        model_envelope="",
        sources=[],
    )
    assert evidence.empty_kind is SimplePublicNewsEmptyKind.VALIDATED_ZERO
    assert evidence.topic_filtered_sources == 0


def test_query_binding_and_empty_proof_fail_closed() -> None:
    with pytest.raises(SimplePublicNewsOutcomeError, match="executed news query"):
        SimplePublicNewsEvidence.from_projection(
            _plan(),
            status=SimplePublicNewsEvidenceStatus.UNAVAILABLE,
            executed_query="changed query",
            outbound_attempted=True,
            research_call_count=1,
            report=None,
            model_envelope="",
            sources=[],
        )
    failed = _report(requested=0, completed=0)
    failed["search_failed"] = True
    with pytest.raises(SimplePublicNewsOutcomeError, match="validated complete zero"):
        SimplePublicNewsEvidence.from_projection(
            _plan(),
            status=SimplePublicNewsEvidenceStatus.EMPTY,
            executed_query=QUERY,
            outbound_attempted=True,
            research_call_count=1,
            report=failed,
            model_envelope="",
            sources=[],
        )


@pytest.mark.parametrize(
    "mutation",
    (
        {"failed_sources": 2, "requested_sources": 2},
        {"timed_out_sources": 1, "requested_sources": 2},
        {"requested_sources": 2},
    ),
)
def test_topic_mismatch_empty_proof_requires_exact_all_filtered_counters(
    mutation: dict[str, int],
) -> None:
    plan = LegacySimplePublicNewsPlan.from_request(
        REQUEST,
        QUERY,
        freshness="week",
        source_class="foreign",
        topic_class="public_news",
    )
    report: dict[str, object] = {
        "query": QUERY,
        "outbound_attempted": True,
        "freshness": "week",
        "applied_search_filters": {"freshness": "week"},
        "source_class": "foreign",
        "topic_class": "public_news",
        "topic_class_satisfied": False,
        "sources": [],
        "requested_sources": 1,
        "completed_sources": 0,
        "failed_sources": 1,
        "timed_out_sources": 0,
        "topic_filtered_sources": 1,
        "search_timed_out": False,
        "search_failed": True,
        "error": "topic_mismatch",
    }
    accepted = SimplePublicNewsEvidence.from_projection(
        plan,
        status=SimplePublicNewsEvidenceStatus.EMPTY,
        executed_query=QUERY,
        outbound_attempted=True,
        research_call_count=1,
        report=report,
        model_envelope="",
        sources=[],
    )
    assert accepted.empty_proof_sha256 is not None

    with pytest.raises(SimplePublicNewsOutcomeError, match="validated complete zero"):
        SimplePublicNewsEvidence.from_projection(
            plan,
            status=SimplePublicNewsEvidenceStatus.EMPTY,
            executed_query=QUERY,
            outbound_attempted=True,
            research_call_count=1,
            report={**report, **mutation},
            model_envelope="",
            sources=[],
        )


@pytest.mark.parametrize(
    "mutation",
    (
        {"requested_sources": None},
        {"search_timed_out": True},
        {"topic_filtered_sources": 1},
        {"empty_proof_sha256": "a" * 64},
    ),
)
def test_gate_rejects_forged_retained_zero_empty_evidence(mutation: dict[str, object]) -> None:
    plan = _plan()
    evidence = replace(_evidence(SimplePublicNewsEvidenceStatus.EMPTY), **mutation)
    result = build_simple_public_news_result(
        evidence,
        content=SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK,
        source_ledger_sha256=None,
        model_generated=False,
        verifier_status="skipped",
        legacy_web_status="empty",
        authority_allowed=True,
    )
    outcome = simple_public_news_outcome(plan, evidence, result, authority_allowed=True)

    with pytest.raises(SimplePublicNewsOutcomeError, match="seal"):
        evaluate_simple_public_news_completion(
            outcome,
            plan=plan,
            evidence=evidence,
            result=result,
            answer=SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK,
            current_source_ledger_sha256=None,
            current_citation_labels=(),
            current_model_envelope_sha256=None,
            verified_content_sha256=None,
            research_call_count=1,
            authority_rechecked=True,
            authority_allowed=True,
        )


@pytest.mark.parametrize(
    "mutation",
    (
        {"requested_sources": 2},
        {"failed_sources": 2},
        {"timed_out_sources": 1},
        {"search_timed_out": True},
        {"empty_proof_sha256": "b" * 64},
    ),
)
def test_gate_rejects_forged_retained_topic_empty_evidence(mutation: dict[str, object]) -> None:
    plan = LegacySimplePublicNewsPlan.from_request(
        REQUEST,
        QUERY,
        freshness="week",
        source_class="foreign",
        topic_class="public_news",
    )
    report: dict[str, object] = {
        "query": QUERY,
        "outbound_attempted": True,
        "freshness": "week",
        "applied_search_filters": {"freshness": "week"},
        "source_class": "foreign",
        "topic_class": "public_news",
        "topic_class_satisfied": False,
        "sources": [],
        "requested_sources": 1,
        "completed_sources": 0,
        "failed_sources": 1,
        "timed_out_sources": 0,
        "topic_filtered_sources": 1,
        "search_timed_out": False,
        "search_failed": True,
        "error": "topic_mismatch",
    }
    evidence = SimplePublicNewsEvidence.from_projection(
        plan,
        status=SimplePublicNewsEvidenceStatus.EMPTY,
        executed_query=QUERY,
        outbound_attempted=True,
        research_call_count=1,
        report=report,
        model_envelope="",
        sources=[],
        topic_filtered_sources=1,
    )
    evidence = replace(evidence, **mutation)
    result = build_simple_public_news_result(
        evidence,
        content=SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK,
        source_ledger_sha256=None,
        model_generated=False,
        verifier_status="skipped",
        legacy_web_status="failed",
        authority_allowed=True,
    )
    outcome = simple_public_news_outcome(plan, evidence, result, authority_allowed=True)

    with pytest.raises(SimplePublicNewsOutcomeError, match="seal"):
        evaluate_simple_public_news_completion(
            outcome,
            plan=plan,
            evidence=evidence,
            result=result,
            answer=SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK,
            current_source_ledger_sha256=None,
            current_citation_labels=(),
            current_model_envelope_sha256=None,
            verified_content_sha256=None,
            research_call_count=1,
            authority_rechecked=True,
            authority_allowed=True,
        )


def test_news_gate_rejects_tampered_final_bindings() -> None:
    evidence = _evidence(SimplePublicNewsEvidenceStatus.SOURCED)
    ledger, labels = simple_public_news_source_ledger_identity(SOURCES)
    result = build_simple_public_news_result(
        evidence,
        content=ANSWER,
        source_ledger_sha256=ledger,
        model_generated=True,
        verifier_status="passed",
        legacy_web_status="sourced",
        authority_allowed=True,
    )
    outcome = simple_public_news_outcome(_plan(), evidence, result, authority_allowed=True)
    base = {
        "plan": _plan(),
        "evidence": evidence,
        "result": result,
        "answer": ANSWER,
        "current_source_ledger_sha256": ledger,
        "current_citation_labels": labels,
        "current_model_envelope_sha256": evidence.model_envelope_sha256,
        "verified_content_sha256": result.content_sha256,
        "research_call_count": 1,
        "authority_rechecked": True,
        "authority_allowed": True,
    }
    for mutation in (
        {"answer": ANSWER + " changed"},
        {"current_source_ledger_sha256": "f" * 64},
        {"current_model_envelope_sha256": "e" * 64},
        {"current_citation_labels": ("A2",)},
        {"verified_content_sha256": "a" * 64},
        {"research_call_count": 0},
        {"authority_rechecked": False},
        {"evidence": replace(evidence, plan_sha256="d" * 64)},
        {"evidence": replace(evidence, executed_query_sha256="f" * 64)},
    ):
        with pytest.raises(SimplePublicNewsOutcomeError):
            evaluate_simple_public_news_completion(outcome, **{**base, **mutation})


def test_file_completion_gate_still_rejects_web_outcome() -> None:
    evidence = _evidence(SimplePublicNewsEvidenceStatus.SOURCED)
    ledger, _labels = simple_public_news_source_ledger_identity(SOURCES)
    result = build_simple_public_news_result(
        evidence,
        content=ANSWER,
        source_ledger_sha256=ledger,
        model_generated=True,
        verifier_status="passed",
        legacy_web_status="sourced",
        authority_allowed=True,
    )
    outcome = simple_public_news_outcome(_plan(), evidence, result, authority_allowed=True)
    assert outcome.route is RouteClass.WEB_READ
    with pytest.raises(CapabilityOutcomeError, match="route binding"):
        evaluate_read_only_completion(
            outcome,
            expected_route=RouteClass.WEB_READ,
            expected_plan_sha256=outcome.plan_sha256,
            expected_evidence_identity_sha256=outcome.evidence_identity_sha256,
            expected_citation_labels=outcome.citation_labels,
            answer=ANSWER,
            authority_rechecked=True,
            verification_passed=True,
        )
