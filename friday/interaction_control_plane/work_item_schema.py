"""Exact schema-40 projection for bounded durable recall and candidate Work Items."""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache

from friday.interaction_control_plane.work_item_contract import (
    ARCHIVE_CANDIDATE_MAX_COUNT,
    ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON,
    RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON,
    WORK_ITEM_ACTIVE_FRAME_MAX_BYTES,
    WORK_ITEM_TTL_HOURS,
    WorkCompletionContract,
    WorkGoal,
    WorkKind,
    WorkPlaybook,
    WorkState,
    WorkTransition,
)
from friday.orchestration.archive_recall_outcome import ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE

WORK_ITEM_SCHEMA_VERSION = 40
WORK_ITEM_SELECTED_SOURCE_REF_MAX_BYTES = 4_096
WORK_ITEM_SELECTED_PASSAGE_REFS_MAX_BYTES = 65_536
WORK_ITEM_SELECTED_PASSAGE_MAX_COUNT = 8

_WORK_ITEM_TABLES = (
    "work_item_archive_candidate_questions",
    "work_item_archive_candidate_set_items",
    "work_item_archive_candidate_sets",
    "work_item_selected_evidence",
    "work_items",
)
_WORK_ITEM_INDEXES = (
    "idx_work_item_archive_candidate_items_work",
    "idx_work_item_archive_candidate_questions_work",
    "idx_work_item_archive_candidate_sets_origin",
    "idx_work_item_selected_evidence_origin_boundary",
    "idx_work_items_conversation_updated",
    "idx_work_items_expiry",
    "idx_work_items_owner_state_updated",
    "uq_work_items_open_conversation",
)
_SCHEMA_39_TABLES = ("work_item_selected_evidence", "work_items")
_SCHEMA_39_INDEXES = (
    "idx_work_item_selected_evidence_origin_boundary",
    "idx_work_items_conversation_updated",
    "idx_work_items_expiry",
    "idx_work_items_owner_state_updated",
    "uq_work_items_active_conversation",
)
_SCHEMA_38_INDEXES = (
    "idx_work_items_conversation_updated",
    "idx_work_items_expiry",
    "idx_work_items_owner_state_updated",
    "uq_work_items_active_conversation",
)


def _sql_values(values: object) -> str:
    return ", ".join(f"'{value.value}'" for value in values)  # type: ignore[attr-defined]


_SCHEMA_39_KIND_SQL = _sql_values((WorkKind.RECALL_CONVERSATION, WorkKind.RECALL_SELECTED_ARCHIVE_EVIDENCE))
_SCHEMA_39_GOAL_SQL = _sql_values(
    (WorkGoal.EXACT_CURRENT_CONVERSATION_RECALL, WorkGoal.EXACT_SELECTED_ARCHIVE_EVIDENCE_RECALL)
)
_SCHEMA_39_STATE_SQL = _sql_values(
    (WorkState.ACTIVE, WorkState.SUSPENDED, WorkState.CANCELLED, WorkState.EXPIRED)
)
_SCHEMA_39_PLAYBOOK_SQL = _sql_values(
    (WorkPlaybook.RECALL_CONVERSATION, WorkPlaybook.RECALL_SELECTED_ARCHIVE_EVIDENCE)
)
_SCHEMA_39_COMPLETION_SQL = _sql_values(
    (
        WorkCompletionContract.ACCEPTED_EXACT_OWNED_MESSAGE_WINDOW,
        WorkCompletionContract.ACCEPTED_EXACT_SELECTED_ARCHIVE_EVIDENCE,
    )
)
_SCHEMA_39_TRANSITION_SQL = _sql_values(
    (
        WorkTransition.CREATED,
        WorkTransition.CONSTRAINT_UPDATED,
        WorkTransition.EVIDENCE_REPLAYED,
        WorkTransition.SUSPENDED,
        WorkTransition.CANCELLED,
        WorkTransition.EXPIRED,
    )
)
_KIND_SQL = _sql_values(WorkKind)
_GOAL_SQL = _sql_values(WorkGoal)
_STATE_SQL = _sql_values(WorkState)
_PLAYBOOK_SQL = _sql_values(WorkPlaybook)
_COMPLETION_SQL = _sql_values(WorkCompletionContract)
_TRANSITION_SQL = _sql_values(WorkTransition)

# Retain the exact released projection so a marker-38 database is authenticated
# before its table is rebuilt. The old DDL must never be relaxed merely to make
# a migration succeed.
_WORK_ITEM_SCHEMA_38 = f"""
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    kind TEXT NOT NULL CHECK(kind IN ('recall_conversation')),
    goal TEXT NOT NULL CHECK(goal IN ('exact_current_conversation_recall')),
    state TEXT NOT NULL CHECK(state IN ('active', 'suspended', 'cancelled', 'expired')),
    playbook TEXT NOT NULL CHECK(playbook IN ('recall_conversation')),
    completion_contract TEXT NOT NULL CHECK(completion_contract IN ('accepted_exact_owned_message_window')),
    active_frame_json TEXT NOT NULL CHECK(
        typeof(active_frame_json)='text'
        AND length(CAST(active_frame_json AS BLOB))<={WORK_ITEM_ACTIVE_FRAME_MAX_BYTES}
        AND json_valid(active_frame_json)
        AND json_type(active_frame_json)='object'
    ),
    anchor_user_message_id TEXT NOT NULL REFERENCES messages(id),
    anchor_assistant_message_id TEXT NOT NULL REFERENCES messages(id),
    accepted_plan_sha256 TEXT NOT NULL,
    accepted_outcome_sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
    transition TEXT NOT NULL CHECK(transition IN ('created', 'constraint_updated', 'suspended', 'cancelled', 'expired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK(length(id)=21 AND substr(id,1,5)='work_'
          AND substr(id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_user_message_id)=20 AND substr(anchor_user_message_id,1,4)='msg_'
          AND substr(anchor_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_assistant_message_id)=20
          AND substr(anchor_assistant_message_id,1,4)='msg_'
          AND substr(anchor_assistant_message_id,5) NOT GLOB '*[^0-9a-f]*'
          AND anchor_assistant_message_id<>anchor_user_message_id),
    CHECK(length(accepted_plan_sha256)=64
          AND accepted_plan_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(accepted_outcome_sha256)=64
          AND accepted_outcome_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(created_at) BETWEEN 20 AND 64
          AND length(updated_at) BETWEEN 20 AND 64
          AND length(expires_at) BETWEEN 20 AND 64
          AND (closed_at IS NULL OR length(closed_at) BETWEEN 20 AND 64)),
    CHECK(updated_at>=created_at),
    CHECK(unixepoch(updated_at) IS NOT NULL
          AND unixepoch(expires_at) IS NOT NULL
          AND unixepoch(expires_at)-unixepoch(updated_at)<={WORK_ITEM_TTL_HOURS * 60 * 60}),
    CHECK((state IN ('active','suspended') AND closed_at IS NULL)
          OR (state IN ('cancelled','expired') AND closed_at=updated_at)),
    CHECK((state IN ('active','suspended') AND expires_at>updated_at)
          OR state='cancelled'
          OR (state='expired' AND expires_at<=updated_at)),
    CHECK((state='active' AND transition IN ('created','constraint_updated'))
          OR (state='suspended' AND transition='suspended')
          OR (state='cancelled' AND transition='cancelled')
          OR (state='expired' AND transition='expired')),
    CHECK((transition='created' AND revision=1)
          OR (transition<>'created' AND revision>=2))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_items_active_conversation
    ON work_items(user_id, conversation_id) WHERE state='active';
CREATE INDEX IF NOT EXISTS idx_work_items_owner_state_updated
    ON work_items(user_id, state, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_conversation_updated
    ON work_items(user_id, conversation_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_expiry
    ON work_items(state, expires_at);
"""

_WORK_ITEM_SCHEMA_39 = f"""
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    kind TEXT NOT NULL CHECK(kind IN ({_SCHEMA_39_KIND_SQL})),
    goal TEXT NOT NULL CHECK(goal IN ({_SCHEMA_39_GOAL_SQL})),
    state TEXT NOT NULL CHECK(state IN ({_SCHEMA_39_STATE_SQL})),
    playbook TEXT NOT NULL CHECK(playbook IN ({_SCHEMA_39_PLAYBOOK_SQL})),
    completion_contract TEXT NOT NULL CHECK(completion_contract IN ({_SCHEMA_39_COMPLETION_SQL})),
    active_frame_json TEXT NOT NULL CHECK(
        typeof(active_frame_json)='text'
        AND length(CAST(active_frame_json AS BLOB))<={WORK_ITEM_ACTIVE_FRAME_MAX_BYTES}
        AND json_valid(active_frame_json)
        AND json_type(active_frame_json)='object'
    ),
    anchor_user_message_id TEXT NOT NULL REFERENCES messages(id),
    anchor_assistant_message_id TEXT NOT NULL REFERENCES messages(id),
    accepted_plan_sha256 TEXT NOT NULL,
    accepted_outcome_sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
    transition TEXT NOT NULL CHECK(transition IN ({_SCHEMA_39_TRANSITION_SQL})),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK(length(id)=21 AND substr(id,1,5)='work_'
          AND substr(id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_user_message_id)=20 AND substr(anchor_user_message_id,1,4)='msg_'
          AND substr(anchor_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_assistant_message_id)=20
          AND substr(anchor_assistant_message_id,1,4)='msg_'
          AND substr(anchor_assistant_message_id,5) NOT GLOB '*[^0-9a-f]*'
          AND anchor_assistant_message_id<>anchor_user_message_id),
    CHECK(length(accepted_plan_sha256)=64
          AND accepted_plan_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(accepted_outcome_sha256)=64
          AND accepted_outcome_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(created_at) BETWEEN 20 AND 64
          AND length(updated_at) BETWEEN 20 AND 64
          AND length(expires_at) BETWEEN 20 AND 64
          AND (closed_at IS NULL OR length(closed_at) BETWEEN 20 AND 64)),
    CHECK(updated_at>=created_at),
    CHECK(unixepoch(updated_at) IS NOT NULL
          AND unixepoch(expires_at) IS NOT NULL
          AND unixepoch(expires_at)-unixepoch(updated_at)<={WORK_ITEM_TTL_HOURS * 60 * 60}),
    CHECK((state IN ('active','suspended') AND closed_at IS NULL)
          OR (state IN ('cancelled','expired') AND closed_at=updated_at)),
    CHECK((state IN ('active','suspended') AND expires_at>updated_at)
          OR state='cancelled'
          OR (state='expired' AND expires_at<=updated_at)),
    CHECK((state='active' AND transition IN ('created','constraint_updated','evidence_replayed'))
          OR (state='suspended' AND transition='suspended')
          OR (state='cancelled' AND transition='cancelled')
          OR (state='expired' AND transition='expired')),
    CHECK((transition='created' AND revision=1)
          OR (transition<>'created' AND revision>=2)),
    CHECK(
        (kind='recall_conversation'
         AND goal='exact_current_conversation_recall'
         AND playbook='recall_conversation'
         AND completion_contract='accepted_exact_owned_message_window'
         AND transition<>'evidence_replayed')
        OR
        (kind='recall_selected_archive_evidence'
         AND goal='exact_selected_archive_evidence_recall'
         AND playbook='recall_selected_archive_evidence'
         AND completion_contract='accepted_exact_selected_archive_evidence'
         AND transition<>'constraint_updated')
    ),
    CHECK(kind<>'recall_selected_archive_evidence'
          OR active_frame_json='{RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON}')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_items_active_conversation
    ON work_items(user_id, conversation_id) WHERE state='active';
CREATE INDEX IF NOT EXISTS idx_work_items_owner_state_updated
    ON work_items(user_id, state, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_conversation_updated
    ON work_items(user_id, conversation_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_expiry
    ON work_items(state, expires_at);

CREATE TABLE IF NOT EXISTS work_item_selected_evidence (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id) ON DELETE CASCADE,
    corpus TEXT NOT NULL CHECK(corpus IN ('documents','knowledge','messages')),
    source_ref_json TEXT NOT NULL CHECK(
        typeof(source_ref_json)='text'
        AND length(CAST(source_ref_json AS BLOB)) BETWEEN 2 AND {WORK_ITEM_SELECTED_SOURCE_REF_MAX_BYTES}
        AND json_valid(source_ref_json)
        AND json_type(source_ref_json)='object'
    ),
    passage_refs_json TEXT NOT NULL CHECK(
        typeof(passage_refs_json)='text'
        AND length(CAST(passage_refs_json AS BLOB)) BETWEEN 3 AND {WORK_ITEM_SELECTED_PASSAGE_REFS_MAX_BYTES}
        AND json_valid(passage_refs_json)
        AND json_type(passage_refs_json)='array'
        AND json_array_length(passage_refs_json) BETWEEN 1 AND {WORK_ITEM_SELECTED_PASSAGE_MAX_COUNT}
    ),
    source_snapshot_sha256 TEXT NOT NULL CHECK(
        length(source_snapshot_sha256)=64
        AND source_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    coverage_sha256 TEXT NOT NULL CHECK(
        length(coverage_sha256)=64
        AND coverage_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    coverage_grade TEXT NOT NULL CHECK(coverage_grade IN ('complete','partial')),
    origin_boundary_user_message_id TEXT NOT NULL REFERENCES messages(id),
    CHECK(length(work_item_id)=21 AND substr(work_item_id,1,5)='work_'
          AND substr(work_item_id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(origin_boundary_user_message_id)=20
          AND substr(origin_boundary_user_message_id,1,4)='msg_'
          AND substr(origin_boundary_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(
        (corpus='documents'
         AND json_extract(source_ref_json,'$.source_kind')='document')
        OR
        (corpus='knowledge'
         AND json_extract(source_ref_json,'$.source_kind')
             IN ('document','web_capture','generated_artifact'))
        OR
        (corpus='messages'
         AND json_extract(source_ref_json,'$.source_kind')='conversation')
    )
);

CREATE INDEX IF NOT EXISTS idx_work_item_selected_evidence_origin_boundary
    ON work_item_selected_evidence(origin_boundary_user_message_id, work_item_id);

CREATE TRIGGER IF NOT EXISTS trg_work_item_selected_evidence_scope_insert
BEFORE INSERT ON work_item_selected_evidence
WHEN NOT EXISTS (
        SELECT 1
          FROM work_items work
          JOIN messages boundary
            ON boundary.id=NEW.origin_boundary_user_message_id
           AND boundary.user_id=work.user_id
           AND boundary.conversation_id=work.conversation_id
           AND boundary.role='user'
         WHERE work.id=NEW.work_item_id
           AND work.kind='recall_selected_archive_evidence'
           AND work.active_frame_json='{RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON}'
           AND work.anchor_user_message_id=NEW.origin_boundary_user_message_id
           AND json_extract(NEW.source_ref_json,'$.principal_id')=work.user_id
    )
    OR EXISTS (
        SELECT 1 FROM json_each(NEW.passage_refs_json) passage
         WHERE passage.type<>'object'
            OR json_extract(passage.value,'$.schema')<>'friday.passage-ref.private.v1'
            OR json(json_extract(passage.value,'$.source_ref'))<>json(NEW.source_ref_json)
    )
BEGIN
    SELECT RAISE(ABORT, 'selected archive evidence scope is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_selected_evidence_immutable
BEFORE UPDATE ON work_item_selected_evidence
BEGIN
    SELECT RAISE(ABORT, 'selected archive evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_items_workflow_identity_immutable
BEFORE UPDATE OF kind,goal,playbook,completion_contract ON work_items
WHEN NEW.kind<>OLD.kind
  OR NEW.goal<>OLD.goal
  OR NEW.playbook<>OLD.playbook
  OR NEW.completion_contract<>OLD.completion_contract
BEGIN
    SELECT RAISE(ABORT, 'work item workflow identity is immutable');
END;
"""

WORK_ITEM_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    kind TEXT NOT NULL CHECK(kind IN ({_KIND_SQL})),
    goal TEXT NOT NULL CHECK(goal IN ({_GOAL_SQL})),
    state TEXT NOT NULL CHECK(state IN ({_STATE_SQL})),
    playbook TEXT NOT NULL CHECK(playbook IN ({_PLAYBOOK_SQL})),
    completion_contract TEXT NOT NULL CHECK(completion_contract IN ({_COMPLETION_SQL})),
    active_frame_json TEXT NOT NULL CHECK(
        typeof(active_frame_json)='text'
        AND length(CAST(active_frame_json AS BLOB))<={WORK_ITEM_ACTIVE_FRAME_MAX_BYTES}
        AND json_valid(active_frame_json)
        AND json_type(active_frame_json)='object'
    ),
    anchor_user_message_id TEXT NOT NULL REFERENCES messages(id),
    anchor_assistant_message_id TEXT NOT NULL REFERENCES messages(id),
    accepted_plan_sha256 TEXT NOT NULL,
    accepted_outcome_sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
    transition TEXT NOT NULL CHECK(transition IN ({_TRANSITION_SQL})),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    CHECK(length(id)=21 AND substr(id,1,5)='work_'
          AND substr(id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_user_message_id)=20 AND substr(anchor_user_message_id,1,4)='msg_'
          AND substr(anchor_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_assistant_message_id)=20
          AND substr(anchor_assistant_message_id,1,4)='msg_'
          AND substr(anchor_assistant_message_id,5) NOT GLOB '*[^0-9a-f]*'
          AND anchor_assistant_message_id<>anchor_user_message_id),
    CHECK(length(accepted_plan_sha256)=64
          AND accepted_plan_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(accepted_outcome_sha256)=64
          AND accepted_outcome_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(created_at) BETWEEN 20 AND 64
          AND length(updated_at) BETWEEN 20 AND 64
          AND length(expires_at) BETWEEN 20 AND 64
          AND (closed_at IS NULL OR length(closed_at) BETWEEN 20 AND 64)),
    CHECK(updated_at>=created_at),
    CHECK(unixepoch(updated_at) IS NOT NULL
          AND unixepoch(expires_at) IS NOT NULL
          AND unixepoch(expires_at)-unixepoch(updated_at)<={WORK_ITEM_TTL_HOURS * 60 * 60}),
    CHECK((state IN ('active','waiting_for_input','suspended') AND closed_at IS NULL)
          OR (state IN ('completed','cancelled','expired') AND closed_at=updated_at)),
    CHECK((state IN ('active','waiting_for_input','suspended') AND expires_at>updated_at)
          OR (state='completed' AND expires_at>updated_at)
          OR state='cancelled'
          OR (state='expired' AND expires_at<=updated_at)),
    CHECK((state='active' AND transition IN ('created','constraint_updated','evidence_replayed'))
          OR (state='waiting_for_input' AND transition IN ('question_asked','question_reasked'))
          OR (state='completed' AND transition='candidate_replayed')
          OR (state='suspended' AND transition='suspended')
          OR (state='cancelled' AND transition='cancelled')
          OR (state='expired' AND transition='expired')),
    CHECK((transition IN ('created','question_asked') AND revision=1)
          OR (transition NOT IN ('created','question_asked') AND revision>=2)),
    CHECK(
        (kind='recall_conversation'
         AND goal='exact_current_conversation_recall'
         AND playbook='recall_conversation'
         AND completion_contract='accepted_exact_owned_message_window'
         AND state IN ('active','suspended','cancelled','expired')
         AND transition IN ('created','constraint_updated','suspended','cancelled','expired'))
        OR
        (kind='recall_selected_archive_evidence'
         AND goal='exact_selected_archive_evidence_recall'
         AND playbook='recall_selected_archive_evidence'
         AND completion_contract='accepted_exact_selected_archive_evidence'
         AND state IN ('active','suspended','cancelled','expired')
         AND transition IN ('created','evidence_replayed','suspended','cancelled','expired'))
        OR
        (kind='select_archive_candidate_and_replay_evidence'
         AND goal='exact_archive_candidate_selection_and_evidence_replay'
         AND playbook='select_archive_candidate_and_replay_evidence'
         AND completion_contract='accepted_exact_archive_candidate_and_evidence_replay'
         AND state IN ('waiting_for_input','completed','suspended','cancelled','expired')
         AND transition IN ('question_asked','question_reasked','candidate_replayed',
                            'suspended','cancelled','expired'))
    ),
    CHECK(kind<>'recall_selected_archive_evidence'
          OR active_frame_json='{RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON}'),
    CHECK(kind<>'select_archive_candidate_and_replay_evidence'
          OR active_frame_json='{ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON}')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_items_open_conversation
    ON work_items(user_id, conversation_id) WHERE state IN ('active','waiting_for_input');
CREATE INDEX IF NOT EXISTS idx_work_items_owner_state_updated
    ON work_items(user_id, state, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_conversation_updated
    ON work_items(user_id, conversation_id, updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_work_items_expiry
    ON work_items(state, expires_at);

CREATE TABLE IF NOT EXISTS work_item_selected_evidence (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id) ON DELETE CASCADE,
    corpus TEXT NOT NULL CHECK(corpus IN ('documents','knowledge','messages')),
    source_ref_json TEXT NOT NULL CHECK(
        typeof(source_ref_json)='text'
        AND length(CAST(source_ref_json AS BLOB)) BETWEEN 2 AND {WORK_ITEM_SELECTED_SOURCE_REF_MAX_BYTES}
        AND json_valid(source_ref_json)
        AND json_type(source_ref_json)='object'
    ),
    passage_refs_json TEXT NOT NULL CHECK(
        typeof(passage_refs_json)='text'
        AND length(CAST(passage_refs_json AS BLOB)) BETWEEN 3 AND {WORK_ITEM_SELECTED_PASSAGE_REFS_MAX_BYTES}
        AND json_valid(passage_refs_json)
        AND json_type(passage_refs_json)='array'
        AND json_array_length(passage_refs_json) BETWEEN 1 AND {WORK_ITEM_SELECTED_PASSAGE_MAX_COUNT}
    ),
    source_snapshot_sha256 TEXT NOT NULL CHECK(
        length(source_snapshot_sha256)=64
        AND source_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    coverage_sha256 TEXT NOT NULL CHECK(
        length(coverage_sha256)=64
        AND coverage_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    coverage_grade TEXT NOT NULL CHECK(coverage_grade IN ('complete','partial')),
    origin_boundary_user_message_id TEXT NOT NULL REFERENCES messages(id),
    CHECK(length(work_item_id)=21 AND substr(work_item_id,1,5)='work_'
          AND substr(work_item_id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(origin_boundary_user_message_id)=20
          AND substr(origin_boundary_user_message_id,1,4)='msg_'
          AND substr(origin_boundary_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(
        (corpus='documents'
         AND json_extract(source_ref_json,'$.source_kind')='document')
        OR
        (corpus='knowledge'
         AND json_extract(source_ref_json,'$.source_kind')
             IN ('document','web_capture','generated_artifact'))
        OR
        (corpus='messages'
         AND json_extract(source_ref_json,'$.source_kind')='conversation')
    )
);

CREATE INDEX IF NOT EXISTS idx_work_item_selected_evidence_origin_boundary
    ON work_item_selected_evidence(origin_boundary_user_message_id, work_item_id);

CREATE TABLE IF NOT EXISTS work_item_archive_candidate_sets (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL UNIQUE REFERENCES work_items(id) ON DELETE CASCADE,
    evidence_sha256 TEXT NOT NULL CHECK(
        length(evidence_sha256)=64 AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    coverage_sha256 TEXT NOT NULL CHECK(
        length(coverage_sha256)=64 AND coverage_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    coverage_grade TEXT NOT NULL CHECK(coverage_grade IN ('complete','partial')),
    authority_projection_sha256 TEXT NOT NULL CHECK(
        length(authority_projection_sha256)=64
        AND authority_projection_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    origin_boundary_user_message_id TEXT NOT NULL REFERENCES messages(id),
    candidate_set_sha256 TEXT NOT NULL CHECK(
        length(candidate_set_sha256)=64 AND candidate_set_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    UNIQUE(id, work_item_id),
    CHECK(length(id)=21 AND substr(id,1,5)='cset_'
          AND substr(id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(work_item_id)=21 AND substr(work_item_id,1,5)='work_'
          AND substr(work_item_id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(origin_boundary_user_message_id)=20
          AND substr(origin_boundary_user_message_id,1,4)='msg_'
          AND substr(origin_boundary_user_message_id,5) NOT GLOB '*[^0-9a-f]*')
);

CREATE INDEX IF NOT EXISTS idx_work_item_archive_candidate_sets_origin
    ON work_item_archive_candidate_sets(origin_boundary_user_message_id, work_item_id);

CREATE TABLE IF NOT EXISTS work_item_archive_candidate_set_items (
    candidate_set_id TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND {ARCHIVE_CANDIDATE_MAX_COUNT}),
    public_citation_label TEXT NOT NULL CHECK(
        length(public_citation_label) BETWEEN 2 AND 4
        AND substr(public_citation_label,1,1)='A'
        AND substr(public_citation_label,2) NOT GLOB '*[^0-9]*'
        AND CAST(substr(public_citation_label,2) AS INTEGER) BETWEEN 1 AND 640
        AND public_citation_label='A'||CAST(CAST(substr(public_citation_label,2) AS INTEGER) AS TEXT)
    ),
    corpus TEXT NOT NULL CHECK(corpus IN ('documents','knowledge','messages')),
    source_ref_json TEXT NOT NULL CHECK(
        typeof(source_ref_json)='text'
        AND length(CAST(source_ref_json AS BLOB)) BETWEEN 2 AND {WORK_ITEM_SELECTED_SOURCE_REF_MAX_BYTES}
        AND json_valid(source_ref_json)
        AND json_type(source_ref_json)='object'
    ),
    passage_refs_json TEXT NOT NULL CHECK(
        typeof(passage_refs_json)='text'
        AND length(CAST(passage_refs_json AS BLOB)) BETWEEN 3 AND {WORK_ITEM_SELECTED_PASSAGE_REFS_MAX_BYTES}
        AND json_valid(passage_refs_json)
        AND json_type(passage_refs_json)='array'
        AND json_array_length(passage_refs_json) BETWEEN 1 AND {WORK_ITEM_SELECTED_PASSAGE_MAX_COUNT}
    ),
    source_snapshot_sha256 TEXT NOT NULL CHECK(
        length(source_snapshot_sha256)=64
        AND source_snapshot_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY(candidate_set_id, ordinal),
    UNIQUE(candidate_set_id, source_ref_json),
    UNIQUE(candidate_set_id, public_citation_label),
    FOREIGN KEY(candidate_set_id, work_item_id)
        REFERENCES work_item_archive_candidate_sets(id, work_item_id) ON DELETE CASCADE,
    CHECK(length(candidate_set_id)=21 AND substr(candidate_set_id,1,5)='cset_'
          AND substr(candidate_set_id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(work_item_id)=21 AND substr(work_item_id,1,5)='work_'
          AND substr(work_item_id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(
        (corpus='documents'
         AND json_extract(source_ref_json,'$.source_kind')='document')
        OR
        (corpus='knowledge'
         AND json_extract(source_ref_json,'$.source_kind')
             IN ('document','web_capture','generated_artifact'))
        OR
        (corpus='messages'
         AND json_extract(source_ref_json,'$.source_kind')='conversation')
    )
);

CREATE INDEX IF NOT EXISTS idx_work_item_archive_candidate_items_work
    ON work_item_archive_candidate_set_items(work_item_id, candidate_set_id, ordinal);

CREATE TABLE IF NOT EXISTS work_item_archive_candidate_questions (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL UNIQUE,
    candidate_set_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind='select_archive_candidate_ordinal'),
    minimum_ordinal INTEGER NOT NULL CHECK(minimum_ordinal=1),
    maximum_ordinal INTEGER NOT NULL CHECK(
        maximum_ordinal BETWEEN 2 AND {ARCHIVE_CANDIDATE_MAX_COUNT}
    ),
    state TEXT NOT NULL CHECK(state IN ('waiting','answered')),
    selected_ordinal INTEGER,
    created_at TEXT NOT NULL,
    prompt_boundary_user_message_id TEXT NOT NULL REFERENCES messages(id),
    prompt_assistant_message_id TEXT NOT NULL REFERENCES messages(id),
    prompt_updated_at TEXT NOT NULL,
    prompt_revision INTEGER NOT NULL CHECK(prompt_revision BETWEEN 1 AND 2147483647),
    answered_at TEXT,
    replay_boundary_user_message_id TEXT REFERENCES messages(id),
    replay_assistant_message_id TEXT REFERENCES messages(id),
    accepted_replay_plan_sha256 TEXT,
    accepted_replay_outcome_sha256 TEXT,
    failed_ordinal INTEGER,
    failure_boundary_user_message_id TEXT REFERENCES messages(id),
    failure_assistant_message_id TEXT REFERENCES messages(id),
    failure_recorded_at TEXT,
    accepted_failure_plan_sha256 TEXT,
    accepted_failure_outcome_sha256 TEXT,
    FOREIGN KEY(candidate_set_id, work_item_id)
        REFERENCES work_item_archive_candidate_sets(id, work_item_id) ON DELETE CASCADE,
    CHECK(length(id)=25 AND substr(id,1,9)='question_'
          AND substr(id,10) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(work_item_id)=21 AND substr(work_item_id,1,5)='work_'
          AND substr(work_item_id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(candidate_set_id)=21 AND substr(candidate_set_id,1,5)='cset_'
          AND substr(candidate_set_id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(created_at) BETWEEN 20 AND 64
          AND length(prompt_updated_at) BETWEEN 20 AND 64
          AND (answered_at IS NULL OR length(answered_at) BETWEEN 20 AND 64)
          AND (failure_recorded_at IS NULL
               OR length(failure_recorded_at) BETWEEN 20 AND 64)),
    CHECK(unixepoch(created_at) IS NOT NULL
          AND unixepoch(prompt_updated_at) IS NOT NULL
          AND prompt_updated_at>=created_at
          AND (answered_at IS NULL OR unixepoch(answered_at) IS NOT NULL)
          AND (failure_recorded_at IS NULL
               OR (unixepoch(failure_recorded_at) IS NOT NULL
                   AND failure_recorded_at>=prompt_updated_at))),
    CHECK(length(prompt_boundary_user_message_id)=20
          AND substr(prompt_boundary_user_message_id,1,4)='msg_'
          AND substr(prompt_boundary_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(prompt_assistant_message_id)=20
          AND substr(prompt_assistant_message_id,1,4)='msg_'
          AND substr(prompt_assistant_message_id,5) NOT GLOB '*[^0-9a-f]*'
          AND prompt_assistant_message_id<>prompt_boundary_user_message_id),
    CHECK(replay_boundary_user_message_id IS NULL
          OR (length(replay_boundary_user_message_id)=20
              AND substr(replay_boundary_user_message_id,1,4)='msg_'
              AND substr(replay_boundary_user_message_id,5) NOT GLOB '*[^0-9a-f]*')),
    CHECK(replay_assistant_message_id IS NULL
          OR (length(replay_assistant_message_id)=20
              AND substr(replay_assistant_message_id,1,4)='msg_'
              AND substr(replay_assistant_message_id,5) NOT GLOB '*[^0-9a-f]*')),
    CHECK(accepted_replay_plan_sha256 IS NULL
          OR (length(accepted_replay_plan_sha256)=64
              AND accepted_replay_plan_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(accepted_replay_outcome_sha256 IS NULL
          OR (length(accepted_replay_outcome_sha256)=64
              AND accepted_replay_outcome_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(failure_boundary_user_message_id IS NULL
          OR (length(failure_boundary_user_message_id)=20
              AND substr(failure_boundary_user_message_id,1,4)='msg_'
              AND substr(failure_boundary_user_message_id,5) NOT GLOB '*[^0-9a-f]*')),
    CHECK(failure_assistant_message_id IS NULL
          OR (length(failure_assistant_message_id)=20
              AND substr(failure_assistant_message_id,1,4)='msg_'
              AND substr(failure_assistant_message_id,5) NOT GLOB '*[^0-9a-f]*')),
    CHECK(accepted_failure_plan_sha256 IS NULL
          OR (length(accepted_failure_plan_sha256)=64
              AND accepted_failure_plan_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(accepted_failure_outcome_sha256 IS NULL
          OR (length(accepted_failure_outcome_sha256)=64
              AND accepted_failure_outcome_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK((failed_ordinal IS NULL
           AND failure_boundary_user_message_id IS NULL
           AND failure_assistant_message_id IS NULL
           AND failure_recorded_at IS NULL
           AND accepted_failure_plan_sha256 IS NULL
           AND accepted_failure_outcome_sha256 IS NULL)
          OR (state='waiting'
              AND failed_ordinal BETWEEN minimum_ordinal AND maximum_ordinal
              AND failure_boundary_user_message_id IS NOT NULL
              AND failure_assistant_message_id IS NOT NULL
              AND failure_boundary_user_message_id<>failure_assistant_message_id
              AND failure_recorded_at IS NOT NULL
              AND accepted_failure_plan_sha256 IS NOT NULL
              AND accepted_failure_outcome_sha256 IS NOT NULL)),
    CHECK((state='waiting' AND selected_ordinal IS NULL AND answered_at IS NULL
           AND replay_boundary_user_message_id IS NULL
           AND replay_assistant_message_id IS NULL
           AND accepted_replay_plan_sha256 IS NULL
           AND accepted_replay_outcome_sha256 IS NULL)
          OR (state='answered'
              AND selected_ordinal BETWEEN minimum_ordinal AND maximum_ordinal
              AND answered_at>=created_at
              AND replay_boundary_user_message_id IS NOT NULL
              AND replay_assistant_message_id IS NOT NULL
              AND replay_boundary_user_message_id<>replay_assistant_message_id
              AND accepted_replay_plan_sha256 IS NOT NULL
              AND accepted_replay_outcome_sha256 IS NOT NULL
              AND failed_ordinal IS NULL
              AND failure_boundary_user_message_id IS NULL
              AND failure_assistant_message_id IS NULL
              AND failure_recorded_at IS NULL
              AND accepted_failure_plan_sha256 IS NULL
              AND accepted_failure_outcome_sha256 IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_work_item_archive_candidate_questions_work
    ON work_item_archive_candidate_questions(work_item_id, state, id);

CREATE TRIGGER IF NOT EXISTS trg_work_item_selected_evidence_scope_insert
BEFORE INSERT ON work_item_selected_evidence
WHEN NOT EXISTS (
        SELECT 1
          FROM work_items work
          JOIN messages boundary
            ON boundary.id=NEW.origin_boundary_user_message_id
           AND boundary.user_id=work.user_id
           AND boundary.conversation_id=work.conversation_id
           AND boundary.role='user'
         WHERE work.id=NEW.work_item_id
           AND work.kind='recall_selected_archive_evidence'
           AND work.active_frame_json='{RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON}'
           AND work.anchor_user_message_id=NEW.origin_boundary_user_message_id
           AND json_extract(NEW.source_ref_json,'$.principal_id')=work.user_id
    )
    OR EXISTS (
        SELECT 1 FROM json_each(NEW.passage_refs_json) passage
         WHERE passage.type<>'object'
            OR json_extract(passage.value,'$.schema')<>'friday.passage-ref.private.v1'
            OR json(json_extract(passage.value,'$.source_ref'))<>json(NEW.source_ref_json)
    )
BEGIN
    SELECT RAISE(ABORT, 'selected archive evidence scope is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_selected_evidence_immutable
BEFORE UPDATE ON work_item_selected_evidence
BEGIN
    SELECT RAISE(ABORT, 'selected archive evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_archive_candidate_sets_scope_insert
BEFORE INSERT ON work_item_archive_candidate_sets
WHEN NOT EXISTS (
        SELECT 1
          FROM work_items work
          JOIN messages boundary
            ON boundary.id=NEW.origin_boundary_user_message_id
           AND boundary.user_id=work.user_id
           AND boundary.conversation_id=work.conversation_id
           AND boundary.role='user'
         WHERE work.id=NEW.work_item_id
           AND work.kind='select_archive_candidate_and_replay_evidence'
           AND work.state='waiting_for_input'
           AND work.transition='question_asked'
           AND work.active_frame_json='{ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON}'
           AND work.anchor_user_message_id=NEW.origin_boundary_user_message_id
    )
BEGIN
    SELECT RAISE(ABORT, 'archive candidate set scope is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_archive_candidate_sets_immutable
BEFORE UPDATE ON work_item_archive_candidate_sets
BEGIN
    SELECT RAISE(ABORT, 'archive candidate set is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_archive_candidate_sets_delete_immutable
BEFORE DELETE ON work_item_archive_candidate_sets
WHEN EXISTS (SELECT 1 FROM work_items work WHERE work.id=OLD.work_item_id)
BEGIN
    SELECT RAISE(ABORT, 'archive candidate set deletion is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_archive_candidate_items_scope_insert
BEFORE INSERT ON work_item_archive_candidate_set_items
WHEN NOT EXISTS (
        SELECT 1
          FROM work_item_archive_candidate_sets candidate_set
          JOIN work_items work ON work.id=candidate_set.work_item_id
         WHERE candidate_set.id=NEW.candidate_set_id
           AND candidate_set.work_item_id=NEW.work_item_id
           AND work.kind='select_archive_candidate_and_replay_evidence'
           AND json_extract(NEW.source_ref_json,'$.principal_id')=work.user_id
    )
    OR EXISTS (
        SELECT 1 FROM json_each(NEW.passage_refs_json) passage
         WHERE passage.type<>'object'
            OR json_extract(passage.value,'$.schema')<>'friday.passage-ref.private.v1'
            OR json(json_extract(passage.value,'$.source_ref'))<>json(NEW.source_ref_json)
    )
    OR EXISTS (
        SELECT 1 FROM work_item_archive_candidate_questions question
         WHERE question.candidate_set_id=NEW.candidate_set_id
    )
BEGIN
    SELECT RAISE(ABORT, 'archive candidate item scope is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_archive_candidate_items_immutable
BEFORE UPDATE ON work_item_archive_candidate_set_items
BEGIN
    SELECT RAISE(ABORT, 'archive candidate item is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_archive_candidate_items_delete_immutable
BEFORE DELETE ON work_item_archive_candidate_set_items
WHEN EXISTS (
    SELECT 1 FROM work_item_archive_candidate_sets candidate_set
     WHERE candidate_set.id=OLD.candidate_set_id
       AND candidate_set.work_item_id=OLD.work_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'archive candidate item deletion is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_archive_candidate_questions_scope_insert
BEFORE INSERT ON work_item_archive_candidate_questions
WHEN NOT EXISTS (
        SELECT 1
          FROM work_item_archive_candidate_sets candidate_set
          JOIN work_items work ON work.id=candidate_set.work_item_id
         WHERE candidate_set.id=NEW.candidate_set_id
           AND candidate_set.work_item_id=NEW.work_item_id
           AND work.kind='select_archive_candidate_and_replay_evidence'
           AND work.state='waiting_for_input'
           AND work.transition='question_asked'
           AND work.revision=1
           AND work.created_at=NEW.created_at
           AND NEW.prompt_boundary_user_message_id=work.anchor_user_message_id
           AND NEW.prompt_assistant_message_id=work.anchor_assistant_message_id
           AND NEW.prompt_updated_at=work.created_at
    )
BEGIN
    SELECT RAISE(ABORT, 'archive candidate question scope is invalid');
END;

-- Keep each schema expression below SQLite's fixed parser-depth budget. The
-- three BEFORE INSERT guards are one contract: scope, pristine initial state,
-- and a complete contiguous candidate set.
CREATE TRIGGER IF NOT EXISTS trg_work_item_archive_candidate_questions_initial_insert
BEFORE INSERT ON work_item_archive_candidate_questions
WHEN NEW.prompt_revision<>1
  OR NEW.state<>'waiting'
  OR NEW.selected_ordinal IS NOT NULL
  OR NEW.answered_at IS NOT NULL
  OR NEW.replay_boundary_user_message_id IS NOT NULL
  OR NEW.replay_assistant_message_id IS NOT NULL
  OR NEW.accepted_replay_plan_sha256 IS NOT NULL
  OR NEW.accepted_replay_outcome_sha256 IS NOT NULL
  OR NEW.failed_ordinal IS NOT NULL
  OR NEW.failure_boundary_user_message_id IS NOT NULL
  OR NEW.failure_assistant_message_id IS NOT NULL
  OR NEW.failure_recorded_at IS NOT NULL
  OR NEW.accepted_failure_plan_sha256 IS NOT NULL
  OR NEW.accepted_failure_outcome_sha256 IS NOT NULL
  OR NEW.minimum_ordinal<>1
BEGIN
    SELECT RAISE(ABORT, 'archive candidate question scope is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_archive_candidate_questions_cardinality_insert
BEFORE INSERT ON work_item_archive_candidate_questions
WHEN NOT EXISTS (
        SELECT 1
          FROM work_item_archive_candidate_set_items item
         WHERE item.candidate_set_id=NEW.candidate_set_id
           AND item.work_item_id=NEW.work_item_id
         GROUP BY item.candidate_set_id,item.work_item_id
        HAVING COUNT(*)=NEW.maximum_ordinal
           AND MIN(item.ordinal)=1
           AND MAX(item.ordinal)=NEW.maximum_ordinal
    )
BEGIN
    SELECT RAISE(ABORT, 'archive candidate question scope is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_archive_candidate_questions_delete_immutable
BEFORE DELETE ON work_item_archive_candidate_questions
WHEN EXISTS (
    SELECT 1 FROM work_item_archive_candidate_sets candidate_set
     WHERE candidate_set.id=OLD.candidate_set_id
       AND candidate_set.work_item_id=OLD.work_item_id
)
BEGIN
    SELECT RAISE(ABORT, 'archive candidate question deletion is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_archive_candidate_questions_update
BEFORE UPDATE ON work_item_archive_candidate_questions
WHEN OLD.state<>'waiting'
  OR OLD.selected_ordinal IS NOT NULL
  OR OLD.answered_at IS NOT NULL
  OR OLD.failed_ordinal IS NOT NULL
  OR OLD.failure_boundary_user_message_id IS NOT NULL
  OR OLD.failure_assistant_message_id IS NOT NULL
  OR OLD.failure_recorded_at IS NOT NULL
  OR OLD.accepted_failure_plan_sha256 IS NOT NULL
  OR OLD.accepted_failure_outcome_sha256 IS NOT NULL
  OR NEW.id<>OLD.id
  OR NEW.work_item_id<>OLD.work_item_id
  OR NEW.candidate_set_id<>OLD.candidate_set_id
  OR NEW.kind<>OLD.kind
  OR NEW.minimum_ordinal<>OLD.minimum_ordinal
  OR NEW.maximum_ordinal<>OLD.maximum_ordinal
  OR NEW.created_at<>OLD.created_at
  OR NOT (
      (
          NEW.state='waiting'
          AND NEW.selected_ordinal IS NULL
          AND NEW.answered_at IS NULL
          AND NEW.replay_boundary_user_message_id IS NULL
          AND NEW.replay_assistant_message_id IS NULL
          AND NEW.accepted_replay_plan_sha256 IS NULL
          AND NEW.accepted_replay_outcome_sha256 IS NULL
          AND NEW.failed_ordinal IS NULL
          AND NEW.failure_boundary_user_message_id IS NULL
          AND NEW.failure_assistant_message_id IS NULL
          AND NEW.failure_recorded_at IS NULL
          AND NEW.accepted_failure_plan_sha256 IS NULL
          AND NEW.accepted_failure_outcome_sha256 IS NULL
          AND NEW.prompt_revision=OLD.prompt_revision+1
          AND NEW.prompt_updated_at>=OLD.prompt_updated_at
          AND NEW.prompt_assistant_message_id<>OLD.prompt_assistant_message_id
          AND EXISTS (
              SELECT 1
                FROM work_items work
                JOIN messages previous
                  ON previous.id=OLD.prompt_assistant_message_id
                 AND previous.user_id=work.user_id
                 AND previous.conversation_id=work.conversation_id
                 AND previous.role='assistant'
                JOIN messages boundary
                  ON boundary.id=NEW.prompt_boundary_user_message_id
                 AND boundary.user_id=work.user_id
                 AND boundary.conversation_id=work.conversation_id
                 AND boundary.role='user'
                 AND (friday_archive_candidate_ordinal(boundary.content) IS NULL
                      OR friday_archive_candidate_ordinal(boundary.content)
                         > NEW.maximum_ordinal)
                 AND previous.rowid<boundary.rowid
                 AND NOT EXISTS (
                     SELECT 1 FROM messages intervening
                      WHERE intervening.user_id=work.user_id
                        AND intervening.conversation_id=work.conversation_id
                        AND intervening.rowid>previous.rowid
                        AND intervening.rowid<boundary.rowid
                 )
                JOIN messages assistant
                  ON assistant.id=NEW.prompt_assistant_message_id
                 AND assistant.user_id=work.user_id
                 AND assistant.conversation_id=work.conversation_id
                 AND assistant.role='assistant'
                 AND assistant.reply_to=boundary.id
                 AND boundary.rowid<assistant.rowid
                 AND assistant.content=(
                     'Не распознал выбор. Ответьте только номером от 1 до '
                     || NEW.maximum_ordinal
                     || ' или одним порядковым словом (RU/EN).'
                 )
                 AND json_extract(assistant.metadata_json,
                                  '$.structural.verdict_kind')
                     ='archive_candidate_ordinal_reask'
                 AND json_extract(assistant.metadata_json,
                                  '$.structural.answer_present')=1
                 AND json_extract(assistant.metadata_json,
                                  '$.structural.model_spoke')=0
                 AND json_extract(assistant.metadata_json,
                                  '$.accepted_archive_recall_outcome') IS NULL
                 AND json_extract(assistant.metadata_json,
                                  '$.accepted_capability_outcome') IS NULL
                 AND json_extract(assistant.metadata_json,
                                  '$.accepted_simple_public_news_outcome') IS NULL
                 AND COALESCE(json_extract(assistant.metadata_json,
                                           '$.structural.model_spoke'),0)=0
                 AND NOT EXISTS (
                     SELECT 1 FROM messages intervening
                      WHERE intervening.user_id=work.user_id
                        AND intervening.conversation_id=work.conversation_id
                        AND intervening.rowid>boundary.rowid
                        AND intervening.rowid<assistant.rowid
                 )
               WHERE work.id=OLD.work_item_id
                 AND work.kind='select_archive_candidate_and_replay_evidence'
                 AND work.state='waiting_for_input'
                 AND work.transition IN ('question_asked','question_reasked')
                 AND work.revision=OLD.prompt_revision
                 AND NEW.prompt_revision=work.revision+1
                 AND work.updated_at<=NEW.prompt_updated_at
                 AND work.expires_at>NEW.prompt_updated_at
                 AND NOT EXISTS (
                     SELECT 1 FROM messages later
                      WHERE later.user_id=work.user_id
                        AND later.conversation_id=work.conversation_id
                        AND later.rowid>assistant.rowid
                 )
          )
      )
      OR
      (
          NEW.state='answered'
          AND NEW.selected_ordinal BETWEEN OLD.minimum_ordinal AND OLD.maximum_ordinal
          AND NEW.answered_at>=OLD.created_at
          AND NEW.prompt_boundary_user_message_id=OLD.prompt_boundary_user_message_id
          AND NEW.prompt_assistant_message_id=OLD.prompt_assistant_message_id
          AND NEW.prompt_updated_at=OLD.prompt_updated_at
          AND NEW.prompt_revision=OLD.prompt_revision
          AND NEW.replay_boundary_user_message_id IS NOT NULL
          AND NEW.replay_assistant_message_id IS NOT NULL
          AND NEW.replay_boundary_user_message_id<>NEW.replay_assistant_message_id
          AND NEW.accepted_replay_plan_sha256 IS NOT NULL
          AND NEW.accepted_replay_outcome_sha256 IS NOT NULL
          AND NEW.failed_ordinal IS NULL
          AND NEW.failure_boundary_user_message_id IS NULL
          AND NEW.failure_assistant_message_id IS NULL
          AND NEW.failure_recorded_at IS NULL
          AND NEW.accepted_failure_plan_sha256 IS NULL
          AND NEW.accepted_failure_outcome_sha256 IS NULL
          AND EXISTS (
              SELECT 1
                FROM work_items work
                JOIN messages previous
                  ON previous.id=OLD.prompt_assistant_message_id
                 AND previous.user_id=work.user_id
                 AND previous.conversation_id=work.conversation_id
                 AND previous.role='assistant'
                JOIN messages boundary
                  ON boundary.id=NEW.replay_boundary_user_message_id
                 AND boundary.user_id=work.user_id
                 AND boundary.conversation_id=work.conversation_id
                 AND boundary.role='user'
                 AND friday_archive_candidate_ordinal(boundary.content)
                     =NEW.selected_ordinal
                 AND previous.rowid<boundary.rowid
                 AND NOT EXISTS (
                     SELECT 1 FROM messages intervening
                      WHERE intervening.user_id=work.user_id
                        AND intervening.conversation_id=work.conversation_id
                        AND intervening.rowid>previous.rowid
                        AND intervening.rowid<boundary.rowid
                 )
                JOIN messages assistant
                  ON assistant.id=NEW.replay_assistant_message_id
                 AND assistant.user_id=work.user_id
                 AND assistant.conversation_id=work.conversation_id
                 AND assistant.role='assistant'
                 AND assistant.reply_to=boundary.id
                 AND boundary.rowid<assistant.rowid
                 AND NOT EXISTS (
                     SELECT 1 FROM messages intervening
                      WHERE intervening.user_id=work.user_id
                        AND intervening.conversation_id=work.conversation_id
                        AND intervening.rowid>boundary.rowid
                        AND intervening.rowid<assistant.rowid
                 )
               WHERE work.id=OLD.work_item_id
                 AND work.kind='select_archive_candidate_and_replay_evidence'
                 AND work.state='waiting_for_input'
                 AND work.transition IN ('question_asked','question_reasked')
                 AND work.revision=OLD.prompt_revision
                 AND work.updated_at<=NEW.answered_at
                 AND work.expires_at>NEW.answered_at
                 AND NOT EXISTS (
                     SELECT 1 FROM messages later
                      WHERE later.user_id=work.user_id
                        AND later.conversation_id=work.conversation_id
                        AND later.rowid>assistant.rowid
                 )
          )
      )
      OR
      (
          NEW.state='waiting'
          AND NEW.selected_ordinal IS NULL
          AND NEW.answered_at IS NULL
          AND NEW.replay_boundary_user_message_id IS NULL
          AND NEW.replay_assistant_message_id IS NULL
          AND NEW.accepted_replay_plan_sha256 IS NULL
          AND NEW.accepted_replay_outcome_sha256 IS NULL
          AND NEW.prompt_boundary_user_message_id=OLD.prompt_boundary_user_message_id
          AND NEW.prompt_assistant_message_id=OLD.prompt_assistant_message_id
          AND NEW.prompt_updated_at=OLD.prompt_updated_at
          AND NEW.prompt_revision=OLD.prompt_revision
          AND NEW.failed_ordinal BETWEEN OLD.minimum_ordinal AND OLD.maximum_ordinal
          AND NEW.failure_boundary_user_message_id IS NOT NULL
          AND NEW.failure_assistant_message_id IS NOT NULL
          AND NEW.failure_boundary_user_message_id<>NEW.failure_assistant_message_id
          AND NEW.failure_recorded_at>=OLD.prompt_updated_at
          AND NEW.accepted_failure_plan_sha256 IS NOT NULL
          AND NEW.accepted_failure_outcome_sha256 IS NOT NULL
          AND EXISTS (
              SELECT 1
                FROM work_items work
                JOIN messages previous
                  ON previous.id=OLD.prompt_assistant_message_id
                 AND previous.user_id=work.user_id
                 AND previous.conversation_id=work.conversation_id
                 AND previous.role='assistant'
                JOIN messages boundary
                  ON boundary.id=NEW.failure_boundary_user_message_id
                 AND boundary.user_id=work.user_id
                 AND boundary.conversation_id=work.conversation_id
                 AND boundary.role='user'
                 AND friday_archive_candidate_ordinal(boundary.content)
                     =NEW.failed_ordinal
                 AND previous.rowid<boundary.rowid
                 AND NOT EXISTS (
                     SELECT 1 FROM messages intervening
                      WHERE intervening.user_id=work.user_id
                        AND intervening.conversation_id=work.conversation_id
                        AND intervening.rowid>previous.rowid
                        AND intervening.rowid<boundary.rowid
                 )
                JOIN messages assistant
                  ON assistant.id=NEW.failure_assistant_message_id
                 AND assistant.user_id=work.user_id
                 AND assistant.conversation_id=work.conversation_id
                 AND assistant.role='assistant'
                 AND assistant.reply_to=boundary.id
                 AND boundary.rowid<assistant.rowid
                 AND assistant.content='{ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE}'
                 AND json_extract(assistant.metadata_json,
                                  '$.accepted_archive_recall_outcome') IS NOT NULL
                 AND json_extract(assistant.metadata_json,
                                  '$.accepted_capability_outcome') IS NULL
                 AND json_extract(assistant.metadata_json,
                                  '$.accepted_simple_public_news_outcome') IS NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM messages intervening
                      WHERE intervening.user_id=work.user_id
                        AND intervening.conversation_id=work.conversation_id
                        AND intervening.rowid>boundary.rowid
                        AND intervening.rowid<assistant.rowid
                 )
               WHERE work.id=OLD.work_item_id
                 AND work.kind='select_archive_candidate_and_replay_evidence'
                 AND work.state='waiting_for_input'
                 AND work.transition IN ('question_asked','question_reasked')
                 AND work.revision=OLD.prompt_revision
                 AND work.updated_at<=NEW.failure_recorded_at
                 AND work.expires_at>NEW.failure_recorded_at
                 AND NOT EXISTS (
                     SELECT 1 FROM messages later
                      WHERE later.user_id=work.user_id
                        AND later.conversation_id=work.conversation_id
                        AND later.rowid>assistant.rowid
                 )
          )
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'archive candidate question update is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_items_archive_candidate_lifecycle_update
BEFORE UPDATE ON work_items
WHEN OLD.kind='select_archive_candidate_and_replay_evidence'
 AND (
      NEW.id<>OLD.id
      OR NEW.user_id<>OLD.user_id
      OR NEW.conversation_id<>OLD.conversation_id
      OR NEW.kind<>OLD.kind
      OR NEW.goal<>OLD.goal
      OR NEW.playbook<>OLD.playbook
      OR NEW.completion_contract<>OLD.completion_contract
      OR NEW.active_frame_json<>OLD.active_frame_json
      OR NEW.anchor_user_message_id<>OLD.anchor_user_message_id
      OR NEW.anchor_assistant_message_id<>OLD.anchor_assistant_message_id
      OR NEW.accepted_plan_sha256<>OLD.accepted_plan_sha256
      OR NEW.accepted_outcome_sha256<>OLD.accepted_outcome_sha256
      OR NEW.created_at<>OLD.created_at
      OR (NEW.state='waiting_for_input' AND NEW.transition='question_reasked'
          AND unixepoch(NEW.expires_at)-unixepoch(NEW.updated_at)<>43200)
      OR (NOT (NEW.state='waiting_for_input' AND NEW.transition='question_reasked')
          AND NEW.expires_at<>OLD.expires_at)
      OR NEW.revision<>OLD.revision+1
      OR NEW.updated_at<OLD.updated_at
      OR NOT (
          (OLD.state='waiting_for_input'
           AND ((NEW.state='waiting_for_input' AND NEW.transition='question_reasked')
                OR (NEW.state='completed' AND NEW.transition='candidate_replayed')
                OR (NEW.state='suspended' AND NEW.transition='suspended')
                OR (NEW.state='cancelled' AND NEW.transition='cancelled')
                OR (NEW.state='expired' AND NEW.transition='expired')))
          OR
          (OLD.state='suspended'
           AND ((NEW.state='cancelled' AND NEW.transition='cancelled')
                OR (NEW.state='expired' AND NEW.transition='expired')))
      )
      OR (NEW.state='waiting_for_input' AND NOT EXISTS (
          SELECT 1 FROM work_item_archive_candidate_questions question
           WHERE question.work_item_id=OLD.id
             AND question.state='waiting'
             AND question.selected_ordinal IS NULL
             AND question.answered_at IS NULL
             AND question.failed_ordinal IS NULL
             AND question.failure_boundary_user_message_id IS NULL
             AND question.failure_assistant_message_id IS NULL
             AND question.failure_recorded_at IS NULL
             AND question.accepted_failure_plan_sha256 IS NULL
             AND question.accepted_failure_outcome_sha256 IS NULL
             AND question.prompt_revision=NEW.revision
             AND question.prompt_updated_at=NEW.updated_at
      ))
      OR (NEW.state='completed' AND NOT EXISTS (
          SELECT 1 FROM work_item_archive_candidate_questions question
           WHERE question.work_item_id=OLD.id
             AND question.state='answered'
             AND question.selected_ordinal BETWEEN question.minimum_ordinal
                                               AND question.maximum_ordinal
             AND question.answered_at=NEW.updated_at
      ))
      OR (NEW.state='suspended' AND EXISTS (
          SELECT 1 FROM work_item_archive_candidate_questions question
           WHERE question.work_item_id=OLD.id
             AND question.failed_ordinal IS NOT NULL
             AND (question.failure_recorded_at<>NEW.updated_at
                  OR question.prompt_revision<>OLD.revision)
      ))
      OR
      (NEW.state IN ('suspended','cancelled','expired')
       AND NOT EXISTS (
          SELECT 1 FROM work_item_archive_candidate_questions question
           WHERE question.work_item_id=OLD.id
             AND question.state='waiting'
             AND question.selected_ordinal IS NULL
             AND question.answered_at IS NULL
       ))
 )
BEGIN
    SELECT RAISE(ABORT, 'archive candidate Work Item lifecycle is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_items_workflow_identity_immutable
BEFORE UPDATE OF kind,goal,playbook,completion_contract ON work_items
WHEN NEW.kind<>OLD.kind
  OR NEW.goal<>OLD.goal
  OR NEW.playbook<>OLD.playbook
  OR NEW.completion_contract<>OLD.completion_contract
BEGIN
    SELECT RAISE(ABORT, 'work item workflow identity is immutable');
END;
"""


def _normalize_schema_sql(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _execute_schema(conn: sqlite3.Connection, schema: str) -> None:
    statement = ""
    for line in schema.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            statement = ""
            if sql:
                conn.execute(sql)
    if statement.strip():  # pragma: no cover - constants are complete SQL
        raise sqlite3.DatabaseError("Work Item schema contains an incomplete statement")


def _schema_objects_for_tables(
    conn: sqlite3.Connection,
    tables: tuple[str, ...],
) -> dict[tuple[str, str], str]:
    placeholders = ",".join("?" for _item in tables)
    return {
        (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
        for row in conn.execute(
            f"""SELECT type,name,sql FROM sqlite_master
                  WHERE sql IS NOT NULL
                    AND (name IN ({placeholders}) OR tbl_name IN ({placeholders}))
                  ORDER BY type,name""",  # nosec B608 - generated placeholders only
            (*tables, *tables),
        )
    }


def _schema_objects(conn: sqlite3.Connection, *, current: bool) -> dict[tuple[str, str], str]:
    return _schema_objects_for_tables(
        conn,
        _WORK_ITEM_TABLES if current else ("work_items",),
    )


@lru_cache(maxsize=1)
def _canonical_work_item_schema_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        _execute_schema(conn, WORK_ITEM_SCHEMA)
        return _schema_objects(conn, current=True)
    finally:
        conn.close()


@lru_cache(maxsize=1)
def _canonical_schema_38_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        _execute_schema(conn, _WORK_ITEM_SCHEMA_38)
        return _schema_objects(conn, current=False)
    finally:
        conn.close()


@lru_cache(maxsize=1)
def _canonical_schema_39_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        _execute_schema(conn, _WORK_ITEM_SCHEMA_39)
        return _schema_objects_for_tables(conn, _SCHEMA_39_TABLES)
    finally:
        conn.close()


def _related_schema_objects(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    return {
        (str(row[0]), str(row[1])): _normalize_schema_sql(str(row[2]))
        for row in conn.execute(
            """SELECT type,name,sql FROM sqlite_master
                  WHERE sql IS NOT NULL
                    AND (name='work_items'
                         OR name LIKE 'work_item_%'
                         OR name IN ('uq_work_items_active_conversation',
                                     'uq_work_items_open_conversation',
                                     'idx_work_items_owner_state_updated',
                                     'idx_work_items_conversation_updated',
                                     'idx_work_items_expiry')
                         OR tbl_name='work_items'
                         OR tbl_name LIKE 'work_item_%')
                  ORDER BY type,name""",
        )
    }


def register_work_item_connection_functions(conn: sqlite3.Connection) -> None:
    """Install deterministic UDFs required by persistent Work Item triggers.

    SQLite functions belong to a connection, not to the database image.  This
    registrar must therefore run for every application and offline connection,
    including thread-local connections that do not perform schema migration.
    """

    from friday.interaction_control_plane.archive_candidate_selection import (
        parse_archive_candidate_ordinal,
    )

    conn.create_function(
        "friday_archive_candidate_ordinal",
        1,
        parse_archive_candidate_ordinal,
        deterministic=True,
    )


def _validate_current_data(conn: sqlite3.Connection) -> None:
    # Retrieval contracts import storage-backed package exports. Delay that
    # dependency until schema bootstrap has completed module initialization.
    from friday.interaction_control_plane.archive_candidate_selection import (
        ARCHIVE_CANDIDATE_REASK_VERDICT_KIND,
        ArchiveCandidateOrdinalQuestion,
        ArchiveCandidateSelectionError,
        ArchiveCandidateSelectionWorkItem,
        ArchiveCandidateSet,
        archive_candidate_reask_prompt,
    )
    from friday.interaction_control_plane.archive_candidate_selection_store import (
        _validate_stored_anchor,
    )
    from friday.interaction_control_plane.archive_evidence_work_item import (
        RecallSelectedArchiveEvidenceWorkItem,
    )
    from friday.interaction_control_plane.selected_archive_evidence import (
        SelectedArchiveEvidence,
        SelectedArchiveEvidenceError,
    )
    from friday.interaction_control_plane.work_item_contract import (
        RecallConversationWorkItem,
        WorkItemContractError,
    )

    recall_cursor = conn.execute("SELECT * FROM work_items WHERE kind='recall_conversation' ORDER BY id")
    recall_columns = tuple(str(item[0]) for item in recall_cursor.description or ())
    for raw in recall_cursor.fetchall():
        row = dict(raw) if isinstance(raw, sqlite3.Row) else dict(zip(recall_columns, raw, strict=True))
        try:
            RecallConversationWorkItem.from_storage_row(row)
        except WorkItemContractError as exc:
            raise sqlite3.DatabaseError("Schema 40 RecallConversation data is invalid") from exc

    mismatch = conn.execute(
        """SELECT 1
             FROM work_items work
             LEFT JOIN work_item_selected_evidence evidence
               ON evidence.work_item_id=work.id
            WHERE (work.kind='recall_selected_archive_evidence' AND evidence.work_item_id IS NULL)
               OR (work.kind<>'recall_selected_archive_evidence' AND evidence.work_item_id IS NOT NULL)
            LIMIT 1"""
    ).fetchone()
    if mismatch is not None:
        raise sqlite3.DatabaseError("Schema 40 selected evidence cardinality is invalid")

    candidate_mismatch = conn.execute(
        """SELECT 1
             FROM work_items work
            WHERE (
                    work.kind='select_archive_candidate_and_replay_evidence'
                    AND ((SELECT COUNT(*) FROM work_item_archive_candidate_sets candidate_set
                           WHERE candidate_set.work_item_id=work.id)<>1
                         OR (SELECT COUNT(*) FROM work_item_archive_candidate_questions question
                              WHERE question.work_item_id=work.id)<>1
                         OR (SELECT COUNT(*)
                               FROM work_item_archive_candidate_set_items item
                              WHERE item.work_item_id=work.id) NOT BETWEEN 2 AND ?)
                  )
               OR (
                    work.kind<>'select_archive_candidate_and_replay_evidence'
                    AND ((SELECT COUNT(*) FROM work_item_archive_candidate_sets candidate_set
                           WHERE candidate_set.work_item_id=work.id)<>0
                         OR (SELECT COUNT(*) FROM work_item_archive_candidate_questions question
                              WHERE question.work_item_id=work.id)<>0
                         OR (SELECT COUNT(*)
                               FROM work_item_archive_candidate_set_items item
                              WHERE item.work_item_id=work.id)<>0)
                  )
            LIMIT 1""",
        (ARCHIVE_CANDIDATE_MAX_COUNT,),
    ).fetchone()
    if candidate_mismatch is not None:
        raise sqlite3.DatabaseError("Schema 40 candidate sidecar cardinality is invalid")
    candidate_orphan = conn.execute(
        """SELECT 1
              FROM work_item_archive_candidate_sets candidate_set
              LEFT JOIN work_items work ON work.id=candidate_set.work_item_id
             WHERE work.id IS NULL
                OR work.kind<>'select_archive_candidate_and_replay_evidence'
            UNION ALL
            SELECT 1
              FROM work_item_archive_candidate_set_items item
              LEFT JOIN work_item_archive_candidate_sets candidate_set
                ON candidate_set.id=item.candidate_set_id
               AND candidate_set.work_item_id=item.work_item_id
             WHERE candidate_set.id IS NULL
            UNION ALL
            SELECT 1
              FROM work_item_archive_candidate_questions question
              LEFT JOIN work_item_archive_candidate_sets candidate_set
                ON candidate_set.id=question.candidate_set_id
               AND candidate_set.work_item_id=question.work_item_id
             WHERE candidate_set.id IS NULL
             LIMIT 1"""
    ).fetchone()
    if candidate_orphan is not None:
        raise sqlite3.DatabaseError("Schema 40 candidate sidecar ownership is invalid")
    cursor = conn.execute("SELECT * FROM work_item_selected_evidence ORDER BY work_item_id")
    columns = tuple(str(item[0]) for item in cursor.description or ())
    for raw in cursor.fetchall():
        row = dict(raw) if isinstance(raw, sqlite3.Row) else dict(zip(columns, raw, strict=True))
        try:
            evidence = SelectedArchiveEvidence.from_storage_row(row)
        except SelectedArchiveEvidenceError as exc:
            raise sqlite3.DatabaseError("Schema 40 selected evidence identity is invalid") from exc
        work_cursor = conn.execute("SELECT * FROM work_items WHERE id=?", (evidence.work_item_id,))
        raw_work = work_cursor.fetchone()
        if raw_work is None:  # pragma: no cover - the sidecar FK already proves this
            raise sqlite3.DatabaseError("Schema 40 selected evidence owner is missing")
        work_columns = tuple(str(item[0]) for item in work_cursor.description or ())
        work = (
            dict(raw_work)
            if isinstance(raw_work, sqlite3.Row)
            else dict(zip(work_columns, raw_work, strict=True))
        )
        if evidence.source_ref.principal_id != work.get("user_id"):
            raise sqlite3.DatabaseError("Schema 40 selected evidence owner is invalid")
        try:
            RecallSelectedArchiveEvidenceWorkItem.from_storage_rows(work, evidence)
        except WorkItemContractError as exc:
            raise sqlite3.DatabaseError("Schema 40 archive Work Item data is invalid") from exc
        boundary = conn.execute(
            """SELECT 1
                 FROM work_items work
                 JOIN messages message
                   ON message.id=?
                  AND message.user_id=work.user_id
                  AND message.conversation_id=work.conversation_id
                  AND message.role='user'
                WHERE work.id=? AND work.kind='recall_selected_archive_evidence'""",
            (evidence.origin_boundary_user_message_id, evidence.work_item_id),
        ).fetchone()
        if boundary is None:
            raise sqlite3.DatabaseError("Schema 40 selected evidence boundary is invalid")

    work_cursor = conn.execute(
        """SELECT * FROM work_items
            WHERE kind='select_archive_candidate_and_replay_evidence' ORDER BY id"""
    )
    work_columns = tuple(str(item[0]) for item in work_cursor.description or ())
    for raw_work in work_cursor.fetchall():
        work = (
            dict(raw_work)
            if isinstance(raw_work, sqlite3.Row)
            else dict(zip(work_columns, raw_work, strict=True))
        )
        set_cursor = conn.execute(
            "SELECT * FROM work_item_archive_candidate_sets WHERE work_item_id=?",
            (work["id"],),
        )
        raw_set = set_cursor.fetchone()
        question_cursor = conn.execute(
            "SELECT * FROM work_item_archive_candidate_questions WHERE work_item_id=?",
            (work["id"],),
        )
        raw_question = question_cursor.fetchone()
        if raw_set is None or raw_question is None:  # pragma: no cover - cardinality proved above
            raise sqlite3.DatabaseError("Schema 40 candidate sidecar is missing")
        set_columns = tuple(str(item[0]) for item in set_cursor.description or ())
        set_row = (
            dict(raw_set)
            if isinstance(raw_set, sqlite3.Row)
            else dict(zip(set_columns, raw_set, strict=True))
        )
        item_cursor = conn.execute(
            """SELECT * FROM work_item_archive_candidate_set_items
                WHERE candidate_set_id=? AND work_item_id=? ORDER BY ordinal""",
            (set_row["id"], work["id"]),
        )
        item_columns = tuple(str(item[0]) for item in item_cursor.description or ())
        item_rows = [
            (dict(raw) if isinstance(raw, sqlite3.Row) else dict(zip(item_columns, raw, strict=True)))
            for raw in item_cursor.fetchall()
        ]
        question_columns = tuple(str(item[0]) for item in question_cursor.description or ())
        question_row = (
            dict(raw_question)
            if isinstance(raw_question, sqlite3.Row)
            else dict(zip(question_columns, raw_question, strict=True))
        )
        try:
            candidate_set = ArchiveCandidateSet.from_storage_rows(set_row, item_rows)
            question = ArchiveCandidateOrdinalQuestion.from_storage_row(question_row)
            item = ArchiveCandidateSelectionWorkItem.from_storage_rows(
                work,
                candidate_set,
                question,
            )
        except (ArchiveCandidateSelectionError, WorkItemContractError) as exc:
            raise sqlite3.DatabaseError("Schema 40 candidate Work Item data is invalid") from exc
        try:
            _validate_stored_anchor(
                conn,
                item,
                require_latest_message=False,
                allow_disabled_owner=True,
            )
        except Exception as exc:
            raise sqlite3.DatabaseError("Schema 40 candidate publication receipts are invalid") from exc
        if any(candidate.source_ref.principal_id != item.user_id for candidate in candidate_set.candidates):
            raise sqlite3.DatabaseError("Schema 40 candidate owner is invalid")
        boundary = conn.execute(
            """SELECT 1 FROM messages
                WHERE id=? AND user_id=? AND conversation_id=? AND role='user'""",
            (
                candidate_set.origin_boundary_user_message_id,
                item.user_id,
                item.conversation_id,
            ),
        ).fetchone()
        if boundary is None:
            raise sqlite3.DatabaseError("Schema 40 candidate origin boundary is invalid")
        if question.prompt_assistant_message_id != item.anchor_assistant_message_id:
            prompt = conn.execute(
                """SELECT 1
                     FROM messages origin
                     JOIN messages boundary
                       ON boundary.id=?
                      AND boundary.user_id=origin.user_id
                      AND boundary.conversation_id=origin.conversation_id
                      AND boundary.role='user'
                      AND origin.rowid<boundary.rowid
                     JOIN messages assistant
                       ON assistant.id=?
                      AND assistant.user_id=origin.user_id
                      AND assistant.conversation_id=origin.conversation_id
                      AND assistant.role='assistant'
                      AND assistant.reply_to=boundary.id
                      AND boundary.rowid<assistant.rowid
                      AND assistant.content=?
                      AND json_extract(assistant.metadata_json,
                                       '$.structural.verdict_kind')=?
                      AND json_extract(assistant.metadata_json,
                                       '$.structural.answer_present')=1
                      AND json_extract(assistant.metadata_json,
                                       '$.structural.model_spoke')=0
                      AND json_extract(assistant.metadata_json,
                                       '$.accepted_archive_recall_outcome') IS NULL
                      AND json_extract(assistant.metadata_json,
                                       '$.accepted_capability_outcome') IS NULL
                      AND json_extract(assistant.metadata_json,
                                       '$.accepted_simple_public_news_outcome') IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM messages intervening
                           WHERE intervening.user_id=origin.user_id
                             AND intervening.conversation_id=origin.conversation_id
                             AND intervening.rowid>boundary.rowid
                             AND intervening.rowid<assistant.rowid
                      )
                    WHERE origin.id=? AND origin.user_id=?
                      AND origin.conversation_id=? AND origin.role='assistant'""",
                (
                    question.prompt_boundary_user_message_id,
                    question.prompt_assistant_message_id,
                    archive_candidate_reask_prompt(question.maximum_ordinal),
                    ARCHIVE_CANDIDATE_REASK_VERDICT_KIND,
                    item.anchor_assistant_message_id,
                    item.user_id,
                    item.conversation_id,
                ),
            ).fetchone()
            if prompt is None:
                raise sqlite3.DatabaseError("Schema 40 candidate re-ask boundary is invalid")
        if question.state.value == "answered":
            replay = conn.execute(
                """SELECT 1
                     FROM messages boundary
                     JOIN messages assistant
                       ON assistant.id=?
                      AND assistant.user_id=boundary.user_id
                      AND assistant.conversation_id=boundary.conversation_id
                      AND assistant.role='assistant'
                      AND assistant.reply_to=boundary.id
                      AND boundary.rowid<assistant.rowid
                      AND NOT EXISTS (
                          SELECT 1 FROM messages intervening
                           WHERE intervening.user_id=boundary.user_id
                             AND intervening.conversation_id=boundary.conversation_id
                             AND intervening.rowid>boundary.rowid
                             AND intervening.rowid<assistant.rowid
                      )
                    WHERE boundary.id=? AND boundary.user_id=?
                      AND boundary.conversation_id=? AND boundary.role='user'""",
                (
                    question.replay_assistant_message_id,
                    question.replay_boundary_user_message_id,
                    item.user_id,
                    item.conversation_id,
                ),
            ).fetchone()
            if replay is None:
                raise sqlite3.DatabaseError("Schema 40 candidate replay boundary is invalid")
        if question.has_replay_failure_receipt:
            failure = conn.execute(
                """SELECT 1
                     FROM messages previous
                     JOIN messages boundary
                       ON boundary.id=?
                      AND boundary.user_id=previous.user_id
                      AND boundary.conversation_id=previous.conversation_id
                      AND boundary.role='user'
                      AND previous.rowid<boundary.rowid
                      AND NOT EXISTS (
                          SELECT 1 FROM messages intervening
                           WHERE intervening.user_id=previous.user_id
                             AND intervening.conversation_id=previous.conversation_id
                             AND intervening.rowid>previous.rowid
                             AND intervening.rowid<boundary.rowid
                      )
                     JOIN messages assistant
                       ON assistant.id=?
                      AND assistant.user_id=previous.user_id
                      AND assistant.conversation_id=previous.conversation_id
                      AND assistant.role='assistant'
                      AND assistant.reply_to=boundary.id
                      AND boundary.rowid<assistant.rowid
                      AND assistant.content=?
                      AND json_extract(assistant.metadata_json,
                                       '$.accepted_archive_recall_outcome') IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM messages intervening
                           WHERE intervening.user_id=previous.user_id
                             AND intervening.conversation_id=previous.conversation_id
                             AND intervening.rowid>boundary.rowid
                             AND intervening.rowid<assistant.rowid
                      )
                    WHERE previous.id=? AND previous.user_id=?
                      AND previous.conversation_id=? AND previous.role='assistant'""",
                (
                    question.failure_boundary_user_message_id,
                    question.failure_assistant_message_id,
                    ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE,
                    question.prompt_assistant_message_id,
                    item.user_id,
                    item.conversation_id,
                ),
            ).fetchone()
            if failure is None:
                raise sqlite3.DatabaseError("Schema 40 candidate replay failure boundary is invalid")


def validate_work_item_schema(conn: sqlite3.Connection, *, required: bool = True) -> None:
    """Fail closed when the exact schema-40 Work Item projection is weakened."""

    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_items'").fetchone()
    if row is None:
        if required:
            raise sqlite3.DatabaseError("Schema 40 work item store is missing")
        if _related_schema_objects(conn):
            raise sqlite3.DatabaseError("Schema 40 work item DDL is incomplete or altered")
        return
    register_work_item_connection_functions(conn)
    if _schema_objects(conn, current=True) != _canonical_work_item_schema_objects():
        raise sqlite3.DatabaseError("Schema 40 work item DDL is incomplete or altered")

    expected_index_columns = {
        "uq_work_items_open_conversation": ("user_id", "conversation_id"),
        "idx_work_items_owner_state_updated": ("user_id", "state", "updated_at", "id"),
        "idx_work_items_conversation_updated": ("user_id", "conversation_id", "updated_at", "id"),
        "idx_work_items_expiry": ("state", "expires_at"),
        "idx_work_item_selected_evidence_origin_boundary": (
            "origin_boundary_user_message_id",
            "work_item_id",
        ),
        "idx_work_item_archive_candidate_sets_origin": (
            "origin_boundary_user_message_id",
            "work_item_id",
        ),
        "idx_work_item_archive_candidate_items_work": (
            "work_item_id",
            "candidate_set_id",
            "ordinal",
        ),
        "idx_work_item_archive_candidate_questions_work": (
            "work_item_id",
            "state",
            "id",
        ),
    }
    named_index_columns = {
        name: tuple(str(column[2]) for column in conn.execute(f'PRAGMA index_info("{name}")'))
        for name in expected_index_columns
    }
    if named_index_columns != expected_index_columns:
        raise sqlite3.DatabaseError("Schema 40 work item indexes are invalid")

    columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(work_items)")
    }
    if columns != {
        "id": ("TEXT", 0, 1),
        "user_id": ("TEXT", 1, 0),
        "conversation_id": ("TEXT", 1, 0),
        "kind": ("TEXT", 1, 0),
        "goal": ("TEXT", 1, 0),
        "state": ("TEXT", 1, 0),
        "playbook": ("TEXT", 1, 0),
        "completion_contract": ("TEXT", 1, 0),
        "active_frame_json": ("TEXT", 1, 0),
        "anchor_user_message_id": ("TEXT", 1, 0),
        "anchor_assistant_message_id": ("TEXT", 1, 0),
        "accepted_plan_sha256": ("TEXT", 1, 0),
        "accepted_outcome_sha256": ("TEXT", 1, 0),
        "revision": ("INTEGER", 1, 0),
        "transition": ("TEXT", 1, 0),
        "created_at": ("TEXT", 1, 0),
        "updated_at": ("TEXT", 1, 0),
        "expires_at": ("TEXT", 1, 0),
        "closed_at": ("TEXT", 0, 0),
    }:
        raise sqlite3.DatabaseError("Schema 40 work item store shape is invalid")
    evidence_columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(work_item_selected_evidence)")
    }
    if evidence_columns != {
        "work_item_id": ("TEXT", 0, 1),
        "corpus": ("TEXT", 1, 0),
        "source_ref_json": ("TEXT", 1, 0),
        "passage_refs_json": ("TEXT", 1, 0),
        "source_snapshot_sha256": ("TEXT", 1, 0),
        "coverage_sha256": ("TEXT", 1, 0),
        "coverage_grade": ("TEXT", 1, 0),
        "origin_boundary_user_message_id": ("TEXT", 1, 0),
    }:
        raise sqlite3.DatabaseError("Schema 40 selected evidence store shape is invalid")

    work_item_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(work_items)")
    }
    if work_item_foreign_keys != {
        ("user_id", "users", "id", "NO ACTION", "NO ACTION"),
        ("conversation_id", "conversations", "id", "NO ACTION", "NO ACTION"),
        ("anchor_user_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
        ("anchor_assistant_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
    }:
        raise sqlite3.DatabaseError("Schema 40 work item ownership anchors are invalid")
    evidence_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(work_item_selected_evidence)")
    }
    if evidence_foreign_keys != {
        ("work_item_id", "work_items", "id", "NO ACTION", "CASCADE"),
        ("origin_boundary_user_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
    }:
        raise sqlite3.DatabaseError("Schema 40 selected evidence foreign keys are invalid")
    _validate_current_data(conn)


def _drop_legacy_schema_objects(
    conn: sqlite3.Connection,
    objects: dict[tuple[str, str], str],
) -> None:
    for (kind, name), _sql in objects.items():
        if kind in {"index", "trigger"}:
            conn.execute(f'DROP {kind.upper()} "{name}"')


def _copy_work_items(conn: sqlite3.Connection, source_table: str) -> None:
    if source_table not in {"work_items_schema38", "work_items_schema39"}:
        raise sqlite3.DatabaseError("Work Item migration source is invalid")
    conn.execute(
        """INSERT INTO work_items(
               id,user_id,conversation_id,kind,goal,state,playbook,
               completion_contract,active_frame_json,anchor_user_message_id,
               anchor_assistant_message_id,accepted_plan_sha256,
               accepted_outcome_sha256,revision,transition,created_at,
               updated_at,expires_at,closed_at
           )
           SELECT id,user_id,conversation_id,kind,goal,state,playbook,
                  completion_contract,active_frame_json,anchor_user_message_id,
                  anchor_assistant_message_id,accepted_plan_sha256,
                  accepted_outcome_sha256,revision,transition,created_at,
                  updated_at,expires_at,closed_at
             FROM """
        + f'"{source_table}"'  # nosec B608 - source is closed above
    )


def upgrade_work_item_schema_to_40(
    conn: sqlite3.Connection,
    *,
    required: bool,
) -> None:
    """Authenticate and atomically rebuild only exact released schemas 38/39."""

    if not conn.in_transaction:
        raise RuntimeError("Work Item schema upgrade requires an existing transaction")
    related = _related_schema_objects(conn)
    if not related:
        if required:
            raise sqlite3.DatabaseError("Work Item store is missing")
        return
    if _schema_objects(conn, current=True) == _canonical_work_item_schema_objects():
        validate_work_item_schema(conn)
        return

    installed_39 = _schema_objects_for_tables(conn, _SCHEMA_39_TABLES)
    if installed_39 == _canonical_schema_39_objects() and related == installed_39:
        conn.execute("ALTER TABLE work_items RENAME TO work_items_schema39")
        conn.execute("ALTER TABLE work_item_selected_evidence RENAME TO work_item_selected_evidence_schema39")
        _drop_legacy_schema_objects(conn, _canonical_schema_39_objects())
        _execute_schema(conn, WORK_ITEM_SCHEMA)
        _copy_work_items(conn, "work_items_schema39")
        conn.execute(
            """INSERT INTO work_item_selected_evidence(
                   work_item_id,corpus,source_ref_json,passage_refs_json,
                   source_snapshot_sha256,coverage_sha256,coverage_grade,
                   origin_boundary_user_message_id
               )
               SELECT work_item_id,corpus,source_ref_json,passage_refs_json,
                      source_snapshot_sha256,coverage_sha256,coverage_grade,
                      origin_boundary_user_message_id
                 FROM work_item_selected_evidence_schema39"""
        )
        conn.execute("DROP TABLE work_item_selected_evidence_schema39")
        conn.execute("DROP TABLE work_items_schema39")
    else:
        installed_38 = _schema_objects_for_tables(conn, ("work_items",))
        if installed_38 != _canonical_schema_38_objects() or related != installed_38:
            raise sqlite3.DatabaseError("Released Work Item DDL is incomplete or altered")
        conn.execute("ALTER TABLE work_items RENAME TO work_items_schema38")
        _drop_legacy_schema_objects(conn, _canonical_schema_38_objects())
        _execute_schema(conn, WORK_ITEM_SCHEMA)
        _copy_work_items(conn, "work_items_schema38")
        conn.execute("DROP TABLE work_items_schema38")
    validate_work_item_schema(conn)


__all__ = [
    "WORK_ITEM_SCHEMA",
    "WORK_ITEM_SCHEMA_VERSION",
    "register_work_item_connection_functions",
    "upgrade_work_item_schema_to_40",
    "validate_work_item_schema",
]
