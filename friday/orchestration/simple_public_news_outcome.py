"""Typed publication contract for the existing isolated legacy news lane.

This module does not route or execute web work.  It binds the already existing
``AgentRuntime`` contour to a privacy-safe ``CapabilityOutcome`` and to the
accepted-outcome receipt used by the V12 file routes.  Raw requests, outbound
queries, source titles/URLs and evidence bodies are transient inputs only; the
closed objects retain their SHA-256 identities.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from friday.orchestration.capability_outcome import (
    CapabilityOutcome,
    CapabilityOutcomeError,
    CapabilityOutcomeStatus,
    CompletionGateDecision,
)
from friday.orchestration.contracts import RouteClass
from friday.public_web_url import canonical_public_web_url_key, sanitize_public_web_url
from friday.web_research_contract import (
    MAX_RESEARCH_ATTEMPTS,
    MAX_RESEARCH_SOURCE_ROWS,
    target_research_report_is_valid,
)
from friday.web_surfer import web_source_matches_class

SIMPLE_PUBLIC_NEWS_PLAN_SCHEMA = "friday.legacy-simple-public-news-plan.v1"
SIMPLE_PUBLIC_NEWS_EVIDENCE_SCHEMA = "friday.simple-public-news-evidence.v2"
SIMPLE_PUBLIC_NEWS_EVIDENCE_MARKER = "simple_public_news_full"
SIMPLE_PUBLIC_NEWS_EVIDENCE_MAX_CHARS = 12_100

SIMPLE_PUBLIC_NEWS_UNVERIFIED_FALLBACK = (
    "Поиск завершён и проверяемые источники получены, но сводку не удалось "
    "подтвердить по полученной выдаче. Ниже оставляю только найденные источники; "
    "неподтверждённые факты не публикую."
)
SIMPLE_PUBLIC_NEWS_SYNTHESIS_FALLBACK = (
    "Поиск завершён и проверяемые источники получены, но модель не успела "
    "подготовить сводку за ограниченное время. Ниже оставляю найденные ссылки; "
    "факты без завершённой обработки не пересказываю."
)
SIMPLE_PUBLIC_NEWS_ENVELOPE_FALLBACK = (
    "Поиск завершён и проверяемые источники получены, но полная ограниченная "
    "выдача недоступна для проверки. Ниже оставляю только найденные источники; "
    "непроверенные факты не публикую."
)
SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK = (
    "В этом ходе я не получила проверяемую интернет-выдачу и не буду выдавать ответ "
    "из памяти за свежую интернет-сводку. Повтори запрос позже; если поиск отключён "
    "из-за приватных вложений, открой новый диалог без файлов."
)

_DIGEST = re.compile(r"[0-9a-f]{64}")
_SAFE_TOPIC = re.compile(r"[a-z][a-z0-9_]{0,63}")
_FRESHNESS = frozenset({"day", "week", "month", "year"})
_SOURCE_CLASSES = frozenset({"", "foreign"})
_WEB_STATUSES = frozenset({"none", "failed", "empty", "sourced", "partial"})
_VERIFIER_STATUSES = frozenset({"passed", "failed", "unknown", "skipped"})
_MAX_SOURCES = 5


class SimplePublicNewsOutcomeError(CapabilityOutcomeError):
    """The isolated news result is outside its closed publication contract."""


class SimplePublicNewsEvidenceStatus(StrEnum):
    SOURCED = "sourced"
    PARTIAL = "partial"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"


class SimplePublicNewsEmptyKind(StrEnum):
    NONE = "none"
    VALIDATED_ZERO = "validated_zero"
    TOPIC_MISMATCH = "topic_mismatch"


class SimplePublicNewsResultKind(StrEnum):
    VERIFIED_FACTUAL = "verified_factual"
    SYNTHESIS_SOURCES_ONLY = "synthesis_sources_only"
    VERIFIER_REJECTED_SOURCES_ONLY = "verifier_rejected_sources_only"
    EMPTY_FALLBACK = "empty_fallback"
    UNAVAILABLE_FALLBACK = "unavailable_fallback"
    DENIED_FALLBACK = "denied_fallback"


_EVIDENCE_FACTORY_TOKEN = object()


class _SimplePublicNewsEvidenceSeal:
    __slots__ = ("digest",)

    def __init__(self, token: object, digest: str) -> None:
        if token is not _EVIDENCE_FACTORY_TOKEN:
            raise SimplePublicNewsOutcomeError("news evidence seal is factory-only")
        self.digest = str(_digest(digest, label="news evidence seal"))


def _sha256_text(value: object, *, label: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise SimplePublicNewsOutcomeError(f"{label} must be text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SimplePublicNewsOutcomeError(f"{label} must be valid UTF-8") from exc
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: object, *, label: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise SimplePublicNewsOutcomeError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


_EVIDENCE_SEAL_FIELDS = frozenset(
    {
        "plan_sha256",
        "executed_query_sha256",
        "status",
        "outbound_attempted",
        "research_call_count",
        "target_sources",
        "requested_sources",
        "completed_sources",
        "failed_sources",
        "timed_out_sources",
        "search_timed_out",
        "topic_filtered_sources",
        "projection_truncated",
        "report_incomplete",
        "empty_kind",
        "empty_proof_sha256",
        "model_envelope_sha256",
        "source_ledger_sha256",
        "citation_labels",
    }
)


def _evidence_seal_sha256(fields: Mapping[str, object]) -> str:
    if set(fields) != _EVIDENCE_SEAL_FIELDS:
        raise SimplePublicNewsOutcomeError("news evidence seal fields are not closed")
    return _canonical_sha256(
        {
            "schema": "friday.simple-public-news-evidence-seal.v2",
            **dict(fields),
        }
    )


def simple_public_news_content_identity(content: str) -> str:
    """Hash one transient publication body without retaining its prose."""

    return _sha256_text(content, label="news publication content")


def _optional_count(report: Mapping[str, Any] | None, key: str) -> int | None:
    if report is None or key not in report:
        return None
    value = report.get(key)
    limit = MAX_RESEARCH_SOURCE_ROWS if key == "completed_sources" else MAX_RESEARCH_ATTEMPTS
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= limit:
        return None
    return value


def _optional_boolean(report: Mapping[str, Any] | None, key: str) -> bool | None:
    if report is None or key not in report:
        return None
    value = report.get(key)
    return value if isinstance(value, bool) else None


def _validated_empty_proof_sha256(
    plan: LegacySimplePublicNewsPlan,
    report: Mapping[str, Any] | None,
    *,
    executed_query_sha256: str,
) -> tuple[SimplePublicNewsEmptyKind, str]:
    """Validate a kernel-shaped zero result; malformed/failure is unavailable."""

    if report is None or report.get("outbound_attempted") is not True:
        raise SimplePublicNewsOutcomeError("empty news evidence has no outbound report")
    if report.get("freshness") != plan.freshness:
        raise SimplePublicNewsOutcomeError("empty news evidence has no freshness proof")
    filters = report.get("applied_search_filters")
    if not isinstance(filters, Mapping) or filters.get("freshness") != plan.freshness:
        raise SimplePublicNewsOutcomeError("empty news evidence has no filter attestation")
    if str(report.get("source_class") or "") != plan.source_class:
        raise SimplePublicNewsOutcomeError("empty news evidence changed source class")
    if simple_public_news_topic_mismatch_is_empty(
        report,
        expected_topic_class=plan.topic_class,
        expected_max_sources=plan.max_sources,
    ):
        if plan.source_class and report.get("source_class_satisfied") is not True:
            raise SimplePublicNewsOutcomeError("empty news evidence has no source-class proof")
        return (
            SimplePublicNewsEmptyKind.TOPIC_MISMATCH,
            _canonical_sha256(
                {
                    "schema": "friday.simple-public-news-empty-proof.v1",
                    "kind": "topic_mismatch",
                    "plan_sha256": plan.canonical_sha256(),
                    "executed_query_sha256": executed_query_sha256,
                    "requested_sources": report.get("requested_sources"),
                    "failed_sources": report.get("failed_sources"),
                    "timed_out_sources": report.get("timed_out_sources"),
                    "topic_filtered_sources": report.get("topic_filtered_sources"),
                }
            ),
        )
    counters = tuple(
        report.get(key)
        for key in (
            "requested_sources",
            "completed_sources",
            "failed_sources",
            "timed_out_sources",
        )
    )
    failure_flags = tuple(
        report.get(key, False) for key in ("search_failed", "search_timed_out", "refused", "quota_exhausted")
    )
    if (
        report.get("sources") != []
        or counters != (0, 0, 0, 0)
        or any(not isinstance(value, bool) for value in failure_flags)
        or any(failure_flags)
        or str(report.get("error") or "").strip()
        or report.get("topic_filtered_sources", 0) != 0
    ):
        raise SimplePublicNewsOutcomeError("empty news evidence is not a validated complete zero result")
    return (
        SimplePublicNewsEmptyKind.VALIDATED_ZERO,
        _canonical_sha256(
            {
                "schema": "friday.simple-public-news-empty-proof.v1",
                "kind": "validated_zero",
                "plan_sha256": plan.canonical_sha256(),
                "executed_query_sha256": executed_query_sha256,
                "requested_sources": 0,
                "completed_sources": 0,
                "failed_sources": 0,
                "timed_out_sources": 0,
            }
        ),
    )


def _canonical_news_source_url(url: str) -> str:
    """Match the runtime's source identity before minting typed evidence."""

    return canonical_public_web_url_key(url)


def simple_public_news_source_ledger_identity(
    sources: object,
) -> tuple[str | None, tuple[str, ...]]:
    """Hash the exact bounded public source order without retaining its values."""

    if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes, bytearray)):
        raise SimplePublicNewsOutcomeError("news source ledger must be a sequence")
    if len(sources) > _MAX_SOURCES:
        raise SimplePublicNewsOutcomeError("news source ledger exceeds its closed limit")
    projected: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in sources:
        if not isinstance(item, Mapping) or set(item) != {"url", "title"}:
            raise SimplePublicNewsOutcomeError("news source ledger has an open row shape")
        url = item.get("url")
        title = item.get("title")
        if not isinstance(url, str) or not url or len(url) > 2_048:
            raise SimplePublicNewsOutcomeError("news source ledger has an invalid URL")
        if not isinstance(title, str) or len(title) > 500:
            raise SimplePublicNewsOutcomeError("news source ledger has an invalid title")
        if not sanitize_public_web_url(url):
            raise SimplePublicNewsOutcomeError("news source ledger has an unsafe URL")
        identity = _canonical_news_source_url(url)
        if not identity or identity in seen_urls:
            raise SimplePublicNewsOutcomeError("news source ledger has a duplicate or invalid URL")
        seen_urls.add(identity)
        projected.append({"label": f"A{len(projected) + 1}", "title": title, "url": url})
    if not projected:
        return None, ()
    labels = tuple(item["label"] for item in projected)
    return _canonical_sha256(
        {"schema": "friday.simple-public-news-source-ledger.v1", "sources": projected}
    ), labels


def simple_public_news_model_envelope_identity(entries: object) -> str | None:
    """Return the digest of the sole full evidence envelope, or no identity."""

    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes, bytearray)):
        return None
    marked: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if (
            str(entry.get("tool") or "") == "web_research"
            and entry.get("evidence_scope") == SIMPLE_PUBLIC_NEWS_EVIDENCE_MARKER
        ):
            output = entry.get("output")
            if (
                not isinstance(output, str)
                or not output
                or len(output) > SIMPLE_PUBLIC_NEWS_EVIDENCE_MAX_CHARS
            ):
                return None
            marked.append(output)
    return _sha256_text(marked[0], label="news evidence envelope") if len(marked) == 1 else None


@dataclass(frozen=True, slots=True)
class LegacySimplePublicNewsPlan:
    """Code-owned plan identity for the legacy contour, never a V12 TurnPlan."""

    request_sha256: str
    outbound_query_sha256: str
    freshness: str
    source_class: str
    topic_class: str
    max_sources: int = 3

    def __post_init__(self) -> None:
        _digest(self.request_sha256, label="request_sha256")
        _digest(self.outbound_query_sha256, label="outbound_query_sha256")
        if self.freshness not in _FRESHNESS:
            raise SimplePublicNewsOutcomeError("news plan freshness is outside the closed lane")
        if self.source_class not in _SOURCE_CLASSES:
            raise SimplePublicNewsOutcomeError("news plan source class is outside the closed lane")
        if self.topic_class and _SAFE_TOPIC.fullmatch(self.topic_class) is None:
            raise SimplePublicNewsOutcomeError("news plan topic class is outside the closed lane")
        if self.max_sources != 3:
            raise SimplePublicNewsOutcomeError("news plan source count is outside the closed lane")

    @classmethod
    def from_request(
        cls,
        request: str,
        outbound_query: str,
        *,
        freshness: str,
        source_class: str,
        topic_class: str,
        max_sources: int = 3,
    ) -> LegacySimplePublicNewsPlan:
        return cls(
            request_sha256=_sha256_text(request, label="news request"),
            outbound_query_sha256=_sha256_text(outbound_query, label="outbound news query"),
            freshness=freshness,
            source_class=source_class,
            topic_class=topic_class,
            max_sources=max_sources,
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema": SIMPLE_PUBLIC_NEWS_PLAN_SCHEMA,
            "route": RouteClass.WEB_READ.value,
            "lane": "legacy_simple_public_news",
            "request_sha256": self.request_sha256,
            "outbound_query_sha256": self.outbound_query_sha256,
            "tool": "web_research",
            "security_id": "web.research",
            "risk": "mutate",
            "max_sources": self.max_sources,
            "freshness": self.freshness,
            "source_class": self.source_class,
            "topic_class": self.topic_class,
        }

    def canonical_sha256(self) -> str:
        return _canonical_sha256(self.payload())


@dataclass(frozen=True, slots=True)
class SimplePublicNewsEvidence:
    """Digest-only identity of the exact accepted public-news projection."""

    plan_sha256: str
    executed_query_sha256: str
    status: SimplePublicNewsEvidenceStatus
    outbound_attempted: bool
    research_call_count: int
    target_sources: int | None
    requested_sources: int | None
    completed_sources: int | None
    failed_sources: int | None
    timed_out_sources: int | None
    search_timed_out: bool | None
    topic_filtered_sources: int
    projection_truncated: bool
    report_incomplete: bool
    empty_kind: SimplePublicNewsEmptyKind
    empty_proof_sha256: str | None
    model_envelope_sha256: str | None
    source_ledger_sha256: str | None
    citation_labels: tuple[str, ...]
    _factory_seal: _SimplePublicNewsEvidenceSeal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _digest(self.plan_sha256, label="news evidence plan_sha256")
        _digest(self.executed_query_sha256, label="news evidence executed query sha256")
        if not isinstance(self.status, SimplePublicNewsEvidenceStatus):
            raise SimplePublicNewsOutcomeError("news evidence status is not closed")
        if not isinstance(self.outbound_attempted, bool):
            raise SimplePublicNewsOutcomeError("news evidence outbound flag must be boolean")
        if self.research_call_count not in {0, 1}:
            raise SimplePublicNewsOutcomeError("news evidence call count must be zero or one")
        for label, value in (
            ("target_sources", self.target_sources),
            ("requested_sources", self.requested_sources),
            ("completed_sources", self.completed_sources),
            ("failed_sources", self.failed_sources),
            ("timed_out_sources", self.timed_out_sources),
        ):
            limit = (
                _MAX_SOURCES
                if label == "target_sources"
                else MAX_RESEARCH_SOURCE_ROWS
                if label == "completed_sources"
                else MAX_RESEARCH_ATTEMPTS
            )
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= limit
            ):
                raise SimplePublicNewsOutcomeError(f"news evidence {label} is invalid")
        if self.search_timed_out is not None and not isinstance(self.search_timed_out, bool):
            raise SimplePublicNewsOutcomeError("news evidence timeout flag is invalid")
        if (
            not isinstance(self.topic_filtered_sources, int)
            or isinstance(self.topic_filtered_sources, bool)
            or self.topic_filtered_sources < 0
            or self.topic_filtered_sources > MAX_RESEARCH_ATTEMPTS
            or self.failed_sources is not None
            and self.topic_filtered_sources > self.failed_sources
        ):
            raise SimplePublicNewsOutcomeError("news evidence topic-filter count is invalid")
        if not isinstance(self.projection_truncated, bool):
            raise SimplePublicNewsOutcomeError("news evidence truncation flag is invalid")
        if not isinstance(self.report_incomplete, bool):
            raise SimplePublicNewsOutcomeError("news evidence report flag is invalid")
        if not isinstance(self.empty_kind, SimplePublicNewsEmptyKind):
            raise SimplePublicNewsOutcomeError("news empty kind is not closed")
        if type(self._factory_seal) is not _SimplePublicNewsEvidenceSeal:
            raise SimplePublicNewsOutcomeError("news evidence was not minted by its factory")
        _digest(
            self.empty_proof_sha256,
            label="news empty proof sha256",
            optional=True,
        )
        _digest(
            self.model_envelope_sha256,
            label="news model envelope sha256",
            optional=True,
        )
        _digest(
            self.source_ledger_sha256,
            label="news source ledger sha256",
            optional=True,
        )
        if not isinstance(self.citation_labels, tuple):
            raise SimplePublicNewsOutcomeError("news evidence labels must be immutable")
        if self.citation_labels != tuple(f"A{index}" for index in range(1, len(self.citation_labels) + 1)):
            raise SimplePublicNewsOutcomeError("news evidence labels must be sequential")
        source_bearing = self.status in {
            SimplePublicNewsEvidenceStatus.SOURCED,
            SimplePublicNewsEvidenceStatus.PARTIAL,
        }
        if source_bearing:
            if (
                not self.outbound_attempted
                or self.research_call_count != 1
                or self.model_envelope_sha256 is None
                or self.source_ledger_sha256 is None
                or not self.citation_labels
            ):
                raise SimplePublicNewsOutcomeError(
                    "source-bearing news evidence requires one bounded envelope and source ledger"
                )
        elif (
            self.model_envelope_sha256 is not None
            or self.source_ledger_sha256 is not None
            or self.citation_labels
        ):
            raise SimplePublicNewsOutcomeError(
                "source-free news evidence cannot retain an envelope or ledger"
            )
        if self.status is SimplePublicNewsEvidenceStatus.EMPTY and (
            not self.outbound_attempted or self.research_call_count != 1
        ):
            raise SimplePublicNewsOutcomeError("empty news evidence requires one outbound attempt")
        if (self.status is SimplePublicNewsEvidenceStatus.EMPTY) != (self.empty_proof_sha256 is not None):
            raise SimplePublicNewsOutcomeError("empty news evidence requires its closed proof")
        if (self.status is SimplePublicNewsEvidenceStatus.EMPTY) != (
            self.empty_kind is not SimplePublicNewsEmptyKind.NONE
        ):
            raise SimplePublicNewsOutcomeError("news empty evidence kind is inconsistent")

    def _seal_fields(self) -> dict[str, object]:
        return {
            "plan_sha256": self.plan_sha256,
            "executed_query_sha256": self.executed_query_sha256,
            "status": self.status.value,
            "outbound_attempted": self.outbound_attempted,
            "research_call_count": self.research_call_count,
            "target_sources": self.target_sources,
            "requested_sources": self.requested_sources,
            "completed_sources": self.completed_sources,
            "failed_sources": self.failed_sources,
            "timed_out_sources": self.timed_out_sources,
            "search_timed_out": self.search_timed_out,
            "topic_filtered_sources": self.topic_filtered_sources,
            "projection_truncated": self.projection_truncated,
            "report_incomplete": self.report_incomplete,
            "empty_kind": self.empty_kind.value,
            "empty_proof_sha256": self.empty_proof_sha256,
            "model_envelope_sha256": self.model_envelope_sha256,
            "source_ledger_sha256": self.source_ledger_sha256,
            "citation_labels": list(self.citation_labels),
        }

    def _factory_seal_is_valid(self) -> bool:
        return hmac.compare_digest(
            self._factory_seal.digest,
            _evidence_seal_sha256(self._seal_fields()),
        )

    @classmethod
    def from_projection(
        cls,
        plan: LegacySimplePublicNewsPlan,
        *,
        status: SimplePublicNewsEvidenceStatus,
        executed_query: str,
        outbound_attempted: bool,
        research_call_count: int,
        report: Mapping[str, Any] | None,
        model_envelope: str,
        sources: object,
        topic_filtered_sources: int = 0,
        projection_truncated: bool = False,
    ) -> SimplePublicNewsEvidence:
        if (
            not isinstance(topic_filtered_sources, int)
            or isinstance(topic_filtered_sources, bool)
            or not 0 <= topic_filtered_sources <= MAX_RESEARCH_ATTEMPTS
        ):
            raise SimplePublicNewsOutcomeError("news topic-filter count is invalid")
        executed_query_sha256 = _sha256_text(executed_query, label="executed news query")
        if executed_query_sha256 != plan.outbound_query_sha256:
            raise SimplePublicNewsOutcomeError("executed news query is not bound to the plan")
        if report is not None and report.get("query") != executed_query:
            raise SimplePublicNewsOutcomeError("news report query is not the executed query")
        empty_kind, empty_proof_sha256 = (
            _validated_empty_proof_sha256(
                plan,
                report,
                executed_query_sha256=executed_query_sha256,
            )
            if status is SimplePublicNewsEvidenceStatus.EMPTY
            else (SimplePublicNewsEmptyKind.NONE, None)
        )
        retained_topic_filtered_sources = topic_filtered_sources
        if empty_kind is SimplePublicNewsEmptyKind.TOPIC_MISMATCH:
            report_topic_filtered_sources = _optional_count(report, "topic_filtered_sources")
            if report_topic_filtered_sources is None or topic_filtered_sources not in {
                0,
                report_topic_filtered_sources,
            }:
                raise SimplePublicNewsOutcomeError("news topic-filter proof changed during projection")
            retained_topic_filtered_sources = report_topic_filtered_sources
        source_digest, labels = simple_public_news_source_ledger_identity(sources)
        envelope_digest = (
            _sha256_text(model_envelope, label="news evidence envelope") if model_envelope else None
        )
        target_sources = _optional_count(report, "target_sources")
        requested_sources = _optional_count(report, "requested_sources")
        completed_sources = _optional_count(report, "completed_sources")
        failed_sources = _optional_count(report, "failed_sources")
        timed_out_sources = _optional_count(report, "timed_out_sources")
        search_timed_out = _optional_boolean(report, "search_timed_out")
        if failed_sources is not None and topic_filtered_sources > failed_sources:
            raise SimplePublicNewsOutcomeError("news topic-filter count exceeds failed sources")
        if (
            report is not None
            and "target_sources" in report
            and not target_research_report_is_valid(
                report,
                configured_max_sources=plan.max_sources,
                allow_source_subset=True,
            )
        ):
            raise SimplePublicNewsOutcomeError("news report target contract is malformed")
        if "target_sources" in (report or {}) and (
            target_sources is None
            or target_sources > plan.max_sources
            or requested_sources is None
            or target_sources > requested_sources
        ):
            raise SimplePublicNewsOutcomeError("news report target_sources is malformed")
        report_incomplete = report is None
        source_bearing = status in {
            SimplePublicNewsEvidenceStatus.SOURCED,
            SimplePublicNewsEvidenceStatus.PARTIAL,
        }
        if source_bearing:
            if (
                report is None
                or not outbound_attempted
                or report.get("outbound_attempted") is not True
                or report.get("freshness") != plan.freshness
                or not isinstance(report.get("applied_search_filters"), Mapping)
                or report["applied_search_filters"].get("freshness") != plan.freshness
                or str(report.get("source_class") or "") != plan.source_class
                or plan.topic_class
                and (
                    report.get("topic_class") != plan.topic_class
                    or report.get("topic_class_satisfied") is not True
                )
                or envelope_digest is None
                or len(model_envelope) > SIMPLE_PUBLIC_NEWS_EVIDENCE_MAX_CHARS
                or not labels
                or len(labels) > plan.max_sources
            ):
                raise SimplePublicNewsOutcomeError(
                    "source-bearing news evidence lacks its attested bounded report"
                )
            for failure_flag in (
                "search_failed",
                "search_timed_out",
                "refused",
                "quota_exhausted",
            ):
                if report.get(failure_flag) is not False:
                    raise SimplePublicNewsOutcomeError("source-bearing news report claims failure")
            if report.get("error") != "":
                raise SimplePublicNewsOutcomeError("source-bearing news report has an error")
            raw_report_sources = report.get("sources")
            if not isinstance(raw_report_sources, list):
                raise SimplePublicNewsOutcomeError("source-bearing news report has no source rows")
            report_sources: list[dict[str, str]] = []
            report_incomplete = any(
                key not in report
                for key in (
                    "requested_sources",
                    "completed_sources",
                    "failed_sources",
                    "timed_out_sources",
                    "search_timed_out",
                )
            )
            for item in raw_report_sources:
                if not isinstance(item, Mapping):
                    raise SimplePublicNewsOutcomeError("source-bearing news report row is malformed")
                url = item.get("url")
                title = item.get("title")
                text = item.get("text")
                text_length = item.get("text_length")
                status_code = item.get("status_code")
                item_shape_incomplete = not {
                    "text_length",
                    "status_code",
                    "error",
                    "truncated",
                }.issubset(item)
                item_error = item.get("error", "")
                truncated = item.get("truncated", False)
                if (
                    not isinstance(url, str)
                    or not isinstance(title, str)
                    or not isinstance(text, str)
                    or not text.strip()
                    or not isinstance(text_length, int)
                    or isinstance(text_length, bool)
                    or text_length < len(text)
                    or not isinstance(status_code, int)
                    or isinstance(status_code, bool)
                    or not 200 <= status_code < 300
                    or not isinstance(item_error, str)
                    or bool(item_error.strip())
                    or not isinstance(truncated, bool)
                    or plan.source_class
                    and not web_source_matches_class(url, plan.source_class)
                ):
                    raise SimplePublicNewsOutcomeError("source-bearing news report row is malformed")
                report_sources.append({"url": url, "title": title})
                if item_shape_incomplete or truncated or text_length > len(text):
                    report_incomplete = True
            report_source_digest, report_labels = simple_public_news_source_ledger_identity(report_sources)
            if report_source_digest != source_digest or report_labels != labels:
                raise SimplePublicNewsOutcomeError("news report rows changed before evidence projection")
            for key, value in (
                ("target_sources", target_sources),
                ("requested_sources", requested_sources),
                ("completed_sources", completed_sources),
                ("failed_sources", failed_sources),
                ("timed_out_sources", timed_out_sources),
            ):
                if key in report and value is None:
                    raise SimplePublicNewsOutcomeError("news report counter is malformed")
            if "search_timed_out" in report and search_timed_out is None:
                raise SimplePublicNewsOutcomeError("news report timeout flag is malformed")
            if (
                requested_sources is not None
                and completed_sources is not None
                and failed_sources is not None
                and timed_out_sources is not None
                and (
                    failed_sources + timed_out_sources > requested_sources
                    or requested_sources > completed_sources + failed_sources + timed_out_sources
                    or completed_sources < len(labels)
                    or target_sources is not None
                    and (
                        target_sources == 0
                        or target_sources > plan.max_sources
                        or target_sources > requested_sources
                    )
                )
            ):
                raise SimplePublicNewsOutcomeError("news report counters are contradictory")
            complete_shape = bool(
                requested_sources is not None
                and completed_sources is not None
                and failed_sources is not None
                and timed_out_sources is not None
                and search_timed_out is not None
            )
            legacy_complete_projection = bool(
                complete_shape
                and completed_sources == len(labels)
                and requested_sources is not None
                and completed_sources is not None
                and 0 < requested_sources <= completed_sources
                and failed_sources == 0
                and timed_out_sources == 0
                and retained_topic_filtered_sources == 0
                and search_timed_out is False
                and not projection_truncated
                and not report_incomplete
            )
            target_complete_projection = bool(
                complete_shape
                and target_sources is not None
                and target_sources > 0
                and len(labels) >= target_sources
                and retained_topic_filtered_sources == 0
                and search_timed_out is False
                and not projection_truncated
                and not report_incomplete
            )
            complete_projection = (
                target_complete_projection if target_sources is not None else legacy_complete_projection
            )
            if status is SimplePublicNewsEvidenceStatus.SOURCED and not complete_projection:
                raise SimplePublicNewsOutcomeError("sourced news evidence is not complete")
            if status is SimplePublicNewsEvidenceStatus.PARTIAL and complete_projection:
                raise SimplePublicNewsOutcomeError("partial news evidence has no degradation reason")
        elif status is SimplePublicNewsEvidenceStatus.EMPTY:
            if empty_kind is SimplePublicNewsEmptyKind.VALIDATED_ZERO and target_sources not in {None, 0}:
                raise SimplePublicNewsOutcomeError("zero-result news evidence has a nonzero target")
            if empty_kind is SimplePublicNewsEmptyKind.TOPIC_MISMATCH and not (
                target_sources is None or 0 < target_sources <= retained_topic_filtered_sources
            ):
                raise SimplePublicNewsOutcomeError("topic-mismatch news evidence changed its target")
            report_incomplete = False

        evidence_fields: dict[str, object] = {
            "plan_sha256": plan.canonical_sha256(),
            "executed_query_sha256": executed_query_sha256,
            "status": status.value,
            "outbound_attempted": outbound_attempted,
            "research_call_count": research_call_count,
            "target_sources": target_sources,
            "requested_sources": requested_sources,
            "completed_sources": completed_sources,
            "failed_sources": failed_sources,
            "timed_out_sources": timed_out_sources,
            "search_timed_out": search_timed_out,
            "topic_filtered_sources": retained_topic_filtered_sources,
            "projection_truncated": projection_truncated,
            "report_incomplete": report_incomplete,
            "empty_kind": empty_kind.value,
            "empty_proof_sha256": empty_proof_sha256,
            "model_envelope_sha256": envelope_digest,
            "source_ledger_sha256": source_digest,
            "citation_labels": list(labels),
        }
        seal = _SimplePublicNewsEvidenceSeal(
            _EVIDENCE_FACTORY_TOKEN,
            _evidence_seal_sha256(evidence_fields),
        )
        return cls(
            plan_sha256=str(evidence_fields["plan_sha256"]),
            executed_query_sha256=executed_query_sha256,
            status=status,
            outbound_attempted=outbound_attempted,
            research_call_count=research_call_count,
            target_sources=target_sources,
            requested_sources=requested_sources,
            completed_sources=completed_sources,
            failed_sources=failed_sources,
            timed_out_sources=timed_out_sources,
            search_timed_out=search_timed_out,
            topic_filtered_sources=retained_topic_filtered_sources,
            projection_truncated=projection_truncated,
            report_incomplete=report_incomplete,
            empty_kind=empty_kind,
            empty_proof_sha256=empty_proof_sha256,
            model_envelope_sha256=envelope_digest,
            source_ledger_sha256=source_digest,
            citation_labels=labels,
            _factory_seal=seal,
        )

    @property
    def identity_sha256(self) -> str | None:
        if self.status is SimplePublicNewsEvidenceStatus.UNAVAILABLE:
            return None
        return _canonical_sha256(
            {
                "schema": SIMPLE_PUBLIC_NEWS_EVIDENCE_SCHEMA,
                "plan_sha256": self.plan_sha256,
                "executed_query_sha256": self.executed_query_sha256,
                "status": self.status.value,
                "outbound_attempted": self.outbound_attempted,
                "research_call_count": self.research_call_count,
                "target_sources": self.target_sources,
                "requested_sources": self.requested_sources,
                "completed_sources": self.completed_sources,
                "failed_sources": self.failed_sources,
                "timed_out_sources": self.timed_out_sources,
                "search_timed_out": self.search_timed_out,
                "topic_filtered_sources": self.topic_filtered_sources,
                "projection_truncated": self.projection_truncated,
                "report_incomplete": self.report_incomplete,
                "empty_kind": self.empty_kind.value,
                "empty_proof_sha256": self.empty_proof_sha256,
                "model_envelope_sha256": self.model_envelope_sha256,
                "source_ledger_sha256": self.source_ledger_sha256,
                "citation_labels": list(self.citation_labels),
            }
        )


@dataclass(frozen=True, slots=True)
class SimplePublicNewsResult:
    """Digest-only description of the exact final legacy publication."""

    kind: SimplePublicNewsResultKind
    content_sha256: str
    source_ledger_sha256: str | None
    model_generated: bool
    verifier_status: str
    legacy_web_status: str
    research_call_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SimplePublicNewsResultKind):
            raise SimplePublicNewsOutcomeError("news result kind is not closed")
        _digest(self.content_sha256, label="news result content_sha256")
        _digest(
            self.source_ledger_sha256,
            label="news result source ledger sha256",
            optional=True,
        )
        if not isinstance(self.model_generated, bool):
            raise SimplePublicNewsOutcomeError("news result model flag must be boolean")
        if self.verifier_status not in _VERIFIER_STATUSES:
            raise SimplePublicNewsOutcomeError("news result verifier status is not closed")
        if self.legacy_web_status not in _WEB_STATUSES:
            raise SimplePublicNewsOutcomeError("news result web status is not closed")
        if self.research_call_count not in {0, 1}:
            raise SimplePublicNewsOutcomeError("news result call count must be zero or one")
        source_kind = self.kind in {
            SimplePublicNewsResultKind.VERIFIED_FACTUAL,
            SimplePublicNewsResultKind.SYNTHESIS_SOURCES_ONLY,
            SimplePublicNewsResultKind.VERIFIER_REJECTED_SOURCES_ONLY,
        }
        if source_kind != (self.source_ledger_sha256 is not None):
            raise SimplePublicNewsOutcomeError("news result kind and source ledger disagree")
        if self.kind is SimplePublicNewsResultKind.VERIFIED_FACTUAL:
            if not self.model_generated or self.verifier_status != "passed":
                raise SimplePublicNewsOutcomeError("verified news result requires a passed model body")
        elif self.model_generated:
            raise SimplePublicNewsOutcomeError("news fallback cannot claim model publication")
        expected_fallback = {
            SimplePublicNewsResultKind.SYNTHESIS_SOURCES_ONLY: (SIMPLE_PUBLIC_NEWS_SYNTHESIS_FALLBACK),
            SimplePublicNewsResultKind.VERIFIER_REJECTED_SOURCES_ONLY: (
                SIMPLE_PUBLIC_NEWS_UNVERIFIED_FALLBACK
            ),
            SimplePublicNewsResultKind.EMPTY_FALLBACK: SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK,
            SimplePublicNewsResultKind.UNAVAILABLE_FALLBACK: SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK,
            SimplePublicNewsResultKind.DENIED_FALLBACK: SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK,
        }.get(self.kind)
        if expected_fallback is not None and self.content_sha256 != _sha256_text(
            expected_fallback,
            label="news fallback",
        ):
            raise SimplePublicNewsOutcomeError("news fallback content does not match its result kind")

    @property
    def identity_sha256(self) -> str:
        return _canonical_sha256(
            {
                "schema": "friday.simple-public-news-result.v1",
                "kind": self.kind.value,
                "content_sha256": self.content_sha256,
                "source_ledger_sha256": self.source_ledger_sha256,
                "model_generated": self.model_generated,
                "verifier_status": self.verifier_status,
                "legacy_web_status": self.legacy_web_status,
                "research_call_count": self.research_call_count,
            }
        )


def build_simple_public_news_result(
    evidence: SimplePublicNewsEvidence,
    *,
    content: str,
    source_ledger_sha256: str | None,
    model_generated: bool,
    verifier_status: str,
    legacy_web_status: str,
    authority_allowed: bool,
) -> SimplePublicNewsResult:
    """Classify only the existing visible answer/fallback forms."""

    content_sha256 = _sha256_text(content, label="news publication content")
    if not authority_allowed:
        kind = SimplePublicNewsResultKind.DENIED_FALLBACK
    elif evidence.status is SimplePublicNewsEvidenceStatus.SOURCED or (
        evidence.status is SimplePublicNewsEvidenceStatus.PARTIAL
    ):
        if model_generated and verifier_status == "passed":
            kind = SimplePublicNewsResultKind.VERIFIED_FACTUAL
        elif not model_generated and content == SIMPLE_PUBLIC_NEWS_SYNTHESIS_FALLBACK:
            kind = SimplePublicNewsResultKind.SYNTHESIS_SOURCES_ONLY
        elif not model_generated and content == SIMPLE_PUBLIC_NEWS_UNVERIFIED_FALLBACK:
            kind = SimplePublicNewsResultKind.VERIFIER_REJECTED_SOURCES_ONLY
        else:
            raise SimplePublicNewsOutcomeError("source-backed news result is not an accepted projection")
    elif evidence.status is SimplePublicNewsEvidenceStatus.EMPTY:
        kind = SimplePublicNewsResultKind.EMPTY_FALLBACK
    else:
        kind = SimplePublicNewsResultKind.UNAVAILABLE_FALLBACK
    return SimplePublicNewsResult(
        kind=kind,
        content_sha256=content_sha256,
        source_ledger_sha256=(
            source_ledger_sha256
            if kind
            in {
                SimplePublicNewsResultKind.VERIFIED_FACTUAL,
                SimplePublicNewsResultKind.SYNTHESIS_SOURCES_ONLY,
                SimplePublicNewsResultKind.VERIFIER_REJECTED_SOURCES_ONLY,
            }
            else None
        ),
        model_generated=model_generated,
        verifier_status=verifier_status,
        legacy_web_status=legacy_web_status,
        research_call_count=evidence.research_call_count,
    )


def simple_public_news_topic_mismatch_is_empty(
    report: object,
    *,
    expected_topic_class: str,
    expected_max_sources: int = 3,
) -> bool:
    """Recognise only the kernel's closed, outbound, all-filtered report."""

    if not expected_topic_class or not isinstance(report, Mapping):
        return False
    if not target_research_report_is_valid(
        report,
        configured_max_sources=expected_max_sources,
        allow_source_subset=True,
    ):
        return False
    sources = report.get("sources")
    filtered = report.get("topic_filtered_sources")
    completed = report.get("completed_sources")
    failed = report.get("failed_sources")
    requested = report.get("requested_sources")
    timed_out = report.get("timed_out_sources")
    target = report.get("target_sources")
    filtered_count = (
        filtered if isinstance(filtered, int) and not isinstance(filtered, bool) and filtered > 0 else None
    )
    failed_count = failed if isinstance(failed, int) and not isinstance(failed, bool) else None
    requested_count = requested if isinstance(requested, int) and not isinstance(requested, bool) else None
    timed_out_count = (
        timed_out
        if isinstance(timed_out, int) and not isinstance(timed_out, bool) and timed_out >= 0
        else None
    )
    valid_counts = bool(
        filtered_count is not None
        and failed_count is not None
        and failed_count >= filtered_count
        and requested_count is not None
        and timed_out_count is not None
    )
    legacy_complete = bool(
        target is None
        and valid_counts
        and failed_count == filtered_count
        and requested_count == filtered_count
        and timed_out_count == 0
    )
    target_complete = bool(
        isinstance(target, int)
        and not isinstance(target, bool)
        and filtered_count is not None
        and 0 < target <= filtered_count
        and failed_count is not None
        and failed_count >= filtered_count
        and requested_count is not None
        and timed_out_count is not None
        and requested_count == failed_count + timed_out_count
    )
    return bool(
        report.get("topic_class") == expected_topic_class
        and report.get("topic_class_satisfied") is False
        and report.get("outbound_attempted") is True
        and report.get("search_failed") is True
        and report.get("error") == "topic_mismatch"
        and isinstance(sources, list)
        and not sources
        and completed == 0
        and (legacy_complete or target_complete)
        and report.get("search_timed_out") is False
        and report.get("refused", False) is False
        and report.get("quota_exhausted", False) is False
    )


def _accepted_evidence_result_identity(
    evidence: SimplePublicNewsEvidence,
    result: SimplePublicNewsResult,
) -> str:
    evidence_identity = evidence.identity_sha256
    if evidence_identity is None:
        raise SimplePublicNewsOutcomeError("accepted news result has no evidence identity")
    return _canonical_sha256(
        {
            "schema": "friday.simple-public-news-accepted-evidence-result.v1",
            "evidence_sha256": evidence_identity,
            "result_sha256": result.identity_sha256,
        }
    )


def _require_canonical_retained_empty_proof(
    plan: LegacySimplePublicNewsPlan,
    evidence: SimplePublicNewsEvidence,
) -> None:
    if evidence.status is not SimplePublicNewsEvidenceStatus.EMPTY:
        return
    if evidence.empty_kind is SimplePublicNewsEmptyKind.TOPIC_MISMATCH:
        if not plan.topic_class:
            raise SimplePublicNewsOutcomeError("retained topic-mismatch proof has no topic plan")
        filtered = evidence.topic_filtered_sources
        legacy_counts = bool(
            evidence.target_sources is None
            and evidence.requested_sources == filtered
            and evidence.failed_sources == filtered
            and evidence.timed_out_sources == 0
        )
        target_counts = bool(
            evidence.target_sources is not None
            and 0 < evidence.target_sources <= filtered
            and evidence.failed_sources is not None
            and evidence.failed_sources >= filtered
            and evidence.timed_out_sources is not None
            and evidence.requested_sources == evidence.failed_sources + evidence.timed_out_sources
        )
        if (
            filtered <= 0
            or evidence.completed_sources != 0
            or evidence.search_timed_out is not False
            or not (legacy_counts or target_counts)
        ):
            raise SimplePublicNewsOutcomeError("retained topic-mismatch proof is not canonical")
        expected = _canonical_sha256(
            {
                "schema": "friday.simple-public-news-empty-proof.v1",
                "kind": "topic_mismatch",
                "plan_sha256": plan.canonical_sha256(),
                "executed_query_sha256": evidence.executed_query_sha256,
                "requested_sources": evidence.requested_sources,
                "failed_sources": evidence.failed_sources,
                "timed_out_sources": evidence.timed_out_sources,
                "topic_filtered_sources": filtered,
            }
        )
    elif evidence.empty_kind is SimplePublicNewsEmptyKind.VALIDATED_ZERO:
        if (
            evidence.target_sources not in {None, 0}
            or evidence.requested_sources != 0
            or evidence.completed_sources != 0
            or evidence.failed_sources != 0
            or evidence.timed_out_sources != 0
            or evidence.search_timed_out is not False
            or evidence.topic_filtered_sources != 0
        ):
            raise SimplePublicNewsOutcomeError("retained zero-result proof is not canonical")
        expected = _canonical_sha256(
            {
                "schema": "friday.simple-public-news-empty-proof.v1",
                "kind": "validated_zero",
                "plan_sha256": plan.canonical_sha256(),
                "executed_query_sha256": evidence.executed_query_sha256,
                "requested_sources": 0,
                "completed_sources": 0,
                "failed_sources": 0,
                "timed_out_sources": 0,
            }
        )
    else:
        raise SimplePublicNewsOutcomeError("retained empty proof has no closed kind")
    if evidence.empty_proof_sha256 != expected:
        raise SimplePublicNewsOutcomeError("retained empty proof digest is not canonical")


def _expected_outcome(
    plan: LegacySimplePublicNewsPlan,
    evidence: SimplePublicNewsEvidence,
    result: SimplePublicNewsResult,
    *,
    authority_allowed: bool,
) -> CapabilityOutcome:
    if not authority_allowed:
        status = CapabilityOutcomeStatus.DENIED
        evidence_identity = None
        citations: tuple[str, ...] = ()
        authority = True
        verified = False
    elif evidence.status is SimplePublicNewsEvidenceStatus.SOURCED:
        status = (
            CapabilityOutcomeStatus.COMPLETE
            if result.kind is SimplePublicNewsResultKind.VERIFIED_FACTUAL
            else CapabilityOutcomeStatus.PARTIAL
        )
        evidence_identity = _accepted_evidence_result_identity(evidence, result)
        citations = evidence.citation_labels
        authority = True
        verified = True
    elif evidence.status is SimplePublicNewsEvidenceStatus.PARTIAL:
        status = CapabilityOutcomeStatus.PARTIAL
        evidence_identity = _accepted_evidence_result_identity(evidence, result)
        citations = evidence.citation_labels
        authority = True
        verified = True
    elif evidence.status is SimplePublicNewsEvidenceStatus.EMPTY:
        status = CapabilityOutcomeStatus.EMPTY
        evidence_identity = _accepted_evidence_result_identity(evidence, result)
        citations = ()
        authority = True
        verified = True
    else:
        status = CapabilityOutcomeStatus.UNAVAILABLE
        evidence_identity = None
        citations = ()
        authority = False
        verified = False
    return CapabilityOutcome(
        route=RouteClass.WEB_READ,
        status=status,
        plan_sha256=plan.canonical_sha256(),
        evidence_identity_sha256=evidence_identity,
        citation_labels=citations,
        authority_rechecked=authority,
        verified=verified,
    )


def evaluate_simple_public_news_completion(
    outcome: CapabilityOutcome,
    *,
    plan: LegacySimplePublicNewsPlan,
    evidence: SimplePublicNewsEvidence,
    result: SimplePublicNewsResult,
    answer: str,
    current_source_ledger_sha256: str | None,
    current_citation_labels: tuple[str, ...],
    current_model_envelope_sha256: str | None,
    verified_content_sha256: str | None,
    research_call_count: int,
    authority_rechecked: bool,
    authority_allowed: bool,
) -> CompletionGateDecision:
    """Bind the final legacy projection without applying file-answer rules."""

    if type(outcome) is not CapabilityOutcome:
        raise SimplePublicNewsOutcomeError("news completion gate requires CapabilityOutcome v1")
    if type(plan) is not LegacySimplePublicNewsPlan:
        raise SimplePublicNewsOutcomeError("news completion gate requires its code-owned plan")
    if type(evidence) is not SimplePublicNewsEvidence:
        raise SimplePublicNewsOutcomeError("news completion gate requires typed evidence")
    if type(result) is not SimplePublicNewsResult:
        raise SimplePublicNewsOutcomeError("news completion gate requires a typed result")
    if not evidence._factory_seal_is_valid():
        raise SimplePublicNewsOutcomeError("news evidence factory seal does not match its fields")
    if authority_rechecked is not True or not isinstance(authority_allowed, bool):
        raise SimplePublicNewsOutcomeError("news publication authority was not rechecked")
    if evidence.plan_sha256 != plan.canonical_sha256():
        raise SimplePublicNewsOutcomeError("news evidence is not bound to the plan")
    if evidence.executed_query_sha256 != plan.outbound_query_sha256:
        raise SimplePublicNewsOutcomeError("news evidence query is not bound to the plan")
    if len(evidence.citation_labels) > plan.max_sources:
        raise SimplePublicNewsOutcomeError("news evidence exceeds the planned source bound")
    _require_canonical_retained_empty_proof(plan, evidence)
    if result.content_sha256 != _sha256_text(answer, label="news publication content"):
        raise SimplePublicNewsOutcomeError("news result is not bound to the final answer")
    expected_result = build_simple_public_news_result(
        evidence,
        content=answer,
        source_ledger_sha256=current_source_ledger_sha256,
        model_generated=result.model_generated,
        verifier_status=result.verifier_status,
        legacy_web_status=result.legacy_web_status,
        authority_allowed=authority_allowed,
    )
    if result != expected_result:
        raise SimplePublicNewsOutcomeError("news result was not produced by its closed classifier")
    _digest(
        verified_content_sha256,
        label="news verified content sha256",
        optional=True,
    )
    if (
        result.kind is SimplePublicNewsResultKind.VERIFIED_FACTUAL
        and result.content_sha256 != verified_content_sha256
    ):
        raise SimplePublicNewsOutcomeError("news publication changed after its passed verification")
    if (
        research_call_count != evidence.research_call_count
        or result.research_call_count != research_call_count
    ):
        raise SimplePublicNewsOutcomeError("news result call count changed before publication")
    if not isinstance(current_citation_labels, tuple):
        raise SimplePublicNewsOutcomeError("news current labels must be immutable")
    source_bearing_result = result.kind in {
        SimplePublicNewsResultKind.VERIFIED_FACTUAL,
        SimplePublicNewsResultKind.SYNTHESIS_SOURCES_ONLY,
        SimplePublicNewsResultKind.VERIFIER_REJECTED_SOURCES_ONLY,
    }
    if authority_allowed and source_bearing_result:
        if (
            current_source_ledger_sha256 != evidence.source_ledger_sha256
            or result.source_ledger_sha256 != evidence.source_ledger_sha256
            or current_citation_labels != evidence.citation_labels
            or current_model_envelope_sha256 != evidence.model_envelope_sha256
            or result.kind is SimplePublicNewsResultKind.VERIFIED_FACTUAL
            and evidence.model_envelope_sha256 is None
        ):
            raise SimplePublicNewsOutcomeError("news evidence changed before publication")
    elif (
        current_source_ledger_sha256 is not None
        or current_citation_labels
        or current_model_envelope_sha256 is not None
        or result.source_ledger_sha256 is not None
    ):
        raise SimplePublicNewsOutcomeError("source-free news projection retained evidence")
    expected = _expected_outcome(
        plan,
        evidence,
        result,
        authority_allowed=authority_allowed,
    )
    if outcome != expected:
        raise SimplePublicNewsOutcomeError("news capability outcome does not match the final projection")
    return {
        CapabilityOutcomeStatus.COMPLETE: CompletionGateDecision.READY_TO_PUBLISH,
        CapabilityOutcomeStatus.PARTIAL: CompletionGateDecision.RETURN_PARTIAL,
        CapabilityOutcomeStatus.EMPTY: CompletionGateDecision.RETURN_EMPTY,
        # ``web_research`` is catalogued as mutate: it may already have
        # captured Raw/Inbox state, so this gate reports unavailability but
        # never instructs an automatic replay.
        CapabilityOutcomeStatus.UNAVAILABLE: CompletionGateDecision.RETURN_UNAVAILABLE,
        CapabilityOutcomeStatus.DENIED: CompletionGateDecision.DENY,
    }[outcome.status]


def require_accepted_simple_public_news_publication(
    outcome: CapabilityOutcome,
    **gate_inputs: Any,
) -> CapabilityOutcome:
    """Accept a truthful final answer or code-owned fallback, never replay web."""

    evaluate_simple_public_news_completion(outcome, **gate_inputs)
    return outcome


def simple_public_news_outcome(
    plan: LegacySimplePublicNewsPlan,
    evidence: SimplePublicNewsEvidence,
    result: SimplePublicNewsResult,
    *,
    authority_allowed: bool,
) -> CapabilityOutcome:
    """Build the sole outcome admitted by the dedicated news completion gate."""

    return _expected_outcome(plan, evidence, result, authority_allowed=authority_allowed)


__all__ = [
    "SIMPLE_PUBLIC_NEWS_ENVELOPE_FALLBACK",
    "SIMPLE_PUBLIC_NEWS_EVIDENCE_MARKER",
    "SIMPLE_PUBLIC_NEWS_EVIDENCE_MAX_CHARS",
    "SIMPLE_PUBLIC_NEWS_MISSING_FALLBACK",
    "SIMPLE_PUBLIC_NEWS_SYNTHESIS_FALLBACK",
    "SIMPLE_PUBLIC_NEWS_UNVERIFIED_FALLBACK",
    "LegacySimplePublicNewsPlan",
    "SimplePublicNewsEvidence",
    "SimplePublicNewsEvidenceStatus",
    "SimplePublicNewsOutcomeError",
    "SimplePublicNewsResult",
    "SimplePublicNewsResultKind",
    "build_simple_public_news_result",
    "evaluate_simple_public_news_completion",
    "require_accepted_simple_public_news_publication",
    "simple_public_news_model_envelope_identity",
    "simple_public_news_content_identity",
    "simple_public_news_outcome",
    "simple_public_news_source_ledger_identity",
    "simple_public_news_topic_mismatch_is_empty",
]
