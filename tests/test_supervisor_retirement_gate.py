from __future__ import annotations

import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from friday.orchestration import supervisor_retirement_repository as retirement_repository
from friday.orchestration.supervisor_contracts import TaskClass, canonical_dumps
from friday.orchestration.supervisor_retirement_gate import (
    AcceptedSourceRetirementEvidence,
    AcceptedSourceRollbackWitness,
    RetirementEvidenceAuthority,
    RetirementGateError,
    RetirementGateReason,
    RetirementRollbackAuthority,
    accept_source_retirement_evidence,
    accept_source_rollback_witness,
    accepted_source_evidence_is_current,
    accepted_source_rollback_is_current,
    evaluate_heuristic_retirement,
)
from friday.orchestration.supervisor_retirement_repository import (
    SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
    RepositoryRetirementSurface,
    RetirementInventoryReason,
    RetirementRepositoryError,
    RetirementSurfaceClass,
    accept_repository_retirement_candidate,
    accepted_repository_assessment_is_current,
    accepted_repository_candidate_is_current,
    accepted_repository_file_is_current,
    accepted_repository_surface_is_current,
    assess_repository_retirement_inventory,
    inspect_repository_retirement_surface,
    registered_retirement_surfaces,
)

ROOT = Path(__file__).resolve().parents[1]

_CURRENT_FILE_SOURCE = """
def current_file_web_request_is_admitted(value: object) -> bool:
    return bool(value)
""".lstrip()

_AGENT_SOURCE = """
def _requires_outward_intent_arbiter(message: str) -> bool:
    return bool(message)


class AgentRuntime:
    async def _attachment_web_query_by_arbiter(
        self,
        message: str,
        *,
        previous_turn: str = "",
        context: object | None = None,
    ) -> tuple[str, str | None]:
        return await self._web_query_by_arbiter(message, previous_turn=previous_turn)

    async def _web_query_by_arbiter(
        self,
        message: str,
        *,
        previous_turn: str = "",
    ) -> tuple[str, str | None]:
        return message, previous_turn or None
""".lstrip()


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_inventory_sources(
    repository: Path,
    *,
    current_file_source: str = _CURRENT_FILE_SOURCE,
    agent_source: str = _AGENT_SOURCE,
    registry_sha256: str = SUPERVISOR_RETIREMENT_REGISTRY_SHA256,
    registry_payload: tuple[dict[str, str], ...] | None = None,
) -> None:
    current_file = repository / "friday/orchestration/current_file_web_comparison.py"
    agent_runtime = repository / "friday/agent_runtime/__init__.py"
    current_file.parent.mkdir(parents=True, exist_ok=True)
    agent_runtime.parent.mkdir(parents=True, exist_ok=True)
    current_file.write_text(current_file_source, encoding="utf-8")
    agent_runtime.write_text(agent_source, encoding="utf-8")
    registry = repository / "friday/orchestration/supervisor_retirement_repository.py"
    if registry_payload is None:
        registry_payload = retirement_repository._REVIEWED_RETIREMENT_REGISTRY_PAYLOAD
    registry.write_text(
        f"_REVIEWED_RETIREMENT_REGISTRY_PAYLOAD = {registry_payload!r}\n"
        f'SUPERVISOR_RETIREMENT_REGISTRY_SHA256 = "{registry_sha256}"\n',
        encoding="utf-8",
    )


@pytest.fixture
def inventory_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "retirement-tests@example.invalid")
    _git(repository, "config", "user.name", "Retirement Tests")
    _write_inventory_sources(repository)
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "inventory")
    return repository, _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def current_inventory_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "current-inventory"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "retirement-tests@example.invalid")
    _git(repository, "config", "user.name", "Retirement Tests")
    source_paths = {surface.source_path for surface in registered_retirement_surfaces()} | {
        "friday/orchestration/supervisor_retirement_repository.py"
    }
    for source_path in source_paths:
        target = repository / source_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / source_path).read_bytes())
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "current inventory")
    return repository, _git(repository, "rev-parse", "HEAD")


def _semantic_predecessor_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, RepositoryRetirementSurface, str]:
    descriptor = RepositoryRetirementSurface(
        candidate_id="fixture.semantic_branch",
        journey=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        surface_class=RetirementSurfaceClass.SEMANTIC_HEURISTIC,
        source_path="friday/orchestration/current_file_web_comparison.py",
        qualified_symbol="current_file_web_request_is_admitted",
    )
    surfaces = (descriptor,)
    registry_sha256 = retirement_repository._retirement_registry_sha256(surfaces)
    monkeypatch.setattr(retirement_repository, "_REGISTERED_SURFACES", surfaces)
    monkeypatch.setattr(
        retirement_repository,
        "_REVIEWED_RETIREMENT_REGISTRY_PAYLOAD",
        tuple(surface.payload() for surface in surfaces),
    )
    monkeypatch.setattr(
        retirement_repository,
        "_SURFACE_BY_ID",
        {descriptor.candidate_id: descriptor},
    )
    monkeypatch.setattr(
        retirement_repository,
        "SUPERVISOR_RETIREMENT_REGISTRY_SHA256",
        registry_sha256,
    )

    repository = tmp_path / "candidate-repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.email", "retirement-tests@example.invalid")
    _git(repository, "config", "user.name", "Retirement Tests")
    _write_inventory_sources(repository, registry_sha256=registry_sha256)
    support = {
        "friday/semantic_supervisor_policy.py": "POLICY = 'closed'\n",
        "friday/orchestration/capability_manifest.py": "MANIFEST = ('read',)\n",
        "friday/orchestration/capability_binding.py": "BINDINGS = ('adapter',)\n",
        "outer_sol/GPT_OSS_SEMANTIC_SUPERVISOR_ROUTING_INVARIANT_AUDIT.md": "before\n",
        "outer_sol/PROJECT_BACKLOG.md": "before\n",
    }
    for relative_path, content in support.items():
        target = repository / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "predecessor")
    return repository, descriptor, _git(repository, "rev-parse", "HEAD")


def _commit_semantic_deletion(
    repository: Path,
    descriptor: RepositoryRetirementSurface,
    *,
    replacement_source: str = "UNCHANGED_GUARDS = True\n",
) -> str:
    (repository / descriptor.source_path).write_text(replacement_source, encoding="utf-8")
    (repository / "outer_sol/GPT_OSS_SEMANTIC_SUPERVISOR_ROUTING_INVARIANT_AUDIT.md").write_text(
        "retirement candidate documented\n",
        encoding="utf-8",
    )
    (repository / "outer_sol/PROJECT_BACKLOG.md").write_text(
        "retirement candidate recorded\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "delete semantic branch")
    return _git(repository, "rev-parse", "HEAD")


def test_current_inventory_has_no_eligible_candidate_and_protects_exact_surfaces(
    current_inventory_repository: tuple[Path, str],
) -> None:
    repository, head = current_inventory_repository

    assessment = assess_repository_retirement_inventory(repository, source_commit=head)

    assert assessment.reason is RetirementInventoryReason.NO_ELIGIBLE_CANDIDATE
    assert assessment.eligible_candidate_ids == ()
    assert assessment.retirement_authorized is False
    assert {
        surface.descriptor.candidate_id: surface.descriptor.surface_class for surface in assessment.surfaces
    } == {
        "current_file_web.request_preflight": RetirementSurfaceClass.DETERMINISTIC_INVARIANT,
        "legacy.absolute_reminder_outward_intent_guard": (RetirementSurfaceClass.DETERMINISTIC_INVARIANT),
        "legacy.attachment_web_query_arbiter": RetirementSurfaceClass.LEGACY_MIXED,
        "legacy.shared_web_query_arbiter": RetirementSurfaceClass.LEGACY_MIXED,
    }
    assert assessment.protected_surface_ids == tuple(
        surface.descriptor.candidate_id for surface in assessment.surfaces
    )
    assert all(surface.source_file.source_commit == head for surface in assessment.surfaces)
    assert all(accepted_repository_surface_is_current(surface) for surface in assessment.surfaces)
    assert all(accepted_repository_file_is_current(surface.source_file) for surface in assessment.surfaces)
    assert accepted_repository_assessment_is_current(assessment)
    assert assessment.registry_sha256 == SUPERVISOR_RETIREMENT_REGISTRY_SHA256


def test_inventory_payload_is_body_free_and_repository_relative(
    current_inventory_repository: tuple[Path, str],
) -> None:
    repository, head = current_inventory_repository
    assessment = assess_repository_retirement_inventory(repository, source_commit=head)

    encoded = canonical_dumps(assessment.payload())

    assert str(repository) not in encoded
    assert "async def _web_query_by_arbiter" not in encoded
    assert "return bool(value)" not in encoded
    assert all(not surface.source_file.source_path.startswith("/") for surface in assessment.surfaces)
    assert assessment.canonical_sha256() == assessment.canonical_sha256()


def test_exact_commit_identity_ignores_mutable_worktree(
    inventory_repository: tuple[Path, str],
) -> None:
    repository, commit = inventory_repository
    accepted = assess_repository_retirement_inventory(repository, source_commit=commit)
    before = accepted.canonical_sha256()
    _write_inventory_sources(
        repository,
        current_file_source="this worktree is deliberately not Python\n",
        agent_source="neither is this worktree\n",
    )

    repeated = assess_repository_retirement_inventory(repository, source_commit=commit)

    assert repeated.canonical_sha256() == before
    assert _git(repository, "rev-parse", "HEAD") == commit
    assert _git(repository, "status", "--short")


def test_committed_ast_change_changes_exact_surface_identity(
    inventory_repository: tuple[Path, str],
) -> None:
    repository, first_commit = inventory_repository
    first = inspect_repository_retirement_surface(
        repository,
        source_commit=first_commit,
        candidate_id="current_file_web.request_preflight",
    )
    _write_inventory_sources(
        repository,
        current_file_source=_CURRENT_FILE_SOURCE.replace("return bool(value)", "return value is not None"),
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "change AST")
    second_commit = _git(repository, "rev-parse", "HEAD")

    second = inspect_repository_retirement_surface(
        repository,
        source_commit=second_commit,
        candidate_id="current_file_web.request_preflight",
    )

    assert second.source_file.source_commit != first.source_file.source_commit
    assert second.source_file.blob_oid != first.source_file.blob_oid
    assert second.source_node_sha256 != first.source_node_sha256


def test_target_commit_must_expose_the_reviewed_registry_digest(
    inventory_repository: tuple[Path, str],
) -> None:
    repository, _ = inventory_repository
    registry = repository / "friday/orchestration/supervisor_retirement_repository.py"
    registry.write_text(
        "_REVIEWED_RETIREMENT_REGISTRY_PAYLOAD = "
        f"{retirement_repository._REVIEWED_RETIREMENT_REGISTRY_PAYLOAD!r}\n"
        f'SUPERVISOR_RETIREMENT_REGISTRY_SHA256 = "{"0" * 64}"\n',
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "forge registry marker")

    with pytest.raises(RetirementRepositoryError, match="does not match reviewed code"):
        assess_repository_retirement_inventory(
            repository,
            source_commit=_git(repository, "rev-parse", "HEAD"),
        )


def test_target_commit_registry_marker_cannot_mask_a_different_payload(
    inventory_repository: tuple[Path, str],
) -> None:
    repository, _ = inventory_repository
    registry = repository / "friday/orchestration/supervisor_retirement_repository.py"
    changed_payload = tuple(reversed(retirement_repository._REVIEWED_RETIREMENT_REGISTRY_PAYLOAD))
    registry.write_text(
        f"_REVIEWED_RETIREMENT_REGISTRY_PAYLOAD = {changed_payload!r}\n"
        f'SUPERVISOR_RETIREMENT_REGISTRY_SHA256 = "{SUPERVISOR_RETIREMENT_REGISTRY_SHA256}"\n',
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "mask changed registry payload")

    with pytest.raises(RetirementRepositoryError, match="payload does not match reviewed code"):
        assess_repository_retirement_inventory(
            repository,
            source_commit=_git(repository, "rev-parse", "HEAD"),
        )


def test_runtime_registry_tuple_and_map_cannot_change_without_exact_binding(
    inventory_repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, commit = inventory_repository
    surfaces = tuple(reversed(retirement_repository._REGISTERED_SURFACES))
    monkeypatch.setattr(retirement_repository, "_REGISTERED_SURFACES", surfaces)
    monkeypatch.setattr(
        retirement_repository,
        "_SURFACE_BY_ID",
        {surface.candidate_id: surface for surface in surfaces},
    )

    with pytest.raises(RetirementRepositoryError, match="registry identity mismatch"):
        assess_repository_retirement_inventory(repository, source_commit=commit)


def test_missing_registered_symbol_fails_closed(
    inventory_repository: tuple[Path, str],
) -> None:
    repository, _ = inventory_repository
    missing = _AGENT_SOURCE.replace("    async def _web_query_by_arbiter(", "    async def renamed(")
    _write_inventory_sources(repository, agent_source=missing)
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "remove registered symbol")

    with pytest.raises(RetirementRepositoryError, match="registered repository symbol is absent"):
        assess_repository_retirement_inventory(
            repository,
            source_commit=_git(repository, "rev-parse", "HEAD"),
        )


@pytest.mark.parametrize("source_commit", ["HEAD", "0" * 12, "0" * 40])
def test_inventory_requires_an_existing_exact_full_commit(
    inventory_repository: tuple[Path, str],
    source_commit: str,
) -> None:
    repository, _ = inventory_repository

    with pytest.raises(RetirementRepositoryError):
        assess_repository_retirement_inventory(repository, source_commit=source_commit)


def test_repository_root_must_be_exact_top_level(
    inventory_repository: tuple[Path, str],
) -> None:
    repository, commit = inventory_repository

    with pytest.raises(RetirementRepositoryError, match="exact Git top level"):
        assess_repository_retirement_inventory(
            repository / "friday",
            source_commit=commit,
        )


def test_unreviewed_and_protected_candidate_nominations_fail_closed() -> None:
    head = _git(ROOT, "rev-parse", "HEAD")
    with pytest.raises(RetirementRepositoryError, match="code-owned inventory"):
        accept_repository_retirement_candidate(
            ROOT,
            candidate_id="current_file_web.semantic_route_hint",
            predecessor_commit=head,
            deletion_commit=head,
        )

    for surface in registered_retirement_surfaces():
        with pytest.raises(RetirementRepositoryError, match="surface_is_not_semantic"):
            accept_repository_retirement_candidate(
                ROOT,
                candidate_id=surface.candidate_id,
                predecessor_commit=head,
                deletion_commit=head,
            )


def test_arbitrary_repository_paths_cannot_enter_the_inventory() -> None:
    with pytest.raises(RetirementRepositoryError, match="repository-relative"):
        RepositoryRetirementSurface(
            candidate_id="fake.semantic_branch",
            journey=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
            surface_class=RetirementSurfaceClass.SEMANTIC_HEURISTIC,
            source_path="../friday/agent_runtime/__init__.py",
            qualified_symbol="AgentRuntime._web_query_by_arbiter",
        )


def test_repository_acceptance_seals_detect_tampering(
    inventory_repository: tuple[Path, str],
) -> None:
    repository, head = inventory_repository
    surface = inspect_repository_retirement_surface(
        repository,
        source_commit=head,
        candidate_id="current_file_web.request_preflight",
    )
    assessment = assess_repository_retirement_inventory(repository, source_commit=head)

    with pytest.raises(RetirementRepositoryError, match="not accepted by this process"):
        replace(surface.source_file, _raw=b"forged")
    with pytest.raises(RetirementRepositoryError, match="not accepted by this process"):
        replace(surface, source_node_sha256="0" * 64)
    with pytest.raises(RetirementRepositoryError, match="not accepted by this process"):
        replace(assessment, _process_seal_sha256="0" * 64)


@pytest.mark.parametrize("relocated", [False, True], ids=["rename", "move"])
def test_candidate_rejects_renamed_or_moved_normalized_predecessor_ast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    relocated: bool,
) -> None:
    repository, descriptor, predecessor_commit = _semantic_predecessor_repository(
        tmp_path,
        monkeypatch,
    )
    renamed_source = _CURRENT_FILE_SOURCE.replace(
        "current_file_web_request_is_admitted",
        "renamed_semantic_branch",
    )
    replacement_source = renamed_source
    if relocated:
        replacement_source = "UNCHANGED_GUARDS = True\n"
        moved = repository / "friday/relocated_semantic_branch.py"
        moved.write_text(renamed_source, encoding="utf-8")
    deletion_commit = _commit_semantic_deletion(
        repository,
        descriptor,
        replacement_source=replacement_source,
    )

    with pytest.raises(RetirementRepositoryError, match="normalized predecessor AST remains"):
        accept_repository_retirement_candidate(
            repository,
            candidate_id=descriptor.candidate_id,
            predecessor_commit=predecessor_commit,
            deletion_commit=deletion_commit,
        )


def test_candidate_deletion_scan_fails_closed_at_file_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, descriptor, predecessor_commit = _semantic_predecessor_repository(
        tmp_path,
        monkeypatch,
    )
    deletion_commit = _commit_semantic_deletion(repository, descriptor)
    monkeypatch.setattr(retirement_repository, "_MAX_DELETION_SCAN_FILES", 1)

    with pytest.raises(RetirementRepositoryError, match="file budget"):
        accept_repository_retirement_candidate(
            repository,
            candidate_id=descriptor.candidate_id,
            predecessor_commit=predecessor_commit,
            deletion_commit=deletion_commit,
        )


def test_real_exact_git_tree_fits_streaming_deletion_scan_budget() -> None:
    head = _git(ROOT, "rev-parse", "HEAD")
    reader = retirement_repository._RepositoryReader(ROOT)

    receipt = retirement_repository._require_normalized_predecessor_absent(
        reader,
        deletion_commit=head,
        normalized_predecessor_sha256="0" * 64,
    )

    listing = _git(ROOT, "ls-tree", "-r", "-l", head, "--", "friday").splitlines()
    python_entries = [line.split(maxsplit=4) for line in listing if line.endswith(".py")]
    assert receipt.file_count == len(python_entries)
    assert receipt.byte_count == sum(int(parts[3]) for parts in python_entries)
    assert 1_000_000 < receipt.ast_node_count <= retirement_repository._MAX_DELETION_SCAN_AST_NODES
    assert receipt.file_count <= retirement_repository._MAX_DELETION_SCAN_FILES
    assert receipt.byte_count <= retirement_repository._MAX_DELETION_SCAN_BYTES
    assert reader._files == {}
    assert reader._modules == {}

    encoded = canonical_dumps(receipt.payload())
    assert set(receipt.payload()) == {
        "scope",
        "file_count",
        "byte_count",
        "ast_node_count",
        "files_sha256",
        "source_paths_included",
        "source_bodies_included",
    }
    assert receipt.payload()["source_paths_included"] is False
    assert receipt.payload()["source_bodies_included"] is False
    assert str(ROOT) not in encoded
    assert "def " not in encoded
    assert len(encoded.encode("utf-8")) < 512


def test_current_commit_inventory_is_exact_and_has_no_retirement_candidate() -> None:
    head = _git(ROOT, "rev-parse", "HEAD")

    assessment = assess_repository_retirement_inventory(ROOT, source_commit=head)

    assert accepted_repository_assessment_is_current(assessment)
    assert assessment.reason is RetirementInventoryReason.NO_ELIGIBLE_CANDIDATE
    assert assessment.eligible_candidate_ids == ()
    assert assessment.protected_surface_ids == tuple(
        surface.candidate_id for surface in registered_retirement_surfaces()
    )
    assert assessment.retirement_authorized is False


def test_repository_blob_size_is_preflighted_before_body_capture(
    inventory_repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, head = inventory_repository
    reader = retirement_repository._RepositoryReader(repository)
    reader.commit(head)
    calls: list[tuple[str, ...]] = []
    original_run_git = retirement_repository._run_git

    def tracked_run_git(
        repository_root: Path,
        *arguments: str,
        accepted_returncodes: frozenset[int] = frozenset({0}),
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        return original_run_git(
            repository_root,
            *arguments,
            accepted_returncodes=accepted_returncodes,
        )

    monkeypatch.setattr(retirement_repository, "_run_git", tracked_run_git)
    monkeypatch.setattr(retirement_repository, "_MAX_GIT_OUTPUT_BYTES", 1_024)

    with pytest.raises(RetirementRepositoryError, match="source exceeds its byte budget"):
        reader.file(head, "friday/orchestration/supervisor_retirement_repository.py")

    assert any(arguments[:2] == ("cat-file", "-s") for arguments in calls)
    assert not any(arguments[:2] == ("cat-file", "blob") for arguments in calls)


def test_git_capture_stops_at_the_configured_output_budget(
    inventory_repository: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, head = inventory_repository
    monkeypatch.setattr(retirement_repository, "_MAX_GIT_OUTPUT_BYTES", 64)

    with pytest.raises(RetirementRepositoryError, match="exceeded its byte budget"):
        retirement_repository._run_git(repository, "ls-tree", "-r", head)


def test_candidate_requires_reviewed_registry_identity_at_deletion_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, descriptor, predecessor_commit = _semantic_predecessor_repository(
        tmp_path,
        monkeypatch,
    )
    registry = repository / "friday/orchestration/supervisor_retirement_repository.py"
    registry.write_text(
        "_REVIEWED_RETIREMENT_REGISTRY_PAYLOAD = "
        f"{retirement_repository._REVIEWED_RETIREMENT_REGISTRY_PAYLOAD!r}\n"
        f'SUPERVISOR_RETIREMENT_REGISTRY_SHA256 = "{"0" * 64}"\n',
        encoding="utf-8",
    )
    deletion_commit = _commit_semantic_deletion(repository, descriptor)

    with pytest.raises(RetirementRepositoryError, match="does not match reviewed code"):
        accept_repository_retirement_candidate(
            repository,
            candidate_id=descriptor.candidate_id,
            predecessor_commit=predecessor_commit,
            deletion_commit=deletion_commit,
        )


def test_code_reviewed_semantic_fixture_builds_source_only_candidate_evidence_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, descriptor, predecessor_commit = _semantic_predecessor_repository(
        tmp_path,
        monkeypatch,
    )
    deletion_commit = _commit_semantic_deletion(repository, descriptor)

    candidate = accept_repository_retirement_candidate(
        repository,
        candidate_id=descriptor.candidate_id,
        predecessor_commit=predecessor_commit,
        deletion_commit=deletion_commit,
    )

    assert accepted_repository_candidate_is_current(candidate)
    assert candidate.predecessor_surface.source_node_kind == "FunctionDef"
    assert candidate.deletion_commit == deletion_commit
    assert candidate.registry_sha256 == retirement_repository.SUPERVISOR_RETIREMENT_REGISTRY_SHA256
    assert candidate.deletion_scan_file_count > 0
    with pytest.raises(RetirementRepositoryError, match="not accepted by this process"):
        replace(candidate, deletion_scan_files_sha256="0" * 64)

    evidence_payload = {
        "schema": "friday.supervisor-retirement-source-evidence.v2",
        "evidence_id": "fixture_joined_window",
        "candidate_sha256": candidate.canonical_sha256(),
        "journey": candidate.journey.value,
        "deletion_commit": deletion_commit,
        "shadow_bundle_sha256": "1" * 64,
        "canary_bundle_sha256": "2" * 64,
        "promoted_journey_sha256": "3" * 64,
        "primary_fallback_sha256": "4" * 64,
        "production_trace_set_sha256": "5" * 64,
        "observation_count": 10,
        "joined_trace_count": 10,
        "hidden_owner_count": 0,
        "duplicate_capability_count": 0,
        "duplicate_effect_count": 0,
        "duplicate_publication_count": 0,
        "false_completion_regression_count": 0,
        "user_visible_regression_count": 0,
    }
    evidence_path = "outer_sol/evidence/retirement.json"
    artifact = canonical_dumps(evidence_payload).encode("utf-8")
    artifact_path = repository / evidence_path
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(artifact)
    (repository / descriptor.source_path).write_text(_CURRENT_FILE_SOURCE, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "source rollback and evidence")
    evidence_commit = _git(repository, "rev-parse", "HEAD")

    evidence = accept_source_retirement_evidence(
        repository,
        candidate=candidate,
        evidence_commit=evidence_commit,
        evidence_path=evidence_path,
        expected_file_sha256=hashlib.sha256(artifact).hexdigest(),
    )
    rollback = accept_source_rollback_witness(
        repository,
        candidate=candidate,
        rollback_commit=evidence_commit,
    )
    decision = evaluate_heuristic_retirement(candidate, evidence, rollback)

    assert evidence.authority is RetirementEvidenceAuthority.SOURCE_BOUND_ONLY
    assert rollback.authority is RetirementRollbackAuthority.SOURCE_PREIMAGE_ONLY
    assert accepted_source_evidence_is_current(evidence)
    assert accepted_source_rollback_is_current(rollback)
    assert decision.admitted is False
    assert decision.reason is RetirementGateReason.PRODUCTION_EVIDENCE_REQUIRED


def test_source_evidence_and_rollback_factories_reject_unaccepted_candidates() -> None:
    with pytest.raises(TypeError, match="candidate must be accepted"):
        accept_source_retirement_evidence(
            ROOT,
            candidate=object(),  # type: ignore[arg-type]
            evidence_commit="0" * 40,
            evidence_path="outer_sol/evidence/retirement.json",
            expected_file_sha256="0" * 64,
        )
    with pytest.raises(TypeError, match="candidate must be accepted"):
        accept_source_rollback_witness(
            ROOT,
            candidate=object(),  # type: ignore[arg-type]
            rollback_commit="0" * 40,
        )


def test_source_only_accepted_types_cannot_be_directly_forged(
    inventory_repository: tuple[Path, str],
) -> None:
    repository, head = inventory_repository
    surface = inspect_repository_retirement_surface(
        repository,
        source_commit=head,
        candidate_id="current_file_web.request_preflight",
    )

    with pytest.raises(RetirementGateError, match="not accepted by this process"):
        AcceptedSourceRetirementEvidence(
            evidence_id="forged_evidence",
            candidate_sha256="0" * 64,
            journey=TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
            deletion_commit=head,
            shadow_bundle_sha256="1" * 64,
            canary_bundle_sha256="2" * 64,
            promoted_journey_sha256="3" * 64,
            primary_fallback_sha256="4" * 64,
            production_trace_set_sha256="5" * 64,
            observation_count=1,
            joined_trace_count=1,
            hidden_owner_count=0,
            duplicate_capability_count=0,
            duplicate_effect_count=0,
            duplicate_publication_count=0,
            false_completion_regression_count=0,
            user_visible_regression_count=0,
            repository_artifact=surface.source_file,
            _process_authority=object(),
            _process_seal_sha256="0" * 64,
        )
    with pytest.raises(RetirementGateError, match="not accepted by this process"):
        AcceptedSourceRollbackWitness(
            candidate_sha256="0" * 64,
            rollback_surface=surface,
            deletion_commit=head,
            _process_authority=object(),
            _process_seal_sha256="0" * 64,
        )
    assert accepted_source_evidence_is_current(object()) is False
    assert accepted_source_rollback_is_current(object()) is False


def test_gate_rejects_unaccepted_lookalikes_before_reading_claimed_values() -> None:
    with pytest.raises(TypeError, match="candidate must be accepted"):
        evaluate_heuristic_retirement(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
        )
