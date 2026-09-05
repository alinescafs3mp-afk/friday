"""Process-private public-web evidence for a future file comparison lane.

The ordinary ``web_research`` tool is deliberately not used here: that tool can
capture pages into Raw/Inbox.  This adapter has only two dependencies -- the
authorization reader and a storage-free ``WebSurfer.research``-compatible
reader -- and returns an in-memory projection.  It does not publish an answer
and it is not a router entry point.

Most importantly, the outbound query is not accepted as an argument.  Code can
seal it only from the current user message: either one explicitly quoted public
clause, or the independent public-web topic of an admitted current-file
comparison turn.  File bytes never enter the sealer.  A later executor must
present that same message, actor and conversation scope again before the
single outbound call.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from friday.orchestration.current_file_web_query import (
    extract_compare_current_file_public_web_query,
)
from friday.orchestration.supervisor_contracts import SupervisorContractError, parse_query_intent
from friday.orchestration.turn_context_runtime import current_primary_authenticated_turn_context
from friday.orchestration.web_currentness_policy import WebCurrentnessDecision
from friday.orchestration.web_provider_policy import (
    ProviderObservation,
    WebProviderPolicyError,
    WebProviderStatus,
    select_web_provider,
)
from friday.orchestration.web_research_consumption import (
    WebResearchConsumptionState,
    build_web_research_consumption,
)
from friday.permissions import ActorContext, AuthorizationService

LOGGER = logging.getLogger("friday.orchestration.transient_web_comparison")

TRANSIENT_WEB_SECURITY_ID = "web.compare.transient"
TRANSIENT_WEB_ADAPTER_ID = "transient_web_comparison"
TRANSIENT_WEB_PLAN_SCHEMA = "friday.transient-web-comparison-plan.v1"
TRANSIENT_WEB_EVIDENCE_SCHEMA = "friday.transient-web-comparison-evidence.v1"

_MAX_SOURCES = 3
_MAX_CURRENT_MESSAGE_UTF8_BYTES = 16_384
_MAX_URL_CHARS = 2_048
_MAX_TITLE_CHARS = 300
_MAX_UPSTREAM_SOURCE_TEXT_UTF8_BYTES = 64_000
_MAX_SOURCE_TEXT_UTF8_BYTES = 12_000
_MAX_TOTAL_TEXT_UTF8_BYTES = 24_000
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_CONTROL_OR_SPACE_RE = re.compile(r"[\x00-\x20\x7f]")
_PUBLIC_MARKER_RE = re.compile(r"(?:публичный веб-запрос|public web query)[ \t]*:", re.IGNORECASE)
_PUBLIC_CLAUSE_RE = re.compile(
    r"^[ \t]*(?:публичный веб-запрос|public web query)[ \t]*:[ \t]*"
    r"(?:«(?P<guillemet>[^»\r\n]+)»|\"(?P<quote>[^\"\r\n]+)\")[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_PROCESS_AUTHORITY = object()
_SEAL_KEY = secrets.token_bytes(32)
_CONSUMPTION_ID = "transient.web.comparison"
_CONSUMPTION_TURN_ID = "transient.web.comparison"
_CONSUMPTION_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


class TransientWebComparisonError(ValueError):
    """The transient read is outside its closed authority/evidence contract."""


class TransientWebEvidenceStatus(StrEnum):
    SOURCED = "sourced"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"


class TransientWebUnavailableReason(StrEnum):
    NONE = "none"
    SEARCH_TIMED_OUT = "search_timed_out"
    SEARCH_FAILED = "search_failed"
    NO_READABLE_SOURCE = "no_readable_source"
    PROVIDER_ERROR = "provider_error"


class TransientWebResearch(Protocol):
    """The intentionally narrow, persistence-free WebSurfer factorization."""

    async def research(self, query: str, *, max_sources: int = _MAX_SOURCES) -> Mapping[str, Any]: ...


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str, *, label: str) -> str:
    if type(value) is not str:
        raise TransientWebComparisonError(f"{label} must be exact text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise TransientWebComparisonError(f"{label} must be valid UTF-8 text") from exc
    return _sha256_bytes(encoded)


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise TransientWebComparisonError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_sha256(value: object) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _scope_sha256(conversation_id: str | None) -> str:
    if conversation_id is None:
        return _sha256_bytes(b"conversation:none")
    if type(conversation_id) is not str or not conversation_id.strip() or len(conversation_id) > 256:
        raise TransientWebComparisonError("conversation_id must be bounded exact text or None")
    return _sha256_text(conversation_id, label="conversation_id")


def _actor_sha256(actor: ActorContext) -> str:
    if not isinstance(actor, ActorContext):
        raise TransientWebComparisonError("actor must be an authenticated ActorContext")
    if not actor.user_id or not actor.own_id or not actor.preset_key:
        raise TransientWebComparisonError("actor identity is incomplete")
    return _canonical_sha256(
        {
            "tenant": actor.user_id,
            "principal": actor.own_id,
            "preset": actor.preset_key,
            "shared_tenant": actor.shared_tenant,
        }
    )


def _message_bytes(current_user_message: object) -> bytes:
    # ``bytes`` is deliberately not decoded here.  File/archive evidence enters
    # the system as bytes; accepting it would create a second query-minting path.
    if type(current_user_message) is not str:
        raise TransientWebComparisonError("current_user_message must be exact user-authored text")
    try:
        encoded = current_user_message.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise TransientWebComparisonError("current_user_message must be valid UTF-8 text") from exc
    if not encoded or len(encoded) > _MAX_CURRENT_MESSAGE_UTF8_BYTES or b"\x00" in encoded:
        raise TransientWebComparisonError("current_user_message is empty or exceeds its closed bound")
    return encoded


def _inside_code_fence(message: str, offset: int) -> bool:
    prefix = message[:offset]
    return prefix.count("```") % 2 == 1 or prefix.count("~~~") % 2 == 1


def _extract_explicit_public_query(current_user_message: object) -> tuple[str, str]:
    encoded = _message_bytes(current_user_message)
    assert type(current_user_message) is str  # narrowed by _message_bytes
    markers = tuple(_PUBLIC_MARKER_RE.finditer(current_user_message))
    matches = tuple(_PUBLIC_CLAUSE_RE.finditer(current_user_message))
    if len(markers) != 1 or len(matches) != 1:
        raise TransientWebComparisonError("exactly one standalone quoted public-web clause is required")
    match = matches[0]
    if not (match.start() <= markers[0].start() < match.end()) or _inside_code_fence(
        current_user_message, match.start()
    ):
        raise TransientWebComparisonError("the public-web clause is not an executable standalone clause")
    raw_query = match.group("guillemet") if match.group("guillemet") is not None else match.group("quote")
    try:
        query = parse_query_intent(raw_query, label="explicit public web query")
    except SupervisorContractError as exc:
        raise TransientWebComparisonError(
            "the explicit public-web query is not safe natural language"
        ) from exc
    return query, _sha256_bytes(encoded)


def _extract_compare_current_file_public_query(current_user_message: object) -> tuple[str, str]:
    encoded = _message_bytes(current_user_message)
    query = extract_compare_current_file_public_web_query(current_user_message)
    if not query:
        raise TransientWebComparisonError("compare-current-file public-web topic is not separable")
    return query, _sha256_bytes(encoded)


def _bound_query_candidates(current_user_message: object) -> tuple[tuple[str, str], ...]:
    """Return every closed query this exact message may mint."""

    candidates: list[tuple[str, str]] = []
    for extractor in (_extract_explicit_public_query, _extract_compare_current_file_public_query):
        try:
            candidates.append(extractor(current_user_message))
        except TransientWebComparisonError:
            continue
    return tuple(candidates)


def _plan_seal(fields: Mapping[str, object]) -> str:
    payload = json.dumps(
        dict(fields),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hmac.new(_SEAL_KEY, payload, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class SealedPublicWebQuery:
    """Process-owned query authority minted from one current-message clause."""

    current_message_sha256: str
    query_sha256: str
    actor_sha256: str
    conversation_scope_sha256: str
    max_sources: int
    _query: str = field(repr=False, compare=False)
    _seal: str = field(repr=False, compare=False)
    _process_authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("current_message_sha256", self.current_message_sha256),
            ("query_sha256", self.query_sha256),
            ("actor_sha256", self.actor_sha256),
            ("conversation_scope_sha256", self.conversation_scope_sha256),
        ):
            _require_digest(value, label=label)
        if (
            self._process_authority is not _PROCESS_AUTHORITY
            or self.max_sources != _MAX_SOURCES
            or _sha256_text(self._query, label="sealed query") != self.query_sha256
            or not hmac.compare_digest(self._seal, _plan_seal(self._seal_fields()))
        ):
            raise TransientWebComparisonError("public-web query seal is invalid")

    def _seal_fields(self) -> dict[str, object]:
        return {
            "schema": TRANSIENT_WEB_PLAN_SCHEMA,
            "adapter_id": TRANSIENT_WEB_ADAPTER_ID,
            "security_id": TRANSIENT_WEB_SECURITY_ID,
            "current_message_sha256": self.current_message_sha256,
            "query_sha256": self.query_sha256,
            "actor_sha256": self.actor_sha256,
            "conversation_scope_sha256": self.conversation_scope_sha256,
            "max_sources": self.max_sources,
        }

    def identity_payload(self) -> dict[str, object]:
        """Return a body-free identity suitable for control-plane pinning."""

        return self._seal_fields()

    def canonical_sha256(self) -> str:
        return _canonical_sha256(self.identity_payload())

    def owned_query(self) -> str:
        """Return the process-owned outbound query. Do not log this string."""

        self.__post_init__()
        return self._query


def seal_explicit_public_web_query(
    *,
    current_user_message: str,
    actor: ActorContext,
    conversation_id: str | None,
) -> SealedPublicWebQuery:
    """Mint outbound authority only from the explicit clause in this message."""

    query, message_sha256 = _extract_explicit_public_query(current_user_message)
    fields: dict[str, object] = {
        "schema": TRANSIENT_WEB_PLAN_SCHEMA,
        "adapter_id": TRANSIENT_WEB_ADAPTER_ID,
        "security_id": TRANSIENT_WEB_SECURITY_ID,
        "current_message_sha256": message_sha256,
        "query_sha256": _sha256_text(query, label="explicit public web query"),
        "actor_sha256": _actor_sha256(actor),
        "conversation_scope_sha256": _scope_sha256(conversation_id),
        "max_sources": _MAX_SOURCES,
    }
    return SealedPublicWebQuery(
        current_message_sha256=message_sha256,
        query_sha256=str(fields["query_sha256"]),
        actor_sha256=str(fields["actor_sha256"]),
        conversation_scope_sha256=str(fields["conversation_scope_sha256"]),
        max_sources=_MAX_SOURCES,
        _query=query,
        _seal=_plan_seal(fields),
        _process_authority=_PROCESS_AUTHORITY,
    )


def seal_compare_current_file_public_web_query(
    *,
    current_user_message: str,
    actor: ActorContext,
    conversation_id: str | None,
) -> SealedPublicWebQuery:
    """Mint outbound authority from the independent public topic of a file compare."""

    query, message_sha256 = _extract_compare_current_file_public_query(current_user_message)
    fields: dict[str, object] = {
        "schema": TRANSIENT_WEB_PLAN_SCHEMA,
        "adapter_id": TRANSIENT_WEB_ADAPTER_ID,
        "security_id": TRANSIENT_WEB_SECURITY_ID,
        "current_message_sha256": message_sha256,
        "query_sha256": _sha256_text(query, label="compare-current-file public web query"),
        "actor_sha256": _actor_sha256(actor),
        "conversation_scope_sha256": _scope_sha256(conversation_id),
        "max_sources": _MAX_SOURCES,
    }
    return SealedPublicWebQuery(
        current_message_sha256=message_sha256,
        query_sha256=str(fields["query_sha256"]),
        actor_sha256=str(fields["actor_sha256"]),
        conversation_scope_sha256=str(fields["conversation_scope_sha256"]),
        max_sources=_MAX_SOURCES,
        _query=query,
        _seal=_plan_seal(fields),
        _process_authority=_PROCESS_AUTHORITY,
    )


def _bounded_utf8(value: object, *, label: str, maximum: int, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TransientWebComparisonError(f"{label} must be text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise TransientWebComparisonError(f"{label} must be valid UTF-8 text") from exc
    if (not allow_empty and not value.strip()) or len(encoded) > maximum:
        raise TransientWebComparisonError(f"{label} is empty or exceeds its closed bound")
    return value


def _truncate_utf8(value: str, maximum: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) <= maximum:
        return value, False
    return encoded[:maximum].decode("utf-8", errors="ignore"), True


def _public_url(value: object) -> str:
    url = _bounded_utf8(value, label="source url", maximum=_MAX_URL_CHARS)
    if _CONTROL_OR_SPACE_RE.search(url) is not None:
        raise TransientWebComparisonError("source url contains forbidden characters")
    try:
        parsed = urllib.parse.urlsplit(url)
        _ = parsed.port
    except ValueError as exc:
        raise TransientWebComparisonError("source url is malformed") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise TransientWebComparisonError("source url is not a credential-free public HTTP(S) URL")
    return urllib.parse.urlunsplit(parsed._replace(fragment=""))


@dataclass(frozen=True, slots=True)
class TransientWebSource:
    label: str
    content_sha256: str
    truncated: bool
    _url: str = field(repr=False)
    _title: str = field(repr=False)
    _text: str = field(repr=False)
    _process_authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            self._process_authority is not _PROCESS_AUTHORITY
            or self.label not in {"W1", "W2", "W3"}
            or type(self.truncated) is not bool
        ):
            raise TransientWebComparisonError("transient web source is not factory-owned")
        _require_digest(self.content_sha256, label="source content_sha256")
        if self.content_sha256 != _canonical_sha256(
            {
                "label": self.label,
                "url": self._url,
                "title": self._title,
                "text": self._text,
                "truncated": self.truncated,
            }
        ):
            raise TransientWebComparisonError("transient web source digest is invalid")

    def synthesis_payload(self) -> dict[str, object]:
        return {
            "label": self.label,
            "url": self._url,
            "title": self._title,
            "text": self._text,
            "truncated": self.truncated,
            "untrusted_source_data": True,
        }


@dataclass(frozen=True, slots=True)
class TransientWebPublicCitation:
    """Non-authorizing URL/title projection derived from process-owned evidence."""

    label: str
    url: str
    title: str
    source_content_sha256: str

    def __post_init__(self) -> None:
        if self.label not in {"W1", "W2", "W3"}:
            raise TransientWebComparisonError("public citation label is invalid")
        if _public_url(self.url) != self.url:
            raise TransientWebComparisonError("public citation url is not canonical")
        _bounded_utf8(
            self.title,
            label="public citation title",
            maximum=_MAX_TITLE_CHARS,
            allow_empty=True,
        )
        _require_digest(self.source_content_sha256, label="source_content_sha256")

    def payload(self) -> dict[str, str]:
        """Return only the fields safe to persist and show beside the answer."""

        return {"label": self.label, "url": self.url, "title": self.title}


@dataclass(frozen=True, slots=True)
class TransientWebComparisonEvidence:
    plan_sha256: str
    query_sha256: str
    status: TransientWebEvidenceStatus
    unavailable_reason: TransientWebUnavailableReason
    outbound_attempted: bool
    research_call_count: int
    requested_sources: int | None
    completed_sources: int | None
    failed_sources: int | None
    timed_out_sources: int | None
    search_timed_out: bool | None
    projection_truncated: bool
    sources: tuple[TransientWebSource, ...] = field(repr=False)
    _query: str = field(repr=False, compare=False)
    _process_authority: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_digest(self.plan_sha256, label="evidence plan_sha256")
        _require_digest(self.query_sha256, label="evidence query_sha256")
        if (
            self._process_authority is not _PROCESS_AUTHORITY
            or not isinstance(self.status, TransientWebEvidenceStatus)
            or not isinstance(self.unavailable_reason, TransientWebUnavailableReason)
            or type(self.outbound_attempted) is not bool
            or self.research_call_count not in {0, 1}
            or type(self.projection_truncated) is not bool
            or type(self.sources) is not tuple
            or len(self.sources) > _MAX_SOURCES
            or _sha256_text(self._query, label="evidence query") != self.query_sha256
        ):
            raise TransientWebComparisonError("transient web evidence is outside its closed contract")
        for label, value in (
            ("requested_sources", self.requested_sources),
            ("completed_sources", self.completed_sources),
            ("failed_sources", self.failed_sources),
            ("timed_out_sources", self.timed_out_sources),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise TransientWebComparisonError(f"{label} is invalid")
        if self.search_timed_out is not None and type(self.search_timed_out) is not bool:
            raise TransientWebComparisonError("search_timed_out is invalid")
        labels = tuple(source.label for source in self.sources)
        if labels != tuple(f"W{index}" for index in range(1, len(labels) + 1)):
            raise TransientWebComparisonError("transient web source labels are not canonical")
        if self.status is TransientWebEvidenceStatus.SOURCED:
            if not self.sources or self.unavailable_reason is not TransientWebUnavailableReason.NONE:
                raise TransientWebComparisonError("sourced evidence needs sources and no failure reason")
        elif self.sources:
            raise TransientWebComparisonError("source-free evidence cannot retain source bodies")
        if (self.status is TransientWebEvidenceStatus.UNAVAILABLE) != (
            self.unavailable_reason is not TransientWebUnavailableReason.NONE
        ):
            raise TransientWebComparisonError("unavailable evidence reason is inconsistent")
        if self.outbound_attempted != (self.research_call_count == 1):
            raise TransientWebComparisonError("outbound attempt and call count are inconsistent")

    def identity_payload(self) -> dict[str, object]:
        """Body-free, restart-storable identity; source bodies remain process-only."""

        return {
            "schema": TRANSIENT_WEB_EVIDENCE_SCHEMA,
            "adapter_id": TRANSIENT_WEB_ADAPTER_ID,
            "security_id": TRANSIENT_WEB_SECURITY_ID,
            "plan_sha256": self.plan_sha256,
            "query_sha256": self.query_sha256,
            "status": self.status.value,
            "unavailable_reason": self.unavailable_reason.value,
            "outbound_attempted": self.outbound_attempted,
            "research_call_count": self.research_call_count,
            "requested_sources": self.requested_sources,
            "completed_sources": self.completed_sources,
            "failed_sources": self.failed_sources,
            "timed_out_sources": self.timed_out_sources,
            "search_timed_out": self.search_timed_out,
            "projection_truncated": self.projection_truncated,
            "source_sha256": [source.content_sha256 for source in self.sources],
            "citation_labels": [source.label for source in self.sources],
        }

    def canonical_sha256(self) -> str:
        return _canonical_sha256(self.identity_payload())

    def to_synthesis_payload(self) -> dict[str, object]:
        """Materialize the bounded, untrusted in-memory evidence for local synthesis."""

        return {
            "schema": TRANSIENT_WEB_EVIDENCE_SCHEMA,
            "query": self._query,
            "status": self.status.value,
            "sources": [source.synthesis_payload() for source in self.sources],
        }

    def public_citations(self) -> tuple[TransientWebPublicCitation, ...]:
        """Project labels and public locator metadata without touching source text."""

        self.__post_init__()
        citations: list[TransientWebPublicCitation] = []
        for source in self.sources:
            source.__post_init__()
            citations.append(
                TransientWebPublicCitation(
                    label=source.label,
                    url=source._url,
                    title=source._title,
                    source_content_sha256=source.content_sha256,
                )
            )
        return tuple(citations)


def _counter(report: Mapping[str, Any], name: str) -> int:
    value = report.get(name)
    if type(value) is not int or value < 0:
        raise TransientWebComparisonError(f"web report {name} is malformed")
    return value


def _evidence(
    plan: SealedPublicWebQuery,
    *,
    status: TransientWebEvidenceStatus,
    reason: TransientWebUnavailableReason,
    sources: tuple[TransientWebSource, ...] = (),
    requested: int | None = None,
    completed: int | None = None,
    failed: int | None = None,
    timed_out: int | None = None,
    search_timed_out: bool | None = None,
    projection_truncated: bool = False,
) -> TransientWebComparisonEvidence:
    return TransientWebComparisonEvidence(
        plan_sha256=plan.canonical_sha256(),
        query_sha256=plan.query_sha256,
        status=status,
        unavailable_reason=reason,
        outbound_attempted=True,
        research_call_count=1,
        requested_sources=requested,
        completed_sources=completed,
        failed_sources=failed,
        timed_out_sources=timed_out,
        search_timed_out=search_timed_out,
        projection_truncated=projection_truncated,
        sources=sources,
        _query=plan._query,
        _process_authority=_PROCESS_AUTHORITY,
    )


def _consumption_turn_id() -> str:
    context = current_primary_authenticated_turn_context()
    token = str(getattr(context, "turn_id", "") or "") if context is not None else ""
    if _CONSUMPTION_IDENTITY_RE.fullmatch(token):
        return token
    return _CONSUMPTION_TURN_ID


def _report_source_urls(raw_sources: object) -> tuple[str, ...]:
    if type(raw_sources) is not list:
        return ()
    urls: list[str] = []
    for item in raw_sources:
        if not isinstance(item, Mapping):
            continue
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            urls.append(url)
    return tuple(urls)


def _report_blocked_private(report: Mapping[str, Any], raw_sources: object) -> bool:
    """Refuse observed private URLs; do not treat them as grounded comparison sources."""

    urls = _report_source_urls(raw_sources)
    if not urls:
        return False
    selected_id = report.get("selected_provider_id")
    try:
        if isinstance(selected_id, str) and selected_id.strip():
            admitted = len(urls)
            search_count = min(admitted, _MAX_SOURCES)
            selected = ProviderObservation(
                provider_id=selected_id,
                status=WebProviderStatus.COMPLETED,
                source_count=search_count,
                direct_source_count=admitted - search_count,
                source_urls=urls,
            )
            primary_id = report.get("provider_primary_id")
            used_fallback = report.get("provider_used_fallback") is True
            if (
                used_fallback
                and isinstance(primary_id, str)
                and primary_id.strip()
                and primary_id.strip().casefold() != selected_id.strip().casefold()
            ):
                primary = ProviderObservation(provider_id=primary_id, status=WebProviderStatus.REFUSED)
                selection = select_web_provider(primary, selected)
            else:
                selection = select_web_provider(selected)
            consumption = build_web_research_consumption(
                _CONSUMPTION_ID,
                _consumption_turn_id(),
                WebCurrentnessDecision.SEARCH_REQUIRED,
                selection,
                source_urls=urls,
                topic="",
            )
            return consumption.usability not in {
                WebResearchConsumptionState.CONSUMABLE,
                WebResearchConsumptionState.CONSUMABLE_DEGRADED,
            }
        consumption = build_web_research_consumption(
            _CONSUMPTION_ID,
            _consumption_turn_id(),
            WebCurrentnessDecision.SEARCH_REQUIRED,
            None,
            source_urls=urls,
            topic="",
        )
    except (TypeError, ValueError, WebProviderPolicyError):
        return isinstance(selected_id, str) and bool(selected_id.strip())
    return consumption.usability is WebResearchConsumptionState.BLOCKED_PRIVATE


def _project_report(
    plan: SealedPublicWebQuery,
    report: object,
) -> TransientWebComparisonEvidence:
    if not isinstance(report, Mapping) or report.get("query") != plan._query:
        raise TransientWebComparisonError("web report is not bound to the sealed query")
    raw_sources = report.get("sources")
    if type(raw_sources) is not list:
        raise TransientWebComparisonError("web report sources must be a list")
    requested = _counter(report, "requested_sources")
    completed = _counter(report, "completed_sources")
    failed = _counter(report, "failed_sources")
    timed_out = _counter(report, "timed_out_sources")
    search_timed_out = report.get("search_timed_out")
    if type(search_timed_out) is not bool:
        raise TransientWebComparisonError("web report search_timed_out is malformed")
    search_failed = report.get("search_failed", False)
    if type(search_failed) is not bool:
        raise TransientWebComparisonError("web report search_failed is malformed")
    report_error = report.get("error", "")
    if type(report_error) is not str:
        raise TransientWebComparisonError("web report error is malformed")
    if (
        completed != len(raw_sources)
        or failed + timed_out > requested
        or requested > completed + failed + timed_out
        or (search_timed_out or search_failed or report_error)
        and raw_sources
    ):
        raise TransientWebComparisonError("web report counters or failure flags are contradictory")
    if _report_blocked_private(report, raw_sources):
        return _evidence(
            plan,
            status=TransientWebEvidenceStatus.UNAVAILABLE,
            reason=TransientWebUnavailableReason.SEARCH_FAILED,
            requested=requested,
            completed=completed,
            failed=failed,
            timed_out=timed_out,
            search_timed_out=False,
        )

    if search_timed_out:
        return _evidence(
            plan,
            status=TransientWebEvidenceStatus.UNAVAILABLE,
            reason=TransientWebUnavailableReason.SEARCH_TIMED_OUT,
            requested=requested,
            completed=completed,
            failed=failed,
            timed_out=timed_out,
            search_timed_out=True,
        )
    if search_failed or report_error:
        return _evidence(
            plan,
            status=TransientWebEvidenceStatus.UNAVAILABLE,
            reason=TransientWebUnavailableReason.SEARCH_FAILED,
            requested=requested,
            completed=completed,
            failed=failed,
            timed_out=timed_out,
            search_timed_out=False,
        )

    candidates: list[tuple[str, str, str, bool]] = []
    projection_truncated = False
    seen_urls: set[str] = set()
    for row in raw_sources:
        if not isinstance(row, Mapping):
            raise TransientWebComparisonError("web report source row is malformed")
        error = row.get("error", "")
        text = row.get("text")
        if type(error) is not str or type(text) is not str:
            raise TransientWebComparisonError("web report source text/error is malformed")
        if error:
            if text.strip():
                raise TransientWebComparisonError("failed web source unexpectedly contains evidence text")
            continue
        text_length = row.get("text_length")
        status_code = row.get("status_code")
        truncated = row.get("truncated")
        title = row.get("title")
        if (
            not text.strip()
            or type(text_length) is not int
            or text_length < len(text)
            or type(status_code) is not int
            or not 200 <= status_code < 300
            or type(truncated) is not bool
            or type(title) is not str
        ):
            raise TransientWebComparisonError("readable web source row is malformed")
        url = _public_url(row.get("url"))
        _bounded_utf8(title, label="source title", maximum=_MAX_TITLE_CHARS, allow_empty=True)
        _bounded_utf8(
            text,
            label="source text",
            maximum=_MAX_UPSTREAM_SOURCE_TEXT_UTF8_BYTES,
        )
        if url in seen_urls:
            projection_truncated = True
            continue
        seen_urls.add(url)
        candidates.append((url, title, text, truncated or text_length > len(text)))

    retained: list[TransientWebSource] = []
    total_budget = _MAX_TOTAL_TEXT_UTF8_BYTES
    for url, title, text, upstream_truncated in candidates:
        if len(retained) >= _MAX_SOURCES or total_budget <= 0:
            projection_truncated = True
            break
        bounded_text, local_truncated = _truncate_utf8(
            text,
            min(_MAX_SOURCE_TEXT_UTF8_BYTES, total_budget),
        )
        if not bounded_text.strip():
            projection_truncated = True
            continue
        total_budget -= len(bounded_text.encode("utf-8"))
        item_truncated = upstream_truncated or local_truncated
        label = f"W{len(retained) + 1}"
        content_sha256 = _canonical_sha256(
            {
                "label": label,
                "url": url,
                "title": title,
                "text": bounded_text,
                "truncated": item_truncated,
            }
        )
        retained.append(
            TransientWebSource(
                label=label,
                content_sha256=content_sha256,
                truncated=item_truncated,
                _url=url,
                _title=title,
                _text=bounded_text,
                _process_authority=_PROCESS_AUTHORITY,
            )
        )
        projection_truncated = projection_truncated or item_truncated
    if len(candidates) > len(retained):
        projection_truncated = True

    if retained:
        return _evidence(
            plan,
            status=TransientWebEvidenceStatus.SOURCED,
            reason=TransientWebUnavailableReason.NONE,
            sources=tuple(retained),
            requested=requested,
            completed=completed,
            failed=failed,
            timed_out=timed_out,
            search_timed_out=False,
            projection_truncated=projection_truncated,
        )
    if raw_sources or requested or failed or timed_out:
        return _evidence(
            plan,
            status=TransientWebEvidenceStatus.UNAVAILABLE,
            reason=TransientWebUnavailableReason.NO_READABLE_SOURCE,
            requested=requested,
            completed=completed,
            failed=failed,
            timed_out=timed_out,
            search_timed_out=False,
        )
    return _evidence(
        plan,
        status=TransientWebEvidenceStatus.EMPTY,
        reason=TransientWebUnavailableReason.NONE,
        requested=0,
        completed=0,
        failed=0,
        timed_out=0,
        search_timed_out=False,
    )


class TransientWebComparisonAdapter:
    """One authorized WebSurfer read with no capture, ingestion or publication."""

    __slots__ = ("_authorization", "_web")

    def __init__(self, authorization: AuthorizationService, web: TransientWebResearch) -> None:
        if not isinstance(authorization, AuthorizationService):
            raise TypeError("authorization must be an AuthorizationService")
        if not callable(getattr(web, "research", None)):
            raise TypeError("web must provide the bounded async research reader")
        self._authorization = authorization
        self._web = web

    async def research(
        self,
        *,
        plan: SealedPublicWebQuery,
        actor: ActorContext,
        conversation_id: str | None,
        current_user_message: str,
        absolute_deadline: float | None = None,
    ) -> TransientWebComparisonEvidence:
        if type(plan) is not SealedPublicWebQuery:
            raise TransientWebComparisonError("plan must be a sealed public-web query")
        # Re-running __post_init__ catches a deliberately corrupted object even
        # if a caller bypassed frozen dataclass assignment with object.__setattr__.
        plan.__post_init__()
        bound = False
        for query, message_sha256 in _bound_query_candidates(current_user_message):
            if (
                message_sha256 == plan.current_message_sha256
                and _sha256_text(query, label="executed public web query") == plan.query_sha256
            ):
                bound = True
                break
        if (
            not bound
            or _actor_sha256(actor) != plan.actor_sha256
            or _scope_sha256(conversation_id) != plan.conversation_scope_sha256
        ):
            raise TransientWebComparisonError("public-web query is not bound to this exact turn")
        deadline: float | None = None
        if absolute_deadline is not None:
            if (
                isinstance(absolute_deadline, bool)
                or not isinstance(absolute_deadline, int | float)
                or not math.isfinite(float(absolute_deadline))
            ):
                raise TransientWebComparisonError("absolute_deadline is invalid")
            deadline = float(absolute_deadline)
            if deadline - asyncio.get_running_loop().time() <= 0:
                raise TimeoutError("transient web comparison deadline expired before authorization")

        # This is intentionally the final gate before the outbound call.  A
        # grant used to mint the plan does not survive a later revoke/deny.
        self._authorization.require(actor, TRANSIENT_WEB_SECURITY_ID)
        timeout_seconds = None if deadline is None else deadline - asyncio.get_running_loop().time()
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise TimeoutError("transient web comparison deadline expired before outbound")
        try:
            if timeout_seconds is None:
                report = await self._web.research(plan._query, max_sources=_MAX_SOURCES)
            else:
                async with asyncio.timeout(timeout_seconds):
                    report = await self._web.research(plan._query, max_sources=_MAX_SOURCES)
        except TimeoutError:
            LOGGER.warning("transient web comparison unavailable: search_timed_out")
            return _evidence(
                plan,
                status=TransientWebEvidenceStatus.UNAVAILABLE,
                reason=TransientWebUnavailableReason.SEARCH_TIMED_OUT,
                search_timed_out=True,
            )
        except Exception:  # noqa: BLE001 -- exception text must not cross this privacy boundary
            LOGGER.warning("transient web comparison unavailable: provider_error")
            return _evidence(
                plan,
                status=TransientWebEvidenceStatus.UNAVAILABLE,
                reason=TransientWebUnavailableReason.PROVIDER_ERROR,
            )
        evidence = _project_report(plan, report)
        if evidence.status is TransientWebEvidenceStatus.UNAVAILABLE:
            LOGGER.warning(
                "transient web comparison unavailable: %s",
                evidence.unavailable_reason.value,
            )
        return evidence
