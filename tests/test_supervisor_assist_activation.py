from __future__ import annotations

import hashlib
import inspect
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from friday import semantic_supervisor_policy
from friday.orchestration.capability_binding import (
    CapabilityBindingSnapshot,
    operational_capability_snapshot,
)
from friday.orchestration.supervisor_assist_activation import (
    SUPERVISOR_ASSIST_ACTIVATION_STATUS_SCHEMA,
    AssistPromotionActivationError,
    AssistPromotionActivationReason,
    LoadedAssistPromotionEvidence,
    RawAssistPromotionActivationSettings,
    derive_installed_release_tree_sha256,
    load_assist_promotion_activation,
    load_assist_promotion_live_evidence,
    parse_assist_promotion_live_evidence,
    scheduler_admission_snapshot_from_status,
)
from friday.orchestration.supervisor_assist_promotion import (
    SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS,
    SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS,
    SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
    AssistPromotionEvidenceAuthority,
    AssistPromotionLiveEvidence,
    AssistPromotionOutcomeEvidence,
    AssistPromotionQualityBasis,
    AssistPromotionReadinessEvidence,
)
from friday.orchestration.supervisor_contracts import SupervisorMode, TaskClass
from friday.orchestration.supervisor_promoted_product_event import (
    SupervisorLatencyBudgetDocument,
)

ACTOR = "1" * 64
OTHER = "f" * 64
_ZERO = "0" * 64


def _readiness_product(**changes: object) -> AssistPromotionReadinessEvidence:
    values: dict[str, object] = {
        "baseline_window_sha256": "4" * 64,
        "baseline_observation_count": 20,
        "baseline_complete_count": 8,
        "documented_failure_class_id": "capability:source_unavailable",
        "documented_failure_class_sha256": "5" * 64,
        "baseline_failure_class_count": 5,
        "readiness_witness_sha256": "6" * 64,
        "readiness_observation_count": 20,
        "latency_budget_target_mode": SupervisorMode.ASSIST,
        "latency_budget_source_revision_sha256": _ZERO,
        "latency_budget_ms": 2_500,
        "latency_budget_sha256": "7" * 64,
        "latency_total_ms": 20_000,
        "latency_max_ms": 1_500,
        "call_rate_observation_count": 20,
        "supervisor_invocation_count": 20,
        "unnecessary_supervisor_invocation_count": 0,
        "user_visible_observation_count": 20,
        "user_visible_regression_count": 0,
    }
    values.update(changes)
    return AssistPromotionReadinessEvidence(**values)  # type: ignore[arg-type]


def _outcome_product(**changes: object) -> AssistPromotionOutcomeEvidence:
    values: dict[str, object] = {
        "quality_basis": AssistPromotionQualityBasis.COMPLETION_RATE_IMPROVEMENT,
        "baseline_window_sha256": "4" * 64,
        "promoted_window_sha256": "8" * 64,
        "baseline_observation_count": 20,
        "baseline_complete_count": 8,
        "promoted_observation_count": 20,
        "promoted_complete_count": 12,
        "documented_failure_class_id": "none",
        "documented_failure_class_sha256": None,
        "baseline_failure_class_count": 0,
        "promoted_failure_class_count": 0,
        "latency_budget_target_mode": SupervisorMode.CANARY,
        "latency_budget_source_revision_sha256": _ZERO,
        "latency_budget_ms": 2_500,
        "latency_budget_sha256": "7" * 64,
        "latency_observation_count": 20,
        "latency_total_ms": 20_000,
        "latency_max_ms": 1_500,
        "call_rate_observation_count": 20,
        "supervisor_invocation_count": 20,
        "unnecessary_supervisor_invocation_count": 0,
        "user_visible_observation_count": 20,
        "user_visible_regression_count": 0,
    }
    values.update(changes)
    return AssistPromotionOutcomeEvidence(**values)  # type: ignore[arg-type]


def _release_root(tmp_path: Path, *, manifest: bytes | None = None) -> tuple[Path, str]:
    root = tmp_path / "installed-release"
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True)
    raw = manifest or f"D 0500 {_ZERO} artifacts\n".encode("ascii")
    target = artifacts / "release-tree.sha256"
    target.write_bytes(raw)
    target.chmod(0o400)
    return root, hashlib.sha256(raw).hexdigest()


def _scheduler_status(
    *,
    requested_mode: str = "assist",
    runtime_available: bool = False,
    **changes: object,
) -> tuple[dict[str, object], dict[str, object]]:
    supervisor: dict[str, object] = {
        "workload": semantic_supervisor_policy.SUPERVISOR_WORKLOAD,
        "requested_mode": requested_mode,
        "effective_mode": "shadow",
        "policy_id": semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID,
        "policy_sha256": semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256,
        "workload_available": True,
        "runtime_available": runtime_available,
        "closed_reason": "admitted",
    }
    state = "healthy" if runtime_available else "probing"
    public: dict[str, object] = {
        "schema": "friday.optional-secondary-health.v1",
        "role": "optional_advisory",
        "enabled": True,
        "configured": True,
        "mode": "assist",
        "state": state,
        "available": runtime_available,
        "semantic_supervisor": supervisor,
    }
    diagnostics: dict[str, object] = {
        **public,
        "profile": semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        "profile_admission": "accepted",
        "profile_manifest_match": runtime_available,
        "served_model_match": runtime_available,
        "workloads": {
            "plan_candidate": {
                "routing_mode": "shadow",
                "available": True,
                "closed_reason": "admitted",
                "selected_total": 0,
            }
        },
    }
    for key, value in changes.items():
        if key.startswith("supervisor__"):
            supervisor[key.removeprefix("supervisor__")] = value
        elif key.startswith("diagnostics__"):
            diagnostics[key.removeprefix("diagnostics__")] = value
        elif key.startswith("plan__"):
            plan = diagnostics["workloads"]["plan_candidate"]  # type: ignore[index]
            plan[key.removeprefix("plan__")] = value  # type: ignore[index]
        else:
            public[key] = value
            diagnostics[key] = value
    return public, diagnostics


def _evidence(
    *,
    source_sha256: str,
    registry_sha256: str,
    observed_mode: SupervisorMode = SupervisorMode.SHADOW,
    authority: AssistPromotionEvidenceAuthority = AssistPromotionEvidenceAuthority.PRODUCTION_JOINED,
    latency_budget_sha256: str = "7" * 64,
    latency_budget_ms: int = 2_500,
    **changes: object,
) -> AssistPromotionLiveEvidence:
    # Future contract fixture only.  No repository or live acceptance is claimed.
    observed_policy = semantic_supervisor_policy.supervisor_product_policy_identity_for_mode(observed_mode)
    values: dict[str, object] = {
        "evidence_id": "future_activation_loader_fixture",
        "authority": authority,
        "observed_mode": observed_mode,
        "task_class": TaskClass.COMPARE_CURRENT_FILE_WITH_CURRENT_WEB,
        "source_revision_sha256": source_sha256,
        "promotion_policy_sha256": SUPERVISOR_ASSIST_PROMOTION_POLICY_SHA256,
        "observed_policy_id": observed_policy.policy_id,
        "observed_policy_sha256": observed_policy.policy_sha256,
        "target_policy_id": semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID,
        "target_policy_sha256": semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_SHA256,
        "runtime_profile_id": semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID,
        "runtime_profile_manifest_sha256": (
            semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
        ),
        "registry_binding_sha256": registry_sha256,
        "max_steps": SUPERVISOR_ASSIST_PROMOTION_MAX_STEPS,
        "max_review_rounds": SUPERVISOR_ASSIST_PROMOTION_MAX_REVIEW_ROUNDS,
        "observation_count": 20,
        "joined_trace_count": 20,
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
        "product_evidence": (
            _outcome_product(
                latency_budget_source_revision_sha256=source_sha256,
                latency_budget_sha256=latency_budget_sha256,
                latency_budget_ms=latency_budget_ms,
            )
            if observed_mode is SupervisorMode.ASSIST
            else _readiness_product(
                latency_budget_source_revision_sha256=source_sha256,
                latency_budget_sha256=latency_budget_sha256,
                latency_budget_ms=latency_budget_ms,
            )
        ),
    }
    values.update(changes)
    return AssistPromotionLiveEvidence(**values)  # type: ignore[arg-type]


def _evidence_bytes(evidence: AssistPromotionLiveEvidence) -> bytes:
    return json.dumps(
        evidence.payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _evidence_file(tmp_path: Path, evidence: AssistPromotionLiveEvidence) -> tuple[Path, str]:
    raw = _evidence_bytes(evidence)
    path = tmp_path / "private-promotion-evidence.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    return path, hashlib.sha256(raw).hexdigest()


def _latency_budget_file(
    tmp_path: Path,
    *,
    mode: SupervisorMode,
    source_sha256: str,
    maximum_ms: int = 2_500,
    label: str = "",
) -> tuple[Path, str]:
    document = SupervisorLatencyBudgetDocument(
        target_mode=mode,
        source_revision_sha256=source_sha256,
        maximum_user_visible_latency_ms=maximum_ms,
    )
    raw = json.dumps(
        document.payload(),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    suffix = f"-{label}" if label else ""
    path = tmp_path / f"private-{mode.value}-latency-budget{suffix}.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    return path, hashlib.sha256(raw).hexdigest()


def _raw_settings(
    *,
    evidence_path: Path,
    evidence_sha256: str,
    latency_budget_path: Path,
    latency_budget_sha256: str,
    source_sha256: str,
    registry_sha256: str,
    mode: str = "assist",
    actors: tuple[str, ...] = (),
    **changes: object,
) -> RawAssistPromotionActivationSettings:
    values: dict[str, object] = {
        "enabled": True,
        "requested_mode": mode,
        "evidence_file": str(evidence_path),
        "evidence_sha256": evidence_sha256,
        "latency_budget_file": str(latency_budget_path),
        "latency_budget_sha256": latency_budget_sha256,
        "source_revision_sha256": source_sha256,
        "registry_binding_sha256": registry_sha256,
        "canary_actor_bindings": actors,
    }
    values.update(changes)
    return RawAssistPromotionActivationSettings(**values)


def _activation_fixture(
    tmp_path: Path,
    *,
    mode: str = "assist",
    runtime_available: bool = False,
    authority: AssistPromotionEvidenceAuthority = AssistPromotionEvidenceAuthority.PRODUCTION_JOINED,
) -> tuple[
    RawAssistPromotionActivationSettings,
    Path,
    dict[str, object],
    dict[str, object],
    CapabilityBindingSnapshot,
]:
    root, source = _release_root(tmp_path)
    binding = operational_capability_snapshot()
    observed = SupervisorMode.ASSIST if mode == "canary" else SupervisorMode.SHADOW
    budget_path, budget_sha256 = _latency_budget_file(
        tmp_path,
        mode=SupervisorMode(mode),
        source_sha256=source,
    )
    evidence = _evidence(
        source_sha256=source,
        registry_sha256=binding.digest_hex(),
        observed_mode=observed,
        authority=authority,
        latency_budget_sha256=budget_sha256,
    )
    evidence_path, evidence_sha = _evidence_file(tmp_path, evidence)
    raw = _raw_settings(
        evidence_path=evidence_path,
        evidence_sha256=evidence_sha,
        latency_budget_path=budget_path,
        latency_budget_sha256=budget_sha256,
        source_sha256=source,
        registry_sha256=binding.digest_hex(),
        mode=mode,
        actors=(ACTOR,) if mode == "canary" else (),
    )
    public, diagnostics = _scheduler_status(
        requested_mode=mode,
        runtime_available=runtime_available,
    )
    return raw, root, public, diagnostics, binding


def test_release_tree_identity_is_the_exact_installed_manifest_bytes(tmp_path: Path) -> None:
    first = f"D 0500 {_ZERO} artifacts\n".encode("ascii")
    second = first + f"D 0500 {_ZERO} venv\n".encode("ascii")
    root, expected = _release_root(tmp_path, manifest=first)

    assert derive_installed_release_tree_sha256(root) == expected

    manifest = root / "artifacts/release-tree.sha256"
    manifest.chmod(0o600)
    manifest.write_bytes(second)
    manifest.chmod(0o400)
    assert derive_installed_release_tree_sha256(root) == hashlib.sha256(second).hexdigest()


@pytest.mark.parametrize(
    "manifest",
    [
        b"",
        f"D 0500 {_ZERO} artifacts".encode("ascii"),
        b"not a release manifest\n",
        f"D 0500 {_ZERO} ../escape\n".encode("ascii"),
        f"F 0400 {_ZERO} artifacts/release-tree.sha256\n".encode("ascii"),
        (f"D 0500 {_ZERO} venv\nD 0500 {_ZERO} artifacts\n").encode("ascii"),
        (f"D 0500 {_ZERO} artifacts\nD 0500 {_ZERO} artifacts\n").encode("ascii"),
        b"D 0500 " + _ZERO.encode("ascii") + b" bad\xff\n",
    ],
)
def test_malformed_release_manifest_is_not_a_source_revision(
    tmp_path: Path,
    manifest: bytes,
) -> None:
    root, _digest = _release_root(tmp_path, manifest=manifest or b"placeholder")
    target = root / "artifacts/release-tree.sha256"
    target.chmod(0o600)
    target.write_bytes(manifest)
    target.chmod(0o400)

    assert derive_installed_release_tree_sha256(root) is None


def test_worktree_absence_and_unsafe_manifest_file_close_without_exception(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "ordinary-worktree"
    worktree.mkdir()
    assert derive_installed_release_tree_sha256(worktree) is None

    root, _digest = _release_root(tmp_path)
    manifest = root / "artifacts/release-tree.sha256"
    manifest.chmod(0o644)
    assert derive_installed_release_tree_sha256(root) is None

    manifest.chmod(0o600)
    manifest.unlink()
    manifest.symlink_to(tmp_path / "external-manifest")
    (tmp_path / "external-manifest").write_text(
        f"D 0500 {_ZERO} artifacts\n",
        encoding="ascii",
    )
    assert derive_installed_release_tree_sha256(root) is None


def test_manifest_change_during_descriptor_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _digest = _release_root(tmp_path)
    target = root / "artifacts/release-tree.sha256"
    original_read = os.read
    changed = False

    def changing_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = original_read(descriptor, size)
        if chunk and not changed:
            changed = True
            target.chmod(0o600)
        return chunk

    monkeypatch.setattr(os, "read", changing_read)

    assert derive_installed_release_tree_sha256(root) is None


def test_strict_evidence_parser_round_trips_without_retaining_raw_bytes(tmp_path: Path) -> None:
    root, source = _release_root(tmp_path)
    del root
    registry = operational_capability_snapshot().digest_hex()
    evidence = _evidence(source_sha256=source, registry_sha256=registry)
    raw = _evidence_bytes(evidence)
    digest = hashlib.sha256(raw).hexdigest()

    loaded = parse_assist_promotion_live_evidence(raw, digest)

    assert loaded == LoadedAssistPromotionEvidence(evidence=evidence, file_sha256=digest)
    assert not hasattr(loaded, "raw")
    assert not hasattr(loaded, "body")


def _parse_payload(payload: object) -> LoadedAssistPromotionEvidence:
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=True).encode("utf-8")
    return parse_assist_promotion_live_evidence(raw, hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize(
    "mutation",
    [
        "extra",
        "missing",
        "nonfinite_nan",
        "nonfinite_infinity",
        "finite_float",
        "wrong_schema",
        "wrong_enum",
        "wrong_bool",
        "list_root",
    ],
)
def test_evidence_parser_rejects_extra_missing_nonfinite_and_malformed_fields(
    tmp_path: Path,
    mutation: str,
) -> None:
    _root, source = _release_root(tmp_path)
    evidence = _evidence(
        source_sha256=source,
        registry_sha256=operational_capability_snapshot().digest_hex(),
    )
    payload: Any = evidence.payload()
    if mutation == "extra":
        payload["body"] = "must never enter activation material"
    elif mutation == "missing":
        payload.pop("evidence_id")
    elif mutation == "nonfinite_nan":
        payload["observation_count"] = float("nan")
    elif mutation == "nonfinite_infinity":
        payload["observation_count"] = float("inf")
    elif mutation == "finite_float":
        payload["observation_count"] = 20.0
    elif mutation == "wrong_schema":
        payload["schema"] = "friday.supervisor-assist-promotion.v1"
    elif mutation == "wrong_enum":
        payload["authority"] = "operator_says_yes"
    elif mutation == "wrong_bool":
        payload["primary_fallback_proven"] = 1
    else:
        payload = [payload]

    with pytest.raises(AssistPromotionActivationError) as captured:
        _parse_payload(payload)

    assert captured.value.reason is AssistPromotionActivationReason.EVIDENCE_INVALID


def test_old_v1_v2_v3_evidence_and_old_product_grammar_are_explicitly_rejected(
    tmp_path: Path,
) -> None:
    _root, source = _release_root(tmp_path)
    evidence = _evidence(
        source_sha256=source,
        registry_sha256=operational_capability_snapshot().digest_hex(),
    )
    old_top = evidence.payload()
    old_top["schema"] = "friday.supervisor-assist-promotion.v1"
    old_top.pop("product_evidence")
    old_v2 = evidence.payload()
    old_v2["schema"] = "friday.supervisor-assist-promotion.v2"
    old_v2["p1_policy_id"] = old_v2.pop("observed_policy_id")
    old_v2["p1_policy_sha256"] = old_v2.pop("observed_policy_sha256")
    old_v2.pop("target_policy_id")
    old_v2.pop("target_policy_sha256")
    old_v3 = evidence.payload()
    old_v3["schema"] = "friday.supervisor-assist-promotion.v3"
    old_v3_product = old_v3["product_evidence"]
    assert isinstance(old_v3_product, dict)
    old_v3_product["schema"] = "friday.supervisor-assist-readiness-evidence.v1"
    old_v3_product.pop("latency_budget_target_mode")
    old_v3_product.pop("latency_budget_source_revision_sha256")
    old_product = evidence.payload()
    product = old_product["product_evidence"]
    assert isinstance(product, dict)
    product["schema"] = "friday.supervisor-assist-product-evidence.v1"

    for payload in (old_top, old_v2, old_v3, old_product):
        with pytest.raises(AssistPromotionActivationError) as captured:
            _parse_payload(payload)
        assert captured.value.reason is AssistPromotionActivationReason.EVIDENCE_INVALID


@pytest.mark.parametrize("mutation", ["extra", "missing", "wrong_schema"])
def test_nested_readiness_evidence_uses_an_exact_closed_keyset(
    tmp_path: Path,
    mutation: str,
) -> None:
    _root, source = _release_root(tmp_path)
    payload = _evidence(
        source_sha256=source,
        registry_sha256=operational_capability_snapshot().digest_hex(),
    ).payload()
    product = payload["product_evidence"]
    assert isinstance(product, dict)
    if mutation == "extra":
        product["path"] = "/private/file"
    elif mutation == "missing":
        product.pop("readiness_witness_sha256")
    else:
        product["schema"] = "friday.supervisor-assist-outcome-evidence.v1"

    with pytest.raises(AssistPromotionActivationError) as captured:
        _parse_payload(payload)
    assert captured.value.reason is AssistPromotionActivationReason.EVIDENCE_INVALID


def test_parser_round_trips_the_distinct_actual_assist_outcome_contract(
    tmp_path: Path,
) -> None:
    _root, source = _release_root(tmp_path)
    evidence = _evidence(
        source_sha256=source,
        registry_sha256=operational_capability_snapshot().digest_hex(),
        observed_mode=SupervisorMode.ASSIST,
    )

    loaded = _parse_payload(evidence.payload())

    assert loaded.evidence == evidence
    assert isinstance(loaded.evidence.product_evidence, AssistPromotionOutcomeEvidence)


def test_evidence_parser_rejects_duplicate_keys_invalid_utf8_and_wrong_digest(
    tmp_path: Path,
) -> None:
    _root, source = _release_root(tmp_path)
    evidence = _evidence(
        source_sha256=source,
        registry_sha256=operational_capability_snapshot().digest_hex(),
    )
    payload = _evidence_bytes(evidence)
    duplicate = payload.replace(
        b'"schema":',
        b'"schema":"duplicate","schema":',
        1,
    )
    invalid_utf8 = payload[:-1] + b"\xff}"

    for raw in (duplicate, invalid_utf8):
        with pytest.raises(AssistPromotionActivationError) as captured:
            parse_assist_promotion_live_evidence(raw, hashlib.sha256(raw).hexdigest())
        assert captured.value.reason is AssistPromotionActivationReason.EVIDENCE_INVALID

    with pytest.raises(AssistPromotionActivationError) as captured:
        parse_assist_promotion_live_evidence(payload, OTHER)
    assert captured.value.reason is AssistPromotionActivationReason.EVIDENCE_DIGEST_MISMATCH


def test_evidence_file_requires_private_stable_regular_non_symlink(tmp_path: Path) -> None:
    _root, source = _release_root(tmp_path)
    evidence = _evidence(
        source_sha256=source,
        registry_sha256=operational_capability_snapshot().digest_hex(),
    )
    path, digest = _evidence_file(tmp_path, evidence)

    assert load_assist_promotion_live_evidence(path, digest).evidence == evidence

    path.chmod(0o644)
    with pytest.raises(AssistPromotionActivationError) as public_file:
        load_assist_promotion_live_evidence(path, digest)
    assert public_file.value.reason is AssistPromotionActivationReason.EVIDENCE_FILE_UNAVAILABLE

    path.chmod(0o600)
    alias = tmp_path / "evidence-alias.json"
    alias.symlink_to(path)
    with pytest.raises(AssistPromotionActivationError) as symlink:
        load_assist_promotion_live_evidence(alias, digest)
    assert symlink.value.reason is AssistPromotionActivationReason.EVIDENCE_FILE_UNAVAILABLE


def test_scheduler_projection_is_exact_and_prestart_unavailability_is_retained() -> None:
    public, diagnostics = _scheduler_status(runtime_available=False)

    snapshot = scheduler_admission_snapshot_from_status(public, diagnostics)

    assert snapshot.requested_mode == "assist"
    assert snapshot.effective_mode == "shadow"
    assert snapshot.policy_id == semantic_supervisor_policy.SUPERVISOR_ASSIST_PRODUCT_POLICY_ID
    assert snapshot.runtime_profile_id == semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_ID
    assert snapshot.runtime_profile_manifest_sha256 == (
        semantic_supervisor_policy.SUPERVISOR_RUNTIME_PROFILE_MANIFEST_SHA256
    )
    assert snapshot.workload_available is True
    assert snapshot.runtime_available is False
    assert not hasattr(snapshot, "prompt")
    assert not hasattr(snapshot, "body")


def test_healthy_scheduler_projection_requires_profile_and_served_model_match() -> None:
    public, diagnostics = _scheduler_status(runtime_available=True)
    assert scheduler_admission_snapshot_from_status(public, diagnostics).runtime_available is True

    for key in ("profile_manifest_match", "served_model_match"):
        drifted = dict(diagnostics)
        drifted[key] = False
        with pytest.raises(AssistPromotionActivationError) as captured:
            scheduler_admission_snapshot_from_status(public, drifted)
        assert captured.value.reason is AssistPromotionActivationReason.SCHEDULER_IDENTITY_MISMATCH


@pytest.mark.parametrize(
    "changes",
    [
        {"supervisor__workload": "extract"},
        {"supervisor__requested_mode": "shadow"},
        {"supervisor__effective_mode": "assist"},
        {"supervisor__policy_id": semantic_supervisor_policy.SUPERVISOR_PRODUCT_POLICY_ID},
        {"supervisor__policy_sha256": OTHER},
        {"supervisor__workload_available": False},
        {"supervisor__closed_reason": "endpoint_unavailable"},
        {"diagnostics__profile": "different-profile"},
        {"diagnostics__profile_admission": "provisional_shadow"},
        {"plan__routing_mode": "assist"},
        {"plan__available": False},
        {"plan__closed_reason": "disabled"},
        {"mode": "shadow"},
    ],
)
def test_scheduler_identity_drift_is_a_finite_closed_reason(
    changes: dict[str, object],
) -> None:
    public, diagnostics = _scheduler_status(**changes)  # type: ignore[arg-type]

    with pytest.raises(AssistPromotionActivationError) as captured:
        scheduler_admission_snapshot_from_status(public, diagnostics)

    assert captured.value.reason is AssistPromotionActivationReason.SCHEDULER_IDENTITY_MISMATCH


def test_scheduler_projection_rejects_extra_public_keys_mismatch_and_body_fields() -> None:
    public, diagnostics = _scheduler_status()
    extra_public = {**public, "new_field": True}
    with pytest.raises(AssistPromotionActivationError) as extra:
        scheduler_admission_snapshot_from_status(extra_public, diagnostics)
    assert extra.value.reason is AssistPromotionActivationReason.SCHEDULER_PROJECTION_INVALID

    mismatch = dict(diagnostics)
    mismatch["state"] = "degraded"
    with pytest.raises(AssistPromotionActivationError) as changed:
        scheduler_admission_snapshot_from_status(public, mismatch)
    assert changed.value.reason is AssistPromotionActivationReason.SCHEDULER_PROJECTION_INVALID

    body = dict(diagnostics)
    body["prompt"] = "private text"
    with pytest.raises(AssistPromotionActivationError) as leaked:
        scheduler_admission_snapshot_from_status(public, body)
    assert leaked.value.reason is AssistPromotionActivationReason.SCHEDULER_PROJECTION_INVALID

    nested_public = dict(public)
    nested_public["semantic_supervisor"] = {
        **public["semantic_supervisor"],  # type: ignore[dict-item]
        "response": "private output",
    }
    with pytest.raises(AssistPromotionActivationError):
        scheduler_admission_snapshot_from_status(nested_public, diagnostics)


def test_default_raw_settings_are_closed_without_touching_missing_files(tmp_path: Path) -> None:
    material = load_assist_promotion_activation(
        RawAssistPromotionActivationSettings(),
        installed_release_root=tmp_path / "missing-release",
        scheduler_public_status={},
        scheduler_diagnostics_status={},
        binding_snapshot=operational_capability_snapshot(),
    )

    assert material.configured is False
    assert material.reason is AssistPromotionActivationReason.DEFAULT_OFF
    assert material.operator_gate.enabled is False
    assert material.public_status()["promotion_admitted"] is False


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"enabled": "1"}, AssistPromotionActivationReason.RAW_SETTINGS_INVALID),
        ({"requested_mode": "shadow"}, AssistPromotionActivationReason.MODE_NOT_ADMITTED),
        ({"requested_mode": "ASSIST"}, AssistPromotionActivationReason.MODE_NOT_ADMITTED),
        ({"evidence_file": "relative.json"}, AssistPromotionActivationReason.RAW_SETTINGS_INVALID),
        ({"evidence_sha256": "bad"}, AssistPromotionActivationReason.RAW_SETTINGS_INVALID),
        (
            {"latency_budget_file": "relative.json"},
            AssistPromotionActivationReason.RAW_SETTINGS_INVALID,
        ),
        (
            {"latency_budget_sha256": "bad"},
            AssistPromotionActivationReason.RAW_SETTINGS_INVALID,
        ),
        ({"source_revision_sha256": "bad"}, AssistPromotionActivationReason.RAW_SETTINGS_INVALID),
        ({"registry_binding_sha256": "bad"}, AssistPromotionActivationReason.RAW_SETTINGS_INVALID),
        ({"canary_actor_bindings": []}, AssistPromotionActivationReason.CANARY_ALLOWLIST_INVALID),
        ({"canary_actor_bindings": (ACTOR,)}, AssistPromotionActivationReason.CANARY_ALLOWLIST_INVALID),
    ],
)
def test_invalid_raw_operator_fields_return_typed_closed_material(
    tmp_path: Path,
    changes: dict[str, object],
    reason: AssistPromotionActivationReason,
) -> None:
    raw, root, public, diagnostics, binding = _activation_fixture(tmp_path)
    material = load_assist_promotion_activation(
        replace(raw, **changes),
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )

    assert material.configured is False
    assert material.reason is reason
    assert material.operator_gate.enabled is False


def test_canary_raw_settings_require_a_nonempty_unique_exact_digest_allowlist(
    tmp_path: Path,
) -> None:
    raw, root, public, diagnostics, binding = _activation_fixture(tmp_path, mode="canary")

    configured = load_assist_promotion_activation(
        raw,
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )
    empty = load_assist_promotion_activation(
        replace(raw, canary_actor_bindings=()),
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )
    duplicate = load_assist_promotion_activation(
        replace(raw, canary_actor_bindings=(ACTOR, ACTOR)),
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )

    assert configured.configured is True
    assert configured.operator_gate.canary_actor_bindings == (ACTOR,)
    assert empty.reason is AssistPromotionActivationReason.CANARY_ALLOWLIST_INVALID
    assert duplicate.reason is AssistPromotionActivationReason.CANARY_ALLOWLIST_INVALID


def test_activation_identity_rejects_actual_outcome_evidence_for_first_assist(
    tmp_path: Path,
) -> None:
    raw, root, public, diagnostics, binding = _activation_fixture(tmp_path)
    assert isinstance(raw.source_revision_sha256, str)
    wrong = _evidence(
        source_sha256=raw.source_revision_sha256,
        registry_sha256=binding.digest_hex(),
        product_evidence=_outcome_product(),
    )
    evidence_path, evidence_sha256 = _evidence_file(tmp_path, wrong)

    material = load_assist_promotion_activation(
        replace(
            raw,
            evidence_file=str(evidence_path),
            evidence_sha256=evidence_sha256,
        ),
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )

    assert material.configured is False
    assert material.reason is AssistPromotionActivationReason.EVIDENCE_IDENTITY_MISMATCH


def test_valid_static_material_is_loaded_but_never_described_as_accepted(
    tmp_path: Path,
) -> None:
    raw, root, public, diagnostics, binding = _activation_fixture(tmp_path)

    material = load_assist_promotion_activation(
        raw,
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )

    assert material.configured is True
    assert material.reason is AssistPromotionActivationReason.MATERIAL_LOADED_NOT_ACCEPTED
    assert material.requested_mode is SupervisorMode.ASSIST
    assert material.source_revision_sha256 == raw.source_revision_sha256
    assert material.registry_binding_sha256 == binding.digest_hex()
    assert material.loaded_evidence is not None
    assert material.accepted_latency_budget is not None
    assert material.accepted_latency_budget.document_sha256 == raw.latency_budget_sha256
    assert material.accepted_latency_budget.document.target_mode is SupervisorMode.ASSIST
    assert material.accepted_latency_budget.document.source_revision_sha256 == raw.source_revision_sha256
    assert material.accepted_latency_budget.document.maximum_user_visible_latency_ms == 2_500
    assert not hasattr(material.accepted_latency_budget, "raw")
    assert material.operator_gate.enabled is True
    status = material.public_status()
    assert status == {
        "schema": SUPERVISOR_ASSIST_ACTIVATION_STATUS_SCHEMA,
        "configured": True,
        "reason": "material_loaded_not_accepted",
        "requested_mode": "assist",
        "source_revision_loaded": True,
        "registry_binding_loaded": True,
        "scheduler_projection_loaded": True,
        "scheduler_runtime_available": False,
        "evidence_loaded": True,
        "evidence_authority": "production_joined",
        "operator_gate_enabled": True,
        "canary_actor_binding_count": 0,
        "promotion_admitted": False,
        "evidence_accepted": False,
        "acceptance_authority": "none",
        "body_free": True,
    }
    assert not {
        "path",
        "evidence_file",
        "source_revision_sha256",
        "registry_binding_sha256",
        "latency_budget_file",
        "latency_budget_sha256",
        "actor_binding",
        "body",
        "prompt",
        "response",
    } & set(status)


def test_source_hash_from_raw_settings_cannot_replace_installed_manifest(
    tmp_path: Path,
) -> None:
    raw, _root, public, diagnostics, binding = _activation_fixture(tmp_path)
    missing_root = tmp_path / "worktree-without-release-manifest"
    missing_root.mkdir()

    material = load_assist_promotion_activation(
        raw,
        installed_release_root=missing_root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )

    assert material.configured is False
    assert material.reason is AssistPromotionActivationReason.SOURCE_REVISION_UNAVAILABLE
    assert material.source_revision_sha256 is None


def test_source_registry_scheduler_evidence_hash_and_identity_drift_close_typed(
    tmp_path: Path,
) -> None:
    raw, root, public, diagnostics, binding = _activation_fixture(tmp_path)

    cases: list[
        tuple[
            RawAssistPromotionActivationSettings,
            dict[str, object],
            dict[str, object],
            AssistPromotionActivationReason,
        ]
    ] = [
        (
            replace(raw, source_revision_sha256=OTHER),
            public,
            diagnostics,
            AssistPromotionActivationReason.SOURCE_REVISION_MISMATCH,
        ),
        (
            replace(raw, registry_binding_sha256=OTHER),
            public,
            diagnostics,
            AssistPromotionActivationReason.REGISTRY_BINDING_MISMATCH,
        ),
        (
            raw,
            {**public, "state": "unknown"},
            {**diagnostics, "state": "unknown"},
            AssistPromotionActivationReason.SCHEDULER_IDENTITY_MISMATCH,
        ),
        (
            replace(raw, evidence_sha256=OTHER),
            public,
            diagnostics,
            AssistPromotionActivationReason.EVIDENCE_DIGEST_MISMATCH,
        ),
    ]
    for configured, public_value, diagnostics_value, reason in cases:
        material = load_assist_promotion_activation(
            configured,
            installed_release_root=root,
            scheduler_public_status=public_value,
            scheduler_diagnostics_status=diagnostics_value,
            binding_snapshot=binding,
        )
        assert material.configured is False
        assert material.reason is reason

    evidence_path = Path(str(raw.evidence_file))
    loaded = load_assist_promotion_live_evidence(
        evidence_path,
        str(raw.evidence_sha256),
    )
    wrong_stage = replace(loaded.evidence, observed_mode=SupervisorMode.ASSIST)
    wrong_path, wrong_sha = _evidence_file(tmp_path, wrong_stage)
    identity_drift = load_assist_promotion_activation(
        replace(raw, evidence_file=str(wrong_path), evidence_sha256=wrong_sha),
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )
    assert identity_drift.reason is AssistPromotionActivationReason.EVIDENCE_IDENTITY_MISMATCH


def test_latency_budget_file_and_digest_fail_closed_before_evidence_admission(
    tmp_path: Path,
) -> None:
    raw, root, public, diagnostics, binding = _activation_fixture(tmp_path)

    missing = load_assist_promotion_activation(
        replace(raw, latency_budget_file=str(tmp_path / "missing-budget.json")),
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )
    wrong_digest = load_assist_promotion_activation(
        replace(raw, latency_budget_sha256=OTHER),
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )
    malformed_path = tmp_path / "malformed-latency-budget.json"
    malformed_bytes = b"{}"
    malformed_path.write_bytes(malformed_bytes)
    malformed_path.chmod(0o600)
    malformed = load_assist_promotion_activation(
        replace(
            raw,
            latency_budget_file=str(malformed_path),
            latency_budget_sha256=hashlib.sha256(malformed_bytes).hexdigest(),
        ),
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )
    budget_path = Path(str(raw.latency_budget_file))
    budget_path.chmod(0o644)
    public_file = load_assist_promotion_activation(
        raw,
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )
    budget_path.chmod(0o600)
    alias = tmp_path / "budget-alias.json"
    alias.symlink_to(budget_path)
    symlink = load_assist_promotion_activation(
        replace(raw, latency_budget_file=str(alias)),
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )

    assert missing.reason is AssistPromotionActivationReason.LATENCY_BUDGET_FILE_UNAVAILABLE
    assert wrong_digest.reason is AssistPromotionActivationReason.LATENCY_BUDGET_DIGEST_MISMATCH
    assert malformed.reason is AssistPromotionActivationReason.LATENCY_BUDGET_INVALID
    assert public_file.reason is AssistPromotionActivationReason.LATENCY_BUDGET_FILE_UNAVAILABLE
    assert symlink.reason is AssistPromotionActivationReason.LATENCY_BUDGET_FILE_UNAVAILABLE


def test_latency_budget_mode_source_value_and_evidence_binding_are_exact(
    tmp_path: Path,
) -> None:
    raw, root, public, diagnostics, binding = _activation_fixture(tmp_path)
    assert isinstance(raw.source_revision_sha256, str)

    wrong_mode_path, wrong_mode_sha = _latency_budget_file(
        tmp_path,
        mode=SupervisorMode.CANARY,
        source_sha256=raw.source_revision_sha256,
        label="wrong-mode",
    )
    wrong_source_path, wrong_source_sha = _latency_budget_file(
        tmp_path,
        mode=SupervisorMode.ASSIST,
        source_sha256=OTHER,
        label="wrong-source",
    )
    different_value_path, different_value_sha = _latency_budget_file(
        tmp_path,
        mode=SupervisorMode.ASSIST,
        source_sha256=raw.source_revision_sha256,
        maximum_ms=2_400,
        label="different-value",
    )
    cases = (
        (
            replace(
                raw,
                latency_budget_file=str(wrong_mode_path),
                latency_budget_sha256=wrong_mode_sha,
            ),
            AssistPromotionActivationReason.LATENCY_BUDGET_IDENTITY_MISMATCH,
        ),
        (
            replace(
                raw,
                latency_budget_file=str(wrong_source_path),
                latency_budget_sha256=wrong_source_sha,
            ),
            AssistPromotionActivationReason.LATENCY_BUDGET_IDENTITY_MISMATCH,
        ),
        (
            replace(
                raw,
                latency_budget_file=str(different_value_path),
                latency_budget_sha256=different_value_sha,
            ),
            AssistPromotionActivationReason.EVIDENCE_IDENTITY_MISMATCH,
        ),
    )
    for configured, reason in cases:
        material = load_assist_promotion_activation(
            configured,
            installed_release_root=root,
            scheduler_public_status=public,
            scheduler_diagnostics_status=diagnostics,
            binding_snapshot=binding,
        )
        assert material.configured is False
        assert material.reason is reason

    loaded = load_assist_promotion_live_evidence(
        Path(str(raw.evidence_file)),
        str(raw.evidence_sha256),
    )
    drifted_product = replace(
        loaded.evidence.product_evidence,
        latency_budget_source_revision_sha256=OTHER,
    )
    drifted_evidence = replace(loaded.evidence, product_evidence=drifted_product)
    evidence_path, evidence_sha = _evidence_file(tmp_path, drifted_evidence)
    evidence_drift = load_assist_promotion_activation(
        replace(raw, evidence_file=str(evidence_path), evidence_sha256=evidence_sha),
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )
    assert evidence_drift.reason is AssistPromotionActivationReason.EVIDENCE_IDENTITY_MISMATCH


def test_prestart_material_rebuilds_a_fresh_candidate_after_laptop_health_changes(
    tmp_path: Path,
) -> None:
    raw, root, public, diagnostics, binding = _activation_fixture(
        tmp_path,
        runtime_available=False,
    )
    material = load_assist_promotion_activation(
        raw,
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )
    initial = material.fresh_candidate(public, diagnostics, binding)
    healthy_public, healthy_diagnostics = _scheduler_status(runtime_available=True)
    healthy = material.fresh_candidate(
        healthy_public,
        healthy_diagnostics,
        binding,
        actor_binding_sha256=ACTOR,
    )

    assert material.configured is True
    assert material.scheduler_snapshot is not None
    assert material.scheduler_snapshot.runtime_available is False
    assert initial is not None and initial.scheduler.runtime_available is False
    assert healthy is not None and healthy.scheduler.runtime_available is True
    assert healthy.latency_budget_sha256 == raw.latency_budget_sha256
    assert healthy.latency_budget_ms == 2_500
    assert healthy.actor_binding_sha256 == ACTOR
    assert material.scheduler_snapshot.runtime_available is False


def test_fresh_candidate_fails_closed_on_registry_or_status_drift(tmp_path: Path) -> None:
    raw, root, public, diagnostics, binding = _activation_fixture(tmp_path)
    material = load_assist_promotion_activation(
        raw,
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )
    drifted_bindings = CapabilityBindingSnapshot(bindings=binding.bindings[:-1])
    malformed_public = {**public, "prompt": "private"}

    assert material.fresh_candidate(public, diagnostics, drifted_bindings) is None
    assert material.fresh_candidate(malformed_public, diagnostics, binding) is None


def test_programmer_type_errors_raise_but_external_faults_are_typed_closed(
    tmp_path: Path,
) -> None:
    raw, root, public, diagnostics, binding = _activation_fixture(tmp_path)
    with pytest.raises(TypeError):
        load_assist_promotion_activation(
            object(),  # type: ignore[arg-type]
            installed_release_root=root,
            scheduler_public_status=public,
            scheduler_diagnostics_status=diagnostics,
            binding_snapshot=binding,
        )
    with pytest.raises(TypeError):
        derive_installed_release_tree_sha256(str(root))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        parse_assist_promotion_live_evidence("{}", OTHER)  # type: ignore[arg-type]

    missing = load_assist_promotion_activation(
        replace(raw, evidence_file=str(tmp_path / "missing.json")),
        installed_release_root=root,
        scheduler_public_status=public,
        scheduler_diagnostics_status=diagnostics,
        binding_snapshot=binding,
    )
    assert missing.reason is AssistPromotionActivationReason.EVIDENCE_FILE_UNAVAILABLE


def test_activation_loader_api_has_no_executor_publisher_or_storage_dependency() -> None:
    assert set(inspect.signature(load_assist_promotion_activation).parameters) == {
        "raw",
        "installed_release_root",
        "scheduler_public_status",
        "scheduler_diagnostics_status",
        "binding_snapshot",
    }
    assert set(inspect.signature(scheduler_admission_snapshot_from_status).parameters) == {
        "public_status",
        "diagnostics_status",
    }
