"""Closed contracts for authenticated exact memory retrieval.

The durable request binds an authenticated tenant, principal and active turn,
but its durable identity carries only a SHA-256 of the normalized query.  Raw
queries exist only in the private request serialization.  Storage-selected
candidates retain exact revision bindings and a bounded query-aware excerpt;
they never retain a full knowledge body.  Candidate, page and late-publication
values are process-private sealed carriers.  Only ``MemoryExactProjection`` and
its bounded graph projection are suitable for model input.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import unicodedata
from calendar import monthrange
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, NoReturn, SupportsIndex, cast

MEMORY_EXACT_REQUEST_SCHEMA = "friday.memory-exact-request.private.v1"
MEMORY_EXACT_REQUEST_IDENTITY_SCHEMA = "friday.memory-exact-request-identity.private.v1"
MEMORY_EXACT_MODEL_PROJECTION_SCHEMA = "friday.memory-exact-projection.model.v1"
MEMORY_EXACT_GRAPH_PROJECTION_SCHEMA = "friday.memory-exact-graph-projection.model.v1"
MEMORY_EXACT_PUBLICATION_DECISION_SCHEMA = "friday.memory-exact-publication-decision.v1"

MEMORY_EXACT_DEFAULT_PAGE_SIZE = 10
MEMORY_EXACT_MAX_PAGE_SIZE = 20
MEMORY_EXACT_DEFAULT_SNAPSHOT_LIMIT = 50
MEMORY_EXACT_MAX_SNAPSHOT_LIMIT = 50
MEMORY_EXACT_MAX_QUERY_CHARS = 700
MEMORY_EXACT_MAX_EXCERPT_CHARS = 600
MEMORY_EXACT_MAX_GRAPH_NODES = 12
MEMORY_EXACT_MAX_GRAPH_RELATIONS = 20
MEMORY_EXACT_MAX_GRAPH_PATHS = 6
MEMORY_EXACT_MAX_GRAPH_PATH_EDGES = 4

# Compatibility spellings for callers which describe the bounded selection as
# a top snapshot rather than a snapshot limit.
MEMORY_EXACT_DEFAULT_TOP_SNAPSHOT = MEMORY_EXACT_DEFAULT_SNAPSHOT_LIMIT
MEMORY_EXACT_MAX_TOP_SNAPSHOT = MEMORY_EXACT_MAX_SNAPSHOT_LIMIT

_MAX_REQUEST_JSON_BYTES = 16_384
_MAX_CONTINUATION_BYTES = 4_096
_MIN_CONTINUATION_BYTES = 32
_MAX_SCOPE_BYTES = 240
_MAX_STORED_BODY_BYTES = 4_000_000
_MAX_REVISION_JSON_BYTES = 4_500_000
_MAX_GRAPH_JSON_BYTES = 80_000
_MAX_MODEL_JSON_BYTES = 160_000
_MAX_COUNT = 1_000_000_000
_MAX_MODEL_TITLE_CHARS = 200
_MAX_NAME_CHARS = 240
_MAX_KIND_CHARS = 80
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TURN_ID = re.compile(r"turn_[0-9a-f]{64}\Z")
_OPAQUE_TOKEN = re.compile(r"[A-Za-z0-9_-]+\Z")
_LEGACY_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\Z")
_LEGACY_DMY_DATE = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{4})\Z")
_LEGACY_YEAR_MONTH = re.compile(r"(\d{4})[-./](\d{1,2})\Z")
_LEGACY_MONTH_YEAR = re.compile(r"(\d{1,2})[-./](\d{4})\Z")
_LEGACY_YEAR = re.compile(r"\d{4}\Z")
_KNOWN_AT_INPUT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")
_CARRIER_FACTORY = object()
_DECISION_FACTORY = object()
_PROCESS_KEY = secrets.token_bytes(32)
_PUBLICATION_LOCK = threading.RLock()


class MemoryExactContractError(ValueError):
    """A value is outside the closed exact-memory contract."""


class MemoryExactLifecycleStage(StrEnum):
    """Searchable promoted-knowledge lifecycle stages."""

    ACTIVE = "active"
    ARCHIVED = "archived"
    DEPRECATED = "deprecated"


class MemoryExactTemporalBasis(StrEnum):
    VALID_TIME = "valid_time"
    BITEMPORAL = "bitemporal"


class MemoryExactGraphDirection(StrEnum):
    FORWARD = "forward"
    REVERSE = "reverse"


class MemoryExactGraphEvidenceBasis(StrEnum):
    """Closed, ID-free authority basis for one graph assertion."""

    RELATION_ROW_ONLY = "relation_row_only"
    REVIEWED_RELATION = "reviewed_relation"
    ACCEPTED_LINKS = "accepted_links"


class MemoryExactRowCoverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


class MemoryExactContentCoverage(StrEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"


class MemoryExactGraphCoverage(StrEnum):
    """Whether one bounded graph collection is complete, partial or unknowable."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class MemoryExactPublicationStatus(StrEnum):
    AUTHORIZED = "authorized"
    DENIED = "denied"
    DRIFTED = "drifted"
    UNAVAILABLE = "unavailable"


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (MemoryError, OverflowError, RecursionError, TypeError, UnicodeError, ValueError):
        raise MemoryExactContractError("exact-memory value is not canonical JSON") from None


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MemoryExactContractError("exact-memory JSON contains a duplicate key")
        result[key] = value
    return result


def _finite_json_float(value: str) -> float:
    try:
        parsed = float(value)
    except (OverflowError, ValueError):
        raise MemoryExactContractError("exact-memory JSON contains an invalid number") from None
    if not math.isfinite(parsed):
        raise MemoryExactContractError("exact-memory JSON contains a non-finite number")
    return parsed


def _parse_canonical_object(value: object, *, label: str) -> dict[str, Any]:
    if type(value) is not str or not value or value != value.strip():
        raise MemoryExactContractError(f"{label} must be canonical JSON text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise MemoryExactContractError(f"{label} must be valid UTF-8") from None
    if len(encoded) > _MAX_REQUEST_JSON_BYTES:
        raise MemoryExactContractError(f"{label} exceeds the closed byte limit")
    try:
        parsed = json.loads(
            value,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                MemoryExactContractError(f"{label} contains a non-finite number")
            ),
            parse_float=_finite_json_float,
            object_pairs_hook=_closed_object,
        )
    except MemoryExactContractError:
        raise
    except (MemoryError, OverflowError, RecursionError, UnicodeError, ValueError):
        raise MemoryExactContractError(f"{label} must contain one JSON object") from None
    if type(parsed) is not dict or value != _canonical_json(parsed):
        raise MemoryExactContractError(f"{label} must be closed canonical JSON")
    return cast(dict[str, Any], parsed)


def _exact_object(value: object, keys: frozenset[str], *, label: str) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != keys:
        raise MemoryExactContractError(f"{label} keys do not match the closed contract")
    return cast(dict[str, Any], value)


def _valid_utf8(
    value: object,
    *,
    label: str,
    maximum_bytes: int,
    allow_empty: bool,
) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise MemoryExactContractError(f"{label} must be text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise MemoryExactContractError(f"{label} must be valid UTF-8") from None
    if len(encoded) > maximum_bytes:
        raise MemoryExactContractError(f"{label} exceeds the closed byte limit")
    return value


def _scope(value: object, *, label: str) -> str:
    text = _valid_utf8(value, label=label, maximum_bytes=_MAX_SCOPE_BYTES, allow_empty=False)
    if text != text.strip() or any(unicodedata.category(char).startswith("C") for char in text):
        raise MemoryExactContractError(f"{label} is not canonical")
    return text


def _display_text(
    value: object,
    *,
    label: str,
    maximum_chars: int,
    allow_empty: bool,
) -> str:
    text = _valid_utf8(
        value,
        label=label,
        maximum_bytes=maximum_chars * 4,
        allow_empty=allow_empty,
    )
    if len(text) > maximum_chars or any(unicodedata.category(char).startswith("C") for char in text):
        raise MemoryExactContractError(f"{label} is outside the closed text envelope")
    if text and text != text.strip():
        raise MemoryExactContractError(f"{label} is not canonical")
    return text


def _title_text(value: object, *, label: str, maximum_chars: int) -> str:
    text = _valid_utf8(
        value,
        label=label,
        maximum_bytes=maximum_chars * 4,
        allow_empty=True,
    )
    if len(text) > maximum_chars or any(
        unicodedata.category(char).startswith("C") and char not in {"\t", "\n", "\r"} for char in text
    ):
        raise MemoryExactContractError(f"{label} is outside the closed text envelope")
    return text


def _project_title(value: object) -> str:
    source = _valid_utf8(
        value,
        label="candidate source title",
        maximum_bytes=1_000_000,
        allow_empty=True,
    )
    projected = (source or "Без названия")[:_MAX_MODEL_TITLE_CHARS]
    return _title_text(
        projected,
        label="candidate title",
        maximum_chars=_MAX_MODEL_TITLE_CHARS,
    )


def _sha256(value: object, *, label: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise MemoryExactContractError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _active_turn_id(value: object) -> str:
    text = _scope(value, label="memory active-turn identity")
    if _TURN_ID.fullmatch(text) is None:
        raise MemoryExactContractError("memory active-turn identity is invalid")
    return text


def _count(value: object, *, label: str, low: int = 0, high: int = _MAX_COUNT) -> int:
    if isinstance(value, bool) or type(value) is not int or not low <= value <= high:
        raise MemoryExactContractError(f"{label} is outside the closed range")
    return value


def _enum(enum_type: type[StrEnum], value: object, *, label: str) -> StrEnum:
    if type(value) is not str or len(value) > 80:
        raise MemoryExactContractError(f"{label} is outside the closed enum")
    try:
        return enum_type(value)
    except ValueError:
        raise MemoryExactContractError(f"{label} is outside the closed enum") from None


def _query(value: object, *, normalize: bool) -> str:
    if type(value) is not str:
        raise MemoryExactContractError("memory query must be text")
    text = " ".join(value.split()) if normalize else value
    if not normalize and text != " ".join(text.split()):
        raise MemoryExactContractError("memory query must already be normalized")
    _valid_utf8(
        text,
        label="memory query",
        maximum_bytes=MEMORY_EXACT_MAX_QUERY_CHARS * 4,
        allow_empty=False,
    )
    if len(text) > MEMORY_EXACT_MAX_QUERY_CHARS or any(
        unicodedata.category(char).startswith("C") for char in text
    ):
        raise MemoryExactContractError("memory query is outside the closed character limit")
    return text


def _query_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _normalized_date(value: object, *, label: str) -> str:
    if type(value) is not str or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise MemoryExactContractError(f"{label} must be a normalized calendar date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise MemoryExactContractError(f"{label} must be a valid calendar date") from None
    if parsed.isoformat() != value:
        raise MemoryExactContractError(f"{label} must already be normalized")
    return value


def _request_normalized_date(value: object, *, label: str) -> str:
    normalized = _normalized_date(value, label=label)
    year = int(normalized[:4])
    if not 1900 <= year <= 2200:
        raise MemoryExactContractError(f"{label} is outside the supported year range")
    return normalized


def _legacy_exact_date_input(value: date | str, *, label: str) -> str:
    """Mirror storage ``iso_date`` for exact model-supplied document dates."""

    if isinstance(value, datetime):
        raise MemoryExactContractError(f"{label} must be a calendar date, not an instant")
    if type(value) is date:
        return _request_normalized_date(value.isoformat(), label=label)
    if type(value) is not str:
        raise MemoryExactContractError(f"{label} must be a calendar date")
    text = " ".join(value.split())
    match = _LEGACY_ISO_DATE.fullmatch(text)
    if match is not None:
        year, month, day = (int(part) for part in match.groups())
    else:
        match = _LEGACY_DMY_DATE.fullmatch(text)
        if match is None:
            raise MemoryExactContractError(f"{label} must be an exact calendar date")
        day, month, year = (int(part) for part in match.groups())
    if not 1900 <= year <= 2200:
        raise MemoryExactContractError(f"{label} is outside the supported year range")
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        raise MemoryExactContractError(f"{label} must be a valid calendar date") from None


def _window_date_input(
    value: date | str | None,
    *,
    label: str,
    edge: str,
) -> str | None:
    """Mirror legacy ``_window_bound`` including edge-specific partial dates."""

    if value is None or value == "":
        return None
    if type(value) is str and not " ".join(value.split()):
        return None
    if edge not in {"since", "until"}:
        raise MemoryExactContractError("memory window edge is invalid")
    try:
        return _legacy_exact_date_input(value, label=label)
    except MemoryExactContractError:
        if type(value) is not str:
            raise
    text = " ".join(cast(str, value).split())
    year_month = _LEGACY_YEAR_MONTH.fullmatch(text)
    month_year = _LEGACY_MONTH_YEAR.fullmatch(text)
    if year_month is not None:
        year, month = (int(part) for part in year_month.groups())
    elif month_year is not None:
        month, year = (int(part) for part in month_year.groups())
    else:
        year = month = 0
    if 1900 <= year <= 2200 and 1 <= month <= 12:
        day = 1 if edge == "since" else monthrange(year, month)[1]
        return date(year, month, day).isoformat()
    if _LEGACY_YEAR.fullmatch(text) is not None and 1900 <= int(text) <= 2200:
        suffix = "01-01" if edge == "since" else "12-31"
        return f"{text}-{suffix}"
    raise MemoryExactContractError(f"{label} must be a valid inclusive calendar bound")


def _as_of_date_input(value: date | str | None, *, label: str) -> str | None:
    if value is None or value == "":
        return None
    if type(value) is str and not " ".join(value.split()):
        return None
    return _legacy_exact_date_input(value, label=label)


def _instant(value: object, *, label: str) -> str:
    text = _valid_utf8(value, label=label, maximum_bytes=64, allow_empty=False)
    if text != text.strip():
        raise MemoryExactContractError(f"{label} is not canonical")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise MemoryExactContractError(f"{label} must be an ISO-8601 instant") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryExactContractError(f"{label} must include an offset")
    canonical = parsed.astimezone(UTC).isoformat()
    if text != canonical:
        raise MemoryExactContractError(f"{label} must already be normalized to UTC")
    return canonical


def _instant_input(value: datetime | str, *, label: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise MemoryExactContractError(f"{label} must include an offset")
        return value.astimezone(UTC).isoformat()
    return _instant(value, label=label)


def _known_at_canonical(
    value: object,
    *,
    label: str,
    reject_future: bool,
) -> str:
    if type(value) is not str or _KNOWN_AT_INPUT.fullmatch(value) is None:
        raise MemoryExactContractError(f"{label} must be an offset-aware RFC3339 instant")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MemoryExactContractError(f"{label} must be an offset-aware RFC3339 instant") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryExactContractError(f"{label} must include an offset")
    normalized = parsed.astimezone(UTC)
    canonical = normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if value != canonical:
        raise MemoryExactContractError(f"{label} must already be canonical UTC")
    if reject_future and normalized > datetime.now(UTC):
        raise MemoryExactContractError(f"{label} cannot be in the future")
    return canonical


def _known_at_input(value: datetime | str | None, *, label: str) -> str | None:
    if value is None or value == "":
        return None
    if type(value) is str and not value.strip():
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise MemoryExactContractError(f"{label} must include an offset")
        normalized = value.astimezone(UTC)
        if normalized > datetime.now(UTC):
            raise MemoryExactContractError(f"{label} cannot be in the future")
        return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if type(value) is not str:
        raise MemoryExactContractError(f"{label} must be an offset-aware RFC3339 instant")
    text = value.strip()
    if _KNOWN_AT_INPUT.fullmatch(text) is None:
        raise MemoryExactContractError(f"{label} must be an offset-aware RFC3339 instant")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise MemoryExactContractError(f"{label} must be an offset-aware RFC3339 instant") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryExactContractError(f"{label} must include an offset")
    normalized = parsed.astimezone(UTC)
    if normalized > datetime.now(UTC):
        raise MemoryExactContractError(f"{label} cannot be in the future")
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _lifecycles(values: Iterable[MemoryExactLifecycleStage]) -> tuple[MemoryExactLifecycleStage, ...]:
    try:
        result = tuple(values)
    except TypeError:
        raise MemoryExactContractError("memory lifecycle stages are outside the closed contract") from None
    if not result or any(type(item) is not MemoryExactLifecycleStage for item in result):
        raise MemoryExactContractError("memory lifecycle stages are outside the closed contract")
    canonical = tuple(sorted(result, key=lambda item: item.value))
    if len(canonical) != len(set(canonical)):
        raise MemoryExactContractError("memory lifecycle stages must be unique")
    return canonical


def _keyed_handle(domain: bytes, payload: Mapping[str, Any]) -> str:
    material = domain + b"\0" + _canonical_json(payload).encode("ascii")
    return hmac.new(_PROCESS_KEY, material, hashlib.sha256).hexdigest()


def _digest_payload(domain: bytes, payload: object) -> str:
    return hashlib.sha256(domain + b"\0" + _canonical_json(payload).encode("ascii")).hexdigest()


def _token_sha256(value: MemoryExactContinuation | None) -> str | None:
    if value is None:
        return None
    value._validate()
    return hashlib.sha256(value.token.encode("ascii")).hexdigest()


class _ProcessPrivate:
    __slots__ = ()

    def __copy__(self) -> NoReturn:
        raise TypeError("exact-memory carrier is process-private")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise TypeError("exact-memory carrier is process-private")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise TypeError("exact-memory carrier is process-private")


@dataclass(frozen=True, slots=True, repr=False)
class MemoryExactContinuation:
    """An opaque restart token; storage owns its signed payload and key."""

    token: str

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if type(self.token) is not str:
            raise MemoryExactContractError("memory continuation must be opaque text")
        try:
            encoded = self.token.encode("ascii", errors="strict")
        except UnicodeEncodeError:
            raise MemoryExactContractError("memory continuation must use canonical ASCII") from None
        if (
            not _MIN_CONTINUATION_BYTES <= len(encoded) <= _MAX_CONTINUATION_BYTES
            or _OPAQUE_TOKEN.fullmatch(self.token) is None
        ):
            raise MemoryExactContractError("memory continuation is outside the closed envelope")

    def __repr__(self) -> str:
        return "MemoryExactContinuation(private=True)"

    @classmethod
    def create(cls, token: str) -> MemoryExactContinuation:
        return cls(token)


_REQUEST_KEYS = frozenset(
    {
        "active_turn_id",
        "as_of",
        "continuation",
        "known_at",
        "lifecycle_stages",
        "page_size",
        "principal_id",
        "query",
        "schema",
        "since",
        "snapshot_limit",
        "tenant_id",
        "until",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class MemoryExactRequest:
    """One immutable authenticated memory-selection intent."""

    tenant_id: str
    principal_id: str
    active_turn_id: str
    query: str
    since: str | None = None
    until: str | None = None
    as_of: str | None = None
    known_at: str | None = None
    lifecycle_stages: tuple[MemoryExactLifecycleStage, ...] = (
        MemoryExactLifecycleStage.ACTIVE,
        MemoryExactLifecycleStage.ARCHIVED,
        MemoryExactLifecycleStage.DEPRECATED,
    )
    page_size: int = MEMORY_EXACT_DEFAULT_PAGE_SIZE
    snapshot_limit: int = MEMORY_EXACT_DEFAULT_SNAPSHOT_LIMIT
    continuation: MemoryExactContinuation | None = None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _scope(self.tenant_id, label="memory tenant identity")
        _scope(self.principal_id, label="memory principal identity")
        _active_turn_id(self.active_turn_id)
        _query(self.query, normalize=False)
        if self.since is not None:
            _request_normalized_date(self.since, label="memory window start")
        if self.until is not None:
            _request_normalized_date(self.until, label="memory window end")
        if self.since is not None and self.until is not None and self.since > self.until:
            raise MemoryExactContractError("memory date window must be inclusive and ordered")
        if self.as_of is not None:
            _request_normalized_date(self.as_of, label="memory as-of date")
        if self.known_at is not None:
            _known_at_canonical(
                self.known_at,
                label="memory known-at boundary",
                reject_future=True,
            )
        if type(self.lifecycle_stages) is not tuple or self.lifecycle_stages != _lifecycles(
            self.lifecycle_stages
        ):
            raise MemoryExactContractError("memory lifecycle stages must be canonical")
        _count(
            self.page_size,
            label="memory page size",
            low=1,
            high=MEMORY_EXACT_MAX_PAGE_SIZE,
        )
        _count(
            self.snapshot_limit,
            label="memory snapshot limit",
            low=1,
            high=MEMORY_EXACT_MAX_SNAPSHOT_LIMIT,
        )
        if self.page_size > self.snapshot_limit:
            raise MemoryExactContractError("memory page size cannot exceed its top snapshot")
        if self.continuation is not None and type(self.continuation) is not MemoryExactContinuation:
            raise MemoryExactContractError("memory continuation must use the opaque wrapper")
        if self.continuation is not None:
            self.continuation._validate()
        if len(_canonical_json(self._private_payload_unchecked()).encode("ascii")) > _MAX_REQUEST_JSON_BYTES:
            raise MemoryExactContractError("memory request exceeds the closed byte limit")

    def __repr__(self) -> str:
        return (
            "MemoryExactRequest(private_scope=True, private_query=True, "
            f"page_size={self.page_size}, snapshot_limit={self.snapshot_limit}, "
            f"temporal={self.known_at is not None or self.as_of is not None})"
        )

    @property
    def query_sha256(self) -> str:
        return _query_sha256(self.query)

    @property
    def top_snapshot_limit(self) -> int:
        return self.snapshot_limit

    @classmethod
    def create(
        cls,
        *,
        tenant_id: str,
        principal_id: str,
        active_turn_id: str,
        query: str,
        since: date | str | None = None,
        until: date | str | None = None,
        as_of: date | str | None = None,
        known_at: datetime | str | None = None,
        lifecycle_stages: Iterable[MemoryExactLifecycleStage] = (
            MemoryExactLifecycleStage.ACTIVE,
            MemoryExactLifecycleStage.ARCHIVED,
            MemoryExactLifecycleStage.DEPRECATED,
        ),
        page_size: int = MEMORY_EXACT_DEFAULT_PAGE_SIZE,
        snapshot_limit: int = MEMORY_EXACT_DEFAULT_SNAPSHOT_LIMIT,
        continuation: MemoryExactContinuation | None = None,
    ) -> MemoryExactRequest:
        return cls(
            tenant_id=_scope(tenant_id, label="memory tenant identity"),
            principal_id=_scope(principal_id, label="memory principal identity"),
            active_turn_id=_active_turn_id(active_turn_id),
            query=_query(query, normalize=True),
            since=_window_date_input(
                since,
                label="memory window start",
                edge="since",
            ),
            until=_window_date_input(
                until,
                label="memory window end",
                edge="until",
            ),
            as_of=_as_of_date_input(as_of, label="memory as-of date"),
            known_at=_known_at_input(known_at, label="memory known-at boundary"),
            lifecycle_stages=_lifecycles(lifecycle_stages),
            page_size=_count(
                page_size,
                label="memory page size",
                low=1,
                high=MEMORY_EXACT_MAX_PAGE_SIZE,
            ),
            snapshot_limit=_count(
                snapshot_limit,
                label="memory snapshot limit",
                low=1,
                high=MEMORY_EXACT_MAX_SNAPSHOT_LIMIT,
            ),
            continuation=continuation,
        )

    def _private_payload_unchecked(self) -> dict[str, object]:
        return {
            "active_turn_id": self.active_turn_id,
            "as_of": self.as_of,
            "continuation": None if self.continuation is None else self.continuation.token,
            "known_at": self.known_at,
            "lifecycle_stages": [item.value for item in self.lifecycle_stages],
            "page_size": self.page_size,
            "principal_id": self.principal_id,
            "query": self.query,
            "schema": MEMORY_EXACT_REQUEST_SCHEMA,
            "since": self.since,
            "snapshot_limit": self.snapshot_limit,
            "tenant_id": self.tenant_id,
            "until": self.until,
        }

    def to_private_payload(self) -> dict[str, object]:
        self._validate()
        return self._private_payload_unchecked()

    def to_private_json(self) -> str:
        return _canonical_json(self.to_private_payload())

    def to_identity_payload(self) -> dict[str, object]:
        self._validate()
        return {
            "active_turn_id": self.active_turn_id,
            "as_of": self.as_of,
            "known_at": self.known_at,
            "lifecycle_stages": [item.value for item in self.lifecycle_stages],
            "page_size": self.page_size,
            "principal_id": self.principal_id,
            "query_sha256": self.query_sha256,
            "schema": MEMORY_EXACT_REQUEST_IDENTITY_SCHEMA,
            "since": self.since,
            "snapshot_limit": self.snapshot_limit,
            "tenant_id": self.tenant_id,
            "until": self.until,
        }

    def to_identity_json(self) -> str:
        return _canonical_json(self.to_identity_payload())

    def identity_digest_material(self) -> bytes:
        return b"friday/memory-exact-request-identity/v1\0" + self.to_identity_json().encode("ascii")

    def identity_sha256(self) -> str:
        return hashlib.sha256(self.identity_digest_material()).hexdigest()

    @classmethod
    def from_private_payload(cls, value: object) -> MemoryExactRequest:
        payload = _exact_object(value, _REQUEST_KEYS, label="exact-memory request")
        if payload["schema"] != MEMORY_EXACT_REQUEST_SCHEMA:
            raise MemoryExactContractError("exact-memory request schema is unsupported")
        lifecycle_values = payload["lifecycle_stages"]
        if type(lifecycle_values) is not list:
            raise MemoryExactContractError("memory lifecycle stages must be one closed array")
        for key in ("tenant_id", "principal_id", "active_turn_id", "query"):
            if type(payload[key]) is not str:
                raise MemoryExactContractError("memory request private text field is invalid")
        for key in ("since", "until", "as_of", "known_at", "continuation"):
            if payload[key] is not None and type(payload[key]) is not str:
                raise MemoryExactContractError("memory request optional text field is invalid")
        continuation = payload["continuation"]
        return cls(
            tenant_id=cast(str, payload["tenant_id"]),
            principal_id=cast(str, payload["principal_id"]),
            active_turn_id=cast(str, payload["active_turn_id"]),
            query=cast(str, payload["query"]),
            since=cast(str | None, payload["since"]),
            until=cast(str | None, payload["until"]),
            as_of=cast(str | None, payload["as_of"]),
            known_at=cast(str | None, payload["known_at"]),
            lifecycle_stages=tuple(
                cast(
                    MemoryExactLifecycleStage,
                    _enum(MemoryExactLifecycleStage, item, label="memory lifecycle stage"),
                )
                for item in lifecycle_values
            ),
            page_size=_count(
                payload["page_size"],
                label="memory page size",
                low=1,
                high=MEMORY_EXACT_MAX_PAGE_SIZE,
            ),
            snapshot_limit=_count(
                payload["snapshot_limit"],
                label="memory snapshot limit",
                low=1,
                high=MEMORY_EXACT_MAX_SNAPSHOT_LIMIT,
            ),
            continuation=(
                None if continuation is None else MemoryExactContinuation.create(cast(str, continuation))
            ),
        )

    @classmethod
    def parse_private(cls, value: str) -> MemoryExactRequest:
        result = cls.from_private_payload(_parse_canonical_object(value, label="exact-memory request"))
        if result.to_private_json() != value:
            raise MemoryExactContractError("exact-memory request is not semantically canonical")
        return result


def _memory_exact_revision_sha256(schema: str, exact_payload: Mapping[str, object]) -> str:
    """Hash a storage-owned exact revision without owning its database schema.

    Callers choose and version ``schema`` and must pass the complete exact row
    payload.  The helper supplies duplicate/non-finite resistance and a hard byte
    bound; it does not interpret or publish any row field.
    """

    revision_schema = _scope(schema, label="memory revision schema")
    if type(exact_payload) is not dict or any(type(key) is not str for key in exact_payload):
        raise MemoryExactContractError("memory exact revision must be one plain object")
    material = {"payload": exact_payload, "schema": revision_schema}
    encoded = _canonical_json(material).encode("ascii")
    if len(encoded) > _MAX_REVISION_JSON_BYTES:
        raise MemoryExactContractError("memory exact revision exceeds the closed byte limit")
    return hashlib.sha256(b"friday/memory-exact-storage-revision/v1\0" + encoded).hexdigest()


def _query_aware_excerpt(query: str, body: str) -> tuple[str, bool]:
    """Use the released memory-search excerpt algorithm without retaining body.

    ``best_snippet`` can deliberately join a table header to one record and can
    append its bounded missing-term note.  It is therefore not always one
    contiguous source span; claiming start/end offsets here would be dishonest.
    The candidate instead binds the exact excerpt digest and complete body digest.
    The released helper treats ``max_chars`` as its source-window budget, so its
    boundary ellipses or missing-term note can extend the returned string beyond
    that number.  This stricter carrier keeps the exact legacy result when it fits
    and otherwise keeps its exact first 600 characters.
    """

    # Deferred to avoid introducing a package-initialization cycle.  Importing a
    # submodule first initializes ``friday.retrieval`` completely, so this is the
    # already-released function used by legacy ``memory_search``.
    from friday.retrieval import best_snippet

    try:
        legacy_excerpt = best_snippet(
            query,
            body,
            max_chars=MEMORY_EXACT_MAX_EXCERPT_CHARS,
        )
    except (MemoryError, OverflowError, RecursionError, TypeError, UnicodeError, ValueError):
        raise MemoryExactContractError("memory query-aware excerpt is unavailable") from None
    legacy_visible = _valid_utf8(
        legacy_excerpt,
        label="legacy memory query-aware excerpt",
        maximum_bytes=(MEMORY_EXACT_MAX_EXCERPT_CHARS + 256) * 4,
        allow_empty=True,
    )
    visible = legacy_visible[:MEMORY_EXACT_MAX_EXCERPT_CHARS]
    return visible, visible != body


def _candidate_revision_sha256(
    *,
    request_identity_sha256: object,
    knowledge_id: object,
    raw_object_id: object,
    source_handle: object,
    knowledge_revision_sha256: object,
    raw_revision_sha256: object,
    title: object,
    knowledge_kind: object,
    lifecycle_stage: object,
    updated_at: object,
    excerpt: object,
    excerpt_truncated: object,
    content_chars: object,
    body_sha256: object,
) -> str:
    request_identity = _sha256(
        request_identity_sha256,
        label="candidate request identity",
    )
    knowledge = _scope(knowledge_id, label="candidate knowledge identity")
    raw = _scope(raw_object_id, label="candidate raw-source identity")
    source = _sha256(source_handle, label="candidate opaque source handle")
    knowledge_revision = _sha256(
        knowledge_revision_sha256,
        label="candidate knowledge revision",
    )
    raw_revision = _sha256(raw_revision_sha256, label="candidate raw revision")
    display_title = _project_title(title)
    kind = _display_text(
        knowledge_kind,
        label="candidate knowledge kind",
        maximum_chars=_MAX_KIND_CHARS,
        allow_empty=False,
    )
    if type(lifecycle_stage) is not MemoryExactLifecycleStage:
        raise MemoryExactContractError("candidate lifecycle stage is invalid")
    updated = _instant(updated_at, label="candidate update timestamp")
    visible = _valid_utf8(
        excerpt,
        label="candidate bounded excerpt",
        maximum_bytes=MEMORY_EXACT_MAX_EXCERPT_CHARS * 4,
        allow_empty=True,
    )
    if len(visible) > MEMORY_EXACT_MAX_EXCERPT_CHARS:
        raise MemoryExactContractError("candidate excerpt exceeds its character limit")
    total = _count(content_chars, label="candidate source character count")
    if type(excerpt_truncated) is not bool:
        raise MemoryExactContractError("candidate excerpt provenance is invalid")
    if excerpt_truncated:
        if total <= len(visible):
            raise MemoryExactContractError("candidate excerpt coverage is inconsistent")
    elif total != len(visible):
        raise MemoryExactContractError("candidate excerpt truncation is inconsistent")
    body_revision = _sha256(body_sha256, label="candidate body digest")
    return _memory_exact_revision_sha256(
        "friday.memory-exact-candidate-binding.private.v1",
        {
            "body_sha256": body_revision,
            "content_chars": total,
            "excerpt_sha256": hashlib.sha256(visible.encode("utf-8")).hexdigest(),
            "excerpt_truncated": excerpt_truncated,
            "knowledge_id": knowledge,
            "knowledge_kind": kind,
            "knowledge_revision_sha256": knowledge_revision,
            "lifecycle_stage": lifecycle_stage.value,
            "raw_object_id": raw,
            "raw_revision_sha256": raw_revision,
            "request_identity_sha256": request_identity,
            "source_handle": source,
            "title": display_title,
            "updated_at": updated,
        },
    )


class MemoryExactCandidate(_ProcessPrivate):
    """One exact selected knowledge row without its full stored body."""

    __slots__ = (
        "_request_identity_sha256",
        "_seal",
        "body_sha256",
        "candidate_revision_sha256",
        "content_chars",
        "excerpt",
        "excerpt_truncated",
        "knowledge_id",
        "knowledge_kind",
        "knowledge_revision_sha256",
        "lifecycle_stage",
        "raw_object_id",
        "raw_revision_sha256",
        "source_handle",
        "title",
        "updated_at",
    )

    def __init__(
        self,
        *,
        request: MemoryExactRequest,
        knowledge_id: str,
        raw_object_id: str,
        source_handle: str,
        knowledge_revision_sha256: str,
        raw_revision_sha256: str,
        title: str,
        knowledge_kind: str,
        lifecycle_stage: MemoryExactLifecycleStage,
        updated_at: str,
        body: str,
        _factory: object = None,
    ) -> None:
        if _factory is not _CARRIER_FACTORY:
            raise MemoryExactContractError("memory candidate requires the private carrier factory")
        if type(request) is not MemoryExactRequest:
            raise MemoryExactContractError("memory candidate requires its canonical request")
        request._validate()
        if type(lifecycle_stage) is not MemoryExactLifecycleStage or lifecycle_stage not in (
            request.lifecycle_stages
        ):
            raise MemoryExactContractError("memory candidate escaped its lifecycle selection")
        full_body = _valid_utf8(
            body,
            label="stored knowledge body",
            maximum_bytes=_MAX_STORED_BODY_BYTES,
            allow_empty=True,
        )
        excerpt, excerpt_truncated = _query_aware_excerpt(
            request.query,
            full_body,
        )
        body_digest = hashlib.sha256(full_body.encode("utf-8")).hexdigest()
        request_identity = request.identity_sha256()
        revision = _candidate_revision_sha256(
            request_identity_sha256=request_identity,
            knowledge_id=knowledge_id,
            raw_object_id=raw_object_id,
            source_handle=source_handle,
            knowledge_revision_sha256=knowledge_revision_sha256,
            raw_revision_sha256=raw_revision_sha256,
            title=title,
            knowledge_kind=knowledge_kind,
            lifecycle_stage=lifecycle_stage,
            updated_at=updated_at,
            excerpt=excerpt,
            excerpt_truncated=excerpt_truncated,
            content_chars=len(full_body),
            body_sha256=body_digest,
        )
        seal = _keyed_handle(
            b"friday/memory-exact-candidate-seal/v1",
            {"candidate_revision_sha256": revision},
        )
        values: tuple[tuple[str, object], ...] = (
            ("_request_identity_sha256", request_identity),
            ("knowledge_id", _scope(knowledge_id, label="candidate knowledge identity")),
            ("raw_object_id", _scope(raw_object_id, label="candidate raw-source identity")),
            ("source_handle", _sha256(source_handle, label="candidate opaque source handle")),
            (
                "knowledge_revision_sha256",
                _sha256(knowledge_revision_sha256, label="candidate knowledge revision"),
            ),
            (
                "raw_revision_sha256",
                _sha256(raw_revision_sha256, label="candidate raw revision"),
            ),
            (
                "title",
                _project_title(title),
            ),
            (
                "knowledge_kind",
                _display_text(
                    knowledge_kind,
                    label="candidate knowledge kind",
                    maximum_chars=_MAX_KIND_CHARS,
                    allow_empty=False,
                ),
            ),
            ("lifecycle_stage", lifecycle_stage),
            ("updated_at", _instant(updated_at, label="candidate update timestamp")),
            ("excerpt", excerpt),
            ("excerpt_truncated", excerpt_truncated),
            ("content_chars", len(full_body)),
            ("body_sha256", body_digest),
            ("candidate_revision_sha256", revision),
            ("_seal", seal),
        )
        for name, value in values:
            object.__setattr__(self, name, value)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("exact-memory candidate is immutable")

    def __repr__(self) -> str:
        if not self._is_process_owned():
            return "MemoryExactCandidate(invalid=True, body_retained=False)"
        return "MemoryExactCandidate(private_source=True, bounded_excerpt=True, body_retained=False)"

    @property
    def revision_sha256(self) -> str:
        return self.candidate_revision_sha256

    @property
    def request_identity_sha256(self) -> str:
        return self._request_identity_sha256

    def _is_process_owned(self) -> bool:
        try:
            current_revision = _candidate_revision_sha256(
                request_identity_sha256=self._request_identity_sha256,
                knowledge_id=self.knowledge_id,
                raw_object_id=self.raw_object_id,
                source_handle=self.source_handle,
                knowledge_revision_sha256=self.knowledge_revision_sha256,
                raw_revision_sha256=self.raw_revision_sha256,
                title=self.title,
                knowledge_kind=self.knowledge_kind,
                lifecycle_stage=self.lifecycle_stage,
                updated_at=self.updated_at,
                excerpt=self.excerpt,
                excerpt_truncated=self.excerpt_truncated,
                content_chars=self.content_chars,
                body_sha256=self.body_sha256,
            )
            expected_seal = _keyed_handle(
                b"friday/memory-exact-candidate-seal/v1",
                {"candidate_revision_sha256": current_revision},
            )
            return hmac.compare_digest(
                self.candidate_revision_sha256,
                current_revision,
            ) and hmac.compare_digest(self._seal, expected_seal)
        except (AttributeError, MemoryExactContractError, TypeError, UnicodeError):
            return False


def _create_memory_exact_candidate(
    *,
    request: MemoryExactRequest,
    knowledge_id: str,
    raw_object_id: str,
    source_handle: str,
    knowledge_revision_sha256: str,
    raw_revision_sha256: str,
    title: str,
    knowledge_kind: str,
    lifecycle_stage: MemoryExactLifecycleStage,
    updated_at: str,
    body: str,
) -> MemoryExactCandidate:
    """Private storage seam; the returned carrier discards ``body``."""

    return MemoryExactCandidate(
        request=request,
        knowledge_id=knowledge_id,
        raw_object_id=raw_object_id,
        source_handle=source_handle,
        knowledge_revision_sha256=knowledge_revision_sha256,
        raw_revision_sha256=raw_revision_sha256,
        title=title,
        knowledge_kind=knowledge_kind,
        lifecycle_stage=lifecycle_stage,
        updated_at=updated_at,
        body=body,
        _factory=_CARRIER_FACTORY,
    )


@dataclass(frozen=True, slots=True, repr=False)
class MemoryExactTemporalStatus:
    """The exact valid-time/transaction-time attestation for one page."""

    as_of: str | None
    known_at: str | None
    known_at_floor: str | None
    history_complete: bool
    identity_basis: str
    temporal_basis: MemoryExactTemporalBasis

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if self.as_of is not None:
            _request_normalized_date(self.as_of, label="memory temporal as-of date")
        if type(self.history_complete) is not bool or self.history_complete is not True:
            raise MemoryExactContractError("memory temporal history must be explicitly complete")
        if self.identity_basis != "current_names":
            raise MemoryExactContractError("memory temporal identity basis is unsupported")
        if type(self.temporal_basis) is not MemoryExactTemporalBasis:
            raise MemoryExactContractError("memory temporal basis is invalid")
        if self.known_at is None:
            if (
                self.known_at_floor is not None
                or self.temporal_basis is not MemoryExactTemporalBasis.VALID_TIME
            ):
                raise MemoryExactContractError("current memory temporal status is inconsistent")
            return
        known = _known_at_canonical(
            self.known_at,
            label="memory temporal known-at boundary",
            reject_future=True,
        )
        if self.known_at_floor is None:
            raise MemoryExactContractError("memory relation-history floor is missing")
        floor = _known_at_canonical(
            self.known_at_floor,
            label="memory relation-history floor",
            reject_future=False,
        )
        if floor > known or self.temporal_basis is not MemoryExactTemporalBasis.BITEMPORAL:
            raise MemoryExactContractError("memory bitemporal status is inconsistent")

    def __repr__(self) -> str:
        return (
            f"MemoryExactTemporalStatus(temporal_basis={self.temporal_basis.value!r}, history_complete=True)"
        )

    @classmethod
    def create(
        cls,
        *,
        as_of: date | str | None = None,
        known_at: datetime | str | None = None,
        known_at_floor: datetime | str | None = None,
        history_complete: bool = True,
        identity_basis: str = "current_names",
    ) -> MemoryExactTemporalStatus:
        normalized_known = _known_at_input(known_at, label="memory temporal known-at boundary")
        if known_at_floor is None or known_at_floor == "":
            normalized_floor = None
        elif isinstance(known_at_floor, datetime):
            if known_at_floor.tzinfo is None or known_at_floor.utcoffset() is None:
                raise MemoryExactContractError("memory relation-history floor must include an offset")
            normalized_floor = (
                known_at_floor.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            )
        elif type(known_at_floor) is str:
            text = known_at_floor.strip()
            if _KNOWN_AT_INPUT.fullmatch(text) is None:
                raise MemoryExactContractError(
                    "memory relation-history floor must be an offset-aware RFC3339 instant"
                )
            try:
                parsed_floor = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                raise MemoryExactContractError(
                    "memory relation-history floor must be an offset-aware RFC3339 instant"
                ) from None
            if parsed_floor.tzinfo is None or parsed_floor.utcoffset() is None:
                raise MemoryExactContractError("memory relation-history floor must include an offset")
            normalized_floor = (
                parsed_floor.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            )
        else:
            raise MemoryExactContractError(
                "memory relation-history floor must be an offset-aware RFC3339 instant"
            )
        return cls(
            as_of=_as_of_date_input(as_of, label="memory temporal as-of date"),
            known_at=normalized_known,
            known_at_floor=normalized_floor,
            history_complete=history_complete,
            identity_basis=identity_basis,
            temporal_basis=(
                MemoryExactTemporalBasis.BITEMPORAL
                if normalized_known is not None
                else MemoryExactTemporalBasis.VALID_TIME
            ),
        )

    def to_model_payload(self) -> dict[str, object]:
        self._validate()
        return {
            "as_of": self.as_of,
            "history_complete": self.history_complete,
            "identity_basis": self.identity_basis,
            "known_at": self.known_at,
            "known_at_floor": self.known_at_floor,
            "temporal_basis": self.temporal_basis.value,
        }


@dataclass(frozen=True, slots=True, repr=False)
class MemoryExactDateWindowStatus:
    """Exact, ID-free attestation of legacy date-window application."""

    since: str | None
    until: str | None
    applied: bool
    empty: bool

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if self.since is not None:
            _request_normalized_date(self.since, label="memory date-window lower bound")
        if self.until is not None:
            _request_normalized_date(self.until, label="memory date-window upper bound")
        if self.since is not None and self.until is not None and self.since > self.until:
            raise MemoryExactContractError("memory date-window bounds are reversed")
        if type(self.applied) is not bool or type(self.empty) is not bool:
            raise MemoryExactContractError("memory date-window status is invalid")
        if not self.requested and (self.applied or self.empty):
            raise MemoryExactContractError("memory date-window status invented a request")
        if self.empty and not self.applied:
            raise MemoryExactContractError("empty memory date-window must have been applied")

    @property
    def requested(self) -> bool:
        return self.since is not None or self.until is not None

    def __repr__(self) -> str:
        return (
            "MemoryExactDateWindowStatus("
            f"requested={self.requested}, applied={self.applied}, empty={self.empty})"
        )

    def to_model_payload(self) -> dict[str, object]:
        self._validate()
        return {
            "applied": self.applied,
            "empty": self.empty,
            "requested": self.requested,
            "since": self.since,
            "until": self.until,
        }


def _graph_date(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    return _normalized_date(value, label=label)


@dataclass(frozen=True, slots=True, repr=False)
class MemoryExactGraphNodeProjection:
    """One human-readable graph node addressed only by page-local ordinal."""

    ordinal: int
    name: str
    entity_type: str

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _count(
            self.ordinal,
            label="memory graph node ordinal",
            low=1,
            high=MEMORY_EXACT_MAX_GRAPH_NODES,
        )
        _display_text(
            self.name,
            label="memory graph node name",
            maximum_chars=_MAX_NAME_CHARS,
            allow_empty=False,
        )
        _display_text(
            self.entity_type,
            label="memory graph node type",
            maximum_chars=_MAX_KIND_CHARS,
            allow_empty=False,
        )

    @property
    def alias(self) -> str:
        return f"n{self.ordinal}"

    def __repr__(self) -> str:
        return f"MemoryExactGraphNodeProjection(alias={self.alias!r}, private_identity=False)"

    def to_model_payload(self) -> dict[str, object]:
        self._validate()
        return {"alias": self.alias, "name": self.name, "type": self.entity_type}


def _validate_graph_relation_fields(
    *,
    ordinal: object,
    source_ordinal: object,
    target_ordinal: object,
    relation_type: object,
    valid_from: object,
    valid_to: object,
    ordinal_label: str,
    ordinal_high: int,
) -> None:
    _count(ordinal, label=ordinal_label, low=1, high=ordinal_high)
    source = _count(
        source_ordinal,
        label="memory graph source ordinal",
        low=1,
        high=MEMORY_EXACT_MAX_GRAPH_NODES,
    )
    target = _count(
        target_ordinal,
        label="memory graph target ordinal",
        low=1,
        high=MEMORY_EXACT_MAX_GRAPH_NODES,
    )
    if source == target:
        raise MemoryExactContractError("memory graph relation endpoints must be distinct")
    _display_text(
        relation_type,
        label="memory graph relation type",
        maximum_chars=_MAX_KIND_CHARS,
        allow_empty=False,
    )
    start = _graph_date(valid_from, label="memory graph valid-from date")
    end = _graph_date(valid_to, label="memory graph valid-to date")
    if start is not None and end is not None and end < start:
        raise MemoryExactContractError("memory graph valid-time interval is reversed")


def _validate_graph_evidence_fields(
    *,
    implicit: object,
    evidence_basis: object,
    evidence_result_ordinal: object,
) -> None:
    if type(implicit) is not bool:
        raise MemoryExactContractError("memory graph implicit marker is invalid")
    if type(evidence_basis) is not MemoryExactGraphEvidenceBasis:
        raise MemoryExactContractError("memory graph evidence basis is invalid")
    if implicit != (evidence_basis is MemoryExactGraphEvidenceBasis.ACCEPTED_LINKS):
        raise MemoryExactContractError("memory graph evidence basis contradicts its relation kind")
    if evidence_result_ordinal is not None:
        _count(
            evidence_result_ordinal,
            label="memory graph evidence result ordinal",
            low=1,
            high=MEMORY_EXACT_MAX_PAGE_SIZE,
        )
        if evidence_basis is MemoryExactGraphEvidenceBasis.RELATION_ROW_ONLY:
            raise MemoryExactContractError("an unreviewed graph row cannot claim model-visible evidence")


@dataclass(frozen=True, slots=True, repr=False)
class MemoryExactGraphRelationProjection:
    """One bounded relation without a database relation or endpoint ID."""

    ordinal: int
    source_ordinal: int
    target_ordinal: int
    relation_type: str
    valid_from: str | None = None
    valid_to: str | None = None
    implicit: bool = False
    evidence_basis: MemoryExactGraphEvidenceBasis = MemoryExactGraphEvidenceBasis.RELATION_ROW_ONLY
    evidence_result_ordinal: int | None = None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _validate_graph_relation_fields(
            ordinal=self.ordinal,
            source_ordinal=self.source_ordinal,
            target_ordinal=self.target_ordinal,
            relation_type=self.relation_type,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            ordinal_label="memory graph relation ordinal",
            ordinal_high=MEMORY_EXACT_MAX_GRAPH_RELATIONS,
        )
        _validate_graph_evidence_fields(
            implicit=self.implicit,
            evidence_basis=self.evidence_basis,
            evidence_result_ordinal=self.evidence_result_ordinal,
        )

    @property
    def alias(self) -> str:
        return f"r{self.ordinal}"

    def __repr__(self) -> str:
        return f"MemoryExactGraphRelationProjection(alias={self.alias!r}, raw_ids=False)"

    def to_model_payload(self) -> dict[str, object]:
        self._validate()
        return {
            "alias": self.alias,
            "evidence_basis": self.evidence_basis.value,
            "evidence_result_ordinal": self.evidence_result_ordinal,
            "implicit": self.implicit,
            "relation": self.relation_type,
            "source": f"n{self.source_ordinal}",
            "target": f"n{self.target_ordinal}",
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
        }


@dataclass(frozen=True, slots=True, repr=False)
class MemoryExactGraphEdgeProjection:
    """One complete path edge, addressed only by path-local ordinal."""

    ordinal: int
    source_ordinal: int
    target_ordinal: int
    relation_type: str
    direction: MemoryExactGraphDirection
    valid_from: str | None = None
    valid_to: str | None = None
    implicit: bool = False
    evidence_basis: MemoryExactGraphEvidenceBasis = MemoryExactGraphEvidenceBasis.RELATION_ROW_ONLY
    evidence_result_ordinal: int | None = None

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _validate_graph_relation_fields(
            ordinal=self.ordinal,
            source_ordinal=self.source_ordinal,
            target_ordinal=self.target_ordinal,
            relation_type=self.relation_type,
            valid_from=self.valid_from,
            valid_to=self.valid_to,
            ordinal_label="memory graph path-edge ordinal",
            ordinal_high=MEMORY_EXACT_MAX_GRAPH_PATH_EDGES,
        )
        if type(self.direction) is not MemoryExactGraphDirection:
            raise MemoryExactContractError("memory graph path direction is invalid")
        _validate_graph_evidence_fields(
            implicit=self.implicit,
            evidence_basis=self.evidence_basis,
            evidence_result_ordinal=self.evidence_result_ordinal,
        )

    @property
    def alias(self) -> str:
        return f"e{self.ordinal}"

    def __repr__(self) -> str:
        return f"MemoryExactGraphEdgeProjection(alias={self.alias!r}, raw_ids=False)"

    def to_model_payload(self) -> dict[str, object]:
        self._validate()
        return {
            "alias": self.alias,
            "direction": self.direction.value,
            "evidence_basis": self.evidence_basis.value,
            "evidence_result_ordinal": self.evidence_result_ordinal,
            "from": f"n{self.source_ordinal}",
            "implicit": self.implicit,
            "relation": self.relation_type,
            "to": f"n{self.target_ordinal}",
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
        }


@dataclass(frozen=True, slots=True, repr=False)
class MemoryExactGraphPathProjection:
    """One complete graph path; invalid or overlong paths are never prefixes."""

    ordinal: int
    edges: tuple[MemoryExactGraphEdgeProjection, ...]

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _count(
            self.ordinal,
            label="memory graph path ordinal",
            low=1,
            high=MEMORY_EXACT_MAX_GRAPH_PATHS,
        )
        if (
            type(self.edges) is not tuple
            or not self.edges
            or len(self.edges) > MEMORY_EXACT_MAX_GRAPH_PATH_EDGES
            or any(type(item) is not MemoryExactGraphEdgeProjection for item in self.edges)
        ):
            raise MemoryExactContractError("memory graph path edges are outside the closed bound")
        for edge in self.edges:
            edge._validate()
        if tuple(item.ordinal for item in self.edges) != tuple(range(1, len(self.edges) + 1)):
            raise MemoryExactContractError("memory graph path-edge ordinals must be consecutive")
        for previous, current in zip(self.edges, self.edges[1:], strict=False):
            if previous.target_ordinal != current.source_ordinal:
                raise MemoryExactContractError("memory graph path is not contiguous")
        visited = (self.edges[0].source_ordinal,) + tuple(item.target_ordinal for item in self.edges)
        if len(visited) != len(set(visited)):
            raise MemoryExactContractError("memory graph path must be simple")

    @property
    def alias(self) -> str:
        return f"p{self.ordinal}"

    @property
    def grounded(self) -> bool:
        return all(
            item.evidence_basis is not MemoryExactGraphEvidenceBasis.RELATION_ROW_ONLY
            and item.evidence_result_ordinal is not None
            for item in self.edges
        )

    def __repr__(self) -> str:
        return f"MemoryExactGraphPathProjection(alias={self.alias!r}, edge_count={len(self.edges)})"

    def to_model_payload(self) -> dict[str, object]:
        self._validate()
        return {
            "alias": self.alias,
            "edges": [item.to_model_payload() for item in self.edges],
            "grounded": self.grounded,
        }


def _graph_evidence_result_ordinals(
    graph: MemoryExactGraphProjection,
) -> frozenset[int]:
    relation_ordinals = (
        item.evidence_result_ordinal for item in graph.relations if item.evidence_result_ordinal is not None
    )
    edge_ordinals = (
        edge.evidence_result_ordinal
        for path in graph.paths
        for edge in path.edges
        if edge.evidence_result_ordinal is not None
    )
    return frozenset((*relation_ordinals, *edge_ordinals))


@dataclass(frozen=True, slots=True, repr=False)
class MemoryExactGraphProjection:
    """Bounded, ID-free graph context with explicit lower-bound coverage."""

    effective_query: str = ""
    nodes: tuple[MemoryExactGraphNodeProjection, ...] = ()
    relations: tuple[MemoryExactGraphRelationProjection, ...] = ()
    paths: tuple[MemoryExactGraphPathProjection, ...] = ()
    root_ordinals: tuple[int, ...] = ()
    nodes_matched_at_least: int = 0
    relations_matched_at_least: int = 0
    paths_matched_at_least: int = 0
    nodes_coverage: MemoryExactGraphCoverage = MemoryExactGraphCoverage.COMPLETE
    relations_coverage: MemoryExactGraphCoverage = MemoryExactGraphCoverage.COMPLETE
    paths_coverage: MemoryExactGraphCoverage = MemoryExactGraphCoverage.COMPLETE
    expanded: bool = False

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if type(self.expanded) is not bool:
            raise MemoryExactContractError("memory graph expansion flag is invalid")
        if self.effective_query:
            _query(self.effective_query, normalize=False)
        if (
            type(self.nodes) is not tuple
            or len(self.nodes) > MEMORY_EXACT_MAX_GRAPH_NODES
            or any(type(item) is not MemoryExactGraphNodeProjection for item in self.nodes)
        ):
            raise MemoryExactContractError("memory graph nodes exceed the closed projection")
        if (
            type(self.relations) is not tuple
            or len(self.relations) > MEMORY_EXACT_MAX_GRAPH_RELATIONS
            or any(type(item) is not MemoryExactGraphRelationProjection for item in self.relations)
        ):
            raise MemoryExactContractError("memory graph relations exceed the closed projection")
        if (
            type(self.paths) is not tuple
            or len(self.paths) > MEMORY_EXACT_MAX_GRAPH_PATHS
            or any(type(item) is not MemoryExactGraphPathProjection for item in self.paths)
        ):
            raise MemoryExactContractError("memory graph paths exceed the closed projection")
        for item in self.nodes:
            item._validate()
        for item in self.relations:
            item._validate()
        for item in self.paths:
            item._validate()
        if tuple(item.ordinal for item in self.nodes) != tuple(range(1, len(self.nodes) + 1)):
            raise MemoryExactContractError("memory graph node aliases must be consecutive")
        if tuple(item.ordinal for item in self.relations) != tuple(range(1, len(self.relations) + 1)):
            raise MemoryExactContractError("memory graph relation aliases must be consecutive")
        if tuple(item.ordinal for item in self.paths) != tuple(range(1, len(self.paths) + 1)):
            raise MemoryExactContractError("memory graph path aliases must be consecutive")
        node_ordinals = {item.ordinal for item in self.nodes}
        if (
            type(self.root_ordinals) is not tuple
            or len(self.root_ordinals) > MEMORY_EXACT_MAX_GRAPH_NODES
            or any(isinstance(item, bool) or type(item) is not int for item in self.root_ordinals)
            or len(self.root_ordinals) != len(set(self.root_ordinals))
            or not set(self.root_ordinals) <= node_ordinals
        ):
            raise MemoryExactContractError("memory graph roots escaped their page-local nodes")
        if (self.nodes or self.relations or self.paths or self.root_ordinals) and not (self.effective_query):
            raise MemoryExactContractError("memory graph evidence requires its effective query")
        relation_endpoints = {
            endpoint for item in self.relations for endpoint in (item.source_ordinal, item.target_ordinal)
        }
        path_endpoints = {
            endpoint
            for path in self.paths
            for edge in path.edges
            for endpoint in (edge.source_ordinal, edge.target_ordinal)
        }
        if not (relation_endpoints | path_endpoints) <= node_ordinals:
            raise MemoryExactContractError("memory graph projection references an absent node alias")
        nodes_matched = _count(
            self.nodes_matched_at_least,
            label="memory graph matched-node lower bound",
        )
        relations_matched = _count(
            self.relations_matched_at_least,
            label="memory graph matched-relation lower bound",
        )
        paths_matched = _count(
            self.paths_matched_at_least,
            label="memory graph matched-path lower bound",
        )
        if (
            nodes_matched < len(self.nodes)
            or relations_matched < len(self.relations)
            or paths_matched < len(self.paths)
        ):
            raise MemoryExactContractError("memory graph coverage understates projected rows")
        coverage = (
            ("node", self.nodes_coverage, nodes_matched, len(self.nodes)),
            (
                "relation",
                self.relations_coverage,
                relations_matched,
                len(self.relations),
            ),
            ("path", self.paths_coverage, paths_matched, len(self.paths)),
        )
        for label, state, matched, shown in coverage:
            if type(state) is not MemoryExactGraphCoverage:
                raise MemoryExactContractError(f"memory graph {label} coverage is invalid")
            if state is MemoryExactGraphCoverage.COMPLETE and matched != shown:
                raise MemoryExactContractError(f"complete memory graph {label} coverage changed its count")
            if state is MemoryExactGraphCoverage.PARTIAL and matched <= shown:
                raise MemoryExactContractError(
                    f"partial memory graph {label} coverage lacks a lower-bound witness"
                )
            if state is MemoryExactGraphCoverage.UNKNOWN and matched != shown:
                raise MemoryExactContractError(f"unknown memory graph {label} coverage invented a count")
        if not self.expanded and (
            self.relations
            or self.paths
            or relations_matched
            or paths_matched
            or self.relations_coverage is not MemoryExactGraphCoverage.COMPLETE
            or self.paths_coverage is not MemoryExactGraphCoverage.COMPLETE
        ):
            raise MemoryExactContractError(
                "unexpanded memory graph cannot carry traversed relations or paths"
            )
        encoded = _canonical_json(self._model_payload_unchecked()).encode("ascii")
        if len(encoded) > _MAX_GRAPH_JSON_BYTES:
            raise MemoryExactContractError("memory graph projection exceeds the closed byte limit")

    @classmethod
    def empty(cls, *, expanded: bool = False) -> MemoryExactGraphProjection:
        return cls(expanded=expanded)

    @property
    def nodes_truncated(self) -> bool | None:
        if self.nodes_coverage is MemoryExactGraphCoverage.UNKNOWN:
            return None
        return self.nodes_coverage is MemoryExactGraphCoverage.PARTIAL

    @property
    def relations_truncated(self) -> bool | None:
        if self.relations_coverage is MemoryExactGraphCoverage.UNKNOWN:
            return None
        return self.relations_coverage is MemoryExactGraphCoverage.PARTIAL

    @property
    def paths_truncated(self) -> bool | None:
        if self.paths_coverage is MemoryExactGraphCoverage.UNKNOWN:
            return None
        return self.paths_coverage is MemoryExactGraphCoverage.PARTIAL

    def __repr__(self) -> str:
        return (
            "MemoryExactGraphProjection("
            f"nodes={len(self.nodes)}, relations={len(self.relations)}, paths={len(self.paths)}, "
            f"coverage=({self.nodes_coverage.value!r}, "
            f"{self.relations_coverage.value!r}, {self.paths_coverage.value!r}), "
            "raw_ids=False)"
        )

    def _model_payload_unchecked(self) -> dict[str, object]:
        return {
            "expanded": self.expanded,
            "nodes": [item.to_model_payload() for item in self.nodes],
            "nodes_coverage": self.nodes_coverage.value,
            "nodes_matched_at_least": self.nodes_matched_at_least,
            "nodes_shown": len(self.nodes),
            "nodes_truncated": self.nodes_truncated,
            "paths": [item.to_model_payload() for item in self.paths],
            "paths_coverage": self.paths_coverage.value,
            "paths_matched_at_least": self.paths_matched_at_least,
            "paths_shown": len(self.paths),
            "paths_truncated": self.paths_truncated,
            "relations": [item.to_model_payload() for item in self.relations],
            "relations_coverage": self.relations_coverage.value,
            "relations_matched_at_least": self.relations_matched_at_least,
            "relations_shown": len(self.relations),
            "relations_truncated": self.relations_truncated,
            "query": self.effective_query,
            "roots": [f"n{item}" for item in self.root_ordinals],
            "schema": MEMORY_EXACT_GRAPH_PROJECTION_SCHEMA,
        }

    def to_model_payload(self) -> dict[str, object]:
        self._validate()
        return self._model_payload_unchecked()

    def to_model_json(self) -> str:
        return json.dumps(
            self.to_model_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def _page_selection_handle(
    *,
    request: object,
    candidates: object,
    date_window_status: object,
    temporal_status: object,
    graph_projection: object,
    graph_source_set_sha256: object,
    authority_handle: object,
    snapshot_handle: object,
    offset: object,
    total_rows: object,
    snapshot_rows: object,
    matched_rows: object,
    next_continuation: object,
) -> str:
    if type(request) is not MemoryExactRequest:
        raise MemoryExactContractError("memory page requires its canonical request")
    request._validate()
    if type(candidates) is not tuple or any(
        type(item) is not MemoryExactCandidate or not item._is_process_owned() for item in candidates
    ):
        raise MemoryExactContractError("memory page candidates require storage authority")
    if type(date_window_status) is not MemoryExactDateWindowStatus:
        raise MemoryExactContractError("memory page date-window status is invalid")
    date_window_status._validate()
    if type(temporal_status) is not MemoryExactTemporalStatus:
        raise MemoryExactContractError("memory page temporal status is invalid")
    temporal_status._validate()
    if type(graph_projection) is not MemoryExactGraphProjection:
        raise MemoryExactContractError("memory page graph projection is invalid")
    graph_projection._validate()
    graph_source_set = _sha256(
        graph_source_set_sha256,
        label="memory graph source-set revision",
    )
    authority = _sha256(authority_handle, label="memory authority handle")
    snapshot = _sha256(snapshot_handle, label="memory snapshot handle")
    page_offset = _count(offset, label="memory page offset")
    total = _count(total_rows, label="memory authorized row total")
    retained = _count(
        snapshot_rows,
        label="memory retained snapshot row count",
        high=request.snapshot_limit,
    )
    matched = _count(matched_rows, label="memory matched-row lower bound")
    if next_continuation is not None and type(next_continuation) is not MemoryExactContinuation:
        raise MemoryExactContractError("outbound memory continuation is invalid")
    if next_continuation is not None:
        next_continuation._validate()
    return _keyed_handle(
        b"friday/memory-exact-page-selection/v1",
        {
            "authority_handle": authority,
            "candidate_revisions": [item.candidate_revision_sha256 for item in candidates],
            "date_window_status_sha256": _digest_payload(
                b"friday/memory-exact-date-window-status/v1",
                date_window_status.to_model_payload(),
            ),
            "graph_projection_sha256": _digest_payload(
                b"friday/memory-exact-graph-projection/v1",
                graph_projection.to_model_payload(),
            ),
            "graph_source_set_sha256": graph_source_set,
            "inbound_continuation_sha256": _token_sha256(request.continuation),
            "matched_rows": matched,
            "next_continuation_sha256": _token_sha256(
                cast(MemoryExactContinuation | None, next_continuation)
            ),
            "offset": page_offset,
            "request_identity_sha256": request.identity_sha256(),
            "schema": "friday.memory-exact-page-selection.private.v1",
            "snapshot_handle": snapshot,
            "snapshot_rows": retained,
            "temporal_status_sha256": _digest_payload(
                b"friday/memory-exact-temporal-status/v1",
                temporal_status.to_model_payload(),
            ),
            "total_rows": total,
        },
    )


class MemoryExactPage(_ProcessPrivate):
    """One authorized, stable top-snapshot page of exact candidates."""

    __slots__ = (
        "_seal",
        "authority_handle",
        "candidates",
        "date_window_status",
        "graph_projection",
        "graph_source_set_sha256",
        "matched_rows",
        "next_continuation",
        "offset",
        "request",
        "selection_handle",
        "snapshot_handle",
        "snapshot_rows",
        "temporal_status",
        "total_rows",
    )

    def __init__(
        self,
        *,
        request: MemoryExactRequest,
        candidates: tuple[MemoryExactCandidate, ...],
        date_window_status: MemoryExactDateWindowStatus,
        temporal_status: MemoryExactTemporalStatus,
        graph_projection: MemoryExactGraphProjection,
        graph_source_set_sha256: str,
        authority_handle: str,
        snapshot_handle: str,
        offset: int,
        total_rows: int,
        snapshot_rows: int,
        matched_rows: int,
        next_continuation: MemoryExactContinuation | None,
        _factory: object = None,
    ) -> None:
        if _factory is not _CARRIER_FACTORY:
            raise MemoryExactContractError("memory page requires the private carrier factory")
        if type(request) is not MemoryExactRequest:
            raise MemoryExactContractError("memory page requires its canonical request")
        request._validate()
        if type(candidates) is not tuple or len(candidates) > request.page_size:
            raise MemoryExactContractError("memory page candidates exceed the closed page size")
        if any(type(item) is not MemoryExactCandidate or not item._is_process_owned() for item in candidates):
            raise MemoryExactContractError("memory page candidates require storage authority")
        request_identity = request.identity_sha256()
        if any(
            not hmac.compare_digest(item.request_identity_sha256, request_identity)
            or item.lifecycle_stage not in request.lifecycle_stages
            for item in candidates
        ):
            raise MemoryExactContractError("memory candidate escaped its exact request")
        if len({item.knowledge_id for item in candidates}) != len(candidates) or len(
            {item.candidate_revision_sha256 for item in candidates}
        ) != len(candidates):
            raise MemoryExactContractError("memory page must retain each exact candidate once")
        if type(date_window_status) is not MemoryExactDateWindowStatus:
            raise MemoryExactContractError("memory page date-window status is invalid")
        date_window_status._validate()
        if date_window_status.since != request.since or date_window_status.until != request.until:
            raise MemoryExactContractError("memory page changed its requested date window")
        if type(temporal_status) is not MemoryExactTemporalStatus:
            raise MemoryExactContractError("memory page temporal status is invalid")
        temporal_status._validate()
        if temporal_status.as_of != request.as_of or temporal_status.known_at != request.known_at:
            raise MemoryExactContractError("memory page changed its requested temporal boundary")
        if type(graph_projection) is not MemoryExactGraphProjection:
            raise MemoryExactContractError("memory page graph projection is invalid")
        graph_projection._validate()
        if any(ordinal > len(candidates) for ordinal in _graph_evidence_result_ordinals(graph_projection)):
            raise MemoryExactContractError("memory graph evidence escaped its model-visible result page")
        graph_source_set = _sha256(
            graph_source_set_sha256,
            label="memory graph source-set revision",
        )
        authority = _sha256(authority_handle, label="memory authority handle")
        snapshot = _sha256(snapshot_handle, label="memory snapshot handle")
        page_offset = _count(offset, label="memory page offset")
        total = _count(total_rows, label="memory authorized row total")
        retained = _count(
            snapshot_rows,
            label="memory retained snapshot row count",
            high=request.snapshot_limit,
        )
        matched = _count(matched_rows, label="memory matched-row lower bound")
        if retained > matched or matched > total:
            raise MemoryExactContractError("memory match and snapshot coverage are inconsistent")
        covered_through = page_offset + len(candidates)
        if page_offset > retained or covered_through > retained:
            raise MemoryExactContractError("memory page coverage exceeds its bounded top snapshot")
        if (request.continuation is None) != (page_offset == 0):
            raise MemoryExactContractError("memory page offset is not bound to its continuation")
        if next_continuation is not None and type(next_continuation) is not MemoryExactContinuation:
            raise MemoryExactContractError("outbound memory continuation is invalid")
        if next_continuation is not None:
            next_continuation._validate()
        if next_continuation is not None and not candidates:
            raise MemoryExactContractError("an empty memory page cannot continue")
        if (next_continuation is None) != (covered_through == retained):
            raise MemoryExactContractError("memory continuation and top-snapshot coverage disagree")
        if (
            request.continuation is not None
            and next_continuation is not None
            and hmac.compare_digest(request.continuation.token, next_continuation.token)
        ):
            raise MemoryExactContractError("outbound continuation must advance the memory page")
        selection = _page_selection_handle(
            request=request,
            candidates=candidates,
            date_window_status=date_window_status,
            temporal_status=temporal_status,
            graph_projection=graph_projection,
            graph_source_set_sha256=graph_source_set,
            authority_handle=authority,
            snapshot_handle=snapshot,
            offset=page_offset,
            total_rows=total,
            snapshot_rows=retained,
            matched_rows=matched,
            next_continuation=next_continuation,
        )
        seal = _keyed_handle(
            b"friday/memory-exact-page-seal/v1",
            {"selection_handle": selection},
        )
        values: tuple[tuple[str, object], ...] = (
            ("request", request),
            ("candidates", candidates),
            ("date_window_status", date_window_status),
            ("temporal_status", temporal_status),
            ("graph_projection", graph_projection),
            ("graph_source_set_sha256", graph_source_set),
            ("authority_handle", authority),
            ("snapshot_handle", snapshot),
            ("offset", page_offset),
            ("total_rows", total),
            ("snapshot_rows", retained),
            ("matched_rows", matched),
            ("next_continuation", next_continuation),
            ("selection_handle", selection),
            ("_seal", seal),
        )
        for name, value in values:
            object.__setattr__(self, name, value)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("exact-memory page is immutable")

    def __repr__(self) -> str:
        if not self._is_process_owned():
            return "MemoryExactPage(invalid=True, private_candidates=True, bodies_retained=False)"
        return (
            f"MemoryExactPage(candidate_count={len(self.candidates)}, "
            "private_candidates=True, bodies_retained=False)"
        )

    @property
    def matched_at_least(self) -> int:
        return self.matched_rows

    @property
    def top_snapshot_rows(self) -> int:
        return self.snapshot_rows

    @property
    def has_more(self) -> bool:
        return self.next_continuation is not None

    def _is_process_owned(self) -> bool:
        try:
            rebuilt = MemoryExactPage(
                request=self.request,
                candidates=self.candidates,
                date_window_status=self.date_window_status,
                temporal_status=self.temporal_status,
                graph_projection=self.graph_projection,
                graph_source_set_sha256=self.graph_source_set_sha256,
                authority_handle=self.authority_handle,
                snapshot_handle=self.snapshot_handle,
                offset=self.offset,
                total_rows=self.total_rows,
                snapshot_rows=self.snapshot_rows,
                matched_rows=self.matched_rows,
                next_continuation=self.next_continuation,
                _factory=_CARRIER_FACTORY,
            )
            return hmac.compare_digest(
                self.selection_handle,
                rebuilt.selection_handle,
            ) and hmac.compare_digest(self._seal, rebuilt._seal)
        except (AttributeError, MemoryExactContractError, TypeError, UnicodeError):
            return False


def _create_memory_exact_page(
    *,
    request: MemoryExactRequest,
    candidates: tuple[MemoryExactCandidate, ...],
    date_window_status: MemoryExactDateWindowStatus,
    temporal_status: MemoryExactTemporalStatus,
    graph_projection: MemoryExactGraphProjection,
    graph_source_set_sha256: str,
    authority_handle: str,
    snapshot_handle: str,
    offset: int,
    total_rows: int,
    snapshot_rows: int,
    matched_rows: int,
    next_continuation: MemoryExactContinuation | None,
) -> MemoryExactPage:
    """Private storage seam; rows and counts must already be authorized."""

    return MemoryExactPage(
        request=request,
        candidates=candidates,
        date_window_status=date_window_status,
        temporal_status=temporal_status,
        graph_projection=graph_projection,
        graph_source_set_sha256=graph_source_set_sha256,
        authority_handle=authority_handle,
        snapshot_handle=snapshot_handle,
        offset=offset,
        total_rows=total_rows,
        snapshot_rows=snapshot_rows,
        matched_rows=matched_rows,
        next_continuation=next_continuation,
        _factory=_CARRIER_FACTORY,
    )


@dataclass(frozen=True, slots=True, repr=False)
class MemoryExactProjectionRow:
    """One model-safe result with no database, source or authority identity."""

    ordinal: int
    title: str
    knowledge_kind: str
    lifecycle_stage: MemoryExactLifecycleStage
    updated_at: str
    excerpt: str
    excerpt_truncated: bool
    content_chars: int

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _count(
            self.ordinal,
            label="memory projection ordinal",
            low=1,
            high=MEMORY_EXACT_MAX_PAGE_SIZE,
        )
        _title_text(
            self.title,
            label="memory projection title",
            maximum_chars=_MAX_MODEL_TITLE_CHARS,
        )
        _display_text(
            self.knowledge_kind,
            label="memory projection knowledge kind",
            maximum_chars=_MAX_KIND_CHARS,
            allow_empty=False,
        )
        if type(self.lifecycle_stage) is not MemoryExactLifecycleStage:
            raise MemoryExactContractError("memory projection lifecycle stage is invalid")
        _instant(self.updated_at, label="memory projection update timestamp")
        text = _valid_utf8(
            self.excerpt,
            label="memory projection excerpt",
            maximum_bytes=MEMORY_EXACT_MAX_EXCERPT_CHARS * 4,
            allow_empty=True,
        )
        if len(text) > MEMORY_EXACT_MAX_EXCERPT_CHARS:
            raise MemoryExactContractError("memory projection excerpt exceeds its character limit")
        if type(self.excerpt_truncated) is not bool:
            raise MemoryExactContractError("memory projection truncation flag is invalid")
        total = _count(self.content_chars, label="memory projection source character count")
        visible_source_chars = len(text) - int(text.startswith("…")) - int(text.endswith("…"))
        if self.excerpt_truncated:
            if total <= visible_source_chars:
                raise MemoryExactContractError("truncated memory projection is inconsistent")
        elif total != len(text):
            raise MemoryExactContractError("complete memory projection is inconsistent")

    def __repr__(self) -> str:
        return (
            f"MemoryExactProjectionRow(ordinal={self.ordinal}, "
            f"excerpt_truncated={self.excerpt_truncated}, private_text=True)"
        )

    def to_model_payload(self) -> dict[str, object]:
        self._validate()
        return {
            "content_chars": self.content_chars,
            "excerpt": self.excerpt,
            "excerpt_truncated": self.excerpt_truncated,
            "kind": self.knowledge_kind,
            "lifecycle_stage": self.lifecycle_stage.value,
            "ordinal": self.ordinal,
            "title": self.title,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True, repr=False)
class MemoryExactProjection:
    """The sole bounded model-facing view of an exact memory page."""

    rows: tuple[MemoryExactProjectionRow, ...]
    graph_projection: MemoryExactGraphProjection
    date_window_status: MemoryExactDateWindowStatus
    temporal_status: MemoryExactTemporalStatus
    row_coverage: MemoryExactRowCoverage
    content_coverage: MemoryExactContentCoverage
    offset: int
    shown_rows: int
    total_rows: int
    snapshot_rows: int
    matched_at_least: int
    snapshot_limit: int
    truncated_rows: int

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if type(self.rows) is not tuple or any(
            type(item) is not MemoryExactProjectionRow for item in self.rows
        ):
            raise MemoryExactContractError("memory projection rows must be immutable typed values")
        for item in self.rows:
            item._validate()
        if tuple(item.ordinal for item in self.rows) != tuple(range(1, len(self.rows) + 1)):
            raise MemoryExactContractError("memory projection ordinals must be consecutive")
        if type(self.graph_projection) is not MemoryExactGraphProjection:
            raise MemoryExactContractError("memory projection graph context is invalid")
        self.graph_projection._validate()
        if not _graph_evidence_result_ordinals(self.graph_projection) <= {item.ordinal for item in self.rows}:
            raise MemoryExactContractError("memory graph evidence escaped its model-visible result page")
        if type(self.date_window_status) is not MemoryExactDateWindowStatus:
            raise MemoryExactContractError("memory projection date-window status is invalid")
        self.date_window_status._validate()
        if type(self.temporal_status) is not MemoryExactTemporalStatus:
            raise MemoryExactContractError("memory projection temporal status is invalid")
        self.temporal_status._validate()
        if type(self.row_coverage) is not MemoryExactRowCoverage:
            raise MemoryExactContractError("memory row coverage is invalid")
        if type(self.content_coverage) is not MemoryExactContentCoverage:
            raise MemoryExactContractError("memory content coverage is invalid")
        offset = _count(self.offset, label="memory projection offset")
        shown = _count(
            self.shown_rows,
            label="shown memory rows",
            high=MEMORY_EXACT_MAX_PAGE_SIZE,
        )
        total = _count(self.total_rows, label="memory authorized row total")
        snapshot_limit = _count(
            self.snapshot_limit,
            label="memory projection snapshot limit",
            low=1,
            high=MEMORY_EXACT_MAX_SNAPSHOT_LIMIT,
        )
        retained = _count(
            self.snapshot_rows,
            label="memory retained snapshot row count",
            high=snapshot_limit,
        )
        matched = _count(self.matched_at_least, label="memory matched-row lower bound")
        truncated = _count(
            self.truncated_rows,
            label="truncated memory rows",
            high=MEMORY_EXACT_MAX_PAGE_SIZE,
        )
        if shown != len(self.rows) or retained > matched or matched > total or offset + shown > retained:
            raise MemoryExactContractError("memory projection row counts are inconsistent")
        # Reaching the end of the provider-retained snapshot proves pagination
        # exhaustion, not complete recall.  ``matched_at_least`` is only a lower
        # bound and a lifecycle-subset request is selected after the released
        # provider ranks without that selector.  Complete recall is therefore
        # claimed only when every authorized eligible corpus row was retained.
        complete = offset + shown == retained and retained == total
        if (self.row_coverage is MemoryExactRowCoverage.COMPLETE) != complete:
            raise MemoryExactContractError("memory row coverage is inconsistent")
        actual_truncated = sum(item.excerpt_truncated for item in self.rows)
        if truncated != actual_truncated or (
            self.content_coverage is MemoryExactContentCoverage.COMPLETE
        ) != (actual_truncated == 0):
            raise MemoryExactContractError("memory content coverage is inconsistent")
        encoded = json.dumps(
            self._model_payload_unchecked(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", errors="strict")
        if len(encoded) > _MAX_MODEL_JSON_BYTES:
            raise MemoryExactContractError("memory model projection exceeds the closed byte limit")

    def __repr__(self) -> str:
        return (
            f"MemoryExactProjection(row_count={len(self.rows)}, "
            f"row_coverage={self.row_coverage.value!r}, "
            f"content_coverage={self.content_coverage.value!r}, private_text=True)"
        )

    def _model_payload_unchecked(self) -> dict[str, object]:
        temporal = self.temporal_status.to_model_payload()
        return {
            **temporal,
            "content_coverage": self.content_coverage.value,
            "count": self.shown_rows,
            "date_window": self.date_window_status.to_model_payload(),
            "eligible_corpus_rows": self.total_rows,
            "graph_context": self.graph_projection.to_model_payload(),
            "matched_at_least": self.matched_at_least,
            "offset": self.offset,
            "results": [item.to_model_payload() for item in self.rows],
            "row_coverage": self.row_coverage.value,
            "schema": MEMORY_EXACT_MODEL_PROJECTION_SCHEMA,
            "shown_rows": self.shown_rows,
            "snapshot_limit": self.snapshot_limit,
            "snapshot_rows": self.snapshot_rows,
            "snapshot_exhausted": self.offset + self.shown_rows == self.snapshot_rows,
            "snapshot_truncated": self.matched_at_least > self.snapshot_rows,
            "truncated_rows": self.truncated_rows,
        }

    def to_model_payload(self) -> dict[str, object]:
        self._validate()
        return self._model_payload_unchecked()

    def to_model_json(self) -> str:
        return json.dumps(
            self.to_model_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def project_memory_exact_page(page: MemoryExactPage) -> MemoryExactProjection:
    """Produce the only model-facing projection from one process-owned page."""

    if type(page) is not MemoryExactPage or not page._is_process_owned():
        raise MemoryExactContractError("memory projection requires a process-owned page")
    rows = tuple(
        MemoryExactProjectionRow(
            ordinal=ordinal,
            title=candidate.title,
            knowledge_kind=candidate.knowledge_kind,
            lifecycle_stage=candidate.lifecycle_stage,
            updated_at=candidate.updated_at,
            excerpt=candidate.excerpt,
            excerpt_truncated=candidate.excerpt_truncated,
            content_chars=candidate.content_chars,
        )
        for ordinal, candidate in enumerate(page.candidates, 1)
    )
    truncated_rows = sum(item.excerpt_truncated for item in rows)
    complete_rows = (
        page.next_continuation is None
        and page.offset + len(rows) == page.snapshot_rows
        and page.snapshot_rows == page.total_rows
    )
    return MemoryExactProjection(
        rows=rows,
        graph_projection=page.graph_projection,
        date_window_status=page.date_window_status,
        temporal_status=page.temporal_status,
        row_coverage=(MemoryExactRowCoverage.COMPLETE if complete_rows else MemoryExactRowCoverage.PARTIAL),
        content_coverage=(
            MemoryExactContentCoverage.COMPLETE
            if truncated_rows == 0
            else MemoryExactContentCoverage.TRUNCATED
        ),
        offset=page.offset,
        shown_rows=len(rows),
        total_rows=page.total_rows,
        snapshot_rows=page.snapshot_rows,
        matched_at_least=page.matched_rows,
        snapshot_limit=page.request.snapshot_limit,
        truncated_rows=truncated_rows,
    )


class MemoryExactPublicationDecision(_ProcessPrivate):
    """A body-free, process-sealed, one-shot late-authorization receipt."""

    __slots__ = (
        "_authority_handle",
        "_claim_sha256",
        "_consumed",
        "_seal",
        "_selection_handle",
        "status",
    )

    def __init__(
        self,
        *,
        status: MemoryExactPublicationStatus,
        selection_handle: str,
        authority_handle: str,
        _factory: object = None,
    ) -> None:
        if _factory is not _DECISION_FACTORY:
            raise MemoryExactContractError("publication decision requires late reauthorization")
        if type(status) is not MemoryExactPublicationStatus:
            raise MemoryExactContractError("memory publication status is invalid")
        selection = _sha256(selection_handle, label="memory selection handle")
        authority = _sha256(authority_handle, label="memory authority handle")
        seal = _keyed_handle(
            b"friday/memory-exact-publication-decision/v1",
            {
                "authority_handle": authority,
                "claim_sha256": None,
                "consumed": False,
                "selection_handle": selection,
                "status": status.value,
            },
        )
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "_selection_handle", selection)
        object.__setattr__(self, "_authority_handle", authority)
        object.__setattr__(self, "_claim_sha256", None)
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_seal", seal)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("memory publication decision is immutable")

    def __repr__(self) -> str:
        if not self._is_process_owned():
            return "MemoryExactPublicationDecision(invalid=True, body_free=True, one_shot=True)"
        return f"MemoryExactPublicationDecision(status={self.status.value!r}, body_free=True, one_shot=True)"

    def _is_process_owned(self) -> bool:
        with _PUBLICATION_LOCK:
            try:
                if type(self.status) is not MemoryExactPublicationStatus:
                    return False
                if type(self._consumed) is not bool:
                    return False
                claim_sha256 = self._claim_sha256
                if claim_sha256 is not None:
                    _sha256(claim_sha256, label="memory publication claim")
                    if not self._consumed:
                        return False
                selection = _sha256(self._selection_handle, label="memory selection handle")
                authority = _sha256(self._authority_handle, label="memory authority handle")
                expected = _keyed_handle(
                    b"friday/memory-exact-publication-decision/v1",
                    {
                        "authority_handle": authority,
                        "claim_sha256": claim_sha256,
                        "consumed": self._consumed,
                        "selection_handle": selection,
                        "status": self.status.value,
                    },
                )
                return hmac.compare_digest(self._seal, expected)
            except (AttributeError, MemoryExactContractError, TypeError, UnicodeError):
                return False

    @property
    def authorized(self) -> bool:
        with _PUBLICATION_LOCK:
            return (
                self._is_process_owned()
                and not self._consumed
                and self.status is MemoryExactPublicationStatus.AUTHORIZED
            )

    def authorizes(self, page: MemoryExactPage) -> bool:
        """Fail closed outside the adapter-owned live publication edge."""

        del page
        return False

    def _claim_live_edge(
        self,
        page: MemoryExactPage,
        *,
        _factory: object = None,
    ) -> str | None:
        """Atomically burn this receipt before the live authority work awaits."""

        with _PUBLICATION_LOCK:
            if (
                _factory is not _DECISION_FACTORY
                or not self._is_process_owned()
                or self._consumed
                or type(page) is not MemoryExactPage
                or not page._is_process_owned()
                or not hmac.compare_digest(self._selection_handle, page.selection_handle)
                or not hmac.compare_digest(self._authority_handle, page.authority_handle)
            ):
                return None
            token = secrets.token_urlsafe(32)
            claim_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
            claimed_seal = _keyed_handle(
                b"friday/memory-exact-publication-decision/v1",
                {
                    "authority_handle": self._authority_handle,
                    "claim_sha256": claim_sha256,
                    "consumed": True,
                    "selection_handle": self._selection_handle,
                    "status": self.status.value,
                },
            )
            object.__setattr__(self, "_claim_sha256", claim_sha256)
            object.__setattr__(self, "_consumed", True)
            object.__setattr__(self, "_seal", claimed_seal)
            return token

    def _finish_live_edge(
        self,
        page: MemoryExactPage,
        *,
        claim_token: str,
        live_authorized: bool,
        _factory: object = None,
    ) -> bool:
        """Finish one claimed edge immediately after its final synchronous check."""

        if type(claim_token) is not str or type(live_authorized) is not bool:
            return False
        try:
            claim_sha256 = hashlib.sha256(claim_token.encode("ascii", errors="strict")).hexdigest()
        except UnicodeEncodeError:
            return False
        with _PUBLICATION_LOCK:
            if (
                _factory is not _DECISION_FACTORY
                or not self._is_process_owned()
                or not self._consumed
                or self._claim_sha256 is None
                or type(page) is not MemoryExactPage
                or not page._is_process_owned()
                or not hmac.compare_digest(self._selection_handle, page.selection_handle)
                or not hmac.compare_digest(self._authority_handle, page.authority_handle)
                or not hmac.compare_digest(
                    self._claim_sha256,
                    claim_sha256,
                )
            ):
                return False
            finished_seal = _keyed_handle(
                b"friday/memory-exact-publication-decision/v1",
                {
                    "authority_handle": self._authority_handle,
                    "claim_sha256": None,
                    "consumed": True,
                    "selection_handle": self._selection_handle,
                    "status": self.status.value,
                },
            )
            object.__setattr__(self, "_claim_sha256", None)
            object.__setattr__(self, "_seal", finished_seal)
            return live_authorized and self.status is MemoryExactPublicationStatus.AUTHORIZED

    def to_public_payload(self) -> dict[str, object]:
        """Return a body-, query-, scope-, identity- and handle-free receipt."""

        with _PUBLICATION_LOCK:
            if not self._is_process_owned():
                raise MemoryExactContractError("memory publication decision integrity failed")
            return {
                "authorized": self.authorized,
                "one_shot": True,
                "schema": MEMORY_EXACT_PUBLICATION_DECISION_SCHEMA,
                "status": self.status.value,
            }


def _create_memory_exact_publication_decision(
    *,
    page: MemoryExactPage,
    status: MemoryExactPublicationStatus,
) -> MemoryExactPublicationDecision:
    """Private late-authorization seam; the decision contains no candidate data."""

    if type(page) is not MemoryExactPage or not page._is_process_owned():
        raise MemoryExactContractError("publication decision requires a process-owned memory page")
    return MemoryExactPublicationDecision(
        status=status,
        selection_handle=page.selection_handle,
        authority_handle=page.authority_handle,
        _factory=_DECISION_FACTORY,
    )


def _claim_memory_exact_publication_decision(
    *,
    decision: MemoryExactPublicationDecision,
    page: MemoryExactPage,
) -> str | None:
    """Private adapter seam that burns a bound receipt before any await."""

    if type(decision) is not MemoryExactPublicationDecision:
        return None
    return decision._claim_live_edge(
        page,
        _factory=_DECISION_FACTORY,
    )


def _finish_memory_exact_publication_decision(
    *,
    decision: MemoryExactPublicationDecision,
    page: MemoryExactPage,
    claim_token: str,
    live_authorized: bool,
) -> bool:
    """Private adapter seam used with no await after final live validation."""

    if type(decision) is not MemoryExactPublicationDecision:
        return False
    return decision._finish_live_edge(
        page,
        claim_token=claim_token,
        live_authorized=live_authorized,
        _factory=_DECISION_FACTORY,
    )


# Storage code naturally speaks about graph values before they become model
# JSON.  Keep these concise spellings as exact aliases; there is still only one
# implementation and every instance enforces the projection-only field set.
MemoryExactGraphNode = MemoryExactGraphNodeProjection
MemoryExactGraphRelation = MemoryExactGraphRelationProjection
MemoryExactGraphPathEdge = MemoryExactGraphEdgeProjection
MemoryExactGraphPath = MemoryExactGraphPathProjection


__all__ = [
    "MEMORY_EXACT_DEFAULT_PAGE_SIZE",
    "MEMORY_EXACT_DEFAULT_SNAPSHOT_LIMIT",
    "MEMORY_EXACT_DEFAULT_TOP_SNAPSHOT",
    "MEMORY_EXACT_GRAPH_PROJECTION_SCHEMA",
    "MEMORY_EXACT_MAX_EXCERPT_CHARS",
    "MEMORY_EXACT_MAX_GRAPH_NODES",
    "MEMORY_EXACT_MAX_GRAPH_PATH_EDGES",
    "MEMORY_EXACT_MAX_GRAPH_PATHS",
    "MEMORY_EXACT_MAX_GRAPH_RELATIONS",
    "MEMORY_EXACT_MAX_PAGE_SIZE",
    "MEMORY_EXACT_MAX_QUERY_CHARS",
    "MEMORY_EXACT_MAX_SNAPSHOT_LIMIT",
    "MEMORY_EXACT_MAX_TOP_SNAPSHOT",
    "MEMORY_EXACT_MODEL_PROJECTION_SCHEMA",
    "MEMORY_EXACT_PUBLICATION_DECISION_SCHEMA",
    "MEMORY_EXACT_REQUEST_IDENTITY_SCHEMA",
    "MEMORY_EXACT_REQUEST_SCHEMA",
    "MemoryExactCandidate",
    "MemoryExactContentCoverage",
    "MemoryExactContinuation",
    "MemoryExactContractError",
    "MemoryExactDateWindowStatus",
    "MemoryExactGraphDirection",
    "MemoryExactGraphEvidenceBasis",
    "MemoryExactGraphCoverage",
    "MemoryExactGraphEdgeProjection",
    "MemoryExactGraphNode",
    "MemoryExactGraphNodeProjection",
    "MemoryExactGraphPath",
    "MemoryExactGraphPathEdge",
    "MemoryExactGraphPathProjection",
    "MemoryExactGraphProjection",
    "MemoryExactGraphRelation",
    "MemoryExactGraphRelationProjection",
    "MemoryExactLifecycleStage",
    "MemoryExactPage",
    "MemoryExactProjection",
    "MemoryExactProjectionRow",
    "MemoryExactPublicationDecision",
    "MemoryExactPublicationStatus",
    "MemoryExactRequest",
    "MemoryExactRowCoverage",
    "MemoryExactTemporalBasis",
    "MemoryExactTemporalStatus",
    "project_memory_exact_page",
]
