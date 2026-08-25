from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import tools.immutable_release_operator as operator

_NOW = 1_800_000_000
_PREDECESSOR_METADATA_SHA256 = "a" * 64
_PREDECESSOR_WHEEL_SHA256 = "b" * 64
_PREDECESSOR_ENV_SHA256 = "c" * 64
_PREDECESSOR_ENV_PATH_SHA256 = "d" * 64
_PREDECESSOR_ANCHOR_PATH_SHA256 = "e" * 64


def _release(name: str, commit: str, *, version: str = "0.207.27") -> operator.ReleaseIdentity:
    return operator.ReleaseIdentity(
        root=Path("/private/releases") / name,
        commit=commit,
        version=version,
        tree_manifest_sha256=hashlib.sha256(name.encode()).hexdigest(),
        max_schema=42,
    )


def _profile_identity() -> dict[str, Any]:
    return {
        "admission": "accepted",
        "allow_private_text": True,
        "context_tokens": 4096,
        "gateway_ca_certificate_sha256": operator._SECONDARY_FINALIST_CA_SHA256,  # noqa: SLF001
        "manifest_sha256": "7" * 64,
        "mode": "assist",
        "profile_id": operator._SECONDARY_FINALIST_PROFILE_ID,  # noqa: SLF001
        "served_model_alias": operator._SECONDARY_FINALIST_MODEL_ALIAS,  # noqa: SLF001
    }


def _receipt() -> tuple[dict[str, Any], str]:
    lookup_token = "9" * 64
    attestation = {
        "schema": operator._SECONDARY_DOCUMENT_MAP_ATTESTATION_SCHEMA,  # noqa: SLF001
        "attestation_id": "1" * 32,
        "workload": "document_map",
        "routing_mode": "shadow",
        "shadow_policy_id": operator._SECONDARY_DOCUMENT_MAP_SHADOW_POLICY_ID,  # noqa: SLF001
        "shadow_policy_manifest_sha256": operator._SECONDARY_DOCUMENT_MAP_SHADOW_POLICY_SHA256,  # noqa: SLF001
        "observation_kind": "exclusive_owner_one_shot",
        "scheduler_selected_delta": 1,
        "scheduler_success_delta": 1,
        "shadow_valid_delta": 1,
        "shadow_invalid_delta": 0,
        "shadow_skipped_delta": 0,
        "shadow_in_flight_before": 0,
        "shadow_in_flight_after": 0,
        "observation_binding_sha256": "2" * 64,
        "owner_binding_sha256": "3" * 64,
        "primary_pid": 4242,
        "primary_process_epoch_sha256": "4" * 64,
        "primary_backend_version": "0.207.27",
        "primary_ca_certificate_sha256": "5" * 64,
        "predecessor_release_commit": "a" * 40,
        "predecessor_release_tree_manifest_sha256": hashlib.sha256(b"previous").hexdigest(),
        "predecessor_release_metadata_sha256": _PREDECESSOR_METADATA_SHA256,
        "predecessor_release_wheel_sha256": _PREDECESSOR_WHEEL_SHA256,
        "predecessor_live_env_sha256": _PREDECESSOR_ENV_SHA256,
        "predecessor_live_env_path_sha256": _PREDECESSOR_ENV_PATH_SHA256,
        "predecessor_release_anchor_path_sha256": _PREDECESSOR_ANCHOR_PATH_SHA256,
        "candidate_profile_id": operator._SECONDARY_FINALIST_PROFILE_ID,  # noqa: SLF001
        "candidate_profile_mode": "assist",
        "candidate_profile_allow_private_text": True,
        "candidate_profile_context_tokens": 4096,
        "candidate_profile_sha256": operator._SECONDARY_FINALIST_CANDIDATE_PROFILE_SHA256,  # noqa: SLF001
        "candidate_profile_manifest_sha256": "7" * 64,
        "candidate_profile_admission": "accepted",
        "served_model_alias": operator._SECONDARY_FINALIST_MODEL_ALIAS,  # noqa: SLF001
        "gateway_ca_certificate_sha256": operator._SECONDARY_FINALIST_CA_SHA256,  # noqa: SLF001
        "document_text_retained": False,
        "model_response_retained": False,
        "document_text_digest_retained": False,
        "model_response_digest_retained": False,
        "state_version": 1,
        "issued_at": _NOW,
        "expires_at": _NOW + 3_600,
        "lookup_token_sha256": hashlib.sha256(lookup_token.encode()).hexdigest(),
        "signature": "8" * 64,
    }
    receipt = {
        "schema": operator._SECONDARY_DOCUMENT_MAP_RECEIPT_SCHEMA,  # noqa: SLF001
        "status": "passed",
        "server_rollout_attestation": attestation,
        "server_rollout_attestation_sha256": hashlib.sha256(
            operator._secondary_product_canonical(attestation)  # noqa: SLF001
        ).hexdigest(),
        "server_rollout_lookup_token": lookup_token,
        "document_text_retained_in_evidence": False,
        "model_response_retained_in_evidence": False,
        "document_text_digest_retained_in_evidence": False,
        "model_response_digest_retained_in_evidence": False,
    }
    digest = hashlib.sha256(operator._secondary_product_canonical(receipt)).hexdigest()  # noqa: SLF001
    return receipt, digest


def _replace_attestation(
    receipt: dict[str, Any],
    changes: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    attestation = {**receipt["server_rollout_attestation"], **changes}
    changed = {
        **receipt,
        "server_rollout_attestation": attestation,
        "server_rollout_attestation_sha256": hashlib.sha256(
            operator._secondary_product_canonical(attestation)  # noqa: SLF001
        ).hexdigest(),
    }
    digest = hashlib.sha256(operator._secondary_product_canonical(changed)).hexdigest()  # noqa: SLF001
    return changed, digest


def _validate(receipt: dict[str, Any], digest: str) -> dict[str, Any]:
    return operator._validate_secondary_document_map_rollout_receipt(  # noqa: SLF001
        receipt,
        receipt_sha256=digest,
        previous=_release("previous", "a" * 40),
        predecessor_release_metadata_sha256=_PREDECESSOR_METADATA_SHA256,
        predecessor_release_wheel_sha256=_PREDECESSOR_WHEEL_SHA256,
        predecessor_live_env_sha256=_PREDECESSOR_ENV_SHA256,
        predecessor_live_env_path_sha256=_PREDECESSOR_ENV_PATH_SHA256,
        predecessor_release_anchor_path_sha256=_PREDECESSOR_ANCHOR_PATH_SHA256,
        profile_identity=_profile_identity(),
        primary_pid=4242,
        primary_process_epoch_sha256="4" * 64,
        primary_ca_certificate_sha256="5" * 64,
    )


def test_operator_pins_exact_policy_and_live_receipt_without_env_authority() -> None:
    assert operator._SECONDARY_DOCUMENT_MAP_ASSIST_POLICY_SHA256 == (  # noqa: SLF001
        "d2ab9b67ff24a54727fec9592dcd0db1c35036e1b5ee91ac6a5daf4d3694e92e"
    )
    assert operator._SECONDARY_DOCUMENT_MAP_ACCEPTED_SHADOW_RECEIPT_SHA256 == (  # noqa: SLF001
        "a00f18f8c50a7449d1fa6a357d8d5bb1ca37b0c397c81a96c0e621231bc09e2d"
    )
    receipt, digest = _receipt()
    with pytest.raises(operator.ReleaseFailure, match="secondary_document_map_shadow_receipt_not_accepted"):
        _validate(receipt, digest)


def test_operator_accepts_only_exact_fresh_identity_bound_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, digest = _receipt()
    monkeypatch.setattr(operator, "_SECONDARY_DOCUMENT_MAP_ASSIST_POLICY_SHA256", "a" * 64)
    monkeypatch.setattr(
        operator,
        "_SECONDARY_DOCUMENT_MAP_ACCEPTED_SHADOW_RECEIPT_SHA256",
        digest,
    )
    monkeypatch.setattr(operator.time, "time", lambda: float(_NOW + 1))

    assert _validate(receipt, digest) == receipt["server_rollout_attestation"]

    with pytest.raises(operator.ReleaseFailure, match="shadow_receipt_not_accepted"):
        _validate(receipt, "b" * 64)
    with pytest.raises(operator.ReleaseFailure, match="shadow_receipt_invalid"):
        operator._validate_secondary_document_map_rollout_receipt(  # noqa: SLF001
            receipt,
            receipt_sha256=digest,
            previous=_release("previous", "a" * 40),
            predecessor_release_metadata_sha256=_PREDECESSOR_METADATA_SHA256,
            predecessor_release_wheel_sha256=_PREDECESSOR_WHEEL_SHA256,
            predecessor_live_env_sha256=_PREDECESSOR_ENV_SHA256,
            predecessor_live_env_path_sha256=_PREDECESSOR_ENV_PATH_SHA256,
            predecessor_release_anchor_path_sha256=_PREDECESSOR_ANCHOR_PATH_SHA256,
            profile_identity=_profile_identity(),
            primary_pid=4243,
            primary_process_epoch_sha256="4" * 64,
            primary_ca_certificate_sha256="5" * 64,
        )
    monkeypatch.setattr(operator.time, "time", lambda: float(_NOW + 3_601))
    with pytest.raises(operator.ReleaseFailure, match="shadow_receipt_invalid"):
        _validate(receipt, digest)


@pytest.mark.parametrize(
    "changes",
    [
        {"observation_kind": "natural_scheduler_valid_result"},
        {
            "observation_kind": "natural_scheduler_valid_result",
            "scheduler_selected_delta": None,
            "scheduler_success_delta": None,
            "shadow_valid_delta": None,
            "shadow_invalid_delta": None,
            "shadow_skipped_delta": None,
            "shadow_in_flight_before": None,
            "shadow_in_flight_after": None,
        },
        {"scheduler_selected_delta": True},
        {"scheduler_success_delta": 0},
        {"shadow_valid_delta": 0},
        {"shadow_invalid_delta": 1},
        {"shadow_skipped_delta": 1},
        {"shadow_in_flight_before": 1},
        {"shadow_in_flight_after": 1},
        {"predecessor_release_commit": "b" * 40},
        {"predecessor_release_tree_manifest_sha256": "1" * 64},
        {"predecessor_release_metadata_sha256": "1" * 64},
        {"predecessor_release_wheel_sha256": "1" * 64},
        {"predecessor_live_env_sha256": "1" * 64},
        {"predecessor_live_env_path_sha256": "1" * 64},
        {"predecessor_release_anchor_path_sha256": "1" * 64},
    ],
)
def test_operator_rejects_nonexclusive_or_predecessor_drifted_receipt_even_when_digest_is_bound(
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
) -> None:
    receipt, _digest = _receipt()
    changed, changed_digest = _replace_attestation(receipt, changes)
    monkeypatch.setattr(operator, "_SECONDARY_DOCUMENT_MAP_ASSIST_POLICY_SHA256", "a" * 64)
    monkeypatch.setattr(
        operator,
        "_SECONDARY_DOCUMENT_MAP_ACCEPTED_SHADOW_RECEIPT_SHA256",
        changed_digest,
    )
    monkeypatch.setattr(operator.time, "time", lambda: float(_NOW + 1))

    with pytest.raises(operator.ReleaseFailure, match="shadow_receipt_invalid"):
        _validate(changed, changed_digest)


def test_operator_consume_request_requires_a_distinct_candidate_and_bound_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, digest = _receipt()
    monkeypatch.setattr(operator, "_SECONDARY_DOCUMENT_MAP_ASSIST_POLICY_SHA256", "a" * 64)
    monkeypatch.setattr(
        operator,
        "_SECONDARY_DOCUMENT_MAP_ACCEPTED_SHADOW_RECEIPT_SHA256",
        digest,
    )
    previous = _release("previous", "a" * 40)
    candidate = _release("candidate", "b" * 40)
    request = operator._secondary_document_map_rollout_consume_request(  # noqa: SLF001
        lookup_token=receipt["server_rollout_lookup_token"],
        attestation_sha256=receipt["server_rollout_attestation_sha256"],
        previous=previous,
        candidate=candidate,
        predecessor_env_sha256=_PREDECESSOR_ENV_SHA256,
        next_env_sha256="f" * 64,
        product_receipt_sha256=digest,
    )
    assert request["candidate_commit"] != request["predecessor_commit"]
    assert request["predecessor_env_sha256"] == _PREDECESSOR_ENV_SHA256
    assert request["accepted_shadow_receipt_sha256"] == digest
    assert request["candidate_policy_manifest_sha256"] == "a" * 64

    with pytest.raises(operator.ReleaseFailure, match="secondary_document_map_consume_request_invalid"):
        operator._secondary_document_map_rollout_consume_request(  # noqa: SLF001
            lookup_token=receipt["server_rollout_lookup_token"],
            attestation_sha256=receipt["server_rollout_attestation_sha256"],
            previous=previous,
            candidate=replace(candidate, commit=previous.commit),
            predecessor_env_sha256=_PREDECESSOR_ENV_SHA256,
            next_env_sha256="f" * 64,
            product_receipt_sha256=digest,
        )
    with pytest.raises(operator.ReleaseFailure, match="secondary_document_map_consume_request_invalid"):
        operator._secondary_document_map_rollout_consume_request(  # noqa: SLF001
            lookup_token=receipt["server_rollout_lookup_token"],
            attestation_sha256=receipt["server_rollout_attestation_sha256"],
            previous=previous,
            candidate=candidate,
            predecessor_env_sha256=_PREDECESSOR_ENV_SHA256,
            next_env_sha256=_PREDECESSOR_ENV_SHA256,
            product_receipt_sha256=digest,
        )


def test_operator_checks_full_predecessor_tree_before_and_after_consume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = _release("previous", "a" * 40)
    events: list[str] = []

    def verify_tree(release: operator.ReleaseIdentity) -> None:
        assert release is previous
        events.append("tree")

    monkeypatch.setattr(operator, "verify_release_tree", verify_tree)
    operator._consume_secondary_document_map_after_exact_rechecks(  # noqa: SLF001
        previous,
        recheck_identity=lambda: events.append("identity"),
        consume=lambda: events.append("consume"),
    )
    assert events == ["tree", "identity", "consume", "tree", "identity"]

    events.clear()

    def drift_after_consume(_release: operator.ReleaseIdentity) -> None:
        events.append("tree")
        if events.count("tree") == 2:
            raise operator.ReleaseFailure("release_tree_digest_mismatch")

    monkeypatch.setattr(operator, "verify_release_tree", drift_after_consume)
    with pytest.raises(operator.ReleaseFailure, match="release_tree_digest_mismatch"):
        operator._consume_secondary_document_map_after_exact_rechecks(  # noqa: SLF001
            previous,
            recheck_identity=lambda: events.append("identity"),
            consume=lambda: events.append("consume"),
        )
    assert events == ["tree", "identity", "consume", "tree"]


def test_operator_consume_response_exactly_echoes_predecessor_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt, digest = _receipt()
    monkeypatch.setattr(operator, "_SECONDARY_DOCUMENT_MAP_ASSIST_POLICY_SHA256", "a" * 64)
    monkeypatch.setattr(
        operator,
        "_SECONDARY_DOCUMENT_MAP_ACCEPTED_SHADOW_RECEIPT_SHA256",
        digest,
    )
    monkeypatch.setattr(operator.time, "time", lambda: float(_NOW + 2))
    request = operator._secondary_document_map_rollout_consume_request(  # noqa: SLF001
        lookup_token=receipt["server_rollout_lookup_token"],
        attestation_sha256=receipt["server_rollout_attestation_sha256"],
        previous=_release("previous", "a" * 40),
        candidate=_release("candidate", "b" * 40),
        predecessor_env_sha256=_PREDECESSOR_ENV_SHA256,
        next_env_sha256="f" * 64,
        product_receipt_sha256=digest,
    )
    response = {
        "schema": operator._SECONDARY_DOCUMENT_MAP_CONSUME_RESPONSE_SCHEMA,  # noqa: SLF001
        "status": "consumed",
        **{
            name: request[name]
            for name in operator._SECONDARY_DOCUMENT_MAP_CONSUME_RESPONSE_KEYS  # noqa: SLF001
            if name in request and name != "schema"
        },
        "lookup_token_sha256": receipt["server_rollout_attestation"]["lookup_token_sha256"],
        "request_sha256": hashlib.sha256(
            operator._secondary_product_canonical(request)  # noqa: SLF001
        ).hexdigest(),
        "consumed_at": _NOW + 2,
        "state_version": 2,
        "consume_binding_sha256": "6" * 64,
    }
    operator._validate_secondary_document_map_consume_response(  # noqa: SLF001
        response,
        request=request,
        attestation=receipt["server_rollout_attestation"],
    )

    with pytest.raises(operator.ReleaseFailure, match="secondary_document_map_consume_response_invalid"):
        operator._validate_secondary_document_map_consume_response(  # noqa: SLF001
            {**response, "predecessor_env_sha256": "1" * 64},
            request=request,
            attestation=receipt["server_rollout_attestation"],
        )
