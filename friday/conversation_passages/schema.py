"""Exact schema-50 contract for body-free conversation-passage projections.

``messages.content`` remains the only ordinary-table source of chat text.  The
sidecar stores one authenticated anchor per eligible message and an incremental
conversation-prefix revision.  Its external-content FTS table is a rebuildable
derivative over an exact view; neither table grants authority to read a message.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from functools import lru_cache
from typing import cast

from friday.conversation_passages.contract import (
    CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256,
    CONVERSATION_PASSAGE_EMPTY_SET_SHA256,
    CONVERSATION_PASSAGE_INDEX_REVISION,
    CONVERSATION_PASSAGE_MAX_COUNT,
    ConversationPassageIncompleteReason,
    ConversationPassageProjectionStatus,
)

CONVERSATION_PASSAGE_SCHEMA_VERSION = 50
CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES = 4 * 1024 * 1024

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CONVERSATION_ID = re.compile(r"conv_[0-9a-f]{16}\Z")
_MESSAGE_ID = re.compile(r"msg_[0-9a-f]{16}\Z")
_OWNED_REFERENCE_RE = re.compile(
    r"(?<![0-9A-Za-z_])(?:conversation_passage_projections|conversation_passages"
    r"|conversation_passage_search_content|conversation_passages_fts)(?![0-9A-Za-z_])",
    re.IGNORECASE,
)
_REASONS = tuple(item.value for item in ConversationPassageIncompleteReason)
_REASON_SQL = ", ".join(f"'{item}'" for item in _REASONS)
# Frozen over all nine normalized schema-49 optional objects (the declared
# view/vtable/triggers and FTS5's four shadow tables).  Schema 50 deliberately
# preserves that public/FTS layout byte for byte.  This lets a SQLite build
# without FTS5 authenticate sealed derivative DDL without trying to load it.
_CONVERSATION_PASSAGE_FTS_SCHEMA_SHA256 = "dfe1007e5af973a050bfbc9e95b048c34373461c2ec7efee91964b6c472b0ede"

# Both parent write guards use this closed rollup predicate.  A non-empty
# projection is publishable only when its tail, count, ordered set digest and
# every child/source binding agree in the same SQLite statement.  The message
# identity grammar is intentionally narrower than the generic safe-text helper:
# a parent field must never become an arbitrary body/path storage seam.
_PROJECTION_ROLLUP_VALID_SQL = """(
    (
    (
        NEW.passage_count=0
        AND NOT EXISTS (
            SELECT 1 FROM conversation_passages passage
             WHERE passage.conversation_id=NEW.conversation_id
        )
    )
    OR
    (
        NEW.passage_count>0
        AND typeof(NEW.indexed_through_message_id)='text'
        AND length(NEW.indexed_through_message_id)=20
        AND substr(NEW.indexed_through_message_id,1,4)='msg_'
        AND substr(NEW.indexed_through_message_id,5,16)
            NOT GLOB '*[^0-9a-f]*'
        AND NEW.passage_count=(
            SELECT COUNT(*) FROM conversation_passages passage
             WHERE passage.conversation_id=NEW.conversation_id
        )
        AND NOT EXISTS (
            SELECT 1
              FROM conversation_passages passage
              JOIN conversations conversation
                ON conversation.id=NEW.conversation_id
              LEFT JOIN messages source
                ON source.id=passage.anchor_message_id
               AND source.conversation_id=conversation.id
               AND source.user_id=conversation.user_id
              LEFT JOIN conversation_passages previous
                ON previous.conversation_id=passage.conversation_id
               AND previous.anchor_ordinal=passage.anchor_ordinal-1
             WHERE passage.conversation_id=NEW.conversation_id
               AND (
                    source.id IS NULL
                    OR source.role NOT IN ('user','assistant')
                    OR length(source.id)<>20
                    OR substr(source.id,1,4)<>'msg_'
                    OR substr(source.id,5,16) GLOB '*[^0-9a-f]*'
                    OR passage.anchor_ordinal<>(
                        SELECT COUNT(*)
                          FROM messages predecessor
                         WHERE predecessor.conversation_id=source.conversation_id
                           AND predecessor.user_id=conversation.user_id
                           AND predecessor.role IN ('user','assistant')
                           AND predecessor.rowid<source.rowid
                    )
                    OR friday_conversation_passage_anchor_valid(
                           source.id,source.conversation_id,source.user_id,
                           conversation.user_id,source.role,source.content,
                           source.created_at,passage.conversation_id,
                           passage.anchor_message_id,passage.anchor_ordinal,
                           passage.anchor_message_revision_sha256,
                           passage.anchor_content_sha256,
                           passage.anchor_locator_sha256)<>1
                    OR passage.conversation_prefix_sha256<>
                       friday_conversation_passage_prefix_sha256(
                           CASE WHEN passage.anchor_ordinal=0 THEN NULL
                                ELSE previous.conversation_prefix_sha256 END,
                           passage.anchor_ordinal,
                           passage.anchor_message_revision_sha256)
                    OR (passage.anchor_ordinal>0 AND previous.passage_rowid IS NULL)
               )
        )
        AND EXISTS (
            SELECT 1
              FROM conversation_passages tail
              JOIN conversations conversation
                ON conversation.id=tail.conversation_id
              JOIN messages source
                ON source.id=tail.anchor_message_id
               AND source.conversation_id=conversation.id
               AND source.user_id=conversation.user_id
             WHERE tail.conversation_id=NEW.conversation_id
               AND tail.anchor_ordinal=NEW.passage_count-1
               AND source.role IN ('user','assistant')
               AND NEW.indexed_through_message_id=tail.anchor_message_id
               AND NEW.indexed_conversation_revision_sha256=
                   tail.conversation_prefix_sha256
        )
        AND NEW.passage_set_sha256=(
            SELECT friday_conversation_passage_set_sha256(
                       ordered.anchor_ordinal,
                       ordered.anchor_message_id,
                       ordered.anchor_message_revision_sha256,
                       ordered.anchor_content_sha256,
                       ordered.anchor_locator_sha256,
                       ordered.conversation_prefix_sha256)
              FROM (
                  SELECT passage.anchor_ordinal,
                         passage.anchor_message_id,
                         passage.anchor_message_revision_sha256,
                         passage.anchor_content_sha256,
                         passage.anchor_locator_sha256,
                         passage.conversation_prefix_sha256
                    FROM conversation_passages passage
                   WHERE passage.conversation_id=NEW.conversation_id
                   ORDER BY passage.anchor_ordinal ASC
                   LIMIT -1
              ) AS ordered
        )
    )
    )
    AND (
        NEW.projection_status<>'current'
        OR NEW.passage_count=(
            SELECT COUNT(*)
              FROM messages source
              JOIN conversations conversation
                ON conversation.id=source.conversation_id
               AND conversation.user_id=source.user_id
             WHERE source.conversation_id=NEW.conversation_id
               AND source.role IN ('user','assistant')
        )
    )
)"""


_RELEASED_CONVERSATION_PASSAGE_SCHEMA_V49 = f"""
CREATE TABLE IF NOT EXISTS conversation_passage_projections (
    conversation_id TEXT NOT NULL PRIMARY KEY
        REFERENCES conversations(id) ON DELETE CASCADE ON UPDATE CASCADE,
    indexed_message_count INTEGER NOT NULL
        CHECK(typeof(indexed_message_count)='integer'
              AND indexed_message_count BETWEEN 0 AND {CONVERSATION_PASSAGE_MAX_COUNT}),
    indexed_through_message_id TEXT,
    indexed_conversation_revision_sha256 TEXT
        CHECK(indexed_conversation_revision_sha256 IS NULL
              OR (typeof(indexed_conversation_revision_sha256)='text'
                  AND length(indexed_conversation_revision_sha256)=64
                  AND indexed_conversation_revision_sha256 NOT GLOB '*[^0-9a-f]*')),
    passage_set_sha256 TEXT
        CHECK(passage_set_sha256 IS NULL
              OR (typeof(passage_set_sha256)='text'
                  AND length(passage_set_sha256)=64
                  AND passage_set_sha256 NOT GLOB '*[^0-9a-f]*')),
    passage_index_revision TEXT NOT NULL
        CHECK(passage_index_revision='{CONVERSATION_PASSAGE_INDEX_REVISION}'),
    projection_status TEXT NOT NULL
        CHECK(projection_status IN ('current','incomplete')),
    incomplete_reason TEXT
        CHECK(incomplete_reason IS NULL OR incomplete_reason IN ({_REASON_SQL})),
    passage_count INTEGER NOT NULL
        CHECK(typeof(passage_count)='integer'
              AND passage_count BETWEEN 0 AND {CONVERSATION_PASSAGE_MAX_COUNT}),
    projected_at TEXT NOT NULL
        CHECK(typeof(projected_at)='text'
              AND length(projected_at)=20
              AND projected_at GLOB
                  '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
              AND substr(projected_at,1,4) BETWEEN '0001' AND '9999'
              AND substr(projected_at,6,2) BETWEEN '01' AND '12'
              AND substr(projected_at,9,2) BETWEEN '01' AND CASE
                  WHEN substr(projected_at,6,2) IN ('01','03','05','07','08','10','12')
                  THEN '31'
                  WHEN substr(projected_at,6,2) IN ('04','06','09','11')
                  THEN '30'
                  WHEN CAST(substr(projected_at,1,4) AS INTEGER)%400=0
                    OR (CAST(substr(projected_at,1,4) AS INTEGER)%4=0
                        AND CAST(substr(projected_at,1,4) AS INTEGER)%100<>0)
                  THEN '29'
                  ELSE '28'
              END
              AND substr(projected_at,12,2) BETWEEN '00' AND '23'
              AND substr(projected_at,15,2) BETWEEN '00' AND '59'
              AND substr(projected_at,18,2) BETWEEN '00' AND '59'
              AND strftime('%Y-%m-%dT%H:%M:%SZ',projected_at) IS NOT NULL
              AND strftime('%Y-%m-%dT%H:%M:%SZ',projected_at)=projected_at),
    CHECK(indexed_message_count=passage_count),
    CHECK(
        (projection_status='current'
         AND incomplete_reason IS NULL
         AND indexed_conversation_revision_sha256 IS NOT NULL
         AND passage_set_sha256 IS NOT NULL
         AND ((passage_count=0 AND indexed_through_message_id IS NULL)
              OR (passage_count>0 AND indexed_through_message_id IS NOT NULL)))
        OR
        (projection_status='incomplete'
         AND incomplete_reason IS NOT NULL
         AND ((passage_count=0
               AND indexed_through_message_id IS NULL
               AND ((indexed_conversation_revision_sha256 IS NULL
                     AND passage_set_sha256 IS NULL)
                    OR (incomplete_reason='source_changed'
                        AND indexed_conversation_revision_sha256=
                            '{CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256}'
                        AND passage_set_sha256=
                            '{CONVERSATION_PASSAGE_EMPTY_SET_SHA256}')))
              OR (passage_count>0
                  AND incomplete_reason<>'source_unavailable'
                  AND indexed_through_message_id IS NOT NULL
                  AND indexed_conversation_revision_sha256 IS NOT NULL
                  AND passage_set_sha256 IS NOT NULL)))
    )
);

CREATE TABLE IF NOT EXISTS conversation_passages (
    passage_rowid INTEGER NOT NULL PRIMARY KEY
        CHECK(typeof(passage_rowid)='integer' AND passage_rowid>=1),
    conversation_id TEXT NOT NULL
        REFERENCES conversation_passage_projections(conversation_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    anchor_message_id TEXT NOT NULL
        REFERENCES messages(id) ON DELETE CASCADE ON UPDATE CASCADE,
    anchor_ordinal INTEGER NOT NULL
        CHECK(typeof(anchor_ordinal)='integer'
              AND anchor_ordinal BETWEEN 0 AND {CONVERSATION_PASSAGE_MAX_COUNT - 1}),
    anchor_message_revision_sha256 TEXT NOT NULL
        CHECK(typeof(anchor_message_revision_sha256)='text'
              AND length(anchor_message_revision_sha256)=64
              AND anchor_message_revision_sha256 NOT GLOB '*[^0-9a-f]*'),
    anchor_content_sha256 TEXT NOT NULL
        CHECK(typeof(anchor_content_sha256)='text'
              AND length(anchor_content_sha256)=64
              AND anchor_content_sha256 NOT GLOB '*[^0-9a-f]*'),
    anchor_locator_sha256 TEXT NOT NULL
        CHECK(typeof(anchor_locator_sha256)='text'
              AND length(anchor_locator_sha256)=64
              AND anchor_locator_sha256 NOT GLOB '*[^0-9a-f]*'),
    conversation_prefix_sha256 TEXT NOT NULL
        CHECK(typeof(conversation_prefix_sha256)='text'
              AND length(conversation_prefix_sha256)=64
              AND conversation_prefix_sha256 NOT GLOB '*[^0-9a-f]*'),
    UNIQUE(conversation_id,anchor_message_id),
    UNIQUE(conversation_id,anchor_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_conversation_passage_projection_status
    ON conversation_passage_projections(
        projection_status,incomplete_reason,conversation_id
    );
CREATE INDEX IF NOT EXISTS idx_conversation_passage_anchor_revision
    ON conversation_passages(anchor_message_revision_sha256,conversation_id,anchor_ordinal);

CREATE TRIGGER IF NOT EXISTS conversation_passage_projection_bi_validate
BEFORE INSERT ON conversation_passage_projections
WHEN NOT EXISTS (
    SELECT 1 FROM conversations source
     WHERE source.id=NEW.conversation_id
       AND friday_conversation_passage_projection_valid(
               NEW.conversation_id,NEW.indexed_message_count,
               NEW.indexed_through_message_id,
               NEW.indexed_conversation_revision_sha256,
               NEW.passage_set_sha256,NEW.passage_index_revision,
               NEW.projection_status,NEW.incomplete_reason,
               NEW.passage_count)=1
       AND {_PROJECTION_ROLLUP_VALID_SQL}
)
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_projection_invalid');
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_projection_bu_validate
BEFORE UPDATE ON conversation_passage_projections
WHEN NEW.conversation_id IS NOT OLD.conversation_id
  OR NOT EXISTS (
    SELECT 1 FROM conversations source
     WHERE source.id=NEW.conversation_id
       AND friday_conversation_passage_projection_valid(
               NEW.conversation_id,NEW.indexed_message_count,
               NEW.indexed_through_message_id,
               NEW.indexed_conversation_revision_sha256,
               NEW.passage_set_sha256,NEW.passage_index_revision,
               NEW.projection_status,NEW.incomplete_reason,
               NEW.passage_count)=1
       AND {_PROJECTION_ROLLUP_VALID_SQL}
)
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_projection_invalid');
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_projection_bu_identity_immutable
BEFORE UPDATE ON conversation_passage_projections
WHEN NEW.rowid IS NOT OLD.rowid
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_projection_identity_immutable');
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_projection_bi_identity_immutable
BEFORE INSERT ON conversation_passage_projections
WHEN EXISTS (
    SELECT 1 FROM conversation_passage_projections existing
     WHERE existing.conversation_id=NEW.conversation_id
)
    OR EXISTS (
        SELECT 1 FROM conversation_passage_projections existing
         WHERE existing.rowid=NEW.rowid
    )
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_projection_identity_immutable');
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_bi_validate
BEFORE INSERT ON conversation_passages
WHEN NOT EXISTS (
    SELECT 1
      FROM conversation_passage_projections projection
      JOIN conversations conversation
        ON conversation.id=projection.conversation_id
     JOIN messages source
        ON source.id=NEW.anchor_message_id
       AND source.conversation_id=conversation.id
       AND source.user_id=conversation.user_id
     WHERE projection.conversation_id=NEW.conversation_id
       AND NEW.anchor_ordinal=projection.passage_count
       AND NEW.anchor_ordinal=(
           SELECT COUNT(*)
             FROM messages predecessor
            WHERE predecessor.conversation_id=source.conversation_id
              AND predecessor.user_id=conversation.user_id
              AND predecessor.role IN ('user','assistant')
              AND predecessor.rowid<source.rowid
       )
       AND friday_conversation_passage_anchor_valid(
               source.id,source.conversation_id,source.user_id,
               conversation.user_id,source.role,source.content,source.created_at,
               NEW.conversation_id,NEW.anchor_message_id,NEW.anchor_ordinal,
               NEW.anchor_message_revision_sha256,NEW.anchor_content_sha256,
               NEW.anchor_locator_sha256)=1
       AND (
           (NEW.anchor_ordinal=0 AND NOT EXISTS (
                SELECT 1 FROM conversation_passages existing
                 WHERE existing.conversation_id=NEW.conversation_id
           ) AND NEW.conversation_prefix_sha256=
               friday_conversation_passage_prefix_sha256(
                   NULL,NEW.anchor_ordinal,NEW.anchor_message_revision_sha256))
           OR
           (NEW.anchor_ordinal>0 AND EXISTS (
                SELECT 1 FROM conversation_passages previous
                 WHERE previous.conversation_id=NEW.conversation_id
                   AND previous.anchor_ordinal=NEW.anchor_ordinal-1
                   AND NEW.conversation_prefix_sha256=
                       friday_conversation_passage_prefix_sha256(
                           previous.conversation_prefix_sha256,
                           NEW.anchor_ordinal,
                           NEW.anchor_message_revision_sha256)
           ))
       )
)
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_anchor_invalid');
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_bu_validate
BEFORE UPDATE ON conversation_passages
WHEN NEW.passage_rowid IS NOT OLD.passage_rowid
  OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.anchor_message_id IS NOT OLD.anchor_message_id
  OR NEW.anchor_ordinal IS NOT OLD.anchor_ordinal
  OR NOT EXISTS (
    SELECT 1
      FROM conversation_passage_projections projection
      JOIN conversations conversation
        ON conversation.id=projection.conversation_id
      JOIN messages source
        ON source.id=NEW.anchor_message_id
       AND source.conversation_id=conversation.id
       AND source.user_id=conversation.user_id
     WHERE projection.conversation_id=NEW.conversation_id
       AND NEW.anchor_ordinal=(
           SELECT COUNT(*)
             FROM messages predecessor
            WHERE predecessor.conversation_id=source.conversation_id
              AND predecessor.user_id=conversation.user_id
              AND predecessor.role IN ('user','assistant')
              AND predecessor.rowid<source.rowid
       )
       AND friday_conversation_passage_anchor_valid(
               source.id,source.conversation_id,source.user_id,
               conversation.user_id,source.role,source.content,source.created_at,
               NEW.conversation_id,NEW.anchor_message_id,NEW.anchor_ordinal,
               NEW.anchor_message_revision_sha256,NEW.anchor_content_sha256,
               NEW.anchor_locator_sha256)=1
       AND NEW.conversation_prefix_sha256=
           friday_conversation_passage_prefix_sha256(
               CASE WHEN NEW.anchor_ordinal=0 THEN NULL ELSE (
                   SELECT previous.conversation_prefix_sha256
                     FROM conversation_passages previous
                    WHERE previous.conversation_id=NEW.conversation_id
                      AND previous.anchor_ordinal=NEW.anchor_ordinal-1
               ) END,
               NEW.anchor_ordinal,NEW.anchor_message_revision_sha256)
)
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_anchor_invalid');
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_bi_identity_immutable
BEFORE INSERT ON conversation_passages
WHEN EXISTS (
        SELECT 1 FROM conversation_passages existing
         WHERE existing.passage_rowid=NEW.passage_rowid
    )
    OR EXISTS (
        SELECT 1 FROM conversation_passages existing
         WHERE existing.conversation_id=NEW.conversation_id
           AND existing.anchor_message_id=NEW.anchor_message_id
    )
    OR EXISTS (
        SELECT 1 FROM conversation_passages existing
         WHERE existing.conversation_id=NEW.conversation_id
           AND existing.anchor_ordinal=NEW.anchor_ordinal
    )
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_anchor_identity_immutable');
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_conversation_ai_seed
AFTER INSERT ON conversations
BEGIN
    INSERT INTO conversation_passage_projections(
        conversation_id,indexed_message_count,indexed_through_message_id,
        indexed_conversation_revision_sha256,passage_set_sha256,
        passage_index_revision,projection_status,incomplete_reason,
        passage_count,projected_at
    ) VALUES(
        NEW.id,0,NULL,NULL,NULL,'{CONVERSATION_PASSAGE_INDEX_REVISION}',
        'incomplete','backfill_pending',0,strftime('%Y-%m-%dT%H:%M:%SZ','now')
    );
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_message_rowid_bu_immutable
BEFORE UPDATE ON messages
WHEN NEW.rowid IS NOT OLD.rowid
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_message_rowid_immutable');
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_message_bu_identity_immutable
BEFORE UPDATE OF id,conversation_id,user_id ON messages
WHEN NEW.id IS NOT OLD.id
  OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_message_identity_immutable');
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_message_bi_identity_immutable
BEFORE INSERT ON messages
WHEN EXISTS (
        SELECT 1 FROM messages existing WHERE existing.id=NEW.id
    )
    OR EXISTS (
        SELECT 1 FROM messages existing WHERE existing.rowid=NEW.rowid
    )
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_message_identity_immutable');
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_message_rowid_ai_monotonic
AFTER INSERT ON messages
WHEN NEW.rowid<1 OR EXISTS (
    SELECT 1 FROM messages existing WHERE existing.rowid>NEW.rowid
)
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_message_rowid_nonmonotonic');
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_message_ai_invalidate
AFTER INSERT ON messages
WHEN NEW.role IN ('user','assistant')
BEGIN
    UPDATE conversation_passage_projections
       SET projection_status='incomplete',
           incomplete_reason=CASE
               WHEN projection_status='current' THEN 'source_changed'
               ELSE incomplete_reason
           END,
           projected_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
     WHERE conversation_id=NEW.conversation_id;
    INSERT INTO conversation_passage_projections(
        conversation_id,indexed_message_count,indexed_through_message_id,
        indexed_conversation_revision_sha256,passage_set_sha256,
        passage_index_revision,projection_status,incomplete_reason,
        passage_count,projected_at
    )
    SELECT NEW.conversation_id,0,NULL,NULL,NULL,
           '{CONVERSATION_PASSAGE_INDEX_REVISION}',
           'incomplete','source_changed',0,
           strftime('%Y-%m-%dT%H:%M:%SZ','now')
     WHERE NOT EXISTS (
         SELECT 1 FROM conversation_passage_projections projection
          WHERE projection.conversation_id=NEW.conversation_id
     );
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_message_au_reset
BEFORE UPDATE OF created_at ON messages
WHEN OLD.role IN ('user','assistant') OR NEW.role IN ('user','assistant')
BEGIN
    DELETE FROM conversation_passage_projections
     WHERE conversation_id IN (OLD.conversation_id,NEW.conversation_id);
    INSERT INTO conversation_passage_projections(
        conversation_id,indexed_message_count,indexed_through_message_id,
        indexed_conversation_revision_sha256,passage_set_sha256,
        passage_index_revision,projection_status,incomplete_reason,
        passage_count,projected_at
    )
    SELECT source.id,0,NULL,NULL,NULL,'{CONVERSATION_PASSAGE_INDEX_REVISION}',
           'incomplete','source_changed',0,
           strftime('%Y-%m-%dT%H:%M:%SZ','now')
      FROM conversations source
     WHERE source.id IN (OLD.conversation_id,NEW.conversation_id)
    ON CONFLICT(conversation_id) DO NOTHING;
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_conversation_bu_reset
BEFORE UPDATE OF id,user_id ON conversations
BEGIN
    DELETE FROM conversation_passage_projections
     WHERE conversation_id=OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_conversation_au_seed
AFTER UPDATE OF id,user_id ON conversations
BEGIN
    INSERT INTO conversation_passage_projections(
        conversation_id,indexed_message_count,indexed_through_message_id,
        indexed_conversation_revision_sha256,passage_set_sha256,
        passage_index_revision,projection_status,incomplete_reason,
        passage_count,projected_at
    ) VALUES(
        NEW.id,0,NULL,NULL,NULL,'{CONVERSATION_PASSAGE_INDEX_REVISION}',
        'incomplete','source_changed',0,strftime('%Y-%m-%dT%H:%M:%SZ','now')
    );
END;
"""


_SCHEMA_50_MESSAGE_SOURCE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_conversation_passage_message_source_order
    ON messages(user_id,conversation_id)
    WHERE role IN ('user','assistant');
"""


_SCHEMA_50_CONVERSATION_KEYSET_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_conversation_passage_conversation_owner_keyset
    ON conversations(user_id,id);
"""


_SCHEMA_50_CONVERSATION_BI_IDENTITY_SQL = """
CREATE TRIGGER IF NOT EXISTS conversation_passage_conversation_bi_identity
BEFORE INSERT ON conversations
WHEN friday_conversation_passage_conversation_identity_valid(NEW.id)<>1
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_conversation_identity_invalid');
END;
"""


_SCHEMA_50_MESSAGE_BI_IDENTITY_SQL = """
CREATE TRIGGER IF NOT EXISTS conversation_passage_message_bi_identity_immutable
BEFORE INSERT ON messages
WHEN EXISTS (
        SELECT 1 FROM messages existing WHERE existing.id=NEW.id
    )
    OR EXISTS (
        SELECT 1 FROM messages existing WHERE existing.rowid=NEW.rowid
    )
    OR (
        NEW.role IN ('user','assistant')
        AND NOT EXISTS (
            SELECT 1 FROM conversations conversation
             WHERE conversation.id=NEW.conversation_id
               AND friday_conversation_passage_source_descriptor_valid(
                       NEW.id,NEW.conversation_id,conversation.id,
                       NEW.user_id,conversation.user_id,NEW.role,
                       typeof(NEW.content),NEW.created_at)=1
               AND friday_conversation_passage_utf8_valid(
                       CAST(NEW.content AS BLOB))=1
        )
    )
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_message_identity_immutable');
END;
"""


_SCHEMA_50_MESSAGE_AU_RESET_SQL = f"""
CREATE TRIGGER IF NOT EXISTS conversation_passage_message_au_reset
BEFORE UPDATE OF created_at ON messages
WHEN OLD.role IN ('user','assistant') OR NEW.role IN ('user','assistant')
BEGIN
    SELECT CASE WHEN NEW.role IN ('user','assistant') AND NOT EXISTS (
        SELECT 1 FROM conversations conversation
         WHERE conversation.id=NEW.conversation_id
           AND friday_conversation_passage_source_descriptor_valid(
                   NEW.id,NEW.conversation_id,conversation.id,
                   NEW.user_id,conversation.user_id,NEW.role,
                   typeof(NEW.content),NEW.created_at)=1
           AND friday_conversation_passage_utf8_valid(
                   CAST(NEW.content AS BLOB))=1
    ) THEN RAISE(ABORT,'conversation_passage_message_identity_immutable') END;

    UPDATE conversation_passage_projections
       SET indexed_message_count=0,
           indexed_through_message_id=NULL,
           indexed_conversation_revision_sha256=NULL,
           passage_set_sha256=NULL,
           passage_index_revision='{CONVERSATION_PASSAGE_INDEX_REVISION}',
           projection_status='incomplete',
           incomplete_reason='source_changed',
           passage_count=0,
           projected_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
     WHERE conversation_id IN (OLD.conversation_id,NEW.conversation_id)
       AND incomplete_reason IS NOT 'source_unavailable';
END;
"""


_SCHEMA_50_CONVERSATION_BU_IDENTITY_SQL = """
CREATE TRIGGER IF NOT EXISTS conversation_passage_conversation_bu_reset
BEFORE UPDATE OF id,user_id ON conversations
WHEN NEW.id IS NOT OLD.id OR NEW.user_id IS NOT OLD.user_id
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_conversation_identity_immutable');
END;
"""


def _first_oversized_source_valid_sql(
    *,
    conversation_id_sql: str,
    principal_id_sql: str,
    force_source_order_index: bool,
) -> str:
    index_clause = (
        " INDEXED BY idx_conversation_passage_message_source_order" if force_source_order_index else ""
    )
    return f"""EXISTS (
        SELECT 1
          FROM messages first_source
         WHERE first_source.rowid=(
                   SELECT candidate.rowid
                     FROM messages candidate{index_clause}
                    WHERE candidate.user_id={principal_id_sql}
                      AND candidate.conversation_id={conversation_id_sql}
                      AND candidate.role IN ('user','assistant')
                    ORDER BY candidate.rowid ASC
                    LIMIT 1
               )
           AND first_source.user_id={principal_id_sql}
           AND first_source.conversation_id={conversation_id_sql}
           AND first_source.role IN ('user','assistant')
           AND friday_conversation_passage_source_descriptor_valid(
                   first_source.id,
                   first_source.conversation_id,
                   {conversation_id_sql},
                   first_source.user_id,
                   {principal_id_sql},
                   first_source.role,
                   typeof(first_source.content),
                   first_source.created_at
               )=1
           AND friday_conversation_passage_utf8_valid(
                   CAST(first_source.content AS BLOB))=1
           AND length(CAST(first_source.content AS BLOB))>
                   {CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES}
           AND NOT EXISTS (
               SELECT 1
                 FROM messages predecessor{index_clause}
                WHERE predecessor.user_id={principal_id_sql}
                  AND predecessor.conversation_id={conversation_id_sql}
                  AND predecessor.role IN ('user','assistant')
                  AND predecessor.rowid<first_source.rowid
                LIMIT 1
           )
    )"""


_SCHEMA_50_SOURCE_UNAVAILABLE_UPDATE_VALID_SQL = _first_oversized_source_valid_sql(
    conversation_id_sql="conversation.id",
    principal_id_sql="conversation.user_id",
    force_source_order_index=True,
)


_SCHEMA_50_MESSAGE_AI_INVALIDATE_SQL = f"""
CREATE TRIGGER IF NOT EXISTS conversation_passage_message_ai_invalidate
AFTER INSERT ON messages
WHEN NEW.role IN ('user','assistant')
BEGIN
    UPDATE conversation_passage_projections
       SET projection_status='incomplete',
           incomplete_reason=CASE
               WHEN projection_status='current' THEN 'source_changed'
               ELSE incomplete_reason
           END,
           projected_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
     WHERE conversation_id=NEW.conversation_id;
    INSERT INTO conversation_passage_projections(
        conversation_id,indexed_message_count,indexed_through_message_id,
        indexed_conversation_revision_sha256,passage_set_sha256,
        passage_index_revision,projection_status,incomplete_reason,
        passage_count,projected_at
    )
    SELECT NEW.conversation_id,0,NULL,NULL,NULL,
           '{CONVERSATION_PASSAGE_INDEX_REVISION}',
           'incomplete','source_changed',0,
           strftime('%Y-%m-%dT%H:%M:%SZ','now')
     WHERE NOT EXISTS (
         SELECT 1 FROM conversation_passage_projections projection
          WHERE projection.conversation_id=NEW.conversation_id
     );

    -- Admission has already authenticated the complete TEXT body. Settle an
    -- exact oversized first source here so the bounded writer never has to
    -- read or validate more than its fixed per-tick body budget.
    UPDATE conversation_passage_projections
       SET indexed_message_count=0,
           indexed_through_message_id=NULL,
           indexed_conversation_revision_sha256=NULL,
           passage_set_sha256=NULL,
           projection_status='incomplete',
           incomplete_reason='source_unavailable',
           passage_count=0,
           projected_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
     WHERE conversation_id=NEW.conversation_id
       AND projection_status='incomplete'
       AND incomplete_reason IN ('backfill_pending','source_changed')
       AND passage_count=0
       AND length(CAST(NEW.content AS BLOB))>
               {CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES}
       AND EXISTS (
           SELECT 1
             FROM conversations conversation
            WHERE conversation.id=NEW.conversation_id
              AND conversation.user_id=NEW.user_id
              AND NEW.rowid=(
                  SELECT candidate.rowid
                    FROM messages candidate
                         INDEXED BY idx_conversation_passage_message_source_order
                   WHERE candidate.user_id=conversation.user_id
                     AND candidate.conversation_id=conversation.id
                     AND candidate.role IN ('user','assistant')
                   ORDER BY candidate.rowid ASC
                   LIMIT 1
              )
       );
END;
"""


_SCHEMA_50_PROJECTION_BI_VALIDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS conversation_passage_projection_bi_validate
BEFORE INSERT ON conversation_passage_projections
WHEN NOT EXISTS (
    SELECT 1
      FROM conversations conversation
     WHERE conversation.id=NEW.conversation_id
       AND friday_conversation_passage_projection_valid(
               NEW.conversation_id,NEW.indexed_message_count,
               NEW.indexed_through_message_id,
               NEW.indexed_conversation_revision_sha256,
               NEW.passage_set_sha256,NEW.passage_index_revision,
               NEW.projection_status,NEW.incomplete_reason,
               NEW.passage_count)=1
       AND NEW.passage_count=0
       AND NEW.incomplete_reason IS NOT 'source_unavailable'
       AND NOT EXISTS (
               SELECT 1 FROM conversation_passages passage
                WHERE passage.conversation_id=NEW.conversation_id
                LIMIT 1
           )
       AND (
           NEW.projection_status='incomplete'
           OR (
               NEW.projection_status='current'
               AND NOT EXISTS (
                   SELECT 1
                     FROM messages AS source
                          INDEXED BY idx_conversation_passage_message_source_order
                    WHERE source.user_id=conversation.user_id
                      AND source.conversation_id=conversation.id
                      AND source.role IN ('user','assistant')
                    LIMIT 1
               )
           )
       )
)
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_projection_invalid');
END;
"""


_SCHEMA_50_PROJECTION_BU_VALIDATE_SQL = f"""
CREATE TRIGGER IF NOT EXISTS conversation_passage_projection_bu_validate
BEFORE UPDATE ON conversation_passage_projections
WHEN NEW.conversation_id IS NOT OLD.conversation_id
  OR NOT EXISTS (
    SELECT 1
      FROM conversations conversation
     WHERE conversation.id=NEW.conversation_id
       AND friday_conversation_passage_projection_valid(
               NEW.conversation_id,NEW.indexed_message_count,
               NEW.indexed_through_message_id,
               NEW.indexed_conversation_revision_sha256,
               NEW.passage_set_sha256,NEW.passage_index_revision,
               NEW.projection_status,NEW.incomplete_reason,
               NEW.passage_count)=1
       AND (
           -- A timestamp-only refresh is body-free and does not republish state.
           (
               NEW.indexed_message_count IS OLD.indexed_message_count
               AND NEW.indexed_through_message_id IS OLD.indexed_through_message_id
               AND NEW.indexed_conversation_revision_sha256
                       IS OLD.indexed_conversation_revision_sha256
               AND NEW.passage_set_sha256 IS OLD.passage_set_sha256
               AND NEW.passage_index_revision IS OLD.passage_index_revision
               AND NEW.projection_status IS OLD.projection_status
               AND NEW.incomplete_reason IS OLD.incomplete_reason
               AND NEW.passage_count IS OLD.passage_count
           )
           OR
           -- A derivative-only reset is one atomic parent transition.  Its
           -- AFTER trigger removes every old child before this statement can
           -- commit, leaving an exact retryable source_changed/0 contour.
           (
               OLD.incomplete_reason IS NOT 'source_unavailable'
               AND NEW.indexed_message_count=0
               AND NEW.indexed_through_message_id IS NULL
               AND NEW.indexed_conversation_revision_sha256 IS NULL
               AND NEW.passage_set_sha256 IS NULL
               AND NEW.passage_index_revision IS OLD.passage_index_revision
               AND NEW.projection_status='incomplete'
               AND NEW.incomplete_reason='source_changed'
               AND NEW.passage_count=0
           )
           OR
           -- An eligible append invalidates a previously exact CURRENT tail.
           (
               OLD.projection_status='current'
               AND NEW.projection_status='incomplete'
               AND NEW.incomplete_reason='source_changed'
               AND NEW.indexed_message_count=OLD.indexed_message_count
               AND NEW.indexed_through_message_id IS OLD.indexed_through_message_id
               AND NEW.indexed_conversation_revision_sha256
                       IS OLD.indexed_conversation_revision_sha256
               AND NEW.passage_set_sha256 IS OLD.passage_set_sha256
               AND NEW.passage_index_revision IS OLD.passage_index_revision
               AND NEW.passage_count=OLD.passage_count
               AND (
                   (OLD.passage_count=0 AND EXISTS (
                       SELECT 1
                         FROM messages AS next_source
                              INDEXED BY idx_conversation_passage_message_source_order
                        WHERE next_source.user_id=conversation.user_id
                          AND next_source.conversation_id=conversation.id
                          AND next_source.role IN ('user','assistant')
                        LIMIT 1
                   ))
                   OR
                   (OLD.passage_count>0 AND EXISTS (
                       SELECT 1
                         FROM conversation_passages tail
                         JOIN messages tail_source
                           ON tail_source.id=tail.anchor_message_id
                          AND tail_source.user_id=conversation.user_id
                          AND tail_source.conversation_id=conversation.id
                          AND tail_source.role IN ('user','assistant')
                        WHERE tail.conversation_id=conversation.id
                          AND tail.anchor_ordinal=OLD.passage_count-1
                          AND tail.anchor_message_id=OLD.indexed_through_message_id
                          AND tail.conversation_prefix_sha256=
                                OLD.indexed_conversation_revision_sha256
                          AND EXISTS (
                              SELECT 1
                                FROM messages AS next_source
                                     INDEXED BY idx_conversation_passage_message_source_order
                               WHERE next_source.user_id=conversation.user_id
                                 AND next_source.conversation_id=conversation.id
                                 AND next_source.role IN ('user','assistant')
                                 AND next_source.rowid>tail_source.rowid
                               LIMIT 1
                          )
                   ))
               )
           )
           OR
           -- Only this transition may inspect a complete oversized body. A
           -- lazy CASE prevents ordinary writer CAS updates from evaluating
           -- strict UTF-8 outside their body-work accounting.
           CASE WHEN
               OLD.projection_status='incomplete'
               AND OLD.incomplete_reason IN ('backfill_pending','source_changed')
               AND OLD.passage_count=0
               AND NEW.projection_status='incomplete'
               AND NEW.incomplete_reason='source_unavailable'
               AND NEW.indexed_message_count=0
               AND NEW.indexed_through_message_id IS NULL
               AND NEW.indexed_conversation_revision_sha256 IS NULL
               AND NEW.passage_set_sha256 IS NULL
               AND NEW.passage_index_revision IS OLD.passage_index_revision
               AND NEW.passage_count=0
               AND NOT EXISTS (
                   SELECT 1 FROM conversation_passages passage
                    WHERE passage.conversation_id=conversation.id
                    LIMIT 1
               )
           THEN CASE WHEN {_SCHEMA_50_SOURCE_UNAVAILABLE_UPDATE_VALID_SQL}
                     THEN 1 ELSE 0 END
           ELSE 0 END=1
           OR
           -- Completion is a one-parent CAS over the already stored tail.
           (
               OLD.projection_status='incomplete'
               AND OLD.incomplete_reason IN ('backfill_pending','source_changed')
               AND NEW.projection_status='current'
               AND NEW.incomplete_reason IS NULL
               AND NEW.indexed_message_count=OLD.indexed_message_count
               AND NEW.indexed_through_message_id IS OLD.indexed_through_message_id
               AND NEW.passage_index_revision IS OLD.passage_index_revision
               AND NEW.passage_count=OLD.passage_count
               AND NOT EXISTS (
                   SELECT 1 FROM conversation_passages suffix
                    WHERE suffix.conversation_id=conversation.id
                      AND suffix.anchor_ordinal>=OLD.passage_count
                    LIMIT 1
               )
               AND (
                   (OLD.passage_count=0
                    AND NEW.indexed_conversation_revision_sha256=
                            '{CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256}'
                    AND NEW.passage_set_sha256=
                            '{CONVERSATION_PASSAGE_EMPTY_SET_SHA256}'
                    AND NOT EXISTS (
                        SELECT 1
                          FROM messages AS next_source
                               INDEXED BY idx_conversation_passage_message_source_order
                         WHERE next_source.user_id=conversation.user_id
                           AND next_source.conversation_id=conversation.id
                           AND next_source.role IN ('user','assistant')
                         LIMIT 1
                    ))
                   OR
                   (OLD.passage_count>0
                    AND NEW.indexed_conversation_revision_sha256=
                            OLD.indexed_conversation_revision_sha256
                    AND NEW.passage_set_sha256=OLD.passage_set_sha256
                    AND EXISTS (
                        SELECT 1
                          FROM conversation_passages tail
                          JOIN messages tail_source
                            ON tail_source.id=tail.anchor_message_id
                           AND tail_source.user_id=conversation.user_id
                           AND tail_source.conversation_id=conversation.id
                           AND tail_source.role IN ('user','assistant')
                         WHERE tail.conversation_id=conversation.id
                           AND tail.anchor_ordinal=OLD.passage_count-1
                           AND tail.anchor_message_id=OLD.indexed_through_message_id
                           AND tail.conversation_prefix_sha256=
                                 OLD.indexed_conversation_revision_sha256
                           AND NOT EXISTS (
                               SELECT 1
                                 FROM messages AS next_source
                                      INDEXED BY idx_conversation_passage_message_source_order
                                WHERE next_source.user_id=conversation.user_id
                                  AND next_source.conversation_id=conversation.id
                                  AND next_source.role IN ('user','assistant')
                                  AND next_source.rowid>tail_source.rowid
                                LIMIT 1
                           )
                    ))
               )
           )
           OR
           -- Exactly one newly admitted child advances both digest chains.
           (
               OLD.projection_status='incomplete'
               AND OLD.incomplete_reason IN ('backfill_pending','source_changed')
               AND OLD.passage_count<{CONVERSATION_PASSAGE_MAX_COUNT}
               AND NEW.indexed_message_count=OLD.indexed_message_count+1
               AND NEW.passage_count=OLD.passage_count+1
               AND NEW.passage_index_revision IS OLD.passage_index_revision
               AND NOT EXISTS (
                   SELECT 1 FROM conversation_passages suffix
                    WHERE suffix.conversation_id=conversation.id
                      AND suffix.anchor_ordinal>=NEW.passage_count
                    LIMIT 1
               )
               AND EXISTS (
                   SELECT 1
                     FROM conversation_passages child
                     JOIN messages source
                       ON source.id=child.anchor_message_id
                      AND source.user_id=conversation.user_id
                      AND source.conversation_id=conversation.id
                      AND source.role IN ('user','assistant')
                    WHERE child.conversation_id=conversation.id
                      AND child.anchor_ordinal=OLD.passage_count
                      AND NEW.indexed_through_message_id=child.anchor_message_id
                      AND NEW.indexed_conversation_revision_sha256=
                            child.conversation_prefix_sha256
                      AND child.conversation_prefix_sha256=
                            friday_conversation_passage_prefix_sha256(
                                CASE WHEN OLD.passage_count=0 THEN NULL
                                     ELSE OLD.indexed_conversation_revision_sha256 END,
                                OLD.passage_count,
                                child.anchor_message_revision_sha256)
                      AND NEW.passage_set_sha256=
                            friday_conversation_passage_set_extend_sha256(
                                CASE WHEN OLD.passage_count=0
                                     THEN '{CONVERSATION_PASSAGE_EMPTY_SET_SHA256}'
                                     ELSE OLD.passage_set_sha256 END,
                                child.anchor_ordinal,
                                child.anchor_message_id,
                                child.anchor_message_revision_sha256,
                                child.anchor_content_sha256,
                                child.anchor_locator_sha256,
                                child.conversation_prefix_sha256)
                      AND (
                          (OLD.passage_count=0 AND NOT EXISTS (
                              SELECT 1
                                FROM messages AS predecessor
                                     INDEXED BY idx_conversation_passage_message_source_order
                               WHERE predecessor.user_id=conversation.user_id
                                 AND predecessor.conversation_id=conversation.id
                                 AND predecessor.role IN ('user','assistant')
                                 AND predecessor.rowid<source.rowid
                               LIMIT 1
                          ))
                          OR
                          (OLD.passage_count>0 AND EXISTS (
                              SELECT 1
                                FROM conversation_passages previous
                                JOIN messages previous_source
                                  ON previous_source.id=previous.anchor_message_id
                                 AND previous_source.user_id=conversation.user_id
                                 AND previous_source.conversation_id=conversation.id
                                 AND previous_source.role IN ('user','assistant')
                               WHERE previous.conversation_id=conversation.id
                                 AND previous.anchor_ordinal=OLD.passage_count-1
                                 AND previous.anchor_message_id=
                                       OLD.indexed_through_message_id
                                 AND previous.conversation_prefix_sha256=
                                       OLD.indexed_conversation_revision_sha256
                                 AND previous_source.rowid<source.rowid
                                 AND NOT EXISTS (
                                     SELECT 1
                                       FROM messages AS skipped
                                            INDEXED BY idx_conversation_passage_message_source_order
                                      WHERE skipped.user_id=conversation.user_id
                                        AND skipped.conversation_id=conversation.id
                                        AND skipped.role IN ('user','assistant')
                                        AND skipped.rowid>previous_source.rowid
                                        AND skipped.rowid<source.rowid
                                      LIMIT 1
                                 )
                          ))
                      )
                      AND (
                          (NEW.projection_status='current'
                           AND NEW.incomplete_reason IS NULL
                           AND NOT EXISTS (
                               SELECT 1
                                 FROM messages AS next_source
                                      INDEXED BY idx_conversation_passage_message_source_order
                                WHERE next_source.user_id=conversation.user_id
                                  AND next_source.conversation_id=conversation.id
                                  AND next_source.role IN ('user','assistant')
                                  AND next_source.rowid>source.rowid
                                LIMIT 1
                           ))
                          OR
                          (NEW.projection_status='incomplete'
                           AND NEW.incomplete_reason=OLD.incomplete_reason
                           AND EXISTS (
                               SELECT 1
                                 FROM messages AS next_source
                                      INDEXED BY idx_conversation_passage_message_source_order
                                WHERE next_source.user_id=conversation.user_id
                                  AND next_source.conversation_id=conversation.id
                                  AND next_source.role IN ('user','assistant')
                                  AND next_source.rowid>source.rowid
                                LIMIT 1
                           ))
                      )
               )
           )
       )
)
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_projection_invalid');
END;
"""


_SCHEMA_50_PROJECTION_BD_VALIDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS conversation_passage_projection_bd_validate
BEFORE DELETE ON conversation_passage_projections
WHEN EXISTS (
    SELECT 1 FROM conversations conversation
     WHERE conversation.id=OLD.conversation_id
)
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_projection_delete_invalid');
END;
"""


_SCHEMA_50_PROJECTION_AU_RESET_CHILDREN_SQL = f"""
CREATE TRIGGER IF NOT EXISTS conversation_passage_projection_au_reset_children
AFTER UPDATE ON conversation_passage_projections
WHEN NEW.indexed_message_count=0
 AND NEW.indexed_through_message_id IS NULL
 AND NEW.indexed_conversation_revision_sha256 IS NULL
 AND NEW.passage_set_sha256 IS NULL
 AND NEW.projection_status='incomplete'
 AND NEW.incomplete_reason='source_changed'
 AND NEW.passage_count=0
BEGIN
    DELETE FROM conversation_passages
     WHERE conversation_id=NEW.conversation_id;

    -- Every authorized derivative reset closes an exact oversized first source
    -- after its old children are gone. The bounded writer never executes this
    -- reset transition and therefore never inherits the strict body proof.
    UPDATE conversation_passage_projections
       SET incomplete_reason='source_unavailable',
           projected_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
     WHERE conversation_id=NEW.conversation_id
       AND projection_status='incomplete'
       AND incomplete_reason='source_changed'
       AND passage_count=0
       AND EXISTS (
           SELECT 1
             FROM conversations conversation
            WHERE conversation.id=NEW.conversation_id
              AND {_SCHEMA_50_SOURCE_UNAVAILABLE_UPDATE_VALID_SQL}
       );
END;
"""


_SCHEMA_50_PASSAGE_BI_VALIDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS conversation_passage_bi_validate
BEFORE INSERT ON conversation_passages
WHEN NOT EXISTS (
    SELECT 1
      FROM conversation_passage_projections projection
      JOIN conversations conversation
        ON conversation.id=projection.conversation_id
      JOIN messages source
        ON source.id=NEW.anchor_message_id
       AND source.conversation_id=conversation.id
       AND source.user_id=conversation.user_id
       AND source.role IN ('user','assistant')
     WHERE projection.conversation_id=NEW.conversation_id
       AND projection.projection_status='incomplete'
       AND projection.incomplete_reason IN ('backfill_pending','source_changed')
       AND NEW.anchor_ordinal=projection.passage_count
       AND NOT EXISTS (
               SELECT 1 FROM conversation_passages suffix
                WHERE suffix.conversation_id=NEW.conversation_id
                  AND suffix.anchor_ordinal>=projection.passage_count
                LIMIT 1
           )
       AND friday_conversation_passage_utf8_valid(
               CAST(source.content AS BLOB))=1
       AND friday_conversation_passage_anchor_valid(
               source.id,source.conversation_id,source.user_id,
               conversation.user_id,source.role,
               CAST(source.content AS BLOB),source.created_at,
               NEW.conversation_id,NEW.anchor_message_id,NEW.anchor_ordinal,
               NEW.anchor_message_revision_sha256,NEW.anchor_content_sha256,
               NEW.anchor_locator_sha256)=1
       AND friday_conversation_passage_source_descriptor_valid(
               source.id,source.conversation_id,conversation.id,
               source.user_id,conversation.user_id,source.role,
               typeof(source.content),source.created_at)=1
       AND NEW.conversation_prefix_sha256=
               friday_conversation_passage_prefix_sha256(
                   CASE WHEN projection.passage_count=0 THEN NULL
                        ELSE projection.indexed_conversation_revision_sha256 END,
                   NEW.anchor_ordinal,
                   NEW.anchor_message_revision_sha256)
       AND (
           (projection.passage_count=0 AND NOT EXISTS (
               SELECT 1
                 FROM messages AS predecessor
                      INDEXED BY idx_conversation_passage_message_source_order
                WHERE predecessor.user_id=conversation.user_id
                  AND predecessor.conversation_id=conversation.id
                  AND predecessor.role IN ('user','assistant')
                  AND predecessor.rowid<source.rowid
                LIMIT 1
           ))
           OR
           (projection.passage_count>0 AND EXISTS (
               SELECT 1
                 FROM conversation_passages previous
                 JOIN messages previous_source
                   ON previous_source.id=previous.anchor_message_id
                  AND previous_source.user_id=conversation.user_id
                  AND previous_source.conversation_id=conversation.id
                  AND previous_source.role IN ('user','assistant')
                WHERE previous.conversation_id=NEW.conversation_id
                  AND previous.anchor_ordinal=projection.passage_count-1
                  AND previous.anchor_message_id=projection.indexed_through_message_id
                  AND previous.conversation_prefix_sha256=
                        projection.indexed_conversation_revision_sha256
                  AND previous_source.rowid<source.rowid
                  AND NOT EXISTS (
                      SELECT 1
                        FROM messages AS skipped
                             INDEXED BY idx_conversation_passage_message_source_order
                       WHERE skipped.user_id=conversation.user_id
                         AND skipped.conversation_id=conversation.id
                         AND skipped.role IN ('user','assistant')
                         AND skipped.rowid>previous_source.rowid
                         AND skipped.rowid<source.rowid
                       LIMIT 1
                  )
           ))
       )
)
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_anchor_invalid');
END;
"""


_SCHEMA_50_NEW_CHILD_HAS_SUCCESSOR_SQL = """EXISTS (
    SELECT 1
      FROM messages source
      JOIN conversations conversation
        ON conversation.id=NEW.conversation_id
       AND conversation.user_id=source.user_id
     WHERE source.id=NEW.anchor_message_id
       AND source.conversation_id=NEW.conversation_id
       AND source.role IN ('user','assistant')
       AND EXISTS (
           SELECT 1
             FROM messages successor
                  INDEXED BY idx_conversation_passage_message_source_order
            WHERE successor.user_id=conversation.user_id
              AND successor.conversation_id=NEW.conversation_id
              AND successor.role IN ('user','assistant')
              AND successor.rowid>source.rowid
            LIMIT 1
       )
)"""


_SCHEMA_50_PASSAGE_AI_PARENT_CAS_SQL = f"""
CREATE TRIGGER IF NOT EXISTS conversation_passage_ai_parent_cas
AFTER INSERT ON conversation_passages
BEGIN
    UPDATE conversation_passage_projections
       SET indexed_message_count=NEW.anchor_ordinal+1,
           indexed_through_message_id=NEW.anchor_message_id,
           indexed_conversation_revision_sha256=NEW.conversation_prefix_sha256,
           passage_set_sha256=friday_conversation_passage_set_extend_sha256(
               CASE WHEN passage_count=0
                    THEN '{CONVERSATION_PASSAGE_EMPTY_SET_SHA256}'
                    ELSE passage_set_sha256 END,
               NEW.anchor_ordinal,
               NEW.anchor_message_id,
               NEW.anchor_message_revision_sha256,
               NEW.anchor_content_sha256,
               NEW.anchor_locator_sha256,
               NEW.conversation_prefix_sha256
           ),
           projection_status=CASE
               WHEN {_SCHEMA_50_NEW_CHILD_HAS_SUCCESSOR_SQL}
               THEN 'incomplete' ELSE 'current' END,
           incomplete_reason=CASE
               WHEN {_SCHEMA_50_NEW_CHILD_HAS_SUCCESSOR_SQL}
               THEN incomplete_reason ELSE NULL END,
           passage_count=NEW.anchor_ordinal+1,
           projected_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
     WHERE conversation_id=NEW.conversation_id
       AND projection_status='incomplete'
       AND incomplete_reason IN ('backfill_pending','source_changed')
       AND passage_count=NEW.anchor_ordinal;

    SELECT CASE WHEN NOT EXISTS (
        SELECT 1
          FROM conversation_passage_projections projection
         WHERE projection.conversation_id=NEW.conversation_id
           AND projection.indexed_message_count=NEW.anchor_ordinal+1
           AND projection.indexed_through_message_id=NEW.anchor_message_id
           AND projection.indexed_conversation_revision_sha256=
                   NEW.conversation_prefix_sha256
           AND projection.passage_count=NEW.anchor_ordinal+1
    ) THEN RAISE(ABORT,'conversation_passage_projection_invalid') END;
END;
"""


_SCHEMA_50_PASSAGE_BU_VALIDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS conversation_passage_bu_validate
BEFORE UPDATE ON conversation_passages
WHEN NEW.passage_rowid IS NOT OLD.passage_rowid
  OR NEW.conversation_id IS NOT OLD.conversation_id
  OR NEW.anchor_message_id IS NOT OLD.anchor_message_id
  OR NEW.anchor_ordinal IS NOT OLD.anchor_ordinal
  OR NEW.anchor_message_revision_sha256 IS NOT OLD.anchor_message_revision_sha256
  OR NEW.anchor_content_sha256 IS NOT OLD.anchor_content_sha256
  OR NEW.anchor_locator_sha256 IS NOT OLD.anchor_locator_sha256
  OR NEW.conversation_prefix_sha256 IS NOT OLD.conversation_prefix_sha256
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_anchor_invalid');
END;
"""


_SCHEMA_50_PASSAGE_BD_VALIDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS conversation_passage_bd_validate
BEFORE DELETE ON conversation_passages
WHEN EXISTS (
    SELECT 1 FROM conversation_passage_projections projection
     WHERE projection.conversation_id=OLD.conversation_id
       AND NOT (
           projection.indexed_message_count=0
           AND projection.indexed_through_message_id IS NULL
           AND projection.indexed_conversation_revision_sha256 IS NULL
           AND projection.passage_set_sha256 IS NULL
           AND projection.projection_status='incomplete'
           AND projection.incomplete_reason='source_changed'
           AND projection.passage_count=0
       )
)
BEGIN
    SELECT RAISE(ABORT,'conversation_passage_anchor_invalid');
END;
"""


def _replace_schema_trigger(script: str, name: str, replacement: str) -> str:
    marker = f"CREATE TRIGGER IF NOT EXISTS {name}"
    start = script.index(marker)
    end = script.index("\nEND;", start) + len("\nEND;")
    return f"{script[:start]}{replacement.strip()}{script[end:]}"


def _schema_50_conversation_passage_sql() -> str:
    anchor_index = """CREATE INDEX IF NOT EXISTS idx_conversation_passage_anchor_revision
    ON conversation_passages(anchor_message_revision_sha256,conversation_id,anchor_ordinal);
"""
    script = _RELEASED_CONVERSATION_PASSAGE_SCHEMA_V49.replace(
        anchor_index,
        (
            f"{anchor_index}\n{_SCHEMA_50_MESSAGE_SOURCE_INDEX_SQL.strip()}\n"
            f"{_SCHEMA_50_CONVERSATION_KEYSET_INDEX_SQL.strip()}\n"
        ),
        1,
    )
    for name, replacement in (
        ("conversation_passage_projection_bi_validate", _SCHEMA_50_PROJECTION_BI_VALIDATE_SQL),
        ("conversation_passage_projection_bu_validate", _SCHEMA_50_PROJECTION_BU_VALIDATE_SQL),
        ("conversation_passage_bi_validate", _SCHEMA_50_PASSAGE_BI_VALIDATE_SQL),
        ("conversation_passage_bu_validate", _SCHEMA_50_PASSAGE_BU_VALIDATE_SQL),
        (
            "conversation_passage_message_bi_identity_immutable",
            _SCHEMA_50_MESSAGE_BI_IDENTITY_SQL,
        ),
        (
            "conversation_passage_message_ai_invalidate",
            _SCHEMA_50_MESSAGE_AI_INVALIDATE_SQL,
        ),
        ("conversation_passage_message_au_reset", _SCHEMA_50_MESSAGE_AU_RESET_SQL),
        (
            "conversation_passage_conversation_bu_reset",
            _SCHEMA_50_CONVERSATION_BU_IDENTITY_SQL,
        ),
    ):
        script = _replace_schema_trigger(script, name, replacement)
    parent_update_guard = _SCHEMA_50_PROJECTION_BU_VALIDATE_SQL.strip()
    script = script.replace(
        parent_update_guard,
        (
            f"{parent_update_guard}\n\n{_SCHEMA_50_PROJECTION_BD_VALIDATE_SQL.strip()}\n\n"
            f"{_SCHEMA_50_PROJECTION_AU_RESET_CHILDREN_SQL.strip()}"
        ),
        1,
    )
    passage_insert_guard = _SCHEMA_50_PASSAGE_BI_VALIDATE_SQL.strip()
    script = script.replace(
        passage_insert_guard,
        f"{passage_insert_guard}\n\n{_SCHEMA_50_PASSAGE_AI_PARENT_CAS_SQL.strip()}",
        1,
    )
    passage_update_guard = _SCHEMA_50_PASSAGE_BU_VALIDATE_SQL.strip()
    script = script.replace(
        passage_update_guard,
        f"{passage_update_guard}\n\n{_SCHEMA_50_PASSAGE_BD_VALIDATE_SQL.strip()}",
        1,
    )
    conversation_seed = "CREATE TRIGGER IF NOT EXISTS conversation_passage_conversation_ai_seed"
    return script.replace(
        conversation_seed,
        f"{_SCHEMA_50_CONVERSATION_BI_IDENTITY_SQL.strip()}\n\n{conversation_seed}",
        1,
    )


CONVERSATION_PASSAGE_SCHEMA = _schema_50_conversation_passage_sql()


# The view is the only non-authoritative SQL object which exposes message text.
# FTS5 stores only its token derivative; ordinary sidecar tables remain body-free.
CONVERSATION_PASSAGE_FTS_SCHEMA = """
CREATE VIEW IF NOT EXISTS conversation_passage_search_content AS
SELECT passage.passage_rowid AS passage_rowid,
       source.content AS content
  FROM conversation_passages passage
  JOIN conversation_passage_projections projection
    ON projection.conversation_id=passage.conversation_id
  JOIN conversations conversation
    ON conversation.id=projection.conversation_id
  JOIN messages source
    ON source.id=passage.anchor_message_id
   AND source.conversation_id=conversation.id
   AND source.user_id=conversation.user_id
 WHERE friday_conversation_passage_anchor_valid(
           source.id,source.conversation_id,source.user_id,
           conversation.user_id,source.role,source.content,source.created_at,
           passage.conversation_id,passage.anchor_message_id,
           passage.anchor_ordinal,passage.anchor_message_revision_sha256,
           passage.anchor_content_sha256,passage.anchor_locator_sha256)=1;

CREATE VIRTUAL TABLE IF NOT EXISTS conversation_passages_fts USING fts5(
    content,
    content='conversation_passage_search_content',
    content_rowid='passage_rowid',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS conversation_passage_fts_ai
AFTER INSERT ON conversation_passages
BEGIN
    INSERT INTO conversation_passages_fts(rowid,content)
    SELECT NEW.passage_rowid,source.content
      FROM messages source
     WHERE source.id=NEW.anchor_message_id;
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_fts_ad
AFTER DELETE ON conversation_passages
BEGIN
    INSERT INTO conversation_passages_fts(
        conversation_passages_fts,rowid,content
    )
    SELECT 'delete',OLD.passage_rowid,source.content
      FROM messages source
     WHERE source.id=OLD.anchor_message_id;
END;

CREATE TRIGGER IF NOT EXISTS conversation_passage_fts_au
AFTER UPDATE ON conversation_passages
BEGIN
    INSERT INTO conversation_passages_fts(
        conversation_passages_fts,rowid,content
    )
    SELECT 'delete',OLD.passage_rowid,source.content
      FROM messages source
     WHERE source.id=OLD.anchor_message_id;
    INSERT INTO conversation_passages_fts(rowid,content)
    SELECT NEW.passage_rowid,source.content
      FROM messages source
     WHERE source.id=NEW.anchor_message_id;
END;
"""


def _valid_digest(value: object) -> str | None:
    return value if type(value) is str and _DIGEST.fullmatch(value) is not None else None


def _safe_text(value: object, *, maximum_bytes: int = 200) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("conversation-passage identity is invalid")
    encoded = value.encode("utf-8", errors="strict")
    if len(encoded) > maximum_bytes or any(ord(character) < 32 for character in value):
        raise ValueError("conversation-passage identity is invalid")
    return value


def _canonical_sha256(payload: dict[str, object]) -> str:
    material = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(material).hexdigest()


def _canonical_source_utc(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > 64:
        raise ValueError("conversation-passage source timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("conversation-passage source timestamp is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("conversation-passage source timestamp is invalid")
    if parsed.astimezone(UTC).isoformat() != value:
        raise ValueError("conversation-passage source timestamp is invalid")
    return value


def _conversation_identity_valid(value: object) -> int:
    return int(type(value) is str and _CONVERSATION_ID.fullmatch(value) is not None)


def _source_descriptor_valid(*values: object) -> int:
    """Validate one body-free canonical eligible-source descriptor."""

    try:
        if len(values) != 8:
            return 0
        (
            source_id,
            source_conversation_id,
            authoritative_conversation_id,
            source_principal_id,
            conversation_principal_id,
            role,
            content_storage_class,
            created_at,
        ) = values
        return int(
            type(source_id) is str
            and _MESSAGE_ID.fullmatch(source_id) is not None
            and type(source_conversation_id) is str
            and _CONVERSATION_ID.fullmatch(source_conversation_id) is not None
            and source_conversation_id == authoritative_conversation_id
            and _safe_text(source_principal_id) == _safe_text(conversation_principal_id)
            and role in {"user", "assistant"}
            and content_storage_class == "text"
            and _canonical_source_utc(created_at) == created_at
        )
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return 0


def _utf8_valid(value: object) -> int:
    """Validate a SQLite TEXT body through its lossless BLOB representation."""

    if type(value) is not bytes:
        return 0
    try:
        value.decode("utf-8", errors="strict")
    except UnicodeError:
        return 0
    return 1


def conversation_passage_message_revision_sha256(
    *,
    message_id: str,
    conversation_id: str,
    principal_id: str,
    role: str,
    content: str,
    created_at: str,
) -> str:
    """Return the released exact archive-message row identity."""

    message = _safe_text(message_id)
    conversation = _safe_text(conversation_id)
    principal = _safe_text(principal_id)
    if role not in {"user", "assistant"} or type(content) is not str:
        raise ValueError("conversation-passage source row is invalid")
    content.encode("utf-8", errors="strict")
    timestamp = _canonical_source_utc(created_at)
    return _canonical_sha256(
        {
            "schema": "friday.private-message-window-row.v1",
            "id": message,
            "conversation_id": conversation,
            "person_id": principal,
            "role": role,
            "content": content,
            "created_at": timestamp,
        }
    )


def conversation_passage_content_sha256(content: str) -> str:
    if type(content) is not str:
        raise ValueError("conversation-passage content is invalid")
    return hashlib.sha256(content.encode("utf-8", errors="strict")).hexdigest()


def conversation_passage_anchor_locator_sha256(
    *,
    conversation_id: str,
    anchor_message_id: str,
    anchor_ordinal: int,
) -> str:
    if type(anchor_ordinal) is not int or not 0 <= anchor_ordinal < CONVERSATION_PASSAGE_MAX_COUNT:
        raise ValueError("conversation-passage anchor ordinal is invalid")
    return _canonical_sha256(
        {
            "schema": "friday.conversation-passage-anchor-locator.v1",
            "conversation_id": _safe_text(conversation_id),
            "anchor_message_id": _safe_text(anchor_message_id),
            "anchor_ordinal": anchor_ordinal,
            "passage_index_revision": CONVERSATION_PASSAGE_INDEX_REVISION,
        }
    )


def conversation_passage_prefix_sha256(
    previous_prefix_sha256: str | None,
    anchor_ordinal: int,
    anchor_message_revision_sha256: str,
) -> str:
    if (
        type(anchor_ordinal) is not int
        or not 0 <= anchor_ordinal < CONVERSATION_PASSAGE_MAX_COUNT
        or _valid_digest(anchor_message_revision_sha256) is None
    ):
        raise ValueError("conversation-passage prefix input is invalid")
    if anchor_ordinal == 0:
        if previous_prefix_sha256 is not None:
            raise ValueError("first conversation-passage prefix has a predecessor")
        previous = CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256
    else:
        validated_previous = _valid_digest(previous_prefix_sha256)
        if validated_previous is None:
            raise ValueError("conversation-passage prefix predecessor is invalid")
        previous = validated_previous
    digest = hashlib.sha256(b"friday.conversation-passage-prefix.v1\0")
    digest.update(bytes.fromhex(previous))
    digest.update(anchor_ordinal.to_bytes(8, "big"))
    digest.update(bytes.fromhex(anchor_message_revision_sha256))
    return digest.hexdigest()


_PassageSetRow = tuple[int, str, str, str, str, str]


def conversation_passage_set_extend_sha256(
    previous_set_sha256: str,
    row: _PassageSetRow,
) -> str:
    """Extend one authenticated set digest without replaying its earlier rows."""

    previous = _valid_digest(previous_set_sha256)
    if previous is None or type(row) is not tuple or len(row) != 6:
        raise ValueError("conversation-passage set extension is invalid")
    ordinal, anchor_id, message_revision, content_digest, locator_digest, prefix_digest = row
    if (
        type(ordinal) is not int
        or not 0 <= ordinal < CONVERSATION_PASSAGE_MAX_COUNT
        or _valid_digest(message_revision) is None
        or _valid_digest(content_digest) is None
        or _valid_digest(locator_digest) is None
        or _valid_digest(prefix_digest) is None
    ):
        raise ValueError("conversation-passage set row is invalid")
    anchor = _safe_text(anchor_id).encode("utf-8", errors="strict")
    digest = hashlib.sha256(b"friday.conversation-passage-set.v1\0")
    digest.update(bytes.fromhex(previous))
    digest.update(ordinal.to_bytes(8, "big"))
    digest.update(len(anchor).to_bytes(2, "big"))
    digest.update(anchor)
    digest.update(bytes.fromhex(message_revision))
    digest.update(bytes.fromhex(content_digest))
    digest.update(bytes.fromhex(locator_digest))
    digest.update(bytes.fromhex(prefix_digest))
    return digest.hexdigest()


def conversation_passage_set_sha256(rows: tuple[_PassageSetRow, ...]) -> str:
    """Hash an ordered body-free anchor set with a restart-safe chain."""

    if type(rows) is not tuple or len(rows) > CONVERSATION_PASSAGE_MAX_COUNT:
        raise ValueError("conversation-passage set is invalid")
    current = CONVERSATION_PASSAGE_EMPTY_SET_SHA256
    for expected_ordinal, row in enumerate(rows):
        if type(row) is not tuple or len(row) != 6:
            raise ValueError("conversation-passage set row is malformed")
        ordinal, anchor_id, message_revision, content_digest, locator_digest, prefix_digest = row
        if (
            type(ordinal) is not int
            or ordinal != expected_ordinal
            or _valid_digest(message_revision) is None
            or _valid_digest(content_digest) is None
            or _valid_digest(locator_digest) is None
            or _valid_digest(prefix_digest) is None
        ):
            raise ValueError("conversation-passage set row is invalid")
        current = conversation_passage_set_extend_sha256(current, row)
    return current


def _projection_valid(*values: object) -> int:
    try:
        if len(values) != 9:
            return 0
        (
            conversation_id,
            indexed_count,
            indexed_through,
            conversation_revision,
            passage_set,
            index_revision,
            status,
            incomplete_reason,
            passage_count,
        ) = values
        _safe_text(conversation_id)
        if (
            type(indexed_count) is not int
            or type(passage_count) is not int
            or indexed_count != passage_count
            or not 0 <= passage_count <= CONVERSATION_PASSAGE_MAX_COUNT
            or index_revision != CONVERSATION_PASSAGE_INDEX_REVISION
            or status not in {item.value for item in ConversationPassageProjectionStatus}
        ):
            return 0
        if status == ConversationPassageProjectionStatus.CURRENT.value:
            if incomplete_reason is not None:
                return 0
            if _valid_digest(conversation_revision) is None or _valid_digest(passage_set) is None:
                return 0
            if passage_count == 0:
                return int(
                    indexed_through is None
                    and conversation_revision == CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256
                    and passage_set == CONVERSATION_PASSAGE_EMPTY_SET_SHA256
                )
            _safe_text(indexed_through)
            return 1
        if incomplete_reason not in _REASONS:
            return 0
        if passage_count == 0:
            return int(
                indexed_through is None
                and (
                    (conversation_revision is None and passage_set is None)
                    or (
                        incomplete_reason == ConversationPassageIncompleteReason.SOURCE_CHANGED.value
                        and conversation_revision == CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256
                        and passage_set == CONVERSATION_PASSAGE_EMPTY_SET_SHA256
                    )
                )
            )
        return int(
            incomplete_reason != ConversationPassageIncompleteReason.SOURCE_UNAVAILABLE.value
            and _safe_text(indexed_through) == indexed_through
            and _valid_digest(conversation_revision) is not None
            and _valid_digest(passage_set) is not None
        )
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return 0


def _anchor_valid(*values: object) -> int:
    try:
        if len(values) != 13:
            return 0
        (
            source_id,
            source_conversation_id,
            source_principal_id,
            conversation_principal_id,
            role,
            content,
            created_at,
            passage_conversation_id,
            anchor_message_id,
            anchor_ordinal,
            message_revision,
            content_digest,
            locator_digest,
        ) = values
        if type(content) is bytes:
            try:
                content = content.decode("utf-8", errors="strict")
            except UnicodeError:
                return 0
        if (
            type(source_id) is not str
            or _MESSAGE_ID.fullmatch(source_id) is None
            or source_conversation_id != passage_conversation_id
            or source_id != anchor_message_id
            or source_principal_id != conversation_principal_id
            or role not in {"user", "assistant"}
            or type(content) is not str
            or type(created_at) is not str
            or type(anchor_ordinal) is not int
        ):
            return 0
        expected_revision = conversation_passage_message_revision_sha256(
            message_id=_safe_text(source_id),
            conversation_id=_safe_text(source_conversation_id),
            principal_id=_safe_text(source_principal_id),
            role=str(role),
            content=content,
            created_at=created_at,
        )
        return int(
            message_revision == expected_revision
            and content_digest == conversation_passage_content_sha256(content)
            and locator_digest
            == conversation_passage_anchor_locator_sha256(
                conversation_id=_safe_text(passage_conversation_id),
                anchor_message_id=_safe_text(anchor_message_id),
                anchor_ordinal=anchor_ordinal,
            )
        )
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return 0


def _prefix_udf(previous: object, ordinal: object, message_revision: object) -> str:
    try:
        if type(ordinal) is not int or type(message_revision) is not str:
            return ""
        if previous is not None and type(previous) is not str:
            return ""
        return conversation_passage_prefix_sha256(previous, ordinal, message_revision)
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return ""


def _set_extend_udf(*values: object) -> str:
    try:
        if len(values) != 7:
            return ""
        previous, ordinal, anchor_id, message_revision, content_digest, locator_digest, prefix_digest = values
        if (
            type(previous) is not str
            or type(ordinal) is not int
            or any(
                type(item) is not str
                for item in (
                    anchor_id,
                    message_revision,
                    content_digest,
                    locator_digest,
                    prefix_digest,
                )
            )
        ):
            return ""
        return conversation_passage_set_extend_sha256(
            previous,
            cast(_PassageSetRow, values[1:]),
        )
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return ""


class _PassageSetSha256:
    def __init__(self) -> None:
        self._expected_ordinal = 0
        self._current = CONVERSATION_PASSAGE_EMPTY_SET_SHA256
        self._invalid = False

    def step(self, *values: object) -> None:
        if self._invalid:
            return
        try:
            if len(values) != 6:
                self._invalid = True
                return
            ordinal, anchor_id, message_revision, content_digest, locator_digest, prefix_digest = values
            if (
                type(ordinal) is not int
                or ordinal != self._expected_ordinal
                or ordinal >= CONVERSATION_PASSAGE_MAX_COUNT
                or any(
                    type(item) is not str
                    for item in (
                        anchor_id,
                        message_revision,
                        content_digest,
                        locator_digest,
                        prefix_digest,
                    )
                )
            ):
                self._invalid = True
                return
            row = cast(_PassageSetRow, values)
            self._current = conversation_passage_set_extend_sha256(
                self._current,
                row,
            )
            self._expected_ordinal += 1
        except (TypeError, ValueError, UnicodeError, OverflowError):
            self._invalid = True

    def finalize(self) -> str:
        return "" if self._invalid else self._current


def register_conversation_passage_connection_functions(conn: sqlite3.Connection) -> None:
    conn.create_function(
        "friday_conversation_passage_conversation_identity_valid",
        1,
        _conversation_identity_valid,
        deterministic=True,
    )
    conn.create_function(
        "friday_conversation_passage_source_descriptor_valid",
        8,
        _source_descriptor_valid,
        deterministic=True,
    )
    conn.create_function(
        "friday_conversation_passage_utf8_valid",
        1,
        _utf8_valid,
        deterministic=True,
    )
    conn.create_function(
        "friday_conversation_passage_projection_valid", 9, _projection_valid, deterministic=True
    )
    conn.create_function("friday_conversation_passage_anchor_valid", 13, _anchor_valid, deterministic=True)
    conn.create_function("friday_conversation_passage_prefix_sha256", 3, _prefix_udf, deterministic=True)
    conn.create_function(
        "friday_conversation_passage_set_extend_sha256",
        7,
        _set_extend_udf,
        deterministic=True,
    )
    conn.create_aggregate(
        "friday_conversation_passage_set_sha256",
        6,
        _PassageSetSha256,  # type: ignore[arg-type]
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
        raise sqlite3.DatabaseError("Conversation passage schema contains incomplete SQL")


def _normalize_schema_sql(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _schema_objects_sha256(objects: dict[tuple[str, str], str]) -> str:
    material = "\n".join(f"{kind}\0{name}\0{sql}" for (kind, name), sql in sorted(objects.items()))
    return hashlib.sha256(material.encode("utf-8", errors="strict")).hexdigest()


def _schema_objects(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {
        (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
        for row in conn.execute(
            """SELECT type,name,sql FROM sqlite_master
                 WHERE sql IS NOT NULL
                   AND (name LIKE 'conversation_passage%'
                        OR tbl_name LIKE 'conversation_passage%'
                        OR name LIKE 'idx_conversation_passage_%')
                 ORDER BY type,name"""
        )
    }


def _is_fts_object(key: tuple[str, str]) -> bool:
    _kind, name = key
    return bool(
        name == "conversation_passage_search_content"
        or name == "conversation_passages_fts"
        or name.startswith("conversation_passages_fts_")
        or name.startswith("conversation_passage_fts_")
    )


def _ordinary_objects(
    objects: dict[tuple[str, str], str],
) -> dict[tuple[str, str], str]:
    return {key: value for key, value in objects.items() if not _is_fts_object(key)}


def _fts_objects(
    objects: dict[tuple[str, str], str],
) -> dict[tuple[str, str], str]:
    return {key: value for key, value in objects.items() if _is_fts_object(key)}


@lru_cache(maxsize=2)
def _canonical_schema_objects(*, include_fts: bool) -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        register_conversation_passage_connection_functions(conn)
        conn.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY);
            CREATE TABLE conversations(
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id)
            );
            CREATE TABLE messages(
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                user_id TEXT NOT NULL REFERENCES users(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        _execute_schema(conn, CONVERSATION_PASSAGE_SCHEMA)
        if include_fts:
            _execute_schema(conn, CONVERSATION_PASSAGE_FTS_SCHEMA)
        return _schema_objects(conn)
    finally:
        conn.close()


@lru_cache(maxsize=2)
def _canonical_released_schema_v49_objects(
    *,
    include_fts: bool,
) -> dict[tuple[str, str], str]:
    """Return the exact reader-first sidecar shipped by schema 49."""

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        register_conversation_passage_connection_functions(conn)
        conn.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY);
            CREATE TABLE conversations(
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(id)
            );
            CREATE TABLE messages(
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL REFERENCES conversations(id),
                user_id TEXT NOT NULL REFERENCES users(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )
        _execute_schema(conn, _RELEASED_CONVERSATION_PASSAGE_SCHEMA_V49)
        if include_fts:
            _execute_schema(conn, CONVERSATION_PASSAGE_FTS_SCHEMA)
        return _schema_objects(conn)
    finally:
        conn.close()


def _canonical_fts_objects_if_available() -> dict[tuple[str, str], str] | None:
    try:
        return _fts_objects(_canonical_schema_objects(include_fts=True))
    except sqlite3.OperationalError as exc:
        if str(exc).strip().casefold() != "no such module: fts5":
            raise
        return None


def _canonical_released_fts_objects_if_available() -> dict[tuple[str, str], str] | None:
    try:
        return _fts_objects(_canonical_released_schema_v49_objects(include_fts=True))
    except sqlite3.OperationalError as exc:
        if str(exc).strip().casefold() != "no such module: fts5":
            raise
        return None


def _validate_no_external_dependencies(
    conn: sqlite3.Connection,
    canonical: dict[tuple[str, str], str],
    *,
    schema_version: int,
) -> None:
    for kind, name, sql in conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
    ):
        key = (str(kind), str(name))
        if key in canonical:
            continue
        if _OWNED_REFERENCE_RE.search(str(sql)) is not None:
            raise sqlite3.DatabaseError(
                f"Schema {schema_version} conversation passage external dependency is unexpected"
            )


def _validate_shape(
    conn: sqlite3.Connection,
    *,
    include_fts: bool,
    schema_version: int,
) -> None:
    projection_columns = {
        str(row[1]): (str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in conn.execute("PRAGMA table_info(conversation_passage_projections)")
    }
    if projection_columns != {
        "conversation_id": ("TEXT", 1, 1),
        "indexed_message_count": ("INTEGER", 1, 0),
        "indexed_through_message_id": ("TEXT", 0, 0),
        "indexed_conversation_revision_sha256": ("TEXT", 0, 0),
        "passage_set_sha256": ("TEXT", 0, 0),
        "passage_index_revision": ("TEXT", 1, 0),
        "projection_status": ("TEXT", 1, 0),
        "incomplete_reason": ("TEXT", 0, 0),
        "passage_count": ("INTEGER", 1, 0),
        "projected_at": ("TEXT", 1, 0),
    }:
        raise sqlite3.DatabaseError(
            f"Schema {schema_version} conversation passage projection shape is invalid"
        )
    passage_columns = {
        str(row[1]): (str(row[2]).upper(), int(row[3]), int(row[5]))
        for row in conn.execute("PRAGMA table_info(conversation_passages)")
    }
    if passage_columns != {
        "passage_rowid": ("INTEGER", 1, 1),
        "conversation_id": ("TEXT", 1, 0),
        "anchor_message_id": ("TEXT", 1, 0),
        "anchor_ordinal": ("INTEGER", 1, 0),
        "anchor_message_revision_sha256": ("TEXT", 1, 0),
        "anchor_content_sha256": ("TEXT", 1, 0),
        "anchor_locator_sha256": ("TEXT", 1, 0),
        "conversation_prefix_sha256": ("TEXT", 1, 0),
    }:
        raise sqlite3.DatabaseError(f"Schema {schema_version} conversation passage row shape is invalid")
    projection_fks = {
        (str(row[3]), str(row[2]), str(row[4]), str(row[5]), str(row[6]))
        for row in conn.execute("PRAGMA foreign_key_list(conversation_passage_projections)")
    }
    passage_fks = {
        (str(row[3]), str(row[2]), str(row[4]), str(row[5]), str(row[6]))
        for row in conn.execute("PRAGMA foreign_key_list(conversation_passages)")
    }
    if projection_fks != {
        ("conversation_id", "conversations", "id", "CASCADE", "CASCADE")
    } or passage_fks != {
        ("conversation_id", "conversation_passage_projections", "conversation_id", "CASCADE", "CASCADE"),
        ("anchor_message_id", "messages", "id", "CASCADE", "CASCADE"),
    }:
        raise sqlite3.DatabaseError(f"Schema {schema_version} conversation passage ownership is invalid")
    if include_fts:
        fts_columns = tuple(
            str(row[1]) for row in conn.execute("PRAGMA table_info(conversation_passages_fts)")
        )
        view_columns = tuple(
            str(row[1]) for row in conn.execute("PRAGMA table_info(conversation_passage_search_content)")
        )
        if fts_columns != ("content",) or view_columns != ("passage_rowid", "content"):
            raise sqlite3.DatabaseError(f"Schema {schema_version} conversation passage FTS shape is invalid")


def _validate_authoritative_rows(
    conn: sqlite3.Connection,
    *,
    schema_version: int,
    source_order_index_available: bool,
) -> None:
    invalid_message_rowid = conn.execute("SELECT 1 FROM messages WHERE rowid<1 LIMIT 1").fetchone()
    invalid_conversation_identity = conn.execute(
        """SELECT 1 FROM conversations conversation
            WHERE friday_conversation_passage_conversation_identity_valid(
                      conversation.id)<>1
            LIMIT 1"""
    ).fetchone()
    invalid_source_descriptor = conn.execute(
        """SELECT 1
             FROM messages source
             LEFT JOIN conversations conversation
               ON conversation.id=source.conversation_id
            WHERE source.role IN ('user','assistant')
              AND (
                  conversation.id IS NULL
                  OR friday_conversation_passage_source_descriptor_valid(
                         source.id,source.conversation_id,conversation.id,
                         source.user_id,conversation.user_id,source.role,
                         typeof(source.content),source.created_at)<>1
                  OR friday_conversation_passage_utf8_valid(
                         CAST(source.content AS BLOB))<>1
              )
            LIMIT 1"""
    ).fetchone()
    missing = conn.execute(
        """SELECT 1 FROM conversations source
            WHERE NOT EXISTS (
                SELECT 1 FROM conversation_passage_projections projection
                 WHERE projection.conversation_id=source.id
            ) LIMIT 1"""
    ).fetchone()
    invalid_projection = conn.execute(
        """SELECT 1
             FROM conversation_passage_projections projection
             LEFT JOIN conversations source ON source.id=projection.conversation_id
            WHERE source.id IS NULL
               OR friday_conversation_passage_projection_valid(
                      projection.conversation_id,projection.indexed_message_count,
                      projection.indexed_through_message_id,
                      projection.indexed_conversation_revision_sha256,
                      projection.passage_set_sha256,projection.passage_index_revision,
                      projection.projection_status,projection.incomplete_reason,
                      projection.passage_count)<>1
            LIMIT 1"""
    ).fetchone()
    source_unavailable_valid_sql = _first_oversized_source_valid_sql(
        conversation_id_sql="conversation.id",
        principal_id_sql="conversation.user_id",
        force_source_order_index=source_order_index_available,
    )
    invalid_source_unavailable = conn.execute(
        f"""SELECT 1
              FROM conversation_passage_projections projection
              JOIN conversations conversation
                ON conversation.id=projection.conversation_id
             WHERE projection.projection_status='incomplete'
               AND projection.incomplete_reason='source_unavailable'
               AND NOT ({source_unavailable_valid_sql})
             LIMIT 1"""
    ).fetchone()
    invalid_anchor = conn.execute(
        """WITH canonical_source_order AS MATERIALIZED (
                   SELECT conversation.id AS conversation_id,
                          message.id AS message_id,
                          ROW_NUMBER() OVER (
                              PARTITION BY conversation.id
                              ORDER BY message.rowid ASC
                          )-1 AS anchor_ordinal,
                          message.user_id,message.role,message.content,message.created_at,
                          conversation.user_id AS conversation_user_id
                     FROM conversations conversation
                     JOIN messages message
                       ON message.conversation_id=conversation.id
                      AND message.user_id=conversation.user_id
                    WHERE message.role IN ('user','assistant')
               ),
               mapped_order AS MATERIALIZED (
                   SELECT passage.passage_rowid,
                          source.conversation_id,source.message_id,
                          source.anchor_ordinal,source.user_id,source.role,
                          source.content,source.created_at,
                          source.conversation_user_id
                     FROM conversation_passages passage
                     JOIN canonical_source_order source
                       ON source.conversation_id=passage.conversation_id
                      AND source.message_id=passage.anchor_message_id
               )
               SELECT 1
                 FROM conversation_passages passage
                 LEFT JOIN mapped_order source
                   ON source.passage_rowid=passage.passage_rowid
                WHERE source.message_id IS NULL
                   OR source.conversation_id<>passage.conversation_id
                   OR source.message_id<>passage.anchor_message_id
                   OR source.anchor_ordinal<>passage.anchor_ordinal
                   OR friday_conversation_passage_anchor_valid(
                          source.message_id,source.conversation_id,source.user_id,
                          source.conversation_user_id,source.role,
                          CAST(source.content AS BLOB),
                          source.created_at,passage.conversation_id,
                          passage.anchor_message_id,passage.anchor_ordinal,
                          passage.anchor_message_revision_sha256,
                          passage.anchor_content_sha256,
                          passage.anchor_locator_sha256)<>1
                LIMIT 1"""
    ).fetchone()
    invalid_chain = conn.execute(
        """SELECT 1
             FROM conversation_passages passage
             LEFT JOIN conversation_passages previous
               ON previous.conversation_id=passage.conversation_id
              AND previous.anchor_ordinal=passage.anchor_ordinal-1
            WHERE passage.conversation_prefix_sha256<>
                  friday_conversation_passage_prefix_sha256(
                      CASE WHEN passage.anchor_ordinal=0 THEN NULL
                           ELSE previous.conversation_prefix_sha256 END,
                      passage.anchor_ordinal,
                      passage.anchor_message_revision_sha256)
               OR (passage.anchor_ordinal>0 AND previous.passage_rowid IS NULL)
            LIMIT 1"""
    ).fetchone()
    invalid_rollup = conn.execute(
        f"""SELECT 1
              FROM conversation_passage_projections projection
             WHERE projection.passage_count<>(
                       SELECT COUNT(*) FROM conversation_passages passage
                        WHERE passage.conversation_id=projection.conversation_id)
                OR (projection.passage_count>0 AND (
                       projection.indexed_through_message_id<>(
                           SELECT passage.anchor_message_id
                             FROM conversation_passages passage
                            WHERE passage.conversation_id=projection.conversation_id
                            ORDER BY passage.anchor_ordinal DESC LIMIT 1)
                       OR projection.indexed_conversation_revision_sha256<>(
                           SELECT passage.conversation_prefix_sha256
                             FROM conversation_passages passage
                            WHERE passage.conversation_id=projection.conversation_id
                            ORDER BY passage.anchor_ordinal DESC LIMIT 1)
                       OR projection.passage_set_sha256<>(
                           SELECT friday_conversation_passage_set_sha256(
                                      ordered.anchor_ordinal,
                                      ordered.anchor_message_id,
                                      ordered.anchor_message_revision_sha256,
                                      ordered.anchor_content_sha256,
                                      ordered.anchor_locator_sha256,
                                      ordered.conversation_prefix_sha256)
                             FROM (
                                 SELECT passage.anchor_ordinal,
                                        passage.anchor_message_id,
                                        passage.anchor_message_revision_sha256,
                                        passage.anchor_content_sha256,
                                        passage.anchor_locator_sha256,
                                        passage.conversation_prefix_sha256
                                   FROM conversation_passages passage
                                  WHERE passage.conversation_id=
                                        projection.conversation_id
                                  ORDER BY passage.anchor_ordinal ASC
                                  LIMIT -1
                             ) AS ordered)
                ))
                OR (projection.projection_status='current' AND (
                       projection.passage_count<>(
                           SELECT COUNT(*)
                             FROM messages source
                             JOIN conversations conversation
                               ON conversation.id=source.conversation_id
                              AND conversation.user_id=source.user_id
                            WHERE source.conversation_id=projection.conversation_id
                              AND source.role IN ('user','assistant'))
                       OR (projection.passage_count=0 AND (
                              projection.indexed_conversation_revision_sha256<>
                                  '{CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256}'
                              OR projection.passage_set_sha256<>
                                  '{CONVERSATION_PASSAGE_EMPTY_SET_SHA256}'))
                ))
             LIMIT 1"""
    ).fetchone()
    if any(
        item is not None
        for item in (
            invalid_message_rowid,
            invalid_conversation_identity,
            invalid_source_descriptor,
            missing,
            invalid_projection,
            invalid_source_unavailable,
            invalid_anchor,
            invalid_chain,
            invalid_rollup,
        )
    ):
        raise sqlite3.DatabaseError(f"Schema {schema_version} conversation passage data is invalid")


def _validate_fts_rows(
    conn: sqlite3.Connection,
    *,
    schema_version: int,
) -> None:
    fts_mismatch = conn.execute(
        """SELECT 1 FROM conversation_passages passage
             LEFT JOIN conversation_passages_fts_docsize fts
               ON fts.id=passage.passage_rowid
            WHERE fts.id IS NULL
            UNION ALL
           SELECT 1 FROM conversation_passages_fts_docsize fts
             LEFT JOIN conversation_passages passage
               ON passage.passage_rowid=fts.id
            WHERE passage.passage_rowid IS NULL
            LIMIT 1"""
    ).fetchone()
    if fts_mismatch is not None:
        raise sqlite3.DatabaseError(f"Schema {schema_version} conversation passage data is invalid")
    if conn.in_transaction and int(conn.execute("PRAGMA query_only").fetchone()[0]) == 0:
        try:
            cursor = conn.execute(
                "INSERT INTO conversation_passages_fts(conversation_passages_fts,rank) "
                "VALUES('integrity-check',1)"
            )
            cursor.close()
        except sqlite3.DatabaseError:
            raise sqlite3.DatabaseError(
                f"Schema {schema_version} conversation passage FTS data is invalid"
            ) from None


def _validate_released_conversation_passage_schema_v49(
    conn: sqlite3.Connection,
    *,
    required: bool = True,
    validate_data: bool = True,
    require_fts: bool = False,
    validate_fts_data: bool = True,
) -> None:
    """Authenticate the exact released schema-49 sidecar before replacement."""

    register_conversation_passage_connection_functions(conn)
    installed = _schema_objects(conn)
    if not installed:
        if required:
            raise sqlite3.DatabaseError("Schema 49 conversation passage projection is missing")
        return
    installed_ordinary = _ordinary_objects(installed)
    installed_fts = _fts_objects(installed)
    canonical_ordinary = _ordinary_objects(_canonical_released_schema_v49_objects(include_fts=False))
    if installed_ordinary != canonical_ordinary:
        raise sqlite3.DatabaseError("Schema 49 conversation passage DDL is incomplete or altered")
    if installed_fts and _schema_objects_sha256(installed_fts) != _CONVERSATION_PASSAGE_FTS_SCHEMA_SHA256:
        raise sqlite3.DatabaseError("Schema 49 conversation passage FTS DDL is incomplete or altered")
    canonical_fts: dict[tuple[str, str], str] | None = {}
    if installed_fts or require_fts:
        canonical_fts = _canonical_released_fts_objects_if_available()
    fts_available = canonical_fts is not None
    if fts_available:
        if installed_fts and installed_fts != canonical_fts:
            raise sqlite3.DatabaseError("Schema 49 conversation passage FTS DDL is incomplete or altered")
        if require_fts and installed_fts != canonical_fts:
            raise sqlite3.DatabaseError("Schema 49 conversation passage FTS projection is missing")
    elif require_fts or validate_fts_data:
        raise sqlite3.OperationalError("no such module: fts5")
    authenticated = dict(canonical_ordinary)
    if installed_fts:
        authenticated.update(installed_fts)
    _validate_no_external_dependencies(conn, authenticated, schema_version=49)
    _validate_shape(
        conn,
        include_fts=bool(installed_fts) and fts_available,
        schema_version=49,
    )
    if validate_data:
        _validate_authoritative_rows(
            conn,
            schema_version=49,
            source_order_index_available=False,
        )
        if installed_fts and fts_available and validate_fts_data:
            _validate_fts_rows(conn, schema_version=49)


def validate_conversation_passage_schema(
    conn: sqlite3.Connection,
    *,
    required: bool = True,
    validate_data: bool = True,
    require_fts: bool = False,
    validate_fts_data: bool = True,
    _register_functions: bool = True,
    _validate_authoritative_data: bool = True,
) -> None:
    """Validate authoritative schema and any installed optional FTS derivative.

    The core migration uses ``require_fts=False`` because FTS5 is installed in
    its separately committed, restart-repairable phase.  Pre-core recovery may
    use ``validate_fts_data=False``: authoritative data and optional FTS shape
    remain exact, while corrupt derivative rows are allowed through to the
    post-commit rebuild.  A partially installed FTS contour is never accepted.
    """

    if _register_functions:
        register_conversation_passage_connection_functions(conn)
    installed = _schema_objects(conn)
    if not installed:
        if required:
            raise sqlite3.DatabaseError("Schema 50 conversation passage projection is missing")
        return
    installed_ordinary = _ordinary_objects(installed)
    installed_fts = _fts_objects(installed)
    canonical_ordinary = _canonical_schema_objects(include_fts=False)
    if installed_ordinary != canonical_ordinary:
        raise sqlite3.DatabaseError("Schema 50 conversation passage DDL is incomplete or altered")
    if installed_fts and _schema_objects_sha256(installed_fts) != _CONVERSATION_PASSAGE_FTS_SCHEMA_SHA256:
        raise sqlite3.DatabaseError("Schema 50 conversation passage FTS DDL is incomplete or altered")
    canonical_fts: dict[tuple[str, str], str] | None = {}
    if installed_fts or require_fts:
        canonical_fts = _canonical_fts_objects_if_available()
    fts_available = canonical_fts is not None
    if fts_available:
        if installed_fts and installed_fts != canonical_fts:
            raise sqlite3.DatabaseError("Schema 50 conversation passage FTS DDL is incomplete or altered")
        if require_fts and installed_fts != canonical_fts:
            raise sqlite3.DatabaseError("Schema 50 conversation passage FTS projection is missing")
    elif require_fts or validate_fts_data:
        raise sqlite3.OperationalError("no such module: fts5")
    authenticated = dict(canonical_ordinary)
    if installed_fts:
        # The frozen digest above authenticates these exact raw sqlite_master
        # definitions even when this process cannot instantiate their module.
        authenticated.update(installed_fts)
    _validate_no_external_dependencies(conn, authenticated, schema_version=50)
    _validate_shape(
        conn,
        include_fts=bool(installed_fts) and fts_available,
        schema_version=50,
    )
    if validate_data:
        if _validate_authoritative_data:
            _validate_authoritative_rows(
                conn,
                schema_version=50,
                source_order_index_available=True,
            )
        if installed_fts and fts_available and validate_fts_data:
            _validate_fts_rows(conn, schema_version=50)


def validate_conversation_passage_fts_schema(
    conn: sqlite3.Connection,
    *,
    required: bool = True,
    validate_data: bool = True,
    _register_functions: bool = True,
    _validate_authoritative_data: bool = True,
) -> None:
    """Validate the optional derivative without weakening authoritative DDL."""

    validate_conversation_passage_schema(
        conn,
        required=True,
        validate_data=validate_data,
        require_fts=required,
        validate_fts_data=validate_data,
        _register_functions=_register_functions,
        _validate_authoritative_data=_validate_authoritative_data,
    )


_SCHEMA_49_PUBLICATION_GUARDS = (
    "conversation_passage_projection_bi_validate",
    "conversation_passage_projection_bu_validate",
    "conversation_passage_bi_validate",
    "conversation_passage_bu_validate",
    "conversation_passage_message_bi_identity_immutable",
    "conversation_passage_message_ai_invalidate",
    "conversation_passage_message_au_reset",
    "conversation_passage_conversation_bu_reset",
)


def _terminalize_valid_oversized_first_sources(conn: sqlite3.Connection) -> None:
    """Settle admitted historical oversize without charging a writer tick."""

    valid_source_sql = _first_oversized_source_valid_sql(
        conversation_id_sql="conversation.id",
        principal_id_sql="conversation.user_id",
        force_source_order_index=True,
    )
    conn.execute(
        f"""UPDATE conversation_passage_projections
               SET indexed_message_count=0,
                   indexed_through_message_id=NULL,
                   indexed_conversation_revision_sha256=NULL,
                   passage_set_sha256=NULL,
                   projection_status='incomplete',
                   incomplete_reason='source_unavailable',
                   passage_count=0,
                   projected_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')
             WHERE projection_status='incomplete'
               AND incomplete_reason IN ('backfill_pending','source_changed')
               AND passage_count=0
               AND EXISTS (
                   SELECT 1
                     FROM conversations conversation
                    WHERE conversation.id=
                          conversation_passage_projections.conversation_id
                      AND {valid_source_sql}
               )"""
    )


def upgrade_conversation_passage_schema_49_to_50(
    conn: sqlite3.Connection,
    *,
    required: bool = True,
) -> None:
    """Replace schema-49 publication guards and add the two bounded-work indexes."""

    if not conn.in_transaction:
        raise RuntimeError("Conversation passage schema upgrade requires an existing transaction")
    installed = _schema_objects(conn)
    if not installed:
        if required:
            raise sqlite3.DatabaseError("Schema 49 conversation passage projection is missing")
        return

    # An exact schema-50 contour under the schema-49 marker is not an interrupted
    # migration: this upgrade and the marker move share one outer transaction.
    _validate_released_conversation_passage_schema_v49(
        conn,
        validate_fts_data=False,
    )
    savepoint = "conversation_passage_49_to_50"
    conn.execute(f'SAVEPOINT "{savepoint}"')
    try:
        for trigger_name in _SCHEMA_49_PUBLICATION_GUARDS:
            conn.execute(f'DROP TRIGGER "{trigger_name}"')  # nosec B608 - fixed allowlist
        _execute_schema(conn, CONVERSATION_PASSAGE_SCHEMA)
        _terminalize_valid_oversized_first_sources(conn)
        validate_conversation_passage_schema(
            conn,
            require_fts=False,
            validate_fts_data=False,
            _register_functions=False,
        )
    except BaseException:
        conn.execute(f'ROLLBACK TO SAVEPOINT "{savepoint}"')
        conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')
        raise
    conn.execute(f'RELEASE SAVEPOINT "{savepoint}"')


def conversation_passage_schema_fingerprint(conn: sqlite3.Connection) -> str:
    validate_conversation_passage_schema(conn, validate_data=False)
    material = "\n".join(
        f"{kind}\0{name}\0{sql}"
        for (kind, name), sql in sorted(_ordinary_objects(_schema_objects(conn)).items())
    )
    return hashlib.sha256(material.encode("utf-8", errors="strict")).hexdigest()


def conversation_passage_fts_schema_fingerprint(conn: sqlite3.Connection) -> str:
    validate_conversation_passage_fts_schema(conn, validate_data=False)
    return _schema_objects_sha256(_fts_objects(_schema_objects(conn)))


def install_conversation_passage_schema(conn: sqlite3.Connection) -> None:
    """Install and seed dormant reader-first coverage inside schema migration."""

    if not conn.in_transaction:
        raise RuntimeError("Conversation passage installation requires an existing transaction")
    register_conversation_passage_connection_functions(conn)
    installed = _schema_objects(conn)
    installed_ordinary = _ordinary_objects(installed)
    installed_fts = _fts_objects(installed)
    canonical_ordinary = _canonical_schema_objects(include_fts=False)
    if installed_ordinary and installed_ordinary != canonical_ordinary:
        raise sqlite3.DatabaseError("Schema 50 conversation passage DDL is incomplete or altered")
    if installed_fts and _schema_objects_sha256(installed_fts) != _CONVERSATION_PASSAGE_FTS_SCHEMA_SHA256:
        raise sqlite3.DatabaseError("Schema 50 conversation passage FTS DDL is incomplete or altered")
    if not installed_ordinary:
        if installed_fts:
            raise sqlite3.DatabaseError("Schema 50 conversation passage DDL is incomplete or altered")
        _execute_schema(conn, CONVERSATION_PASSAGE_SCHEMA)
    conn.execute(
        f"""INSERT INTO conversation_passage_projections(
                conversation_id,indexed_message_count,indexed_through_message_id,
                indexed_conversation_revision_sha256,passage_set_sha256,
                passage_index_revision,projection_status,incomplete_reason,
                passage_count,projected_at
            )
            SELECT source.id,0,NULL,NULL,NULL,
                   '{CONVERSATION_PASSAGE_INDEX_REVISION}',
                   'incomplete','backfill_pending',0,
                   strftime('%Y-%m-%dT%H:%M:%SZ','now')
              FROM conversations source
             WHERE NOT EXISTS (
                       SELECT 1
                         FROM conversation_passage_projections projection
                        WHERE projection.conversation_id=source.id
                   )"""
    )
    _terminalize_valid_oversized_first_sources(conn)
    validate_conversation_passage_schema(
        conn,
        require_fts=False,
        validate_fts_data=False,
        _register_functions=False,
    )


def install_conversation_passage_fts_schema(
    conn: sqlite3.Connection,
    *,
    _register_functions: bool = True,
    _validate_authoritative_data: bool = True,
) -> None:
    """Install/rebuild optional FTS after the authoritative schema commit."""

    if not conn.in_transaction:
        raise RuntimeError("Conversation passage FTS installation requires an existing transaction")
    validate_conversation_passage_schema(
        conn,
        require_fts=False,
        validate_fts_data=False,
        _register_functions=_register_functions,
        _validate_authoritative_data=_validate_authoritative_data,
    )
    installed_fts = _fts_objects(_schema_objects(conn))
    canonical_fts = _fts_objects(_canonical_schema_objects(include_fts=True))
    if installed_fts and installed_fts != canonical_fts:
        raise sqlite3.DatabaseError("Schema 50 conversation passage FTS DDL is incomplete or altered")
    if not installed_fts:
        _execute_schema(conn, CONVERSATION_PASSAGE_FTS_SCHEMA)
    cursor = conn.execute(
        "INSERT INTO conversation_passages_fts(conversation_passages_fts) VALUES('rebuild')"
    )
    cursor.close()
    validate_conversation_passage_fts_schema(
        conn,
        _register_functions=False,
        _validate_authoritative_data=False,
    )


__all__ = [
    "CONVERSATION_PASSAGE_EMPTY_PREFIX_SHA256",
    "CONVERSATION_PASSAGE_EMPTY_SET_SHA256",
    "CONVERSATION_PASSAGE_FTS_SCHEMA",
    "CONVERSATION_PASSAGE_INDEX_REVISION",
    "CONVERSATION_PASSAGE_SCHEMA",
    "CONVERSATION_PASSAGE_SCHEMA_VERSION",
    "CONVERSATION_PASSAGE_TEXT_WORK_BUDGET_BYTES",
    "conversation_passage_anchor_locator_sha256",
    "conversation_passage_content_sha256",
    "conversation_passage_fts_schema_fingerprint",
    "conversation_passage_message_revision_sha256",
    "conversation_passage_prefix_sha256",
    "conversation_passage_schema_fingerprint",
    "conversation_passage_set_extend_sha256",
    "conversation_passage_set_sha256",
    "install_conversation_passage_fts_schema",
    "install_conversation_passage_schema",
    "register_conversation_passage_connection_functions",
    "upgrade_conversation_passage_schema_49_to_50",
    "validate_conversation_passage_fts_schema",
    "validate_conversation_passage_schema",
]
