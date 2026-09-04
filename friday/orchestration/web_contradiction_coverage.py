"""Pure contradiction-coverage contract for observed public-web evidence.

The builder consumes claims and admitted source facts only.  It does not fetch
pages, read files, persist evidence, or wire a live retrieval route.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from friday.orchestration.web_evidence_bundle import (
    MAX_CLAIMS,
    WebEvidenceBundleError,
    WebEvidenceBundleV1,
    WebEvidenceClaimV1,
    build_web_evidence_bundle,
)
from friday.orchestration.web_provider_policy import validate_public_web_url


class WebContradictionCoverageError(ValueError):
    """The coverage identity or equivalent claim/source facts are invalid."""


class WebContradictionCoverageState(StrEnum):
    """Closed contradiction-coverage outcomes."""

    EMPTY = "empty"
    NONE = "none"
    PRESENT = "present"
    UNIVERSAL = "universal"
    BLOCKED = "blocked"


class WebContradictionCoverageReason(StrEnum):
    """Closed short reasons for one contradiction-coverage result."""

    NO_CLAIMS = "no_claims"
    NO_CONTRADICTED_CLAIMS = "no_contradicted_claims"
    SOME_CLAIMS_CONTRADICTED = "some_claims_contradicted"
    ALL_CLAIMS_CONTRADICTED = "all_claims_contradicted"
    UNKNOWN_SOURCE_ID = "unknown_source_id"
    INVALID_BUNDLE = "invalid_bundle"
    PRIVATE_SOURCE_URL = "private_source_url"
    INVALID_SOURCE_FACTS = "invalid_source_facts"


@dataclass(frozen=True, slots=True)
class WebContradictionCoverageV1:
    """Frozen body-free contradiction coverage for one authenticated turn."""

    coverage_id: str
    authenticated_turn_id: str
    coverage: WebContradictionCoverageState
    contradicted_claim_count: int
    claim_count: int
    reason: WebContradictionCoverageReason

    @property
    def state(self) -> WebContradictionCoverageState:
        return self.coverage

    @property
    def closed_coverage(self) -> WebContradictionCoverageState:
        return self.coverage

    @property
    def decision(self) -> WebContradictionCoverageState:
        return self.coverage

    @property
    def closed_reason(self) -> WebContradictionCoverageReason:
        return self.reason

    def __post_init__(self) -> None:
        _identity(self.coverage_id, field="coverage_id")
        _identity(self.authenticated_turn_id, field="authenticated_turn_id")
        coverage = _coverage(self.coverage)
        reason = _reason(self.reason)
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "reason", reason)
        _count(self.contradicted_claim_count, field="contradicted_claim_count")
        _count(self.claim_count, field="claim_count")
        if self.contradicted_claim_count > self.claim_count:
            raise WebContradictionCoverageError("contradicted_claim_count exceeds claim_count")
        if coverage is WebContradictionCoverageState.EMPTY and self.claim_count != 0:
            raise WebContradictionCoverageError("empty coverage must have no claims")
        if coverage is WebContradictionCoverageState.BLOCKED and (
            self.contradicted_claim_count != 0 or self.claim_count != 0
        ):
            raise WebContradictionCoverageError("blocked coverage cannot expose claim counts")
        if coverage is WebContradictionCoverageState.NONE and not (
            self.claim_count > 0 and self.contradicted_claim_count == 0
        ):
            raise WebContradictionCoverageError("none coverage needs claims and no contradictions")
        if coverage is WebContradictionCoverageState.PRESENT and not (
            0 < self.contradicted_claim_count < self.claim_count
        ):
            raise WebContradictionCoverageError("present coverage needs both contradicted and clear claims")
        if coverage is WebContradictionCoverageState.UNIVERSAL and not (
            self.claim_count > 0 and self.contradicted_claim_count == self.claim_count
        ):
            raise WebContradictionCoverageError("universal coverage needs every claim contradicted")


ContradictionCoverageState = WebContradictionCoverageState
ContradictionCoverageReason = WebContradictionCoverageReason
WebContradictionCoverage = WebContradictionCoverageV1
WebContradictionCoverageDecision = WebContradictionCoverageState


_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SOURCE_ID_RE = _IDENTITY_RE
_MISSING = object()


def _identity(value: object, *, field: str) -> str:
    if type(value) is not str or _IDENTITY_RE.fullmatch(value) is None:
        raise WebContradictionCoverageError(f"{field} must be a bounded opaque identifier")
    return value


def _coverage(value: object) -> WebContradictionCoverageState:
    if isinstance(value, WebContradictionCoverageState):
        return value
    if type(value) is not str:
        raise WebContradictionCoverageError("coverage must be a closed value")
    try:
        return WebContradictionCoverageState(value.strip().casefold())
    except ValueError as exc:
        raise WebContradictionCoverageError("unknown coverage value") from exc


def _reason(value: object) -> WebContradictionCoverageReason:
    if isinstance(value, WebContradictionCoverageReason):
        return value
    if type(value) is not str:
        raise WebContradictionCoverageError("reason must be a closed value")
    try:
        return WebContradictionCoverageReason(value.strip().casefold())
    except ValueError as exc:
        raise WebContradictionCoverageError("unknown coverage reason") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CLAIMS:
        raise WebContradictionCoverageError(f"{field} is outside its closed bound")
    return value


def _source_ids(value: object) -> frozenset[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebContradictionCoverageError("admitted_source_ids must be a sequence")
    if len(value) > MAX_CLAIMS:
        raise WebContradictionCoverageError("admitted_source_ids exceed the closed bound")
    result: list[str] = []
    for item in value:
        if type(item) is not str or _SOURCE_ID_RE.fullmatch(item) is None:
            raise WebContradictionCoverageError("admitted source id is invalid")
        result.append(item)
    if len(set(result)) != len(result):
        raise WebContradictionCoverageError("admitted source ids must be unique")
    return frozenset(result)


def _public_urls(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebContradictionCoverageError("admitted_source_urls must be a URL sequence")
    if len(value) > MAX_CLAIMS:
        raise WebContradictionCoverageError("admitted_source_urls exceed the closed bound")
    try:
        result = tuple(validate_public_web_url(item, field="admitted_source_url") for item in value)
    except (TypeError, ValueError) as exc:
        raise WebContradictionCoverageError("private or invalid source URL") from exc
    if len(set(result)) != len(result):
        raise WebContradictionCoverageError("admitted source URLs must be unique")
    return result


def _claim_from_mapping(value: Mapping[str, object]) -> WebEvidenceClaimV1:
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
        raise WebContradictionCoverageError("claim contains unknown fields")
    supporting = value.get("supporting_source_ids", value.get("supporting_sources", ()))
    contradicting = value.get("contradicting_source_ids", value.get("contradicting_sources", ()))
    if isinstance(supporting, (str, bytes, bytearray)) or not isinstance(supporting, Sequence):
        raise WebContradictionCoverageError("supporting source ids must be a sequence")
    if isinstance(contradicting, (str, bytes, bytearray)) or not isinstance(contradicting, Sequence):
        raise WebContradictionCoverageError("contradicting source ids must be a sequence")
    evidence_state = value.get("evidence_state", value.get("state", _MISSING))
    if evidence_state is _MISSING:
        raise WebContradictionCoverageError("claim evidence_state is missing")
    current_sensitive = value.get("current_sensitive", value.get("current_sensitive_flag", _MISSING))
    if current_sensitive is _MISSING:
        raise WebContradictionCoverageError("claim current_sensitive is missing")
    try:
        return WebEvidenceClaimV1(
            claim_id=value.get("claim_id", value.get("id")),  # type: ignore[arg-type]
            normalized_claim=value.get("normalized_claim", value.get("claim")),  # type: ignore[arg-type]
            supporting_source_ids=tuple(supporting),
            contradicting_source_ids=tuple(contradicting),
            evidence_state=evidence_state,  # type: ignore[arg-type]
            current_sensitive=current_sensitive,  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        raise WebContradictionCoverageError("invalid claim facts") from exc


def _claims(value: object) -> tuple[WebEvidenceClaimV1, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebContradictionCoverageError("claims must be a sequence")
    if len(value) > MAX_CLAIMS:
        raise WebContradictionCoverageError("claims exceed the closed bound")
    result: list[WebEvidenceClaimV1] = []
    for item in value:
        if isinstance(item, WebEvidenceClaimV1):
            result.append(item)
        elif isinstance(item, Mapping):
            result.append(_claim_from_mapping(item))
        else:
            raise WebContradictionCoverageError("claim item is invalid")
    claim_ids = tuple(item.claim_id for item in result)
    if len(set(claim_ids)) != len(claim_ids):
        raise WebContradictionCoverageError("claim ids must be unique")
    return tuple(result)


def _bundle_has_private_url(value: Mapping[str, object]) -> bool:
    sources = value.get("sources", ())
    if isinstance(sources, (str, bytes, bytearray)) or not isinstance(sources, Sequence):
        return False
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        raw_url = source.get("canonical_url", source.get("url", _MISSING))
        if raw_url is _MISSING:
            continue
        try:
            validate_public_web_url(raw_url, field="source_url")
        except (TypeError, ValueError):
            return True
    return False


def _bundle(value: WebEvidenceBundleV1 | Mapping[str, object]) -> WebEvidenceBundleV1:
    try:
        return build_web_evidence_bundle(value)
    except (TypeError, ValueError, WebEvidenceBundleError) as exc:
        raise WebContradictionCoverageError("invalid evidence bundle") from exc


def _result(
    coverage_id: str,
    authenticated_turn_id: str,
    coverage: WebContradictionCoverageState,
    reason: WebContradictionCoverageReason,
    *,
    contradicted: int = 0,
    claims: int = 0,
) -> WebContradictionCoverageV1:
    return WebContradictionCoverageV1(
        coverage_id=coverage_id,
        authenticated_turn_id=authenticated_turn_id,
        coverage=coverage,
        contradicted_claim_count=contradicted,
        claim_count=claims,
        reason=reason,
    )


def build_web_contradiction_coverage(
    coverage_id: str,
    authenticated_turn_id: str,
    evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
    claims: Sequence[WebEvidenceClaimV1 | Mapping[str, object]] | None = None,
    admitted_source_ids: Sequence[str] | None = None,
    *,
    admitted_source_urls: Sequence[str] = (),
    admitted_source_facts: Sequence[Mapping[str, object]] | None = None,
) -> WebContradictionCoverageV1:
    """Build contradiction coverage from already-observed evidence facts."""

    _identity(coverage_id, field="coverage_id")
    _identity(authenticated_turn_id, field="authenticated_turn_id")
    try:
        source_urls = _public_urls(admitted_source_urls)
    except WebContradictionCoverageError:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebContradictionCoverageState.BLOCKED,
            WebContradictionCoverageReason.PRIVATE_SOURCE_URL,
        )

    bundle: WebEvidenceBundleV1 | None = None
    if evidence_bundle is not None:
        if isinstance(evidence_bundle, Mapping) and _bundle_has_private_url(evidence_bundle):
            return _result(
                coverage_id,
                authenticated_turn_id,
                WebContradictionCoverageState.BLOCKED,
                WebContradictionCoverageReason.PRIVATE_SOURCE_URL,
            )
        try:
            bundle = _bundle(evidence_bundle)
        except WebContradictionCoverageError:
            return _result(
                coverage_id,
                authenticated_turn_id,
                WebContradictionCoverageState.BLOCKED,
                WebContradictionCoverageReason.INVALID_BUNDLE,
            )
        if bundle.authenticated_turn_id != authenticated_turn_id:
            return _result(
                coverage_id,
                authenticated_turn_id,
                WebContradictionCoverageState.BLOCKED,
                WebContradictionCoverageReason.INVALID_BUNDLE,
            )
        bundle_source_ids = frozenset(source.source_id for source in bundle.sources)
        bundle_claims = bundle.claims
    else:
        bundle_source_ids = frozenset()
        bundle_claims = ()

    try:
        if admitted_source_facts is not None:
            fact_ids: list[str] = []
            fact_urls: list[str] = []
            if len(admitted_source_facts) > MAX_CLAIMS:
                raise WebContradictionCoverageError("admitted source facts exceed the closed bound")
            for fact in admitted_source_facts:
                if not isinstance(fact, Mapping):
                    raise WebContradictionCoverageError("admitted source fact is invalid")
                if set(fact) - {"source_id", "id", "canonical_url", "url"}:
                    raise WebContradictionCoverageError("admitted source fact contains unknown fields")
                raw_id = fact.get("source_id", fact.get("id", _MISSING))
                if raw_id is _MISSING:
                    raise WebContradictionCoverageError("admitted source id is missing")
                fact_ids.append(_identity(raw_id, field="admitted_source_id"))
                raw_url = fact.get("canonical_url", fact.get("url", _MISSING))
                if raw_url is not _MISSING:
                    fact_urls.append(validate_public_web_url(raw_url, field="admitted_source_url"))
            if len(set(fact_ids)) != len(fact_ids):
                raise WebContradictionCoverageError("admitted source ids must be unique")
            if fact_urls:
                source_urls = tuple((*source_urls, *fact_urls))
            fact_source_ids = frozenset(fact_ids)
        else:
            fact_source_ids = frozenset()

        explicit_source_ids = (
            _source_ids(admitted_source_ids) if admitted_source_ids is not None else frozenset()
        )
        if bundle is not None:
            if admitted_source_ids is not None and explicit_source_ids != bundle_source_ids:
                raise WebContradictionCoverageError("admitted source ids disagree with bundle")
            if fact_source_ids and fact_source_ids != bundle_source_ids:
                raise WebContradictionCoverageError("admitted source facts disagree with bundle")
            source_id_set = bundle_source_ids
        elif fact_source_ids:
            if explicit_source_ids and explicit_source_ids != fact_source_ids:
                raise WebContradictionCoverageError("admitted source representations disagree")
            source_id_set = fact_source_ids
        else:
            source_id_set = explicit_source_ids

        observed_claims = _claims(claims) if claims is not None else bundle_claims
        if (
            claims is not None
            and bundle is not None
            and tuple(claim.claim_id for claim in observed_claims)
            != tuple(claim.claim_id for claim in bundle_claims)
        ):
            raise WebContradictionCoverageError("claims disagree with bundle")
    except WebContradictionCoverageError as exc:
        reason = (
            WebContradictionCoverageReason.PRIVATE_SOURCE_URL
            if "url" in str(exc).casefold()
            else WebContradictionCoverageReason.INVALID_SOURCE_FACTS
        )
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebContradictionCoverageState.BLOCKED,
            reason,
        )
    except (TypeError, ValueError):
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebContradictionCoverageState.BLOCKED,
            WebContradictionCoverageReason.INVALID_SOURCE_FACTS,
        )

    if not observed_claims:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebContradictionCoverageState.EMPTY,
            WebContradictionCoverageReason.NO_CLAIMS,
        )

    referenced_ids = {
        source_id
        for claim in observed_claims
        for source_id in (*claim.supporting_source_ids, *claim.contradicting_source_ids)
    }
    if not referenced_ids.issubset(source_id_set):
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebContradictionCoverageState.BLOCKED,
            WebContradictionCoverageReason.UNKNOWN_SOURCE_ID,
        )

    contradicted = sum(
        bool(set(claim.contradicting_source_ids).intersection(source_id_set)) for claim in observed_claims
    )
    claim_count = len(observed_claims)
    if contradicted == claim_count:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebContradictionCoverageState.UNIVERSAL,
            WebContradictionCoverageReason.ALL_CLAIMS_CONTRADICTED,
            contradicted=contradicted,
            claims=claim_count,
        )
    if contradicted:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebContradictionCoverageState.PRESENT,
            WebContradictionCoverageReason.SOME_CLAIMS_CONTRADICTED,
            contradicted=contradicted,
            claims=claim_count,
        )
    return _result(
        coverage_id,
        authenticated_turn_id,
        WebContradictionCoverageState.NONE,
        WebContradictionCoverageReason.NO_CONTRADICTED_CLAIMS,
        claims=claim_count,
    )


def assess_web_contradiction_coverage(
    coverage_id: str,
    authenticated_turn_id: str,
    evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
    claims: Sequence[WebEvidenceClaimV1 | Mapping[str, object]] | None = None,
    admitted_source_ids: Sequence[str] | None = None,
    *,
    admitted_source_urls: Sequence[str] = (),
    admitted_source_facts: Sequence[Mapping[str, object]] | None = None,
) -> WebContradictionCoverageV1:
    """Alias for the explicit contradiction-coverage builder."""

    return build_web_contradiction_coverage(
        coverage_id,
        authenticated_turn_id,
        evidence_bundle,
        claims,
        admitted_source_ids,
        admitted_source_urls=admitted_source_urls,
        admitted_source_facts=admitted_source_facts,
    )


decide_web_contradiction_coverage = build_web_contradiction_coverage
evaluate_web_contradiction_coverage = build_web_contradiction_coverage


class WebContradictionCoveragePolicy:
    """Stateless façade for orchestration dependency injection."""

    @staticmethod
    def build(
        coverage_id: str,
        authenticated_turn_id: str,
        evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
        claims: Sequence[WebEvidenceClaimV1 | Mapping[str, object]] | None = None,
        admitted_source_ids: Sequence[str] | None = None,
        *,
        admitted_source_urls: Sequence[str] = (),
        admitted_source_facts: Sequence[Mapping[str, object]] | None = None,
    ) -> WebContradictionCoverageV1:
        return build_web_contradiction_coverage(
            coverage_id,
            authenticated_turn_id,
            evidence_bundle,
            claims,
            admitted_source_ids,
            admitted_source_urls=admitted_source_urls,
            admitted_source_facts=admitted_source_facts,
        )


__all__ = (
    "ContradictionCoverageReason",
    "ContradictionCoverageState",
    "WebContradictionCoverage",
    "WebContradictionCoverageDecision",
    "WebContradictionCoverageError",
    "WebContradictionCoveragePolicy",
    "WebContradictionCoverageReason",
    "WebContradictionCoverageState",
    "WebContradictionCoverageV1",
    "assess_web_contradiction_coverage",
    "build_web_contradiction_coverage",
    "decide_web_contradiction_coverage",
    "evaluate_web_contradiction_coverage",
)
