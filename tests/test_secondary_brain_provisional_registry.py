from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from friday.secondary_brain.profiles import (
    ACCEPTED_SECONDARY_RUNTIME_PROFILES,
    PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES,
    SecondaryProfileAdmission,
    SecondaryRuntimeAdmission,
    get_secondary_runtime_admission,
    get_secondary_runtime_profile,
)

_PROFILE_ID = "gptoss20b-ce6c00ff988e35c97d7381bde47cfa56f6e89c3eeb879bf6e7ba5e0b4a9d81e3"
_CANDIDATE_SHA256 = "6607c9b865c8b1d89779327ac04ef7178b9b18f9d0daae198193b348955fb5cb"
_CANDIDATE_PATH = Path(__file__).parent / "fixtures" / "secondary_v9_profile_candidate.json"
_EVIDENCE_KEYS = (
    "quality_evidence_sha256",
    "capacity_evidence_sha256",
    "soak_evidence_sha256",
    "failure_evidence_sha256",
)


def test_v9_is_registered_only_as_the_exact_provisional_shadow_candidate() -> None:
    raw = _CANDIDATE_PATH.read_bytes()
    candidate = json.loads(raw)

    assert hashlib.sha256(raw).hexdigest() == _CANDIDATE_SHA256
    assert ACCEPTED_SECONDARY_RUNTIME_PROFILES == {}
    assert set(PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES) == {_PROFILE_ID}

    profile = PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES[_PROFILE_ID]
    assert profile.is_well_formed is True
    assert profile.manifest_sha256 == _CANDIDATE_SHA256
    assert profile.allowed_modes == frozenset({"assist", "shadow"})
    assert profile.allowed_workloads == frozenset({"extract"})
    assert candidate["status"] == "candidate"
    assert [candidate[key] for key in _EVIDENCE_KEYS] == ["0" * 64] * len(_EVIDENCE_KEYS)

    assert get_secondary_runtime_profile(_PROFILE_ID) is None
    assert get_secondary_runtime_admission(_PROFILE_ID, mode="assist") is None
    admission = get_secondary_runtime_admission(_PROFILE_ID, mode="shadow")
    assert admission == SecondaryRuntimeAdmission(
        profile=profile,
        kind=SecondaryProfileAdmission.PROVISIONAL_SHADOW,
    )
    assert admission.accepts_manifest(raw) is True
    assert profile.accepts_manifest(raw) is False


def test_v9_provisional_admission_rejects_candidate_lookalikes() -> None:
    raw = _CANDIDATE_PATH.read_bytes()
    profile = PROVISIONAL_SHADOW_SECONDARY_RUNTIME_PROFILES[_PROFILE_ID]

    for mutated in (
        raw + b" ",
        raw.replace(b'"status":"candidate"', b'"status":"accepted"'),
        raw.replace(
            b'"quality_evidence_sha256":"' + b"0" * 64,
            b'"quality_evidence_sha256":"' + b"1" * 64,
        ),
    ):
        admission = SecondaryRuntimeAdmission(
            profile=replace(profile, manifest_sha256=hashlib.sha256(mutated).hexdigest()),
            kind=SecondaryProfileAdmission.PROVISIONAL_SHADOW,
        )
        assert admission.accepts_manifest(mutated) is False
