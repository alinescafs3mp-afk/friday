from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from typing import Any

import pytest

import friday.retrieval.archive_search_service as service_module
from friday.retrieval.archive_search_authority import (
    ArchiveSearchPublicationDenied,
    attest_archive_search_before_publication,
    canonical_archive_search_targets,
    create_archive_model_batch_ledger,
)
from friday.retrieval.archive_search_contract import (
    ArchiveEvidenceAuthority,
    ArchiveMatchChannel,
    ArchiveMatchRank,
    ArchiveReviewState,
    ArchiveSearchCandidate,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
)
from friday.retrieval.archive_search_federation import federate_archive_search
from friday.retrieval.archive_search_service import (
    ArchiveSearchServiceError,
    prepare_archive_search_in_transaction,
    reauthorize_archive_search_candidate,
    reauthorize_archive_search_coverage,
    refresh_archive_search_reauthorization_in_transaction,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    CoverageState,
    LifecycleRef,
    LifecycleState,
    RepresentationKind,
    ResolvedSource,
    RevalidationTarget,
    RevisionKind,
    SearchCorpus,
    SearchCoverage,
    SearchLane,
    SourceKind,
    SourceRef,
    SourceRepresentation,
    SourceRevision,
)
from friday.storage import SCHEMA_VERSION
from friday.storage.models import InboxItem, InboxStatus, RawObject

TENANT = "archive-service-tenant"
PRINCIPAL = "archive-service-principal"
SNAPSHOT = "archive-service-snapshot"
_TURNS = itertools.count(1)


class _AlienReviewState(StrEnum):
    CONFIRMED = "confirmed"


def _ledger(*, principal: str = PRINCIPAL):
    return create_archive_model_batch_ledger(
        tenant_id=TENANT,
        principal_id=principal,
        turn_discriminator=f"archive-service-turn-{next(_TURNS)}",
    )


def _payload(prepared: Any) -> dict[str, Any]:
    return json.loads(prepared.authorized_batch.model_visible_canonical_bytes)


def _coverage(payload: dict[str, Any], lane: SearchLane) -> dict[str, Any]:
    return next(item for item in payload["coverage"] if item["lane"] == lane.value)


def test_facade_requires_one_caller_owned_transaction_and_closes_unsupported_plan() -> None:
    conn = sqlite3.connect(":memory:")
    request = ArchiveSearchRequest.create(
        query="private artifact",
        corpora=(ArchiveSearchCorpus.GENERATED,),
        limit=2,
    )
    ledger = _ledger()

    with pytest.raises(ArchiveSearchServiceError):
        prepare_archive_search_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="outside-transaction",
            turn_ledger=ledger,
        )

    conn.execute("BEGIN")
    prepared = prepare_archive_search_in_transaction(
        conn,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        request=request,
        snapshot_discriminator=SNAPSHOT,
        run_discriminator="inside-transaction",
        turn_ledger=ledger,
    )
    payload = _payload(prepared)
    assert conn.in_transaction
    assert payload["candidates"] == []
    assert payload["absence"] == "not_established"
    assert len(payload["coverage"]) == 5
    assert all(
        item["states"] == [CoverageState.UNAVAILABLE.value]
        and item["authority_rechecked"] is True
        and item["snapshot_current"] is True
        for item in payload["coverage"]
    )


@pytest.mark.parametrize(
    ("seed_users", "expected_states", "authority_rechecked", "snapshot_current"),
    (
        (
            True,
            [CoverageState.PARTIAL.value, CoverageState.PERMISSION_FILTERED.value],
            True,
            True,
        ),
        (
            False,
            [CoverageState.PARTIAL.value, CoverageState.UNAVAILABLE.value],
            False,
            False,
        ),
    ),
)
def test_document_denial_and_storage_failure_are_not_false_absence(
    seed_users: bool,
    expected_states: list[str],
    authority_rechecked: bool,
    snapshot_current: bool,
) -> None:
    conn = sqlite3.connect(":memory:")
    if seed_users:
        conn.execute("CREATE TABLE users(id TEXT PRIMARY KEY, status TEXT NOT NULL)")
        conn.commit()
    conn.execute("BEGIN")
    request = ArchiveSearchRequest.create(
        query="private document",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
    )
    prepared = prepare_archive_search_in_transaction(
        conn,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        request=request,
        snapshot_discriminator=SNAPSHOT,
        run_discriminator=f"document-failure-{seed_users}",
        turn_ledger=_ledger(),
    )
    payload = _payload(prepared)
    for lane in (SearchLane.CATALOG, SearchLane.LEXICAL):
        item = _coverage(payload, lane)
        assert item["states"] == expected_states
        assert item["authority_rechecked"] is authority_rechecked
        assert item["snapshot_current"] is snapshot_current
    assert payload["absence"] == "not_established"


def test_document_lanes_are_federated_from_the_authoritative_store(storage: Any) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(PRINCIPAL)
    body = "Needle private archive body"
    storage.store_raw_object(
        RawObject(
            id="raw_00000000000000a1",
            user_id=TENANT,
            source="upload",
            source_ref="telegram-file:archive-service",
            raw_content=body,
            content_type="file",
            metadata_json={
                "filename": "Friday Architecture.md",
                "media_kind": "document",
                "mime_type": "text/markdown",
                "uploaded_by": PRINCIPAL,
            },
            content_hash=hashlib.sha256(body.encode()).hexdigest(),
            received_at="2026-08-23T10:00:00+00:00",
            created_at="2026-08-23T10:00:00+00:00",
        )
    )
    storage.store_inbox_item(
        InboxItem(
            id="inbox_00000000000000a1",
            user_id=TENANT,
            raw_object_id="raw_00000000000000a1",
            knowledge_object_id=None,
            status=InboxStatus.CLASSIFIED,
            created_at="2026-08-23T10:01:00+00:00",
            reviewed_at="2026-08-23T10:02:00+00:00",
            reviewed_by=PRINCIPAL,
        )
    )
    request = ArchiveSearchRequest.create(
        query="Needle",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        filename_hints=("Friday Architecture.md",),
        limit=5,
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="document-live",
            turn_ledger=_ledger(),
        )
        assert conn.in_transaction
    payload = _payload(prepared)
    assert len(payload["candidates"]) == 1
    assert _coverage(payload, SearchLane.CATALOG)["states"] == ["complete"]
    assert _coverage(payload, SearchLane.LEXICAL)["states"] == ["complete"]
    assert _coverage(payload, SearchLane.DENSE)["states"] == ["unavailable"]


@contextmanager
def _message_database() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
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
               CREATE TABLE users (id TEXT PRIMARY KEY, status TEXT NOT NULL);
               CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
               CREATE VIRTUAL TABLE messages_fts USING fts5(
                   content,
                   content=messages,
                   content_rowid=rowid,
                   tokenize='unicode61 remove_diacritics 2'
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
        conn.execute(
            "INSERT INTO users(id, status) VALUES(?, 'active')",
            (PRINCIPAL,),
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('fts_build', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT INTO conversations(id, user_id, title, is_archived) VALUES(?, ?, ?, 0)",
            ("conv_0000000000000001", PRINCIPAL, "Friday project"),
        )
        conn.executemany(
            """INSERT INTO messages(id, conversation_id, user_id, role, content, created_at)
               VALUES(?, 'conv_0000000000000001', ?, ?, ?, ?)""",
            (
                (
                    "msg_0000000000000001",
                    PRINCIPAL,
                    "user",
                    "Needle private conversation",
                    "2026-08-23T08:00:00+00:00",
                ),
                (
                    "msg_0000000000000002",
                    PRINCIPAL,
                    "assistant",
                    "Adjacent context",
                    "2026-08-23T08:01:00+00:00",
                ),
            ),
        )
        conn.commit()
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def test_message_history_uses_authorized_context_and_leaves_other_lanes_unavailable() -> None:
    request = ArchiveSearchRequest.create(
        query="Needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        limit=5,
    )
    with _message_database() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="messages-live",
            turn_ledger=_ledger(),
        )
    payload = _payload(prepared)
    assert len(payload["candidates"]) == 1
    assert _coverage(payload, SearchLane.MESSAGE_HISTORY)["states"] == ["complete"]
    assert _coverage(payload, SearchLane.LEXICAL)["states"] == ["unavailable"]
    assert _coverage(payload, SearchLane.DENSE)["states"] == ["unavailable"]


def test_obsidian_lanes_verify_exact_bytes_and_merge_one_stable_source(storage: Any) -> None:
    storage.ensure_user(TENANT)
    storage.ensure_user(PRINCIPAL)
    storage.create_obsidian_bundle(
        PRINCIPAL,
        config_root="/private/config/archive-service",
        database_root="/private/data/archive-service",
        api_endpoint="unix:///private/run/archive-service.sock",
        api_key_ref="secret:obsidian:archive-service",
        server_path="/private/vaults/archive-service",
        folder_id="friday-archive-service",
        setup_token_hash=hashlib.sha256(b"archive-service-token").hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )
    vault = storage.update_obsidian_vault(PRINCIPAL, state="ready")
    body = "Project Phoenix is the private release plan."
    revision = hashlib.sha256(body.encode()).hexdigest()
    binding = storage.upsert_obsidian_note_binding(
        PRINCIPAL,
        vault_id=str(vault["id"]),
        integration_id="archive-service-note",
        current_path="Projects/Phoenix.md",
        current_revision=revision,
        origin="user",
    )
    storage.upsert_obsidian_note_index(
        PRINCIPAL,
        binding_id=str(binding["id"]),
        revision=revision,
        metadata={"aliases": ["Project Phoenix"]},
        metadata_coverage="complete",
        body_text=body,
        body_coverage="complete",
        source_size_bytes=len(body.encode()),
        title="Phoenix",
    )
    reads: list[tuple[str, str, str]] = []

    def exact_reader(vault_id: str, path: str, expected_sha256: str, /) -> bytes:
        reads.append((vault_id, path, expected_sha256))
        assert expected_sha256 == revision
        return body.encode()

    request = ArchiveSearchRequest.create(
        query="Phoenix",
        corpora=(ArchiveSearchCorpus.OBSIDIAN,),
        title_hints=("Phoenix",),
        filename_hints=("Phoenix.md",),
        limit=5,
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="obsidian-live",
            turn_ledger=_ledger(),
            exact_file_reader=exact_reader,
        )
    payload = _payload(prepared)
    assert len(payload["candidates"]) == 1
    assert len(reads) == 2
    assert {item[1] for item in reads} == {"Projects/Phoenix.md"}
    assert _coverage(payload, SearchLane.LEXICAL)["states"] == ["complete"]
    assert _coverage(payload, SearchLane.DENSE)["states"] == ["unavailable"]


def _synthetic_candidate(index: int, rank: int) -> ArchiveSearchCandidate:
    raw_id = f"raw_{index:016x}"
    source_ref = SourceRef(
        SourceKind.DOCUMENT,
        AuthorityScope.TENANT_PRINCIPAL,
        TENANT,
        PRINCIPAL,
        CanonicalObjectKind.RAW_OBJECT,
        raw_id,
    )
    representation = SourceRepresentation(RepresentationKind.RAW_OBJECT, raw_id)
    knowledge = SourceRepresentation(
        RepresentationKind.KNOWLEDGE_OBJECT,
        f"ko_{index:016x}",
    )
    resolved = ResolvedSource.create(
        source_ref=source_ref,
        representations=(representation, knowledge),
        lifecycle=(
            LifecycleRef(representation, LifecycleState.ACTIVE),
            LifecycleRef(knowledge, LifecycleState.ACTIVE),
        ),
        revisions=(
            SourceRevision(
                representation,
                RevisionKind.RAW_CONTENT_SHA256,
                f"{index:x}" * 64,
            ),
            SourceRevision(knowledge, RevisionKind.KNOWLEDGE_VERSION, "1"),
        ),
        revalidation_targets=(
            RevalidationTarget(representation, AuthorityScope.TENANT_PRINCIPAL),
            RevalidationTarget(knowledge, AuthorityScope.TENANT_PRINCIPAL),
        ),
    )
    return ArchiveSearchCandidate.create(
        corpus=ArchiveSearchCorpus.DOCUMENTS,
        resolved_source=resolved,
        title=f"Document {index}",
        review_state=ArchiveReviewState.CONFIRMED,
        evidence_authority=ArchiveEvidenceAuthority.NAVIGATION_ONLY,
        lifecycle_state=LifecycleState.ACTIVE,
        matches=(ArchiveMatchRank(ArchiveMatchChannel.CATALOG, rank),),
    )


def _synthetic_federation(_conn: sqlite3.Connection, **values: Any):
    recipe = values["recipe"]
    run = values["run"]
    binding = run.execution_binding
    targets = canonical_archive_search_targets(recipe.request)
    candidates = tuple(_synthetic_candidate(index, index) for index in range(1, 4))
    by_target: dict[
        tuple[SearchCorpus, SearchLane], tuple[ArchiveSearchCandidate, ...]
    ] = {target: () for target in targets}
    by_target[(SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG)] = candidates
    coverage: list[SearchCoverage] = []
    for target in targets:
        if target == (SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG):
            coverage.append(
                SearchCoverage.create(
                    corpus=target[0],
                    lane=target[1],
                    execution_binding=binding,
                    states=(CoverageState.COMPLETE,),
                    eligible_authorized=3,
                    examined=3,
                    matched_at_least=3,
                    returned=3,
                    authority_rechecked=True,
                    snapshot_current=True,
                )
            )
        else:
            coverage.append(
                SearchCoverage.create(
                    corpus=target[0],
                    lane=target[1],
                    execution_binding=binding,
                    states=(CoverageState.UNAVAILABLE,),
                    eligible_authorized=None,
                    examined=0,
                    matched_at_least=0,
                    returned=0,
                    authority_rechecked=True,
                    snapshot_current=True,
                )
            )
    return federate_archive_search(
        request=recipe.request,
        execution_binding=binding,
        coverage=tuple(coverage),
        candidates_by_target=by_target,
    )


def test_deterministic_continuation_reauthorizes_against_fresh_full_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service_module,
        "_collect_federated_in_transaction",
        _synthetic_federation,
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("BEGIN")
    ledger = _ledger()
    initial_request = ArchiveSearchRequest.create(
        query="private documents",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=1,
    )
    first = prepare_archive_search_in_transaction(
        conn,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        request=initial_request,
        snapshot_discriminator=SNAPSHOT,
        run_discriminator="continuation-first",
        turn_ledger=ledger,
    )
    first_payload = _payload(first)
    assert len(first_payload["candidates"]) == 1
    assert isinstance(first_payload["continuation"], str)

    resumed_request = ArchiveSearchRequest.create(
        query="private documents",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=1,
        continuation=first_payload["continuation"],
    )
    second = prepare_archive_search_in_transaction(
        conn,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        request=resumed_request,
        snapshot_discriminator=SNAPSHOT,
        run_discriminator="continuation-second",
        turn_ledger=ledger,
    )
    second_payload = _payload(second)
    assert len(second_payload["candidates"]) == 1
    assert second_payload["continuation"] is not None
    assert first_payload["candidates"][0] != second_payload["candidates"][0]
    for prepared in (first, second):
        ledger.admit_model_tool_bytes(
            prepared.run_binding,
            prepared.authorized_batch,
            prepared.authorized_batch.model_visible_canonical_bytes,
        )
    ledger.freeze_for_publication()
    context = refresh_archive_search_reauthorization_in_transaction(
        conn,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        prepared_searches=(first, second),
    )
    attestation = attest_archive_search_before_publication(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=ledger,
        answer="Two exact archive pages",
        candidate_reauthorizer=reauthorize_archive_search_candidate,
        coverage_reauthorizer=reauthorize_archive_search_coverage,
        authority_context=context,
    )
    assert attestation.attests_answer("Two exact archive pages")


def test_same_json_foreign_nested_type_invalidates_candidate_and_prepared_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _synthetic_candidate(1, 1)
    alien = _synthetic_candidate(1, 1)
    object.__setattr__(alien, "review_state", _AlienReviewState.CONFIRMED)
    assert alien.to_private_json() == canonical.to_private_json()
    assert not service_module._same_candidate(alien, canonical)

    monkeypatch.setattr(
        service_module,
        "_collect_federated_in_transaction",
        _synthetic_federation,
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("BEGIN")
    request = ArchiveSearchRequest.create(
        query="private documents",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=1,
    )
    prepared = prepare_archive_search_in_transaction(
        conn,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        request=request,
        snapshot_discriminator=SNAPSHOT,
        run_discriminator="same-json-spoof",
        turn_ledger=_ledger(),
    )
    run = prepared.run_binding
    batch = prepared.authorized_batch
    carried = batch._page.results[0].candidate
    object.__setattr__(carried, "review_state", _AlienReviewState.CONFIRMED)
    with pytest.raises(ArchiveSearchServiceError):
        refresh_archive_search_reauthorization_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )
    assert run.execution_binding.is_live_private_request_binding


def test_publication_refresh_attests_exact_batch_and_rejects_wrong_actor() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("BEGIN")
    request = ArchiveSearchRequest.create(
        query="private artifact",
        corpora=(ArchiveSearchCorpus.GENERATED,),
    )
    ledger = _ledger()
    prepared = prepare_archive_search_in_transaction(
        conn,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        request=request,
        snapshot_discriminator=SNAPSHOT,
        run_discriminator="publication",
        turn_ledger=ledger,
    )
    body = prepared.authorized_batch.model_visible_canonical_bytes
    ledger.admit_model_tool_bytes(
        prepared.run_binding,
        prepared.authorized_batch,
        body,
    )
    ledger.freeze_for_publication()
    with pytest.raises(ArchiveSearchServiceError):
        refresh_archive_search_reauthorization_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id="wrong-principal",
            prepared_searches=(prepared,),
        )
    context = refresh_archive_search_reauthorization_in_transaction(
        conn,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        prepared_searches=(prepared,),
    )
    attestation = attest_archive_search_before_publication(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=ledger,
        answer="Bounded answer",
        candidate_reauthorizer=reauthorize_archive_search_candidate,
        coverage_reauthorizer=reauthorize_archive_search_coverage,
        authority_context=context,
    )
    assert attestation.attests_answer("Bounded answer")
    with pytest.raises(ArchiveSearchPublicationDenied):
        attest_archive_search_before_publication(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer="Replay",
            candidate_reauthorizer=reauthorize_archive_search_candidate,
            coverage_reauthorizer=reauthorize_archive_search_coverage,
            authority_context=context,
        )


def test_publication_refresh_reproduces_honestly_degraded_storage_coverage() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("BEGIN")
    request = ArchiveSearchRequest.create(
        query="missing storage",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
    )
    ledger = _ledger()
    prepared = prepare_archive_search_in_transaction(
        conn,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        request=request,
        snapshot_discriminator=SNAPSHOT,
        run_discriminator="degraded-publication",
        turn_ledger=ledger,
    )
    body = prepared.authorized_batch.model_visible_canonical_bytes
    ledger.admit_model_tool_bytes(prepared.run_binding, prepared.authorized_batch, body)
    ledger.freeze_for_publication()
    context = refresh_archive_search_reauthorization_in_transaction(
        conn,
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        prepared_searches=(prepared,),
    )
    attestation = attest_archive_search_before_publication(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        ledger=ledger,
        answer="Coverage is unavailable",
        candidate_reauthorizer=reauthorize_archive_search_candidate,
        coverage_reauthorizer=reauthorize_archive_search_coverage,
        authority_context=context,
    )
    assert attestation.attests_answer("Coverage is unavailable")
