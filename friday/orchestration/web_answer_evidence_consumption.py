"""Consume an actual web answer against the already-admitted evidence.

Research I/O observers keep empty claim lists.  This module is the requesting
workflow: it binds the published answer's claims to admitted sources, reuses
the existing support/currentness/contradiction/citation contracts, and returns
a closed publication decision.  It does not fetch, invent sources, or call a
provider.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NoReturn, TypedDict, cast
from urllib.parse import urlsplit

from friday.orchestration.web_citation_coverage import (
    WebCitationCoverageState,
    build_web_citation_coverage,
)
from friday.orchestration.web_claim_currentness import (
    WebClaimCurrentnessState,
    build_web_claim_currentness,
)
from friday.orchestration.web_claim_support import (
    WebClaimSupportState,
    build_web_claim_support,
)
from friday.orchestration.web_contradiction_coverage import (
    WebContradictionCoverageState,
    build_web_contradiction_coverage,
)
from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_evidence_bundle import MAX_CLAIM_CHARS, WebEvidenceClaimV1
from friday.orchestration.web_evidence_grounding import (
    WebEvidenceGroundingState,
    build_web_evidence_grounding,
)
from friday.orchestration.web_provider_policy import (
    WebProviderDecision,
    WebProviderPolicyError,
    WebProviderSelection,
    validate_public_web_url,
)
from friday.public_web_url import canonical_public_web_url_key
from friday.web_research_contract import MAX_RESEARCH_SOURCES

WEB_ANSWER_EVIDENCE_CONSUMPTION_SCHEMA = "friday.web-answer-evidence-consumption.v1"

_HOLD_TEXT = "Не публикую эти сведения как подтверждённый факт: допущенные источники их не подтверждают."
_BLOCKED_TEXT = "Публикация остановлена: источник недопустим."
_CURRENTNESS_HOLD_TEXT = "Не публикую текущие сведения без проверки актуальности."
_PROVIDER_HOLD_TEXT = "Не публикую сведения: выбранный поисковый канал недоступен."

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_URL_RE = re.compile(r"(?i)(?<![\w])(?:[a-z][a-z0-9+.-]*://[^\s<>\"']+)")
_POSIX_PATH_RE = re.compile(r"(?:^|[\s(\[\"'=,:])/(?!/)[^\s<>\"']*")
_HOME_PATH_RE = re.compile(r"(?:^|[\s(\[\"'=,:])~(?:/|\\)[^\s<>\"']*")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:^|[\s(\[\"'=,:])(?:[a-z]:[\\/]|\\\\)[^\s<>\"']*")


class WebAnswerEvidenceConsumptionError(ValueError):
    """The consumption identity or closed input type is invalid."""


class WebAnswerEvidencePublication(StrEnum):
    """Closed publication outcomes for one requesting-answer consumption."""

    ADMITTED = "admitted"
    ADMITTED_DEGRADED = "admitted_degraded"
    HOLD = "hold"
    BLOCKED = "blocked"


class WebAnswerEvidenceReason(StrEnum):
    """Closed short reasons for one answer-evidence consumption."""

    CLAIMS_SUPPORTED = "claims_supported"
    CLAIMS_PARTIAL = "claims_partial"
    CLAIMS_UNSUPPORTED = "claims_unsupported"
    CLAIMS_EMPTY = "claims_empty"
    CURRENTNESS_HOLD = "currentness_hold"
    CURRENTNESS_BLOCKED = "currentness_blocked"
    CONTRADICTION_UNIVERSAL = "contradiction_universal"
    PRIVATE_OR_INVALID = "private_or_invalid"
    UNKNOWN_SOURCE = "unknown_source"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_FALLBACK = "provider_fallback"
    CITATION_EMPTY = "citation_empty"


@dataclass(frozen=True, slots=True)
class WebAnswerEvidenceConsumptionV1:
    """Frozen requesting-workflow decision over an actual answer and admitted sources."""

    consumption_id: str
    authenticated_turn_id: str
    publication: WebAnswerEvidencePublication
    reason: WebAnswerEvidenceReason
    claim_support: str
    claim_currentness: str
    grounding: str
    contradiction: str
    citation_coverage: str
    claim_count: int
    supported_claim_count: int

    def __post_init__(self) -> None:
        _identifier(self.consumption_id, field="consumption_id")
        _identifier(self.authenticated_turn_id, field="authenticated_turn_id")
        publication = _publication(self.publication)
        reason = _reason(self.reason)
        object.__setattr__(self, "publication", publication)
        object.__setattr__(self, "reason", reason)
        for field, value in (
            ("claim_support", self.claim_support),
            ("claim_currentness", self.claim_currentness),
            ("grounding", self.grounding),
            ("contradiction", self.contradiction),
            ("citation_coverage", self.citation_coverage),
        ):
            _token(value, field=field)
        if type(self.claim_count) is not int or not 0 <= self.claim_count <= 128:
            _fail("claim_count", "range")
        if (
            type(self.supported_claim_count) is not int
            or not 0 <= self.supported_claim_count <= self.claim_count
        ):
            _fail("supported_claim_count", "range")
        if publication is WebAnswerEvidencePublication.BLOCKED and (
            self.claim_count != 0 or self.supported_claim_count != 0
        ):
            _fail("blocked_counts", "nonzero")

    @property
    def state(self) -> WebAnswerEvidencePublication:
        return self.publication

    @property
    def decision(self) -> WebAnswerEvidencePublication:
        return self.publication

    @property
    def closed_reason(self) -> WebAnswerEvidenceReason:
        return self.reason

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": WEB_ANSWER_EVIDENCE_CONSUMPTION_SCHEMA,
            "consumption_id": self.consumption_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "publication": self.publication.value,
            "reason": self.reason.value,
            "claim_support": self.claim_support,
            "claim_currentness": self.claim_currentness,
            "grounding": self.grounding,
            "contradiction": self.contradiction,
            "citation_coverage": self.citation_coverage,
            "claim_count": self.claim_count,
            "supported_claim_count": self.supported_claim_count,
        }


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise WebAnswerEvidenceConsumptionError(f"{field}_{detail}")


def _identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _publication(value: object) -> WebAnswerEvidencePublication:
    try:
        return WebAnswerEvidencePublication(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise WebAnswerEvidenceConsumptionError("publication_closed") from exc


def _reason(value: object) -> WebAnswerEvidenceReason:
    try:
        return WebAnswerEvidenceReason(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise WebAnswerEvidenceConsumptionError("reason_closed") from exc


def _token(value: object, *, field: str) -> str:
    if type(value) is not str or not value or len(value) > 64:
        _fail(field, "token")
    return value


def _closed(value: object) -> str:
    enum = getattr(value, "value", value)
    return str(enum)


def _has_private_carrier(text: str) -> bool:
    # Bare filenames such as ``report.pdf`` are ordinary answer prose (file+web
    # compare, obsidian, archive).  A private carrier here is a path, not an
    # extension.  Private URLs are rejected separately via validate_public_web_url.
    without_urls = _URL_RE.sub(" public-url ", text)
    return bool(
        _POSIX_PATH_RE.search(without_urls)
        or _HOME_PATH_RE.search(without_urls)
        or _WINDOWS_PATH_RE.search(without_urls)
    )


def _normalize_claim_text(value: str) -> str:
    text = " ".join(value.split())
    if len(text) > MAX_CLAIM_CHARS:
        text = text[:MAX_CLAIM_CHARS].rstrip()
    return text


def _source_facts(
    admitted_source_urls: Sequence[str],
    admitted_source_ids: Sequence[str] | None,
) -> tuple[tuple[dict[str, str], ...], tuple[str, ...]]:
    urls: list[str] = []
    seen: set[str] = set()
    for raw in admitted_source_urls:
        canonical = validate_public_web_url(raw, field="source_url")
        key = canonical_public_web_url_key(canonical)
        if not key or key in seen:
            continue
        seen.add(key)
        urls.append(canonical)
        if len(urls) >= MAX_RESEARCH_SOURCES:
            break
    if admitted_source_ids is None:
        ids = tuple(f"s{index}" for index in range(1, len(urls) + 1))
    else:
        ids = tuple(_identifier(item, field="admitted_source_id") for item in admitted_source_ids)
        if len(ids) != len(urls):
            _fail("admitted_source_ids", "mismatch")
        if len(set(ids)) != len(ids):
            _fail("admitted_source_ids", "duplicate")
    facts = tuple(
        {"source_id": source_id, "canonical_url": url} for source_id, url in zip(ids, urls, strict=True)
    )
    return facts, tuple(urls)


def _urls_in_text(text: str) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text):
        raw = match.group(0).rstrip(").,;:!?]")
        key = raw.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(raw)
    return tuple(found)


def claims_from_web_answer(
    answer: str,
    admitted_source_facts: Sequence[Mapping[str, str]],
    *,
    current_sensitive: bool = False,
) -> tuple[WebEvidenceClaimV1, ...]:
    """Bind the actual answer to admitted sources.  Extra public URLs stay unsupported."""

    if type(answer) is not str:
        _fail("answer", "type")
    text = _normalize_claim_text(answer)
    if not text:
        return ()
    if _has_private_carrier(text):
        _fail("answer", "private")
    id_by_key: dict[str, str] = {}
    for fact in admitted_source_facts:
        url = fact["canonical_url"]
        id_by_key[canonical_public_web_url_key(url)] = fact["source_id"]
        host = (urlsplit(url).hostname or "").casefold()
        if host and host not in id_by_key:
            id_by_key[host] = fact["source_id"]
    cited: list[str] = []
    extra_public = False
    for raw in _urls_in_text(text):
        try:
            canonical = validate_public_web_url(raw, field="source_url")
        except (TypeError, ValueError, WebProviderPolicyError) as exc:
            raise WebAnswerEvidenceConsumptionError("answer_private") from exc
        key = canonical_public_web_url_key(canonical)
        source_id = id_by_key.get(key) or id_by_key.get((urlsplit(canonical).hostname or "").casefold())
        if source_id is None:
            extra_public = True
            continue
        if source_id not in cited:
            cited.append(source_id)
    claims: list[WebEvidenceClaimV1] = []
    supporting = tuple(cited) if cited else tuple(fact["source_id"] for fact in admitted_source_facts)
    evidence_state = "supported" if supporting else "unverified"
    claims.append(
        WebEvidenceClaimV1(
            claim_id="claim-1",
            normalized_claim=text,
            supporting_source_ids=supporting,
            contradicting_source_ids=(),
            evidence_state=evidence_state,
            current_sensitive=current_sensitive,
        )
    )
    if extra_public:
        claims.append(
            WebEvidenceClaimV1(
                claim_id="claim-2",
                normalized_claim="Unadmitted public URL cited in the answer.",
                supporting_source_ids=(),
                contradicting_source_ids=(),
                evidence_state="unverified",
                current_sensitive=current_sensitive,
            )
        )
    return tuple(claims)


class _ConsumptionCompact(TypedDict):
    claim_support: str
    claim_currentness: str
    grounding: str
    contradiction: str
    citation_coverage: str
    claim_count: int
    supported_claim_count: int


def _result(
    consumption_id: str,
    authenticated_turn_id: str,
    publication: WebAnswerEvidencePublication,
    reason: WebAnswerEvidenceReason,
    *,
    claim_support: str = "blocked",
    claim_currentness: str = "blocked",
    grounding: str = "blocked",
    contradiction: str = "blocked",
    citation_coverage: str = "blocked_private",
    claim_count: int = 0,
    supported_claim_count: int = 0,
) -> WebAnswerEvidenceConsumptionV1:
    if publication is WebAnswerEvidencePublication.BLOCKED:
        claim_count = 0
        supported_claim_count = 0
    return WebAnswerEvidenceConsumptionV1(
        consumption_id=consumption_id,
        authenticated_turn_id=authenticated_turn_id,
        publication=publication,
        reason=reason,
        claim_support=claim_support,
        claim_currentness=claim_currentness,
        grounding=grounding,
        contradiction=contradiction,
        citation_coverage=citation_coverage,
        claim_count=claim_count,
        supported_claim_count=supported_claim_count,
    )


def published_web_answer(answer: str, consumption: WebAnswerEvidenceConsumptionV1) -> str:
    """Return the bytes that may be published after consumption."""

    if consumption.publication in {
        WebAnswerEvidencePublication.ADMITTED,
        WebAnswerEvidencePublication.ADMITTED_DEGRADED,
    }:
        return answer
    if consumption.reason is WebAnswerEvidenceReason.CURRENTNESS_HOLD:
        return _CURRENTNESS_HOLD_TEXT
    if consumption.reason is WebAnswerEvidenceReason.PROVIDER_UNAVAILABLE:
        return _PROVIDER_HOLD_TEXT
    if consumption.publication is WebAnswerEvidencePublication.BLOCKED:
        return _BLOCKED_TEXT
    return _HOLD_TEXT


def consume_web_answer_evidence(
    consumption_id: str,
    authenticated_turn_id: str,
    *,
    answer: str = "",
    admitted_source_urls: Sequence[str] = (),
    admitted_source_ids: Sequence[str] | None = None,
    claims: Sequence[WebEvidenceClaimV1 | Mapping[str, object]] | None = None,
    currentness: WebCurrentnessDecision | str = WebCurrentnessDecision.SEARCH_NOT_REQUIRED,
    provider_selection: WebProviderSelection | None = None,
    current_sensitive: bool = False,
) -> WebAnswerEvidenceConsumptionV1:
    """Decide publication from the actual answer and the admitted source set."""

    _identifier(consumption_id, field="consumption_id")
    _identifier(authenticated_turn_id, field="authenticated_turn_id")
    try:
        decision = WebCurrentnessDecision(currentness)
    except (TypeError, ValueError) as exc:
        raise WebAnswerEvidenceConsumptionError("currentness_closed") from exc
    if decision is WebCurrentnessDecision.SEARCH_BLOCKED_PRIVATE:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.BLOCKED,
            WebAnswerEvidenceReason.CURRENTNESS_BLOCKED,
        )
    try:
        facts, urls = _source_facts(admitted_source_urls, admitted_source_ids)
    except (TypeError, ValueError, WebProviderPolicyError, WebAnswerEvidenceConsumptionError):
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.BLOCKED,
            WebAnswerEvidenceReason.PRIVATE_OR_INVALID,
        )
    if provider_selection is not None and (
        provider_selection.decision is WebProviderDecision.UNAVAILABLE
        or provider_selection.admitted_source_count == 0
    ):
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.HOLD,
            WebAnswerEvidenceReason.PROVIDER_UNAVAILABLE,
            claim_support="empty",
            claim_currentness="empty",
            grounding="empty",
            contradiction="empty",
            citation_coverage="empty",
        )
    try:
        observed_claims = (
            tuple(claims)
            if claims is not None
            else claims_from_web_answer(answer, facts, current_sensitive=current_sensitive)
        )
    except WebAnswerEvidenceConsumptionError:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.BLOCKED,
            WebAnswerEvidenceReason.PRIVATE_OR_INVALID,
        )
    source_ids = tuple(fact["source_id"] for fact in facts)
    support = build_web_claim_support(
        f"{consumption_id}.support",
        authenticated_turn_id,
        claims=observed_claims,
        admitted_source_ids=source_ids,
        admitted_source_urls=urls,
        admitted_source_facts=facts,
    )
    if support.support is WebClaimSupportState.BLOCKED:
        reason = (
            WebAnswerEvidenceReason.UNKNOWN_SOURCE
            if support.reason.value == "unknown_source_id"
            else WebAnswerEvidenceReason.PRIVATE_OR_INVALID
        )
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.BLOCKED,
            reason,
        )
    grounding = build_web_evidence_grounding(
        f"{consumption_id}.grounding",
        authenticated_turn_id,
        claims=observed_claims,
        admitted_source_ids=source_ids,
        admitted_source_urls=urls,
        admitted_source_facts=facts,
    )
    contradiction = build_web_contradiction_coverage(
        f"{consumption_id}.contradiction",
        authenticated_turn_id,
        claims=observed_claims,
        admitted_source_ids=source_ids,
        admitted_source_urls=urls,
        admitted_source_facts=facts,
    )
    claim_currentness = build_web_claim_currentness(
        f"{consumption_id}.currentness",
        authenticated_turn_id,
        decision,
        observed_claims,
    )
    cited_urls = urls if source_ids else ()
    citation = build_web_citation_coverage(
        f"{consumption_id}.citation",
        authenticated_turn_id,
        urls,
        cited_urls,
    )
    compact: _ConsumptionCompact = {
        "claim_support": _closed(support.support),
        "claim_currentness": _closed(claim_currentness.admission),
        "grounding": _closed(grounding.grounding),
        "contradiction": _closed(contradiction.coverage),
        "citation_coverage": _closed(citation.coverage),
        "claim_count": support.claim_count,
        "supported_claim_count": support.supported_claim_count,
    }
    if claim_currentness.admission is WebClaimCurrentnessState.BLOCKED:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.BLOCKED,
            WebAnswerEvidenceReason.CURRENTNESS_BLOCKED,
        )
    if claim_currentness.admission is WebClaimCurrentnessState.HOLD:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.HOLD,
            WebAnswerEvidenceReason.CURRENTNESS_HOLD,
            **compact,
        )
    if contradiction.coverage is WebContradictionCoverageState.UNIVERSAL:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.HOLD,
            WebAnswerEvidenceReason.CONTRADICTION_UNIVERSAL,
            **compact,
        )
    if support.support is WebClaimSupportState.EMPTY:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.HOLD,
            WebAnswerEvidenceReason.CLAIMS_EMPTY,
            **compact,
        )
    if support.support is WebClaimSupportState.UNSUPPORTED:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.HOLD,
            WebAnswerEvidenceReason.CLAIMS_UNSUPPORTED,
            **compact,
        )
    if support.support is WebClaimSupportState.PARTIAL:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.HOLD,
            WebAnswerEvidenceReason.CLAIMS_PARTIAL,
            **compact,
        )
    if citation.coverage in {WebCitationCoverageState.EMPTY, WebCitationCoverageState.BLOCKED_PRIVATE}:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.HOLD,
            WebAnswerEvidenceReason.CITATION_EMPTY,
            **compact,
        )
    if provider_selection is not None and provider_selection.decision is WebProviderDecision.FALLBACK_USED:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.ADMITTED_DEGRADED,
            WebAnswerEvidenceReason.PROVIDER_FALLBACK,
            **compact,
        )
    if (
        provider_selection is not None and provider_selection.decision is WebProviderDecision.DEGRADED_PARTIAL
    ) or contradiction.coverage is WebContradictionCoverageState.PRESENT:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.ADMITTED_DEGRADED,
            WebAnswerEvidenceReason.CLAIMS_SUPPORTED,
            **compact,
        )
    if grounding.grounding is WebEvidenceGroundingState.BLOCKED:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebAnswerEvidencePublication.BLOCKED,
            WebAnswerEvidenceReason.PRIVATE_OR_INVALID,
        )
    return _result(
        consumption_id,
        authenticated_turn_id,
        WebAnswerEvidencePublication.ADMITTED,
        WebAnswerEvidenceReason.CLAIMS_SUPPORTED,
        **compact,
    )


consume_web_answer = consume_web_answer_evidence
publish_web_answer = published_web_answer

__all__ = (
    "WEB_ANSWER_EVIDENCE_CONSUMPTION_SCHEMA",
    "WebAnswerEvidenceConsumptionError",
    "WebAnswerEvidenceConsumptionV1",
    "WebAnswerEvidencePublication",
    "WebAnswerEvidenceReason",
    "claims_from_web_answer",
    "consume_web_answer",
    "consume_web_answer_evidence",
    "publish_web_answer",
    "published_web_answer",
)
