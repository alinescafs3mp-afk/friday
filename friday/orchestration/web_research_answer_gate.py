"""Pure admission gate for public-web answer publication.

The gate composes already-built readiness and citation-coverage facts.  It
does not fetch, synthesize, invent, store, or wire an answer.  Invalid facts,
identity mismatches, and incomplete coverage fail closed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.web_citation_coverage import (
    WebCitationCoverageReason,
    WebCitationCoverageState,
    WebCitationCoverageV1,
)
from friday.orchestration.web_research_readiness import (
    WebResearchReadinessState,
    WebResearchReadinessV1,
    build_web_research_readiness,
)

WEB_RESEARCH_ANSWER_GATE_SCHEMA = "friday.web-research-answer-gate.v1"
MAX_GATE_ID_CHARS = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class WebResearchAnswerGateError(ValueError):
    """A gate value is outside the closed answer-admission contract."""


class WebResearchAnswerAdmission(StrEnum):
    """Closed publication-admission outcomes."""

    ADMITTED = "admitted"
    ADMITTED_DEGRADED = "admitted_degraded"
    HOLD = "hold"
    BLOCKED = "blocked"


class WebResearchAnswerGateReason(StrEnum):
    """Closed explanation for one answer-admission outcome."""

    READY_COMPLETE_COVERAGE = "ready_complete_coverage"
    READY_DEGRADED_READINESS = "ready_degraded_readiness"
    PARTIAL_COVERAGE = "partial_coverage"
    READINESS_NOT_READY = "readiness_not_ready"
    COVERAGE_EMPTY = "coverage_empty"
    COVERAGE_BLOCKED_PRIVATE = "coverage_blocked_private"
    IDENTITY_MISMATCH = "identity_mismatch"
    INPUTS_INVALID = "inputs_invalid"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise WebResearchAnswerGateError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _admission(value: object) -> WebResearchAnswerAdmission:
    try:
        return WebResearchAnswerAdmission(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise WebResearchAnswerGateError("admission_closed") from exc


def _reason(value: object) -> WebResearchAnswerGateReason:
    try:
        return WebResearchAnswerGateReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise WebResearchAnswerGateError("reason_closed") from exc


@dataclass(frozen=True, slots=True)
class WebResearchAnswerGateV1:
    """Immutable gate result with both input identities or none."""

    gate_id: str
    authenticated_turn_id: str | None
    readiness_id: str | None
    coverage_id: str | None
    admission: WebResearchAnswerAdmission
    reason: WebResearchAnswerGateReason

    def __post_init__(self) -> None:
        _identifier(self.gate_id, field="gate_id")
        ids = (self.readiness_id, self.coverage_id)
        if any(value is not None for value in ids):
            if any(value is None for value in ids) or self.authenticated_turn_id is None:
                _fail("input_identities", "all_or_none")
            _identifier(self.readiness_id, field="readiness_id")
            _identifier(self.coverage_id, field="coverage_id")
            _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        elif self.authenticated_turn_id is not None:
            _fail("input_identities", "all_or_none")
        admission = _admission(self.admission)
        reason = _reason(self.reason)
        object.__setattr__(self, "admission", admission)
        object.__setattr__(self, "reason", reason)
        if (
            admission is WebResearchAnswerAdmission.ADMITTED
            and reason is not WebResearchAnswerGateReason.READY_COMPLETE_COVERAGE
        ):
            _fail("admission_reason", "inconsistent")
        if admission is WebResearchAnswerAdmission.ADMITTED_DEGRADED and reason not in {
            WebResearchAnswerGateReason.READY_DEGRADED_READINESS,
            WebResearchAnswerGateReason.PARTIAL_COVERAGE,
        }:
            _fail("admission_reason", "inconsistent")
        if admission in {
            WebResearchAnswerAdmission.ADMITTED,
            WebResearchAnswerAdmission.ADMITTED_DEGRADED,
        } and any(value is None for value in ids):
            _fail("admission", "missing_identities")

    @property
    def state(self) -> WebResearchAnswerAdmission:
        return self.admission

    @property
    def closed_admission(self) -> WebResearchAnswerAdmission:
        return self.admission

    @property
    def decision(self) -> WebResearchAnswerAdmission:
        return self.admission

    @property
    def closed_reason(self) -> WebResearchAnswerGateReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": WEB_RESEARCH_ANSWER_GATE_SCHEMA,
            "gate_id": self.gate_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "readiness_id": self.readiness_id,
            "coverage_id": self.coverage_id,
            "admission": self.admission.value,
            "reason": self.reason.value,
        }


AnswerAdmission = WebResearchAnswerAdmission
AnswerGateReason = WebResearchAnswerGateReason
WebResearchAnswerGate = WebResearchAnswerGateV1


def _coerce_readiness(value: object) -> WebResearchReadinessV1 | None:
    try:
        result = value if isinstance(value, WebResearchReadinessV1) else build_web_research_readiness(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result


def _coerce_coverage(value: object) -> WebCitationCoverageV1 | None:
    if isinstance(value, WebCitationCoverageV1):
        return value
    if not isinstance(value, Mapping):
        return None
    try:
        allowed = {
            "schema",
            "coverage_id",
            "authenticated_turn_id",
            "coverage",
            "state",
            "cited_host_count",
            "admitted_host_count",
            "reason",
        }
        if set(value) - allowed:
            return None
        coverage = value.get("coverage", value.get("state"))
        return WebCitationCoverageV1(
            coverage_id=cast(str, value.get("coverage_id")),
            authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
            coverage=cast(WebCitationCoverageState, coverage),
            cited_host_count=cast(int, value.get("cited_host_count")),
            admitted_host_count=cast(int, value.get("admitted_host_count")),
            reason=cast(WebCitationCoverageReason, value.get("reason")),
        )
    except (TypeError, ValueError):
        return None


def _not_ready_gate(gate_id: str, reason: WebResearchAnswerGateReason) -> WebResearchAnswerGateV1:
    return WebResearchAnswerGateV1(
        gate_id=gate_id,
        authenticated_turn_id=None,
        readiness_id=None,
        coverage_id=None,
        admission=WebResearchAnswerAdmission.HOLD
        if reason is not WebResearchAnswerGateReason.COVERAGE_BLOCKED_PRIVATE
        else WebResearchAnswerAdmission.BLOCKED,
        reason=reason,
    )


def _admit_gate(
    gate_id: str,
    readiness: WebResearchReadinessV1,
    coverage: WebCitationCoverageV1,
    admission: WebResearchAnswerAdmission,
    reason: WebResearchAnswerGateReason,
) -> WebResearchAnswerGateV1:
    return WebResearchAnswerGateV1(
        gate_id=gate_id,
        authenticated_turn_id=readiness.authenticated_turn_id,
        readiness_id=readiness.readiness_id,
        coverage_id=coverage.coverage_id,
        admission=admission,
        reason=reason,
    )


def _known_mapping_keys(raw: Mapping[str, Any]) -> bool:
    return not (
        set(raw)
        - {
            "schema",
            "gate_id",
            "readiness",
            "research_readiness",
            "readiness_facts",
            "coverage",
            "citation_coverage",
            "coverage_facts",
            "authenticated_turn_id",
            "admission",
            "state",
            "readiness_id",
            "coverage_id",
            "reason",
        }
    )


def build_web_research_answer_gate(
    gate_id: str | Mapping[str, Any],
    readiness: object = None,
    coverage: object = None,
    *,
    authenticated_turn_id: object = None,
    citation_coverage: object = None,
) -> WebResearchAnswerGateV1:
    """Compose readiness and citation coverage into one closed gate result."""

    if citation_coverage is not None:
        if coverage is not None:
            _fail("coverage", "duplicate_arguments")
        coverage = citation_coverage
    if isinstance(gate_id, Mapping):
        raw = gate_id
        if not _known_mapping_keys(raw):
            _fail("gate", "unknown_fields")
        if raw.get("schema", WEB_RESEARCH_ANSWER_GATE_SCHEMA) != WEB_RESEARCH_ANSWER_GATE_SCHEMA:
            _fail("schema")
        output_keys = {"admission", "state", "readiness_id", "coverage_id", "reason"}
        fact_keys = {
            "readiness",
            "research_readiness",
            "readiness_facts",
            "coverage",
            "citation_coverage",
            "coverage_facts",
        }
        if output_keys.intersection(raw) and fact_keys.intersection(raw):
            _fail("gate", "duplicate_representations")
        if output_keys.intersection(raw):
            return WebResearchAnswerGateV1(
                gate_id=cast(str, raw.get("gate_id")),
                authenticated_turn_id=cast(str | None, raw.get("authenticated_turn_id")),
                readiness_id=cast(str | None, raw.get("readiness_id")),
                coverage_id=cast(str | None, raw.get("coverage_id")),
                admission=cast(WebResearchAnswerAdmission, raw.get("admission", raw.get("state"))),
                reason=cast(WebResearchAnswerGateReason, raw.get("reason")),
            )
        gate_id = cast(str, raw.get("gate_id"))
        authenticated_turn_id = raw.get("authenticated_turn_id", authenticated_turn_id)
        readiness = raw.get("readiness", raw.get("research_readiness", raw.get("readiness_facts")))
        coverage = raw.get("coverage", raw.get("citation_coverage", raw.get("coverage_facts")))

    gate_key = _identifier(gate_id, field="gate_id")
    readiness_value = _coerce_readiness(readiness)
    coverage_value = _coerce_coverage(coverage)
    if readiness_value is None or coverage_value is None:
        return _not_ready_gate(gate_key, WebResearchAnswerGateReason.INPUTS_INVALID)
    if coverage_value.coverage is WebCitationCoverageState.BLOCKED_PRIVATE:
        return _not_ready_gate(gate_key, WebResearchAnswerGateReason.COVERAGE_BLOCKED_PRIVATE)
    if readiness_value.readiness is WebResearchReadinessState.NOT_READY:
        return _not_ready_gate(gate_key, WebResearchAnswerGateReason.READINESS_NOT_READY)
    expected_turn = readiness_value.authenticated_turn_id
    if coverage_value.authenticated_turn_id != expected_turn or (
        authenticated_turn_id is not None and authenticated_turn_id != expected_turn
    ):
        return _not_ready_gate(gate_key, WebResearchAnswerGateReason.IDENTITY_MISMATCH)

    if coverage_value.coverage is WebCitationCoverageState.EMPTY:
        return _not_ready_gate(gate_key, WebResearchAnswerGateReason.COVERAGE_EMPTY)
    if (
        readiness_value.readiness is WebResearchReadinessState.READY
        and coverage_value.coverage is WebCitationCoverageState.COMPLETE
    ):
        return _admit_gate(
            gate_key,
            readiness_value,
            coverage_value,
            WebResearchAnswerAdmission.ADMITTED,
            WebResearchAnswerGateReason.READY_COMPLETE_COVERAGE,
        )
    if readiness_value.readiness is WebResearchReadinessState.READY_DEGRADED:
        reason = WebResearchAnswerGateReason.READY_DEGRADED_READINESS
    else:
        reason = WebResearchAnswerGateReason.PARTIAL_COVERAGE
    return _admit_gate(
        gate_key,
        readiness_value,
        coverage_value,
        WebResearchAnswerAdmission.ADMITTED_DEGRADED,
        reason,
    )


def validate_web_research_answer_gate(value: object) -> bool:
    """Return whether a gate or mapping is a valid frozen result."""

    try:
        if isinstance(value, WebResearchAnswerGateV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping) or not _known_mapping_keys(value):
            return False
        if value.get("schema", WEB_RESEARCH_ANSWER_GATE_SCHEMA) != WEB_RESEARCH_ANSWER_GATE_SCHEMA:
            return False
        return (
            WebResearchAnswerGateV1(
                gate_id=cast(str, value.get("gate_id")),
                authenticated_turn_id=cast(str | None, value.get("authenticated_turn_id")),
                readiness_id=cast(str | None, value.get("readiness_id")),
                coverage_id=cast(str | None, value.get("coverage_id")),
                admission=cast(WebResearchAnswerAdmission, value.get("admission", value.get("state"))),
                reason=cast(WebResearchAnswerGateReason, value.get("reason")),
            )
            is not None
        )
    except (TypeError, ValueError):
        return False


admit_web_research_answer = build_web_research_answer_gate
decide_web_research_answer_gate = build_web_research_answer_gate
validate_answer_gate = validate_web_research_answer_gate


__all__ = [
    "MAX_GATE_ID_CHARS",
    "WEB_RESEARCH_ANSWER_GATE_SCHEMA",
    "AnswerAdmission",
    "AnswerGateReason",
    "WebResearchAnswerAdmission",
    "WebResearchAnswerGate",
    "WebResearchAnswerGateError",
    "WebResearchAnswerGateReason",
    "WebResearchAnswerGateV1",
    "admit_web_research_answer",
    "build_web_research_answer_gate",
    "decide_web_research_answer_gate",
    "validate_answer_gate",
    "validate_web_research_answer_gate",
]
