"""Pure passage-reference coverage for observed public-web sources.

The builder only validates supplied source metadata.  It does not fetch pages,
invent passages, read files, persist evidence, or wire retrieval.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from friday.orchestration.web_evidence_bundle import (
    MAX_REFERENCES_PER_SOURCE,
    MAX_RESEARCH_SOURCES,
    WebEvidenceSourceV1,
)
from friday.orchestration.web_provider_policy import validate_public_web_url


class WebPassageReferenceCoverageError(ValueError):
    """The coverage identity or equivalent source facts are invalid."""


class WebPassageReferenceCoverageState(StrEnum):
    """Closed passage-reference coverage outcomes."""

    EMPTY = "empty"
    REFERENCED = "referenced"
    PARTIAL = "partial"
    BARE = "bare"
    BLOCKED = "blocked"


class WebPassageReferenceCoverageReason(StrEnum):
    """Closed short reasons for one passage-reference coverage result."""

    NO_SOURCES = "no_sources"
    ALL_SOURCES_REFERENCED = "all_sources_referenced"
    SOME_SOURCES_REFERENCED = "some_sources_referenced"
    NO_SOURCES_REFERENCED = "no_sources_referenced"
    PRIVATE_URL = "private_url"
    EMPTY_REFERENCE = "empty_reference"
    INVALID_SOURCE_FACTS = "invalid_source_facts"


@dataclass(frozen=True, slots=True)
class WebPassageReferenceCoverageV1:
    """Frozen body-free passage-reference coverage for one authenticated turn."""

    coverage_id: str
    authenticated_turn_id: str
    coverage: WebPassageReferenceCoverageState
    referenced_source_count: int
    source_count: int
    reason: WebPassageReferenceCoverageReason

    @property
    def state(self) -> WebPassageReferenceCoverageState:
        return self.coverage

    @property
    def closed_coverage(self) -> WebPassageReferenceCoverageState:
        return self.coverage

    @property
    def decision(self) -> WebPassageReferenceCoverageState:
        return self.coverage

    @property
    def closed_reason(self) -> WebPassageReferenceCoverageReason:
        return self.reason

    def __post_init__(self) -> None:
        _identity(self.coverage_id, field="coverage_id")
        _identity(self.authenticated_turn_id, field="authenticated_turn_id")
        coverage = _coverage(self.coverage)
        reason = _reason(self.reason)
        object.__setattr__(self, "coverage", coverage)
        object.__setattr__(self, "reason", reason)
        _count(self.referenced_source_count, field="referenced_source_count")
        _count(self.source_count, field="source_count")
        if self.referenced_source_count > self.source_count:
            raise WebPassageReferenceCoverageError("referenced_source_count exceeds source_count")
        if coverage is WebPassageReferenceCoverageState.EMPTY and self.source_count != 0:
            raise WebPassageReferenceCoverageError("empty coverage must have no sources")
        if coverage is WebPassageReferenceCoverageState.BLOCKED and (
            self.referenced_source_count != 0 or self.source_count != 0
        ):
            raise WebPassageReferenceCoverageError("blocked coverage cannot expose source counts")
        if coverage is WebPassageReferenceCoverageState.REFERENCED and (
            self.source_count == 0 or self.referenced_source_count != self.source_count
        ):
            raise WebPassageReferenceCoverageError("referenced coverage needs every source referenced")
        if coverage is WebPassageReferenceCoverageState.PARTIAL and not (
            0 < self.referenced_source_count < self.source_count
        ):
            raise WebPassageReferenceCoverageError("partial coverage needs referenced and bare sources")
        if coverage is WebPassageReferenceCoverageState.BARE and not (
            self.source_count > 0 and self.referenced_source_count == 0
        ):
            raise WebPassageReferenceCoverageError("bare coverage needs sources and no references")


PassageReferenceCoverageState = WebPassageReferenceCoverageState
PassageReferenceCoverageReason = WebPassageReferenceCoverageReason
WebPassageReferenceCoverage = WebPassageReferenceCoverageV1
WebPassageReferenceCoverageDecision = WebPassageReferenceCoverageState


_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_MISSING = object()


def _identity(value: object, *, field: str) -> str:
    if type(value) is not str or _IDENTITY_RE.fullmatch(value) is None:
        raise WebPassageReferenceCoverageError(f"{field} must be a bounded opaque identifier")
    return value


def _coverage(value: object) -> WebPassageReferenceCoverageState:
    if isinstance(value, WebPassageReferenceCoverageState):
        return value
    if type(value) is not str:
        raise WebPassageReferenceCoverageError("coverage must be a closed value")
    try:
        return WebPassageReferenceCoverageState(value.strip().casefold())
    except ValueError as exc:
        raise WebPassageReferenceCoverageError("unknown coverage value") from exc


def _reason(value: object) -> WebPassageReferenceCoverageReason:
    if isinstance(value, WebPassageReferenceCoverageReason):
        return value
    if type(value) is not str:
        raise WebPassageReferenceCoverageError("reason must be a closed value")
    try:
        return WebPassageReferenceCoverageReason(value.strip().casefold())
    except ValueError as exc:
        raise WebPassageReferenceCoverageError("unknown coverage reason") from exc


def _count(value: object, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_RESEARCH_SOURCES:
        raise WebPassageReferenceCoverageError(f"{field} is outside its closed bound")
    return value


def _references(value: object, *, required: bool) -> tuple[str, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebPassageReferenceCoverageError("relevant_passage_references must be a sequence")
    if len(value) > MAX_REFERENCES_PER_SOURCE:
        raise WebPassageReferenceCoverageError("relevant_passage_references exceed the closed bound")
    result: list[str] = []
    for reference in value:
        if (
            type(reference) is not str
            or not reference
            or reference != reference.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in reference)
        ):
            raise WebPassageReferenceCoverageError("empty or invalid passage reference")
        result.append(reference)
    if len(set(result)) != len(result):
        raise WebPassageReferenceCoverageError("relevant_passage_references must be unique")
    if required and not result:
        raise WebPassageReferenceCoverageError("passage references are missing")
    return tuple(result)


def _source_from_mapping(value: Mapping[str, object]) -> tuple[str, tuple[str, ...]]:
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
        raise WebPassageReferenceCoverageError("source contains unknown fields")
    raw_id = value.get("source_id", value.get("id", _MISSING))
    if raw_id is _MISSING:
        raise WebPassageReferenceCoverageError("source_id is missing")
    source_id = _identity(raw_id, field="source_id")
    raw_url = value.get("canonical_url", value.get("url", _MISSING))
    if raw_url is _MISSING:
        raise WebPassageReferenceCoverageError("canonical_url is missing")
    try:
        validate_public_web_url(raw_url, field="canonical_url")
    except (TypeError, ValueError) as exc:
        raise WebPassageReferenceCoverageError("private or invalid source URL") from exc
    raw_references = value.get(
        "relevant_passage_references",
        value.get("passage_references", value.get("passages", ())),
    )
    return source_id, _references(raw_references, required=False)


def _source(value: object) -> tuple[str, tuple[str, ...]]:
    if isinstance(value, WebEvidenceSourceV1):
        source_id = _identity(value.source_id, field="source_id")
        try:
            validate_public_web_url(value.canonical_url, field="canonical_url")
        except (TypeError, ValueError) as exc:
            raise WebPassageReferenceCoverageError("private or invalid source URL") from exc
        return source_id, _references(value.relevant_passage_references, required=False)
    if isinstance(value, Mapping):
        return _source_from_mapping(value)
    raise WebPassageReferenceCoverageError("source fact is invalid")


def _sources(value: object) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise WebPassageReferenceCoverageError("sources must be a sequence")
    if len(value) > MAX_RESEARCH_SOURCES:
        raise WebPassageReferenceCoverageError("sources exceed the closed bound")
    result = tuple(_source(item) for item in value)
    source_ids = tuple(source_id for source_id, _ in result)
    if len(set(source_ids)) != len(source_ids):
        raise WebPassageReferenceCoverageError("source ids must be unique")
    return result


def _result(
    coverage_id: str,
    authenticated_turn_id: str,
    coverage: WebPassageReferenceCoverageState,
    reason: WebPassageReferenceCoverageReason,
    *,
    referenced: int = 0,
    sources: int = 0,
) -> WebPassageReferenceCoverageV1:
    return WebPassageReferenceCoverageV1(
        coverage_id=coverage_id,
        authenticated_turn_id=authenticated_turn_id,
        coverage=coverage,
        referenced_source_count=referenced,
        source_count=sources,
        reason=reason,
    )


def build_web_passage_reference_coverage(
    coverage_id: str,
    authenticated_turn_id: str,
    sources: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
    *,
    source_facts: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
) -> WebPassageReferenceCoverageV1:
    """Build passage-reference coverage from observed source metadata."""

    _identity(coverage_id, field="coverage_id")
    _identity(authenticated_turn_id, field="authenticated_turn_id")
    try:
        if sources is not None and source_facts is not None:
            raise WebPassageReferenceCoverageError("sources and source_facts cannot both be supplied")
        observed_sources = _sources(
            source_facts if source_facts is not None else (sources if sources is not None else ())
        )
    except WebPassageReferenceCoverageError as exc:
        message = str(exc).casefold()
        if "url" in message:
            reason = WebPassageReferenceCoverageReason.PRIVATE_URL
        elif "empty" in message or "reference" in message:
            reason = WebPassageReferenceCoverageReason.EMPTY_REFERENCE
        else:
            reason = WebPassageReferenceCoverageReason.INVALID_SOURCE_FACTS
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebPassageReferenceCoverageState.BLOCKED,
            reason,
        )
    except (TypeError, ValueError):
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebPassageReferenceCoverageState.BLOCKED,
            WebPassageReferenceCoverageReason.INVALID_SOURCE_FACTS,
        )

    source_count = len(observed_sources)
    if source_count == 0:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebPassageReferenceCoverageState.EMPTY,
            WebPassageReferenceCoverageReason.NO_SOURCES,
        )
    referenced = sum(bool(references) for _, references in observed_sources)
    if referenced == source_count:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebPassageReferenceCoverageState.REFERENCED,
            WebPassageReferenceCoverageReason.ALL_SOURCES_REFERENCED,
            referenced=referenced,
            sources=source_count,
        )
    if referenced:
        return _result(
            coverage_id,
            authenticated_turn_id,
            WebPassageReferenceCoverageState.PARTIAL,
            WebPassageReferenceCoverageReason.SOME_SOURCES_REFERENCED,
            referenced=referenced,
            sources=source_count,
        )
    return _result(
        coverage_id,
        authenticated_turn_id,
        WebPassageReferenceCoverageState.BARE,
        WebPassageReferenceCoverageReason.NO_SOURCES_REFERENCED,
        sources=source_count,
    )


def assess_web_passage_reference_coverage(
    coverage_id: str,
    authenticated_turn_id: str,
    sources: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
    *,
    source_facts: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
) -> WebPassageReferenceCoverageV1:
    """Alias for the explicit passage-reference coverage builder."""

    return build_web_passage_reference_coverage(
        coverage_id, authenticated_turn_id, sources, source_facts=source_facts
    )


calculate_web_passage_reference_coverage = build_web_passage_reference_coverage
decide_web_passage_reference_coverage = build_web_passage_reference_coverage
evaluate_web_passage_reference_coverage = build_web_passage_reference_coverage


class WebPassageReferenceCoveragePolicy:
    """Stateless façade for orchestration dependency injection."""

    @staticmethod
    def build(
        coverage_id: str,
        authenticated_turn_id: str,
        sources: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
        *,
        source_facts: Sequence[WebEvidenceSourceV1 | Mapping[str, object]] | None = None,
    ) -> WebPassageReferenceCoverageV1:
        return build_web_passage_reference_coverage(
            coverage_id, authenticated_turn_id, sources, source_facts=source_facts
        )


__all__ = (
    "PassageReferenceCoverageReason",
    "PassageReferenceCoverageState",
    "WebPassageReferenceCoverage",
    "WebPassageReferenceCoverageDecision",
    "WebPassageReferenceCoverageError",
    "WebPassageReferenceCoveragePolicy",
    "WebPassageReferenceCoverageReason",
    "WebPassageReferenceCoverageState",
    "WebPassageReferenceCoverageV1",
    "assess_web_passage_reference_coverage",
    "build_web_passage_reference_coverage",
    "calculate_web_passage_reference_coverage",
    "decide_web_passage_reference_coverage",
    "evaluate_web_passage_reference_coverage",
)
