"""Closed, body-free authenticity contour for secondary-brain product witnesses."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from friday import __version__
from friday.audit_privacy import decode_audit_privacy_key

SECONDARY_PRODUCT_WITNESS_STAGES = (
    "public-shadow",
    "private-shadow",
    "assist",
    "outage",
    "cooldown",
    "recovery",
)
SECONDARY_PRODUCT_WITNESS_SOURCE_PREFIX = "secondary-product-witness:"
SECONDARY_PRODUCT_STORAGE_BINDING_SCHEMA = "friday.secondary-product-storage-binding.v1"
SECONDARY_PRODUCT_DIAGNOSTICS_SCHEMA = "friday.secondary-product-diagnostics.v1"
SECONDARY_PRODUCT_ADVICE_PROOF_SCHEMA = "friday.secondary-product-advice-proof.v1"
SECONDARY_PRODUCT_OPERATION_CORE_SCHEMA = "friday.secondary-product-operation-core.v1"
SECONDARY_PRODUCT_CLEANUP_CORE_SCHEMA = "friday.secondary-product-cleanup-core.v1"
SECONDARY_PRODUCT_ZERO_RESIDUE_SCHEMA = "friday.secondary-product-cleanup-zero-residue.v1"
SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_SCHEMA = "friday.secondary-product-rollout-attestation.v1"
SECONDARY_PRODUCT_CONSUME_REQUEST_SCHEMA = "friday.secondary-product-rollout-consume-request.v1"
SECONDARY_PRODUCT_CONSUME_RESPONSE_SCHEMA = "friday.secondary-product-rollout-consume-response.v1"
SECONDARY_PRODUCT_CONSUME_BINDING_SCHEMA = "friday.secondary-product-rollout-consume-binding.v1"
SECONDARY_PRODUCT_BACKUP_LEASE_FILENAME = "secondary-product-witness-backup.lock"
SECONDARY_PRODUCT_BACKUP_LEASE_PROTOCOL = "friday.secondary-product-witness-backup.v1"
SECONDARY_PRODUCT_ATTESTATION_TTL_SEC = 570
SECONDARY_PRODUCT_ATTESTATION_SKEW_SEC = 30

_SOURCE_REF = re.compile(
    r"secondary-product-witness:(public-shadow|private-shadow|assist|outage|cooldown|recovery):"
    r"([0-9a-f]{32})\Z"
)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_ATTESTATION_ID = re.compile(r"[0-9a-f]{32}\Z")

SECONDARY_PRODUCT_ADVICE_PROOF_KEYS = frozenset(
    {
        "schema",
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
        "issued_at",
        "expires_at",
        "signature",
    }
)

SECONDARY_PRODUCT_OPERATION_CORE_KEYS = frozenset(
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
        "advice_proof_sha256",
        "stage_diagnostics_binding_sha256",
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

SECONDARY_PRODUCT_ZERO_RESIDUE_KEYS = frozenset(
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

SECONDARY_PRODUCT_CLEANUP_CORE_KEYS = frozenset(
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

SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_KEYS = frozenset(
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
        "operation_binding_sha256",
        "stage_diagnostics_binding_sha256",
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
        "raw_residue",
        "inbox_residue",
        "knowledge_residue",
        "alias_residue",
        "ko_state_residue",
        "feedback_residue",
        "feedback_state_residue",
        "review_residue",
        "lookup_token_sha256",
        "state_version",
        "issued_at",
        "expires_at",
        "signature",
    }
)

SECONDARY_PRODUCT_CONSUME_REQUEST_KEYS = frozenset(
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

SECONDARY_PRODUCT_CONSUME_RESPONSE_KEYS = frozenset(
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

SECONDARY_PRODUCT_STAGE_TRANSITIONS = {
    "public-shadow": "secondary_shadow_to_private_shadow",
    "private-shadow": "secondary_shadow_to_assist",
}


def secondary_product_canonical(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def secondary_product_sha256(value: Mapping[str, Any] | bytes | str) -> str:
    raw = (
        secondary_product_canonical(value)
        if isinstance(value, Mapping)
        else value.encode("utf-8")
        if isinstance(value, str)
        else value
    )
    return hashlib.sha256(raw).hexdigest()


def secondary_product_signing_key(storage: Any) -> bytes:
    row = storage.execute("SELECT value FROM schema_meta WHERE key='audit_privacy_hmac_key'").fetchone()
    return decode_audit_privacy_key(row[0] if row is not None else None)


def _sign(key: bytes, schema: str, projection: Mapping[str, Any]) -> str:
    return hmac.new(
        key,
        schema.encode("ascii") + b"\0" + secondary_product_canonical(projection),
        hashlib.sha256,
    ).hexdigest()


def _valid_sha(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def secondary_product_witness_source_ref(stage: str, nonce: str) -> str:
    if stage not in SECONDARY_PRODUCT_WITNESS_STAGES or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise ValueError("secondary product witness stage is outside the closed set")
    return f"{SECONDARY_PRODUCT_WITNESS_SOURCE_PREFIX}{stage}:{nonce}"


def secondary_product_witness_content(stage: str, nonce: str) -> str:
    source_ref = secondary_product_witness_source_ref(stage, nonce)
    return (
        f"Synthetic Friday secondary witness ({stage}; {source_ref.rsplit(':', 1)[-1]}). "
        "Project Atlas uses PostgreSQL 16 for a bounded advisory check."
    )


def parse_secondary_product_witness_source_ref(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    matched = _SOURCE_REF.fullmatch(value)
    return (matched.group(1), matched.group(2)) if matched else None


def is_secondary_product_witness_raw(raw: Mapping[str, Any] | None) -> bool:
    if not isinstance(raw, Mapping):
        return False
    source_ref = str(raw.get("source_ref") or "")
    parsed = parse_secondary_product_witness_source_ref(source_ref)
    if parsed is None:
        return False
    stage, nonce = parsed
    content = secondary_product_witness_content(stage, nonce)
    return (
        raw.get("source") == "api"
        and _metadata(raw.get("metadata_json")).get("secondary_product_witness") is True
        and raw.get("raw_content") == content
        and raw.get("content_hash") == hashlib.sha256(content.encode()).hexdigest()
    )


def secondary_product_storage_binding(raw: Mapping[str, Any], inbox: Mapping[str, Any]) -> str:
    """Hash the exact persisted Raw/Inbox relation without returning either body."""
    if (
        not is_secondary_product_witness_raw(raw)
        or inbox.get("raw_object_id") != raw.get("id")
        or inbox.get("user_id") != raw.get("user_id")
    ):
        raise ValueError("secondary product witness storage relation is invalid")
    uploaded_by = _metadata(raw.get("metadata_json")).get("uploaded_by")
    if not isinstance(uploaded_by, str) or not uploaded_by:
        raise ValueError("secondary product witness uploader binding is invalid")
    return secondary_product_sha256(
        {
            "schema": SECONDARY_PRODUCT_STORAGE_BINDING_SCHEMA,
            "source": "api",
            "storage_user_id": raw.get("user_id"),
            "source_ref": raw.get("source_ref"),
            "metadata_marker": True,
            "uploaded_by": uploaded_by,
            "content_sha256": raw.get("content_hash"),
            "raw_object_id": raw.get("id"),
            "inbox_id": inbox.get("id"),
            "inbox_status": inbox.get("status"),
            "knowledge_object_id": inbox.get("knowledge_object_id"),
        }
    )


def secondary_product_advice_storage_binding(item: Mapping[str, Any], suggestions: Mapping[str, Any]) -> str:
    """Match the product runner's exact body-free persisted advice projection."""
    stored = _metadata(item.get("suggestions_json"))
    if secondary_product_canonical(stored) != secondary_product_canonical(suggestions):
        raise ValueError("secondary product advice storage differs from result")
    inbox_id, raw_id = str(item.get("id") or ""), str(item.get("raw_object_id") or "")
    if (
        not inbox_id
        or not raw_id
        or item.get("status") != "pending"
        or item.get("knowledge_object_id") is not None
    ):
        raise ValueError("secondary product advice storage is not pending Inbox-only")
    reviewed_at, reviewed_by = item.get("reviewed_at"), item.get("reviewed_by")
    if (reviewed_at is None) != (reviewed_by is None):
        raise ValueError("secondary product advice review identity is invalid")
    return secondary_product_sha256(
        {
            "inbox_id_sha256": secondary_product_sha256(inbox_id),
            "raw_object_id_sha256": secondary_product_sha256(raw_id),
            "status": "pending",
            "knowledge_object_id": None,
            "reviewed": reviewed_at is not None,
            "reviewed_at_sha256": secondary_product_sha256(str(reviewed_at or "")),
            "reviewed_by_sha256": secondary_product_sha256(str(reviewed_by or "")),
            "suggested_action": str(item.get("suggested_action") or "")[:32],
            "suggestions_sha256": secondary_product_sha256(dict(suggestions)),
        }
    )


def secondary_product_diagnostics_receipt(
    source_ref: str, before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind one exact synthetic source to bounded scheduler counter snapshots."""
    if parse_secondary_product_witness_source_ref(source_ref) is None:
        raise ValueError("secondary product diagnostics source is invalid")

    def product_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
        workloads = value.get("workloads")
        workload = workloads.get("extract") if isinstance(workloads, Mapping) else None
        shadow = value.get("shadow")
        if not isinstance(workload, Mapping) or not isinstance(shadow, Mapping):
            raise ValueError("secondary product diagnostics projection is invalid")

        def integer(raw: Any) -> int:
            if raw is None:
                raise ValueError("secondary product diagnostics counter is invalid")
            return int(raw)

        def seconds(raw: Any) -> float:
            if raw is None:
                raise ValueError("secondary product diagnostics duration is invalid")
            return round(float(raw), 3)

        def reasons(raw: Any) -> dict[str, int]:
            if not isinstance(raw, Mapping):
                raise ValueError("secondary product diagnostics reasons are invalid")
            return dict(sorted((str(key), int(count)) for key, count in raw.items() if int(count)))

        return {
            "schema": value.get("schema"),
            "role": value.get("role"),
            "enabled": value.get("enabled"),
            "configured": value.get("configured"),
            "mode": value.get("mode"),
            "state": value.get("state"),
            "available": value.get("available"),
            "last_failure": value.get("last_failure"),
            "profile_id": value.get("profile"),
            "profile_admission": value.get("profile_admission"),
            "profile_manifest_match": value.get("profile_manifest_match"),
            "served_model_match": value.get("served_model_match"),
            **{
                key: integer(value.get(key))
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
                )
            },
            "circuit_retry_after_sec": seconds(value.get("circuit_retry_after_sec")),
            "skip_reasons": reasons(value.get("skip_reasons")),
            "fallback_reasons": reasons(value.get("fallback_reasons")),
            "shadow": {
                key: integer(shadow.get(key))
                for key in ("in_flight", "invalid_total", "skipped_total", "valid_total")
            },
            "workload": {
                "name": "extract",
                "selected_total": integer(workload.get("selected_total")),
                "success_total": integer(workload.get("success_total")),
                "skip_reasons": reasons(workload.get("skip_reasons")),
                "fallback_reasons": reasons(workload.get("fallback_reasons")),
            },
        }

    value = {
        "schema": SECONDARY_PRODUCT_DIAGNOSTICS_SCHEMA,
        "source_ref_sha256": secondary_product_sha256(source_ref),
        "before": product_snapshot(before),
        "after": product_snapshot(after),
    }
    return {**value, "binding_sha256": secondary_product_sha256(value)}


def secondary_product_process_epoch_sha256(pid: int | None = None) -> str:
    process_id = os.getpid() if pid is None else pid
    if isinstance(process_id, bool) or process_id < 2:
        raise RuntimeError("secondary product primary process identity is invalid")
    try:
        text = (Path("/proc") / str(process_id) / "stat").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("secondary product primary process identity is unavailable") from exc
    closing = text.rfind(")")
    fields = text[closing + 2 :].split() if closing > 0 else []
    if len(fields) < 20 or not fields[19].isdigit():
        raise RuntimeError("secondary product primary process identity is invalid")
    return secondary_product_sha256(f"{process_id}:{fields[19]}")


def secondary_product_primary_certificate_sha256(settings: Any) -> str:
    """Hash the exact configured primary TLS trust certificate without following a symlink."""
    path_value = str(getattr(settings, "backend_ca_file", "") or getattr(settings, "ssl_certfile", "") or "")
    if not path_value:
        raise RuntimeError("secondary product primary TLS certificate is not configured")
    path, descriptor = Path(path_value), None
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= 65_536:
            raise RuntimeError("secondary product primary TLS certificate is invalid")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
        before = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw, after = stream.read(65_537), os.fstat(stream.fileno())
        if len(raw) > 65_536 or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RuntimeError("secondary product primary TLS certificate changed while read")
        return hashlib.sha256(raw).hexdigest()
    except OSError as exc:
        raise RuntimeError("secondary product primary TLS certificate is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def secondary_product_runtime_identity(secondary: Any) -> dict[str, Any]:
    projection: Any = getattr(secondary, "product_attestation_identity", lambda: {})()
    keys = {
        "candidate_profile_id",
        "candidate_profile_mode",
        "candidate_profile_allow_private_text",
        "candidate_profile_context_tokens",
        "candidate_profile_sha256",
        "candidate_profile_manifest_sha256",
        "candidate_profile_admission",
        "served_model_alias",
        "gateway_ca_certificate_sha256",
    }
    if (
        not isinstance(projection, dict)
        or set(projection) != keys
        or (
            not isinstance(projection["candidate_profile_id"], str)
            or not projection["candidate_profile_id"]
            or projection["candidate_profile_mode"] not in {"shadow", "assist"}
            or type(projection["candidate_profile_allow_private_text"]) is not bool
            or type(projection["candidate_profile_context_tokens"]) is not int
            or projection["candidate_profile_context_tokens"] <= 0
            or not _valid_sha(projection["candidate_profile_sha256"])
            or not _valid_sha(projection["candidate_profile_manifest_sha256"])
            or (
                projection["candidate_profile_admission"] == "provisional_shadow"
                and projection["candidate_profile_sha256"] != projection["candidate_profile_manifest_sha256"]
            )
            or projection["candidate_profile_admission"] not in {"provisional_shadow", "accepted"}
            or not isinstance(projection["served_model_alias"], str)
            or not projection["served_model_alias"]
            or not _valid_sha(projection["gateway_ca_certificate_sha256"])
        )
    ):
        raise RuntimeError("secondary product runtime identity is unavailable")
    return projection


def secondary_product_current_server_identity(settings: Any, secondary: Any) -> dict[str, Any]:
    """Return the live process/config identity rechecked at issue and consume time."""
    pid = os.getpid()
    return {
        "primary_pid": pid,
        "primary_process_epoch_sha256": secondary_product_process_epoch_sha256(pid),
        "primary_backend_version": __version__,
        "primary_ca_certificate_sha256": secondary_product_primary_certificate_sha256(settings),
        **secondary_product_runtime_identity(secondary),
    }


def _observer_identity(observer: Mapping[str, Any], *, settings: Any, secondary: Any) -> dict[str, Any]:
    expected = {
        "observer_source_head",
        "observer_runner_sha256",
        "primary_pid",
        "primary_process_epoch_sha256",
        "primary_backend_version",
        "primary_ca_certificate_sha256",
        "candidate_profile_sha256",
    }
    current = secondary_product_current_server_identity(settings, secondary)
    if set(observer) != expected or (
        not isinstance(observer.get("observer_source_head"), str)
        or _COMMIT.fullmatch(str(observer["observer_source_head"])) is None
        or not _valid_sha(observer.get("observer_runner_sha256"))
        or observer.get("primary_pid") != current["primary_pid"]
        or observer.get("primary_process_epoch_sha256") != current["primary_process_epoch_sha256"]
        or observer.get("primary_backend_version") != current["primary_backend_version"]
        or observer.get("primary_ca_certificate_sha256") != current["primary_ca_certificate_sha256"]
        or observer.get("candidate_profile_sha256") != current["candidate_profile_sha256"]
    ):
        raise ValueError("secondary product observer identity is invalid")
    return {
        "observer_source_head": observer["observer_source_head"],
        "observer_runner_sha256": observer["observer_runner_sha256"],
        **current,
    }


def issue_secondary_product_advice_proof(
    storage: Any,
    *,
    raw: Mapping[str, Any],
    result: Mapping[str, Any],
    observer: Mapping[str, Any],
    settings: Any,
    secondary: Any,
    now: int | None = None,
) -> dict[str, Any]:
    """Issue one server-origin proof over exact persisted advice, never its bodies."""
    if not is_secondary_product_witness_raw(raw):
        raise ValueError("secondary product advice proof source is invalid")
    parsed, item = parse_secondary_product_witness_source_ref(raw.get("source_ref")), result.get("item")
    suggestions, advice = result.get("suggestions"), result.get("model_advice")
    diagnostics = result.get("secondary_product_diagnostics")
    if (
        parsed is None
        or not isinstance(item, Mapping)
        or not isinstance(suggestions, Mapping)
        or not isinstance(advice, Mapping)
        or not isinstance(diagnostics, Mapping)
        or set(diagnostics) != {"schema", "source_ref_sha256", "before", "after", "binding_sha256"}
    ):
        raise ValueError("secondary product advice proof input is invalid")
    stage, _nonce = parsed
    expected_role = "secondary" if stage in {"assist", "recovery"} else "primary"
    endpoint_role, model = advice.get("endpoint_role"), advice.get("model")
    if endpoint_role != expected_role or not isinstance(model, str) or not model:
        raise ValueError("secondary product advice endpoint identity is invalid")
    identity = _observer_identity(observer, settings=settings, secondary=secondary)
    if expected_role == "secondary" and model != identity["served_model_alias"]:
        raise ValueError("secondary product advice model identity is invalid")
    uploaded_by = _metadata(raw.get("metadata_json")).get("uploaded_by")
    if not isinstance(uploaded_by, str) or not uploaded_by:
        raise ValueError("secondary product advice uploader identity is invalid")
    inbox = {
        "id": item.get("id"),
        "user_id": item.get("user_id"),
        "raw_object_id": item.get("raw_object_id"),
        "knowledge_object_id": item.get("knowledge_object_id"),
        "status": item.get("status"),
    }
    if inbox["user_id"] != raw.get("user_id") or inbox["raw_object_id"] != raw.get("id"):
        raise ValueError("secondary product advice persisted relation is invalid")
    issued_at = int(time.time()) if now is None else now
    if type(issued_at) is not int or issued_at < 1:
        raise ValueError("secondary product advice proof time is invalid")
    projection = {
        "schema": SECONDARY_PRODUCT_ADVICE_PROOF_SCHEMA,
        "stage": stage,
        "source_ref_sha256": secondary_product_sha256(str(raw["source_ref"])),
        "raw_object_id_sha256": secondary_product_sha256(str(raw["id"])),
        "inbox_id_sha256": secondary_product_sha256(str(item["id"])),
        "content_sha256": str(raw.get("content_hash") or ""),
        "uploader_sha256": secondary_product_sha256(uploaded_by),
        "ingest_storage_binding_sha256": secondary_product_storage_binding(raw, inbox),
        "advice_storage_binding_sha256": secondary_product_advice_storage_binding(item, suggestions),
        "advice_diagnostics_receipt_sha256": secondary_product_sha256(dict(diagnostics)),
        "diagnostics_binding_sha256": diagnostics.get("binding_sha256"),
        "advice_endpoint_role": endpoint_role,
        "advice_model_sha256": secondary_product_sha256(model),
        **identity,
        "issued_at": issued_at,
        "expires_at": issued_at + SECONDARY_PRODUCT_ATTESTATION_TTL_SEC,
    }
    key = secondary_product_signing_key(storage)
    return {**projection, "signature": _sign(key, SECONDARY_PRODUCT_ADVICE_PROOF_SCHEMA, projection)}


def verify_secondary_product_advice_proof(
    key: bytes, proof: Mapping[str, Any], *, now: int | None = None
) -> bool:
    if set(proof) != SECONDARY_PRODUCT_ADVICE_PROOF_KEYS:
        return False
    projection = {name: proof[name] for name in proof if name != "signature"}
    issued_at, expires_at = proof.get("issued_at"), proof.get("expires_at")
    current, signature = int(time.time()) if now is None else now, proof.get("signature")
    return bool(
        proof.get("schema") == SECONDARY_PRODUCT_ADVICE_PROOF_SCHEMA
        and proof.get("stage") in SECONDARY_PRODUCT_WITNESS_STAGES
        and type(issued_at) is int
        and type(expires_at) is int
        and type(current) is int
        and 0 < expires_at - issued_at <= SECONDARY_PRODUCT_ATTESTATION_TTL_SEC
        and current >= issued_at - SECONDARY_PRODUCT_ATTESTATION_SKEW_SEC
        and current <= expires_at
        and _valid_sha(signature)
        and hmac.compare_digest(str(signature), _sign(key, SECONDARY_PRODUCT_ADVICE_PROOF_SCHEMA, projection))
    )


def secondary_product_zero_residue_projection(
    *, raw_object_id_sha256: str, inbox_id_sha256: str, residues: Mapping[str, Any]
) -> dict[str, Any]:
    projection = {
        "schema": SECONDARY_PRODUCT_ZERO_RESIDUE_SCHEMA,
        "raw_object_id_sha256": raw_object_id_sha256,
        "inbox_id_sha256": inbox_id_sha256,
        **dict(residues),
    }
    if (
        set(projection) != SECONDARY_PRODUCT_ZERO_RESIDUE_KEYS
        or not _valid_sha(raw_object_id_sha256)
        or not _valid_sha(inbox_id_sha256)
        or any(
            type(projection[key]) is not int or projection[key] != 0
            for key in SECONDARY_PRODUCT_ZERO_RESIDUE_KEYS
            if key.endswith("_residue")
        )
    ):
        raise ValueError("secondary product cleanup residue is not zero")
    return projection


def secondary_product_cleanup_core(
    *,
    storage_binding_sha256: str,
    raw_object_id_sha256: str,
    inbox_id_sha256: str,
    residues: Mapping[str, Any],
) -> dict[str, Any]:
    zero = secondary_product_zero_residue_projection(
        raw_object_id_sha256=raw_object_id_sha256,
        inbox_id_sha256=inbox_id_sha256,
        residues=residues,
    )
    return {
        "schema": SECONDARY_PRODUCT_CLEANUP_CORE_SCHEMA,
        "purged": True,
        "raw_deleted": 1,
        "inbox_deleted": 1,
        "storage_binding_sha256": storage_binding_sha256,
        "raw_object_id_sha256": raw_object_id_sha256,
        "inbox_id_sha256": inbox_id_sha256,
        "cleanup_zero_residue_binding_sha256": secondary_product_sha256(zero),
        **{key: zero[key] for key in SECONDARY_PRODUCT_ZERO_RESIDUE_KEYS if key.endswith("_residue")},
    }


def validate_secondary_product_operation_core(operation: Mapping[str, Any]) -> bool:
    if (
        set(operation) != SECONDARY_PRODUCT_OPERATION_CORE_KEYS
        or operation.get("schema") != SECONDARY_PRODUCT_OPERATION_CORE_SCHEMA
    ):
        return False
    return bool(
        all(_valid_sha(value) for key, value in operation.items() if key.endswith("_sha256"))
        and type(operation.get("ingest_idempotent_replay")) is bool
        and operation.get("advice_endpoint_role") in {"primary", "secondary"}
        and type(operation.get("exact_secondary_model_observed")) is bool
        and operation.get("cleanup_status") == "purged"
        and operation.get("knowledge_object_created") is False
        and operation.get("tool_requested") is False
        and operation.get("effect_requested") is False
    )


def issue_secondary_product_rollout_attestation(
    key: bytes,
    *,
    advice_proof: Mapping[str, Any],
    operation: Mapping[str, Any],
    cleanup_core: Mapping[str, Any],
    now: int | None = None,
    attestation_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    issued_at = int(time.time()) if now is None else now
    if (
        not verify_secondary_product_advice_proof(key, advice_proof, now=issued_at)
        or not validate_secondary_product_operation_core(operation)
        or set(cleanup_core) != SECONDARY_PRODUCT_CLEANUP_CORE_KEYS
        or cleanup_core.get("schema") != SECONDARY_PRODUCT_CLEANUP_CORE_SCHEMA
        or operation.get("cleanup_core_sha256") != secondary_product_sha256(cleanup_core)
        or operation.get("advice_proof_sha256") != secondary_product_sha256(dict(advice_proof))
        or issued_at < advice_proof["issued_at"]
        or issued_at >= advice_proof["expires_at"]
    ):
        raise ValueError("secondary product rollout attestation input is invalid")
    identity = attestation_id or secrets.token_hex(16)
    if _ATTESTATION_ID.fullmatch(identity) is None:
        raise ValueError("secondary product rollout attestation id is invalid")
    projection = {
        "schema": SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_SCHEMA,
        "attestation_id": identity,
        **{
            key: advice_proof[key]
            for key in (
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
            )
        },
        "operation_binding_sha256": secondary_product_sha256(dict(operation)),
        "stage_diagnostics_binding_sha256": operation["stage_diagnostics_binding_sha256"],
        "advice_proof_sha256": secondary_product_sha256(dict(advice_proof)),
        **{
            key: advice_proof[key]
            for key in (
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
            )
        },
        "cleanup_storage_binding_sha256": cleanup_core["storage_binding_sha256"],
        "cleanup_zero_residue_binding_sha256": cleanup_core["cleanup_zero_residue_binding_sha256"],
        **{key: cleanup_core[key] for key in SECONDARY_PRODUCT_CLEANUP_CORE_KEYS if key.endswith("_residue")},
        "state_version": 1,
        "issued_at": issued_at,
        "expires_at": min(
            issued_at + SECONDARY_PRODUCT_ATTESTATION_TTL_SEC,
            int(advice_proof["expires_at"]),
        ),
    }
    lookup_token = hmac.new(
        key,
        b"friday.secondary-product-rollout-lookup-token.v1\0" + secondary_product_canonical(projection),
        hashlib.sha256,
    ).hexdigest()
    projection["lookup_token_sha256"] = secondary_product_sha256(lookup_token)
    return {
        **projection,
        "signature": _sign(key, SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_SCHEMA, projection),
    }, lookup_token


def verify_secondary_product_rollout_attestation(
    key: bytes, attestation: Mapping[str, Any], *, now: int | None = None
) -> bool:
    if set(attestation) != SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_KEYS:
        return False
    projection = {name: attestation[name] for name in attestation if name != "signature"}
    issued_at, expires_at = attestation.get("issued_at"), attestation.get("expires_at")
    current, signature = int(time.time()) if now is None else now, attestation.get("signature")
    return bool(
        attestation.get("schema") == SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_SCHEMA
        and _ATTESTATION_ID.fullmatch(str(attestation.get("attestation_id") or "")) is not None
        and attestation.get("stage") in SECONDARY_PRODUCT_WITNESS_STAGES
        and attestation.get("state_version") == 1
        and type(issued_at) is int
        and type(expires_at) is int
        and type(current) is int
        and 0 < expires_at - issued_at <= SECONDARY_PRODUCT_ATTESTATION_TTL_SEC
        and current >= issued_at - SECONDARY_PRODUCT_ATTESTATION_SKEW_SEC
        and current <= expires_at
        and all(attestation.get(key) == 0 for key in attestation if key.endswith("_residue"))
        and _valid_sha(signature)
        and hmac.compare_digest(
            str(signature), _sign(key, SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_SCHEMA, projection)
        )
    )


def secondary_product_rollout_lookup_token(key: bytes, attestation: Mapping[str, Any]) -> str:
    if not verify_secondary_product_rollout_attestation(key, attestation):
        raise ValueError("secondary product rollout attestation is invalid or stale")
    base = {
        name: attestation[name] for name in attestation if name not in {"signature", "lookup_token_sha256"}
    }
    token = hmac.new(
        key,
        b"friday.secondary-product-rollout-lookup-token.v1\0" + secondary_product_canonical(base),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(secondary_product_sha256(token), str(attestation["lookup_token_sha256"])):
        raise ValueError("secondary product rollout lookup token is invalid")
    return token


def validate_secondary_product_consume_request(value: Mapping[str, Any]) -> bool:
    stage = value.get("stage")
    return bool(
        set(value) == SECONDARY_PRODUCT_CONSUME_REQUEST_KEYS
        and value.get("schema") == SECONDARY_PRODUCT_CONSUME_REQUEST_SCHEMA
        and _SHA256.fullmatch(str(value.get("attestation_lookup_token") or "")) is not None
        and _valid_sha(value.get("server_rollout_attestation_sha256"))
        and isinstance(stage, str)
        and stage in SECONDARY_PRODUCT_STAGE_TRANSITIONS
        and value.get("transition") == SECONDARY_PRODUCT_STAGE_TRANSITIONS[stage]
        and _COMMIT.fullmatch(str(value.get("predecessor_commit") or "")) is not None
        and _COMMIT.fullmatch(str(value.get("candidate_commit") or "")) is not None
        and value.get("candidate_commit") != value.get("predecessor_commit")
        and all(
            _valid_sha(value.get(key))
            for key in (
                "predecessor_tree_sha256",
                "candidate_tree_sha256",
                "next_env_sha256",
                "product_receipt_sha256",
                "sealed_runner_sha256",
            )
        )
    )


def secondary_product_consume_response(
    key: bytes,
    *,
    request_value: Mapping[str, Any],
    attestation: Mapping[str, Any],
    consumed_at: int,
) -> dict[str, Any]:
    if not validate_secondary_product_consume_request(request_value) or not (
        verify_secondary_product_rollout_attestation(key, attestation, now=consumed_at)
        and hmac.compare_digest(
            str(request_value["server_rollout_attestation_sha256"]),
            secondary_product_sha256(dict(attestation)),
        )
    ):
        raise ValueError("secondary product rollout consume input is invalid")
    request_sha256 = secondary_product_sha256(dict(request_value))
    binding = {
        "schema": SECONDARY_PRODUCT_CONSUME_BINDING_SCHEMA,
        "attestation_signature_sha256": secondary_product_sha256(str(attestation["signature"])),
        "request_sha256": request_sha256,
        **{
            field: request_value[field]
            for field in (
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
            )
        },
        "lookup_token_sha256": attestation["lookup_token_sha256"],
        "consumed_at": consumed_at,
        "state_version": 2,
    }
    return {
        "schema": SECONDARY_PRODUCT_CONSUME_RESPONSE_SCHEMA,
        "status": "consumed",
        **{
            field: binding[field]
            for field in (
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
            )
        },
        "consume_binding_sha256": _sign(key, SECONDARY_PRODUCT_CONSUME_BINDING_SCHEMA, binding),
    }


__all__ = [name for name in globals() if name.startswith("SECONDARY_PRODUCT_")] + [
    "is_secondary_product_witness_raw",
    "issue_secondary_product_advice_proof",
    "issue_secondary_product_rollout_attestation",
    "parse_secondary_product_witness_source_ref",
    "secondary_product_advice_storage_binding",
    "secondary_product_canonical",
    "secondary_product_cleanup_core",
    "secondary_product_consume_response",
    "secondary_product_current_server_identity",
    "secondary_product_diagnostics_receipt",
    "secondary_product_primary_certificate_sha256",
    "secondary_product_process_epoch_sha256",
    "secondary_product_rollout_lookup_token",
    "secondary_product_runtime_identity",
    "secondary_product_sha256",
    "secondary_product_signing_key",
    "secondary_product_storage_binding",
    "secondary_product_witness_content",
    "secondary_product_witness_source_ref",
    "validate_secondary_product_consume_request",
    "validate_secondary_product_operation_core",
    "verify_secondary_product_advice_proof",
    "verify_secondary_product_rollout_attestation",
]
