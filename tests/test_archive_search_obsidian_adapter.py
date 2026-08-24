from __future__ import annotations

import copy
import hashlib
import pickle
import unicodedata
from enum import StrEnum
from typing import Any, cast

import pytest

from friday.retrieval.archive_search_contract import (
    ArchiveEvidenceAuthority,
    ArchiveMatchChannel,
    ArchiveReviewState,
    ArchiveSearchCorpus,
    ArchiveSearchRequest,
)
from friday.retrieval.archive_search_obsidian_adapter import (
    OBSIDIAN_PASSAGE_INDEX_VERSION,
    ArchiveObsidianAdapterError,
    ArchiveObsidianLaneProjection,
    project_archive_obsidian_lane_page_in_transaction,
)
from friday.retrieval.contracts import (
    AuthorityScope,
    CanonicalObjectKind,
    EmbeddingCompatibility,
    LifecycleState,
    RepresentationKind,
    RevisionKind,
    SearchCorpus,
    SearchExecutionBinding,
    SearchLane,
    TextSpanLocator,
)
from friday.storage._archive_search_obsidian import (
    ArchiveObsidianLanePage,
    ArchiveObsidianReadPhase,
    select_archive_obsidian_lane_in_transaction,
)

OWNER = "archive-adapter-owner"
FOREIGN_OWNER = "archive-adapter-foreign-owner"
TENANT = "archive-adapter-tenant"
FOREIGN_TENANT = "archive-adapter-foreign-tenant"
SNAPSHOT = "archive-adapter-snapshot"


class _ForeignArchiveMatchChannel(StrEnum):
    CATALOG = "catalog"


class _SameJsonCount(int):
    pass


class _SameJsonDifferentDisplay(str):
    def strip(self, _chars: str | None = None, /) -> str:
        return "FORGED TITLE"


def _request(query: str) -> ArchiveSearchRequest:
    return ArchiveSearchRequest.create(
        query=query,
        corpora=(ArchiveSearchCorpus.OBSIDIAN,),
        limit=20,
    )


def _binding(
    request: ArchiveSearchRequest,
    lane: SearchLane,
    *,
    tenant_id: str = TENANT,
    principal_id: str = OWNER,
    snapshot: str = SNAPSHOT,
    run: str = "archive-adapter-run",
) -> SearchExecutionBinding:
    return SearchExecutionBinding.create(
        normalized_private_request_json=request.to_identity_json(),
        authority_scope=AuthorityScope.TENANT_PRINCIPAL,
        tenant_id=tenant_id,
        principal_id=principal_id,
        requested_targets=((SearchCorpus.OBSIDIAN, lane),),
        snapshot_discriminator=snapshot,
        run_discriminator=run,
        privacy_key=b"a" * 32,
    )


def _seed(
    storage: Any,
    *,
    path: str = "Projects/Phoenix.md",
    title: str = "Phoenix",
    aliases: tuple[str, ...] = ("Project Phoenix", "Legacy Codename"),
    body: str = "ordinary body",
) -> dict[str, Any]:
    storage.ensure_user(OWNER)
    bundle = storage.create_obsidian_bundle(
        OWNER,
        config_root="/private/config/archive-adapter",
        database_root="/private/data/archive-adapter",
        api_endpoint="unix:///private/run/archive-adapter.sock",
        api_key_ref="secret:obsidian:archive-adapter",
        server_path="/private/vaults/archive-adapter",
        folder_id="friday-archive-adapter",
        setup_token_hash=hashlib.sha256(b"archive-adapter-token").hexdigest(),
        expires_at="2030-01-01T00:00:00+00:00",
    )
    vault = storage.update_obsidian_vault(OWNER, state="ready")
    encoded = body.encode("utf-8")
    revision = hashlib.sha256(encoded).hexdigest()
    binding = storage.upsert_obsidian_note_binding(
        OWNER,
        vault_id=str(vault["id"]),
        integration_id="archive-adapter-note",
        current_path=path,
        current_revision=revision,
        origin="user",
    )
    storage.upsert_obsidian_note_index(
        OWNER,
        binding_id=str(binding["id"]),
        revision=revision,
        metadata={"aliases": list(aliases)},
        metadata_coverage="complete",
        body_text=body,
        body_coverage="complete",
        source_size_bytes=len(encoded),
        title=title,
    )
    return {
        "binding": binding,
        "body": body,
        "bundle": bundle,
        "revision": revision,
        "vault_id": str(vault["id"]),
    }


def _select(
    storage: Any,
    *,
    request: ArchiveSearchRequest,
    lane: SearchLane,
    execution_binding: SearchExecutionBinding,
) -> ArchiveObsidianLanePage:
    with storage.transaction() as conn:
        return select_archive_obsidian_lane_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            execution_binding=execution_binding,
            lane=lane,
        )


def _project(
    storage: Any,
    *,
    request: ArchiveSearchRequest,
    lane: SearchLane,
    phase: ArchiveObsidianReadPhase,
    exact_file_reader: Any = None,
) -> tuple[ArchiveObsidianLanePage, ArchiveObsidianLaneProjection, SearchExecutionBinding]:
    execution_binding = _binding(request, lane)
    with storage.transaction() as conn:
        page = select_archive_obsidian_lane_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            execution_binding=execution_binding,
            lane=lane,
        )
        projection = project_archive_obsidian_lane_page_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            execution_binding=execution_binding,
            page=page,
            phase=phase,
            exact_file_reader=exact_file_reader,
        )
    return page, projection, execution_binding


@pytest.mark.parametrize(
    ("lane", "query", "phase"),
    (
        (SearchLane.CATALOG, "Phoenix", ArchiveObsidianReadPhase.BEFORE_MODEL),
        (
            SearchLane.EXACT_IDENTITY,
            "Legacy Codename",
            ArchiveObsidianReadPhase.BEFORE_PUBLICATION,
        ),
        (
            SearchLane.APPROXIMATE_IDENTITY,
            "Projec Phoenix",
            ArchiveObsidianReadPhase.BEFORE_MODEL,
        ),
    ),
)
def test_navigation_lanes_project_only_reauthorized_canonical_candidates(
    storage: Any,
    lane: SearchLane,
    query: str,
    phase: ArchiveObsidianReadPhase,
) -> None:
    seeded = _seed(storage)
    request = _request(query)

    page, projection, execution_binding = _project(
        storage,
        request=request,
        lane=lane,
        phase=phase,
    )

    assert projection.lane is lane
    assert projection.phase is phase
    assert projection.same_evidence_as(projection)
    assert len(projection.candidates) == 1
    candidate = projection.candidates[0]
    assert candidate.corpus is ArchiveSearchCorpus.OBSIDIAN
    assert candidate.review_state is ArchiveReviewState.NOT_APPLICABLE
    assert candidate.evidence_authority is ArchiveEvidenceAuthority.NAVIGATION_ONLY
    assert candidate.lifecycle_state is LifecycleState.ACTIVE
    assert candidate.match_channels == (ArchiveMatchChannel(lane.value),)
    assert candidate.passages == ()
    assert candidate.title == "Phoenix" and candidate.filename == "Phoenix.md"

    source = candidate.resolved_source
    assert source.source_ref.authority_scope is AuthorityScope.PRINCIPAL
    assert source.source_ref.tenant_id is None
    assert source.source_ref.principal_id == OWNER
    assert source.source_ref.canonical_object_kind is CanonicalObjectKind.OBSIDIAN_BINDING
    assert source.source_ref.canonical_object_id == seeded["binding"]["id"]
    assert source.representations[0].kind is RepresentationKind.OBSIDIAN_BINDING
    assert source.revisions[0].kind is RevisionKind.OBSIDIAN_REVISION_SHA256
    assert source.revisions[0].value == seeded["revision"]

    expected = page.to_coverage(
        execution_binding=execution_binding,
        tenant_id=TENANT,
        principal_id=OWNER,
        request=request,
        snapshot_discriminator=SNAPSHOT,
    )
    actual = projection.to_coverage(
        execution_binding=execution_binding,
        tenant_id=TENANT,
        principal_id=OWNER,
        request=request,
        snapshot_discriminator=SNAPSHOT,
        phase=phase,
    )
    assert actual is projection.to_coverage(
        execution_binding=execution_binding,
        tenant_id=TENANT,
        principal_id=OWNER,
        request=request,
        snapshot_discriminator=SNAPSHOT,
        phase=phase,
    )
    assert actual.to_payload() == expected.to_payload()


def test_lexical_projection_uses_exact_bytes_and_codepoint_span(storage: Any) -> None:
    body = "Префикс\nCafé Straße и Фиолетовый QNAP лежат здесь.\nХвост"
    seeded = _seed(
        storage,
        path="Infrastructure/QNAP.md",
        title="QNAP",
        aliases=(),
        body=body,
    )
    request = _request("STRASSE")
    reads: list[tuple[str, str, str]] = []

    def exact_reader(vault_id: str, path: str, revision: str, /) -> bytes:
        reads.append((vault_id, path, revision))
        return body.encode("utf-8")

    _page, projection, _binding_value = _project(
        storage,
        request=request,
        lane=SearchLane.LEXICAL,
        phase=ArchiveObsidianReadPhase.BEFORE_PUBLICATION,
        exact_file_reader=exact_reader,
    )

    assert reads == [(seeded["vault_id"], "Infrastructure/QNAP.md", seeded["revision"])]
    assert len(projection.candidates) == 1
    candidate = projection.candidates[0]
    assert candidate.evidence_authority is ArchiveEvidenceAuthority.CANONICAL
    assert candidate.lifecycle_state is LifecycleState.ACTIVE
    assert candidate.match_channels == (ArchiveMatchChannel.LEXICAL,)
    assert len(candidate.passages) == 1
    passage = candidate.passages[0]
    locator = cast(TextSpanLocator, passage.passage_ref.locator)
    assert type(locator) is TextSpanLocator
    assert locator.chunk_index == 0
    assert passage.excerpt == body[locator.start_char : locator.end_char]
    assert "Straße" in passage.excerpt
    assert not any(unicodedata.category(char).startswith("C") for char in passage.excerpt)
    assert passage.passage_ref.passage_index_version == OBSIDIAN_PASSAGE_INDEX_VERSION
    assert passage.passage_ref.source_revision.kind is RevisionKind.OBSIDIAN_REVISION_SHA256
    assert passage.passage_ref.source_revision.value == seeded["revision"]
    assert passage.passage_ref.embedding.compatibility is EmbeddingCompatibility.NOT_APPLICABLE


def test_lexical_projection_maps_hangul_jamo_to_exact_original_codepoints(storage: Any) -> None:
    decomposed_match = "가"
    body = f"앞 {decomposed_match} 뒤"
    _seed(
        storage,
        path="Unicode/Hangul.md",
        title="Hangul",
        aliases=(),
        body=body,
    )
    request = _request("가")

    def exact_reader(_vault_id: str, _path: str, _revision: str, /) -> bytes:
        return body.encode("utf-8")

    _page, projection, _binding_value = _project(
        storage,
        request=request,
        lane=SearchLane.LEXICAL,
        phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
        exact_file_reader=exact_reader,
    )

    passage = projection.candidates[0].passages[0]
    locator = cast(TextSpanLocator, passage.passage_ref.locator)
    assert passage.excerpt == body[locator.start_char : locator.end_char]
    assert decomposed_match in passage.excerpt
    assert "가" not in passage.excerpt


def test_projection_scope_lane_seals_and_process_privacy_fail_closed(storage: Any) -> None:
    seeded = _seed(storage)
    request = _request("Phoenix")
    execution_binding = _binding(request, SearchLane.CATALOG)
    page = _select(
        storage,
        request=request,
        lane=SearchLane.CATALOG,
        execution_binding=execution_binding,
    )

    failures = (
        (FOREIGN_TENANT, OWNER, request, SNAPSHOT, execution_binding, page),
        (TENANT, FOREIGN_OWNER, request, SNAPSHOT, execution_binding, page),
        (TENANT, OWNER, _request("different"), SNAPSHOT, execution_binding, page),
        (TENANT, OWNER, request, "different-snapshot", execution_binding, page),
        (
            TENANT,
            OWNER,
            request,
            SNAPSHOT,
            _binding(request, SearchLane.CATALOG, run="different-run"),
            page,
        ),
        (
            TENANT,
            OWNER,
            request,
            SNAPSHOT,
            _binding(request, SearchLane.EXACT_IDENTITY),
            page,
        ),
        (
            TENANT,
            OWNER,
            request,
            SNAPSHOT,
            SearchExecutionBinding.from_payload(execution_binding.to_payload()),
            page,
        ),
    )
    for tenant, principal, supplied_request, snapshot, binding_value, supplied_page in failures:
        with storage.transaction() as conn, pytest.raises(ArchiveObsidianAdapterError) as error:
            project_archive_obsidian_lane_page_in_transaction(
                conn,
                tenant_id=tenant,
                principal_id=principal,
                request=supplied_request,
                snapshot_discriminator=snapshot,
                execution_binding=binding_value,
                page=supplied_page,
                phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
            )
        assert error.value.__cause__ is None
        assert str(seeded["binding"]["id"]) not in str(error.value)

    tampered = _select(
        storage,
        request=request,
        lane=SearchLane.CATALOG,
        execution_binding=execution_binding,
    )
    object.__setattr__(tampered.hits[0], "lane", SearchLane.LEXICAL)
    with storage.transaction() as conn, pytest.raises(ArchiveObsidianAdapterError):
        project_archive_obsidian_lane_page_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            execution_binding=execution_binding,
            page=tampered,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
        )

    _, projection, _ = _project(
        storage,
        request=request,
        lane=SearchLane.CATALOG,
        phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
    )
    with pytest.raises(ArchiveObsidianAdapterError):
        ArchiveObsidianLaneProjection()
    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
        with pytest.raises(TypeError, match="process-private"):
            operation(projection)
    assert "Phoenix" not in repr(projection)
    object.__setattr__(projection, "_phase", ArchiveObsidianReadPhase.BEFORE_PUBLICATION)
    with pytest.raises(ArchiveObsidianAdapterError):
        _ = projection.candidates


def test_nested_same_json_types_in_projection_fail_exact_graph_validation(storage: Any) -> None:
    _seed(storage)
    request = _request("Phoenix")

    _page, candidate_projection, _execution_binding = _project(
        storage,
        request=request,
        lane=SearchLane.CATALOG,
        phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
    )
    candidate = candidate_projection.candidates[0]
    candidate_json = candidate.to_private_json()
    object.__setattr__(
        candidate.matches[0],
        "channel",
        _ForeignArchiveMatchChannel.CATALOG,
    )
    assert candidate.to_private_json() == candidate_json
    assert not candidate_projection.is_valid()
    assert not candidate_projection.same_evidence_as(candidate_projection)
    with pytest.raises(ArchiveObsidianAdapterError):
        _ = candidate_projection.candidates

    _page, coverage_projection, execution_binding = _project(
        storage,
        request=request,
        lane=SearchLane.CATALOG,
        phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
    )
    coverage = coverage_projection.to_coverage(
        execution_binding=execution_binding,
        tenant_id=TENANT,
        principal_id=OWNER,
        request=request,
        snapshot_discriminator=SNAPSHOT,
        phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
    )
    coverage_json = coverage.to_json()
    object.__setattr__(coverage, "returned", _SameJsonCount(coverage.returned))
    assert coverage.to_json() == coverage_json
    assert not coverage_projection.is_valid()
    with pytest.raises(ArchiveObsidianAdapterError):
        coverage_projection.to_coverage(
            execution_binding=execution_binding,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
        )


def test_same_json_storage_hit_type_is_rejected_before_projection(storage: Any) -> None:
    seeded = _seed(storage)
    request = _request("Phoenix")
    execution_binding = _binding(request, SearchLane.CATALOG)
    page = _select(
        storage,
        request=request,
        lane=SearchLane.CATALOG,
        execution_binding=execution_binding,
    )
    original_seal = page._seal
    object.__setattr__(page.hits[0], "title", _SameJsonDifferentDisplay("Phoenix"))
    assert page._seal == original_seal

    with storage.transaction() as conn, pytest.raises(ArchiveObsidianAdapterError) as error:
        project_archive_obsidian_lane_page_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            execution_binding=execution_binding,
            page=page,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
        )
    assert "FORGED TITLE" not in str(error.value)
    assert seeded["body"] not in str(error.value)


def test_factual_hit_primitives_are_captured_before_exact_reader_callback(storage: Any) -> None:
    body = "Фиолетовый QNAP"
    _seed(storage, body=body)
    request = _request("Фиолетовый")
    execution_binding = _binding(request, SearchLane.LEXICAL)

    with storage.transaction() as conn:
        page = select_archive_obsidian_lane_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            execution_binding=execution_binding,
            lane=SearchLane.LEXICAL,
        )

        def mutating_reader(_vault: str, _path: str, _revision: str, /) -> bytes:
            object.__setattr__(
                page.hits[0],
                "title",
                _SameJsonDifferentDisplay("Phoenix"),
            )
            return body.encode("utf-8")

        projection = project_archive_obsidian_lane_page_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=request,
            snapshot_discriminator=SNAPSHOT,
            execution_binding=execution_binding,
            page=page,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
            exact_file_reader=mutating_reader,
        )

    assert projection.candidates[0].title == "Phoenix"
    assert projection.is_valid()


def test_exact_reader_mismatch_navigation_drift_and_unsupported_lane_are_rejected(
    storage: Any,
) -> None:
    body = "Фиолетовый QNAP"
    seeded = _seed(storage, body=body)
    lexical_request = _request("Фиолетовый")
    lexical_binding = _binding(lexical_request, SearchLane.LEXICAL)
    lexical_page = _select(
        storage,
        request=lexical_request,
        lane=SearchLane.LEXICAL,
        execution_binding=lexical_binding,
    )
    for reader in (None, lambda _vault, _path, _revision: b"wrong exact bytes"):
        with storage.transaction() as conn, pytest.raises(ArchiveObsidianAdapterError) as error:
            project_archive_obsidian_lane_page_in_transaction(
                conn,
                tenant_id=TENANT,
                principal_id=OWNER,
                request=lexical_request,
                snapshot_discriminator=SNAPSHOT,
                execution_binding=lexical_binding,
                page=lexical_page,
                phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
                exact_file_reader=reader,
            )
        assert body not in str(error.value)

    navigation_request = _request("Phoenix")
    navigation_binding = _binding(navigation_request, SearchLane.CATALOG)
    navigation_page = _select(
        storage,
        request=navigation_request,
        lane=SearchLane.CATALOG,
        execution_binding=navigation_binding,
    )
    storage.upsert_obsidian_note_binding(
        OWNER,
        vault_id=seeded["vault_id"],
        integration_id="archive-adapter-note",
        current_path="Projects/Renamed.md",
        current_revision=hashlib.sha256(b"renamed revision").hexdigest(),
        origin="user",
        expected_current_revision=seeded["revision"],
    )
    with storage.transaction() as conn, pytest.raises(ArchiveObsidianAdapterError):
        project_archive_obsidian_lane_page_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=navigation_request,
            snapshot_discriminator=SNAPSHOT,
            execution_binding=navigation_binding,
            page=navigation_page,
            phase=ArchiveObsidianReadPhase.BEFORE_PUBLICATION,
        )

    dense_request = _request("anything")
    dense_binding = _binding(dense_request, SearchLane.DENSE)
    dense_page = _select(
        storage,
        request=dense_request,
        lane=SearchLane.DENSE,
        execution_binding=dense_binding,
    )
    with storage.transaction() as conn, pytest.raises(ArchiveObsidianAdapterError):
        project_archive_obsidian_lane_page_in_transaction(
            conn,
            tenant_id=TENANT,
            principal_id=OWNER,
            request=dense_request,
            snapshot_discriminator=SNAPSHOT,
            execution_binding=dense_binding,
            page=dense_page,
            phase=ArchiveObsidianReadPhase.BEFORE_MODEL,
        )
