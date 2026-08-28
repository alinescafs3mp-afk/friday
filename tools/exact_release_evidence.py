#!/usr/bin/env python3
"""Produce exact-release journey evidence from one closed pytest inventory."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import immutable_release_operator as release_operator  # noqa: E402
from tools import quality_gate  # noqa: E402

RECEIPT_SCHEMA = "friday.golden-journey-sanitized-receipt.v2"
PRODUCER_PATH = "tools/exact_release_evidence.py"
PYTEST_TIMEOUT_SECONDS = 900
_PYTEST_BOOTSTRAP = (
    "import pathlib,sys; "
    "root=str(pathlib.Path(sys.argv.pop(1)).resolve(strict=True)); "
    "sys.path.insert(0,root); import pytest; raise SystemExit(pytest.main(sys.argv[1:]))"
)

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
_CHECK_SUFFIXES = {
    "deterministic contract": ("contract_suite",),
    "integration path": ("integration_suite",),
    "clean artifact path": ("installed_surface", "schema_migration", "wheel_reproducibility"),
    "synthetic live path": ("synthetic_live_battery",),
    "production read-only observation": ("database_integrity", "schema_attestation", "service_health"),
    "physical device evidence": ("android_round_trip", "real_conflict_preserved"),
    "restart and recovery evidence": ("cancellation", "expiry", "restart_resume"),
    "rollback evidence": ("activation_rollback",),
    "backup and restore evidence": ("clean_restore",),
}


def _parameterized_refs(base: str, parameter_ids: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{base}[{parameter_id}]" for parameter_id in parameter_ids)


_PROMOTED_WINDOW_REFS = _parameterized_refs(
    "tests/test_message_window_runtime_integration.py::"
    "test_promoted_exact_window_is_deterministic_scoped_and_receipted",
    ("2-complete", "21-partial", "0-empty"),
)
_MESSAGE_ARCHIVE_REFS = _parameterized_refs(
    "tests/test_archive_search_runtime_publication.py::"
    "test_selected_message_archive_evidence_replays_after_restart_then_fails_closed",
    (
        "exact-document-reference-search-denied",
        "exact-document-reference-corpus-denied",
        "exact-document-reference-source-drifted",
        "natural-content-reference-search-denied",
        "natural-content-reference-corpus-denied",
        "natural-content-reference-source-drifted",
    ),
)
_ARCHIVE_REPLAY_FAILURE_REFS = _parameterized_refs(
    "tests/test_archive_search_runtime_publication.py::"
    "test_selected_archive_replay_failure_is_source_free_and_suspends",
    ("denied-denied", "drifted-drifted"),
)
_OBSIDIAN_MESSAGE_MATRIX_REFS = _parameterized_refs(
    "tests/test_agent_obsidian_acceptance_message_matrix.py::"
    "test_every_exact_tier_a_b_message_routes_through_full_chat_once",
    (
        "OBS-NOTE-01",
        "OBS-NOTE-02",
        "OBS-DAILY-01",
        "OBS-TASK-01-add",
        "OBS-TASK-01-query",
        "OBS-META-01",
        "OBS-SEARCH-01",
        "OBS-SEARCH-02",
        "OBS-SYNC-01",
        "OBS-LINK-01",
        "OBS-MOVE-01-move",
        "OBS-MOVE-01-backlinks",
        "OBS-TEMPLATE-01",
        "OBS-WORK-01-save",
        "OBS-WORK-01-links",
        "OBS-BASE-01",
        "OBS-OFFLINE-01",
        "OBS-CONFLICT-01-replace",
        "OBS-CONFLICT-01-preview",
        "OBS-RECOVERY-01-append",
        "OBS-RECOVERY-01-resume",
        "OBS-DELETE-01-delete",
        "OBS-DELETE-01-search",
    ),
)
_SUPERVISOR_REVIEW_REFS = _parameterized_refs(
    "tests/test_supervisor_assist_controller.py::test_review_and_web_recovery_are_strictly_bounded",
    ("0", "1"),
)


_PROOF_REFS_BY_JOURNEY_CLASS = {
    ("conversation_recall", "deterministic contract"): (*_PROMOTED_WINDOW_REFS,),
    ("conversation_recall", "integration path"): (
        *_PROMOTED_WINDOW_REFS,
        *_MESSAGE_ARCHIVE_REFS,
    ),
    ("conversation_recall", "restart and recovery evidence"): (
        "tests/test_message_window_work_item_runtime.py::test_restart_temporal_followup_reuses_identity_role_and_zone_with_one_cas_update",
        *_MESSAGE_ARCHIVE_REFS,
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
        *_ARCHIVE_REPLAY_FAILURE_REFS,
    ),
    ("obsidian_write_sync", "deterministic contract"): (
        "tests/test_obsidian_structured_acceptance_core.py::test_conflict_preview_is_non_destructive_and_contains_both_versions",
    ),
    ("obsidian_write_sync", "integration path"): (*_OBSIDIAN_MESSAGE_MATRIX_REFS,),
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
        "tests/test_search_provider_refusal_is_not_emptiness.py::test_202_from_duckduckgo_is_a_refusal_not_an_empty_result[asyncio]",
    ),
    ("honest_degradation", "integration path"): (
        "tests/test_search_provider_refusal_is_not_emptiness.py::test_the_chain_moves_on_when_the_first_provider_refuses[asyncio]",
    ),
    ("honest_degradation", "synthetic live path"): (
        "tests/test_synthetic_live_battery.py::test_package_a_oracle_does_not_share_the_mutated_production_predicate",
    ),
    ("honest_degradation", "restart and recovery evidence"): (
        "tests/test_message_window_work_item_runtime.py::test_post_boundary_admission_race_returns_atomic_clarification_without_execution",
    ),
    ("current_file_web_comparison", "deterministic contract"): (
        "tests/test_compare_current_file_web_work_graph_schema45.py::test_schema45_exact_binding_is_durable_immutable_and_revision_cas",
    ),
    ("current_file_web_comparison", "integration path"): (*_SUPERVISOR_REVIEW_REFS,),
    ("current_file_web_comparison", "restart and recovery evidence"): (
        "tests/test_supervisor_assist_graph_adapter.py::test_terminal_cancel_and_startup_reconcile_publish_closed_receipts",
    ),
}
_GENERIC_OPERATOR_REFS = frozenset(
    {
        "tools/immutable_release_operator.py",
        "tests/test_immutable_release_operator.py::test_installed_surface_smoke_uses_one_hermetic_environment_and_cleans_it",
        "tests/test_immutable_release_operator.py::test_backend_start_uncertainty_never_restores_backup_or_runs_schema33",
        "tests/test_immutable_release_operator.py::test_obsidian_root_is_restored_exactly_with_database_and_inbox",
        "tests/test_storage_and_lifecycle.py::test_verified_backup_restore_is_atomic_and_creates_safety_copy",
    }
)

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_TEST_NAME = re.compile(r"test_[A-Za-z0-9_]{1,159}\Z")
_SAFE_ID = re.compile(r"[a-z][a-z0-9_.:-]{1,127}\Z")
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
        "execution",
        "proofs",
        "owner_smoke",
    }
)


class ExactReleaseEvidenceError(ValueError):
    """One closed validation or production failure."""


_EXECUTION_WITNESS_AUTHORITY = object()


@dataclass(frozen=True, slots=True)
class _ExecutionWitness:
    """Process-local proof returned only by the closed pytest runner."""

    outcomes: tuple[str, ...]
    exit_code: int
    collection_sha256: str
    outcome_projection_sha256: str
    authority: object


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    source_commit: str
    tree_sha256: str
    wheel_sha256: str
    database_schema: int

    def __post_init__(self) -> None:
        if (
            type(self) is not ReleaseIdentity
            or type(self.source_commit) is not str
            or type(self.tree_sha256) is not str
            or type(self.wheel_sha256) is not str
            or _COMMIT.fullmatch(self.source_commit) is None
            or _SHA256.fullmatch(self.tree_sha256) is None
            or _SHA256.fullmatch(self.wheel_sha256) is None
            or type(self.database_schema) is not int
            or self.database_schema < 1
        ):
            raise ExactReleaseEvidenceError("release_identity_invalid")

    def payload(self) -> dict[str, object]:
        return _release_payload(self)


@dataclass(frozen=True, slots=True)
class AuthenticatedOwnerSmokeBinding:
    """Expected binding supplied only after a separate authenticator accepted it.

    Constructing this value is not authentication.  A receipt embedding the
    same fields is rejected unless the validator receives this external
    expected binding.
    """

    schema: str
    authority: str
    artifact_ref: str
    artifact_sha256: str

    def payload(self) -> dict[str, str]:
        payload = _owner_smoke_payload(self)
        assert payload is not None
        return payload


def _release_payload(identity: ReleaseIdentity) -> dict[str, object]:
    if (
        type(identity) is not ReleaseIdentity
        or type(identity.source_commit) is not str
        or type(identity.tree_sha256) is not str
        or type(identity.wheel_sha256) is not str
        or _COMMIT.fullmatch(identity.source_commit) is None
        or _SHA256.fullmatch(identity.tree_sha256) is None
        or _SHA256.fullmatch(identity.wheel_sha256) is None
        or type(identity.database_schema) is not int
        or identity.database_schema < 1
    ):
        raise ExactReleaseEvidenceError("release_identity_invalid")
    return {
        "database_schema": identity.database_schema,
        "source_commit": identity.source_commit,
        "tree_sha256": identity.tree_sha256,
        "wheel_sha256": identity.wheel_sha256,
    }


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def proof_refs(journey_id: str, evidence_class: str) -> tuple[str, ...]:
    if type(journey_id) is not str or type(evidence_class) is not str:
        raise ExactReleaseEvidenceError("proof_inventory_invalid")
    refs = _PROOF_REFS_BY_JOURNEY_CLASS.get((journey_id, evidence_class), ())
    if not refs or any(ref in _GENERIC_OPERATOR_REFS for ref in refs) or len(set(refs)) != len(refs):
        raise ExactReleaseEvidenceError("proof_inventory_invalid")
    return refs


def _closed_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExactReleaseEvidenceError("receipt_json_invalid")
        result[key] = value
    return result


def _load_canonical_receipt(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > 65_536:
        raise ExactReleaseEvidenceError("receipt_json_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_closed_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ExactReleaseEvidenceError("receipt_json_invalid")
            ),
        )
    except (UnicodeError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ExactReleaseEvidenceError("receipt_json_invalid") from exc
    if type(value) is not dict:
        raise ExactReleaseEvidenceError("receipt_json_invalid")
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExactReleaseEvidenceError("receipt_json_invalid") from exc
    if raw != canonical:
        raise ExactReleaseEvidenceError("receipt_json_invalid")
    return value


def _git(repo_root: Path, *args: str) -> bytes:
    try:
        completed = subprocess.run(
            ("git", *args),
            cwd=repo_root,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise ExactReleaseEvidenceError("git_identity_unavailable") from exc
    if completed.returncode != 0 or completed.stderr:
        raise ExactReleaseEvidenceError("git_identity_unavailable")
    return completed.stdout


def _resolve_directory(path: Path, failure_code: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ExactReleaseEvidenceError(failure_code) from exc
    if not resolved.is_dir():
        raise ExactReleaseEvidenceError(failure_code)
    return resolved


def _exact_git_blob(repo_root: Path, commit: str, path: str) -> bytes:
    candidate = PurePosixPath(path)
    if (
        candidate.is_absolute()
        or str(candidate) != path
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ExactReleaseEvidenceError("git_blob_invalid")
    name = f"{commit}:{path}"
    if _git(repo_root, "--no-replace-objects", "cat-file", "-t", name) != b"blob\n":
        raise ExactReleaseEvidenceError("git_blob_invalid")
    return _git(repo_root, "--no-replace-objects", "cat-file", "blob", name)


def _test_source(repo_root: Path, commit: str, test_ref: str) -> bytes:
    if test_ref.count("::") != 1:
        raise ExactReleaseEvidenceError("test_ref_invalid")
    path, name = test_ref.split("::")
    function_name, separator, parameter_id = name.partition("[")
    parameter_valid = not separator or (
        name.endswith("]")
        and 1 <= len(parameter_id) <= 1024
        and not any(character in "\x00\r\n" for character in parameter_id)
    )
    if (
        not path.startswith("tests/test_")
        or not path.endswith(".py")
        or _TEST_NAME.fullmatch(function_name) is None
        or not parameter_valid
    ):
        raise ExactReleaseEvidenceError("test_ref_invalid")
    raw = _exact_git_blob(repo_root, commit, path)
    try:
        module = ast.parse(raw.decode("utf-8", errors="strict"), filename=f"{commit}:{path}")
    except (UnicodeError, SyntaxError) as exc:
        raise ExactReleaseEvidenceError("test_ref_invalid") from exc
    names = {node.name for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if function_name not in names:
        raise ExactReleaseEvidenceError("test_ref_invalid")
    return raw


def _check_ids(journey_id: str, evidence_class: str) -> list[str]:
    try:
        values = sorted(f"{journey_id}.{suffix}" for suffix in _CHECK_SUFFIXES[evidence_class])
    except KeyError as exc:
        raise ExactReleaseEvidenceError("evidence_class_invalid") from exc
    if _SAFE_ID.fullmatch(journey_id) is None or any(_SAFE_ID.fullmatch(value) is None for value in values):
        raise ExactReleaseEvidenceError("check_id_invalid")
    return values


def _owner_smoke_payload(value: AuthenticatedOwnerSmokeBinding | None) -> dict[str, str] | None:
    if value is None:
        return None
    if type(value) is not AuthenticatedOwnerSmokeBinding:
        raise ExactReleaseEvidenceError("owner_smoke_not_authenticated")
    if (
        type(value.schema) is not str
        or type(value.authority) is not str
        or type(value.artifact_ref) is not str
        or type(value.artifact_sha256) is not str
    ):
        raise ExactReleaseEvidenceError("owner_smoke_binding_invalid")
    path = PurePosixPath(value.artifact_ref)
    if (
        not value.schema.startswith("friday.")
        or _SAFE_ID.fullmatch(value.authority) is None
        or _SHA256.fullmatch(value.artifact_sha256) is None
        or path.is_absolute()
        or str(path) != value.artifact_ref
        or not path.parts
        or path.parts[0] != "evidence"
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ExactReleaseEvidenceError("owner_smoke_binding_invalid")
    return {
        "artifact_ref": value.artifact_ref,
        "artifact_sha256": value.artifact_sha256,
        "authority": value.authority,
        "schema": value.schema,
    }


def _require_neutralized_ignored_files(repo_root: Path) -> None:
    raw = _git(
        repo_root,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--full-name",
        "-z",
        "--",
    )
    if raw and not raw.endswith(b"\0"):
        raise ExactReleaseEvidenceError("checkout_ignored_artifact")
    for encoded in raw[:-1].split(b"\0") if raw else ():
        try:
            path = encoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ExactReleaseEvidenceError("checkout_ignored_artifact") from exc
        candidate = PurePosixPath(path)
        valid = bool(
            path
            and not candidate.is_absolute()
            and str(candidate) == path
            and all(part not in {"", ".", ".."} for part in candidate.parts)
        )
        regular_without_symlink_parent = False
        if valid:
            try:
                parent = repo_root
                parent_is_exact = True
                for part in candidate.parts[:-1]:
                    parent /= part
                    parent_is_exact = parent_is_exact and stat.S_ISDIR(parent.lstat().st_mode)
                regular_without_symlink_parent = parent_is_exact and stat.S_ISREG(
                    (repo_root / path).lstat().st_mode
                )
            except OSError:
                regular_without_symlink_parent = False
        inert_root_cache = len(candidate.parts) > 1 and candidate.parts[0] in {
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
        }
        neutralized_bytecode = (
            len(candidate.parts) > 1
            and candidate.parts[-2] == "__pycache__"
            and candidate.name.endswith(".pyc")
        )
        if not regular_without_symlink_parent or not (inert_root_cache or neutralized_bytecode):
            raise ExactReleaseEvidenceError("checkout_ignored_artifact")


def _require_exact_checkout(repo_root: Path, commit: str) -> None:
    try:
        head = _git(repo_root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    except UnicodeError as exc:
        raise ExactReleaseEvidenceError("git_identity_unavailable") from exc
    status = _git(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if head != commit or status:
        raise ExactReleaseEvidenceError("checkout_not_exact_clean_commit")
    _require_neutralized_ignored_files(repo_root)


def _require_running_producer(repo_root: Path, commit: str) -> None:
    producer_blob = _exact_git_blob(repo_root, commit, PRODUCER_PATH)
    producer_path = repo_root / PRODUCER_PATH
    try:
        producer_bytes = producer_path.read_bytes()
        running_producer = Path(__file__).resolve(strict=True)
        repository_producer = producer_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ExactReleaseEvidenceError("producer_source_invalid") from exc
    if running_producer != repository_producer or producer_bytes != producer_blob:
        raise ExactReleaseEvidenceError("producer_source_invalid")


@contextmanager
def _validation_source_checkout(
    repo_root: Path,
    source_commit: str,
) -> Iterator[tuple[Path, bool]]:
    """Yield an exact source tree while keeping a later validator HEAD valid."""

    root = _resolve_directory(repo_root, "repo_root_invalid")
    try:
        current_head = _git(root, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
    except UnicodeError as exc:
        raise ExactReleaseEvidenceError("git_identity_unavailable") from exc
    if _COMMIT.fullmatch(current_head) is None:
        raise ExactReleaseEvidenceError("git_identity_unavailable")
    _require_exact_checkout(root, current_head)
    _require_running_producer(root, current_head)
    if current_head == source_commit:
        yield root, True
        return
    if _COMMIT.fullmatch(source_commit) is None:
        raise ExactReleaseEvidenceError("release_identity_invalid")

    try:
        with tempfile.TemporaryDirectory(prefix="friday-exact-source-") as temporary:
            detached = Path(temporary) / "repository"
            _git(
                root,
                "-c",
                "core.hooksPath=/dev/null",
                "clone",
                "--quiet",
                "--shared",
                "--no-checkout",
                "--",
                str(root),
                str(detached),
            )
            _git(
                detached,
                "-c",
                "core.hooksPath=/dev/null",
                "checkout",
                "--quiet",
                "--detach",
                source_commit,
            )
            _require_exact_checkout(detached, source_commit)
            yield detached, False
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("source_checkout_unavailable") from exc


def _source_proofs(
    repo_root: Path,
    identity: ReleaseIdentity,
    journey_id: str,
    evidence_class: str,
    *,
    require_running_producer: bool = True,
) -> tuple[str, list[dict[str, str]]]:
    producer_blob = _exact_git_blob(repo_root, identity.source_commit, PRODUCER_PATH)
    if require_running_producer:
        _require_running_producer(repo_root, identity.source_commit)
    proofs = []
    for test_ref in proof_refs(journey_id, evidence_class):
        source = _test_source(repo_root, identity.source_commit, test_ref)
        path = repo_root / test_ref.split("::", maxsplit=1)[0]
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise ExactReleaseEvidenceError("test_source_not_exact") from exc
        if current != source:
            raise ExactReleaseEvidenceError("test_source_not_exact")
        proofs.append(
            {
                "outcome": "",
                "runner": "pytest",
                "test_ref": test_ref,
                "test_source_sha256": hashlib.sha256(source).hexdigest(),
            }
        )
    return hashlib.sha256(producer_blob).hexdigest(), proofs


def _xml_name(tag: str) -> str:
    return tag.rpartition("}")[2]


def _pytest_outcomes(report_path: Path, expected: tuple[str, ...]) -> tuple[str, ...]:
    try:
        summary = quality_gate.junit_summary(report_path)
        root = ET.parse(report_path).getroot()
    except (OSError, RuntimeError, ET.ParseError, ValueError) as exc:
        raise ExactReleaseEvidenceError("pytest_report_invalid") from exc
    if summary.errors or summary.skipped or summary.nodeids != expected:
        raise ExactReleaseEvidenceError("pytest_report_invalid")
    outcomes: dict[str, str] = {}
    for testcase in (element for element in root.iter() if _xml_name(element.tag) == "testcase"):
        values = [
            item.attrib.get("value")
            for item in testcase.iter()
            if _xml_name(item.tag) == "property" and item.attrib.get("name") == "friday_nodeid"
        ]
        failures = [child for child in testcase if _xml_name(child.tag) == "failure"]
        if len(values) != 1 or values[0] in outcomes or len(failures) > 1:
            raise ExactReleaseEvidenceError("pytest_report_invalid")
        outcomes[str(values[0])] = "FAILED" if failures else "PASSED"
    if (
        tuple(outcomes) != expected
        or sum(value == "FAILED" for value in outcomes.values()) != summary.failures
    ):
        raise ExactReleaseEvidenceError("pytest_report_invalid")
    return tuple(outcomes[nodeid] for nodeid in expected)


def _execution_witness(
    outcomes: tuple[str, ...],
    exit_code: int,
    collection_sha256: str,
    outcome_projection_sha256: str,
) -> _ExecutionWitness:
    if (
        type(outcomes) is not tuple
        or not outcomes
        or any(outcome not in {"PASSED", "FAILED"} for outcome in outcomes)
        or type(exit_code) is not int
        or exit_code != (0 if all(outcome == "PASSED" for outcome in outcomes) else 1)
        or type(collection_sha256) is not str
        or type(outcome_projection_sha256) is not str
        or _SHA256.fullmatch(collection_sha256) is None
        or _SHA256.fullmatch(outcome_projection_sha256) is None
    ):
        raise ExactReleaseEvidenceError("pytest_execution_evidence_invalid")
    return _ExecutionWitness(
        outcomes,
        exit_code,
        collection_sha256,
        outcome_projection_sha256,
        _EXECUTION_WITNESS_AUTHORITY,
    )


def _require_execution_witness(value: object) -> _ExecutionWitness:
    if type(value) is not _ExecutionWitness or value.authority is not _EXECUTION_WITNESS_AUTHORITY:
        raise ExactReleaseEvidenceError("pytest_execution_evidence_invalid")
    return _execution_witness(
        value.outcomes,
        value.exit_code,
        value.collection_sha256,
        value.outcome_projection_sha256,
    )


def _outcome_projection_sha256(nodeids: tuple[str, ...], outcomes: tuple[str, ...]) -> str:
    """Hash the deterministic outcome projection derived from strict JUnit."""

    return hashlib.sha256(
        canonical_json_bytes(
            {
                "nodeids": list(nodeids),
                "outcomes": list(outcomes),
                "version": 1,
            }
        )
    ).hexdigest()


def _run_closed_pytest(
    repo_root: Path,
    identity: ReleaseIdentity,
    journey_id: str,
    evidence_class: str,
    *,
    require_running_producer: bool = True,
) -> _ExecutionWitness:
    nodeids = proof_refs(journey_id, evidence_class)
    _require_exact_checkout(repo_root, identity.source_commit)
    _source_proofs(
        repo_root,
        identity,
        journey_id,
        evidence_class,
        require_running_producer=require_running_producer,
    )
    run_error: BaseException | None = None
    result: subprocess.CompletedProcess[bytes] | None = None
    outcomes: tuple[str, ...] = ()
    collection_sha256 = ""
    outcome_projection_sha256 = ""
    try:
        with tempfile.TemporaryDirectory(prefix="friday-exact-evidence-") as temporary:
            scratch = Path(temporary)
            report = scratch / "results.xml"
            collection = scratch / "collection.json"
            python_cache = scratch / "python-cache"
            with quality_gate._isolated_test_environment() as environment:  # noqa: SLF001
                environment.pop("PYTEST_PLUGINS", None)
                environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
                environment["PYTHONPYCACHEPREFIX"] = str(python_cache)
                result = subprocess.run(
                    (
                        sys.executable,
                        "-I",
                        "-X",
                        f"pycache_prefix={python_cache}",
                        "-c",
                        _PYTEST_BOOTSTRAP,
                        str(repo_root),
                        "-q",
                        "-o",
                        "addopts=",
                        "-p",
                        "no:cacheprovider",
                        "-p",
                        "pytest_asyncio.plugin",
                        "-p",
                        "anyio.pytest_plugin",
                        "-p",
                        "xdist.plugin",
                        "-p",
                        "tools.quality_gate",
                        "-n",
                        "0",
                        f"--junitxml={report}",
                        f"--friday-collection-manifest={collection}",
                        f"--basetemp={scratch / 'pytest'}",
                        *nodeids,
                    ),
                    cwd=repo_root,
                    env=environment,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=PYTEST_TIMEOUT_SECONDS,
                )
            try:
                collected = quality_gate.collection_nodeids(collection)
                collection_raw = collection.read_bytes()
                report.read_bytes()
            except (OSError, RuntimeError, ValueError) as exc:
                raise ExactReleaseEvidenceError("pytest_collection_invalid") from exc
            if collected != nodeids:
                raise ExactReleaseEvidenceError("pytest_collection_invalid")
            outcomes = _pytest_outcomes(report, nodeids)
            expected_code = 0 if all(outcome == "PASSED" for outcome in outcomes) else 1
            if result.returncode != expected_code:
                raise ExactReleaseEvidenceError("pytest_exit_invalid")
            collection_sha256 = hashlib.sha256(collection_raw).hexdigest()
            outcome_projection_sha256 = _outcome_projection_sha256(nodeids, outcomes)
    except (OSError, subprocess.SubprocessError, RuntimeError, ExactReleaseEvidenceError) as exc:
        run_error = exc
    finally:
        _require_exact_checkout(repo_root, identity.source_commit)
        _source_proofs(
            repo_root,
            identity,
            journey_id,
            evidence_class,
            require_running_producer=require_running_producer,
        )
    if run_error is not None:
        if isinstance(run_error, ExactReleaseEvidenceError):
            raise run_error
        raise ExactReleaseEvidenceError("pytest_execution_failed") from run_error
    assert result is not None
    return _execution_witness(
        outcomes,
        result.returncode,
        collection_sha256,
        outcome_projection_sha256,
    )


def _canonical_utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _produce_for_identity(
    *,
    repo_root: Path,
    identity: ReleaseIdentity,
    journey_id: str,
    evidence_class: str,
    owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
) -> bytes:
    """Run the code-owned tests; callers cannot provide a result or runner."""

    release_payload = _release_payload(identity)
    owner_smoke_payload = _owner_smoke_payload(owner_smoke)
    root = _resolve_directory(repo_root, "repo_root_invalid")
    producer_sha256, proofs = _source_proofs(root, identity, journey_id, evidence_class)
    witness = _run_closed_pytest(root, identity, journey_id, evidence_class)
    for proof, outcome in zip(proofs, witness.outcomes, strict=True):
        proof["outcome"] = outcome
    result = "VERIFIED" if witness.exit_code == 0 else "FAILED"
    receipt = {
        "$schema": RECEIPT_SCHEMA,
        "check_ids": _check_ids(journey_id, evidence_class),
        "environment": ENVIRONMENT_BY_CLASS[evidence_class],
        "evidence_class": evidence_class,
        "execution": {
            "collection_sha256": witness.collection_sha256,
            "exit_code": witness.exit_code,
            "outcome_projection_sha256": witness.outcome_projection_sha256,
            "producer_path": PRODUCER_PATH,
            "producer_source_sha256": producer_sha256,
            "runner": "pytest",
        },
        "journey_id": journey_id,
        "observed_at_utc": _canonical_utc_now(),
        "owner_smoke": owner_smoke_payload,
        "proofs": proofs,
        "release": release_payload,
        "result": result,
    }
    raw = canonical_json_bytes(receipt)
    _validate_receipt(
        raw,
        expected_release=identity,
        expected_journey_id=journey_id,
        expected_evidence_class=evidence_class,
        repo_root=root,
        authenticated_owner_smoke=owner_smoke,
        execution_witness=witness,
    )
    return raw


def _validate_receipt(
    raw: bytes,
    *,
    expected_release: ReleaseIdentity,
    expected_journey_id: str,
    expected_evidence_class: str,
    repo_root: Path,
    authenticated_owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
    execution_witness: _ExecutionWitness | None = None,
    require_running_producer: bool = True,
) -> dict[str, Any]:
    """Validate against external release and already-authenticated owner roots.

    ``authenticated_owner_smoke`` is an expected value from a separate
    authenticator.  The embedded object, a boolean, or an artifact path alone
    never establishes owner authority.
    """

    expected_release_payload = _release_payload(expected_release)
    root = _resolve_directory(repo_root, "repo_root_invalid")
    expected_producer_sha256, _source_inventory = _source_proofs(
        root,
        expected_release,
        expected_journey_id,
        expected_evidence_class,
        require_running_producer=require_running_producer,
    )
    value = _load_canonical_receipt(raw)
    if set(value) != _RECEIPT_FIELDS:
        raise ExactReleaseEvidenceError("receipt_fields_invalid")
    refs = proof_refs(expected_journey_id, expected_evidence_class)
    expected_checks = _check_ids(expected_journey_id, expected_evidence_class)
    if (
        value.get("$schema") != RECEIPT_SCHEMA
        or value.get("journey_id") != expected_journey_id
        or value.get("evidence_class") != expected_evidence_class
        or value.get("environment") != ENVIRONMENT_BY_CLASS[expected_evidence_class]
        or value.get("check_ids") != expected_checks
        or value.get("release") != expected_release_payload
    ):
        raise ExactReleaseEvidenceError("receipt_binding_invalid")

    observed_at = value.get("observed_at_utc")
    if type(observed_at) is not str or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", observed_at
    ):
        raise ExactReleaseEvidenceError("receipt_time_invalid")
    try:
        datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ExactReleaseEvidenceError("receipt_time_invalid") from exc

    execution = value.get("execution")
    execution_fields = {
        "collection_sha256",
        "exit_code",
        "outcome_projection_sha256",
        "producer_path",
        "producer_source_sha256",
        "runner",
    }
    if type(execution) is not dict or set(execution) != execution_fields:
        raise ExactReleaseEvidenceError("execution_binding_invalid")
    expected_collection = canonical_json_bytes({"nodeids": list(refs), "version": 1})
    if (
        execution.get("producer_path") != PRODUCER_PATH
        or execution.get("producer_source_sha256") != expected_producer_sha256
        or execution.get("runner") != "pytest"
        or execution.get("collection_sha256") != hashlib.sha256(expected_collection).hexdigest()
        or _SHA256.fullmatch(str(execution.get("outcome_projection_sha256") or "")) is None
        or type(execution.get("exit_code")) is not int
        or execution.get("exit_code") not in {0, 1}
    ):
        raise ExactReleaseEvidenceError("execution_binding_invalid")

    proofs = value.get("proofs")
    if type(proofs) is not list or len(proofs) != len(refs):
        raise ExactReleaseEvidenceError("proofs_invalid")
    outcomes: list[str] = []
    for proof, test_ref in zip(proofs, refs, strict=True):
        source = _test_source(root, expected_release.source_commit, test_ref)
        expected_base = {
            "runner": "pytest",
            "test_ref": test_ref,
            "test_source_sha256": hashlib.sha256(source).hexdigest(),
        }
        if (
            type(proof) is not dict
            or set(proof) != {"outcome", *expected_base}
            or any(proof.get(key) != item for key, item in expected_base.items())
            or proof.get("outcome") not in {"PASSED", "FAILED"}
        ):
            raise ExactReleaseEvidenceError("proofs_invalid")
        outcomes.append(str(proof["outcome"]))
    derived_result = "VERIFIED" if all(outcome == "PASSED" for outcome in outcomes) else "FAILED"
    derived_exit = 0 if derived_result == "VERIFIED" else 1
    if value.get("result") != derived_result or execution.get("exit_code") != derived_exit:
        raise ExactReleaseEvidenceError("result_not_machine_derived")
    if execution.get("outcome_projection_sha256") != _outcome_projection_sha256(
        refs,
        tuple(outcomes),
    ):
        raise ExactReleaseEvidenceError("execution_evidence_mismatch")

    expected_smoke = _owner_smoke_payload(authenticated_owner_smoke)
    embedded_smoke = value.get("owner_smoke")
    if embedded_smoke != expected_smoke:
        raise ExactReleaseEvidenceError("owner_smoke_not_authenticated")
    if embedded_smoke is not None and (
        type(embedded_smoke) is not dict
        or set(embedded_smoke) != {"artifact_ref", "artifact_sha256", "authority", "schema"}
    ):
        raise ExactReleaseEvidenceError("owner_smoke_binding_invalid")

    witness = (
        _run_closed_pytest(
            root,
            expected_release,
            expected_journey_id,
            expected_evidence_class,
            require_running_producer=require_running_producer,
        )
        if execution_witness is None
        else execution_witness
    )
    witness = _require_execution_witness(witness)
    if (
        tuple(outcomes) != witness.outcomes
        or execution.get("exit_code") != witness.exit_code
        or execution.get("collection_sha256") != witness.collection_sha256
        or execution.get("outcome_projection_sha256") != witness.outcome_projection_sha256
        or witness.outcome_projection_sha256 != _outcome_projection_sha256(refs, witness.outcomes)
    ):
        raise ExactReleaseEvidenceError("execution_evidence_mismatch")
    return value


def validate_receipt(
    raw: bytes,
    *,
    expected_release: ReleaseIdentity,
    expected_journey_id: str,
    expected_evidence_class: str,
    repo_root: Path,
    authenticated_owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
) -> dict[str, Any]:
    """Validate structure, then independently rerun the exact closed inventory."""

    with _validation_source_checkout(repo_root, expected_release.source_commit) as (
        source_root,
        require_running_producer,
    ):
        return _validate_receipt(
            raw,
            expected_release=expected_release,
            expected_journey_id=expected_journey_id,
            expected_evidence_class=expected_evidence_class,
            repo_root=source_root,
            authenticated_owner_smoke=authenticated_owner_smoke,
            require_running_producer=require_running_producer,
        )


def _stable_file(path: Path, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum_bytes:
            raise ExactReleaseEvidenceError("release_artifact_invalid")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1 << 20, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        stable = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_size", "st_mtime_ns")
        if len(raw) != before.st_size or any(
            getattr(before, name) != getattr(after, name) for name in stable
        ):
            raise ExactReleaseEvidenceError("release_artifact_changed")
        return raw
    except OSError as exc:
        raise ExactReleaseEvidenceError("release_artifact_invalid") from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise ExactReleaseEvidenceError("release_artifact_invalid") from exc


def derive_release_identity(release_root: Path) -> ReleaseIdentity:
    """Derive identity from a sealed wheel release and run its installed smoke."""

    try:
        root = Path(os.path.abspath(release_root)).resolve(strict=True)
        manifest = root / "artifacts/release-tree.sha256"
        manifest_raw = _stable_file(manifest, 64 << 20)
        tree_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        installed = release_operator.load_release_identity(root, expected_tree_sha256=tree_sha256)
        smoke_sha256 = release_operator.installed_surface_smoke(installed)
        if _SHA256.fullmatch(smoke_sha256) is None:
            raise ExactReleaseEvidenceError("installed_surface_smoke_invalid")
        release_operator.verify_release_tree(installed)
        if _stable_file(manifest, 64 << 20) != manifest_raw:
            raise ExactReleaseEvidenceError("release_artifact_changed")
        metadata_raw = _stable_file(root / "artifacts/immutable-release.json", 1 << 20)
        if not metadata_raw.endswith(b"\n"):
            raise ExactReleaseEvidenceError("release_metadata_invalid")
        metadata = json.loads(
            metadata_raw[:-1].decode("ascii", errors="strict"),
            object_pairs_hook=_closed_object,
        )
    except (
        OSError,
        TypeError,
        UnicodeError,
        ValueError,
        RuntimeError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        release_operator.ReleaseFailure,
    ) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("release_identity_invalid") from exc
    if type(metadata) is not dict:
        raise ExactReleaseEvidenceError("release_metadata_invalid")
    try:
        metadata_canonical = canonical_json_bytes(metadata)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExactReleaseEvidenceError("release_metadata_invalid") from exc
    if metadata_raw != metadata_canonical + b"\n":
        raise ExactReleaseEvidenceError("release_metadata_invalid")
    wheel_sha256 = metadata.get("wheel_sha256")
    if (
        metadata.get("commit") != installed.commit
        or metadata.get("max_schema") != installed.max_schema
        or type(wheel_sha256) is not str
        or _SHA256.fullmatch(wheel_sha256) is None
    ):
        raise ExactReleaseEvidenceError("release_metadata_invalid")
    return ReleaseIdentity(
        source_commit=installed.commit,
        tree_sha256=tree_sha256,
        wheel_sha256=wheel_sha256,
        database_schema=installed.max_schema,
    )


def produce_receipt(
    *,
    repo_root: Path,
    release_root: Path,
    journey_id: str,
    evidence_class: str,
    authenticated_owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
) -> bytes:
    """Produce evidence, optionally binding a separately authenticated smoke token.

    Constructing the token is not authentication.  Callers may pass one only
    after a separate authenticator established the exact expected binding;
    consumers must independently supply that same expected token to validation.
    """

    identity = derive_release_identity(release_root)
    raw = _produce_for_identity(
        repo_root=repo_root,
        identity=identity,
        journey_id=journey_id,
        evidence_class=evidence_class,
        owner_smoke=authenticated_owner_smoke,
    )
    if derive_release_identity(release_root) != identity:
        raise ExactReleaseEvidenceError("release_identity_changed")
    return raw


def _cleanup_owned_target(target: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        current = os.lstat(target)
        if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            os.unlink(target)
    except OSError:
        pass


def write_receipt_exclusive(path: Path, raw: bytes) -> str:
    """Write one complete canonical receipt without replacing an existing name."""

    _load_canonical_receipt(raw)
    try:
        target = Path(os.path.abspath(path))
        parent = target.parent.resolve(strict=True)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ExactReleaseEvidenceError("receipt_output_invalid") from exc
    if parent != target.parent or target.name in {"", ".", ".."}:
        raise ExactReleaseEvidenceError("receipt_output_invalid")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    owned_identity: tuple[int, int] | None = None
    failure: OSError | None = None
    try:
        descriptor = os.open(target, flags, 0o600)
        opened = os.fstat(descriptor)
        owned_identity = (opened.st_dev, opened.st_ino)
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("receipt write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_nlink != 1
            or status.st_size != len(raw)
        ):
            raise OSError("receipt postcondition failed")
    except OSError as exc:
        failure = exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                if failure is None:
                    failure = exc
    if failure is not None:
        _cleanup_owned_target(target, owned_identity)
        raise ExactReleaseEvidenceError("receipt_output_invalid") from failure
    return hashlib.sha256(raw).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--release-root", required=True, type=Path)
    run.add_argument("--repo-root", required=True, type=Path)
    run.add_argument(
        "--journey-id", required=True, choices=sorted({key[0] for key in _PROOF_REFS_BY_JOURNEY_CLASS})
    )
    run.add_argument(
        "--evidence-class",
        required=True,
        choices=sorted({key[1] for key in _PROOF_REFS_BY_JOURNEY_CLASS}),
    )
    run.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        raw = produce_receipt(
            repo_root=args.repo_root,
            release_root=args.release_root,
            journey_id=args.journey_id,
            evidence_class=args.evidence_class,
        )
        digest = write_receipt_exclusive(args.output, raw)
        receipt = _load_canonical_receipt(raw)
        print(canonical_json_bytes({"receipt_sha256": digest, "result": receipt["result"]}).decode())
        return 0 if receipt["result"] == "VERIFIED" else 1
    except ExactReleaseEvidenceError as exc:
        print(canonical_json_bytes({"failure_code": str(exc), "status": "failed_closed"}).decode())
        return 2
    except Exception:  # Last-resort CLI boundary: never emit a runtime traceback.
        print(
            canonical_json_bytes(
                {"failure_code": "unexpected_runtime_failure", "status": "failed_closed"}
            ).decode()
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
