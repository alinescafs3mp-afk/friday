"""Read-only document and Knowledge lanes for the archive-search facade.

The caller owns the SQLite connection and its snapshot.  Every query materializes
the exact tenant/owner/privacy/lifecycle scope before counting, matching, ranking,
or limiting.  Returned identifiers and source bodies remain process-private.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, NoReturn, SupportsIndex

from friday.raw_metadata import RAW_FILE_METADATA_MAX_BYTES
from friday.retrieval.archive_search_contract import (
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
from friday.storage._knowledge import _fts_terms
from friday.storage._privacy import (
    _not_audio_document,
    _not_private_inbox_dependency,
    _not_private_knowledge_dependency,
    _not_private_raw_dependency,
)

MAX_ARCHIVE_DOCUMENT_RESULTS: Final = 20
# Compatibility export for non-sidecar document producers and the Knowledge
# lane.  Only a verified current document-passage child uses the distinct v2
# identity imported above.
PASSAGE_INDEX_VERSION: Final = LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
_DOCUMENT_PASSAGE_MAX_COUNT: Final = 64
_MAX_ACTOR_BYTES = 200
_MAX_SNAPSHOT_BYTES = 256
_MAX_EXCERPT_CHARS = 720
_RAW_ID = re.compile(r"raw_[0-9a-f]{16}\Z")
_KO_ID = re.compile(r"ko_[A-Za-z0-9_-]{8,120}\Z")
_INBOX_ID = re.compile(r"inbox_[0-9a-f]{16}\Z")
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PASSAGE_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,119}\Z")
_SUPPORTED_CORPORA = frozenset({ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.KNOWLEDGE})
_SUPPORTED_LANES = frozenset({SearchLane.CATALOG, SearchLane.LEXICAL})
_SEARCH_CORPUS = {
    ArchiveSearchCorpus.DOCUMENTS: SearchCorpus.RAW_DOCUMENTS,
    ArchiveSearchCorpus.KNOWLEDGE: SearchCorpus.KNOWLEDGE,
}
_MATCH_CHANNEL = {
    SearchLane.CATALOG: ArchiveMatchChannel.CATALOG,
    SearchLane.LEXICAL: ArchiveMatchChannel.LEXICAL,
}
_PAGE_KEY = secrets.token_bytes(32)
_PAGE_PROCESS_AUTHORITY = object()
_REPLAY_FACTORY = object()

_SUPPORTED_TEMPORAL_ROLES = {
    ArchiveSearchCorpus.DOCUMENTS: frozenset(
        # ``raw_objects`` records receipt, not a separately attested upload
        # instant.  Treating the two roles as aliases would silently substitute
        # temporal meaning, so UPLOADED_AT remains explicitly unavailable.
        {TemporalRole.RECEIVED_AT}
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


def _limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ARCHIVE_DOCUMENT_RESULTS:
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
    return all(
        item.role in supported
        and item.value_kind is TemporalValueKind.INSTANT
        and item.precision is TemporalPrecision.INSTANT
        for item in _temporal_constraints(request, corpus)
    )


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
        ready = _canonical_utc_sql(expression)
        clauses.append(f"(NOT {ready} OR ({expression}>=? AND {expression}<?))")
        parameters.extend((constraint.start, constraint.end))
    return ("" if not clauses else " AND " + " AND ".join(clauses), tuple(parameters))


def _temporal_ready_expression(
    request: ArchiveSearchRequest,
    corpus: ArchiveSearchCorpus,
    temporal_expressions: dict[TemporalRole, str],
) -> str:
    expressions = tuple(temporal_expressions[item.role] for item in _temporal_constraints(request, corpus))
    if not expressions:
        return "1"
    return " AND ".join(_canonical_utc_sql(item) for item in expressions)


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
        if self.lane is SearchLane.LEXICAL:
            if any(
                type(item) is not bool
                for item in (
                    self.derivative_current,
                    self.derivative_backfill_pending,
                    self.derivative_unavailable,
                )
            ):
                raise _fail("archive lexical page requires derivative health")
            if self.derivative_current is (
                bool(self.derivative_backfill_pending) or bool(self.derivative_unavailable)
            ):
                raise _fail("archive lexical derivative health is inconsistent")
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
            if self.lane is SearchLane.LEXICAL:
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
            limit=self.returned if self.has_more else None,
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
        derivative_current=False if lane is SearchLane.LEXICAL else None,
        derivative_backfill_pending=False if lane is SearchLane.LEXICAL else None,
        derivative_unavailable=True if lane is SearchLane.LEXICAL else None,
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


def _catalog_metadata_valid(alias: str = "r") -> str:
    """Attest fields that decide document classification and navigation."""

    metadata = _safe_raw_metadata(alias)
    keys = ("filename", "mime_type", "mime", "content_type", "media_kind")
    quoted = ",".join(f"'{key}'" for key in keys)
    shape = " AND ".join(
        f"(json_type({metadata},'$.{key}') IS NULL OR json_type({metadata},'$.{key}') IN ('text','null'))"
        for key in keys
    )
    return f"""(
        NOT EXISTS (
            SELECT 1 FROM json_each({metadata}) catalog_member
             WHERE catalog_member.key IN ({quoted})
             GROUP BY CAST(catalog_member.key AS TEXT)
            HAVING COUNT(*)>1
        )
        AND {shape}
    )"""


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
    return f"({_raw_owner_attribution_valid(corpus, alias=alias)} AND {_catalog_metadata_valid(alias)})"


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
               knowledge_title, knowledge_summary, knowledge_tags_json, knowledge_created_at,
               knowledge_updated_at{knowledge_body_outer}
          FROM (
            SELECT k.rowid AS knowledge_rowid, k.id AS knowledge_id, k.raw_object_id,
                   k.version AS knowledge_version,
                   k.lifecycle_stage AS knowledge_lifecycle,
                   k.superseded_by_id AS knowledge_superseded_by_id,
                   k.title AS knowledge_title, k.summary AS knowledge_summary,
                   k.tags_json AS knowledge_tags_json,
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


_DOCUMENT_LIFECYCLE = "CASE WHEN ci.inbox_id IS NULL THEN COALESCE(ck.knowledge_lifecycle,'active') WHEN ci.status='pending' THEN 'pending' WHEN ci.status='classified' THEN 'classified' WHEN ci.status='archived' THEN 'archived' ELSE NULL END"
_DOCUMENT_REVIEW = "CASE WHEN ci.inbox_id IS NULL THEN CASE ck.knowledge_lifecycle WHEN 'active' THEN 'confirmed' WHEN 'archived' THEN 'archived' WHEN 'deprecated' THEN 'archived' ELSE NULL END WHEN ci.status='pending' THEN 'pending' WHEN ci.status='classified' THEN 'confirmed' WHEN ci.status='archived' THEN 'archived' ELSE NULL END"
_KNOWLEDGE_LIFECYCLE = "ck.knowledge_lifecycle"
_KNOWLEDGE_REVIEW = "CASE ck.knowledge_lifecycle WHEN 'active' THEN 'confirmed' ELSE 'archived' END"


def _source_cte(
    corpus: ArchiveSearchCorpus,
    request: ArchiveSearchRequest,
    *,
    include_body: bool,
) -> tuple[str, tuple[object, ...]]:
    common = _common_raw_authority(corpus, include_body=include_body)
    if corpus is ArchiveSearchCorpus.DOCUMENTS:
        temporal_expressions = {
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
        body_projection = ", ar.raw_content AS passage_body" if include_body else ""
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
                       ck.knowledge_created_at, ck.knowledge_updated_at,
                       {_DOCUMENT_LIFECYCLE} AS lifecycle_state,
                       {_DOCUMENT_REVIEW} AS review_state,
                       {_temporal_ready_expression(request, corpus, temporal_expressions)}
                           AS temporal_ready,
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
    return f"""CASE
        WHEN json_type({alias}.metadata_json,'$.filename')='text'
         AND length(json_extract({alias}.metadata_json,'$.filename')) BETWEEN 1 AND 260
         AND trim(json_extract({alias}.metadata_json,'$.filename'))=
             json_extract({alias}.metadata_json,'$.filename')
         AND instr(json_extract({alias}.metadata_json,'$.filename'),char(0))=0
         AND instr(json_extract({alias}.metadata_json,'$.filename'),char(10))=0
         AND instr(json_extract({alias}.metadata_json,'$.filename'),char(13))=0
        THEN json_extract({alias}.metadata_json,'$.filename') ELSE '' END"""


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
    rows = _select_rows(
        conn,
        """SELECT projection.raw_object_id AS projection_raw_object_id,
                      projection.source_version AS projection_source_version,
                      projection.source_content_sha256 AS projection_source_content_sha256,
                      projection.extracted_text_sha256 AS projection_extracted_text_sha256,
                      projection.source_char_count AS projection_source_char_count,
                      projection.passage_set_sha256 AS projection_passage_set_sha256,
                      projection.passage_index_revision AS projection_passage_index_revision,
                      projection.projection_status AS projection_status,
                      projection.incomplete_reason AS projection_incomplete_reason,
                      projection.passage_count AS projection_passage_count,
                      passage.chunk_index AS passage_chunk_index,
                      passage.start_char AS passage_start_char,
                      passage.end_char AS passage_end_char,
                      passage.content_sha256 AS passage_content_sha256
                 FROM document_passage_projections projection
                 JOIN document_passages passage
                   ON passage.raw_object_id=projection.raw_object_id
                WHERE projection.raw_object_id=?
                ORDER BY passage.chunk_index""",
        (raw_object_id,),
    )
    if not rows:
        return None
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
    title = _title_expression("s")
    folded_filename = "friday_archive_fold(c.filename)"
    folded_title = "friday_archive_fold(c.title)"
    folded_semantic_title = "friday_archive_fold(c.catalog_semantic_title)"
    folded_alias = "friday_archive_fold(a.supplied_filename)"
    folded_exact = "friday_archive_fold(n.exact)"
    folded_like = "friday_archive_fold(n.pattern)"
    exact_hit = f"""EXISTS (
        SELECT 1 FROM needles n
         WHERE {folded_filename}={folded_exact}
            OR {folded_title}={folded_exact}
            OR {folded_semantic_title}={folded_exact}
            OR EXISTS (SELECT 1 FROM authorized_aliases a
                        WHERE a.raw_object_id=c.raw_id AND {folded_alias}={folded_exact})
    )"""
    prefix_hit = f"""EXISTS (
        SELECT 1 FROM needles n
         WHERE {folded_filename} LIKE {folded_like}||'%' ESCAPE '\\'
            OR {folded_title} LIKE {folded_like}||'%' ESCAPE '\\'
            OR {folded_semantic_title} LIKE {folded_like}||'%' ESCAPE '\\'
            OR EXISTS (SELECT 1 FROM authorized_aliases a
                        WHERE a.raw_object_id=c.raw_id
                          AND {folded_alias} LIKE {folded_like}||'%' ESCAPE '\\')
    )"""
    substring_hit = f"""EXISTS (
        SELECT 1 FROM needles n
         WHERE {folded_filename} LIKE '%'||{folded_like}||'%' ESCAPE '\\'
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
            SELECT s.*, {filename} AS filename, {title} AS title,
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
        if constraint.role in {TemporalRole.RECEIVED_AT, TemporalRole.UPLOADED_AT}:
            revision = raw_revision
            value = row.get("raw_received_at")
            origin = TemporalOrigin.STORAGE_COLUMN
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
    passages: tuple[ArchiveSearchPassage, ...] = ()
    evidence_authority = ArchiveEvidenceAuthority.NAVIGATION_ONLY
    if (
        lane is SearchLane.LEXICAL
        and lifecycle_state is not LifecycleState.DEPRECATED
        and not (
            corpus is ArchiveSearchCorpus.KNOWLEDGE
            and row.get("inbox_status") == LifecycleState.PENDING.value
        )
    ):
        stored_excerpt = (
            _stored_document_excerpt(
                row.get("passage_body"),
                request.query,
                document_passages,
            )
            if corpus is ArchiveSearchCorpus.DOCUMENTS
            else None
        )
        excerpt = (
            (stored_excerpt[0], stored_excerpt[1], stored_excerpt[2])
            if stored_excerpt is not None
            else _excerpt(row.get("passage_body"), request.query)
        )
        if excerpt is not None:
            text, start, end = excerpt
            chunk_index = stored_excerpt[3] if stored_excerpt is not None else 0
            passage_index_version = (
                DOCUMENT_STORED_PASSAGE_INDEX_VERSION
                if stored_excerpt is not None
                else LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION
            )
            passage_ref = PassageRef.from_resolved_source(
                resolved,
                source_revision=passage_revision,
                locator=TextSpanLocator(chunk_index=chunk_index, start_char=start, end_char=end),
                passage_index_version=passage_index_version,
                embedding=EmbeddingIdentity.unindexed(EmbeddingCompatibility.NOT_APPLICABLE),
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


def _select_authorized_archive_document_replay_source_in_transaction(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    owner_id: str,
    origin_boundary_user_message_id: str,
    corpus: ArchiveSearchCorpus,
    source_ref: SourceRef,
    knowledge_object_id: str | None = None,
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
    source_cte, scope_parameters = _source_cte(corpus, request, include_body=True)
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
        )
    except ArchiveDocumentStorageError:
        raise
    except Exception:
        raise _fail("archive document replay selection is unavailable") from None


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
    limit: int | None = None,
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
    page_limit = _limit(request.limit if limit is None else limit)
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
        if not terms:
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
            )
        if corpus is ArchiveSearchCorpus.DOCUMENTS:
            document_passage_contract = _load_document_passage_contract(conn)
            derivative_available = document_passage_contract is not None
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
