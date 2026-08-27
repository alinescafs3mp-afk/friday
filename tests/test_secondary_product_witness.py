from __future__ import annotations

import copy
import hashlib
import importlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from friday import __version__
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.storage.models import InboxStatus

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "deploy" / "secondary-brain" / "windows-sglang" / "scripts"
RUNTIME = ROOT / "deploy" / "secondary-brain" / "windows-sglang" / "runtime"
IDENTITY = {
    "candidate_profile_id": "gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f",
    "candidate_profile_sha256": "51af2164fa07ff3c01813e318076f7ac8b37eeecb73e695b6ca7543061c93439",
    "served_model_alias": "friday-secondary-gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f",
    "gateway_ca_certificate_sha256": "392756a74fd9100635c42f4fbf7e5a5f1822d18ea898ebb7848b9fdd0bddc1fe",
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _product_tables(storage: Any) -> dict[str, int]:
    return {
        table: int(storage.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])
        for table in (
            "raw_objects",
            "inbox",
            "knowledge_objects",
            "file_source_aliases",
            "feedback",
            "feedback_state",
        )
    }


class _NoPrimaryAdvice:
    enabled = True
    model = "primary-must-not-run"

    async def chat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("accepted assist witness must use the actual secondary scheduler")


class _PrimaryAdvice:
    enabled = True
    model = "primary-test-model"

    async def chat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "content": json.dumps(
                {
                    "title": "Project Atlas storage",
                    "summary": "Project Atlas uses PostgreSQL 16.",
                    "knowledge_kind": "technical_note",
                    "importance": 0.5,
                    "tags": ["atlas"],
                    "entities": [],
                    "recommended_action": "review",
                    "confidence": 0.8,
                    "rationale": "PRIMARY_MODEL_BODY_SENTINEL",
                }
            )
        }


class _AcceptedSecondaryClient:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            served_model_alias=IDENTITY["served_model_alias"],
            max_context_tokens=4096,
            max_output_tokens=512,
            call_budget_sec=2.0,
            health_interval_sec=60.0,
            admission_timeout_sec=0.1,
            api_key="e" * 64,
            profile_id=IDENTITY["candidate_profile_id"],
            profile_manifest_sha256=IDENTITY["candidate_profile_sha256"],
            ca_sha256=IDENTITY["gateway_ca_certificate_sha256"],
        )
        self.selected_total = 0
        self.success_total = 0
        self.fallback_total = 0

    def validate_request(self, _request: Any) -> None:
        return None

    async def call(self, _request: Any) -> Any:
        from friday.secondary_brain import SecondaryAttempt, SecondaryResult
        from friday.secondary_brain.contracts import ModelUsage

        self.selected_total += 1
        self.success_total += 1
        payload = {
            "title": "Project Atlas storage",
            "summary": "Project Atlas uses PostgreSQL 16.",
            "knowledge_kind": "technical_note",
            "importance": 0.5,
            "tags": ["atlas"],
            "entities": [],
            "recommended_action": "review",
            "confidence": 0.8,
            "rationale": "SECONDARY_MODEL_BODY_SENTINEL",
        }
        return SecondaryAttempt.success(
            SecondaryResult(
                visible_content=json.dumps(payload, ensure_ascii=False),
                structured_output=payload,
                served_model_alias=IDENTITY["served_model_alias"],
                usage=ModelUsage(100, 50, 150),
                latency_sec=0.01,
            )
        )

    def status(self) -> Any:
        from friday.secondary_brain import SecondaryState, SecondaryStatus

        return SecondaryStatus(
            state=SecondaryState.HEALTHY,
            last_failure=None,
            selected_total=self.selected_total,
            success_total=self.success_total,
            skipped_total=0,
            fallback_total=self.fallback_total,
            active_requests=0,
            context_cap_tokens=4096,
            served_model_match=True,
            profile_manifest_match=True,
            endpoint_request_total=self.selected_total,
            endpoint_success_total=self.success_total,
        )

    def protocol_rejection_counts(self) -> dict[Any, int]:
        return {}

    def record_fallback(self) -> None:
        self.fallback_total += 1

    async def invalidate(self, _failure: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _accepted_secondary_scheduler() -> Any:
    from friday.secondary_brain import ModelWorkload, SecondaryMode, SecondaryState
    from friday.secondary_brain.profiles import SecondaryProfileAdmission
    from friday.secondary_brain.scheduler import SecondaryBrainScheduler

    scheduler = SecondaryBrainScheduler(
        mode=SecondaryMode.ASSIST,
        allowed_workloads=frozenset({ModelWorkload.EXTRACT}),
        allow_private_text=True,
        client=_AcceptedSecondaryClient(),
        unavailable_state=SecondaryState.PROBING,
        profile_admission=SecondaryProfileAdmission.ACCEPTED,
    )
    scheduler._epoch_admitted = True
    scheduler._last_probe_success_monotonic = time.monotonic()
    return scheduler


def _attach_accepted_secondary(app: Any) -> Any:
    scheduler = _accepted_secondary_scheduler()
    app.state.secondary_brain = scheduler
    app.state.ingestion.secondary_brain = scheduler
    app.state.llm = _NoPrimaryAdvice()
    return scheduler


def _attach_private_shadow_secondary(app: Any) -> Any:
    from friday.secondary_brain import ModelWorkload, SecondaryMode, SecondaryState
    from friday.secondary_brain.profiles import SecondaryProfileAdmission
    from friday.secondary_brain.scheduler import SecondaryBrainScheduler

    scheduler = SecondaryBrainScheduler(
        mode=SecondaryMode.SHADOW,
        allowed_workloads=frozenset({ModelWorkload.EXTRACT}),
        allow_private_text=True,
        client=_AcceptedSecondaryClient(),
        unavailable_state=SecondaryState.PROBING,
        profile_admission=SecondaryProfileAdmission.ACCEPTED,
    )
    scheduler._epoch_admitted = True
    scheduler._last_probe_success_monotonic = time.monotonic()
    app.state.secondary_brain = scheduler
    app.state.ingestion.secondary_brain = scheduler
    app.state.llm = _PrimaryAdvice()
    return scheduler


@pytest.fixture(scope="module")
def live() -> Any:
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(RUNTIME))
    try:
        yield importlib.import_module("live_failure_battery")
    finally:
        sys.path.remove(str(RUNTIME))
        sys.path.remove(str(SCRIPTS))


@pytest.fixture(autouse=True)
def _product_identity(live: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "evidence_identity", lambda: dict(IDENTITY))
    monkeypatch.setattr(live, "configured_profile_context_tokens", lambda: 4096)


def _snapshot(
    *,
    mode: str,
    admission: str = "accepted",
    state: str = "healthy",
    available: bool = True,
) -> dict[str, Any]:
    return {
        "schema": "friday.optional-secondary-health.v1",
        "role": "optional_advisory",
        "enabled": True,
        "configured": True,
        "mode": mode,
        "state": state,
        "available": available,
        "last_failure": None,
        "profile_id": IDENTITY["candidate_profile_id"],
        "profile_admission": admission,
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


def _stage_pair(stage: str) -> tuple[dict[str, Any], dict[str, Any]]:
    mode = "shadow" if stage in {"public-shadow", "private-shadow"} else "assist"
    admission = "provisional_shadow" if stage == "public-shadow" else "accepted"
    if stage in {"cooldown", "recovery"}:
        before = _snapshot(mode=mode, admission=admission, state="cooldown", available=False)
    else:
        before = _snapshot(mode=mode, admission=admission)
    after = copy.deepcopy(before)
    if stage == "public-shadow":
        after["skipped_total"] += 1
        after["skip_reasons"]["private_text_disallowed"] = 1
        after["workload"]["skip_reasons"]["private_text_disallowed"] = 1
        after["shadow"]["skipped_total"] += 1
        after["last_failure"] = "private_text_disallowed"
    elif stage in {"private-shadow", "assist"}:
        after["selected_total"] += 1
        after["success_total"] += 1
        after["endpoint_request_total"] += 1
        after["endpoint_success_total"] += 1
        after["workload"]["selected_total"] += 1
        after["workload"]["success_total"] += 1
        if stage == "private-shadow":
            after["shadow"]["valid_total"] += 1
    elif stage == "outage":
        after.update(state="cooldown", available=False, last_failure="connect_failed")
        after["selected_total"] += 1
        after["endpoint_request_total"] += 1
        after["skipped_total"] += 1
        after["primary_fallback_total"] += 1
        after["workload"]["selected_total"] += 1
        for target in (
            after["skip_reasons"],
            after["fallback_reasons"],
            after["workload"]["skip_reasons"],
            after["workload"]["fallback_reasons"],
        ):
            target["connect_failed"] = 1
    elif stage == "cooldown":
        after["last_failure"] = "cooldown"
        after["skipped_total"] += 2
        after["primary_fallback_total"] += 1
        after["probe_failure_total"] += 1
        after["model_inventory_probe_failure_total"] += 1
        for target in (
            after["skip_reasons"],
            after["fallback_reasons"],
            after["workload"]["skip_reasons"],
            after["workload"]["fallback_reasons"],
        ):
            target["cooldown"] = 1
    else:
        after.update(state="healthy", available=True, last_failure=None)
        after["selected_total"] += 1
        after["success_total"] += 1
        after["endpoint_request_total"] += 4
        after["endpoint_success_total"] += 4
        after["probe_success_total"] += 1
        after["model_inventory_probe_success_total"] += 1
        after["workload"]["selected_total"] += 1
        after["workload"]["success_total"] += 1
    return before, after


def _raw_secondary_diagnostics(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            key: snapshot[key]
            for key in (
                "schema",
                "role",
                "enabled",
                "configured",
                "mode",
                "state",
                "available",
                "last_failure",
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
            )
        },
        "profile": snapshot["profile_id"],
        "profile_admission": snapshot["profile_admission"],
        "profile_manifest_match": snapshot["profile_manifest_match"],
        "served_model_match": snapshot["served_model_match"],
        "workloads": {
            "extract": {key: value for key, value in snapshot["workload"].items() if key != "name"}
        },
    }


@pytest.mark.parametrize(
    "stage",
    ["public-shadow", "private-shadow", "assist", "outage", "cooldown", "recovery"],
)
def test_product_stage_oracles_require_exact_diagnostics(live: Any, stage: str) -> None:
    before, after = _stage_pair(stage)
    deltas = live._product_stage_deltas(stage, before, after)
    assert set(deltas) == live._PRODUCT_STAGE_DELTA_KEYS

    mismatched = copy.deepcopy(after)
    mismatched["primary_fallback_total"] += 1
    with pytest.raises(live.LiveFailureBatteryError, match="diagnostics do not match"):
        live._product_stage_deltas(stage, before, mismatched)


def test_recovery_oracle_requires_the_four_physical_endpoint_requests(live: Any) -> None:
    before, after = _stage_pair("recovery")

    deltas = live._product_stage_deltas("recovery", before, after)

    assert deltas["endpoint_request_total"] == 4
    assert deltas["endpoint_success_total"] == 4
    assert deltas["selected_total"] == 1
    assert deltas["probe_success_total"] == 1

    legacy_logical_counts = copy.deepcopy(after)
    legacy_logical_counts["endpoint_request_total"] -= 1
    legacy_logical_counts["endpoint_success_total"] -= 1
    with pytest.raises(live.LiveFailureBatteryError, match="diagnostics do not match"):
        live._product_stage_deltas("recovery", before, legacy_logical_counts)


def test_public_shadow_accepts_stale_healthy_admission_on_both_snapshots(live: Any) -> None:
    before, after = _stage_pair("public-shadow")
    before["available"] = False
    after["available"] = False

    deltas = live._product_stage_deltas("public-shadow", before, after)

    assert deltas["endpoint_request_total"] == 0
    assert deltas["probe_success_total"] == 0
    assert deltas["workload_skip_reason_deltas"] == {"private_text_disallowed": 1}


@pytest.mark.parametrize("stage", ["private-shadow", "assist"])
def test_private_product_stage_readmits_one_stale_healthy_process(live: Any, stage: str) -> None:
    before, after = _stage_pair(stage)
    before["available"] = False
    after["endpoint_request_total"] += 2
    after["endpoint_success_total"] += 2
    after["probe_success_total"] += 1
    after["model_inventory_probe_success_total"] += 1

    deltas = live._product_stage_deltas(stage, before, after)

    assert deltas["endpoint_request_total"] == 3
    assert deltas["probe_success_total"] == 1
    assert after["available"] is True


@pytest.mark.parametrize("stage", ["private-shadow", "assist"])
def test_private_product_stage_rejects_impossible_two_request_sequence(
    live: Any,
    stage: str,
) -> None:
    before, after = _stage_pair(stage)
    after["endpoint_request_total"] += 1
    after["endpoint_success_total"] += 1

    with pytest.raises(live.LiveFailureBatteryError, match="diagnostics do not match"):
        live._product_stage_deltas(stage, before, after)


@pytest.mark.parametrize("stage", ["private-shadow", "assist"])
def test_private_product_stage_rejects_stale_post_success_snapshot(live: Any, stage: str) -> None:
    before, after = _stage_pair(stage)
    before["available"] = False
    after["available"] = False

    with pytest.raises(live.LiveFailureBatteryError, match="admitted healthy secondary"):
        live._product_stage_deltas(stage, before, after)


def test_public_shadow_stale_exception_does_not_relax_failure_stages(live: Any) -> None:
    outage_before, outage_after = _stage_pair("outage")
    outage_before["available"] = False
    with pytest.raises(live.LiveFailureBatteryError, match="admitted healthy secondary"):
        live._product_stage_deltas("outage", outage_before, outage_after)

    recovery_before, recovery_after = _stage_pair("recovery")
    recovery_after["available"] = False
    with pytest.raises(live.LiveFailureBatteryError, match="admitted healthy secondary"):
        live._product_stage_deltas("recovery", recovery_before, recovery_after)


def test_product_source_seal_covers_the_full_isolation_boundary(live: Any) -> None:
    required = {
        "friday/admin_api/_inbox.py",
        "friday/api/ingest.py",
        "friday/executive/service.py",
        "friday/ingestion/_review.py",
        "friday/secondary_product_witness.py",
        "friday/server.py",
        "friday/storage/_core.py",
        "friday/storage/_feedback.py",
        "friday/storage/_intake.py",
        "friday/storage/_maintenance.py",
        "friday/workers/__init__.py",
        "tests/test_mission_proposer_restraint.py",
        "tests/test_secondary_product_witness.py",
        "tests/test_workers.py",
    }
    assert required <= set(live.PRODUCT_SURFACE_FILES)


def _operation(live: Any, *, stage: str, role: str) -> dict[str, Any]:
    value = {key: "4" * 64 for key in live._PRODUCT_OPERATION_KEYS if key.endswith("_sha256")}
    nonce = "a" * 32
    value.update(
        {
            "schema": live.PRODUCT_OPERATION_CORE_SCHEMA,
            "ingest_idempotent_replay": False,
            "advice_endpoint_role": role,
            "exact_secondary_model_observed": role == "secondary",
            "cleanup_status": "purged",
            "knowledge_object_created": False,
            "tool_requested": False,
            "effect_requested": False,
        }
    )
    value["source_ref_sha256"] = live._sha256(live._product_source_ref(stage, nonce))
    value["synthetic_content_sha256"] = live._sha256(live._product_content(stage, nonce))
    value["synthetic_nonce_sha256"] = live._sha256(nonce)
    return value


def _receipt(live: Any, *, stage: str = "assist") -> dict[str, Any]:
    before, after = _stage_pair(stage)
    deltas = live._product_stage_deltas(stage, before, after)
    operation = _operation(
        live,
        stage=stage,
        role="secondary" if stage in {"assist", "recovery"} else "primary",
    )
    stage_binding = live._sha256(
        live._canonical(
            {
                "source_ref_sha256": operation["source_ref_sha256"],
                "before": before,
                "after": after,
                "deltas": deltas,
            }
        )
    )
    operation["stage_diagnostics_binding_sha256"] = stage_binding
    operation_binding = live._sha256(live._canonical(operation))
    attestation = {key: "9" * 64 for key in live._PRODUCT_ATTESTATION_KEYS if key.endswith("_sha256")}
    attestation.update(
        schema=live.PRODUCT_ROLLOUT_ATTESTATION_SCHEMA,
        attestation_id="9" * 32,
        stage=stage,
        advice_endpoint_role=operation["advice_endpoint_role"],
        primary_pid=2613,
        primary_backend_version="0.207.10",
        observer_source_head="5" * 40,
        candidate_profile_id=IDENTITY["candidate_profile_id"],
        candidate_profile_mode="shadow" if "shadow" in stage else "assist",
        candidate_profile_allow_private_text=stage != "public-shadow",
        candidate_profile_context_tokens=4096,
        candidate_profile_admission="accepted",
        served_model_alias=IDENTITY["served_model_alias"],
        state_version=1,
        issued_at=1_777_000_000,
        expires_at=1_777_000_570,
        signature="f" * 64,
        **{key: 0 for key in live._PRODUCT_ATTESTATION_KEYS if key.endswith("_residue")},
    )
    lookup = "b" * 64
    attestation.update(
        operation_binding_sha256=operation_binding,
        stage_diagnostics_binding_sha256=stage_binding,
        source_ref_sha256=operation["source_ref_sha256"],
        advice_proof_sha256=operation["advice_proof_sha256"],
        advice_diagnostics_receipt_sha256=operation["advice_diagnostics_receipt_sha256"],
        lookup_token_sha256=live._sha256(lookup),
    )
    return {
        "schema": live.PRODUCT_STAGE_SCHEMA,
        "status": "passed",
        "stage": stage,
        **IDENTITY,
        "observer_source_head": "5" * 40,
        "observer_runner_sha256": "6" * 64,
        "primary_pid": 2613,
        "primary_process_epoch_sha256": "7" * 64,
        "primary_version": "0.207.10",
        "primary_ca_certificate_sha256": "8" * 64,
        "diagnostics_before": before,
        "diagnostics_after": after,
        "diagnostics_deltas": deltas,
        "diagnostics_binding_sha256": attestation["diagnostics_binding_sha256"],
        "stage_diagnostics_binding_sha256": stage_binding,
        "operation": operation,
        "operation_binding_sha256": operation_binding,
        "server_rollout_attestation": attestation,
        "server_rollout_attestation_sha256": live._sha256(live._canonical(attestation)),
        "server_rollout_lookup_token": lookup,
        "rollout_lookup_token_retained": True,
        "raw_content_retained_in_evidence": False,
        "model_response_retained_in_evidence": False,
        "credentials_retained": False,
    }


def test_primary_ca_identity_prefers_backend_trust_ca_and_rejects_leaf(tmp_path: Path) -> None:
    import os

    import friday.secondary_product_witness as product_witness

    trust_ca = tmp_path / "backend-ca.pem"
    leaf = tmp_path / "backend-leaf.pem"
    trust_ca.write_bytes(b"PRIMARY TRUST CA\n")
    leaf.write_bytes(b"PRIMARY LEAF CERTIFICATE\n")
    trust_ca_sha256 = hashlib.sha256(trust_ca.read_bytes()).hexdigest()
    leaf_sha256 = hashlib.sha256(leaf.read_bytes()).hexdigest()
    configured = SimpleNamespace(backend_ca_file=str(trust_ca), ssl_certfile=str(leaf))
    assert product_witness.secondary_product_primary_certificate_sha256(configured) == trust_ca_sha256
    assert (
        product_witness.secondary_product_primary_certificate_sha256(
            SimpleNamespace(backend_ca_file="", ssl_certfile=str(leaf))
        )
        == leaf_sha256
    )

    runtime_identity = {
        **IDENTITY,
        "candidate_profile_mode": "shadow",
        "candidate_profile_allow_private_text": False,
        "candidate_profile_context_tokens": 4096,
        "candidate_profile_manifest_sha256": IDENTITY["candidate_profile_sha256"],
        "candidate_profile_admission": "provisional_shadow",
    }
    secondary = SimpleNamespace(product_attestation_identity=lambda: runtime_identity)
    observer = {
        "observer_source_head": "5" * 40,
        "observer_runner_sha256": "6" * 64,
        "primary_pid": os.getpid(),
        "primary_process_epoch_sha256": product_witness.secondary_product_process_epoch_sha256(),
        "primary_backend_version": __version__,
        "primary_ca_certificate_sha256": trust_ca_sha256,
        "candidate_profile_sha256": IDENTITY["candidate_profile_sha256"],
    }
    assert (
        product_witness._observer_identity(  # noqa: SLF001
            observer,
            settings=configured,
            secondary=secondary,
        )["primary_ca_certificate_sha256"]
        == trust_ca_sha256
    )
    with pytest.raises(ValueError, match="observer identity"):
        product_witness._observer_identity(  # noqa: SLF001
            {**observer, "primary_ca_certificate_sha256": leaf_sha256},
            settings=configured,
            secondary=secondary,
        )


def test_product_stage_parser_rejects_counter_only_and_mismatched_receipts(live: Any) -> None:
    assert live.PRODUCT_STAGE_SCHEMA == "friday.secondary-product-stage-evidence.v3"
    assert live.PRODUCT_DIAGNOSTICS_SCHEMA == "friday.secondary-product-diagnostics.v2"
    receipt = _receipt(live)
    live.validate_product_stage_evidence(receipt, expected_stage="assist")

    with pytest.raises(live.LiveFailureBatteryError, match="incomplete"):
        live.validate_product_stage_evidence(
            {"schema": live.PRODUCT_STAGE_SCHEMA, "status": "passed", "stage": "assist"}
        )

    legacy_counter_semantics = copy.deepcopy(receipt)
    legacy_counter_semantics["schema"] = "friday.secondary-product-stage-evidence.v2"
    with pytest.raises(live.LiveFailureBatteryError, match="incomplete"):
        live.validate_product_stage_evidence(legacy_counter_semantics)

    forged = copy.deepcopy(receipt)
    forged["diagnostics_deltas"]["success_total"] = 0
    with pytest.raises(live.LiveFailureBatteryError, match="diagnostics binding"):
        live.validate_product_stage_evidence(forged)

    forged = copy.deepcopy(receipt)
    forged["operation"]["cleanup_status"] = "archived"
    with pytest.raises(live.LiveFailureBatteryError, match="operation binding"):
        live.validate_product_stage_evidence(forged)


def test_product_stage_runner_performs_and_purges_the_authenticated_vertical(
    live: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before, after = _stage_pair("assist")
    snapshots = iter((before, after))
    monkeypatch.setattr(live, "configure_expected_model", lambda *_args: None)
    monkeypatch.setattr(live, "_source_identity", lambda: ("5" * 40, "6" * 64))
    monkeypatch.setattr(live, "_friday_backend_main_pid", lambda: 2613)
    monkeypatch.setattr(live, "_primary_process_epoch_sha256", lambda _pid: "7" * 64)
    monkeypatch.setattr(live, "_primary_health", lambda *_args: ("0.207.10", "8" * 64))
    monkeypatch.setattr(live.secrets, "token_hex", lambda _size: "a" * 32)
    monkeypatch.setattr(
        live,
        "_product_snapshot",
        lambda **_kwargs: (next(snapshots), "8" * 64),
    )

    inbox_id = "inbox_0123456789abcdef"
    raw_id = "raw_0123456789abcdef"
    advice = {
        "policy_version": "promotion-v1",
        "model": IDENTITY["served_model_alias"],
        "endpoint_role": "secondary",
        "generated_at": "2026-08-24T12:00:00+00:00",
        "requested_by": LEGACY_OWNER_USER_ID,
        "recommended_action": "review",
        "confidence": 0.8,
        "rationale": "MODEL_RESPONSE_BODY_SENTINEL",
        "validated_entity_count": 0,
        "advisory_only": True,
    }
    suggestions = {"title": "Synthetic witness", "model_advice": advice}
    diagnostics_value = {
        "schema": live.PRODUCT_DIAGNOSTICS_SCHEMA,
        "source_ref_sha256": live._sha256(live._product_source_ref("assist", "a" * 32)),
        "before": _raw_secondary_diagnostics(before),
        "after": _raw_secondary_diagnostics(after),
    }
    diagnostics = {
        **diagnostics_value,
        "binding_sha256": live._sha256(live._canonical(diagnostics_value)),
    }
    pending = {
        "id": inbox_id,
        "raw_object_id": raw_id,
        "status": "pending",
        "knowledge_object_id": None,
        "reviewed_at": None,
        "reviewed_by": None,
        "suggested_action": "review",
        "suggestions_json": json.dumps(suggestions, ensure_ascii=False, sort_keys=True),
    }
    expected_binding = live._product_storage_binding_sha256(
        stage="assist",
        nonce="a" * 32,
        inbox_id=inbox_id,
        raw_object_id=raw_id,
        inbox_status="pending",
        storage_user_id=LEGACY_OWNER_USER_ID,
        uploaded_by=LEGACY_OWNER_USER_ID,
    )
    fake_proof = {
        "server_proof": "9" * 64,
        "diagnostics_binding_sha256": diagnostics["binding_sha256"],
    }
    advice_storage = live._storage_proof(
        pending,
        inbox_id=inbox_id,
        raw_object_id=raw_id,
        expected_status="pending",
        expected_suggestions=suggestions,
    )
    monkeypatch.setattr(
        live,
        "_validate_advice_result",
        lambda *_args, **_kwargs: (
            advice_storage,
            "secondary",
            True,
            before,
            after,
            live._sha256(live._canonical(diagnostics)),
            fake_proof,
        ),
    )
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def api_request(
        endpoint: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], str, bytes, bytes]:
        calls.append((method, endpoint, payload))
        if endpoint == live.PRIMARY_IDENTITY_ENDPOINT:
            value = {
                "actor": {
                    "user_id": LEGACY_OWNER_USER_ID,
                    "preset_key": "owner",
                    "source": "api-token",
                }
            }
        elif endpoint == live.PRIMARY_INGEST_ENDPOINT:
            assert payload and payload["force_review"] is True
            value = {
                "queued_for_review": True,
                "promoted": False,
                "persisted": True,
                "action": "review",
                "inbox_id": inbox_id,
                "raw_object_id": raw_id,
                "secondary_product_storage_binding_sha256": expected_binding,
                "secondary_product_storage_user_id": LEGACY_OWNER_USER_ID,
            }
        elif endpoint.endswith("/advise"):
            value = {
                "item": pending,
                "suggestions": suggestions,
                "model_advice": advice,
                "idempotent_replay": False,
                "secondary_product_diagnostics": diagnostics,
            }
        else:
            assert endpoint == live.PRIMARY_WITNESS_PURGE_ENDPOINT
            assert payload and payload["source_ref_sha256"] == live._sha256(
                live._product_source_ref("assist", "a" * 32)
            )
            assert payload["cleanup_token"] == "a" * 32
            assert payload and isinstance(payload.get("operation"), dict)
            operation = payload["operation"]
            cleanup_core = {
                "schema": live.PRODUCT_CLEANUP_CORE_SCHEMA,
                "purged": True,
                "raw_deleted": 1,
                "inbox_deleted": 1,
                "storage_binding_sha256": expected_binding,
                "raw_object_id_sha256": live._sha256(raw_id),
                "inbox_id_sha256": live._sha256(inbox_id),
                "cleanup_zero_residue_binding_sha256": "c" * 64,
                **{key: 0 for key in live._PRODUCT_CLEANUP_CORE_KEYS if key.endswith("_residue")},
            }
            operation["cleanup_core_sha256"] = live._sha256(live._canonical(cleanup_core))
            attestation = copy.deepcopy(_receipt(live)["server_rollout_attestation"])
            attestation.update(
                operation_binding_sha256=live._sha256(live._canonical(operation)),
                stage_diagnostics_binding_sha256=operation["stage_diagnostics_binding_sha256"],
                advice_proof_sha256=operation["advice_proof_sha256"],
                advice_diagnostics_receipt_sha256=operation["advice_diagnostics_receipt_sha256"],
                diagnostics_binding_sha256=fake_proof["diagnostics_binding_sha256"],
                source_ref_sha256=operation["source_ref_sha256"],
                cleanup_storage_binding_sha256=expected_binding,
                cleanup_zero_residue_binding_sha256=cleanup_core["cleanup_zero_residue_binding_sha256"],
            )
            lookup = "b" * 64
            attestation["lookup_token_sha256"] = live._sha256(lookup)
            value = {
                "schema": "friday.secondary-product-purge-response.v2",
                "cleanup_core": cleanup_core,
                "cleanup_core_sha256": live._sha256(live._canonical(cleanup_core)),
                "server_rollout_attestation": attestation,
                "server_rollout_lookup_token": lookup,
            }
        request_raw = b"" if payload is None else live._canonical(payload)
        return value, "8" * 64, request_raw, live._canonical(value)

    monkeypatch.setattr(live, "_primary_api_request", api_request)
    key = tmp_path / "primary.key"
    key.write_text("p" * 64, encoding="ascii")
    key.chmod(0o600)
    output = tmp_path / "product.assist.json"
    result = live.run_product_stage(
        candidate=tmp_path / "candidate.json",
        ca_file=tmp_path / "secondary-ca.crt",
        primary_api_key_file=key,
        primary_ca_file=tmp_path / "primary-ca.crt",
        primary_pid=2613,
        stage="assist",
        output=output,
    )

    evidence = json.loads(output.read_text(encoding="utf-8"))
    live.validate_product_stage_evidence(evidence, expected_stage="assist")
    assert result["status"] == "product_stage_passed"
    assert [endpoint.rsplit("/", 1)[-1] for _method, endpoint, _payload in calls] == [
        "me",
        "ingest",
        "advise",
        "purge",
    ]
    serialized = json.dumps(evidence)
    assert "Synthetic Friday secondary witness" not in serialized
    assert "Atlas uses PostgreSQL" not in serialized
    assert "MODEL_RESPONSE_BODY_SENTINEL" not in serialized
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in serialized
    assert evidence["operation"]["cleanup_status"] == "purged"


def test_runner_recovers_lost_ingest_and_cleanup_responses_through_real_api(
    live: Any,
    settings: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    from urllib.parse import urlsplit

    import friday.admin_api._inbox as inbox_api
    import friday.secondary_product_witness as product_witness
    import tools.immutable_release_operator as release_operator
    from friday.server import create_app

    monkeypatch.setattr(
        product_witness,
        "secondary_product_primary_certificate_sha256",
        lambda _settings: "8" * 64,
    )

    app = create_app(replace(settings, llm_enabled=True, workers_enabled=False))
    owner = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        scheduler = _attach_private_shadow_secondary(app)
        baseline = _product_tables(app.state.storage)
        monkeypatch.setattr(live, "configure_expected_model", lambda *_args: None)
        monkeypatch.setattr(live, "_source_identity", lambda: ("5" * 40, "6" * 64))
        primary_pid = os.getpid()
        primary_epoch = product_witness.secondary_product_process_epoch_sha256(primary_pid)
        monkeypatch.setattr(live, "_friday_backend_main_pid", lambda: primary_pid)
        monkeypatch.setattr(live, "_primary_process_epoch_sha256", lambda _pid: primary_epoch)
        monkeypatch.setattr(live, "_primary_health", lambda *_args: (__version__, "8" * 64))
        original_token_hex = live.secrets.token_hex
        witness_tokens = iter(tuple(character * 32 for character in "def12345"))
        monkeypatch.setattr(
            live.secrets,
            "token_hex",
            lambda size: next(witness_tokens) if size == 16 else original_token_hex(size),
        )
        calls = {"ingest": 0, "purge": 0}
        race = {
            "overwrite_advice": False,
            "fallback_response_losses": 0,
            "fail_checkpoint": False,
            "checkpoint_failures": 0,
        }
        original_checkpoint = inbox_api.checkpoint_secondary_product_witness_wal

        def checkpoint(value: Any) -> None:
            if race["fail_checkpoint"] and race["checkpoint_failures"] == 0:
                race["checkpoint_failures"] += 1
                raise RuntimeError("simulated post-commit witness checkpoint failure")
            original_checkpoint(value)

        monkeypatch.setattr(
            inbox_api,
            "checkpoint_secondary_product_witness_wal",
            checkpoint,
        )

        def api_request(
            endpoint: str,
            *,
            method: str = "GET",
            payload: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> tuple[dict[str, Any], str, bytes, bytes]:
            path = urlsplit(endpoint).path
            response = client.request(method, path, headers=owner, json=payload)
            if path.endswith("/advise") and race["overwrite_advice"] and response.status_code == 200:
                app.state.storage.update_inbox_suggestions(
                    response.json()["item"]["id"],
                    LEGACY_OWNER_USER_ID,
                    suggestions={"model_advice": {"source": "simulated-worker-race"}},
                )
            if endpoint == live.PRIMARY_WITNESS_PURGE_ENDPOINT:
                calls["purge"] += 1
                if response.status_code == 503 and race["fail_checkpoint"]:
                    raise live.LiveFailureBatteryError("simulated post-commit witness checkpoint failure")
                if (
                    race["overwrite_advice"]
                    and response.status_code == 200
                    and isinstance(payload, dict)
                    and "operation" not in payload
                    and race["fallback_response_losses"] == 0
                ):
                    race["fallback_response_losses"] += 1
                    raise live.LiveFailureBatteryError(
                        "simulated lost committed source-bound cleanup response"
                    )
            assert response.status_code == 200, response.text
            if endpoint == live.PRIMARY_INGEST_ENDPOINT:
                calls["ingest"] += 1
                if calls["ingest"] == 1:
                    raise live.LiveFailureBatteryError("simulated lost committed ingest response")
            if endpoint == live.PRIMARY_WITNESS_PURGE_ENDPOINT and calls["purge"] == 1:
                raise live.LiveFailureBatteryError("simulated lost committed purge response")
            request_raw = b"" if payload is None else live._canonical(payload)
            return response.json(), "8" * 64, request_raw, response.content

        monkeypatch.setattr(live, "_primary_api_request", api_request)
        key = tmp_path / "primary-real.key"
        key.write_text("p" * 64, encoding="ascii")
        key.chmod(0o600)
        output = tmp_path / "product.actual-private-shadow.json"
        result = live.run_product_stage(
            candidate=tmp_path / "candidate.json",
            ca_file=tmp_path / "secondary-ca.crt",
            primary_api_key_file=key,
            primary_ca_file=tmp_path / "primary-ca.crt",
            primary_pid=primary_pid,
            stage="private-shadow",
            output=output,
        )

        evidence = json.loads(output.read_text(encoding="utf-8"))
        live.validate_product_stage_evidence(evidence, expected_stage="private-shadow")
        assert result["status"] == "product_stage_passed"
        assert calls == {"ingest": 2, "purge": 2}
        assert evidence["operation"]["ingest_idempotent_replay"] is True
        assert scheduler.diagnostics_status()["workloads"]["extract"]["success_total"] == 1
        assert evidence["diagnostics_deltas"]["selected_total"] == 1
        assert evidence["diagnostics_deltas"]["success_total"] == 1
        assert evidence["diagnostics_deltas"]["shadow_valid_total"] == 1
        serialized = json.dumps(evidence, ensure_ascii=False)
        assert "SECONDARY_MODEL_BODY_SENTINEL" not in serialized
        assert "dddddddddddddddddddddddddddddddd" not in serialized
        assert _product_tables(app.state.storage) == baseline
        previous = release_operator.ReleaseIdentity(
            root=tmp_path / "previous",
            commit="5" * 40,
            version=__version__,
            tree_manifest_sha256="6" * 64,
            max_schema=39,
            secondary_product_runner_sha256="6" * 64,
        )
        candidate = release_operator.ReleaseIdentity(
            root=tmp_path / "candidate",
            commit="9" * 40,
            version=__version__,
            tree_manifest_sha256="1" * 64,
            max_schema=39,
            secondary_product_runner_sha256="6" * 64,
        )
        profile_identity = {
            "admission": "accepted",
            "allow_private_text": True,
            "context_tokens": 4096,
            "gateway_ca_certificate_sha256": IDENTITY["gateway_ca_certificate_sha256"],
            "manifest_sha256": evidence["server_rollout_attestation"]["candidate_profile_manifest_sha256"],
            "mode": "shadow",
            "profile_id": IDENTITY["candidate_profile_id"],
            "served_model_alias": IDENTITY["served_model_alias"],
        }
        attestation = release_operator._validate_secondary_rollout_receipt(  # noqa: SLF001
            evidence,
            expected_stage="private-shadow",
            previous=previous,
            observer_runner_sha256="6" * 64,
            profile_identity=profile_identity,
            primary_pid=primary_pid,
            primary_process_epoch_sha256=primary_epoch,
            primary_ca_certificate_sha256="8" * 64,
        )
        consume_request = release_operator._secondary_rollout_consume_request(  # noqa: SLF001
            lookup_token=evidence["server_rollout_lookup_token"],
            stage="private-shadow",
            transition="secondary_shadow_to_assist",
            previous=previous,
            candidate=candidate,
            next_env_sha256="a" * 64,
            product_receipt_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
            sealed_runner_sha256="6" * 64,
            server_rollout_attestation_sha256=product_witness.secondary_product_sha256(attestation),
        )
        wrong = client.post(
            "/api/admin/secondary-product-witness/consume-rollout-attestation",
            headers=owner,
            json={**consume_request, "predecessor_commit": "4" * 40},
        )
        assert wrong.status_code == 400
        forged = client.post(
            "/api/admin/secondary-product-witness/consume-rollout-attestation",
            headers=owner,
            json={**consume_request, "attestation_lookup_token": "f" * 64},
        )
        assert forged.status_code == 400
        unused_tombstone = app.state.storage.execute(
            """SELECT response_json FROM request_idempotency
                WHERE user_id=? AND request_key LIKE
                      'secondary-product-witness-purge:private-shadow:%'""",
            (LEGACY_OWNER_USER_ID,),
        ).fetchone()
        assert unused_tombstone is not None
        unused_state = json.loads(unused_tombstone["response_json"])
        assert unused_state["rollout_consume_state"] == "unused"
        assert unused_state["rollout_state_version"] == 1
        mismatched_local_attestation = client.post(
            "/api/admin/secondary-product-witness/consume-rollout-attestation",
            headers=owner,
            json={**consume_request, "server_rollout_attestation_sha256": "f" * 64},
        )
        assert mismatched_local_attestation.status_code == 400
        still_unused_tombstone = app.state.storage.execute(
            """SELECT response_json FROM request_idempotency
                WHERE user_id=? AND request_key LIKE
                      'secondary-product-witness-purge:private-shadow:%'""",
            (LEGACY_OWNER_USER_ID,),
        ).fetchone()
        assert still_unused_tombstone is not None
        still_unused_state = json.loads(still_unused_tombstone["response_json"])
        assert still_unused_state["rollout_consume_state"] == "unused"
        assert still_unused_state["rollout_state_version"] == 1
        with monkeypatch.context() as stale_clock:
            stale_clock.setattr(
                time,
                "time",
                lambda: evidence["server_rollout_attestation"]["expires_at"] + 1,
            )
            stale = client.post(
                "/api/admin/secondary-product-witness/consume-rollout-attestation",
                headers=owner,
                json=consume_request,
            )
        assert stale.status_code == 400
        consumed = client.post(
            "/api/admin/secondary-product-witness/consume-rollout-attestation",
            headers=owner,
            json=consume_request,
        )
        assert consumed.status_code == 200, consumed.text
        assert consumed.content == product_witness.secondary_product_canonical(consumed.json())
        release_operator._validate_secondary_rollout_consume_response(  # noqa: SLF001
            consumed.json(), request=consume_request, attestation=attestation
        )
        # A lost response is deliberately unrecoverable: the CAS already burned it.
        replay = client.post(
            "/api/admin/secondary-product-witness/consume-rollout-attestation",
            headers=owner,
            json=consume_request,
        )
        assert replay.status_code == 409
        tombstone = app.state.storage.execute(
            """SELECT response_json FROM request_idempotency
                WHERE user_id=? AND request_key LIKE 'secondary-product-witness-purge:private-shadow:%'""",
            (LEGACY_OWNER_USER_ID,),
        ).fetchone()
        assert tombstone is not None
        retained = str(tombstone["response_json"])
        for forbidden in (
            evidence["server_rollout_lookup_token"],
            "SECONDARY_MODEL_BODY_SENTINEL",
            "PRIMARY_MODEL_BODY_SENTINEL",
            "Synthetic Friday secondary witness",
        ):
            assert forbidden not in retained

        race["fail_checkpoint"] = True
        checkpoint_failed_output = tmp_path / "product.checkpoint-failed-private-shadow.json"
        with pytest.raises(
            live.LiveFailureBatteryError,
            match="attestation failed after exact source-bound cleanup",
        ):
            live.run_product_stage(
                candidate=tmp_path / "candidate.json",
                ca_file=tmp_path / "secondary-ca.crt",
                primary_api_key_file=key,
                primary_ca_file=tmp_path / "primary-ca.crt",
                primary_pid=primary_pid,
                stage="private-shadow",
                output=checkpoint_failed_output,
            )
        assert race["checkpoint_failures"] == 1
        assert not checkpoint_failed_output.exists()
        assert _product_tables(app.state.storage) == baseline
        checkpoint_tombstone = app.state.storage.execute(
            """SELECT response_json FROM request_idempotency
                WHERE user_id=? AND request_key LIKE
                      'secondary-product-witness-purge:private-shadow:%'""",
            (LEGACY_OWNER_USER_ID,),
        ).fetchone()
        assert checkpoint_tombstone is not None
        checkpoint_state = json.loads(checkpoint_tombstone["response_json"])
        assert checkpoint_state["server_rollout_attestation"] is None
        assert checkpoint_state["rollout_consume_state"] == "unavailable"
        assert checkpoint_state["rollout_state_version"] == 0
        race["fail_checkpoint"] = False

        race["overwrite_advice"] = True
        failed_output = tmp_path / "product.raced-private-shadow.json"
        with pytest.raises(
            live.LiveFailureBatteryError,
            match="attestation failed after exact source-bound cleanup",
        ):
            live.run_product_stage(
                candidate=tmp_path / "candidate.json",
                ca_file=tmp_path / "secondary-ca.crt",
                primary_api_key_file=key,
                primary_ca_file=tmp_path / "primary-ca.crt",
                primary_pid=primary_pid,
                stage="private-shadow",
                output=failed_output,
            )
        assert race["fallback_response_losses"] == 1
        assert not failed_output.exists()
        assert _product_tables(app.state.storage) == baseline
        cleanup_only = app.state.storage.execute(
            """SELECT response_json FROM request_idempotency
                WHERE user_id=? AND request_key LIKE 'secondary-product-witness-purge:private-shadow:%'""",
            (LEGACY_OWNER_USER_ID,),
        ).fetchone()
        assert cleanup_only is not None
        assert json.loads(cleanup_only["response_json"])["server_rollout_attestation"] is None


def test_runner_source_bound_purge_cleans_when_both_ingest_receipts_are_lost(
    live: Any,
    settings: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    from urllib.parse import urlsplit

    import friday.secondary_product_witness as product_witness
    from friday.server import create_app

    monkeypatch.setattr(
        product_witness,
        "secondary_product_primary_certificate_sha256",
        lambda _settings: "8" * 64,
    )

    app = create_app(replace(settings, llm_enabled=True, workers_enabled=False))
    owner = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        _attach_accepted_secondary(app)
        baseline = _product_tables(app.state.storage)
        monkeypatch.setattr(live, "configure_expected_model", lambda *_args: None)
        monkeypatch.setattr(live, "_source_identity", lambda: ("5" * 40, "6" * 64))
        primary_pid = os.getpid()
        primary_epoch = product_witness.secondary_product_process_epoch_sha256(primary_pid)
        monkeypatch.setattr(live, "_friday_backend_main_pid", lambda: primary_pid)
        monkeypatch.setattr(live, "_primary_process_epoch_sha256", lambda _pid: primary_epoch)
        monkeypatch.setattr(live, "_primary_health", lambda *_args: (__version__, "8" * 64))
        original_token_hex = live.secrets.token_hex
        monkeypatch.setattr(
            live.secrets,
            "token_hex",
            lambda size: "f" * 32 if size == 16 else original_token_hex(size),
        )
        calls = {"ingest": 0, "purge": 0}

        def api_request(
            endpoint: str,
            *,
            method: str = "GET",
            payload: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> tuple[dict[str, Any], str, bytes, bytes]:
            response = client.request(method, urlsplit(endpoint).path, headers=owner, json=payload)
            assert response.status_code == 200, response.text
            if endpoint == live.PRIMARY_INGEST_ENDPOINT:
                calls["ingest"] += 1
                raise live.LiveFailureBatteryError("simulated lost committed ingest response")
            if endpoint == live.PRIMARY_WITNESS_PURGE_ENDPOINT:
                calls["purge"] += 1
            request_raw = b"" if payload is None else live._canonical(payload)
            return response.json(), "8" * 64, request_raw, response.content

        monkeypatch.setattr(live, "_primary_api_request", api_request)
        key = tmp_path / "primary-lost.key"
        key.write_text("p" * 64, encoding="ascii")
        key.chmod(0o600)
        output = tmp_path / "must-not-exist.json"
        with pytest.raises(live.LiveFailureBatteryError, match="recoverable identity"):
            live.run_product_stage(
                candidate=tmp_path / "candidate.json",
                ca_file=tmp_path / "secondary-ca.crt",
                primary_api_key_file=key,
                primary_ca_file=tmp_path / "primary-ca.crt",
                primary_pid=primary_pid,
                stage="assist",
                output=output,
            )
        assert calls == {"ingest": 2, "purge": 1}
        assert not output.exists()
        assert _product_tables(app.state.storage) == baseline


@pytest.mark.parametrize("shared_archive", [False, True])
def test_reserved_witness_routes_reject_scoped_token_without_storage_rows(
    settings: Any,
    shared_archive: bool,
) -> None:
    from friday.secondary_product_witness import (
        secondary_product_witness_content,
        secondary_product_witness_source_ref,
    )
    from friday.server import create_app

    configured = replace(settings, shared_archive=shared_archive, workers_enabled=False)
    app = create_app(configured)
    secret = "delegated-secondary-witness-test-token"
    with TestClient(app) as client:
        storage = app.state.storage
        storage.ensure_user("delegated", preset_key="owner")
        storage.update_user("delegated", preset_key="owner")
        storage.create_api_token(
            "delegated",
            hashlib.sha256(secret.encode("utf-8")).hexdigest(),
            label="test",
            created_by="test",
        )
        delegated = {"Authorization": f"Bearer {secret}"}
        baseline = _product_tables(storage)
        baseline_idempotency = int(
            storage.execute("SELECT COUNT(*) AS count FROM request_idempotency").fetchone()["count"]
        )
        nonce = "9" * 32
        source_ref = secondary_product_witness_source_ref("assist", nonce)
        content = secondary_product_witness_content("assist", nonce)

        ingest = client.post(
            "/api/ingest",
            headers=delegated,
            json={
                "content": content,
                "force_review": True,
                "metadata": {"secondary_product_witness": True},
                "source_ref": source_ref,
            },
        )
        purge = client.post(
            "/api/admin/secondary-product-witness/purge",
            headers=delegated,
            json={
                "stage": "assist",
                "cleanup_token": nonce,
                "source_ref_sha256": _sha256_text(source_ref),
                "content_sha256": _sha256_text(content),
            },
        )
        consume = client.post(
            "/api/admin/secondary-product-witness/consume-rollout-attestation",
            headers=delegated,
            json={
                "schema": "friday.secondary-product-rollout-consume-request.v1",
                "attestation_lookup_token": "1" * 64,
                "server_rollout_attestation_sha256": "7" * 64,
                "stage": "private-shadow",
                "transition": "secondary_shadow_to_assist",
                "predecessor_commit": "2" * 40,
                "predecessor_tree_sha256": "3" * 64,
                "candidate_commit": "8" * 40,
                "candidate_tree_sha256": "9" * 64,
                "next_env_sha256": "4" * 64,
                "product_receipt_sha256": "5" * 64,
                "sealed_runner_sha256": "6" * 64,
            },
        )

        assert ingest.status_code == 403
        assert purge.status_code == 403
        assert consume.status_code == 403
        assert _product_tables(storage) == baseline
        assert (
            int(storage.execute("SELECT COUNT(*) AS count FROM request_idempotency").fetchone()["count"])
            == baseline_idempotency
        )


@pytest.mark.parametrize("feedback_state", [False, True])
@pytest.mark.parametrize("target_kind", ["raw", "inbox"])
def test_witness_purge_refuses_raw_or_inbox_feedback_dependencies(
    settings: Any,
    feedback_state: bool,
    target_kind: str,
) -> None:
    from friday.secondary_product_witness import (
        secondary_product_witness_content,
        secondary_product_witness_source_ref,
    )
    from friday.server import create_app

    app = create_app(replace(settings, workers_enabled=False))
    owner = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        storage = app.state.storage
        nonce = ("7" if target_kind == "raw" else "8") * 32
        source_ref = secondary_product_witness_source_ref("assist", nonce)
        content = secondary_product_witness_content("assist", nonce)
        ingested = client.post(
            "/api/ingest",
            headers=owner,
            json={
                "content": content,
                "force_review": True,
                "metadata": {"secondary_product_witness": True},
                "source_ref": source_ref,
            },
        )
        assert ingested.status_code == 200, ingested.text
        target_id = (
            ingested.json()[f"{target_kind}_object_id"]
            if target_kind == "raw"
            else ingested.json()["inbox_id"]
        )
        feedback_id = f"feedback_secondary_{target_kind}_{int(feedback_state)}"
        created_at = "2026-08-25T00:00:00+00:00"
        storage.execute(
            """INSERT INTO feedback(
                   id, user_id, target_type, target_id, feedback_type,
                   score, comment, context_json, created_at
               ) VALUES(?, ?, 'synthetic', ?, 'general', 0, '', '{}', ?)""",
            (feedback_id, LEGACY_OWNER_USER_ID, target_id, created_at),
        )
        if feedback_state:
            storage.execute(
                """INSERT INTO feedback_state(
                       user_id, target_type, target_id, feedback_type, score,
                       comment, context_json, feedback_id, updated_at
                   ) VALUES(?, 'synthetic', ?, 'general', 0, '', '{}', ?, ?)""",
                (LEGACY_OWNER_USER_ID, target_id, feedback_id, created_at),
            )
        storage.commit()

        purge = client.post(
            "/api/admin/secondary-product-witness/purge",
            headers=owner,
            json={
                "stage": "assist",
                "cleanup_token": nonce,
                "source_ref_sha256": _sha256_text(source_ref),
                "content_sha256": _sha256_text(content),
            },
        )
        assert purge.status_code == 400
        assert storage.get_raw_object(ingested.json()["raw_object_id"], LEGACY_OWNER_USER_ID)
        assert storage.get_inbox_item(ingested.json()["inbox_id"], LEGACY_OWNER_USER_ID)


def test_reserved_witness_advice_rejects_delegated_admin(settings: Any) -> None:
    from friday.secondary_product_witness import (
        secondary_product_witness_content,
        secondary_product_witness_source_ref,
    )
    from friday.server import create_app

    app = create_app(replace(settings, workers_enabled=False))
    owner = {"Authorization": f"Bearer {settings.api_token}"}
    secret = "delegated-secondary-witness-advice-token"
    with TestClient(app) as client:
        storage = app.state.storage
        storage.ensure_user("delegated-advice", preset_key="admin")
        storage.create_api_token(
            "delegated-advice",
            hashlib.sha256(secret.encode()).hexdigest(),
            label="test",
            created_by="test",
        )
        delegated = {"Authorization": f"Bearer {secret}"}
        nonce = "6" * 32
        source_ref = secondary_product_witness_source_ref("assist", nonce)
        content = secondary_product_witness_content("assist", nonce)
        ingested = client.post(
            "/api/ingest",
            headers=owner,
            json={
                "content": content,
                "force_review": True,
                "metadata": {"secondary_product_witness": True},
                "source_ref": source_ref,
            },
        )
        assert ingested.status_code == 200
        denied = client.post(
            f"/api/admin/inbox/{ingested.json()['inbox_id']}/advise",
            headers=delegated,
            json={"user_id": LEGACY_OWNER_USER_ID, "force": True},
        )
        assert denied.status_code == 403
        cleaned = client.post(
            "/api/admin/secondary-product-witness/purge",
            headers=owner,
            json={
                "stage": "assist",
                "cleanup_token": nonce,
                "source_ref_sha256": _sha256_text(source_ref),
                "content_sha256": _sha256_text(content),
            },
        )
        assert cleaned.status_code == 200


def test_force_review_admin_advice_and_purge_leave_no_product_material(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import sqlite3

    import friday.secondary_product_witness as product_witness
    import tools.immutable_release_operator as release_operator
    from friday import __version__
    from friday.secondary_product_witness import (
        secondary_product_storage_binding,
        secondary_product_witness_content,
        secondary_product_witness_source_ref,
    )
    from friday.server import create_app

    class LocalAdvice:
        enabled = True
        model = "primary-test-model"

        async def chat(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "content": json.dumps(
                    {
                        "title": "Atlas storage",
                        "summary": "Atlas uses PostgreSQL 16.",
                        "knowledge_kind": "technical_note",
                        "importance": 0.5,
                        "tags": ["atlas"],
                        "entities": [],
                        "recommended_action": "review",
                        "confidence": 0.8,
                        "rationale": "MODEL_RESPONSE_BODY_SENTINEL",
                    }
                )
            }

    configured = replace(settings, llm_enabled=True)
    app = create_app(configured)
    monkeypatch.setattr(
        product_witness,
        "secondary_product_primary_certificate_sha256",
        lambda _settings: "8" * 64,
    )
    owner = {"Authorization": f"Bearer {configured.api_token}"}
    with TestClient(app) as client:
        _attach_accepted_secondary(app)
        storage = app.state.storage

        def count(table: str) -> int:
            return int(storage.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"])

        baseline = {
            table: count(table)
            for table in (
                "raw_objects",
                "inbox",
                "knowledge_objects",
                "file_source_aliases",
                "feedback",
                "feedback_state",
            )
        }
        baseline_idempotency = count("request_idempotency")
        baseline_visible_pending = storage.count_inbox(LEGACY_OWNER_USER_ID, InboxStatus.PENDING)
        pre_witness_backup = storage.create_backup(label="before-secondary-witness")
        pre_witness_backup_bytes = Path(pre_witness_backup["path"]).read_bytes().lower()
        assert b"postgresql" not in pre_witness_backup_bytes
        invalid = client.post(
            "/api/ingest",
            headers=owner,
            json={"content": "x", "force_review": True, "force_knowledge": True},
        )
        assert invalid.status_code == 400
        ordinary_force_review = client.post(
            "/api/ingest",
            headers=owner,
            json={"content": "Ordinary material", "force_review": True},
        )
        assert ordinary_force_review.status_code == 400

        reserved_without_marker = client.post(
            "/api/ingest",
            headers=owner,
            json={
                "content": "x",
                "force_review": True,
                "source_ref": "secondary-product-witness:assist:" + "b" * 32,
            },
        )
        assert reserved_without_marker.status_code == 400
        marker_without_reserved_source = client.post(
            "/api/ingest",
            headers=owner,
            json={
                "content": "x",
                "force_review": True,
                "metadata": {"secondary_product_witness": True},
                "source_ref": "ordinary",
            },
        )
        assert marker_without_reserved_source.status_code == 400

        nonce = "c" * 32
        source_ref = secondary_product_witness_source_ref("assist", nonce)
        content = secondary_product_witness_content("assist", nonce)
        ingest_payload = {
            "content": content,
            "force_review": True,
            "metadata": {"secondary_product_witness": True},
            "source_ref": source_ref,
        }
        ingested = client.post(
            "/api/ingest",
            headers=owner,
            json=ingest_payload,
        )
        assert ingested.status_code == 200, ingested.text
        created = ingested.json()
        assert created["queued_for_review"] is True
        assert created["promoted"] is False
        inbox_id = created["inbox_id"]
        raw_id = created["raw_object_id"]
        raw = storage.get_raw_object(raw_id, LEGACY_OWNER_USER_ID)
        inbox = storage.get_inbox_item(inbox_id, LEGACY_OWNER_USER_ID)
        assert raw and inbox
        assert raw["source"] == "api"
        assert raw["source_ref"] == source_ref
        assert raw["raw_content"] == content
        assert json.loads(raw["metadata_json"])["secondary_product_witness"] is True
        expected_binding = secondary_product_storage_binding(raw, inbox)
        assert created["secondary_product_storage_binding_sha256"] == expected_binding
        assert created["secondary_product_storage_user_id"] == LEGACY_OWNER_USER_ID
        assert storage.count_inbox(LEGACY_OWNER_USER_ID, InboxStatus.PENDING) == baseline_visible_pending
        assert all(
            item["id"] != inbox_id for item in storage.list_inbox(LEGACY_OWNER_USER_ID, InboxStatus.PENDING)
        )
        assert all(
            item["id"] != inbox_id
            for item in storage.list_inbox_detailed(LEGACY_OWNER_USER_ID, InboxStatus.PENDING)
        )
        assert all(
            item["id"] != raw_id for item in storage.search_raw_objects(LEGACY_OWNER_USER_ID, "PostgreSQL 16")
        )
        assert (
            storage.search_raw_objects_in_set(
                LEGACY_OWNER_USER_ID,
                "PostgreSQL 16",
                [raw_id],
            )
            == []
        )
        grouping = storage.group_pending_inbox(LEGACY_OWNER_USER_ID)
        assert grouping["items_total"] == baseline_visible_pending
        assert all(inbox_id not in group["inbox_ids"] for group in grouping["groups"])
        grouped = client.get(
            "/api/admin/inbox/groups",
            headers=owner,
            params={"user_id": LEGACY_OWNER_USER_ID},
        )
        assert grouped.status_code == 200, grouped.text
        assert grouped.json()["pending_total"] == baseline_visible_pending

        single_review = client.post(
            f"/api/admin/inbox/{inbox_id}/classify",
            headers=owner,
            json={"user_id": LEGACY_OWNER_USER_ID, "status": "ignored"},
        )
        assert single_review.status_code == 400
        bulk_review = client.post(
            "/api/admin/inbox/bulk",
            headers=owner,
            json={
                "user_id": LEGACY_OWNER_USER_ID,
                "inbox_ids": [inbox_id],
                "status": "ignored",
                "promote": False,
            },
        )
        assert bulk_review.status_code == 200, bulk_review.text
        assert bulk_review.json()["changed_count"] == 0
        assert bulk_review.json()["skipped"][0]["id"] == inbox_id
        untouched = storage.get_inbox_item(inbox_id, LEGACY_OWNER_USER_ID)
        assert untouched and untouched["status"] == "pending"
        assert untouched["reviewed_at"] is None

        replayed = client.post("/api/ingest", headers=owner, json=ingest_payload)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json()["idempotent_replay"] is True
        assert replayed.json()["queued_for_review"] is True
        assert replayed.json()["inbox_id"] == inbox_id
        assert replayed.json()["raw_object_id"] == raw_id
        assert replayed.json()["secondary_product_storage_binding_sha256"] == expected_binding

        advised = client.post(
            f"/api/admin/inbox/{inbox_id}/advise",
            headers=owner,
            json={
                "user_id": LEGACY_OWNER_USER_ID,
                "force": True,
                "secondary_product_observer": {
                    "observer_source_head": "5" * 40,
                    "observer_runner_sha256": "6" * 64,
                    "primary_pid": os.getpid(),
                    "primary_process_epoch_sha256": product_witness.secondary_product_process_epoch_sha256(),
                    "primary_backend_version": __version__,
                    "primary_ca_certificate_sha256": "8" * 64,
                    "candidate_profile_sha256": IDENTITY["candidate_profile_sha256"],
                },
            },
        )
        assert advised.status_code == 200, advised.text
        advised_item = advised.json()["item"]
        assert advised_item["status"] == "pending"
        assert advised_item["knowledge_object_id"] is None
        assert advised.json()["model_advice"]["advisory_only"] is True
        advice_diagnostics = advised.json()["secondary_product_diagnostics"]
        assert advice_diagnostics["schema"] == "friday.secondary-product-diagnostics.v2"
        assert advice_diagnostics["source_ref_sha256"] == _sha256_text(source_ref)
        assert advice_diagnostics["binding_sha256"] == _sha256_text(
            json.dumps(
                {key: advice_diagnostics[key] for key in ("schema", "source_ref_sha256", "before", "after")},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        assert "MODEL_RESPONSE_BODY_SENTINEL" not in json.dumps(advice_diagnostics)
        assert storage.count_knowledge_objects(LEGACY_OWNER_USER_ID) == baseline["knowledge_objects"]

        for target_type, target_id in (
            ("raw_object", raw_id),
            ("classification", raw_id),
            ("inbox", inbox_id),
        ):
            denied_feedback = client.post(
                "/api/feedback",
                headers=owner,
                json={
                    "target_type": target_type,
                    "target_id": target_id,
                    "feedback_type": "general",
                    "score": 1,
                },
            )
            assert denied_feedback.status_code == 404, denied_feedback.text
        from friday.storage.models import FeedbackItem, new_id

        with pytest.raises(ValueError, match="private knowledge"):
            storage.store_feedback(
                FeedbackItem(
                    id=new_id("fb"),
                    user_id=LEGACY_OWNER_USER_ID,
                    target_type="inbox",
                    target_id=inbox_id,
                )
            )
        assert count("feedback") == baseline["feedback"]
        assert count("feedback_state") == baseline["feedback_state"]

        exported = storage.export_user(LEGACY_OWNER_USER_ID)
        exported_payload = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
        assert all(row["id"] != raw_id for row in exported_payload["raw_objects"])
        assert all(row["id"] != inbox_id for row in exported_payload["inbox"])
        exported_text = json.dumps(exported_payload, ensure_ascii=False, sort_keys=True)
        assert content not in exported_text
        assert "MODEL_RESPONSE_BODY_SENTINEL" not in exported_text

        before_backup = set(configured.backups_dir.glob("*"))
        with pytest.raises(RuntimeError, match="transient secondary product witness"):
            storage.create_backup(label="active-secondary-witness")
        assert set(configured.backups_dir.glob("*")) == before_backup

        cleaned = client.post(
            "/api/admin/secondary-product-witness/purge",
            headers=owner,
            json={
                "stage": "assist",
                "cleanup_token": nonce,
                "source_ref_sha256": _sha256_text(source_ref),
                "content_sha256": _sha256_text(content),
            },
        )
        assert cleaned.status_code == 200, cleaned.text
        cleanup = cleaned.json()
        assert cleanup["schema"] == "friday.secondary-product-purge-response.v2"
        assert cleanup["cleanup_core"]["storage_binding_sha256"] == expected_binding
        assert cleanup["cleanup_core"]["raw_object_id_sha256"] == _sha256_text(raw_id)
        assert cleanup["cleanup_core"]["inbox_id_sha256"] == _sha256_text(inbox_id)
        assert cleanup["server_rollout_attestation"] is None
        assert cleanup["server_rollout_lookup_token"] == ""
        replayed_cleanup = client.post(
            "/api/admin/secondary-product-witness/purge",
            headers=owner,
            json={
                "stage": "assist",
                "cleanup_token": nonce,
                "source_ref_sha256": _sha256_text(source_ref),
                "content_sha256": _sha256_text(content),
            },
        )
        assert replayed_cleanup.status_code == 200, replayed_cleanup.text
        assert replayed_cleanup.json() == cleanup
        assert storage.get_raw_object(raw_id, LEGACY_OWNER_USER_ID) is None
        assert storage.get_inbox_item(inbox_id, LEGACY_OWNER_USER_ID) is None
        assert storage.search_raw_objects(LEGACY_OWNER_USER_ID, "PostgreSQL 16") == []
        clean_backup = storage.create_backup(label="after-secondary-witness")
        backup_conn = __import__("sqlite3").connect(clean_backup["path"])
        try:
            assert (
                backup_conn.execute(
                    "SELECT COUNT(*) FROM raw_objects WHERE source_ref LIKE 'secondary-product-witness:%'"
                ).fetchone()[0]
                == 0
            )
        finally:
            backup_conn.close()
        backup_bytes = Path(clean_backup["path"]).read_bytes()
        assert content.encode("utf-8") not in backup_bytes
        assert b"postgresql" not in backup_bytes.lower()
        assert b"MODEL_RESPONSE_BODY_SENTINEL" not in backup_bytes
        operator_inbox = configured.state_dir / "telegram-inbox.sqlite3"
        if not operator_inbox.exists():
            operator_inbox_conn = sqlite3.connect(operator_inbox)
            try:
                operator_inbox_conn.execute("CREATE TABLE queue_marker(value TEXT)")
                operator_inbox_conn.commit()
            finally:
                operator_inbox_conn.close()
        operator_inbox.chmod(0o600)
        operator_env = configured.state_dir / "product-backup-test.env"
        operator_env.write_text("FRIDAY_PROFILE=test\n", encoding="ascii")
        operator_env.chmod(0o600)
        operator_ca = configured.state_dir / "product-backup-test-ca.pem"
        operator_ca.write_text("synthetic test CA\n", encoding="ascii")
        operator_ca.chmod(0o600)
        operator_backup = release_operator._exact_sqlite_backup(  # noqa: SLF001
            release_operator.SystemdConfig(
                anchor=configured.state_dir / "product-backup-test-anchor",
                env_file=operator_env,
                env_file_sha256=hashlib.sha256(operator_env.read_bytes()).hexdigest(),
                friday_home=configured.home,
                unit_dir=configured.state_dir,
                database=configured.database_path,
                inbox_database=operator_inbox,
                backup_dir=configured.backups_dir / "operator-cutover",
                state_dir=configured.state_dir,
                health_ca=operator_ca,
                health_ca_sha256=hashlib.sha256(operator_ca.read_bytes()).hexdigest(),
            )
        )
        operator_payload = operator_backup.opaque
        assert isinstance(operator_payload, release_operator._ExactBackupPayload)  # noqa: SLF001
        for name, _digest, _size in operator_payload.files:
            copied = (operator_payload.directory / name).read_bytes().lower()
            assert b"postgresql" not in copied
            assert b"model_response_body_sentinel" not in copied
            assert content.encode("utf-8").lower() not in copied
        listed = client.get(
            "/api/admin/inbox",
            headers=owner,
            params={"user_id": LEGACY_OWNER_USER_ID},
        )
        assert listed.status_code == 200
        assert all(item["id"] != inbox_id for item in listed.json()["items"])
        for table, expected in baseline.items():
            assert count(table) == expected
        tombstone = storage.execute(
            """SELECT request_key, request_hash, response_json, state
                 FROM request_idempotency WHERE user_id=? AND request_key=?""",
            (
                LEGACY_OWNER_USER_ID,
                f"secondary-product-witness-purge:assist:{nonce}",
            ),
        ).fetchone()
        assert tombstone is not None
        assert tombstone["state"] == "complete"
        assert count("request_idempotency") == baseline_idempotency + 1
        tombstone_text = str(tombstone["response_json"])
        for forbidden in (content, source_ref, raw_id, inbox_id, "MODEL_RESPONSE_BODY_SENTINEL"):
            assert forbidden not in tombstone_text

        next_nonce = "e" * 32
        next_source_ref = secondary_product_witness_source_ref("assist", next_nonce)
        next_content = secondary_product_witness_content("assist", next_nonce)
        next_ingest = client.post(
            "/api/ingest",
            headers=owner,
            json={
                "content": next_content,
                "force_review": True,
                "metadata": {"secondary_product_witness": True},
                "source_ref": next_source_ref,
            },
        )
        assert next_ingest.status_code == 200, next_ingest.text
        next_cleanup_payload = {
            "stage": "assist",
            "cleanup_token": next_nonce,
            "source_ref_sha256": _sha256_text(next_source_ref),
            "content_sha256": _sha256_text(next_content),
        }
        next_cleanup = client.post(
            "/api/admin/secondary-product-witness/purge",
            headers=owner,
            json=next_cleanup_payload,
        )
        assert next_cleanup.status_code == 200, next_cleanup.text
        next_cleanup_replay = client.post(
            "/api/admin/secondary-product-witness/purge",
            headers=owner,
            json=next_cleanup_payload,
        )
        assert next_cleanup_replay.status_code == 200, next_cleanup_replay.text
        assert next_cleanup_replay.json() == next_cleanup.json()
        stage_tombstones = storage.execute(
            """SELECT request_key, response_json FROM request_idempotency
                 WHERE user_id=? AND request_key LIKE ?""",
            (LEGACY_OWNER_USER_ID, "secondary-product-witness-purge:assist:%"),
        ).fetchall()
        assert len(stage_tombstones) == 1
        assert stage_tombstones[0]["request_key"] == (f"secondary-product-witness-purge:assist:{next_nonce}")
        assert nonce not in str(stage_tombstones[0]["response_json"])
        assert _product_tables(storage) == baseline
        assert count("request_idempotency") == baseline_idempotency + 1
        storage.execute(
            """UPDATE request_idempotency SET created_at='2000-01-01T00:00:00+00:00',
                   updated_at='2000-01-01T00:00:00+00:00'
                 WHERE user_id=? AND request_key LIKE ?""",
            (LEGACY_OWNER_USER_ID, "secondary-product-witness-purge:assist:%"),
        )
        storage.commit()
        assert storage.idempotency_prune(days=30) == 1
        assert count("request_idempotency") == baseline_idempotency
        audit_actions = {
            str(row["action"])
            for row in storage.execute(
                "SELECT action FROM audit_log WHERE action='admin.inbox.purge_secondary_witness'"
            ).fetchall()
        }
        assert audit_actions == {"admin.inbox.purge_secondary_witness"}


def test_backup_start_boundary_blocks_reserved_ingest_before_copy_publication(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import friday.admin_api._inbox as inbox_api
    from friday.secondary_product_witness import (
        secondary_product_witness_content,
        secondary_product_witness_source_ref,
    )
    from friday.server import create_app

    configured = replace(settings, workers_enabled=False)
    app = create_app(configured)
    owner = {"Authorization": f"Bearer {configured.api_token}"}
    with TestClient(app) as client:
        storage = app.state.storage
        entered_verify = threading.Event()
        release_verify = threading.Event()
        original_verify = storage._verify_backup_conn  # noqa: SLF001

        def held_verify(backup_conn: Any) -> Any:
            entered_verify.set()
            if not release_verify.wait(5):
                raise AssertionError("test did not release backup verification")
            return original_verify(backup_conn)

        monkeypatch.setattr(storage, "_verify_backup_conn", held_verify)
        nonce = "f" * 32
        source_ref = secondary_product_witness_source_ref("assist", nonce)
        content = secondary_product_witness_content("assist", nonce)
        payload = {
            "content": content,
            "force_review": True,
            "metadata": {"secondary_product_witness": True},
            "source_ref": source_ref,
        }
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(storage.create_backup, label="lease-before-witness")
            assert entered_verify.wait(5)
            blocked = client.post("/api/ingest", headers=owner, json=payload)
            assert blocked.status_code == 503, blocked.text
            assert (
                storage.execute(
                    "SELECT 1 FROM raw_objects WHERE source_ref=?",
                    (source_ref,),
                ).fetchone()
                is None
            )
            release_verify.set()
            backup = future.result(timeout=10)
        assert Path(backup["path"]).is_file()

        admitted = client.post("/api/ingest", headers=owner, json=payload)
        assert admitted.status_code == 200, admitted.text
        entered_checkpoint = threading.Event()
        release_checkpoint = threading.Event()
        original_checkpoint = inbox_api.checkpoint_secondary_product_witness_wal

        def held_checkpoint(value: Any) -> None:
            entered_checkpoint.set()
            if not release_checkpoint.wait(5):
                raise AssertionError("test did not release witness checkpoint")
            original_checkpoint(value)

        monkeypatch.setattr(
            inbox_api,
            "checkpoint_secondary_product_witness_wal",
            held_checkpoint,
        )
        cleanup_payload = {
            "stage": "assist",
            "cleanup_token": nonce,
            "source_ref_sha256": _sha256_text(source_ref),
            "content_sha256": _sha256_text(content),
        }
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                client.post,
                "/api/admin/secondary-product-witness/purge",
                headers=owner,
                json=cleanup_payload,
            )
            assert entered_checkpoint.wait(5)
            with pytest.raises(RuntimeError, match="secondary product witness boundary"):
                storage.create_backup(label="must-not-race-witness-checkpoint")
            release_checkpoint.set()
            cleaned = future.result(timeout=10)
        assert cleaned.status_code == 200, cleaned.text
