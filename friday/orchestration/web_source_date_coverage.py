"""Pure publication-date coverage for observed public-web sources.

The builder only validates supplied source metadata.  It does not fetch pages,
derive a freshness window, read files, persist evidence, or wire retrieval.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from friday.orchestration.web_evidence_bundle import (
    MAX_RESEARCH_SOURCES,
    WebEvidenceSourceV1,
)
from friday.orchestration.web_provider_policy import validate_public_web_url


class WebSourceDateCoverageError(ValueError):
    """The coverage identity or equivalent source facts are invalid."""


class WebSourceDateCoverageState(StrEnum):
    """Closed source publication-date coverage outcomes."""

    EMPTY = "empty"
    DATED = "dated"
    PARTIAL = "partial"
    UNDATED = "undated"
    BLOCKED = "blocked"


class WebSourceDateCoverageReason(StrEnum):
    """Closed short reasons for one source-date coverage result."""

    NO_SOURCES = "no_sources"
    ALL_SOURCES_DATED = "all_sources_dated"
    SOME_SOURCES_DATED = "some_sources_dated"
    NO_DATED_SOURCES = "no_dated_sources"
    PRIVATE_URL = "private_url"
    INVALID_DATE = "invalid_date"
    INVALID_SOURCE_FACTS = "invalid_source_facts"


@dataclass(frozen=True, slots=True)
class WebSourceDateCoverageV1:
    """Frozen body-free publication-date coverage for one authenticated turn."""

    coverage_id: str
    authenticated_turn_id: str
    coverage: WebSourceDateCoverageState
    dated_source_count: int
    source_count: int
    reason: WebSourceDateCoverageReason

    @property
    def state(self) -> WebSourceDateCoverageState:
        return self.coverage

    @property
    def closed_coverage(self) -> WebSourceDateCoverageState:
        return self.coverage

    @property
    def decision(self) -> WebSourceDateCoverageState:
        return self.coverage

    @property
    def closed_reason(self) -> WebSourceDateCoverageReason:
        return self.reason

    def __post_init__(self) -> None:
        _identity(self.coverage_id, field="coverage_id")
        _identity(self.authenticated_turn_id, field="authenticated_turn_id")
        coverage = _coverage(self.coverage)
        reason = _reason(self.reason)
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "reason", reason)
        _count(self.dated_source_count, field="dated_source_count")
        _count(self.source_count, field="source_count")
        if self.dated_source_count > self.source_count:
            raise WebSourceDateCoverageError("dated_source_count exceeds source_count")
        if coverage is WebSourceDateCoverageState.EMPTY and self.source_count != 0:
            raise WebSourceDateCoverageError("empty coverage must have no sources")
        if coverage is WebSourceDateCoverageState.BLOCKED and (
            self.dated_source_count != 0 or self.source_count != 0
        ):
            raise WebSourceDateCoverageError("blocked coverage cannot expose source counts")
        if coverage is WebSourceDateCoverageState.DATED and (
            self.source_count == 0 or self.dated_source_count != self.source_count
        ):
            raise WebSourceDateCoverageError("dated coverage needs every source dated")
        if coverage is WebSourceDateCoverageState.PARTIAL and not (
            0 < self.dated_source_count < self.source_count
        ):
            raise WebSourceDateCoverageError("partial coverage needs dated and undated sources")
        if coverage is WebSourceDateCoverageState.UNDATED and not (
            self.source_count > 0 and self.dated_source_count == 0
        ):
            raise WebSourceDateCoverageError("undated coverage needs sources and no dates")


SourceDateCoverageState = WebSourceDateCoverageState
SourceDateCoverageReason = WebSourceDateCoverageReason
WebSourceDateCoverage = WebSourceDateCoverageV1
WebSourceDateCoverageDecision = WebSourceDateCoverageState


_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MISSING = object()


def _identity(value: object, *, field: str) -> str:
    if type(value) is not str or _IDENTITY_RE.fullmatch(value) is None:
        raise WebSourceDateCoverageError(f"{field} must be a bounded opaque identifier")
    return value


def _coverage(value: object) -> WebSourceDateCoverageState:
    if isinstance(value, WebSourceDateCoverageState):
        return value
    if type(value) is not str:
        raise WebSourceDateCoverageError("coverage must be a closed value")
    try:
        return WebSourceDateCoverageState(value.strip().casefold())
    except ValueError as exc:
        raise WebSourceDateCoverageError("unknown coverage value") from exc


def _reason(value: object) -> WebSourceDateCoverageReason:
    if isinstance(value, WebSourceDateCoverageReason):
        return value
    if type(value) is not str:
        raise WebSourceDateCoverageError("reason must be a closed value")
    try:
        return WebSourceDateCoverageReason(value.strip().casefold())
    except ValueError as exc:
        raise WebSourceDateCoverageError("unknown coverage reason") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_RESEARCH_SOURCES:
        raise WebSourceDateCoverageError(f"{field} is outside its closed bound")
    return value


def _bounded_text(value: object, *, field: str, maximum: int = 80) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise WebSourceDateCoverageError(f"{field} is invalid")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise WebSourceDateCoverageError(f"{field} is invalid")
    return value


def _publication_date(value: object) -> str | None:
    if value is None:
        return None
    text = _bounded_text(value, field="publication_or_update_date")
    try:
        date.fromisoformat(text)
    except ValueError:
        normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise WebSourceDateCoverageError("publication_or_update_date is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise WebSourceDateCoverageError("publication_or_update_date needs a timezone") from None
    return text


def _retrieved_at(value: object) -> str:
    text = _bounded_text(value, field="retrieved_at")
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WebSourceDateCoverageError("retrieved_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WebSourceDateCoverageError("retrieved_at needs a timezone")
    return text


def _source_from_mapping(value: Mapping[str, object]) -> tuple[str, str | None]:
    allowed = {
        "source_id",
        "id",
        "canonical_url",
        "url",
        "title",
        "publisher_domain",
        "publisher",
        "domain",
        "publication_or_update_date",
        "publication_date",
        "updated_at",
        "retrieved_at",
        "source_class",
        "content_digest",
        "content_digest_sha256",
        "sha256",
        "relevant_passage_references",
        "passage_references",
        "passages",
    }
    if set(value) - allowed:
        raise WebSourceDateCoverageError("source contains unknown fields")
    raw_id = value.get("source_id", value.get("id", _MISSING))
    if raw_id is _MISSING:
        raise WebSourceDateCoverageError("source_id is missing")
    source_id = _identity(raw_id, field="source_id")
    raw_url = value.get("canonical_url", value.get("url", _MISSING))
    if raw_url is _MISSING:
        raise WebSourceDateCoverageError("canonical_url is missing")
    try:
        validate_public_web_url(raw_url, field="canonical_url")
    except (TypeError, ValueError) as exc:
        raise WebSourceDateCoverageError("private or invalid source URL") from exc
    raw_date = value.get(
        "publication_or_update_date",
        value.get("publication_date", value.get("updated_at", None)),
    )
    publication_date = _publication_date(raw_date)
    if "retrieved_at" in value:
        _retrieved_at(value["retrieved_at"])
    return source_id, publication_date


def _source(value: object) -> tuple[str, str | None]:
    if isinstance(value, WebEvidenceSourceV1):
        source_id = _identity(value.source_id, field="source_id")
        try:
            validate_public_web_url(value.canonical_url, field="canonical_url")
        except (TypeError, ValueError) as exc:
            raise WebSourceDateCoverageError("private or invalid source URL") from exc
        publication_date = _publication_date(value.publication_or_update_date)
        _retrieved_at(value.retrieved_at)
        return source_id, publication_date
    if isinstance(value, Mapping):
        return _source_from_mapping(value)
    raise WebSourceDateCoverageError("source fact is invalid")


def _sources(value: object) -> tuple[tuple[str, str | None], ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebSourceDateCoverageError("sources must be a sequence")
    if len(value) > MAX_RESEARCH_SOURCES:
        raise WebSourceDateCoverageError("sources exceed the closed bound")
    result = tuple(_source(item) for item in value)
    source_ids = tuple(source_id for source_id, _ in result)
    if len(set(source_ids)) != len(source_ids):
        raise WebSourceDateCoverageError("source ids must be unique")
    return result


def _result(
    coverage_id: str,
    authenticated_turn_id: str,
    coverage: WebSourceDateCoverageState,
    reason: WebSourceDateCoverageReason,
    *,
    dated: int = 0,
    sources: int = 0,
) -> WebSourceDateCoverageV1:
    return WebSourceDateCoverageV1(
        coverage_id=coverage_id,
        authenticated_turn_id=authenticated_turn_id,
        coverage=coverage,
        dated_source_count=dated,
        source_count=sources,
        reason=reason,
    )


def build_web_source_date_coverage(
    coverage_id: str,
    authenticated_turn_id: str,
    sources: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
    *,
    source_facts: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
) -> WebSourceDateCoverageV1:
    """Build date coverage from already-observed source metadata."""

    _identity(coverage_id, field="coverage_id")
    _identity(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if sources is not None and source_facts is not None:
            raise WebSourceDateCoverageError("sources and source_facts cannot both be supplied")
        observed_sources = _sources(
            source_facts if source_facts is not None else (sources if sources is not None else ())
        )
    except WebSourceDateCoverageError as exc:
        message = str(exc).casefold()
        if "url" in message:
            reason = WebSourceDateCoverageReason.PRIVATE_URL
        elif "date" in message:
            reason = WebSourceDateCoverageReason.INVALID_DATE
        else:
            reason = WebSourceDateCoverageReason.INVALID_SOURCE_FACTS
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebSourceDateCoverageState.BLOCKED,
            reason,
        )
    except (TypeError, ValueError):
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebSourceDateCoverageState.BLOCKED,
            WebSourceDateCoverageReason.INVALID_SOURCE_FACTS,
        )

    source_count = len(observed_sources)
    if source_count == 0:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebSourceDateCoverageState.EMPTY,
            WebSourceDateCoverageReason.NO_SOURCES,
        )
    dated = sum(publication_date is not None for _, publication_date in observed_sources)
    if dated == source_count:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebSourceDateCoverageState.DATED,
            WebSourceDateCoverageReason.ALL_SOURCES_DATED,
            dated=dated,
            sources=source_count,
        )
    if dated:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebSourceDateCoverageState.PARTIAL,
            WebSourceDateCoverageReason.SOME_SOURCES_DATED,
            dated=dated,
            sources=source_count,
        )
    return _result(
        coverage_id,
        authenticated_turn_id,
        WebSourceDateCoverageState.UNDATED,
        WebSourceDateCoverageReason.NO_DATED_SOURCES,
        sources=source_count,
    )


def assess_web_source_date_coverage(
    coverage_id: str,
    authenticated_turn_id: str,
    sources: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
    *,
    source_facts: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
) -> WebSourceDateCoverageV1:
    """Alias for the explicit source-date coverage builder."""

    return build_web_source_date_coverage(
        coverage_id, authenticated_turn_id, sources, source_facts=source_facts
    )


calculate_web_source_date_coverage = build_web_source_date_coverage
decide_web_source_date_coverage = build_web_source_date_coverage
evaluate_web_source_date_coverage = build_web_source_date_coverage


class WebSourceDateCoveragePolicy:
    """Stateless façade for orchestration dependency injection."""

    @staticmethod
    def build(
        coverage_id: str,
        authenticated_turn_id: str,
        sources: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
        *,
        source_facts: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
    ) -> WebSourceDateCoverageV1:
        return build_web_source_date_coverage(
            coverage_id, authenticated_turn_id, sources, source_facts=source_facts
        )


__all__ = (
    "SourceDateCoverageReason",
    "SourceDateCoverageState",
    "WebSourceDateCoverage",
    "WebSourceDateCoverageDecision",
    "WebSourceDateCoverageError",
    "WebSourceDateCoveragePolicy",
    "WebSourceDateCoverageReason",
    "WebSourceDateCoverageState",
    "WebSourceDateCoverageV1",
    "assess_web_source_date_coverage",
    "build_web_source_date_coverage",
    "calculate_web_source_date_coverage",
    "decide_web_source_date_coverage",
    "evaluate_web_source_date_coverage",
)
