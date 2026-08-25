from __future__ import annotations

import hashlib
import json
import stat
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import friday.secondary_brain.document_map_evidence as evidence
from friday.permissions import LEGACY_OWNER_USER_ID
from friday.secondary_brain.contracts import (
    EffectClass,
    ModelModality,
    ModelPriority,
    ModelRequest,
    ModelUsage,
    ModelWorkload,
    SecondaryAttempt,
    SecondaryFailure,
    SecondaryMode,
    SecondaryResult,
    SecondaryState,
)
from friday.secondary_brain.profiles import (
    ACCEPTED_SECONDARY_RUNTIME_PROFILES,
    SecondaryProfileAdmission,
)
from friday.secondary_brain.scheduler import SecondaryBrainScheduler
from friday.secondary_product_witness import (
    secondary_product_canonical,
    secondary_product_sha256,
    secondary_product_signing_key,
)

_PROFILE = next(iter(ACCEPTED_SECONDARY_RUNTIME_PROFILES.values()))
_CANDIDATE_PROFILE_SHA256 = "51af2164fa07ff3c01813e318076f7ac8b37eeecb73e695b6ca7543061c93439"
_NOW = 1_800_000_000
_DOCUMENT_SENTINEL = "PRIVATE-DOCUMENT-EVIDENCE-MUST-NOT-PERSIST"
_RESULT_SENTINEL = "PRIVATE-SECONDARY-RESULT-MUST-NOT-PERSIST"


def _identity(*, pid: int = 4242, process_epoch: str = "1" * 64) -> dict[str, Any]:
    return {
        "primary_pid": pid,
        "primary_process_epoch_sha256": process_epoch,
        "primary_backend_version": "0.207.27",
        "primary_ca_certificate_sha256": "2" * 64,
        "candidate_profile_id": _PROFILE.profile_id,
        "candidate_profile_mode": "assist",
        "candidate_profile_allow_private_text": True,
        "candidate_profile_context_tokens": 4096,
        "candidate_profile_sha256": _CANDIDATE_PROFILE_SHA256,
        "candidate_profile_manifest_sha256": _PROFILE.manifest_sha256,
        "candidate_profile_admission": "accepted",
        "served_model_alias": _PROFILE.served_model_alias,
        "gateway_ca_certificate_sha256": _PROFILE.gateway_ca_certificate_sha256,
        "predecessor_release_commit": "a" * 40,
        "predecessor_release_tree_manifest_sha256": "b" * 64,
        "predecessor_release_metadata_sha256": "4" * 64,
        "predecessor_release_wheel_sha256": "5" * 64,
        "predecessor_live_env_sha256": "9" * 64,
        "predecessor_live_env_path_sha256": "6" * 64,
        "predecessor_release_anchor_path_sha256": "7" * 64,
    }


def _request() -> ModelRequest:
    return ModelRequest(
        workload=ModelWorkload.DOCUMENT_MAP,
        messages=(
            {"role": "system", "content": "Read-only map."},
            {"role": "user", "content": _DOCUMENT_SENTINEL},
        ),
        max_output_tokens=512,
        absolute_deadline_monotonic=time.monotonic() + 30.0,
        priority=ModelPriority.BACKGROUND,
        effect_class=EffectClass.READ_ONLY,
        modality=ModelModality.TEXT,
        require_structured_output=True,
        structured_output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["summary"],
            "properties": {"summary": {"type": "string", "maxLength": 3200}},
        },
        require_independent_model=True,
        contains_private_text=True,
    )


def _result() -> SecondaryResult:
    return SecondaryResult(
        visible_content=_RESULT_SENTINEL,
        structured_output={"summary": _RESULT_SENTINEL},
        served_model_alias=_PROFILE.served_model_alias,
        usage=ModelUsage(120, 20, 140),
        latency_sec=0.25,
    )


def _shadow_settings(settings: Any) -> Any:
    return replace(
        settings,
        secondary_llm_mode="assist",
        secondary_llm_allow_private_text=True,
        secondary_llm_document_map_mode="shadow",
        secondary_llm_workloads=("document_map", "extract"),
    )


def _consume_request(receipt: dict[str, Any], receipt_sha256: str) -> dict[str, Any]:
    return {
        "schema": evidence.DOCUMENT_MAP_SHADOW_CONSUME_REQUEST_SCHEMA,
        "attestation_lookup_token": receipt["server_rollout_lookup_token"],
        "server_rollout_attestation_sha256": receipt["server_rollout_attestation_sha256"],
        "transition": evidence.DOCUMENT_MAP_SHADOW_TRANSITION,
        "predecessor_commit": "a" * 40,
        "predecessor_tree_sha256": "b" * 64,
        "predecessor_env_sha256": "9" * 64,
        "candidate_commit": "c" * 40,
        "candidate_tree_sha256": "d" * 64,
        "next_env_sha256": "e" * 64,
        "product_receipt_sha256": receipt_sha256,
        "predecessor_policy_id": evidence.DOCUMENT_MAP_SHADOW_POLICY_ID,
        "predecessor_policy_manifest_sha256": evidence.DOCUMENT_MAP_SHADOW_POLICY_SHA256,
        "candidate_policy_id": "gptoss20b-document-map-v2",
        "candidate_policy_manifest_sha256": "f" * 64,
        "accepted_shadow_receipt_sha256": receipt_sha256,
    }


def _record(
    storage: Any,
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    promotion_grade: bool = False,
) -> tuple[Path, str, dict[str, Any], Any]:
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        display_name="Owner",
        preset_key="owner",
    )
    shadow_settings = _shadow_settings(settings)
    identity = _identity()
    monkeypatch.setattr(evidence, "_server_identity", lambda *_args, **_kwargs: identity)
    path, digest = evidence.record_document_map_shadow_result(
        storage,
        owner_user_id=LEGACY_OWNER_USER_ID,
        request=_request(),
        result=_result(),
        settings=shadow_settings,
        secondary=object(),
        now=_NOW,
        attestation_id="3" * 32,
        diagnostics_proof=(
            {
                "observation_kind": "exclusive_owner_one_shot",
                "scheduler_selected_delta": 1,
                "scheduler_success_delta": 1,
                "shadow_valid_delta": 1,
                "shadow_invalid_delta": 0,
                "shadow_skipped_delta": 0,
                "shadow_in_flight_before": 0,
                "shadow_in_flight_after": 0,
            }
            if promotion_grade
            else None
        ),
    )
    if promotion_grade:
        identity_sha256 = secondary_product_sha256(identity)
        request_key, started_json = evidence._one_shot_claim(  # noqa: SLF001
            storage,
            owner_user_id=LEGACY_OWNER_USER_ID,
            identity_sha256=identity_sha256,
            now=_NOW,
        )
        evidence._finish_one_shot(  # noqa: SLF001
            storage,
            owner_user_id=LEGACY_OWNER_USER_ID,
            request_key=request_key,
            started_json=started_json,
            identity_sha256=identity_sha256,
            status="passed",
            now=_NOW,
            receipt_sha256=digest,
        )
    return path, digest, json.loads(path.read_text(encoding="utf-8")), shadow_settings


def test_real_shadow_receipt_is_owner_private_content_free_and_causal(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, digest, receipt, _shadow_settings_value = _record(storage, settings, monkeypatch)
    raw = path.read_bytes()
    attestation = receipt["server_rollout_attestation"]

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert hashlib.sha256(raw).hexdigest() == digest
    assert _DOCUMENT_SENTINEL.encode() not in raw
    assert _RESULT_SENTINEL.encode() not in raw
    assert hashlib.sha256(_DOCUMENT_SENTINEL.encode()).hexdigest().encode() not in raw
    assert hashlib.sha256(_RESULT_SENTINEL.encode()).hexdigest().encode() not in raw
    assert b"selected_total" not in raw and b"success_total" not in raw
    stored = storage.execute(
        "SELECT response_json FROM request_idempotency WHERE request_key LIKE ?",
        ("secondary-document-map-shadow:%",),
    ).fetchone()
    assert stored is not None
    stored_raw = str(stored["response_json"]).encode()
    assert _DOCUMENT_SENTINEL.encode() not in stored_raw
    assert _RESULT_SENTINEL.encode() not in stored_raw
    assert receipt["document_text_retained_in_evidence"] is False
    assert receipt["model_response_retained_in_evidence"] is False
    assert attestation["workload"] == "document_map"
    assert attestation["routing_mode"] == "shadow"
    assert attestation["shadow_policy_manifest_sha256"] == evidence.DOCUMENT_MAP_SHADOW_POLICY_SHA256
    assert evidence.verify_document_map_shadow_attestation(
        secondary_product_signing_key(storage),
        attestation,
        now=_NOW + 1,
        current_server_identity=_identity(),
    )


def test_observation_binding_is_structural_not_a_document_or_response_digest() -> None:
    first = evidence.document_map_shadow_observation(_request(), _result())
    changed_request = replace(
        _request(),
        messages=(
            {"role": "system", "content": "completely different policy text"},
            {"role": "user", "content": "completely different document text"},
        ),
    )
    changed_result = replace(
        _result(),
        visible_content="completely different visible result",
        structured_output={"summary": "completely different validated summary"},
    )

    assert evidence.document_map_shadow_observation(changed_request, changed_result) == first


def test_tamper_stale_and_process_drift_are_rejected(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _path, _digest, receipt, _shadow_settings_value = _record(storage, settings, monkeypatch)
    key = secondary_product_signing_key(storage)
    attestation = receipt["server_rollout_attestation"]

    tampered = {**attestation, "routing_mode": "assist"}
    assert not evidence.verify_document_map_shadow_attestation(key, tampered, now=_NOW + 1)
    assert not evidence.verify_document_map_shadow_attestation(
        key,
        attestation,
        now=_NOW + evidence.DOCUMENT_MAP_SHADOW_ATTESTATION_TTL_SEC + 1,
    )
    assert not evidence.verify_document_map_shadow_attestation(
        key,
        attestation,
        now=_NOW + 1,
        current_server_identity=_identity(pid=4243),
    )
    for field, value in (
        ("predecessor_release_commit", "f" * 40),
        ("predecessor_release_tree_manifest_sha256", "f" * 64),
        ("predecessor_release_metadata_sha256", "f" * 64),
        ("predecessor_release_wheel_sha256", "f" * 64),
        ("predecessor_live_env_sha256", "f" * 64),
        ("predecessor_live_env_path_sha256", "f" * 64),
        ("predecessor_release_anchor_path_sha256", "f" * 64),
    ):
        assert not evidence.verify_document_map_shadow_attestation(
            key,
            attestation,
            now=_NOW + 1,
            current_server_identity={**_identity(), field: value},
        )


@pytest.mark.parametrize("field", ["request_key", "request_hash"])
def test_receipt_database_key_and_hash_drift_fail_closed(
    field: str,
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _path, _digest, _receipt_value, shadow_settings = _record(storage, settings, monkeypatch)
    replacement = f"secondary-document-map-shadow:{'4' * 32}" if field == "request_key" else "f" * 64
    with storage.transaction() as connection:
        connection.execute(
            f"UPDATE request_idempotency SET {field}=? WHERE request_key LIKE ?",  # noqa: S608
            (replacement, "secondary-document-map-shadow:%"),
        )
    with pytest.raises(RuntimeError, match="database binding is invalid"):
        evidence.record_document_map_shadow_result(
            storage,
            owner_user_id=LEGACY_OWNER_USER_ID,
            request=_request(),
            result=_result(),
            settings=shadow_settings,
            secondary=object(),
            now=_NOW + 1,
        )


def test_fresh_unused_receipt_is_stable_across_later_shadow_chunks(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, first_digest, first_receipt, shadow_settings = _record(storage, settings, monkeypatch)
    second_path, second_digest = evidence.record_document_map_shadow_result(
        storage,
        owner_user_id=LEGACY_OWNER_USER_ID,
        request=replace(
            _request(),
            messages=(
                {"role": "system", "content": "Read-only map."},
                {"role": "user", "content": "a later chunk"},
            ),
        ),
        result=replace(_result(), structured_output={"summary": "a later valid summary"}),
        settings=shadow_settings,
        secondary=object(),
        now=_NOW + 10,
        attestation_id="4" * 32,
    )

    assert second_path == path
    assert second_digest == first_digest
    assert json.loads(path.read_text(encoding="utf-8")) == first_receipt
    count = storage.execute(
        "SELECT COUNT(*) AS count FROM request_idempotency WHERE request_key LIKE ?",
        ("secondary-document-map-shadow:%",),
    ).fetchone()
    assert count["count"] == 1


def test_exact_consume_is_idempotent_but_rejects_candidate_rebinding(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _path, digest, receipt, shadow_settings = _record(
        storage,
        settings,
        monkeypatch,
        promotion_grade=True,
    )
    request = _consume_request(receipt, digest)
    assert evidence.validate_document_map_shadow_consume_request(request)

    same_candidate = {**request, "candidate_commit": request["predecessor_commit"]}
    assert not evidence.validate_document_map_shadow_consume_request(same_candidate)
    rebound = {**request, "accepted_shadow_receipt_sha256": "9" * 64}
    assert not evidence.validate_document_map_shadow_consume_request(rebound)
    for field in ("predecessor_tree_sha256", "predecessor_env_sha256"):
        with pytest.raises(ValueError, match="identity is invalid"):
            evidence.consume_document_map_shadow_rollout_attestation(
                storage,
                LEGACY_OWNER_USER_ID,
                request_value={**request, field: "8" * 64},
                settings=shadow_settings,
                secondary=object(),
                now=_NOW + 1,
            )

    response = evidence.consume_document_map_shadow_rollout_attestation(
        storage,
        LEGACY_OWNER_USER_ID,
        request_value=request,
        settings=shadow_settings,
        secondary=object(),
        now=_NOW + 2,
    )
    assert response["status"] == "consumed"
    assert response["candidate_commit"] == "c" * 40
    assert response["request_sha256"] == secondary_product_sha256(request)
    retried = evidence.consume_document_map_shadow_rollout_attestation(
        storage,
        LEGACY_OWNER_USER_ID,
        request_value=request,
        settings=shadow_settings,
        secondary=object(),
        now=_NOW + 3,
    )
    assert retried == response
    with pytest.raises(RuntimeError, match="consumed audit is invalid"):
        evidence.consume_document_map_shadow_rollout_attestation(
            storage,
            LEGACY_OWNER_USER_ID,
            request_value={**request, "candidate_commit": "d" * 40},
            settings=shadow_settings,
            secondary=object(),
            now=_NOW + 3,
        )


def test_receipt_survives_storage_restart_before_one_use_consume(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.storage import init_storage

    first = init_storage(settings)
    try:
        _path, digest, receipt, shadow_settings = _record(
            first,
            settings,
            monkeypatch,
            promotion_grade=True,
        )
    finally:
        first.close()
    second = init_storage(settings)
    try:
        response = evidence.consume_document_map_shadow_rollout_attestation(
            second,
            LEGACY_OWNER_USER_ID,
            request_value=_consume_request(receipt, digest),
            settings=shadow_settings,
            secondary=object(),
            now=_NOW + 2,
        )
        assert response["status"] == "consumed"
    finally:
        second.close()


@pytest.mark.asyncio
async def test_scheduler_observer_is_causal_primary_once_and_non_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = SecondaryBrainScheduler(
        mode=SecondaryMode.ASSIST,
        allowed_workloads=frozenset({ModelWorkload.DOCUMENT_MAP}),
        allow_private_text=True,
        client=None,
        unavailable_state=SecondaryState.PROBING,
        profile_admission=SecondaryProfileAdmission.ACCEPTED,
        document_map_mode=SecondaryMode.SHADOW,
    )
    result = _result()
    request = _request()
    primary_value = {"content": "exact-primary", "opaque": object()}
    primary_calls = 0
    observed: list[tuple[ModelRequest, SecondaryResult]] = []

    async def attempt(request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        assert shadow is True
        return SecondaryAttempt.success(result)

    async def primary() -> dict[str, Any]:
        nonlocal primary_calls
        primary_calls += 1
        return primary_value

    async def observer(request: ModelRequest, value: SecondaryResult) -> None:
        observed.append((request, value))
        raise RuntimeError("receipt disk unavailable")

    monkeypatch.setattr(scheduler, "attempt", attempt)
    returned = await scheduler.run_shadow(
        lambda: request,
        primary,
        validator=lambda value: value is result,
        valid_result_observer=observer,
    )
    await scheduler.drain_shadow()

    assert returned is primary_value
    assert primary_calls == 1
    assert observed == [(request, result)]
    assert scheduler.diagnostics_status()["shadow"]["valid_total"] == 1


@pytest.mark.asyncio
async def test_real_agent_document_map_seam_emits_receipt_only_after_valid_shadow(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.agent_runtime as agent_runtime_module
    from friday.agent_runtime import AgentContext, AgentRuntime

    scheduler = SecondaryBrainScheduler(
        mode=SecondaryMode.ASSIST,
        allowed_workloads=frozenset({ModelWorkload.DOCUMENT_MAP}),
        allow_private_text=True,
        client=None,
        unavailable_state=SecondaryState.PROBING,
        profile_admission=SecondaryProfileAdmission.ACCEPTED,
        document_map_mode=SecondaryMode.SHADOW,
    )
    primary_result = {"content": "exact primary map", "finish_reason": "stop"}
    primary_calls = 0
    recorded: list[tuple[ModelRequest, SecondaryResult]] = []

    class Primary:
        async def chat(self, _messages: Any, **_kwargs: Any) -> dict[str, Any]:
            nonlocal primary_calls
            primary_calls += 1
            return primary_result

    async def attempt(request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        assert shadow is True
        return SecondaryAttempt.success(_result())

    monkeypatch.setattr(scheduler, "attempt", attempt)
    monkeypatch.setattr(
        scheduler,
        "workload_mode",
        lambda workload: (
            SecondaryMode.SHADOW if workload is ModelWorkload.DOCUMENT_MAP else SecondaryMode.ASSIST
        ),
    )
    runtime = AgentRuntime(settings, storage, llm=Primary(), secondary_brain=scheduler)
    monkeypatch.setattr(runtime, "_secondary_document_map_profile_limits", lambda: (4096, 512))
    monkeypatch.setattr(
        evidence,
        "record_document_map_shadow_result",
        lambda _storage, *, request, result, **_kwargs: recorded.append((request, result)),
    )
    context = AgentContext(
        conversation_id="document-map-evidence",
        user_id=LEGACY_OWNER_USER_ID,
        person_id=LEGACY_OWNER_USER_ID,
        current_attachment_present=True,
    )
    messages = [
        {"role": "system", "content": "Read-only map."},
        {
            "role": "user",
            "content": agent_runtime_module._ATTACHMENT_CHUNK_PREFIX + "\n" + _DOCUMENT_SENTINEL,
        },
    ]

    returned = await runtime._attachment_prepass_chat(  # noqa: SLF001
        context,
        messages,
        secondary_output_max_chars=3_200,
        tools=[],
        max_tokens=512,
        priority="background",
    )
    await scheduler.drain_shadow()

    assert returned is primary_result
    assert primary_calls == 1
    assert len(recorded) == 1
    assert recorded[0][0].workload is ModelWorkload.DOCUMENT_MAP
    assert recorded[0][1].structured_output == {"summary": _RESULT_SENTINEL}


def _one_shot_scheduler(monkeypatch: pytest.MonkeyPatch) -> SecondaryBrainScheduler:
    scheduler = SecondaryBrainScheduler(
        mode=SecondaryMode.ASSIST,
        allowed_workloads=frozenset({ModelWorkload.DOCUMENT_MAP}),
        allow_private_text=True,
        client=None,
        unavailable_state=SecondaryState.PROBING,
        profile_admission=SecondaryProfileAdmission.ACCEPTED,
        document_map_mode=SecondaryMode.SHADOW,
    )
    monkeypatch.setattr(
        scheduler,
        "workload_mode",
        lambda workload: (
            SecondaryMode.SHADOW if workload is ModelWorkload.DOCUMENT_MAP else SecondaryMode.ASSIST
        ),
    )
    return scheduler


@pytest.mark.asyncio
async def test_bodyless_one_shot_issues_exact_promotion_receipt_and_is_not_replayable(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        display_name="Owner",
        preset_key="owner",
    )
    scheduler = _one_shot_scheduler(monkeypatch)

    async def attempt(request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        assert shadow is True
        scheduler._selected_by_workload[request.workload] += 1  # noqa: SLF001
        return SecondaryAttempt.success(_result())

    monkeypatch.setattr(scheduler, "_attempt_unobserved", attempt)
    monkeypatch.setattr(evidence, "_server_identity", lambda *_args, **_kwargs: _identity())
    result = await evidence.run_document_map_shadow_one_shot(
        storage,
        owner_user_id=LEGACY_OWNER_USER_ID,
        settings=_shadow_settings(settings),
        secondary=scheduler,
        now=_NOW,
        attestation_id="8" * 32,
    )

    receipt_path = settings.state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME
    receipt_raw = receipt_path.read_bytes()
    receipt = json.loads(receipt_raw)
    attestation = receipt["server_rollout_attestation"]
    assert result["status"] == "passed"
    assert result["receipt_sha256"] == hashlib.sha256(receipt_raw).hexdigest()
    assert attestation["observation_kind"] == "exclusive_owner_one_shot"
    assert attestation["scheduler_selected_delta"] == 1
    assert attestation["scheduler_success_delta"] == 1
    assert attestation["shadow_valid_delta"] == 1
    assert attestation["shadow_invalid_delta"] == 0
    assert attestation["shadow_skipped_delta"] == 0
    assert _RESULT_SENTINEL.encode() not in receipt_raw
    assert hashlib.sha256(_RESULT_SENTINEL.encode()).hexdigest().encode() not in receipt_raw
    with pytest.raises(evidence.DocumentMapShadowOneShotReplayError):
        await evidence.run_document_map_shadow_one_shot(
            storage,
            owner_user_id=LEGACY_OWNER_USER_ID,
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW + 1,
        )


@pytest.mark.asyncio
async def test_one_shot_and_consume_terminal_states_survive_connection_restart(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.storage import init_storage

    scheduler = _one_shot_scheduler(monkeypatch)

    async def attempt(request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        scheduler._selected_by_workload[request.workload] += 1  # noqa: SLF001
        return SecondaryAttempt.success(_result())

    monkeypatch.setattr(scheduler, "_attempt_unobserved", attempt)
    monkeypatch.setattr(evidence, "_server_identity", lambda *_args, **_kwargs: _identity())
    first = init_storage(settings)
    try:
        first.ensure_user(
            LEGACY_OWNER_USER_ID,
            source="api-token",
            display_name="Owner",
            preset_key="owner",
        )
        result = await evidence.run_document_map_shadow_one_shot(
            first,
            owner_user_id=LEGACY_OWNER_USER_ID,
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW,
            attestation_id="8" * 32,
        )
        receipt = json.loads(
            (settings.state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME).read_text(encoding="utf-8")
        )
        consume_request = _consume_request(receipt, result["receipt_sha256"])
        consumed = evidence.consume_document_map_shadow_rollout_attestation(
            first,
            LEGACY_OWNER_USER_ID,
            request_value=consume_request,
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW + 1,
        )
        assert consumed["status"] == "consumed"
        assert first.conn.in_transaction is False
    finally:
        first.close()
    second = init_storage(settings)
    try:
        latch = second.execute(
            "SELECT response_json FROM request_idempotency WHERE request_key LIKE ?",
            ("secondary-document-map-shadow-one-shot:%",),
        ).fetchone()
        receipt_state = second.execute(
            "SELECT response_json FROM request_idempotency WHERE request_key LIKE ?",
            ("secondary-document-map-shadow:%",),
        ).fetchone()
        assert latch is not None and json.loads(latch["response_json"])["status"] == "consumed"
        assert receipt_state is not None
        assert json.loads(receipt_state["response_json"])["rollout_consume_state"] == "consumed"
        retried = evidence.consume_document_map_shadow_rollout_attestation(
            second,
            LEGACY_OWNER_USER_ID,
            request_value=consume_request,
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW + 20,
        )
        assert retried == consumed
        assert second.conn.in_transaction is False
    finally:
        second.close()


@pytest.mark.asyncio
async def test_later_natural_shadow_cannot_replace_consumed_promotion_receipt(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        display_name="Owner",
        preset_key="owner",
    )
    scheduler = _one_shot_scheduler(monkeypatch)

    async def attempt(request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        scheduler._selected_by_workload[request.workload] += 1  # noqa: SLF001
        return SecondaryAttempt.success(_result())

    monkeypatch.setattr(scheduler, "_attempt_unobserved", attempt)
    monkeypatch.setattr(evidence, "_server_identity", lambda *_args, **_kwargs: _identity())
    result = await evidence.run_document_map_shadow_one_shot(
        storage,
        owner_user_id=LEGACY_OWNER_USER_ID,
        settings=_shadow_settings(settings),
        secondary=scheduler,
        now=_NOW,
        attestation_id="8" * 32,
    )
    receipt_path = settings.state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    evidence.consume_document_map_shadow_rollout_attestation(
        storage,
        LEGACY_OWNER_USER_ID,
        request_value=_consume_request(receipt, result["receipt_sha256"]),
        settings=_shadow_settings(settings),
        secondary=scheduler,
        now=_NOW + 1,
    )
    original_file = receipt_path.read_bytes()
    original_row = storage.execute(
        "SELECT request_key,response_json FROM request_idempotency WHERE request_key LIKE ?",
        ("secondary-document-map-shadow:%",),
    ).fetchone()
    assert original_row is not None

    returned_path, returned_sha256 = evidence.record_document_map_shadow_result(
        storage,
        owner_user_id=LEGACY_OWNER_USER_ID,
        request=replace(
            _request(),
            messages=(
                {"role": "system", "content": "later natural map"},
                {"role": "user", "content": "another private body"},
            ),
        ),
        result=replace(_result(), structured_output={"summary": "later natural summary"}),
        settings=_shadow_settings(settings),
        secondary=scheduler,
        now=_NOW + 2,
    )
    final_rows = storage.execute(
        "SELECT request_key,response_json FROM request_idempotency WHERE request_key LIKE ?",
        ("secondary-document-map-shadow:%",),
    ).fetchall()
    assert returned_path == receipt_path
    assert returned_sha256 == result["receipt_sha256"]
    assert receipt_path.read_bytes() == original_file
    assert len(final_rows) == 1
    assert final_rows[0]["request_key"] == original_row["request_key"]
    assert final_rows[0]["response_json"] == original_row["response_json"]


@pytest.mark.asyncio
async def test_new_process_can_issue_again_without_mutating_consumed_audit(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        display_name="Owner",
        preset_key="owner",
    )
    scheduler = _one_shot_scheduler(monkeypatch)

    async def attempt(request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        scheduler._selected_by_workload[request.workload] += 1  # noqa: SLF001
        return SecondaryAttempt.success(_result())

    current_identity = _identity()
    monkeypatch.setattr(scheduler, "_attempt_unobserved", attempt)
    monkeypatch.setattr(
        evidence,
        "_server_identity",
        lambda *_args, **_kwargs: current_identity,
    )
    first = await evidence.run_document_map_shadow_one_shot(
        storage,
        owner_user_id=LEGACY_OWNER_USER_ID,
        settings=_shadow_settings(settings),
        secondary=scheduler,
        now=_NOW,
        attestation_id="8" * 32,
    )
    receipt_path = settings.state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME
    first_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    evidence.consume_document_map_shadow_rollout_attestation(
        storage,
        LEGACY_OWNER_USER_ID,
        request_value=_consume_request(first_receipt, first["receipt_sha256"]),
        settings=_shadow_settings(settings),
        secondary=scheduler,
        now=_NOW + 1,
    )
    first_key = f"secondary-document-map-shadow:{'8' * 32}"
    first_row = storage.execute(
        "SELECT response_json FROM request_idempotency WHERE user_id=? AND request_key=?",
        (LEGACY_OWNER_USER_ID, first_key),
    ).fetchone()
    assert first_row is not None
    immutable_consumed_json = str(first_row["response_json"])

    current_identity = _identity(pid=4243, process_epoch="8" * 64)
    second = await evidence.run_document_map_shadow_one_shot(
        storage,
        owner_user_id=LEGACY_OWNER_USER_ID,
        settings=_shadow_settings(settings),
        secondary=scheduler,
        now=_NOW + 10,
        attestation_id="9" * 32,
    )
    second_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    rows = storage.execute(
        "SELECT request_key,response_json FROM request_idempotency WHERE request_key LIKE ?",
        ("secondary-document-map-shadow:%",),
    ).fetchall()
    assert len(rows) == 2
    assert next(row for row in rows if row["request_key"] == first_key)["response_json"] == (
        immutable_consumed_json
    )
    assert sum(json.loads(row["response_json"])["rollout_consume_state"] == "unused" for row in rows) == 1
    assert second["receipt_sha256"] != first["receipt_sha256"]
    assert second_receipt["server_rollout_attestation"]["primary_pid"] == 4243

    evidence.consume_document_map_shadow_rollout_attestation(
        storage,
        LEGACY_OWNER_USER_ID,
        request_value=_consume_request(second_receipt, second["receipt_sha256"]),
        settings=_shadow_settings(settings),
        secondary=scheduler,
        now=_NOW + 11,
    )
    final_rows = storage.execute(
        "SELECT request_key,response_json FROM request_idempotency WHERE request_key LIKE ?",
        ("secondary-document-map-shadow:%",),
    ).fetchall()
    assert len(final_rows) == 2
    assert next(row for row in final_rows if row["request_key"] == first_key)["response_json"] == (
        immutable_consumed_json
    )
    assert all(json.loads(row["response_json"])["rollout_consume_state"] == "consumed" for row in final_rows)


@pytest.mark.asyncio
async def test_idempotency_prune_preserves_every_witness_terminal_and_receipt(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        display_name="Owner",
        preset_key="owner",
    )
    scheduler = _one_shot_scheduler(monkeypatch)
    current_identity = _identity()

    async def success(request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        scheduler._selected_by_workload[request.workload] += 1  # noqa: SLF001
        return SecondaryAttempt.success(_result())

    async def failure(_request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        return SecondaryAttempt.rejected(SecondaryFailure.TIMEOUT)

    monkeypatch.setattr(scheduler, "_attempt_unobserved", success)
    monkeypatch.setattr(
        evidence,
        "_server_identity",
        lambda *_args, **_kwargs: current_identity,
    )
    consumed_result = await evidence.run_document_map_shadow_one_shot(
        storage,
        owner_user_id=LEGACY_OWNER_USER_ID,
        settings=_shadow_settings(settings),
        secondary=scheduler,
        now=_NOW,
        attestation_id="8" * 32,
    )
    consumed_receipt = json.loads(
        (settings.state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME).read_text(encoding="utf-8")
    )
    with storage.transaction() as connection:
        connection.execute(
            "UPDATE request_idempotency SET created_at='2000-01-01',updated_at='2000-01-01'",
        )
    assert storage.idempotency_prune(days=1) == 0
    evidence.consume_document_map_shadow_rollout_attestation(
        storage,
        LEGACY_OWNER_USER_ID,
        request_value=_consume_request(consumed_receipt, consumed_result["receipt_sha256"]),
        settings=_shadow_settings(settings),
        secondary=scheduler,
        now=_NOW + 1,
    )

    current_identity = _identity(pid=4243, process_epoch="3" * 64)
    monkeypatch.setattr(scheduler, "_attempt_unobserved", failure)
    with pytest.raises(evidence.DocumentMapShadowOneShotUnavailable):
        await evidence.run_document_map_shadow_one_shot(
            storage,
            owner_user_id=LEGACY_OWNER_USER_ID,
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW + 2,
        )

    started_identity = _identity(pid=4244, process_epoch="4" * 64)
    evidence._one_shot_claim(  # noqa: SLF001
        storage,
        owner_user_id=LEGACY_OWNER_USER_ID,
        identity_sha256=secondary_product_sha256(started_identity),
        now=_NOW + 3,
    )

    current_identity = _identity(pid=4245, process_epoch="5" * 64)
    monkeypatch.setattr(scheduler, "_attempt_unobserved", success)
    await evidence.run_document_map_shadow_one_shot(
        storage,
        owner_user_id=LEGACY_OWNER_USER_ID,
        settings=_shadow_settings(settings),
        secondary=scheduler,
        now=_NOW + 4,
        attestation_id="9" * 32,
    )
    with storage.transaction() as connection:
        connection.execute(
            "UPDATE request_idempotency SET created_at='2000-01-01',updated_at='2000-01-01'",
        )
    before = storage.execute(
        "SELECT request_key,response_json FROM request_idempotency ORDER BY request_key"
    ).fetchall()
    assert storage.idempotency_prune(days=1) == 0
    after = storage.execute(
        "SELECT request_key,response_json FROM request_idempotency ORDER BY request_key"
    ).fetchall()
    assert [(row["request_key"], row["response_json"]) for row in after] == [
        (row["request_key"], row["response_json"]) for row in before
    ]
    latch_states = {
        json.loads(row["response_json"])["status"]
        for row in after
        if str(row["request_key"]).startswith("secondary-document-map-shadow-one-shot:")
    }
    receipt_states = {
        json.loads(row["response_json"])["rollout_consume_state"]
        for row in after
        if str(row["request_key"]).startswith("secondary-document-map-shadow:")
    }
    assert latch_states == {"started", "failed", "passed", "consumed"}
    assert receipt_states == {"unused", "consumed"}
    with pytest.raises(evidence.DocumentMapShadowOneShotReplayError):
        await evidence.run_document_map_shadow_one_shot(
            storage,
            owner_user_id=LEGACY_OWNER_USER_ID,
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW + 5,
        )


@pytest.mark.asyncio
async def test_failed_canary_is_one_shot_and_never_issues_a_receipt(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        display_name="Owner",
        preset_key="owner",
    )
    scheduler = _one_shot_scheduler(monkeypatch)

    async def failed(_request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        assert shadow is True
        return SecondaryAttempt.rejected(SecondaryFailure.TIMEOUT)

    monkeypatch.setattr(scheduler, "_attempt_unobserved", failed)
    monkeypatch.setattr(evidence, "_server_identity", lambda *_args, **_kwargs: _identity())
    with pytest.raises(evidence.DocumentMapShadowOneShotUnavailable):
        await evidence.run_document_map_shadow_one_shot(
            storage,
            owner_user_id=LEGACY_OWNER_USER_ID,
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW,
        )
    assert not (settings.state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME).exists()
    tombstone = storage.execute(
        "SELECT response_json FROM request_idempotency WHERE request_key LIKE ?",
        ("secondary-document-map-shadow-one-shot:%",),
    ).fetchone()
    assert tombstone is not None
    assert json.loads(tombstone["response_json"])["status"] == "failed"
    with pytest.raises(evidence.DocumentMapShadowOneShotReplayError):
        await evidence.run_document_map_shadow_one_shot(
            storage,
            owner_user_id=LEGACY_OWNER_USER_ID,
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW + 1,
        )


@pytest.mark.asyncio
async def test_terminal_failure_leaves_issued_receipt_non_consumable(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        display_name="Owner",
        preset_key="owner",
    )
    scheduler = _one_shot_scheduler(monkeypatch)

    async def attempt(request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        scheduler._selected_by_workload[request.workload] += 1  # noqa: SLF001
        return SecondaryAttempt.success(_result())

    original_finish = evidence._finish_one_shot  # noqa: SLF001

    def fail_passed(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("status") == "passed":
            raise RuntimeError("terminal commit failed")
        original_finish(*args, **kwargs)

    monkeypatch.setattr(scheduler, "_attempt_unobserved", attempt)
    monkeypatch.setattr(evidence, "_server_identity", lambda *_args, **_kwargs: _identity())
    monkeypatch.setattr(evidence, "_finish_one_shot", fail_passed)
    with pytest.raises(RuntimeError, match="terminal commit failed"):
        await evidence.run_document_map_shadow_one_shot(
            storage,
            owner_user_id=LEGACY_OWNER_USER_ID,
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW,
            attestation_id="8" * 32,
        )
    receipt_path = settings.state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME
    receipt_raw = receipt_path.read_bytes()
    receipt = json.loads(receipt_raw)
    with pytest.raises(ValueError, match="did not pass durably"):
        evidence.consume_document_map_shadow_rollout_attestation(
            storage,
            LEGACY_OWNER_USER_ID,
            request_value=_consume_request(receipt, hashlib.sha256(receipt_raw).hexdigest()),
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW + 1,
        )
    latch = storage.execute(
        "SELECT response_json FROM request_idempotency WHERE request_key LIKE ?",
        ("secondary-document-map-shadow-one-shot:%",),
    ).fetchone()
    assert latch is not None and json.loads(latch["response_json"])["status"] == "failed"


@pytest.mark.asyncio
async def test_release_tree_is_strictly_reverified_before_receipt_issuance(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        display_name="Owner",
        preset_key="owner",
    )
    scheduler = _one_shot_scheduler(monkeypatch)

    async def attempt(request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        scheduler._selected_by_workload[request.workload] += 1  # noqa: SLF001
        return SecondaryAttempt.success(_result())

    strict_calls: list[bool] = []

    def identity(*_args: Any, verify_release_tree: bool = False, **_kwargs: Any) -> dict[str, Any]:
        strict_calls.append(verify_release_tree)
        if len(strict_calls) == 2:
            raise ValueError("sealed tree drifted")
        return _identity()

    monkeypatch.setattr(scheduler, "_attempt_unobserved", attempt)
    monkeypatch.setattr(evidence, "_server_identity", identity)
    with pytest.raises(ValueError, match="sealed tree drifted"):
        await evidence.run_document_map_shadow_one_shot(
            storage,
            owner_user_id=LEGACY_OWNER_USER_ID,
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW,
        )
    assert strict_calls == [True, True]
    assert not (settings.state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME).exists()


@pytest.mark.asyncio
async def test_exclusive_observer_failure_propagates_and_natural_observer_failure_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _one_shot_scheduler(monkeypatch)

    async def attempt(request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        scheduler._selected_by_workload[request.workload] += 1  # noqa: SLF001
        return SecondaryAttempt.success(_result())

    async def primary() -> object:
        return object()

    async def fail(_request: ModelRequest, _result: SecondaryResult) -> None:
        raise RuntimeError("receipt write failed")

    monkeypatch.setattr(scheduler, "_attempt_unobserved", attempt)
    with pytest.raises(RuntimeError, match="receipt write failed"):
        await scheduler.run_shadow_observed(
            _request,
            primary,
            validator=lambda _value: True,
            valid_result_observer=fail,
            exclusive=True,
        )


@pytest.mark.asyncio
async def test_busy_scheduler_fails_one_shot_without_queueing_product_traffic(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage.ensure_user(
        LEGACY_OWNER_USER_ID,
        source="api-token",
        display_name="Owner",
        preset_key="owner",
    )
    scheduler = _one_shot_scheduler(monkeypatch)
    scheduler._ordinary_attempts_in_flight = 1  # noqa: SLF001
    monkeypatch.setattr(evidence, "_server_identity", lambda *_args, **_kwargs: _identity())
    with pytest.raises(RuntimeError, match="not idle"):
        await evidence.run_document_map_shadow_one_shot(
            storage,
            owner_user_id=LEGACY_OWNER_USER_ID,
            settings=_shadow_settings(settings),
            secondary=scheduler,
            now=_NOW,
        )
    assert not (settings.state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME).exists()


def test_receipt_bytes_are_canonical_and_do_not_accept_duplicate_key_tampering(
    settings: Any,
    storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _digest, receipt, _shadow_settings_value = _record(storage, settings, monkeypatch)
    assert path.read_bytes() == secondary_product_canonical(receipt)
    duplicate = path.read_bytes()[:-2] + b',"status":"passed"}\n'
    assert duplicate != secondary_product_canonical(json.loads(duplicate))


def test_owner_only_document_map_routes_are_registered() -> None:
    from friday.admin_api._inbox import router

    registered = {(route.path, frozenset(route.methods or ())) for route in router.routes}
    assert (
        "/secondary-document-map-witness/consume-rollout-attestation",
        frozenset({"POST"}),
    ) in registered
    assert (
        "/secondary-document-map-witness/observe-shadow",
        frozenset({"POST"}),
    ) in registered


def test_one_shot_route_is_owner_token_only_and_rejects_every_request_body(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.admin_api._inbox as inbox_api
    from friday.server import create_app

    calls = 0

    async def observed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {
            "schema": evidence.DOCUMENT_MAP_SHADOW_ONE_SHOT_RESPONSE_SCHEMA,
            "status": "passed",
            "receipt_sha256": "a" * 64,
            "server_rollout_attestation_sha256": "b" * 64,
        }

    monkeypatch.setattr(inbox_api, "run_document_map_shadow_one_shot", observed)
    app = create_app(replace(settings, workers_enabled=False))
    owner = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        unauthorized = client.post(
            "/api/admin/secondary-document-map-witness/observe-shadow",
            content=b"",
        )
        assert unauthorized.status_code in {401, 403}
        body = client.post(
            "/api/admin/secondary-document-map-witness/observe-shadow",
            headers=owner,
            json={"user_text": _DOCUMENT_SENTINEL},
        )
        assert body.status_code == 400
        assert calls == 0
        accepted = client.post(
            "/api/admin/secondary-document-map-witness/observe-shadow",
            headers=owner,
            content=b"",
        )
        assert accepted.status_code == 200
        assert calls == 1


def test_one_shot_route_fails_closed_on_receipt_failure_and_burns_attempt(
    settings: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from friday.server import create_app

    scheduler = _one_shot_scheduler(monkeypatch)

    async def attempt(request: ModelRequest, *, shadow: bool = False) -> SecondaryAttempt:
        scheduler._selected_by_workload[request.workload] += 1  # noqa: SLF001
        return SecondaryAttempt.success(_result())

    monkeypatch.setattr(scheduler, "_attempt_unobserved", attempt)
    monkeypatch.setattr(evidence, "_server_identity", lambda *_args, **_kwargs: _identity())
    monkeypatch.setattr(
        evidence,
        "record_document_map_shadow_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("receipt write failed")),
    )
    app = create_app(_shadow_settings(replace(settings, workers_enabled=False)))
    owner = {"Authorization": f"Bearer {settings.api_token}"}
    with TestClient(app) as client:
        app.state.secondary_brain = scheduler
        failed = client.post(
            "/api/admin/secondary-document-map-witness/observe-shadow",
            headers=owner,
            content=b"",
        )
        assert failed.status_code == 503
        replay = client.post(
            "/api/admin/secondary-document-map-witness/observe-shadow",
            headers=owner,
            content=b"",
        )
        assert replay.status_code == 409
        tombstone = app.state.storage.execute(
            "SELECT response_json FROM request_idempotency WHERE request_key LIKE ?",
            ("secondary-document-map-shadow-one-shot:%",),
        ).fetchone()
        assert tombstone is not None
        assert json.loads(tombstone["response_json"])["status"] == "failed"
    assert not (settings.state_dir / evidence.DOCUMENT_MAP_SHADOW_RECEIPT_FILENAME).exists()
