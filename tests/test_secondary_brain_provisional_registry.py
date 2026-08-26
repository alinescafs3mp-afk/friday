from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import friday.secondary_brain.contracts as secondary_contracts
from friday.secondary_brain.contracts import SecondaryEndpointConfig
from friday.secondary_brain.profiles import (
    ACCEPTED_SECONDARY_RUNTIME_PROFILES,
    PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES,
    SECONDARY_DOCUMENT_MAP_ASSIST_ACCEPTANCE,
    SECONDARY_WORKLOAD_POLICIES,
    SecondaryProfileAdmission,
    SecondaryRuntimeAdmission,
    get_secondary_document_map_assist_acceptance,
    get_secondary_runtime_admission,
    get_secondary_runtime_profile,
    secondary_effective_workloads,
)

_PROFILE_ID = "gptoss20b-2335df123cac7fc0e13e347cde1e1ffa8562daafcaf0fc76ade1a851d2b0ff1f"
_ACCEPTED_SHA256 = "93ea5698b8b6a9bf8a7dc697ffe37d7353055aa16555188991747bba73d059e3"
_CANDIDATE_SHA256 = "51af2164fa07ff3c01813e318076f7ac8b37eeecb73e695b6ca7543061c93439"
_ACCEPTED_PATH = Path(__file__).parent / "fixtures" / "secondary_finalist_profile_accepted.json"
_CANDIDATE_PATH = Path(__file__).parent / "fixtures" / "secondary_finalist_profile_candidate.json"
_DOCUMENT_MAP_POLICY_PATH = (
    Path(__file__).parents[1]
    / "deploy"
    / "secondary-brain"
    / "windows-sglang"
    / "workload-policy.document-map.v1.json"
)
_DOCUMENT_MAP_ASSIST_POLICY_PATH = (
    Path(__file__).parents[1]
    / "deploy"
    / "secondary-brain"
    / "windows-sglang"
    / "workload-policy.document-map.v2.json"
)
_DOCUMENT_MAP_ASSIST_PENDING_PATH = (
    Path(__file__).parents[1]
    / "deploy"
    / "secondary-brain"
    / "windows-sglang"
    / "workload-policy.document-map.v2.acceptance-pending.json"
)
_DOCUMENT_MAP_ASSIST_ACCEPTED_PATH = (
    Path(__file__).parents[1]
    / "deploy"
    / "secondary-brain"
    / "windows-sglang"
    / "workload-policy.document-map.v2.acceptance.json"
)
_ABLITERATED_PROFILE_ID = "gptoss20b-d4c2207151c7507f9d71a1d3d5d387d6ae98bb89b04f3171ba667098c2ad2d25"
_ABLITERATED_CANDIDATE_SHA256 = "612ed412143458fc32bcee2b78cfa66afdaec0f947b7c6b78422afa6d9fd5a64"
_ABLITERATED_CANDIDATE_PATH = (
    Path(__file__).parent / "fixtures" / "secondary_abliterated_profile_candidate.json"
)
_EVIDENCE_KEYS = (
    "quality_evidence_sha256",
    "capacity_evidence_sha256",
    "soak_evidence_sha256",
    "failure_evidence_sha256",
)


def test_finalist_is_registered_only_as_the_exact_accepted_profile() -> None:
    raw = _ACCEPTED_PATH.read_bytes()
    accepted = json.loads(raw)
    candidate_raw = _CANDIDATE_PATH.read_bytes()

    assert hashlib.sha256(raw).hexdigest() == _ACCEPTED_SHA256
    assert hashlib.sha256(candidate_raw).hexdigest() == _CANDIDATE_SHA256
    assert set(ACCEPTED_SECONDARY_RUNTIME_PROFILES) == {_PROFILE_ID}
    assert set(PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES) == {_ABLITERATED_PROFILE_ID}
    assert set(ACCEPTED_SECONDARY_RUNTIME_PROFILES).isdisjoint(PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES)

    profile = ACCEPTED_SECONDARY_RUNTIME_PROFILES[_PROFILE_ID]
    assert profile.is_well_formed is True
    assert profile.manifest_sha256 == _ACCEPTED_SHA256
    assert profile.allowed_modes == frozenset({"assist", "shadow"})
    assert profile.allowed_workloads == frozenset({"extract"})
    assert profile.chunked_prefill_size == 256
    assert profile.cuda_graph_backend_decode == "full"
    assert profile.cuda_graph_max_bs_decode == 1
    assert profile.cuda_graph_bs_decode == (1,)
    assert accepted["status"] == "accepted"
    assert all(accepted[key] != "0" * 64 for key in _EVIDENCE_KEYS)

    assert get_secondary_runtime_profile(_PROFILE_ID) is profile
    for mode in ("shadow", "assist"):
        admission = get_secondary_runtime_admission(_PROFILE_ID, mode=mode)
        assert admission == SecondaryRuntimeAdmission(
            profile=profile,
            kind=SecondaryProfileAdmission.ACCEPTED,
        )
        assert admission.accepts_manifest(raw) is True
    assert profile.accepts_manifest(raw) is True
    assert profile.accepts_manifest(candidate_raw) is False


def test_finalist_accepted_admission_rejects_manifest_lookalikes() -> None:
    raw = _ACCEPTED_PATH.read_bytes()
    profile = ACCEPTED_SECONDARY_RUNTIME_PROFILES[_PROFILE_ID]
    admission = SecondaryRuntimeAdmission(
        profile=profile,
        kind=SecondaryProfileAdmission.ACCEPTED,
    )

    for mutated in (
        raw + b" ",
        raw.replace(b'"status":"accepted"', b'"status":"candidate"'),
        raw.replace(
            b'"quality_evidence_sha256":"7',
            b'"quality_evidence_sha256":"6',
        ),
    ):
        assert admission.accepts_manifest(mutated) is False

    candidate_raw = _CANDIDATE_PATH.read_bytes()
    candidate_lookalike = SecondaryRuntimeAdmission(
        profile=replace(profile, manifest_sha256=hashlib.sha256(candidate_raw).hexdigest()),
        kind=SecondaryProfileAdmission.ACCEPTED,
    )
    assert candidate_lookalike.accepts_manifest(candidate_raw) is False


def test_document_map_policy_extends_product_work_only_without_rebinding_windows_runtime() -> None:
    profile = ACCEPTED_SECONDARY_RUNTIME_PROFILES[_PROFILE_ID]
    policy = SECONDARY_WORKLOAD_POLICIES["gptoss20b-document-map-v1"]
    raw = _DOCUMENT_MAP_POLICY_PATH.read_bytes()

    assert policy.is_well_formed is True
    assert policy.accepts_manifest(raw) is True
    assert policy.runtime_profile_id == profile.profile_id
    assert policy.runtime_profile_manifest_sha256 == profile.manifest_sha256 == _ACCEPTED_SHA256
    assert policy.document_map_modes == frozenset({"shadow"})
    assert secondary_effective_workloads(
        profile,
        global_mode="shadow",
        document_map_mode="shadow",
    ) == frozenset({"extract"})
    assert secondary_effective_workloads(
        profile,
        global_mode="assist",
        document_map_mode="shadow",
    ) == frozenset({"document_map", "extract"})
    assert policy.accepts_manifest(raw + b" ") is False


def test_document_map_assist_policy_is_exact_and_evidence_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ACCEPTED_SECONDARY_RUNTIME_PROFILES[_PROFILE_ID]
    policy = SECONDARY_WORKLOAD_POLICIES["gptoss20b-document-map-v2"]
    raw = _DOCUMENT_MAP_ASSIST_POLICY_PATH.read_bytes()
    assert policy.is_well_formed is True
    assert policy.accepts_manifest(raw) is True
    assert policy.document_map_modes == frozenset({"assist"})
    assert policy.allowed_global_modes == frozenset({"assist"})
    assert policy.runtime_profile_id == profile.profile_id
    assert policy.runtime_profile_manifest_sha256 == profile.manifest_sha256
    assert policy.accepts_manifest(raw + b" ") is False
    monkeypatch.setattr(secondary_contracts, "_load_pinned_ca_pem", lambda *_args: "certificate")
    endpoint = SecondaryEndpointConfig(
        base_url=profile.endpoint_base_url,
        served_model_alias=profile.served_model_alias,
        api_key="a" * 64,
        ca_file="/private/friday-secondary-ca.pem",
        ca_sha256=profile.gateway_ca_certificate_sha256,
        max_context_tokens=profile.max_context_tokens,
        max_concurrency=profile.max_concurrency,
        max_output_tokens=profile.max_output_tokens,
        profile_id=profile.profile_id,
        profile_manifest_sha256=profile.manifest_sha256,
    )
    arguments = {
        "primary_base_url": "http://127.0.0.1:30000/v1",
        "primary_model": "primary-model",
        "primary_timeout_sec": 60.0,
        "workload_names": ("document_map", "extract"),
        "mode": "assist",
        "allow_private_text": True,
    }

    assert secondary_contracts.secondary_configuration_is_admissible(
        endpoint,
        **arguments,
        document_map_mode="shadow",
    )
    assert secondary_contracts.secondary_configuration_is_admissible(
        endpoint,
        **arguments,
        document_map_mode="assist",
    )


def test_document_map_assist_binding_preserves_pending_history_but_uses_exact_acceptance() -> None:
    profile = ACCEPTED_SECONDARY_RUNTIME_PROFILES[_PROFILE_ID]
    pending_raw = _DOCUMENT_MAP_ASSIST_PENDING_PATH.read_bytes()
    pending = json.loads(pending_raw)
    raw = _DOCUMENT_MAP_ASSIST_ACCEPTED_PATH.read_bytes()
    value = json.loads(raw)

    assert (
        hashlib.sha256(raw).hexdigest() == SECONDARY_DOCUMENT_MAP_ASSIST_ACCEPTANCE.acceptance_manifest_sha256
    )
    assert SECONDARY_DOCUMENT_MAP_ASSIST_ACCEPTANCE.accepts_manifest(raw) is True
    assert value["status"] == SECONDARY_DOCUMENT_MAP_ASSIST_ACCEPTANCE.status == "accepted"
    assert value["candidate_policy_manifest_sha256"] == (
        SECONDARY_WORKLOAD_POLICIES["gptoss20b-document-map-v2"].manifest_sha256
    )
    assert value["accepted_shadow_receipt_sha256"] == (
        "a00f18f8c50a7449d1fa6a357d8d5bb1ca37b0c397c81a96c0e621231bc09e2d"
    )
    assert pending["status"] == "acceptance_pending"
    assert pending["candidate_policy_manifest_sha256"] == ""
    assert pending["accepted_shadow_receipt_sha256"] == ""
    assert SECONDARY_DOCUMENT_MAP_ASSIST_ACCEPTANCE.is_bound is True
    assert get_secondary_document_map_assist_acceptance(profile) is SECONDARY_DOCUMENT_MAP_ASSIST_ACCEPTANCE
    assert SECONDARY_DOCUMENT_MAP_ASSIST_ACCEPTANCE.accepts_manifest(raw + b" ") is False


def test_exact_code_owned_acceptance_binding_is_the_only_runtime_assist_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import friday.secondary_brain.profiles as profiles

    profile = ACCEPTED_SECONDARY_RUNTIME_PROFILES[_PROFILE_ID]
    accepted = SECONDARY_DOCUMENT_MAP_ASSIST_ACCEPTANCE
    assert replace(accepted, candidate_policy_manifest_sha256="0" * 64).is_bound is False
    assert replace(accepted, accepted_shadow_receipt_sha256="0" * 64).is_bound is False
    monkeypatch.setattr(secondary_contracts, "_load_pinned_ca_pem", lambda *_args: "certificate")
    endpoint = SecondaryEndpointConfig(
        base_url=profile.endpoint_base_url,
        served_model_alias=profile.served_model_alias,
        api_key="a" * 64,
        ca_file="/private/friday-secondary-ca.pem",
        ca_sha256=profile.gateway_ca_certificate_sha256,
        max_context_tokens=profile.max_context_tokens,
        max_concurrency=profile.max_concurrency,
        max_output_tokens=profile.max_output_tokens,
        profile_id=profile.profile_id,
        profile_manifest_sha256=profile.manifest_sha256,
    )

    assert get_secondary_document_map_assist_acceptance(profile) == accepted
    assert secondary_contracts.secondary_configuration_is_admissible(
        endpoint,
        primary_base_url="http://127.0.0.1:30000/v1",
        primary_model="primary-model",
        primary_timeout_sec=60.0,
        workload_names=("document_map", "extract"),
        mode="assist",
        allow_private_text=True,
        document_map_mode="assist",
    )

    for policy_id, change in (
        ("gptoss20b-document-map-v1", {"manifest_sha256": "0" * 64}),
        ("gptoss20b-document-map-v1", {"document_map_modes": frozenset({"assist"})}),
        ("gptoss20b-document-map-v2", {"manifest_sha256": "0" * 64}),
        ("gptoss20b-document-map-v2", {"document_map_modes": frozenset({"shadow"})}),
    ):
        policy = SECONDARY_WORKLOAD_POLICIES[policy_id]
        monkeypatch.setattr(
            profiles,
            "SECONDARY_WORKLOAD_POLICIES",
            {**SECONDARY_WORKLOAD_POLICIES, policy_id: replace(policy, **change)},
        )
        assert get_secondary_document_map_assist_acceptance(profile) is None
        assert not secondary_contracts.secondary_configuration_is_admissible(
            endpoint,
            primary_base_url="http://127.0.0.1:30000/v1",
            primary_model="primary-model",
            primary_timeout_sec=60.0,
            workload_names=("document_map", "extract"),
            mode="assist",
            allow_private_text=True,
            document_map_mode="assist",
        )
        monkeypatch.setattr(profiles, "SECONDARY_WORKLOAD_POLICIES", SECONDARY_WORKLOAD_POLICIES)

    monkeypatch.setattr(
        profiles,
        "SECONDARY_DOCUMENT_MAP_ASSIST_ACCEPTANCE",
        replace(accepted, accepted_shadow_receipt_sha256="0" * 64),
    )
    assert get_secondary_document_map_assist_acceptance(profile) is None
    assert not secondary_contracts.secondary_configuration_is_admissible(
        endpoint,
        primary_base_url="http://127.0.0.1:30000/v1",
        primary_model="primary-model",
        primary_timeout_sec=60.0,
        workload_names=("document_map", "extract"),
        mode="assist",
        allow_private_text=True,
        document_map_mode="assist",
    )


def test_abliterated_candidate_is_exact_provisional_shadow_only() -> None:
    raw = _ABLITERATED_CANDIDATE_PATH.read_bytes()
    value = json.loads(raw)

    assert hashlib.sha256(raw).hexdigest() == _ABLITERATED_CANDIDATE_SHA256
    assert value["status"] == "candidate"
    assert value["profile_id"] == _ABLITERATED_PROFILE_ID
    assert value["engine_binding_sha256"] == _ABLITERATED_PROFILE_ID.removeprefix("gptoss20b-")
    assert value["served_model_alias"] == f"friday-secondary-{_ABLITERATED_PROFILE_ID}"
    assert value["source_model_repository"] == ("huihui-ai/Huihui-gpt-oss-20b-mxfp4-abliterated-v2")
    assert value["source_model_revision"] == "79f64a520a4a0275f639c1a47d9a5614a8a54477"
    assert value["source_model_manifest_sha256"] == (
        "8dfc3a50d1a9407fbb07dde5f1b494157664c75cdd0e140ecb85f7d55732a296"
    )
    assert all(value[key] == "0" * 64 for key in _EVIDENCE_KEYS)

    profile = PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES[_ABLITERATED_PROFILE_ID]
    assert profile.is_well_formed is True
    assert profile.manifest_sha256 == _ABLITERATED_CANDIDATE_SHA256
    assert profile.allowed_modes == frozenset({"assist", "shadow"})
    assert profile.allowed_workloads == frozenset({"extract"})
    assert profile.cuda_graph_backend_decode == "full"
    assert profile.cuda_graph_max_bs_decode == 1
    assert profile.cuda_graph_bs_decode == (1,)
    assert get_secondary_runtime_profile(_ABLITERATED_PROFILE_ID) is None
    assert get_secondary_runtime_admission(_ABLITERATED_PROFILE_ID, mode="assist") is None

    admission = get_secondary_runtime_admission(_ABLITERATED_PROFILE_ID, mode="shadow")
    assert admission == SecondaryRuntimeAdmission(
        profile=profile,
        kind=SecondaryProfileAdmission.PROVISIONAL_SHADOW,
    )
    assert admission.accepts_manifest(raw) is True
    assert profile.accepts_manifest(raw) is False
    assert admission.accepts_manifest(raw + b" ") is False

    accepted_lookalike = raw.replace(b'"status":"candidate"', b'"status":"accepted"')
    lookalike = SecondaryRuntimeAdmission(
        profile=replace(
            profile,
            manifest_sha256=hashlib.sha256(accepted_lookalike).hexdigest(),
        ),
        kind=SecondaryProfileAdmission.PROVISIONAL_SHADOW,
    )
    assert lookalike.accepts_manifest(accepted_lookalike) is False
