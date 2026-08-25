"""Exact schema-41 contract for the rebuildable DocumentCatalog sidecar.

The table intentionally contains no source body, summary, tags, arbitrary metadata,
or model response.  Raw Object remains the authority; this projection only carries
an exact revision binding and bounded navigation metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from enum import StrEnum
from functools import lru_cache
from typing import Any

DOCUMENT_CATALOG_SCHEMA_VERSION = 41
DOCUMENT_CATALOG_ENRICHMENT_REVISION = 1


class DocumentCatalogStatus(StrEnum):
    CURRENT = "current"
    INCOMPLETE = "incomplete"


class DocumentCatalogIncompleteReason(StrEnum):
    BACKFILL_PENDING = "backfill_pending"
    EXTRACTION_FAILED = "extraction_failed"
    EXTRACTION_INCOMPLETE = "extraction_incomplete"
    NO_TEXT = "no_text"
    UNSUPPORTED_CONTENT = "unsupported_content"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_CHANGED = "source_changed"


DOCUMENT_CATALOG_INCOMPLETE_REASONS = tuple(item.value for item in DocumentCatalogIncompleteReason)
_REASON_SQL = ", ".join(f"'{item}'" for item in DOCUMENT_CATALOG_INCOMPLETE_REASONS)
_EXTRACTION_LOSS_FLAGS = (
    "parse_deadline_reached",
    "parse_pages_truncated",
    "pages_truncated",
    "text_truncated",
    "rows_truncated",
    "extraction_truncated",
    "archive_truncated",
    "archive_budget_exhausted",
    "source_truncated_for_parse",
    "page_cap_reached",
    "partial",
)
_EXTRACTION_ADVISORY_FLAGS = (
    "vision_used",
    "vision_review_required",
    "unsupported_format",
    "advisory_only",
)
_EXTRACTION_COUNTERS = (
    "parse_pages_read",
    "parse_total_pages",
    "vision_pages_total",
    "vision_pages_read",
    "archive_files",
    "archive_files_read",
)

DOCUMENT_CATALOG_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS document_catalog (
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
    semantic_title TEXT
        CHECK(semantic_title IS NULL
              OR (semantic_title=trim(semantic_title)
                  AND length(semantic_title) BETWEEN 1 AND 240
                  AND length(CAST(semantic_title AS BLOB))<=1024)),
    title_authority TEXT NOT NULL DEFAULT 'navigation_only'
        CHECK(title_authority='navigation_only'),
    enrichment_revision INTEGER NOT NULL
        CHECK(typeof(enrichment_revision)='integer'
              AND enrichment_revision={DOCUMENT_CATALOG_ENRICHMENT_REVISION}),
    enrichment_status TEXT NOT NULL
        CHECK(enrichment_status IN ('current','incomplete')),
    incomplete_reason TEXT
        CHECK(incomplete_reason IS NULL OR incomplete_reason IN ({_REASON_SQL})),
    enriched_at TEXT NOT NULL
        CHECK(typeof(enriched_at)='text'
              AND length(enriched_at)=20
              AND enriched_at GLOB
                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
              AND substr(enriched_at,12,2) BETWEEN '00' AND '23'
              AND substr(enriched_at,15,2) BETWEEN '00' AND '59'
              AND substr(enriched_at,18,2) BETWEEN '00' AND '59'
              AND strftime('%Y-%m-%dT%H:%M:%SZ',enriched_at)=enriched_at),
    CHECK(
        (enrichment_status='current'
         AND incomplete_reason IS NULL
         AND source_version IS NOT NULL
         AND source_content_sha256 IS NOT NULL
         AND extracted_text_sha256 IS NOT NULL)
        OR
        (enrichment_status='incomplete'
         AND incomplete_reason IS NOT NULL
         AND extracted_text_sha256 IS NULL
         AND semantic_title IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_document_catalog_status
    ON document_catalog(enrichment_status, raw_object_id);
CREATE INDEX IF NOT EXISTS idx_document_catalog_reason
    ON document_catalog(incomplete_reason, raw_object_id)
    WHERE enrichment_status='incomplete';
CREATE INDEX IF NOT EXISTS idx_document_catalog_text
    ON document_catalog(extracted_text_sha256, raw_object_id)
    WHERE enrichment_status='current';

CREATE TRIGGER IF NOT EXISTS document_catalog_bi_validate
BEFORE INSERT ON document_catalog
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM raw_objects source
         WHERE source.id=NEW.raw_object_id
           AND source.content_type='file'
           AND source.deleted_at IS NULL
           AND (
                source.version=NEW.source_version
                OR (NEW.enrichment_status='incomplete'
                    AND NEW.incomplete_reason='source_unavailable'
                    AND NEW.source_version IS NULL
                    AND (typeof(source.version)<>'integer' OR source.version<1))
           )
           AND (
                source.content_hash=NEW.source_content_sha256
                OR (NEW.enrichment_status='incomplete'
                    AND NEW.incomplete_reason='source_unavailable'
                    AND NEW.source_content_sha256 IS NULL
                    AND (typeof(source.content_hash)<>'text'
                         OR length(source.content_hash)<>64
                         OR source.content_hash GLOB '*[^0-9a-f]*'))
           )
           AND (
                NEW.enrichment_status='current'
                OR (
                    NEW.incomplete_reason IN ('backfill_pending','source_changed')
                    AND typeof(source.version)='integer' AND source.version>=1
                    AND typeof(source.content_hash)='text'
                    AND length(source.content_hash)=64
                    AND source.content_hash NOT GLOB '*[^0-9a-f]*'
                )
                OR (
                    NEW.incomplete_reason='source_unavailable'
                    AND (typeof(source.version)<>'integer' OR source.version<1
                         OR typeof(source.content_hash)<>'text'
                         OR length(source.content_hash)<>64
                         OR source.content_hash GLOB '*[^0-9a-f]*')
                )
                OR (
                    NEW.incomplete_reason IN (
                        'extraction_failed','extraction_incomplete',
                        'no_text','unsupported_content'
                    )
                    AND typeof(source.version)='integer' AND source.version>=1
                    AND typeof(source.content_hash)='text'
                    AND length(source.content_hash)=64
                    AND source.content_hash NOT GLOB '*[^0-9a-f]*'
                    AND NEW.incomplete_reason=
                        friday_document_catalog_extraction_state(
                            source.raw_content,source.metadata_json)
                )
           )
           AND (NEW.enrichment_status<>'current' OR (
                typeof(source.version)='integer' AND source.version>=1
                AND typeof(source.content_hash)='text'
                AND length(source.content_hash)=64
                AND source.content_hash NOT GLOB '*[^0-9a-f]*'
                AND friday_document_catalog_extraction_state(
                        source.raw_content,source.metadata_json)='current'
                AND friday_exact_text_sha256(source.raw_content)=NEW.extracted_text_sha256
                AND NEW.semantic_title IS friday_document_catalog_semantic_title(source.raw_content)
           ))
    ) THEN RAISE(ABORT,'document catalog source binding is invalid') END;
END;

CREATE TRIGGER IF NOT EXISTS document_catalog_bu_validate
BEFORE UPDATE ON document_catalog
BEGIN
    SELECT CASE WHEN NEW.raw_object_id<>OLD.raw_object_id
                     OR NOT EXISTS (
        SELECT 1 FROM raw_objects source
         WHERE source.id=NEW.raw_object_id
           AND source.content_type='file'
           AND source.deleted_at IS NULL
           AND (
                source.version=NEW.source_version
                OR (NEW.enrichment_status='incomplete'
                    AND NEW.incomplete_reason='source_unavailable'
                    AND NEW.source_version IS NULL
                    AND (typeof(source.version)<>'integer' OR source.version<1))
           )
           AND (
                source.content_hash=NEW.source_content_sha256
                OR (NEW.enrichment_status='incomplete'
                    AND NEW.incomplete_reason='source_unavailable'
                    AND NEW.source_content_sha256 IS NULL
                    AND (typeof(source.content_hash)<>'text'
                         OR length(source.content_hash)<>64
                         OR source.content_hash GLOB '*[^0-9a-f]*'))
           )
           AND (
                NEW.enrichment_status='current'
                OR (
                    NEW.incomplete_reason IN ('backfill_pending','source_changed')
                    AND typeof(source.version)='integer' AND source.version>=1
                    AND typeof(source.content_hash)='text'
                    AND length(source.content_hash)=64
                    AND source.content_hash NOT GLOB '*[^0-9a-f]*'
                )
                OR (
                    NEW.incomplete_reason='source_unavailable'
                    AND (typeof(source.version)<>'integer' OR source.version<1
                         OR typeof(source.content_hash)<>'text'
                         OR length(source.content_hash)<>64
                         OR source.content_hash GLOB '*[^0-9a-f]*')
                )
                OR (
                    NEW.incomplete_reason IN (
                        'extraction_failed','extraction_incomplete',
                        'no_text','unsupported_content'
                    )
                    AND typeof(source.version)='integer' AND source.version>=1
                    AND typeof(source.content_hash)='text'
                    AND length(source.content_hash)=64
                    AND source.content_hash NOT GLOB '*[^0-9a-f]*'
                    AND NEW.incomplete_reason=
                        friday_document_catalog_extraction_state(
                            source.raw_content,source.metadata_json)
                )
           )
           AND (NEW.enrichment_status<>'current' OR (
                typeof(source.version)='integer' AND source.version>=1
                AND typeof(source.content_hash)='text'
                AND length(source.content_hash)=64
                AND source.content_hash NOT GLOB '*[^0-9a-f]*'
                AND friday_document_catalog_extraction_state(
                        source.raw_content,source.metadata_json)='current'
                AND friday_exact_text_sha256(source.raw_content)=NEW.extracted_text_sha256
                AND NEW.semantic_title IS friday_document_catalog_semantic_title(source.raw_content)
           ))
    ) THEN RAISE(ABORT,'document catalog source binding is invalid') END;
END;

CREATE TRIGGER IF NOT EXISTS document_catalog_raw_ai_seed
AFTER INSERT ON raw_objects
WHEN NEW.content_type='file' AND NEW.deleted_at IS NULL
BEGIN
    INSERT INTO document_catalog(
        raw_object_id,source_version,source_content_sha256,
        extracted_text_sha256,semantic_title,title_authority,
        enrichment_revision,enrichment_status,incomplete_reason,enriched_at
    ) VALUES(
        NEW.id,
        CASE WHEN typeof(NEW.version)='integer' AND NEW.version>=1
             THEN NEW.version ELSE NULL END,
        CASE WHEN typeof(NEW.content_hash)='text'
                   AND length(NEW.content_hash)=64
                   AND NEW.content_hash NOT GLOB '*[^0-9a-f]*'
             THEN NEW.content_hash ELSE NULL END,
        NULL,NULL,'navigation_only',{DOCUMENT_CATALOG_ENRICHMENT_REVISION},
        'incomplete',
        CASE WHEN typeof(NEW.version)<>'integer' OR NEW.version<1
             THEN 'source_unavailable'
             WHEN typeof(NEW.content_hash)='text'
                   AND length(NEW.content_hash)=64
                   AND NEW.content_hash NOT GLOB '*[^0-9a-f]*'
             THEN 'backfill_pending' ELSE 'source_unavailable' END,
        strftime('%Y-%m-%dT%H:%M:%SZ','now')
    );
END;

CREATE TRIGGER IF NOT EXISTS document_catalog_raw_au_reconcile
AFTER UPDATE OF user_id,content_type,content_hash,version,raw_content,deleted_at ON raw_objects
BEGIN
    DELETE FROM document_catalog
     WHERE raw_object_id=NEW.id
       AND (NEW.content_type<>'file' OR NEW.deleted_at IS NOT NULL);

    INSERT INTO document_catalog(
        raw_object_id,source_version,source_content_sha256,
        extracted_text_sha256,semantic_title,title_authority,
        enrichment_revision,enrichment_status,incomplete_reason,enriched_at
    )
    SELECT NEW.id,
           CASE WHEN typeof(NEW.version)='integer' AND NEW.version>=1
                THEN NEW.version ELSE NULL END,
           CASE WHEN typeof(NEW.content_hash)='text'
                      AND length(NEW.content_hash)=64
                      AND NEW.content_hash NOT GLOB '*[^0-9a-f]*'
                THEN NEW.content_hash ELSE NULL END,
           NULL,NULL,'navigation_only',{DOCUMENT_CATALOG_ENRICHMENT_REVISION},
           'incomplete',
           CASE WHEN typeof(NEW.version)<>'integer' OR NEW.version<1
                     OR typeof(NEW.content_hash)<>'text'
                     OR length(NEW.content_hash)<>64
                          OR NEW.content_hash GLOB '*[^0-9a-f]*'
                THEN 'source_unavailable'
                WHEN OLD.content_type='file' AND OLD.deleted_at IS NULL
                THEN 'source_changed'
                ELSE 'backfill_pending' END,
           strftime('%Y-%m-%dT%H:%M:%SZ','now')
     WHERE NEW.content_type='file' AND NEW.deleted_at IS NULL
       AND (OLD.user_id IS NOT NEW.user_id
            OR OLD.content_type IS NOT NEW.content_type
            OR OLD.content_hash IS NOT NEW.content_hash
            OR OLD.version IS NOT NEW.version
            OR OLD.raw_content IS NOT NEW.raw_content
            OR OLD.deleted_at IS NOT NEW.deleted_at)
    ON CONFLICT(raw_object_id) DO UPDATE SET
        source_version=excluded.source_version,
        source_content_sha256=excluded.source_content_sha256,
        extracted_text_sha256=NULL,
        semantic_title=NULL,
        title_authority='navigation_only',
        enrichment_revision={DOCUMENT_CATALOG_ENRICHMENT_REVISION},
        enrichment_status='incomplete',
        incomplete_reason=excluded.incomplete_reason,
        enriched_at=excluded.enriched_at;
END;

CREATE TRIGGER IF NOT EXISTS document_catalog_raw_au_extraction_state
AFTER UPDATE OF metadata_json ON raw_objects
WHEN NEW.content_type='file' AND NEW.deleted_at IS NULL
 AND OLD.metadata_json IS NOT NEW.metadata_json
BEGIN
    UPDATE document_catalog
       SET extracted_text_sha256=NULL,
           semantic_title=NULL,
           enrichment_revision={DOCUMENT_CATALOG_ENRICHMENT_REVISION},
           enrichment_status='incomplete',
           incomplete_reason='source_changed',
           enriched_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
     WHERE raw_object_id=NEW.id;
END;
"""


def _exact_text_sha256(value: Any) -> str:
    """Hash the exact persisted extracted-text representation."""

    if type(value) is not str:
        raise ValueError("DocumentCatalog extracted text must be TEXT")
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def deterministic_document_extraction_state(raw_content: Any, metadata_json: Any) -> str:
    """Classify extraction readiness without guessing or parsing SQL-side JSON."""

    if isinstance(metadata_json, dict):
        metadata = metadata_json
    elif type(metadata_json) is str:

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate extraction metadata key")
                value[key] = item
            return value

        try:
            parsed = json.loads(metadata_json, object_pairs_hook=unique_object)
        except (TypeError, ValueError, json.JSONDecodeError):
            return DocumentCatalogIncompleteReason.EXTRACTION_FAILED.value
        if not isinstance(parsed, dict):
            return DocumentCatalogIncompleteReason.EXTRACTION_FAILED.value
        metadata = parsed
    else:
        return DocumentCatalogIncompleteReason.EXTRACTION_FAILED.value

    if metadata.get("unsupported_format") is True:
        return DocumentCatalogIncompleteReason.UNSUPPORTED_CONTENT.value
    if any(metadata.get(field) is True for field in _EXTRACTION_LOSS_FLAGS) or any(
        metadata.get(field) is True for field in _EXTRACTION_ADVISORY_FLAGS if field != "unsupported_format"
    ):
        return DocumentCatalogIncompleteReason.EXTRACTION_INCOMPLETE.value
    if any(field in metadata for field in ("transcription", "vision")):
        return DocumentCatalogIncompleteReason.EXTRACTION_INCOMPLETE.value
    extraction_error_present = "extraction_error" in metadata
    extraction_error = metadata.get("extraction_error")
    extraction_success = metadata.get("extraction_success")
    text_success = metadata.get("text_extraction_success")
    if extraction_success is False or (extraction_error_present and extraction_error != ""):
        return DocumentCatalogIncompleteReason.EXTRACTION_FAILED.value
    if type(raw_content) is not str:
        return DocumentCatalogIncompleteReason.EXTRACTION_FAILED.value

    exact_present_flags = all(
        field not in metadata or metadata.get(field) is False
        for field in (*_EXTRACTION_LOSS_FLAGS, *_EXTRACTION_ADVISORY_FLAGS)
    )

    def nonnegative_counter_if_present(field: str) -> bool:
        if field not in metadata:
            return True
        value = metadata[field]
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    present_counters_valid = all(nonnegative_counter_if_present(field) for field in _EXTRACTION_COUNTERS)
    normalized = " ".join(raw_content.split())
    expected_digest = (
        hashlib.sha256(normalized.encode("utf-8", errors="strict")).hexdigest() if normalized else ""
    )
    optional_receipt_valid = bool(
        (
            "extraction_receipt_version" not in metadata
            or (
                type(metadata.get("extraction_receipt_version")) is int
                and metadata.get("extraction_receipt_version") == 1
            )
        )
        and (
            "extraction_chars" not in metadata
            or (
                type(metadata.get("extraction_chars")) is int
                and metadata.get("extraction_chars") == len(raw_content)
            )
        )
        and (
            "text_sha256" not in metadata
            or (type(metadata.get("text_sha256")) is str and metadata.get("text_sha256") == expected_digest)
        )
        and present_counters_valid
        and (
            "parse_pages_read" not in metadata
            or "parse_total_pages" not in metadata
            or metadata.get("parse_pages_read") == metadata.get("parse_total_pages")
        )
        and (
            "archive_files" not in metadata
            or "archive_files_read" not in metadata
            or metadata.get("archive_files") == metadata.get("archive_files_read")
        )
        and metadata.get("vision_pages_total", 0) == 0
        and metadata.get("vision_pages_read", 0) == 0
    )
    if not raw_content.strip():
        if (
            extraction_success is True
            and text_success is False
            and (not extraction_error_present or extraction_error == "")
            and exact_present_flags
            and optional_receipt_valid
        ):
            return DocumentCatalogIncompleteReason.NO_TEXT.value
        return DocumentCatalogIncompleteReason.EXTRACTION_INCOMPLETE.value
    if (
        extraction_success is not True
        or text_success is not True
        or (extraction_error_present and extraction_error != "")
        or not exact_present_flags
        or not optional_receipt_valid
    ):
        return DocumentCatalogIncompleteReason.EXTRACTION_INCOMPLETE.value
    return DocumentCatalogStatus.CURRENT.value


def deterministic_document_semantic_title(extracted_text: Any) -> str | None:
    """Derive only an explicit source heading; ordinary prose safely yields NULL."""

    if type(extracted_text) is not str:
        return None
    for raw_line in extracted_text.splitlines():
        normalized = unicodedata.normalize("NFC", " ".join(raw_line.split())).strip()
        if not normalized:
            continue
        title: str | None = None
        if normalized.startswith("#"):
            marker, separator, remainder = normalized.partition(" ")
            if separator and marker and set(marker) == {"#"} and len(marker) <= 6:
                title = remainder.strip()
        else:
            folded = normalized.casefold()
            for prefix in ("title:", "subject:", "заголовок:", "тема:"):
                if folded.startswith(prefix):
                    title = normalized[len(prefix) :].strip()
                    break
        if not title:
            return None
        if len(title) > 240 or len(title.encode("utf-8")) > 1_024:
            return None
        if any(unicodedata.category(character).startswith("C") for character in title):
            return None
        return title if any(character.isalnum() for character in title) else None
    return None


def register_document_catalog_connection_functions(conn: sqlite3.Connection) -> None:
    """Install the source-binding UDF required by persistent catalog guards."""

    conn.create_function(
        "friday_exact_text_sha256",
        1,
        _exact_text_sha256,
        deterministic=True,
    )
    conn.create_function(
        "friday_document_catalog_semantic_title",
        1,
        deterministic_document_semantic_title,
        deterministic=True,
    )
    conn.create_function(
        "friday_document_catalog_extraction_state",
        2,
        deterministic_document_extraction_state,
        deterministic=True,
    )


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
        raise sqlite3.DatabaseError("DocumentCatalog schema contains incomplete SQL")


def _normalize_schema_sql(value: str) -> str:
    return re.sub(r"\s+", "", value)


_OBJECT_NAMES = frozenset(
    {
        "document_catalog",
        "idx_document_catalog_status",
        "idx_document_catalog_reason",
        "idx_document_catalog_text",
        "document_catalog_bi_validate",
        "document_catalog_bu_validate",
        "document_catalog_raw_ai_seed",
        "document_catalog_raw_au_reconcile",
        "document_catalog_raw_au_extraction_state",
    }
)


def _schema_objects(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {
        (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
        for row in conn.execute(
            """SELECT type,name,sql FROM sqlite_master
                 WHERE sql IS NOT NULL
                   AND (name='document_catalog'
                        OR tbl_name='document_catalog'
                        OR name LIKE 'document_catalog_%'
                        OR name LIKE 'idx_document_catalog_%'
                        OR instr(lower(sql),'document_catalog')>0)
                 ORDER BY type,name"""
        )
    }


def _schema_fingerprint(objects: dict[tuple[str, str], str]) -> str:
    material = "\n".join(f"{kind}\0{name}\0{sql}" for (kind, name), sql in sorted(objects.items()))
    return hashlib.sha256(material.encode("utf-8", errors="strict")).hexdigest()


def document_catalog_source_binding_sql(
    catalog_alias: str = "catalog",
    source_alias: str = "source",
) -> str:
    """Return the one exact SQL predicate for a sidecar-to-Raw revision binding."""

    identifier = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    if identifier.fullmatch(catalog_alias) is None or identifier.fullmatch(source_alias) is None:
        raise ValueError("DocumentCatalog SQL aliases must be fixed identifiers")
    catalog = catalog_alias
    source = source_alias
    return f"""(
        (
            {catalog}.source_version={source}.version
            OR (
                {catalog}.enrichment_status='incomplete'
                AND {catalog}.incomplete_reason='source_unavailable'
                AND {catalog}.source_version IS NULL
                AND (typeof({source}.version)<>'integer' OR {source}.version<1)
            )
        )
        AND
        (
            {catalog}.source_content_sha256={source}.content_hash
            OR (
                {catalog}.enrichment_status='incomplete'
                AND {catalog}.incomplete_reason='source_unavailable'
                AND {catalog}.source_content_sha256 IS NULL
                AND (typeof({source}.content_hash)<>'text'
                     OR length({source}.content_hash)<>64
                     OR {source}.content_hash GLOB '*[^0-9a-f]*')
            )
        )
    )"""


@lru_cache(maxsize=1)
def _canonical_document_catalog_schema_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        register_document_catalog_connection_functions(conn)
        conn.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY);
            CREATE TABLE raw_objects(
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                raw_content TEXT NOT NULL DEFAULT '',
                content_type TEXT NOT NULL DEFAULT 'text',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                deleted_at TEXT
            );
            """
        )
        _execute_schema(conn, DOCUMENT_CATALOG_SCHEMA)
        return _schema_objects(conn)
    finally:
        conn.close()


def _validate_document_catalog_data(conn: sqlite3.Connection) -> None:
    source_binding = document_catalog_source_binding_sql()
    mismatch = conn.execute(
        f"""SELECT 1
             FROM document_catalog catalog
             LEFT JOIN raw_objects source ON source.id=catalog.raw_object_id
            WHERE source.id IS NULL
               OR source.content_type<>'file'
               OR source.deleted_at IS NOT NULL
               OR NOT ({source_binding})
               OR (catalog.enrichment_status='current' AND (
                    typeof(source.version)<>'integer' OR source.version<1
                    OR typeof(source.raw_content)<>'text'
                    OR friday_document_catalog_extraction_state(
                           source.raw_content,source.metadata_json)<>'current'
                    OR friday_exact_text_sha256(source.raw_content)
                         <>catalog.extracted_text_sha256
                    OR catalog.semantic_title IS NOT
                         friday_document_catalog_semantic_title(source.raw_content)
               ))
               OR (catalog.enrichment_status='incomplete' AND NOT (
                    (
                        catalog.incomplete_reason IN ('backfill_pending','source_changed')
                        AND typeof(source.version)='integer' AND source.version>=1
                        AND typeof(source.content_hash)='text'
                        AND length(source.content_hash)=64
                        AND source.content_hash NOT GLOB '*[^0-9a-f]*'
                    )
                    OR (
                        catalog.incomplete_reason='source_unavailable'
                        AND (typeof(source.version)<>'integer' OR source.version<1
                             OR typeof(source.content_hash)<>'text'
                             OR length(source.content_hash)<>64
                             OR source.content_hash GLOB '*[^0-9a-f]*')
                    )
                    OR (
                        catalog.incomplete_reason IN (
                            'extraction_failed','extraction_incomplete',
                            'no_text','unsupported_content'
                        )
                        AND typeof(source.version)='integer' AND source.version>=1
                        AND typeof(source.content_hash)='text'
                        AND length(source.content_hash)=64
                        AND source.content_hash NOT GLOB '*[^0-9a-f]*'
                        AND catalog.incomplete_reason=
                            friday_document_catalog_extraction_state(
                                source.raw_content,source.metadata_json)
                    )
               ))
            LIMIT 1"""  # nosec B608 - fragment is generated from fixed aliases
    ).fetchone()
    if mismatch is not None:
        raise sqlite3.DatabaseError("Schema 41 DocumentCatalog source binding is invalid")
    missing = conn.execute(
        """SELECT 1 FROM raw_objects source
            WHERE source.content_type='file' AND source.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM document_catalog catalog
                   WHERE catalog.raw_object_id=source.id
              )
            LIMIT 1"""
    ).fetchone()
    if missing is not None:
        raise sqlite3.DatabaseError("Schema 41 DocumentCatalog coverage is incomplete")


def validate_document_catalog_schema(
    conn: sqlite3.Connection,
    *,
    required: bool = True,
    validate_data: bool = True,
) -> None:
    """Fail closed when the exact body-free projection is missing or weakened."""

    register_document_catalog_connection_functions(conn)
    installed = _schema_objects(conn)
    if not installed:
        if required:
            raise sqlite3.DatabaseError("Schema 41 DocumentCatalog is missing")
        return
    if installed != _canonical_document_catalog_schema_objects():
        raise sqlite3.DatabaseError("Schema 41 DocumentCatalog DDL is incomplete or altered")

    columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(document_catalog)")
    }
    if columns != {
        "raw_object_id": ("TEXT", 1, 1),
        "source_version": ("INTEGER", 0, 0),
        "source_content_sha256": ("TEXT", 0, 0),
        "extracted_text_sha256": ("TEXT", 0, 0),
        "semantic_title": ("TEXT", 0, 0),
        "title_authority": ("TEXT", 1, 0),
        "enrichment_revision": ("INTEGER", 1, 0),
        "enrichment_status": ("TEXT", 1, 0),
        "incomplete_reason": ("TEXT", 0, 0),
        "enriched_at": ("TEXT", 1, 0),
    }:
        raise sqlite3.DatabaseError("Schema 41 DocumentCatalog shape is invalid")

    foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(document_catalog)")
    }
    if foreign_keys != {("raw_object_id", "raw_objects", "id", "NO ACTION", "CASCADE")}:
        raise sqlite3.DatabaseError("Schema 41 DocumentCatalog ownership is invalid")

    expected_indexes = {
        "idx_document_catalog_status": ("enrichment_status", "raw_object_id"),
        "idx_document_catalog_reason": ("incomplete_reason", "raw_object_id"),
        "idx_document_catalog_text": ("extracted_text_sha256", "raw_object_id"),
    }
    observed_indexes = {
        name: tuple(str(column[2]) for column in conn.execute(f'PRAGMA index_info("{name}")'))
        for name in expected_indexes
    }
    if observed_indexes != expected_indexes:
        raise sqlite3.DatabaseError("Schema 41 DocumentCatalog indexes are invalid")
    if validate_data:
        _validate_document_catalog_data(conn)


def document_catalog_schema_fingerprint(conn: sqlite3.Connection) -> str:
    """Return a fingerprint only after exact table/index/trigger authentication."""

    validate_document_catalog_schema(conn, validate_data=False)
    installed = _schema_objects(conn)
    canonical = _canonical_document_catalog_schema_objects()
    if installed != canonical:  # defensive if validation changes independently
        raise sqlite3.DatabaseError("Schema 41 DocumentCatalog DDL is incomplete or altered")
    return _schema_fingerprint(installed)


def install_document_catalog_schema(conn: sqlite3.Connection) -> None:
    """Authenticate, install and seed schema 41 inside the core migration."""

    if not conn.in_transaction:
        raise RuntimeError("DocumentCatalog schema installation requires an existing transaction")
    installed = _schema_objects(conn)
    if installed and installed != _canonical_document_catalog_schema_objects():
        raise sqlite3.DatabaseError("Schema 41 DocumentCatalog DDL is incomplete or altered")
    if not installed:
        _execute_schema(conn, DOCUMENT_CATALOG_SCHEMA)

    # Existing released rows enter the projection explicitly incomplete.  No body
    # is copied and no title is guessed during migration; the bounded deterministic
    # rebuild API can enrich them after startup.
    conn.execute(
        f"""INSERT INTO document_catalog(
                raw_object_id,source_version,source_content_sha256,
                extracted_text_sha256,semantic_title,title_authority,
                enrichment_revision,enrichment_status,incomplete_reason,enriched_at
            )
            SELECT source.id,
                   CASE WHEN typeof(source.version)='integer' AND source.version>=1
                        THEN source.version ELSE NULL END,
                   CASE WHEN typeof(source.content_hash)='text'
                              AND length(source.content_hash)=64
                              AND source.content_hash NOT GLOB '*[^0-9a-f]*'
                        THEN source.content_hash ELSE NULL END,
                   NULL,NULL,'navigation_only',{DOCUMENT_CATALOG_ENRICHMENT_REVISION},
                   'incomplete',
                   CASE WHEN typeof(source.version)<>'integer' OR source.version<1
                                  OR typeof(source.content_hash)<>'text'
                                  OR length(source.content_hash)<>64
                                  OR source.content_hash GLOB '*[^0-9a-f]*'
                        THEN 'source_unavailable'
                        ELSE 'backfill_pending' END,
                   strftime('%Y-%m-%dT%H:%M:%SZ','now')
              FROM raw_objects source
             WHERE source.content_type='file' AND source.deleted_at IS NULL
            ON CONFLICT(raw_object_id) DO NOTHING"""
    )
    validate_document_catalog_schema(conn)


__all__ = [
    "DOCUMENT_CATALOG_ENRICHMENT_REVISION",
    "DOCUMENT_CATALOG_INCOMPLETE_REASONS",
    "DOCUMENT_CATALOG_SCHEMA",
    "DOCUMENT_CATALOG_SCHEMA_VERSION",
    "DocumentCatalogIncompleteReason",
    "DocumentCatalogStatus",
    "deterministic_document_extraction_state",
    "deterministic_document_semantic_title",
    "document_catalog_schema_fingerprint",
    "document_catalog_source_binding_sql",
    "install_document_catalog_schema",
    "register_document_catalog_connection_functions",
    "validate_document_catalog_schema",
]
