from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "outer_sol" / "PROJECT_IMPLEMENTATION_STATUS.md"

READINESS_STATES = frozenset({"READY", "DEGRADED", "UNVERIFIED", "BLOCKED", "OUT_OF_SCOPE"})
EVIDENCE_STATES = frozenset({"VERIFIED", "AVAILABLE", "MISSING", "STALE", "FAILED", "NOT_APPLICABLE"})
EVIDENCE_CLASSES = (
    "deterministic contract",
    "integration path",
    "clean artifact path",
    "synthetic live path",
    "production read-only observation",
    "physical device evidence",
    "restart and recovery evidence",
    "rollback evidence",
    "backup and restore evidence",
)
CURRENT_JOURNEYS = {
    "conversation_recall": (
        "Conversation recall",
        "DEGRADED",
        ("semantic_recall_missing", "cross_lane_coverage_missing"),
    ),
    "document_recall_answer": (
        "Document recall and answer",
        "DEGRADED",
        ("cross_lane_coverage_missing",),
    ),
    "obsidian_write_sync": (
        "Obsidian write and synchronization",
        "UNVERIFIED",
        (
            "physical_android_round_trip_missing",
            "real_conflict_evidence_missing",
        ),
    ),
    "durable_scheduled_work": (
        "Durable scheduled work",
        "UNVERIFIED",
        ("current_code_journey_audit_missing", "at_most_once_delivery_recovery_missing"),
    ),
    "honest_degradation": (
        "Honest degradation",
        "DEGRADED",
        (
            "product_multi_lane_coverage_missing",
            "candidate_bound_fault_continuation_evidence_missing",
        ),
    ),
    "current_file_web_comparison": (
        "Current file and web comparison",
        "UNVERIFIED",
        (
            "assist_promotion_evidence_missing",
            "clean_release_artifact_missing",
            "activation_rollback_evidence_missing",
        ),
    ),
}
ENVIRONMENT_BY_CLASS = {
    "deterministic contract": "deterministic_contract",
    "integration path": "integration",
    "clean artifact path": "clean_artifact",
    "synthetic live path": "synthetic_live",
    "production read-only observation": "production_read_only",
    "physical device evidence": "physical_android",
    "restart and recovery evidence": "restart_recovery",
    "rollback evidence": "rollback",
    "backup and restore evidence": "backup_restore",
}
_CHECK_ID_SUFFIXES_BY_CLASS = {
    "deterministic contract": ("contract_suite",),
    "integration path": ("integration_suite",),
    "clean artifact path": ("installed_surface", "schema_migration", "wheel_reproducibility"),
    "synthetic live path": ("synthetic_live_battery",),
    "production read-only observation": (
        "database_integrity",
        "schema_attestation",
        "service_health",
    ),
    "physical device evidence": ("android_round_trip", "real_conflict_preserved"),
    "restart and recovery evidence": ("cancellation", "expiry", "restart_resume"),
    "rollback evidence": ("activation_rollback",),
    "backup and restore evidence": ("clean_restore",),
}
_PROOF_REFS_BY_JOURNEY_CLASS = {
    ("conversation_recall", "deterministic contract"): (
        "tests/test_message_window_runtime_integration.py::test_promoted_exact_window_is_deterministic_scoped_and_receipted",
    ),
    ("conversation_recall", "integration path"): (
        "tests/test_message_window_runtime_integration.py::test_promoted_exact_window_is_deterministic_scoped_and_receipted",
        "tests/test_archive_search_runtime_publication.py::test_selected_message_archive_evidence_replays_after_restart_then_fails_closed",
    ),
    ("conversation_recall", "restart and recovery evidence"): (
        "tests/test_message_window_work_item_runtime.py::test_restart_temporal_followup_reuses_identity_role_and_zone_with_one_cas_update",
        "tests/test_archive_search_runtime_publication.py::test_selected_message_archive_evidence_replays_after_restart_then_fails_closed",
    ),
    ("document_recall_answer", "deterministic contract"): (
        "tests/test_v12_file_evidence_reader.py::test_current_turn_native_files_form_one_process_owned_bundle",
    ),
    ("document_recall_answer", "integration path"): (
        "tests/test_v12_file_evidence_reader.py::test_reader_contract_matches_real_ingestion_projections",
        "tests/test_archive_search_runtime_publication.py::test_selected_canonical_archive_evidence_replays_exactly_after_runtime_restart",
        "tests/test_archive_search_runtime_publication.py::test_locate_select_and_explain_document_survives_both_runtime_restarts",
    ),
    ("document_recall_answer", "synthetic live path"): (
        "tests/test_document_contour_live_battery.py::test_manifest_is_exactly_ten_unique_document_scenarios",
    ),
    ("document_recall_answer", "restart and recovery evidence"): (
        "tests/test_archive_search_runtime_publication.py::test_selected_canonical_archive_evidence_replays_exactly_after_runtime_restart",
        "tests/test_archive_search_runtime_publication.py::test_locate_select_and_explain_document_survives_both_runtime_restarts",
        "tests/test_archive_search_runtime_publication.py::test_selected_archive_replay_failure_is_source_free_and_suspends",
    ),
    ("obsidian_write_sync", "deterministic contract"): (
        "tests/test_obsidian_structured_acceptance_core.py::test_conflict_preview_is_non_destructive_and_contains_both_versions",
    ),
    ("obsidian_write_sync", "integration path"): (
        "tests/test_agent_obsidian_acceptance_message_matrix.py::test_every_exact_tier_a_b_message_routes_through_full_chat_once",
    ),
    ("obsidian_write_sync", "synthetic live path"): (
        "tests/test_obsidian_syncthing_live.py::test_pinned_syncthing_generates_and_accepts_the_managed_rest_contract",
    ),
    ("obsidian_write_sync", "restart and recovery evidence"): (
        "tests/test_obsidian_runtime.py::test_resume_reuses_daily_operation_identity_without_duplicate_text",
    ),
    ("durable_scheduled_work", "deterministic contract"): (
        "tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_tool_is_removed_so_nobody_is_woken_twice",
    ),
    ("durable_scheduled_work", "integration path"): (
        "tests/test_a_reminder_is_set_before_the_model_speaks.py::test_the_reminder_is_set_without_asking_the_model",
    ),
    ("durable_scheduled_work", "synthetic live path"): (
        "tests/test_synthetic_live_battery.py::test_exact_reminder_oracle_owns_the_model_boundary",
    ),
    ("durable_scheduled_work", "restart and recovery evidence"): (
        "tests/test_mission_budgets_and_recovery.py::test_spent_budget_survives_a_restart",
    ),
    ("honest_degradation", "deterministic contract"): (
        "tests/test_search_provider_refusal_is_not_emptiness.py::test_202_from_duckduckgo_is_a_refusal_not_an_empty_result",
    ),
    ("honest_degradation", "integration path"): (
        "tests/test_search_provider_refusal_is_not_emptiness.py::test_the_chain_moves_on_when_the_first_provider_refuses",
    ),
    ("honest_degradation", "synthetic live path"): (
        "tests/test_synthetic_live_battery.py::test_full_package_a_oracle_accepts_natural_honest_refusals",
    ),
    ("honest_degradation", "restart and recovery evidence"): (
        "tests/test_message_window_work_item_runtime.py::test_post_boundary_admission_race_returns_atomic_clarification_without_execution",
    ),
    ("current_file_web_comparison", "deterministic contract"): (
        "tests/test_compare_current_file_web_work_graph_schema45.py::test_schema45_exact_binding_is_durable_immutable_and_revision_cas",
    ),
    ("current_file_web_comparison", "integration path"): (
        "tests/test_supervisor_assist_controller.py::test_review_and_web_recovery_are_strictly_bounded",
    ),
    ("current_file_web_comparison", "restart and recovery evidence"): (
        "tests/test_supervisor_assist_graph_adapter.py::test_terminal_cancel_and_startup_reconcile_publish_closed_receipts",
    ),
}
_CURRENT_SNAPSHOT_MISSING_CLASSES = (
    "clean artifact path",
    "rollback evidence",
    "backup and restore evidence",
)
_GENERIC_OPERATOR_REFS = frozenset(
    {
        "tools/immutable_release_operator.py",
        "tests/test_immutable_release_operator.py::test_installed_surface_smoke_uses_one_hermetic_environment_and_cleans_it",
        "tests/test_immutable_release_operator.py::test_backend_start_uncertainty_never_restores_backup_or_runs_schema33",
        "tests/test_immutable_release_operator.py::test_obsidian_root_is_restored_exactly_with_database_and_inbox",
        "tests/test_storage_and_lifecycle.py::test_verified_backup_restore_is_atomic_and_creates_safety_copy",
    }
)

_MANIFEST_FIELDS = frozenset({"$schema", "journey_id", "evidence_class", "result", "release", "observation"})
_RELEASE_FIELDS = frozenset({"source_commit", "tree_sha256", "wheel_sha256", "database_schema"})
_OBSERVATION_FIELDS = frozenset(
    {
        "environment",
        "observed_at_utc",
        "check_ids",
        "artifact_ref",
        "artifact_schema",
        "artifact_sha256",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "$schema",
        "journey_id",
        "evidence_class",
        "result",
        "environment",
        "observed_at_utc",
        "check_ids",
        "release",
        "proofs",
    }
)
_PROOF_FIELDS = frozenset({"runner", "test_ref", "test_source_sha256", "outcome"})
_MANIFEST_SCHEMA = "friday.golden-journey-evidence.v1"
_RECEIPT_SCHEMA = "friday.golden-journey-sanitized-receipt.v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")
_CHECK_ID = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,127}")
_JOURNEY_ID = re.compile(r"[a-z][a-z0-9_]{1,63}")
_LIMITATION_CODE = re.compile(r"[a-z][a-z0-9_]{1,95}")
_TEST_NAME = re.compile(r"test_[A-Za-z0-9_]{1,159}")
_GIT_PATH = re.compile(r"[A-Za-z0-9_./-]{1,240}")
_UTC_INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)")


class RegistryValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    source_commit: str
    tree_sha256: str
    wheel_sha256: str
    database_schema: int


@dataclass(frozen=True, slots=True)
class RepositoryLink:
    label: str
    target: str


@dataclass(frozen=True, slots=True)
class EvidenceClaim:
    state: str
    refs: tuple[RepositoryLink, ...]


@dataclass(frozen=True, slots=True)
class JourneyRow:
    journey_id: str
    journey: str
    readiness: str
    evidence: dict[str, EvidenceClaim]
    limitations: tuple[str, ...]


def _closed_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryValidationError("evidence JSON contains a duplicate key")
        result[key] = value
    return result


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@cache
def _tracked_repo_paths() -> frozenset[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise RegistryValidationError("repository tracked-file inventory is unavailable")
    try:
        paths = completed.stdout.decode("utf-8", errors="strict").split("\0")
    except UnicodeDecodeError as exc:
        raise RegistryValidationError("repository tracked-file inventory is not UTF-8") from exc
    return frozenset(path for path in paths if path)


@cache
def _exact_git_blob(source_commit: str, path_text: str) -> bytes:
    path = PurePosixPath(path_text)
    if (
        re.fullmatch(r"[0-9a-f]{40}", source_commit) is None
        or _GIT_PATH.fullmatch(path_text) is None
        or path.is_absolute()
        or str(path) != path_text
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RegistryValidationError("exact-release git object identity is malformed")
    object_name = f"{source_commit}:{path_text}"
    object_type = subprocess.run(
        ["git", "--no-replace-objects", "cat-file", "-t", object_name],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if object_type.returncode != 0 or object_type.stdout != b"blob\n":
        raise RegistryValidationError("proof source is missing or not a file at the manifest commit")
    blob = subprocess.run(
        ["git", "--no-replace-objects", "cat-file", "blob", object_name],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    if blob.returncode != 0:
        raise RegistryValidationError("proof source bytes are unavailable at the manifest commit")
    return blob.stdout


def _release_identity(markdown: str) -> ReleaseIdentity:
    commit = re.search(r"Deployed implementation head: `([0-9a-f]{40})`", markdown)
    live = re.search(
        r"- Live: Friday [^\n]* / `([0-9a-f]{40})`;\s*"
        r"tree `([0-9a-f]{64})`;\s*wheel `([0-9a-f]{64})`",
        markdown,
        flags=re.DOTALL,
    )
    schema = re.search(r"- Database schema: ([0-9]+)", markdown)
    if commit is None or live is None or schema is None:
        raise RegistryValidationError("canonical current release identity is incomplete")
    if commit.group(1) != live.group(1):
        raise RegistryValidationError("deployed head and live commit identity diverge")
    return ReleaseIdentity(commit.group(1), live.group(2), live.group(3), int(schema.group(1)))


def _parse_claim(cell: str) -> EvidenceClaim:
    states = tuple(token for token in re.findall(r"`([^`]+)`", cell) if token in EVIDENCE_STATES)
    if len(states) != 1 or not cell.startswith(f"`{states[0]}`"):
        raise RegistryValidationError("evidence state is outside the closed vocabulary")
    refs = tuple(RepositoryLink(label, target) for label, target in _LINK.findall(cell))
    expected_cell = f"`{states[0]}`" + "".join(f"<br>[{ref.label}]({ref.target})" for ref in refs)
    if cell != expected_cell:
        raise RegistryValidationError("evidence cell contains non-canonical text or markup")
    return EvidenceClaim(states[0], refs)


def _parse_limitations(cell: str) -> tuple[str, ...]:
    limitations = tuple(re.findall(r"`([^`]+)`", cell))
    if (
        not limitations
        or any(_LIMITATION_CODE.fullmatch(code) is None for code in limitations)
        or len(set(limitations)) != len(limitations)
        or cell != "<br>".join(f"`{code}`" for code in limitations)
    ):
        raise RegistryValidationError("limitation cell must contain only unique closed codes")
    return limitations


def _registry_rows(markdown: str) -> tuple[JourneyRow, ...]:
    heading = "## Canonical golden-journey/evidence registry"
    start = markdown.find(heading)
    if start < 0:
        raise RegistryValidationError("canonical registry heading is missing")
    section = markdown[start + len(heading) :]
    following_heading = section.find("\n## ")
    if following_heading >= 0:
        section = section[:following_heading]
    lines = [line for line in section.splitlines() if line.startswith("|")]
    if len(lines) != 8:
        raise RegistryValidationError("registry must have one header, one divider and six journeys")
    header = tuple(cell.strip() for cell in lines[0].strip("|").split("|"))
    expected_header = (
        "Journey ID",
        "Journey",
        "Readiness",
        *EVIDENCE_CLASSES,
        "Limitation codes",
    )
    if header != expected_header:
        raise RegistryValidationError("registry columns do not match the closed contract")
    if any(set(cell.strip()) - {"-", ":"} for cell in lines[1].strip("|").split("|")):
        raise RegistryValidationError("registry divider is malformed")

    rows: list[JourneyRow] = []
    for line in lines[2:]:
        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if len(cells) != len(expected_header):
            raise RegistryValidationError("registry row width does not match its header")
        id_tokens = re.findall(r"`([^`]+)`", cells[0])
        if (
            len(id_tokens) != 1
            or cells[0] != f"`{id_tokens[0]}`"
            or _JOURNEY_ID.fullmatch(id_tokens[0]) is None
        ):
            raise RegistryValidationError("journey id is not one closed stable identifier")
        if not cells[1] or any(marker in cells[1] for marker in ("`", "[", "]", "<", ">")):
            raise RegistryValidationError("journey display title is not plain bounded text")
        readiness_tokens = re.findall(r"`([^`]+)`", cells[2])
        if (
            len(readiness_tokens) != 1
            or cells[2] != f"`{readiness_tokens[0]}`"
            or readiness_tokens[0] not in READINESS_STATES
        ):
            raise RegistryValidationError("journey readiness is outside the closed vocabulary")
        evidence = {
            evidence_class: _parse_claim(cell)
            for evidence_class, cell in zip(EVIDENCE_CLASSES, cells[3:-1], strict=True)
        }
        rows.append(
            JourneyRow(
                journey_id=id_tokens[0],
                journey=cells[1],
                readiness=readiness_tokens[0],
                evidence=evidence,
                limitations=_parse_limitations(cells[-1]),
            )
        )
    return tuple(rows)


def _safe_repo_path(path_text: str, *, root: Path) -> Path:
    if len(path_text) > 300 or "\\" in path_text:
        raise RegistryValidationError("repository reference is not bounded canonical syntax")
    path = PurePosixPath(path_text)
    if (
        path.is_absolute()
        or str(path) != path_text
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[0] not in {"friday", "tools", "tests", "docs", "outer_sol", "evidence"}
    ):
        raise RegistryValidationError("reference is not safe and repository-relative")
    candidate = root / path_text
    if (
        candidate.is_symlink()
        or not candidate.is_file()
        or not candidate.resolve().is_relative_to(root.resolve())
    ):
        raise RegistryValidationError("reference does not name an existing repository file")
    if root.resolve() == ROOT.resolve() and path_text not in _tracked_repo_paths():
        raise RegistryValidationError("repository evidence reference is not checked in")
    return candidate


def _safe_repo_link(ref: RepositoryLink) -> tuple[Path, str | None]:
    if ref.label.count("::") > 1:
        raise RegistryValidationError("Markdown reference label is not canonical")
    path_text, separator, _locator = ref.label.partition("::")
    if ref.target != f"../{path_text}":
        raise RegistryValidationError("Markdown target must resolve its exact canonical label")
    candidate = _safe_repo_path(path_text, root=ROOT)
    rendered_target = (STATUS_PATH.parent / ref.target).resolve()
    if rendered_target != candidate.resolve():
        raise RegistryValidationError("Markdown target escapes or differs from its repository label")
    if separator:
        return _safe_test_ref(ref.label, root=ROOT)
    return candidate, None


def _safe_test_ref(test_ref: str, *, root: Path) -> tuple[Path, str]:
    if test_ref.count("::") != 1:
        raise RegistryValidationError("proof reference is not one executable test node id")
    path_text, locator = test_ref.split("::", maxsplit=1)
    if not path_text.startswith("tests/test_") or _TEST_NAME.fullmatch(locator) is None:
        raise RegistryValidationError("test reference is not an executable top-level node id")
    candidate = _safe_repo_path(path_text, root=root)
    module = ast.parse(candidate.read_text(encoding="utf-8"), filename=path_text)
    test_names = {
        node.name for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if locator not in test_names:
        raise RegistryValidationError("referenced executable test does not exist")
    return candidate, locator


def _exact_git_test_source(test_ref: str, *, source_commit: str) -> bytes:
    if test_ref.count("::") != 1:
        raise RegistryValidationError("proof reference is not one executable test node id")
    path_text, locator = test_ref.split("::", maxsplit=1)
    if not path_text.startswith("tests/test_") or _TEST_NAME.fullmatch(locator) is None:
        raise RegistryValidationError("proof reference is not a canonical pytest node id")
    raw = _exact_git_blob(source_commit, path_text)
    try:
        source = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RegistryValidationError("proof source at the manifest commit is not UTF-8") from exc
    module = ast.parse(source, filename=f"{source_commit}:{path_text}")
    test_names = {
        node.name for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if locator not in test_names:
        raise RegistryValidationError("proof node id is absent at the manifest source commit")
    return raw


def _expected_check_ids(journey_id: str, evidence_class: str) -> list[str]:
    if journey_id not in CURRENT_JOURNEYS or evidence_class not in EVIDENCE_CLASSES:
        raise RegistryValidationError("manifest identity is outside the closed journey registry")
    return sorted(f"{journey_id}.{suffix}" for suffix in _CHECK_ID_SUFFIXES_BY_CLASS[evidence_class])


def _release_payload(identity: ReleaseIdentity) -> dict[str, object]:
    return {
        "database_schema": identity.database_schema,
        "source_commit": identity.source_commit,
        "tree_sha256": identity.tree_sha256,
        "wheel_sha256": identity.wheel_sha256,
    }


def _release_binding(identity: ReleaseIdentity) -> str:
    return hashlib.sha256(_canonical_json_bytes(_release_payload(identity))).hexdigest()


def _expected_artifact_ref(
    journey_id: str,
    evidence_class: str,
    result: str,
    identity: ReleaseIdentity,
) -> str:
    if journey_id not in CURRENT_JOURNEYS or evidence_class not in EVIDENCE_CLASSES:
        raise RegistryValidationError("artifact identity is outside the closed journey registry")
    if result not in {"VERIFIED", "FAILED"}:
        raise RegistryValidationError("artifact result is outside the closed evidence contract")
    return (
        "evidence/golden_journeys/receipts/"
        f"{journey_id}--{ENVIRONMENT_BY_CLASS[evidence_class]}--{result.lower()}--"
        f"{_release_binding(identity)}.json"
    )


def _expected_manifest_ref(
    journey_id: str,
    evidence_class: str,
    result: str,
    identity: ReleaseIdentity,
) -> str:
    if journey_id not in CURRENT_JOURNEYS or evidence_class not in EVIDENCE_CLASSES:
        raise RegistryValidationError("manifest identity is outside the closed journey registry")
    if result not in {"VERIFIED", "FAILED"}:
        raise RegistryValidationError("manifest result is outside the closed evidence contract")
    return (
        "evidence/golden_journeys/manifests/"
        f"{journey_id}--{ENVIRONMENT_BY_CLASS[evidence_class]}--{result.lower()}--"
        f"{_release_binding(identity)}.json"
    )


def _validate_canonical_utc(value: object) -> None:
    if type(value) is not str or _UTC_INSTANT.fullmatch(value) is None:
        raise RegistryValidationError("evidence time is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistryValidationError("evidence time is not an offset-aware UTC instant") from exc
    utc_offset = parsed.utcoffset()
    if utc_offset is None or utc_offset.total_seconds() != 0:
        raise RegistryValidationError("evidence time is not an offset-aware UTC instant")


def _expected_executable_proofs(
    *,
    journey_id: str,
    evidence_class: str,
    expected_result: str,
    manifest_identity: ReleaseIdentity,
) -> list[dict[str, str]]:
    expected_refs = _PROOF_REFS_BY_JOURNEY_CLASS.get((journey_id, evidence_class), ())
    if not expected_refs:
        raise RegistryValidationError("journey class has no closed executable proof inventory")
    if any(test_ref in _GENERIC_OPERATOR_REFS for test_ref in expected_refs):
        raise RegistryValidationError("generic operator checks are not journey executable proof")
    expected_outcome = "PASSED" if expected_result == "VERIFIED" else "FAILED"
    result: list[dict[str, str]] = []
    for expected_ref in expected_refs:
        test_source = _exact_git_test_source(
            expected_ref,
            source_commit=manifest_identity.source_commit,
        )
        result.append(
            {
                "outcome": expected_outcome,
                "runner": "pytest",
                "test_ref": expected_ref,
                "test_source_sha256": hashlib.sha256(test_source).hexdigest(),
            }
        )
    return result


def _validate_executable_proofs(
    proofs: object,
    *,
    journey_id: str,
    evidence_class: str,
    expected_result: str,
    manifest_identity: ReleaseIdentity,
) -> list[dict[str, str]]:
    expected = _expected_executable_proofs(
        journey_id=journey_id,
        evidence_class=evidence_class,
        expected_result=expected_result,
        manifest_identity=manifest_identity,
    )
    if (
        type(proofs) is not list
        or any(not isinstance(proof, dict) or frozenset(proof) != _PROOF_FIELDS for proof in proofs)
        or proofs != expected
    ):
        raise RegistryValidationError("executable proof is not bound to exact test bytes and node id")
    return expected


def _validate_sanitized_receipt(
    observation: dict[str, Any],
    *,
    journey_id: str,
    evidence_class: str,
    expected_result: str,
    manifest_identity: ReleaseIdentity,
    repo_root: Path,
) -> None:
    artifact_ref = observation.get("artifact_ref")
    artifact_schema = observation.get("artifact_schema")
    if (
        type(artifact_ref) is not str
        or type(artifact_schema) is not str
        or artifact_schema != _RECEIPT_SCHEMA
    ):
        raise RegistryValidationError("manifest does not bind a canonical sanitized receipt")
    expected_artifact_ref = _expected_artifact_ref(
        journey_id,
        evidence_class,
        expected_result,
        manifest_identity,
    )
    if artifact_ref != expected_artifact_ref:
        raise RegistryValidationError("sanitized receipt path is not the deterministic closed artifact_ref")
    artifact_path = _safe_repo_path(artifact_ref, root=repo_root)
    raw = artifact_path.read_bytes()
    if not raw or len(raw) > 65_536:
        raise RegistryValidationError("sanitized receipt exceeds its public size bound")
    if hashlib.sha256(raw).hexdigest() != observation.get("artifact_sha256"):
        raise RegistryValidationError("sanitized receipt digest does not match its actual bytes")
    try:
        receipt = json.loads(raw, object_pairs_hook=_closed_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryValidationError("sanitized receipt is not closed JSON") from exc
    if raw != _canonical_json_bytes(receipt):
        raise RegistryValidationError("sanitized receipt is not canonical JSON")
    if not isinstance(receipt, dict) or frozenset(receipt) != _RECEIPT_FIELDS:
        raise RegistryValidationError("sanitized receipt violates the privacy allowlist")
    expected_proofs = _validate_executable_proofs(
        receipt.get("proofs"),
        journey_id=journey_id,
        evidence_class=evidence_class,
        expected_result=expected_result,
        manifest_identity=manifest_identity,
    )
    expected_receipt = {
        "$schema": artifact_schema,
        "check_ids": _expected_check_ids(journey_id, evidence_class),
        "environment": ENVIRONMENT_BY_CLASS[evidence_class],
        "evidence_class": evidence_class,
        "journey_id": journey_id,
        "observed_at_utc": observation.get("observed_at_utc"),
        "proofs": expected_proofs,
        "release": _release_payload(manifest_identity),
        "result": expected_result,
    }
    if receipt != expected_receipt:
        raise RegistryValidationError("sanitized receipt does not match the manifest's closed claim")


def _validate_manifest_payload(
    payload: object,
    *,
    manifest_ref: str,
    journey_id: str,
    evidence_class: str,
    current: ReleaseIdentity,
    require_current: bool,
    repo_root: Path,
    expected_result: str = "VERIFIED",
) -> NoReturn:
    if (
        journey_id not in CURRENT_JOURNEYS
        or evidence_class not in EVIDENCE_CLASSES
        or expected_result not in {"VERIFIED", "FAILED"}
    ):
        raise RegistryValidationError("manifest identity is outside the closed journey registry")
    if not isinstance(payload, dict) or frozenset(payload) != _MANIFEST_FIELDS:
        raise RegistryValidationError("evidence manifest violates the top-level privacy allowlist")
    release = payload.get("release")
    observation = payload.get("observation")
    if not isinstance(release, dict) or frozenset(release) != _RELEASE_FIELDS:
        raise RegistryValidationError("evidence manifest release binding is not closed")
    if not isinstance(observation, dict) or frozenset(observation) != _OBSERVATION_FIELDS:
        raise RegistryValidationError("evidence manifest observation violates the privacy allowlist")
    expected_check_ids = _expected_check_ids(journey_id, evidence_class)
    check_ids = observation.get("check_ids")
    if (
        payload.get("$schema") != _MANIFEST_SCHEMA
        or payload.get("journey_id") != journey_id
        or payload.get("evidence_class") != evidence_class
        or payload.get("result") != expected_result
        or observation.get("environment") != ENVIRONMENT_BY_CLASS[evidence_class]
        or check_ids != expected_check_ids
        or any(_CHECK_ID.fullmatch(item) is None for item in expected_check_ids)
        or _SHA256.fullmatch(str(observation.get("artifact_sha256"))) is None
    ):
        raise RegistryValidationError("evidence manifest does not attest the exact journey class")
    _validate_canonical_utc(observation.get("observed_at_utc"))
    raw_database_schema = release.get("database_schema")
    database_schema = raw_database_schema if type(raw_database_schema) is int else -1
    manifest_identity = ReleaseIdentity(
        source_commit=str(release.get("source_commit")),
        tree_sha256=str(release.get("tree_sha256")),
        wheel_sha256=str(release.get("wheel_sha256")),
        database_schema=database_schema,
    )
    if (
        re.fullmatch(r"[0-9a-f]{40}", manifest_identity.source_commit) is None
        or _SHA256.fullmatch(manifest_identity.tree_sha256) is None
        or _SHA256.fullmatch(manifest_identity.wheel_sha256) is None
        or manifest_identity.database_schema < 1
    ):
        raise RegistryValidationError("evidence manifest release identity is malformed")
    expected_manifest_ref = _expected_manifest_ref(
        journey_id,
        evidence_class,
        expected_result,
        manifest_identity,
    )
    if manifest_ref != expected_manifest_ref:
        raise RegistryValidationError("manifest path is not the deterministic closed release-bound reference")
    _validate_sanitized_receipt(
        observation,
        journey_id=journey_id,
        evidence_class=evidence_class,
        expected_result=expected_result,
        manifest_identity=manifest_identity,
        repo_root=repo_root,
    )
    if require_current and manifest_identity != current:
        raise RegistryValidationError("manifest is not bound to the current exact release")
    raise RegistryValidationError("trusted_execution_attestation_unavailable")


def _validate_claim(
    row: JourneyRow,
    evidence_class: str,
    claim: EvidenceClaim,
    current: ReleaseIdentity,
) -> None:
    if len({ref.label for ref in claim.refs}) != len(claim.refs):
        raise RegistryValidationError("journey evidence references must be unique")
    resolved = tuple((_safe_repo_link(ref), ref.label) for ref in claim.refs)
    if claim.state == "AVAILABLE":
        if not resolved or any(path.suffix == ".json" for (path, _), _label in resolved):
            raise RegistryValidationError("AVAILABLE requires non-manifest repository artifacts")
        if any(label in _GENERIC_OPERATOR_REFS for (_path, _locator), label in resolved):
            raise RegistryValidationError("generic operator checks are not journey evidence")
        return
    if claim.state in {"MISSING", "NOT_APPLICABLE"}:
        if resolved:
            raise RegistryValidationError("missing or non-applicable evidence cannot cite proof")
        return
    if not resolved or any(
        path.suffix != ".json" or locator is not None for (path, locator), _label in resolved
    ):
        raise RegistryValidationError("manifest-backed evidence requires JSON manifest links only")
    for (path, _locator), label in resolved:
        if not label.startswith("evidence/golden_journeys/manifests/"):
            raise RegistryValidationError("decisive evidence must use the canonical manifest directory")
        raw = path.read_bytes()
        if not raw or len(raw) > 131_072:
            raise RegistryValidationError("evidence manifest exceeds its public size bound")
        try:
            payload = json.loads(raw, object_pairs_hook=_closed_json_object)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryValidationError("evidence manifest is not closed JSON") from exc
        if raw != _canonical_json_bytes(payload):
            raise RegistryValidationError("evidence manifest is not canonical JSON")
        _validate_manifest_payload(
            payload,
            manifest_ref=label,
            journey_id=row.journey_id,
            evidence_class=evidence_class,
            current=current,
            require_current=claim.state in {"VERIFIED", "FAILED"},
            repo_root=ROOT,
            expected_result="FAILED" if claim.state == "FAILED" else "VERIFIED",
        )


def _validate_applicability(row: JourneyRow) -> None:
    for evidence_class, claim in row.evidence.items():
        applicable = row.journey_id == "obsidian_write_sync" or evidence_class != ("physical device evidence")
        if applicable == (claim.state == "NOT_APPLICABLE"):
            raise RegistryValidationError("journey evidence applicability matrix is not exact")


def _validate_readiness(row: JourneyRow) -> None:
    states = tuple(claim.state for claim in row.evidence.values())
    applicable = tuple(state for state in states if state != "NOT_APPLICABLE")
    if row.readiness == "READY":
        if not applicable or any(state != "VERIFIED" for state in applicable) or row.limitations:
            raise RegistryValidationError("READY requires current manifests for every applicable class")
    elif row.readiness == "BLOCKED":
        if "FAILED" not in applicable or not row.limitations:
            raise RegistryValidationError("BLOCKED requires current failed decisive evidence")
    elif row.readiness == "DEGRADED":
        if (
            "FAILED" in applicable
            or "AVAILABLE" not in applicable
            or all(state == "VERIFIED" for state in applicable)
            or not row.limitations
        ):
            raise RegistryValidationError("DEGRADED must identify a bounded available but incomplete path")
    elif row.readiness == "UNVERIFIED":
        if (
            "FAILED" in applicable
            or not any(state in {"MISSING", "STALE"} for state in applicable)
            or not row.limitations
        ):
            raise RegistryValidationError("UNVERIFIED must identify missing or stale decisive evidence")
    elif row.readiness == "OUT_OF_SCOPE" and any(state != "NOT_APPLICABLE" for state in states):
        raise RegistryValidationError("OUT_OF_SCOPE cannot claim applicable evidence")
    obsidian = row.journey_id == "obsidian_write_sync"
    physical = row.evidence["physical device evidence"].state
    blocked_by_current_failure = row.readiness == "BLOCKED" and "FAILED" in applicable
    if (
        obsidian
        and physical != "VERIFIED"
        and row.readiness != "UNVERIFIED"
        and not blocked_by_current_failure
    ):
        raise RegistryValidationError("Obsidian needs physical Android evidence or an honest BLOCKED state")


def _write_fake_receipt(
    repo_root: Path,
    *,
    journey_id: str,
    evidence_class: str,
    result: str,
    identity: ReleaseIdentity,
) -> tuple[str, str]:
    artifact_ref = _expected_artifact_ref(journey_id, evidence_class, result, identity)
    path = repo_root / artifact_ref
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {
        "$schema": _RECEIPT_SCHEMA,
        "check_ids": _expected_check_ids(journey_id, evidence_class),
        "environment": ENVIRONMENT_BY_CLASS[evidence_class],
        "evidence_class": evidence_class,
        "journey_id": journey_id,
        "observed_at_utc": "2026-08-23T10:00:00Z",
        "proofs": _expected_executable_proofs(
            journey_id=journey_id,
            evidence_class=evidence_class,
            expected_result=result,
            manifest_identity=identity,
        ),
        "release": _release_payload(identity),
        "result": result,
    }
    raw = _canonical_json_bytes(receipt)
    path.write_bytes(raw)
    return artifact_ref, hashlib.sha256(raw).hexdigest()


def _fake_manifest(
    identity: ReleaseIdentity,
    repo_root: Path,
    *,
    result: str = "VERIFIED",
) -> dict[str, Any]:
    journey_id = "conversation_recall"
    evidence_class = "deterministic contract"
    artifact_ref, artifact_sha256 = _write_fake_receipt(
        repo_root,
        journey_id=journey_id,
        evidence_class=evidence_class,
        result=result,
        identity=identity,
    )
    return {
        "$schema": _MANIFEST_SCHEMA,
        "evidence_class": evidence_class,
        "journey_id": journey_id,
        "observation": {
            "artifact_ref": artifact_ref,
            "artifact_schema": _RECEIPT_SCHEMA,
            "artifact_sha256": artifact_sha256,
            "check_ids": _expected_check_ids(journey_id, evidence_class),
            "environment": "deterministic_contract",
            "observed_at_utc": "2026-08-23T10:00:00Z",
        },
        "release": _release_payload(identity),
        "result": result,
    }


def test_canonical_golden_journey_registry_is_closed_current_and_privacy_safe(
    tmp_path: Path,
) -> None:
    markdown = STATUS_PATH.read_text(encoding="utf-8")
    identity = _release_identity(markdown)
    rows = _registry_rows(markdown)

    assert len(rows) == len(CURRENT_JOURNEYS) == 6
    assert tuple(row.journey_id for row in rows) == tuple(CURRENT_JOURNEYS)
    assert {row.journey_id: (row.journey, row.readiness, row.limitations) for row in rows} == CURRENT_JOURNEYS
    assert sum(row.readiness == "READY" for row in rows) == 0
    assert sum(len(row.evidence) for row in rows) == 54
    assert sum(row.evidence["physical device evidence"].state == "NOT_APPLICABLE" for row in rows) == 5
    for row in rows:
        assert tuple(row.evidence) == EVIDENCE_CLASSES
        _validate_applicability(row)
        for evidence_class, claim in row.evidence.items():
            _validate_claim(row, evidence_class, claim, identity)
        _validate_readiness(row)
    assert all(
        row.evidence[evidence_class].state == "MISSING"
        for row in rows
        for evidence_class in _CURRENT_SNAPSHOT_MISSING_CLASSES
    )
    document = next(row for row in rows if row.journey_id == "document_recall_answer")
    assert document.evidence["restart and recovery evidence"].state == "AVAILABLE"
    restart_proof = (
        "tests/test_archive_search_runtime_publication.py::"
        "test_locate_select_and_explain_document_survives_both_runtime_restarts"
    )
    for evidence_class in ("integration path", "restart and recovery evidence"):
        assert restart_proof in tuple(ref.label for ref in document.evidence[evidence_class].refs)
        assert restart_proof in _PROOF_REFS_BY_JOURNEY_CLASS[("document_recall_answer", evidence_class)]

    detailed = (ROOT / "outer_sol" / "INTERACTION_CONTROL_PLANE_IMPLEMENTATION_STATUS.md").read_text(
        encoding="utf-8"
    )
    assert "outer_sol/PROJECT_IMPLEMENTATION_STATUS.md" in detailed

    with pytest.raises(RegistryValidationError, match="limitation cell"):
        _registry_rows(markdown.replace("`semantic_recall_missing`", "`semantic_recall_missing` prose"))
    obsidian = next(row for row in rows if row.journey_id == "obsidian_write_sync")
    with pytest.raises(RegistryValidationError, match="physical Android"):
        _validate_readiness(replace(obsidian, readiness="DEGRADED"))
    failed_evidence = dict(obsidian.evidence)
    failed_evidence["deterministic contract"] = EvidenceClaim("FAILED", ())
    _validate_readiness(replace(obsidian, readiness="BLOCKED", evidence=failed_evidence))
    conversation = next(row for row in rows if row.journey_id == "conversation_recall")
    future_evidence = dict(conversation.evidence)
    future_evidence["clean artifact path"] = EvidenceClaim("VERIFIED", ())
    _validate_applicability(replace(conversation, evidence=future_evidence))
    generic_operator = RepositoryLink(
        "tools/immutable_release_operator.py",
        "../tools/immutable_release_operator.py",
    )
    with pytest.raises(RegistryValidationError, match="generic operator"):
        _validate_claim(
            conversation,
            "clean artifact path",
            EvidenceClaim("AVAILABLE", (generic_operator,)),
            identity,
        )
    with pytest.raises(RegistryValidationError, match="READY requires"):
        _validate_readiness(replace(conversation, readiness="READY", limitations=()))
    with pytest.raises(RegistryValidationError, match="BLOCKED requires"):
        _validate_readiness(replace(conversation, readiness="BLOCKED"))
    mismatched_live = markdown.replace(
        f"/ `{identity.source_commit}`;",
        f"/ `{'0' * 40}`;",
        1,
    )
    with pytest.raises(RegistryValidationError, match="live commit identity diverge"):
        _release_identity(mismatched_live)
    current_only_proof = (
        "tests/test_golden_journey_registry.py::"
        "test_canonical_golden_journey_registry_is_closed_current_and_privacy_safe"
    )
    assert (ROOT / current_only_proof.split("::", maxsplit=1)[0]).is_file()
    repository_root = subprocess.run(
        ["git", "rev-list", "--max-parents=0", identity.source_commit],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    root_commits = repository_root.stdout.splitlines()
    assert repository_root.returncode == 0 and len(root_commits) == 1
    with pytest.raises(RegistryValidationError, match="missing or not a file at the manifest commit"):
        _exact_git_test_source(current_only_proof, source_commit=root_commits[0])

    manifest_journey_id = "conversation_recall"
    manifest_evidence_class = "deterministic contract"
    manifest_ref = _expected_manifest_ref(
        manifest_journey_id,
        manifest_evidence_class,
        "VERIFIED",
        identity,
    )
    identity_variants = (
        replace(identity, source_commit="0" * 40),
        replace(identity, tree_sha256="a" * 64),
        replace(identity, wheel_sha256="b" * 64),
        replace(identity, database_schema=identity.database_schema + 1),
    )
    assert (
        len(
            {
                manifest_ref,
                *(
                    _expected_manifest_ref(
                        manifest_journey_id,
                        manifest_evidence_class,
                        "VERIFIED",
                        variant,
                    )
                    for variant in identity_variants
                ),
            }
        )
        == 5
    )
    fake_manifest = _fake_manifest(identity, tmp_path)
    with pytest.raises(
        RegistryValidationError,
        match="^trusted_execution_attestation_unavailable$",
    ):
        _validate_manifest_payload(
            fake_manifest,
            manifest_ref=manifest_ref,
            journey_id=manifest_journey_id,
            evidence_class=manifest_evidence_class,
            current=identity,
            require_current=True,
            repo_root=tmp_path,
        )
    leaked = dict(fake_manifest)
    leaked["raw_response"] = "forbidden"
    with pytest.raises(RegistryValidationError, match="privacy allowlist"):
        _validate_manifest_payload(
            leaked,
            manifest_ref=manifest_ref,
            journey_id=manifest_journey_id,
            evidence_class=manifest_evidence_class,
            current=identity,
            require_current=True,
            repo_root=tmp_path,
        )

    with pytest.raises(RegistryValidationError, match="deterministic closed release-bound"):
        _validate_manifest_payload(
            fake_manifest,
            manifest_ref="evidence/golden_journeys/manifests/arbitrary.json",
            journey_id=manifest_journey_id,
            evidence_class=manifest_evidence_class,
            current=identity,
            require_current=True,
            repo_root=tmp_path,
        )

    stale_identity = replace(identity, wheel_sha256="b" * 64)
    stale_root = tmp_path / "stale-release"
    wrong_release = _fake_manifest(stale_identity, stale_root)
    stale_manifest_ref = _expected_manifest_ref(
        journey_id=manifest_journey_id,
        evidence_class=manifest_evidence_class,
        result="VERIFIED",
        identity=stale_identity,
    )
    with pytest.raises(RegistryValidationError, match="current exact release"):
        _validate_manifest_payload(
            wrong_release,
            manifest_ref=stale_manifest_ref,
            journey_id=manifest_journey_id,
            evidence_class=manifest_evidence_class,
            current=identity,
            require_current=True,
            repo_root=stale_root,
        )

    wrong_journey_checks = _fake_manifest(identity, tmp_path)
    wrong_journey_checks["observation"] = {
        **wrong_journey_checks["observation"],
        "check_ids": ["obsidian_write_sync.contract_suite"],
    }
    with pytest.raises(RegistryValidationError, match="exact journey class"):
        _validate_manifest_payload(
            wrong_journey_checks,
            manifest_ref=manifest_ref,
            journey_id=manifest_journey_id,
            evidence_class=manifest_evidence_class,
            current=identity,
            require_current=True,
            repo_root=tmp_path,
        )

    wrong_digest = _fake_manifest(identity, tmp_path)
    wrong_digest["observation"] = {
        **wrong_digest["observation"],
        "artifact_sha256": "c" * 64,
    }
    with pytest.raises(RegistryValidationError, match="actual bytes"):
        _validate_manifest_payload(
            wrong_digest,
            manifest_ref=manifest_ref,
            journey_id=manifest_journey_id,
            evidence_class=manifest_evidence_class,
            current=identity,
            require_current=True,
            repo_root=tmp_path,
        )

    arbitrary_root = tmp_path / "arbitrary-artifact"
    arbitrary_manifest = _fake_manifest(identity, arbitrary_root)
    arbitrary_observation = arbitrary_manifest["observation"]
    canonical_receipt = arbitrary_root / str(arbitrary_observation["artifact_ref"])
    arbitrary_ref = "evidence/golden_journeys/receipts/arbitrary.json"
    arbitrary_path = arbitrary_root / arbitrary_ref
    arbitrary_path.write_bytes(canonical_receipt.read_bytes())
    arbitrary_manifest["observation"] = {
        **arbitrary_observation,
        "artifact_ref": arbitrary_ref,
    }
    with pytest.raises(RegistryValidationError, match="deterministic closed artifact_ref"):
        _validate_manifest_payload(
            arbitrary_manifest,
            manifest_ref=manifest_ref,
            journey_id=manifest_journey_id,
            evidence_class=manifest_evidence_class,
            current=identity,
            require_current=True,
            repo_root=arbitrary_root,
        )

    proof_tamper_root = tmp_path / "proof-tamper"
    proof_tamper_manifest = _fake_manifest(identity, proof_tamper_root)
    proof_observation = proof_tamper_manifest["observation"]
    proof_receipt_path = proof_tamper_root / str(proof_observation["artifact_ref"])
    proof_receipt = json.loads(proof_receipt_path.read_bytes())
    proof_receipt["proofs"][0]["test_source_sha256"] = "d" * 64
    proof_receipt_bytes = _canonical_json_bytes(proof_receipt)
    proof_receipt_path.write_bytes(proof_receipt_bytes)
    proof_tamper_manifest["observation"] = {
        **proof_observation,
        "artifact_sha256": hashlib.sha256(proof_receipt_bytes).hexdigest(),
    }
    with pytest.raises(RegistryValidationError, match="exact test bytes and node id"):
        _validate_manifest_payload(
            proof_tamper_manifest,
            manifest_ref=manifest_ref,
            journey_id=manifest_journey_id,
            evidence_class=manifest_evidence_class,
            current=identity,
            require_current=True,
            repo_root=proof_tamper_root,
        )

    failed_root = tmp_path / "failed"
    failed_manifest = _fake_manifest(identity, failed_root, result="FAILED")
    failed_manifest_ref = _expected_manifest_ref(
        manifest_journey_id,
        manifest_evidence_class,
        "FAILED",
        identity,
    )
    with pytest.raises(
        RegistryValidationError,
        match="^trusted_execution_attestation_unavailable$",
    ):
        _validate_manifest_payload(
            failed_manifest,
            manifest_ref=failed_manifest_ref,
            journey_id=manifest_journey_id,
            evidence_class=manifest_evidence_class,
            current=identity,
            require_current=True,
            repo_root=failed_root,
            expected_result="FAILED",
        )

    leaked_receipt_root = tmp_path / "leaked-receipt"
    leaked_receipt_manifest = _fake_manifest(identity, leaked_receipt_root)
    leaked_observation = leaked_receipt_manifest["observation"]
    leaked_receipt_path = leaked_receipt_root / str(leaked_observation["artifact_ref"])
    leaked_receipt = json.loads(leaked_receipt_path.read_bytes())
    leaked_receipt["raw_content"] = "forbidden"
    leaked_receipt_bytes = _canonical_json_bytes(leaked_receipt)
    leaked_receipt_path.write_bytes(leaked_receipt_bytes)
    leaked_receipt_manifest["observation"] = {
        **leaked_observation,
        "artifact_sha256": hashlib.sha256(leaked_receipt_bytes).hexdigest(),
    }
    with pytest.raises(RegistryValidationError, match="privacy allowlist"):
        _validate_manifest_payload(
            leaked_receipt_manifest,
            manifest_ref=manifest_ref,
            journey_id=manifest_journey_id,
            evidence_class=manifest_evidence_class,
            current=identity,
            require_current=True,
            repo_root=leaked_receipt_root,
        )
