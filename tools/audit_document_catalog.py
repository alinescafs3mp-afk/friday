#!/usr/bin/env python
"""Secret-free, read-only audit of document discoverability in an offline DB copy.

The auditor recognizes Friday's exact, body-free ``document_catalog`` and
schema-48 document-passage sidecars.  A missing or look-alike table remains
``not_available``/``unsupported``; it is never guessed into coverage.  Current,
explicitly incomplete, missing and stale source-bound rows are counted
separately, while later embedding and typed-date projections continue to report
honest gaps.

Only an explicitly named, private, quiescent SQLite copy is accepted.  There is
no settings-derived/default database path and no mutation/fix mode.  Output is
limited to counts, fixed status/reason labels, versions, and SHA-256 digests; no
IDs, filenames, paths, document text, excerpts, or source content hashes leave
the process.

``registered`` here means a durable authorized Raw ``content_type='file'`` row
with non-empty extracted text.  Physical byte/path validity remains the separate
``audit_file_registry`` contract.  Policy exclusions form a mutually-exclusive
first-reason partition (deleted, ignored, private, audio, uploader); empty
extraction is reported separately so OCR/parser failures cannot hide as policy.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sqlite3
import stat
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from friday.storage import SCHEMA_VERSION  # noqa: E402
from friday.storage._archive_search_documents import _canonical_utc_sql  # noqa: E402
from friday.storage._privacy import (  # noqa: E402
    _exact_uploader_raw_dependency,
    _not_audio_document,
    _not_private_inbox_dependency,
    _not_private_raw_dependency,
)

REPORT_SCHEMA = "friday.document-catalog-audit.v3"
DEFAULT_MAX_ROWS = 100_000
HARD_MAX_ROWS = 1_000_000
HEX64 = re.compile(r"^[0-9a-f]{64}$")

COUNT_KEYS = (
    "tenant_file_rows_examined",
    "registered_authorized_live_text_bearing_files",
    "pending_registered_files",
    "catalogued_files",
    "lexically_searchable_files",
    "files_with_semantic_title",
    "files_with_passages",
    "files_with_current_embeddings",
    "files_with_typed_dates",
    "pending_files_with_semantic_index",
    "files_with_stale_enrichment_revision",
    "files_with_incomplete_catalog",
    "files_with_index_incomplete_reason",
    "files_excluded_by_policy",
    "files_without_extracted_text",
)
EXCLUSION_KEYS = (
    "deleted_lifecycle",
    "ignored_lifecycle",
    "private_authority",
    "audio_document",
    "uploader_authority",
)
INCOMPLETE_KEYS = (
    "catalog_projection_not_available",
    "catalog_row_missing",
    "catalog_row_stale",
    "catalog_row_incomplete",
    "lexical_projection_not_available",
    "lexical_row_missing",
    "semantic_title_projection_not_available",
    "semantic_title_missing",
    "passage_projection_not_available",
    "passage_row_missing",
    "passage_row_stale",
    "passage_row_incomplete",
    "embedding_projection_not_available",
    "typed_date_projection_not_available",
    "pending_semantic_projection_not_available",
    "enrichment_revision_projection_not_available",
    "extracted_text_unavailable",
)
PROJECTION_KEYS = (
    "raw_registry",
    "lexical_source_index",
    "document_catalog",
    "semantic_title",
    "document_passages",
    "document_embeddings",
    "typed_dates",
    "pending_semantic_index",
    "enrichment_revision",
)
FUTURE_TABLES = {
    "document_catalog": ("document_catalog",),
    "semantic_title": ("document_catalog",),
    "document_embeddings": ("document_embeddings",),
    "typed_dates": ("document_temporal_facts",),
    "pending_semantic_index": ("document_catalog", "document_embeddings"),
    "enrichment_revision": ("document_catalog",),
}

CURRENT_ENRICHMENT_REVISION = 1
CATALOG_INCOMPLETE_REASONS = (
    "backfill_pending",
    "extraction_failed",
    "extraction_incomplete",
    "no_text",
    "unsupported_content",
    "source_unavailable",
    "source_changed",
)
PASSAGE_INCOMPLETE_REASONS = CATALOG_INCOMPLETE_REASONS
PASSAGE_PROJECTION_TABLES = (
    "document_passage_projections",
    "document_passages",
)


class ContractError(RuntimeError):
    """The operator, input snapshot, or output contract is not safe to use."""


class _OfflineConnection(sqlite3.Connection):
    """Connection type issued only after the offline-copy checks pass."""

    _friday_offline_copy_verified: bool


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _exact_text_present(value: Any) -> int:
    """Mirror the Python extraction contract without SQLite NUL truncation."""

    return int(type(value) is str and bool(value.strip()))


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _identity(value: str, *, label: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 200 or any(ord(char) < 32 for char in text):
        raise ContractError(f"invalid {label}")
    return text


def _bounded_rows(value: int) -> int:
    try:
        bounded = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError("max rows must be an integer") from exc
    if bounded < 1 or bounded > HARD_MAX_ROWS:
        raise ContractError(f"max rows must be between 1 and {HARD_MAX_ROWS}")
    return bounded


def open_offline_database(database: Path) -> sqlite3.Connection:
    """Open a caller-attested offline copy, rejecting common live/alias forms."""

    try:
        path = database.resolve(strict=True)
        info = path.stat()
    except OSError as exc:
        raise ContractError("database copy is unavailable") from exc
    if database.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise ContractError("database copy must be a regular non-symlink file")
    if info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise ContractError("database copy must be privately owned and not hard-linked")
    if stat.S_IMODE(info.st_mode) not in {0o400, 0o600}:
        raise ContractError("database copy permissions must be private (0600 or 0400)")
    if any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ContractError("database copy must be quiescent and have no SQLite sidecars")
    try:
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise ContractError("database copy is not SQLite")
        conn = sqlite3.connect(
            f"{path.as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=5.0,
            isolation_level=None,
            factory=_OfflineConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            conn.close()
            raise ContractError("SQLite query-only mode was not established")
        conn._friday_offline_copy_verified = True
        return conn
    except ContractError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ContractError("database copy could not be opened read-only") from exc


def _table_sql(conn: sqlite3.Connection, table: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table,),
    ).fetchone()
    return str(row[0] or "") if row is not None else None


def _schema_hash(conn: sqlite3.Connection, names: tuple[str, ...]) -> str | None:
    definitions: list[tuple[str, str]] = []
    for name in sorted(set(names)):
        sql = _table_sql(conn, name)
        if sql is not None:
            definitions.append((name, sql))
    return _sha256(_canonical_json(definitions)) if definitions else None


_RAW_FTS_SCHEMA = """
CREATE VIRTUAL TABLE raw_fts USING fts5(
    raw_content,
    content=raw_objects,
    content_rowid=rowid,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER raw_objects_ai AFTER INSERT ON raw_objects BEGIN
    INSERT INTO raw_fts(rowid, raw_content) VALUES (new.rowid, new.raw_content);
END;
CREATE TRIGGER raw_objects_ad AFTER DELETE ON raw_objects BEGIN
    INSERT INTO raw_fts(raw_fts, rowid, raw_content) VALUES ('delete', old.rowid, old.raw_content);
END;
CREATE TRIGGER raw_objects_au AFTER UPDATE ON raw_objects BEGIN
    INSERT INTO raw_fts(raw_fts, rowid, raw_content) VALUES ('delete', old.rowid, old.raw_content);
    INSERT INTO raw_fts(rowid, raw_content) VALUES (new.rowid, new.raw_content);
END;
"""
_RAW_FTS_OBJECT_NAMES = (
    "raw_fts",
    "raw_objects_ai",
    "raw_objects_ad",
    "raw_objects_au",
)
_RAW_FTS_SHADOW_TABLES = frozenset({"raw_fts_data", "raw_fts_idx", "raw_fts_docsize", "raw_fts_config"})


def _normalized_schema_objects(
    conn: sqlite3.Connection,
    names: tuple[str, ...],
) -> dict[tuple[str, str], str]:
    placeholders = ",".join("?" for _ in names)
    return {
        (str(row[0]), str(row[1])): re.sub(r"\s+", "", str(row[2]))
        for row in conn.execute(
            f"""SELECT type,name,sql FROM sqlite_master
                 WHERE sql IS NOT NULL AND name IN ({placeholders})
                 ORDER BY type,name""",  # nosec B608 - fixed placeholders only
            names,
        )
    }


@lru_cache(maxsize=1)
def _canonical_raw_fts_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE raw_objects(id TEXT PRIMARY KEY, raw_content TEXT NOT NULL)")
        conn.executescript(_RAW_FTS_SCHEMA)
        return _normalized_schema_objects(conn, _RAW_FTS_OBJECT_NAMES)
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def _raw_fts_schema_fingerprint(conn: sqlite3.Connection) -> str | None:
    canonical = _canonical_raw_fts_objects()
    if not canonical or _normalized_schema_objects(conn, _RAW_FTS_OBJECT_NAMES) != canonical:
        return None
    shadow_tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'raw_fts_*'")
    }
    if shadow_tables != _RAW_FTS_SHADOW_TABLES:
        return None
    marker = conn.execute("SELECT value FROM schema_meta WHERE key='fts_build'").fetchone()
    if marker is None or str(marker[0]) != str(SCHEMA_VERSION):
        return None
    return _sha256(_canonical_json([sorted(canonical.items()), str(marker[0])]))


def _document_catalog_schema_fingerprint(conn: sqlite3.Connection) -> str | None:
    """Delegate exact recognition to the shipped schema-41 validator."""

    try:
        schema_module = importlib.import_module("friday.document_catalog.schema")
    except ImportError:
        return None
    try:
        schema_module.register_document_catalog_connection_functions(conn)
        digest = schema_module.document_catalog_schema_fingerprint(conn)
    except (RuntimeError, ValueError, sqlite3.Error):
        return None
    return str(digest) if HEX64.fullmatch(str(digest)) else None


def _document_passage_schema_fingerprint(conn: sqlite3.Connection) -> str | None:
    """Delegate exact recognition to the shipped schema-48 validator."""

    try:
        schema_module = importlib.import_module("friday.document_catalog.passage_schema")
        schema_module.register_document_passage_connection_functions(conn)
        digest = schema_module.document_passage_schema_fingerprint(conn)
    except (AttributeError, ImportError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return None
    return str(digest) if HEX64.fullmatch(str(digest)) else None


def _required_schema(conn: sqlite3.Connection) -> int:
    required = {
        "schema_meta",
        "users",
        "raw_objects",
        "inbox",
        "knowledge_objects",
        "private_entity_material_cache_state",
        "private_entity_material_derivative_state",
        "private_entity_material_derivative_cache",
    }
    present = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if not required.issubset(present):
        raise ContractError("database copy lacks required Friday authority tables")
    row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if row is None:
        raise ContractError("database copy has no schema version")
    try:
        version = int(row[0])
    except (TypeError, ValueError) as exc:
        raise ContractError("database copy has an invalid schema version") from exc
    if version != int(SCHEMA_VERSION):
        raise ContractError("database copy must use the current Friday schema")
    return version


def _validate_inbox_ordering_scope(
    conn: sqlite3.Connection,
    *,
    tenant: str,
    uploader: str | None,
) -> None:
    """Reject an archive scope whose latest Inbox verdict cannot be ordered."""

    uploader_expression = _exact_uploader_raw_dependency("r") if uploader else "1"
    invalid_timestamp = f"""SELECT 1
         FROM inbox i
         JOIN raw_objects r ON r.id=i.raw_object_id AND r.user_id=i.user_id
        WHERE r.user_id=? AND r.content_type='file' AND r.deleted_at IS NULL
          AND ({_not_private_raw_dependency("r")})
          AND ({_not_audio_document("r")})
          AND ({uploader_expression})
          AND (
               NOT {_canonical_utc_sql("i.created_at")}
               OR (i.reviewed_at IS NOT NULL AND NOT {_canonical_utc_sql("i.reviewed_at")})
               OR i.status NOT IN ('pending','classified','archived','ignored')
          )
        LIMIT 1"""
    parameters: tuple[Any, ...] = (tenant, uploader) if uploader else (tenant,)
    try:
        invalid = conn.execute(invalid_timestamp, parameters).fetchone()
    except sqlite3.Error as exc:
        raise ContractError("current Inbox authority shape is unavailable") from exc
    if invalid is not None:
        raise ContractError("current Inbox authority ordering is invalid")


def _projection_report(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    raw_hash = _schema_hash(conn, ("raw_objects", "inbox"))
    lexical_present = _table_sql(conn, "raw_fts") is not None
    lexical_fingerprint = _raw_fts_schema_fingerprint(conn) if lexical_present else None
    projections: dict[str, dict[str, Any]] = {
        "raw_registry": {"status": "available", "revision_sha256": raw_hash},
        "lexical_source_index": {
            "status": (
                "available"
                if lexical_fingerprint is not None
                else "unsupported"
                if lexical_present
                else "not_available"
            ),
            "revision_sha256": (
                lexical_fingerprint
                if lexical_fingerprint is not None
                else _schema_hash(conn, ("raw_fts", "raw_fts_docsize"))
                if lexical_present
                else None
            ),
        },
    }
    catalog_fingerprint = _document_catalog_schema_fingerprint(conn)
    catalog_exact = catalog_fingerprint is not None
    for name, tables in FUTURE_TABLES.items():
        present = any(_table_sql(conn, table) is not None for table in tables)
        implemented = catalog_exact and name in {
            "document_catalog",
            "semantic_title",
            "enrichment_revision",
        }
        catalog_only_pending_gap = (
            name == "pending_semantic_index"
            and catalog_exact
            and _table_sql(conn, "document_embeddings") is None
        )
        status = (
            "available"
            if implemented
            else "not_available"
            if catalog_only_pending_gap or not present
            else "unsupported"
        )
        projections[name] = {
            "status": status,
            "revision_sha256": (
                catalog_fingerprint
                if implemented
                else _schema_hash(conn, tables)
                if status != "not_available"
                else None
            ),
        }
    passage_present = any(_table_sql(conn, table) is not None for table in PASSAGE_PROJECTION_TABLES)
    passage_fingerprint = _document_passage_schema_fingerprint(conn) if passage_present else None
    projections["document_passages"] = {
        "status": (
            "available"
            if passage_fingerprint is not None
            else "unsupported"
            if passage_present
            else "not_available"
        ),
        "revision_sha256": (
            passage_fingerprint
            if passage_fingerprint is not None
            else _schema_hash(conn, PASSAGE_PROJECTION_TABLES)
            if passage_present
            else None
        ),
    }
    return {key: projections[key] for key in PROJECTION_KEYS}


def audit_document_catalog(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    uploader: str | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> dict[str, Any]:
    """Audit one exact authorization scope without reading bodies into Python."""

    if not isinstance(conn, _OfflineConnection) or not getattr(conn, "_friday_offline_copy_verified", False):
        raise ContractError("audit requires a connection opened from an explicit offline copy")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise ContractError("audit connection is not query-only")
    tenant = _identity(tenant_id, label="tenant id")
    exact_uploader = _identity(uploader, label="uploader id") if uploader is not None else None
    cap = _bounded_rows(max_rows)
    schema_version = _required_schema(conn)
    conn.create_function(
        "friday_audit_exact_text_present",
        1,
        _exact_text_present,
        deterministic=True,
    )

    tenant_row = conn.execute("SELECT status FROM users WHERE id=?", (tenant,)).fetchone()
    if tenant_row is None or str(tenant_row[0]) != "active":
        raise ContractError("tenant must identify one active account")
    if exact_uploader is not None:
        uploader_row = conn.execute("SELECT status FROM users WHERE id=?", (exact_uploader,)).fetchone()
        if uploader_row is None or str(uploader_row[0]) != "active":
            raise ContractError("uploader must identify one active account")

    row_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM raw_objects WHERE user_id=? AND content_type='file'",
            (tenant,),
        ).fetchone()[0]
    )
    if row_count > cap:
        raise ContractError("tenant file scope exceeds the explicit row bound")
    _validate_inbox_ordering_scope(
        conn,
        tenant=tenant,
        uploader=exact_uploader,
    )

    projections = _projection_report(conn)
    lexical_available = projections["lexical_source_index"]["status"] == "available"
    lexical_expression = (
        "EXISTS (SELECT 1 FROM raw_fts_docsize lexical_docsize WHERE lexical_docsize.id=r.rowid)"
        if lexical_available
        else "0"
    )
    catalog_available = projections["document_catalog"]["status"] == "available"
    catalog_reasons_sql = ",".join(f"'{reason}'" for reason in CATALOG_INCOMPLETE_REASONS)
    catalog_projection = (
        f"""
               c.raw_object_id IS NOT NULL AS has_catalog_row,
               c.source_content_sha256 AS catalog_source_material,
               c.extracted_text_sha256 AS catalog_text_material,
               c.enrichment_revision AS catalog_revision_material,
               c.enrichment_status AS catalog_status_material,
               c.incomplete_reason AS catalog_reason_material,
               c.enriched_at AS catalog_time_material,
               CASE WHEN typeof(c.semantic_title)='text'
                    THEN friday_exact_text_sha256(c.semantic_title) ELSE '' END
                   AS catalog_title_material,
               (((typeof(r.version)='integer' AND r.version>=1
                  AND c.source_version=r.version)
                 OR
                 ((typeof(r.version)<>'integer' OR r.version<1)
                  AND c.enrichment_status='incomplete'
                  AND c.incomplete_reason='source_unavailable'
                  AND c.source_version IS NULL))
                AND
                ((typeof(r.content_hash)='text'
                  AND length(r.content_hash)=64
                  AND r.content_hash NOT GLOB '*[^0-9a-f]*'
                  AND c.source_content_sha256=r.content_hash)
                 OR
                 ((typeof(r.content_hash)<>'text'
                   OR length(r.content_hash)<>64
                   OR r.content_hash GLOB '*[^0-9a-f]*')
                  AND c.enrichment_status='incomplete'
                  AND c.incomplete_reason='source_unavailable'
                  AND c.source_content_sha256 IS NULL))) AS catalog_source_current,
               (typeof(c.enrichment_revision)='integer'
                AND c.enrichment_revision={CURRENT_ENRICHMENT_REVISION})
                   AS catalog_revision_current,
               (c.enrichment_status='current'
                AND c.incomplete_reason IS NULL
                AND typeof(c.extracted_text_sha256)='text'
                AND length(c.extracted_text_sha256)=64
                AND c.extracted_text_sha256 NOT GLOB '*[^0-9a-f]*'
                AND CASE WHEN typeof(r.raw_content)='text'
                         THEN friday_exact_text_sha256(r.raw_content)=c.extracted_text_sha256
                         ELSE 0 END
                AND friday_document_catalog_extraction_state(
                        r.raw_content,r.metadata_json)='current'
                AND c.semantic_title IS
                    friday_document_catalog_semantic_title(r.raw_content)
                AND c.title_authority='navigation_only'
                AND (c.semantic_title IS NULL OR (
                    typeof(c.semantic_title)='text'
                    AND c.semantic_title=trim(c.semantic_title)
                    AND length(c.semantic_title) BETWEEN 1 AND 240
                    AND length(CAST(c.semantic_title AS BLOB))<=1024
                    AND instr(c.semantic_title,char(0))=0
                    AND instr(c.semantic_title,char(10))=0
                    AND instr(c.semantic_title,char(13))=0
                ))
                AND typeof(c.enriched_at)='text'
                AND length(c.enriched_at)=20
                AND strftime('%Y-%m-%dT%H:%M:%SZ',c.enriched_at)=c.enriched_at)
                   AS catalog_current,
               (c.enrichment_status='incomplete'
                AND c.incomplete_reason IN ({catalog_reasons_sql})
                AND c.extracted_text_sha256 IS NULL
                AND c.semantic_title IS NULL
                AND c.title_authority='navigation_only'
                AND typeof(c.enriched_at)='text'
                AND length(c.enriched_at)=20
                AND strftime('%Y-%m-%dT%H:%M:%SZ',c.enriched_at)=c.enriched_at)
                   AS catalog_incomplete,
               (typeof(c.semantic_title)='text'
                AND c.semantic_title=trim(c.semantic_title)
                AND length(c.semantic_title) BETWEEN 1 AND 240
                AND length(CAST(c.semantic_title AS BLOB))<=1024
                AND instr(c.semantic_title,char(0))=0
                AND instr(c.semantic_title,char(10))=0
                AND instr(c.semantic_title,char(13))=0) AS has_semantic_title
        """
        if catalog_available
        else """
               0 AS has_catalog_row,
               NULL AS catalog_source_material,
               NULL AS catalog_text_material,
               NULL AS catalog_revision_material,
               NULL AS catalog_status_material,
               NULL AS catalog_reason_material,
               NULL AS catalog_time_material,
               NULL AS catalog_title_material,
               0 AS catalog_source_current,
               0 AS catalog_revision_current,
               0 AS catalog_current,
               0 AS catalog_incomplete,
               0 AS has_semantic_title
        """
    )
    catalog_join = "LEFT JOIN document_catalog c ON c.raw_object_id=r.id" if catalog_available else ""
    passage_available = projections["document_passages"]["status"] == "available"
    passage_projection = (
        """
               p.raw_object_id IS NOT NULL AS has_passage_projection,
               p.source_content_sha256 AS passage_source_material,
               p.extracted_text_sha256 AS passage_text_material,
               p.source_char_count AS passage_char_count_material,
               p.passage_set_sha256 AS passage_set_material,
               p.passage_index_revision AS passage_revision_material,
               p.projection_status AS passage_status_material,
               p.incomplete_reason AS passage_reason_material,
               p.passage_count AS passage_count_material,
               p.projected_at AS passage_time_material,
               (p.raw_object_id IS NOT NULL
                AND friday_document_passage_projection_valid(
                    r.id,r.version,r.content_hash,r.raw_content,r.metadata_json,
                    p.source_version,p.source_content_sha256,
                    p.extracted_text_sha256,p.source_char_count,
                    p.passage_set_sha256,p.passage_index_revision,
                    p.projection_status,p.incomplete_reason,p.passage_count)=1
                AND typeof(p.projected_at)='text'
                AND length(p.projected_at)=20
                AND strftime('%Y-%m-%dT%H:%M:%SZ',p.projected_at)=p.projected_at)
                   AS passage_parent_valid,
               (SELECT COUNT(*) FROM document_passages child
                 WHERE child.raw_object_id=p.raw_object_id) AS passage_child_count,
               COALESCE((
                   SELECT friday_document_passage_set_sha256(
                              child.chunk_index,child.start_char,
                              child.end_char,child.content_sha256)
                     FROM document_passages child
                    WHERE child.raw_object_id=p.raw_object_id
               ),'') AS passage_child_set_material
        """
        if passage_available
        else """
               0 AS has_passage_projection,
               NULL AS passage_source_material,
               NULL AS passage_text_material,
               NULL AS passage_char_count_material,
               NULL AS passage_set_material,
               NULL AS passage_revision_material,
               NULL AS passage_status_material,
               NULL AS passage_reason_material,
               NULL AS passage_count_material,
               NULL AS passage_time_material,
               0 AS passage_parent_valid,
               0 AS passage_child_count,
               NULL AS passage_child_set_material
        """
    )
    passage_join = (
        "LEFT JOIN document_passage_projections p ON p.raw_object_id=r.id" if passage_available else ""
    )
    uploader_expression = _exact_uploader_raw_dependency("r") if exact_uploader else "1"
    query = f"""WITH current_inbox AS MATERIALIZED (
        SELECT inbox_id,raw_object_id,status FROM (
            SELECT i.id AS inbox_id,i.raw_object_id,i.status,
                   ROW_NUMBER() OVER (
                       PARTITION BY i.raw_object_id
                       ORDER BY i.created_at DESC,
                                COALESCE(i.reviewed_at,'') DESC,
                                i.id DESC
                   ) AS choice
              FROM inbox i
             WHERE i.user_id=?
               AND ({_not_private_inbox_dependency("i")})
        ) WHERE choice=1
    )
        SELECT r.id AS opaque_id,
               r.version AS source_version,
               r.content_hash AS source_content_material,
               r.deleted_at IS NOT NULL AS is_deleted,
               (ci.status='ignored') AS is_ignored,
               ({_not_private_raw_dependency("r")}) AS is_public,
               ({_not_audio_document("r")}) AS is_document,
               ({uploader_expression}) AS uploader_allowed,
               friday_audit_exact_text_present(r.raw_content)=1 AS has_text,
               (ci.status='pending') AS is_pending,
               ({lexical_expression}) AS has_lexical_row,
               {catalog_projection},
               {passage_projection}
          FROM raw_objects r
          LEFT JOIN current_inbox ci ON ci.raw_object_id=r.id
          {catalog_join}
          {passage_join}
         WHERE r.user_id=? AND r.content_type='file'
         ORDER BY r.rowid
    """  # nosec B608 - every interpolated fragment is a code-owned SQL predicate
    params: tuple[Any, ...] = (tenant, exact_uploader, tenant) if exact_uploader else (tenant, tenant)

    excluded: Counter[str] = Counter()
    registered = 0
    pending = 0
    lexical = 0
    without_text = 0
    catalogued = 0
    catalog_missing = 0
    catalog_stale = 0
    catalog_incomplete = 0
    semantic_titles = 0
    passage_current = 0
    passage_explicit_incomplete = 0
    passage_missing = 0
    passage_stale = 0
    passage_child_rows = 0
    passage_text_current = 0
    passage_incomplete_reasons: Counter[str] = Counter()
    fingerprint = hashlib.sha256()
    # A hard VM-step fuse complements the row cap for malformed/adversarial copies.
    conn.set_progress_handler(lambda: 1, max(250_000, (row_count + 1) * 25_000))
    try:
        rows = conn.execute(query, params)
        seen = 0
        for row in rows:
            seen += 1
            raw_id = str(row["opaque_id"] or "")
            state: str
            if bool(row["is_deleted"]):
                state = "deleted_lifecycle"
                excluded[state] += 1
            elif bool(row["is_ignored"]):
                state = "ignored_lifecycle"
                excluded[state] += 1
            elif not bool(row["is_public"]):
                state = "private_authority"
                excluded[state] += 1
            elif not bool(row["is_document"]):
                state = "audio_document"
                excluded[state] += 1
            elif not bool(row["uploader_allowed"]):
                state = "uploader_authority"
                excluded[state] += 1
            elif not bool(row["has_text"]):
                state = "extracted_text_unavailable"
                without_text += 1
            else:
                state = "registered"
                registered += 1
                pending += int(bool(row["is_pending"]))
                lexical += int(bool(row["has_lexical_row"]))
                if not catalog_available:
                    catalog_state = "not_available"
                elif not bool(row["has_catalog_row"]):
                    catalog_state = "missing"
                    catalog_missing += 1
                elif not (bool(row["catalog_source_current"]) and bool(row["catalog_revision_current"])):
                    catalog_state = "stale"
                    catalog_stale += 1
                elif bool(row["catalog_current"]):
                    catalog_state = "current"
                    catalogued += 1
                    semantic_titles += int(bool(row["has_semantic_title"]))
                elif bool(row["catalog_incomplete"]):
                    catalog_state = "incomplete"
                    catalogued += 1
                    catalog_incomplete += 1
                else:
                    catalog_state = "stale"
                    catalog_stale += 1
            row_passage_eligible = state in {"registered", "extracted_text_unavailable"}
            passage_state = "excluded"
            if row_passage_eligible:
                if not passage_available:
                    passage_state = "not_available"
                elif not bool(row["has_passage_projection"]):
                    passage_state = "missing"
                    passage_missing += 1
                elif not bool(row["passage_parent_valid"]):
                    passage_state = "stale"
                    passage_stale += 1
                elif str(row["passage_status_material"] or "") == "current":
                    child_count = int(row["passage_child_count"] or 0)
                    if child_count == int(row["passage_count_material"] or 0) and str(
                        row["passage_child_set_material"] or ""
                    ) == str(row["passage_set_material"] or ""):
                        passage_state = "current"
                        passage_current += 1
                        passage_child_rows += child_count
                        passage_text_current += int(state == "registered")
                    else:
                        passage_state = "stale"
                        passage_stale += 1
                elif (
                    str(row["passage_status_material"] or "") == "incomplete"
                    and row["passage_text_material"] is None
                    and row["passage_char_count_material"] is None
                    and row["passage_set_material"] is None
                    and type(row["passage_count_material"]) is int
                    and int(row["passage_count_material"]) == 0
                    and int(row["passage_child_count"] or 0) == 0
                    and str(row["passage_reason_material"] or "") in PASSAGE_INCOMPLETE_REASONS
                ):
                    passage_state = "incomplete"
                    passage_explicit_incomplete += 1
                    passage_incomplete_reasons[str(row["passage_reason_material"])] += 1
                else:
                    passage_state = "stale"
                    passage_stale += 1
            # IDs never leave the hash state. Include the classification so policy
            # or lifecycle mutations change the snapshot fingerprint.
            fingerprint.update(
                _canonical_json(
                    [
                        raw_id,
                        int(row["source_version"] or 0),
                        state,
                        int(bool(row["has_lexical_row"])),
                        int(bool(row["is_pending"])),
                        catalog_state if state == "registered" else "excluded",
                        str(row["source_content_material"] or ""),
                        str(row["catalog_source_material"] or ""),
                        str(row["catalog_text_material"] or ""),
                        str(row["catalog_revision_material"] or ""),
                        str(row["catalog_status_material"] or ""),
                        str(row["catalog_reason_material"] or ""),
                        str(row["catalog_time_material"] or ""),
                        str(row["catalog_title_material"] or ""),
                        passage_state,
                        str(row["passage_source_material"] or "") if row_passage_eligible else "",
                        str(row["passage_text_material"] or "") if row_passage_eligible else "",
                        str(row["passage_char_count_material"] or "") if row_passage_eligible else "",
                        str(row["passage_set_material"] or "") if row_passage_eligible else "",
                        str(row["passage_revision_material"] or "") if row_passage_eligible else "",
                        str(row["passage_status_material"] or "") if row_passage_eligible else "",
                        str(row["passage_reason_material"] or "") if row_passage_eligible else "",
                        str(row["passage_count_material"] or "") if row_passage_eligible else "",
                        str(row["passage_time_material"] or "") if row_passage_eligible else "",
                        str(row["passage_child_count"] or "") if row_passage_eligible else "",
                        str(row["passage_child_set_material"] or "") if row_passage_eligible else "",
                    ]
                )
            )
        if seen != row_count:
            raise ContractError("bounded catalog scan did not account for every scoped row")
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc).casefold():
            raise ContractError("catalog scan exceeded the SQLite work bound") from exc
        raise ContractError("catalog scan failed") from exc
    finally:
        conn.set_progress_handler(None, 0)

    passage_eligible = registered + without_text
    passage_coverage_complete = bool(
        passage_available
        and passage_current + passage_explicit_incomplete == passage_eligible
        and passage_missing == 0
        and passage_stale == 0
    )
    passage_index_complete = bool(
        passage_coverage_complete and passage_text_current == registered and passage_current == registered
    )
    future_missing = {
        "catalog_projection_not_available": 0 if catalog_available else registered,
        "semantic_title_projection_not_available": 0 if catalog_available else registered,
        "passage_projection_not_available": 0 if passage_available else passage_eligible,
        "embedding_projection_not_available": registered,
        "typed_date_projection_not_available": registered,
        "pending_semantic_projection_not_available": pending,
        "enrichment_revision_projection_not_available": 0 if catalog_available else registered,
    }
    incomplete = {
        **future_missing,
        "catalog_row_missing": catalog_missing,
        "catalog_row_stale": catalog_stale,
        "catalog_row_incomplete": catalog_incomplete,
        "lexical_projection_not_available": 0 if lexical_available else registered,
        "lexical_row_missing": registered - lexical if lexical_available else 0,
        "semantic_title_missing": registered - semantic_titles if catalog_available else 0,
        "passage_row_missing": passage_missing,
        "passage_row_stale": passage_stale,
        "passage_row_incomplete": passage_explicit_incomplete,
        "extracted_text_unavailable": without_text,
    }
    incomplete = {key: int(incomplete[key]) for key in INCOMPLETE_KEYS}
    policy_total = sum(int(excluded.get(key, 0)) for key in EXCLUSION_KEYS)
    counts = {
        "tenant_file_rows_examined": row_count,
        "registered_authorized_live_text_bearing_files": registered,
        "pending_registered_files": pending,
        "catalogued_files": catalogued,
        "lexically_searchable_files": lexical,
        "files_with_semantic_title": semantic_titles,
        "files_with_passages": passage_current,
        "files_with_current_embeddings": 0,
        "files_with_typed_dates": 0,
        "pending_files_with_semantic_index": 0,
        "files_with_stale_enrichment_revision": catalog_stale,
        "files_with_incomplete_catalog": catalog_incomplete,
        "files_with_index_incomplete_reason": passage_eligible - passage_current,
        "files_excluded_by_policy": policy_total,
        "files_without_extracted_text": without_text,
    }
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "mode": "read_only_offline",
        "source_attestation": "explicit_private_offline_copy",
        "scope": {
            "tenant_sha256": _sha256(tenant.encode()),
            "uploader_sha256": _sha256(exact_uploader.encode()) if exact_uploader else None,
            "lifecycle": "live_only",
            "text_bearing": "nonempty_extracted_text",
            "max_rows": cap,
        },
        "database": {
            "schema_version": schema_version,
            "schema_fingerprint_sha256": _schema_hash(
                conn,
                (
                    "schema_meta",
                    "users",
                    "raw_objects",
                    "inbox",
                    "knowledge_objects",
                    "raw_fts",
                    "raw_fts_docsize",
                    "document_catalog",
                    "document_passage_projections",
                    "document_passages",
                    "private_entity_material_cache_state",
                    "private_entity_material_derivative_state",
                    "private_entity_material_derivative_cache",
                ),
            ),
        },
        "projections": projections,
        "counts": {key: int(counts[key]) for key in COUNT_KEYS},
        "incomplete_reasons": incomplete,
        "passage_index": {
            "eligible_authorized_files": passage_eligible,
            "current": passage_current,
            "explicit_incomplete": passage_explicit_incomplete,
            "missing": passage_missing,
            "stale": passage_stale,
            "child_rows": passage_child_rows,
            "incomplete_reasons": {
                reason: int(passage_incomplete_reasons.get(reason, 0))
                for reason in PASSAGE_INCOMPLETE_REASONS
            },
            "coverage_complete": passage_coverage_complete,
            "index_complete": passage_index_complete,
        },
        "excluded_by_policy": {key: int(excluded.get(key, 0)) for key in EXCLUSION_KEYS},
        "completeness": {
            "status": "incomplete",
            "uncapped": True,
            "scope_accounted": True,
            "catalog_complete": bool(
                catalog_available and catalogued == registered and catalog_incomplete == 0
            ),
            "lexical_complete": bool(lexical_available and lexical == registered),
            "semantic_complete": bool(catalog_available and semantic_titles == registered),
            "passage_coverage_complete": passage_coverage_complete,
            "passage_index_complete": passage_index_complete,
            "typed_dates_complete": False,
        },
        "scope_fingerprint_sha256": fingerprint.hexdigest(),
    }
    report["report_sha256"] = _sha256(_canonical_json(report))
    validate_report(report)
    return report


def _exact_keys(value: Any, expected: tuple[str, ...] | set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ContractError(f"invalid {label} keys")
    return value


def _counts(value: Any, expected: tuple[str, ...], *, label: str) -> dict[str, int]:
    obj = _exact_keys(value, expected, label=label)
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in obj.values()):
        raise ContractError(f"invalid {label} counts")
    return obj  # type: ignore[return-value]


def validate_report(report: dict[str, Any]) -> None:
    """Validate exact v3 shape and reject internally coherent-looking lies."""

    top = _exact_keys(
        report,
        {
            "schema",
            "mode",
            "source_attestation",
            "scope",
            "database",
            "projections",
            "counts",
            "incomplete_reasons",
            "passage_index",
            "excluded_by_policy",
            "completeness",
            "scope_fingerprint_sha256",
            "report_sha256",
        },
        label="report",
    )
    if (
        top["schema"] != REPORT_SCHEMA
        or top["mode"] != "read_only_offline"
        or top["source_attestation"] != "explicit_private_offline_copy"
    ):
        raise ContractError("invalid report identity")
    scope = _exact_keys(
        top["scope"],
        ("tenant_sha256", "uploader_sha256", "lifecycle", "text_bearing", "max_rows"),
        label="scope",
    )
    if not HEX64.fullmatch(str(scope["tenant_sha256"])):
        raise ContractError("invalid tenant digest")
    if scope["uploader_sha256"] is not None and not HEX64.fullmatch(str(scope["uploader_sha256"])):
        raise ContractError("invalid uploader digest")
    if scope["lifecycle"] != "live_only" or scope["text_bearing"] != "nonempty_extracted_text":
        raise ContractError("invalid scope semantics")
    _bounded_rows(scope["max_rows"])
    database = _exact_keys(top["database"], ("schema_version", "schema_fingerprint_sha256"), label="database")
    if database["schema_version"] != int(SCHEMA_VERSION) or not HEX64.fullmatch(
        str(database["schema_fingerprint_sha256"])
    ):
        raise ContractError("invalid database identity")

    projections = _exact_keys(top["projections"], PROJECTION_KEYS, label="projections")
    for name, value in projections.items():
        projection = _exact_keys(value, ("status", "revision_sha256"), label=f"projection {name}")
        if projection["status"] not in {"available", "not_available", "unsupported"}:
            raise ContractError("invalid projection status")
        digest = projection["revision_sha256"]
        if projection["status"] == "available" and not HEX64.fullmatch(str(digest)):
            raise ContractError("available projection lacks a revision digest")
        if projection["status"] == "not_available" and digest is not None:
            raise ContractError("absent projection has a revision digest")
        if projection["status"] == "unsupported" and not HEX64.fullmatch(str(digest)):
            raise ContractError("unsupported projection lacks a schema digest")
    if projections["raw_registry"]["status"] != "available":
        raise ContractError("raw registry must be available")
    catalog_available = projections["document_catalog"]["status"] == "available"
    if catalog_available != (
        projections["semantic_title"]["status"] == "available"
        and projections["enrichment_revision"]["status"] == "available"
    ):
        raise ContractError("catalog projection availability is inconsistent")
    passage_available = projections["document_passages"]["status"] == "available"
    # Remaining later projection shapes remain intentionally unsupported.
    metric_for_projection = {
        "document_embeddings": "files_with_current_embeddings",
        "typed_dates": "files_with_typed_dates",
        "pending_semantic_index": "pending_files_with_semantic_index",
    }

    counts = _counts(top["counts"], COUNT_KEYS, label="report")
    reasons = _counts(top["incomplete_reasons"], INCOMPLETE_KEYS, label="incomplete reason")
    excluded = _counts(top["excluded_by_policy"], EXCLUSION_KEYS, label="policy exclusion")
    registered = counts["registered_authorized_live_text_bearing_files"]
    pending = counts["pending_registered_files"]
    passage_eligible = registered + counts["files_without_extracted_text"]
    bounded_metrics = (
        "catalogued_files",
        "lexically_searchable_files",
        "files_with_semantic_title",
        "files_with_stale_enrichment_revision",
        "files_with_incomplete_catalog",
        "files_with_passages",
    )
    if pending > registered or any(counts[key] > registered for key in bounded_metrics):
        raise ContractError("coverage exceeds the registered scope")
    if counts["files_excluded_by_policy"] != sum(excluded.values()):
        raise ContractError("policy exclusion partition is inconsistent")
    if counts["tenant_file_rows_examined"] != (
        registered + counts["files_excluded_by_policy"] + counts["files_without_extracted_text"]
    ):
        raise ContractError("scoped row partition is inconsistent")
    for projection_name, metric in metric_for_projection.items():
        if projections[projection_name]["status"] == "available":
            raise ContractError("v3 cannot claim an unimplemented later projection")
        if counts[metric] != 0:
            raise ContractError("unavailable projection has nonzero coverage")
    if not catalog_available and (
        counts["catalogued_files"]
        or counts["files_with_semantic_title"]
        or counts["files_with_stale_enrichment_revision"]
        or counts["files_with_incomplete_catalog"]
    ):
        raise ContractError("unavailable catalog projection has nonzero coverage")
    if counts["files_with_semantic_title"] > counts["catalogued_files"]:
        raise ContractError("semantic title coverage exceeds catalog coverage")
    expected_reasons = {
        "catalog_projection_not_available": 0 if catalog_available else registered,
        "catalog_row_missing": (
            registered - counts["catalogued_files"] - counts["files_with_stale_enrichment_revision"]
            if catalog_available
            else 0
        ),
        "catalog_row_stale": (counts["files_with_stale_enrichment_revision"] if catalog_available else 0),
        "catalog_row_incomplete": (counts["files_with_incomplete_catalog"] if catalog_available else 0),
        "semantic_title_projection_not_available": 0 if catalog_available else registered,
        "semantic_title_missing": (
            registered - counts["files_with_semantic_title"] if catalog_available else 0
        ),
        "passage_projection_not_available": 0 if passage_available else passage_eligible,
        "passage_row_missing": reasons["passage_row_missing"] if passage_available else 0,
        "passage_row_stale": reasons["passage_row_stale"] if passage_available else 0,
        "passage_row_incomplete": reasons["passage_row_incomplete"] if passage_available else 0,
        "embedding_projection_not_available": registered,
        "typed_date_projection_not_available": registered,
        "pending_semantic_projection_not_available": pending,
        "enrichment_revision_projection_not_available": 0 if catalog_available else registered,
        "lexical_projection_not_available": (
            registered if projections["lexical_source_index"]["status"] != "available" else 0
        ),
        "lexical_row_missing": (
            registered - counts["lexically_searchable_files"]
            if projections["lexical_source_index"]["status"] == "available"
            else 0
        ),
        "extracted_text_unavailable": counts["files_without_extracted_text"],
    }
    if counts["files_with_incomplete_catalog"] > counts["catalogued_files"]:
        raise ContractError("incomplete catalog rows exceed catalog coverage")
    if reasons != {key: expected_reasons[key] for key in INCOMPLETE_KEYS}:
        raise ContractError("incomplete reason accounting is inconsistent")
    passage = _exact_keys(
        top["passage_index"],
        (
            "eligible_authorized_files",
            "current",
            "explicit_incomplete",
            "missing",
            "stale",
            "child_rows",
            "incomplete_reasons",
            "coverage_complete",
            "index_complete",
        ),
        label="passage index",
    )
    passage_counts = _counts(
        {
            key: passage[key]
            for key in (
                "eligible_authorized_files",
                "current",
                "explicit_incomplete",
                "missing",
                "stale",
                "child_rows",
            )
        },
        (
            "eligible_authorized_files",
            "current",
            "explicit_incomplete",
            "missing",
            "stale",
            "child_rows",
        ),
        label="passage index",
    )
    passage_reason_counts = _counts(
        passage["incomplete_reasons"],
        PASSAGE_INCOMPLETE_REASONS,
        label="passage incomplete reason",
    )
    if type(passage["coverage_complete"]) is not bool or type(passage["index_complete"]) is not bool:
        raise ContractError("invalid passage completeness flags")
    if passage_counts["eligible_authorized_files"] != passage_eligible:
        raise ContractError("passage scope is inconsistent")
    passage_state_total = sum(
        passage_counts[key] for key in ("current", "explicit_incomplete", "missing", "stale")
    )
    if passage_available:
        if passage_state_total != passage_eligible:
            raise ContractError("passage state partition is inconsistent")
        if not (passage_counts["current"] <= passage_counts["child_rows"] <= passage_counts["current"] * 64):
            raise ContractError("passage child count is inconsistent")
    elif passage_state_total != 0 or passage_counts["child_rows"] != 0:
        raise ContractError("unavailable passage projection has nonzero coverage")
    if sum(passage_reason_counts.values()) != passage_counts["explicit_incomplete"]:
        raise ContractError("passage incomplete reasons do not form a closed partition")
    if counts["files_with_passages"] != passage_counts["current"]:
        raise ContractError("passage coverage count is inconsistent")
    if counts["files_with_index_incomplete_reason"] != (
        passage_counts["eligible_authorized_files"] - passage_counts["current"]
    ):
        raise ContractError("passage incomplete count is inconsistent")
    if reasons["passage_row_missing"] != passage_counts["missing"]:
        raise ContractError("passage missing count is inconsistent")
    if reasons["passage_row_stale"] != passage_counts["stale"]:
        raise ContractError("passage stale count is inconsistent")
    if reasons["passage_row_incomplete"] != passage_counts["explicit_incomplete"]:
        raise ContractError("passage incomplete reason count is inconsistent")
    expected_passage_coverage_complete = bool(
        passage_available
        and passage_counts["current"] + passage_counts["explicit_incomplete"] == passage_eligible
        and passage_counts["missing"] == 0
        and passage_counts["stale"] == 0
    )
    expected_passage_index_complete = bool(
        expected_passage_coverage_complete and passage_counts["current"] == registered
    )
    if passage["coverage_complete"] != expected_passage_coverage_complete:
        raise ContractError("false passage coverage completeness claim")
    if passage["index_complete"] != expected_passage_index_complete:
        raise ContractError("false passage index completeness claim")

    completeness = _exact_keys(
        top["completeness"],
        (
            "status",
            "uncapped",
            "scope_accounted",
            "catalog_complete",
            "lexical_complete",
            "semantic_complete",
            "passage_coverage_complete",
            "passage_index_complete",
            "typed_dates_complete",
        ),
        label="completeness",
    )
    expected_lexical_complete = bool(
        projections["lexical_source_index"]["status"] == "available"
        and counts["lexically_searchable_files"] == registered
    )
    expected_catalog_complete = bool(
        catalog_available
        and counts["catalogued_files"] == registered
        and counts["files_with_incomplete_catalog"] == 0
    )
    expected_semantic_complete = bool(catalog_available and counts["files_with_semantic_title"] == registered)
    if completeness != {
        "status": "incomplete",
        "uncapped": True,
        "scope_accounted": True,
        "catalog_complete": expected_catalog_complete,
        "lexical_complete": expected_lexical_complete,
        "semantic_complete": expected_semantic_complete,
        "passage_coverage_complete": expected_passage_coverage_complete,
        "passage_index_complete": expected_passage_index_complete,
        "typed_dates_complete": False,
    }:
        raise ContractError("false or inconsistent completeness claim")
    if not HEX64.fullmatch(str(top["scope_fingerprint_sha256"])):
        raise ContractError("invalid scope fingerprint")
    supplied_hash = str(top["report_sha256"])
    unhashed = dict(top)
    unhashed.pop("report_sha256", None)
    if not HEX64.fullmatch(supplied_hash) or supplied_hash != _sha256(_canonical_json(unhashed)):
        raise ContractError("report digest mismatch")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="Explicit offline SQLite copy")
    parser.add_argument(
        "--offline-copy",
        required=True,
        action="store_true",
        help="Attest that --database is a private, quiescent offline copy",
    )
    parser.add_argument("--tenant", required=True, help="Exact tenant user_id")
    parser.add_argument("--uploader", default=None, help="Optional exact uploaded_by scope")
    parser.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS)
    args = parser.parse_args(argv)

    try:
        conn = open_offline_database(args.database)
        try:
            report = audit_document_catalog(
                conn,
                tenant_id=args.tenant,
                uploader=args.uploader,
                max_rows=args.max_rows,
            )
        finally:
            conn.close()
    except ContractError as exc:
        payload = {
            "schema": REPORT_SCHEMA,
            "mode": "read_only_offline",
            "error": "contract",
            "message": str(exc),
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
        return 2
    sys.stdout.write(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
