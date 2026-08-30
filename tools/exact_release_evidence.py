#!/usr/bin/env python3
"""Produce exact-release journey evidence from one closed pytest inventory."""

from __future__ import annotations

import argparse
import ast
import fcntl
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
from contextlib import contextmanager, suppress
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
CLEAN_ARTIFACT_RECEIPT_SCHEMA = "friday.golden-journey-sanitized-receipt.v4"
MANIFEST_SCHEMA = "friday.golden-journey-evidence.v1"
PRODUCER_PATH = "tools/exact_release_evidence.py"
PYTEST_TIMEOUT_SECONDS = 900
_EVIDENCE_ROOT = PurePosixPath("evidence/golden_journeys")
_CLEAN_ARTIFACT_CLASS = "clean artifact path"
_SUBPROCESS_POLICY = "cpython_audit_deny"
_PYTEST_BOOTSTRAP = (
    "import pathlib,sys; "
    "root=str(pathlib.Path(sys.argv.pop(1)).resolve(strict=True)); "
    "sys.path.insert(0,root); import pytest; raise SystemExit(pytest.main(sys.argv[1:]))"
)
_INSTALLED_PYTEST_BOOTSTRAP = r"""
import hashlib,importlib.machinery,json,os,pathlib,posix,sys
source_root=pathlib.Path(sys.argv.pop(1)).resolve(strict=True)
release_root=pathlib.Path(sys.argv.pop(1)).resolve(strict=True)
site=pathlib.Path(sys.argv.pop(1)).resolve(strict=True)
site_ref=sys.argv.pop(1)
report_path=pathlib.Path(sys.argv.pop(1))
source_commit=sys.argv.pop(1)
wheel_sha256=sys.argv.pop(1)
if site != release_root / site_ref:
    raise RuntimeError("installed_site_binding_invalid")
sys.path.insert(0,str(source_root))
sys.path.insert(0,str(site))
blocked={"os.exec","os.fork","os.forkpty","os.posix_spawn","os.posix_spawnp","os.system","pty.spawn","subprocess.Popen"}
violations=set()
source_product_roots=tuple((source_root/name).resolve(strict=False) for name in ("friday","friday_host_agent","friday_package_broker"))
def resolves_to_source_product(raw_path,dir_fd=None):
    candidate=pathlib.Path(os.fsdecode(raw_path))
    if not candidate.is_absolute():
        if dir_fd is None:
            base=pathlib.Path.cwd()
        else:
            try:
                base=pathlib.Path("/proc/self/fd")/str(dir_fd)
                base=base.resolve(strict=True)
            except (OSError,RuntimeError,ValueError) as exc:
                violations.add("dir_fd_open_unattested")
                raise RuntimeError("dir_fd_open_unattested") from exc
        candidate=base/candidate
    resolved=candidate.resolve(strict=False)
    return any(resolved == root or root in resolved.parents for root in source_product_roots)
def deny_child(event,args):
    if event in blocked:
        violations.add("child_execution_unattested")
        raise RuntimeError("child_execution_unattested")
    if event == "open" and args and isinstance(args[0],(str,bytes,os.PathLike)):
        if resolves_to_source_product(args[0]):
            violations.add("source_first_party_read_unattested")
            raise RuntimeError("source_first_party_read_unattested")
sys.addaudithook(deny_child)
raw_os_open=os.open
def guarded_os_open(path,flags,mode=0o777,*,dir_fd=None):
    if dir_fd is not None and isinstance(path,(str,bytes,os.PathLike)) and resolves_to_source_product(path,dir_fd):
        violations.add("source_first_party_read_unattested")
        raise RuntimeError("source_first_party_read_unattested")
    return raw_os_open(path,flags,mode,dir_fd=dir_fd)
os.open=guarded_os_open
posix.open=guarded_os_open
origins=set()
def first_party(name):
    return name == "friday" or name.startswith("friday.") or name.startswith("friday_")
def confined(path):
    resolved=pathlib.Path(path).resolve(strict=True)
    try:
        relative=resolved.relative_to(site).as_posix()
    except ValueError as exc:
        violations.add("first_party_origin_escaped_release")
        raise RuntimeError("first_party_origin_escaped_release") from exc
    origins.add(relative)
    return resolved
class FirstPartyGuard:
    def find_spec(self,fullname,path=None,target=None):
        if not first_party(fullname):
            return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec is None:
            violations.add("first_party_origin_missing")
            raise RuntimeError("first_party_origin_missing")
        if spec.origin in {None,"built-in","frozen"}:
            violations.add("first_party_origin_missing")
            raise RuntimeError("first_party_origin_missing")
        confined(spec.origin)
        locations=spec.submodule_search_locations
        if locations is not None:
            for location in locations:
                confined(location)
        return spec
sys.meta_path.insert(0,FirstPartyGuard())
import friday
friday_origin=pathlib.Path(friday.__file__).resolve(strict=True)
if friday_origin != site / "friday" / "__init__.py":
    raise RuntimeError("installed_friday_origin_invalid")
from tools import quality_gate as exact_quality_gate
if pathlib.Path(exact_quality_gate.__file__).resolve(strict=True) != source_root / "tools" / "quality_gate.py":
    raise RuntimeError("quality_gate_origin_invalid")
import pytest
code=pytest.main(sys.argv[1:])
if violations:
    raise RuntimeError(sorted(violations)[0])
for name,module in sorted(sys.modules.items()):
    if not first_party(name):
        continue
    raw_origin=getattr(module,"__file__",None)
    if raw_origin is None:
        raise RuntimeError("first_party_origin_missing")
    confined(raw_origin)
ordered_origins=sorted(origins)
origin_bytes=json.dumps(ordered_origins,ensure_ascii=True,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
report={
    "module_count":len(ordered_origins),
    "module_origins_sha256":hashlib.sha256(origin_bytes).hexdigest(),
    "schema":"friday.clean-artifact-import-origin.v1",
    "site_packages_ref":site_ref,
    "source_commit":source_commit,
    "subprocess_policy":"cpython_audit_deny",
    "wheel_sha256":wheel_sha256,
}
raw=json.dumps(report,ensure_ascii=True,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
descriptor=os.open(report_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_CLOEXEC,0o600)
try:
    view=memoryview(raw)
    while view:
        written=os.write(descriptor,view)
        if written < 1:
            raise RuntimeError("origin_report_write_failed")
        view=view[written:]
    os.fsync(descriptor)
finally:
    os.close(descriptor)
raise SystemExit(code)
"""

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
    "clean artifact path": ("installed_journey_suite",),
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

_CONVERSATION_CLEAN_ARTIFACT_REFS = (
    *_PROMOTED_WINDOW_REFS,
    *_parameterized_refs(
        "tests/test_message_window_runtime_integration.py::"
        "test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried",
        ("content", "snapshot", "insert"),
    ),
    "tests/test_archive_search_runtime_publication.py::"
    "test_real_router_preserves_two_exact_archive_pages_through_final_answer",
)
_DOCUMENT_CLEAN_ARTIFACT_REFS = (
    "tests/test_v12_file_evidence_reader.py::test_current_turn_native_files_form_one_process_owned_bundle",
    "tests/test_v12_file_evidence_reader.py::test_reader_contract_matches_real_ingestion_projections",
    "tests/test_archive_search_runtime_publication.py::"
    "test_natural_selected_document_question_uses_bound_preingestion_v12_without_ordinary_paths",
)
_DURABLE_CLEAN_ARTIFACT_REFS = (
    "tests/test_a_reminder_is_set_before_the_model_speaks.py::"
    "test_the_reminder_is_set_without_asking_the_model",
    "tests/test_durable_scheduled_work_recovery.py::test_two_workers_only_one_claims_pending_task",
    *_parameterized_refs(
        "tests/test_durable_scheduled_work_recovery.py::"
        "test_post_checkpoint_failure_is_uncertain_and_never_replayed",
        ("exception", "cancelled"),
    ),
    "tests/test_reminder_send_edge_storage.py::test_two_storage_workers_get_one_due_reminder_body",
    *_parameterized_refs(
        "tests/test_reminder_send_edge_storage.py::"
        "test_pending_reminder_cannot_be_settled_without_send_edge_claim",
        ("sent", "failed", "uncertain"),
    ),
    "tests/test_reminder_delivery_fence.py::test_lost_ack_reacks_off_page_after_restart_without_resend",
    "tests/test_release_bound_reminder_scan.py::"
    "test_release_evidence_scan_stops_at_exact_ten_pages_of_two_hundred",
    "tests/test_release_bound_reminder_scan.py::"
    "test_release_evidence_scan_stops_when_continuation_cursor_is_missing",
)
_HONEST_DEGRADATION_CLEAN_ARTIFACT_REFS = (
    "tests/test_search_provider_refusal_is_not_emptiness.py::"
    "test_202_from_duckduckgo_is_a_refusal_not_an_empty_result[asyncio]",
    "tests/test_search_provider_refusal_is_not_emptiness.py::"
    "test_a_provider_that_honestly_found_nothing_is_not_a_refusal[asyncio]",
    "tests/test_search_provider_refusal_is_not_emptiness.py::"
    "test_the_chain_moves_on_when_the_first_provider_refuses[asyncio]",
    *_parameterized_refs(
        "tests/test_message_window_runtime_integration.py::"
        "test_final_message_snapshot_drift_is_unavailable_source_free_and_not_retried",
        ("content", "snapshot", "insert"),
    ),
    "tests/test_message_window_work_item_runtime.py::"
    "test_post_boundary_admission_race_returns_atomic_clarification_without_execution",
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
    ("conversation_recall", "clean artifact path"): _CONVERSATION_CLEAN_ARTIFACT_REFS,
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
    ("document_recall_answer", "clean artifact path"): _DOCUMENT_CLEAN_ARTIFACT_REFS,
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
    ("durable_scheduled_work", "clean artifact path"): _DURABLE_CLEAN_ARTIFACT_REFS,
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
    ("honest_degradation", "clean artifact path"): _HONEST_DEGRADATION_CLEAN_ARTIFACT_REFS,
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
_TEST_PATH = re.compile(r"tests/(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_]{1,180}\.py\Z")
_TEST_NAME = re.compile(r"test_[A-Za-z0-9_]{1,159}\Z")
_PARAMETER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
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
_MANIFEST_FIELDS = frozenset({"$schema", "journey_id", "evidence_class", "result", "release", "observation"})
_MANIFEST_OBSERVATION_FIELDS = frozenset(
    {
        "environment",
        "observed_at_utc",
        "check_ids",
        "artifact_ref",
        "artifact_schema",
        "artifact_sha256",
    }
)
_EXECUTION_FIELDS = frozenset(
    {
        "collection_sha256",
        "exit_code",
        "outcome_projection_sha256",
        "producer_path",
        "producer_source_sha256",
        "runner",
    }
)
_ARTIFACT_IMPORT_FIELDS = frozenset({"origin_report_sha256", "site_packages_ref", "subprocess_policy"})


class ExactReleaseEvidenceError(ValueError):
    """One closed validation or production failure."""


_EXECUTION_WITNESS_AUTHORITY = object()
_EVIDENCE_BUNDLE_AUTHORITY = object()
_RELEASE_RUNTIME_AUTHORITY = object()


@dataclass(frozen=True, slots=True)
class _ExecutionWitness:
    """Process-local proof returned only by the closed pytest runner."""

    outcomes: tuple[str, ...]
    exit_code: int
    collection_sha256: str
    outcome_projection_sha256: str
    authority: object
    artifact_origin_sha256: str | None = None
    site_packages_ref: str | None = None
    subprocess_policy: str | None = None


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Process-authenticated canonical receipt/manifest bytes and references."""

    receipt_ref: str
    receipt: bytes
    receipt_sha256: str
    manifest_ref: str
    manifest: bytes
    manifest_sha256: str
    result: str
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
class _AuthenticatedReleaseRuntime:
    """Process-local authority for one sealed installed package surface."""

    root: Path
    identity: ReleaseIdentity
    site_packages: Path
    site_packages_ref: str
    package_root: Path
    authority: object


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


def receipt_schema(evidence_class: str) -> str:
    if type(evidence_class) is not str or evidence_class not in ENVIRONMENT_BY_CLASS:
        raise ExactReleaseEvidenceError("evidence_class_invalid")
    return CLEAN_ARTIFACT_RECEIPT_SCHEMA if evidence_class == _CLEAN_ARTIFACT_CLASS else RECEIPT_SCHEMA


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


def _load_canonical_manifest(raw: bytes) -> dict[str, Any]:
    if type(raw) is not bytes or not raw or len(raw) > 65_536:
        raise ExactReleaseEvidenceError("manifest_json_invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_closed_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ExactReleaseEvidenceError("manifest_json_invalid")
            ),
        )
    except (UnicodeError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ExactReleaseEvidenceError("manifest_json_invalid") from exc
    if type(value) is not dict:
        raise ExactReleaseEvidenceError("manifest_json_invalid")
    try:
        canonical = canonical_json_bytes(value)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ExactReleaseEvidenceError("manifest_json_invalid") from exc
    if raw != canonical:
        raise ExactReleaseEvidenceError("manifest_json_invalid")
    return value


def release_binding_sha256(identity: ReleaseIdentity) -> str:
    """Hash all four exact release fields for deterministic artifact names."""

    return hashlib.sha256(canonical_json_bytes(_release_payload(identity))).hexdigest()


def _evidence_ref(
    *,
    identity: ReleaseIdentity,
    journey_id: str,
    evidence_class: str,
    result: str,
    kind: str,
) -> str:
    proof_refs(journey_id, evidence_class)
    if result not in {"VERIFIED", "FAILED"} or kind not in {"receipts", "manifests"}:
        raise ExactReleaseEvidenceError("evidence_ref_invalid")
    environment = ENVIRONMENT_BY_CLASS.get(evidence_class)
    if environment is None or _SAFE_ID.fullmatch(environment) is None:
        raise ExactReleaseEvidenceError("evidence_ref_invalid")
    filename = f"{journey_id}--{environment}--{result.lower()}--{release_binding_sha256(identity)}.json"
    return str(_EVIDENCE_ROOT / kind / filename)


def _bundle_from_receipt(
    raw: bytes,
    *,
    identity: ReleaseIdentity,
    journey_id: str,
    evidence_class: str,
) -> EvidenceBundle:
    receipt = _load_canonical_receipt(raw)
    expected_release = _release_payload(identity)
    result = receipt.get("result")
    expected_schema = receipt_schema(evidence_class)
    if (
        set(receipt) != _RECEIPT_FIELDS
        or receipt.get("$schema") != expected_schema
        or receipt.get("journey_id") != journey_id
        or receipt.get("evidence_class") != evidence_class
        or receipt.get("environment") != ENVIRONMENT_BY_CLASS.get(evidence_class)
        or receipt.get("check_ids") != _check_ids(journey_id, evidence_class)
        or receipt.get("release") != expected_release
        or result not in {"VERIFIED", "FAILED"}
    ):
        raise ExactReleaseEvidenceError("bundle_receipt_invalid")
    observed_at = receipt.get("observed_at_utc")
    if (
        type(observed_at) is not str
        or re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            observed_at,
        )
        is None
    ):
        raise ExactReleaseEvidenceError("bundle_receipt_invalid")
    try:
        datetime.strptime(observed_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ExactReleaseEvidenceError("bundle_receipt_invalid") from exc
    receipt_ref = _evidence_ref(
        identity=identity,
        journey_id=journey_id,
        evidence_class=evidence_class,
        result=result,
        kind="receipts",
    )
    receipt_sha256 = hashlib.sha256(raw).hexdigest()
    manifest_ref = _evidence_ref(
        identity=identity,
        journey_id=journey_id,
        evidence_class=evidence_class,
        result=result,
        kind="manifests",
    )
    manifest_value = {
        "$schema": MANIFEST_SCHEMA,
        "evidence_class": evidence_class,
        "journey_id": journey_id,
        "observation": {
            "artifact_ref": receipt_ref,
            "artifact_schema": expected_schema,
            "artifact_sha256": receipt_sha256,
            "check_ids": receipt["check_ids"],
            "environment": receipt["environment"],
            "observed_at_utc": observed_at,
        },
        "release": expected_release,
        "result": result,
    }
    manifest = canonical_json_bytes(manifest_value)
    loaded_manifest = _load_canonical_manifest(manifest)
    observation = loaded_manifest.get("observation")
    if (
        set(loaded_manifest) != _MANIFEST_FIELDS
        or type(observation) is not dict
        or set(observation) != _MANIFEST_OBSERVATION_FIELDS
    ):
        raise ExactReleaseEvidenceError("manifest_fields_invalid")
    return EvidenceBundle(
        receipt_ref=receipt_ref,
        receipt=raw,
        receipt_sha256=receipt_sha256,
        manifest_ref=manifest_ref,
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        result=result,
        authority=_EVIDENCE_BUNDLE_AUTHORITY,
    )


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
        name.endswith("]") and _PARAMETER_ID.fullmatch(parameter_id[:-1]) is not None
    )
    if (
        _TEST_PATH.fullmatch(path) is None
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
    *,
    artifact_origin_sha256: str | None = None,
    site_packages_ref: str | None = None,
    subprocess_policy: str | None = None,
) -> _ExecutionWitness:
    artifact_values = (artifact_origin_sha256, site_packages_ref, subprocess_policy)
    artifact_valid = all(value is None for value in artifact_values) or (
        type(artifact_origin_sha256) is str
        and _SHA256.fullmatch(artifact_origin_sha256) is not None
        and type(site_packages_ref) is str
        and re.fullmatch(r"venv/lib/python[0-9]+\.[0-9]+/site-packages", site_packages_ref) is not None
        and subprocess_policy == _SUBPROCESS_POLICY
    )
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
        or not artifact_valid
    ):
        raise ExactReleaseEvidenceError("pytest_execution_evidence_invalid")
    return _ExecutionWitness(
        outcomes,
        exit_code,
        collection_sha256,
        outcome_projection_sha256,
        _EXECUTION_WITNESS_AUTHORITY,
        artifact_origin_sha256,
        site_packages_ref,
        subprocess_policy,
    )


def _require_execution_witness(value: object) -> _ExecutionWitness:
    if type(value) is not _ExecutionWitness or value.authority is not _EXECUTION_WITNESS_AUTHORITY:
        raise ExactReleaseEvidenceError("pytest_execution_evidence_invalid")
    return _execution_witness(
        value.outcomes,
        value.exit_code,
        value.collection_sha256,
        value.outcome_projection_sha256,
        artifact_origin_sha256=value.artifact_origin_sha256,
        site_packages_ref=value.site_packages_ref,
        subprocess_policy=value.subprocess_policy,
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


def _artifact_origin_report_sha256(
    path: Path,
    runtime: _AuthenticatedReleaseRuntime,
) -> str:
    try:
        raw = _stable_file(path, 4096)
        value = json.loads(
            raw.decode("ascii", errors="strict"),
            object_pairs_hook=_closed_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ExactReleaseEvidenceError("artifact_origin_report_invalid")
            ),
        )
    except (OSError, UnicodeError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        raise ExactReleaseEvidenceError("artifact_origin_report_invalid") from exc
    fields = {
        "module_count",
        "module_origins_sha256",
        "schema",
        "site_packages_ref",
        "source_commit",
        "subprocess_policy",
        "wheel_sha256",
    }
    if (
        type(value) is not dict
        or set(value) != fields
        or raw != canonical_json_bytes(value)
        or value.get("schema") != "friday.clean-artifact-import-origin.v1"
        or type(value.get("module_count")) is not int
        or not 1 <= value["module_count"] <= 10_000
        or _SHA256.fullmatch(str(value.get("module_origins_sha256") or "")) is None
        or value.get("site_packages_ref") != runtime.site_packages_ref
        or value.get("source_commit") != runtime.identity.source_commit
        or value.get("wheel_sha256") != runtime.identity.wheel_sha256
        or value.get("subprocess_policy") != _SUBPROCESS_POLICY
    ):
        raise ExactReleaseEvidenceError("artifact_origin_report_invalid")
    return hashlib.sha256(raw).hexdigest()


def _run_closed_pytest(
    repo_root: Path,
    identity: ReleaseIdentity,
    journey_id: str,
    evidence_class: str,
    *,
    require_running_producer: bool = True,
    release_runtime: _AuthenticatedReleaseRuntime | None = None,
) -> _ExecutionWitness:
    nodeids = proof_refs(journey_id, evidence_class)
    clean_artifact = evidence_class == _CLEAN_ARTIFACT_CLASS
    if clean_artifact:
        runtime = _require_release_runtime(release_runtime, identity)
    elif release_runtime is not None:
        raise ExactReleaseEvidenceError("release_runtime_unexpected")
    else:
        runtime = None
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
    artifact_origin_sha256: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="friday-exact-evidence-") as temporary:
            scratch = Path(temporary)
            report = scratch / "results.xml"
            collection = scratch / "collection.json"
            python_cache = scratch / "python-cache"
            origin_report = scratch / "artifact-origin.json"
            if runtime is None:
                bootstrap = (_PYTEST_BOOTSTRAP, str(repo_root))
                artifact_options: tuple[str, ...] = ()
            else:
                bootstrap = (
                    _INSTALLED_PYTEST_BOOTSTRAP,
                    str(repo_root),
                    str(runtime.root),
                    str(runtime.site_packages),
                    runtime.site_packages_ref,
                    str(origin_report),
                    identity.source_commit,
                    identity.wheel_sha256,
                )
                artifact_options = ("-o", "pythonpath=", "--import-mode=importlib")
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
                        *bootstrap,
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
                        *artifact_options,
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
            if runtime is not None:
                artifact_origin_sha256 = _artifact_origin_report_sha256(origin_report, runtime)
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
        if runtime is not None:
            try:
                _reauthenticate_release_runtime(runtime)
            except ExactReleaseEvidenceError as exc:
                if run_error is None:
                    run_error = exc
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
        artifact_origin_sha256=artifact_origin_sha256,
        site_packages_ref=(None if runtime is None else runtime.site_packages_ref),
        subprocess_policy=(None if runtime is None else _SUBPROCESS_POLICY),
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
    release_runtime: _AuthenticatedReleaseRuntime | None = None,
) -> bytes:
    """Run the code-owned tests; callers cannot provide a result or runner."""

    release_payload = _release_payload(identity)
    owner_smoke_payload = _owner_smoke_payload(owner_smoke)
    root = _resolve_directory(repo_root, "repo_root_invalid")
    clean_artifact = evidence_class == _CLEAN_ARTIFACT_CLASS
    if clean_artifact:
        runtime = _require_release_runtime(release_runtime, identity)
    elif release_runtime is not None:
        raise ExactReleaseEvidenceError("release_runtime_unexpected")
    else:
        runtime = None
    producer_sha256, proofs = _source_proofs(root, identity, journey_id, evidence_class)
    if runtime is None:
        witness = _run_closed_pytest(root, identity, journey_id, evidence_class)
    else:
        witness = _run_closed_pytest(
            root,
            identity,
            journey_id,
            evidence_class,
            release_runtime=runtime,
        )
    for proof, outcome in zip(proofs, witness.outcomes, strict=True):
        proof["outcome"] = outcome
    result = "VERIFIED" if witness.exit_code == 0 else "FAILED"
    execution: dict[str, object] = {
        "collection_sha256": witness.collection_sha256,
        "exit_code": witness.exit_code,
        "outcome_projection_sha256": witness.outcome_projection_sha256,
        "producer_path": PRODUCER_PATH,
        "producer_source_sha256": producer_sha256,
        "runner": "pytest",
    }
    if runtime is not None:
        execution["artifact_import"] = {
            "origin_report_sha256": witness.artifact_origin_sha256,
            "site_packages_ref": witness.site_packages_ref,
            "subprocess_policy": witness.subprocess_policy,
        }
    receipt = {
        "$schema": receipt_schema(evidence_class),
        "check_ids": _check_ids(journey_id, evidence_class),
        "environment": ENVIRONMENT_BY_CLASS[evidence_class],
        "evidence_class": evidence_class,
        "execution": execution,
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
        release_runtime=runtime,
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
    release_runtime: _AuthenticatedReleaseRuntime | None = None,
) -> dict[str, Any]:
    """Validate against external release and already-authenticated owner roots.

    ``authenticated_owner_smoke`` is an expected value from a separate
    authenticator.  The embedded object, a boolean, or an artifact path alone
    never establishes owner authority.
    """

    expected_release_payload = _release_payload(expected_release)
    clean_artifact = expected_evidence_class == _CLEAN_ARTIFACT_CLASS
    if clean_artifact:
        runtime = _require_release_runtime(release_runtime, expected_release)
    elif release_runtime is not None:
        raise ExactReleaseEvidenceError("release_runtime_unexpected")
    else:
        runtime = None
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
        value.get("$schema") != receipt_schema(expected_evidence_class)
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
    execution_fields = set(_EXECUTION_FIELDS)
    if clean_artifact:
        execution_fields.add("artifact_import")
    if type(execution) is not dict or set(execution) != execution_fields:
        raise ExactReleaseEvidenceError("execution_binding_invalid")
    artifact_import = execution.get("artifact_import")
    if clean_artifact:
        assert runtime is not None
        if (
            type(artifact_import) is not dict
            or set(artifact_import) != _ARTIFACT_IMPORT_FIELDS
            or _SHA256.fullmatch(str(artifact_import.get("origin_report_sha256") or "")) is None
            or artifact_import.get("site_packages_ref") != runtime.site_packages_ref
            or artifact_import.get("subprocess_policy") != _SUBPROCESS_POLICY
        ):
            raise ExactReleaseEvidenceError("artifact_execution_binding_invalid")
    elif artifact_import is not None:
        raise ExactReleaseEvidenceError("artifact_execution_binding_invalid")
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

    if execution_witness is None and runtime is None:
        witness = _run_closed_pytest(
            root,
            expected_release,
            expected_journey_id,
            expected_evidence_class,
            require_running_producer=require_running_producer,
        )
    elif execution_witness is None:
        witness = _run_closed_pytest(
            root,
            expected_release,
            expected_journey_id,
            expected_evidence_class,
            require_running_producer=require_running_producer,
            release_runtime=runtime,
        )
    else:
        witness = execution_witness
    witness = _require_execution_witness(witness)
    if (
        tuple(outcomes) != witness.outcomes
        or execution.get("exit_code") != witness.exit_code
        or execution.get("collection_sha256") != witness.collection_sha256
        or execution.get("outcome_projection_sha256") != witness.outcome_projection_sha256
        or witness.outcome_projection_sha256 != _outcome_projection_sha256(refs, witness.outcomes)
        or (
            clean_artifact
            and (
                witness.artifact_origin_sha256 != artifact_import.get("origin_report_sha256")
                or witness.site_packages_ref != artifact_import.get("site_packages_ref")
                or witness.subprocess_policy != artifact_import.get("subprocess_policy")
            )
        )
        or (
            not clean_artifact
            and any(
                item is not None
                for item in (
                    witness.artifact_origin_sha256,
                    witness.site_packages_ref,
                    witness.subprocess_policy,
                )
            )
        )
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
    release_root: Path | None = None,
) -> dict[str, Any]:
    """Validate structure, then independently rerun the exact closed inventory."""

    if expected_evidence_class == _CLEAN_ARTIFACT_CLASS:
        if release_root is None:
            raise ExactReleaseEvidenceError("release_runtime_required")
        runtime = _authenticate_release_runtime(release_root)
        if runtime.identity != expected_release:
            raise ExactReleaseEvidenceError("release_identity_mismatch")
    elif release_root is not None:
        raise ExactReleaseEvidenceError("release_runtime_unexpected")
    else:
        runtime = None

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
            release_runtime=runtime,
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


def _authenticate_release_runtime(release_root: Path) -> _AuthenticatedReleaseRuntime:
    """Authenticate one sealed release and discover its unique installed package."""

    try:
        root = Path(os.path.abspath(release_root)).resolve(strict=True)
        identity = derive_release_identity(root)
        venv = root / "venv"
        library = venv / "lib"
        for directory in (venv, library):
            if not stat.S_ISDIR(directory.lstat().st_mode) or directory.resolve(strict=True) != directory:
                raise ExactReleaseEvidenceError("release_runtime_invalid")
        candidates = tuple(
            path
            for path in sorted(library.glob("python*/site-packages"))
            if re.fullmatch(r"python[0-9]+\.[0-9]+", path.parent.name) is not None
        )
        if len(candidates) != 1:
            raise ExactReleaseEvidenceError("release_runtime_invalid")
        site_packages = candidates[0]
        python_directory = site_packages.parent
        expected_python_directory = f"python{sys.version_info.major}.{sys.version_info.minor}"
        package_root = site_packages / "friday"
        package_init = package_root / "__init__.py"
        for directory in (python_directory, site_packages, package_root):
            if not stat.S_ISDIR(directory.lstat().st_mode) or directory.resolve(strict=True) != directory:
                raise ExactReleaseEvidenceError("release_runtime_invalid")
        if (
            not stat.S_ISREG(package_init.lstat().st_mode)
            or package_init.resolve(strict=True) != package_init
            or python_directory.name != expected_python_directory
        ):
            raise ExactReleaseEvidenceError("release_runtime_invalid")
        distributions = tuple(sorted(site_packages.glob("friday-*.dist-info")))
        if (
            len(distributions) != 1
            or re.fullmatch(r"friday-[A-Za-z0-9_.+-]+\.dist-info", distributions[0].name) is None
            or not stat.S_ISDIR(distributions[0].lstat().st_mode)
            or distributions[0].resolve(strict=True) != distributions[0]
        ):
            raise ExactReleaseEvidenceError("release_runtime_invalid")
        site_packages_ref = site_packages.relative_to(root).as_posix()
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("release_runtime_invalid") from exc
    return _AuthenticatedReleaseRuntime(
        root=root,
        identity=identity,
        site_packages=site_packages,
        site_packages_ref=site_packages_ref,
        package_root=package_root,
        authority=_RELEASE_RUNTIME_AUTHORITY,
    )


def _require_release_runtime(
    value: object,
    identity: ReleaseIdentity,
) -> _AuthenticatedReleaseRuntime:
    if (
        type(value) is not _AuthenticatedReleaseRuntime
        or value.authority is not _RELEASE_RUNTIME_AUTHORITY
        or value.identity != identity
        or not value.root.is_absolute()
        or value.site_packages != value.root / value.site_packages_ref
        or value.package_root != value.site_packages / "friday"
        or PurePosixPath(value.site_packages_ref).is_absolute()
        or any(part in {"", ".", ".."} for part in PurePosixPath(value.site_packages_ref).parts)
    ):
        raise ExactReleaseEvidenceError("release_runtime_not_authenticated")
    return value


def _reauthenticate_release_runtime(runtime: _AuthenticatedReleaseRuntime) -> None:
    refreshed = _authenticate_release_runtime(runtime.root)
    projection = ("root", "identity", "site_packages", "site_packages_ref", "package_root")
    if any(getattr(runtime, field) != getattr(refreshed, field) for field in projection):
        raise ExactReleaseEvidenceError("release_identity_changed")


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

    if evidence_class == _CLEAN_ARTIFACT_CLASS:
        raise ExactReleaseEvidenceError("clean_artifact_bundle_required")
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


def manifest_from_receipt(
    raw: bytes,
    *,
    expected_release: ReleaseIdentity,
    expected_journey_id: str,
    expected_evidence_class: str,
    repo_root: Path,
    authenticated_owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
    release_root: Path | None = None,
) -> EvidenceBundle:
    """Revalidate machine evidence and derive its only canonical manifest."""

    validate_receipt(
        raw,
        expected_release=expected_release,
        expected_journey_id=expected_journey_id,
        expected_evidence_class=expected_evidence_class,
        repo_root=repo_root,
        authenticated_owner_smoke=authenticated_owner_smoke,
        release_root=release_root,
    )
    return _bundle_from_receipt(
        raw,
        identity=expected_release,
        journey_id=expected_journey_id,
        evidence_class=expected_evidence_class,
    )


def produce_evidence_bundle(
    *,
    repo_root: Path,
    release_root: Path,
    journey_id: str,
    evidence_class: str,
    authenticated_owner_smoke: AuthenticatedOwnerSmokeBinding | None = None,
) -> EvidenceBundle:
    """Run the closed verifier and derive receipt plus manifest without caller claims."""

    if evidence_class == _CLEAN_ARTIFACT_CLASS:
        runtime = _authenticate_release_runtime(release_root)
        identity = runtime.identity
    else:
        runtime = None
        identity = derive_release_identity(release_root)
    if runtime is None:
        raw = _produce_for_identity(
            repo_root=repo_root,
            identity=identity,
            journey_id=journey_id,
            evidence_class=evidence_class,
            owner_smoke=authenticated_owner_smoke,
        )
    else:
        raw = _produce_for_identity(
            repo_root=repo_root,
            identity=identity,
            journey_id=journey_id,
            evidence_class=evidence_class,
            owner_smoke=authenticated_owner_smoke,
            release_runtime=runtime,
        )
    if runtime is None:
        if derive_release_identity(release_root) != identity:
            raise ExactReleaseEvidenceError("release_identity_changed")
    else:
        _reauthenticate_release_runtime(runtime)
    return _bundle_from_receipt(
        raw,
        identity=identity,
        journey_id=journey_id,
        evidence_class=evidence_class,
    )


def _cleanup_owned_target(target: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        current = os.lstat(target)
        if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
            os.unlink(target)
    except OSError:
        pass


def _write_canonical_exclusive(
    path: Path,
    raw: bytes,
    *,
    failure_code: str,
) -> tuple[str, tuple[int, int]]:
    try:
        target = Path(os.path.abspath(path))
        parent = target.parent.resolve(strict=True)
    except (OSError, TypeError, ValueError, RuntimeError) as exc:
        raise ExactReleaseEvidenceError(failure_code) from exc
    if parent != target.parent or target.name in {"", ".", ".."}:
        raise ExactReleaseEvidenceError(failure_code)
    descriptor = -1
    staging: Path | None = None
    owned_identity: tuple[int, int] | None = None
    linked = False
    try:
        descriptor, staging_text = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=parent,
        )
        staging = Path(staging_text)
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
            or status.st_uid != os.geteuid()
            or status.st_size != len(raw)
        ):
            raise OSError("receipt postcondition failed")
        os.close(descriptor)
        descriptor = -1
        os.link(staging, target, follow_symlinks=False)
        linked = True
        published = os.lstat(target)
        if (
            not stat.S_ISREG(published.st_mode)
            or (published.st_dev, published.st_ino) != owned_identity
            or stat.S_IMODE(published.st_mode) != 0o600
            or published.st_nlink != 2
            or published.st_uid != os.geteuid()
            or published.st_size != len(raw)
        ):
            raise OSError("receipt publication postcondition failed")
        os.unlink(staging)
        staging = None
        published = os.lstat(target)
        if (published.st_dev, published.st_ino) != owned_identity or published.st_nlink != 1:
            raise OSError("receipt publication link count invalid")
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return hashlib.sha256(raw).hexdigest(), owned_identity
    except BaseException as exc:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if staging is not None:
            _cleanup_owned_target(staging, owned_identity)
        if linked:
            _cleanup_owned_target(target, owned_identity)
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise ExactReleaseEvidenceError(failure_code) from exc


def write_receipt_exclusive(path: Path, raw: bytes) -> str:
    """Write one complete canonical receipt without replacing an existing name."""

    receipt = _load_canonical_receipt(raw)
    if receipt.get("$schema") == CLEAN_ARTIFACT_RECEIPT_SCHEMA:
        raise ExactReleaseEvidenceError("clean_artifact_bundle_required")
    digest, _identity = _write_canonical_exclusive(
        path,
        raw,
        failure_code="receipt_output_invalid",
    )
    return digest


def _require_evidence_bundle(value: object) -> EvidenceBundle:
    if type(value) is not EvidenceBundle or value.authority is not _EVIDENCE_BUNDLE_AUTHORITY:
        raise ExactReleaseEvidenceError("evidence_bundle_invalid")
    if (
        _SHA256.fullmatch(value.receipt_sha256) is None
        or _SHA256.fullmatch(value.manifest_sha256) is None
        or hashlib.sha256(value.receipt).hexdigest() != value.receipt_sha256
        or hashlib.sha256(value.manifest).hexdigest() != value.manifest_sha256
        or value.result not in {"VERIFIED", "FAILED"}
    ):
        raise ExactReleaseEvidenceError("evidence_bundle_invalid")
    receipt = _load_canonical_receipt(value.receipt)
    manifest = _load_canonical_manifest(value.manifest)
    observation = manifest.get("observation")
    release = receipt.get("release")
    if (
        set(manifest) != _MANIFEST_FIELDS
        or type(observation) is not dict
        or set(observation) != _MANIFEST_OBSERVATION_FIELDS
        or type(release) is not dict
        or set(release) != {"database_schema", "source_commit", "tree_sha256", "wheel_sha256"}
        or manifest.get("result") != value.result
        or observation.get("artifact_ref") != value.receipt_ref
        or observation.get("artifact_sha256") != value.receipt_sha256
        or receipt.get("result") != value.result
        or not value.manifest_ref.startswith(str(_EVIDENCE_ROOT / "manifests") + "/")
        or not value.receipt_ref.startswith(str(_EVIDENCE_ROOT / "receipts") + "/")
    ):
        raise ExactReleaseEvidenceError("evidence_bundle_invalid")
    try:
        identity = ReleaseIdentity(
            source_commit=release["source_commit"],
            tree_sha256=release["tree_sha256"],
            wheel_sha256=release["wheel_sha256"],
            database_schema=release["database_schema"],
        )
        expected = _bundle_from_receipt(
            value.receipt,
            identity=identity,
            journey_id=receipt["journey_id"],
            evidence_class=receipt["evidence_class"],
        )
    except (KeyError, TypeError, ExactReleaseEvidenceError) as exc:
        raise ExactReleaseEvidenceError("evidence_bundle_invalid") from exc
    comparable = (
        "receipt_ref",
        "receipt",
        "receipt_sha256",
        "manifest_ref",
        "manifest",
        "manifest_sha256",
        "result",
    )
    if any(getattr(value, field) != getattr(expected, field) for field in comparable):
        raise ExactReleaseEvidenceError("evidence_bundle_invalid")
    return value


def _ensure_bundle_parent(root: Path, ref: str) -> Path:
    candidate = PurePosixPath(ref)
    if (
        candidate.is_absolute()
        or str(candidate) != ref
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or tuple(candidate.parts[:2]) != ("evidence", "golden_journeys")
    ):
        raise ExactReleaseEvidenceError("bundle_output_invalid")
    parent = root
    try:
        for part in candidate.parts[:-1]:
            parent /= part
            with suppress(FileExistsError):
                parent.mkdir(mode=0o700)
            if not stat.S_ISDIR(parent.lstat().st_mode) or parent.resolve(strict=True) != parent:
                raise ExactReleaseEvidenceError("bundle_output_invalid")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, ExactReleaseEvidenceError):
            raise
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc
    return root / ref


def _require_exact_existing_bundle_file(path: Path, raw: bytes) -> str:
    """Adopt only an already-complete byte-identical create-only artifact."""

    try:
        existing = _stable_file(path, len(raw))
        status = path.lstat()
        if status.st_nlink == 2:
            prefix = f".{path.name}."
            candidates = []
            for candidate in path.parent.iterdir():
                if candidate.name.startswith(prefix) and candidate.name.endswith(".tmp"):
                    candidate_status = candidate.lstat()
                    if (candidate_status.st_dev, candidate_status.st_ino) == (
                        status.st_dev,
                        status.st_ino,
                    ):
                        candidates.append(candidate)
            if len(candidates) != 1:
                raise ExactReleaseEvidenceError("bundle_output_invalid")
            os.unlink(candidates[0])
            status = path.lstat()
    except (OSError, RuntimeError, ExactReleaseEvidenceError) as exc:
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc
    if (
        existing != raw
        or not stat.S_ISREG(status.st_mode)
        or stat.S_IMODE(status.st_mode) != 0o600
        or status.st_nlink != 1
        or status.st_uid != os.geteuid()
        or status.st_size != len(raw)
    ):
        raise ExactReleaseEvidenceError("bundle_output_invalid")
    return hashlib.sha256(existing).hexdigest()


@contextmanager
def _exclusive_bundle_root(root: Path) -> Iterator[None]:
    """Serialize create-only publication; a process crash releases the directory lock."""

    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(root, flags)
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
            raise ExactReleaseEvidenceError("bundle_output_invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except (OSError, RuntimeError) as exc:
        raise ExactReleaseEvidenceError("bundle_output_invalid") from exc
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            with suppress(OSError):
                os.close(descriptor)


def write_evidence_bundle_exclusive(output_root: Path, bundle: EvidenceBundle) -> dict[str, str]:
    """Publish a manifest-committed bundle, healing only byte-identical prior writes."""

    exact = _require_evidence_bundle(bundle)
    root = _resolve_directory(output_root, "bundle_output_invalid")
    with _exclusive_bundle_root(root):
        receipt_path = _ensure_bundle_parent(root, exact.receipt_ref)
        manifest_path = _ensure_bundle_parent(root, exact.manifest_ref)
        receipt_identity: tuple[int, int] | None = None
        try:
            try:
                receipt_digest, receipt_identity = _write_canonical_exclusive(
                    receipt_path,
                    exact.receipt,
                    failure_code="bundle_output_invalid",
                )
            except ExactReleaseEvidenceError:
                receipt_digest = _require_exact_existing_bundle_file(receipt_path, exact.receipt)
            try:
                manifest_digest, _manifest_identity = _write_canonical_exclusive(
                    manifest_path,
                    exact.manifest,
                    failure_code="bundle_output_invalid",
                )
            except ExactReleaseEvidenceError:
                manifest_digest = _require_exact_existing_bundle_file(manifest_path, exact.manifest)
        except BaseException:
            _cleanup_owned_target(receipt_path, receipt_identity)
            raise
        return {
            "manifest_ref": exact.manifest_ref,
            "manifest_sha256": manifest_digest,
            "receipt_ref": exact.receipt_ref,
            "receipt_sha256": receipt_digest,
            "result": exact.result,
        }


def _external_bundle_output_root(repo_root: Path, output_root: Path) -> Path:
    repository = _resolve_directory(repo_root, "repo_root_invalid")
    output = _resolve_directory(output_root, "bundle_output_invalid")
    if output == repository or output.is_relative_to(repository):
        raise ExactReleaseEvidenceError("bundle_output_must_be_external")
    return output


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
        choices=sorted({key[1] for key in _PROOF_REFS_BY_JOURNEY_CLASS} - {"clean artifact path"}),
    )
    run.add_argument("--output", required=True, type=Path)
    bundle = commands.add_parser("bundle")
    bundle.add_argument("--release-root", required=True, type=Path)
    bundle.add_argument("--repo-root", required=True, type=Path)
    bundle.add_argument(
        "--journey-id",
        required=True,
        choices=sorted(
            journey_id
            for journey_id, evidence_class in _PROOF_REFS_BY_JOURNEY_CLASS
            if evidence_class == "clean artifact path"
        ),
    )
    bundle.add_argument("--output-root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "bundle":
            output_root = _external_bundle_output_root(args.repo_root, args.output_root)
            bundle = produce_evidence_bundle(
                repo_root=args.repo_root,
                release_root=args.release_root,
                journey_id=args.journey_id,
                evidence_class="clean artifact path",
            )
            published = write_evidence_bundle_exclusive(output_root, bundle)
            print(canonical_json_bytes(published).decode())
            return 0 if published["result"] == "VERIFIED" else 1
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
