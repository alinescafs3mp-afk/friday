"""Pure claim-support contract for already-observed public-web evidence.

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


class WebClaimSupportError(ValueError):
    """The support identity or equivalent claim/source facts are invalid."""


class WebClaimSupportState(StrEnum):
    """Closed claim-support outcomes."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


class WebClaimSupportReason(StrEnum):
    """Closed short reasons for one support result."""

    ALL_CLAIMS_SUPPORTED = "all_claims_supported"
    SOME_CLAIMS_SUPPORTED = "some_claims_supported"
    NO_CLAIMS = "no_claims"
    NO_CLAIM_SUPPORT = "no_claim_support"
    UNKNOWN_SOURCE_ID = "unknown_source_id"
    INVALID_BUNDLE = "invalid_bundle"
    PRIVATE_SOURCE_URL = "private_source_url"
    INVALID_SOURCE_FACTS = "invalid_source_facts"


@dataclass(frozen=True, slots=True)
class WebClaimSupportV1:
    """Frozen body-free support summary for one authenticated turn."""

    support_id: str
    authenticated_turn_id: str
    support: WebClaimSupportState
    supported_claim_count: int
    claim_count: int
    reason: WebClaimSupportReason

    @property
    def state(self) -> WebClaimSupportState:
        return self.support

    @property
    def closed_support(self) -> WebClaimSupportState:
        return self.support

    @property
    def decision(self) -> WebClaimSupportState:
        return self.support

    @property
    def closed_reason(self) -> WebClaimSupportReason:
        return self.reason

    def __post_init__(self) -> None:
        _identity(self.support_id, field="support_id")
        _identity(self.authenticated_turn_id, field="authenticated_turn_id")
        support = _state(self.support)
        reason = _reason(self.reason)
        object.__setattr__(self, "support", support)
        object.__setattr__(self, "reason", reason)
        _count(self.supported_claim_count, field="supported_claim_count")
        _count(self.claim_count, field="claim_count")
        if self.supported_claim_count > self.claim_count:
            raise WebClaimSupportError("supported_claim_count exceeds claim_count")
        if support is WebClaimSupportState.EMPTY and self.claim_count != 0:
            raise WebClaimSupportError("empty support must have no claims")
        if support is WebClaimSupportState.BLOCKED and (
            self.supported_claim_count != 0 or self.claim_count != 0
        ):
            raise WebClaimSupportError("blocked support cannot expose claim counts")
        if support is WebClaimSupportState.COMPLETE and (
            self.claim_count == 0 or self.supported_claim_count != self.claim_count
        ):
            raise WebClaimSupportError("complete support needs every claim supported")
        if support is WebClaimSupportState.PARTIAL and not (
            0 < self.supported_claim_count < self.claim_count
        ):
            raise WebClaimSupportError("partial support needs both supported and unsupported claims")
        if support is WebClaimSupportState.UNSUPPORTED and not (
            self.claim_count > 0 and self.supported_claim_count == 0
        ):
            raise WebClaimSupportError("unsupported support needs claims and no supported claims")


ClaimSupportState = WebClaimSupportState
ClaimSupportReason = WebClaimSupportReason
WebClaimSupport = WebClaimSupportV1
WebClaimSupportDecision = WebClaimSupportState


_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_SOURCE_ID_RE = _IDENTITY_RE
_SUPPORTING_STATES = frozenset({"proven", "supported", "partial"})
_MISSING = object()


def _identity(value: object, *, field: str) -> str:
    if type(value) is not str or _IDENTITY_RE.fullmatch(value) is None:
        raise WebClaimSupportError(f"{field} must be a bounded opaque identifier")
    return value


def _state(value: object) -> WebClaimSupportState:
    if isinstance(value, WebClaimSupportState):
        return value
    if type(value) is not str:
        raise WebClaimSupportError("support must be a closed value")
    try:
        return WebClaimSupportState(value.strip().casefold())
    except ValueError as exc:
        raise WebClaimSupportError("unknown support value") from exc


def _reason(value: object) -> WebClaimSupportReason:
    if isinstance(value, WebClaimSupportReason):
        return value
    if type(value) is not str:
        raise WebClaimSupportError("reason must be a closed value")
    try:
        return WebClaimSupportReason(value.strip().casefold())
    except ValueError as exc:
        raise WebClaimSupportError("unknown support reason") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_CLAIMS:
        raise WebClaimSupportError(f"{field} is outside its closed bound")
    return value


def _source_ids(value: object) -> frozenset[str]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebClaimSupportError("admitted_source_ids must be a sequence")
    if len(value) > MAX_CLAIMS:
        raise WebClaimSupportError("admitted_source_ids exceed the closed bound")
    result: list[str] = []
    for item in value:
        if type(item) is not str or _SOURCE_ID_RE.fullmatch(item) is None:
            raise WebClaimSupportError("admitted source id is invalid")
        result.append(item)
    if len(set(result)) != len(result):
        raise WebClaimSupportError("admitted source ids must be unique")
    return frozenset(result)


def _public_urls(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebClaimSupportError("admitted_source_urls must be a URL sequence")
    if len(value) > MAX_CLAIMS:
        raise WebClaimSupportError("admitted_source_urls exceed the closed bound")
    try:
        result = tuple(validate_public_web_url(item, field="admitted_source_url") for item in value)
    except (TypeError, ValueError) as exc:
        raise WebClaimSupportError("private or invalid source URL") from exc
    if len(set(result)) != len(result):
        raise WebClaimSupportError("admitted source URLs must be unique")
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
        raise WebClaimSupportError("claim contains unknown fields")
    supporting = value.get("supporting_source_ids", value.get("supporting_sources", ()))
    contradicting = value.get("contradicting_source_ids", value.get("contradicting_sources", ()))
    if isinstance(supporting, (str, bytes, bytearray)) or not isinstance(supporting, Sequence):
        raise WebClaimSupportError("supporting source ids must be a sequence")
    if isinstance(contradicting, (str, bytes, bytearray)) or not isinstance(contradicting, Sequence):
        raise WebClaimSupportError("contradicting source ids must be a sequence")
    evidence_state = value.get("evidence_state", value.get("state", _MISSING))
    if evidence_state is _MISSING:
        raise WebClaimSupportError("claim evidence_state is missing")
    current_sensitive = value.get("current_sensitive", value.get("current_sensitive_flag", _MISSING))
    if current_sensitive is _MISSING:
        raise WebClaimSupportError("claim current_sensitive is missing")
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
        raise WebClaimSupportError("invalid claim facts") from exc


def _claims(value: object) -> tuple[WebEvidenceClaimV1, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebClaimSupportError("claims must be a sequence")
    if len(value) > MAX_CLAIMS:
        raise WebClaimSupportError("claims exceed the closed bound")
    result: list[WebEvidenceClaimV1] = []
    for item in value:
        if isinstance(item, WebEvidenceClaimV1):
            result.append(item)
        elif isinstance(item, Mapping):
            result.append(_claim_from_mapping(item))
        else:
            raise WebClaimSupportError("claim item is invalid")
    claim_ids = tuple(item.claim_id for item in result)
    if len(set(claim_ids)) != len(claim_ids):
        raise WebClaimSupportError("claim ids must be unique")
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
        raise WebClaimSupportError("invalid evidence bundle") from exc


def _result(
    support_id: str,
    authenticated_turn_id: str,
    support: WebClaimSupportState,
    reason: WebClaimSupportReason,
    *,
    supported: int = 0,
    claims: int = 0,
) -> WebClaimSupportV1:
    return WebClaimSupportV1(
        support_id=support_id,
        authenticated_turn_id=authenticated_turn_id,
        support=support,
        supported_claim_count=supported,
        claim_count=claims,
        reason=reason,
    )


def build_web_claim_support(
    support_id: str,
    authenticated_turn_id: str,
    evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
    claims: Sequence[WebEvidenceClaimV1 | Mapping[str, object]] | None = None,
    admitted_source_ids: Sequence[str] | None = None,
    *,
    admitted_source_urls: Sequence[str] = (),
    admitted_source_facts: Sequence[Mapping[str, object]] | None = None,
) -> WebClaimSupportV1:
    """Build support from an evidence bundle or equivalent claim/source facts."""

    _identity(support_id, field="support_id")
    _identity(authenticated_turn_id, field="authenticated_turn_id")
    try:
        source_urls = _public_urls(admitted_source_urls)
    except WebClaimSupportError:
        return _result(
            support_id,
            authenticated_turn_id,
            WebClaimSupportState.BLOCKED,
            WebClaimSupportReason.PRIVATE_SOURCE_URL,
        )

    bundle: WebEvidenceBundleV1 | None = None
    if evidence_bundle is not None:
        if isinstance(evidence_bundle, Mapping) and _bundle_has_private_url(evidence_bundle):
            return _result(
                support_id,
                authenticated_turn_id,
                WebClaimSupportState.BLOCKED,
                WebClaimSupportReason.PRIVATE_SOURCE_URL,
            )
        try:
            bundle = _bundle(evidence_bundle)
        except WebClaimSupportError:
            return _result(
                support_id,
                authenticated_turn_id,
                WebClaimSupportState.BLOCKED,
                WebClaimSupportReason.INVALID_BUNDLE,
            )
        if bundle.authenticated_turn_id != authenticated_turn_id:
            return _result(
                support_id,
                authenticated_turn_id,
                WebClaimSupportState.BLOCKED,
                WebClaimSupportReason.INVALID_BUNDLE,
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
                raise WebClaimSupportError("admitted source facts exceed the closed bound")
            for fact in admitted_source_facts:
                if not isinstance(fact, Mapping):
                    raise WebClaimSupportError("admitted source fact is invalid")
                if set(fact) - {"source_id", "id", "canonical_url", "url"}:
                    raise WebClaimSupportError("admitted source fact contains unknown fields")
                raw_id = fact.get("source_id", fact.get("id", _MISSING))
                if raw_id is _MISSING:
                    raise WebClaimSupportError("admitted source id is missing")
                fact_ids.append(_identity(raw_id, field="admitted_source_id"))
                raw_url = fact.get("canonical_url", fact.get("url", _MISSING))
                if raw_url is not _MISSING:
                    fact_urls.append(validate_public_web_url(raw_url, field="admitted_source_url"))
            if len(set(fact_ids)) != len(fact_ids):
                raise WebClaimSupportError("admitted source ids must be unique")
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
                raise WebClaimSupportError("admitted source ids disagree with bundle")
            if fact_source_ids and fact_source_ids != bundle_source_ids:
                raise WebClaimSupportError("admitted source facts disagree with bundle")
            source_id_set = bundle_source_ids
        elif fact_source_ids:
            if explicit_source_ids and explicit_source_ids != fact_source_ids:
                raise WebClaimSupportError("admitted source representations disagree")
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
            raise WebClaimSupportError("claims disagree with bundle")
    except WebClaimSupportError as exc:
        reason = (
            WebClaimSupportReason.PRIVATE_SOURCE_URL
            if "url" in str(exc).casefold()
            else WebClaimSupportReason.INVALID_SOURCE_FACTS
        )
        return _result(
            support_id,
            authenticated_turn_id,
            WebClaimSupportState.BLOCKED,
            reason,
        )
    except (TypeError, ValueError):
        return _result(
            support_id,
            authenticated_turn_id,
            WebClaimSupportState.BLOCKED,
            WebClaimSupportReason.INVALID_SOURCE_FACTS,
        )

    if not observed_claims:
        return _result(
            support_id,
            authenticated_turn_id,
            WebClaimSupportState.EMPTY,
            WebClaimSupportReason.NO_CLAIMS,
        )

    referenced_ids = {
        source_id
        for claim in observed_claims
        for source_id in (*claim.supporting_source_ids, *claim.contradicting_source_ids)
    }
    if not referenced_ids.issubset(source_id_set):
        return _result(
            support_id,
            authenticated_turn_id,
            WebClaimSupportState.BLOCKED,
            WebClaimSupportReason.UNKNOWN_SOURCE_ID,
        )
    supported = sum(
        bool(set(claim.supporting_source_ids).intersection(source_id_set))
        and claim.evidence_state.casefold() in _SUPPORTING_STATES
        for claim in observed_claims
    )
    claim_count = len(observed_claims)
    if supported == claim_count:
        return _result(
            support_id,
            authenticated_turn_id,
            WebClaimSupportState.COMPLETE,
            WebClaimSupportReason.ALL_CLAIMS_SUPPORTED,
            supported=supported,
            claims=claim_count,
        )
    if supported:
        return _result(
            support_id,
            authenticated_turn_id,
            WebClaimSupportState.PARTIAL,
            WebClaimSupportReason.SOME_CLAIMS_SUPPORTED,
            supported=supported,
            claims=claim_count,
        )
    return _result(
        support_id,
        authenticated_turn_id,
        WebClaimSupportState.UNSUPPORTED,
        WebClaimSupportReason.NO_CLAIM_SUPPORT,
        claims=claim_count,
    )


def assess_web_claim_support(
    support_id: str,
    authenticated_turn_id: str,
    evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
    claims: Sequence[WebEvidenceClaimV1 | Mapping[str, object]] | None = None,
    admitted_source_ids: Sequence[str] | None = None,
    *,
    admitted_source_urls: Sequence[str] = (),
    admitted_source_facts: Sequence[Mapping[str, object]] | None = None,
) -> WebClaimSupportV1:
    """Alias for the explicit support builder."""

    return build_web_claim_support(
        support_id,
        authenticated_turn_id,
        evidence_bundle,
        claims,
        admitted_source_ids,
        admitted_source_urls=admitted_source_urls,
        admitted_source_facts=admitted_source_facts,
    )


decide_web_claim_support = build_web_claim_support
evaluate_web_claim_support = build_web_claim_support


class WebClaimSupportPolicy:
    """Stateless façade for orchestration dependency injection."""

    @staticmethod
    def build(
        support_id: str,
        authenticated_turn_id: str,
        evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
        claims: Sequence[WebEvidenceClaimV1 | Mapping[str, object]] | None = None,
        admitted_source_ids: Sequence[str] | None = None,
        *,
        admitted_source_urls: Sequence[str] = (),
        admitted_source_facts: Sequence[Mapping[str, object]] | None = None,
    ) -> WebClaimSupportV1:
        return build_web_claim_support(
            support_id,
            authenticated_turn_id,
            evidence_bundle,
            claims,
            admitted_source_ids,
            admitted_source_urls=admitted_source_urls,
            admitted_source_facts=admitted_source_facts,
        )


__all__ = (
    "ClaimSupportReason",
    "ClaimSupportState",
    "WebClaimSupport",
    "WebClaimSupportDecision",
    "WebClaimSupportError",
    "WebClaimSupportPolicy",
    "WebClaimSupportReason",
    "WebClaimSupportState",
    "WebClaimSupportV1",
    "assess_web_claim_support",
    "build_web_claim_support",
    "decide_web_claim_support",
    "evaluate_web_claim_support",
)
