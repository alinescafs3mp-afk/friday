"""Atomic writer for the schema-47 body-free document passage sidecar.

The durable cursor and work budget belong to the existing DocumentCatalog
worker.  This module owns only one source-bound projection transition inside
that worker's SQLite transaction; a process failure therefore leaves either the
old explicit-incomplete row or the complete parent/child set, never a partial
publication.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from friday.document_catalog.passage_projection import (
    DOCUMENT_PASSAGE_INDEX_REVISION,
    DocumentPassageProjection,
    DocumentPassageProjectionStatus,
)
from friday.document_catalog.passage_schema import document_passage_set_sha256
from friday.document_catalog.schema import deterministic_document_extraction_state
from friday.retrieval._contract_utils import RetrievalContractError, bounded_text

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RETRYABLE_REASONS = ("backfill_pending", "source_changed")


def document_passage_retryable_sql(
    source_alias: str = "source",
    projection_alias: str = "passage",
) -> str:
    """Return the fixed predicate consumed by the bounded catalog worker."""

    aliases = (source_alias, projection_alias)
    if any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias) is None for alias in aliases):
        raise ValueError("document passage SQL alias is invalid")
    source, projection = aliases
    return f"""(
        {projection}.raw_object_id IS {source}.id
        AND {projection}.source_version IS {source}.version
        AND {projection}.source_content_sha256 IS {source}.content_hash
        AND {projection}.passage_index_revision='{DOCUMENT_PASSAGE_INDEX_REVISION}'
        AND {projection}.projection_status='incomplete'
        AND {projection}.incomplete_reason IN ('backfill_pending','source_changed')
    )"""


def _valid_source_version(value: object) -> int | None:
    return value if type(value) is int and value >= 1 else None


def _valid_sha256(value: object) -> str | None:
    return value if type(value) is str and _HEX64.fullmatch(value) is not None else None


def _passage_rows(projection: DocumentPassageProjection) -> tuple[tuple[int, int, int, str], ...]:
    return tuple(
        (
            passage.chunk_index,
            passage.start_char,
            passage.end_char,
            passage.content_sha256,
        )
        for passage in projection.passages
    )


def _stored_child_rows(
    conn: sqlite3.Connection,
    raw_object_id: str,
) -> tuple[tuple[int, int, int, str], ...]:
    return tuple(
        (int(row[0]), int(row[1]), int(row[2]), str(row[3]))
        for row in conn.execute(
            """SELECT chunk_index,start_char,end_char,content_sha256
                 FROM document_passages
                WHERE raw_object_id=? ORDER BY chunk_index""",
            (raw_object_id,),
        ).fetchall()
    )


def _current_projection_is_consistent(
    row: sqlite3.Row,
    *,
    source_version: int | None,
    source_digest: str | None,
    passage_rows: tuple[tuple[int, int, int, str], ...],
) -> bool:
    try:
        source_chars = row["source_char_count"]
        passage_count = row["passage_count"]
        return bool(
            source_version is not None
            and source_digest is not None
            and row["source_version"] == source_version
            and row["source_content_sha256"] == source_digest
            and _valid_sha256(row["extracted_text_sha256"]) is not None
            and type(source_chars) is int
            and source_chars >= 1
            and _valid_sha256(row["passage_set_sha256"]) is not None
            and row["passage_index_revision"] == DOCUMENT_PASSAGE_INDEX_REVISION
            and row["projection_status"] == DocumentPassageProjectionStatus.CURRENT.value
            and row["incomplete_reason"] is None
            and type(passage_count) is int
            and passage_count == len(passage_rows)
            and 1 <= passage_count <= 64
            and passage_rows[0][0] == 0
            and passage_rows[0][1] == 0
            and passage_rows[-1][0] == passage_count - 1
            and passage_rows[-1][2] == source_chars
            and document_passage_set_sha256(passage_rows) == row["passage_set_sha256"]
        )
    except (IndexError, TypeError, ValueError):
        return False


def publish_document_passages_in_transaction(
    conn: sqlite3.Connection,
    raw: sqlite3.Row | dict[str, Any],
    *,
    projected_at: str,
) -> int:
    """CAS one exact current projection; return one only for a new publication."""

    if not conn.in_transaction:
        raise RuntimeError("document passage publication requires an existing transaction")
    raw_object_id = raw["id"]
    source_version = _valid_source_version(raw["version"])
    source_digest = _valid_sha256(raw["content_hash"])
    if type(raw_object_id) is not str:
        return 0
    stored = conn.execute(
        "SELECT * FROM document_passage_projections WHERE raw_object_id=?",
        (raw_object_id,),
    ).fetchone()
    if stored is None:
        raise sqlite3.DatabaseError("document passage projection admission is missing")

    if stored["projection_status"] == DocumentPassageProjectionStatus.CURRENT.value:
        stored_children = _stored_child_rows(conn, raw_object_id)
        if not _current_projection_is_consistent(
            stored,
            source_version=source_version,
            source_digest=source_digest,
            passage_rows=stored_children,
        ):
            raise sqlite3.DatabaseError("current document passage projection is inconsistent")
        return 0

    if stored["incomplete_reason"] not in _RETRYABLE_REASONS:
        return 0
    raw_content = raw["raw_content"]
    if (
        stored["projection_status"] != DocumentPassageProjectionStatus.INCOMPLETE.value
        or source_version is None
        or source_digest is None
        or stored["source_version"] != source_version
        or stored["source_content_sha256"] != source_digest
        or int(stored["passage_count"]) != 0
        or _stored_child_rows(conn, raw_object_id)
    ):
        raise sqlite3.DatabaseError("document passage projection admission is inconsistent")
    if (
        type(raw_content) is not str
        or deterministic_document_extraction_state(raw_content, raw["metadata_json"]) != "current"
    ):
        return 0
    try:
        bounded_text(
            raw_object_id,
            label="document passage Raw Object ID",
            maximum_bytes=200,
        )
    except RetrievalContractError:
        # Schema 47 already admits historical arbitrary SQL TEXT identities in
        # explicit-incomplete state, but its released validator never admitted
        # them as CURRENT. Preserve that fallback-compatible boundary.
        return 0
    projection = DocumentPassageProjection.from_complete_text(
        raw_object_id=raw_object_id,
        source_version=source_version,
        source_content_sha256=source_digest,
        extracted_text=raw_content,
    )
    passage_rows = _passage_rows(projection)
    set_digest = document_passage_set_sha256(passage_rows)

    changed = conn.execute(
        """UPDATE document_passage_projections
              SET extracted_text_sha256=?,source_char_count=?,passage_set_sha256=?,
                  passage_index_revision=?,projection_status='current',
                  incomplete_reason=NULL,passage_count=?,projected_at=?
            WHERE raw_object_id=? AND projection_status='incomplete'
              AND incomplete_reason IN ('backfill_pending','source_changed')
              AND source_version=? AND source_content_sha256=? AND passage_count=0""",
        (
            projection.extracted_text_sha256,
            projection.source_char_count,
            set_digest,
            projection.passage_index_revision,
            len(projection.passages),
            projected_at,
            raw_object_id,
            source_version,
            source_digest,
        ),
    )
    if changed.rowcount != 1:
        raise sqlite3.DatabaseError("document passage projection admission changed during publication")
    conn.executemany(
        """INSERT INTO document_passages(
               raw_object_id,chunk_index,start_char,end_char,content_sha256
           ) VALUES(?,?,?,?,?)""",
        ((raw_object_id, *passage) for passage in passage_rows),
    )
    if _stored_child_rows(conn, raw_object_id) != passage_rows:
        raise sqlite3.DatabaseError("document passage publication is incomplete")
    return 1


__all__ = [
    "document_passage_retryable_sql",
    "publish_document_passages_in_transaction",
]
