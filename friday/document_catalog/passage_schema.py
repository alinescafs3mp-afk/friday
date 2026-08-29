"""Exact schema-48 contract for body-free Raw document passage projections.

Raw Object text remains the sole source of truth.  The projection table stores
only its exact revision/digests and closed coverage state; child rows store only
codepoint offsets and slice digests.  Review, tenant, filename, path, authority
and source bodies deliberately stay outside this rebuildable sidecar.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import unicodedata
import uuid
from collections import OrderedDict
from functools import lru_cache

from friday.document_catalog.schema import deterministic_document_extraction_state

DOCUMENT_PASSAGE_SCHEMA_VERSION = 48
DOCUMENT_PASSAGE_INDEX_REVISION = "document-char-v1:chunk-spans-v3:1200:200:64"
DOCUMENT_PASSAGE_MAX_CHARS = 1_200
DOCUMENT_PASSAGE_OVERLAP_CHARS = 200
DOCUMENT_PASSAGE_MAX_COUNT = 64
_RELEASED_DOCUMENT_PASSAGE_INDEX_REVISION_V2 = "document-char-v1:chunk-spans-v2:1200:200:64"

_PassageRows = tuple[tuple[int, int, int, str], ...]
_PASSAGE_ROWS_CACHE_LIMIT = 32
_PASSAGE_ROWS_CACHE: OrderedDict[str, _PassageRows] = OrderedDict()
_PASSAGE_ROWS_CACHE_LOCK = threading.Lock()
_PASSAGE_TABLE_REFERENCE_RE = re.compile(
    r"(?<![0-9A-Za-z_])(?:document_passage_projections|document_passages)(?![0-9A-Za-z_])",
    re.IGNORECASE,
)

_BACKFILL_PENDING = "backfill_pending"
_EXTRACTION_FAILED = "extraction_failed"
_SOURCE_UNAVAILABLE = "source_unavailable"
_SOURCE_CHANGED = "source_changed"
_REASONS = (
    _BACKFILL_PENDING,
    _EXTRACTION_FAILED,
    "extraction_incomplete",
    "no_text",
    "unsupported_content",
    _SOURCE_UNAVAILABLE,
    _SOURCE_CHANGED,
)
_REASON_SQL = ", ".join(f"'{item}'" for item in _REASONS)


_RELEASED_DOCUMENT_PASSAGE_SCHEMA_V2 = f"""
CREATE TABLE IF NOT EXISTS document_passage_projections (
    raw_object_id TEXT NOT NULL PRIMARY KEY
        REFERENCES raw_objects(id) ON DELETE CASCADE,
    source_version INTEGER
        CHECK(source_version IS NULL
              OR (typeof(source_version)='integer' AND source_version>=1)),
    source_content_sha256 TEXT
        CHECK(source_content_sha256 IS NULL
              OR (typeof(source_content_sha256)='text'
                  AND length(source_content_sha256)=64
                  AND source_content_sha256 NOT GLOB '*[^0-9a-f]*')),
    extracted_text_sha256 TEXT
        CHECK(extracted_text_sha256 IS NULL
              OR (typeof(extracted_text_sha256)='text'
                  AND length(extracted_text_sha256)=64
                  AND extracted_text_sha256 NOT GLOB '*[^0-9a-f]*')),
    source_char_count INTEGER
        CHECK(source_char_count IS NULL
              OR (typeof(source_char_count)='integer' AND source_char_count>=1)),
    passage_set_sha256 TEXT
        CHECK(passage_set_sha256 IS NULL
              OR (typeof(passage_set_sha256)='text'
                  AND length(passage_set_sha256)=64
                  AND passage_set_sha256 NOT GLOB '*[^0-9a-f]*')),
    passage_index_revision TEXT NOT NULL
        CHECK(passage_index_revision='{_RELEASED_DOCUMENT_PASSAGE_INDEX_REVISION_V2}'),
    projection_status TEXT NOT NULL
        CHECK(projection_status IN ('current','incomplete')),
    incomplete_reason TEXT
        CHECK(incomplete_reason IS NULL OR incomplete_reason IN ({_REASON_SQL})),
    passage_count INTEGER NOT NULL
        CHECK(typeof(passage_count)='integer'
              AND passage_count BETWEEN 0 AND {DOCUMENT_PASSAGE_MAX_COUNT}),
    projected_at TEXT NOT NULL
        CHECK(typeof(projected_at)='text'
              AND length(projected_at)=20
              AND projected_at GLOB
                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
              AND substr(projected_at,12,2) BETWEEN '00' AND '23'
              AND substr(projected_at,15,2) BETWEEN '00' AND '59'
              AND substr(projected_at,18,2) BETWEEN '00' AND '59'
              AND strftime('%Y-%m-%dT%H:%M:%SZ',projected_at)=projected_at),
    CHECK(
        (projection_status='current'
         AND incomplete_reason IS NULL
         AND source_version IS NOT NULL
         AND source_content_sha256 IS NOT NULL
         AND extracted_text_sha256 IS NOT NULL
         AND source_char_count IS NOT NULL
         AND passage_set_sha256 IS NOT NULL
         AND passage_count BETWEEN 1 AND {DOCUMENT_PASSAGE_MAX_COUNT})
        OR
        (projection_status='incomplete'
         AND incomplete_reason IS NOT NULL
         AND extracted_text_sha256 IS NULL
         AND source_char_count IS NULL
         AND passage_set_sha256 IS NULL
         AND passage_count=0)
    )
);

CREATE TABLE IF NOT EXISTS document_passages (
    raw_object_id TEXT NOT NULL
        REFERENCES document_passage_projections(raw_object_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL
        CHECK(typeof(chunk_index)='integer'
              AND chunk_index BETWEEN 0 AND {DOCUMENT_PASSAGE_MAX_COUNT - 1}),
    start_char INTEGER NOT NULL
        CHECK(typeof(start_char)='integer' AND start_char>=0),
    end_char INTEGER NOT NULL
        CHECK(typeof(end_char)='integer' AND end_char>start_char),
    content_sha256 TEXT NOT NULL
        CHECK(typeof(content_sha256)='text'
              AND length(content_sha256)=64
              AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(raw_object_id,chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_document_passage_projection_status
    ON document_passage_projections(projection_status,incomplete_reason,raw_object_id);
CREATE INDEX IF NOT EXISTS idx_document_passage_projection_text
    ON document_passage_projections(extracted_text_sha256,raw_object_id);
CREATE INDEX IF NOT EXISTS idx_document_passage_content
    ON document_passages(content_sha256,raw_object_id,chunk_index);

CREATE TRIGGER IF NOT EXISTS document_passage_projection_bi_validate
BEFORE INSERT ON document_passage_projections
WHEN NOT EXISTS (
    SELECT 1 FROM raw_objects source
     WHERE source.id=NEW.raw_object_id
       AND source.content_type='file' AND source.deleted_at IS NULL
       AND friday_document_passage_projection_valid(
               source.id,source.version,source.content_hash,
               source.raw_content,source.metadata_json,
               NEW.source_version,NEW.source_content_sha256,
               NEW.extracted_text_sha256,NEW.source_char_count,
               NEW.passage_set_sha256,
               NEW.passage_index_revision,NEW.projection_status,
               NEW.incomplete_reason,NEW.passage_count)=1
)
BEGIN
    SELECT RAISE(ABORT,'document_passage_projection_invalid');
END;

CREATE TRIGGER IF NOT EXISTS document_passage_projection_bu_validate
BEFORE UPDATE ON document_passage_projections
WHEN NEW.raw_object_id IS NOT OLD.raw_object_id
  OR NOT EXISTS (
    SELECT 1 FROM raw_objects source
     WHERE source.id=NEW.raw_object_id
       AND source.content_type='file' AND source.deleted_at IS NULL
       AND friday_document_passage_projection_valid(
               source.id,source.version,source.content_hash,
               source.raw_content,source.metadata_json,
               NEW.source_version,NEW.source_content_sha256,
               NEW.extracted_text_sha256,NEW.source_char_count,
               NEW.passage_set_sha256,
               NEW.passage_index_revision,NEW.projection_status,
               NEW.incomplete_reason,NEW.passage_count)=1
)
BEGIN
    SELECT RAISE(ABORT,'document_passage_projection_invalid');
END;

CREATE TRIGGER IF NOT EXISTS document_passage_bi_validate
BEFORE INSERT ON document_passages
WHEN NOT EXISTS (
    SELECT 1
      FROM document_passage_projections projection
      JOIN raw_objects source ON source.id=projection.raw_object_id
     WHERE projection.raw_object_id=NEW.raw_object_id
       AND projection.projection_status='current'
       AND NEW.chunk_index<projection.passage_count
       AND friday_document_passage_span_valid(
               source.raw_content,projection.extracted_text_sha256,
               NEW.chunk_index,NEW.start_char,
               NEW.end_char,NEW.content_sha256)=1
)
BEGIN
    SELECT RAISE(ABORT,'document_passage_span_invalid');
END;

CREATE TRIGGER IF NOT EXISTS document_passage_bu_validate
BEFORE UPDATE ON document_passages
WHEN NOT EXISTS (
    SELECT 1
      FROM document_passage_projections projection
      JOIN raw_objects source ON source.id=projection.raw_object_id
     WHERE projection.raw_object_id=NEW.raw_object_id
       AND projection.projection_status='current'
       AND NEW.raw_object_id IS OLD.raw_object_id
       AND NEW.chunk_index<projection.passage_count
       AND friday_document_passage_span_valid(
               source.raw_content,projection.extracted_text_sha256,
               NEW.chunk_index,NEW.start_char,
               NEW.end_char,NEW.content_sha256)=1
)
BEGIN
    SELECT RAISE(ABORT,'document_passage_span_invalid');
END;

CREATE TRIGGER IF NOT EXISTS document_passage_raw_ai_seed
AFTER INSERT ON raw_objects
WHEN NEW.content_type='file' AND NEW.deleted_at IS NULL
BEGIN
    INSERT INTO document_passage_projections(
        raw_object_id,source_version,source_content_sha256,
        extracted_text_sha256,source_char_count,passage_set_sha256,passage_index_revision,
        projection_status,incomplete_reason,passage_count,projected_at
    ) VALUES(
        NEW.id,
        CASE WHEN typeof(NEW.version)='integer' AND NEW.version>=1
             THEN NEW.version ELSE NULL END,
        CASE WHEN typeof(NEW.content_hash)='text' AND length(NEW.content_hash)=64
                  AND NEW.content_hash NOT GLOB '*[^0-9a-f]*'
             THEN NEW.content_hash ELSE NULL END,
        NULL,NULL,NULL,'{_RELEASED_DOCUMENT_PASSAGE_INDEX_REVISION_V2}','incomplete',
        friday_document_passage_seed_reason(
            NEW.version,NEW.content_hash,NEW.raw_content,NEW.metadata_json,0
        ),0,strftime('%Y-%m-%dT%H:%M:%SZ','now')
    );
END;

CREATE TRIGGER IF NOT EXISTS document_passage_raw_au_reset
AFTER UPDATE OF content_type,deleted_at,version,content_hash,raw_content,metadata_json
ON raw_objects
BEGIN
    DELETE FROM document_passage_projections WHERE raw_object_id=NEW.id;
    INSERT INTO document_passage_projections(
        raw_object_id,source_version,source_content_sha256,
        extracted_text_sha256,source_char_count,passage_set_sha256,passage_index_revision,
        projection_status,incomplete_reason,passage_count,projected_at
    )
    SELECT NEW.id,
           CASE WHEN typeof(NEW.version)='integer' AND NEW.version>=1
                THEN NEW.version ELSE NULL END,
           CASE WHEN typeof(NEW.content_hash)='text' AND length(NEW.content_hash)=64
                     AND NEW.content_hash NOT GLOB '*[^0-9a-f]*'
                THEN NEW.content_hash ELSE NULL END,
           NULL,NULL,NULL,'{_RELEASED_DOCUMENT_PASSAGE_INDEX_REVISION_V2}','incomplete',
           friday_document_passage_seed_reason(
               NEW.version,NEW.content_hash,NEW.raw_content,NEW.metadata_json,1
           ),0,strftime('%Y-%m-%dT%H:%M:%SZ','now')
     WHERE NEW.content_type='file' AND NEW.deleted_at IS NULL;
END;
"""

_REVISION_LITERAL_COUNT = _RELEASED_DOCUMENT_PASSAGE_SCHEMA_V2.count(
    _RELEASED_DOCUMENT_PASSAGE_INDEX_REVISION_V2
)
if _REVISION_LITERAL_COUNT != 3:  # pragma: no cover - source-code invariant
    raise RuntimeError("released document passage revision anchor is ambiguous")
DOCUMENT_PASSAGE_SCHEMA = _RELEASED_DOCUMENT_PASSAGE_SCHEMA_V2.replace(
    _RELEASED_DOCUMENT_PASSAGE_INDEX_REVISION_V2,
    DOCUMENT_PASSAGE_INDEX_REVISION,
)


def _valid_version(value: object) -> int | None:
    return value if type(value) is int and value >= 1 else None


def _valid_digest(value: object) -> str | None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        return None
    return value


def _exact_text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _released_v2_projection(
    *,
    raw_object_id: str,
    source_version: int,
    source_content_sha256: str,
    extracted_text: str,
) -> tuple[str, int, _PassageRows]:
    """Recompute the byte-exact released schema-47/v2 projection.

    This intentionally does not call the current projection class.  Schema 47
    must remain independently authenticatable after the document-only v3
    topology repair ships.
    """

    if not extracted_text.strip():
        raise ValueError("released document passage source text must be non-empty")
    if len(extracted_text) > 1_000_000_000:
        raise ValueError("released document passage source text exceeds the closed cap")
    if (
        not isinstance(raw_object_id, str)
        or not raw_object_id
        or raw_object_id != raw_object_id.strip()
        or any(unicodedata.category(character).startswith("C") for character in raw_object_id)
        or len(raw_object_id.encode("utf-8", errors="strict")) > 200
    ):
        raise ValueError("released document passage Raw Object ID is invalid")
    if _valid_version(source_version) is None or _valid_digest(source_content_sha256) is None:
        raise ValueError("released document passage source binding is invalid")
    from friday.retrieval import chunk_spans

    spans = tuple(
        chunk_spans(
            extracted_text,
            max_chars=DOCUMENT_PASSAGE_MAX_CHARS,
            overlap_chars=DOCUMENT_PASSAGE_OVERLAP_CHARS,
            max_chunks=DOCUMENT_PASSAGE_MAX_COUNT,
        )
    )
    if not 1 <= len(spans) <= DOCUMENT_PASSAGE_MAX_COUNT:
        raise ValueError("released document passage projection has no spans")
    if spans[0][0] != 0 or spans[-1][1] != len(extracted_text):
        raise ValueError("released document passages do not cover source boundaries")
    rows: list[tuple[int, int, int, str]] = []
    previous: tuple[int, int] | None = None
    for chunk_index, (start_char, end_char) in enumerate(spans):
        if (
            type(start_char) is not int
            or type(end_char) is not int
            or not 0 <= start_char < end_char <= 1_000_000_000
            or (
                previous is not None
                and (start_char <= previous[0] or start_char > previous[1] or end_char <= previous[1])
            )
        ):
            raise ValueError("released document passage topology is invalid")
        rows.append(
            (
                chunk_index,
                start_char,
                end_char,
                _exact_text_sha256(extracted_text[start_char:end_char]),
            )
        )
        previous = (start_char, end_char)
    return _exact_text_sha256(extracted_text), len(extracted_text), tuple(rows)


def _seed_reason(
    source_version: object,
    source_digest: object,
    raw_content: object,
    metadata_json: object,
    source_changed: object,
) -> str:
    if _valid_version(source_version) is None or _valid_digest(source_digest) is None:
        return _SOURCE_UNAVAILABLE
    state = deterministic_document_extraction_state(raw_content, metadata_json)
    if state == "current":
        return _SOURCE_CHANGED if source_changed == 1 else _BACKFILL_PENDING
    if state in _REASONS:
        return state
    return _EXTRACTION_FAILED


def _projection_valid(
    raw_object_id: object,
    raw_version: object,
    raw_digest: object,
    raw_content: object,
    metadata_json: object,
    source_version: object,
    source_digest: object,
    extracted_digest: object,
    source_char_count: object,
    passage_set_digest: object,
    index_revision: object,
    status: object,
    incomplete_reason: object,
    passage_count: object,
) -> int:
    try:
        if (
            type(raw_object_id) is not str
            or index_revision != DOCUMENT_PASSAGE_INDEX_REVISION
            or status not in {"current", "incomplete"}
            or type(passage_count) is not int
        ):
            return 0
        valid_version = _valid_version(raw_version)
        valid_digest = _valid_digest(raw_digest)
        if status == "current":
            from friday.document_catalog.passage_projection import DocumentPassageProjection

            if (
                valid_version is None
                or valid_digest is None
                or source_version != valid_version
                or source_digest != valid_digest
                or type(raw_content) is not str
                or deterministic_document_extraction_state(raw_content, metadata_json) != "current"
            ):
                return 0
            expected = DocumentPassageProjection.from_complete_text(
                raw_object_id=raw_object_id,
                source_version=valid_version,
                source_content_sha256=valid_digest,
                extracted_text=raw_content,
            )
            expected_rows = tuple(
                (
                    passage.chunk_index,
                    passage.start_char,
                    passage.end_char,
                    passage.content_sha256,
                )
                for passage in expected.passages
            )
            expected_text_digest = expected.extracted_text_sha256
            if expected_text_digest is None:
                return 0
            _remember_passage_rows(
                DOCUMENT_PASSAGE_INDEX_REVISION,
                expected_text_digest,
                expected_rows,
            )
            return int(
                incomplete_reason is None
                and extracted_digest == expected_text_digest
                and source_char_count == expected.source_char_count
                and passage_set_digest == document_passage_set_sha256(expected_rows)
                and passage_count == len(expected.passages)
            )
        expected_reason = _seed_reason(
            raw_version,
            raw_digest,
            raw_content,
            metadata_json,
            int(incomplete_reason == _SOURCE_CHANGED),
        )
        if incomplete_reason != expected_reason:
            return 0
        if passage_set_digest is not None:
            return 0
        if expected_reason == _SOURCE_UNAVAILABLE:
            return int(
                (source_version == valid_version if valid_version is not None else source_version is None)
                and (source_digest == valid_digest if valid_digest is not None else source_digest is None)
                and (valid_version is None or valid_digest is None)
            )
        return int(source_version == valid_version and source_digest == valid_digest)
    except (TypeError, ValueError, UnicodeError):
        return 0


def _released_v2_projection_valid(
    raw_object_id: object,
    raw_version: object,
    raw_digest: object,
    raw_content: object,
    metadata_json: object,
    source_version: object,
    source_digest: object,
    extracted_digest: object,
    source_char_count: object,
    passage_set_digest: object,
    index_revision: object,
    status: object,
    incomplete_reason: object,
    passage_count: object,
) -> int:
    """Validate frozen schema-47 rows without consulting v3 code."""

    try:
        if (
            type(raw_object_id) is not str
            or index_revision != _RELEASED_DOCUMENT_PASSAGE_INDEX_REVISION_V2
            or status not in {"current", "incomplete"}
            or type(passage_count) is not int
        ):
            return 0
        valid_version = _valid_version(raw_version)
        valid_digest = _valid_digest(raw_digest)
        if status == "current":
            if (
                valid_version is None
                or valid_digest is None
                or source_version != valid_version
                or source_digest != valid_digest
                or type(raw_content) is not str
                or deterministic_document_extraction_state(raw_content, metadata_json) != "current"
            ):
                return 0
            expected_text_digest, expected_char_count, expected_rows = _released_v2_projection(
                raw_object_id=raw_object_id,
                source_version=valid_version,
                source_content_sha256=valid_digest,
                extracted_text=raw_content,
            )
            _remember_passage_rows(
                _RELEASED_DOCUMENT_PASSAGE_INDEX_REVISION_V2,
                expected_text_digest,
                expected_rows,
            )
            return int(
                incomplete_reason is None
                and extracted_digest == expected_text_digest
                and source_char_count == expected_char_count
                and passage_set_digest == document_passage_set_sha256(expected_rows)
                and passage_count == len(expected_rows)
            )
        expected_reason = _seed_reason(
            raw_version,
            raw_digest,
            raw_content,
            metadata_json,
            int(incomplete_reason == _SOURCE_CHANGED),
        )
        if incomplete_reason != expected_reason or passage_set_digest is not None:
            return 0
        if expected_reason == _SOURCE_UNAVAILABLE:
            return int(
                (source_version == valid_version if valid_version is not None else source_version is None)
                and (source_digest == valid_digest if valid_digest is not None else source_digest is None)
                and (valid_version is None or valid_digest is None)
            )
        return int(source_version == valid_version and source_digest == valid_digest)
    except (TypeError, ValueError, UnicodeError):
        return 0


def _remember_passage_rows(revision: str, digest: str, rows: _PassageRows) -> None:
    with _PASSAGE_ROWS_CACHE_LOCK:
        cache_key = f"{revision}\0{digest}"
        _PASSAGE_ROWS_CACHE[cache_key] = rows
        _PASSAGE_ROWS_CACHE.move_to_end(cache_key)
        while len(_PASSAGE_ROWS_CACHE) > _PASSAGE_ROWS_CACHE_LIMIT:
            _PASSAGE_ROWS_CACHE.popitem(last=False)


def _passage_rows(
    raw_content: object,
    extracted_digest: object,
    *,
    revision: str = DOCUMENT_PASSAGE_INDEX_REVISION,
) -> _PassageRows | None:
    if type(raw_content) is not str or _valid_digest(extracted_digest) is None:
        return None
    digest = str(extracted_digest)
    cache_key = f"{revision}\0{digest}"
    with _PASSAGE_ROWS_CACHE_LOCK:
        cached = _PASSAGE_ROWS_CACHE.get(cache_key)
        if cached is not None:
            _PASSAGE_ROWS_CACHE.move_to_end(cache_key)
            return cached
    try:
        if revision == DOCUMENT_PASSAGE_INDEX_REVISION:
            from friday.document_catalog.passage_projection import DocumentPassageProjection

            projection = DocumentPassageProjection.from_complete_text(
                raw_object_id="raw_0000000000000000",
                source_version=1,
                source_content_sha256="0" * 64,
                extracted_text=raw_content,
            )
            text_digest = projection.extracted_text_sha256
            rows = tuple(
                (
                    passage.chunk_index,
                    passage.start_char,
                    passage.end_char,
                    passage.content_sha256,
                )
                for passage in projection.passages
            )
        elif revision == _RELEASED_DOCUMENT_PASSAGE_INDEX_REVISION_V2:
            text_digest, _source_char_count, rows = _released_v2_projection(
                raw_object_id="raw_0000000000000000",
                source_version=1,
                source_content_sha256="0" * 64,
                extracted_text=raw_content,
            )
        else:  # pragma: no cover - callers use closed revision constants
            return None
    except (TypeError, ValueError, UnicodeError):
        return None
    if text_digest != digest:
        return None
    _remember_passage_rows(revision, digest, rows)
    return rows


def document_passage_rows_match_current_projection(
    raw_content: object,
    extracted_digest: object,
    rows: object,
) -> bool:
    """Authenticate canonical stored rows against the exact current v3 body.

    The body digest is recomputed even on a cache hit.  This keeps callers from
    using a digest-keyed cached topology with different source text, while the
    exact tuple checks prevent list/tuple subclasses, booleans, or other
    equality-compatible carrier shapes from crossing the schema seam.
    """

    try:
        digest = _valid_digest(extracted_digest)
        if (
            type(raw_content) is not str
            or digest is None
            or _exact_text_sha256(raw_content) != digest
            or type(rows) is not tuple
            or not 1 <= len(rows) <= DOCUMENT_PASSAGE_MAX_COUNT
        ):
            return False
        for expected_index, row in enumerate(rows):
            if type(row) is not tuple or len(row) != 4:
                return False
            chunk_index, start_char, end_char, content_digest = row
            if (
                type(chunk_index) is not int
                or chunk_index != expected_index
                or type(start_char) is not int
                or type(end_char) is not int
                or not 0 <= start_char < end_char <= 1_000_000_000
                or _valid_digest(content_digest) is None
            ):
                return False
        expected = _passage_rows(raw_content, digest)
        return expected is not None and rows == expected
    except (TypeError, ValueError, UnicodeError):
        return False


def _span_valid(
    raw_content: object,
    extracted_digest: object,
    chunk_index: object,
    start_char: object,
    end_char: object,
    content_digest: object,
) -> int:
    try:
        if type(chunk_index) is not int:
            return 0
        rows = _passage_rows(raw_content, extracted_digest)
        if rows is None or not 0 <= chunk_index < len(rows):
            return 0
        return int((chunk_index, start_char, end_char, content_digest) == rows[chunk_index])
    except (TypeError, ValueError, UnicodeError):
        return 0


def _released_v2_span_valid(
    raw_content: object,
    extracted_digest: object,
    chunk_index: object,
    start_char: object,
    end_char: object,
    content_digest: object,
) -> int:
    try:
        if type(chunk_index) is not int:
            return 0
        rows = _passage_rows(
            raw_content,
            extracted_digest,
            revision=_RELEASED_DOCUMENT_PASSAGE_INDEX_REVISION_V2,
        )
        if rows is None or not 0 <= chunk_index < len(rows):
            return 0
        return int((chunk_index, start_char, end_char, content_digest) == rows[chunk_index])
    except (TypeError, ValueError, UnicodeError):
        return 0


def document_passage_set_sha256(rows: _PassageRows) -> str:
    """Hash one canonical ordered body-free passage set."""

    if type(rows) is not tuple or not 1 <= len(rows) <= DOCUMENT_PASSAGE_MAX_COUNT:
        raise ValueError("document passage set is empty or exceeds the closed cap")
    digest = hashlib.sha256(b"friday.document-passage-set.v1\0")
    for expected_index, row in enumerate(rows):
        if type(row) is not tuple or len(row) != 4:
            raise ValueError("document passage set row is malformed")
        chunk_index, start_char, end_char, content_digest = row
        if (
            type(chunk_index) is not int
            or chunk_index != expected_index
            or type(start_char) is not int
            or type(end_char) is not int
            or not 0 <= start_char < end_char <= 1_000_000_000
            or _valid_digest(content_digest) is None
        ):
            raise ValueError("document passage set row is invalid")
        digest.update(chunk_index.to_bytes(2, "big"))
        digest.update(start_char.to_bytes(8, "big"))
        digest.update(end_char.to_bytes(8, "big"))
        digest.update(bytes.fromhex(str(content_digest)))
    return digest.hexdigest()


class _PassageSetSha256:
    """Hash stored child rows once without re-reading or rechunking source text."""

    def __init__(self) -> None:
        self._rows: dict[int, tuple[int, int, int, str]] = {}
        self._invalid = False

    def step(self, *values: object) -> None:
        if self._invalid:
            return
        try:
            if len(values) != 4:
                self._invalid = True
                return
            chunk_index, start_char, end_char, content_digest = values
            if (
                type(chunk_index) is not int
                or type(start_char) is not int
                or type(end_char) is not int
                or type(content_digest) is not str
                or chunk_index in self._rows
                or len(self._rows) >= DOCUMENT_PASSAGE_MAX_COUNT
            ):
                self._invalid = True
                return
            self._rows[chunk_index] = (chunk_index, start_char, end_char, content_digest)
        except (TypeError, ValueError, UnicodeError, OverflowError):
            self._invalid = True

    def finalize(self) -> str:
        if self._invalid or not self._rows:
            return ""
        try:
            return document_passage_set_sha256(tuple(self._rows[index] for index in sorted(self._rows)))
        except (TypeError, ValueError, UnicodeError, OverflowError):
            return ""


def register_document_passage_connection_functions(conn: sqlite3.Connection) -> None:
    conn.create_function(
        "friday_document_passage_seed_reason",
        5,
        _seed_reason,
        deterministic=True,
    )
    conn.create_function(
        "friday_document_passage_projection_valid",
        14,
        _projection_valid,
        deterministic=True,
    )
    conn.create_function(
        "friday_document_passage_span_valid",
        6,
        _span_valid,
        deterministic=True,
    )
    conn.create_aggregate(
        "friday_document_passage_set_sha256",
        4,
        _PassageSetSha256,  # type: ignore[arg-type]  # typeshed cannot express fixed arity
    )


def _register_released_document_passage_v2_connection_functions(
    conn: sqlite3.Connection,
) -> None:
    """Temporarily bind the exact functions referenced by released v2 DDL."""

    conn.create_function(
        "friday_document_passage_seed_reason",
        5,
        _seed_reason,
        deterministic=True,
    )
    conn.create_function(
        "friday_document_passage_projection_valid",
        14,
        _released_v2_projection_valid,
        deterministic=True,
    )
    conn.create_function(
        "friday_document_passage_span_valid",
        6,
        _released_v2_span_valid,
        deterministic=True,
    )
    conn.create_aggregate(
        "friday_document_passage_set_sha256",
        4,
        _PassageSetSha256,  # type: ignore[arg-type]  # typeshed cannot express fixed arity
    )


def _v3_identity_unchanged(
    raw_object_id: object,
    source_version: object,
    source_digest: object,
    raw_content: object,
    extracted_digest: object,
    source_char_count: object,
    passage_set_digest: object,
    passage_count: object,
) -> int:
    """Return one only when a released CURRENT row is exactly v3-identical."""

    try:
        if (
            type(raw_object_id) is not str
            or type(source_version) is not int
            or type(source_digest) is not str
            or type(raw_content) is not str
        ):
            return 0
        from friday.document_catalog.passage_projection import DocumentPassageProjection

        expected = DocumentPassageProjection.from_complete_text(
            raw_object_id=raw_object_id,
            source_version=source_version,
            source_content_sha256=source_digest,
            extracted_text=raw_content,
        )
        rows = tuple(
            (
                passage.chunk_index,
                passage.start_char,
                passage.end_char,
                passage.content_sha256,
            )
            for passage in expected.passages
        )
        return int(
            expected.extracted_text_sha256 == extracted_digest
            and expected.source_char_count == source_char_count
            and document_passage_set_sha256(rows) == passage_set_digest
            and len(rows) == passage_count
        )
    except (TypeError, ValueError, UnicodeError):
        return 0


def _execute_schema(conn: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise sqlite3.DatabaseError("Document passage schema contains incomplete SQL")


def _normalize_schema_sql(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _schema_objects(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {
        (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
        for row in conn.execute(
            """SELECT type,name,sql FROM sqlite_master
                 WHERE sql IS NOT NULL
                   AND (name LIKE 'document_passage%'
                        OR tbl_name LIKE 'document_passage%'
                        OR name LIKE 'idx_document_passage_%')
                 ORDER BY type,name"""
        )
    }


def _validate_no_external_document_passage_dependencies(
    conn: sqlite3.Connection,
    *,
    canonical_objects: dict[tuple[str, str], str],
    schema_version: int,
) -> None:
    """Reject SQL objects outside the sidecar that depend on its tables.

    SQLite rewrites references in arbitrary triggers/views when a table is
    renamed.  The schema-47 rebuild therefore must prove that every object
    referring to either passage table is one of the exact authenticated owned
    objects before the first ``ALTER TABLE`` executes.
    """

    for kind, name, sql in conn.execute(
        """SELECT type,name,sql FROM sqlite_master
             WHERE sql IS NOT NULL
             ORDER BY type,name"""
    ):
        key = (str(kind), str(name))
        if key in canonical_objects:
            continue
        if _PASSAGE_TABLE_REFERENCE_RE.search(str(sql)) is not None:
            raise sqlite3.DatabaseError(
                f"Schema {schema_version} document passage external dependency is unexpected"
            )


def _schema_fingerprint(objects: dict[tuple[str, str], str]) -> str:
    material = "\n".join(f"{kind}\0{name}\0{sql}" for (kind, name), sql in sorted(objects.items()))
    return hashlib.sha256(material.encode("utf-8", errors="strict")).hexdigest()


@lru_cache(maxsize=1)
def _canonical_document_passage_schema_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        register_document_passage_connection_functions(conn)
        conn.executescript(
            """
            CREATE TABLE raw_objects(
                id TEXT PRIMARY KEY,
                raw_content TEXT NOT NULL DEFAULT '',
                content_type TEXT NOT NULL DEFAULT 'text',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT
            );
            """
        )
        _execute_schema(conn, DOCUMENT_PASSAGE_SCHEMA)
        return _schema_objects(conn)
    finally:
        conn.close()


@lru_cache(maxsize=1)
def _canonical_released_document_passage_schema_v2_objects() -> dict[tuple[str, str], str]:
    """Return the exact schema-47 sidecar shipped with the v2 revision."""

    conn = sqlite3.connect(":memory:")
    try:
        _register_released_document_passage_v2_connection_functions(conn)
        conn.executescript(
            """
            CREATE TABLE raw_objects(
                id TEXT PRIMARY KEY,
                raw_content TEXT NOT NULL DEFAULT '',
                content_type TEXT NOT NULL DEFAULT 'text',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT
            );
            """
        )
        _execute_schema(conn, _RELEASED_DOCUMENT_PASSAGE_SCHEMA_V2)
        return _schema_objects(conn)
    finally:
        conn.close()


def _validate_document_passage_data(
    conn: sqlite3.Connection,
    *,
    schema_version: int,
) -> None:
    mismatch = conn.execute(
        """SELECT 1
             FROM document_passage_projections projection
             LEFT JOIN raw_objects source ON source.id=projection.raw_object_id
            WHERE source.id IS NULL OR source.content_type<>'file'
               OR source.deleted_at IS NOT NULL
               OR friday_document_passage_projection_valid(
                      source.id,source.version,source.content_hash,
                      source.raw_content,source.metadata_json,
                      projection.source_version,projection.source_content_sha256,
                      projection.extracted_text_sha256,projection.source_char_count,
                      projection.passage_set_sha256,
                      projection.passage_index_revision,projection.projection_status,
                      projection.incomplete_reason,projection.passage_count)<>1
               OR (projection.projection_status='current' AND (
                    (SELECT COUNT(*) FROM document_passages passage
                      WHERE passage.raw_object_id=projection.raw_object_id)
                        <>projection.passage_count
                    OR COALESCE((
                        SELECT friday_document_passage_set_sha256(
                                   passage.chunk_index,passage.start_char,
                                   passage.end_char,passage.content_sha256)
                          FROM document_passages passage
                         WHERE passage.raw_object_id=projection.raw_object_id
                    ),'')<>projection.passage_set_sha256
               ))
               OR (projection.projection_status='incomplete' AND EXISTS (
                    SELECT 1 FROM document_passages passage
                     WHERE passage.raw_object_id=projection.raw_object_id
               ))
            LIMIT 1"""
    ).fetchone()
    if mismatch is not None:
        raise sqlite3.DatabaseError(f"Schema {schema_version} document passage data is invalid")
    orphan = conn.execute(
        """SELECT 1
             FROM document_passages passage
             LEFT JOIN document_passage_projections projection
                    ON projection.raw_object_id=passage.raw_object_id
            WHERE projection.raw_object_id IS NULL
            LIMIT 1"""
    ).fetchone()
    if orphan is not None:
        raise sqlite3.DatabaseError(f"Schema {schema_version} document passage data is invalid")
    missing = conn.execute(
        """SELECT 1 FROM raw_objects source
            WHERE source.content_type='file' AND source.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM document_passage_projections projection
                   WHERE projection.raw_object_id=source.id
              )
            LIMIT 1"""
    ).fetchone()
    if missing is not None:
        raise sqlite3.DatabaseError(f"Schema {schema_version} document passage coverage is incomplete")


def _validate_document_passage_shape(
    conn: sqlite3.Connection,
    *,
    schema_version: int,
) -> None:
    projection_columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(document_passage_projections)")
    }
    if projection_columns != {
        "raw_object_id": ("TEXT", 1, 1),
        "source_version": ("INTEGER", 0, 0),
        "source_content_sha256": ("TEXT", 0, 0),
        "extracted_text_sha256": ("TEXT", 0, 0),
        "source_char_count": ("INTEGER", 0, 0),
        "passage_set_sha256": ("TEXT", 0, 0),
        "passage_index_revision": ("TEXT", 1, 0),
        "projection_status": ("TEXT", 1, 0),
        "incomplete_reason": ("TEXT", 0, 0),
        "passage_count": ("INTEGER", 1, 0),
        "projected_at": ("TEXT", 1, 0),
    }:
        raise sqlite3.DatabaseError(f"Schema {schema_version} document passage projection shape is invalid")
    passage_columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(document_passages)")
    }
    if passage_columns != {
        "raw_object_id": ("TEXT", 1, 1),
        "chunk_index": ("INTEGER", 1, 2),
        "start_char": ("INTEGER", 1, 0),
        "end_char": ("INTEGER", 1, 0),
        "content_sha256": ("TEXT", 1, 0),
    }:
        raise sqlite3.DatabaseError(f"Schema {schema_version} document passage row shape is invalid")
    projection_fks = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(document_passage_projections)")
    }
    passage_fks = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(document_passages)")
    }
    if projection_fks != {("raw_object_id", "raw_objects", "id", "CASCADE")} or passage_fks != {
        ("raw_object_id", "document_passage_projections", "raw_object_id", "CASCADE")
    }:
        raise sqlite3.DatabaseError(f"Schema {schema_version} document passage ownership is invalid")


def validate_document_passage_schema(
    conn: sqlite3.Connection,
    *,
    required: bool = True,
    validate_data: bool = True,
) -> None:
    """Fail closed when the exact body-free passage sidecar is weakened."""

    register_document_passage_connection_functions(conn)
    installed = _schema_objects(conn)
    if not installed:
        if required:
            raise sqlite3.DatabaseError("Schema 48 document passage projection is missing")
        return
    if installed != _canonical_document_passage_schema_objects():
        raise sqlite3.DatabaseError("Schema 48 document passage DDL is incomplete or altered")
    _validate_no_external_document_passage_dependencies(
        conn,
        canonical_objects=_canonical_document_passage_schema_objects(),
        schema_version=48,
    )
    _validate_document_passage_shape(conn, schema_version=48)
    if validate_data:
        _validate_document_passage_data(conn, schema_version=48)


def _validate_released_document_passage_schema_v2(
    conn: sqlite3.Connection,
    *,
    required: bool = True,
    validate_data: bool = True,
) -> None:
    """Authenticate the exact released schema-47 DDL and v2 projection data."""

    installed = _schema_objects(conn)
    if not installed:
        if required:
            raise sqlite3.DatabaseError("Schema 47 document passage projection is missing")
        return
    if installed != _canonical_released_document_passage_schema_v2_objects():
        raise sqlite3.DatabaseError("Schema 47 document passage DDL is incomplete or altered")
    _validate_no_external_document_passage_dependencies(
        conn,
        canonical_objects=_canonical_released_document_passage_schema_v2_objects(),
        schema_version=47,
    )
    _validate_document_passage_shape(conn, schema_version=47)
    if not validate_data:
        return
    _register_released_document_passage_v2_connection_functions(conn)
    try:
        _validate_document_passage_data(conn, schema_version=47)
    finally:
        register_document_passage_connection_functions(conn)


def _drop_released_document_passage_runtime_objects(conn: sqlite3.Connection) -> None:
    for (kind, name), _sql in _canonical_released_document_passage_schema_v2_objects().items():
        if kind in {"index", "trigger"}:
            conn.execute(f'DROP {kind.upper()} "{name}"')


def upgrade_document_passage_schema_47_to_48(
    conn: sqlite3.Connection,
    *,
    required: bool = True,
) -> None:
    """Authenticate and atomically rebuild only the released passage sidecar.

    A released CURRENT projection is carried only when its exact source/text
    identity, ordered coordinates and slice-digest set recompute identically
    under v3.  Every other CURRENT row becomes body-free ``backfill_pending``;
    the ordinary bounded writer owns its later repair.
    """

    if not conn.in_transaction:
        raise RuntimeError("Document passage schema upgrade requires an existing transaction")
    installed = _schema_objects(conn)
    if not installed:
        if required:
            raise sqlite3.DatabaseError("Schema 47 document passage projection is missing")
        return
    # No current-schema shortcut is accepted under an old marker.  An exact v3
    # sidecar with schema_version <=47 cannot be the product of this atomic
    # migration; accepting it would let mixed/interrupted DDL bypass the frozen
    # predecessor authentication below.
    _validate_released_document_passage_schema_v2(conn)

    suffix = uuid.uuid4().hex
    carry_table = f"friday_document_passage_v3_carry_{suffix}"
    savepoint = f"document_passage_47_to_48_{suffix}"
    conn.create_function(
        "friday_document_passage_v3_identity_unchanged",
        8,
        _v3_identity_unchanged,
        deterministic=True,
    )
    conn.execute(f'SAVEPOINT "{savepoint}"')  # nosec B608 - generated hexadecimal identifier
    try:
        conn.execute(
            f'''CREATE TEMP TABLE "{carry_table}"(
                    raw_object_id TEXT NOT NULL PRIMARY KEY
                ) WITHOUT ROWID'''  # nosec B608 - generated hexadecimal identifier
        )
        conn.execute(
            f'''INSERT INTO "{carry_table}"(raw_object_id)
                SELECT projection.raw_object_id
                  FROM document_passage_projections projection
                  JOIN raw_objects source ON source.id=projection.raw_object_id
                 WHERE projection.projection_status='current'
                   AND friday_document_passage_v3_identity_unchanged(
                           source.id,source.version,source.content_hash,source.raw_content,
                           projection.extracted_text_sha256,projection.source_char_count,
                           projection.passage_set_sha256,projection.passage_count)=1'''  # nosec B608
        )

        _drop_released_document_passage_runtime_objects(conn)
        conn.execute("ALTER TABLE document_passages RENAME TO document_passages_schema47")
        conn.execute(
            "ALTER TABLE document_passage_projections RENAME TO document_passage_projections_schema47"
        )
        _execute_schema(conn, DOCUMENT_PASSAGE_SCHEMA)
        conn.execute(
            f'''INSERT INTO document_passage_projections(
                    raw_object_id,source_version,source_content_sha256,
                    extracted_text_sha256,source_char_count,passage_set_sha256,
                    passage_index_revision,projection_status,incomplete_reason,
                    passage_count,projected_at
                )
                SELECT predecessor.raw_object_id,
                       predecessor.source_version,
                       predecessor.source_content_sha256,
                       CASE WHEN carry.raw_object_id IS NOT NULL
                            THEN predecessor.extracted_text_sha256 ELSE NULL END,
                       CASE WHEN carry.raw_object_id IS NOT NULL
                            THEN predecessor.source_char_count ELSE NULL END,
                       CASE WHEN carry.raw_object_id IS NOT NULL
                            THEN predecessor.passage_set_sha256 ELSE NULL END,
                       '{DOCUMENT_PASSAGE_INDEX_REVISION}',
                       CASE WHEN carry.raw_object_id IS NOT NULL
                            THEN 'current' ELSE 'incomplete' END,
                       CASE WHEN carry.raw_object_id IS NOT NULL THEN NULL
                            WHEN predecessor.projection_status='current'
                            THEN friday_document_passage_seed_reason(
                                     source.version,source.content_hash,
                                     source.raw_content,source.metadata_json,0)
                            ELSE predecessor.incomplete_reason END,
                       CASE WHEN carry.raw_object_id IS NOT NULL
                            THEN predecessor.passage_count ELSE 0 END,
                       CASE WHEN predecessor.projection_status='current'
                                      AND carry.raw_object_id IS NULL
                            THEN strftime('%Y-%m-%dT%H:%M:%SZ','now')
                            ELSE predecessor.projected_at END
                  FROM document_passage_projections_schema47 predecessor
                  JOIN raw_objects source ON source.id=predecessor.raw_object_id
                  LEFT JOIN "{carry_table}" carry
                         ON carry.raw_object_id=predecessor.raw_object_id'''  # nosec B608
        )
        conn.execute(
            f'''INSERT INTO document_passages(
                    raw_object_id,chunk_index,start_char,end_char,content_sha256
                )
                SELECT predecessor.raw_object_id,predecessor.chunk_index,
                       predecessor.start_char,predecessor.end_char,
                       predecessor.content_sha256
                  FROM document_passages_schema47 predecessor
                  JOIN "{carry_table}" carry
                    ON carry.raw_object_id=predecessor.raw_object_id
                 ORDER BY predecessor.raw_object_id,predecessor.chunk_index'''  # nosec B608
        )
        conn.execute("DROP TABLE document_passages_schema47")
        conn.execute("DROP TABLE document_passage_projections_schema47")
        conn.execute(f'DROP TABLE "{carry_table}"')  # nosec B608 - generated identifier
        validate_document_passage_schema(conn)
    except BaseException:
        conn.execute(f'ROLLBACK TO SAVEPOINT "{savepoint}"')  # nosec B608
        conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')  # nosec B608
        raise
    finally:
        conn.create_function(
            "friday_document_passage_v3_identity_unchanged",
            8,
            None,
        )
    conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')  # nosec B608


def document_passage_schema_fingerprint(conn: sqlite3.Connection) -> str:
    validate_document_passage_schema(conn, validate_data=False)
    installed = _schema_objects(conn)
    canonical = _canonical_document_passage_schema_objects()
    if installed != canonical:
        raise sqlite3.DatabaseError("Schema 48 document passage DDL is incomplete or altered")
    return _schema_fingerprint(installed)


def install_document_passage_schema(conn: sqlite3.Connection) -> None:
    """Authenticate, install and seed the reader-first schema inside migration."""

    if not conn.in_transaction:
        raise RuntimeError("Document passage installation requires an existing transaction")
    installed = _schema_objects(conn)
    if installed and installed != _canonical_document_passage_schema_objects():
        raise sqlite3.DatabaseError("Schema 48 document passage DDL is incomplete or altered")
    if not installed:
        _execute_schema(conn, DOCUMENT_PASSAGE_SCHEMA)
    conn.execute(
        f"""INSERT INTO document_passage_projections(
                raw_object_id,source_version,source_content_sha256,
                extracted_text_sha256,source_char_count,
                passage_set_sha256,passage_index_revision,
                projection_status,incomplete_reason,passage_count,projected_at
            )
            SELECT source.id,
                   CASE WHEN typeof(source.version)='integer' AND source.version>=1
                        THEN source.version ELSE NULL END,
                   CASE WHEN typeof(source.content_hash)='text'
                              AND length(source.content_hash)=64
                              AND source.content_hash NOT GLOB '*[^0-9a-f]*'
                        THEN source.content_hash ELSE NULL END,
                   NULL,NULL,NULL,'{DOCUMENT_PASSAGE_INDEX_REVISION}','incomplete',
                   friday_document_passage_seed_reason(
                       source.version,source.content_hash,
                       source.raw_content,source.metadata_json,0
                   ),0,strftime('%Y-%m-%dT%H:%M:%SZ','now')
              FROM raw_objects source
             WHERE source.content_type='file' AND source.deleted_at IS NULL
            ON CONFLICT(raw_object_id) DO NOTHING"""
    )
    validate_document_passage_schema(conn)


__all__ = [
    "DOCUMENT_PASSAGE_INDEX_REVISION",
    "DOCUMENT_PASSAGE_SCHEMA",
    "DOCUMENT_PASSAGE_SCHEMA_VERSION",
    "document_passage_schema_fingerprint",
    "document_passage_rows_match_current_projection",
    "document_passage_set_sha256",
    "install_document_passage_schema",
    "register_document_passage_connection_functions",
    "upgrade_document_passage_schema_47_to_48",
    "validate_document_passage_schema",
]
