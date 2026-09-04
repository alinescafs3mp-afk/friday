"""Pure consumption gate for already-observed public-web research facts.

The gate consumes currentness, provider-selection, and optional evidence-bundle
facts.  It never fetches, reads, stores, or invents sources.  Invalid private
facts become a closed ``BLOCKED_PRIVATE`` result; invalid non-private evidence
cannot become a successful consumption result.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_evidence_bundle import (
    WebEvidenceBundleError,
    WebEvidenceBundleV1,
    build_web_evidence_bundle,
)
from friday.orchestration.web_provider_policy import (
    WebProviderDecision,
    WebProviderId,
    WebProviderSelection,
    validate_public_web_url,
)


class WebResearchConsumptionError(ValueError):
    """The consumption identity or closed input type is invalid."""


class WebResearchConsumptionState(StrEnum):
    """Closed usability states exposed to a requesting workflow."""

    CONSUMABLE = "consumable"
    CONSUMABLE_DEGRADED = "consumable_degraded"
    BLOCKED_PRIVATE = "blocked_private"
    UNAVAILABLE = "unavailable"


class WebResearchConsumptionReason(StrEnum):
    """Closed short explanations for one consumption state."""

    PRIMARY_SOURCES = "primary_sources"
    FALLBACK_SOURCES = "fallback_sources"
    PARTIAL_SOURCES = "partial_sources"
    CURRENTNESS_PRIVATE = "currentness_private"
    TOPIC_PRIVATE = "topic_private"
    SOURCE_FACT_PRIVATE = "source_fact_private"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NO_ADMITTED_SOURCES = "no_admitted_sources"
    PROVIDER_FACTS_INVALID = "provider_facts_invalid"
    EVIDENCE_INVALID = "evidence_invalid"
    EVIDENCE_MISMATCH = "evidence_mismatch"


@dataclass(frozen=True, slots=True)
class WebResearchConsumptionV1:
    """Frozen, body-free decision that public research may be consumed."""

    consumption_id: str
    authenticated_turn_id: str
    usability: WebResearchConsumptionState
    selected_provider_id: str | None
    admitted_source_count: int
    reason: WebResearchConsumptionReason

    @property
    def state(self) -> WebResearchConsumptionState:
        return self.usability

    @property
    def closed_usability(self) -> WebResearchConsumptionState:
        return self.usability

    @property
    def decision(self) -> WebResearchConsumptionState:
        return self.usability

    @property
    def closed_reason(self) -> WebResearchConsumptionReason:
        return self.reason

    @property
    def provider_id(self) -> str | None:
        return self.selected_provider_id

    def __post_init__(self) -> None:
        _safe_identity(self.consumption_id, field="consumption_id")
        _safe_identity(self.authenticated_turn_id, field="authenticated_turn_id")
        usability = _coerce_state(self.usability)
        reason = _coerce_reason(self.reason)
        object.__setattr__(self, "usability", usability)
        object.__setattr__(self, "reason", reason)
        if type(self.admitted_source_count) is not int or not 0 <= self.admitted_source_count <= 11:
            raise WebResearchConsumptionError("admitted_source_count is outside its closed bound")
        if self.selected_provider_id is not None:
            _provider_id(self.selected_provider_id)
        if usability in {
            WebResearchConsumptionState.BLOCKED_PRIVATE,
            WebResearchConsumptionState.UNAVAILABLE,
        } and (self.selected_provider_id is not None or self.admitted_source_count != 0):
            raise WebResearchConsumptionError("blocked and unavailable results cannot expose sources")
        if usability in {
            WebResearchConsumptionState.CONSUMABLE,
            WebResearchConsumptionState.CONSUMABLE_DEGRADED,
        } and (self.selected_provider_id is None or self.admitted_source_count < 1):
            raise WebResearchConsumptionError("consumable results need a provider and a source")


ConsumptionState = WebResearchConsumptionState
ConsumptionReason = WebResearchConsumptionReason
WebResearchConsumptionDecision = WebResearchConsumptionState
WebResearchConsumption = WebResearchConsumptionV1


_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_URL_RE = re.compile(r"(?i)(?<![\w])https?://[^\s<>\"']+")
_PRIVATE_FILENAME_RE = re.compile(
    r"(?i)(?<![\w.-])[\w .-]{1,128}\.(?:7z|csv|doc|docx|gz|jpeg|jpg|json|odt|pdf|png|ppt|pptx|rar|rtf|tar|txt|xls|xlsx|zip)(?![\w])"
)
_PRIVATE_PATH_RE = re.compile(
    r"(?:^|[\s(\[\"'=,:])/(?!/)[^\s<>\"']*"
    r"|(?:^|[\s(\[\"'=,:])~(?:[/\\])[^\s<>\"']*"
    r"|(?:^|[\s(\[\"'=,:])[a-z]:[/\\][^\s<>\"']*"
    r"|(?:^|\s)(?:private|local|workspace|archive|attachments?)[/\\][^\s]+"
    r"|(?:^|\s)(?:[\w.-]+[/\\]){2,}[\w.-]+",
    re.IGNORECASE,
)
_PRIVATE_IDENTIFIER_RE = re.compile(
    r"\b(?:private|secret|raw|file|path|job|task|run|conv(?:ersation)?|msg|message|archive|entity|"
    r"document|record)[_-][A-Za-z0-9_-]{3,}\b"
    r"|\b[0-9a-f]{32,64}\b"
    r"|\b[0-9a-f]{8}-[0-9a-f-]{17,}\b"
    r"|\b[0-9]{12,}\b",
    re.IGNORECASE,
)
_PRIVATE_DEICTIC_RE = re.compile(
    r"\b(?:this|that|these|those)(?!\s+(?:year|month|week|day|quarter|time))\b"
    r"|\b(?:here|there|above|below|attached|enclosed|my|our|local|the\s+attached)\b"
    r"|\b(?:эт(?:от|а|о|и|ому|им|ой|ом|их|ими)|т(?:от|а|о|и|ому|им|ой|ом|их|ими)|"
    r"здесь|там|выше|ниже|приложенн\w*|вложенн\w*|прикрепл\w*|мо(?:й|я|ё|е)|"
    r"наш\w*|локальн\w*)\b",
    re.IGNORECASE,
)
_PRIVATE_CONTEXT_RE = re.compile(
    r"\b(?:my|our|private|local)\s+(?:file|document|archive|notes?|report|folder|path)\b"
    r"|(?:мой|моего|моя|моё|нашего|личн\w*|локальн\w*)\s+"
    r"(?:файл\w*|документ\w*|архив\w*|заметк\w*|отч[её]т\w*|папк\w*)",
    re.IGNORECASE,
)
_MISSING = object()


def _safe_identity(value: object, *, field: str) -> str:
    if type(value) is not str or _IDENTITY_RE.fullmatch(value) is None:
        raise WebResearchConsumptionError(f"{field} must be a bounded opaque identifier")
    return value


def _provider_id(value: object) -> str:
    if type(value) is not str:
        raise WebResearchConsumptionError("selected_provider_id must be exact text")
    try:
        return WebProviderId(value.strip().casefold()).value
    except ValueError as exc:
        raise WebResearchConsumptionError("unknown selected provider") from exc


def _coerce_state(value: object) -> WebResearchConsumptionState:
    if isinstance(value, WebResearchConsumptionState):
        return value
    if type(value) is not str:
        raise WebResearchConsumptionError("usability must be a closed state")
    try:
        return WebResearchConsumptionState(value.strip().casefold())
    except ValueError as exc:
        raise WebResearchConsumptionError("unknown consumption state") from exc


def _coerce_reason(value: object) -> WebResearchConsumptionReason:
    if isinstance(value, WebResearchConsumptionReason):
        return value
    if type(value) is not str:
        raise WebResearchConsumptionError("reason must be a closed value")
    try:
        return WebResearchConsumptionReason(value.strip().casefold())
    except ValueError as exc:
        raise WebResearchConsumptionError("unknown consumption reason") from exc


def _private_text(value: object) -> bool:
    if type(value) is not str or any(ord(character) < 32 or ord(character) == 127 for character in value):
        return True
    without_urls = _URL_RE.sub(" public-url ", value)
    return bool(
        _PRIVATE_FILENAME_RE.search(without_urls)
        or _PRIVATE_PATH_RE.search(without_urls)
        or _PRIVATE_IDENTIFIER_RE.search(without_urls)
        or _PRIVATE_DEICTIC_RE.search(without_urls)
        or _PRIVATE_CONTEXT_RE.search(without_urls)
    )


def _topic_has_private_fact(value: object) -> bool:
    if _private_text(value):
        return True
    assert type(value) is str
    for match in _URL_RE.finditer(value):
        try:
            validate_public_web_url(match.group(0), field="topic_url")
        except (TypeError, ValueError):
            return True
    return False


def _source_urls(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebResearchConsumptionError("source_urls must be a URL sequence")
    if len(value) > 11:
        raise WebResearchConsumptionError("source_urls exceed the source bound")
    result = tuple(validate_public_web_url(item, field="source_url") for item in value)
    if len(set(result)) != len(result):
        raise WebResearchConsumptionError("source_urls contain duplicates")
    return result


def _bundle_private_facts(value: object) -> bool:
    if isinstance(value, WebEvidenceBundleV1):
        facts: list[object] = [value.task_topic]
        for source in value.sources:
            facts.extend((source.title, source.publisher_domain, *source.relevant_passage_references))
        return any(_private_text(fact) for fact in facts)
    if not isinstance(value, Mapping):
        return False
    if _topic_has_private_fact(value.get("task_topic", "")):
        return True
    sources = value.get("sources", ())
    if isinstance(sources, (str, bytes, bytearray)) or not isinstance(sources, Sequence):
        return False
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        url = source.get("canonical_url", source.get("url"))
        if url is not None:
            try:
                validate_public_web_url(url, field="source_url")
            except (TypeError, ValueError):
                return True
        for key in (
            "title",
            "publisher_domain",
            "publisher",
            "relevant_passage_references",
            "passage_references",
        ):
            fact = source.get(key, "")
            if isinstance(fact, Sequence) and not isinstance(fact, (str, bytes, bytearray)):
                if any(_private_text(item) for item in fact):
                    return True
            elif _private_text(fact):
                return True
    return False


def _coerce_provider_selection(value: object) -> WebProviderSelection | None:
    if isinstance(value, WebProviderSelection):
        return value
    if not isinstance(value, Mapping):
        raise WebResearchConsumptionError("provider selection must be a WebProviderSelection or mapping")
    allowed = {
        "decision",
        "outcome",
        "selected_provider_id",
        "provider_id",
        "source_count",
        "direct_source_count",
        "admitted_source_count",
        "requested_sources",
        "completed_sources",
        "failed_sources",
        "timed_out_sources",
        "used_fallback",
        "fallback_used",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise WebResearchConsumptionError("unknown provider selection fields")
    decision_value = value.get("decision", value.get("outcome", _MISSING))
    if decision_value is _MISSING:
        raise WebResearchConsumptionError("provider selection decision is missing")
    try:
        decision = WebProviderDecision(str(decision_value).strip().casefold())
    except ValueError as exc:
        raise WebResearchConsumptionError("unknown provider selection decision") from exc
    selected = value.get("selected_provider_id", value.get("provider_id"))
    if selected is not None:
        selected = _provider_id(selected)
    admitted = value.get("admitted_source_count")
    if admitted is None:
        source_count = value.get("source_count", 0)
        direct_source_count = value.get("direct_source_count", 0)
        if type(source_count) is not int or type(direct_source_count) is not int:
            raise WebResearchConsumptionError("provider source counts must be integers")
        admitted = source_count + direct_source_count
    if type(admitted) is not int or not 0 <= admitted <= 11:
        raise WebResearchConsumptionError("provider admitted source count is outside its bound")
    used_fallback = value.get("used_fallback", value.get("fallback_used", False))
    if type(used_fallback) is not bool:
        raise WebResearchConsumptionError("used_fallback must be boolean")
    return WebProviderSelection(
        decision=decision,
        selected_provider_id=selected,
        source_count=admitted,
        direct_source_count=0,
        requested_sources=0,
        completed_sources=0,
        failed_sources=0,
        timed_out_sources=0,
        used_fallback=used_fallback,
    )


def _valid_selection(selection: WebProviderSelection) -> bool:
    try:
        decision = WebProviderDecision(selection.decision)
        selected = (
            None if selection.selected_provider_id is None else _provider_id(selection.selected_provider_id)
        )
    except (TypeError, ValueError, WebResearchConsumptionError):
        return False
    admitted = selection.admitted_source_count
    if type(admitted) is not int or not 0 <= admitted <= 11:
        return False
    if decision is WebProviderDecision.UNAVAILABLE:
        return selected is None and admitted == 0
    return selected is not None and admitted > 0


def _bundle_source_count(value: WebEvidenceBundleV1 | Mapping[str, object]) -> int | None:
    if isinstance(value, WebEvidenceBundleV1):
        return len(value.sources)
    if isinstance(value, Mapping):
        sources = value.get("sources")
        if isinstance(sources, Sequence) and not isinstance(sources, (str, bytes, bytearray)):
            return len(sources)
    return None


def _result(
    consumption_id: str,
    authenticated_turn_id: str,
    state: WebResearchConsumptionState,
    reason: WebResearchConsumptionReason,
    *,
    provider_id: str | None = None,
    source_count: int = 0,
) -> WebResearchConsumptionV1:
    return WebResearchConsumptionV1(
        consumption_id=consumption_id,
        authenticated_turn_id=authenticated_turn_id,
        usability=state,
        selected_provider_id=provider_id,
        admitted_source_count=source_count,
        reason=reason,
    )


def build_web_research_consumption(
    consumption_id: str,
    authenticated_turn_id: str,
    currentness: WebCurrentnessDecision | str,
    provider_selection: WebProviderSelection | Mapping[str, object] | None,
    evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
    *,
    topic: str = "",
    source_urls: Sequence[str] = (),
) -> WebResearchConsumptionV1:
    """Build one closed consumption decision from observed facts only."""

    _safe_identity(consumption_id, field="consumption_id")
    _safe_identity(authenticated_turn_id, field="authenticated_turn_id")
    try:
        current = WebCurrentnessDecision(currentness)
    except (TypeError, ValueError) as exc:
        raise WebResearchConsumptionError("unknown currentness decision") from exc

    if current is WebCurrentnessDecision.SEARCH_BLOCKED_PRIVATE:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebResearchConsumptionState.BLOCKED_PRIVATE,
            WebResearchConsumptionReason.CURRENTNESS_PRIVATE,
        )
    if _topic_has_private_fact(topic):
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebResearchConsumptionState.BLOCKED_PRIVATE,
            WebResearchConsumptionReason.TOPIC_PRIVATE,
        )
    try:
        observed_urls = _source_urls(source_urls)
    except (TypeError, ValueError, WebResearchConsumptionError):
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebResearchConsumptionState.BLOCKED_PRIVATE,
            WebResearchConsumptionReason.SOURCE_FACT_PRIVATE,
        )
    if evidence_bundle is not None and _bundle_private_facts(evidence_bundle):
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebResearchConsumptionState.BLOCKED_PRIVATE,
            WebResearchConsumptionReason.SOURCE_FACT_PRIVATE,
        )

    try:
        selection = _coerce_provider_selection(provider_selection) if provider_selection is not None else None
    except WebResearchConsumptionError:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebResearchConsumptionState.UNAVAILABLE,
            WebResearchConsumptionReason.PROVIDER_FACTS_INVALID,
        )
    if selection is None:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebResearchConsumptionState.UNAVAILABLE,
            WebResearchConsumptionReason.PROVIDER_UNAVAILABLE,
        )
    if not _valid_selection(selection):
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebResearchConsumptionState.UNAVAILABLE,
            WebResearchConsumptionReason.PROVIDER_FACTS_INVALID,
        )
    if selection.decision is WebProviderDecision.UNAVAILABLE or selection.admitted_source_count == 0:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebResearchConsumptionState.UNAVAILABLE,
            WebResearchConsumptionReason.NO_ADMITTED_SOURCES,
        )
    if observed_urls and len(observed_urls) != selection.admitted_source_count:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebResearchConsumptionState.UNAVAILABLE,
            WebResearchConsumptionReason.EVIDENCE_MISMATCH,
        )
    if evidence_bundle is not None:
        try:
            bundle = (
                evidence_bundle
                if isinstance(evidence_bundle, WebEvidenceBundleV1)
                else build_web_evidence_bundle(evidence_bundle)
            )
        except (TypeError, ValueError, WebEvidenceBundleError):
            return _result(
                consumption_id,
                authenticated_turn_id,
                WebResearchConsumptionState.UNAVAILABLE,
                WebResearchConsumptionReason.EVIDENCE_INVALID,
            )
        if _bundle_source_count(bundle) != selection.admitted_source_count:
            return _result(
                consumption_id,
                authenticated_turn_id,
                WebResearchConsumptionState.UNAVAILABLE,
                WebResearchConsumptionReason.EVIDENCE_MISMATCH,
            )

    provider_id = _provider_id(selection.selected_provider_id)
    if selection.decision is WebProviderDecision.PRIMARY_OK:
        return _result(
            consumption_id,
            authenticated_turn_id,
            WebResearchConsumptionState.CONSUMABLE,
            WebResearchConsumptionReason.PRIMARY_SOURCES,
            provider_id=provider_id,
            source_count=selection.admitted_source_count,
        )
    if selection.decision is WebProviderDecision.FALLBACK_USED:
        reason = WebResearchConsumptionReason.FALLBACK_SOURCES
    else:
        reason = WebResearchConsumptionReason.PARTIAL_SOURCES
    return _result(
        consumption_id,
        authenticated_turn_id,
        WebResearchConsumptionState.CONSUMABLE_DEGRADED,
        reason,
        provider_id=provider_id,
        source_count=selection.admitted_source_count,
    )


def consume_web_research(
    consumption_id: str,
    authenticated_turn_id: str,
    currentness: WebCurrentnessDecision | str,
    provider_selection: WebProviderSelection | Mapping[str, object] | None,
    evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
    *,
    topic: str = "",
    source_urls: Sequence[str] = (),
) -> WebResearchConsumptionV1:
    """Alias for the explicit consumption builder."""

    return build_web_research_consumption(
        consumption_id,
        authenticated_turn_id,
        currentness,
        provider_selection,
        evidence_bundle,
        topic=topic,
        source_urls=source_urls,
    )


decide_web_research_consumption = build_web_research_consumption
evaluate_web_research_consumption = build_web_research_consumption


class WebResearchConsumptionPolicy:
    """Stateless façade for orchestration dependency injection."""

    @staticmethod
    def build(
        consumption_id: str,
        authenticated_turn_id: str,
        currentness: WebCurrentnessDecision | str,
        provider_selection: WebProviderSelection | Mapping[str, object] | None,
        evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
        *,
        topic: str = "",
        source_urls: Sequence[str] = (),
    ) -> WebResearchConsumptionV1:
        return build_web_research_consumption(
            consumption_id,
            authenticated_turn_id,
            currentness,
            provider_selection,
            evidence_bundle,
            topic=topic,
            source_urls=source_urls,
        )


__all__ = (
    "ConsumptionReason",
    "ConsumptionState",
    "WebResearchConsumption",
    "WebResearchConsumptionDecision",
    "WebResearchConsumptionError",
    "WebResearchConsumptionPolicy",
    "WebResearchConsumptionReason",
    "WebResearchConsumptionState",
    "WebResearchConsumptionV1",
    "build_web_research_consumption",
    "consume_web_research",
    "decide_web_research_consumption",
    "evaluate_web_research_consumption",
)
