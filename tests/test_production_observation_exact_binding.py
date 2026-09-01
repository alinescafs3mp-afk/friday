"""Exact-release binding for authenticated production read-only evidence."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import traceback
from dataclasses import replace
from pathlib import Path

import pytest

from friday.diagnostics.production_observation import (
    PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256,
)
from tools import exact_release_evidence as evidence
from tools import production_read_only_observation_operator as observation_operator

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
_OBSERVER_SOURCE = b"# exact production observer\n"
_OBSERVER_SHA256 = hashlib.sha256(_OBSERVER_SOURCE).hexdigest()


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
        "production_observation_operator_sha256": _OBSERVER_SHA256,
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
    return evidence.binding_from_release_captain_artifact(
        raw,
        expected_release=_RELEASE,
        expected_production_observation_operator_sha256=_OBSERVER_SHA256,
    )


def test_exact_schema_attestation_matches_the_runtime_observer_contract() -> None:
    assert evidence.PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256 == (
        PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256
    )
    assert observation_operator.PRODUCTION_SCHEDULED_WORK_SCHEMA_ATTESTATION_SHA256 == (
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
    with pytest.raises(evidence.ExactReleaseEvidenceError):
        evidence.binding_from_release_captain_artifact(
            evidence.canonical_json_bytes(_artifact_payload()),
            expected_release=_RELEASE,
            expected_production_observation_operator_sha256="3" * 64,
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
            production_observation_operator_sha256=(binding.production_observation_operator_sha256),
        )

    other_release = replace(_RELEASE, source_commit="8" * 40)
    with pytest.raises(evidence.ExactReleaseEvidenceError):
        evidence.binding_from_release_captain_artifact(
            evidence.canonical_json_bytes(_artifact_payload()),
            expected_release=other_release,
            expected_production_observation_operator_sha256=_OBSERVER_SHA256,
        )


def test_public_api_has_no_caller_result_or_hash_claims_and_no_owner_smoke_substitute(
    tmp_path: Path,
) -> None:
    produce = inspect.signature(evidence.produce_production_observation_bundle)
    validate = inspect.signature(evidence.validate_production_observation_bundle)
    factory = inspect.signature(evidence.binding_from_release_captain_artifact)
    private_factory = inspect.signature(evidence.binding_from_private_release_captain_artifact)

    assert set(produce.parameters) == {"authenticated_binding", "journey_id"}
    assert set(validate.parameters) == {
        "bundle",
        "authenticated_binding",
        "expected_journey_id",
    }
    assert set(factory.parameters) == {
        "raw",
        "expected_release",
        "expected_production_observation_operator_sha256",
    }
    assert set(private_factory.parameters) == {
        "artifact",
        "expected_artifact_sha256",
        "expected_release",
        "expected_production_observation_operator_sha256",
    }
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


def test_private_release_captain_artifact_requires_exact_custody_and_publisher_digest(
    tmp_path: Path,
) -> None:
    authority_root = tmp_path / "release-captain"
    authority_root.mkdir(mode=0o700)
    authority_root.chmod(0o700)
    raw = evidence.canonical_json_bytes(_artifact_payload())
    artifact = authority_root / "production-observation.json"
    artifact.write_bytes(raw)
    artifact.chmod(0o400)
    digest = hashlib.sha256(raw).hexdigest()

    binding = evidence.binding_from_private_release_captain_artifact(
        artifact,
        expected_artifact_sha256=digest,
        expected_release=_RELEASE,
        expected_production_observation_operator_sha256=_OBSERVER_SHA256,
    )
    assert binding == _binding()
    with pytest.raises(
        evidence.ExactReleaseEvidenceError,
        match="release_captain_observation_authority_invalid",
    ):
        evidence.binding_from_private_release_captain_artifact(
            artifact,
            expected_artifact_sha256="9" * 64,
            expected_release=_RELEASE,
            expected_production_observation_operator_sha256=_OBSERVER_SHA256,
        )

    private_sentinel = "never-log-private-observation-path"
    with pytest.raises(
        evidence.ExactReleaseEvidenceError,
        match="release_captain_observation_authority_invalid",
    ) as missing:
        evidence.binding_from_private_release_captain_artifact(
            authority_root / private_sentinel,
            expected_artifact_sha256=digest,
            expected_release=_RELEASE,
            expected_production_observation_operator_sha256=_OBSERVER_SHA256,
        )
    assert private_sentinel not in "".join(traceback.format_exception(missing.value))

    alias = authority_root / "hardlink.json"
    alias.hardlink_to(artifact)
    with pytest.raises(
        evidence.ExactReleaseEvidenceError,
        match="release_captain_observation_authority_invalid",
    ):
        evidence.binding_from_private_release_captain_artifact(
            artifact,
            expected_artifact_sha256=digest,
            expected_release=_RELEASE,
            expected_production_observation_operator_sha256=_OBSERVER_SHA256,
        )
    alias.unlink()

    symlink = authority_root / "symlink.json"
    symlink.symlink_to(artifact)
    for invalid in (Path("relative.json"), symlink):
        with pytest.raises(
            evidence.ExactReleaseEvidenceError,
            match="release_captain_observation_authority_invalid",
        ):
            evidence.binding_from_private_release_captain_artifact(
                invalid,
                expected_artifact_sha256=digest,
                expected_release=_RELEASE,
                expected_production_observation_operator_sha256=_OBSERVER_SHA256,
            )

    artifact.chmod(0o600)
    with pytest.raises(
        evidence.ExactReleaseEvidenceError,
        match="release_captain_observation_authority_invalid",
    ):
        evidence.binding_from_private_release_captain_artifact(
            artifact,
            expected_artifact_sha256=digest,
            expected_release=_RELEASE,
            expected_production_observation_operator_sha256=_OBSERVER_SHA256,
        )


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
        evidence.binding_from_release_captain_artifact(
            raw,
            expected_release=_RELEASE,
            expected_production_observation_operator_sha256=_OBSERVER_SHA256,
        )


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
            expected_production_observation_operator_sha256=_OBSERVER_SHA256,
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
            expected_production_observation_operator_sha256=_OBSERVER_SHA256,
        )


def test_receipt_outcomes_and_hashes_are_rederived_and_bundle_only(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
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

    authority_root = tmp_path / "release-captain"
    authority_root.mkdir(mode=0o700)
    artifact = authority_root / "production-observation.json"
    artifact_raw = evidence.canonical_json_bytes(_artifact_payload())
    artifact.write_bytes(artifact_raw)
    artifact.chmod(0o400)
    artifact_sha256 = hashlib.sha256(artifact_raw).hexdigest()
    cli_root = tmp_path / "cli-bundle"
    cli_root.mkdir(mode=0o700)
    monkeypatch.setattr(evidence, "_require_producer_process_authority", lambda: None)
    monkeypatch.setattr(
        evidence,
        "_running_exact_checkout",
        lambda: (evidence.ROOT, _RELEASE.source_commit),
    )
    monkeypatch.setattr(
        evidence,
        "_exact_git_blob",
        lambda _root, _commit, path: (
            _OBSERVER_SOURCE if path == evidence.PRODUCTION_OBSERVATION_OPERATOR_PATH else b"unexpected"
        ),
    )

    assert (
        evidence.main(
            [
                "production-bundle",
                "--artifact",
                str(artifact),
                "--expected-artifact-sha256",
                artifact_sha256,
                "--expected-source-commit",
                _RELEASE.source_commit,
                "--expected-tree-sha256",
                _RELEASE.tree_sha256,
                "--expected-wheel-sha256",
                _RELEASE.wheel_sha256,
                "--expected-database-schema",
                str(_RELEASE.database_schema),
                "--output-root",
                str(cli_root),
            ]
        )
        == 0
    )
    published_cli = json.loads(capsys.readouterr().out)
    assert published_cli == {
        "manifest_ref": bundle.manifest_ref,
        "manifest_sha256": bundle.manifest_sha256,
        "receipt_ref": bundle.receipt_ref,
        "receipt_sha256": bundle.receipt_sha256,
        "result": "VERIFIED",
    }
    assert (cli_root / bundle.manifest_ref).read_bytes() == bundle.manifest
    assert (cli_root / bundle.receipt_ref).read_bytes() == bundle.receipt
    assert str(artifact) not in json.dumps(published_cli)

    failed_root = tmp_path / "failed-cli-bundle"
    failed_root.mkdir(mode=0o700)
    failed_argv = [
        "production-bundle",
        "--artifact",
        str(artifact),
        "--expected-artifact-sha256",
        "9" * 64,
        "--expected-source-commit",
        _RELEASE.source_commit,
        "--expected-tree-sha256",
        _RELEASE.tree_sha256,
        "--expected-wheel-sha256",
        _RELEASE.wheel_sha256,
        "--expected-database-schema",
        str(_RELEASE.database_schema),
        "--output-root",
        str(failed_root),
    ]
    assert evidence.main(failed_argv) == 2
    failure = capsys.readouterr().out
    assert json.loads(failure) == {
        "failure_code": "release_captain_observation_authority_invalid",
        "status": "failed_closed",
    }
    assert str(artifact) not in failure
    assert list(failed_root.iterdir()) == []

    substituted_artifact = authority_root / "substituted-production-observation.json"
    substituted_payload = _artifact_payload()
    substituted_payload["production_observation_operator_sha256"] = "3" * 64
    substituted_raw = evidence.canonical_json_bytes(substituted_payload)
    substituted_artifact.write_bytes(substituted_raw)
    substituted_artifact.chmod(0o400)
    substituted_root = tmp_path / "substituted-cli-bundle"
    substituted_root.mkdir(mode=0o700)
    substituted_argv = [
        str(substituted_artifact)
        if value == str(artifact)
        else hashlib.sha256(substituted_raw).hexdigest()
        if value == "9" * 64
        else str(substituted_root)
        if value == str(failed_root)
        else value
        for value in failed_argv
    ]
    assert evidence.main(substituted_argv) == 2
    substituted_failure = capsys.readouterr().out
    assert json.loads(substituted_failure) == {
        "failure_code": "release_captain_observation_artifact_invalid",
        "status": "failed_closed",
    }
    assert str(substituted_artifact) not in substituted_failure
    assert list(substituted_root.iterdir()) == []

    wrong_head_root = tmp_path / "wrong-head-cli-bundle"
    wrong_head_root.mkdir(mode=0o700)
    wrong_head_argv = [
        artifact_sha256 if value == "9" * 64 else str(wrong_head_root) if value == str(failed_root) else value
        for value in failed_argv
    ]
    artifact_reads: list[object] = []
    monkeypatch.setattr(
        evidence,
        "_running_exact_checkout",
        lambda: (evidence.ROOT, "8" * 40),
    )
    monkeypatch.setattr(
        evidence,
        "binding_from_private_release_captain_artifact",
        lambda *_args, **_kwargs: artifact_reads.append((_args, _kwargs)),
    )
    assert evidence.main(wrong_head_argv) == 2
    wrong_head_failure = capsys.readouterr().out
    assert json.loads(wrong_head_failure) == {
        "failure_code": "production_observation_release_invalid",
        "status": "failed_closed",
    }
    assert str(artifact) not in wrong_head_failure
    assert artifact_reads == []
    assert list(wrong_head_root.iterdir()) == []
