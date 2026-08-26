#!/usr/bin/env python3
"""Build and activate wheel-only Friday releases without a mixed source tree.

The builder creates a previously absent sibling directory, installs only from an
offline wheelhouse, runs the installed interpreter with ``-I -B``, seals every
byte, and publishes the sibling by one rename.  The activation state machine is
deliberately injected: production systemd/HTTP glue and tests use the same
ordering and rollback policy, while no import from the candidate is ever made
inside this controller process.

Public receipts contain hashes, counts, schema numbers and closed status codes.
They never contain environment values, Telegram payloads, filenames, chat ids,
sender ids, or database paths.
"""

from __future__ import annotations

import argparse
import base64
import csv
import fcntl
import hashlib
import importlib
import io
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

OPERATOR_SCHEMA = "friday.immutable-release-operator.v1"
BUILD_RECEIPT_SCHEMA = "friday.immutable-wheel-release.v1"
ACTIVATION_RECEIPT_SCHEMA = "friday.immutable-release-activation.v1"
ACTIVATION_JOURNAL_SCHEMA = "friday.immutable-release-activation-journal.v1"
UNIT_INSTALL_JOURNAL_SCHEMA = "friday.immutable-release-unit-install-journal.v1"
ALBUM_RECOVERY_SCHEMA = "friday.telegram-historical-album-recovery.v1"
ALBUM_RECOVERY_PENDING_RECEIPT_SCHEMA = "friday.telegram-historical-album-recovery-pending-receipt.v1"
ALBUM_RECOVERY_RECEIPT_SCHEMA = "friday.telegram-historical-album-recovery-receipt.v1"
ALBUM_RECOVERY_COMPLETION_EVIDENCE_SCHEMA = "friday.telegram-historical-album-recovery-completion-evidence.v1"
ALBUM_RECOVERY_JOURNAL_SCHEMA = "friday.telegram-historical-album-recovery-journal.v1"
ALIAS_REPAIR_RECEIPT_SCHEMA = "friday.file-alias-release-repair-receipt.v1"
MEMORY_VAULT_MODES = ("disabled", "full_owner")
MEMORY_VAULT_MODE_CONTRACT = "v1"
OBSIDIAN_MODES = ("disabled", "enabled")
OBSIDIAN_CUTOVER_CONTRACT = "exact-root-v1"
VENV_RELOCATION_CONTRACT = "absolute-final-v1"
RUNTIME_CONFIG_SCHEMA_V1 = "friday.immutable-release-runtime-config.v1"
RUNTIME_CONFIG_SCHEMA_V2 = "friday.immutable-release-runtime-config.v2"
RUNTIME_CONFIG_SCHEMA_V3 = "friday.immutable-release-runtime-config.v3"
RUNTIME_CONFIG_SCOPE_SCHEMA = "friday.immutable-release-runtime-config-scope.v1"
RUNTIME_CONFIG_RETRY_SCOPE_SCHEMA = "friday.immutable-release-runtime-config-retry-scope.v1"

HISTORICAL_ALBUM_UPDATE_IDS = tuple(range(102500242, 102500252))
HISTORICAL_ALBUM_PLAN_SHA256 = "6bbee1178d051e80561c5193b3b015410fccfde87c965168f963ad7ade7b8a40"
FORBIDDEN_ROLLBACK_COMMITS = frozenset({"8345179af57a71cc6a64916c275cce5627abfd63"})

_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:[a-z0-9.+-]*)?")
_PIN = re.compile(r"(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^;\s]+)(?:\s*;.*)?$")
_UNIT = re.compile(r"[A-Za-z0-9_.@:-]{1,128}\.service")
_ALBUM_MESSAGE_IDS = tuple(range(1842, 1852))
_HISTORICAL_ALBUM_DEAD_ERROR = "PermanentUpdateError"
HISTORICAL_ALBUM_COMPLETION_TIMEOUT_SEC = 600.0
HISTORICAL_ALBUM_COMPLETION_POLL_SEC = 0.25
MAX_WHEEL_BYTES = 1 << 30
MAX_LOCK_BYTES = 1 << 20
MAX_WHEELHOUSE_FILES = 512
MAX_CONSOLE_SCRIPT_BYTES = 1 << 20
MAX_RECORD_BYTES = 64 << 20
MAX_EXACT_MANIFEST_BYTES = 64 << 20
MAX_SHEBANG_BYTES = 255
MAX_OBSIDIAN_BACKUP_ENTRIES = 100_000
MAX_OBSIDIAN_BACKUP_BYTES = 16 << 30
_SMOKE_SCRATCH_ROOT = Path("/var/tmp/friday-immutable-smoke")
_OBSIDIAN_ENABLE_TRANSITION = "obsidian_enable"
_ENGINEER_MODE_ENABLE_TRANSITION = "engineer_mode_enable"
_SECONDARY_SHADOW_ENABLE_TRANSITION = "secondary_shadow_enable"
_SECONDARY_SHADOW_DISABLE_TRANSITION = "secondary_shadow_disable"
_SECONDARY_SHADOW_TO_PRIVATE_SHADOW_TRANSITION = "secondary_shadow_to_private_shadow"
_SECONDARY_SHADOW_TO_ASSIST_TRANSITION = "secondary_shadow_to_assist"
_SECONDARY_ASSIST_TO_DISABLED_TRANSITION = "secondary_assist_to_disabled"
_SECONDARY_ASSIST_ENABLE_DOCUMENT_MAP_SHADOW_TRANSITION = "secondary_assist_enable_document_map_shadow"
_SECONDARY_DOCUMENT_MAP_SHADOW_TO_ASSIST_TRANSITION = "secondary_document_map_shadow_to_assist"
_SEMANTIC_SUPERVISOR_SHADOW_ENABLE_TRANSITION = "semantic_supervisor_shadow_enable"
_SEMANTIC_SUPERVISOR_SHADOW_DISABLE_TRANSITION = "semantic_supervisor_shadow_disable"
_SEMANTIC_SUPERVISOR_SHADOW_TO_ASSIST_TRANSITION = "semantic_supervisor_shadow_to_assist"
_SEMANTIC_SUPERVISOR_ASSIST_TO_SHADOW_TRANSITION = "semantic_supervisor_assist_to_shadow"
_SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION = "semantic_supervisor_assist_to_canary"
_STAGED_TRANSITION_VALIDATION_SCHEMA = (
    "friday.immutable-release-staged-transition-validation.v1"
)
_SEMANTIC_SUPERVISOR_CANARY_TO_ASSIST_TRANSITION = "semantic_supervisor_canary_to_assist"
_SEMANTIC_SUPERVISOR_SHADOW_POLICY_ID = "gptoss20b-semantic-supervisor-v1"
_SEMANTIC_SUPERVISOR_SHADOW_POLICY_SHA256 = "edea7fce6ae8d9bfcbe461a3f90d98bd9aab897ebe7712cdb23a2d77e8de780c"
_SEMANTIC_SUPERVISOR_ASSIST_POLICY_ID = "gptoss20b-semantic-supervisor-v2"
_SEMANTIC_SUPERVISOR_ASSIST_POLICY_SHA256 = "95dc4ae7e246e7104b1e1cd036ea9706fdb014de6889d69789fca66cec9fd98b"
# P1 compatibility aliases remain source-stable for shadow rollout tests.
_SEMANTIC_SUPERVISOR_POLICY_ID = _SEMANTIC_SUPERVISOR_SHADOW_POLICY_ID
_SEMANTIC_SUPERVISOR_POLICY_SHA256 = _SEMANTIC_SUPERVISOR_SHADOW_POLICY_SHA256
_PRIMARY_PROCESS_EPOCH_DOMAIN = b"friday.primary-process-epoch.v2\0"
_SECONDARY_PRODUCT_STAGE_SCHEMA = "friday.secondary-product-stage-evidence.v3"
_SECONDARY_PRODUCT_OPERATION_SCHEMA = "friday.secondary-product-operation-core.v1"
_SECONDARY_PRODUCT_DIAGNOSTICS_SCHEMA = "friday.secondary-product-diagnostics.v2"
_SECONDARY_PRODUCT_CLEANUP_CORE_SCHEMA = "friday.secondary-product-cleanup-core.v1"
_SECONDARY_PRODUCT_CLEANUP_ZERO_SCHEMA = "friday.secondary-product-cleanup-zero-residue.v1"
_SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_SCHEMA = "friday.secondary-product-rollout-attestation.v1"
_SECONDARY_PRODUCT_CONSUME_REQUEST_SCHEMA = "friday.secondary-product-rollout-consume-request.v1"
_SECONDARY_PRODUCT_CONSUME_RESPONSE_SCHEMA = "friday.secondary-product-rollout-consume-response.v1"
_SECONDARY_PRODUCT_CONSUME_URL = (
    "https://127.0.0.1:8000/api/admin/secondary-product-witness/consume-rollout-attestation"
)
_SECONDARY_DOCUMENT_MAP_CONSUME_URL = (
    "https://127.0.0.1:8000/api/admin/secondary-document-map-witness/consume-rollout-attestation"
)
_SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_CONSUME_URL = (
    "https://127.0.0.1:8000/api/admin/semantic-supervisor-witness/"
    "consume-representative-window-attestation"
)
_SECONDARY_DOCUMENT_MAP_SHADOW_POLICY_ID = "gptoss20b-document-map-v1"
_SECONDARY_DOCUMENT_MAP_SHADOW_POLICY_SHA256 = (
    "7d57947d7ecda675e8a4da3f56332baf32484c08c0504afd7fa420b9c6323cd9"
)
_SECONDARY_DOCUMENT_MAP_ASSIST_POLICY_ID = "gptoss20b-document-map-v2"
# Exact code-owned v2 policy and the fresh one-shot live v1 shadow receipt it
# earned. Neither identity may be supplied or overridden through ENV/CLI.
_SECONDARY_DOCUMENT_MAP_ASSIST_POLICY_SHA256 = (
    "d2ab9b67ff24a54727fec9592dcd0db1c35036e1b5ee91ac6a5daf4d3694e92e"
)
_SECONDARY_DOCUMENT_MAP_ACCEPTED_SHADOW_RECEIPT_SHA256 = (
    "a00f18f8c50a7449d1fa6a357d8d5bb1ca37b0c397c81a96c0e621231bc09e2d"
)
_SECONDARY_DOCUMENT_MAP_RECEIPT_SCHEMA = "friday.secondary-document-map-shadow-receipt.v1"
_SECONDARY_DOCUMENT_MAP_ATTESTATION_SCHEMA = "friday.secondary-document-map-shadow-attestation.v1"
_SECONDARY_DOCUMENT_MAP_CONSUME_REQUEST_SCHEMA = "friday.secondary-document-map-shadow-consume-request.v1"
_SECONDARY_DOCUMENT_MAP_CONSUME_RESPONSE_SCHEMA = "friday.secondary-document-map-shadow-consume-response.v1"
_SECONDARY_PRODUCT_RUNNER_SOURCE = Path(
    "deploy/secondary-brain/windows-sglang/scripts/live_failure_battery.py"
)
_SECONDARY_PRODUCT_RUNNER_ARTIFACT = Path("artifacts/secondary-product-witness-runner.py")
_SECONDARY_ROLLOUT_RECEIPT_STAGE = {
    _SECONDARY_SHADOW_TO_PRIVATE_SHADOW_TRANSITION: "public-shadow",
    _SECONDARY_SHADOW_TO_ASSIST_TRANSITION: "private-shadow",
    _SECONDARY_DOCUMENT_MAP_SHADOW_TO_ASSIST_TRANSITION: "document-map-shadow",
}
_SECONDARY_CONFIG_TRANSITIONS = frozenset(
    {
        _SECONDARY_SHADOW_ENABLE_TRANSITION,
        _SECONDARY_SHADOW_DISABLE_TRANSITION,
        _SECONDARY_SHADOW_TO_PRIVATE_SHADOW_TRANSITION,
        _SECONDARY_SHADOW_TO_ASSIST_TRANSITION,
        _SECONDARY_ASSIST_TO_DISABLED_TRANSITION,
        _SECONDARY_ASSIST_ENABLE_DOCUMENT_MAP_SHADOW_TRANSITION,
        _SECONDARY_DOCUMENT_MAP_SHADOW_TO_ASSIST_TRANSITION,
    }
)
_SEMANTIC_SUPERVISOR_CONFIG_TRANSITIONS = frozenset(
    {
        _SEMANTIC_SUPERVISOR_SHADOW_ENABLE_TRANSITION,
        _SEMANTIC_SUPERVISOR_SHADOW_DISABLE_TRANSITION,
        _SEMANTIC_SUPERVISOR_SHADOW_TO_ASSIST_TRANSITION,
        _SEMANTIC_SUPERVISOR_ASSIST_TO_SHADOW_TRANSITION,
        _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION,
        _SEMANTIC_SUPERVISOR_CANARY_TO_ASSIST_TRANSITION,
    }
)
_SEMANTIC_SUPERVISOR_TRANSITION_MODES = {
    _SEMANTIC_SUPERVISOR_SHADOW_ENABLE_TRANSITION: ("off", "shadow"),
    _SEMANTIC_SUPERVISOR_SHADOW_DISABLE_TRANSITION: ("shadow", "off"),
    _SEMANTIC_SUPERVISOR_SHADOW_TO_ASSIST_TRANSITION: ("shadow", "assist"),
    _SEMANTIC_SUPERVISOR_ASSIST_TO_SHADOW_TRANSITION: ("assist", "shadow"),
    _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION: ("assist", "canary"),
    _SEMANTIC_SUPERVISOR_CANARY_TO_ASSIST_TRANSITION: ("canary", "assist"),
}
_EXACT_ENV_CONFIG_TRANSITIONS = frozenset(
    {
        _ENGINEER_MODE_ENABLE_TRANSITION,
        *_SECONDARY_CONFIG_TRANSITIONS,
        *_SEMANTIC_SUPERVISOR_CONFIG_TRANSITIONS,
    }
)
_STAGED_CONFIG_TRANSITIONS = frozenset(
    {
        _OBSIDIAN_ENABLE_TRANSITION,
        *_EXACT_ENV_CONFIG_TRANSITIONS,
    }
)
_ENGINEER_MODE_ENV_LINES = (
    (
        "FRIDAY_ENGINEER_MODE_ENABLED",
        b"FRIDAY_ENGINEER_MODE_ENABLED=0\n",
        b"FRIDAY_ENGINEER_MODE_ENABLED=1\n",
    ),
    (
        "FRIDAY_HOST_ALLOWED_CIDRS",
        b"FRIDAY_HOST_ALLOWED_CIDRS=\n",
        b"FRIDAY_HOST_ALLOWED_CIDRS=192.168.1.0/24\n",
    ),
)
_ENGINEER_MODE_LEGACY_ENV_KEYS = frozenset(
    {
        "JERICHO_ENGINEER_MODE_ENABLED",
        "JERICHO_HOST_ALLOWED_CIDRS",
    }
)
_SECONDARY_LLM_ENV_PREFIX = "FRIDAY_SECONDARY_LLM_"
_SECONDARY_LLM_ENV_KEYS = frozenset(
    {
        "FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC",
        "FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT",
        "FRIDAY_SECONDARY_LLM_API_KEY",
        "FRIDAY_SECONDARY_LLM_BASE_URL",
        "FRIDAY_SECONDARY_LLM_CALL_BUDGET_SEC",
        "FRIDAY_SECONDARY_LLM_CA_FILE",
        "FRIDAY_SECONDARY_LLM_CONNECT_TIMEOUT_SEC",
        "FRIDAY_SECONDARY_LLM_COOLDOWN_SEC",
        "FRIDAY_SECONDARY_LLM_ENABLED",
        "FRIDAY_SECONDARY_LLM_HEALTH_INTERVAL_SEC",
        "FRIDAY_SECONDARY_LLM_MAX_CONCURRENCY",
        "FRIDAY_SECONDARY_LLM_MAX_CONTEXT_TOKENS",
        "FRIDAY_SECONDARY_LLM_MODE",
        "FRIDAY_SECONDARY_LLM_MODEL",
        "FRIDAY_SECONDARY_LLM_PROFILE",
        "FRIDAY_SECONDARY_LLM_READ_TIMEOUT_SEC",
        "FRIDAY_SECONDARY_LLM_WORKLOADS",
        "FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE",
    }
)
_SEMANTIC_SUPERVISOR_ENV_PREFIX = "FRIDAY_SEMANTIC_SUPERVISOR_"
_SEMANTIC_SUPERVISOR_ENV_KEYS = frozenset(
    {
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS",
        "FRIDAY_SEMANTIC_SUPERVISOR_MODE",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256",
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256",
        "FRIDAY_SEMANTIC_SUPERVISOR_TASKS",
        "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC",
    }
)
_SECONDARY_FINALIST_PROFILE_ID = "gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f"
_SECONDARY_FINALIST_MODEL_ALIAS = f"friday-secondary-{_SECONDARY_FINALIST_PROFILE_ID}"
_SECONDARY_FINALIST_CA_SHA256 = "392756a74fd9100635c42f4fbf7e5a5f1822d18ea898ebb7848b9fdd0bddc1fe"
_SECONDARY_FINALIST_CANDIDATE_PROFILE_SHA256 = (
    "51af2164fa07ff3c01813e318076f7ac8b37eeecb73e695b6ca7543061c93439"
)
_SECONDARY_PRODUCT_MAX_COUNTER = (1 << 63) - 1
_SECONDARY_PRODUCT_FAILURES = frozenset(
    {
        "disabled",
        "misconfigured",
        "mode_disallowed",
        "workload_disallowed",
        "private_text_disallowed",
        "secret_material_denied",
        "unsupported_modality",
        "effect_denied",
        "context_exceeded",
        "admission_busy",
        "cooldown",
        "deadline",
        "connect_failed",
        "timeout",
        "http_transient",
        "http_rejected",
        "auth_rejected",
        "wrong_profile",
        "wrong_model",
        "malformed_response",
        "tool_call_rejected",
        "reasoning_leak",
        "degeneration",
        "cancelled",
    }
)
_SECONDARY_PRODUCT_SNAPSHOT_KEYS = frozenset(
    {
        "schema",
        "role",
        "enabled",
        "configured",
        "mode",
        "state",
        "available",
        "last_failure",
        "profile_id",
        "profile_admission",
        "profile_manifest_match",
        "served_model_match",
        "context_cap_tokens",
        "selected_total",
        "success_total",
        "endpoint_request_total",
        "endpoint_success_total",
        "skipped_total",
        "primary_fallback_total",
        "probe_success_total",
        "probe_failure_total",
        "model_inventory_probe_success_total",
        "model_inventory_probe_failure_total",
        "circuit_retry_after_sec",
        "skip_reasons",
        "fallback_reasons",
        "shadow",
        "workload",
    }
)
_SECONDARY_PRODUCT_SHADOW_KEYS = frozenset({"valid_total", "invalid_total", "skipped_total", "in_flight"})
_SECONDARY_PRODUCT_WORKLOAD_KEYS = frozenset(
    {"name", "selected_total", "success_total", "skip_reasons", "fallback_reasons"}
)
_SECONDARY_PRODUCT_DELTA_KEYS = frozenset(
    {
        "selected_total",
        "success_total",
        "endpoint_request_total",
        "endpoint_success_total",
        "skipped_total",
        "primary_fallback_total",
        "probe_success_total",
        "probe_failure_total",
        "model_inventory_probe_success_total",
        "model_inventory_probe_failure_total",
        "skip_reason_deltas",
        "fallback_reason_deltas",
        "workload_skip_reason_deltas",
        "workload_fallback_reason_deltas",
        "shadow_valid_total",
        "shadow_invalid_total",
        "shadow_skipped_total",
    }
)
_SECONDARY_PRODUCT_OPERATION_KEYS = frozenset(
    {
        "schema",
        "identity_result_sha256",
        "ingest_request_sha256",
        "ingest_result_sha256",
        "ingest_storage_sha256",
        "ingest_idempotent_replay",
        "advice_request_sha256",
        "advice_result_sha256",
        "advice_storage_sha256",
        "advice_diagnostics_receipt_sha256",
        "stage_diagnostics_binding_sha256",
        "advice_proof_sha256",
        "source_ref_sha256",
        "synthetic_content_sha256",
        "synthetic_nonce_sha256",
        "storage_user_id_sha256",
        "uploader_id_sha256",
        "inbox_id_sha256",
        "raw_object_id_sha256",
        "advice_endpoint_role",
        "exact_secondary_model_observed",
        "cleanup_core_sha256",
        "cleanup_status",
        "knowledge_object_created",
        "tool_requested",
        "effect_requested",
    }
)
_SECONDARY_PRODUCT_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "status",
        "stage",
        "candidate_profile_id",
        "candidate_profile_sha256",
        "served_model_alias",
        "gateway_ca_certificate_sha256",
        "observer_source_head",
        "observer_runner_sha256",
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_version",
        "primary_ca_certificate_sha256",
        "diagnostics_before",
        "diagnostics_after",
        "diagnostics_deltas",
        "diagnostics_binding_sha256",
        "stage_diagnostics_binding_sha256",
        "operation",
        "operation_binding_sha256",
        "server_rollout_attestation",
        "server_rollout_attestation_sha256",
        "server_rollout_lookup_token",
        "rollout_lookup_token_retained",
        "raw_content_retained_in_evidence",
        "model_response_retained_in_evidence",
        "credentials_retained",
    }
)
_SECONDARY_PRODUCT_CLEANUP_ZERO_KEYS = frozenset(
    {
        "schema",
        "raw_object_id_sha256",
        "inbox_id_sha256",
        "raw_residue",
        "inbox_residue",
        "knowledge_residue",
        "alias_residue",
        "ko_state_residue",
        "feedback_residue",
        "feedback_state_residue",
        "review_residue",
    }
)
_SECONDARY_PRODUCT_CLEANUP_CORE_KEYS = frozenset(
    {
        "schema",
        "purged",
        "raw_deleted",
        "inbox_deleted",
        "storage_binding_sha256",
        "raw_object_id_sha256",
        "inbox_id_sha256",
        "cleanup_zero_residue_binding_sha256",
        "raw_residue",
        "inbox_residue",
        "knowledge_residue",
        "alias_residue",
        "ko_state_residue",
        "feedback_residue",
        "feedback_state_residue",
        "review_residue",
    }
)
_SECONDARY_PRODUCT_RESIDUE_KEYS = (
    "raw_residue",
    "inbox_residue",
    "knowledge_residue",
    "alias_residue",
    "ko_state_residue",
    "feedback_residue",
    "feedback_state_residue",
    "review_residue",
)
_SECONDARY_PRODUCT_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "attestation_id",
        "stage",
        "source_ref_sha256",
        "raw_object_id_sha256",
        "inbox_id_sha256",
        "content_sha256",
        "uploader_sha256",
        "ingest_storage_binding_sha256",
        "advice_storage_binding_sha256",
        "advice_diagnostics_receipt_sha256",
        "diagnostics_binding_sha256",
        "stage_diagnostics_binding_sha256",
        "operation_binding_sha256",
        "advice_proof_sha256",
        "advice_endpoint_role",
        "advice_model_sha256",
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_backend_version",
        "primary_ca_certificate_sha256",
        "observer_source_head",
        "observer_runner_sha256",
        "candidate_profile_id",
        "candidate_profile_mode",
        "candidate_profile_allow_private_text",
        "candidate_profile_context_tokens",
        "candidate_profile_sha256",
        "candidate_profile_manifest_sha256",
        "candidate_profile_admission",
        "served_model_alias",
        "gateway_ca_certificate_sha256",
        "cleanup_storage_binding_sha256",
        "cleanup_zero_residue_binding_sha256",
        *_SECONDARY_PRODUCT_RESIDUE_KEYS,
        "lookup_token_sha256",
        "state_version",
        "issued_at",
        "expires_at",
        "signature",
    }
)
_SECONDARY_PRODUCT_CONSUME_REQUEST_KEYS = frozenset(
    {
        "schema",
        "attestation_lookup_token",
        "server_rollout_attestation_sha256",
        "stage",
        "transition",
        "predecessor_commit",
        "predecessor_tree_sha256",
        "candidate_commit",
        "candidate_tree_sha256",
        "next_env_sha256",
        "product_receipt_sha256",
        "sealed_runner_sha256",
    }
)
_SECONDARY_PRODUCT_CONSUME_RESPONSE_KEYS = frozenset(
    {
        "schema",
        "status",
        "stage",
        "transition",
        "predecessor_commit",
        "predecessor_tree_sha256",
        "candidate_commit",
        "candidate_tree_sha256",
        "next_env_sha256",
        "product_receipt_sha256",
        "sealed_runner_sha256",
        "server_rollout_attestation_sha256",
        "lookup_token_sha256",
        "request_sha256",
        "consumed_at",
        "state_version",
        "consume_binding_sha256",
    }
)
_SECONDARY_DOCUMENT_MAP_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "status",
        "server_rollout_attestation",
        "server_rollout_attestation_sha256",
        "server_rollout_lookup_token",
        "document_text_retained_in_evidence",
        "model_response_retained_in_evidence",
        "document_text_digest_retained_in_evidence",
        "model_response_digest_retained_in_evidence",
    }
)
_SECONDARY_DOCUMENT_MAP_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "attestation_id",
        "workload",
        "routing_mode",
        "shadow_policy_id",
        "shadow_policy_manifest_sha256",
        "observation_kind",
        "scheduler_selected_delta",
        "scheduler_success_delta",
        "shadow_valid_delta",
        "shadow_invalid_delta",
        "shadow_skipped_delta",
        "shadow_in_flight_before",
        "shadow_in_flight_after",
        "observation_binding_sha256",
        "owner_binding_sha256",
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_backend_version",
        "primary_ca_certificate_sha256",
        "predecessor_release_commit",
        "predecessor_release_tree_manifest_sha256",
        "predecessor_release_metadata_sha256",
        "predecessor_release_wheel_sha256",
        "predecessor_live_env_sha256",
        "predecessor_live_env_path_sha256",
        "predecessor_release_anchor_path_sha256",
        "candidate_profile_id",
        "candidate_profile_mode",
        "candidate_profile_allow_private_text",
        "candidate_profile_context_tokens",
        "candidate_profile_sha256",
        "candidate_profile_manifest_sha256",
        "candidate_profile_admission",
        "served_model_alias",
        "gateway_ca_certificate_sha256",
        "document_text_retained",
        "model_response_retained",
        "document_text_digest_retained",
        "model_response_digest_retained",
        "state_version",
        "issued_at",
        "expires_at",
        "lookup_token_sha256",
        "signature",
    }
)
_SECONDARY_DOCUMENT_MAP_CONSUME_REQUEST_KEYS = frozenset(
    {
        "schema",
        "attestation_lookup_token",
        "server_rollout_attestation_sha256",
        "transition",
        "predecessor_commit",
        "predecessor_tree_sha256",
        "predecessor_env_sha256",
        "candidate_commit",
        "candidate_tree_sha256",
        "next_env_sha256",
        "product_receipt_sha256",
        "predecessor_policy_id",
        "predecessor_policy_manifest_sha256",
        "candidate_policy_id",
        "candidate_policy_manifest_sha256",
        "accepted_shadow_receipt_sha256",
    }
)
_SECONDARY_DOCUMENT_MAP_CONSUME_RESPONSE_KEYS = frozenset(
    {
        "schema",
        "status",
        "transition",
        "predecessor_commit",
        "predecessor_tree_sha256",
        "predecessor_env_sha256",
        "candidate_commit",
        "candidate_tree_sha256",
        "next_env_sha256",
        "product_receipt_sha256",
        "predecessor_policy_id",
        "predecessor_policy_manifest_sha256",
        "candidate_policy_id",
        "candidate_policy_manifest_sha256",
        "accepted_shadow_receipt_sha256",
        "server_rollout_attestation_sha256",
        "lookup_token_sha256",
        "request_sha256",
        "consumed_at",
        "state_version",
        "consume_binding_sha256",
    }
)
_SECONDARY_SHADOW_EXACT_VALUES = {
    "FRIDAY_SECONDARY_LLM_ADMISSION_TIMEOUT_SEC": "0.10",
    "FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT": "0",
    "FRIDAY_SECONDARY_LLM_BASE_URL": "https://192.168.1.35:8443/v1",
    "FRIDAY_SECONDARY_LLM_CALL_BUDGET_SEC": "15.0",
    "FRIDAY_SECONDARY_LLM_CONNECT_TIMEOUT_SEC": "1.0",
    "FRIDAY_SECONDARY_LLM_COOLDOWN_SEC": "60",
    "FRIDAY_SECONDARY_LLM_ENABLED": "1",
    "FRIDAY_SECONDARY_LLM_HEALTH_INTERVAL_SEC": "30",
    "FRIDAY_SECONDARY_LLM_MAX_CONCURRENCY": "1",
    "FRIDAY_SECONDARY_LLM_MAX_CONTEXT_TOKENS": "4096",
    "FRIDAY_SECONDARY_LLM_MODE": "shadow",
    "FRIDAY_SECONDARY_LLM_MODEL": _SECONDARY_FINALIST_MODEL_ALIAS,
    "FRIDAY_SECONDARY_LLM_PROFILE": _SECONDARY_FINALIST_PROFILE_ID,
    "FRIDAY_SECONDARY_LLM_READ_TIMEOUT_SEC": "12.0",
    "FRIDAY_SECONDARY_LLM_WORKLOADS": "extract",
}
_SECONDARY_SHADOW_DISABLED_EXACT_VALUES = {
    **_SECONDARY_SHADOW_EXACT_VALUES,
    "FRIDAY_SECONDARY_LLM_ENABLED": "0",
}
_SECONDARY_PRIVATE_SHADOW_EXACT_VALUES = {
    **_SECONDARY_SHADOW_EXACT_VALUES,
    "FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT": "1",
}
_SECONDARY_PRIVATE_SHADOW_DISABLED_EXACT_VALUES = {
    **_SECONDARY_PRIVATE_SHADOW_EXACT_VALUES,
    "FRIDAY_SECONDARY_LLM_ENABLED": "0",
}
_SECONDARY_ASSIST_EXACT_VALUES = {
    **_SECONDARY_PRIVATE_SHADOW_EXACT_VALUES,
    "FRIDAY_SECONDARY_LLM_MODE": "assist",
}
_SECONDARY_ASSIST_DISABLED_EXACT_VALUES = {
    **_SECONDARY_ASSIST_EXACT_VALUES,
    "FRIDAY_SECONDARY_LLM_ENABLED": "0",
}
_SECONDARY_DOCUMENT_MAP_SHADOW_EXACT_VALUES = {
    **_SECONDARY_ASSIST_EXACT_VALUES,
    "FRIDAY_SECONDARY_LLM_WORKLOADS": "document_map,extract",
    "FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE": "shadow",
}
_SECONDARY_DOCUMENT_MAP_ASSIST_EXACT_VALUES = {
    **_SECONDARY_DOCUMENT_MAP_SHADOW_EXACT_VALUES,
    "FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE": "assist",
}
_SECONDARY_DOCUMENT_MAP_SHADOW_DISABLED_EXACT_VALUES = {
    **_SECONDARY_DOCUMENT_MAP_SHADOW_EXACT_VALUES,
    "FRIDAY_SECONDARY_LLM_ENABLED": "0",
}
_SECONDARY_DOCUMENT_MAP_ASSIST_DISABLED_EXACT_VALUES = {
    **_SECONDARY_DOCUMENT_MAP_ASSIST_EXACT_VALUES,
    "FRIDAY_SECONDARY_LLM_ENABLED": "0",
}
_SEMANTIC_SUPERVISOR_LEGACY_OFF_EXACT_VALUES = {
    "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS": "1",
    "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS": "6",
    "FRIDAY_SEMANTIC_SUPERVISOR_MODE": "off",
    "FRIDAY_SEMANTIC_SUPERVISOR_TASKS": "",
    "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC": "12",
}
_SEMANTIC_SUPERVISOR_LEGACY_SHADOW_EXACT_VALUES = {
    "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS": "0",
    "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS": "6",
    "FRIDAY_SEMANTIC_SUPERVISOR_MODE": "shadow",
    "FRIDAY_SEMANTIC_SUPERVISOR_TASKS": (
        "compare_archive_with_current_web,compare_current_file_with_current_web"
    ),
    "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC": "12",
}
_SEMANTIC_SUPERVISOR_PRE_LATENCY_OFF_EXACT_VALUES = {
    **_SEMANTIC_SUPERVISOR_LEGACY_OFF_EXACT_VALUES,
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS": "",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED": "0",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE": "",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256": "",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256": "",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256": "",
}
_SEMANTIC_SUPERVISOR_PRE_LATENCY_SHADOW_EXACT_VALUES = {
    **_SEMANTIC_SUPERVISOR_LEGACY_SHADOW_EXACT_VALUES,
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS": "",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED": "0",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE": "",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256": "",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256": "",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256": "",
}
_SEMANTIC_SUPERVISOR_OFF_EXACT_VALUES = {
    **_SEMANTIC_SUPERVISOR_PRE_LATENCY_OFF_EXACT_VALUES,
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE": "",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256": "",
}
_SEMANTIC_SUPERVISOR_SHADOW_EXACT_VALUES = {
    **_SEMANTIC_SUPERVISOR_PRE_LATENCY_SHADOW_EXACT_VALUES,
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE": "",
    "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256": "",
}
_SEMANTIC_SUPERVISOR_PROMOTED_TASK = "compare_current_file_with_current_web"
_SEMANTIC_SUPERVISOR_ACTIVATION_STATUS_SCHEMA = "friday.supervisor-assist-activation-status.v1"
_SEMANTIC_SUPERVISOR_ACTIVATION_STATUS_KEYS = frozenset(
    {
        "schema",
        "configured",
        "reason",
        "requested_mode",
        "source_revision_loaded",
        "registry_binding_loaded",
        "scheduler_projection_loaded",
        "scheduler_runtime_available",
        "evidence_loaded",
        "evidence_authority",
        "operator_gate_enabled",
        "canary_actor_binding_count",
        "promotion_admitted",
        "evidence_accepted",
        "acceptance_authority",
        "body_free",
    }
)
_SEMANTIC_SUPERVISOR_ASSIST_CONTROLLER_STATUS_SCHEMA = (
    "friday.semantic-supervisor-assist-controller-status.v1"
)
_SEMANTIC_SUPERVISOR_ASSIST_CONTROLLER_STATUS_KEYS = frozenset(
    {
        "schema",
        "installed",
        "role",
        "requested_mode",
        "effective_mode",
        "promotion_admitted",
        "max_review_rounds",
        "promotion_attempt_total",
        "promotion_evaluation_total",
        "promotion_admitted_total",
        "active_tasks",
        "retained_active_graphs",
        "fallback_total",
        "invoked_total",
        "publication_total",
        "terminal_publication_total",
        "event_success_total",
        "event_failure_total",
        "ordinary_event_success_total",
        "ordinary_event_failure_total",
        "ownership_uncertain_total",
        "fallback_reasons",
        "runtime_owner",
        "publication_owner",
        "tools_allowed",
        "effects_allowed",
        "closed",
        "scheduler",
        "activation",
    }
)
_SEMANTIC_SUPERVISOR_ASSIST_CONTROLLER_SCHEDULER_KEYS = frozenset(
    {
        "state",
        "available",
        "workload",
        "policy_id",
        "policy_sha256",
        "workload_available",
        "runtime_available",
        "closed_reason",
        "circuit_retry_after_sec",
    }
)
_SEMANTIC_SUPERVISOR_MAX_PROMOTION_EVIDENCE_BYTES = 2_097_152
_SEMANTIC_SUPERVISOR_MAX_LATENCY_BUDGET_BYTES = 4_096
_SEMANTIC_SUPERVISOR_PROMOTION_SCHEMA = "friday.supervisor-assist-promotion.v5"
_SEMANTIC_SUPERVISOR_PROMOTION_BUNDLE_SCHEMA = (
    "friday.semantic-supervisor-promotion-bundle.v1"
)
_SEMANTIC_SUPERVISOR_PROMOTION_BUNDLE_RECEIPT_SCHEMA = (
    "friday.semantic-supervisor-promotion-bundle-receipt.v1"
)
_SEMANTIC_SUPERVISOR_PROMOTION_ATTESTATION_SCHEMA = (
    "friday.semantic-supervisor-promotion-operator-attestation.v1"
)
_SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA = (
    "friday.semantic-supervisor-representative-window-attestation.v1"
)
_SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA = (
    "friday.semantic-supervisor-representative-window-issue-response.v1"
)
_SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_CONSUME_REQUEST_SCHEMA = (
    "friday.semantic-supervisor-representative-window-consume-request.v1"
)
_SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_SCHEMA = (
    "friday.semantic-supervisor-representative-window-consume-response.v1"
)
_SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_AUTHORITY = (
    "server_recomputed_live_production"
)
_SEMANTIC_SUPERVISOR_PRODUCTION_BASELINE_SCHEMA = (
    "friday.semantic-supervisor-production-baseline.v2"
)
_SEMANTIC_SUPERVISOR_PRODUCTION_BASELINE_KIND = (
    "joined_body_free_production_candidate"
)
_SEMANTIC_SUPERVISOR_PRODUCT_WINDOW_SCHEMA = (
    "friday.semantic-supervisor-product-window.v1"
)
_SEMANTIC_SUPERVISOR_READINESS_EVIDENCE_SCHEMA = "friday.supervisor-assist-readiness-evidence.v2"
_SEMANTIC_SUPERVISOR_OUTCOME_EVIDENCE_SCHEMA = "friday.supervisor-assist-outcome-evidence.v2"
_SEMANTIC_SUPERVISOR_PROMOTION_POLICY_SHA256 = (
    "496b65744334196de8e53499b6edbe2bc8c75333411a8871ddba5384ec20f8c1"
)
_SEMANTIC_SUPERVISOR_RUNTIME_PROFILE_ID = (
    "gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f"
)
_SEMANTIC_SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256 = (
    "93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3"
)
_SEMANTIC_SUPERVISOR_MIN_PRODUCT_OBSERVATIONS = 20
_SEMANTIC_SUPERVISOR_MAX_UNNECESSARY_CALL_RATE_BPS = 0
_SEMANTIC_SUPERVISOR_MAX_LATENCY_MS = 86_400_000
_SEMANTIC_SUPERVISOR_SAFE_EVIDENCE_ID = re.compile(r"[a-z][a-z0-9_.-]{0,95}")
_SEMANTIC_SUPERVISOR_SAFE_FAILURE_CLASS_ID = re.compile(r"[a-z][a-z0-9_.:-]{0,127}")
_SEMANTIC_SUPERVISOR_LATENCY_BUDGET_SCHEMA = "friday.semantic-supervisor-latency-budget-document.v1"
_SEMANTIC_SUPERVISOR_LATENCY_BUDGET_ID = "current-file-web-user-visible-latency-v1"
_SEMANTIC_SUPERVISOR_LATENCY_MEASUREMENT = "committed_turn_trace.budget.latency_ms"
_SEMANTIC_SUPERVISOR_LATENCY_BUDGET_KEYS = frozenset(
    {
        "schema",
        "budget_id",
        "task_class",
        "target_mode",
        "source_revision_sha256",
        "latency_measurement",
        "maximum_user_visible_latency_ms",
    }
)
_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "evidence_id",
        "authority",
        "observed_mode",
        "task_class",
        "source_revision_sha256",
        "promotion_policy_sha256",
        "observed_policy_id",
        "observed_policy_sha256",
        "target_policy_id",
        "target_policy_sha256",
        "runtime_profile_id",
        "runtime_profile_manifest_sha256",
        "registry_binding_sha256",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "operator_attestation_sha256",
        "precursor_assist_promotion_evidence_sha256",
        "max_steps",
        "max_review_rounds",
        "observation_count",
        "joined_trace_count",
        "representative_window_attested",
        "primary_fallback_proven",
        "laptop_unavailable_fallback_proven",
        "final_authority_recheck_proven",
        "primary_publication_owner_proven",
        "hidden_owner_count",
        "duplicate_capability_count",
        "duplicate_effect_count",
        "duplicate_publication_count",
        "false_completion_regression_count",
        "product_evidence",
    }
)
_SEMANTIC_SUPERVISOR_READINESS_EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "baseline_window_sha256",
        "baseline_observation_count",
        "baseline_complete_count",
        "documented_failure_class_id",
        "documented_failure_class_sha256",
        "baseline_failure_class_count",
        "readiness_witness_sha256",
        "readiness_observation_count",
        "latency_budget_target_mode",
        "latency_budget_source_revision_sha256",
        "latency_budget_ms",
        "latency_budget_sha256",
        "latency_total_ms",
        "latency_max_ms",
        "call_rate_observation_count",
        "supervisor_invocation_count",
        "unnecessary_supervisor_invocation_count",
        "user_visible_observation_count",
        "user_visible_regression_count",
    }
)
_SEMANTIC_SUPERVISOR_OUTCOME_EVIDENCE_KEYS = frozenset(
    {
        "schema",
        "quality_basis",
        "baseline_window_sha256",
        "promoted_window_sha256",
        "baseline_observation_count",
        "baseline_complete_count",
        "promoted_observation_count",
        "promoted_complete_count",
        "documented_failure_class_id",
        "documented_failure_class_sha256",
        "baseline_failure_class_count",
        "promoted_failure_class_count",
        "latency_budget_target_mode",
        "latency_budget_source_revision_sha256",
        "latency_budget_ms",
        "latency_budget_sha256",
        "latency_observation_count",
        "latency_total_ms",
        "latency_max_ms",
        "call_rate_observation_count",
        "supervisor_invocation_count",
        "unnecessary_supervisor_invocation_count",
        "user_visible_observation_count",
        "user_visible_regression_count",
    }
)
_SEMANTIC_SUPERVISOR_BUNDLE_KEYS = frozenset(
    {
        "schema",
        "body_free",
        "baseline",
        "operator_attestation",
        "promotion_evidence",
        "representative_window_issue",
        "producer_receipt",
        "producer_receipt_sha256",
    }
)
_SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ISSUE_KEYS = frozenset(
    {
        "schema",
        "status",
        "server_attestation",
        "server_attestation_sha256",
        "attestation_lookup_token",
        "lookup_token_sha256",
        "state_version",
    }
)
_SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "attestation_id",
        "authority",
        "target_mode",
        "observed_mode",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "latency_budget_document_sha256",
        "latency_budget_target_mode",
        "latency_budget_source_revision_sha256",
        "maximum_user_visible_latency_ms",
        "precursor_assist_promotion_evidence_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_backend_version",
        "requested_mode",
        "observed_release_commit",
        "observed_release_metadata_sha256",
        "observed_release_tree_sha256",
        "observed_registry_binding_sha256",
        "supervisor_policy_id",
        "supervisor_policy_sha256",
        "runtime_profile_id",
        "runtime_profile_manifest_sha256",
        "observer_runner_sha256",
        "sample_limit",
        "turn_trace_count",
        "joined_trace_count",
        "representative_window_sha256",
        "server_recomputed",
        "representative_window_attested",
        "synthetic_authority",
        "lookup_token_sha256",
        "state_version",
        "issued_at",
        "expires_at",
        "signature",
    }
)
_SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_CONSUME_REQUEST_KEYS = frozenset(
    {
        "schema",
        "attestation_lookup_token",
        "server_attestation_sha256",
        "target_mode",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "latency_budget_document_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "observer_runner_sha256",
        "precursor_assist_promotion_evidence_sha256",
    }
)
_SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_KEYS = frozenset(
    {
        "schema",
        "status",
        "attestation_id",
        "target_mode",
        "observed_mode",
        "server_attestation_sha256",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "latency_budget_document_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "observer_runner_sha256",
        "representative_window_sha256",
        "precursor_assist_promotion_evidence_sha256",
        "lookup_token_sha256",
        "consume_request_sha256",
        "consumed_at",
        "state_version",
        "consume_binding_sha256",
        "server_attestation",
    }
)
_SEMANTIC_SUPERVISOR_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "target_mode",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "representative_window_attested",
        "primary_fallback_proven",
        "laptop_unavailable_fallback_proven",
        "final_authority_recheck_proven",
        "primary_publication_owner_proven",
        "zero_hidden_owners_attested",
        "zero_duplicate_capabilities_attested",
        "zero_duplicate_effects_attested",
        "zero_duplicate_publications_attested",
        "zero_false_completion_regressions_attested",
        "precursor_assist_promotion_evidence_sha256",
        "quality_basis",
    }
)
_SEMANTIC_SUPERVISOR_BUNDLE_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "target_mode",
        "source_revision_sha256",
        "registry_binding_sha256",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "operator_attestation_sha256",
        "representative_window_server_attestation_sha256",
        "representative_window_lookup_token_sha256",
        "representative_window_sha256",
        "representative_window_observer_runner_sha256",
        "promotion_evidence_file_sha256",
        "promotion_evidence_canonical_sha256",
        "precursor_assist_promotion_evidence_sha256",
        "body_free",
        "promotion_authority_granted",
        "activation_performed",
    }
)
_SEMANTIC_SUPERVISOR_BASELINE_KEYS = frozenset(
    {
        "schema",
        "evidence",
        "sample",
        "primary_baseline",
        "supervisor_join",
        "product_windows",
        "report_sha256",
    }
)
_SEMANTIC_SUPERVISOR_BASELINE_EVIDENCE_KEYS = frozenset(
    {
        "kind",
        "body_free",
        "production_acceptance",
        "acceptance_authority",
        "representative_window_attested",
        "promotion_authority",
    }
)
_SEMANTIC_SUPERVISOR_BASELINE_SAMPLE_KEYS = frozenset(
    {
        "limit",
        "turn_traces",
        "joined_supervisor_events",
        "promoted_product_events",
        "malformed_turn_traces",
        "malformed_joined_events",
        "malformed_promoted_product_events",
        "duplicate_turn_trace_digests",
        "duplicate_shadow_product_events",
        "duplicate_promoted_product_events",
        "unmatched_shadow_product_events",
        "unmatched_promoted_product_events",
    }
)
_SEMANTIC_SUPERVISOR_BASELINE_ANOMALY_KEYS = frozenset(
    _SEMANTIC_SUPERVISOR_BASELINE_SAMPLE_KEYS
    - {"limit", "turn_traces", "joined_supervisor_events", "promoted_product_events"}
)
_SEMANTIC_SUPERVISOR_BASELINE_PRIMARY_KEYS = frozenset(
    {
        "intent_counts",
        "playbook_counts",
        "completion_counts",
        "publication_counts",
        "failure_counts",
        "authority_rechecked_count",
        "partial_coverage_count",
        "state_restored_count",
    }
)
_SEMANTIC_SUPERVISOR_BASELINE_JOIN_KEYS = frozenset(
    {
        "task_counts",
        "skip_counts",
        "parse_counts",
        "policy_reason_counts",
        "planner_latency_bucket_counts",
        "actual_completion_counts",
        "actual_publication_counts",
        "actual_capability_outcome_counts",
        "invoked_count",
        "admitted_count",
        "final_authority_rechecked_count",
        "state_restored_count",
        "retry_occurred_count",
    }
)
_SEMANTIC_SUPERVISOR_METRIC_WINDOW_KEYS = frozenset(
    {
        "schema",
        "stage",
        "observation_count",
        "completion_counts",
        "complete_count",
        "failure_class_counts",
        "latency_observation_count",
        "latency_total_ms",
        "latency_max_ms",
        "window_sha256",
    }
)
_SEMANTIC_SUPERVISOR_SHADOW_WINDOW_KEYS = frozenset(
    {
        "schema",
        "mode",
        "production_joined",
        "actual_promoted_execution",
        "quality_claim",
        "observation_count",
        "joined_trace_count",
        "baseline",
        "readiness_observation_count",
        "call_rate_observation_count",
        "supervisor_invocation_count",
        "unnecessary_supervisor_invocation_count",
        "user_visible_observation_count",
        "user_visible_regression_count",
        "readiness_witness_sha256",
    }
)
_SEMANTIC_SUPERVISOR_PROMOTED_WINDOW_KEYS = frozenset(
    {
        "schema",
        "mode",
        "production_joined",
        "actual_promoted_execution",
        "observation_count",
        "joined_trace_count",
        "promotion_evidence_count",
        "promotion_evidence_sha256",
        "promoted",
        "call_rate_observation_count",
        "supervisor_invocation_count",
        "unnecessary_supervisor_invocation_count",
        "user_visible_observation_count",
        "user_visible_regression_count",
        "product_window_sha256",
    }
)
BOOTSTRAP_WHEELS = (("pip", "26.1.2", "pip-26.1.2-py3-none-any.whl"),)
_ACTIVATION_SMOKE_RECEIPT = b"friday-activation-smoke:clear:v1\n"
_OBSIDIAN_SETTINGS_IDENTITY_PROBE = """
has_obsidian_enabled=hasattr(settings,'obsidian_enabled')
has_obsidian_root=hasattr(settings,'obsidian_effective_root')
if obsidian_identity_required:
 assert has_obsidian_enabled and has_obsidian_root
else:
 assert obsidian_mode=='disabled'
if has_obsidian_enabled or has_obsidian_root:
 assert has_obsidian_enabled and has_obsidian_root
 assert type(settings.obsidian_enabled) is bool
 assert settings.obsidian_enabled==(obsidian_mode=='enabled')
 assert isinstance(settings.obsidian_effective_root,pathlib.Path)
 assert settings.obsidian_effective_root.absolute()==obsidian_root
"""


class ReleaseFailure(RuntimeError):
    """One closed operational failure code safe for a public receipt."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _canonical_json(value: Mapping[str, Any] | Sequence[Any]) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _release_binds_memory_vault_mode(release: ReleaseIdentity) -> bool:
    """Use an attested installed capability, never a forgeable version heuristic."""

    return release.memory_vault_mode_contract == MEMORY_VAULT_MODE_CONTRACT


def _require_venv_relocation_contract(release: ReleaseIdentity, *, code: str) -> None:
    if release.venv_relocation_contract != VENV_RELOCATION_CONTRACT:
        raise ReleaseFailure(code)


def _require_obsidian_cutover_contract(release: ReleaseIdentity, *, code: str) -> None:
    if release.obsidian_cutover_contract not in {"", OBSIDIAN_CUTOVER_CONTRACT} or (
        release.max_schema >= 35 and release.obsidian_cutover_contract != OBSIDIAN_CUTOVER_CONTRACT
    ):
        raise ReleaseFailure(code)


def _memory_vault_health_identity_matches(
    payload: Mapping[str, Any],
    release: ReleaseIdentity,
    expected_mode: str,
) -> bool:
    """Treat a mode-less release as its honest legacy ``full_owner`` behavior."""

    if not _release_binds_memory_vault_mode(release):
        return expected_mode == "full_owner" and "memory_vault" not in payload
    return payload.get("memory_vault") == {
        "mode": expected_mode,
        "body_free_mode": expected_mode == "disabled",
        "body_projection_enabled": expected_mode == "full_owner",
    }


def _obsidian_health_identity_matches(
    payload: Mapping[str, Any],
    release: ReleaseIdentity,
    expected_mode: str,
    expected_root_sha256: str,
) -> bool:
    expected = {
        "mode": expected_mode,
        "root_sha256": expected_root_sha256,
    }
    if payload.get("obsidian") == expected:
        return True
    legacy_release = release.max_schema < 35 and not release.obsidian_cutover_contract
    return legacy_release and expected_mode == "disabled" and "obsidian" not in payload


def _semantic_supervisor_health_identity_matches(
    payload: Mapping[str, Any],
    *,
    expected_mode: str,
) -> bool:
    """Bind a semantic ENV transition to the source-level runtime seam."""

    semantic = payload.get("semantic_supervisor")
    secondary = payload.get("secondary")
    if not isinstance(semantic, Mapping) or not isinstance(secondary, Mapping):
        return False
    nested = secondary.get("semantic_supervisor")
    if not isinstance(nested, Mapping):
        return False
    expected_policy_id, expected_policy_sha256 = (
        (
            _SEMANTIC_SUPERVISOR_ASSIST_POLICY_ID,
            _SEMANTIC_SUPERVISOR_ASSIST_POLICY_SHA256,
        )
        if expected_mode in {"assist", "canary"}
        else (
            _SEMANTIC_SUPERVISOR_SHADOW_POLICY_ID,
            _SEMANTIC_SUPERVISOR_SHADOW_POLICY_SHA256,
        )
    )
    secondary_common = bool(
        secondary.get("schema") == "friday.optional-secondary-health.v1"
        and secondary.get("role") == "optional_advisory"
        and secondary.get("enabled") is True
        and secondary.get("configured") is True
        and secondary.get("mode") == "assist"
        and type(secondary.get("available")) is bool
        and nested.get("workload") == "plan_candidate"
        and nested.get("policy_id") == expected_policy_id
        and nested.get("policy_sha256") == expected_policy_sha256
        and type(nested.get("runtime_available")) is bool
    )
    if not secondary_common:
        return False

    if expected_mode in {"assist", "canary"}:
        if set(semantic) != _SEMANTIC_SUPERVISOR_ASSIST_CONTROLLER_STATUS_KEYS:
            return False
        controller_scheduler = semantic.get("scheduler")
        activation = semantic.get("activation")
        if (
            not isinstance(controller_scheduler, Mapping)
            or set(controller_scheduler) != _SEMANTIC_SUPERVISOR_ASSIST_CONTROLLER_SCHEDULER_KEYS
            or not isinstance(activation, Mapping)
            or set(activation) != _SEMANTIC_SUPERVISOR_ACTIVATION_STATUS_KEYS
        ):
            return False
        counters = (
            "promotion_attempt_total",
            "promotion_evaluation_total",
            "promotion_admitted_total",
            "active_tasks",
            "retained_active_graphs",
            "fallback_total",
            "invoked_total",
            "publication_total",
            "terminal_publication_total",
            "event_success_total",
            "event_failure_total",
            "ordinary_event_success_total",
            "ordinary_event_failure_total",
            "ownership_uncertain_total",
        )
        if any(type(semantic.get(key)) is not int or semantic[key] < 0 for key in counters):
            return False
        fallback_reasons = semantic.get("fallback_reasons")
        if (
            not isinstance(fallback_reasons, Mapping)
            or len(fallback_reasons) > 32
            or any(
                type(key) is not str or type(value) is not int or value < 0
                for key, value in fallback_reasons.items()
            )
        ):
            return False
        retry_after = controller_scheduler.get("circuit_retry_after_sec")
        if retry_after is not None and (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, int | float)
            or not math.isfinite(float(retry_after))
            or float(retry_after) < 0
        ):
            return False
        actor_count = activation.get("canary_actor_binding_count")
        if not bool(
            type(actor_count) is int
            and (actor_count == 0 if expected_mode == "assist" else 1 <= actor_count <= 32)
        ):
            return False
        promotion_admitted = semantic.get("promotion_admitted")
        effective_mode = semantic.get("effective_mode")
        if type(promotion_admitted) is not bool or effective_mode != (
            expected_mode if promotion_admitted else "off"
        ):
            return False
        return bool(
            semantic.get("schema") == _SEMANTIC_SUPERVISOR_ASSIST_CONTROLLER_STATUS_SCHEMA
            and semantic.get("installed") is True
            and semantic.get("role") == "durable_read_only_assist"
            and semantic.get("requested_mode") == expected_mode
            and semantic.get("max_review_rounds") == 1
            and semantic.get("runtime_owner") == "durable_graph_after_admission"
            and semantic.get("publication_owner") == "primary"
            and semantic.get("tools_allowed") is False
            and semantic.get("effects_allowed") is False
            and semantic.get("closed") is False
            and semantic["promotion_admitted_total"] <= semantic["promotion_evaluation_total"]
            and semantic["promotion_evaluation_total"] <= semantic["promotion_attempt_total"]
            and controller_scheduler.get("state") in {"probing", "healthy", "degraded", "cooldown"}
            and type(controller_scheduler.get("available")) is bool
            and controller_scheduler.get("workload") == "plan_candidate"
            and controller_scheduler.get("policy_id") == expected_policy_id
            and controller_scheduler.get("policy_sha256") == expected_policy_sha256
            and controller_scheduler.get("workload_available") is True
            and type(controller_scheduler.get("runtime_available")) is bool
            and controller_scheduler.get("closed_reason") == "admitted"
            and controller_scheduler.get("state") == secondary.get("state")
            and controller_scheduler.get("available") == secondary.get("available")
            and controller_scheduler.get("runtime_available") == nested.get("runtime_available")
            and nested.get("requested_mode") == expected_mode
            and nested.get("effective_mode") == "shadow"
            and nested.get("workload_available") is True
            and nested.get("closed_reason") == "admitted"
            and activation.get("schema") == _SEMANTIC_SUPERVISOR_ACTIVATION_STATUS_SCHEMA
            and activation.get("configured") is True
            and activation.get("reason") == "material_loaded_not_accepted"
            and activation.get("requested_mode") == expected_mode
            and activation.get("source_revision_loaded") is True
            and activation.get("registry_binding_loaded") is True
            and activation.get("scheduler_projection_loaded") is True
            and type(activation.get("scheduler_runtime_available")) is bool
            and activation.get("scheduler_runtime_available") == controller_scheduler.get("runtime_available")
            and activation.get("evidence_loaded") is True
            and activation.get("evidence_authority") == "production_joined"
            and activation.get("operator_gate_enabled") is True
            and activation.get("promotion_admitted") is False
            and activation.get("evidence_accepted") is False
            and activation.get("acceptance_authority") == "none"
            and activation.get("body_free") is True
        )

    shadow_common = bool(
        semantic.get("schema") == "friday.semantic-supervisor-shadow-runtime.v1"
        and semantic.get("role") == "discarded_advisory_shadow"
        and semantic.get("promotion_admitted") is False
        and semantic.get("runtime_owner") == "unchanged"
        and semantic.get("publication_owner") == "primary"
        and semantic.get("tools_allowed") is False
        and semantic.get("effects_allowed") is False
        and semantic.get("execution_allowed") is False
    )
    if not shadow_common:
        return False
    if expected_mode == "shadow":
        return bool(
            semantic.get("installed") is True
            and semantic.get("requested_mode") == "shadow"
            and semantic.get("effective_mode") == "shadow"
            and semantic.get("policy_id") == expected_policy_id
            and semantic.get("policy_sha256") == expected_policy_sha256
            and semantic.get("accepted_profile_id") == _SECONDARY_FINALIST_PROFILE_ID
            and semantic.get("max_pending") == 4
            and nested.get("requested_mode") == "shadow"
            and nested.get("effective_mode") == "shadow"
            and nested.get("workload_available") is True
            and nested.get("closed_reason") == "admitted"
        )
    if expected_mode == "off":
        return bool(
            semantic.get("installed") is False
            and semantic.get("requested_mode") == "off"
            and semantic.get("effective_mode") == "off"
            and nested.get("requested_mode") == "off"
            and nested.get("effective_mode") == "off"
            and nested.get("workload_available") is False
            and nested.get("runtime_available") is False
            and nested.get("closed_reason") == "mode_off"
        )
    return False


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError("short write")
        written += count


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_durable(path: Path, value: bytes, *, final_mode: int) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        _write_all(descriptor, value)
        os.fchmod(descriptor, final_mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_private_durable(path: Path, value: bytes) -> None:
    """Durably replace one owner-only state file without following links."""

    parent = _private_directory(path.parent)
    lexical = Path(os.path.abspath(path))
    if lexical.parent != parent or lexical.name in {"", ".", ".."}:
        raise ReleaseFailure("durable_state_path_invalid")
    temporary = parent / f".{lexical.name}.{os.getpid()}.{os.urandom(8).hex()}.new"
    try:
        _write_private_durable(temporary, value, final_mode=0o600)
        os.replace(temporary, lexical)
        os.chmod(lexical, 0o600)
        _fsync_directory(parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        status = os.lstat(path)
        if stat.S_ISREG(status.st_mode):
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        elif stat.S_ISDIR(status.st_mode):
            _fsync_directory(path)
    _fsync_directory(root)


def _closed_hash(value: str, code: str) -> str:
    if _HEX64.fullmatch(value) is None:
        raise ReleaseFailure(code)
    return value


def _closed_commit(value: str) -> str:
    if _HEX40.fullmatch(value) is None:
        raise ReleaseFailure("candidate_commit_invalid")
    return value


def _private_directory(path: Path, *, create: bool = False) -> Path:
    lexical = Path(os.path.abspath(path))
    if create and not lexical.exists():
        lexical.mkdir(parents=True, mode=0o700)
        os.chmod(lexical, 0o700)
    try:
        resolved = lexical.resolve(strict=True)
        status = os.stat(lexical, follow_symlinks=False)
    except OSError as exc:
        raise ReleaseFailure("private_directory_invalid") from exc
    if (
        resolved != lexical
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise ReleaseFailure("private_directory_invalid")
    return resolved


def _owned_directory(path: Path) -> Path:
    lexical = Path(os.path.abspath(path))
    try:
        resolved = lexical.resolve(strict=True)
        status = os.stat(lexical, follow_symlinks=False)
    except OSError as exc:
        raise ReleaseFailure("owned_directory_invalid") from exc
    if (
        resolved != lexical
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o022
    ):
        raise ReleaseFailure("owned_directory_invalid")
    return resolved


def _regular_file(path: Path, *, maximum_bytes: int, code: str) -> Path:
    lexical = Path(os.path.abspath(path))
    try:
        resolved = lexical.resolve(strict=True)
        status = os.stat(lexical, follow_symlinks=False)
    except OSError as exc:
        raise ReleaseFailure(code) from exc
    if (
        resolved != lexical
        or not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or not 0 < status.st_size <= maximum_bytes
    ):
        raise ReleaseFailure(code)
    return resolved


def _private_regular_file(path: Path, *, maximum_bytes: int, code: str) -> Path:
    resolved = _regular_file(path, maximum_bytes=maximum_bytes, code=code)
    status = os.stat(resolved, follow_symlinks=False)
    if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) & 0o077:
        raise ReleaseFailure(code)
    return resolved


def _private_regular_file_allow_empty(path: Path, *, maximum_bytes: int, code: str) -> Path:
    lexical = Path(os.path.abspath(path))
    try:
        resolved = lexical.resolve(strict=True)
        status = os.stat(lexical, follow_symlinks=False)
    except OSError as exc:
        raise ReleaseFailure(code) from exc
    if (
        resolved != lexical
        or not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o077
        or not 0 <= status.st_size <= maximum_bytes
    ):
        raise ReleaseFailure(code)
    return resolved


def _read_private_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    code: str,
    allowed_modes: frozenset[int] | None = None,
) -> bytes:
    """Read one private file through a stable no-follow descriptor."""

    lexical = Path(os.path.abspath(path))
    parent = _private_directory(lexical.parent)
    if lexical.parent != parent or lexical.name in {"", ".", ".."}:
        raise ReleaseFailure(code)
    descriptor = -1
    try:
        descriptor = os.open(
            lexical,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or (
                allowed_modes is not None
                and stat.S_IMODE(before.st_mode) not in allowed_modes
            )
            or not 0 < before.st_size <= maximum_bytes
        ):
            raise ReleaseFailure(code)
        chunks: list[bytes] = []
        remaining = int(before.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1 << 20))
            if not chunk:
                raise ReleaseFailure(code)
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ReleaseFailure(code)
        after = os.fstat(descriptor)
        current = os.stat(lexical, follow_symlinks=False)
        identity = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(before.st_ctime_ns),
        )
        if (
            identity
            != (
                int(after.st_dev),
                int(after.st_ino),
                int(after.st_size),
                int(after.st_mtime_ns),
                int(after.st_ctime_ns),
            )
            or (int(current.st_dev), int(current.st_ino)) != identity[:2]
        ):
            raise ReleaseFailure(code)
        return b"".join(chunks)
    except ReleaseFailure:
        raise
    except OSError as exc:
        raise ReleaseFailure(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_stable_regular_file(path: Path, *, maximum_bytes: int, code: str) -> bytes:
    """Read one regular file through a stable no-follow descriptor."""

    lexical = Path(os.path.abspath(path))
    try:
        parent = lexical.parent.resolve(strict=True)
        parent_status = os.stat(lexical.parent, follow_symlinks=False)
    except OSError as exc:
        raise ReleaseFailure(code) from exc
    if lexical.parent != parent or not stat.S_ISDIR(parent_status.st_mode) or lexical.name in {"", ".", ".."}:
        raise ReleaseFailure(code)
    descriptor = -1
    try:
        descriptor = os.open(
            lexical,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum_bytes
        ):
            raise ReleaseFailure(code)
        chunks: list[bytes] = []
        remaining = int(before.st_size)
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1 << 20))
            if not chunk:
                raise ReleaseFailure(code)
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ReleaseFailure(code)
        after = os.fstat(descriptor)
        current = os.stat(lexical, follow_symlinks=False)
        identity = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(before.st_ctime_ns),
        )
        if (
            identity
            != (
                int(after.st_dev),
                int(after.st_ino),
                int(after.st_size),
                int(after.st_mtime_ns),
                int(after.st_ctime_ns),
            )
            or (int(current.st_dev), int(current.st_ino)) != identity[:2]
        ):
            raise ReleaseFailure(code)
        return b"".join(chunks)
    except ReleaseFailure:
        raise
    except OSError as exc:
        raise ReleaseFailure(code) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _secondary_environment_parts(raw: bytes) -> tuple[dict[str, str], bytes, bytes]:
    """Mirror ``load_local_env_file`` and retain both byte domains exactly."""

    values: dict[str, str] = {}
    unrelated: list[bytes] = []
    secondary: list[bytes] = []
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ReleaseFailure("secondary_shadow_environment_invalid") from exc
    raw_lines = text.splitlines(keepends=True)
    logical_lines = text.splitlines()
    if len(raw_lines) != len(logical_lines):  # pragma: no cover - Python owns both operations
        raise ReleaseFailure("secondary_shadow_environment_invalid")
    previous_ending = ""
    canonical_endings = {"", "\n", "\r", "\r\n"}
    for raw_line, line in zip(raw_lines, logical_lines, strict=True):
        ending = raw_line[len(line) :] if raw_line.startswith(line) else "\x00"
        follows_canonical_line = previous_ending in canonical_endings
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            unrelated.append(raw_line.encode("utf-8"))
            previous_ending = ending
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not key:
            unrelated.append(raw_line.encode("utf-8"))
            previous_ending = ending
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key.startswith("JERICHO_SECONDARY_LLM_"):
            raise ReleaseFailure("secondary_shadow_legacy_environment_forbidden")
        if not key.startswith(_SECONDARY_LLM_ENV_PREFIX):
            unrelated.append(raw_line.encode("utf-8"))
            previous_ending = ending
            continue
        if (
            key not in _SECONDARY_LLM_ENV_KEYS
            or key in values
            or line != f"{key}={value}"
            or ending not in canonical_endings
            or not follows_canonical_line
        ):
            raise ReleaseFailure("secondary_shadow_environment_invalid")
        if any(character in value for character in "\x00\r\n"):
            raise ReleaseFailure("secondary_shadow_environment_invalid")
        values[key] = value
        secondary.append(raw_line.encode("utf-8"))
        previous_ending = ending
    return values, b"".join(unrelated), b"".join(secondary)


def _secondary_environment_view(raw: bytes) -> tuple[dict[str, str], bytes]:
    values, unrelated, _secondary = _secondary_environment_parts(raw)
    return values, unrelated


def _semantic_supervisor_environment_parts(raw: bytes) -> tuple[dict[str, str], bytes, bytes]:
    """Split the thirteen closed supervisor keys from the non-secondary prefix."""

    values: dict[str, str] = {}
    unrelated: list[bytes] = []
    supervisor: list[bytes] = []
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ReleaseFailure("semantic_supervisor_environment_invalid") from exc
    raw_lines = text.splitlines(keepends=True)
    logical_lines = text.splitlines()
    if len(raw_lines) != len(logical_lines):  # pragma: no cover - Python owns both operations
        raise ReleaseFailure("semantic_supervisor_environment_invalid")
    previous_ending = ""
    canonical_endings = {"", "\n", "\r", "\r\n"}
    for raw_line, line in zip(raw_lines, logical_lines, strict=True):
        ending = raw_line[len(line) :] if raw_line.startswith(line) else "\x00"
        follows_canonical_line = previous_ending in canonical_endings
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            unrelated.append(raw_line.encode("utf-8"))
            previous_ending = ending
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not key:
            unrelated.append(raw_line.encode("utf-8"))
            previous_ending = ending
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key.startswith("JERICHO_SEMANTIC_SUPERVISOR_"):
            raise ReleaseFailure("semantic_supervisor_legacy_environment_forbidden")
        if not key.startswith(_SEMANTIC_SUPERVISOR_ENV_PREFIX):
            unrelated.append(raw_line.encode("utf-8"))
            previous_ending = ending
            continue
        if (
            key not in _SEMANTIC_SUPERVISOR_ENV_KEYS
            or key in values
            or line != f"{key}={value}"
            or ending not in canonical_endings
            or not follows_canonical_line
            or any(character in value for character in "\x00\r\n")
        ):
            raise ReleaseFailure("semantic_supervisor_environment_invalid")
        values[key] = value
        supervisor.append(raw_line.encode("utf-8"))
        previous_ending = ending
    return values, b"".join(unrelated), b"".join(supervisor)


def _canonical_environment_values(values: Mapping[str, str]) -> bytes:
    return b"".join(f"{key}={value}\n".encode() for key, value in sorted(values.items()))


def _semantic_supervisor_environment_bytes(raw: bytes) -> bytes:
    """Return the literal supervisor block while ignoring the final secondary block."""

    _secondary_values, nonsecondary, _secondary = _secondary_environment_parts(raw)
    _values, _unrelated, supervisor = _semantic_supervisor_environment_parts(nonsecondary)
    return supervisor


def _secondary_rollout_api_token(raw: bytes) -> str:
    """Extract the one literal owner API token without exposing it in process arguments."""

    token_keys = frozenset({"FRIDAY_API_TOKEN", "JERICHO_API_TOKEN"})
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeError as exc:
        raise ReleaseFailure("secondary_rollout_api_token_invalid") from exc
    matches: list[tuple[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        normalized_key = key.strip()
        shell_words = normalized_key.split(None, 1)
        if len(shell_words) == 2 and shell_words[0] == "export":
            normalized_key = shell_words[1].strip()
        if normalized_key not in token_keys:
            continue
        if (
            not separator
            or key not in token_keys
            or line != f"{key}={value}"
            or re.fullmatch(r"[A-Za-z0-9._~-]{32,4096}", value) is None
        ):
            raise ReleaseFailure("secondary_rollout_api_token_invalid")
        matches.append((key, value))
    if len(matches) != 1:
        raise ReleaseFailure("secondary_rollout_api_token_invalid")
    return matches[0][1]


def _engineer_mode_environment_parts(
    raw: bytes,
    *,
    code: str,
) -> tuple[list[bytes], dict[str, int]]:
    """Locate exact engineer-policy assignments without normalizing any bytes."""

    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ReleaseFailure(code) from exc
    raw_lines = text.splitlines(keepends=True)
    if "".join(raw_lines) != text:  # pragma: no cover - Python owns splitlines
        raise ReleaseFailure(code)
    assignments: dict[str, int] = {}
    engineer_keys = {key for key, _disabled, _enabled in _ENGINEER_MODE_ENV_LINES}
    encoded_lines: list[bytes] = []
    for index, raw_line in enumerate(raw_lines):
        encoded = raw_line.encode("utf-8")
        encoded_lines.append(encoded)
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].strip()
        key, separator, _value = stripped.partition("=")
        key = key.strip()
        if key in _ENGINEER_MODE_LEGACY_ENV_KEYS:
            raise ReleaseFailure(code)
        if key not in engineer_keys:
            continue
        if not separator or key in assignments:
            raise ReleaseFailure(code)
        assignments[key] = index
    return encoded_lines, assignments


def _validate_engineer_mode_enable_environment(
    predecessor: bytes | None,
    target: bytes,
) -> None:
    """Enable the exact owner workbench while preserving every unrelated byte."""

    target_lines, target_assignments = _engineer_mode_environment_parts(
        target,
        code="engineer_mode_environment_invalid",
    )
    for key, _disabled, enabled in _ENGINEER_MODE_ENV_LINES:
        index = target_assignments.get(key)
        if index is None or target_lines[index] != enabled:
            raise ReleaseFailure("engineer_mode_environment_invalid")
    if predecessor is None:
        return

    predecessor_lines, predecessor_assignments = _engineer_mode_environment_parts(
        predecessor,
        code="engineer_mode_predecessor_not_disabled",
    )
    expected = list(predecessor_lines)
    missing: list[bytes] = []
    for key, disabled, enabled in _ENGINEER_MODE_ENV_LINES:
        index = predecessor_assignments.get(key)
        if index is None:
            missing.append(enabled)
            continue
        if predecessor_lines[index] != disabled:
            raise ReleaseFailure("engineer_mode_predecessor_not_disabled")
        expected[index] = enabled
    if missing:
        if predecessor and not predecessor.endswith((b"\n", b"\r")):
            expected.append(b"\n")
        expected.extend(missing)
    if b"".join(expected) != target:
        raise ReleaseFailure("engineer_mode_unrelated_environment_changed")


def _validate_staged_environment_transition(
    transition: str,
    predecessor: bytes | None,
    target: bytes,
) -> None:
    """Enforce the exact byte contract for the selected staged ENV transition."""

    if transition == _ENGINEER_MODE_ENABLE_TRANSITION:
        _validate_engineer_mode_enable_environment(predecessor, target)
        return
    if transition in _SECONDARY_CONFIG_TRANSITIONS:
        _validate_secondary_config_transition(transition, predecessor, target)
        return
    if transition in _SEMANTIC_SUPERVISOR_CONFIG_TRANSITIONS:
        _validate_semantic_supervisor_config_transition(transition, predecessor, target)
        return
    target_supervisor = _semantic_supervisor_environment_bytes(target)
    if predecessor is not None:
        predecessor_supervisor = _semantic_supervisor_environment_bytes(predecessor)
        if predecessor_supervisor != target_supervisor:
            raise ReleaseFailure("nonsemantic_transition_changed_semantic_supervisor_environment")
    _target_values, _target_unrelated, target_secondary = _secondary_environment_parts(target)
    if predecessor is None:
        return
    _predecessor_values, _predecessor_unrelated, predecessor_secondary = _secondary_environment_parts(
        predecessor
    )
    if predecessor_secondary != target_secondary:
        raise ReleaseFailure("nonsecondary_transition_changed_secondary_environment")


def _staged_transition_validation_sha256(
    transition: str,
    predecessor_env_sha256: str,
    next_env_file: Path,
    next_env_file_sha256: str,
) -> str:
    """Bind a durable replay receipt to one already-validated ENV pair."""

    if transition not in _STAGED_CONFIG_TRANSITIONS:
        raise ReleaseFailure("staged_config_transition_invalid")
    predecessor = _closed_hash(
        predecessor_env_sha256,
        "staged_predecessor_env_digest_invalid",
    )
    target = _closed_hash(
        next_env_file_sha256,
        "staged_next_env_digest_invalid",
    )
    lexical = Path(os.path.abspath(next_env_file))
    if (
        not next_env_file.is_absolute()
        or lexical != next_env_file
        or any(character in str(lexical) for character in "\x00\r\n")
        or predecessor == target
    ):
        raise ReleaseFailure("staged_environment_identity_invalid")
    return _sha256_bytes(
        _canonical_json(
            {
                "schema": _STAGED_TRANSITION_VALIDATION_SCHEMA,
                "transition": transition,
                "predecessor_env_sha256": predecessor,
                "next_env_file": str(lexical),
                "next_env_file_sha256": target,
            }
        )
    )


def _canonical_secondary_environment(unrelated: bytes, values: Mapping[str, str]) -> bytes:
    return unrelated + b"".join(f"{key}={value}\n".encode() for key, value in sorted(values.items()))


def _validate_secondary_finalist_values(
    values: Mapping[str, str],
    *,
    exact_values: Mapping[str, str],
    invalid_code: str,
) -> None:
    expected_keys = set(exact_values) | {
        "FRIDAY_SECONDARY_LLM_API_KEY",
        "FRIDAY_SECONDARY_LLM_CA_FILE",
    }
    if set(values) != expected_keys or any(values.get(key) != value for key, value in exact_values.items()):
        raise ReleaseFailure(invalid_code)
    api_key = values["FRIDAY_SECONDARY_LLM_API_KEY"]
    if _HEX64.fullmatch(api_key) is None:
        raise ReleaseFailure(invalid_code)
    ca_raw = values["FRIDAY_SECONDARY_LLM_CA_FILE"]
    ca_path = Path(ca_raw)
    if (
        not ca_path.is_absolute()
        or Path(os.path.abspath(ca_path)) != ca_path
        or any(character in ca_raw for character in "\x00\r\n")
    ):
        raise ReleaseFailure("secondary_shadow_ca_invalid")
    ca = _read_private_regular_file(
        ca_path,
        maximum_bytes=1 << 20,
        code="secondary_shadow_ca_invalid",
    )
    if _sha256_bytes(ca) != _SECONDARY_FINALIST_CA_SHA256:
        raise ReleaseFailure("secondary_shadow_ca_digest_mismatch")


def _canonical_semantic_supervisor_environment_parts(
    raw: bytes,
    *,
    invalid_code: str,
) -> tuple[dict[str, str], bytes, dict[str, str]]:
    """Return one canonical supervisor block over the accepted private profile."""

    secondary_values, nonsecondary, secondary = _secondary_environment_parts(raw)
    _validate_secondary_finalist_values(
        secondary_values,
        exact_values=_SECONDARY_DOCUMENT_MAP_ASSIST_EXACT_VALUES,
        invalid_code="semantic_supervisor_secondary_prerequisite_invalid",
    )
    canonical_secondary = _canonical_environment_values(secondary_values)
    if secondary != canonical_secondary or raw != nonsecondary + canonical_secondary:
        raise ReleaseFailure("semantic_supervisor_secondary_prerequisite_invalid")

    values, unrelated, supervisor = _semantic_supervisor_environment_parts(nonsecondary)
    canonical_supervisor = _canonical_environment_values(values)
    if supervisor != canonical_supervisor or nonsecondary != unrelated + canonical_supervisor:
        raise ReleaseFailure(invalid_code)
    return values, unrelated, secondary_values


def _validate_semantic_supervisor_environment(
    raw: bytes,
    *,
    exact_values: Mapping[str, str],
    invalid_code: str,
) -> tuple[bytes, dict[str, str]]:
    """Require one exact canonical supervisor block over the accepted private profile."""

    values, unrelated, secondary_values = _canonical_semantic_supervisor_environment_parts(
        raw,
        invalid_code=invalid_code,
    )
    if values != exact_values:
        raise ReleaseFailure(invalid_code)
    return unrelated, secondary_values


def _semantic_supervisor_closed_json(raw: bytes, *, invalid_code: str) -> dict[str, Any]:
    """Decode one strict JSON object without importing candidate release code."""

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError, RecursionError) as exc:
        raise ReleaseFailure(invalid_code) from exc
    if type(parsed) is not dict:
        raise ReleaseFailure(invalid_code)
    return parsed


def _semantic_supervisor_latency_budget_identity(
    raw: bytes,
    *,
    expected_sha256: str,
    expected_mode: str,
    expected_source_sha256: str,
    invalid_code: str,
) -> tuple[str, int]:
    """Return the accepted source and maximum from one exact closed document."""

    if _sha256_bytes(raw) != expected_sha256:
        raise ReleaseFailure(invalid_code)
    document = _semantic_supervisor_closed_json(raw, invalid_code=invalid_code)
    maximum = document.get("maximum_user_visible_latency_ms")
    if (
        set(document) != _SEMANTIC_SUPERVISOR_LATENCY_BUDGET_KEYS
        or document.get("schema") != _SEMANTIC_SUPERVISOR_LATENCY_BUDGET_SCHEMA
        or document.get("budget_id") != _SEMANTIC_SUPERVISOR_LATENCY_BUDGET_ID
        or document.get("task_class") != _SEMANTIC_SUPERVISOR_PROMOTED_TASK
        or document.get("target_mode") != expected_mode
        or document.get("source_revision_sha256") != expected_source_sha256
        or document.get("latency_measurement") != _SEMANTIC_SUPERVISOR_LATENCY_MEASUREMENT
        or type(maximum) is not int
        or not 1 <= maximum <= _SEMANTIC_SUPERVISOR_MAX_LATENCY_MS
    ):
        raise ReleaseFailure(invalid_code)
    return expected_source_sha256, maximum


def _semantic_supervisor_is_digest(value: object) -> bool:
    return type(value) is str and _HEX64.fullmatch(value) is not None


def _semantic_supervisor_is_count(value: object) -> bool:
    return type(value) is int and value >= 0


def _semantic_supervisor_exact_counts(
    payload: Mapping[str, Any],
    keys: tuple[str, ...],
) -> bool:
    return all(_semantic_supervisor_is_count(payload.get(key)) for key in keys)


def _semantic_supervisor_exact_digests(
    payload: Mapping[str, Any],
    keys: tuple[str, ...],
) -> bool:
    return all(_semantic_supervisor_is_digest(payload.get(key)) for key in keys)


def _semantic_supervisor_exact_object(
    value: object,
    keys: frozenset[str],
    *,
    invalid_code: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise ReleaseFailure(invalid_code)
    return value


def _semantic_supervisor_bounded_count(value: object, *, invalid_code: str) -> int:
    if type(value) is not int or not 0 <= value <= 100_000:
        raise ReleaseFailure(invalid_code)
    return value


def _semantic_supervisor_count_map(
    value: object,
    *,
    invalid_code: str,
) -> dict[str, int]:
    if type(value) is not dict or len(value) > 256:
        raise ReleaseFailure(invalid_code)
    result: dict[str, int] = {}
    for key, count in value.items():
        if (
            type(key) is not str
            or re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{0,127}", key) is None
        ):
            raise ReleaseFailure(invalid_code)
        result[key] = _semantic_supervisor_bounded_count(
            count,
            invalid_code=invalid_code,
        )
    return result


def _semantic_supervisor_metric_window(
    value: object,
    *,
    stage: str,
    invalid_code: str,
) -> dict[str, Any]:
    item = _semantic_supervisor_exact_object(
        value,
        _SEMANTIC_SUPERVISOR_METRIC_WINDOW_KEYS,
        invalid_code=invalid_code,
    )
    observations = _semantic_supervisor_bounded_count(
        item.get("observation_count"),
        invalid_code=invalid_code,
    )
    complete = _semantic_supervisor_bounded_count(
        item.get("complete_count"),
        invalid_code=invalid_code,
    )
    latency_observations = _semantic_supervisor_bounded_count(
        item.get("latency_observation_count"),
        invalid_code=invalid_code,
    )
    latency_total = item.get("latency_total_ms")
    latency_max = item.get("latency_max_ms")
    completions = _semantic_supervisor_count_map(
        item.get("completion_counts"),
        invalid_code=invalid_code,
    )
    failures = _semantic_supervisor_count_map(
        item.get("failure_class_counts"),
        invalid_code=invalid_code,
    )
    if (
        item.get("schema") != _SEMANTIC_SUPERVISOR_PRODUCT_WINDOW_SCHEMA
        or item.get("stage") != stage
        or not _semantic_supervisor_is_digest(item.get("window_sha256"))
        or sum(completions.values()) != observations
        or sum(failures.values()) != observations
        or completions.get("complete", 0) != complete
        or latency_observations != observations
        or type(latency_total) is not int
        or type(latency_max) is not int
        or not 0 <= latency_max <= _SEMANTIC_SUPERVISOR_MAX_LATENCY_MS
        or not 0 <= latency_total <= _SEMANTIC_SUPERVISOR_MAX_LATENCY_MS * max(1, observations)
        or (observations == 0 and (latency_total != 0 or latency_max != 0))
        or (
            observations > 0
            and not latency_max <= latency_total <= latency_max * observations
        )
    ):
        raise ReleaseFailure(invalid_code)
    return item


def _semantic_supervisor_shadow_window(
    value: object,
    *,
    invalid_code: str,
) -> dict[str, Any]:
    item = _semantic_supervisor_exact_object(
        value,
        _SEMANTIC_SUPERVISOR_SHADOW_WINDOW_KEYS,
        invalid_code=invalid_code,
    )
    observations = _semantic_supervisor_bounded_count(
        item.get("observation_count"),
        invalid_code=invalid_code,
    )
    joined = _semantic_supervisor_bounded_count(
        item.get("joined_trace_count"),
        invalid_code=invalid_code,
    )
    readiness = _semantic_supervisor_bounded_count(
        item.get("readiness_observation_count"),
        invalid_code=invalid_code,
    )
    calls = _semantic_supervisor_bounded_count(
        item.get("call_rate_observation_count"),
        invalid_code=invalid_code,
    )
    invoked = _semantic_supervisor_bounded_count(
        item.get("supervisor_invocation_count"),
        invalid_code=invalid_code,
    )
    unnecessary = _semantic_supervisor_bounded_count(
        item.get("unnecessary_supervisor_invocation_count"),
        invalid_code=invalid_code,
    )
    visible = _semantic_supervisor_bounded_count(
        item.get("user_visible_observation_count"),
        invalid_code=invalid_code,
    )
    regressions = _semantic_supervisor_bounded_count(
        item.get("user_visible_regression_count"),
        invalid_code=invalid_code,
    )
    baseline = _semantic_supervisor_metric_window(
        item.get("baseline"),
        stage="shadow",
        invalid_code=invalid_code,
    )
    if (
        item.get("schema") != _SEMANTIC_SUPERVISOR_PRODUCT_WINDOW_SCHEMA
        or item.get("mode") != "shadow"
        or item.get("production_joined") is not True
        or item.get("actual_promoted_execution") is not False
        or item.get("quality_claim") != "documented_baseline_failure_only"
        or not _semantic_supervisor_is_digest(item.get("readiness_witness_sha256"))
        or joined != observations
        or calls != observations
        or readiness != baseline["observation_count"]
        or visible != readiness
        or readiness > observations
        or invoked > calls
        or unnecessary > invoked
        or regressions > visible
    ):
        raise ReleaseFailure(invalid_code)
    return item


def _semantic_supervisor_promoted_window(
    value: object,
    *,
    mode: str,
    invalid_code: str,
) -> dict[str, Any]:
    item = _semantic_supervisor_exact_object(
        value,
        _SEMANTIC_SUPERVISOR_PROMOTED_WINDOW_KEYS,
        invalid_code=invalid_code,
    )
    observations = _semantic_supervisor_bounded_count(
        item.get("observation_count"),
        invalid_code=invalid_code,
    )
    joined = _semantic_supervisor_bounded_count(
        item.get("joined_trace_count"),
        invalid_code=invalid_code,
    )
    evidence_count = _semantic_supervisor_bounded_count(
        item.get("promotion_evidence_count"),
        invalid_code=invalid_code,
    )
    calls = _semantic_supervisor_bounded_count(
        item.get("call_rate_observation_count"),
        invalid_code=invalid_code,
    )
    invoked = _semantic_supervisor_bounded_count(
        item.get("supervisor_invocation_count"),
        invalid_code=invalid_code,
    )
    unnecessary = _semantic_supervisor_bounded_count(
        item.get("unnecessary_supervisor_invocation_count"),
        invalid_code=invalid_code,
    )
    visible = _semantic_supervisor_bounded_count(
        item.get("user_visible_observation_count"),
        invalid_code=invalid_code,
    )
    regressions = _semantic_supervisor_bounded_count(
        item.get("user_visible_regression_count"),
        invalid_code=invalid_code,
    )
    promoted = _semantic_supervisor_metric_window(
        item.get("promoted"),
        stage=mode,
        invalid_code=invalid_code,
    )
    evidence_sha256 = item.get("promotion_evidence_sha256")
    if (
        item.get("schema") != _SEMANTIC_SUPERVISOR_PRODUCT_WINDOW_SCHEMA
        or item.get("mode") != mode
        or item.get("production_joined") is not True
        or item.get("actual_promoted_execution") is not True
        or not _semantic_supervisor_is_digest(item.get("product_window_sha256"))
        or (
            evidence_count == 1
            and not _semantic_supervisor_is_digest(evidence_sha256)
        )
        or (evidence_count != 1 and evidence_sha256 is not None)
        or joined != observations
        or calls != observations
        or promoted["observation_count"] > observations
        or invoked > calls
        or unnecessary > invoked
        or visible > promoted["observation_count"]
        or regressions > visible
    ):
        raise ReleaseFailure(invalid_code)
    return item


def _semantic_supervisor_baseline_identity(
    value: object,
    *,
    invalid_code: str,
) -> tuple[str, str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    report = _semantic_supervisor_exact_object(
        value,
        _SEMANTIC_SUPERVISOR_BASELINE_KEYS,
        invalid_code=invalid_code,
    )
    raw = _canonical_json(report) + b"\n"
    if report.get("schema") != _SEMANTIC_SUPERVISOR_PRODUCTION_BASELINE_SCHEMA:
        raise ReleaseFailure(invalid_code)
    report_sha256 = report.get("report_sha256")
    unsigned = dict(report)
    unsigned.pop("report_sha256")
    if (
        not _semantic_supervisor_is_digest(report_sha256)
        or _sha256_bytes(_canonical_json(unsigned)) != report_sha256
    ):
        raise ReleaseFailure(invalid_code)
    evidence = _semantic_supervisor_exact_object(
        report.get("evidence"),
        _SEMANTIC_SUPERVISOR_BASELINE_EVIDENCE_KEYS,
        invalid_code=invalid_code,
    )
    if evidence != {
        "kind": _SEMANTIC_SUPERVISOR_PRODUCTION_BASELINE_KIND,
        "body_free": True,
        "production_acceptance": False,
        "acceptance_authority": "operator_review_required",
        "representative_window_attested": False,
        "promotion_authority": False,
    }:
        raise ReleaseFailure(invalid_code)
    sample = _semantic_supervisor_exact_object(
        report.get("sample"),
        _SEMANTIC_SUPERVISOR_BASELINE_SAMPLE_KEYS,
        invalid_code=invalid_code,
    )
    limit = _semantic_supervisor_bounded_count(
        sample.get("limit"),
        invalid_code=invalid_code,
    )
    traces = _semantic_supervisor_bounded_count(
        sample.get("turn_traces"),
        invalid_code=invalid_code,
    )
    joins = _semantic_supervisor_bounded_count(
        sample.get("joined_supervisor_events"),
        invalid_code=invalid_code,
    )
    promoted_rows = _semantic_supervisor_bounded_count(
        sample.get("promoted_product_events"),
        invalid_code=invalid_code,
    )
    if (
        limit < 1
        or max(traces, joins, promoted_rows) > limit
        or any(
            _semantic_supervisor_bounded_count(
                sample.get(key),
                invalid_code=invalid_code,
            )
            != 0
            for key in _SEMANTIC_SUPERVISOR_BASELINE_ANOMALY_KEYS
        )
    ):
        raise ReleaseFailure(invalid_code)
    primary = _semantic_supervisor_exact_object(
        report.get("primary_baseline"),
        _SEMANTIC_SUPERVISOR_BASELINE_PRIMARY_KEYS,
        invalid_code=invalid_code,
    )
    for key in (
        "intent_counts",
        "playbook_counts",
        "completion_counts",
        "publication_counts",
        "failure_counts",
    ):
        if sum(
            _semantic_supervisor_count_map(
                primary.get(key),
                invalid_code=invalid_code,
            ).values()
        ) != traces:
            raise ReleaseFailure(invalid_code)
    for key in (
        "authority_rechecked_count",
        "partial_coverage_count",
        "state_restored_count",
    ):
        if (
            _semantic_supervisor_bounded_count(
                primary.get(key),
                invalid_code=invalid_code,
            )
            > traces
        ):
            raise ReleaseFailure(invalid_code)
    joined = _semantic_supervisor_exact_object(
        report.get("supervisor_join"),
        _SEMANTIC_SUPERVISOR_BASELINE_JOIN_KEYS,
        invalid_code=invalid_code,
    )
    for key in (
        "task_counts",
        "skip_counts",
        "parse_counts",
        "policy_reason_counts",
        "planner_latency_bucket_counts",
        "actual_completion_counts",
        "actual_publication_counts",
    ):
        if sum(
            _semantic_supervisor_count_map(
                joined.get(key),
                invalid_code=invalid_code,
            ).values()
        ) != joins:
            raise ReleaseFailure(invalid_code)
    _semantic_supervisor_count_map(
        joined.get("actual_capability_outcome_counts"),
        invalid_code=invalid_code,
    )
    for key in (
        "invoked_count",
        "admitted_count",
        "final_authority_rechecked_count",
        "state_restored_count",
        "retry_occurred_count",
    ):
        if (
            _semantic_supervisor_bounded_count(
                joined.get(key),
                invalid_code=invalid_code,
            )
            > joins
        ):
            raise ReleaseFailure(invalid_code)
    product_windows = _semantic_supervisor_exact_object(
        report.get("product_windows"),
        frozenset({"shadow_readiness", "promoted_execution"}),
        invalid_code=invalid_code,
    )
    shadow = _semantic_supervisor_shadow_window(
        product_windows.get("shadow_readiness"),
        invalid_code=invalid_code,
    )
    promoted = _semantic_supervisor_exact_object(
        product_windows.get("promoted_execution"),
        frozenset({"assist", "canary"}),
        invalid_code=invalid_code,
    )
    assist = _semantic_supervisor_promoted_window(
        promoted.get("assist"),
        mode="assist",
        invalid_code=invalid_code,
    )
    canary = _semantic_supervisor_promoted_window(
        promoted.get("canary"),
        mode="canary",
        invalid_code=invalid_code,
    )
    if (
        shadow["observation_count"] != joins
        or assist["observation_count"] + canary["observation_count"]
        != promoted_rows
    ):
        raise ReleaseFailure(invalid_code)
    return _sha256_bytes(raw), report_sha256, shadow, assist, canary


def _semantic_supervisor_readiness_evidence_is_live(
    product: Mapping[str, Any],
    *,
    observation_count: int,
    source_sha256: str,
    latency_budget_sha256: str,
    latency_budget_ms: int,
) -> bool:
    count_keys = (
        "baseline_observation_count",
        "baseline_complete_count",
        "baseline_failure_class_count",
        "readiness_observation_count",
        "latency_budget_ms",
        "latency_total_ms",
        "latency_max_ms",
        "call_rate_observation_count",
        "supervisor_invocation_count",
        "unnecessary_supervisor_invocation_count",
        "user_visible_observation_count",
        "user_visible_regression_count",
    )
    digest_keys = (
        "baseline_window_sha256",
        "documented_failure_class_sha256",
        "readiness_witness_sha256",
        "latency_budget_source_revision_sha256",
        "latency_budget_sha256",
    )
    if not _semantic_supervisor_exact_counts(
        product,
        count_keys,
    ) or not _semantic_supervisor_exact_digests(product, digest_keys):
        return False

    baseline_observations = product["baseline_observation_count"]
    baseline_complete = product["baseline_complete_count"]
    baseline_failures = product["baseline_failure_class_count"]
    readiness_observations = product["readiness_observation_count"]
    latency_total = product["latency_total_ms"]
    latency_max = product["latency_max_ms"]
    call_rate_observations = product["call_rate_observation_count"]
    supervisor_invocations = product["supervisor_invocation_count"]
    unnecessary_invocations = product["unnecessary_supervisor_invocation_count"]
    visible_observations = product["user_visible_observation_count"]
    visible_regressions = product["user_visible_regression_count"]
    failure_class_id = product.get("documented_failure_class_id")
    return bool(
        product.get("schema") == _SEMANTIC_SUPERVISOR_READINESS_EVIDENCE_SCHEMA
        and type(failure_class_id) is str
        and failure_class_id != "none"
        and _SEMANTIC_SUPERVISOR_SAFE_FAILURE_CLASS_ID.fullmatch(failure_class_id) is not None
        and product.get("latency_budget_target_mode") == "assist"
        and product.get("latency_budget_source_revision_sha256") == source_sha256
        and product.get("latency_budget_sha256") == latency_budget_sha256
        and product.get("latency_budget_ms") == latency_budget_ms
        and baseline_observations >= _SEMANTIC_SUPERVISOR_MIN_PRODUCT_OBSERVATIONS
        and readiness_observations >= _SEMANTIC_SUPERVISOR_MIN_PRODUCT_OBSERVATIONS
        and readiness_observations <= observation_count
        and call_rate_observations == observation_count
        and visible_observations == readiness_observations
        and baseline_complete <= baseline_observations
        and 1 <= baseline_failures <= baseline_observations
        and supervisor_invocations <= call_rate_observations
        and unnecessary_invocations <= supervisor_invocations
        and unnecessary_invocations * 10_000
        <= _SEMANTIC_SUPERVISOR_MAX_UNNECESSARY_CALL_RATE_BPS * call_rate_observations
        and visible_regressions == 0
        and latency_max <= _SEMANTIC_SUPERVISOR_MAX_LATENCY_MS
        and latency_max <= latency_total <= latency_max * readiness_observations
        and latency_max <= latency_budget_ms
        and latency_total <= latency_budget_ms * readiness_observations
    )


def _semantic_supervisor_outcome_evidence_is_live(
    product: Mapping[str, Any],
    *,
    observation_count: int,
    source_sha256: str,
    latency_budget_sha256: str,
    latency_budget_ms: int,
) -> bool:
    count_keys = (
        "baseline_observation_count",
        "baseline_complete_count",
        "promoted_observation_count",
        "promoted_complete_count",
        "baseline_failure_class_count",
        "promoted_failure_class_count",
        "latency_budget_ms",
        "latency_observation_count",
        "latency_total_ms",
        "latency_max_ms",
        "call_rate_observation_count",
        "supervisor_invocation_count",
        "unnecessary_supervisor_invocation_count",
        "user_visible_observation_count",
        "user_visible_regression_count",
    )
    digest_keys = (
        "baseline_window_sha256",
        "promoted_window_sha256",
        "latency_budget_source_revision_sha256",
        "latency_budget_sha256",
    )
    if not _semantic_supervisor_exact_counts(
        product,
        count_keys,
    ) or not _semantic_supervisor_exact_digests(product, digest_keys):
        return False

    baseline_window = product["baseline_window_sha256"]
    promoted_window = product["promoted_window_sha256"]
    baseline_observations = product["baseline_observation_count"]
    baseline_complete = product["baseline_complete_count"]
    promoted_observations = product["promoted_observation_count"]
    promoted_complete = product["promoted_complete_count"]
    baseline_failures = product["baseline_failure_class_count"]
    promoted_failures = product["promoted_failure_class_count"]
    latency_observations = product["latency_observation_count"]
    latency_total = product["latency_total_ms"]
    latency_max = product["latency_max_ms"]
    call_rate_observations = product["call_rate_observation_count"]
    supervisor_invocations = product["supervisor_invocation_count"]
    unnecessary_invocations = product["unnecessary_supervisor_invocation_count"]
    visible_observations = product["user_visible_observation_count"]
    visible_regressions = product["user_visible_regression_count"]
    failure_class_id = product.get("documented_failure_class_id")
    failure_class_sha256 = product.get("documented_failure_class_sha256")
    quality_basis = product.get("quality_basis")
    completion_improved = (
        promoted_complete * baseline_observations
        > baseline_complete * promoted_observations
    )
    completion_claim_is_valid = bool(
        quality_basis == "completion_rate_improvement"
        and failure_class_id == "none"
        and failure_class_sha256 is None
        and baseline_failures == 0
        and promoted_failures == 0
        and completion_improved
    )
    removal_claim_is_valid = bool(
        quality_basis == "documented_failure_class_removal"
        and type(failure_class_id) is str
        and failure_class_id != "none"
        and _SEMANTIC_SUPERVISOR_SAFE_FAILURE_CLASS_ID.fullmatch(failure_class_id) is not None
        and _semantic_supervisor_is_digest(failure_class_sha256)
        and baseline_failures >= 1
        and promoted_failures == 0
    )
    latency_aggregate_is_valid = (
        latency_max <= latency_total <= latency_max * latency_observations
        if latency_observations
        else latency_total == latency_max == 0
    )
    return bool(
        product.get("schema") == _SEMANTIC_SUPERVISOR_OUTCOME_EVIDENCE_SCHEMA
        and product.get("latency_budget_target_mode") == "canary"
        and product.get("latency_budget_source_revision_sha256") == source_sha256
        and product.get("latency_budget_sha256") == latency_budget_sha256
        and product.get("latency_budget_ms") == latency_budget_ms
        and baseline_window != promoted_window
        and baseline_observations >= _SEMANTIC_SUPERVISOR_MIN_PRODUCT_OBSERVATIONS
        and promoted_observations >= _SEMANTIC_SUPERVISOR_MIN_PRODUCT_OBSERVATIONS
        and promoted_observations <= observation_count
        and baseline_complete <= baseline_observations
        and promoted_complete <= promoted_observations
        and baseline_failures <= baseline_observations
        and promoted_failures <= promoted_observations
        and latency_observations == promoted_observations
        and call_rate_observations == observation_count
        and supervisor_invocations <= call_rate_observations
        and unnecessary_invocations <= supervisor_invocations
        and unnecessary_invocations * 10_000
        <= _SEMANTIC_SUPERVISOR_MAX_UNNECESSARY_CALL_RATE_BPS * call_rate_observations
        and visible_observations == promoted_observations
        and visible_regressions == 0
        and latency_max <= _SEMANTIC_SUPERVISOR_MAX_LATENCY_MS
        and latency_aggregate_is_valid
        and latency_max <= latency_budget_ms
        and latency_total <= latency_budget_ms * latency_observations
        and (completion_claim_is_valid or removal_claim_is_valid)
    )


def _validate_semantic_supervisor_evidence_budget_binding(
    raw: bytes,
    *,
    mode: str,
    source_sha256: str,
    registry_sha256: str,
    latency_budget_sha256: str,
    latency_budget_ms: int,
    invalid_code: str,
    expected_precursor_assist_evidence_sha256: str | None = None,
) -> str:
    """Validate evidence and return its canonical payload digest for chaining."""

    evidence = _semantic_supervisor_closed_json(raw, invalid_code=invalid_code)
    product = evidence.get("product_evidence")
    expected_observed_mode = "shadow" if mode == "assist" else "assist"
    expected_observed_policy_id = (
        _SEMANTIC_SUPERVISOR_SHADOW_POLICY_ID
        if mode == "assist"
        else _SEMANTIC_SUPERVISOR_ASSIST_POLICY_ID
    )
    expected_observed_policy_sha256 = (
        _SEMANTIC_SUPERVISOR_SHADOW_POLICY_SHA256
        if mode == "assist"
        else _SEMANTIC_SUPERVISOR_ASSIST_POLICY_SHA256
    )
    expected_product_schema = (
        _SEMANTIC_SUPERVISOR_READINESS_EVIDENCE_SCHEMA
        if mode == "assist"
        else _SEMANTIC_SUPERVISOR_OUTCOME_EVIDENCE_SCHEMA
    )
    expected_product_keys = (
        _SEMANTIC_SUPERVISOR_READINESS_EVIDENCE_KEYS
        if mode == "assist"
        else _SEMANTIC_SUPERVISOR_OUTCOME_EVIDENCE_KEYS
    )
    count_keys = (
        "observation_count",
        "joined_trace_count",
        "hidden_owner_count",
        "duplicate_capability_count",
        "duplicate_effect_count",
        "duplicate_publication_count",
        "false_completion_regression_count",
    )
    digest_keys = (
        "source_revision_sha256",
        "promotion_policy_sha256",
        "observed_policy_sha256",
        "target_policy_sha256",
        "runtime_profile_manifest_sha256",
        "registry_binding_sha256",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "operator_attestation_sha256",
    )
    proof_keys = (
        "representative_window_attested",
        "primary_fallback_proven",
        "laptop_unavailable_fallback_proven",
        "final_authority_recheck_proven",
        "primary_publication_owner_proven",
    )
    anomaly_keys = (
        "hidden_owner_count",
        "duplicate_capability_count",
        "duplicate_effect_count",
        "duplicate_publication_count",
        "false_completion_regression_count",
    )
    evidence_id = evidence.get("evidence_id")
    precursor = evidence.get("precursor_assist_promotion_evidence_sha256")
    provenance_is_valid = (
        precursor is None and expected_precursor_assist_evidence_sha256 is None
        if mode == "assist"
        else (
            type(precursor) is str
            and _HEX64.fullmatch(precursor) is not None
            and (
                expected_precursor_assist_evidence_sha256 is None
                or precursor == expected_precursor_assist_evidence_sha256
            )
        )
    )
    if (
        set(evidence) != _SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_KEYS
        or evidence.get("schema") != _SEMANTIC_SUPERVISOR_PROMOTION_SCHEMA
        or type(evidence_id) is not str
        or _SEMANTIC_SUPERVISOR_SAFE_EVIDENCE_ID.fullmatch(evidence_id) is None
        or evidence.get("authority") != "production_joined"
        or evidence.get("observed_mode") != expected_observed_mode
        or evidence.get("task_class") != _SEMANTIC_SUPERVISOR_PROMOTED_TASK
        or evidence.get("source_revision_sha256") != source_sha256
        or evidence.get("promotion_policy_sha256")
        != _SEMANTIC_SUPERVISOR_PROMOTION_POLICY_SHA256
        or evidence.get("observed_policy_id") != expected_observed_policy_id
        or evidence.get("observed_policy_sha256") != expected_observed_policy_sha256
        or evidence.get("target_policy_id") != _SEMANTIC_SUPERVISOR_ASSIST_POLICY_ID
        or evidence.get("target_policy_sha256") != _SEMANTIC_SUPERVISOR_ASSIST_POLICY_SHA256
        or evidence.get("runtime_profile_id") != _SEMANTIC_SUPERVISOR_RUNTIME_PROFILE_ID
        or evidence.get("runtime_profile_manifest_sha256")
        != _SEMANTIC_SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        or evidence.get("registry_binding_sha256") != registry_sha256
        or not _semantic_supervisor_exact_digests(evidence, digest_keys)
        or not provenance_is_valid
        or type(evidence.get("max_steps")) is not int
        or evidence.get("max_steps") != 6
        or type(evidence.get("max_review_rounds")) is not int
        or evidence.get("max_review_rounds") != 1
        or not _semantic_supervisor_exact_counts(evidence, count_keys)
        or evidence["observation_count"] < _SEMANTIC_SUPERVISOR_MIN_PRODUCT_OBSERVATIONS
        or evidence["joined_trace_count"] != evidence["observation_count"]
        or any(evidence.get(key) is not True for key in proof_keys)
        or any(evidence.get(key) != 0 for key in anomaly_keys)
        or type(product) is not dict
        or set(product) != expected_product_keys
        or product.get("schema") != expected_product_schema
        or product.get("latency_budget_target_mode") != mode
        or product.get("latency_budget_source_revision_sha256") != source_sha256
        or product.get("latency_budget_sha256") != latency_budget_sha256
        or product.get("latency_budget_ms") != latency_budget_ms
    ):
        raise ReleaseFailure(invalid_code)
    product_is_live = (
        _semantic_supervisor_readiness_evidence_is_live(
            product,
            observation_count=evidence["observation_count"],
            source_sha256=source_sha256,
            latency_budget_sha256=latency_budget_sha256,
            latency_budget_ms=latency_budget_ms,
        )
        if mode == "assist"
        else _semantic_supervisor_outcome_evidence_is_live(
            product,
            observation_count=evidence["observation_count"],
            source_sha256=source_sha256,
            latency_budget_sha256=latency_budget_sha256,
            latency_budget_ms=latency_budget_ms,
        )
    )
    if not product_is_live:
        raise ReleaseFailure(invalid_code)
    return _sha256_bytes(_canonical_json(evidence))


def _semantic_supervisor_expected_bundle_evidence(
    *,
    evidence: dict[str, Any],
    attestation: dict[str, Any],
    baseline_file_sha256: str,
    baseline_report_sha256: str,
    shadow: dict[str, Any],
    assist: dict[str, Any],
    mode: str,
    source_sha256: str,
    registry_sha256: str,
    latency_budget_sha256: str,
    latency_budget_ms: int,
    invalid_code: str,
) -> dict[str, Any]:
    product = evidence.get("product_evidence")
    if type(product) is not dict:
        raise ReleaseFailure(invalid_code)
    shadow_baseline = shadow["baseline"]
    if type(shadow_baseline) is not dict:
        raise ReleaseFailure(invalid_code)
    if mode == "assist":
        failure_id = product.get("documented_failure_class_id")
        failure_sha256 = product.get("documented_failure_class_sha256")
        failure_counts = shadow_baseline["failure_class_counts"]
        if (
            type(failure_counts) is not dict
            or type(failure_id) is not str
            or not _semantic_supervisor_is_digest(failure_sha256)
            or type(failure_counts.get(failure_id)) is not int
            or failure_counts[failure_id] < 1
        ):
            raise ReleaseFailure(invalid_code)
        expected_product: dict[str, Any] = {
            "schema": _SEMANTIC_SUPERVISOR_READINESS_EVIDENCE_SCHEMA,
            "baseline_window_sha256": shadow_baseline["window_sha256"],
            "baseline_observation_count": shadow_baseline["observation_count"],
            "baseline_complete_count": shadow_baseline["complete_count"],
            "documented_failure_class_id": failure_id,
            "documented_failure_class_sha256": failure_sha256,
            "baseline_failure_class_count": failure_counts[failure_id],
            "readiness_witness_sha256": shadow["readiness_witness_sha256"],
            "readiness_observation_count": shadow["readiness_observation_count"],
            "latency_budget_target_mode": mode,
            "latency_budget_source_revision_sha256": source_sha256,
            "latency_budget_ms": latency_budget_ms,
            "latency_budget_sha256": latency_budget_sha256,
            "latency_total_ms": shadow_baseline["latency_total_ms"],
            "latency_max_ms": shadow_baseline["latency_max_ms"],
            "call_rate_observation_count": shadow["call_rate_observation_count"],
            "supervisor_invocation_count": shadow["supervisor_invocation_count"],
            "unnecessary_supervisor_invocation_count": shadow[
                "unnecessary_supervisor_invocation_count"
            ],
            "user_visible_observation_count": shadow["user_visible_observation_count"],
            "user_visible_regression_count": shadow["user_visible_regression_count"],
        }
        observation_count = shadow["observation_count"]
        joined_trace_count = shadow["joined_trace_count"]
        observed_mode = "shadow"
        observed_policy_id = _SEMANTIC_SUPERVISOR_SHADOW_POLICY_ID
        observed_policy_sha256 = _SEMANTIC_SUPERVISOR_SHADOW_POLICY_SHA256
    else:
        assist_promoted = assist["promoted"]
        if type(assist_promoted) is not dict:
            raise ReleaseFailure(invalid_code)
        quality_basis = attestation.get("quality_basis")
        failure_id = product.get("documented_failure_class_id")
        failure_sha256 = product.get("documented_failure_class_sha256")
        baseline_failures = shadow_baseline["failure_class_counts"]
        promoted_failures = assist_promoted["failure_class_counts"]
        if type(baseline_failures) is not dict or type(promoted_failures) is not dict:
            raise ReleaseFailure(invalid_code)
        if quality_basis == "completion_rate_improvement":
            if failure_id != "none" or failure_sha256 is not None:
                raise ReleaseFailure(invalid_code)
            baseline_failure_count = 0
            promoted_failure_count = 0
        elif quality_basis == "documented_failure_class_removal":
            if (
                type(failure_id) is not str
                or not _semantic_supervisor_is_digest(failure_sha256)
                or type(baseline_failures.get(failure_id)) is not int
                or baseline_failures[failure_id] < 1
                or promoted_failures.get(failure_id, 0) != 0
            ):
                raise ReleaseFailure(invalid_code)
            baseline_failure_count = baseline_failures[failure_id]
            promoted_failure_count = 0
        else:
            raise ReleaseFailure(invalid_code)
        expected_product = {
            "schema": _SEMANTIC_SUPERVISOR_OUTCOME_EVIDENCE_SCHEMA,
            "quality_basis": quality_basis,
            "baseline_window_sha256": shadow_baseline["window_sha256"],
            "promoted_window_sha256": assist_promoted["window_sha256"],
            "baseline_observation_count": shadow_baseline["observation_count"],
            "baseline_complete_count": shadow_baseline["complete_count"],
            "promoted_observation_count": assist_promoted["observation_count"],
            "promoted_complete_count": assist_promoted["complete_count"],
            "documented_failure_class_id": failure_id,
            "documented_failure_class_sha256": failure_sha256,
            "baseline_failure_class_count": baseline_failure_count,
            "promoted_failure_class_count": promoted_failure_count,
            "latency_budget_target_mode": mode,
            "latency_budget_source_revision_sha256": source_sha256,
            "latency_budget_ms": latency_budget_ms,
            "latency_budget_sha256": latency_budget_sha256,
            "latency_observation_count": assist_promoted["latency_observation_count"],
            "latency_total_ms": assist_promoted["latency_total_ms"],
            "latency_max_ms": assist_promoted["latency_max_ms"],
            "call_rate_observation_count": assist["call_rate_observation_count"],
            "supervisor_invocation_count": assist["supervisor_invocation_count"],
            "unnecessary_supervisor_invocation_count": assist[
                "unnecessary_supervisor_invocation_count"
            ],
            "user_visible_observation_count": assist["user_visible_observation_count"],
            "user_visible_regression_count": assist["user_visible_regression_count"],
        }
        observation_count = assist["observation_count"]
        joined_trace_count = assist["joined_trace_count"]
        observed_mode = "assist"
        observed_policy_id = _SEMANTIC_SUPERVISOR_ASSIST_POLICY_ID
        observed_policy_sha256 = _SEMANTIC_SUPERVISOR_ASSIST_POLICY_SHA256
    expected = {
        "schema": _SEMANTIC_SUPERVISOR_PROMOTION_SCHEMA,
        "evidence_id": evidence.get("evidence_id"),
        "authority": "production_joined",
        "observed_mode": observed_mode,
        "task_class": _SEMANTIC_SUPERVISOR_PROMOTED_TASK,
        "source_revision_sha256": source_sha256,
        "promotion_policy_sha256": _SEMANTIC_SUPERVISOR_PROMOTION_POLICY_SHA256,
        "observed_policy_id": observed_policy_id,
        "observed_policy_sha256": observed_policy_sha256,
        "target_policy_id": _SEMANTIC_SUPERVISOR_ASSIST_POLICY_ID,
        "target_policy_sha256": _SEMANTIC_SUPERVISOR_ASSIST_POLICY_SHA256,
        "runtime_profile_id": _SEMANTIC_SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": (
            _SEMANTIC_SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        ),
        "registry_binding_sha256": registry_sha256,
        "baseline_file_sha256": baseline_file_sha256,
        "baseline_report_sha256": baseline_report_sha256,
        "operator_attestation_sha256": _sha256_bytes(_canonical_json(attestation)),
        "precursor_assist_promotion_evidence_sha256": attestation.get(
            "precursor_assist_promotion_evidence_sha256"
        ),
        "max_steps": 6,
        "max_review_rounds": 1,
        "observation_count": observation_count,
        "joined_trace_count": joined_trace_count,
        "representative_window_attested": True,
        "primary_fallback_proven": True,
        "laptop_unavailable_fallback_proven": True,
        "final_authority_recheck_proven": True,
        "primary_publication_owner_proven": True,
        "hidden_owner_count": 0,
        "duplicate_capability_count": 0,
        "duplicate_effect_count": 0,
        "duplicate_publication_count": 0,
        "false_completion_regression_count": 0,
        "product_evidence": expected_product,
    }
    if evidence != expected:
        raise ReleaseFailure(invalid_code)
    return evidence


def _semantic_supervisor_representative_window_sha256(value: Mapping[str, Any]) -> str:
    """Match the server witness canonical domain without importing candidate code."""

    return _sha256_bytes(_canonical_json(value) + b"\n")


def _validate_semantic_supervisor_representative_window_issue(
    value: object,
    *,
    baseline: Mapping[str, Any],
    baseline_file_sha256: str,
    baseline_report_sha256: str,
    shadow: Mapping[str, Any],
    assist: Mapping[str, Any],
    mode: str,
    source_sha256: str,
    registry_sha256: str,
    latency_budget_sha256: str,
    latency_budget_ms: int,
    precursor_assist_evidence_sha256: object,
    invalid_code: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the body-free issue envelope; the live server verifies its HMAC."""

    issue = _semantic_supervisor_exact_object(
        value,
        _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ISSUE_KEYS,
        invalid_code=invalid_code,
    )
    attestation = _semantic_supervisor_exact_object(
        issue.get("server_attestation"),
        _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ATTESTATION_KEYS,
        invalid_code=invalid_code,
    )
    expected_observed_mode = "shadow" if mode == "assist" else "assist"
    expected_window_sha256 = (
        shadow.get("readiness_witness_sha256")
        if mode == "assist"
        else assist.get("product_window_sha256")
    )
    expected_joined_trace_count = (
        shadow.get("joined_trace_count")
        if mode == "assist"
        else assist.get("joined_trace_count")
    )
    sample = _semantic_supervisor_exact_object(
        baseline.get("sample"),
        _SEMANTIC_SUPERVISOR_BASELINE_SAMPLE_KEYS,
        invalid_code=invalid_code,
    )
    lookup_token = issue.get("attestation_lookup_token")
    lookup_token_sha256 = issue.get("lookup_token_sha256")
    server_attestation_sha256 = issue.get("server_attestation_sha256")
    digest_fields = (
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "latency_budget_document_sha256",
        "latency_budget_source_revision_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "primary_process_epoch_sha256",
        "observed_release_metadata_sha256",
        "observed_release_tree_sha256",
        "observed_registry_binding_sha256",
        "supervisor_policy_sha256",
        "runtime_profile_manifest_sha256",
        "observer_runner_sha256",
        "representative_window_sha256",
        "lookup_token_sha256",
        "signature",
    )
    if (
        issue.get("schema")
        != _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ISSUE_RESPONSE_SCHEMA
        or issue.get("status") != "unused"
        or issue.get("state_version") != 1
        or attestation.get("schema")
        != _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ATTESTATION_SCHEMA
        or re.fullmatch(r"sswindow_[0-9a-f]{32}", str(attestation.get("attestation_id")))
        is None
        or attestation.get("authority")
        != _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_AUTHORITY
        or attestation.get("target_mode") != mode
        or attestation.get("observed_mode") != expected_observed_mode
        or attestation.get("requested_mode") != "assist"
        or attestation.get("baseline_file_sha256") != baseline_file_sha256
        or attestation.get("baseline_report_sha256") != baseline_report_sha256
        or attestation.get("latency_budget_file_sha256") != latency_budget_sha256
        or attestation.get("latency_budget_document_sha256") != latency_budget_sha256
        or attestation.get("latency_budget_target_mode") != mode
        or attestation.get("latency_budget_source_revision_sha256") != source_sha256
        or attestation.get("maximum_user_visible_latency_ms") != latency_budget_ms
        or attestation.get("source_revision_sha256") != source_sha256
        or attestation.get("registry_binding_sha256") != registry_sha256
        or attestation.get("observed_registry_binding_sha256") != registry_sha256
        or attestation.get("supervisor_policy_id")
        != _SEMANTIC_SUPERVISOR_ASSIST_POLICY_ID
        or attestation.get("supervisor_policy_sha256")
        != _SEMANTIC_SUPERVISOR_ASSIST_POLICY_SHA256
        or attestation.get("runtime_profile_id") != _SEMANTIC_SUPERVISOR_RUNTIME_PROFILE_ID
        or attestation.get("runtime_profile_manifest_sha256")
        != _SEMANTIC_SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        or attestation.get("representative_window_sha256") != expected_window_sha256
        or attestation.get("joined_trace_count") != expected_joined_trace_count
        or attestation.get("turn_trace_count") != sample.get("turn_traces")
        or attestation.get("sample_limit") != sample.get("limit")
        or attestation.get("server_recomputed") is not True
        or attestation.get("representative_window_attested") is not True
        or attestation.get("synthetic_authority") is not False
        or attestation.get("precursor_assist_promotion_evidence_sha256")
        != precursor_assist_evidence_sha256
        or type(lookup_token) is not str
        or _HEX64.fullmatch(lookup_token) is None
        or set(lookup_token) == {"0"}
        or not _semantic_supervisor_is_digest(lookup_token_sha256)
        or _sha256_bytes(lookup_token.encode("ascii")) != lookup_token_sha256
        or attestation.get("lookup_token_sha256") != lookup_token_sha256
        or not _semantic_supervisor_is_digest(server_attestation_sha256)
        or _semantic_supervisor_representative_window_sha256(attestation)
        != server_attestation_sha256
        or not _semantic_supervisor_exact_digests(attestation, digest_fields)
        or type(attestation.get("primary_pid")) is not int
        or attestation["primary_pid"] <= 0
        or _HEX40.fullmatch(str(attestation.get("observed_release_commit"))) is None
        or type(attestation.get("primary_backend_version")) is not str
        or not attestation["primary_backend_version"]
        or type(attestation.get("sample_limit")) is not int
        or type(attestation.get("turn_trace_count")) is not int
        or type(attestation.get("joined_trace_count")) is not int
        or not 0
        < attestation["joined_trace_count"]
        <= attestation["turn_trace_count"]
        < attestation["sample_limit"]
        or type(attestation.get("issued_at")) is not int
        or type(attestation.get("expires_at")) is not int
        or not attestation["issued_at"] < attestation["expires_at"]
        or attestation.get("state_version") != 1
    ):
        raise ReleaseFailure(invalid_code)
    return issue, attestation


def _semantic_supervisor_representative_window_consume_request(
    issue: Mapping[str, Any],
) -> dict[str, Any]:
    attestation = issue.get("server_attestation")
    if (
        type(attestation) is not dict
        or set(issue) != _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ISSUE_KEYS
        or set(attestation)
        != _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ATTESTATION_KEYS
    ):
        raise ReleaseFailure("semantic_supervisor_representative_window_issue_invalid")
    return {
        "schema": _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_CONSUME_REQUEST_SCHEMA,
        "attestation_lookup_token": issue["attestation_lookup_token"],
        "server_attestation_sha256": issue["server_attestation_sha256"],
        "target_mode": attestation["target_mode"],
        "baseline_file_sha256": attestation["baseline_file_sha256"],
        "baseline_report_sha256": attestation["baseline_report_sha256"],
        "latency_budget_file_sha256": attestation["latency_budget_file_sha256"],
        "latency_budget_document_sha256": attestation[
            "latency_budget_document_sha256"
        ],
        "source_revision_sha256": attestation["source_revision_sha256"],
        "registry_binding_sha256": attestation["registry_binding_sha256"],
        "observer_runner_sha256": attestation["observer_runner_sha256"],
        "precursor_assist_promotion_evidence_sha256": attestation[
            "precursor_assist_promotion_evidence_sha256"
        ],
    }


def _validate_semantic_supervisor_representative_window_consume_response(
    value: object,
    *,
    request: Mapping[str, Any],
    attestation: Mapping[str, Any],
) -> None:
    response = _semantic_supervisor_exact_object(
        value,
        _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_KEYS,
        invalid_code="semantic_supervisor_representative_window_consume_response_invalid",
    )
    digest_fields = (
        "server_attestation_sha256",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "latency_budget_document_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "observer_runner_sha256",
        "representative_window_sha256",
        "lookup_token_sha256",
        "consume_request_sha256",
        "consume_binding_sha256",
    )
    request_bindings = (
        "server_attestation_sha256",
        "target_mode",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "latency_budget_document_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "observer_runner_sha256",
        "precursor_assist_promotion_evidence_sha256",
    )
    attestation_bindings = (
        "attestation_id",
        "target_mode",
        "observed_mode",
        "baseline_file_sha256",
        "baseline_report_sha256",
        "latency_budget_file_sha256",
        "latency_budget_document_sha256",
        "source_revision_sha256",
        "registry_binding_sha256",
        "observer_runner_sha256",
        "representative_window_sha256",
        "precursor_assist_promotion_evidence_sha256",
    )
    lookup_token = request.get("attestation_lookup_token")
    consumed_at = response.get("consumed_at")
    if (
        response.get("schema")
        != _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_CONSUME_RESPONSE_SCHEMA
        or response.get("status") != "consumed"
        or response.get("state_version") != 2
        or response.get("server_attestation") != attestation
        or any(response.get(name) != request.get(name) for name in request_bindings)
        or any(response.get(name) != attestation.get(name) for name in attestation_bindings)
        or type(lookup_token) is not str
        or response.get("lookup_token_sha256")
        != _sha256_bytes(lookup_token.encode("ascii"))
        or response.get("consume_request_sha256")
        != _semantic_supervisor_representative_window_sha256(request)
        or not _semantic_supervisor_exact_digests(response, digest_fields)
        or type(consumed_at) is not int
        or consumed_at < attestation.get("issued_at", 0)
        or consumed_at > attestation.get("expires_at", -1)
    ):
        raise ReleaseFailure(
            "semantic_supervisor_representative_window_consume_response_invalid"
        )


def _validate_semantic_supervisor_promotion_bundle(
    raw: bytes,
    *,
    mode: str,
    source_sha256: str,
    registry_sha256: str,
    latency_budget_sha256: str,
    latency_budget_ms: int,
    invalid_code: str,
    expected_precursor_assist_evidence_sha256: str | None = None,
) -> str:
    bundle = _semantic_supervisor_closed_json(raw, invalid_code=invalid_code)
    if (
        set(bundle) != _SEMANTIC_SUPERVISOR_BUNDLE_KEYS
        or bundle.get("schema") != _SEMANTIC_SUPERVISOR_PROMOTION_BUNDLE_SCHEMA
        or bundle.get("body_free") is not True
        or raw != _canonical_json(bundle) + b"\n"
    ):
        raise ReleaseFailure(invalid_code)
    (
        baseline_file_sha256,
        baseline_report_sha256,
        shadow,
        assist,
        _canary,
    ) = _semantic_supervisor_baseline_identity(
        bundle.get("baseline"),
        invalid_code=invalid_code,
    )
    attestation = _semantic_supervisor_exact_object(
        bundle.get("operator_attestation"),
        _SEMANTIC_SUPERVISOR_ATTESTATION_KEYS,
        invalid_code=invalid_code,
    )
    invariant_keys = (
        "representative_window_attested",
        "primary_fallback_proven",
        "laptop_unavailable_fallback_proven",
        "final_authority_recheck_proven",
        "primary_publication_owner_proven",
        "zero_hidden_owners_attested",
        "zero_duplicate_capabilities_attested",
        "zero_duplicate_effects_attested",
        "zero_duplicate_publications_attested",
        "zero_false_completion_regressions_attested",
    )
    precursor = attestation.get("precursor_assist_promotion_evidence_sha256")
    quality_basis = attestation.get("quality_basis")
    if (
        attestation.get("schema")
        != _SEMANTIC_SUPERVISOR_PROMOTION_ATTESTATION_SCHEMA
        or attestation.get("target_mode") != mode
        or attestation.get("baseline_file_sha256") != baseline_file_sha256
        or attestation.get("baseline_report_sha256") != baseline_report_sha256
        or attestation.get("latency_budget_file_sha256") != latency_budget_sha256
        or attestation.get("source_revision_sha256") != source_sha256
        or attestation.get("registry_binding_sha256") != registry_sha256
        or any(attestation.get(key) is not True for key in invariant_keys)
        or (
            mode == "assist"
            and (precursor is not None or quality_basis is not None)
        )
        or (
            mode == "canary"
            and (
                not _semantic_supervisor_is_digest(precursor)
                or quality_basis
                not in {
                    "completion_rate_improvement",
                    "documented_failure_class_removal",
                }
                or assist.get("promotion_evidence_count") != 1
                or assist.get("promotion_evidence_sha256") != precursor
            )
        )
    ):
        raise ReleaseFailure(invalid_code)
    representative_window_issue, representative_window_attestation = (
        _validate_semantic_supervisor_representative_window_issue(
            bundle.get("representative_window_issue"),
            baseline=_semantic_supervisor_exact_object(
                bundle.get("baseline"),
                _SEMANTIC_SUPERVISOR_BASELINE_KEYS,
                invalid_code=invalid_code,
            ),
            baseline_file_sha256=baseline_file_sha256,
            baseline_report_sha256=baseline_report_sha256,
            shadow=shadow,
            assist=assist,
            mode=mode,
            source_sha256=source_sha256,
            registry_sha256=registry_sha256,
            latency_budget_sha256=latency_budget_sha256,
            latency_budget_ms=latency_budget_ms,
            precursor_assist_evidence_sha256=precursor,
            invalid_code=invalid_code,
        )
    )
    evidence = _semantic_supervisor_exact_object(
        bundle.get("promotion_evidence"),
        _SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_KEYS,
        invalid_code=invalid_code,
    )
    evidence = _semantic_supervisor_expected_bundle_evidence(
        evidence=evidence,
        attestation=attestation,
        baseline_file_sha256=baseline_file_sha256,
        baseline_report_sha256=baseline_report_sha256,
        shadow=shadow,
        assist=assist,
        mode=mode,
        source_sha256=source_sha256,
        registry_sha256=registry_sha256,
        latency_budget_sha256=latency_budget_sha256,
        latency_budget_ms=latency_budget_ms,
        invalid_code=invalid_code,
    )
    evidence_raw = _canonical_json(evidence) + b"\n"
    evidence_canonical_sha256 = _validate_semantic_supervisor_evidence_budget_binding(
        evidence_raw,
        mode=mode,
        source_sha256=source_sha256,
        registry_sha256=registry_sha256,
        latency_budget_sha256=latency_budget_sha256,
        latency_budget_ms=latency_budget_ms,
        invalid_code=invalid_code,
        expected_precursor_assist_evidence_sha256=(
            expected_precursor_assist_evidence_sha256
        ),
    )
    receipt = _semantic_supervisor_exact_object(
        bundle.get("producer_receipt"),
        _SEMANTIC_SUPERVISOR_BUNDLE_RECEIPT_KEYS,
        invalid_code=invalid_code,
    )
    expected_receipt: dict[str, Any] = {
        "schema": _SEMANTIC_SUPERVISOR_PROMOTION_BUNDLE_RECEIPT_SCHEMA,
        "target_mode": mode,
        "source_revision_sha256": source_sha256,
        "registry_binding_sha256": registry_sha256,
        "baseline_file_sha256": baseline_file_sha256,
        "baseline_report_sha256": baseline_report_sha256,
        "latency_budget_file_sha256": latency_budget_sha256,
        "operator_attestation_sha256": _sha256_bytes(_canonical_json(attestation)),
        "representative_window_server_attestation_sha256": (
            representative_window_issue["server_attestation_sha256"]
        ),
        "representative_window_lookup_token_sha256": (
            representative_window_issue["lookup_token_sha256"]
        ),
        "representative_window_sha256": representative_window_attestation[
            "representative_window_sha256"
        ],
        "representative_window_observer_runner_sha256": (
            representative_window_attestation["observer_runner_sha256"]
        ),
        "promotion_evidence_file_sha256": _sha256_bytes(evidence_raw),
        "promotion_evidence_canonical_sha256": evidence_canonical_sha256,
        "precursor_assist_promotion_evidence_sha256": precursor,
        "body_free": True,
        "promotion_authority_granted": False,
        "activation_performed": False,
    }
    receipt_sha256 = _sha256_bytes(_canonical_json(expected_receipt))
    if (
        receipt != expected_receipt
        or bundle.get("producer_receipt_sha256") != receipt_sha256
    ):
        raise ReleaseFailure(invalid_code)
    return evidence_canonical_sha256


def _validate_semantic_supervisor_promoted_values(
    values: Mapping[str, str],
    *,
    mode: str,
    invalid_code: str,
    expected_precursor_assist_evidence_sha256: str | None = None,
) -> str:
    """Validate the dynamic evidence-bound part of assist/canary exactly."""

    if mode not in {"assist", "canary"}:  # pragma: no cover - code-owned callers
        raise ReleaseFailure("staged_config_transition_invalid")
    expected_literals = {
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_REVIEW_ROUNDS": "1",
        "FRIDAY_SEMANTIC_SUPERVISOR_MAX_STEPS": "6",
        "FRIDAY_SEMANTIC_SUPERVISOR_MODE": mode,
        "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_ENABLED": "1",
        "FRIDAY_SEMANTIC_SUPERVISOR_TASKS": _SEMANTIC_SUPERVISOR_PROMOTED_TASK,
        "FRIDAY_SEMANTIC_SUPERVISOR_TIMEOUT_SEC": "12",
    }
    if set(values) != _SEMANTIC_SUPERVISOR_ENV_KEYS or any(
        values.get(key) != value for key, value in expected_literals.items()
    ):
        raise ReleaseFailure(invalid_code)

    evidence_path_raw = values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE"]
    evidence_path = Path(evidence_path_raw)
    evidence_sha256 = values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_SHA256"]
    latency_budget_path_raw = values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE"]
    latency_budget_path = Path(latency_budget_path_raw)
    latency_budget_sha256 = values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_SHA256"]
    source_sha256 = values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256"]
    registry_sha256 = values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256"]
    if (
        not evidence_path.is_absolute()
        or str(evidence_path) != evidence_path_raw
        or Path(os.path.abspath(evidence_path)) != evidence_path
        or any(character in evidence_path_raw for character in "\x00\r\n")
        or not latency_budget_path.is_absolute()
        or str(latency_budget_path) != latency_budget_path_raw
        or Path(os.path.abspath(latency_budget_path)) != latency_budget_path
        or any(character in latency_budget_path_raw for character in "\x00\r\n")
        or _HEX64.fullmatch(evidence_sha256) is None
        or _HEX64.fullmatch(latency_budget_sha256) is None
        or _HEX64.fullmatch(source_sha256) is None
        or _HEX64.fullmatch(registry_sha256) is None
    ):
        raise ReleaseFailure(invalid_code)
    evidence = _read_private_regular_file(
        evidence_path,
        maximum_bytes=_SEMANTIC_SUPERVISOR_MAX_PROMOTION_EVIDENCE_BYTES,
        code=invalid_code,
        allowed_modes=frozenset({0o400, 0o600}),
    )
    if _sha256_bytes(evidence) != evidence_sha256:
        raise ReleaseFailure(invalid_code)
    latency_budget = _read_private_regular_file(
        latency_budget_path,
        maximum_bytes=_SEMANTIC_SUPERVISOR_MAX_LATENCY_BUDGET_BYTES,
        code=invalid_code,
        allowed_modes=frozenset({0o400, 0o600}),
    )
    _budget_source, latency_budget_ms = _semantic_supervisor_latency_budget_identity(
        latency_budget,
        expected_sha256=latency_budget_sha256,
        expected_mode=mode,
        expected_source_sha256=source_sha256,
        invalid_code=invalid_code,
    )
    evidence_canonical_sha256 = _validate_semantic_supervisor_promotion_bundle(
        evidence,
        mode=mode,
        source_sha256=source_sha256,
        registry_sha256=registry_sha256,
        latency_budget_sha256=latency_budget_sha256,
        latency_budget_ms=latency_budget_ms,
        invalid_code=invalid_code,
        expected_precursor_assist_evidence_sha256=(
            expected_precursor_assist_evidence_sha256
        ),
    )

    actor_raw = values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_CANARY_ACTOR_BINDINGS"]
    if mode == "assist":
        if actor_raw:
            raise ReleaseFailure(invalid_code)
        return evidence_canonical_sha256
    actors = tuple(actor_raw.split(",")) if actor_raw else ()
    if (
        not 1 <= len(actors) <= 32
        or any(_HEX64.fullmatch(actor) is None for actor in actors)
        or actors != tuple(sorted(set(actors)))
    ):
        raise ReleaseFailure(invalid_code)
    return evidence_canonical_sha256


def _validate_semantic_supervisor_promoted_environment(
    raw: bytes,
    *,
    mode: str,
    invalid_code: str,
) -> tuple[bytes, dict[str, str]]:
    values, unrelated, secondary_values = _canonical_semantic_supervisor_environment_parts(
        raw,
        invalid_code=invalid_code,
    )
    _validate_semantic_supervisor_promoted_values(
        values,
        mode=mode,
        invalid_code=invalid_code,
    )
    return unrelated, secondary_values


def _semantic_supervisor_validated_environment(
    raw: bytes,
    *,
    mode: str,
    invalid_code: str,
) -> tuple[bytes, dict[str, str]]:
    if mode == "off":
        return _validate_semantic_supervisor_environment(
            raw,
            exact_values=_SEMANTIC_SUPERVISOR_OFF_EXACT_VALUES,
            invalid_code=invalid_code,
        )
    if mode == "shadow":
        return _validate_semantic_supervisor_environment(
            raw,
            exact_values=_SEMANTIC_SUPERVISOR_SHADOW_EXACT_VALUES,
            invalid_code=invalid_code,
        )
    return _validate_semantic_supervisor_promoted_environment(
        raw,
        mode=mode,
        invalid_code=invalid_code,
    )


def _validate_exact_semantic_supervisor_transition(
    predecessor: bytes | None,
    target: bytes,
    *,
    predecessor_exact_values: Mapping[str, str],
    target_exact_values: Mapping[str, str],
    invalid_code: str,
    predecessor_invalid_code: str,
) -> None:
    """Change only the exact thirteen-key block over an unchanged accepted runtime."""

    target_unrelated, target_secondary = _validate_semantic_supervisor_environment(
        target,
        exact_values=target_exact_values,
        invalid_code=invalid_code,
    )
    if predecessor is None:
        return
    predecessor_unrelated, predecessor_secondary = _validate_semantic_supervisor_environment(
        predecessor,
        exact_values=predecessor_exact_values,
        invalid_code=predecessor_invalid_code,
    )
    if predecessor_unrelated != target_unrelated or predecessor_secondary != target_secondary:
        raise ReleaseFailure("semantic_supervisor_unrelated_environment_changed")


def _validate_semantic_supervisor_shadow_enable_environment(
    predecessor: bytes | None,
    target: bytes,
) -> None:
    """Enable from canonical off or the exact legacy implicit-off generation."""

    target_unrelated, target_secondary = _validate_semantic_supervisor_environment(
        target,
        exact_values=_SEMANTIC_SUPERVISOR_SHADOW_EXACT_VALUES,
        invalid_code="semantic_supervisor_shadow_enable_environment_invalid",
    )
    if predecessor is None:
        return

    secondary_values, nonsecondary, secondary = _secondary_environment_parts(predecessor)
    _validate_secondary_finalist_values(
        secondary_values,
        exact_values=_SECONDARY_DOCUMENT_MAP_ASSIST_EXACT_VALUES,
        invalid_code="semantic_supervisor_secondary_prerequisite_invalid",
    )
    canonical_secondary = _canonical_environment_values(secondary_values)
    if secondary != canonical_secondary or predecessor != nonsecondary + canonical_secondary:
        raise ReleaseFailure("semantic_supervisor_secondary_prerequisite_invalid")
    values, predecessor_unrelated, supervisor = _semantic_supervisor_environment_parts(nonsecondary)
    canonical_supervisor = _canonical_environment_values(values)
    if (
        values
        not in (
            {},
            _SEMANTIC_SUPERVISOR_LEGACY_OFF_EXACT_VALUES,
            _SEMANTIC_SUPERVISOR_PRE_LATENCY_OFF_EXACT_VALUES,
            _SEMANTIC_SUPERVISOR_OFF_EXACT_VALUES,
        )
        or supervisor != canonical_supervisor
        or nonsecondary != predecessor_unrelated + canonical_supervisor
    ):
        raise ReleaseFailure("semantic_supervisor_shadow_enable_predecessor_not_off")
    if predecessor_unrelated != target_unrelated or secondary_values != target_secondary:
        raise ReleaseFailure("semantic_supervisor_unrelated_environment_changed")


def _validate_semantic_supervisor_shadow_disable_environment(
    predecessor: bytes | None,
    target: bytes,
) -> None:
    """Return only the exact bounded P1 shadow to its canonical default-off state."""

    target_unrelated, target_secondary = _validate_semantic_supervisor_environment(
        target,
        exact_values=_SEMANTIC_SUPERVISOR_OFF_EXACT_VALUES,
        invalid_code="semantic_supervisor_shadow_disable_environment_invalid",
    )
    if predecessor is None:
        return
    values, predecessor_unrelated, predecessor_secondary = _canonical_semantic_supervisor_environment_parts(
        predecessor,
        invalid_code="semantic_supervisor_shadow_disable_predecessor_not_shadow",
    )
    if values not in (
        _SEMANTIC_SUPERVISOR_LEGACY_SHADOW_EXACT_VALUES,
        _SEMANTIC_SUPERVISOR_PRE_LATENCY_SHADOW_EXACT_VALUES,
        _SEMANTIC_SUPERVISOR_SHADOW_EXACT_VALUES,
    ):
        raise ReleaseFailure("semantic_supervisor_shadow_disable_predecessor_not_shadow")
    if predecessor_unrelated != target_unrelated or predecessor_secondary != target_secondary:
        raise ReleaseFailure("semantic_supervisor_unrelated_environment_changed")


def _validate_semantic_supervisor_mode_transition(
    predecessor: bytes | None,
    target: bytes,
    *,
    predecessor_mode: str,
    target_mode: str,
    invalid_code: str,
    predecessor_invalid_code: str,
) -> None:
    """Validate one reversible exact promoted-mode transition."""

    if predecessor_mode == "assist" and target_mode == "canary":
        if predecessor is None:
            raise ReleaseFailure(predecessor_invalid_code)
        predecessor_values, predecessor_unrelated, predecessor_secondary = (
            _canonical_semantic_supervisor_environment_parts(
                predecessor,
                invalid_code=predecessor_invalid_code,
            )
        )
        precursor_sha256 = _validate_semantic_supervisor_promoted_values(
            predecessor_values,
            mode="assist",
            invalid_code=predecessor_invalid_code,
        )
        target_values, target_unrelated, target_secondary = (
            _canonical_semantic_supervisor_environment_parts(
                target,
                invalid_code=invalid_code,
            )
        )
        _validate_semantic_supervisor_promoted_values(
            target_values,
            mode="canary",
            invalid_code=invalid_code,
            expected_precursor_assist_evidence_sha256=precursor_sha256,
        )
        for key in (
            "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256",
            "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_REGISTRY_BINDING_SHA256",
        ):
            if predecessor_values.get(key) != target_values.get(key):
                raise ReleaseFailure("semantic_supervisor_promotion_identity_drift")
        if predecessor_unrelated != target_unrelated or predecessor_secondary != target_secondary:
            raise ReleaseFailure("semantic_supervisor_unrelated_environment_changed")
        return

    target_unrelated, target_secondary = _semantic_supervisor_validated_environment(
        target,
        mode=target_mode,
        invalid_code=invalid_code,
    )
    if predecessor is None:
        return
    if predecessor_mode == "shadow" and target_mode == "assist":
        values, predecessor_unrelated, predecessor_secondary = (
            _canonical_semantic_supervisor_environment_parts(
                predecessor,
                invalid_code=predecessor_invalid_code,
            )
        )
        if values not in (
            _SEMANTIC_SUPERVISOR_LEGACY_SHADOW_EXACT_VALUES,
            _SEMANTIC_SUPERVISOR_PRE_LATENCY_SHADOW_EXACT_VALUES,
            _SEMANTIC_SUPERVISOR_SHADOW_EXACT_VALUES,
        ):
            raise ReleaseFailure(predecessor_invalid_code)
    else:
        predecessor_unrelated, predecessor_secondary = _semantic_supervisor_validated_environment(
            predecessor,
            mode=predecessor_mode,
            invalid_code=predecessor_invalid_code,
        )
    if predecessor_unrelated != target_unrelated or predecessor_secondary != target_secondary:
        raise ReleaseFailure("semantic_supervisor_unrelated_environment_changed")


def _validate_semantic_supervisor_config_transition(
    transition: str,
    predecessor: bytes | None,
    target: bytes,
) -> None:
    validators = {
        _SEMANTIC_SUPERVISOR_SHADOW_ENABLE_TRANSITION: (
            _validate_semantic_supervisor_shadow_enable_environment
        ),
        _SEMANTIC_SUPERVISOR_SHADOW_DISABLE_TRANSITION: (
            _validate_semantic_supervisor_shadow_disable_environment
        ),
    }
    validator = validators.get(transition)
    if validator is not None:
        validator(predecessor, target)
        return
    try:
        predecessor_mode, target_mode = _SEMANTIC_SUPERVISOR_TRANSITION_MODES[transition]
    except KeyError as exc:  # pragma: no cover - callers gate the closed vocabulary
        raise ReleaseFailure("staged_config_transition_invalid") from exc
    _validate_semantic_supervisor_mode_transition(
        predecessor,
        target,
        predecessor_mode=predecessor_mode,
        target_mode=target_mode,
        invalid_code=f"semantic_supervisor_{target_mode}_environment_invalid",
        predecessor_invalid_code=f"semantic_supervisor_{predecessor_mode}_environment_invalid",
    )


def _validate_exact_secondary_transition(
    predecessor: bytes | None,
    target: bytes,
    *,
    predecessor_exact_values: Mapping[str, str],
    target_exact_values: Mapping[str, str],
    invalid_code: str,
    predecessor_invalid_code: str,
    replacements: Mapping[str, tuple[str, str]],
) -> None:
    """Require canonical full environments and only the declared value edits."""

    target_values, target_unrelated = _secondary_environment_view(target)
    _validate_secondary_finalist_values(
        target_values,
        exact_values=target_exact_values,
        invalid_code=invalid_code,
    )
    if target != _canonical_secondary_environment(target_unrelated, target_values):
        raise ReleaseFailure(invalid_code)
    if predecessor is None:
        return
    predecessor_values, predecessor_unrelated = _secondary_environment_view(predecessor)
    _validate_secondary_finalist_values(
        predecessor_values,
        exact_values=predecessor_exact_values,
        invalid_code=predecessor_invalid_code,
    )
    if predecessor_unrelated != target_unrelated:
        raise ReleaseFailure("secondary_shadow_unrelated_environment_changed")
    if predecessor != _canonical_secondary_environment(predecessor_unrelated, predecessor_values):
        raise ReleaseFailure(predecessor_invalid_code)
    expected = predecessor
    for key, (source_value, target_value) in sorted(replacements.items()):
        source = f"{key}={source_value}\n".encode("ascii")
        replacement = f"{key}={target_value}\n".encode("ascii")
        if expected.count(source) != 1:
            raise ReleaseFailure(predecessor_invalid_code)
        expected = expected.replace(source, replacement, 1)
    if expected != target:
        raise ReleaseFailure(invalid_code)


def _validate_secondary_shadow_environment(
    predecessor: bytes | None,
    target: bytes,
) -> None:
    """Admit only the exact code-owned finalist shadow profile and its private CA."""

    target_values, target_unrelated = _secondary_environment_view(target)
    _validate_secondary_finalist_values(
        target_values,
        exact_values=_SECONDARY_SHADOW_EXACT_VALUES,
        invalid_code="secondary_shadow_environment_invalid",
    )
    if target != _canonical_secondary_environment(target_unrelated, target_values):
        raise ReleaseFailure("secondary_shadow_environment_invalid")
    if predecessor is None:
        return
    predecessor_values, predecessor_unrelated = _secondary_environment_view(predecessor)
    if predecessor_unrelated != target_unrelated:
        raise ReleaseFailure("secondary_shadow_unrelated_environment_changed")
    if predecessor_values.get("FRIDAY_SECONDARY_LLM_ENABLED", "0") != "0" or (
        predecessor_values.get("FRIDAY_SECONDARY_LLM_MODE", "disabled") not in {"disabled", "shadow"}
    ):
        raise ReleaseFailure("secondary_shadow_predecessor_not_disabled")


def _validate_secondary_shadow_disable_environment(
    predecessor: bytes | None,
    target: bytes,
) -> None:
    """Disable exact public/private shadow while preserving its privacy bit."""

    target_values, _target_unrelated = _secondary_environment_view(target)
    allow_private = target_values.get("FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT")
    if allow_private == "0":
        predecessor_values = _SECONDARY_SHADOW_EXACT_VALUES
        target_values = _SECONDARY_SHADOW_DISABLED_EXACT_VALUES
    elif allow_private == "1":
        predecessor_values = _SECONDARY_PRIVATE_SHADOW_EXACT_VALUES
        target_values = _SECONDARY_PRIVATE_SHADOW_DISABLED_EXACT_VALUES
    else:
        raise ReleaseFailure("secondary_shadow_disable_environment_invalid")

    _validate_exact_secondary_transition(
        predecessor,
        target,
        predecessor_exact_values=predecessor_values,
        target_exact_values=target_values,
        invalid_code="secondary_shadow_disable_environment_invalid",
        predecessor_invalid_code="secondary_shadow_disable_predecessor_not_enabled",
        replacements={"FRIDAY_SECONDARY_LLM_ENABLED": ("1", "0")},
    )


def _validate_secondary_shadow_to_private_shadow_environment(
    predecessor: bytes | None,
    target: bytes,
) -> None:
    """Admit private text in shadow while retaining discarded shadow output."""

    _validate_exact_secondary_transition(
        predecessor,
        target,
        predecessor_exact_values=_SECONDARY_SHADOW_EXACT_VALUES,
        target_exact_values=_SECONDARY_PRIVATE_SHADOW_EXACT_VALUES,
        invalid_code="secondary_shadow_to_private_shadow_environment_invalid",
        predecessor_invalid_code="secondary_shadow_to_private_shadow_predecessor_not_public_shadow",
        replacements={"FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT": ("0", "1")},
    )


def _validate_secondary_shadow_to_assist_environment(
    predecessor: bytes | None,
    target: bytes,
) -> None:
    """Promote exact private shadow to assist by changing only its mode."""

    _validate_exact_secondary_transition(
        predecessor,
        target,
        predecessor_exact_values=_SECONDARY_PRIVATE_SHADOW_EXACT_VALUES,
        target_exact_values=_SECONDARY_ASSIST_EXACT_VALUES,
        invalid_code="secondary_shadow_to_assist_environment_invalid",
        predecessor_invalid_code="secondary_shadow_to_assist_predecessor_not_private_shadow",
        replacements={"FRIDAY_SECONDARY_LLM_MODE": ("shadow", "assist")},
    )


def _validate_secondary_assist_enable_document_map_shadow_environment(
    predecessor: bytes | None,
    target: bytes,
) -> None:
    """Add one workload in discarded shadow without changing live extract assist."""

    target_values, target_unrelated = _secondary_environment_view(target)
    _validate_secondary_finalist_values(
        target_values,
        exact_values=_SECONDARY_DOCUMENT_MAP_SHADOW_EXACT_VALUES,
        invalid_code="secondary_document_map_shadow_environment_invalid",
    )
    if target != _canonical_secondary_environment(target_unrelated, target_values):
        raise ReleaseFailure("secondary_document_map_shadow_environment_invalid")
    if predecessor is None:
        return
    predecessor_values, predecessor_unrelated = _secondary_environment_view(predecessor)
    _validate_secondary_finalist_values(
        predecessor_values,
        exact_values=_SECONDARY_ASSIST_EXACT_VALUES,
        invalid_code="secondary_document_map_shadow_predecessor_not_assist",
    )
    if predecessor_unrelated != target_unrelated:
        raise ReleaseFailure("secondary_shadow_unrelated_environment_changed")
    if predecessor != _canonical_secondary_environment(predecessor_unrelated, predecessor_values):
        raise ReleaseFailure("secondary_document_map_shadow_predecessor_not_assist")
    expected_values = {
        **predecessor_values,
        "FRIDAY_SECONDARY_LLM_WORKLOADS": "document_map,extract",
        "FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE": "shadow",
    }
    if expected_values != target_values:
        raise ReleaseFailure("secondary_document_map_shadow_environment_invalid")


def _validate_secondary_document_map_shadow_to_assist_environment(
    predecessor: bytes | None,
    target: bytes,
) -> None:
    """Change only document-map routing after its exact receipt is accepted."""

    _validate_exact_secondary_transition(
        predecessor,
        target,
        predecessor_exact_values=_SECONDARY_DOCUMENT_MAP_SHADOW_EXACT_VALUES,
        target_exact_values=_SECONDARY_DOCUMENT_MAP_ASSIST_EXACT_VALUES,
        invalid_code="secondary_document_map_assist_environment_invalid",
        predecessor_invalid_code="secondary_document_map_assist_predecessor_not_shadow",
        replacements={"FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE": ("shadow", "assist")},
    )


def _validate_secondary_assist_to_disabled_environment(
    predecessor: bytes | None,
    target: bytes,
) -> None:
    """Disable exact assist by changing its one code-owned admission bit."""

    target_values, _target_unrelated = _secondary_environment_view(target)
    document_map_mode = target_values.get("FRIDAY_SECONDARY_LLM_DOCUMENT_MAP_MODE")
    workloads = target_values.get("FRIDAY_SECONDARY_LLM_WORKLOADS")
    if document_map_mode == "shadow" and workloads == "document_map,extract":
        predecessor_values = _SECONDARY_DOCUMENT_MAP_SHADOW_EXACT_VALUES
        disabled_values = _SECONDARY_DOCUMENT_MAP_SHADOW_DISABLED_EXACT_VALUES
    elif document_map_mode == "assist" and workloads == "document_map,extract":
        predecessor_values = _SECONDARY_DOCUMENT_MAP_ASSIST_EXACT_VALUES
        disabled_values = _SECONDARY_DOCUMENT_MAP_ASSIST_DISABLED_EXACT_VALUES
    elif document_map_mode is None and workloads == "extract":
        predecessor_values = _SECONDARY_ASSIST_EXACT_VALUES
        disabled_values = _SECONDARY_ASSIST_DISABLED_EXACT_VALUES
    else:
        raise ReleaseFailure("secondary_assist_to_disabled_environment_invalid")

    _validate_exact_secondary_transition(
        predecessor,
        target,
        predecessor_exact_values=predecessor_values,
        target_exact_values=disabled_values,
        invalid_code="secondary_assist_to_disabled_environment_invalid",
        predecessor_invalid_code="secondary_assist_to_disabled_predecessor_not_assist",
        replacements={"FRIDAY_SECONDARY_LLM_ENABLED": ("1", "0")},
    )


def _validate_secondary_config_transition(
    transition: str,
    predecessor: bytes | None,
    target: bytes,
) -> None:
    validators = {
        _SECONDARY_SHADOW_ENABLE_TRANSITION: _validate_secondary_shadow_environment,
        _SECONDARY_SHADOW_DISABLE_TRANSITION: _validate_secondary_shadow_disable_environment,
        _SECONDARY_SHADOW_TO_PRIVATE_SHADOW_TRANSITION: (
            _validate_secondary_shadow_to_private_shadow_environment
        ),
        _SECONDARY_SHADOW_TO_ASSIST_TRANSITION: _validate_secondary_shadow_to_assist_environment,
        _SECONDARY_ASSIST_TO_DISABLED_TRANSITION: _validate_secondary_assist_to_disabled_environment,
        _SECONDARY_ASSIST_ENABLE_DOCUMENT_MAP_SHADOW_TRANSITION: (
            _validate_secondary_assist_enable_document_map_shadow_environment
        ),
        _SECONDARY_DOCUMENT_MAP_SHADOW_TO_ASSIST_TRANSITION: (
            _validate_secondary_document_map_shadow_to_assist_environment
        ),
    }
    try:
        validator = validators[transition]
    except KeyError as exc:  # pragma: no cover - callers gate the closed vocabulary
        raise ReleaseFailure("staged_config_transition_invalid") from exc
    validator(predecessor, target)


def _secondary_product_canonical(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ReleaseFailure("secondary_rollout_receipt_invalid") from exc
    return (encoded + "\n").encode("utf-8")


def _secondary_product_json(raw: bytes) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite number")

    try:
        parsed = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseFailure("secondary_rollout_receipt_invalid") from exc
    if not isinstance(parsed, dict) or raw != _secondary_product_canonical(parsed):
        raise ReleaseFailure("secondary_rollout_receipt_invalid")
    return parsed


def _secondary_product_counter(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= _SECONDARY_PRODUCT_MAX_COUNTER:
        raise ReleaseFailure("secondary_rollout_receipt_invalid")
    return value


def _secondary_product_reasons(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > len(_SECONDARY_PRODUCT_FAILURES):
        raise ReleaseFailure("secondary_rollout_receipt_invalid")
    result: dict[str, int] = {}
    for reason, count in value.items():
        if reason not in _SECONDARY_PRODUCT_FAILURES:
            raise ReleaseFailure("secondary_rollout_receipt_invalid")
        normalized = _secondary_product_counter(count)
        if normalized:
            result[str(reason)] = normalized
    canonical = dict(sorted(result.items()))
    if value != canonical:
        raise ReleaseFailure("secondary_rollout_receipt_invalid")
    return canonical


def _validate_secondary_product_snapshot(
    value: Any,
    *,
    stage: str,
    profile_id: str,
    profile_admission: str,
    after: bool,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseFailure("secondary_rollout_receipt_invalid")
    workload = value.get("workload")
    shadow = value.get("shadow")
    last_failure = value.get("last_failure")
    retry_after = value.get("circuit_retry_after_sec")
    available = value.get("available")
    if (
        set(value) != _SECONDARY_PRODUCT_SNAPSHOT_KEYS
        or value.get("schema") != "friday.optional-secondary-health.v1"
        or value.get("role") != "optional_advisory"
        or value.get("enabled") is not True
        or value.get("configured") is not True
        or value.get("mode") != "shadow"
        or value.get("state") != "healthy"
        or type(available) is not bool
        or ((stage != "private-shadow" or after) and available is not True)
        or (last_failure is not None and last_failure not in _SECONDARY_PRODUCT_FAILURES)
        or value.get("profile_id") != profile_id
        or value.get("profile_admission") != profile_admission
        or (stage == "private-shadow" and profile_admission != "accepted")
        or (stage == "public-shadow" and profile_admission not in {"provisional_shadow", "accepted"})
        or value.get("profile_manifest_match") is not True
        or value.get("served_model_match") is not True
        or value.get("context_cap_tokens") != 4096
        or not isinstance(workload, dict)
        or set(workload) != _SECONDARY_PRODUCT_WORKLOAD_KEYS
        or workload.get("name") != "extract"
        or not isinstance(shadow, dict)
        or set(shadow) != _SECONDARY_PRODUCT_SHADOW_KEYS
        or shadow.get("in_flight") != 0
        or isinstance(retry_after, bool)
        or not isinstance(retry_after, (int, float))
        or not math.isfinite(float(retry_after))
        or not 0.0 <= float(retry_after) <= 86_400.0
        or float(retry_after) != round(float(retry_after), 3)
    ):
        raise ReleaseFailure("secondary_rollout_receipt_invalid")
    for key in (
        "context_cap_tokens",
        "selected_total",
        "success_total",
        "endpoint_request_total",
        "endpoint_success_total",
        "skipped_total",
        "primary_fallback_total",
        "probe_success_total",
        "probe_failure_total",
        "model_inventory_probe_success_total",
        "model_inventory_probe_failure_total",
    ):
        _secondary_product_counter(value.get(key))
    assert isinstance(workload, dict) and isinstance(shadow, dict)
    _secondary_product_counter(workload.get("selected_total"))
    _secondary_product_counter(workload.get("success_total"))
    for key in _SECONDARY_PRODUCT_SHADOW_KEYS:
        _secondary_product_counter(shadow.get(key))
    _secondary_product_reasons(value.get("skip_reasons"))
    _secondary_product_reasons(value.get("fallback_reasons"))
    _secondary_product_reasons(workload.get("skip_reasons"))
    _secondary_product_reasons(workload.get("fallback_reasons"))
    return value


def _secondary_product_delta(after: Any, before: Any) -> int:
    after_counter = _secondary_product_counter(after)
    before_counter = _secondary_product_counter(before)
    if after_counter < before_counter:
        raise ReleaseFailure("secondary_rollout_receipt_invalid")
    return after_counter - before_counter


def _secondary_product_reason_deltas(after: Any, before: Any) -> dict[str, int]:
    after_reasons = _secondary_product_reasons(after)
    before_reasons = _secondary_product_reasons(before)
    result: dict[str, int] = {}
    for reason in sorted(set(after_reasons) | set(before_reasons)):
        delta = _secondary_product_delta(after_reasons.get(reason, 0), before_reasons.get(reason, 0))
        if delta:
            result[reason] = delta
    return result


def _secondary_product_stage_deltas(
    stage: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    before_workload = before["workload"]
    after_workload = after["workload"]
    before_shadow = before["shadow"]
    after_shadow = after["shadow"]
    if not all(
        isinstance(value, Mapping) for value in (before_workload, after_workload, before_shadow, after_shadow)
    ):
        raise ReleaseFailure("secondary_rollout_receipt_invalid")
    deltas: dict[str, Any] = {
        "selected_total": _secondary_product_delta(
            after_workload["selected_total"], before_workload["selected_total"]
        ),
        "success_total": _secondary_product_delta(
            after_workload["success_total"], before_workload["success_total"]
        ),
        "endpoint_request_total": _secondary_product_delta(
            after["endpoint_request_total"], before["endpoint_request_total"]
        ),
        "endpoint_success_total": _secondary_product_delta(
            after["endpoint_success_total"], before["endpoint_success_total"]
        ),
        "skipped_total": _secondary_product_delta(after["skipped_total"], before["skipped_total"]),
        "primary_fallback_total": _secondary_product_delta(
            after["primary_fallback_total"], before["primary_fallback_total"]
        ),
        "probe_success_total": _secondary_product_delta(
            after["probe_success_total"], before["probe_success_total"]
        ),
        "probe_failure_total": _secondary_product_delta(
            after["probe_failure_total"], before["probe_failure_total"]
        ),
        "model_inventory_probe_success_total": _secondary_product_delta(
            after["model_inventory_probe_success_total"],
            before["model_inventory_probe_success_total"],
        ),
        "model_inventory_probe_failure_total": _secondary_product_delta(
            after["model_inventory_probe_failure_total"],
            before["model_inventory_probe_failure_total"],
        ),
        "skip_reason_deltas": _secondary_product_reason_deltas(after["skip_reasons"], before["skip_reasons"]),
        "fallback_reason_deltas": _secondary_product_reason_deltas(
            after["fallback_reasons"], before["fallback_reasons"]
        ),
        "workload_skip_reason_deltas": _secondary_product_reason_deltas(
            after_workload["skip_reasons"], before_workload["skip_reasons"]
        ),
        "workload_fallback_reason_deltas": _secondary_product_reason_deltas(
            after_workload["fallback_reasons"], before_workload["fallback_reasons"]
        ),
        "shadow_valid_total": _secondary_product_delta(
            after_shadow["valid_total"], before_shadow["valid_total"]
        ),
        "shadow_invalid_total": _secondary_product_delta(
            after_shadow["invalid_total"], before_shadow["invalid_total"]
        ),
        "shadow_skipped_total": _secondary_product_delta(
            after_shadow["skipped_total"], before_shadow["skipped_total"]
        ),
    }
    if set(deltas) != _SECONDARY_PRODUCT_DELTA_KEYS or (
        _secondary_product_delta(after["selected_total"], before["selected_total"])
        != deltas["selected_total"]
        or _secondary_product_delta(after["success_total"], before["success_total"])
        != deltas["success_total"]
    ):
        raise ReleaseFailure("secondary_rollout_receipt_invalid")
    reasons_clear = not any(
        deltas[key]
        for key in (
            "skip_reason_deltas",
            "fallback_reason_deltas",
            "workload_skip_reason_deltas",
            "workload_fallback_reason_deltas",
        )
    )
    if stage == "public-shadow":
        valid = (
            deltas["selected_total"] == 0
            and deltas["success_total"] == 0
            and deltas["endpoint_request_total"] == 0
            and deltas["endpoint_success_total"] == 0
            and deltas["skipped_total"] == 1
            and deltas["primary_fallback_total"] == 0
            and deltas["skip_reason_deltas"] == {"private_text_disallowed": 1}
            and not deltas["fallback_reason_deltas"]
            and deltas["workload_skip_reason_deltas"] == {"private_text_disallowed": 1}
            and not deltas["workload_fallback_reason_deltas"]
            and deltas["shadow_valid_total"] == 0
            and deltas["shadow_invalid_total"] == 0
            and deltas["shadow_skipped_total"] == 1
            and deltas["probe_success_total"] == 0
            and deltas["probe_failure_total"] == 0
            and deltas["model_inventory_probe_success_total"] == 0
            and deltas["model_inventory_probe_failure_total"] == 0
        )
    elif stage == "private-shadow":
        endpoint_delta = deltas["endpoint_request_total"]
        valid = (
            deltas["selected_total"] == 1
            and deltas["success_total"] == 1
            and endpoint_delta in {1, 3}
            and deltas["endpoint_success_total"] == endpoint_delta
            and deltas["skipped_total"] == 0
            and deltas["primary_fallback_total"] == 0
            and reasons_clear
            and deltas["shadow_valid_total"] == 1
            and deltas["shadow_invalid_total"] == 0
            and deltas["shadow_skipped_total"] == 0
            and deltas["probe_failure_total"] == 0
            and deltas["model_inventory_probe_failure_total"] == 0
            and deltas["probe_success_total"] in {0, 1}
            and deltas["model_inventory_probe_success_total"] == deltas["probe_success_total"]
        )
    else:  # pragma: no cover - the closed transition map proves the stage
        valid = False
    if not valid:
        raise ReleaseFailure("secondary_rollout_receipt_oracle_mismatch")
    return deltas


def _validate_secondary_rollout_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_stage: str,
    previous: ReleaseIdentity,
    observer_runner_sha256: str,
    profile_identity: Mapping[str, Any],
    primary_pid: int,
    primary_process_epoch_sha256: str,
    primary_ca_certificate_sha256: str,
) -> dict[str, Any]:
    before = receipt.get("diagnostics_before")
    after = receipt.get("diagnostics_after")
    supplied_deltas = receipt.get("diagnostics_deltas")
    operation = receipt.get("operation")
    expected_profile_keys = {
        "admission",
        "allow_private_text",
        "context_tokens",
        "gateway_ca_certificate_sha256",
        "manifest_sha256",
        "mode",
        "profile_id",
        "served_model_alias",
    }
    if (
        expected_stage not in {"public-shadow", "private-shadow"}
        or set(receipt) != _SECONDARY_PRODUCT_RECEIPT_KEYS
        or receipt.get("schema") != _SECONDARY_PRODUCT_STAGE_SCHEMA
        or receipt.get("status") != "passed"
        or receipt.get("stage") != expected_stage
        or set(profile_identity) != expected_profile_keys
        or profile_identity.get("mode") != "shadow"
        or type(profile_identity.get("allow_private_text")) is not bool
        or profile_identity.get("allow_private_text") is not (expected_stage == "private-shadow")
        or type(profile_identity.get("context_tokens")) is not int
        or profile_identity.get("context_tokens") != 4096
        or profile_identity.get("profile_id") != _SECONDARY_FINALIST_PROFILE_ID
        or profile_identity.get("served_model_alias") != _SECONDARY_FINALIST_MODEL_ALIAS
        or profile_identity.get("gateway_ca_certificate_sha256") != _SECONDARY_FINALIST_CA_SHA256
        or (expected_stage == "private-shadow" and profile_identity.get("admission") != "accepted")
        or (
            expected_stage == "public-shadow"
            and profile_identity.get("admission") not in {"provisional_shadow", "accepted"}
        )
        or _HEX64.fullmatch(str(profile_identity.get("manifest_sha256") or "")) is None
        or receipt.get("candidate_profile_id") != profile_identity.get("profile_id")
        or receipt.get("candidate_profile_sha256") != _SECONDARY_FINALIST_CANDIDATE_PROFILE_SHA256
        or (
            profile_identity.get("admission") == "provisional_shadow"
            and profile_identity.get("manifest_sha256") != _SECONDARY_FINALIST_CANDIDATE_PROFILE_SHA256
        )
        or receipt.get("served_model_alias") != profile_identity.get("served_model_alias")
        or receipt.get("gateway_ca_certificate_sha256")
        != profile_identity.get("gateway_ca_certificate_sha256")
        or receipt.get("observer_source_head") != previous.commit
        or receipt.get("observer_runner_sha256") != observer_runner_sha256
        or type(receipt.get("primary_pid")) is not int
        or receipt.get("primary_pid") != primary_pid
        or receipt.get("primary_process_epoch_sha256") != primary_process_epoch_sha256
        or receipt.get("primary_version") != previous.version
        or receipt.get("primary_ca_certificate_sha256") != primary_ca_certificate_sha256
        or not isinstance(before, dict)
        or not isinstance(after, dict)
        or not isinstance(supplied_deltas, dict)
        or not isinstance(operation, dict)
        or set(operation) != _SECONDARY_PRODUCT_OPERATION_KEYS
        or receipt.get("raw_content_retained_in_evidence") is not False
        or receipt.get("model_response_retained_in_evidence") is not False
        or receipt.get("credentials_retained") is not False
        or receipt.get("rollout_lookup_token_retained") is not True
    ):
        raise ReleaseFailure("secondary_rollout_receipt_identity_mismatch")
    before = _validate_secondary_product_snapshot(
        before,
        stage=expected_stage,
        profile_id=str(profile_identity["profile_id"]),
        profile_admission=str(profile_identity["admission"]),
        after=False,
    )
    after = _validate_secondary_product_snapshot(
        after,
        stage=expected_stage,
        profile_id=str(profile_identity["profile_id"]),
        profile_admission=str(profile_identity["admission"]),
        after=True,
    )
    computed_deltas = _secondary_product_stage_deltas(expected_stage, before, after)
    stage_projection = {
        "source_ref_sha256": operation["source_ref_sha256"],
        "before": before,
        "after": after,
        "deltas": supplied_deltas,
    }
    stage_binding_sha256 = _sha256_bytes(_secondary_product_canonical(stage_projection))
    diagnostics_projection = {
        "schema": _SECONDARY_PRODUCT_DIAGNOSTICS_SCHEMA,
        "source_ref_sha256": operation["source_ref_sha256"],
        "before": before,
        "after": after,
    }
    diagnostics_binding_sha256 = _sha256_bytes(_secondary_product_canonical(diagnostics_projection))
    diagnostics_receipt = {
        **diagnostics_projection,
        "binding_sha256": diagnostics_binding_sha256,
    }
    if (
        supplied_deltas != computed_deltas
        or receipt.get("stage_diagnostics_binding_sha256") != stage_binding_sha256
        or operation.get("stage_diagnostics_binding_sha256") != stage_binding_sha256
        or receipt.get("diagnostics_binding_sha256") != diagnostics_binding_sha256
        or operation.get("advice_diagnostics_receipt_sha256")
        != _sha256_bytes(_secondary_product_canonical(diagnostics_receipt))
    ):
        raise ReleaseFailure("secondary_rollout_receipt_diagnostics_mismatch")
    for key, value in operation.items():
        if key.endswith("_sha256") and (
            not isinstance(value, str) or _HEX64.fullmatch(value) is None or set(value) == {"0"}
        ):
            raise ReleaseFailure("secondary_rollout_receipt_operation_invalid")
    if (
        operation.get("schema") != _SECONDARY_PRODUCT_OPERATION_SCHEMA
        or type(operation.get("ingest_idempotent_replay")) is not bool
        or operation.get("advice_endpoint_role") != "primary"
        or operation.get("exact_secondary_model_observed") is not False
        or operation.get("cleanup_status") != "purged"
        or operation.get("knowledge_object_created") is not False
        or operation.get("tool_requested") is not False
        or operation.get("effect_requested") is not False
        or receipt.get("operation_binding_sha256") != _sha256_bytes(_secondary_product_canonical(operation))
    ):
        raise ReleaseFailure("secondary_rollout_receipt_operation_invalid")
    return _validate_secondary_rollout_attestation(
        receipt,
        operation=operation,
        expected_stage=expected_stage,
        previous=previous,
        observer_runner_sha256=observer_runner_sha256,
        profile_identity=profile_identity,
        primary_pid=primary_pid,
        primary_process_epoch_sha256=primary_process_epoch_sha256,
        primary_ca_certificate_sha256=primary_ca_certificate_sha256,
    )


def _validate_secondary_rollout_attestation(
    receipt: Mapping[str, Any],
    *,
    operation: Mapping[str, Any],
    expected_stage: str,
    previous: ReleaseIdentity,
    observer_runner_sha256: str,
    profile_identity: Mapping[str, Any],
    primary_pid: int,
    primary_process_epoch_sha256: str,
    primary_ca_certificate_sha256: str,
) -> dict[str, Any]:
    value = receipt.get("server_rollout_attestation")
    lookup_token = receipt.get("server_rollout_lookup_token")
    if not isinstance(value, dict) or set(value) != _SECONDARY_PRODUCT_ATTESTATION_KEYS:
        raise ReleaseFailure("secondary_rollout_attestation_invalid")
    attestation = dict(value)
    for key, item in attestation.items():
        if (key.endswith("_sha256") or key == "signature") and (
            not isinstance(item, str) or _HEX64.fullmatch(item) is None or set(item) == {"0"}
        ):
            raise ReleaseFailure("secondary_rollout_attestation_invalid")
    attestation_id = attestation.get("attestation_id")
    issued_at = attestation.get("issued_at")
    expires_at = attestation.get("expires_at")
    current_time = int(time.time())
    if (
        attestation.get("schema") != _SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_SCHEMA
        or not isinstance(attestation_id, str)
        or re.fullmatch(r"[0-9a-f]{32}", attestation_id) is None
        or set(attestation_id) == {"0"}
        or type(issued_at) is not int
        or type(expires_at) is not int
        or not 0 < expires_at - issued_at <= 570
        or current_time < issued_at - 30
        or current_time > expires_at
        or type(attestation.get("state_version")) is not int
        or attestation.get("state_version") != 1
        or issued_at < 1
        or any(type(attestation.get(key)) is not int for key in _SECONDARY_PRODUCT_RESIDUE_KEYS)
        or any(attestation.get(key) != 0 for key in _SECONDARY_PRODUCT_RESIDUE_KEYS)
        or not isinstance(lookup_token, str)
        or _HEX64.fullmatch(lookup_token) is None
        or set(lookup_token) == {"0"}
        or attestation.get("lookup_token_sha256") != _sha256_bytes(lookup_token.encode("ascii"))
        or receipt.get("server_rollout_attestation_sha256")
        != _sha256_bytes(_secondary_product_canonical(attestation))
        or attestation.get("stage") != expected_stage
        or attestation.get("source_ref_sha256") != operation.get("source_ref_sha256")
        or attestation.get("raw_object_id_sha256") != operation.get("raw_object_id_sha256")
        or attestation.get("inbox_id_sha256") != operation.get("inbox_id_sha256")
        or attestation.get("content_sha256") != operation.get("synthetic_content_sha256")
        or attestation.get("uploader_sha256") != operation.get("uploader_id_sha256")
        or attestation.get("ingest_storage_binding_sha256") != operation.get("ingest_storage_sha256")
        or attestation.get("advice_storage_binding_sha256") != operation.get("advice_storage_sha256")
        or attestation.get("advice_diagnostics_receipt_sha256")
        != operation.get("advice_diagnostics_receipt_sha256")
        or attestation.get("diagnostics_binding_sha256") != receipt.get("diagnostics_binding_sha256")
        or attestation.get("stage_diagnostics_binding_sha256")
        != receipt.get("stage_diagnostics_binding_sha256")
        or attestation.get("stage_diagnostics_binding_sha256")
        != operation.get("stage_diagnostics_binding_sha256")
        or attestation.get("operation_binding_sha256") != receipt.get("operation_binding_sha256")
        or attestation.get("advice_proof_sha256") != operation.get("advice_proof_sha256")
        or attestation.get("advice_endpoint_role") != operation.get("advice_endpoint_role")
        or type(attestation.get("primary_pid")) is not int
        or attestation.get("primary_pid") != primary_pid
        or attestation.get("primary_process_epoch_sha256") != primary_process_epoch_sha256
        or attestation.get("primary_backend_version") != previous.version
        or attestation.get("primary_ca_certificate_sha256") != primary_ca_certificate_sha256
        or attestation.get("observer_source_head") != previous.commit
        or attestation.get("observer_runner_sha256") != observer_runner_sha256
        or attestation.get("candidate_profile_id") != profile_identity.get("profile_id")
        or attestation.get("candidate_profile_mode") != profile_identity.get("mode")
        or attestation.get("candidate_profile_allow_private_text")
        is not profile_identity.get("allow_private_text")
        or type(attestation.get("candidate_profile_context_tokens")) is not int
        or attestation.get("candidate_profile_context_tokens") != profile_identity.get("context_tokens")
        or attestation.get("candidate_profile_sha256") != receipt.get("candidate_profile_sha256")
        or attestation.get("candidate_profile_manifest_sha256") != profile_identity.get("manifest_sha256")
        or attestation.get("candidate_profile_admission") != profile_identity.get("admission")
        or attestation.get("served_model_alias") != profile_identity.get("served_model_alias")
        or attestation.get("gateway_ca_certificate_sha256")
        != profile_identity.get("gateway_ca_certificate_sha256")
    ):
        raise ReleaseFailure("secondary_rollout_attestation_invalid")
    zero_projection = {
        "schema": _SECONDARY_PRODUCT_CLEANUP_ZERO_SCHEMA,
        "raw_object_id_sha256": attestation["raw_object_id_sha256"],
        "inbox_id_sha256": attestation["inbox_id_sha256"],
        **{key: attestation[key] for key in _SECONDARY_PRODUCT_RESIDUE_KEYS},
    }
    if set(zero_projection) != _SECONDARY_PRODUCT_CLEANUP_ZERO_KEYS or attestation.get(
        "cleanup_zero_residue_binding_sha256"
    ) != _sha256_bytes(_secondary_product_canonical(zero_projection)):
        raise ReleaseFailure("secondary_rollout_attestation_cleanup_invalid")
    cleanup_core = {
        "schema": _SECONDARY_PRODUCT_CLEANUP_CORE_SCHEMA,
        "purged": True,
        "raw_deleted": 1,
        "inbox_deleted": 1,
        "storage_binding_sha256": attestation["cleanup_storage_binding_sha256"],
        "raw_object_id_sha256": attestation["raw_object_id_sha256"],
        "inbox_id_sha256": attestation["inbox_id_sha256"],
        "cleanup_zero_residue_binding_sha256": attestation["cleanup_zero_residue_binding_sha256"],
        **{key: attestation[key] for key in _SECONDARY_PRODUCT_RESIDUE_KEYS},
    }
    if set(cleanup_core) != _SECONDARY_PRODUCT_CLEANUP_CORE_KEYS or operation.get(
        "cleanup_core_sha256"
    ) != _sha256_bytes(_secondary_product_canonical(cleanup_core)):
        raise ReleaseFailure("secondary_rollout_attestation_cleanup_invalid")
    return attestation


def _secondary_product_runner_artifact_sha256(previous: ReleaseIdentity) -> str:
    metadata_sha256 = _closed_hash(
        previous.secondary_product_runner_sha256,
        "secondary_product_runner_capability_missing",
    )
    artifact = Path(os.path.abspath(previous.root / _SECONDARY_PRODUCT_RUNNER_ARTIFACT))
    expected = Path(os.path.abspath(previous.root)) / _SECONDARY_PRODUCT_RUNNER_ARTIFACT
    if artifact != expected:
        raise ReleaseFailure("secondary_product_runner_artifact_invalid")
    raw = _read_stable_regular_file(
        artifact,
        maximum_bytes=4 << 20,
        code="secondary_product_runner_artifact_invalid",
    )
    status = os.stat(artifact, follow_symlinks=False)
    if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != 0o400:
        raise ReleaseFailure("secondary_product_runner_artifact_invalid")
    actual_sha256 = _sha256_bytes(raw)
    if actual_sha256 != metadata_sha256:
        raise ReleaseFailure("secondary_product_runner_artifact_digest_mismatch")
    return actual_sha256


def _secondary_rollout_consume_request(
    *,
    lookup_token: str,
    stage: str,
    transition: str,
    previous: ReleaseIdentity,
    candidate: ReleaseIdentity,
    next_env_sha256: str,
    product_receipt_sha256: str,
    sealed_runner_sha256: str,
    server_rollout_attestation_sha256: str,
) -> dict[str, Any]:
    request = {
        "schema": _SECONDARY_PRODUCT_CONSUME_REQUEST_SCHEMA,
        "attestation_lookup_token": lookup_token,
        "server_rollout_attestation_sha256": server_rollout_attestation_sha256,
        "stage": stage,
        "transition": transition,
        "predecessor_commit": previous.commit,
        "predecessor_tree_sha256": previous.tree_manifest_sha256,
        "candidate_commit": candidate.commit,
        "candidate_tree_sha256": candidate.tree_manifest_sha256,
        "next_env_sha256": next_env_sha256,
        "product_receipt_sha256": product_receipt_sha256,
        "sealed_runner_sha256": sealed_runner_sha256,
    }
    expected_transition = {
        "public-shadow": _SECONDARY_SHADOW_TO_PRIVATE_SHADOW_TRANSITION,
        "private-shadow": _SECONDARY_SHADOW_TO_ASSIST_TRANSITION,
    }.get(stage)
    if (
        set(request) != _SECONDARY_PRODUCT_CONSUME_REQUEST_KEYS
        or transition != expected_transition
        or candidate.commit == previous.commit
        or _HEX64.fullmatch(lookup_token) is None
        or set(lookup_token) == {"0"}
    ):
        raise ReleaseFailure("secondary_rollout_consume_request_invalid")
    for key in (
        "predecessor_tree_sha256",
        "candidate_tree_sha256",
        "next_env_sha256",
        "product_receipt_sha256",
        "sealed_runner_sha256",
        "server_rollout_attestation_sha256",
    ):
        _closed_hash(str(request[key]), "secondary_rollout_consume_request_invalid")
    _closed_commit(str(request["predecessor_commit"]))
    _closed_commit(str(request["candidate_commit"]))
    return request


def _validate_secondary_rollout_consume_response(
    value: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    attestation: Mapping[str, Any],
) -> None:
    request_sha256 = _sha256_bytes(_secondary_product_canonical(request))
    attestation_sha256 = _sha256_bytes(_secondary_product_canonical(attestation))
    lookup_token_sha256 = _sha256_bytes(str(request["attestation_lookup_token"]).encode("ascii"))
    consumed_at = value.get("consumed_at")
    if (
        set(value) != _SECONDARY_PRODUCT_CONSUME_RESPONSE_KEYS
        or value.get("schema") != _SECONDARY_PRODUCT_CONSUME_RESPONSE_SCHEMA
        or value.get("status") != "consumed"
        or value.get("stage") != request.get("stage")
        or value.get("transition") != request.get("transition")
        or value.get("predecessor_commit") != request.get("predecessor_commit")
        or value.get("predecessor_tree_sha256") != request.get("predecessor_tree_sha256")
        or value.get("candidate_commit") != request.get("candidate_commit")
        or value.get("candidate_tree_sha256") != request.get("candidate_tree_sha256")
        or value.get("next_env_sha256") != request.get("next_env_sha256")
        or value.get("product_receipt_sha256") != request.get("product_receipt_sha256")
        or value.get("sealed_runner_sha256") != request.get("sealed_runner_sha256")
        or request.get("server_rollout_attestation_sha256") != attestation_sha256
        or value.get("server_rollout_attestation_sha256") != attestation_sha256
        or value.get("lookup_token_sha256") != lookup_token_sha256
        or value.get("lookup_token_sha256") != attestation.get("lookup_token_sha256")
        or value.get("request_sha256") != request_sha256
        or value.get("state_version") != 2
        or type(consumed_at) is not int
        or consumed_at < int(attestation["issued_at"])
        or consumed_at > int(attestation["expires_at"])
        or consumed_at > int(time.time()) + 30
        or not isinstance(value.get("consume_binding_sha256"), str)
        or _HEX64.fullmatch(str(value.get("consume_binding_sha256"))) is None
        or set(str(value.get("consume_binding_sha256"))) == {"0"}
    ):
        raise ReleaseFailure("secondary_rollout_consume_response_invalid")


def _validate_secondary_document_map_rollout_receipt(
    receipt: Mapping[str, Any],
    *,
    receipt_sha256: str,
    previous: ReleaseIdentity,
    predecessor_release_metadata_sha256: str,
    predecessor_release_wheel_sha256: str,
    predecessor_live_env_sha256: str,
    predecessor_live_env_path_sha256: str,
    predecessor_release_anchor_path_sha256: str,
    profile_identity: Mapping[str, Any],
    primary_pid: int,
    primary_process_epoch_sha256: str,
    primary_ca_certificate_sha256: str,
) -> dict[str, Any]:
    """Validate one exclusive causal shadow receipt without trusting cumulative counters."""

    if (
        _HEX64.fullmatch(_SECONDARY_DOCUMENT_MAP_ASSIST_POLICY_SHA256) is None
        or _HEX64.fullmatch(_SECONDARY_DOCUMENT_MAP_ACCEPTED_SHADOW_RECEIPT_SHA256) is None
    ):
        raise ReleaseFailure("secondary_document_map_assist_acceptance_pending")
    if receipt_sha256 != _SECONDARY_DOCUMENT_MAP_ACCEPTED_SHADOW_RECEIPT_SHA256:
        raise ReleaseFailure("secondary_document_map_shadow_receipt_not_accepted")
    attestation = receipt.get("server_rollout_attestation")
    lookup_token = receipt.get("server_rollout_lookup_token")
    issued_at = attestation.get("issued_at") if isinstance(attestation, dict) else None
    expires_at = attestation.get("expires_at") if isinstance(attestation, dict) else None
    now = int(time.time())
    if (
        set(receipt) != _SECONDARY_DOCUMENT_MAP_RECEIPT_KEYS
        or receipt.get("schema") != _SECONDARY_DOCUMENT_MAP_RECEIPT_SCHEMA
        or receipt.get("status") != "passed"
        or receipt.get("document_text_retained_in_evidence") is not False
        or receipt.get("model_response_retained_in_evidence") is not False
        or receipt.get("document_text_digest_retained_in_evidence") is not False
        or receipt.get("model_response_digest_retained_in_evidence") is not False
        or not isinstance(attestation, dict)
        or set(attestation) != _SECONDARY_DOCUMENT_MAP_ATTESTATION_KEYS
        or attestation.get("schema") != _SECONDARY_DOCUMENT_MAP_ATTESTATION_SCHEMA
        or re.fullmatch(r"[0-9a-f]{32}", str(attestation.get("attestation_id") or "")) is None
        or set(str(attestation.get("attestation_id") or "")) == {"0"}
        or attestation.get("workload") != "document_map"
        or attestation.get("routing_mode") != "shadow"
        or attestation.get("shadow_policy_id") != _SECONDARY_DOCUMENT_MAP_SHADOW_POLICY_ID
        or attestation.get("shadow_policy_manifest_sha256") != _SECONDARY_DOCUMENT_MAP_SHADOW_POLICY_SHA256
        or attestation.get("observation_kind") != "exclusive_owner_one_shot"
        or any(
            type(attestation.get(name)) is not int
            for name in (
                "scheduler_selected_delta",
                "scheduler_success_delta",
                "shadow_valid_delta",
                "shadow_invalid_delta",
                "shadow_skipped_delta",
                "shadow_in_flight_before",
                "shadow_in_flight_after",
            )
        )
        or (
            attestation.get("scheduler_selected_delta"),
            attestation.get("scheduler_success_delta"),
            attestation.get("shadow_valid_delta"),
            attestation.get("shadow_invalid_delta"),
            attestation.get("shadow_skipped_delta"),
            attestation.get("shadow_in_flight_before"),
            attestation.get("shadow_in_flight_after"),
        )
        != (1, 1, 1, 0, 0, 0, 0)
        or attestation.get("primary_pid") != primary_pid
        or attestation.get("primary_process_epoch_sha256") != primary_process_epoch_sha256
        or attestation.get("primary_backend_version") != previous.version
        or attestation.get("primary_ca_certificate_sha256") != primary_ca_certificate_sha256
        or attestation.get("predecessor_release_commit") != previous.commit
        or attestation.get("predecessor_release_tree_manifest_sha256") != previous.tree_manifest_sha256
        or attestation.get("predecessor_release_metadata_sha256") != predecessor_release_metadata_sha256
        or attestation.get("predecessor_release_wheel_sha256") != predecessor_release_wheel_sha256
        or attestation.get("predecessor_live_env_sha256") != predecessor_live_env_sha256
        or attestation.get("predecessor_live_env_path_sha256") != predecessor_live_env_path_sha256
        or attestation.get("predecessor_release_anchor_path_sha256") != predecessor_release_anchor_path_sha256
        or attestation.get("candidate_profile_id") != profile_identity.get("profile_id")
        or attestation.get("candidate_profile_mode") != "assist"
        or attestation.get("candidate_profile_mode") != profile_identity.get("mode")
        or attestation.get("candidate_profile_allow_private_text") is not True
        or attestation.get("candidate_profile_allow_private_text")
        is not profile_identity.get("allow_private_text")
        or attestation.get("candidate_profile_context_tokens") != 4096
        or attestation.get("candidate_profile_context_tokens") != profile_identity.get("context_tokens")
        or attestation.get("candidate_profile_sha256") != _SECONDARY_FINALIST_CANDIDATE_PROFILE_SHA256
        or attestation.get("candidate_profile_manifest_sha256") != profile_identity.get("manifest_sha256")
        or attestation.get("candidate_profile_admission") != "accepted"
        or attestation.get("candidate_profile_admission") != profile_identity.get("admission")
        or attestation.get("served_model_alias") != profile_identity.get("served_model_alias")
        or attestation.get("gateway_ca_certificate_sha256")
        != profile_identity.get("gateway_ca_certificate_sha256")
        or attestation.get("document_text_retained") is not False
        or attestation.get("model_response_retained") is not False
        or attestation.get("document_text_digest_retained") is not False
        or attestation.get("model_response_digest_retained") is not False
        or attestation.get("state_version") != 1
        or type(issued_at) is not int
        or type(expires_at) is not int
        or not 0 < expires_at - issued_at <= 3_600
        or now < issued_at - 30
        or now > expires_at
        or not isinstance(lookup_token, str)
        or _HEX64.fullmatch(lookup_token) is None
        or set(lookup_token) == {"0"}
        or attestation.get("lookup_token_sha256") != _sha256_bytes(lookup_token.encode("ascii"))
        or receipt.get("server_rollout_attestation_sha256")
        != _sha256_bytes(_secondary_product_canonical(attestation))
    ):
        raise ReleaseFailure("secondary_document_map_shadow_receipt_invalid")
    for key, value in attestation.items():
        if (key.endswith("_sha256") or key == "signature") and (
            not isinstance(value, str) or _HEX64.fullmatch(value) is None or set(value) == {"0"}
        ):
            raise ReleaseFailure("secondary_document_map_shadow_receipt_invalid")
    return dict(attestation)


def _secondary_document_map_rollout_consume_request(
    *,
    lookup_token: str,
    attestation_sha256: str,
    previous: ReleaseIdentity,
    candidate: ReleaseIdentity,
    predecessor_env_sha256: str,
    next_env_sha256: str,
    product_receipt_sha256: str,
) -> dict[str, Any]:
    request = {
        "schema": _SECONDARY_DOCUMENT_MAP_CONSUME_REQUEST_SCHEMA,
        "attestation_lookup_token": lookup_token,
        "server_rollout_attestation_sha256": attestation_sha256,
        "transition": _SECONDARY_DOCUMENT_MAP_SHADOW_TO_ASSIST_TRANSITION,
        "predecessor_commit": previous.commit,
        "predecessor_tree_sha256": previous.tree_manifest_sha256,
        "predecessor_env_sha256": predecessor_env_sha256,
        "candidate_commit": candidate.commit,
        "candidate_tree_sha256": candidate.tree_manifest_sha256,
        "next_env_sha256": next_env_sha256,
        "product_receipt_sha256": product_receipt_sha256,
        "predecessor_policy_id": _SECONDARY_DOCUMENT_MAP_SHADOW_POLICY_ID,
        "predecessor_policy_manifest_sha256": _SECONDARY_DOCUMENT_MAP_SHADOW_POLICY_SHA256,
        "candidate_policy_id": _SECONDARY_DOCUMENT_MAP_ASSIST_POLICY_ID,
        "candidate_policy_manifest_sha256": _SECONDARY_DOCUMENT_MAP_ASSIST_POLICY_SHA256,
        "accepted_shadow_receipt_sha256": (_SECONDARY_DOCUMENT_MAP_ACCEPTED_SHADOW_RECEIPT_SHA256),
    }
    if (
        set(request) != _SECONDARY_DOCUMENT_MAP_CONSUME_REQUEST_KEYS
        or candidate.commit == previous.commit
        or predecessor_env_sha256 == next_env_sha256
        or product_receipt_sha256 != _SECONDARY_DOCUMENT_MAP_ACCEPTED_SHADOW_RECEIPT_SHA256
    ):
        raise ReleaseFailure("secondary_document_map_consume_request_invalid")
    for field in (
        "attestation_lookup_token",
        "server_rollout_attestation_sha256",
        "predecessor_tree_sha256",
        "predecessor_env_sha256",
        "candidate_tree_sha256",
        "next_env_sha256",
        "product_receipt_sha256",
        "predecessor_policy_manifest_sha256",
        "candidate_policy_manifest_sha256",
        "accepted_shadow_receipt_sha256",
    ):
        _closed_hash(str(request[field]), "secondary_document_map_consume_request_invalid")
    _closed_commit(previous.commit)
    _closed_commit(candidate.commit)
    return request


def _validate_secondary_document_map_consume_response(
    value: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
    attestation: Mapping[str, Any],
) -> None:
    request_sha256 = _sha256_bytes(_secondary_product_canonical(request))
    attestation_sha256 = _sha256_bytes(_secondary_product_canonical(attestation))
    lookup_sha256 = _sha256_bytes(str(request["attestation_lookup_token"]).encode("ascii"))
    consumed_at = value.get("consumed_at")
    copied = _SECONDARY_DOCUMENT_MAP_CONSUME_RESPONSE_KEYS - {
        "schema",
        "status",
        "lookup_token_sha256",
        "request_sha256",
        "consumed_at",
        "state_version",
        "consume_binding_sha256",
    }
    if (
        set(value) != _SECONDARY_DOCUMENT_MAP_CONSUME_RESPONSE_KEYS
        or value.get("schema") != _SECONDARY_DOCUMENT_MAP_CONSUME_RESPONSE_SCHEMA
        or value.get("status") != "consumed"
        or any(value.get(field) != request.get(field) for field in copied)
        or value.get("server_rollout_attestation_sha256") != attestation_sha256
        or value.get("lookup_token_sha256") != lookup_sha256
        or value.get("lookup_token_sha256") != attestation.get("lookup_token_sha256")
        or value.get("request_sha256") != request_sha256
        or type(consumed_at) is not int
        or consumed_at < int(attestation["issued_at"])
        or consumed_at > int(attestation["expires_at"])
        or consumed_at > int(time.time()) + 30
        or value.get("state_version") != 2
        or _HEX64.fullmatch(str(value.get("consume_binding_sha256") or "")) is None
        or set(str(value.get("consume_binding_sha256") or "")) == {"0"}
    ):
        raise ReleaseFailure("secondary_document_map_consume_response_invalid")


def _consume_secondary_document_map_after_exact_rechecks(
    previous: ReleaseIdentity,
    *,
    recheck_identity: Callable[[], None],
    consume: Callable[[], None],
) -> None:
    """Keep irreversible consume strictly between two full-tree checks."""

    verify_release_tree(previous)
    recheck_identity()
    consume()
    verify_release_tree(previous)
    recheck_identity()


def _load_secondary_rollout_receipt(
    path: Path,
    expected_sha256: str,
) -> dict[str, Any]:
    lexical = Path(os.path.abspath(path))
    if not path.is_absolute() or lexical != path:
        raise ReleaseFailure("secondary_rollout_receipt_path_invalid")
    raw = _read_private_regular_file(
        lexical,
        maximum_bytes=1 << 20,
        code="secondary_rollout_receipt_invalid",
    )
    if _sha256_bytes(raw) != _closed_hash(
        expected_sha256,
        "secondary_rollout_receipt_digest_invalid",
    ):
        raise ReleaseFailure("secondary_rollout_receipt_digest_mismatch")
    return _secondary_product_json(raw)


def _runtime_pins(path: Path) -> dict[str, str]:
    raw = _regular_file(path, maximum_bytes=MAX_LOCK_BYTES, code="runtime_lock_invalid").read_text(
        encoding="utf-8"
    )
    pins: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PIN.fullmatch(stripped)
        if match is None:
            raise ReleaseFailure("runtime_lock_not_exactly_pinned")
        name = match.group("name").replace("_", "-").casefold()
        if name in pins:
            raise ReleaseFailure("runtime_lock_duplicate_pin")
        pins[name] = match.group("version")
    if not pins:
        raise ReleaseFailure("runtime_lock_empty")
    return pins


def _preflight_base_python(base_python: Path) -> None:
    """Reject a Python that cannot create the release venv."""

    try:
        result = subprocess.run(  # noqa: S603
            [str(base_python), "-I", "-B", "-c", "import venv"],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseFailure("base_python_venv_unavailable") from exc
    if result.returncode != 0 or result.stdout or result.stderr:
        raise ReleaseFailure("base_python_venv_unavailable")


def _create_pipless_venv(base_python: Path, target: Path) -> None:
    try:
        result = subprocess.run(  # noqa: S603
            [
                str(base_python),
                "-I",
                "-B",
                "-m",
                "venv",
                "--without-pip",
                "--copies",
                str(target),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseFailure("release_venv_creation_failed") from exc
    if result.returncode != 0:
        raise ReleaseFailure("release_venv_creation_failed")


@dataclass(frozen=True)
class BuildSpec:
    commit: str
    version: str
    wheel: Path
    wheel_sha256: str
    runtime_lock: Path
    runtime_lock_sha256: str
    wheelhouse: Path
    wheelhouse_manifest: Path
    wheelhouse_manifest_sha256: str
    releases_root: Path
    anchor: Path
    env_file: Path
    friday_home: Path
    base_python: Path
    base_python_sha256: str
    alias_tool: Path
    alias_tool_sha256: str
    alias_dependency: Path
    alias_dependency_sha256: str
    secondary_product_runner: Path
    secondary_product_runner_sha256: str
    max_schema: int


@dataclass(frozen=True)
class ReleaseIdentity:
    root: Path
    commit: str
    version: str
    tree_manifest_sha256: str
    max_schema: int
    memory_vault_mode_contract: str = ""
    venv_relocation_contract: str = ""
    obsidian_cutover_contract: str = ""
    secondary_product_runner_sha256: str = ""


@dataclass(frozen=True)
class DatabaseBackup:
    schema_version: int
    receipt_sha256: str
    inbox_receipt_sha256: str
    opaque: Any = None
    obsidian_receipt_sha256: str = "0" * 64


@dataclass
class ActivationState:
    bridge_stopped: bool = False
    backend_stopped: bool = False
    anchor_switched: bool = False
    candidate_backend_started: bool = False
    candidate_bridge_started: bool = False


class ActivationPort(Protocol):
    def activation_policy_receipt(self) -> Mapping[str, str]: ...

    def verify_release(
        self,
        release: ReleaseIdentity,
        *,
        use_predecessor_config: bool = False,
    ) -> None: ...

    def verify_units(self, candidate: ReleaseIdentity) -> None: ...

    def verify_active_anchor(
        self,
        previous: ReleaseIdentity,
        candidate: ReleaseIdentity,
    ) -> None: ...

    def stop_bridge(self) -> None: ...

    def stop_backend(self) -> None: ...

    def services_inactive(self) -> bool: ...

    def writer_leases_held(self) -> bool: ...

    def acquire_writer_leases(self) -> None: ...

    def release_writer_leases(self) -> None: ...

    def validate_staged_config_transition(
        self,
        transition: str,
        predecessor_env_sha256: str,
        next_env_file: Path,
        next_env_file_sha256: str,
    ) -> None: ...

    def activate_staged_config_transition(
        self,
        transition: str,
        predecessor_env_sha256: str,
        next_env_file: Path,
        next_env_file_sha256: str,
    ) -> None: ...

    def select_predecessor_config_transition(
        self,
        transition: str,
        predecessor_env_sha256: str,
        next_env_file: Path,
        next_env_file_sha256: str,
    ) -> None: ...

    def backup_database(self) -> DatabaseBackup: ...

    def offline_migrate(self, release: ReleaseIdentity, backup: DatabaseBackup) -> None: ...

    def repair_file_aliases(
        self,
        release: ReleaseIdentity,
        backup: DatabaseBackup,
    ) -> Mapping[str, Any]: ...

    def switch_anchor(self, release: ReleaseIdentity) -> None: ...

    def start_backend(self, release: ReleaseIdentity) -> None: ...

    def accept_backend(self, release: ReleaseIdentity) -> None: ...

    def start_bridge(self, release: ReleaseIdentity) -> None: ...

    def accept_bridge(self, release: ReleaseIdentity) -> None: ...

    def restore_database(self, backup: DatabaseBackup) -> None: ...


class ActivationJournalPort(Protocol):
    def begin(
        self,
        *,
        candidate: ReleaseIdentity,
        previous: ReleaseIdentity,
        fallback: ReleaseIdentity,
    ) -> None: ...

    def record(
        self,
        phase: str,
        *,
        backup: DatabaseBackup | None = None,
        database_mutation_possible: bool = False,
        network_writer_uncertain: bool = False,
        writer_target: str = "",
        terminal_receipt_sha256: str = "",
        staged_transition_validation_sha256: str = "",
    ) -> None: ...

    def load(self) -> Mapping[str, Any]: ...

    def release_identities(self) -> tuple[ReleaseIdentity, ReleaseIdentity, ReleaseIdentity]: ...

    def database_backup(self) -> DatabaseBackup | None: ...


_JOURNAL_PHASES = frozenset(
    {
        "prepared",
        "bridge_stop_attempted",
        "backend_stop_attempted",
        "writers_quiesced",
        "leases_acquired",
        "backup_complete",
        "environment_swap_attempted",
        "environment_active",
        "migration_attempted",
        "alias_repair_attempted",
        "candidate_anchor_attempted",
        "candidate_anchor_active",
        "backend_start_attempted",
        "backend_accepted",
        "bridge_start_attempted",
        "bridge_accepted",
        "rollback_stop_attempted",
        "rollback_restore_attempted",
        "rollback_anchor_attempted",
        "rollback_backend_start_attempted",
        "rollback_backend_accepted",
        "rollback_bridge_start_attempted",
        "recovery_stop_attempted",
        "recovery_restore_attempted",
        "recovery_anchor_attempted",
        "recovery_backend_start_attempted",
        "recovery_backend_accepted",
        "recovery_bridge_start_attempted",
        "clear",
        "rolled_back",
        "recovered",
    }
)
_TERMINAL_JOURNAL_PHASES = frozenset({"clear", "rolled_back", "recovered"})

_ACTIVATION_FORWARD: dict[str, frozenset[str]] = {
    "prepared": frozenset({"bridge_stop_attempted"}),
    "bridge_stop_attempted": frozenset({"backend_stop_attempted"}),
    "backend_stop_attempted": frozenset({"writers_quiesced"}),
    "writers_quiesced": frozenset({"leases_acquired"}),
    "leases_acquired": frozenset({"backup_complete"}),
    "backup_complete": frozenset({"migration_attempted", "environment_swap_attempted"}),
    "environment_swap_attempted": frozenset({"environment_active"}),
    "environment_active": frozenset({"migration_attempted"}),
    "migration_attempted": frozenset({"alias_repair_attempted"}),
    "alias_repair_attempted": frozenset({"candidate_anchor_attempted"}),
    "candidate_anchor_attempted": frozenset({"candidate_anchor_active"}),
    "candidate_anchor_active": frozenset({"backend_start_attempted"}),
    "backend_start_attempted": frozenset({"backend_accepted"}),
    "backend_accepted": frozenset({"bridge_start_attempted"}),
    "bridge_start_attempted": frozenset({"bridge_accepted"}),
    "bridge_accepted": frozenset({"clear"}),
    "rollback_stop_attempted": frozenset({"rollback_restore_attempted", "rollback_anchor_attempted"}),
    "rollback_restore_attempted": frozenset({"rollback_anchor_attempted"}),
    "rollback_anchor_attempted": frozenset({"rollback_backend_start_attempted"}),
    "rollback_backend_start_attempted": frozenset({"rollback_backend_accepted"}),
    "rollback_backend_accepted": frozenset({"rollback_bridge_start_attempted"}),
    "rollback_bridge_start_attempted": frozenset({"rolled_back"}),
    "recovery_stop_attempted": frozenset({"recovery_restore_attempted", "recovery_anchor_attempted"}),
    "recovery_restore_attempted": frozenset({"recovery_anchor_attempted"}),
    "recovery_anchor_attempted": frozenset({"recovery_backend_start_attempted"}),
    "recovery_backend_start_attempted": frozenset({"recovery_backend_accepted"}),
    "recovery_backend_accepted": frozenset({"recovery_bridge_start_attempted"}),
    "recovery_bridge_start_attempted": frozenset({"recovered"}),
}


def _journal_transition_allowed(current: str, following: str) -> bool:
    if current in _TERMINAL_JOURNAL_PHASES:
        return False
    if following == "recovery_stop_attempted":
        # Recovery always re-quiesces and may safely replay from its first phase.
        return True
    if following == "rollback_stop_attempted" and not current.startswith("recovery_"):
        return True
    return following in _ACTIVATION_FORWARD.get(current, frozenset())


class OperatorTransactionLock:
    """One owner-only nonblocking process lock for all release mutations."""

    def __init__(self, path: Path) -> None:
        parent = _private_directory(path.parent)
        lexical = Path(os.path.abspath(path))
        if lexical.parent != parent or lexical.name != "immutable-release-operator.v1.lock":
            raise ReleaseFailure("operator_transaction_lock_path_invalid")
        self.path = lexical
        self._descriptor = -1

    def __enter__(self) -> OperatorTransactionLock:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise ReleaseFailure("operator_transaction_lock_invalid") from exc
        try:
            status = os.fstat(descriptor)
            lexical = os.stat(self.path, follow_symlinks=False)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
                or status.st_uid != os.geteuid()
                or stat.S_IMODE(status.st_mode) & 0o077
                or (status.st_dev, status.st_ino) != (lexical.st_dev, lexical.st_ino)
            ):
                raise ReleaseFailure("operator_transaction_lock_invalid")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReleaseFailure("operator_transaction_in_progress") from exc
            after = os.stat(self.path, follow_symlinks=False)
            if (status.st_dev, status.st_ino) != (after.st_dev, after.st_ino):
                raise ReleaseFailure("operator_transaction_lock_changed")
        except BaseException:
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        descriptor, self._descriptor = self._descriptor, -1
        if descriptor >= 0:
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def render_units(*, anchor: Path, env_file: Path, friday_home: Path) -> dict[str, str]:
    """Generate the one stable systemd contract shared by every release."""

    for candidate in (anchor, env_file, friday_home):
        if not candidate.is_absolute() or any(char in str(candidate) for char in "\n\r\0"):
            raise ReleaseFailure("unit_path_invalid")
    prefix = """[Unit]
StartLimitIntervalSec=60
StartLimitBurst=3
"""
    service = f"""
[Service]
Type=simple
Restart=on-failure
RestartSec=5
KillMode=control-group
UMask=0077
Environment=FRIDAY_HOME={friday_home}
WorkingDirectory={friday_home}
UnsetEnvironment=PYTHONPATH
"""
    backend = (
        prefix
        + "Description=Friday backend (API + Admin UI)\nAfter=network-online.target\n"
        + service
        + f"ExecStart={anchor}/venv/bin/python -I -B -m friday.cli --env-file {env_file} server\n"
        + "\n[Install]\nWantedBy=default.target\n"
    )
    bridge = (
        prefix
        + "Description=Friday Telegram bridge\nAfter=network-online.target friday-backend.service\n"
        + service
        + f"ExecStart={anchor}/venv/bin/python -I -B -m friday.cli --env-file {env_file} telegram-bridge\n"
        + "\n[Install]\nWantedBy=default.target\n"
    )
    return {"friday-backend.service": backend, "friday-bridge.service": bridge}


def _smoke_script(
    expected_root: Path,
    expected_version: str,
    expected_schema: int,
    obsidian_cutover_contract: str,
) -> str:
    values = json.dumps(
        {
            "root": str(expected_root),
            "version": expected_version,
            "schema": expected_schema,
            "obsidian_cutover_contract": obsidian_cutover_contract,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    return f"""
import importlib.metadata, inspect, json, os, pathlib, sys
expected=json.loads({values!r})
import friday, friday.cli, friday.config, friday.execution_kernel, friday.orchestration.file_read
import friday.server, friday.storage, friday.storage._conversations, friday.telegram_bridge
host_modules=()
if expected['schema']>=43:
 import friday_host_agent, friday_package_broker
 host_modules=(friday_host_agent,friday_package_broker)
root=pathlib.Path(expected['root']).resolve(strict=True)
assert pathlib.Path(sys.prefix).resolve(strict=True)==(root/'venv').resolve(strict=True)
assert pathlib.Path(sys.executable).resolve(strict=True)==(root/'venv/bin/python').resolve(strict=True)
assert pathlib.Path(sys.base_prefix).resolve(strict=True)!=pathlib.Path(sys.prefix).resolve(strict=True)
modules=(friday, friday.cli, friday.execution_kernel, friday.orchestration.file_read,
 friday.server, friday.storage, friday.storage._conversations, friday.telegram_bridge)+host_modules
assert all(pathlib.Path(m.__file__).resolve(strict=True).is_relative_to(root) for m in modules)
assert friday.__version__ == expected['version']
assert importlib.metadata.version('friday') == expected['version']
assert friday.storage.SCHEMA_VERSION == expected['schema']
assert callable(friday.cli.build_parser)
assert callable(friday.server.create_app)
assert callable(friday.execution_kernel.confirm_staged_request_effect)
assert callable(friday.storage._conversations.create_conversation_in_transaction)
assert callable(friday.telegram_bridge.TelegramBridge)
assert 'confirm_staged_request_effect' in inspect.getsource(friday.orchestration.file_read)
settings=friday.config.load_settings()
contract=''
if tuple(getattr(friday.config,'MEMORY_VAULT_MODES',()))==('disabled','full_owner'):
 assert getattr(settings,'memory_vault_mode',None)=='disabled'
 os.environ['FRIDAY_MEMORY_VAULT_MODE']='full_owner'
 assert friday.config.load_settings().memory_vault_mode=='full_owner'
 os.environ['FRIDAY_MEMORY_VAULT_MODE']='invalid'
 try:
  friday.config.load_settings()
 except ValueError:
  pass
 else:
  raise AssertionError('unknown memory-vault mode did not fail closed')
 os.environ.pop('FRIDAY_MEMORY_VAULT_MODE',None)
 contract='v1'
obsidian_contract=expected['obsidian_cutover_contract']
assert obsidian_contract in ('','exact-root-v1')
obsidian_capable=obsidian_contract=='exact-root-v1'
assert expected['schema']<35 or obsidian_capable
obsidian_mode='disabled'
obsidian_root=(pathlib.Path(os.environ['FRIDAY_HOME'])/'data'/'obsidian').absolute()
obsidian_identity_required=obsidian_capable
{_OBSIDIAN_SETTINGS_IDENTITY_PROBE}
print(json.dumps({{'memory_vault_mode_contract':contract,'status':'clear','schema':expected['schema']}},sort_keys=True,separators=(',',':')))
"""


def _direct_console_smoke(
    release: ReleaseIdentity,
    *,
    scratch: Path,
    environment: Mapping[str, str],
) -> None:
    """Execute release entry points as users do, through their own shebangs."""

    probes = (("friday", "--help"), ("jericho", "--help"), ("pip", "--version"))
    for name, argument in probes:
        entrypoint = release.root / "venv/bin" / name
        try:
            result = subprocess.run(  # noqa: S603
                [str(entrypoint), argument],
                check=False,
                capture_output=True,
                timeout=30,
                cwd=scratch,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseFailure("installed_direct_entrypoint_smoke_failed") from exc
        if result.returncode != 0 or result.stderr or not result.stdout or len(result.stdout) > 1 << 20:
            raise ReleaseFailure("installed_direct_entrypoint_smoke_failed")
        if name == "pip" and (
            not result.stdout.startswith(f"pip {BOOTSTRAP_WHEELS[0][1]} from ".encode("ascii"))
            or os.fsencode(str(release.root / "venv")) not in result.stdout
        ):
            raise ReleaseFailure("installed_direct_entrypoint_smoke_invalid")


def _activation_smoke(
    *,
    physical_root: Path,
    bound_root: Path,
    require_interpreter: bool,
    scratch: Path,
    environment: Mapping[str, str],
) -> None:
    """Source the generated Bash activation and verify its effective binding."""

    script = r"""
source "$1" || exit 91
expected_venv="$2/venv"
[[ "${VIRTUAL_ENV-}" == "$expected_venv" ]] || exit 92
[[ "${PATH%%:*}" == "$expected_venv/bin" ]] || exit 93
if [[ "$3" == 1 ]]; then
    effective_python=$(command -v python) || exit 94
    [[ "$effective_python" == "$expected_venv/bin/python" ]] || exit 95
    "$effective_python" -I -B -c "$4" "$2" || exit 96
fi
printf '%s\n' 'friday-activation-smoke:clear:v1'
"""
    python_probe = r"""
import os, pathlib, sys
root = pathlib.Path(sys.argv[1]).resolve(strict=True)
venv = (root / "venv").resolve(strict=True)
assert pathlib.Path(os.environ["VIRTUAL_ENV"]).resolve(strict=True) == venv
assert pathlib.Path(sys.prefix).resolve(strict=True) == venv
assert pathlib.Path(sys.executable).resolve(strict=True) == (venv / "bin/python").resolve(strict=True)
"""
    command = [
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        script,
        "friday-activation-smoke",
        str(physical_root / "venv/bin/activate"),
        str(bound_root),
        "1" if require_interpreter else "0",
        python_probe,
    ]
    try:
        result = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            timeout=30,
            cwd=scratch,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseFailure("installed_activation_smoke_failed") from exc
    if result.returncode != 0 or result.stdout != _ACTIVATION_SMOKE_RECEIPT or result.stderr:
        raise ReleaseFailure("installed_activation_smoke_failed")


@contextmanager
def _isolated_smoke_environment(release_root: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    """Keep import probes away from operator configuration, secrets, and live state."""

    scratch: Path | None = None
    try:
        resolved_release = Path(os.path.abspath(release_root)).resolve(strict=True)
        scratch_root = _private_directory(_SMOKE_SCRATCH_ROOT, create=True)
        if (
            scratch_root == resolved_release
            or scratch_root.is_relative_to(resolved_release)
            or resolved_release.is_relative_to(scratch_root)
        ):
            raise ReleaseFailure("installed_surface_smoke_isolation_failed")
        scratch = Path(tempfile.mkdtemp(prefix="friday-release-smoke-", dir=scratch_root))
        scratch = _private_directory(scratch)
        home = _private_directory(scratch / "home", create=True)
        data = _private_directory(home / "data", create=True)
        state = _private_directory(data / "state", create=True)
        config = _private_directory(home / "config", create=True)
        child_tmp = _private_directory(scratch / "tmp", create=True)
        database = str(state / "smoke.sqlite3")
        env_file = str(config / "no-env-file")
        environment = {
            "HOME": str(home),
            "FRIDAY_HOME": str(home),
            "JERICHO_HOME": str(home),
            "FRIDAY_DATA_DIR": str(data),
            "JERICHO_DATA_DIR": str(data),
            "FRIDAY_STATE_DIR": str(state),
            "JERICHO_STATE_DIR": str(state),
            "FRIDAY_DATABASE_PATH": database,
            "JERICHO_DATABASE_PATH": database,
            "FRIDAY_DATABASE_MUST_EXIST": "0",
            "JERICHO_DATABASE_MUST_EXIST": "0",
            "FRIDAY_ENV_FILE": env_file,
            "JERICHO_ENV_FILE": env_file,
            "TMPDIR": str(child_tmp),
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    except ReleaseFailure:
        if scratch is not None:
            with suppress(Exception):
                _cleanup_staging_tree(scratch)
        raise
    except OSError as exc:
        if scratch is not None:
            with suppress(Exception):
                _cleanup_staging_tree(scratch)
        raise ReleaseFailure("installed_surface_smoke_isolation_failed") from exc

    try:
        yield scratch, environment
    except BaseException:  # cleanup must preserve the active smoke failure unchanged
        with suppress(Exception):
            _cleanup_staging_tree(scratch)
        raise
    else:
        with suppress(Exception):
            _cleanup_staging_tree(scratch)
        try:
            os.lstat(scratch)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ReleaseFailure("installed_surface_smoke_cleanup_failed") from exc
        raise ReleaseFailure("installed_surface_smoke_cleanup_failed")


def installed_surface_smoke(release: ReleaseIdentity) -> str:
    python = release.root / "venv/bin/python"
    command = [
        str(python),
        "-I",
        "-B",
        "-c",
        _smoke_script(
            release.root,
            release.version,
            release.max_schema,
            release.obsidian_cutover_contract,
        ),
    ]
    with _isolated_smoke_environment(release.root) as (scratch, environment):
        result = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            timeout=90,
            cwd=scratch,
            env=environment,
        )
        if result.returncode != 0 or result.stderr or len(result.stdout) > 4096:
            raise ReleaseFailure("installed_surface_smoke_failed")
        try:
            receipt = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseFailure("installed_surface_smoke_invalid") from exc
        if receipt != {
            "memory_vault_mode_contract": release.memory_vault_mode_contract,
            "schema": release.max_schema,
            "status": "clear",
        }:
            raise ReleaseFailure("installed_surface_smoke_invalid")
        if release.venv_relocation_contract == VENV_RELOCATION_CONTRACT:
            _direct_console_smoke(release, scratch=scratch, environment=environment)
            _activation_smoke(
                physical_root=release.root,
                bound_root=release.root,
                require_interpreter=True,
                scratch=scratch,
                environment=environment,
            )
        else:
            help_result = subprocess.run(  # noqa: S603
                [str(python), "-I", "-B", "-m", "friday.cli", "--help"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
                cwd=scratch,
                env=environment,
            )
            if help_result.returncode != 0 or help_result.stderr:
                raise ReleaseFailure("installed_cli_smoke_failed")
        return _sha256_bytes(result.stdout)


def _installed_pin_smoke(python: Path, pins: Mapping[str, str]) -> None:
    expected_pins = {**pins, **{name: version for name, version, _filename in BOOTSTRAP_WHEELS}}
    expected = json.dumps(dict(sorted(expected_pins.items())), ensure_ascii=True, separators=(",", ":"))
    script = f"""
import importlib.metadata, json
expected=json.loads({expected!r})
norm=lambda value:value.replace('_','-').casefold()
actual={{norm(dist.metadata['Name']):dist.version for dist in importlib.metadata.distributions()}}
assert all(actual.get(name)==version for name,version in expected.items())
assert set(actual)==set(expected)|{{'friday'}}
"""
    result = subprocess.run(  # noqa: S603
        [str(python), "-I", "-B", "-c", script],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0 or result.stdout or result.stderr:
        raise ReleaseFailure("installed_dependency_surface_mismatch")


def _entrypoint_shebang(python: Path) -> bytes:
    encoded = os.fsencode(str(python))
    shebang = b"#!" + encoded + b"\n"
    if (
        not python.is_absolute()
        or any(character in encoded for character in b" \t\r\n\0")
        or len(shebang) > MAX_SHEBANG_BYTES
    ):
        raise ReleaseFailure("release_entrypoint_shebang_invalid")
    return shebang


def _owned_release_directory(path: Path, *, code: str) -> Path:
    lexical = Path(os.path.abspath(path))
    try:
        resolved = lexical.resolve(strict=True)
        status = os.stat(lexical, follow_symlinks=False)
    except OSError as exc:
        raise ReleaseFailure(code) from exc
    if resolved != lexical or not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
        raise ReleaseFailure(code)
    return resolved


def _discover_python_entrypoints(root: Path, *, bound_python: Path) -> tuple[Path, ...]:
    """Return every executable text launcher, rejecting a non-exact shebang."""

    bin_directory = _owned_release_directory(
        root / "venv/bin",
        code="release_entrypoint_directory_invalid",
    )
    physical_python = _regular_file(
        bin_directory / "python",
        maximum_bytes=1 << 30,
        code="release_entrypoint_interpreter_invalid",
    )
    if not os.access(physical_python, os.X_OK):
        raise ReleaseFailure("release_entrypoint_interpreter_invalid")
    expected = _entrypoint_shebang(bound_python)
    entrypoints: list[Path] = []
    for path in sorted(bin_directory.iterdir(), key=lambda item: os.fsencode(item.name)):
        status = os.lstat(path)
        if not stat.S_ISREG(status.st_mode) or not status.st_mode & 0o111:
            continue
        with path.open("rb") as stream:
            first_line = stream.readline(MAX_SHEBANG_BYTES + 2)
        if not first_line.startswith(b"#!"):
            continue
        if first_line != expected:
            raise ReleaseFailure("release_entrypoint_shebang_mismatch")
        if status.st_uid != os.geteuid() or status.st_nlink != 1 or status.st_size > MAX_CONSOLE_SCRIPT_BYTES:
            raise ReleaseFailure("release_entrypoint_invalid")
        entrypoints.append(path)
    required = {bin_directory / name for name in ("friday", "jericho", "pip")}
    if not required.issubset(entrypoints):
        raise ReleaseFailure("release_required_entrypoint_missing")
    return tuple(entrypoints)


def _record_files(venv: Path) -> tuple[tuple[Path, Path], ...]:
    site_directories = sorted((venv / "lib").glob("python*/site-packages"))
    if len(site_directories) != 1:
        raise ReleaseFailure("installed_record_layout_invalid")
    site = _owned_release_directory(site_directories[0], code="installed_record_layout_invalid")
    records = tuple(
        (site, _regular_file(path, maximum_bytes=MAX_RECORD_BYTES, code="installed_record_invalid"))
        for path in sorted(site.glob("*.dist-info/RECORD"))
    )
    if not records:
        raise ReleaseFailure("installed_record_layout_invalid")
    return records


def _read_record_rows(record: Path) -> list[list[str]]:
    try:
        text = record.read_text(encoding="utf-8")
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ReleaseFailure("installed_record_invalid") from exc
    if not rows or any(len(row) != 3 for row in rows):
        raise ReleaseFailure("installed_record_invalid")
    return rows


def _record_target(*, site: Path, venv: Path, value: str) -> Path:
    if not value or any(character in value for character in "\0\r\n") or Path(value).is_absolute():
        raise ReleaseFailure("installed_record_path_invalid")
    lexical = Path(os.path.abspath(site / value))
    if not lexical.is_relative_to(venv):
        raise ReleaseFailure("installed_record_path_escape")
    return lexical


def _record_digest(path: Path) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).rstrip(b"=")
    return "sha256=" + digest.decode("ascii")


def _validate_installed_records(
    root: Path,
    entrypoints: Sequence[Path],
    *,
    require_deleted_pycache: bool,
) -> None:
    """Validate every non-empty RECORD digest and the unique console owners."""

    venv = _owned_release_directory(root / "venv", code="installed_record_layout_invalid")
    entrypoint_set = {Path(os.path.abspath(path)) for path in entrypoints}
    owners = {path: 0 for path in entrypoint_set}
    for site, record in _record_files(venv):
        seen: set[Path] = set()
        for path_value, digest, size in _read_record_rows(record):
            target = _record_target(site=site, venv=venv, value=path_value)
            if target in seen:
                raise ReleaseFailure("installed_record_duplicate_path")
            seen.add(target)
            if target in owners:
                owners[target] += 1
            if bool(digest) != bool(size):
                raise ReleaseFailure("installed_record_invalid")
            if not digest:
                is_self = target == record
                is_cache = "__pycache__" in target.parts and target.suffix == ".pyc"
                if not is_self and not is_cache:
                    raise ReleaseFailure("installed_record_unbound_entry")
                if is_cache and require_deleted_pycache and (target.exists() or target.is_symlink()):
                    raise ReleaseFailure("installed_record_pycache_present")
                if is_self and not target.is_file():
                    raise ReleaseFailure("installed_record_invalid")
                continue
            try:
                status = os.lstat(target)
                resolved = target.resolve(strict=True)
            except OSError as exc:
                raise ReleaseFailure("installed_record_target_missing") from exc
            if (
                resolved != target
                or not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
                or status.st_uid != os.geteuid()
                or digest != _record_digest(target)
                or size != str(status.st_size)
            ):
                raise ReleaseFailure("installed_record_mismatch")
    if any(count != 1 for count in owners.values()):
        raise ReleaseFailure("installed_entrypoint_record_owner_mismatch")


def _write_record_rows(record: Path, rows: Sequence[Sequence[str]]) -> None:
    output = io.StringIO(newline="")
    writer = csv.writer(output, dialect="excel")
    writer.writerows(rows)
    record.write_text(output.getvalue(), encoding="utf-8", newline="")


def _update_entrypoint_records(root: Path, entrypoints: Sequence[Path]) -> None:
    venv = _owned_release_directory(root / "venv", code="installed_record_layout_invalid")
    expected = {Path(os.path.abspath(path)): 0 for path in entrypoints}
    for site, record in _record_files(venv):
        rows = _read_record_rows(record)
        changed = False
        seen: set[Path] = set()
        for row in rows:
            target = _record_target(site=site, venv=venv, value=row[0])
            if target in seen:
                raise ReleaseFailure("installed_record_duplicate_path")
            seen.add(target)
            if target not in expected:
                continue
            expected[target] += 1
            status = os.lstat(target)
            row[1] = _record_digest(target)
            row[2] = str(status.st_size)
            changed = True
        if changed:
            _write_record_rows(record, rows)
    if any(count != 1 for count in expected.values()):
        raise ReleaseFailure("installed_entrypoint_record_owner_mismatch")


_ACTIVATION_REFERENCE_COUNTS = {"activate": 2, "activate.csh": 1, "activate.fish": 1}


def _verify_activation_bindings(root: Path, *, bound_root: Path) -> None:
    bin_directory = root / "venv/bin"
    encoded_root = os.fsencode(str(bound_root))
    bound_venv = os.fsencode(str(bound_root / "venv"))
    expected_references = {
        "activate": (
            b"        VIRTUAL_ENV=$(cygpath " + bound_venv + b")",
            b"        export VIRTUAL_ENV=" + bound_venv,
        ),
        "activate.csh": (b"setenv VIRTUAL_ENV " + bound_venv,),
        "activate.fish": (b"set -gx VIRTUAL_ENV " + bound_venv,),
    }
    for name, expected_lines in expected_references.items():
        path = _regular_file(
            bin_directory / name,
            maximum_bytes=1 << 20,
            code="release_activation_script_invalid",
        )
        lines = path.read_bytes().splitlines()
        references = tuple(line for line in lines if encoded_root in line)
        if name == "activate":
            assignments = tuple(
                line
                for line in lines
                if line.lstrip().startswith(b"VIRTUAL_ENV=")
                or line.lstrip().startswith(b"export VIRTUAL_ENV=")
            )
        elif name == "activate.csh":
            assignments = tuple(line for line in lines if line.lstrip().startswith(b"setenv VIRTUAL_ENV "))
        else:
            assignments = tuple(line for line in lines if line.lstrip().startswith(b"set -gx VIRTUAL_ENV "))
        if references != expected_lines or assignments != expected_lines:
            raise ReleaseFailure("release_activation_binding_mismatch")
    powershell = _regular_file(
        bin_directory / "Activate.ps1",
        maximum_bytes=1 << 20,
        code="release_activation_script_invalid",
    ).read_bytes()
    if (
        encoded_root in powershell
        or b"$VenvExecPath = Split-Path -Parent $MyInvocation.MyCommand.Definition" not in powershell
        or b"$VenvDir = $VenvExecDir.Parent.FullName.TrimEnd" not in powershell
    ):
        raise ReleaseFailure("release_activation_binding_mismatch")
    config = _regular_file(
        root / "venv/pyvenv.cfg",
        maximum_bytes=1 << 20,
        code="release_pyvenv_config_invalid",
    ).read_bytes()
    command_lines = [line for line in config.splitlines() if line.startswith(b"command = ")]
    if len(command_lines) != 1 or not command_lines[0].endswith(b" " + bound_venv):
        raise ReleaseFailure("release_pyvenv_binding_mismatch")
    if config.count(encoded_root) != 1:
        raise ReleaseFailure("release_pyvenv_binding_mismatch")


def _assert_path_bytes_absent(root: Path, forbidden: bytes) -> None:
    if not forbidden:
        raise ReleaseFailure("release_staging_path_invalid")
    overlap = max(0, len(forbidden) - 1)
    try:
        for path in root.rglob("*"):
            status = os.lstat(path)
            if stat.S_ISLNK(status.st_mode):
                if forbidden in os.fsencode(os.readlink(path)):
                    raise ReleaseFailure("release_staging_path_leaked")
                continue
            if not stat.S_ISREG(status.st_mode):
                continue
            carry = b""
            with path.open("rb") as stream:
                while chunk := stream.read(1 << 20):
                    candidate = carry + chunk
                    if forbidden in candidate:
                        raise ReleaseFailure("release_staging_path_leaked")
                    carry = candidate[-overlap:] if overlap else b""
    except ReleaseFailure:
        raise
    except OSError as exc:
        raise ReleaseFailure("release_staging_scan_failed") from exc


def _rewrite_exact_path_references(path: Path, *, old: bytes, new: bytes, count: int) -> None:
    source = _regular_file(path, maximum_bytes=1 << 20, code="release_relocation_source_invalid")
    content = source.read_bytes()
    if content.count(old) != count:
        raise ReleaseFailure("release_relocation_reference_mismatch")
    source.write_bytes(content.replace(old, new))


def _relocate_venv_generated_paths(staging: Path, target: Path) -> tuple[Path, ...]:
    """Rebind every venv-generated absolute path before the immutable seal."""

    staging = Path(os.path.abspath(staging))
    target = Path(os.path.abspath(target))
    if (
        not staging.is_absolute()
        or not target.is_absolute()
        or staging.parent != target.parent
        or staging == target
        or target.exists()
        or target.is_symlink()
    ):
        raise ReleaseFailure("release_relocation_target_invalid")
    old_root = os.fsencode(str(staging))
    new_root = os.fsencode(str(target))
    old_python = staging / "venv/bin/python"
    new_python = target / "venv/bin/python"
    old_shebang = _entrypoint_shebang(old_python)
    new_shebang = _entrypoint_shebang(new_python)
    entrypoints = _discover_python_entrypoints(staging, bound_python=old_python)
    _validate_installed_records(staging, entrypoints, require_deleted_pycache=False)
    for entrypoint in entrypoints:
        content = entrypoint.read_bytes()
        if not content.startswith(old_shebang) or content.count(old_root) != 1:
            raise ReleaseFailure("release_entrypoint_relocation_mismatch")
        entrypoint.write_bytes(new_shebang + content[len(old_shebang) :])
    _update_entrypoint_records(staging, entrypoints)
    for name, expected_count in _ACTIVATION_REFERENCE_COUNTS.items():
        _rewrite_exact_path_references(
            staging / "venv/bin" / name,
            old=old_root,
            new=new_root,
            count=expected_count,
        )
    _rewrite_exact_path_references(
        staging / "venv/pyvenv.cfg",
        old=old_root,
        new=new_root,
        count=1,
    )
    return entrypoints


def _verify_relocated_venv(
    physical_root: Path,
    *,
    bound_root: Path,
    forbidden_staging_root: Path | None,
) -> None:
    entrypoints = _discover_python_entrypoints(
        physical_root,
        bound_python=bound_root / "venv/bin/python",
    )
    _verify_activation_bindings(physical_root, bound_root=bound_root)
    _validate_installed_records(physical_root, entrypoints, require_deleted_pycache=True)
    if forbidden_staging_root is not None:
        _assert_path_bytes_absent(physical_root, os.fsencode(str(forbidden_staging_root)))


def _manifest_entries(root: Path, *, mode_overrides: Mapping[str, int] | None = None) -> list[str]:
    entries: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == "artifacts/release-tree.sha256" or "__pycache__" in path.parts:
            continue
        status = os.lstat(path)
        mode = (mode_overrides or {}).get(relative, stat.S_IMODE(status.st_mode))
        if stat.S_ISLNK(status.st_mode):
            target = os.readlink(path)
            digest = _sha256_bytes(target.encode("utf-8", errors="surrogatepass"))
            entries.append(f"L {mode:04o} {digest} {relative}")
        elif stat.S_ISREG(status.st_mode):
            entries.append(f"F {mode:04o} {_sha256_file(path)} {relative}")
        elif stat.S_ISDIR(status.st_mode):
            entries.append(f"D {mode:04o} {'0' * 64} {relative}")
        else:
            raise ReleaseFailure("release_tree_special_file")
    return entries


def _wheel_pin(filename: str) -> tuple[str, str]:
    if not filename.endswith(".whl"):
        raise ReleaseFailure("wheelhouse_pin_mismatch")
    fields = filename[:-4].split("-")
    if len(fields) < 5 or not fields[0] or not fields[1]:
        raise ReleaseFailure("wheelhouse_pin_mismatch")
    return fields[0].replace("_", "-").casefold(), fields[1]


def _verify_wheelhouse(
    directory: Path,
    manifest_path: Path,
    runtime_pins: Mapping[str, str],
) -> str:
    manifest = _regular_file(
        manifest_path,
        maximum_bytes=MAX_LOCK_BYTES,
        code="wheelhouse_manifest_invalid",
    )
    declared: dict[str, str] = {}
    for line in manifest.read_text(encoding="ascii").splitlines():
        digest, separator, filename = line.partition("  ")
        if (
            not separator
            or _HEX64.fullmatch(digest) is None
            or not filename
            or filename != Path(filename).name
            or filename in declared
        ):
            raise ReleaseFailure("wheelhouse_manifest_invalid")
        declared[filename] = digest
    actual: dict[str, str] = {}
    actual_pins: dict[str, str] = {}
    actual_filenames: dict[str, str] = {}
    for item in directory.iterdir():
        status = os.lstat(item)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1 or status.st_uid != os.geteuid():
            raise ReleaseFailure("wheelhouse_contains_unsafe_entry")
        actual[item.name] = _sha256_file(item)
        name, version = _wheel_pin(item.name)
        if name in actual_pins:
            raise ReleaseFailure("wheelhouse_pin_mismatch")
        actual_pins[name] = version
        actual_filenames[name] = item.name
    if not declared or len(declared) > MAX_WHEELHOUSE_FILES or actual != declared:
        raise ReleaseFailure("wheelhouse_manifest_mismatch")
    bootstrap_pins = {name: version for name, version, _filename in BOOTSTRAP_WHEELS}
    if set(runtime_pins) & set(bootstrap_pins):
        raise ReleaseFailure("runtime_lock_contains_bootstrap_pin")
    expected_pins = {**runtime_pins, **bootstrap_pins}
    if actual_pins != expected_pins or any(
        actual_filenames.get(name) != filename for name, _version, filename in BOOTSTRAP_WHEELS
    ):
        raise ReleaseFailure("wheelhouse_pin_mismatch")
    return _sha256_file(manifest)


def _bootstrap_target_pip(target_python: Path, wheelhouse: Path) -> None:
    pip_wheel = wheelhouse / BOOTSTRAP_WHEELS[0][2]
    bootstrap_wheels = [str(wheelhouse / filename) for _name, _version, filename in BOOTSTRAP_WHEELS]
    command = [
        str(target_python),
        "-I",
        "-B",
        f"{pip_wheel}/pip",
        "--isolated",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-index",
        "--no-deps",
        *bootstrap_wheels,
    ]
    try:
        result = subprocess.run(  # noqa: S603
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseFailure("release_pip_bootstrap_failed") from exc
    if result.returncode != 0:
        raise ReleaseFailure("release_pip_bootstrap_failed")


def verify_release_tree(release: ReleaseIdentity) -> None:
    root = release.root.resolve(strict=True)
    root_status = os.stat(root, follow_symlinks=False)
    if (
        root != Path(os.path.abspath(release.root))
        or not stat.S_ISDIR(root_status.st_mode)
        or root_status.st_uid != os.geteuid()
        or stat.S_IMODE(root_status.st_mode) != 0o500
    ):
        raise ReleaseFailure("release_root_not_sealed")
    manifest = _regular_file(
        root / "artifacts/release-tree.sha256",
        maximum_bytes=64 << 20,
        code="release_tree_manifest_invalid",
    )
    raw = manifest.read_bytes()
    if _sha256_bytes(raw) != release.tree_manifest_sha256:
        raise ReleaseFailure("release_tree_manifest_digest_mismatch")
    expected = raw.decode("utf-8").splitlines()
    if expected != _manifest_entries(root):
        raise ReleaseFailure("release_tree_changed")
    manifest_status = os.stat(manifest, follow_symlinks=False)
    if manifest_status.st_uid != os.geteuid() or stat.S_IMODE(manifest_status.st_mode) != 0o400:
        raise ReleaseFailure("release_tree_not_sealed")
    for path in root.rglob("*"):
        status = os.lstat(path)
        if stat.S_ISLNK(status.st_mode):
            try:
                if not path.resolve(strict=True).is_relative_to(root):
                    raise ReleaseFailure("release_tree_external_symlink")
            except OSError as exc:
                raise ReleaseFailure("release_tree_dangling_symlink") from exc
        elif stat.S_ISDIR(status.st_mode):
            if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != 0o500:
                raise ReleaseFailure("release_tree_not_sealed")
        elif stat.S_ISREG(status.st_mode) and (
            status.st_uid != os.geteuid()
            or status.st_nlink != 1
            or stat.S_IMODE(status.st_mode) not in {0o400, 0o500}
        ):
            raise ReleaseFailure("release_tree_not_sealed")
    if release.venv_relocation_contract:
        if release.venv_relocation_contract != VENV_RELOCATION_CONTRACT:
            raise ReleaseFailure("release_venv_relocation_contract_invalid")
        if root.name != release.commit:
            raise ReleaseFailure("release_venv_relocation_identity_mismatch")
        _verify_relocated_venv(
            root,
            bound_root=root,
            forbidden_staging_root=root.parent / f".{release.commit}.",
        )


def _seal_release_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        os.chmod(path, 0o500 if path.is_dir() or os.access(path, os.X_OK) else 0o400)
    os.chmod(root, 0o500)


def _cleanup_staging_tree(staging: Path) -> None:
    """Best-effort two-pass removal that never masks the build failure."""

    with suppress(Exception):
        try:
            staging_status = os.lstat(staging)
        except OSError:
            return
        if not stat.S_ISDIR(staging_status.st_mode):
            with suppress(OSError):
                os.unlink(staging)
            return

        with suppress(OSError):
            os.chmod(staging, 0o700)
        paths: list[Path] = []
        for current, directory_names, file_names in os.walk(
            staging,
            topdown=True,
            onerror=lambda _error: None,
            followlinks=False,
        ):
            current_path = Path(current)
            with suppress(OSError):
                if stat.S_ISDIR(os.lstat(current_path).st_mode):
                    os.chmod(current_path, 0o700)
            for name in (*directory_names, *file_names):
                path = current_path / name
                paths.append(path)
                with suppress(OSError):
                    if stat.S_ISDIR(os.lstat(path).st_mode):
                        os.chmod(path, 0o700)

        for path in sorted(paths, key=lambda item: len(item.parts), reverse=True):
            try:
                status = os.lstat(path)
            except OSError:
                continue
            with suppress(OSError):
                os.rmdir(path) if stat.S_ISDIR(status.st_mode) else os.unlink(path)
        with suppress(OSError):
            os.rmdir(staging)


def build_release(spec: BuildSpec) -> ReleaseIdentity:
    """Build one previously absent sibling release from pinned offline wheels."""

    commit = _closed_commit(spec.commit)
    if _VERSION.fullmatch(spec.version) is None or type(spec.max_schema) is not int or spec.max_schema <= 0:
        raise ReleaseFailure("release_metadata_invalid")
    wheel = _regular_file(spec.wheel, maximum_bytes=MAX_WHEEL_BYTES, code="wheel_invalid")
    if _sha256_file(wheel) != _closed_hash(spec.wheel_sha256, "wheel_digest_invalid"):
        raise ReleaseFailure("wheel_digest_mismatch")
    runtime_lock = _regular_file(
        spec.runtime_lock,
        maximum_bytes=MAX_LOCK_BYTES,
        code="runtime_lock_invalid",
    )
    pins = _runtime_pins(runtime_lock)
    runtime_lock_sha256 = _sha256_file(runtime_lock)
    if runtime_lock_sha256 != _closed_hash(
        spec.runtime_lock_sha256,
        "runtime_lock_digest_invalid",
    ):
        raise ReleaseFailure("runtime_lock_digest_mismatch")
    root = _private_directory(spec.releases_root)
    wheelhouse = _private_directory(spec.wheelhouse)
    wheelhouse_manifest_sha = _verify_wheelhouse(wheelhouse, spec.wheelhouse_manifest, pins)
    if wheelhouse_manifest_sha != _closed_hash(
        spec.wheelhouse_manifest_sha256,
        "wheelhouse_manifest_digest_invalid",
    ):
        raise ReleaseFailure("wheelhouse_manifest_digest_mismatch")
    target = root / commit
    if target.exists() or target.is_symlink():
        raise ReleaseFailure("release_target_exists")
    base_python = _regular_file(spec.base_python, maximum_bytes=1 << 30, code="base_python_invalid")
    if _sha256_file(base_python) != _closed_hash(spec.base_python_sha256, "base_python_digest_invalid"):
        raise ReleaseFailure("base_python_digest_mismatch")
    _preflight_base_python(base_python)
    operator_source = _regular_file(
        Path(__file__),
        maximum_bytes=4 << 20,
        code="operator_source_invalid",
    )
    product_runner_source = spec.secondary_product_runner
    product_runner_lexical = Path(os.path.abspath(product_runner_source))
    source_parts = _SECONDARY_PRODUCT_RUNNER_SOURCE.parts
    if tuple(product_runner_lexical.parts[-len(source_parts) :]) != source_parts:
        raise ReleaseFailure("secondary_product_runner_source_invalid")
    product_runner_bytes = _read_stable_regular_file(
        product_runner_lexical,
        maximum_bytes=4 << 20,
        code="secondary_product_runner_source_invalid",
    )
    product_runner_sha256 = _sha256_bytes(product_runner_bytes)
    if product_runner_sha256 != _closed_hash(
        spec.secondary_product_runner_sha256,
        "secondary_product_runner_digest_invalid",
    ):
        raise ReleaseFailure("secondary_product_runner_digest_mismatch")
    staging = Path(tempfile.mkdtemp(prefix=f".{commit}.", dir=root))
    try:
        _create_pipless_venv(base_python, staging / "venv")
        python = staging / "venv/bin/python"
        _bootstrap_target_pip(python, wheelhouse)
        subprocess.run(  # noqa: S603
            [
                str(python),
                "-I",
                "-B",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "-r",
                str(runtime_lock),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=1_800,
        )
        subprocess.run(  # noqa: S603
            [
                str(python),
                "-I",
                "-B",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--no-deps",
                str(wheel),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=300,
        )
        _installed_pin_smoke(python, pins)
        pip_check = subprocess.run(  # noqa: S603
            [str(python), "-I", "-B", "-m", "pip", "--isolated", "check"],
            check=False,
            capture_output=True,
            timeout=120,
        )
        if pip_check.returncode != 0 or pip_check.stderr:
            raise ReleaseFailure("installed_dependency_check_failed")
        if (
            _sha256_file(wheel) != spec.wheel_sha256
            or _sha256_file(runtime_lock) != runtime_lock_sha256
            or _verify_wheelhouse(wheelhouse, spec.wheelhouse_manifest, pins) != wheelhouse_manifest_sha
            or _sha256_file(base_python) != spec.base_python_sha256
            or _read_stable_regular_file(
                product_runner_lexical,
                maximum_bytes=4 << 20,
                code="secondary_product_runner_source_invalid",
            )
            != product_runner_bytes
        ):
            raise ReleaseFailure("release_build_input_changed")
        artifacts = staging / "artifacts"
        artifacts.mkdir(mode=0o700)
        operator_bytes = operator_source.read_bytes()
        (artifacts / "immutable_release_operator.py").write_bytes(operator_bytes)
        (staging / _SECONDARY_PRODUCT_RUNNER_ARTIFACT).write_bytes(product_runner_bytes)
        alias_tool = _regular_file(spec.alias_tool, maximum_bytes=4 << 20, code="alias_tool_invalid")
        alias_dependency = _regular_file(
            spec.alias_dependency,
            maximum_bytes=4 << 20,
            code="alias_dependency_invalid",
        )
        alias_tool_sha256 = _closed_hash(spec.alias_tool_sha256, "alias_tool_digest_invalid")
        alias_dependency_sha256 = _closed_hash(
            spec.alias_dependency_sha256,
            "alias_dependency_digest_invalid",
        )
        alias_tool_bytes = alias_tool.read_bytes()
        alias_dependency_bytes = alias_dependency.read_bytes()
        if _sha256_bytes(alias_tool_bytes) != alias_tool_sha256:
            raise ReleaseFailure("alias_tool_digest_mismatch")
        if _sha256_bytes(alias_dependency_bytes) != alias_dependency_sha256:
            raise ReleaseFailure("alias_dependency_digest_mismatch")
        release_tools = staging / "tools"
        release_tools.mkdir(mode=0o700)
        (release_tools / "backfill_file_alias_filenames.py").write_bytes(alias_tool_bytes)
        (release_tools / "backfill_telegram_file_aliases.py").write_bytes(alias_dependency_bytes)
        units = render_units(anchor=spec.anchor, env_file=spec.env_file, friday_home=spec.friday_home)
        for name, content in units.items():
            (artifacts / name).write_text(content, encoding="utf-8")
        metadata = {
            "schema": BUILD_RECEIPT_SCHEMA,
            "commit": commit,
            "version": spec.version,
            "max_schema": spec.max_schema,
            "wheel_sha256": spec.wheel_sha256,
            "runtime_lock_sha256": runtime_lock_sha256,
            "runtime_pin_count": len(pins),
            "bootstrap_pins": {name: version for name, version, _filename in BOOTSTRAP_WHEELS},
            "bootstrap_wheel_sha256": {
                name: _sha256_file(wheelhouse / filename) for name, _version, filename in BOOTSTRAP_WHEELS
            },
            "wheelhouse_manifest_sha256": wheelhouse_manifest_sha,
            "base_python_sha256": spec.base_python_sha256,
            "operator_sha256": _sha256_bytes(operator_bytes),
            "secondary_product_runner_sha256": product_runner_sha256,
            "alias_tool_sha256": alias_tool_sha256,
            "alias_dependency_sha256": alias_dependency_sha256,
            "memory_vault_mode_contract": MEMORY_VAULT_MODE_CONTRACT,
            "obsidian_cutover_contract": OBSIDIAN_CUTOVER_CONTRACT,
            "venv_relocation_contract": VENV_RELOCATION_CONTRACT,
        }
        (artifacts / "immutable-release.json").write_bytes(_canonical_json(metadata) + b"\n")
        provisional = ReleaseIdentity(
            staging,
            commit,
            spec.version,
            "0" * 64,
            spec.max_schema,
            MEMORY_VAULT_MODE_CONTRACT,
            VENV_RELOCATION_CONTRACT,
            OBSIDIAN_CUTOVER_CONTRACT,
            product_runner_sha256,
        )
        installed_surface_smoke(provisional)
        _relocate_venv_generated_paths(staging, target)
        for cache in sorted(staging.rglob("__pycache__"), reverse=True):
            for child in cache.iterdir():
                if child.is_file() or child.is_symlink():
                    child.unlink()
            cache.rmdir()
        _verify_relocated_venv(
            staging,
            bound_root=target,
            forbidden_staging_root=staging,
        )
        with _isolated_smoke_environment(staging) as (scratch, environment):
            _activation_smoke(
                physical_root=staging,
                bound_root=target,
                require_interpreter=False,
                scratch=scratch,
                environment=environment,
            )
        _seal_release_tree(staging)
        manifest = staging / "artifacts/release-tree.sha256"
        os.chmod(artifacts, 0o700)
        manifest_bytes = (
            "\n".join(_manifest_entries(staging, mode_overrides={"artifacts": 0o500})) + "\n"
        ).encode("utf-8")
        _write_private_durable(
            manifest,
            manifest_bytes,
            final_mode=0o400,
        )
        os.chmod(artifacts, 0o500)
        os.chmod(staging, 0o500)
        _fsync_tree(staging)
        manifest_sha = _sha256_file(manifest)
        os.replace(staging, target)
        _fsync_directory(root)
        release = ReleaseIdentity(
            target,
            commit,
            spec.version,
            manifest_sha,
            spec.max_schema,
            MEMORY_VAULT_MODE_CONTRACT,
            VENV_RELOCATION_CONTRACT,
            OBSIDIAN_CUTOVER_CONTRACT,
            product_runner_sha256,
        )
        verify_release_tree(release)
        installed_surface_smoke(release)
        return release
    except ReleaseFailure:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseFailure("release_build_failed") from exc
    finally:
        _cleanup_staging_tree(staging)


def _validated_alias_repair_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "applied_count",
        "backup_database_sha256",
        "backup_inbox_sha256",
        "backup_manifest_sha256",
        "plan_sha256",
        "pre_apply_database_sha256",
        "receipt_sha256",
        "schema",
        "status",
        "writer_quiescence_sha256",
    }
    if set(value) != expected_keys or value.get("schema") != ALIAS_REPAIR_RECEIPT_SCHEMA:
        raise ReleaseFailure("alias_repair_receipt_invalid")
    status = value.get("status")
    applied_count = value.get("applied_count")
    if status not in {"clear", "not_requested"} or type(applied_count) is not int or applied_count < 0:
        raise ReleaseFailure("alias_repair_receipt_invalid")
    hashes = {
        key: _closed_hash(str(value.get(key) or ""), "alias_repair_receipt_invalid")
        for key in expected_keys
        if key.endswith("sha256")
    }
    core = {
        "schema": ALIAS_REPAIR_RECEIPT_SCHEMA,
        "status": status,
        "applied_count": applied_count,
        "plan_sha256": hashes["plan_sha256"],
        "backup_manifest_sha256": hashes["backup_manifest_sha256"],
        "backup_database_sha256": hashes["backup_database_sha256"],
        "backup_inbox_sha256": hashes["backup_inbox_sha256"],
        "pre_apply_database_sha256": hashes["pre_apply_database_sha256"],
        "writer_quiescence_sha256": hashes["writer_quiescence_sha256"],
    }
    if hashes["receipt_sha256"] != _sha256_bytes(_canonical_json(core)):
        raise ReleaseFailure("alias_repair_receipt_invalid")
    evidence_hashes = [value_ for key, value_ in hashes.items() if key != "receipt_sha256"]
    if status == "not_requested":
        if applied_count != 0 or any(set(value_) != {"0"} for value_ in evidence_hashes):
            raise ReleaseFailure("alias_repair_receipt_invalid")
    elif applied_count <= 0 or any(set(value_) == {"0"} for value_ in evidence_hashes):
        raise ReleaseFailure("alias_repair_receipt_invalid")
    return {**core, "receipt_sha256": hashes["receipt_sha256"]}


def _staged_config_transition(
    state: Mapping[str, Any],
) -> tuple[str, str, Path, str] | None:
    fields = (
        "prebackup_config_transition",
        "predecessor_env_sha256",
        "next_env_file",
        "next_env_file_sha256",
    )
    present = tuple(field in state for field in fields)
    if not any(present):
        return None
    if present == (True, True, False, False):
        if state.get("prebackup_config_transition") == "" and state.get("predecessor_env_sha256") == "":
            return None
        raise ReleaseFailure("activation_prebackup_transition_invalid")
    if not all(present):
        raise ReleaseFailure("activation_prebackup_transition_invalid")
    transition = state.get("prebackup_config_transition")
    predecessor_env_sha256 = str(state.get("predecessor_env_sha256") or "")
    next_env_file = str(state.get("next_env_file") or "")
    next_env_file_sha256 = str(state.get("next_env_file_sha256") or "")
    if transition == "" and predecessor_env_sha256 == next_env_file == next_env_file_sha256 == "":
        return None
    if transition not in _STAGED_CONFIG_TRANSITIONS:
        raise ReleaseFailure("activation_prebackup_transition_invalid")
    next_path = Path(next_env_file)
    if (
        not next_path.is_absolute()
        or Path(os.path.abspath(next_path)) != next_path
        or any(character in next_env_file for character in "\x00\r\n")
    ):
        raise ReleaseFailure("activation_next_env_path_invalid")
    predecessor_digest = _closed_hash(predecessor_env_sha256, "activation_predecessor_env_digest_invalid")
    next_digest = _closed_hash(next_env_file_sha256, "activation_next_env_digest_invalid")
    if predecessor_digest == next_digest:
        raise ReleaseFailure("activation_environment_transition_not_distinct")
    return transition, predecessor_digest, next_path, next_digest


def activate_release(
    port: ActivationPort,
    journal: ActivationJournalPort,
    *,
    candidate: ReleaseIdentity,
    previous: ReleaseIdentity,
    schema_capable_fallback: ReleaseIdentity,
) -> dict[str, Any]:
    """Backend-first activation with schema-aware exact rollback."""

    state = ActivationState()
    backup: DatabaseBackup | None = None
    alias_repair: Mapping[str, Any] = {}
    if candidate.root in {previous.root, schema_capable_fallback.root} or candidate.commit in {
        previous.commit,
        schema_capable_fallback.commit,
    }:
        raise ReleaseFailure("candidate_rollback_identity_not_distinct")
    if previous.commit in FORBIDDEN_ROLLBACK_COMMITS:
        raise ReleaseFailure("forbidden_corrupt_rollback_release")
    if schema_capable_fallback.max_schema < candidate.max_schema:
        raise ReleaseFailure("schema_capable_fallback_required")
    _require_venv_relocation_contract(
        candidate,
        code="candidate_venv_relocation_contract_missing",
    )
    _require_venv_relocation_contract(
        schema_capable_fallback,
        code="fallback_venv_relocation_contract_missing",
    )
    _require_obsidian_cutover_contract(
        candidate,
        code="candidate_obsidian_cutover_contract_missing",
    )
    _require_obsidian_cutover_contract(
        schema_capable_fallback,
        code="fallback_obsidian_cutover_contract_missing",
    )
    _require_obsidian_cutover_contract(
        previous,
        code="previous_obsidian_cutover_contract_missing",
    )
    port.verify_release(candidate)
    # The predecessor is the currently live binary and must remain provable
    # against ENV0 while an authenticated ENV1 transition is only staged.
    port.verify_release(previous, use_predecessor_config=True)
    if previous.obsidian_cutover_contract == OBSIDIAN_CUTOVER_CONTRACT:
        # A capable predecessor can also be the exact post-backup rollback
        # target, so attest it against ENV1 before quiescing services.
        port.verify_release(previous)
    port.verify_release(schema_capable_fallback)
    port.verify_units(candidate)
    port.verify_active_anchor(previous, candidate)
    journal.begin(
        candidate=candidate,
        previous=previous,
        fallback=schema_capable_fallback,
    )
    staged_transition = _staged_config_transition(journal.load())
    staged_transition_validation_sha256 = ""
    if staged_transition is not None:
        port.validate_staged_config_transition(*staged_transition)
        if staged_transition[0] == _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION:
            staged_transition_validation_sha256 = (
                _staged_transition_validation_sha256(*staged_transition)
            )
    try:
        journal.record(
            "bridge_stop_attempted",
            staged_transition_validation_sha256=(
                staged_transition_validation_sha256
            ),
        )
        port.stop_bridge()
        state.bridge_stopped = True
        journal.record("backend_stop_attempted")
        port.stop_backend()
        state.backend_stopped = True
        if not port.services_inactive():
            raise ReleaseFailure("writers_not_quiesced")
        journal.record("writers_quiesced")
        port.acquire_writer_leases()
        if not port.writer_leases_held():
            raise ReleaseFailure("writer_leases_not_held")
        journal.record("leases_acquired")
        backup = port.backup_database()
        if backup.schema_version > candidate.max_schema:
            raise ReleaseFailure("candidate_schema_too_old")
        _closed_hash(backup.receipt_sha256, "database_backup_invalid")
        _closed_hash(backup.inbox_receipt_sha256, "inbox_backup_invalid")
        _closed_hash(backup.obsidian_receipt_sha256, "obsidian_backup_invalid")
        journal.record("backup_complete", backup=backup)
        if staged_transition is not None:
            journal.record("environment_swap_attempted", backup=backup)
            port.activate_staged_config_transition(*staged_transition)
            journal.record("environment_active", backup=backup)
        journal.record(
            "migration_attempted",
            backup=backup,
            database_mutation_possible=True,
        )
        port.offline_migrate(candidate, backup)
        if not port.writer_leases_held():
            raise ReleaseFailure("offline_migration_lost_writer_leases")
        journal.record(
            "alias_repair_attempted",
            backup=backup,
            database_mutation_possible=True,
        )
        alias_repair = _validated_alias_repair_receipt(port.repair_file_aliases(candidate, backup))
        if not port.writer_leases_held():
            raise ReleaseFailure("alias_repair_lost_writer_leases")
        journal.record(
            "candidate_anchor_attempted",
            backup=backup,
            database_mutation_possible=True,
        )
        port.switch_anchor(candidate)
        state.anchor_switched = True
        journal.record(
            "candidate_anchor_active",
            backup=backup,
            database_mutation_possible=True,
        )
        port.release_writer_leases()
        # Once a network-facing writer start is attempted, its externally
        # visible durable effects are uncertain even if systemd returns an
        # error.  From this boundary rollback must stay schema-capable and may
        # not restore the pre-cutover databases.
        state.candidate_backend_started = True
        journal.record(
            "backend_start_attempted",
            backup=backup,
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="candidate",
        )
        port.start_backend(candidate)
        port.accept_backend(candidate)
        journal.record(
            "backend_accepted",
            backup=backup,
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="candidate",
        )
        state.candidate_bridge_started = True
        journal.record(
            "bridge_start_attempted",
            backup=backup,
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="candidate",
        )
        port.start_bridge(candidate)
        port.accept_bridge(candidate)
        journal.record(
            "bridge_accepted",
            backup=backup,
            database_mutation_possible=True,
            network_writer_uncertain=True,
            writer_target="candidate",
        )
        runtime_policy = dict(port.activation_policy_receipt())
        expected_policy = {
            "memory_vault_cutover_phase": (
                "phase_b_body_free"
                if runtime_policy.get("memory_vault_mode") == "disabled"
                else "phase_a_full_owner_bridge"
            ),
            "memory_vault_mode": runtime_policy.get("memory_vault_mode"),
        }
        if (
            runtime_policy != expected_policy
            or runtime_policy.get("memory_vault_mode") not in MEMORY_VAULT_MODES
        ):
            raise ReleaseFailure("activation_runtime_policy_receipt_invalid")
        receipt = {
            "schema": ACTIVATION_RECEIPT_SCHEMA,
            "status": "clear",
            "candidate_tree_sha256": candidate.tree_manifest_sha256,
            "backup_receipt_sha256": backup.receipt_sha256,
            "inbox_backup_receipt_sha256": backup.inbox_receipt_sha256,
            "obsidian_backup_receipt_sha256": backup.obsidian_receipt_sha256,
            "database_schema_before": backup.schema_version,
            "alias_repair": dict(alias_repair),
            "runtime_policy": runtime_policy,
            "backend_accepted": True,
            "bridge_accepted": True,
        }
        receipt_sha256 = _sha256_bytes(_canonical_json(receipt))
        journal.record(
            "clear",
            backup=backup,
            database_mutation_possible=True,
            network_writer_uncertain=True,
            terminal_receipt_sha256=receipt_sha256,
        )
        return {**receipt, "receipt_sha256": receipt_sha256}
    except BaseException as original:
        try:
            journal.record(
                "rollback_stop_attempted",
                backup=backup,
                database_mutation_possible=backup is not None,
                network_writer_uncertain=state.candidate_backend_started,
            )
            port.stop_bridge()
            port.stop_backend()
            if not port.services_inactive():
                raise ReleaseFailure("rollback_writers_not_quiesced")
            if backup is None:
                # No DB/inbox mutation was authorized and canonical ENV0 was
                # never replaced.  Prove that invariant before admitting the
                # predecessor writer.
                port.acquire_writer_leases()
                if not port.writer_leases_held():
                    raise ReleaseFailure("rollback_writer_leases_not_held")
                if staged_transition is not None:
                    port.select_predecessor_config_transition(*staged_transition)
                rollback = previous
            else:
                port.acquire_writer_leases()
                if not port.writer_leases_held():
                    raise ReleaseFailure("rollback_writer_leases_not_held")
                if staged_transition is not None:
                    # Once a verified backup exists, all subsequent writers use
                    # ENV1.  This is idempotent if replacement completed before
                    # an abrupt interruption.
                    port.activate_staged_config_transition(*staged_transition)
                if (
                    not state.candidate_backend_started
                    and not state.candidate_bridge_started
                    and backup.schema_version <= previous.max_schema
                ):
                    # No bridge delivery was admitted.  Exact backup restore makes
                    # a pre-schema release safe even if backend startup migrated.
                    journal.record(
                        "rollback_restore_attempted",
                        backup=backup,
                        database_mutation_possible=True,
                    )
                    port.restore_database(backup)
                    rollback = (
                        previous
                        if staged_transition is None
                        or previous.obsidian_cutover_contract == OBSIDIAN_CUTOVER_CONTRACT
                        else schema_capable_fallback
                    )
                else:
                    # Once bridge admission can have written schema-34 state, never
                    # put a schema-33 binary over it.  No implicit lossy DB restore.
                    rollback = schema_capable_fallback
            journal.record(
                "rollback_anchor_attempted",
                backup=backup,
                database_mutation_possible=backup is not None,
                network_writer_uncertain=state.candidate_backend_started,
            )
            port.switch_anchor(rollback)
            port.release_writer_leases()
            journal.record(
                "rollback_backend_start_attempted",
                backup=backup,
                database_mutation_possible=backup is not None,
                network_writer_uncertain=True,
                writer_target=("previous" if rollback is previous else "fallback"),
            )
            port.start_backend(rollback)
            port.accept_backend(rollback)
            journal.record(
                "rollback_backend_accepted",
                backup=backup,
                database_mutation_possible=backup is not None,
                network_writer_uncertain=True,
                writer_target=("previous" if rollback is previous else "fallback"),
            )
            journal.record(
                "rollback_bridge_start_attempted",
                backup=backup,
                database_mutation_possible=backup is not None,
                network_writer_uncertain=True,
                writer_target=("previous" if rollback is previous else "fallback"),
            )
            port.start_bridge(rollback)
            port.accept_bridge(rollback)
            rollback_receipt = _sha256_bytes(
                _canonical_json(
                    {
                        "status": "rolled_back",
                        "rollback_tree_sha256": rollback.tree_manifest_sha256,
                    }
                )
            )
            journal.record(
                "rolled_back",
                backup=backup,
                database_mutation_possible=backup is not None,
                network_writer_uncertain=True,
                writer_target=("previous" if rollback is previous else "fallback"),
                terminal_receipt_sha256=rollback_receipt,
            )
        except BaseException as rollback_error:
            with suppress(BaseException):
                port.release_writer_leases()
            raise ReleaseFailure("activation_and_exact_rollback_failed") from rollback_error
        raise ReleaseFailure("activation_failed_rolled_back") from original


def recover_interrupted_activation(
    port: ActivationPort,
    journal: ActivationJournalPort,
) -> dict[str, Any]:
    """Resume one fsync'd cutover to a safe exact release after process or host death."""

    state = dict(journal.load())
    phase = str(state.get("phase") or "")
    if phase in _TERMINAL_JOURNAL_PHASES:
        terminal_receipt = {
            "schema": ACTIVATION_RECEIPT_SCHEMA,
            "status": "already_terminal",
            "terminal_phase": phase,
            "terminal_receipt_sha256": str(state.get("terminal_receipt_sha256") or ""),
        }
        return {
            **terminal_receipt,
            "receipt_sha256": _sha256_bytes(_canonical_json(terminal_receipt)),
        }
    candidate, previous, fallback = journal.release_identities()
    if candidate.root in {previous.root, fallback.root} or candidate.commit in {
        previous.commit,
        fallback.commit,
    }:
        raise ReleaseFailure("journal_release_identity_not_distinct")
    if previous.commit in FORBIDDEN_ROLLBACK_COMMITS or fallback.max_schema < candidate.max_schema:
        raise ReleaseFailure("journal_rollback_identity_invalid")
    _require_venv_relocation_contract(
        candidate,
        code="recovery_candidate_venv_relocation_contract_missing",
    )
    _require_venv_relocation_contract(
        fallback,
        code="recovery_fallback_venv_relocation_contract_missing",
    )
    _require_obsidian_cutover_contract(
        candidate,
        code="recovery_candidate_obsidian_cutover_contract_missing",
    )
    _require_obsidian_cutover_contract(
        fallback,
        code="recovery_fallback_obsidian_cutover_contract_missing",
    )
    _require_obsidian_cutover_contract(
        previous,
        code="recovery_previous_obsidian_cutover_contract_missing",
    )
    backup = journal.database_backup()
    staged_transition = _staged_config_transition(state)
    port.verify_release(candidate)
    if staged_transition is None:
        port.verify_release(previous)
    elif backup is None:
        port.verify_release(previous, use_predecessor_config=True)
    elif previous.obsidian_cutover_contract == OBSIDIAN_CUTOVER_CONTRACT:
        port.verify_release(previous)
    port.verify_release(fallback)
    port.verify_units(candidate)
    network_uncertain = state.get("network_writer_uncertain") is True
    database_mutation_possible = state.get("database_mutation_possible") is True
    writer_target = str(state.get("writer_target") or "")
    if (
        staged_transition is not None
        and backup is None
        and (database_mutation_possible or writer_target not in {"", "previous"})
    ):
        raise ReleaseFailure("recovery_prebackup_transition_invalid")
    journal.record(
        "recovery_stop_attempted",
        backup=backup,
        database_mutation_possible=database_mutation_possible,
        network_writer_uncertain=network_uncertain,
        writer_target=writer_target,
    )
    port.stop_bridge()
    port.stop_backend()
    if not port.services_inactive():
        raise ReleaseFailure("recovery_writers_not_quiesced")
    port.acquire_writer_leases()
    if not port.writer_leases_held():
        raise ReleaseFailure("recovery_writer_leases_not_held")
    if staged_transition is not None:
        if backup is None:
            port.select_predecessor_config_transition(*staged_transition)
        else:
            # A durable verified backup is the one-way configuration boundary:
            # recovery must converge ENV0/ENV1 to ENV1 before any writer start.
            port.activate_staged_config_transition(*staged_transition)
    target: ReleaseIdentity
    if network_uncertain:
        # A previously started clean previous release may retain its exact DB.
        # Candidate/fallback writers require the schema-capable fallback.
        target = previous if writer_target == "previous" else fallback
    elif database_mutation_possible:
        if backup is None:
            raise ReleaseFailure("recovery_backup_required")
        journal.record(
            "recovery_restore_attempted",
            backup=backup,
            database_mutation_possible=True,
        )
        port.restore_database(backup)
        target = previous
    else:
        target = previous
    if (
        staged_transition is not None
        and backup is not None
        and target is previous
        and previous.obsidian_cutover_contract != OBSIDIAN_CUTOVER_CONTRACT
    ):
        target = fallback
    journal.record(
        "recovery_anchor_attempted",
        backup=backup,
        database_mutation_possible=database_mutation_possible,
        network_writer_uncertain=network_uncertain,
        writer_target=("previous" if target is previous else "fallback"),
    )
    port.switch_anchor(target)
    port.release_writer_leases()
    target_name = "previous" if target is previous else "fallback"
    journal.record(
        "recovery_backend_start_attempted",
        backup=backup,
        database_mutation_possible=database_mutation_possible,
        network_writer_uncertain=True,
        writer_target=target_name,
    )
    port.start_backend(target)
    port.accept_backend(target)
    journal.record(
        "recovery_backend_accepted",
        backup=backup,
        database_mutation_possible=database_mutation_possible,
        network_writer_uncertain=True,
        writer_target=target_name,
    )
    journal.record(
        "recovery_bridge_start_attempted",
        backup=backup,
        database_mutation_possible=database_mutation_possible,
        network_writer_uncertain=True,
        writer_target=target_name,
    )
    port.start_bridge(target)
    port.accept_bridge(target)
    receipt: dict[str, Any] = {
        "schema": ACTIVATION_RECEIPT_SCHEMA,
        "status": "recovered",
        "recovered_tree_sha256": target.tree_manifest_sha256,
        "backup_restored": bool(not network_uncertain and database_mutation_possible),
        "network_writer_uncertain": network_uncertain,
    }
    receipt_sha256 = _sha256_bytes(_canonical_json(receipt))
    journal.record(
        "recovered",
        backup=backup,
        database_mutation_possible=database_mutation_possible,
        network_writer_uncertain=True,
        writer_target=target_name,
        terminal_receipt_sha256=receipt_sha256,
    )
    return {**receipt, "receipt_sha256": receipt_sha256}


@dataclass(frozen=True)
class SystemdConfig:
    anchor: Path
    env_file: Path
    env_file_sha256: str
    friday_home: Path
    unit_dir: Path
    database: Path
    inbox_database: Path
    backup_dir: Path
    state_dir: Path
    health_ca: Path
    health_ca_sha256: str
    alias_claim_manifests: tuple[Path, ...] = ()
    alias_expected_counts: tuple[int, ...] = ()
    alias_expected_plan_sha256s: tuple[str, ...] = ()
    memory_vault_mode: str = "disabled"
    obsidian_mode: str = "disabled"
    obsidian_root: Path | None = None
    next_env_file: Path | None = None
    next_env_file_sha256: str = ""
    staged_config_transition: str = ""
    staged_transition_validation_sha256: str = ""
    secondary_rollout_receipt: Path | None = None
    secondary_rollout_receipt_sha256: str = ""
    health_url: str = "https://127.0.0.1:8000/api/health"
    backend_unit: str = "friday-backend.service"
    bridge_unit: str = "friday-bridge.service"


def _obsidian_root(config: SystemdConfig) -> Path:
    configured = config.obsidian_root or (config.friday_home / "data" / "obsidian")
    return Path(os.path.abspath(configured))


def _obsidian_root_sha256(config: SystemdConfig) -> str:
    return _sha256_bytes(str(_obsidian_root(config)).encode("utf-8"))


def _requested_staged_config_transition(config: SystemdConfig) -> str:
    if config.staged_config_transition:
        if config.staged_config_transition not in _STAGED_CONFIG_TRANSITIONS:
            raise ReleaseFailure("staged_config_transition_invalid")
        return config.staged_config_transition
    if config.next_env_file is not None or config.next_env_file_sha256:
        # Preserve the established Obsidian CLI/programmatic contract.  The new
        # secondary transition must always be selected explicitly.
        return _OBSIDIAN_ENABLE_TRANSITION
    return ""


def _secondary_rollout_receipt_stage(config: SystemdConfig) -> str | None:
    """Require one exact automatic predecessor-stage receipt only for promotion."""

    transition = _requested_staged_config_transition(config)
    expected_stage = _SECONDARY_ROLLOUT_RECEIPT_STAGE.get(transition)
    has_path = config.secondary_rollout_receipt is not None
    has_digest = bool(config.secondary_rollout_receipt_sha256)
    if expected_stage is None:
        if has_path or has_digest:
            raise ReleaseFailure("secondary_rollout_receipt_not_permitted")
        return None
    if not has_path or not has_digest:
        raise ReleaseFailure("secondary_rollout_receipt_required")
    assert config.secondary_rollout_receipt is not None
    lexical = Path(os.path.abspath(config.secondary_rollout_receipt))
    if (
        not config.secondary_rollout_receipt.is_absolute()
        or lexical != config.secondary_rollout_receipt
        or any(character in str(lexical) for character in "\x00\r\n")
    ):
        raise ReleaseFailure("secondary_rollout_receipt_path_invalid")
    _closed_hash(
        config.secondary_rollout_receipt_sha256,
        "secondary_rollout_receipt_digest_invalid",
    )
    return expected_stage


def _activation_target_config(config: SystemdConfig) -> SystemdConfig:
    """Return the exact post-backup runtime identity for a staged activation."""

    has_path = config.next_env_file is not None
    has_digest = bool(config.next_env_file_sha256)
    if has_path != has_digest:
        raise ReleaseFailure("next_environment_arguments_incomplete")
    if bool(config.staged_config_transition) and not has_path:
        raise ReleaseFailure("next_environment_arguments_incomplete")
    if not has_path:
        return config
    _requested_staged_config_transition(config)
    return replace(
        config,
        env_file_sha256=_closed_hash(
            config.next_env_file_sha256,
            "next_environment_file_digest_invalid",
        ),
        next_env_file=None,
        next_env_file_sha256="",
        staged_config_transition="",
        staged_transition_validation_sha256="",
        secondary_rollout_receipt=None,
        secondary_rollout_receipt_sha256="",
    )


def _activation_predecessor_config(config: SystemdConfig) -> SystemdConfig:
    """Return the exact live ENV0 identity while ENV1 is staged."""

    if config.next_env_file is None or not config.next_env_file_sha256:
        return config
    transition = _requested_staged_config_transition(config)
    changes: dict[str, Any] = {
        "next_env_file": None,
        "next_env_file_sha256": "",
        "staged_config_transition": "",
        "staged_transition_validation_sha256": "",
        "secondary_rollout_receipt": None,
        "secondary_rollout_receipt_sha256": "",
    }
    if transition == _OBSIDIAN_ENABLE_TRANSITION:
        changes["obsidian_mode"] = "disabled"
    return replace(config, **changes)


def _systemd_config_identity_v2(config: SystemdConfig) -> str:
    """Exact identity emitted by the pre-Obsidian release operator."""

    return _sha256_bytes(
        _canonical_json(
            {
                "schema": RUNTIME_CONFIG_SCHEMA_V2,
                "alias_claim_manifests": [str(path) for path in config.alias_claim_manifests],
                "alias_expected_counts": list(config.alias_expected_counts),
                "alias_expected_plan_sha256s": list(config.alias_expected_plan_sha256s),
                "anchor": str(config.anchor),
                "backup_dir": str(config.backup_dir),
                "database": str(config.database),
                "env_file": str(config.env_file),
                "env_file_sha256": config.env_file_sha256,
                "friday_home": str(config.friday_home),
                "health_ca": str(config.health_ca),
                "health_ca_sha256": config.health_ca_sha256,
                "health_url": config.health_url,
                "inbox_database": str(config.inbox_database),
                "memory_vault_mode": config.memory_vault_mode,
                "state_dir": str(config.state_dir),
                "unit_dir": str(config.unit_dir),
                "unit_names": [config.backend_unit, config.bridge_unit],
            }
        )
    )


def _systemd_config_identity(config: SystemdConfig) -> str:
    return _sha256_bytes(
        _canonical_json(
            {
                "schema": RUNTIME_CONFIG_SCHEMA_V3,
                "alias_claim_manifests": [str(path) for path in config.alias_claim_manifests],
                "alias_expected_counts": list(config.alias_expected_counts),
                "alias_expected_plan_sha256s": list(config.alias_expected_plan_sha256s),
                "anchor": str(config.anchor),
                "backup_dir": str(config.backup_dir),
                "database": str(config.database),
                "env_file": str(config.env_file),
                "env_file_sha256": config.env_file_sha256,
                "friday_home": str(config.friday_home),
                "health_ca": str(config.health_ca),
                "health_ca_sha256": config.health_ca_sha256,
                "health_url": config.health_url,
                "inbox_database": str(config.inbox_database),
                "memory_vault_mode": config.memory_vault_mode,
                "obsidian_mode": config.obsidian_mode,
                "obsidian_root": str(_obsidian_root(config)),
                "state_dir": str(config.state_dir),
                "unit_dir": str(config.unit_dir),
                "unit_names": [config.backend_unit, config.bridge_unit],
            }
        )
    )


def _systemd_config_identity_v1(config: SystemdConfig) -> str:
    """Exact pre-mode identity accepted only to supersede a terminal journal."""

    return _sha256_bytes(
        _canonical_json(
            {
                "schema": RUNTIME_CONFIG_SCHEMA_V1,
                "anchor": str(config.anchor),
                "database": str(config.database),
                "env_file_sha256": config.env_file_sha256,
                "friday_home": str(config.friday_home),
                "inbox_database": str(config.inbox_database),
                "state_dir": str(config.state_dir),
                "unit_names": [config.backend_unit, config.bridge_unit],
            }
        )
    )


def _activation_legacy_config_identity(
    config: SystemdConfig,
    terminal_journal_env_sha256: str | None,
) -> str:
    """Bind a terminal legacy journal to the explicitly attested pre-edit env."""

    predecessor_env_sha256 = _closed_hash(
        terminal_journal_env_sha256 or config.env_file_sha256,
        "terminal_journal_env_digest_invalid",
    )
    return _systemd_config_identity_v1(replace(config, env_file_sha256=predecessor_env_sha256))


def _systemd_config_scope_identity(config: SystemdConfig) -> str:
    """Persistent identity stable across the authenticated two-phase cutover.

    Per-run alias claims stay in each phase's full config identity, but are
    intentionally absent here: Phase A consumes them exactly once and Phase B
    must omit them after the database mutation has committed.
    """

    return _sha256_bytes(
        _canonical_json(
            {
                "schema": RUNTIME_CONFIG_SCOPE_SCHEMA,
                "anchor": str(config.anchor),
                "backup_dir": str(config.backup_dir),
                "database": str(config.database),
                "env_file": str(config.env_file),
                "friday_home": str(config.friday_home),
                "health_ca": str(config.health_ca),
                "health_ca_sha256": config.health_ca_sha256,
                "health_url": config.health_url,
                "inbox_database": str(config.inbox_database),
                "obsidian_root": str(_obsidian_root(config)),
                "state_dir": str(config.state_dir),
                "unit_dir": str(config.unit_dir),
                "unit_names": [config.backend_unit, config.bridge_unit],
            }
        )
    )


def _systemd_config_retry_scope_identity(config: SystemdConfig) -> str:
    """Bind every config input except Phase-A's consumed one-shot alias claims."""

    return _sha256_bytes(
        _canonical_json(
            {
                "schema": RUNTIME_CONFIG_RETRY_SCOPE_SCHEMA,
                "anchor": str(config.anchor),
                "backup_dir": str(config.backup_dir),
                "database": str(config.database),
                "env_file": str(config.env_file),
                "env_file_sha256": config.env_file_sha256,
                "friday_home": str(config.friday_home),
                "health_ca": str(config.health_ca),
                "health_ca_sha256": config.health_ca_sha256,
                "health_url": config.health_url,
                "inbox_database": str(config.inbox_database),
                "memory_vault_mode": config.memory_vault_mode,
                "obsidian_mode": config.obsidian_mode,
                "obsidian_root": str(_obsidian_root(config)),
                "state_dir": str(config.state_dir),
                "unit_dir": str(config.unit_dir),
                "unit_names": [config.backend_unit, config.bridge_unit],
            }
        )
    )


def _activation_v2_config_identity(
    config: SystemdConfig,
    terminal_journal_env_sha256: str | None,
) -> str:
    predecessor_env_sha256 = _closed_hash(
        terminal_journal_env_sha256 or config.env_file_sha256,
        "terminal_journal_env_digest_invalid",
    )
    return _systemd_config_identity_v2(replace(config, env_file_sha256=predecessor_env_sha256))


def _activation_transition_predecessor_identity(
    config: SystemdConfig,
    terminal_journal_env_sha256: str | None,
    transition: str,
) -> str:
    if terminal_journal_env_sha256 is None:
        return ""
    if transition not in _STAGED_CONFIG_TRANSITIONS:
        raise ReleaseFailure("staged_config_transition_invalid")
    predecessor_env_sha256 = _closed_hash(
        terminal_journal_env_sha256,
        "terminal_journal_env_digest_invalid",
    )
    predecessor = replace(config, env_file_sha256=predecessor_env_sha256)
    if transition == _OBSIDIAN_ENABLE_TRANSITION:
        predecessor = replace(predecessor, obsidian_mode="disabled")
    return _systemd_config_identity(predecessor)


def _activation_obsidian_predecessor_identity(
    config: SystemdConfig,
    terminal_journal_env_sha256: str | None,
) -> str:
    return _activation_transition_predecessor_identity(
        config,
        terminal_journal_env_sha256,
        _OBSIDIAN_ENABLE_TRANSITION,
    )


@dataclass(frozen=True)
class _ExactObsidianBackup:
    present: bool
    manifest_sha256: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class _ExactBackupPayload:
    directory: Path
    files: tuple[tuple[str, str, int], ...]
    obsidian: _ExactObsidianBackup | None = None


@dataclass(frozen=True)
class _ExactInboxBackup:
    directory: Path
    receipt_sha256: str


def _journal_release(release: ReleaseIdentity) -> dict[str, Any]:
    return {
        "commit": release.commit,
        "max_schema": release.max_schema,
        "root": str(release.root),
        "tree_manifest_sha256": release.tree_manifest_sha256,
        "version": release.version,
    }


def _validate_journal_release_record(value: Any, *, code: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "commit",
        "max_schema",
        "root",
        "tree_manifest_sha256",
        "version",
    }:
        raise ReleaseFailure(code)
    _closed_commit(str(value.get("commit") or ""))
    _closed_hash(str(value.get("tree_manifest_sha256") or ""), code)
    root = str(value.get("root") or "")
    if (
        type(value.get("max_schema")) is not int
        or int(value["max_schema"]) <= 0
        or _VERSION.fullmatch(str(value.get("version") or "")) is None
        or not Path(root).is_absolute()
        or any(character in root for character in "\x00\r\n")
    ):
        raise ReleaseFailure(code)


class DurableActivationJournal:
    """Private fsync'd recovery boundary for every cutover phase."""

    def __init__(
        self,
        path: Path,
        *,
        backup_root: Path,
        config_identity_sha256: str | None,
        legacy_config_identity_sha256: str | None = None,
        legacy_v2_config_identity_sha256: str | None = None,
        transition_config_identity_sha256: str | None = None,
        config_scope_sha256: str | None = None,
        config_retry_scope_sha256: str | None = None,
        alias_claim_count: int = 0,
        memory_vault_mode: str = "disabled",
        obsidian_mode: str = "disabled",
        obsidian_root_sha256: str | None = None,
        predecessor_env_sha256: str | None = None,
        next_env_file: Path | None = None,
        next_env_file_sha256: str | None = None,
        staged_config_transition: str | None = None,
    ) -> None:
        parent = _private_directory(path.parent)
        lexical = Path(os.path.abspath(path))
        if lexical.parent != parent or lexical.name != "immutable-release-activation.v1.json":
            raise ReleaseFailure("activation_journal_path_invalid")
        self.path = lexical
        self.backup_root = _private_directory(backup_root, create=True)
        self.config_identity_sha256 = (
            _closed_hash(config_identity_sha256, "activation_config_identity_invalid")
            if config_identity_sha256 is not None
            else ""
        )
        self.legacy_config_identity_sha256 = (
            _closed_hash(
                legacy_config_identity_sha256,
                "activation_legacy_config_identity_invalid",
            )
            if legacy_config_identity_sha256 is not None
            else ""
        )
        self.legacy_v2_config_identity_sha256 = (
            _closed_hash(
                legacy_v2_config_identity_sha256,
                "activation_legacy_v2_config_identity_invalid",
            )
            if legacy_v2_config_identity_sha256 is not None
            else ""
        )
        self.transition_config_identity_sha256 = (
            _closed_hash(
                transition_config_identity_sha256,
                "activation_transition_config_identity_invalid",
            )
            if transition_config_identity_sha256
            else ""
        )
        if config_scope_sha256 is not None:
            self.config_scope_sha256 = _closed_hash(
                config_scope_sha256,
                "activation_config_scope_invalid",
            )
        elif self.config_identity_sha256:
            # Direct journal users that predate the explicit scope parameter still
            # get a closed identity.  Production callers always pass the real
            # non-env configuration scope below.
            self.config_scope_sha256 = self.config_identity_sha256
        else:
            # ``install-units`` only inspects an existing journal for unfinished
            # work; it is not authorised to begin or supersede an activation.
            self.config_scope_sha256 = ""
        if config_retry_scope_sha256 is not None:
            self.config_retry_scope_sha256 = _closed_hash(
                config_retry_scope_sha256,
                "activation_config_retry_scope_invalid",
            )
        elif self.config_identity_sha256:
            self.config_retry_scope_sha256 = self.config_identity_sha256
        else:
            self.config_retry_scope_sha256 = ""
        if type(alias_claim_count) is not int or not 0 <= alias_claim_count <= 64:
            raise ReleaseFailure("activation_alias_claim_count_invalid")
        self.alias_claim_count = alias_claim_count
        if memory_vault_mode not in MEMORY_VAULT_MODES:
            raise ReleaseFailure("activation_memory_vault_mode_invalid")
        self.memory_vault_mode = memory_vault_mode
        if obsidian_mode not in OBSIDIAN_MODES:
            raise ReleaseFailure("activation_obsidian_mode_invalid")
        self.obsidian_mode = obsidian_mode
        self.obsidian_root_sha256 = _closed_hash(
            obsidian_root_sha256 or ("0" * 64),
            "activation_obsidian_root_digest_invalid",
        )
        self.predecessor_env_sha256 = (
            _closed_hash(
                predecessor_env_sha256,
                "activation_predecessor_env_digest_invalid",
            )
            if predecessor_env_sha256
            else ""
        )
        self.next_env_file = ""
        if next_env_file is not None:
            lexical_next = Path(os.path.abspath(next_env_file))
            raw_next = str(next_env_file)
            if (
                not next_env_file.is_absolute()
                or lexical_next != next_env_file
                or lexical_next.parent != parent
                or any(character in raw_next for character in "\x00\r\n")
            ):
                raise ReleaseFailure("activation_next_env_path_invalid")
            self.next_env_file = raw_next
        self.next_env_file_sha256 = (
            _closed_hash(
                next_env_file_sha256,
                "activation_next_env_digest_invalid",
            )
            if next_env_file_sha256
            else ""
        )
        staged_fields = (
            bool(self.predecessor_env_sha256),
            bool(self.next_env_file),
            bool(self.next_env_file_sha256),
        )
        self.staged_config_transition = staged_config_transition or (
            _OBSIDIAN_ENABLE_TRANSITION if all(staged_fields) else ""
        )
        if self.staged_config_transition and self.staged_config_transition not in _STAGED_CONFIG_TRANSITIONS:
            raise ReleaseFailure("activation_staged_environment_policy_invalid")
        if any(staged_fields) and not all(staged_fields):
            raise ReleaseFailure("activation_staged_environment_incomplete")
        if bool(self.staged_config_transition) != all(staged_fields):
            raise ReleaseFailure("activation_staged_environment_incomplete")
        if all(staged_fields):
            if self.predecessor_env_sha256 == self.next_env_file_sha256:
                raise ReleaseFailure("activation_environment_transition_not_distinct")
            if self.alias_claim_count or (
                self.staged_config_transition == _OBSIDIAN_ENABLE_TRANSITION
                and self.obsidian_mode != "enabled"
            ):
                raise ReleaseFailure("activation_staged_environment_policy_invalid")
        self._accepted_terminal_transition = ""
        self._state: dict[str, Any] | None = None

    def _write(self, core: Mapping[str, Any]) -> None:
        payload = {**core, "journal_sha256": _sha256_bytes(_canonical_json(core))}
        raw = _canonical_json(payload) + b"\n"
        temporary = self.path.parent / f".{self.path.name}.{os.getpid()}.{os.urandom(8).hex()}.new"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            _write_all(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            _fsync_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        self._state = dict(core)

    def _read(
        self,
        *,
        allow_terminal_config_transition: bool = False,
        transition_previous: ReleaseIdentity | None = None,
        transition_fallback: ReleaseIdentity | None = None,
        transition_candidate: ReleaseIdentity | None = None,
    ) -> dict[str, Any]:
        path = _private_regular_file(
            self.path,
            maximum_bytes=1 << 20,
            code="activation_journal_invalid",
        )
        try:
            payload = _unique_json(path.read_text(encoding="ascii"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseFailure("activation_journal_invalid") from exc
        legacy_expected = {
            "backup",
            "candidate",
            "config_identity_sha256",
            "database_mutation_possible",
            "fallback",
            "journal_sha256",
            "network_writer_uncertain",
            "phase",
            "previous",
            "schema",
            "terminal_receipt_sha256",
            "transaction_id",
            "writer_target",
        }
        schema_v2_expected = legacy_expected | {"config_identity_schema"}
        v2_current_expected = schema_v2_expected | {
            "alias_claim_count",
            "config_scope_sha256",
            "config_retry_scope_sha256",
            "memory_vault_mode",
        }
        v3_legacy_expected = v2_current_expected | {"obsidian_mode", "obsidian_root_sha256"}
        v3_predecessor_expected = v3_legacy_expected | {
            "prebackup_config_transition",
            "predecessor_env_sha256",
        }
        current_expected = v3_predecessor_expected | {
            "next_env_file",
            "next_env_file_sha256",
        }
        validation_expected = current_expected | {
            "staged_transition_validation_sha256"
        }
        current_transition_expected = (current_expected, validation_expected)
        v3_expected = (
            v3_legacy_expected,
            v3_predecessor_expected,
            *current_transition_expected,
        )
        payload_keys = set(payload)
        if payload_keys not in (
            legacy_expected,
            schema_v2_expected,
            v2_current_expected,
            *v3_expected,
        ):
            raise ReleaseFailure("activation_journal_invalid")
        supplied = str(payload.pop("journal_sha256") or "")
        if supplied != _sha256_bytes(_canonical_json(payload)):
            raise ReleaseFailure("activation_journal_digest_mismatch")
        if (
            payload.get("schema") != ACTIVATION_JOURNAL_SCHEMA
            or payload.get("phase") not in _JOURNAL_PHASES
            or _HEX64.fullmatch(str(payload.get("transaction_id") or "")) is None
            or type(payload.get("database_mutation_possible")) is not bool
            or type(payload.get("network_writer_uncertain")) is not bool
            or payload.get("writer_target") not in {"", "candidate", "previous", "fallback"}
            or _HEX64.fullmatch(str(payload.get("config_identity_sha256") or "")) is None
            or (
                payload_keys in (schema_v2_expected, v2_current_expected)
                and payload.get("config_identity_schema") != RUNTIME_CONFIG_SCHEMA_V2
            )
            or (
                payload_keys in v3_expected
                and payload.get("config_identity_schema") != RUNTIME_CONFIG_SCHEMA_V3
            )
            or (
                payload_keys in (v2_current_expected, *v3_expected)
                and (
                    _HEX64.fullmatch(str(payload.get("config_scope_sha256") or "")) is None
                    or _HEX64.fullmatch(str(payload.get("config_retry_scope_sha256") or "")) is None
                    or type(payload.get("alias_claim_count")) is not int
                    or not 0 <= int(payload["alias_claim_count"]) <= 64
                    or payload.get("memory_vault_mode") not in MEMORY_VAULT_MODES
                )
            )
            or (
                payload_keys in v3_expected
                and (
                    payload.get("obsidian_mode") not in OBSIDIAN_MODES
                    or _HEX64.fullmatch(str(payload.get("obsidian_root_sha256") or "")) is None
                )
            )
            or (
                payload_keys == v3_predecessor_expected
                and (
                    payload.get("prebackup_config_transition") != ""
                    or payload.get("predecessor_env_sha256") != ""
                )
            )
            or (
                payload_keys in current_transition_expected
                and (
                    payload.get("prebackup_config_transition") not in {"", *_STAGED_CONFIG_TRANSITIONS}
                    or (
                        payload.get("prebackup_config_transition") == ""
                        and any(
                            payload.get(key) != ""
                            for key in (
                                "predecessor_env_sha256",
                                "next_env_file",
                                "next_env_file_sha256",
                            )
                        )
                    )
                    or (
                        payload.get("prebackup_config_transition") in _STAGED_CONFIG_TRANSITIONS
                        and (
                            _HEX64.fullmatch(str(payload.get("predecessor_env_sha256") or "")) is None
                            or _HEX64.fullmatch(str(payload.get("next_env_file_sha256") or "")) is None
                            or payload.get("predecessor_env_sha256") == payload.get("next_env_file_sha256")
                            or not isinstance(payload.get("next_env_file"), str)
                            or not Path(str(payload.get("next_env_file"))).is_absolute()
                            or Path(os.path.abspath(str(payload.get("next_env_file"))))
                            != Path(str(payload.get("next_env_file")))
                            or Path(str(payload.get("next_env_file"))).parent != self.path.parent
                            or any(character in str(payload.get("next_env_file")) for character in "\x00\r\n")
                            or (
                                payload.get("prebackup_config_transition") == _OBSIDIAN_ENABLE_TRANSITION
                                and payload.get("obsidian_mode") != "enabled"
                            )
                            or int(payload.get("alias_claim_count") or 0) != 0
                        )
                    )
                )
            )
            or (
                payload_keys == validation_expected
                and (
                    payload.get("prebackup_config_transition")
                    != _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION
                    or payload.get("phase") == "prepared"
                    or payload.get("staged_transition_validation_sha256")
                    != _staged_transition_validation_sha256(
                        str(payload.get("prebackup_config_transition") or ""),
                        str(payload.get("predecessor_env_sha256") or ""),
                        Path(str(payload.get("next_env_file") or "")),
                        str(payload.get("next_env_file_sha256") or ""),
                    )
                )
            )
            or (
                payload_keys == current_expected
                and payload.get("prebackup_config_transition")
                == _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION
                and payload.get("phase") != "prepared"
            )
            or (
                payload.get("phase") in {"environment_swap_attempted", "environment_active"}
                and (
                    payload_keys not in current_transition_expected
                    or payload.get("prebackup_config_transition") not in _STAGED_CONFIG_TRANSITIONS
                    or payload.get("backup") is None
                )
            )
        ):
            raise ReleaseFailure("activation_journal_invalid")
        terminal_hash = str(payload.get("terminal_receipt_sha256") or "")
        if (payload["phase"] in _TERMINAL_JOURNAL_PHASES) != (_HEX64.fullmatch(terminal_hash) is not None):
            raise ReleaseFailure("activation_journal_invalid")
        for key in ("candidate", "previous", "fallback"):
            identity = payload.get(key)
            if not isinstance(identity, dict) or set(identity) != {
                "commit",
                "max_schema",
                "root",
                "tree_manifest_sha256",
                "version",
            }:
                raise ReleaseFailure("activation_journal_invalid")
            _closed_commit(str(identity.get("commit") or ""))
            _closed_hash(
                str(identity.get("tree_manifest_sha256") or ""),
                "activation_journal_invalid",
            )
            root = str(identity.get("root") or "")
            if (
                type(identity.get("max_schema")) is not int
                or int(identity["max_schema"]) <= 0
                or _VERSION.fullmatch(str(identity.get("version") or "")) is None
                or not Path(root).is_absolute()
                or any(character in root for character in "\x00\r\n")
            ):
                raise ReleaseFailure("activation_journal_invalid")
        config_identity_changed = bool(
            self.config_identity_sha256 and payload["config_identity_sha256"] != self.config_identity_sha256
        )
        config_scope_changed = bool(
            self.config_scope_sha256
            and payload_keys in (v2_current_expected, *v3_expected)
            and payload.get("config_scope_sha256") != self.config_scope_sha256
        )
        config_retry_scope_changed = bool(
            self.config_retry_scope_sha256
            and payload_keys in (v2_current_expected, *v3_expected)
            and payload.get("config_retry_scope_sha256") != self.config_retry_scope_sha256
        )
        alias_claim_count_changed = bool(
            self.config_identity_sha256
            and payload_keys in (v2_current_expected, *v3_expected)
            and payload.get("alias_claim_count") != self.alias_claim_count
        )
        runtime_policy_changed = bool(
            self.config_identity_sha256
            and payload_keys in v3_expected
            and (
                payload.get("memory_vault_mode") != self.memory_vault_mode
                or payload.get("obsidian_mode") != self.obsidian_mode
                or payload.get("obsidian_root_sha256") != self.obsidian_root_sha256
            )
        )
        self._accepted_terminal_transition = ""
        if (
            config_identity_changed
            or config_scope_changed
            or config_retry_scope_changed
            or alias_claim_count_changed
            or runtime_policy_changed
        ):
            terminal = payload["phase"] in _TERMINAL_JOURNAL_PHASES
            legacy_v1_transition = bool(
                allow_terminal_config_transition
                and terminal
                and payload_keys == legacy_expected
                and self.legacy_config_identity_sha256
                and self.memory_vault_mode == "full_owner"
                and payload["config_identity_sha256"] == self.legacy_config_identity_sha256
            )
            expected_previous = (
                _journal_release(transition_previous) if transition_previous is not None else None
            )
            expected_fallback = (
                _journal_release(transition_fallback) if transition_fallback is not None else None
            )
            expected_candidate = (
                _journal_release(transition_candidate) if transition_candidate is not None else None
            )
            exact_env_terminal_release_matches = bool(
                expected_previous is not None
                and expected_previous == expected_fallback
                and (
                    (payload["phase"] == "clear" and payload.get("candidate") == expected_previous)
                    or (
                        payload["phase"] in {"rolled_back", "recovered"}
                        and payload.get("backup") is not None
                        and payload.get("database_mutation_possible") is True
                        and payload.get("network_writer_uncertain") is True
                        and payload.get("writer_target") in {"previous", "fallback"}
                        and payload.get("previous") == expected_previous
                        and payload.get("fallback") == expected_previous
                    )
                )
            )
            v2_to_v3_transition = bool(
                allow_terminal_config_transition
                and payload["phase"] == "clear"
                and payload_keys in (schema_v2_expected, v2_current_expected)
                and self.legacy_v2_config_identity_sha256
                and payload.get("config_identity_sha256") == self.legacy_v2_config_identity_sha256
                and self.obsidian_mode == "disabled"
                and self.alias_claim_count == 0
                and (
                    payload_keys == schema_v2_expected
                    or (
                        payload.get("memory_vault_mode") == self.memory_vault_mode
                        and int(payload.get("alias_claim_count") or 0) == 0
                    )
                )
                and expected_previous is not None
                and payload.get("candidate") == expected_previous
            )
            phase_a_to_b_transition = bool(
                allow_terminal_config_transition
                and payload["phase"] == "clear"
                and payload_keys in v3_expected
                and self.config_scope_sha256
                and payload.get("config_scope_sha256") == self.config_scope_sha256
                and payload.get("memory_vault_mode") == "full_owner"
                and self.memory_vault_mode == "disabled"
                and payload.get("obsidian_mode") == self.obsidian_mode
                and payload.get("obsidian_root_sha256") == self.obsidian_root_sha256
                and self.alias_claim_count == 0
                and expected_previous is not None
                and payload.get("candidate") == expected_previous
                and expected_previous == expected_fallback
            )
            obsidian_enable_transition = bool(
                allow_terminal_config_transition
                and self.staged_config_transition == _OBSIDIAN_ENABLE_TRANSITION
                and payload["phase"] == "clear"
                and payload_keys in v3_expected
                and self.transition_config_identity_sha256
                and payload.get("config_identity_sha256") == self.transition_config_identity_sha256
                and self.config_scope_sha256
                and payload.get("config_scope_sha256") == self.config_scope_sha256
                and payload.get("memory_vault_mode") == self.memory_vault_mode
                and payload.get("obsidian_mode") == "disabled"
                and self.obsidian_mode == "enabled"
                and payload.get("obsidian_root_sha256") == self.obsidian_root_sha256
                and int(payload.get("alias_claim_count") or 0) == 0
                and self.alias_claim_count == 0
                and expected_previous is not None
                and payload.get("candidate") == expected_previous
                and expected_previous == expected_fallback
            )
            exact_env_config_transition = bool(
                allow_terminal_config_transition
                and self.staged_config_transition in _EXACT_ENV_CONFIG_TRANSITIONS
                and payload_keys in v3_expected
                and self.transition_config_identity_sha256
                and payload.get("config_identity_sha256") == self.transition_config_identity_sha256
                and self.config_scope_sha256
                and payload.get("config_scope_sha256") == self.config_scope_sha256
                and payload.get("memory_vault_mode") == self.memory_vault_mode
                and payload.get("obsidian_mode") == self.obsidian_mode
                and payload.get("obsidian_root_sha256") == self.obsidian_root_sha256
                and int(payload.get("alias_claim_count") or 0) == 0
                and self.alias_claim_count == 0
                and exact_env_terminal_release_matches
            )
            phase_a_retry_after_fallback = bool(
                allow_terminal_config_transition
                and payload["phase"] in {"rolled_back", "recovered"}
                and payload_keys in v3_expected
                and self.config_scope_sha256
                and payload.get("config_scope_sha256") == self.config_scope_sha256
                and self.config_retry_scope_sha256
                and payload.get("config_retry_scope_sha256") == self.config_retry_scope_sha256
                and payload.get("memory_vault_mode") == "full_owner"
                and self.memory_vault_mode == "full_owner"
                and payload.get("obsidian_mode") == self.obsidian_mode
                and payload.get("obsidian_root_sha256") == self.obsidian_root_sha256
                and int(payload.get("alias_claim_count") or 0) > 0
                and self.alias_claim_count == 0
                and payload.get("database_mutation_possible") is True
                and payload.get("network_writer_uncertain") is True
                and payload.get("writer_target") == "fallback"
                and expected_candidate is not None
                and payload.get("candidate") == expected_candidate
                and expected_previous is not None
                and payload.get("fallback") == expected_previous
                and expected_previous == expected_fallback
            )
            if not (
                legacy_v1_transition
                or v2_to_v3_transition
                or phase_a_to_b_transition
                or obsidian_enable_transition
                or exact_env_config_transition
                or phase_a_retry_after_fallback
            ):
                raise ReleaseFailure("activation_config_identity_changed")
            if obsidian_enable_transition:
                self._accepted_terminal_transition = _OBSIDIAN_ENABLE_TRANSITION
            elif exact_env_config_transition:
                self._accepted_terminal_transition = self.staged_config_transition
        self._state = payload
        return dict(payload)

    def load(self) -> Mapping[str, Any]:
        return self._read()

    def begin(
        self,
        *,
        candidate: ReleaseIdentity,
        previous: ReleaseIdentity,
        fallback: ReleaseIdentity,
    ) -> None:
        if not self.config_identity_sha256 or not self.config_scope_sha256:
            raise ReleaseFailure("activation_config_identity_required")
        prebackup_config_transition = ""
        predecessor_env_sha256 = ""
        next_env_file = ""
        next_env_file_sha256 = ""
        staged_requested = bool(self.next_env_file)
        requested_descriptor = (
            self.predecessor_env_sha256,
            self.next_env_file,
            self.next_env_file_sha256,
        )
        if self.path.exists() or self.path.is_symlink():
            current = self._read(
                allow_terminal_config_transition=True,
                transition_previous=previous,
                transition_fallback=fallback,
                transition_candidate=candidate,
            )
            if current["phase"] not in _TERMINAL_JOURNAL_PHASES:
                raise ReleaseFailure("unfinished_activation_requires_recovery")
            if (
                self._accepted_terminal_transition in _EXACT_ENV_CONFIG_TRANSITIONS
                and current["phase"] in {"rolled_back", "recovered"}
                and self.database_backup() is None
            ):
                raise ReleaseFailure("activation_terminal_backup_required")
            carry_prebackup_transition = bool(
                current.get("phase") in {"rolled_back", "recovered"}
                and current.get("prebackup_config_transition") in _STAGED_CONFIG_TRANSITIONS
                and _HEX64.fullmatch(str(current.get("predecessor_env_sha256") or "")) is not None
                and isinstance(current.get("next_env_file"), str)
                and Path(str(current.get("next_env_file"))).is_absolute()
                and _HEX64.fullmatch(str(current.get("next_env_file_sha256") or "")) is not None
                and current.get("backup") is None
                and current.get("database_mutation_possible") is False
                and current.get("writer_target") == "previous"
            )
            if (
                current.get("phase") in {"rolled_back", "recovered"}
                and current.get("prebackup_config_transition") in _STAGED_CONFIG_TRANSITIONS
                and current.get("backup") is None
                and not carry_prebackup_transition
            ):
                raise ReleaseFailure("activation_prebackup_carry_invalid")
            if self._accepted_terminal_transition in _STAGED_CONFIG_TRANSITIONS:
                if (
                    not staged_requested
                    or self.staged_config_transition != self._accepted_terminal_transition
                ):
                    raise ReleaseFailure("activation_staged_environment_required")
            elif carry_prebackup_transition:
                current_descriptor = (
                    str(current["predecessor_env_sha256"]),
                    str(current["next_env_file"]),
                    str(current["next_env_file_sha256"]),
                )
                if (
                    not staged_requested
                    or self.staged_config_transition != current.get("prebackup_config_transition")
                    or requested_descriptor != current_descriptor
                ):
                    raise ReleaseFailure("activation_staged_environment_changed")
            elif staged_requested:
                raise ReleaseFailure("activation_staged_environment_not_permitted")
        if staged_requested:
            prebackup_config_transition = self.staged_config_transition
            predecessor_env_sha256 = self.predecessor_env_sha256
            next_env_file = self.next_env_file
            next_env_file_sha256 = self.next_env_file_sha256
        self._write(
            {
                "schema": ACTIVATION_JOURNAL_SCHEMA,
                "transaction_id": os.urandom(32).hex(),
                "phase": "prepared",
                "config_identity_sha256": self.config_identity_sha256,
                "config_identity_schema": RUNTIME_CONFIG_SCHEMA_V3,
                "config_scope_sha256": self.config_scope_sha256,
                "config_retry_scope_sha256": self.config_retry_scope_sha256,
                "alias_claim_count": self.alias_claim_count,
                "memory_vault_mode": self.memory_vault_mode,
                "obsidian_mode": self.obsidian_mode,
                "obsidian_root_sha256": self.obsidian_root_sha256,
                "prebackup_config_transition": prebackup_config_transition,
                "predecessor_env_sha256": predecessor_env_sha256,
                "next_env_file": next_env_file,
                "next_env_file_sha256": next_env_file_sha256,
                "candidate": _journal_release(candidate),
                "previous": _journal_release(previous),
                "fallback": _journal_release(fallback),
                "backup": None,
                "database_mutation_possible": False,
                "network_writer_uncertain": False,
                "terminal_receipt_sha256": "",
                "writer_target": "",
            }
        )

    @staticmethod
    def _backup_record(backup: DatabaseBackup) -> dict[str, Any]:
        payload = backup.opaque
        if not isinstance(payload, _ExactBackupPayload) or payload.obsidian is None:
            raise ReleaseFailure("activation_journal_backup_invalid")
        obsidian = payload.obsidian
        if backup.obsidian_receipt_sha256 != obsidian.manifest_sha256:
            raise ReleaseFailure("activation_journal_backup_invalid")
        return {
            "directory": str(payload.directory),
            "files": [{"name": name, "sha256": digest, "size": size} for name, digest, size in payload.files],
            "inbox_receipt_sha256": backup.inbox_receipt_sha256,
            "obsidian": {
                "file_count": obsidian.file_count,
                "manifest_sha256": obsidian.manifest_sha256,
                "present": obsidian.present,
                "total_bytes": obsidian.total_bytes,
            },
            "obsidian_receipt_sha256": backup.obsidian_receipt_sha256,
            "receipt_sha256": backup.receipt_sha256,
            "schema_version": backup.schema_version,
        }

    def record(
        self,
        phase: str,
        *,
        backup: DatabaseBackup | None = None,
        database_mutation_possible: bool = False,
        network_writer_uncertain: bool = False,
        writer_target: str = "",
        terminal_receipt_sha256: str = "",
        staged_transition_validation_sha256: str = "",
    ) -> None:
        if phase not in _JOURNAL_PHASES:
            raise ReleaseFailure("activation_journal_phase_invalid")
        if (phase in _TERMINAL_JOURNAL_PHASES) != bool(terminal_receipt_sha256):
            raise ReleaseFailure("activation_journal_terminal_digest_invalid")
        state = dict(self._state or self._read())
        current_phase = str(state.get("phase") or "")
        if not _journal_transition_allowed(current_phase, phase):
            raise ReleaseFailure("activation_journal_transition_invalid")
        staged_transition = _staged_config_transition(state)
        special_transition = bool(
            staged_transition is not None
            and staged_transition[0]
            == _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION
        )
        if (
            bool(staged_transition_validation_sha256)
            != (special_transition and current_phase == "prepared" and phase == "bridge_stop_attempted")
            or (
                staged_transition_validation_sha256
                and (
                    staged_transition is None
                    or staged_transition_validation_sha256
                    != _staged_transition_validation_sha256(*staged_transition)
                )
            )
        ):
            raise ReleaseFailure("activation_staged_transition_validation_invalid")
        if current_phase == "backup_complete" and (
            (staged_transition is None and phase == "environment_swap_attempted")
            or (staged_transition is not None and phase == "migration_attempted")
        ):
            raise ReleaseFailure("activation_environment_transition_missing")
        if phase in {"environment_swap_attempted", "environment_active"} and staged_transition is None:
            raise ReleaseFailure("activation_environment_transition_unexpected")
        state["phase"] = phase
        if backup is not None:
            backup_record = self._backup_record(backup)
            existing_backup = state.get("backup")
            if existing_backup is not None and existing_backup != backup_record:
                raise ReleaseFailure("activation_journal_backup_changed")
            state["backup"] = backup_record
        state["database_mutation_possible"] = bool(
            state["database_mutation_possible"] or database_mutation_possible
        )
        state["network_writer_uncertain"] = bool(
            state["network_writer_uncertain"] or network_writer_uncertain
        )
        if writer_target:
            if writer_target not in {"candidate", "previous", "fallback"}:
                raise ReleaseFailure("activation_journal_writer_target_invalid")
            state["writer_target"] = writer_target
        if terminal_receipt_sha256:
            state["terminal_receipt_sha256"] = _closed_hash(
                terminal_receipt_sha256,
                "activation_journal_terminal_digest_invalid",
            )
        if staged_transition_validation_sha256:
            state["staged_transition_validation_sha256"] = (
                staged_transition_validation_sha256
            )
        self._write(state)

    def release_identities(self) -> tuple[ReleaseIdentity, ReleaseIdentity, ReleaseIdentity]:
        state = dict(self._state or self._read())

        def identity(key: str) -> ReleaseIdentity:
            raw = state[key]
            return load_release_identity(
                Path(str(raw["root"])),
                expected_tree_sha256=str(raw["tree_manifest_sha256"]),
            )

        return identity("candidate"), identity("previous"), identity("fallback")

    def database_backup(self) -> DatabaseBackup | None:
        state = dict(self._state or self._read())
        raw = state.get("backup")
        if raw is None:
            return None
        legacy_keys = {
            "directory",
            "files",
            "inbox_receipt_sha256",
            "receipt_sha256",
            "schema_version",
        }
        current_keys = legacy_keys | {"obsidian", "obsidian_receipt_sha256"}
        if not isinstance(raw, dict) or frozenset(raw) not in {
            frozenset(legacy_keys),
            frozenset(current_keys),
        }:
            raise ReleaseFailure("activation_journal_backup_invalid")
        if state.get("config_identity_schema") == RUNTIME_CONFIG_SCHEMA_V3 and set(raw) != current_keys:
            raise ReleaseFailure("activation_journal_backup_obsidian_required")
        directory = _private_directory(Path(str(raw["directory"])))
        if directory.parent != self.backup_root or not directory.name.startswith("immutable-cutover-"):
            raise ReleaseFailure("activation_journal_backup_invalid")
        files_raw = raw.get("files")
        if not isinstance(files_raw, list) or not files_raw:
            raise ReleaseFailure("activation_journal_backup_invalid")
        allowed_names = {
            "database.sqlite3",
            "database.sqlite3-wal",
            "inbox.sqlite3",
            "inbox.sqlite3-wal",
        }
        files: list[tuple[str, str, int]] = []
        seen: set[str] = set()
        for item in files_raw:
            if not isinstance(item, dict) or set(item) != {"name", "sha256", "size"}:
                raise ReleaseFailure("activation_journal_backup_invalid")
            name = str(item.get("name") or "")
            size = item.get("size")
            digest = _closed_hash(str(item.get("sha256") or ""), "activation_journal_backup_invalid")
            if name not in allowed_names or name in seen or type(size) is not int or int(size) < 0:
                raise ReleaseFailure("activation_journal_backup_invalid")
            seen.add(name)
            source = directory / name
            try:
                source_status = os.stat(source, follow_symlinks=False)
                source_resolved = source.resolve(strict=True)
            except OSError as exc:
                raise ReleaseFailure("activation_journal_backup_invalid") from exc
            if (
                source_resolved != source
                or not stat.S_ISREG(source_status.st_mode)
                or source_status.st_nlink != 1
                or source_status.st_uid != os.geteuid()
                or stat.S_IMODE(source_status.st_mode) & 0o077
                or source_status.st_size != size
                or _sha256_file(source) != digest
            ):
                raise ReleaseFailure("activation_journal_backup_changed")
            files.append((name, digest, int(size)))
        if not {"database.sqlite3", "inbox.sqlite3"}.issubset(seen):
            raise ReleaseFailure("activation_journal_backup_invalid")
        schema_version = raw.get("schema_version")
        if type(schema_version) is not int or schema_version <= 0:
            raise ReleaseFailure("activation_journal_backup_invalid")
        entries = [{"name": name, "sha256": digest, "size": size} for name, digest, size in sorted(files)]
        database_receipt = _sha256_bytes(
            _canonical_json([item for item in entries if str(item["name"]).startswith("database")])
        )
        inbox_receipt = _sha256_bytes(
            _canonical_json([item for item in entries if str(item["name"]).startswith("inbox")])
        )
        if database_receipt != str(raw.get("receipt_sha256") or "") or inbox_receipt != str(
            raw.get("inbox_receipt_sha256") or ""
        ):
            raise ReleaseFailure("activation_journal_backup_receipt_mismatch")
        manifest_path = _private_regular_file(
            directory / "manifest.json",
            maximum_bytes=1 << 20,
            code="activation_journal_backup_invalid",
        )
        try:
            manifest = _unique_json(manifest_path.read_text(encoding="ascii"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseFailure("activation_journal_backup_invalid") from exc
        if manifest != {
            "schema": "friday.immutable-cutover-exact-backup.v1",
            "database_schema": schema_version,
            "files": entries,
        }:
            raise ReleaseFailure("activation_journal_backup_manifest_mismatch")
        obsidian: _ExactObsidianBackup | None = None
        obsidian_receipt = "0" * 64
        if set(raw) == current_keys:
            obsidian_raw = raw.get("obsidian")
            if not isinstance(obsidian_raw, dict) or set(obsidian_raw) != {
                "file_count",
                "manifest_sha256",
                "present",
                "total_bytes",
            }:
                raise ReleaseFailure("activation_journal_backup_invalid")
            file_count = obsidian_raw.get("file_count")
            total_bytes = obsidian_raw.get("total_bytes")
            present = obsidian_raw.get("present")
            manifest_sha256 = _closed_hash(
                str(obsidian_raw.get("manifest_sha256") or ""),
                "activation_journal_backup_invalid",
            )
            obsidian_receipt = _closed_hash(
                str(raw.get("obsidian_receipt_sha256") or ""),
                "activation_journal_backup_invalid",
            )
            if (
                type(file_count) is not int
                or not 0 <= int(file_count) <= MAX_OBSIDIAN_BACKUP_ENTRIES
                or type(total_bytes) is not int
                or not 0 <= int(total_bytes) <= MAX_OBSIDIAN_BACKUP_BYTES
                or type(present) is not bool
                or manifest_sha256 != obsidian_receipt
            ):
                raise ReleaseFailure("activation_journal_backup_invalid")
            obsidian = _ExactObsidianBackup(
                bool(present),
                manifest_sha256,
                int(file_count),
                int(total_bytes),
            )
            _verify_obsidian_backup(directory, obsidian)
        expected_top_level = {name for name, _digest, _size in files} | {"manifest.json"}
        if obsidian is not None:
            expected_top_level.add("obsidian-manifest.json")
            if obsidian.present:
                expected_top_level.add("obsidian-root")
        try:
            actual_top_level = {path.name for path in directory.iterdir()}
        except OSError as exc:
            raise ReleaseFailure("activation_journal_backup_invalid") from exc
        if actual_top_level != expected_top_level:
            raise ReleaseFailure("activation_journal_backup_manifest_mismatch")
        return DatabaseBackup(
            schema_version=schema_version,
            receipt_sha256=database_receipt,
            inbox_receipt_sha256=inbox_receipt,
            obsidian_receipt_sha256=obsidian_receipt,
            opaque=_ExactBackupPayload(directory, tuple(sorted(files)), obsidian),
        )


def _copy_private(source: Path, destination: Path) -> None:
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    source_descriptor = os.open(source, source_flags)
    descriptor = -1
    try:
        descriptor = os.open(destination, destination_flags, 0o600)
        source_status = os.fstat(source_descriptor)
        identity = (
            int(source_status.st_dev),
            int(source_status.st_ino),
            int(source_status.st_size),
            int(source_status.st_mtime_ns),
            int(source_status.st_ctime_ns),
        )
        if (
            not stat.S_ISREG(source_status.st_mode)
            or source_status.st_nlink != 1
            or source_status.st_uid != os.geteuid()
            or stat.S_IMODE(source_status.st_mode) & 0o077
        ):
            raise ReleaseFailure("backup_source_invalid")
        with (
            os.fdopen(source_descriptor, "rb", closefd=False) as reader,
            os.fdopen(descriptor, "wb", closefd=False) as writer,
        ):
            shutil.copyfileobj(reader, writer, length=1 << 20)
            writer.flush()
            os.fsync(writer.fileno())
        after = os.fstat(source_descriptor)
        lexical = os.stat(source, follow_symlinks=False)
        if (
            identity
            != (
                int(after.st_dev),
                int(after.st_ino),
                int(after.st_size),
                int(after.st_mtime_ns),
                int(after.st_ctime_ns),
            )
            or (int(lexical.st_dev), int(lexical.st_ino)) != identity[:2]
        ):
            raise ReleaseFailure("backup_source_changed")
    finally:
        os.close(source_descriptor)
        if descriptor >= 0:
            os.close(descriptor)
    if source_status.st_size != destination.stat().st_size:
        raise ReleaseFailure("backup_copy_changed")


def _private_file_attestation(path: Path) -> tuple[tuple[int, int, int, int, int], str]:
    resolved = _private_regular_file(path, maximum_bytes=1 << 40, code="private_file_invalid")
    before = os.stat(resolved, follow_symlinks=False)
    identity = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(before.st_mtime_ns),
        int(before.st_ctime_ns),
    )
    digest = _sha256_file(resolved)
    after = os.stat(resolved, follow_symlinks=False)
    if identity != (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
        int(after.st_ctime_ns),
    ):
        raise ReleaseFailure("private_file_changed_during_attestation")
    return identity, digest


@contextmanager
def _exact_environment(overrides: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        os.environ.update(overrides)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _sqlite_integrity(path: Path, *, require_schema: bool) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    try:
        if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
            raise ReleaseFailure("backup_integrity_failed")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ReleaseFailure("backup_foreign_keys_failed")
        if not require_schema:
            return 0
        row = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        if row is None or type(row[0]) not in {str, int}:
            raise ReleaseFailure("backup_schema_invalid")
        return int(row[0])
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise ReleaseFailure("backup_verification_failed") from exc
    finally:
        connection.close()


def _verify_sqlite_snapshot_copy(
    source_directory: Path,
    *,
    label: str,
    require_schema: bool,
) -> int:
    scratch = Path(tempfile.mkdtemp(prefix=f".{label}-verify-", dir=source_directory.parent))
    os.chmod(scratch, 0o700)
    try:
        for suffix in ("", "-wal"):
            source = source_directory / f"{label}.sqlite3{suffix}"
            if source.exists():
                _copy_private(source, scratch / source.name)
        return _sqlite_integrity(scratch / f"{label}.sqlite3", require_schema=require_schema)
    finally:
        for child in scratch.iterdir():
            with suppress(OSError):
                os.chmod(child, 0o600)
            child.unlink(missing_ok=True)
        scratch.rmdir()


def _obsidian_entry_identity(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(status.st_dev),
        int(status.st_ino),
        int(status.st_size),
        int(status.st_mtime_ns),
        int(status.st_ctime_ns),
    )


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1 << 20):
        digest.update(chunk)
    return digest.hexdigest()


def _copy_descriptor_private(source_descriptor: int, destination: Path) -> str:
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    digest = hashlib.sha256()
    try:
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        while chunk := os.read(source_descriptor, 1 << 20):
            digest.update(chunk)
            _write_all(descriptor, chunk)
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _obsidian_relative_path(parent: str, name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or any(character in name for character in "\x00\r\n"):
        raise ReleaseFailure("obsidian_backup_path_invalid")
    return f"{parent}/{name}" if parent else name


def _validate_obsidian_source_root(root: Path, *, allow_absent: bool) -> tuple[Path, bool]:
    lexical = Path(os.path.abspath(root))
    if not lexical.is_absolute() or any(character in str(lexical) for character in "\x00\r\n"):
        raise ReleaseFailure("obsidian_root_invalid")
    try:
        parent = _private_directory(lexical.parent)
    except ReleaseFailure as exc:
        if not allow_absent or lexical.parent.exists() or lexical.parent.is_symlink():
            raise
        existing = lexical.parent
        while not existing.exists() and not existing.is_symlink():
            if existing.parent == existing:
                raise ReleaseFailure("obsidian_root_invalid") from exc
            existing = existing.parent
        _private_directory(existing)
        if lexical.parent.resolve(strict=False) != lexical.parent:
            raise ReleaseFailure("obsidian_root_invalid") from exc
        parent = lexical.parent
    if lexical.parent != parent:
        raise ReleaseFailure("obsidian_root_invalid")
    try:
        status = os.stat(lexical, follow_symlinks=False)
    except FileNotFoundError as exc:
        if lexical.is_symlink() or not allow_absent:
            raise ReleaseFailure("obsidian_root_invalid") from exc
        return lexical, False
    except OSError as exc:
        raise ReleaseFailure("obsidian_root_invalid") from exc
    try:
        resolved = lexical.resolve(strict=True)
    except OSError as exc:
        raise ReleaseFailure("obsidian_root_invalid") from exc
    if (
        resolved != lexical
        or not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        raise ReleaseFailure("obsidian_root_invalid")
    return lexical, True


def _capture_obsidian_tree(
    root: Path,
    *,
    destination: Path | None,
) -> tuple[dict[str, Any], tuple[tuple[str, tuple[int, int, int, int, int]], ...]]:
    """Capture one stable, no-follow tree and optionally copy it privately."""

    lexical, present = _validate_obsidian_source_root(root, allow_absent=True)
    if not present:
        if lexical.exists() or lexical.is_symlink():
            raise ReleaseFailure("obsidian_backup_source_changed")
        return (
            {
                "schema": "friday.immutable-cutover-obsidian-root.v1",
                "present": False,
                "root": None,
                "directories": [],
                "files": [],
            },
            (),
        )
    if destination is not None:
        destination.mkdir(mode=0o700)
        os.chmod(destination, 0o700)
    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_descriptor = os.open(lexical, root_flags)
    directories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    identities: list[tuple[str, tuple[int, int, int, int, int]]] = []
    total_bytes = 0
    entry_count = 0

    def walk(descriptor: int, relative: str, destination_directory: Path | None) -> None:
        nonlocal entry_count, total_bytes
        try:
            entries = sorted(os.scandir(descriptor), key=lambda item: item.name)
        except OSError as exc:
            raise ReleaseFailure("obsidian_backup_source_invalid") from exc
        for entry in entries:
            relative_path = _obsidian_relative_path(relative, entry.name)
            try:
                status = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ReleaseFailure("obsidian_backup_source_changed") from exc
            entry_count += 1
            if entry_count > MAX_OBSIDIAN_BACKUP_ENTRIES:
                raise ReleaseFailure("obsidian_backup_entry_bound_exceeded")
            mode = stat.S_IMODE(status.st_mode)
            if status.st_uid != os.geteuid() or not 0 <= mode <= 0o777:
                raise ReleaseFailure("obsidian_backup_source_invalid")
            if stat.S_ISDIR(status.st_mode):
                child_descriptor = os.open(
                    entry.name,
                    root_flags,
                    dir_fd=descriptor,
                )
                try:
                    opened = os.fstat(child_descriptor)
                    identity = _obsidian_entry_identity(status)
                    if not stat.S_ISDIR(opened.st_mode) or _obsidian_entry_identity(opened) != identity:
                        raise ReleaseFailure("obsidian_backup_source_changed")
                    child_destination = (
                        destination_directory / entry.name if destination_directory is not None else None
                    )
                    if child_destination is not None:
                        child_destination.mkdir(mode=0o700)
                        os.chmod(child_destination, 0o700)
                    directories.append(
                        {"path": relative_path, "mode": mode, "mtime_ns": int(status.st_mtime_ns)}
                    )
                    identities.append((relative_path + "/", identity))
                    walk(child_descriptor, relative_path, child_destination)
                    after = os.fstat(child_descriptor)
                    lexical_after = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                    if (
                        _obsidian_entry_identity(after) != identity
                        or _obsidian_entry_identity(lexical_after) != identity
                    ):
                        raise ReleaseFailure("obsidian_backup_source_changed")
                    if child_destination is not None:
                        _fsync_directory(child_destination)
                finally:
                    os.close(child_descriptor)
                continue
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                raise ReleaseFailure("obsidian_backup_source_invalid")
            total_bytes += int(status.st_size)
            if total_bytes > MAX_OBSIDIAN_BACKUP_BYTES:
                raise ReleaseFailure("obsidian_backup_byte_bound_exceeded")
            file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            source_descriptor = os.open(entry.name, file_flags, dir_fd=descriptor)
            try:
                opened = os.fstat(source_descriptor)
                identity = _obsidian_entry_identity(status)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or _obsidian_entry_identity(opened) != identity
                ):
                    raise ReleaseFailure("obsidian_backup_source_changed")
                digest = (
                    _copy_descriptor_private(source_descriptor, destination_directory / entry.name)
                    if destination_directory is not None
                    else _hash_descriptor(source_descriptor)
                )
                after = os.fstat(source_descriptor)
                lexical_after = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                if (
                    _obsidian_entry_identity(after) != identity
                    or _obsidian_entry_identity(lexical_after) != identity
                ):
                    raise ReleaseFailure("obsidian_backup_source_changed")
            finally:
                os.close(source_descriptor)
            files.append(
                {
                    "path": relative_path,
                    "sha256": digest,
                    "size": int(status.st_size),
                    "mode": mode,
                    "mtime_ns": int(status.st_mtime_ns),
                }
            )
            identities.append((relative_path, identity))

    try:
        root_status = os.fstat(root_descriptor)
        lexical_status = os.stat(lexical, follow_symlinks=False)
        root_identity = _obsidian_entry_identity(root_status)
        if not stat.S_ISDIR(root_status.st_mode) or _obsidian_entry_identity(lexical_status) != root_identity:
            raise ReleaseFailure("obsidian_backup_source_changed")
        identities.append(("/", root_identity))
        walk(root_descriptor, "", destination)
        after = os.fstat(root_descriptor)
        lexical_after = os.stat(lexical, follow_symlinks=False)
        if (
            _obsidian_entry_identity(after) != root_identity
            or _obsidian_entry_identity(lexical_after) != root_identity
        ):
            raise ReleaseFailure("obsidian_backup_source_changed")
    finally:
        os.close(root_descriptor)
    return (
        {
            "schema": "friday.immutable-cutover-obsidian-root.v1",
            "present": True,
            "root": {
                "mode": stat.S_IMODE(root_status.st_mode),
                "mtime_ns": int(root_status.st_mtime_ns),
            },
            "directories": sorted(directories, key=lambda item: str(item["path"])),
            "files": sorted(files, key=lambda item: str(item["path"])),
        },
        tuple(sorted(identities)),
    )


def _snapshot_obsidian_root(config: SystemdConfig, directory: Path) -> _ExactObsidianBackup:
    destination = directory / "obsidian-root"
    first, identities = _capture_obsidian_tree(_obsidian_root(config), destination=destination)
    second, second_identities = _capture_obsidian_tree(_obsidian_root(config), destination=None)
    if first != second or identities != second_identities:
        raise ReleaseFailure("obsidian_backup_source_changed")
    manifest_bytes = _canonical_json(first) + b"\n"
    if len(manifest_bytes) > MAX_EXACT_MANIFEST_BYTES:
        raise ReleaseFailure("obsidian_backup_manifest_bound_exceeded")
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    _write_private_durable(
        directory / "obsidian-manifest.json",
        manifest_bytes,
        final_mode=0o400,
    )
    if destination.exists():
        _fsync_tree(destination)
    return _ExactObsidianBackup(
        present=bool(first["present"]),
        manifest_sha256=manifest_sha256,
        file_count=len(first["files"]),
        total_bytes=sum(int(item["size"]) for item in first["files"]),
    )


def _validated_obsidian_manifest_path(value: Any) -> str:
    if not isinstance(value, str) or not value or any(character in value for character in "\x00\r\n"):
        raise ReleaseFailure("obsidian_backup_manifest_invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseFailure("obsidian_backup_manifest_invalid")
    return value


def _validate_obsidian_manifest(
    payload: Any,
) -> tuple[bool, dict[str, Any], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "present",
        "root",
        "directories",
        "files",
    }:
        raise ReleaseFailure("obsidian_backup_manifest_invalid")
    if (
        payload.get("schema") != "friday.immutable-cutover-obsidian-root.v1"
        or type(payload.get("present")) is not bool
        or not isinstance(payload.get("directories"), list)
        or not isinstance(payload.get("files"), list)
    ):
        raise ReleaseFailure("obsidian_backup_manifest_invalid")
    present = bool(payload["present"])
    if not present:
        if payload.get("root") is not None or payload["directories"] or payload["files"]:
            raise ReleaseFailure("obsidian_backup_manifest_invalid")
        return False, {}, {}
    root = payload.get("root")
    if not isinstance(root, dict) or set(root) != {"mode", "mtime_ns"}:
        raise ReleaseFailure("obsidian_backup_manifest_invalid")
    root_mode = root.get("mode")
    root_mtime = root.get("mtime_ns")
    if (
        type(root_mode) is not int
        or not 0 <= int(root_mode) <= 0o777
        or int(root_mode) != 0o700
        or type(root_mtime) is not int
        or int(root_mtime) < 0
    ):
        raise ReleaseFailure("obsidian_backup_manifest_invalid")
    directories: dict[str, dict[str, Any]] = {}
    files: dict[str, dict[str, Any]] = {}
    for item in payload["directories"]:
        if not isinstance(item, dict) or set(item) != {"path", "mode", "mtime_ns"}:
            raise ReleaseFailure("obsidian_backup_manifest_invalid")
        path = _validated_obsidian_manifest_path(item.get("path"))
        mode = item.get("mode")
        mtime_ns = item.get("mtime_ns")
        if (
            path in directories
            or type(mode) is not int
            or not 0 <= int(mode) <= 0o777
            or type(mtime_ns) is not int
            or int(mtime_ns) < 0
        ):
            raise ReleaseFailure("obsidian_backup_manifest_invalid")
        directories[path] = item
    total_bytes = 0
    for item in payload["files"]:
        if not isinstance(item, dict) or set(item) != {
            "path",
            "sha256",
            "size",
            "mode",
            "mtime_ns",
        }:
            raise ReleaseFailure("obsidian_backup_manifest_invalid")
        path = _validated_obsidian_manifest_path(item.get("path"))
        mode = item.get("mode")
        size = item.get("size")
        mtime_ns = item.get("mtime_ns")
        _closed_hash(str(item.get("sha256") or ""), "obsidian_backup_manifest_invalid")
        if (
            path in files
            or path in directories
            or type(mode) is not int
            or not 0 <= int(mode) <= 0o777
            or type(size) is not int
            or int(size) < 0
            or type(mtime_ns) is not int
            or int(mtime_ns) < 0
        ):
            raise ReleaseFailure("obsidian_backup_manifest_invalid")
        total_bytes += int(size)
        files[path] = item
    if len(directories) + len(files) > MAX_OBSIDIAN_BACKUP_ENTRIES:
        raise ReleaseFailure("obsidian_backup_entry_bound_exceeded")
    if total_bytes > MAX_OBSIDIAN_BACKUP_BYTES:
        raise ReleaseFailure("obsidian_backup_byte_bound_exceeded")
    all_paths = set(directories) | set(files)
    for path in all_paths:
        parent = str(PurePosixPath(path).parent)
        if parent != "." and parent not in directories:
            raise ReleaseFailure("obsidian_backup_manifest_invalid")
    return True, directories, files


def _verify_obsidian_backup(
    directory: Path,
    descriptor: _ExactObsidianBackup,
) -> dict[str, Any]:
    manifest_path = _private_regular_file(
        directory / "obsidian-manifest.json",
        maximum_bytes=MAX_EXACT_MANIFEST_BYTES,
        code="obsidian_backup_manifest_invalid",
    )
    raw = manifest_path.read_bytes()
    if _sha256_bytes(raw) != _closed_hash(
        descriptor.manifest_sha256,
        "obsidian_backup_receipt_invalid",
    ):
        raise ReleaseFailure("obsidian_backup_manifest_changed")
    try:
        manifest = _unique_json(raw.decode("ascii"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseFailure("obsidian_backup_manifest_invalid") from exc
    present, directories, files = _validate_obsidian_manifest(manifest)
    if (
        present != descriptor.present
        or len(files) != descriptor.file_count
        or sum(int(item["size"]) for item in files.values()) != descriptor.total_bytes
    ):
        raise ReleaseFailure("obsidian_backup_receipt_mismatch")
    backup_root = directory / "obsidian-root"
    if not present:
        if backup_root.exists() or backup_root.is_symlink():
            raise ReleaseFailure("obsidian_backup_manifest_mismatch")
        return manifest
    actual, _identities = _capture_obsidian_tree(backup_root, destination=None)
    if actual.get("present") is not True or actual.get("root", {}).get("mode") != 0o700:
        raise ReleaseFailure("obsidian_backup_manifest_mismatch")
    actual_directories = {
        str(item["path"]): item for item in actual.get("directories", []) if isinstance(item, dict)
    }
    actual_files = {str(item["path"]): item for item in actual.get("files", []) if isinstance(item, dict)}
    if set(actual_directories) != set(directories) or set(actual_files) != set(files):
        raise ReleaseFailure("obsidian_backup_manifest_mismatch")
    if any(int(item.get("mode", -1)) != 0o700 for item in actual_directories.values()):
        raise ReleaseFailure("obsidian_backup_manifest_mismatch")
    for path, expected in files.items():
        observed = actual_files[path]
        if (
            int(observed.get("mode", -1)) != 0o600
            or observed.get("sha256") != expected.get("sha256")
            or observed.get("size") != expected.get("size")
        ):
            raise ReleaseFailure("obsidian_backup_manifest_mismatch")
    return manifest


def _secondary_product_sqlite_sidecar(path: Path) -> os.stat_result | None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReleaseFailure("backup_secondary_product_sidecar_invalid") from exc
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) & 0o077
    ):
        raise ReleaseFailure("backup_secondary_product_sidecar_invalid")
    return status


def _prepare_secondary_product_backup_boundary(config: SystemdConfig) -> None:
    """Reject a live probe and truncate its already-scrubbed WAL before raw copy."""

    database = _private_regular_file(
        config.database,
        maximum_bytes=1 << 40,
        code="backup_database_source_invalid",
    )
    wal = database.with_name(f"{database.name}-wal")
    shm = database.with_name(f"{database.name}-shm")
    _secondary_product_sqlite_sidecar(wal)
    _secondary_product_sqlite_sidecar(shm)
    checkpoint: tuple[Any, ...] | sqlite3.Row | None = None
    try:
        connection = sqlite3.connect(
            str(database),
            timeout=30.0,
            isolation_level=None,
        )
        try:
            raw_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_objects'"
            ).fetchone()
            if (
                raw_table is not None
                and connection.execute(
                    """SELECT 1 FROM raw_objects
                     WHERE source_ref LIKE 'secondary-product-witness:%' LIMIT 1"""
                ).fetchone()
                is not None
            ):
                raise ReleaseFailure("backup_active_secondary_product_witness")
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            connection.close()
    except ReleaseFailure:
        raise
    except sqlite3.DatabaseError as exc:
        raise ReleaseFailure("backup_secondary_product_checkpoint_failed") from exc
    if (
        checkpoint is None
        or len(checkpoint) != 3
        or any(type(value) is not int for value in checkpoint)
        or tuple(int(value) for value in checkpoint) not in {(0, 0, 0), (0, -1, -1)}
    ):
        raise ReleaseFailure("backup_secondary_product_checkpoint_failed")
    wal_status = _secondary_product_sqlite_sidecar(wal)
    _secondary_product_sqlite_sidecar(shm)
    if wal_status is not None and wal_status.st_size != 0:
        raise ReleaseFailure("backup_secondary_product_wal_invalid")


def _exact_sqlite_backup(config: SystemdConfig) -> DatabaseBackup:
    _prepare_secondary_product_backup_boundary(config)
    backup_root = _private_directory(config.backup_dir, create=True)
    directory = Path(tempfile.mkdtemp(prefix="immutable-cutover-", dir=backup_root))
    os.chmod(directory, 0o700)
    files: list[tuple[str, str, int]] = []
    groups = (("database", config.database), ("inbox", config.inbox_database))
    try:
        for label, source in groups:
            for suffix in ("", "-wal"):
                candidate = Path(f"{source}{suffix}")
                name = f"{label}.sqlite3{suffix}"
                if not candidate.exists():
                    continue
                destination = directory / name
                _copy_private(candidate, destination)
                files.append((name, _sha256_file(destination), destination.stat().st_size))
        if not any(name == "database.sqlite3" for name, _digest, _size in files) or not any(
            name == "inbox.sqlite3" for name, _digest, _size in files
        ):
            raise ReleaseFailure("backup_main_file_missing")
        schema_version = _verify_sqlite_snapshot_copy(
            directory,
            label="database",
            require_schema=True,
        )
        _verify_sqlite_snapshot_copy(directory, label="inbox", require_schema=False)
        obsidian = _snapshot_obsidian_root(config, directory)
        manifest: dict[str, Any] = {
            "schema": "friday.immutable-cutover-exact-backup.v1",
            "database_schema": schema_version,
            "files": [{"name": name, "sha256": digest, "size": size} for name, digest, size in sorted(files)],
        }
        manifest_bytes = _canonical_json(manifest) + b"\n"
        manifest_path = directory / "manifest.json"
        _write_private_durable(manifest_path, manifest_bytes, final_mode=0o400)
        _fsync_directory(directory)
        _fsync_directory(backup_root)
        _verify_obsidian_backup(directory, obsidian)
        database_basis = [item for item in manifest["files"] if str(item["name"]).startswith("database")]
        inbox_basis = [item for item in manifest["files"] if str(item["name"]).startswith("inbox")]
        return DatabaseBackup(
            schema_version=schema_version,
            receipt_sha256=_sha256_bytes(_canonical_json(database_basis)),
            inbox_receipt_sha256=_sha256_bytes(_canonical_json(inbox_basis)),
            obsidian_receipt_sha256=obsidian.manifest_sha256,
            opaque=_ExactBackupPayload(directory, tuple(files), obsidian),
        )
    except BaseException:
        # Keep a partial private directory for forensic inspection.  It is never
        # eligible for restore because no DatabaseBackup object was returned.
        raise


def _exact_inbox_backup(config: SystemdConfig) -> _ExactInboxBackup:
    backup_root = _private_directory(config.backup_dir, create=True)
    directory = Path(tempfile.mkdtemp(prefix="historical-album-inbox-", dir=backup_root))
    os.chmod(directory, 0o700)
    entries: list[dict[str, Any]] = []
    for suffix in ("", "-wal"):
        source = Path(f"{config.inbox_database}{suffix}")
        if not source.exists():
            continue
        destination = directory / f"inbox.sqlite3{suffix}"
        _copy_private(source, destination)
        entries.append(
            {
                "name": destination.name,
                "sha256": _sha256_file(destination),
                "size": destination.stat().st_size,
            }
        )
    if not any(item["name"] == "inbox.sqlite3" for item in entries):
        raise ReleaseFailure("inbox_backup_main_missing")
    _verify_sqlite_snapshot_copy(directory, label="inbox", require_schema=False)
    receipt = {
        "schema": "friday.historical-album-inbox-backup.v1",
        "files": entries,
    }
    receipt_bytes = _canonical_json(receipt) + b"\n"
    receipt_path = directory / "manifest.json"
    _write_private_durable(receipt_path, receipt_bytes, final_mode=0o400)
    _fsync_directory(directory)
    _fsync_directory(backup_root)
    return _ExactInboxBackup(directory, _sha256_bytes(receipt_bytes))


def _verify_exact_inbox_backup(config: SystemdConfig, backup: _ExactInboxBackup) -> _ExactInboxBackup:
    backup_root = _private_directory(config.backup_dir, create=True)
    directory = _private_directory(backup.directory)
    if directory.parent != backup_root or not directory.name.startswith("historical-album-inbox-"):
        raise ReleaseFailure("inbox_backup_identity_invalid")
    manifest = _private_regular_file(
        directory / "manifest.json",
        maximum_bytes=1 << 20,
        code="inbox_backup_manifest_invalid",
    )
    raw = manifest.read_bytes()
    if _sha256_bytes(raw) != _closed_hash(backup.receipt_sha256, "inbox_backup_receipt_invalid"):
        raise ReleaseFailure("inbox_backup_manifest_changed")
    try:
        payload = _unique_json(raw.decode("ascii"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseFailure("inbox_backup_manifest_invalid") from exc
    if (
        set(payload) != {"files", "schema"}
        or payload.get("schema") != "friday.historical-album-inbox-backup.v1"
    ):
        raise ReleaseFailure("inbox_backup_manifest_invalid")
    items = payload.get("files")
    if not isinstance(items, list) or not items:
        raise ReleaseFailure("inbox_backup_manifest_invalid")
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {"name", "sha256", "size"}:
            raise ReleaseFailure("inbox_backup_manifest_invalid")
        name = str(item.get("name") or "")
        digest = _closed_hash(str(item.get("sha256") or ""), "inbox_backup_manifest_invalid")
        size = item.get("size")
        if (
            name not in {"inbox.sqlite3", "inbox.sqlite3-wal"}
            or name in seen
            or type(size) is not int
            or size < 0
        ):
            raise ReleaseFailure("inbox_backup_manifest_invalid")
        seen.add(name)
        source = (
            _private_regular_file_allow_empty(
                directory / name,
                maximum_bytes=1 << 40,
                code="inbox_backup_file_invalid",
            )
            if name.endswith("-wal")
            else _private_regular_file(
                directory / name,
                maximum_bytes=1 << 40,
                code="inbox_backup_file_invalid",
            )
        )
        if source.stat().st_size != size or _sha256_file(source) != digest:
            raise ReleaseFailure("inbox_backup_file_changed")
    if "inbox.sqlite3" not in seen:
        raise ReleaseFailure("inbox_backup_main_missing")
    _verify_sqlite_snapshot_copy(directory, label="inbox", require_schema=False)
    return backup


_ALBUM_RECOVERY_PHASES = (
    "prepared",
    "bridge_stop_attempted",
    "bridge_quiesced",
    "backup_complete",
    "cas_attempted",
    "cas_complete",
    "bridge_start_attempted",
    "bridge_accepted",
    "complete",
)


class DurableAlbumRecoveryJournal:
    """Private idempotency boundary for the one historical album repair."""

    def __init__(
        self,
        path: Path,
        *,
        backup_root: Path,
        config_identity_sha256: str,
    ) -> None:
        parent = _private_directory(path.parent)
        lexical = Path(os.path.abspath(path))
        if lexical.parent != parent or lexical.name != "historical-album-recovery.v1.json":
            raise ReleaseFailure("album_recovery_journal_path_invalid")
        self.path = lexical
        self.backup_root = _private_directory(backup_root, create=True)
        self.config_identity_sha256 = _closed_hash(
            config_identity_sha256,
            "album_recovery_config_identity_invalid",
        )
        self._state: dict[str, Any] | None = None

    def _write(self, core: Mapping[str, Any]) -> None:
        payload = {**core, "journal_sha256": _sha256_bytes(_canonical_json(core))}
        _replace_private_durable(self.path, _canonical_json(payload) + b"\n")
        self._state = dict(core)

    def _read(self) -> dict[str, Any]:
        path = _private_regular_file(
            self.path,
            maximum_bytes=1 << 20,
            code="album_recovery_journal_invalid",
        )
        try:
            payload = _unique_json(path.read_text(encoding="ascii"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseFailure("album_recovery_journal_invalid") from exc
        current_expected = {
            "backup",
            "cas_receipt_sha256",
            "completion_receipt_sha256",
            "config_identity_sha256",
            "config_identity_schema",
            "journal_sha256",
            "phase",
            "plan_sha256",
            "release",
            "schema",
            "transaction_id",
        }
        payload_keys = set(payload)
        if payload_keys != current_expected:
            raise ReleaseFailure("album_recovery_journal_invalid")
        supplied = str(payload.pop("journal_sha256") or "")
        if supplied != _sha256_bytes(_canonical_json(payload)):
            raise ReleaseFailure("album_recovery_journal_digest_mismatch")
        phase = str(payload.get("phase") or "")
        cas_receipt_sha256 = str(payload.get("cas_receipt_sha256") or "")
        completion_receipt_sha256 = str(payload.get("completion_receipt_sha256") or "")
        if (
            payload.get("schema") != ALBUM_RECOVERY_JOURNAL_SCHEMA
            or phase not in _ALBUM_RECOVERY_PHASES
            or payload.get("plan_sha256") != HISTORICAL_ALBUM_PLAN_SHA256
            or _HEX64.fullmatch(str(payload.get("transaction_id") or "")) is None
            or payload.get("config_identity_schema") != RUNTIME_CONFIG_SCHEMA_V2
            or (
                phase
                in {
                    "cas_complete",
                    "bridge_start_attempted",
                    "bridge_accepted",
                    "complete",
                }
            )
            != (_HEX64.fullmatch(cas_receipt_sha256) is not None)
            or (phase == "complete") != (_HEX64.fullmatch(completion_receipt_sha256) is not None)
        ):
            raise ReleaseFailure("album_recovery_journal_invalid")
        if payload.get("config_identity_sha256") != self.config_identity_sha256:
            raise ReleaseFailure("album_recovery_journal_invalid")
        _validate_journal_release_record(
            payload.get("release"),
            code="album_recovery_journal_invalid",
        )
        backup = payload.get("backup")
        if phase in {"prepared", "bridge_stop_attempted", "bridge_quiesced"}:
            if backup is not None:
                raise ReleaseFailure("album_recovery_journal_invalid")
        elif (
            not isinstance(backup, dict)
            or set(backup) != {"directory", "receipt_sha256"}
            or not Path(str(backup.get("directory") or "")).is_absolute()
            or _HEX64.fullmatch(str(backup.get("receipt_sha256") or "")) is None
        ):
            raise ReleaseFailure("album_recovery_journal_invalid")
        self._state = payload
        return dict(payload)

    def begin_or_resume(self, release: ReleaseIdentity) -> Mapping[str, Any]:
        expected_release = _journal_release(release)
        if self.path.exists() or self.path.is_symlink():
            current = self._read()
            if current["release"] != expected_release:
                raise ReleaseFailure("album_recovery_release_identity_changed")
            return current
        core = {
            "schema": ALBUM_RECOVERY_JOURNAL_SCHEMA,
            "transaction_id": os.urandom(32).hex(),
            "phase": "prepared",
            "config_identity_sha256": self.config_identity_sha256,
            "config_identity_schema": RUNTIME_CONFIG_SCHEMA_V2,
            "release": expected_release,
            "plan_sha256": HISTORICAL_ALBUM_PLAN_SHA256,
            "backup": None,
            "cas_receipt_sha256": "",
            "completion_receipt_sha256": "",
        }
        self._write(core)
        return core

    def record(
        self,
        phase: str,
        *,
        backup: _ExactInboxBackup | None = None,
        cas_receipt_sha256: str = "",
        completion_receipt_sha256: str = "",
    ) -> None:
        if phase not in _ALBUM_RECOVERY_PHASES:
            raise ReleaseFailure("album_recovery_journal_phase_invalid")
        state = dict(self._state or self._read())
        current_index = _ALBUM_RECOVERY_PHASES.index(str(state["phase"]))
        if _ALBUM_RECOVERY_PHASES.index(phase) != current_index + 1:
            raise ReleaseFailure("album_recovery_journal_transition_invalid")
        state["phase"] = phase
        if backup is not None:
            if state.get("backup") is not None:
                raise ReleaseFailure("album_recovery_journal_backup_changed")
            state["backup"] = {
                "directory": str(backup.directory),
                "receipt_sha256": backup.receipt_sha256,
            }
        if cas_receipt_sha256:
            digest = _closed_hash(
                cas_receipt_sha256,
                "album_recovery_journal_receipt_invalid",
            )
            existing = str(state.get("cas_receipt_sha256") or "")
            if existing and existing != digest:
                raise ReleaseFailure("album_recovery_journal_receipt_changed")
            state["cas_receipt_sha256"] = digest
        if completion_receipt_sha256:
            digest = _closed_hash(
                completion_receipt_sha256,
                "album_recovery_journal_completion_receipt_invalid",
            )
            existing = str(state.get("completion_receipt_sha256") or "")
            if existing and existing != digest:
                raise ReleaseFailure("album_recovery_journal_completion_receipt_changed")
            state["completion_receipt_sha256"] = digest
        cas_digest_present = _HEX64.fullmatch(str(state.get("cas_receipt_sha256") or "")) is not None
        completion_digest_present = (
            _HEX64.fullmatch(str(state.get("completion_receipt_sha256") or "")) is not None
        )
        if (
            phase
            in {
                "cas_complete",
                "bridge_start_attempted",
                "bridge_accepted",
                "complete",
            }
        ) != cas_digest_present or (phase == "complete") != completion_digest_present:
            raise ReleaseFailure("album_recovery_journal_receipt_invalid")
        self._write(state)

    def load(self) -> Mapping[str, Any]:
        return self._read()

    def backup(self, config: SystemdConfig) -> _ExactInboxBackup | None:
        state = dict(self._state or self._read())
        raw = state.get("backup")
        if raw is None:
            return None
        backup = _ExactInboxBackup(
            Path(str(raw["directory"])),
            str(raw["receipt_sha256"]),
        )
        if backup.directory.parent != self.backup_root:
            raise ReleaseFailure("album_recovery_journal_backup_invalid")
        return _verify_exact_inbox_backup(config, backup)


def _obsidian_tree_matches_manifest(root: Path, manifest: Mapping[str, Any]) -> bool:
    try:
        expected_present, expected_directories, expected_files = _validate_obsidian_manifest(manifest)
        actual, _identities = _capture_obsidian_tree(root, destination=None)
        actual_present, actual_directories, actual_files = _validate_obsidian_manifest(actual)
    except (OSError, ReleaseFailure):
        return False
    if actual_present != expected_present:
        return False
    if not expected_present:
        return True
    if actual.get("root") != manifest.get("root"):
        return False
    return actual_directories == expected_directories and actual_files == expected_files


def _build_obsidian_restore_staging(
    backup_directory: Path,
    staging: Path,
    manifest: Mapping[str, Any],
) -> None:
    present, directories, files = _validate_obsidian_manifest(manifest)
    if not present:
        return
    staging.mkdir(mode=0o700)
    os.chmod(staging, 0o700)
    try:
        for relative, _item in sorted(
            directories.items(),
            key=lambda pair: (len(PurePosixPath(pair[0]).parts), pair[0]),
        ):
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            destination.mkdir(mode=0o700)
            os.chmod(destination, 0o700)
        for relative, item in sorted(files.items()):
            source = (backup_directory / "obsidian-root").joinpath(*PurePosixPath(relative).parts)
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            _copy_private(source, destination)
            os.chmod(destination, int(item["mode"]))
            os.utime(
                destination,
                ns=(int(item["mtime_ns"]), int(item["mtime_ns"])),
                follow_symlinks=False,
            )
        for relative, item in sorted(
            directories.items(),
            key=lambda pair: (-len(PurePosixPath(pair[0]).parts), pair[0]),
        ):
            destination = staging.joinpath(*PurePosixPath(relative).parts)
            os.chmod(destination, int(item["mode"]))
            os.utime(
                destination,
                ns=(int(item["mtime_ns"]), int(item["mtime_ns"])),
                follow_symlinks=False,
            )
        root = manifest["root"]
        os.chmod(staging, int(root["mode"]))
        os.utime(
            staging,
            ns=(int(root["mtime_ns"]), int(root["mtime_ns"])),
            follow_symlinks=False,
        )
        _fsync_tree(staging)
    except BaseException:
        # Never erase an ambiguous partial tree.  Give it a unique forensic name;
        # a replay can create a fresh deterministic staging directory.
        failed = staging.with_name(f"{staging.name}.failed-{os.urandom(8).hex()}")
        with suppress(OSError):
            os.replace(staging, failed)
            _fsync_directory(staging.parent)
        raise


def _restore_exact_obsidian_backup(config: SystemdConfig, payload: _ExactBackupPayload) -> None:
    descriptor = payload.obsidian
    if descriptor is None:
        # Compatibility for recovery of an unfinished pre-Obsidian V2 journal.
        return
    manifest = _verify_obsidian_backup(payload.directory, descriptor)
    root, _present = _validate_obsidian_source_root(
        _obsidian_root(config),
        allow_absent=True,
    )
    if _obsidian_tree_matches_manifest(root, manifest):
        return
    parent = _private_directory(root.parent, create=True)
    token = _sha256_bytes(f"{payload.directory}:{descriptor.manifest_sha256}".encode())[:20]
    staging = parent / f".{root.name}.friday-restore-{token}.new"
    quarantine = parent / f".{root.name}.friday-restore-{token}.old"
    desired_present = bool(manifest["present"])

    if (staging.exists() or staging.is_symlink()) and (
        staging.is_symlink() or not _obsidian_tree_matches_manifest(staging, manifest)
    ):
        if staging.is_symlink():
            raise ReleaseFailure("obsidian_restore_staging_unsafe")
        failed = staging.with_name(f"{staging.name}.failed-{os.urandom(8).hex()}")
        os.replace(staging, failed)
        _fsync_directory(parent)
    if desired_present and not staging.exists():
        _build_obsidian_restore_staging(payload.directory, staging, manifest)
        if not _obsidian_tree_matches_manifest(staging, manifest):
            raise ReleaseFailure("obsidian_restore_staging_mismatch")
    _verify_obsidian_backup(payload.directory, descriptor)

    target_exists = root.exists() or root.is_symlink()
    if target_exists:
        if root.is_symlink():
            raise ReleaseFailure("obsidian_restore_target_unsafe")
        root_status = os.stat(root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(root_status.st_mode)
            or root_status.st_uid != os.geteuid()
            or stat.S_IMODE(root_status.st_mode) & 0o077
        ):
            raise ReleaseFailure("obsidian_restore_target_unsafe")
        if quarantine.exists() or quarantine.is_symlink():
            raise ReleaseFailure("obsidian_restore_quarantine_conflict")
        os.replace(root, quarantine)
        _fsync_directory(parent)
    elif quarantine.is_symlink():
        raise ReleaseFailure("obsidian_restore_quarantine_conflict")

    if desired_present:
        if not staging.exists() or staging.is_symlink():
            raise ReleaseFailure("obsidian_restore_staging_missing")
        os.replace(staging, root)
        _fsync_directory(parent)
        if not _obsidian_tree_matches_manifest(root, manifest):
            raise ReleaseFailure("obsidian_restore_target_mismatch")
    elif root.exists() or root.is_symlink():
        raise ReleaseFailure("obsidian_restore_target_mismatch")


def _restore_exact_sqlite_backup(config: SystemdConfig, backup: DatabaseBackup) -> None:
    payload = backup.opaque
    if not isinstance(payload, _ExactBackupPayload):
        raise ReleaseFailure("backup_restore_identity_invalid")
    declared = {name: (digest, size) for name, digest, size in payload.files}
    for name, (digest, size) in declared.items():
        source = _private_regular_file(
            payload.directory / name,
            maximum_bytes=1 << 40,
            code="backup_restore_source_changed",
        )
        if source.stat().st_size != size or _sha256_file(source) != digest:
            raise ReleaseFailure("backup_restore_source_changed")
    if payload.obsidian is not None:
        # Prove every component before mutating any live state.  The second
        # verification in the root restore closes drift during DB replacement.
        _verify_obsidian_backup(payload.directory, payload.obsidian)
    for label, destination in (("database", config.database), ("inbox", config.inbox_database)):
        _private_directory(destination.parent)
        for suffix in ("", "-wal", "-shm"):
            name = f"{label}.sqlite3{suffix}"
            active = Path(f"{destination}{suffix}")
            if active.is_symlink():
                raise ReleaseFailure("backup_restore_target_unsafe")
            if active.exists():
                _private_regular_file(
                    active,
                    maximum_bytes=1 << 40,
                    code="backup_restore_target_unsafe",
                )
            if name not in declared:
                active.unlink(missing_ok=True)
                continue
            temporary = active.with_name(f".{active.name}.restore-{os.getpid()}")
            if temporary.exists() or temporary.is_symlink():
                raise ReleaseFailure("backup_restore_staging_exists")
            _copy_private(payload.directory / name, temporary)
            os.replace(temporary, active)
            os.chmod(active, 0o600)
        directory_fd = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    if _sqlite_integrity(config.database, require_schema=True) != backup.schema_version:
        raise ReleaseFailure("restored_database_schema_changed")
    _sqlite_integrity(config.inbox_database, require_schema=False)
    _restore_exact_obsidian_backup(config, payload)


def _atomic_anchor_root(anchor: Path, release_root: Path) -> None:
    parent = _private_directory(anchor.parent)
    root = _owned_directory(release_root)
    if anchor.exists() and not anchor.is_symlink():
        raise ReleaseFailure("release_anchor_not_symlink")
    temporary = parent / f".{anchor.name}.{os.getpid()}.new"
    if temporary.exists() or temporary.is_symlink():
        raise ReleaseFailure("release_anchor_staging_exists")
    try:
        os.symlink(root, temporary, target_is_directory=True)
        os.replace(temporary, anchor)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    if not anchor.is_symlink() or anchor.resolve(strict=True) != root:
        raise ReleaseFailure("release_anchor_switch_failed")


def _atomic_anchor(anchor: Path, release: ReleaseIdentity) -> None:
    _atomic_anchor_root(anchor, release.root)


_UNIT_INSTALL_PHASES = (
    "prepared",
    "transition_anchor_active",
    "units_converged",
    "manager_reloaded",
    "previous_anchor_active",
    "complete",
)

_RUNTIME_UNIT_NAMES = ("friday-backend.service", "friday-bridge.service")
_UNIT_DROPIN_NAMES: Mapping[str, tuple[str, ...]] = {
    "friday-backend.service": ("database.conf", "security.conf"),
    "friday-bridge.service": ("database.conf", "dependency.conf", "security.conf"),
}
_UNIT_SURFACE_KEYS = (
    "friday-backend.service",
    "friday-backend.service.d/database.conf",
    "friday-backend.service.d/security.conf",
    "friday-bridge.service",
    "friday-bridge.service.d/database.conf",
    "friday-bridge.service.d/dependency.conf",
    "friday-bridge.service.d/security.conf",
)
_LEGACY_PRIVATE_TMP_SECURITY = b"[Service]\nLimitCORE=0\nPrivateTmp=true\n"
_RECOVERY_PRIVATE_TMP_SECURITY = b"[Service]\nLimitCORE=0\nPrivateTmp=false\n"


def _unit_runtime_directory_name(unit: str) -> str:
    names = {
        "friday-backend.service": "friday-backend-tmp",
        "friday-bridge.service": "friday-bridge-tmp",
    }
    try:
        return names[unit]
    except KeyError as exc:
        raise ReleaseFailure("noncanonical_unit_name") from exc


def _unit_runtime_tmp_directory(unit: str) -> Path:
    return Path("/run/user") / str(os.geteuid()) / _unit_runtime_directory_name(unit)


def _unit_security_dropin(unit: str) -> bytes:
    runtime_name = _unit_runtime_directory_name(unit)
    return (
        "[Service]\n"
        "LimitCORE=0\n"
        "PrivateTmp=false\n"
        "PrivateUsers=false\n"
        f"RuntimeDirectory={runtime_name}\n"
        "RuntimeDirectoryMode=0700\n"
        "RuntimeDirectoryPreserve=no\n"
        f"Environment=TMPDIR={_unit_runtime_tmp_directory(unit)}\n"
    ).encode()


def _unit_surface_path(unit_dir: Path, key: str) -> Path:
    if key not in _UNIT_SURFACE_KEYS:
        raise ReleaseFailure("unit_surface_key_invalid")
    return unit_dir / Path(key)


class DurableUnitInstallJournal:
    """Crash boundary for the only non-atomic part of first unit installation."""

    def __init__(self, path: Path) -> None:
        parent = _private_directory(path.parent)
        lexical = Path(os.path.abspath(path))
        if lexical.parent != parent or lexical.name != "immutable-release-unit-install.v1.json":
            raise ReleaseFailure("unit_install_journal_path_invalid")
        self.path = lexical
        self._state: dict[str, Any] | None = None

    def _write(self, core: Mapping[str, Any]) -> None:
        payload = {**core, "journal_sha256": _sha256_bytes(_canonical_json(core))}
        _replace_private_durable(self.path, _canonical_json(payload) + b"\n")
        self._state = dict(core)

    def _read(self) -> dict[str, Any]:
        path = _private_regular_file(
            self.path,
            maximum_bytes=1 << 20,
            code="unit_install_journal_invalid",
        )
        try:
            payload = _unique_json(path.read_text(encoding="ascii"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseFailure("unit_install_journal_invalid") from exc
        expected = {
            "candidate",
            "candidate_unit_hashes",
            "journal_sha256",
            "phase",
            "previous",
            "receipt_sha256",
            "schema",
            "transaction_id",
            "transition_root",
            "transition_unit_hashes",
        }
        if set(payload) != expected:
            raise ReleaseFailure("unit_install_journal_invalid")
        supplied = str(payload.pop("journal_sha256") or "")
        if supplied != _sha256_bytes(_canonical_json(payload)):
            raise ReleaseFailure("unit_install_journal_digest_mismatch")
        phase = str(payload.get("phase") or "")
        receipt_sha256 = str(payload.get("receipt_sha256") or "")
        if (
            payload.get("schema") != UNIT_INSTALL_JOURNAL_SCHEMA
            or phase not in _UNIT_INSTALL_PHASES
            or _HEX64.fullmatch(str(payload.get("transaction_id") or "")) is None
            or (phase == "complete") != (_HEX64.fullmatch(receipt_sha256) is not None)
            or not Path(str(payload.get("transition_root") or "")).is_absolute()
        ):
            raise ReleaseFailure("unit_install_journal_invalid")
        for key in ("candidate", "previous"):
            _validate_journal_release_record(
                payload.get(key),
                code="unit_install_journal_invalid",
            )
        candidate_hashes = payload.get("candidate_unit_hashes")
        transition_hashes = payload.get("transition_unit_hashes")
        if not isinstance(candidate_hashes, dict) or frozenset(candidate_hashes) not in {
            frozenset(_RUNTIME_UNIT_NAMES),
            frozenset(_UNIT_SURFACE_KEYS),
        }:
            raise ReleaseFailure("unit_install_journal_invalid")
        if not isinstance(transition_hashes, dict) or set(transition_hashes) != set(_RUNTIME_UNIT_NAMES):
            raise ReleaseFailure("unit_install_journal_invalid")
        for hashes in (candidate_hashes, transition_hashes):
            for digest in hashes.values():
                _closed_hash(str(digest or ""), "unit_install_journal_invalid")
        if phase != "complete" and set(candidate_hashes) == set(_RUNTIME_UNIT_NAMES):
            # Legacy two-file transactions may only be read at their durable
            # terminal boundary.  An unfinished one must be resumed by the
            # exact older sealed operator which created it.
            raise ReleaseFailure("unit_install_journal_legacy_unfinished")
        if phase == "complete":
            candidate = payload["candidate"]
            previous = payload["previous"]
            expected_receipt = _sha256_bytes(
                _canonical_json(
                    {
                        "candidate_tree_sha256": candidate["tree_manifest_sha256"],
                        "previous_tree_sha256": previous["tree_manifest_sha256"],
                        "unit_hashes": payload["candidate_unit_hashes"],
                    }
                )
            )
            if receipt_sha256 != expected_receipt:
                raise ReleaseFailure("unit_install_journal_receipt_mismatch")
        self._state = payload
        return dict(payload)

    def begin_or_resume(
        self,
        *,
        candidate: ReleaseIdentity,
        previous: ReleaseIdentity,
        transition_root: Path,
        candidate_unit_hashes: Mapping[str, str],
        transition_unit_hashes: Mapping[str, str],
    ) -> Mapping[str, Any]:
        identity = {
            "candidate": _journal_release(candidate),
            "previous": _journal_release(previous),
            "transition_root": str(transition_root),
            "candidate_unit_hashes": dict(candidate_unit_hashes),
            "transition_unit_hashes": dict(transition_unit_hashes),
        }
        if self.path.exists() or self.path.is_symlink():
            current = self._read()
            current_identity = {key: current[key] for key in identity}
            if current_identity == identity:
                return current
            if current["phase"] != "complete":
                raise ReleaseFailure("unfinished_unit_install_identity_changed")
            if current["candidate"] == identity["candidate"] and set(current["candidate_unit_hashes"]) == set(
                _UNIT_SURFACE_KEYS
            ):
                raise ReleaseFailure("completed_unit_install_identity_changed")
        core = {
            "schema": UNIT_INSTALL_JOURNAL_SCHEMA,
            "transaction_id": os.urandom(32).hex(),
            "phase": "prepared",
            **identity,
            "receipt_sha256": "",
        }
        self._write(core)
        return core

    def record(self, phase: str, *, receipt_sha256: str = "") -> None:
        if phase not in _UNIT_INSTALL_PHASES:
            raise ReleaseFailure("unit_install_journal_phase_invalid")
        state = dict(self._state or self._read())
        current_index = _UNIT_INSTALL_PHASES.index(str(state["phase"]))
        following_index = _UNIT_INSTALL_PHASES.index(phase)
        if following_index != current_index + 1:
            raise ReleaseFailure("unit_install_journal_transition_invalid")
        if (phase == "complete") != bool(receipt_sha256):
            raise ReleaseFailure("unit_install_journal_receipt_invalid")
        state["phase"] = phase
        if receipt_sha256:
            state["receipt_sha256"] = _closed_hash(
                receipt_sha256,
                "unit_install_journal_receipt_invalid",
            )
        self._write(state)

    def load(self) -> Mapping[str, Any]:
        return self._read()


def _require_completed_unit_install(state_dir: Path, candidate: ReleaseIdentity) -> None:
    state = DurableUnitInstallJournal(state_dir / "immutable-release-unit-install.v1.json").load()
    if state.get("phase") != "complete" or state.get("candidate") != _journal_release(candidate):
        raise ReleaseFailure("unit_install_not_complete_for_candidate")


def _unit_exec_argv(content: bytes, *, code: str) -> tuple[str, ...]:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise ReleaseFailure(code) from exc
    values = [line.removeprefix("ExecStart=") for line in text.splitlines() if line.startswith("ExecStart=")]
    if len(values) != 1:
        raise ReleaseFailure(code)
    try:
        argv = tuple(shlex.split(values[0]))
    except ValueError as exc:
        raise ReleaseFailure(code) from exc
    if len(argv) != 8 or argv[1:5] != ("-I", "-B", "-m", "friday.cli") or argv[5] != "--env-file":
        raise ReleaseFailure(code)
    return argv


def _systemd_exec_argv(value: bytes, *, code: str) -> tuple[str, ...] | None:
    if len(value) > 16_384:
        raise ReleaseFailure(code)
    try:
        text = value.decode("utf-8").strip()
    except UnicodeError as exc:
        raise ReleaseFailure(code) from exc
    if not text:
        return None
    if not text.startswith("{ ") or not text.endswith(" }") or "\n" in text or "\r" in text:
        raise ReleaseFailure(code)
    values: dict[str, str] = {}
    for item in text[2:-2].split(" ; "):
        key, separator, raw = item.partition("=")
        if not separator or key in values:
            raise ReleaseFailure(code)
        values[key] = raw
    if (
        tuple(values)
        != (
            "path",
            "argv[]",
            "ignore_errors",
            "start_time",
            "stop_time",
            "pid",
            "code",
            "status",
        )
        or values["ignore_errors"] != "no"
    ):
        raise ReleaseFailure(code)
    try:
        argv = tuple(shlex.split(values["argv[]"]))
    except ValueError as exc:
        raise ReleaseFailure(code) from exc
    if not argv or values["path"] != argv[0]:
        raise ReleaseFailure(code)
    return argv


def _systemd_environment(value: bytes, *, code: str) -> dict[str, str]:
    if len(value) > 65_536:
        raise ReleaseFailure(code)
    try:
        tokens = shlex.split(value.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ReleaseFailure(code) from exc
    environment: dict[str, str] = {}
    for token in tokens:
        key, separator, item = token.partition("=")
        if not separator or key in environment:
            raise ReleaseFailure(code)
        environment[key] = item
    return environment


def _read_owned_unit_surface_file(path: Path, *, code: str) -> bytes:
    resolved = _regular_file(path, maximum_bytes=1 << 20, code=code)
    status = os.stat(resolved, follow_symlinks=False)
    if status.st_uid != os.geteuid() or status.st_nlink != 1 or stat.S_IMODE(status.st_mode) & 0o022:
        raise ReleaseFailure(code)
    return resolved.read_bytes()


def _database_dropin_path(content: bytes) -> Path:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise ReleaseFailure("installed_database_dropin_invalid") from exc
    lines = text.splitlines()
    path_prefix = "Environment=FRIDAY_DATABASE_PATH="
    if len(lines) != 4 or lines[0] != "[Service]" or not lines[1].startswith(path_prefix):
        raise ReleaseFailure("installed_database_dropin_invalid")
    raw_path = lines[1].removeprefix(path_prefix)
    database = Path(raw_path)
    if (
        not raw_path
        or not database.is_absolute()
        or any(character in raw_path for character in "\x00\r\n")
        or lines[2] != "Environment=FRIDAY_DATABASE_MUST_EXIST=1"
        or lines[3] != f"ExecStartPre=/usr/bin/test -s {database}"
        or not text.endswith("\n")
    ):
        raise ReleaseFailure("installed_database_dropin_invalid")
    return database


def _candidate_unit_surface(
    directory: Path,
    main_content: Mapping[str, bytes],
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Return exact target bytes and the safely admitted predecessor surface."""

    current: dict[str, bytes] = {}
    for unit in _RUNTIME_UNIT_NAMES:
        dropin_directory = _owned_directory(directory / f"{unit}.d")
        expected_paths = {dropin_directory / name for name in _UNIT_DROPIN_NAMES[unit]}
        if set(dropin_directory.iterdir()) != expected_paths:
            raise ReleaseFailure("installed_unit_dropin_set_invalid")
        for path in expected_paths:
            key = path.relative_to(directory).as_posix()
            current[key] = _read_owned_unit_surface_file(
                path,
                code="installed_unit_dropin_invalid",
            )

    backend_database = current["friday-backend.service.d/database.conf"]
    bridge_database = current["friday-bridge.service.d/database.conf"]
    if _database_dropin_path(backend_database) != _database_dropin_path(bridge_database):
        raise ReleaseFailure("installed_database_dropins_not_identical")
    if backend_database != bridge_database:
        raise ReleaseFailure("installed_database_dropins_not_identical")
    expected_dependency = b"[Unit]\nWants=friday-backend.service\nAfter=friday-backend.service\n"
    if current["friday-bridge.service.d/dependency.conf"] != expected_dependency:
        raise ReleaseFailure("installed_dependency_dropin_invalid")

    target: dict[str, bytes] = {}
    for unit in _RUNTIME_UNIT_NAMES:
        target[unit] = main_content[unit]
        database_key = f"{unit}.d/database.conf"
        target[database_key] = current[database_key]
        if unit == "friday-bridge.service":
            target[f"{unit}.d/dependency.conf"] = expected_dependency
        security_key = f"{unit}.d/security.conf"
        security = current[security_key]
        if security not in {
            _LEGACY_PRIVATE_TMP_SECURITY,
            _RECOVERY_PRIVATE_TMP_SECURITY,
            _unit_security_dropin(unit),
        }:
            raise ReleaseFailure("installed_security_dropin_invalid")
        target[security_key] = _unit_security_dropin(unit)
    if tuple(target) != _UNIT_SURFACE_KEYS:
        raise ReleaseFailure("unit_surface_identity_invalid")
    return target, current


def _verify_converged_unit_surface(directory: Path, expected: Mapping[str, bytes]) -> None:
    if tuple(expected) != _UNIT_SURFACE_KEYS:
        raise ReleaseFailure("unit_surface_identity_invalid")
    for key, content in expected.items():
        path = _unit_surface_path(directory, key)
        _verify_owned_static_file(path, content, code="installed_unit_surface_drift")
        if stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode) != 0o600:
            raise ReleaseFailure("installed_unit_surface_mode_invalid")


def _unit_surface_environment(directory: Path, unit: str) -> dict[str, str]:
    paths = [directory / unit]
    paths.extend(directory / f"{unit}.d" / name for name in _UNIT_DROPIN_NAMES[unit])
    environment: dict[str, str] = {}
    try:
        for path in paths:
            content = _read_owned_unit_surface_file(path, code="installed_unit_surface_drift")
            for line in content.decode("utf-8").splitlines():
                if not line.startswith("Environment="):
                    continue
                assignments = shlex.split(line.removeprefix("Environment="))
                if not assignments:
                    raise ReleaseFailure("installed_unit_environment_invalid")
                for assignment in assignments:
                    key, separator, value = assignment.partition("=")
                    if not separator or key in environment:
                        raise ReleaseFailure("installed_unit_environment_invalid")
                    environment[key] = value
    except (UnicodeError, ValueError) as exc:
        raise ReleaseFailure("installed_unit_environment_invalid") from exc
    if environment.get("TMPDIR") != str(_unit_runtime_tmp_directory(unit)):
        raise ReleaseFailure("installed_unit_environment_invalid")
    return environment


def _verify_manager_unit_surface(
    directory: Path,
    unit: str,
    expected_argv: Sequence[str],
) -> None:
    manager_argv = _systemd_exec_argv(
        _run_systemctl("show", unit, "--property=ExecStart", "--value").stdout,
        code="systemd_manager_execstart_invalid",
    )
    if manager_argv != tuple(expected_argv):
        raise ReleaseFailure("systemd_manager_execstart_invalid")
    fragment = _run_systemctl("show", unit, "--property=FragmentPath", "--value").stdout
    try:
        fragment_path = Path(fragment.decode("utf-8").strip())
    except UnicodeError as exc:
        raise ReleaseFailure("systemd_manager_fragment_invalid") from exc
    if fragment_path != directory / unit:
        raise ReleaseFailure("systemd_manager_fragment_invalid")
    dropins = _run_systemctl("show", unit, "--property=DropInPaths", "--value").stdout
    try:
        manager_dropins = tuple(Path(item) for item in shlex.split(dropins.decode("utf-8")))
    except (UnicodeError, ValueError) as exc:
        raise ReleaseFailure("systemd_manager_dropins_invalid") from exc
    expected_dropins = tuple(directory / f"{unit}.d" / name for name in _UNIT_DROPIN_NAMES[unit])
    if manager_dropins != expected_dropins:
        raise ReleaseFailure("systemd_manager_dropins_invalid")
    environment = _systemd_environment(
        _run_systemctl("show", unit, "--property=Environment", "--value").stdout,
        code="systemd_manager_environment_invalid",
    )
    if environment != _unit_surface_environment(directory, unit):
        raise ReleaseFailure("systemd_manager_environment_invalid")
    exact_properties = {
        "LimitCORE": b"0",
        "PrivateTmp": b"no",
        "PrivateUsers": b"no",
        "RuntimeDirectory": _unit_runtime_directory_name(unit).encode(),
        "RuntimeDirectoryMode": b"0700",
        "RuntimeDirectoryPreserve": b"no",
    }
    for property_name, expected_value in exact_properties.items():
        actual_value = _run_systemctl(
            "show",
            unit,
            f"--property={property_name}",
            "--value",
        ).stdout.strip()
        if actual_value != expected_value:
            raise ReleaseFailure("systemd_manager_property_invalid")


def _run_systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(  # noqa: S603
        ["/usr/bin/systemctl", "--user", *arguments],
        check=False,
        capture_output=True,
        timeout=45,
    )
    if check and result.returncode != 0:
        raise ReleaseFailure("systemd_command_failed")
    return result


def _unit_effective_root_is(
    argv: Sequence[str],
    *,
    expected: Sequence[str],
    anchor: Path,
    transition_root: Path,
) -> bool:
    if tuple(argv[1:]) != tuple(expected[1:]):
        return False
    direct_python = transition_root / "venv/bin/python"
    anchor_python = anchor / "venv/bin/python"
    if argv[0] == str(direct_python):
        return True
    if argv[0] != str(anchor_python) or not anchor.is_symlink():
        return False
    try:
        return anchor.resolve(strict=True) == transition_root
    except OSError:
        return False


def _replace_unit_file(destination: Path, content: bytes) -> None:
    temporary = destination.parent / f".{destination.name}.{os.getpid()}.{os.urandom(8).hex()}.new"
    try:
        _write_private_durable(temporary, content, final_mode=0o600)
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def install_units(
    candidate: ReleaseIdentity,
    previous: ReleaseIdentity,
    *,
    unit_dir: Path,
    anchor: Path,
    transition_runtime_root: Path,
    transition_unit_hashes: Mapping[str, str],
    journal: DurableUnitInstallJournal,
) -> dict[str, str]:
    """Converge the complete unit surface without mixed runtime roots."""

    _require_venv_relocation_contract(
        candidate,
        code="unit_candidate_venv_relocation_contract_missing",
    )
    directory = _owned_directory(unit_dir)
    transition_root = _owned_directory(transition_runtime_root)
    if set(transition_unit_hashes) != set(_RUNTIME_UNIT_NAMES):
        raise ReleaseFailure("transition_unit_hashes_invalid")
    main_content: dict[str, bytes] = {}
    expected_argv: dict[str, tuple[str, ...]] = {}
    for name in _RUNTIME_UNIT_NAMES:
        source = _regular_file(
            candidate.root / "artifacts" / name,
            maximum_bytes=1 << 20,
            code="release_unit_invalid",
        )
        content = source.read_bytes()
        main_content[name] = content
        expected_argv[name] = _unit_exec_argv(content, code="release_unit_exec_invalid")
        _closed_hash(str(transition_unit_hashes[name]), "transition_unit_hash_invalid")
    expected_content, predecessor_dropins = _candidate_unit_surface(directory, main_content)
    candidate_hashes = {key: _sha256_bytes(content) for key, content in expected_content.items()}
    for name in _RUNTIME_UNIT_NAMES:
        enabled = _run_systemctl("is-enabled", name, check=False)
        if enabled.returncode != 0 or enabled.stdout.strip() != b"enabled":
            raise ReleaseFailure("systemd_unit_not_enabled")
    state = journal.begin_or_resume(
        candidate=candidate,
        previous=previous,
        transition_root=transition_root,
        candidate_unit_hashes=candidate_hashes,
        transition_unit_hashes=transition_unit_hashes,
    )
    phase = str(state["phase"])
    if phase == "complete":
        _run_systemctl("daemon-reload")
        _verify_converged_unit_surface(directory, expected_content)
        for name in _RUNTIME_UNIT_NAMES:
            _verify_manager_unit_surface(directory, name, expected_argv[name])
        if not anchor.is_symlink() or anchor.resolve(strict=True) != previous.root.resolve(strict=True):
            raise ReleaseFailure("completed_unit_anchor_drift")
        return candidate_hashes

    before_manager_reload = _UNIT_INSTALL_PHASES.index(phase) < _UNIT_INSTALL_PHASES.index("manager_reloaded")
    if before_manager_reload:
        for name in _RUNTIME_UNIT_NAMES:
            installed = _regular_file(
                directory / name,
                maximum_bytes=1 << 20,
                code="installed_transition_unit_invalid",
            )
            digest = _sha256_file(installed)
            if digest not in {str(transition_unit_hashes[name]), candidate_hashes[name]}:
                raise ReleaseFailure("installed_transition_unit_digest_mismatch")
        for key, predecessor_content in predecessor_dropins.items():
            installed_content = _read_owned_unit_surface_file(
                _unit_surface_path(directory, key),
                code="installed_transition_dropin_invalid",
            )
            if key.endswith("/security.conf"):
                unit = key.partition(".d/")[0]
                if installed_content not in {
                    _LEGACY_PRIVATE_TMP_SECURITY,
                    _RECOVERY_PRIVATE_TMP_SECURITY,
                    _unit_security_dropin(unit),
                }:
                    raise ReleaseFailure("installed_transition_security_invalid")
            elif installed_content != predecessor_content or installed_content != expected_content[key]:
                raise ReleaseFailure("installed_transition_dropin_digest_mismatch")
        _atomic_anchor_root(anchor, transition_root)
        if phase == "prepared":
            journal.record("transition_anchor_active")
            phase = "transition_anchor_active"
        for name in _RUNTIME_UNIT_NAMES:
            current = (directory / name).read_bytes()
            argv = _unit_exec_argv(current, code="installed_transition_unit_exec_invalid")
            if not _unit_effective_root_is(
                argv,
                expected=expected_argv[name],
                anchor=anchor,
                transition_root=transition_root,
            ):
                raise ReleaseFailure("unit_install_would_mix_runtime_roots")
            manager_argv = _systemd_exec_argv(
                _run_systemctl("show", name, "--property=ExecStart", "--value").stdout,
                code="systemd_transition_execstart_invalid",
            )
            if manager_argv is None or not _unit_effective_root_is(
                manager_argv,
                expected=expected_argv[name],
                anchor=anchor,
                transition_root=transition_root,
            ):
                raise ReleaseFailure("systemd_transition_would_mix_runtime_roots")
        for key, content in expected_content.items():
            _replace_unit_file(_unit_surface_path(directory, key), content)
        _verify_converged_unit_surface(directory, expected_content)
        if phase == "transition_anchor_active":
            journal.record("units_converged")
        _run_systemctl("daemon-reload")
        for name in _RUNTIME_UNIT_NAMES:
            _verify_manager_unit_surface(directory, name, expected_argv[name])
        journal.record("manager_reloaded")
        phase = "manager_reloaded"

    _verify_converged_unit_surface(directory, expected_content)
    for name in _RUNTIME_UNIT_NAMES:
        _verify_manager_unit_surface(directory, name, expected_argv[name])
    _atomic_anchor(anchor, previous)
    if phase == "manager_reloaded":
        journal.record("previous_anchor_active")
    receipt_core = {
        "candidate_tree_sha256": candidate.tree_manifest_sha256,
        "previous_tree_sha256": previous.tree_manifest_sha256,
        "unit_hashes": candidate_hashes,
    }
    receipt_sha256 = _sha256_bytes(_canonical_json(receipt_core))
    journal.record("complete", receipt_sha256=receipt_sha256)
    return candidate_hashes


def _expected_unit_dropins(config: SystemdConfig, unit: str) -> tuple[tuple[Path, bytes], ...]:
    database = (
        "[Service]\n"
        f"Environment=FRIDAY_DATABASE_PATH={config.database}\n"
        "Environment=FRIDAY_DATABASE_MUST_EXIST=1\n"
        f"ExecStartPre=/usr/bin/test -s {config.database}\n"
    ).encode()
    security = _unit_security_dropin(unit)
    directory = config.unit_dir / f"{unit}.d"
    values: list[tuple[Path, bytes]] = [(directory / "database.conf", database)]
    if unit == config.bridge_unit:
        values.append(
            (
                directory / "dependency.conf",
                f"[Unit]\nWants={config.backend_unit}\nAfter={config.backend_unit}\n".encode(),
            )
        )
    values.append((directory / "security.conf", security))
    return tuple(values)


def _verify_owned_static_file(path: Path, expected: bytes, *, code: str) -> None:
    resolved = _regular_file(path, maximum_bytes=1 << 20, code=code)
    status = os.stat(resolved, follow_symlinks=False)
    if (
        status.st_uid != os.geteuid()
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) & 0o022
        or resolved.read_bytes() != expected
    ):
        raise ReleaseFailure(code)


class SystemdActivationPort:
    """Concrete Linux boundary; construction and methods are side-effect free until called."""

    def __init__(self, config: SystemdConfig) -> None:
        if _UNIT.fullmatch(config.backend_unit) is None or _UNIT.fullmatch(config.bridge_unit) is None:
            raise ReleaseFailure("systemd_unit_invalid")
        if config.backend_unit == config.bridge_unit:
            raise ReleaseFailure("systemd_units_not_distinct")
        if config.health_url != "https://127.0.0.1:8000/api/health":
            raise ReleaseFailure("health_endpoint_not_exact_loopback_tls")
        if config.inbox_database != config.state_dir / "telegram-inbox.sqlite3":
            raise ReleaseFailure("inbox_database_not_runtime_queue")
        if config.memory_vault_mode not in MEMORY_VAULT_MODES:
            raise ReleaseFailure("memory_vault_mode_invalid")
        if config.obsidian_mode not in OBSIDIAN_MODES:
            raise ReleaseFailure("obsidian_mode_invalid")
        if config.obsidian_root is not None and not config.obsidian_root.is_absolute():
            raise ReleaseFailure("obsidian_root_invalid")
        ca = _private_regular_file(
            config.health_ca,
            maximum_bytes=1 << 20,
            code="health_ca_invalid",
        )
        if _sha256_file(ca) != _closed_hash(config.health_ca_sha256, "health_ca_digest_invalid"):
            raise ReleaseFailure("health_ca_digest_mismatch")
        _private_directory(config.anchor.parent)
        environment_file = _private_regular_file(
            config.env_file,
            maximum_bytes=1 << 20,
            code="environment_file_invalid",
        )
        predecessor_env_sha256 = _closed_hash(
            config.env_file_sha256,
            "environment_file_digest_invalid",
        )
        canonical_env_sha256 = _sha256_file(environment_file)
        has_next_path = config.next_env_file is not None
        has_next_digest = bool(config.next_env_file_sha256)
        if has_next_path != has_next_digest:
            raise ReleaseFailure("next_environment_arguments_incomplete")
        if bool(config.staged_config_transition) and not has_next_path:
            raise ReleaseFailure("next_environment_arguments_incomplete")
        requested_transition = _requested_staged_config_transition(config)
        next_env_sha256 = (
            _closed_hash(
                config.next_env_file_sha256,
                "next_environment_file_digest_invalid",
            )
            if has_next_digest
            else ""
        )
        if not has_next_path and canonical_env_sha256 != predecessor_env_sha256:
            raise ReleaseFailure("environment_file_digest_mismatch")
        if has_next_path and (
            (requested_transition == _OBSIDIAN_ENABLE_TRANSITION and config.obsidian_mode != "enabled")
            or predecessor_env_sha256 == next_env_sha256
            or canonical_env_sha256 not in {predecessor_env_sha256, next_env_sha256}
        ):
            raise ReleaseFailure("staged_environment_identity_invalid")
        _owned_directory(config.unit_dir)
        for directory in (
            config.friday_home,
            config.database.parent,
            config.inbox_database.parent,
            config.backup_dir,
            config.state_dir,
        ):
            _private_directory(directory, create=directory in {config.backup_dir})
        staged_descriptor: tuple[str, str, Path, str] | None = None
        staged_target: SystemdConfig | None = None
        staged_predecessor: SystemdConfig | None = None
        staged_transition_validation_sha256 = ""
        if config.staged_transition_validation_sha256 and config.next_env_file is None:
            raise ReleaseFailure("staged_transition_validation_not_permitted")
        if config.next_env_file is not None:
            state_dir = _private_directory(config.state_dir)
            next_env_file = Path(os.path.abspath(config.next_env_file))
            if (
                not config.next_env_file.is_absolute()
                or next_env_file != config.next_env_file
                or next_env_file.parent != state_dir
                or next_env_file
                in {
                    config.env_file,
                    config.database,
                    config.inbox_database,
                    config.health_ca,
                    state_dir / "immutable-release-activation.v1.json",
                    state_dir / "immutable-release-operator.v1.lock",
                }
                or next_env_file in config.alias_claim_manifests
                or any(character in str(next_env_file) for character in "\x00\r\n")
            ):
                raise ReleaseFailure("next_environment_file_path_invalid")
            staged_bytes: bytes | None = None
            if canonical_env_sha256 == predecessor_env_sha256 or (
                next_env_file.exists() or next_env_file.is_symlink()
            ):
                staged_bytes = _read_private_regular_file(
                    next_env_file,
                    maximum_bytes=1 << 20,
                    code="next_environment_file_invalid",
                )
                if _sha256_bytes(staged_bytes) != next_env_sha256:
                    raise ReleaseFailure("next_environment_file_digest_mismatch")
            canonical_bytes = _read_private_regular_file(
                config.env_file,
                maximum_bytes=1 << 20,
                code="environment_file_invalid",
            )
            expected_transition_validation = _staged_transition_validation_sha256(
                requested_transition,
                predecessor_env_sha256,
                next_env_file,
                next_env_sha256,
            )
            if (
                config.staged_transition_validation_sha256
                and requested_transition
                != _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION
            ):
                raise ReleaseFailure("staged_transition_validation_not_permitted")
            if canonical_env_sha256 == predecessor_env_sha256:
                if staged_bytes is None:  # pragma: no cover - read condition above proves it
                    raise ReleaseFailure("next_environment_file_invalid")
                _validate_staged_environment_transition(
                    requested_transition,
                    canonical_bytes,
                    staged_bytes,
                )
                if (
                    config.staged_transition_validation_sha256
                    and config.staged_transition_validation_sha256
                    != expected_transition_validation
                ):
                    raise ReleaseFailure("staged_transition_validation_changed")
                if (
                    requested_transition
                    == _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION
                ):
                    staged_transition_validation_sha256 = (
                        expected_transition_validation
                    )
            else:
                if (
                    requested_transition
                    == _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION
                ):
                    if config.staged_transition_validation_sha256 != (
                        expected_transition_validation
                    ):
                        raise ReleaseFailure("staged_transition_validation_required")
                    _semantic_supervisor_validated_environment(
                        canonical_bytes,
                        mode="canary",
                        invalid_code="semantic_supervisor_canary_environment_invalid",
                    )
                    staged_transition_validation_sha256 = (
                        expected_transition_validation
                    )
                else:
                    if config.staged_transition_validation_sha256:
                        raise ReleaseFailure(
                            "staged_transition_validation_not_permitted"
                        )
                    _validate_staged_environment_transition(
                        requested_transition,
                        None,
                        canonical_bytes,
                    )
            staged_descriptor = (
                requested_transition,
                predecessor_env_sha256,
                next_env_file,
                next_env_sha256,
            )
            staged_target = _activation_target_config(config)
            staged_predecessor = _activation_predecessor_config(config)
        obsidian_root, obsidian_present = _validate_obsidian_source_root(
            _obsidian_root(config),
            allow_absent=config.obsidian_mode == "disabled",
        )
        if config.obsidian_mode == "enabled" and not obsidian_present:
            raise ReleaseFailure("obsidian_root_required")
        protected = (
            config.state_dir,
            config.backup_dir,
            config.database,
            config.inbox_database,
            config.unit_dir,
        )
        for path in protected:
            lexical = Path(os.path.abspath(path))
            if (
                lexical == obsidian_root
                or lexical.is_relative_to(obsidian_root)
                or obsidian_root.is_relative_to(lexical)
            ):
                raise ReleaseFailure("obsidian_root_not_dedicated")
        smoke_root = Path(os.path.abspath(_SMOKE_SCRATCH_ROOT))
        for path in (
            config.friday_home,
            config.state_dir,
            config.database,
            config.inbox_database,
            obsidian_root,
        ):
            lexical = Path(os.path.abspath(path))
            if (
                lexical == smoke_root
                or lexical.is_relative_to(smoke_root)
                or smoke_root.is_relative_to(lexical)
            ):
                raise ReleaseFailure("smoke_scratch_runtime_overlap")
        database = _private_regular_file(config.database, maximum_bytes=1 << 40, code="database_file_invalid")
        inbox = _private_regular_file(
            config.inbox_database,
            maximum_bytes=1 << 40,
            code="inbox_database_file_invalid",
        )
        if database == inbox:
            raise ReleaseFailure("database_paths_not_distinct")
        alias_lengths = (
            len(config.alias_claim_manifests),
            len(config.alias_expected_counts),
            len(config.alias_expected_plan_sha256s),
        )
        if len(set(alias_lengths)) != 1:
            raise ReleaseFailure("alias_repair_arguments_incomplete")
        if config.memory_vault_mode == "disabled" and alias_lengths[0]:
            raise ReleaseFailure("alias_repair_not_allowed_in_body_free_phase")
        if alias_lengths[0] > 64:
            raise ReleaseFailure("alias_repair_claim_bound_exceeded")
        seen_manifests: set[Path] = set()
        seen_plan_digests: set[str] = set()
        for claim_manifest, expected_count, expected_plan_sha256 in zip(
            config.alias_claim_manifests,
            config.alias_expected_counts,
            config.alias_expected_plan_sha256s,
            strict=True,
        ):
            manifest = _private_regular_file(
                claim_manifest,
                maximum_bytes=64 << 10,
                code="alias_claim_manifest_invalid",
            )
            if manifest in seen_manifests:
                raise ReleaseFailure("alias_claim_manifest_duplicate")
            seen_manifests.add(manifest)
            if type(expected_count) is not int or expected_count <= 0:
                raise ReleaseFailure("alias_expected_count_invalid")
            plan_digest = _closed_hash(expected_plan_sha256, "alias_plan_digest_invalid")
            if plan_digest in seen_plan_digests:
                raise ReleaseFailure("alias_plan_digest_duplicate")
            seen_plan_digests.add(plan_digest)
        self.config = config
        self._staged_descriptor = staged_descriptor
        self._staged_target_config = staged_target
        self._staged_predecessor_config = staged_predecessor
        self._staged_transition_validation_sha256 = (
            staged_transition_validation_sha256
        )
        self._semantic_health_transition = (
            staged_descriptor[0]
            if staged_descriptor is not None
            and staged_descriptor[0] in _SEMANTIC_SUPERVISOR_CONFIG_TRANSITIONS
            else ""
        )
        self._leases: list[Any] = []

    def activation_policy_receipt(self) -> Mapping[str, str]:
        return {
            "memory_vault_cutover_phase": (
                "phase_b_body_free"
                if self.config.memory_vault_mode == "disabled"
                else "phase_a_full_owner_bridge"
            ),
            "memory_vault_mode": self.config.memory_vault_mode,
        }

    def _verify_environment_file(self) -> None:
        environment = _read_private_regular_file(
            self.config.env_file,
            maximum_bytes=1 << 20,
            code="environment_file_invalid",
        )
        digest = _sha256_bytes(environment)
        allowed = {self.config.env_file_sha256}
        if self.config.next_env_file is not None and self.config.next_env_file_sha256:
            allowed.add(self.config.next_env_file_sha256)
        if digest not in allowed:
            raise ReleaseFailure("environment_file_changed")

    def _require_staged_descriptor(
        self,
        transition: str,
        predecessor_env_sha256: str,
        next_env_file: Path,
        next_env_file_sha256: str,
    ) -> tuple[str, str, Path, str]:
        supplied = (
            transition,
            _closed_hash(
                predecessor_env_sha256,
                "staged_predecessor_env_digest_invalid",
            ),
            Path(os.path.abspath(next_env_file)),
            _closed_hash(
                next_env_file_sha256,
                "staged_next_env_digest_invalid",
            ),
        )
        if supplied != self._staged_descriptor:
            raise ReleaseFailure("staged_environment_identity_changed")
        return supplied

    def _canonical_environment_digest(self) -> str:
        current = _read_private_regular_file(
            self.config.env_file,
            maximum_bytes=1 << 20,
            code="environment_file_invalid",
        )
        return _sha256_bytes(current)

    @staticmethod
    def _staged_environment_bytes(path: Path, expected_sha256: str) -> bytes:
        staged = _read_private_regular_file(
            path,
            maximum_bytes=1 << 20,
            code="next_environment_file_invalid",
        )
        if _sha256_bytes(staged) != expected_sha256:
            raise ReleaseFailure("next_environment_file_digest_mismatch")
        return staged

    def validate_staged_config_transition(
        self,
        transition: str,
        predecessor_env_sha256: str,
        next_env_file: Path,
        next_env_file_sha256: str,
    ) -> None:
        selected_transition, predecessor, staged_path, next_digest = self._require_staged_descriptor(
            transition,
            predecessor_env_sha256,
            next_env_file,
            next_env_file_sha256,
        )
        canonical = _read_private_regular_file(
            self.config.env_file,
            maximum_bytes=1 << 20,
            code="environment_file_invalid",
        )
        if _sha256_bytes(canonical) != predecessor:
            raise ReleaseFailure("staged_predecessor_environment_changed")
        staged = self._staged_environment_bytes(staged_path, next_digest)
        _validate_staged_environment_transition(selected_transition, canonical, staged)
        if selected_transition == _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION:
            self._staged_transition_validation_sha256 = (
                _staged_transition_validation_sha256(
                    selected_transition,
                    predecessor,
                    staged_path,
                    next_digest,
                )
            )

    def activate_staged_config_transition(
        self,
        transition: str,
        predecessor_env_sha256: str,
        next_env_file: Path,
        next_env_file_sha256: str,
    ) -> None:
        selected_transition, predecessor, staged_path, next_digest = self._require_staged_descriptor(
            transition,
            predecessor_env_sha256,
            next_env_file,
            next_env_file_sha256,
        )
        current = self._canonical_environment_digest()
        if current == predecessor:
            staged = self._staged_environment_bytes(staged_path, next_digest)
            predecessor_bytes = _read_private_regular_file(
                self.config.env_file,
                maximum_bytes=1 << 20,
                code="environment_file_invalid",
            )
            _validate_staged_environment_transition(
                selected_transition,
                predecessor_bytes,
                staged,
            )
            _replace_private_durable(self.config.env_file, staged)
        elif current == next_digest:
            target = (
                self._staged_environment_bytes(staged_path, next_digest)
                if staged_path.exists() or staged_path.is_symlink()
                else _read_private_regular_file(
                    self.config.env_file,
                    maximum_bytes=1 << 20,
                    code="environment_file_invalid",
                )
            )
            if selected_transition == _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION:
                expected_validation = _staged_transition_validation_sha256(
                    selected_transition,
                    predecessor,
                    staged_path,
                    next_digest,
                )
                if self._staged_transition_validation_sha256 != expected_validation:
                    raise ReleaseFailure("staged_transition_validation_required")
                _semantic_supervisor_validated_environment(
                    target,
                    mode="canary",
                    invalid_code="semantic_supervisor_canary_environment_invalid",
                )
            else:
                _validate_staged_environment_transition(
                    selected_transition,
                    None,
                    target,
                )
        else:
            raise ReleaseFailure("staged_canonical_environment_changed")
        if self._canonical_environment_digest() != next_digest:
            raise ReleaseFailure("staged_environment_activation_failed")
        if staged_path.exists() or staged_path.is_symlink():
            self._staged_environment_bytes(staged_path, next_digest)
            try:
                staged_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ReleaseFailure("staged_environment_cleanup_failed") from exc
        # Also fsync an already absent path: this completes an interruption
        # between unlink(2) and the original directory fsync.
        _fsync_directory(staged_path.parent)
        if staged_path.exists() or staged_path.is_symlink():
            raise ReleaseFailure("staged_environment_cleanup_failed")
        if self._staged_target_config is None:  # pragma: no cover - descriptor proves it
            raise ReleaseFailure("staged_environment_identity_changed")
        self.config = self._staged_target_config

    def select_predecessor_config_transition(
        self,
        transition: str,
        predecessor_env_sha256: str,
        next_env_file: Path,
        next_env_file_sha256: str,
    ) -> None:
        selected_transition, predecessor, staged_path, next_digest = self._require_staged_descriptor(
            transition,
            predecessor_env_sha256,
            next_env_file,
            next_env_file_sha256,
        )
        canonical = _read_private_regular_file(
            self.config.env_file,
            maximum_bytes=1 << 20,
            code="environment_file_invalid",
        )
        if _sha256_bytes(canonical) != predecessor:
            raise ReleaseFailure("staged_predecessor_environment_changed")
        staged = self._staged_environment_bytes(staged_path, next_digest)
        _validate_staged_environment_transition(selected_transition, canonical, staged)
        if self._staged_predecessor_config is None:  # pragma: no cover - descriptor proves it
            raise ReleaseFailure("staged_environment_identity_changed")
        self.config = self._staged_predecessor_config

    def _verification_environment(
        self,
        *,
        use_predecessor_config: bool,
    ) -> tuple[SystemdConfig, Path, str, str]:
        descriptor = self._staged_descriptor
        if descriptor is None:
            current = self._canonical_environment_digest()
            if current != self.config.env_file_sha256:
                raise ReleaseFailure("environment_file_changed")
            return self.config, self.config.env_file, self.config.env_file_sha256, current
        _transition, predecessor, staged_path, next_digest = descriptor
        current = self._canonical_environment_digest()
        if use_predecessor_config:
            if current != predecessor or self._staged_predecessor_config is None:
                raise ReleaseFailure("staged_predecessor_environment_changed")
            self._staged_environment_bytes(staged_path, next_digest)
            return self._staged_predecessor_config, self.config.env_file, predecessor, current
        if self._staged_target_config is None:  # pragma: no cover - descriptor proves it
            raise ReleaseFailure("staged_environment_identity_changed")
        if current == predecessor:
            self._staged_environment_bytes(staged_path, next_digest)
            return self._staged_target_config, staged_path, next_digest, current
        if current == next_digest:
            if staged_path.exists() or staged_path.is_symlink():
                self._staged_environment_bytes(staged_path, next_digest)
            return self._staged_target_config, self.config.env_file, next_digest, current
        raise ReleaseFailure("staged_canonical_environment_changed")

    def _systemctl(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return _run_systemctl(*arguments, check=check)

    def verify_release(
        self,
        release: ReleaseIdentity,
        *,
        use_predecessor_config: bool = False,
    ) -> None:
        verification_config, verification_env_file, verification_env_sha256, canonical_before = (
            self._verification_environment(use_predecessor_config=use_predecessor_config)
        )
        verify_release_tree(release)
        _require_obsidian_cutover_contract(
            release,
            code="release_obsidian_cutover_contract_missing",
        )
        legacy_obsidian_release = release.max_schema < 35 and not release.obsidian_cutover_contract
        if legacy_obsidian_release and verification_config.obsidian_mode != "disabled":
            raise ReleaseFailure("release_obsidian_cutover_contract_missing")
        installed_surface_smoke(release)
        script = (
            """
import hashlib, json, logging, os, pathlib, sys
logging.disable(logging.CRITICAL)
os.environ['FRIDAY_ENV_FILE']=sys.argv[1]
from friday.config import load_local_env_file, load_settings, validate_settings
load_local_env_file(); settings=load_settings()
home=pathlib.Path(sys.argv[2]).resolve(strict=True)
state=pathlib.Path(sys.argv[3]).resolve(strict=True)
database=pathlib.Path(sys.argv[4]).resolve(strict=True)
inbox=pathlib.Path(sys.argv[5]).resolve(strict=True)
memory_vault_mode=sys.argv[6]
obsidian_mode=sys.argv[7]
obsidian_root=pathlib.Path(sys.argv[8]).absolute()
obsidian_identity_required=sys.argv[9]=='required'
assert settings.home.resolve(strict=True)==home
assert settings.state_dir.resolve(strict=True)==state
assert settings.database_path.resolve(strict=True)==database
assert settings.database_must_exist is True
assert (settings.state_dir/'telegram-inbox.sqlite3').resolve(strict=True)==inbox
effective_memory_vault_mode=getattr(settings,'memory_vault_mode','full_owner')
"""
            + _OBSIDIAN_SETTINGS_IDENTITY_PROBE
            + """
assert not [problem for problem in validate_settings(settings,production=True) if not problem.startswith('warning: ')]
print(json.dumps({'memory_vault_mode':effective_memory_vault_mode,'obsidian_mode':obsidian_mode,'obsidian_root_sha256':hashlib.sha256(str(obsidian_root).encode()).hexdigest(),'status':'clear'},sort_keys=True,separators=(',',':')))
"""
        )
        if (
            not _release_binds_memory_vault_mode(release)
            and verification_config.memory_vault_mode != "full_owner"
        ):
            raise ReleaseFailure("candidate_memory_vault_mode_contract_missing")
        child_environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"LANG", "LC_ALL", "SSL_CERT_DIR", "SSL_CERT_FILE", "XDG_RUNTIME_DIR"}
        }
        child_environment.update(
            {
                "FRIDAY_ENV_FILE": str(verification_env_file),
                "FRIDAY_HOME": str(verification_config.friday_home),
                "FRIDAY_DATABASE_PATH": str(verification_config.database),
                "FRIDAY_DATABASE_MUST_EXIST": "1",
            }
        )
        result = subprocess.run(  # noqa: S603
            [
                str(release.root / "venv/bin/python"),
                "-I",
                "-B",
                "-c",
                script,
                str(verification_env_file),
                str(verification_config.friday_home),
                str(verification_config.state_dir),
                str(verification_config.database),
                str(verification_config.inbox_database),
                verification_config.memory_vault_mode,
                verification_config.obsidian_mode,
                str(_obsidian_root(verification_config)),
                "legacy" if legacy_obsidian_release else "required",
            ],
            check=False,
            capture_output=True,
            env=child_environment,
            timeout=60,
        )
        expected_receipt = {
            "memory_vault_mode": verification_config.memory_vault_mode,
            "obsidian_mode": verification_config.obsidian_mode,
            "obsidian_root_sha256": _obsidian_root_sha256(verification_config),
            "status": "clear",
        }
        try:
            receipt = _unique_json(result.stdout.decode("ascii"))
        except (UnicodeError, ValueError, json.JSONDecodeError):
            receipt = {}
        if result.returncode != 0 or result.stderr or receipt != expected_receipt:
            raise ReleaseFailure("candidate_runtime_config_identity_mismatch")
        verified = _read_private_regular_file(
            verification_env_file,
            maximum_bytes=1 << 20,
            code="environment_file_invalid",
        )
        if (
            _sha256_bytes(verified) != verification_env_sha256
            or self._canonical_environment_digest() != canonical_before
        ):
            raise ReleaseFailure("environment_file_changed")

    def verify_units(self, candidate: ReleaseIdentity) -> None:
        self._verify_environment_file()
        roles = (
            (self.config.backend_unit, "server"),
            (self.config.bridge_unit, "telegram-bridge"),
        )
        for name, _role in roles:
            if name not in {"friday-backend.service", "friday-bridge.service"}:
                raise ReleaseFailure("noncanonical_unit_name")
            expected_unit = candidate.root / "artifacts" / name
            _verify_owned_static_file(
                self.config.unit_dir / name,
                expected_unit.read_bytes(),
                code="installed_unit_drift",
            )
            expected_dropins = _expected_unit_dropins(self.config, name)
            dropin_directory = _owned_directory(self.config.unit_dir / f"{name}.d")
            if set(dropin_directory.iterdir()) != {path for path, _content in expected_dropins}:
                raise ReleaseFailure("systemd_dropin_set_invalid")
            for path, content in expected_dropins:
                _verify_owned_static_file(path, content, code="systemd_dropin_invalid")
        self._systemctl("daemon-reload")
        for unit, role in roles:
            expected_argv = (
                str(self.config.anchor / "venv/bin/python"),
                "-I",
                "-B",
                "-m",
                "friday.cli",
                "--env-file",
                str(self.config.env_file),
                role,
            )
            manager_argv = _systemd_exec_argv(
                self._systemctl("show", unit, "--property=ExecStart", "--value").stdout,
                code="systemd_manager_execstart_invalid",
            )
            if manager_argv != expected_argv:
                raise ReleaseFailure("systemd_manager_execstart_invalid")
            pre_argv = _systemd_exec_argv(
                self._systemctl("show", unit, "--property=ExecStartPre", "--value").stdout,
                code="systemd_manager_execstartpre_invalid",
            )
            if pre_argv != ("/usr/bin/test", "-s", str(self.config.database)):
                raise ReleaseFailure("systemd_manager_execstartpre_invalid")
            for property_name in (
                "ExecCondition",
                "ExecStartPost",
                "ExecReload",
                "ExecStop",
                "ExecStopPost",
            ):
                if (
                    _systemd_exec_argv(
                        self._systemctl("show", unit, f"--property={property_name}", "--value").stdout,
                        code="systemd_manager_extra_exec_invalid",
                    )
                    is not None
                ):
                    raise ReleaseFailure("systemd_manager_extra_exec_invalid")
            fragment = self._systemctl("show", unit, "--property=FragmentPath", "--value").stdout
            try:
                fragment_path = _regular_file(
                    Path(fragment.decode("utf-8").strip()),
                    maximum_bytes=1 << 20,
                    code="systemd_manager_fragment_invalid",
                )
            except (OSError, UnicodeError) as exc:
                raise ReleaseFailure("systemd_manager_fragment_invalid") from exc
            if fragment_path != self.config.unit_dir / unit:
                raise ReleaseFailure("systemd_manager_fragment_invalid")
            dropin_paths = self._systemctl(
                "show",
                unit,
                "--property=DropInPaths",
                "--value",
            ).stdout
            try:
                manager_dropins = tuple(Path(value) for value in shlex.split(dropin_paths.decode("utf-8")))
            except (UnicodeError, ValueError) as exc:
                raise ReleaseFailure("systemd_manager_dropins_invalid") from exc
            expected_manager_dropins = tuple(
                path for path, _content in _expected_unit_dropins(self.config, unit)
            )
            if manager_dropins != expected_manager_dropins:
                raise ReleaseFailure("systemd_manager_dropins_invalid")
            relevant = _systemd_environment(
                self._systemctl("show", unit, "--property=Environment", "--value").stdout,
                code="systemd_manager_environment_invalid",
            )
            if relevant != {
                "FRIDAY_DATABASE_MUST_EXIST": "1",
                "FRIDAY_DATABASE_PATH": str(self.config.database),
                "FRIDAY_HOME": str(self.config.friday_home),
                "TMPDIR": str(_unit_runtime_tmp_directory(unit)),
            }:
                raise ReleaseFailure("systemd_manager_environment_invalid")
            if (
                self._systemctl(
                    "show",
                    unit,
                    "--property=KillMode",
                    "--value",
                ).stdout.strip()
                != b"control-group"
            ):
                raise ReleaseFailure("systemd_manager_kill_mode_invalid")
            if (
                self._systemctl(
                    "show",
                    unit,
                    "--property=UMask",
                    "--value",
                ).stdout.strip()
                != b"0077"
            ):
                raise ReleaseFailure("systemd_manager_umask_invalid")
            if (
                self._systemctl(
                    "show",
                    unit,
                    "--property=UnitFileState",
                    "--value",
                ).stdout.strip()
                != b"enabled"
            ):
                raise ReleaseFailure("systemd_unit_not_enabled")
            exact_properties = {
                "EnvironmentFiles": b"",
                "LimitCORE": b"0",
                "PrivateTmp": b"no",
                "PrivateUsers": b"no",
                "RuntimeDirectory": _unit_runtime_directory_name(unit).encode(),
                "RuntimeDirectoryMode": b"0700",
                "RuntimeDirectoryPreserve": b"no",
                "UnsetEnvironment": b"PYTHONPATH",
                "WorkingDirectory": str(self.config.friday_home).encode(),
            }
            for property_name, expected_value in exact_properties.items():
                actual_value = self._systemctl(
                    "show",
                    unit,
                    f"--property={property_name}",
                    "--value",
                ).stdout.strip()
                if actual_value != expected_value:
                    raise ReleaseFailure("systemd_manager_property_invalid")
            _verify_owned_static_file(
                self.config.unit_dir / unit,
                (candidate.root / "artifacts" / unit).read_bytes(),
                code="installed_unit_changed_during_attestation",
            )
            for path, content in _expected_unit_dropins(self.config, unit):
                _verify_owned_static_file(
                    path,
                    content,
                    code="systemd_dropin_changed_during_attestation",
                )

    def _secondary_rollout_profile_identity(
        self,
        previous: ReleaseIdentity,
        *,
        expected_stage: str,
    ) -> dict[str, Any]:
        verification_config = self._staged_predecessor_config
        if verification_config is None:
            raise ReleaseFailure("secondary_rollout_config_identity_mismatch")
        environment_before = _read_private_regular_file(
            verification_config.env_file,
            maximum_bytes=1 << 20,
            code="environment_file_invalid",
        )
        environment_sha256 = _sha256_bytes(environment_before)
        if environment_sha256 != verification_config.env_file_sha256:
            raise ReleaseFailure("secondary_rollout_config_identity_mismatch")
        script = """
import json, logging, os, sys
logging.disable(logging.CRITICAL)
os.environ['FRIDAY_ENV_FILE']=sys.argv[1]
from friday.config import load_local_env_file, load_settings
from friday.secondary_brain.profiles import get_secondary_runtime_admission
load_local_env_file(); settings=load_settings()
admission=get_secondary_runtime_admission(settings.secondary_llm_profile,mode=settings.secondary_llm_mode)
assert admission is not None
profile=admission.profile
print(json.dumps({
 'admission':admission.kind.value,
 'allow_private_text':settings.secondary_llm_allow_private_text,
 'context_tokens':settings.secondary_llm_max_context_tokens,
 'gateway_ca_certificate_sha256':profile.gateway_ca_certificate_sha256,
 'manifest_sha256':profile.manifest_sha256,
 'mode':settings.secondary_llm_mode,
 'profile_id':settings.secondary_llm_profile,
 'served_model_alias':settings.secondary_llm_model,
},sort_keys=True,separators=(',',':')))
"""
        child_environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"LANG", "LC_ALL", "SSL_CERT_DIR", "SSL_CERT_FILE", "XDG_RUNTIME_DIR"}
        }
        child_environment.update(
            {
                "FRIDAY_ENV_FILE": str(verification_config.env_file),
                "FRIDAY_HOME": str(verification_config.friday_home),
                "FRIDAY_DATABASE_PATH": str(verification_config.database),
                "FRIDAY_DATABASE_MUST_EXIST": "1",
            }
        )
        try:
            result = subprocess.run(  # noqa: S603
                [
                    str(previous.root / "venv/bin/python"),
                    "-I",
                    "-B",
                    "-c",
                    script,
                    str(verification_config.env_file),
                ],
                check=False,
                capture_output=True,
                env=child_environment,
                timeout=60,
            )
            identity = _unique_json(result.stdout.decode("ascii", errors="strict"))
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseFailure("secondary_rollout_profile_identity_invalid") from exc
        expected_admissions = {"provisional_shadow", "accepted"}
        if expected_stage in {"private-shadow", "document-map-shadow"}:
            expected_admissions = {"accepted"}
        expected_private = expected_stage in {"private-shadow", "document-map-shadow"}
        expected_mode = "assist" if expected_stage == "document-map-shadow" else "shadow"
        if (
            result.returncode != 0
            or result.stderr
            or set(identity)
            != {
                "admission",
                "allow_private_text",
                "context_tokens",
                "gateway_ca_certificate_sha256",
                "manifest_sha256",
                "mode",
                "profile_id",
                "served_model_alias",
            }
            or identity.get("admission") not in expected_admissions
            or identity.get("allow_private_text") is not expected_private
            or identity.get("context_tokens") != 4096
            or identity.get("gateway_ca_certificate_sha256") != _SECONDARY_FINALIST_CA_SHA256
            or _HEX64.fullmatch(str(identity.get("manifest_sha256") or "")) is None
            or identity.get("mode") != expected_mode
            or identity.get("profile_id") != _SECONDARY_FINALIST_PROFILE_ID
            or identity.get("served_model_alias") != _SECONDARY_FINALIST_MODEL_ALIAS
        ):
            raise ReleaseFailure("secondary_rollout_profile_identity_invalid")
        environment_after = _read_private_regular_file(
            verification_config.env_file,
            maximum_bytes=1 << 20,
            code="environment_file_invalid",
        )
        if environment_after != environment_before:
            raise ReleaseFailure("secondary_rollout_config_identity_mismatch")
        return identity

    def _current_backend_process_identity(
        self,
        previous: ReleaseIdentity,
        *,
        proc_root: Path = Path("/proc"),
    ) -> tuple[int, str]:
        def main_pid() -> int:
            result = self._systemctl(
                "show",
                self.config.backend_unit,
                "--property=MainPID",
                "--value",
                check=False,
            )
            try:
                value = int(result.stdout.strip() or b"0")
            except ValueError as exc:
                raise ReleaseFailure("secondary_rollout_process_identity_invalid") from exc
            if result.returncode != 0 or not 2 <= value <= 4_194_304:
                raise ReleaseFailure("secondary_rollout_process_identity_invalid")
            return value

        def boot_id() -> str:
            descriptor = -1
            try:
                descriptor = os.open(
                    proc_root / "sys/kernel/random/boot_id",
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
                raw = os.read(descriptor, 38)
                if not raw or len(raw) > 37 or b"\x00" in raw or os.read(descriptor, 1):
                    raise ReleaseFailure("secondary_rollout_process_identity_invalid")
            except ReleaseFailure:
                raise
            except OSError as exc:
                raise ReleaseFailure("secondary_rollout_process_identity_invalid") from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
            try:
                text = raw.decode("ascii", errors="strict")
            except UnicodeError as exc:
                raise ReleaseFailure("secondary_rollout_process_identity_invalid") from exc
            value = text.strip()
            if _BOOT_ID.fullmatch(value) is None or text not in {value, f"{value}\n"}:
                raise ReleaseFailure("secondary_rollout_process_identity_invalid")
            return value

        pid = main_pid()
        observed_boot_id = boot_id()
        if not self._process_matches(pid, previous, "backend", proc_root=proc_root):
            raise ReleaseFailure("secondary_rollout_process_identity_invalid")
        descriptor = -1
        try:
            descriptor = os.open(
                proc_root / str(pid) / "stat",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            raw = os.read(descriptor, 8193)
            if len(raw) > 8192 or not raw or b"\x00" in raw or os.read(descriptor, 1):
                raise ReleaseFailure("secondary_rollout_process_identity_invalid")
        except ReleaseFailure:
            raise
        except OSError as exc:
            raise ReleaseFailure("secondary_rollout_process_identity_invalid") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            text = raw.decode("ascii", errors="strict")
        except UnicodeError as exc:
            raise ReleaseFailure("secondary_rollout_process_identity_invalid") from exc
        closing = text.rfind(")")
        fields = text[closing + 2 :].split() if closing > 0 else []
        if len(fields) < 20 or not fields[19].isdigit():
            raise ReleaseFailure("secondary_rollout_process_identity_invalid")
        if (
            main_pid() != pid
            or boot_id() != observed_boot_id
            or not self._process_matches(pid, previous, "backend", proc_root=proc_root)
        ):
            raise ReleaseFailure("secondary_rollout_process_identity_changed")
        epoch = (
            _PRIMARY_PROCESS_EPOCH_DOMAIN
            + observed_boot_id.encode("ascii")
            + b"\0"
            + str(pid).encode("ascii")
            + b"\0"
            + fields[19].encode("ascii")
        )
        return pid, _sha256_bytes(epoch)

    def _consume_secondary_rollout_attestation(
        self,
        request_payload: Mapping[str, Any],
        *,
        attestation: Mapping[str, Any],
        api_token: str,
        primary_ca: bytes,
    ) -> None:
        if set(request_payload) != _SECONDARY_PRODUCT_CONSUME_REQUEST_KEYS:
            raise ReleaseFailure("secondary_rollout_consume_request_invalid")
        try:
            context = ssl.create_default_context(cadata=primary_ca.decode("ascii", errors="strict"))
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
                _NoRedirect(),
            )
            request = urllib.request.Request(
                _SECONDARY_PRODUCT_CONSUME_URL,
                data=_secondary_product_canonical(request_payload),
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with opener.open(  # noqa: S310 - exact pinned loopback TLS endpoint
                request,
                timeout=10.0,
            ) as response:
                status = response.status
                response_url = response.geturl()
                raw = response.read(65_537)
        except (OSError, UnicodeError, ValueError, ssl.SSLError, urllib.error.URLError):
            raise ReleaseFailure("secondary_rollout_attestation_consume_failed") from None
        if status != 200 or response_url != _SECONDARY_PRODUCT_CONSUME_URL or len(raw) > 65_536:
            raise ReleaseFailure("secondary_rollout_attestation_consume_failed")
        try:
            payload = _secondary_product_json(raw)
        except ReleaseFailure:
            raise ReleaseFailure("secondary_rollout_consume_response_invalid") from None
        _validate_secondary_rollout_consume_response(
            payload,
            request=request_payload,
            attestation=attestation,
        )

    def _consume_secondary_document_map_rollout_attestation(
        self,
        request_payload: Mapping[str, Any],
        *,
        attestation: Mapping[str, Any],
        api_token: str,
        primary_ca: bytes,
    ) -> None:
        if set(request_payload) != _SECONDARY_DOCUMENT_MAP_CONSUME_REQUEST_KEYS:
            raise ReleaseFailure("secondary_document_map_consume_request_invalid")
        try:
            context = ssl.create_default_context(cadata=primary_ca.decode("ascii", errors="strict"))
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
                _NoRedirect(),
            )
            request = urllib.request.Request(
                _SECONDARY_DOCUMENT_MAP_CONSUME_URL,
                data=_secondary_product_canonical(request_payload),
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with opener.open(  # noqa: S310 - exact pinned loopback TLS endpoint
                request,
                timeout=10.0,
            ) as response:
                status = response.status
                response_url = response.geturl()
                raw = response.read(65_537)
        except (OSError, UnicodeError, ValueError, ssl.SSLError, urllib.error.URLError):
            raise ReleaseFailure("secondary_document_map_attestation_consume_failed") from None
        if status != 200 or response_url != _SECONDARY_DOCUMENT_MAP_CONSUME_URL or len(raw) > 65_536:
            raise ReleaseFailure("secondary_document_map_attestation_consume_failed")
        try:
            payload = _secondary_product_json(raw)
        except ReleaseFailure:
            raise ReleaseFailure("secondary_document_map_consume_response_invalid") from None
        _validate_secondary_document_map_consume_response(
            payload,
            request=request_payload,
            attestation=attestation,
        )

    def _consume_semantic_supervisor_representative_window_attestation(
        self,
        request_payload: Mapping[str, Any],
        *,
        attestation: Mapping[str, Any],
        api_token: str,
        primary_ca: bytes,
    ) -> None:
        if (
            set(request_payload)
            != _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_CONSUME_REQUEST_KEYS
        ):
            raise ReleaseFailure(
                "semantic_supervisor_representative_window_consume_request_invalid"
            )
        try:
            context = ssl.create_default_context(
                cadata=primary_ca.decode("ascii", errors="strict")
            )
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}),
                urllib.request.HTTPSHandler(context=context),
                _NoRedirect(),
            )
            request = urllib.request.Request(
                _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_CONSUME_URL,
                data=_canonical_json(request_payload) + b"\n",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with opener.open(  # noqa: S310 - exact pinned loopback TLS endpoint
                request,
                timeout=10.0,
            ) as response:
                status = response.status
                response_url = response.geturl()
                raw = response.read(65_537)
        except (OSError, UnicodeError, ValueError, ssl.SSLError, urllib.error.URLError):
            raise ReleaseFailure(
                "semantic_supervisor_representative_window_consume_failed"
            ) from None
        if (
            status != 200
            or response_url
            != _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_CONSUME_URL
            or len(raw) > 65_536
        ):
            raise ReleaseFailure(
                "semantic_supervisor_representative_window_consume_failed"
            )
        try:
            payload = _semantic_supervisor_closed_json(
                raw,
                invalid_code=(
                    "semantic_supervisor_representative_window_consume_response_invalid"
                ),
            )
        except ReleaseFailure:
            raise ReleaseFailure(
                "semantic_supervisor_representative_window_consume_response_invalid"
            ) from None
        _validate_semantic_supervisor_representative_window_consume_response(
            payload,
            request=request_payload,
            attestation=attestation,
        )

    def _validate_secondary_document_map_rollout_gate(
        self,
        previous: ReleaseIdentity,
        candidate: ReleaseIdentity,
        *,
        secondary_rollout_receipt: Path,
    ) -> None:
        canonical = _read_private_regular_file(
            self.config.env_file,
            maximum_bytes=1 << 20,
            code="environment_file_invalid",
        )
        if _sha256_bytes(canonical) != self.config.env_file_sha256:
            raise ReleaseFailure("secondary_document_map_config_identity_mismatch")
        predecessor_env_sha256 = _sha256_bytes(canonical)
        predecessor_env_path = Path(os.path.abspath(self.config.env_file))
        predecessor_anchor_path = Path(os.path.abspath(self.config.anchor))
        if predecessor_env_path != self.config.env_file or predecessor_anchor_path != self.config.anchor:
            raise ReleaseFailure("secondary_document_map_config_identity_mismatch")
        predecessor_env_path_sha256 = _sha256_bytes(str(predecessor_env_path).encode("utf-8"))
        predecessor_anchor_path_sha256 = _sha256_bytes(str(predecessor_anchor_path).encode("utf-8"))
        predecessor_metadata_path = Path(os.path.abspath(previous.root / "artifacts/immutable-release.json"))
        expected_metadata_path = Path(os.path.abspath(previous.root)) / "artifacts/immutable-release.json"
        if predecessor_metadata_path != expected_metadata_path:
            raise ReleaseFailure("secondary_document_map_predecessor_release_identity_invalid")
        predecessor_metadata = _read_stable_regular_file(
            predecessor_metadata_path,
            maximum_bytes=1 << 20,
            code="secondary_document_map_predecessor_release_identity_invalid",
        )
        try:
            predecessor_metadata_value = _unique_json(predecessor_metadata.decode("ascii", errors="strict"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseFailure("secondary_document_map_predecessor_release_identity_invalid") from exc
        if (
            not isinstance(predecessor_metadata_value, dict)
            or predecessor_metadata_value.get("commit") != previous.commit
            or predecessor_metadata_value.get("version") != previous.version
            or predecessor_metadata_value.get("max_schema") != previous.max_schema
        ):
            raise ReleaseFailure("secondary_document_map_predecessor_release_identity_invalid")
        predecessor_metadata_sha256 = _sha256_bytes(predecessor_metadata)
        predecessor_wheel_sha256 = _closed_hash(
            str(predecessor_metadata_value.get("wheel_sha256") or ""),
            "secondary_document_map_predecessor_release_identity_invalid",
        )
        values, unrelated = _secondary_environment_view(canonical)
        _validate_secondary_finalist_values(
            values,
            exact_values=_SECONDARY_DOCUMENT_MAP_SHADOW_EXACT_VALUES,
            invalid_code="secondary_document_map_config_identity_mismatch",
        )
        if canonical != _canonical_secondary_environment(unrelated, values):
            raise ReleaseFailure("secondary_document_map_config_identity_mismatch")
        api_token = _secondary_rollout_api_token(canonical)
        secondary_ca_path = Path(values["FRIDAY_SECONDARY_LLM_CA_FILE"])
        secondary_ca = _read_private_regular_file(
            secondary_ca_path,
            maximum_bytes=1 << 20,
            code="secondary_shadow_ca_invalid",
        )
        if _sha256_bytes(secondary_ca) != _SECONDARY_FINALIST_CA_SHA256:
            raise ReleaseFailure("secondary_shadow_ca_digest_mismatch")
        primary_ca = _read_private_regular_file(
            self.config.health_ca,
            maximum_bytes=1 << 20,
            code="health_ca_invalid",
        )
        primary_ca_sha256 = _sha256_bytes(primary_ca)
        if primary_ca_sha256 != self.config.health_ca_sha256:
            raise ReleaseFailure("health_ca_digest_mismatch")
        next_env_file = self.config.next_env_file
        if next_env_file is None:
            raise ReleaseFailure("secondary_document_map_next_environment_invalid")
        next_environment = _read_private_regular_file(
            next_env_file,
            maximum_bytes=1 << 20,
            code="secondary_document_map_next_environment_invalid",
        )
        next_env_sha256 = _closed_hash(
            self.config.next_env_file_sha256,
            "secondary_document_map_next_environment_invalid",
        )
        if _sha256_bytes(next_environment) != next_env_sha256:
            raise ReleaseFailure("secondary_document_map_next_environment_invalid")
        transition = _requested_staged_config_transition(self.config)
        if transition != _SECONDARY_DOCUMENT_MAP_SHADOW_TO_ASSIST_TRANSITION:
            raise ReleaseFailure("secondary_document_map_transition_invalid")
        _validate_staged_environment_transition(transition, canonical, next_environment)
        receipt = _load_secondary_rollout_receipt(
            secondary_rollout_receipt,
            self.config.secondary_rollout_receipt_sha256,
        )
        profile_identity = self._secondary_rollout_profile_identity(
            previous,
            expected_stage="document-map-shadow",
        )
        primary_pid, process_epoch = self._current_backend_process_identity(previous)
        attestation = _validate_secondary_document_map_rollout_receipt(
            receipt,
            receipt_sha256=self.config.secondary_rollout_receipt_sha256,
            previous=previous,
            predecessor_release_metadata_sha256=predecessor_metadata_sha256,
            predecessor_release_wheel_sha256=predecessor_wheel_sha256,
            predecessor_live_env_sha256=predecessor_env_sha256,
            predecessor_live_env_path_sha256=predecessor_env_path_sha256,
            predecessor_release_anchor_path_sha256=predecessor_anchor_path_sha256,
            profile_identity=profile_identity,
            primary_pid=primary_pid,
            primary_process_epoch_sha256=process_epoch,
            primary_ca_certificate_sha256=primary_ca_sha256,
        )

        def anchor_is_exact_predecessor() -> bool:
            try:
                return self.config.anchor.is_symlink() and self.config.anchor.resolve(
                    strict=True
                ) == previous.root.resolve(strict=True)
            except OSError:
                return False

        def recheck_identity() -> None:
            if (
                _read_private_regular_file(
                    self.config.env_file,
                    maximum_bytes=1 << 20,
                    code="environment_file_invalid",
                )
                != canonical
                or _read_stable_regular_file(
                    predecessor_metadata_path,
                    maximum_bytes=1 << 20,
                    code="secondary_document_map_predecessor_release_identity_invalid",
                )
                != predecessor_metadata
                or not anchor_is_exact_predecessor()
                or _read_private_regular_file(
                    self.config.health_ca,
                    maximum_bytes=1 << 20,
                    code="health_ca_invalid",
                )
                != primary_ca
                or _read_private_regular_file(
                    secondary_ca_path,
                    maximum_bytes=1 << 20,
                    code="secondary_shadow_ca_invalid",
                )
                != secondary_ca
                or _read_private_regular_file(
                    next_env_file,
                    maximum_bytes=1 << 20,
                    code="secondary_document_map_next_environment_invalid",
                )
                != next_environment
                or _load_secondary_rollout_receipt(
                    secondary_rollout_receipt,
                    self.config.secondary_rollout_receipt_sha256,
                )
                != receipt
                or self._secondary_rollout_profile_identity(
                    previous,
                    expected_stage="document-map-shadow",
                )
                != profile_identity
                or self._current_backend_process_identity(previous) != (primary_pid, process_epoch)
            ):
                raise ReleaseFailure("secondary_document_map_rollout_identity_changed")

        attestation_sha256 = _sha256_bytes(_secondary_product_canonical(attestation))
        request_payload = _secondary_document_map_rollout_consume_request(
            lookup_token=str(receipt["server_rollout_lookup_token"]),
            attestation_sha256=attestation_sha256,
            previous=previous,
            candidate=candidate,
            predecessor_env_sha256=predecessor_env_sha256,
            next_env_sha256=next_env_sha256,
            product_receipt_sha256=self.config.secondary_rollout_receipt_sha256,
        )
        _consume_secondary_document_map_after_exact_rechecks(
            previous,
            recheck_identity=recheck_identity,
            consume=lambda: self._consume_secondary_document_map_rollout_attestation(
                request_payload,
                attestation=attestation,
                api_token=api_token,
                primary_ca=primary_ca,
            ),
        )

    def _validate_secondary_rollout_gate(
        self,
        previous: ReleaseIdentity,
        candidate: ReleaseIdentity,
    ) -> None:
        expected_stage = _secondary_rollout_receipt_stage(self.config)
        if expected_stage is None:
            return
        secondary_rollout_receipt = self.config.secondary_rollout_receipt
        assert secondary_rollout_receipt is not None
        if expected_stage == "document-map-shadow":
            self._validate_secondary_document_map_rollout_gate(
                previous,
                candidate,
                secondary_rollout_receipt=secondary_rollout_receipt,
            )
            return
        canonical = _read_private_regular_file(
            self.config.env_file,
            maximum_bytes=1 << 20,
            code="environment_file_invalid",
        )
        if _sha256_bytes(canonical) != self.config.env_file_sha256:
            raise ReleaseFailure("secondary_rollout_config_identity_mismatch")
        values, unrelated = _secondary_environment_view(canonical)
        exact_values = (
            _SECONDARY_SHADOW_EXACT_VALUES
            if expected_stage == "public-shadow"
            else _SECONDARY_PRIVATE_SHADOW_EXACT_VALUES
        )
        _validate_secondary_finalist_values(
            values,
            exact_values=exact_values,
            invalid_code="secondary_rollout_config_identity_mismatch",
        )
        if canonical != _canonical_secondary_environment(unrelated, values):
            raise ReleaseFailure("secondary_rollout_config_identity_mismatch")
        api_token = _secondary_rollout_api_token(canonical)
        secondary_ca_path = Path(values["FRIDAY_SECONDARY_LLM_CA_FILE"])
        secondary_ca = _read_private_regular_file(
            secondary_ca_path,
            maximum_bytes=1 << 20,
            code="secondary_shadow_ca_invalid",
        )
        if _sha256_bytes(secondary_ca) != _SECONDARY_FINALIST_CA_SHA256:
            raise ReleaseFailure("secondary_shadow_ca_digest_mismatch")
        primary_ca = _read_private_regular_file(
            self.config.health_ca,
            maximum_bytes=1 << 20,
            code="health_ca_invalid",
        )
        primary_ca_sha256 = _sha256_bytes(primary_ca)
        if primary_ca_sha256 != self.config.health_ca_sha256:
            raise ReleaseFailure("health_ca_digest_mismatch")
        next_env_file = self.config.next_env_file
        if next_env_file is None:
            raise ReleaseFailure("secondary_rollout_next_environment_invalid")
        next_environment = _read_private_regular_file(
            next_env_file,
            maximum_bytes=1 << 20,
            code="secondary_rollout_next_environment_invalid",
        )
        next_env_sha256 = _closed_hash(
            self.config.next_env_file_sha256,
            "secondary_rollout_next_environment_invalid",
        )
        if _sha256_bytes(next_environment) != next_env_sha256:
            raise ReleaseFailure("secondary_rollout_next_environment_invalid")
        transition = _requested_staged_config_transition(self.config)
        _validate_staged_environment_transition(transition, canonical, next_environment)
        receipt = _load_secondary_rollout_receipt(
            secondary_rollout_receipt,
            self.config.secondary_rollout_receipt_sha256,
        )
        observer_runner_sha256 = _secondary_product_runner_artifact_sha256(previous)
        profile_identity = self._secondary_rollout_profile_identity(
            previous,
            expected_stage=expected_stage,
        )
        primary_pid, process_epoch = self._current_backend_process_identity(previous)
        attestation = _validate_secondary_rollout_receipt(
            receipt,
            expected_stage=expected_stage,
            previous=previous,
            observer_runner_sha256=observer_runner_sha256,
            profile_identity=profile_identity,
            primary_pid=primary_pid,
            primary_process_epoch_sha256=process_epoch,
            primary_ca_certificate_sha256=primary_ca_sha256,
        )

        def recheck_identity() -> None:
            if (
                _read_private_regular_file(
                    self.config.env_file,
                    maximum_bytes=1 << 20,
                    code="environment_file_invalid",
                )
                != canonical
                or _read_private_regular_file(
                    self.config.health_ca,
                    maximum_bytes=1 << 20,
                    code="health_ca_invalid",
                )
                != primary_ca
                or _read_private_regular_file(
                    secondary_ca_path,
                    maximum_bytes=1 << 20,
                    code="secondary_shadow_ca_invalid",
                )
                != secondary_ca
                or _read_private_regular_file(
                    next_env_file,
                    maximum_bytes=1 << 20,
                    code="secondary_rollout_next_environment_invalid",
                )
                != next_environment
                or _load_secondary_rollout_receipt(
                    secondary_rollout_receipt,
                    self.config.secondary_rollout_receipt_sha256,
                )
                != receipt
                or _secondary_product_runner_artifact_sha256(previous) != observer_runner_sha256
                or self._secondary_rollout_profile_identity(
                    previous,
                    expected_stage=expected_stage,
                )
                != profile_identity
                or self._current_backend_process_identity(previous) != (primary_pid, process_epoch)
            ):
                raise ReleaseFailure("secondary_rollout_identity_changed")

        request_payload = _secondary_rollout_consume_request(
            lookup_token=str(receipt["server_rollout_lookup_token"]),
            stage=expected_stage,
            transition=transition,
            previous=previous,
            candidate=candidate,
            next_env_sha256=next_env_sha256,
            product_receipt_sha256=self.config.secondary_rollout_receipt_sha256,
            sealed_runner_sha256=observer_runner_sha256,
            server_rollout_attestation_sha256=_sha256_bytes(_secondary_product_canonical(attestation)),
        )
        recheck_identity()
        self._consume_secondary_rollout_attestation(
            request_payload,
            attestation=attestation,
            api_token=api_token,
            primary_ca=primary_ca,
        )
        recheck_identity()

    def _validate_semantic_supervisor_representative_window_gate(
        self,
        candidate: ReleaseIdentity,
    ) -> None:
        transition = _requested_staged_config_transition(self.config)
        if transition not in {
            _SEMANTIC_SUPERVISOR_SHADOW_TO_ASSIST_TRANSITION,
            _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION,
        }:
            return
        target_mode = (
            "assist"
            if transition == _SEMANTIC_SUPERVISOR_SHADOW_TO_ASSIST_TRANSITION
            else "canary"
        )
        predecessor = _read_private_regular_file(
            self.config.env_file,
            maximum_bytes=1 << 20,
            code="semantic_supervisor_predecessor_environment_invalid",
        )
        if _sha256_bytes(predecessor) != self.config.env_file_sha256:
            raise ReleaseFailure("semantic_supervisor_predecessor_environment_invalid")
        next_env_file = self.config.next_env_file
        if next_env_file is None:
            raise ReleaseFailure("semantic_supervisor_next_environment_invalid")
        target = _read_private_regular_file(
            next_env_file,
            maximum_bytes=1 << 20,
            code="semantic_supervisor_next_environment_invalid",
        )
        next_env_sha256 = _closed_hash(
            self.config.next_env_file_sha256,
            "semantic_supervisor_next_environment_invalid",
        )
        if _sha256_bytes(target) != next_env_sha256:
            raise ReleaseFailure("semantic_supervisor_next_environment_invalid")
        _validate_staged_environment_transition(transition, predecessor, target)
        target_values, _unrelated, _secondary = (
            _canonical_semantic_supervisor_environment_parts(
                target,
                invalid_code=f"semantic_supervisor_{target_mode}_environment_invalid",
            )
        )
        expected_precursor = None
        if target_mode == "canary":
            predecessor_values, _predecessor_unrelated, _predecessor_secondary = (
                _canonical_semantic_supervisor_environment_parts(
                    predecessor,
                    invalid_code="semantic_supervisor_assist_environment_invalid",
                )
            )
            expected_precursor = _validate_semantic_supervisor_promoted_values(
                predecessor_values,
                mode="assist",
                invalid_code="semantic_supervisor_assist_environment_invalid",
            )
        _validate_semantic_supervisor_promoted_values(
            target_values,
            mode=target_mode,
            invalid_code=f"semantic_supervisor_{target_mode}_environment_invalid",
            expected_precursor_assist_evidence_sha256=expected_precursor,
        )
        if target_values.get(
            "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_SOURCE_REVISION_SHA256"
        ) != candidate.tree_manifest_sha256:
            raise ReleaseFailure("semantic_supervisor_candidate_source_identity_mismatch")

        evidence_path = Path(
            target_values["FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_EVIDENCE_FILE"]
        )
        budget_path = Path(
            target_values[
                "FRIDAY_SEMANTIC_SUPERVISOR_PROMOTION_LATENCY_BUDGET_FILE"
            ]
        )
        evidence = _read_private_regular_file(
            evidence_path,
            maximum_bytes=_SEMANTIC_SUPERVISOR_MAX_PROMOTION_EVIDENCE_BYTES,
            code="semantic_supervisor_representative_window_issue_invalid",
            allowed_modes=frozenset({0o400, 0o600}),
        )
        budget = _read_private_regular_file(
            budget_path,
            maximum_bytes=_SEMANTIC_SUPERVISOR_MAX_LATENCY_BUDGET_BYTES,
            code="semantic_supervisor_representative_window_issue_invalid",
            allowed_modes=frozenset({0o400, 0o600}),
        )
        bundle = _semantic_supervisor_closed_json(
            evidence,
            invalid_code="semantic_supervisor_representative_window_issue_invalid",
        )
        issue = _semantic_supervisor_exact_object(
            bundle.get("representative_window_issue"),
            _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ISSUE_KEYS,
            invalid_code="semantic_supervisor_representative_window_issue_invalid",
        )
        attestation = _semantic_supervisor_exact_object(
            issue.get("server_attestation"),
            _SEMANTIC_SUPERVISOR_REPRESENTATIVE_WINDOW_ATTESTATION_KEYS,
            invalid_code="semantic_supervisor_representative_window_issue_invalid",
        )
        request_payload = _semantic_supervisor_representative_window_consume_request(
            issue
        )
        api_token = _secondary_rollout_api_token(predecessor)
        primary_ca = _read_private_regular_file(
            self.config.health_ca,
            maximum_bytes=1 << 20,
            code="health_ca_invalid",
        )
        if _sha256_bytes(primary_ca) != self.config.health_ca_sha256:
            raise ReleaseFailure("health_ca_digest_mismatch")

        def recheck_identity() -> None:
            if (
                _read_private_regular_file(
                    self.config.env_file,
                    maximum_bytes=1 << 20,
                    code="semantic_supervisor_predecessor_environment_invalid",
                )
                != predecessor
                or _read_private_regular_file(
                    next_env_file,
                    maximum_bytes=1 << 20,
                    code="semantic_supervisor_next_environment_invalid",
                )
                != target
                or _read_private_regular_file(
                    evidence_path,
                    maximum_bytes=_SEMANTIC_SUPERVISOR_MAX_PROMOTION_EVIDENCE_BYTES,
                    code="semantic_supervisor_representative_window_issue_invalid",
                    allowed_modes=frozenset({0o400, 0o600}),
                )
                != evidence
                or _read_private_regular_file(
                    budget_path,
                    maximum_bytes=_SEMANTIC_SUPERVISOR_MAX_LATENCY_BUDGET_BYTES,
                    code="semantic_supervisor_representative_window_issue_invalid",
                    allowed_modes=frozenset({0o400, 0o600}),
                )
                != budget
                or _read_private_regular_file(
                    self.config.health_ca,
                    maximum_bytes=1 << 20,
                    code="health_ca_invalid",
                )
                != primary_ca
            ):
                raise ReleaseFailure(
                    "semantic_supervisor_representative_window_identity_changed"
                )
            _validate_staged_environment_transition(transition, predecessor, target)
            _validate_semantic_supervisor_promoted_values(
                target_values,
                mode=target_mode,
                invalid_code=f"semantic_supervisor_{target_mode}_environment_invalid",
                expected_precursor_assist_evidence_sha256=expected_precursor,
            )

        recheck_identity()
        self._consume_semantic_supervisor_representative_window_attestation(
            request_payload,
            attestation=attestation,
            api_token=api_token,
            primary_ca=primary_ca,
        )
        recheck_identity()

    def verify_active_anchor(
        self,
        previous: ReleaseIdentity,
        candidate: ReleaseIdentity,
    ) -> None:
        if not self.config.anchor.is_symlink() or self.config.anchor.resolve(
            strict=True
        ) != previous.root.resolve(strict=True):
            raise ReleaseFailure("active_anchor_not_exact_previous")
        self._validate_secondary_rollout_gate(previous, candidate)
        self._validate_semantic_supervisor_representative_window_gate(candidate)

    def stop_bridge(self) -> None:
        self._systemctl("stop", self.config.bridge_unit)

    def stop_backend(self) -> None:
        self._systemctl("stop", self.config.backend_unit)

    def services_inactive(self) -> bool:
        return all(
            self._systemctl("is-active", unit, check=False).stdout.strip() in {b"inactive", b"failed"}
            for unit in (self.config.backend_unit, self.config.bridge_unit)
        )

    def acquire_writer_leases(self) -> None:
        if self._leases:
            return
        from friday.diagnostics.runtime_lease import ProcessLease, process_owns_lease

        leases = [
            ProcessLease(self.config.state_dir / "backend.lock", protocol="friday.backend.v1"),
            ProcessLease(
                self.config.state_dir / "telegram-inbox.sqlite3.lock",
                protocol="friday.telegram-bridge.v1",
            ),
        ]
        try:
            for lease in leases:
                lease.acquire()
                lexical = os.stat(lease.path, follow_symlinks=False)
                if lease.held_file_identity != (
                    int(lexical.st_dev),
                    int(lexical.st_ino),
                ) or not process_owns_lease(lease.path, protocol=lease.protocol):
                    raise ReleaseFailure("writer_lease_identity_changed")
            self._leases = leases
        except BaseException:
            for lease in reversed(leases):
                lease.release()
            raise

    def writer_leases_held(self) -> bool:
        if len(self._leases) != 2:
            return False
        from friday.diagnostics.runtime_lease import process_owns_lease

        for lease in self._leases:
            try:
                lexical = os.stat(lease.path, follow_symlinks=False)
            except OSError:
                return False
            if lease.held_file_identity != (
                int(lexical.st_dev),
                int(lexical.st_ino),
            ) or not process_owns_lease(lease.path, protocol=lease.protocol):
                return False
        return True

    def release_writer_leases(self) -> None:
        for lease in reversed(self._leases):
            lease.release()
        self._leases = []

    def backup_database(self) -> DatabaseBackup:
        if not self.writer_leases_held():
            raise ReleaseFailure("backup_without_writer_leases")
        return _exact_sqlite_backup(self.config)

    def offline_migrate(self, release: ReleaseIdentity, backup: DatabaseBackup) -> None:
        del backup
        if not self.writer_leases_held():
            raise ReleaseFailure("migration_without_writer_leases")
        script = """
import json, logging, os, pathlib, sys
logging.disable(logging.CRITICAL)
os.environ['FRIDAY_ENV_FILE']=sys.argv[1]
from friday.config import load_local_env_file, load_settings
from friday.storage import FridayStorage, SCHEMA_VERSION
load_local_env_file(); settings=load_settings()
assert settings.database_path.resolve(strict=True)==pathlib.Path(sys.argv[2]).resolve(strict=True)
assert settings.database_must_exist is True
assert settings.home.resolve(strict=True)==pathlib.Path(sys.argv[4]).resolve(strict=True)
assert SCHEMA_VERSION==int(sys.argv[3])
store=FridayStorage(settings)
try:
 row=store.conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
 assert row is not None and int(row[0])==SCHEMA_VERSION
 assert store.conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
 assert not store.conn.execute('PRAGMA foreign_key_check').fetchall()
finally: store.close(final=True)
print(json.dumps({'schema':SCHEMA_VERSION,'status':'clear'},sort_keys=True,separators=(',',':')))
"""
        child_environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"LANG", "LC_ALL", "SSL_CERT_DIR", "SSL_CERT_FILE", "XDG_RUNTIME_DIR"}
        }
        child_environment.update(
            {
                "FRIDAY_ENV_FILE": str(self.config.env_file),
                "FRIDAY_HOME": str(self.config.friday_home),
                "FRIDAY_DATABASE_PATH": str(self.config.database),
                "FRIDAY_DATABASE_MUST_EXIST": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        result = subprocess.run(  # noqa: S603
            [
                str(release.root / "venv/bin/python"),
                "-I",
                "-B",
                "-c",
                script,
                str(self.config.env_file),
                str(self.config.database),
                str(release.max_schema),
                str(self.config.friday_home),
            ],
            check=False,
            capture_output=True,
            env=child_environment,
            timeout=600,
        )
        if result.returncode != 0 or len(result.stdout) > 4096:
            raise ReleaseFailure("offline_schema_migration_failed")
        try:
            parsed = json.loads(result.stdout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseFailure("offline_schema_migration_receipt_invalid") from exc
        if parsed != {"schema": release.max_schema, "status": "clear"}:
            raise ReleaseFailure("offline_schema_migration_receipt_invalid")

    def repair_file_aliases(
        self,
        release: ReleaseIdentity,
        backup: DatabaseBackup,
    ) -> Mapping[str, Any]:
        if not self.config.alias_claim_manifests:
            core = {
                "schema": ALIAS_REPAIR_RECEIPT_SCHEMA,
                "status": "not_requested",
                "applied_count": 0,
                "plan_sha256": "0" * 64,
                "backup_manifest_sha256": "0" * 64,
                "backup_database_sha256": "0" * 64,
                "backup_inbox_sha256": "0" * 64,
                "pre_apply_database_sha256": "0" * 64,
                "writer_quiescence_sha256": "0" * 64,
            }
            return {**core, "receipt_sha256": _sha256_bytes(_canonical_json(core))}
        if not self.writer_leases_held() or len(self._leases) != 2:
            raise ReleaseFailure("alias_repair_without_writer_leases")
        payload = backup.opaque
        if not isinstance(payload, _ExactBackupPayload):
            raise ReleaseFailure("alias_repair_backup_identity_invalid")
        manifest_path = payload.directory / "manifest.json"
        module = _load_candidate_alias_tool(release)
        expected_evidence = {
            "applied_count",
            "applied_plan_sha256",
            "backup_database_sha256",
            "backup_inbox_sha256",
            "backup_manifest_sha256",
            "pre_apply_database_sha256",
            "writer_quiescence_sha256",
        }
        evidence_rows: list[Mapping[str, Any]] = []
        plans: list[Any] = []
        seen_uploaders: set[str] = set()
        tenant_owner: tuple[str, str] | None = None
        environment = {
            "FRIDAY_DATABASE_MUST_EXIST": "1",
            "FRIDAY_DATABASE_PATH": str(self.config.database),
            "FRIDAY_ENV_FILE": str(self.config.env_file),
            "FRIDAY_HOME": str(self.config.friday_home),
            "FRIDAY_STATE_DIR": str(self.config.state_dir),
        }
        try:
            with _exact_environment(environment):
                for claim_manifest, expected_count, expected_plan_sha256 in zip(
                    self.config.alias_claim_manifests,
                    self.config.alias_expected_counts,
                    self.config.alias_expected_plan_sha256s,
                    strict=True,
                ):
                    live_identity, live_sha256 = _private_file_attestation(self.config.database)
                    external_receipt = module.ExternalBackupReceipt(
                        schema=module.EXTERNAL_BACKUP_SCHEMA,
                        manifest_path=manifest_path,
                        manifest_sha256=_sha256_file(manifest_path),
                        database_files_sha256=backup.receipt_sha256,
                        inbox_files_sha256=backup.inbox_receipt_sha256,
                        live_database_identity=live_identity,
                        live_database_sha256=live_sha256,
                    )
                    plan, evidence = module.apply_plan_under_held_leases(
                        self.config.database,
                        claim_manifest=claim_manifest,
                        expected_count=expected_count,
                        expected_plan_sha256=expected_plan_sha256,
                        backend_lease=self._leases[0],
                        bridge_lease=self._leases[1],
                        verified_backup_receipt=external_receipt,
                    )
                    uploader = str(getattr(plan, "uploader_id", "") or "")
                    scope = (
                        str(getattr(plan, "tenant_id", "") or ""),
                        str(getattr(plan, "owner_id", "") or ""),
                    )
                    if (
                        not isinstance(evidence, dict)
                        or set(evidence) != expected_evidence
                        or type(evidence.get("applied_count")) is not int
                        or evidence["applied_count"] != expected_count
                        or getattr(plan, "candidate_count", None) != expected_count
                        or getattr(plan, "plan_sha256", None) != expected_plan_sha256
                        or evidence.get("applied_plan_sha256") != expected_plan_sha256
                        or not uploader
                        or uploader in seen_uploaders
                        or not all(scope)
                        or (tenant_owner is not None and scope != tenant_owner)
                    ):
                        raise ReleaseFailure("alias_repair_evidence_invalid")
                    tenant_owner = scope
                    seen_uploaders.add(uploader)
                    plans.append(plan)
                    evidence_rows.append(evidence)
        except ReleaseFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - private details never cross the receipt boundary
            raise ReleaseFailure("alias_repair_failed") from exc
        if len(plans) != len(self.config.alias_claim_manifests) or not evidence_rows:
            raise ReleaseFailure("alias_repair_evidence_invalid")
        manifest_hashes = {str(row["backup_manifest_sha256"]) for row in evidence_rows}
        database_hashes = {str(row["backup_database_sha256"]) for row in evidence_rows}
        inbox_hashes = {str(row["backup_inbox_sha256"]) for row in evidence_rows}
        quiescence_hashes = {str(row["writer_quiescence_sha256"]) for row in evidence_rows}
        if any(
            len(values) != 1 for values in (manifest_hashes, database_hashes, inbox_hashes, quiescence_hashes)
        ):
            raise ReleaseFailure("alias_repair_evidence_invalid")
        plan_hashes = [str(row["applied_plan_sha256"]) for row in evidence_rows]
        pre_apply_hashes = [str(row["pre_apply_database_sha256"]) for row in evidence_rows]
        core = {
            "schema": ALIAS_REPAIR_RECEIPT_SCHEMA,
            "status": "clear",
            "applied_count": sum(int(row["applied_count"]) for row in evidence_rows),
            "plan_sha256": _sha256_bytes(_canonical_json(plan_hashes)),
            "backup_manifest_sha256": _closed_hash(
                manifest_hashes.pop(),
                "alias_repair_evidence_invalid",
            ),
            "backup_database_sha256": _closed_hash(
                database_hashes.pop(),
                "alias_repair_evidence_invalid",
            ),
            "backup_inbox_sha256": _closed_hash(
                inbox_hashes.pop(),
                "alias_repair_evidence_invalid",
            ),
            "pre_apply_database_sha256": _sha256_bytes(_canonical_json(pre_apply_hashes)),
            "writer_quiescence_sha256": _closed_hash(
                quiescence_hashes.pop(),
                "alias_repair_evidence_invalid",
            ),
        }
        return {**core, "receipt_sha256": _sha256_bytes(_canonical_json(core))}

    def switch_anchor(self, release: ReleaseIdentity) -> None:
        verify_release_tree(release)
        _atomic_anchor(self.config.anchor, release)

    def _wait_process(self, unit: str, release: ReleaseIdentity, role: str) -> int:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            result = self._systemctl("show", unit, "--property=MainPID", "--value", check=False)
            try:
                pid = int(result.stdout.strip() or b"0")
            except ValueError:
                pid = 0
            if pid > 0 and self._process_matches(pid, release, role):
                return pid
            time.sleep(0.2)
        raise ReleaseFailure(f"{role}_process_identity_timeout")

    def _start(self, unit: str, release: ReleaseIdentity, role: str) -> None:
        self._systemctl("reset-failed", unit, check=False)
        self._systemctl("start", unit)
        self._wait_process(unit, release, role)

    def _process_matches(
        self,
        pid: int,
        release: ReleaseIdentity,
        role: str,
        *,
        proc_root: Path = Path("/proc"),
    ) -> bool:
        try:
            process_root = proc_root / str(pid)
            command = [item for item in (process_root / "cmdline").read_bytes().split(b"\0") if item]
            expected_python = str(self.config.anchor / "venv/bin/python").encode()
            expected_tail = b"server" if role == "backend" else b"telegram-bridge"
            expected = [
                expected_python,
                b"-I",
                b"-B",
                b"-m",
                b"friday.cli",
                b"--env-file",
                str(self.config.env_file).encode(),
                expected_tail,
            ]
            executable = (process_root / "exe").resolve(strict=True)
            release_python = (release.root / "venv/bin/python").resolve(strict=True)
            return bool(
                command == expected
                and executable == release_python
                and self.config.anchor.resolve(strict=True) == release.root.resolve(strict=True)
            )
        except OSError:
            return False

    def start_backend(self, release: ReleaseIdentity) -> None:
        self._verify_environment_file()
        self._start(self.config.backend_unit, release, "backend")

    def _expected_semantic_health_mode(self) -> str:
        """Bind every staged-transition writer to the canonical ENV generation."""

        if not self._semantic_health_transition:
            return ""
        descriptor = self._staged_descriptor
        if descriptor is None:  # pragma: no cover - transition proves it
            raise ReleaseFailure("staged_environment_identity_changed")
        _transition, predecessor_digest, _staged_path, target_digest = descriptor
        current_digest = self._canonical_environment_digest()
        try:
            predecessor_mode, target_mode = _SEMANTIC_SUPERVISOR_TRANSITION_MODES[
                self._semantic_health_transition
            ]
        except KeyError as exc:  # pragma: no cover - constructor gates the vocabulary
            raise ReleaseFailure("staged_environment_identity_changed") from exc
        if current_digest == target_digest:
            return target_mode
        if current_digest == predecessor_digest:
            return predecessor_mode
        raise ReleaseFailure("staged_canonical_environment_changed")

    def accept_backend(self, release: ReleaseIdentity) -> None:
        ca = _private_regular_file(
            self.config.health_ca,
            maximum_bytes=1 << 20,
            code="health_ca_invalid",
        )
        if _sha256_file(ca) != self.config.health_ca_sha256:
            raise ReleaseFailure("health_ca_changed")
        context = ssl.create_default_context(cafile=str(ca))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            _NoRedirect(),
        )
        expected_semantic_mode = self._expected_semantic_health_mode()
        deadline = time.monotonic() + 420.0
        while time.monotonic() < deadline:
            try:
                request = urllib.request.Request(self.config.health_url, method="GET")
                with opener.open(  # noqa: S310 - exact pinned loopback TLS endpoint
                    request,
                    timeout=5.0,
                ) as response:
                    body = response.read(65_537)
                payload = _unique_json(body.decode("utf-8"))
                if (
                    response.status == 200
                    and response.geturl() == self.config.health_url
                    and len(body) <= 65_536
                    and isinstance(payload, dict)
                    and payload.get("status") == "ok"
                    and payload.get("version") == release.version
                    and _memory_vault_health_identity_matches(
                        payload,
                        release,
                        self.config.memory_vault_mode,
                    )
                    and _obsidian_health_identity_matches(
                        payload,
                        release,
                        self.config.obsidian_mode,
                        _obsidian_root_sha256(self.config),
                    )
                    and (
                        not expected_semantic_mode
                        or _semantic_supervisor_health_identity_matches(
                            payload,
                            expected_mode=expected_semantic_mode,
                        )
                    )
                ):
                    self._wait_process(self.config.backend_unit, release, "backend")
                    return
            except (OSError, urllib.error.URLError, UnicodeError, ValueError):
                pass
            time.sleep(0.2)
        raise ReleaseFailure("backend_health_identity_timeout")

    def start_bridge(self, release: ReleaseIdentity) -> None:
        self._verify_environment_file()
        self._start(self.config.bridge_unit, release, "bridge")

    def accept_bridge(self, release: ReleaseIdentity) -> None:
        from friday.diagnostics.runtime_lease import inspect_process_lease

        stable_pid = 0
        stable_samples = 0
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and stable_samples < 3:
            pid = self._wait_process(self.config.bridge_unit, release, "bridge")
            lease = inspect_process_lease(
                self.config.state_dir / "telegram-inbox.sqlite3.lock",
                protocol="friday.telegram-bridge.v1",
            )
            if (
                pid > 0
                and pid == stable_pid
                and lease.get("active") is True
                and lease.get("pid") == pid
                and lease.get("protocol_matches") is True
                and lease.get("state") == "active"
            ):
                stable_samples += 1
            elif (
                lease.get("active") is True
                and lease.get("pid") == pid
                and lease.get("protocol_matches") is True
                and lease.get("state") == "active"
            ):
                stable_pid = pid
                stable_samples = 1
            else:
                stable_pid = 0
                stable_samples = 0
            time.sleep(0.25)
        if stable_samples < 3:
            raise ReleaseFailure("bridge_lease_identity_not_stable")

    def restore_database(self, backup: DatabaseBackup) -> None:
        if not self.writer_leases_held():
            raise ReleaseFailure("restore_without_writer_leases")
        _restore_exact_sqlite_backup(self.config, backup)

    def _historical_album_completion_snapshot(self) -> bool:
        """Return true only when the exact reset set durably left the queue."""

        database = _private_regular_file(
            self.config.inbox_database,
            maximum_bytes=1 << 40,
            code="inbox_database_file_invalid",
        )
        placeholders = ",".join("?" for _ in HISTORICAL_ALBUM_UPDATE_IDS)
        try:
            connection = sqlite3.connect(
                f"{database.as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA query_only=ON")
                rows = connection.execute(
                    f"""SELECT update_id,status,attempts FROM updates
                           WHERE update_id IN ({placeholders}) ORDER BY update_id""",  # nosec B608
                    HISTORICAL_ALBUM_UPDATE_IDS,
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            raise ReleaseFailure("historical_album_completion_observation_failed") from exc
        if not rows:
            return True
        if len(rows) != len(HISTORICAL_ALBUM_UPDATE_IDS):
            raise ReleaseFailure("historical_album_completion_partial")
        if tuple(int(row["update_id"]) for row in rows) != HISTORICAL_ALBUM_UPDATE_IDS:
            raise ReleaseFailure("historical_album_completion_identity_changed")
        statuses = {str(row["status"]) for row in rows}
        attempts = [row["attempts"] for row in rows]
        if "dead_letter" in statuses:
            raise ReleaseFailure("historical_album_completion_dead_lettered")
        if statuses != {"pending"} or any(type(value) is not int or int(value) < 0 for value in attempts):
            raise ReleaseFailure("historical_album_completion_state_invalid")
        return False

    def _wait_for_historical_album_completion(self, release: ReleaseIdentity) -> None:
        """Boundedly prove bridge completion; CAS and process health are insufficient."""

        deadline = time.monotonic() + HISTORICAL_ALBUM_COMPLETION_TIMEOUT_SEC
        absent_observations = 0
        self.accept_bridge(release)
        while True:
            if self._historical_album_completion_snapshot():
                absent_observations += 1
                if absent_observations == 2:
                    return
                # Bind the two durable absence observations to an accepted
                # bridge process from the exact final release.
                self.accept_bridge(release)
            else:
                absent_observations = 0
            if time.monotonic() >= deadline:
                self.accept_bridge(release)
                raise ReleaseFailure("historical_album_completion_pending")
            time.sleep(HISTORICAL_ALBUM_COMPLETION_POLL_SEC)

    def _require_historical_album_completion_still_durable(
        self,
        release: ReleaseIdentity,
    ) -> None:
        """Revalidate a terminal journal without converting it into blind trust."""

        self.accept_bridge(release)
        if not self._historical_album_completion_snapshot():
            raise ReleaseFailure("historical_album_completion_no_longer_durable")
        self.accept_bridge(release)
        if not self._historical_album_completion_snapshot():
            raise ReleaseFailure("historical_album_completion_no_longer_durable")

    def recover_historical_album_live(self, release: ReleaseIdentity) -> dict[str, Any]:
        """Crash-resume the exact one-time reset after the v2 bridge was live."""

        if self.config.memory_vault_mode != "disabled":
            raise ReleaseFailure("album_recovery_requires_body_free_phase")
        activation_path = self.config.state_dir / "immutable-release-activation.v1.json"
        if not activation_path.exists() or activation_path.is_symlink():
            raise ReleaseFailure("album_recovery_requires_accepted_phase_b")
        accepted_activation = DurableActivationJournal(
            activation_path,
            backup_root=self.config.backup_dir,
            config_identity_sha256=_systemd_config_identity(self.config),
            config_scope_sha256=_systemd_config_scope_identity(self.config),
            config_retry_scope_sha256=_systemd_config_retry_scope_identity(self.config),
            alias_claim_count=len(self.config.alias_claim_manifests),
            memory_vault_mode=self.config.memory_vault_mode,
            obsidian_mode=self.config.obsidian_mode,
            obsidian_root_sha256=_obsidian_root_sha256(self.config),
        ).load()
        if accepted_activation.get("phase") != "clear":
            raise ReleaseFailure("album_recovery_requires_clear_phase_b")
        if accepted_activation.get("candidate") != _journal_release(release):
            raise ReleaseFailure("album_recovery_phase_b_candidate_mismatch")
        self.verify_release(release)
        if not self.config.anchor.is_symlink() or self.config.anchor.resolve(
            strict=True
        ) != release.root.resolve(strict=True):
            raise ReleaseFailure("album_v2_anchor_not_live")
        self.accept_backend(release)
        journal = DurableAlbumRecoveryJournal(
            self.config.state_dir / "historical-album-recovery.v1.json",
            backup_root=self.config.backup_dir,
            config_identity_sha256=_systemd_config_identity(self.config),
        )
        if not journal.path.exists() and not journal.path.is_symlink():
            self.accept_bridge(release)
        state = dict(journal.begin_or_resume(release))
        if state["phase"] == "complete":
            backup = journal.backup(self.config)
            if backup is None:
                raise ReleaseFailure("album_recovery_completed_without_backup")
            terminal_cas_receipt = _historical_album_pending_receipt(backup.receipt_sha256)
            terminal_receipt = _historical_album_receipt(backup.receipt_sha256, release)
            if terminal_cas_receipt["receipt_sha256"] != state["cas_receipt_sha256"]:
                raise ReleaseFailure("album_recovery_cas_receipt_changed")
            if terminal_receipt["receipt_sha256"] != state["completion_receipt_sha256"]:
                raise ReleaseFailure("album_recovery_terminal_receipt_changed")
            self._require_historical_album_completion_still_durable(release)
            return terminal_receipt

        from friday.diagnostics.runtime_lease import ProcessLease, process_owns_lease

        cas_receipt: dict[str, Any] | None = None
        failure: BaseException | None = None
        phase = str(journal.load()["phase"])
        if phase not in {"bridge_start_attempted", "bridge_accepted"}:
            lease = ProcessLease(
                self.config.state_dir / "telegram-inbox.sqlite3.lock",
                protocol="friday.telegram-bridge.v1",
            )
            bridge_quiesced = False
            try:
                phase = str(journal.load()["phase"])
                if phase == "prepared":
                    journal.record("bridge_stop_attempted")
                self.stop_bridge()
                deadline = time.monotonic() + 45.0
                while time.monotonic() < deadline:
                    unit_state = self._systemctl(
                        "is-active",
                        self.config.bridge_unit,
                        check=False,
                    ).stdout.strip()
                    if unit_state in {b"inactive", b"failed"}:
                        bridge_quiesced = True
                        break
                    time.sleep(0.1)
                if not bridge_quiesced:
                    raise ReleaseFailure("bridge_did_not_quiesce_for_album_recovery")
                phase = str(journal.load()["phase"])
                if phase == "bridge_stop_attempted":
                    journal.record("bridge_quiesced")
                lease.acquire()

                def lease_held() -> bool:
                    try:
                        lexical = os.stat(lease.path, follow_symlinks=False)
                    except OSError:
                        return False
                    return bool(
                        lease.held_file_identity == (int(lexical.st_dev), int(lexical.st_ino))
                        and process_owns_lease(lease.path, protocol=lease.protocol)
                    )

                phase = str(journal.load()["phase"])
                if phase == "bridge_quiesced":
                    backup = _exact_inbox_backup(self.config)
                    journal.record("backup_complete", backup=backup)
                else:
                    backup = journal.backup(self.config)
                    if backup is None:
                        raise ReleaseFailure("album_recovery_backup_missing")
                assert backup is not None
                backup_receipt_sha256 = backup.receipt_sha256
                phase = str(journal.load()["phase"])
                if phase == "backup_complete":
                    journal.record("cas_attempted")
                connection = sqlite3.connect(
                    str(self.config.inbox_database),
                    timeout=30.0,
                    isolation_level=None,
                )
                connection.row_factory = sqlite3.Row
                try:
                    phase = str(journal.load()["phase"])
                    if phase == "cas_attempted":
                        recovery_state = _historical_album_recovery_state(connection)
                        if recovery_state == "dead_letter":
                            cas_receipt = recover_historical_album(
                                connection,
                                v2_binary_live=lambda: True,
                                verified_backup=lambda: backup_receipt_sha256,
                                bridge_lease_held=lease_held,
                            )
                        else:
                            if not lease_held():
                                raise ReleaseFailure("bridge_writer_not_quiesced")
                            cas_receipt = _historical_album_pending_receipt(backup_receipt_sha256)
                        journal.record(
                            "cas_complete",
                            cas_receipt_sha256=str(cas_receipt["receipt_sha256"]),
                        )
                    else:
                        cas_receipt = _historical_album_pending_receipt(backup_receipt_sha256)
                        if cas_receipt["receipt_sha256"] != journal.load()["cas_receipt_sha256"]:
                            raise ReleaseFailure("album_recovery_cas_receipt_changed")
                finally:
                    connection.close()
            except BaseException as exc:
                failure = exc
            finally:
                lease.release()
            if failure is None:
                phase = str(journal.load()["phase"])
                if phase == "cas_complete":
                    journal.record("bridge_start_attempted")
            elif str(journal.load()["phase"]) == "cas_attempted":
                # The SQLite commit may have won while its journal write failed.
                # Keep the writer stopped until a rerun reconciles exact pending
                # rows; otherwise it could consume them before durable recovery.
                raise ReleaseFailure("historical_album_recovery_failed") from failure
            if failure is not None:
                try:
                    self.start_bridge(release)
                    self.accept_bridge(release)
                except BaseException as restart_error:
                    raise ReleaseFailure("album_recovery_bridge_restart_failed") from restart_error
                raise ReleaseFailure("historical_album_recovery_failed") from failure

        backup = journal.backup(self.config)
        if backup is None:
            raise ReleaseFailure("album_recovery_backup_missing")
        cas_receipt = cas_receipt or _historical_album_pending_receipt(backup.receipt_sha256)
        if cas_receipt["receipt_sha256"] != journal.load()["cas_receipt_sha256"]:
            raise ReleaseFailure("album_recovery_cas_receipt_changed")
        phase = str(journal.load()["phase"])
        if phase not in {"bridge_start_attempted", "bridge_accepted"}:
            raise ReleaseFailure("album_recovery_journal_phase_invalid")
        try:
            if phase == "bridge_start_attempted":
                self.start_bridge(release)
                self.accept_bridge(release)
                journal.record("bridge_accepted")
            else:
                self.accept_bridge(release)
        except BaseException as restart_error:
            raise ReleaseFailure("album_recovery_bridge_restart_failed") from restart_error
        self._wait_for_historical_album_completion(release)
        terminal_receipt = _historical_album_receipt(backup.receipt_sha256, release)
        try:
            journal.record(
                "complete",
                completion_receipt_sha256=str(terminal_receipt["receipt_sha256"]),
            )
        except BaseException as exc:
            raise ReleaseFailure("album_recovery_completion_not_durable") from exc
        return terminal_receipt


def load_release_identity(root: Path, *, expected_tree_sha256: str) -> ReleaseIdentity:
    resolved = Path(os.path.abspath(root)).resolve(strict=True)
    metadata_path = _regular_file(
        resolved / "artifacts/immutable-release.json",
        maximum_bytes=1 << 20,
        code="release_metadata_invalid",
    )
    try:
        metadata = _unique_json(metadata_path.read_text(encoding="ascii"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReleaseFailure("release_metadata_invalid") from exc
    if isinstance(metadata, dict) and metadata.get("schema") == "friday.immutable-release.v1":
        raise ReleaseFailure("legacy_release_requires_fresh_wheel_only_sibling")
    expected_metadata_keys = {
        "alias_dependency_sha256",
        "alias_tool_sha256",
        "base_python_sha256",
        "bootstrap_pins",
        "bootstrap_wheel_sha256",
        "commit",
        "max_schema",
        "operator_sha256",
        "runtime_lock_sha256",
        "runtime_pin_count",
        "schema",
        "version",
        "wheel_sha256",
        "wheelhouse_manifest_sha256",
    }
    metadata_keys = set(metadata) if isinstance(metadata, dict) else set()
    optional_capability_keys = {
        "memory_vault_mode_contract",
        "obsidian_cutover_contract",
        "secondary_product_runner_sha256",
        "venv_relocation_contract",
    }
    memory_vault_mode_contract = (
        str(metadata.get("memory_vault_mode_contract") or "") if isinstance(metadata, dict) else ""
    )
    venv_relocation_contract = (
        str(metadata.get("venv_relocation_contract") or "") if isinstance(metadata, dict) else ""
    )
    obsidian_cutover_contract = (
        str(metadata.get("obsidian_cutover_contract") or "") if isinstance(metadata, dict) else ""
    )
    secondary_product_runner_sha256 = (
        str(metadata.get("secondary_product_runner_sha256") or "") if isinstance(metadata, dict) else ""
    )
    if (
        not isinstance(metadata, dict)
        or not expected_metadata_keys.issubset(metadata_keys)
        or not metadata_keys.issubset(expected_metadata_keys | optional_capability_keys)
        or (
            ("memory_vault_mode_contract" in metadata_keys)
            != (memory_vault_mode_contract == MEMORY_VAULT_MODE_CONTRACT)
        )
        or (
            ("venv_relocation_contract" in metadata_keys)
            != (venv_relocation_contract == VENV_RELOCATION_CONTRACT)
        )
        or (
            ("obsidian_cutover_contract" in metadata_keys)
            != (obsidian_cutover_contract == OBSIDIAN_CUTOVER_CONTRACT)
        )
        or metadata.get("schema") != BUILD_RECEIPT_SCHEMA
        or _VERSION.fullmatch(str(metadata.get("version") or "")) is None
        or type(metadata.get("max_schema")) is not int
        or int(metadata["max_schema"]) <= 0
        or type(metadata.get("runtime_pin_count")) is not int
        or int(metadata["runtime_pin_count"]) <= 0
        or metadata.get("bootstrap_pins") != {name: version for name, version, _filename in BOOTSTRAP_WHEELS}
        or not isinstance(metadata.get("bootstrap_wheel_sha256"), dict)
        or set(metadata["bootstrap_wheel_sha256"]) != {name for name, _version, _filename in BOOTSTRAP_WHEELS}
    ):
        raise ReleaseFailure("release_metadata_invalid")
    for key in (
        "alias_dependency_sha256",
        "alias_tool_sha256",
        "base_python_sha256",
        "operator_sha256",
        "runtime_lock_sha256",
        "wheel_sha256",
        "wheelhouse_manifest_sha256",
    ):
        _closed_hash(str(metadata.get(key) or ""), "release_metadata_digest_invalid")
    if "secondary_product_runner_sha256" in metadata_keys:
        _closed_hash(
            secondary_product_runner_sha256,
            "release_secondary_product_runner_digest_invalid",
        )
    for digest in metadata["bootstrap_wheel_sha256"].values():
        _closed_hash(str(digest), "release_metadata_digest_invalid")
    operator_path = _regular_file(
        resolved / "artifacts/immutable_release_operator.py",
        maximum_bytes=4 << 20,
        code="release_operator_invalid",
    )
    if _sha256_file(operator_path) != _closed_hash(
        str(metadata.get("operator_sha256") or ""), "release_operator_digest_invalid"
    ):
        raise ReleaseFailure("release_operator_digest_mismatch")
    if secondary_product_runner_sha256:
        product_runner = _regular_file(
            resolved / _SECONDARY_PRODUCT_RUNNER_ARTIFACT,
            maximum_bytes=4 << 20,
            code="release_secondary_product_runner_invalid",
        )
        product_runner_status = os.stat(product_runner, follow_symlinks=False)
        if (
            product_runner_status.st_uid != os.geteuid()
            or stat.S_IMODE(product_runner_status.st_mode) != 0o400
            or _sha256_file(product_runner) != secondary_product_runner_sha256
        ):
            raise ReleaseFailure("release_secondary_product_runner_digest_mismatch")
    for relative, metadata_key, code in (
        (
            Path("tools/backfill_file_alias_filenames.py"),
            "alias_tool_sha256",
            "release_alias_tool",
        ),
        (
            Path("tools/backfill_telegram_file_aliases.py"),
            "alias_dependency_sha256",
            "release_alias_dependency",
        ),
    ):
        source = _regular_file(resolved / relative, maximum_bytes=4 << 20, code=f"{code}_invalid")
        if _sha256_file(source) != _closed_hash(
            str(metadata.get(metadata_key) or ""),
            f"{code}_digest_invalid",
        ):
            raise ReleaseFailure(f"{code}_digest_mismatch")
    actual_tree_sha256 = _sha256_file(resolved / "artifacts/release-tree.sha256")
    if actual_tree_sha256 != _closed_hash(expected_tree_sha256, "expected_release_tree_digest_invalid"):
        raise ReleaseFailure("expected_release_tree_digest_mismatch")
    release = ReleaseIdentity(
        root=resolved,
        commit=_closed_commit(str(metadata.get("commit") or "")),
        version=str(metadata.get("version") or ""),
        tree_manifest_sha256=actual_tree_sha256,
        max_schema=int(metadata.get("max_schema") or 0),
        memory_vault_mode_contract=memory_vault_mode_contract,
        venv_relocation_contract=venv_relocation_contract,
        obsidian_cutover_contract=obsidian_cutover_contract,
        secondary_product_runner_sha256=secondary_product_runner_sha256,
    )
    verify_release_tree(release)
    return release


def _load_candidate_alias_tool(release: ReleaseIdentity) -> Any:
    module_name = "tools.backfill_file_alias_filenames"
    dependency_name = "tools.backfill_telegram_file_aliases"
    expected_module = release.root / "tools/backfill_file_alias_filenames.py"
    expected_dependency = release.root / "tools/backfill_telegram_file_aliases.py"
    prior = {name: sys.modules.get(name) for name in (module_name, dependency_name)}
    for name in prior:
        sys.modules.pop(name, None)
    release_root = str(release.root)
    sys.path.insert(0, release_root)
    importlib.invalidate_caches()
    try:
        module = importlib.import_module(module_name)
        dependency = sys.modules.get(dependency_name)
        if (
            Path(str(module.__file__)).resolve(strict=True) != expected_module.resolve(strict=True)
            or dependency is None
            or Path(str(dependency.__file__)).resolve(strict=True) != expected_dependency.resolve(strict=True)
        ):
            raise ReleaseFailure("candidate_alias_tool_import_identity_invalid")
        return module
    except (ImportError, AttributeError, OSError) as exc:
        raise ReleaseFailure("candidate_alias_tool_import_failed") from exc
    finally:
        sys.path.remove(release_root)
        for name, previous in prior.items():
            sys.modules.pop(name, None)
            if previous is not None:
                sys.modules[name] = previous
        importlib.invalidate_caches()


def _require_candidate_bound_operator(release: ReleaseIdentity) -> None:
    _require_venv_relocation_contract(
        release,
        code="operator_release_venv_relocation_contract_missing",
    )
    _require_obsidian_cutover_contract(
        release,
        code="operator_release_obsidian_cutover_contract_missing",
    )
    expected_script = release.root / "artifacts/immutable_release_operator.py"
    try:
        script_bound = Path(__file__).resolve(strict=True).samefile(expected_script)
        interpreter_bound = Path(sys.executable).resolve(strict=True) == (
            release.root / "venv/bin/python"
        ).resolve(strict=True)
    except OSError as exc:
        raise ReleaseFailure("operator_execution_identity_invalid") from exc
    if not script_bound or not interpreter_bound:
        raise ReleaseFailure("operator_not_executed_by_candidate")


def _unique_json(text: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    parsed = json.loads(text, object_pairs_hook=pairs)
    if not isinstance(parsed, dict):
        raise ValueError("not object")
    return parsed


def _historical_album_manifest(
    conn: sqlite3.Connection,
    *,
    expected_state: str = "dead_letter",
) -> dict[str, Any]:
    if expected_state not in {"dead_letter", "pending"}:
        raise ReleaseFailure("historical_album_state_invalid")
    placeholders = ",".join("?" for _ in HISTORICAL_ALBUM_UPDATE_IDS)
    rows = conn.execute(
        f"""SELECT update_id,payload_json,attempts,last_error,backend_response_json,status,ordering_key,
                  next_attempt_at,failed_at
               FROM updates WHERE update_id IN ({placeholders}) ORDER BY update_id""",  # nosec B608
        HISTORICAL_ALBUM_UPDATE_IDS,
    ).fetchall()
    if len(rows) != len(HISTORICAL_ALBUM_UPDATE_IDS):
        raise ReleaseFailure("historical_album_rows_missing")
    items: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload_text = str(row["payload_json"])
            payload = _unique_json(payload_text)
            message = payload["message"]
            if not isinstance(message, dict):
                raise ValueError("message")
            chat = message["chat"]
            sender = message["from"]
            if not isinstance(chat, dict) or not isinstance(sender, dict):
                raise ValueError("identity")
            item = {
                "attempts": row["attempts"],
                "backend_response_absent": row["backend_response_json"] is None,
                "chat_id": chat["id"],
                "last_error": row["last_error"],
                "media_group_id": message["media_group_id"],
                "message_id": message["message_id"],
                "ordering_key": row["ordering_key"],
                "payload_sha256": _sha256_bytes(payload_text.encode("utf-8")),
                "sender_id": sender["id"],
                "status": row["status"],
                "update_id": row["update_id"],
            }
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ReleaseFailure("historical_album_payload_invalid") from exc
        items.append(item)
        if (
            row["attempts"] != 0
            or row["backend_response_json"] is not None
            or (
                expected_state == "dead_letter"
                and (row["status"] != "dead_letter" or row["last_error"] != _HISTORICAL_ALBUM_DEAD_ERROR)
            )
            or (
                expected_state == "pending"
                and (
                    row["status"] != "pending"
                    or row["last_error"] != ""
                    or row["next_attempt_at"] != 0
                    or row["failed_at"] is not None
                )
            )
        ):
            raise ReleaseFailure("historical_album_state_changed")
    normalized_items = [dict(item) for item in items]
    if expected_state == "pending":
        for item in normalized_items:
            item["status"] = "dead_letter"
            item["last_error"] = _HISTORICAL_ALBUM_DEAD_ERROR
    manifest = {"schema": ALBUM_RECOVERY_SCHEMA, "items": normalized_items}
    if tuple(item["update_id"] for item in items) != HISTORICAL_ALBUM_UPDATE_IDS:
        raise ReleaseFailure("historical_album_update_identity_changed")
    if tuple(item["message_id"] for item in items) != _ALBUM_MESSAGE_IDS:
        raise ReleaseFailure("historical_album_message_identity_changed")
    if _sha256_bytes(_canonical_json(manifest)) != HISTORICAL_ALBUM_PLAN_SHA256:
        raise ReleaseFailure("historical_album_plan_changed")
    return manifest


def _historical_album_recovery_state(conn: sqlite3.Connection) -> str:
    try:
        _historical_album_manifest(conn, expected_state="dead_letter")
    except ReleaseFailure as dead_letter_error:
        try:
            _historical_album_manifest(conn, expected_state="pending")
        except ReleaseFailure as pending_error:
            raise ReleaseFailure("historical_album_recovery_state_invalid") from pending_error
        if str(dead_letter_error) not in {
            "historical_album_state_changed",
            "historical_album_plan_changed",
        }:
            raise dead_letter_error
        return "pending"
    return "dead_letter"


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _historical_album_pending_receipt(backup_sha256: str) -> dict[str, Any]:
    receipt = {
        "schema": ALBUM_RECOVERY_PENDING_RECEIPT_SCHEMA,
        "status": "pending",
        "reset_count": len(HISTORICAL_ALBUM_UPDATE_IDS),
        "plan_sha256": HISTORICAL_ALBUM_PLAN_SHA256,
        "backup_sha256": _closed_hash(backup_sha256, "inbox_backup_invalid"),
    }
    return {**receipt, "receipt_sha256": _sha256_bytes(_canonical_json(receipt))}


def _historical_album_completion_evidence(release: ReleaseIdentity) -> dict[str, Any]:
    evidence = {
        "schema": ALBUM_RECOVERY_COMPLETION_EVIDENCE_SCHEMA,
        "observation": "exact_update_rows_absent_after_accepted_bridge",
        "observation_count": 2,
        "completed_update_count": len(HISTORICAL_ALBUM_UPDATE_IDS),
        "plan_sha256": HISTORICAL_ALBUM_PLAN_SHA256,
        "release_tree_sha256": release.tree_manifest_sha256,
    }
    return {**evidence, "evidence_sha256": _sha256_bytes(_canonical_json(evidence))}


def _historical_album_receipt(
    backup_sha256: str,
    release: ReleaseIdentity,
) -> dict[str, Any]:
    pending = _historical_album_pending_receipt(backup_sha256)
    completion = _historical_album_completion_evidence(release)
    receipt = {
        "schema": ALBUM_RECOVERY_RECEIPT_SCHEMA,
        "status": "clear",
        "reset_count": len(HISTORICAL_ALBUM_UPDATE_IDS),
        "completed_update_count": len(HISTORICAL_ALBUM_UPDATE_IDS),
        "plan_sha256": HISTORICAL_ALBUM_PLAN_SHA256,
        "backup_sha256": pending["backup_sha256"],
        "cas_receipt_sha256": pending["receipt_sha256"],
        "completion_evidence_sha256": completion["evidence_sha256"],
        "release_tree_sha256": release.tree_manifest_sha256,
    }
    return {**receipt, "receipt_sha256": _sha256_bytes(_canonical_json(receipt))}


def recover_historical_album(
    conn: sqlite3.Connection,
    *,
    v2_binary_live: Callable[[], bool],
    verified_backup: Callable[[], str],
    bridge_lease_held: Callable[[], bool],
) -> dict[str, Any]:
    """Reset only the exact ten historical siblings after v2 is live."""

    if not v2_binary_live():
        raise ReleaseFailure("album_v2_binary_not_live")
    if not bridge_lease_held():
        raise ReleaseFailure("bridge_writer_not_quiesced")
    initial = _historical_album_manifest(conn)
    backup_sha = _closed_hash(verified_backup(), "inbox_backup_invalid")
    with _immediate(conn):
        if not bridge_lease_held() or _historical_album_manifest(conn) != initial:
            raise ReleaseFailure("historical_album_plan_drifted")
        placeholders = ",".join("?" for _ in HISTORICAL_ALBUM_UPDATE_IDS)
        cursor = conn.execute(
            f"""UPDATE updates
                   SET status='pending',last_error='',next_attempt_at=0,failed_at=NULL
                 WHERE update_id IN ({placeholders})
                   AND status='dead_letter' AND attempts=0 AND backend_response_json IS NULL""",  # nosec B608
            HISTORICAL_ALBUM_UPDATE_IDS,
        )
        if cursor.rowcount != len(HISTORICAL_ALBUM_UPDATE_IDS):
            raise ReleaseFailure("historical_album_cas_failed")
    return _historical_album_pending_receipt(backup_sha)


def _add_systemd_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--anchor", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--env-file-sha256", required=True)
    parser.add_argument("--friday-home", required=True, type=Path)
    parser.add_argument("--unit-dir", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--inbox-database", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--health-ca", required=True, type=Path)
    parser.add_argument("--health-ca-sha256", required=True)
    parser.add_argument(
        "--memory-vault-mode",
        choices=MEMORY_VAULT_MODES,
        default="disabled",
        help="Effective code-owned plaintext projection mode expected from the candidate",
    )
    parser.add_argument(
        "--obsidian-mode",
        choices=OBSIDIAN_MODES,
        default="disabled",
        help="Exact Obsidian integration mode expected from the candidate environment",
    )
    parser.add_argument(
        "--obsidian-root",
        type=Path,
        help="Dedicated owner-private Obsidian root (defaults to FRIDAY_HOME/data/obsidian)",
    )
    parser.add_argument("--health-url", default="https://127.0.0.1:8000/api/health")
    parser.add_argument("--alias-claim-manifest", action="append", type=Path, default=[])
    parser.add_argument("--alias-expect-count", action="append", type=int, default=[])
    parser.add_argument("--alias-expect-plan-sha256", action="append", default=[])


def _systemd_config(args: argparse.Namespace) -> SystemdConfig:
    return SystemdConfig(
        anchor=args.anchor,
        env_file=args.env_file,
        env_file_sha256=args.env_file_sha256,
        friday_home=args.friday_home,
        unit_dir=args.unit_dir,
        database=args.database,
        inbox_database=args.inbox_database,
        backup_dir=args.backup_dir,
        state_dir=args.state_dir,
        health_ca=args.health_ca,
        health_ca_sha256=args.health_ca_sha256,
        memory_vault_mode=args.memory_vault_mode,
        obsidian_mode=args.obsidian_mode,
        obsidian_root=args.obsidian_root,
        next_env_file=getattr(args, "next_env_file", None),
        next_env_file_sha256=getattr(args, "next_env_file_sha256", None) or "",
        staged_config_transition=getattr(args, "staged_config_transition", None) or "",
        secondary_rollout_receipt=getattr(args, "secondary_rollout_receipt", None),
        secondary_rollout_receipt_sha256=(getattr(args, "secondary_rollout_receipt_sha256", None) or ""),
        alias_claim_manifests=tuple(args.alias_claim_manifest),
        alias_expected_counts=tuple(args.alias_expect_count),
        alias_expected_plan_sha256s=tuple(args.alias_expect_plan_sha256),
        health_url=args.health_url,
    )


def _activation_recovery_systemd_config(
    config: SystemdConfig,
    state: Mapping[str, Any],
) -> SystemdConfig:
    if config.staged_transition_validation_sha256:
        raise ReleaseFailure("recovery_staged_transition_validation_not_permitted")
    transition = _staged_config_transition(state)
    if transition is None:
        return config
    transition_name, predecessor_env_sha256, next_env_file, next_env_file_sha256 = transition
    phase = str(state.get("phase") or "")
    if (
        (transition_name == _OBSIDIAN_ENABLE_TRANSITION and config.obsidian_mode != "enabled")
        or config.next_env_file is not None
        or config.next_env_file_sha256
        or config.staged_config_transition
        or Path(os.path.abspath(next_env_file)).parent != _private_directory(config.state_dir)
    ):
        raise ReleaseFailure("recovery_staged_transition_invalid")
    current = _read_private_regular_file(
        config.env_file,
        maximum_bytes=1 << 20,
        code="environment_file_invalid",
    )
    current_sha256 = _sha256_bytes(current)
    validation_sha256 = str(
        state.get("staged_transition_validation_sha256") or ""
    )
    expected_validation_sha256 = _staged_transition_validation_sha256(
        transition_name,
        predecessor_env_sha256,
        next_env_file,
        next_env_file_sha256,
    )
    special_transition = (
        transition_name == _SEMANTIC_SUPERVISOR_ASSIST_TO_CANARY_TRANSITION
    )
    if (
        current_sha256 != config.env_file_sha256
        or current_sha256 not in {predecessor_env_sha256, next_env_file_sha256}
        or state.get("backup") is None
        and current_sha256 != predecessor_env_sha256
        or state.get("backup") is None
        and (
            state.get("database_mutation_possible") is not False
            or state.get("writer_target") not in {"", "previous"}
            or phase in _TERMINAL_JOURNAL_PHASES
            and (phase not in {"rolled_back", "recovered"} or state.get("writer_target") != "previous")
        )
        or (
            special_transition
            and (state.get("backup") is not None or current_sha256 == next_env_file_sha256)
            and validation_sha256 != expected_validation_sha256
        )
        or (not special_transition and bool(validation_sha256))
        or (
            validation_sha256
            and validation_sha256 != expected_validation_sha256
        )
    ):
        raise ReleaseFailure("environment_file_changed")
    return replace(
        config,
        env_file_sha256=predecessor_env_sha256,
        next_env_file=next_env_file,
        next_env_file_sha256=next_env_file_sha256,
        staged_config_transition=transition_name,
        staged_transition_validation_sha256=validation_sha256,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--commit", required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--wheel", required=True, type=Path)
    build.add_argument("--wheel-sha256", required=True)
    build.add_argument("--runtime-lock", required=True, type=Path)
    build.add_argument("--runtime-lock-sha256", required=True)
    build.add_argument("--wheelhouse", required=True, type=Path)
    build.add_argument("--wheelhouse-manifest", required=True, type=Path)
    build.add_argument("--wheelhouse-manifest-sha256", required=True)
    build.add_argument("--releases-root", required=True, type=Path)
    build.add_argument("--anchor", required=True, type=Path)
    build.add_argument("--env-file", required=True, type=Path)
    build.add_argument("--friday-home", required=True, type=Path)
    build.add_argument("--base-python", required=True, type=Path)
    build.add_argument("--base-python-sha256", required=True)
    build.add_argument("--alias-tool", required=True, type=Path)
    build.add_argument("--alias-tool-sha256", required=True)
    build.add_argument("--alias-dependency", required=True, type=Path)
    build.add_argument("--alias-dependency-sha256", required=True)
    build.add_argument(
        "--secondary-product-runner",
        required=True,
        type=Path,
        help=("Exact deploy/secondary-brain/windows-sglang/scripts/live_failure_battery.py source artifact"),
    )
    build.add_argument("--secondary-product-runner-sha256", required=True)
    build.add_argument("--max-schema", required=True, type=int)

    install = commands.add_parser("install-units")
    install.add_argument("--release", required=True, type=Path)
    install.add_argument("--release-tree-sha256", required=True)
    install.add_argument("--previous", required=True, type=Path)
    install.add_argument("--previous-tree-sha256", required=True)
    install.add_argument("--anchor", required=True, type=Path)
    install.add_argument("--unit-dir", required=True, type=Path)
    install.add_argument("--state-dir", required=True, type=Path)
    install.add_argument("--backup-dir", required=True, type=Path)
    install.add_argument("--transition-runtime-root", required=True, type=Path)
    install.add_argument("--transition-backend-unit-sha256", required=True)
    install.add_argument("--transition-bridge-unit-sha256", required=True)

    activate = commands.add_parser("activate")
    activate.add_argument("--candidate", required=True, type=Path)
    activate.add_argument("--candidate-tree-sha256", required=True)
    activate.add_argument("--previous", required=True, type=Path)
    activate.add_argument("--previous-tree-sha256", required=True)
    activate.add_argument("--schema-capable-fallback", required=True, type=Path)
    activate.add_argument("--schema-capable-fallback-tree-sha256", required=True)
    activate.add_argument(
        "--terminal-journal-env-sha256",
        help=(
            "Exact pre-edit env-file digest used only to authenticate a terminal "
            "activation journal during an explicitly permitted config transition"
        ),
    )
    _add_systemd_arguments(activate)
    activate.add_argument(
        "--next-env-file",
        type=Path,
        help="Private staged ENV1 file used only after the verified backup boundary",
    )
    activate.add_argument("--next-env-file-sha256")
    activate.add_argument(
        "--staged-config-transition",
        choices=tuple(sorted(_STAGED_CONFIG_TRANSITIONS)),
        help=(
            "Explicit immutable ENV0 to ENV1 transition (Obsidian, Engineer Mode, exact "
            "secondary finalist, or semantic-supervisor rollout state change); omitted staged "
            "activations retain the established obsidian_enable contract"
        ),
    )
    activate.add_argument(
        "--secondary-rollout-receipt",
        type=Path,
        help=(
            "Owner-private automatic predecessor product-stage receipt; required only for "
            "public-shadow to private-shadow and private-shadow to assist promotions"
        ),
    )
    activate.add_argument("--secondary-rollout-receipt-sha256")

    recovery = commands.add_parser("recover-historical-album")
    recovery.add_argument("--release", required=True, type=Path)
    recovery.add_argument("--release-tree-sha256", required=True)
    _add_systemd_arguments(recovery)
    activation_recovery = commands.add_parser("recover-activation")
    activation_recovery.add_argument("--executor-release", required=True, type=Path)
    activation_recovery.add_argument("--executor-tree-sha256", required=True)
    _add_systemd_arguments(activation_recovery)
    return parser


def _run_cli(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "build":
        release = build_release(
            BuildSpec(
                commit=args.commit,
                version=args.version,
                wheel=args.wheel,
                wheel_sha256=args.wheel_sha256,
                runtime_lock=args.runtime_lock,
                runtime_lock_sha256=args.runtime_lock_sha256,
                wheelhouse=args.wheelhouse,
                wheelhouse_manifest=args.wheelhouse_manifest,
                wheelhouse_manifest_sha256=args.wheelhouse_manifest_sha256,
                releases_root=args.releases_root,
                anchor=args.anchor,
                env_file=args.env_file,
                friday_home=args.friday_home,
                base_python=args.base_python,
                base_python_sha256=args.base_python_sha256,
                alias_tool=args.alias_tool,
                alias_tool_sha256=args.alias_tool_sha256,
                alias_dependency=args.alias_dependency,
                alias_dependency_sha256=args.alias_dependency_sha256,
                secondary_product_runner=args.secondary_product_runner,
                secondary_product_runner_sha256=args.secondary_product_runner_sha256,
                max_schema=args.max_schema,
            )
        )
        receipt = {
            "schema": OPERATOR_SCHEMA,
            "operation": "build",
            "status": "clear",
            "commit": release.commit,
            "version": release.version,
            "max_schema": release.max_schema,
            "tree_manifest_sha256": release.tree_manifest_sha256,
        }
    elif args.command == "install-units":
        release = load_release_identity(
            args.release,
            expected_tree_sha256=args.release_tree_sha256,
        )
        _require_candidate_bound_operator(release)
        previous = load_release_identity(
            args.previous,
            expected_tree_sha256=args.previous_tree_sha256,
        )
        if release.root == previous.root or release.commit == previous.commit:
            raise ReleaseFailure("candidate_previous_identity_not_distinct")
        with OperatorTransactionLock(args.state_dir / "immutable-release-operator.v1.lock"):
            journal = DurableActivationJournal(
                args.state_dir / "immutable-release-activation.v1.json",
                backup_root=args.backup_dir,
                config_identity_sha256=None,
            )
            if journal.path.exists() or journal.path.is_symlink():
                phase = str(journal.load().get("phase") or "")
                if phase not in _TERMINAL_JOURNAL_PHASES:
                    raise ReleaseFailure("unfinished_activation_requires_recovery")
            unit_hashes = install_units(
                release,
                previous,
                unit_dir=args.unit_dir,
                anchor=args.anchor,
                transition_runtime_root=args.transition_runtime_root,
                transition_unit_hashes={
                    "friday-backend.service": args.transition_backend_unit_sha256,
                    "friday-bridge.service": args.transition_bridge_unit_sha256,
                },
                journal=DurableUnitInstallJournal(args.state_dir / "immutable-release-unit-install.v1.json"),
            )
        receipt = {
            "schema": OPERATOR_SCHEMA,
            "operation": "install-units",
            "status": "clear",
            "release_tree_sha256": release.tree_manifest_sha256,
            "previous_tree_sha256": previous.tree_manifest_sha256,
            "unit_hashes": unit_hashes,
        }
    elif args.command == "activate":
        config = _systemd_config(args)
        staged_config_transition = _requested_staged_config_transition(config)
        _secondary_rollout_receipt_stage(config)
        target_config = _activation_target_config(config)
        if config.next_env_file is not None and (
            args.terminal_journal_env_sha256 is None
            or _closed_hash(
                args.terminal_journal_env_sha256,
                "terminal_journal_env_digest_invalid",
            )
            != config.env_file_sha256
        ):
            raise ReleaseFailure("staged_predecessor_env_digest_mismatch")
        port = SystemdActivationPort(config)
        with OperatorTransactionLock(config.state_dir / "immutable-release-operator.v1.lock"):
            journal = DurableActivationJournal(
                config.state_dir / "immutable-release-activation.v1.json",
                backup_root=config.backup_dir,
                config_identity_sha256=_systemd_config_identity(target_config),
                legacy_config_identity_sha256=_activation_legacy_config_identity(
                    target_config,
                    args.terminal_journal_env_sha256,
                ),
                legacy_v2_config_identity_sha256=_activation_v2_config_identity(
                    target_config,
                    args.terminal_journal_env_sha256,
                ),
                transition_config_identity_sha256=(
                    _activation_transition_predecessor_identity(
                        target_config,
                        args.terminal_journal_env_sha256,
                        staged_config_transition,
                    )
                    if staged_config_transition
                    else _activation_obsidian_predecessor_identity(
                        target_config,
                        args.terminal_journal_env_sha256,
                    )
                ),
                config_scope_sha256=_systemd_config_scope_identity(target_config),
                config_retry_scope_sha256=_systemd_config_retry_scope_identity(target_config),
                alias_claim_count=len(target_config.alias_claim_manifests),
                memory_vault_mode=target_config.memory_vault_mode,
                obsidian_mode=target_config.obsidian_mode,
                obsidian_root_sha256=_obsidian_root_sha256(target_config),
                predecessor_env_sha256=(config.env_file_sha256 if config.next_env_file is not None else None),
                next_env_file=config.next_env_file,
                next_env_file_sha256=config.next_env_file_sha256 or None,
                staged_config_transition=staged_config_transition or None,
            )
            candidate = load_release_identity(
                args.candidate,
                expected_tree_sha256=args.candidate_tree_sha256,
            )
            _require_candidate_bound_operator(candidate)
            _require_completed_unit_install(config.state_dir, candidate)
            receipt = activate_release(
                port,
                journal,
                candidate=candidate,
                previous=load_release_identity(
                    args.previous,
                    expected_tree_sha256=args.previous_tree_sha256,
                ),
                schema_capable_fallback=load_release_identity(
                    args.schema_capable_fallback,
                    expected_tree_sha256=args.schema_capable_fallback_tree_sha256,
                ),
            )
    elif args.command == "recover-activation":
        config = _systemd_config(args)
        with OperatorTransactionLock(config.state_dir / "immutable-release-operator.v1.lock"):
            journal_path = config.state_dir / "immutable-release-activation.v1.json"
            journal_probe = DurableActivationJournal(
                journal_path,
                backup_root=config.backup_dir,
                config_identity_sha256=None,
            )
            config = _activation_recovery_systemd_config(config, journal_probe.load())
            target_config = _activation_target_config(config)
            port = SystemdActivationPort(config)
            journal = DurableActivationJournal(
                journal_path,
                backup_root=config.backup_dir,
                config_identity_sha256=_systemd_config_identity(target_config),
                legacy_config_identity_sha256=_systemd_config_identity_v1(target_config),
                config_scope_sha256=_systemd_config_scope_identity(target_config),
                config_retry_scope_sha256=_systemd_config_retry_scope_identity(target_config),
                alias_claim_count=len(target_config.alias_claim_manifests),
                memory_vault_mode=target_config.memory_vault_mode,
                obsidian_mode=target_config.obsidian_mode,
                obsidian_root_sha256=_obsidian_root_sha256(target_config),
                predecessor_env_sha256=(config.env_file_sha256 if config.next_env_file is not None else None),
                next_env_file=config.next_env_file,
                next_env_file_sha256=config.next_env_file_sha256 or None,
                staged_config_transition=(config.staged_config_transition or None),
            )
            executor = load_release_identity(
                args.executor_release,
                expected_tree_sha256=args.executor_tree_sha256,
            )
            _require_candidate_bound_operator(executor)
            candidate, _previous, fallback = journal.release_identities()
            if executor.root not in {candidate.root, fallback.root}:
                raise ReleaseFailure("recovery_executor_not_schema_capable_release")
            _require_completed_unit_install(config.state_dir, candidate)
            receipt = recover_interrupted_activation(port, journal)
    elif args.command == "recover-historical-album":
        config = _systemd_config(args)
        port = SystemdActivationPort(config)
        release = load_release_identity(
            args.release,
            expected_tree_sha256=args.release_tree_sha256,
        )
        _require_candidate_bound_operator(release)
        with OperatorTransactionLock(config.state_dir / "immutable-release-operator.v1.lock"):
            _require_completed_unit_install(config.state_dir, release)
            receipt = port.recover_historical_album_live(release)
    else:  # pragma: no cover - argparse owns the closed set
        raise ReleaseFailure("unknown_operation")
    return {**receipt, "operator_schema": OPERATOR_SCHEMA}


def main(argv: list[str] | None = None) -> int:
    try:
        receipt = _run_cli(build_parser().parse_args(argv))
        print(json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
        return 0
    except ReleaseFailure as exc:
        print(
            json.dumps(
                {
                    "schema": OPERATOR_SCHEMA,
                    "status": "failed_closed",
                    "failure_code": str(exc),
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - never publish exception text
        print(
            json.dumps(
                {
                    "schema": OPERATOR_SCHEMA,
                    "status": "failed_closed",
                    "failure_code": f"internal_{type(exc).__name__}",
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 3


__all__ = [
    "ACTIVATION_RECEIPT_SCHEMA",
    "ACTIVATION_JOURNAL_SCHEMA",
    "ALIAS_REPAIR_RECEIPT_SCHEMA",
    "ALBUM_RECOVERY_JOURNAL_SCHEMA",
    "ALBUM_RECOVERY_RECEIPT_SCHEMA",
    "BOOTSTRAP_WHEELS",
    "BUILD_RECEIPT_SCHEMA",
    "FORBIDDEN_ROLLBACK_COMMITS",
    "HISTORICAL_ALBUM_PLAN_SHA256",
    "HISTORICAL_ALBUM_UPDATE_IDS",
    "OBSIDIAN_CUTOVER_CONTRACT",
    "ActivationPort",
    "DurableAlbumRecoveryJournal",
    "DurableActivationJournal",
    "DurableUnitInstallJournal",
    "BuildSpec",
    "DatabaseBackup",
    "OperatorTransactionLock",
    "ReleaseFailure",
    "ReleaseIdentity",
    "SystemdActivationPort",
    "SystemdConfig",
    "UNIT_INSTALL_JOURNAL_SCHEMA",
    "activate_release",
    "build_release",
    "installed_surface_smoke",
    "install_units",
    "load_release_identity",
    "recover_historical_album",
    "recover_interrupted_activation",
    "render_units",
    "verify_release_tree",
]


if __name__ == "__main__":
    raise SystemExit(main())
