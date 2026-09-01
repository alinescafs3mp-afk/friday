"""Exact-release binding for authenticated production read-only evidence."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
from dataclasses import replace
from pathlib import Path

import pytest

from friday.diagnostics.production_observation import (
    PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256,
)
from tools import exact_release_evidence as evidence

_RELEASE = evidence.ReleaseIdentity(
    source_commit="a" * 40,
    tree_sha256="b" * 64,
    wheel_sha256="c" * 64,
    database_schema=50,
)
_CHALLENGE = "d" * 64
_EPOCH = "e" * 64
_HEALTH_BEFORE = "f" * 64
_HEALTH_AFTER = "1" * 64


def _zero_counts(names: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(names, 0)


def _endpoint_payload() -> dict[str, object]:
    return {
        "schema": evidence.PRODUCTION_READ_ONLY_OBSERVATION_SCHEMA,
        "challenge_sha256": _CHALLENGE,
        "backend_process_epoch_sha256": _EPOCH,
        "backend_lease_owned": True,
        "database": {
            "schema_version": 50,
            "schema_attestation_sha256": (evidence.PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256),
            "integrity": "ok",
            "foreign_key_violations": 0,
        },
        "scheduled_work": {
            "missions": {
                **_zero_counts(
                    (
                        "proposed",
                        "ready",
                        "running",
                        "paused",
                        "blocked",
                        "completed",
                        "failed",
                        "cancelled",
                    )
                ),
                "ready": 3,
            },
            "mission_tasks": {
                **_zero_counts(
                    (
                        "pending",
                        "running",
                        "done",
                        "failed",
                        "skipped",
                        "uncertain",
                        "compensated",
                    )
                ),
                "pending": 5,
                "uncertain": 1,
            },
            "reminders": {
                **_zero_counts(("pending", "uncertain", "sent", "failed", "dismissed")),
                "sent": 7,
                "dismissed": 2,
            },
            "workers": {
                "present": 2,
                "missing": 0,
                "health_states": {
                    **_zero_counts(
                        (
                            "scheduled",
                            "running",
                            "ok",
                            "error",
                            "timeout",
                            "skipped",
                            "unknown",
                        )
                    ),
                    "ok": 2,
                },
            },
        },
        "hard_contradictions": 0,
    }


def _artifact_payload(
    endpoint: dict[str, object] | None = None,
    *,
    release: evidence.ReleaseIdentity = _RELEASE,
) -> dict[str, object]:
    response = endpoint if endpoint is not None else _endpoint_payload()
    response_raw = evidence.canonical_json_bytes(response)
    return {
        "schema": evidence.RELEASE_CAPTAIN_PRODUCTION_OBSERVATION_SCHEMA,
        "release": release.payload(),
        "release_binding_sha256": evidence.release_binding_sha256(release),
        "endpoint_response": response,
        "endpoint_response_sha256": hashlib.sha256(response_raw).hexdigest(),
        "challenge_sha256": _CHALLENGE,
        "backend_process_epoch_sha256": _EPOCH,
        "health_before_sha256": _HEALTH_BEFORE,
        "health_after_sha256": _HEALTH_AFTER,
    }


def _binding(
    endpoint: dict[str, object] | None = None,
) -> evidence.AuthenticatedProductionObservationBinding:
    raw = evidence.canonical_json_bytes(_artifact_payload(endpoint))
    return evidence.binding_from_release_captain_artifact(raw, expected_release=_RELEASE)


def test_exact_schema_attestation_matches_the_runtime_observer_contract() -> None:
    assert evidence.PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256 == (
        PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256
    )


def test_release_captain_artifact_derives_one_timestamp_free_body_free_bundle() -> None:
    binding = _binding()
    bundle = evidence.produce_production_observation_bundle(
        authenticated_binding=binding,
    )
    receipt = evidence.validate_production_observation_bundle(
        bundle,
        authenticated_binding=binding,
    )
    manifest = json.loads(bundle.manifest)

    assert receipt["result"] == bundle.result == "VERIFIED"
    assert receipt["check_ids"] == [
        "durable_scheduled_work.database_integrity",
        "durable_scheduled_work.schema_attestation",
        "durable_scheduled_work.service_health",
    ]
    assert receipt["checks"] == [
        {"check_id": check_id, "outcome": "PASSED"} for check_id in receipt["check_ids"]
    ]
    assert receipt["observation"] == binding.payload()
    assert manifest["$schema"] == evidence.PRODUCTION_OBSERVATION_MANIFEST_SCHEMA
    assert manifest["observation"]["artifact_schema"] == (evidence.PRODUCTION_OBSERVATION_RECEIPT_SCHEMA)
    assert bundle.receipt == evidence.canonical_json_bytes(receipt)
    assert bundle.manifest == evidence.canonical_json_bytes(manifest)

    rendered = (repr(binding) + bundle.receipt.decode() + bundle.manifest.decode()).casefold()
    for forbidden in (
        '"observed_at_utc"',
        '"owner_smoke"',
        '"proofs"',
        '"runner"',
        '"pid"',
        '"path"',
        '"url"',
        '"body"',
        '"prompt"',
        '"output"',
        '"argv"',
        '"scheduled_work"',
    ):
        assert forbidden not in rendered
    assert binding.endpoint_response not in bundle.receipt

    with pytest.raises(dataclasses.FrozenInstanceError):
        binding.challenge_sha256 = "3" * 64  # type: ignore[misc]


def test_validation_requires_the_same_exact_external_binding() -> None:
    binding = _binding()
    bundle = evidence.produce_production_observation_bundle(authenticated_binding=binding)
    drifted = replace(binding, health_after_sha256="3" * 64)

    with pytest.raises(
        evidence.ExactReleaseEvidenceError,
        match="production_observation_not_authenticated",
    ):
        evidence.validate_production_observation_bundle(
            bundle,
            authenticated_binding=drifted,
        )

    class ForgedBinding(evidence.AuthenticatedProductionObservationBinding):
        pass

    with pytest.raises(
        evidence.ExactReleaseEvidenceError,
        match="production_observation_not_authenticated",
    ):
        ForgedBinding(
            release=binding.release,
            endpoint_response=binding.endpoint_response,
            challenge_sha256=binding.challenge_sha256,
            backend_process_epoch_sha256=binding.backend_process_epoch_sha256,
            health_before_sha256=binding.health_before_sha256,
            health_after_sha256=binding.health_after_sha256,
        )

    other_release = replace(_RELEASE, source_commit="8" * 40)
    with pytest.raises(evidence.ExactReleaseEvidenceError):
        evidence.binding_from_release_captain_artifact(
            evidence.canonical_json_bytes(_artifact_payload()),
            expected_release=other_release,
        )


def test_public_api_has_no_caller_result_or_hash_claims_and_no_owner_smoke_substitute(
    tmp_path: Path,
) -> None:
    produce = inspect.signature(evidence.produce_production_observation_bundle)
    validate = inspect.signature(evidence.validate_production_observation_bundle)
    factory = inspect.signature(evidence.binding_from_release_captain_artifact)

    assert set(produce.parameters) == {"authenticated_binding", "journey_id"}
    assert set(validate.parameters) == {
        "bundle",
        "authenticated_binding",
        "expected_journey_id",
    }
    assert set(factory.parameters) == {"raw", "expected_release"}
    assert not {
        "result",
        "outcome",
        "response_sha256",
        "release_binding_sha256",
        "owner_smoke",
        "repo_root",
        "test_refs",
    } & set(produce.parameters)

    with pytest.raises(TypeError):
        evidence.produce_production_observation_bundle(  # type: ignore[call-arg]
            authenticated_binding=_binding(),
            result="VERIFIED",
        )

    binding = _binding()
    raw = evidence.produce_production_observation_bundle(
        authenticated_binding=binding,
    ).receipt
    legacy_calls = (
        lambda: evidence.produce_receipt(
            repo_root=tmp_path,
            release_root=tmp_path,
            journey_id="durable_scheduled_work",
            evidence_class="production read-only observation",
        ),
        lambda: evidence.produce_evidence_bundle(
            repo_root=tmp_path,
            release_root=tmp_path,
            journey_id="durable_scheduled_work",
            evidence_class="production read-only observation",
        ),
        lambda: evidence.validate_receipt(
            raw,
            expected_release=_RELEASE,
            expected_journey_id="durable_scheduled_work",
            expected_evidence_class="production read-only observation",
            repo_root=tmp_path,
        ),
        lambda: evidence.manifest_from_receipt(
            raw,
            expected_release=_RELEASE,
            expected_journey_id="durable_scheduled_work",
            expected_evidence_class="production read-only observation",
            repo_root=tmp_path,
        ),
    )
    for call in legacy_calls:
        with pytest.raises(
            evidence.ExactReleaseEvidenceError,
            match="production_observation_external_binding_required",
        ):
            call()


@pytest.mark.parametrize(
    "raw",
    (
        b'{"schema":"friday.production-read-only-release-captain-artifact.v1",'
        b'"schema":"friday.production-read-only-release-captain-artifact.v1"}',
        b'{"schema":NaN}',
        b"{}\n",
        b" " * 65_537,
    ),
    ids=("duplicate-key", "non-finite", "trailing-newline", "oversized"),
)
def test_release_captain_artifact_serialization_is_closed(raw: bytes) -> None:
    with pytest.raises(
        evidence.ExactReleaseEvidenceError,
        match="release_captain_observation_artifact_invalid",
    ):
        evidence.binding_from_release_captain_artifact(raw, expected_release=_RELEASE)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema", "friday.production-read-only-release-captain-artifact.v2"),
        ("release_binding_sha256", "9" * 64),
        ("endpoint_response_sha256", "9" * 64),
        ("challenge_sha256", "0" * 64),
        ("backend_process_epoch_sha256", "0" * 64),
        ("health_before_sha256", "0" * 64),
        ("health_after_sha256", "0" * 64),
    ),
    ids=(
        "schema",
        "release-binding",
        "endpoint-response",
        "challenge",
        "process-epoch",
        "health-before",
        "health-after",
    ),
)
def test_release_captain_artifact_cannot_self_declare_expected_hashes(
    field: str,
    value: str,
) -> None:
    artifact = _artifact_payload()
    artifact[field] = value
    with pytest.raises(evidence.ExactReleaseEvidenceError):
        evidence.binding_from_release_captain_artifact(
            evidence.canonical_json_bytes(artifact),
            expected_release=_RELEASE,
        )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.update(challenge_sha256="9" * 64),
        lambda value: value.update(backend_process_epoch_sha256="9" * 64),
        lambda value: value["database"].update(integrity="failed"),
        lambda value: value["database"].update(schema_version=49),
        lambda value: value["database"].update(schema_version=50.0),
        lambda value: value["database"].update(foreign_key_violations=False),
        lambda value: value["database"].update(schema_attestation_sha256="0" * 64),
        lambda value: value["database"].update(schema_attestation_sha256="2" * 64),
        lambda value: value.update(hard_contradictions=1),
        lambda value: value.update(hard_contradictions=False),
        lambda value: value["scheduled_work"]["missions"].update(ready=-1),
        lambda value: value["scheduled_work"]["mission_tasks"].update(pending=1 << 63),
        lambda value: value["scheduled_work"]["reminders"].update(invented=1),
        lambda value: value["scheduled_work"]["workers"].update(missing=1),
        lambda value: value["scheduled_work"]["workers"]["health_states"].update(ok=1),
        lambda value: value.update(private_body="must-not-be-accepted"),
    ),
    ids=(
        "challenge-drift",
        "process-epoch-drift",
        "integrity-failed",
        "schema-drift",
        "schema-float-alias",
        "foreign-key-bool-alias",
        "schema-attestation-zero",
        "schema-attestation-drift",
        "hard-contradiction",
        "hard-contradiction-bool-alias",
        "negative-count",
        "oversized-count",
        "unknown-reminder-state",
        "worker-cardinality",
        "worker-health-cardinality",
        "extra-private-field",
    ),
)
def test_endpoint_response_drift_and_unsuccessful_facts_fail_closed(mutation) -> None:
    endpoint = _endpoint_payload()
    mutation(endpoint)
    artifact = _artifact_payload(endpoint)
    with pytest.raises(evidence.ExactReleaseEvidenceError):
        evidence.binding_from_release_captain_artifact(
            evidence.canonical_json_bytes(artifact),
            expected_release=_RELEASE,
        )


def test_receipt_outcomes_and_hashes_are_rederived_and_bundle_only(tmp_path) -> None:
    binding = _binding()
    bundle = evidence.produce_production_observation_bundle(authenticated_binding=binding)
    receipt = json.loads(bundle.receipt)

    for mutate in (
        lambda value: value.update(result="FAILED"),
        lambda value: value["checks"][0].update(outcome="FAILED"),
        lambda value: value["observation"].update(endpoint_response_sha256="9" * 64),
        lambda value: value.update(owner_smoke=None),
    ):
        forged = json.loads(bundle.receipt)
        mutate(forged)
        with pytest.raises(evidence.ExactReleaseEvidenceError):
            evidence.validate_production_observation_receipt(
                evidence.canonical_json_bytes(forged),
                authenticated_binding=binding,
            )

    standalone = tmp_path / "standalone.json"
    with pytest.raises(
        evidence.ExactReleaseEvidenceError,
        match="production_observation_bundle_required",
    ):
        evidence.write_receipt_exclusive(standalone, bundle.receipt)
    assert not standalone.exists()

    bundle_root = tmp_path / "bundle"
    bundle_root.mkdir(mode=0o700)
    published = evidence.write_evidence_bundle_exclusive(bundle_root, bundle)
    assert published["result"] == "VERIFIED"
    assert published["receipt_sha256"] == bundle.receipt_sha256
    assert receipt["observation"]["endpoint_response_sha256"] == (
        hashlib.sha256(binding.endpoint_response).hexdigest()
    )
