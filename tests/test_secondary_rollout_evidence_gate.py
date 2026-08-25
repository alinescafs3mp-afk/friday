"""Fail-closed evidence gate for secondary-brain authority promotions."""

from __future__ import annotations

import copy
import hashlib
import importlib
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import friday.secondary_product_witness as witness
import tools.immutable_release_operator as operator

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "deploy" / "secondary-brain" / "windows-sglang" / "scripts"
RUNTIME = ROOT / "deploy" / "secondary-brain" / "windows-sglang" / "runtime"


def _previous(tmp_path: Path) -> operator.ReleaseIdentity:
    return operator.ReleaseIdentity(
        root=tmp_path / "previous",
        commit="5" * 40,
        version="0.207.10",
        tree_manifest_sha256="6" * 64,
        max_schema=39,
        secondary_product_runner_sha256="6" * 64,
    )


def _candidate(tmp_path: Path) -> operator.ReleaseIdentity:
    return operator.ReleaseIdentity(
        root=tmp_path / "candidate",
        commit="9" * 40,
        version="0.207.11",
        tree_manifest_sha256="1" * 64,
        max_schema=39,
        secondary_product_runner_sha256="6" * 64,
    )


def _profile_identity(*, stage: str) -> dict[str, Any]:
    return {
        "admission": "provisional_shadow" if stage == "public-shadow" else "accepted",
        "allow_private_text": stage == "private-shadow",
        "context_tokens": 4096,
        "gateway_ca_certificate_sha256": operator._SECONDARY_FINALIST_CA_SHA256,  # noqa: SLF001
        "manifest_sha256": (
            operator._SECONDARY_FINALIST_CANDIDATE_PROFILE_SHA256  # noqa: SLF001
            if stage == "public-shadow"
            else "2" * 64
        ),
        "mode": "shadow",
        "profile_id": operator._SECONDARY_FINALIST_PROFILE_ID,  # noqa: SLF001
        "served_model_alias": operator._SECONDARY_FINALIST_MODEL_ALIAS,  # noqa: SLF001
    }


def _snapshot(*, stage: str) -> dict[str, Any]:
    return {
        "schema": "friday.optional-secondary-health.v1",
        "role": "optional_advisory",
        "enabled": True,
        "configured": True,
        "mode": "shadow",
        "state": "healthy",
        "available": True,
        "last_failure": None,
        "profile_id": operator._SECONDARY_FINALIST_PROFILE_ID,  # noqa: SLF001
        "profile_admission": "provisional_shadow" if stage == "public-shadow" else "accepted",
        "profile_manifest_match": True,
        "served_model_match": True,
        "context_cap_tokens": 4096,
        "selected_total": 10,
        "success_total": 9,
        "endpoint_request_total": 14,
        "endpoint_success_total": 13,
        "skipped_total": 4,
        "primary_fallback_total": 2,
        "probe_success_total": 2,
        "probe_failure_total": 1,
        "model_inventory_probe_success_total": 2,
        "model_inventory_probe_failure_total": 1,
        "circuit_retry_after_sec": 0.0,
        "skip_reasons": {"timeout": 4},
        "fallback_reasons": {"timeout": 2},
        "shadow": {
            "valid_total": 3,
            "invalid_total": 1,
            "skipped_total": 2,
            "in_flight": 0,
        },
        "workload": {
            "name": "extract",
            "selected_total": 10,
            "success_total": 9,
            "skip_reasons": {"timeout": 4},
            "fallback_reasons": {"timeout": 2},
        },
    }


def _receipt(
    tmp_path: Path,
    *,
    stage: str,
    before_available: bool = True,
    after_available: bool = True,
) -> dict[str, Any]:
    before = _snapshot(stage=stage)
    before["available"] = before_available
    after = copy.deepcopy(before)
    after["available"] = after_available
    if stage == "public-shadow":
        after["skipped_total"] += 1
        after["skip_reasons"]["private_text_disallowed"] = 1
        after["workload"]["skip_reasons"]["private_text_disallowed"] = 1
        after["shadow"]["skipped_total"] += 1
        after["last_failure"] = "private_text_disallowed"
    else:
        after["selected_total"] += 1
        after["success_total"] += 1
        after["endpoint_request_total"] += 1
        after["endpoint_success_total"] += 1
        after["workload"]["selected_total"] += 1
        after["workload"]["success_total"] += 1
        after["shadow"]["valid_total"] += 1
    deltas = operator._secondary_product_stage_deltas(stage, before, after)  # noqa: SLF001
    operation = {
        key: "4" * 64
        for key in operator._SECONDARY_PRODUCT_OPERATION_KEYS  # noqa: SLF001
        if key.endswith("_sha256")
    }
    operation.update(
        {
            "schema": operator._SECONDARY_PRODUCT_OPERATION_SCHEMA,  # noqa: SLF001
            "ingest_idempotent_replay": False,
            "advice_endpoint_role": "primary",
            "exact_secondary_model_observed": False,
            "cleanup_status": "purged",
            "knowledge_object_created": False,
            "tool_requested": False,
            "effect_requested": False,
        }
    )
    profile = _profile_identity(stage=stage)
    zero_projection = {
        "schema": operator._SECONDARY_PRODUCT_CLEANUP_ZERO_SCHEMA,  # noqa: SLF001
        "raw_object_id_sha256": operation["raw_object_id_sha256"],
        "inbox_id_sha256": operation["inbox_id_sha256"],
        **{key: 0 for key in operator._SECONDARY_PRODUCT_RESIDUE_KEYS},  # noqa: SLF001
    }
    zero_binding = hashlib.sha256(
        operator._secondary_product_canonical(zero_projection)  # noqa: SLF001
    ).hexdigest()
    cleanup_storage_binding = "9" * 64
    cleanup_core = {
        "schema": operator._SECONDARY_PRODUCT_CLEANUP_CORE_SCHEMA,  # noqa: SLF001
        "purged": True,
        "raw_deleted": 1,
        "inbox_deleted": 1,
        "storage_binding_sha256": cleanup_storage_binding,
        "raw_object_id_sha256": operation["raw_object_id_sha256"],
        "inbox_id_sha256": operation["inbox_id_sha256"],
        "cleanup_zero_residue_binding_sha256": zero_binding,
        **{key: 0 for key in operator._SECONDARY_PRODUCT_RESIDUE_KEYS},  # noqa: SLF001
    }
    operation["cleanup_core_sha256"] = hashlib.sha256(
        operator._secondary_product_canonical(cleanup_core)  # noqa: SLF001
    ).hexdigest()
    stage_diagnostics_binding = hashlib.sha256(
        operator._secondary_product_canonical(  # noqa: SLF001
            {
                "source_ref_sha256": operation["source_ref_sha256"],
                "before": before,
                "after": after,
                "deltas": deltas,
            }
        )
    ).hexdigest()
    operation["stage_diagnostics_binding_sha256"] = stage_diagnostics_binding
    diagnostics_projection = {
        "schema": operator._SECONDARY_PRODUCT_DIAGNOSTICS_SCHEMA,  # noqa: SLF001
        "source_ref_sha256": operation["source_ref_sha256"],
        "before": before,
        "after": after,
    }
    diagnostics_binding = hashlib.sha256(
        operator._secondary_product_canonical(diagnostics_projection)  # noqa: SLF001
    ).hexdigest()
    operation["advice_diagnostics_receipt_sha256"] = hashlib.sha256(
        operator._secondary_product_canonical(  # noqa: SLF001
            {**diagnostics_projection, "binding_sha256": diagnostics_binding}
        )
    ).hexdigest()
    operation_binding = hashlib.sha256(
        operator._secondary_product_canonical(operation)  # noqa: SLF001
    ).hexdigest()
    lookup_token = "a" * 64
    issued_at = int(operator.time.time())
    attestation = {
        "schema": operator._SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_SCHEMA,  # noqa: SLF001
        "attestation_id": "b" * 32,
        "stage": stage,
        "source_ref_sha256": operation["source_ref_sha256"],
        "raw_object_id_sha256": operation["raw_object_id_sha256"],
        "inbox_id_sha256": operation["inbox_id_sha256"],
        "content_sha256": operation["synthetic_content_sha256"],
        "uploader_sha256": operation["uploader_id_sha256"],
        "ingest_storage_binding_sha256": operation["ingest_storage_sha256"],
        "advice_storage_binding_sha256": operation["advice_storage_sha256"],
        "advice_diagnostics_receipt_sha256": operation["advice_diagnostics_receipt_sha256"],
        "diagnostics_binding_sha256": diagnostics_binding,
        "stage_diagnostics_binding_sha256": stage_diagnostics_binding,
        "operation_binding_sha256": operation_binding,
        "advice_proof_sha256": operation["advice_proof_sha256"],
        "advice_endpoint_role": "primary",
        "advice_model_sha256": "c" * 64,
        "primary_pid": 2613,
        "primary_process_epoch_sha256": "7" * 64,
        "primary_backend_version": _previous(tmp_path).version,
        "primary_ca_certificate_sha256": "8" * 64,
        "observer_source_head": _previous(tmp_path).commit,
        "observer_runner_sha256": "6" * 64,
        "candidate_profile_id": profile["profile_id"],
        "candidate_profile_mode": profile["mode"],
        "candidate_profile_allow_private_text": profile["allow_private_text"],
        "candidate_profile_context_tokens": profile["context_tokens"],
        "candidate_profile_sha256": operator._SECONDARY_FINALIST_CANDIDATE_PROFILE_SHA256,  # noqa: SLF001,E501
        "candidate_profile_manifest_sha256": profile["manifest_sha256"],
        "candidate_profile_admission": profile["admission"],
        "served_model_alias": profile["served_model_alias"],
        "gateway_ca_certificate_sha256": profile["gateway_ca_certificate_sha256"],
        "cleanup_storage_binding_sha256": cleanup_storage_binding,
        "cleanup_zero_residue_binding_sha256": zero_binding,
        **{key: 0 for key in operator._SECONDARY_PRODUCT_RESIDUE_KEYS},  # noqa: SLF001
        "lookup_token_sha256": hashlib.sha256(lookup_token.encode("ascii")).hexdigest(),
        "state_version": 1,
        "issued_at": issued_at,
        "expires_at": issued_at + 570,
        "signature": "d" * 64,
    }
    receipt = {
        "schema": operator._SECONDARY_PRODUCT_STAGE_SCHEMA,  # noqa: SLF001
        "status": "passed",
        "stage": stage,
        "candidate_profile_id": profile["profile_id"],
        "candidate_profile_sha256": operator._SECONDARY_FINALIST_CANDIDATE_PROFILE_SHA256,  # noqa: SLF001,E501
        "served_model_alias": profile["served_model_alias"],
        "gateway_ca_certificate_sha256": profile["gateway_ca_certificate_sha256"],
        "observer_source_head": _previous(tmp_path).commit,
        "observer_runner_sha256": "6" * 64,
        "primary_pid": 2613,
        "primary_process_epoch_sha256": "7" * 64,
        "primary_version": _previous(tmp_path).version,
        "primary_ca_certificate_sha256": "8" * 64,
        "diagnostics_before": before,
        "diagnostics_after": after,
        "diagnostics_deltas": deltas,
        "diagnostics_binding_sha256": diagnostics_binding,
        "stage_diagnostics_binding_sha256": stage_diagnostics_binding,
        "operation": operation,
        "operation_binding_sha256": operation_binding,
        "server_rollout_attestation": attestation,
        "server_rollout_attestation_sha256": hashlib.sha256(
            operator._secondary_product_canonical(attestation)  # noqa: SLF001
        ).hexdigest(),
        "server_rollout_lookup_token": lookup_token,
        "rollout_lookup_token_retained": True,
        "raw_content_retained_in_evidence": False,
        "model_response_retained_in_evidence": False,
        "credentials_retained": False,
    }
    return receipt


def _validate(tmp_path: Path, receipt: dict[str, Any], *, stage: str) -> None:
    operator._validate_secondary_rollout_receipt(  # noqa: SLF001
        receipt,
        expected_stage=stage,
        previous=_previous(tmp_path),
        observer_runner_sha256="6" * 64,
        profile_identity=_profile_identity(stage=stage),
        primary_pid=2613,
        primary_process_epoch_sha256="7" * 64,
        primary_ca_certificate_sha256="8" * 64,
    )


def test_gate_contract_matches_the_automatic_product_runner() -> None:
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(RUNTIME))
    try:
        live = importlib.import_module("live_failure_battery")
    finally:
        sys.path.remove(str(RUNTIME))
        sys.path.remove(str(SCRIPTS))
    assert operator._SECONDARY_PRODUCT_STAGE_SCHEMA == live.PRODUCT_STAGE_SCHEMA  # noqa: SLF001
    assert operator._SECONDARY_PRODUCT_RECEIPT_KEYS == live.PRODUCT_STAGE_KEYS  # noqa: SLF001
    assert operator._SECONDARY_PRODUCT_OPERATION_KEYS == live._PRODUCT_OPERATION_KEYS  # noqa: SLF001
    assert operator._SECONDARY_PRODUCT_DELTA_KEYS == live._PRODUCT_STAGE_DELTA_KEYS  # noqa: SLF001
    assert operator._SECONDARY_PRODUCT_SNAPSHOT_KEYS == live._PRODUCT_SNAPSHOT_KEYS  # noqa: SLF001
    assert operator._SECONDARY_PRODUCT_SHADOW_KEYS == live._PRODUCT_SHADOW_KEYS  # noqa: SLF001
    assert operator._SECONDARY_PRODUCT_WORKLOAD_KEYS == live._PRODUCT_WORKLOAD_KEYS  # noqa: SLF001
    assert operator._SECONDARY_PRODUCT_FAILURES == live._SECONDARY_FAILURES  # noqa: SLF001
    assert (  # noqa: SLF001
        operator._SECONDARY_PRODUCT_OPERATION_SCHEMA == witness.SECONDARY_PRODUCT_OPERATION_CORE_SCHEMA
    )
    assert operator._SECONDARY_PRODUCT_OPERATION_KEYS == witness.SECONDARY_PRODUCT_OPERATION_CORE_KEYS  # noqa: SLF001,E501
    assert operator._SECONDARY_PRODUCT_CLEANUP_ZERO_SCHEMA == witness.SECONDARY_PRODUCT_ZERO_RESIDUE_SCHEMA  # noqa: SLF001,E501
    assert operator._SECONDARY_PRODUCT_CLEANUP_ZERO_KEYS == witness.SECONDARY_PRODUCT_ZERO_RESIDUE_KEYS  # noqa: SLF001,E501
    assert operator._SECONDARY_PRODUCT_CLEANUP_CORE_SCHEMA == witness.SECONDARY_PRODUCT_CLEANUP_CORE_SCHEMA  # noqa: SLF001,E501
    assert operator._SECONDARY_PRODUCT_CLEANUP_CORE_KEYS == witness.SECONDARY_PRODUCT_CLEANUP_CORE_KEYS  # noqa: SLF001,E501
    assert (
        operator._SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_SCHEMA
        == witness.SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_SCHEMA
    )  # noqa: SLF001,E501
    assert operator._SECONDARY_PRODUCT_ATTESTATION_KEYS == witness.SECONDARY_PRODUCT_ROLLOUT_ATTESTATION_KEYS  # noqa: SLF001,E501
    assert (
        operator._SECONDARY_PRODUCT_CONSUME_REQUEST_SCHEMA == witness.SECONDARY_PRODUCT_CONSUME_REQUEST_SCHEMA
    )  # noqa: SLF001,E501
    assert operator._SECONDARY_PRODUCT_CONSUME_REQUEST_KEYS == witness.SECONDARY_PRODUCT_CONSUME_REQUEST_KEYS  # noqa: SLF001,E501
    assert (
        operator._SECONDARY_PRODUCT_CONSUME_RESPONSE_SCHEMA
        == witness.SECONDARY_PRODUCT_CONSUME_RESPONSE_SCHEMA
    )  # noqa: SLF001,E501
    assert (
        operator._SECONDARY_PRODUCT_CONSUME_RESPONSE_KEYS == witness.SECONDARY_PRODUCT_CONSUME_RESPONSE_KEYS
    )  # noqa: SLF001,E501


@pytest.mark.parametrize("stage", ["public-shadow", "private-shadow"])
def test_exact_automatic_predecessor_receipt_is_accepted(tmp_path: Path, stage: str) -> None:
    _validate(tmp_path, _receipt(tmp_path, stage=stage), stage=stage)


def test_private_shadow_accepts_stale_healthy_before_and_requires_fresh_after(
    tmp_path: Path,
) -> None:
    _validate(
        tmp_path,
        _receipt(tmp_path, stage="private-shadow", before_available=False),
        stage="private-shadow",
    )

    stale_after = _receipt(
        tmp_path,
        stage="private-shadow",
        after_available=False,
    )
    with pytest.raises(operator.ReleaseFailure, match="secondary_rollout_receipt_invalid"):
        _validate(tmp_path, stale_after, stage="private-shadow")


def test_stale_private_before_must_still_be_healthy_and_public_remains_fresh(
    tmp_path: Path,
) -> None:
    outage_before = _receipt(
        tmp_path,
        stage="private-shadow",
        before_available=False,
    )
    outage_before["diagnostics_before"]["state"] = "degraded"
    with pytest.raises(operator.ReleaseFailure, match="secondary_rollout_receipt_invalid"):
        _validate(tmp_path, outage_before, stage="private-shadow")

    stale_public = _receipt(
        tmp_path,
        stage="public-shadow",
        before_available=False,
    )
    with pytest.raises(operator.ReleaseFailure, match="secondary_rollout_receipt_invalid"):
        _validate(tmp_path, stale_public, stage="public-shadow")


@pytest.mark.parametrize(
    ("issued_offset", "expires_offset"),
    [(-1_000, -500), (31, 100), (0, 571)],
)
def test_stale_future_or_overlong_server_attestation_is_rejected(
    tmp_path: Path,
    issued_offset: int,
    expires_offset: int,
) -> None:
    receipt = _receipt(tmp_path, stage="public-shadow")
    now = int(operator.time.time())
    attestation = receipt["server_rollout_attestation"]
    attestation["issued_at"] = now + issued_offset
    attestation["expires_at"] = now + expires_offset
    receipt["server_rollout_attestation_sha256"] = hashlib.sha256(
        operator._secondary_product_canonical(attestation)  # noqa: SLF001
    ).hexdigest()
    with pytest.raises(operator.ReleaseFailure, match="attestation_invalid"):
        _validate(tmp_path, receipt, stage="public-shadow")


def test_consume_response_requires_every_safe_request_echo_and_consumed_state(
    tmp_path: Path,
) -> None:
    receipt = _receipt(tmp_path, stage="public-shadow")
    attestation = operator._validate_secondary_rollout_receipt(  # noqa: SLF001
        receipt,
        expected_stage="public-shadow",
        previous=_previous(tmp_path),
        observer_runner_sha256="6" * 64,
        profile_identity=_profile_identity(stage="public-shadow"),
        primary_pid=2613,
        primary_process_epoch_sha256="7" * 64,
        primary_ca_certificate_sha256="8" * 64,
    )
    request = operator._secondary_rollout_consume_request(  # noqa: SLF001
        lookup_token=receipt["server_rollout_lookup_token"],
        stage="public-shadow",
        transition="secondary_shadow_to_private_shadow",
        previous=_previous(tmp_path),
        candidate=_candidate(tmp_path),
        next_env_sha256="3" * 64,
        product_receipt_sha256="4" * 64,
        sealed_runner_sha256="6" * 64,
        server_rollout_attestation_sha256=receipt["server_rollout_attestation_sha256"],
    )
    response = {
        "schema": operator._SECONDARY_PRODUCT_CONSUME_RESPONSE_SCHEMA,  # noqa: SLF001
        "status": "consumed",
        **{
            key: request[key]
            for key in (
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
        "request_sha256": hashlib.sha256(
            operator._secondary_product_canonical(request)  # noqa: SLF001
        ).hexdigest(),
        "consumed_at": attestation["issued_at"],
        "state_version": 2,
        "consume_binding_sha256": "e" * 64,
    }
    operator._validate_secondary_rollout_consume_response(  # noqa: SLF001
        response,
        request=request,
        attestation=attestation,
    )
    for field, replacement in (
        ("predecessor_tree_sha256", "f" * 64),
        ("candidate_commit", "f" * 40),
        ("candidate_tree_sha256", "f" * 64),
        ("product_receipt_sha256", "f" * 64),
        ("sealed_runner_sha256", "f" * 64),
        ("server_rollout_attestation_sha256", "f" * 64),
        ("next_env_sha256", "f" * 64),
        ("state_version", 1),
    ):
        tampered = {**response, field: replacement}
        with pytest.raises(operator.ReleaseFailure, match="consume_response_invalid"):
            operator._validate_secondary_rollout_consume_response(  # noqa: SLF001
                tampered,
                request=request,
                attestation=attestation,
            )

    mismatched_request = {**request, "server_rollout_attestation_sha256": "f" * 64}
    mismatched_response = {
        **response,
        "server_rollout_attestation_sha256": "f" * 64,
        "request_sha256": hashlib.sha256(
            operator._secondary_product_canonical(mismatched_request)  # noqa: SLF001
        ).hexdigest(),
    }
    with pytest.raises(operator.ReleaseFailure, match="consume_response_invalid"):
        operator._validate_secondary_rollout_consume_response(  # noqa: SLF001
            mismatched_response,
            request=mismatched_request,
            attestation=attestation,
        )


def test_owner_token_must_be_one_literal_private_environment_value() -> None:
    token = "owner_" + "a" * 58
    for key in ("FRIDAY_API_TOKEN", "JERICHO_API_TOKEN"):
        assert (
            operator._secondary_rollout_api_token(  # noqa: SLF001
                f"{key}={token}\n".encode()
            )
            == token
        )
    for raw in (
        f"FRIDAY_API_TOKEN={token}\nFRIDAY_API_TOKEN={token}\n",
        f"JERICHO_API_TOKEN={token}\nJERICHO_API_TOKEN={token}\n",
        f"FRIDAY_API_TOKEN={token}\nJERICHO_API_TOKEN={token}\n",
        f"export FRIDAY_API_TOKEN={token}\n",
        f"export JERICHO_API_TOKEN={token}\n",
        f"FRIDAY_API_TOKEN='{token}'\n",
        f" JERICHO_API_TOKEN={token}\n",
        f"JERICHO_API_TOKEN = {token}\n",
        "JERICHO_API_TOKEN=short\n",
        f"FRIDAY_API_TOKEN={token}\nexport JERICHO_API_TOKEN={token}\n",
    ):
        with pytest.raises(operator.ReleaseFailure, match="api_token_invalid"):
            operator._secondary_rollout_api_token(raw.encode())  # noqa: SLF001


def _config(
    *,
    transition: str,
    receipt: Path | None = None,
    receipt_sha256: str = "",
) -> operator.SystemdConfig:
    return operator.SystemdConfig(
        anchor=Path("/runtime/current"),
        env_file=Path("/private/friday.env"),
        env_file_sha256="1" * 64,
        friday_home=Path("/private/friday"),
        unit_dir=Path("/units"),
        database=Path("/private/friday.sqlite3"),
        inbox_database=Path("/private/telegram-inbox.sqlite3"),
        backup_dir=Path("/private/backups"),
        state_dir=Path("/private"),
        health_ca=Path("/private/primary-ca.pem"),
        health_ca_sha256="8" * 64,
        next_env_file=Path("/private/next.env"),
        next_env_file_sha256="3" * 64,
        staged_config_transition=transition,
        secondary_rollout_receipt=receipt,
        secondary_rollout_receipt_sha256=receipt_sha256,
    )


@pytest.mark.parametrize(
    ("transition", "stage"),
    [
        ("secondary_shadow_to_private_shadow", "public-shadow"),
        ("secondary_shadow_to_assist", "private-shadow"),
    ],
)
def test_promotion_requires_the_exact_predecessor_stage_receipt(
    transition: str,
    stage: str,
) -> None:
    with pytest.raises(operator.ReleaseFailure, match="secondary_rollout_receipt_required"):
        operator._secondary_rollout_receipt_stage(_config(transition=transition))  # noqa: SLF001
    assert (
        operator._secondary_rollout_receipt_stage(  # noqa: SLF001
            _config(
                transition=transition,
                receipt=Path("/private/product.json"),
                receipt_sha256="4" * 64,
            )
        )
        == stage
    )


def test_receipt_is_forbidden_on_nonpromotion_transition() -> None:
    with pytest.raises(operator.ReleaseFailure, match="secondary_rollout_receipt_not_permitted"):
        operator._secondary_rollout_receipt_stage(  # noqa: SLF001
            _config(
                transition="secondary_shadow_disable",
                receipt=Path("/private/product.json"),
                receipt_sha256="4" * 64,
            )
        )


def test_receipt_loader_rejects_digest_canonicalization_and_duplicate_key_tampering(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "product.public-shadow.json"
    receipt = _receipt(tmp_path, stage="public-shadow")
    raw = operator._secondary_product_canonical(receipt)  # noqa: SLF001
    path.write_bytes(raw)
    path.chmod(0o600)
    assert (
        operator._load_secondary_rollout_receipt(  # noqa: SLF001
            path, hashlib.sha256(raw).hexdigest()
        )
        == receipt
    )

    with pytest.raises(operator.ReleaseFailure, match="digest_mismatch"):
        operator._load_secondary_rollout_receipt(path, "0" * 64)  # noqa: SLF001

    path.write_text('{"schema":"x","schema":"x"}\n', encoding="ascii")
    duplicate = path.read_bytes()
    with pytest.raises(operator.ReleaseFailure, match="receipt_invalid"):
        operator._load_secondary_rollout_receipt(  # noqa: SLF001
            path, hashlib.sha256(duplicate).hexdigest()
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("observer_source_head", "9" * 40),
        ("primary_version", "0.207.9"),
        ("candidate_profile_sha256", "9" * 64),
        ("primary_pid", 2614),
        ("primary_process_epoch_sha256", "9" * 64),
        ("primary_ca_certificate_sha256", "9" * 64),
    ],
)
def test_receipt_rejects_mixed_source_release_profile_process_or_config_identity(
    tmp_path: Path,
    field: str,
    replacement: Any,
) -> None:
    receipt = _receipt(tmp_path, stage="public-shadow")
    receipt[field] = replacement
    with pytest.raises(operator.ReleaseFailure, match="identity_mismatch"):
        _validate(tmp_path, receipt, stage="public-shadow")


def test_receipt_rejects_rebound_counter_sidecar_and_effect_authority(tmp_path: Path) -> None:
    receipt = _receipt(tmp_path, stage="private-shadow")
    receipt["diagnostics_deltas"]["success_total"] = 0
    receipt["diagnostics_binding_sha256"] = hashlib.sha256(
        operator._secondary_product_canonical(  # noqa: SLF001
            {
                "source_ref_sha256": receipt["operation"]["source_ref_sha256"],
                "before": receipt["diagnostics_before"],
                "after": receipt["diagnostics_after"],
                "deltas": receipt["diagnostics_deltas"],
            }
        )
    ).hexdigest()
    with pytest.raises(operator.ReleaseFailure, match="diagnostics_mismatch"):
        _validate(tmp_path, receipt, stage="private-shadow")

    receipt = _receipt(tmp_path, stage="private-shadow")
    receipt["operation"]["effect_requested"] = True
    receipt["operation_binding_sha256"] = hashlib.sha256(
        operator._secondary_product_canonical(receipt["operation"])  # noqa: SLF001
    ).hexdigest()
    with pytest.raises(operator.ReleaseFailure, match="operation_invalid"):
        _validate(tmp_path, receipt, stage="private-shadow")


def _secondary_environment(ca_file: Path, *, private: bool) -> bytes:
    values = {
        **operator._SECONDARY_SHADOW_EXACT_VALUES,  # noqa: SLF001
        "FRIDAY_SECONDARY_LLM_API_KEY": "a" * 64,
        "FRIDAY_SECONDARY_LLM_CA_FILE": str(ca_file),
    }
    if private:
        values["FRIDAY_SECONDARY_LLM_ALLOW_PRIVATE_TEXT"] = "1"
    return (
        b"JERICHO_API_TOKEN=owner_"
        + b"a" * 58
        + b"\n"
        + b"".join(f"{key}={value}\n".encode("ascii") for key, value in sorted(values.items()))
    )


def test_live_gate_binds_current_env_ca_profile_and_process_before_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o700)
    secondary_ca = tmp_path / "secondary-ca.pem"
    secondary_ca.write_bytes(b"secondary-ca\n")
    secondary_ca.chmod(0o600)
    secondary_ca_sha256 = hashlib.sha256(secondary_ca.read_bytes()).hexdigest()
    monkeypatch.setattr(operator, "_SECONDARY_FINALIST_CA_SHA256", secondary_ca_sha256)
    primary_ca = tmp_path / "primary-ca.pem"
    primary_ca.write_bytes(b"primary-ca\n")
    primary_ca.chmod(0o600)
    primary_ca_sha256 = hashlib.sha256(primary_ca.read_bytes()).hexdigest()
    environment = _secondary_environment(secondary_ca, private=False)
    env_file = tmp_path / "friday.env"
    env_file.write_bytes(environment)
    env_file.chmod(0o600)
    receipt = _receipt(tmp_path, stage="public-shadow")
    previous = _previous(tmp_path)
    runner = previous.root / operator._SECONDARY_PRODUCT_RUNNER_ARTIFACT  # noqa: SLF001
    runner.parent.mkdir(parents=True, mode=0o700)
    runner.write_bytes(b"synthetic sealed product witness runner\n")
    runner.chmod(0o400)
    runner_sha256 = hashlib.sha256(runner.read_bytes()).hexdigest()
    previous = replace(previous, secondary_product_runner_sha256=runner_sha256)
    receipt["observer_runner_sha256"] = runner_sha256
    receipt["server_rollout_attestation"]["observer_runner_sha256"] = runner_sha256
    receipt["gateway_ca_certificate_sha256"] = secondary_ca_sha256
    receipt["server_rollout_attestation"]["gateway_ca_certificate_sha256"] = secondary_ca_sha256
    receipt["primary_ca_certificate_sha256"] = primary_ca_sha256
    receipt["server_rollout_attestation"]["primary_ca_certificate_sha256"] = primary_ca_sha256
    receipt["server_rollout_attestation_sha256"] = hashlib.sha256(
        operator._secondary_product_canonical(  # noqa: SLF001
            receipt["server_rollout_attestation"]
        )
    ).hexdigest()
    raw = operator._secondary_product_canonical(receipt)  # noqa: SLF001
    receipt_path = tmp_path / "product.public-shadow.json"
    receipt_path.write_bytes(raw)
    receipt_path.chmod(0o600)
    next_environment = _secondary_environment(secondary_ca, private=True)
    next_env_file = tmp_path / "next.env"
    next_env_file.write_bytes(next_environment)
    next_env_file.chmod(0o600)
    config = operator.SystemdConfig(
        anchor=tmp_path / "current",
        env_file=env_file,
        env_file_sha256=hashlib.sha256(environment).hexdigest(),
        friday_home=tmp_path,
        unit_dir=tmp_path,
        database=tmp_path / "friday.sqlite3",
        inbox_database=tmp_path / "telegram-inbox.sqlite3",
        backup_dir=tmp_path,
        state_dir=tmp_path,
        health_ca=primary_ca,
        health_ca_sha256=primary_ca_sha256,
        next_env_file=next_env_file,
        next_env_file_sha256=hashlib.sha256(next_environment).hexdigest(),
        staged_config_transition="secondary_shadow_to_private_shadow",
        secondary_rollout_receipt=receipt_path,
        secondary_rollout_receipt_sha256=hashlib.sha256(raw).hexdigest(),
    )
    port = object.__new__(operator.SystemdActivationPort)
    port.config = config
    port._secondary_rollout_profile_identity = lambda *_args, **_kwargs: {  # type: ignore[method-assign]  # noqa: SLF001,E501
        **_profile_identity(stage="public-shadow"),
        "gateway_ca_certificate_sha256": secondary_ca_sha256,
    }
    port._current_backend_process_identity = lambda *_args, **_kwargs: (2613, "7" * 64)  # type: ignore[method-assign]  # noqa: SLF001,E501
    consumed: list[dict[str, Any]] = []

    def consume(
        request: dict[str, Any],
        *,
        attestation: dict[str, Any],
        api_token: str,
        primary_ca: bytes,
    ) -> None:
        if consumed:
            raise operator.ReleaseFailure("secondary_rollout_attestation_consume_failed")
        assert api_token == "owner_" + "a" * 58
        assert primary_ca == b"primary-ca\n"
        consumed.append(dict(request))
        response = {
            "schema": operator._SECONDARY_PRODUCT_CONSUME_RESPONSE_SCHEMA,  # noqa: SLF001
            "status": "consumed",
            **{
                key: request[key]
                for key in (
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
            "request_sha256": hashlib.sha256(
                operator._secondary_product_canonical(request)  # noqa: SLF001
            ).hexdigest(),
            "consumed_at": attestation["issued_at"],
            "state_version": 2,
            "consume_binding_sha256": "e" * 64,
        }
        operator._validate_secondary_rollout_consume_response(  # noqa: SLF001
            response,
            request=request,
            attestation=attestation,
        )

    port._consume_secondary_rollout_attestation = consume  # type: ignore[method-assign]  # noqa: SLF001,E501

    candidate = _candidate(tmp_path)
    port._validate_secondary_rollout_gate(previous, candidate)  # noqa: SLF001
    assert len(consumed) == 1
    assert consumed[0]["server_rollout_attestation_sha256"] == receipt["server_rollout_attestation_sha256"]
    assert consumed[0]["candidate_commit"] == candidate.commit
    assert consumed[0]["candidate_tree_sha256"] == candidate.tree_manifest_sha256
    with pytest.raises(operator.ReleaseFailure, match="attestation_consume_failed"):
        port._validate_secondary_rollout_gate(previous, candidate)  # noqa: SLF001

    primary_ca.unlink()
    primary_ca.symlink_to(secondary_ca)
    with pytest.raises(operator.ReleaseFailure, match="health_ca_invalid"):
        port._validate_secondary_rollout_gate(previous, candidate)  # noqa: SLF001


def test_receipt_runner_must_match_the_exact_sealed_predecessor_artifact(
    tmp_path: Path,
) -> None:
    previous = _previous(tmp_path)
    runner = previous.root / operator._SECONDARY_PRODUCT_RUNNER_ARTIFACT  # noqa: SLF001
    runner.parent.mkdir(parents=True, mode=0o700)
    runner.write_bytes(b"trusted product witness runner\n")
    runner.chmod(0o400)
    with pytest.raises(operator.ReleaseFailure, match="runner_capability_missing"):
        operator._secondary_product_runner_artifact_sha256(  # noqa: SLF001
            replace(previous, secondary_product_runner_sha256="")
        )
    previous = replace(
        previous,
        secondary_product_runner_sha256=hashlib.sha256(runner.read_bytes()).hexdigest(),
    )
    trusted_sha256 = operator._secondary_product_runner_artifact_sha256(previous)  # noqa: SLF001
    receipt = _receipt(tmp_path, stage="public-shadow")
    assert receipt["observer_runner_sha256"] != trusted_sha256
    with pytest.raises(operator.ReleaseFailure, match="identity_mismatch"):
        operator._validate_secondary_rollout_receipt(  # noqa: SLF001
            receipt,
            expected_stage="public-shadow",
            previous=previous,
            observer_runner_sha256=trusted_sha256,
            profile_identity=_profile_identity(stage="public-shadow"),
            primary_pid=2613,
            primary_process_epoch_sha256="7" * 64,
            primary_ca_certificate_sha256="8" * 64,
        )

    runner.chmod(0o600)
    with pytest.raises(operator.ReleaseFailure, match="runner_artifact_invalid"):
        operator._secondary_product_runner_artifact_sha256(previous)  # noqa: SLF001


def test_process_epoch_attestation_rejects_systemd_pid_change(
    tmp_path: Path,
) -> None:
    pid = 2613
    proc_root = tmp_path / "proc"
    process_root = proc_root / str(pid)
    process_root.mkdir(parents=True)
    fields = ["S", *("0" for _ in range(18)), "424242", "0"]
    (process_root / "stat").write_text(f"{pid} (friday) {' '.join(fields)}\n", encoding="ascii")
    port = object.__new__(operator.SystemdActivationPort)
    observed = iter((pid, pid + 1))
    port.config = SimpleNamespace(backend_unit="friday-backend.service")
    port._systemctl = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]  # noqa: SLF001,E501
        returncode=0,
        stdout=f"{next(observed)}\n".encode("ascii"),
    )
    port._process_matches = lambda *_args, **_kwargs: True  # type: ignore[method-assign]  # noqa: SLF001,E501
    with pytest.raises(operator.ReleaseFailure, match="process_identity_changed"):
        port._current_backend_process_identity(_previous(tmp_path), proc_root=proc_root)  # noqa: SLF001
