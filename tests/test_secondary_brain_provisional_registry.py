from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from friday.secondary_brain.profiles import (
    ACCEPTED_SECONDARY_RUNTIME_PROFILES,
    PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES,
    SECONDARY_WORKLOAD_POLICIES,
    SecondaryProfileAdmission,
    SecondaryRuntimeAdmission,
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
    assert PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES == {}

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
    (policy,) = SECONDARY_WORKLOAD_POLICIES.values()
    raw = _DOCUMENT_MAP_POLICY_PATH.read_bytes()

    assert policy.is_well_formed is True
    assert policy.accepts_manifest(raw) is True
    assert policy.runtime_profile_id == profile.profile_id
    assert policy.runtime_profile_manifest_sha256 == profile.manifest_sha256 == _ACCEPTED_SHA256
    assert secondary_effective_workloads(profile, global_mode="shadow") == frozenset({"extract"})
    assert secondary_effective_workloads(profile, global_mode="assist") == frozenset(
        {"document_map", "extract"}
    )
    assert policy.accepts_manifest(raw + b" ") is False
