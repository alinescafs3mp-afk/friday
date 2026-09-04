"""Pure provider selection and degraded-fallback policy for public web research.

The policy consumes already-observed provider facts.  It does not call a
provider, resolve DNS, read files, or persist a report.  Its result is frozen
and deliberately distinguishes a real primary result, an honest named
fallback, a partial result, and complete unavailability.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qsl, urlsplit

from friday.web_research_contract import (
    MAX_DIRECT_RESEARCH_SOURCES,
    MAX_RESEARCH_ATTEMPTS,
    MAX_RESEARCH_SOURCES,
    research_attempt_counters_are_conserved,
)


class WebProviderPolicyError(ValueError):
    """Provider facts are unknown, unsafe, inconsistent, or out of bounds."""


class WebProviderId(StrEnum):
    """Provider ids admitted by the current WebSurfer provider chain."""

    YANDEX = "yandex"
    BRAVE = "brave"
    TAVILY = "tavily"
    SERPER = "serper"
    BRAVE_HTML = "brave-html"
    DUCKDUCKGO = "duckduckgo"
    WIKIPEDIA = "wikipedia"


class WebProviderStatus(StrEnum):
    """Closed statuses for one already-observed provider attempt."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    EMPTY = "empty"


class WebProviderDecision(StrEnum):
    """Closed provider-selection outcomes."""

    PRIMARY_OK = "primary_ok"
    FALLBACK_USED = "fallback_used"
    DEGRADED_PARTIAL = "degraded_partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    """Bounded facts returned by one provider adapter.

    ``source_count`` is the ordinary search-source count.  Direct source
    additions are tracked separately so the direct-source ceiling remains
    visible.  Attempt counters describe the complete observed attempt set and
    must conserve exactly according to ``web_research_contract``.
    """

    provider_id: str
    status: str | WebProviderStatus
    source_count: int = 0
    direct_source_count: int = 0
    requested_sources: int = 0
    completed_sources: int = 0
    failed_sources: int = 0
    timed_out_sources: int = 0
    required_filter_refused: bool = False
    source_urls: tuple[str, ...] = ()
    endpoint_url: str | None = None

    def __post_init__(self) -> None:
        provider_id = _provider_id(self.provider_id)
        status = _provider_status(self.status)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "status", status)
        _bounded_int(self.source_count, maximum=MAX_RESEARCH_SOURCES, field="source_count")
        _bounded_int(
            self.direct_source_count,
            maximum=MAX_DIRECT_RESEARCH_SOURCES,
            field="direct_source_count",
        )
        admitted = self.admitted_source_count
        if admitted > MAX_RESEARCH_SOURCES + MAX_DIRECT_RESEARCH_SOURCES:
            raise WebProviderPolicyError("admitted source count exceeds the research contract")
        if type(self.required_filter_refused) is not bool:
            raise WebProviderPolicyError("required_filter_refused must be boolean")

        # A compact observation with sources but no explicit counter facts is
        # safe to expand into one completed attempt per admitted source.  An
        # explicit non-zero counter is never inferred or repaired.
        counters = (
            self.requested_sources,
            self.completed_sources,
            self.failed_sources,
            self.timed_out_sources,
        )
        if admitted and counters == (0, 0, 0, 0):
            object.__setattr__(self, "requested_sources", admitted)
            object.__setattr__(self, "completed_sources", admitted)
        for field in (
            "requested_sources",
            "completed_sources",
            "failed_sources",
            "timed_out_sources",
        ):
            _bounded_int(getattr(self, field), maximum=MAX_RESEARCH_ATTEMPTS, field=field)
        if self.completed_sources > MAX_RESEARCH_SOURCES + MAX_DIRECT_RESEARCH_SOURCES:
            raise WebProviderPolicyError("completed_sources exceeds the source-row contract")
        if admitted > self.completed_sources:
            raise WebProviderPolicyError("admitted sources exceed completed source attempts")
        if not research_attempt_counters_are_conserved(self.counters_mapping()):
            raise WebProviderPolicyError("provider attempt counters are not conserved")

        urls = _public_urls(self.source_urls, field="source_urls")
        if urls and len(urls) != admitted:
            raise WebProviderPolicyError("source_urls must describe every admitted source")
        if len(set(urls)) != len(urls):
            raise WebProviderPolicyError("source_urls must not contain duplicates")
        object.__setattr__(self, "source_urls", urls)
        if self.endpoint_url is not None:
            object.__setattr__(
                self,
                "endpoint_url",
                validate_public_web_url(self.endpoint_url, field="endpoint_url"),
            )

        if (
            status in {WebProviderStatus.REFUSED, WebProviderStatus.UNAVAILABLE, WebProviderStatus.EMPTY}
            and admitted
        ):
            raise WebProviderPolicyError("refused, unavailable, and empty providers cannot admit sources")

    @property
    def admitted_source_count(self) -> int:
        return self.source_count + self.direct_source_count

    @property
    def provider(self) -> str:
        return self.provider_id

    @property
    def outcome(self) -> WebProviderStatus:
        return WebProviderStatus(self.status)

    def counters_mapping(self) -> dict[str, int]:
        return {
            "requested_sources": self.requested_sources,
            "completed_sources": self.completed_sources,
            "failed_sources": self.failed_sources,
            "timed_out_sources": self.timed_out_sources,
        }


@dataclass(frozen=True, slots=True)
class WebProviderSelection:
    """Frozen selected-provider result with the selected facts copied out."""

    decision: WebProviderDecision
    selected_provider_id: str | None
    source_count: int
    direct_source_count: int
    requested_sources: int
    completed_sources: int
    failed_sources: int
    timed_out_sources: int
    used_fallback: bool = False

    @property
    def outcome(self) -> WebProviderDecision:
        return WebProviderDecision(self.decision)

    @property
    def provider_id(self) -> str | None:
        return self.selected_provider_id

    @property
    def admitted_source_count(self) -> int:
        return self.source_count + self.direct_source_count

    @property
    def fallback_used(self) -> bool:
        return self.used_fallback


ProviderObservationFacts = ProviderObservation
WebProviderObservation = ProviderObservation
ProviderSelection = WebProviderSelection
ProviderOutcome = WebProviderDecision
ProviderPolicyDecision = WebProviderDecision


_KNOWN_PROVIDER_IDS = frozenset(item.value for item in WebProviderId)
_STATUS_ALIASES = {
    "ok": WebProviderStatus.COMPLETED.value,
    "success": WebProviderStatus.COMPLETED.value,
    "timeout": WebProviderStatus.TIMED_OUT.value,
    "filter_refused": WebProviderStatus.REFUSED.value,
}
_PRIVATE_HOST_SUFFIXES = (
    ".local",
    ".localhost",
    ".invalid",
    ".test",
    ".example",
    ".internal",
    ".home",
    ".lan",
)
_PRIVATE_HOSTS = frozenset({"localhost", "localhost.localdomain", "broadcasthost"})
_CREDENTIAL_QUERY_RE = re.compile(
    r"(?:^|[_-])(?:access[_-]?token|api[_-]?key|auth|cookie|credential|key|password|"
    r"secret|session|signature|sig|token)(?:$|[_-])",
    re.IGNORECASE,
)
_CREDENTIAL_PATH_RE = re.compile(
    r"(?:^|/)(?:access[_-]?token|api[_-]?key|password|secret|session|signature|token)(?:/|$)",
    re.IGNORECASE,
)
_MISSING = object()


def _provider_id(value: object) -> str:
    if type(value) is not str:
        raise WebProviderPolicyError("provider_id must be exact text")
    canonical = value.strip().casefold()
    if canonical not in _KNOWN_PROVIDER_IDS:
        raise WebProviderPolicyError("unknown provider_id")
    return canonical


def _provider_status(value: object) -> WebProviderStatus:
    if isinstance(value, WebProviderStatus):
        return value
    if type(value) is not str:
        raise WebProviderPolicyError("provider status must be exact text")
    canonical = value.strip().casefold().replace("-", "_")
    canonical = _STATUS_ALIASES.get(canonical, canonical)
    try:
        return WebProviderStatus(canonical)
    except ValueError as exc:
        raise WebProviderPolicyError("unknown provider status") from exc


def _bounded_int(value: object, *, maximum: int, field: str) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise WebProviderPolicyError(f"{field} is outside its closed bound")
    return value


def _public_url(value: object, *, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise WebProviderPolicyError(f"{field} must be a public URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise WebProviderPolicyError(f"{field} is malformed") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not hostname:
        raise WebProviderPolicyError(f"{field} must use an http(s) public endpoint")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise WebProviderPolicyError(f"{field} contains credentials or an unsafe fragment")
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if _CREDENTIAL_QUERY_RE.search(key):
            raise WebProviderPolicyError(f"{field} contains credential-bearing query data")
    if _CREDENTIAL_PATH_RE.search(parsed.path):
        raise WebProviderPolicyError(f"{field} contains a credential-bearing path")
    host = hostname.rstrip(".").casefold()
    if host in _PRIVATE_HOSTS or host.endswith(_PRIVATE_HOST_SUFFIXES):
        raise WebProviderPolicyError(f"{field} targets a private endpoint")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise WebProviderPolicyError(f"{field} targets a non-public address")
    if address is None and (host.isdigit() or re.fullmatch(r"[0-9.]+", host) is not None):
        raise WebProviderPolicyError(f"{field} has an unsafe numeric host")
    return value


def validate_public_web_url(value: object, *, field: str = "url") -> str:
    """Validate one observed URL without DNS or network access."""

    return _public_url(value, field=field)


def _public_urls(value: object, *, field: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebProviderPolicyError(f"{field} must be a sequence of URLs")
    if len(value) > MAX_RESEARCH_SOURCES + MAX_DIRECT_RESEARCH_SOURCES:
        raise WebProviderPolicyError(f"{field} exceeds the source bound")
    return tuple(validate_public_web_url(item, field=f"{field}_item") for item in value)


def _mapping_value(mapping: Mapping[str, object], *names: str, default: object = _MISSING) -> object:
    for name in names:
        if name in mapping:
            return mapping[name]
    if default is not _MISSING:
        return default
    raise WebProviderPolicyError(f"missing {names[0]}")


def _observation_from_mapping(value: Mapping[str, object]) -> ProviderObservation:
    aliases = {
        "provider_id",
        "provider",
        "name",
        "status",
        "outcome",
        "result",
        "source_count",
        "sources",
        "admitted_source_count",
        "direct_source_count",
        "direct_sources",
        "requested_sources",
        "requested",
        "completed_sources",
        "completed",
        "failed_sources",
        "failed",
        "timed_out_sources",
        "timed_out",
        "required_filter_refused",
        "filter_refused",
        "required_filter_refusal",
        "source_urls",
        "public_source_urls",
        "urls",
        "endpoint_url",
        "provider_endpoint",
        "endpoint",
    }
    unknown = sorted(set(value) - aliases)
    if unknown:
        raise WebProviderPolicyError(f"unknown provider observation fields: {', '.join(unknown)}")
    admitted = _mapping_value(value, "source_count", "sources", "admitted_source_count", default=0)
    direct = _mapping_value(value, "direct_source_count", "direct_sources", default=0)
    if "admitted_source_count" in value and "source_count" not in value and "sources" not in value:
        admitted_value = admitted
        if type(admitted_value) is not int:
            raise WebProviderPolicyError("admitted_source_count must be an integer")
        admitted = admitted_value
        direct = 0
    return ProviderObservation(
        provider_id=_mapping_value(value, "provider_id", "provider", "name"),  # type: ignore[arg-type]
        status=_mapping_value(value, "status", "outcome", "result"),  # type: ignore[arg-type]
        source_count=admitted,  # type: ignore[arg-type]
        direct_source_count=direct,  # type: ignore[arg-type]
        requested_sources=_mapping_value(value, "requested_sources", "requested", default=0),  # type: ignore[arg-type]
        completed_sources=_mapping_value(value, "completed_sources", "completed", default=0),  # type: ignore[arg-type]
        failed_sources=_mapping_value(value, "failed_sources", "failed", default=0),  # type: ignore[arg-type]
        timed_out_sources=_mapping_value(value, "timed_out_sources", "timed_out", default=0),  # type: ignore[arg-type]
        required_filter_refused=_mapping_value(
            value,
            "required_filter_refused",
            "filter_refused",
            "required_filter_refusal",
            default=False,
        ),  # type: ignore[arg-type]
        source_urls=_mapping_value(value, "source_urls", "public_source_urls", "urls", default=()),  # type: ignore[arg-type]
        endpoint_url=_mapping_value(value, "endpoint_url", "provider_endpoint", "endpoint", default=None),  # type: ignore[arg-type]
    )


def _coerce_observation(value: object) -> ProviderObservation:
    if isinstance(value, ProviderObservation):
        return value
    if isinstance(value, Mapping):
        return _observation_from_mapping(value)
    raise WebProviderPolicyError("provider observation must be a ProviderObservation or mapping")


def _partial_result(observation: ProviderObservation) -> bool:
    return observation.admitted_source_count > 0 and (
        observation.failed_sources > 0
        or observation.timed_out_sources > 0
        or observation.status
        in {WebProviderStatus.PARTIAL, WebProviderStatus.FAILED, WebProviderStatus.TIMED_OUT}
    )


def _completed_with_sources(observation: ProviderObservation) -> bool:
    return (
        observation.status is WebProviderStatus.COMPLETED
        and observation.admitted_source_count > 0
        and not observation.required_filter_refused
    )


def _selection(
    decision: WebProviderDecision, observation: ProviderObservation | None, *, used_fallback: bool
) -> WebProviderSelection:
    if observation is None:
        return WebProviderSelection(decision, None, 0, 0, 0, 0, 0, 0, used_fallback=False)
    return WebProviderSelection(
        decision=decision,
        selected_provider_id=observation.provider_id,
        source_count=observation.source_count,
        direct_source_count=observation.direct_source_count,
        requested_sources=observation.requested_sources,
        completed_sources=observation.completed_sources,
        failed_sources=observation.failed_sources,
        timed_out_sources=observation.timed_out_sources,
        used_fallback=used_fallback,
    )


def select_web_provider(
    primary: ProviderObservation | Mapping[str, object],
    fallback: ProviderObservation | Mapping[str, object] | None = None,
) -> WebProviderSelection:
    """Select one provider from already-observed primary and named fallback facts."""

    primary_observation = _coerce_observation(primary)
    fallback_observation = _coerce_observation(fallback) if fallback is not None else None
    if (
        fallback_observation is not None
        and fallback_observation.provider_id == primary_observation.provider_id
    ):
        raise WebProviderPolicyError("fallback provider must be distinct and named")

    if _completed_with_sources(primary_observation):
        decision = (
            WebProviderDecision.DEGRADED_PARTIAL
            if _partial_result(primary_observation)
            else WebProviderDecision.PRIMARY_OK
        )
        return _selection(decision, primary_observation, used_fallback=False)

    if _partial_result(primary_observation) and not primary_observation.required_filter_refused:
        return _selection(WebProviderDecision.DEGRADED_PARTIAL, primary_observation, used_fallback=False)

    if fallback_observation is not None:
        if _completed_with_sources(fallback_observation):
            decision = (
                WebProviderDecision.DEGRADED_PARTIAL
                if _partial_result(fallback_observation)
                else WebProviderDecision.FALLBACK_USED
            )
            return _selection(decision, fallback_observation, used_fallback=True)
        if _partial_result(fallback_observation) and not fallback_observation.required_filter_refused:
            return _selection(WebProviderDecision.DEGRADED_PARTIAL, fallback_observation, used_fallback=True)

    return _selection(WebProviderDecision.UNAVAILABLE, None, used_fallback=False)


def decide_web_provider(
    primary: ProviderObservation | Mapping[str, object],
    fallback: ProviderObservation | Mapping[str, object] | None = None,
) -> WebProviderSelection:
    """Alias for the explicit provider decision API."""

    return select_web_provider(primary, fallback)


def classify_provider_outcome(
    primary: ProviderObservation | Mapping[str, object],
    fallback: ProviderObservation | Mapping[str, object] | None = None,
) -> WebProviderSelection:
    """Compatibility alias for callers that name the result an outcome."""

    return select_web_provider(primary, fallback)


class WebProviderPolicy:
    """Stateless façade suitable for dependency injection."""

    @staticmethod
    def select(
        primary: ProviderObservation | Mapping[str, object],
        fallback: ProviderObservation | Mapping[str, object] | None = None,
    ) -> WebProviderSelection:
        return select_web_provider(primary, fallback)


choose_web_provider = select_web_provider
evaluate_web_provider = select_web_provider


__all__ = (
    "ProviderObservation",
    "ProviderObservationFacts",
    "ProviderOutcome",
    "ProviderPolicyDecision",
    "ProviderSelection",
    "WebProviderDecision",
    "WebProviderId",
    "WebProviderObservation",
    "WebProviderPolicy",
    "WebProviderPolicyError",
    "WebProviderSelection",
    "WebProviderStatus",
    "choose_web_provider",
    "classify_provider_outcome",
    "decide_web_provider",
    "evaluate_web_provider",
    "select_web_provider",
    "validate_public_web_url",
)
