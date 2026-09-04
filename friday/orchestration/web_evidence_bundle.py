"""Immutable, body-free evidence contract for bounded public-web research.

This module is intentionally a pure boundary.  It does not fetch URLs, read
files, persist evidence, or choose a provider.  A research worker can build a
validated bundle from already observed public metadata and pass that bundle to
later synthesis code.
"""

from __future__ import annotations

import ipaddress
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any, NoReturn, cast
from urllib.parse import urlsplit

from friday.web_research_contract import MAX_OUTBOUND_WEB_QUERY_CHARS, MAX_RESEARCH_SOURCES

WEB_EVIDENCE_BUNDLE_SCHEMA = "friday.web-evidence-bundle.v1"
MIN_QUERY_PLAN = 2
MAX_QUERY_PLAN = 8
MAX_SOURCE_TITLE_CHARS = 300
MAX_SOURCE_PUBLISHER_CHARS = 200
MAX_TASK_TOPIC_CHARS = 1_000
MAX_CLAIM_CHARS = 2_000
MAX_REFERENCE_CHARS = 160
MAX_REFERENCES_PER_SOURCE = 32
MAX_CLAIMS = 128
MAX_CONTRADICTIONS = 64
MAX_MISSING_EVIDENCE = 64
MAX_PROVIDER_OUTCOMES = 16
MAX_URL_CHARS = 4_096

_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_URL_RE = re.compile(r"(?i)(?<![\w])(?:[a-z][a-z0-9+.-]*://[^\s<>\"']+)")
_PROTOCOL_RELATIVE_URL_RE = re.compile(r"(?i)(?<![\w:])//[^\s<>\"']+")
_PATH_PREFIX = r"(?:^|[\s(\[\"'=,:])"
_POSIX_PATH_RE = re.compile(rf"{_PATH_PREFIX}/(?!/)[^\s<>\"']*")
_HOME_PATH_RE = re.compile(rf"{_PATH_PREFIX}~(?:/|\\)[^\s<>\"']*")
_WINDOWS_PATH_RE = re.compile(rf"(?i){_PATH_PREFIX}(?:[a-z]:[\\/]|\\\\)[^\s<>\"']*")
_PRIVATE_FILENAME_RE = re.compile(
    r"(?i)(?<![\w.-])[\w .-]{1,128}\.(?:7z|csv|doc|docx|gz|jpeg|jpg|json|odt|pdf|png|ppt|pptx|rar|rtf|tar|txt|xls|xlsx|zip)(?![\w])"
)
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")
_NON_PUBLIC_HOST_SUFFIXES = (
    ".local",
    ".localhost",
    ".invalid",
    ".test",
    ".example",
    ".internal",
    ".home",
    ".lan",
)
_NON_PUBLIC_HOSTS = frozenset({"localhost", "localhost.localdomain", "broadcasthost"})
_MISSING = object()


class WebEvidenceBundleError(ValueError):
    """A value is outside the immutable public-web evidence contract."""


class WebEvidenceState(StrEnum):
    """Common evidence states accepted by the claim contract."""

    PROVEN = "proven"
    SUPPORTED = "supported"
    PARTIAL = "partial"
    CONTRADICTED = "contradicted"
    UNKNOWN = "unknown"
    UNVERIFIED = "unverified"


def _fail(field: str, detail: str = "invalid") -> NoReturn:
    raise WebEvidenceBundleError(f"{field}_{detail}")


def _exact_text(value: object, *, field: str, maximum: int, allow_empty: bool = False) -> str:
    if type(value) is not str:
        _fail(field)
    if not allow_empty and not value:
        _fail(field, "empty")
    if value != value.strip():
        _fail(field, "whitespace")
    if len(value) > maximum:
        _fail(field, "too_long")
    if any(unicodedata.category(character).startswith("C") for character in value):
        _fail(field, "control")
    return cast(str, value)


def _safe_text(value: object, *, field: str, maximum: int, allow_empty: bool = False) -> str:
    text = _exact_text(value, field=field, maximum=maximum, allow_empty=allow_empty)
    if _POSIX_PATH_RE.search(text) or _HOME_PATH_RE.search(text) or _WINDOWS_PATH_RE.search(text):
        _fail(field, "path")
    return text


def _safe_identifier(value: object, *, field: str) -> str:
    if type(value) is not str or _ID_RE.fullmatch(value) is None:
        _fail(field, "id")
    return cast(str, value)


def _safe_token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _TOKEN_RE.fullmatch(value.casefold()) is None:
        _fail(field, "token")
    return str(value)


def _digest(value: object, *, field: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail(field, "digest")
    return cast(str, value)


def _sequence(value: object, *, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail(field, "sequence")
    return cast(Sequence[Any], value)


def _field(mapping: Mapping[str, Any], *names: str, default: object = _MISSING) -> object:
    for name in names:
        if name in mapping:
            return mapping[name]
    if default is not _MISSING:
        return default
    _fail(names[0], "missing")


def _public_url(value: object, *, field: str, allow_fragment: bool = False) -> str:
    text = _exact_text(value, field=field, maximum=MAX_URL_CHARS)
    if _UNSAFE_CONTROL_RE.search(text):
        _fail(field, "whitespace")
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise WebEvidenceBundleError(f"{field}_malformed") from exc
    if parsed.scheme.casefold() not in {"http", "https"}:
        _fail(field, "scheme")
    if not hostname or parsed.username is not None or parsed.password is not None:
        _fail(field, "authority")
    if parsed.fragment and not allow_fragment:
        _fail(field, "fragment")
    host = hostname.rstrip(".").casefold()
    if (
        not host
        or host in _NON_PUBLIC_HOSTS
        or host.endswith(_NON_PUBLIC_HOST_SUFFIXES)
        or "%" in host
        or host.isdigit()
        or re.fullmatch(r"[0-9.]+", host) is not None
        or re.fullmatch(r"0x[0-9a-f]+", host) is not None
    ):
        _fail(field, "non_public_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        _fail(field, "non_public_host")
    return text


def _query(value: object, *, field: str) -> str:
    text = _safe_text(value, field=field, maximum=MAX_OUTBOUND_WEB_QUERY_CHARS)
    urls = tuple(_URL_RE.finditer(text))
    for match in urls:
        _public_url(match.group(0), field=f"{field}_url", allow_fragment=False)
    relative_urls = tuple(_PROTOCOL_RELATIVE_URL_RE.finditer(text))
    for match in relative_urls:
        _public_url(f"https:{match.group(0)}", field=f"{field}_url", allow_fragment=False)
    without_urls = _URL_RE.sub(" public-url ", text)
    without_urls = _PROTOCOL_RELATIVE_URL_RE.sub(" public-url ", without_urls)
    if _POSIX_PATH_RE.search(without_urls) or _HOME_PATH_RE.search(without_urls):
        _fail(field, "path")
    if _WINDOWS_PATH_RE.search(without_urls):
        _fail(field, "path")
    if _PRIVATE_FILENAME_RE.search(without_urls):
        _fail(field, "private_filename")
    return text


def _iso_datetime(value: object, *, field: str) -> str:
    text = _exact_text(value, field=field, maximum=80)
    normalized = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise WebEvidenceBundleError(f"{field}_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(field, "timezone")
    return text


def _optional_iso_date(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    text = _exact_text(value, field=field, maximum=80)
    try:
        date.fromisoformat(text)
    except ValueError:
        try:
            _iso_datetime(text, field=field)
        except WebEvidenceBundleError as exc:
            raise WebEvidenceBundleError(f"{field}_date") from exc
    return text


def _string_tuple(
    value: object,
    *,
    field: str,
    maximum: int,
    item_maximum: int,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    items = _sequence(value, field=field)
    if len(items) > maximum:
        _fail(field, "too_many")
    result = tuple(_safe_text(item, field=f"{field}_item", maximum=item_maximum) for item in items)
    if not allow_empty and not result:
        _fail(field, "empty")
    if len(set(result)) != len(result):
        _fail(field, "duplicate")
    return result


@dataclass(frozen=True, slots=True)
class WebEvidenceSourceV1:
    """Public locator metadata and passage references for one source."""

    source_id: str
    canonical_url: str
    title: str
    publisher_domain: str
    publication_or_update_date: str | None
    retrieved_at: str
    source_class: str
    content_digest: str
    relevant_passage_references: tuple[str, ...]

    @property
    def publisher(self) -> str:
        return self.publisher_domain

    @property
    def content_digest_sha256(self) -> str:
        return self.content_digest

    @property
    def passage_references(self) -> tuple[str, ...]:
        return self.relevant_passage_references

    def __post_init__(self) -> None:
        _safe_identifier(self.source_id, field="source_id")
        if _public_url(self.canonical_url, field="canonical_url") != self.canonical_url:
            _fail("canonical_url", "not_canonical")
        _safe_text(self.title, field="title", maximum=MAX_SOURCE_TITLE_CHARS)
        _safe_text(self.publisher_domain, field="publisher_domain", maximum=MAX_SOURCE_PUBLISHER_CHARS)
        _optional_iso_date(self.publication_or_update_date, field="publication_or_update_date")
        _iso_datetime(self.retrieved_at, field="retrieved_at")
        _safe_token(self.source_class, field="source_class")
        _digest(self.content_digest, field="content_digest")
        if type(self.relevant_passage_references) is not tuple:
            _fail("relevant_passage_references", "immutable")
        _string_tuple(
            self.relevant_passage_references,
            field="relevant_passage_references",
            maximum=MAX_REFERENCES_PER_SOURCE,
            item_maximum=MAX_REFERENCE_CHARS,
            allow_empty=False,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "publisher_domain": self.publisher_domain,
            "publication_or_update_date": self.publication_or_update_date,
            "retrieved_at": self.retrieved_at,
            "source_class": self.source_class,
            "content_digest": self.content_digest,
            "relevant_passage_references": list(self.relevant_passage_references),
        }


def _build_source(value: object) -> WebEvidenceSourceV1:
    if isinstance(value, WebEvidenceSourceV1):
        return value
    if not isinstance(value, Mapping):
        _fail("sources_item")
    return WebEvidenceSourceV1(
        source_id=_safe_identifier(_field(value, "source_id", "id"), field="source_id"),
        canonical_url=_public_url(_field(value, "canonical_url", "url"), field="canonical_url"),
        title=_safe_text(_field(value, "title"), field="title", maximum=MAX_SOURCE_TITLE_CHARS),
        publisher_domain=_safe_text(
            _field(value, "publisher_domain", "publisher", "domain"),
            field="publisher_domain",
            maximum=MAX_SOURCE_PUBLISHER_CHARS,
        ),
        publication_or_update_date=_optional_iso_date(
            _field(value, "publication_or_update_date", "publication_date", "updated_at", default=None),
            field="publication_or_update_date",
        ),
        retrieved_at=_iso_datetime(_field(value, "retrieved_at"), field="retrieved_at"),
        source_class=_safe_token(_field(value, "source_class"), field="source_class"),
        content_digest=_digest(
            _field(value, "content_digest", "content_digest_sha256", "sha256"),
            field="content_digest",
        ),
        relevant_passage_references=_string_tuple(
            _field(value, "relevant_passage_references", "passage_references", "passages"),
            field="relevant_passage_references",
            maximum=MAX_REFERENCES_PER_SOURCE,
            item_maximum=MAX_REFERENCE_CHARS,
            allow_empty=False,
        ),
    )


@dataclass(frozen=True, slots=True)
class WebEvidenceClaimV1:
    """One body-free normalized claim and its source bindings."""

    claim_id: str
    normalized_claim: str
    supporting_source_ids: tuple[str, ...]
    contradicting_source_ids: tuple[str, ...]
    evidence_state: str
    current_sensitive: bool

    @property
    def current_sensitive_flag(self) -> bool:
        return self.current_sensitive

    def __post_init__(self) -> None:
        _safe_identifier(self.claim_id, field="claim_id")
        _safe_text(self.normalized_claim, field="normalized_claim", maximum=MAX_CLAIM_CHARS)
        for field, value in (
            ("supporting_source_ids", self.supporting_source_ids),
            ("contradicting_source_ids", self.contradicting_source_ids),
        ):
            if type(value) is not tuple:
                _fail(field, "immutable")
            if len(set(value)) != len(value):
                _fail(field, "duplicate")
            for source_id in value:
                _safe_identifier(source_id, field=f"{field}_item")
        if set(self.supporting_source_ids).intersection(self.contradicting_source_ids):
            _fail("claim_source_ids", "overlap")
        _safe_token(self.evidence_state, field="evidence_state")
        if type(self.current_sensitive) is not bool:
            _fail("current_sensitive", "boolean")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "normalized_claim": self.normalized_claim,
            "supporting_source_ids": list(self.supporting_source_ids),
            "contradicting_source_ids": list(self.contradicting_source_ids),
            "evidence_state": self.evidence_state,
            "current_sensitive": self.current_sensitive,
        }


def _build_claim(value: object) -> WebEvidenceClaimV1:
    if isinstance(value, WebEvidenceClaimV1):
        return value
    if not isinstance(value, Mapping):
        _fail("claims_item")
    return WebEvidenceClaimV1(
        claim_id=_safe_identifier(_field(value, "claim_id", "id"), field="claim_id"),
        normalized_claim=_safe_text(
            _field(value, "normalized_claim", "claim"),
            field="normalized_claim",
            maximum=MAX_CLAIM_CHARS,
        ),
        supporting_source_ids=_string_tuple(
            _field(value, "supporting_source_ids", "supporting_sources", default=()),
            field="supporting_source_ids",
            maximum=MAX_RESEARCH_SOURCES,
            item_maximum=128,
        ),
        contradicting_source_ids=_string_tuple(
            _field(value, "contradicting_source_ids", "contradicting_sources", default=()),
            field="contradicting_source_ids",
            maximum=MAX_RESEARCH_SOURCES,
            item_maximum=128,
        ),
        evidence_state=_safe_token(_field(value, "evidence_state", "state"), field="evidence_state"),
        current_sensitive=cast(bool, _field(value, "current_sensitive", "current_sensitive_flag")),
    )


@dataclass(frozen=True, slots=True)
class WebProviderOutcomeV1:
    """Body-free result metadata for one provider."""

    provider_id: str
    status: str
    source_count: int

    @property
    def provider(self) -> str:
        return self.provider_id

    @property
    def outcome(self) -> str:
        return self.status

    def __post_init__(self) -> None:
        _safe_token(self.provider_id, field="provider_id")
        _safe_token(self.status, field="provider_status")
        if type(self.source_count) is not int or not 0 <= self.source_count <= MAX_RESEARCH_SOURCES:
            _fail("provider_source_count", "range")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "status": self.status,
            "source_count": self.source_count,
        }


def _build_provider_outcome(value: object) -> WebProviderOutcomeV1:
    if isinstance(value, WebProviderOutcomeV1):
        return value
    if not isinstance(value, Mapping):
        _fail("provider_outcomes_item")
    source_count = _field(value, "source_count", "sources", default=0)
    return WebProviderOutcomeV1(
        provider_id=_safe_token(_field(value, "provider_id", "provider", "name"), field="provider_id"),
        status=_safe_token(_field(value, "status", "outcome", "result"), field="provider_status"),
        source_count=cast(int, source_count),
    )


@dataclass(frozen=True, slots=True)
class WebEvidenceBundleV1:
    """Immutable, bounded evidence for one authenticated research turn."""

    research_id: str
    authenticated_turn_id: str
    task_topic: str
    freshness_requirement: str
    query_plan: tuple[str, ...]
    sources: tuple[WebEvidenceSourceV1, ...]
    claims: tuple[WebEvidenceClaimV1, ...]
    contradictions: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    coverage: float
    retrieved_at: str
    provider_outcomes: tuple[WebProviderOutcomeV1, ...]

    def __post_init__(self) -> None:
        _validate_bundle_fields(self)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": WEB_EVIDENCE_BUNDLE_SCHEMA,
            "research_id": self.research_id,
            "authenticated_turn_id": self.authenticated_turn_id,
            "task_topic": self.task_topic,
            "freshness_requirement": self.freshness_requirement,
            "query_plan": list(self.query_plan),
            "sources": [source.to_mapping() for source in self.sources],
            "claims": [claim.to_mapping() for claim in self.claims],
            "contradictions": list(self.contradictions),
            "missing_evidence": list(self.missing_evidence),
            "coverage": self.coverage,
            "retrieved_at": self.retrieved_at,
            "provider_outcomes": [outcome.to_mapping() for outcome in self.provider_outcomes],
        }


def _validate_bundle_fields(bundle: WebEvidenceBundleV1) -> None:
    _safe_identifier(bundle.research_id, field="research_id")
    _safe_identifier(bundle.authenticated_turn_id, field="authenticated_turn_id")
    _safe_text(bundle.task_topic, field="task_topic", maximum=MAX_TASK_TOPIC_CHARS)
    _safe_text(bundle.freshness_requirement, field="freshness_requirement", maximum=160)
    if type(bundle.query_plan) is not tuple:
        _fail("query_plan", "immutable")
    if not MIN_QUERY_PLAN <= len(bundle.query_plan) <= MAX_QUERY_PLAN:
        _fail("query_plan", "count")
    for query in bundle.query_plan:
        _query(query, field="query")
    normalized_queries = tuple(" ".join(query.casefold().split()) for query in bundle.query_plan)
    if len(set(normalized_queries)) != len(normalized_queries):
        _fail("query_plan", "duplicate")

    if type(bundle.sources) is not tuple or len(bundle.sources) > MAX_RESEARCH_SOURCES:
        _fail("sources", "bound")
    if any(not isinstance(source, WebEvidenceSourceV1) for source in bundle.sources):
        _fail("sources", "item")
    source_ids = tuple(source.source_id for source in bundle.sources)
    if len(set(source_ids)) != len(source_ids):
        _fail("sources", "duplicate_id")

    if type(bundle.claims) is not tuple or len(bundle.claims) > MAX_CLAIMS:
        _fail("claims", "bound")
    if any(not isinstance(claim, WebEvidenceClaimV1) for claim in bundle.claims):
        _fail("claims", "item")
    claim_ids = tuple(claim.claim_id for claim in bundle.claims)
    if len(set(claim_ids)) != len(claim_ids):
        _fail("claims", "duplicate_id")
    known_sources = set(source_ids)
    for claim in bundle.claims:
        referenced = set(claim.supporting_source_ids).union(claim.contradicting_source_ids)
        if not referenced.issubset(known_sources):
            _fail("claim_source_ids", "unknown")

    for field, value, maximum in (
        ("contradictions", bundle.contradictions, MAX_CONTRADICTIONS),
        ("missing_evidence", bundle.missing_evidence, MAX_MISSING_EVIDENCE),
    ):
        if type(value) is not tuple:
            _fail(field, "immutable")
        if len(value) > maximum:
            _fail(field, "bound")
        _string_tuple(value, field=field, maximum=maximum, item_maximum=MAX_CLAIM_CHARS)

    if type(bundle.coverage) not in {int, float} or isinstance(bundle.coverage, bool):
        _fail("coverage", "number")
    if not math.isfinite(float(bundle.coverage)) or not 0.0 <= float(bundle.coverage) <= 1.0:
        _fail("coverage", "range")
    _iso_datetime(bundle.retrieved_at, field="retrieved_at")

    if type(bundle.provider_outcomes) is not tuple or len(bundle.provider_outcomes) > MAX_PROVIDER_OUTCOMES:
        _fail("provider_outcomes", "bound")
    if any(not isinstance(outcome, WebProviderOutcomeV1) for outcome in bundle.provider_outcomes):
        _fail("provider_outcomes", "item")
    provider_ids = tuple(outcome.provider_id for outcome in bundle.provider_outcomes)
    if len(set(provider_ids)) != len(provider_ids):
        _fail("provider_outcomes", "duplicate_id")


def build_web_evidence_bundle(
    raw: Mapping[str, Any] | WebEvidenceBundleV1,
) -> WebEvidenceBundleV1:
    """Build and validate one immutable bundle from body-free mappings."""

    if isinstance(raw, WebEvidenceBundleV1):
        _validate_bundle_fields(raw)
        return raw
    if not isinstance(raw, Mapping):
        _fail("bundle")
    schema = raw.get("schema", WEB_EVIDENCE_BUNDLE_SCHEMA)
    if schema != WEB_EVIDENCE_BUNDLE_SCHEMA:
        _fail("schema")
    source_values = _sequence(_field(raw, "sources"), field="sources")
    claim_values = _sequence(_field(raw, "claims"), field="claims")
    provider_values = _sequence(_field(raw, "provider_outcomes"), field="provider_outcomes")
    bundle = WebEvidenceBundleV1(
        research_id=_safe_identifier(_field(raw, "research_id"), field="research_id"),
        authenticated_turn_id=_safe_identifier(
            _field(raw, "authenticated_turn_id"),
            field="authenticated_turn_id",
        ),
        task_topic=_safe_text(_field(raw, "task_topic"), field="task_topic", maximum=MAX_TASK_TOPIC_CHARS),
        freshness_requirement=_safe_text(
            _field(raw, "freshness_requirement"),
            field="freshness_requirement",
            maximum=160,
        ),
        query_plan=tuple(
            _query(query, field="query") for query in _sequence(_field(raw, "query_plan"), field="query_plan")
        ),
        sources=tuple(_build_source(source) for source in source_values),
        claims=tuple(_build_claim(claim) for claim in claim_values),
        contradictions=_string_tuple(
            _field(raw, "contradictions"),
            field="contradictions",
            maximum=MAX_CONTRADICTIONS,
            item_maximum=MAX_CLAIM_CHARS,
        ),
        missing_evidence=_string_tuple(
            _field(raw, "missing_evidence"),
            field="missing_evidence",
            maximum=MAX_MISSING_EVIDENCE,
            item_maximum=MAX_CLAIM_CHARS,
        ),
        coverage=raw.get("coverage", _MISSING),
        retrieved_at=_iso_datetime(_field(raw, "retrieved_at"), field="retrieved_at"),
        provider_outcomes=tuple(_build_provider_outcome(outcome) for outcome in provider_values),
    )
    return bundle


def validate_web_evidence_bundle(value: object) -> bool:
    """Return whether a mapping or bundle satisfies the complete contract."""

    try:
        build_web_evidence_bundle(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return True


__all__ = [
    "MAX_QUERY_PLAN",
    "MAX_RESEARCH_SOURCES",
    "MIN_QUERY_PLAN",
    "WEB_EVIDENCE_BUNDLE_SCHEMA",
    "WebEvidenceBundleError",
    "WebEvidenceBundleV1",
    "WebEvidenceClaimV1",
    "WebEvidenceSourceV1",
    "WebEvidenceState",
    "WebProviderOutcomeV1",
    "build_web_evidence_bundle",
    "validate_web_evidence_bundle",
]
