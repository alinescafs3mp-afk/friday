"""Read-only document and Knowledge lanes for the archive-search facade.

The caller owns the SQLite connection and its snapshot.  Every query materializes
the exact tenant/owner/privacy/lifecycle scope before counting, matching, ranking,
or limiting.  Returned identifiers and source bodies remain process-private.
"""

from __future__ import annotations

import array
import hashlib
import hmac
import importlib
import json
import math
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final, NoReturn, SupportsIndex

from friday.raw_metadata import RAW_FILE_METADATA_MAX_BYTES
from friday.retrieval.archive_search_contract import (
    MAX_ARCHIVE_MATERIALIZED_CANDIDATES,
    ArchiveEvidenceAuthority,
    ArchiveMatchChannel,
    ArchiveMatchRank,
    ArchiveReviewState,
    ArchiveSearchCandidate,
    ArchiveSearchCorpus,
    ArchiveSearchPassage,
    ArchiveSearchRequest,
    ArchiveTemporalConstraint,
    ReviewScope,
)
from friday.retrieval.archive_search_dense import (
    ArchiveDenseQueryPlan,
    ArchiveDenseQueryProjection,
    project_archive_dense_query_plan,
)
from friday.retrieval.archive_search_document_locator import (
    DOCUMENT_STORED_PASSAGE_INDEX_VERSION,
    LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    CoverageState,
    EmbeddingCompatibility,
    EmbeddingIdentity,
    LifecycleRef,
    LifecycleState,
    PassageRef,
    RepresentationKind,
    ResolvedSource,
    RevalidationTarget,
    RevisionKind,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
    TemporalFact,
    TemporalOrigin,
    TemporalPrecision,
    TemporalRole,
    TemporalValueKind,
    TextSpanLocator,
)
from friday.retrieval.source_focus import (
    MAX_SOURCE_FOCUS_ANCHOR_TERMS,
    SourceFocusMatchKind,
    SourceFocusProjection,
    project_source_focus,
    source_focus_fts_tokens,
)
from friday.storage._knowledge import _fts_term_groups, _fts_terms
from friday.storage._privacy import (
    _not_audio_document,
    _not_private_inbox_dependency,
    _not_private_knowledge_dependency,
    _not_private_raw_dependency,
)

MAX_ARCHIVE_DOCUMENT_RESULTS: Final = 20
_MAX_ARCHIVE_DOCUMENT_MATERIALIZED_RESULTS: Final = MAX_ARCHIVE_MATERIALIZED_CANDIDATES
# Compatibility export for non-sidecar document producers and the Knowledge
# lane.  Only a verified current document-passage child uses the distinct v2
# identity imported above.
PASSAGE_INDEX_VERSION: Final = LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
_DOCUMENT_PASSAGE_MAX_COUNT: Final = 64
_MAX_ACTOR_BYTES = 200
_MAX_SNAPSHOT_BYTES = 256
_MAX_EXCERPT_CHARS = 720
_FOCUSED_DOCUMENT_LEAD_CAP = 100
_FOCUSED_DOCUMENT_BODY_MAX_BYTES = 1 * 1024 * 1024
_FOCUSED_DOCUMENT_BODY_BUDGET_BYTES = 4 * 1024 * 1024
_DENSE_CHUNK_BODY_MAX_BYTES = 1 * 1024 * 1024
_DENSE_CHUNK_BODY_BUDGET_BYTES = 4 * 1024 * 1024
_RAW_ID = re.compile(r"raw_[0-9a-f]{16}\Z")
_KO_ID = re.compile(r"ko_[A-Za-z0-9_-]{8,120}\Z")
_INBOX_ID = re.compile(r"inbox_[0-9a-f]{16}\Z")
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PASSAGE_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,119}\Z")
_SUPPORTED_CORPORA = frozenset({ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE})
_SUPPORTED_LANES = frozenset({SearchLane.CATALOG, SearchLane.LEXICAL, SearchLane.DENSE})
_SEARCH_CORPUS = {
    ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
    ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
}
_MATCH_CHANNEL = {
    SearchLane.CATALOG: ArchiveMatchChannel.CATALOG,
    SearchLane.LEXICAL: ArchiveMatchChannel.LEXICAL,
    SearchLane.DENSE: ArchiveMatchChannel.DENSE,
}
_PAGE_KEY = secrets.token_bytes(32)
_PAGE_PROCESS_AUTHORITY = object()
_REPLAY_FACTORY = object()

_SUPPORTED_TEMPORAL_ROLES = {
    ArchiveSearchCorpus.DOCUMENTS: frozenset(
        # ``raw_objects`` records receipt, not a separately attested upload
        # instant.  Treating the two roles as aliases would silently substitute
        # temporal meaning, so UPLOADED_AT remains explicitly unavailable.
        {
            TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE,
            TemporalRole.RECEIVED_AT,
        }
    ),
    ArchiveSearchCorpus.KNOWLEDGE: frozenset(
        {
            TemporalRole.RECEIVED_AT,
            TemporalRole.KNOWLEDGE_PROJECTION_CREATED_AT,
            TemporalRole.KNOWLEDGE_PROJECTION_MODIFIED_AT,
        }
    ),
}


class ArchiveDocumentStorageError(RuntimeError):
    """Body-free failure at the read-only document storage seam."""


_PassageRows = tuple[tuple[int, int, int, str], ...]


@dataclass(frozen=True, slots=True)
class _DocumentPassageContract:
    index_revision: str
    set_sha256: Callable[[_PassageRows], str]
    rows_match_current_projection: Callable[[object, object, object], bool]


@dataclass(frozen=True, slots=True)
class _StoredDocumentPassage:
    chunk_index: int
    start_char: int
    end_char: int
    content_sha256: str


class ArchiveDocumentReplaySource:
    """Exact authorized body and source snapshot for durable evidence replay."""

    __slots__ = ("_stored_passages", "body", "corpus", "resolved_source")

    corpus: ArchiveSearchCorpus
    resolved_source: ResolvedSource
    body: str
    _stored_passages: tuple[_StoredDocumentPassage, ...] | None

    def __init__(
        self,
        corpus: ArchiveSearchCorpus,
        resolved_source: ResolvedSource,
        body: str,
        stored_passages: tuple[_StoredDocumentPassage, ...] | None,
        *,
        _factory: object = None,
    ) -> None:
        if (
            _factory is not _REPLAY_FACTORY
            or corpus not in _SUPPORTED_CORPORA
            or type(resolved_source) is not ResolvedSource
            or type(body) is not str
            or (
                stored_passages is not None
                and (
                    type(stored_passages) is not tuple
                    or not stored_passages
                    or any(type(item) is not _StoredDocumentPassage for item in stored_passages)
                )
            )
            or (corpus is ArchiveSearchCorpus.KNOWLEDGE and stored_passages is not None)
        ):
            raise _fail("archive document replay source is invalid")
        try:
            body.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise _fail("archive document replay source is invalid") from None
        object.__setattr__(self, "corpus", corpus)
        object.__setattr__(self, "resolved_source", resolved_source)
        object.__setattr__(self, "body", body)
        object.__setattr__(self, "_stored_passages", stored_passages)

    def __setattr__(self, _name: str, _value: object) -> NoReturn:
        raise TypeError("archive document replay source is immutable")

    def __repr__(self) -> str:
        return f"ArchiveDocumentReplaySource(corpus={self.corpus.value!r}, private=True)"

    def __copy__(self) -> NoReturn:
        raise _fail("archive document replay source cannot be copied")

    def __deepcopy__(self, _memo: object) -> NoReturn:
        raise _fail("archive document replay source cannot be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise _fail("archive document replay source cannot be serialized")

    def stored_passage_text(self, locator: TextSpanLocator) -> str | None:
        """Resolve one v2 locator only through its exact persisted child."""

        if type(locator) is not TextSpanLocator or self._stored_passages is None:
            return None
        passage = next(
            (item for item in self._stored_passages if item.chunk_index == locator.chunk_index),
            None,
        )
        if (
            passage is None
            or not passage.start_char <= locator.start_char < locator.end_char <= passage.end_char
        ):
            return None
        return self.body[locator.start_char : locator.end_char]


def _fail(message: str) -> ArchiveDocumentStorageError:
    return ArchiveDocumentStorageError(message)


def _actor(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _fail("archive document authority is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _fail("archive document authority is invalid") from None
    if len(encoded) > _MAX_ACTOR_BYTES or any(unicodedata.category(char).startswith("C") for char in value):
        raise _fail("archive document authority is invalid")
    return value


def _snapshot(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _fail("archive document snapshot binding is invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _fail("archive document snapshot binding is invalid") from None
    if len(encoded) > _MAX_SNAPSHOT_BYTES or any(
        unicodedata.category(char).startswith("C") for char in value
    ):
        raise _fail("archive document snapshot binding is invalid")
    return value


def _limit(value: object, *, maximum: int = MAX_ARCHIVE_DOCUMENT_RESULTS) -> int:
    if (
        maximum
        not in {
            MAX_ARCHIVE_DOCUMENT_RESULTS,
            _MAX_ARCHIVE_DOCUMENT_MATERIALIZED_RESULTS,
        }
        or type(value) is not int
        or not 1 <= value <= maximum
    ):
        raise _fail("archive document page limit is invalid")
    return value


def _safe_display(value: object) -> str | None:
    if type(value) is not str:
        return None
    text = value.strip()
    if (
        not text
        or text != value
        or len(text) > 260
        or any(unicodedata.category(char).startswith("C") for char in text)
    ):
        return None
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return None
    return text


def _row(cursor: sqlite3.Cursor, value: sqlite3.Row | tuple[object, ...]) -> dict[str, Any]:
    columns = tuple(str(item[0]) for item in (cursor.description or ()))
    return dict(zip(columns, tuple(value), strict=True))


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            is not None
        )
    except sqlite3.Error:
        raise _fail("archive document storage is unavailable") from None


def _document_catalog_contract(conn: sqlite3.Connection) -> tuple[bool, int]:
    """Authenticate the optional schema-41 sidecar without trusting its name.

    Schema 40 does not ship the document-catalog package.  Once schema 41 is
    present, its fingerprint helper authenticates the complete table contour;
    absent, partial, or counterfeit objects remain a coverage miss and are
    never queried.
    """

    try:
        schema = importlib.import_module("friday.document_catalog.schema")
        fingerprint_reader = schema.document_catalog_schema_fingerprint
        enrichment_revision = schema.DOCUMENT_CATALOG_ENRICHMENT_REVISION
        fingerprint = fingerprint_reader(conn)
    except (ImportError, AttributeError, TypeError, RuntimeError, ValueError, sqlite3.Error):
        return False, 0
    if (
        type(enrichment_revision) is not int
        or enrichment_revision < 1
        or type(fingerprint) is not str
        or _SHA256.fullmatch(fingerprint) is None
    ):
        return False, 0
    return True, enrichment_revision


def _load_document_passage_contract(
    conn: sqlite3.Connection,
) -> _DocumentPassageContract | None:
    """Authenticate the optional reader-first passage sidecar.

    A schema-46 database legitimately has no passage projection.  Missing,
    partial, counterfeit, or policy-drifted schema therefore selects the
    existing authorized whole-body fallback instead of being queried.
    """

    try:
        schema = importlib.import_module("friday.document_catalog.passage_schema")
        projection = importlib.import_module("friday.document_catalog.passage_projection")
        fingerprint_reader = schema.document_passage_schema_fingerprint
        index_revision = schema.DOCUMENT_PASSAGE_INDEX_REVISION
        projection_revision = projection.DOCUMENT_PASSAGE_INDEX_REVISION
        set_sha256 = schema.document_passage_set_sha256
        rows_match_current_projection = schema.document_passage_rows_match_current_projection
        fingerprint = fingerprint_reader(conn)
    except (ImportError, AttributeError, TypeError, RuntimeError, ValueError, sqlite3.Error):
        return None
    if (
        type(index_revision) is not str
        or _PASSAGE_REVISION.fullmatch(index_revision) is None
        or index_revision != projection_revision
        or not callable(set_sha256)
        or not callable(rows_match_current_projection)
        or type(fingerprint) is not str
        or _SHA256.fullmatch(fingerprint) is None
    ):
        return None
    return _DocumentPassageContract(
        index_revision,
        set_sha256,
        rows_match_current_projection,
    )


def _document_passage_contract(conn: sqlite3.Connection) -> bool:
    """Compatibility probe used by focused reader contract tests."""

    return _load_document_passage_contract(conn) is not None


def _ensure_archive_search_fold(conn: sqlite3.Connection) -> None:
    """Install the deterministic fold once without re-registering mid-snapshot."""

    probe = "Ёж-Archive-Probe"
    expected = _archive_search_fold(probe)
    try:
        cursor = conn.execute("SELECT friday_archive_fold(?)", (probe,))
        row = cursor.fetchone()
        cursor.close()
    except sqlite3.Error:
        try:
            conn.create_function(
                "friday_archive_fold",
                1,
                _archive_search_fold,
                deterministic=True,
            )
            cursor = conn.execute("SELECT friday_archive_fold(?)", (probe,))
            row = cursor.fetchone()
            cursor.close()
        except sqlite3.Error:
            raise _fail("archive document lexical fold is unavailable") from None
    if row is None or len(row) != 1 or type(row[0]) is not str or row[0] != expected:
        raise _fail("archive document lexical fold is unavailable")


def _ensure_archive_catalog_title_validator(conn: sqlite3.Connection) -> None:
    title_probes = ("Archive title", "unsafe\tarchive title")
    try:
        cursor = conn.execute(
            "SELECT friday_archive_catalog_title_valid(?), friday_archive_catalog_title_valid(?)",
            title_probes,
        )
        title_row = cursor.fetchone()
        cursor.close()
    except sqlite3.Error:
        try:
            conn.create_function(
                "friday_archive_catalog_title_valid",
                1,
                _archive_catalog_title_valid,
                deterministic=True,
            )
            cursor = conn.execute(
                "SELECT friday_archive_catalog_title_valid(?), friday_archive_catalog_title_valid(?)",
                title_probes,
            )
            title_row = cursor.fetchone()
            cursor.close()
        except sqlite3.Error:
            raise _fail("archive document catalog title validation is unavailable") from None
    if title_row is None or tuple(title_row) != (1, 0):
        raise _fail("archive document catalog title validation is unavailable")


def _authority_is_active(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
) -> bool:
    try:
        row = conn.execute(
            """SELECT EXISTS (
                       SELECT 1 FROM users WHERE id=? AND status='active'
                   ), EXISTS (
                       SELECT 1 FROM users WHERE id=? AND status='active'
                   )""",
            (tenant_id, owner_id),
        ).fetchone()
    except sqlite3.Error:
        raise _fail("archive document authority is unavailable") from None
    if row is None or len(row) != 2 or any(type(item) is not int for item in row):
        raise _fail("archive document authority is unavailable")
    return tuple(row) == (1, 1)


def _lifecycle_states(request: ArchiveSearchRequest, corpus: ArchiveSearchCorpus) -> tuple[str, ...]:
    for constraint in request.lifecycle_constraints:
        if constraint.corpus is corpus:
            return tuple(item.value for item in constraint.states)
    return ()


def _temporal_constraints(
    request: ArchiveSearchRequest,
    corpus: ArchiveSearchCorpus,
) -> tuple[ArchiveTemporalConstraint, ...]:
    return tuple(item for item in request.temporal_constraints if item.corpus is corpus)


def _temporal_supported(
    request: ArchiveSearchRequest,
    corpus: ArchiveSearchCorpus,
) -> bool:
    supported = _SUPPORTED_TEMPORAL_ROLES[corpus]
    for item in _temporal_constraints(request, corpus):
        if item.role not in supported:
            return False
        if item.role is TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE:
            if (
                item.value_kind is not TemporalValueKind.DATE_INTERVAL
                or item.precision is not TemporalPrecision.DAY
            ):
                return False
        elif (
            item.value_kind is not TemporalValueKind.INSTANT
            or item.precision is not TemporalPrecision.INSTANT
        ):
            return False
    return True


def _canonical_utc_sql(expression: str) -> str:
    """Recognize the exact UTC text form used by the temporal contract.

    SQLite's date functions round sub-millisecond instants.  They are suitable
    for shape validation here, but the actual half-open comparison below stays
    textual so a microsecond boundary cannot admit an out-of-range source.
    """

    return f"""(
        typeof({expression})='text'
        AND julianday({expression}) IS NOT NULL
        AND substr({expression},5,1)='-'
        AND substr({expression},8,1)='-'
        AND substr({expression},11,1)='T'
        AND substr({expression},14,1)=':'
        AND substr({expression},17,1)=':'
        AND substr({expression},1,4) NOT GLOB '*[^0-9]*'
        AND substr({expression},6,2) NOT GLOB '*[^0-9]*'
        AND substr({expression},9,2) NOT GLOB '*[^0-9]*'
        AND substr({expression},12,2) NOT GLOB '*[^0-9]*'
        AND substr({expression},15,2) NOT GLOB '*[^0-9]*'
        AND substr({expression},18,2) NOT GLOB '*[^0-9]*'
        AND date(substr({expression},1,10))=substr({expression},1,10)
        AND CAST(substr({expression},12,2) AS INTEGER) BETWEEN 0 AND 23
        AND CAST(substr({expression},15,2) AS INTEGER) BETWEEN 0 AND 59
        AND CAST(substr({expression},18,2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%S',{expression})=substr({expression},1,19)
        AND (
            (length({expression})=25 AND substr({expression},20,6)='+00:00')
            OR (
                length({expression})=32
                AND substr({expression},20,1)='.'
                AND substr({expression},21,6) NOT GLOB '*[^0-9]*'
                AND substr({expression},21,6)<>'000000'
                AND substr({expression},27,6)='+00:00'
            )
        )
    )"""


def _canonical_date_sql(expression: str) -> str:
    """Recognize one exact ISO calendar date without temporal substitution."""

    return f"""(
        typeof({expression})='text'
        AND length({expression})=10
        AND substr({expression},5,1)='-'
        AND substr({expression},8,1)='-'
        AND substr({expression},1,4) NOT GLOB '*[^0-9]*'
        AND substr({expression},6,2) NOT GLOB '*[^0-9]*'
        AND substr({expression},9,2) NOT GLOB '*[^0-9]*'
        AND date({expression})={expression}
    )"""


def _archive_search_fold(value: object) -> str:
    """Mirror the archive lexical fold, including FTS unicode61 diacritics."""

    if type(value) is not str:
        return ""
    decomposed = unicodedata.normalize("NFD", value.casefold())
    return unicodedata.normalize(
        "NFC",
        "".join(character for character in decomposed if not unicodedata.combining(character)),
    )


def _archive_catalog_title_valid(value: object) -> int:
    if type(value) is not str:
        return 0
    return int(not any(unicodedata.category(character).startswith("C") for character in value))


def _scope_suffix(
    request: ArchiveSearchRequest,
    corpus: ArchiveSearchCorpus,
    *,
    lifecycle_expression: str,
    review_expression: str,
    temporal_expressions: dict[TemporalRole, str],
    include_unknown: bool = False,
) -> tuple[str, tuple[object, ...]]:
    clauses: list[str] = []
    parameters: list[object] = []
    states = _lifecycle_states(request, corpus)
    if states:
        placeholders = ",".join("?" for _item in states)
        condition = f"{lifecycle_expression} IN ({placeholders})"
        clauses.append(f"({lifecycle_expression} IS NULL OR {condition})" if include_unknown else condition)
        parameters.extend(states)
    if request.review_scope is ReviewScope.CONFIRMED_ONLY:
        condition = f"{review_expression}='confirmed'"
        clauses.append(f"({review_expression} IS NULL OR {condition})" if include_unknown else condition)
    for constraint in _temporal_constraints(request, corpus):
        expression = temporal_expressions.get(constraint.role)
        if expression is None:
            raise _fail("archive document temporal role is unavailable")
        ready = _temporal_ready_sql(constraint, expression)
        clauses.append(f"(NOT {ready} OR ({expression}>=? AND {expression}<?))")
        parameters.extend((constraint.start, constraint.end))
    return ("" if not clauses else " AND " + " AND ".join(clauses), tuple(parameters))


def _temporal_ready_sql(constraint: ArchiveTemporalConstraint, expression: str) -> str:
    if constraint.role is TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE:
        return _canonical_date_sql(expression)
    return _canonical_utc_sql(expression)


def _temporal_ready_expression(
    request: ArchiveSearchRequest,
    corpus: ArchiveSearchCorpus,
    temporal_expressions: dict[TemporalRole, str],
) -> str:
    constraints = _temporal_constraints(request, corpus)
    if not constraints:
        return "1"
    return " AND ".join(_temporal_ready_sql(item, temporal_expressions[item.role]) for item in constraints)


def _actor_handle(tenant_id: str, owner_id: str) -> bytes:
    material = json.dumps([tenant_id, owner_id], ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return hmac.digest(_PAGE_KEY, b"friday/archive-document-actor/v1\0" + material, "sha256")


def _request_handle(request: ArchiveSearchRequest) -> bytes:
    try:
        material = request.to_identity_json().encode("ascii", errors="strict")
    except Exception:
        raise _fail("archive document request binding is invalid") from None
    return hmac.digest(
        _PAGE_KEY,
        b"friday/archive-document-request/v1\0" + material,
        "sha256",
    )


def _snapshot_handle(snapshot_discriminator: str) -> bytes:
    return hmac.digest(
        _PAGE_KEY,
        b"friday/archive-document-snapshot/v1\0" + snapshot_discriminator.encode("utf-8", errors="strict"),
        "sha256",
    )


def _page_seal_material(page: ArchiveDocumentLanePage) -> bytes:
    candidate_digests = [
        hashlib.sha256(item.to_private_json().encode("ascii")).hexdigest() for item in page.candidates
    ]
    payload = {
        "actor": page._actor_handle.hex(),
        "authority_scope_complete": page.authority_scope_complete,
        "authority_rechecked": page.authority_rechecked,
        "available": page.available,
        "applied_limit": page.applied_limit,
        "binding": page._execution_handle,
        "catalog_projection_current": page.catalog_projection_current,
        "candidates": candidate_digests,
        "corpus": page.corpus.value,
        "derivative_current": page.derivative_current,
        "derivative_backfill_pending": page.derivative_backfill_pending,
        "derivative_unavailable": page.derivative_unavailable,
        "examined": page.examined,
        "has_more": page.has_more,
        "lane": page.lane.value,
        "matched": page.matched,
        "request_handle": page._request_handle.hex(),
        "returned": page.returned,
        "snapshot_current": page.snapshot_current,
        "snapshot_handle": page._snapshot_handle.hex(),
        "total": page.total,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ArchiveDocumentLanePage:
    corpus: ArchiveSearchCorpus
    lane: SearchLane
    candidates: tuple[ArchiveSearchCandidate, ...]
    total: int | None
    examined: int
    matched: int
    returned: int
    has_more: bool
    available: bool
    applied_limit: int
    derivative_current: bool | None
    derivative_backfill_pending: bool | None
    derivative_unavailable: bool | None
    catalog_projection_current: bool | None
    authority_scope_complete: bool
    authority_rechecked: bool
    snapshot_current: bool
    _execution_handle: str
    _request_handle: bytes
    _actor_handle: bytes
    _snapshot_handle: bytes
    _seal: bytes
    _process_authority: object

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise _fail("archive document page requires its private factory")

    def __post_init__(self) -> None:
        if type(self.corpus) is not ArchiveSearchCorpus or type(self.lane) is not SearchLane:
            raise _fail("archive document page target is invalid")
        if self.corpus not in _SUPPORTED_CORPORA or self.lane not in _SUPPORTED_LANES:
            raise _fail("archive document page target is invalid")
        if type(self.candidates) is not tuple or any(
            type(item) is not ArchiveSearchCandidate
            or item.corpus is not self.corpus
            or item.match_channels != (_MATCH_CHANNEL[self.lane],)
            for item in self.candidates
        ):
            raise _fail("archive document page candidates are invalid")
        counts = (self.examined, self.matched, self.returned)
        if any(type(item) is not int or item < 0 for item in counts):
            raise _fail("archive document page counts are invalid")
        if self.total is not None and (type(self.total) is not int or self.total < 0):
            raise _fail("archive document page total is invalid")
        if (
            (self.total is not None and self.examined > self.total)
            or self.matched > self.examined
            or self.returned > self.matched
            or self.returned != len(self.candidates)
            or type(self.has_more) is not bool
            or type(self.available) is not bool
            or type(self.applied_limit) is not int
            or not 1 <= self.applied_limit <= _MAX_ARCHIVE_DOCUMENT_MATERIALIZED_RESULTS
            or self.returned > self.applied_limit
            or type(self.authority_scope_complete) is not bool
            or type(self.authority_rechecked) is not bool
            or type(self.snapshot_current) is not bool
            or (self.derivative_current is not None and type(self.derivative_current) is not bool)
            or (
                self.derivative_backfill_pending is not None
                and type(self.derivative_backfill_pending) is not bool
            )
            or (self.derivative_unavailable is not None and type(self.derivative_unavailable) is not bool)
            or (
                self.catalog_projection_current is not None
                and type(self.catalog_projection_current) is not bool
            )
        ):
            raise _fail("archive document page is inconsistent")
        if self.lane is SearchLane.CATALOG and any(
            item is not None
            for item in (
                self.derivative_current,
                self.derivative_backfill_pending,
                self.derivative_unavailable,
            )
        ):
            raise _fail("archive catalog page cannot claim derivative health")
        if self.lane in {SearchLane.LEXICAL, SearchLane.DENSE}:
            if any(
                type(item) is not bool
                for item in (
                    self.derivative_current,
                    self.derivative_backfill_pending,
                    self.derivative_unavailable,
                )
            ):
                raise _fail("archive derivative lane requires derivative health")
            if self.derivative_current is (
                bool(self.derivative_backfill_pending) or bool(self.derivative_unavailable)
            ):
                raise _fail("archive derivative health is inconsistent")
        document_catalog_page = (
            self.corpus is ArchiveSearchCorpus.DOCUMENTS and self.lane is SearchLane.CATALOG
        )
        if self.available and document_catalog_page:
            if type(self.catalog_projection_current) is not bool:
                raise _fail("archive document catalog projection health is invalid")
        elif self.catalog_projection_current is not None:
            raise _fail("archive document catalog projection health is invalid")
        if not self.available and (
            self.total is not None or self.examined or self.matched or self.returned or self.has_more
        ):
            raise _fail("unavailable archive document page claims search work")
        if self.available and (
            not self.authority_rechecked or self.authority_scope_complete is (self.total is None)
        ):
            raise _fail("available archive document page lacks authority evidence")
        if (
            type(self._execution_handle) is not str
            or _SHA256.fullmatch(self._execution_handle) is None
            or type(self._request_handle) is not bytes
            or len(self._request_handle) != hashlib.sha256().digest_size
            or type(self._actor_handle) is not bytes
            or len(self._actor_handle) != hashlib.sha256().digest_size
            or type(self._snapshot_handle) is not bytes
            or len(self._snapshot_handle) != hashlib.sha256().digest_size
            or self._process_authority is not _PAGE_PROCESS_AUTHORITY
            or type(self._seal) is not bytes
            or not hmac.compare_digest(
                self._seal,
                hmac.digest(_PAGE_KEY, _page_seal_material(self), "sha256"),
            )
        ):
            raise _fail("archive document page binding is invalid")

    def __repr__(self) -> str:
        corpus = self.corpus.value if type(self.corpus) is ArchiveSearchCorpus else "invalid"
        lane = self.lane.value if type(self.lane) is SearchLane else "invalid"
        returned = self.returned if type(self.returned) is int else "invalid"
        has_more = self.has_more if type(self.has_more) is bool else "invalid"
        available = self.available if type(self.available) is bool else "invalid"
        return (
            "ArchiveDocumentLanePage("
            f"corpus={corpus!r}, lane={lane!r}, returned={returned!r}, "
            f"has_more={has_more!r}, available={available!r})"
        )

    def __copy__(self) -> ArchiveDocumentLanePage:
        raise _fail("archive document page cannot be copied")

    def __deepcopy__(self, _memo: object) -> ArchiveDocumentLanePage:
        raise _fail("archive document page cannot be copied")

    def __reduce_ex__(self, _protocol: SupportsIndex) -> NoReturn:
        raise _fail("archive document page cannot be serialized")

    def to_coverage(
        self,
        *,
        execution_binding: SearchExecutionBinding,
        tenant_id: str,
        owner_id: str,
        request: ArchiveSearchRequest,
        snapshot_discriminator: str,
    ) -> SearchCoverage:
        try:
            self.__post_init__()
            tenant = _actor(tenant_id)
            owner = _actor(owner_id)
            snapshot = _snapshot(snapshot_discriminator)
            target = (_SEARCH_CORPUS[self.corpus], self.lane)
            if (
                type(request) is not ArchiveSearchRequest
                or type(execution_binding) is not SearchExecutionBinding
                or not execution_binding.is_live_private_request_binding
                or execution_binding.authority_scope is not AuthorityScope.TENANT_PRINCIPAL
                or target not in execution_binding.requested_targets
                or not execution_binding.attests_private_request(request.to_identity_json())
                or not execution_binding.attests_authority(
                    authority_scope=AuthorityScope.TENANT_PRINCIPAL,
                    tenant_id=tenant,
                    principal_id=owner,
                )
                or not execution_binding.attests_snapshot(snapshot)
                or not hmac.compare_digest(
                    execution_binding.opaque_handle,
                    self._execution_handle,
                )
                or not hmac.compare_digest(
                    self._request_handle,
                    _request_handle(request),
                )
                or not hmac.compare_digest(
                    self._actor_handle,
                    _actor_handle(tenant, owner),
                )
                or not hmac.compare_digest(
                    self._snapshot_handle,
                    _snapshot_handle(snapshot),
                )
            ):
                raise _fail("archive document coverage binding is invalid")
        except ArchiveDocumentStorageError:
            raise
        except Exception:
            raise _fail("archive document coverage binding is invalid") from None
        target = (_SEARCH_CORPUS[self.corpus], self.lane)
        states: tuple[CoverageState, ...]
        if not self.available:
            states = (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)
        else:
            incomplete: set[CoverageState] = set()
            if (
                not self.authority_scope_complete
                or (self.total is not None and self.examined < self.total)
                or self.catalog_projection_current is False
            ):
                incomplete.add(CoverageState.BACKFILL_PENDING)
            if self.lane in {SearchLane.LEXICAL, SearchLane.DENSE}:
                if self.derivative_backfill_pending:
                    incomplete.add(CoverageState.BACKFILL_PENDING)
                if self.derivative_unavailable:
                    incomplete.add(CoverageState.UNAVAILABLE)
            if self.has_more:
                incomplete.add(CoverageState.CAPPED)
            states = (
                tuple(sorted({CoverageState.PARTIAL, *incomplete}, key=lambda item: item.value))
                if incomplete
                else (CoverageState.COMPLETE,)
            )
        return SearchCoverage.create(
            corpus=target[0],
            lane=target[1],
            execution_binding=execution_binding,
            states=states,
            eligible_authorized=self.total,
            examined=self.examined,
            matched_at_least=self.matched,
            returned=self.returned,
            limit=self.applied_limit if self.has_more else None,
            next_cursor_available=False,
            authority_rechecked=self.authority_rechecked,
            snapshot_current=self.snapshot_current,
        )


def _new_page(
    *,
    corpus: ArchiveSearchCorpus,
    lane: SearchLane,
    candidates: tuple[ArchiveSearchCandidate, ...],
    total: int | None,
    examined: int,
    matched: int,
    has_more: bool,
    available: bool,
    applied_limit: int,
    derivative_current: bool | None,
    derivative_backfill_pending: bool | None,
    derivative_unavailable: bool | None,
    catalog_projection_current: bool | None,
    authority_scope_complete: bool,
    authority_rechecked: bool,
    snapshot_current: bool,
    execution_binding: SearchExecutionBinding,
    request: ArchiveSearchRequest,
    tenant_id: str,
    owner_id: str,
    snapshot_discriminator: str,
) -> ArchiveDocumentLanePage:
    target = (_SEARCH_CORPUS[corpus], lane)
    if (
        type(execution_binding) is not SearchExecutionBinding
        or not execution_binding.is_live_private_request_binding
        or execution_binding.authority_scope is not AuthorityScope.TENANT_PRINCIPAL
        or target not in execution_binding.requested_targets
        or not execution_binding.attests_private_request(request.to_identity_json())
        or not execution_binding.attests_authority(
            authority_scope=AuthorityScope.TENANT_PRINCIPAL,
            tenant_id=tenant_id,
            principal_id=owner_id,
        )
        or not execution_binding.attests_snapshot(snapshot_discriminator)
    ):
        raise _fail("archive document page binding is invalid")
    value = object.__new__(ArchiveDocumentLanePage)
    fields: tuple[tuple[str, object], ...] = (
        ("corpus", corpus),
        ("lane", lane),
        ("candidates", candidates),
        ("total", total),
        ("examined", examined),
        ("matched", matched),
        ("returned", len(candidates)),
        ("has_more", has_more),
        ("available", available),
        ("applied_limit", applied_limit),
        ("derivative_current", derivative_current),
        ("derivative_backfill_pending", derivative_backfill_pending),
        ("derivative_unavailable", derivative_unavailable),
        ("catalog_projection_current", catalog_projection_current),
        ("authority_scope_complete", authority_scope_complete),
        ("authority_rechecked", authority_rechecked),
        ("snapshot_current", snapshot_current),
        ("_execution_handle", execution_binding.opaque_handle),
        ("_request_handle", _request_handle(request)),
        ("_actor_handle", _actor_handle(tenant_id, owner_id)),
        ("_snapshot_handle", _snapshot_handle(snapshot_discriminator)),
        ("_seal", b""),
        ("_process_authority", _PAGE_PROCESS_AUTHORITY),
    )
    for field, item in fields:
        object.__setattr__(value, field, item)
    object.__setattr__(
        value,
        "_seal",
        hmac.digest(_PAGE_KEY, _page_seal_material(value), "sha256"),
    )
    value.__post_init__()
    return value


def _unavailable_page(
    corpus: ArchiveSearchCorpus,
    lane: SearchLane,
    *,
    execution_binding: SearchExecutionBinding,
    request: ArchiveSearchRequest,
    tenant_id: str,
    owner_id: str,
    snapshot_discriminator: str,
    snapshot_current: bool,
    authority_rechecked: bool,
    applied_limit: int,
) -> ArchiveDocumentLanePage:
    return _new_page(
        corpus=corpus,
        lane=lane,
        candidates=(),
        total=None,
        examined=0,
        matched=0,
        has_more=False,
        available=False,
        applied_limit=applied_limit,
        derivative_current=False if lane in {SearchLane.LEXICAL, SearchLane.DENSE} else None,
        derivative_backfill_pending=False if lane in {SearchLane.LEXICAL, SearchLane.DENSE} else None,
        derivative_unavailable=True if lane in {SearchLane.LEXICAL, SearchLane.DENSE} else None,
        catalog_projection_current=None,
        authority_scope_complete=False,
        authority_rechecked=authority_rechecked,
        snapshot_current=snapshot_current,
        execution_binding=execution_binding,
        request=request,
        tenant_id=tenant_id,
        owner_id=owner_id,
        snapshot_discriminator=snapshot_discriminator,
    )


def _safe_raw_metadata(alias: str = "r") -> str:
    value = f"{alias}.metadata_json"
    return (
        f"(CASE WHEN typeof({value})='text' "
        f"AND length(CAST({value} AS BLOB))<={RAW_FILE_METADATA_MAX_BYTES} "
        f"AND json_valid({value}) AND json_type({value})='object' "
        f"THEN {value} ELSE '{{}}' END)"
    )


def _raw_metadata_shape(alias: str = "r") -> str:
    value = f"{alias}.metadata_json"
    return f"""(
        typeof({value})='text'
        AND length(CAST({value} AS BLOB))<={RAW_FILE_METADATA_MAX_BYTES}
        AND json_valid({value})
        AND json_type({value})='object'
    )"""


def _mime_value_valid(expression: str) -> str:
    """Accept the same closed concrete media-type grammar as file delivery."""

    return f"""(
        length({expression}) BETWEEN 3 AND 200
        AND {expression} NOT GLOB '*[^A-Za-z0-9!#$&^_.+/-]*'
        AND instr({expression},'/') BETWEEN 2 AND length({expression})-1
        AND instr(substr({expression},instr({expression},'/')+1),'/')=0
    )"""


def _format_metadata_valid(alias: str = "r") -> str:
    """Attest optional MIME aliases without accepting ambiguous navigation."""

    metadata = _safe_raw_metadata(alias)
    mime_type = f"json_extract({metadata},'$.mime_type')"
    legacy_mime = f"json_extract({metadata},'$.mime')"

    def valid_if_present(path: str, expression: str) -> str:
        kind = f"json_type({metadata},'{path}')"
        return f"""(
            {kind} IS NULL OR {kind}='null' OR (
                {kind}='text'
                AND {_mime_value_valid(expression)}
            )
        )"""

    return f"""(
        {valid_if_present("$.mime_type", mime_type)}
        AND {valid_if_present("$.mime", legacy_mime)}
        AND (
            COALESCE(json_type({metadata},'$.mime_type'),'')<>'text'
            OR COALESCE(json_type({metadata},'$.mime'),'')<>'text'
            OR lower({mime_type})=lower({legacy_mime})
        )
    )"""


def _catalog_metadata_valid(
    alias: str = "r",
    *,
    require_format_ready: bool = False,
) -> str:
    """Attest fields that decide document classification and navigation."""

    metadata = _safe_raw_metadata(alias)
    keys = ("filename", "mime_type", "mime", "content_type", "media_kind")
    quoted = ",".join(f"'{key}'" for key in keys)
    shape = " AND ".join(
        f"(json_type({metadata},'$.{key}') IS NULL OR json_type({metadata},'$.{key}') IN ('text','null'))"
        for key in keys
    )
    format_ready = _format_metadata_valid(alias) if require_format_ready else "1"
    return f"""(
        NOT EXISTS (
            SELECT 1 FROM json_each({metadata}) catalog_member
             WHERE catalog_member.key IN ({quoted})
             GROUP BY CAST(catalog_member.key AS TEXT)
            HAVING COUNT(*)>1
        )
        AND {shape}
        AND {format_ready}
    )"""


def _document_date_expression(alias: str = "r") -> str:
    """Return one parser-owned legacy date, or SQL NULL for ambiguous input."""

    metadata = _safe_raw_metadata(alias)
    value = f"json_extract({metadata},'$.document_date')"
    return f"""CASE
        WHEN NOT EXISTS (
                 SELECT 1 FROM json_each({metadata}) document_date_member
                  WHERE document_date_member.key='document_date'
                  GROUP BY CAST(document_date_member.key AS TEXT)
                 HAVING COUNT(*)>1
             )
         AND json_type({metadata},'$.document_date')='text'
         AND jericho_iso_date({value}) IS NOT NULL
        THEN jericho_iso_date({value}) ELSE NULL END"""


def _principal_raw_authority(
    corpus: ArchiveSearchCorpus,
    *,
    alias: str = "r",
) -> str:
    metadata = _safe_raw_metadata(alias)
    uploaded = f"json_extract({metadata},'$.uploaded_by')"
    requested = f"json_extract({metadata},'$.requested_by')"
    generated_for = f"json_extract({metadata},'$.generated_for')"
    duplicate_guard = f"""NOT EXISTS (
        SELECT 1 FROM json_tree({metadata}) owner_member
         WHERE owner_member.key IN ('uploaded_by','requested_by','generated_for')
         GROUP BY owner_member.parent, CAST(owner_member.key AS TEXT)
        HAVING COUNT(*)>1
    )"""
    exact_owner = f"""(
        {_raw_metadata_shape(alias)}
        AND {duplicate_guard}
        AND (json_type({metadata},'$.uploaded_by') IS NULL
             OR json_type({metadata},'$.uploaded_by')='null'
             OR (json_type({metadata},'$.uploaded_by')='text'
                 AND {uploaded}=principal_authority.owner_id))
        AND (json_type({metadata},'$.requested_by') IS NULL
             OR json_type({metadata},'$.requested_by')='null'
             OR (json_type({metadata},'$.requested_by')='text'
                 AND {requested}=principal_authority.owner_id))
        AND (json_type({metadata},'$.generated_for') IS NULL
             OR json_type({metadata},'$.generated_for')='null'
             OR (json_type({metadata},'$.generated_for')='text'
                 AND {generated_for}=principal_authority.owner_id))
    )"""
    if corpus is ArchiveSearchCorpus.DOCUMENTS:
        return f"""(
            {exact_owner}
            AND json_type({metadata},'$.uploaded_by')='text'
            AND {uploaded}=principal_authority.owner_id
        )"""
    return f"""(
        {exact_owner}
        AND (
            (json_type({metadata},'$.uploaded_by')='text'
             AND {uploaded}=principal_authority.owner_id)
            OR (json_type({metadata},'$.requested_by')='text'
                AND {requested}=principal_authority.owner_id)
            OR (json_type({metadata},'$.generated_for')='text'
                AND {generated_for}=principal_authority.owner_id)
            OR (
                {alias}.user_id=principal_authority.owner_id
                AND COALESCE(json_type({metadata},'$.uploaded_by'),'null')='null'
                AND COALESCE(json_type({metadata},'$.requested_by'),'null')='null'
                AND COALESCE(json_type({metadata},'$.generated_for'),'null')='null'
            )
        )
    )"""


def _raw_owner_attribution_valid(
    corpus: ArchiveSearchCorpus,
    *,
    alias: str = "r",
) -> str:
    metadata = _safe_raw_metadata(alias)
    fields = tuple(
        f"json_extract({metadata},'$.{name}')" for name in ("uploaded_by", "requested_by", "generated_for")
    )
    types = tuple(
        f"json_type({metadata},'$.{name}')" for name in ("uploaded_by", "requested_by", "generated_for")
    )
    anchor = f"COALESCE({fields[0]},{fields[1]},{fields[2]})"
    duplicate_guard = f"""NOT EXISTS (
        SELECT 1 FROM json_tree({metadata}) owner_member
         WHERE owner_member.key IN ('uploaded_by','requested_by','generated_for')
         GROUP BY owner_member.parent, CAST(owner_member.key AS TEXT)
        HAVING COUNT(*)>1
    )"""
    # json_type(path) returns SQL NULL for an absent field, while the closed
    # owner shape treats absent and explicit JSON null identically.
    shape = " AND ".join(f"({item} IS NULL OR {item} IN ('text','null'))" for item in types)
    agrees = " AND ".join(
        f"({kind} IS NULL OR {kind}='null' OR ({kind}='text' AND {value}={anchor}))"
        for kind, value in zip(types, fields, strict=True)
    )
    if corpus is ArchiveSearchCorpus.DOCUMENTS:
        attribution = f"{types[0]}='text' AND {fields[0]}={anchor}"
    else:
        attribution = (
            f"({anchor} IS NOT NULL OR ({alias}.user_id=principal_authority.owner_id AND {anchor} IS NULL))"
        )
    return f"({_raw_metadata_shape(alias)} AND {duplicate_guard} AND {shape} AND {agrees} AND {attribution})"


def _raw_attribution_valid(
    corpus: ArchiveSearchCorpus,
    *,
    alias: str = "r",
) -> str:
    metadata_valid = _catalog_metadata_valid(
        alias,
        require_format_ready=corpus is ArchiveSearchCorpus.DOCUMENTS,
    )
    return f"""(
        {_raw_owner_attribution_valid(corpus, alias=alias)}
        AND {metadata_valid}
    )"""


def _common_raw_authority(
    corpus: ArchiveSearchCorpus,
    *,
    include_body: bool,
) -> str:
    body_projection = ", live_raw.raw_content" if include_body else ""
    body_join = (
        "JOIN raw_objects live_raw ON live_raw.rowid=r.raw_rowid "
        "AND live_raw.id=r.raw_id AND live_raw.user_id=r.user_id"
        if include_body
        else ""
    )
    tenant_scope = "AND r.content_type='file'" if corpus is ArchiveSearchCorpus.DOCUMENTS else ""
    principal_scope = f"AND {_not_audio_document('r')}" if corpus is ArchiveSearchCorpus.DOCUMENTS else ""
    raw_backfill = (
        f"(NOT {_raw_owner_attribution_valid(corpus)} OR "
        f"({_principal_raw_authority(corpus)} AND NOT {_raw_attribution_valid(corpus)}))"
    )
    knowledge_body_inner = ", k.content AS knowledge_content" if include_body else ""
    knowledge_body_outer = ", knowledge_content" if include_body else ""
    return f"""principal_authority(owner_id) AS MATERIALIZED (SELECT ?),
    tenant_raw AS MATERIALIZED (
        SELECT r.rowid AS raw_rowid, r.id AS raw_id, r.user_id, r.source,
               r.source_ref, r.content_type, r.metadata_json, r.content_hash,
               r.version AS raw_version, r.received_at, r.created_at
          FROM raw_objects r
          JOIN principal_authority ON 1=1
         WHERE r.user_id=?
           AND EXISTS (SELECT 1 FROM users tenant_authority
                        WHERE tenant_authority.id=? AND tenant_authority.status='active')
           AND EXISTS (SELECT 1 FROM users owner_authority
                        WHERE owner_authority.id=principal_authority.owner_id
                          AND owner_authority.status='active')
           AND r.deleted_at IS NULL
           {tenant_scope}
           AND {_not_private_raw_dependency("r")}
    ),
    principal_scoped_raw AS MATERIALIZED (
        SELECT r.* FROM tenant_raw r
          JOIN principal_authority ON 1=1
         WHERE {_principal_raw_authority(corpus)}
           {principal_scope}
    ),
    raw_authority_backfill(value) AS MATERIALIZED (
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM tenant_raw r
              JOIN principal_authority ON 1=1
             WHERE {raw_backfill}
        ) THEN 1 ELSE 0 END
    ),
    authorized_raw AS MATERIALIZED (
        SELECT r.*{body_projection}
          FROM principal_scoped_raw r
          {body_join}
    ),
    current_inbox_all AS MATERIALIZED (
        SELECT inbox_id, raw_object_id, status, ordering_ready FROM (
            SELECT i.id AS inbox_id, i.raw_object_id, i.status,
                   CASE WHEN {_canonical_utc_sql("i.created_at")}
                              AND (i.reviewed_at IS NULL OR {_canonical_utc_sql("i.reviewed_at")})
                        THEN 1 ELSE 0 END AS ordering_ready,
                   ROW_NUMBER() OVER (
                       PARTITION BY i.raw_object_id
                       ORDER BY CASE WHEN {_canonical_utc_sql("i.created_at")}
                                     THEN i.created_at END DESC,
                                CASE WHEN i.reviewed_at IS NULL
                                          OR {_canonical_utc_sql("i.reviewed_at")}
                                     THEN COALESCE(i.reviewed_at,'') END DESC,
                                i.id DESC
                   ) AS choice
             FROM inbox i
              JOIN authorized_raw ar ON ar.raw_id=i.raw_object_id AND ar.user_id=i.user_id
             WHERE {_not_private_inbox_dependency("i")}
        ) WHERE choice=1
    ),
    current_inbox AS MATERIALIZED (
        SELECT * FROM current_inbox_all
         WHERE status IN ('pending','classified','archived','ignored')
    ),
    inbox_scope_backfill(value) AS MATERIALIZED (
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM current_inbox_all
             WHERE status NOT IN ('pending','classified','archived','ignored')
                OR ordering_ready<>1
        ) OR EXISTS (
            SELECT 1 FROM inbox i
              JOIN authorized_raw ar ON ar.raw_id=i.raw_object_id AND ar.user_id=i.user_id
             WHERE NOT {_canonical_utc_sql("i.created_at")}
                OR (i.reviewed_at IS NOT NULL AND NOT {_canonical_utc_sql("i.reviewed_at")})
        ) THEN 1 ELSE 0 END
    ),
    knowledge_source_rows AS MATERIALIZED (
        SELECT knowledge_rowid, knowledge_id, raw_object_id, knowledge_version, knowledge_lifecycle,
               knowledge_superseded_by_id,
               knowledge_title, knowledge_summary, knowledge_tags_json, knowledge_kind,
               knowledge_created_at,
               knowledge_updated_at{knowledge_body_outer}
          FROM (
            SELECT k.rowid AS knowledge_rowid, k.id AS knowledge_id, k.raw_object_id,
                   k.version AS knowledge_version,
                   k.lifecycle_stage AS knowledge_lifecycle,
                   k.superseded_by_id AS knowledge_superseded_by_id,
                   k.title AS knowledge_title, k.summary AS knowledge_summary,
                   k.tags_json AS knowledge_tags_json, k.knowledge_kind AS knowledge_kind,
                   k.created_at AS knowledge_created_at,
                   k.updated_at AS knowledge_updated_at{knowledge_body_inner}
             FROM knowledge_objects k
              JOIN authorized_raw ar ON ar.raw_id=k.raw_object_id AND ar.user_id=k.user_id
             WHERE k.deleted_at IS NULL
               AND {_not_private_knowledge_dependency("k")}
        )
    ),
    knowledge_sources AS MATERIALIZED (
        SELECT * FROM knowledge_source_rows
         WHERE knowledge_lifecycle IN ('active','archived','deprecated')
           AND (knowledge_superseded_by_id IS NULL
                OR knowledge_lifecycle='deprecated')
    ),
    knowledge_scope_backfill(value) AS MATERIALIZED (
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM knowledge_source_rows
             WHERE knowledge_lifecycle NOT IN ('active','archived','deprecated')
                OR (knowledge_superseded_by_id IS NOT NULL
                    AND knowledge_lifecycle<>'deprecated')
        ) THEN 1 ELSE 0 END
    ),
    authority_backfill(value) AS MATERIALIZED (
        SELECT CASE WHEN (SELECT value FROM raw_authority_backfill)=1
                          OR (SELECT value FROM inbox_scope_backfill)=1
                          OR (SELECT value FROM knowledge_scope_backfill)=1
                    THEN 1 ELSE 0 END
    )"""


def _bounded_common_raw_authority(
    corpus: ArchiveSearchCorpus,
    *,
    include_document_date: bool,
) -> str:
    """Authority CTE whose materialized rows contain bounded scalars only."""

    tenant_scope = "AND r.content_type='file'" if corpus is ArchiveSearchCorpus.DOCUMENTS else ""
    principal_scope = "AND raw_not_audio=1" if corpus is ArchiveSearchCorpus.DOCUMENTS else ""
    document_date = _document_date_expression("r") if include_document_date else "NULL"
    raw_id_ready = """(
        typeof(r.id)='text' AND length(r.id)=20 AND substr(r.id,1,4)='raw_'
        AND substr(r.id,5)<>'' AND substr(r.id,5) NOT GLOB '*[^0-9a-f]*'
    )"""
    raw_material_ready = f"""(
        {raw_id_ready}
        AND typeof(r.source)='text' AND length(CAST(r.source AS BLOB)) BETWEEN 1 AND 64
        AND typeof(r.content_type)='text'
        AND length(CAST(r.content_type AS BLOB)) BETWEEN 1 AND 64
        AND typeof(r.content_hash)='text' AND length(r.content_hash)=64
        AND r.content_hash NOT GLOB '*[^0-9a-f]*'
        AND typeof(r.version)='integer' AND r.version BETWEEN 1 AND 1000000000
        AND typeof(r.received_at)='text'
        AND length(CAST(r.received_at AS BLOB)) BETWEEN 1 AND 64
        AND typeof(r.created_at)='text'
        AND length(CAST(r.created_at AS BLOB)) BETWEEN 1 AND 64
    )"""
    raw_filename = _filename_expression("r")
    raw_principal = _principal_raw_authority(corpus, alias="r")
    raw_owner_valid = _raw_owner_attribution_valid(corpus, alias="r")
    raw_attribution_valid = _raw_attribution_valid(corpus, alias="r")
    raw_not_audio = _not_audio_document("r") if corpus is ArchiveSearchCorpus.DOCUMENTS else "1"
    return f"""principal_authority(owner_id) AS MATERIALIZED (SELECT ?),
    tenant_raw AS MATERIALIZED (
        SELECT r.rowid AS raw_rowid,
               CASE WHEN {raw_id_ready} THEN r.id END AS raw_id,
               r.user_id,
               CASE WHEN typeof(r.source)='text'
                          AND length(CAST(r.source AS BLOB)) BETWEEN 1 AND 64
                    THEN r.source END AS source,
               CASE WHEN typeof(r.content_type)='text'
                          AND length(CAST(r.content_type AS BLOB)) BETWEEN 1 AND 64
                    THEN r.content_type END AS content_type,
               CASE WHEN typeof(r.content_hash)='text' AND length(r.content_hash)=64
                          AND r.content_hash NOT GLOB '*[^0-9a-f]*'
                    THEN r.content_hash END AS content_hash,
               CASE WHEN typeof(r.version)='integer' AND r.version BETWEEN 1 AND 1000000000
                    THEN r.version END AS raw_version,
               CASE WHEN typeof(r.received_at)='text'
                          AND length(CAST(r.received_at AS BLOB)) BETWEEN 1 AND 64
                    THEN r.received_at END AS received_at,
               CASE WHEN typeof(r.created_at)='text'
                          AND length(CAST(r.created_at AS BLOB)) BETWEEN 1 AND 64
                    THEN r.created_at END AS created_at,
               {raw_filename} AS raw_filename,
               {document_date} AS raw_document_date,
               CASE WHEN {raw_principal} THEN 1 ELSE 0 END AS raw_principal_authorized,
               CASE WHEN {raw_owner_valid} THEN 1 ELSE 0 END AS raw_owner_valid,
               CASE WHEN {raw_attribution_valid} THEN 1 ELSE 0 END AS raw_attribution_valid,
               CASE WHEN {raw_not_audio} THEN 1 ELSE 0 END AS raw_not_audio,
               CASE WHEN {raw_material_ready} THEN 1 ELSE 0 END AS raw_material_ready
          FROM raw_objects r
          JOIN principal_authority ON 1=1
         WHERE r.user_id=?
           AND EXISTS (SELECT 1 FROM users tenant_authority
                        WHERE tenant_authority.id=? AND tenant_authority.status='active')
           AND EXISTS (SELECT 1 FROM users owner_authority
                        WHERE owner_authority.id=principal_authority.owner_id
                          AND owner_authority.status='active')
           AND r.deleted_at IS NULL
           {tenant_scope}
           AND {_not_private_raw_dependency("r")}
    ),
    principal_scoped_raw AS MATERIALIZED (
        SELECT raw_rowid, raw_id, user_id, source, content_type, content_hash,
               raw_version, received_at, created_at, raw_filename, raw_document_date
          FROM tenant_raw
         WHERE raw_principal_authorized=1 AND raw_material_ready=1 {principal_scope}
    ),
    raw_authority_backfill(value) AS MATERIALIZED (
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM tenant_raw
             WHERE raw_owner_valid<>1
                OR (raw_principal_authorized=1
                    AND (raw_attribution_valid<>1 OR raw_material_ready<>1))
        ) THEN 1 ELSE 0 END
    ),
    authorized_raw AS MATERIALIZED (
        SELECT raw_rowid, raw_id, user_id, source, content_type, content_hash,
               raw_version, received_at, created_at, raw_filename, raw_document_date
          FROM principal_scoped_raw
    ),
    current_inbox_all AS MATERIALIZED (
        SELECT inbox_id, raw_object_id, status, ordering_ready FROM (
            SELECT CASE WHEN typeof(i.id)='text'
                                  AND length(CAST(i.id AS BLOB)) BETWEEN 1 AND 200
                        THEN i.id END AS inbox_id,
                   i.raw_object_id,
                   CASE WHEN typeof(i.status)='text'
                                  AND length(CAST(i.status AS BLOB)) BETWEEN 1 AND 32
                        THEN i.status END AS status,
                   CASE WHEN {_canonical_utc_sql("i.created_at")}
                              AND (i.reviewed_at IS NULL OR {_canonical_utc_sql("i.reviewed_at")})
                        THEN 1 ELSE 0 END AS ordering_ready,
                   ROW_NUMBER() OVER (
                       PARTITION BY i.raw_object_id
                       ORDER BY CASE WHEN {_canonical_utc_sql("i.created_at")}
                                     THEN i.created_at END DESC,
                                CASE WHEN i.reviewed_at IS NULL
                                          OR {_canonical_utc_sql("i.reviewed_at")}
                                     THEN COALESCE(i.reviewed_at,'') END DESC,
                                CASE WHEN typeof(i.id)='text'
                                          AND length(CAST(i.id AS BLOB))<=200
                                     THEN i.id END DESC
                   ) AS choice
              FROM inbox i
              JOIN authorized_raw ar
                ON ar.raw_id=i.raw_object_id AND ar.user_id=i.user_id
             WHERE {_not_private_inbox_dependency("i")}
        ) WHERE choice=1
    ),
    current_inbox AS MATERIALIZED (
        SELECT inbox_id, raw_object_id, status, ordering_ready
          FROM current_inbox_all
         WHERE status IN ('pending','classified','archived','ignored')
    ),
    inbox_scope_backfill(value) AS MATERIALIZED (
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM current_inbox_all
             WHERE status NOT IN ('pending','classified','archived','ignored')
                OR status IS NULL OR inbox_id IS NULL OR ordering_ready<>1
        ) OR EXISTS (
            SELECT 1 FROM inbox i
              JOIN authorized_raw ar
                ON ar.raw_id=i.raw_object_id AND ar.user_id=i.user_id
             WHERE NOT {_canonical_utc_sql("i.created_at")}
                OR (i.reviewed_at IS NOT NULL AND NOT {_canonical_utc_sql("i.reviewed_at")})
        ) THEN 1 ELSE 0 END
    ),
    knowledge_source_rows AS MATERIALIZED (
        SELECT k.rowid AS knowledge_rowid,
               CASE WHEN typeof(k.id)='text'
                          AND length(CAST(k.id AS BLOB)) BETWEEN 11 AND 123
                          AND substr(k.id,1,3)='ko_'
                          AND substr(k.id,4) NOT GLOB '*[^A-Za-z0-9_-]*'
                    THEN k.id END AS knowledge_id,
               k.raw_object_id,
               CASE WHEN typeof(k.version)='integer' AND k.version BETWEEN 1 AND 1000000000
                    THEN k.version END AS knowledge_version,
               CASE WHEN typeof(k.lifecycle_stage)='text'
                          AND length(CAST(k.lifecycle_stage AS BLOB)) BETWEEN 1 AND 32
                    THEN k.lifecycle_stage END AS knowledge_lifecycle,
               CASE WHEN k.superseded_by_id IS NULL THEN 0 ELSE 1 END
                    AS knowledge_superseded,
               NULL AS knowledge_title, NULL AS knowledge_summary,
               NULL AS knowledge_tags_json, NULL AS knowledge_kind,
               CASE WHEN typeof(k.created_at)='text'
                          AND length(CAST(k.created_at AS BLOB)) BETWEEN 1 AND 64
                    THEN k.created_at END AS knowledge_created_at,
               CASE WHEN typeof(k.updated_at)='text'
                          AND length(CAST(k.updated_at AS BLOB)) BETWEEN 1 AND 64
                    THEN k.updated_at END AS knowledge_updated_at,
               CASE WHEN typeof(k.id)='text'
                          AND length(CAST(k.id AS BLOB)) BETWEEN 11 AND 123
                          AND substr(k.id,1,3)='ko_'
                          AND substr(k.id,4) NOT GLOB '*[^A-Za-z0-9_-]*'
                          AND typeof(k.version)='integer' AND k.version BETWEEN 1 AND 1000000000
                          AND typeof(k.lifecycle_stage)='text'
                          AND length(CAST(k.lifecycle_stage AS BLOB)) BETWEEN 1 AND 32
                          AND typeof(k.created_at)='text'
                          AND length(CAST(k.created_at AS BLOB)) BETWEEN 1 AND 64
                          AND typeof(k.updated_at)='text'
                          AND length(CAST(k.updated_at AS BLOB)) BETWEEN 1 AND 64
                    THEN 1 ELSE 0 END AS knowledge_material_ready
          FROM knowledge_objects k
          JOIN authorized_raw ar ON ar.raw_id=k.raw_object_id AND ar.user_id=k.user_id
         WHERE k.deleted_at IS NULL
           AND {_not_private_knowledge_dependency("k")}
    ),
    knowledge_sources AS MATERIALIZED (
        SELECT knowledge_rowid, knowledge_id, raw_object_id, knowledge_version,
               knowledge_lifecycle, knowledge_superseded,
               knowledge_title, knowledge_summary, knowledge_tags_json, knowledge_kind,
               knowledge_created_at, knowledge_updated_at
          FROM knowledge_source_rows
         WHERE knowledge_material_ready=1
           AND knowledge_lifecycle IN ('active','archived','deprecated')
           AND (knowledge_superseded=0 OR knowledge_lifecycle='deprecated')
    ),
    knowledge_scope_backfill(value) AS MATERIALIZED (
        SELECT CASE WHEN EXISTS (
            SELECT 1 FROM knowledge_source_rows
             WHERE knowledge_material_ready<>1
                OR knowledge_lifecycle NOT IN ('active','archived','deprecated')
                OR (knowledge_superseded=1 AND knowledge_lifecycle<>'deprecated')
        ) THEN 1 ELSE 0 END
    ),
    authority_backfill(value) AS MATERIALIZED (
        SELECT CASE WHEN (SELECT value FROM raw_authority_backfill)=1
                          OR (SELECT value FROM inbox_scope_backfill)=1
                          OR (SELECT value FROM knowledge_scope_backfill)=1
                    THEN 1 ELSE 0 END
    )"""


_DOCUMENT_LIFECYCLE = "CASE WHEN ci.inbox_id IS NULL THEN COALESCE(ck.knowledge_lifecycle,'active') WHEN ci.status='pending' THEN 'pending' WHEN ci.status='classified' THEN 'classified' WHEN ci.status='archived' THEN 'archived' ELSE NULL END"
_DOCUMENT_REVIEW = "CASE WHEN ci.inbox_id IS NULL THEN CASE ck.knowledge_lifecycle WHEN 'active' THEN 'confirmed' WHEN 'archived' THEN 'archived' WHEN 'deprecated' THEN 'archived' ELSE NULL END WHEN ci.status='pending' THEN 'pending' WHEN ci.status='classified' THEN 'confirmed' WHEN ci.status='archived' THEN 'archived' ELSE NULL END"
_KNOWLEDGE_LIFECYCLE = "ck.knowledge_lifecycle"
_KNOWLEDGE_REVIEW = "CASE ck.knowledge_lifecycle WHEN 'active' THEN 'confirmed' ELSE 'archived' END"


def _source_cte(
    corpus: ArchiveSearchCorpus,
    request: ArchiveSearchRequest,
    *,
    include_body: bool,
    bounded_material: bool = False,
) -> tuple[str, tuple[object, ...]]:
    if bounded_material and include_body:
        raise _fail("bounded archive source contour cannot include bodies")
    common = (
        _bounded_common_raw_authority(
            corpus,
            include_document_date=any(
                item.role is TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE
                for item in _temporal_constraints(request, corpus)
            ),
        )
        if bounded_material
        else _common_raw_authority(corpus, include_body=include_body)
    )
    if corpus is ArchiveSearchCorpus.DOCUMENTS:
        document_date = (
            "ar.raw_document_date"
            if bounded_material
            else _document_date_expression("ar")
            if any(
                item.role is TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE
                for item in _temporal_constraints(request, corpus)
            )
            else "NULL"
        )
        temporal_expressions = {
            TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE: document_date,
            TemporalRole.RECEIVED_AT: "ar.received_at",
        }
        suffix, parameters = _scope_suffix(
            request,
            corpus,
            lifecycle_expression=_DOCUMENT_LIFECYCLE,
            review_expression=_DOCUMENT_REVIEW,
            temporal_expressions=temporal_expressions,
            include_unknown=True,
        )
        body_projection = (
            ", ar.raw_content AS passage_body, ck.knowledge_content AS knowledge_content"
            if include_body
            else ""
        )
        return (
            f"""{common},
            document_knowledge AS MATERIALIZED (
                SELECT * FROM (
                    SELECT ks.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY ks.raw_object_id
                               ORDER BY CASE ks.knowledge_lifecycle
                                            WHEN 'active' THEN 0
                                            WHEN 'archived' THEN 1 ELSE 2 END,
                                        ks.knowledge_updated_at DESC,
                                        ks.knowledge_id ASC
                           ) AS choice
                      FROM knowledge_sources ks
                ) WHERE choice=1
            ),
            eligible_sources AS MATERIALIZED (
                SELECT ar.*, ci.inbox_id, ci.status AS inbox_status,
                       ck.knowledge_rowid, ck.knowledge_id,
                       ck.knowledge_version, ck.knowledge_lifecycle,
                       ck.knowledge_title, ck.knowledge_summary, ck.knowledge_tags_json,
                       ck.knowledge_kind,
                       ck.knowledge_created_at, ck.knowledge_updated_at,
                       {_DOCUMENT_LIFECYCLE} AS lifecycle_state,
                       {_DOCUMENT_REVIEW} AS review_state,
                       {_temporal_ready_expression(request, corpus, temporal_expressions)}
                           AS temporal_ready,
                       {document_date} AS raw_document_date,
                       ar.received_at AS raw_received_at,
                       ar.received_at AS sort_time{body_projection}
                  FROM authorized_raw ar
                  LEFT JOIN current_inbox_all ci ON ci.raw_object_id=ar.raw_id
                  LEFT JOIN document_knowledge ck ON ck.raw_object_id=ar.raw_id
                 WHERE (ci.status IS NULL OR ci.status<>'ignored'){suffix}
            ),
            authorized_sources AS MATERIALIZED (
                SELECT * FROM eligible_sources
                 WHERE lifecycle_state IS NOT NULL AND review_state IS NOT NULL
                   AND temporal_ready=1
            ),
            lane_backfill(value) AS MATERIALIZED (
                SELECT CASE WHEN EXISTS (
                    SELECT 1 FROM eligible_sources
                     WHERE lifecycle_state IS NULL OR review_state IS NULL
                        OR temporal_ready=0
                ) THEN 1 ELSE 0 END
            )""",
            parameters,
        )
    temporal_expressions = {
        TemporalRole.RECEIVED_AT: "ar.received_at",
        TemporalRole.KNOWLEDGE_PROJECTION_CREATED_AT: "ck.knowledge_created_at",
        TemporalRole.KNOWLEDGE_PROJECTION_MODIFIED_AT: "ck.knowledge_updated_at",
    }
    suffix, parameters = _scope_suffix(
        request,
        corpus,
        lifecycle_expression=_KNOWLEDGE_LIFECYCLE,
        review_expression=_KNOWLEDGE_REVIEW,
        temporal_expressions=temporal_expressions,
    )
    body_projection = ", ck.knowledge_content AS passage_body" if include_body else ""
    return (
        f"""{common},
        eligible_sources AS MATERIALIZED (
            SELECT ar.*, ci.inbox_id, ci.status AS inbox_status,
                   ck.knowledge_rowid, ck.knowledge_id,
                   ck.knowledge_version, ck.knowledge_lifecycle,
                   ck.knowledge_title, ck.knowledge_summary, ck.knowledge_tags_json,
                   ck.knowledge_kind,
                   ck.knowledge_created_at, ck.knowledge_updated_at,
                   {_KNOWLEDGE_LIFECYCLE} AS lifecycle_state,
                   {_KNOWLEDGE_REVIEW} AS review_state,
                   {_temporal_ready_expression(request, corpus, temporal_expressions)}
                       AS temporal_ready,
                   ar.received_at AS raw_received_at,
                   ck.knowledge_updated_at AS sort_time{body_projection}
             FROM authorized_raw ar
              LEFT JOIN current_inbox ci ON ci.raw_object_id=ar.raw_id
             JOIN knowledge_sources ck ON ck.raw_object_id=ar.raw_id
             WHERE (ci.status IS NULL OR ci.status<>'ignored'){suffix}
        ),
        authorized_sources AS MATERIALIZED (
            SELECT * FROM eligible_sources WHERE temporal_ready=1
        ),
        lane_backfill(value) AS MATERIALIZED (
            SELECT CASE WHEN EXISTS (
                SELECT 1 FROM eligible_sources WHERE temporal_ready=0
            ) THEN 1 ELSE 0 END
        )""",
        parameters,
    )


def _authority_parameters(tenant_id: str, owner_id: str) -> tuple[object, ...]:
    return (owner_id, tenant_id, tenant_id)


def _filename_expression(alias: str = "s") -> str:
    metadata = _safe_raw_metadata(alias)
    return f"""CASE
        WHEN json_type({metadata},'$.filename')='text'
         AND length(json_extract({metadata},'$.filename')) BETWEEN 1 AND 260
         AND trim(json_extract({metadata},'$.filename'))=
             json_extract({metadata},'$.filename')
         AND instr(json_extract({metadata},'$.filename'),char(0))=0
         AND instr(json_extract({metadata},'$.filename'),char(10))=0
         AND instr(json_extract({metadata},'$.filename'),char(13))=0
        THEN json_extract({metadata},'$.filename') ELSE '' END"""


def _format_expression(alias: str = "s") -> str:
    """Project one unambiguous bounded MIME value for navigation matching."""

    metadata = _safe_raw_metadata(alias)
    mime_type = f"json_extract({metadata},'$.mime_type')"
    legacy_mime = f"json_extract({metadata},'$.mime')"

    def valid(path: str, expression: str) -> str:
        return f"""(
            COALESCE(json_type({metadata},'{path}'),'')='text'
            AND {_mime_value_valid(expression)}
        )"""

    mime_type_valid = valid("$.mime_type", mime_type)
    legacy_mime_valid = valid("$.mime", legacy_mime)
    return f"""CASE
        WHEN {_catalog_metadata_valid(alias, require_format_ready=True)}
         AND ({mime_type_valid} OR {legacy_mime_valid})
         AND (NOT ({mime_type_valid}) OR NOT ({legacy_mime_valid})
              OR lower({mime_type})=lower({legacy_mime}))
        THEN COALESCE(
                 CASE WHEN {mime_type_valid} THEN {mime_type} END,
                 CASE WHEN {legacy_mime_valid} THEN {legacy_mime} END,
                 ''
             )
        ELSE '' END"""


def _title_expression(alias: str = "s") -> str:
    return f"""CASE
        WHEN length(COALESCE({alias}.knowledge_title,'')) BETWEEN 1 AND 260
         AND trim({alias}.knowledge_title)={alias}.knowledge_title
         AND instr({alias}.knowledge_title,char(0))=0
         AND instr({alias}.knowledge_title,char(10))=0
         AND instr({alias}.knowledge_title,char(13))=0
        THEN {alias}.knowledge_title ELSE '' END"""


def _document_catalog_title_expression(alias: str = "dc") -> str:
    return f"""CASE
        WHEN typeof({alias}.semantic_title)='text'
         AND length({alias}.semantic_title) BETWEEN 1 AND 240
         AND length(CAST({alias}.semantic_title AS BLOB))<=1024
         AND trim({alias}.semantic_title)={alias}.semantic_title
         AND instr({alias}.semantic_title,char(0))=0
         AND instr({alias}.semantic_title,char(10))=0
         AND instr({alias}.semantic_title,char(13))=0
         AND friday_archive_catalog_title_valid({alias}.semantic_title)=1
        THEN {alias}.semantic_title ELSE '' END"""


def _document_catalog_utc_sql(expression: str) -> str:
    """Recognize the schema-41 second-precision ``...Z`` timestamp."""

    return f"""(
        typeof({expression})='text'
        AND length({expression})=20
        AND substr({expression},5,1)='-'
        AND substr({expression},8,1)='-'
        AND substr({expression},11,1)='T'
        AND substr({expression},14,1)=':'
        AND substr({expression},17,1)=':'
        AND substr({expression},20,1)='Z'
        AND substr({expression},1,4) NOT GLOB '*[^0-9]*'
        AND substr({expression},6,2) NOT GLOB '*[^0-9]*'
        AND substr({expression},9,2) NOT GLOB '*[^0-9]*'
        AND substr({expression},12,2) NOT GLOB '*[^0-9]*'
        AND substr({expression},15,2) NOT GLOB '*[^0-9]*'
        AND substr({expression},18,2) NOT GLOB '*[^0-9]*'
        AND date(substr({expression},1,10))=substr({expression},1,10)
        AND CAST(substr({expression},12,2) AS INTEGER) BETWEEN 0 AND 23
        AND CAST(substr({expression},15,2) AS INTEGER) BETWEEN 0 AND 59
        AND CAST(substr({expression},18,2) AS INTEGER) BETWEEN 0 AND 59
        AND strftime('%Y-%m-%dT%H:%M:%SZ',{expression})={expression}
    )"""


def _document_catalog_current_expression(
    alias: str = "dc",
    *,
    enrichment_revision: int,
) -> str:
    semantic_title = _document_catalog_title_expression(alias)
    enriched_at = _document_catalog_utc_sql(f"{alias}.enriched_at")
    return f"""(
        {alias}.raw_object_id IS NOT NULL
        AND typeof({alias}.source_version)='integer'
        AND {alias}.source_version>=1
        AND typeof({alias}.source_content_sha256)='text'
        AND length({alias}.source_content_sha256)=64
        AND {alias}.source_content_sha256 NOT GLOB '*[^0-9a-f]*'
        AND typeof({alias}.extracted_text_sha256)='text'
        AND length({alias}.extracted_text_sha256)=64
        AND {alias}.extracted_text_sha256 NOT GLOB '*[^0-9a-f]*'
        AND ({alias}.semantic_title IS NULL OR {semantic_title}<>'')
        AND {alias}.title_authority='navigation_only'
        AND typeof({alias}.enrichment_revision)='integer'
        AND {alias}.enrichment_revision={enrichment_revision}
        AND {alias}.enrichment_status='current'
        AND {alias}.incomplete_reason IS NULL
        AND {enriched_at}
    )"""


def _document_passage_current_expression(
    projection_alias: str = "dp",
    *,
    source_alias: str = "s",
    index_revision: str,
) -> str:
    """Exact source-bound readiness predicate for the reader-first sidecar.

    Passage rows grant no authority and are deliberately joined only after
    ``authorized_sources``.  This proves that the complete code-owned
    projection exists before a DOCUMENTS lexical lane may claim complete index
    coverage or mint a locator from one of its persisted child rows.
    """

    projection = projection_alias
    source = source_alias
    if type(index_revision) is not str or _PASSAGE_REVISION.fullmatch(index_revision) is None:
        raise _fail("archive document passage revision is invalid")
    return f"""(
        {projection}.raw_object_id IS {source}.raw_id
        AND typeof({source}.raw_version)='integer'
        AND {source}.raw_version>=1
        AND {projection}.source_version IS {source}.raw_version
        AND typeof({source}.content_hash)='text'
        AND length({source}.content_hash)=64
        AND {source}.content_hash NOT GLOB '*[^0-9a-f]*'
        AND {projection}.source_content_sha256 IS {source}.content_hash
        AND typeof({source}.passage_body)='text'
        AND friday_exact_text_char_count({source}.passage_body)>0
        AND typeof({projection}.extracted_text_sha256)='text'
        AND length({projection}.extracted_text_sha256)=64
        AND {projection}.extracted_text_sha256 NOT GLOB '*[^0-9a-f]*'
        AND {projection}.extracted_text_sha256=friday_exact_text_sha256({source}.passage_body)
        AND typeof({projection}.source_char_count)='integer'
        AND {projection}.source_char_count=friday_exact_text_char_count({source}.passage_body)
        AND typeof({projection}.passage_set_sha256)='text'
        AND length({projection}.passage_set_sha256)=64
        AND {projection}.passage_set_sha256 NOT GLOB '*[^0-9a-f]*'
        AND {projection}.passage_index_revision='{index_revision}'
        AND {projection}.projection_status='current'
        AND {projection}.incomplete_reason IS NULL
        AND typeof({projection}.passage_count)='integer'
        AND {projection}.passage_count BETWEEN 1 AND {_DOCUMENT_PASSAGE_MAX_COUNT}
        AND (SELECT COUNT(*) FROM document_passages passage
              WHERE passage.raw_object_id={projection}.raw_object_id)
            ={projection}.passage_count
        AND (SELECT MIN(passage.chunk_index) FROM document_passages passage
              WHERE passage.raw_object_id={projection}.raw_object_id)=0
        AND (SELECT MAX(passage.chunk_index) FROM document_passages passage
              WHERE passage.raw_object_id={projection}.raw_object_id)
            ={projection}.passage_count-1
        AND (SELECT MIN(passage.start_char) FROM document_passages passage
              WHERE passage.raw_object_id={projection}.raw_object_id)=0
        AND (SELECT MAX(passage.end_char) FROM document_passages passage
              WHERE passage.raw_object_id={projection}.raw_object_id)
            ={projection}.source_char_count
        AND COALESCE((
            SELECT friday_document_passage_set_sha256(
                       passage.chunk_index,passage.start_char,
                       passage.end_char,passage.content_sha256)
              FROM document_passages passage
             WHERE passage.raw_object_id={projection}.raw_object_id
        ),'')={projection}.passage_set_sha256
    )"""


def _document_passage_backfill_expression(
    projection_alias: str = "dp",
    *,
    source_alias: str = "s",
    index_revision: str,
) -> str:
    """Classify only repairable/missing source-bound passage coverage."""

    projection = projection_alias
    source = source_alias
    if type(index_revision) is not str or _PASSAGE_REVISION.fullmatch(index_revision) is None:
        raise _fail("archive document passage revision is invalid")
    return f"""(
        {projection}.raw_object_id IS NULL
        OR {projection}.source_version IS NOT {source}.raw_version
        OR {projection}.source_content_sha256 IS NOT {source}.content_hash
        OR {projection}.passage_index_revision<>'{index_revision}'
        OR ({projection}.projection_status='incomplete'
            AND {projection}.incomplete_reason IN ('backfill_pending','source_changed'))
    )"""


def _passage_projection_cte(
    corpus: ArchiveSearchCorpus,
    *,
    document_passage_available: bool,
    document_passage_revision: str | None,
) -> str:
    if corpus is not ArchiveSearchCorpus.DOCUMENTS:
        return """passage_projection_sources AS MATERIALIZED (
            SELECT s.*, 1 AS passage_projection_current,
                   0 AS passage_projection_backfill_pending,
                   0 AS passage_projection_unavailable
              FROM authorized_sources s
        )"""
    if not document_passage_available or document_passage_revision is None:
        return """passage_projection_sources AS MATERIALIZED (
            SELECT s.*, 0 AS passage_projection_current,
                   0 AS passage_projection_backfill_pending,
                   1 AS passage_projection_unavailable
              FROM authorized_sources s
        )"""
    current = _document_passage_current_expression(
        "dp",
        source_alias="s",
        index_revision=document_passage_revision,
    )
    backfill = _document_passage_backfill_expression(
        "dp",
        source_alias="s",
        index_revision=document_passage_revision,
    )
    return f"""passage_projection_joined AS MATERIALIZED (
        SELECT s.*, CASE WHEN {current} THEN 1 ELSE 0 END AS passage_projection_current,
               CASE WHEN {backfill} THEN 1 ELSE 0 END AS passage_projection_backfill_pending
          FROM authorized_sources s
          LEFT JOIN document_passage_projections dp
            ON dp.raw_object_id=s.raw_id
    ),
    passage_projection_sources AS MATERIALIZED (
        SELECT joined.*,
               CASE WHEN joined.passage_projection_current=0
                          AND joined.passage_projection_backfill_pending=0
                    THEN 1 ELSE 0 END AS passage_projection_unavailable
          FROM passage_projection_joined joined
    )"""


def _catalog_projection_cte(
    corpus: ArchiveSearchCorpus,
    *,
    document_catalog_available: bool,
    enrichment_revision: int,
) -> str:
    if corpus is not ArchiveSearchCorpus.DOCUMENTS:
        return """catalog_projection_sources AS MATERIALIZED (
            SELECT s.*, 1 AS catalog_projection_current,
                   '' AS catalog_semantic_title
              FROM authorized_sources s
        )"""
    if not document_catalog_available:
        return """catalog_projection_sources AS MATERIALIZED (
            SELECT s.*, 0 AS catalog_projection_current,
                   '' AS catalog_semantic_title
              FROM authorized_sources s
        )"""
    current = _document_catalog_current_expression(
        "dc",
        enrichment_revision=enrichment_revision,
    )
    semantic_title = _document_catalog_title_expression("dc")
    return f"""document_catalog_joined AS MATERIALIZED (
        SELECT s.*,
               CASE WHEN {current} THEN 1 ELSE 0 END AS catalog_projection_current,
               {semantic_title} AS catalog_semantic_title_candidate
          FROM authorized_sources s
          LEFT JOIN document_catalog dc
            ON dc.raw_object_id=s.raw_id
           AND dc.source_version=s.raw_version
           AND dc.source_content_sha256=s.content_hash
    ),
    catalog_projection_sources AS MATERIALIZED (
        SELECT j.*,
               CASE WHEN j.catalog_projection_current=1
                    THEN j.catalog_semantic_title_candidate ELSE '' END
                   AS catalog_semantic_title
          FROM document_catalog_joined j
    )"""


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _catalog_needles(request: ArchiveSearchRequest) -> str:
    values = tuple(dict.fromkeys((request.query, *request.filename_hints, *request.title_hints)))
    return json.dumps(
        [{"exact": item, "like": _like_escape(item)} for item in values],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _select_rows(
    conn: sqlite3.Connection,
    sql: str,
    parameters: tuple[object, ...],
) -> list[dict[str, Any]]:
    try:
        cursor = conn.execute(sql, parameters)
        return [_row(cursor, item) for item in cursor.fetchall()]
    except sqlite3.Error:
        raise _fail("archive document storage read is unavailable") from None


def _dense_chunk_slice(row: dict[str, Any]) -> tuple[str, int, int] | None:
    """Return one exact, bounded body slice with its absolute source offsets."""

    body = row.get("dense_chunk_body")
    start = row.get("dense_start_char")
    end = row.get("dense_end_char")
    if (
        type(body) is not str
        or type(start) is not int
        or type(end) is not int
        or not 0 <= start < end
        or len(body) != end - start
    ):
        return None
    return body, start, end


def _dense_chunk_text(row: dict[str, Any], projection: ArchiveDenseQueryProjection) -> str | None:
    """Rebuild the exact text whose hash is stored beside one chunk vector."""

    match = re.fullmatch(r"v2:([1-9][0-9]*):([0-9]+):([1-9][0-9]*)", projection.chunk_scheme)
    if match is None:
        return None
    max_chars, overlap_chars, max_chunks = (int(item) for item in match.groups())
    if max_chars > 1_000_000 or overlap_chars >= max_chars or max_chunks > 1_000_000:
        return None
    selected = _dense_chunk_slice(row)
    if selected is None:
        return None
    body, _start, _end = selected
    chunk_index = row.get("dense_chunk_index")
    if type(chunk_index) is not int or not 0 <= chunk_index < max_chunks:
        return None
    header = row.get("dense_header")
    if type(header) is not str or len(header) > max(0, max_chars // 4):
        return None
    return f"{header}\n\n{body}"


def _dense_score(blob: object, projection: ArchiveDenseQueryProjection) -> float | None:
    if type(blob) is not bytes or len(blob) != projection.dimensions * 4:
        return None
    values = array.array("f")
    try:
        values.frombytes(blob)
    except (BufferError, EOFError, MemoryError, ValueError):
        return None
    if len(values) != projection.dimensions or any(not math.isfinite(value) for value in values):
        return None
    query_norm = math.sqrt(sum(value * value for value in projection.query_vector))
    vector_norm = math.sqrt(sum(float(value) * float(value) for value in values))
    if query_norm <= 0.0 or vector_norm <= 0.0:
        return None
    score = sum(
        query * float(value) for query, value in zip(projection.query_vector, values, strict=True)
    ) / (query_norm * vector_norm)
    return score if math.isfinite(score) else None


def _dense_excerpt(row: dict[str, Any]) -> tuple[str, int, int] | None:
    selected = _dense_chunk_slice(row)
    if selected is None:
        return None
    body, absolute_start, absolute_end = selected
    # Archive passages cannot contain control characters.  Extracted documents
    # commonly contain newlines, so rejecting the whole winning chunk would make
    # dense recall disappear on ordinary PDFs.  Select the longest exact bounded
    # control-free run; ties stay at the earliest stable offset.
    best_start = best_end = 0
    run_start = 0
    for index in range(0, len(body) + 1):
        if index < len(body) and not unicodedata.category(body[index]).startswith("C"):
            continue
        run_end = index
        while run_start < run_end and body[run_start].isspace():
            run_start += 1
        while run_end > run_start and body[run_end - 1].isspace():
            run_end -= 1
        if run_end - run_start > best_end - best_start:
            best_start, best_end = run_start, run_end
        run_start = index + 1
    excerpt_start = best_start
    excerpt_end = min(best_end, excerpt_start + _MAX_EXCERPT_CHARS)
    while excerpt_end > excerpt_start and body[excerpt_end - 1].isspace():
        excerpt_end -= 1
    text = body[excerpt_start:excerpt_end]
    if (
        not text
        or text != text.strip()
        or any(unicodedata.category(character).startswith("C") for character in text)
    ):
        return None
    start = absolute_start + excerpt_start
    end = absolute_start + excerpt_end
    if not absolute_start <= start < end <= absolute_end:
        return None
    return text, start, end


def _dense_header_from_prefixes(
    prefixes: tuple[str, str, str],
    *,
    maximum: int,
) -> str | None:
    """Rebuild the exact code-owned header from bounded field prefixes."""

    if type(maximum) is not int or not 0 <= maximum <= 250_000:
        return None
    if maximum == 0:
        return ""
    included: list[str] = []
    for prefix in prefixes:
        if type(prefix) is not str or len(prefix) > maximum + 1:
            return None
        material = " ".join(included)
        if len(material) >= maximum:
            break
        truncated = len(prefix) == maximum + 1
        if truncated and not prefix.strip():
            # The unseen suffix could either keep this field blank (excluded)
            # or make it nonblank (included); fail closed instead of guessing.
            return None
        if prefix.strip():
            included.append(prefix)
    return " ".join(included)[:maximum]


def _read_dense_chunk_body(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    *,
    corpus: ArchiveSearchCorpus,
    max_bytes: int,
) -> tuple[str, int, str | None, str, str | None, str | None] | None:
    """Read one exact winning span after body-free authority admission.

    The lead row binds both parent revisions and the embedding child.  This
    second SELECT repeats those immutable identities in the same snapshot and
    returns only ``[start:end]``.  For documents, the promoted KO and Raw slices
    must still be byte-for-byte equal before the Raw span can be cited; neither
    complete TEXT value is projected or compared.
    """

    raw_rowid = source.get("raw_rowid")
    raw_id = source.get("raw_id")
    user_id = source.get("user_id")
    raw_version = source.get("raw_version")
    raw_digest = source.get("content_hash")
    knowledge_rowid = source.get("knowledge_rowid")
    knowledge_id = source.get("knowledge_id")
    knowledge_version = source.get("knowledge_version")
    start = source.get("dense_start_char")
    end = source.get("dense_end_char")
    if (
        type(max_bytes) is not int
        or max_bytes <= 0
        or type(raw_rowid) is not int
        or raw_rowid <= 0
        or type(raw_id) is not str
        or _RAW_ID.fullmatch(raw_id) is None
        or type(user_id) is not str
        or not user_id
        or type(raw_version) is not int
        or raw_version < 1
        or type(raw_digest) is not str
        or _SHA256.fullmatch(raw_digest) is None
        or type(knowledge_rowid) is not int
        or knowledge_rowid <= 0
        or type(knowledge_id) is not str
        or _KO_ID.fullmatch(knowledge_id) is None
        or type(knowledge_version) is not int
        or knowledge_version < 1
        or type(start) is not int
        or type(end) is not int
        or not 0 <= start < end
        or end - start > 1_000_000
    ):
        return None
    span_chars = end - start
    header_char_cap = source.get("dense_header_char_cap")
    if type(header_char_cap) is not int or not 0 <= header_char_cap <= 250_000:
        return None
    header_prefix_chars = header_char_cap + 1
    title_prefix_chars = max(header_char_cap, 260) + 1
    full_focus_required = bool(
        corpus is ArchiveSearchCorpus.DOCUMENTS and source.get("dense_full_focus_required") is True
    )
    full_focus_cap = min(_DENSE_CHUNK_BODY_MAX_BYTES, max_bytes)
    if type(full_focus_cap) is not int or full_focus_cap <= 0:
        return None
    focus_source_expression = "substr(CAST(r.raw_content AS BLOB),1,?)" if full_focus_required else "x''"
    focus_source_parameters: tuple[object, ...] = (full_focus_cap + 1,) if full_focus_required else ()
    focus_source_guard = (
        "AND length(CAST(r.raw_content AS BLOB)) BETWEEN 1 AND ?" if full_focus_required else ""
    )
    focus_source_guard_parameters: tuple[object, ...] = (full_focus_cap,) if full_focus_required else ()
    focus_source_min_bytes = 1 if full_focus_required else 0
    focus_source_max_bytes = full_focus_cap if full_focus_required else 0
    selected_body = "r.raw_content" if corpus is ArchiveSearchCorpus.DOCUMENTS else "k.content"
    if corpus is ArchiveSearchCorpus.DOCUMENTS:
        representation_guard = """AND typeof(r.raw_content)='text'
              AND typeof(k.content)='text'
              AND substr(k.content,?,?) IS substr(r.raw_content,?,?)"""
        representation_parameters: tuple[object, ...] = (
            start + 1,
            span_chars,
            start + 1,
            span_chars,
        )
    else:
        representation_guard = "AND typeof(k.content)='text'"
        representation_parameters = ()
    rows = _select_rows(
        conn,
        f"""WITH bounded_material AS MATERIALIZED (
                   SELECT substr({selected_body},?,?) AS dense_chunk_body,
                          substr(COALESCE(k.title,''),1,?) AS dense_title_prefix,
                          substr(COALESCE(k.summary,''),1,?) AS dense_summary_prefix,
                          substr(COALESCE(k.knowledge_kind,''),1,?) AS dense_kind_prefix,
                          {focus_source_expression} AS dense_focus_source_blob
                     FROM raw_objects r
                     JOIN knowledge_objects k
                       ON k.rowid=? AND k.id=? AND k.user_id=?
                      AND k.raw_object_id=r.id AND k.deleted_at IS NULL
                      AND k.version=?
                    WHERE r.rowid=? AND r.id=? AND r.user_id=?
                      AND r.deleted_at IS NULL AND r.version=? AND r.content_hash=?
                      {focus_source_guard}
                      {representation_guard}
                      AND (k.title IS NULL OR typeof(k.title)='text')
                      AND (k.summary IS NULL OR typeof(k.summary)='text')
                      AND (k.knowledge_kind IS NULL OR typeof(k.knowledge_kind)='text')
               )
               SELECT *,
                      length(CAST(dense_chunk_body AS BLOB))
                      + length(CAST(dense_title_prefix AS BLOB))
                      + length(CAST(dense_summary_prefix AS BLOB))
                      + length(CAST(dense_kind_prefix AS BLOB))
                      + length(dense_focus_source_blob) AS dense_material_bytes
                 FROM bounded_material
                WHERE length(CAST(dense_chunk_body AS BLOB)) BETWEEN 1 AND ?
                  AND length(dense_focus_source_blob) BETWEEN ? AND ?
                  AND length(CAST(dense_chunk_body AS BLOB))
                      + length(CAST(dense_title_prefix AS BLOB))
                      + length(CAST(dense_summary_prefix AS BLOB))
                      + length(CAST(dense_kind_prefix AS BLOB))
                      + length(dense_focus_source_blob)
                      BETWEEN 1 AND ?""",  # nosec B608 -- fixed code-owned expressions
        (
            start + 1,
            span_chars,
            title_prefix_chars,
            header_prefix_chars,
            header_prefix_chars,
            *focus_source_parameters,
            knowledge_rowid,
            knowledge_id,
            user_id,
            knowledge_version,
            raw_rowid,
            raw_id,
            user_id,
            raw_version,
            raw_digest,
            *focus_source_guard_parameters,
            *representation_parameters,
            max_bytes,
            focus_source_min_bytes,
            focus_source_max_bytes,
            max_bytes,
        ),
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise _fail("archive dense chunk selection is invalid")
    body = rows[0].get("dense_chunk_body")
    material_bytes = rows[0].get("dense_material_bytes")
    title_prefix = rows[0].get("dense_title_prefix")
    summary_prefix = rows[0].get("dense_summary_prefix")
    kind_prefix = rows[0].get("dense_kind_prefix")
    focus_source_blob = rows[0].get("dense_focus_source_blob")
    if (
        type(body) is not str
        or len(body) != span_chars
        or type(material_bytes) is not int
        or not 1 <= material_bytes <= max_bytes
        or type(title_prefix) is not str
        or len(title_prefix) > title_prefix_chars
        or type(summary_prefix) is not str
        or len(summary_prefix) > header_prefix_chars
        or type(kind_prefix) is not str
        or len(kind_prefix) > header_prefix_chars
        or type(focus_source_blob) is not bytes
        or not focus_source_min_bytes <= len(focus_source_blob) <= focus_source_max_bytes
    ):
        raise _fail("archive dense chunk selection is invalid")
    try:
        decoded_focus_source = focus_source_blob.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    full_focus_source: str | None = decoded_focus_source if full_focus_required else None
    full_focus_text_digest: str | None = None
    if full_focus_required:
        full_focus_text_digest = hashlib.sha256(focus_source_blob).hexdigest()
    try:
        exact_size = sum(
            len(item.encode("utf-8", errors="strict"))
            for item in (body, title_prefix, summary_prefix, kind_prefix)
        ) + len(focus_source_blob)
    except UnicodeEncodeError:
        return None
    if exact_size != material_bytes:
        raise _fail("archive dense chunk size is invalid")
    header = _dense_header_from_prefixes(
        (
            title_prefix[:header_prefix_chars],
            summary_prefix,
            kind_prefix,
        ),
        maximum=header_char_cap,
    )
    if header is None:
        return None
    title = title_prefix if len(title_prefix) <= 260 else None
    return body, material_bytes, title, header, full_focus_source, full_focus_text_digest


def _dense_rows(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
    request: ArchiveSearchRequest,
    corpus: ArchiveSearchCorpus,
    projection: ArchiveDenseQueryProjection,
) -> tuple[int, int, bool, bool, bool, list[dict[str, Any]]]:
    """Reauthorize and re-score the bounded dense selection in this snapshot."""

    scheme_match = re.fullmatch(
        r"v2:([1-9][0-9]*):([0-9]+):([1-9][0-9]*)",
        projection.chunk_scheme,
    )
    if scheme_match is None:
        raise _fail("archive dense chunk scheme is unavailable")
    max_chars, overlap_chars, max_chunks = (int(item) for item in scheme_match.groups())
    if max_chars > 1_000_000 or overlap_chars >= max_chars or max_chunks > 1_000_000:
        raise _fail("archive dense chunk scheme is unavailable")
    header_char_cap = max_chars // 4
    source_cte, scope_parameters = _source_cte(
        corpus,
        request,
        include_body=False,
        bounded_material=True,
    )
    authority_parameters = (*_authority_parameters(tenant_id, owner_id), *scope_parameters)
    totals = _select_rows(
        conn,
        f"""WITH {source_cte}
            SELECT (SELECT COUNT(DISTINCT raw_id) FROM authorized_sources) AS total,
                   CASE WHEN (SELECT value FROM authority_backfill)=1
                              OR (SELECT value FROM lane_backfill)=1
                        THEN 1 ELSE 0 END AS authority_backfill""",
        authority_parameters,
    )
    if (
        len(totals) != 1
        or type(totals[0].get("total")) is not int
        or int(totals[0]["total"]) < 0
        or type(totals[0].get("authority_backfill")) is not int
        or totals[0]["authority_backfill"] not in {0, 1}
    ):
        raise _fail("archive dense coverage is unavailable")
    total = int(totals[0]["total"])
    authority_backfill = totals[0].get("authority_backfill") == 1
    if not projection.candidates:
        return total, 0, authority_backfill, False, False, []
    candidate_json = json.dumps(
        [[item.knowledge_object_id, item.chunk_index] for item in projection.candidates],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    document_temporal_projection = ", s.raw_document_date" if corpus is ArchiveSearchCorpus.DOCUMENTS else ""
    rows = _select_rows(
        conn,
        f"""WITH {source_cte},
            dense_candidates AS MATERIALIZED (
                SELECT CAST(key AS INTEGER) AS admitted_rank,
                       json_extract(value,'$[0]') AS knowledge_id,
                       json_extract(value,'$[1]') AS chunk_index
                  FROM json_each(?)
                 WHERE type='array'
                   AND json_type(value,'$[0]')='text'
                   AND json_type(value,'$[1]')='integer'
            )
            SELECT s.raw_rowid, s.raw_id, s.user_id,
                   substr(s.source,1,64) AS source,
                   substr(s.content_type,1,64) AS content_type,
                   s.content_hash, s.raw_version,
                   s.raw_received_at{document_temporal_projection},
                   s.inbox_id, s.inbox_status,
                   s.knowledge_rowid, s.knowledge_id, s.knowledge_version,
                   s.knowledge_lifecycle, s.knowledge_created_at,
                   s.knowledge_updated_at, s.lifecycle_state, s.review_state,
                   s.raw_filename AS filename, d.admitted_rank,
                   e.chunk_index AS dense_chunk_index,
                   e.start_char AS dense_start_char,
                   e.end_char AS dense_end_char,
                   e.content_hash AS dense_content_hash,
                   e.vector AS dense_vector
              FROM dense_candidates d
              JOIN authorized_sources s ON s.knowledge_id=d.knowledge_id
              JOIN knowledge_chunk_embeddings e
                ON e.knowledge_object_id=s.knowledge_id
               AND e.chunk_index=d.chunk_index
               AND e.user_id=? AND e.model=? AND e.dim=?
               AND e.source_version=s.knowledge_version
               AND e.chunk_scheme=?
               AND typeof(e.vector)='blob' AND length(e.vector)=?
               AND typeof(e.content_hash)='text'
               AND length(e.content_hash)=64
               AND e.content_hash NOT GLOB '*[^0-9a-f]*'
               AND typeof(e.start_char)='integer' AND e.start_char>=0
               AND typeof(e.end_char)='integer'
               AND e.end_char>e.start_char
               AND e.end_char-e.start_char<=1000000
             ORDER BY d.admitted_rank, s.raw_id""",
        (
            *authority_parameters,
            candidate_json,
            tenant_id,
            projection.model_id,
            projection.dimensions,
            projection.chunk_scheme,
            projection.dimensions * 4,
        ),
    )
    ranked_leads: list[dict[str, Any]] = []
    for row in rows:
        score = _dense_score(row.get("dense_vector"), projection)
        digest = row.get("dense_content_hash")
        start = row.get("dense_start_char")
        end = row.get("dense_end_char")
        raw_id = row.get("raw_id")
        if (
            score is None
            or type(digest) is not str
            or _SHA256.fullmatch(digest) is None
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end
            or end - start > 1_000_000
            or type(raw_id) is not str
            or _RAW_ID.fullmatch(raw_id) is None
        ):
            continue
        row["dense_score"] = score
        row["dense_header_char_cap"] = header_char_cap
        row["dense_full_focus_required"] = bool(corpus is ArchiveSearchCorpus.DOCUMENTS and request.focus)
        ranked_leads.append(row)
    ranked_leads.sort(
        key=lambda item: (
            -float(item["dense_score"]),
            int(item.get("admitted_rank") or 0),
            str(item.get("knowledge_id") or ""),
            str(item.get("raw_id") or ""),
        )
    )
    # The private plan may contain more than one passage from the same source.
    # Only the best authenticated vector may trigger a body read.
    winning_leads: list[dict[str, Any]] = []
    selected_sources: set[str] = set()
    for row in ranked_leads:
        raw_id = str(row["raw_id"])
        if raw_id in selected_sources:
            continue
        selected_sources.add(raw_id)
        winning_leads.append(row)

    body_max_bytes = _DENSE_CHUNK_BODY_MAX_BYTES
    body_budget_bytes = _DENSE_CHUNK_BODY_BUDGET_BYTES
    if (
        type(body_max_bytes) is not int
        or type(body_budget_bytes) is not int
        or not 1 <= body_max_bytes <= body_budget_bytes
    ):
        raise _fail("archive dense chunk budget is invalid")
    valid: list[dict[str, Any]] = []
    examined_sources: set[str] = set()
    body_budget_remaining = body_budget_bytes
    # Invalid/stale derivative rows are ordinary dense misses.  Only a source
    # skipped by the explicit read ceilings makes this lane unavailable.
    body_budget_incomplete = False
    body_recall_capped = False
    for row in winning_leads:
        if body_budget_remaining <= 0:
            body_budget_incomplete = True
            body_recall_capped = True
            break
        attempt_cap = min(body_max_bytes, body_budget_remaining)
        body_budget_remaining -= attempt_cap
        selected_body = _read_dense_chunk_body(
            conn,
            row,
            corpus=corpus,
            max_bytes=attempt_cap,
        )
        if selected_body is None:
            body_budget_incomplete = True
            body_recall_capped = True
            continue
        body, consumed_bytes, title, header, full_focus_source, full_focus_text_digest = selected_body
        body_budget_remaining += attempt_cap - consumed_bytes
        row["dense_chunk_body"] = body
        row["knowledge_title"] = title
        row["dense_header"] = header
        if type(full_focus_source) is str:
            # The complete source is admitted only for focused documents and is
            # already charged to the closed dense material budget.  Retain it
            # privately so a stored-passage topology can be authenticated against
            # the exact current code-owned projection before minting a v2 locator.
            row["dense_full_focus_source"] = full_focus_source
            row["dense_full_focus_char_count"] = len(full_focus_source)
            row["dense_full_focus_text_sha256"] = full_focus_text_digest
        expected = _dense_chunk_text(row, projection)
        digest = row.get("dense_content_hash")
        if expected is None or not hmac.compare_digest(
            hashlib.sha256(expected.encode("utf-8", errors="strict")).hexdigest(),
            str(digest),
        ):
            continue
        raw_id = row.get("raw_id")
        if type(raw_id) is str:
            examined_sources.add(raw_id)
        if float(row["dense_score"]) < projection.minimum_score:
            continue
        if _dense_excerpt(row) is None:
            continue
        source_focus_projection: SourceFocusProjection | None = None
        if corpus is ArchiveSearchCorpus.DOCUMENTS and request.focus:
            dense_start = row.get("dense_start_char")
            dense_end = row.get("dense_end_char")
            if (
                type(dense_start) is not int
                or type(dense_end) is not int
                or type(full_focus_source) is not str
            ):
                continue
            try:
                source_focus_projection = _project_focused_dense_source(
                    full_focus_source,
                    body,
                    request.query,
                    request.focus,
                    dense_start=dense_start,
                    dense_end=dense_end,
                    max_chars=_MAX_EXCERPT_CHARS,
                )
            except (TypeError, ValueError, UnicodeError):
                source_focus_projection = None
            if source_focus_projection is None:
                continue
            if (
                not dense_start <= source_focus_projection.start < source_focus_projection.end <= dense_end
                or source_focus_projection.end - source_focus_projection.start > _MAX_EXCERPT_CHARS
                or source_focus_projection.excerpt
                != body[
                    source_focus_projection.start - dense_start : source_focus_projection.end - dense_start
                ]
            ):
                continue
        if source_focus_projection is not None:
            row["source_focus_projection"] = source_focus_projection
        valid.append(row)
    valid.sort(
        key=lambda item: (
            (
                0
                if type(item.get("source_focus_projection")) is SourceFocusProjection
                and item["source_focus_projection"].focus_match_kind is SourceFocusMatchKind.FULL
                else 1
            ),
            -(
                item["source_focus_projection"].matched_focus_count
                if type(item.get("source_focus_projection")) is SourceFocusProjection
                else 0
            ),
            -float(item["dense_score"]),
            str(item.get("knowledge_id") or ""),
            str(item.get("raw_id") or ""),
        )
    )
    best_by_source: dict[str, dict[str, Any]] = {}
    for item in valid:
        best_by_source.setdefault(str(item["raw_id"]), item)
    valid = list(best_by_source.values())
    for lane_rank, row in enumerate(valid, 1):
        row["lane_rank"] = lane_rank
    return (
        total,
        len(examined_sources),
        authority_backfill,
        body_budget_incomplete,
        body_recall_capped,
        valid,
    )


def _select_bounded_document_passage_rows(
    conn: sqlite3.Connection,
    *,
    raw_object_id: str,
    source_version: int,
    source_digest: str,
    extracted_digest: str,
    source_char_count: int,
    index_revision: str,
) -> list[dict[str, Any]]:
    """Enumerate one complete child topology without projecting corrupt TEXT."""

    return _select_rows(
        conn,
        """SELECT projection.raw_object_id AS projection_raw_object_id,
                      projection.source_version AS projection_source_version,
                      projection.source_content_sha256 AS projection_source_content_sha256,
                      projection.extracted_text_sha256 AS projection_extracted_text_sha256,
                      projection.source_char_count AS projection_source_char_count,
                      substr(CASE WHEN typeof(projection.passage_set_sha256)='text'
                                  THEN projection.passage_set_sha256 ELSE '' END,1,65)
                          AS projection_passage_set_sha256,
                      projection.passage_index_revision AS projection_passage_index_revision,
                      projection.projection_status AS projection_status,
                      CASE WHEN projection.incomplete_reason IS NULL THEN NULL ELSE 'invalid' END
                          AS projection_incomplete_reason,
                      projection.passage_count AS projection_passage_count,
                      CASE WHEN typeof(passage.chunk_index)='integer'
                           THEN passage.chunk_index END AS passage_chunk_index,
                      CASE WHEN typeof(passage.start_char)='integer'
                           THEN passage.start_char END AS passage_start_char,
                      CASE WHEN typeof(passage.end_char)='integer'
                           THEN passage.end_char END AS passage_end_char,
                      substr(CASE WHEN typeof(passage.content_sha256)='text'
                                  THEN passage.content_sha256 ELSE '' END,1,65)
                          AS passage_content_sha256
                 FROM document_passage_projections projection
                 JOIN document_passages passage
                   ON passage.raw_object_id=projection.raw_object_id
                WHERE projection.raw_object_id=?
                  AND projection.source_version=?
                  AND projection.source_content_sha256=?
                  AND projection.extracted_text_sha256=?
                  AND projection.source_char_count=?
                  AND projection.passage_index_revision=?
                  AND projection.projection_status='current'
                  AND projection.incomplete_reason IS NULL
                  AND typeof(projection.source_version)='integer'
                  AND typeof(projection.source_content_sha256)='text'
                  AND typeof(projection.extracted_text_sha256)='text'
                  AND typeof(projection.source_char_count)='integer'
                  AND typeof(projection.passage_index_revision)='text'
                  AND typeof(projection.projection_status)='text'
                  AND typeof(projection.passage_count)='integer'
                  AND projection.passage_count BETWEEN 1 AND ?
                LIMIT ?""",
        (
            raw_object_id,
            source_version,
            source_digest,
            extracted_digest,
            source_char_count,
            index_revision,
            _DOCUMENT_PASSAGE_MAX_COUNT,
            _DOCUMENT_PASSAGE_MAX_COUNT + 1,
        ),
    )


def _select_current_document_passages(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    contract: _DocumentPassageContract,
) -> tuple[_StoredDocumentPassage, ...] | None:
    """Read and authenticate one exact code-owned current child set."""

    raw_object_id = source.get("raw_id")
    source_version = source.get("raw_version")
    source_digest = source.get("content_hash")
    body = source.get("passage_body")
    if (
        type(raw_object_id) is not str
        or _RAW_ID.fullmatch(raw_object_id) is None
        or type(source_version) is not int
        or source_version < 1
        or type(source_digest) is not str
        or _SHA256.fullmatch(source_digest) is None
        or type(body) is not str
        or not body
    ):
        return None
    try:
        body_digest = hashlib.sha256(body.encode("utf-8", errors="strict")).hexdigest()
    except UnicodeEncodeError:
        return None
    rows = _select_bounded_document_passage_rows(
        conn,
        raw_object_id=raw_object_id,
        source_version=source_version,
        source_digest=source_digest,
        extracted_digest=body_digest,
        source_char_count=len(body),
        index_revision=contract.index_revision,
    )
    if not rows:
        return None
    if len(rows) > _DOCUMENT_PASSAGE_MAX_COUNT or any(
        type(row.get("passage_chunk_index")) is not int for row in rows
    ):
        return None
    rows.sort(key=lambda row: int(row["passage_chunk_index"]))
    parent = rows[0]
    passage_count = parent.get("projection_passage_count")
    if (
        parent.get("projection_raw_object_id") != raw_object_id
        or parent.get("projection_source_version") != source_version
        or parent.get("projection_source_content_sha256") != source_digest
        or parent.get("projection_extracted_text_sha256") != body_digest
        or parent.get("projection_source_char_count") != len(body)
        or parent.get("projection_passage_index_revision") != contract.index_revision
        or parent.get("projection_status") != "current"
        or parent.get("projection_incomplete_reason") is not None
        or type(passage_count) is not int
        or passage_count != len(rows)
        or not 1 <= passage_count <= _DOCUMENT_PASSAGE_MAX_COUNT
        or type(parent.get("projection_passage_set_sha256")) is not str
        or _SHA256.fullmatch(str(parent["projection_passage_set_sha256"])) is None
    ):
        return None
    parent_keys = (
        "projection_raw_object_id",
        "projection_source_version",
        "projection_source_content_sha256",
        "projection_extracted_text_sha256",
        "projection_source_char_count",
        "projection_passage_set_sha256",
        "projection_passage_index_revision",
        "projection_status",
        "projection_incomplete_reason",
        "projection_passage_count",
    )
    passages: list[_StoredDocumentPassage] = []
    digest_rows: list[tuple[int, int, int, str]] = []
    previous: _StoredDocumentPassage | None = None
    for expected_index, row in enumerate(rows):
        if any(row.get(key) != parent.get(key) for key in parent_keys):
            return None
        chunk_index = row.get("passage_chunk_index")
        start_char = row.get("passage_start_char")
        end_char = row.get("passage_end_char")
        content_digest = row.get("passage_content_sha256")
        if (
            type(chunk_index) is not int
            or chunk_index != expected_index
            or type(start_char) is not int
            or type(end_char) is not int
            or not 0 <= start_char < end_char <= len(body)
            or type(content_digest) is not str
            or _SHA256.fullmatch(content_digest) is None
        ):
            return None
        passage = _StoredDocumentPassage(
            chunk_index,
            start_char,
            end_char,
            content_digest,
        )
        if previous is not None and (
            passage.start_char <= previous.start_char
            or passage.start_char > previous.end_char
            or passage.end_char <= previous.end_char
        ):
            return None
        try:
            exact_digest = hashlib.sha256(
                body[start_char:end_char].encode("utf-8", errors="strict")
            ).hexdigest()
        except UnicodeEncodeError:
            return None
        if not hmac.compare_digest(exact_digest, content_digest):
            return None
        passages.append(passage)
        digest_rows.append((chunk_index, start_char, end_char, content_digest))
        previous = passage
    if passages[0].start_char != 0 or passages[-1].end_char != len(body):
        return None
    canonical_rows = tuple(digest_rows)
    try:
        set_digest = contract.set_sha256(canonical_rows)
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return None
    if (
        type(set_digest) is not str
        or _SHA256.fullmatch(set_digest) is None
        or not hmac.compare_digest(set_digest, str(parent["projection_passage_set_sha256"]))
    ):
        return None
    try:
        exact_current_projection = contract.rows_match_current_projection(
            body,
            body_digest,
            canonical_rows,
        )
    except Exception:
        return None
    if exact_current_projection is not True:
        return None
    return tuple(passages)


def _select_current_dense_document_passage(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    contract: _DocumentPassageContract,
    *,
    start: int,
    end: int,
) -> tuple[_StoredDocumentPassage, ...] | None:
    """Authenticate only the stored passage containing one dense excerpt.

    The focused dense admission already loaded and charged the complete Raw
    document under its 1 MiB ceiling.  This path validates the body-free child
    topology and then authenticates it against that exact bounded source.
    """

    raw_object_id = source.get("raw_id")
    source_version = source.get("raw_version")
    source_digest = source.get("content_hash")
    source_char_count = source.get("dense_full_focus_char_count")
    extracted_digest = source.get("dense_full_focus_text_sha256")
    full_source = source.get("dense_full_focus_source")
    if (
        type(raw_object_id) is not str
        or _RAW_ID.fullmatch(raw_object_id) is None
        or type(source_version) is not int
        or source_version < 1
        or type(source_digest) is not str
        or _SHA256.fullmatch(source_digest) is None
        or type(source_char_count) is not int
        or not 1 <= source_char_count <= _DENSE_CHUNK_BODY_MAX_BYTES
        or type(extracted_digest) is not str
        or _SHA256.fullmatch(extracted_digest) is None
        or type(full_source) is not str
        or len(full_source) != source_char_count
        or type(start) is not int
        or type(end) is not int
        or not 0 <= start < end
    ):
        return None
    rows = _select_bounded_document_passage_rows(
        conn,
        raw_object_id=raw_object_id,
        source_version=source_version,
        source_digest=source_digest,
        extracted_digest=extracted_digest,
        source_char_count=source_char_count,
        index_revision=contract.index_revision,
    )
    if not rows:
        return None
    if len(rows) > _DOCUMENT_PASSAGE_MAX_COUNT or any(
        type(row.get("passage_chunk_index")) is not int for row in rows
    ):
        return None
    rows.sort(key=lambda row: int(row["passage_chunk_index"]))
    parent = rows[0]
    passage_count = parent.get("projection_passage_count")
    if (
        parent.get("projection_raw_object_id") != raw_object_id
        or parent.get("projection_source_version") != source_version
        or parent.get("projection_source_content_sha256") != source_digest
        or parent.get("projection_extracted_text_sha256") != extracted_digest
        or parent.get("projection_source_char_count") != source_char_count
        or parent.get("projection_passage_index_revision") != contract.index_revision
        or parent.get("projection_status") != "current"
        or parent.get("projection_incomplete_reason") is not None
        or type(passage_count) is not int
        or passage_count != len(rows)
        or not 1 <= passage_count <= _DOCUMENT_PASSAGE_MAX_COUNT
        or type(parent.get("projection_source_char_count")) is not int
        or type(parent.get("projection_passage_set_sha256")) is not str
        or _SHA256.fullmatch(str(parent["projection_passage_set_sha256"])) is None
    ):
        return None
    parent_keys = (
        "projection_raw_object_id",
        "projection_source_version",
        "projection_source_content_sha256",
        "projection_extracted_text_sha256",
        "projection_source_char_count",
        "projection_passage_set_sha256",
        "projection_passage_index_revision",
        "projection_status",
        "projection_incomplete_reason",
        "projection_passage_count",
    )
    source_char_count = int(parent["projection_source_char_count"])
    digest_rows: list[tuple[int, int, int, str]] = []
    passages: list[_StoredDocumentPassage] = []
    previous: _StoredDocumentPassage | None = None
    selected: _StoredDocumentPassage | None = None
    for expected_index, row in enumerate(rows):
        if any(row.get(key) != parent.get(key) for key in parent_keys):
            return None
        chunk_index = row.get("passage_chunk_index")
        passage_start = row.get("passage_start_char")
        passage_end = row.get("passage_end_char")
        content_digest = row.get("passage_content_sha256")
        if (
            type(chunk_index) is not int
            or chunk_index != expected_index
            or type(passage_start) is not int
            or type(passage_end) is not int
            or not 0 <= passage_start < passage_end <= source_char_count
            or type(content_digest) is not str
            or _SHA256.fullmatch(content_digest) is None
        ):
            return None
        passage = _StoredDocumentPassage(chunk_index, passage_start, passage_end, content_digest)
        if previous is not None and (
            passage.start_char <= previous.start_char
            or passage.start_char > previous.end_char
            or passage.end_char <= previous.end_char
        ):
            return None
        if passage.start_char <= start < end <= passage.end_char:
            if selected is not None:
                return None
            selected = passage
        passages.append(passage)
        digest_rows.append((chunk_index, passage_start, passage_end, content_digest))
        previous = passage
    if passages[0].start_char != 0 or passages[-1].end_char != source_char_count or selected is None:
        return None
    try:
        set_digest = contract.set_sha256(tuple(digest_rows))
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return None
    if not hmac.compare_digest(set_digest, str(parent["projection_passage_set_sha256"])):
        return None
    try:
        exact_current_projection = contract.rows_match_current_projection(
            full_source,
            extracted_digest,
            tuple(digest_rows),
        )
    except Exception:
        return None
    if exact_current_projection is not True:
        return None
    try:
        exact_digest = hashlib.sha256(
            full_source[selected.start_char : selected.end_char].encode(
                "utf-8",
                errors="strict",
            )
        ).hexdigest()
    except UnicodeEncodeError:
        return None
    if not hmac.compare_digest(exact_digest, selected.content_sha256):
        return None
    return (selected,)


def _summary(
    rows: list[dict[str, Any]],
) -> tuple[int, int, int, int, int, int, int, list[dict[str, Any]]]:
    if not rows:
        raise _fail("archive document storage summary is unavailable")
    try:
        values = tuple(
            rows[0][key]
            for key in (
                "total",
                "examined",
                "matched",
                "derivative_mismatches",
                "derivative_backfills",
                "derivative_unavailable",
                "authority_backfill",
            )
        )
    except KeyError:
        raise _fail("archive document storage summary is invalid") from None
    if any(type(item) is not int for item in values):
        raise _fail("archive document storage summary is invalid")
    (
        total,
        examined,
        matched,
        derivative_mismatches,
        derivative_backfills,
        derivative_unavailable,
        authority_backfill,
    ) = values
    hits = [item for item in rows if item.get("source_id") is not None]
    if (
        min(
            total,
            examined,
            matched,
            derivative_mismatches,
            derivative_backfills,
            derivative_unavailable,
            authority_backfill,
        )
        < 0
        or examined > total
        or matched > examined
        or derivative_mismatches > examined
        or derivative_backfills > derivative_mismatches
        or derivative_unavailable > derivative_mismatches
        or authority_backfill not in {0, 1}
    ):
        raise _fail("archive document storage summary is invalid")
    return (
        total,
        examined,
        matched,
        derivative_mismatches,
        derivative_backfills,
        derivative_unavailable,
        authority_backfill,
        hits,
    )


def _page_select() -> str:
    return """SELECT totals.total, totals.examined, totals.matched,
                     totals.derivative_mismatches, totals.derivative_backfills,
                     totals.derivative_unavailable, totals.authority_backfill,
                     page.*
                FROM (
                    SELECT (SELECT COUNT(DISTINCT raw_id) FROM eligible_sources) AS total,
                           (SELECT COUNT(DISTINCT raw_id) FROM indexed_sources) AS examined,
                           (SELECT COUNT(*) FROM ranked) AS matched,
                           (SELECT value FROM derivative_mismatches) AS derivative_mismatches,
                           (SELECT value FROM derivative_backfills) AS derivative_backfills,
                           (SELECT value FROM derivative_unavailable) AS derivative_unavailable,
                           CASE WHEN (SELECT value FROM authority_backfill)=1
                                      OR (SELECT value FROM lane_backfill)=1
                                THEN 1 ELSE 0 END AS authority_backfill
                ) totals
                LEFT JOIN page ON 1=1
               ORDER BY CASE WHEN page.lane_rank IS NULL THEN 1 ELSE 0 END,
                        page.lane_rank ASC"""


def _lexical_sql(
    corpus: ArchiveSearchCorpus,
    request: ArchiveSearchRequest,
    *,
    derivative_available: bool,
    document_passage_revision: str | None = None,
) -> tuple[str, tuple[object, ...]]:
    source_cte, scope_parameters = _source_cte(corpus, request, include_body=True)
    passage_projection_cte = _passage_projection_cte(
        corpus,
        document_passage_available=derivative_available,
        document_passage_revision=document_passage_revision,
    )
    folded_fields: tuple[str, ...]
    if corpus is ArchiveSearchCorpus.DOCUMENTS:
        # The global raw-object index intentionally contains rejected rows and
        # therefore cannot be consulted by this archive facade.  The authorized
        # body scan below remains authoritative.  The passage sidecar may
        # provide evidence identity after ranking, but cannot admit, remove or
        # reorder a candidate.
        derivative_expression = "0"
        derivative_mismatch_expression = "passage_projection_current<>1"
        derivative_backfill_expression = "passage_projection_backfill_pending=1"
        derivative_unavailable_expression = "passage_projection_unavailable=1"
        folded_fields = ("friday_archive_fold(s.passage_body)",)
        # Filename affinity only orders rows already admitted by the body-only
        # direct_match below.  Invalid metadata contributes the empty string,
        # and every canonical lexical needle must be present before a boost.
        safe_filename = (
            f"CASE WHEN {_catalog_metadata_valid('s')} THEN {_filename_expression('s')} ELSE '' END"
        )
        filename_score = f"""CASE
            WHEN EXISTS (SELECT 1 FROM lexical_needles)
             AND NOT EXISTS (
                 SELECT 1 FROM lexical_needles n
                  WHERE instr(friday_archive_fold({safe_filename}),
                              friday_archive_fold(n.value))=0
             ) THEN 0 ELSE 1 END"""
        score = """CASE WHEN instr(
            friday_archive_fold(s.passage_body),
            friday_archive_fold(?)
        )>0 THEN 0 ELSE 1 END"""
    else:
        derivative_expression = (
            "EXISTS (SELECT 1 FROM knowledge_fts "
            "WHERE knowledge_fts.rowid=s.knowledge_rowid AND knowledge_fts MATCH ?)"
            if derivative_available
            else "0"
        )
        derivative_mismatch_expression = "direct_match<>derivative_match"
        derivative_backfill_expression = derivative_mismatch_expression
        derivative_unavailable_expression = "0"
        folded_fields = (
            "friday_archive_fold(s.passage_body)",
            "friday_archive_fold(COALESCE(s.knowledge_title,''))",
            "friday_archive_fold(COALESCE(s.knowledge_summary,''))",
            "friday_archive_fold(COALESCE(s.knowledge_tags_json,''))",
        )
        filename_score = "1"
        score = """CASE
            WHEN instr(friday_archive_fold(COALESCE(s.knowledge_title,'')),
                       friday_archive_fold(?))>0 THEN 0
            WHEN instr(friday_archive_fold(s.passage_body),
                       friday_archive_fold(?))>0 THEN 1
            WHEN instr(friday_archive_fold(COALESCE(s.knowledge_summary,'')),
                       friday_archive_fold(?))>0 THEN 2
            ELSE 3 END"""
    score_parameters: tuple[object, ...] = (
        (request.query,)
        if corpus is ArchiveSearchCorpus.DOCUMENTS
        else (request.query, request.query, request.query)
    )
    direct_match = (
        "EXISTS (SELECT 1 FROM lexical_needles n WHERE "
        + " OR ".join(f"instr({field}, friday_archive_fold(n.value))>0" for field in folded_fields)
        + ")"
    )
    source_choice_order = (
        "CASE m.knowledge_lifecycle WHEN 'active' THEN 0 WHEN 'archived' THEN 1 ELSE 2 END, "
        if corpus is ArchiveSearchCorpus.KNOWLEDGE
        else ""
    )
    sql = f"""WITH {source_cte},
        {passage_projection_cte},
        lexical_needles AS MATERIALIZED (
            SELECT value
              FROM json_each(?)
             WHERE type='text' AND value<>''
        ),
        scanned_sources AS MATERIALIZED (
            SELECT s.*, s.raw_id AS source_id,
                   {filename_score} AS filename_score, {score} AS score,
                   '' AS sort_text,
                   CASE WHEN {direct_match} THEN 1 ELSE 0 END AS direct_match,
                   CASE WHEN {derivative_expression} THEN 1 ELSE 0 END AS derivative_match
              FROM passage_projection_sources s
        ),
        indexed_sources AS MATERIALIZED (
            SELECT * FROM scanned_sources
        ),
        matched_sources AS MATERIALIZED (
            SELECT * FROM scanned_sources WHERE direct_match=1
        ),
        source_choices AS MATERIALIZED (
            SELECT * FROM (
                SELECT m.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY m.raw_id
                           ORDER BY {source_choice_order}m.filename_score ASC,
                                    m.score ASC, m.sort_time DESC,
                                    COALESCE(m.knowledge_id,'') ASC
                       ) AS source_choice
                  FROM matched_sources m
            ) WHERE source_choice=1
        ),
        ranked AS MATERIALIZED (
            SELECT m.*,
                   ROW_NUMBER() OVER (
                       ORDER BY m.filename_score ASC, m.score ASC, m.sort_text ASC,
                                m.sort_time DESC, m.source_id ASC
                   ) AS lane_rank
              FROM source_choices m
        ),
        derivative_mismatches(value) AS MATERIALIZED (
            SELECT COUNT(*) FROM scanned_sources
             WHERE {derivative_mismatch_expression}
        ),
        derivative_backfills(value) AS MATERIALIZED (
            SELECT COUNT(*) FROM scanned_sources
             WHERE {derivative_backfill_expression}
        ),
        derivative_unavailable(value) AS MATERIALIZED (
            SELECT COUNT(*) FROM scanned_sources
             WHERE {derivative_unavailable_expression}
        ),
        page AS MATERIALIZED (
            SELECT * FROM ranked
             ORDER BY filename_score ASC, score ASC, sort_text ASC,
                      sort_time DESC, source_id ASC
             LIMIT ?
        )
        {_page_select()}"""
    terms = _fts_terms(request.query)
    match_query = " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"*'
        if not term.endswith("*")
        else f'"{term[:-1].replace(chr(34), chr(34) * 2)}"*'
        for term in terms
    )
    direct_terms = tuple(
        dict.fromkeys(
            term.rstrip("*").strip('"').strip() for term in terms if term.rstrip("*").strip('"').strip()
        )
    )
    parameters = (
        *scope_parameters,
        json.dumps(direct_terms, ensure_ascii=False, separators=(",", ":")),
        *score_parameters,
        *((match_query,) if derivative_available and corpus is ArchiveSearchCorpus.KNOWLEDGE else ()),
    )
    return sql, parameters


def _focused_document_lexical_rows(
    conn: sqlite3.Connection,
    request: ArchiveSearchRequest,
    *,
    tenant_id: str,
    owner_id: str,
    derivative_available: bool,
    document_passage_revision: str | None,
) -> tuple[int, int, int, int, int, int, bool, list[dict[str, Any]]]:
    """Use two authorized FTS leads before reading any exact source body."""

    if not request.focus or request.corpora != (ArchiveSearchCorpus.DOCUMENTS,):
        raise _fail("focused archive document request is invalid")
    if not _table_exists(conn, "raw_fts"):
        raise _fail("focused archive document lexical index is unavailable")
    source_cte, scope_parameters = _source_cte(
        ArchiveSearchCorpus.DOCUMENTS,
        request,
        include_body=False,
        bounded_material=True,
    )

    def source_term_groups(value: str) -> tuple[tuple[str, ...], ...]:
        groups: list[tuple[str, ...]] = []
        for token in source_focus_fts_tokens(value):
            normalized = unicodedata.normalize("NFKC", token).strip()
            expanded = tuple(
                dict.fromkeys(
                    (
                        *(term for group in _fts_term_groups(token) for term in group),
                        token,
                        normalized,
                    )
                )
            )
            expanded = tuple(
                term for term in expanded if term and any(character.isalnum() for character in term)
            )
            if expanded:
                groups.append(expanded)
        return tuple(groups)

    anchor_source_tokens = source_focus_fts_tokens(request.query)
    raw_focus_groups = source_term_groups(request.focus)
    raw_focus_terms = tuple(dict.fromkeys(term for group in raw_focus_groups for term in group))
    raw_anchor_groups = source_term_groups(request.query)
    compatibility_lead = any(
        token.isascii() or unicodedata.normalize("NFKC", token) != token for token in anchor_source_tokens
    )
    if (
        not raw_anchor_groups
        or len(raw_anchor_groups) > MAX_SOURCE_FOCUS_ANCHOR_TERMS
        or any(not group for group in raw_anchor_groups)
    ):
        raise _fail("focused archive document anchor budget is unavailable")
    anchor_terms = tuple(dict.fromkeys(term for group in raw_anchor_groups for term in group))

    def term_key(value: str) -> str:
        return value.rstrip("*").strip('"').strip().casefold()

    anchor_term_keys = frozenset(term_key(item) for item in anchor_terms)
    focus_terms = (
        tuple(item for item in raw_focus_terms if term_key(item) not in anchor_term_keys) or raw_focus_terms
    )

    def match_query(values: tuple[str, ...]) -> str:
        return " OR ".join(
            f'"{term_key(item).replace(chr(34), chr(34) * 2)}"*' for item in values if term_key(item)
        )

    focus_detail_query = match_query(focus_terms)
    anchor_group_queries = tuple(match_query(group) for group in raw_anchor_groups)
    anchor_match_query = " AND ".join(f"({item})" for item in anchor_group_queries if item)
    if not focus_detail_query or not anchor_match_query:
        raise _fail("focused archive document lexical query is unavailable")
    focus_match_query = f"({anchor_match_query}) AND ({focus_detail_query})"

    cap = _FOCUSED_DOCUMENT_LEAD_CAP
    sentinel_limit = cap + 1
    # Body-bound passage authentication runs only after the sequential body
    # budget below.  Doing it in this lead query would materialize every Raw body
    # before Python can enforce either the per-source or aggregate ceiling.
    del derivative_available, document_passage_revision

    sql = f"""WITH {source_cte},
        focus_seed AS MATERIALIZED (
            SELECT rowid AS raw_rowid FROM raw_fts
             WHERE raw_fts MATCH ? LIMIT {sentinel_limit}
        ),
        focus_pool AS MATERIALIZED (
            SELECT s.raw_id, s.sort_time
              FROM focus_seed f
              JOIN authorized_sources s ON s.raw_rowid=f.raw_rowid
             ORDER BY s.sort_time DESC, s.raw_id ASC
        ),
        anchor_seed AS MATERIALIZED (
            SELECT rowid AS raw_rowid FROM raw_fts
             WHERE raw_fts MATCH ? LIMIT {sentinel_limit}
        ),
        anchor_pool AS MATERIALIZED (
            SELECT s.raw_id, s.sort_time
              FROM anchor_seed a
              JOIN authorized_sources s ON s.raw_rowid=a.raw_rowid
             ORDER BY s.sort_time DESC, s.raw_id ASC
        ),
        detail_seed AS MATERIALIZED (
            SELECT rowid AS raw_rowid FROM raw_fts
             WHERE ?=1 AND raw_fts MATCH ? LIMIT {sentinel_limit}
        ),
        detail_pool AS MATERIALIZED (
            SELECT s.raw_id, s.sort_time
              FROM detail_seed d
              JOIN authorized_sources s ON s.raw_rowid=d.raw_rowid
             ORDER BY s.sort_time DESC, s.raw_id ASC
        ),
        compatibility_fallback_seed AS MATERIALIZED (
            SELECT s.raw_id, s.sort_time
              FROM authorized_sources s
             WHERE ?=1
               AND NOT EXISTS (SELECT 1 FROM focus_pool)
               AND NOT EXISTS (SELECT 1 FROM anchor_pool)
               AND NOT EXISTS (SELECT 1 FROM detail_pool)
             LIMIT {sentinel_limit}
        ),
        compatibility_fallback_pool AS MATERIALIZED (
            SELECT * FROM compatibility_fallback_seed
             ORDER BY sort_time DESC, raw_id ASC
        ),
        focus_ranked AS MATERIALIZED (
            SELECT f.*, ROW_NUMBER() OVER (
                       ORDER BY f.sort_time DESC, f.raw_id ASC
                   ) AS lead_rank
              FROM focus_pool f
        ),
        anchor_ranked AS MATERIALIZED (
            SELECT a.*, ROW_NUMBER() OVER (
                       ORDER BY a.sort_time DESC, a.raw_id ASC
                   ) AS lead_rank
              FROM anchor_pool a
        ),
        detail_ranked AS MATERIALIZED (
            SELECT f.*, ROW_NUMBER() OVER (
                       ORDER BY f.sort_time DESC, f.raw_id ASC
                   ) AS lead_rank
              FROM detail_pool f
        ),
        fallback_ranked AS MATERIALIZED (
            SELECT f.*, ROW_NUMBER() OVER (
                       ORDER BY f.sort_time DESC, f.raw_id ASC
                   ) AS lead_rank
              FROM compatibility_fallback_pool f
        ),
        combined_leads AS MATERIALIZED (
            SELECT f.raw_id, 0 AS lead_kind, f.lead_rank
              FROM focus_ranked f WHERE f.lead_rank<={cap}
            UNION ALL
            SELECT a.raw_id, 1 AS lead_kind, a.lead_rank
              FROM anchor_ranked a WHERE a.lead_rank<={cap}
            UNION ALL
            SELECT f.raw_id, 2 AS lead_kind, f.lead_rank
              FROM detail_ranked f WHERE f.lead_rank<={cap}
            UNION ALL
            SELECT f.raw_id, 3 AS lead_kind, f.lead_rank
              FROM fallback_ranked f WHERE f.lead_rank<={cap}
        ),
        deduplicated_leads AS MATERIALIZED (
            SELECT * FROM (
                SELECT c.*, ROW_NUMBER() OVER (
                           PARTITION BY c.raw_id
                           ORDER BY c.lead_kind ASC, c.lead_rank ASC, c.raw_id ASC
                       ) AS source_choice
                  FROM combined_leads c
            ) WHERE source_choice=1
        ),
        admitted_ids AS MATERIALIZED (
            SELECT d.*, ROW_NUMBER() OVER (
                       ORDER BY d.lead_kind ASC, d.lead_rank ASC, d.raw_id ASC
                   ) AS admitted_order
              FROM deduplicated_leads d
        ),
        admitted_sources AS MATERIALIZED (
            SELECT s.*, admitted.raw_id AS source_id,
                   admitted.lead_kind, admitted.lead_rank, admitted.admitted_order,
                   0 AS passage_projection_current,
                   0 AS passage_projection_backfill_pending,
                   1 AS passage_projection_unavailable
              FROM admitted_ids admitted
              JOIN authorized_sources s ON s.raw_id=admitted.raw_id
        ),
        totals AS MATERIALIZED (
            SELECT (SELECT COUNT(DISTINCT raw_id) FROM eligible_sources) AS total,
                   (SELECT COUNT(DISTINCT raw_id) FROM authorized_sources) AS examined,
                   (SELECT COUNT(*) FROM admitted_sources
                     WHERE passage_projection_current<>1) AS derivative_mismatches,
                   (SELECT COUNT(*) FROM admitted_sources
                     WHERE passage_projection_backfill_pending=1) AS derivative_backfills,
                   (SELECT COUNT(*) FROM admitted_sources
                     WHERE passage_projection_unavailable=1) AS derivative_unavailable,
                   CASE WHEN (SELECT value FROM authority_backfill)=1
                                  OR (SELECT value FROM lane_backfill)=1
                        THEN 1 ELSE 0 END AS authority_backfill,
                   (SELECT COUNT(*) FROM focus_ranked) AS focus_lead_matches,
                   (SELECT COUNT(*) FROM anchor_ranked) AS anchor_lead_matches,
                   (SELECT COUNT(*) FROM detail_ranked) AS detail_lead_matches,
                   (SELECT COUNT(*) FROM fallback_ranked) AS fallback_lead_matches
        )
        SELECT totals.*, admitted_sources.*
          FROM totals LEFT JOIN admitted_sources ON 1=1
         ORDER BY CASE WHEN admitted_sources.admitted_order IS NULL THEN 1 ELSE 0 END,
                  admitted_sources.admitted_order ASC"""
    rows = _select_rows(
        conn,
        sql,
        (
            *_authority_parameters(tenant_id, owner_id),
            *scope_parameters,
            focus_match_query,
            anchor_match_query,
            1 if compatibility_lead else 0,
            focus_detail_query,
            1 if compatibility_lead else 0,
        ),
    )
    if not rows:
        raise _fail("focused archive document summary is unavailable")
    keys = (
        "total",
        "examined",
        "derivative_mismatches",
        "derivative_backfills",
        "derivative_unavailable",
        "authority_backfill",
        "focus_lead_matches",
        "anchor_lead_matches",
        "detail_lead_matches",
        "fallback_lead_matches",
    )
    try:
        values = tuple(rows[0][key] for key in keys)
    except KeyError:
        raise _fail("focused archive document summary is invalid") from None
    if any(type(item) is not int for item in values):
        raise _fail("focused archive document summary is invalid")
    (
        total,
        examined,
        derivative_mismatches,
        derivative_backfills,
        derivative_unavailable,
        authority_backfill,
        focus_lead_matches,
        anchor_lead_matches,
        detail_lead_matches,
        fallback_lead_matches,
    ) = values
    if (
        min(values) < 0
        or authority_backfill not in {0, 1}
        or examined > total
        or max(
            derivative_mismatches,
            derivative_backfills,
            derivative_unavailable,
            focus_lead_matches,
            anchor_lead_matches,
            detail_lead_matches,
            fallback_lead_matches,
        )
        > examined
        or focus_lead_matches > sentinel_limit
        or anchor_lead_matches > sentinel_limit
        or detail_lead_matches > sentinel_limit
        or fallback_lead_matches > sentinel_limit
        or derivative_backfills > derivative_mismatches
        or derivative_unavailable > derivative_mismatches
    ):
        raise _fail("focused archive document summary is invalid")
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        source_id = row.get("source_id")
        if source_id is None:
            continue
        lead_kind = row.get("lead_kind")
        lead_rank = row.get("lead_rank")
        if (
            type(source_id) is not str
            or _RAW_ID.fullmatch(source_id) is None
            or lead_kind not in {0, 1, 2, 3}
            or type(lead_rank) is not int
            or not 1 <= lead_rank <= cap
        ):
            raise _fail("focused archive document lead is invalid")
        if source_id in seen:
            continue
        seen.add(source_id)
        hits.append(row)
    lead_capped = (
        focus_lead_matches > cap
        or anchor_lead_matches > cap
        or detail_lead_matches > cap
        or fallback_lead_matches > cap
    )
    return (
        total,
        examined,
        derivative_mismatches,
        derivative_backfills,
        derivative_unavailable,
        authority_backfill,
        lead_capped,
        hits,
    )


def _read_focused_document_body(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    *,
    max_bytes: int,
) -> tuple[str, int] | None:
    """Read at most one exact authorized body under a byte ceiling."""

    raw_rowid = source.get("raw_rowid")
    raw_id = source.get("raw_id")
    user_id = source.get("user_id")
    raw_version = source.get("raw_version")
    raw_digest = source.get("content_hash")
    if (
        type(max_bytes) is not int
        or max_bytes <= 0
        or type(raw_rowid) is not int
        or raw_rowid <= 0
        or type(raw_id) is not str
        or _RAW_ID.fullmatch(raw_id) is None
        or type(user_id) is not str
        or not user_id
        or type(raw_version) is not int
        or raw_version < 1
        or type(raw_digest) is not str
        or _SHA256.fullmatch(raw_digest) is None
    ):
        return None
    rows = _select_rows(
        conn,
        """SELECT substr(CAST(raw_content AS BLOB),1,?) AS passage_body_blob
             FROM raw_objects
            WHERE rowid=? AND id=? AND user_id=?
              AND version=? AND content_hash=?
              AND deleted_at IS NULL AND typeof(raw_content)='text'
              AND length(CAST(raw_content AS BLOB)) BETWEEN 1 AND ?
            LIMIT 2""",
        (max_bytes + 1, raw_rowid, raw_id, user_id, raw_version, raw_digest, max_bytes),
    )
    if not rows:
        return None
    if len(rows) != 1:
        raise _fail("focused archive document body selection is invalid")
    body_blob = rows[0].get("passage_body_blob")
    if type(body_blob) is not bytes or not 1 <= len(body_blob) <= max_bytes:
        return None
    try:
        body = body_blob.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if not body:
        raise _fail("focused archive document body selection is invalid")
    return body, len(body_blob)


def _catalog_sql(
    corpus: ArchiveSearchCorpus,
    request: ArchiveSearchRequest,
    owner_id: str,
    *,
    document_catalog_available: bool,
    enrichment_revision: int,
) -> tuple[str, tuple[object, ...]]:
    source_cte, scope_parameters = _source_cte(corpus, request, include_body=False)
    catalog_projection_cte = _catalog_projection_cte(
        corpus,
        document_catalog_available=document_catalog_available,
        enrichment_revision=enrichment_revision,
    )
    source_id = "c.raw_id"
    filename = _filename_expression("s")
    source_format = _format_expression("s") if corpus is ArchiveSearchCorpus.DOCUMENTS else "''"
    title = _title_expression("s")
    folded_filename = "friday_archive_fold(c.filename)"
    folded_format = "friday_archive_fold(c.source_format)"
    folded_title = "friday_archive_fold(c.title)"
    folded_semantic_title = "friday_archive_fold(c.catalog_semantic_title)"
    folded_alias = "friday_archive_fold(a.supplied_filename)"
    folded_exact = "friday_archive_fold(n.exact)"
    folded_like = "friday_archive_fold(n.pattern)"
    exact_hit = f"""EXISTS (
        SELECT 1 FROM needles n
         WHERE {folded_filename}={folded_exact}
            OR {folded_format}={folded_exact}
            OR {folded_title}={folded_exact}
            OR {folded_semantic_title}={folded_exact}
            OR EXISTS (SELECT 1 FROM authorized_aliases a
                        WHERE a.raw_object_id=c.raw_id AND {folded_alias}={folded_exact})
    )"""
    prefix_hit = f"""EXISTS (
        SELECT 1 FROM needles n
         WHERE {folded_filename} LIKE {folded_like}||'%' ESCAPE '\\'
            OR {folded_format} LIKE {folded_like}||'%' ESCAPE '\\'
            OR {folded_title} LIKE {folded_like}||'%' ESCAPE '\\'
            OR {folded_semantic_title} LIKE {folded_like}||'%' ESCAPE '\\'
            OR EXISTS (SELECT 1 FROM authorized_aliases a
                        WHERE a.raw_object_id=c.raw_id
                          AND {folded_alias} LIKE {folded_like}||'%' ESCAPE '\\')
    )"""
    substring_hit = f"""EXISTS (
        SELECT 1 FROM needles n
         WHERE {folded_filename} LIKE '%'||{folded_like}||'%' ESCAPE '\\'
            OR {folded_format} LIKE '%'||{folded_like}||'%' ESCAPE '\\'
            OR {folded_title} LIKE '%'||{folded_like}||'%' ESCAPE '\\'
            OR {folded_semantic_title} LIKE '%'||{folded_like}||'%' ESCAPE '\\'
            OR EXISTS (SELECT 1 FROM authorized_aliases a
                        WHERE a.raw_object_id=c.raw_id
                          AND {folded_alias} LIKE '%'||{folded_like}||'%' ESCAPE '\\')
    )"""
    source_choice_order = (
        "CASE m.knowledge_lifecycle WHEN 'active' THEN 0 WHEN 'archived' THEN 1 ELSE 2 END, "
        if corpus is ArchiveSearchCorpus.KNOWLEDGE
        else ""
    )
    sql = f"""WITH {source_cte},
        {catalog_projection_cte},
        needles AS MATERIALIZED (
            SELECT json_extract(value,'$.exact') AS exact,
                   json_extract(value,'$.like') AS pattern
              FROM json_each(?)
             WHERE json_type(value,'$.exact')='text'
               AND json_type(value,'$.like')='text'
        ),
        authorized_aliases AS MATERIALIZED (
            SELECT a.raw_object_id, a.supplied_filename
              FROM file_source_aliases a
              JOIN catalog_projection_sources s
                ON s.raw_id=a.raw_object_id AND s.user_id=a.user_id
             WHERE a.uploaded_by=?
               AND length(a.supplied_filename) BETWEEN 1 AND 260
               AND trim(a.supplied_filename)=a.supplied_filename
               AND instr(a.supplied_filename,char(0))=0
               AND instr(a.supplied_filename,char(10))=0
               AND instr(a.supplied_filename,char(13))=0
        ),
        catalog_sources AS MATERIALIZED (
            SELECT s.*, {filename} AS filename, {source_format} AS source_format, {title} AS title,
                   (SELECT a.supplied_filename
                      FROM authorized_aliases a
                     WHERE a.raw_object_id=s.raw_id
                       AND EXISTS (
                           SELECT 1 FROM needles n
                            WHERE friday_archive_fold(a.supplied_filename)
                                  LIKE '%'||friday_archive_fold(n.pattern)||'%'
                                       ESCAPE '\\'
                       )
                     ORDER BY friday_archive_fold(a.supplied_filename) ASC,
                              a.supplied_filename ASC LIMIT 1) AS matching_alias,
                   COALESCE(
                       NULLIF({filename},''),
                       (SELECT a.supplied_filename FROM authorized_aliases a
                         WHERE a.raw_object_id=s.raw_id
                         ORDER BY friday_archive_fold(a.supplied_filename) ASC,
                                  a.supplied_filename ASC LIMIT 1), ''
                   ) AS display_filename,
                   COALESCE(
                       NULLIF(s.catalog_semantic_title,''),
                       NULLIF({title},''), NULLIF({filename},''),
                       (SELECT a.supplied_filename FROM authorized_aliases a
                         WHERE a.raw_object_id=s.raw_id
                         ORDER BY friday_archive_fold(a.supplied_filename) ASC,
                                  a.supplied_filename ASC LIMIT 1), ''
                   ) AS display_text
              FROM catalog_projection_sources s
        ),
        matched_sources AS MATERIALIZED (
            SELECT c.*, {source_id} AS source_id,
                   CASE WHEN {exact_hit} THEN 0 WHEN {prefix_hit} THEN 1 ELSE 2 END AS score,
                   friday_archive_fold(c.display_text) AS sort_text
              FROM catalog_sources c
             WHERE {substring_hit}
        ),
        source_choices AS MATERIALIZED (
            SELECT * FROM (
                SELECT m.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY m.raw_id
                           ORDER BY {source_choice_order}m.score ASC, m.sort_text ASC,
                                    m.sort_time DESC,
                                    COALESCE(m.knowledge_id,'') ASC
                       ) AS source_choice
                  FROM matched_sources m
            ) WHERE source_choice=1
        ),
        indexed_sources AS MATERIALIZED (SELECT * FROM catalog_projection_sources),
        ranked AS MATERIALIZED (
            SELECT m.*,
                   ROW_NUMBER() OVER (
                       ORDER BY m.score ASC, m.sort_text ASC,
                                m.sort_time DESC, m.source_id ASC
                   ) AS lane_rank
              FROM source_choices m
        ),
        derivative_mismatches(value) AS MATERIALIZED (
            SELECT COUNT(*) FROM catalog_projection_sources
             WHERE catalog_projection_current<>1
        ),
        derivative_backfills(value) AS MATERIALIZED (SELECT 0),
        derivative_unavailable(value) AS MATERIALIZED (SELECT 0),
        page AS MATERIALIZED (
            SELECT * FROM ranked
             ORDER BY score ASC, sort_text ASC, sort_time DESC, source_id ASC
             LIMIT ?
        )
        {_page_select()}"""
    return (
        sql,
        (
            *scope_parameters,
            _catalog_needles(request),
            owner_id,
        ),
    )


def _folded_locator_text(value: str) -> tuple[str, tuple[tuple[int, int], ...]]:
    """Mirror the SQL fold while retaining offsets into the exact stored text."""

    folded: list[str] = []
    offsets: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(value) + 1):
        if index < len(value) and unicodedata.combining(value[index]) != 0:
            continue
        segment = value[start:index]
        normalized = _archive_search_fold(segment)
        folded.append(normalized)
        offsets.extend((start, index) for _char in normalized)
        start = index
    return "".join(folded), tuple(offsets)


def _exact_span(body: str, query: str) -> tuple[int, int] | None:
    candidates = [query, *(_fts_terms(query))]
    folded_body, offsets = _folded_locator_text(body)
    for raw_term in candidates:
        term = raw_term.rstrip("*").strip('"').strip()
        if not term or any(unicodedata.category(char).startswith("C") for char in term):
            continue
        folded_term, _term_offsets = _folded_locator_text(term)
        start = folded_body.find(folded_term)
        if folded_term and start >= 0:
            end = start + len(folded_term)
            return offsets[start][0], offsets[end - 1][1]
    return None


def _excerpt_for_match(
    body: str,
    match: tuple[int, int],
    *,
    lower_bound: int,
    upper_bound: int,
    allow_oversized_match: bool,
) -> tuple[str, int, int] | None:
    match_start, match_end = match
    if not 0 <= lower_bound <= match_start < match_end <= upper_bound <= len(body) or (
        not allow_oversized_match and match_end - match_start > _MAX_EXCERPT_CHARS
    ):
        return None
    budget = min(_MAX_EXCERPT_CHARS, upper_bound - lower_bound)
    start = max(lower_bound, match_start - budget // 2)
    end = min(upper_bound, start + budget)
    start = max(lower_bound, end - budget)
    for index in range(start, match_start):
        if unicodedata.category(body[index]).startswith("C"):
            start = index + 1
    for index in range(match_end, end):
        if unicodedata.category(body[index]).startswith("C"):
            end = index
            break
    while start < match_start and body[start].isspace():
        start += 1
    while end > match_end and body[end - 1].isspace():
        end -= 1
    text = body[start:end]
    if (
        not text
        or text != text.strip()
        or len(text) > _MAX_EXCERPT_CHARS
        or any(unicodedata.category(char).startswith("C") for char in text)
    ):
        text = body[match_start:match_end]
        start, end = match_start, match_end
    if (
        not text
        or text != text.strip()
        or (not allow_oversized_match and len(text) > _MAX_EXCERPT_CHARS)
        or any(unicodedata.category(char).startswith("C") for char in text)
    ):
        return None
    return text, start, end


def _excerpt(body: object, query: str) -> tuple[str, int, int] | None:
    if type(body) is not str or not body:
        return None
    match = _exact_span(body, query)
    if match is None:
        return None
    return _excerpt_for_match(
        body,
        match,
        lower_bound=0,
        upper_bound=len(body),
        allow_oversized_match=True,
    )


def _stored_document_excerpt(
    body: object,
    query: str,
    passages: tuple[_StoredDocumentPassage, ...] | None,
) -> tuple[str, int, int, int] | None:
    """Choose the lowest persisted child containing the whole exact match."""

    if type(body) is not str or not body or passages is None:
        return None
    match = _exact_span(body, query)
    if match is None:
        return None
    match_start, match_end = match
    passage = next(
        (item for item in passages if item.start_char <= match_start < match_end <= item.end_char),
        None,
    )
    if passage is None:
        return None
    excerpt = _excerpt_for_match(
        body,
        match,
        lower_bound=passage.start_char,
        upper_bound=passage.end_char,
        allow_oversized_match=False,
    )
    if excerpt is None:
        return None
    text, start, end = excerpt
    return text, start, end, passage.chunk_index


def _focused_document_excerpt(
    body: object,
    projection: SourceFocusProjection,
    passages: tuple[_StoredDocumentPassage, ...] | None,
) -> tuple[str, int, int, int | None] | None:
    """Bind one focus projection to exact Raw offsets and, when possible, v2."""

    if type(body) is not str or type(projection) is not SourceFocusProjection:
        return None
    start = projection.start
    end = projection.end
    if (
        not 0 <= start < end <= len(body)
        or end - start > _MAX_EXCERPT_CHARS
        or projection.excerpt != body[start:end]
    ):
        return None
    passage = next(
        (item for item in passages or () if item.start_char <= start < end <= item.end_char),
        None,
    )
    return projection.excerpt, start, end, None if passage is None else passage.chunk_index


def _focused_dense_excerpt(
    row: dict[str, Any],
    projection: SourceFocusProjection,
    passages: tuple[_StoredDocumentPassage, ...] | None,
) -> tuple[str, int, int, int | None] | None:
    """Bind a focus projection to one bounded dense span and absolute offsets."""

    selected = _dense_chunk_slice(row)
    if selected is None or type(projection) is not SourceFocusProjection:
        return None
    body, dense_start, dense_end = selected
    start = projection.start
    end = projection.end
    relative_start = start - dense_start
    relative_end = end - dense_start
    if (
        not dense_start <= start < end <= dense_end
        or end - start > _MAX_EXCERPT_CHARS
        or not 0 <= relative_start < relative_end <= len(body)
        or projection.excerpt != body[relative_start:relative_end]
    ):
        return None
    passage = next(
        (item for item in passages or () if item.start_char <= start < end <= item.end_char),
        None,
    )
    return projection.excerpt, start, end, None if passage is None else passage.chunk_index


def _project_focused_dense_source(
    full_source: str,
    dense_body: str,
    query: str,
    focus: str,
    *,
    dense_start: int,
    dense_end: int,
    max_chars: int,
) -> SourceFocusProjection | None:
    """Project inside one dense span while retaining the real source boundaries."""

    if (
        not 0 <= dense_start < dense_end <= len(full_source)
        or full_source[dense_start:dense_end] != dense_body
    ):
        return None

    def boundary_shape(value: str) -> str:
        # Keep every whitespace/record boundary and every absolute offset.  A
        # non-whitespace exterior character becomes a neutral token character,
        # so an edge in the middle of a token or beside a nonblank record cannot
        # masquerade as a source boundary while remote anchor matches disappear.
        return "".join(character if character.isspace() else "x" for character in value)

    shaped_source = (
        boundary_shape(full_source[:dense_start]) + dense_body + boundary_shape(full_source[dense_end:])
    )
    try:
        return project_source_focus(
            shaped_source,
            query,
            focus,
            max_chars=max_chars,
        )
    except (TypeError, ValueError, UnicodeError):
        return None


def _closed_lifecycle(value: object, *, label: str) -> LifecycleState:
    if type(value) is not str:
        raise _fail(f"archive document {label} is invalid")
    try:
        return LifecycleState(value)
    except ValueError:
        raise _fail(f"archive document {label} is invalid") from None


def _positive_version(value: object) -> str:
    if type(value) is not int:
        raise _fail("archive document revision is invalid")
    version = value
    if not 1 <= version <= 1_000_000_000:
        raise _fail("archive document revision is invalid")
    return str(version)


def _resolved_source(
    row: dict[str, Any],
    *,
    corpus: ArchiveSearchCorpus,
    tenant_id: str,
    owner_id: str,
) -> tuple[
    ResolvedSource,
    SourceRevision,
    SourceRevision,
    SourceRevision | None,
]:
    raw_id = row.get("raw_id")
    raw_hash = row.get("content_hash")
    if type(raw_id) is not str or _RAW_ID.fullmatch(raw_id) is None:
        raise _fail("archive document source identity is invalid")
    if type(raw_hash) is not str or _SHA256.fullmatch(raw_hash) is None:
        raise _fail("archive document revision is invalid")
    raw_source = row.get("source")
    raw_content_type = row.get("content_type")
    if corpus is ArchiveSearchCorpus.DOCUMENTS:
        source_kind = SourceKind.DOCUMENT
    elif raw_source == "web":
        source_kind = SourceKind.WEB_CAPTURE
    elif raw_source == "generated" or raw_content_type == "generated_file":
        source_kind = SourceKind.GENERATED_ARTIFACT
    else:
        source_kind = SourceKind.DOCUMENT
    source_ref = SourceRef(
        source_kind,
        AuthorityScope.TENANT_PRINCIPAL,
        tenant_id,
        owner_id,
        CanonicalObjectKind.RAW_OBJECT,
        raw_id,
    )
    raw = SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id)
    raw_revision = SourceRevision(raw, RevisionKind.RAW_CONTENT_SHA256, raw_hash)
    representations = [raw]
    lifecycle = [LifecycleRef(raw, LifecycleState.ACTIVE)]
    revisions = [raw_revision]
    targets = [RevalidationTarget(raw, AuthorityScope.TENANT_PRINCIPAL)]

    inbox_id = row.get("inbox_id")
    inbox_status = row.get("inbox_status")
    if inbox_id is not None or inbox_status is not None:
        if (
            type(inbox_id) is not str
            or _INBOX_ID.fullmatch(inbox_id) is None
            or inbox_status not in {"pending", "classified", "archived"}
        ):
            raise _fail("archive document review identity is invalid")
        inbox = SourceRepresentation(RepresentationKind.INBOX_ITEM, inbox_id)
        representations.append(inbox)
        lifecycle.append(LifecycleRef(inbox, _closed_lifecycle(inbox_status, label="review lifecycle")))
        targets.append(RevalidationTarget(inbox, AuthorityScope.TENANT_PRINCIPAL))

    knowledge_revision: SourceRevision | None = None
    knowledge_id = row.get("knowledge_id")
    if knowledge_id is not None:
        if type(knowledge_id) is not str or _KO_ID.fullmatch(knowledge_id) is None:
            raise _fail("archive document knowledge identity is invalid")
        knowledge = SourceRepresentation(RepresentationKind.KNOWLEDGE_OBJECT, knowledge_id)
        knowledge_state = _closed_lifecycle(
            row.get("knowledge_lifecycle"),
            label="knowledge lifecycle",
        )
        knowledge_revision = SourceRevision(
            knowledge,
            RevisionKind.KNOWLEDGE_VERSION,
            _positive_version(row.get("knowledge_version")),
        )
        representations.append(knowledge)
        lifecycle.append(LifecycleRef(knowledge, knowledge_state))
        revisions.append(knowledge_revision)
        targets.append(RevalidationTarget(knowledge, AuthorityScope.TENANT_PRINCIPAL))
    if corpus is ArchiveSearchCorpus.KNOWLEDGE and knowledge_revision is None:
        raise _fail("archive document knowledge revision is unavailable")
    resolved = ResolvedSource.create(
        source_ref=source_ref,
        representations=representations,
        lifecycle=lifecycle,
        revisions=revisions,
        revalidation_targets=targets,
    )
    selected_revision = knowledge_revision if corpus is ArchiveSearchCorpus.KNOWLEDGE else raw_revision
    assert selected_revision is not None
    return resolved, selected_revision, raw_revision, knowledge_revision


def _utc_instant(value: object, *, label: str) -> datetime:
    if type(value) is not str:
        raise _fail(f"archive document {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _fail(f"archive document {label} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _fail(f"archive document {label} is invalid")
    return parsed.astimezone(UTC)


def _calendar_date(value: object, *, label: str) -> date:
    if type(value) is not str:
        raise _fail(f"archive document {label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise _fail(f"archive document {label} is invalid") from None
    if parsed.isoformat() != value:
        raise _fail(f"archive document {label} is invalid")
    return parsed


def _candidate_temporal_facts(
    row: dict[str, Any],
    *,
    request: ArchiveSearchRequest,
    corpus: ArchiveSearchCorpus,
    raw_revision: SourceRevision,
    knowledge_revision: SourceRevision | None,
) -> tuple[TemporalFact, ...]:
    facts: dict[TemporalRole, TemporalFact] = {}
    for constraint in _temporal_constraints(request, corpus):
        if constraint.role is TemporalRole.RECEIVED_AT:
            revision = raw_revision
            value = row.get("raw_received_at")
            origin = TemporalOrigin.STORAGE_COLUMN
        elif constraint.role is TemporalRole.LEGACY_UNCLASSIFIED_DOCUMENT_DATE:
            facts[constraint.role] = TemporalFact.for_date(
                role=constraint.role,
                value=_calendar_date(row.get("raw_document_date"), label="document date"),
                precision=TemporalPrecision.DAY,
                origin=TemporalOrigin.LEGACY_COLLAPSED,
                source_revision=raw_revision,
            )
            continue
        elif constraint.role is TemporalRole.KNOWLEDGE_PROJECTION_CREATED_AT:
            if knowledge_revision is None:
                raise _fail("archive document temporal revision is unavailable")
            revision = knowledge_revision
            value = row.get("knowledge_created_at")
            origin = TemporalOrigin.KNOWLEDGE_PROJECTION
        elif constraint.role is TemporalRole.KNOWLEDGE_PROJECTION_MODIFIED_AT:
            if knowledge_revision is None:
                raise _fail("archive document temporal revision is unavailable")
            revision = knowledge_revision
            value = row.get("knowledge_updated_at")
            origin = TemporalOrigin.KNOWLEDGE_PROJECTION
        else:
            raise _fail("archive document temporal role is unavailable")
        facts[constraint.role] = TemporalFact.for_instant(
            role=constraint.role,
            value=_utc_instant(value, label="temporal value"),
            origin=origin,
            source_revision=revision,
        )
    return tuple(sorted(facts.values(), key=lambda item: item.to_private_json()))


def _candidate(
    row: dict[str, Any],
    *,
    corpus: ArchiveSearchCorpus,
    lane: SearchLane,
    request: ArchiveSearchRequest,
    tenant_id: str,
    owner_id: str,
    document_passages: tuple[_StoredDocumentPassage, ...] | None = None,
    source_focus_projection: SourceFocusProjection | None = None,
    dense_projection: ArchiveDenseQueryProjection | None = None,
) -> ArchiveSearchCandidate:
    resolved, passage_revision, raw_revision, knowledge_revision = _resolved_source(
        row,
        corpus=corpus,
        tenant_id=tenant_id,
        owner_id=owner_id,
    )
    try:
        raw_review = row["review_state"]
        raw_lifecycle = row["lifecycle_state"]
        raw_lane_rank = row["lane_rank"]
    except KeyError:
        raise _fail("archive document candidate state is invalid") from None
    if type(raw_review) is not str or type(raw_lifecycle) is not str or type(raw_lane_rank) is not int:
        raise _fail("archive document candidate state is invalid")
    try:
        review_state = ArchiveReviewState(raw_review)
        lifecycle_state = LifecycleState(raw_lifecycle)
        lane_rank = raw_lane_rank
    except ValueError:
        raise _fail("archive document candidate state is invalid") from None
    if (lane is SearchLane.DENSE) != (dense_projection is not None):
        raise _fail("archive dense passage identity is invalid")
    if source_focus_projection is not None and not (
        lane in {SearchLane.LEXICAL, SearchLane.DENSE}
        and corpus is ArchiveSearchCorpus.DOCUMENTS
        and request.focus
    ):
        raise _fail("archive source-focus passage identity is invalid")
    if (
        request.focus
        and corpus is ArchiveSearchCorpus.DOCUMENTS
        and lane
        in {
            SearchLane.LEXICAL,
            SearchLane.DENSE,
        }
        and source_focus_projection is None
    ):
        raise _fail("archive source-focus passage identity is unavailable")
    passages: tuple[ArchiveSearchPassage, ...] = ()
    evidence_authority = ArchiveEvidenceAuthority.NAVIGATION_ONLY
    if (
        lane in {SearchLane.LEXICAL, SearchLane.DENSE}
        and lifecycle_state is not LifecycleState.DEPRECATED
        and not (
            corpus is ArchiveSearchCorpus.KNOWLEDGE
            and row.get("inbox_status") == LifecycleState.PENDING.value
        )
    ):
        focused_excerpt = (
            _focused_dense_excerpt(row, source_focus_projection, document_passages)
            if source_focus_projection is not None and lane is SearchLane.DENSE
            else _focused_document_excerpt(
                row.get("passage_body"),
                source_focus_projection,
                document_passages,
            )
            if source_focus_projection is not None
            else None
        )
        if source_focus_projection is not None and focused_excerpt is None:
            raise _fail("archive source-focus passage identity is unavailable")
        stored_excerpt = (
            _stored_document_excerpt(
                row.get("passage_body"),
                request.query,
                document_passages,
            )
            if (
                source_focus_projection is None
                and lane is SearchLane.LEXICAL
                and corpus is ArchiveSearchCorpus.DOCUMENTS
            )
            else None
        )
        excerpt = (
            (focused_excerpt[0], focused_excerpt[1], focused_excerpt[2])
            if focused_excerpt is not None
            else _dense_excerpt(row)
            if lane is SearchLane.DENSE
            else (stored_excerpt[0], stored_excerpt[1], stored_excerpt[2])
            if stored_excerpt is not None
            else _excerpt(row.get("passage_body"), request.query)
        )
        if excerpt is not None:
            text, start, end = excerpt
            if lane is SearchLane.DENSE:
                chunk_index = row.get("dense_chunk_index")
                chunk_digest = row.get("dense_content_hash")
                if (
                    dense_projection is None
                    or knowledge_revision is None
                    or type(chunk_index) is not int
                    or chunk_index < 0
                    or type(chunk_digest) is not str
                    or _SHA256.fullmatch(chunk_digest) is None
                ):
                    raise _fail("archive dense passage identity is unavailable")
                if focused_excerpt is not None:
                    passage_index_version = (
                        DOCUMENT_STORED_PASSAGE_INDEX_VERSION
                        if focused_excerpt[3] is not None
                        else LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
                    )
                    chunk_index = focused_excerpt[3] if focused_excerpt[3] is not None else 0
                    embedding = EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE)
                else:
                    passage_revision = knowledge_revision
                    passage_index_version = dense_projection.chunk_scheme
                    embedding = EmbeddingIdentity.indexed(
                        EmbeddingCompatibility.CURRENT,
                        model_id=dense_projection.model_id,
                        dimensions=dense_projection.dimensions,
                        source_version=int(knowledge_revision.value),
                        chunk_scheme=dense_projection.chunk_scheme,
                        chunk_content_sha256=chunk_digest,
                    )
            else:
                if focused_excerpt is not None:
                    chunk_index = focused_excerpt[3] if focused_excerpt[3] is not None else 0
                    passage_index_version = (
                        DOCUMENT_STORED_PASSAGE_INDEX_VERSION
                        if focused_excerpt[3] is not None
                        else LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
                    )
                else:
                    chunk_index = stored_excerpt[3] if stored_excerpt is not None else 0
                    passage_index_version = (
                        DOCUMENT_STORED_PASSAGE_INDEX_VERSION
                        if stored_excerpt is not None
                        else LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
                    )
                embedding = EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE)
            passage_ref = PassageRef.from_resolved_source(
                resolved,
                source_revision=passage_revision,
                locator=TextSpanLocator(chunk_index=chunk_index, start_char=start, end_char=end),
                passage_index_version=passage_index_version,
                embedding=embedding,
            )
            passages = (ArchiveSearchPassage(passage_ref, text),)
            evidence_authority = (
                ArchiveEvidenceAuthority.NONCANONICAL
                if review_state is ArchiveReviewState.PENDING
                else ArchiveEvidenceAuthority.CANONICAL
            )
    return ArchiveSearchCandidate.create(
        corpus=corpus,
        resolved_source=resolved,
        review_state=review_state,
        evidence_authority=evidence_authority,
        lifecycle_state=lifecycle_state,
        matches=(ArchiveMatchRank(_MATCH_CHANNEL[lane], lane_rank),),
        title=_safe_display(
            row.get("catalog_semantic_title") or row.get("title") or row.get("knowledge_title")
        ),
        filename=_safe_display(
            row.get("matching_alias") or row.get("filename") or row.get("display_filename")
        ),
        temporal_facts=_candidate_temporal_facts(
            row,
            request=request,
            corpus=corpus,
            raw_revision=raw_revision,
            knowledge_revision=knowledge_revision,
        ),
        passages=passages,
    )


def _read_exact_archive_replay_body(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    *,
    corpus: ArchiveSearchCorpus,
) -> str | None:
    """Read exactly one already-authorized representation body."""

    raw_rowid = source.get("raw_rowid")
    raw_id = source.get("raw_id")
    user_id = source.get("user_id")
    raw_version = source.get("raw_version")
    raw_digest = source.get("content_hash")
    if (
        type(raw_rowid) is not int
        or raw_rowid <= 0
        or type(raw_id) is not str
        or _RAW_ID.fullmatch(raw_id) is None
        or type(user_id) is not str
        or not user_id
        or type(raw_version) is not int
        or raw_version < 1
        or type(raw_digest) is not str
        or _SHA256.fullmatch(raw_digest) is None
    ):
        return None
    if corpus is ArchiveSearchCorpus.DOCUMENTS:
        sql = """SELECT r.raw_content AS replay_body
                   FROM raw_objects r
                  WHERE r.rowid=? AND r.id=? AND r.user_id=?
                    AND r.version=? AND r.content_hash=?
                    AND r.deleted_at IS NULL AND typeof(r.raw_content)='text'
                  LIMIT 2"""
        parameters: tuple[object, ...] = (
            raw_rowid,
            raw_id,
            user_id,
            raw_version,
            raw_digest,
        )
    else:
        knowledge_rowid = source.get("knowledge_rowid")
        knowledge_id = source.get("knowledge_id")
        knowledge_version = source.get("knowledge_version")
        if (
            type(knowledge_rowid) is not int
            or knowledge_rowid <= 0
            or type(knowledge_id) is not str
            or _KO_ID.fullmatch(knowledge_id) is None
            or type(knowledge_version) is not int
            or knowledge_version < 1
        ):
            return None
        sql = """SELECT k.content AS replay_body
                   FROM raw_objects r
                   JOIN knowledge_objects k
                     ON k.rowid=? AND k.id=? AND k.user_id=?
                    AND k.raw_object_id=r.id AND k.version=?
                    AND k.deleted_at IS NULL AND typeof(k.content)='text'
                  WHERE r.rowid=? AND r.id=? AND r.user_id=?
                    AND r.version=? AND r.content_hash=? AND r.deleted_at IS NULL
                  LIMIT 2"""
        parameters = (
            knowledge_rowid,
            knowledge_id,
            user_id,
            knowledge_version,
            raw_rowid,
            raw_id,
            user_id,
            raw_version,
            raw_digest,
        )
    rows = _select_rows(conn, sql, parameters)
    if len(rows) != 1 or type(rows[0].get("replay_body")) is not str:
        return None
    return str(rows[0]["replay_body"])


def _select_authorized_archive_document_replay_source_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
    origin_boundary_user_message_id: str,
    corpus: ArchiveSearchCorpus,
    source_ref: SourceRef,
    knowledge_object_id: str | None = None,
    source_revision: SourceRevision | None = None,
) -> ArchiveDocumentReplaySource | None:
    """Reselect one exact authorized document source without searching.

    ``None`` means that the accepted-turn boundary or selected source is no
    longer in the actor's authorized scope.  The caller owns the transaction
    and classifies that absence with its separately refreshed capability signal.
    """

    if type(conn) is not sqlite3.Connection or not conn.in_transaction:
        raise _fail("archive document replay requires a caller-owned snapshot")
    tenant = _actor(tenant_id)
    owner = _actor(owner_id)
    if (
        type(origin_boundary_user_message_id) is not str
        or _MESSAGE_ID.fullmatch(origin_boundary_user_message_id) is None
    ):
        raise _fail("archive document replay boundary is invalid")
    if type(corpus) is not ArchiveSearchCorpus or corpus not in _SUPPORTED_CORPORA:
        raise _fail("archive document replay corpus is invalid")
    if (
        type(source_ref) is not SourceRef
        or source_ref.authority_scope is not AuthorityScope.TENANT_PRINCIPAL
        or source_ref.tenant_id != tenant
        or source_ref.principal_id != owner
        or source_ref.canonical_object_kind is not CanonicalObjectKind.RAW_OBJECT
        or (corpus is ArchiveSearchCorpus.DOCUMENTS and source_ref.source_kind is not SourceKind.DOCUMENT)
        or (
            corpus is ArchiveSearchCorpus.KNOWLEDGE
            and source_ref.source_kind
            not in {SourceKind.DOCUMENT, SourceKind.WEB_CAPTURE, SourceKind.GENERATED_ARTIFACT}
        )
    ):
        raise _fail("archive document replay source is invalid")
    if corpus is ArchiveSearchCorpus.DOCUMENTS:
        if knowledge_object_id is not None:
            raise _fail("archive document replay representation is invalid")
    elif type(knowledge_object_id) is not str or _KO_ID.fullmatch(knowledge_object_id) is None:
        raise _fail("archive document replay representation is invalid")

    request = ArchiveSearchRequest.create(
        query="exact replay",
        corpora=(corpus,),
        review_scope=ReviewScope.DISCOVERABLE,
        limit=1,
    )
    source_cte, scope_parameters = _source_cte(
        corpus,
        request,
        include_body=False,
        bounded_material=True,
    )
    representation_clause = "" if knowledge_object_id is None else " AND s.knowledge_id=?"
    sql = f"""WITH {source_cte},
        replay_boundary AS MATERIALIZED (
            SELECT b.id
              FROM messages b
              JOIN conversations c
                ON c.id=b.conversation_id AND c.user_id=b.user_id
             WHERE b.id=? AND b.user_id=? AND b.role='user'
               AND c.user_id=?
        )
        SELECT s.*
          FROM authorized_sources s
          CROSS JOIN replay_boundary
         WHERE s.raw_id=?{representation_clause}
         LIMIT 2"""  # nosec B608
    parameters: tuple[object, ...] = (
        *_authority_parameters(tenant, owner),
        *scope_parameters,
        origin_boundary_user_message_id,
        owner,
        owner,
        source_ref.canonical_object_id,
        *((knowledge_object_id,) if knowledge_object_id is not None else ()),
    )
    rows = _select_rows(conn, sql, parameters)
    if not rows:
        return None
    if len(rows) != 1:
        raise _fail("archive document replay source is ambiguous")
    try:
        if source_revision is not None:
            if type(source_revision) is not SourceRevision:
                raise _fail("archive document replay revision is invalid")
            if corpus is ArchiveSearchCorpus.DOCUMENTS:
                revision_matches = bool(
                    source_revision.kind is RevisionKind.RAW_CONTENT_SHA256
                    and source_revision.representation.kind is RepresentationKind.RAW_OBJECT
                    and source_revision.representation.object_id == rows[0].get("raw_id")
                    and source_revision.value == rows[0].get("content_hash")
                )
            else:
                revision_matches = bool(
                    source_revision.kind is RevisionKind.KNOWLEDGE_VERSION
                    and source_revision.representation.kind is RepresentationKind.KNOWLEDGE_OBJECT
                    and source_revision.representation.object_id == rows[0].get("knowledge_id")
                    and source_revision.value == str(rows[0].get("knowledge_version"))
                )
            if not revision_matches:
                return None
        body = _read_exact_archive_replay_body(conn, rows[0], corpus=corpus)
        if body is None:
            return None
        rows[0]["passage_body"] = body
        rows[0]["filename"] = rows[0].get("raw_filename")
        resolved, _selected, _raw, _knowledge = _resolved_source(
            rows[0],
            corpus=corpus,
            tenant_id=tenant,
            owner_id=owner,
        )
        body = rows[0].get("passage_body")
        if type(body) is not str:
            raise _fail("archive document replay body is unavailable")
        stored_passages: tuple[_StoredDocumentPassage, ...] | None = None
        if corpus is ArchiveSearchCorpus.DOCUMENTS:
            passage_contract = _load_document_passage_contract(conn)
            if passage_contract is not None:
                try:
                    stored_passages = _select_current_document_passages(
                        conn,
                        rows[0],
                        passage_contract,
                    )
                except ArchiveDocumentStorageError:
                    # A sidecar read can never make a legacy v1 selection less
                    # replayable.  A v2 caller will observe the missing exact
                    # child and close as drifted below the storage seam.
                    stored_passages = None
        return ArchiveDocumentReplaySource(
            corpus=corpus,
            resolved_source=resolved,
            body=body,
            stored_passages=stored_passages,
            _factory=_REPLAY_FACTORY,
        )
    except ArchiveDocumentStorageError:
        raise
    except Exception:
        raise _fail("archive document replay source is unavailable") from None


def select_authorized_archive_document_replay_source_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
    origin_boundary_user_message_id: str,
    corpus: ArchiveSearchCorpus,
    source_ref: SourceRef,
    knowledge_object_id: str | None = None,
    source_revision: SourceRevision | None = None,
) -> ArchiveDocumentReplaySource | None:
    """Public body-free wrapper for one exact document replay SELECT."""

    try:
        return _select_authorized_archive_document_replay_source_in_transaction(
            conn,
            tenant_id=tenant_id,
            owner_id=owner_id,
            origin_boundary_user_message_id=origin_boundary_user_message_id,
            corpus=corpus,
            source_ref=source_ref,
            knowledge_object_id=knowledge_object_id,
            source_revision=source_revision,
        )
    except ArchiveDocumentStorageError:
        raise
    except Exception:
        raise _fail("archive document replay selection is unavailable") from None


def _search_focused_document_lexical_lane(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
    request: ArchiveSearchRequest,
    execution_binding: SearchExecutionBinding,
    snapshot_discriminator: str,
    snapshot_current: bool,
    page_limit: int,
    document_passage_contract: _DocumentPassageContract | None,
) -> ArchiveDocumentLanePage:
    derivative_available = document_passage_contract is not None
    (
        total,
        examined,
        derivative_mismatches,
        derivative_backfills,
        derivative_unavailable,
        authority_backfill,
        lead_capped,
        lead_rows,
    ) = _focused_document_lexical_rows(
        conn,
        request,
        tenant_id=tenant_id,
        owner_id=owner_id,
        derivative_available=derivative_available,
        document_passage_revision=(
            document_passage_contract.index_revision if document_passage_contract is not None else None
        ),
    )

    body_max_bytes = _FOCUSED_DOCUMENT_BODY_MAX_BYTES
    body_budget_bytes = _FOCUSED_DOCUMENT_BODY_BUDGET_BYTES
    if (
        type(body_max_bytes) is not int
        or type(body_budget_bytes) is not int
        or not 1 <= body_max_bytes <= body_budget_bytes
    ):
        raise _fail("focused archive document body budget is invalid")

    projected: list[tuple[int, dict[str, Any], SourceFocusProjection]] = []
    body_budget_remaining = body_budget_bytes
    body_budget_incomplete = False
    for lead_order, row in enumerate(lead_rows):
        if body_budget_remaining <= 0:
            body_budget_incomplete = True
            break
        attempt_cap = min(body_max_bytes, body_budget_remaining)
        body_budget_remaining -= attempt_cap
        selected_body = _read_focused_document_body(
            conn,
            row,
            max_bytes=attempt_cap,
        )
        if selected_body is None:
            body_budget_incomplete = True
            continue
        body, consumed_bytes = selected_body
        body_budget_remaining += attempt_cap - consumed_bytes
        row["passage_body"] = body
        row["filename"] = row.get("raw_filename")
        try:
            projection = project_source_focus(
                body,
                request.query,
                request.focus,
                max_chars=_MAX_EXCERPT_CHARS,
            )
        except (TypeError, ValueError, UnicodeError):
            projection = None
        if projection is not None and not any(
            unicodedata.category(character).startswith("C") and character != "\n"
            for character in projection.excerpt
        ):
            try:
                projection.excerpt.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                row.pop("passage_body", None)
                continue
            projected.append((lead_order, row, projection))
        else:
            row.pop("passage_body", None)

    # The two recall leads establish only a stable candidate order.  Exact
    # source projection owns eligibility, and only its closed full/contextual
    # class may reorder that sequence.
    projected.sort(
        key=lambda item: (
            0 if item[2].focus_match_kind is SourceFocusMatchKind.FULL else 1,
            -item[2].matched_focus_count,
            item[0],
        )
    )
    # raw_fts has no authenticated completeness generation.  One valid indexed
    # target can survive after another target row disappeared, so a non-empty
    # page is evidence for what it shows but never proof of complete recall.
    # An authoritatively empty source scope remains the sole safe exhaustive case.
    if examined > 0:
        derivative_mismatches = max(1, derivative_mismatches)
        derivative_unavailable = max(1, derivative_unavailable)
    recall_capped = lead_capped or body_budget_incomplete
    for lane_rank, (_lead_order, row, _projection) in enumerate(projected, 1):
        row["lane_rank"] = lane_rank
    visible = projected[:page_limit]

    document_passages: dict[str, tuple[_StoredDocumentPassage, ...]] = {}
    invalid_current_passages = 0
    if document_passage_contract is not None:
        try:
            for _lead_order, row, _projection in visible:
                raw_object_id = row.get("raw_id")
                passages = _select_current_document_passages(
                    conn,
                    row,
                    document_passage_contract,
                )
                if type(raw_object_id) is str and passages is not None:
                    document_passages[raw_object_id] = passages
                else:
                    invalid_current_passages += 1
        except ArchiveDocumentStorageError:
            document_passages.clear()
            invalid_current_passages = len(visible)
    derivative_mismatches += invalid_current_passages
    derivative_unavailable += invalid_current_passages
    try:
        candidates = tuple(
            _candidate(
                row,
                corpus=ArchiveSearchCorpus.DOCUMENTS,
                lane=SearchLane.LEXICAL,
                request=request,
                tenant_id=tenant_id,
                owner_id=owner_id,
                document_passages=document_passages.get(str(row.get("raw_id"))),
                source_focus_projection=projection,
            )
            for _lead_order, row, projection in visible
        )
    except ArchiveDocumentStorageError:
        raise
    except Exception:
        raise _fail("archive source-focus evidence projection is unavailable") from None
    scope_complete = authority_backfill == 0
    return _new_page(
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        lane=SearchLane.LEXICAL,
        candidates=candidates,
        total=total if scope_complete else None,
        examined=examined,
        matched=len(projected),
        has_more=recall_capped or len(projected) > page_limit,
        available=True,
        applied_limit=page_limit,
        derivative_current=derivative_available and derivative_mismatches == 0,
        derivative_backfill_pending=derivative_backfills > 0,
        derivative_unavailable=(not derivative_available or derivative_unavailable > 0),
        catalog_projection_current=None,
        authority_scope_complete=scope_complete,
        authority_rechecked=True,
        snapshot_current=snapshot_current,
        execution_binding=execution_binding,
        request=request,
        tenant_id=tenant_id,
        owner_id=owner_id,
        snapshot_discriminator=snapshot_discriminator,
    )


def _search_archive_document_lane(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
    request: ArchiveSearchRequest,
    corpus: ArchiveSearchCorpus,
    lane: SearchLane,
    execution_binding: SearchExecutionBinding,
    snapshot_discriminator: str,
    snapshot_current: bool,
    dense_query_plan: ArchiveDenseQueryPlan | None = None,
    limit: int | None = None,
    maximum_results: int = MAX_ARCHIVE_DOCUMENT_RESULTS,
) -> ArchiveDocumentLanePage:
    """Search one authorized document/Knowledge lane in one SQLite snapshot.

    This function performs SELECT statements only and never starts, commits, or
    closes a transaction.  A caller needing multiple lanes in one exact snapshot
    must own that transaction around its calls.
    """

    if type(conn) is not sqlite3.Connection:
        raise _fail("archive document connection is invalid")
    if not conn.in_transaction:
        raise _fail("archive document search requires a caller-owned snapshot")
    _ensure_archive_search_fold(conn)
    if type(request) is not ArchiveSearchRequest:
        raise _fail("archive document request is invalid")
    if (
        type(corpus) is not ArchiveSearchCorpus
        or corpus not in _SUPPORTED_CORPORA
        or corpus not in request.corpora
    ):
        raise _fail("archive document corpus is invalid")
    if type(lane) is not SearchLane or lane not in _SUPPORTED_LANES:
        raise _fail("archive document lane is invalid")
    if type(snapshot_current) is not bool:
        raise _fail("archive document snapshot attestation is invalid")
    tenant = _actor(tenant_id)
    owner = _actor(owner_id)
    snapshot = _snapshot(snapshot_discriminator)
    page_limit = _limit(request.limit if limit is None else limit, maximum=maximum_results)
    target = (_SEARCH_CORPUS[corpus], lane)
    if (
        type(execution_binding) is not SearchExecutionBinding
        or not execution_binding.is_live_private_request_binding
        or execution_binding.authority_scope is not AuthorityScope.TENANT_PRINCIPAL
        or target not in execution_binding.requested_targets
        or not execution_binding.attests_private_request(request.to_identity_json())
        or not execution_binding.attests_authority(
            authority_scope=AuthorityScope.TENANT_PRINCIPAL,
            tenant_id=tenant,
            principal_id=owner,
        )
        or not execution_binding.attests_snapshot(snapshot)
    ):
        raise _fail("archive document execution binding is invalid")
    if not _authority_is_active(
        conn,
        tenant_id=tenant,
        owner_id=owner,
    ):
        return _unavailable_page(
            corpus,
            lane,
            execution_binding=execution_binding,
            request=request,
            tenant_id=tenant,
            owner_id=owner,
            snapshot_discriminator=snapshot,
            snapshot_current=snapshot_current,
            authority_rechecked=False,
            applied_limit=page_limit,
        )
    if request.continuation is not None or not _temporal_supported(request, corpus):
        return _unavailable_page(
            corpus,
            lane,
            execution_binding=execution_binding,
            request=request,
            tenant_id=tenant,
            owner_id=owner,
            snapshot_discriminator=snapshot,
            snapshot_current=snapshot_current,
            authority_rechecked=True,
            applied_limit=page_limit,
        )
    if request.focus and corpus is ArchiveSearchCorpus.DOCUMENTS and lane is SearchLane.CATALOG:
        return _unavailable_page(
            corpus,
            lane,
            execution_binding=execution_binding,
            request=request,
            tenant_id=tenant,
            owner_id=owner,
            snapshot_discriminator=snapshot,
            snapshot_current=snapshot_current,
            authority_rechecked=True,
            applied_limit=page_limit,
        )

    dense_projection = (
        project_archive_dense_query_plan(
            dense_query_plan,
            principal_id=owner,
            query=request.dense_query,
        )
        if lane is SearchLane.DENSE
        else None
    )
    if lane is SearchLane.DENSE:
        if dense_projection is None:
            return _unavailable_page(
                corpus,
                lane,
                execution_binding=execution_binding,
                request=request,
                tenant_id=tenant,
                owner_id=owner,
                snapshot_discriminator=snapshot,
                snapshot_current=snapshot_current,
                authority_rechecked=True,
                applied_limit=page_limit,
            )
        (
            total,
            examined,
            dense_authority_backfill,
            dense_derivative_unavailable,
            dense_recall_capped,
            hits,
        ) = _dense_rows(
            conn,
            tenant_id=tenant,
            owner_id=owner,
            request=request,
            corpus=corpus,
            projection=dense_projection,
        )
        visible_hits = hits[:page_limit]
        focused_document_passages: dict[str, tuple[_StoredDocumentPassage, ...]] = {}
        if request.focus and corpus is ArchiveSearchCorpus.DOCUMENTS:
            passage_contract = _load_document_passage_contract(conn)
            if passage_contract is not None:
                try:
                    for item in visible_hits:
                        raw_object_id = item.get("raw_id")
                        focus_projection = item.get("source_focus_projection")
                        passages = (
                            _select_current_dense_document_passage(
                                conn,
                                item,
                                passage_contract,
                                start=focus_projection.start,
                                end=focus_projection.end,
                            )
                            if type(focus_projection) is SourceFocusProjection
                            else None
                        )
                        if type(raw_object_id) is str and passages is not None:
                            focused_document_passages[raw_object_id] = passages
                except ArchiveDocumentStorageError:
                    focused_document_passages.clear()
        try:
            candidates = tuple(
                _candidate(
                    item,
                    corpus=corpus,
                    lane=lane,
                    request=request,
                    tenant_id=tenant,
                    owner_id=owner,
                    source_focus_projection=(item.get("source_focus_projection") if request.focus else None),
                    document_passages=focused_document_passages.get(str(item.get("raw_id"))),
                    dense_projection=dense_projection,
                )
                for item in visible_hits
            )
        except ArchiveDocumentStorageError:
            raise
        except Exception:
            raise _fail("archive dense evidence projection is unavailable") from None
        scope_complete = not dense_authority_backfill
        # This first slice authenticates every returned passage but deliberately
        # does not scan and prove the complete expected chunk topology for every
        # authorized source.  It may improve recall; it may not establish absence.
        derivative_current = False
        return _new_page(
            corpus=corpus,
            lane=lane,
            candidates=candidates,
            total=total if scope_complete else None,
            examined=examined,
            matched=len(hits),
            has_more=dense_recall_capped or len(hits) > page_limit,
            available=True,
            applied_limit=page_limit,
            derivative_current=derivative_current,
            derivative_backfill_pending=True,
            derivative_unavailable=dense_derivative_unavailable,
            catalog_projection_current=None,
            authority_scope_complete=scope_complete,
            authority_rechecked=True,
            snapshot_current=snapshot_current,
            execution_binding=execution_binding,
            request=request,
            tenant_id=tenant,
            owner_id=owner,
            snapshot_discriminator=snapshot,
        )

    document_catalog_available = False
    document_catalog_enrichment_revision = 0
    document_passage_contract: _DocumentPassageContract | None = None
    if corpus is ArchiveSearchCorpus.DOCUMENTS and lane is SearchLane.CATALOG:
        (
            document_catalog_available,
            document_catalog_enrichment_revision,
        ) = _document_catalog_contract(conn)
        if document_catalog_available:
            _ensure_archive_catalog_title_validator(conn)
    if lane is SearchLane.LEXICAL:
        terms = _fts_terms(request.query)
        focused_document_terms = bool(
            corpus is ArchiveSearchCorpus.DOCUMENTS
            and request.focus
            and source_focus_fts_tokens(request.query)
        )
        if not terms and not focused_document_terms:
            return _unavailable_page(
                corpus,
                lane,
                execution_binding=execution_binding,
                request=request,
                tenant_id=tenant,
                owner_id=owner,
                snapshot_discriminator=snapshot,
                snapshot_current=snapshot_current,
                authority_rechecked=True,
                applied_limit=page_limit,
            )
        if corpus is ArchiveSearchCorpus.DOCUMENTS:
            document_passage_contract = _load_document_passage_contract(conn)
            derivative_available = document_passage_contract is not None
            if request.focus:
                try:
                    return _search_focused_document_lexical_lane(
                        conn,
                        tenant_id=tenant,
                        owner_id=owner,
                        request=request,
                        execution_binding=execution_binding,
                        snapshot_discriminator=snapshot,
                        snapshot_current=snapshot_current,
                        page_limit=page_limit,
                        document_passage_contract=document_passage_contract,
                    )
                except ArchiveDocumentStorageError:
                    if not derivative_available:
                        raise
                    return _search_focused_document_lexical_lane(
                        conn,
                        tenant_id=tenant,
                        owner_id=owner,
                        request=request,
                        execution_binding=execution_binding,
                        snapshot_discriminator=snapshot,
                        snapshot_current=snapshot_current,
                        page_limit=page_limit,
                        document_passage_contract=None,
                    )
        else:
            derivative_available = _table_exists(conn, "knowledge_fts")
        sql, lane_parameters = _lexical_sql(
            corpus,
            request,
            derivative_available=derivative_available,
            document_passage_revision=(
                document_passage_contract.index_revision if document_passage_contract is not None else None
            ),
        )
    else:
        derivative_available = False
        sql, lane_parameters = _catalog_sql(
            corpus,
            request,
            owner,
            document_catalog_available=document_catalog_available,
            enrichment_revision=document_catalog_enrichment_revision,
        )

    parameters = (
        *_authority_parameters(tenant, owner),
        *lane_parameters,
        page_limit + 1,
    )
    try:
        rows = _select_rows(conn, sql, parameters)
    except ArchiveDocumentStorageError:
        if lane is not SearchLane.LEXICAL or not derivative_available:
            raise
        derivative_available = False
        document_passage_contract = None
        sql, lane_parameters = _lexical_sql(
            corpus,
            request,
            derivative_available=False,
        )
        parameters = (
            *_authority_parameters(tenant, owner),
            *lane_parameters,
            page_limit + 1,
        )
        rows = _select_rows(conn, sql, parameters)
    (
        total,
        examined,
        matched,
        derivative_mismatches,
        derivative_backfills,
        derivative_unavailable,
        authority_backfill,
        hits,
    ) = _summary(rows)
    visible_hits = hits[:page_limit]
    document_passages: dict[str, tuple[_StoredDocumentPassage, ...]] = {}
    invalid_current_passages = 0
    if (
        corpus is ArchiveSearchCorpus.DOCUMENTS
        and lane is SearchLane.LEXICAL
        and document_passage_contract is not None
    ):
        try:
            for item in visible_hits:
                if item.get("passage_projection_current") != 1:
                    continue
                raw_object_id = item.get("raw_id")
                passages = _select_current_document_passages(
                    conn,
                    item,
                    document_passage_contract,
                )
                if type(raw_object_id) is str and passages is not None:
                    document_passages[raw_object_id] = passages
                else:
                    invalid_current_passages += 1
        except ArchiveDocumentStorageError:
            # Keep the released body scan and ordering available when the
            # optional child read itself is unavailable.  Coverage and locator
            # identity both close to the legacy fallback.
            document_passages.clear()
            invalid_current_passages = sum(
                item.get("passage_projection_current") == 1 for item in visible_hits
            )
    derivative_mismatches += invalid_current_passages
    derivative_unavailable += invalid_current_passages
    try:
        candidates = tuple(
            _candidate(
                item,
                corpus=corpus,
                lane=lane,
                request=request,
                tenant_id=tenant,
                owner_id=owner,
                document_passages=document_passages.get(str(item.get("raw_id"))),
            )
            for item in visible_hits
        )
    except ArchiveDocumentStorageError:
        raise
    except Exception:
        raise _fail("archive document evidence projection is unavailable") from None
    scope_complete = authority_backfill == 0
    return _new_page(
        corpus=corpus,
        lane=lane,
        candidates=candidates,
        total=total if scope_complete else None,
        examined=examined,
        matched=matched,
        has_more=len(hits) > page_limit,
        available=True,
        applied_limit=page_limit,
        derivative_current=(
            derivative_available and derivative_mismatches == 0 if lane is SearchLane.LEXICAL else None
        ),
        derivative_backfill_pending=(
            (not derivative_available or derivative_backfills > 0)
            if lane is SearchLane.LEXICAL and corpus is not ArchiveSearchCorpus.DOCUMENTS
            else derivative_backfills > 0
            if lane is SearchLane.LEXICAL
            else None
        ),
        derivative_unavailable=(
            (not derivative_available or derivative_unavailable > 0)
            if lane is SearchLane.LEXICAL and corpus is ArchiveSearchCorpus.DOCUMENTS
            else False
            if lane is SearchLane.LEXICAL
            else None
        ),
        catalog_projection_current=(
            document_catalog_available and derivative_mismatches == 0
            if corpus is ArchiveSearchCorpus.DOCUMENTS and lane is SearchLane.CATALOG
            else None
        ),
        authority_scope_complete=scope_complete,
        authority_rechecked=True,
        snapshot_current=snapshot_current,
        execution_binding=execution_binding,
        request=request,
        tenant_id=tenant,
        owner_id=owner,
        snapshot_discriminator=snapshot,
    )


def search_archive_document_lane(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
    request: ArchiveSearchRequest,
    corpus: ArchiveSearchCorpus,
    lane: SearchLane,
    execution_binding: SearchExecutionBinding,
    snapshot_discriminator: str,
    snapshot_current: bool,
    dense_query_plan: ArchiveDenseQueryPlan | None = None,
    limit: int | None = None,
) -> ArchiveDocumentLanePage:
    """Search one lane through the released twenty-result storage seam."""

    return _search_archive_document_lane(
        conn,
        tenant_id=tenant_id,
        owner_id=owner_id,
        request=request,
        corpus=corpus,
        lane=lane,
        execution_binding=execution_binding,
        snapshot_discriminator=snapshot_discriminator,
        snapshot_current=snapshot_current,
        dense_query_plan=dense_query_plan,
        limit=limit,
        maximum_results=MAX_ARCHIVE_DOCUMENT_RESULTS,
    )


def _materialize_archive_document_lane(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
    request: ArchiveSearchRequest,
    corpus: ArchiveSearchCorpus,
    lane: SearchLane,
    execution_binding: SearchExecutionBinding,
    snapshot_discriminator: str,
    snapshot_current: bool,
    dense_query_plan: ArchiveDenseQueryPlan | None = None,
    limit: int,
) -> ArchiveDocumentLanePage:
    """Collect one bounded process-private lane tail for the archive facade."""

    return _search_archive_document_lane(
        conn,
        tenant_id=tenant_id,
        owner_id=owner_id,
        request=request,
        corpus=corpus,
        lane=lane,
        execution_binding=execution_binding,
        snapshot_discriminator=snapshot_discriminator,
        snapshot_current=snapshot_current,
        dense_query_plan=dense_query_plan,
        limit=limit,
        maximum_results=_MAX_ARCHIVE_DOCUMENT_MATERIALIZED_RESULTS,
    )


__all__ = [
    "ArchiveDocumentLanePage",
    "ArchiveDocumentReplaySource",
    "ArchiveDocumentStorageError",
    "DOCUMENT_STORED_PASSAGE_INDEX_VERSION",
    "MAX_ARCHIVE_DOCUMENT_RESULTS",
    "PASSAGE_INDEX_VERSION",
    "select_authorized_archive_document_replay_source_in_transaction",
    "search_archive_document_lane",
]
