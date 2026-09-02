"""Closed private values and the safe projection for read-only archive search.

``web`` means stored web captures and ``external`` means registered sources.
Neither corpus authorizes outbound lookup; this contract has no such field.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Final, TypeVar, cast

from friday.retrieval.contracts import (
    AbsenceDecision,
    CoverageState,
    EmbeddingIdentity,
    LifecycleRef,
    LifecycleState,
    MessageRole,
    MessageWindowLocator,
    PassageRef,
    RepresentationKind,
    ResolvedSource,
    RetrievalContractError,
    RevalidationTarget,
    SearchCorpus,
    SearchCoverage,
    SearchLane,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TemporalFact,
    TemporalPrecision,
    TemporalRole,
    TemporalValueKind,
    TextSpanLocator,
    aggregate_absence_decision,
)

ARCHIVE_SEARCH_REQUEST_SCHEMA: Final = "friday.archive-search-request.private.v2"
ARCHIVE_SEARCH_REQUEST_IDENTITY_SCHEMA: Final = "friday.archive-search-request-identity.private.v2"
_ARCHIVE_SEARCH_REQUEST_SCHEMA_V1: Final = "friday.archive-search-request.private.v1"
_ARCHIVE_SEARCH_REQUEST_IDENTITY_SCHEMA_V1: Final = "friday.archive-search-request-identity.private.v1"
ARCHIVE_SEARCH_CANDIDATE_SCHEMA: Final = "friday.archive-search-candidate.private.v1"
ARCHIVE_SEARCH_PUBLIC_PAGE_SCHEMA: Final = "friday.archive-search-page.public.v1"
_ARCHIVE_SEARCH_CANDIDATE_SCHEMA_V2: Final = "friday.archive-search-candidate.private.v2"
_ARCHIVE_SEARCH_PUBLIC_PAGE_SCHEMA_V2: Final = "friday.archive-search-page.public.v2"
MAX_QUERY_CHARS: Final = 1_000
MAX_FOCUS_CHARS: Final = 480
MAX_FOCUSED_SOURCE_QUERY_CHARS: Final = 240
MAX_HINTS_PER_KIND: Final = 8
MAX_HINT_CHARS: Final = 260
MAX_DISPLAY_CHARS: Final = 260
MAX_EXCERPT_CHARS: Final = 2_000
MAX_PASSAGES_PER_CANDIDATE: Final = 8
MAX_CONTINUATION_CHARS: Final = 512
# The largest existing legacy source-adapter candidate page.  This is an
# internal materialization ceiling, not a model-visible page limit;
# ArchiveSearchRequest.limit remains closed at twenty.
MAX_ARCHIVE_MATERIALIZED_CANDIDATES: Final = 100
_MAX_TEMPORAL_CONSTRAINTS = 8
_MAX_TEMPORAL_FACTS = 32
_MAX_PRIVATE_REQUEST_BYTES = 32_768
_MAX_PRIVATE_CANDIDATE_BYTES = 65_536
_TOKEN = re.compile(r"[A-Za-z0-9_-]+\Z")
E = TypeVar("E", bound=StrEnum)
T = TypeVar("T")


def _control(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def _json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _json_object(
    value: str,
    *,
    label: str = "archive request",
    maximum_bytes: int = _MAX_PRIVATE_REQUEST_BYTES,
) -> dict[str, Any]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RetrievalContractError(f"{label} must be canonical JSON text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RetrievalContractError(f"{label} must be valid UTF-8") from exc
    if len(encoded) > maximum_bytes:
        raise RetrievalContractError(f"{label} exceeds the closed byte limit")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in values:
            if key in result:
                raise RetrievalContractError(f"{label} contains a duplicate key")
            result[key] = item
        return result

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=pairs,
            parse_constant=lambda _item: (_ for _ in ()).throw(
                RetrievalContractError(f"{label} contains a non-finite number")
            ),
        )
    except json.JSONDecodeError as exc:
        raise RetrievalContractError(f"{label} must contain one JSON object") from exc
    if type(parsed) is not dict or value != _json(parsed):
        raise RetrievalContractError(f"{label} must be closed canonical JSON")
    return cast(dict[str, Any], parsed)


def _exact(value: object, keys: frozenset[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != keys:
        raise RetrievalContractError(f"{label} keys do not match the closed contract")
    return cast(dict[str, Any], value)


def _enum(kind: type[E], value: object, label: str) -> E:
    if not isinstance(value, str) or len(value) > 80 or _control(value):
        raise RetrievalContractError(f"{label} must be a closed enum value")
    try:
        return kind(value)
    except ValueError as exc:
        raise RetrievalContractError(f"{label} must be a closed enum value") from exc


def _enum_tuple(values: Iterable[E], kind: type[E], label: str, *, empty: bool) -> tuple[E, ...]:
    items = tuple(values)
    if (not items and not empty) or any(type(item) is not kind for item in items):
        raise RetrievalContractError(f"{label} must use the closed typed contract")
    result = tuple(sorted(items, key=lambda item: item.value))
    if len(result) != len(set(result)):
        raise RetrievalContractError(f"{label} must be unique")
    return result


def _text(value: object, label: str, maximum: int, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or _control(value)
    ):
        raise RetrievalContractError(f"{label} must be bounded canonical text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RetrievalContractError(f"{label} must be valid UTF-8") from exc
    return value


def _passage_excerpt(value: object) -> str:
    """Validate one exact source slice while retaining canonical line structure."""

    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_EXCERPT_CHARS
        or "\r" in value
        or any(unicodedata.category(character).startswith("C") and character != "\n" for character in value)
    ):
        raise RetrievalContractError("passage excerpt must be bounded canonical source text")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RetrievalContractError("passage excerpt must be valid UTF-8") from exc
    return value


def _query(value: object) -> str:
    if not isinstance(value, str):
        raise RetrievalContractError("archive query must be text")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > MAX_QUERY_CHARS or _control(normalized):
        raise RetrievalContractError("archive query must contain at most 1000 canonical characters")
    try:
        normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RetrievalContractError("archive query must be valid UTF-8") from exc
    return normalized


def _focus(value: object) -> str:
    if not isinstance(value, str):
        raise RetrievalContractError("archive focus must be text")
    if value == "":
        return value
    if value != " ".join(value.split()) or len(value) > MAX_FOCUS_CHARS or _control(value):
        raise RetrievalContractError("archive focus must contain at most 480 canonical characters")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise RetrievalContractError("archive focus must be valid UTF-8") from exc
    return value


def _integer(value: object, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RetrievalContractError(f"{label} is outside the closed range")
    return value


def _token(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > MAX_CONTINUATION_CHARS or _TOKEN.fullmatch(value) is None:
        raise RetrievalContractError("continuation must be a bounded opaque token")
    return value


def _hints(values: Iterable[object], label: str) -> tuple[str, ...]:
    items = tuple(values)
    if len(items) > MAX_HINTS_PER_KIND:
        raise RetrievalContractError(f"{label} exceeds the closed item limit")
    result = tuple(sorted(cast(str, _text(item, label, MAX_HINT_CHARS)) for item in items))
    if len(result) != len(set(result)):
        raise RetrievalContractError(f"{label} must be unique")
    return result


def _typed_sorted(
    values: Iterable[T],
    kind: type[T],
    key: Callable[[T], str],
    maximum: int,
    label: str,
) -> tuple[T, ...]:
    items = tuple(values)
    if len(items) > maximum or any(type(item) is not kind for item in items):
        raise RetrievalContractError(f"{label} exceed the closed typed contract")
    result = tuple(sorted(items, key=key))
    keys = tuple(key(item) for item in result)
    if len(keys) != len(set(keys)):
        raise RetrievalContractError(f"{label} must have unique exact identities")
    return result


class ArchiveSearchCorpus(StrEnum):
    DOCUMENTS = "documents"
    KNOWLEDGE = "knowledge"
    MESSAGES = "messages"
    OBSIDIAN = "obsidian"
    GENERATED = "generated"
    WEB = "web"
    EXTERNAL = "external"


class ConversationScope(StrEnum):
    ALL = "all"
    CURRENT = "current"


class ReviewScope(StrEnum):
    CONFIRMED_ONLY = "confirmed_only"
    DISCOVERABLE = "discoverable"


class ArchiveReviewState(StrEnum):
    CONFIRMED = "confirmed"
    PENDING = "pending"
    ARCHIVED = "archived"
    NOT_APPLICABLE = "not_applicable"


class ArchiveEvidenceAuthority(StrEnum):
    CANONICAL = "canonical"
    NONCANONICAL = "noncanonical"
    NAVIGATION_ONLY = "navigation_only"


class ArchiveMatchChannel(StrEnum):
    CATALOG = SearchLane.CATALOG.value
    EXACT_IDENTITY = SearchLane.EXACT_IDENTITY.value
    LEXICAL = SearchLane.LEXICAL.value
    APPROXIMATE_IDENTITY = SearchLane.APPROXIMATE_IDENTITY.value
    DENSE = SearchLane.DENSE.value
    MESSAGE_HISTORY = SearchLane.MESSAGE_HISTORY.value

    @property
    def search_lane(self) -> SearchLane:
        return SearchLane(self.value)


@dataclass(frozen=True, slots=True)
class ArchiveMatchRank:
    channel: ArchiveMatchChannel
    rank: int

    def __post_init__(self) -> None:
        if not isinstance(self.channel, ArchiveMatchChannel):
            raise RetrievalContractError("match rank requires a closed channel")
        _integer(self.rank, "match lane rank", 1, 1_000_000_000)

    def to_private_payload(self) -> dict[str, object]:
        return {"channel": self.channel.value, "rank": self.rank}

    @classmethod
    def from_private_payload(cls, value: object) -> ArchiveMatchRank:
        payload = _exact(value, frozenset({"channel", "rank"}), "match rank")
        return cls(
            _enum(ArchiveMatchChannel, payload["channel"], "match channel"),
            _integer(payload["rank"], "match lane rank", 1, 1_000_000_000),
        )


class ArchiveSearchWarning(StrEnum):
    BACKFILL_PENDING = "backfill_pending"
    CONTINUATION_UNAVAILABLE = "continuation_unavailable"
    EXTERNAL_OUTBOUND_DISABLED = "external_outbound_disabled"
    LANE_CAPPED = "lane_capped"
    LANE_UNAVAILABLE = "lane_unavailable"
    PERMISSION_FILTERED = "permission_filtered"
    SNAPSHOT_CHANGED = "snapshot_changed"


_SEARCH_CORPUS = {
    ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
    ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
    ArchiveSearchCorpus.MESSAGES: SearchCorpus.CONVERSATION,
    ArchiveSearchCorpus.OBSIDIAN: SearchCorpus.OBSIDIAN,
    ArchiveSearchCorpus.GENERATED: SearchCorpus.GENERATED_ARTIFACTS,
    ArchiveSearchCorpus.WEB: SearchCorpus.WEB_CAPTURES,
    ArchiveSearchCorpus.EXTERNAL: SearchCorpus.EXTERNAL,
}
_SOURCE_KINDS = {
    ArchiveSearchCorpus.DOCUMENTS: frozenset({SourceKind.DOCUMENT}),
    ArchiveSearchCorpus.KNOWLEDGE: frozenset(
        {SourceKind.DOCUMENT, SourceKind.WEB_CAPTURE, SourceKind.GENERATED_ARTIFACT}
    ),
    ArchiveSearchCorpus.MESSAGES: frozenset({SourceKind.CONVERSATION}),
    ArchiveSearchCorpus.OBSIDIAN: frozenset({SourceKind.OBSIDIAN_NOTE}),
    ArchiveSearchCorpus.GENERATED: frozenset({SourceKind.GENERATED_ARTIFACT}),
    ArchiveSearchCorpus.WEB: frozenset({SourceKind.WEB_CAPTURE}),
    ArchiveSearchCorpus.EXTERNAL: frozenset({SourceKind.EXTERNAL_REGISTERED_SOURCE}),
}

_DOCUMENT_LIKE = frozenset(
    {
        ArchiveSearchCorpus.DOCUMENTS,
        ArchiveSearchCorpus.KNOWLEDGE,
        ArchiveSearchCorpus.OBSIDIAN,
        ArchiveSearchCorpus.GENERATED,
        ArchiveSearchCorpus.WEB,
        ArchiveSearchCorpus.EXTERNAL,
    }
)
_CONTENT_BEARING = frozenset({*_DOCUMENT_LIKE, ArchiveSearchCorpus.MESSAGES})
_TEMPORAL_ROLE_CORPORA = {
    TemporalRole.DOCUMENT_CREATED_AT: _DOCUMENT_LIKE,
    TemporalRole.DOCUMENT_MODIFIED_AT: _DOCUMENT_LIKE,
    TemporalRole.RECEIVED_AT: _DOCUMENT_LIKE - {ArchiveSearchCorpus.OBSIDIAN},
    TemporalRole.UPLOADED_AT: _DOCUMENT_LIKE - {ArchiveSearchCorpus.OBSIDIAN},
    TemporalRole.INDEXED_AT: _DOCUMENT_LIKE,
    TemporalRole.EVENT_DATE: _CONTENT_BEARING,
    TemporalRole.MENTIONED_DATE: _CONTENT_BEARING,
    TemporalRole.CONVERSATION_TIME: frozenset({ArchiveSearchCorpus.MESSAGES}),
    TemporalRole.VALID_FROM: _CONTENT_BEARING,
    TemporalRole.VALID_TO: _CONTENT_BEARING,
    TemporalRole.KNOWLEDGE_PROJECTION_CREATED_AT: frozenset({ArchiveSearchCorpus.KNOWLEDGE}),
    TemporalRole.KNOWLEDGE_PROJECTION_MODIFIED_AT: frozenset({ArchiveSearchCorpus.KNOWLEDGE}),
    TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE: frozenset(
        {ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE}
    ),
}
_INSTANT_ONLY_ROLES = frozenset(
    {
        TemporalRole.RECEIVED_AT,
        TemporalRole.UPLOADED_AT,
        TemporalRole.INDEXED_AT,
        TemporalRole.CONVERSATION_TIME,
        TemporalRole.KNOWLEDGE_PROJECTION_CREATED_AT,
        TemporalRole.KNOWLEDGE_PROJECTION_MODIFIED_AT,
    }
)


def _date_or_utc(value: object, label: str) -> tuple[str, date | datetime]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise RetrievalContractError(f"{label} must be a canonical date or UTC instant")
    try:
        parsed_date = date.fromisoformat(value)
    except ValueError:
        parsed_date = None
    if parsed_date is not None and value == parsed_date.isoformat():
        return "date", parsed_date
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetrievalContractError(f"{label} must be a canonical date or UTC instant") from exc
    if instant.tzinfo is None or instant.utcoffset() is None or value != instant.astimezone(UTC).isoformat():
        raise RetrievalContractError(f"{label} must already be normalized to UTC")
    return "instant", instant.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ArchiveTemporalConstraint:
    corpus: ArchiveSearchCorpus
    role: TemporalRole
    value_kind: TemporalValueKind
    precision: TemporalPrecision
    start: str
    end: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.corpus, ArchiveSearchCorpus)
            or not isinstance(self.role, TemporalRole)
            or not isinstance(self.value_kind, TemporalValueKind)
            or not isinstance(self.precision, TemporalPrecision)
        ):
            raise RetrievalContractError("temporal constraint requires exact typed fields")
        if self.corpus not in _TEMPORAL_ROLE_CORPORA[self.role]:
            raise RetrievalContractError("temporal role is not applicable to the selected corpus")
        start_kind, start = _date_or_utc(self.start, "temporal start")
        end_kind, end = _date_or_utc(self.end, "temporal end")
        if start_kind != end_kind or end <= start:
            raise RetrievalContractError("temporal constraint must be a non-empty half-open range")
        if self.value_kind is TemporalValueKind.INSTANT:
            if start_kind != "instant" or self.precision is not TemporalPrecision.INSTANT:
                raise RetrievalContractError("instant constraints require UTC instant bounds and precision")
        elif self.precision is TemporalPrecision.INSTANT or start_kind != "date":
            raise RetrievalContractError("date constraints require date bounds and bounded precision")
        else:
            assert isinstance(start, date) and isinstance(end, date)
            if self.precision is TemporalPrecision.YEAR and any(
                (item.month, item.day) != (1, 1) for item in (start, end)
            ):
                raise RetrievalContractError("year constraints must use January 1 boundaries")
            if self.precision is TemporalPrecision.MONTH and any(item.day != 1 for item in (start, end)):
                raise RetrievalContractError("month constraints must use first-day boundaries")
        if self.role in _INSTANT_ONLY_ROLES and self.value_kind is not TemporalValueKind.INSTANT:
            raise RetrievalContractError("this temporal role requires exact instant bounds")
        if self.role is TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE and (
            self.value_kind is not TemporalValueKind.DATE_INTERVAL
            or self.precision is not TemporalPrecision.DAY
        ):
            raise RetrievalContractError("legacy document dates require exact day precision")

    def to_private_payload(self) -> dict[str, str]:
        return {
            "corpus": self.corpus.value,
            "end": self.end,
            "precision": self.precision.value,
            "role": self.role.value,
            "start": self.start,
            "value_kind": self.value_kind.value,
        }

    @classmethod
    def from_private_payload(cls, value: object) -> ArchiveTemporalConstraint:
        payload = _exact(
            value,
            frozenset({"corpus", "end", "precision", "role", "start", "value_kind"}),
            "temporal constraint",
        )
        if not isinstance(payload["start"], str) or not isinstance(payload["end"], str):
            raise RetrievalContractError("temporal constraint bounds must be text")
        return cls(
            _enum(ArchiveSearchCorpus, payload["corpus"], "temporal corpus"),
            _enum(TemporalRole, payload["role"], "temporal role"),
            _enum(TemporalValueKind, payload["value_kind"], "temporal value kind"),
            _enum(TemporalPrecision, payload["precision"], "temporal precision"),
            payload["start"],
            payload["end"],
        )


def _constraints(values: Iterable[ArchiveTemporalConstraint]) -> tuple[ArchiveTemporalConstraint, ...]:
    return _typed_sorted(
        values,
        ArchiveTemporalConstraint,
        lambda item: (
            f"{item.corpus.value}\0{item.role.value}\0{item.value_kind.value}\0"
            f"{item.precision.value}\0{item.start}\0{item.end}"
        ),
        _MAX_TEMPORAL_CONSTRAINTS,
        "temporal constraints",
    )


_CORPUS_LIFECYCLE_REPRESENTATIONS = {
    ArchiveSearchCorpus.DOCUMENTS: frozenset(
        {
            RepresentationKind.RAW_OBJECT,
            RepresentationKind.INBOX_ITEM,
            RepresentationKind.KNOWLEDGE_OBJECT,
        }
    ),
    ArchiveSearchCorpus.KNOWLEDGE: frozenset({RepresentationKind.KNOWLEDGE_OBJECT}),
    ArchiveSearchCorpus.MESSAGES: frozenset({RepresentationKind.CONVERSATION}),
    ArchiveSearchCorpus.OBSIDIAN: frozenset({RepresentationKind.OBSIDIAN_BINDING}),
    ArchiveSearchCorpus.GENERATED: frozenset(
        {
            RepresentationKind.RAW_OBJECT,
            RepresentationKind.INBOX_ITEM,
            RepresentationKind.KNOWLEDGE_OBJECT,
        }
    ),
    ArchiveSearchCorpus.WEB: frozenset(
        {
            RepresentationKind.RAW_OBJECT,
            RepresentationKind.INBOX_ITEM,
            RepresentationKind.KNOWLEDGE_OBJECT,
        }
    ),
    ArchiveSearchCorpus.EXTERNAL: frozenset({RepresentationKind.EXTERNAL_SOURCE}),
}
_CORPUS_LIFECYCLE_PRIORITY = {
    ArchiveSearchCorpus.DOCUMENTS: (
        RepresentationKind.INBOX_ITEM,
        RepresentationKind.KNOWLEDGE_OBJECT,
        RepresentationKind.RAW_OBJECT,
    ),
    ArchiveSearchCorpus.KNOWLEDGE: (RepresentationKind.KNOWLEDGE_OBJECT,),
    ArchiveSearchCorpus.MESSAGES: (RepresentationKind.CONVERSATION,),
    ArchiveSearchCorpus.OBSIDIAN: (RepresentationKind.OBSIDIAN_BINDING,),
    ArchiveSearchCorpus.GENERATED: (
        RepresentationKind.INBOX_ITEM,
        RepresentationKind.KNOWLEDGE_OBJECT,
        RepresentationKind.RAW_OBJECT,
    ),
    ArchiveSearchCorpus.WEB: (
        RepresentationKind.INBOX_ITEM,
        RepresentationKind.KNOWLEDGE_OBJECT,
        RepresentationKind.RAW_OBJECT,
    ),
    ArchiveSearchCorpus.EXTERNAL: (RepresentationKind.EXTERNAL_SOURCE,),
}
_REPRESENTATION_LIFECYCLE_STATES = {
    RepresentationKind.RAW_OBJECT: frozenset({LifecycleState.ACTIVE, LifecycleState.DELETED}),
    RepresentationKind.INBOX_ITEM: frozenset(
        {
            LifecycleState.PENDING,
            LifecycleState.CLASSIFIED,
            LifecycleState.ARCHIVED,
            LifecycleState.IGNORED,
        }
    ),
    RepresentationKind.KNOWLEDGE_OBJECT: frozenset(
        {
            LifecycleState.ACTIVE,
            LifecycleState.ARCHIVED,
            LifecycleState.DEPRECATED,
            LifecycleState.DELETED,
        }
    ),
    RepresentationKind.OBSIDIAN_BINDING: frozenset(
        {LifecycleState.ACTIVE, LifecycleState.TOMBSTONED, LifecycleState.DELETED}
    ),
    RepresentationKind.CONVERSATION: frozenset(
        {LifecycleState.ACTIVE, LifecycleState.ARCHIVED, LifecycleState.DELETED}
    ),
    RepresentationKind.EXTERNAL_SOURCE: frozenset(
        {LifecycleState.ACTIVE, LifecycleState.UNAVAILABLE, LifecycleState.DELETED}
    ),
}


def _allowed_lifecycle_states(corpus: ArchiveSearchCorpus) -> frozenset[LifecycleState]:
    return frozenset(
        state
        for representation in _CORPUS_LIFECYCLE_REPRESENTATIONS[corpus]
        for state in _REPRESENTATION_LIFECYCLE_STATES[representation]
    )


@dataclass(frozen=True, slots=True)
class ArchiveLifecycleConstraint:
    corpus: ArchiveSearchCorpus
    states: tuple[LifecycleState, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.corpus, ArchiveSearchCorpus):
            raise RetrievalContractError("lifecycle constraint requires a closed corpus")
        if type(self.states) is not tuple:
            raise RetrievalContractError("lifecycle states must use an exact tuple")
        canonical = _enum_tuple(self.states, LifecycleState, "lifecycle states", empty=False)
        if self.states != canonical or not set(self.states) <= _allowed_lifecycle_states(self.corpus):
            raise RetrievalContractError("lifecycle constraint is not canonical for its corpus")

    @classmethod
    def create(
        cls,
        corpus: ArchiveSearchCorpus,
        states: Iterable[LifecycleState],
    ) -> ArchiveLifecycleConstraint:
        return cls(corpus, _enum_tuple(states, LifecycleState, "lifecycle states", empty=False))

    def to_private_payload(self) -> dict[str, object]:
        return {"corpus": self.corpus.value, "states": [item.value for item in self.states]}

    @classmethod
    def from_private_payload(cls, value: object) -> ArchiveLifecycleConstraint:
        payload = _exact(value, frozenset({"corpus", "states"}), "lifecycle constraint")
        states = payload["states"]
        if type(states) is not list:
            raise RetrievalContractError("lifecycle constraint states must be an array")
        return cls.create(
            _enum(ArchiveSearchCorpus, payload["corpus"], "lifecycle corpus"),
            (_enum(LifecycleState, item, "lifecycle state") for item in states),
        )


def _lifecycle_constraints(
    values: Iterable[ArchiveLifecycleConstraint],
) -> tuple[ArchiveLifecycleConstraint, ...]:
    result = _typed_sorted(
        values,
        ArchiveLifecycleConstraint,
        lambda item: item.corpus.value,
        len(ArchiveSearchCorpus),
        "lifecycle constraints",
    )
    return result


@dataclass(frozen=True, slots=True)
class ArchiveContextWindow:
    before: int = 0
    after: int = 0

    def __post_init__(self) -> None:
        _integer(self.before, "context before", 0, 3)
        _integer(self.after, "context after", 0, 3)

    def to_private_payload(self) -> dict[str, int]:
        return {"after": self.after, "before": self.before}

    @classmethod
    def from_private_payload(cls, value: object) -> ArchiveContextWindow:
        payload = _exact(value, frozenset({"after", "before"}), "context window")
        return cls(
            _integer(payload["before"], "context before", 0, 3),
            _integer(payload["after"], "context after", 0, 3),
        )


_PRIVATE_REQUEST_V2_KEYS = frozenset(
    {
        "context",
        "continuation",
        "conversation_scope",
        "corpora",
        "entity_hints",
        "filename_hints",
        "focus",
        "lifecycle_constraints",
        "limit",
        "query",
        "review_scope",
        "roles",
        "schema",
        "temporal_constraints",
        "title_hints",
    }
)
_PRIVATE_REQUEST_V1_KEYS = _PRIVATE_REQUEST_V2_KEYS - {"focus"}
_MODEL_REQUEST_KEYS = _PRIVATE_REQUEST_V2_KEYS - {"schema"}


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveSearchRequest:
    query: str
    corpora: tuple[ArchiveSearchCorpus, ...]
    title_hints: tuple[str, ...]
    filename_hints: tuple[str, ...]
    entity_hints: tuple[str, ...]
    temporal_constraints: tuple[ArchiveTemporalConstraint, ...]
    lifecycle_constraints: tuple[ArchiveLifecycleConstraint, ...]
    conversation_scope: ConversationScope
    roles: tuple[MessageRole, ...]
    review_scope: ReviewScope
    limit: int
    context: ArchiveContextWindow
    continuation: str | None
    focus: str = ""

    def __post_init__(self) -> None:
        if any(
            type(value) is not tuple
            for value in (
                self.corpora,
                self.title_hints,
                self.filename_hints,
                self.entity_hints,
                self.temporal_constraints,
                self.lifecycle_constraints,
                self.roles,
            )
        ):
            raise RetrievalContractError("archive request collections must use exact tuples")
        if self.query != _query(self.query):
            raise RetrievalContractError("archive query must already be canonical")
        if self.focus != _focus(self.focus):
            raise RetrievalContractError("archive focus must already be canonical")
        if self.corpora != _enum_tuple(self.corpora, ArchiveSearchCorpus, "archive corpora", empty=False):
            raise RetrievalContractError("archive corpora must be sorted and unique")
        if self.title_hints != _hints(self.title_hints, "title hints"):
            raise RetrievalContractError("title hints must be sorted and unique")
        if self.filename_hints != _hints(self.filename_hints, "filename hints"):
            raise RetrievalContractError("filename hints must be sorted and unique")
        if self.entity_hints != _hints(self.entity_hints, "entity hints"):
            raise RetrievalContractError("entity hints must be sorted and unique")
        if self.temporal_constraints != _constraints(self.temporal_constraints):
            raise RetrievalContractError("temporal constraints must be sorted and unique")
        if any(item.corpus not in self.corpora for item in self.temporal_constraints):
            raise RetrievalContractError("temporal constraint corpus was not requested")
        if self.lifecycle_constraints != _lifecycle_constraints(self.lifecycle_constraints):
            raise RetrievalContractError("lifecycle constraints must be sorted and unique")
        if any(item.corpus not in self.corpora for item in self.lifecycle_constraints):
            raise RetrievalContractError("lifecycle constraint corpus was not requested")
        if not isinstance(self.conversation_scope, ConversationScope):
            raise RetrievalContractError("conversation scope must be a closed enum")
        if self.roles != _enum_tuple(self.roles, MessageRole, "message roles", empty=True):
            raise RetrievalContractError("message roles must be sorted and unique")
        if any(item not in {MessageRole.USER, MessageRole.ASSISTANT} for item in self.roles):
            raise RetrievalContractError("archive message roles allow only user and assistant")
        if not isinstance(self.review_scope, ReviewScope) or type(self.context) is not ArchiveContextWindow:
            raise RetrievalContractError("request scopes must use closed typed contracts")
        _integer(self.limit, "archive result limit", 1, 20)
        _token(self.continuation)
        if ArchiveSearchCorpus.MESSAGES not in self.corpora and (
            self.roles
            or self.conversation_scope is ConversationScope.CURRENT
            or self.context != ArchiveContextWindow()
        ):
            raise RetrievalContractError("conversation filters require the messages corpus")
        if self.focus and (
            self.corpora != (ArchiveSearchCorpus.DOCUMENTS,)
            or len(self.query) > MAX_FOCUSED_SOURCE_QUERY_CHARS
        ):
            raise RetrievalContractError(
                "archive focus requires a documents-only request and a query of at most 240 characters"
            )
        if len(self.to_private_json().encode("ascii")) > _MAX_PRIVATE_REQUEST_BYTES:
            raise RetrievalContractError("archive request exceeds the closed byte limit")

    def __repr__(self) -> str:
        return f"ArchiveSearchRequest(private_query=True, corpus_count={len(self.corpora)})"

    @property
    def permits_outbound(self) -> bool:
        return False

    @property
    def dense_query(self) -> str:
        """Exact text bound into the optional dense source-recall sidecar."""

        return f"{self.query} {self.focus}" if self.focus else self.query

    @classmethod
    def create(
        cls,
        *,
        query: str,
        corpora: Iterable[ArchiveSearchCorpus],
        title_hints: Iterable[object] = (),
        filename_hints: Iterable[object] = (),
        entity_hints: Iterable[object] = (),
        temporal_constraints: Iterable[ArchiveTemporalConstraint] = (),
        lifecycle_constraints: Iterable[ArchiveLifecycleConstraint] = (),
        conversation_scope: ConversationScope = ConversationScope.ALL,
        roles: Iterable[MessageRole] = (),
        review_scope: ReviewScope = ReviewScope.DISCOVERABLE,
        limit: int = 10,
        context: ArchiveContextWindow = ArchiveContextWindow(),
        continuation: str | None = None,
        focus: str = "",
    ) -> ArchiveSearchRequest:
        return cls(
            _query(query),
            _enum_tuple(corpora, ArchiveSearchCorpus, "archive corpora", empty=False),
            _hints(title_hints, "title hints"),
            _hints(filename_hints, "filename hints"),
            _hints(entity_hints, "entity hints"),
            _constraints(temporal_constraints),
            _lifecycle_constraints(lifecycle_constraints),
            conversation_scope,
            _enum_tuple(roles, MessageRole, "message roles", empty=True),
            review_scope,
            _integer(limit, "archive result limit", 1, 20),
            context,
            _token(continuation),
            _focus(focus),
        )

    def to_private_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "context": self.context.to_private_payload(),
            "continuation": self.continuation,
            "conversation_scope": self.conversation_scope.value,
            "corpora": [item.value for item in self.corpora],
            "entity_hints": list(self.entity_hints),
            "filename_hints": list(self.filename_hints),
            "lifecycle_constraints": [item.to_private_payload() for item in self.lifecycle_constraints],
            "limit": self.limit,
            "query": self.query,
            "review_scope": self.review_scope.value,
            "roles": [item.value for item in self.roles],
            "schema": (ARCHIVE_SEARCH_REQUEST_SCHEMA if self.focus else _ARCHIVE_SEARCH_REQUEST_SCHEMA_V1),
            "temporal_constraints": [item.to_private_payload() for item in self.temporal_constraints],
            "title_hints": list(self.title_hints),
        }
        if self.focus:
            payload["focus"] = self.focus
        return payload

    def to_private_json(self) -> str:
        return _json(self.to_private_payload())

    def to_identity_payload(self) -> dict[str, object]:
        """Canonical private request identity; a page token is transport, not intent."""

        payload = self.to_private_payload()
        del payload["continuation"]
        payload["schema"] = (
            ARCHIVE_SEARCH_REQUEST_IDENTITY_SCHEMA
            if self.focus
            else _ARCHIVE_SEARCH_REQUEST_IDENTITY_SCHEMA_V1
        )
        return payload

    def to_identity_json(self) -> str:
        return _json(self.to_identity_payload())

    def identity_digest_material(self) -> bytes:
        domain = (
            b"friday/archive-search-request-identity/v2\0"
            if self.focus
            else b"friday/archive-search-request-identity/v1\0"
        )
        return domain + self.to_identity_json().encode("ascii")

    @classmethod
    def _from_fields(cls, payload: Mapping[str, object]) -> ArchiveSearchRequest:
        names = (
            "corpora",
            "title_hints",
            "filename_hints",
            "entity_hints",
            "temporal_constraints",
            "lifecycle_constraints",
            "roles",
        )
        raw = {name: payload.get(name, []) for name in names}
        if any(type(value) is not list for value in raw.values()):
            raise RetrievalContractError("archive request collections must be arrays")
        return cls.create(
            query=cast(str, payload.get("query")),
            corpora=(
                _enum(ArchiveSearchCorpus, item, "archive corpus")
                for item in cast(list[object], raw["corpora"])
            ),
            title_hints=cast(list[object], raw["title_hints"]),
            filename_hints=cast(list[object], raw["filename_hints"]),
            entity_hints=cast(list[object], raw["entity_hints"]),
            temporal_constraints=(
                ArchiveTemporalConstraint.from_private_payload(item)
                for item in cast(list[object], raw["temporal_constraints"])
            ),
            lifecycle_constraints=(
                ArchiveLifecycleConstraint.from_private_payload(item)
                for item in cast(list[object], raw["lifecycle_constraints"])
            ),
            conversation_scope=_enum(
                ConversationScope,
                payload.get("conversation_scope", ConversationScope.ALL.value),
                "conversation scope",
            ),
            roles=(_enum(MessageRole, item, "message role") for item in cast(list[object], raw["roles"])),
            review_scope=_enum(
                ReviewScope,
                payload.get("review_scope", ReviewScope.DISCOVERABLE.value),
                "review scope",
            ),
            limit=_integer(payload.get("limit", 10), "archive result limit", 1, 20),
            context=ArchiveContextWindow.from_private_payload(
                payload.get("context", {"after": 0, "before": 0})
            ),
            continuation=_token(payload.get("continuation")),
            focus=_focus(payload.get("focus", "")),
        )

    @classmethod
    def from_model_payload(cls, value: object) -> ArchiveSearchRequest:
        if type(value) is not dict:
            raise RetrievalContractError("archive request must be one closed object")
        payload = cast(dict[str, object], value)
        if not {"query", "corpora"} <= set(payload) or not set(payload) <= _MODEL_REQUEST_KEYS:
            raise RetrievalContractError("archive request keys do not match the closed contract")
        return cls._from_fields(payload)

    @classmethod
    def from_private_payload(cls, value: object) -> ArchiveSearchRequest:
        schema = value.get("schema") if type(value) is dict else None
        if schema == _ARCHIVE_SEARCH_REQUEST_SCHEMA_V1:
            payload = _exact(value, _PRIVATE_REQUEST_V1_KEYS, "archive request")
        elif schema == ARCHIVE_SEARCH_REQUEST_SCHEMA:
            payload = _exact(value, _PRIVATE_REQUEST_V2_KEYS, "archive request")
            if payload["focus"] == "":
                raise RetrievalContractError("archive request v2 requires a non-empty focus")
        else:
            raise RetrievalContractError("archive request schema is unsupported")
        return cls._from_fields(payload)

    @classmethod
    def parse_private(cls, value: str) -> ArchiveSearchRequest:
        result = cls.from_private_payload(_json_object(value))
        if value != result.to_private_json():
            raise RetrievalContractError("archive request JSON is not semantically canonical")
        return result

    @classmethod
    def parse(cls, value: object) -> ArchiveSearchRequest:
        return cls.parse_private(value) if isinstance(value, str) else cls.from_model_payload(value)


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveSearchPassage:
    passage_ref: PassageRef
    excerpt: str

    def __post_init__(self) -> None:
        passage_ref = self.passage_ref
        if (
            type(passage_ref) is not PassageRef
            or type(passage_ref.source_ref) is not SourceRef
            or type(passage_ref.source_revision) is not SourceRevision
            or type(passage_ref.source_revision.representation) is not SourceRepresentation
            or type(passage_ref.locator) not in {TextSpanLocator, MessageWindowLocator}
            or type(passage_ref.embedding) is not EmbeddingIdentity
        ):
            raise RetrievalContractError("factual excerpts require an exact PassageRef")
        _passage_excerpt(self.excerpt)

    def __repr__(self) -> str:
        return "ArchiveSearchPassage(private_passage=True)"

    def to_private_payload(self) -> dict[str, object]:
        """Serialize only the released standalone single-line carrier."""

        return _passage_to_private_payload(self, allow_multiline=False)

    @classmethod
    def from_private_payload(cls, value: object) -> ArchiveSearchPassage:
        """Parse the released single-line passage domain.

        Multiline passages are admitted only by the explicitly versioned
        candidate-v2 parser below.  Keeping this standalone parser narrow makes
        an old candidate-v1 carrier fail closed instead of silently widening its
        durable evidence semantics.
        """

        return _passage_from_private_payload(value, allow_multiline=False)


def _passage_to_private_payload(
    passage: ArchiveSearchPassage,
    *,
    allow_multiline: bool,
) -> dict[str, object]:
    if type(passage) is not ArchiveSearchPassage or type(allow_multiline) is not bool:
        raise RetrievalContractError("archive passage serializer mode is invalid")
    if "\n" in passage.excerpt and not allow_multiline:
        raise RetrievalContractError("passage excerpt requires an explicitly versioned carrier")
    return {"excerpt": passage.excerpt, "passage_ref": passage.passage_ref.to_private_payload()}


def _passage_from_private_payload(
    value: object,
    *,
    allow_multiline: bool,
) -> ArchiveSearchPassage:
    if type(allow_multiline) is not bool:
        raise RetrievalContractError("archive passage parser mode is invalid")
    payload = _exact(value, frozenset({"excerpt", "passage_ref"}), "archive passage")
    if allow_multiline:
        excerpt = _passage_excerpt(payload["excerpt"])
    else:
        single_line_excerpt = _text(payload["excerpt"], "passage excerpt", MAX_EXCERPT_CHARS)
        assert isinstance(single_line_excerpt, str)
        excerpt = single_line_excerpt
    return ArchiveSearchPassage(PassageRef.from_private_payload(payload["passage_ref"]), excerpt)


def _candidate_uses_multiline_passage(candidate: ArchiveSearchCandidate) -> bool:
    return any("\n" in passage.excerpt for passage in candidate.passages)


def _passages(values: Iterable[ArchiveSearchPassage]) -> tuple[ArchiveSearchPassage, ...]:
    return _typed_sorted(
        values,
        ArchiveSearchPassage,
        lambda item: item.passage_ref.to_private_json(),
        MAX_PASSAGES_PER_CANDIDATE,
        "archive passages",
    )


def _facts(values: Iterable[TemporalFact]) -> tuple[TemporalFact, ...]:
    return _typed_sorted(
        values, TemporalFact, lambda item: item.to_private_json(), _MAX_TEMPORAL_FACTS, "temporal facts"
    )


def _match_ranks(values: Iterable[ArchiveMatchRank]) -> tuple[ArchiveMatchRank, ...]:
    return _typed_sorted(
        values,
        ArchiveMatchRank,
        lambda item: item.channel.value,
        len(ArchiveMatchChannel),
        "match channels",
    )


_CANDIDATE_KEYS = frozenset(
    {
        "corpus",
        "evidence_authority",
        "filename",
        "lifecycle_state",
        "matches",
        "passages",
        "resolved_source",
        "review_state",
        "schema",
        "temporal_facts",
        "title",
    }
)

_CORPUS_REVIEW_STATES = {
    ArchiveSearchCorpus.DOCUMENTS: frozenset(
        {ArchiveReviewState.CONFIRMED, ArchiveReviewState.PENDING, ArchiveReviewState.ARCHIVED}
    ),
    ArchiveSearchCorpus.KNOWLEDGE: frozenset({ArchiveReviewState.CONFIRMED, ArchiveReviewState.ARCHIVED}),
    ArchiveSearchCorpus.MESSAGES: frozenset({ArchiveReviewState.NOT_APPLICABLE}),
    ArchiveSearchCorpus.OBSIDIAN: frozenset({ArchiveReviewState.NOT_APPLICABLE}),
    ArchiveSearchCorpus.GENERATED: frozenset({ArchiveReviewState.NOT_APPLICABLE}),
    ArchiveSearchCorpus.WEB: frozenset({ArchiveReviewState.NOT_APPLICABLE}),
    ArchiveSearchCorpus.EXTERNAL: frozenset({ArchiveReviewState.NOT_APPLICABLE}),
}
_CORPUS_EVIDENCE_AUTHORITIES = {
    ArchiveSearchCorpus.DOCUMENTS: frozenset(ArchiveEvidenceAuthority),
    ArchiveSearchCorpus.KNOWLEDGE: frozenset(
        {ArchiveEvidenceAuthority.CANONICAL, ArchiveEvidenceAuthority.NAVIGATION_ONLY}
    ),
    ArchiveSearchCorpus.MESSAGES: frozenset(
        {ArchiveEvidenceAuthority.CANONICAL, ArchiveEvidenceAuthority.NAVIGATION_ONLY}
    ),
    ArchiveSearchCorpus.OBSIDIAN: frozenset(
        {ArchiveEvidenceAuthority.CANONICAL, ArchiveEvidenceAuthority.NAVIGATION_ONLY}
    ),
    ArchiveSearchCorpus.GENERATED: frozenset(ArchiveEvidenceAuthority),
    ArchiveSearchCorpus.WEB: frozenset(ArchiveEvidenceAuthority),
    ArchiveSearchCorpus.EXTERNAL: frozenset(ArchiveEvidenceAuthority),
}
_REVIEW_EVIDENCE_AUTHORITIES = {
    ArchiveReviewState.CONFIRMED: frozenset(
        {ArchiveEvidenceAuthority.CANONICAL, ArchiveEvidenceAuthority.NAVIGATION_ONLY}
    ),
    ArchiveReviewState.PENDING: frozenset(
        {ArchiveEvidenceAuthority.NONCANONICAL, ArchiveEvidenceAuthority.NAVIGATION_ONLY}
    ),
    ArchiveReviewState.ARCHIVED: frozenset(
        {ArchiveEvidenceAuthority.CANONICAL, ArchiveEvidenceAuthority.NAVIGATION_ONLY}
    ),
    ArchiveReviewState.NOT_APPLICABLE: frozenset(ArchiveEvidenceAuthority),
}
_REVIEW_LIFECYCLE_STATES = {
    ArchiveReviewState.CONFIRMED: frozenset({LifecycleState.ACTIVE, LifecycleState.CLASSIFIED}),
    ArchiveReviewState.PENDING: frozenset({LifecycleState.PENDING}),
    ArchiveReviewState.ARCHIVED: frozenset({LifecycleState.ARCHIVED, LifecycleState.DEPRECATED}),
    ArchiveReviewState.NOT_APPLICABLE: frozenset(LifecycleState),
}
_FACTUAL_REPRESENTATION_STATES = frozenset({LifecycleState.ACTIVE, LifecycleState.ARCHIVED})
_INVALID_CANONICAL_STATES = frozenset(
    {
        LifecycleState.DELETED,
        LifecycleState.TOMBSTONED,
        LifecycleState.UNAVAILABLE,
        LifecycleState.IGNORED,
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveSearchCandidate:
    corpus: ArchiveSearchCorpus
    resolved_source: ResolvedSource
    title: str | None
    filename: str | None
    review_state: ArchiveReviewState
    evidence_authority: ArchiveEvidenceAuthority
    lifecycle_state: LifecycleState
    matches: tuple[ArchiveMatchRank, ...]
    temporal_facts: tuple[TemporalFact, ...]
    passages: tuple[ArchiveSearchPassage, ...]

    def __post_init__(self) -> None:
        if any(type(value) is not tuple for value in (self.matches, self.temporal_facts, self.passages)):
            raise RetrievalContractError("archive candidate evidence must use exact tuples")
        if (
            not isinstance(self.corpus, ArchiveSearchCorpus)
            or type(self.resolved_source) is not ResolvedSource
        ):
            raise RetrievalContractError("archive candidate requires typed corpus and source")
        source = self.resolved_source
        if (
            type(source.source_ref) is not SourceRef
            or any(type(item) is not SourceRepresentation for item in source.representations)
            or any(
                type(item) is not LifecycleRef or type(item.representation) is not SourceRepresentation
                for item in source.lifecycle
            )
            or any(
                type(item) is not SourceRevision or type(item.representation) is not SourceRepresentation
                for item in source.revisions
            )
            or any(
                type(item) is not RevalidationTarget or type(item.representation) is not SourceRepresentation
                for item in source.revalidation_targets
            )
        ):
            raise RetrievalContractError("archive candidate source graph must use exact contract types")
        if self.resolved_source.source_ref.source_kind not in _SOURCE_KINDS[self.corpus]:
            raise RetrievalContractError("candidate corpus and stable source kind disagree")
        _text(self.title, "candidate title", MAX_DISPLAY_CHARS, optional=True)
        _text(self.filename, "candidate filename", MAX_DISPLAY_CHARS, optional=True)
        if not isinstance(self.review_state, ArchiveReviewState) or not isinstance(
            self.evidence_authority, ArchiveEvidenceAuthority
        ):
            raise RetrievalContractError("candidate review and evidence authority must be closed")
        if (
            self.review_state not in _CORPUS_REVIEW_STATES[self.corpus]
            or self.evidence_authority not in _CORPUS_EVIDENCE_AUTHORITIES[self.corpus]
            or self.evidence_authority not in _REVIEW_EVIDENCE_AUTHORITIES[self.review_state]
        ):
            raise RetrievalContractError("candidate corpus, review and evidence authority disagree")
        if not isinstance(self.lifecycle_state, LifecycleState):
            raise RetrievalContractError("candidate lifecycle state must be closed")
        if any(item.state is LifecycleState.IGNORED for item in self.resolved_source.lifecycle):
            raise RetrievalContractError("ignored source lifecycle is not archive-discoverable")
        relevant_lifecycle: tuple[LifecycleRef, ...] = ()
        for representation_kind in _CORPUS_LIFECYCLE_PRIORITY[self.corpus]:
            relevant_lifecycle = tuple(
                item
                for item in self.resolved_source.lifecycle
                if item.representation.kind is representation_kind
            )
            if relevant_lifecycle:
                break
        if not relevant_lifecycle or relevant_lifecycle[0].state is not self.lifecycle_state:
            raise RetrievalContractError("candidate lifecycle is absent from its relevant source snapshot")
        if self.lifecycle_state not in _REVIEW_LIFECYCLE_STATES[self.review_state]:
            raise RetrievalContractError("candidate review and lifecycle disagree")
        if self.review_state is ArchiveReviewState.PENDING and not any(
            item.state is LifecycleState.PENDING for item in relevant_lifecycle
        ):
            raise RetrievalContractError("pending review requires a real pending lifecycle")
        pending_inbox = any(
            item.representation.kind is RepresentationKind.INBOX_ITEM and item.state is LifecycleState.PENDING
            for item in self.resolved_source.lifecycle
        )
        if pending_inbox and self.evidence_authority is ArchiveEvidenceAuthority.CANONICAL:
            raise RetrievalContractError("pending source cannot claim canonical evidence authority")
        if self.corpus is ArchiveSearchCorpus.DOCUMENTS and self.review_state is ArchiveReviewState.CONFIRMED:
            has_classified_inbox = any(
                item.representation.kind is RepresentationKind.INBOX_ITEM
                and item.state is LifecycleState.CLASSIFIED
                for item in self.resolved_source.lifecycle
            )
            has_current_knowledge = any(
                item.representation.kind is RepresentationKind.KNOWLEDGE_OBJECT
                and item.state in {LifecycleState.ACTIVE, LifecycleState.ARCHIVED}
                for item in self.resolved_source.lifecycle
            )
            if not has_classified_inbox and not has_current_knowledge:
                raise RetrievalContractError("confirmed document requires authoritative review lifecycle")
        if not self.matches or self.matches != _match_ranks(self.matches):
            raise RetrievalContractError("candidate match channels must be canonical and non-empty")
        if self.temporal_facts != _facts(self.temporal_facts) or self.passages != _passages(self.passages):
            raise RetrievalContractError("candidate evidence must be canonical")
        if self.evidence_authority is ArchiveEvidenceAuthority.NAVIGATION_ONLY:
            if self.passages:
                raise RetrievalContractError("navigation-only candidates must have zero passages")
        elif not self.passages:
            raise RetrievalContractError("canonical and noncanonical evidence require exact passages")
        if not self.navigation_only and any(
            item.state in _INVALID_CANONICAL_STATES for item in self.resolved_source.lifecycle
        ):
            raise RetrievalContractError("factual evidence has an invalid source lifecycle")
        if (
            self.review_state is ArchiveReviewState.PENDING
            and self.evidence_authority is ArchiveEvidenceAuthority.CANONICAL
        ):
            raise RetrievalContractError("pending material cannot claim canonical evidence authority")
        source = self.resolved_source
        if any(
            item.passage_ref.source_ref != source.source_ref or not item.passage_ref.revision_matches(source)
            for item in self.passages
        ):
            raise RetrievalContractError("candidate passages must match the exact source snapshot")
        lifecycle_by_representation = {
            item.representation: item.state for item in self.resolved_source.lifecycle
        }
        if not self.navigation_only and any(
            lifecycle_by_representation.get(item.passage_ref.source_revision.representation)
            not in _FACTUAL_REPRESENTATION_STATES
            for item in self.passages
        ):
            raise RetrievalContractError("factual evidence requires a current allowed representation")
        if any(
            type(item.source_revision) is not SourceRevision
            or type(item.source_revision.representation) is not SourceRepresentation
            or item.source_revision not in source.revisions
            for item in self.temporal_facts
        ):
            raise RetrievalContractError("candidate temporal facts must match the exact source snapshot")

    def __repr__(self) -> str:
        return f"ArchiveSearchCandidate(private_source=True, passage_count={len(self.passages)})"

    @property
    def navigation_only(self) -> bool:
        return self.evidence_authority is ArchiveEvidenceAuthority.NAVIGATION_ONLY

    @property
    def match_channels(self) -> tuple[ArchiveMatchChannel, ...]:
        return tuple(item.channel for item in self.matches)

    @classmethod
    def create(
        cls,
        *,
        corpus: ArchiveSearchCorpus,
        resolved_source: ResolvedSource,
        review_state: ArchiveReviewState,
        evidence_authority: ArchiveEvidenceAuthority,
        lifecycle_state: LifecycleState,
        matches: Iterable[ArchiveMatchRank],
        title: str | None = None,
        filename: str | None = None,
        temporal_facts: Iterable[TemporalFact] = (),
        passages: Iterable[ArchiveSearchPassage] = (),
    ) -> ArchiveSearchCandidate:
        return cls(
            corpus,
            resolved_source,
            cast(str | None, _text(title, "candidate title", MAX_DISPLAY_CHARS, optional=True)),
            cast(str | None, _text(filename, "candidate filename", MAX_DISPLAY_CHARS, optional=True)),
            review_state,
            evidence_authority,
            lifecycle_state,
            _match_ranks(matches),
            _facts(temporal_facts),
            _passages(passages),
        )

    def to_private_payload(self) -> dict[str, object]:
        multiline = _candidate_uses_multiline_passage(self)
        return {
            "corpus": self.corpus.value,
            "evidence_authority": self.evidence_authority.value,
            "filename": self.filename,
            "lifecycle_state": self.lifecycle_state.value,
            "matches": [item.to_private_payload() for item in self.matches],
            "passages": [
                _passage_to_private_payload(item, allow_multiline=multiline) for item in self.passages
            ],
            "resolved_source": self.resolved_source.to_private_payload(),
            "review_state": self.review_state.value,
            "schema": (_ARCHIVE_SEARCH_CANDIDATE_SCHEMA_V2 if multiline else ARCHIVE_SEARCH_CANDIDATE_SCHEMA),
            "temporal_facts": [item.to_private_payload() for item in self.temporal_facts],
            "title": self.title,
        }

    def to_private_json(self) -> str:
        return _json(self.to_private_payload())

    @classmethod
    def from_private_payload(cls, value: object) -> ArchiveSearchCandidate:
        payload = _exact(value, _CANDIDATE_KEYS, "archive candidate")
        schema = payload["schema"]
        if schema not in {ARCHIVE_SEARCH_CANDIDATE_SCHEMA, _ARCHIVE_SEARCH_CANDIDATE_SCHEMA_V2}:
            raise RetrievalContractError("archive candidate schema is unsupported")
        multiline = schema == _ARCHIVE_SEARCH_CANDIDATE_SCHEMA_V2
        title = _text(payload["title"], "candidate title", MAX_DISPLAY_CHARS, optional=True)
        filename = _text(payload["filename"], "candidate filename", MAX_DISPLAY_CHARS, optional=True)
        matches, facts, passages = (
            payload["matches"],
            payload["temporal_facts"],
            payload["passages"],
        )
        if any(type(item) is not list for item in (matches, facts, passages)):
            raise RetrievalContractError("archive candidate evidence collections must be arrays")
        candidate = cls.create(
            corpus=_enum(ArchiveSearchCorpus, payload["corpus"], "candidate corpus"),
            resolved_source=ResolvedSource.from_private_payload(payload["resolved_source"]),
            title=cast(str | None, title),
            filename=cast(str | None, filename),
            review_state=_enum(ArchiveReviewState, payload["review_state"], "review state"),
            evidence_authority=_enum(
                ArchiveEvidenceAuthority,
                payload["evidence_authority"],
                "evidence authority",
            ),
            lifecycle_state=_enum(LifecycleState, payload["lifecycle_state"], "lifecycle state"),
            matches=(ArchiveMatchRank.from_private_payload(item) for item in matches),
            temporal_facts=(TemporalFact.from_private_payload(item) for item in facts),
            passages=(_passage_from_private_payload(item, allow_multiline=multiline) for item in passages),
        )
        if _candidate_uses_multiline_passage(candidate) is not multiline:
            raise RetrievalContractError("archive candidate schema is not semantically canonical")
        return candidate

    @classmethod
    def parse_private(cls, value: str) -> ArchiveSearchCandidate:
        result = cls.from_private_payload(
            _json_object(
                value,
                label="archive candidate",
                maximum_bytes=_MAX_PRIVATE_CANDIDATE_BYTES,
            )
        )
        if value != result.to_private_json():
            raise RetrievalContractError("archive candidate JSON is not semantically canonical")
        return result


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveSearchResult:
    ordinal: int
    candidate: ArchiveSearchCandidate

    def __post_init__(self) -> None:
        _integer(self.ordinal, "archive result ordinal", 1, 20)
        if type(self.candidate) is not ArchiveSearchCandidate:
            raise RetrievalContractError("archive result requires a typed candidate")

    def __repr__(self) -> str:
        return f"ArchiveSearchResult(ordinal={self.ordinal}, private_candidate=True)"


def _warnings(values: Iterable[ArchiveSearchWarning]) -> tuple[ArchiveSearchWarning, ...]:
    return _enum_tuple(values, ArchiveSearchWarning, "archive warnings", empty=True)


def _coverages(values: Iterable[SearchCoverage]) -> tuple[SearchCoverage, ...]:
    items = tuple(values)
    if not items or any(type(item) is not SearchCoverage for item in items):
        raise RetrievalContractError("archive page coverage must use the typed contract")
    return tuple(sorted(items, key=lambda item: (item.corpus.value, item.lane.value)))


def _safe_fact(item: TemporalFact) -> dict[str, object]:
    return {
        "end": item.end,
        "origin": item.origin.value,
        "precision": item.precision.value,
        "role": item.role.value,
        "start": item.start,
        "value_kind": item.value_kind.value,
    }


@dataclass(frozen=True, slots=True, repr=False)
class ArchiveSearchPage:
    request: ArchiveSearchRequest
    results: tuple[ArchiveSearchResult, ...]
    coverage: tuple[SearchCoverage, ...]
    warnings: tuple[ArchiveSearchWarning, ...]
    continuation: str | None

    def __post_init__(self) -> None:
        if type(self.request) is not ArchiveSearchRequest:
            raise RetrievalContractError("archive page requires its canonical private request")
        if any(type(value) is not tuple for value in (self.results, self.coverage, self.warnings)):
            raise RetrievalContractError("archive page collections must use exact tuples")
        expected = tuple(range(1, len(self.results) + 1))
        if (
            type(self.results) is not tuple
            or len(self.results) > self.request.limit
            or any(type(item) is not ArchiveSearchResult for item in self.results)
            or tuple(item.ordinal for item in self.results) != expected
        ):
            raise RetrievalContractError("archive page results must have consecutive canonical ordinals")
        sources = tuple(item.candidate.resolved_source.source_ref for item in self.results)
        if len(sources) != len(set(sources)):
            raise RetrievalContractError("archive page must deduplicate stable sources")
        if any(item.candidate.corpus not in self.request.corpora for item in self.results):
            raise RetrievalContractError("archive result corpus was not requested")
        if self.coverage != _coverages(self.coverage):
            raise RetrievalContractError("archive page coverage must be canonical")
        targets = tuple((item.corpus, item.lane) for item in self.coverage)
        if len(targets) != len(set(targets)) or len({item.execution_binding for item in self.coverage}) != 1:
            raise RetrievalContractError("archive page coverage targets or binding are inconsistent")
        binding = self.coverage[0].execution_binding
        if any(
            not item.execution_binding.attests_private_request(self.request.to_identity_json())
            for item in self.coverage
        ):
            raise RetrievalContractError("archive page binding does not attest its private request")
        if set(targets) != set(binding.requested_targets):
            raise RetrievalContractError("archive page coverage must include every bound target")
        if {item.corpus for item in self.coverage} != {_SEARCH_CORPUS[item] for item in self.request.corpora}:
            raise RetrievalContractError("archive page coverage must represent every requested corpus")
        coverage_by_target = {(item.corpus, item.lane): item for item in self.coverage}
        ranked_targets: set[tuple[SearchCorpus, SearchLane, int]] = set()
        for result in self.results:
            for match in result.candidate.matches:
                target = (_SEARCH_CORPUS[result.candidate.corpus], match.channel.search_lane)
                target_coverage = coverage_by_target.get(target)
                ranked = (*target, match.rank)
                if (
                    target_coverage is None
                    or match.rank > target_coverage.matched_at_least
                    or ranked in ranked_targets
                ):
                    raise RetrievalContractError("candidate lane rank is not attested by bound coverage")
                ranked_targets.add(ranked)
        constraints = {item.corpus: item.states for item in self.request.lifecycle_constraints}
        if any(
            result.candidate.corpus in constraints
            and result.candidate.lifecycle_state not in constraints[result.candidate.corpus]
            for result in self.results
        ):
            raise RetrievalContractError("candidate lifecycle is outside the requested constraint")
        if self.request.review_scope is ReviewScope.CONFIRMED_ONLY and any(
            result.candidate.evidence_authority is ArchiveEvidenceAuthority.NONCANONICAL
            or (
                result.candidate.corpus in {ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE}
                and result.candidate.review_state is not ArchiveReviewState.CONFIRMED
            )
            for result in self.results
        ):
            raise RetrievalContractError("confirmed-only search cannot publish unconfirmed evidence")
        if self.warnings != _warnings(self.warnings):
            raise RetrievalContractError("archive page warnings must be canonical")
        _token(self.continuation)
        if (self.continuation is not None) != any(item.next_cursor_available for item in self.coverage):
            raise RetrievalContractError("archive page continuation and lane coverage disagree")
        if self.continuation is not None and self.continuation == self.request.continuation:
            raise RetrievalContractError("outbound continuation must differ from the inbound token")

    def __repr__(self) -> str:
        return f"ArchiveSearchPage(private_request=True, result_count={len(self.results)})"

    @classmethod
    def create(
        cls,
        *,
        request: ArchiveSearchRequest,
        candidates: Iterable[ArchiveSearchCandidate],
        coverage: Iterable[SearchCoverage],
        warnings: Iterable[ArchiveSearchWarning] = (),
        continuation: str | None = None,
    ) -> ArchiveSearchPage:
        candidates = tuple(candidates)
        if any(type(item) is not ArchiveSearchCandidate for item in candidates):
            raise RetrievalContractError("archive page candidates must use the typed contract")
        return cls(
            request,
            tuple(ArchiveSearchResult(index, item) for index, item in enumerate(candidates, 1)),
            _coverages(coverage),
            _warnings(warnings),
            _token(continuation),
        )

    @property
    def absence_decision(self) -> AbsenceDecision:
        decision = aggregate_absence_decision(
            self.coverage,
            requested_targets=self.coverage[0].execution_binding.requested_targets,
        )
        if decision is not AbsenceDecision.NOT_ESTABLISHED:
            return decision

        # MESSAGE_HISTORY is the released complete conversation corpus reader.
        # The productive lexical lane is deliberately capped/partial and DENSE
        # remains unavailable, so requiring all three equivalent lanes to be
        # complete would make an authenticated zero-hit history incapable of
        # proving absence.  Keep unrelated corpora under the generic all-lanes
        # rule and never substitute a partial, stale or denied history lane.
        history = next(
            (
                item
                for item in self.coverage
                if item.corpus is SearchCorpus.CONVERSATION and item.lane is SearchLane.MESSAGE_HISTORY
            ),
            None,
        )
        if (
            history is not None
            and history.absence_decision() is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
            and all(
                (item.corpus is SearchCorpus.CONVERSATION and item.matched_at_least == 0)
                or item.absence_decision() is AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
                for item in self.coverage
            )
        ):
            return AbsenceDecision.AUTHORIZED_ABSENCE_CONFIRMED
        return decision

    @property
    def exhaustive(self) -> bool:
        return self.continuation is None and all(
            item.states == (CoverageState.COMPLETE,)
            and item.authority_rechecked
            and item.snapshot_current
            and not item.next_cursor_available
            for item in self.coverage
        )

    def to_public_payload(self, privacy_key: bytes) -> dict[str, object]:
        candidates: list[dict[str, object]] = []
        for result in self.results:
            candidate = result.candidate
            candidates.append(
                {
                    "filename": candidate.filename,
                    "corpus": candidate.corpus.value,
                    "evidence_authority": candidate.evidence_authority.value,
                    "label": f"A{result.ordinal}",
                    "lifecycle_state": candidate.lifecycle_state.value,
                    "match_channels": [item.value for item in candidate.match_channels],
                    "matches": [item.to_private_payload() for item in candidate.matches],
                    "navigation_only": candidate.navigation_only,
                    "passages": [
                        {
                            "excerpt": passage.excerpt,
                            "label": f"A{result.ordinal}.{index}",
                            "passage_handle": passage.passage_ref.passage_digest(privacy_key),
                        }
                        for index, passage in enumerate(candidate.passages, 1)
                    ],
                    "review_state": candidate.review_state.value,
                    "source_handle": candidate.resolved_source.logical_digest(privacy_key),
                    "source_kind": candidate.resolved_source.source_ref.source_kind.value,
                    "temporal_facts": [_safe_fact(item) for item in candidate.temporal_facts],
                    "title": candidate.title,
                }
            )
        coverage: list[dict[str, object]] = []
        for item in self.coverage:
            projected = item.to_payload()
            del projected["execution_binding"]
            coverage.append(projected)
        payload: dict[str, object] = {
            "absence": self.absence_decision.value,
            "candidates": candidates,
            "continuation": self.continuation,
            "coverage": coverage,
            "execution_binding": self.coverage[0].execution_binding.to_payload(),
            "exhaustive": self.exhaustive,
            "schema": (
                _ARCHIVE_SEARCH_PUBLIC_PAGE_SCHEMA_V2
                if any(_candidate_uses_multiline_passage(item.candidate) for item in self.results)
                else ARCHIVE_SEARCH_PUBLIC_PAGE_SCHEMA
            ),
            "warnings": [item.value for item in self.warnings],
        }
        if (
            len(json.dumps(payload, ensure_ascii=False)) > 7_900
            or len(json.dumps(payload, ensure_ascii=False, indent=2)) > 11_900
            or len(_json(payload)) > 7_900
        ):
            raise RetrievalContractError("archive public page exceeds the real ToolResult envelope")
        return payload

    def to_public_json(self, privacy_key: bytes) -> str:
        return _json(self.to_public_payload(privacy_key))


ArchiveSearchPrivateRequest = ArchiveSearchRequest
ArchiveSearchPrivateCandidate = ArchiveSearchCandidate
ArchiveSearchPrivateResult = ArchiveSearchResult
ArchiveSearchPrivatePage = ArchiveSearchPage

__all__ = [
    "ArchiveContextWindow",
    "ArchiveEvidenceAuthority",
    "ArchiveLifecycleConstraint",
    "ArchiveMatchChannel",
    "ArchiveMatchRank",
    "ArchiveReviewState",
    "ArchiveSearchCandidate",
    "ArchiveSearchCorpus",
    "ArchiveSearchPage",
    "ArchiveSearchPassage",
    "ArchiveSearchRequest",
    "ArchiveSearchResult",
    "ArchiveSearchWarning",
    "ArchiveTemporalConstraint",
    "ConversationScope",
    "ReviewScope",
]
