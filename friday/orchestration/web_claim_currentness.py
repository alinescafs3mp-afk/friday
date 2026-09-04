"""Pure currentness admission for already-built public-web claims.

The contract consumes typed evidence claims and an already-classified
``WebCurrentnessDecision``.  It never classifies a question, fetches a page,
reads a file, or wires a retrieval route.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, cast

from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_evidence_bundle import WebEvidenceClaimV1

WEB_CLAIM_CURRENTNESS_SCHEMA = "friday.web-claim-currentness.v1"
MAX_CURRENTNESS_ID_CHARS = 128
MAX_CLAIMS = 128

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class WebClaimCurrentnessError(ValueError):
    """A claim-currentness value is outside the closed contract."""


class WebClaimCurrentnessState(StrEnum):
    """Closed claim-currentness admission outcomes."""

    EMPTY = "empty"
    ADMITTED = "admitted"
    HOLD = "hold"
    BLOCKED = "blocked"


class WebClaimCurrentnessReason(StrEnum):
    """Closed short reason for one claim-currentness outcome."""

    NO_CLAIMS = "no_claims"
    NO_CURRENT_SENSITIVE_CLAIMS = "no_current_sensitive_claims"
    CURRENTNESS_REQUIRED = "currentness_required"
    CURRENTNESS_NOT_REQUIRED = "currentness_not_required"
    CURRENTNESS_PRIVATE = "currentness_private"
    FACTS_INVALID = "facts_invalid"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise WebClaimCurrentnessError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _state(value: object) -> WebClaimCurrentnessState:
    try:
        return WebClaimCurrentnessState(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise WebClaimCurrentnessError("admission_closed") from exc


def _reason(value: object) -> WebClaimCurrentnessReason:
    try:
        return WebClaimCurrentnessReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise WebClaimCurrentnessError("reason_closed") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CLAIMS:
        _fail(field, "range")
    return cast(int, value)


@dataclass(frozen=True, slots=True)
class WebClaimCurrentnessV1:
    """Immutable admission facts for current-sensitive public-web claims."""

    currentness_id: str
    authenticated_turn_id: str
    admission: WebClaimCurrentnessState
    current_sensitive_claim_count: int
    claim_count: int
    reason: WebClaimCurrentnessReason

    def __post_init__(self) -> None:
        _identifier(self.currentness_id, field="currentness_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        admission = _state(self.admission)
        reason = _reason(self.reason)
        object.__setattr__(self, "admission", admission)
        object.__setattr__(self, "reason", reason)
        sensitive = _count(self.current_sensitive_claim_count, field="current_sensitive_claim_count")
        claims = _count(self.claim_count, field="claim_count")
        if sensitive > claims:
            _fail("claim_counts", "inconsistent")
        if admission is WebClaimCurrentnessState.BLOCKED and (sensitive or claims):
            _fail("blocked_counts", "nonzero")
        if admission is WebClaimCurrentnessState.EMPTY and claims:
            _fail("empty_claims", "nonzero")
        if admission is WebClaimCurrentnessState.HOLD and sensitive < 1:
            _fail("hold_claims", "missing_sensitive")
        if admission is WebClaimCurrentnessState.ADMITTED and claims < 1:
            _fail("admitted_claims", "missing")

    @property
    def state(self) -> WebClaimCurrentnessState:
        return self.admission

    @property
    def closed_admission(self) -> WebClaimCurrentnessState:
        return self.admission

    @property
    def decision(self) -> WebClaimCurrentnessState:
        return self.admission

    @property
    def closed_reason(self) -> WebClaimCurrentnessReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": WEB_CLAIM_CURRENTNESS_SCHEMA,
            "currentness_id": self.currentness_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "admission": self.admission.value,
            "current_sensitive_claim_count": self.current_sensitive_claim_count,
            "claim_count": self.claim_count,
            "reason": self.reason.value,
        }


ClaimCurrentnessState = WebClaimCurrentnessState
ClaimCurrentnessReason = WebClaimCurrentnessReason
ClaimCurrentnessAdmission = WebClaimCurrentnessState
ClaimCurrentnessDecision = WebClaimCurrentnessState
WebClaimCurrentnessDecision = WebClaimCurrentnessState
WebClaimCurrentness = WebClaimCurrentnessV1


def _coerce_decision(value: object) -> WebCurrentnessDecision:
    try:
        # This is deliberately enum coercion only; classification belongs to
        # the caller and must not be repeated at this claim boundary.
        return WebCurrentnessDecision(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise WebClaimCurrentnessError("currentness_invalid") from exc


def _coerce_claim(value: object) -> WebEvidenceClaimV1:
    if isinstance(value, WebEvidenceClaimV1):
        return value
    if not isinstance(value, Mapping):
        _fail("claims_item", "type")
    allowed = {
        "claim_id",
        "id",
        "normalized_claim",
        "claim",
        "supporting_source_ids",
        "supporting_sources",
        "contradicting_source_ids",
        "contradicting_sources",
        "evidence_state",
        "state",
        "current_sensitive",
        "current_sensitive_flag",
    }
    if set(value) - allowed:
        _fail("claims_item", "closed")
    try:
        supporting = value.get("supporting_source_ids", value.get("supporting_sources", ()))
        contradicting = value.get("contradicting_source_ids", value.get("contradicting_sources", ()))
        current_sensitive = value.get("current_sensitive", value.get("current_sensitive_flag"))
        return WebEvidenceClaimV1(
            claim_id=cast(str, value.get("claim_id", value.get("id"))),
            normalized_claim=cast(str, value.get("normalized_claim", value.get("claim"))),
            supporting_source_ids=tuple(cast(Sequence[str], supporting)),
            contradicting_source_ids=tuple(cast(Sequence[str], contradicting)),
            evidence_state=cast(str, value.get("evidence_state", value.get("state"))),
            current_sensitive=cast(bool, current_sensitive),
        )
    except (TypeError, ValueError) as exc:
        raise WebClaimCurrentnessError("claims_item_invalid") from exc


def _coerce_claims(value: object) -> tuple[WebEvidenceClaimV1, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _fail("claims", "sequence")
    if len(value) > MAX_CLAIMS:
        _fail("claims", "bound")
    claims = tuple(_coerce_claim(item) for item in value)
    claim_ids = tuple(claim.claim_id for claim in claims)
    if len(set(claim_ids)) != len(claim_ids):
        _fail("claims", "duplicate_id")
    return claims


def _result(
    currentness_id: str,
    authenticated_turn_id: str,
    admission: WebClaimCurrentnessState,
    reason: WebClaimCurrentnessReason,
    *,
    sensitive: int = 0,
    claims: int = 0,
) -> WebClaimCurrentnessV1:
    return WebClaimCurrentnessV1(
        currentness_id=currentness_id,
        authenticated_turn_id=authenticated_turn_id,
        admission=admission,
        current_sensitive_claim_count=sensitive,
        claim_count=claims,
        reason=reason,
    )


def build_web_claim_currentness(
    currentness_id: str | Mapping[str, Any],
    authenticated_turn_id: str | None = None,
    currentness: WebCurrentnessDecision | str | None = None,
    claims: object = (),
) -> WebClaimCurrentnessV1:
    """Admit current-sensitive claims against an already-classified decision."""

    if isinstance(currentness_id, Mapping):
        raw = currentness_id
        known = {
            "schema",
            "currentness_id",
            "authenticated_turn_id",
            "currentness",
            "decision",
            "claims",
            "claim_facts",
            "admission",
            "state",
            "current_sensitive_claim_count",
            "claim_count",
            "reason",
        }
        if set(raw) - known:
            _fail("currentness", "unknown_fields")
        if raw.get("schema", WEB_CLAIM_CURRENTNESS_SCHEMA) != WEB_CLAIM_CURRENTNESS_SCHEMA:
            _fail("schema")
        output_keys = {
            "admission",
            "state",
            "current_sensitive_claim_count",
            "claim_count",
            "reason",
        }
        fact_keys = {"currentness", "decision", "claims", "claim_facts"}
        if output_keys.intersection(raw) and fact_keys.intersection(raw):
            _fail("currentness", "duplicate_representations")
        if output_keys.intersection(raw):
            return WebClaimCurrentnessV1(
                currentness_id=cast(str, raw.get("currentness_id")),
                authenticated_turn_id=cast(str, raw.get("authenticated_turn_id")),
                admission=cast(WebClaimCurrentnessState, raw.get("admission", raw.get("state"))),
                current_sensitive_claim_count=cast(int, raw.get("current_sensitive_claim_count")),
                claim_count=cast(int, raw.get("claim_count")),
                reason=cast(WebClaimCurrentnessReason, raw.get("reason")),
            )
        currentness_id = cast(str, raw.get("currentness_id"))
        authenticated_turn_id = cast(str, raw.get("authenticated_turn_id"))
        currentness = cast(WebCurrentnessDecision | str, raw.get("currentness", raw.get("decision")))
        claims = raw.get("claims", raw.get("claim_facts", ()))

    currentness_key = _identifier(currentness_id, field="currentness_id")
    turn_key = _identifier(authenticated_turn_id, field="authenticated_turn_id")
    try:
        decision = _coerce_decision(currentness)
        claim_values = _coerce_claims(claims)
    except WebClaimCurrentnessError as exc:
        reason = (
            WebClaimCurrentnessReason.CURRENTNESS_PRIVATE
            if "currentness_private" in str(exc)
            else WebClaimCurrentnessReason.FACTS_INVALID
        )
        return _result(currentness_key, turn_key, WebClaimCurrentnessState.BLOCKED, reason)

    if decision is WebCurrentnessDecision.SEARCH_BLOCKED_PRIVATE:
        return _result(
            currentness_key,
            turn_key,
            WebClaimCurrentnessState.BLOCKED,
            WebClaimCurrentnessReason.CURRENTNESS_PRIVATE,
        )
    claim_count = len(claim_values)
    sensitive_count = sum(claim.current_sensitive for claim in claim_values)
    if claim_count == 0:
        return _result(
            currentness_key,
            turn_key,
            WebClaimCurrentnessState.EMPTY,
            WebClaimCurrentnessReason.NO_CLAIMS,
        )
    if sensitive_count == 0:
        return _result(
            currentness_key,
            turn_key,
            WebClaimCurrentnessState.ADMITTED,
            WebClaimCurrentnessReason.NO_CURRENT_SENSITIVE_CLAIMS,
            sensitive=sensitive_count,
            claims=claim_count,
        )
    if decision is WebCurrentnessDecision.SEARCH_NOT_REQUIRED:
        return _result(
            currentness_key,
            turn_key,
            WebClaimCurrentnessState.HOLD,
            WebClaimCurrentnessReason.CURRENTNESS_NOT_REQUIRED,
            sensitive=sensitive_count,
            claims=claim_count,
        )
    return _result(
        currentness_key,
        turn_key,
        WebClaimCurrentnessState.ADMITTED,
        WebClaimCurrentnessReason.CURRENTNESS_REQUIRED,
        sensitive=sensitive_count,
        claims=claim_count,
    )


def validate_web_claim_currentness(value: object) -> bool:
    """Return whether a serialized claim-currentness result is valid."""

    try:
        if isinstance(value, WebClaimCurrentnessV1):
            value.__post_init__()
            return True
        if not isinstance(value, Mapping):
            return False
        allowed = {
            "schema",
            "currentness_id",
            "authenticated_turn_id",
            "admission",
            "current_sensitive_claim_count",
            "claim_count",
            "reason",
        }
        if set(value) - allowed or value.get("schema") != WEB_CLAIM_CURRENTNESS_SCHEMA:
            return False
        if set(value) != allowed:
            return False
        return (
            WebClaimCurrentnessV1(
                currentness_id=cast(str, value.get("currentness_id")),
                authenticated_turn_id=cast(str, value.get("authenticated_turn_id")),
                admission=cast(WebClaimCurrentnessState, value.get("admission")),
                current_sensitive_claim_count=cast(int, value.get("current_sensitive_claim_count")),
                claim_count=cast(int, value.get("claim_count")),
                reason=cast(WebClaimCurrentnessReason, value.get("reason")),
            )
            is not None
        )
    except (TypeError, ValueError):
        return False


decide_web_claim_currentness = build_web_claim_currentness
evaluate_web_claim_currentness = build_web_claim_currentness
validate_claim_currentness = validate_web_claim_currentness


__all__ = [
    "MAX_CLAIMS",
    "MAX_CURRENTNESS_ID_CHARS",
    "WEB_CLAIM_CURRENTNESS_SCHEMA",
    "ClaimCurrentnessAdmission",
    "ClaimCurrentnessDecision",
    "ClaimCurrentnessReason",
    "ClaimCurrentnessState",
    "WebClaimCurrentness",
    "WebClaimCurrentnessError",
    "WebClaimCurrentnessReason",
    "WebClaimCurrentnessState",
    "WebClaimCurrentnessDecision",
    "WebClaimCurrentnessV1",
    "build_web_claim_currentness",
    "decide_web_claim_currentness",
    "evaluate_web_claim_currentness",
    "validate_claim_currentness",
    "validate_web_claim_currentness",
]
