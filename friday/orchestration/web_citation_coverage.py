"""Pure host-coverage contract for already-admitted public-web citations.

The builder only validates supplied URLs and compares their lexical hostnames.
It performs no DNS lookup, public-suffix lookup, network request, file I/O, or
live retrieval wiring.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from friday.orchestration.web_evidence_bundle import (
    WebEvidenceBundleError,
    WebEvidenceBundleV1,
    build_web_evidence_bundle,
)
from friday.orchestration.web_provider_policy import validate_public_web_url
from friday.web_research_contract import MAX_RESEARCH_SOURCES


class WebCitationCoverageError(ValueError):
    """The coverage identity or source representation is outside the contract."""


class WebCitationCoverageState(StrEnum):
    """Closed coverage outcomes for one admitted public-source set."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    EMPTY = "empty"
    BLOCKED_PRIVATE = "blocked_private"


class WebCitationCoverageReason(StrEnum):
    """Closed short reasons for a coverage result."""

    ALL_ADMITTED_HOSTS_CITED = "all_admitted_hosts_cited"
    SOME_ADMITTED_HOSTS_CITED = "some_admitted_hosts_cited"
    NO_ADMITTED_HOSTS = "no_admitted_hosts"
    NO_CITED_PUBLIC_HOSTS = "no_cited_public_hosts"
    PRIVATE_URL = "private_url"
    EVIDENCE_INVALID = "evidence_invalid"
    HOST_SET_MISMATCH = "host_set_mismatch"


@dataclass(frozen=True, slots=True)
class WebCitationCoverageV1:
    """Frozen body-free host coverage for citations over admitted sources."""

    coverage_id: str
    authenticated_turn_id: str
    coverage: WebCitationCoverageState
    cited_host_count: int
    admitted_host_count: int
    reason: WebCitationCoverageReason

    @property
    def state(self) -> WebCitationCoverageState:
        return self.coverage

    @property
    def closed_coverage(self) -> WebCitationCoverageState:
        return self.coverage

    @property
    def decision(self) -> WebCitationCoverageState:
        return self.coverage

    @property
    def closed_reason(self) -> WebCitationCoverageReason:
        return self.reason

    def __post_init__(self) -> None:
        _identity(self.coverage_id, field="coverage_id")
        _identity(self.authenticated_turn_id, field="authenticated_turn_id")
        coverage = _coverage(self.coverage)
        reason = _reason(self.reason)
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "reason", reason)
        _count(self.cited_host_count, field="cited_host_count")
        _count(self.admitted_host_count, field="admitted_host_count")
        if coverage in {WebCitationCoverageState.EMPTY, WebCitationCoverageState.BLOCKED_PRIVATE} and (
            self.cited_host_count or self.admitted_host_count
        ):
            raise WebCitationCoverageError("empty and blocked coverage cannot expose host counts")
        if coverage in {WebCitationCoverageState.COMPLETE, WebCitationCoverageState.PARTIAL} and not (
            self.admitted_host_count and self.cited_host_count
        ):
            raise WebCitationCoverageError("non-empty coverage needs cited and admitted hosts")


CitationCoverageState = WebCitationCoverageState
CitationCoverageReason = WebCitationCoverageReason
WebCitationCoverage = WebCitationCoverageV1
WebCitationCoverageDecision = WebCitationCoverageState


_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MISSING = object()


def _identity(value: object, *, field: str) -> str:
    if type(value) is not str or _IDENTITY_RE.fullmatch(value) is None:
        raise WebCitationCoverageError(f"{field} must be a bounded opaque identifier")
    return value


def _coverage(value: object) -> WebCitationCoverageState:
    if isinstance(value, WebCitationCoverageState):
        return value
    if type(value) is not str:
        raise WebCitationCoverageError("coverage must be a closed value")
    try:
        return WebCitationCoverageState(value.strip().casefold())
    except ValueError as exc:
        raise WebCitationCoverageError("unknown coverage value") from exc


def _reason(value: object) -> WebCitationCoverageReason:
    if isinstance(value, WebCitationCoverageReason):
        return value
    if type(value) is not str:
        raise WebCitationCoverageError("reason must be a closed value")
    try:
        return WebCitationCoverageReason(value.strip().casefold())
    except ValueError as exc:
        raise WebCitationCoverageError("unknown coverage reason") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_RESEARCH_SOURCES:
        raise WebCitationCoverageError(f"{field} is outside its closed bound")
    return value


def _source_sequence(value: object, *, field: str) -> tuple[object, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebCitationCoverageError(f"{field} must be a bounded URL sequence")
    if len(value) > MAX_RESEARCH_SOURCES:
        raise WebCitationCoverageError(f"{field} exceeds the source bound")
    return tuple(value)


def _url_from_item(item: object, *, field: str) -> object:
    if not isinstance(item, Mapping):
        return item
    keys = set(item)
    if keys - {"canonical_url", "url"}:
        raise WebCitationCoverageError(f"{field} contains unknown source fields")
    if "canonical_url" in item:
        return item["canonical_url"]
    return item.get("url")


def _host_set(value: object, *, field: str) -> frozenset[str]:
    hosts: set[str] = set()
    for index, item in enumerate(_source_sequence(value, field=field)):
        raw_url = _url_from_item(item, field=f"{field}[{index}]")
        try:
            validated = validate_public_web_url(raw_url, field=f"{field}[{index}]")
            hostname = urlsplit(validated).hostname
        except (TypeError, ValueError) as exc:
            raise WebCitationCoverageError(f"{field}[{index}] is not a public URL") from exc
        if not hostname:
            raise WebCitationCoverageError(f"{field}[{index}] has no host")
        hosts.add(hostname.rstrip(".").casefold())
    return frozenset(hosts)


def _bundle_urls(value: object) -> tuple[str, ...]:
    if isinstance(value, WebEvidenceBundleV1):
        return tuple(source.canonical_url for source in value.sources)
    if not isinstance(value, Mapping):
        raise WebCitationCoverageError("evidence_bundle must be a bundle or mapping")
    try:
        bundle = build_web_evidence_bundle(value)
    except (TypeError, ValueError, WebEvidenceBundleError) as exc:
        raise WebCitationCoverageError("evidence bundle is invalid") from exc
    return tuple(source.canonical_url for source in bundle.sources)


def _result(
    coverage_id: str,
    authenticated_turn_id: str,
    coverage: WebCitationCoverageState,
    reason: WebCitationCoverageReason,
    *,
    cited: int = 0,
    admitted: int = 0,
) -> WebCitationCoverageV1:
    return WebCitationCoverageV1(
        coverage_id=coverage_id,
        authenticated_turn_id=authenticated_turn_id,
        coverage=coverage,
        cited_host_count=cited,
        admitted_host_count=admitted,
        reason=reason,
    )


def build_web_citation_coverage(
    coverage_id: str,
    authenticated_turn_id: str,
    admitted_source_urls: Sequence[object] | WebEvidenceBundleV1 | Mapping[str, object] | None = None,
    cited_source_urls: Sequence[object] = (),
    *,
    evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
) -> WebCitationCoverageV1:
    """Build host coverage from already-observed admitted and cited URLs."""

    _identity(coverage_id, field="coverage_id")
    _identity(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if evidence_bundle is not None:
            bundle_urls = _bundle_urls(evidence_bundle)
            if admitted_source_urls is None:
                admitted_source_urls = bundle_urls
            elif _host_set(admitted_source_urls, field="admitted_source_urls") != _host_set(
                bundle_urls, field="evidence_bundle_sources"
            ):
                raise WebCitationCoverageError("admitted URLs disagree with evidence bundle")
        elif isinstance(admitted_source_urls, (WebEvidenceBundleV1, Mapping)):
            admitted_source_urls = _bundle_urls(admitted_source_urls)
        admitted_hosts = _host_set(admitted_source_urls or (), field="admitted_source_urls")
        cited_hosts = _host_set(cited_source_urls, field="cited_source_urls")
    except WebCitationCoverageError:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebCitationCoverageState.BLOCKED_PRIVATE,
            WebCitationCoverageReason.PRIVATE_URL,
        )
    except (TypeError, ValueError):
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebCitationCoverageState.BLOCKED_PRIVATE,
            WebCitationCoverageReason.PRIVATE_URL,
        )

    if not admitted_hosts:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebCitationCoverageState.EMPTY,
            WebCitationCoverageReason.NO_ADMITTED_HOSTS,
        )
    if not cited_hosts:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebCitationCoverageState.EMPTY,
            WebCitationCoverageReason.NO_CITED_PUBLIC_HOSTS,
        )
    if cited_hosts == admitted_hosts:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebCitationCoverageState.COMPLETE,
            WebCitationCoverageReason.ALL_ADMITTED_HOSTS_CITED,
            cited=len(cited_hosts),
            admitted=len(admitted_hosts),
        )
    return _result(
        coverage_id,
        authenticated_turn_id,
        WebCitationCoverageState.PARTIAL,
        WebCitationCoverageReason.SOME_ADMITTED_HOSTS_CITED
        if cited_hosts.issubset(admitted_hosts)
        else WebCitationCoverageReason.HOST_SET_MISMATCH,
        cited=len(cited_hosts),
        admitted=len(admitted_hosts),
    )


def calculate_web_citation_coverage(
    coverage_id: str,
    authenticated_turn_id: str,
    admitted_source_urls: Sequence[object] | WebEvidenceBundleV1 | Mapping[str, object] | None = None,
    cited_source_urls: Sequence[object] = (),
    *,
    evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
) -> WebCitationCoverageV1:
    """Alias for the explicit citation-coverage builder."""

    return build_web_citation_coverage(
        coverage_id,
        authenticated_turn_id,
        admitted_source_urls,
        cited_source_urls,
        evidence_bundle=evidence_bundle,
    )


decide_web_citation_coverage = build_web_citation_coverage
evaluate_web_citation_coverage = build_web_citation_coverage


class WebCitationCoveragePolicy:
    """Stateless façade for orchestration dependency injection."""

    @staticmethod
    def build(
        coverage_id: str,
        authenticated_turn_id: str,
        admitted_source_urls: Sequence[object] | WebEvidenceBundleV1 | Mapping[str, object] | None = None,
        cited_source_urls: Sequence[object] = (),
        *,
        evidence_bundle: WebEvidenceBundleV1 | Mapping[str, object] | None = None,
    ) -> WebCitationCoverageV1:
        return build_web_citation_coverage(
            coverage_id,
            authenticated_turn_id,
            admitted_source_urls,
            cited_source_urls,
            evidence_bundle=evidence_bundle,
        )


__all__ = (
    "CitationCoverageReason",
    "CitationCoverageState",
    "WebCitationCoverage",
    "WebCitationCoverageDecision",
    "WebCitationCoverageError",
    "WebCitationCoveragePolicy",
    "WebCitationCoverageReason",
    "WebCitationCoverageState",
    "WebCitationCoverageV1",
    "build_web_citation_coverage",
    "calculate_web_citation_coverage",
    "decide_web_citation_coverage",
    "evaluate_web_citation_coverage",
)
