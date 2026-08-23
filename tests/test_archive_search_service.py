from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pytest

import friday.retrieval.archive_search_service as service_module
from friday.organs.obsidian import OBSIDIAN_READ
from friday.permissions import ActorContext, AuthorizationService
from friday.retrieval.archive_search_authority import (
    ArchiveSearchPublicationDenialReason,
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
from friday.storage.models import InboxItem, InboxStatus, RawObject

TENANT = "archive-service-tenant"
PRINCIPAL = "archive-service-principal"
SNAPSHOT = "archive-service-snapshot"
_TURNS = itertools.count(1)


class _AlienReviewState(StrEnum):
    CONFIRMED = "confirmed"


@dataclass(frozen=True)
class _AlienActorContext(ActorContext):
    pass


def _actor(*, principal: str = PRINCIPAL) -> ActorContext:
    return ActorContext(
        user_id=TENANT,
        preset_key="user",
        source="archive-service-test",
        shared_tenant=True,
        person_id=principal,
    )


def _authorization(storage: Any) -> AuthorizationService:
    authorization = AuthorizationService(storage, shared_tenant=TENANT)
    authorization.register_capability(OBSIDIAN_READ)
    return authorization


def _seed_authority(storage: Any, *, principal: str = PRINCIPAL) -> AuthorizationService:
    storage.ensure_user(TENANT)
    storage.ensure_user(principal)
    return _authorization(storage)


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


def test_facade_requires_one_caller_owned_transaction_and_closes_unsupported_plan(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    actor = _actor()
    conn = storage.conn
    request = ArchiveSearchRequest.create(
        query="private artifact",
        corpora=(ArchiveSearchCorpus.GENERATED,),
        limit=2,
    )
    ledger = _ledger()

    with pytest.raises(ArchiveSearchServiceError):
        prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=actor,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="outside-transaction",
            turn_ledger=ledger,
        )

    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=actor,
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="inside-transaction",
            turn_ledger=ledger,
        )
        assert conn.in_transaction
    payload = _payload(prepared)
    assert payload["candidates"] == []
    assert payload["absence"] == "not_established"
    assert len(payload["coverage"]) == 5
    assert all(
        item["states"] == [CoverageState.UNAVAILABLE.value]
        and item["authority_rechecked"] is True
        and item["snapshot_current"] is True
        for item in payload["coverage"]
    )


def test_document_storage_failure_is_not_false_absence(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    monkeypatch.setattr(
        service_module,
        "search_archive_document_lane",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError()),
    )
    request = ArchiveSearchRequest.create(
        query="private document",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="document-storage-failure",
            turn_ledger=_ledger(),
        )
    payload = _payload(prepared)
    for lane in (SearchLane.CATALOG, SearchLane.LEXICAL):
        item = _coverage(payload, lane)
        assert item["states"] == [
            CoverageState.PARTIAL.value,
            CoverageState.UNAVAILABLE.value,
        ]
        assert item["authority_rechecked"] is False
        assert item["snapshot_current"] is False
    assert payload["absence"] == "not_established"


def test_document_lanes_are_federated_from_the_authoritative_store(storage: Any) -> None:
    authorization = _seed_authority(storage)
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
            authorization=authorization,
            actor=_actor(),
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
    assert _coverage(payload, SearchLane.LEXICAL)["states"] == [
        "backfill_pending",
        "partial",
    ]
    assert _coverage(payload, SearchLane.DENSE)["states"] == ["unavailable"]


def test_message_history_uses_authorized_context_and_leaves_other_lanes_unavailable(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    conversation = storage.create_conversation(PRINCIPAL, "Friday project")
    storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "user",
        "Needle private conversation",
    )
    storage.store_message(
        conversation["id"],
        PRINCIPAL,
        "assistant",
        "Adjacent context",
    )
    request = ArchiveSearchRequest.create(
        query="Needle",
        corpora=(ArchiveSearchCorpus.MESSAGES,),
        limit=5,
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
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
    authorization = _seed_authority(storage)
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
            authorization=authorization,
            actor=_actor(),
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


@pytest.mark.parametrize(
    ("corpus", "capability"),
    (
        (ArchiveSearchCorpus.DOCUMENTS, "knowledge.read"),
        (ArchiveSearchCorpus.KNOWLEDGE, "knowledge.read"),
        (ArchiveSearchCorpus.MESSAGES, "conversations.read"),
        (ArchiveSearchCorpus.OBSIDIAN, "obsidian.read"),
        (ArchiveSearchCorpus.GENERATED, "knowledge.read"),
        (ArchiveSearchCorpus.WEB, "knowledge.read"),
    ),
)
def test_initial_explicit_corpus_denial_is_honest_and_reads_no_lane(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
    corpus: ArchiveSearchCorpus,
    capability: str,
) -> None:
    authorization = _seed_authority(storage)
    storage.set_permission_override(PRINCIPAL, capability, "deny")
    lane_calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        lane_calls.append("called")
        raise AssertionError("a denied corpus reached its storage lane")

    monkeypatch.setattr(service_module, "_collect_document_target", forbidden)
    monkeypatch.setattr(service_module, "_collect_message_target", forbidden)
    monkeypatch.setattr(service_module, "_collect_obsidian_target", forbidden)
    request = ArchiveSearchRequest.create(query="private denied", corpora=(corpus,))
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator=f"initial-deny-{corpus.value}",
            turn_ledger=_ledger(),
        )
    payload = _payload(prepared)
    assert lane_calls == []
    assert payload["candidates"] == []
    assert payload["absence"] == "not_established"
    assert all(
        set(item["states"])
        == {CoverageState.PARTIAL.value, CoverageState.PERMISSION_FILTERED.value}
        and item["authority_rechecked"] is True
        and item["snapshot_current"] is True
        for item in payload["coverage"]
    )


def test_external_archive_corpus_is_unavailable_and_never_reaches_a_lane(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    lane_calls: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        lane_calls.append("called")
        raise AssertionError("external archive search attempted an execution lane")

    monkeypatch.setattr(service_module, "_collect_document_target", forbidden)
    monkeypatch.setattr(service_module, "_collect_message_target", forbidden)
    monkeypatch.setattr(service_module, "_collect_obsidian_target", forbidden)
    request = ArchiveSearchRequest.create(
        query="private external",
        corpora=(ArchiveSearchCorpus.EXTERNAL,),
    )
    assert request.permits_outbound is False
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="external-never-outbound",
            turn_ledger=_ledger(),
        )
    payload = _payload(prepared)
    assert lane_calls == []
    assert payload["candidates"] == []
    assert all(item["states"] == [CoverageState.UNAVAILABLE.value] for item in payload["coverage"])
    assert all(
        item.capability is None and item.allowed is False
        for item in prepared._recipe.target_authority
    )


def test_global_search_capability_remains_the_kernel_boundary(storage: Any) -> None:
    authorization = _seed_authority(storage)
    storage.set_permission_override(PRINCIPAL, "search.use", "deny")
    request = ArchiveSearchRequest.create(
        query="kernel already admitted this search",
        corpora=(ArchiveSearchCorpus.GENERATED,),
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="kernel-search-boundary",
            turn_ledger=_ledger(),
        )
    assert all(
        CoverageState.PERMISSION_FILTERED.value not in item["states"]
        for item in _payload(prepared)["coverage"]
    )


def test_same_value_foreign_actor_and_unbound_principal_fail_closed(storage: Any) -> None:
    authorization = _seed_authority(storage)
    canonical = _actor()
    alien = _AlienActorContext(
        user_id=canonical.user_id,
        preset_key=canonical.preset_key,
        source=canonical.source,
        identity_id=canonical.identity_id,
        session_id=canonical.session_id,
        shared_tenant=canonical.shared_tenant,
        person_id=canonical.person_id,
    )
    request = ArchiveSearchRequest.create(
        query="private actor",
        corpora=(ArchiveSearchCorpus.GENERATED,),
    )
    for actor in (
        alien,
        ActorContext(
            user_id=TENANT,
            preset_key="owner",
            source="archive-service-test",
            person_id=PRINCIPAL,
        ),
    ):
        with storage.transaction() as conn, pytest.raises(ArchiveSearchServiceError):
            prepare_archive_search_in_transaction(
                conn,
                authorization=authorization,
                actor=actor,
                tenant_id=TENANT,
                principal_id=PRINCIPAL,
                request=request,
                snapshot_discriminator=SNAPSHOT,
                run_discriminator=f"actor-spoof-{type(actor).__name__}",
                turn_ledger=_ledger(),
            )


@pytest.mark.parametrize(
    ("corpus", "capability"),
    (
        (ArchiveSearchCorpus.GENERATED, "knowledge.read"),
        (ArchiveSearchCorpus.MESSAGES, "conversations.read"),
        (ArchiveSearchCorpus.OBSIDIAN, "obsidian.read"),
    ),
)
def test_between_phase_explicit_deny_blocks_publication(
    storage: Any,
    corpus: ArchiveSearchCorpus,
    capability: str,
) -> None:
    authorization = _seed_authority(storage)
    request = ArchiveSearchRequest.create(query="private late deny", corpora=(corpus,))
    ledger = _ledger()
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator=f"late-deny-{corpus.value}",
            turn_ledger=ledger,
        )
    ledger.admit_model_tool_bytes(
        prepared.run_binding,
        prepared.authorized_batch,
        prepared.authorized_batch.model_visible_canonical_bytes,
    )
    ledger.freeze_for_publication()
    storage.set_permission_override(PRINCIPAL, capability, "deny")
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )
    with pytest.raises(ArchiveSearchPublicationDenied) as denied:
        attest_archive_search_before_publication(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer="This must not publish",
            candidate_reauthorizer=reauthorize_archive_search_candidate,
            coverage_reauthorizer=reauthorize_archive_search_coverage,
            authority_context=context,
        )
    assert denied.value.reason is ArchiveSearchPublicationDenialReason.AUTHORITY_CHANGED


def test_between_phase_explicit_allow_revocation_blocks_publication(storage: Any) -> None:
    authorization = _seed_authority(storage)
    authorization.create_custom_preset(
        "archive_denied",
        "Archive denied",
        set(),
        created_by=PRINCIPAL,
    )
    authorization.set_user_preset(PRINCIPAL, "archive_denied")
    storage.set_permission_override(PRINCIPAL, "knowledge.read", "allow")
    request = ArchiveSearchRequest.create(
        query="private late revoke",
        corpora=(ArchiveSearchCorpus.GENERATED,),
    )
    ledger = _ledger()
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="late-explicit-allow-revoke",
            turn_ledger=ledger,
        )
    ledger.admit_model_tool_bytes(
        prepared.run_binding,
        prepared.authorized_batch,
        prepared.authorized_batch.model_visible_canonical_bytes,
    )
    ledger.freeze_for_publication()
    storage.set_permission_override(PRINCIPAL, "knowledge.read", None)
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )
    with pytest.raises(ArchiveSearchPublicationDenied) as denied:
        attest_archive_search_before_publication(
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            ledger=ledger,
            answer="This must not publish",
            candidate_reauthorizer=reauthorize_archive_search_candidate,
            coverage_reauthorizer=reauthorize_archive_search_coverage,
            authority_context=context,
        )
    assert denied.value.reason is ArchiveSearchPublicationDenialReason.AUTHORITY_CHANGED


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


def test_sealed_corpus_authority_rejects_a_federation_bypass(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    storage.set_permission_override(PRINCIPAL, "knowledge.read", "deny")
    monkeypatch.setattr(
        service_module,
        "_collect_federated_in_transaction",
        _synthetic_federation,
    )
    request = ArchiveSearchRequest.create(
        query="private bypass",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=1,
    )
    with storage.transaction() as conn, pytest.raises(ArchiveSearchServiceError):
        prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="federation-bypass",
            turn_ledger=_ledger(),
        )


def test_continuation_cannot_widen_a_previously_denied_corpus(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    storage.set_permission_override(PRINCIPAL, "obsidian.read", "deny")
    obsidian_calls: list[tuple[SearchCorpus, SearchLane]] = []

    def synthetic_document_lane(
        _conn: sqlite3.Connection,
        **values: Any,
    ) -> tuple[tuple[ArchiveSearchCandidate, ...], SearchCoverage]:
        target = values["target"]
        binding = values["run"].execution_binding
        candidates = (
            tuple(_synthetic_candidate(index, index) for index in range(1, 4))
            if target == (SearchCorpus.RAW_DOCUMENTS, SearchLane.CATALOG)
            else ()
        )
        return candidates, SearchCoverage.create(
            corpus=target[0],
            lane=target[1],
            execution_binding=binding,
            states=(
                (CoverageState.COMPLETE,)
                if candidates
                else (CoverageState.UNAVAILABLE,)
            ),
            eligible_authorized=3 if candidates else None,
            examined=3 if candidates else 0,
            matched_at_least=3 if candidates else 0,
            returned=3 if candidates else 0,
            authority_rechecked=True,
            snapshot_current=True,
        )

    def observed_obsidian_lane(
        _conn: sqlite3.Connection,
        **values: Any,
    ) -> tuple[tuple[ArchiveSearchCandidate, ...], SearchCoverage]:
        target = values["target"]
        obsidian_calls.append(target)
        return (), service_module._unsupported(
            target,
            values["run"].execution_binding,
        )

    monkeypatch.setattr(
        service_module,
        "_collect_document_target",
        synthetic_document_lane,
    )
    monkeypatch.setattr(
        service_module,
        "_collect_obsidian_target",
        observed_obsidian_lane,
    )
    ledger = _ledger()
    request = ArchiveSearchRequest.create(
        query="private continuation",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.OBSIDIAN),
        limit=1,
    )
    with storage.transaction() as conn:
        first = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="authority-continuation-first",
            turn_ledger=ledger,
        )
    first_payload = _payload(first)
    assert isinstance(first_payload["continuation"], str)
    assert obsidian_calls == []

    storage.set_permission_override(PRINCIPAL, "obsidian.read", None)
    resumed = ArchiveSearchRequest.create(
        query="private continuation",
        corpora=(ArchiveSearchCorpus.DOCUMENTS, ArchiveSearchCorpus.OBSIDIAN),
        limit=1,
        continuation=first_payload["continuation"],
    )
    with storage.transaction() as conn:
        second = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            request=resumed,
            snapshot_discriminator=SNAPSHOT,
            run_discriminator="authority-continuation-second",
            turn_ledger=ledger,
        )
    assert obsidian_calls == []
    assert all(
        item.allowed is False
        for item in second._recipe.target_authority
        if item.corpus is SearchCorpus.OBSIDIAN
    )
    assert all(
        item["corpus"] == ArchiveSearchCorpus.DOCUMENTS.value
        for item in _payload(second)["candidates"]
    )


def test_deterministic_continuation_reauthorizes_against_fresh_full_universe(
    monkeypatch: pytest.MonkeyPatch,
    storage: Any,
) -> None:
    monkeypatch.setattr(
        service_module,
        "_collect_federated_in_transaction",
        _synthetic_federation,
    )
    authorization = _seed_authority(storage)
    ledger = _ledger()
    initial_request = ArchiveSearchRequest.create(
        query="private documents",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=1,
    )
    with storage.transaction() as conn:
        first = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
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
    with storage.transaction() as conn:
        second = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
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
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
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
    storage: Any,
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
    authorization = _seed_authority(storage)
    request = ArchiveSearchRequest.create(
        query="private documents",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
        limit=1,
    )
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
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
    with storage.transaction() as conn, pytest.raises(ArchiveSearchServiceError):
        refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id=PRINCIPAL,
            prepared_searches=(prepared,),
        )
    assert run.execution_binding.is_live_private_request_binding


def test_publication_refresh_attests_exact_batch_and_rejects_wrong_actor(
    storage: Any,
) -> None:
    authorization = _seed_authority(storage)
    request = ArchiveSearchRequest.create(
        query="private artifact",
        corpora=(ArchiveSearchCorpus.GENERATED,),
    )
    ledger = _ledger()
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
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
    with storage.transaction() as conn, pytest.raises(ArchiveSearchServiceError):
        refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
            tenant_id=TENANT,
            principal_id="wrong-principal",
            prepared_searches=(prepared,),
        )
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
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


def test_publication_refresh_reproduces_honestly_degraded_storage_coverage(
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _seed_authority(storage)
    monkeypatch.setattr(
        service_module,
        "search_archive_document_lane",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError()),
    )
    request = ArchiveSearchRequest.create(
        query="missing storage",
        corpora=(ArchiveSearchCorpus.DOCUMENTS,),
    )
    ledger = _ledger()
    with storage.transaction() as conn:
        prepared = prepare_archive_search_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
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
    with storage.transaction() as conn:
        context = refresh_archive_search_reauthorization_in_transaction(
            conn,
            authorization=authorization,
            actor=_actor(),
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
