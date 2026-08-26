"""Exact schema-45 projection for bounded durable interaction Work Items/graphs."""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache

from friday.interaction_control_plane.compare_current_file_web_work_graph import (
    COMPARE_CURRENT_FILE_WEB_ARCHIVED_RESPONSE,
    COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE,
    COMPARE_CURRENT_FILE_WEB_COMPLETION_CONTRACT,
    COMPARE_CURRENT_FILE_WEB_EXPIRED_RESPONSE,
    COMPARE_CURRENT_FILE_WEB_FALLBACK_OWNER,
    COMPARE_CURRENT_FILE_WEB_MAX_ACTIVE_REVISION,
    COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS,
    COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY,
    COMPARE_CURRENT_FILE_WEB_PUBLICATION_OWNER,
    COMPARE_CURRENT_FILE_WEB_PUBLICATION_RECEIPT_SCHEMA,
    COMPARE_CURRENT_FILE_WEB_RESTART_UNAVAILABLE_RESPONSE,
    COMPARE_CURRENT_FILE_WEB_STEP_OUTCOME_SCHEMA,
    COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY,
    COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_RECEIPT_SCHEMA,
    COMPARE_CURRENT_FILE_WEB_UNBOUND_SCHEMA44_REQUEST_SHA256,
    WEB_SEARCH_CURRENT_ADAPTER_ID,
    WEB_SEARCH_CURRENT_SECURITY_ID,
)
from friday.interaction_control_plane.work_item_contract import (
    ARCHIVE_CANDIDATE_MAX_COUNT,
    ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON,
    COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_JSON,
    COMPARE_CONVERSATION_DOCUMENT_ANSWER_MAX_BYTES,
    COMPARE_DOCUMENT_REFERENCE_PROMPT,
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

WORK_ITEM_SCHEMA_VERSION = 45
WORK_ITEM_SELECTED_SOURCE_REF_MAX_BYTES = 4_096
WORK_ITEM_SELECTED_PASSAGE_REFS_MAX_BYTES = 65_536
WORK_ITEM_SELECTED_PASSAGE_MAX_COUNT = 8

_SCHEMA_42_TABLES = (
    "work_item_compare_outcomes",
    "work_item_compare_document_evidence",
    "work_item_compare_document_questions",
    "work_item_archive_candidate_questions",
    "work_item_archive_candidate_set_items",
    "work_item_archive_candidate_sets",
    "work_item_selected_evidence",
    "work_items",
)
_WORK_ITEM_TABLES = (
    "work_item_compare_current_file_web_restart_rebind_steps",
    "work_item_compare_current_file_web_restart_rebinds",
    "work_item_compare_current_file_web_steps",
    "work_item_compare_current_file_web_graphs",
    *_SCHEMA_42_TABLES,
)
_SCHEMA_42_INDEXES = (
    "idx_work_item_compare_outcomes_assistant",
    "idx_work_item_compare_document_evidence_origin",
    "uq_work_item_compare_document_question_waiting",
    "idx_work_item_compare_document_questions_work",
    "idx_work_item_archive_candidate_items_work",
    "idx_work_item_archive_candidate_questions_work",
    "idx_work_item_archive_candidate_sets_origin",
    "idx_work_item_selected_evidence_origin_boundary",
    "idx_work_items_conversation_updated",
    "idx_work_items_expiry",
    "idx_work_items_owner_state_updated",
    "uq_work_items_open_conversation",
)
_WORK_ITEM_INDEXES = (
    "idx_work_item_compare_current_file_web_steps_state",
    "idx_work_item_compare_current_file_web_graphs_expiry",
    "idx_work_item_compare_current_file_web_graphs_owner",
    "uq_work_item_compare_current_file_web_graphs_assistant",
    "uq_work_item_compare_current_file_web_graphs_active",
    *_SCHEMA_42_INDEXES,
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
_SCHEMA_40_TABLES = (
    "work_item_archive_candidate_questions",
    "work_item_archive_candidate_set_items",
    "work_item_archive_candidate_sets",
    "work_item_selected_evidence",
    "work_items",
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
_SCHEMA_40_KIND_SQL = _sql_values(
    (
        WorkKind.RECALL_CONVERSATION,
        WorkKind.RECALL_SELECTED_ARCHIVE_EVIDENCE,
        WorkKind.SELECT_ARCHIVE_CANDIDATE_AND_REPLAY_EVIDENCE,
    )
)
_SCHEMA_40_GOAL_SQL = _sql_values(
    (
        WorkGoal.EXACT_CURRENT_CONVERSATION_RECALL,
        WorkGoal.EXACT_SELECTED_ARCHIVE_EVIDENCE_RECALL,
        WorkGoal.EXACT_ARCHIVE_CANDIDATE_SELECTION_AND_EVIDENCE_REPLAY,
    )
)
_SCHEMA_40_STATE_SQL = _sql_values(
    (
        WorkState.ACTIVE,
        WorkState.WAITING_FOR_INPUT,
        WorkState.COMPLETED,
        WorkState.SUSPENDED,
        WorkState.CANCELLED,
        WorkState.EXPIRED,
    )
)
_SCHEMA_40_PLAYBOOK_SQL = _sql_values(
    (
        WorkPlaybook.RECALL_CONVERSATION,
        WorkPlaybook.RECALL_SELECTED_ARCHIVE_EVIDENCE,
        WorkPlaybook.SELECT_ARCHIVE_CANDIDATE_AND_REPLAY_EVIDENCE,
    )
)
_SCHEMA_40_COMPLETION_SQL = _sql_values(
    (
        WorkCompletionContract.ACCEPTED_EXACT_OWNED_MESSAGE_WINDOW,
        WorkCompletionContract.ACCEPTED_EXACT_SELECTED_ARCHIVE_EVIDENCE,
        WorkCompletionContract.ACCEPTED_EXACT_ARCHIVE_CANDIDATE_AND_EVIDENCE_REPLAY,
    )
)
_SCHEMA_40_TRANSITION_SQL = _sql_values(
    (
        WorkTransition.CREATED,
        WorkTransition.QUESTION_ASKED,
        WorkTransition.QUESTION_REASKED,
        WorkTransition.CANDIDATE_REPLAYED,
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

_WORK_ITEM_SCHEMA_40 = f"""
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    kind TEXT NOT NULL CHECK(kind IN ({_SCHEMA_40_KIND_SQL})),
    goal TEXT NOT NULL CHECK(goal IN ({_SCHEMA_40_GOAL_SQL})),
    state TEXT NOT NULL CHECK(state IN ({_SCHEMA_40_STATE_SQL})),
    playbook TEXT NOT NULL CHECK(playbook IN ({_SCHEMA_40_PLAYBOOK_SQL})),
    completion_contract TEXT NOT NULL CHECK(completion_contract IN ({_SCHEMA_40_COMPLETION_SQL})),
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
    transition TEXT NOT NULL CHECK(transition IN ({_SCHEMA_40_TRANSITION_SQL})),
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


def _schema_42_full_from_40() -> str:
    """Expand the exact schema-40 image with the complete dormant reader shape."""

    candidate_message_receipt_sql = """AND json_extract(assistant.metadata_json,
                             '$.accepted_archive_recall_outcome.schema')
                ='friday.accepted-archive-recall-outcome-receipt.v1'
            AND json_extract(assistant.metadata_json,
                             '$.accepted_archive_recall_outcome.outcome.plan_sha256')
                =NEW.accepted_search_plan_sha256
            AND json_extract(assistant.metadata_json,
                             '$.accepted_archive_recall_outcome.outcome_sha256')
                =NEW.accepted_search_outcome_sha256"""
    candidate_set_receipt_sql = """AND json_extract(assistant.metadata_json,
                                    '$.accepted_archive_recall_outcome.outcome.lane')
                       ='federated_search'
                   AND json_extract(assistant.metadata_json,
                                    '$.accepted_archive_recall_outcome.outcome.status')
                       IN ('complete','partial')
                   AND json_extract(assistant.metadata_json,
                                    '$.accepted_archive_recall_outcome.outcome.evidence_sha256')
                       =candidate_set.evidence_sha256
                   AND json_extract(assistant.metadata_json,
                                    '$.accepted_archive_recall_outcome.outcome.coverage_sha256')
                       =candidate_set.coverage_sha256
                   AND json_extract(assistant.metadata_json,
                                    '$.accepted_archive_recall_outcome.outcome.coverage_grade')
                       =candidate_set.coverage_grade
                   AND json_extract(assistant.metadata_json,
                                    '$.accepted_archive_recall_outcome.outcome.candidate_projection_sha256')
                       =candidate_set.authority_projection_sha256
                   AND json_extract(assistant.metadata_json,
                                    '$.accepted_archive_recall_outcome.outcome.selected_evidence')
                       IS NULL"""
    schema = _WORK_ITEM_SCHEMA_40
    replacements = (
        (
            f"kind TEXT NOT NULL CHECK(kind IN ({_SCHEMA_40_KIND_SQL}))",
            f"kind TEXT NOT NULL CHECK(kind IN ({_KIND_SQL}))",
        ),
        (
            f"goal TEXT NOT NULL CHECK(goal IN ({_SCHEMA_40_GOAL_SQL}))",
            f"goal TEXT NOT NULL CHECK(goal IN ({_GOAL_SQL}))",
        ),
        (
            f"playbook TEXT NOT NULL CHECK(playbook IN ({_SCHEMA_40_PLAYBOOK_SQL}))",
            f"playbook TEXT NOT NULL CHECK(playbook IN ({_PLAYBOOK_SQL}))",
        ),
        (
            f"completion_contract TEXT NOT NULL CHECK(completion_contract IN ({_SCHEMA_40_COMPLETION_SQL}))",
            f"completion_contract TEXT NOT NULL CHECK(completion_contract IN ({_COMPLETION_SQL}))",
        ),
        (
            f"transition TEXT NOT NULL CHECK(transition IN ({_SCHEMA_40_TRANSITION_SQL}))",
            f"transition TEXT NOT NULL CHECK(transition IN ({_TRANSITION_SQL}))",
        ),
        (
            """    CHECK((state='active' AND transition IN ('created','constraint_updated','evidence_replayed'))
          OR (state='waiting_for_input' AND transition IN ('question_asked','question_reasked'))
          OR (state='completed' AND transition='candidate_replayed')
""",
            """    CHECK((state='active' AND transition IN ('created','constraint_updated','evidence_replayed',
                                                    'document_resolved'))
          OR (state='waiting_for_input' AND transition IN ('question_asked','question_reasked'))
          OR (state='completed' AND transition IN ('candidate_replayed','comparison_published'))
""",
        ),
        (
            """        (kind='select_archive_candidate_and_replay_evidence'
         AND goal='exact_archive_candidate_selection_and_evidence_replay'
         AND playbook='select_archive_candidate_and_replay_evidence'
         AND completion_contract='accepted_exact_archive_candidate_and_evidence_replay'
         AND state IN ('waiting_for_input','completed','suspended','cancelled','expired')
         AND transition IN ('question_asked','question_reasked','candidate_replayed',
                            'suspended','cancelled','expired'))
""",
            """        (kind='select_archive_candidate_and_replay_evidence'
         AND goal='exact_archive_candidate_selection_and_evidence_replay'
         AND playbook='select_archive_candidate_and_replay_evidence'
         AND completion_contract='accepted_exact_archive_candidate_and_evidence_replay'
         AND state IN ('waiting_for_input','completed','suspended','cancelled','expired')
         AND transition IN ('question_asked','question_reasked','candidate_replayed',
                            'suspended','cancelled','expired'))
        OR
        (kind='compare_conversation_with_document'
         AND goal='compare_exact_message_evidence_with_document'
         AND playbook='compare_conversation_with_document'
         AND completion_contract='accepted_exact_message_and_document_comparison'
         AND state IN ('waiting_for_input','active','completed','suspended','cancelled','expired')
         AND transition IN ('question_asked','question_reasked','document_resolved',
                            'comparison_published','suspended','cancelled','expired'))
""",
        ),
        (
            f"""    CHECK(kind<>'select_archive_candidate_and_replay_evidence'
          OR active_frame_json='{ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON}')
""",
            f"""    CHECK(kind<>'select_archive_candidate_and_replay_evidence'
          OR active_frame_json='{ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON}'),
    CHECK(kind<>'compare_conversation_with_document'
          OR active_frame_json='{COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_JSON}')
""",
        ),
        (
            f"""         WHERE work.id=NEW.work_item_id
           AND work.kind='recall_selected_archive_evidence'
           AND work.active_frame_json='{RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON}'
           AND work.anchor_user_message_id=NEW.origin_boundary_user_message_id
           AND json_extract(NEW.source_ref_json,'$.principal_id')=work.user_id
""",
            f"""         WHERE work.id=NEW.work_item_id
           AND (
                (work.kind='recall_selected_archive_evidence'
                 AND work.active_frame_json='{RECALL_SELECTED_ARCHIVE_EVIDENCE_ACTIVE_FRAME_JSON}'
                 AND work.anchor_user_message_id=NEW.origin_boundary_user_message_id)
                OR
                (work.kind='compare_conversation_with_document'
                 AND work.state='waiting_for_input'
                 AND work.transition='question_asked'
                 AND work.revision=1
                 AND work.active_frame_json='{COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_JSON}'
                 AND work.anchor_user_message_id=NEW.origin_boundary_user_message_id
                 AND NEW.corpus='messages')
           )
           AND json_extract(NEW.source_ref_json,'$.principal_id')=work.user_id
""",
        ),
        (
            f"""         WHERE work.id=NEW.work_item_id
           AND work.kind='select_archive_candidate_and_replay_evidence'
           AND work.state='waiting_for_input'
           AND work.transition='question_asked'
           AND work.active_frame_json='{ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON}'
           AND work.anchor_user_message_id=NEW.origin_boundary_user_message_id
""",
            f"""         WHERE work.id=NEW.work_item_id
           AND (
                (work.kind='select_archive_candidate_and_replay_evidence'
                 AND work.state='waiting_for_input'
                 AND work.transition='question_asked'
                 AND work.active_frame_json='{ARCHIVE_CANDIDATE_SELECTION_ACTIVE_FRAME_JSON}'
                 AND work.anchor_user_message_id=NEW.origin_boundary_user_message_id)
                OR
                (work.kind='compare_conversation_with_document'
                 AND work.state='waiting_for_input'
                 AND work.transition='question_asked'
                 AND work.revision=1
                 AND work.active_frame_json='{COMPARE_CONVERSATION_DOCUMENT_ACTIVE_FRAME_JSON}'
                 AND EXISTS (
                     SELECT 1 FROM work_item_compare_document_questions question
                      WHERE question.work_item_id=work.id
                        AND question.work_revision=1
                        AND question.state='answered'
                        AND question.answer_user_message_id=NEW.origin_boundary_user_message_id
                 ))
           )
""",
        ),
        (
            """           AND work.kind='select_archive_candidate_and_replay_evidence'
           AND json_extract(NEW.source_ref_json,'$.principal_id')=work.user_id
""",
            """           AND work.kind IN ('select_archive_candidate_and_replay_evidence',
                            'compare_conversation_with_document')
           AND json_extract(NEW.source_ref_json,'$.principal_id')=work.user_id
           AND (work.kind<>'compare_conversation_with_document'
                OR (NEW.corpus='documents'
                    AND json_type(NEW.source_ref_json,'$.tenant_id')='text'
                    AND NOT EXISTS (
                        SELECT 1 FROM work_item_archive_candidate_set_items sibling
                         WHERE sibling.candidate_set_id=NEW.candidate_set_id
                           AND sibling.work_item_id=NEW.work_item_id
                           AND json_extract(sibling.source_ref_json,'$.tenant_id')
                               <>json_extract(NEW.source_ref_json,'$.tenant_id')
                    )))
""",
        ),
    )
    for old, new in replacements:
        if schema.count(old) != 1:
            raise RuntimeError("released schema-40 transformation anchor is ambiguous")
        schema = schema.replace(old, new)

    return (
        schema
        + f"""

CREATE TABLE IF NOT EXISTS work_item_compare_document_questions (
    id TEXT PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('provide_document_reference','select_document_candidate')),
    admission_shape TEXT NOT NULL CHECK(admission_shape IN ('direct_compound','selected_evidence_followup')),
    state TEXT NOT NULL CHECK(state IN ('waiting','answered','closed')),
    created_at TEXT NOT NULL,
    prompt_boundary_user_message_id TEXT NOT NULL REFERENCES messages(id),
    prompt_assistant_message_id TEXT NOT NULL REFERENCES messages(id),
    work_revision INTEGER NOT NULL CHECK(work_revision BETWEEN 1 AND 2),
    candidate_set_id TEXT,
    answered_at TEXT,
    answer_user_message_id TEXT REFERENCES messages(id),
    selected_ordinal INTEGER CHECK(selected_ordinal BETWEEN 1 AND {ARCHIVE_CANDIDATE_MAX_COUNT}),
    accepted_search_plan_sha256 TEXT CHECK(
        accepted_search_plan_sha256 IS NULL
        OR (length(accepted_search_plan_sha256)=64
            AND accepted_search_plan_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    accepted_search_outcome_sha256 TEXT CHECK(
        accepted_search_outcome_sha256 IS NULL
        OR (length(accepted_search_outcome_sha256)=64
            AND accepted_search_outcome_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    closed_at TEXT,
    close_reason TEXT CHECK(close_reason IN ('answered','suspended','cancelled','expired')),
    UNIQUE(work_item_id,work_revision),
    FOREIGN KEY(candidate_set_id,work_item_id)
        REFERENCES work_item_archive_candidate_sets(id,work_item_id),
    CHECK(length(id)=25 AND substr(id,1,9)='question_'
          AND substr(id,10) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(work_item_id)=21 AND substr(work_item_id,1,5)='work_'
          AND substr(work_item_id,6) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(prompt_boundary_user_message_id)=20
          AND substr(prompt_boundary_user_message_id,1,4)='msg_'
          AND substr(prompt_boundary_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(prompt_assistant_message_id)=20
          AND substr(prompt_assistant_message_id,1,4)='msg_'
          AND substr(prompt_assistant_message_id,5) NOT GLOB '*[^0-9a-f]*'
          AND prompt_assistant_message_id<>prompt_boundary_user_message_id),
    CHECK(length(created_at) BETWEEN 20 AND 64 AND unixepoch(created_at) IS NOT NULL),
    CHECK((kind='provide_document_reference' AND work_revision=1
           AND candidate_set_id IS NULL AND selected_ordinal IS NULL
           AND accepted_search_plan_sha256 IS NULL
           AND accepted_search_outcome_sha256 IS NULL)
          OR (kind='select_document_candidate' AND work_revision=2
              AND candidate_set_id IS NOT NULL
              AND accepted_search_plan_sha256 IS NOT NULL
              AND accepted_search_outcome_sha256 IS NOT NULL)),
    CHECK((state='waiting' AND answered_at IS NULL AND answer_user_message_id IS NULL
           AND selected_ordinal IS NULL AND closed_at IS NULL AND close_reason IS NULL)
          OR (state='answered' AND answered_at IS NOT NULL
              AND answer_user_message_id IS NOT NULL AND closed_at=answered_at
              AND close_reason='answered'
              AND ((kind='provide_document_reference' AND selected_ordinal IS NULL)
                   OR (kind='select_document_candidate' AND selected_ordinal IS NOT NULL)))
          OR (state='closed' AND answered_at IS NULL AND answer_user_message_id IS NULL
              AND selected_ordinal IS NULL AND closed_at IS NOT NULL
              AND close_reason IN ('suspended','cancelled','expired'))),
    CHECK((answered_at IS NULL OR (length(answered_at) BETWEEN 20 AND 64
                                   AND unixepoch(answered_at) IS NOT NULL))
          AND (closed_at IS NULL OR (length(closed_at) BETWEEN 20 AND 64
                                     AND unixepoch(closed_at) IS NOT NULL
                                     AND closed_at>=created_at)))
);

CREATE INDEX IF NOT EXISTS idx_work_item_compare_document_questions_work
    ON work_item_compare_document_questions(work_item_id,state,id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_work_item_compare_document_question_waiting
    ON work_item_compare_document_questions(work_item_id) WHERE state='waiting';

CREATE TABLE IF NOT EXISTS work_item_compare_document_evidence (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id) ON DELETE CASCADE,
    provenance TEXT NOT NULL CHECK(provenance IN (
        'current_turn_attachment','historical_exact_reference','historical_candidate_ordinal'
    )),
    source_ref_json TEXT NOT NULL CHECK(
        typeof(source_ref_json)='text'
        AND length(CAST(source_ref_json AS BLOB)) BETWEEN 2 AND {WORK_ITEM_SELECTED_SOURCE_REF_MAX_BYTES}
        AND json_valid(source_ref_json) AND json_type(source_ref_json)='object'
    ),
    raw_object_id TEXT NOT NULL REFERENCES raw_objects(id),
    raw_source_identity_sha256 TEXT NOT NULL CHECK(
        length(raw_source_identity_sha256)=64
        AND raw_source_identity_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    raw_content_sha256 TEXT NOT NULL CHECK(
        length(raw_content_sha256)=64 AND raw_content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    content_sha256 TEXT NOT NULL CHECK(
        length(content_sha256)=64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    candidate_source_snapshot_sha256 TEXT CHECK(
        candidate_source_snapshot_sha256 IS NULL
        OR (length(candidate_source_snapshot_sha256)=64
            AND candidate_source_snapshot_sha256 NOT GLOB '*[^0-9a-f]*')
    ),
    origin_boundary_user_message_id TEXT NOT NULL REFERENCES messages(id),
    resolved_revision INTEGER NOT NULL CHECK(resolved_revision BETWEEN 2 AND 3),
    resolved_at TEXT NOT NULL CHECK(length(resolved_at) BETWEEN 20 AND 64
                                    AND unixepoch(resolved_at) IS NOT NULL),
    candidate_set_id TEXT,
    selected_ordinal INTEGER CHECK(selected_ordinal BETWEEN 1 AND {ARCHIVE_CANDIDATE_MAX_COUNT}),
    FOREIGN KEY(candidate_set_id,work_item_id)
        REFERENCES work_item_archive_candidate_sets(id,work_item_id),
    CHECK(length(raw_object_id)=20 AND substr(raw_object_id,1,4)='raw_'
          AND substr(raw_object_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(json_extract(source_ref_json,'$.canonical_object_kind')='raw_object'
          AND json_extract(source_ref_json,'$.canonical_object_id')=raw_object_id
          AND json_extract(source_ref_json,'$.source_kind')
              IN ('document','web_capture','generated_artifact')
          AND json_extract(source_ref_json,'$.authority_scope')='tenant_principal'),
    CHECK((provenance='historical_candidate_ordinal'
           AND candidate_set_id IS NOT NULL AND selected_ordinal IS NOT NULL
           AND candidate_source_snapshot_sha256 IS NOT NULL)
          OR (provenance<>'historical_candidate_ordinal'
              AND candidate_set_id IS NULL AND selected_ordinal IS NULL
              AND candidate_source_snapshot_sha256 IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_work_item_compare_document_evidence_origin
    ON work_item_compare_document_evidence(origin_boundary_user_message_id,work_item_id);

CREATE TABLE IF NOT EXISTS work_item_compare_outcomes (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id) ON DELETE CASCADE,
    answer_boundary_user_message_id TEXT NOT NULL REFERENCES messages(id),
    answer_assistant_message_id TEXT NOT NULL REFERENCES messages(id),
    accepted_plan_sha256 TEXT NOT NULL,
    accepted_outcome_sha256 TEXT NOT NULL,
    comparison_status TEXT NOT NULL CHECK(comparison_status IN ('complete','partial')),
    message_coverage_grade TEXT NOT NULL CHECK(message_coverage_grade IN ('complete','partial')),
    document_verification_complete INTEGER NOT NULL CHECK(document_verification_complete=1),
    publication_attested INTEGER NOT NULL CHECK(publication_attested=1),
    semantic_verified INTEGER NOT NULL CHECK(semantic_verified=1),
    message_evidence_sha256 TEXT NOT NULL,
    document_evidence_sha256 TEXT NOT NULL,
    evidence_bundle_sha256 TEXT NOT NULL,
    model_evidence_sha256 TEXT NOT NULL,
    completed_revision INTEGER NOT NULL CHECK(completed_revision BETWEEN 3 AND 4),
    completed_at TEXT NOT NULL,
    CHECK(answer_boundary_user_message_id<>answer_assistant_message_id),
    CHECK(comparison_status=message_coverage_grade),
    CHECK(length(answer_boundary_user_message_id)=20
          AND substr(answer_boundary_user_message_id,1,4)='msg_'
          AND substr(answer_boundary_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(answer_assistant_message_id)=20
          AND substr(answer_assistant_message_id,1,4)='msg_'
          AND substr(answer_assistant_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(accepted_plan_sha256)=64 AND accepted_plan_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(accepted_outcome_sha256)=64 AND accepted_outcome_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(message_evidence_sha256)=64 AND message_evidence_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(document_evidence_sha256)=64 AND document_evidence_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(evidence_bundle_sha256)=64 AND evidence_bundle_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(model_evidence_sha256)=64
          AND model_evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(completed_at) BETWEEN 20 AND 64 AND unixepoch(completed_at) IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_work_item_compare_outcomes_assistant
    ON work_item_compare_outcomes(answer_assistant_message_id);

CREATE TRIGGER IF NOT EXISTS trg_work_items_compare_document_initial_insert
BEFORE INSERT ON work_items
WHEN NEW.kind='compare_conversation_with_document'
 AND (NEW.state<>'waiting_for_input' OR NEW.transition<>'question_asked'
      OR NEW.revision<>1 OR NEW.closed_at IS NOT NULL
      OR NEW.created_at<>NEW.updated_at OR NEW.expires_at<=NEW.updated_at)
BEGIN SELECT RAISE(ABORT,'comparison Work Item initial state is invalid'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_questions_insert
BEFORE INSERT ON work_item_compare_document_questions
WHEN NOT EXISTS (
    SELECT 1
      FROM work_items work
      JOIN work_item_selected_evidence evidence ON evidence.work_item_id=work.id
      JOIN messages boundary ON boundary.id=NEW.prompt_boundary_user_message_id
                            AND boundary.user_id=work.user_id
                            AND boundary.conversation_id=work.conversation_id
                            AND boundary.role='user'
      JOIN messages assistant ON assistant.id=NEW.prompt_assistant_message_id
                             AND assistant.user_id=work.user_id
                             AND assistant.conversation_id=work.conversation_id
                             AND assistant.role='assistant'
                             AND assistant.reply_to=boundary.id
                             AND NOT EXISTS (
                                 SELECT 1 FROM messages intervening
                                  WHERE intervening.user_id=work.user_id
                                    AND intervening.conversation_id=work.conversation_id
                                    AND intervening.rowid>boundary.rowid
                                    AND intervening.rowid<assistant.rowid
                             )
     WHERE work.id=NEW.work_item_id
       AND work.kind='compare_conversation_with_document'
       AND work.state='waiting_for_input'
       AND evidence.corpus='messages'
       AND (
           (NEW.kind='provide_document_reference' AND NEW.work_revision=1
            AND work.revision=1 AND work.transition='question_asked'
            AND json_extract(assistant.metadata_json,'$.structural.answer_present')=1
            AND json_extract(assistant.metadata_json,'$.structural.model_spoke')=0
            AND work.created_at=NEW.created_at
            AND json_extract(assistant.metadata_json,'$.structural.verdict_kind')
                ='compare_conversation_document_reference_required'
            AND ((NEW.admission_shape='direct_compound'
                  AND NEW.prompt_boundary_user_message_id=work.anchor_user_message_id
                 AND NEW.prompt_assistant_message_id=work.anchor_assistant_message_id)
                 OR (NEW.admission_shape='selected_evidence_followup'
                     AND assistant.content='{COMPARE_DOCUMENT_REFERENCE_PROMPT}'
                     AND NOT EXISTS (
                         SELECT 1 FROM json_each(assistant.metadata_json) receipt
                          WHERE receipt.key GLOB 'accepted_*_outcome'
                     )
                     AND EXISTS (
                         SELECT 1 FROM messages origin
                          WHERE origin.id=work.anchor_assistant_message_id
                            AND origin.user_id=work.user_id
                            AND origin.conversation_id=work.conversation_id
                            AND origin.role='assistant' AND origin.rowid<boundary.rowid
                            AND NOT EXISTS (
                                SELECT 1 FROM messages intervening
                                 WHERE intervening.user_id=work.user_id
                                   AND intervening.conversation_id=work.conversation_id
                                   AND intervening.rowid>origin.rowid
                                   AND intervening.rowid<boundary.rowid
                            )
                     ))))
           OR
           (NEW.kind='select_document_candidate' AND NEW.work_revision=2
            AND work.revision=1 AND work.transition='question_asked'
            {candidate_message_receipt_sql}
            AND EXISTS (
                SELECT 1 FROM work_item_compare_document_questions first
                 WHERE first.work_item_id=work.id AND first.work_revision=1
                   AND first.state='answered'
                   AND first.admission_shape=NEW.admission_shape
                   AND first.answer_user_message_id=NEW.prompt_boundary_user_message_id
                   AND NEW.created_at>=first.closed_at
            )
            AND EXISTS (
                SELECT 1 FROM work_item_archive_candidate_sets candidate_set
                 WHERE candidate_set.id=NEW.candidate_set_id
                   AND candidate_set.work_item_id=work.id
                   AND candidate_set.origin_boundary_user_message_id
                       =NEW.prompt_boundary_user_message_id
                   {candidate_set_receipt_sql}
                   AND (SELECT COUNT(*) FROM work_item_archive_candidate_set_items item
                         WHERE item.candidate_set_id=candidate_set.id
                           AND item.work_item_id=work.id) BETWEEN 2 AND {ARCHIVE_CANDIDATE_MAX_COUNT}
            ))
       )
)
BEGIN
    SELECT RAISE(ABORT,'comparison document question scope is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_questions_update
BEFORE UPDATE ON work_item_compare_document_questions
WHEN OLD.state<>'waiting'
  OR NEW.id<>OLD.id OR NEW.work_item_id<>OLD.work_item_id OR NEW.kind<>OLD.kind
  OR NEW.admission_shape<>OLD.admission_shape OR NEW.created_at<>OLD.created_at
  OR NEW.prompt_boundary_user_message_id<>OLD.prompt_boundary_user_message_id
  OR NEW.prompt_assistant_message_id<>OLD.prompt_assistant_message_id
  OR NEW.work_revision<>OLD.work_revision
  OR NEW.accepted_search_plan_sha256 IS NOT OLD.accepted_search_plan_sha256
  OR NEW.accepted_search_outcome_sha256 IS NOT OLD.accepted_search_outcome_sha256
  OR NOT (
      (NEW.state='answered' AND NEW.close_reason='answered'
       AND NEW.answered_at=NEW.closed_at AND NEW.answer_user_message_id IS NOT NULL
       AND ((NEW.kind='provide_document_reference' AND NEW.selected_ordinal IS NULL)
            OR (NEW.kind='select_document_candidate' AND NEW.selected_ordinal IS NOT NULL
                AND EXISTS (
                    SELECT 1 FROM work_item_archive_candidate_set_items candidate
                     WHERE candidate.candidate_set_id=NEW.candidate_set_id
                       AND candidate.work_item_id=NEW.work_item_id
                       AND candidate.ordinal=NEW.selected_ordinal
                )
                AND EXISTS (
                    SELECT 1 FROM messages answer
                      JOIN work_items work ON work.id=NEW.work_item_id
                     WHERE answer.id=NEW.answer_user_message_id
                       AND answer.user_id=work.user_id
                       AND answer.conversation_id=work.conversation_id
                       AND answer.role='user'
                       AND friday_archive_candidate_ordinal(answer.content)=NEW.selected_ordinal
                ))))
      OR
      (NEW.state='closed' AND NEW.close_reason IN ('suspended','cancelled','expired')
       AND NEW.closed_at IS NOT NULL AND NEW.answered_at IS NULL
       AND NEW.answer_user_message_id IS NULL AND NEW.selected_ordinal IS NULL)
  )
  OR NOT EXISTS (
      SELECT 1 FROM work_items work
       WHERE work.id=OLD.work_item_id
         AND work.kind='compare_conversation_with_document'
         AND work.state='waiting_for_input'
         AND work.revision=OLD.work_revision
  )
BEGIN
    SELECT RAISE(ABORT,'comparison document question update is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_questions_delete
BEFORE DELETE ON work_item_compare_document_questions
WHEN EXISTS (SELECT 1 FROM work_items work WHERE work.id=OLD.work_item_id)
BEGIN SELECT RAISE(ABORT,'comparison document question is immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_document_evidence_insert
BEFORE INSERT ON work_item_compare_document_evidence
WHEN NOT EXISTS (
    SELECT 1 FROM work_items work
      JOIN raw_objects raw ON raw.id=NEW.raw_object_id
                          AND raw.user_id=json_extract(NEW.source_ref_json,'$.tenant_id')
                          AND raw.deleted_at IS NULL
      JOIN file_source_aliases alias ON alias.user_id=raw.user_id
                                    AND alias.uploaded_by=work.user_id
                                    AND alias.raw_object_id=raw.id
      JOIN messages boundary ON boundary.id=NEW.origin_boundary_user_message_id
                            AND boundary.user_id=work.user_id
                            AND boundary.conversation_id=work.conversation_id
                            AND boundary.role='user'
     WHERE work.id=NEW.work_item_id
       AND work.kind='compare_conversation_with_document'
       AND work.state='waiting_for_input'
       AND work.revision=NEW.resolved_revision-1
       AND raw.content_hash=NEW.raw_content_sha256
       AND json_extract(raw.metadata_json,'$.sha256')=NEW.content_sha256
       AND json_extract(NEW.source_ref_json,'$.principal_id')=work.user_id
       AND NOT EXISTS (
           SELECT 1 FROM work_item_compare_document_questions open_question
            WHERE open_question.work_item_id=work.id AND open_question.state<>'answered'
       )
       AND NEW.origin_boundary_user_message_id=(
           SELECT answer_user_message_id FROM work_item_compare_document_questions latest
            WHERE latest.work_item_id=work.id ORDER BY work_revision DESC LIMIT 1
       )
       AND NEW.resolved_revision=1+(
           SELECT COUNT(*) FROM work_item_compare_document_questions question
            WHERE question.work_item_id=work.id
       )
       AND ((NEW.provenance='historical_candidate_ordinal' AND EXISTS (
                SELECT 1 FROM work_item_compare_document_questions selected
                  JOIN work_item_archive_candidate_set_items candidate
                    ON candidate.candidate_set_id=selected.candidate_set_id
                   AND candidate.work_item_id=selected.work_item_id
                   AND candidate.ordinal=selected.selected_ordinal
                 WHERE selected.work_item_id=work.id AND selected.work_revision=2
                   AND selected.state='answered'
                   AND selected.candidate_set_id=NEW.candidate_set_id
                   AND selected.selected_ordinal=NEW.selected_ordinal
                   AND candidate.source_ref_json=NEW.source_ref_json
                   AND candidate.source_snapshot_sha256
                       =NEW.candidate_source_snapshot_sha256
                   AND NOT EXISTS (
                       SELECT 1 FROM json_each(candidate.passage_refs_json) passage
                        WHERE json_extract(passage.value,'$.source_revision.kind')
                                  <>'raw_content_sha256'
                           OR json_extract(passage.value,'$.source_revision.value')
                                  <>NEW.raw_content_sha256
                   )
            )) OR (NEW.provenance='current_turn_attachment'
                    AND NOT EXISTS (SELECT 1 FROM work_item_archive_candidate_sets candidate_set
                                     WHERE candidate_set.work_item_id=work.id)
                    AND json_type(boundary.metadata_json,
                                  '$.conversation_uploaded_raw_ids')='array'
                    AND json_array_length(boundary.metadata_json,
                                          '$.conversation_uploaded_raw_ids')=1
                    AND json_extract(boundary.metadata_json,
                                     '$.conversation_uploaded_raw_ids[0]')=NEW.raw_object_id)
               OR (NEW.provenance='historical_exact_reference'
                    AND NOT EXISTS (SELECT 1 FROM work_item_archive_candidate_sets candidate_set
                                     WHERE candidate_set.work_item_id=work.id)
                    AND json_type(boundary.metadata_json,
                                  '$.conversation_attachment_raw_ids')='array'
                    AND json_array_length(boundary.metadata_json,
                                          '$.conversation_attachment_raw_ids')=1
                    AND json_extract(boundary.metadata_json,
                                     '$.conversation_attachment_raw_ids[0]')=NEW.raw_object_id))
)
BEGIN SELECT RAISE(ABORT,'comparison document evidence scope is invalid'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_document_evidence_update
BEFORE UPDATE ON work_item_compare_document_evidence
BEGIN SELECT RAISE(ABORT,'comparison document evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_document_evidence_delete
BEFORE DELETE ON work_item_compare_document_evidence
WHEN EXISTS (SELECT 1 FROM work_items work WHERE work.id=OLD.work_item_id)
BEGIN SELECT RAISE(ABORT,'comparison document evidence is immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_outcomes_insert
BEFORE INSERT ON work_item_compare_outcomes
WHEN NOT EXISTS (
    SELECT 1 FROM work_items work
      JOIN work_item_compare_document_evidence document ON document.work_item_id=work.id
      JOIN work_item_selected_evidence selected_message ON selected_message.work_item_id=work.id
      JOIN messages boundary ON boundary.id=NEW.answer_boundary_user_message_id
                            AND boundary.user_id=work.user_id
                            AND boundary.conversation_id=work.conversation_id
                            AND boundary.role='user'
      JOIN messages assistant ON assistant.id=NEW.answer_assistant_message_id
                             AND assistant.user_id=work.user_id
                             AND assistant.conversation_id=work.conversation_id
                             AND assistant.role='assistant'
                             AND assistant.reply_to=boundary.id
     WHERE work.id=NEW.work_item_id
       AND work.kind='compare_conversation_with_document'
       AND work.state='active' AND work.transition='document_resolved'
       AND NEW.completed_revision=work.revision+1
       AND NEW.completed_at>=work.updated_at
       AND NEW.message_coverage_grade=selected_message.coverage_grade
       AND NEW.answer_boundary_user_message_id=document.origin_boundary_user_message_id
       AND length(CAST(assistant.content AS BLOB))
           BETWEEN 1 AND {COMPARE_CONVERSATION_DOCUMENT_ANSWER_MAX_BYTES}
       AND json_extract(assistant.metadata_json,'$.structural.answer_present')=1
       AND json_extract(assistant.metadata_json,'$.structural.model_spoke')=1
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.schema')
           ='friday.compare-conversation-document-outcome-receipt.v1'
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.outcome_sha256')
           =NEW.accepted_outcome_sha256
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.outcome.plan_sha256')
           =NEW.accepted_plan_sha256
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.outcome.status')
           =NEW.comparison_status
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.outcome.message_coverage_grade')
           =NEW.message_coverage_grade
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.outcome.document_verification_complete')
           =NEW.document_verification_complete
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.outcome.publication_attested')
           =NEW.publication_attested
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.outcome.semantic_verified')
           =NEW.semantic_verified
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.outcome.message_evidence_sha256')
           =NEW.message_evidence_sha256
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.outcome.document_evidence_sha256')
           =NEW.document_evidence_sha256
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.outcome.evidence_bundle_sha256')
           =NEW.evidence_bundle_sha256
       AND json_extract(assistant.metadata_json,
                        '$.accepted_compare_conversation_document_outcome.outcome.model_evidence_sha256')
           =NEW.model_evidence_sha256
)
BEGIN SELECT RAISE(ABORT,'comparison outcome scope is invalid'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_outcomes_update
BEFORE UPDATE ON work_item_compare_outcomes
BEGIN SELECT RAISE(ABORT,'comparison outcome is immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_outcomes_delete
BEFORE DELETE ON work_item_compare_outcomes
WHEN EXISTS (SELECT 1 FROM work_items work WHERE work.id=OLD.work_item_id)
BEGIN SELECT RAISE(ABORT,'comparison outcome is immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_items_compare_document_lifecycle_update
BEFORE UPDATE ON work_items
WHEN OLD.kind='compare_conversation_with_document'
 AND (
      NEW.id<>OLD.id OR NEW.user_id<>OLD.user_id OR NEW.conversation_id<>OLD.conversation_id
      OR NEW.kind<>OLD.kind OR NEW.goal<>OLD.goal OR NEW.playbook<>OLD.playbook
      OR NEW.completion_contract<>OLD.completion_contract
      OR NEW.active_frame_json<>OLD.active_frame_json
      OR NEW.anchor_user_message_id<>OLD.anchor_user_message_id
      OR NEW.anchor_assistant_message_id<>OLD.anchor_assistant_message_id
      OR NEW.accepted_plan_sha256<>OLD.accepted_plan_sha256
      OR NEW.accepted_outcome_sha256<>OLD.accepted_outcome_sha256
      OR NEW.created_at<>OLD.created_at
      OR (NEW.transition='question_reasked'
          AND unixepoch(NEW.expires_at)-unixepoch(NEW.updated_at)
              <>{WORK_ITEM_TTL_HOURS * 60 * 60})
      OR (NEW.transition<>'question_reasked' AND NEW.expires_at<>OLD.expires_at)
      OR NEW.revision<>OLD.revision+1 OR NEW.updated_at<OLD.updated_at
      OR NOT (
          (OLD.state='waiting_for_input' AND NEW.state='waiting_for_input'
           AND NEW.transition='question_reasked' AND NEW.revision=2
           AND EXISTS (
               SELECT 1 FROM work_item_compare_document_questions question
                WHERE question.work_item_id=OLD.id AND question.work_revision=2
                  AND question.state='waiting'
                  AND question.created_at=NEW.updated_at
           ))
          OR
          (OLD.state='waiting_for_input' AND NEW.state='active'
           AND NEW.transition='document_resolved'
           AND EXISTS (
               SELECT 1 FROM work_item_compare_document_evidence document
                WHERE document.work_item_id=OLD.id
                  AND document.resolved_revision=NEW.revision
                  AND document.resolved_at=NEW.updated_at
           ))
          OR
          (OLD.state='active' AND NEW.state='completed'
           AND NEW.transition='comparison_published' AND NEW.closed_at=NEW.updated_at
           AND EXISTS (
               SELECT 1 FROM work_item_compare_outcomes outcome
                WHERE outcome.work_item_id=OLD.id
                  AND outcome.completed_revision=NEW.revision
                  AND outcome.completed_at=NEW.updated_at
           ))
          OR
          (OLD.state IN ('waiting_for_input','active') AND NEW.state='suspended'
           AND NEW.transition='suspended')
          OR
          (OLD.state IN ('waiting_for_input','active','suspended')
           AND NEW.state='cancelled' AND NEW.transition='cancelled'
           AND NEW.closed_at=NEW.updated_at)
          OR
          (OLD.state IN ('waiting_for_input','active','suspended')
           AND NEW.state='expired' AND NEW.transition='expired'
           AND NEW.closed_at=NEW.updated_at AND NEW.expires_at<=NEW.updated_at)
      )
      OR (NEW.state IN ('active','waiting_for_input','suspended') AND NEW.closed_at IS NOT NULL)
      OR (NEW.state IN ('active','waiting_for_input','suspended') AND NEW.expires_at<=NEW.updated_at)
      OR (NEW.state IN ('suspended','cancelled','expired')
          AND EXISTS (SELECT 1 FROM work_item_compare_document_questions question
                       WHERE question.work_item_id=OLD.id AND question.state='waiting'))
      OR (OLD.state='waiting_for_input'
          AND NEW.state IN ('suspended','cancelled','expired')
          AND NOT EXISTS (
              SELECT 1 FROM work_item_compare_document_questions question
               WHERE question.work_item_id=OLD.id
                 AND question.work_revision=OLD.revision
                 AND question.state='closed'
                 AND question.close_reason=NEW.state
                 AND question.closed_at=NEW.updated_at
          ))
 )
BEGIN SELECT RAISE(ABORT,'comparison Work Item lifecycle is invalid'); END;
"""
    )


_WORK_ITEM_SCHEMA_42 = _schema_42_full_from_40()

# Schema 44 adds one dormant, journey-specific execution graph.  It deliberately
# does not alter the released schema-42 Work Item tables and it exposes no
# generic graph/node vocabulary.  The only admitted topology is enforced by the
# step table checks below: two independent reads followed by primary synthesis.
_WORK_ITEM_SCHEMA_44_EXTENSION = f"""
CREATE TABLE IF NOT EXISTS work_item_compare_current_file_web_graphs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    anchor_user_message_id TEXT NOT NULL REFERENCES messages(id),
    current_file_raw_object_id TEXT NOT NULL REFERENCES raw_objects(id),
    state TEXT NOT NULL CHECK(state IN ('active','terminal','completed')),
    revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
    transition TEXT NOT NULL CHECK(transition IN (
        'admitted','step_claimed','step_settled','step_requeued','restart_rebind',
        'review_recovery_admitted','terminal_settled','publication_committed'
    )),
    proposal_sha256 TEXT NOT NULL,
    accepted_plan_sha256 TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL,
    runtime_profile_sha256 TEXT NOT NULL,
    adapter_registry_sha256 TEXT NOT NULL,
    actor_binding_sha256 TEXT NOT NULL,
    conversation_binding_sha256 TEXT NOT NULL,
    current_file_source_identity_sha256 TEXT NOT NULL,
    current_file_content_sha256 TEXT NOT NULL,
    completion_contract TEXT NOT NULL CHECK(
        completion_contract='{COMPARE_CURRENT_FILE_WEB_COMPLETION_CONTRACT}'
    ),
    fallback_owner TEXT NOT NULL CHECK(
        fallback_owner='{COMPARE_CURRENT_FILE_WEB_FALLBACK_OWNER}'
    ),
    publication_owner TEXT NOT NULL CHECK(
        publication_owner='{COMPARE_CURRENT_FILE_WEB_PUBLICATION_OWNER}'
    ),
    max_attempts INTEGER NOT NULL CHECK(max_attempts={COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS}),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    closed_at TEXT,
    outcome_status TEXT CHECK(outcome_status IN (
        'complete','partial','empty','unavailable','denied','failed'
    )),
    outcome_reason TEXT CHECK(outcome_reason IN (
        'none','partial_evidence','no_comparable_evidence','capability_unavailable',
        'authority_denied','step_failed','evidence_not_replayable',
        'conversation_archived','cancelled','expired'
    )),
    publication_assistant_message_id TEXT REFERENCES messages(id),
    accepted_graph_outcome_sha256 TEXT,
    accepted_steps_sha256 TEXT,
    terminal_publication_receipt_sha256 TEXT,
    publication_receipt_sha256 TEXT,
    CHECK(length(id)=22 AND substr(id,1,6)='graph_'
          AND substr(id,7) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_user_message_id)=20 AND substr(anchor_user_message_id,1,4)='msg_'
          AND substr(anchor_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(current_file_raw_object_id)=20 AND substr(current_file_raw_object_id,1,4)='raw_'
          AND substr(current_file_raw_object_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(publication_assistant_message_id IS NULL
          OR (length(publication_assistant_message_id)=20
              AND substr(publication_assistant_message_id,1,4)='msg_'
              AND substr(publication_assistant_message_id,5) NOT GLOB '*[^0-9a-f]*')),
    CHECK(length(proposal_sha256)=64 AND proposal_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(accepted_plan_sha256)=64 AND accepted_plan_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(manifest_sha256)=64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(policy_sha256)=64 AND policy_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(runtime_profile_sha256)=64 AND runtime_profile_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(adapter_registry_sha256)=64 AND adapter_registry_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(actor_binding_sha256)=64 AND actor_binding_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(conversation_binding_sha256)=64 AND conversation_binding_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(current_file_source_identity_sha256)=64
              AND current_file_source_identity_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(current_file_content_sha256)=64
              AND current_file_content_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(accepted_graph_outcome_sha256 IS NULL
          OR (length(accepted_graph_outcome_sha256)=64
              AND accepted_graph_outcome_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(accepted_steps_sha256 IS NULL
          OR (length(accepted_steps_sha256)=64
              AND accepted_steps_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(publication_receipt_sha256 IS NULL
          OR (length(publication_receipt_sha256)=64
              AND publication_receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(terminal_publication_receipt_sha256 IS NULL
          OR (length(terminal_publication_receipt_sha256)=64
              AND terminal_publication_receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(length(created_at) BETWEEN 20 AND 64 AND unixepoch(created_at) IS NOT NULL
          AND length(updated_at) BETWEEN 20 AND 64 AND unixepoch(updated_at) IS NOT NULL
          AND length(expires_at) BETWEEN 20 AND 64 AND unixepoch(expires_at) IS NOT NULL
          AND (closed_at IS NULL
               OR (length(closed_at) BETWEEN 20 AND 64 AND unixepoch(closed_at) IS NOT NULL))),
    CHECK(updated_at>=created_at
          AND ((state='active' AND expires_at>updated_at)
               OR (state<>'active' AND closed_at=updated_at))
          AND unixepoch(expires_at)-unixepoch(created_at)<={WORK_ITEM_TTL_HOURS * 60 * 60}),
    CHECK(state<>'active' OR revision<={COMPARE_CURRENT_FILE_WEB_MAX_ACTIVE_REVISION}),
    CHECK((state='active' AND transition IN (
              'admitted','step_claimed','step_settled','step_requeued','restart_rebind',
              'review_recovery_admitted'
          ) AND closed_at IS NULL
          AND outcome_status IS NULL AND outcome_reason IS NULL
          AND publication_assistant_message_id IS NULL
          AND accepted_graph_outcome_sha256 IS NULL
          AND accepted_steps_sha256 IS NULL
          AND terminal_publication_receipt_sha256 IS NULL
          AND publication_receipt_sha256 IS NULL)
          OR (state='terminal' AND transition='terminal_settled'
              AND closed_at=updated_at
              AND outcome_status IN ('partial','empty','unavailable','denied','failed')
              AND outcome_reason IN (
                  'partial_evidence','no_comparable_evidence','capability_unavailable',
                  'authority_denied','step_failed','evidence_not_replayable',
                  'conversation_archived','cancelled','expired'
              )
              AND publication_assistant_message_id IS NOT NULL
              AND accepted_graph_outcome_sha256 IS NOT NULL
              AND accepted_steps_sha256 IS NOT NULL
              AND terminal_publication_receipt_sha256 IS NOT NULL
              AND publication_receipt_sha256 IS NULL)
          OR (state='completed' AND transition='publication_committed'
              AND closed_at=updated_at
              AND outcome_status='complete' AND outcome_reason='none'
              AND publication_assistant_message_id IS NOT NULL
              AND accepted_graph_outcome_sha256 IS NOT NULL
              AND accepted_steps_sha256 IS NOT NULL
              AND terminal_publication_receipt_sha256 IS NULL
              AND publication_receipt_sha256 IS NOT NULL)),
    CHECK((transition='admitted' AND revision=1)
          OR (transition<>'admitted' AND revision>=2))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_work_item_compare_current_file_web_graphs_active
    ON work_item_compare_current_file_web_graphs(user_id,conversation_id)
    WHERE state='active';
CREATE INDEX IF NOT EXISTS idx_work_item_compare_current_file_web_graphs_owner
    ON work_item_compare_current_file_web_graphs(user_id,state,updated_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_work_item_compare_current_file_web_graphs_expiry
    ON work_item_compare_current_file_web_graphs(state,expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uq_work_item_compare_current_file_web_graphs_assistant
    ON work_item_compare_current_file_web_graphs(publication_assistant_message_id)
    WHERE publication_assistant_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS work_item_compare_current_file_web_steps (
    graph_id TEXT NOT NULL REFERENCES work_item_compare_current_file_web_graphs(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('file_current_read','web_current_read','primary_synthesis')),
    capability_id TEXT NOT NULL,
    security_id TEXT,
    adapter_id TEXT,
    effect_class TEXT NOT NULL CHECK(effect_class='read'),
    evidence_replayability TEXT NOT NULL CHECK(evidence_replayability='process_private'),
    depends_on_json TEXT NOT NULL CHECK(json_valid(depends_on_json) AND json_type(depends_on_json)='array'),
    parallel_group TEXT,
    input_identity_sha256 TEXT NOT NULL,
    idempotency_key_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN (
        'pending','running','complete','partial','empty','unavailable','denied','failed'
    )),
    attempt INTEGER NOT NULL CHECK(attempt BETWEEN 0 AND {COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS}),
    outcome_schema TEXT NOT NULL CHECK(outcome_schema='{COMPARE_CURRENT_FILE_WEB_STEP_OUTCOME_SCHEMA}'),
    outcome_sha256 TEXT,
    prior_outcome_sha256 TEXT,
    recovery_review_sha256 TEXT,
    recovery_context_sha256 TEXT,
    evidence_identity_sha256 TEXT,
    authority_rechecked INTEGER NOT NULL CHECK(authority_rechecked IN (0,1)),
    verified INTEGER NOT NULL CHECK(verified IN (0,1)),
    started_at TEXT,
    settled_at TEXT,
    PRIMARY KEY(graph_id,step_id),
    UNIQUE(graph_id,kind),
    CHECK(length(step_id) BETWEEN 1 AND 64),
    CHECK((kind='file_current_read' AND step_id='read_current_file'
           AND capability_id='file.current.read' AND security_id IS NULL
           AND adapter_id IS NULL AND depends_on_json='[]'
           AND parallel_group='current_evidence')
          OR (kind='web_current_read' AND step_id='read_current_web'
              AND capability_id='web.search.current'
              AND security_id='{WEB_SEARCH_CURRENT_SECURITY_ID}'
              AND adapter_id='{WEB_SEARCH_CURRENT_ADAPTER_ID}'
              AND depends_on_json='[]'
              AND parallel_group='current_evidence')
          OR (kind='primary_synthesis' AND step_id='primary_synthesis'
              AND capability_id='model.primary.synthesis' AND security_id IS NULL
              AND adapter_id IS NULL
              AND depends_on_json='["read_current_file","read_current_web"]'
              AND parallel_group IS NULL)),
    CHECK(length(input_identity_sha256)=64 AND input_identity_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(idempotency_key_sha256)=64
              AND idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(outcome_sha256 IS NULL
          OR (length(outcome_sha256)=64 AND outcome_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(prior_outcome_sha256 IS NULL
          OR (length(prior_outcome_sha256)=64
              AND prior_outcome_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK((recovery_review_sha256 IS NULL AND recovery_context_sha256 IS NULL)
          OR (kind='web_current_read' AND attempt>=1 AND prior_outcome_sha256 IS NOT NULL
              AND length(recovery_review_sha256)=64
              AND recovery_review_sha256 NOT GLOB '*[^0-9a-f]*'
              AND length(recovery_context_sha256)=64
              AND recovery_context_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(evidence_identity_sha256 IS NULL
          OR (length(evidence_identity_sha256)=64
              AND evidence_identity_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK((started_at IS NULL
           OR (length(started_at) BETWEEN 20 AND 64 AND unixepoch(started_at) IS NOT NULL))
          AND (settled_at IS NULL
               OR (length(settled_at) BETWEEN 20 AND 64 AND unixepoch(settled_at) IS NOT NULL))),
    CHECK((state='pending' AND outcome_sha256 IS NULL AND evidence_identity_sha256 IS NULL
           AND authority_rechecked=0 AND verified=0 AND started_at IS NULL AND settled_at IS NULL
           AND (prior_outcome_sha256 IS NULL OR attempt>=1))
          OR (state='running' AND attempt>=1 AND started_at IS NOT NULL AND settled_at IS NULL
              AND outcome_sha256 IS NULL AND evidence_identity_sha256 IS NULL
              AND authority_rechecked=0 AND verified=0
              AND (attempt>1 OR prior_outcome_sha256 IS NULL))
          OR (state IN ('complete','partial','empty') AND attempt>=1
              AND started_at IS NOT NULL AND settled_at>=started_at AND outcome_sha256 IS NOT NULL
              AND evidence_identity_sha256 IS NOT NULL AND verified=1
              AND ((kind IN ('file_current_read','web_current_read') AND authority_rechecked=1)
                   OR (kind='primary_synthesis' AND authority_rechecked=0
                       AND state IN ('complete','partial'))))
          OR (state='denied' AND kind IN ('file_current_read','web_current_read') AND attempt>=1
              AND started_at IS NOT NULL AND settled_at>=started_at AND outcome_sha256 IS NOT NULL
              AND evidence_identity_sha256 IS NULL AND authority_rechecked=1 AND verified=0)
          OR (state IN ('unavailable','failed') AND attempt>=1
              AND started_at IS NOT NULL AND settled_at>=started_at AND outcome_sha256 IS NOT NULL
              AND evidence_identity_sha256 IS NULL AND authority_rechecked=0 AND verified=0)),
    CHECK(kind<>'primary_synthesis' OR state NOT IN ('empty','denied'))
);

CREATE INDEX IF NOT EXISTS idx_work_item_compare_current_file_web_steps_state
    ON work_item_compare_current_file_web_steps(graph_id,state,step_id);

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_graphs_scope_insert
BEFORE INSERT ON work_item_compare_current_file_web_graphs
WHEN NOT EXISTS (
        SELECT 1
          FROM conversations conversation
          JOIN messages boundary
            ON boundary.id=NEW.anchor_user_message_id
           AND boundary.user_id=conversation.user_id
           AND boundary.conversation_id=conversation.id
           AND boundary.role='user'
          JOIN raw_objects source
            ON source.id=NEW.current_file_raw_object_id
           AND source.user_id=conversation.user_id
         WHERE conversation.id=NEW.conversation_id
           AND conversation.user_id=NEW.user_id
           AND conversation.is_archived=0
    )
    OR EXISTS (
        SELECT 1 FROM work_items work
         WHERE work.user_id=NEW.user_id AND work.conversation_id=NEW.conversation_id
           AND work.state IN ('active','waiting_for_input')
    )
BEGIN
    SELECT RAISE(ABORT,'current-file/web WorkGraph scope is invalid or already owned');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_items_current_file_web_graph_owner_insert
BEFORE INSERT ON work_items
WHEN NEW.state IN ('active','waiting_for_input')
 AND EXISTS (
     SELECT 1 FROM work_item_compare_current_file_web_graphs graph
      WHERE graph.user_id=NEW.user_id AND graph.conversation_id=NEW.conversation_id
        AND graph.state='active'
 )
BEGIN
    SELECT RAISE(ABORT,'current-file/web WorkGraph already owns the conversation');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_items_current_file_web_graph_owner_update
BEFORE UPDATE OF user_id,conversation_id,state ON work_items
WHEN NEW.state IN ('active','waiting_for_input')
 AND EXISTS (
     SELECT 1 FROM work_item_compare_current_file_web_graphs graph
      WHERE graph.user_id=NEW.user_id AND graph.conversation_id=NEW.conversation_id
        AND graph.state='active'
 )
BEGIN
    SELECT RAISE(ABORT,'current-file/web WorkGraph already owns the conversation');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_graphs_identity_immutable
BEFORE UPDATE ON work_item_compare_current_file_web_graphs
WHEN NEW.id<>OLD.id OR NEW.user_id<>OLD.user_id
  OR NEW.conversation_id<>OLD.conversation_id
  OR NEW.anchor_user_message_id<>OLD.anchor_user_message_id
  OR NEW.current_file_raw_object_id<>OLD.current_file_raw_object_id
  OR NEW.proposal_sha256<>OLD.proposal_sha256
  OR NEW.accepted_plan_sha256<>OLD.accepted_plan_sha256
  OR NEW.manifest_sha256<>OLD.manifest_sha256
  OR NEW.policy_sha256<>OLD.policy_sha256
  OR NEW.runtime_profile_sha256<>OLD.runtime_profile_sha256
  OR NEW.adapter_registry_sha256<>OLD.adapter_registry_sha256
  OR NEW.actor_binding_sha256<>OLD.actor_binding_sha256
  OR NEW.conversation_binding_sha256<>OLD.conversation_binding_sha256
  OR NEW.current_file_source_identity_sha256<>OLD.current_file_source_identity_sha256
  OR NEW.current_file_content_sha256<>OLD.current_file_content_sha256
  OR NEW.completion_contract<>OLD.completion_contract
  OR NEW.fallback_owner<>OLD.fallback_owner
  OR NEW.publication_owner<>OLD.publication_owner
  OR NEW.max_attempts<>OLD.max_attempts
  OR NEW.created_at<>OLD.created_at OR NEW.expires_at<>OLD.expires_at
BEGIN
    SELECT RAISE(ABORT,'current-file/web WorkGraph identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_graphs_lifecycle
BEFORE UPDATE ON work_item_compare_current_file_web_graphs
WHEN OLD.state<>'active'
  OR NEW.revision<>OLD.revision+1
  OR NEW.updated_at<OLD.updated_at
  OR (NEW.state='active' AND NEW.updated_at>=NEW.expires_at)
  OR NOT (
      (NEW.state='active'
       AND NEW.transition IN (
           'step_claimed','step_settled','step_requeued','restart_rebind',
           'review_recovery_admitted'
       )
       AND NEW.closed_at IS NULL
       AND NEW.outcome_status IS NULL AND NEW.outcome_reason IS NULL
       AND NEW.publication_assistant_message_id IS NULL
       AND NEW.accepted_graph_outcome_sha256 IS NULL
       AND NEW.accepted_steps_sha256 IS NULL
       AND NEW.terminal_publication_receipt_sha256 IS NULL
       AND NEW.publication_receipt_sha256 IS NULL)
      OR
      (NEW.state='terminal' AND NEW.transition='terminal_settled'
       AND NEW.closed_at=NEW.updated_at
       AND NEW.outcome_status IN ('partial','empty','unavailable','denied','failed')
       AND NEW.outcome_reason IN (
           'partial_evidence','no_comparable_evidence','capability_unavailable',
           'authority_denied','step_failed','evidence_not_replayable',
           'conversation_archived','cancelled','expired'
       )
       AND NEW.publication_assistant_message_id IS NOT NULL
       AND NEW.accepted_graph_outcome_sha256 IS NOT NULL
       AND NEW.accepted_steps_sha256 IS NOT NULL
       AND NEW.terminal_publication_receipt_sha256 IS NOT NULL
       AND NEW.publication_receipt_sha256 IS NULL)
      OR
      (NEW.state='completed' AND NEW.transition='publication_committed'
       AND NEW.closed_at=NEW.updated_at
       AND NEW.outcome_status='complete' AND NEW.outcome_reason='none'
       AND NEW.publication_assistant_message_id IS NOT NULL
       AND NEW.accepted_graph_outcome_sha256 IS NOT NULL
       AND NEW.accepted_steps_sha256 IS NOT NULL
       AND NEW.terminal_publication_receipt_sha256 IS NULL
       AND NEW.publication_receipt_sha256 IS NOT NULL)
  )
BEGIN
    SELECT RAISE(ABORT,'current-file/web WorkGraph lifecycle is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_graphs_completion
BEFORE UPDATE ON work_item_compare_current_file_web_graphs
WHEN NEW.state='completed' AND NOT (
    (SELECT COUNT(*) FROM work_item_compare_current_file_web_steps step
      WHERE step.graph_id=OLD.id)=3
    AND NOT EXISTS (
        SELECT 1 FROM work_item_compare_current_file_web_steps step
         WHERE step.graph_id=OLD.id AND step.state<>'complete'
    )
    AND EXISTS (
        SELECT 1 FROM messages assistant
         WHERE assistant.id=NEW.publication_assistant_message_id
           AND assistant.user_id=OLD.user_id
           AND assistant.conversation_id=OLD.conversation_id
           AND assistant.role='assistant'
           AND assistant.reply_to=OLD.anchor_user_message_id
           AND json_valid(assistant.metadata_json)
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY}.schema')
               ='{COMPARE_CURRENT_FILE_WEB_PUBLICATION_RECEIPT_SCHEMA}'
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY}.graph_id')=OLD.id
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY}.completed_revision')
               =NEW.revision
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY}.accepted_plan_sha256')
               =OLD.accepted_plan_sha256
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY}.graph_outcome_sha256')
               =NEW.accepted_graph_outcome_sha256
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY}.steps_sha256')
               =NEW.accepted_steps_sha256
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_PUBLICATION_METADATA_KEY}.final_authority_rechecked')=1
    )
)
BEGIN
    SELECT RAISE(ABORT,'current-file/web WorkGraph publication receipt is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_graphs_terminal_publication
BEFORE UPDATE ON work_item_compare_current_file_web_graphs
WHEN NEW.state='terminal' AND NOT (
    (SELECT COUNT(*) FROM work_item_compare_current_file_web_steps step
      WHERE step.graph_id=OLD.id)=3
    AND (
        (NEW.outcome_status='partial' AND NEW.outcome_reason='partial_evidence'
         AND EXISTS (
             SELECT 1 FROM work_item_compare_current_file_web_steps synthesis
              WHERE synthesis.graph_id=OLD.id AND synthesis.step_id='primary_synthesis'
                AND synthesis.state='partial'
         ))
        OR (NEW.outcome_status='denied'
            AND NEW.outcome_reason='authority_denied'
            AND EXISTS (
                SELECT 1 FROM work_item_compare_current_file_web_steps synthesis
                 WHERE synthesis.graph_id=OLD.id AND synthesis.step_id='primary_synthesis'
                   AND synthesis.state='unavailable'
            )
            AND EXISTS (
                SELECT 1 FROM work_item_compare_current_file_web_steps read_step
                 WHERE read_step.graph_id=OLD.id
                   AND read_step.step_id IN ('read_current_file','read_current_web')
                   AND read_step.state='denied'
            )
            AND EXISTS (
                SELECT 1 FROM work_item_compare_current_file_web_steps read_step
                 WHERE read_step.graph_id=OLD.id
                   AND read_step.step_id IN ('read_current_file','read_current_web')
                   AND read_step.state IN ('complete','partial')
            ))
        OR (NEW.outcome_status='unavailable'
            AND NEW.outcome_reason='capability_unavailable'
            AND EXISTS (
                SELECT 1 FROM work_item_compare_current_file_web_steps synthesis
                 WHERE synthesis.graph_id=OLD.id AND synthesis.step_id='primary_synthesis'
                   AND synthesis.state='unavailable'
            )
            AND NOT EXISTS (
                SELECT 1 FROM work_item_compare_current_file_web_steps read_step
                 WHERE read_step.graph_id=OLD.id
                   AND read_step.step_id IN ('read_current_file','read_current_web')
                   AND read_step.state='denied'
            ))
        OR (NEW.outcome_status='failed' AND NEW.outcome_reason='step_failed'
            AND EXISTS (
                SELECT 1 FROM work_item_compare_current_file_web_steps synthesis
                 WHERE synthesis.graph_id=OLD.id AND synthesis.step_id='primary_synthesis'
                   AND synthesis.state='failed'
            ))
        OR (NEW.outcome_reason='evidence_not_replayable'
            AND NEW.outcome_status='unavailable')
        OR (NEW.outcome_status='unavailable'
            AND NEW.outcome_reason='conversation_archived'
            AND EXISTS (
                SELECT 1 FROM conversations conversation
                 WHERE conversation.id=OLD.conversation_id
                   AND conversation.user_id=OLD.user_id
                   AND conversation.is_archived=1
            ))
        OR (NEW.outcome_status='unavailable'
            AND NEW.outcome_reason='cancelled')
        OR (NEW.outcome_status='unavailable'
            AND NEW.outcome_reason='expired'
            AND NEW.updated_at>=OLD.expires_at)
        OR (EXISTS (
                SELECT 1 FROM work_item_compare_current_file_web_steps synthesis
                 WHERE synthesis.graph_id=OLD.id AND synthesis.step_id='primary_synthesis'
                   AND synthesis.state='pending'
            )
            AND NOT EXISTS (
                SELECT 1 FROM work_item_compare_current_file_web_steps read_step
                 WHERE read_step.graph_id=OLD.id
                   AND read_step.step_id IN ('read_current_file','read_current_web')
                   AND read_step.state IN ('pending','running','complete','partial')
            )
            AND (
                (NEW.outcome_status='denied' AND NEW.outcome_reason='authority_denied'
                 AND EXISTS (
                     SELECT 1 FROM work_item_compare_current_file_web_steps read_step
                      WHERE read_step.graph_id=OLD.id
                        AND read_step.step_id IN ('read_current_file','read_current_web')
                        AND read_step.state='denied'
                 ))
                OR (NEW.outcome_status='failed' AND NEW.outcome_reason='step_failed'
                    AND NOT EXISTS (
                        SELECT 1 FROM work_item_compare_current_file_web_steps read_step
                         WHERE read_step.graph_id=OLD.id
                           AND read_step.step_id IN ('read_current_file','read_current_web')
                           AND read_step.state='denied'
                    )
                    AND EXISTS (
                        SELECT 1 FROM work_item_compare_current_file_web_steps read_step
                         WHERE read_step.graph_id=OLD.id
                           AND read_step.step_id IN ('read_current_file','read_current_web')
                           AND read_step.state='failed'
                    ))
                OR (NEW.outcome_status='unavailable'
                    AND NEW.outcome_reason='capability_unavailable'
                    AND NOT EXISTS (
                        SELECT 1 FROM work_item_compare_current_file_web_steps read_step
                         WHERE read_step.graph_id=OLD.id
                           AND read_step.step_id IN ('read_current_file','read_current_web')
                           AND read_step.state IN ('denied','failed')
                    )
                    AND EXISTS (
                        SELECT 1 FROM work_item_compare_current_file_web_steps read_step
                         WHERE read_step.graph_id=OLD.id
                           AND read_step.step_id IN ('read_current_file','read_current_web')
                           AND read_step.state='unavailable'
                    ))
                OR (NEW.outcome_status='empty'
                    AND NEW.outcome_reason='no_comparable_evidence'
                    AND NOT EXISTS (
                        SELECT 1 FROM work_item_compare_current_file_web_steps read_step
                         WHERE read_step.graph_id=OLD.id
                           AND read_step.step_id IN ('read_current_file','read_current_web')
                           AND read_step.state<>'empty'
                    ))
            ))
    )
    AND EXISTS (
        SELECT 1 FROM messages assistant
         WHERE assistant.id=NEW.publication_assistant_message_id
           AND assistant.user_id=OLD.user_id
           AND assistant.conversation_id=OLD.conversation_id
           AND assistant.role='assistant'
           AND assistant.reply_to=OLD.anchor_user_message_id
           AND json_valid(assistant.metadata_json)
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.schema')
               ='{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_RECEIPT_SCHEMA}'
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.graph_id')=OLD.id
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.terminal_revision')
               =NEW.revision
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.accepted_plan_sha256')
               =OLD.accepted_plan_sha256
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.status')
               =NEW.outcome_status
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.reason')
               =NEW.outcome_reason
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.graph_outcome_sha256')
               =NEW.accepted_graph_outcome_sha256
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.steps_sha256')
               =NEW.accepted_steps_sha256
           AND json_extract(assistant.metadata_json,
                            '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.completion_claimed')=0
           AND (NEW.outcome_reason<>'conversation_archived'
                OR assistant.content='{COMPARE_CURRENT_FILE_WEB_ARCHIVED_RESPONSE}')
           AND (NEW.outcome_reason<>'cancelled'
                OR assistant.content='{COMPARE_CURRENT_FILE_WEB_CANCELLED_RESPONSE}')
           AND (NEW.outcome_reason<>'expired'
                OR assistant.content='{COMPARE_CURRENT_FILE_WEB_EXPIRED_RESPONSE}')
           AND (NEW.outcome_reason<>'evidence_not_replayable'
                OR assistant.content='{COMPARE_CURRENT_FILE_WEB_RESTART_UNAVAILABLE_RESPONSE}')
           AND (
               (NEW.outcome_status='partial'
                AND json_extract(assistant.metadata_json,
                    '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.model_spoke')=1
                AND json_extract(assistant.metadata_json,
                    '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.evidence_cited')=1
                AND json_extract(assistant.metadata_json,
                    '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.final_authority_rechecked')=1)
               OR (NEW.outcome_status<>'partial'
                   AND json_extract(assistant.metadata_json,
                       '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.model_spoke')=0
                   AND json_extract(assistant.metadata_json,
                       '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.evidence_cited')=0
                   AND json_extract(assistant.metadata_json,
                       '$.{COMPARE_CURRENT_FILE_WEB_TERMINAL_PUBLICATION_METADATA_KEY}.final_authority_rechecked')=0)
           )
    )
)
BEGIN
    SELECT RAISE(ABORT,'current-file/web WorkGraph terminal publication receipt is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_steps_identity_immutable
BEFORE UPDATE ON work_item_compare_current_file_web_steps
WHEN NEW.graph_id<>OLD.graph_id OR NEW.step_id<>OLD.step_id OR NEW.kind<>OLD.kind
  OR NEW.capability_id<>OLD.capability_id OR NEW.effect_class<>OLD.effect_class
  OR NEW.security_id IS NOT OLD.security_id OR NEW.adapter_id IS NOT OLD.adapter_id
  OR NEW.evidence_replayability<>OLD.evidence_replayability
  OR NEW.depends_on_json<>OLD.depends_on_json
  OR NEW.parallel_group IS NOT OLD.parallel_group
  OR NEW.input_identity_sha256<>OLD.input_identity_sha256
  OR NEW.idempotency_key_sha256<>OLD.idempotency_key_sha256
  OR NEW.outcome_schema<>OLD.outcome_schema
BEGIN
    SELECT RAISE(ABORT,'current-file/web WorkGraph step identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_steps_lifecycle
BEFORE UPDATE ON work_item_compare_current_file_web_steps
WHEN NOT (
    (OLD.state='pending' AND NEW.state='running' AND NEW.attempt=OLD.attempt+1
     AND NEW.started_at IS NOT NULL AND NEW.settled_at IS NULL
     AND NEW.outcome_sha256 IS NULL AND NEW.evidence_identity_sha256 IS NULL
     AND NEW.prior_outcome_sha256 IS OLD.prior_outcome_sha256
     AND NEW.recovery_review_sha256 IS OLD.recovery_review_sha256
     AND NEW.recovery_context_sha256 IS OLD.recovery_context_sha256
     AND NEW.authority_rechecked=0 AND NEW.verified=0)
    OR
    (OLD.state='running'
     AND NEW.state IN ('complete','partial','empty','unavailable','denied','failed')
     AND NEW.attempt=OLD.attempt AND NEW.started_at=OLD.started_at
     AND NEW.prior_outcome_sha256 IS OLD.prior_outcome_sha256
     AND NEW.recovery_review_sha256 IS OLD.recovery_review_sha256
     AND NEW.recovery_context_sha256 IS OLD.recovery_context_sha256
     AND NEW.settled_at>=OLD.started_at AND NEW.outcome_sha256 IS NOT NULL)
    OR
    (OLD.state='running' AND NEW.state='pending' AND NEW.attempt=OLD.attempt
     AND NEW.started_at IS NULL AND NEW.settled_at IS NULL
     AND NEW.outcome_sha256 IS NULL AND NEW.evidence_identity_sha256 IS NULL
     AND NEW.prior_outcome_sha256 IS OLD.prior_outcome_sha256
     AND NEW.recovery_review_sha256 IS OLD.recovery_review_sha256
     AND NEW.recovery_context_sha256 IS OLD.recovery_context_sha256
     AND NEW.authority_rechecked=0 AND NEW.verified=0)
    OR
    (OLD.state IN ('complete','partial','empty') AND NEW.state='pending'
     AND OLD.attempt<{COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS}
     AND NEW.attempt=OLD.attempt AND NEW.started_at IS NULL AND NEW.settled_at IS NULL
     AND NEW.outcome_sha256 IS NULL AND NEW.evidence_identity_sha256 IS NULL
     AND NEW.prior_outcome_sha256=OLD.outcome_sha256
     AND NEW.recovery_review_sha256 IS OLD.recovery_review_sha256
     AND NEW.recovery_context_sha256 IS OLD.recovery_context_sha256
     AND NEW.authority_rechecked=0 AND NEW.verified=0
     AND EXISTS (
         SELECT 1 FROM work_item_compare_current_file_web_graphs graph
          WHERE graph.id=OLD.graph_id AND graph.state='active'
            AND graph.transition='restart_rebind'
     ))
    OR
    (OLD.kind='web_current_read'
     AND OLD.state IN ('empty','unavailable','failed')
     AND OLD.attempt<{COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS}
     AND OLD.prior_outcome_sha256 IS NULL
     AND OLD.recovery_review_sha256 IS NULL AND OLD.recovery_context_sha256 IS NULL
     AND NEW.state='pending' AND NEW.attempt=OLD.attempt
     AND NEW.started_at IS NULL AND NEW.settled_at IS NULL
     AND NEW.outcome_sha256 IS NULL AND NEW.evidence_identity_sha256 IS NULL
     AND NEW.prior_outcome_sha256=OLD.outcome_sha256
     AND NEW.recovery_review_sha256 IS NOT NULL
     AND NEW.recovery_context_sha256 IS NOT NULL
     AND NEW.authority_rechecked=0 AND NEW.verified=0
     AND EXISTS (
         SELECT 1 FROM work_item_compare_current_file_web_graphs graph
          WHERE graph.id=OLD.graph_id AND graph.state='active'
            AND graph.transition='review_recovery_admitted'
     ))
)
BEGIN
    SELECT RAISE(ABORT,'current-file/web WorkGraph step lifecycle is invalid');
END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_steps_delete_immutable
BEFORE DELETE ON work_item_compare_current_file_web_steps
WHEN EXISTS (
    SELECT 1 FROM work_item_compare_current_file_web_graphs graph WHERE graph.id=OLD.graph_id
)
BEGIN
    SELECT RAISE(ABORT,'current-file/web WorkGraph step deletion is immutable');
END;
"""

_WORK_ITEM_SCHEMA_44 = _WORK_ITEM_SCHEMA_42 + _WORK_ITEM_SCHEMA_44_EXTENSION


def _schema_45_extension_from_44() -> str:
    """Add an exact request root and one audit-preserving restart rebind.

    Schema 45 has not shipped.  Its fixed graph may therefore gain the restart
    contract here without creating a second migration marker.  A rebind is a
    single graph UPDATE: SQLite snapshots the predecessor control/step rows,
    installs fresh plan identities, and resets all three read-only nodes in one
    atomic trigger program.  Old process-private evidence is never restored.
    """

    schema = _WORK_ITEM_SCHEMA_44_EXTENSION
    replacements = (
        (
            """    anchor_user_message_id TEXT NOT NULL REFERENCES messages(id),
    current_file_raw_object_id TEXT NOT NULL REFERENCES raw_objects(id),""",
            """    anchor_user_message_id TEXT NOT NULL REFERENCES messages(id),
    anchor_request_binding_sha256 TEXT NOT NULL,
    current_file_raw_object_id TEXT NOT NULL REFERENCES raw_objects(id),""",
        ),
        (
            f"""    max_attempts INTEGER NOT NULL CHECK(max_attempts={COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS}),
    created_at TEXT NOT NULL,""",
            f"""    max_attempts INTEGER NOT NULL CHECK(max_attempts={COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS}),
    restart_count INTEGER NOT NULL CHECK(restart_count IN (0,1)),
    restart_rebound_at TEXT,
    restart_file_input_identity_sha256 TEXT,
    restart_file_idempotency_key_sha256 TEXT,
    restart_web_input_identity_sha256 TEXT,
    restart_web_idempotency_key_sha256 TEXT,
    restart_synthesis_input_identity_sha256 TEXT,
    restart_synthesis_idempotency_key_sha256 TEXT,
    created_at TEXT NOT NULL,""",
        ),
        (
            """    CHECK(length(anchor_user_message_id)=20 AND substr(anchor_user_message_id,1,4)='msg_'
          AND substr(anchor_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(current_file_raw_object_id)=20""",
            """    CHECK(length(anchor_user_message_id)=20 AND substr(anchor_user_message_id,1,4)='msg_'
          AND substr(anchor_user_message_id,5) NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(anchor_request_binding_sha256)=64
          AND anchor_request_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(length(current_file_raw_object_id)=20""",
        ),
        (
            """  OR NEW.anchor_user_message_id<>OLD.anchor_user_message_id
  OR NEW.current_file_raw_object_id<>OLD.current_file_raw_object_id""",
            """  OR NEW.anchor_user_message_id<>OLD.anchor_user_message_id
  OR NEW.anchor_request_binding_sha256<>OLD.anchor_request_binding_sha256
  OR NEW.current_file_raw_object_id<>OLD.current_file_raw_object_id""",
        ),
        (
            """    CHECK(terminal_publication_receipt_sha256 IS NULL
          OR (length(terminal_publication_receipt_sha256)=64
              AND terminal_publication_receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(length(created_at)""",
            """    CHECK(terminal_publication_receipt_sha256 IS NULL
          OR (length(terminal_publication_receipt_sha256)=64
              AND terminal_publication_receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK((restart_count=0
           AND restart_rebound_at IS NULL
           AND restart_file_input_identity_sha256 IS NULL
           AND restart_file_idempotency_key_sha256 IS NULL
           AND restart_web_input_identity_sha256 IS NULL
           AND restart_web_idempotency_key_sha256 IS NULL
           AND restart_synthesis_input_identity_sha256 IS NULL
           AND restart_synthesis_idempotency_key_sha256 IS NULL)
          OR (restart_count=1
              AND length(restart_rebound_at) BETWEEN 20 AND 64
              AND unixepoch(restart_rebound_at) IS NOT NULL
              AND restart_rebound_at>=created_at AND restart_rebound_at<=updated_at
              AND length(restart_file_input_identity_sha256)=64
              AND restart_file_input_identity_sha256 NOT GLOB '*[^0-9a-f]*'
              AND length(restart_file_idempotency_key_sha256)=64
              AND restart_file_idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'
              AND length(restart_web_input_identity_sha256)=64
              AND restart_web_input_identity_sha256 NOT GLOB '*[^0-9a-f]*'
              AND length(restart_web_idempotency_key_sha256)=64
              AND restart_web_idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'
              AND length(restart_synthesis_input_identity_sha256)=64
              AND restart_synthesis_input_identity_sha256 NOT GLOB '*[^0-9a-f]*'
              AND length(restart_synthesis_idempotency_key_sha256)=64
              AND restart_synthesis_idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(transition<>'restart_rebind' OR restart_count=1),
    CHECK(length(created_at)""",
        ),
    )
    for old, new in replacements:
        if schema.count(old) != 1:
            raise RuntimeError("released schema-44 transformation anchor is ambiguous")
        schema = schema.replace(old, new)

    # These eight bindings are process/plan authority, not durable request
    # identity.  They may change only through the exact one-shot trigger below.
    mutable_identity_lines = """  OR NEW.proposal_sha256<>OLD.proposal_sha256
  OR NEW.accepted_plan_sha256<>OLD.accepted_plan_sha256
  OR NEW.manifest_sha256<>OLD.manifest_sha256
  OR NEW.policy_sha256<>OLD.policy_sha256
  OR NEW.runtime_profile_sha256<>OLD.runtime_profile_sha256
  OR NEW.adapter_registry_sha256<>OLD.adapter_registry_sha256
  OR NEW.actor_binding_sha256<>OLD.actor_binding_sha256
  OR NEW.conversation_binding_sha256<>OLD.conversation_binding_sha256
"""
    if schema.count(mutable_identity_lines) != 1:
        raise RuntimeError("released graph mutable-identity anchor is ambiguous")
    schema = schema.replace(mutable_identity_lines, "")

    history_sql = f"""
CREATE TABLE IF NOT EXISTS work_item_compare_current_file_web_restart_rebinds (
    graph_id TEXT PRIMARY KEY
        REFERENCES work_item_compare_current_file_web_graphs(id) ON DELETE CASCADE,
    from_revision INTEGER NOT NULL CHECK(from_revision BETWEEN 1 AND 2147483646),
    from_transition TEXT NOT NULL CHECK(from_transition IN (
        'admitted','step_claimed','step_settled','step_requeued',
        'review_recovery_admitted'
    )),
    from_updated_at TEXT NOT NULL,
    to_revision INTEGER NOT NULL CHECK(to_revision=from_revision+1),
    rebound_at TEXT NOT NULL,
    predecessor_proposal_sha256 TEXT NOT NULL,
    successor_proposal_sha256 TEXT NOT NULL,
    predecessor_accepted_plan_sha256 TEXT NOT NULL,
    successor_accepted_plan_sha256 TEXT NOT NULL,
    predecessor_manifest_sha256 TEXT NOT NULL,
    successor_manifest_sha256 TEXT NOT NULL,
    predecessor_policy_sha256 TEXT NOT NULL,
    successor_policy_sha256 TEXT NOT NULL,
    predecessor_runtime_profile_sha256 TEXT NOT NULL,
    successor_runtime_profile_sha256 TEXT NOT NULL,
    predecessor_adapter_registry_sha256 TEXT NOT NULL,
    successor_adapter_registry_sha256 TEXT NOT NULL,
    predecessor_actor_binding_sha256 TEXT NOT NULL,
    successor_actor_binding_sha256 TEXT NOT NULL,
    predecessor_conversation_binding_sha256 TEXT NOT NULL,
    successor_conversation_binding_sha256 TEXT NOT NULL,
    CHECK(length(from_updated_at) BETWEEN 20 AND 64
          AND unixepoch(from_updated_at) IS NOT NULL
          AND length(rebound_at) BETWEEN 20 AND 64
          AND unixepoch(rebound_at) IS NOT NULL
          AND rebound_at>=from_updated_at),
    CHECK(length(predecessor_proposal_sha256)=64
          AND predecessor_proposal_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(successor_proposal_sha256)=64
          AND successor_proposal_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(predecessor_accepted_plan_sha256)=64
          AND predecessor_accepted_plan_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(successor_accepted_plan_sha256)=64
          AND successor_accepted_plan_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(predecessor_manifest_sha256)=64
          AND predecessor_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(successor_manifest_sha256)=64
          AND successor_manifest_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(predecessor_policy_sha256)=64
          AND predecessor_policy_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(successor_policy_sha256)=64
          AND successor_policy_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(predecessor_runtime_profile_sha256)=64
          AND predecessor_runtime_profile_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(successor_runtime_profile_sha256)=64
          AND successor_runtime_profile_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(predecessor_adapter_registry_sha256)=64
          AND predecessor_adapter_registry_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(successor_adapter_registry_sha256)=64
          AND successor_adapter_registry_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(predecessor_actor_binding_sha256)=64
          AND predecessor_actor_binding_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(successor_actor_binding_sha256)=64
          AND successor_actor_binding_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(predecessor_conversation_binding_sha256)=64
          AND predecessor_conversation_binding_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(successor_conversation_binding_sha256)=64
          AND successor_conversation_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(predecessor_actor_binding_sha256<>successor_actor_binding_sha256),
    CHECK(predecessor_conversation_binding_sha256<>successor_conversation_binding_sha256)
);

CREATE TABLE IF NOT EXISTS work_item_compare_current_file_web_restart_rebind_steps (
    graph_id TEXT NOT NULL
        REFERENCES work_item_compare_current_file_web_restart_rebinds(graph_id)
        ON DELETE CASCADE,
    step_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'file_current_read','web_current_read','primary_synthesis'
    )),
    predecessor_input_identity_sha256 TEXT NOT NULL,
    successor_input_identity_sha256 TEXT NOT NULL,
    predecessor_idempotency_key_sha256 TEXT NOT NULL,
    successor_idempotency_key_sha256 TEXT NOT NULL,
    predecessor_state TEXT NOT NULL CHECK(predecessor_state IN (
        'pending','running','complete','partial','empty','unavailable','denied','failed'
    )),
    predecessor_attempt INTEGER NOT NULL CHECK(
        predecessor_attempt BETWEEN 0 AND {COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS - 1}
    ),
    predecessor_outcome_sha256 TEXT,
    predecessor_prior_outcome_sha256 TEXT,
    predecessor_recovery_review_sha256 TEXT,
    predecessor_recovery_context_sha256 TEXT,
    predecessor_evidence_identity_sha256 TEXT,
    predecessor_authority_rechecked INTEGER NOT NULL CHECK(
        predecessor_authority_rechecked IN (0,1)
    ),
    predecessor_verified INTEGER NOT NULL CHECK(predecessor_verified IN (0,1)),
    predecessor_started_at TEXT,
    predecessor_settled_at TEXT,
    successor_attempt INTEGER NOT NULL CHECK(
        successor_attempt=predecessor_attempt
        AND successor_attempt BETWEEN 0 AND {COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS - 1}
    ),
    successor_prior_outcome_sha256 TEXT,
    PRIMARY KEY(graph_id,step_id),
    UNIQUE(graph_id,kind),
    CHECK((kind='file_current_read' AND step_id='read_current_file')
          OR (kind='web_current_read' AND step_id='read_current_web')
          OR (kind='primary_synthesis' AND step_id='primary_synthesis')),
    CHECK(length(predecessor_input_identity_sha256)=64
          AND predecessor_input_identity_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(successor_input_identity_sha256)=64
          AND successor_input_identity_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(predecessor_idempotency_key_sha256)=64
          AND predecessor_idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'
          AND length(successor_idempotency_key_sha256)=64
          AND successor_idempotency_key_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK(predecessor_outcome_sha256 IS NULL
          OR (length(predecessor_outcome_sha256)=64
              AND predecessor_outcome_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(predecessor_prior_outcome_sha256 IS NULL
          OR (length(predecessor_prior_outcome_sha256)=64
              AND predecessor_prior_outcome_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(predecessor_recovery_review_sha256 IS NULL
          OR (length(predecessor_recovery_review_sha256)=64
              AND predecessor_recovery_review_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(predecessor_recovery_context_sha256 IS NULL
          OR (length(predecessor_recovery_context_sha256)=64
              AND predecessor_recovery_context_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(predecessor_evidence_identity_sha256 IS NULL
          OR (length(predecessor_evidence_identity_sha256)=64
              AND predecessor_evidence_identity_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK(successor_prior_outcome_sha256 IS NULL
          OR (length(successor_prior_outcome_sha256)=64
              AND successor_prior_outcome_sha256 NOT GLOB '*[^0-9a-f]*')),
    CHECK((predecessor_started_at IS NULL
           OR (length(predecessor_started_at) BETWEEN 20 AND 64
               AND unixepoch(predecessor_started_at) IS NOT NULL))
          AND (predecessor_settled_at IS NULL
               OR (length(predecessor_settled_at) BETWEEN 20 AND 64
                   AND unixepoch(predecessor_settled_at) IS NOT NULL))),
    CHECK(successor_prior_outcome_sha256 IS
          CASE WHEN predecessor_state IN (
              'complete','partial','empty','unavailable','denied','failed'
          ) THEN predecessor_outcome_sha256
          ELSE predecessor_prior_outcome_sha256 END)
);

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_restart_rebinds_insert_guard
BEFORE INSERT ON work_item_compare_current_file_web_restart_rebinds
WHEN NOT EXISTS (
    SELECT 1 FROM work_item_compare_current_file_web_graphs graph
     WHERE graph.id=NEW.graph_id AND graph.state='active'
       AND graph.transition='restart_rebind' AND graph.restart_count=1
       AND graph.revision=NEW.to_revision AND graph.updated_at=NEW.rebound_at
       AND graph.proposal_sha256=NEW.successor_proposal_sha256
       AND graph.accepted_plan_sha256=NEW.successor_accepted_plan_sha256
       AND graph.manifest_sha256=NEW.successor_manifest_sha256
       AND graph.policy_sha256=NEW.successor_policy_sha256
       AND graph.runtime_profile_sha256=NEW.successor_runtime_profile_sha256
       AND graph.adapter_registry_sha256=NEW.successor_adapter_registry_sha256
       AND graph.actor_binding_sha256=NEW.successor_actor_binding_sha256
       AND graph.conversation_binding_sha256=NEW.successor_conversation_binding_sha256
)
BEGIN SELECT RAISE(ABORT,'restart rebind audit has no exact successor graph'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_restart_rebinds_update_immutable
BEFORE UPDATE ON work_item_compare_current_file_web_restart_rebinds
BEGIN SELECT RAISE(ABORT,'restart rebind audit is immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_restart_rebinds_delete_immutable
BEFORE DELETE ON work_item_compare_current_file_web_restart_rebinds
WHEN EXISTS (
    SELECT 1 FROM work_item_compare_current_file_web_graphs graph
     WHERE graph.id=OLD.graph_id
)
BEGIN SELECT RAISE(ABORT,'restart rebind audit is immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_restart_steps_insert_guard
BEFORE INSERT ON work_item_compare_current_file_web_restart_rebind_steps
WHEN NOT EXISTS (
        SELECT 1 FROM work_item_compare_current_file_web_restart_rebinds audit
         WHERE audit.graph_id=NEW.graph_id
    )
 OR NOT EXISTS (
        SELECT 1 FROM work_item_compare_current_file_web_steps step
         WHERE step.graph_id=NEW.graph_id AND step.step_id=NEW.step_id
           AND step.kind=NEW.kind
           AND step.input_identity_sha256=NEW.predecessor_input_identity_sha256
           AND step.idempotency_key_sha256=NEW.predecessor_idempotency_key_sha256
           AND step.state=NEW.predecessor_state
           AND step.attempt=NEW.predecessor_attempt
           AND step.outcome_sha256 IS NEW.predecessor_outcome_sha256
           AND step.prior_outcome_sha256 IS NEW.predecessor_prior_outcome_sha256
           AND step.recovery_review_sha256 IS NEW.predecessor_recovery_review_sha256
           AND step.recovery_context_sha256 IS NEW.predecessor_recovery_context_sha256
           AND step.evidence_identity_sha256 IS NEW.predecessor_evidence_identity_sha256
           AND step.authority_rechecked=NEW.predecessor_authority_rechecked
           AND step.verified=NEW.predecessor_verified
           AND step.started_at IS NEW.predecessor_started_at
           AND step.settled_at IS NEW.predecessor_settled_at
    )
BEGIN SELECT RAISE(ABORT,'restart rebind step audit is not predecessor-exact'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_restart_steps_update_immutable
BEFORE UPDATE ON work_item_compare_current_file_web_restart_rebind_steps
BEGIN SELECT RAISE(ABORT,'restart rebind step audit is immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_restart_steps_delete_immutable
BEFORE DELETE ON work_item_compare_current_file_web_restart_rebind_steps
WHEN EXISTS (
    SELECT 1 FROM work_item_compare_current_file_web_graphs graph
     WHERE graph.id=OLD.graph_id
)
BEGIN SELECT RAISE(ABORT,'restart rebind step audit is immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_graphs_restart_apply
AFTER UPDATE ON work_item_compare_current_file_web_graphs
WHEN OLD.restart_count=0 AND NEW.restart_count=1 AND NEW.transition='restart_rebind'
BEGIN
    INSERT INTO work_item_compare_current_file_web_restart_rebinds(
        graph_id,from_revision,from_transition,from_updated_at,to_revision,rebound_at,
        predecessor_proposal_sha256,successor_proposal_sha256,
        predecessor_accepted_plan_sha256,successor_accepted_plan_sha256,
        predecessor_manifest_sha256,successor_manifest_sha256,
        predecessor_policy_sha256,successor_policy_sha256,
        predecessor_runtime_profile_sha256,successor_runtime_profile_sha256,
        predecessor_adapter_registry_sha256,successor_adapter_registry_sha256,
        predecessor_actor_binding_sha256,successor_actor_binding_sha256,
        predecessor_conversation_binding_sha256,successor_conversation_binding_sha256
    ) VALUES(
        OLD.id,OLD.revision,OLD.transition,OLD.updated_at,NEW.revision,NEW.updated_at,
        OLD.proposal_sha256,NEW.proposal_sha256,
        OLD.accepted_plan_sha256,NEW.accepted_plan_sha256,
        OLD.manifest_sha256,NEW.manifest_sha256,
        OLD.policy_sha256,NEW.policy_sha256,
        OLD.runtime_profile_sha256,NEW.runtime_profile_sha256,
        OLD.adapter_registry_sha256,NEW.adapter_registry_sha256,
        OLD.actor_binding_sha256,NEW.actor_binding_sha256,
        OLD.conversation_binding_sha256,NEW.conversation_binding_sha256
    );
    INSERT INTO work_item_compare_current_file_web_restart_rebind_steps(
        graph_id,step_id,kind,
        predecessor_input_identity_sha256,successor_input_identity_sha256,
        predecessor_idempotency_key_sha256,successor_idempotency_key_sha256,
        predecessor_state,predecessor_attempt,predecessor_outcome_sha256,
        predecessor_prior_outcome_sha256,predecessor_recovery_review_sha256,
        predecessor_recovery_context_sha256,predecessor_evidence_identity_sha256,
        predecessor_authority_rechecked,predecessor_verified,
        predecessor_started_at,predecessor_settled_at,
        successor_attempt,successor_prior_outcome_sha256
    )
    SELECT step.graph_id,step.step_id,step.kind,
           step.input_identity_sha256,
           CASE step.step_id
             WHEN 'read_current_file' THEN NEW.restart_file_input_identity_sha256
             WHEN 'read_current_web' THEN NEW.restart_web_input_identity_sha256
             ELSE NEW.restart_synthesis_input_identity_sha256 END,
           step.idempotency_key_sha256,
           CASE step.step_id
             WHEN 'read_current_file' THEN NEW.restart_file_idempotency_key_sha256
             WHEN 'read_current_web' THEN NEW.restart_web_idempotency_key_sha256
             ELSE NEW.restart_synthesis_idempotency_key_sha256 END,
           step.state,step.attempt,step.outcome_sha256,step.prior_outcome_sha256,
           step.recovery_review_sha256,step.recovery_context_sha256,
           step.evidence_identity_sha256,step.authority_rechecked,step.verified,
           step.started_at,step.settled_at,step.attempt,
           CASE WHEN step.state IN (
               'complete','partial','empty','unavailable','denied','failed'
           ) THEN step.outcome_sha256 ELSE step.prior_outcome_sha256 END
      FROM work_item_compare_current_file_web_steps step
     WHERE step.graph_id=NEW.id;
    UPDATE work_item_compare_current_file_web_steps
       SET input_identity_sha256=CASE step_id
             WHEN 'read_current_file' THEN NEW.restart_file_input_identity_sha256
             WHEN 'read_current_web' THEN NEW.restart_web_input_identity_sha256
             ELSE NEW.restart_synthesis_input_identity_sha256 END,
           idempotency_key_sha256=CASE step_id
             WHEN 'read_current_file' THEN NEW.restart_file_idempotency_key_sha256
             WHEN 'read_current_web' THEN NEW.restart_web_idempotency_key_sha256
             ELSE NEW.restart_synthesis_idempotency_key_sha256 END,
           state='pending',
           outcome_sha256=NULL,
           prior_outcome_sha256=CASE WHEN state IN (
               'complete','partial','empty','unavailable','denied','failed'
           ) THEN outcome_sha256 ELSE prior_outcome_sha256 END,
           recovery_review_sha256=NULL,recovery_context_sha256=NULL,
           evidence_identity_sha256=NULL,authority_rechecked=0,verified=0,
           started_at=NULL,settled_at=NULL
     WHERE graph_id=NEW.id;
    SELECT RAISE(ABORT,'restart rebind did not reset the exact fixed graph')
     WHERE (SELECT COUNT(*) FROM work_item_compare_current_file_web_steps
             WHERE graph_id=NEW.id)<>3
        OR EXISTS (
            SELECT 1 FROM work_item_compare_current_file_web_steps step
             WHERE step.graph_id=NEW.id
               AND (step.state<>'pending' OR step.attempt>={COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS})
        );
END;
"""

    scope_trigger = (
        "CREATE TRIGGER IF NOT EXISTS "
        "trg_work_item_compare_current_file_web_graphs_scope_insert"
    )
    if schema.count(scope_trigger) != 1:
        raise RuntimeError("released graph scope trigger anchor is ambiguous")
    schema = schema.replace(scope_trigger, history_sql + "\n" + scope_trigger)

    control_trigger = f"""
CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_graphs_control_rebind
BEFORE UPDATE ON work_item_compare_current_file_web_graphs
WHEN (
       NEW.proposal_sha256<>OLD.proposal_sha256
    OR NEW.accepted_plan_sha256<>OLD.accepted_plan_sha256
    OR NEW.manifest_sha256<>OLD.manifest_sha256
    OR NEW.policy_sha256<>OLD.policy_sha256
    OR NEW.runtime_profile_sha256<>OLD.runtime_profile_sha256
    OR NEW.adapter_registry_sha256<>OLD.adapter_registry_sha256
    OR NEW.actor_binding_sha256<>OLD.actor_binding_sha256
    OR NEW.conversation_binding_sha256<>OLD.conversation_binding_sha256
    OR NEW.restart_count<>OLD.restart_count
    OR NEW.restart_rebound_at IS NOT OLD.restart_rebound_at
    OR NEW.restart_file_input_identity_sha256 IS NOT OLD.restart_file_input_identity_sha256
    OR NEW.restart_file_idempotency_key_sha256 IS NOT OLD.restart_file_idempotency_key_sha256
    OR NEW.restart_web_input_identity_sha256 IS NOT OLD.restart_web_input_identity_sha256
    OR NEW.restart_web_idempotency_key_sha256 IS NOT OLD.restart_web_idempotency_key_sha256
    OR NEW.restart_synthesis_input_identity_sha256 IS NOT OLD.restart_synthesis_input_identity_sha256
    OR NEW.restart_synthesis_idempotency_key_sha256 IS NOT OLD.restart_synthesis_idempotency_key_sha256
)
AND NOT (
    OLD.state='active' AND NEW.state='active'
    AND OLD.restart_count=0 AND NEW.restart_count=1
    AND OLD.restart_rebound_at IS NULL AND NEW.restart_rebound_at=NEW.updated_at
    AND NEW.transition='restart_rebind' AND NEW.revision=OLD.revision+1
    AND NEW.actor_binding_sha256<>OLD.actor_binding_sha256
    AND NEW.conversation_binding_sha256<>OLD.conversation_binding_sha256
    AND (SELECT COUNT(*) FROM work_item_compare_current_file_web_steps step
          WHERE step.graph_id=OLD.id)=3
    AND NOT EXISTS (
        SELECT 1 FROM work_item_compare_current_file_web_steps step
         WHERE step.graph_id=OLD.id AND step.attempt>={COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS}
    )
)
BEGIN SELECT RAISE(ABORT,'current-file/web WorkGraph control rebind is invalid'); END;

CREATE TRIGGER IF NOT EXISTS trg_work_item_compare_current_file_web_graphs_restart_transition
BEFORE UPDATE ON work_item_compare_current_file_web_graphs
WHEN NEW.transition='restart_rebind'
 AND NOT (OLD.restart_count=0 AND NEW.restart_count=1
          AND NEW.revision=OLD.revision+1 AND NEW.restart_rebound_at=NEW.updated_at)
BEGIN SELECT RAISE(ABORT,'current-file/web WorkGraph restart rebind is not one-shot'); END;

"""
    lifecycle_trigger = (
        "CREATE TRIGGER IF NOT EXISTS "
        "trg_work_item_compare_current_file_web_graphs_lifecycle"
    )
    if schema.count(lifecycle_trigger) != 1:
        raise RuntimeError("released graph lifecycle trigger anchor is ambiguous")
    schema = schema.replace(lifecycle_trigger, control_trigger + lifecycle_trigger)

    old_step_identity_tail = """  OR NEW.input_identity_sha256<>OLD.input_identity_sha256
  OR NEW.idempotency_key_sha256<>OLD.idempotency_key_sha256
  OR NEW.outcome_schema<>OLD.outcome_schema
"""
    new_step_identity_tail = """  OR NEW.outcome_schema<>OLD.outcome_schema
  OR ((NEW.input_identity_sha256<>OLD.input_identity_sha256
       OR NEW.idempotency_key_sha256<>OLD.idempotency_key_sha256)
      AND NOT EXISTS (
          SELECT 1 FROM work_item_compare_current_file_web_graphs graph
           WHERE graph.id=OLD.graph_id AND graph.state='active'
             AND graph.transition='restart_rebind' AND graph.restart_count=1
             AND OLD.attempt<2
             AND NEW.input_identity_sha256=CASE OLD.step_id
                 WHEN 'read_current_file' THEN graph.restart_file_input_identity_sha256
                 WHEN 'read_current_web' THEN graph.restart_web_input_identity_sha256
                 ELSE graph.restart_synthesis_input_identity_sha256 END
             AND NEW.idempotency_key_sha256=CASE OLD.step_id
                 WHEN 'read_current_file' THEN graph.restart_file_idempotency_key_sha256
                 WHEN 'read_current_web' THEN graph.restart_web_idempotency_key_sha256
                 ELSE graph.restart_synthesis_idempotency_key_sha256 END
      ))
"""
    if schema.count(old_step_identity_tail) != 1:
        raise RuntimeError("released step identity trigger anchor is ambiguous")
    schema = schema.replace(old_step_identity_tail, new_step_identity_tail)

    restart_lifecycle_branch = f"""    OR
    (NEW.state='pending' AND NEW.attempt=OLD.attempt
     AND OLD.attempt<{COMPARE_CURRENT_FILE_WEB_MAX_ATTEMPTS}
     AND NEW.started_at IS NULL AND NEW.settled_at IS NULL
     AND NEW.outcome_sha256 IS NULL AND NEW.evidence_identity_sha256 IS NULL
     AND NEW.prior_outcome_sha256 IS CASE WHEN OLD.state IN (
         'complete','partial','empty','unavailable','denied','failed'
     ) THEN OLD.outcome_sha256 ELSE OLD.prior_outcome_sha256 END
     AND NEW.recovery_review_sha256 IS NULL
     AND NEW.recovery_context_sha256 IS NULL
     AND NEW.authority_rechecked=0 AND NEW.verified=0
     AND EXISTS (
         SELECT 1 FROM work_item_compare_current_file_web_graphs graph
          WHERE graph.id=OLD.graph_id AND graph.state='active'
            AND graph.transition='restart_rebind' AND graph.restart_count=1
            AND NEW.input_identity_sha256=CASE OLD.step_id
                WHEN 'read_current_file' THEN graph.restart_file_input_identity_sha256
                WHEN 'read_current_web' THEN graph.restart_web_input_identity_sha256
                ELSE graph.restart_synthesis_input_identity_sha256 END
            AND NEW.idempotency_key_sha256=CASE OLD.step_id
                WHEN 'read_current_file' THEN graph.restart_file_idempotency_key_sha256
                WHEN 'read_current_web' THEN graph.restart_web_idempotency_key_sha256
                ELSE graph.restart_synthesis_idempotency_key_sha256 END
     ))
"""
    settled_restart_anchor = """    OR
    (OLD.state IN ('complete','partial','empty') AND NEW.state='pending'
"""
    if schema.count(settled_restart_anchor) != 1:
        raise RuntimeError("released step restart lifecycle anchor is ambiguous")
    schema = schema.replace(
        settled_restart_anchor,
        restart_lifecycle_branch + settled_restart_anchor,
    )
    return schema


_WORK_ITEM_SCHEMA_45_EXTENSION = _schema_45_extension_from_44()
_WORK_ITEM_SCHEMA_45 = _WORK_ITEM_SCHEMA_42 + _WORK_ITEM_SCHEMA_45_EXTENSION
WORK_ITEM_SCHEMA = _WORK_ITEM_SCHEMA_45


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
def _canonical_schema_44_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        _execute_schema(conn, _WORK_ITEM_SCHEMA_44)
        return _schema_objects(conn, current=True)
    finally:
        conn.close()


@lru_cache(maxsize=1)
def _canonical_schema_42_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        _execute_schema(conn, _WORK_ITEM_SCHEMA_42)
        return _schema_objects_for_tables(conn, _SCHEMA_42_TABLES)
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


@lru_cache(maxsize=1)
def _canonical_schema_40_objects() -> dict[tuple[str, str], str]:
    conn = sqlite3.connect(":memory:")
    try:
        register_work_item_connection_functions(conn)
        _execute_schema(conn, _WORK_ITEM_SCHEMA_40)
        return _schema_objects_for_tables(conn, _SCHEMA_40_TABLES)
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
    from friday.interaction_control_plane.compare_conversation_document_store import (
        _fetch as _fetch_compare_item,
    )
    from friday.interaction_control_plane.compare_conversation_document_store import (
        _validate_stored_item as _validate_compare_stored_item,
    )
    from friday.interaction_control_plane.compare_current_file_web_work_graph_store import (
        _fetch as _fetch_current_file_web_graph,
    )
    from friday.interaction_control_plane.compare_current_file_web_work_graph_store import (
        _validate_stored_graph as _validate_current_file_web_graph,
    )
    from friday.interaction_control_plane.selected_archive_evidence import (
        SelectedArchiveEvidence,
        SelectedArchiveEvidenceError,
    )
    from friday.interaction_control_plane.work_item_contract import (
        RecallConversationWorkItem,
        WorkItemContractError,
    )

    graph_rows = conn.execute(
        """SELECT id,user_id,conversation_id
             FROM work_item_compare_current_file_web_graphs
            ORDER BY id"""
    ).fetchall()
    for graph_row in graph_rows:
        graph_id = str(graph_row["id"] if isinstance(graph_row, sqlite3.Row) else graph_row[0])
        user_id = str(graph_row["user_id"] if isinstance(graph_row, sqlite3.Row) else graph_row[1])
        conversation_id = str(
            graph_row["conversation_id"] if isinstance(graph_row, sqlite3.Row) else graph_row[2]
        )
        try:
            graph = _fetch_current_file_web_graph(
                conn,
                graph_id=graph_id,
                user_id=user_id,
                conversation_id=conversation_id,
            )
            if graph is None:
                raise sqlite3.DatabaseError("Schema 45 WorkGraph disappeared during validation")
            _validate_current_file_web_graph(conn, graph)
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise sqlite3.DatabaseError("Schema 45 current-file/web WorkGraph data is invalid") from exc

    graph_orphan = conn.execute(
        """SELECT 1
             FROM work_item_compare_current_file_web_steps step
             LEFT JOIN work_item_compare_current_file_web_graphs graph
               ON graph.id=step.graph_id
            WHERE graph.id IS NULL
            LIMIT 1"""
    ).fetchone()
    if graph_orphan is not None:
        raise sqlite3.DatabaseError("Schema 45 current-file/web WorkGraph step is orphaned")

    restart_mismatch = conn.execute(
        """SELECT 1
             FROM work_item_compare_current_file_web_graphs graph
             LEFT JOIN work_item_compare_current_file_web_restart_rebinds audit
               ON audit.graph_id=graph.id
            WHERE (graph.restart_count=0 AND audit.graph_id IS NOT NULL)
               OR (graph.restart_count=1 AND (
                      audit.graph_id IS NULL
                   OR audit.to_revision>graph.revision
                   OR audit.rebound_at<>graph.restart_rebound_at
                   OR audit.successor_proposal_sha256<>graph.proposal_sha256
                   OR audit.successor_accepted_plan_sha256<>graph.accepted_plan_sha256
                   OR audit.successor_manifest_sha256<>graph.manifest_sha256
                   OR audit.successor_policy_sha256<>graph.policy_sha256
                   OR audit.successor_runtime_profile_sha256<>graph.runtime_profile_sha256
                   OR audit.successor_adapter_registry_sha256<>graph.adapter_registry_sha256
                   OR audit.successor_actor_binding_sha256<>graph.actor_binding_sha256
                   OR audit.successor_conversation_binding_sha256
                      <>graph.conversation_binding_sha256
                   OR (SELECT COUNT(*)
                         FROM work_item_compare_current_file_web_restart_rebind_steps history
                        WHERE history.graph_id=graph.id)<>3
                   OR EXISTS (
                       SELECT 1
                         FROM work_item_compare_current_file_web_restart_rebind_steps history
                         JOIN work_item_compare_current_file_web_steps step
                           ON step.graph_id=history.graph_id
                          AND step.step_id=history.step_id
                        WHERE history.graph_id=graph.id
                          AND (step.input_identity_sha256
                               <>history.successor_input_identity_sha256
                               OR step.idempotency_key_sha256
                                  <>history.successor_idempotency_key_sha256)
                   )
               ))
            LIMIT 1"""
    ).fetchone()
    if restart_mismatch is not None:
        raise sqlite3.DatabaseError("Schema 45 restart rebind audit is inconsistent")

    restart_orphan = conn.execute(
        """SELECT 1
             FROM work_item_compare_current_file_web_restart_rebind_steps history
             LEFT JOIN work_item_compare_current_file_web_steps step
               ON step.graph_id=history.graph_id AND step.step_id=history.step_id
            WHERE step.graph_id IS NULL
            LIMIT 1"""
    ).fetchone()
    if restart_orphan is not None:
        raise sqlite3.DatabaseError("Schema 45 restart rebind step audit is orphaned")

    recall_cursor = conn.execute("SELECT * FROM work_items WHERE kind='recall_conversation' ORDER BY id")
    recall_columns = tuple(str(item[0]) for item in recall_cursor.description or ())
    for raw in recall_cursor.fetchall():
        row = dict(raw) if isinstance(raw, sqlite3.Row) else dict(zip(recall_columns, raw, strict=True))
        try:
            RecallConversationWorkItem.from_storage_row(row)
        except WorkItemContractError as exc:
            raise sqlite3.DatabaseError("Schema 42 RecallConversation data is invalid") from exc

    mismatch = conn.execute(
        """SELECT 1
             FROM work_items work
             LEFT JOIN work_item_selected_evidence evidence
               ON evidence.work_item_id=work.id
            WHERE (work.kind IN ('recall_selected_archive_evidence',
                                 'compare_conversation_with_document')
                   AND evidence.work_item_id IS NULL)
               OR (work.kind NOT IN ('recall_selected_archive_evidence',
                                     'compare_conversation_with_document')
                   AND evidence.work_item_id IS NOT NULL)
            LIMIT 1"""
    ).fetchone()
    if mismatch is not None:
        raise sqlite3.DatabaseError("Schema 42 selected evidence cardinality is invalid")

    compare_mismatch = conn.execute(
        """SELECT 1 FROM work_items work
            WHERE (work.kind='compare_conversation_with_document'
                   AND (SELECT COUNT(*) FROM work_item_compare_document_questions question
                         WHERE question.work_item_id=work.id) NOT BETWEEN 1 AND 2)
               OR (work.kind<>'compare_conversation_with_document'
                   AND (SELECT COUNT(*) FROM work_item_compare_document_questions question
                         WHERE question.work_item_id=work.id)<>0)
               OR (work.kind='compare_conversation_with_document'
                   AND ((work.state='waiting_for_input'
                         AND (SELECT COUNT(*) FROM work_item_compare_document_evidence document
                               WHERE document.work_item_id=work.id)<>0)
                        OR (work.state IN ('active','completed')
                            AND (SELECT COUNT(*) FROM work_item_compare_document_evidence document
                                  WHERE document.work_item_id=work.id)<>1)
                        OR (work.state='completed'
                            AND (SELECT COUNT(*) FROM work_item_compare_outcomes outcome
                                  WHERE outcome.work_item_id=work.id)<>1)
                        OR (work.state<>'completed'
                            AND (SELECT COUNT(*) FROM work_item_compare_outcomes outcome
                                  WHERE outcome.work_item_id=work.id)<>0)))
               OR (work.kind<>'compare_conversation_with_document'
                   AND ((SELECT COUNT(*) FROM work_item_compare_document_evidence document
                          WHERE document.work_item_id=work.id)<>0
                        OR (SELECT COUNT(*) FROM work_item_compare_outcomes outcome
                             WHERE outcome.work_item_id=work.id)<>0))
            LIMIT 1"""
    ).fetchone()
    if compare_mismatch is not None:
        raise sqlite3.DatabaseError("Schema 42 comparison question cardinality is invalid")

    compare_orphan = conn.execute(
        """SELECT 1
              FROM work_item_compare_document_questions question
              LEFT JOIN work_items work ON work.id=question.work_item_id
             WHERE work.id IS NULL OR work.kind<>'compare_conversation_with_document'
            UNION ALL
            SELECT 1
              FROM work_item_compare_document_evidence document
              LEFT JOIN work_items work ON work.id=document.work_item_id
             WHERE work.id IS NULL OR work.kind<>'compare_conversation_with_document'
            UNION ALL
            SELECT 1
              FROM work_item_compare_outcomes outcome
              LEFT JOIN work_items work ON work.id=outcome.work_item_id
             WHERE work.id IS NULL OR work.kind<>'compare_conversation_with_document'
             LIMIT 1"""
    ).fetchone()
    if compare_orphan is not None:
        raise sqlite3.DatabaseError("Schema 42 comparison sidecar ownership is invalid")

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
                    work.kind='compare_conversation_with_document'
                    AND (((SELECT COUNT(*) FROM work_item_compare_document_questions question
                            WHERE question.work_item_id=work.id)=2
                          AND ((SELECT COUNT(*) FROM work_item_archive_candidate_sets candidate_set
                                 WHERE candidate_set.work_item_id=work.id)<>1
                               OR (SELECT COUNT(*) FROM work_item_archive_candidate_set_items item
                                    WHERE item.work_item_id=work.id) NOT BETWEEN 2 AND ?))
                         OR ((SELECT COUNT(*) FROM work_item_compare_document_questions question
                               WHERE question.work_item_id=work.id)=1
                             AND ((SELECT COUNT(*) FROM work_item_archive_candidate_sets candidate_set
                                    WHERE candidate_set.work_item_id=work.id)<>0
                                  OR (SELECT COUNT(*) FROM work_item_archive_candidate_set_items item
                                       WHERE item.work_item_id=work.id)<>0))
                         OR (SELECT COUNT(*) FROM work_item_archive_candidate_questions question
                              WHERE question.work_item_id=work.id)<>0)
                  )
               OR (
                    work.kind NOT IN ('select_archive_candidate_and_replay_evidence',
                                      'compare_conversation_with_document')
                    AND ((SELECT COUNT(*) FROM work_item_archive_candidate_sets candidate_set
                           WHERE candidate_set.work_item_id=work.id)<>0
                         OR (SELECT COUNT(*) FROM work_item_archive_candidate_questions question
                              WHERE question.work_item_id=work.id)<>0
                         OR (SELECT COUNT(*)
                               FROM work_item_archive_candidate_set_items item
                              WHERE item.work_item_id=work.id)<>0)
                  )
            LIMIT 1""",
        (ARCHIVE_CANDIDATE_MAX_COUNT, ARCHIVE_CANDIDATE_MAX_COUNT),
    ).fetchone()
    if candidate_mismatch is not None:
        raise sqlite3.DatabaseError("Schema 42 candidate sidecar cardinality is invalid")
    candidate_orphan = conn.execute(
        """SELECT 1
              FROM work_item_archive_candidate_sets candidate_set
              LEFT JOIN work_items work ON work.id=candidate_set.work_item_id
             WHERE work.id IS NULL
                OR work.kind NOT IN ('select_archive_candidate_and_replay_evidence',
                                     'compare_conversation_with_document')
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
        raise sqlite3.DatabaseError("Schema 42 candidate sidecar ownership is invalid")
    cursor = conn.execute("SELECT * FROM work_item_selected_evidence ORDER BY work_item_id")
    columns = tuple(str(item[0]) for item in cursor.description or ())
    for raw in cursor.fetchall():
        row = dict(raw) if isinstance(raw, sqlite3.Row) else dict(zip(columns, raw, strict=True))
        try:
            evidence = SelectedArchiveEvidence.from_storage_row(row)
        except SelectedArchiveEvidenceError as exc:
            raise sqlite3.DatabaseError("Schema 42 selected evidence identity is invalid") from exc
        work_cursor = conn.execute("SELECT * FROM work_items WHERE id=?", (evidence.work_item_id,))
        raw_work = work_cursor.fetchone()
        if raw_work is None:  # pragma: no cover - the sidecar FK already proves this
            raise sqlite3.DatabaseError("Schema 42 selected evidence owner is missing")
        work_columns = tuple(str(item[0]) for item in work_cursor.description or ())
        work = (
            dict(raw_work)
            if isinstance(raw_work, sqlite3.Row)
            else dict(zip(work_columns, raw_work, strict=True))
        )
        if evidence.source_ref.principal_id != work.get("user_id"):
            raise sqlite3.DatabaseError("Schema 42 selected evidence owner is invalid")
        if work.get("kind") == WorkKind.RECALL_SELECTED_ARCHIVE_EVIDENCE.value:
            try:
                RecallSelectedArchiveEvidenceWorkItem.from_storage_rows(work, evidence)
            except WorkItemContractError as exc:
                raise sqlite3.DatabaseError("Schema 42 archive Work Item data is invalid") from exc
        boundary = conn.execute(
            """SELECT 1
                 FROM work_items work
                 JOIN messages message
                   ON message.id=?
                  AND message.user_id=work.user_id
                  AND message.conversation_id=work.conversation_id
                  AND message.role='user'
                WHERE work.id=?
                  AND work.kind IN ('recall_selected_archive_evidence',
                                    'compare_conversation_with_document')""",
            (evidence.origin_boundary_user_message_id, evidence.work_item_id),
        ).fetchone()
        if boundary is None:
            raise sqlite3.DatabaseError("Schema 42 selected evidence boundary is invalid")

    compare_cursor = conn.execute(
        """SELECT id,user_id,conversation_id FROM work_items
            WHERE kind='compare_conversation_with_document' ORDER BY id"""
    )
    for raw_work in compare_cursor.fetchall():
        work_id, owner_id, conversation_id = map(str, raw_work)
        try:
            compare_item = _fetch_compare_item(
                conn,
                work_item_id=work_id,
                user_id=owner_id,
                conversation_id=conversation_id,
            )
            if compare_item is None:  # pragma: no cover - selected above
                raise WorkItemContractError("comparison Work Item disappeared")
            _validate_compare_stored_item(
                conn,
                compare_item,
                allow_disabled_owner=True,
                require_latest_message=False,
            )
        except Exception as exc:
            raise sqlite3.DatabaseError("Schema 42 comparison Work Item data is invalid") from exc

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
            candidate_item = ArchiveCandidateSelectionWorkItem.from_storage_rows(
                work,
                candidate_set,
                question,
            )
        except (ArchiveCandidateSelectionError, WorkItemContractError) as exc:
            raise sqlite3.DatabaseError("Schema 40 candidate Work Item data is invalid") from exc
        try:
            _validate_stored_anchor(
                conn,
                candidate_item,
                require_latest_message=False,
                allow_disabled_owner=True,
            )
        except Exception as exc:
            raise sqlite3.DatabaseError("Schema 40 candidate publication receipts are invalid") from exc
        if any(
            candidate.source_ref.principal_id != candidate_item.user_id
            for candidate in candidate_set.candidates
        ):
            raise sqlite3.DatabaseError("Schema 40 candidate owner is invalid")
        boundary = conn.execute(
            """SELECT 1 FROM messages
                WHERE id=? AND user_id=? AND conversation_id=? AND role='user'""",
            (
                candidate_set.origin_boundary_user_message_id,
                candidate_item.user_id,
                candidate_item.conversation_id,
            ),
        ).fetchone()
        if boundary is None:
            raise sqlite3.DatabaseError("Schema 40 candidate origin boundary is invalid")
        if question.prompt_assistant_message_id != candidate_item.anchor_assistant_message_id:
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
                    candidate_item.anchor_assistant_message_id,
                    candidate_item.user_id,
                    candidate_item.conversation_id,
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
                    candidate_item.user_id,
                    candidate_item.conversation_id,
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
                    candidate_item.user_id,
                    candidate_item.conversation_id,
                ),
            ).fetchone()
            if failure is None:
                raise sqlite3.DatabaseError("Schema 40 candidate replay failure boundary is invalid")


def validate_work_item_schema(conn: sqlite3.Connection, *, required: bool = True) -> None:
    """Fail closed when the exact schema-45 Work Item/graph projection is weakened."""

    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_items'").fetchone()
    if row is None:
        if required:
            raise sqlite3.DatabaseError("Schema 45 work item store is missing")
        if _related_schema_objects(conn):
            raise sqlite3.DatabaseError("Schema 45 work item DDL is incomplete or altered")
        return
    register_work_item_connection_functions(conn)
    if _schema_objects(conn, current=True) != _canonical_work_item_schema_objects():
        raise sqlite3.DatabaseError("Schema 45 work item DDL is incomplete or altered")

    expected_index_columns = {
        "idx_work_item_compare_current_file_web_steps_state": ("graph_id", "state", "step_id"),
        "idx_work_item_compare_current_file_web_graphs_expiry": ("state", "expires_at"),
        "idx_work_item_compare_current_file_web_graphs_owner": (
            "user_id",
            "state",
            "updated_at",
            "id",
        ),
        "uq_work_item_compare_current_file_web_graphs_active": ("user_id", "conversation_id"),
        "idx_work_item_compare_outcomes_assistant": ("answer_assistant_message_id",),
        "idx_work_item_compare_document_evidence_origin": (
            "origin_boundary_user_message_id",
            "work_item_id",
        ),
        "uq_work_item_compare_document_question_waiting": ("work_item_id",),
        "idx_work_item_compare_document_questions_work": ("work_item_id", "state", "id"),
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
        raise sqlite3.DatabaseError("Schema 45 work item indexes are invalid")

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
        raise sqlite3.DatabaseError("Schema 42 work item store shape is invalid")
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
        raise sqlite3.DatabaseError("Schema 42 selected evidence store shape is invalid")
    question_columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(work_item_compare_document_questions)")
    }
    if question_columns != {
        "id": ("TEXT", 0, 1),
        "work_item_id": ("TEXT", 1, 0),
        "kind": ("TEXT", 1, 0),
        "admission_shape": ("TEXT", 1, 0),
        "state": ("TEXT", 1, 0),
        "created_at": ("TEXT", 1, 0),
        "prompt_boundary_user_message_id": ("TEXT", 1, 0),
        "prompt_assistant_message_id": ("TEXT", 1, 0),
        "work_revision": ("INTEGER", 1, 0),
        "candidate_set_id": ("TEXT", 0, 0),
        "answered_at": ("TEXT", 0, 0),
        "answer_user_message_id": ("TEXT", 0, 0),
        "selected_ordinal": ("INTEGER", 0, 0),
        "accepted_search_plan_sha256": ("TEXT", 0, 0),
        "accepted_search_outcome_sha256": ("TEXT", 0, 0),
        "closed_at": ("TEXT", 0, 0),
        "close_reason": ("TEXT", 0, 0),
    }:
        raise sqlite3.DatabaseError("Schema 42 comparison question store shape is invalid")
    document_columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(work_item_compare_document_evidence)")
    }
    if document_columns != {
        "work_item_id": ("TEXT", 0, 1),
        "provenance": ("TEXT", 1, 0),
        "source_ref_json": ("TEXT", 1, 0),
        "raw_object_id": ("TEXT", 1, 0),
        "raw_source_identity_sha256": ("TEXT", 1, 0),
        "raw_content_sha256": ("TEXT", 1, 0),
        "content_sha256": ("TEXT", 1, 0),
        "candidate_source_snapshot_sha256": ("TEXT", 0, 0),
        "origin_boundary_user_message_id": ("TEXT", 1, 0),
        "resolved_revision": ("INTEGER", 1, 0),
        "resolved_at": ("TEXT", 1, 0),
        "candidate_set_id": ("TEXT", 0, 0),
        "selected_ordinal": ("INTEGER", 0, 0),
    }:
        raise sqlite3.DatabaseError("Schema 42 comparison document store shape is invalid")
    outcome_columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(work_item_compare_outcomes)")
    }
    if outcome_columns != {
        "work_item_id": ("TEXT", 0, 1),
        "answer_boundary_user_message_id": ("TEXT", 1, 0),
        "answer_assistant_message_id": ("TEXT", 1, 0),
        "accepted_plan_sha256": ("TEXT", 1, 0),
        "accepted_outcome_sha256": ("TEXT", 1, 0),
        "comparison_status": ("TEXT", 1, 0),
        "message_coverage_grade": ("TEXT", 1, 0),
        "document_verification_complete": ("INTEGER", 1, 0),
        "publication_attested": ("INTEGER", 1, 0),
        "semantic_verified": ("INTEGER", 1, 0),
        "message_evidence_sha256": ("TEXT", 1, 0),
        "document_evidence_sha256": ("TEXT", 1, 0),
        "evidence_bundle_sha256": ("TEXT", 1, 0),
        "model_evidence_sha256": ("TEXT", 1, 0),
        "completed_revision": ("INTEGER", 1, 0),
        "completed_at": ("TEXT", 1, 0),
    }:
        raise sqlite3.DatabaseError("Schema 42 comparison outcome store shape is invalid")

    graph_columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(work_item_compare_current_file_web_graphs)")
    }
    if graph_columns != {
        "id": ("TEXT", 0, 1),
        "user_id": ("TEXT", 1, 0),
        "conversation_id": ("TEXT", 1, 0),
        "anchor_user_message_id": ("TEXT", 1, 0),
        "anchor_request_binding_sha256": ("TEXT", 1, 0),
        "current_file_raw_object_id": ("TEXT", 1, 0),
        "state": ("TEXT", 1, 0),
        "revision": ("INTEGER", 1, 0),
        "transition": ("TEXT", 1, 0),
        "proposal_sha256": ("TEXT", 1, 0),
        "accepted_plan_sha256": ("TEXT", 1, 0),
        "manifest_sha256": ("TEXT", 1, 0),
        "policy_sha256": ("TEXT", 1, 0),
        "runtime_profile_sha256": ("TEXT", 1, 0),
        "adapter_registry_sha256": ("TEXT", 1, 0),
        "actor_binding_sha256": ("TEXT", 1, 0),
        "conversation_binding_sha256": ("TEXT", 1, 0),
        "current_file_source_identity_sha256": ("TEXT", 1, 0),
        "current_file_content_sha256": ("TEXT", 1, 0),
        "completion_contract": ("TEXT", 1, 0),
        "fallback_owner": ("TEXT", 1, 0),
        "publication_owner": ("TEXT", 1, 0),
        "max_attempts": ("INTEGER", 1, 0),
        "restart_count": ("INTEGER", 1, 0),
        "restart_rebound_at": ("TEXT", 0, 0),
        "restart_file_input_identity_sha256": ("TEXT", 0, 0),
        "restart_file_idempotency_key_sha256": ("TEXT", 0, 0),
        "restart_web_input_identity_sha256": ("TEXT", 0, 0),
        "restart_web_idempotency_key_sha256": ("TEXT", 0, 0),
        "restart_synthesis_input_identity_sha256": ("TEXT", 0, 0),
        "restart_synthesis_idempotency_key_sha256": ("TEXT", 0, 0),
        "created_at": ("TEXT", 1, 0),
        "updated_at": ("TEXT", 1, 0),
        "expires_at": ("TEXT", 1, 0),
        "closed_at": ("TEXT", 0, 0),
        "outcome_status": ("TEXT", 0, 0),
        "outcome_reason": ("TEXT", 0, 0),
        "publication_assistant_message_id": ("TEXT", 0, 0),
        "accepted_graph_outcome_sha256": ("TEXT", 0, 0),
        "accepted_steps_sha256": ("TEXT", 0, 0),
        "terminal_publication_receipt_sha256": ("TEXT", 0, 0),
        "publication_receipt_sha256": ("TEXT", 0, 0),
    }:
        raise sqlite3.DatabaseError("Schema 45 current-file/web WorkGraph store shape is invalid")

    graph_step_columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute("PRAGMA table_info(work_item_compare_current_file_web_steps)")
    }
    if graph_step_columns != {
        "graph_id": ("TEXT", 1, 1),
        "step_id": ("TEXT", 1, 2),
        "kind": ("TEXT", 1, 0),
        "capability_id": ("TEXT", 1, 0),
        "security_id": ("TEXT", 0, 0),
        "adapter_id": ("TEXT", 0, 0),
        "effect_class": ("TEXT", 1, 0),
        "evidence_replayability": ("TEXT", 1, 0),
        "depends_on_json": ("TEXT", 1, 0),
        "parallel_group": ("TEXT", 0, 0),
        "input_identity_sha256": ("TEXT", 1, 0),
        "idempotency_key_sha256": ("TEXT", 1, 0),
        "state": ("TEXT", 1, 0),
        "attempt": ("INTEGER", 1, 0),
        "outcome_schema": ("TEXT", 1, 0),
        "outcome_sha256": ("TEXT", 0, 0),
        "prior_outcome_sha256": ("TEXT", 0, 0),
        "recovery_review_sha256": ("TEXT", 0, 0),
        "recovery_context_sha256": ("TEXT", 0, 0),
        "evidence_identity_sha256": ("TEXT", 0, 0),
        "authority_rechecked": ("INTEGER", 1, 0),
        "verified": ("INTEGER", 1, 0),
        "started_at": ("TEXT", 0, 0),
        "settled_at": ("TEXT", 0, 0),
    }:
        raise sqlite3.DatabaseError("Schema 45 current-file/web WorkGraph step shape is invalid")

    restart_columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute(
            "PRAGMA table_info(work_item_compare_current_file_web_restart_rebinds)"
        )
    }
    if restart_columns != {
        "graph_id": ("TEXT", 0, 1),
        "from_revision": ("INTEGER", 1, 0),
        "from_transition": ("TEXT", 1, 0),
        "from_updated_at": ("TEXT", 1, 0),
        "to_revision": ("INTEGER", 1, 0),
        "rebound_at": ("TEXT", 1, 0),
        "predecessor_proposal_sha256": ("TEXT", 1, 0),
        "successor_proposal_sha256": ("TEXT", 1, 0),
        "predecessor_accepted_plan_sha256": ("TEXT", 1, 0),
        "successor_accepted_plan_sha256": ("TEXT", 1, 0),
        "predecessor_manifest_sha256": ("TEXT", 1, 0),
        "successor_manifest_sha256": ("TEXT", 1, 0),
        "predecessor_policy_sha256": ("TEXT", 1, 0),
        "successor_policy_sha256": ("TEXT", 1, 0),
        "predecessor_runtime_profile_sha256": ("TEXT", 1, 0),
        "successor_runtime_profile_sha256": ("TEXT", 1, 0),
        "predecessor_adapter_registry_sha256": ("TEXT", 1, 0),
        "successor_adapter_registry_sha256": ("TEXT", 1, 0),
        "predecessor_actor_binding_sha256": ("TEXT", 1, 0),
        "successor_actor_binding_sha256": ("TEXT", 1, 0),
        "predecessor_conversation_binding_sha256": ("TEXT", 1, 0),
        "successor_conversation_binding_sha256": ("TEXT", 1, 0),
    }:
        raise sqlite3.DatabaseError("Schema 45 restart rebind audit shape is invalid")

    restart_step_columns = {
        str(item[1]): (str(item[2]).upper(), int(item[3]), int(item[5]))
        for item in conn.execute(
            "PRAGMA table_info(work_item_compare_current_file_web_restart_rebind_steps)"
        )
    }
    if restart_step_columns != {
        "graph_id": ("TEXT", 1, 1),
        "step_id": ("TEXT", 1, 2),
        "kind": ("TEXT", 1, 0),
        "predecessor_input_identity_sha256": ("TEXT", 1, 0),
        "successor_input_identity_sha256": ("TEXT", 1, 0),
        "predecessor_idempotency_key_sha256": ("TEXT", 1, 0),
        "successor_idempotency_key_sha256": ("TEXT", 1, 0),
        "predecessor_state": ("TEXT", 1, 0),
        "predecessor_attempt": ("INTEGER", 1, 0),
        "predecessor_outcome_sha256": ("TEXT", 0, 0),
        "predecessor_prior_outcome_sha256": ("TEXT", 0, 0),
        "predecessor_recovery_review_sha256": ("TEXT", 0, 0),
        "predecessor_recovery_context_sha256": ("TEXT", 0, 0),
        "predecessor_evidence_identity_sha256": ("TEXT", 0, 0),
        "predecessor_authority_rechecked": ("INTEGER", 1, 0),
        "predecessor_verified": ("INTEGER", 1, 0),
        "predecessor_started_at": ("TEXT", 0, 0),
        "predecessor_settled_at": ("TEXT", 0, 0),
        "successor_attempt": ("INTEGER", 1, 0),
        "successor_prior_outcome_sha256": ("TEXT", 0, 0),
    }:
        raise sqlite3.DatabaseError("Schema 45 restart rebind step audit shape is invalid")

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
        raise sqlite3.DatabaseError("Schema 42 work item ownership anchors are invalid")
    evidence_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(work_item_selected_evidence)")
    }
    if evidence_foreign_keys != {
        ("work_item_id", "work_items", "id", "NO ACTION", "CASCADE"),
        ("origin_boundary_user_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
    }:
        raise sqlite3.DatabaseError("Schema 42 selected evidence foreign keys are invalid")
    question_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(work_item_compare_document_questions)")
    }
    if question_foreign_keys != {
        ("work_item_id", "work_items", "id", "NO ACTION", "CASCADE"),
        ("work_item_id", "work_item_archive_candidate_sets", "work_item_id", "NO ACTION", "NO ACTION"),
        ("candidate_set_id", "work_item_archive_candidate_sets", "id", "NO ACTION", "NO ACTION"),
        ("prompt_boundary_user_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
        ("prompt_assistant_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
        ("answer_user_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
    }:
        raise sqlite3.DatabaseError("Schema 42 comparison question foreign keys are invalid")
    document_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(work_item_compare_document_evidence)")
    }
    if document_foreign_keys != {
        ("work_item_id", "work_items", "id", "NO ACTION", "CASCADE"),
        ("work_item_id", "work_item_archive_candidate_sets", "work_item_id", "NO ACTION", "NO ACTION"),
        ("candidate_set_id", "work_item_archive_candidate_sets", "id", "NO ACTION", "NO ACTION"),
        ("raw_object_id", "raw_objects", "id", "NO ACTION", "NO ACTION"),
        ("origin_boundary_user_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
    }:
        raise sqlite3.DatabaseError("Schema 42 comparison document foreign keys are invalid")
    outcome_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(work_item_compare_outcomes)")
    }
    if outcome_foreign_keys != {
        ("work_item_id", "work_items", "id", "NO ACTION", "CASCADE"),
        ("answer_boundary_user_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
        ("answer_assistant_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
    }:
        raise sqlite3.DatabaseError("Schema 42 comparison outcome foreign keys are invalid")
    graph_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(work_item_compare_current_file_web_graphs)")
    }
    if graph_foreign_keys != {
        ("user_id", "users", "id", "NO ACTION", "NO ACTION"),
        ("conversation_id", "conversations", "id", "NO ACTION", "NO ACTION"),
        ("anchor_user_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
        ("current_file_raw_object_id", "raw_objects", "id", "NO ACTION", "NO ACTION"),
        ("publication_assistant_message_id", "messages", "id", "NO ACTION", "NO ACTION"),
    }:
        raise sqlite3.DatabaseError("Schema 45 current-file/web WorkGraph anchors are invalid")
    graph_step_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute("PRAGMA foreign_key_list(work_item_compare_current_file_web_steps)")
    }
    if graph_step_foreign_keys != {
        (
            "graph_id",
            "work_item_compare_current_file_web_graphs",
            "id",
            "NO ACTION",
            "CASCADE",
        ),
    }:
        raise sqlite3.DatabaseError("Schema 45 current-file/web WorkGraph step ownership is invalid")
    restart_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute(
            "PRAGMA foreign_key_list(work_item_compare_current_file_web_restart_rebinds)"
        )
    }
    if restart_foreign_keys != {
        (
            "graph_id",
            "work_item_compare_current_file_web_graphs",
            "id",
            "NO ACTION",
            "CASCADE",
        ),
    }:
        raise sqlite3.DatabaseError("Schema 45 restart rebind audit ownership is invalid")
    restart_step_foreign_keys = {
        (str(item[3]), str(item[2]), str(item[4]), str(item[5]), str(item[6]))
        for item in conn.execute(
            "PRAGMA foreign_key_list(work_item_compare_current_file_web_restart_rebind_steps)"
        )
    }
    if restart_step_foreign_keys != {
        (
            "graph_id",
            "work_item_compare_current_file_web_restart_rebinds",
            "graph_id",
            "NO ACTION",
            "CASCADE",
        ),
    }:
        raise sqlite3.DatabaseError("Schema 45 restart rebind step ownership is invalid")
    _validate_current_data(conn)


def _drop_legacy_schema_objects(
    conn: sqlite3.Connection,
    objects: dict[tuple[str, str], str],
) -> None:
    for (kind, name), _sql in objects.items():
        if kind in {"index", "trigger"}:
            conn.execute(f'DROP {kind.upper()} "{name}"')


def _drop_current_work_item_triggers(conn: sqlite3.Connection) -> None:
    """Disable only authenticated current triggers while legacy rows are copied."""

    for (kind, name), _sql in _canonical_work_item_schema_objects().items():
        if kind == "trigger":
            conn.execute(f'DROP TRIGGER "{name}"')


def _copy_work_items(conn: sqlite3.Connection, source_table: str) -> None:
    if source_table not in {
        "work_items_schema38",
        "work_items_schema39",
        "work_items_schema40",
    }:
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


def _upgrade_exact_schema_44_graph_to_45(conn: sqlite3.Connection) -> None:
    """Rebuild only the fixed graph tables and mark unrecoverable root identity."""

    old_objects = _canonical_schema_44_objects()
    schema_42_objects = _canonical_schema_42_objects()
    old_extension_objects = {
        key: value for key, value in old_objects.items() if key not in schema_42_objects
    }
    _drop_legacy_schema_objects(conn, old_extension_objects)
    conn.execute(
        "ALTER TABLE work_item_compare_current_file_web_steps "
        "RENAME TO work_item_compare_current_file_web_steps_schema44"
    )
    conn.execute(
        "ALTER TABLE work_item_compare_current_file_web_graphs "
        "RENAME TO work_item_compare_current_file_web_graphs_schema44"
    )
    _execute_schema(conn, WORK_ITEM_SCHEMA)
    _drop_current_work_item_triggers(conn)
    conn.execute(
        """INSERT INTO work_item_compare_current_file_web_graphs(
               id,user_id,conversation_id,anchor_user_message_id,
               anchor_request_binding_sha256,current_file_raw_object_id,
               state,revision,transition,proposal_sha256,accepted_plan_sha256,
               manifest_sha256,policy_sha256,runtime_profile_sha256,
               adapter_registry_sha256,actor_binding_sha256,
               conversation_binding_sha256,current_file_source_identity_sha256,
               current_file_content_sha256,completion_contract,fallback_owner,
               publication_owner,max_attempts,restart_count,restart_rebound_at,
               restart_file_input_identity_sha256,
               restart_file_idempotency_key_sha256,
               restart_web_input_identity_sha256,
               restart_web_idempotency_key_sha256,
               restart_synthesis_input_identity_sha256,
               restart_synthesis_idempotency_key_sha256,
               created_at,updated_at,expires_at,
               closed_at,outcome_status,outcome_reason,
               publication_assistant_message_id,accepted_graph_outcome_sha256,
               accepted_steps_sha256,terminal_publication_receipt_sha256,
               publication_receipt_sha256
           )
           SELECT id,user_id,conversation_id,anchor_user_message_id,?,
                  current_file_raw_object_id,state,revision,transition,
                  proposal_sha256,accepted_plan_sha256,manifest_sha256,
                  policy_sha256,runtime_profile_sha256,adapter_registry_sha256,
                  actor_binding_sha256,conversation_binding_sha256,
                  current_file_source_identity_sha256,current_file_content_sha256,
                  completion_contract,fallback_owner,publication_owner,max_attempts,
                  0,NULL,NULL,NULL,NULL,NULL,NULL,NULL,
                  created_at,updated_at,expires_at,closed_at,outcome_status,
                  outcome_reason,publication_assistant_message_id,
                  accepted_graph_outcome_sha256,accepted_steps_sha256,
                  terminal_publication_receipt_sha256,publication_receipt_sha256
             FROM work_item_compare_current_file_web_graphs_schema44""",
        (COMPARE_CURRENT_FILE_WEB_UNBOUND_SCHEMA44_REQUEST_SHA256,),
    )
    conn.execute(
        """INSERT INTO work_item_compare_current_file_web_steps(
               graph_id,step_id,kind,capability_id,security_id,adapter_id,
               effect_class,evidence_replayability,depends_on_json,parallel_group,
               input_identity_sha256,idempotency_key_sha256,state,attempt,
               outcome_schema,outcome_sha256,prior_outcome_sha256,
               recovery_review_sha256,recovery_context_sha256,
               evidence_identity_sha256,authority_rechecked,verified,
               started_at,settled_at
           )
           SELECT graph_id,step_id,kind,capability_id,security_id,adapter_id,
                  effect_class,evidence_replayability,depends_on_json,parallel_group,
                  input_identity_sha256,idempotency_key_sha256,state,attempt,
                  outcome_schema,outcome_sha256,prior_outcome_sha256,
                  recovery_review_sha256,recovery_context_sha256,
                  evidence_identity_sha256,authority_rechecked,verified,
                  started_at,settled_at
             FROM work_item_compare_current_file_web_steps_schema44"""
    )
    conn.execute("DROP TABLE work_item_compare_current_file_web_steps_schema44")
    conn.execute("DROP TABLE work_item_compare_current_file_web_graphs_schema44")
    _execute_schema(conn, WORK_ITEM_SCHEMA)


def upgrade_work_item_schema_to_45(
    conn: sqlite3.Connection,
    *,
    required: bool,
) -> None:
    """Authenticate and atomically advance exact released Work Item schemas."""

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

    installed_44 = _schema_objects(conn, current=True)
    canonical_44 = _canonical_schema_44_objects()
    if installed_44 == canonical_44 and related == canonical_44:
        _upgrade_exact_schema_44_graph_to_45(conn)
        validate_work_item_schema(conn)
        return

    installed_42 = _schema_objects_for_tables(conn, _SCHEMA_42_TABLES)
    if installed_42 == _canonical_schema_42_objects() and related == installed_42:
        # Schema 43's Work Item sub-projection is exactly schema 42.  Add only
        # the schema-45 graph tables/triggers; no released Work Item row
        # is rebuilt or rewritten.
        _execute_schema(conn, _WORK_ITEM_SCHEMA_45_EXTENSION)
        validate_work_item_schema(conn)
        return

    installed_40 = _schema_objects_for_tables(conn, _SCHEMA_40_TABLES)
    if installed_40 == _canonical_schema_40_objects() and related == installed_40:
        for table in _SCHEMA_40_TABLES:
            conn.execute(f'ALTER TABLE "{table}" RENAME TO "{table}_schema40"')
        _drop_legacy_schema_objects(conn, _canonical_schema_40_objects())
        _execute_schema(conn, WORK_ITEM_SCHEMA)
        _drop_current_work_item_triggers(conn)
        _copy_work_items(conn, "work_items_schema40")
        conn.execute(
            """INSERT INTO work_item_selected_evidence(
                   work_item_id,corpus,source_ref_json,passage_refs_json,
                   source_snapshot_sha256,coverage_sha256,coverage_grade,
                   origin_boundary_user_message_id
               )
               SELECT work_item_id,corpus,source_ref_json,passage_refs_json,
                      source_snapshot_sha256,coverage_sha256,coverage_grade,
                      origin_boundary_user_message_id
                 FROM work_item_selected_evidence_schema40"""
        )
        conn.execute(
            """INSERT INTO work_item_archive_candidate_sets
               SELECT * FROM work_item_archive_candidate_sets_schema40"""
        )
        conn.execute(
            """INSERT INTO work_item_archive_candidate_set_items
               SELECT * FROM work_item_archive_candidate_set_items_schema40"""
        )
        conn.execute(
            """INSERT INTO work_item_archive_candidate_questions
               SELECT * FROM work_item_archive_candidate_questions_schema40"""
        )
        for table in (
            "work_item_archive_candidate_questions_schema40",
            "work_item_archive_candidate_set_items_schema40",
            "work_item_archive_candidate_sets_schema40",
            "work_item_selected_evidence_schema40",
            "work_items_schema40",
        ):
            conn.execute(f'DROP TABLE "{table}"')
        _execute_schema(conn, WORK_ITEM_SCHEMA)
    else:
        installed_39 = _schema_objects_for_tables(conn, _SCHEMA_39_TABLES)
        if installed_39 == _canonical_schema_39_objects() and related == installed_39:
            conn.execute("ALTER TABLE work_items RENAME TO work_items_schema39")
            conn.execute(
                "ALTER TABLE work_item_selected_evidence RENAME TO work_item_selected_evidence_schema39"
            )
            _drop_legacy_schema_objects(conn, _canonical_schema_39_objects())
            _execute_schema(conn, WORK_ITEM_SCHEMA)
            _drop_current_work_item_triggers(conn)
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
            _execute_schema(conn, WORK_ITEM_SCHEMA)
        else:
            installed_38 = _schema_objects_for_tables(conn, ("work_items",))
            if installed_38 != _canonical_schema_38_objects() or related != installed_38:
                raise sqlite3.DatabaseError("Released Work Item DDL is incomplete or altered")
            conn.execute("ALTER TABLE work_items RENAME TO work_items_schema38")
            _drop_legacy_schema_objects(conn, _canonical_schema_38_objects())
            _execute_schema(conn, WORK_ITEM_SCHEMA)
            _drop_current_work_item_triggers(conn)
            _copy_work_items(conn, "work_items_schema38")
            conn.execute("DROP TABLE work_items_schema38")
            _execute_schema(conn, WORK_ITEM_SCHEMA)
    validate_work_item_schema(conn)


# Historical tests and offline callers import released upgrader names. Keep
# source-compatible aliases while new core wiring names schema 45.
upgrade_work_item_schema_to_44 = upgrade_work_item_schema_to_45
upgrade_work_item_schema_to_42 = upgrade_work_item_schema_to_45


__all__ = [
    "WORK_ITEM_SCHEMA",
    "WORK_ITEM_SCHEMA_VERSION",
    "register_work_item_connection_functions",
    "upgrade_work_item_schema_to_42",
    "upgrade_work_item_schema_to_44",
    "upgrade_work_item_schema_to_45",
    "validate_work_item_schema",
]
