"""Pure grounding contract for observed public-web evidence.

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


class WebEvidenceGroundingError(ValueError):
    """The grounding identity or equivalent claim/source facts are invalid."""


class WebEvidenceGroundingState(StrEnum):
    """Closed grounding outcomes."""

    EMPTY = "empty"
    GROUNDED = "grounded"
    PARTIAL = "partial"
    UNGROUNDED = "ungrounded"
    BLOCKED = "blocked"


class WebEvidenceGroundingReason(StrEnum):
    """Closed short reasons for one grounding result."""

    NO_CLAIMS = "no_claims"
    ALL_CLAIMS_GROUNDED = "all_claims_grounded"
    SOME_CLAIMS_GROUNDED = "some_claims_grounded"
    NO_GROUNDED_CLAIMS = "no_grounded_claims"
    UNKNOWN_SOURCE_ID = "unknown_source_id"
    INVALID_BUNDLE = "invalid_bundle"
    PRIVATE_SOURCE_URL = "private_source_url"
    INVALID_SOURCE_FACTS = "invalid_source_facts"


@dataclass(frozen=True, slots=True)
class WebEvidenceGroundingV1:
    """Frozen body-free grounding summary for one authenticated turn."""

    grounding_id: str
    authenticated_turn_id: str
    grounding: WebEvidenceGroundingState
    grounded_claim_count: int
    claim_count: int
    reason: WebEvidenceGroundingReason

    @property
    def state(self) -> WebEvidenceGroundingState:
        return self.grounding

    @property
    def closed_grounding(self) -> WebEvidenceGroundingState:
        return self.grounding

    @property
    def decision(self) -> WebEvidenceGroundingState:
        return self.grounding

    @property
    def closed_reason(self) -> WebEvidenceGroundingReason:
        return self.reason

    def __post_init__(self) -> None:
        _identity(self.grounding_id, field="grounding_id")
        _identity(self.authenticated_turn_id, field="authenticated_turn_id")
        grounding = _grounding(self.grounding)
        reason = _reason(self.reason)
        object.__setattr__(self, "grounding", grounding)
        object.__setattr__(self, "reason", reason)
        _count(self.grounded_claim_count, field="grounded_claim_count")
        _count(self.claim_count, field="claim_count")
        if self.grounded_claim_count > self.claim_count:
            raise WebEvidenceGroundingError("grounded_claim_count exceeds claim_count")
        if grounding is WebEvidenceGroundingState.EMPTY and self.claim_count != 0:
            raise WebEvidenceGroundingError("empty grounding must have no claims")
        if grounding is WebEvidenceGroundingState.BLOCKED and (
            self.grounded_claim_count != 0 or self.claim_count != 0
        ):
            raise WebEvidenceGroundingError("blocked grounding cannot expose claim counts")
        if grounding is WebEvidenceGroundingState.GROUNDED and (
            self.claim_count == 0 or self.grounded_claim_count != self.claim_count
        ):
            raise WebEvidenceGroundingError("grounded result needs every claim grounded")
        if grounding is WebEvidenceGroundingState.PARTIAL and not (
            0 < self.grounded_claim_count < self.claim_count
        ):
            raise WebEvidenceGroundingError("partial grounding needs both grounded and ungrounded claims")
        if grounding is WebEvidenceGroundingState.UNGROUNDED and not (
            self.claim_count > 0 and self.grounded_claim_count == 0
        ):
            raise WebEvidenceGroundingError("ungrounded result needs claims and no grounded claims")


EvidenceGroundingState = WebEvidenceGroundingState
EvidenceGroundingReason = WebEvidenceGroundingReason
WebEvidenceGrounding = WebEvidenceGroundingV1
WebEvidenceGroundingDecision = WebEvidenceGroundingState


_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SOURCE_ID_RE = _IDENTITY_RE
_MISSING = object()


def _identity(value: object, *, field: str) -> str:
    if type(value) is not str or _IDENTITY_RE.fullmatch(value) is None:
        raise WebEvidenceGroundingError(f"{field} must be a bounded opaque identifier")
    return value


def _grounding(value: object) -> WebEvidenceGroundingState:
    if isinstance(value, WebEvidenceGroundingState):
        return value
    if type(value) is not str:
        raise WebEvidenceGroundingError("grounding must be a closed value")
    try:
        return WebEvidenceGroundingState(value.strip().casefold())
    except ValueError as exc:
        raise WebEvidenceGroundingError("unknown grounding value") from exc


def _reason(value: object) -> WebEvidenceGroundingReason:
    if isinstance(value, WebEvidenceGroundingReason):
        return value
    if type(value) is not str:
        raise WebEvidenceGroundingError("reason must be a closed value")
    try:
        return WebEvidenceGroundingReason(value.strip().casefold())
    except ValueError as exc:
        raise WebEvidenceGroundingError("unknown grounding reason") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CLAIMS:
        raise WebEvidenceGroundingError(f"{field} is outside its closed bound")
    return value


def _source_ids(value: object) -> frozenset[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebEvidenceGroundingError("admitted_source_ids must be a sequence")
    if len(value) > MAX_CLAIMS:
        raise WebEvidenceGroundingError("admitted_source_ids exceed the closed bound")
    result: list[str] = []
    for item in value:
        if type(item) is not str or _SOURCE_ID_RE.fullmatch(item) is None:
            raise WebEvidenceGroundingError("admitted source id is invalid")
        result.append(item)
    if len(set(result)) != len(result):
        raise WebEvidenceGroundingError("admitted source ids must be unique")
    return frozenset(result)


def _public_urls(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebEvidenceGroundingError("admitted_source_urls must be a URL sequence")
    if len(value) > MAX_CLAIMS:
        raise WebEvidenceGroundingError("admitted_source_urls exceed the closed bound")
    try:
        result = tuple(validate_public_web_url(item, field="admitted_source_url") for item in value)
    except (TypeError, ValueError) as exc:
        raise WebEvidenceGroundingError("private or invalid source URL") from exc
    if len(set(result)) != len(result):
        raise WebEvidenceGroundingError("admitted source URLs must be unique")
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
        raise WebEvidenceGroundingError("claim contains unknown fields")
    supporting = value.get("supporting_source_ids", value.get("supporting_sources", ()))
    contradicting = value.get("contradicting_source_ids", value.get("contradicting_sources", ()))
    if isinstance(supporting, (str, bytes, bytearray)) or not isinstance(supporting, Sequence):
        raise WebEvidenceGroundingError("supporting source ids must be a sequence")
    if isinstance(contradicting, (str, bytes, bytearray)) or not isinstance(contradicting, Sequence):
        raise WebEvidenceGroundingError("contradicting source ids must be a sequence")
    evidence_state = value.get("evidence_state", value.get("state", _MISSING))
    if evidence_state is _MISSING:
        raise WebEvidenceGroundingError("claim evidence_state is missing")
    current_sensitive = value.get("current_sensitive", value.get("current_sensitive_flag", _MISSING))
    if current_sensitive is _MISSING:
        raise WebEvidenceGroundingError("claim current_sensitive is missing")
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
        raise WebEvidenceGroundingError("invalid claim facts") from exc


def _claims(value: object) -> tuple[WebEvidenceClaimV1, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebEvidenceGroundingError("claims must be a sequence")
    if len(value) > MAX_CLAIMS:
        raise WebEvidenceGroundingError("claims exceed the closed bound")
    result: list[WebEvidenceClaimV1] = []
    for item in value:
        if isinstance(item, WebEvidenceClaimV1):
            result.append(item)
        elif isinstance(item, Mapping):
            result.append(_claim_from_mapping(item))
        else:
            raise WebEvidenceGroundingError("claim item is invalid")
    claim_ids = tuple(item.claim_id for item in result)
    if len(set(claim_ids)) != len(claim_ids):
        raise WebEvidenceGroundingError("claim ids must be unique")
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
        raise WebEvidenceGroundingError("invalid evidence bundle") from exc


def _result(
    grounding_id: str,
    authenticated_turn_id: str,
    grounding: WebEvidenceGroundingState,
    reason: WebEvidenceGroundingReason,
    *,
    grounded: int = 0,
    claims: int = 0,
) -> WebEvidenceGroundingV1:
    return WebEvidenceGroundingV1(
        grounding_id=grounding_id,
        authenticated_turn_id=authenticated_turn_id,
        grounding=grounding,
        grounded_claim_count=grounded,
        claim_count=claims,
        reason=reason,
    )


def build_web_evidence_grounding(
    grounding_id: str,
    authenticated_turn_id: str,
    evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
    claims: Sequence[WebEvidenceClaimV1 | Mapping[str, object]] | None = None,
    admitted_source_ids: Sequence[str] | None = None,
    *,
    admitted_source_urls: Sequence[str] = (),
    admitted_source_facts: Sequence[Mapping[str, object]] | None = None,
) -> WebEvidenceGroundingV1:
    """Build grounding from already-observed claim and source facts."""

    _identity(grounding_id, field="grounding_id")
    _identity(authenticated_turn_id, field="authenticated_turn_id")
    try:
        source_urls = _public_urls(admitted_source_urls)
    except WebEvidenceGroundingError:
        return _result(
            grounding_id,
            authenticated_turn_id,
            WebEvidenceGroundingState.BLOCKED,
            WebEvidenceGroundingReason.PRIVATE_SOURCE_URL,
        )

    bundle: WebEvidenceBundleV1 | None = None
    if evidence_bundle is not None:
        if isinstance(evidence_bundle, Mapping) and _bundle_has_private_url(evidence_bundle):
            return _result(
                grounding_id,
                authenticated_turn_id,
                WebEvidenceGroundingState.BLOCKED,
                WebEvidenceGroundingReason.PRIVATE_SOURCE_URL,
            )
        try:
            bundle = _bundle(evidence_bundle)
        except WebEvidenceGroundingError:
            return _result(
                grounding_id,
                authenticated_turn_id,
                WebEvidenceGroundingState.BLOCKED,
                WebEvidenceGroundingReason.INVALID_BUNDLE,
            )
        if bundle.authenticated_turn_id != authenticated_turn_id:
            return _result(
                grounding_id,
                authenticated_turn_id,
                WebEvidenceGroundingState.BLOCKED,
                WebEvidenceGroundingReason.INVALID_BUNDLE,
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
                raise WebEvidenceGroundingError("admitted source facts exceed the closed bound")
            for fact in admitted_source_facts:
                if not isinstance(fact, Mapping):
                    raise WebEvidenceGroundingError("admitted source fact is invalid")
                if set(fact) - {"source_id", "id", "canonical_url", "url"}:
                    raise WebEvidenceGroundingError("admitted source fact contains unknown fields")
                raw_id = fact.get("source_id", fact.get("id", _MISSING))
                if raw_id is _MISSING:
                    raise WebEvidenceGroundingError("admitted source id is missing")
                fact_ids.append(_identity(raw_id, field="admitted_source_id"))
                raw_url = fact.get("canonical_url", fact.get("url", _MISSING))
                if raw_url is not _MISSING:
                    fact_urls.append(validate_public_web_url(raw_url, field="admitted_source_url"))
            if len(set(fact_ids)) != len(fact_ids):
                raise WebEvidenceGroundingError("admitted source ids must be unique")
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
                raise WebEvidenceGroundingError("admitted source ids disagree with bundle")
            if fact_source_ids and fact_source_ids != bundle_source_ids:
                raise WebEvidenceGroundingError("admitted source facts disagree with bundle")
            source_id_set = bundle_source_ids
        elif fact_source_ids:
            if explicit_source_ids and explicit_source_ids != fact_source_ids:
                raise WebEvidenceGroundingError("admitted source representations disagree")
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
            raise WebEvidenceGroundingError("claims disagree with bundle")
    except WebEvidenceGroundingError as exc:
        reason = (
            WebEvidenceGroundingReason.PRIVATE_SOURCE_URL
            if "url" in str(exc).casefold()
            else WebEvidenceGroundingReason.INVALID_SOURCE_FACTS
        )
        return _result(
            grounding_id,
            authenticated_turn_id,
            WebEvidenceGroundingState.BLOCKED,
            reason,
        )
    except (TypeError, ValueError):
        return _result(
            grounding_id,
            authenticated_turn_id,
            WebEvidenceGroundingState.BLOCKED,
            WebEvidenceGroundingReason.INVALID_SOURCE_FACTS,
        )

    if not observed_claims:
        return _result(
            grounding_id,
            authenticated_turn_id,
            WebEvidenceGroundingState.EMPTY,
            WebEvidenceGroundingReason.NO_CLAIMS,
        )

    referenced_ids = {
        source_id
        for claim in observed_claims
        for source_id in (*claim.supporting_source_ids, *claim.contradicting_source_ids)
    }
    if not referenced_ids.issubset(source_id_set):
        return _result(
            grounding_id,
            authenticated_turn_id,
            WebEvidenceGroundingState.BLOCKED,
            WebEvidenceGroundingReason.UNKNOWN_SOURCE_ID,
        )

    grounded = sum(
        bool(
            set(claim.supporting_source_ids).intersection(source_id_set)
            or set(claim.contradicting_source_ids).intersection(source_id_set)
        )
        for claim in observed_claims
    )
    claim_count = len(observed_claims)
    if grounded == claim_count:
        return _result(
            grounding_id,
            authenticated_turn_id,
            WebEvidenceGroundingState.GROUNDED,
            WebEvidenceGroundingReason.ALL_CLAIMS_GROUNDED,
            grounded=grounded,
            claims=claim_count,
        )
    if grounded:
        return _result(
            grounding_id,
            authenticated_turn_id,
            WebEvidenceGroundingState.PARTIAL,
            WebEvidenceGroundingReason.SOME_CLAIMS_GROUNDED,
            grounded=grounded,
            claims=claim_count,
        )
    return _result(
        grounding_id,
        authenticated_turn_id,
        WebEvidenceGroundingState.UNGROUNDED,
        WebEvidenceGroundingReason.NO_GROUNDED_CLAIMS,
        claims=claim_count,
    )


def assess_web_evidence_grounding(
    grounding_id: str,
    authenticated_turn_id: str,
    evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
    claims: Sequence[WebEvidenceClaimV1 | Mapping[str, object]] | None = None,
    admitted_source_ids: Sequence[str] | None = None,
    *,
    admitted_source_urls: Sequence[str] = (),
    admitted_source_facts: Sequence[Mapping[str, object]] | None = None,
) -> WebEvidenceGroundingV1:
    """Alias for the explicit grounding builder."""

    return build_web_evidence_grounding(
        grounding_id,
        authenticated_turn_id,
        evidence_bundle,
        claims,
        admitted_source_ids,
        admitted_source_urls=admitted_source_urls,
        admitted_source_facts=admitted_source_facts,
    )


decide_web_evidence_grounding = build_web_evidence_grounding
evaluate_web_evidence_grounding = build_web_evidence_grounding


class WebEvidenceGroundingPolicy:
    """Stateless façade for orchestration dependency injection."""

    @staticmethod
    def build(
        grounding_id: str,
        authenticated_turn_id: str,
        evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
        claims: Sequence[WebEvidenceClaimV1 | Mapping[str, object]] | None = None,
        admitted_source_ids: Sequence[str] | None = None,
        *,
        admitted_source_urls: Sequence[str] = (),
        admitted_source_facts: Sequence[Mapping[str, object]] | None = None,
    ) -> WebEvidenceGroundingV1:
        return build_web_evidence_grounding(
            grounding_id,
            authenticated_turn_id,
            evidence_bundle,
            claims,
            admitted_source_ids,
            admitted_source_urls=admitted_source_urls,
            admitted_source_facts=admitted_source_facts,
        )


__all__ = (
    "EvidenceGroundingReason",
    "EvidenceGroundingState",
    "WebEvidenceGrounding",
    "WebEvidenceGroundingDecision",
    "WebEvidenceGroundingError",
    "WebEvidenceGroundingPolicy",
    "WebEvidenceGroundingReason",
    "WebEvidenceGroundingState",
    "WebEvidenceGroundingV1",
    "assess_web_evidence_grounding",
    "build_web_evidence_grounding",
    "decide_web_evidence_grounding",
    "evaluate_web_evidence_grounding",
)
