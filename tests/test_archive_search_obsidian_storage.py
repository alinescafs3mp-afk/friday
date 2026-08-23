from __future__ import annotations

import copy
import hashlib
import json
import pickle
import re
from typing import Any

import pytest

from friday.retrieval.archive_search_contract import (
    ArchiveLifecycleConstraint,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
    ArchiveTemporalConstraint,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CoverageState,
    LifecycleState,
    SearchCorpus,
    SearchCoverage,
    SearchExecutionBinding,
    SearchLane,
    TemporalPrecision,
    TemporalRole,
    TemporalValueKind,
)
from friday.storage._archive_search_obsidian import (
    ArchiveObsidianCoverage,
    ArchiveObsidianHit,
    ArchiveObsidianIndexState,
    ArchiveObsidianLanePage,
    ArchiveObsidianMatchKind,
    ArchiveObsidianReadPhase,
    ArchiveObsidianStorageError,
    ArchiveObsidianUnavailableReason,
    VerifiedArchiveObsidianBody,
    VerifiedArchiveObsidianNavigation,
    select_archive_obsidian_lane_in_transaction,
    verify_archive_obsidian_factual_hit_in_transaction,
    verify_archive_obsidian_navigation_hit_in_transaction,
)

OWNER = "obsidian-archive-owner"
FOREIGN = "obsidian-archive-foreign"
TENANT = "obsidian-archive-actor"
FOREIGN_TENANT = "obsidian-archive-foreign-actor"
PRIVATE_BODY = "Фиолетовый маршрутизатор QNAP хранится в закрытом шкафу."
SNAPSHOT = "obsidian-archive-snapshot"


def _bundle(storage: Any, owner: str) -> dict[str, dict[str, Any]]:
    storage.ensure_user(owner)
    bundle = storage.create_obsidian_bundle(
        owner,
        config_root=f"/private/config/{owner}",
        database_root=f"/private/data/{owner}",
        api_endpoint=f"unix:///private/run/{owner}.sock",
        api_key_ref=f"secret:obsidian:{owner}",
        server_path=f"/private/vaults/{owner}",
        folder_id=f"friday-{owner}",
        setup_token_hash=hashlib.sha256(f"token:{owner}".encode()).hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )
    bundle["vault"] = storage.update_obsidian_vault(owner, state="ready")
    return bundle


def _seed(
    storage: Any,
    *,
    owner: str,
    vault_id: str,
    number: int,
    path: str,
    body: str = "ordinary note body",
    title: str = "",
    aliases: tuple[str, ...] = (),
    body_coverage: str = "complete",
    indexed_body: str | None = None,
) -> dict[str, Any]:
    encoded = body.encode("utf-8")
    revision = hashlib.sha256(encoded).hexdigest()
    binding = storage.upsert_obsidian_note_binding(
        owner,
        vault_id=vault_id,
        integration_id=f"archive-note-{number}",
        current_path=path,
        current_revision=revision,
        origin="user",
    )
    projection = body if indexed_body is None else indexed_body
    storage.upsert_obsidian_note_index(
        owner,
        binding_id=str(binding["id"]),
        revision=revision,
        metadata={"aliases": list(aliases)},
        metadata_coverage="complete",
        body_text=projection,
        body_coverage=body_coverage,
        source_size_bytes=(len(encoded) if body_coverage == "complete" else len(encoded) + 1),
        title=title,
    )
    return {**binding, "body": body, "revision": revision}


def _request(
    query: str,
    *,
    limit: int = 20,
    temporal: tuple[ArchiveTemporalConstraint, ...] = (),
    lifecycle: tuple[ArchiveLifecycleConstraint, ...] = (),
) -> ArchiveSearchRequest:
    return ArchiveSearchRequest.create(
        query=query,
        corpora=(ArchiveSearchCorpus.OBSIDIAN,),
        temporal_constraints=temporal,
        lifecycle_constraints=lifecycle,
        limit=limit,
    )


def _search(
    storage: Any,
    request: ArchiveSearchRequest,
    lane: SearchLane,
    *,
    tenant: str = TENANT,
    owner: str = OWNER,
    snapshot: str = SNAPSHOT,
    execution_binding: SearchExecutionBinding | None = None,
    limit: int | None = None,
) -> ArchiveObsidianLanePage:
    storage.conn.execute("BEGIN")
    try:
        binding = execution_binding or _binding(
            request,
            lane,
            tenant=tenant,
            owner=owner,
            snapshot=snapshot,
        )
        return select_archive_obsidian_lane_in_transaction(
            storage.conn,
            tenant_id=tenant,
            principal_id=owner,
            request=request,
            snapshot_discriminator=snapshot,
            execution_binding=binding,
            lane=lane,
            limit=limit,
        )
    finally:
        storage.conn.rollback()


def _binding(
    request: ArchiveSearchRequest,
    lane: SearchLane,
    *,
    tenant: str = TENANT,
    owner: str = OWNER,
    snapshot: str = SNAPSHOT,
    run: str = "obsidian-archive-run",
) -> SearchExecutionBinding:
    return SearchExecutionBinding.create(
        normalized_private_request_json=request.to_identity_json(),
        authority_scope=AuthorityScope.TENANT_PRINCIPAL,
        tenant_id=tenant,
        principal_id=owner,
        requested_targets=((SearchCorpus.OBSIDIAN, lane),),
        snapshot_discriminator=snapshot,
        run_discriminator=run,
        privacy_key=b"o" * 32,
    )


def _coverage(
    page: ArchiveObsidianLanePage,
    request: ArchiveSearchRequest,
    lane: SearchLane,
    *,
    tenant: str = TENANT,
    owner: str = OWNER,
    snapshot: str = SNAPSHOT,
    run: str = "obsidian-archive-run",
) -> SearchCoverage:
    return page.to_coverage(
        execution_binding=_binding(
            request,
            lane,
            tenant=tenant,
            owner=owner,
            snapshot=snapshot,
            run=run,
        ),
        tenant_id=tenant,
        principal_id=owner,
        request=request,
        snapshot_discriminator=snapshot,
    )


def _capture_navigation(
    *,
    binding_id: str,
    vault_id: str,
    path: str,
    title: str,
    aliases: tuple[str, ...],
    current_revision: str,
    lifecycle: LifecycleState,
    index_state: ArchiveObsidianIndexState,
    index_revision_current: bool,
    index_path_current: bool,
    metadata_coverage: ArchiveObsidianCoverage,
    body_coverage: ArchiveObsidianCoverage,
    lane: SearchLane,
    match_kind: ArchiveObsidianMatchKind,
    rank: int,
) -> tuple[object, ...]:
    return (
        binding_id,
        vault_id,
        path,
        title,
        aliases,
        current_revision,
        lifecycle,
        index_state,
        index_revision_current,
        index_path_current,
        metadata_coverage,
        body_coverage,
        lane,
        match_kind,
        rank,
    )


def test_owner_materialization_precedes_counts_and_foreign_rows_cannot_change_ranks(storage) -> None:
    own = _bundle(storage, OWNER)
    foreign = _bundle(storage, FOREIGN)
    own_vault = str(own["vault"]["id"])
    foreign_vault = str(foreign["vault"]["id"])
    _seed(
        storage,
        owner=OWNER,
        vault_id=own_vault,
        number=1,
        path="Projects/Friday.md",
        title="Friday",
    )
    _seed(
        storage,
        owner=OWNER,
        vault_id=own_vault,
        number=2,
        path="Projects/Friday Alpha.md",
        title="Friday Alpha",
    )
    _seed(
        storage,
        owner=OWNER,
        vault_id=own_vault,
        number=3,
        path="Archive/Annual Friday Report.md",
        title="Annual Friday Report",
    )
    request = _request("Friday")
    before = _search(storage, request, SearchLane.CATALOG)
    before_signature = tuple((hit.path, hit.match_kind, hit.rank) for hit in before.hits)

    for number in range(20, 35):
        _seed(
            storage,
            owner=FOREIGN,
            vault_id=foreign_vault,
            number=number,
            path=f"Friday/Foreign {number}.md",
            title="Friday",
        )
    after = _search(storage, request, SearchLane.CATALOG)

    assert (after.eligible_authorized, after.examined, after.matched, after.returned) == (3, 3, 3, 3)
    assert tuple((hit.path, hit.match_kind, hit.rank) for hit in after.hits) == before_signature
    assert [hit.match_kind for hit in after.hits] == [
        ArchiveObsidianMatchKind.EXACT,
        ArchiveObsidianMatchKind.PREFIX,
        ArchiveObsidianMatchKind.SUBSTRING,
    ]
    assert all(not hit.factual and not hit.requires_exact_file_reauthorization for hit in after.hits)
    coverage = _coverage(after, request, SearchLane.CATALOG)
    assert coverage.states == (CoverageState.COMPLETE,)
    assert coverage.eligible_authorized == coverage.examined == 3


def test_absent_and_inactive_principals_are_denied_not_complete_zero(storage) -> None:
    request = _request("Nothing may establish absence")
    absent = _search(storage, request, SearchLane.CATALOG, owner="absent-principal")
    absent_coverage = _coverage(
        absent,
        request,
        SearchLane.CATALOG,
        owner="absent-principal",
    )
    assert absent.unavailable_reason is ArchiveObsidianUnavailableReason.PRINCIPAL_DENIED
    assert absent.eligible_authorized is None
    assert absent_coverage.authority_rechecked is False
    assert absent_coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)
    assert absent_coverage.absence_decision().value == "not_established"

    bundle = _bundle(storage, OWNER)
    _seed(
        storage,
        owner=OWNER,
        vault_id=str(bundle["vault"]["id"]),
        number=16,
        path="Private/Invisible.md",
    )
    with storage.transaction() as conn:
        conn.execute("UPDATE users SET status='disabled' WHERE id=?", (OWNER,))
    inactive = _search(storage, request, SearchLane.CATALOG)
    inactive_coverage = _coverage(inactive, request, SearchLane.CATALOG)
    assert inactive.unavailable_reason is ArchiveObsidianUnavailableReason.PRINCIPAL_DENIED
    assert inactive.eligible_authorized is None
    assert inactive_coverage.authority_rechecked is False
    assert CoverageState.COMPLETE not in inactive_coverage.states
    assert inactive_coverage.absence_decision().value == "not_established"


def test_missing_or_nonready_owner_vault_is_unavailable_without_foreign_influence(storage) -> None:
    storage.ensure_user(OWNER)
    foreign = _bundle(storage, FOREIGN)
    _seed(
        storage,
        owner=FOREIGN,
        vault_id=str(foreign["vault"]["id"]),
        number=160,
        path="Foreign/Friday.md",
        title="Friday",
    )
    request = _request("Friday")

    missing = _search(storage, request, SearchLane.CATALOG)
    missing_coverage = _coverage(missing, request, SearchLane.CATALOG)
    assert missing.unavailable_reason is ArchiveObsidianUnavailableReason.VAULT_UNAVAILABLE
    assert missing.eligible_authorized is None
    assert missing_coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)
    assert missing_coverage.absence_decision().value == "not_established"

    _bundle(storage, OWNER)
    storage.update_obsidian_vault(OWNER, state="initial_sync")
    not_ready = _search(storage, request, SearchLane.CATALOG)
    assert not_ready.unavailable_reason is ArchiveObsidianUnavailableReason.VAULT_UNAVAILABLE
    assert CoverageState.COMPLETE not in _coverage(not_ready, request, SearchLane.CATALOG).states

    storage.update_obsidian_vault(OWNER, state="ready")
    ready = _search(storage, request, SearchLane.CATALOG)
    ready_coverage = _coverage(ready, request, SearchLane.CATALOG)
    assert ready_coverage.states == (CoverageState.COMPLETE,)
    assert ready_coverage.eligible_authorized == ready_coverage.examined == 0


def test_unsearchable_explicit_or_default_lifecycle_never_confirms_false_absence(storage) -> None:
    bundle = _bundle(storage, OWNER)
    _seed(
        storage,
        owner=OWNER,
        vault_id=str(bundle["vault"]["id"]),
        number=161,
        path="Notes/Lifecycle.md",
        body="searchable active lifecycle body",
        title="Lifecycle",
    )
    doomed = _seed(
        storage,
        owner=OWNER,
        vault_id=str(bundle["vault"]["id"]),
        number=162,
        path="Notes/Tombstoned Secret.md",
        body="default tombstoned lexical needle",
        title="Tombstoned Secret",
    )
    storage.tombstone_obsidian_note_binding(
        OWNER,
        "archive-note-162",
        vault_id=str(bundle["vault"]["id"]),
        expected_revision=str(doomed["revision"]),
    )

    default_request = _request("default tombstoned lexical needle")
    default_page = _search(storage, default_request, SearchLane.LEXICAL)
    default_coverage = _coverage(default_page, default_request, SearchLane.LEXICAL)
    assert default_page.unavailable_reason is ArchiveObsidianUnavailableReason.LIFECYCLE_UNSUPPORTED
    assert default_coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)
    assert default_coverage.absence_decision().value == "not_established"

    mixed = ArchiveLifecycleConstraint.create(
        ArchiveSearchCorpus.OBSIDIAN,
        (LifecycleState.ACTIVE, LifecycleState.TOMBSTONED),
    )
    mixed_request = _request("lifecycle", lifecycle=(mixed,))
    mixed_page = _search(storage, mixed_request, SearchLane.LEXICAL)
    mixed_coverage = _coverage(mixed_page, mixed_request, SearchLane.LEXICAL)
    assert mixed_page.unavailable_reason is ArchiveObsidianUnavailableReason.LIFECYCLE_UNSUPPORTED
    assert CoverageState.COMPLETE not in mixed_coverage.states
    assert mixed_coverage.absence_decision().value == "not_established"

    tombstoned = ArchiveLifecycleConstraint.create(
        ArchiveSearchCorpus.OBSIDIAN,
        (LifecycleState.TOMBSTONED,),
    )
    lexical_request = _request("lifecycle", lifecycle=(tombstoned,))
    lexical = _search(storage, lexical_request, SearchLane.LEXICAL)
    lexical_coverage = _coverage(lexical, lexical_request, SearchLane.LEXICAL)
    assert lexical.unavailable_reason is ArchiveObsidianUnavailableReason.LIFECYCLE_UNSUPPORTED
    assert CoverageState.COMPLETE not in lexical_coverage.states
    assert lexical_coverage.absence_decision().value == "not_established"

    deleted = ArchiveLifecycleConstraint.create(
        ArchiveSearchCorpus.OBSIDIAN,
        (LifecycleState.DELETED,),
    )
    identity_request = _request("Lifecycle", lifecycle=(deleted,))
    identity = _search(storage, identity_request, SearchLane.CATALOG)
    identity_coverage = _coverage(identity, identity_request, SearchLane.CATALOG)
    assert identity.unavailable_reason is ArchiveObsidianUnavailableReason.LIFECYCLE_UNSUPPORTED
    assert identity.eligible_authorized is None
    assert identity_coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)
    assert identity_coverage.absence_decision().value == "not_established"

    active = ArchiveLifecycleConstraint.create(
        ArchiveSearchCorpus.OBSIDIAN,
        (LifecycleState.ACTIVE,),
    )
    active_request = _request("lifecycle", lifecycle=(active,))
    active_lexical = _search(storage, active_request, SearchLane.LEXICAL)
    active_coverage = _coverage(active_lexical, active_request, SearchLane.LEXICAL)
    assert active_coverage.states == (CoverageState.COMPLETE,)
    assert active_coverage.returned == 1


def test_page_and_nested_hits_reject_cross_scope_and_object_setattr_tamper(storage) -> None:
    bundle = _bundle(storage, OWNER)
    _seed(
        storage,
        owner=OWNER,
        vault_id=str(bundle["vault"]["id"]),
        number=17,
        path="Projects/Sealed Request.md",
        title="Sealed Request",
    )
    request = _request("Sealed Request")

    def coverage_for(
        page: ArchiveObsidianLanePage,
        *,
        tenant: str = TENANT,
        owner: str = OWNER,
        bound_request: ArchiveSearchRequest = request,
        supplied_request: ArchiveSearchRequest = request,
        snapshot: str = SNAPSHOT,
        run: str = "obsidian-archive-run",
    ) -> SearchCoverage:
        return page.to_coverage(
            execution_binding=_binding(
                bound_request,
                SearchLane.CATALOG,
                tenant=tenant,
                owner=owner,
                snapshot=snapshot,
                run=run,
            ),
            tenant_id=tenant,
            principal_id=owner,
            request=supplied_request,
            snapshot_discriminator=snapshot,
        )

    page = _search(storage, request, SearchLane.CATALOG)
    assert coverage_for(page).states == (CoverageState.COMPLETE,)
    with pytest.raises(ArchiveObsidianStorageError):
        _search(
            storage,
            request,
            SearchLane.CATALOG,
            execution_binding=_binding(
                request,
                SearchLane.CATALOG,
                tenant=FOREIGN_TENANT,
            ),
        )
    public_only = SearchExecutionBinding.from_payload(_binding(request, SearchLane.CATALOG).to_payload())
    with pytest.raises(ArchiveObsidianStorageError):
        _search(
            storage,
            request,
            SearchLane.CATALOG,
            execution_binding=public_only,
        )
    failures = (
        lambda: coverage_for(
            page,
            bound_request=_request("Different request"),
            supplied_request=_request("Different request"),
        ),
        lambda: coverage_for(page, owner=FOREIGN),
        lambda: coverage_for(page, tenant=FOREIGN_TENANT),
        lambda: coverage_for(page, snapshot="different-snapshot"),
        lambda: coverage_for(page, run="different-run"),
        lambda: coverage_for(
            page,
            bound_request=_request("Different binding"),
            supplied_request=request,
        ),
    )
    for operation in failures:
        with pytest.raises(ArchiveObsidianStorageError) as error:
            operation()
        assert error.value.__cause__ is None
        assert str(page.hits[0].binding_id) not in str(error.value)

    tampered_page = _search(storage, request, SearchLane.CATALOG)
    object.__setattr__(tampered_page, "eligible_authorized", 0)
    with pytest.raises(ArchiveObsidianStorageError):
        coverage_for(tampered_page)

    nested_tamper = _search(storage, request, SearchLane.CATALOG)
    object.__setattr__(nested_tamper.hits[0], "path", "Forged.md")
    with pytest.raises(ArchiveObsidianStorageError):
        coverage_for(nested_tamper)


def test_alias_typo_and_keyboard_layout_are_bounded_after_native_identity_matches(storage) -> None:
    bundle = _bundle(storage, OWNER)
    vault = str(bundle["vault"]["id"])
    _seed(
        storage,
        owner=OWNER,
        vault_id=vault,
        number=40,
        path="Notes/Phoenix.md",
        title="Phoenix",
        aliases=("Project Phoenix", "Legacy Codename"),
    )
    _seed(
        storage,
        owner=OWNER,
        vault_id=vault,
        number=41,
        path="Notes/Greeting.md",
        title="Greeting",
        aliases=("Привет",),
    )

    exact = _search(storage, _request("Legacy Codename"), SearchLane.EXACT_IDENTITY)
    typo = _search(storage, _request("Projec Phoenix"), SearchLane.APPROXIMATE_IDENTITY)
    layout = _search(storage, _request("ghbdtn"), SearchLane.APPROXIMATE_IDENTITY)

    assert exact.returned == 1 and exact.hits[0].match_kind is ArchiveObsidianMatchKind.EXACT
    assert typo.returned == 1 and typo.hits[0].match_kind is ArchiveObsidianMatchKind.TYPO
    assert layout.returned == 1
    assert layout.hits[0].match_kind is ArchiveObsidianMatchKind.KEYBOARD_LAYOUT
    assert all(hit.rank == 1 for hit in (exact.hits[0], typo.hits[0], layout.hits[0]))


def test_navigation_hit_requires_exact_reauthorization_and_denies_drift(storage) -> None:
    bundle = _bundle(storage, OWNER)
    vault = str(bundle["vault"]["id"])
    seeded = _seed(
        storage,
        owner=OWNER,
        vault_id=vault,
        number=45,
        path="Projects/Reauthorize Me.md",
        title="Reauthorize Me",
    )
    request = _request("Reauthorize Me")
    hit = _search(storage, request, SearchLane.CATALOG).hits[0]

    def authorize(
        candidate: ArchiveObsidianHit,
        phase: ArchiveObsidianReadPhase,
    ) -> VerifiedArchiveObsidianNavigation:
        storage.conn.execute("BEGIN")
        try:
            return verify_archive_obsidian_navigation_hit_in_transaction(
                storage.conn,
                execution_binding=_binding(request, SearchLane.CATALOG),
                tenant_id=TENANT,
                principal_id=OWNER,
                request=request,
                snapshot_discriminator=SNAPSHOT,
                hit=candidate,
                phase=phase,
            )
        finally:
            storage.conn.rollback()

    attestations: list[VerifiedArchiveObsidianNavigation] = []
    for phase in (
        ArchiveObsidianReadPhase.BEFORE_MODEL,
        ArchiveObsidianReadPhase.BEFORE_PUBLICATION,
    ):
        attestation = authorize(hit, phase)
        projection = attestation.consume_with(
            execution_binding=_binding(request, SearchLane.CATALOG),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            hit=hit,
            phase=phase,
            consumer=_capture_navigation,
        )
        assert projection[2:4] == ("Projects/Reauthorize Me.md", "Reauthorize Me")
        with pytest.raises(ArchiveObsidianStorageError):
            attestation.consume_with(
                execution_binding=_binding(request, SearchLane.CATALOG),
                tenant_id=TENANT,
                principal_id=OWNER,
                request=request,
                snapshot_discriminator=SNAPSHOT,
                hit=hit,
                phase=phase,
                consumer=_capture_navigation,
            )
        attestations.append(attestation)

    foreign_actor = authorize(hit, ArchiveObsidianReadPhase.BEFORE_MODEL)
    with pytest.raises(ArchiveObsidianStorageError):
        foreign_actor.consume_with(
            execution_binding=_binding(
                request,
                SearchLane.CATALOG,
                tenant=FOREIGN_TENANT,
            ),
            tenant_id=FOREIGN_TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            hit=hit,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
            consumer=_capture_navigation,
        )

    different_run = authorize(hit, ArchiveObsidianReadPhase.BEFORE_MODEL)
    with pytest.raises(ArchiveObsidianStorageError):
        different_run.consume_with(
            execution_binding=_binding(
                request,
                SearchLane.CATALOG,
                run="different-run",
            ),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            hit=hit,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
            consumer=_capture_navigation,
        )

    assert "Reauthorize Me" not in repr(attestations[0])
    with pytest.raises(ArchiveObsidianStorageError):
        VerifiedArchiveObsidianNavigation()
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError, match="process-private"):
            operation(attestations[0])

    tampered_hit = _search(storage, request, SearchLane.CATALOG).hits[0]
    tampered_carrier = authorize(tampered_hit, ArchiveObsidianReadPhase.BEFORE_MODEL)
    object.__setattr__(tampered_hit, "path", "Forged After Attestation.md")
    callbacks: list[object] = []
    with pytest.raises(ArchiveObsidianStorageError):
        tampered_carrier.consume_with(
            execution_binding=_binding(request, SearchLane.CATALOG),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            hit=tampered_hit,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
            consumer=lambda **values: callbacks.append(values),
        )
    assert callbacks == []

    carrier_tamper = authorize(hit, ArchiveObsidianReadPhase.BEFORE_MODEL)
    object.__setattr__(carrier_tamper, "_phase", ArchiveObsidianReadPhase.BEFORE_PUBLICATION)
    with pytest.raises(ArchiveObsidianStorageError):
        carrier_tamper.consume_with(
            execution_binding=_binding(request, SearchLane.CATALOG),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            hit=hit,
            phase=ArchiveObsidianReadPhase.BEFORE_PUBLICATION,
            consumer=_capture_navigation,
        )

    storage.upsert_obsidian_note_binding(
        OWNER,
        vault_id=vault,
        integration_id="archive-note-45",
        current_path="Projects/Reauthorized Elsewhere.md",
        current_revision=hashlib.sha256(b"new navigation revision").hexdigest(),
        origin="user",
        expected_current_revision=str(seeded["revision"]),
    )
    storage.conn.execute("BEGIN")
    try:
        with pytest.raises(ArchiveObsidianStorageError) as drifted:
            verify_archive_obsidian_navigation_hit_in_transaction(
                storage.conn,
                execution_binding=_binding(request, SearchLane.CATALOG),
                tenant_id=TENANT,
                principal_id=OWNER,
                request=request,
                snapshot_discriminator=SNAPSHOT,
                hit=hit,
                phase=ArchiveObsidianReadPhase.BEFORE_PUBLICATION,
            )
    finally:
        storage.conn.rollback()
    assert drifted.value.__cause__ is None
    assert str(seeded["id"]) not in str(drifted.value)


def test_stale_revision_partial_body_rename_and_tombstone_never_publish_factual_body(storage) -> None:
    bundle = _bundle(storage, OWNER)
    vault = str(bundle["vault"]["id"])
    stale = _seed(
        storage,
        owner=OWNER,
        vault_id=vault,
        number=50,
        path="Projects/Old Name.md",
        body=f"old {PRIVATE_BODY}",
        title="Old Name",
    )
    partial = _seed(
        storage,
        owner=OWNER,
        vault_id=vault,
        number=51,
        path="Projects/Partial.md",
        body=f"partial {PRIVATE_BODY}",
        title="Partial",
        body_coverage="partial",
        indexed_body="partial Фиолетовый",
    )
    new_body = "renamed current content"
    renamed = storage.upsert_obsidian_note_binding(
        OWNER,
        vault_id=vault,
        integration_id="archive-note-50",
        current_path="Projects/Renamed.md",
        current_revision=hashlib.sha256(new_body.encode()).hexdigest(),
        origin="user",
        expected_current_revision=str(stale["revision"]),
    )

    navigation = _search(storage, _request("Renamed"), SearchLane.CATALOG)
    lexical = _search(storage, _request("Фиолетовый"), SearchLane.LEXICAL)
    assert navigation.returned == 1
    renamed_hit = navigation.hits[0]
    assert renamed_hit.binding_id == renamed["id"] == stale["id"]
    assert renamed_hit.path == "Projects/Renamed.md"
    assert renamed_hit.index_state is ArchiveObsidianIndexState.STALE
    assert not renamed_hit.index_revision_current and not renamed_hit.index_path_current
    assert not renamed_hit.factual
    assert (lexical.eligible_authorized, lexical.examined, lexical.matched) == (2, 0, 0)
    assert lexical.stale == 1 and lexical.backfill_pending == 1
    lexical_coverage = _coverage(lexical, _request("Фиолетовый"), SearchLane.LEXICAL)
    assert set(lexical_coverage.states) == {
        CoverageState.BACKFILL_PENDING,
        CoverageState.PARTIAL,
        CoverageState.STALE,
    }

    tombstone = storage.tombstone_obsidian_note_binding(
        OWNER,
        "archive-note-51",
        vault_id=vault,
        expected_revision=str(partial["revision"]),
    )
    deleted_request = _request("Partial")
    deleted_navigation = _search(storage, deleted_request, SearchLane.CATALOG)
    deleted_hit = next(hit for hit in deleted_navigation.hits if hit.binding_id == tombstone["id"])
    assert deleted_hit.lifecycle is LifecycleState.TOMBSTONED
    assert not deleted_hit.factual and deleted_hit.body_coverage is ArchiveObsidianCoverage.NONE
    storage.conn.execute("BEGIN")
    try:
        deleted_attestation = verify_archive_obsidian_navigation_hit_in_transaction(
            storage.conn,
            execution_binding=_binding(deleted_request, SearchLane.CATALOG),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=deleted_request,
            snapshot_discriminator=SNAPSHOT,
            hit=deleted_hit,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
        )
    finally:
        storage.conn.rollback()
    deleted_projection = deleted_attestation.consume_with(
        execution_binding=_binding(deleted_request, SearchLane.CATALOG),
        tenant_id=TENANT,
        principal_id=OWNER,
        request=deleted_request,
        snapshot_discriminator=SNAPSHOT,
        hit=deleted_hit,
        phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
        consumer=_capture_navigation,
    )
    assert deleted_projection[6] is LifecycleState.TOMBSTONED


def test_factual_hit_requires_exact_sha_file_read_at_both_boundaries_and_is_private(storage) -> None:
    bundle = _bundle(storage, OWNER)
    vault = str(bundle["vault"]["id"])
    seeded = _seed(
        storage,
        owner=OWNER,
        vault_id=vault,
        number=60,
        path="Infrastructure/QNAP.md",
        body=PRIVATE_BODY,
        title="QNAP",
    )
    request = _request("Фиолетовый маршрутизатор")
    page = _search(storage, request, SearchLane.LEXICAL)
    assert (page.eligible_authorized, page.examined, page.matched, page.returned) == (1, 1, 1, 1)
    hit = page.hits[0]
    assert hit.factual and hit.requires_exact_file_reauthorization

    reads: list[tuple[str, str, str]] = []

    def exact_reader(vault_id: str, path: str, revision: str) -> bytes:
        reads.append((vault_id, path, revision))
        return PRIVATE_BODY.encode("utf-8")

    def authorize(
        candidate: ArchiveObsidianHit,
        phase: ArchiveObsidianReadPhase,
    ) -> VerifiedArchiveObsidianBody:
        storage.conn.execute("BEGIN")
        try:
            return verify_archive_obsidian_factual_hit_in_transaction(
                storage.conn,
                execution_binding=_binding(request, SearchLane.LEXICAL),
                tenant_id=TENANT,
                principal_id=OWNER,
                request=request,
                snapshot_discriminator=SNAPSHOT,
                hit=candidate,
                phase=phase,
                exact_file_reader=exact_reader,
            )
        finally:
            storage.conn.rollback()

    def consume_text(text: str) -> str:
        return text

    phases = (
        ArchiveObsidianReadPhase.BEFORE_MODEL,
        ArchiveObsidianReadPhase.BEFORE_PUBLICATION,
    )
    verified = [authorize(hit, phase) for phase in phases]
    consumed = [
        item.consume_with(
            execution_binding=_binding(request, SearchLane.LEXICAL),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            hit=hit,
            phase=phase,
            consumer=consume_text,
        )
        for item, phase in zip(verified, phases, strict=True)
    ]
    assert consumed == [PRIVATE_BODY, PRIVATE_BODY]
    assert reads == [(vault, "Infrastructure/QNAP.md", seeded["revision"])] * 2
    with pytest.raises(ArchiveObsidianStorageError):
        verified[0].consume_with(
            execution_binding=_binding(request, SearchLane.LEXICAL),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            hit=hit,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
            consumer=consume_text,
        )

    private_values: tuple[object, ...] = (page, hit, *verified)
    with pytest.raises(ArchiveObsidianStorageError):
        ArchiveObsidianHit()
    with pytest.raises(ArchiveObsidianStorageError):
        VerifiedArchiveObsidianBody()
    for value in private_values:
        rendered = repr(value) + json.dumps(value, default=str, ensure_ascii=False)
        assert PRIVATE_BODY not in rendered and str(seeded["id"]) not in rendered
        with pytest.raises(TypeError):
            json.dumps(value)
        with pytest.raises(TypeError, match="process-private"):
            copy.copy(value)
        with pytest.raises(TypeError, match="process-private"):
            copy.deepcopy(value)
        with pytest.raises(TypeError, match="process-private"):
            pickle.dumps(value)

    for wrong_tenant, wrong_principal, wrong_request, wrong_snapshot, wrong_run in (
        (FOREIGN_TENANT, OWNER, request, SNAPSHOT, "obsidian-archive-run"),
        (TENANT, FOREIGN, request, SNAPSHOT, "obsidian-archive-run"),
        (TENANT, OWNER, _request("different factual request"), SNAPSHOT, "obsidian-archive-run"),
        (TENANT, OWNER, request, "different-snapshot", "obsidian-archive-run"),
        (TENANT, OWNER, request, SNAPSHOT, "different-run"),
    ):
        scoped_carrier = authorize(hit, ArchiveObsidianReadPhase.BEFORE_MODEL)
        callbacks: list[str] = []
        with pytest.raises(ArchiveObsidianStorageError) as rejected:
            scoped_carrier.consume_with(
                execution_binding=_binding(
                    wrong_request,
                    SearchLane.LEXICAL,
                    tenant=wrong_tenant,
                    owner=wrong_principal,
                    snapshot=wrong_snapshot,
                    run=wrong_run,
                ),
                tenant_id=wrong_tenant,
                principal_id=wrong_principal,
                request=wrong_request,
                snapshot_discriminator=wrong_snapshot,
                hit=hit,
                phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
                consumer=lambda text, captured=callbacks: captured.append(text),
            )
        assert rejected.value.__cause__ is None
        assert PRIVATE_BODY not in str(rejected.value)
        assert callbacks == []

    carrier_tamper = authorize(hit, ArchiveObsidianReadPhase.BEFORE_MODEL)
    object.__setattr__(carrier_tamper, "_text", "forged body")
    with pytest.raises(ArchiveObsidianStorageError):
        carrier_tamper.consume_with(
            execution_binding=_binding(request, SearchLane.LEXICAL),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            hit=hit,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
            consumer=consume_text,
        )

    tampered_hit = _search(storage, request, SearchLane.LEXICAL).hits[0]
    post_attestation = authorize(tampered_hit, ArchiveObsidianReadPhase.BEFORE_MODEL)
    object.__setattr__(tampered_hit, "path", "Forged After Attestation.md")
    callbacks_after_tamper: list[str] = []
    with pytest.raises(ArchiveObsidianStorageError):
        post_attestation.consume_with(
            execution_binding=_binding(request, SearchLane.LEXICAL),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            hit=tampered_hit,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
            consumer=lambda text: callbacks_after_tamper.append(text),
        )
    assert callbacks_after_tamper == []

    leaking_consumer_carrier = authorize(hit, ArchiveObsidianReadPhase.BEFORE_MODEL)

    def leaking_consumer(_text: str) -> str:
        raise ArchiveObsidianStorageError(f"consumer leaked {PRIVATE_BODY} {seeded['id']}")

    with pytest.raises(ArchiveObsidianStorageError) as consumer_error:
        leaking_consumer_carrier.consume_with(
            execution_binding=_binding(request, SearchLane.LEXICAL),
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            hit=hit,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
            consumer=leaking_consumer,
        )
    assert consumer_error.value.__cause__ is None
    assert PRIVATE_BODY not in str(consumer_error.value)

    def bad_reader(_vault: str, _path: str, _revision: str) -> bytes:
        raise ArchiveObsidianStorageError(f"private callback body {PRIVATE_BODY} {seeded['id']}")

    storage.conn.execute("BEGIN")
    try:
        with pytest.raises(ArchiveObsidianStorageError) as denied:
            verify_archive_obsidian_factual_hit_in_transaction(
                storage.conn,
                execution_binding=_binding(request, SearchLane.LEXICAL),
                tenant_id=TENANT,
                principal_id=OWNER,
                request=request,
                snapshot_discriminator=SNAPSHOT,
                hit=hit,
                phase=ArchiveObsidianReadPhase.BEFORE_PUBLICATION,
                exact_file_reader=bad_reader,
            )
    finally:
        storage.conn.rollback()
    assert denied.value.__cause__ is None
    assert PRIVATE_BODY not in (str(denied.value) + repr(denied.value))

    storage.upsert_obsidian_note_binding(
        OWNER,
        vault_id=vault,
        integration_id="archive-note-60",
        current_path="Infrastructure/QNAP Renamed.md",
        current_revision=hashlib.sha256(b"changed after model").hexdigest(),
        origin="user",
        expected_current_revision=str(seeded["revision"]),
    )
    storage.conn.execute("BEGIN")
    try:
        with pytest.raises(ArchiveObsidianStorageError) as drifted:
            verify_archive_obsidian_factual_hit_in_transaction(
                storage.conn,
                execution_binding=_binding(request, SearchLane.LEXICAL),
                tenant_id=TENANT,
                principal_id=OWNER,
                request=request,
                snapshot_discriminator=SNAPSHOT,
                hit=hit,
                phase=ArchiveObsidianReadPhase.BEFORE_PUBLICATION,
                exact_file_reader=exact_reader,
            )
    finally:
        storage.conn.rollback()
    assert drifted.value.__cause__ is None


def test_dense_and_temporal_requests_are_explicitly_unavailable(storage) -> None:
    bundle = _bundle(storage, OWNER)
    _seed(
        storage,
        owner=OWNER,
        vault_id=str(bundle["vault"]["id"]),
        number=70,
        path="Notes/Temporal.md",
    )
    ordinary = _request("Temporal")
    dense = _search(storage, ordinary, SearchLane.DENSE)
    dense_coverage = _coverage(dense, ordinary, SearchLane.DENSE)
    assert dense.unavailable and dense.eligible_authorized == 1
    assert dense.unavailable_reason is ArchiveObsidianUnavailableReason.LANE_UNSUPPORTED
    assert dense_coverage.states == (CoverageState.PARTIAL, CoverageState.UNAVAILABLE)
    assert dense_coverage.absence_decision().value == "not_established"

    temporal_constraint = ArchiveTemporalConstraint(
        ArchiveSearchCorpus.OBSIDIAN,
        TemporalRole.DOCUMENT_MODIFIED_AT,
        TemporalValueKind.DATE_INTERVAL,
        TemporalPrecision.DAY,
        "2026-08-01",
        "2026-08-24",
    )
    temporal = _request("Temporal", temporal=(temporal_constraint,))
    unsupported = _search(storage, temporal, SearchLane.CATALOG)
    temporal_coverage = _coverage(unsupported, temporal, SearchLane.CATALOG)
    assert unsupported.unavailable and unsupported.eligible_authorized is None
    assert unsupported.unavailable_reason is ArchiveObsidianUnavailableReason.TEMPORAL_UNSUPPORTED
    assert CoverageState.COMPLETE not in temporal_coverage.states
    assert CoverageState.UNAVAILABLE in temporal_coverage.states


def test_selector_requires_existing_transaction_and_executes_selects_only(storage) -> None:
    bundle = _bundle(storage, OWNER)
    _seed(
        storage,
        owner=OWNER,
        vault_id=str(bundle["vault"]["id"]),
        number=80,
        path="Notes/Read Only.md",
        title="Read Only",
    )
    request = _request("Read Only")
    with pytest.raises(ArchiveObsidianStorageError):
        select_archive_obsidian_lane_in_transaction(
            storage.conn,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            execution_binding=_binding(request, SearchLane.CATALOG),
            lane=SearchLane.CATALOG,
        )

    statements: list[str] = []
    before_changes = storage.conn.total_changes
    storage.conn.set_trace_callback(statements.append)
    storage.conn.execute("BEGIN")
    try:
        page = select_archive_obsidian_lane_in_transaction(
            storage.conn,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            execution_binding=_binding(request, SearchLane.CATALOG),
            lane=SearchLane.CATALOG,
        )
        assert storage.conn.in_transaction is True and page.returned == 1
    finally:
        storage.conn.rollback()
        storage.conn.set_trace_callback(None)
    assert storage.conn.total_changes == before_changes
    assert any(" AS MATERIALIZED " in statement for statement in statements)
    assert not any(
        re.match(r"\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", statement, re.I)
        for statement in statements
    )


def test_malformed_index_alias_fails_body_free_without_raw_identity(storage) -> None:
    bundle = _bundle(storage, OWNER)
    seeded = _seed(
        storage,
        owner=OWNER,
        vault_id=str(bundle["vault"]["id"]),
        number=90,
        path="Notes/Corrupt Alias.md",
        title="Corrupt Alias",
    )
    with storage.transaction() as conn:
        conn.execute(
            "UPDATE obsidian_note_index SET metadata_json=? WHERE user_id=? AND binding_id=?",
            ('{"aliases":[123]}', OWNER, seeded["id"]),
        )

    with pytest.raises(ArchiveObsidianStorageError) as error:
        _search(storage, _request("Corrupt Alias"), SearchLane.CATALOG)
    assert error.value.__cause__ is None
    rendered = str(error.value) + repr(error.value)
    assert str(seeded["id"]) not in rendered and PRIVATE_BODY not in rendered
