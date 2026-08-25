"""Storage API for the body-free, rebuildable DocumentCatalog sidecar."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from friday.document_catalog.schema import (
    DOCUMENT_CATALOG_ENRICHMENT_REVISION,
    DOCUMENT_CATALOG_INCOMPLETE_REASONS,
    DocumentCatalogIncompleteReason,
    DocumentCatalogStatus,
    deterministic_document_extraction_state,
    deterministic_document_semantic_title,
    document_catalog_source_binding_sql,
)
from friday.storage._base import StorageShared, utc_now, validate_user_id

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def _canonical_timestamp(value: str | None) -> str:
    text = utc_now() if value is None else str(value).strip()
    if _RFC3339.fullmatch(text) is None:
        raise ValueError("enriched_at must be an offset-aware RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("enriched_at must be an offset-aware RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("enriched_at must include a UTC offset")
    return parsed.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _valid_source_version(value: object) -> int | None:
    return value if type(value) is int and value >= 1 else None


def _valid_sha256(value: object) -> str | None:
    return value if type(value) is str and _HEX64.fullmatch(value) is not None else None


def _exact_extracted_text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _deterministic_projection(raw: sqlite3.Row | dict[str, Any]) -> tuple[str, str | None, str | None]:
    """Return status, incomplete reason and exact text hash/title input state."""

    source_version = _valid_source_version(raw["version"])
    source_hash = _valid_sha256(raw["content_hash"])
    if source_version is None or source_hash is None:
        return (
            DocumentCatalogStatus.INCOMPLETE.value,
            DocumentCatalogIncompleteReason.SOURCE_UNAVAILABLE.value,
            None,
        )

    raw_text = raw["raw_content"]
    extraction_state = deterministic_document_extraction_state(raw_text, raw["metadata_json"])
    if extraction_state != DocumentCatalogStatus.CURRENT.value:
        return DocumentCatalogStatus.INCOMPLETE.value, extraction_state, None
    assert type(raw_text) is str
    return DocumentCatalogStatus.CURRENT.value, None, raw_text


def _closed_status(value: str) -> DocumentCatalogStatus:
    try:
        return DocumentCatalogStatus(value)
    except (TypeError, ValueError):
        raise ValueError("enrichment_status must use the closed DocumentCatalog enum") from None


def _closed_reason(value: str | None) -> DocumentCatalogIncompleteReason | None:
    if value is None:
        return None
    try:
        return DocumentCatalogIncompleteReason(value)
    except (TypeError, ValueError):
        raise ValueError("incomplete_reason must use the closed DocumentCatalog enum") from None


def _bounded_limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 5_000:
        raise ValueError("limit must be between 1 and 5000")
    return value


def _catalog_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _write_projection(
    conn: sqlite3.Connection,
    raw: sqlite3.Row,
    *,
    status: DocumentCatalogStatus,
    reason: DocumentCatalogIncompleteReason | None,
    extracted_text: str | None,
    enriched_at: str,
) -> int:
    source_version = _valid_source_version(raw["version"])
    source_hash = _valid_sha256(raw["content_hash"])
    if status is DocumentCatalogStatus.CURRENT:
        if reason is not None or source_version is None or source_hash is None or extracted_text is None:
            raise ValueError("current enrichment requires an exact valid source and extracted text")
        extracted_hash = _exact_extracted_text_sha256(extracted_text)
        semantic_title = deterministic_document_semantic_title(extracted_text)
    else:
        if reason is None:
            raise ValueError("incomplete enrichment requires an explicit closed reason")
        valid_source = source_version is not None and source_hash is not None
        if reason is DocumentCatalogIncompleteReason.SOURCE_UNAVAILABLE:
            if valid_source:
                raise ValueError("source_unavailable requires an invalid authoritative source revision")
        elif reason in {
            DocumentCatalogIncompleteReason.BACKFILL_PENDING,
            DocumentCatalogIncompleteReason.SOURCE_CHANGED,
        }:
            if not valid_source:
                raise ValueError("transient enrichment state requires a valid source revision")
        else:
            exact_state = deterministic_document_extraction_state(raw["raw_content"], raw["metadata_json"])
            if reason.value != exact_state:
                raise ValueError("incomplete reason does not match authoritative extraction state")
        extracted_hash = None
        semantic_title = None

    cursor = conn.execute(
        """INSERT INTO document_catalog(
               raw_object_id,source_version,source_content_sha256,
               extracted_text_sha256,semantic_title,title_authority,
               enrichment_revision,enrichment_status,incomplete_reason,enriched_at
           ) VALUES(?,?,?,?,?,'navigation_only',?,?,?,?)
           ON CONFLICT(raw_object_id) DO UPDATE SET
               source_version=excluded.source_version,
               source_content_sha256=excluded.source_content_sha256,
               extracted_text_sha256=excluded.extracted_text_sha256,
               semantic_title=excluded.semantic_title,
               title_authority='navigation_only',
               enrichment_revision=excluded.enrichment_revision,
               enrichment_status=excluded.enrichment_status,
               incomplete_reason=excluded.incomplete_reason,
               enriched_at=excluded.enriched_at
           WHERE document_catalog.source_version IS NOT excluded.source_version
              OR document_catalog.source_content_sha256 IS NOT excluded.source_content_sha256
              OR document_catalog.extracted_text_sha256 IS NOT excluded.extracted_text_sha256
              OR document_catalog.semantic_title IS NOT excluded.semantic_title
              OR document_catalog.title_authority<>'navigation_only'
              OR document_catalog.enrichment_revision<>excluded.enrichment_revision
              OR document_catalog.enrichment_status<>excluded.enrichment_status
              OR document_catalog.incomplete_reason IS NOT excluded.incomplete_reason""",
        (
            str(raw["id"]),
            source_version,
            source_hash,
            extracted_hash,
            semantic_title,
            DOCUMENT_CATALOG_ENRICHMENT_REVISION,
            status.value,
            reason.value if reason is not None else None,
            enriched_at,
        ),
    )
    return max(0, int(cursor.rowcount))


def project_document_catalog_raw_in_transaction(
    conn: sqlite3.Connection,
    raw_object_id: str,
) -> None:
    """Project a newly inserted Raw inside its authoritative write transaction."""

    raw = conn.execute(
        """SELECT id,version,content_hash,raw_content,metadata_json
             FROM raw_objects
            WHERE id=? AND content_type='file' AND deleted_at IS NULL""",
        (raw_object_id,),
    ).fetchone()
    if raw is None:
        return
    raw_status, raw_reason, extracted_text = _deterministic_projection(raw)
    status = DocumentCatalogStatus(raw_status)
    reason = DocumentCatalogIncompleteReason(raw_reason) if raw_reason is not None else None
    _write_projection(
        conn,
        raw,
        status=status,
        reason=reason,
        extracted_text=extracted_text,
        enriched_at=_canonical_timestamp(None),
    )


class DocumentCatalogMixin(StorageShared):
    def get_document_catalog_entry(
        self,
        user_id: str,
        raw_object_id: str,
    ) -> dict[str, Any] | None:
        """Read one live exact projection through its authoritative owner."""

        owner = validate_user_id(user_id)
        raw_id = str(raw_object_id or "").strip()
        if not raw_id:
            return None
        source_binding = document_catalog_source_binding_sql()
        row = self.execute(
            f"""SELECT catalog.*
                 FROM raw_objects source
                 JOIN document_catalog catalog ON catalog.raw_object_id=source.id
                WHERE source.id=? AND source.user_id=?
                  AND source.content_type='file' AND source.deleted_at IS NULL
                  AND ({source_binding})""",  # nosec B608 - fixed canonical predicate
            (raw_id, owner),
        ).fetchone()
        return _catalog_row(row)

    def upsert_document_catalog_entry(
        self,
        user_id: str,
        raw_object_id: str,
        *,
        expected_source_version: int,
        expected_source_content_sha256: str,
        enrichment_status: str = "current",
        incomplete_reason: str | None = None,
        enriched_at: str | None = None,
    ) -> dict[str, Any] | None:
        """CAS one source-derived catalog entry without accepting prose input."""

        owner = validate_user_id(user_id)
        raw_id = str(raw_object_id or "").strip()
        if not raw_id:
            raise ValueError("raw_object_id is required")
        if type(expected_source_version) is not int or expected_source_version < 1:
            raise ValueError("expected_source_version must be a positive integer")
        if _valid_sha256(expected_source_content_sha256) is None:
            raise ValueError("expected_source_content_sha256 must be lowercase hex64")
        status = _closed_status(enrichment_status)
        reason = _closed_reason(incomplete_reason)
        if status is DocumentCatalogStatus.CURRENT and reason is not None:
            raise ValueError("current enrichment cannot carry an incomplete reason")
        if status is DocumentCatalogStatus.INCOMPLETE and reason is None:
            raise ValueError("incomplete enrichment requires an explicit reason")
        timestamp = _canonical_timestamp(enriched_at)
        with self.transaction() as conn:
            raw = conn.execute(
                """SELECT id,version,content_hash,raw_content,metadata_json
                     FROM raw_objects
                    WHERE id=? AND user_id=? AND content_type='file' AND deleted_at IS NULL
                      AND version=? AND content_hash=?""",
                (
                    raw_id,
                    owner,
                    expected_source_version,
                    expected_source_content_sha256,
                ),
            ).fetchone()
            if raw is None:
                return None
            extracted_text: str | None = None
            if status is DocumentCatalogStatus.CURRENT:
                projected_status, projected_reason, projected_text = _deterministic_projection(raw)
                if projected_status != DocumentCatalogStatus.CURRENT.value or projected_reason is not None:
                    raise ValueError("source extraction is not complete")
                extracted_text = projected_text
            _write_projection(
                conn,
                raw,
                status=status,
                reason=reason,
                extracted_text=extracted_text,
                enriched_at=timestamp,
            )
            stored = conn.execute(
                "SELECT * FROM document_catalog WHERE raw_object_id=?",
                (raw_id,),
            ).fetchone()
        return _catalog_row(stored)

    def rebuild_document_catalog(
        self,
        user_id: str,
        *,
        after_raw_object_id: str = "",
        limit: int = 500,
    ) -> dict[str, Any]:
        """Deterministically enrich one bounded owner page from authoritative Raw."""

        owner = validate_user_id(user_id)
        bounded = _bounded_limit(limit)
        cursor = str(after_raw_object_id or "").strip()
        if len(cursor) > 200 or any(ord(character) < 32 for character in cursor):
            raise ValueError("after_raw_object_id is invalid")
        now = _canonical_timestamp(None)
        reason_counts: Counter[str] = Counter()
        current = 0
        changed = 0
        with self.transaction() as conn:
            rows = conn.execute(
                """SELECT id,version,content_hash,raw_content,metadata_json
                     FROM raw_objects
                    WHERE user_id=? AND content_type='file' AND deleted_at IS NULL
                      AND id>?
                    ORDER BY id ASC LIMIT ?""",
                (owner, cursor, bounded + 1),
            ).fetchall()
            page = rows[:bounded]
            for raw in page:
                raw_status, raw_reason, extracted_text = _deterministic_projection(raw)
                status = DocumentCatalogStatus(raw_status)
                reason = DocumentCatalogIncompleteReason(raw_reason) if raw_reason is not None else None
                changed += _write_projection(
                    conn,
                    raw,
                    status=status,
                    reason=reason,
                    extracted_text=extracted_text,
                    enriched_at=now,
                )
                if status is DocumentCatalogStatus.CURRENT:
                    current += 1
                else:
                    assert reason is not None
                    reason_counts[reason.value] += 1
        has_more = len(rows) > bounded
        return {
            "processed": len(page),
            "changed": changed,
            "current": current,
            "explicit_incomplete": len(page) - current,
            "incomplete_reasons": {
                reason: int(reason_counts.get(reason, 0)) for reason in DOCUMENT_CATALOG_INCOMPLETE_REASONS
            },
            "next_after_raw_object_id": str(page[-1]["id"]) if has_more and page else None,
        }

    def backfill_document_catalog(
        self,
        user_id: str,
        *,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Converge one bounded retryable page without a process-local cursor."""

        owner = validate_user_id(user_id)
        bounded = _bounded_limit(limit)
        now = _canonical_timestamp(None)
        source_binding = document_catalog_source_binding_sql()
        reason_counts: Counter[str] = Counter()
        changed = 0
        current = 0
        with self.transaction() as conn:
            rows = conn.execute(
                f"""SELECT source.id,source.version,source.content_hash,
                           source.raw_content,source.metadata_json
                      FROM raw_objects source
                      LEFT JOIN document_catalog catalog ON catalog.raw_object_id=source.id
                     WHERE source.user_id=? AND source.content_type='file'
                       AND source.deleted_at IS NULL
                       AND (
                            catalog.raw_object_id IS NULL
                            OR NOT ({source_binding})
                            OR (catalog.enrichment_status='incomplete'
                                AND catalog.incomplete_reason IN (
                                    'backfill_pending','source_changed'
                                ))
                       )
                     ORDER BY source.id LIMIT ?""",  # nosec B608 - canonical fixed predicate
                (owner, bounded),
            ).fetchall()
            for raw in rows:
                raw_status, raw_reason, extracted_text = _deterministic_projection(raw)
                status = DocumentCatalogStatus(raw_status)
                reason = DocumentCatalogIncompleteReason(raw_reason) if raw_reason is not None else None
                changed += _write_projection(
                    conn,
                    raw,
                    status=status,
                    reason=reason,
                    extracted_text=extracted_text,
                    enriched_at=now,
                )
                if status is DocumentCatalogStatus.CURRENT:
                    current += 1
                else:
                    assert reason is not None
                    reason_counts[reason.value] += 1
        return {
            "processed": len(rows),
            "changed": changed,
            "current": current,
            "explicit_incomplete": len(rows) - current,
            "incomplete_reasons": {
                reason: int(reason_counts.get(reason, 0)) for reason in DOCUMENT_CATALOG_INCOMPLETE_REASONS
            },
            "remaining_retryable": int(
                self.execute(
                    f"""SELECT COUNT(*)
                          FROM raw_objects source
                          LEFT JOIN document_catalog catalog ON catalog.raw_object_id=source.id
                         WHERE source.user_id=? AND source.content_type='file'
                           AND source.deleted_at IS NULL
                           AND (
                                catalog.raw_object_id IS NULL
                                OR NOT ({source_binding})
                                OR (catalog.enrichment_status='incomplete'
                                    AND catalog.incomplete_reason IN (
                                        'backfill_pending','source_changed'
                                    ))
                           )""",  # nosec B608 - canonical fixed predicate
                    (owner,),
                ).fetchone()[0]
            ),
        }

    def reconcile_document_catalog(
        self,
        user_id: str,
        *,
        limit: int = 500,
    ) -> dict[str, int]:
        """Boundedly repair missing/stale derivative rows; never source authority."""

        owner = validate_user_id(user_id)
        bounded = _bounded_limit(limit)
        now = _canonical_timestamp(None)
        removed = 0
        reset = 0
        inserted = 0
        source_binding = document_catalog_source_binding_sql()
        with self.transaction() as conn:
            stale = conn.execute(
                f"""SELECT catalog.raw_object_id,source.id AS source_id,
                          source.version,source.content_hash,
                          source.content_type,source.deleted_at
                     FROM document_catalog catalog
                     JOIN raw_objects source ON source.id=catalog.raw_object_id
                    WHERE source.user_id=? AND (
                          source.content_type<>'file'
                          OR source.deleted_at IS NOT NULL
                          OR NOT ({source_binding})
                    )
                    ORDER BY catalog.raw_object_id LIMIT ?""",  # nosec B608
                (owner, bounded),
            ).fetchall()
            for row in stale:
                if row["content_type"] != "file" or row["deleted_at"] is not None:
                    removed += max(
                        0,
                        int(
                            conn.execute(
                                "DELETE FROM document_catalog WHERE raw_object_id=?",
                                (row["raw_object_id"],),
                            ).rowcount
                        ),
                    )
                    continue
                source = conn.execute(
                    """SELECT id,version,content_hash,raw_content,metadata_json
                         FROM raw_objects WHERE id=? AND user_id=?
                           AND content_type='file' AND deleted_at IS NULL""",
                    (row["raw_object_id"], owner),
                ).fetchone()
                if source is None:
                    continue
                reason = (
                    DocumentCatalogIncompleteReason.SOURCE_CHANGED
                    if _valid_source_version(source["version"]) is not None
                    and _valid_sha256(source["content_hash"]) is not None
                    else DocumentCatalogIncompleteReason.SOURCE_UNAVAILABLE
                )
                reset += _write_projection(
                    conn,
                    source,
                    status=DocumentCatalogStatus.INCOMPLETE,
                    reason=reason,
                    extracted_text=None,
                    enriched_at=now,
                )

            remaining = max(0, bounded - len(stale))
            if remaining:
                missing = conn.execute(
                    """SELECT source.id,source.version,source.content_hash,
                              source.raw_content,source.metadata_json
                         FROM raw_objects source
                         LEFT JOIN document_catalog catalog ON catalog.raw_object_id=source.id
                        WHERE source.user_id=? AND source.content_type='file'
                          AND source.deleted_at IS NULL AND catalog.raw_object_id IS NULL
                        ORDER BY source.id LIMIT ?""",
                    (owner, remaining),
                ).fetchall()
                for source in missing:
                    reason = (
                        DocumentCatalogIncompleteReason.BACKFILL_PENDING
                        if _valid_source_version(source["version"]) is not None
                        and _valid_sha256(source["content_hash"]) is not None
                        else DocumentCatalogIncompleteReason.SOURCE_UNAVAILABLE
                    )
                    inserted += _write_projection(
                        conn,
                        source,
                        status=DocumentCatalogStatus.INCOMPLETE,
                        reason=reason,
                        extracted_text=None,
                        enriched_at=now,
                    )
        return {"examined": len(stale) + inserted, "inserted": inserted, "reset": reset, "removed": removed}

    def document_catalog_coverage(self, user_id: str) -> dict[str, Any]:
        """Count current, explicitly incomplete, missing and stale owner rows."""

        owner = validate_user_id(user_id)
        source_binding = document_catalog_source_binding_sql()
        row = self.execute(
            f"""SELECT COUNT(*) AS eligible,
                      SUM(catalog.raw_object_id IS NOT NULL
                          AND ({source_binding})) AS catalogued,
                      SUM(catalog.raw_object_id IS NULL) AS missing,
                      SUM(catalog.raw_object_id IS NOT NULL
                          AND NOT ({source_binding})) AS stale,
                      SUM(catalog.enrichment_status='current'
                          AND ({source_binding})) AS current,
                      SUM(catalog.enrichment_status='incomplete'
                          AND catalog.incomplete_reason IS NOT NULL
                          AND ({source_binding})) AS explicit_incomplete
                 FROM raw_objects source
                 LEFT JOIN document_catalog catalog ON catalog.raw_object_id=source.id
                WHERE source.user_id=? AND source.content_type='file'
                  AND source.deleted_at IS NULL""",  # nosec B608 - canonical fixed predicate
            (owner,),
        ).fetchone()
        counts = {
            key: int((row[key] if row is not None else 0) or 0)
            for key in ("eligible", "catalogued", "missing", "stale", "current", "explicit_incomplete")
        }
        reasons = {
            str(item["incomplete_reason"]): int(item["count"])
            for item in self.execute(
                f"""SELECT catalog.incomplete_reason,COUNT(*) AS count
                     FROM raw_objects source
                     JOIN document_catalog catalog ON catalog.raw_object_id=source.id
                    WHERE source.user_id=? AND source.content_type='file'
                      AND source.deleted_at IS NULL
                      AND catalog.enrichment_status='incomplete'
                      AND ({source_binding})
                    GROUP BY catalog.incomplete_reason""",  # nosec B608
                (owner,),
            ).fetchall()
        }
        complete = (
            counts["missing"] == 0 and counts["stale"] == 0 and counts["catalogued"] == counts["eligible"]
        )
        return {
            **counts,
            "incomplete_reasons": {
                reason: int(reasons.get(reason, 0)) for reason in DOCUMENT_CATALOG_INCOMPLETE_REASONS
            },
            "coverage_complete": complete,
            "enrichment_complete": complete and counts["current"] == counts["eligible"],
        }


__all__ = [
    "DocumentCatalogMixin",
    "deterministic_document_semantic_title",
    "project_document_catalog_raw_in_transaction",
]
