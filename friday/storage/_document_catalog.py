"""Storage API for the body-free, rebuildable DocumentCatalog sidecar."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections import Counter
from collections.abc import Sequence
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
from friday.document_catalog.worker_state import (
    DOCUMENT_CATALOG_WORKER_STATE_KEY,
    decode_document_catalog_worker_state,
    document_catalog_worker_tenant_key,
    load_document_catalog_worker_namespace_key,
)
from friday.storage._base import (
    StorageShared,
    deleted_account_tombstone_key,
    utc_now,
    validate_user_id,
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")
DOCUMENT_CATALOG_DEFAULT_WORK_ITEMS = 64
DOCUMENT_CATALOG_MAX_WORK_ITEMS = 256
DOCUMENT_CATALOG_RAW_TEXT_WORK_BUDGET_BYTES = 8 * 1024 * 1024


def _document_passage_retryable_sql() -> str:
    # ``passage_projection`` imports the public retrieval package, which imports
    # the assembled storage surface.  Resolve the writer only after storage has
    # finished importing so the storage-independent projection stays cycle-free.
    from friday.document_catalog.passage_writer import document_passage_retryable_sql

    return document_passage_retryable_sql("source", "passage")


def _publish_document_passages_in_transaction(
    conn: sqlite3.Connection,
    raw: sqlite3.Row,
    *,
    projected_at: str,
) -> int:
    from friday.document_catalog.passage_writer import publish_document_passages_in_transaction

    return publish_document_passages_in_transaction(conn, raw, projected_at=projected_at)


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
    if type(value) is not int or not 1 <= value <= DOCUMENT_CATALOG_MAX_WORK_ITEMS:
        raise ValueError(f"limit must be between 1 and {DOCUMENT_CATALOG_MAX_WORK_ITEMS}")
    return value


def _bounded_cursor(value: str | None) -> str | None:
    if value is not None and type(value) is not str:
        raise ValueError("after_raw_object_id must be exact TEXT or None")
    return value


def _exact_raw_object_id(value: str) -> str:
    if type(value) is not str:
        raise ValueError("raw_object_id must be exact TEXT")
    return value


def _raw_text_descriptors(
    conn: sqlite3.Connection,
    query: str,
    params: tuple[object, ...],
) -> list[sqlite3.Row]:
    """Fetch one bounded body-free identity/state page before sidecar mutations."""

    return conn.execute(query, params).fetchall()


def _raw_projection_source(
    conn: sqlite3.Connection,
    *,
    owner: str,
    raw_object_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT id,version,content_hash,raw_content,metadata_json
             FROM raw_objects
            WHERE id=? AND user_id=? AND content_type='file' AND deleted_at IS NULL""",
        (raw_object_id, owner),
    ).fetchone()


def _raw_projection_size(
    conn: sqlite3.Connection,
    *,
    owner: str,
    raw_object_id: str,
) -> int | None:
    row = conn.execute(
        """SELECT COALESCE(length(CAST(raw_content AS BLOB)),0) AS raw_text_bytes
             FROM raw_objects
            WHERE id=? AND user_id=? AND content_type='file' AND deleted_at IS NULL""",
        (raw_object_id, owner),
    ).fetchone()
    return None if row is None else max(0, int(row["raw_text_bytes"] or 0))


def _fits_raw_text_budget(*, processed: int, consumed: int, next_size: int) -> bool:
    """Allow one oversized row for progress, then stop before exceeding the cap."""

    return processed == 0 or consumed + next_size <= DOCUMENT_CATALOG_RAW_TEXT_WORK_BUDGET_BYTES


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
            _exact_raw_object_id(raw["id"]),
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


def _document_catalog_owner_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT owner.id AS user_id
             FROM users AS owner
            WHERE owner.status='active' AND EXISTS (
                  SELECT 1
                    FROM raw_objects AS source
                         INDEXED BY idx_document_catalog_source_owner_id
                   WHERE source.user_id=owner.id AND source.content_type='file'
                     AND source.deleted_at IS NULL
                   LIMIT 1)
            ORDER BY owner.id"""
    ).fetchall()
    return [validate_user_id(row["user_id"]) for row in rows]


class DocumentCatalogMixin(StorageShared):
    def list_document_catalog_owner_ids(self) -> list[str]:
        """Return every extant live-file owner, independent of shared-archive mode."""

        return _document_catalog_owner_ids(self.conn)

    def checkpoint_document_catalog_worker_state(
        self,
        *,
        expected_value: str | None,
        value: str,
        tenant_ids: Sequence[str],
    ) -> bool:
        """CAS worker state only while its exact file-owner snapshot is still live."""

        if expected_value is not None and type(expected_value) is not str:
            raise ValueError("expected worker state must be TEXT or None")
        if type(value) is not str:
            raise ValueError("worker state must be TEXT")
        if isinstance(tenant_ids, str):
            raise ValueError("tenant_ids must be a sequence of exact user ids")
        owners = tuple(validate_user_id(item) for item in tenant_ids)
        if owners != tuple(sorted(set(owners))):
            raise ValueError("tenant_ids must be exact, unique and sorted")
        state, supported = decode_document_catalog_worker_state(value)
        if not supported:
            raise ValueError("worker state must use the closed supported format")

        with self.transaction() as conn:
            current_owners = tuple(_document_catalog_owner_ids(conn))
            if current_owners != owners:
                return False
            namespace_key = load_document_catalog_worker_namespace_key(conn)
            owner_keys = {
                document_catalog_worker_tenant_key(owner, namespace_key=namespace_key) for owner in owners
            }
            if not set(state.tenants).issubset(owner_keys):
                return False
            tombstone_keys = tuple(deleted_account_tombstone_key(owner) for owner in owners)
            for offset in range(0, len(tombstone_keys), 500):
                chunk = tombstone_keys[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                if conn.execute(
                    f"SELECT 1 FROM runtime_kv WHERE key IN ({placeholders}) LIMIT 1",  # nosec B608
                    chunk,
                ).fetchone():
                    return False
            row = conn.execute(
                "SELECT value FROM runtime_kv WHERE key=?",
                (DOCUMENT_CATALOG_WORKER_STATE_KEY,),
            ).fetchone()
            current_value = str(row["value"]) if row is not None else None
            if current_value != expected_value:
                return False
            conn.execute(
                """INSERT INTO runtime_kv(key,value,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value,updated_at=excluded.updated_at""",
                (DOCUMENT_CATALOG_WORKER_STATE_KEY, value, utc_now()),
            )
        return True

    def get_document_catalog_entry(
        self,
        user_id: str,
        raw_object_id: str,
    ) -> dict[str, Any] | None:
        """Read one live exact projection through its authoritative owner."""

        owner = validate_user_id(user_id)
        raw_id = _exact_raw_object_id(raw_object_id)
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
        raw_id = _exact_raw_object_id(raw_object_id)
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
        after_raw_object_id: str | None = None,
        limit: int = DOCUMENT_CATALOG_DEFAULT_WORK_ITEMS,
    ) -> dict[str, Any]:
        """Deterministically enrich one bounded owner page from authoritative Raw."""

        owner = validate_user_id(user_id)
        bounded = _bounded_limit(limit)
        cursor = _bounded_cursor(after_raw_object_id)
        cursor_predicate = "" if cursor is None else "AND source.id>?"
        cursor_params: tuple[object, ...] = () if cursor is None else (cursor,)
        now = _canonical_timestamp(None)
        reason_counts: Counter[str] = Counter()
        current = 0
        changed = 0
        consumed_bytes = 0
        processed_ids: list[str] = []
        stopped_for_budget = False
        with self.transaction() as conn:
            descriptors = _raw_text_descriptors(
                conn,
                f"""SELECT source.id
                     FROM raw_objects AS source
                          INDEXED BY idx_document_catalog_source_owner_id
                    WHERE source.user_id=? AND source.content_type='file'
                      AND source.deleted_at IS NULL {cursor_predicate}
                    ORDER BY source.id ASC LIMIT ?""",  # nosec B608 - fixed cursor predicate
                (owner, *cursor_params, bounded + 1),
            )
            for descriptor in descriptors[:bounded]:
                raw_id = _exact_raw_object_id(descriptor["id"])
                raw_text_bytes = _raw_projection_size(conn, owner=owner, raw_object_id=raw_id)
                if raw_text_bytes is None:
                    continue
                if not _fits_raw_text_budget(
                    processed=len(processed_ids),
                    consumed=consumed_bytes,
                    next_size=raw_text_bytes,
                ):
                    stopped_for_budget = True
                    break
                raw = _raw_projection_source(conn, owner=owner, raw_object_id=raw_id)
                if raw is None:
                    continue
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
                processed_ids.append(raw_id)
                consumed_bytes += raw_text_bytes
                del extracted_text, raw
            has_more = len(descriptors) > len(processed_ids)
        return {
            "processed": len(processed_ids),
            "changed": changed,
            "current": current,
            "explicit_incomplete": len(processed_ids) - current,
            "incomplete_reasons": {
                reason: int(reason_counts.get(reason, 0)) for reason in DOCUMENT_CATALOG_INCOMPLETE_REASONS
            },
            "has_more": has_more,
            "next_after_raw_object_id": processed_ids[-1] if has_more and processed_ids else None,
            "raw_text_bytes_examined": consumed_bytes,
            "raw_text_byte_budget": DOCUMENT_CATALOG_RAW_TEXT_WORK_BUDGET_BYTES,
            "byte_budget_reached": stopped_for_budget,
        }

    def backfill_document_catalog(
        self,
        user_id: str,
        *,
        after_raw_object_id: str | None,
        limit: int = DOCUMENT_CATALOG_DEFAULT_WORK_ITEMS,
        include_document_passages: bool = False,
    ) -> dict[str, Any]:
        """Converge retryable rows in one bounded, caller-checkpointed Raw page."""

        owner = validate_user_id(user_id)
        bounded = _bounded_limit(limit)
        if type(include_document_passages) is not bool:
            raise ValueError("include_document_passages must be an exact boolean")
        cursor = _bounded_cursor(after_raw_object_id)
        cursor_predicate = "" if cursor is None else "AND source.id>?"
        cursor_params: tuple[object, ...] = () if cursor is None else (cursor,)
        now = _canonical_timestamp(None)
        source_binding = document_catalog_source_binding_sql()
        passage_retryable_predicate = _document_passage_retryable_sql()
        reason_counts: Counter[str] = Counter()
        changed = 0
        passage_changed = 0
        passage_processed = 0
        current = 0
        consumed_bytes = 0
        catalog_processed_ids: list[str] = []
        work_ids: list[str] = []
        examined_ids: list[str] = []
        stopped_for_budget = False
        with self.transaction() as conn:
            descriptors = _raw_text_descriptors(
                conn,
                f"""SELECT source.id,
                           CASE WHEN catalog.raw_object_id IS NULL
                                  OR NOT ({source_binding})
                                  OR (catalog.enrichment_status='incomplete'
                                      AND catalog.incomplete_reason IN (
                                          'backfill_pending','source_changed'
                                      ))
                                THEN 1 ELSE 0 END AS catalog_retryable,
                           CASE WHEN {int(include_document_passages)}=1
                                      AND {passage_retryable_predicate}
                                THEN 1 ELSE 0 END AS passage_retryable
                      FROM raw_objects AS source
                           INDEXED BY idx_document_catalog_source_owner_id
                      LEFT JOIN document_catalog catalog ON catalog.raw_object_id=source.id
                      LEFT JOIN document_passage_projections passage
                             ON passage.raw_object_id=source.id
                     WHERE source.user_id=? AND source.content_type='file'
                       AND source.deleted_at IS NULL {cursor_predicate}
                     ORDER BY source.id LIMIT ?""",  # nosec B608 - canonical fixed predicate
                (owner, *cursor_params, bounded + 1),
            )
            for descriptor in descriptors[:bounded]:
                raw_id = _exact_raw_object_id(descriptor["id"])
                catalog_retryable = bool(descriptor["catalog_retryable"])
                passage_is_retryable = bool(descriptor["passage_retryable"])
                if not catalog_retryable and not passage_is_retryable:
                    examined_ids.append(raw_id)
                    continue
                raw_text_bytes = _raw_projection_size(conn, owner=owner, raw_object_id=raw_id)
                if raw_text_bytes is None:
                    examined_ids.append(raw_id)
                    continue
                if not _fits_raw_text_budget(
                    processed=len(work_ids),
                    consumed=consumed_bytes,
                    next_size=raw_text_bytes,
                ):
                    stopped_for_budget = True
                    break
                raw = _raw_projection_source(conn, owner=owner, raw_object_id=raw_id)
                if raw is None:
                    examined_ids.append(raw_id)
                    continue
                raw_status, raw_reason, extracted_text = _deterministic_projection(raw)
                status = DocumentCatalogStatus(raw_status)
                reason = DocumentCatalogIncompleteReason(raw_reason) if raw_reason is not None else None
                if catalog_retryable:
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
                    catalog_processed_ids.append(raw_id)
                if passage_is_retryable:
                    passage_changed += _publish_document_passages_in_transaction(
                        conn,
                        raw,
                        projected_at=now,
                    )
                    passage_processed += 1
                work_ids.append(raw_id)
                examined_ids.append(raw_id)
                consumed_bytes += raw_text_bytes
                del extracted_text, raw
            has_more = len(descriptors) > len(examined_ids)
        return {
            "examined": len(examined_ids),
            "processed": len(catalog_processed_ids),
            "changed": changed,
            "passage_processed": passage_processed,
            "passage_changed": passage_changed,
            "current": current,
            "explicit_incomplete": len(catalog_processed_ids) - current,
            "incomplete_reasons": {
                reason: int(reason_counts.get(reason, 0)) for reason in DOCUMENT_CATALOG_INCOMPLETE_REASONS
            },
            "has_more": has_more,
            "next_after_raw_object_id": examined_ids[-1] if has_more and examined_ids else None,
            "raw_text_bytes_examined": consumed_bytes,
            "raw_text_byte_budget": DOCUMENT_CATALOG_RAW_TEXT_WORK_BUDGET_BYTES,
            "byte_budget_reached": stopped_for_budget,
        }

    def reconcile_document_catalog(
        self,
        user_id: str,
        *,
        after_raw_object_id: str | None,
        limit: int = DOCUMENT_CATALOG_DEFAULT_WORK_ITEMS,
    ) -> dict[str, Any]:
        """Inspect one indexed Raw page and repair missing/stale derivative rows."""

        owner = validate_user_id(user_id)
        bounded = _bounded_limit(limit)
        cursor = _bounded_cursor(after_raw_object_id)
        cursor_predicate = "" if cursor is None else "AND source.id>?"
        cursor_params: tuple[object, ...] = () if cursor is None else (cursor,)
        now = _canonical_timestamp(None)
        reset = 0
        inserted = 0
        source_binding = document_catalog_source_binding_sql()
        with self.transaction() as conn:
            descriptors = conn.execute(
                f"""SELECT source.id,source.version,source.content_hash,
                           CASE WHEN catalog.raw_object_id IS NULL THEN 1 ELSE 0 END AS missing,
                           CASE WHEN catalog.raw_object_id IS NOT NULL
                                      AND NOT ({source_binding})
                                THEN 1 ELSE 0 END AS stale
                      FROM raw_objects AS source
                           INDEXED BY idx_document_catalog_source_owner_id
                      LEFT JOIN document_catalog catalog ON catalog.raw_object_id=source.id
                     WHERE source.user_id=? AND source.content_type='file'
                       AND source.deleted_at IS NULL {cursor_predicate}
                     ORDER BY source.id LIMIT ?""",  # nosec B608 - canonical fixed predicate
                (owner, *cursor_params, bounded + 1),
            ).fetchall()
            page = descriptors[:bounded]
            for source in page:
                missing = bool(source["missing"])
                stale = bool(source["stale"])
                if not missing and not stale:
                    continue
                reason = (
                    DocumentCatalogIncompleteReason.BACKFILL_PENDING
                    if missing
                    and _valid_source_version(source["version"]) is not None
                    and _valid_sha256(source["content_hash"]) is not None
                    else DocumentCatalogIncompleteReason.SOURCE_CHANGED
                    if _valid_source_version(source["version"]) is not None
                    and _valid_sha256(source["content_hash"]) is not None
                    else DocumentCatalogIncompleteReason.SOURCE_UNAVAILABLE
                )
                changed = _write_projection(
                    conn,
                    source,
                    status=DocumentCatalogStatus.INCOMPLETE,
                    reason=reason,
                    extracted_text=None,
                    enriched_at=now,
                )
                if missing:
                    inserted += changed
                else:
                    reset += changed
            has_more = len(descriptors) > len(page)
        return {
            "examined": len(page),
            "inserted": inserted,
            "reset": reset,
            # Exact Raw triggers prune non-live catalog rows in the same write;
            # this bounded owner scan never performs an unindexed global sweep.
            "removed": 0,
            "has_more": has_more,
            "next_after_raw_object_id": (_exact_raw_object_id(page[-1]["id"]) if has_more and page else None),
        }

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
