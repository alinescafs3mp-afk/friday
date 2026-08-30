from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import pickle
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from friday.document_catalog.passage_schema import (
    document_passage_set_sha256,
    register_document_passage_connection_functions,
)
from friday.orchestration.archive_recall_outcome import (
    ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE,
    ArchiveRecallLane,
    ArchiveRecallStatus,
    accept_archive_evidence_replay,
)
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval.archive_evidence_replay import (
    ArchiveEvidenceReplayCoverageGrade,
    ArchiveEvidenceReplayError,
    ArchiveEvidenceReplayStatus,
    replay_archive_evidence_in_transaction,
)
from friday.retrieval.archive_evidence_snapshot import (
    archive_selected_evidence_snapshot_sha256,
)
from friday.retrieval.archive_search_authority import (
    ArchiveSearchCoverageGrade,
    ArchiveSearchSelectedEvidence,
)
from friday.retrieval.archive_search_contract import (
    ArchiveContextWindow,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    ConversationScope,
)
from friday.retrieval.archive_search_document_locator import (
    DOCUMENT_STORED_PASSAGE_INDEX_VERSION,
    LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION,
)
from friday.retrieval.archive_search_message_adapter import (
    archive_message_storage_controls,
    project_archive_message_page,
)
from friday.retrieval.catalog_contract import (
    CatalogIndexLane,
    CatalogIndexState,
    CatalogIndexStatus,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    MessageRole,
    PassageRef,
    SearchCorpus,
    SearchExecutionBinding,
    SearchLane,
    TextSpanLocator,
)
from friday.storage import SCHEMA_VERSION
from friday.storage._archive_search_documents import search_archive_document_lane
from friday.storage._archive_search_messages import (
    ArchiveMessageScope,
    select_authorized_archive_message_page_in_transaction,
)
from friday.storage.models import KnowledgeObject, RawObject

TENANT = "replay-tenant"
PRINCIPAL = "replay-principal"
DOCUMENT_BODY = "Raw exact needle replay passage"
KNOWLEDGE_BODY = "Knowledge exact needle replay passage"


def _actor() -> ActorContext:
    return ActorContext(
        user_id=TENANT,
        preset_key="user",
        source="test",
        shared_tenant=True,
        person_id=PRINCIPAL,
    )


def _selected_snapshot(candidate: Any) -> str:
    return archive_selected_evidence_snapshot_sha256(
        candidate.resolved_source,
        tuple(item.passage_ref for item in candidate.passages),
        tuple(item.excerpt for item in candidate.passages),
    )


def _binding(
    request: ArchiveSearchRequest,
    target: tuple[SearchCorpus, SearchLane],
    *,
    snapshot: str,
) -> SearchExecutionBinding:
    return SearchExecutionBinding.create(
        normalized_private_request_json=request.to_identity_json(),
        authority_scope=AuthorityScope.TENANT_PRINCIPAL,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        requested_targets=(target,),
        snapshot_discriminator=snapshot,
        run_discriminator=f"{snapshot}-run",
        privacy_key=b"r" * 32,
    )


def _seed_document_source(  # type: ignore[no-untyped-def]
    storage,
    *,
    passage_ready: bool = False,
    document_body: str = DOCUMENT_BODY,
) -> tuple[str, str, str]:
    storage.ensure_user(TENANT)
    storage.ensure_user(PRINCIPAL)
    conversation = storage.create_conversation(PRINCIPAL, "Replay origin")
    boundary = storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "accepted archive answer",
    )
    raw_id = "raw_0000000000000a11"
    knowledge_id = "ko_0000000000000a11"
    storage.store_raw_object(
        RawObject(
            id=raw_id,
            user_id=TENANT,
            source="upload",
            source_ref="telegram-file:replay",
            raw_content=document_body,
            content_type="file",
            metadata_json={
                "filename": "replay.pdf",
                "media_kind": "document",
                "mime_type": "application/pdf",
                "uploaded_by": PRINCIPAL,
                **(
                    {
                        "extraction_success": True,
                        "text_extraction_success": True,
                    }
                    if passage_ready
                    else {}
                ),
            },
            content_hash=hashlib.sha256(b"replay-source-v1").hexdigest(),
            received_at="2026-08-24T08:00:00+00:00",
            created_at="2026-08-24T08:00:00+00:00",
        )
    )
    storage.store_knowledge_object(
        KnowledgeObject(
            id=knowledge_id,
            user_id=TENANT,
            raw_object_id=raw_id,
            content=KNOWLEDGE_BODY,
            content_type="document",
            title="Replay knowledge",
            summary="Replay summary",
            lifecycle_stage="active",
            version=2,
            created_at="2026-08-24T08:01:00+00:00",
            updated_at="2026-08-24T08:01:00+00:00",
        )
    )
    return raw_id, knowledge_id, boundary["id"]


@pytest.mark.parametrize(
    ("corpus", "search_corpus", "expected_text"),
    (
        (ArchiveSearchCorpus.DOCUMENTS, SearchCorpus.RAW_DOCUMENTS, DOCUMENT_BODY),
        (ArchiveSearchCorpus.KNOWLEDGE, SearchCorpus.KNOWLEDGE, KNOWLEDGE_BODY),
    ),
)
def test_document_and_knowledge_exact_replay_preserves_partial_grade_and_private_carrier(
    storage,
    corpus: ArchiveSearchCorpus,
    search_corpus: SearchCorpus,
    expected_text: str,
) -> None:
    raw_id, knowledge_id, boundary_id = _seed_document_source(storage)
    request = ArchiveSearchRequest.create(query="needle", corpora=(corpus,), limit=1)
    snapshot = f"replay-{corpus.value}"
    binding = _binding(request, (search_corpus, SearchLane.LEXICAL), snapshot=snapshot)
    conn = storage.conn
    authorization = AuthorizationService(storage)
    conn.execute("BEGIN")
    try:
        page = search_archive_document_lane(
            conn,
            tenant_id=TENANT,
            owner_id=PRINCIPAL,
            request=request,
            corpus=corpus,
            lane=SearchLane.LEXICAL,
            execution_binding=binding,
            snapshot_discriminator=snapshot,
            snapshot_current=True,
        )
        candidate = page.candidates[0]
        passage_refs = tuple(item.passage_ref for item in candidate.passages)
        result = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            origin_boundary_user_message_id=boundary_id,
            corpus=corpus,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=passage_refs,
            expected_source_snapshot_sha256=_selected_snapshot(candidate),
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.PARTIAL,
        )
        assert result.status is ArchiveEvidenceReplayStatus.EXACT
        assert result.coverage_grade is ArchiveEvidenceReplayCoverageGrade.PARTIAL
        assert result.excerpts[0].text == expected_text
        payload = json.loads(result.model_visible_bytes)
        assert payload["evidence"] == [{"citation": "[A1.1]", "excerpt": expected_text}]
        assert candidate.resolved_source.source_ref.canonical_object_id.encode() not in (
            result.model_visible_bytes
        )
        selected = ArchiveSearchSelectedEvidence(
            corpus=corpus,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=passage_refs,
            resolved_snapshot_sha256=_selected_snapshot(candidate),
        )
        content, outcome = accept_archive_evidence_replay(
            request="Покажи фрагмент",
            result=result,
            selected_evidence=selected,
            coverage_sha256="c" * 64,
            coverage_grade=ArchiveSearchCoverageGrade.PARTIAL,
        )
        assert content == (
            f"В выбранном источнике:\n\nОхват исходного поиска был частичным.\n\n[A1.1] {expected_text}"
        )
        assert outcome.lane is ArchiveRecallLane.SELECTED_EVIDENCE_REPLAY
        assert outcome.status is ArchiveRecallStatus.PARTIAL
        assert outcome.semantic_verified is True
        assert outcome.selected_evidence == selected
        with pytest.raises(TypeError):
            copy.copy(result)
        with pytest.raises(TypeError):
            pickle.dumps(result)
        with pytest.raises(TypeError):
            dataclasses.asdict(result)  # type: ignore[call-overload]
        if corpus is ArchiveSearchCorpus.DOCUMENTS:
            conn.execute(
                "UPDATE raw_objects SET raw_content=? WHERE id=?",
                (f"tampered {expected_text}", raw_id),
            )
        else:
            conn.execute(
                "UPDATE knowledge_objects SET content=? WHERE id=?",
                (f"tampered {expected_text}", knowledge_id),
            )
        tampered = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            origin_boundary_user_message_id=boundary_id,
            corpus=corpus,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=passage_refs,
            expected_source_snapshot_sha256=_selected_snapshot(candidate),
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.PARTIAL,
        )
        assert tampered.status is ArchiveEvidenceReplayStatus.DRIFTED
        assert tampered.excerpts == ()
    finally:
        conn.rollback()


@pytest.mark.parametrize("stored_child_drift", ("missing", "alternate_topology"))
def test_document_v2_replay_requires_exact_stored_child_while_legacy_survives_drift(
    storage,
    stored_child_drift: str,
) -> None:
    body = (
        DOCUMENT_BODY
        if stored_child_drift == "missing"
        else DOCUMENT_BODY + " " + ("alpha beta gamma. " * 100)
    )
    raw_id, _knowledge_id, boundary_id = _seed_document_source(
        storage,
        passage_ready=True,
        document_body=body,
    )
    backfill = storage.backfill_document_catalog(
        TENANT,
        after_raw_object_id=None,
        limit=1,
        include_document_passages=True,
    )
    assert backfill["passage_changed"] == 1
    corpus = ArchiveSearchCorpus.DOCUMENTS
    request = ArchiveSearchRequest.create(query="needle", corpora=(corpus,), limit=1)
    binding = _binding(
        request,
        (SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL),
        snapshot="replay-stored-passage",
    )
    conn = storage.conn
    authorization = AuthorizationService(storage)
    conn.execute("BEGIN")
    try:
        page = search_archive_document_lane(
            conn,
            tenant_id=TENANT,
            owner_id=PRINCIPAL,
            request=request,
            corpus=corpus,
            lane=SearchLane.LEXICAL,
            execution_binding=binding,
            snapshot_discriminator="replay-stored-passage",
            snapshot_current=True,
        )
        candidate = page.candidates[0]
        stored_ref = candidate.passages[0].passage_ref
        excerpt = candidate.passages[0].excerpt
        assert stored_ref.passage_index_version == DOCUMENT_STORED_PASSAGE_INDEX_VERSION
        stored_result = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            origin_boundary_user_message_id=boundary_id,
            corpus=corpus,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=(stored_ref,),
            expected_source_snapshot_sha256=_selected_snapshot(candidate),
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.COMPLETE,
        )
        assert stored_result.status is ArchiveEvidenceReplayStatus.EXACT

        legacy_ref = dataclasses.replace(
            stored_ref,
            locator=TextSpanLocator(
                chunk_index=0,
                start_char=stored_ref.locator.start_char,  # type: ignore[union-attr]
                end_char=stored_ref.locator.end_char,  # type: ignore[union-attr]
            ),
            passage_index_version=LEGACY_DOCUMENT_PASSAGE_INDEX_VERSION,
        )
        legacy_snapshot = archive_selected_evidence_snapshot_sha256(
            candidate.resolved_source,
            (legacy_ref,),
            (excerpt,),
        )
        if stored_child_drift == "missing":
            conn.execute("DELETE FROM document_passages WHERE raw_object_id=?", (raw_id,))
        else:
            original_rows = tuple(
                (int(row[0]), int(row[1]), int(row[2]), str(row[3]))
                for row in conn.execute(
                    """SELECT chunk_index,start_char,end_char,content_sha256
                         FROM document_passages
                        WHERE raw_object_id=? ORDER BY chunk_index""",
                    (raw_id,),
                )
            )
            assert len(original_rows) >= 2
            first_index, first_start, first_end, _first_digest = original_rows[0]
            forged_end = first_end - 1
            assert int(original_rows[1][1]) <= forged_end < int(original_rows[1][2])
            forged_first = (
                first_index,
                first_start,
                forged_end,
                hashlib.sha256(body[first_start:forged_end].encode()).hexdigest(),
            )
            forged_rows = (forged_first, *original_rows[1:])
            conn.create_function(
                "friday_document_passage_span_valid",
                6,
                lambda *_args: 1,
                deterministic=True,
            )
            conn.create_function(
                "friday_document_passage_projection_valid",
                14,
                lambda *_args: 1,
                deterministic=True,
            )
            try:
                conn.execute(
                    """UPDATE document_passages
                          SET end_char=?,content_sha256=?
                        WHERE raw_object_id=? AND chunk_index=?""",
                    (forged_end, forged_first[3], raw_id, first_index),
                )
                conn.execute(
                    """UPDATE document_passage_projections
                          SET passage_set_sha256=? WHERE raw_object_id=?""",
                    (document_passage_set_sha256(forged_rows), raw_id),
                )
            finally:
                register_document_passage_connection_functions(conn)
            persisted_rows = tuple(
                (int(row[0]), int(row[1]), int(row[2]), str(row[3]))
                for row in conn.execute(
                    """SELECT chunk_index,start_char,end_char,content_sha256
                         FROM document_passages
                        WHERE raw_object_id=? ORDER BY chunk_index""",
                    (raw_id,),
                )
            )
            assert persisted_rows == forged_rows
            assert all(
                hashlib.sha256(body[start:end].encode()).hexdigest() == digest
                for _index, start, end, digest in persisted_rows
            )
            assert (
                document_passage_set_sha256(persisted_rows)
                == conn.execute(
                    """SELECT passage_set_sha256 FROM document_passage_projections
                    WHERE raw_object_id=?""",
                    (raw_id,),
                ).fetchone()[0]
            )

        drifted = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            origin_boundary_user_message_id=boundary_id,
            corpus=corpus,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=(stored_ref,),
            expected_source_snapshot_sha256=_selected_snapshot(candidate),
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.COMPLETE,
        )
        assert drifted.status is ArchiveEvidenceReplayStatus.DRIFTED
        legacy = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            origin_boundary_user_message_id=boundary_id,
            corpus=corpus,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=(legacy_ref,),
            expected_source_snapshot_sha256=legacy_snapshot,
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.COMPLETE,
        )
        assert legacy.status is ArchiveEvidenceReplayStatus.EXACT
        assert legacy.excerpts[0].text == excerpt
    finally:
        conn.rollback()


def test_document_replay_closes_on_snapshot_or_locator_drift_and_caller_denial(storage) -> None:
    raw_id, _knowledge_id, boundary_id = _seed_document_source(storage)
    corpus = ArchiveSearchCorpus.DOCUMENTS
    request = ArchiveSearchRequest.create(query="needle", corpora=(corpus,), limit=1)
    binding = _binding(
        request,
        (SearchCorpus.RAW_DOCUMENTS, SearchLane.LEXICAL),
        snapshot="replay-drift",
    )
    conn = storage.conn
    authorization = AuthorizationService(storage)
    conn.execute("BEGIN")
    try:
        page = search_archive_document_lane(
            conn,
            tenant_id=TENANT,
            owner_id=PRINCIPAL,
            request=request,
            corpus=corpus,
            lane=SearchLane.LEXICAL,
            execution_binding=binding,
            snapshot_discriminator="replay-drift",
            snapshot_current=True,
        )
        candidate = page.candidates[0]
        original = candidate.passages[0].passage_ref
        digest = _selected_snapshot(candidate)
        conn.execute(
            """INSERT INTO user_permission_overrides(
                   user_id, security_id, effect, updated_at
               ) VALUES(?, 'knowledge.read', 'deny', ?)""",
            (PRINCIPAL, "2026-08-24T08:02:00+00:00"),
        )
        denied = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            origin_boundary_user_message_id=boundary_id,
            corpus=corpus,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=(original,),
            expected_source_snapshot_sha256=digest,
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.COMPLETE,
        )
        assert denied.status is ArchiveEvidenceReplayStatus.DENIED
        assert denied.excerpts == ()
        selected = ArchiveSearchSelectedEvidence(
            corpus=corpus,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=(original,),
            resolved_snapshot_sha256=digest,
        )
        denied_content, denied_outcome = accept_archive_evidence_replay(
            request="Что в нём сказано?",
            result=denied,
            selected_evidence=selected,
            coverage_sha256="d" * 64,
            coverage_grade=ArchiveSearchCoverageGrade.COMPLETE,
        )
        assert denied_content == ARCHIVE_EVIDENCE_REPLAY_UNAVAILABLE
        assert denied_outcome.status is ArchiveRecallStatus.DENIED
        assert denied_outcome.semantic_verified is False
        assert denied_outcome.selected_evidence is None
        with pytest.raises(ArchiveEvidenceReplayError):
            _ = denied.model_visible_bytes
        conn.execute(
            "DELETE FROM user_permission_overrides WHERE user_id=? AND security_id='knowledge.read'",
            (PRINCIPAL,),
        )

        locator = original.locator
        assert type(locator) is TextSpanLocator
        oversized = PassageRef(
            original.source_ref,
            original.source_revision,
            TextSpanLocator(locator.chunk_index, locator.start_char, locator.end_char + 721),
            original.passage_index_version,
            original.embedding,
        )
        locator_drift = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            origin_boundary_user_message_id=boundary_id,
            corpus=corpus,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=(oversized,),
            expected_source_snapshot_sha256=digest,
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.COMPLETE,
        )
        assert locator_drift.status is ArchiveEvidenceReplayStatus.DRIFTED
        assert locator_drift.excerpts == ()

        conn.execute(
            "UPDATE raw_objects SET content_hash=? WHERE id=?",
            (hashlib.sha256(b"replay-source-v2").hexdigest(), raw_id),
        )
        revision_drift = replay_archive_evidence_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            origin_boundary_user_message_id=boundary_id,
            corpus=corpus,
            source_ref=candidate.resolved_source.source_ref,
            passage_refs=(original,),
            expected_source_snapshot_sha256=digest,
            expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.COMPLETE,
        )
        assert revision_drift.status is ArchiveEvidenceReplayStatus.DRIFTED
        assert revision_drift.excerpts == ()
    finally:
        conn.rollback()


CURRENT = "conv_00000000000000a1"
BOUNDARY = "msg_0000000000000064"


def _message_id(number: int) -> str:
    return f"msg_{number:016x}"


class _AuthorizationStorage:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_permission_overrides(self, user_id: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT security_id, effect FROM user_permission_overrides WHERE user_id=?",
            (user_id,),
        ).fetchall()
        return {str(row["security_id"]): str(row["effect"]) for row in rows}


def _message_authorization(conn: sqlite3.Connection) -> AuthorizationService:
    return AuthorizationService(cast(Any, _AuthorizationStorage(conn)))


@contextmanager
def _message_database() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            """CREATE TABLE conversations (
                   id TEXT PRIMARY KEY,
                   user_id TEXT NOT NULL,
                   title TEXT NOT NULL DEFAULT '',
                   is_archived INTEGER NOT NULL DEFAULT 0
               );
               CREATE TABLE messages (
                   id TEXT PRIMARY KEY,
                   conversation_id TEXT NOT NULL,
                   user_id TEXT NOT NULL,
                   role TEXT NOT NULL,
                   content TEXT NOT NULL,
                   created_at TEXT NOT NULL
               );
               CREATE TABLE users (
                   id TEXT PRIMARY KEY,
                   status TEXT NOT NULL,
                   preset_key TEXT NOT NULL
               );
               CREATE TABLE user_permission_overrides (
                   user_id TEXT NOT NULL,
                   security_id TEXT NOT NULL,
                   effect TEXT NOT NULL,
                   updated_at TEXT NOT NULL,
                   PRIMARY KEY(user_id, security_id)
               );
               CREATE TABLE schema_meta (
                   key TEXT PRIMARY KEY,
                   value TEXT NOT NULL
               );
               CREATE INDEX idx_messages_conversation
                   ON messages(user_id,conversation_id,created_at);
               CREATE VIRTUAL TABLE messages_fts USING fts5(
                   content, content=messages, content_rowid=rowid
               );
               CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
                   INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
               END;
               CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
                   INSERT INTO messages_fts(messages_fts, rowid, content)
                   VALUES ('delete', old.rowid, old.content);
               END;
               CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
                   INSERT INTO messages_fts(messages_fts, rowid, content)
                   VALUES ('delete', old.rowid, old.content);
                   INSERT INTO messages_fts(rowid, content) VALUES (new.rowid, new.content);
               END;"""
        )
        conn.executemany(
            "INSERT INTO users(id, status, preset_key) VALUES(?, 'active', 'user')",
            ((TENANT,), (PRINCIPAL,), ("foreign",)),
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('fts_build', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT INTO conversations(id, user_id, title, is_archived) VALUES(?, ?, ?, 0)",
            (CURRENT, PRINCIPAL, "Replay conversation"),
        )
        for number, role, content, created_at in (
            (10, "user", "context before", "2026-08-24T09:00:00+00:00"),
            (20, "assistant", "needle exact answer", "2026-08-24T09:01:00+00:00"),
            (30, "user", "context after", "2026-08-24T09:02:00+00:00"),
            (100, "user", "accepted boundary", "2026-08-24T09:03:00+00:00"),
        ):
            conn.execute(
                """INSERT INTO messages(
                       rowid, id, conversation_id, user_id, role, content, created_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (number, _message_id(number), CURRENT, PRINCIPAL, role, content, created_at),
            )
        conn.commit()
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _message_candidate(conn: sqlite3.Connection):  # type: ignore[no-untyped-def]
    request = ArchiveSearchRequest.create(
        query="needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        conversation_scope=ConversationScope.CURRENT,
        roles=(MessageRole.USER, MessageRole.ASSISTANT),
        limit=1,
        context=ArchiveContextWindow(1, 1),
    )
    controls = archive_message_storage_controls(request)
    page = select_authorized_archive_message_page_in_transaction(
        conn,
        principal_id=PRINCIPAL,
        query=request.query,
        scope=ArchiveMessageScope.CURRENT,
        conversation_id=CURRENT,
        boundary_user_message_id=BOUNDARY,
        roles=controls["roles"],  # type: ignore[arg-type]
        lifecycle_states=controls["lifecycle_states"],  # type: ignore[arg-type]
        since=controls["since"],  # type: ignore[arg-type]
        until=controls["until"],  # type: ignore[arg-type]
        limit=1,
        context_before=1,
        context_after=1,
    )
    assert page is not None
    binding = _binding(
        request,
        (SearchCorpus.CONVERSATION, SearchLane.MESSAGE_HISTORY),
        snapshot="replay-message",
    )
    projection = project_archive_message_page(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        request=request,
        page=page,
        index_state=CatalogIndexState(
            CatalogIndexLane.LEXICAL,
            CatalogIndexStatus.CURRENT,
            None,
        ),
        execution_binding=binding,
        snapshot_discriminator="replay-message",
        current_conversation_id=CURRENT,
        boundary_user_message_id=BOUNDARY,
    )
    return projection.candidates[0]


def _replay_message(conn: sqlite3.Connection, candidate):  # type: ignore[no-untyped-def]
    return replay_archive_evidence_in_transaction(
        conn,
        authorization=_message_authorization(conn),
        actor=_actor(),
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        origin_boundary_user_message_id=BOUNDARY,
        corpus=ArchiveSearchCorpus.MESSAGES,
        source_ref=candidate.resolved_source.source_ref,
        passage_refs=tuple(item.passage_ref for item in candidate.passages),
        expected_source_snapshot_sha256=_selected_snapshot(candidate),
        expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.COMPLETE,
    )


def test_message_replay_uses_original_pre_boundary_ledger_without_fts() -> None:
    with _message_database() as conn:
        candidate = _message_candidate(conn)
        conn.execute(
            """INSERT INTO messages(
                   rowid, id, conversation_id, user_id, role, content, created_at
               ) VALUES(110, ?, ?, ?, 'assistant', 'later secret',
                        '2026-08-24T09:04:00+00:00')""",
            (_message_id(110), CURRENT, PRINCIPAL),
        )
        conn.executescript(
            """DROP TRIGGER messages_ai;
               DROP TRIGGER messages_ad;
               DROP TRIGGER messages_au;
               DROP TABLE messages_fts;"""
        )
        conn.execute("BEGIN")
        result = _replay_message(conn, candidate)
        assert result.status is ArchiveEvidenceReplayStatus.EXACT
        assert result.excerpts[0].text == (
            "Пользователь: context before | Friday: needle exact answer | Пользователь: context after"
        )
        assert b"later secret" not in result.model_visible_bytes

        conn.execute(
            "UPDATE messages SET content='edited before boundary' WHERE id=?",
            (_message_id(10),),
        )
        drifted = _replay_message(conn, candidate)
        assert drifted.status is ArchiveEvidenceReplayStatus.DRIFTED
        assert drifted.excerpts == ()


def test_message_replay_fails_closed_for_malformed_storage_and_contract() -> None:
    with _message_database() as conn:
        candidate = _message_candidate(conn)
        passages = tuple(item.passage_ref for item in candidate.passages)
        with pytest.raises(ArchiveEvidenceReplayError):
            replay_archive_evidence_in_transaction(
                conn,
                authorization=_message_authorization(conn),
                actor=_actor(),
                tenant_id=TENANT,
                principal_id=PRINCIPAL,
                origin_boundary_user_message_id=BOUNDARY,
                corpus=ArchiveSearchCorpus.MESSAGES,
                source_ref=candidate.resolved_source.source_ref,
                passage_refs=(passages[0], passages[0]),
                expected_source_snapshot_sha256=_selected_snapshot(candidate),
                expected_coverage_grade=ArchiveEvidenceReplayCoverageGrade.COMPLETE,
            )
        conn.execute(
            "UPDATE messages SET created_at='not-a-time' WHERE id=?",
            (_message_id(10),),
        )
        unavailable = _replay_message(conn, candidate)
        assert unavailable.status is ArchiveEvidenceReplayStatus.UNAVAILABLE
        assert unavailable.excerpts == ()
