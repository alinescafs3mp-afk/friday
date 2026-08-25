#!/usr/bin/env python
"""Secret-free, read-only audit of document discoverability in an offline DB copy.

The auditor recognizes only Friday's exact, body-free ``document_catalog``
sidecar.  A missing or look-alike table remains ``not_available``/``unsupported``;
it is never guessed into coverage.  Current, explicitly incomplete, missing and
stale source-bound rows are counted separately, while later passage, embedding
and typed-date projections continue to report honest gaps.

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
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from friday.storage import SCHEMA_VERSION  # noqa: E402
from friday.storage._privacy import (  # noqa: E402
    _exact_uploader_raw_dependency,
    _not_audio_document,
    _not_private_raw_dependency,
)

REPORT_SCHEMA = "friday.document-catalog-audit.v2"
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
    "document_passages": ("document_passages",),
    "document_embeddings": ("document_embeddings",),
    "typed_dates": ("document_temporal_facts",),
    "pending_semantic_index": ("document_catalog", "document_embeddings"),
    "enrichment_revision": ("document_catalog",),
}

CURRENT_ENRICHMENT_REVISION = 1


class ContractError(RuntimeError):
    """The operator, input snapshot, or output contract is not safe to use."""


class _OfflineConnection(sqlite3.Connection):
    """Connection type issued only after the offline-copy checks pass."""

    _friday_offline_copy_verified: bool


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


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


def _document_catalog_schema_fingerprint(conn: sqlite3.Connection) -> str | None:
    """Delegate exact recognition to the shipped schema-41 validator."""

    try:
        schema_module = importlib.import_module("friday.document_catalog.schema")
    except ImportError:
        return None
    try:
        digest = schema_module.document_catalog_schema_fingerprint(conn)
    except (RuntimeError, ValueError, sqlite3.Error):
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


def _projection_report(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    raw_hash = _schema_hash(conn, ("raw_objects", "inbox"))
    lexical_sql = _table_sql(conn, "raw_fts")
    docsize_sql = _table_sql(conn, "raw_fts_docsize")
    lexical_available = lexical_sql is not None and docsize_sql is not None
    projections: dict[str, dict[str, Any]] = {
        "raw_registry": {"status": "available", "revision_sha256": raw_hash},
        "lexical_source_index": {
            "status": "available" if lexical_available else "not_available",
            "revision_sha256": (
                _schema_hash(conn, ("raw_fts", "raw_fts_docsize")) if lexical_available else None
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

    projections = _projection_report(conn)
    lexical_available = projections["lexical_source_index"]["status"] == "available"
    lexical_expression = (
        "EXISTS (SELECT 1 FROM raw_fts_docsize lexical_docsize WHERE lexical_docsize.id=r.rowid)"
        if lexical_available
        else "0"
    )
    catalog_available = projections["document_catalog"]["status"] == "available"
    catalog_projection = (
        """
               c.raw_object_id IS NOT NULL AS has_catalog_row,
               (c.source_version=r.version
                AND c.source_content_sha256=r.content_hash) AS catalog_source_current,
               (c.enrichment_revision=1) AS catalog_revision_current,
               (c.enrichment_status='current'
                AND c.incomplete_reason IS NULL
                AND typeof(c.extracted_text_sha256)='text'
                AND length(c.extracted_text_sha256)=64
                AND lower(c.extracted_text_sha256) NOT GLOB '*[^0-9a-f]*') AS catalog_current,
               (c.enrichment_status='incomplete'
                AND c.incomplete_reason IS NOT NULL
                AND c.extracted_text_sha256 IS NULL
                AND c.semantic_title IS NULL) AS catalog_incomplete,
               (c.enrichment_status='current'
                AND typeof(c.semantic_title)='text'
                AND length(trim(c.semantic_title))>0) AS has_semantic_title
        """
        if catalog_available
        else """
               0 AS has_catalog_row,
               0 AS catalog_source_current,
               0 AS catalog_revision_current,
               0 AS catalog_current,
               0 AS catalog_incomplete,
               0 AS has_semantic_title
        """
    )
    catalog_join = (
        "LEFT JOIN document_catalog c ON c.raw_object_id=r.id"
        if catalog_available
        else ""
    )
    uploader_expression = _exact_uploader_raw_dependency("r") if exact_uploader else "1"
    query = f"""
        SELECT r.id AS opaque_id,
               r.version AS source_version,
               r.deleted_at IS NOT NULL AS is_deleted,
               EXISTS (
                   SELECT 1 FROM inbox ignored_inbox
                    WHERE ignored_inbox.raw_object_id=r.id
                      AND ignored_inbox.user_id=r.user_id
                      AND ignored_inbox.status='ignored'
               ) AS is_ignored,
               ({_not_private_raw_dependency("r")}) AS is_public,
               ({_not_audio_document("r")}) AS is_document,
               ({uploader_expression}) AS uploader_allowed,
               (typeof(r.raw_content)='text' AND length(trim(r.raw_content))>0) AS has_text,
               EXISTS (
                   SELECT 1 FROM inbox pending_inbox
                    WHERE pending_inbox.raw_object_id=r.id
                      AND pending_inbox.user_id=r.user_id
                      AND pending_inbox.status='pending'
               ) AS is_pending,
               ({lexical_expression}) AS has_lexical_row,
               {catalog_projection}
          FROM raw_objects r
          {catalog_join}
         WHERE r.user_id=? AND r.content_type='file'
         ORDER BY r.rowid
    """  # nosec B608 - every interpolated fragment is a code-owned SQL predicate
    params: tuple[Any, ...] = (exact_uploader, tenant) if exact_uploader else (tenant,)

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
                elif not (
                    bool(row["catalog_source_current"])
                    and bool(row["catalog_revision_current"])
                ):
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
            # IDs never leave the hash state. Include the classification so policy
            # or lifecycle mutations change the snapshot fingerprint.
            fingerprint.update(
                _canonical_json(
                    [
                        raw_id,
                        int(row["source_version"] or 0),
                        state,
                        int(bool(row["has_lexical_row"])),
                        catalog_state if state == "registered" else "excluded",
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

    future_missing = {
        "catalog_projection_not_available": 0 if catalog_available else registered,
        "semantic_title_projection_not_available": 0 if catalog_available else registered,
        "passage_projection_not_available": registered,
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
        "files_with_passages": 0,
        "files_with_current_embeddings": 0,
        "files_with_typed_dates": 0,
        "pending_files_with_semantic_index": 0,
        "files_with_stale_enrichment_revision": catalog_stale,
        "files_with_incomplete_catalog": catalog_incomplete,
        "files_with_index_incomplete_reason": registered + without_text,
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
                    "private_entity_material_cache_state",
                    "private_entity_material_derivative_state",
                    "private_entity_material_derivative_cache",
                ),
            ),
        },
        "projections": projections,
        "counts": {key: int(counts[key]) for key in COUNT_KEYS},
        "incomplete_reasons": incomplete,
        "excluded_by_policy": {key: int(excluded.get(key, 0)) for key in EXCLUSION_KEYS},
        "completeness": {
            "status": "incomplete",
            "uncapped": True,
            "scope_accounted": True,
            "catalog_complete": bool(catalog_available and catalogued == registered),
            "lexical_complete": bool(lexical_available and lexical == registered),
            "semantic_complete": bool(catalog_available and semantic_titles == registered),
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
    """Validate exact v2 shape and reject internally coherent-looking lies."""

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
    # Later projection shapes remain intentionally unsupported by this release.
    metric_for_projection = {
        "document_passages": "files_with_passages",
        "document_embeddings": "files_with_current_embeddings",
        "typed_dates": "files_with_typed_dates",
        "pending_semantic_index": "pending_files_with_semantic_index",
    }

    counts = _counts(top["counts"], COUNT_KEYS, label="report")
    reasons = _counts(top["incomplete_reasons"], INCOMPLETE_KEYS, label="incomplete reason")
    excluded = _counts(top["excluded_by_policy"], EXCLUSION_KEYS, label="policy exclusion")
    registered = counts["registered_authorized_live_text_bearing_files"]
    pending = counts["pending_registered_files"]
    bounded_metrics = (
        "catalogued_files",
        "lexically_searchable_files",
        "files_with_semantic_title",
        "files_with_stale_enrichment_revision",
        "files_with_incomplete_catalog",
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
            raise ContractError("v2 cannot claim an unimplemented later projection")
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
            registered
            - counts["catalogued_files"]
            - counts["files_with_stale_enrichment_revision"]
            if catalog_available
            else 0
        ),
        "catalog_row_stale": (
            counts["files_with_stale_enrichment_revision"] if catalog_available else 0
        ),
        "catalog_row_incomplete": (
            counts["files_with_incomplete_catalog"] if catalog_available else 0
        ),
        "semantic_title_projection_not_available": 0 if catalog_available else registered,
        "semantic_title_missing": (
            registered - counts["files_with_semantic_title"] if catalog_available else 0
        ),
        "passage_projection_not_available": registered,
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
    if counts["files_with_index_incomplete_reason"] != (registered + counts["files_without_extracted_text"]):
        raise ContractError("projection and extraction gaps must have explicit incomplete reasons")

    completeness = _exact_keys(
        top["completeness"],
        (
            "status",
            "uncapped",
            "scope_accounted",
            "catalog_complete",
            "lexical_complete",
            "semantic_complete",
            "typed_dates_complete",
        ),
        label="completeness",
    )
    expected_lexical_complete = bool(
        projections["lexical_source_index"]["status"] == "available"
        and counts["lexically_searchable_files"] == registered
    )
    expected_catalog_complete = bool(
        catalog_available and counts["catalogued_files"] == registered
    )
    expected_semantic_complete = bool(
        catalog_available and counts["files_with_semantic_title"] == registered
    )
    if completeness != {
        "status": "incomplete",
        "uncapped": True,
        "scope_accounted": True,
        "catalog_complete": expected_catalog_complete,
        "lexical_complete": expected_lexical_complete,
        "semantic_complete": expected_semantic_complete,
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
